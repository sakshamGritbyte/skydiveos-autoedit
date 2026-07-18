"""Per-second scoring of *external-cameraman* scenes with a YOLO person detector.

The FaceLandmarker scorer in :mod:`analysis.score` is built for the instructor
selfie cam, where the customer's face fills the frame. On external-cameraman
footage the tandem is far from the lens, so the face detector finds nothing and
every second silently scores ~0 — starving the downstream EDL/photo logic of any
signal. This module scores that footage from *body* geometry instead: it detects
people with YOLO and grades subject size, framing, and whether both jumpers are
in shot.

Crucially it emits the **exact same four fields** as :class:`FreefallScorer`
(``smile``, ``eye_contact``, ``face_in_frame``, ``face_centered``) so the
``scores.json`` schema is unchanged and every downstream consumer keeps working.
The body-geometry features are mapped onto those names:

===================  ==================================================================
scores.json field    external (YOLO) meaning
===================  ==================================================================
``face_in_frame``    ``subject_size`` scaled: ``clamp01(subject_size / 0.10)`` — a
                     person filling >=10% of the frame reads as fully "in frame" for a
                     distant cam.
``face_centered``    ``composition_centered`` — largest person box centred in the
                     middle 60%x60% of frame.
``smile``            constant ``0.0`` — undetectable at distance. Downstream
                     ``.get("smile", 0.0)`` paths already tolerate zeros.
``eye_contact``      ``both_visible`` — proxy for an "engaging shot": exactly two
                     well-separated people in frame (the tandem pair).
===================  ==================================================================

As with :class:`FreefallScorer`, a frame with no person detected scores 0.0 on
all four, and frames are averaged into one row per whole second of the scene.

TODO: add a ``camera_stable`` signal from GPMF gyro once the gyro-parsing
dependency is verified; skipped here to keep this PR self-contained.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from .extract import DEFAULT_FPS, extract_freefall_frames
from .proxy import _probe_duration

# Sampling rate for external scenes — matches the pipeline's 5 fps highlight scoring.
SCORE_FPS = DEFAULT_FPS

# COCO class 0 is "person"; the only class we care about.
_PERSON_CLASS = 0
_MODEL_NAME = "yolov8n.pt"

# Feature thresholds (documented where used).
_SUBJECT_SIZE_FULL = 0.10   # person area fraction that reads as fully "in frame"
_CENTER_MARGIN = 0.20       # middle (1 - 2*margin) box of the frame counts as centred
_BOTH_MAX_IOU = 0.30        # two boxes overlapping more than this aren't "both visible"
_BOTH_MAX_CENTER_DX = 0.30  # horizontal centre gap (frame-width fraction) for two people

# Output fields, identical to analysis.score.SCORE_FIELDS (schema contract).
SCORE_FIELDS: tuple[str, ...] = ("smile", "eye_contact", "face_in_frame", "face_centered")

# --------------------------------------------------------------------------- #
# Model cache — the YOLO weights load once per process, never per call/frame.
# --------------------------------------------------------------------------- #

_MODEL: Any = None


def _load_model() -> Any:
    """Construct the YOLO detector (imported lazily so ``import analysis`` stays cheap)."""
    try:
        from ultralytics import YOLO
    except ImportError as e:  # pragma: no cover - environment-dependent
        from .models import AnalysisError

        raise AnalysisError(
            f"ultralytics is required for external scoring but failed to import: {e!r}"
        ) from e
    return YOLO(_MODEL_NAME)


def get_model() -> Any:
    """Return the process-wide cached YOLO model, loading it on first use."""
    global _MODEL
    if _MODEL is None:
        _MODEL = _load_model()
    return _MODEL


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


# --------------------------------------------------------------------------- #
# Per-frame geometry.
# --------------------------------------------------------------------------- #


def _person_boxes(model: Any, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Detect people in one RGB frame; return pixel ``(x0, y0, x1, y1)`` boxes.

    ultralytics treats numpy input as BGR (OpenCV convention), so we flip the RGB
    frames from :func:`extract_freefall_frames` before inference.
    """
    bgr = np.ascontiguousarray(frame[:, :, ::-1])
    results = model(bgr, classes=[_PERSON_CLASS], verbose=False)
    boxes: list[tuple[float, float, float, float]] = []
    for r in results:
        b = getattr(r, "boxes", None)
        if b is None:
            continue
        xyxy = b.xyxy
        arr = xyxy.cpu().numpy() if hasattr(xyxy, "cpu") else np.asarray(xyxy)
        for row in arr:
            boxes.append((float(row[0]), float(row[1]), float(row[2]), float(row[3])))
    return boxes


