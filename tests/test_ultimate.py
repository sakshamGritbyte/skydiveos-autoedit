"""Tests for the two-camera Ultimate package (api/selfie.run_ultimum_pipeline).

The Ultimate package reuses the selfie editing logic on different footage sets, so
these tests focus on the *new* behavior: combining both cameras for the full video +
highlights, building each freefall cut from one camera alone, the music-only audio
rules, and the per-camera scene-set wiring. Every heavy stage (classify / build /
score / compose / curate / render) is monkeypatched so the suite runs fully offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api import selfie
from api.jobs import Job, JobStatus, JobStore, Package
from api.selfie import Clip, EDLResponse, FileSignals, GpmfSignals

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


def _sig(*, filename: str = "GH010001.MP4", has_altitude: bool = False) -> FileSignals:
    return FileSignals(
        filename=filename,
        path=f"/raw/{filename}",
        chapter=1,
        duration=10.0,
        gpmf=GpmfSignals(
            altitude_mean=0.0, altitude_first=0.0, altitude_last=0.0, altitude_delta=0.0,
            accl_z_mean=1.0, accl_z_std=0.1, accl_mag_mean=1.0, accl_mag_std=0.0,
            accl_mag_min=1.0, has_altitude=has_altitude,
        ),
    )


def _manifest() -> dict[str, Any]:
    return {
        "scenes": [
            {"name": "freefall", "combined_path": "/x/freefall.mp4", "duration": 30.0}
        ],
        "flagged": [],
    }


def _new_ultimum_job(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path)
    store.create(Job(job_id="job1", customer_name="Jane", package=Package.ultimum))
    store.write_booking("job1", {"customer_name": "Jane", "music": None})
    for role in selfie.CAMERA_ROLES:
        d = store.camera_raw_dir("job1", role)
        d.mkdir(parents=True, exist_ok=True)
        (d / "GH010001.MP4").write_bytes(b"x")  # same name in both — the collision case
    return store


# --------------------------------------------------------------------------- #
# Package routing
# --------------------------------------------------------------------------- #


def test_ultimum_package_routing_flags() -> None:
    p = Package.ultimum
    assert p.uses_scene_pipeline is True   # goes through the scene queue
    assert p.is_ultimum is True
    assert p.makes_videos is False         # not the standard three-video render
    assert p.makes_photos is False         # no photo set for the ultimate product


def test_ultimum_music_deliverables() -> None:
    assert Package.ultimum.music_deliverables == (
        "full_video", "highlights", "external_freefall", "chute_libre_selfie"
    )


def test_ultimum_music_paths_prefers_uploaded_then_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from edl.storage import job_dir

    mdir = job_dir("J", tmp_path) / "music"
    mdir.mkdir(parents=True)
    (mdir / "external_freefall.mp3").write_bytes(b"x")  # only one deliverable uploaded
    monkeypatch.setattr(selfie, "_resolve_music", lambda name: f"tmpl:{name}" if name else None)

    paths = selfie._ultimum_music_paths({"music": "base"}, "J", tmp_path)
    assert paths["external_freefall"].endswith("music/external_freefall.mp3")  # uploaded wins
    assert paths["full_video"] == "tmpl:base"        # not uploaded -> base template
    assert paths["chute_libre_selfie"] == "tmpl:base"
    # Backward compatible: no job_id -> template/base only (no uploaded lookup).
    assert selfie._ultimum_music_paths({"music": "base"})["full_video"] == "tmpl:base"


def test_run_selfie_pipeline_delegates_ultimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scene-pipeline entrypoint hands an ultimum job straight to its orchestrator.
    store = JobStore(tmp_path)
    store.create(Job(job_id="job1", package=Package.ultimum))

    called: dict[str, Any] = {}

    def fake_ultimum(job_id: str, **kw: Any) -> dict[str, str]:
        called["args"] = (job_id, kw)
        return {"full_video": "x"}

    monkeypatch.setattr(selfie, "run_ultimum_pipeline", fake_ultimum)
    out = selfie.run_selfie_pipeline("job1", store=store, jobs_root=tmp_path)
    assert out == {"full_video": "x"}
    assert called["args"][0] == "job1"


# --------------------------------------------------------------------------- #
# classify_camera_files
# --------------------------------------------------------------------------- #


def test_classify_camera_files_gathers_from_all_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst, ext = tmp_path / "instructor", tmp_path / "external"
    for d, name in ((inst, "GH010001.MP4"), (ext, "GH010001.MP4")):
        d.mkdir(parents=True)
        (d / name).write_bytes(b"x")
    monkeypatch.setattr(selfie, "build_file_signals", lambda p: _sig(filename=Path(p).name))

    classified = selfie.classify_camera_files([inst, ext])
    assert len(classified) == 2  # both cameras' clips gathered despite identical names


def test_classify_camera_files_no_mp4s_raises(tmp_path: Path) -> None:
    with pytest.raises(selfie.SelfieError):
        selfie.classify_camera_files([tmp_path / "instructor", tmp_path / "external"])


# --------------------------------------------------------------------------- #
# run_ultimum_pipeline
# --------------------------------------------------------------------------- #


def _patch_ultimum_stages(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub every heavy stage; record classify dirs and render calls."""
    rec: dict[str, list[Any]] = {"classify": [], "render": [], "build": []}

    def fake_classify(raw_dirs: Any, **kw: Any) -> Any:
        rec["classify"].append([str(d) for d in raw_dirs])
        return [("freefall", _sig())]

    def fake_build(*a: Any, **k: Any) -> Any:
        rec["build"].append(k.get("scenes_subdir", "scenes"))
        return _manifest()

    def fake_render(out: Any, clips: Any, scene_paths: Any, **kw: Any) -> Path:
        rec["render"].append({"out": Path(out).name, "music_only": kw.get("music_only")})
        Path(out).write_bytes(b"video")
        return Path(out)

    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(selfie, "classify_camera_files", fake_classify)
    monkeypatch.setattr(selfie, "build_scenes", fake_build)
    monkeypatch.setattr(selfie, "score_scenes", lambda *a, **k: {"freefall": []})
    monkeypatch.setattr(
        selfie, "compose_edls",
        lambda *a, **k: EDLResponse.model_validate_json(VALID_EDL_JSON),
    )
    monkeypatch.setattr(
        selfie, "_curated_freefall",
        lambda scenes, scores, **k: [Clip(scene="freefall", src_start=0.0, src_end=5.0)],
    )
    monkeypatch.setattr(selfie, "render_selfie_video", fake_render)
    monkeypatch.setattr(selfie, "extract_photos", lambda *a, **k: [])
    return rec


