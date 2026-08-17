"""Tests for the gallery poster frames (api/thumbnail.py).

Two halves, matching the module's own split:

* the **pure** picker — which frame sells this video — asserted with plain
  :class:`FrameStats`, no video, no FFmpeg;
* the **I/O** wrapper, with FFmpeg faked through the injectable ``runner`` (it writes
  real little JPEGs so Pillow measures something), asserting the three rules that
  matter: a locked deliverable's poster comes from the *watermarked* preview, posters
  are cached rather than rebuilt, and nothing here ever raises — a poster that can't
  be made is a card that looks exactly as it did before this feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.jobs import DeliverableAccess, Entitlement, Job, JobStore
from api.preview import preview_path
from api.thumbnail import (
    CANDIDATE_COUNT,
    DEFAULT_PROFILE,
    POSTER_DIRNAME,
    POSTER_H,
    POSTER_W,
    FrameStats,
    build_poster,
    candidate_plan,
    dump_candidates,
    ensure_poster,
    poster_path,
    profile_for,
    render_job_posters,
    score_faces,
    select_frame,
)

from .test_delivery import _settings


def _frame(ts: float, **fields: float) -> FrameStats:
    base: dict[str, float] = {
        "sharpness": 100.0, "exposure": 0.8, "brightness": 0.5, "colour": 0.3
    }
    base.update(fields)
    return FrameStats(ts=ts, **base)  # type: ignore[arg-type]


class FakeFFmpeg:
    """Records commands; writes a real (tiny) JPEG at each command's output path.

    Candidate dumps use an ``%03d`` pattern, so the fake expands it into ``count``
    files the way FFmpeg would.
    """

    def __init__(self, *, count: int = CANDIDATE_COUNT, fail: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.count = count
        self.fail = fail

    def __call__(self, cmd: list[str]) -> None:
        self.commands.append(cmd)
        if self.fail:
            raise RuntimeError("ffmpeg exploded")
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        targets = (
            [out.with_name(out.name.replace("%03d", f"{i + 1:03d}")) for i in range(self.count)]
            if "%03d" in out.name
            else [out]
        )
        from PIL import Image

        for i, t in enumerate(targets):
            # Varying grey so the frames aren't identical (and none is black).
            Image.new("RGB", (64, 36), (60 + i, 90, 130)).save(t, format="JPEG")

    def arg(self, flag: str, *, cmd: int = 0) -> str:
        c = self.commands[cmd]
        return c[c.index(flag) + 1]


def _job(store: JobStore, **fields: object) -> Job:
    base: dict[str, object] = {"job_id": "j1"}
    base.update(fields)
    return store.create(Job(**base))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The picker (pure)
# --------------------------------------------------------------------------- #


def test_candidate_plan_skips_the_intro_and_outro_cards() -> None:
    """The renderer opens on a title card and closes on the logo card."""
    plan = candidate_plan(120.0)
    assert len(plan.times) == CANDIDATE_COUNT
    assert plan.times[0] >= 3.0  # past the intro card + fade
    assert plan.times[-1] <= 117.0  # before the outro card
    assert plan.times == tuple(sorted(plan.times))


def test_candidate_plan_samples_a_short_clip_whole() -> None:
    """Too short to trim → sample it all rather than sample nothing."""
    plan = candidate_plan(5.0)
    assert plan.start == 0.0
    assert plan.times[0] == 0.0
    assert plan.fps > 0


def test_candidate_plan_of_an_unreadable_video_is_empty() -> None:
    assert candidate_plan(0.0).times == ()
    assert dump_candidates(Path("x.mp4"), Path("out"), candidate_plan(0.0)) == []


def test_a_smiling_face_beats_a_sharper_empty_sky() -> None:
    """Rule 3: faces and emotion outrank raw image quality."""
    sky = _frame(10.0, sharpness=900.0, colour=0.5)
    grin = _frame(20.0, sharpness=300.0, smile=0.9, eye_contact=0.8, face_in_frame=1.0,
                  face_centered=0.9)
    assert select_frame([sky, grin], DEFAULT_PROFILE) is grin


def test_black_and_blown_out_frames_are_never_chosen() -> None:
    """Rule 4: fades, title cards and flare frames are rejected, not merely ranked."""
    black = _frame(5.0, brightness=0.01, sharpness=5000.0, smile=1.0, face_in_frame=1.0)
    white = _frame(9.0, brightness=0.995, sharpness=5000.0, smile=1.0, face_in_frame=1.0)
    plain = _frame(12.0, sharpness=50.0)
    assert select_frame([black, white, plain], DEFAULT_PROFILE) is plain


def test_no_usable_frame_returns_none() -> None:
    """Rule 9's fallback: an all-black video gets no poster, not a black poster."""
    assert select_frame([_frame(1.0, brightness=0.0), _frame(2.0, brightness=0.01)]) is None
    assert select_frame([]) is None


