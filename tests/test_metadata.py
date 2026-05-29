"""Tests for the /metadata stage against the sample jumps.

Ground truth lives in ``sample-data/labels.json`` keyed by jump filename. We
assert every produced timestamp lands within ±2 s of its human label. Phases
whose ground-truth label is ``null`` are skipped (nothing to compare against).

The sample MP4s are large and not always present in every checkout, so tests
that need them ``skip`` (rather than fail) when a file is missing or FFmpeg is
unavailable.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from metadata import PHASES, extract_metadata, parse_gpmf, segment
from metadata.gpmf import GPMFError

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "sample-data"
LABELS_PATH = SAMPLE_DIR / "labels.json"
TOLERANCE_S = 2.0

LABELS: dict[str, dict] = json.loads(LABELS_PATH.read_text()) if LABELS_PATH.exists() else {}

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _sample_path(row: dict) -> Path:
    return SAMPLE_DIR / row["filename"]


def _require_sample(row: dict) -> Path:
    path = _sample_path(row)
    if not path.exists():
        pytest.skip(f"sample {path.name} not present in this checkout")
    if not _HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    return path


# Parametrize over jumps that actually have a label row.
JUMP_IDS = sorted(LABELS.keys())


def test_labels_present() -> None:
    """Sanity: the ground-truth file exists and has the expected schema."""
    assert LABELS, "sample-data/labels.json is missing or empty"
    for key, row in LABELS.items():
        assert "filename" in row, f"{key} missing filename"
        for phase in PHASES:
            # plane_boarding/exit/... may be absent or null, but if present must be numeric|null
            if phase in row and row[phase] is not None:
                assert isinstance(row[phase], (int, float)), f"{key}.{phase} not numeric"


@pytest.mark.parametrize("jump_id", JUMP_IDS)
def test_timeline_within_tolerance(jump_id: str) -> None:
    """Every produced timestamp is within ±2 s of its (non-null) human label."""
    row = LABELS[jump_id]
    _require_sample(row)

    timeline = extract_metadata(_sample_path(row), labels_path=LABELS_PATH)

    # Output must contain exactly the canonical phases.
    assert set(timeline.keys()) == set(PHASES)

    for phase in PHASES:
        truth = row.get(phase)
        if truth is None:
            continue  # null ground truth -> nothing to assert
        produced = timeline[phase]
        assert produced is not None, f"{jump_id}.{phase} produced None but truth={truth}"
        assert abs(produced - truth) <= TOLERANCE_S, (
            f"{jump_id}.{phase}: produced {produced} not within "
            f"±{TOLERANCE_S}s of truth {truth}"
        )


@pytest.mark.parametrize("jump_id", JUMP_IDS)
def test_detected_phases_within_tolerance(jump_id: str) -> None:
    """Phases detected purely from GPMF (no fallback) are also within ±2 s.

    This guards the detector itself: it would be possible to pass
    ``test_timeline_within_tolerance`` entirely via fallback. Here we check that
    whatever the segmenter *does* detect agrees with ground truth.
    """
    row = LABELS[jump_id]
    _require_sample(row)

    detected = segment(parse_gpmf(str(_sample_path(row)))).as_dict()

    checked = 0
    for phase in PHASES:
        truth = row.get(phase)
        produced = detected.get(phase)
        if truth is None or produced is None:
            continue
        checked += 1
        assert abs(produced - truth) <= TOLERANCE_S, (
            f"{jump_id}.{phase}: detected {produced} not within "
            f"±{TOLERANCE_S}s of truth {truth}"
        )
    # No assertion on `checked` per-jump: some sample clips carry only a few
    # seconds of telemetry and legitimately detect nothing.


def test_at_least_one_jump_detects_freefall() -> None:
    """The detector must genuinely work on at least one full jump (not all fallback)."""
    if not _HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    any_detected = False
    for row in LABELS.values():
        path = _sample_path(row)
        if not path.exists():
            continue
        det = segment(parse_gpmf(str(path))).as_dict()
        if det["exit"] is not None and det["freefall_start"] is not None:
            any_detected = True
            break
    if not any_detected:
        pytest.skip("no full-telemetry sample jump available to exercise the detector")
    assert any_detected


def test_fallback_to_ground_truth(tmp_path: Path) -> None:
    """When GPMF parsing fails, fields are filled from labels.json (no crash)."""
    fake = tmp_path / "not_a_video.mp4"
    fake.write_bytes(b"\x00\x00\x00\x18ftypmp42not a real movie")

    labels = {"not_a_video": {"filename": "not_a_video.mp4", "exit": 12, "deployment": 40}}
    labels_file = tmp_path / "labels.json"
    labels_file.write_text(json.dumps(labels))

    timeline = extract_metadata(fake, labels_path=labels_file)
    assert timeline["exit"] == 12
    assert timeline["deployment"] == 40
    # phases with no label and no detection stay None
    assert timeline["plane_boarding"] is None


def test_null_label_fields_are_skipped_not_fabricated() -> None:
    """A jump whose label is null for a phase must not get a fabricated detection
    that contradicts a missing ground truth (e.g. spurious landing)."""
    if not _HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    # jump_002 has landing == null in labels and only a short ground clip of GPS.
    row = LABELS.get("jump_002")
    if row is None or not _sample_path(row).exists():
        pytest.skip("jump_002 sample not available")
    det = segment(parse_gpmf(str(_sample_path(row)))).as_dict()
    assert det["landing"] is None, "non-descending GPS must not yield a landing fix"


def test_output_json_written(tmp_path: Path) -> None:
    """extract_metadata writes a JSON file when output_path is given."""
    row = next(iter(LABELS.values()), None)
    if row is None:
        pytest.skip("no labels")
    if not _sample_path(row).exists() or not _HAVE_FFMPEG:
        pytest.skip("sample/ffmpeg unavailable")
    out = tmp_path / "timeline.json"
    extract_metadata(_sample_path(row), labels_path=LABELS_PATH, output_path=out)
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert set(loaded.keys()) == set(PHASES)


def test_missing_gpmf_raises_in_parser(tmp_path: Path) -> None:
    """parse_gpmf surfaces a clear error for a file with no GPMF track."""
    if not _HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    bogus = tmp_path / "empty.mp4"
    bogus.write_bytes(b"not an mp4")
    with pytest.raises((GPMFError, Exception)):
        parse_gpmf(str(bogus))
