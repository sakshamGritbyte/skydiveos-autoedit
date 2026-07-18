"""Tests for the selfie-package pipeline (api/selfie.py + process_selfie_package).

Every external boundary is mocked so these run fully offline: GPMF extraction and
ffprobe are monkeypatched (no GoPro files), FFmpeg concat/render are stubbed, the
MediaPipe scorer is replaced with canned rows, the Claude call uses a scripted fake
client, and frame decode / JPEG writes are faked. So we exercise the *logic* —
classification, scene assembly, scoring shape, the one-call+one-retry EDL contract,
parallel render fan-out, and photo selection — without the heavy deps.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from api import selfie
from api.jobs import JobStatus, JobStore, Package
from api.selfie import (
    Clip,
    EDLResponse,
    FileSignals,
    GpmfSignals,
    LowConfidenceError,
    chapter_from_filename,
    classify_files,
    classify_scene,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sig(
    *,
    altitude_mean: float = 0.0,
    altitude_first: float = 0.0,
    altitude_last: float = 0.0,
    accl_z_mean: float = 1.0,
    accl_z_std: float = 0.1,
    accl_mag_mean: float = 1.0,
    accl_mag_std: float = 0.0,
    accl_mag_min: float = 1.0,
    has_altitude: bool = True,
    chapter: int = 1,
    filename: str = "GH010001.MP4",
    duration: float = 10.0,
) -> FileSignals:
    return FileSignals(
        filename=filename,
        path=f"/raw/{filename}",
        chapter=chapter,
        duration=duration,
        gpmf=GpmfSignals(
            altitude_mean=altitude_mean,
            altitude_first=altitude_first,
            altitude_last=altitude_last,
            altitude_delta=altitude_last - altitude_first,
            accl_z_mean=accl_z_mean,
            accl_z_std=accl_z_std,
            accl_mag_mean=accl_mag_mean,
            accl_mag_std=accl_mag_std,
            accl_mag_min=accl_mag_min,
            has_altitude=has_altitude,
        ),
    )


def _text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _FakeMessages:
    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return _text_response(self._replies[idx])


class FakeClient:
    def __init__(self, *replies: str) -> None:
        self.messages = _FakeMessages(list(replies))


VALID_EDL_JSON = json.dumps(
    {
        "full_video": [
            {"scene": "freefall", "src_start": 0, "src_end": 10, "speed_multiplier": 1.0}
        ],
        "highlights": [
            {"scene": "freefall", "src_start": 2, "src_end": 8, "speed_multiplier": 0.4}
        ],
        "freefall": [
            {"scene": "freefall", "src_start": 1, "src_end": 9, "speed_multiplier": 0.4}
        ],
    }
)


# --------------------------------------------------------------------------- #
# Step 1: classification
# --------------------------------------------------------------------------- #


def test_chapter_from_filename() -> None:
    assert chapter_from_filename("GH010001.MP4") == 1
    assert chapter_from_filename("GH020001.MP4") == 2
    assert chapter_from_filename("GX110007.MP4") == 11
    assert chapter_from_filename("weird.mp4") == 1  # graceful fallback


def test_classify_all_seven_scene_types() -> None:
    # index/total position the two interview scenes (first vs last 20%).
    total = 7
    cases = {
        "intro_interview": (_sig(altitude_mean=10, altitude_first=10, altitude_last=10), 0),
        "boarding": (
            _sig(altitude_mean=10, altitude_first=10, altitude_last=10, accl_z_std=0.5),
            3,
        ),
        "takeoff": (_sig(altitude_mean=500, altitude_first=100, altitude_last=900), 1),
        "plane": (
            _sig(altitude_mean=3000, altitude_first=3000, altitude_last=3000, accl_z_std=0.1),
            2,
        ),
        "freefall": (
            _sig(altitude_mean=2200, altitude_first=3800, altitude_last=600, accl_z_mean=0.1),
            4,
        ),
        "canopy": (_sig(altitude_mean=1500, altitude_first=2500, altitude_last=500), 5),
        "outro_interview": (
            _sig(altitude_mean=10, altitude_first=10, altitude_last=10), 6
        ),
    }
    for expected, (sig, index) in cases.items():
        assert classify_scene(sig, index, total) == expected, expected


def test_classify_no_gps_uses_accelerometer() -> None:
    # Real GoPro telemetry with GPS off (all altitude 0): freefall/canopy must still
    # be found from the accelerometer magnitude. Values mirror an actual 7-clip jump.
    total = 7
    cases = {
        # idx 0-1 in first 20% -> intro; calm ~1 g.
        "intro_interview": (_sig(has_altitude=False, accl_mag_mean=1.02,
                                 accl_mag_std=0.12, accl_mag_min=0.55), 0),
        # calm middle clip -> boarding (plane indistinguishable without GPS).
        "boarding": (_sig(has_altitude=False, accl_mag_mean=1.06,
                          accl_mag_std=0.13, accl_mag_min=0.65), 2),
        # violent buffeting + ~0 g exit dip -> freefall.
        "freefall": (_sig(has_altitude=False, accl_mag_mean=1.44,
                          accl_mag_std=0.95, accl_mag_min=0.07), 4),
        # sustained >1 g, moderate swing -> canopy.
        "canopy": (_sig(has_altitude=False, accl_mag_mean=1.28,
                       accl_mag_std=0.41, accl_mag_min=0.47), 5),
        # idx 6 in last 20% -> outro.
        "outro_interview": (_sig(has_altitude=False, accl_mag_mean=1.02,
                                accl_mag_std=0.12, accl_mag_min=0.57), 6),
    }
    for expected, (sig, index) in cases.items():
        assert classify_scene(sig, index, total) == expected, expected


def test_classify_no_gps_never_unknown() -> None:
    # Without GPS, a calm clip always lands on a ground scene (never 'unknown'),
    # so a GPS-less jump never trips LowConfidenceError.
    sig = _sig(has_altitude=False, accl_mag_mean=1.0, accl_mag_std=0.1, accl_mag_min=0.6)
    assert classify_scene(sig, 3, 7) == "boarding"


def test_classify_unknown_when_no_rule_matches() -> None:
    # mid altitude, flat, quiet, middle of the jump -> nothing matches.
    sig = _sig(altitude_mean=70, altitude_first=70, altitude_last=70)
    assert classify_scene(sig, 3, 7) == "unknown"


def test_classify_is_ground_relative_for_elevated_dropzone() -> None:
    # A dropzone at 60 m elevation: a ground clip reads alt 60, not < 50. With the
    # ground passed in, it still classifies as a ground scene (regression: was unknown).
    ground = _sig(altitude_mean=60, altitude_first=60, altitude_last=60)
    assert classify_scene(ground, 0, 7, ground=60.0) == "intro_interview"
    assert classify_scene(ground, 6, 7, ground=60.0) == "outro_interview"
    # A clip 700 m above that ground while climbing -> takeoff.
    climb = _sig(altitude_mean=760, altitude_first=400, altitude_last=1100)
    assert classify_scene(climb, 2, 7, ground=60.0) == "takeoff"


def test_classify_files_ground_relative_no_false_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whole jump at a 60 m dropzone: ground clips must NOT trip LowConfidenceError.
    raw = tmp_path / "raw"
    raw.mkdir()
    alts = {"GH010001.MP4": 60, "GH020001.MP4": 61, "GH030001.MP4": 62, "GH040001.MP4": 60}
    for name in alts:
        (raw / name).write_bytes(b"x")
    monkeypatch.setattr(
        selfie, "build_file_signals",
        lambda path: _sig(
            altitude_mean=alts[Path(path).name], altitude_first=alts[Path(path).name],
            altitude_last=alts[Path(path).name], accl_z_std=0.5,
            filename=Path(path).name,
        ),
    )
    classified = classify_files(raw)  # no error
    assert all(label != "unknown" for label, _ in classified)


def test_natural_filename_ordering_independent_of_prefix() -> None:
    # Ordering must work for ANY naming scheme, and put clip2 before clip10.
    assert selfie._natural_key("clip2.mp4") < selfie._natural_key("clip10.mp4")
    assert selfie._natural_key("VID_3.MP4") < selfie._natural_key("VID_12.MP4")
    # Recording time wins over filename when present.
    a = _sig(filename="z_late.mp4")
    a.recorded_at = 100.0
    b = _sig(filename="a_early.mp4")
    b.recorded_at = 50.0
    assert selfie._order_key(b) < selfie._order_key(a)


def test_classify_files_raises_low_confidence_on_three_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    names = ["GH010001.MP4", "GH020001.MP4", "GH030001.MP4", "GH040001.MP4"]
    for name in names:
        (raw / name).write_bytes(b"x")

    # First clip is ground (60 m); the rest sit +70 m above it with no climb/descent —
    # the "dead zone" the classifier can't resolve -> 3 unknown (> 2) -> error.
    def fake_build(path: str | Path) -> FileSignals:
        p = Path(path)
        alt = 60.0 if p.name == "GH010001.MP4" else 130.0
        return _sig(
            altitude_mean=alt, altitude_first=alt, altitude_last=alt,
            chapter=chapter_from_filename(p.name), filename=p.name,
        )

    monkeypatch.setattr(selfie, "build_file_signals", fake_build)
    with pytest.raises(LowConfidenceError, match="could not be classified"):
        classify_files(raw)


def test_scene_labels_override_forces_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    for name in ("GH010001.MP4", "GH020001.MP4"):
        (raw / name).write_bytes(b"x")

    # Telemetry would call both 'unknown' (mid-altitude, flat) ...
    def fake_build(path: str | Path) -> FileSignals:
        p = Path(path)
        return _sig(
            altitude_mean=70, altitude_first=70, altitude_last=70,
            chapter=chapter_from_filename(p.name), filename=p.name,
        )

    monkeypatch.setattr(selfie, "build_file_signals", fake_build)
    # ... but the manual override pins the scenes (job dir = raw.parent).
    (tmp_path / "scene_labels.json").write_text(
        json.dumps({"GH010001.MP4": "freefall", "GH020001.MP4": "canopy"})
    )
    classified = classify_files(raw)
    by_name = {sig.filename: label for label, sig in classified}
    assert by_name == {"GH010001.MP4": "freefall", "GH020001.MP4": "canopy"}


def test_scene_labels_override_prevents_low_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    names = ["GH010001.MP4", "GH020001.MP4", "GH030001.MP4", "GH040001.MP4"]
    for name in names:
        (raw / name).write_bytes(b"x")

    # GH010001 is ground; GH02/03/04 sit in the dead zone (+70 m, flat) -> unknown.
    def fake_build(path: str | Path) -> FileSignals:
        p = Path(path)
        alt = 60.0 if p.name == "GH010001.MP4" else 130.0
        return _sig(
            altitude_mean=alt, altitude_first=alt, altitude_last=alt,
            chapter=chapter_from_filename(p.name), filename=p.name,
        )

    monkeypatch.setattr(selfie, "build_file_signals", fake_build)
    # 3 dead-zone clips would be unknown (-> error); overriding one leaves 2 -> no error.
    (tmp_path / "scene_labels.json").write_text(json.dumps({"GH030001.MP4": "freefall"}))
    classified = classify_files(raw)
    assert len(classified) == 4
    assert sum(1 for label, _ in classified if label == "unknown") == 2


def test_scene_labels_invalid_scene_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "GH010001.MP4").write_bytes(b"x")
    monkeypatch.setattr(selfie, "build_file_signals", lambda path: _sig(filename=Path(path).name))
    (tmp_path / "scene_labels.json").write_text(json.dumps({"GH010001.MP4": "wingsuit"}))
    with pytest.raises(selfie.SelfieError, match="not a valid"):
        classify_files(raw)


def test_load_scene_labels_absent_is_empty(tmp_path: Path) -> None:
    assert selfie.load_scene_labels(tmp_path / "nope.json") == {}


def test_classify_files_sorts_by_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    for name in ("GH020001.MP4", "GH010001.MP4"):
        (raw / name).write_bytes(b"x")

    def fake_build(path: str | Path) -> FileSignals:
        p = Path(path)
        # Both freefall, distinguishable only by chapter.
        return _sig(
            altitude_mean=2000, altitude_first=3800, altitude_last=600, accl_z_mean=0.1,
            chapter=chapter_from_filename(p.name), filename=p.name,
        )

    monkeypatch.setattr(selfie, "build_file_signals", fake_build)
    classified = classify_files(raw)
    assert [s.chapter for _, s in classified] == [1, 2]  # chapter-ordered


# --------------------------------------------------------------------------- #
# Step 1: scene assembly (concat)
# --------------------------------------------------------------------------- #


def test_build_scenes_concats_multi_chapter_and_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    concat_calls: list[tuple[list[str], str]] = []

    def fake_concat(source_paths: Any, out_path: Any) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"scene")
        concat_calls.append((list(source_paths), str(out)))
        return out

    monkeypatch.setattr(selfie, "concat_scene", fake_concat)

    classified = [
        ("freefall", _sig(filename="GH010003.MP4", chapter=1, altitude_first=3800,
                           altitude_last=600, altitude_mean=2200, accl_z_mean=0.1)),
        ("freefall", _sig(filename="GH020003.MP4", chapter=2, altitude_first=600,
                           altitude_last=400, altitude_mean=500, accl_z_mean=0.1)),
        ("canopy", _sig(filename="GH010004.MP4", chapter=1, altitude_first=2500,
                        altitude_last=500, altitude_mean=1500)),
    ]
    manifest = selfie.build_scenes("job1", classified, tmp_path)

    scenes = {s["name"]: s for s in manifest["scenes"]}
    assert set(scenes) == {"freefall", "canopy"}
    # Freefall scene concatenated both chapters, in order.
    assert scenes["freefall"]["source_files"] == ["GH010003.MP4", "GH020003.MP4"]
    ff_call = next(c for c in concat_calls if c[1].endswith("freefall.mp4"))
    assert ff_call[0] == ["/raw/GH010003.MP4", "/raw/GH020003.MP4"]
    # Duration sums; delta spans first clip's first to last clip's last.
    assert scenes["freefall"]["duration"] == pytest.approx(20.0)
    assert scenes["freefall"]["gpmf_signals"]["altitude_delta"] == pytest.approx(-3400.0)
    assert manifest["flagged"] == []

    # Persisted to scene_manifest.json.
    saved = json.loads((tmp_path / "job1" / "scene_manifest.json").read_text())
    assert saved == manifest


def test_concat_scene_writes_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The concat demuxer resolves relative entries against the listfile's dir, so the
    # filelist must hold absolute paths (regression: doubled jobs/.../scenes/jobs/...).
    monkeypatch.setattr(
        selfie.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stderr=""),
    )

    clip = tmp_path / "raw" / "GX010052.MP4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    out = tmp_path / "scenes" / "intro_interview.mp4"

    selfie.concat_scene([str(clip)], out)
    listfile = out.parent / "intro_interview_filelist.txt"
    body = listfile.read_text()
    assert body == f"file '{clip.resolve()}'\n"
    assert body.startswith("file '/")  # absolute, never a doubled relative path


def test_build_scenes_flags_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        selfie, "concat_scene",
        lambda src, out: (Path(out).parent.mkdir(parents=True, exist_ok=True),
                          Path(out).write_bytes(b"x"), Path(out))[-1],
    )
    classified = [("unknown", _sig(altitude_mean=70, altitude_first=70, altitude_last=70))]
    manifest = selfie.build_scenes("j", classified, tmp_path)
    assert manifest["flagged"] == ["unknown"]
    assert manifest["scenes"][0]["needs_review"] is True


# --------------------------------------------------------------------------- #
# Step 2: scoring
# --------------------------------------------------------------------------- #


def test_score_scenes_writes_valid_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"ts": 0.0, "smile": 0.8, "eye_contact": 0.7, "face_in_frame": 1.0, "face_centered": 0.9},
        {"ts": 1.0, "smile": 0.5, "eye_contact": 0.6, "face_in_frame": 1.0, "face_centered": 0.8},
    ]
    monkeypatch.setattr(selfie, "score_scene", lambda path, **kw: rows)

    manifest = {
        "scenes": [
            {"name": "freefall", "combined_path": str(tmp_path / "freefall.mp4")},
            {"name": "canopy", "combined_path": str(tmp_path / "canopy.mp4")},
        ],
        "flagged": [],
    }
    scores = selfie.score_scenes(manifest, "job1", tmp_path)
    assert set(scores) == {"freefall", "canopy"}
    assert scores["freefall"][0]["smile"] == 0.8
    saved = json.loads((tmp_path / "job1" / "scores.json").read_text())
    assert saved == scores


# --------------------------------------------------------------------------- #
# Step 3: compose three EDLs
# --------------------------------------------------------------------------- #


def _scores() -> dict[str, list[dict[str, float]]]:
    return {
        "freefall": [
            {"ts": float(t), "smile": 0.9 - t * 0.1, "eye_contact": 0.8,
             "face_in_frame": 1.0, "face_centered": 0.9}
            for t in range(5)
        ]
    }


def _manifest() -> dict[str, Any]:
    return {
        "scenes": [{"name": "freefall", "duration": 10.0, "combined_path": "/x/freefall.mp4"}],
        "flagged": [],
    }


def test_compose_edls_happy_path_persists_three_files(tmp_path: Path) -> None:
    client = FakeClient(VALID_EDL_JSON)
    booking = {"customer_name": "Jane", "jump_date": "2026-06-02", "music": None}
    edls = selfie.compose_edls(
        _scores(), _manifest(), booking, "job1", tmp_path, client=client
    )
    assert isinstance(edls, EDLResponse)
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["model"] == selfie.CLAUDE_MODEL
    assert client.messages.calls[0]["max_tokens"] == 2000

    jd = tmp_path / "job1"
    for name in ("edl_full.json", "edl_highlights.json", "edl_freefall.json"):
        assert (jd / name).exists()
    full = json.loads((jd / "edl_full.json").read_text())
    assert full[0]["scene"] == "freefall"


def test_compose_edls_retries_once_then_succeeds(tmp_path: Path) -> None:
    # First reply: missing the required 'freefall' key -> invalid. Second: valid.
    bad = json.dumps({"full_video": [], "highlights": []})
    client = FakeClient(bad, VALID_EDL_JSON)
    booking = {"customer_name": "Jane", "jump_date": "2026-06-02"}
    edls = selfie.compose_edls(
        _scores(), _manifest(), booking, "job1", tmp_path, client=client
    )
    assert len(edls.freefall) == 1
    assert len(client.messages.calls) == 2  # initial + one retry
    retry_messages = client.messages.calls[1]["messages"]
    assert retry_messages[1]["role"] == "assistant"
    assert "not a valid EDL" in retry_messages[2]["content"]


def test_compose_edls_gives_up_after_two_invalid(tmp_path: Path) -> None:
    client = FakeClient("not json", "still not json")
    with pytest.raises(selfie.SelfieError, match="after 2 attempts"):
        selfie.compose_edls(_scores(), _manifest(), {}, "job1", tmp_path, client=client)
    assert len(client.messages.calls) == 2


def test_compose_edls_offline_fallback_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No client and no ANTHROPIC_API_KEY -> deterministic house cut, no network.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 20.0, "combined_path": "/x/intro.mp4"},
            {"name": "freefall", "duration": 60.0, "combined_path": "/x/freefall.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.5 + (t == 30) * 0.4, "eye_contact": 0.7,
             "face_in_frame": 1.0 if t >= 5 else 0.1, "face_centered": 0.8}
            for t in range(60)
        ]
    }
    edls = selfie.compose_edls(scores, manifest, {}, "job1", tmp_path)

    assert edls.full_video and edls.highlights and edls.freefall
    # Freefall full-video clips include a slow-mo on the peak-smile second (ts=30).
    assert any(c.scene == "freefall" and c.speed_multiplier == 0.4 for c in edls.full_video)
    # The exit always leads the freefall cut, anchored at the true scene start (ts 0).
    assert edls.freefall[0].scene == "freefall"
    assert edls.freefall[0].src_start == 0.0
    # All three files written.
    for name in ("edl_full.json", "edl_highlights.json", "edl_freefall.json"):
        assert (tmp_path / "job1" / name).exists()


def test_compose_edls_use_ai_false_forces_house_cut(tmp_path: Path) -> None:
    # The external package composes deterministically even with a client present: the
    # AI editor must NOT be called, and the house cut's complete in-order edit is used.
    client = FakeClient(VALID_EDL_JSON)
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 20.0, "combined_path": "/x/i.mp4"},
            {"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 80.0, "combined_path": "/x/f.mp4"},
            {"name": "canopy", "duration": 50.0, "combined_path": "/x/c.mp4"},
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
    edls = selfie.compose_edls(
        scores, manifest, {}, "job1", tmp_path, client=client, use_ai=False
    )
    assert len(client.messages.calls) == 0  # the model was never consulted
    # The full video covers every scene, in jump order (nothing dropped or scrambled).
    fv_scenes = [c.scene for c in edls.full_video]
    assert set(fv_scenes) == {"intro_interview", "boarding", "freefall", "canopy"}
    ranks = [selfie._scene_rank(s) for s in fv_scenes]
    assert ranks == sorted(ranks)


def _slowmo_manifest_scores() -> tuple[dict[str, Any], dict[str, list[dict[str, float]]]]:
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.95 if t in (10, 20, 30) else 0.2,
             "eye_contact": 0.7, "face_in_frame": 1.0, "face_centered": 0.8}
            for t in range(40)
        ],
        "canopy": [
            {"ts": float(t), "smile": 0.4, "eye_contact": 0.6,
             "face_in_frame": 1.0, "face_centered": 0.7}
            for t in range(20)
        ],
    }
    manifest = {
        "scenes": [
            {"name": "freefall", "duration": 40.0, "combined_path": "/x/freefall.mp4"},
            {"name": "canopy", "duration": 20.0, "combined_path": "/x/canopy.mp4"},
        ],
        "flagged": [],
    }
    return manifest, scores


def test_house_full_video_has_multi_segment_slowmo() -> None:
    manifest, scores = _slowmo_manifest_scores()
    edls = selfie._house_edls(scores, manifest, 90.0)
    full = edls.full_video
    ff_slow = [c for c in full if c.scene == "freefall" and c.speed_multiplier == 0.4]
    assert len(ff_slow) >= 3  # exit + multiple freefall smile peaks
    # Never slows a whole scene: normal-speed freefall segments remain.
    assert any(c.scene == "freefall" and c.speed_multiplier == 1.0 for c in full)
    # Deployment beat: canopy start slowed.
    assert any(c.scene == "canopy" and c.speed_multiplier == 0.4 for c in full)


def test_house_highlights_includes_freefall_with_slowmo() -> None:
    manifest, scores = _slowmo_manifest_scores()
    edls = selfie._house_edls(scores, manifest, 90.0)
    ff = [c for c in edls.highlights if c.scene == "freefall"]
    assert ff, "highlights must include freefall"
    assert any(c.speed_multiplier == 0.4 for c in ff)  # slow-mo present


def test_curated_freefall_has_deployment_and_slowmo() -> None:
    manifest, scores = _slowmo_manifest_scores()
    edls = selfie._house_edls(scores, manifest, 90.0)
    ff = edls.freefall
    assert any(c.speed_multiplier == 0.4 for c in ff)  # slow-mo on peaks
    # Ends with the deployment beat from the canopy scene.
    assert ff[-1].scene == "canopy"


def _full_jump_manifest_scores() -> tuple[dict[str, Any], dict[str, list[dict[str, float]]]]:
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 30.0, "combined_path": "/x/i.mp4"},
            {"name": "boarding", "duration": 120.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 70.0, "combined_path": "/x/f.mp4"},
            {"name": "canopy", "duration": 60.0, "combined_path": "/x/c.mp4"},
            {"name": "outro_interview", "duration": 20.0, "combined_path": "/x/o.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9 if t in (10, 30, 50) else 0.3, "eye_contact": 0.7,
             "face_in_frame": 1.0 if t >= 2 else 0.1, "face_centered": 0.8}
            for t in range(70)
        ]
    }
    return manifest, scores


def test_freefall_video_leads_with_door_exit_then_jump() -> None:
    # The freefall edit must start at the aircraft door/exit-prep (tail of the boarding
    # scene) and always contain the actual exit (freefall ts 0).
    manifest, scores = _full_jump_manifest_scores()
    edls = selfie._house_edls(scores, manifest, 90.0)
    ff = edls.freefall
    # Leads with the door/exit-prep: the END of the boarding scene.
    assert ff[0].scene == "boarding"
    assert ff[0].src_end == pytest.approx(120.0)        # the very tail (door)
    assert ff[0].src_start == pytest.approx(112.0)      # last 8 s
    # The actual exit/jump is present, anchored at the freefall start.
    assert any(c.scene == "freefall" and c.src_start == 0.0 for c in ff)
    # The exit sequence is a CONTINUOUS block (not a 3 s snippet): the freefall plays
    # uninterrupted from ts 0 through the configured exit-sequence length.
    covered = 0.0
    for c in ff:
        if c.scene == "freefall" and abs(c.src_start - covered) < 0.01:
            covered = c.src_end
    assert covered >= selfie._EXIT_SEQUENCE_S
    # Ends on the deployment (canopy opening).
    assert ff[-1].scene == "canopy"


def test_highlights_includes_all_milestones() -> None:
    # Milestone-first: intro, boarding (entry/inside/door), the exit/jump, freefall,
    # canopy (opening/landing), and outro must all be represented.
    manifest, scores = _full_jump_manifest_scores()
    edls = selfie._house_edls(scores, manifest, 90.0)
    scenes_present = {c.scene for c in edls.highlights}
    # Intro is never skipped, and the canopy opening is featured.
    expected = {"intro_interview", "boarding", "freefall", "canopy", "outro_interview"}
    assert expected <= scenes_present
    # The intro gets a longer beat (it sets up the story).
    intro_secs = sum(
        c.src_end - c.src_start for c in edls.highlights if c.scene == "intro_interview"
    )
    assert intro_secs == pytest.approx(selfie._HL_INTRO_S)
    # The exit/jump (freefall ts 0) is always included.
    assert any(c.scene == "freefall" and c.src_start == 0.0 for c in edls.highlights)
    # The canopy-opening beat (start of the canopy scene) is a slow-mo highlight.
    assert any(c.scene == "canopy" and c.src_start == 0.0 and c.speed_multiplier == 0.4
               for c in edls.highlights)
    # A boarding clip captures the door/exit-prep (tail of the boarding scene).
    assert any(c.scene == "boarding" and c.src_end >= 117.0 for c in edls.highlights)


def _fake_gpmf(g_by_second: list[float]) -> Any:
    """A GpmfData with one ACCL payload per second at the given magnitude (g)."""
    from metadata.gpmf import GpmfData, StreamSamples

    payloads = [[(0.0, 0.0, g * 9.80665)] * 5 for g in g_by_second]
    times = [float(t) for t in range(len(g_by_second))]
    accl = StreamSamples(fourcc="ACCL", payloads=payloads, times=times)
    return GpmfData(streams={"ACCL": accl}, duration_s=float(len(g_by_second)))


def test_detect_exit_offset_finds_subg_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    # ~1 g for 10 s (in the plane), then a sub-g collapse at t=10 (the exit).
    data = _fake_gpmf([1.0] * 10 + [0.4, 0.4, 0.5, 0.6])
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: data)
    assert selfie.detect_exit_offset("/x/freefall.mp4") == 10.0


def test_detect_exit_offset_zero_when_no_dip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Steady ~1 g throughout (clip already in freefall / no clear exit) -> 0.0.
    data = _fake_gpmf([1.0] * 15)
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: data)
    assert selfie.detect_exit_offset("/x/freefall.mp4") == 0.0


def test_exit_offset_drives_freefall_and_highlights() -> None:
    # The freefall clip starts inside the plane; the real exit is at 20 s.
    manifest = {
        "scenes": [
            {"name": "boarding", "duration": 30.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 70.0,
             "combined_path": "/x/f.mp4", "exit_offset": 20.0},
            {"name": "canopy", "duration": 18.0, "combined_path": "/x/c.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9 if t in (40, 55) else 0.3, "eye_contact": 0.7,
             "face_in_frame": 1.0 if t >= 20 else 0.1, "face_centered": 0.8}
            for t in range(70)
        ]
    }
    edls = selfie._house_edls(scores, manifest, 90.0)

    ff = edls.freefall
    # The exit/jump slow-mo is anchored at the DETECTED exit (20 s), not ts 0.
    assert any(
        c.scene == "freefall" and c.src_start == 20.0 and c.speed_multiplier == 0.4 for c in ff
    )
    # Nothing from the in-plane head (before ~exit - door_prep) sneaks in.
    lead_in = 20.0 - selfie._DOOR_PREP_S
    assert all(c.src_start >= lead_in for c in ff if c.scene == "freefall")
    # Highlights' exit beat also starts at the detected exit.
    assert any(
        c.scene == "freefall" and c.src_start == 20.0 and c.speed_multiplier == 0.4
        for c in edls.highlights
    )


def test_detect_deploy_offset_finds_opening_shock(monkeypatch: pytest.MonkeyPatch) -> None:
    # ~1 g freefall, then a sustained high-g shock at t=40 (the canopy opening).
    data = _fake_gpmf([1.0] * 40 + [2.2, 2.0, 1.9, 1.0])
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: data)
    assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 40.0
    # Steady freefall with no opening -> 0.
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: _fake_gpmf([1.0] * 30))
    assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 0.0


def test_detect_deploy_picks_strongest_shock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real tandem: the canopy opening is the HARDEST deceleration of the jump (the snap),
    # stronger than mid-freefall buffets before it and any canopy maneuver after it.
    # Pick the strongest sustained shock (t=19) — not the first (would truncate freefall
    # to a mid-air buffet) nor the last (would land on a late canopy maneuver).
    g = (
        [1.0] * 7          # 0-6: post-exit freefall, settling toward terminal
        + [2.2, 2.0]       # 7-8: a freefall buffet (weaker than the opening)
        + [1.0] * 10       # 9-18: more freefall
        + [4.0, 3.5]       # 19-20: the canopy opening — the strongest shock
        + [1.0] * 4        # 21-24: settling under canopy
        + [2.1, 2.0]       # 25-26: a later canopy maneuver (weaker)
        + [1.0] * 3        # 27-29: calm canopy ride
    )
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: _fake_gpmf(g))
    assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 19.0


def test_deploy_rejects_midfreefall_spike_confirms_later_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A mid-freefall jolt (t=61) out-spikes the real main opening (t=74) — the exact
    # b0b3176f… failure. The strong t=61 shock is followed by CONTINUED freefall
    # buffeting (alternating 0.5/2.0 g chaos), the weaker t=74 shock by a SMOOTH ~1.15 g
    # canopy ride. Strongest-first evaluation rejects t=61 (chaos) and confirms t=74.
    g = (
        [1.0] * 61                                    # 0-60: freefall
        + [4.0, 3.5]                                  # 61-62: strong FALSE shock
        + [0.5, 2.0, 0.5, 2.0, 0.5, 2.0,
           0.5, 2.0, 0.5, 2.0, 0.5]                   # 63-73: buffeting continues
        + [2.5, 2.2]                                  # 74-75: the real (weaker) opening
        + [1.15] * 8                                  # 76-83: smooth canopy ride
    )
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: _fake_gpmf(g))
    assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 74.0


def test_deploy_keeps_current_behavior_when_only_one_confirmed_shock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One clean opening at t=40 followed by a smooth canopy ride (enough tail to judge):
    # confirmed via the canopy signature, returns the same value the old code would.
    g = [1.0] * 40 + [2.2, 2.0, 1.9] + [1.15] * 8
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: _fake_gpmf(g))
    assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 40.0


def test_deploy_fallback_when_no_candidate_confirmed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Every shock (t=5 weak, t=13 strongest) is followed by continued chaos — nothing
    # looks like a canopy. Rather than regress to 0.0, fall back to the strongest shock
    # (t=13) and log a warning naming the fallback path.
    g = (
        [1.0] * 5
        + [2.2, 2.0]                                  # 5-6: weak shock
        + [2.0, 0.5, 2.0, 0.5, 2.0, 0.5]             # 7-12: chaos (ends low, isolates below)
        + [4.0, 3.5]                                  # 13-14: strongest shock
        + [0.5, 2.0, 0.5, 2.0, 0.5, 2.0, 0.5, 2.0]   # 15-22: chaos continues
    )
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: _fake_gpmf(g))
    with caplog.at_level(logging.WARNING, logger="api.selfie"):
        assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 13.0
    assert "falling back to strongest shock" in caplog.text


def test_deploy_candidate_too_near_eof_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The opening at t=40 has only ~3 s of telemetry after it — too little to confirm a
    # canopy ride. It's unconfirmed, so we fall back to the strongest shock (still t=40)
    # and warn, rather than trusting an unverifiable window.
    g = [1.0] * 40 + [2.2, 2.0, 1.9, 1.0]
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: _fake_gpmf(g))
    with caplog.at_level(logging.WARNING, logger="api.selfie"):
        assert selfie.detect_deploy_offset(["/x/freefall.mp4"]) == 40.0
    assert "falling back to strongest shock" in caplog.text


def test_canopy_opening_detected_inside_freefall_scene() -> None:
    # No separate canopy scene: the canopy ride is part of the 110 s freefall clip, with
    # the opening detected at 86 s. It must still be featured as a beat, and freefall
    # moments must NOT be mined from the canopy descent (after 86 s).
    manifest = {
        "scenes": [
            {"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 110.0,
             "combined_path": "/x/f.mp4", "exit_offset": 4.0, "deploy_offset": 86.0},
            {"name": "outro_interview", "duration": 20.0, "combined_path": "/x/o.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9 if t in (30, 60, 95) else 0.3, "eye_contact": 0.7,
             "face_in_frame": 1.0, "face_centered": 0.8}
            for t in range(110)
        ]
    }
    edls = selfie._house_edls(scores, manifest, 90.0)

    # The canopy opening (freefall @86 s, slow-mo) is featured in highlights and freefall.
    assert any(c.scene == "freefall" and c.src_start == 86.0 and c.speed_multiplier == 0.4
               for c in edls.highlights)
    assert any(c.scene == "freefall" and c.src_start == 86.0 and c.speed_multiplier == 0.4
               for c in edls.freefall)
    # The opening is the FINAL beat of the freefall cut.
    assert edls.freefall[-1].src_start >= 86.0
    # No freefall "best moment" is mined from the canopy descent past the opening beat:
    # the 95 s smile peak (under canopy) must be excluded.
    descent = [c for c in edls.freefall if c.scene == "freefall" and c.src_start > 89.0]
    assert descent == []


def test_resolve_outro_logo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG")
    monkeypatch.setenv("OUTRO_LOGO", str(logo))
    assert selfie.resolve_outro_logo() == logo

    monkeypatch.delenv("OUTRO_LOGO", raising=False)
    monkeypatch.setattr(selfie, "_DEFAULT_LOGO", tmp_path / "nope.png")
    assert selfie.resolve_outro_logo() is None


def test_compose_prompt_carries_booking_and_rules(tmp_path: Path) -> None:
    client = FakeClient(VALID_EDL_JSON)
    booking = {"customer_name": "Jane Doe", "jump_date": "2026-06-02"}
    selfie.compose_edls(
        _scores(), _manifest(), booking, "job1", tmp_path, client=client,
        target_duration=75.0,
    )
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "Jane Doe" in sent
    assert "full_video" in sent and "highlights" in sent and "freefall" in sent
    assert "75" in sent  # target duration reaches the model
    # The aircraft entry (staircase) is anchored to the head of boarding, even low-scoring.
    assert "aircraft ENTRY" in sent


def test_compose_prompt_scopes_freefall_to_aerial_window() -> None:
    # The freefall clip starts in the aircraft (exit 31 s) and runs through canopy
    # (deploy 75 s). The prompt must hand the model that window AND drop scored seconds
    # outside it, so an in-aircraft or under-canopy smile can't be picked as "freefall".
    manifest = {
        "scenes": [{
            "name": "freefall", "duration": 80.0, "combined_path": "/x/f.mp4",
            "exit_offset": 31.0, "deploy_offset": 75.0,
        }],
        "flagged": [],
    }
    scores = {"freefall": [
        {"ts": 10.0, "smile": 0.99, "eye_contact": 0.9, "face_in_frame": 1.0, "face_centered": 0.9},
        {"ts": 50.0, "smile": 0.80, "eye_contact": 0.7, "face_in_frame": 1.0, "face_centered": 0.8},
        {"ts": 78.0, "smile": 0.99, "eye_contact": 0.9, "face_in_frame": 1.0, "face_centered": 0.9},
    ]}
    prompt = selfie._build_compose_prompt(scores, manifest, {}, target_duration=75.0)
    after = prompt.split("Signals for this jump as JSON:")[1]
    payload = json.loads(after.rsplit("Return the JSON now.", 1)[0].strip())

    ff = next(s for s in payload["scenes"] if s["name"] == "freefall")
    assert ff["exit_offset"] == 31.0 and ff["deploy_offset"] == 75.0
    tops = {r["ts"] for r in ff["scored_seconds"]["top"]}
    assert 50.0 in tops                       # the real freefall second is offered
    assert 10.0 not in tops and 78.0 not in tops  # in-aircraft + canopy filtered out
    assert "ts 0 is NOT" in prompt             # the rule correcting the old assumption


def test_ensure_aircraft_entry_injects_when_model_drops_it() -> None:
    # The model returned NO boarding clip (the staircase entry scores low). The backstop
    # must inject it into full_video (in jump order) and highlights (after the intro),
    # while leaving the freefall cut alone.
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 20.0, "combined_path": "/x/i.mp4"},
            {"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 80.0, "combined_path": "/x/f.mp4"},
        ],
        "flagged": [],
    }
    edls = EDLResponse(
        full_video=[
            Clip(scene="intro_interview", src_start=0.0, src_end=5.0),
            Clip(scene="freefall", src_start=31.0, src_end=40.0),
        ],
        highlights=[
            Clip(scene="intro_interview", src_start=0.0, src_end=5.0),
            Clip(scene="freefall", src_start=31.0, src_end=33.0),
        ],
        freefall=[Clip(scene="freefall", src_start=31.0, src_end=33.0)],
    )
    out = selfie._ensure_aircraft_entry(edls, manifest)

    assert any(c.scene == "boarding" and c.src_start == 0.0 for c in out.full_video)
    assert any(c.scene == "boarding" and c.src_start == 0.0 for c in out.highlights)
    # full_video stays in jump order: intro → boarding → freefall.
    fv = [c.scene for c in out.full_video]
    assert fv.index("intro_interview") < fv.index("boarding") < fv.index("freefall")
    # The freefall cut is untouched (entry never belongs there).
    assert all(c.scene == "freefall" for c in out.freefall)


def test_ensure_aircraft_entry_is_noop_when_present() -> None:
    # Already has the boarding head (e.g. the offline house cut) → no duplicate added.
    manifest = {"scenes": [{"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"}],
                "flagged": []}
    edls = EDLResponse(
        full_video=[Clip(scene="boarding", src_start=0.0, src_end=3.0)],
        highlights=[Clip(scene="boarding", src_start=0.0, src_end=3.0)],
        freefall=[Clip(scene="freefall", src_start=0.0, src_end=2.0)],
    )
    out = selfie._ensure_aircraft_entry(edls, manifest)
    assert len([c for c in out.full_video if c.scene == "boarding"]) == 1
    assert len([c for c in out.highlights if c.scene == "boarding"]) == 1


def _full_journey_manifest() -> dict[str, Any]:
    """A jump with every scene present, plus a detected exit/deploy inside freefall."""
    return {
        "scenes": [
            {"name": "intro_interview", "duration": 20.0, "combined_path": "/x/i.mp4"},
            {"name": "boarding", "duration": 40.0, "combined_path": "/x/b.mp4"},
            {"name": "plane", "duration": 60.0, "combined_path": "/x/p.mp4"},
            {"name": "freefall", "duration": 90.0, "combined_path": "/x/f.mp4",
             "exit_offset": 30.0, "deploy_offset": 75.0},
            {"name": "canopy", "duration": 50.0, "combined_path": "/x/c.mp4"},
            {"name": "outro_interview", "duration": 15.0, "combined_path": "/x/o.mp4"},
        ],
        "flagged": [],
    }


def test_ensure_story_reorders_scrambled_clips_into_jump_order() -> None:
    # The model emitted a scrambled full video — landing (canopy) first, boarding after
    # freefall. _ensure_story must reorder it into the customer's chronological journey.
    manifest = _full_journey_manifest()
    edls = EDLResponse(
        full_video=[
            Clip(scene="canopy", src_start=45.0, src_end=50.0),         # landing — wrongly first
            Clip(scene="intro_interview", src_start=0.0, src_end=5.0),
            Clip(scene="freefall", src_start=30.0, src_end=45.0),
            Clip(scene="boarding", src_start=0.0, src_end=3.0),          # boarding — wrongly late
        ],
        highlights=[
            Clip(scene="canopy", src_start=45.0, src_end=48.0),         # landing first — the bug
            Clip(scene="intro_interview", src_start=0.0, src_end=4.0),
            Clip(scene="freefall", src_start=30.0, src_end=36.0),
        ],
        freefall=[Clip(scene="freefall", src_start=30.0, src_end=40.0)],
    )
    out = selfie._ensure_story(edls, manifest)

    order = [selfie._scene_rank(c.scene) for c in out.full_video]
    assert order == sorted(order)  # strictly non-decreasing jump order
    fv = [c.scene for c in out.full_video]
    assert (
        fv.index("intro_interview") < fv.index("boarding")
        < fv.index("freefall") < fv.index("canopy")
    )
    # Highlights: the landing no longer opens the cut; intro leads, canopy trails.
    assert out.highlights[0].scene == "intro_interview"
    assert out.highlights[-1].scene == "canopy"


def test_ensure_story_injects_missing_exit_canopy_and_landing() -> None:
    # The model dropped the exit/jump, the canopy opening, and the landing. The backstop
    # must inject all three into full_video and highlights, and the exit into freefall.
    manifest = _full_journey_manifest()
    edls = EDLResponse(
        full_video=[
            Clip(scene="intro_interview", src_start=0.0, src_end=5.0),
            Clip(scene="freefall", src_start=55.0, src_end=65.0),  # a mid-air smile only
        ],
        highlights=[
            Clip(scene="intro_interview", src_start=0.0, src_end=4.0),
            Clip(scene="freefall", src_start=55.0, src_end=58.0),
        ],
        freefall=[Clip(scene="freefall", src_start=55.0, src_end=60.0)],  # no exit!
    )
    out = selfie._ensure_story(edls, manifest)

    cdur = 50.0  # canopy duration; landing beat is its tail (cdur - _MILESTONE_S)
    for cut in (out.full_video, out.highlights):
        # Exit/jump present: a freefall clip starting at the detected exit (30.0).
        assert any(c.scene == "freefall" and abs(c.src_start - 30.0) < 0.5 for c in cut)
        # Canopy opening present: a canopy clip at the head of the canopy scene.
        assert any(c.scene == "canopy" and c.src_start < 1.0 for c in cut)
        # Landing present: a clip from the tail of the canopy scene.
        assert any(
            c.scene == "canopy" and c.src_start >= cdur - selfie._MILESTONE_S - 0.5
            for c in cut
        )
    # Freefall cut now leads with the exit.
    assert out.freefall[0].scene == "freefall"
    assert abs(out.freefall[0].src_start - 30.0) < 0.5


def test_ensure_story_is_noop_when_house_cut_already_correct() -> None:
    # The offline house cut already builds every milestone in order — _ensure_story must
    # not duplicate beats or reorder it.
    manifest, scores = _slowmo_manifest_scores()
    house = selfie._house_edls(scores, manifest, 90.0)
    out = selfie._ensure_story(house, manifest)
    assert [c.model_dump() for c in out.full_video] == [c.model_dump() for c in house.full_video]
    assert [c.model_dump() for c in out.freefall] == [c.model_dump() for c in house.freefall]


# --------------------------------------------------------------------------- #
# Step 4: render three outputs in parallel
# --------------------------------------------------------------------------- #


def test_render_outputs_per_deliverable_music_and_audio_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered: list[tuple[str, str | None, bool]] = []

    def fake_render(
        out_path: Any, clips: Any, scene_paths: Any, *,
        booking: Any, music_path: str | None = None, music_only: bool = True, **kw: Any,
    ) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00 ftyp")
        rendered.append((out.name, music_path, music_only))
        return out

    monkeypatch.setattr(selfie, "render_selfie_video", fake_render)

    edls = EDLResponse.model_validate_json(VALID_EDL_JSON)
    manifest = {
        "scenes": [{"name": "freefall", "combined_path": str(tmp_path / "freefall.mp4")}],
        "flagged": [],
    }
    outputs = selfie.render_outputs(
        "job1", edls, manifest, {}, tmp_path,
        music_paths={"full_video": "a.mp3", "highlights": "b.mp3", "freefall": "c.mp3"},
    )

    assert set(outputs) == {"full_video", "highlights", "freefall"}
    for path in outputs.values():
        assert Path(path).exists()
    by_name = {name: (mp, mo) for name, mp, mo in rendered}
    # Each deliverable: own track; full = cinematic mix, highlights/freefall = music-only.
    assert by_name["full_video.mp4"] == ("a.mp3", False)
    assert by_name["highlights.mp4"] == ("b.mp3", True)
    assert by_name["freefall.mp4"] == ("c.mp3", True)


def test_music_paths_prefers_uploaded_then_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from edl.storage import job_dir

    mdir = job_dir("job1", tmp_path) / "music"
    mdir.mkdir(parents=True)
    (mdir / "highlights.mp3").write_bytes(b"x")  # only highlights uploaded
    monkeypatch.setattr(selfie, "_resolve_music", lambda name: f"tmpl:{name}" if name else None)

    paths = selfie._music_paths({"music": "base"}, "job1", tmp_path)
    assert paths["highlights"].endswith("music/highlights.mp3")  # uploaded wins
    assert paths["full_video"] == "tmpl:base"   # not uploaded -> base template
    assert paths["freefall"] == "tmpl:base"
    # Backward compatible: template-only when no job_id is threaded through.
    assert selfie._music_paths({"music": "base"})["highlights"] == "tmpl:base"


def test_render_full_video_audio_is_cinematic_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # full video (music_only=False): music until canopy, original audio re-enabled at
    # canopy under ducked music; no stretched slow-mo audio.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(selfie, "_run_ffmpeg", lambda cmd: captured.setdefault("cmd", cmd))
    monkeypatch.setattr(selfie, "scene_has_audio", lambda p: True)
    monkeypatch.setattr(selfie, "_card_input", lambda *a, **k: str(tmp_path / "_outro.mp4"))

    clips = [
        selfie.Clip(scene="boarding", src_start=0.0, src_end=4.0),
        selfie.Clip(scene="freefall", src_start=10.0, src_end=11.0, speed_multiplier=0.4),
        selfie.Clip(scene="freefall", src_start=11.0, src_end=20.0),
        selfie.Clip(scene="canopy", src_start=0.0, src_end=5.0),
    ]
    scene_paths = {"boarding": "/x/b.mp4", "freefall": "/x/f.mp4", "canopy": "/x/c.mp4"}
    selfie.render_selfie_video(
        tmp_path / "out.mp4", clips, scene_paths,
        booking={}, music_path="/x/music.mp3", music_only=False,
    )
    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc                   # original audio mixed with music
    assert "volume='if(lt(t," in fc               # music ducks at canopy
    assert "[2:a]atrim=0.000:5.000" in fc          # canopy (idx 2) original audio re-enabled
    assert "atempo=" not in fc                     # never stretched slow-mo audio


def test_render_music_only_drops_original_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # highlights/freefall (music_only=True): NO original audio, constant solo music.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(selfie, "_run_ffmpeg", lambda cmd: captured.setdefault("cmd", cmd))
    monkeypatch.setattr(selfie, "scene_has_audio", lambda p: True)
    monkeypatch.setattr(selfie, "_card_input", lambda *a, **k: str(tmp_path / "_outro.mp4"))

    clips = [
        selfie.Clip(scene="freefall", src_start=0.0, src_end=4.0),
        selfie.Clip(scene="freefall", src_start=4.0, src_end=5.0, speed_multiplier=0.4),
    ]
    selfie.render_selfie_video(
        tmp_path / "out.mp4", clips, {"freefall": "/x/f.mp4"},
        booking={}, music_path="/x/music.mp3", music_only=True,
    )
    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "[0:a]atrim" not in fc                   # no original audio taken from the source
    assert "anullsrc" in fc                         # clips contribute silence
    assert "amix=inputs=2" in fc                    # silence + solo music
    assert "atempo=" not in fc                       # no stretched slow-mo audio


def test_clamp_clips_to_scenes_trims_overshoot_keeping_av_in_sync() -> None:
    # A clip whose src_end runs past the scene file's real duration is clamped so the
    # video trim can't end short of its audio (the cause of "video freezes, audio plays").
    monkey_durs = {"/x/f.mp4": 40.0, "/x/c.mp4": 12.0}
    import api.selfie as s

    orig = s.probe_duration
    s.probe_duration = lambda p: monkey_durs.get(str(p), 0.0)  # type: ignore[assignment]
    try:
        clips = [
            selfie.Clip(scene="freefall", src_start=10.0, src_end=80.0),   # 80 > 40 -> clamp
            selfie.Clip(scene="canopy", src_start=0.0, src_end=5.0),        # within 12 -> keep
            selfie.Clip(scene="freefall", src_start=50.0, src_end=60.0),    # fully past 40 -> drop
        ]
        out = s._clamp_clips_to_scenes(clips, {"freefall": "/x/f.mp4", "canopy": "/x/c.mp4"})
    finally:
        s.probe_duration = orig  # type: ignore[assignment]

    # Overshoot clamped to the real end; in-range clip untouched; out-of-range clip dropped.
    assert [(c.scene, c.src_start, c.src_end) for c in out] == [
        ("freefall", 10.0, 40.0),
        ("canopy", 0.0, 5.0),
    ]
    # Every kept clip's output length now matches what the audio side will build.
    assert all(c.src_end <= 40.0 for c in out if c.scene == "freefall")


def test_scene_playable_duration_uses_shorter_stream(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # A GoPro -c copy concat: the audio stream (13.82) outruns the video (13.20). The
    # playable duration is the VIDEO length — clamping to the container/audio would let a
    # video trim run past the video stream's end (frozen frame, audio keeps playing).
    monkeypatch.setattr(selfie, "_stream_durations", lambda p: (13.20, 13.82))
    assert selfie.scene_playable_duration("/x/scene.mp4") == 13.20
    # Falls back to the container duration when no per-stream duration is reported.
    monkeypatch.setattr(selfie, "_stream_durations", lambda p: (0.0, 0.0))
    monkeypatch.setattr(selfie, "probe_duration", lambda p: 42.0)
    assert selfie.scene_playable_duration("/x/scene.mp4") == 42.0


def test_clamp_uses_video_stream_not_container(monkeypatch: pytest.MonkeyPatch) -> None:
    # The clamp must trim to the playable (video) length, not the longer audio/container,
    # so the video trim window never exceeds the frames that actually exist.
    monkeypatch.setattr(selfie, "_stream_durations", lambda p: (13.20, 13.82))
    clips = [selfie.Clip(scene="freefall", src_start=0.0, src_end=13.80)]  # past video end
    out = selfie._clamp_clips_to_scenes(clips, {"freefall": "/x/f.mp4"})
    assert out[0].src_end == 13.20  # clamped to the video stream, not 13.80/13.82


def test_render_clamps_overshooting_clip_so_total_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end: an overshooting clip must be clamped before the filter is built, so the
    # video trim window never exceeds the scene duration that the audio side assumes.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(selfie, "_run_ffmpeg", lambda cmd: captured.setdefault("cmd", cmd))
    monkeypatch.setattr(selfie, "scene_has_audio", lambda p: True)
    monkeypatch.setattr(selfie, "_card_input", lambda *a, **k: str(tmp_path / "_outro.mp4"))
    monkeypatch.setattr(selfie, "probe_duration", lambda p: 30.0)  # freefall is really 30 s

    clips = [selfie.Clip(scene="freefall", src_start=0.0, src_end=120.0)]  # claims 120 s
    selfie.render_selfie_video(
        tmp_path / "out.mp4", clips, {"freefall": "/x/f.mp4"},
        booking={}, music_path=None, music_only=True,
    )
    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "trim=0.000:30.000" in fc       # clamped to the real 30 s, not 120
    assert "trim=0.000:120.000" not in fc
    # The output cap reflects the clamped length (30 s + the outro card).
    t_arg = captured["cmd"][captured["cmd"].index("-t") + 1]
    assert abs(float(t_arg) - (30.0 + selfie._CARD_SECONDS)) < 0.01


def test_audio_markers_finds_boarding_and_canopy() -> None:
    clips = [
        selfie.Clip(scene="intro_interview", src_start=0.0, src_end=10.0),  # 10 s
        selfie.Clip(scene="boarding", src_start=0.0, src_end=5.0),           # music starts @10
        selfie.Clip(scene="freefall", src_start=0.0, src_end=20.0),
        selfie.Clip(scene="canopy", src_start=0.0, src_end=8.0),             # calmer @35
    ]
    music_start, canopy_start = selfie._audio_markers(clips)
    assert music_start == 10.0
    assert canopy_start == 35.0


def test_audio_markers_canopy_inside_freefall_via_deploy() -> None:
    # No canopy scene: the canopy ride is part of the freefall clip. The original audio
    # must come back where the freefall reaches the detected deployment offset (86 s).
    clips = [
        selfie.Clip(scene="boarding", src_start=0.0, src_end=5.0),    # music starts @0
        selfie.Clip(scene="freefall", src_start=4.0, src_end=86.0),   # freefall, ends 5+82=87
        selfie.Clip(scene="freefall", src_start=86.0, src_end=110.0),  # canopy ride @87
    ]
    music_start, canopy_start = selfie._audio_markers(clips, deploy_offset=86.0)
    assert music_start == 0.0
    assert canopy_start == 87.0  # output time where the freefall clip reaches deploy 86


# --------------------------------------------------------------------------- #
# Step 5: photo extraction
# --------------------------------------------------------------------------- #


def _patch_photo_seams(
    monkeypatch: pytest.MonkeyPatch, scores: dict[str, list[dict[str, float]]]
) -> None:
    """Fake the JPEG dump (one file per scored second) and a constant quality."""

    def fake_dump(scene_path: Any, out_dir: Path, *, fps: float = 1.0) -> list[tuple[int, Path]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for r in scores.get(out_dir.name, []):
            ts = int(r["ts"])
            p = out_dir / f"frame_{ts:05d}.jpg"
            p.write_bytes(b"jpg")
            frames.append((ts, p))
        return frames

    monkeypatch.setattr(selfie, "dump_scene_jpegs", fake_dump)
    monkeypatch.setattr(selfie, "frame_quality", lambda jpg: (100.0, 1.0))


def test_extract_photos_delivers_55plus_distributed_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long freefall (160 s) and a short intro (20 s), all high quality -> well over
    # 55 de-duplicated candidates available; deliver them all (under the cap).
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9, "eye_contact": 0.8,
             "face_in_frame": 1.0, "face_centered": 0.9}
            for t in range(160)
        ],
        "intro_interview": [
            {"ts": float(t), "smile": 0.9, "eye_contact": 0.8,
             "face_in_frame": 1.0, "face_centered": 0.9}
            for t in range(20)
        ],
    }
    manifest = {
        "scenes": [
            {"name": "intro_interview", "combined_path": "/x/intro.mp4"},
            {"name": "freefall", "combined_path": "/x/freefall.mp4"},
        ],
        "flagged": [],
    }
    _patch_photo_seams(monkeypatch, scores)

    index = selfie.extract_photos("job1", scores, manifest, tmp_path)
    assert len(index) >= 55           # the 55+ aim is met when footage allows
    assert len(index) <= selfie.MAX_PHOTOS
    # Distributed: NOT all from freefall — intro contributes too.
    assert {e["scene"] for e in index} == {"freefall", "intro_interview"}
    # De-duplicated in time: freefall picks are >= 2 s apart.
    ff_ts = sorted(e["ts"] for e in index if e["scene"] == "freefall")
    assert all(b - a >= 2.0 for a, b in zip(ff_ts, ff_ts[1:], strict=False))
    assert "score" in index[0] and "sharpness" in index[0]
    on_disk = list((tmp_path / "job1" / "photos").glob("*.jpg"))
    assert len(on_disk) == len(index)


def test_extract_photos_backfill_reaches_target_on_faceless_footage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distant camera-flyer ("external") footage: MediaPipe locks onto no face, so EVERY
    # second scores 0 on face_in_frame. Face-gated selection returns almost nothing;
    # backfill ranks the whole jump by image quality and still fills the ~50 target.
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.0, "eye_contact": 0.0,
             "face_in_frame": 0.0, "face_centered": 0.0}
            for t in range(120)
        ]
    }
    manifest = {"scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}], "flagged": []}
    _patch_photo_seams(monkeypatch, scores)

    # Face-gated (no backfill): nothing survives the in-frame floor.
    assert selfie.extract_photos("gated", scores, manifest, tmp_path) == []
    # Backfill: the set still fills to the target from image quality alone.
    index = selfie.extract_photos(
        "backfilled", scores, manifest, tmp_path,
        target=selfie.SELFIE_PHOTO_TARGET,
        min_gap=selfie._SELFIE_PHOTO_MIN_GAP_S,
        min_visible=selfie._SELFIE_PHOTO_MIN_VISIBLE,
        backfill=True,
    )
    assert len(index) == selfie.SELFIE_PHOTO_TARGET


def test_extract_photos_backfill_prefers_faces_then_fills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A few seconds show a clear face; the rest don't. Backfill must keep the face shots
    # AND top up with faceless frames to reach the target — faces ranked first.
    scores = {
        "freefall": [
            {"ts": float(t),
             "smile": 0.9 if t < 5 else 0.0, "eye_contact": 0.9 if t < 5 else 0.0,
             "face_in_frame": 1.0 if t < 5 else 0.0, "face_centered": 0.9 if t < 5 else 0.0}
            for t in range(80)
        ]
    }
    manifest = {"scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}], "flagged": []}
    _patch_photo_seams(monkeypatch, scores)
    index = selfie.extract_photos(
        "job1", scores, manifest, tmp_path,
        target=selfie.SELFIE_PHOTO_TARGET, min_gap=selfie._SELFIE_PHOTO_MIN_GAP_S,
        backfill=True,
    )
    assert len(index) == selfie.SELFIE_PHOTO_TARGET
    # The five face seconds (ts 0–4) are all kept and rank at the very top (best-first).
    assert {e["ts"] for e in index} >= {0.0, 1.0, 2.0, 3.0, 4.0}
    assert {e["ts"] for e in index[:5]} == {0.0, 1.0, 2.0, 3.0, 4.0}


def test_extract_photos_respects_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Far more candidates than the cap -> capped at MAX_PHOTOS.
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9, "eye_contact": 0.8,
             "face_in_frame": 1.0, "face_centered": 0.9}
            for t in range(600)
        ]
    }
    manifest = {"scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}], "flagged": []}
    _patch_photo_seams(monkeypatch, scores)
    index = selfie.extract_photos("job1", scores, manifest, tmp_path)
    assert len(index) == selfie.MAX_PHOTOS


def test_extract_photos_photo_only_target_delivers_90_to_100(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The photo-only package raises the target to 90–100. With ample high-quality
    # footage and the tighter 1 s gap, extract_photos fills right up to the target.
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9, "eye_contact": 0.8,
             "face_in_frame": 1.0, "face_centered": 0.9}
            for t in range(200)
        ],
        "canopy": [
            {"ts": float(t), "smile": 0.7, "eye_contact": 0.7,
             "face_in_frame": 1.0, "face_centered": 0.8}
            for t in range(60)
        ],
    }
    manifest = {
        "scenes": [
            {"name": "freefall", "combined_path": "/x/freefall.mp4"},
            {"name": "canopy", "combined_path": "/x/canopy.mp4"},
        ],
        "flagged": [],
    }
    _patch_photo_seams(monkeypatch, scores)

    index = selfie.extract_photos(
        "job1", scores, manifest, tmp_path,
        target=selfie.PHOTO_ONLY_TARGET, min_gap=selfie._PHOTO_ONLY_MIN_GAP_S,
    )
    assert 90 <= len(index) <= selfie.PHOTO_ONLY_TARGET
    # Tighter 1 s gap honoured within a scene.
    ff_ts = sorted(e["ts"] for e in index if e["scene"] == "freefall")
    assert all(b - a >= 1.0 for a, b in zip(ff_ts, ff_ts[1:], strict=False))


def test_extract_photos_covers_all_scenes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Photos are mined from every phase, not just freefall/canopy.
    good = {"smile": 0.9, "eye_contact": 0.8, "face_in_frame": 1.0, "face_centered": 0.9}
    scores = {
        "intro_interview": [{"ts": 0.0, **good}],
        "plane": [{"ts": 0.0, **good}],
        "freefall": [{"ts": 0.0, **good}],
        "outro_interview": [{"ts": 0.0, **good}],
    }
    manifest = {
        "scenes": [
            {"name": "intro_interview", "combined_path": "/x/intro.mp4"},
            {"name": "plane", "combined_path": "/x/plane.mp4"},
            {"name": "freefall", "combined_path": "/x/freefall.mp4"},
            {"name": "outro_interview", "combined_path": "/x/outro.mp4"},
        ],
        "flagged": [],
    }
    _patch_photo_seams(monkeypatch, scores)

    index = selfie.extract_photos("job1", scores, manifest, tmp_path)
    assert {e["scene"] for e in index} == {
        "intro_interview", "plane", "freefall", "outro_interview"
    }


def test_extract_photos_filters_low_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scores = {
        "freefall": [
            {"ts": 0.0, "smile": 0.9, "eye_contact": 0.8,
             "face_in_frame": 1.0, "face_centered": 0.9},
            # face barely in frame -> below _PHOTO_MIN_VISIBLE -> dropped.
            {"ts": 5.0, "smile": 0.9, "eye_contact": 0.8,
             "face_in_frame": 0.1, "face_centered": 0.9},
        ]
    }
    manifest = {"scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}], "flagged": []}
    _patch_photo_seams(monkeypatch, scores)

    index = selfie.extract_photos("job1", scores, manifest, tmp_path)
    assert [e["ts"] for e in index] == [0.0]  # only the visible-face second


# --------------------------------------------------------------------------- #
# Orchestrator: end-to-end with every step mocked
# --------------------------------------------------------------------------- #


def test_run_selfie_pipeline_drives_status_and_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path)
    from api.jobs import Job

    store.create(Job(job_id="job1", customer_name="Jane", package=Package.selfie))
    store.write_booking("job1", {"customer_name": "Jane", "music": None})
    store.raw_dir("job1").mkdir(parents=True, exist_ok=True)
    (store.raw_dir("job1") / "GH010001.MP4").write_bytes(b"x")

    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(selfie, "classify_files", lambda raw: [("freefall", _sig())])
    manifest = {"scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}], "flagged": []}
    monkeypatch.setattr(selfie, "build_scenes", lambda *a, **k: manifest)
    monkeypatch.setattr(selfie, "score_scenes", lambda *a, **k: {"freefall": []})
    monkeypatch.setattr(
        selfie, "compose_edls",
        lambda *a, **k: EDLResponse.model_validate_json(VALID_EDL_JSON),
    )
    monkeypatch.setattr(
        selfie, "render_outputs",
        lambda *a, **k: {
            "full_video": "/jobs/job1/full_video.mp4",
            "highlights": "/jobs/job1/highlights.mp4",
            "freefall": "/jobs/job1/freefall.mp4",
        },
    )
    monkeypatch.setattr(selfie, "extract_photos", lambda *a, **k: [])

    outputs = selfie.run_selfie_pipeline("job1", store=store, jobs_root=tmp_path)

    assert set(outputs) == {"full_video", "highlights", "freefall", "photos"}
    job = store.load("job1")
    assert job.status == JobStatus.ready
    assert job.outputs == outputs


def _patch_pipeline_stages(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub every heavy stage of run_selfie_pipeline; record which ones ran."""
    ran: dict[str, list[Any]] = {"compose": [], "render": [], "photos": []}

    manifest = {
        "scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}],
        "flagged": [],
    }
    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(selfie, "classify_files", lambda raw: [("freefall", _sig())])
    monkeypatch.setattr(selfie, "build_scenes", lambda *a, **k: manifest)
    monkeypatch.setattr(selfie, "score_scenes", lambda *a, **k: {"freefall": []})

    def fake_compose(*a: Any, **k: Any) -> EDLResponse:
        ran["compose"].append(a)
        return EDLResponse.model_validate_json(VALID_EDL_JSON)

    def fake_render(*a: Any, **k: Any) -> dict[str, str]:
        ran["render"].append(a)
        return {
            "full_video": "/jobs/job1/full_video.mp4",
            "highlights": "/jobs/job1/highlights.mp4",
            "freefall": "/jobs/job1/freefall.mp4",
        }

    def fake_photos(*a: Any, **k: Any) -> list[Any]:
        ran["photos"].append(k)
        return []

    monkeypatch.setattr(selfie, "compose_edls", fake_compose)
    monkeypatch.setattr(selfie, "render_outputs", fake_render)
    monkeypatch.setattr(selfie, "extract_photos", fake_photos)
    return ran


