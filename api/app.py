"""The REST API SkydiveOS calls to drive a jump through the auto-edit pipeline.

This is the front door (stage boundary 6–7: review + deliver). It is intentionally
*thin*: every endpoint validates the request, mutates the persisted
:class:`~api.jobs.Job` state, and enqueues the heavy work onto Celery
(:mod:`api.queue`) — it never segments, scores, composes, or renders inline.

Endpoints (all under the OpenAPI docs at ``/docs``):

==========================  ===============================================
``POST /jobs``              open a job, get a ``job_id``
``POST /jobs/{id}/upload``  attach a raw MP4 (or trigger an Open GoPro pull)
``GET  /jobs/{id}``         current status + metadata
``GET  /jobs/{id}/edl``     the job's persisted EDL (the review UI's timeline)
``POST /jobs/{id}/approve`` instructor approves → deliver
``POST /jobs/{id}/reject``  instructor rejects with a reason → re-queue
``POST /jobs/{id}/tweak``   instructor edits the EDL → re-render
``GET  /jobs/{id}/preview`` stream the rendered ``final.mp4`` (single-master)
``GET  /jobs/{id}/deliverables``        list a job's videos + photo set (URLs)
``GET  /jobs/{id}/deliverables/{name}`` stream one video deliverable
``GET  /jobs/{id}/photos``              list the job's selected stills
``GET  /jobs/{id}/photos/{filename}``   fetch one full-res photo
``GET  /jobs/{id}/music``               per-deliverable music selectors (+ uploaded)
``POST /jobs/{id}/music``               upload/replace a deliverable's backing track
``GET  /jobs/{id}/music/{deliverable}`` fetch an uploaded track
``DELETE /jobs/{id}/music/{deliverable}`` remove an uploaded track
``POST /jobs/{id}/unlock``  payment captured (SkydiveOS) → entitlement unlocked
``GET  /j/{code}``          the customer gallery landing page (token-authed)
``GET  /j/{code}/media/{name}``   stream a deliverable (preview while locked)
``GET  /j/{code}/photos/{filename}`` fetch one photo (unlocked only)
==========================  ===============================================

Run locally with ``uvicorn api.app:app --reload`` (and a Celery worker — see
:mod:`api.celery_app`).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from edl.schema import EditDecisionList
from ingest.registry import CameraRegistry

from . import archive
from .auth import AdminDep, PrincipalDep, service_token_allows
from .config import Settings, get_settings
from .gallery import render_gallery_html
from .jobs import MUSIC_SUFFIXES, REVIEWABLE, Entitlement, Job, JobStatus, JobStore
from .preview import preview_path
from .queue import CeleryJobQueue, JobQueue
from .schemas import (
    AssignCameraRequest,
    CameraInfo,
    CamerasResponse,
    CreateJobRequest,
    CreateJobResponse,
    DeliverableInfo,
    DeliverablesResponse,
    JobResponse,
    JobsListResponse,
    MusicSlot,
    MusicSlotsResponse,
    MusicUploadResponse,
    PhotoInfo,
    PhotosResponse,
    RejectRequest,
    TweakRequest,
    UnlockRequest,
    UploadResponse,
)
from .upsell import link_tiles

if TYPE_CHECKING:
    from ingest.events import EventEmitter
    from ingest.scanner import CameraScanner

logger = logging.getLogger(__name__)

#: Human labels for the per-deliverable music selectors (drives the upload UI).
_MUSIC_LABELS = {
    "full_video": "Full Video Music",
    "highlights": "Highlights Music",
    "freefall": "Freefall Music",
    "external_freefall": "External Freefall Music",
    "chute_libre_selfie": "Chute Libre Selfie Music",
}

# Streamed to disk a megabyte at a time so a 30-min 4K master never lands in RAM.
_UPLOAD_CHUNK = 1024 * 1024

API_DESCRIPTION = """\
Automated editing pipeline for tandem skydiving footage. Open a **job** per jump,
attach the raw GoPro master (or pull it off the camera), and the pipeline segments,
scores, composes an EDL, and renders a 60–120 s customer edit for instructor review.

Heavy work runs asynchronously on Celery workers; these endpoints only enqueue it
and report status. Nothing is delivered to the customer until an instructor approves —
unless the deployment sets `AUTO_DELIVER=1`, in which case a finished render is
auto-approved and delivered straight to the customer (presigned S3 links, emailed).
"""

TAGS_METADATA = [
    {"name": "jobs", "description": "Create jobs, attach footage, and track status."},
    {
        "name": "review",
        "description": "The instructor review gate: approve, reject, tweak, preview.",
    },
    {
        "name": "cameras",
        "description": "The paired-camera registry that drives auto-discovery.",
    },
]


# --------------------------------------------------------------------------- #
# Dependencies (overridable in tests via app.dependency_overrides)
# --------------------------------------------------------------------------- #


def get_store(settings: Annotated[Settings, Depends(get_settings)]) -> JobStore:
    """The job store, rooted at the configured jobs root."""
    return JobStore(settings.jobs_root)


def get_queue() -> JobQueue:
    """The async job queue (Celery in production; a fake in tests)."""
    return CeleryJobQueue()


def get_registry(settings: Annotated[Settings, Depends(get_settings)]) -> CameraRegistry:
    """The paired-camera registry (Mongo-backed; disabled when ``MONGO_URL`` unset)."""
    return CameraRegistry(settings.mongo_url, db_name=settings.mongo_db)


StoreDep = Annotated[JobStore, Depends(get_store)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated[CameraRegistry, Depends(get_registry)]
JobId = Annotated[str, PathParam(description="Job identifier returned by POST /jobs")]


def _load_or_404(store: JobStore, job_id: str) -> Job:
    try:
        return store.load(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from None


def enforce_job_ownership(
    request: Request, store: StoreDep, principal: PrincipalDep
) -> None:
    """App-wide guard: a caller may only touch a job their account owns.

    Registered as an application dependency so it runs ahead of *every* request and
    automatically covers any route carrying a ``{job_id}`` path parameter — no
    per-endpoint wiring. Routes without a ``job_id`` (create, the jobs list, the
    camera registry, the customer gallery, docs) are a no-op. A non-owner gets a 404
    (not 403) so an instructor can't probe another instructor's job ids. With
    ``ENFORCE_INSTRUCTOR_AUTH`` off every caller is an admin, so this is a no-op and
    behaviour is unchanged.

    The customer gallery (``/j/{code}``) carries no ``job_id`` and no SkydiveOS
    identity — see :data:`api.auth.PUBLIC_PATH_PREFIX` for how the principal is
    resolved there; its unguessable short code is its only credential.
    """
    job_id = request.path_params.get("job_id")
    if job_id is None:
        return
    job = _load_or_404(store, job_id)
    if not principal.owns(job.instructor_id):
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")


def _is_mp4(file: UploadFile) -> bool:
    """Whether an uploaded file is a (renderable) MP4 master, by extension or MIME.

    An ``.lrv`` proxy shares the MP4 container and may arrive with a ``video/mp4`` MIME,
    so it's excluded explicitly here — a proxy is never a render/source master.
    """
    name = (file.filename or "").lower()
    if name.endswith(".lrv"):
        return False
    if name.endswith(".mp4"):
        return True
    return file.content_type in {"video/mp4", "application/mp4"}


def _is_lrv(file: UploadFile) -> bool:
    """Whether an uploaded file is a GoPro LRV proxy (by extension).

    Staged alongside its MP4 so the analysis stages can use it when
    ``USE_PROXY_ANALYSIS`` is enabled (see :mod:`analysis.proxy`); never used for
    rendering, photos, or as a job's ``source_path``.
    """
    return (file.filename or "").lower().endswith(".lrv")


def _is_safe_segment(name: str) -> bool:
    """True if ``name`` is a single, traversal-free path segment (no ``/`` / ``..``).

    Guards the deliverable-name and photo-filename path parameters so a request can
    never reach outside the job's own directory.
    """
    return bool(name) and name == Path(name).name and ".." not in name


def _served_under(path: Path, root: Path) -> bool:
    """True if ``path`` resolves to a file genuinely inside ``root`` (defence in depth)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_audio(file: UploadFile) -> bool:
    """Whether an uploaded file is an accepted audio track (by extension or MIME)."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix in MUSIC_SUFFIXES:
        return True
    return (file.content_type or "").startswith("audio/")


def _music_slots(store: JobStore, job: Job) -> MusicSlotsResponse:
    """The per-deliverable music selectors for a job's package + any uploaded tracks."""
    slots: list[MusicSlot] = []
    for deliverable in job.package.music_deliverables:
        track = store.music_file(job.job_id, deliverable)
        slots.append(
            MusicSlot(
                deliverable=deliverable,
                label=_MUSIC_LABELS.get(deliverable, deliverable),
                filename=track.name if track else None,
                url=f"/jobs/{job.job_id}/music/{deliverable}" if track else None,
            )
        )
    return MusicSlotsResponse(job_id=job.job_id, package=job.package, slots=slots)


