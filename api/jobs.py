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
from edl.storage import edl_path, job_dir, jobs_root, persist_edl
from render.render import FINAL_FILENAME

JOB_FILENAME = "job.json"
#: Filename of the uploaded full-res master inside a job's directory.
SOURCE_FILENAME = "source.mp4"
#: Subdirectory that holds the raw GoPro MP4s uploaded for a multi-clip package.
RAW_DIRNAME = "raw"
#: Subdirectory holding optional per-deliverable backing tracks uploaded for a job
#: (``music/full_video.mp3`` …). Preferred over the global ``templates/music`` library.
MUSIC_DIRNAME = "music"
#: Audio extensions accepted for an uploaded per-deliverable track.
MUSIC_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"})
#: Booking sidecar written alongside the raw footage (the metadata the selfie
#: pipeline reads back: customer, date, package, music).
BOOKING_FILENAME = "booking.json"
#: Where instructor EDL edits are appended — a training signal for the v2 scoring
#: model (CLAUDE.md: "Every instructor adjustment is logged").
ADJUSTMENTS_FILENAME = "adjustments.jsonl"


class Package(StrEnum):
    """The product a jump was booked under (drives which pipeline runs).

    Five products run through the multi-clip scene pipeline
    (:func:`api.tasks.process_selfie_package`), differing only in which deliverables
    they emit:

    * ``selfie`` — the three videos (full / highlights / freefall) *and* the photos.
    * ``external`` — same as selfie (the three videos *and* the photos); the difference
      is operational (a camera-flyer shoots it), the pipeline is identical.
    * ``video_only`` — the three videos, no photos.
    * ``photo_only`` — only the photos (90–100 best moments), no videos.
    * ``ultimum`` — the two-camera "Ultimate" product: a combo full video + highlights
      drawing on *both* the instructor selfie cam and the external cameraman, plus a
      freefall cut from each camera alone (external-only, and the instructor-only
      "chute libre selfie"). Its raw clips upload into per-camera subfolders
      (``raw/instructor/`` and ``raw/external/``) because two GoPros emit colliding
      filenames; it runs through its own orchestrator
      (:func:`api.selfie.run_ultimum_pipeline`), reusing the selfie editing logic.

    Use :attr:`uses_scene_pipeline`, :attr:`makes_videos`, :attr:`makes_photos`, and
    :attr:`is_ultimum` rather than comparing the enum member directly, so adding a new
    product is a one-line change here. (The single-master edit pipeline still backs
    Open GoPro camera pulls via :func:`api.tasks.process_job`.)
    """

    selfie = "selfie"
    external = "external"
    video_only = "video_only"
    photo_only = "photo_only"
    ultimum = "ultimum"

    @property
    def uses_scene_pipeline(self) -> bool:
        """Whether this package is processed by the multi-clip scene pipeline."""
        return self in {
            Package.selfie, Package.external, Package.video_only,
            Package.photo_only, Package.ultimum,
        }

    @property
    def makes_videos(self) -> bool:
        """Whether the scene pipeline renders the three standard videos for this package.

        ``ultimum`` is excluded: it emits its own four-deliverable set through
        :func:`api.selfie.run_ultimum_pipeline`, not the standard three-video render.
        """
        return self in {Package.selfie, Package.external, Package.video_only}

    @property
    def makes_photos(self) -> bool:
        """Whether the scene pipeline extracts the photo set for this package."""
        return self in {Package.selfie, Package.external, Package.photo_only}

    @property
    def is_ultimum(self) -> bool:
        """Whether this is the two-camera Ultimate package (its own orchestrator)."""
        return self is Package.ultimum

    @property
    def music_deliverables(self) -> tuple[str, ...]:
        """The video deliverables that take a backing track, for this package.

        Drives the per-deliverable music selectors a client shows and validates which
        ``jobs/<id>/music/<deliverable>.<ext>`` uploads are accepted. ``photo_only``
        (and any non-video package) returns ``()`` — no music selection.
        """
        if self is Package.ultimum:
            return ("full_video", "highlights", "external_freefall", "chute_libre_selfie")
        if self.makes_videos:  # selfie / external / video_only
            return ("full_video", "highlights", "freefall")
        return ()


