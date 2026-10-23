"""The freefall cut puts the aircraft exit on the music's beat drop (client rule 2026-10-22).

"The freefall clip should never be cut up in the middle. It may only be shortened at the
beginning, so that the moment of exit from the plane lines up with the beat drop in the
music." Covered here:

* :mod:`render.beatdrop` — the drop detector (synthetic tracks), the sidecar override,
  the cache, and the two pure planning functions (lead before the exit, music offset).
* :mod:`api.selfie` — the lead picked at compose, the exit's output time, the music
  offset the renderer applies, and ``exclude.json`` trimming only the HEAD of a
  freefall cut (never splitting it).
* A REAL ffmpeg render: the output's drop is measured and lands on the exit.

The whole-freefall continuity itself is :mod:`edl.validate`'s rule
(``tests/test_freefall_uncut.py``); this file only checks that the lead it is handed
reaches the persisted EDL.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from api import selfie
from api.selfie import Clip, EDLResponse
from render import beatdrop
from render.beatdrop import (
    ANALYSIS_RATE,
    FREEFALL_MAX_LEAD_S,
    beat_drop_for,
    find_beat_drop,
    music_offset_for,
    plan_exit_lead,
    sidecar_path,
)

RATE = ANALYSIS_RATE


def _tone(freq: float, seconds: float, amp: float, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _build_then_drop(build_s: float, after_s: float, rate: int = RATE) -> np.ndarray:
    """A quiet high-pitched build, then the kick + bass come in at ``build_s``."""
    build = _tone(2000, build_s, 0.05, rate)
    drop = _tone(60, after_s, 0.6, rate) + _tone(2000, after_s, 0.05, rate)
    return np.concatenate([build, drop])


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("build_s", [6.0, 12.0, 31.5])
def test_detects_the_drop_where_the_bass_comes_in(build_s: float) -> None:
    drop = find_beat_drop(_build_then_drop(build_s, 30.0))
    assert drop is not None
    assert abs(drop - build_s) <= 0.1


def test_steady_track_has_no_drop() -> None:
    assert find_beat_drop(_tone(60, 40.0, 0.5) + _tone(2000, 40.0, 0.05)) is None


def test_fade_in_from_silence_is_not_a_drop() -> None:
    # The silent head of a file followed by steady music: the "before" window holds no
    # music, so the rise is the track starting, not a drop.
    track = np.concatenate([np.zeros(5 * RATE, np.float32), _tone(60, 30.0, 0.5)])
    assert find_beat_drop(track) is None


def test_earliest_comparable_drop_wins() -> None:
    # Two drops of similar size: the first one belongs on the exit.
    first = _build_then_drop(10.0, 15.0)            # drop at 10
    second = _build_then_drop(10.0, 15.0)           # drop at 25 + 10 = 35
    drop = find_beat_drop(np.concatenate([first, second]))
    assert drop is not None and abs(drop - 10.0) <= 0.1


def test_too_short_to_judge() -> None:
    assert find_beat_drop(_tone(60, 3.0, 0.5)) is None


def test_drop_beyond_the_search_window_is_ignored() -> None:
    assert find_beat_drop(_build_then_drop(130.0, 10.0)) is None


# --------------------------------------------------------------------------- #
# Sidecar override + cache + never-raise
# --------------------------------------------------------------------------- #


def _track(tmp_path: Path, name: str = "song.mp3") -> Path:
    p = tmp_path / name
    p.write_bytes(b"not really audio")
    return p


def test_sidecar_pins_the_drop_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = _track(tmp_path)
    sidecar_path(track).write_text(json.dumps({"beat_drop": 31.2}))
    monkeypatch.setattr(beatdrop, "decode_mono", lambda p: pytest.fail("must not decode"))
    assert sidecar_path(track).name == "song.json"
    assert beat_drop_for(track) == 31.2


def test_sidecar_null_means_no_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track = _track(tmp_path, "rock.mp3")
    sidecar_path(track).write_text(json.dumps({"beat_drop": None}))
    monkeypatch.setattr(beatdrop, "decode_mono", lambda p: pytest.fail("must not decode"))
    assert beat_drop_for(track) is None


@pytest.mark.parametrize(
    "content", ["{not json", json.dumps({"other": 1}), json.dumps({"beat_drop": "soon"}),
                json.dumps({"beat_drop": -3})],
)
def test_bad_sidecar_falls_back_to_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    track = _track(tmp_path, f"t{abs(hash(content))}.mp3")
    sidecar_path(track).write_text(content)
    monkeypatch.setattr(beatdrop, "decode_mono", lambda p: _build_then_drop(8.0, 20.0))
    drop = beat_drop_for(track)
    assert drop is not None and abs(drop - 8.0) <= 0.1


def test_detection_is_cached_until_the_track_or_sidecar_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = _track(tmp_path, "cached.mp3")
    calls: list[str] = []

    def fake_decode(p: str) -> np.ndarray:
        calls.append(p)
        return _build_then_drop(9.0, 20.0)

    monkeypatch.setattr(beatdrop, "decode_mono", fake_decode)
    first = beat_drop_for(track)
    assert beat_drop_for(track) == first
    assert len(calls) == 1                       # compose + render decode once
    sidecar_path(track).write_text(json.dumps({"beat_drop": 2.5}))
    assert beat_drop_for(track) == 2.5           # an operator's pin is picked up


def test_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert beat_drop_for(None) is None
    assert beat_drop_for(tmp_path / "missing.mp3") is None
    track = _track(tmp_path, "broken.mp3")

    def boom(p: str) -> np.ndarray:
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(beatdrop, "decode_mono", boom)
    assert beat_drop_for(track) is None


# --------------------------------------------------------------------------- #
# Planning: lead before the exit, and the music offset
# --------------------------------------------------------------------------- #


def test_lead_is_the_drop_when_the_footage_allows() -> None:
    assert plan_exit_lead(6.5, available_lead=20.0, max_lead=FREEFALL_MAX_LEAD_S) == 6.5


def test_lead_is_bounded_by_footage_and_the_cap() -> None:
    assert plan_exit_lead(12.0, available_lead=4.0, max_lead=15.0) == 4.0
    assert plan_exit_lead(40.0, available_lead=60.0, max_lead=15.0) == 15.0


@pytest.mark.parametrize("drop", [None, 0.0, -1.0])
def test_no_drop_means_the_cut_starts_at_the_exit(drop: float | None) -> None:
    assert plan_exit_lead(drop, available_lead=20.0, max_lead=15.0) == 0.0


def test_default_cap_is_fifteen_seconds_of_plane() -> None:
    # QA's expectation (bug reopen 2026-10-23): at most 15 s of plane before the exit.
    # A later drop still lands on the exit — the render enters the track mid-way.
    assert FREEFALL_MAX_LEAD_S == 15.0
    assert plan_exit_lead(28.4, available_lead=60.0, max_lead=FREEFALL_MAX_LEAD_S) == 15.0
    assert music_offset_for(28.4, 15.0) == pytest.approx(13.4)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10", 10.0),
        ("0", 0.0),            # 0 = never lead in: the cut starts at the exit
        ("12.5", 12.5),
        ("", FREEFALL_MAX_LEAD_S),
        ("plenty", FREEFALL_MAX_LEAD_S),
        ("-5", FREEFALL_MAX_LEAD_S),
        ("nan", FREEFALL_MAX_LEAD_S),
    ],
)
def test_lead_cap_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("FREEFALL_MAX_LEAD_S", raw)
    assert beatdrop.freefall_max_lead_s() == expected
    monkeypatch.delenv("FREEFALL_MAX_LEAD_S")
    assert beatdrop.freefall_max_lead_s() == FREEFALL_MAX_LEAD_S


def test_env_cap_bounds_the_composed_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 28.4)
    assert selfie._freefall_lead(_scenes(exit_off=40.0), "/m.mp3") == FREEFALL_MAX_LEAD_S
    monkeypatch.setenv("FREEFALL_MAX_LEAD_S", "30")
    assert selfie._freefall_lead(_scenes(exit_off=40.0), "/m.mp3") == 28.4
    monkeypatch.setenv("FREEFALL_MAX_LEAD_S", "10")
    assert selfie._freefall_lead(_scenes(exit_off=40.0), "/m.mp3") == 10.0
    monkeypatch.setenv("FREEFALL_MAX_LEAD_S", "0")
    assert selfie._freefall_lead(_scenes(exit_off=40.0), "/m.mp3") == 0.0


def test_music_offset_puts_the_drop_on_the_exit() -> None:
    assert music_offset_for(40.0, 15.0) == 25.0   # skip the track's head
    assert music_offset_for(6.0, 6.0) == 0.0      # lead == drop: play from the top
    assert music_offset_for(4.0, 9.0) == -5.0     # hand-lengthened lead: start later
    assert music_offset_for(None, 9.0) == 0.0
    assert music_offset_for(4.0, None) == 0.0


# --------------------------------------------------------------------------- #
# api.selfie wiring
# --------------------------------------------------------------------------- #


def _scenes(
    exit_off: float = 20.0, deploy: float = 70.0, dur: float = 90.0
) -> list[dict[str, Any]]:
    return [
        {
            "name": "freefall", "duration": dur, "combined_path": "/x/ff.mp4",
            "exit_offset": exit_off, "deploy_offset": deploy,
        }
    ]


def test_freefall_lead_follows_the_track(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 7.25)
    assert selfie._freefall_lead(_scenes(), "/m.mp3") == 7.25
    assert selfie._freefall_lead(_scenes(exit_off=3.0), "/m.mp3") == 3.0   # footage-bound
    assert selfie._freefall_lead(_scenes(), None) == 0.0                   # no music
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 60.0)
    assert selfie._freefall_lead(_scenes(exit_off=90.0), "/m.mp3") == FREEFALL_MAX_LEAD_S


def test_freefall_lead_needs_a_detected_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 7.0)
    scenes = [{"name": "freefall", "duration": 60.0, "combined_path": "/x/ff.mp4"}]
    assert selfie._freefall_lead(scenes, "/m.mp3") == 0.0
    assert selfie._freefall_lead([], "/m.mp3") == 0.0


def test_exit_output_time_counts_everything_before_it() -> None:
    clips = [
        Clip(scene="freefall", src_start=12.0, src_end=20.0),
        Clip(scene="freefall", src_start=20.0, src_end=21.0, speed_multiplier=0.4),
        Clip(scene="freefall", src_start=21.0, src_end=73.0),
    ]
    assert selfie._exit_output_time(clips, 20.0) == pytest.approx(8.0)
    assert selfie._exit_output_time(clips, 16.0) == pytest.approx(4.0)
    # Exit inside a slow-mo clip: its source seconds play 2.5x longer.
    assert selfie._exit_output_time(clips, 20.5) == pytest.approx(8.0 + 0.5 / 0.4)
    # Head trimmed past the exit: nothing to sync to.
    assert selfie._exit_output_time(clips[2:], 20.0) is None


def test_freefall_music_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 30.0)
    clips = [Clip(scene="freefall", src_start=5.0, src_end=73.0)]   # 15 s lead
    assert selfie._freefall_music_offset(clips, _scenes(), "/m.mp3") == 15.0
    assert selfie._freefall_music_offset(clips, _scenes(), None) == 0.0
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: None)
    assert selfie._freefall_music_offset(clips, _scenes(), "/m.mp3") == 0.0


# --------------------------------------------------------------------------- #
# exclude.json: a freefall cut is only ever shortened at its start
# --------------------------------------------------------------------------- #


def _ff(a: float, b: float, **kw: Any) -> Clip:
    return Clip(scene="freefall", src_start=a, src_end=b, **kw)


def test_head_exclusion_trims_the_start() -> None:
    out = selfie.exclude_freefall_head([_ff(10.0, 73.0)], {"freefall": [(0.0, 14.0)]})
    assert [(c.src_start, c.src_end) for c in out] == [(14.0, 73.0)]


def test_chained_head_exclusions_and_swallowed_clips() -> None:
    clips = [_ff(10.0, 12.0), _ff(12.0, 13.0, speed_multiplier=0.4), _ff(13.0, 73.0)]
    out = selfie.exclude_freefall_head(clips, {"freefall": [(9.0, 12.5), (12.5, 15.0)]})
    assert [(c.src_start, c.src_end, c.speed_multiplier) for c in out] == [(15.0, 73.0, 1.0)]


def test_middle_and_tail_exclusions_never_split_the_jump(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clips = [_ff(10.0, 73.0, camera="external")]
    out = selfie.exclude_freefall_head(clips, {"freefall": [(30.0, 35.0), (70.0, 80.0)]})
    assert out == clips
    assert "only ever shortened at its start" in caplog.text


def test_head_exclusion_never_empties_the_cut() -> None:
    clips = [_ff(10.0, 73.0)]
    assert selfie.exclude_freefall_head(clips, {"freefall": [(0.0, 100.0)]}) == clips


def test_head_trim_keeps_the_camera_tag() -> None:
    out = selfie.exclude_freefall_head([_ff(10.0, 73.0, camera="instructor")],
                                       {"freefall": [(5.0, 11.0)]})
    assert out[0].camera == "instructor" and out[0].src_start == 11.0


def test_apply_exclusions_splits_other_cuts_but_only_trims_freefall() -> None:
    clips = [_ff(10.0, 73.0)]
    edls = EDLResponse(full_video=clips, highlights=clips, freefall=clips)
    out = selfie.apply_exclusions(edls, {"freefall": [(10.0, 12.0), (30.0, 35.0)]})
    assert [(c.src_start, c.src_end) for c in out.full_video] == [(12.0, 30.0), (35.0, 73.0)]
    assert [(c.src_start, c.src_end) for c in out.freefall] == [(12.0, 73.0)]


# --------------------------------------------------------------------------- #
# Render: the music offset in the filter graph
# --------------------------------------------------------------------------- #


def _render_fc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> str:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(selfie, "_run_ffmpeg", lambda cmd: captured.setdefault("cmd", cmd))
    monkeypatch.setattr(selfie, "scene_has_audio", lambda p: False)
    monkeypatch.setattr(selfie, "_card_input", lambda *a, **k: str(tmp_path / "_outro.mp4"))
    selfie.render_selfie_video(
        tmp_path / "out.mp4", [_ff(5.0, 25.0)], {"freefall": "/x/f.mp4"},
        booking={}, music_path="/x/m.mp3", music_only=True, **kw,
    )
    cmd = captured["cmd"]
    return cmd[cmd.index("-filter_complex") + 1]


def test_no_offset_is_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fc = _render_fc(tmp_path, monkeypatch)
    assert "[2:a]atrim=0:22.500,asetpts=PTS-STARTPTS," in fc
    assert "adelay" not in fc
    assert fc == _render_fc(tmp_path, monkeypatch, music_offset=0.0)


def test_positive_offset_enters_the_track_mid_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fc = _render_fc(tmp_path, monkeypatch, music_offset=12.5)
    assert "[2:a]atrim=12.500:35.000,asetpts=PTS-STARTPTS," in fc
    assert "adelay" not in fc and "apad=whole_dur=22.500" in fc


def test_negative_offset_starts_the_music_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fc = _render_fc(tmp_path, monkeypatch, music_offset=-3.0)
    assert "[2:a]atrim=0.000:19.500,asetpts=PTS-STARTPTS," in fc
    assert "adelay=3000:all=1,apad=whole_dur=22.500" in fc


def test_render_outputs_syncs_only_the_freefall_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, float] = {}

    def fake_render(out: Path, clips: Any, scene_paths: Any, **kw: Any) -> Path:
        seen[Path(out).stem] = kw.get("music_offset", 0.0)
        return Path(out)

    monkeypatch.setattr(selfie, "render_selfie_video", fake_render)
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 30.0)
    clips = [_ff(10.0, 73.0)]          # exit (20) plays at 10 s
    edls = EDLResponse(full_video=clips, highlights=clips, freefall=clips)
    manifest = {"scenes": _scenes()}
    selfie.render_outputs(
        "job1", edls, manifest, {}, tmp_path,
        music_paths={"full_video": "/m.mp3", "highlights": "/m.mp3", "freefall": "/m.mp3"},
    )
    assert seen == {"full_video": 0.0, "highlights": 0.0, "freefall": 20.0}


# --------------------------------------------------------------------------- #
# Compose: the lead reaches the validator, and the persisted freefall EDL
# --------------------------------------------------------------------------- #


def test_compose_hands_the_lead_to_the_freefall_validation_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leads: dict[str, float] = {}
    real = selfie._validated

    def spy(clips: Any, deliverable: str, manifest: Any, mbc: Any = None, **kw: Any) -> Any:
        leads[deliverable] = kw.get("freefall_lead", 0.0)
        return real(clips, deliverable, manifest, mbc, **kw)

    monkeypatch.setattr(selfie, "_validated", spy)
    monkeypatch.setattr(selfie, "beat_drop_for", lambda p: 6.0)
    manifest = {"scenes": _scenes(), "flagged": []}
    scores = {"freefall": [{"ts": float(t), "smile": 0.5, "eye_contact": 0.5,
                            "face_in_frame": 1.0, "face_centered": 0.5} for t in range(90)]}
    edls = selfie.compose_edls(
        scores, manifest, {}, "leadjob", tmp_path, use_ai=False, freefall_music="/m.mp3",
    )
    assert leads == {"full_video": 0.0, "highlights": 0.0, "freefall": 6.0}
    ff = [c for c in edls.freefall if c.scene == "freefall"]
    assert ff[0].src_start == pytest.approx(14.0)              # exit 20 - lead 6
    persisted = json.loads((tmp_path / "leadjob" / "edl_freefall.json").read_text())
    assert persisted[0]["src_start"] == pytest.approx(14.0)
    # ... and the render then plays the track from its top: drop 6 == exit at 6 s.
    assert selfie._freefall_music_offset(edls.freefall, manifest["scenes"], "/m.mp3") == 0.0


# --------------------------------------------------------------------------- #
# A real render: the output's drop lands on the exit
# --------------------------------------------------------------------------- #

_needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _first_bass_onset(path: Path) -> float:
    """Output-time seconds where the 60 Hz bass first comes in and STAYS (half-way level).

    0.19 s frames, so a single encoder click at the very start can't pass for the drop;
    the frame's centre is the reported time.
    """
    x = beatdrop.decode_mono(path)
    frame, hop = 2048, 128
    starts = range(0, len(x) - frame, hop)
    win = np.hanning(frame)
    freqs = np.fft.rfftfreq(frame, 1.0 / RATE)
    bass = np.array([
        10 * np.log10((np.abs(np.fft.rfft(x[i:i + frame] * win)) ** 2)[freqs < 150].sum() + 1e-10)
        for i in starts
    ])
    mid = (np.percentile(bass, 5) + np.percentile(bass, 95)) / 2
    above = bass > mid
    sustain = 8
    for k in range(len(above) - sustain):
        if above[k: k + sustain].all():
            return (k * hop + frame / 2) / RATE
    return float("nan")


@_needs_ffmpeg
@pytest.mark.parametrize(
    ("build_s", "lead"),
    [
        (4.0, 4.0),    # the drop fits the lead: the music plays from its top
        (9.0, 3.0),    # little door footage: the track is entered 6 s in
    ],
)
def test_real_render_lands_the_drop_on_the_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_s: float, lead: float
) -> None:
    exit_off = 5.0
    track = tmp_path / "track.wav"
    _write_wav(track, _build_then_drop(build_s, 8.0, 44100), 44100)   # short: forces a loop
    assert abs((beat_drop_for(track) or 0) - build_s) <= 0.1

    scene = tmp_path / "freefall.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=320x180:rate=30:duration=20", "-pix_fmt", "yuv420p", str(scene)],
        check=True,
    )
    scenes = [{"name": "freefall", "duration": 20.0, "combined_path": str(scene),
               "exit_offset": exit_off, "deploy_offset": 15.0}]
    clips = [_ff(exit_off - lead, 18.0)]
    offset = selfie._freefall_music_offset(clips, scenes, str(track))
    assert offset == pytest.approx(build_s - lead, abs=0.11)

    out = tmp_path / "freefall_out.mp4"
    selfie.render_selfie_video(
        out, clips, {"freefall": str(scene)}, booking={}, music_path=str(track),
        music_only=True, music_offset=offset, width=320, height=180,
    )
    assert abs(_first_bass_onset(out) - lead) <= 0.15
    # The looped track keeps playing after its end (12 s or 17 s of track, 15.5 s of edit).
    tail = beatdrop.decode_mono(out)[int(14.0 * RATE): int(15.0 * RATE)]
    assert float(np.abs(tail).max()) > 0.005


_LIBRARY = Path(__file__).resolve().parent.parent / "templates" / "music"
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
_LIBRARY_TRACKS = sorted(
    p for p in (_LIBRARY.glob("*") if _LIBRARY.is_dir() else ())
    if p.suffix.lower() in _AUDIO_SUFFIXES
)


@_needs_ffmpeg
@pytest.mark.skipif(not _LIBRARY_TRACKS, reason="no tracks in templates/music")
@pytest.mark.parametrize("track", _LIBRARY_TRACKS, ids=lambda p: p.stem)
def test_every_shipped_track_has_a_drop_to_sync_to(track: Path) -> None:
    """Every library track must yield a drop (detected, or pinned by a sidecar) —
    otherwise the freefall cut silently starts at the exit with the music from 0, and
    "the music didn't hit on the exit" is the first anyone hears of it. A track that
    truly has no drop must say so with ``{"beat_drop": null}`` so this skips on purpose.
    Measured 2026-10-23: Fly Away 12.2 s, KsMyHt 11.6 s, Free Fallin' 10.0 s,
    sample2 28.4 s (past the 15 s cap: its cut enters the track 13.4 s in)."""
    found, pinned = beatdrop._read_sidecar(track)
    if found and pinned is None:
        pytest.skip(f"{track.name}: pinned to no drop")
    drop = beat_drop_for(track)
    assert drop is not None and 0.0 < drop <= beatdrop._SEARCH_MAX_S, (
        f"{track.name}: no beat drop found — pin one in {sidecar_path(track).name}"
    )


# --------------------------------------------------------------------------- #
# edl.validate: the lead widens ONLY a freefall-only cut's window
# --------------------------------------------------------------------------- #


def _vmanifest() -> dict[str, Any]:
    return {"scenes": _scenes(exit_off=20.0, deploy=70.0, dur=90.0)}


@pytest.mark.parametrize("deliverable", ["freefall", "external_freefall", "chute_libre_selfie"])
def test_validator_keeps_the_lead_and_fills_the_jump_whole(deliverable: str) -> None:
    from edl.validate import validate_and_repair

    chopped = [
        {"scene": "freefall", "src_start": 10.0, "src_end": 25.0, "speed_multiplier": 1.0},
        {"scene": "freefall", "src_start": 40.0, "src_end": 44.0, "speed_multiplier": 1.0},
    ]
    clips, log = validate_and_repair(chopped, deliverable, _vmanifest(), freefall_lead=6.0)
    ff = [c for c in clips if c["scene"] == "freefall"]
    assert ff[0]["src_start"] == pytest.approx(14.0)        # exit 20 - lead 6, no earlier
    assert ff[-1]["src_end"] == pytest.approx(73.0)         # deploy 70 + 3: the natural end
    for a, b in zip(ff, ff[1:], strict=False):              # nothing skipped in between
        assert a["src_end"] == pytest.approx(b["src_start"])


def test_validator_default_lead_is_the_exit() -> None:
    from edl.validate import validate_and_repair

    clip = [{"scene": "freefall", "src_start": 10.0, "src_end": 73.0, "speed_multiplier": 1.0}]
    clips, _ = validate_and_repair(clip, "freefall", _vmanifest())
    assert clips[0]["src_start"] == pytest.approx(20.0)


def test_validator_ignores_the_lead_for_story_cuts() -> None:
    from edl.validate import validate_and_repair

    clip = [{"scene": "freefall", "src_start": 10.0, "src_end": 73.0, "speed_multiplier": 1.0}]
    clips, _ = validate_and_repair(clip, "full_video", _vmanifest(), freefall_lead=6.0)
    ff = [c for c in clips if c["scene"] == "freefall"]
    assert ff[0]["src_start"] == pytest.approx(20.0)
