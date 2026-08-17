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

Locked **photos** get the same treatment per still (BUG 350): the gallery's Photos
tab shows a watermarked, downscaled preview of every image behind an "unlock your
photos" offer, produced lazily by :func:`ensure_photo_preview` on first request and
cached in ``<job_dir>/preview_photos/``. Deliberately OUTSIDE ``photos/``: the paid
zip archives that directory recursively, delivery's per-photo S3 uploads glob it, and
the jump archive mirrors it — a preview inside would leak into all three.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from render.watermark import render_watermark

from .config import Settings
from .jobs import FINAL_DELIVERABLE, Job, JobStore, locked_deliverables

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


#: Where a job's watermarked photo previews live (a sibling of ``photos/``, never
#: inside it — see the module docstring).
PHOTO_PREVIEW_DIRNAME = "preview_photos"
#: Longest edge of a photo preview. Big enough to sell the moment in the grid and
#: lightbox, far from the full-res still the customer is buying.
PHOTO_PREVIEW_MAX_EDGE = 1280
#: JPEG quality of a photo preview — a teaser, not the product.
_PHOTO_PREVIEW_QUALITY = 72


def preview_path(job_dir: Path, name: str) -> Path:
    """Where deliverable ``name``'s watermarked preview lives in the job dir."""
    return job_dir / f"{PREVIEW_PREFIX}{name}.mp4"


def photo_preview_path(job_dir: Path, filename: str) -> Path:
    """Where the watermarked preview of still ``filename`` lives in the job dir."""
    return job_dir / PHOTO_PREVIEW_DIRNAME / filename


def ensure_photo_preview(job_dir: Path, filename: str, settings: Settings) -> Path | None:
    """Return the watermarked preview of one still, rendering it on first request.

    Lazy + disk-cached (re-rendered if the source still is newer), so unlock stays a
    one-field state change, previews exist for jobs rendered before this feature, and
    a 50-image grid costs its Pillow pass exactly once per photo.

    **Never raises, and never falls back to the clean file**: ``None`` tells the
    caller no preview could be produced, and the caller must refuse the request — a
    watermark failure that served the full-res still would be a paywall bypass.
    """
    src = job_dir / "photos" / filename
    out = photo_preview_path(job_dir, filename)
    try:
        if not src.is_file():
            return None
        if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
            return out

        from PIL import Image

        with Image.open(src) as raw:
            im = raw.convert("RGB")
        im.thumbnail((PHOTO_PREVIEW_MAX_EDGE, PHOTO_PREVIEW_MAX_EDGE))

        logo = Path(settings.watermark_logo) if settings.watermark_logo else None
        if logo is not None and not logo.is_file():
            logo = None  # text-only mark, never a failed preview
        with tempfile.TemporaryDirectory(prefix="photo-watermark-") as tmp:
            png = render_watermark(
                Path(tmp) / "watermark.png",
                width=im.width,
                height=im.height,
                brand=settings.delivery_brand_name,
                message="Preview — unlock your photos",
                logo_path=logo,
            )
            with Image.open(png) as overlay:
                marked = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")

        out.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a concurrent request for the same still never reads a
        # half-written file (FastAPI serves these from a thread pool).
        part = out.with_name(f".{uuid.uuid4().hex}.part")
        marked.save(part, format="JPEG", quality=_PHOTO_PREVIEW_QUALITY)
        part.replace(out)
        return out
    except Exception:
        logger.warning(
            "photo preview failed for %s/%s; refusing (never the clean file)",
            job_dir.name, filename, exc_info=True,
        )
        return None


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
    """Render a watermarked preview for every LOCKED video deliverable of this job.

    Which deliverables are locked is asked **per name** (:func:`api.jobs.entitlement_for`),
    not per job. For the ordinary single-media-ref job that is the same question as
    before — a wholly ``preview_only`` job previews everything, an ``edited_download``
    job previews nothing and gains no new failure mode. For a **mixed** job (a paid
    handcam edit plus a spec external one) it is the only correct question: the job's
    own ``entitlement`` is ``edited_download`` because the customer bought the handcam,
    yet the external deliverables still need watermarked bytes to serve.

    Where any deliverable IS locked this is load-bearing (a locked card with nothing
    watchable breaks the product), so failures raise: the caller's existing except-branch
    marks the job ``failed`` and it can be re-queued.

    Sources are ``job.outputs``'s videos (the scene-pipeline packages), else the classic
    pipeline's ``final.mp4``. Returns ``{name: preview path}`` — informational only;
    previews are found again by filename convention, never via ``Job.outputs``.
    """
    locked = locked_deliverables(job)
    if not locked:
        return {}

    job_dir = store.dir(job.job_id)
    sources: dict[str, Path] = {}
    for name, path in (job.outputs or {}).items():
        if name == "photos" or name not in locked:
            continue  # a directory of stills, or a deliverable the customer owns
        p = Path(path)
        if p.is_file():
            sources[name] = p
    if not sources and FINAL_DELIVERABLE in locked:
        final = store.final_path(job.job_id)
        if final.is_file():
            sources[FINAL_DELIVERABLE] = final
    if not sources:
        raise PreviewError(
            f"job {job.job_id} has no rendered video to preview for its locked "
            f"deliverable(s): {', '.join(sorted(locked))}"
        )

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
