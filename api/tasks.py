"""Celery tasks: the pipeline stages /api runs asynchronously.

Each task is a thin, idempotent wrapper that (1) flips the job's status, (2) calls
the existing pipeline code, and (3) records the outcome — so the REST layer stays
a pure dispatcher and all the real work is replayable from the persisted ``Job`` +
``edl.json``.

Tasks here:

* :func:`process_job` — the full edit: metadata → EDL → render → *ready_for_review*.
  (Reuses ``scripts.process_jump.process_jump``, the offline house-cut path; swap in
  ``edl.compose_edl`` once per-second scores are wired through.)
* :func:`rerender_job` — re-execute the (instructor-tweaked) EDL → *ready_for_review*.
* :func:`deliver_job` — push the approved ``final.mp4`` to the customer → *delivered*.
* :func:`pull_camera_job` — trigger an Open GoPro pull for a job created without an
  uploaded file.

Per CLAUDE.md we render for the review gate but never *deliver* before the
instructor approves, and we never call Claude in a loop (Compose is one call/jump).
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from edl.storage import load_edl
from render import render_edl

from .celery_app import celery_app
from .config import get_settings
from .jobs import Job, JobStatus, JobStore

logger = logging.getLogger(__name__)

# Repo root, anchored to this file (the project isn't installed as a package —
# `package = false` in pyproject.toml — so first-party imports rely on it being
# on sys.path). See _ensure_repo_on_path.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_repo_on_path() -> None:
    """Make the repo root importable before a task's deferred first-party imports.

    Celery loads this module inside its ``cwd_in_path()`` context manager, which puts
    the cwd on ``sys.path`` only for the duration of that import and then removes it.
    The top-level ``edl``/``render`` imports above are cached during that window, but
    imports deferred to task-execution time (``scripts.process_jump``, ``ingest.pull``
    below) run *after* cwd is gone — so without this they raise ``ModuleNotFoundError``
    in the worker. Called at task runtime (not import time, when cwd is still present
    and this would be a no-op). Mirrors the guard in ``scripts/process_jump.py``;
    idempotent and harmless when the root is already importable.
    """
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))


def _store() -> JobStore:
    """A store rooted at the configured jobs root (workers resolve it the same way)."""
    return JobStore(get_settings().jobs_root)


def _jump_date(job: Job) -> str:
    """The date burned onto the intro card — the job's, else today (render-time)."""
    return job.jump_date or date.today().isoformat()


def _notify_skydiveos(job: Job) -> None:
    """Best-effort callback to the SkydiveOS web layer on a state change.

    CLAUDE.md: "pipeline calls back here on job state changes." Fire-and-forget —
    a delivery must never fail because the web layer is briefly unreachable.
    """
    base = get_settings().skydiveos_api_base
    if not base:
        return
    try:
        import httpx

        httpx.post(
            f"{base.rstrip('/')}/jobs/{job.job_id}/status",
            json={"job_id": job.job_id, "status": job.status.value},
            timeout=5.0,
        )
    except Exception as e:  # noqa: BLE001 - never let a callback blip fail the task
        logger.warning("SkydiveOS status callback failed for %s: %r", job.job_id, e)


