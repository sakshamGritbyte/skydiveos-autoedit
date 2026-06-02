"""Compose stage: timeline + scores -> Claude -> a validated Edit Decision List.

This is stage 4 of the pipeline. It takes the structured output of the earlier
stages — the phase :class:`~metadata.segment.Segmentation`, the per-second
freefall ``scores`` from /analysis, and the booking's ``customer_meta`` — and asks
Claude (``claude-sonnet-4-6``, our pinned EDL model per CLAUDE.md) to make the
creative edit decisions, returning them as a JSON EDL.

Design rules from CLAUDE.md:

* **One Claude call per jump, max.** No tight loops. We make a single request and,
  *only if its output fails schema validation*, retry exactly once with the
  validation error fed back to the model.
* The model is given the timeline, the scores, the customer metadata, a target
  duration, and the house stylistic rules (slow-mo the exit, slow-mo the
  peak-smile beats, hard-cut the canopy ride down to ~5 s). It never sees video —
  only the structured signals — keeping this stage cheap and deterministic to test.
* The returned EDL is validated against :mod:`edl.schema` and persisted to
  ``<jobs_root>/{job_id}/edl.json`` so the job can be replayed / A-B tested.

The Claude call is injectable (``client=``) so tests run fully offline with a mock.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from .schema import EDL_VERSION, EditDecisionList
from .storage import persist_edl

if TYPE_CHECKING:  # the SDK's message type — used only to annotate, not at runtime
    from anthropic.types import MessageParam

logger = logging.getLogger(__name__)


class ClaudeClient(Protocol):
    """Structural type for the bit of the Anthropic SDK we use.

    ``anthropic.Anthropic`` satisfies this (it exposes ``.messages.create``), and so
    can a test double — keeping the Claude call injectable without importing the SDK.
    A read-only property (not a bare attribute) so the SDK's own ``messages``
    property and a test double's instance attribute both satisfy it.
    """

    @property
    def messages(self) -> Any: ...

# Pinned EDL model (CLAUDE.md → Tech Stack: "AI decisioning: Claude API for EDL
# generation"). Bumping this is a deliberate, reviewable change.
MODEL = "claude-sonnet-4-6"
DEFAULT_TARGET_DURATION = 90.0  # seconds; customer edits run 60–120 s
_MAX_TOKENS = 4096


class EdlError(RuntimeError):
    """Raised when /edl cannot produce a valid EDL (bad model output, no API key)."""


_SYSTEM_PROMPT = """\
You are the senior video editor for a tandem skydiving operation. You turn the \
telemetry and footage analysis of a single jump into an Edit Decision List (EDL): \
the precise cut of the raw GoPro footage that becomes the customer's 60–120 second \
keepsake edit.