def _booking_sidecar(job: Job) -> dict[str, object]:
    """The booking metadata staged beside a job's footage for the pipeline to read back.

    One definition shared by both upload paths so the two never drift (the selfie
    renderer burns ``customer_name`` / ``jump_date`` onto the intro card from here, and
    :func:`api.selfie._ensure_default_music` persists its random pick back into it).
    """
    return {
        "booking_id": job.booking_id,
        "customer_name": job.customer_name,
        "customer_email": job.customer_email,
        "instructor_name": job.instructor_name,
        "jump_date": job.jump_date,
        "package": job.package.value,
        "music": job.music,
    }


async def _upload_ultimum(
    job: Job,
    store: JobStore,
    queue: JobQueue,
    settings: Settings,
    uploaded: list[UploadFile],
    camera_role: str | None,
) -> UploadResponse:
    """Stage one camera's clips for the Ultimate package; enqueue once both are in.

    Each upload names a ``camera_role`` and lands under ``raw/<role>/`` (two GoPros
    emit colliding filenames). The job is left waiting until both ``instructor`` and
    ``external`` clips are on disk, then the scene pipeline (which dispatches to
    :func:`api.selfie.run_ultimum_pipeline`) is enqueued exactly once.
    """
    from .selfie import CAMERA_ROLES

    if camera_role not in CAMERA_ROLES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"the ultimum package requires camera_role to be one of "
                f"{list(CAMERA_ROLES)} (got {camera_role!r})"
            ),
        )

    role_dir = store.camera_raw_dir(job.job_id, camera_role)
    role_dir.mkdir(parents=True, exist_ok=True)
    for f in uploaded:
        name = Path(f.filename or "clip.mp4").name
        with (role_dir / name).open("wb") as out:
            while chunk := await f.read(_UPLOAD_CHUNK):
                out.write(chunk)

    store.write_booking(job.job_id, _booking_sidecar(job))
    # File this camera's masters under the jump in the browsable archive right away, so
    # the footage is safe there even if the second camera never shows up (or the edit
    # later fails). Idempotent — the other camera's upload adds to the same folder.
    archive.archive_raw_footage(job, store, settings)

    n = len(uploaded)
    if store.camera_roles_present(job.job_id, CAMERA_ROLES):
        store.update(job.job_id, status=JobStatus.queued, error=None)
        queue.enqueue_selfie_processing(job.job_id)
        detail = f"received {n} files for {camera_role}; both cameras present, processing enqueued"
    else:
        missing = [r for r in CAMERA_ROLES if r != camera_role]
        # Only one camera so far. Arm the same watchdog the S3-ingest path arms, so a
        # second camera that never uploads fails the job with an actionable error rather
        # than stranding it in `queued` forever — an upload-created job must not be
        # weaker than a discovery-created one. Skipped under eager mode (countdown
        # scheduling is a no-op there, and tests drive both uploads themselves).
        if not settings.task_always_eager:
            queue.arm_ultimum_watchdog(
                job.job_id, settings.ultimum_second_camera_timeout_s
            )
        detail = (
            f"received {n} files for {camera_role}; "
            f"waiting for the other camera ({', '.join(missing)})"
        )

    return UploadResponse(
        job_id=job.job_id,
        status=store.load(job.job_id).status,
        source="upload",
        package=job.package,
        camera_role=camera_role,
        files_received=n,
        detail=detail,
    )


def _build_scanner(settings: Settings) -> CameraScanner:
    """The discovery scanner for the configured mode.

    ``static`` → a fixed list (no-hardware simulation); ``usb`` → mDNS detection of a
    USB-connected GoPro (the kiosk path); anything else → the real BLE scan.
    """
    from ingest.scanner import BleCameraScanner, StaticCameraScanner, UsbCameraScanner

    if settings.camera_scanner == "static":
        return StaticCameraScanner(list(settings.discovery_fake_cameras))
    if settings.camera_scanner == "usb":
        return UsbCameraScanner()
    return BleCameraScanner()


#: Bundled clip used by the static simulation when DISCOVERY_SAMPLE_MP4 is unset.
_DEFAULT_SAMPLE_MP4 = "sample-data/discovery_sample.mp4"


def _cleanup_kwargs(settings: Settings) -> dict[str, Any]:
    """Card-cleanup arguments for :func:`ingest.pull.pull_camera`.

    Unattended ingest has to free the SD card or it fills within about a week and the
    camera silently stops recording mid-day. Cleanup only ever removes footage S3 has
    already confirmed (:mod:`ingest.retention`) and is off unless
    ``DELETE_AFTER_TRANSFER`` is set, because it destroys the card's copy.
    """
    return {
        "cleanup": settings.delete_after_transfer,
        "cleanup_min_age_s": settings.delete_after_transfer_min_age_h * 3600.0,
        "cleanup_dry_run": settings.delete_after_transfer_dry_run,
    }