def test_blurry_frames_lose_to_sharp_ones_all_else_equal() -> None:
    blurry = _frame(10.0, sharpness=5.0)
    sharp = _frame(20.0, sharpness=800.0)
    assert select_frame([blurry, sharp], DEFAULT_PROFILE) is sharp


def test_selection_is_deterministic() -> None:
    """A poster that moves between identical runs reads as a bug to the operator."""
    frames = [_frame(float(t), sharpness=100.0) for t in range(5, 60, 5)]
    first = select_frame(frames, DEFAULT_PROFILE)
    assert first is not None
    assert select_frame(list(reversed(frames)), DEFAULT_PROFILE).ts == first.ts


def test_profiles_follow_the_deliverable_not_the_filename() -> None:
    """A mixed/Ultimate job's namespaced cuts are judged as what they are."""
    assert profile_for("external_freefall") is profile_for("freefall")
    assert profile_for("chute_libre_selfie") is profile_for("freefall")
    assert profile_for("instructor_highlights") is profile_for("highlights")
    assert profile_for("external_full_video") is profile_for("full_video")
    # Unknown deliverables (and the classic pipeline's `final`) still get a poster.
    assert profile_for("final") is DEFAULT_PROFILE
    assert profile_for("something_new") is DEFAULT_PROFILE


def test_the_highlights_profile_leans_on_the_peak_moment() -> None:
    """Rule 6: for Highlights the strongest moment wins over a merely pretty frame."""
    pretty = _frame(30.0, sharpness=900.0, colour=0.9)
    peak = _frame(18.0, sharpness=400.0, smile=1.0, eye_contact=0.9, face_in_frame=1.0,
                  face_centered=1.0)
    assert select_frame([pretty, peak], profile_for("highlights")) is peak


def test_a_distant_cameraman_cut_still_gets_a_poster() -> None:
    """No face detected anywhere (external footage): quality decides, nothing fails."""
    frames = [
        _frame(10.0, sharpness=20.0, exposure=0.3, colour=0.1),
        _frame(20.0, sharpness=700.0, exposure=0.9, colour=0.6),
    ]
    best = select_frame(frames, profile_for("external_freefall"))
    assert best is not None and best.ts == 20.0


# --------------------------------------------------------------------------- #
# Building a poster (FFmpeg faked)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in deliverable with a known duration and no face scoring."""
    import api.selfie
    import api.thumbnail

    monkeypatch.setattr(api.selfie, "probe_duration", lambda p: 120.0)
    monkeypatch.setattr(api.thumbnail, "score_faces", lambda frames: {})
    src = tmp_path / "full_video.mp4"
    src.write_bytes(b"fake-mp4")
    return src


def test_build_poster_writes_into_the_posters_dir(source: Path) -> None:
    runner = FakeFFmpeg()
    out = build_poster(source, runner=runner)
    assert out == source.parent / POSTER_DIRNAME / "full_video.jpg"
    assert out is not None and out.is_file()
    # Two passes: rank small candidates, then cut the winner at full resolution.
    assert len(runner.commands) == 2
    # ...and the winner is a real timestamp inside the trimmed window.
    assert 3.0 <= float(runner.arg("-ss", cmd=1)) <= 117.0


def test_the_poster_is_cropped_to_the_card_aspect(source: Path) -> None:
    """Rule 8: cover the 16:9 card, never letterbox inside it."""
    runner = FakeFFmpeg()
    build_poster(source, runner=runner)
    vf = runner.arg("-vf", cmd=1)
    assert f"scale={POSTER_W}:{POSTER_H}:force_original_aspect_ratio=increase" in vf
    assert f"crop={POSTER_W}:{POSTER_H}" in vf


def test_no_candidates_means_no_poster(source: Path) -> None:
    """FFmpeg produced nothing to rank → no poster file, and no exception."""
    assert build_poster(source, runner=FakeFFmpeg(count=0)) is None
    assert not list((source.parent / POSTER_DIRNAME).glob("*.jpg"))


def test_ensure_poster_caches_and_rebuilds_when_the_render_changes(source: Path) -> None:
    runner = FakeFFmpeg()
    first = ensure_poster(source, runner=runner)
    assert first is not None
    calls = len(runner.commands)

    assert ensure_poster(source, runner=runner) == first
    assert len(runner.commands) == calls  # served from disk, no second FFmpeg pass

    # A re-render (tweak/replay) must not leave the old moment on the card.
    import os

    os.utime(source, (first.stat().st_mtime + 60, first.stat().st_mtime + 60))
    ensure_poster(source, runner=runner)
    assert len(runner.commands) > calls