def test_run_pipeline_video_only_skips_photos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # video_only: the three videos, no photos and no "photos" output key.
    store = JobStore(tmp_path)
    from api.jobs import Job

    store.create(Job(job_id="job1", package=Package.video_only))
    store.write_booking("job1", {"customer_name": "Jane", "music": None})
    store.raw_dir("job1").mkdir(parents=True, exist_ok=True)

    ran = _patch_pipeline_stages(monkeypatch)
    outputs = selfie.run_selfie_pipeline("job1", store=store, jobs_root=tmp_path)

    assert set(outputs) == {"full_video", "highlights", "freefall"}
    assert ran["compose"] and ran["render"]  # videos were produced
    assert ran["photos"] == []               # photos were skipped
    assert store.load("job1").status == JobStatus.ready


def test_run_pipeline_photo_only_skips_videos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # photo_only: only photos — no Claude compose call, no renders, only a "photos" key.
    store = JobStore(tmp_path)
    from api.jobs import Job

    store.create(Job(job_id="job1", package=Package.photo_only))
    store.write_booking("job1", {"customer_name": "Jane", "music": None})
    store.raw_dir("job1").mkdir(parents=True, exist_ok=True)

    ran = _patch_pipeline_stages(monkeypatch)
    outputs = selfie.run_selfie_pipeline("job1", store=store, jobs_root=tmp_path)

    assert set(outputs) == {"photos"}
    assert ran["compose"] == [] and ran["render"] == []  # no videos, no Claude call
    assert ran["photos"]                                  # photos were produced
    # The photo-only set targets the fuller 90–100 range, not the selfie default cap.
    assert ran["photos"][0]["target"] == selfie.PHOTO_ONLY_TARGET
    assert store.load("job1").status == JobStatus.ready