def _build_pull(settings: Settings) -> Callable[..., Awaitable[Any]] | None:
    """The pull coroutine for the configured mode.

    ``None`` means "use the service default" (the real wireless BLE+WiFi
    :func:`ingest.pull.pull_camera`). ``usb`` returns a pull that runs the real pull
    path against a :class:`~ingest.camera.WiredGoProCamera` (the kiosk path). ``static``
    returns a no-hardware simulation that stages the configured sample MP4
    (``DISCOVERY_SAMPLE_MP4``, or the bundled ``sample-data/discovery_sample.mp4``) and
    emits the same ``ready_for_processing`` event a real download would.
    """
    if settings.camera_scanner == "usb":
        async def _usb_pull(camera_id: str, *, emitter: EventEmitter | None = None) -> object:
            from ingest.camera import WiredGoProCamera
            from ingest.pull import pull_camera

            return await pull_camera(
                camera_id,
                camera=WiredGoProCamera(camera_id),
                emitter=emitter,
                **_cleanup_kwargs(settings),
            )

        return _usb_pull

    if settings.camera_scanner != "static":
        return None

    sample = settings.discovery_sample_mp4
    if not sample and Path(_DEFAULT_SAMPLE_MP4).is_file():
        sample = _DEFAULT_SAMPLE_MP4
    if not sample:
        raise RuntimeError(
            "CAMERA_SCANNER=static (simulation) needs a sample MP4 to stage: set "
            "DISCOVERY_SAMPLE_MP4, or add the bundled sample-data/discovery_sample.mp4."
        )

    async def _simulated_pull(camera_id: str, *, emitter: EventEmitter | None = None) -> object:
        from ingest.camera import LocalSampleCamera
        from ingest.pull import pull_camera

        # A distinct filename per camera so two simulated cameras don't collide; each
        # reports its current clip count (read fresh per pull) like a real card. Bumping
        # the count between scans simulates a new jump landing on the same camera — the
        # running discovery loop then picks up only the new clips on its next sweep.
        cam = LocalSampleCamera(
            sample,
            filename=f"GX0100{camera_id[-2:].zfill(2)}.MP4",
            count=_simulated_clip_count(settings, camera_id),
        )
        return await pull_camera(
            camera_id, camera=cam, emitter=emitter, **_cleanup_kwargs(settings)
        )

    return _simulated_pull


#: Per-camera override file for the simulated clip count (``scripts/sim_add_clip.py``
#: writes it). Lives outside ``<camera_id>/`` so clearing staged footage leaves it.
SIM_CLIPS_DIR = ".sim_clips"


def _simulated_clip_count(settings: Settings, camera_id: str) -> int:
    """How many clips a simulated camera reports, resolved fresh on every pull.

    Defaults to ``DISCOVERY_SAMPLE_COUNT``; a per-camera marker file
    (``<raw-storage>/.sim_clips/<camera_id>``) overrides it when present. Because this
    is read each pull, bumping the marker (see ``scripts/sim_add_clip.py``) makes a
    live discovery loop detect the new clips on its next scan — no restart needed.
    """
    from ingest.storage import storage_root

    base = settings.discovery_sample_count
    try:
        marker = storage_root() / SIM_CLIPS_DIR / camera_id
        if marker.is_file():
            return max(1, int(marker.read_text().strip()))
    except (ValueError, OSError):
        pass
    return base


