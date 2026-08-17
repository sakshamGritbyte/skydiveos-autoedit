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

import json
import logging
import sys
import time
import uuid
from datetime import date
from pathlib import Path

from edl.storage import load_edl
from render import render_edl

from . import archive
from .celery_app import celery_app
from .config import Settings, get_settings
from .jobs import (
    MEDIA_REF_ROLES,
    Entitlement,
    Job,
    JobKind,
    JobStatus,
    JobStore,
    LoadRosterEntry,
    RoleIngest,
    deliverable_names,
    entitlement_for,
)
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
        # Which KIND of job this is (see api.jobs.JobKind). Always sent, including the
        # "jump" default, so SkydiveOS can branch without inferring absence: a
        # `load_child` is a customer-facing gallery it never created and has no booking
        # for, and it must not be reported as a jump that lost its booking.
        "job_kind": job.job_kind.value,
    }
    if job.deliverable_access:
        # A MIXED job's real lock state, which the single ``entitlement`` above CANNOT
        # express: it reads `edited_download` because the customer bought the handcam edit,
        # while the camera-flyer edit beside it is still behind the paywall. Without this
        # SkydiveOS's offer page falls back to `entitlement`, concludes the spec edit is
        # already owned, and gives it away for nothing.
        #
        # Sent fully RESOLVED (every video deliverable, not just the explicit entries) so
        # the consumer never has to reimplement the inherit-from-job rule — and sent ONLY
        # when there is explicit per-deliverable state, so an ordinary job's payload stays
        # byte-identical to what SkydiveOS receives today.
        payload["deliverable_entitlements"] = {
            name: entitlement_for(job, name).value for name in deliverable_names(job)
        }
    if job.media_refs:
        # What this job was opened with, echoed back so SkydiveOS can reconcile the
        # products it manifested against the products we actually recorded.
        payload["media_refs"] = [
            {"role": r.role, "package": r.package.value, "entitlement": r.entitlement.value}
            for r in job.media_refs
        ]
    if job.load_id:
        # Groups a spec flight's master and all its child galleries under one load, which
        # is the only handle SkydiveOS has for them — a child has no booking (nothing was
        # purchased) so `booking_id` below cannot do this job.
        payload["load_id"] = job.load_id
    if job.jumper_index is not None:
        # Which manifest slot, so a per-jumper media chip can be rendered on the load.
        payload["jumper_index"] = job.jumper_index
    if job.source_job_id:
        # The load master this gallery streams — lets SkydiveOS show "Load 17 aerial
        # video" against the master's render rather than treating the child as orphaned.
        payload["source_job_id"] = job.source_job_id
    if job.gallery_token and settings.public_base_url:
        # The short, never-expiring customer gallery link — what SkydiveOS should
        # SMS/email on `delivered` (it may append its own ?s= source tag).
        payload["gallery_url"] = f"{settings.public_base_url}/j/{job.gallery_token}"
    if job.delivery_links:
        # On `delivered`, forward the presigned customer links so the web layer can
        # show/send them too (its booking record knows channels we don't, e.g. WhatsApp).
        payload["delivery_links"] = job.delivery_links
    # Identity, so SkydiveOS can LINK a job it did not create. Its receiver upserts a
    # media record for an unknown job_id (that is what makes the paywall sellable for
    # jobs the bridge creates) — but without these it is booking-less, so reporting is
    # blind and its own footage-matcher could later open a second job for the same
    # booking with nothing to correlate them. All three are gap-fill: absent when we
    # don't know them, and the receiver never lets them move a link it matched itself.
    if job.booking_id:
        payload["booking_id"] = job.booking_id
    if job.customer_email:
        payload["customer_email"] = job.customer_email
    if job.customer_name:
        payload["customer_name"] = job.customer_name
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


