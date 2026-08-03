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
from api.config import get_settings
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

    def enqueue_s3_ingest(
        self, job_id: str, s3_key: str, camera_role: str | None = None
    ) -> None:
        self.calls.append(("s3_ingest", (job_id, s3_key, camera_role)))

    def arm_ultimum_watchdog(self, job_id: str, countdown: float) -> None:
        self.calls.append(("ultimum_watchdog", (job_id, countdown)))

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


def _create(client: TestClient, *, headers: dict[str, str] | None = None, **body: object) -> str:
    resp = client.post("/jobs", json=body, headers=headers)
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


def test_create_job_stores_customer_email(client: TestClient) -> None:
    resp = client.post(
        "/jobs", json={"customer_name": "Jane", "customer_email": "jane@example.com"}
    )
    assert resp.status_code == 201
    job = resp.json()["job"]
    assert job["customer_email"] == "jane@example.com"
    assert job["delivery_links"] is None  # nothing sent until status == delivered


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
        "customer_email": None,
        "instructor_name": None,
        "jump_date": "2026-06-02",
        "package": "selfie",
        "music": None,
    }


def test_upload_files_land_in_the_jump_archive(client: TestClient, tmp_path: Path) -> None:
    """An upload also files the masters under {date}/{instructor}/{customer} in raw-storage.

    The archive tree itself is covered in ``test_archive.py``; this pins the wiring —
    that the endpoint actually reaches it, with the booking's names.
    """
    job_id = _create(
        client,
        customer_name="Marie Dupont",
        instructor_name="Marc Tremblay",
        jump_date="2026-06-02",
    )
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"master", "video/mp4"))],
    )
    assert resp.status_code == 200

    jump_dir = tmp_path / "raw-storage" / "2026-06-02" / "Marc-Tremblay" / "Marie-Dupont"
    assert (jump_dir / "raw" / "GH010001.MP4").read_bytes() == b"master"
    manifest = json.loads((jump_dir / "manifest.json").read_text())
    assert manifest["job_id"] == job_id
    assert manifest["instructor_name"] == "Marc Tremblay"


def test_upload_ultimum_files_both_cameras_into_one_jump_folder(
    client: TestClient, tmp_path: Path
) -> None:
    job_id = _create(
        client,
        package="ultimum",
        customer_name="Marie Dupont",
        instructor_name="Marc Tremblay",
        jump_date="2026-06-02",
    )
    for role, payload in (("instructor", b"selfie"), ("external", b"cameraman")):
        resp = client.post(
            f"/jobs/{job_id}/upload",
            files=[("files", ("GH010001.MP4", payload, "video/mp4"))],
            data={"camera_role": role},
        )
        assert resp.status_code == 200

    raw = tmp_path / "raw-storage" / "2026-06-02" / "Marc-Tremblay" / "Marie-Dupont" / "raw"
    # Both angles under one jump, each in its own role folder (colliding GoPro names).
    assert (raw / "instructor" / "GH010001.MP4").read_bytes() == b"selfie"
    assert (raw / "external" / "GH010001.MP4").read_bytes() == b"cameraman"


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
    # Not processed yet — only one camera present. But the stranded-job watchdog MUST be
    # armed here: without it a second camera that never uploads leaves the job in
    # `queued` forever (the upload path used to skip this, unlike the S3-ingest path).
    assert queue.kinds() == ["ultimum_watchdog"]
    assert queue.calls[0][1][0] == job_id

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
    # Both cameras in → scene pipeline enqueued once; no second watchdog armed.
    assert queue.kinds() == ["ultimum_watchdog", "selfie"]


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


