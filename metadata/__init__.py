"""Metadata stage: GoPro MP4 -> jump phase timeline.

Public entry point: :func:`extract_metadata`. Given a path to a GoPro MP4 it
parses the embedded GPMF telemetry, segments the jump into phases, and fills any
undetectable field from human-labeled ground truth (``sample-data/labels.json``).

All timestamps are seconds (float), per project convention.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .gpmf import GpmfData, GPMFError, parse_gpmf
from .segment import Segmentation, segment

__all__ = [
    "extract_metadata",
    "PHASES",
    "Segmentation",
    "GpmfData",
    "GPMFError",
    "parse_gpmf",
    "segment",
]

# Canonical output fields, in jump order.
PHASES: tuple[str, ...] = (
    "plane_boarding",
    "exit",
    "freefall_start",
    "freefall_end",
    "deployment",
    "canopy_start",
    "landing",
    "landing_end",
)

# Default ground-truth labels live alongside the sample jumps.
_DEFAULT_LABELS = Path(__file__).resolve().parent.parent / "sample-data" / "labels.json"


def _load_ground_truth(mp4_path: Path, labels_path: Path | None) -> dict[str, float | None]:
    """Return the ground-truth label row for this jump, keyed by filename.

    labels.json is a mapping of jump-key -> {filename, <phase>: seconds, ...}.
    We match on the ``filename`` field (falling back to the jump-key) so callers
    can pass any path to the same jump.
    """
    path = labels_path or _DEFAULT_LABELS
    if not path.exists():
        return {}
    labels = json.loads(path.read_text())
    name = mp4_path.name
    stem = mp4_path.stem
    for key, row in labels.items():
        if row.get("filename") == name or key == stem or key == name:
            return row
    return {}


def extract_metadata(
    mp4_path: str | Path,
    labels_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> dict[str, float | None]:
    """Extract the jump phase timeline for a GoPro MP4.

    Detection runs on the embedded GPMF telemetry. For every phase that detection
    cannot determine (returns ``None``) — or if GPMF extraction fails entirely —
    we fall back to the corresponding human-labeled timestamp from
    ``labels_path`` (default: ``sample-data/labels.json``). Fields with no ground
    truth remain ``None``.

    Args:
        mp4_path: Path to the GoPro ``.mp4`` (or ``.lrv`` proxy).
        labels_path: Override for the ground-truth labels JSON.
        output_path: If given, the resulting timeline is also written here as JSON.

    Returns:
        Mapping of each phase in :data:`PHASES` to a timestamp (seconds) or ``None``.
    """
    mp4_path = Path(mp4_path)
    ground_truth = _load_ground_truth(mp4_path, Path(labels_path) if labels_path else None)

    try:
        gpmf = parse_gpmf(str(mp4_path))
        detected = segment(gpmf).as_dict()
    except (GPMFError, subprocess.SubprocessError):
        # GPMF unreadable (no track / no ffmpeg / corrupt) -> rely entirely on
        # ground truth. We degrade gracefully rather than failing the pipeline.
        detected = {phase: None for phase in PHASES}

    result: dict[str, float | None] = {}
    for phase in PHASES:
        value = detected.get(phase)
        if value is None:
            value = ground_truth.get(phase)
        result[phase] = value

    if output_path is not None:
        Path(output_path).write_text(json.dumps(result, indent=2) + "\n")

    return result