def _render_posters(store: JobStore, job_id: str) -> None:
    """Pre-build each deliverable's gallery poster frame (:mod:`api.thumbnail`).

    Called at every "render finished" seam, *beside* the archive mirror and outside the
    task's ``try`` — and, like the archive, it **never raises**: a card with no poster
    falls back to the browser's placeholder, which is not worth failing a customer's
    edit over. Doing it here (rather than only lazily on first request) means the
    customer's first page load is already warm, and the previews it posters for a
    locked job have just been rendered.
    """
    from .thumbnail import render_job_posters

    render_job_posters(store.load(job_id), store, get_settings())


def _auto_deliver_block(store: JobStore, job: Job) -> str | None:
    """Why this job must NOT be auto-delivered, or ``None`` when it's safe to send.

    The jump-evidence gate for a **customer** job, and the answer to the audit's 🔴-3
    (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-J). A clip set with no jump in it still
    renders a complete-looking edit: :func:`api.selfie._curated_freefall` deliberately
    stands in the first scene when there is no ``freefall`` scene "so the EDL is valid",
    which is right for the renderer and wrong for the customer — an interview-only card
    (the normal outcome of a split/stale manifest, §3-B) became a delivered "your
    skydive video is ready".

    So the evidence test that already guards a load master's fan-out
    (:func:`_flew_a_jump`) is applied to customer jobs too, at the auto-deliver seam:

    * **Hold, don't fail.** The render is kept and the job stays in its reviewable state
      with :attr:`api.jobs.Job.hold_reason` set. Scene classification is a heuristic; a
      false negative on a real jump must cost a human glance, not a customer's video. The
      instructor's ``POST /approve`` still delivers it — that is the deliberate override.
    * **Only when we actually have evidence to judge.** No scene manifest at all (the
      single-master :func:`process_job` path, which segments a timeline instead of
      building scenes) means "unknown", and unknown must not block a legitimate job.
    * **Never a load master.** :func:`fan_out_load_job` owns that decision.
    """
    if job.job_kind is JobKind.load_master:
        return None
    if not _scene_manifests(store, job.job_id):
        return None
    if _flew_a_jump(store, job.job_id):
        return None
    return (
        "no jump/freefall scene in this job's footage, so the render is not evidence of a "
        "skydive (an interview-only clip set still produces a valid-looking edit). Held "
        "back from automatic delivery — review the footage and the scene classification "
        "(scene_manifest*.json); approving by hand still delivers it."
    )


def _maybe_auto_deliver(store: JobStore, job_id: str) -> None:
    """Skip the review gate when ``AUTO_DELIVER`` is on: approve + enqueue delivery.

    Called at every "render finished" site. A no-op unless the setting is enabled
    and the job just landed in a reviewable done-state, so the default flow (wait
    for the instructor's ``POST /approve``) is untouched.

    A **load master** is approved the same way but hands off to :func:`fan_out_load_job`
    instead of :func:`deliver_job`: it has no customer to deliver to, and what its render
    unlocks is the *fan-out* — one child gallery per no-media jumper on the load.

    A job with no jump evidence is held for a human instead (:func:`_auto_deliver_block`).
    """
    job = store.load(job_id)
    if job.status not in (JobStatus.ready_for_review, JobStatus.ready):
        return
    if job.hold_reason:
        # A fresh render just reached a reviewable state, so any hold from the previous
        # run is stale: clear it before re-deciding (and unconditionally, since the flag
        # only ever means "auto-delivery declined to send this render").
        job = store.update(job_id, hold_reason=None)
    if not get_settings().auto_deliver:
        return
    blocked = _auto_deliver_block(store, job)
    if blocked is not None:
        store.update(job_id, hold_reason=blocked)
        logger.warning("AUTO_DELIVER: job %s HELD for review — %s", job_id, blocked)
        return
    store.update(job_id, status=JobStatus.approved)
    if job.job_kind is JobKind.load_master:
        logger.info("AUTO_DELIVER: load master %s auto-approved, fan-out enqueued", job_id)
        fan_out_load_job.delay(job_id)
        return
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
    _render_posters(store, job_id)
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
    _render_posters(store, job_id)
    _maybe_auto_deliver(store, job_id)
    return job_id


