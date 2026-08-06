"""Tests for the disk-retention sweep (scripts/prune_jobs.py) + gallery S3 fallback.

The rules that matter, mirroring card retention: a file is deleted only when S3
confirms the exact copy (size-matched HeadObject); a still-locked job's previews
are untouchable; anything unverifiable is kept; the sweep never raises. The
gallery side: a pruned master redirects to a presigned deliveries/ URL — but a
locked job never does (a presigned master URL is the paywall bypass).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from api.jobs import Entitlement, Job, JobStatus, JobStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prune_jobs  # noqa: E402


class FakeS3:
    """head_object answers from a {key: size} dict; anything else raises."""

    def __init__(self, objects: dict[str, int]) -> None:
        self.objects = objects

    def head_object(self, Bucket: str, Key: str) -> dict[str, int]:  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": self.objects[Key]}


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(str(tmp_path / "jobs"))


def _delivered_job(store: JobStore, job_id: str = "j1", **fields: object) -> Job:
    base: dict[str, object] = {
        "job_id": job_id, "status": JobStatus.delivered, "camera_id": "9362",
    }
    base.update(fields)
    job = store.create(Job(**base))  # type: ignore[arg-type]
    return job


def test_raw_pruned_only_when_s3_confirms_size(store: JobStore) -> None:
    job = _delivered_job(store)
    raw = store.dir(job.job_id) / "raw"
    raw.mkdir(parents=True)
    (raw / "GX1.MP4").write_bytes(b"AAAA")
    (raw / "GX2.MP4").write_bytes(b"BBBBBB")
    s3 = FakeS3({"raw/9362/GX1.MP4": 4, "raw/9362/GX2.MP4": 999})  # GX2 size mismatch

    freed = prune_jobs.prune_job_raw(store, job, s3, "bkt", dry_run=False)

    assert not (raw / "GX1.MP4").exists()  # confirmed → gone
    assert (raw / "GX2.MP4").exists()  # size mismatch → kept
    assert freed == 4


def test_raw_kept_without_camera_id(store: JobStore) -> None:
    job = _delivered_job(store, job_id="j2", camera_id=None)
    raw = store.dir(job.job_id) / "raw"
    raw.mkdir(parents=True)
    (raw / "GX1.MP4").write_bytes(b"AAAA")

    freed = prune_jobs.prune_job_raw(store, job, FakeS3({}), "bkt", dry_run=False)

    assert (raw / "GX1.MP4").exists() and freed == 0  # no key derivable → keep all


def test_renders_pruned_with_fallback_but_locked_previews_survive(store: JobStore) -> None:
    job = _delivered_job(
        store, job_id="j3", entitlement=Entitlement.preview_only,
        outputs={"full_video": "x", "photos": "y"},
    )
    jd = store.dir(job.job_id)
    (jd / "full_video.mp4").write_bytes(b"MASTER")
    (jd / "preview_full_video.mp4").write_bytes(b"WM")
    s3 = FakeS3({"deliveries/j3/full_video.mp4": 6})

    prune_jobs.prune_job_renders(store, job, s3, "bkt", dry_run=False)

    assert not (jd / "full_video.mp4").exists()  # master is in S3 → prunable
    assert (jd / "preview_full_video.mp4").exists()  # locked gallery's only media

    # Once unlocked, the previews are derivative and go too.
    unlocked = store.update(job.job_id, entitlement=Entitlement.edited_download)
    prune_jobs.prune_job_renders(store, unlocked, s3, "bkt", dry_run=False)
    assert not (jd / "preview_full_video.mp4").exists()


def test_dry_run_deletes_nothing(store: JobStore) -> None:
    job = _delivered_job(store, job_id="j4", outputs={"full_video": "x"})
    jd = store.dir(job.job_id)
    raw = jd / "raw"
    raw.mkdir(parents=True)
    (raw / "GX1.MP4").write_bytes(b"AAAA")
    (jd / "full_video.mp4").write_bytes(b"MASTER")
    s3 = FakeS3({"raw/9362/GX1.MP4": 4, "deliveries/j4/full_video.mp4": 6})

    freed = prune_jobs.prune_job_raw(store, job, s3, "bkt", dry_run=True)
    freed += prune_jobs.prune_job_renders(store, job, s3, "bkt", dry_run=True)

    assert (raw / "GX1.MP4").exists() and (jd / "full_video.mp4").exists()
    assert freed == 10  # it still reports what it WOULD reclaim


def test_day_dir_pruning_spares_ledgers_and_recent_days(tmp_path: Path) -> None:
    cam = tmp_path / "_camera-staging" / "9362"
    (cam / "2020-01-01").mkdir(parents=True)
    (cam / "2020-01-01" / "old.MP4").write_bytes(b"x")
    fresh = time.strftime("%Y-%m-%d")
    (cam / fresh).mkdir()
    (cam / ".transferred.json").write_text("{}")

    prune_jobs.prune_day_dirs(cam, keep_days=7, dry_run=False, label="staging")

    assert not (cam / "2020-01-01").exists()  # ancient day swept
    assert (cam / fresh).exists()  # today kept
    assert (cam / ".transferred.json").exists()  # the ledger is never a day dir


def test_gallery_falls_back_to_presigned_s3_only_when_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from api.app import create_app, get_store
    from api.config import get_settings

    monkeypatch.setenv("S3_BUCKET", "bkt")
    get_settings.cache_clear()

    class FakePresigner:
        def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:  # noqa: N803
            return f"https://s3.test/{Params['Key']}?sig=1"

    monkeypatch.setattr("api.delivery._default_s3_client", lambda s: FakePresigner())

    app = create_app()
    store = JobStore(str(tmp_path / "jobs"))
    app.dependency_overrides[get_store] = lambda: store
    client = TestClient(app)
    job = store.create(Job(
        job_id="jj", status=JobStatus.delivered, outputs={"full_video": "x"},
    ))
    token = store.ensure_gallery_token("jj")

    # Local master missing (pruned) + unlocked → 302 to the presigned copy.
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://s3.test/deliveries/jj/full_video.mp4?sig=1"

    # A locked job with a pruned preview must 404 — never redirect to the master.
    store.update("jj", entitlement=Entitlement.preview_only)
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 404
    get_settings.cache_clear()