You receive only structured signals (the jump timeline, per-second freefall \
scores, the customer's details, and a target duration) — never the video itself. \
Make confident, tasteful editing decisions from those signals.

Respond with ONE JSON object and nothing else (no prose, no markdown fences). It \
must match this shape exactly:

{
  "version": "<string>",
  "clips": [
    {
      "src_start": <seconds, float>,
      "src_end": <seconds, float, > src_start>,
      "speed_multiplier": <float, 1.0 = real time, 0.4 = slow-mo, >1.0 = sped up>,
      "transition_in": <one of "cut","fade","crossfade","flash", or null>,
      "transition_out": <one of "cut","fade","crossfade","flash", or null>
    }
  ],
  "music": <string track name or null>,
  "notes": <short string explaining your choices, or null>
}

All times are seconds on the source MP4 timeline. Clips play in array order.\
"""

_STYLE_RULES = """\
Editing rules (follow unless the signals make one impossible):
- Open on the exit. Render the exit moment in slow motion (speed_multiplier 0.4).
- Build the middle from the best freefall beats, ranked by the per-second scores
  (smile, eye_contact, face_in_frame, face_centered). Put the single highest
  peak-smile second in slow motion (speed_multiplier 0.4).
- Keep the canopy ride SHORT: a single hard cut down to about 5 seconds total.
  The canopy ride is mostly boring — trim it, do not feature it.
- Include the deployment and a brief landing if their timestamps are available.
- Aim for a total output duration close to the target; do not exceed 120 seconds.
- Use mostly hard cuts; reserve fades/crossfades for the open and the canopy.\
"""


def _segmentation_dict(segmentation: Any) -> dict[str, Any]:
    """Normalise a Segmentation dataclass (or a plain mapping) to a JSON-able dict."""
    as_dict = getattr(segmentation, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    return dict(segmentation)


def _build_user_prompt(
    *,
    segmentation: Mapping[str, Any],
    scores: Sequence[Mapping[str, float]],
    customer_meta: Mapping[str, Any],
    style: Mapping[str, Any] | None,
    target_duration: float,
) -> str:
    """Assemble the single user message: all the signals + the target + the rules."""
    payload = {
        "target_duration_seconds": target_duration,
        "segmentation": segmentation,
        "per_second_scores": list(scores),
        "customer_meta": dict(customer_meta),
        "style": dict(style) if style else {},
    }
    return (
        f"{_STYLE_RULES}\n\n"
        "Here are the signals for this jump as JSON:\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True, default=str)}\n\n"
        f'Produce the EDL now. Use version "{EDL_VERSION}".'
    )


def _response_text(response: Any) -> str:
    """Pull the assistant's text out of a Messages API response."""
    return next((b.text for b in response.content if b.type == "text"), "")


def _extract_json(text: str) -> str:
    """Best-effort isolate the JSON object from the model's reply.

    The model is told to return bare JSON, but defends against stray prose or
    ```json fences by slicing from the first ``{`` to the last ``}``.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise EdlError(f"no JSON object found in model response: {text[:200]!r}")
    return text[start : end + 1]


def _parse_edl(text: str) -> EditDecisionList:
    """Parse + schema-validate one model reply into an EditDecisionList.

    Raises :class:`ValidationError` (schema) or :class:`EdlError` (not even JSON)
    so the caller can decide whether to retry.
    """
    raw = _extract_json(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EdlError(f"model response was not valid JSON: {e}") from e
    return EditDecisionList.model_validate(data)


def compose_edl(
    segmentation: Any,
    scores: Sequence[Mapping[str, float]],
    customer_meta: Mapping[str, Any],
    style: Mapping[str, Any] | None = None,
    *,
    job_id: str | None = None,
    target_duration: float = DEFAULT_TARGET_DURATION,
    model: str = MODEL,
    client: ClaudeClient | None = None,
    jobs_root: str | Path | None = None,
    persist: bool = True,
) -> EditDecisionList:
    """Ask Claude for an EDL for one jump, validate it, and persist it.

    Args:
        segmentation: The jump timeline — a :class:`~metadata.segment.Segmentation`
            (or any mapping of phase -> seconds). ``None`` phases are passed through
            so the model knows which markers are unavailable.
        scores: Per-second freefall rows from /analysis (``ts`` + score fields).
        customer_meta: Booking details (name, tandem/solo, branding, ...). Passed
            to the model verbatim, so keep it free of secrets.
        style: Optional stylistic overrides (music mood, tone) merged into the
            prompt alongside the house rules.
        job_id: Job this EDL belongs to. Required when ``persist`` is True (the EDL
            is written to ``<jobs_root>/{job_id}/edl.json``).
        target_duration: Desired output length in seconds (default 90).
        model: Claude model id (default the pinned ``claude-sonnet-4-6``).
        client: An ``anthropic.Anthropic`` client. If omitted, one is constructed
            from the environment (``ANTHROPIC_API_KEY``). Injectable for tests.
        jobs_root: Override for the jobs storage root (else ``$JOBS_ROOT``/``./jobs``).
        persist: Write the EDL to disk (requires ``job_id``).

    Returns:
        The validated :class:`~edl.schema.EditDecisionList`.

    Raises:
        EdlError: the API client is unavailable, or the model returned an invalid
            EDL twice (initial call + one retry).
        ValueError: ``persist`` is True but ``job_id`` was not provided.
    """
    if persist and job_id is None:
        raise ValueError("job_id is required when persist=True")

    if client is None:
        try:
            from anthropic import Anthropic  # local import: SDK optional at import time
        except ImportError as e:  # pragma: no cover - exercised only without the SDK
            raise EdlError("anthropic SDK not installed; pass client= or install it") from e
        client = Anthropic()

    user_prompt = _build_user_prompt(
        segmentation=_segmentation_dict(segmentation),
        scores=scores,
        customer_meta=customer_meta,
        style=style,
        target_duration=target_duration,
    )
    messages: list[MessageParam] = [{"role": "user", "content": user_prompt}]

    # One call per jump; retry at most once, feeding the validation error back so
    # the model can correct itself (CLAUDE.md: never call Claude in a tight loop).
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001 - surface any SDK/transport error as EdlError
            raise EdlError(f"Claude API call failed: {e!r}") from e

        text = _response_text(response)
        try:
            edl = _parse_edl(text)
            break
        except (ValidationError, EdlError) as e:
            last_error = e
            logger.warning("EDL attempt %d invalid: %s", attempt + 1, e)
            if attempt == 0:
                # Append the bad reply + the error so the retry is grounded.
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That response was not a valid EDL. Error:\n"
                            f"{e}\n\n"
                            "Return ONE corrected JSON EDL object and nothing else."
                        ),
                    }
                )
    else:
        raise EdlError(f"model failed to produce a valid EDL after 2 attempts: {last_error}")

    if persist:
        assert job_id is not None  # guaranteed above; for the type checker
        path = persist_edl(edl, job_id, jobs_root)
        logger.info("persisted EDL for job %s -> %s", job_id, path)

    return edl