def test_run_ultimum_pipeline_produces_five_deliverables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_ultimum_job(tmp_path)
    _patch_ultimum_stages(monkeypatch)

    outputs = selfie.run_ultimum_pipeline("job1", store=store, jobs_root=tmp_path)

    # Four videos + the photo set (photos reuse extract_photos over the combined scenes).
    assert set(outputs) == {
        "full_video", "highlights", "external_freefall", "chute_libre_selfie", "photos"
    }
    job = store.load("job1")
    assert job.status == JobStatus.ready
    assert job.outputs == outputs


def test_run_ultimum_combo_interleaves_both_cameras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The combo full video + highlights must feature BOTH cameras — every clip is tagged
    # with its camera, and both instructor and external appear (the cameraman is no longer
    # dropped). They also interleave rather than playing one camera then the other.
    store = _new_ultimum_job(tmp_path)
    _patch_ultimum_stages(monkeypatch)

    selfie.run_ultimum_pipeline("job1", store=store, jobs_root=tmp_path)

    jd = store.dir("job1")
    full = json.loads((jd / selfie.ULTIMUM_EDL_FILES["full_video"]).read_text())
    cameras = {c["camera"] for c in full}
    assert cameras == {"instructor", "external"}      # both angles present
    # Interleaved: the camera changes from clip to clip rather than all-instructor then
    # all-external.
    seq = [c["camera"] for c in full]
    assert seq[0] != seq[1]
    # Same guarantee for the highlights deliverable.
    highs = json.loads((jd / selfie.ULTIMUM_EDL_FILES["highlights"]).read_text())
    assert {c["camera"] for c in highs} == {"instructor", "external"}


def test_run_ultimum_audio_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Combo full = cinematic mix (music + original); the other three = music only.
    store = _new_ultimum_job(tmp_path)
    rec = _patch_ultimum_stages(monkeypatch)

    selfie.run_ultimum_pipeline("job1", store=store, jobs_root=tmp_path)

    music_only = {r["out"]: r["music_only"] for r in rec["render"]}
    assert music_only["full_video.mp4"] is False
    assert music_only["highlights.mp4"] is True
    assert music_only["external_freefall.mp4"] is True
    assert music_only["chute_libre_selfie.mp4"] is True