def test_run_selfie_pipeline_low_confidence_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path)
    from api.jobs import Job

    store.create(Job(job_id="job1", package=Package.selfie))
    store.write_booking("job1", {"customer_name": "X"})
    store.raw_dir("job1").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)

    def boom(raw: Any) -> Any:
        raise LowConfidenceError("3 unknown")

    monkeypatch.setattr(selfie, "classify_files", boom)
    with pytest.raises(LowConfidenceError):
        selfie.run_selfie_pipeline("job1", store=store, jobs_root=tmp_path)


def test_apply_exclusions_cuts_ranges_from_all_deliverables() -> None:
    edls = EDLResponse(
        full_video=[selfie.Clip(scene="intro_interview", src_start=0.0, src_end=60.0)],
        highlights=[selfie.Clip(scene="intro_interview", src_start=10.0, src_end=50.0)],
        freefall=[selfie.Clip(scene="freefall", src_start=0.0, src_end=10.0)],
    )
    out = selfie.apply_exclusions(edls, {"intro_interview": [(20.0, 40.0)]})
    # The 20-40 s window is cut out of every intro clip (split around it).
    assert [(c.src_start, c.src_end) for c in out.full_video] == [(0.0, 20.0), (40.0, 60.0)]
    assert [(c.src_start, c.src_end) for c in out.highlights] == [(10.0, 20.0), (40.0, 50.0)]
    # Scenes with no exclusion are untouched.
    assert [(c.src_start, c.src_end) for c in out.freefall] == [(0.0, 10.0)]


