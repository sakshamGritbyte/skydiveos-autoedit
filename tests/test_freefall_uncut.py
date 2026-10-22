"""The whole freefall plays without cuts (client rule, 2026-10-22).

Every video carrying the jump (``full_video``, ``freefall``, and the two per-camera
Ultimate cuts) shows the aerial window ``[exit - lead, deploy + tail]`` once, in order:
no second skipped, no second replayed. The editor's slow-mo beats stay where they are —
the renderer hard-concats, so a speed change inside a continuous run is not a cut.

The ONE exception is the Ultimate combo, whose freefall alternates the outside
cameraman and the instructor's handcam — still continuous on the exit-anchored
timeline, only the angle changes. ``highlights`` keeps its short beats on purpose.

Fixtures are the real EDLs + manifests of three processed jobs (see
``test_edl_validate.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api import selfie
from edl.validate import (
    _DEPLOY_TAIL_S,
    _EXIT_LEAD_S,
    _SWITCH_SPACING_OUT_S,
    CONTINUOUS_FREEFALL_DELIVERABLES,
    validate_and_repair,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_TOL = 1e-6


def _fx(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


def _mbc() -> dict[str, dict[str, Any]]:
    return {
        "instructor": _fx("ultimum_0823a77a/scene_manifest_instructor.json"),
        "external": _fx("ultimum_0823a77a/scene_manifest_external.json"),
    }


def _ff(manifest: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in manifest["scenes"] if s["name"] == "freefall")


def _window(manifest: dict[str, Any]) -> tuple[float, float]:
    ff = _ff(manifest)
    lo = max(0.0, ff["exit_offset"] - _EXIT_LEAD_S)
    hi = min(ff["duration"], ff["deploy_offset"] + _DEPLOY_TAIL_S)
    return lo, hi


def _out_dur(c: dict[str, Any]) -> float:
    return (c["src_end"] - c["src_start"]) / c["speed_multiplier"]


def _freefall_run(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The freefall clips, asserting they sit as ONE contiguous block in the edit."""
    idx = [i for i, c in enumerate(clips) if c["scene"] == "freefall"]
    assert idx, "no freefall in the edit"
    assert idx == list(range(idx[0], idx[0] + len(idx))), "freefall is split by other scenes"
    return [clips[i] for i in idx]


def _assert_uncut(run: list[dict[str, Any]], lo: float, hi: float) -> None:
    """Single camera: starts at lo, ends at hi, every clip begins where the last ended."""
    assert run[0]["src_start"] == pytest.approx(lo)
    assert run[-1]["src_end"] == pytest.approx(hi)
    for prev, cur in zip(run, run[1:], strict=False):
        assert cur["src_start"] == pytest.approx(prev["src_end"], abs=_TOL), (
            f"cut between {prev} and {cur}"
        )


# --------------------------------------------------------------------------- #
# Single camera
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("edl_name", "manifest_name", "deliverable"),
    [
        ("selfie_d15c2e42/edl_freefall.json", "selfie_d15c2e42/scene_manifest.json", "freefall"),
        ("selfie_d15c2e42/edl_full.json", "selfie_d15c2e42/scene_manifest.json", "full_video"),
        (
            "external_21cdb2c5/edl_freefall.json",
            "external_21cdb2c5/scene_manifest.json",
            "freefall",
        ),
        ("external_21cdb2c5/edl_full.json", "external_21cdb2c5/scene_manifest.json", "full_video"),
        (
            "ultimum_0823a77a/edl_external_freefall.json",
            "ultimum_0823a77a/scene_manifest_external.json",
            "external_freefall",
        ),
        (
            "ultimum_0823a77a/edl_chute_libre.json",
            "ultimum_0823a77a/scene_manifest_instructor.json",
            "chute_libre_selfie",
        ),
    ],
)
def test_single_camera_freefall_plays_whole(
    edl_name: str, manifest_name: str, deliverable: str
) -> None:
    manifest = _fx(manifest_name)
    edl = _fx(edl_name)
    lo, hi = _window(manifest)

    out, log = validate_and_repair(edl, deliverable, manifest)

    run = _freefall_run(out)
    _assert_uncut(run, lo, hi)
    # The deployment is in it — at the real opening, not a replay after the fact.
    deploy = _ff(manifest)["deploy_offset"]
    assert any(c["src_start"] <= deploy < c["src_end"] for c in run)
    # Fixed point: a second pass is a no-op.
    again, log2 = validate_and_repair(out, deliverable, manifest)
    assert again == out
    assert not any("freefall plays whole" in line for line in log2)
    assert deliverable in CONTINUOUS_FREEFALL_DELIVERABLES


