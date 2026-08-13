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
import os
import secrets
import string
import threading
import time
from collections.abc import Callable, Iterable, Sequence
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
#: Lock file backing :meth:`JobStore.claim_email_send`. Created ``O_EXCL``, so the
#: filesystem — not ``job.json``, which is read-modify-write — arbitrates which worker
#: may email the customer when a delivery task runs twice.
EMAIL_CLAIM_FILENAME = ".email_claimed"


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


class JobKind(StrEnum):
    """What KIND of thing a job is — which is not the same question as its package.

    Almost every job is a ``jump``: one customer, one jump, its own footage. The other
    two exist for the **load master** feature (a camera flyer sent up on spec becomes an
    upsell engine for everyone on the load):

    * ``load_master`` — the flyer's card. It **owns the files** and has no customer: no
      email, no gallery link handed out. When its render lands it *fans out* instead of
      being delivered (:func:`api.tasks.fan_out_load_job`).
    * ``load_child`` — one customer on that load who bought no media. It owns **no files
      at all**; ``source_job_id`` points at the master whose renders its gallery streams.
      It has its own name, its own ``gallery_token``, its own unlock and its own email.

    The invariant that makes children safe: for any gallery request the **files** come
    from ``source_job_id``'s job, while the **lock state** comes from the requesting job.
    So unlocking one child flips only that child — every other child keeps streaming the
    watermarked preview of the very same file.

    A ``jump`` job may also carry ``source_job_id`` (a media buyer on a spec-flight load):
    that one does NOT redirect its media — it just earns a load-video upsell tile in the
    gallery it was already getting, so a customer never receives two links.
    """

    jump = "jump"
    load_master = "load_master"
    load_child = "load_child"


class LoadEvidence(StrEnum):
    """HOW a load master's load was resolved — which decides how much the fan-out
    must double-check before offering the render to that load's customers.

    * ``flight_window`` — a spec flight: the flyer holds no slot on any manifest, so
      the load was resolved from the capture *timestamp* alone
      (``ingest.match.resolve_load_for_staff``). Timestamp-only evidence is weak —
      ground footage shot between loads resolves to the nearest departed load — so the
      fan-out additionally requires a ``freefall`` scene in the master's footage.
    """

    flight_window = "flight_window"


class LoadRosterEntry(BaseModel):
    """One jumper on a load master's manifest — the fan-out work list.

    Persisted on the master at creation (from ``ingest.match.LoadMatchResult``) so the
    fan-out worker never needs a database connection, and so the roster a load fanned out
    to is auditable after the fact. Deliberately the *minimum* PII the fan-out needs:
    who to name the gallery for, where to email it, and which tier they fall into.
    """

    model_config = ConfigDict(extra="forbid")

    #: Index into the load's ``jumpers`` array — the identity the fan-out dedupes on.
    jumper_index: int
    customer_name: str | None = None
    customer_email: str | None = None
    booking_id: str | None = None
    #: ``True`` when this jumper PURCHASED media, so they already receive a gallery of
    #: their own: they get a load-video tile in it, never a second page and never a
    #: second email. ``False`` → they get their own locked child gallery.
    bought_media: bool = False


#: The camera roles a job's footage can arrive under. Mirrors
#: :data:`api.selfie.CAMERA_ROLES`, duplicated here because this module must not import
#: the pipeline (``api.selfie`` imports *this*).
CAMERA_ROLE_INSTRUCTOR = "instructor"
CAMERA_ROLE_EXTERNAL = "external"
MEDIA_REF_ROLES = (CAMERA_ROLE_INSTRUCTOR, CAMERA_ROLE_EXTERNAL)


class MediaRef(BaseModel):
    """One media product on this jump: which camera films it, and who owns the result.

    Almost every job has exactly one, and then this carries nothing the job's own
    ``package``/``entitlement`` didn't already say — which is why an empty list means
    "single product, behave exactly as before".

    Two refs is the **mixed** jump: the customer bought the instructor's handcam edit
    and the desk *also* manifested a speculative camera-flyer edit. Both land on ONE job
    so the customer gets ONE gallery link, and the products differ in the only two ways
    that matter downstream — which raw folder feeds them (``raw/<role>/``) and whether
    the result is clean or watermarked.

    The **primary** ref (:meth:`Job.primary_ref`) owns the plain deliverable names and
    mirrors the job's own ``package``/``entitlement``; every other ref's deliverables are
    namespaced ``<role>_<name>`` so two renders can coexist in one ``outputs`` map.
    """

    model_config = ConfigDict(extra="forbid")

    #: One of :data:`MEDIA_REF_ROLES` — the camera whose clips feed this product.
    role: str
    package: Package
    entitlement: Entitlement

    @field_validator("role")
    @classmethod
    def _known_role(cls, v: str) -> str:
        role = v.strip().lower()
        if role not in MEDIA_REF_ROLES:
            raise ValueError(
                f"media ref role must be one of {MEDIA_REF_ROLES}, got {v!r}"
            )
        return role