@celery_app.task(name="api.process_media_ref_job")
def process_media_ref_job(job_id: str, role: str) -> str:
    """Render ONE media product of a mixed job, from ONE camera's footage.

    A jumper can hold two products — a paid handcam edit and a speculative camera-flyer
    one — on a single job so the customer gets a single gallery link. Each camera's card
    dispatches its own pass here, independently: the paid edit is delivered as soon as its
    clips are quiet, and the spec edit joins the SAME gallery whenever its card turns up
    (the page is a live route, so a deliverable added later simply appears).

    The customer is emailed exactly once no matter how many passes run —
    :func:`api.delivery.send_gallery_email_once` owns that, keyed on ``email_sent_at``.
    """
    store = _store()
    try:
        _ensure_repo_on_path()
        from .selfie import run_media_ref_pipeline

        run_media_ref_pipeline(job_id, role, store=store, jobs_root=get_settings().jobs_root)
        _render_previews(store, job_id)
    except Exception as e:  # noqa: BLE001 - surface failures as a job status, then re-raise
        logger.exception("media-ref processing failed for job %s role %s", job_id, role)
        store.update(job_id, status=JobStatus.failed, error=f"[{role}] {e}")
        raise

    _notify_skydiveos(store.load(job_id))
    _archive_deliverables(store, job_id)
    _render_posters(store, job_id)
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
    _render_posters(store, job_id)
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
    if job.superseded_by:
        # A retired load child whose gallery was adopted by the customer's own job
        # (api.app.create_job) — its link now serves that job, and delivering here
        # would email a second (dead) link. Nothing to send.
        logger.info("job %s was superseded by %s — skipping delivery", job_id, job.superseded_by)
        return job_id
    if job.status != JobStatus.approved:
        raise RuntimeError(f"refusing to deliver job {job_id} in status {job.status}")
    if job.job_kind is JobKind.load_master:
        # A load master has no customer: its "delivery" is the fan-out. Nothing should
        # route it here (``_maybe_auto_deliver`` sends it to fan_out_load_job and the
        # approve endpoint refuses it), so this is a backstop against a hand-queued task
        # emailing a load's video to nobody — or worse, to the roster's first name.
        raise RuntimeError(
            f"job {job_id} is a load master; it fans out (fan_out_load_job) rather than "
            "being delivered to a customer"
        )

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


#: Scene label that proves a card's footage really is a jump — required before a spec
#: flight is offered as a load video, and before ANY job is delivered automatically.
#: The freefall scene is the classifier's most reliable label by a distance (an
#: accelerometer signature, not a position heuristic — see ``AUDIT_SCENE_LABELS.md``),
#: which is what makes it usable as a "this really was a jump" gate.
_JUMP_EVIDENCE_SCENE = "freefall"


def _scene_manifests(store: JobStore, job_id: str) -> list[Path]:
    """Every scene manifest this job produced (``scene_manifest*.json``).

    One file for a single-camera package; one **per camera** for Ultimate
    (``scene_manifest_instructor.json`` / ``_external.json``), because each camera is
    classified into its own scene set. An empty list means the job never built scenes at
    all — the single-master :func:`process_job` path, which segments a timeline instead —
    and callers must read that as "no evidence either way", never as "no jump".
    """
    job_dir = store.dir(job_id)
    if not job_dir.is_dir():
        return []
    return sorted(job_dir.glob("scene_manifest*.json"))