def test_apply_exclusions_never_empties_a_deliverable() -> None:
    # Excluding a clip entirely would empty the list; the EDL needs >= 1 clip, so the
    # originals are kept instead.
    edls = EDLResponse(
        full_video=[selfie.Clip(scene="canopy", src_start=0.0, src_end=5.0)],
        highlights=[selfie.Clip(scene="freefall", src_start=0.0, src_end=5.0)],
        freefall=[selfie.Clip(scene="freefall", src_start=0.0, src_end=5.0)],
    )
    out = selfie.apply_exclusions(edls, {"canopy": [(0.0, 5.0)]})
    assert len(out.full_video) == 1  # not emptied


def test_load_exclusions_reads_file(tmp_path: Path) -> None:
    import json as _json

    jd = tmp_path / "job1"
    jd.mkdir(parents=True)
    # boarding's 5-5 span is empty -> dropped.
    (jd / "exclude.json").write_text(
        _json.dumps({"intro_interview": [[20, 40]], "boarding": [[5, 5]]})
    )
    excl = selfie.load_exclusions("job1", tmp_path)
    assert excl == {"intro_interview": [(20.0, 40.0)]}
    assert selfie.load_exclusions("nope", tmp_path) == {}


def test_capture_and_learn_style_profile(tmp_path: Path) -> None:
    import json as _json

    jd = tmp_path / "job1"
    jd.mkdir(parents=True)
    full = [
        {"scene": "intro_interview", "src_start": 0, "src_end": 15, "speed_multiplier": 1.0},
        {"scene": "freefall", "src_start": 0, "src_end": 40, "speed_multiplier": 1.0},
    ]
    freefall = [
        {"scene": "freefall", "src_start": 0, "src_end": 3, "speed_multiplier": 0.4},
        {"scene": "freefall", "src_start": 10, "src_end": 12, "speed_multiplier": 1.0},
        {"scene": "freefall", "src_start": 20, "src_end": 22, "speed_multiplier": 1.0},
    ]
    (jd / "edl_full.json").write_text(_json.dumps(full))
    (jd / "edl_highlights.json").write_text(_json.dumps(full))
    (jd / "edl_freefall.json").write_text(_json.dumps(freefall))

    exemplar = selfie.capture_exemplar("job1", tmp_path)
    assert exemplar["scene_seconds"]["intro_interview"] == 15.0
    assert exemplar["freefall_beats"] == 3

    profile = selfie.learn_style_profile(tmp_path)
    assert profile["samples"] == 1
    assert profile["scene_seconds"]["intro_interview"] == 15.0
    assert profile["freefall_beats"] == 3
    assert selfie.load_style_profile(tmp_path) == profile
    assert selfie.load_style_profile(tmp_path / "empty") == {}