def primary_ref_of(refs: Sequence[MediaRef]) -> MediaRef | None:
    """The ref that leads a job: owns the plain deliverable names, mirrors its state.

    **Order-independent, deliberately.** Which ref is primary decides deliverable NAMING,
    so it must not change because a caller sent the same two products in the other order —
    a re-created job would then rename its deliverables and the gallery would lose them.
    Pure, so both the wire validator (``api.schemas``) and the persisted record
    (:attr:`Job.primary_ref`) resolve it identically.

    Precedence: **paid over speculative** (the customer's own product leads their gallery),
    then **instructor over external** (the handcam is the product a tandem customer
    recognises, and it is the one that exists on every jump). That second tie-break only
    matters once two refs share a lock state — two paid products, or two speculative ones.
    """
    if not refs:
        return None
    return min(
        refs,
        key=lambda r: (
            r.entitlement is not Entitlement.edited_download,
            r.role != CAMERA_ROLE_INSTRUCTOR,
        ),
    )


class RoleIngest(BaseModel):
    """Per-role ingest state for a multi-ref job — the settle window and dispatch guard.

    A single-ref job tracks these once on the job (``last_raw_clip_at`` +
    ``processing_dispatched``), because one settle window and one render answer for the
    whole jump. A mixed job cannot share them: the two cameras arrive minutes or hours
    apart (the instructor lands and drops his card while the cameraman is still packing),
    and the paid edit must ship as soon as ITS clips are quiet rather than waiting on a
    speculative extra. So each role settles and dispatches on its own.
    """

    model_config = ConfigDict(extra="forbid")

    #: Epoch seconds of the most recent clip staged for this role (``None`` = none yet).
    last_clip_at: float | None = None
    #: Set once this role's render has been enqueued — the exactly-once guard, per role.
    dispatched: bool = False


