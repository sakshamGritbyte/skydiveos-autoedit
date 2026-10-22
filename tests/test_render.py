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
import re
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
    assert "asplit=2[amb_mix][amb_key_raw]" in fc
    # The side-chain key is padded to the full timeline so the compressor (and thus
    # the music) runs the whole length instead of stopping with the footage.
    assert "[amb_key_raw]apad=whole_dur=" in fc
    # ...then mixed back with the ducked music into the final audio pad.
    assert "amix=inputs=2" in fc
    # The mix is padded to the full video length so audio_duration == video_duration
    # (a shorter audio track makes browsers drop audio after a seek).
    assert g.audio_label == "afull"
    assert "[aout]apad=whole_dur=" in fc
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
    assert g.audio_label == "afull"  # padded to the full timeline
    assert "volume=" in fc  # music level still applied


def test_graph_audio_is_padded_to_full_video_length(edl: EditDecisionList) -> None:
    """Every audio branch ends padded to the full timeline.

    Regression for the bug where the audio stream was shorter than the video
    (ambient ends with the footage; cards are silent), which made browsers drop
    audio after a timeline seek. The final pad keeps audio_duration == video.
    """
    for has_audio, music in [(True, "m.mp3"), (False, "m.mp3"), (True, None)]:
        g = build_filtergraph(
            edl, "src.mp4", has_audio=has_audio, music_path=music,
            outro_path="outro.mp4", outro_duration=3.0,
        )
        assert g.audio_label == "afull", (has_audio, music)
        assert "apad=whole_dur=" in g.filter_complex


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


# --------------------------------------------------------------------------- #
# The VBV cap (Bug 373) — one authority, three encoders.
# --------------------------------------------------------------------------- #

def test_deliverable_encode_carries_the_vbv_cap(edl: EditDecisionList) -> None:
    """CRF alone has no ceiling; the cap is what keeps high-motion freefall streamable."""
    from render.render import MAXRATE, VBV_BUFSIZE, _build_command

    g = build_filtergraph(edl, "src.mp4", has_audio=False, music_path=None)
    cmd = _build_command(g, Path("out.mp4"), preset="veryfast", crf=23)
    assert cmd[cmd.index("-maxrate") + 1] == MAXRATE
    assert cmd[cmd.index("-bufsize") + 1] == VBV_BUFSIZE
    # The quality TARGET is untouched — the cap clamps peaks, it does not lower CRF.
    assert cmd[cmd.index("-crf") + 1] == "23"
    assert "+faststart" in cmd


def test_vbv_cap_is_imported_not_repeated() -> None:
    """A re-calibration must be one edit: the other encoders import the numbers.

    Three literal copies of the cap is how the 2026-09-01 cut half-landed — the raw
    proxy had none at all. Source-pinned because the selfie encode is inline in a
    3000-line module with no seam to call it through.
    """
    from render.render import MAXRATE, VBV_BUFSIZE

    for rel in ("api/selfie.py", "api/rawproxy.py"):
        src = (REPO_ROOT / rel).read_text()
        assert "from render.render import" in src, rel
        for literal in (f'"{MAXRATE}"', f'"{VBV_BUFSIZE}"', '"12M"', '"24M"'):
            assert literal not in src, f"{rel} hardcodes {literal}; import render.render's cap"


# --------------------------------------------------------------------------- #
# The intro/outro cards play whole and WITHOUT music (client rule, 2026-10-22).
# --------------------------------------------------------------------------- #


def _music_chain(fc: str) -> str:
    return next(part for part in fc.split(";") if part.endswith("[mus]"))


