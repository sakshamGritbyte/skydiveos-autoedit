"""Request/response models for the REST layer.

These are the *wire contract* SkydiveOS codes against — kept separate from the
internal :class:`~api.jobs.Job` record so the persisted shape can evolve without
breaking the API (and vice versa). FastAPI renders them into the OpenAPI schema at
``/docs``; the ``examples`` here become the "Try it out" defaults.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from edl.schema import EditDecisionList

from .jobs import (
    Entitlement,
    Job,
    JobKind,
    JobStatus,
    LoadEvidence,
    LoadRosterEntry,
    MediaRef,
    Package,
    deliverable_names,
    entitlement_for,
    primary_ref_of,
)
from .lifecycle import MediaState, media_state


class CreateJobRequest(BaseModel):
    """Body for ``POST /jobs`` — the booking details for one jump.

    All fields are optional so a job can be opened before its metadata is known
    (e.g. the instructor creates it, then uploads the footage). Anything omitted
    falls back to the :class:`~api.jobs.Job` defaults.
    """

    model_config = ConfigDict(extra="forbid")

    customer_name: str | None = Field(default=None, examples=["Jane Doe"])
    customer_email: str | None = Field(default=None, examples=["jane@example.com"])
    jump_date: str | None = Field(default=None, examples=["2026-06-02"])
    camera_id: str | None = Field(default=None, examples=["1234"])
    music: str | None = Field(default=None, examples=["sunrise"])
    target_duration: float | None = Field(default=None, gt=0.0, examples=[90.0])
    #: Product booked for this jump. Omitted → the :class:`Job` default ("selfie").
    package: Package | None = Field(default=None, examples=["selfie"])
    booking_id: str | None = Field(default=None, examples=["BK-1001"])
    #: The instructor's display name — names their folder in the jump archive
    #: (``raw-storage/{date}/{instructor}/{customer}/``). Omitted → ``instructor_id``.
    instructor_name: str | None = Field(default=None, examples=["Marc Tremblay"])
    #: Path A vs Path B: ``edited_download`` when the booking bought media,
    #: ``preview_only`` for a speculative capture (watermarked preview + paywall).
    #: Omitted → the :class:`Job` default (``edited_download``).
    entitlement: Entitlement | None = Field(default=None, examples=["preview_only"])
    #: The media products on this jump, one per camera role — send this ONLY for a jumper
    #: carrying more than one (the mixed case: a paid handcam edit plus a speculative
    #: camera-flyer one). Omitted, or a single entry, is the ordinary job and behaves
    #: exactly as before.
    #:
    #: Rules, all enforced here so a bad set is a 422 before any footage is staged:
    #: at most one ref per role; the **primary** ref (the paid one if any, else the
    #: instructor's) must agree with the top-level ``package``/``entitlement``, because
    #: those two fields stay the job's own and something must be authoritative.
    media_refs: list[MediaRef] | None = Field(default=None)

    # -- spec-flight load master / child gallery (see api.jobs.JobKind) -------- #
    #: ``jump`` (default), ``load_master`` (a spec flyer's card, owns the files) or
    #: ``load_child`` (a no-media customer's gallery pointing at a master).
    job_kind: JobKind | None = Field(default=None, examples=["load_master"])
    #: SkydiveOS ``loads._id`` this job belongs to.
    load_id: str | None = Field(default=None, examples=["66f1c0de0000000000000014"])
    #: Human load name, used for the master's archive folder and the child hero line.
    load_label: str | None = Field(default=None, examples=["Load 14"])
    #: Index into the load's ``jumpers`` array (the fan-out's dedupe identity).
    jumper_index: int | None = Field(default=None, ge=0, examples=[2])
    #: The load master supplying this job's load video. **Required** for a
    #: ``load_child`` (it owns no footage of its own) and refused if it names an
    #: unknown job.
    source_job_id: str | None = Field(default=None, examples=["9f21c7..."])
    #: The manifest roster a load master fans out to. Ignored for other kinds.
    load_roster: list[LoadRosterEntry] | None = Field(default=None)
    #: How a load master's load was resolved: ``flight_window`` (spec flight —
    #: timestamp evidence only, the fan-out keeps its freefall guard).
    #: Ignored for other kinds; omitted → treated as ``flight_window``.
    load_evidence: LoadEvidence | None = Field(default=None, examples=["flight_window"])

    @field_validator("entitlement", mode="before")
    @classmethod
    def _coerce_entitlement(cls, v: object) -> object:
        """Accept the design doc's uppercase spellings (``PREVIEW_ONLY`` …).

        Trimmed as well as lower-cased — a casing or whitespace mismatch on this
        cross-service field must fail loudly (422) or resolve correctly, never
        silently default to ``edited_download`` and give the edit away.
        """
        return v.strip().lower() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _coherent_media_refs(self) -> CreateJobRequest:
        """Refuse an incoherent ref set — at creation, before any footage is staged.

        Three ways a caller can hand us something unservable, each rejected rather than
        resolved by guesswork, because every one of them decides where a camera's footage
        goes and whether the result is watermarked:

        * **Two refs on one role** — one camera cannot feed two products, and the second
          would silently overwrite the first's dispatch and deliverable names.
        * **A primary ref that disagrees with the top-level fields** — ``package`` and
          ``entitlement`` remain the job's own (the whole system reads them), so a
          mismatch means two different answers to "what did the customer buy?".
        * **Every ref speculative on a paid job, or vice versa** — caught by the same
          check, since the primary ref is chosen as the paid one when any ref is paid.
        """
        refs = self.media_refs
        if not refs:
            return self
        roles = [r.role for r in refs]
        if len(set(roles)) != len(roles):
            raise ValueError(
                f"media_refs must carry at most one ref per camera role (got {roles}); "
                "one camera cannot feed two products"
            )
        primary = primary_ref_of(refs)
        assert primary is not None  # refs is non-empty here
        if self.package is not None and primary.package is not self.package:
            raise ValueError(
                f"media_refs' primary ref is {primary.package.value!r} but package is "
                f"{self.package.value!r} — the top-level package must mirror the primary "
                "ref (the paid one, else the instructor's)"
            )
        if self.entitlement is not None and primary.entitlement is not self.entitlement:
            raise ValueError(
                f"media_refs' primary ref is {primary.entitlement.value!r} but "
                f"entitlement is {self.entitlement.value!r} — the top-level entitlement "
                "must mirror the primary ref"
            )
        return self


class JobResponse(BaseModel):
    """Public view of a job's state (``GET /jobs/{id}`` and most other returns)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    #: The design doc's product-facing lifecycle state (Frame 02), **derived** from
    #: ``status`` + ``entitlement`` + ``paid_at`` — never stored, so it can't drift.
    #: Read-only: drive UI copy off this, drive the pipeline off ``status``.
    media_state: MediaState
    customer_name: str
    customer_email: str | None
    jump_date: str | None
    camera_id: str | None
    music: str | None
    target_duration: float
    package: Package
    booking_id: str | None
    #: Instructor (SkydiveOS account) that owns this job.
    instructor_id: str | None
    #: The instructor's display name (names their folder in the jump archive).
    instructor_name: str | None
    #: Ordinary jump, spec-flight load master, or a child gallery on one.
    job_kind: JobKind
    #: The load this job belongs to, and its human name (``None`` outside the fan-out).
    load_id: str | None
    load_label: str | None
    #: Index into the load's ``jumpers`` array.
    jumper_index: int | None
    #: The load master supplying this job's load video (files only — the lock state is
    #: always this job's own ``entitlement``). ``load_roster`` is deliberately NOT
    #: projected here: it carries other customers' names and emails.
    source_job_id: str | None
    #: How a load master's load was resolved (``None`` on every other kind).
    load_evidence: LoadEvidence | None
    #: Set on a retired ``load_child`` whose gallery token was adopted by the
    #: customer's own jump job (the gallery-race fix) — this child serves nothing.
    superseded_by: str | None
    #: Path A vs Path B lock state (``gallery_token`` itself is deliberately NOT
    #: exposed here — the secret travels only via the status callback / delivery link).
    entitlement: Entitlement
    #: Epoch seconds when the paywall unlock was captured (``None`` = never).
    paid_at: float | None
    #: Purchased gallery add-ons: item key → captured payment reference. SkydiveOS
    #: reads this to reconcile add-on sales; fulfilment is the gallery's own.
    addons: dict[str, str]
    #: The media products this job carries, one per camera role — empty for every
    #: single-product job. Echoed back so SkydiveOS can reconcile the products it
    #: manifested against the products we actually recorded.
    media_refs: list[MediaRef]
    #: **Resolved** lock state per video deliverable, and the ONLY correct answer for a
    #: mixed job: ``entitlement`` above is the job's default (``edited_download`` when the
    #: customer bought the handcam edit) and cannot say that the camera-flyer edit beside
    #: it is still behind the paywall. Empty until the job has rendered; sent fully
    #: resolved so no consumer reimplements the inherit-from-job rule.
    deliverable_entitlements: dict[str, Entitlement]
    reject_reason: str | None
    error: str | None
    #: Rendered deliverables, present (non-null) only once status == ready.
    outputs: dict[str, str] | None
    #: Presigned customer download links, present only once status == delivered.
    delivery_links: dict[str, str] | None
    created_at: float
    updated_at: float

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        """Project an internal :class:`Job` to its public response shape."""
        return cls(
            job_id=job.job_id,
            status=job.status,
            media_state=media_state(job),
            customer_name=job.customer_name,
            customer_email=job.customer_email,
            jump_date=job.jump_date,
            camera_id=job.camera_id,
            music=job.music,
            target_duration=job.target_duration,
            package=job.package,
            booking_id=job.booking_id,
            instructor_id=job.instructor_id,
            instructor_name=job.instructor_name,
            job_kind=job.job_kind,
            load_id=job.load_id,
            load_label=job.load_label,
            jumper_index=job.jumper_index,
            source_job_id=job.source_job_id,
            load_evidence=job.load_evidence,
            superseded_by=job.superseded_by,
            entitlement=job.entitlement,
            paid_at=job.paid_at,
            addons=job.addons,
            media_refs=job.media_refs,
            deliverable_entitlements=(
                {n: entitlement_for(job, n) for n in deliverable_names(job)}
                if job.deliverable_access
                else {}
            ),
            reject_reason=job.reject_reason,
            error=job.error,
            outputs=job.outputs,
            delivery_links=job.delivery_links,
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


class CardIngestStatus(BaseModel):
    """One SD card's ingest progress (``GET /ingest/cards``).

    ``safe_to_remove`` means the pull finished and the card is idle — the S3
    upload runs from the staged copy and no longer needs the card. Progress is
    approximate (already-staged clips are skipped without re-copying), so the
    terminal ``state`` is the signal, not the percentage.
    """

    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(examples=["1234"])
    state: Literal["detected", "sweeping", "pulling", "safe_to_remove", "error"]
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    #: Master currently copying off the card (``pulling`` only).
    current_file: str | None = Field(default=None, examples=["GX010042.MP4"])
    error: str | None = None
    #: Epoch seconds of the last transition (repo convention: seconds, floats).
    updated_at: float


class AssignCameraRequest(BaseModel):
    """Body for ``POST /cameras/{id}/assign`` — set the camera's owning instructor."""

    model_config = ConfigDict(extra="forbid")

    #: Instructor account to own the camera (``null`` clears the assignment).
    instructor_id: str | None = Field(examples=["inst-42"])
    #: Two-camera (Ultimate) role: ``instructor`` (selfie cam) or ``external``
    #: (cameraman). Omit/``null`` for a single-camera setup.
    role: Literal["instructor", "external"] | None = Field(default=None, examples=["external"])


class UnlockRequest(BaseModel):
    """Body for ``POST /jobs/{id}/unlock`` — proof of the captured payment.

    The reference is SkydiveOS's own payment/transaction id. It is **required**: an
    unlock hands the customer the product, so it must be attributable to a real
    capture rather than being an unattributable state flip. Persisted on the job as
    ``payment_reference``.
    """

    model_config = ConfigDict(extra="forbid")

    payment_reference: str = Field(
        min_length=1,
        max_length=200,
        description="SkydiveOS payment/transaction id for the captured unlock payment",
        examples=["clover_txn_9f21c7"],
    )
    #: Optional amount captured, for the audit trail (display/reconciliation only —
    #: this service never prices anything).
    amount: float | None = Field(default=None, ge=0.0, examples=[39.0])
    #: Which gallery item was purchased. ``unlock`` (default — the paywall flip) or a
    #: purchasable add-on tile key (``raw`` / ``photos``). Fulfilment is gallery-side:
    #: the customer's existing ``/j/{code}`` page grows the purchased section on its
    #: next request. Unknown items are rejected — mirrors SkydiveOS's fail-loud
    #: pricing rule, so a typo'd tile key can't silently succeed-and-deliver-nothing.
    item: str = Field(
        default="unlock",
        min_length=1,
        max_length=40,
        description="Purchased gallery item: 'unlock' (paywall), 'raw', or 'photos'",
        examples=["unlock", "raw", "photos"],
    )


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
