"""Bug 374 — jobs stranded in ``queued`` must self-heal or fail, never hang.

Every dispatch rides on one Celery message; a lost message left a job at
``status=queued`` / ``media_state=UPLOADED`` forever ("Waiting to start..." in
SkydiveOS, no error, no timeout). :mod:`api.reconcile` turns the status read into
the recovery signal. These tests pin the whole contract:

* staged-but-undispatched footage, quiet past the threshold → dispatch re-armed;
* dispatched-but-never-started, quiet past the (generous) timeout → ``failed``
  with an actionable ``error``;
* footage still arriving, footage absent entirely (PENDING_CAPTURE), non-queued
  statuses, eager mode, disabled knobs → untouched — a healthy job must fall
  straight through (Scenarios D/E of the bug report);
* per-role recovery on mixed jobs, and the ultimum stranded verdict, mirror the
  settle task and watchdog they back up.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app, get_queue, get_settings, get_store
from api.config import Settings
from api.jobs import (
    Entitlement,
    Job,
    JobStatus,
    JobStore,
    MediaRef,
    Package,
    RoleIngest,
)
from api.lifecycle import MediaState, media_state
from api.reconcile import reconcile_stuck_job


def _settings(**overrides: Any) -> Settings:
    """A fully-populated Settings with reconcile-friendly defaults for tests."""
    base = Settings(
        redis_url="redis://localhost:6379/0",
        jobs_root=None,
        skydiveos_api_base=None,
        task_always_eager=False,
        enable_auto_discovery=False,
        mongo_url=None,
        mongo_db="skydiveos",
        discovery_interval=30.0,
        camera_scanner="static",
        delete_after_transfer=False,
        delete_after_transfer_min_age_h=24.0,
        delete_after_transfer_dry_run=False,
        discovery_fake_cameras=(),
        discovery_sample_mp4=None,
        discovery_sample_count=1,
        enforce_instructor_auth=False,
        s3_bucket=None,
        s3_endpoint_url=None,
        s3_region=None,
    )
    return replace(base, **overrides)


class FakeQueue:
    """Records what would have been enqueued — same shape as test_api's fake."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def enqueue_processing(self, job_id: str) -> None:
        self.calls.append(("processing", (job_id,)))

    def enqueue_selfie_processing(self, job_id: str) -> None:
        self.calls.append(("selfie", (job_id,)))

    def enqueue_media_ref_processing(self, job_id: str, role: str) -> None:
        self.calls.append(("media_ref", (job_id, role)))

    def enqueue_rerender(self, job_id: str) -> None:
        self.calls.append(("rerender", (job_id,)))

    def enqueue_delivery(self, job_id: str) -> None:
        self.calls.append(("delivery", (job_id,)))

    def enqueue_load_fan_out(self, job_id: str) -> None:
        self.calls.append(("fan_out", (job_id,)))

    def enqueue_pull(self, job_id: str, camera_id: str) -> None:
        self.calls.append(("pull", (job_id, camera_id)))

    def enqueue_s3_ingest(
        self, job_id: str, s3_key: str, camera_role: str | None = None
    ) -> None:
        self.calls.append(("s3_ingest", (job_id, s3_key, camera_role)))

    def arm_ultimum_watchdog(self, job_id: str, countdown: float) -> None:
        self.calls.append(("ultimum_watchdog", (job_id, countdown)))

    def enqueue_raw_proxies(self, job_id: str) -> None:
        self.calls.append(("raw_proxies", (job_id,)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path)


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


NOW = time.time()


def _stuck_job(store: JobStore, **fields: Any) -> Job:
    """A single-product job in the exact reported state: queued, footage staged."""
    defaults: dict[str, Any] = dict(
        job_id="j-stuck",
        package=Package.selfie,
        status=JobStatus.queued,
        source_path="/jobs/j-stuck/raw/GH010001.MP4",
        last_raw_clip_at=NOW - 3600,  # an hour of silence
        processing_dispatched=False,
        booking_id="BK-2026-001",
    )
    defaults.update(fields)
    return store.create(Job(**defaults))


# --------------------------------------------------------------------------- #
# Recovery: staged but never dispatched
# --------------------------------------------------------------------------- #


def test_staged_undispatched_job_is_redispatched(store: JobStore, queue: FakeQueue) -> None:
    job = _stuck_job(store)
    assert media_state(job) is MediaState.uploaded  # the reported symptom

    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)

    assert queue.kinds() == ["selfie"]  # selfie package → scene pipeline
    assert out.processing_dispatched is True
    assert out.status is JobStatus.queued  # the worker moves it, not the reconciler
    assert store.load("j-stuck").processing_dispatched is True


def test_recovery_is_exactly_once(store: JobStore, queue: FakeQueue) -> None:
    job = _stuck_job(store)
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    again = reconcile_stuck_job(out, store, queue, _settings(), now=NOW)
    assert queue.kinds() == ["selfie"]  # the second read must not enqueue a second render
    assert again.processing_dispatched is True