def test_run_ultimum_classifies_each_camera_into_its_own_scene_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No combined concat: each camera is classified from its OWN raw dir into its OWN
    # scene set (the substrate for both the combo and that camera's freefall cut).
    store = _new_ultimum_job(tmp_path)
    rec = _patch_ultimum_stages(monkeypatch)

    selfie.run_ultimum_pipeline("job1", store=store, jobs_root=tmp_path)

    # Each classify call globs exactly one camera's dir (never both together).
    assert all(len(dirs) == 1 for dirs in rec["classify"])
    classified_dirs = {dirs[0] for dirs in rec["classify"]}
    assert classified_dirs == {
        str(store.camera_raw_dir("job1", "instructor")),
        str(store.camera_raw_dir("job1", "external")),
    }
    # ...and each camera builds its own isolated scene set.
    assert "scenes_external" in rec["build"]
    assert "scenes_instructor" in rec["build"]
    assert "scenes" not in rec["build"]  # no combined scene set is built


def test_run_ultimum_persists_reeditable_edls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_ultimum_job(tmp_path)
    _patch_ultimum_stages(monkeypatch)

    selfie.run_ultimum_pipeline("job1", store=store, jobs_root=tmp_path)

    jd = store.dir("job1")
    for filename in selfie.ULTIMUM_EDL_FILES.values():
        assert (jd / filename).exists(), f"{filename} should be persisted for replay/tweak"


def test_run_ultimum_marks_failed_inputs_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A low-confidence classification surfaces (the task wrapper flips the job failed).
    store = _new_ultimum_job(tmp_path)
    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)

    def boom(*a: Any, **k: Any) -> Any:
        raise selfie.LowConfidenceError("3 unknown")

    monkeypatch.setattr(selfie, "classify_camera_files", boom)
    with pytest.raises(selfie.LowConfidenceError):
        selfie.run_ultimum_pipeline("job1", store=store, jobs_root=tmp_path)


# --------------------------------------------------------------------------- #
# replay_ultimum
# --------------------------------------------------------------------------- #


def test_replay_ultimum_rerenders_from_persisted_edls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_ultimum_job(tmp_path)
    jd = store.dir("job1")
    # Persist the four EDLs + the scene manifests a first run would have written.
    clip = [{"scene": "freefall", "src_start": 0, "src_end": 5, "speed_multiplier": 1.0}]
    for filename in selfie.ULTIMUM_EDL_FILES.values():
        (jd / filename).write_text(json.dumps(clip) + "\n")
    (jd / "scene_manifest.json").write_text(json.dumps(_manifest()) + "\n")
    for role in selfie.CAMERA_ROLES:
        (jd / selfie._ULTIMUM_ROLE_MANIFEST[role]).write_text(json.dumps(_manifest()) + "\n")

    rec: list[dict[str, Any]] = []
    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        selfie, "render_selfie_video",
        lambda out, clips, sp, **kw: rec.append(
            {"out": Path(out).name, "music_only": kw.get("music_only")}
        ) or Path(out),
    )

    outputs = selfie.replay_ultimum("job1", store=store, jobs_root=tmp_path)

    assert set(outputs) == {
        "full_video", "highlights", "external_freefall", "chute_libre_selfie"
    }
    music_only = {r["out"]: r["music_only"] for r in rec}
    assert music_only["full_video.mp4"] is False
    assert all(music_only[k] is True for k in
               ("highlights.mp4", "external_freefall.mp4", "chute_libre_selfie.mp4"))
    assert store.load("job1").status == JobStatus.ready


# --------------------------------------------------------------------------- #
# Multi-cam combo building blocks
# --------------------------------------------------------------------------- #


def _cam_manifest() -> dict[str, Any]:
    return {
        "scenes": [
            {"name": "boarding", "duration": 20.0, "combined_path": "/x/b.mp4"},
            {"name": "freefall", "duration": 60.0, "combined_path": "/x/f.mp4",
             "exit_offset": 10.0, "deploy_offset": 50.0},
            {"name": "canopy", "duration": 30.0, "combined_path": "/x/c.mp4"},
        ],
        "flagged": [],
    }


