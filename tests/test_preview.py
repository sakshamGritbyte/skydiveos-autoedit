"""Tests for the watermarked 720p preview render (api/preview.py, render/watermark.py).

The watermark PNG is generated for real (Pillow is a dependency); FFmpeg is faked via
the injectable ``runner``, so these run offline in milliseconds. The two contracts that
matter: an ``edited_download`` job must gain no new work or failure mode, and a
``preview_only`` job must end up with a ``preview_<name>.mp4`` per video — produced
with ``overlay``, never ``drawtext`` (the deployed FFmpeg has no libfreetype).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.config import Settings
from api.jobs import Entitlement, Job, JobStore
from api.preview import (
    PREVIEW_H,
    PREVIEW_PREFIX,
    PREVIEW_W,
    PreviewError,
    preview_path,
    render_job_previews,
    render_preview,
)
from render.watermark import render_watermark

from .test_delivery import _settings


class FakeFFmpeg:
    """Records the commands it was handed and ``touch``es each output file."""

    def __init__(self, *, fail: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd: list[str]) -> None:
        self.commands.append(cmd)
        if self.fail:
            raise PreviewError("boom")
        Path(cmd[-1]).write_bytes(b"fake-preview-bytes")

    @property
    def filters(self) -> list[str]:
        out = []
        for cmd in self.commands:
            out.append(cmd[cmd.index("-filter_complex") + 1])
        return out


def _job(store: JobStore, **fields: object) -> Job:
    base: dict[str, object] = {"job_id": "j1"}
    base.update(fields)
    return store.create(Job(**base))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The watermark PNG
# --------------------------------------------------------------------------- #


def test_watermark_is_a_full_frame_rgba_png(tmp_path: Path) -> None:
    from PIL import Image

    out = render_watermark(
        tmp_path / "wm.png", width=PREVIEW_W, height=PREVIEW_H, brand="Ultimate DZ"
    )
    assert out.exists()
    with Image.open(out) as img:
        assert img.mode == "RGBA"  # transparent, so it overlays at 0:0
        assert img.size == (PREVIEW_W, PREVIEW_H)
        # Translucent, not opaque: some pixels must be fully transparent.
        alphas = {px[3] for px in img.convert("RGBA").getdata()}
    assert 0 in alphas and max(alphas) > 0


def _marked_pixels(png: Path) -> int:
    from PIL import Image

    with Image.open(png) as img:
        return sum(1 for px in img.convert("RGBA").getdata() if px[3] > 0)


def test_watermark_logo_adds_coverage_over_text_only(tmp_path: Path) -> None:
    logo = Path(__file__).resolve().parent.parent / "templates" / "logo.png"
    text_only = render_watermark(
        tmp_path / "plain.png", width=PREVIEW_W, height=PREVIEW_H, brand="Ultimate DZ"
    )
    with_logo = render_watermark(
        tmp_path / "logo.png",
        width=PREVIEW_W,
        height=PREVIEW_H,
        brand="Ultimate DZ",
        logo_path=logo,
    )
    # The logo layers (centre + tiles) must obscure strictly more of the frame.
    assert _marked_pixels(with_logo) > _marked_pixels(text_only)


def test_watermark_survives_a_missing_or_corrupt_logo(tmp_path: Path) -> None:
    # A branding asset must never fail a preview render.
    out = render_watermark(
        tmp_path / "wm.png",
        width=PREVIEW_W,
        height=PREVIEW_H,
        brand="Ultimate DZ",
        logo_path=tmp_path / "nope.png",
    )
    assert out.exists()
    corrupt = tmp_path / "bad.png"
    corrupt.write_bytes(b"not a png")
    out2 = render_watermark(
        tmp_path / "wm2.png",
        width=PREVIEW_W,
        height=PREVIEW_H,
        brand="Ultimate DZ",
        logo_path=corrupt,
    )
    assert out2.exists()


# --------------------------------------------------------------------------- #
# The transcode command
# --------------------------------------------------------------------------- #


def test_render_preview_scales_to_720p_and_overlays_never_drawtext(tmp_path: Path) -> None:
    src = tmp_path / "full_video.mp4"
    src.write_bytes(b"master")
    png = tmp_path / "wm.png"
    png.write_bytes(b"png")
    ffmpeg = FakeFFmpeg()

    out = render_preview(src, tmp_path / "preview_full_video.mp4", png, runner=ffmpeg)

    assert out.exists()
    (cmd,) = ffmpeg.commands
    (graph,) = ffmpeg.filters
    assert f"scale={PREVIEW_W}:{PREVIEW_H}" in graph
    assert "overlay=0:0" in graph
    assert "drawtext" not in graph  # libfreetype is absent on the deploy machines
    assert str(png) in cmd  # the watermark is a real input
    assert "0:a?" in cmd  # audio is optional, not required


def test_render_preview_surfaces_a_failed_transcode(tmp_path: Path) -> None:
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    png = tmp_path / "wm.png"
    png.write_bytes(b"p")
    with pytest.raises(PreviewError):
        render_preview(src, tmp_path / "out.mp4", png, runner=FakeFFmpeg(fail=True))


# --------------------------------------------------------------------------- #
# render_job_previews — the seam the tasks call
# --------------------------------------------------------------------------- #


def test_no_previews_for_an_edited_download_job(tmp_path: Path) -> None:
    """Path A gains no extra encode pass and no new failure mode."""
    store = JobStore(tmp_path)
    (tmp_path / "j1").mkdir()
    (tmp_path / "j1" / "full_video.mp4").write_bytes(b"master")
    job = _job(store, outputs={"full_video": str(tmp_path / "j1" / "full_video.mp4")})
    ffmpeg = FakeFFmpeg()

    assert render_job_previews(job, store, _settings(), runner=ffmpeg) == {}
    assert ffmpeg.commands == []


def test_previews_rendered_per_video_deliverable_skipping_photos(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    jd = tmp_path / "j1"
    (jd / "photos").mkdir(parents=True)
    for name in ("full_video", "highlights"):
        (jd / f"{name}.mp4").write_bytes(b"master")
    job = _job(
        store,
        entitlement=Entitlement.preview_only,
        outputs={
            "full_video": str(jd / "full_video.mp4"),
            "highlights": str(jd / "highlights.mp4"),
            "photos": str(jd / "photos"),  # a directory — never transcoded
        },
    )
    ffmpeg = FakeFFmpeg()

    made = render_job_previews(job, store, _settings(), runner=ffmpeg)

    assert set(made) == {"full_video", "highlights"}
    assert len(ffmpeg.commands) == 2
    for name in ("full_video", "highlights"):
        assert preview_path(jd, name).exists()
        assert preview_path(jd, name).name == f"{PREVIEW_PREFIX}{name}.mp4"
    assert not (jd / f"{PREVIEW_PREFIX}photos.mp4").exists()


def test_previews_fall_back_to_final_mp4_for_the_classic_pipeline(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    jd = tmp_path / "j1"
    jd.mkdir()
    (jd / "final.mp4").write_bytes(b"master")
    job = _job(store, entitlement=Entitlement.preview_only)

    made = render_job_previews(job, store, _settings(), runner=FakeFFmpeg())

    assert set(made) == {"final"}
    assert preview_path(jd, "final").exists()


def test_a_preview_only_job_with_nothing_rendered_raises(tmp_path: Path) -> None:
    """Better a failed (re-queueable) job than a locked gallery with nothing to play."""
    store = JobStore(tmp_path)
    job = _job(store, entitlement=Entitlement.preview_only)
    with pytest.raises(PreviewError, match="no rendered video"):
        render_job_previews(job, store, _settings(), runner=FakeFFmpeg())


def test_preview_render_uses_one_watermark_for_every_deliverable(tmp_path: Path) -> None:
    """The PNG is generated once per job, not once per video."""
    store = JobStore(tmp_path)
    jd = tmp_path / "j1"
    jd.mkdir()
    for name in ("a", "b", "c"):
        (jd / f"{name}.mp4").write_bytes(b"m")
    job = _job(
        store,
        entitlement=Entitlement.preview_only,
        outputs={n: str(jd / f"{n}.mp4") for n in ("a", "b", "c")},
    )
    ffmpeg = FakeFFmpeg()

    render_job_previews(job, store, _settings(), runner=ffmpeg)

    pngs = {cmd[cmd.index("-i", cmd.index("-i") + 1) + 1] for cmd in ffmpeg.commands}
    assert len(pngs) == 1


def test_settings_brand_reaches_the_watermark(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The tile text is the dropzone's brand, so the mark reads as theirs."""
    seen: dict[str, object] = {}
    import api.preview as preview_mod

    def _fake_watermark(out_path, **kwargs):  # noqa: ANN001, ANN003
        seen.update(kwargs)
        Path(out_path).write_bytes(b"png")
        return Path(out_path)

    monkeypatch.setattr(preview_mod, "render_watermark", _fake_watermark)
    store = JobStore(tmp_path)
    jd = tmp_path / "j1"
    jd.mkdir()
    (jd / "final.mp4").write_bytes(b"m")
    job = _job(store, entitlement=Entitlement.preview_only)

    render_job_previews(
        job, store, _settings(delivery_brand_name="Skydive Test"), runner=FakeFFmpeg()
    )

    assert seen["brand"] == "Skydive Test"
    assert (seen["width"], seen["height"]) == (PREVIEW_W, PREVIEW_H)