def test_upload_s3_key_enqueues_ingest(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)  # default selfie package
    resp = client.post(
        f"/jobs/{job_id}/upload", data={"s3_key": "raw/1234/GH010001.MP4"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "s3"
    assert queue.calls == [("s3_ingest", (job_id, "raw/1234/GH010001.MP4", None))]
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"
    # Booking sidecar is written so the scene pipeline can read it back.
    booking = json.loads((job_dir(job_id, client.jobs_root) / "booking.json").read_text())
    assert booking["package"] == "selfie"


def test_upload_s3_key_non_mp4_is_422(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    resp = client.post(f"/jobs/{job_id}/upload", data={"s3_key": "raw/1234/notes.txt"})
    assert resp.status_code == 422
    assert queue.calls == []


def test_upload_s3_key_ultimum_requires_role(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client, package="ultimum")
    # Without camera_role → 422
    bad = client.post(f"/jobs/{job_id}/upload", data={"s3_key": "raw/1/GH010001.MP4"})
    assert bad.status_code == 422
    assert queue.calls == []
    # With camera_role → enqueued, role echoed back.
    ok = client.post(
        f"/jobs/{job_id}/upload",
        data={"s3_key": "raw/1/GH010001.MP4", "camera_role": "instructor"},
    )
    assert ok.status_code == 200
    assert ok.json()["camera_role"] == "instructor"
    assert queue.calls == [("s3_ingest", (job_id, "raw/1/GH010001.MP4", "instructor"))]


def test_upload_no_source_is_422(client: TestClient) -> None:
    job_id = _create(client)
    resp = client.post(f"/jobs/{job_id}/upload", data={})
    assert resp.status_code == 422
    assert "s3_key" in resp.json()["detail"]


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


# --------------------------------------------------------------------------- #
# Entitlement: Path A / Path B, the paywall unlock, and the /j/{code} gallery
# --------------------------------------------------------------------------- #


#: Every unlock must carry proof of a captured payment (F-16).
_PAYMENT_BODY = {"payment_reference": "clover_txn_test"}


def _token(client: TestClient, job_id: str) -> str:
    """The job's gallery short code (minted at creation)."""
    token = JobStore(client.jobs_root).load(job_id).gallery_token
    assert token, "every job should carry a gallery token from creation"
    return token


def test_new_job_defaults_to_edited_download_and_gets_a_short_code(
    client: TestClient,
) -> None:
    job_id = _create(client, customer_name="Sophie")
    job = client.get(f"/jobs/{job_id}").json()
    assert job["entitlement"] == "edited_download"  # Path A unless told otherwise
    assert job["paid_at"] is None
    token = _token(client, job_id)
    assert 10 <= len(token) <= 12 and token.isalnum()  # SMS-short, base62
    # The secret must not leak through the public job view.
    assert "gallery_token" not in job


def test_create_job_accepts_preview_only_in_either_casing(client: TestClient) -> None:
    lower = _create(client, entitlement="preview_only")
    upper = _create(client, entitlement="PREVIEW_ONLY")  # the design doc's spelling
    for job_id in (lower, upper):
        assert client.get(f"/jobs/{job_id}").json()["entitlement"] == "preview_only"


def test_each_job_gets_a_distinct_short_code(client: TestClient) -> None:
    codes = {_token(client, _create(client)) for _ in range(5)}
    assert len(codes) == 5


def test_unlock_flips_entitlement_and_stamps_paid_at(client: TestClient) -> None:
    job_id = _create(client, entitlement="preview_only")
    resp = client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["entitlement"] == "edited_download"
    assert body["paid_at"] and body["paid_at"] > 0


def test_unlock_is_idempotent_and_never_touches_status(client: TestClient) -> None:
    job_id = _create(client, entitlement="preview_only")
    _mark(client, job_id, JobStatus.ready)  # where the scene pipeline leaves a job
    first = client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY).json()
    second = client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY).json()
    assert second["entitlement"] == "edited_download"
    assert second["paid_at"] == first["paid_at"]  # not re-stamped
    assert second["status"] == "ready"  # the review/delivery machine is untouched


def test_unlock_on_an_already_unlocked_job_is_a_no_op(client: TestClient) -> None:
    job_id = _create(client)  # already edited_download
    body = client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY).json()
    assert body["entitlement"] == "edited_download"
    assert body["paid_at"] is None  # nothing was purchased, so nothing is stamped


