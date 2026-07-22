"""Tests for the deterministic EDL post-validation + repair layer (edl/validate.py)
and the scene-assembly changes that back it (api/selfie.build_scenes).

Fixtures are the real EDL + scene-manifest JSON captured from three processed jobs
(``tests/fixtures/``): an external single-cam job (21cdb2c5), a selfie single-cam job
(d15c2e42, an older manifest without ``file_offsets``), and an Ultimate dual-cam job
(0823a77a). Each reproduces a real compose failure the validator must repair.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from api import selfie
from api.selfie import FileSignals, GpmfSignals
from edl.validate import validate_and_repair

_FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


def _out_dur(clip: dict[str, Any]) -> float:
    return (clip["src_end"] - clip["src_start"]) / clip["speed_multiplier"]


def _covers(clips: list[dict[str, Any]], scene: str, lo: float, hi: float) -> bool:
    return any(
        c["scene"] == scene and c["src_start"] < hi and c["src_end"] > lo for c in clips
    )


# --------------------------------------------------------------------------- #
# Freefall-type deliverables: deploy beat + no trailing scenes + window clamp
# --------------------------------------------------------------------------- #


def test_deploy_beat_injected() -> None:
    # External job: freefall scene deploy_offset = 75.08, but the EDL stops at 60.0 —
    # the deployment beat is missing and must be injected at [D-1, D+2] @ 0.4x.
    manifest = _fx("external_21cdb2c5/scene_manifest.json")
    edl = _fx("external_21cdb2c5/edl_freefall.json")
    assert max(c["src_end"] for c in edl if c["scene"] == "freefall") < 75.08

    out, log = validate_and_repair(edl, "freefall", manifest)
    assert _covers(out, "freefall", 74.08, 77.08)
    beat = out[-1]  # chronologically last
    assert beat["scene"] == "freefall" and beat["speed_multiplier"] == 0.4
    assert beat["src_start"] == pytest.approx(74.08)
    assert any("injected deployment beat" in line for line in log)

    # Ultimate external cameraman freefall cut: deploy_offset = 97.1, EDL stops at 62.0.
    ext_manifest = _fx("ultimum_0823a77a/scene_manifest_external.json")
    ext_edl = _fx("ultimum_0823a77a/edl_external_freefall.json")
    out2, _ = validate_and_repair(ext_edl, "external_freefall", ext_manifest)
    assert _covers(out2, "freefall", 96.1, 99.1)


def test_freefall_no_trailing_scenes() -> None:
    manifest = _fx("external_21cdb2c5/scene_manifest.json")  # E=25.02, D=75.08
    out, log = validate_and_repair(_fx("external_21cdb2c5/edl_freefall.json"), "freefall", manifest)
    assert {c["scene"] for c in out} == {"freefall"}  # trailing canopy clips dropped
    for c in out:  # every clip inside [E-8, D+3] = [17.02, 78.08]
        assert c["src_start"] >= 17.02 - 1e-6
        assert c["src_end"] <= 78.08 + 1e-6
    assert any("non-freefall scene" in line for line in log)

    # The chute-libre deliverable string routes through the SAME freefall-type rules
    # (regression: the persist site uses the "chute_libre_selfie" key, not "chute_libre").
    instr_manifest = _fx("ultimum_0823a77a/scene_manifest_instructor.json")  # E=28.03, D=79.08
    chute = _fx("ultimum_0823a77a/edl_chute_libre.json")
    assert any(c["scene"] == "canopy" for c in chute)  # the fixture has trailing canopy
    out2, _ = validate_and_repair(chute, "chute_libre_selfie", instr_manifest)
    assert {c["scene"] for c in out2} == {"freefall"}
    assert out2[0]["src_start"] >= 20.03 - 1e-6  # leading clip kept at/after E-8


def test_story_cut_freefall_window_clamped() -> None:
    # Regression: full_video / highlights (story cuts) also carry freefall footage, so
    # the aerial window [E-0, D+3] must clamp their freefall clips too — pre-exit aircraft
    # and post-opening scenery bled into full_video (job dc26cb4d). Non-freefall clips in
    # a story cut pass through untouched.
    manifest = _fx("external_21cdb2c5/scene_manifest.json")  # E=25.02, D=75.08 -> [25.02, 78.08]
    edl = [
        # non-freefall story clip: must be left alone (no freefall anchors to clamp to).
        {"scene": "boarding", "src_start": 0.0, "src_end": 4.0,
         "speed_multiplier": 1.0, "camera": None},
        # pre-exit aircraft footage -> dropped (entirely before exit).
        {"scene": "freefall", "src_start": 0.0, "src_end": 25.02,
         "speed_multiplier": 1.0, "camera": None},
        # post-opening scenery -> clamped down to the deploy tail D+3.
        {"scene": "freefall", "src_start": 68.07, "src_end": 82.549,
         "speed_multiplier": 1.0, "camera": None},
    ]
    out, log = validate_and_repair(edl, "full_video", manifest)

    freefall = [c for c in out if c["scene"] == "freefall"]
    for c in freefall:  # every freefall clip inside [E-0, D+3] = [25.02, 78.08]
        assert c["src_start"] >= 25.02 - 1e-6
        assert c["src_end"] <= 78.08 + 1e-6
    # The pre-exit clip is dropped entirely; the late scenery's tail is clamped to D+3.
    assert not any(float(c["src_start"]) < 25.02 for c in freefall)
    assert any(c["src_end"] == pytest.approx(78.08) for c in freefall)
    # The boarding (non-freefall) clip survives unchanged.
    assert {"scene": "boarding", "src_start": 0.0, "src_end": 4.0,
            "speed_multiplier": 1.0, "camera": None} in out
    assert any("freefall window" in line for line in log)


# --------------------------------------------------------------------------- #
# Story deliverables: boarding + intro
# --------------------------------------------------------------------------- #


def test_boarding_and_intro_required_in_highlights() -> None:
    # Give boarding file_offsets (three source files) and drop the boarding/intro clips.
    manifest = copy.deepcopy(_fx("selfie_d15c2e42/scene_manifest.json"))
    for s in manifest["scenes"]:
        if s["name"] == "boarding":
            s["file_offsets"] = [
                {"file": "GX010054.MP4", "offset": 0.0},
                {"file": "GX010055.MP4", "offset": 13.4},
                {"file": "GX010056.MP4", "offset": 26.8},
            ]
    highlights = [
        {"scene": "freefall", "src_start": 30.03, "src_end": 40.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 60.06, "src_end": 61.06,
         "speed_multiplier": 0.4, "camera": None},
    ]
    out, log = validate_and_repair(highlights, "highlights", manifest)
    boarding = [c for c in out if c["scene"] == "boarding"]
    # First-file window [0, 13.4) and the last third both covered.
    assert any(c["src_start"] < 13.4 for c in boarding)
    assert any(c["src_end"] > manifest_dur(manifest, "boarding") * 2 / 3 for c in boarding)
    # Intro injected at the head (position 0).
    assert out[0]["scene"] == "intro_interview" and out[0]["src_start"] == 0.0
    assert sum(1 for line in log if "boarding" in line) == 2
    assert any("intro" in line for line in log)

    # Counter-check: a compliant highlights EDL yields no boarding/intro repairs.
    compliant = [
        {"scene": "intro_interview", "src_start": 0.0, "src_end": 8.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "boarding", "src_start": 0.0, "src_end": 4.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "boarding", "src_start": 30.0, "src_end": 33.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 30.03, "src_end": 40.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 60.06, "src_end": 61.06,
         "speed_multiplier": 0.4, "camera": None},
    ]
    _, log2 = validate_and_repair(compliant, "highlights", manifest)
    assert not any("boarding" in line or "intro" in line for line in log2)


def test_boarding_fallback_without_file_offsets() -> None:
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")  # real: no file_offsets
    assert all("file_offsets" not in s for s in manifest["scenes"])
    highlights = [
        {"scene": "intro_interview", "src_start": 0.0, "src_end": 8.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "boarding", "src_start": 33.0, "src_end": 36.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 30.03, "src_end": 40.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 60.06, "src_end": 61.06,
         "speed_multiplier": 0.4, "camera": None},
    ]
    out, log = validate_and_repair(highlights, "highlights", manifest)
    boarding = [c for c in out if c["scene"] == "boarding"]
    assert any(c["src_start"] < 5.0 for c in boarding)  # a head clip was injected
    assert sum(c["src_end"] - c["src_start"] for c in boarding) >= 6.0
    assert any("degraded check" in line for line in log)

    # A compliant fallback EDL (head clip + >=6s coverage) triggers no boarding repair.
    compliant = [
        {"scene": "boarding", "src_start": 0.0, "src_end": 6.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "boarding", "src_start": 33.0, "src_end": 36.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 30.03, "src_end": 40.0,
         "speed_multiplier": 1.0, "camera": None},
        {"scene": "freefall", "src_start": 60.06, "src_end": 61.06,
         "speed_multiplier": 0.4, "camera": None},
    ]
    _, log2 = validate_and_repair(compliant, "highlights", manifest)
    assert not any("boarding" in line for line in log2)


def test_highlights_intro_entry_beat_injected_multicam() -> None:
    # intro spans two files: interview (0.0) + aircraft entry (40.924). Highlights that only
    # has the head interview clip must gain the entry beat at the second file's offset.
    manifest_ext = {
        "scenes": [
            {
                "name": "intro_interview",
                "duration": 50.802,
                "file_offsets": [
                    {"file": "A.MP4", "offset": 0.0},
                    {"file": "B.MP4", "offset": 40.924},
                ],
            },
            {"name": "freefall", "exit_offset": 27.03, "deploy_offset": 91.09,
             "duration": 112.0},
        ]
    }
    manifest = {"scenes": manifest_ext["scenes"]}
    edl = [
        {"scene": "intro_interview", "src_start": 0.0, "src_end": 8.0,
         "speed_multiplier": 1.0, "camera": "external"},
    ]
    repaired, log = validate_and_repair(
        edl, "highlights", manifest,
        manifest_by_camera={"external": manifest_ext, "instructor": manifest_ext},
    )
    intro_clips = [c for c in repaired if c["scene"] == "intro_interview"]
    # head (0-8) kept AND an entry clip covering 40.924 added
    assert any(c["src_start"] <= 40.924 <= c["src_end"] for c in intro_clips), intro_clips
    assert any("aircraft entry" in line for line in log)


def test_highlights_intro_single_file_no_entry_beat() -> None:
    # intro is one continuous file (no separate entry) -> no entry beat injected.
    manifest = {"scenes": [{"name": "intro_interview", "duration": 20.0,
                            "file_offsets": [{"file": "A.MP4", "offset": 0.0}]}]}
    edl = [{"scene": "intro_interview", "src_start": 0.0, "src_end": 8.0,
            "speed_multiplier": 1.0, "camera": None}]
    repaired, log = validate_and_repair(edl, "highlights", manifest)
    assert not any("aircraft entry" in line for line in log)


# --------------------------------------------------------------------------- #
# Ultimate multi-cam: dedupe, order, min-shot, switch-rate
# --------------------------------------------------------------------------- #


def _mbc() -> dict[str, dict[str, Any]]:
    return {
        "instructor": _fx("ultimum_0823a77a/scene_manifest_instructor.json"),
        "external": _fx("ultimum_0823a77a/scene_manifest_external.json"),
    }


def test_ultimum_dedupe_and_order() -> None:
    mbc = _mbc()
    edl = _fx("ultimum_0823a77a/edl_highlights.json")
    # The fixture has literal duplicate instructor freefall ranges.
    dup = [c for c in edl if c.get("camera") == "instructor"
           and c["scene"] == "freefall" and c["src_start"] == 42.5]
    assert len(dup) == 2

    out, _ = validate_and_repair(edl, "highlights", mbc["instructor"], manifest_by_camera=mbc)
    kept = [c for c in out if c.get("camera") == "instructor"
            and c["scene"] == "freefall" and abs(c["src_start"] - 42.5) < 1e-6]
    assert len(kept) == 1  # de-duplicated

    # Chronological per (camera, scene).
    groups: dict[Any, list[float]] = {}
    for c in out:
        groups.setdefault((c.get("camera"), c["scene"]), []).append(c["src_start"])
    for starts in groups.values():
        assert starts == sorted(starts)

    # Both cameras still feature in the freefall block (interleave preserved).
    assert {c.get("camera") for c in out if c["scene"] == "freefall"} == {"instructor", "external"}


def test_ultimum_min_shot_and_switch_rate() -> None:
    mbc = _mbc()
    edl = _fx("ultimum_0823a77a/edl_full.json")
    # The input source ranges, keyed by (camera, scene), for the "no retiming" check.
    original: dict[Any, list[tuple[float, float]]] = {}
    for c in edl:
        original.setdefault((c.get("camera"), c["scene"]), []).append(
            (c["src_start"], c["src_end"])
        )

    out, log = validate_and_repair(edl, "full_video", mbc["instructor"], manifest_by_camera=mbc)

    # (a) Every shot is either a slow-mo beat or >= 1.5s on screen.
    for c in out:
        assert c["speed_multiplier"] == 0.4 or _out_dur(c) >= 1.5 - 1e-6

    # (b) >= 3s of output between camera switches, except where a switch was logged.
    allowed_early = sum("allowed early camera switch" in line for line in log)
    t = 0.0
    last_cam = None
    last_switch = float("-inf")
    early = 0
    for c in out:
        cam = c.get("camera")
        if last_cam is not None and cam != last_cam:
            if t - last_switch < 3.0 - 1e-6:
                early += 1
            last_switch = t
        last_cam = cam
        t += _out_dur(c)
    assert early <= allowed_early

    # (c) No source range was retimed: every kept clip is either an input range / a merge
    #     of contiguous input ranges (boundaries match originals), or a freefall-window
    #     clamp — a sub-range trimmed inside a single original range of the same
    #     (camera, scene). A genuinely shifted clip lands outside its originals and fails
    #     both checks.
    for c in out:
        key = (c.get("camera"), c["scene"])
        ranges = original.get(key, [])
        starts = {round(s, 6) for s, _ in ranges}
        ends = {round(e, 6) for _, e in ranges}
        exact_or_merge = (
            round(c["src_start"], 6) in starts and round(c["src_end"], 6) in ends
        )
        clamped_subrange = any(
            os - 1e-6 <= c["src_start"] and c["src_end"] <= oe + 1e-6 for os, oe in ranges
        )
        assert exact_or_merge or clamped_subrange


# --------------------------------------------------------------------------- #
# Noop on a compliant EDL
# --------------------------------------------------------------------------- #


def test_noop_on_compliant_edl() -> None:
    # d15c2e42's freefall EDL is already compliant: in [E-8, D+3], deploy beat present,
    # no foreign scenes, chronological, with intentional slow-mo sub-windows that must
    # NOT be treated as duplicates.
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    edl = _fx("selfie_d15c2e42/edl_freefall.json")
    out, log = validate_and_repair(edl, "freefall", manifest)
    assert log == []
    assert out == edl


# --------------------------------------------------------------------------- #
# Scene-assembly: canopy -> landing rename + file_offsets (api/selfie.build_scenes)
# --------------------------------------------------------------------------- #


def _sig(
    *,
    accl_z_mean: float = 1.0,
    filename: str = "GH010001.MP4",
    chapter: int = 1,
    duration: float = 10.0,
) -> FileSignals:
    return FileSignals(
        filename=filename,
        path=f"/raw/{filename}",
        chapter=chapter,
        duration=duration,
        gpmf=GpmfSignals(
            altitude_mean=0.0,
            altitude_first=0.0,
            altitude_last=0.0,
            altitude_delta=0.0,
            accl_z_mean=accl_z_mean,
            accl_z_std=0.1,
            accl_mag_mean=1.0,
            accl_mag_std=0.1,
            accl_mag_min=1.0,
            has_altitude=False,
        ),
    )


def _stub_scene_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_concat(source_paths: Any, out_path: Any) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"scene")
        return out

    monkeypatch.setattr(selfie, "concat_scene", fake_concat)
    monkeypatch.setattr(selfie, "detect_exit_offset", lambda src: 5.0)
    monkeypatch.setattr(
        selfie, "detect_deploy_offset", lambda srcs, exit_offset=0.0: 40.0
    )


def test_landing_rename_and_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_scene_assembly(monkeypatch)
    classified = [
        ("boarding", _sig(filename="GH010001.MP4", duration=30.0, accl_z_mean=1.0)),
        ("boarding", _sig(filename="GH020001.MP4", duration=15.0, accl_z_mean=1.0)),
        ("freefall", _sig(filename="GH010002.MP4", duration=50.0, accl_z_mean=0.3)),
        # A post-freefall "canopy" scene with the landing accelerometer signature.
        ("canopy", _sig(filename="GH010003.MP4", duration=8.0, accl_z_mean=2.08)),
        ("outro_interview", _sig(filename="GH010004.MP4", duration=10.0, accl_z_mean=-0.9)),
    ]
    manifest = selfie.build_scenes("landjob", classified, tmp_path)
    names = {s["name"] for s in manifest["scenes"]}
    assert "landing" in names and "canopy" not in names
    assert manifest["flagged"] == ["auto-renamed canopy->landing (accl signature)"]
    landing = next(s for s in manifest["scenes"] if s["name"] == "landing")
    assert landing["combined_path"].endswith("landing.mp4")
    # file_offsets are recorded (cumulative) on every scene.
    boarding = next(s for s in manifest["scenes"] if s["name"] == "boarding")
    assert boarding["file_offsets"] == [
        {"file": "GH010001.MP4", "offset": 0.0},
        {"file": "GH020001.MP4", "offset": 30.0},
    ]


def test_canopy_not_renamed_without_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_scene_assembly(monkeypatch)
    for accl in (1.06, -2.95):  # flying-canopy and turbulent-but-negative: both stay canopy
        classified = [
            ("freefall", _sig(filename="GH010002.MP4", duration=50.0, accl_z_mean=0.3)),
            ("canopy", _sig(filename="GH010003.MP4", duration=20.0, accl_z_mean=accl)),
        ]
        manifest = selfie.build_scenes(f"job_{accl}", classified, tmp_path)
        names = {s["name"] for s in manifest["scenes"]}
        assert "canopy" in names and "landing" not in names
        assert manifest["flagged"] == []


# --------------------------------------------------------------------------- #
# Wiring: compose_edls persists a validation_report and clean freefall EDL
# --------------------------------------------------------------------------- #


def test_compose_edls_writes_validation_report(tmp_path: Path) -> None:
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 30.0, "combined_path": "/x/i.mp4"},
            {"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 80.0, "combined_path": "/x/f.mp4",
             "exit_offset": 25.0, "deploy_offset": 70.0},
            {"name": "canopy", "duration": 20.0, "combined_path": "/x/c.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.6, "eye_contact": 0.7,
             "face_in_frame": 1.0, "face_centered": 0.8}
            for t in range(80)
        ]
    }
    selfie.compose_edls(scores, manifest, {}, "wirejob", tmp_path, use_ai=False)

    report = json.loads((tmp_path / "wirejob" / "validation_report.json").read_text())
    assert set(report["repairs"]) == {"full_video", "highlights", "freefall"}
    # The freefall deliverable contains only the freefall scene (canopy dropped).
    freefall = json.loads((tmp_path / "wirejob" / "edl_freefall.json").read_text())
    assert {c["scene"] for c in freefall} == {"freefall"}


def manifest_dur(manifest: dict[str, Any], scene: str) -> float:
    return next(s["duration"] for s in manifest["scenes"] if s["name"] == scene)
