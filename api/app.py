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
==========================  ===============================================

Run locally with ``uvicorn api.app:app --reload`` (and a Celery worker — see
:mod:`api.celery_app`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from edl.schema import EditDecisionList
from ingest.registry import CameraRegistry

from .auth import AdminDep, PrincipalDep
from .config import Settings, get_settings
from .jobs import MUSIC_SUFFIXES, REVIEWABLE, Job, JobStatus, JobStore
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
    UploadResponse,
)

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
and report status. Nothing is delivered to the customer until an instructor approves.
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
    camera registry, docs) are a no-op. A non-owner gets a 404 (not 403) so an
    instructor can't probe another instructor's job ids. With ``ENFORCE_INSTRUCTOR_AUTH``
    off every caller is an admin, so this is a no-op and behaviour is unchanged.
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


async def _upload_ultimum(
    job: Job,
    store: JobStore,
    queue: JobQueue,
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

    store.write_booking(
        job.job_id,
        {
            "booking_id": job.booking_id,
            "customer_name": job.customer_name,
            "jump_date": job.jump_date,
            "package": job.package.value,
            "music": job.music,
        },
    )

    n = len(uploaded)
    if store.camera_roles_present(job.job_id, CAMERA_ROLES):
        store.update(job.job_id, status=JobStatus.queued, error=None)
        queue.enqueue_selfie_processing(job.job_id)
        detail = f"received {n} files for {camera_role}; both cameras present, processing enqueued"
    else:
        missing = [r for r in CAMERA_ROLES if r != camera_role]
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

            return await pull_camera(camera_id, camera=WiredGoProCamera(camera_id), emitter=emitter)

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
        return await pull_camera(camera_id, camera=cam, emitter=emitter)

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
            from ingest.discovery import CameraDiscoveryService, s3_notify_uploader

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
                ),
                pull=_build_pull(settings),
                interval=settings.discovery_interval,
            )
            await service.start()
            app.state.discovery = service
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
    try:
        yield
    finally:
        if service is not None:
            await service.stop()


def create_app() -> FastAPI:
    """Build the FastAPI application (factory so tests get a fresh instance)."""
    app = FastAPI(
        title="SkydiveOS Auto-Edit API",
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        # Runs ahead of every request; enforces per-instructor job ownership on any
        # route with a {job_id} (no-op when ENFORCE_INSTRUCTOR_AUTH is off).
        dependencies=[Depends(enforce_job_ownership)],
    )
    
    
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
    def create_job(body: CreateJobRequest, store: StoreDep) -> CreateJobResponse:
        """Open a new job for one jump and return its ``job_id``.

        The footage is attached separately via ``POST /jobs/{id}/upload``; the job
        starts ``queued`` and carries the booking metadata supplied here.
        """
        job_id = uuid.uuid4().hex
        fields = body.model_dump(exclude_none=True)
        job = store.create(Job(job_id=job_id, **fields))
        return CreateJobResponse(job_id=job_id, job=JobResponse.from_job(job))

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
    ) -> UploadResponse:
        """Attach the raw footage to a job, then enqueue the right pipeline.

        Provide **either** one or more multipart ``files`` (the raw GoPro MP4s) **or**
        a ``camera_id`` to pull the jump off an Open GoPro. On a file upload the MP4s
        are staged under ``raw/`` and the package's pipeline is enqueued: the scene
        pipeline for the selfie / video-only / photo-only packages (which deliverables
        it emits depends on the package), the single-master edit otherwise.

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
                return await _upload_ultimum(job, store, queue, uploaded, camera_role)

            raw_dir = store.raw_dir(job_id)
            raw_dir.mkdir(parents=True, exist_ok=True)
            for f in uploaded:
                # Keep the original GoPro filename (e.g. GH010001.MP4); strip any path.
                name = Path(f.filename or "clip.mp4").name
                dest = raw_dir / name
                with dest.open("wb") as out:
                    while chunk := await f.read(_UPLOAD_CHUNK):
                        out.write(chunk)

            store.write_booking(
                job_id,
                {
                    "booking_id": job.booking_id,
                    "customer_name": job.customer_name,
                    "jump_date": job.jump_date,
                    "package": job.package.value,
                    "music": job.music,
                },
            )

            # The non-selfie pipelines still cut from a single ``source_path``; point
            # them at the first uploaded MP4 (never an LRV proxy) so they keep working
            # unchanged. Staged LRVs sit beside their MP4 for analysis to discover.
            first_mp4 = next(f for f in uploaded if _is_mp4(f))
            first_path = str(raw_dir / Path(first_mp4.filename or "clip.mp4").name)
            store.update(
                job_id, source_path=first_path, status=JobStatus.queued, error=None
            )

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

        raise HTTPException(status_code=422, detail="provide at least one file or a camera_id")

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