def test_house_edls_applies_learned_profile() -> None:
    manifest = {
        "scenes": [
            {"name": "intro_interview", "duration": 70.0, "combined_path": "/x/i.mp4"},
            {"name": "freefall", "duration": 70.0, "combined_path": "/x/f.mp4"},
            {"name": "canopy", "duration": 18.0, "combined_path": "/x/c.mp4"},
        ],
        "flagged": [],
    }
    scores = {
        "freefall": [
            {"ts": float(t), "smile": 0.9 if t % 10 == 0 else 0.3, "eye_contact": 0.7,
             "face_in_frame": 1.0, "face_centered": 0.8}
            for t in range(70)
        ]
    }
    # Learned: keep intro to ~12 s, feature just 2 freefall beats.
    profile = {"scene_seconds": {"intro_interview": 12.0}, "freefall_beats": 2}
    edls = selfie._house_edls(scores, manifest, 90.0, profile=profile)

    intro = sum(selfie._clip_out_dur(c) for c in edls.full_video if c.scene == "intro_interview")
    assert intro == pytest.approx(12.0, abs=0.2)  # trimmed to the learned length
    # Freefall video features the learned number of AI beats (exit seq + 2 beats + deploy).
    ai_beats = [c for c in edls.freefall if c.scene == "freefall" and c.src_start >= 12.0]
    # The exit sequence + 2 score beats -> a small, bounded number of post-exit windows.
    assert len(ai_beats) <= 8