class DeliverableAccess(BaseModel):
    """Lock state for ONE deliverable — the paywall, one file at a time.

    Exists because a single jump can carry media the customer **bought** alongside
    media filmed on **spec**: a paid handcam (``selfie``) edit plus a speculative
    camera-flyer (``external``) edit, both on one job so the customer gets ONE
    gallery link. ``Job.entitlement`` is a single scalar and cannot express that —
    ``edited_download`` would hand over the unpaid external edit clean, and
    ``preview_only`` would watermark a video the customer paid for.

    Presence in :attr:`Job.deliverable_access` is what makes a deliverable's state
    EXPLICIT; absence means "inherit ``Job.entitlement``", which is what every job
    written before this field did and still does. So an empty map is byte-identical
    to the old behaviour — see :func:`entitlement_for`.
    """

    model_config = ConfigDict(extra="forbid")

    entitlement: Entitlement
    #: True when this deliverable was BORN locked (its media ref was speculative).
    #: IMMUTABLE: :attr:`entitlement` moves when the customer pays, this does not. It
    #: is what tells an unlock which deliverables form the purchasable group, and what
    #: tells an operator browsing the archive that money was once owed on this file.
    born_locked: bool = False
    #: Epoch seconds of the captured per-deliverable unlock (``None`` = never paid).
    paid_at: float | None = None
    #: SkydiveOS's captured-transaction id for that unlock. Same audit rule as
    #: :attr:`Job.payment_reference`: an unlock must trace to real money.
    payment_reference: str | None = None


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

    #: Whether this is an ordinary jump, a spec-flight load master, or a child gallery
    #: hanging off one (see :class:`JobKind`). Defaulted — every ``job.json`` written
    #: before this field existed must keep validating on load.
    job_kind: JobKind = JobKind.jump
    #: The SkydiveOS ``loads._id`` this job belongs to. Set on a load master and on every
    #: job the fan-out touched; ``None`` for jobs created outside that flow.
    load_id: str | None = None
    #: Human name for the load (``"Load 14"``) — the master's archive folder and intro
    #: card, and the child galleries' hero line.
    load_label: str | None = None
    #: Index into the load's ``jumpers`` array. With ``load_id`` this is the identity the
    #: fan-out dedupes on, so a re-run can't open a second child for the same customer.
    jumper_index: int | None = None
    #: The load master whose rendered files back this job's load video.
    #:
    #: For a ``load_child`` this is the ONLY source of media it has. For a ``jump`` it is
    #: purely additive — the customer's own deliverables are untouched and the load video
    #: shows up as an upsell tile. Either way the pointed-at job supplies **files only**;
    #: the lock state always comes from *this* job's ``entitlement``.
    source_job_id: str | None = None
    #: The manifest roster a load master fans out to (empty for every other kind).
    #: Deliberately absent from ``JobResponse`` — it holds other customers' emails.
    load_roster: list[LoadRosterEntry] = Field(default_factory=list)
    #: How a load master's load was resolved (see :class:`LoadEvidence`). ``None``
    #: on every non-master job, and on masters created before the field existed —
    #: which the fan-out treats exactly like ``flight_window`` (the stricter gate).
    load_evidence: LoadEvidence | None = None
    #: Set on a retired ``load_child`` whose gallery token was ADOPTED by the
    #: customer's own later-arriving jump job (the gallery-race fix in
    #: ``api.app.create_job``): the link the customer already received now serves the
    #: adopting job, and this child must never be delivered or resolved again.
    superseded_by: str | None = None

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
    #: Per-deliverable lock state, keyed by deliverable name. EMPTY for every job that
    #: predates this field and for every job whose deliverables all share the job's
    #: default — which is the normal, single-media-ref case. A name absent here resolves
    #: to :attr:`entitlement` (see :func:`entitlement_for`), so an empty map behaves
    #: exactly as before. Populated only for a **mixed** job: one whose footage came
    #: from two media refs with different entitlements (paid handcam + spec external).
    deliverable_access: dict[str, DeliverableAccess] = Field(default_factory=dict)
    #: The media products on this jump, one per camera role. EMPTY or single-element for
    #: every ordinary job, where ``package``/``entitlement`` already say everything — and
    #: empty is what every job written before this field has, so it must behave exactly as
    #: before. Two entries make this a **mixed** job (see :class:`MediaRef`).
    media_refs: list[MediaRef] = Field(default_factory=list)
    #: Per-role settle + dispatch state, keyed by camera role. Used ONLY by a multi-ref
    #: job; a single-ref job keeps using ``last_raw_clip_at`` / ``processing_dispatched``.
    role_ingest: dict[str, RoleIngest] = Field(default_factory=dict)

    #: The S3 key each staged raw clip was downloaded from (filename → key), recorded
    #: by ``ingest_s3_job``. This is the disk-retention authority: the pruner
    #: (``scripts/prune_jobs.py``) deletes a local master only after confirming
    #: exactly this key in S3 — no key derivation, no camera_id guessing.
    raw_s3_keys: dict[str, str] = Field(default_factory=dict)
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
    #: Why this job was held back from automatic delivery even though it rendered.
    #: Set by ``api.tasks._auto_deliver_block`` (today: no jump/freefall evidence in the
    #: scenes — an interview-only clip set still renders a valid-looking edit, see
    #: ``api.selfie._curated_freefall``'s stand-in). ``AUTO_DELIVER`` skips a job that
    #: carries one; the instructor can still approve it by hand, which is the deliberate
    #: human override for a mis-classified scene set. Cleared on every re-render.
    hold_reason: str | None = None
    #: Epoch seconds when the customer's delivery email actually went out. The
    #: idempotency record for delivery: Celery runs with ``task_acks_late=True``, so a
    #: worker killed after sending but before the ack re-runs the whole task — and the
    #: status guard cannot catch it (the job is still ``approved`` while the task runs).
    #: Paired with the atomic claim in :meth:`JobStore.claim_email_send` so two workers
    #: racing the same job cannot both send.
    email_sent_at: float | None = None

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

    @property
    def is_multi_ref(self) -> bool:
        """Whether this jump carries MORE THAN ONE media product (the mixed job).

        The one test the ingest and dispatch paths branch on. False for every job with an
        empty or single-element ``media_refs``, which keeps the single-product paths — and
        every job that predates the field — on exactly the code they ran before.
        """
        return len(self.media_refs) > 1

    @property
    def primary_ref(self) -> MediaRef | None:
        """The ref that owns the plain deliverable names and mirrors the job's own state.

        Chosen by :func:`primary_ref_of`, which is **order-independent**. ``None`` when
        no refs are recorded (every ordinary job).
        """
        return primary_ref_of(self.media_refs)

    def ref_for_role(self, role: str) -> MediaRef | None:
        """This role's media product, or ``None`` if the job has none for it."""
        return next((r for r in self.media_refs if r.role == role), None)

    @property
    def staged_by_camera_role(self) -> bool:
        """Whether raw clips stage under ``raw/<role>/`` rather than ``raw/``.

        True for the two-camera Ultimate product (two GoPros emit colliding filenames)
        and for any multi-ref job (each ref renders from its own camera's footage, so the
        clips must be kept apart for the same reason plus a second one).
        """
        return self.package.is_ultimum or self.is_multi_ref