class JobStatus(StrEnum):
    """Lifecycle state of a job (the value returned by ``GET /jobs/{id}``)."""

    queued = "queued"               # created / re-queued, awaiting the worker
    processing = "processing"       # pipeline running (segment→score→compose→render)
    ready_for_review = "ready_for_review"  # final.mp4 rendered, awaiting instructor
    ready = "ready"                 # selfie outputs rendered (full/highlights/freefall + photos)
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
    package: Package = Package.selfie  # product booked; selects the pipeline
    booking_id: str | None = None  # SkydiveOS booking this jump belongs to
    #: Instructor (SkydiveOS account) that owns this job. Auto-stamped from the
    #: pulling camera's registry entry for auto-discovered jumps; drives access
    #: scoping (an instructor sees only their own jobs; admins see all).
    instructor_id: str | None = None

    # Annotations from the review gate.
    reject_reason: str | None = None
    error: str | None = None  # populated when status == failed

    # Rendered deliverables, set when status == ready (selfie package). Maps a
    # deliverable name (full_video / highlights / freefall / photos) to its path.
    outputs: dict[str, str] | None = None

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

    def raw_dir(self, job_id: str) -> Path:
        """Directory holding the raw GoPro MP4s uploaded for a multi-clip package."""
        return self.dir(job_id) / RAW_DIRNAME

    def camera_raw_dir(self, job_id: str, role: str) -> Path:
        """Per-camera raw subdirectory for the Ultimate package (``raw/<role>/``).

        The two-camera product keeps each camera's clips apart because two GoPros emit
        colliding filenames (``GH010001.MP4`` from each). ``role`` is one of
        :data:`api.selfie.CAMERA_ROLES` (``"instructor"`` / ``"external"``).
        """
        return self.raw_dir(job_id) / role

    def music_dir(self, job_id: str) -> Path:
        """Directory holding the job's optional per-deliverable backing tracks."""
        return self.dir(job_id) / MUSIC_DIRNAME

    def music_file(self, job_id: str, deliverable: str) -> Path | None:
        """The uploaded track for ``deliverable`` (any accepted suffix), or ``None``.

        Files are stored as ``music/<deliverable>.<ext>``; this finds the one whose
        stem matches the deliverable so the renderer can prefer it over the template
        library. Returns ``None`` when nothing was uploaded for that deliverable.
        """
        mdir = self.music_dir(job_id)
        if not mdir.is_dir():
            return None
        for p in sorted(mdir.iterdir()):
            if p.stem == deliverable and p.suffix.lower() in MUSIC_SUFFIXES:
                return p
        return None

    def camera_roles_present(self, job_id: str, roles: tuple[str, ...]) -> bool:
        """True once *every* role's subdir holds at least one MP4 (the enqueue gate).

        Ultimate processing needs both cameras on disk before it can run; an upload
        that fills only one role's folder leaves this False so the worker isn't kicked
        off against a half-uploaded job.
        """
        return all(
            any(
                p.suffix.lower() == ".mp4"
                for p in self.camera_raw_dir(job_id, role).glob("*")
            )
            if self.camera_raw_dir(job_id, role).exists()
            else False
            for role in roles
        )

    def booking_path(self, job_id: str) -> Path:
        """Path to the job's ``booking.json`` sidecar (written at upload time)."""
        return self.dir(job_id) / BOOKING_FILENAME

    def scene_labels_path(self, job_id: str) -> Path:
        """Path to the job's optional ``scene_labels.json`` manual scene overrides.

        Drop ``{"GH010001.MP4": "freefall", ...}`` here to force a clip's scene when
        the selfie pipeline's GPMF classification is missing or ambiguous.
        """
        return self.dir(job_id) / "scene_labels.json"

    def write_booking(self, job_id: str, booking: dict[str, object]) -> Path:
        """Persist the booking sidecar the selfie pipeline reads back."""
        path = self.booking_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(booking, indent=2) + "\n")
        return path

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

    def list_jobs(self, *, instructor_id: str | None = None) -> list[Job]:
        """All persisted jobs (newest first), optionally only one instructor's.

        Scans the jobs root for ``*/job.json``. ``instructor_id`` filters to jobs that
        instructor owns; ``None`` returns every job (the admin view). A directory
        without a readable ``job.json`` is skipped rather than failing the listing.
        """
        root = jobs_root(self._root)
        if not root.is_dir():
            return []
        jobs: list[Job] = []
        for job_file in root.glob(f"*/{JOB_FILENAME}"):
            try:
                job = Job.model_validate_json(job_file.read_text())
            except (OSError, ValueError):
                continue
            if instructor_id is None or job.instructor_id == instructor_id:
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

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
