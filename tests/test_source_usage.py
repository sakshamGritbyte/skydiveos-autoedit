"""Tests for the source-usage manifest (api/source_usage.py).

The manifest answers "which seconds of which RAW MASTER did each deliverable use?" for
SkydiveOS's manual editor. These tests pin the whole contract: scene-time → raw-time
conversion via ``file_offsets`` (including clips spanning a raw-file boundary), the
render's post-persistence transforms (exclusions, playable-duration clamp) being
reflected, role attribution for every package shape (plain, mixed ``{role}_`` prefixes,
ultimum combo + ``camera: null`` freefall cuts), the role-scoped ``raw_s3_keys``
lookup with its bare-name legacy fallback, the API endpoint, and the delivery upload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import selfie
from api.app import create_app, get_queue, get_store
from api.jobs import (
    Entitlement,
    Job,
    JobStatus,
    JobStore,
    MediaRef,
    Package,
    raw_key_for,
)
from api.source_usage import build_source_usage, source_usage_path, write_source_usage
from tests.test_delivery import FakeS3, FakeSMTP, _settings

# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _scene(name: str, files: list[tuple[str, float]], path: str | None = None) -> dict[str, Any]:
    """A scene-manifest entry built exactly the way ``build_scenes`` accumulates it."""
    offsets, cum = [], 0.0
    for filename, duration in files:
        offsets.append({"file": filename, "offset": round(cum, 3)})
        cum += duration
    return {
        "name": name,
        "source_files": [f for f, _ in files],
        "combined_path": path or f"/scenes/{name}.mp4",
        "duration": round(cum, 3),
        "needs_review": False,
        "file_offsets": offsets,
    }


def _write_json(jd: Path, name: str, obj: Any) -> None:
    jd.mkdir(parents=True, exist_ok=True)
    (jd / name).write_text(json.dumps(obj))


def _clip(
    scene: str, start: float, end: float, speed: float = 1.0, camera: str | None = None
) -> dict[str, Any]:
    return {
        "scene": scene,
        "src_start": start,
        "src_end": end,
        "speed_multiplier": speed,
        "camera": camera,
    }


def _plain_job(tmp_path: Path, **fields: Any) -> tuple[JobStore, Job]:
    store = JobStore(tmp_path)
    fields.setdefault("package", Package.selfie)
    fields.setdefault("status", JobStatus.ready)
    job = store.create(Job(job_id="j1", **fields))
    return store, job


# --------------------------------------------------------------------------- #
# Scene-time → raw-time conversion
# --------------------------------------------------------------------------- #


def test_single_file_clip_maps_to_raw_seconds(tmp_path: Path) -> None:
    store, job = _plain_job(
        tmp_path, raw_s3_keys={"GX010990.MP4": "ai-sources/j1/plain/b1/GX010990.MP4"}
    )
    jd = store.dir("j1")
    _write_json(
        jd, "scene_manifest.json", {"scenes": [_scene("freefall", [("GX010990.MP4", 60.0)])]}
    )
    _write_json(jd, "edl_full.json", [_clip("freefall", 10.0, 30.0)])

    usage = build_source_usage("j1", store, tmp_path)
    assert usage is not None
    [entry] = usage["deliverables"]["full_video"]
    assert entry["raw_filename"] == "GX010990.MP4"
    assert entry["role"] is None
    assert entry["s3_key"] == "ai-sources/j1/plain/b1/GX010990.MP4"
    assert (entry["src_start"], entry["src_end"]) == (10.0, 30.0)
    assert (entry["out_start"], entry["out_end"]) == (0.0, 20.0)
    # The raw_files inventory carries the per-file duration (offset deltas).
    [raw] = usage["raw_files"]
    assert raw == {
        "filename": "GX010990.MP4",
        "role": None,
        "s3_key": "ai-sources/j1/plain/b1/GX010990.MP4",
        "duration": 60.0,
    }


def test_clip_spanning_raw_file_boundary_splits(tmp_path: Path) -> None:
    store, _ = _plain_job(tmp_path)
    jd = store.dir("j1")
    scene = _scene("intro_interview", [("GX010990.MP4", 34.368), ("GX010991.MP4", 12.846)])
    _write_json(jd, "scene_manifest.json", {"scenes": [scene]})
    _write_json(jd, "edl_full.json", [_clip("intro_interview", 0.0, 42.153)])

    usage = build_source_usage("j1", store, tmp_path)
    first, second = usage["deliverables"]["full_video"]
    assert first["raw_filename"] == "GX010990.MP4"
    assert (first["src_start"], first["src_end"]) == (0.0, 34.368)
    assert second["raw_filename"] == "GX010991.MP4"
    assert second["src_start"] == 0.0
    assert second["src_end"] == pytest.approx(42.153 - 34.368, abs=0.001)
    # The output timeline is continuous across the split.
    assert first["out_start"] == 0.0
    assert first["out_end"] == pytest.approx(34.368, abs=0.001)
    assert second["out_start"] == pytest.approx(34.368, abs=0.001)
    assert second["out_end"] == pytest.approx(42.153, abs=0.001)


def test_speed_multiplier_shapes_output_times_not_source_times(tmp_path: Path) -> None:
    store, _ = _plain_job(tmp_path)
    jd = store.dir("j1")
    _write_json(jd, "scene_manifest.json", {"scenes": [_scene("freefall", [("A.MP4", 60.0)])]})
    _write_json(
        jd, "edl_full.json",
        [_clip("freefall", 0.0, 10.0), _clip("freefall", 20.0, 30.0, speed=0.5)],
    )

    usage = build_source_usage("j1", store, tmp_path)
    normal, slow = usage["deliverables"]["full_video"]
    assert (slow["src_start"], slow["src_end"]) == (20.0, 30.0)  # source untouched
    assert slow["out_start"] == 10.0
    assert slow["out_end"] == 30.0  # 10 source-seconds at 0.5x = 20 output-seconds


# --------------------------------------------------------------------------- #
# The render's post-persistence transforms are reflected
# --------------------------------------------------------------------------- #


def test_exclusions_are_reflected(tmp_path: Path) -> None:
    store, _ = _plain_job(tmp_path)
    jd = store.dir("j1")
    _write_json(jd, "scene_manifest.json", {"scenes": [_scene("freefall", [("A.MP4", 60.0)])]})
    _write_json(jd, "edl_full.json", [_clip("freefall", 0.0, 30.0)])
    _write_json(jd, selfie.EXCLUDE_FILENAME, {"freefall": [[10.0, 20.0]]})

    usage = build_source_usage("j1", store, tmp_path)
    ranges = [(e["src_start"], e["src_end"]) for e in usage["deliverables"]["full_video"]]
    assert ranges == [(0.0, 10.0), (20.0, 30.0)]


def test_playable_duration_clamp_is_reflected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _plain_job(tmp_path)
    jd = store.dir("j1")
    _write_json(jd, "scene_manifest.json", {"scenes": [_scene("freefall", [("A.MP4", 60.0)])]})
    _write_json(jd, "edl_full.json", [_clip("freefall", 10.0, 55.0)])
    # The scene file's PLAYABLE duration comes up short of the manifest estimate.
    monkeypatch.setattr(selfie, "scene_playable_duration", lambda path: 40.0)

    usage = build_source_usage("j1", store, tmp_path)
    [entry] = usage["deliverables"]["full_video"]
    assert (entry["src_start"], entry["src_end"]) == (10.0, 40.0)


# --------------------------------------------------------------------------- #
# Package shapes: ultimum (combo + camera:null freefalls) and mixed refs
# --------------------------------------------------------------------------- #


def _ultimum_job(tmp_path: Path) -> tuple[JobStore, Job]:
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            package=Package.ultimum,
            status=JobStatus.ready,
            raw_s3_keys={
                "instructor/GH010001.MP4": "ai-sources/j1/instructor/b1/GH010001.MP4",
                "external/GH010001.MP4": "ai-sources/j1/external/b2/GH010001.MP4",
            },
        )
    )
    jd = store.dir("j1")
    _write_json(
        jd, "scene_manifest_instructor.json",
        {"scenes": [_scene("freefall", [("GH010001.MP4", 50.0)], "/si/freefall.mp4")]},
    )
    _write_json(
        jd, "scene_manifest_external.json",
        {"scenes": [_scene("freefall", [("GH010001.MP4", 45.0)], "/se/freefall.mp4")]},
    )
    return store, job


def test_ultimum_combo_attributes_camera_tagged_clips(tmp_path: Path) -> None:
    store, _ = _ultimum_job(tmp_path)
    jd = store.dir("j1")
    _write_json(
        jd, selfie.ULTIMUM_EDL_FILES["full_video"],
        [
            _clip("freefall", 0.0, 5.0, camera="instructor"),
            _clip("freefall", 5.0, 12.0, camera="external"),
        ],
    )

    usage = build_source_usage("j1", store, tmp_path)
    inst, ext = usage["deliverables"]["full_video"]
    assert inst["role"] == "instructor"
    assert inst["s3_key"] == "ai-sources/j1/instructor/b1/GH010001.MP4"
    assert ext["role"] == "external"
    assert ext["s3_key"] == "ai-sources/j1/external/b2/GH010001.MP4"
    # Two colliding GoPro filenames stay two distinct raw_files rows, one per role.
    assert {(r["role"], r["filename"]) for r in usage["raw_files"]} == {
        ("instructor", "GH010001.MP4"),
        ("external", "GH010001.MP4"),
    }


def test_ultimum_freefall_cuts_resolve_role_from_deliverable(tmp_path: Path) -> None:
    store, _ = _ultimum_job(tmp_path)
    jd = store.dir("j1")
    # camera: null on purpose — the freefall EDLs never carry a camera tag; the role is
    # implied by which deliverable the file belongs to (ULTIMUM_FREEFALL_ROLE).
    _write_json(jd, selfie.ULTIMUM_EDL_FILES["external_freefall"], [_clip("freefall", 0.0, 8.0)])
    _write_json(jd, selfie.ULTIMUM_EDL_FILES["chute_libre_selfie"], [_clip("freefall", 1.0, 9.0)])

    usage = build_source_usage("j1", store, tmp_path)
    [ext] = usage["deliverables"]["external_freefall"]
    [inst] = usage["deliverables"]["chute_libre_selfie"]
    assert ext["role"] == "external"
    assert ext["s3_key"] == "ai-sources/j1/external/b2/GH010001.MP4"
    assert inst["role"] == "instructor"
    assert inst["s3_key"] == "ai-sources/j1/instructor/b1/GH010001.MP4"


def test_mixed_refs_use_prefixed_edls_and_namespaced_deliverables(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.create(
        Job(
            job_id="j1",
            package=Package.selfie,
            status=JobStatus.ready,
            media_refs=[
                MediaRef(
                    role="instructor",
                    package=Package.selfie,
                    entitlement=Entitlement.edited_download,
                ),
                MediaRef(
                    role="external",
                    package=Package.external,
                    entitlement=Entitlement.preview_only,
                ),
            ],
            raw_s3_keys={
                "instructor/GH010001.MP4": "ai-sources/j1/instructor/b1/GH010001.MP4",
                "external/GX020002.MP4": "ai-sources/j1/external/b2/GX020002.MP4",
            },
        )
    )
    jd = store.dir("j1")
    _write_json(
        jd, "scene_manifest_instructor.json",
        {"scenes": [_scene("freefall", [("GH010001.MP4", 50.0)], "/si/f.mp4")]},
    )
    _write_json(
        jd, "scene_manifest_external.json",
        {"scenes": [_scene("freefall", [("GX020002.MP4", 45.0)], "/se/f.mp4")]},
    )
    # Primary (paid instructor) ref keeps unprefixed filenames + plain names; the spec
    # external ref is namespaced on both sides.
    _write_json(jd, "edl_full.json", [_clip("freefall", 0.0, 5.0)])
    _write_json(jd, "external_edl_full.json", [_clip("freefall", 2.0, 7.0)])

    usage = build_source_usage("j1", store, tmp_path)
    [primary] = usage["deliverables"]["full_video"]
    assert primary["role"] == "instructor"
    assert primary["raw_filename"] == "GH010001.MP4"
    [spec] = usage["deliverables"]["external_full_video"]
    assert spec["role"] == "external"
    assert spec["s3_key"] == "ai-sources/j1/external/b2/GX020002.MP4"


def test_photo_only_or_unrendered_job_builds_nothing(tmp_path: Path) -> None:
    store, _ = _plain_job(tmp_path, package=Package.photo_only)
    assert build_source_usage("j1", store, tmp_path) is None
    assert write_source_usage("j1", store, tmp_path) is None
    assert not source_usage_path("j1", tmp_path).exists()


# --------------------------------------------------------------------------- #
# raw_key_for: role-scoped first, bare-name legacy fallback
# --------------------------------------------------------------------------- #


def test_raw_key_for_prefers_role_scoped_and_falls_back_bare() -> None:
    job = Job(
        job_id="j1",
        raw_s3_keys={
            "instructor/GH010001.MP4": "role-scoped-key",
            "GH010001.MP4": "legacy-bare-key",
        },
    )
    assert raw_key_for(job, "instructor", "GH010001.MP4") == "role-scoped-key"
    # A legacy job recorded only the bare name — still resolvable for any role.
    assert raw_key_for(job, "external", "GH010001.MP4") == "legacy-bare-key"
    assert raw_key_for(job, None, "GH010001.MP4") == "legacy-bare-key"
    assert raw_key_for(job, None, "MISSING.MP4") is None


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path: Path) -> Any:
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: object()
    with TestClient(app) as c:
        c.store = store  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


def test_endpoint_404_when_job_has_no_usage(client: Any) -> None:
    client.store.create(Job(job_id="j1", package=Package.photo_only))
    resp = client.get("/jobs/j1/source-usage")
    assert resp.status_code == 404


def test_endpoint_builds_on_demand_for_pre_existing_jobs(client: Any, tmp_path: Path) -> None:
    # A job rendered BEFORE source_usage existed: EDLs + manifests on disk, no
    # source_usage.json. The endpoint builds it lazily from those artifacts.
    store: JobStore = client.store
    store.create(Job(job_id="j1", package=Package.selfie, status=JobStatus.ready))
    jd = store.dir("j1")
    _write_json(jd, "scene_manifest.json", {"scenes": [_scene("freefall", [("A.MP4", 60.0)])]})
    _write_json(jd, "edl_full.json", [_clip("freefall", 10.0, 30.0)])

    resp = client.get("/jobs/j1/source-usage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deliverables"]["full_video"][0]["raw_filename"] == "A.MP4"
    assert source_usage_path("j1", tmp_path).exists()  # cached for the next read

    # And the file is served (not rebuilt) once present.
    assert client.get("/jobs/j1/source-usage").status_code == 200


def test_endpoint_404_for_unknown_job(client: Any) -> None:
    assert client.get("/jobs/nope/source-usage").status_code == 404


# --------------------------------------------------------------------------- #
# Delivery uploads the manifest beside the deliverables (best-effort)
# --------------------------------------------------------------------------- #


def test_delivery_uploads_source_usage(tmp_path: Path) -> None:
    from api.delivery import deliver_to_customer

    store = JobStore(tmp_path)
    job = store.create(
        Job(job_id="j1", status=JobStatus.approved, customer_email="jane@example.com")
    )
    final = store.final_path("j1")
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"video")
    _write_json(store.dir("j1"), "source_usage.json", {"job_id": "j1", "version": 1})

    s3 = FakeS3()
    deliver_to_customer(
        job, store, _settings(), s3_client=s3, smtp_factory=lambda: FakeSMTP()  # type: ignore[arg-type,return-value]
    )
    keys = [key for _, _, key, _ in s3.uploads]
    assert "deliveries/j1/source_usage.json" in keys
    args = next(extra for _, _, key, extra in s3.uploads if key.endswith("source_usage.json"))
    assert args["ContentType"] == "application/json"


def test_delivery_survives_missing_source_usage(tmp_path: Path) -> None:
    from api.delivery import deliver_to_customer

    store = JobStore(tmp_path)
    job = store.create(
        Job(job_id="j1", status=JobStatus.approved, customer_email="jane@example.com")
    )
    final = store.final_path("j1")
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"video")

    links = deliver_to_customer(
        job, store, _settings(), s3_client=FakeS3(), smtp_factory=lambda: FakeSMTP()  # type: ignore[arg-type,return-value]
    )
    assert "gallery" in links  # delivery unaffected