def test_music_spans_the_footage_only(edl: EditDecisionList) -> None:
    """The music is cut to the footage, faded out at its end, and delayed past the intro."""
    g = build_filtergraph(
        edl, "src.mp4", has_audio=False, music_path="m.mp3",
        intro_path="intro.mp4", intro_duration=2.5,
        outro_path="outro.mp4", outro_duration=3.0,
    )
    footage = edl.output_duration
    chain = _music_chain(g.filter_complex)
    assert f"atrim=0:{round(footage, 3):g}," in chain
    # Not trimmed to the whole timeline (intro + footage + outro) any more.
    assert f"atrim=0:{round(2.5 + footage + 3.0, 3):g}," not in chain
    # Starts on the first footage frame, after the intro card...
    assert "adelay=2500:all=1" in chain
    # ...and has faded out completely by the outro card's first frame.
    assert f"afade=t=out:st={round(footage - 1.0, 3):g}:d=1" in chain
    assert chain.index("afade=") < chain.index("adelay=")  # fade on the footage axis
    # The cards still keep their full length: the audio is padded to the whole video.
    assert f"apad=whole_dur={round(2.5 + footage + 3.0, 3):g}" in g.filter_complex


def test_music_without_an_intro_starts_at_zero(edl: EditDecisionList) -> None:
    g = build_filtergraph(edl, "src.mp4", has_audio=True, music_path="m.mp3")
    chain = _music_chain(g.filter_complex)
    assert "adelay" not in chain
    assert "afade=t=out" in chain


def test_cards_are_never_trimmed(edl: EditDecisionList) -> None:
    """The intro/outro cards enter the concat whole — no trim, no setpts."""
    g = build_filtergraph(
        edl, "src.mp4", has_audio=True, music_path="m.mp3",
        intro_path="intro.mp4", intro_duration=2.0,
        outro_path="outro.mp4", outro_duration=2.0,
    )
    for part in g.filter_complex.split(";"):
        if part.endswith("[vintro]") or part.endswith("[voutro]"):
            assert "trim=" not in part and "setpts" not in part, part


def _silences(path: Path, band: str) -> list[tuple[float, float]]:
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af",
         f"{band},silencedetect=noise=-45dB:d=0.3", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return [(s, ends[i] if i < len(ends) else float("inf")) for i, s in enumerate(starts)]


def test_rendered_cards_have_no_music(tmp_path: Path) -> None:
    """Real FFmpeg: 2 s intro + 8 s footage + 2 s outro; a 3 kHz bed is silent over both cards."""
    _require_ffmpeg()
    from render.render import _build_command

    def lavfi(name: str, color: str, dur: float, tone: bool) -> Path:
        out = tmp_path / name
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
               "-i", f"color=c={color}:size=320x180:duration={dur}:rate=30"]
        if tone:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=44100:duration={dur}",
                    "-c:a", "aac", "-shortest"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
        subprocess.run(cmd, check=True)
        return out

    src = lavfi("src.mp4", "lime", 12, tone=True)
    intro = lavfi("intro.mp4", "navy", 2, tone=False)
    outro = lavfi("outro.mp4", "black", 2, tone=False)
    music = tmp_path / "music.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=3000:sample_rate=44100:duration=30", str(music)], check=True)

    cut = EditDecisionList(
        clips=[Clip(src_start=0.0, src_end=4.0), Clip(src_start=6.0, src_end=10.0)],
    )
    graph = build_filtergraph(
        cut, str(src), has_audio=True, music_path=str(music),
        intro_path=str(intro), intro_duration=2.0,
        outro_path=str(outro), outro_duration=2.0,
        width=320, height=180,
    )
    out = tmp_path / "final.mp4"
    subprocess.run(_build_command(graph, out, preset="ultrafast", crf=30), check=True)

    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert dur == pytest.approx(12.0, abs=0.15)  # cards kept their full length

    music_silences = _silences(out, "highpass=f=2000,highpass=f=2000")
    covers = lambda a, b: any(s <= a + 0.35 and e >= b - 0.35 for s, e in music_silences)  # noqa: E731
    assert covers(0.0, 2.0), music_silences       # no music over the intro card
    assert covers(10.0, 12.0), music_silences     # none over the outro card
    # ...and the music plays across the footage in between.
    assert not any(s < 8.5 and e > 2.5 for s, e in music_silences), music_silences