def _log_paywall_readiness(settings: Settings) -> None:
    """Report at boot whether Path B (the paywall) is deliverable on this deployment.

    A locked job can only be handed to a customer as the served ``/j/{code}`` gallery,
    so ``PUBLIC_BASE_URL`` is a hard prerequisite for the "film it anyway" product.
    ``POST /jobs`` already refuses to *create* a ``preview_only`` job without it, so a
    fresh deployment can't get into trouble — but a box that had it and lost it (an
    env edit, a bad restart) could be holding locked jobs it can no longer deliver.
    That case is an ERROR with the count, deliberately not a crash: refusing to boot
    would take every *other* customer's gallery offline too.
    """
    if settings.public_base_url:
        logger.info(
            "customer galleries served at %s/j/{code}; the paywall (Path B) is available",
            settings.public_base_url,
        )
        return

    logger.warning(
        "PUBLIC_BASE_URL is unset: customer links fall back to the legacy presigned S3 "
        "gallery, and preview_only (Path B) jobs are REFUSED at creation — the paywalled "
        "gallery has nowhere to be served from."
    )
    try:
        stranded = [
            j.job_id
            for j in JobStore(settings.jobs_root).list_jobs()
            if j.entitlement is Entitlement.preview_only
        ]
    except Exception:  # noqa: BLE001 - a readiness log must never break startup
        return
    if stranded:
        logger.error(
            "%d preview_only job(s) exist but PUBLIC_BASE_URL is unset — these CANNOT be "
            "delivered (delivery refuses the legacy gallery, which would presign the clean "
            "master). Set PUBLIC_BASE_URL and re-queue delivery. First few: %s",
            len(stranded),
            stranded[:5],
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the camera auto-discovery service for the lifetime of the API process.

    Started only when ``ENABLE_AUTO_DISCOVERY`` is set (off by default, so tests and
    the existing flow are untouched). The scanner and pull are chosen by
    ``CAMERA_SCANNER``: ``ble`` (real hardware) or ``static`` (a no-hardware
    simulation that stages ``DISCOVERY_SAMPLE_MP4`` and, for convenience, seeds its
    ``DISCOVERY_FAKE_CAMERAS`` into the registry so they pass the paired-camera
    filter). Under an ASGI server, SIGTERM triggers the server's shutdown, which runs
    the ``finally`` below and awaits ``stop()`` — the graceful-shutdown path — so the
    service does not install its own signal handlers. Imports are lazy so the heavy
    BLE/Mongo/pipeline stack is pulled in only when discovery is actually enabled.
    """
    settings = get_settings()
    service = None
    if settings.enable_auto_discovery:
        try:
            from ingest.discovery import (
                CameraDiscoveryService,
                matcher_role_resolver,
                s3_notify_uploader,
            )

            if not settings.skydiveos_api_base:
                raise RuntimeError(
                    "auto-discovery needs SKYDIVEOS_API_BASE set: pulled files are "
                    "uploaded to S3 and {base}/api/media/raw-upload is notified with the key."
                )
            if not settings.s3_bucket:
                raise RuntimeError(
                    "auto-discovery needs S3_BUCKET set: pulled files are uploaded to S3, "
                    "then SkydiveOS is notified with the object key."
                )
            registry = CameraRegistry(settings.mongo_url, db_name=settings.mongo_db)
            # When the shared DB is reachable, the pulled clip's role is resolved from
            # the load (which slot its owner filled on THAT jump) — authoritative over
            # the registry's static role, so a staff member who is an instructor on one
            # jump and a cameraman on the next routes correctly either way. No DB → the
            # resolver stays None and the static registry role is used, as before.
            matcher = None
            if settings.mongo_url:
                from ingest.match import FootageMatcher

                matcher = FootageMatcher(
                    settings.mongo_url,
                    db_name=settings.mongo_db,
                    clock_tz=settings.camera_clock_tz,
                )
            if settings.camera_scanner == "static":
                for camera_id in settings.discovery_fake_cameras:
                    registry.upsert_paired(camera_id, name="simulated")
                logger.warning(
                    "camera auto-discovery in SIMULATION mode (CAMERA_SCANNER=static): "
                    "fake cameras %s, sample %s",
                    list(settings.discovery_fake_cameras),
                    settings.discovery_sample_mp4 or _DEFAULT_SAMPLE_MP4,
                )
            service = CameraDiscoveryService(
                scanner=_build_scanner(settings),
                registry=registry,
                upload=s3_notify_uploader(
                    settings.skydiveos_api_base,
                    bucket=settings.s3_bucket,
                    endpoint_url=settings.s3_endpoint_url,
                    region_name=settings.s3_region,
                    clock_tz=settings.camera_clock_tz,
                ),
                pull=_build_pull(settings),
                interval=settings.discovery_interval,
                role_resolver=(
                    matcher_role_resolver(matcher, clock_tz=settings.camera_clock_tz)
                    if matcher is not None
                    else None
                ),
            )
            await service.start()
            app.state.discovery = service
            app.state.footage_matcher = matcher
        except Exception:
            # A discovery misconfig must not take the whole API down — log and serve.
            logger.exception("camera auto-discovery failed to start; API running without it")
            if service is not None:
                try:
                    await service.stop()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                service = None
    else:
        logger.info("camera auto-discovery disabled (ENABLE_AUTO_DISCOVERY unset)")
    _log_paywall_readiness(settings)
    try:
        yield
    finally:
        if service is not None:
            await service.stop()
        matcher = getattr(app.state, "footage_matcher", None)
        if matcher is not None:
            try:
                matcher.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def create_app() -> FastAPI:
    """Build the FastAPI application (factory so tests get a fresh instance)."""
    # Under uvicorn only the uvicorn.* loggers get handlers, so our pipeline INFO
    # ("camera auto-discovery started", "Camera X discovered, pull enqueued", …)
    # would fall to Python's WARNING+ last-resort handler and never reach the
    # service logs. No-op when the host process already configured logging.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = FastAPI(
        title="SkydiveOS Auto-Edit API",
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        # Both run ahead of every request, in this order:
        #  1. require_service_token — may this caller talk to us at all? Everything
        #     except the customer gallery /j/{code} needs the shared secret (no-op
        #     until AUTO_EDIT_API_KEY is set). This is what keeps an internet-facing
        #     deployment from treating anonymous callers as admins, since the identity
        #     headers below are self-asserted.
        #  2. enforce_job_ownership — may they touch THIS job? Applies to any route
        #     with a {job_id} (no-op when ENFORCE_INSTRUCTOR_AUTH is off).
        dependencies=[Depends(enforce_job_ownership)],
    )

    @app.middleware("http")
    async def _service_token_gate(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        """Reject any request that doesn't hold the shared service token.

        A middleware, not a route dependency, because FastAPI serves ``/docs``,
        ``/redoc`` and ``/openapi.json`` as raw Starlette routes that skip app-level
        dependencies — publishing the entire API surface to an anonymous caller — and
        because a middleware also covers routes added later. Exemptions and the
        "off until AUTO_EDIT_API_KEY is set" rule live in
        :func:`api.auth.service_token_allows`.
        """
        if not service_token_allows(
            request.url.path,
            request.method,
            request.headers.get("authorization"),
            get_settings(),
        ):
            logger.warning("rejected an unauthenticated request to %s", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"detail": "a valid service token is required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",  # React dev server
            "http://localhost:5173",  # Vite dev server
            "https://dev.ultimatedzm.com",  # dev frontend
            "https://ultimatedzm.com",  # production frontend
            "https://www.ultimatedzm.com",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


    @app.post(
        "/jobs",
        status_code=201,
        response_model=CreateJobResponse,
        tags=["jobs"],
        summary="Create a job",
    )
    def create_job(
        body: CreateJobRequest, store: StoreDep, settings: SettingsDep
    ) -> CreateJobResponse:
        """Open a new job for one jump and return its ``job_id``.

        The footage is attached separately via ``POST /jobs/{id}/upload``; the job
        starts ``queued`` and carries the booking metadata supplied here.

        **A ``preview_only`` job is refused unless ``PUBLIC_BASE_URL`` is set.** This
        is the go-live gate for Path B: a locked job can only be delivered as the
        served ``/j/{code}`` gallery (:func:`api.delivery.deliver_to_customer` refuses
        the legacy S3 page, which would presign the clean masters). Rejecting it here
        — before any footage is uploaded or a single frame is rendered — means that
        delivery-time failure can never be reached in production, and the operator
        finds out at booking time instead of after the jump.
        """
        if (
            body.entitlement is Entitlement.preview_only
            and not settings.public_base_url
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "preview_only (Path B) needs PUBLIC_BASE_URL set: the paywalled "
                    "gallery is served live at {PUBLIC_BASE_URL}/j/{code}, and there is "
                    "no other way to deliver a locked job without handing out the clean "
                    "master. Set PUBLIC_BASE_URL, or create the job as edited_download."
                ),
            )
        job_id = uuid.uuid4().hex
        fields = body.model_dump(exclude_none=True)
        store.create(Job(job_id=job_id, **fields))
        # Every job carries its gallery short code from birth, so the customer link
        # exists (and stays stable) before anything renders or delivers.
        store.ensure_gallery_token(job_id)
        return CreateJobResponse(job_id=job_id, job=JobResponse.from_job(store.load(job_id)))

    @app.get(
        "/jobs",
        response_model=JobsListResponse,
        tags=["jobs"],
        summary="List jobs (an instructor's own, or all for an admin)",
    )
    def list_jobs(store: StoreDep, principal: PrincipalDep) -> JobsListResponse:
        """Every job the caller may see, newest first.

        An instructor sees only the jobs their account owns (those auto-stamped from
        the cameras assigned to them); an admin sees all. With access enforcement off,
        the caller is treated as an admin, so this returns every job.
        """
        instructor_id = None if principal.is_admin else principal.instructor_id
        jobs = store.list_jobs(instructor_id=instructor_id)
        return JobsListResponse(
            count=len(jobs), jobs=[JobResponse.from_job(j) for j in jobs]
        )

    @app.post(
        "/jobs/{job_id}/upload",
        response_model=UploadResponse,
        tags=["jobs"],
        summary="Attach footage (upload GoPro MP4s or trigger a camera pull)",
    )
    async def upload(
        job_id: JobId,
        store: StoreDep,
        queue: QueueDep,
        settings: SettingsDep,
        files: Annotated[
            list[UploadFile] | None,
            File(description="One or more raw GoPro MP4s for this jump"),
        ] = None,
        file: Annotated[
            UploadFile | None, File(description="Legacy single-file field (still accepted)")
        ] = None,
        camera_id: Annotated[
            str | None, Form(description="Open GoPro camera id to pull from")
        ] = None,
        camera_role: Annotated[
            str | None,
            Form(description="Camera source for the Ultimate package: instructor | external"),
        ] = None,
        s3_key: Annotated[
            str | None,
            Form(description="Key of a raw master already in S3 (auto-discovery path)"),
        ] = None,
    ) -> UploadResponse:
        """Attach the raw footage to a job, then enqueue the right pipeline.

        Provide **one** footage source: multipart ``files`` (the raw GoPro MP4s), a
        ``camera_id`` to pull the jump off an Open GoPro, or an ``s3_key`` naming a raw
        master already staged in S3 (the auto-discovery path — the worker downloads it
        instead of the web layer re-streaming multi-GB bytes). On a file upload the MP4s
        are staged under ``raw/`` and the package's pipeline is enqueued: the scene
        pipeline for the selfie / video-only / photo-only packages (which deliverables
        it emits depends on the package), the single-master edit otherwise. The
        ``s3_key`` path hands off to the identical dispatch once the download lands.

        The two-camera **Ultimate** package is the exception: each call must name a
        ``camera_role`` (``instructor`` or ``external``) and its clips are staged under
        ``raw/<role>/`` (two GoPros emit colliding filenames). Processing is enqueued
        only once *both* cameras have been uploaded; an earlier call just stages its
        camera and reports that it's waiting for the other.
        """
        job = _load_or_404(store, job_id)
        if job.status == JobStatus.processing:
            raise HTTPException(status_code=409, detail="job is already processing")

        # Accept both the multi-file ``files`` field and the legacy single ``file``.
        uploaded = list(files or [])
        if file is not None:
            uploaded.append(file)

        if uploaded:
            for f in uploaded:
                if not (_is_mp4(f) or _is_lrv(f)):
                    raise HTTPException(
                        status_code=422,
                        detail=f"unsupported file (expected .mp4 or .lrv): {f.filename!r}",
                    )
            # An LRV proxy is analysis-only; a job needs at least one MP4 master to
            # render and deliver. Reject an LRV-only upload with a clear message.
            if not any(_is_mp4(f) for f in uploaded):
                raise HTTPException(
                    status_code=422,
                    detail="at least one .mp4 is required (an .lrv proxy alone cannot be rendered)",
                )

            if job.package.is_ultimum:
                return await _upload_ultimum(
                    job, store, queue, settings, uploaded, camera_role
                )

            raw_dir = store.raw_dir(job_id)
            raw_dir.mkdir(parents=True, exist_ok=True)
            for f in uploaded:
                # Keep the original GoPro filename (e.g. GH010001.MP4); strip any path.
                name = Path(f.filename or "clip.mp4").name
                dest = raw_dir / name
                with dest.open("wb") as out:
                    while chunk := await f.read(_UPLOAD_CHUNK):
                        out.write(chunk)

            store.write_booking(job_id, _booking_sidecar(job))

            # The non-selfie pipelines still cut from a single ``source_path``; point
            # them at the first uploaded MP4 (never an LRV proxy) so they keep working
            # unchanged. Staged LRVs sit beside their MP4 for analysis to discover.
            first_mp4 = next(f for f in uploaded if _is_mp4(f))
            first_path = str(raw_dir / Path(first_mp4.filename or "clip.mp4").name)
            job = store.update(
                job_id, source_path=first_path, status=JobStatus.queued, error=None
            )

            # File the masters under the jump in the browsable archive before the edit
            # starts, so raw footage is preserved even if processing later fails.
            archive.archive_raw_footage(job, store, settings)

            if job.package.uses_scene_pipeline:
                queue.enqueue_selfie_processing(job_id)
            else:
                queue.enqueue_processing(job_id)

            n = len(uploaded)
            return UploadResponse(
                job_id=job_id,
                status=JobStatus.queued,
                source="upload",
                package=job.package,
                files_received=n,
                detail=f"received {n} files; processing enqueued",
            )

        camera = camera_id or job.camera_id
        if camera:
            store.update(job_id, camera_id=camera, status=JobStatus.queued, error=None)
            queue.enqueue_pull(job_id, camera)
            return UploadResponse(
                job_id=job_id, status=JobStatus.queued, source="pull",
                detail=f"Open GoPro pull from camera {camera} enqueued",
            )

        if s3_key:
            # Auto-discovery already staged the master in S3; source the job straight
            # from the key instead of streaming multi-GB bytes back through the web
            # layer. The worker downloads it and hands off to the same pipeline dispatch.
            if not s3_key.lower().endswith(".mp4"):
                raise HTTPException(
                    status_code=422,
                    detail=f"s3_key must point to an .mp4 master (got {s3_key!r})",
                )
            if job.package.is_ultimum:
                from .selfie import CAMERA_ROLES

                if camera_role not in CAMERA_ROLES:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"the ultimum package requires camera_role to be one of "
                            f"{list(CAMERA_ROLES)} for an S3 ingest (got {camera_role!r})"
                        ),
                    )
            store.write_booking(job_id, _booking_sidecar(job))
            store.update(job_id, status=JobStatus.queued, error=None)
            queue.enqueue_s3_ingest(job_id, s3_key, camera_role)
            detail = f"S3 ingest of {s3_key} enqueued"
            if job.package.is_ultimum:
                detail += f" for {camera_role}"
            return UploadResponse(
                job_id=job_id, status=JobStatus.queued, source="s3",
                package=job.package, camera_role=camera_role, detail=detail,
            )

        raise HTTPException(
            status_code=422, detail="provide at least one file, a camera_id, or an s3_key"
        )

    @app.get(
        "/jobs/{job_id}",
        response_model=JobResponse,
        tags=["jobs"],
        summary="Get job status",
    )
    def get_job(job_id: JobId, store: StoreDep) -> JobResponse:
        """Return a job's current status and metadata."""
        return JobResponse.from_job(_load_or_404(store, job_id))

    @app.get(
        "/jobs/{job_id}/edl",
        response_model=EditDecisionList,
        tags=["review"],
        summary="Get the job's current EDL",
    )
    def get_edl(job_id: JobId, store: StoreDep) -> EditDecisionList:
        """Return the job's persisted EDL — the edit the review UI renders.

        This is the read-side counterpart to ``POST /jobs/{id}/tweak``: the
        instructor screen loads the composed timeline here, edits it, and posts
        the result back. 404s until the Compose stage has written ``edl.json``
        (e.g. while the job is still ``queued``/``processing``).
        """
        _load_or_404(store, job_id)
        edl_file = store.edl_file(job_id)
        if not edl_file.exists():
            raise HTTPException(status_code=404, detail="no EDL yet; job not composed")
        return EditDecisionList.model_validate_json(edl_file.read_text())

    @app.post(
        "/jobs/{job_id}/approve",
        response_model=JobResponse,
        tags=["review"],
        summary="Approve a reviewed edit and deliver it",
    )
    def approve(job_id: JobId, store: StoreDep, queue: QueueDep) -> JobResponse:
        """Instructor approves the rendered edit; delivery to the customer is queued."""
        job = _load_or_404(store, job_id)
        if job.status != JobStatus.ready_for_review:
            raise HTTPException(
                status_code=409,
                detail=f"can only approve a job ready_for_review (is {job.status.value})",
            )
        updated = store.update(job_id, status=JobStatus.approved)
        queue.enqueue_delivery(job_id)
        return JobResponse.from_job(updated)

    @app.post(
        "/jobs/{job_id}/unlock",
        response_model=JobResponse,
        tags=["jobs"],
        summary="Mark the media purchased (SkydiveOS calls this after payment capture)",
    )
    def unlock(
        job_id: JobId, body: UnlockRequest, store: StoreDep, principal: AdminDep
    ) -> JobResponse:
        """Flip a ``preview_only`` job to ``edited_download`` — the paywall unlock.

        Called by SkydiveOS **server-to-server** once the $-unlock payment is captured
        (design doc Path B: "payment captured → watermark-free file unlocked").

        This endpoint gives away the product, so it is the one place where "trusted on
        the network boundary" isn't enough. Three checks stand between a request and a
        free video:

        * the app-wide **service token** (:func:`api.auth.require_service_token`) — no
          browser or stranger can reach it, only the SkydiveOS backend;
        * the **admin** role, so a plain instructor identity can't self-serve;
        * a non-empty ``payment_reference`` — the id of the captured payment in
          SkydiveOS. It is recorded on the job, so every unlock is auditable back to a
          real transaction rather than being an unattributable state flip.

        Idempotent — an already unlocked job returns 200 unchanged (with its original
        reference intact), so SkydiveOS may retry freely. Never touches ``status``: the
        clean deliverables were rendered up front, so the gallery serves them on its
        very next request — no re-render, no re-delivery.

        (``JobStore`` is single-writer by design; this one-field update's lost-update
        window against a running worker is microseconds, and a retry heals it.)
        """
        job = _load_or_404(store, job_id)
        if job.entitlement is Entitlement.edited_download:
            return JobResponse.from_job(job)  # already unlocked — idempotent
        updated = store.update(
            job_id,
            entitlement=Entitlement.edited_download,
            paid_at=time.time(),
            payment_reference=body.payment_reference,
        )
        logger.info(
            "job %s unlocked (preview_only -> edited_download) payment=%s by=%s",
            job_id,
            body.payment_reference,
            principal.instructor_id or "service",
        )
        return JobResponse.from_job(updated)

    @app.post(
        "/jobs/{job_id}/reject",
        response_model=JobResponse,
        tags=["review"],
        summary="Reject a reviewed edit and re-queue it",
    )
    def reject(job_id: JobId, body: RejectRequest, store: StoreDep, queue: QueueDep) -> JobResponse:
        """Instructor rejects the edit with a reason; the job is re-processed.

        The reason is recorded on the job (and logged as a training signal) before
        the pipeline is re-run to produce a fresh edit.
        """
        job = _load_or_404(store, job_id)
        if job.status != JobStatus.ready_for_review:
            raise HTTPException(
                status_code=409,
                detail=f"can only reject a job ready_for_review (is {job.status.value})",
            )
        store.log_adjustment(job_id, {"action": "reject", "reason": body.reason})
        updated = store.update(
            job_id, status=JobStatus.queued, reject_reason=body.reason, error=None
        )
        queue.enqueue_processing(job_id)
        return JobResponse.from_job(updated)

    @app.post(
        "/jobs/{job_id}/tweak",
        response_model=JobResponse,
        tags=["review"],
        summary="Adjust the EDL and re-render",
    )
    def tweak(job_id: JobId, body: TweakRequest, store: StoreDep, queue: QueueDep) -> JobResponse:
        """Instructor replaces the EDL with an adjusted edit; the job re-renders.

        The new EDL is validated, persisted (replacing ``edl.json``), and logged as
        a training signal, then a re-render is enqueued.
        """
        job = _load_or_404(store, job_id)
        if job.status not in REVIEWABLE:
            raise HTTPException(
                status_code=409,
                detail=f"can only tweak a job that has been rendered (is {job.status.value})",
            )
        store.save_edl(job_id, body.edl)
        store.log_adjustment(
            job_id,
            {"action": "tweak", "note": body.note, "edl": body.edl.model_dump(mode="json")},
        )
        updated = store.update(job_id, status=JobStatus.queued, error=None)
        queue.enqueue_rerender(job_id)
        return JobResponse.from_job(updated)

    @app.get(
        "/jobs/{job_id}/preview",
        tags=["review"],
        summary="Stream the rendered preview",
        response_class=FileResponse,
        responses={200: {"content": {"video/mp4": {}}, "description": "The rendered edit"}},
    )
    def preview(job_id: JobId, store: StoreDep) -> FileResponse:
        """Stream the job's rendered ``final.mp4`` (supports HTTP range requests)."""
        job = _load_or_404(store, job_id)
        if job.status not in REVIEWABLE:
            raise HTTPException(
                status_code=409,
                detail=f"no preview yet; job is {job.status.value}",
            )
        final: Path = store.final_path(job_id)
        if not final.exists():
            raise HTTPException(status_code=404, detail="rendered preview not found")
        return FileResponse(final, media_type="video/mp4", filename=f"{job_id}.mp4")

    # ----------------------------------------------------------------------- #
    # Deliverables: fetch the multi-output renders (full_video / highlights /
    # freefall cuts) and the photo set, for a frontend to play / download.
    # ----------------------------------------------------------------------- #

    @app.get(
        "/jobs/{job_id}/deliverables",
        response_model=DeliverablesResponse,
        tags=["review"],
        summary="List a job's downloadable deliverables (videos + photos)",
    )
    def list_deliverables(job_id: JobId, store: StoreDep) -> DeliverablesResponse:
        """Every fetchable output of a finished job, each with a URL to stream/download.

        The scene-pipeline packages (selfie / video_only / photo_only / ultimum) emit
        several deliverables keyed in ``Job.outputs``; this turns that map into playable
        URLs — one per video, plus a ``photos`` entry pointing at the photo list. Empty
        until the job is ``ready``.
        """
        job = _load_or_404(store, job_id)
        items: list[DeliverableInfo] = []
        for name in (job.outputs or {}):
            if name == "photos":
                items.append(
                    DeliverableInfo(
                        name="photos", kind="photos",
                        url=f"/jobs/{job_id}/photos", media_type=None,
                    )
                )
            else:
                items.append(
                    DeliverableInfo(
                        name=name, kind="video",
                        url=f"/jobs/{job_id}/deliverables/{name}", media_type="video/mp4",
                    )
                )
        return DeliverablesResponse(job_id=job_id, status=job.status, deliverables=items)

    @app.get(
        "/jobs/{job_id}/deliverables/{name}",
        tags=["review"],
        summary="Stream one video deliverable",
        response_class=FileResponse,
        responses={200: {"content": {"video/mp4": {}}, "description": "The rendered video"}},
    )
    def get_deliverable(
        job_id: JobId,
        name: Annotated[str, PathParam(description="Deliverable key, e.g. full_video")],
        store: StoreDep,
    ) -> FileResponse:
        """Stream one of a job's rendered videos (range-enabled, so it seeks/plays inline).

        ``name`` must be a video deliverable the job actually produced (a key in
        ``Job.outputs`` other than ``photos``); the file is resolved inside the job's own
        directory, never from the stored path, so the parameter can't escape it.
        """
        job = _load_or_404(store, job_id)
        outputs = job.outputs or {}
        if name == "photos" or name not in outputs or not _is_safe_segment(name):
            raise HTTPException(status_code=404, detail=f"no video deliverable {name!r}")
        path = store.dir(job_id) / f"{name}.mp4"
        if not path.exists() or not _served_under(path, store.dir(job_id)):
            raise HTTPException(status_code=404, detail="deliverable file not found")
        return FileResponse(path, media_type="video/mp4", filename=f"{job_id}_{name}.mp4")

    @app.get(
        "/jobs/{job_id}/photos",
        response_model=PhotosResponse,
        tags=["review"],
        summary="List a job's selected photos",
    )
    def list_photos(job_id: JobId, store: StoreDep) -> PhotosResponse:
        """The job's chosen stills (from ``photos/index.json``), each with a fetch URL."""
        _load_or_404(store, job_id)
        index = store.dir(job_id) / "photos" / "index.json"
        if not index.exists():
            raise HTTPException(status_code=404, detail="no photos for this job")
        entries = json.loads(index.read_text())
        photos = [
            PhotoInfo(
                filename=e["filename"],
                url=f"/jobs/{job_id}/photos/{e['filename']}",
                scene=e.get("scene"), ts=e.get("ts"), score=e.get("score"),
            )
            for e in entries
        ]
        return PhotosResponse(job_id=job_id, count=len(photos), photos=photos)

    @app.get(
        "/jobs/{job_id}/photos/{filename}",
        tags=["review"],
        summary="Fetch one photo (full-res JPEG)",
        response_class=FileResponse,
        responses={200: {"content": {"image/jpeg": {}}, "description": "A still"}},
    )
    def get_photo(
        job_id: JobId,
        filename: Annotated[str, PathParam(description="Photo filename from the photo list")],
        store: StoreDep,
    ) -> FileResponse:
        """Serve one full-res JPEG from the job's photo set (traversal-guarded)."""
        _load_or_404(store, job_id)
        if not _is_safe_segment(filename):
            raise HTTPException(status_code=400, detail="invalid photo filename")
        photos_dir = store.dir(job_id) / "photos"
        path = photos_dir / filename
        if not path.exists() or not _served_under(path, photos_dir):
            raise HTTPException(status_code=404, detail="photo not found")
        return FileResponse(path, media_type="image/jpeg", filename=filename)

    # ----------------------------------------------------------------------- #
    # Customer gallery (the /j/{code} short link SkydiveOS SMS/emails out).
    # No {job_id} path param, so enforce_job_ownership is a deliberate no-op:
    # the unguessable short code is the page's only auth. Never log the code.
    # Served live, so the page flips locked -> unlocked the moment /unlock runs,
    # and the link never expires (media streams from the job dir per request).
    # ----------------------------------------------------------------------- #

    def _job_by_token(store: JobStore, token: str) -> Job:
        job = store.find_by_gallery_token(token)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown gallery link")
        return job

    def _gallery_videos(store: JobStore, job: Job) -> list[str]:
        """The video deliverable names this job's gallery can stream, in order."""
        names = [n for n in (job.outputs or {}) if n != "photos"]
        if not names and store.final_path(job.job_id).is_file():
            names = ["final"]
        return names

    def _gallery_photo_names(store: JobStore, job: Job) -> list[str]:
        index = store.dir(job.job_id) / "photos" / "index.json"
        if not index.exists():
            return []
        try:
            return [e["filename"] for e in json.loads(index.read_text())]
        except (ValueError, KeyError, TypeError):
            return []

    def _primary_download(
        store: JobStore, job: Job, token: str, video_names: list[str]
    ) -> tuple[str | None, str | None]:
        """The unlocked page's primary action: ``(url, note)`` for the main video.

        The design's hero action is one button on *the* video — the full edit when the
        package has one, else the first deliverable. The note carries the reassurance
        line ("1080p MP4 · 214 MB · yours to keep"); the size is dropped rather than
        guessed if the file can't be stat'd.
        """
        if not video_names:
            return None, None
        name = "full_video" if "full_video" in video_names else video_names[0]
        bits = ["1080p MP4"]
        try:
            size = (store.dir(job.job_id) / f"{name}.mp4").stat().st_size
        except OSError:
            size = 0
        if size:
            bits.append(f"{size / 1_000_000:.0f} MB")
        bits.append("yours to keep")
        return f"/j/{token}/media/{name}", "  ·  ".join(bits)

    @app.get("/j/{token}", response_class=HTMLResponse, include_in_schema=False)
    def public_gallery(
        token: str, store: StoreDep, settings: SettingsDep
    ) -> HTMLResponse:
        """The customer landing page — Path A unlocked, Path B watermarked + paywalled.

        Accepts (and ignores) an ``s`` query param: an opaque source tag on the
        outbound links (``?s=e`` email, ``?s=m`` SMS) — analytics for SkydiveOS,
        never auth. Lock state is computed per request, never from the URL.
        """
        job = _job_by_token(store, token)
        locked = job.entitlement is Entitlement.preview_only
        video_names = _gallery_videos(store, job)
        photo_names = _gallery_photo_names(store, job)
        if not video_names and not photo_names:
            brand = html_escape(settings.delivery_brand_name)
            return HTMLResponse(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                f"<title>{brand}</title></head>"
                "<body style='background:#0d0d0d;color:#f2f2f2;font-family:sans-serif;"
                "text-align:center;padding:60px 20px'>"
                f"<h1>{brand}</h1><p>Your jump video is still being edited — "
                "check back in a few minutes.</p></body></html>"
            )

        unlock_url = None
        if locked and settings.checkout_url_template:
            unlock_url = settings.checkout_url_template.format(
                job_id=job.job_id, booking_id=job.booking_id or "", item="unlock"
            )
        dl_url, dl_note = (None, None) if locked else _primary_download(
            store, job, token, video_names
        )
        html_page = render_gallery_html(
            brand=settings.delivery_brand_name,
            customer_name=job.customer_name,
            jump_date=job.jump_date,
            location=settings.delivery_location,
            videos=[(n, f"/j/{token}/media/{n}") for n in video_names],
            photos=[] if locked else [f"/j/{token}/photos/{n}" for n in photo_names],
            download_all_url=None,
            locked=locked,
            unlock_url=unlock_url,
            price_display=settings.preview_price_display,
            photo_count_teaser=len(photo_names),
            tabbed=True,
            show_downloads=not locked,
            instructor_name=job.instructor_name,
            product_label=job.package.display_label,
            primary_download_url=dl_url,
            primary_download_note=dl_note,
            # Entitlement-independent: the same row on the locked and unlocked page.
            upsells=link_tiles(
                settings.upsell_tiles,
                template=settings.checkout_url_template,
                job_id=job.job_id,
                booking_id=job.booking_id,
            ),
        )
        return HTMLResponse(html_page)

    @app.get("/j/{token}/media/{name}", include_in_schema=False, response_class=FileResponse)
    def public_media(token: str, name: str, store: StoreDep) -> FileResponse:
        """Stream one deliverable to the customer (range-enabled).

        The **entitlement**, never the ``name``, selects the file: a locked
        (``preview_only``) job serves only its watermarked ``preview_<name>.mp4`` —
        the clean master is unreachable by any request until ``/unlock``.
        """
        job = _job_by_token(store, token)
        if not _is_safe_segment(name) or name not in _gallery_videos(store, job):
            raise HTTPException(status_code=404, detail="no such video")
        job_dir = store.dir(job.job_id)
        if job.entitlement is Entitlement.preview_only:
            path = preview_path(job_dir, name)
        else:
            path = job_dir / f"{name}.mp4"
        if not path.exists() or not _served_under(path, job_dir):
            raise HTTPException(status_code=404, detail="video not found")
        return FileResponse(path, media_type="video/mp4", filename=f"{name}.mp4")

    @app.get("/j/{token}/photos/{filename}", include_in_schema=False, response_class=FileResponse)
    def public_photo(token: str, filename: str, store: StoreDep) -> FileResponse:
        """Serve one full-res still to the customer — unlocked jobs only."""
        job = _job_by_token(store, token)
        if job.entitlement is Entitlement.preview_only:
            raise HTTPException(status_code=404, detail="photos unlock with the full video")
        if not _is_safe_segment(filename):
            raise HTTPException(status_code=400, detail="invalid photo filename")
        photos_dir = store.dir(job.job_id) / "photos"
        path = photos_dir / filename
        if not path.exists() or not _served_under(path, photos_dir):
            raise HTTPException(status_code=404, detail="photo not found")
        return FileResponse(path, media_type="image/jpeg", filename=filename)

    # ----------------------------------------------------------------------- #
    # Per-deliverable music: upload a backing track per video deliverable BEFORE
    # processing. Stored under jobs/<id>/music/<deliverable>.<ext>; the renderer
    # prefers it over the templates/music library, else falls back to the template.
    # ----------------------------------------------------------------------- #

    @app.get(
        "/jobs/{job_id}/music",
        response_model=MusicSlotsResponse,
        tags=["jobs"],
        summary="List the music selectors for a job's package",
    )
    def list_music(job_id: JobId, store: StoreDep) -> MusicSlotsResponse:
        """Which deliverables take music for this package, and any uploaded tracks.

        Drives the upload UI: ``photo_only`` returns no slots; the video packages return
        one slot per deliverable (full/highlights/freefall, or the four Ultimate cuts),
        each showing the uploaded filename + fetch URL when present.
        """
        return _music_slots(store, _load_or_404(store, job_id))

    @app.post(
        "/jobs/{job_id}/music",
        response_model=MusicUploadResponse,
        tags=["jobs"],
        summary="Upload (or replace) a deliverable's backing track",
    )
    async def upload_music(
        job_id: JobId,
        store: StoreDep,
        deliverable: Annotated[str, Form(description="Deliverable key, e.g. full_video")],
        file: Annotated[UploadFile, File(description="Audio track for this deliverable")],
    ) -> MusicUploadResponse:
        """Store a per-deliverable track under ``jobs/<id>/music/<deliverable>.<ext>``.

        Must be done before processing starts. Replaces any existing track for the same
        deliverable. The deliverable must be valid for the job's package. A job that
        never gets a track for a deliverable falls back to the template ``music``.
        """
        job = _load_or_404(store, job_id)
        if job.status == JobStatus.processing:
            raise HTTPException(
                status_code=409, detail="job is already processing; upload music earlier"
            )
        if deliverable not in job.package.music_deliverables:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{deliverable!r} is not a music deliverable for the "
                    f"{job.package.value} package (expected one of "
                    f"{list(job.package.music_deliverables)})"
                ),
            )
        if not _is_audio(file):
            raise HTTPException(status_code=422, detail=f"not an audio file: {file.filename!r}")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in MUSIC_SUFFIXES:
            raise HTTPException(status_code=422, detail=f"unsupported audio type: {suffix!r}")

        mdir = store.music_dir(job_id)
        mdir.mkdir(parents=True, exist_ok=True)
        for existing in mdir.glob(f"{deliverable}.*"):  # replace any prior track
            existing.unlink()
        dest = mdir / f"{deliverable}{suffix}"
        with dest.open("wb") as out:
            while chunk := await file.read(_UPLOAD_CHUNK):
                out.write(chunk)

        return MusicUploadResponse(
            job_id=job_id, deliverable=deliverable, filename=dest.name,
            detail=f"stored {deliverable} music ({dest.name})",
        )

    @app.get(
        "/jobs/{job_id}/music/{deliverable}",
        tags=["jobs"],
        summary="Fetch a deliverable's uploaded track",
        response_class=FileResponse,
        responses={200: {"content": {"audio/mpeg": {}}, "description": "The track"}},
    )
    def get_music(
        job_id: JobId,
        deliverable: Annotated[str, PathParam(description="Deliverable key")],
        store: StoreDep,
    ) -> FileResponse:
        """Stream a job's uploaded backing track for one deliverable."""
        _load_or_404(store, job_id)
        if not _is_safe_segment(deliverable):
            raise HTTPException(status_code=400, detail="invalid deliverable")
        track = store.music_file(job_id, deliverable)
        if track is None or not _served_under(track, store.music_dir(job_id)):
            raise HTTPException(status_code=404, detail="no music uploaded for this deliverable")
        return FileResponse(track, filename=track.name)

    @app.delete(
        "/jobs/{job_id}/music/{deliverable}",
        response_model=MusicSlotsResponse,
        tags=["jobs"],
        summary="Remove a deliverable's uploaded track",
    )
    def delete_music(
        job_id: JobId,
        deliverable: Annotated[str, PathParam(description="Deliverable key")],
        store: StoreDep,
    ) -> MusicSlotsResponse:
        """Delete a job's uploaded track for a deliverable (it reverts to the template)."""
        job = _load_or_404(store, job_id)
        if job.status == JobStatus.processing:
            raise HTTPException(status_code=409, detail="job is already processing")
        if not _is_safe_segment(deliverable):
            raise HTTPException(status_code=400, detail="invalid deliverable")
        removed = False
        for existing in store.music_dir(job_id).glob(f"{deliverable}.*"):
            existing.unlink()
            removed = True
        if not removed:
            raise HTTPException(status_code=404, detail="no music uploaded for this deliverable")
        return _music_slots(store, job)

    # ----------------------------------------------------------------------- #
    # Camera registry: the paired-camera allow-list that auto-discovery reads.
    # Cameras are added by the `--pair` flow (ingest); these endpoints let the
    # web layer list them and deactivate one so discovery stops auto-pulling it.
    # ----------------------------------------------------------------------- #

    def _cameras_response(registry: CameraRegistry, principal: PrincipalDep) -> CamerasResponse:
        """The camera list scoped to the caller (admin → all; instructor → own)."""
        instructor_id = None if principal.is_admin else principal.instructor_id
        cameras = [
            CameraInfo(
                camera_id=c.camera_id, name=c.name, paired_at=c.paired_at,
                active=c.active, instructor_id=c.instructor_id,
            )
            for c in registry.list_cameras(instructor_id=instructor_id)
        ]
        return CamerasResponse(cameras=cameras)

    @app.get(
        "/cameras",
        response_model=CamerasResponse,
        tags=["cameras"],
        summary="List paired cameras (an instructor's own, or all for an admin)",
    )
    def list_cameras(registry: RegistryDep, principal: PrincipalDep) -> CamerasResponse:
        """Cameras in the discovery registry (newest pairing first).

        An instructor sees only cameras assigned to them; an admin sees all. Empty when
        no cameras are paired or the registry is disabled (``MONGO_URL`` unset).
        ``active: false`` means discovery will not auto-pull it.
        """
        return _cameras_response(registry, principal)

    @app.delete(
        "/cameras/{camera_id}",
        response_model=CamerasResponse,
        tags=["cameras"],
        summary="Deactivate a camera (admin only)",
    )
    def remove_camera(
        camera_id: Annotated[str, PathParam(description="Camera id (trailing serial digits)")],
        registry: RegistryDep,
        principal: AdminDep,
    ) -> CamerasResponse:
        """Soft-delete a camera: discovery stops auto-pulling it; its pairing is kept.

        Admin only. Re-running ``--pair`` re-activates it. 404s if the registry is
        disabled or the camera is unknown. Returns the updated camera list.
        """
        if not registry.deactivate(camera_id):
            raise HTTPException(status_code=404, detail=f"camera not found: {camera_id}")
        return _cameras_response(registry, principal)

    @app.post(
        "/cameras/{camera_id}/assign",
        response_model=CamerasResponse,
        tags=["cameras"],
        summary="Register and/or assign a camera to an instructor (admin only)",
    )
    def assign_camera(
        camera_id: Annotated[str, PathParam(description="Camera id (trailing serial digits)")],
        body: AssignCameraRequest,
        registry: RegistryDep,
        principal: AdminDep,
    ) -> CamerasResponse:
        """Set the instructor that owns a camera, registering it if unknown. Admin only.

        Registration + assignment in one step: a serial not yet in the registry is
        auto-created (active) with this instructor — no separate ``--pair`` needed. Jobs
        auto-pulled from the camera are stamped with the assigned instructor, so the
        footage lands in that account. Returns the updated camera list; 503 only if the
        registry is disabled (``MONGO_URL`` unset).
        """
        if not registry.assign_instructor(camera_id, body.instructor_id, role=body.role):
            raise HTTPException(status_code=503, detail="camera registry is disabled")
        return _cameras_response(registry, principal)

    return app


#: Module-level app for ``uvicorn api.app:app``.
app = create_app()