def test_replay_selfie_rerenders_from_edited_edls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The instructor edits edl_freefall.json (e.g. moves where the exit starts); replay
    # re-renders from those exact clips — no re-classification or re-scoring.
    import json as _json

    from api.jobs import Job

    store = JobStore(tmp_path)
    store.create(Job(job_id="job1", package=Package.selfie))
    store.write_booking("job1", {"customer_name": "Jane", "music": None})
    jd = tmp_path / "job1"
    (jd / "scene_manifest.json").write_text(
        _json.dumps({"scenes": [{"name": "freefall", "combined_path": "/x/freefall.mp4"}],
                     "flagged": []})
    )
    # Hand-edited EDLs: a custom exit window (freefall 7.0 → 20.0).
    edited = [{"scene": "freefall", "src_start": 7.0, "src_end": 20.0, "speed_multiplier": 1.0}]
    for name in ("edl_full.json", "edl_highlights.json", "edl_freefall.json"):
        (jd / name).write_text(_json.dumps(edited))

    captured: dict[str, Any] = {}

    def fake_render(out_path: Any, clips: Any, *a: Any, **k: Any) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00 ftyp")
        captured[out.name] = list(clips)
        return out

    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(selfie, "render_selfie_video", fake_render)

    outputs = selfie.replay_selfie("job1", store=store, jobs_root=tmp_path)

    assert set(outputs) >= {"full_video", "highlights", "freefall"}
    # The edited exit window reached the renderer verbatim.
    ff_clips = captured["freefall.mp4"]
    assert ff_clips[0].src_start == 7.0 and ff_clips[0].src_end == 20.0
    assert store.load("job1").status == JobStatus.ready


