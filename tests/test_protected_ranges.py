"""Protected ranges: the established editing rules in a rendered deliverable's seconds.

A staff re-edit in SkydiveOS works on the RENDERED file, so the bookend rule (Bug 441)
and the uncut-freefall rule are handed over as output-time spans
(``edl.protected.protected_ranges``, served by
``GET /jobs/{id}/deliverables/{name}/protected-ranges``). These tests pin the spans to
the clips of real validated EDLs (the three processed-job fixtures), and the route's
file resolution and 404s. Also: ``POST /jobs/{id}/tweak`` refuses a scene-pipeline job
(it can neither render nor validate one) and keeps the legacy single-master behaviour.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app, get_queue, get_store
from api.config import get_settings
from api.jobs import JobStatus, JobStore
from edl.protected import protected_ranges
from edl.schema import Clip, EditDecisionList
from edl.validate import validate_and_repair

_FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


def _mbc() -> dict[str, dict[str, Any]]:
    return {
        "instructor": _fx("ultimum_0823a77a/scene_manifest_instructor.json"),
        "external": _fx("ultimum_0823a77a/scene_manifest_external.json"),
    }


def _out(c: dict[str, Any]) -> float:
    return (c["src_end"] - c["src_start"]) / c["speed_multiplier"]


def _starts(clips: list[dict[str, Any]]) -> list[float]:
    t, out = 0.0, []
    for c in clips:
        out.append(t)
        t += _out(c)
    return out


def _by_kind(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["kind"]: r for r in result["ranges"]}


# --------------------------------------------------------------------------- #
# Pure: spans line up with the clips of real validated EDLs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("job", ["selfie_d15c2e42", "external_21cdb2c5"])
def test_full_video_bookends_and_freefall_line_up_with_the_clips(job: str) -> None:
    manifest = _fx(f"{job}/scene_manifest.json")
    clips, _ = validate_and_repair(_fx(f"{job}/edl_full.json"), "full_video", manifest)
    starts = _starts(clips)
    footage = starts[-1] + _out(clips[-1])

    result = protected_ranges(clips, "full_video", manifest, tail_card_s=2.5)

    assert result["reason"] is None
    assert result["duration"] == pytest.approx(footage + 2.5, abs=1e-3)
    spans = _by_kind(result)
    assert set(spans) == {"intro_bookend", "outro_bookend", "freefall"}

    intro_i = next(i for i, c in enumerate(clips) if c["scene"] == "intro_interview")
    intro = spans["intro_bookend"]
    assert intro["start"] == pytest.approx(starts[intro_i], abs=1e-3)
    assert intro["end"] == pytest.approx(starts[intro_i] + _out(clips[intro_i]), abs=1e-3)
    assert intro["music"] is False
    assert intro["music_until"] == intro["end"]  # music may come back after the intro

    outro_i = max(i for i, c in enumerate(clips) if c["scene"] == "outro_interview")
    outro = spans["outro_bookend"]
    assert outro["start"] == pytest.approx(starts[outro_i], abs=1e-3)
    if outro_i == len(clips) - 1:
        # The renderer keeps the music off through the logo card.
        assert outro["music_until"] == pytest.approx(footage + 2.5, abs=1e-3)

    ff = [i for i, c in enumerate(clips) if c["scene"] == "freefall"]
    span = spans["freefall"]
    assert span["start"] == pytest.approx(starts[ff[0]], abs=1e-3)
    assert span["end"] == pytest.approx(starts[ff[-1]] + _out(clips[ff[-1]]), abs=1e-3)
    assert "head_trimmable_until" not in span  # the full video's freefall is never trimmed
    # Ordered, inside the file, never overlapping.
    ordered = result["ranges"]
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert a["end"] <= b["start"] + 1e-6


@pytest.mark.parametrize(
    ("edl_name", "manifest_name", "deliverable"),
    [
        ("selfie_d15c2e42/edl_freefall.json", "selfie_d15c2e42/scene_manifest.json", "freefall"),
        (
            "external_21cdb2c5/edl_freefall.json",
            "external_21cdb2c5/scene_manifest.json",
            "freefall",
        ),
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
def test_freefall_cut_is_one_span_with_a_head_trimmable_up_to_the_exit(
    edl_name: str, manifest_name: str, deliverable: str
) -> None:
    manifest = _fx(manifest_name)
    lead = 9.98  # the beat-drop lead compose chose on the real jobs (<= the 15 s cap)
    clips, _ = validate_and_repair(_fx(edl_name), deliverable, manifest, freefall_lead=lead)
    exit_off = next(s for s in manifest["scenes"] if s["name"] == "freefall")["exit_offset"]

    result = protected_ranges(clips, deliverable, manifest)

    (span,) = result["ranges"]
    assert span["kind"] == "freefall"
    assert span["start"] == 0.0
    assert span["end"] == pytest.approx(sum(_out(c) for c in clips), abs=1e-3)
    # The exit plays `lead` seconds into the cut (all lead clips are 1x footage).
    expected = min(lead, exit_off)
    assert span["head_trimmable_until"] == pytest.approx(expected, abs=1e-3)
    assert 0.0 <= span["head_trimmable_until"] < span["end"]


def test_freefall_cut_without_a_lead_has_no_head_to_trim() -> None:
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    clips, _ = validate_and_repair(_fx("selfie_d15c2e42/edl_freefall.json"), "freefall", manifest)
    (span,) = protected_ranges(clips, "freefall", manifest)["ranges"]
    assert span["head_trimmable_until"] == span["start"] == 0.0


def test_highlights_carry_no_protected_range() -> None:
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    clips, _ = validate_and_repair(
        _fx("selfie_d15c2e42/edl_highlights.json"), "highlights", manifest
    )
    result = protected_ranges(clips, "highlights", manifest, tail_card_s=2.5)
    assert result["ranges"] == []
    assert "no protected range" in result["reason"]
    assert result["duration"] == pytest.approx(sum(_out(c) for c in clips) + 2.5, abs=1e-3)


def test_ultimate_full_video_freefall_is_ONE_span_over_the_alternating_run() -> None:
    mbc = _mbc()
    clips, _ = validate_and_repair(
        _fx("ultimum_0823a77a/edl_full.json"), "full_video", mbc["instructor"],
        manifest_by_camera=mbc,
    )
    ff = [i for i, c in enumerate(clips) if c["scene"] == "freefall"]
    assert len({clips[i]["camera"] for i in ff}) == 2  # both angles inside the run
    starts = _starts(clips)

    result = protected_ranges(clips, "full_video", mbc["instructor"], manifest_by_camera=mbc)

    freefall = [r for r in result["ranges"] if r["kind"] == "freefall"]
    assert len(freefall) == 1
    assert freefall[0]["start"] == pytest.approx(starts[ff[0]], abs=1e-3)
    assert freefall[0]["end"] == pytest.approx(starts[ff[-1]] + _out(clips[ff[-1]]), abs=1e-3)
    # The bookends come from ONE camera (the external one here: the instructor's card
    # holds only freefall + canopy), exactly as the renderer gates them.
    kinds = {r["kind"] for r in result["ranges"]}
    assert {"intro_bookend", "outro_bookend"} <= kinds


def test_a_pre_rule_edl_protects_nothing_it_does_not_hold_whole() -> None:
    """The raw fixture predates the bookend rule: its interviews are trimmed, so there is
    no whole bookend to protect — the caller then enforces nothing for it."""
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    raw = _fx("selfie_d15c2e42/edl_full.json")
    result = protected_ranges(raw, "full_video", manifest)
    assert not any(r["kind"].endswith("bookend") for r in result["ranges"])
    assert "bookend" in (result["reason"] or "")


def test_no_anchors_no_freefall_range() -> None:
    manifest = _fx("selfie_d15c2e42/scene_manifest.json")
    clips, _ = validate_and_repair(_fx("selfie_d15c2e42/edl_freefall.json"), "freefall", manifest)
    stripped = json.loads(json.dumps(manifest))
    for s in stripped["scenes"]:
        s.pop("exit_offset", None)
        s.pop("deploy_offset", None)
    result = protected_ranges(clips, "freefall", stripped)
    assert result["ranges"] == []
    assert "anchors" in result["reason"]


def test_empty_edl() -> None:
    assert protected_ranges([], "full_video", {"scenes": []}) == {
        "duration": None, "ranges": [], "reason": "no EDL clips",
    }


def test_fps_rounds_each_clip_to_whole_frames() -> None:
    clips = [
        {"scene": "freefall", "src_start": 0.0, "src_end": 1.01, "speed_multiplier": 1.0},
        {"scene": "freefall", "src_start": 1.01, "src_end": 2.02, "speed_multiplier": 1.0},
    ]
    manifest = {"scenes": [{"name": "freefall", "duration": 3.0,
                            "exit_offset": 0.5, "deploy_offset": 1.5}]}
    (span,) = protected_ranges(clips, "freefall", manifest, fps=30)["ranges"]
    assert span["end"] == pytest.approx(2.0, abs=1e-3)  # 30 + 30 frames


def test_pure_module_does_not_import_api() -> None:
    src = (Path(__file__).parent.parent / "edl" / "protected.py").read_text()
    assert "from api" not in src and "import api" not in src


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #


class _Queue:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def _record(*_a: Any, **_k: Any) -> None:
            self.calls.append(name)
        return _record


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    store = JobStore(tmp_path / "jobs")
    queue = _Queue()
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    with TestClient(app) as c:
        c.store = store  # type: ignore[attr-defined]
        c.queue = queue  # type: ignore[attr-defined]
        yield c
    get_settings.cache_clear()


def _job(client: TestClient, package: str, fixture: str, outputs: list[str]) -> str:
    resp = client.post("/jobs", json={"customer_name": "Alex", "package": package})
    job_id = resp.json()["job_id"]
    store: JobStore = client.store  # type: ignore[attr-defined]
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    for f in (_FIXTURES / fixture).iterdir():
        shutil.copy(f, jd / f.name)
    store.update(
        job_id, status=JobStatus.ready,
        outputs={n: str(jd / f"{n}.mp4") for n in outputs},
    )
    return job_id


def test_route_returns_spans_for_a_selfie_full_video(client: TestClient) -> None:
    job_id = _job(client, "selfie", "selfie_d15c2e42", ["full_video", "highlights", "freefall"])
    body = client.get(f"/jobs/{job_id}/deliverables/full_video/protected-ranges").json()
    assert body["deliverable"] == "full_video"
    # The fixture EDL predates the bookend rule, so only the freefall is protected —
    # and the reason says why the bookends are not.
    assert [r["kind"] for r in body["ranges"]] == ["freefall"]
    assert "bookend" in body["reason"]
    assert body["duration"] > body["ranges"][-1]["end"]

    hl = client.get(f"/jobs/{job_id}/deliverables/highlights/protected-ranges").json()
    assert hl["ranges"] == []

    ff = client.get(f"/jobs/{job_id}/deliverables/freefall/protected-ranges").json()
    assert ff["ranges"][0]["kind"] == "freefall"
    assert "head_trimmable_until" in ff["ranges"][0]


def test_route_resolves_the_ultimate_files(client: TestClient) -> None:
    job_id = _job(
        client, "ultimum", "ultimum_0823a77a",
        ["full_video", "highlights", "external_freefall", "chute_libre_selfie"],
    )
    full = client.get(f"/jobs/{job_id}/deliverables/full_video/protected-ranges").json()
    assert [r["kind"] for r in full["ranges"]].count("freefall") == 1
    for name in ("external_freefall", "chute_libre_selfie"):
        body = client.get(f"/jobs/{job_id}/deliverables/{name}/protected-ranges").json()
        assert body["ranges"][0]["kind"] == "freefall", name


def test_route_404s_and_degrades(client: TestClient) -> None:
    assert client.get("/jobs/nope/deliverables/full_video/protected-ranges").status_code == 404
    job_id = _job(client, "selfie", "selfie_d15c2e42", ["full_video"])
    assert client.get(f"/jobs/{job_id}/deliverables/freefall/protected-ranges").status_code == 404
    assert client.get(f"/jobs/{job_id}/deliverables/photos/protected-ranges").status_code == 404
    # A deliverable whose EDL is gone answers "nothing to enforce", never a 500.
    (client.store.dir(job_id) / "edl_full.json").unlink()  # type: ignore[attr-defined]
    body = client.get(f"/jobs/{job_id}/deliverables/full_video/protected-ranges").json()
    assert body["ranges"] == [] and body["reason"]


# --------------------------------------------------------------------------- #
# /tweak: refused for the scene pipeline, legacy single-master unchanged
# --------------------------------------------------------------------------- #


def _edl() -> dict[str, Any]:
    return EditDecisionList(clips=[Clip(src_start=0.0, src_end=2.0)]).model_dump(mode="json")


def test_tweak_refuses_a_scene_pipeline_job_and_changes_nothing(client: TestClient) -> None:
    job_id = _job(client, "selfie", "selfie_d15c2e42", ["full_video"])
    client.store.update(job_id, status=JobStatus.delivered)  # type: ignore[attr-defined]
    resp = client.post(f"/jobs/{job_id}/tweak", json={"edl": _edl()})
    assert resp.status_code == 409
    assert "scene pipeline" in resp.json()["detail"]
    job = client.store.load(job_id)  # type: ignore[attr-defined]
    assert job.status is JobStatus.delivered  # not flipped to queued / failed
    assert not (client.store.dir(job_id) / "edl.json").exists()  # type: ignore[attr-defined]
    assert client.queue.calls == []  # type: ignore[attr-defined]


def test_tweak_still_works_for_a_legacy_single_master_job(client: TestClient) -> None:
    job_id = client.post("/jobs", json={"customer_name": "Alex"}).json()["job_id"]
    client.store.update(job_id, status=JobStatus.ready_for_review)  # type: ignore[attr-defined]
    resp = client.post(f"/jobs/{job_id}/tweak", json={"edl": _edl()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert client.queue.calls == ["enqueue_rerender"]  # type: ignore[attr-defined]