#: Deliverable name used by the classic single-master pipeline, which records no
#: ``outputs`` map. Kept in one place because three consumers fall back to it
#: (:func:`locked_deliverables`, ``api.preview.render_job_previews`` and
#: ``api.delivery.collect_deliverables``).
FINAL_DELIVERABLE = "final"


def deliverable_name(job: Job, role: str, base: str) -> str:
    """The ``outputs`` key this role's ``base`` deliverable is recorded under.

    The primary ref keeps the plain names the rest of the system already knows
    (``full_video``, ``highlights``, ``freefall``) so a single-product job — and a mixed
    job's *paid* half — is indistinguishable from before. Every other ref is namespaced
    ``<role>_<base>``, which is what lets two renders share one ``outputs`` map and one
    gallery. The names line up with the labels the gallery already carries for the
    Ultimate product's per-camera cuts (``external_freefall``), so the customer reads
    "Freefall · outside camera" without new copy.
    """
    primary = job.primary_ref
    if primary is None or role == primary.role:
        return base
    return f"{role}_{base}"


def entitlement_for(job: Job, name: str) -> Entitlement:
    """This deliverable's lock state — the ONLY way to ask the question.

    Never decide a lock from the URL, the filename, the request or a client hint: a
    ``preview_only`` deliverable's clean master must be unreachable at *any* address
    until it is paid for. An explicit :class:`DeliverableAccess` entry wins; absent,
    the deliverable inherits the job's own :attr:`Job.entitlement`, which is what
    makes an empty map identical to the pre-mixed-job behaviour.
    """
    entry = job.deliverable_access.get(name)
    return entry.entitlement if entry is not None else job.entitlement


def deliverable_names(job: Job) -> list[str]:
    """Every VIDEO deliverable name on this job (``photos`` excluded).

    Falls back to :data:`FINAL_DELIVERABLE` for the classic pipeline, whose render
    writes ``final.mp4`` and no ``outputs`` map.
    """
    names = [n for n in (job.outputs or {}) if n != "photos"]
    return names or [FINAL_DELIVERABLE]


def locked_deliverables(job: Job) -> frozenset[str]:
    """Every video deliverable of this job that is behind the paywall."""
    return frozenset(
        n for n in deliverable_names(job) if entitlement_for(job, n) is Entitlement.preview_only
    )


def any_locked(job: Job) -> bool:
    """Whether ANY video deliverable is behind the paywall.

    The test for "this job's watermarked previews are load-bearing" — a preview must be
    rendered, must not be pruned, and the job may only be delivered as the served
    ``/j/{code}`` gallery. True for a wholly locked job AND for a mixed one.
    """
    return bool(locked_deliverables(job))


def all_locked(job: Job) -> bool:
    """Whether EVERY video deliverable is behind the paywall (a wholly Path-B job)."""
    return locked_deliverables(job) == frozenset(deliverable_names(job))


def role_for_deliverable(job: Job, name: str) -> str | None:
    """Which media ref produced this deliverable — the inverse of :func:`deliverable_name`.

    ``None`` when the job has no refs (an ordinary job, where the question is meaningless).
    Derived rather than stored precisely so it cannot drift: :func:`deliverable_name` is
    the single naming authority, and this reads the same convention back.
    """
    primary = job.primary_ref
    if primary is None:
        return None
    for ref in job.media_refs:
        if ref.role != primary.role and name.startswith(f"{ref.role}_"):
            return ref.role
    return primary.role


