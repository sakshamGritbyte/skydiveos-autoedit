"""Tests for the derived media-job state machine (api/lifecycle.py).

:func:`api.lifecycle.media_state` is the only place the design doc's Frame 02
vocabulary is produced, and it must stay a *projection*: the two terminal branches
(``DELIVERED`` vs ``LOCKED_PREVIEW`` → ``UNLOCKED``) come from ``entitlement`` +
``paid_at``, never from a mutated ``status`` — unlocking must not disturb the
review/delivery machine.
"""

from __future__ import annotations

import pytest

from api.jobs import Entitlement, Job, JobStatus
from api.lifecycle import MediaState, media_state


def _job(**fields: object) -> Job:
    return Job(job_id="j1", **fields)  # type: ignore[arg-type]


def test_a_fresh_job_is_pending_capture() -> None:
    assert media_state(_job()) is MediaState.pending_capture


def test_queued_with_footage_is_uploaded() -> None:
    assert media_state(_job(source_path="/jobs/j1/source.mp4")) is MediaState.uploaded


@pytest.mark.parametrize("status", [JobStatus.processing, JobStatus.rejected])
def test_processing_and_rejected_are_editing(status: JobStatus) -> None:
    """A rejected job is on its way back through the editor, not terminal."""
    assert media_state(_job(status=status)) is MediaState.editing


def test_failed_is_failed_whatever_the_entitlement() -> None:
    job = _job(status=JobStatus.failed, entitlement=Entitlement.preview_only, error="boom")
    assert media_state(job) is MediaState.failed


@pytest.mark.parametrize(
    "status", [JobStatus.ready, JobStatus.ready_for_review, JobStatus.approved]
)
def test_rendered_but_not_delivered_is_ready(status: JobStatus) -> None:
    assert media_state(_job(status=status)) is MediaState.ready


def test_path_a_delivered_is_delivered() -> None:
    job = _job(status=JobStatus.delivered, entitlement=Entitlement.edited_download)
    assert media_state(job) is MediaState.delivered


@pytest.mark.parametrize("status", [JobStatus.ready, JobStatus.delivered])
def test_path_b_is_locked_preview_until_payment(status: JobStatus) -> None:
    """The link may already be out — while the paywall stands, the state is locked."""
    job = _job(status=status, entitlement=Entitlement.preview_only)
    assert media_state(job) is MediaState.locked_preview


def test_unlock_flips_locked_preview_to_unlocked_without_touching_status() -> None:
    locked = _job(status=JobStatus.delivered, entitlement=Entitlement.preview_only)
    assert media_state(locked) is MediaState.locked_preview
    # Exactly what POST /jobs/{id}/unlock does: entitlement + paid_at, no status change.
    unlocked = locked.model_copy(
        update={"entitlement": Entitlement.edited_download, "paid_at": 1_700_000_000.0}
    )
    assert unlocked.status is locked.status
    assert media_state(unlocked) is MediaState.unlocked


def test_states_are_the_design_docs_spellings() -> None:
    assert MediaState.locked_preview.value == "LOCKED_PREVIEW"
    assert MediaState.pending_capture.value == "PENDING_CAPTURE"
