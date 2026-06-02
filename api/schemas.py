"""Request/response models for the REST layer.

These are the *wire contract* SkydiveOS codes against — kept separate from the
internal :class:`~api.jobs.Job` record so the persisted shape can evolve without
breaking the API (and vice versa). FastAPI renders them into the OpenAPI schema at
``/docs``; the ``examples`` here become the "Try it out" defaults.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from edl.schema import EditDecisionList

from .jobs import Job, JobStatus


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
    reject_reason: str | None
    error: str | None
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
            reject_reason=job.reject_reason,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


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
    #: "upload" when a file was received, "pull" when an Open GoPro pull was triggered.
    source: str
    detail: str


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
