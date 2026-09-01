"""Self-healing for jobs stranded in ``queued`` (Bug 374).

Every dispatch decision in this service rides on exactly ONE Celery message: the
``s3_key`` path's settle countdown (:func:`api.tasks.raw_clips_settled_job`), the
byte path's inline ``enqueue_*``, the ultimum watchdog. If that one message is
lost — a worker restarted while a countdown task sat in its memory, a broker
flush, a deploy — nothing re-arms it. The job then sits at ``status=queued`` with
footage staged (``media_state=UPLOADED``) forever, and SkydiveOS's UI shows
"Waiting to start..." indefinitely, with no error, no rejection and no timeout.

:func:`reconcile_stuck_job` turns the status poll itself into the recovery
signal — the one channel guaranteed to still be alive for a job somebody is
waiting on. Each ``GET /jobs/{job_id}`` (and each row of ``GET /jobs``) inspects
the job and

* **re-arms the dispatch** when footage is staged, has been quiet well past the
  settle window, and processing was never dispatched;
* **fails the job** with an actionable ``error`` when processing WAS dispatched
  long ago but no worker ever picked it up (lost broker message / dead worker);
* **fails a stranded one-camera ultimum job** with the watchdog's own verdict —
  the watchdog is itself a single countdown message and can be lost the same way.

What it deliberately never touches:

* a job with **no footage at all** (``media_state=PENDING_CAPTURE``): creation and
  upload are separate calls by design, and SkydiveOS may legitimately park a job
  before uploading — "waiting for footage" is a valid resting state, "footage
  staged but nobody coming" is not;
* footage still **inside the settle window** — the effective recovery threshold
  never undercuts ``raw_clip_settle_seconds`` (+ one poll), so a batch that is
  still arriving is left to the settle task that owns it;
* ``load_child`` jobs (they stream their master's renders and take no footage);
* anything under ``task_always_eager`` — an inline dispatch inside a GET would
  run a whole render inside the request, and an eager setup has no broker to
  lose messages in.

Recovery goes through the :class:`~api.queue.JobQueue` seam (not ``.delay``), so
the REST layer stays broker-agnostic and tests assert dispatches on the recording
fake. The exactly-once guards are the same ones the tasks use
(``Job.processing_dispatched`` / ``RoleIngest.dispatched``), so a recovery racing
a late settle task cannot start a second render.
"""

from __future__ import annotations

import logging
import time

from .config import Settings
from .jobs import (
    MEDIA_REF_ROLES,
    Job,
    JobKind,
    JobStatus,
    JobStore,
)
from .queue import JobQueue

logger = logging.getLogger(__name__)


def _roles_with_masters(store: JobStore, job_id: str) -> list[str]:
    """The camera roles with at least one MP4 master staged on disk.

    Case-insensitive suffix check, same as the ultimum watchdog: GoPro masters are
    ``.MP4``, so a ``*.mp4`` glob finds nothing on a case-sensitive filesystem.
    """
    return [
        r
        for r in MEDIA_REF_ROLES
        if store.camera_raw_dir(job_id, r).exists()
        and any(
            p.suffix.lower() == ".mp4" for p in store.camera_raw_dir(job_id, r).glob("*")
        )
    ]


def _quiet_for(job: Job, now: float) -> float:
    """Seconds since the job last saw a clip (or, failing that, any write at all).

    Mirrors :func:`api.tasks.raw_clips_settled_job`'s rule: a missing stamp must
    read as "something happened recently", never as "quiet forever".
    """
    stamp = job.last_raw_clip_at or job.updated_at or job.created_at or now
    return now - stamp


def _recover_after(settings: Settings) -> float:
    """The quiet threshold for re-arming a lost dispatch; ``<= 0`` disables.

    Never undercuts the settle window plus one poll — the settle task must get
    every chance to do its job before the reconciler concludes it was lost.
    """
    configured = settings.stuck_job_recovery_after_s
    if configured <= 0:
        return 0.0
    return max(
        configured,
        settings.raw_clip_settle_seconds + settings.raw_clip_settle_poll_seconds,
    )


def _dispatch_via_queue(store: JobStore, queue: JobQueue, job: Job) -> None:
    """Mark dispatched and enqueue the package's pipeline — mirror of
    :func:`api.tasks._dispatch_processing`, through the queue seam."""
    store.update(job.job_id, processing_dispatched=True)
    if job.package.uses_scene_pipeline:
        queue.enqueue_selfie_processing(job.job_id)
    else:
        queue.enqueue_processing(job.job_id)


