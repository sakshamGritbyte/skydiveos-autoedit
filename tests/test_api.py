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


# --------------------------------------------------------------------------- #
# Upload (file + camera pull)
# --------------------------------------------------------------------------- #


def test_upload_file_stages_source_and_enqueues(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    resp = client.post(
        f"/jobs/{job_id}/upload",
        files={"file": ("GX010123.MP4", b"fake mp4 bytes", "video/mp4")},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "upload"
    assert queue.kinds() == ["processing"]

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "queued"
    # Source master was written into the job dir.
    src = job_dir(job_id, client.jobs_root) / "source.mp4"
    assert src.read_bytes() == b"fake mp4 bytes"


def test_upload_camera_id_triggers_pull(client: TestClient, queue: FakeQueue) -> None:
    job_id = _create(client)
    resp = client.post(f"/jobs/{job_id}/upload", data={"camera_id": "1234"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "pull"
    assert queue.calls == [("pull", (job_id, "1234"))]
    assert client.get(f"/jobs/{job_id}").json()["camera_id"] == "1234"


def test_upload_without_file_or_camera_is_400(client: TestClient) -> None:
    job_id = _create(client)
    resp = client.post(f"/jobs/{job_id}/upload")
    assert resp.status_code == 400


def test_upload_to_processing_job_conflicts(client: TestClient) -> None:
    job_id = _create(client)
    _mark(client, job_id, JobStatus.processing)
    resp = client.post(
        f"/jobs/{job_id}/upload", files={"file": ("a.MP4", b"x", "video/mp4")}
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
            "/jobs/{job_id}/approve", "/jobs/{job_id}/reject",
            "/jobs/{job_id}/tweak", "/jobs/{job_id}/preview"} <= set(paths)
    assert spec["info"]["title"] == "SkydiveOS Auto-Edit API"
