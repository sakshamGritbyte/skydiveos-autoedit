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
import secrets
import string
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    * ``photo_only`` — only the photos, no videos: the extractor aims for
      ``PHOTO_ONLY_TARGET`` (140) strong stills, vs ~50 for selfie/external.
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
    def display_label(self) -> str:
        """Customer-facing product name for the gallery's hero meta line.

        Every package here is a tandem media product (this module exists for tandem
        footage), so the label leads with the discipline the customer recognises from
        their booking and qualifies it with the angle they bought.
        """
        return {
            Package.selfie: "Tandem · Handcam",
            Package.external: "Tandem · Outside Camera",
            Package.video_only: "Tandem · Video",
            Package.photo_only: "Tandem · Photos",
            Package.ultimum: "Tandem · Ultimate",
        }[self]

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


class Entitlement(StrEnum):
    """What the customer bought — drives the gallery's lock state (design doc Path A/B).

    * ``edited_download`` — media purchased (pre-booked, or unlocked later at the
      paywall): the gallery streams the clean 1080p deliverables with downloads.
    * ``preview_only`` — speculative capture ("we filmed it anyway"): the gallery
      streams only the watermarked 720p previews behind an unlock CTA; the clean
      masters are rendered and stored but unreachable until ``POST /jobs/{id}/unlock``.

    Lowercase wire values by repo convention; the design doc's ``EDITED_DOWNLOAD`` /
    ``PREVIEW_ONLY`` spellings are accepted on input (see ``Job._coerce_entitlement``).
    """

    edited_download = "edited_download"
    preview_only = "preview_only"


#: Alphabet + length for the gallery short code (``/j/{code}``). 11 chars of base62
#: ≈ 65 bits — short enough for an SMS link, unguessable enough to be the page's
#: only auth.
_GALLERY_CODE_ALPHABET = string.ascii_letters + string.digits
_GALLERY_CODE_LENGTH = 11


#: In-process ``{jobs root: {token: job_id}}`` index behind the PUBLIC gallery lookup,
#: so an unknown code costs a dict lookup instead of a scan of every job on disk. A
#: hint only — the job it names is always loaded and its token re-checked.
_TOKEN_INDEX: dict[str, dict[str, str]] = {}
#: When each root's index was last rebuilt, and how long that stands. Short enough that
#: a token minted by another process (a second uvicorn worker) resolves well before a
#: customer could click their link; long enough that a flood of bad codes scans once.
_TOKEN_INDEX_BUILT: dict[str, float] = {}
_TOKEN_INDEX_TTL_S = 5.0
#: Serialises rebuilds so a burst of misses triggers one scan, not one per thread.
_TOKEN_INDEX_LOCK = threading.Lock()


def reset_token_index() -> None:
    """Drop the gallery-token index (tests, and anything that rewrites jobs wholesale)."""
    with _TOKEN_INDEX_LOCK:
        _TOKEN_INDEX.clear()
        _TOKEN_INDEX_BUILT.clear()


