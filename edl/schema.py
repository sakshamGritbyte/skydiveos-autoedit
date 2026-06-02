"""Edit Decision List (EDL) schema — the contract between /edl and /render.

The EDL is the single hand-off artifact of the Compose stage: a version-tagged,
ordered list of source clips that the Render stage executes against the full-res
MP4 with FFmpeg. Everything the editor (human or Claude) decides about *what to
show and how* lives here, in plain JSON, so it can be persisted with the job,
replayed, diffed, and A/B tested (CLAUDE.md: "EDL is JSON, version-tagged,
persisted with every job").

Conventions honoured:

* All timestamps are **seconds (float)** on the source MP4 timeline, never frames.
* ``speed_multiplier`` is the playback rate: ``1.0`` is real time, ``0.4`` is the
  40%-speed slow-mo we use for exits and peak-smile highlights, ``> 1.0`` speeds a
  clip up. Output duration of a clip is therefore ``(src_end - src_start) /
  speed_multiplier``.

The Pydantic models below are the *validation boundary*: the Claude response in
``edl.compose`` is parsed into these types and anything that does not satisfy the
invariants (ordered, positive-length clips, sane speeds) is rejected so the
Render stage never sees a malformed timeline.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bump when the *shape* of the EDL changes incompatibly. Persisted on every EDL so
# a saved job records which schema produced it (lets us replay / migrate old jobs).
EDL_VERSION = "1.0"


class Transition(StrEnum):
    """The cut/transition applied at a clip boundary.

    Kept deliberately small: these map 1:1 to filters the Render stage knows how
    to build. ``cut`` is a hard cut (the default between most clips); the others
    are short blends used sparingly (e.g. ``fade`` from the intro, ``crossfade``
    into the canopy ride).
    """

    cut = "cut"
    fade = "fade"
    crossfade = "crossfade"
    flash = "flash"


class Clip(BaseModel):
    """One segment of the source MP4 to place on the output timeline.

    A clip is a half-open source window ``[src_start, src_end)`` played back at
    ``speed_multiplier``. Transitions are optional; ``None`` means a plain hard
    cut at that boundary.
    """

    model_config = ConfigDict(extra="forbid")

    src_start: float = Field(ge=0.0, description="Source in-point (seconds).")
    src_end: float = Field(gt=0.0, description="Source out-point (seconds), exclusive.")
    speed_multiplier: float = Field(
        default=1.0,
        gt=0.0,
        description="Playback rate: 1.0 normal, 0.4 slow-mo, >1.0 sped up.",
    )
    transition_in: Transition | None = None
    transition_out: Transition | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Clip:
        if self.src_end <= self.src_start:
            raise ValueError(
                f"clip src_end ({self.src_end}) must be greater than "
                f"src_start ({self.src_start})"
            )
        return self

    @property
    def source_duration(self) -> float:
        """Length of the source window consumed, in seconds (real time)."""
        return self.src_end - self.src_start

    @property
    def output_duration(self) -> float:
        """Length this clip occupies on the *output* timeline, after speed ramp."""
        return self.source_duration / self.speed_multiplier


class EditDecisionList(BaseModel):
    """A complete, ordered edit: the clips plus the metadata Render needs.

    ``clips`` are played in list order. Intro/outro cards and the brand overlay
    are applied by Render from /templates and are intentionally *not* clips here —
    the EDL describes the cut of the *footage*; ``music`` names the backing track
    Render should mix under it.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = EDL_VERSION
    clips: list[Clip] = Field(min_length=1)
    music: str | None = Field(default=None, description="Music track id/name for Render.")
    notes: str | None = Field(default=None, description="Editor rationale (logged, not rendered).")

    @property
    def output_duration(self) -> float:
        """Total runtime of the footage edit (excludes intro/outro), in seconds."""
        return sum(clip.output_duration for clip in self.clips)
