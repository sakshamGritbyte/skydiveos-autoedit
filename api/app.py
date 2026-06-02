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
``POST /jobs/{id}/approve`` instructor approves → deliver
``POST /jobs/{id}/reject``  instructor rejects with a reason → re-queue
``POST /jobs/{id}/tweak``   instructor edits the EDL → re-render
``GET  /jobs/{id}/preview`` stream the rendered ``final.mp4``
==========================  ===============================================

Run locally with ``uvicorn api.app:app --reload`` (and a Celery worker — see
:mod:`api.celery_app`).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from .config import Settings, get_settings
from .jobs import REVIEWABLE, Job, JobStatus, JobStore
from .queue import CeleryJobQueue, JobQueue
from .schemas import (
    CreateJobRequest,
    CreateJobResponse,
    JobResponse,
    RejectRequest,
    TweakRequest,
    UploadResponse,
)

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


StoreDep = Annotated[JobStore, Depends(get_store)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]
JobId = Annotated[str, PathParam(description="Job identifier returned by POST /jobs")]


def _load_or_404(store: JobStore, job_id: str) -> Job:
    try:
        return store.load(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from None


def create_app() -> FastAPI:
    """Build the FastAPI application (factory so tests get a fresh instance)."""
    app = FastAPI(
        title="SkydiveOS Auto-Edit API",
        version="1.0.0",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
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

    @app.post(
        "/jobs/{job_id}/upload",
        response_model=UploadResponse,
        tags=["jobs"],
        summary="Attach footage (upload an MP4 or trigger a camera pull)",
    )
    async def upload(
        job_id: JobId,
        store: StoreDep,
        queue: QueueDep,
        file: Annotated[UploadFile | None, File(description="Raw GoPro master MP4")] = None,
        camera_id: Annotated[
            str | None, Form(description="Open GoPro camera id to pull from")
        ] = None,
    ) -> UploadResponse:
        """Attach the source master to a job, then enqueue processing.

        Provide **either** a multipart ``file`` (the raw MP4) **or** a ``camera_id``
        to pull the jump off an Open GoPro. Exactly one is required.
        """
        job = _load_or_404(store, job_id)
        if job.status == JobStatus.processing:
            raise HTTPException(status_code=409, detail="job is already processing")

        if file is not None:
            dest = store.source_path(job_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                while chunk := await file.read(_UPLOAD_CHUNK):
                    out.write(chunk)
            store.update(job_id, source_path=str(dest), status=JobStatus.queued, error=None)
            queue.enqueue_processing(job_id)
            return UploadResponse(
                job_id=job_id, status=JobStatus.queued, source="upload",
                detail=f"received {dest.name}; processing enqueued",
            )

        camera = camera_id or job.camera_id
        if camera:
            store.update(job_id, camera_id=camera, status=JobStatus.queued, error=None)
            queue.enqueue_pull(job_id, camera)
            return UploadResponse(
                job_id=job_id, status=JobStatus.queued, source="pull",
                detail=f"Open GoPro pull from camera {camera} enqueued",
            )

        raise HTTPException(status_code=400, detail="provide either a file upload or a camera_id")

    @app.get(
        "/jobs/{job_id}",
        response_model=JobResponse,
        tags=["jobs"],
        summary="Get job status",
    )
    def get_job(job_id: JobId, store: StoreDep) -> JobResponse:
        """Return a job's current status and metadata."""
        return JobResponse.from_job(_load_or_404(store, job_id))

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

    return app


#: Module-level app for ``uvicorn api.app:app``.
app = create_app()
