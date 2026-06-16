"""Tests for the /api REST layer.

These exercise the full request → state-transition → enqueue flow without a broker
or the heavy pipeline: the Celery queue is replaced by a :class:`FakeQueue` that
just records what *would* run (mirroring how /ingest tests inject a ``FakeCamera``),
and the job store is pointed at a per-test ``tmp_path``. So we assert the contract
of every endpoint — status machine, validation, enqueue calls — fast and offline.

The actual pipeline tasks (render/segment/deliver) are covered by the per-stage
tests; here we only verify the API drives them correctly.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app, get_queue, get_store
from api.jobs import ADJUSTMENTS_FILENAME, JobStatus, JobStore
from edl.schema import Clip, EditDecisionList
from edl.storage import edl_path, job_dir
from render.render import FINAL_FILENAME


class FakeQueue:
    """A :class:`~api.queue.JobQueue` that records calls instead of dispatching."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def enqueue_processing(self, job_id: str) -> None:
        self.calls.append(("processing", (job_id,)))

    def enqueue_selfie_processing(self, job_id: str) -> None:
        self.calls.append(("selfie", (job_id,)))

    def enqueue_rerender(self, job_id: str) -> None:
        self.calls.append(("rerender", (job_id,)))

    def enqueue_delivery(self, job_id: str) -> None:
        self.calls.append(("delivery", (job_id,)))

    def enqueue_pull(self, job_id: str, camera_id: str) -> None:
        self.calls.append(("pull", (job_id, camera_id)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def client(tmp_path: Path, queue: FakeQueue) -> Iterator[TestClient]:
    """A TestClient with the store rooted in tmp_path and the queue faked."""
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    with TestClient(app) as c:
        c.jobs_root = tmp_path  # stash the root for assertions
        yield c
    app.dependency_overrides.clear()


def _create(client: TestClient, **body: object) -> str:
    resp = client.post("/jobs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["job_id"]


def _mark(client: TestClient, job_id: str, status: JobStatus) -> None:
    """Force a job into ``status`` (stand-in for the worker finishing a stage)."""
    JobStore(client.jobs_root).update(job_id, status=status)


# --------------------------------------------------------------------------- #
# Create + fetch
# --------------------------------------------------------------------------- #


def test_create_job_returns_id_and_defaults(client: TestClient) -> None:
    resp = client.post("/jobs", json={"customer_name": "Jane Doe", "jump_date": "2026-06-02"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["job_id"]
    assert body["job"]["status"] == "queued"
    assert body["job"]["customer_name"] == "Jane Doe"
    assert body["job"]["target_duration"] == 90.0  # default applied
    # New booking fields default sensibly and appear in the response.
    assert body["job"]["package"] == "selfie"  # default package
    assert body["job"]["booking_id"] is None
    assert body["job"]["outputs"] is None  # no deliverables until status == ready


def test_create_job_stores_package_and_booking_id(client: TestClient) -> None:
    resp = client.post(
        "/jobs",
        json={"customer_name": "Alex", "package": "ultimum", "booking_id": "BK-77"},
    )
    assert resp.status_code == 201
    job = resp.json()["job"]
    assert job["package"] == "ultimum"
    assert job["booking_id"] == "BK-77"
    # Existing fields still behave exactly as before.
    assert job["customer_name"] == "Alex"
    assert job["target_duration"] == 90.0


def test_create_job_rejects_unknown_package(client: TestClient) -> None:
    resp = client.post("/jobs", json={"package": "not-a-package"})
    assert resp.status_code == 422


def test_create_job_rejects_unknown_field(client: TestClient) -> None:
    resp = client.post("/jobs", json={"nope": 1})
    assert resp.status_code == 422


def test_get_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_get_job_roundtrips(client: TestClient) -> None:
    job_id = _create(client, customer_name="Ann")
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["customer_name"] == "Ann"


def test_get_job_includes_outputs_when_ready(client: TestClient) -> None:
    job_id = _create(client)
    # Before ready, outputs is null.
    assert client.get(f"/jobs/{job_id}").json()["outputs"] is None

    outputs = {
        "full_video": f"/jobs/{job_id}/full_video.mp4",
        "highlights": f"/jobs/{job_id}/highlights.mp4",
        "freefall": f"/jobs/{job_id}/freefall.mp4",
        "photos": f"/jobs/{job_id}/photos/",
    }
    JobStore(client.jobs_root).update(job_id, status=JobStatus.ready, outputs=outputs)

    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "ready"
    assert body["outputs"] == outputs


def _seed_ready_outputs(client: TestClient, job_id: str) -> None:
    """Put a job in ``ready`` with two video deliverables + a photo set on disk."""
    jd = job_dir(job_id, client.jobs_root)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"FULL-VIDEO-BYTES")
    (jd / "highlights.mp4").write_bytes(b"HL-BYTES")
    photos = jd / "photos"
    photos.mkdir(exist_ok=True)
    (photos / "freefall_42.jpg").write_bytes(b"JPEGDATA")
    (photos / "index.json").write_text(
        json.dumps([{"filename": "freefall_42.jpg", "scene": "freefall", "ts": 42.0, "score": 0.9}])
    )
    JobStore(client.jobs_root).update(
        job_id,
        status=JobStatus.ready,
        outputs={
            "full_video": str(jd / "full_video.mp4"),
            "highlights": str(jd / "highlights.mp4"),
            "photos": str(photos),
        },
    )


def test_list_deliverables_returns_urls(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    _seed_ready_outputs(client, job_id)

    body = client.get(f"/jobs/{job_id}/deliverables").json()
    assert body["status"] == "ready"
    by_name = {d["name"]: d for d in body["deliverables"]}
    assert by_name["full_video"] == {
        "name": "full_video", "kind": "video",
        "url": f"/jobs/{job_id}/deliverables/full_video", "media_type": "video/mp4",
    }
    assert by_name["photos"]["kind"] == "photos"
    assert by_name["photos"]["url"] == f"/jobs/{job_id}/photos"


def test_get_video_deliverable_streams_file(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    _seed_ready_outputs(client, job_id)

    resp = client.get(f"/jobs/{job_id}/deliverables/full_video")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b"FULL-VIDEO-BYTES"


def test_get_deliverable_rejects_photos_and_unknown(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    _seed_ready_outputs(client, job_id)

    # "photos" is not a streamable video deliverable.
    assert client.get(f"/jobs/{job_id}/deliverables/photos").status_code == 404
    # A name the job never produced.
    assert client.get(f"/jobs/{job_id}/deliverables/freefall").status_code == 404


def test_list_and_fetch_photos(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    _seed_ready_outputs(client, job_id)

    listing = client.get(f"/jobs/{job_id}/photos").json()
    assert listing["count"] == 1
    photo = listing["photos"][0]
    assert photo["filename"] == "freefall_42.jpg"
    assert photo["url"] == f"/jobs/{job_id}/photos/freefall_42.jpg"
    assert photo["scene"] == "freefall"

    img = client.get(f"/jobs/{job_id}/photos/freefall_42.jpg")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/jpeg"
    assert img.content == b"JPEGDATA"


def test_photos_404_when_none(client: TestClient) -> None:
    job_id = _create(client)  # default selfie job, no photos produced
    assert client.get(f"/jobs/{job_id}/photos").status_code == 404
    assert client.get(f"/jobs/{job_id}/photos/missing.jpg").status_code == 404


def test_deliverable_endpoints_404_for_unknown_job(client: TestClient) -> None:
    assert client.get("/jobs/nope/deliverables").status_code == 404
    assert client.get("/jobs/nope/deliverables/full_video").status_code == 404
    assert client.get("/jobs/nope/photos").status_code == 404


def test_get_photo_rejects_traversal_segment() -> None:
    # Defence-in-depth guard used by the photo endpoint.
    from api.app import _is_safe_segment

    assert _is_safe_segment("freefall_42.jpg")
    assert not _is_safe_segment("..")
    assert not _is_safe_segment("../job.json")
    assert not _is_safe_segment("a/b.jpg")
    assert not _is_safe_segment("")


# --------------------------------------------------------------------------- #
# Per-deliverable music
# --------------------------------------------------------------------------- #


def test_music_slots_depend_on_package(client: TestClient) -> None:
    # Ultimum exposes its four video deliverables; photo_only exposes none.
    ult = _create(client, package="ultimum")
    slots = client.get(f"/jobs/{ult}/music").json()["slots"]
    assert [s["deliverable"] for s in slots] == [
        "full_video", "highlights", "external_freefall", "chute_libre_selfie"
    ]
    assert slots[0]["label"] == "Full Video Music"
    assert all(s["filename"] is None for s in slots)  # nothing uploaded yet

    photo = _create(client, package="photo_only")
    assert client.get(f"/jobs/{photo}/music").json()["slots"] == []


def test_upload_music_stores_lists_and_fetches(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    resp = client.post(
        f"/jobs/{job_id}/music",
        data={"deliverable": "full_video"},
        files={"file": ("mytrack.mp3", b"AUDIO-BYTES", "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "full_video.mp3"  # stored keyed by deliverable

    # It lands under jobs/<id>/music/ (not the global templates folder).
    stored = job_dir(job_id, client.jobs_root) / "music" / "full_video.mp3"
    assert stored.read_bytes() == b"AUDIO-BYTES"

    slot = next(s for s in client.get(f"/jobs/{job_id}/music").json()["slots"]
                if s["deliverable"] == "full_video")
    assert slot["filename"] == "full_video.mp3"
    assert slot["url"] == f"/jobs/{job_id}/music/full_video"

    fetched = client.get(f"/jobs/{job_id}/music/full_video")
    assert fetched.status_code == 200
    assert fetched.content == b"AUDIO-BYTES"


def test_upload_music_replaces_previous_track(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    client.post(f"/jobs/{job_id}/music", data={"deliverable": "highlights"},
                files={"file": ("a.mp3", b"first", "audio/mpeg")})
    client.post(f"/jobs/{job_id}/music", data={"deliverable": "highlights"},
                files={"file": ("b.wav", b"second", "audio/wav")})

    mdir = job_dir(job_id, client.jobs_root) / "music"
    # The .mp3 is replaced by the .wav — only one track remains for the deliverable.
    assert {p.name for p in mdir.glob("highlights.*")} == {"highlights.wav"}
    assert client.get(f"/jobs/{job_id}/music/highlights").content == b"second"


def test_upload_music_rejects_bad_deliverable_and_non_audio(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    # "freefall" is a selfie deliverable, not an ultimum one.
    bad = client.post(f"/jobs/{job_id}/music", data={"deliverable": "freefall"},
                      files={"file": ("a.mp3", b"x", "audio/mpeg")})
    assert bad.status_code == 422
    notaudio = client.post(f"/jobs/{job_id}/music", data={"deliverable": "full_video"},
                           files={"file": ("notes.txt", b"x", "text/plain")})
    assert notaudio.status_code == 422


def test_delete_music_reverts_to_template(client: TestClient) -> None:
    job_id = _create(client, package="ultimum")
    client.post(f"/jobs/{job_id}/music", data={"deliverable": "full_video"},
                files={"file": ("a.mp3", b"x", "audio/mpeg")})

    resp = client.delete(f"/jobs/{job_id}/music/full_video")
    assert resp.status_code == 200
    slot = next(s for s in resp.json()["slots"] if s["deliverable"] == "full_video")
    assert slot["filename"] is None  # back to template fallback
    assert client.get(f"/jobs/{job_id}/music/full_video").status_code == 404
    # Deleting again (nothing there) is a 404.
    assert client.delete(f"/jobs/{job_id}/music/full_video").status_code == 404


def test_music_endpoints_404_for_unknown_job(client: TestClient) -> None:
    assert client.get("/jobs/nope/music").status_code == 404
    assert client.get("/jobs/nope/music/full_video").status_code == 404


# --------------------------------------------------------------------------- #
# Upload (file + camera pull)
# --------------------------------------------------------------------------- #


def test_upload_single_file_backward_compatible(client: TestClient, queue: FakeQueue) -> None:
    # The legacy single-file field still works; a default (selfie) job routes to the
    # selfie pipeline and the clip is staged under raw/ with its original name.
    job_id = _create(client)
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files={"file": ("GH010123.MP4", b"fake mp4 bytes", "video/mp4")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "upload"
    assert body["package"] == "selfie"
    assert body["files_received"] == 1
    assert queue.kinds() == ["selfie"]

    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"
    raw = job_dir(job_id, client.jobs_root) / "raw" / "GH010123.MP4"
    assert raw.read_bytes() == b"fake mp4 bytes"


def test_upload_multiple_files_saved_to_raw(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client, customer_name="Mia", jump_date="2026-06-02", booking_id="BK-9")
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[
            ("files", ("GH010001.MP4", b"one", "video/mp4")),
            ("files", ("GH020001.MP4", b"two", "video/mp4")),
            ("files", ("GH030001.MP4", b"three", "video/mp4")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["files_received"] == 3
    assert body["detail"] == "received 3 files; processing enqueued"
    assert queue.kinds() == ["selfie"]

    raw_dir = job_dir(job_id, client.jobs_root) / "raw"
    assert {p.name for p in raw_dir.iterdir()} == {
        "GH010001.MP4", "GH020001.MP4", "GH030001.MP4"
    }
    # booking.json sidecar is written for the selfie pipeline to read back.
    booking = json.loads((job_dir(job_id, client.jobs_root) / "booking.json").read_text())
    assert booking == {
        "booking_id": "BK-9",
        "customer_name": "Mia",
        "jump_date": "2026-06-02",
        "package": "selfie",
        "music": None,
    }


def test_upload_ultimum_requires_camera_role(client: TestClient, queue: FakeQueue) -> None:
    # The two-camera Ultimate package needs each upload tagged with a camera_role.
    job_id = _create(client, package="ultimum")
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"x", "video/mp4"))],
    )
    assert resp.status_code == 422
    assert queue.calls == []  # nothing enqueued without a camera_role


def test_upload_ultimum_waits_for_both_cameras_then_enqueues(
    client: TestClient, queue: FakeQueue
) -> None:
    # First camera stages and waits; the second triggers the scene pipeline once.
    job_id = _create(client, package="ultimum")

    first = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"instructor", "video/mp4"))],
        data={"camera_role": "instructor"},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["camera_role"] == "instructor"
    assert "waiting" in body["detail"]
    assert queue.calls == []  # not enqueued yet — only one camera present

    # Clips land in raw/instructor/ (not the flat raw/), avoiding GoPro name collisions.
    raw = job_dir(job_id, client.jobs_root) / "raw"
    assert (raw / "instructor" / "GH010001.MP4").read_bytes() == b"instructor"

    second = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"external", "video/mp4"))],
        data={"camera_role": "external"},
    )
    assert second.status_code == 200
    assert second.json()["camera_role"] == "external"
    assert (raw / "external" / "GH010001.MP4").read_bytes() == b"external"
    assert queue.kinds() == ["selfie"]  # both cameras in → scene pipeline enqueued once


def test_upload_ultimum_rejects_unknown_camera_role(
    client: TestClient, queue: FakeQueue
) -> None:
    job_id = _create(client, package="ultimum")
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"x", "video/mp4"))],
        data={"camera_role": "drone"},
    )
    assert resp.status_code == 422
    assert queue.calls == []


@pytest.mark.parametrize("package", ["selfie", "external", "video_only", "photo_only"])
def test_upload_scene_pipeline_packages_enqueue_selfie_processing(
    client: TestClient, queue: FakeQueue, package: str
) -> None:
    # selfie, external, video_only, and photo_only all run through the scene pipeline;
    # which deliverables they emit is decided inside the pipeline, not at enqueue time.
    job_id = _create(client, package=package)
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"x", "video/mp4"))],
    )
    assert resp.status_code == 200
    assert resp.json()["package"] == package
    assert queue.kinds() == ["selfie"]


