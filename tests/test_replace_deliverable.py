"""
POST /jobs/{id}/deliverables/{name}/replace — swap the bytes behind a delivered
video without changing the customer's link — and the late-render S3 upload gap.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import delivery as delivery_mod
from api.app import create_app, get_queue, get_store
from api.jobs import Entitlement, JobStatus, JobStore
from edl.storage import job_dir


class FakeQueue:
    """Records what would be enqueued; this file never needs a broker."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        if name.startswith(("enqueue_", "arm_")):
            return lambda *a, **k: self.calls.append((name, a))
        raise AttributeError(name)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Same shape as test_api.py's: store in tmp_path, queue faked."""
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: FakeQueue()
    with TestClient(app) as c:
        c.jobs_root = tmp_path
        yield c
    app.dependency_overrides.clear()


class FakeS3:
    """upload/head/download over an in-memory object map."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.uploads: list[str] = []

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, Any]) -> None:
        self.objects[key] = Path(filename).read_bytes()
        self.uploads.append(key)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise Exception("404 NotFound")
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[key])

    def generate_presigned_url(self, op: str, Params: dict[str, str], ExpiresIn: int) -> str:
        return f"https://s3.test/{Params['Bucket']}/{Params['Key']}"


def _create(client: TestClient) -> str:
    resp = client.post("/jobs", json={"customer_name": "Ann", "package": "selfie"})
    assert resp.status_code == 201, resp.text
    return resp.json()["job_id"]


def _seed_delivered(client: TestClient, job_id: str, status: JobStatus = JobStatus.delivered) -> Path:
    jd = job_dir(job_id, client.jobs_root)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"ORIGINAL-RENDER")
    (jd / "highlights.mp4").write_bytes(b"HL-ORIGINAL")
    # outputs values are FILESYSTEM paths — collect_deliverables does Path(value).is_file().
    JobStore(client.jobs_root).update(
        job_id, status=status,
        outputs={"full_video": str(jd / "full_video.mp4"), "highlights": str(jd / "highlights.mp4")},
    )
    return jd


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(delivery_mod, "_default_s3_client", lambda settings: fake)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    from api import config as config_mod
    config_mod.get_settings.cache_clear()  # pick up the bucket
    return fake


def test_replace_swaps_local_file_and_uploads_delivery_copy(client: TestClient, s3: FakeS3) -> None:
    job_id = _create(client)
    jd = _seed_delivered(client, job_id)
    s3.objects["media/edited/new-cut.mp4"] = b"NEW-CUT-FROM-SKYDIVEOS"
    before_mtime = (jd / "full_video.mp4").stat().st_mtime_ns

    resp = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": "media/edited/new-cut.mp4"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "full_video"
    assert body["s3_key"] == f"deliveries/{job_id}/full_video.mp4"
    assert body["size"] == len(b"NEW-CUT-FROM-SKYDIVEOS")

    # Local file — what the gallery player serves first — now IS the new cut,
    # with a new mtime (the CDN ?v= cache key).
    assert (jd / "full_video.mp4").read_bytes() == b"NEW-CUT-FROM-SKYDIVEOS"
    assert (jd / "full_video.mp4").stat().st_mtime_ns >= before_mtime
    assert not (jd / ".full_video.mp4.replacing").exists()
    # Durable copy created (it never existed for this job), at the advertised key.
    assert s3.objects[f"deliveries/{job_id}/full_video.mp4"] == b"NEW-CUT-FROM-SKYDIVEOS"
    # …and the sibling that had never been uploaded rode along.
    assert "highlights" in body["uploaded_missing"]
    assert s3.objects[f"deliveries/{job_id}/highlights.mp4"] == b"HL-ORIGINAL"


def test_replace_refuses_preview_only_deliverable(client: TestClient, s3: FakeS3) -> None:
    job_id = _create(client)
    jd = _seed_delivered(client, job_id)
    JobStore(client.jobs_root).update(job_id, entitlement=Entitlement.preview_only)
    s3.objects["media/edited/new-cut.mp4"] = b"CLEAN"
    resp = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": "media/edited/new-cut.mp4"})
    assert resp.status_code == 403
    assert (jd / "full_video.mp4").read_bytes() == b"ORIGINAL-RENDER"   # untouched
    assert f"deliveries/{job_id}/full_video.mp4" not in s3.objects       # nothing published


def test_replace_refuses_before_delivery(client: TestClient, s3: FakeS3) -> None:
    job_id = _create(client)
    _seed_delivered(client, job_id, status=JobStatus.ready)
    s3.objects["media/edited/new-cut.mp4"] = b"NEW"
    resp = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": "media/edited/new-cut.mp4"})
    assert resp.status_code == 409


def test_replace_404s_unknown_deliverable_and_missing_object(client: TestClient, s3: FakeS3) -> None:
    job_id = _create(client)
    _seed_delivered(client, job_id)
    assert client.post(f"/jobs/{job_id}/deliverables/photos/replace", json={"s3_key": "x"}).status_code == 404
    assert client.post(f"/jobs/{job_id}/deliverables/nope/replace", json={"s3_key": "x"}).status_code == 404
    r = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": "media/does-not-exist.mp4"})
    assert r.status_code == 404
    assert "no object" in r.json()["detail"]


def test_replace_rejects_unsafe_keys(client: TestClient, s3: FakeS3) -> None:
    job_id = _create(client)
    _seed_delivered(client, job_id)
    for bad in ["../etc/passwd", "s3://other-bucket/x.mp4", "https://evil/x.mp4"]:
        r = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": bad})
        assert r.status_code == 422, bad


def test_upload_missing_deliverables_is_idempotent(client: TestClient, s3: FakeS3) -> None:
    job_id = _create(client)
    _seed_delivered(client, job_id)
    store = JobStore(client.jobs_root)
    job = store.load(job_id)
    from api.config import get_settings
    first = delivery_mod.upload_missing_deliverables(job, store, get_settings(), s3_client=s3)
    assert sorted(first) == ["full_video", "highlights"]
    second = delivery_mod.upload_missing_deliverables(job, store, get_settings(), s3_client=s3)
    assert second == []                      # nothing re-uploaded
    assert len(s3.uploads) == 2


def test_replace_reports_s3_trouble_as_502_and_touches_nothing(client: TestClient, s3: FakeS3, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = _create(client)
    jd = _seed_delivered(client, job_id)

    class NoCreds(FakeS3):
        def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            raise RuntimeError("Unable to locate credentials")

    monkeypatch.setattr(delivery_mod, "_default_s3_client", lambda settings: NoCreds())
    r = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": "media/edited/new-cut.mp4"})
    assert r.status_code == 502
    assert "credentials" in r.json()["detail"]
    assert (jd / "full_video.mp4").read_bytes() == b"ORIGINAL-RENDER"
    assert not (jd / ".full_video.mp4.replacing").exists()


def test_403_from_s3_reads_as_missing_for_both_paths(client: TestClient, s3: FakeS3, monkeypatch: pytest.MonkeyPatch) -> None:
    """An identity without s3:ListBucket gets 403 for a missing key. That must not
    make upload_missing_deliverables believe the file is already there."""
    job_id = _create(client)
    _seed_delivered(client, job_id)

    class NoList(FakeS3):
        def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            raise RuntimeError("An error occurred (403) when calling the HeadObject operation: Forbidden")

    fake = NoList()
    store = JobStore(client.jobs_root)
    from api.config import get_settings
    uploaded = delivery_mod.upload_missing_deliverables(store.load(job_id), store, get_settings(), s3_client=fake)
    assert sorted(uploaded) == ["full_video", "highlights"]          # re-uploaded, not skipped

    monkeypatch.setattr(delivery_mod, "_default_s3_client", lambda settings: fake)
    r = client.post(f"/jobs/{job_id}/deliverables/full_video/replace", json={"s3_key": "media/edited/x.mp4"})
    assert r.status_code == 404
    assert "ListBucket" in r.json()["detail"]
