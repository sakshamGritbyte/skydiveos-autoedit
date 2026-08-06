"""Watermarked 720p previews for ``preview_only`` jobs (design doc Path B).

A speculative capture ("we filmed it anyway") still renders its normal clean 1080p
deliverables — they're what the customer buys, uploaded to S3 and instantly served
the moment SkydiveOS calls ``POST /jobs/{id}/unlock``. What the *locked* gallery
streams instead is produced here: one cheap second-pass transcode per video
deliverable — scale to 720p + the tiled brand watermark composited with FFmpeg's
``overlay`` (a Pillow PNG from :mod:`render.watermark`; **never** ``drawtext``, the
deployed FFmpeg lacks libfreetype).

Preview files live beside the clean masters as ``<job_dir>/preview_<name>.mp4`` and
are deliberately **not** recorded in ``Job.outputs`` — they'd leak into the S3
delivery set. The gallery route derives them from the ``preview_`` convention.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from render.watermark import render_watermark

from .config import Settings
from .jobs import Entitlement, Job, JobStore

logger = logging.getLogger(__name__)

#: Filename prefix distinguishing a watermarked preview from its clean master.
PREVIEW_PREFIX = "preview_"
#: Preview geometry — 720p, per the design doc's "watermarked 720p preview".
PREVIEW_W, PREVIEW_H = 1280, 720
#: A preview transcode of a 2-minute deliverable takes seconds; this is a backstop.
_FFMPEG_TIMEOUT_S = 600.0

#: Injectable command runner (tests fake it; default runs FFmpeg).
Runner = Callable[[list[str]], None]


class PreviewError(RuntimeError):
    """Raised when a preview transcode cannot be produced."""


def preview_path(job_dir: Path, name: str) -> Path:
    """Where deliverable ``name``'s watermarked preview lives in the job dir."""
    return job_dir / f"{PREVIEW_PREFIX}{name}.mp4"


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run FFmpeg, surfacing its stderr (not a bare exit code) on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise PreviewError(f"ffmpeg timed out after {_FFMPEG_TIMEOUT_S}s") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-800:] or "(no stderr)"
        raise PreviewError(f"ffmpeg failed (exit {proc.returncode}): {tail}")


def render_preview(
    src: Path, out: Path, watermark_png: Path, *, runner: Runner | None = None
) -> Path:
    """Transcode one clean master to its watermarked 720p preview.

    A single FFmpeg pass: scale/pad to 1280x720, composite the full-frame watermark
    PNG at ``0:0``, re-encode cheap (the preview is a teaser, not the product).
    """
    run = runner or _run_ffmpeg
    filter_complex = (
        f"[0:v]scale={PREVIEW_W}:{PREVIEW_H}:force_original_aspect_ratio=decrease,"
        f"pad={PREVIEW_W}:{PREVIEW_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[v];"
        f"[v][1:v]overlay=0:0,format=yuv420p[vo]"
    )
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(src),
        "-i", str(watermark_png),
        "-filter_complex", filter_complex,
        "-map", "[vo]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    run(cmd)
    if not out.exists():
        raise PreviewError(f"preview transcode produced no file: {out}")
    return out


def render_job_previews(
    job: Job, store: JobStore, settings: Settings, *, runner: Runner | None = None
) -> dict[str, str]:
    """Render every video deliverable's watermarked preview for a Path-B job.

    No-op (``{}``) unless ``job.entitlement`` is ``preview_only`` — Path-A jobs gain
    no new failure mode. For Path B this is load-bearing (a locked gallery with
    nothing watchable breaks the product), so failures raise: the caller's existing
    except-branch marks the job ``failed`` and it can be re-queued.

    Sources are ``job.outputs``'s videos (the scene-pipeline packages), else the
    classic pipeline's ``final.mp4``. Returns ``{name: preview path}`` — informational
    only; previews are found again by filename convention, never via ``Job.outputs``.
    """
    if job.entitlement is not Entitlement.preview_only:
        return {}

    job_dir = store.dir(job.job_id)
    sources: dict[str, Path] = {}
    for name, path in (job.outputs or {}).items():
        if name == "photos":
            continue  # a directory of stills, not a video
        p = Path(path)
        if p.is_file():
            sources[name] = p
    if not sources:
        final = store.final_path(job.job_id)
        if final.is_file():
            sources["final"] = final
    if not sources:
        raise PreviewError(f"job {job.job_id} has no rendered video to preview")

    # The dropzone logo makes the mark unmistakably branded; a missing asset just
    # means a text-only watermark, never a failed preview.
    logo = Path(settings.watermark_logo) if settings.watermark_logo else None
    if logo is not None and not logo.is_file():
        logger.warning("watermark logo %s not found; rendering text-only watermark", logo)
        logo = None

    rendered: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="watermark-") as tmp:
        png = render_watermark(
            Path(tmp) / "watermark.png",
            width=PREVIEW_W,
            height=PREVIEW_H,
            brand=settings.delivery_brand_name,
            logo_path=logo,
        )
        for name, src in sources.items():
            out = preview_path(job_dir, name)
            render_preview(src, out, png, runner=runner)
            rendered[name] = str(out)
            logger.info("preview rendered for job %s: %s", job.job_id, out.name)
    return rendered