def unlockable_group(job: Job, *, role: str | None = None) -> frozenset[str]:
    """The deliverables a group unlock buys: those BORN locked and still locked.

    ``role`` narrows the set to ONE camera's deliverables, and it is what lets the two
    angles be **sold separately**. That matters most on a jump where nothing was bought:
    both the handcam's edit and the cameraman's are born locked, and a customer who wants
    only the outside angle must be able to buy only that — an unscoped group would take
    one payment and hand over both.

    ``born_locked`` and still locked, so re-running the same unlock is a no-op rather than
    a re-write: the customer's own paid edits (never born locked) can never be swept in,
    and an already-paid deliverable keeps its original payment reference.
    """
    return frozenset(
        name
        for name, entry in job.deliverable_access.items()
        if entry.born_locked
        and entry.entitlement is Entitlement.preview_only
        and (role is None or role_for_deliverable(job, name) == role)
    )


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

    def claim_email_send(self, job_id: str) -> bool:
        """Atomically claim the right to email this job's customer. True iff we won it.

        The one place in this store that needs real cross-process atomicity. Everything
        else here is single-writer by design (one worker per jump), but delivery is not:
        Celery runs ``task_acks_late=True``, so a worker killed after the SMTP send but
        before the ack re-runs ``deliver_job`` from the top — and the status guard cannot
        see it, because the job is still ``approved`` for the whole run. Two workers
        picking up a re-queued delivery hit the same window.

        ``job.json`` is read-modify-write, so a flag in it cannot arbitrate that race.
        An ``O_CREAT|O_EXCL`` create can: the filesystem grants it to exactly one caller.
        The marker is the lock; :attr:`Job.email_sent_at` is the durable record written
        after a successful send.

        Release it with :meth:`release_email_claim` when the send FAILS — otherwise a
        transient SMTP outage would permanently suppress the customer's email.
        """
        marker = self.dir(job_id) / EMAIL_CLAIM_FILENAME
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{self._clock()}\n")
        return True

    def release_email_claim(self, job_id: str) -> None:
        """Drop the email claim so a retry may send (used when the send failed)."""
        (self.dir(job_id) / EMAIL_CLAIM_FILENAME).unlink(missing_ok=True)

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

    def adopt_gallery_token(self, from_job_id: str, to_job_id: str) -> str:
        """Move ``from_job_id``'s gallery token onto ``to_job_id``, retiring the donor.

        The gallery-race fix (see ``api.app.create_job``): when a customer's own jump
        job arrives *after* a load-master fan-out already opened a ``load_child``
        gallery for them, the new job adopts the child's token — so the link the
        customer was already emailed keeps working and now shows their own footage —
        instead of minting a second link. The donor is marked ``superseded_by`` and
        loses its token (a token resolves to exactly ONE job; ``find_by_gallery_token``
        re-checks the token on the loaded job, so a stale index entry can never serve
        the retired child).

        Returns the token now owned by ``to_job_id``. If the donor has no token
        (should not happen — every job mints one at birth), one is minted fresh.
        """
        donor = self.load(from_job_id)
        token = donor.gallery_token or _new_gallery_token()
        self.update(from_job_id, gallery_token=None, superseded_by=to_job_id)
        self.update(to_job_id, gallery_token=token)
        self._index_token(token, to_job_id)
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

    def set_pipeline_outputs(
        self,
        job_id: str,
        outputs: dict[str, str],
        *,
        status: JobStatus | None = None,
        owns: Iterable[str] | None = None,
    ) -> Job:
        """Record a render pass's deliverables on the job.

        Every ``outputs`` write goes through here because a job can be rendered by MORE
        THAN ONE pass: a mixed job (paid handcam + spec external) renders each media
        ref's footage separately, and a wholesale ``outputs=`` replace would delete
        whatever the other pass produced — the deliverable would vanish from the gallery
        (which lists ``outputs`` keys) while its bytes and its
        :class:`DeliverableAccess` entry lingered.

        ``owns`` is the set of names this pass is responsible for:

        * ``None`` (the default) — **replace** the whole map, exactly as before. Correct
          for a job with a single media ref, where one pass produces everything.
        * a name set — **merge**: names in ``owns`` are authoritative (one that is
          absent from ``outputs`` is dropped, so a deliverable that stopped being
          produced doesn't linger as a broken gallery card), while every name outside
          ``owns`` is preserved untouched. This is what makes a second pass additive.
        """
        changes: dict[str, object] = {}
        if owns is None:
            changes["outputs"] = dict(outputs)
        else:
            owned = set(owns) | set(outputs)
            kept = {k: v for k, v in (self.load(job_id).outputs or {}).items() if k not in owned}
            changes["outputs"] = {**kept, **outputs}
        if status is not None:
            changes["status"] = status
        return self.update(job_id, **changes)

    def set_deliverable_access(
        self, job_id: str, entries: dict[str, DeliverableAccess]
    ) -> Job:
        """Merge per-deliverable lock state onto the job.

        Merged, never replaced, for the same reason as
        :meth:`set_pipeline_outputs`: the second ref's pass must not erase the first
        ref's lock state. An existing entry for a name is **overwritten**, so a re-render
        re-asserts what that ref's entitlement is — but note that a paid unlock is
        recorded on the entry, so a re-render of an UNLOCKED deliverable must pass the
        unlocked entitlement, not the ref's birth value (see
        ``api.selfie._seed_deliverable_access``).
        """
        current = self.load(job_id).deliverable_access
        return self.update(job_id, deliverable_access={**current, **entries})

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