def test_settings_fixture_is_a_real_settings_object() -> None:
    """Guard the shared helper import (these tests reuse test_delivery's builder)."""
    assert isinstance(_settings(), Settings)


# --------------------------------------------------------------------------- #
# The task seam: a Path-B job with no watchable preview must FAIL, not deliver
# --------------------------------------------------------------------------- #


def test_task_fails_a_preview_only_job_whose_preview_render_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import JobStatus

    store = JobStore(tmp_path)
    jd = tmp_path / "j1"
    jd.mkdir()
    (jd / "final.mp4").write_bytes(b"master")
    _job(store, entitlement=Entitlement.preview_only, source_path=str(jd / "final.mp4"))

    monkeypatch.setattr(tasks, "_store", lambda: store)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path)))
    monkeypatch.setattr(tasks, "_notify_skydiveos", lambda job: None)
    monkeypatch.setattr(tasks, "_archive_deliverables", lambda store, job_id: None)
    monkeypatch.setattr(tasks, "_maybe_auto_deliver", lambda store, job_id: None)
    monkeypatch.setattr(
        tasks, "_render_previews",
        lambda store, job_id: (_ for _ in ()).throw(PreviewError("ffmpeg exploded")),
    )
    monkeypatch.setattr(
        "scripts.process_jump.process_jump", lambda *a, **k: None, raising=False
    )

    with pytest.raises(PreviewError):
        tasks.process_job("j1")

    job = store.load("j1")
    assert job.status is JobStatus.failed
    assert job.error and "ffmpeg exploded" in job.error


