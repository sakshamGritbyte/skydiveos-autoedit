"""Locating the MediaPipe FaceLandmarker model bundle.

The /analysis stage scores freefall footage with MediaPipe's Tasks API, which —
unlike the old ``mp.solutions`` solutions — does not ship its model weights inside
the pip package. We need the ``face_landmarker`` ``.task`` bundle (face mesh +
iris + blendshapes + head-pose matrix) on disk before we can run.

Resolution order (first hit wins):

1. an explicit ``model_path`` argument,
2. the ``FACE_LANDMARKER_MODEL`` environment variable (bake this into GPU-worker
   images so they never reach out to the network),
3. a repo-local cache at ``analysis/models/face_landmarker.task``,
4. a one-time download of the pinned bundle into that cache.

The cached bundle is git-ignored (see ``.gitignore``); it is a reproducible build
artifact, not source. If it cannot be obtained (e.g. offline CI) we raise
:class:`AnalysisError` and callers/tests degrade rather than hang.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


class AnalysisError(RuntimeError):
    """Raised when the analysis stage cannot run (missing model, bad proxy, ...)."""


# Pinned float16 FaceLandmarker bundle (face mesh + iris + 52 blendshapes + the
# 4x4 facial transformation matrix). Pinned to a specific version for
# reproducibility — bumping the URL is a deliberate, reviewable change.
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
MODEL_ENV_VAR = "FACE_LANDMARKER_MODEL"
_REPO_CACHE = Path(__file__).resolve().parent / "models" / "face_landmarker.task"
_DOWNLOAD_TIMEOUT_S = 60


def resolve_model(model_path: str | Path | None = None) -> Path:
    """Return a path to the FaceLandmarker ``.task`` bundle, downloading if needed.

    See the module docstring for the resolution order. Raises
    :class:`AnalysisError` if an explicit/env path is missing or if the bundle
    cannot be downloaded into the repo cache.
    """
    if model_path is not None:
        path = Path(model_path)
        if not path.exists():
            raise AnalysisError(f"model_path does not exist: {path}")
        return path

    env = os.environ.get(MODEL_ENV_VAR)
    if env:
        path = Path(env)
        if not path.exists():
            raise AnalysisError(f"{MODEL_ENV_VAR} points at a missing file: {path}")
        return path

    if _REPO_CACHE.exists():
        return _REPO_CACHE

    return _download_model(_REPO_CACHE)


def _download_model(dest: Path) -> Path:
    """Fetch the pinned bundle to ``dest`` atomically (temp file + rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    logger.info("downloading FaceLandmarker model %s -> %s", MODEL_URL, dest)
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
            tmp.write_bytes(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        tmp.unlink(missing_ok=True)
        raise AnalysisError(
            f"could not download FaceLandmarker model from {MODEL_URL}: {e!r}. "
            f"Set ${MODEL_ENV_VAR} to a local copy to run offline."
        ) from e
    tmp.replace(dest)
    return dest