def test_slowmo_beats_are_kept_in_place_and_never_replayed() -> None:
    # External job: E=25.02, D=75.08. The editor's beats at 29 and 32 survive at their
    # own seconds; the 1x stretches between them are filled; nothing plays twice.
    manifest = _fx("external_21cdb2c5/scene_manifest.json")
    edl = [
        {
            "scene": "freefall",
            "src_start": 25.02,
            "src_end": 27.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
        {
            "scene": "freefall",
            "src_start": 29.0,
            "src_end": 30.0,
            "speed_multiplier": 0.4,
            "camera": None,
        },
        {
            "scene": "freefall",
            "src_start": 40.0,
            "src_end": 44.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
        # A replay: overlaps the 29-30 beat -> dropped, never shown twice.
        {
            "scene": "freefall",
            "src_start": 29.5,
            "src_end": 30.5,
            "speed_multiplier": 0.4,
            "camera": None,
        },
        {
            "scene": "freefall",
            "src_start": 50.0,
            "src_end": 51.0,
            "speed_multiplier": 0.4,
            "camera": None,
        },
    ]
    out, log = validate_and_repair(edl, "freefall", manifest)

    _assert_uncut(out, 25.02, 78.08)
    slow = [(c["src_start"], c["src_end"]) for c in out if c["speed_multiplier"] != 1.0]
    assert (29.0, 30.0) in slow and (50.0, 51.0) in slow
    assert (29.5, 30.5) not in slow
    assert any("would replay seconds already shown" in line for line in log)
    # Total source shown == the whole window, exactly once.
    assert sum(c["src_end"] - c["src_start"] for c in out) == pytest.approx(78.08 - 25.02)


def test_story_scenes_around_the_freefall_are_untouched() -> None:
    manifest = _fx("external_21cdb2c5/scene_manifest.json")
    edl = _fx("external_21cdb2c5/edl_full.json")
    out, _ = validate_and_repair(edl, "full_video", manifest)

    # Everything before the freefall still precedes it, everything after still follows.
    ranks = {s["name"]: i for i, s in enumerate(manifest["scenes"])}
    seq = [ranks[c["scene"]] for c in out]
    assert seq == sorted(seq)
    assert [c for c in out if c["scene"] == "boarding"] == [
        c for c in edl if c["scene"] == "boarding"
    ]


def test_highlights_keep_their_short_freefall_beats() -> None:
    # A < 1 min highlights cannot hold a 40-80 s freefall: the rule does not apply.
    manifest = _fx("external_21cdb2c5/scene_manifest.json")
    edl = _fx("external_21cdb2c5/edl_highlights.json")
    out, log = validate_and_repair(edl, "highlights", manifest)
    assert "highlights" not in CONTINUOUS_FREEFALL_DELIVERABLES
    assert not any("freefall plays whole" in line for line in log)
    shown = sum(c["src_end"] - c["src_start"] for c in out if c["scene"] == "freefall")
    lo, hi = _window(manifest)
    assert shown < (hi - lo) / 2


def test_no_anchors_means_no_window_and_no_change() -> None:
    # Exit/deploy undetected: there is no window to fill, so nothing is invented.
    manifest = {"scenes": [{"name": "freefall", "duration": 60.0}]}
    edl = [
        {
            "scene": "freefall",
            "src_start": 5.0,
            "src_end": 9.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
        {
            "scene": "freefall",
            "src_start": 20.0,
            "src_end": 24.0,
            "speed_multiplier": 1.0,
            "camera": None,
        },
    ]
    out, log = validate_and_repair(edl, "freefall", manifest)
    assert out == edl and log == []


def test_house_cut_through_compose_persists_an_uncut_freefall(tmp_path: Path) -> None:
    # End to end through the offline house cut, as the external package composes:
    # the persisted EDLs are what the renderer (and every replay) reads.
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 30.0, "combined_path": "/x/i.mp4"},
            {"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"},
            {
                "name": "freefall",
                "duration": 80.0,
                "combined_path": "/x/f.mp4",
                "exit_offset": 25.0,
                "deploy_offset": 70.0,
            },
            {"name": "canopy", "duration": 20.0, "combined_path": "/x/c.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {
                "ts": float(t),
                "smile": 0.6 if t % 7 else 0.95,
                "eye_contact": 0.7,
                "face_in_frame": 1.0,
                "face_centered": 0.8,
            }
            for t in range(80)
        ]
    }
    selfie.compose_edls(scores, manifest, {}, "uncutjob", tmp_path, use_ai=False)
    jd = tmp_path / "uncutjob"
    for name in ("edl_freefall.json", "edl_full.json"):
        clips = json.loads((jd / name).read_text())
        _assert_uncut(_freefall_run(clips), 25.0, 73.0)