@celery_app.task(name="api.process_job")
def process_job(job_id: str) -> str:
    """Run the full edit for a jump and leave it ready for instructor review.

    Renders the customer-ready ``final.mp4`` (intro/outro, music, speed ramps) from
    the detected timeline. On any failure the job is marked ``failed`` with the
    error so it can be inspected and re-queued — never left stuck in ``processing``.
    """
    store = _store()
    store.update(job_id, status=JobStatus.processing, error=None)
    job = store.load(job_id)
    if not job.source_path:
        store.update(job_id, status=JobStatus.failed, error="no source media for job")
        raise RuntimeError(f"job {job_id} has no source_path")

    try:
        # Imported here (not at module load) so the FastAPI process can import this
        # module to enqueue without pulling in the heavy render/metadata stack.
        _ensure_repo_on_path()
        from scripts.process_jump import process_jump

        process_jump(
            job.source_path,
            job_id=job_id,
            customer_name=job.customer_name,
            jump_date=_jump_date(job),
            music=job.music,
            jobs_root=get_settings().jobs_root,
            target_duration=job.target_duration,
        )
    except Exception as e:  # noqa: BLE001 - surface failures as a job status, then re-raise
        logger.exception("processing failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    updated = store.update(job_id, status=JobStatus.ready_for_review)
    _notify_skydiveos(updated)
    return job_id


@celery_app.task(name="api.process_selfie_package")
def process_selfie_package(job_id: str) -> str:
    """Run the multi-clip scene pipeline for a jump (CLAUDE.md stages 2–5).

    Classifies the raw GoPro clips into scenes and scores them, then emits the
    deliverables the job's package asks for: the three videos and/or the photo set
    (selfie → both, video_only → videos, photo_only → photos). Leaves the job
    ``ready`` with its ``outputs`` populated. On any failure (including a
    low-confidence scene classification) the job is marked ``failed`` with the error,
    never left stuck in ``processing``.
    """
    store = _store()
    try:
        _ensure_repo_on_path()
        from .selfie import run_selfie_pipeline

        run_selfie_pipeline(job_id, store=store, jobs_root=get_settings().jobs_root)
    except Exception as e:  # noqa: BLE001 - surface failures as a job status, then re-raise
        logger.exception("selfie processing failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    _notify_skydiveos(store.load(job_id))
    return job_id


@celery_app.task(name="api.rerender_job")
def rerender_job(job_id: str) -> str:
    """Re-render a job from its persisted (instructor-tweaked) EDL.

    Used by the ``tweak`` endpoint: the new EDL is already saved to ``edl.json`` by
    the request handler, so here we just execute it against the source master again.
    """
    store = _store()
    store.update(job_id, status=JobStatus.processing, error=None)
    job = store.load(job_id)
    if not job.source_path:
        store.update(job_id, status=JobStatus.failed, error="no source media for job")
        raise RuntimeError(f"job {job_id} has no source_path")

    try:
        edl = load_edl(job_id, get_settings().jobs_root)
        render_edl(
            edl,
            job.source_path,
            job_id,
            customer_name=job.customer_name,
            jump_date=_jump_date(job),
            jobs_root=get_settings().jobs_root,
            music=job.music,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("re-render failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    updated = store.update(job_id, status=JobStatus.ready_for_review)
    _notify_skydiveos(updated)
    return job_id


@celery_app.task(name="api.deliver_job")
def deliver_job(job_id: str) -> str:
    """Push an approved job's ``final.mp4`` to the customer, then mark delivered.

    The actual hand-off (email link / WhatsApp / QR) is owned by the SkydiveOS web
    layer; here we confirm the render exists, notify the web layer, and flip the
    status. Guarded so we never deliver something that wasn't approved.
    """
    store = _store()
    job = store.load(job_id)
    if job.status != JobStatus.approved:
        raise RuntimeError(f"refusing to deliver job {job_id} in status {job.status}")

    final = store.final_path(job_id)
    if not final.exists():
        store.update(job_id, status=JobStatus.failed, error="approved job has no final.mp4")
        raise RuntimeError(f"job {job_id} approved but {final} is missing")

    # TODO(delivery): hand the render to the SkydiveOS delivery service (email /
    # WhatsApp / QR). For now we record delivery and let the web layer fetch it.
    logger.info("delivering job %s (%s)", job_id, final)
    updated = store.update(job_id, status=JobStatus.delivered)
    _notify_skydiveos(updated)
    return job_id


@celery_app.task(name="api.pull_camera_job")
def pull_camera_job(job_id: str, camera_id: str) -> str:
    """Trigger an Open GoPro pull for a job whose source comes off a camera.

    Stages the camera's new recordings via :mod:`ingest.pull` (which emits its own
    ``ready_for_processing`` events). The first staged MP4 for ``camera_id`` becomes
    this job's source, after which the normal :func:`process_job` runs.
    """
    import asyncio

    _ensure_repo_on_path()
    from ingest.pull import pull_camera

    store = _store()
    store.update(job_id, status=JobStatus.processing, error=None)
    try:
        jumps = asyncio.run(pull_camera(camera_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("camera pull failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    pulled = next((j for j in jumps if not j.skipped), None) or (jumps[0] if jumps else None)
    if pulled is None:
        store.update(job_id, status=JobStatus.failed, error=f"no recordings on camera {camera_id}")
        raise RuntimeError(f"no recordings pulled from camera {camera_id}")

    store.update(job_id, source_path=str(pulled.mp4_path), status=JobStatus.queued)
    # Hand off to the normal edit pipeline now that we have a master on disk.
    process_job.delay(job_id)
    return job_id