def test_footage_still_arriving_is_left_alone(store: JobStore, queue: FakeQueue) -> None:
    # Scenario D: within the settle window / recovery threshold — the settle task
    # (or a fresh clip) still owns this job.
    job = _stuck_job(store, last_raw_clip_at=NOW - 60)
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.calls == []
    assert out.processing_dispatched is False


def test_recovery_threshold_never_undercuts_the_settle_window(
    store: JobStore, queue: FakeQueue
) -> None:
    # A recovery knob set below the settle window must not race the settle task.
    job = _stuck_job(store, last_raw_clip_at=NOW - 100)
    settings = _settings(
        stuck_job_recovery_after_s=1.0,
        raw_clip_settle_seconds=180.0,
        raw_clip_settle_poll_seconds=30.0,
    )
    out = reconcile_stuck_job(job, store, queue, settings, now=NOW)
    assert queue.calls == []
    assert out.processing_dispatched is False


def test_job_without_footage_is_left_alone(store: JobStore, queue: FakeQueue) -> None:
    # Scenario E / PENDING_CAPTURE: created but never uploaded is a valid resting
    # state (creation and upload are separate calls) — never dispatch, never fail.
    job = _stuck_job(store, source_path=None, last_raw_clip_at=NOW - 10 * 24 * 3600)
    assert media_state(job) is MediaState.pending_capture
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.calls == []
    assert out.status is JobStatus.queued
    assert out.error is None


@pytest.mark.parametrize(
    "status",
    [JobStatus.processing, JobStatus.ready, JobStatus.approved, JobStatus.delivered],
)
def test_non_queued_statuses_are_left_alone(
    store: JobStore, queue: FakeQueue, status: JobStatus
) -> None:
    job = _stuck_job(store, status=status)
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.calls == []
    assert out.status is status


def test_eager_mode_is_a_noop(store: JobStore, queue: FakeQueue) -> None:
    # Under eager a dispatch would run a whole render inside the GET, and an eager
    # setup has no broker to lose messages in.
    job = _stuck_job(store)
    out = reconcile_stuck_job(job, store, queue, _settings(task_always_eager=True), now=NOW)
    assert queue.calls == []
    assert out.processing_dispatched is False


def test_recovery_can_be_disabled(store: JobStore, queue: FakeQueue) -> None:
    job = _stuck_job(store)
    out = reconcile_stuck_job(
        job, store, queue, _settings(stuck_job_recovery_after_s=0.0), now=NOW
    )
    assert queue.calls == []
    assert out.processing_dispatched is False


# --------------------------------------------------------------------------- #
# Fail-fast: dispatched but no worker ever started
# --------------------------------------------------------------------------- #


def test_dispatched_job_quiet_past_timeout_fails_with_reason(
    store: JobStore, queue: FakeQueue
) -> None:
    job = _stuck_job(store, processing_dispatched=True, last_raw_clip_at=NOW - 7 * 3600)
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert out.status is JobStatus.failed
    assert "stalled" in (out.error or "") and "worker" in (out.error or "")
    assert media_state(out) is MediaState.failed  # the UI stops saying "Waiting to start..."
    assert queue.calls == []  # failed, not silently re-rendered


def test_dispatched_job_within_timeout_is_left_alone(
    store: JobStore, queue: FakeQueue
) -> None:
    # A busy day legitimately queues jobs behind one worker for hours.
    job = _stuck_job(store, processing_dispatched=True, last_raw_clip_at=NOW - 3600)
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert out.status is JobStatus.queued
    assert out.error is None


def test_fail_fast_can_be_disabled(store: JobStore, queue: FakeQueue) -> None:
    job = _stuck_job(store, processing_dispatched=True, last_raw_clip_at=NOW - 30 * 24 * 3600)
    out = reconcile_stuck_job(job, store, queue, _settings(queued_job_timeout_s=0.0), now=NOW)
    assert out.status is JobStatus.queued


# --------------------------------------------------------------------------- #
# Mixed (multi-ref) jobs: per-role recovery
# --------------------------------------------------------------------------- #


def _mixed_job(store: JobStore, **fields: Any) -> Job:
    defaults: dict[str, Any] = dict(
        job_id="j-mixed",
        package=Package.selfie,
        entitlement=Entitlement.edited_download,
        status=JobStatus.queued,
        booking_id="BK-2026-002",
        media_refs=[
            MediaRef(
                role="instructor",
                package=Package.selfie,
                entitlement=Entitlement.edited_download,
            ),
            MediaRef(
                role="external",
                package=Package.external,
                entitlement=Entitlement.preview_only,
            ),
        ],
    )
    defaults.update(fields)
    return store.create(Job(**defaults))