def _flew_a_jump(store: JobStore, job_id: str) -> bool:
    """Whether this job's scenes contain freefall — i.e. the card really is a jump.

    Two callers, same question from different directions:

    * **A load master** (:func:`fan_out_load_job`): the spec-flight match resolves a load
      from the capture *timestamp* alone — there is no crew field on a load document to
      confirm the flyer was aboard (``ingest.match.resolve_load_for_staff``) — so a card
      filmed on the ground between loads would resolve to the nearest departed load and,
      unguarded, become a "load video" offered to five customers who were never on it.
    * **A customer job** (:func:`_auto_deliver_block`): an interview-only clip set renders
      a valid-looking edit anyway (``api.selfie._curated_freefall``'s stand-in), so
      without this it is auto-delivered as a finished skydive video.

    The cheap second opinion in both cases, taken from the footage itself rather than the
    manifest. ``True`` if ANY of the job's scene manifests carries a freefall scene: on an
    Ultimate jump each camera has its own manifest, and one camera's freefall is proof the
    jump happened even if the other card is short. Missing/unreadable manifests →
    ``False``; the callers decide what that means (the load master refuses, the customer
    job first checks whether scenes exist at all).
    """
    for manifest_path in _scene_manifests(store, job_id):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        # Scene entries are keyed ``name`` (not ``scene`` — that is the EDL *clip* field);
        # ``edl.validate`` and ``api.selfie`` both read them this way.
        if any(
            str(scene.get("name", "")).lower() == _JUMP_EVIDENCE_SCENE
            for scene in manifest.get("scenes", [])
        ):
            return True
    return False


def _pointer_job(store: JobStore, master_id: str, jumper_index: int | None) -> Job | None:
    """An existing job already pointing at this master for this manifest slot, if any.

    The fan-out's idempotency key (``jobs are idempotent and resumable``): a re-run finds
    what it created last time instead of opening a second gallery for the same customer.
    """
    for job in store.list_jobs():
        if job.source_job_id == master_id and job.jumper_index == jumper_index:
            return job
    return None


def _is_own_job(job: Job, master: Job, entry: LoadRosterEntry) -> bool:
    """Whether ``job`` is this roster entry's OWN jump job on the master's load.

    Deliberately two independent join keys, because neither is reliable alone and the two
    sides of the integration populate different ones:

    * **``booking_id``** — the stable identity. Survives the manifest being edited, which
      the positional key does not: remove a jumper from a load and every later index
      shifts, so a stored ``jumper_index`` can come to name a different customer.
    * **``(load_id, jumper_index)``** — the positional key. The only one available when a
      job carries no booking id, or when the two sides spell booking ids differently.

    Why both, concretely: our bridge sends `load_id` + `jumper_index` on a jump job, but in
    production **SkydiveOS** creates these jobs and its payload carries neither — only
    `booking_id`. With one key, whichever side happens to be creating jobs silently gets no
    tile at all, which is exactly a missed sale with a WARNING as its only trace.

    A caveat for whoever populates the roster: the roster's ``booking_id`` must be the SAME
    identifier the jump job carries. Ours is the booking ObjectId on both sides
    (`ingest.match`), so they join. SkydiveOS uses `booking.bookingNumber` on jump jobs, so
    its roster must use `bookingNumber` too — an ObjectId there would compare against a
    human ref and never match.
    """
    if job.job_kind is not JobKind.jump:
        return False
    if entry.booking_id and job.booking_id and job.booking_id == entry.booking_id:
        return True
    return (
        master.load_id is not None
        and job.load_id == master.load_id
        and job.jumper_index is not None
        and job.jumper_index == entry.jumper_index
    )


