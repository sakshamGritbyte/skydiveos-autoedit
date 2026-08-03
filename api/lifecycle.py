"""The design doc's media-job state machine, derived from a job's own record.

The Media Module design (Frame 02) describes a jump's life in *product* terms::

    PENDING_CAPTURE → CAPTURED → INGESTING → UPLOADED → EDITING → READY
                                                                    │
                              entitlement = EDITED_DOWNLOAD ────────┴──▶ DELIVERED
                              entitlement = PREVIEW_ONLY   ────────────▶ LOCKED_PREVIEW
                                                    payment captured ──▶ UNLOCKED
    (any stage, on error) ─────────────────────────────────────────────▶ FAILED

Internally this service keeps a *pipeline* state machine instead
(:class:`api.jobs.JobStatus`: ``queued → processing → ready → approved →
delivered``) with the lock state living in a separate field
(:class:`api.jobs.Entitlement`), because those two axes move independently:
``POST /jobs/{id}/unlock`` must flip the paywall **without** touching ``status``
(CLAUDE.md) or it would collide with the review/approve machine.

So the design's vocabulary is offered as a **derived, read-only projection** rather
than as a rename: :func:`media_state` folds ``(status, entitlement, paid_at)`` into
one :class:`MediaState` for SkydiveOS's UI and its status callback. Nothing in the
pipeline branches on it — it is a *view*, and it is deliberately never persisted, so
it cannot drift from the fields it is computed from.

Pure: no I/O, no settings, no disk. Safe to call per request.
"""

from __future__ import annotations

from enum import StrEnum

from .jobs import Entitlement, Job, JobStatus


class MediaState(StrEnum):
    """Product-facing lifecycle state of a jump's media (design doc Frame 02).

    The full vocabulary is defined so SkydiveOS can code against the diagram, but
    this service only ever *emits* the subset it can distinguish from a job record:
    ``CAPTURED`` and ``INGESTING`` describe the camera/transfer side of the flow —
    which happens before (or outside) a job record exists — and collapse into
    :attr:`pending_capture` / :attr:`uploaded` here.
    """

    pending_capture = "PENDING_CAPTURE"   # job opened, footage not in hand yet
    captured = "CAPTURED"                 # (ingest-side; not emitted by this service)
    ingesting = "INGESTING"               # (ingest-side; not emitted by this service)
    uploaded = "UPLOADED"                 # footage staged, worker not started
    editing = "EDITING"                   # segment → score → compose → render
    ready = "READY"                       # deliverables rendered, awaiting hand-off
    delivered = "DELIVERED"               # terminal, Path A: customer has the clean edit
    locked_preview = "LOCKED_PREVIEW"     # Path B: watermarked preview behind the paywall
    unlocked = "UNLOCKED"                 # terminal, Path B after payment capture
    failed = "FAILED"                     # error state (``Job.error``); resumable


def media_state(job: Job) -> MediaState:
    """Project a job onto the design doc's state machine.

    The mapping, in evaluation order:

    * ``failed`` → :attr:`MediaState.failed`.
    * ``queued`` → :attr:`MediaState.uploaded` once there's footage or a render to
      point at, else :attr:`MediaState.pending_capture`. (A two-camera Ultimate job
      still waiting for its second GoPro reads ``PENDING_CAPTURE``, which is exactly
      what it is waiting for.)
    * ``processing`` / ``rejected`` → :attr:`MediaState.editing` — a rejected job is
      on its way back through the editor, not a terminal state.
    * anything at/after "rendered" (``ready``, ``ready_for_review``, ``approved``,
      ``delivered``) splits on the paywall, since that is what the customer sees:
      ``preview_only`` → :attr:`MediaState.locked_preview`; a captured payment
      (``paid_at``) → :attr:`MediaState.unlocked`; ``delivered`` →
      :attr:`MediaState.delivered`; otherwise :attr:`MediaState.ready`.
    """
    if job.status is JobStatus.failed:
        return MediaState.failed
    if job.status is JobStatus.queued:
        has_material = bool(job.source_path) or bool(job.outputs)
        return MediaState.uploaded if has_material else MediaState.pending_capture
    if job.status in (JobStatus.processing, JobStatus.rejected):
        return MediaState.editing
    # ready / ready_for_review / approved / delivered — the customer-visible split.
    if job.entitlement is Entitlement.preview_only:
        return MediaState.locked_preview
    if job.paid_at is not None:
        return MediaState.unlocked
    if job.status is JobStatus.delivered:
        return MediaState.delivered
    return MediaState.ready