def test_unlock_unknown_job_is_404(client: TestClient) -> None:
    assert client.post("/jobs/nope/unlock", json=_PAYMENT_BODY).status_code == 404


def _rendered(client: TestClient, job_id: str, *, locked: bool) -> None:
    """Put clean masters (and, when locked, the watermarked previews) on disk."""
    store = JobStore(client.jobs_root)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"CLEAN-MASTER-BYTES")
    if locked:
        (jd / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
    store.update(
        job_id, status=JobStatus.ready, outputs={"full_video": str(jd / "full_video.mp4")}
    )


def test_gallery_page_renders_unlocked_with_downloads(client: TestClient) -> None:
    job_id = _create(client, customer_name="Sophie Lavoie")
    _rendered(client, job_id, locked=False)
    resp = client.get(f"/j/{_token(client, job_id)}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Sophie Lavoie" in resp.text
    assert "Your jump is ready" in resp.text
    assert "Unlock full video" not in resp.text


def test_gallery_page_renders_locked_with_the_paywall(client: TestClient) -> None:
    job_id = _create(client, entitlement="preview_only", customer_name="Sophie")
    _rendered(client, job_id, locked=True)
    resp = client.get(f"/j/{_token(client, job_id)}", params={"s": "e"})
    assert resp.status_code == 200
    assert "We filmed it anyway" in resp.text
    assert "Unlock full video" in resp.text
    assert "720P PREVIEW" in resp.text


def test_gallery_page_shows_the_hero_meta_and_download_action(client: TestClient) -> None:
    """Frame 03: date · product · instructor, then one primary download button."""
    job_id = _create(
        client,
        customer_name="Sophie Lavoie",
        instructor_name="Marc Tremblay",
        jump_date="2026-08-14",
        package="selfie",
    )
    _rendered(client, job_id, locked=False)
    page = client.get(f"/j/{_token(client, job_id)}").text
    assert "14 AUG 2026" in page
    assert "Tandem · Handcam" in page
    assert "Instructor Marc Tremblay" in page
    assert "1080P · FULL QUALITY" in page
    assert "Download video" in page
    assert "yours to keep" in page


def test_gallery_shows_the_upsell_row_in_both_states(client: TestClient) -> None:
    """The row is entitlement-independent — the operator's second revenue line."""
    for locked in (False, True):
        job_id = _create(client, entitlement="preview_only" if locked else "edited_download")
        _rendered(client, job_id, locked=locked)
        page = client.get(f"/j/{_token(client, job_id)}").text
        assert "Add to your day" in page
        for title in ("Raw Footage", "Photo Pack", "Book Again"):
            assert title in page


def test_upsell_tiles_link_through_the_checkout_template(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/c?j={job_id}&i={item}")
    monkeypatch.setenv("UPSELL_TILES", "raw:Raw Footage:Every unedited minute:$29")
    get_settings.cache_clear()
    try:
        job_id = _create(client, entitlement="preview_only")
        _rendered(client, job_id, locked=True)
        page = client.get(f"/j/{_token(client, job_id)}").text
        # {item} resolves per tile, and the unlock CTA still resolves with it present.
        assert f'href="https://pay.test/c?j={job_id}&amp;i=raw"' in page
        assert f'href="https://pay.test/c?j={job_id}&amp;i=unlock"' in page
    finally:
        get_settings.cache_clear()


def test_job_response_carries_the_derived_media_state(client: TestClient) -> None:
    """The design doc's Frame 02 vocabulary, projected from status + entitlement."""
    job_id = _create(client, entitlement="preview_only")
    assert client.get(f"/jobs/{job_id}").json()["media_state"] == "PENDING_CAPTURE"
    _mark(client, job_id, JobStatus.processing)
    assert client.get(f"/jobs/{job_id}").json()["media_state"] == "EDITING"
    _mark(client, job_id, JobStatus.ready)
    assert client.get(f"/jobs/{job_id}").json()["media_state"] == "LOCKED_PREVIEW"
    # Unlock is the only thing that moves the paywall — status stays put.
    body = client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY).json()
    assert body["media_state"] == "UNLOCKED" and body["status"] == "ready"


def test_gallery_state_endpoint_reports_only_the_lock(client: TestClient) -> None:
    """C-4: what the locked page polls to flip itself. One boolean, no PII."""
    job_id = _create(client, entitlement="preview_only", customer_name="Sophie Lavoie")
    _rendered(client, job_id, locked=True)
    token = _token(client, job_id)

    resp = client.get(f"/j/{token}/state")
    assert resp.status_code == 200
    assert resp.json() == {"locked": True}
    assert "Sophie" not in resp.text and token not in resp.text

    client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY)
    assert client.get(f"/j/{token}/state").json() == {"locked": False}


def test_gallery_state_needs_no_service_token(
    tmp_path: Path, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It's part of the customer page, so it lives inside the /j/ exemption."""
    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cret-token")
    get_settings.cache_clear()
    try:
        app = create_app()
        store = JobStore(tmp_path)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_queue] = lambda: queue
        with TestClient(app) as c:
            c.jobs_root = tmp_path
            job_id = _create(c, entitlement="preview_only", headers=_AUTH)
            _rendered(c, job_id, locked=True)
            token = _token(c, job_id)
            assert c.get(f"/j/{token}/state").status_code == 200
    finally:
        get_settings.cache_clear()


def test_unknown_gallery_code_state_is_404(client: TestClient) -> None:
    assert client.get("/j/deadbeef123/state").status_code == 404


def test_locked_gallery_page_carries_the_flip_poll(client: TestClient) -> None:
    job_id = _create(client, entitlement="preview_only")
    _rendered(client, job_id, locked=True)
    token = _token(client, job_id)
    page = client.get(f"/j/{token}").text
    assert f"/j/{token}/state" in page and "location.reload()" in page

    client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY)
    # Once unlocked there is nothing to wait for, so the script is gone.
    assert "location.reload()" not in client.get(f"/j/{token}").text


