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

Per CLAUDE.md we render for the review gate and never call Claude in a loop
(Compose is one call/jump). Delivery normally waits for the instructor's approve —
unless the deployment opts into ``AUTO_DELIVER=1``, where a finished render is
auto-approved and delivery (S3 links + customer email, :mod:`api.delivery`) fires
immediately, making the whole camera → customer flow hands-off.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date
from pathlib import Path

from edl.storage import load_edl
from render import render_edl

from . import archive
from .celery_app import celery_app
from .config import Settings, get_settings
from .jobs import Job, JobStatus, JobStore
from .lifecycle import media_state

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


#: SkydiveOS's status-callback receiver, appended to ``SKYDIVEOS_API_BASE`` (host root —
#: the same base discovery appends ``/api/media/raw-upload`` to). Must match the route
#: SkydiveOS exposes; ``{job_id}`` is filled per call.
STATUS_CALLBACK_PATH = "/api/media/auto-edit/jobs/{job_id}/status"


def _notify_skydiveos(job: Job) -> None:
    """Best-effort callback to the SkydiveOS web layer on a state change.

    CLAUDE.md: "pipeline calls back here on job state changes." Fire-and-forget —
    a delivery must never fail because the web layer is briefly unreachable. Posts to
    SkydiveOS's ``/api/media/auto-edit/jobs/{id}/status`` receiver (deduped there per
    ``{jobId}:{status}``); on ``delivered`` the presigned customer links ride along.
    """
    settings = get_settings()
    base = settings.skydiveos_api_base
    if not base:
        return
    payload: dict[str, object] = {
        "job_id": job.job_id,
        "status": job.status.value,
        "entitlement": job.entitlement.value,
        # The design doc's state-machine vocabulary (derived — see api.lifecycle), so
        # SkydiveOS's UI can label the jump without re-deriving it from two fields.
        "media_state": media_state(job).value,
    }
    if job.gallery_token and settings.public_base_url:
        # The short, never-expiring customer gallery link — what SkydiveOS should
        # SMS/email on `delivered` (it may append its own ?s= source tag).
        payload["gallery_url"] = f"{settings.public_base_url}/j/{job.gallery_token}"
    if job.delivery_links:
        # On `delivered`, forward the presigned customer links so the web layer can
        # show/send them too (its booking record knows channels we don't, e.g. WhatsApp).
        payload["delivery_links"] = job.delivery_links
    headers: dict[str, str] = {}
    if settings.auto_edit_callback_token:
        headers["X-Auto-Edit-Token"] = settings.auto_edit_callback_token
    url = f"{base.rstrip('/')}{STATUS_CALLBACK_PATH.format(job_id=job.job_id)}"
    try:
        import httpx

        httpx.post(url, json=payload, headers=headers, timeout=5.0)
    except Exception as e:  # noqa: BLE001 - never let a callback blip fail the task
        logger.warning("SkydiveOS status callback failed for %s: %r", job.job_id, e)


def _archive_deliverables(store: JobStore, job_id: str) -> None:
    """Mirror a finished job's renders into the browsable jump archive.

    Called at every "render finished" seam so the archive folder
    (``<raw-storage>/{date}/{instructor}/{customer}/``) always holds the current cut
    beside the footage it came from. :mod:`api.archive` never raises — a mirror must
    not fail a customer's edit.
    """
    archive.archive_deliverables(store.load(job_id), store, get_settings())


def _render_previews(store: JobStore, job_id: str) -> None:
    """Render the watermarked 720p previews for a Path-B (``preview_only``) job.

    Called at every "render finished" seam, INSIDE the task's ``try`` — a
    ``preview_only`` job whose previews can't be produced must fail (a locked
    gallery with nothing watchable breaks the product), while an
    ``edited_download`` job returns immediately and gains no new failure mode.
    """
    from .preview import render_job_previews

    render_job_previews(store.load(job_id), store, get_settings())