def _area(box: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = box
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _center_x(box: tuple[float, float, float, float]) -> float:
    return (box[0] + box[2]) / 2.0


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _score_boxes(
    boxes: list[tuple[float, float, float, float]], width: int, height: int
) -> dict[str, float]:
    """Map one frame's person boxes onto the four ``scores.json`` fields.

    No person detected -> all four 0.0 (mirrors ``FreefallScorer``'s empty-frame rule).
    """
    if not boxes or width <= 0 or height <= 0:
        return {"smile": 0.0, "eye_contact": 0.0, "face_in_frame": 0.0, "face_centered": 0.0}

    frame_area = float(width * height)
    largest = max(boxes, key=_area)

    # subject_size: largest person-box area / frame area.
    subject_size = _clamp01(_area(largest) / frame_area)

    # composition_centered: largest box centre inside the middle 60%x60% of frame.
    cx, cy = _center_x(largest), (largest[1] + largest[3]) / 2.0
    centered = (
        _CENTER_MARGIN * width <= cx <= (1.0 - _CENTER_MARGIN) * width
        and _CENTER_MARGIN * height <= cy <= (1.0 - _CENTER_MARGIN) * height
    )
    composition_centered = 1.0 if centered else 0.0

    # both_visible: exactly two well-separated people, centres within 30% of frame width.
    both_visible = 0.0
    if len(boxes) == 2:
        a, b = boxes
        if _iou(a, b) < _BOTH_MAX_IOU and abs(
            _center_x(a) - _center_x(b)
        ) <= _BOTH_MAX_CENTER_DX * width:
            both_visible = 1.0

    return {
        "face_in_frame": _clamp01(subject_size / _SUBJECT_SIZE_FULL),
        "face_centered": composition_centered,
        "smile": 0.0,
        "eye_contact": both_visible,
    }


def score_frames_external(
    frames: Iterable[tuple[float, np.ndarray]], model: Any
) -> list[dict[str, float]]:
    """Score frames with YOLO and collapse to one averaged row per second.

    Buckets frames by the integer second of their source timestamp and averages
    each field — the same bucketing :meth:`FreefallScorer.score_frames` uses — so
    the output is one row ``{"ts", "smile", "eye_contact", "face_in_frame",
    "face_centered"}`` per second, sorted by ``ts``.
    """
    sums: dict[int, dict[str, float]] = {}
    counts: dict[int, int] = {}
    for ts, frame in frames:
        bucket = int(math.floor(ts + 1e-6))
        height, width = frame.shape[0], frame.shape[1]
        score = _score_boxes(_person_boxes(model, frame), width, height)
        agg = sums.get(bucket)
        if agg is None:
            sums[bucket] = dict(score)
            counts[bucket] = 1
        else:
            for field in SCORE_FIELDS:
                agg[field] += score[field]
            counts[bucket] += 1

    rows: list[dict[str, float]] = []
    for bucket in sorted(sums):
        n = counts[bucket]
        agg = sums[bucket]
        row: dict[str, float] = {"ts": float(bucket)}
        for field in SCORE_FIELDS:
            row[field] = round(agg[field] / n, 4)
        rows.append(row)
    return rows


def score_scene_external(
    scene_path: str | Path, *, fps: float = SCORE_FPS
) -> list[dict[str, float]]:
    """Per-second YOLO body scores for one external-cameraman scene MP4.

    Drop-in replacement for :func:`api.selfie.score_scene` on distant footage: it
    reuses :func:`extract_freefall_frames` to pull frames and emits the identical
    ``{ts, smile, eye_contact, face_in_frame, face_centered}`` schema, but grades
    body geometry (see module docstring for the field mapping) instead of faces.
    """
    duration = _probe_duration(Path(scene_path))
    if duration <= 0:
        return []
    frames = extract_freefall_frames(
        scene_path, 0.0, duration, fps=fps, allow_full_res=True
    )
    return score_frames_external(frames, get_model())
