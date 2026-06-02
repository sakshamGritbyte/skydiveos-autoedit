"""Render stage (/render): execute an EDL against the full-res MP4 with FFmpeg.

Public surface:

* :func:`render_edl` — the stage entry point: an :class:`~edl.schema.EditDecisionList`
  + the full-res master -> ``<jobs_root>/{job_id}/final.mp4`` at 1080p / h264 / 30fps,
  with intro/outro cards, a burned-in customer caption, and a ducked music bed.
* :func:`build_filtergraph` / :class:`FilterGraph` — the pure FFmpeg-graph builder
  (trim, speed-ramp, concat, overlay, side-chain duck), unit-testable without FFmpeg.
* :func:`resolve_intro` / :func:`resolve_outro` / :func:`resolve_music` — /templates
  asset lookup.
* :class:`RenderError` — raised when a render cannot be produced.

See :mod:`render.builder` for the graph and :mod:`render.render` for the orchestration.
All timestamps are seconds (float) on the source timeline, per project convention.
"""

from __future__ import annotations

from .builder import (
    OUT_FPS,
    OUT_HEIGHT,
    OUT_WIDTH,
    FilterGraph,
    InputSpec,
    atempo_chain,
    build_filtergraph,
)
from .caption import CaptionError, render_caption, resolve_font
from .render import FINAL_FILENAME, RenderError, render_edl
from .templates import resolve_intro, resolve_music, resolve_outro, templates_root

__all__ = [
    "render_edl",
    "RenderError",
    "FINAL_FILENAME",
    "build_filtergraph",
    "FilterGraph",
    "InputSpec",
    "atempo_chain",
    "OUT_WIDTH",
    "OUT_HEIGHT",
    "OUT_FPS",
    "render_caption",
    "resolve_font",
    "CaptionError",
    "resolve_intro",
    "resolve_outro",
    "resolve_music",
    "templates_root",
]
