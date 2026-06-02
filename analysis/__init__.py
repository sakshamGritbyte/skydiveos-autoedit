"""Analysis stage: freefall footage -> per-second highlight scores.

Public entry point: :func:`score_freefall`. Given the LRV proxy of a jump and the
``freefall_start`` / ``freefall_end`` timestamps from the /metadata stage, it:

1. extracts the freefall window from the **proxy only** at a low frame rate,
2. runs MediaPipe FaceLandmarker on each frame,
3. scores every second on ``smile``, ``eye_contact``, ``face_in_frame`` and
   ``face_centered`` (each 0..1),

returning a list of per-second rows the /edl stage uses to pick highlight clips::

    [{"ts": 412.0, "smile": 0.8, "eye_contact": 0.6,
      "face_in_frame": 1.0, "face_centered": 0.7}, ...]

All timestamps are seconds (float) on the source timeline, per project convention.
"""

from __future__ import annotations

import json
from pathlib import Path

from .extract import DEFAULT_FPS, DEFAULT_WIDTH, extract_freefall_frames
from .models import AnalysisError, resolve_model
from .score import SCORE_FIELDS, FreefallScorer

__all__ = [
    "score_freefall",
    "SCORE_FIELDS",
    "AnalysisError",
    "FreefallScorer",
    "extract_freefall_frames",
    "resolve_model",
]


def score_freefall(
    proxy_path: str | Path,
    freefall_start: float,
    freefall_end: float,
    *,
    fps: float = DEFAULT_FPS,
    proxy_width: int = DEFAULT_WIDTH,
    model_path: str | Path | None = None,
    min_detection_confidence: float = 0.4,
    output_path: str | Path | None = None,
    allow_full_res: bool = False,
) -> list[dict[str, float]]:
    """Score the freefall window of a jump's proxy, one row per second.

    Args:
        proxy_path: Path to the ``.lrv`` proxy. A non-proxy input raises
            :class:`AnalysisError` unless ``allow_full_res`` is set — analysis must
            not decode the full-res master (CLAUDE.md).
        freefall_start: Freefall window start (seconds, from /metadata).
        freefall_end: Freefall window end (seconds, from /metadata). If
            ``<= freefall_start`` an empty list is returned.
        fps: Frame sampling rate (default 5 — enough for per-second scoring).
        proxy_width: Width frames are downscaled to before inference.
        model_path: Override for the FaceLandmarker ``.task`` bundle (see
            :func:`analysis.models.resolve_model`).
        min_detection_confidence: FaceLandmarker detection threshold.
        output_path: If given, the rows are also written here as JSON.
        allow_full_res: Permit a non-``.lrv`` input (tests/debugging).

    Returns:
        List of ``{"ts", "smile", "eye_contact", "face_in_frame", "face_centered"}``
        rows, sorted by ``ts``. Empty if the window is empty or no frames decode.

    Raises:
        AnalysisError: bad proxy, missing ffmpeg/model, or decode/inference failure.
    """
    # Degenerate window: nothing to score, so don't pay the ffmpeg/model startup.
    if freefall_end <= freefall_start:
        rows: list[dict[str, float]] = []
    else:
        frames = extract_freefall_frames(
            proxy_path,
            freefall_start,
            freefall_end,
            fps=fps,
            width=proxy_width,
            allow_full_res=allow_full_res,
        )
        with FreefallScorer(
            model_path, min_detection_confidence=min_detection_confidence
        ) as scorer:
            rows = scorer.score_frames(frames)

    if output_path is not None:
        Path(output_path).write_text(json.dumps(rows, indent=2) + "\n")

    return rows