def test_gallery_ignores_any_source_tag(client: TestClient) -> None:
    """``?s=`` is analytics for SkydiveOS, never auth or lock state."""
    job_id = _create(client, entitlement="preview_only")
    _rendered(client, job_id, locked=True)
    token = _token(client, job_id)
    plain = client.get(f"/j/{token}")
    tagged = client.get(f"/j/{token}", params={"s": "m"})
    spoofed = client.get(f"/j/{token}", params={"s": "edited_download"})
    assert plain.status_code == tagged.status_code == spoofed.status_code == 200
    assert plain.text == tagged.text == spoofed.text
    assert "Unlock full video" in spoofed.text  # can't talk your way past the paywall


def test_gallery_before_the_render_shows_a_still_editing_page(client: TestClient) -> None:
    job_id = _create(client)
    resp = client.get(f"/j/{_token(client, job_id)}")
    assert resp.status_code == 200
    assert "still being edited" in resp.text


def test_unknown_gallery_code_is_404(client: TestClient) -> None:
    assert client.get("/j/deadbeef123").status_code == 404
    assert client.get("/j/deadbeef123/media/full_video").status_code == 404


def test_locked_gallery_serves_only_the_watermarked_preview(client: TestClient) -> None:
    """The entitlement, never the URL, picks the file — the master stays unreachable."""
    job_id = _create(client, entitlement="preview_only")
    _rendered(client, job_id, locked=True)
    resp = client.get(f"/j/{_token(client, job_id)}/media/full_video")
    assert resp.status_code == 200
    assert resp.content == b"WATERMARKED"
    assert b"CLEAN-MASTER-BYTES" not in resp.content