def _fan_out_to_jumper(
    store: JobStore, master: Job, entry: LoadRosterEntry, existing: list[Job]
) -> str | None:
    """Offer the load video to ONE jumper. Returns a log word, or ``None`` if skipped.

    Two tiers, and the test between them is **"do they already have a gallery of their
    own?"** — deliberately NOT "did they buy media?":

    * **has their own job** → stamp ``source_job_id`` on it and stop. The load video becomes
      an upsell tile in the page they are already opening: one customer, one link, no second
      email. Their own deliverables, status and entitlement are untouched.
    * **has no job at all** → a ``load_child`` gallery: their name, their short code, their
      unlock, ``preview_only`` so it streams the master's watermarked preview. Approved on
      creation because there is no edit of its own to review — the master's edit already
      was — then handed to the normal delivery task.

    Why not ``bought_media``: a jumper who bought NOTHING still normally has a gallery,
    because the instructor's handcam films every tandem anyway — that footage becomes a
    speculative ``selfie`` + ``preview_only`` job with its own token and its own unlock
    (``ingest.match.package_and_entitlement_for``). Branching on the purchase would create a
    child ON TOP of it: two links and two emails to one customer, the same failure class as
    the 2026-08-06 four-emails incident. ``bought_media`` survives only to tell the two
    "no job found" cases apart (below).

    Ordering: a non-buyer with no job yet gets a child here, and if their own footage is
    ingested *afterwards*, ``api.app.create_job`` adopts the child's gallery token onto
    the new job (and retires the child, ``superseded_by``) — so the link the customer
    already received keeps working and they never end up with two galleries. Nothing in
    this function can see a job that does not exist yet; the fix lives at job-creation.
    """
    own = next((j for j in existing if _is_own_job(j, master, entry)), None)
    if own is not None:
        if own.source_job_id == master.job_id:
            return None  # already stamped (a re-run)
        store.update(own.job_id, source_job_id=master.job_id)
        return f"tile→{own.job_id}"

    if entry.bought_media:
        # They PAID for media, so a job of their own is coming (SkydiveOS may have created
        # it already, or their footage hasn't landed). A child would become their SECOND
        # link the moment it appears — skip and say so rather than guess.
        logger.warning(
            "load master %s: jumper %s (%s) bought media but has no job on this load "
            "yet — no load-video tile offered",
            master.job_id, entry.jumper_index, entry.customer_name,
        )
        return None

    if _pointer_job(store, master.job_id, entry.jumper_index) is not None:
        return None  # this child already exists (a re-run)

    child_id = uuid.uuid4().hex
    store.create(
        Job(
            job_id=child_id,
            status=JobStatus.approved,
            job_kind=JobKind.load_child,
            source_job_id=master.job_id,
            load_id=master.load_id,
            load_label=master.load_label,
            jumper_index=entry.jumper_index,
            customer_name=entry.customer_name or "Valued Skydiver",
            customer_email=entry.customer_email,
            booking_id=entry.booking_id,
            jump_date=master.jump_date,
            package=master.package,
            # Locked by definition: they bought nothing, so the whole point is a
            # watermarked taste behind an unlock CTA.
            entitlement=Entitlement.preview_only,
            instructor_id=master.instructor_id,
            instructor_name=master.instructor_name,
        )
    )
    store.ensure_gallery_token(child_id)
    deliver_job.delay(child_id)
    return f"child→{child_id}"


