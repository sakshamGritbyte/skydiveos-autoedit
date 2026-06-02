"""Per-second highlight scoring of freefall frames with MediaPipe FaceLandmarker.

For every frame we run the FaceLandmarker (face mesh + iris + 52 blendshapes + a
4x4 head-pose matrix) and derive four 0..1 signals about the most prominent face:

* ``smile``        — ``mouthSmile{Left,Right}`` blendshapes, the customer grinning.
* ``eye_contact``  — head pointed at the lens *and* eyes centred (gaze on camera).
* ``face_in_frame``— fraction of the face bounding box inside the frame (1 = fully
                     in shot, 0 = no face / fully cropped out).
* ``face_centered``— how close the face is to frame centre (1 = dead centre).

A frame with no detected face scores 0 on all four. We then average every frame
in a one-second bucket into that second's score, so the output is one row per
second of freefall — the granularity the /edl stage reasons over when picking
highlight clips.

We only score the single most prominent face. On a tandem the customer's face is
the largest/closest to the GoPro, which is what we want to grade; cleanly
separating instructor vs. customer is left to a future model (CLAUDE.md: train a
separate scoring model for v2).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from .models import AnalysisError, resolve_model

if TYPE_CHECKING:
    from pathlib import Path

# Output fields, in display order. ``ts`` is added alongside these per row.
SCORE_FIELDS: tuple[str, ...] = ("smile", "eye_contact", "face_in_frame", "face_centered")

# Heuristic scales (documented inline where used).
_GAZE_AVERT_SCALE = 0.8  # summed eyeLook magnitude that reads as "looking fully away"
_CENTER_FALLOFF = 0.5    # bbox-centre distance at which face_centered reaches 0


@dataclass
class _FrameScore:
    smile: float
    eye_contact: float
    face_in_frame: float
    face_centered: float

    @classmethod
    def empty(cls) -> _FrameScore:
        """A frame with no detected face: zero on every signal."""
        return cls(0.0, 0.0, 0.0, 0.0)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _smile(blendshapes: dict[str, float]) -> float:
    left = blendshapes.get("mouthSmileLeft", 0.0)
    right = blendshapes.get("mouthSmileRight", 0.0)
    return _clamp01((left + right) / 2.0)


def _gaze_centered(blendshapes: dict[str, float]) -> float:
    """How centred the eyeballs are within the eyes (1 = looking dead ahead).

    The ``eyeLook{In,Out,Up,Down}`` blendshapes are one-sided 0..1 magnitudes; we
    take the per-eye horizontal+vertical deviation, average the eyes, and map a
    fully-averted gaze (``_GAZE_AVERT_SCALE``) to 0.
    """
    def eye_dev(side: str) -> float:
        h = blendshapes.get(f"eyeLookIn{side}", 0.0) + blendshapes.get(f"eyeLookOut{side}", 0.0)
        v = blendshapes.get(f"eyeLookUp{side}", 0.0) + blendshapes.get(f"eyeLookDown{side}", 0.0)
        return math.hypot(h, v)

    dev = (eye_dev("Left") + eye_dev("Right")) / 2.0
    return _clamp01(1.0 - dev / _GAZE_AVERT_SCALE)


def _head_frontality(matrix: np.ndarray | None) -> float:
    """Frontality from the head-pose matrix (1 = facing the lens, 0 = side-on).

    Uses ``cos(yaw) * cos(pitch)`` from the rotation block, so both turning away
    and looking up/down reduce the score. Roll (in-plane tilt) is ignored — a
    tilted-but-facing head still makes eye contact.
    """
    if matrix is None:
        return 1.0  # no pose info -> don't penalise; eye_contact falls back to gaze
    r = matrix[:3, :3]
    yaw = math.atan2(-r[2, 0], math.hypot(r[0, 0], r[1, 0]))
    pitch = math.atan2(r[2, 1], r[2, 2])
    return _clamp01(max(0.0, math.cos(yaw)) * max(0.0, math.cos(pitch)))


def _bbox(landmarks: list[Any]) -> tuple[float, float, float, float]:
    """Axis-aligned bbox (x0, y0, x1, y1) in normalised coords from face mesh.

    Normalised landmark coords can fall outside [0, 1] when the face extends past
    the frame edge, which is exactly what ``face_in_frame`` keys off.
    """
    xs = [p.x for p in landmarks]
    ys = [p.y for p in landmarks]
    return min(xs), min(ys), max(xs), max(ys)


def _face_in_frame(bbox: tuple[float, float, float, float]) -> float:
    """Fraction of the face bbox area that lies inside the frame."""
    x0, y0, x1, y1 = bbox
    full = (x1 - x0) * (y1 - y0)
    if full <= 0:
        return 0.0
    cw = max(0.0, min(1.0, x1) - max(0.0, x0))
    ch = max(0.0, min(1.0, y1) - max(0.0, y0))
    return _clamp01((cw * ch) / full)


def _face_centered(bbox: tuple[float, float, float, float]) -> float:
    """Closeness of the bbox centre to the frame centre (1 = dead centre)."""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dist = math.hypot(cx - 0.5, cy - 0.5)
    return _clamp01(1.0 - dist / _CENTER_FALLOFF)


class FreefallScorer:
    """Holds a loaded FaceLandmarker and scores RGB frames.

    Use as a context manager so the underlying graph is released::

        with FreefallScorer() as scorer:
            rows = scorer.score_frames(frames)

    MediaPipe is imported lazily on construction so ``import analysis`` stays cheap
    (and ``--help`` doesn't pay the GL/graph startup cost).
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        min_detection_confidence: float = 0.4,
    ) -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as e:  # pragma: no cover - environment-dependent
            raise AnalysisError(
                f"mediapipe is required for analysis but failed to import: {e!r}"
            ) from e

        self._mp = mp
        model = resolve_model(model_path)
        options = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.IMAGE,
            num_faces=1,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
            min_face_detection_confidence=min_detection_confidence,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)

    def __enter__(self) -> FreefallScorer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._landmarker.close()

    def _score_frame(self, frame: np.ndarray) -> _FrameScore:
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=frame)
        result = self._landmarker.detect(image)
        if not result.face_landmarks:
            return _FrameScore.empty()

        landmarks = result.face_landmarks[0]
        blendshapes: dict[str, float] = {}
        if result.face_blendshapes:
            blendshapes = {c.category_name: c.score for c in result.face_blendshapes[0]}

        matrix: np.ndarray | None = None
        if result.facial_transformation_matrixes:
            matrix = np.asarray(result.facial_transformation_matrixes[0]).reshape(4, 4)

        bbox = _bbox(landmarks)
        return _FrameScore(
            smile=_smile(blendshapes),
            eye_contact=_clamp01(_head_frontality(matrix) * _gaze_centered(blendshapes)),
            face_in_frame=_face_in_frame(bbox),
            face_centered=_face_centered(bbox),
        )

    def score_frames(
        self, frames: Iterable[tuple[float, np.ndarray]]
    ) -> list[dict[str, float]]:
        """Score frames and collapse them to one averaged row per second.

        Returns rows ``{"ts": <second>, "smile": .., "eye_contact": .., ...}``
        sorted by ``ts``. Frames are bucketed by the integer second of their
        source timestamp; the row ``ts`` is that whole second (float).
        """
        sums: dict[int, _FrameScore] = {}
        counts: dict[int, int] = {}
        for ts, frame in frames:
            bucket = int(math.floor(ts + 1e-6))
            score = self._score_frame(frame)
            agg = sums.get(bucket)
            if agg is None:
                sums[bucket] = _FrameScore(
                    score.smile, score.eye_contact, score.face_in_frame, score.face_centered
                )
                counts[bucket] = 1
            else:
                agg.smile += score.smile
                agg.eye_contact += score.eye_contact
                agg.face_in_frame += score.face_in_frame
                agg.face_centered += score.face_centered
                counts[bucket] += 1

        rows: list[dict[str, float]] = []
        for bucket in sorted(sums):
            n = counts[bucket]
            agg = sums[bucket]
            rows.append(
                {
                    "ts": float(bucket),
                    "smile": round(agg.smile / n, 4),
                    "eye_contact": round(agg.eye_contact / n, 4),
                    "face_in_frame": round(agg.face_in_frame / n, 4),
                    "face_centered": round(agg.face_centered / n, 4),
                }
            )
        return rows
