"""Job state: the lifecycle record /api owns for each jump, and its persistence.

A *job* is one jump moving through the pipeline (CLAUDE.md: "one job per jump;
jobs are idempotent and resumable"). The heavy artifacts already live on disk next
to each other — ``edl.json`` (Compose), ``final.mp4`` (Render) — under
``<jobs_root>/{job_id}/`` via :mod:`edl.storage`. This module adds the small piece
that was missing: the *state* of the job (what status it's in, who the customer is,
where its source master is) so the REST layer and the Celery workers share one
source of truth.

State is persisted as ``<jobs_root>/{job_id}/job.json`` — same directory, same
file conventions (pydantic ``model_dump_json(indent=2)`` + trailing newline) as the
EDL. A file (not a DB) keeps a job fully self-contained and replayable, consistent
with the rest of the pipeline; swapping in Postgres later means re-implementing
:class:`JobStore` only.

The status machine the REST endpoints drive:

    queued ─▶ processing ─▶ ready_for_review ─▶ approved ─▶ delivered
                  ▲                  │
                  └──── rejected ◀───┘         (any stage ─▶ failed on error)

``tweak`` re-renders in place (ready_for_review ─▶ processing ─▶ ready_for_review).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from edl.schema import EditDecisionList
from edl.storage import edl_path, job_dir, persist_edl
from render.render import FINAL_FILENAME

JOB_FILENAME = "job.json"
#: Filename of the uploaded full-res master inside a job's directory.
SOURCE_FILENAME = "source.mp4"
#: Where instructor EDL edits are appended — a training signal for the v2 scoring
#: model (CLAUDE.md: "Every instructor adjustment is logged").
ADJUSTMENTS_FILENAME = "adjustments.jsonl"


class JobStatus(StrEnum):
    """Lifecycle state of a job (the value returned by ``GET /jobs/{id}``)."""

    queued = "queued"               # created / re-queued, awaiting the worker
    processing = "processing"       # pipeline running (segment→score→compose→render)
    ready_for_review = "ready_for_review"  # final.mp4 rendered, awaiting instructor
    approved = "approved"           # instructor approved; delivery enqueued
    delivered = "delivered"         # pushed to the customer
    rejected = "rejected"           # instructor rejected; about to re-queue
    failed = "failed"               # pipeline error (see ``error``); resumable


#: Statuses from which a fresh preview render is available to stream.
REVIEWABLE = {JobStatus.ready_for_review, JobStatus.approved, JobStatus.delivered}


class Job(BaseModel):
    """The persisted state record for one jump.

    The bulky inputs/outputs are referenced by path or by sibling file, not
    embedded: the source master lives at ``source_path`` and the edit/render live
    beside this record (``edl.json`` / ``final.mp4``).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus = JobStatus.queued

    # Inputs needed to (re-)run the pipeline and burn the intro card.
    customer_name: str = "Valued Skydiver"
    jump_date: str | None = None  # ISO date burned onto the intro (None → today at render)
    camera_id: str | None = None  # set when the source came from an Open GoPro pull
    source_path: str | None = None  # full-res master MP4 the render cuts from
    music: str | None = None
    target_duration: float = Field(default=90.0, gt=0.0)

    # Annotations from the review gate.
    reject_reason: str | None = None
    error: str | None = None  # populated when status == failed

    created_at: float = 0.0
    updated_at: float = 0.0


class JobStore:
    """File-backed CRUD for :class:`Job`, one ``job.json`` per job directory.

    Single-writer by design: each job is touched by at most one worker at a time
    (one job per jump), so we don't lock — a later move to a real DB would add
    transactional guarantees here without changing callers.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        #: ``None`` defers root resolution to :mod:`edl.storage` ($JOBS_ROOT/./jobs).
        self._root = root
        self._clock = clock

    @property
    def root(self) -> str | Path | None:
        """The configured jobs root (``None`` defers to :mod:`edl.storage`)."""
        return self._root

    def dir(self, job_id: str) -> Path:
        """The job's directory — where all its artifacts live, under one root."""
        return job_dir(job_id, self._root)

    def source_path(self, job_id: str) -> Path:
        """Where this job's uploaded full-res master is staged."""
        return self.dir(job_id) / SOURCE_FILENAME

    def final_path(self, job_id: str) -> Path:
        """The job's rendered preview/delivery file (may not exist yet)."""
        return self.dir(job_id) / FINAL_FILENAME

    def edl_file(self, job_id: str) -> Path:
        """Path to the job's persisted EDL (``edl.json``)."""
        return edl_path(job_id, self._root)

    def save_edl(self, job_id: str, edl: EditDecisionList) -> Path:
        """Persist (replace) the job's EDL under the same root as its state."""
        return persist_edl(edl, job_id, self._root)

    def _path(self, job_id: str) -> Path:
        return self.dir(job_id) / JOB_FILENAME

    def exists(self, job_id: str) -> bool:
        return self._path(job_id).exists()

    def create(self, job: Job) -> Job:
        """Persist a brand-new job, stamping created/updated. Fails if it exists."""
        if self.exists(job.job_id):
            raise FileExistsError(f"job already exists: {job.job_id}")
        now = self._clock()
        job = job.model_copy(update={"created_at": now, "updated_at": now})
        self._write(job)
        return job

    def load(self, job_id: str) -> Job:
        """Read a job's state. Raises :class:`FileNotFoundError` if unknown."""
        path = self._path(job_id)
        if not path.exists():
            raise FileNotFoundError(job_id)
        return Job.model_validate_json(path.read_text())

    def save(self, job: Job) -> Job:
        """Persist an updated job, refreshing ``updated_at``."""
        job = job.model_copy(update={"updated_at": self._clock()})
        self._write(job)
        return job

    def update(self, job_id: str, **changes: object) -> Job:
        """Load → apply ``changes`` → save, in one shot. Validates the result."""
        current = self.load(job_id)
        updated = current.model_copy(update=changes)
        # Re-validate so an illegal field/value is rejected before it's written.
        return self.save(Job.model_validate(updated.model_dump()))

    def log_adjustment(self, job_id: str, record: dict[str, object]) -> Path:
        """Append one instructor EDL adjustment to the job's training-signal log."""
        path = self.dir(job_id) / ADJUSTMENTS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"at": self._clock(), **record}) + "\n")
        return path

    def _write(self, job: Job) -> None:
        path = self._path(job.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(job.model_dump_json(indent=2) + "\n")