def test_unlock_makes_the_gallery_serve_the_clean_master(client: TestClient) -> None:
    job_id = _create(client, entitlement="preview_only")
    _rendered(client, job_id, locked=True)
    token = _token(client, job_id)
    assert client.get(f"/j/{token}/media/full_video").content == b"WATERMARKED"

    client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY)

    # Same URL, same token — no re-render, no re-delivery, just the clean file.
    assert client.get(f"/j/{token}/media/full_video").content == b"CLEAN-MASTER-BYTES"
    assert "Unlock full video" not in client.get(f"/j/{token}").text


def test_locked_gallery_hides_the_photos(client: TestClient) -> None:
    job_id = _create(client, entitlement="preview_only")
    _rendered(client, job_id, locked=True)
    store = JobStore(client.jobs_root)
    photos = store.dir(job_id) / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / "a.jpg").write_bytes(b"jpeg")
    (photos / "index.json").write_text(json.dumps([{"filename": "a.jpg"}]))
    token = _token(client, job_id)

    page = client.get(f"/j/{token}")
    assert "1 photos included" in page.text  # a teaser, not the grid
    assert client.get(f"/j/{token}/photos/a.jpg").status_code == 404

    client.post(f"/jobs/{job_id}/unlock", json=_PAYMENT_BODY)
    assert client.get(f"/j/{token}/photos/a.jpg").status_code == 200


def test_gallery_media_route_rejects_traversal_and_unknown_names(
    client: TestClient,
) -> None:
    job_id = _create(client)
    _rendered(client, job_id, locked=False)
    token = _token(client, job_id)
    for name in ("../job", "../../etc/passwd", "highlights", "photos"):
        assert client.get(f"/j/{token}/media/{name}").status_code in (404, 400)
    for filename in ("../job.json", "..%2Fjob.json"):
        assert client.get(f"/j/{token}/photos/{filename}").status_code in (404, 400)