# --------------------------------------------------------------------------- #
# The Celery task wrapper (process_selfie_package)
# --------------------------------------------------------------------------- #


def test_process_selfie_task_runs_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job

    store = JobStore(tmp_path)
    store.create(Job(job_id="job1", package=Package.selfie))
    monkeypatch.setattr(tasks, "_store", lambda: store)

    ran: list[str] = []

    def fake_run(job_id: str, *, store: Any, jobs_root: Any = None, client: Any = None) -> dict:
        ran.append(job_id)
        store.update(job_id, status=JobStatus.ready, outputs={"full_video": "x"})
        return {"full_video": "x"}

    monkeypatch.setattr(selfie, "run_selfie_pipeline", fake_run)
    assert tasks.process_selfie_package("job1") == "job1"
    assert ran == ["job1"]
    assert store.load("job1").status == JobStatus.ready


def test_process_selfie_task_marks_failed_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job

    store = JobStore(tmp_path)
    store.create(Job(job_id="job1", package=Package.selfie))
    monkeypatch.setattr(tasks, "_store", lambda: store)

    def boom(*a: Any, **k: Any) -> Any:
        raise LowConfidenceError("3 unknown clips")

    monkeypatch.setattr(selfie, "run_selfie_pipeline", boom)
    with pytest.raises(LowConfidenceError):
        tasks.process_selfie_package("job1")
    job = store.load("job1")
    assert job.status == JobStatus.failed
    assert "3 unknown clips" in (job.error or "")