@celery_app.task(name="api.fan_out_load_job")
def fan_out_load_job(job_id: str) -> str:
    """Offer a finished load master to everybody on its load (the upsell fan-out).

    The load master owns the only render; this turns it into one offer per customer
    without editing anything again (the class-photo principle: shoot the class once, sell
    copies per family). Steps, in order:

    1. **Freefall guard** (:func:`_flew_a_jump`) — no freefall in the master's scenes means
       the timestamp-resolved load is not evidence enough that this card is a jump at all,
       so the job fails with an actionable error and **no** child is created.
    2. The deliverables upload to S3 as the durable copy, with ``presign=False``: a
       presigned URL carries no entitlement check, and every gallery hanging off this
       master is locked.
    3. Each roster entry is offered the video (:func:`_fan_out_to_jumper`).

    Idempotent — a re-run re-stamps nothing and re-creates nothing (a second run would
    otherwise email one customer twice, the failure mode the whole settle window exists to
    prevent).
    """
    store = _store()
    job = store.load(job_id)
    if job.job_kind is not JobKind.load_master:
        raise RuntimeError(f"job {job_id} is not a load master ({job.job_kind})")
    if job.status != JobStatus.approved:
        raise RuntimeError(f"refusing to fan out job {job_id} in status {job.status}")

    try:
        # A master is TIMESTAMP-resolved (spec flight): the window alone can't prove
        # the flyer was aboard, so the footage itself must show a jump.
        if not _flew_a_jump(store, job_id):
            raise RuntimeError(
                f"load master {job_id} has no freefall scene, so there is no evidence its "
                "footage is a jump on the load its timestamps matched. Refusing to offer "
                "it to that load's customers. If this really is a spec flight, check the "
                "scene classification (jobs/<id>/scene_manifest.json) and re-queue."
            )
        settings = get_settings()
        from .delivery import collect_deliverables, upload_and_link

        files = collect_deliverables(job, store)
        if not files:
            raise RuntimeError(f"load master {job_id} has no rendered deliverables")
        # Durable copy only. NOT presigned: every gallery that streams these bytes is
        # behind a paywall, and a presigned URL answers to whoever holds it.
        upload_and_link(files, job_id=job_id, settings=settings, presign=False)

        existing = store.list_jobs()
        actions = [
            action
            for entry in job.load_roster
            if (action := _fan_out_to_jumper(store, job, entry, existing)) is not None
        ]
    except Exception as e:  # noqa: BLE001 - surface as a job status, then re-raise
        logger.exception("fan-out failed for load master %s", job_id)
        store.update(job_id, status=JobStatus.failed, error=str(e))
        raise

    updated = store.update(job_id, status=JobStatus.delivered)
    logger.info(
        "load master %s fanned out to %d of %d on %s: %s",
        job_id, len(actions), len(job.load_roster), job.load_label or "the load",
        ", ".join(actions) or "nobody",
    )
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

    if job.staged_by_camera_role:
        # Two products (or two cameras) on one job: the clips must be kept apart, both
        # because two GoPros emit colliding filenames and because a mixed job resolves
        # WHICH product a clip feeds — and therefore whether the result is watermarked —
        # from its role alone. A missing role is refused rather than defaulted.
        if camera_role not in MEDIA_REF_ROLES:
            what = "ultimum" if job.package.is_ultimum else "multi-product"
            store.update(job_id, status=JobStatus.failed,
                         error=f"{what} S3 ingest needs a valid camera_role (got {camera_role!r})")
            raise RuntimeError(f"job {job_id}: {what} S3 ingest without a valid camera_role")
        if job.is_multi_ref and job.ref_for_role(camera_role) is None:
            store.update(
                job_id,
                status=JobStatus.failed,
                error=(
                    f"no media product on this job for camera_role {camera_role!r} "
                    f"(have {[r.role for r in job.media_refs]}) — refusing to guess which "
                    "product this footage belongs to"
                ),
            )
            raise RuntimeError(f"job {job_id}: no media ref for role {camera_role!r}")
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

    # Mixed job: this role's product renders from this role's footage, on its own settle
    # window. Deliberately NOT "wait for both cameras" like ultimum below — the two are
    # separate products, and the one the customer PAID for must not be held hostage to a
    # speculative card that may arrive much later or never.
    if job.is_multi_ref:
        assert camera_role is not None  # staged_by_camera_role refused a missing role
        state = job.role_ingest.get(camera_role) or RoleIngest()
        store.update(
            job_id,
            error=None,
            role_ingest={
                **job.role_ingest,
                camera_role: state.model_copy(update={"last_clip_at": time.time()}),
            },
        )
        settle = get_settings().raw_clip_settle_seconds
        if settle <= 0:
            _dispatch_processing(store, job_id, camera_role)  # opt-out: dispatch now
        else:
            raw_clips_settled_job.apply_async((job_id, camera_role), countdown=settle)
        return job_id

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