def test_gallery_needs_no_identity_headers_even_when_auth_is_enforced(
    tmp_path: Path, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The customer has no SkydiveOS account — the short code is the only credential."""
    monkeypatch.setenv("ENFORCE_INSTRUCTOR_AUTH", "1")
    from api.config import get_settings

    get_settings.cache_clear()
    try:
        app = create_app()
        store = JobStore(tmp_path)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_queue] = lambda: queue
        with TestClient(app) as c:
            c.jobs_root = tmp_path
            # Set up as SkydiveOS would (an admin caller); the customer has no identity.
            admin = {"X-Instructor-Id": "root", "X-Role": "admin"}
            resp = c.post(
                "/jobs",
                json={"entitlement": "preview_only", "customer_name": "Sophie"},
                headers=admin,
            )
            assert resp.status_code == 201, resp.text
            job_id = resp.json()["job_id"]
            store.update(job_id, instructor_id="inst-42")  # owned by someone
            _rendered(c, job_id, locked=True)
            token = _token(c, job_id)
            # An instructor-scoped route is unreachable without the header …
            assert c.get(f"/jobs/{job_id}").status_code == 401
            # … but the customer's gallery works with no identity at all.
            assert c.get(f"/j/{token}").status_code == 200
            assert c.get(f"/j/{token}/media/full_video").status_code == 200
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Path B go-live gate: a locked job may only be CREATED where it can be delivered.
#
# Delivery refuses the legacy S3 gallery for a preview_only job (it would presign the
# clean master), so without PUBLIC_BASE_URL such a job is undeliverable. Catch it at
# creation — before footage, before a render — so the delivery-time failure can never
# be reached in production.
# --------------------------------------------------------------------------- #


def test_preview_only_is_refused_without_a_public_base_url(
    tmp_path: Path, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app()
        store = JobStore(tmp_path)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_queue] = lambda: queue
        with TestClient(app) as c:
            c.jobs_root = tmp_path
            resp = c.post(
                "/jobs", json={"entitlement": "preview_only", "customer_name": "Sophie"}
            )
            assert resp.status_code == 422
            assert "PUBLIC_BASE_URL" in resp.text
            assert c.get("/jobs").json()["count"] == 0  # nothing was created
    finally:
        get_settings.cache_clear()


def test_preview_only_is_accepted_once_the_gallery_has_an_origin(
    client: TestClient,
) -> None:
    """The pinned test env sets PUBLIC_BASE_URL — the deliverable configuration."""
    resp = client.post("/jobs", json={"entitlement": "PREVIEW_ONLY"})
    assert resp.status_code == 201
    assert resp.json()["job"]["entitlement"] == "preview_only"
    assert resp.json()["job"]["media_state"] == "PENDING_CAPTURE"


def test_path_a_creation_never_needs_a_public_base_url(client: TestClient) -> None:
    """Regression guard: the paid flow is untouched by the gate."""
    for body in ({}, {"entitlement": "edited_download"}, {"customer_name": "Ann"}):
        assert client.post("/jobs", json=body).status_code == 201


# --------------------------------------------------------------------------- #
# The service-token gate (risk probe / Fix #4).
#
# The hole this closes, verified against production on 2026-08-03: the service is
# internet-facing (the SkydiveOS frontend was built to call it from the browser),
# its identity headers are self-asserted, and with ENFORCE_INSTRUCTOR_AUTH off
# every anonymous caller is an admin — so `GET /jobs` returned every customer's
# name, email and delivery links, and /deliverables/{name} streamed their video.
# --------------------------------------------------------------------------- #


@pytest.fixture
def gated(tmp_path: Path, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch):
    """A client whose app requires the shared service token (AUTO_EDIT_API_KEY)."""
    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cret-token")
    get_settings.cache_clear()
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    try:
        with TestClient(app) as c:
            c.jobs_root = tmp_path
            yield c
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


_AUTH = {"Authorization": "Bearer s3cret-token"}


def test_anonymous_calls_are_rejected_when_the_token_is_set(gated: TestClient) -> None:
    """Every staff/admin surface — including the enumeration that leaked PII."""
    for path in ("/jobs", "/cameras", "/docs", "/openapi.json"):
        resp = gated.get(path)
        assert resp.status_code == 401, f"{path} was reachable anonymously"
        assert "s3cret-token" not in resp.text  # never echo the secret


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "s3cret-token"},          # no scheme
        {"Authorization": "Basic s3cret-token"},    # wrong scheme
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer s3cret-token "},  # trailing space is tolerated
        {"X-Role": "admin", "X-Instructor-Id": "root"},  # self-asserted ≠ authorised
    ],
)
def test_only_the_exact_bearer_token_is_accepted(
    gated: TestClient, headers: dict[str, str]
) -> None:
    expected = 200 if headers.get("Authorization", "").strip() == "Bearer s3cret-token" else 401
    assert gated.get("/jobs", headers=headers).status_code == expected


def test_the_customer_gallery_stays_public_under_the_gate(gated: TestClient) -> None:
    """The one exemption: a customer holds a short code, not a service token."""
    job_id = _create(gated, headers=_AUTH)
    _rendered(gated, job_id, locked=False)
    token = _token(gated, job_id)

    assert gated.get(f"/j/{token}").status_code == 200
    assert gated.get(f"/j/{token}/media/full_video").status_code == 200
    assert gated.get(f"/j/{token}/photos/nope.jpg").status_code in (400, 404)
    # And the gate is still shut on the job routes behind that same gallery.
    assert gated.get(f"/jobs/{job_id}").status_code == 401


def test_a_deliverable_cannot_be_streamed_anonymously(gated: TestClient) -> None:
    job_id = _create(gated, headers=_AUTH)
    _rendered(gated, job_id, locked=False)
    assert gated.get(f"/jobs/{job_id}/deliverables/full_video").status_code == 401
    assert (
        gated.get(f"/jobs/{job_id}/deliverables/full_video", headers=_AUTH).status_code == 200
    )


def test_path_a_flow_is_unchanged_when_no_token_is_configured(client: TestClient) -> None:
    """Regression guard: with AUTO_EDIT_API_KEY unset nothing needs a header."""
    job_id = _create(client)
    assert client.get("/jobs").status_code == 200
    assert client.get(f"/jobs/{job_id}").status_code == 200


# --------------------------------------------------------------------------- #
# Unlock hardening (F-16): admin + service token + payment proof
# --------------------------------------------------------------------------- #

_PAYMENT = {"payment_reference": "clover_txn_9f21c7"}


def test_unlock_requires_the_service_token(gated: TestClient) -> None:
    """Anonymous unlock = a free video. This is the revenue-leak test."""
    job_id = _create(gated, entitlement="preview_only", headers=_AUTH)
    assert gated.post(f"/jobs/{job_id}/unlock", json=_PAYMENT).status_code == 401
    # The paywall did not move.
    assert (
        gated.get(f"/jobs/{job_id}", headers=_AUTH).json()["entitlement"] == "preview_only"
    )


def test_unlock_requires_a_payment_reference(gated: TestClient) -> None:
    job_id = _create(gated, entitlement="preview_only", headers=_AUTH)
    for body in ({}, {"payment_reference": ""}, {"amount": 39.0}):
        resp = gated.post(f"/jobs/{job_id}/unlock", json=body, headers=_AUTH)
        assert resp.status_code == 422, body
    assert (
        gated.get(f"/jobs/{job_id}", headers=_AUTH).json()["entitlement"] == "preview_only"
    )


def test_unlock_requires_an_admin_role(
    tmp_path: Path, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain instructor identity must not be able to self-serve an unlock."""
    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cret-token")
    monkeypatch.setenv("ENFORCE_INSTRUCTOR_AUTH", "1")
    get_settings.cache_clear()
    try:
        app = create_app()
        store = JobStore(tmp_path)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_queue] = lambda: queue
        with TestClient(app) as c:
            c.jobs_root = tmp_path
            admin = {**_AUTH, "X-Instructor-Id": "root", "X-Role": "admin"}
            job_id = c.post(
                "/jobs", json={"entitlement": "preview_only"}, headers=admin
            ).json()["job_id"]
            store.update(job_id, instructor_id="inst-42")

            instructor = {**_AUTH, "X-Instructor-Id": "inst-42", "X-Role": "instructor"}
            assert (
                c.post(f"/jobs/{job_id}/unlock", json=_PAYMENT, headers=instructor).status_code
                == 403
            )
            # The owning admin can.
            ok = c.post(f"/jobs/{job_id}/unlock", json=_PAYMENT, headers=admin)
            assert ok.status_code == 200 and ok.json()["entitlement"] == "edited_download"
    finally:
        get_settings.cache_clear()


def test_authorised_unlock_records_the_payment_reference(gated: TestClient) -> None:
    job_id = _create(gated, entitlement="preview_only", headers=_AUTH)
    body = gated.post(f"/jobs/{job_id}/unlock", json=_PAYMENT, headers=_AUTH).json()

    assert body["entitlement"] == "edited_download"
    assert body["paid_at"] > 0
    # Every unlock is attributable to a real capture.
    assert JobStore(gated.jobs_root).load(job_id).payment_reference == "clover_txn_9f21c7"


def test_unlock_stays_idempotent_and_keeps_the_first_reference(gated: TestClient) -> None:
    job_id = _create(gated, entitlement="preview_only", headers=_AUTH)
    first = gated.post(f"/jobs/{job_id}/unlock", json=_PAYMENT, headers=_AUTH).json()
    second = gated.post(
        f"/jobs/{job_id}/unlock", json={"payment_reference": "a-retry"}, headers=_AUTH
    ).json()

    assert second["paid_at"] == first["paid_at"]  # not re-stamped
    assert JobStore(gated.jobs_root).load(job_id).payment_reference == "clover_txn_9f21c7"