def _cam_scores() -> dict[str, list[dict[str, float]]]:
    return {
        "freefall": [
            {"ts": float(t), "smile": 0.6, "eye_contact": 0.6,
             "face_in_frame": 1.0, "face_centered": 0.7}
            for t in range(60)
        ]
    }


def test_compose_combo_edls_features_both_cameras_in_every_scene() -> None:
    rm = {role: _cam_manifest() for role in selfie.CAMERA_ROLES}
    rs = {role: _cam_scores() for role in selfie.CAMERA_ROLES}

    edls = selfie.compose_combo_edls(rm, rs, target_duration=90.0)

    # Every scene of the full video carries BOTH camera angles (cameraman never dropped).
    by_scene: dict[str, set[str | None]] = {}
    for c in edls.full_video:
        by_scene.setdefault(c.scene, set()).add(c.camera)
    for scene in ("boarding", "freefall", "canopy"):
        assert by_scene.get(scene) == {"instructor", "external"}, scene
    # Story order preserved across scenes (boarding → freefall → canopy).
    ranks = [selfie._scene_rank(c.scene) for c in edls.full_video]
    assert ranks == sorted(ranks)
    # Highlights likewise feature both cameras.
    assert {c.camera for c in edls.highlights} == {"instructor", "external"}


def test_compose_combo_uses_one_camera_when_only_one_has_a_scene() -> None:
    # The cameraman has no intro_interview; the instructor does -> intro is the instructor
    # alone, but shared scenes still feature both.
    instr = {
        "scenes": [
            {"name": "intro_interview", "duration": 15.0, "combined_path": "/i/i.mp4"},
            {"name": "freefall", "duration": 40.0, "combined_path": "/i/f.mp4",
             "exit_offset": 5.0, "deploy_offset": 35.0},
        ],
        "flagged": [],
    }
    ext = {
        "scenes": [
            {"name": "freefall", "duration": 40.0, "combined_path": "/e/f.mp4",
             "exit_offset": 5.0, "deploy_offset": 35.0},
        ],
        "flagged": [],
    }
    edls = selfie.compose_combo_edls(
        {"instructor": instr, "external": ext},
        {"instructor": _cam_scores(), "external": _cam_scores()},
        target_duration=90.0,
    )
    intro_cams = {c.camera for c in edls.full_video if c.scene == "intro_interview"}
    ff_cams = {c.camera for c in edls.full_video if c.scene == "freefall"}
    assert intro_cams == {"instructor"}          # only one camera filmed it
    assert ff_cams == {"instructor", "external"}  # shared scene = both angles


def test_multicam_scene_paths_resolves_per_camera_with_bare_fallback() -> None:
    rm = {
        "instructor": {"scenes": [{"name": "freefall", "combined_path": "/i/f.mp4"}]},
        "external": {"scenes": [{"name": "freefall", "combined_path": "/e/f.mp4"}]},
    }
    paths = selfie._multicam_scene_paths(rm)
    assert paths["instructor/freefall"] == "/i/f.mp4"
    assert paths["external/freefall"] == "/e/f.mp4"
    assert paths["freefall"] == "/i/f.mp4"  # bare fallback resolves to the first camera


def test_render_resolves_camera_tagged_clips_to_each_camera_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(selfie, "_run_ffmpeg", lambda cmd: captured.setdefault("cmd", cmd))
    monkeypatch.setattr(selfie, "scene_has_audio", lambda p: False)
    monkeypatch.setattr(selfie, "_card_input", lambda *a, **k: str(tmp_path / "_outro.mp4"))
    monkeypatch.setattr(selfie, "probe_duration", lambda p: 0.0)

    clips = [
        selfie.Clip(scene="freefall", src_start=0.0, src_end=4.0, camera="instructor"),
        selfie.Clip(scene="freefall", src_start=0.0, src_end=4.0, camera="external"),
    ]
    scene_paths = {"instructor/freefall": "/i/f.mp4", "external/freefall": "/e/f.mp4"}
    selfie.render_selfie_video(
        tmp_path / "out.mp4", clips, scene_paths,
        booking={}, music_path=None, music_only=True,
    )
    cmd = captured["cmd"]
    # Both per-camera files are wired as inputs (the two angles really do come from
    # different sources, not one shared file).
    assert "/i/f.mp4" in cmd
    assert "/e/f.mp4" in cmd