def _maybe_auto_deliver(store: JobStore, job_id: str) -> None:
    """Skip the review gate when ``AUTO_DELIVER`` is on: approve + enqueue delivery.

    Called at every "render finished" site. A no-op unless the setting is enabled
    and the job just landed in a reviewable done-state, so the default flow (wait
    for the instructor's ``POST /approve``) is untouched.
    """
    if not get_settings().auto_deliver:
        return
    job = store.load(job_id)
    if job.status not in (JobStatus.ready_for_review, JobStatus.ready):
        return
    store.update(job_id, status=JobStatus.approved)
    logger.info("AUTO_DELIVER: job %s auto-approved, delivery enqueued", job_id)
    deliver_job.delay(job_id)


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
        _render_previews(store, job_id)
    except Exception as e:  # noqa: BLE001 - surface failures as a job status, then re-raise
        logger.exception("processing failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    updated = store.update(job_id, status=JobStatus.ready_for_review)
    _notify_skydiveos(updated)
    _archive_deliverables(store, job_id)
    _maybe_auto_deliver(store, job_id)
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
        _render_previews(store, job_id)
    except Exception as e:  # noqa: BLE001 - surface failures as a job status, then re-raise
        logger.exception("selfie processing failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    _notify_skydiveos(store.load(job_id))
    _archive_deliverables(store, job_id)
    _maybe_auto_deliver(store, job_id)
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
        _render_previews(store, job_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("re-render failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    updated = store.update(job_id, status=JobStatus.ready_for_review)
    _notify_skydiveos(updated)
    _archive_deliverables(store, job_id)
    _maybe_auto_deliver(store, job_id)
    return job_id


@celery_app.task(name="api.deliver_job")
def deliver_job(job_id: str) -> str:
    """Push an approved job's renders to the customer, then mark delivered.

    Uploads every rendered deliverable (``final.mp4``, or the package ``outputs``)
    to S3, presigns download links, and emails them to ``customer_email``
    (:mod:`api.delivery`); the links are persisted on the job and forwarded to
    SkydiveOS in the status callback. Guarded so we never deliver something that
    wasn't approved; a failed hand-off marks the job ``failed`` for re-queueing
    rather than pretending it was delivered.
    """
    store = _store()
    try:
        job = store.load(job_id)
    except FileNotFoundError:
        # Deleted while this delivery sat in the queue (operator cleanup) — there
        # is nothing to send, and crashing would just retry into the same wall.
        logger.warning("delivery for unknown job %s — dropping", job_id)
        return job_id
    if job.status != JobStatus.approved:
        raise RuntimeError(f"refusing to deliver job {job_id} in status {job.status}")

    try:
        from .delivery import deliver_to_customer

        links = deliver_to_customer(job, store, get_settings())
    except Exception as e:  # noqa: BLE001 - surface failures as a job status, then re-raise
        logger.exception("delivery failed for job %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    updated = store.update(job_id, status=JobStatus.delivered, delivery_links=links)
    _notify_skydiveos(updated)
    # Record what the customer got in the jump's archive manifest, so the folder alone
    # answers "was this delivered, and to where?".
    archive.archive_delivery(updated, get_settings())
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

    updated = store.update(job_id, source_path=str(pulled.mp4_path), status=JobStatus.queued)
    # The pull staged into the camera card mirror (raw-storage/_camera-staging/...);
    # file the master under the jump in the browsable archive too, now that this job
    # tells us whose jump it is.
    archive.archive_raw_footage(updated, store, get_settings())
    # Hand off to the normal edit pipeline now that we have a master on disk.
    process_job.delay(job_id)
    return job_id


def _download_s3(s3_key: str, dest: Path, settings: Settings) -> None:
    """Download ``s3_key`` from the configured bucket to ``dest`` (created if needed).

    Streams straight to disk via boto3's managed transfer so a multi-GB master never
    lands in memory. ``boto3`` is imported lazily — only the S3-ingest path needs it.
    """
    if not settings.s3_bucket:
        raise RuntimeError("S3 ingest needs S3_BUCKET (or AWS_S3_BUCKET_NAME) to be set")
    import boto3

    client = boto3.client(
        "s3", endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(settings.s3_bucket, s3_key, str(dest))


@celery_app.task(name="api.ingest_s3_job")
def ingest_s3_job(job_id: str, s3_key: str, camera_role: str | None = None) -> str:
    """Source a job from a raw master already staged in S3 (the auto-discovery path).

    Auto-discovery uploads each pulled MP4 to ``s3://$S3_BUCKET/raw/...`` and notifies
    SkydiveOS; SkydiveOS creates the job here and points it at that key instead of
    re-uploading the (multi-GB) bytes through the web layer. This downloads the object
    into the job's ``raw/`` staging (per-``camera_role`` for the two-camera Ultimate
    package), then hands off to the SAME pipeline dispatch a byte upload would — so the
    editing path is identical regardless of how the footage arrived.
    """
    _ensure_repo_on_path()
    store = _store()
    settings = get_settings()
    job = store.load(job_id)
    filename = Path(s3_key).name

    if job.package.is_ultimum:
        if camera_role not in ("instructor", "external"):
            store.update(job_id, status=JobStatus.failed,
                         error=f"ultimum S3 ingest needs a valid camera_role (got {camera_role!r})")
            raise RuntimeError(f"job {job_id}: ultimum S3 ingest without a valid camera_role")
        dest = store.camera_raw_dir(job_id, camera_role) / filename
    else:
        dest = store.raw_dir(job_id) / filename

    try:
        _download_s3(s3_key, dest, settings)
    except Exception as e:  # noqa: BLE001 - surface as a job status, then re-raise
        logger.exception("S3 ingest failed for job %s (key %s)", job_id, s3_key)
        store.update(job_id, status=JobStatus.failed, error=f"S3 ingest failed: {e}")
        raise

    # Record where this master lives in S3 — the disk-retention authority: the
    # pruner deletes the local copy only after re-confirming exactly this key.
    job = store.load(job_id)
    store.update(job_id, raw_s3_keys={**job.raw_s3_keys, filename: s3_key})

    # File the freshly-downloaded master under the jump in the browsable archive before
    # any editing runs, so the raw footage is preserved even if the edit later fails.
    archive.archive_raw_footage(job, store, settings)

    # Two-camera Ultimate: wait until BOTH roles are on disk before processing.
    if job.package.is_ultimum:
        from .selfie import CAMERA_ROLES

        # Stamp every clip: each camera's jump arrives as SEVERAL per-clip
        # notifications, so "both roles present" alone is NOT dispatchable — the
        # first external clip landing next to the instructor's set would render a
        # partial multi-cam edit (observed live: 2 of 8 cameraman clips made the
        # cut). Both-roles-present only makes the job ELIGIBLE; the settle window
        # decides WHEN, exactly like the single-camera path below.
        store.update(job_id, status=JobStatus.queued, error=None, last_raw_clip_at=time.time())
        if store.camera_roles_present(job_id, CAMERA_ROLES):
            settle = get_settings().raw_clip_settle_seconds
            if settle <= 0:
                # Opt-out: dispatch immediately — through the guard, not a bare
                # .delay(): a re-notified clip arriving after both roles are on disk
                # would otherwise start a second render of the same job.
                _dispatch_processing(store, job_id)
            else:
                raw_clips_settled_job.apply_async((job_id,), countdown=settle)
        else:
            # Only one camera so far. Arm a watchdog so a never-arriving second camera
            # (or a package mis-mapped to ultimum) fails the job instead of stranding it
            # in `queued` forever. Skipped under eager mode (single-process demo/tests
            # drive both uploads themselves, and countdown scheduling is a no-op there).
            if not settings.task_always_eager:
                ultimum_watchdog_job.apply_async(
                    (job_id,), countdown=settings.ultimum_second_camera_timeout_s
                )
        return job_id

    # Single-camera packages cut from a single source master; point at the downloaded MP4.
    store.update(
        job_id,
        source_path=str(dest),
        status=JobStatus.queued,
        error=None,
        last_raw_clip_at=time.time(),
    )

    # One jump can arrive as SEVERAL clips: SkydiveOS notifies once per clip (a GoPro
    # chapters a 4 GB master; an instructor stops/starts recording), and each
    # notification is its own `POST /jobs/{id}/upload`. Dispatching here would then
    # start one render PER CLIP on the same job — concurrent renders sharing a job dir,
    # each cutting whatever subset had landed, and with AUTO_DELIVER on the first to
    # finish emails the customer a partial edit. So wait for the clips to go quiet
    # (same shape as the ultimum watchdog: re-schedule rather than cancel), and let
    # exactly one settle check dispatch.
    settle = get_settings().raw_clip_settle_seconds
    if settle <= 0:
        _dispatch_processing(store, job_id)  # opt-out: dispatch immediately
    else:
        raw_clips_settled_job.apply_async((job_id,), countdown=settle)
    return job_id


def _dispatch_processing(store: JobStore, job_id: str) -> bool:
    """Enqueue the pipeline for ``job_id`` exactly once. True if this call dispatched.

    The guard is what makes the multi-clip ``s3_key`` path safe: a late clip re-arming
    the settle check, two settle checks overlapping, or a duplicate notification must
    never produce a second render of the same job.
    """
    job = store.load(job_id)
    if job.processing_dispatched:
        logger.info("job %s already dispatched — not enqueuing a second render", job_id)
        return False
    store.update(job_id, processing_dispatched=True)
    if job.package.uses_scene_pipeline:
        process_selfie_package.delay(job_id)
    else:
        process_job.delay(job_id)
    return True


@celery_app.task(name="api.raw_clips_settled_job")
def raw_clips_settled_job(job_id: str) -> str:
    """Dispatch processing once a job's raw clips have stopped arriving.

    Armed (with a countdown) by every ``s3_key`` ingest. If another clip landed since,
    this re-schedules itself instead of dispatching — so the pipeline always sees the
    WHOLE jump. Re-scheduling (rather than cancelling the previous countdown, which
    Celery can't do reliably) is the same trick :func:`ultimum_watchdog_job` uses.

    A no-op once the job has been dispatched, has moved past ``queued``, or was failed
    /rejected in the meantime.
    """
    _ensure_repo_on_path()

    settings = get_settings()
    store = _store()
    try:
        job = store.load(job_id)
    except FileNotFoundError:
        # The job was deleted while this check sat in the queue (an operator
        # cleanup, or a stale task from another environment sharing the broker).
        # A missing job has nothing to dispatch — don't crash-loop the worker.
        logger.warning("settle check for unknown job %s — dropping", job_id)
        return job_id

    if job.processing_dispatched or job.status != JobStatus.queued:
        return job_id  # already underway, or no longer waiting to be processed

    # A missing stamp must read as "not settled", never as "quiet forever": the
    # stamp can be absent when this check races the ingest's own update (two
    # writers on job.json — observed on a lagging machine, where it dispatched a
    # 1-clip render of an 8-clip jump). updated_at moves on every write, so it is
    # a safe lower bound for "something happened recently".
    stamp = job.last_raw_clip_at or job.updated_at or time.time()
    quiet_for = time.time() - stamp
    if quiet_for < settings.raw_clip_settle_seconds:
        # Still arriving — check again shortly. Poll interval rather than the full
        # settle window so a jump that just went quiet isn't delayed by a whole window.
        raw_clips_settled_job.apply_async(
            (job_id,), countdown=settings.raw_clip_settle_poll_seconds
        )
        return job_id

    if job.package.is_ultimum:
        from .selfie import CAMERA_ROLES

        if not store.camera_roles_present(job_id, CAMERA_ROLES):
            # Quiet, but the second camera hasn't arrived: not dispatchable. Its own
            # ingest will arm a fresh settle check when it lands; the ultimum
            # watchdog owns the never-arrives case. Dispatching here would render a
            # one-camera "Ultimate".
            return job_id

    n_clips = len(list(store.raw_dir(job_id).glob("*"))) if store.raw_dir(job_id).exists() else 0
    logger.info(
        "job %s: raw clips settled (quiet %.0fs, %d file(s) staged) — dispatching",
        job_id, quiet_for, n_clips,
    )
    _dispatch_processing(store, job_id)
    return job_id


@celery_app.task(name="api.ultimum_watchdog_job")
def ultimum_watchdog_job(job_id: str) -> str:
    """Fail an Ultimate job whose SECOND camera never arrived, instead of hanging.

    Armed (with a countdown) when the first camera's footage lands for an ``ultimum``
    job and both roles aren't yet present. When it fires: if the job is *still* waiting
    (``queued`` with only one role on disk) it's marked ``failed`` with an actionable
    error — the second camera never uploaded, or the booking was mis-mapped to the
    two-camera package. A no-op if the second camera did arrive (the job has since
    progressed past ``queued``, or both roles are now present), so a normal two-camera
    jump is never affected.
    """
    # ``api.selfie`` pulls in top-level ``analysis``, and this import is deferred to
    # task-execution time — after Celery has taken cwd back off sys.path. Without this
    # the watchdog dies with ModuleNotFoundError and the stranded job it exists to fail
    # stays in ``queued`` forever, which is the exact bug it guards against.
    _ensure_repo_on_path()

    from .selfie import CAMERA_ROLES

    store = _store()
    try:
        job = store.load(job_id)
    except FileNotFoundError:
        # Deleted while the countdown ran (operator cleanup) — nothing to fail.
        logger.warning("ultimum watchdog for unknown job %s — dropping", job_id)
        return job_id
    if job.status != JobStatus.queued or store.camera_roles_present(job_id, CAMERA_ROLES):
        return job_id  # second camera arrived / job progressed — nothing to do

    # Case-insensitive: GoPro masters are ``.MP4``, so a ``*.mp4`` glob finds nothing on
    # a case-sensitive filesystem and the error would misreport which camera did arrive.
    present = [
        r for r in CAMERA_ROLES
        if store.camera_raw_dir(job_id, r).exists()
        and any(
            p.suffix.lower() == ".mp4" for p in store.camera_raw_dir(job_id, r).glob("*")
        )
    ]
    missing = [r for r in CAMERA_ROLES if r not in present]
    timeout = int(get_settings().ultimum_second_camera_timeout_s)
    error = (
        f"ultimum job stranded: only {present or ['no']} camera footage arrived within "
        f"{timeout}s; the {missing} camera never uploaded. Likely a missing second "
        f"camera or a booking mis-mapped to the two-camera package."
    )
    logger.warning("job %s: %s", job_id, error)
    store.update(job_id, status=JobStatus.failed, error=error)
    return job_id