# --------------------------------------------------------------------------- #
# Ultimate combo — continuous, alternating the two angles
# --------------------------------------------------------------------------- #


def _anchored(c: dict[str, Any], mbc: dict[str, dict[str, Any]], edge: str) -> float:
    return c[edge] - _ff(mbc[c["camera"]])["exit_offset"]


@pytest.mark.parametrize("edl_name", ["ultimum_0823a77a/edl_full.json"])
def test_ultimate_combo_freefall_is_continuous_and_alternates(edl_name: str) -> None:
    mbc = _mbc()
    out, log = validate_and_repair(
        _fx(edl_name), "full_video", mbc["instructor"], manifest_by_camera=mbc
    )
    run = _freefall_run(out)

    # Both angles feature, and the edit switches back and forth (not one then the other).
    cams = [c["camera"] for c in run]
    assert set(cams) == {"instructor", "external"}
    switches = sum(1 for a, b in zip(cams, cams[1:], strict=False) if a != b)
    assert switches >= 4

    # Continuous on the exit-anchored timeline: the instructor's whole window, once.
    i_lo, i_hi = _window(mbc["instructor"])
    i_exit = _ff(mbc["instructor"])["exit_offset"]
    assert _anchored(run[0], mbc, "src_start") == pytest.approx(i_lo - i_exit)
    assert _anchored(run[-1], mbc, "src_end") == pytest.approx(i_hi - i_exit)
    for prev, cur in zip(run, run[1:], strict=False):
        assert _anchored(cur, mbc, "src_start") == pytest.approx(
            _anchored(prev, mbc, "src_end"), abs=1e-5
        ), f"time jumps between {prev} and {cur}"

    # Each camera is only used inside its OWN aerial window (no aircraft, no canopy).
    for c in run:
        lo, hi = _window(mbc[c["camera"]])
        assert lo - _TOL <= c["src_start"] and c["src_end"] <= hi + _TOL

    # Pacing: inside the freefall, an angle is held >= the switch spacing.
    t, last_switch, last_cam = 0.0, None, None
    for c in run:
        if last_cam is not None and c["camera"] != last_cam:
            if last_switch is not None:
                assert t - last_switch >= _SWITCH_SPACING_OUT_S - _TOL
            last_switch = t
        last_cam = c["camera"]
        t += _out_dur(c)

    assert any("alternating instructor / external" in line for line in log)
    again, _ = validate_and_repair(out, "full_video", mbc["instructor"], manifest_by_camera=mbc)
    assert _freefall_run(again) == run


def test_ultimate_combo_falls_back_to_the_camera_that_has_footage() -> None:
    # The cameraman's window ends early (he broke off): the instructor covers the rest
    # instead of the timeline skipping it.
    mbc = _mbc()
    ext = json.loads(json.dumps(mbc["external"]))
    ff = _ff(ext)
    ff["deploy_offset"] = ff["exit_offset"] + 10.0  # only 10 s of outside footage
    mbc["external"] = ext
    edl = [
        {
            "scene": "freefall",
            "src_start": 30.0,
            "src_end": 35.0,
            "speed_multiplier": 1.0,
            "camera": "instructor",
        },
        {
            "scene": "freefall",
            "src_start": 30.0,
            "src_end": 35.0,
            "speed_multiplier": 1.0,
            "camera": "external",
        },
    ]
    out, _ = validate_and_repair(edl, "full_video", mbc["instructor"], manifest_by_camera=mbc)
    run = _freefall_run(out)
    e_lo, e_hi = _window(ext)
    for c in run:
        if c["camera"] == "external":
            assert c["src_end"] <= e_hi + _TOL
    i_lo, i_hi = _window(mbc["instructor"])
    i_exit = _ff(mbc["instructor"])["exit_offset"]
    assert _anchored(run[-1], mbc, "src_end") == pytest.approx(i_hi - i_exit)
    for prev, cur in zip(run, run[1:], strict=False):
        assert _anchored(cur, mbc, "src_start") == pytest.approx(
            _anchored(prev, mbc, "src_end"), abs=1e-5
        )


def test_ultimate_highlights_are_not_rebuilt() -> None:
    mbc = _mbc()
    _, log = validate_and_repair(
        _fx("ultimum_0823a77a/edl_highlights.json"),
        "highlights",
        mbc["instructor"],
        manifest_by_camera=mbc,
    )
    assert not any("freefall plays whole" in line for line in log)
