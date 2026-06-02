"""Tests for the /analysis stage.

Layered so that as much as possible runs everywhere:

* **Pure-math** tests of the scoring helpers always run (no ffmpeg/model/samples).
* **Synthetic** tests build a tiny face-free ``.lrv`` with ffmpeg's ``testsrc`` and
  need only ffmpeg + the FaceLandmarker model.
* **Real-footage** tests build a small proxy from a sample jump's freefall window
  and additionally need the sample MP4 present.

Anything whose prerequisite is missing (ffmpeg, the downloadable model, or the
sample media) ``skip``s rather than fails — matching the /metadata tests, since
the large sample MP4s aren't in every checkout and CI may be offline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from analysis import SCORE_FIELDS, AnalysisError, resolve_model, score_freefall
from analysis.score import (
    _face_centered,
    _face_in_frame,
    _gaze_centered,
    _head_frontality,
    _smile,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "sample-data"
SAMPLE_MP4 = SAMPLE_DIR / "jump_001.mp4"
# jump_001 freefall is 27..76 (see sample-data/labels.json); take a full 60 s
# window starting at the freefall start for the proxy/timing fixtures.
PROXY_SRC_START = 27.0
PROXY_WINDOW_S = 60.0

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _require_ffmpeg() -> None:
    if not _HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")


def _require_model() -> None:
    try:
        resolve_model()
    except AnalysisError as e:
        pytest.skip(f"FaceLandmarker model unavailable (offline?): {e}")


def _build_proxy(out: Path, *, src: Path, start: float, dur: float, width: int = 480) -> None:
    """Re-encode a window of ``src`` into a small H.264 ``.lrv`` proxy.

    The proxy's internal timeline starts at 0, standing in for a freefall clip.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
            "-i", str(src),
            "-vf", f"scale={width}:-2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            "-f", "mp4", str(out),
        ],
        check=True,
    )


@pytest.fixture(scope="session")
def blank_proxy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An 8 s face-free proxy (ffmpeg ``testsrc``); needs only ffmpeg."""
    _require_ffmpeg()
    out = tmp_path_factory.mktemp("blank") / "blank.lrv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=8:size=320x180:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mp4", str(out),
        ],
        check=True,
    )
    return out


@pytest.fixture(scope="session")
def freefall_proxy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 60 s proxy built from the jump_001 freefall window; needs the sample MP4."""
    _require_ffmpeg()
    if not SAMPLE_MP4.exists():
        pytest.skip(f"sample {SAMPLE_MP4.name} not present in this checkout")
    out = tmp_path_factory.mktemp("freefall") / "jump_001.lrv"
    _build_proxy(out, src=SAMPLE_MP4, start=PROXY_SRC_START, dur=PROXY_WINDOW_S)
    return out


# --------------------------------------------------------------------------- #
# Pure-math tests (no external deps).
# --------------------------------------------------------------------------- #

def test_smile_is_mean_of_blendshapes() -> None:
    assert _smile({"mouthSmileLeft": 0.8, "mouthSmileRight": 0.6}) == pytest.approx(0.7)
    assert _smile({}) == 0.0  # no blendshapes -> no smile


def test_face_centered_peaks_at_frame_center() -> None:
    centered = _face_centered((0.4, 0.4, 0.6, 0.6))  # bbox centre == (0.5, 0.5)
    corner = _face_centered((0.0, 0.0, 0.1, 0.1))  # bbox centre near a corner
    assert centered == pytest.approx(1.0)
    assert 0.0 <= corner < centered


def test_face_in_frame_fraction() -> None:
    assert _face_in_frame((0.25, 0.25, 0.75, 0.75)) == pytest.approx(1.0)  # fully inside
    # half the bbox width sits off the left edge -> half in frame
    assert _face_in_frame((-0.5, 0.0, 0.5, 1.0)) == pytest.approx(0.5)
    assert _face_in_frame((2.0, 2.0, 3.0, 3.0)) == 0.0  # entirely off-frame


def test_gaze_and_frontality_defaults() -> None:
    assert _gaze_centered({}) == pytest.approx(1.0)  # no deviation -> dead ahead
    assert _head_frontality(None) == pytest.approx(1.0)  # no pose info -> not penalised


def test_all_score_fields_in_unit_range_helpers() -> None:
    # extreme/garbage inputs must still clamp into [0, 1]
    assert _face_centered((5.0, 5.0, 6.0, 6.0)) == 0.0
    assert _smile({"mouthSmileLeft": 5.0, "mouthSmileRight": 5.0}) == 1.0


