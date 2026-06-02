"""Tests for the /render stage.

Layered like the rest of the suite so as much as possible runs everywhere:

* **Pure builder** tests assert the ``filter_complex`` *string* and input order
  with no FFmpeg at all (the graph is built by :mod:`render.builder` as pure data).
* **Caption** tests render the overlay PNG (needs only Pillow).
* The **end-to-end** test runs ``scripts/process_jump.py`` on a sample jump and
  checks the headline contract: a valid 1080p/h264/30fps MP4, under 100 MB,
  produced in under 60 s. It ``skip``s when FFmpeg or the sample MP4 is absent,
  matching the /analysis and /metadata tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from edl.schema import Clip, EditDecisionList, Transition
from render import FINAL_FILENAME, atempo_chain, build_filtergraph
from render.builder import OUT_FPS, OUT_HEIGHT, OUT_WIDTH
from render.caption import render_caption

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "sample-data"
# jump_003 is a 1080p master (no rescale needed) and the fastest sample to decode,
# keeping the end-to-end render well inside the 60 s budget. Its timeline lives in
# sample-data/labels.json (exit 57 ... landing_end 132).
SAMPLE_MP4 = SAMPLE_DIR / "jump_003.mp4"

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _require_ffmpeg() -> None:
    if not _HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")


@pytest.fixture
def edl() -> EditDecisionList:
    """A small but representative edit: a slow-mo exit, a beat, the canopy."""
    return EditDecisionList(
        clips=[
            Clip(src_start=27.0, src_end=30.0, speed_multiplier=0.4,
                 transition_in=Transition.fade),
            Clip(src_start=31.0, src_end=37.0),
            Clip(src_start=74.0, src_end=79.0, transition_out=Transition.crossfade),
        ],
        music="upbeat_indie",
    )


# --------------------------------------------------------------------------- #
# atempo_chain (pure).
# --------------------------------------------------------------------------- #

def test_atempo_chain_passthrough_for_real_time() -> None:
    assert atempo_chain(1.0) == []  # no filter needed at real time


def test_atempo_chain_decomposes_slowmo_into_valid_factors() -> None:
    factors = atempo_chain(0.4)  # below atempo's 0.5 floor -> must split
    assert factors  # non-empty
    product = 1.0
    for f in factors:
        assert 0.5 <= f <= 2.0
        product *= f
    assert product == pytest.approx(0.4)


def test_atempo_chain_decomposes_speedup() -> None:
    factors = atempo_chain(3.0)  # above the 2.0 ceiling
    product = 1.0
    for f in factors:
        assert 0.5 <= f <= 2.0
        product *= f
    assert product == pytest.approx(3.0)


def test_atempo_chain_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        atempo_chain(0.0)


# --------------------------------------------------------------------------- #
# build_filtergraph (pure — asserts the graph string + inputs).
# --------------------------------------------------------------------------- #

def test_graph_trims_speed_ramps_and_concats(edl: EditDecisionList) -> None:
    g = build_filtergraph(edl, "src.mp4", has_audio=False)
    fc = g.filter_complex
    # One trim + speed-ramp per clip.
    assert "trim=27:30" in fc and "trim=31:37" in fc and "trim=74:79" in fc
    assert "setpts=(PTS-STARTPTS)/0.4000" in fc  # the slow-mo ramp
    # The clips are concatenated into the single video out pad.
    assert f"concat=n=3:v=1:a=0[{g.video_label}]" in fc
    # Output normalised to the 1080p30 target.
    assert f"scale={OUT_WIDTH}:{OUT_HEIGHT}" in fc and f"fps={OUT_FPS}" in fc


def test_graph_includes_intro_outro_and_caption(edl: EditDecisionList) -> None:
    g = build_filtergraph(
        edl, "src.mp4", has_audio=False,
        intro_path="intro.mp4", intro_duration=2.0,
        outro_path="outro.mp4", outro_duration=2.0,
        caption_path="cap.png",
    )
    fc = g.filter_complex
    # Intro + 3 clips + outro -> 5 concatenated video segments.
    assert "concat=n=5:v=1:a=0" in fc
    assert "[vintro]" in fc and "[voutro]" in fc
    # The caption is overlaid onto the intro card.
    assert "overlay=0:0[vintro]" in fc
    # Inputs registered in order: source, intro, caption, outro.
    paths = [spec.path for spec in g.inputs]
    assert paths == ["src.mp4", "intro.mp4", "cap.png", "outro.mp4"]
    # The caption is held still over the intro's length.
    cap_spec = next(s for s in g.inputs if s.path == "cap.png")
    assert "-loop" in cap_spec.pre_args and "-t" in cap_spec.pre_args


def test_graph_ducks_music_under_source_audio(edl: EditDecisionList) -> None:
    g = build_filtergraph(
        edl, "src.mp4", has_audio=True, music_path="music.mp3",
        intro_path="intro.mp4", intro_duration=2.0,
    )
    fc = g.filter_complex
    # Ambient is built, split, and used to key the side-chain compressor...
    assert "sidechaincompress=" in fc
    assert "asplit=2[amb_mix][amb_key]" in fc
    # ...then mixed back with the ducked music into the final audio pad.
    assert "amix=inputs=2" in fc
    assert g.audio_label == "aout"
    # Ambient sits under the body, delayed by the intro length (2000 ms).
    assert "adelay=2000:all=1" in fc
    # The music input is looped to cover the whole timeline.
    music_spec = next(s for s in g.inputs if s.path == "music.mp3")
    assert music_spec.pre_args == ("-stream_loop", "-1")


def test_graph_music_plays_straight_without_source_audio(edl: EditDecisionList) -> None:
    """A silent source (the common GoPro case) -> music, no ducking."""
    g = build_filtergraph(edl, "src.mp4", has_audio=False, music_path="music.mp3")
    fc = g.filter_complex
    assert "sidechaincompress=" not in fc  # nothing to duck against
    assert g.audio_label == "mus"
    assert "volume=" in fc  # music level still applied


def test_graph_is_video_only_when_silent_and_music_free(edl: EditDecisionList) -> None:
    g = build_filtergraph(edl, "src.mp4", has_audio=False)
    assert g.audio_label is None  # no audio map at all


def test_graph_input_args_flatten_in_order(edl: EditDecisionList) -> None:
    g = build_filtergraph(edl, "src.mp4", has_audio=False, music_path="m.mp3")
    args = g.input_args()
    # Source first, then the looped music with its pre-flags.
    assert args[:2] == ["-i", "src.mp4"]
    assert "-stream_loop" in args and args[-1] == "m.mp3"


# --------------------------------------------------------------------------- #
# Caption (Pillow only).
# --------------------------------------------------------------------------- #

def test_render_caption_writes_png(tmp_path: Path) -> None:
    out = render_caption(
        tmp_path / "cap.png",
        customer_name="Jane Doe", jump_date="2026-06-02",
        width=1920, height=1080,
    )
    assert out.exists()
    # A real PNG header.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# End-to-end: process_jump.py on a sample -> valid MP4, < 100 MB, < 60 s.
# --------------------------------------------------------------------------- #

@pytest.fixture
def templates(tmp_path: Path) -> Path:
    """A throwaway /templates with a short intro, outro, and one music track."""
    _require_ffmpeg()
    root = tmp_path / "templates"
    (root / "music").mkdir(parents=True)
    for name, color in (("intro.mp4", "navy"), ("outro.mp4", "black")):
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:size=1280x720:duration=1.5:rate=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / name),
            ],
            check=True,
        )
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:a=0.2",
            str(root / "music" / "upbeat_indie.wav"),
        ],
        check=True,
    )
    return root


def test_process_jump_end_to_end(templates: Path, tmp_path: Path) -> None:
    """process_jump on a sample file -> a valid MP4 < 100 MB in < 60 s."""
    _require_ffmpeg()
    if not SAMPLE_MP4.exists():
        pytest.skip(f"sample {SAMPLE_MP4.name} not present in this checkout")

    from scripts.process_jump import process_jump

    jobs_root = tmp_path / "jobs"
    start = time.perf_counter()
    out = process_jump(
        SAMPLE_MP4,
        job_id="e2e-jump-003",
        customer_name="Jane Doe",
        jump_date="2026-06-02",
        jobs_root=jobs_root,
        templates_dir=templates,
        music="upbeat_indie",
        preset="ultrafast",  # the test only needs a valid file, not archival quality
    )
    elapsed = time.perf_counter() - start

    # Produced where we expect, and the EDL was persisted alongside it.
    assert out == jobs_root / "e2e-jump-003" / FINAL_FILENAME
    assert out.exists()
    assert (jobs_root / "e2e-jump-003" / "edl.json").exists()

    # Under the size budget.
    size_mb = out.stat().st_size / 1_000_000
    assert size_mb < 100, f"output is {size_mb:.1f} MB, over the 100 MB budget"

    # Under the time budget.
    assert elapsed < 60, f"render took {elapsed:.1f}s, over the 60 s budget"

    # A valid, probeable MP4 at the target geometry: 1080p, h264, 30 fps.
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate",
            "-of", "json",
            str(out),
        ],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "h264"
    assert (stream["width"], stream["height"]) == (OUT_WIDTH, OUT_HEIGHT)
    num, den = stream["r_frame_rate"].split("/")
    assert int(num) / int(den) == pytest.approx(OUT_FPS)