def test_render_previews_seam_is_a_no_op_for_path_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam runs on every job, so it must cost nothing for edited_download."""
    from api import tasks

    store = JobStore(tmp_path)
    jd = tmp_path / "j1"
    jd.mkdir()
    (jd / "final.mp4").write_bytes(b"master")
    _job(store)
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path)))

    tasks._render_previews(store, "j1")  # must not raise, must not write

    assert not list(jd.glob(f"{PREVIEW_PREFIX}*"))


# --------------------------------------------------------------------------- #
# Watermarked photo previews (BUG 350)
# --------------------------------------------------------------------------- #


def _still(path: Path, *, size: tuple[int, int] = (1920, 1080)) -> bytes:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 120, 200)).save(path, format="JPEG", quality=90)
    return path.read_bytes()


def test_photo_preview_is_watermarked_downscaled_and_cached(tmp_path: Path) -> None:
    from PIL import Image

    from api.preview import (
        PHOTO_PREVIEW_MAX_EDGE,
        ensure_photo_preview,
        photo_preview_path,
    )

    clean = _still(tmp_path / "photos" / "f_42.jpg")
    out = ensure_photo_preview(tmp_path, "f_42.jpg", _settings())

    assert out == photo_preview_path(tmp_path, "f_42.jpg")
    assert out is not None and out.is_file()
    assert out.parent.name == "preview_photos"  # NEVER inside photos/ (zip/S3/archive)
    assert out.read_bytes() != clean
    with Image.open(out) as img:
        assert max(img.size) <= PHOTO_PREVIEW_MAX_EDGE  # a teaser, not the product
        assert img.size[0] * 1080 == img.size[1] * 1920  # aspect kept, no pad/crop

    # Cached: a second ask returns the same file without re-rendering it.
    stamp = out.stat().st_mtime_ns
    assert ensure_photo_preview(tmp_path, "f_42.jpg", _settings()) == out
    assert out.stat().st_mtime_ns == stamp


def test_photo_preview_never_falls_back_to_the_clean_file(tmp_path: Path) -> None:
    """An unreadable still yields None — the caller must refuse, not serve clean."""
    from api.preview import ensure_photo_preview

    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "junk.jpg").write_bytes(b"not a jpeg at all")

    assert ensure_photo_preview(tmp_path, "junk.jpg", _settings()) is None
    assert ensure_photo_preview(tmp_path, "missing.jpg", _settings()) is None