def test_ensure_poster_never_raises(tmp_path: Path, source: Path) -> None:
    """Decoration must not 500 a customer's gallery — every failure is just ``None``."""
    assert ensure_poster(source, runner=FakeFFmpeg(fail=True)) is None
    assert ensure_poster(tmp_path / "missing.mp4") is None


def test_ensure_poster_honours_the_off_switch(source: Path) -> None:
    settings = _settings(gallery_thumbnails=False)
    runner = FakeFFmpeg()
    assert ensure_poster(source, runner=runner, settings=settings) is None
    assert runner.commands == []


def test_score_faces_degrades_to_nothing_without_a_model(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    """A cold box must not download 30 MB inside a page request."""
    import analysis.models

    monkeypatch.setattr(analysis.models, "cached_model", lambda: None)
    jpg = tmp_path / "f.jpg"
    from PIL import Image

    Image.new("RGB", (32, 18)).save(jpg, format="JPEG")
    assert score_faces([(1.0, jpg)]) == {}
    assert score_faces([]) == {}


# --------------------------------------------------------------------------- #
# Per-job pre-render: the entitlement picks the source
# --------------------------------------------------------------------------- #


def test_a_locked_deliverable_posters_from_its_watermarked_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paywall rule: a locked card must never show a clean frame of the edit."""
    import api.selfie
    import api.thumbnail

    monkeypatch.setattr(api.selfie, "probe_duration", lambda p: 90.0)
    monkeypatch.setattr(api.thumbnail, "score_faces", lambda frames: {})
    built: list[Path] = []
    monkeypatch.setattr(
        api.thumbnail, "build_poster",
        lambda src, **kw: (built.append(src), poster_path(src))[1],
    )

    store = JobStore(str(tmp_path))
    job = _job(store, entitlement=Entitlement.preview_only)
    job_dir = store.dir(job.job_id)
    (job_dir / "full_video.mp4").write_bytes(b"clean")
    preview_path(job_dir, "full_video").write_bytes(b"watermarked")
    job = store.update(job.job_id, outputs={"full_video": str(job_dir / "full_video.mp4")})

    render_job_posters(job, store, _settings())
    assert built == [preview_path(job_dir, "full_video")]


def test_a_mixed_job_posters_each_deliverable_from_its_own_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per DELIVERABLE, never per job: the paid edit clean, the spec one watermarked."""
    import api.thumbnail

    built: list[Path] = []
    monkeypatch.setattr(
        api.thumbnail, "ensure_poster", lambda src, **kw: built.append(src) or src
    )

    store = JobStore(str(tmp_path))
    job = _job(
        store,
        entitlement=Entitlement.edited_download,
        deliverable_access={
            "external_full_video": DeliverableAccess(
                entitlement=Entitlement.preview_only, born_locked=True
            )
        },
    )
    job_dir = store.dir(job.job_id)
    job = store.update(
        job.job_id,
        outputs={
            "full_video": str(job_dir / "full_video.mp4"),
            "external_full_video": str(job_dir / "external_full_video.mp4"),
        },
    )
    render_job_posters(job, store, _settings())
    assert set(built) == {
        job_dir / "full_video.mp4",
        preview_path(job_dir, "external_full_video"),
    }


def test_render_job_posters_never_fails_a_job(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import api.thumbnail

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.thumbnail, "ensure_poster", boom)
    store = JobStore(str(tmp_path))
    job = _job(store)
    store.update(job.job_id, outputs={"full_video": "x.mp4"})
    assert render_job_posters(store.load(job.job_id), store, _settings()) == {}


def test_posters_are_off_when_the_setting_is(tmp_path: Path) -> None:
    store = JobStore(str(tmp_path))
    job = _job(store)
    settings = _settings(gallery_thumbnails=False)
    assert render_job_posters(job, store, settings) == {}


def test_poster_path_separates_the_clean_and_watermarked_stills(tmp_path: Path) -> None:
    """Unlock flips the card with no regeneration: the two stills are two files."""
    clean = poster_path(tmp_path / "full_video.mp4")
    locked = poster_path(preview_path(tmp_path, "full_video"))
    assert clean != locked
    assert clean.parent == locked.parent == tmp_path / POSTER_DIRNAME
    # Never inside photos/ — the paid zip, the S3 photo uploads and the archive
    # mirror all sweep that directory.
    assert POSTER_DIRNAME != "photos"