def _log_recovery(job: Job, quiet: float, detail: str) -> None:
    logger.warning(
        "job %s: recovering stalled dispatch — status=%s package=%s booking=%s "
        "media_refs=%d quiet=%.0fs (%s); the dispatch message was likely lost "
        "(worker restart / broker flush) — Bug 374",
        job.job_id,
        job.status.value,
        job.package.value,
        job.booking_id,
        len(job.media_refs),
        quiet,
        detail,
    )


def reconcile_stuck_job(
    job: Job,
    store: JobStore,
    queue: JobQueue,
    settings: Settings,
    now: float | None = None,
) -> Job:
    """Heal (or fail, with a reason) a job stranded waiting for a lost dispatch.

    Returns the job to report — reloaded when a write happened, the caller's
    instance untouched otherwise. Safe to call on every status read: every branch
    is guarded by the same exactly-once flags the tasks use, and a healthy job
    falls straight through.
    """
    if settings.task_always_eager:
        return job
    if job.job_kind is JobKind.load_child:
        return job

    now = time.time() if now is None else now
    recover_after = _recover_after(settings)

    # ── Mixed job: each role settles and dispatches on its own ────────────────
    # Mirrors raw_clips_settled_job's status rule: failed/rejected stop it, but
    # `ready`/`delivered` do NOT — a mixed job's second camera routinely lands
    # after the first product shipped, and its render must still happen.
    if job.is_multi_ref:
        if job.status in (JobStatus.failed, JobStatus.rejected) or recover_after <= 0:
            return job
        updated_ingest = dict(job.role_ingest)
        recovered = []
        for ref in job.media_refs:
            state = updated_ingest.get(ref.role)
            if state is None or state.dispatched or state.last_clip_at is None:
                continue
            quiet = now - state.last_clip_at
            if quiet < recover_after:
                continue
            _log_recovery(job, quiet, f"role {ref.role} staged but never dispatched")
            updated_ingest[ref.role] = state.model_copy(update={"dispatched": True})
            recovered.append(ref.role)
        if not recovered:
            return job
        store.update(job.job_id, role_ingest=updated_ingest)
        for role in recovered:
            queue.enqueue_media_ref_processing(job.job_id, role)
        return store.load(job.job_id)

    # Everything below concerns a job still waiting to be processed.
    if job.status is not JobStatus.queued:
        return job

    quiet = _quiet_for(job, now)

    # ── Two-camera Ultimate ────────────────────────────────────────────────────
    if job.package.is_ultimum:
        present = _roles_with_masters(store, job.job_id)
        if len(present) == len(MEDIA_REF_ROLES):
            if (
                not job.processing_dispatched
                and recover_after > 0
                and quiet >= recover_after
            ):
                _log_recovery(job, quiet, "both cameras staged but never dispatched")
                _dispatch_via_queue(store, queue, job)
                return store.load(job.job_id)
        elif present:
            # One camera staged, the other never came, and the watchdog that owns
            # this verdict may itself have been lost — apply the same verdict, on
            # the same timeout, with the watchdog's own wording.
            timeout = settings.ultimum_second_camera_timeout_s
            if timeout > 0 and quiet >= timeout:
                missing = [r for r in MEDIA_REF_ROLES if r not in present]
                error = (
                    f"ultimum job stranded: only {present} camera footage arrived "
                    f"within {int(timeout)}s; the {missing} camera never uploaded. "
                    "Likely a missing second camera or a booking mis-mapped to the "
                    "two-camera package."
                )
                logger.warning("job %s: %s", job.job_id, error)
                return store.update(job.job_id, status=JobStatus.failed, error=error)
        return job

    # ── Single-product job ─────────────────────────────────────────────────────
    if not job.source_path:
        return job  # PENDING_CAPTURE: footage may still be on its way — not ours to judge

    if not job.processing_dispatched:
        if recover_after > 0 and quiet >= recover_after:
            _log_recovery(job, quiet, "footage staged but never dispatched")
            _dispatch_via_queue(store, queue, job)
            return store.load(job.job_id)
        return job

    timeout = settings.queued_job_timeout_s
    if timeout > 0 and quiet >= timeout:
        error = (
            f"job stalled in queue: footage was staged and processing enqueued, but "
            f"no worker started it within {int(timeout)}s. Likely a lost broker "
            "message or a dead worker — re-attach the footage to retry, or check "
            "the Celery worker."
        )
        logger.warning(
            "job %s: %s (package=%s booking=%s media_refs=%d quiet=%.0fs)",
            job.job_id,
            error,
            job.package.value,
            job.booking_id,
            len(job.media_refs),
            quiet,
        )
        return store.update(job.job_id, status=JobStatus.failed, error=error)
    return job
