"""Tests for the /edl (Compose) stage.

The Claude call is always mocked — these tests run fully offline and never touch
the network or need an API key. A :class:`FakeClient` stands in for
``anthropic.Anthropic``: it returns canned ``messages.create`` responses (shaped
like the real SDK: ``response.content`` is a list of blocks with ``.type`` /
``.text``) and records how many times it was called, so we can assert the
"one call per jump, one retry max" contract.

Layers:

* **Schema** tests of the Pydantic models (pure, no client).
* **Compose** tests of ``compose_edl``: happy path + persistence, retry-once on
  invalid output, give-up after two failures, input-shape flexibility, and the
  one-call/two-call budget.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from edl import (
    EDL_VERSION,
    Clip,
    EditDecisionList,
    EdlError,
    Transition,
    compose_edl,
)
from edl.storage import edl_path, load_edl
from metadata.segment import Segmentation

# --------------------------------------------------------------------------- #
# A mock Anthropic client.
# --------------------------------------------------------------------------- #

def _text_response(text: str) -> SimpleNamespace:
    """Mimic a Messages API response with a single text content block."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _FakeMessages:
    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        # Serve the next scripted reply; reuse the last one if over-called.
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return _text_response(self._replies[idx])


class FakeClient:
    """Drop-in for ``anthropic.Anthropic`` that returns scripted replies."""

    def __init__(self, *replies: str) -> None:
        self.messages = _FakeMessages(list(replies))


# --------------------------------------------------------------------------- #
# Fixtures: representative pipeline inputs.
# --------------------------------------------------------------------------- #

@pytest.fixture
def segmentation() -> Segmentation:
    return Segmentation(
        exit=27.0,
        freefall_start=29.0,
        freefall_end=72.0,
        deployment=74.0,
        canopy_start=78.0,
        landing=240.0,
        landing_end=255.0,
    )


@pytest.fixture
def scores() -> list[dict[str, float]]:
    return [
        {"ts": 30.0, "smile": 0.2, "eye_contact": 0.3, "face_in_frame": 1.0, "face_centered": 0.7},
        {"ts": 31.0, "smile": 0.9, "eye_contact": 0.8, "face_in_frame": 1.0, "face_centered": 0.9},
        {"ts": 32.0, "smile": 0.5, "eye_contact": 0.6, "face_in_frame": 1.0, "face_centered": 0.8},
    ]


@pytest.fixture
def customer_meta() -> dict[str, Any]:
    return {"name": "Jane Doe", "product": "tandem", "branding": "SkyHigh Dropzone"}


VALID_EDL_JSON = json.dumps(
    {
        "version": EDL_VERSION,
        "clips": [
            {"src_start": 27.0, "src_end": 30.0, "speed_multiplier": 0.4,
             "transition_in": "fade", "transition_out": "cut"},
            {"src_start": 31.0, "src_end": 32.0, "speed_multiplier": 0.4},
            {"src_start": 74.0, "src_end": 79.0, "speed_multiplier": 1.0,
             "transition_out": "crossfade"},
        ],
        "music": "upbeat_indie",
        "notes": "Slow-mo exit + peak smile at 31s; canopy trimmed to 5s.",
    }
)


# --------------------------------------------------------------------------- #
# Schema tests (no client).
# --------------------------------------------------------------------------- #

def test_clip_durations_account_for_speed() -> None:
    clip = Clip(src_start=10.0, src_end=14.0, speed_multiplier=0.4)
    assert clip.source_duration == pytest.approx(4.0)
    assert clip.output_duration == pytest.approx(10.0)  # 4s of source at 40% speed


def test_clip_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError, match="src_end"):
        Clip(src_start=10.0, src_end=10.0)
    with pytest.raises(ValueError, match="src_end"):
        Clip(src_start=10.0, src_end=5.0)


def test_clip_rejects_non_positive_speed() -> None:
    with pytest.raises(ValueError):
        Clip(src_start=0.0, src_end=5.0, speed_multiplier=0.0)


def test_clip_defaults() -> None:
    clip = Clip(src_start=0.0, src_end=5.0)
    assert clip.speed_multiplier == 1.0
    assert clip.transition_in is None and clip.transition_out is None


def test_edl_requires_at_least_one_clip() -> None:
    with pytest.raises(ValueError):
        EditDecisionList(clips=[])


def test_edl_output_duration_sums_clips() -> None:
    edl = EditDecisionList(
        clips=[
            Clip(src_start=0.0, src_end=4.0, speed_multiplier=0.4),  # 10s out
            Clip(src_start=10.0, src_end=15.0),  # 5s out
        ]
    )
    assert edl.output_duration == pytest.approx(15.0)
    assert edl.version == EDL_VERSION