def test_upload_empty_files_is_422(client: TestClient) -> None:
    job_id = _create(client)
    resp = client.post(f"/jobs/{job_id}/upload")
    assert resp.status_code == 422


def test_upload_non_mp4_is_422(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("notes.txt", b"not a video", "text/plain"))],
    )
    assert resp.status_code == 422
    assert queue.calls == []  # nothing enqueued on a rejected upload


def test_upload_accepts_mp4_with_lrv_proxy(client: TestClient, queue: FakeQueue) -> None:
    # An LRV proxy may be uploaded beside its MP4; both are staged in raw/, and the
    # job's source_path stays the MP4 master (the LRV is analysis-only).
    job_id = _create(client)
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[
            ("files", ("GX010001.MP4", b"master", "video/mp4")),
            # LRV deliberately sent with a video/mp4 MIME — it must still be treated as
            # a proxy (extension wins), never as the renderable master.
            ("files", ("GL010001.LRV", b"proxy", "video/mp4")),
        ],
    )
    assert resp.status_code == 200
    assert resp.json()["files_received"] == 2
    assert queue.kinds() == ["selfie"]

    raw_dir = job_dir(job_id, client.jobs_root) / "raw"
    assert {p.name for p in raw_dir.iterdir()} == {"GX010001.MP4", "GL010001.LRV"}
    # source_path is the MP4 master, never the LRV proxy.
    job = json.loads((job_dir(job_id, client.jobs_root) / "job.json").read_text())
    assert job["source_path"].endswith("GX010001.MP4")