def test_stuck_role_on_a_mixed_job_is_redispatched(
    store: JobStore, queue: FakeQueue
) -> None:
    job = _mixed_job(
        store,
        role_ingest={"instructor": RoleIngest(last_clip_at=NOW - 3600, dispatched=False)},
    )
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.calls == [("media_ref", ("j-mixed", "instructor"))]
    assert out.role_ingest["instructor"].dispatched is True
    # The external role has no footage yet — nothing dispatched, nothing failed.
    assert "external" not in out.role_ingest or not out.role_ingest["external"].dispatched


def test_mixed_job_second_camera_recovers_even_after_delivery(
    store: JobStore, queue: FakeQueue
) -> None:
    # The paid edit routinely ships before the spec card arrives, so the job is no
    # longer `queued` when the second role's settle task gets lost. Mirror the
    # settle task's own rule: only failed/rejected stop a role's recovery.
    job = _mixed_job(
        store,
        status=JobStatus.delivered,
        role_ingest={
            "instructor": RoleIngest(last_clip_at=NOW - 7200, dispatched=True),
            "external": RoleIngest(last_clip_at=NOW - 3600, dispatched=False),
        },
    )
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.calls == [("media_ref", ("j-mixed", "external"))]
    assert out.role_ingest["external"].dispatched is True
    assert out.status is JobStatus.delivered  # never knocked back


def test_mixed_job_fresh_role_is_left_alone(store: JobStore, queue: FakeQueue) -> None:
    job = _mixed_job(
        store,
        role_ingest={"instructor": RoleIngest(last_clip_at=NOW - 60, dispatched=False)},
    )
    reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.calls == []


# --------------------------------------------------------------------------- #
# Ultimum: back up the watchdog's verdict
# --------------------------------------------------------------------------- #


def _stage_master(store: JobStore, job_id: str, role: str) -> None:
    d = store.camera_raw_dir(job_id, role)
    d.mkdir(parents=True, exist_ok=True)
    (d / "GH010001.MP4").write_bytes(b"\x00")


def test_ultimum_with_both_cameras_staged_is_redispatched(
    store: JobStore, queue: FakeQueue
) -> None:
    job = store.create(
        Job(
            job_id="j-ult",
            package=Package.ultimum,
            status=JobStatus.queued,
            last_raw_clip_at=NOW - 3600,
        )
    )
    _stage_master(store, "j-ult", "instructor")
    _stage_master(store, "j-ult", "external")
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert queue.kinds() == ["selfie"]  # ultimum runs through the scene pipeline
    assert out.processing_dispatched is True


def test_ultimum_stranded_one_camera_fails_like_the_watchdog(
    store: JobStore, queue: FakeQueue
) -> None:
    job = store.create(
        Job(
            job_id="j-ult2",
            package=Package.ultimum,
            status=JobStatus.queued,
            last_raw_clip_at=NOW - 2 * 3600,  # past the 1 h ultimum timeout
        )
    )
    _stage_master(store, "j-ult2", "instructor")
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert out.status is JobStatus.failed
    assert "stranded" in (out.error or "") and "external" in (out.error or "")
    assert queue.calls == []


def test_ultimum_one_camera_within_timeout_is_left_alone(
    store: JobStore, queue: FakeQueue
) -> None:
    job = store.create(
        Job(
            job_id="j-ult3",
            package=Package.ultimum,
            status=JobStatus.queued,
            last_raw_clip_at=NOW - 600,
        )
    )
    _stage_master(store, "j-ult3", "instructor")
    out = reconcile_stuck_job(job, store, queue, _settings(), now=NOW)
    assert out.status is JobStatus.queued
    assert queue.calls == []


# --------------------------------------------------------------------------- #
# Through the API: the status poll is the recovery signal
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path: Path, queue: FakeQueue) -> TestClient:
    app = create_app()
    store = JobStore(tmp_path)
    settings = _settings()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    client.app_store = store  # stash for assertions
    return client


def test_get_job_heals_a_stuck_job(client: TestClient, queue: FakeQueue) -> None:
    store: JobStore = client.app_store
    _stuck_job(store)

    resp = client.get("/jobs/j-stuck")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["media_state"] == "UPLOADED"
    assert queue.kinds() == ["selfie"]  # the poll re-armed the lost dispatch

    # The next poll must not enqueue a second render.
    client.get("/jobs/j-stuck")
    assert queue.kinds() == ["selfie"]


def test_get_job_fails_a_dispatched_job_past_timeout(
    client: TestClient, queue: FakeQueue
) -> None:
    store: JobStore = client.app_store
    _stuck_job(store, processing_dispatched=True, last_raw_clip_at=time.time() - 8 * 3600)

    resp = client.get("/jobs/j-stuck")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["media_state"] == "FAILED"
    assert "stalled" in (body["error"] or "")


def test_list_jobs_heals_stuck_rows(client: TestClient, queue: FakeQueue) -> None:
    store: JobStore = client.app_store
    _stuck_job(store)

    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert queue.kinds() == ["selfie"]