def test_edl_roundtrips_through_json() -> None:
    edl = EditDecisionList.model_validate_json(VALID_EDL_JSON)
    assert len(edl.clips) == 3
    assert edl.clips[0].transition_in is Transition.fade
    assert edl.clips[0].speed_multiplier == pytest.approx(0.4)
    assert edl.music == "upbeat_indie"


# --------------------------------------------------------------------------- #
# compose_edl tests (mocked client).
# --------------------------------------------------------------------------- #

def test_compose_happy_path_and_persist(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
    tmp_path: Path,
) -> None:
    client = FakeClient(VALID_EDL_JSON)
    edl = compose_edl(
        segmentation, scores, customer_meta,
        job_id="job-123", client=client, jobs_root=tmp_path,
    )

    assert isinstance(edl, EditDecisionList)
    assert len(edl.clips) == 3
    # One call per jump.
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"

    # Persisted to <jobs_root>/{job_id}/edl.json and reloadable.
    path = edl_path("job-123", tmp_path)
    assert path == tmp_path / "job-123" / "edl.json"
    assert path.exists()
    assert load_edl("job-123", tmp_path).clips[0].src_start == pytest.approx(27.0)


def test_compose_passes_signals_into_prompt(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    client = FakeClient(VALID_EDL_JSON)
    style = {"mood": "epic", "music": "cinematic"}
    compose_edl(
        segmentation, scores, customer_meta, style,
        job_id="j", client=client, persist=False, target_duration=75.0,
    )
    sent = client.messages.calls[0]["messages"][0]["content"]
    # Timeline, scores, customer, style, target, and house rules all reach the model.
    assert "freefall_start" in sent
    assert "Jane Doe" in sent
    assert '"mood": "epic"' in sent
    assert "75.0" in sent
    assert "slow motion" in sent  # stylistic rules block
    assert "canopy ride" in sent.lower()


def test_compose_retries_once_on_invalid_then_succeeds(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    # First reply: clip with src_end <= src_start (schema-invalid). Second: valid.
    bad = json.dumps({"version": EDL_VERSION, "clips": [{"src_start": 5.0, "src_end": 1.0}]})
    client = FakeClient(bad, VALID_EDL_JSON)
    edl = compose_edl(
        segmentation, scores, customer_meta,
        job_id="j", client=client, persist=False,
    )
    assert len(edl.clips) == 3
    assert len(client.messages.calls) == 2  # initial + one retry
    # The retry carries the prior bad reply + the correction request.
    retry_messages = client.messages.calls[1]["messages"]
    assert len(retry_messages) == 3
    assert retry_messages[1]["role"] == "assistant"
    assert "not a valid EDL" in retry_messages[2]["content"]


def test_compose_gives_up_after_two_invalid(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    client = FakeClient("not json at all", "still { not valid")
    with pytest.raises(EdlError, match="after 2 attempts"):
        compose_edl(segmentation, scores, customer_meta, job_id="j", client=client, persist=False)
    assert len(client.messages.calls) == 2  # never loops more than twice


def test_compose_strips_markdown_fences(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    fenced = f"Here is your EDL:\n```json\n{VALID_EDL_JSON}\n```\n"
    client = FakeClient(fenced)
    edl = compose_edl(segmentation, scores, customer_meta, job_id="j", client=client, persist=False)
    assert len(edl.clips) == 3
    assert len(client.messages.calls) == 1  # fence is tolerated, no retry needed


def test_compose_accepts_segmentation_dict(
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    """A plain mapping works as well as a Segmentation dataclass."""
    seg_dict = {"exit": 27.0, "freefall_start": 29.0, "freefall_end": 72.0, "deployment": None}
    client = FakeClient(VALID_EDL_JSON)
    edl = compose_edl(seg_dict, scores, customer_meta, job_id="j", client=client, persist=False)
    assert isinstance(edl, EditDecisionList)
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "freefall_start" in sent


def test_compose_requires_job_id_when_persisting(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    client = FakeClient(VALID_EDL_JSON)
    with pytest.raises(ValueError, match="job_id is required"):
        compose_edl(segmentation, scores, customer_meta, client=client)  # persist defaults True


def test_compose_wraps_api_errors(
    segmentation: Segmentation,
    scores: list[dict[str, float]],
    customer_meta: dict[str, Any],
) -> None:
    class BoomClient:
        class messages:  # noqa: N801 - mimic SDK attribute shape
            @staticmethod
            def create(**_: Any) -> Any:
                raise RuntimeError("network down")

    with pytest.raises(EdlError, match="Claude API call failed"):
        compose_edl(segmentation, scores, customer_meta, job_id="j",
                    client=BoomClient(), persist=False)
