"""Bug 441 — the filmed intro/outro "bookends" play WHOLE and WITHOUT music.

Handcam/selfie jobs go through the Claude editor, which cut the customer's
intro interview and post-landing outro into "best-scored" fragments; the house
cut (external) kept them whole only while the source fit its 210 s budget. The
client's rule: the first and last filmed clips are never trimmed and play with
their own sound, no music. Enforced in code, at the two places it can be:

* ``edl.validate.validate_and_repair`` (every persist site) guarantees ONE
  real-time clip spanning the first intro file / last outro file in
  ``full_video``, removing the fragments that overlapped it;
* ``render_selfie_video`` keeps those clips' original audio and gates the music
  off over them (through the logo card when the outro ends the edit).

Highlights deliberately keep their short intro/outro beats (a <60 s cut).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from api import selfie
from edl.validate import BOOKEND_DELIVERABLES, bookend_ranges, validate_and_repair

_FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


def _scene(clips: list[dict[str, Any]], name: str) -> list[tuple[float, float, float]]:
    return [
        (
            round(float(c["src_start"]), 3),
            round(float(c["src_end"]), 3),
            float(c.get("speed_multiplier", 1.0)),
        )
        for c in clips
        if c["scene"] == name
    ]


# --------------------------------------------------------------------------- #
# The ranges
# --------------------------------------------------------------------------- #


def test_bookends_are_the_first_intro_file_and_the_last_outro_file() -> None:
    manifest = {
        "scenes": [
            {
                "name": "intro_interview",
                "duration": 50.0,
                "file_offsets": [
                    {"file": "GX01.MP4", "offset": 0.0},
                    {"file": "GX02.MP4", "offset": 31.5},
                ],
            },
            {"name": "freefall", "duration": 60.0},
            {
                "name": "outro_interview",
                "duration": 40.0,
                "file_offsets": [
                    {"file": "GX08.MP4", "offset": 0.0},
                    {"file": "GX09.MP4", "offset": 22.0},
                ],
            },
        ]
    }
    assert bookend_ranges(manifest) == [
        {"scene": "intro_interview", "camera": None, "src_start": 0.0, "src_end": 31.5},
        {"scene": "outro_interview", "camera": None, "src_start": 22.0, "src_end": 40.0},
    ]


def test_a_single_file_or_offset_less_scene_is_whole() -> None:
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")  # no file_offsets
    assert bookend_ranges(manifest) == [
        {"scene": "intro_interview", "camera": None, "src_start": 0.0, "src_end": 69.97},
        {"scene": "outro_interview", "camera": None, "src_start": 0.0, "src_end": 21.221},
    ]


def test_no_interview_scenes_means_no_bookends() -> None:
    assert bookend_ranges({"scenes": [{"name": "freefall", "duration": 60.0}]}) == []


def test_multicam_takes_one_camera_instructor_first() -> None:
    by_cam = {
        "external": {"scenes": [{"name": "intro_interview", "duration": 30.0}]},
        "instructor": {"scenes": [{"name": "intro_interview", "duration": 20.0}]},
    }
    assert bookend_ranges({}, by_cam, multicam=True) == [
        {"scene": "intro_interview", "camera": "instructor", "src_start": 0.0, "src_end": 20.0},
    ]
    # Ultimate: usually only the external camera filmed the interviews.
    only_ext = {
        "external": by_cam["external"],
        "instructor": {"scenes": [{"name": "freefall", "duration": 9}]},
    }
    assert bookend_ranges({}, only_ext, multicam=True)[0]["camera"] == "external"


# --------------------------------------------------------------------------- #
# The validator — the reported job, reproduced from its real EDL
# --------------------------------------------------------------------------- #


def test_reported_selfie_job_gets_its_intro_and_outro_back_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import edl.validate as validate_mod

    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    edl = _fx("selfie_d15c2e42/edl_full.json")
    # The bug, as delivered: intro fragments [0,10] [19,24] [51,54]; outro head cut.
    assert _scene(edl, "intro_interview") == [
        (0.0, 10.0, 1.0),
        (19.0, 24.0, 1.0),
        (51.0, 54.0, 1.0),
    ]

    clips, log = validate_and_repair(edl, "full_video", manifest)

    assert _scene(clips, "intro_interview") == [(0.0, 69.97, 1.0)]
    assert _scene(clips, "outro_interview") == [(0.0, 21.221, 1.0)]
    assert any("intro bookend whole" in line for line in log)
    assert any("outro bookend whole" in line for line in log)
    # Chronological: intro first, outro last.
    assert clips[0]["scene"] == "intro_interview"
    assert clips[-1]["scene"] == "outro_interview"
    # Every OTHER clip is exactly what the validator produced before this rule.
    monkeypatch.setattr(validate_mod, "BOOKEND_DELIVERABLES", frozenset())
    baseline, _ = validate_mod.validate_and_repair(edl, "full_video", manifest)

    def _not_interview(cs: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [
            (c["scene"], c["src_start"], c["src_end"], c.get("speed_multiplier"))
            for c in cs
            if c["scene"] not in ("intro_interview", "outro_interview")
        ]

    assert _not_interview(clips) == _not_interview(baseline)


def test_external_job_already_whole_is_untouched() -> None:
    # The external house cut that the client called correct: no bookend repair at all.
    manifest = _fx("external_21cdb2c5/scene_manifest.json")
    edl = _fx("external_21cdb2c5/edl_full.json")
    clips, log = validate_and_repair(edl, "full_video", manifest)
    assert not any("bookend" in line for line in log)
    before, _ = validate_and_repair(edl, "full_video", manifest)
    assert clips == before


def test_highlights_keep_their_short_beats() -> None:
    assert BOOKEND_DELIVERABLES == frozenset({"full_video"})
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    edl = _fx("selfie_d15c2e42/edl_highlights.json")
    _, log = validate_and_repair(edl, "highlights", manifest)
    assert not any("bookend" in line for line in log)


def test_the_bookend_is_never_duplicated_and_neighbours_outside_it_survive() -> None:
    manifest = {
        "scenes": [
            {
                "name": "intro_interview",
                "duration": 40.0,
                "file_offsets": [
                    {"file": "A.MP4", "offset": 0.0},
                    {"file": "B.MP4", "offset": 25.0},
                ],
            },
            {"name": "freefall", "duration": 60.0},
        ]
    }
    edl = [
        {
            "scene": "intro_interview",
            "src_start": 2.0,
            "src_end": 6.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
        {
            "scene": "intro_interview",
            "src_start": 20.0,
            "src_end": 30.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
        {
            "scene": "intro_interview",
            "src_start": 10.0,
            "src_end": 12.0,
            "speed_multiplier": 0.4,
            "camera": None,
        },
        {
            "scene": "freefall",
            "src_start": 5.0,
            "src_end": 15.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
    ]
    clips, _ = validate_and_repair(edl, "full_video", manifest)
    # First FILE [0, 25) whole at 1x, then only the part of [20,30] past the file boundary.
    assert _scene(clips, "intro_interview") == [(0.0, 25.0, 1.0), (25.0, 30.0, 1.0)]
    # No slow-mo fragment inside the bookend survives (it would replay the same seconds).
    assert all(
        not (c["scene"] == "intro_interview" and c["speed_multiplier"] != 1.0) for c in clips
    )


def test_an_exact_bookend_is_left_alone_and_logged_nothing() -> None:
    manifest = {
        "scenes": [
            {"name": "outro_interview", "duration": 12.0},
            {"name": "freefall", "duration": 30.0},
        ]
    }
    edl = [
        {
            "scene": "freefall",
            "src_start": 1.0,
            "src_end": 9.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
        {
            "scene": "outro_interview",
            "src_start": 0.0,
            "src_end": 12.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
    ]
    clips, log = validate_and_repair(copy.deepcopy(edl), "full_video", manifest)
    assert not any("bookend" in line for line in log)
    assert _scene(clips, "outro_interview") == [(0.0, 12.0, 1.0)]


def test_a_missing_outro_is_not_invented() -> None:
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 8.0},
            {"name": "freefall", "duration": 30.0},
        ]
    }
    edl = [
        {
            "scene": "freefall",
            "src_start": 1.0,
            "src_end": 9.0,
            "speed_multiplier": 1.0,
            "camera": None,
        }
    ]
    clips, _ = validate_and_repair(edl, "full_video", manifest)
    assert _scene(clips, "outro_interview") == []
    assert _scene(clips, "intro_interview") == [(0.0, 8.0, 1.0)]
    assert clips[0]["scene"] == "intro_interview"


def test_ultimum_combo_tags_the_bookend_with_its_camera() -> None:
    by_cam = {
        "external": {
            "scenes": [
                {"name": "intro_interview", "duration": 49.333},
                {"name": "freefall", "duration": 60.0},
                {"name": "outro_interview", "duration": 17.301},
            ]
        },
        "instructor": {
            "scenes": [{"name": "freefall", "duration": 60.0}, {"name": "canopy", "duration": 30.0}]
        },
    }
    edl = [
        {
            "scene": "intro_interview",
            "src_start": 0.0,
            "src_end": 44.14,
            "speed_multiplier": 1.0,
            "camera": "external",
        },
        {
            "scene": "freefall",
            "src_start": 10.0,
            "src_end": 20.0,
            "speed_multiplier": 1.0,
            "camera": "instructor",
        },
        {
            "scene": "outro_interview",
            "src_start": 0.0,
            "src_end": 15.48,
            "speed_multiplier": 1.0,
            "camera": "external",
        },
    ]
    clips, _ = validate_and_repair(
        edl, "full_video", by_cam["instructor"], manifest_by_camera=by_cam
    )
    intro = [c for c in clips if c["scene"] == "intro_interview"]
    outro = [c for c in clips if c["scene"] == "outro_interview"]
    assert [(c["camera"], c["src_start"], c["src_end"]) for c in intro] == [
        ("external", 0.0, 49.333)
    ]
    assert [(c["camera"], c["src_start"], c["src_end"]) for c in outro] == [
        ("external", 0.0, 17.301)
    ]


# --------------------------------------------------------------------------- #
# The render — original audio, no music, over the bookends
# --------------------------------------------------------------------------- #


def _render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clips: list[selfie.Clip], **kw: Any
) -> str:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(selfie, "_run_ffmpeg", lambda cmd: captured.setdefault("cmd", cmd))
    monkeypatch.setattr(selfie, "scene_has_audio", lambda p: True)
    monkeypatch.setattr(selfie, "_card_input", lambda *a, **k: str(tmp_path / "_outro.mp4"))
    scene_paths = {c.scene: f"/x/{c.scene}.mp4" for c in clips}
    selfie.render_selfie_video(
        tmp_path / "out.mp4",
        clips,
        scene_paths,
        booking={},
        music_path="/x/music.mp3",
        music_only=False,
        **kw,
    )
    cmd = captured["cmd"]
    return cmd[cmd.index("-filter_complex") + 1]


_CLIPS = [
    selfie.Clip(scene="intro_interview", src_start=0.0, src_end=10.0),
    selfie.Clip(scene="boarding", src_start=0.0, src_end=4.0),
    selfie.Clip(scene="freefall", src_start=10.0, src_end=20.0),
    selfie.Clip(scene="outro_interview", src_start=0.0, src_end=6.0),
]
_BOOKENDS = [
    {"scene": "intro_interview", "camera": None, "src_start": 0.0, "src_end": 10.0},
    {"scene": "outro_interview", "camera": None, "src_start": 0.0, "src_end": 6.0},
]


def test_bookends_keep_their_own_sound_and_silence_the_music(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fc = _render(tmp_path, monkeypatch, _CLIPS, bookends=_BOOKENDS)
    # Original audio for both bookends (inputs 0 and 3)…
    assert "[0:a]atrim=0.000:10.000" in fc
    assert "[3:a]atrim=0.000:6.000" in fc
    # …and the music gated off over them: intro [0,10), outro [24,30) extended
    # through the 2.5 s logo card to the end (32.5) so it doesn't pop back in.
    assert "between(t,0.000,10.000)" in fc
    assert "between(t,24.000,32.500)" in fc
    assert "[musg]" in fc and "[amb][musg]amix" in fc


def test_without_bookends_the_mix_is_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _render(tmp_path, monkeypatch, _CLIPS)
    assert "between(t," not in before
    assert "[amb][mus]amix" in before
    again = _render(tmp_path / "b", monkeypatch, _CLIPS, bookends=None)
    assert again == before.replace(str(tmp_path), str(tmp_path / "b"))


def test_a_slow_mo_clip_is_never_a_bookend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clips = [selfie.Clip(scene="intro_interview", src_start=0.0, src_end=4.0, speed_multiplier=0.4)]
    fc = _render(tmp_path, monkeypatch, clips, bookends=[_BOOKENDS[0]])
    assert "[0:a]atrim" not in fc  # no stretched audio, ever
    assert "between(t," not in fc


def test_render_outputs_passes_bookends_to_full_video_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_render(out: Path, clips: Any, scene_paths: Any, **kw: Any) -> Path:
        seen[Path(out).name] = kw.get("bookends")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"x")
        return Path(out)

    monkeypatch.setattr(selfie, "render_selfie_video", fake_render)
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 9.0, "combined_path": "/x/i.mp4"},
            {"name": "freefall", "duration": 30.0, "combined_path": "/x/f.mp4"},
        ]
    }
    one = [selfie.Clip(scene="freefall", src_start=0.0, src_end=5.0)]
    edls = selfie.EDLResponse(full_video=one, highlights=one, freefall=one)
    selfie.render_outputs("job1", edls, manifest, {}, tmp_path)
    assert seen["full_video.mp4"] == [
        {"scene": "intro_interview", "camera": None, "src_start": 0.0, "src_end": 9.0}
    ]
    assert seen["highlights.mp4"] is None
    assert seen["freefall.mp4"] is None


def test_the_compose_prompt_asks_for_whole_bookends() -> None:
    assert "NEVER trim the customer's filmed intro or outro" in selfie._COMPOSE_RULES
