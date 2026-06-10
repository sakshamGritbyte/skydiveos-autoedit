"""Request/response models for the REST layer.

These are the *wire contract* SkydiveOS codes against — kept separate from the
internal :class:`~api.jobs.Job` record so the persisted shape can evolve without
breaking the API (and vice versa). FastAPI renders them into the OpenAPI schema at
``/docs``; the ``examples`` here become the "Try it out" defaults.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from edl.schema import EditDecisionList

from .jobs import Job, JobStatus, Package


class CreateJobRequest(BaseModel):
    """Body for ``POST /jobs`` — the booking details for one jump.

    All fields are optional so a job can be opened before its metadata is known
    (e.g. the instructor creates it, then uploads the footage). Anything omitted
    falls back to the :class:`~api.jobs.Job` defaults.
    """

    model_config = ConfigDict(extra="forbid")

    customer_name: str | None = Field(default=None, examples=["Jane Doe"])
    jump_date: str | None = Field(default=None, examples=["2026-06-02"])
    camera_id: str | None = Field(default=None, examples=["1234"])
    music: str | None = Field(default=None, examples=["sunrise"])
    target_duration: float | None = Field(default=None, gt=0.0, examples=[90.0])
    #: Product booked for this jump. Omitted → the :class:`Job` default ("selfie").
    package: Package | None = Field(default=None, examples=["selfie"])
    booking_id: str | None = Field(default=None, examples=["BK-1001"])


class JobResponse(BaseModel):
    """Public view of a job's state (``GET /jobs/{id}`` and most other returns)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    customer_name: str
    jump_date: str | None
    camera_id: str | None
    music: str | None
    target_duration: float
    package: Package
    booking_id: str | None
    #: Instructor (SkydiveOS account) that owns this job.
    instructor_id: str | None
    reject_reason: str | None
    error: str | None
    #: Rendered deliverables, present (non-null) only once status == ready.
    outputs: dict[str, str] | None
    created_at: float
    updated_at: float

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        """Project an internal :class:`Job` to its public response shape."""
        return cls(
            job_id=job.job_id,
            status=job.status,
            customer_name=job.customer_name,
            jump_date=job.jump_date,
            camera_id=job.camera_id,
            music=job.music,
            target_duration=job.target_duration,
            package=job.package,
            booking_id=job.booking_id,
            instructor_id=job.instructor_id,
            reject_reason=job.reject_reason,
            error=job.error,
            outputs=job.outputs,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class JobsListResponse(BaseModel):
    """Body for ``GET /jobs`` — the caller's jobs (an instructor's own, or all for admin)."""

    model_config = ConfigDict(extra="forbid")

    count: int
    jobs: list[JobResponse]


class CreateJobResponse(BaseModel):
    """Body for ``POST /jobs`` — just the new id (plus the full job for convenience)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    job: JobResponse


class UploadResponse(BaseModel):
    """Body for ``POST /jobs/{id}/upload`` — what was accepted and where it queued."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    #: "upload" when files were received, "pull" when an Open GoPro pull was triggered.
    source: str
    #: The job's package (only set on the file-upload path; ``None`` for a pull).
    package: Package | None = None
    #: Camera this upload was filed under (Ultimate package only; ``None`` otherwise).
    camera_role: str | None = None
    #: Number of raw files saved (only set on the file-upload path).
    files_received: int | None = None
    detail: str


class DeliverableInfo(BaseModel):
    """One downloadable output of a finished job (a video file, or the photo set)."""

    model_config = ConfigDict(extra="forbid")

    #: Deliverable key (e.g. ``full_video``, ``highlights``, ``photos``).
    name: str = Field(examples=["full_video"])
    #: ``"video"`` (stream the MP4) or ``"photos"`` (a browsable set of stills).
    kind: str = Field(examples=["video"])
    #: Relative URL to fetch it (an MP4 stream, or the photo-list endpoint).
    url: str = Field(examples=["/jobs/abc123/deliverables/full_video"])
    #: MIME type for a video deliverable (``None`` for the photo set).
    media_type: str | None = Field(default=None, examples=["video/mp4"])


class DeliverablesResponse(BaseModel):
    """Body for ``GET /jobs/{id}/deliverables`` — every fetchable output + its URL."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    deliverables: list[DeliverableInfo]


class PhotoInfo(BaseModel):
    """One still in a job's photo set, with the URL to fetch the full-res JPEG."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(examples=["freefall_42.jpg"])
    url: str = Field(examples=["/jobs/abc123/photos/freefall_42.jpg"])
    scene: str | None = None
    ts: float | None = None
    score: float | None = None


class PhotosResponse(BaseModel):
    """Body for ``GET /jobs/{id}/photos`` — the job's selected stills + their URLs."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    count: int
    photos: list[PhotoInfo]


class MusicSlot(BaseModel):
    """One per-deliverable music selector for a job (drives the upload UI)."""

    model_config = ConfigDict(extra="forbid")

    #: Deliverable key, e.g. ``full_video`` / ``external_freefall``.
    deliverable: str = Field(examples=["full_video"])
    #: Human label for the selector, e.g. "Full Video Music".
    label: str = Field(examples=["Full Video Music"])
    #: Filename of the uploaded track, or ``None`` if none uploaded yet (template used).
    filename: str | None = None
    #: URL to fetch the uploaded track (``None`` until one is uploaded).
    url: str | None = None


class MusicSlotsResponse(BaseModel):
    """Body for ``GET /jobs/{id}/music`` — the music selectors for the job's package."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    package: Package
    slots: list[MusicSlot]


class MusicUploadResponse(BaseModel):
    """Body for ``POST /jobs/{id}/music`` — the stored per-deliverable track."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    deliverable: str
    filename: str
    detail: str


class CameraInfo(BaseModel):
    """One paired camera in the auto-discovery registry (``GET /cameras``)."""

    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(examples=["1234"])
    name: str | None = Field(default=None, examples=["Tandem cam A"])
    #: When the camera was last paired (epoch seconds).
    paired_at: float
    #: Whether discovery is allowed to auto-pull it (``DELETE /cameras/{id}`` clears this).
    active: bool
    #: Instructor (SkydiveOS account) that owns the camera; auto-pulled jobs inherit it.
    instructor_id: str | None = Field(default=None, examples=["inst-42"])


class CamerasResponse(BaseModel):
    """Body for ``GET /cameras`` / ``DELETE /cameras/{id}`` — the registered cameras."""

    model_config = ConfigDict(extra="forbid")

    cameras: list[CameraInfo]


class AssignCameraRequest(BaseModel):
    """Body for ``POST /cameras/{id}/assign`` — set the camera's owning instructor."""

    model_config = ConfigDict(extra="forbid")

    #: Instructor account to own the camera (``null`` clears the assignment).
    instructor_id: str | None = Field(examples=["inst-42"])


class RejectRequest(BaseModel):
    """Body for ``POST /jobs/{id}/reject`` — the instructor's reason (logged)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, examples=["Customer's face is out of frame at the exit"])


class TweakRequest(BaseModel):
    """Body for ``POST /jobs/{id}/tweak`` — the instructor's adjusted EDL.

    The full replacement EDL (validated against :mod:`edl.schema`) plus an optional
    note explaining the change. Both the new EDL and the note are persisted and
    logged as a training signal before the re-render is enqueued.
    """

    model_config = ConfigDict(extra="forbid")

    edl: EditDecisionList
    note: str | None = Field(default=None, examples=["Trimmed the canopy beat, slowed the exit"])