# --------------------------------------------------------------------------- #
# Guard / degenerate-input tests (no model needed).
# --------------------------------------------------------------------------- #

def test_proxy_guard_rejects_full_res(tmp_path: Path) -> None:
    """A non-.lrv input is refused unless allow_full_res is set."""
    fake_master = tmp_path / "jump.mp4"
    fake_master.write_bytes(b"\x00\x00\x00\x18ftypmp42not a real movie")
    with pytest.raises(AnalysisError, match="non-proxy"):
        score_freefall(fake_master, 0.0, 5.0)


def test_empty_window_returns_empty(tmp_path: Path) -> None:
    """A non-positive window short-circuits to [] without loading the model."""
    proxy = tmp_path / "jump.lrv"  # need not even exist for a degenerate window
    assert score_freefall(proxy, 10.0, 5.0) == []
    assert score_freefall(proxy, 7.0, 7.0) == []


# --------------------------------------------------------------------------- #
# Synthetic-footage tests (ffmpeg + model).
# --------------------------------------------------------------------------- #

def test_schema_and_unit_range(blank_proxy: Path) -> None:
    """Output is one well-formed, in-range row per second."""
    _require_model()
    rows = score_freefall(blank_proxy, 0.0, 5.0)

    assert isinstance(rows, list) and rows, "expected per-second rows"
    timestamps = [r["ts"] for r in rows]
    assert timestamps == sorted(timestamps), "rows must be sorted by ts"
    assert len(rows) == 5  # one row per second of a 5 s window

    for row in rows:
        assert set(row) == {"ts", *SCORE_FIELDS}
        assert float(row["ts"]).is_integer()
        for field in SCORE_FIELDS:
            assert 0.0 <= row[field] <= 1.0, f"{field}={row[field]} out of [0,1]"


def test_no_faces_score_zero(blank_proxy: Path) -> None:
    """A face-free clip scores 0 on every signal (detector found nothing)."""
    _require_model()
    rows = score_freefall(blank_proxy, 0.0, 5.0)
    for row in rows:
        for field in SCORE_FIELDS:
            assert row[field] == 0.0


def test_ts_maps_to_source_timeline(blank_proxy: Path) -> None:
    """Row ts reflects the source-timeline window, not proxy-local 0."""
    _require_model()
    rows = score_freefall(blank_proxy, 2.0, 6.0)  # 4 s window starting at t=2
    assert [r["ts"] for r in rows] == [2.0, 3.0, 4.0, 5.0]


def test_output_json_written(blank_proxy: Path, tmp_path: Path) -> None:
    _require_model()
    out = tmp_path / "scores.json"
    rows = score_freefall(blank_proxy, 0.0, 3.0, output_path=out)
    assert out.exists()
    assert json.loads(out.read_text()) == rows


# --------------------------------------------------------------------------- #
# Real-footage tests (sample MP4 + ffmpeg + model).
# --------------------------------------------------------------------------- #

def test_real_freefall_schema_and_range(freefall_proxy: Path) -> None:
    _require_model()
    rows = score_freefall(freefall_proxy, 0.0, PROXY_WINDOW_S)
    assert rows, "expected scored rows from real freefall footage"
    assert len(rows) == int(PROXY_WINDOW_S)
    for row in rows:
        assert set(row) == {"ts", *SCORE_FIELDS}
        for field in SCORE_FIELDS:
            assert 0.0 <= row[field] <= 1.0


def test_real_freefall_detects_a_face(freefall_proxy: Path) -> None:
    """The detector genuinely fires on real footage (not all-zero like blank)."""
    _require_model()
    rows = score_freefall(freefall_proxy, 0.0, PROXY_WINDOW_S)
    assert max(r["face_in_frame"] for r in rows) > 0.0, "no face detected in real freefall"


def test_timing_budget_under_10s(freefall_proxy: Path) -> None:
    """Scoring a 60 s freefall on the proxy stays under the 10 s CPU budget."""
    _require_model()
    start = time.perf_counter()
    rows = score_freefall(freefall_proxy, 0.0, PROXY_WINDOW_S)
    elapsed = time.perf_counter() - start
    assert rows
    assert elapsed < 10.0, f"scoring took {elapsed:.1f}s, over the 10s budget"