def test_upload_lrv_only_is_422(client: TestClient, queue: FakeQueue) -> None:
    # A proxy alone cannot be rendered/delivered — reject with a clear message.
    job_id = _create(client)
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GL010001.LRV", b"proxy", "video/mp4"))],
    )
    assert resp.status_code == 422
    assert queue.calls == []


def test_upload_unknown_job_is_404(client: TestClient) -> None:
    resp = client.post(
        "/jobs/does-not-exist/upload",
        files=[("files", ("GH010001.MP4", b"x", "video/mp4"))],
    )
    assert resp.status_code == 404


def test_upload_camera_id_triggers_pull(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    resp = client.post(f"/jobs/{job_id}/upload", data={"camera_id": "1234"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "pull"
    assert queue.calls == [("pull", (job_id, "1234"))]
    assert client.get(f"/jobs/{job_id}").json()["camera_id"] == "1234"


def test_upload_to_processing_job_conflicts(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.processing)
    resp = client.post(
        f"/jobs/{job_id}/upload", files=[("files", ("a.MP4", b"x", "video/mp4"))]
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Review gate: approve / reject / tweak
# --------------------------------------------------------------------------- #


def test_approve_requires_ready_for_review(client: TestClient) -> None:
    job_id = _create(client)  # still queued
    assert client.post(f"/jobs/{job_id}/approve").status_code == 409


def test_approve_marks_approved_and_enqueues_delivery(
    client: TestClient, queue: FakeQueue
) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    resp = client.post(f"/jobs/{job_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert queue.kinds() == ["delivery"]


def test_reject_records_reason_and_requeues(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    resp = client.post(f"/jobs/{job_id}/reject", json={"reason": "face out of frame"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["reject_reason"] == "face out of frame"
    assert queue.kinds() == ["processing"]
    # The rejection is logged as a training signal.
    log = job_dir(job_id, client.jobs_root) / ADJUSTMENTS_FILENAME
    assert "face out of frame" in log.read_text()


def test_reject_requires_reason(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    assert client.post(f"/jobs/{job_id}/reject", json={"reason": ""}).status_code == 422
    assert client.post(f"/jobs/{job_id}/reject", json={}).status_code == 422


def test_tweak_persists_edl_logs_and_rerenders(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    edl = EditDecisionList(clips=[Clip(src_start=1.0, src_end=5.0, speed_multiplier=0.4)])
    resp = client.post(
        f"/jobs/{job_id}/tweak",
        json={"edl": edl.model_dump(mode="json"), "note": "slow the exit"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert queue.kinds() == ["rerender"]
    # The new EDL replaced edl.json and the tweak was logged.
    saved = EditDecisionList.model_validate_json(
        edl_path(job_id, client.jobs_root).read_text()
    )
    assert saved.clips[0].speed_multiplier == 0.4
    log = job_dir(job_id, client.jobs_root) / ADJUSTMENTS_FILENAME
    assert "slow the exit" in log.read_text()


def test_tweak_rejects_invalid_edl(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    # src_end <= src_start violates the EDL schema.
    bad = {"clips": [{"src_start": 5.0, "src_end": 1.0}]}
    assert client.post(f"/jobs/{job_id}/tweak", json={"edl": bad}).status_code == 422


def test_tweak_before_render_conflicts(client: TestClient) -> None:
    job_id = _create(client)  # queued, nothing rendered yet
    edl = EditDecisionList(clips=[Clip(src_start=0.0, src_end=2.0)])
    resp = client.post(f"/jobs/{job_id}/tweak", json={"edl": edl.model_dump(mode="json")})
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# EDL read-back (the review UI's timeline)
# --------------------------------------------------------------------------- #


def test_get_edl_returns_persisted_edit(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    edl = EditDecisionList(
        clips=[Clip(src_start=1.0, src_end=5.0, speed_multiplier=0.4)],
        music="sunrise",
    )
    JobStore(client.jobs_root).save_edl(job_id, edl)

    resp = client.get(f"/jobs/{job_id}/edl")
    assert resp.status_code == 200
    body = resp.json()
    assert body["music"] == "sunrise"
    assert body["clips"][0]["src_start"] == 1.0
    assert body["clips"][0]["speed_multiplier"] == 0.4
    # Round-trips back through the schema (so the UI can POST it to /tweak as-is).
    assert EditDecisionList.model_validate(body).clips[0].src_end == 5.0


def test_get_edl_before_compose_is_404(client: TestClient) -> None:
    job_id = _create(client)  # no edl.json written yet
    assert client.get(f"/jobs/{job_id}/edl").status_code == 404


def test_get_edl_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/does-not-exist/edl").status_code == 404


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #


def test_preview_streams_rendered_file(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)
    final = job_dir(job_id, client.jobs_root) / FINAL_FILENAME
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00\x00\x00 ftypisom rendered")
    resp = client.get(f"/jobs/{job_id}/preview")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b"\x00\x00\x00 ftypisom rendered"


def test_preview_before_review_conflicts(client: TestClient) -> None:
    job_id = _create(client)
    assert client.get(f"/jobs/{job_id}/preview").status_code == 409


def test_preview_missing_file_is_404(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.ready_for_review)  # status says ready, but no file
    assert client.get(f"/jobs/{job_id}/preview").status_code == 404


# --------------------------------------------------------------------------- #
# OpenAPI docs
# --------------------------------------------------------------------------- #


def test_openapi_documents_all_endpoints(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert {"/jobs", "/jobs/{job_id}", "/jobs/{job_id}/upload",
            "/jobs/{job_id}/edl", "/jobs/{job_id}/approve", "/jobs/{job_id}/reject",
            "/jobs/{job_id}/tweak", "/jobs/{job_id}/preview"} <= set(paths)
    assert spec["info"]["title"] == "SkydiveOS Auto-Edit API"