def _new_gallery_token() -> str:
    """A fresh URL-safe short code for the customer gallery link."""
    return "".join(secrets.choice(_GALLERY_CODE_ALPHABET) for _ in range(_GALLERY_CODE_LENGTH))


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
    #: Where the delivery email goes. ``None`` → the delivery step can't email; it
    #: still generates links and reports them to SkydiveOS (which knows the booking).
    customer_email: str | None = None
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
    #: The instructor's *display* name, as the dropzone writes it on the manifest
    #: ("Marc Tremblay"). Supplied by SkydiveOS on ``POST /jobs`` (it owns the staff
    #: records; we only ever store what it tells us). Used to name the jump's folder in
    #: the archive (:mod:`api.archive`), which falls back to ``instructor_id`` when this
    #: is absent — so an omitted name degrades the folder name, nothing else.
    instructor_name: str | None = None

    #: Path A vs Path B (design doc): whether the customer already owns the edit.
    #: SkydiveOS sends it on ``POST /jobs``; ``POST /jobs/{id}/unlock`` flips a
    #: ``preview_only`` job to ``edited_download`` after payment capture.
    entitlement: Entitlement = Entitlement.edited_download
    #: Short code backing the customer gallery link (``{PUBLIC_BASE_URL}/j/{code}``).
    #: The code is the page's only auth — never expose it in list endpoints or logs.
    #: Minted once by :meth:`JobStore.ensure_gallery_token` (NOT a default_factory:
    #: a legacy job.json missing the field must not mint a new code on every load).
    gallery_token: str | None = None
    #: Epoch seconds when the paywall unlock was captured (``None`` = never).
    paid_at: float | None = None
    #: SkydiveOS's payment/transaction id for that unlock (``None`` = never unlocked).
    #: Required on ``POST /jobs/{id}/unlock`` so giving away the product is always
    #: attributable to a real capture; kept for audit/reconciliation only.
    payment_reference: str | None = None
    #: Post-jump add-on purchases from the gallery's upsell row: item key
    #: (``raw`` / ``photos``) → the captured payment reference, same audit rule as
    #: ``payment_reference``. Fulfilment is entirely gallery-side — the same
    #: ``/j/{code}`` page grows the purchased section on its next request (raw
    #: footage players, or the photo grid for a still-locked job), so nothing is
    #: re-rendered, re-delivered or re-emailed. Set only by ``POST /jobs/{id}/unlock``
    #: with an ``item``; never touches ``entitlement`` or ``status``.
    addons: dict[str, str] = Field(default_factory=dict)

    #: Epoch seconds when the most recent raw clip landed via the ``s3_key`` ingest
    #: path. SkydiveOS notifies once PER CLIP, so a jump filmed as several files
    #: (GoPro chapters a 4 GB master; an instructor stops/starts recording) arrives as
    #: several ``POST /jobs/{id}/upload`` calls. ``raw_clips_settled_job`` waits for
    #: this stamp to go quiet before dispatching, so the pipeline sees the WHOLE jump.
    last_raw_clip_at: float | None = None
    #: Set once processing has been dispatched for this job, so two settle checks (or a
    #: settle check racing a late clip) can never enqueue a second render of the same
    #: job — concurrent renders share a job dir and, with AUTO_DELIVER on, whichever
    #: finishes first emails the customer a PARTIAL edit.
    processing_dispatched: bool = False

    # Annotations from the review gate.
    reject_reason: str | None = None
    error: str | None = None  # populated when status == failed

    # Rendered deliverables, set when status == ready (selfie package). Maps a
    # deliverable name (full_video / highlights / freefall / photos) to its path.
    outputs: dict[str, str] | None = None

    #: Presigned download links sent to the customer, set when status == delivered.
    #: Maps deliverable name → URL (expires after ``DELIVERY_LINK_TTL_DAYS``).
    delivery_links: dict[str, str] | None = None

    created_at: float = 0.0
    updated_at: float = 0.0

    @field_validator("entitlement", mode="before")
    @classmethod
    def _coerce_entitlement(cls, v: object) -> object:
        """Accept the design doc's uppercase spellings (``PREVIEW_ONLY`` …).

        Also trims whitespace: this value crosses a service boundary (SkydiveOS sends
        it on ``POST /jobs``), and a stray space or a different casing must not fall
        through to the ``edited_download`` default — that would silently unlock a
        speculative capture.
        """
        return v.strip().lower() if isinstance(v, str) else v


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

    def ensure_gallery_token(self, job_id: str) -> str:
        """Return the job's gallery short code, minting + persisting one on first use.

        Idempotent — an existing code is never regenerated (the customer's link must
        stay stable across replays/tweaks, like the persisted music pick).
        """
        job = self.load(job_id)
        if job.gallery_token:
            return job.gallery_token
        token = _new_gallery_token()
        self.update(job_id, gallery_token=token)
        # Resolvable immediately in this process; other workers pick it up on their
        # next index rebuild (well within the TTL of a customer receiving the link).
        self._index_token(token, job_id)
        return token

    def find_by_gallery_token(self, token: str) -> Job | None:
        """Resolve a gallery short code to its job, or ``None``.

        Backed by an in-process ``token → job_id`` index (:data:`_TOKEN_INDEX`), because
        this is the one **public, unauthenticated** lookup in the service: the customer
        gallery and its ``/state`` poll both land here. A plain directory scan meant
        every *miss* globbed and JSON-parsed every ``job.json`` on disk, so an
        unauthenticated caller could turn cheap requests into unbounded disk I/O.

        The index is rebuilt at most once per :data:`_TOKEN_INDEX_TTL_S`, so a flood of
        unknown codes costs one scan per window instead of one per request; a token
        minted in *this* process is added immediately (see
        :meth:`ensure_gallery_token`), and one minted by another process/worker becomes
        resolvable within the TTL — which is irrelevant to a customer, since a render
        takes minutes.

        The index is only ever a *hint*: the job it points at is loaded and its token
        re-checked, so a stale or poisoned entry yields ``None`` rather than the wrong
        jump. Empty/missing tokens never match (legacy jobs carry ``None``).
        """
        if not token:
            return None
        root = jobs_root(self._root)
        if not root.is_dir():
            return None
        key = str(root.resolve())

        job = self._resolve_indexed_token(key, token)
        if job is not None:
            return job
        # Miss against a fresh index means the code really is unknown — answer from
        # memory. Only a stale index earns a rescan.
        if self._token_index_is_fresh(key):
            return None
        self._rebuild_token_index(key, root)
        return self._resolve_indexed_token(key, token)

    def _resolve_indexed_token(self, key: str, token: str) -> Job | None:
        """Load the job the index maps ``token`` to, verifying the token still matches."""
        job_id = _TOKEN_INDEX.get(key, {}).get(token)
        if job_id is None:
            return None
        try:
            job = self.load(job_id)
        except (FileNotFoundError, ValueError):
            _TOKEN_INDEX.get(key, {}).pop(token, None)
            return None
        if job.gallery_token != token:  # the index went stale; never guess
            _TOKEN_INDEX.get(key, {}).pop(token, None)
            return None
        return job

    def _token_index_is_fresh(self, key: str) -> bool:
        built = _TOKEN_INDEX_BUILT.get(key)
        return built is not None and (self._clock() - built) < _TOKEN_INDEX_TTL_S

    def _rebuild_token_index(self, key: str, root: Path) -> None:
        """One directory scan → the whole token map. Serialised so a burst scans once."""
        with _TOKEN_INDEX_LOCK:
            if self._token_index_is_fresh(key):  # another thread just did it
                return
            index: dict[str, str] = {}
            for job_file in root.glob(f"*/{JOB_FILENAME}"):
                try:
                    job = Job.model_validate_json(job_file.read_text())
                except (OSError, ValueError):
                    continue
                if job.gallery_token:
                    index[job.gallery_token] = job.job_id
            _TOKEN_INDEX[key] = index
            _TOKEN_INDEX_BUILT[key] = self._clock()

    def _index_token(self, token: str, job_id: str) -> None:
        """Register a freshly minted token so its gallery resolves in this process at once."""
        key = str(jobs_root(self._root).resolve())
        _TOKEN_INDEX.setdefault(key, {})[token] = job_id

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