def _dispatch_processing(store: JobStore, job_id: str, role: str | None = None) -> bool:
    """Enqueue the pipeline exactly once for a job — or, on a mixed job, for one ROLE.

    The guard is what makes the multi-clip ``s3_key`` path safe: a late clip re-arming
    the settle check, two settle checks overlapping, or a duplicate notification must
    never produce a second render of the same footage.

    ``role`` is set only for a multi-ref (mixed) job, where each media product renders
    from its own camera and gets its own exactly-once guard in ``Job.role_ingest``. The
    two are deliberately independent: the paid handcam edit must ship as soon as ITS
    clips are quiet, rather than waiting on a speculative camera-flyer card that may
    arrive an hour later or never.
    """
    job = store.load(job_id)
    if role is None:
        if job.processing_dispatched:
            logger.info("job %s already dispatched — not enqueuing a second render", job_id)
            return False
        store.update(job_id, processing_dispatched=True)
        if job.package.uses_scene_pipeline:
            process_selfie_package.delay(job_id)
        else:
            process_job.delay(job_id)
        return True

    state = job.role_ingest.get(role) or RoleIngest()
    if state.dispatched:
        logger.info(
            "job %s role %s already dispatched — not enqueuing a second render",
            job_id, role,
        )
        return False
    store.update(
        job_id,
        role_ingest={
            **job.role_ingest,
            role: state.model_copy(update={"dispatched": True}),
        },
    )
    process_media_ref_job.delay(job_id, role)
    return True


@celery_app.task(name="api.raw_clips_settled_job")
def raw_clips_settled_job(job_id: str, role: str | None = None) -> str:
    """Dispatch processing once a job's raw clips have stopped arriving.

    Armed (with a countdown) by every ``s3_key`` ingest. If another clip landed since,
    this re-schedules itself instead of dispatching — so the pipeline always sees the
    WHOLE jump. Re-scheduling (rather than cancelling the previous countdown, which
    Celery can't do reliably) is the same trick :func:`ultimum_watchdog_job` uses.

    ``role`` is set only for a multi-ref (mixed) job: each camera's product settles on its
    own stamp and dispatches its own render, so one card's silence never holds up the
    other's edit. Everything else about the check is identical.

    A no-op once that footage has been dispatched, once the job has moved past ``queued``,
    or if it was failed/rejected in the meantime.
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

    if role is None:
        if job.processing_dispatched or job.status != JobStatus.queued:
            return job_id  # already underway, or no longer waiting to be processed
    else:
        state = job.role_ingest.get(role)
        if state is not None and state.dispatched:
            return job_id  # this role's render is already underway
        if job.status in (JobStatus.failed, JobStatus.rejected):
            return job_id
        # NOTE: a mixed job is deliberately NOT required to be `queued` here. Its second
        # camera routinely lands after the first render finished, by which time the job
        # reads `ready`/`delivered` — the whole point of the flow is that the paid edit
        # ships first and the spec one joins the same gallery later.

    # A missing stamp must read as "not settled", never as "quiet forever": the
    # stamp can be absent when this check races the ingest's own update (two
    # writers on job.json — observed on a lagging machine, where it dispatched a
    # 1-clip render of an 8-clip jump). updated_at moves on every write, so it is
    # a safe lower bound for "something happened recently".
    if role is None:
        stamp = job.last_raw_clip_at or job.updated_at or time.time()
    else:
        role_state = job.role_ingest.get(role)
        stamp = (role_state.last_clip_at if role_state else None) or job.updated_at or time.time()
    quiet_for = time.time() - stamp
    if quiet_for < settings.raw_clip_settle_seconds:
        # Still arriving — check again shortly. Poll interval rather than the full
        # settle window so a jump that just went quiet isn't delayed by a whole window.
        raw_clips_settled_job.apply_async(
            (job_id, role), countdown=settings.raw_clip_settle_poll_seconds
        )
        return job_id

    if role is None and job.package.is_ultimum:
        from .selfie import CAMERA_ROLES

        if not store.camera_roles_present(job_id, CAMERA_ROLES):
            # Quiet, but the second camera hasn't arrived: not dispatchable. Its own
            # ingest will arm a fresh settle check when it lands; the ultimum
            # watchdog owns the never-arrives case. Dispatching here would render a
            # one-camera "Ultimate".
            return job_id

    raw = store.camera_raw_dir(job_id, role) if role else store.raw_dir(job_id)
    n_clips = len(list(raw.glob("*"))) if raw.exists() else 0
    logger.info(
        "job %s%s: raw clips settled (quiet %.0fs, %d file(s) staged) — dispatching",
        job_id, f" role {role}" if role else "", quiet_for, n_clips,
    )
    _dispatch_processing(store, job_id, role)
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
