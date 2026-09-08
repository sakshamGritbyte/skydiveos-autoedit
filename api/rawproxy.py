"""Browser-playable proxies for the purchased Raw Footage section.

A camera master is whatever the GoPro wrote: 4K/5.3K, 60 fps, and on every recent
model **HEVC**. Chrome/Edge/Firefox cannot decode HEVC (and choke on a 100 Mbit/s 4K
H.264 stream over the internet), so the gallery's ``<video src=master>`` showed the
duration and a black frame — the container parsed, the picture never came. The
customer had paid for footage they could download but not watch.

This module produces, per master, ONE web proxy — H.264 / AAC, capped at
:attr:`~api.config.Settings.raw_web_max_height`, ``+faststart`` — under the job's
``raw-web/`` directory, mirroring the ``raw/`` tree (``raw-web/<role>/<name>`` for a
role-staged job). Three rules:

* **The player streams the proxy; the Download button stays on the master.** The
  customer bought exactly the camera's bytes, and that is what they save. The proxy
  exists only so the page can show them what they bought.
* **A master the browser can already play gets no proxy** — an H.264 file at or under
  the cap is served as-is, recorded by a ``<name>.native`` marker so the gallery can
  tell "nothing to do" from "not done yet". Re-encoding it would only lose quality.
* **``raw-web/`` is a sibling of ``raw/``, never inside it.** ``raw/`` is what the
  jump archive mirrors, what the pruner reasons about and what
  ``_gallery_raw_clips`` lists as the customer's product; a proxy inside it would be
  listed as a second camera master.

The transcode is queued when the ``raw`` add-on is purchased (``POST /jobs/{id}/unlock``)
and, as a self-heal for purchases that predate this or a lost task, again from the
gallery request when a master is still pending (rate-limited by a dispatch marker).
Until the proxy lands the card says so — a black player is the bug, not a state.
Every state is derived from files on disk, so a restarted worker or a wiped
``raw-web/`` simply rebuilds: nothing here is recorded on the job.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Settings
from .jobs import Job, JobStore

logger = logging.getLogger(__name__)

#: Where a job's web proxies live — beside ``raw/``, never inside it (module docstring).
RAW_WEB_DIRNAME = "raw-web"
#: Marker beside where a proxy would be: the master itself is browser-playable.
NATIVE_SUFFIX = ".native"
#: Marker beside where a proxy would be: the transcode failed (its text is the reason).
#: The gallery falls back to the master for that card and the job is not re-queued in a
#: loop; deleting the marker (or ``raw-web/``) lets the next dispatch retry.
FAILED_SUFFIX = ".failed"
#: Written in progress; ``os.replace``d into place so a half-written proxy is never served.
PARTIAL_SUFFIX = ".part"
#: Touched when a proxy render is queued; the gallery's self-heal re-queues a job with
#: pending masters only once this is older than :data:`REDISPATCH_AFTER_S`.
DISPATCH_MARKER = ".dispatched"
REDISPATCH_AFTER_S = 30 * 60
#: A 30-minute 5.3K HEVC master transcodes in the tens of minutes on a small box; this
#: is a backstop against a hung ffmpeg, not a budget.
FFMPEG_TIMEOUT_S = 3 * 3600.0
#: Codecs every current browser decodes in an MP4 container.
BROWSER_CODECS = frozenset({"h264"})
#: Pixel formats every browser decodes; 10-bit / 4:2:2 H.264 is NOT playable in Chrome.
BROWSER_PIX_FMTS = frozenset({"yuv420p", "yuvj420p"})

#: Injectable command runner (tests fake it; default runs FFmpeg).
Runner = Callable[[list[str]], None]

Playback = Literal["proxy", "native", "pending", "failed"]


class RawProxyError(RuntimeError):
    """Raised when a web proxy cannot be produced."""


@dataclass(frozen=True)
class VideoStream:
    """What decides playability: the first video stream's codec, height and pixel format."""

    codec: str | None
    height: int | None
    pix_fmt: str | None


Prober = Callable[[Path], VideoStream]


# --------------------------------------------------------------------------- #
# Masters and their proxy paths
# --------------------------------------------------------------------------- #


def iter_raw_masters(raw_dir: Path) -> Iterator[Path]:
    """The customer's camera masters under ``raw/``: MP4s only, sorted, any depth ≤ 1.

    ``.lrv`` GoPro proxies stage beside their MP4 for analysis and are pipeline
    internals, never a product — the same filter the gallery listing applies.
    """
    if not raw_dir.is_dir():
        return
    for p in sorted(raw_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() == ".mp4":
            yield p


def web_dir(store: JobStore, job_id: str) -> Path:
    return store.dir(job_id) / RAW_WEB_DIRNAME


def proxy_path(store: JobStore, job_id: str, rel: str) -> Path:
    """Where ``raw/<rel>``'s web proxy lives: ``raw-web/<rel>`` (same name, same tree)."""
    return web_dir(store, job_id) / rel


def _is_fresh(candidate: Path, master: Path) -> bool:
    """A proxy/marker written for THIS master — not one left over from a re-upload."""
    try:
        return candidate.stat().st_mtime >= master.stat().st_mtime
    except OSError:
        return False


def playback_for(store: JobStore, job_id: str, rel: str) -> Playback:
    """How the gallery should play ``raw/<rel>`` — decided from files on disk only.

    * ``proxy``   — a finished web proxy exists (and is newer than the master).
    * ``native``  — the master is browser-playable as-is (``.native`` marker).
    * ``failed``  — the transcode failed; play the master, don't re-queue.
    * ``pending`` — none of the above: not probed/rendered yet.
    """
    master = store.raw_dir(job_id) / rel
    out = proxy_path(store, job_id, rel)
    if out.is_file() and _is_fresh(out, master):
        return "proxy"
    native = out.with_name(out.name + NATIVE_SUFFIX)
    if native.is_file() and _is_fresh(native, master):
        return "native"
    failed = out.with_name(out.name + FAILED_SUFFIX)
    if failed.is_file() and _is_fresh(failed, master):
        return "failed"
    return "pending"


def pending_masters(store: JobStore, job: Job) -> list[str]:
    """Relpaths (under ``raw/``) of purchased masters that still have no playback decision."""
    raw_dir = store.raw_dir(job.job_id)
    return [
        str(p.relative_to(raw_dir))
        for p in iter_raw_masters(raw_dir)
        if playback_for(store, job.job_id, str(p.relative_to(raw_dir))) == "pending"
    ]


# --------------------------------------------------------------------------- #
# Dispatch bookkeeping (the gallery's self-heal)
# --------------------------------------------------------------------------- #


def needs_dispatch(store: JobStore, job: Job, *, now: float | None = None) -> bool:
    """Should the gallery (re-)queue this job's proxy render?

    True when the customer owns ``raw``, some master is still ``pending``, and no
    dispatch is recorded within :data:`REDISPATCH_AFTER_S`. The window is what keeps a
    polled gallery from queueing the same multi-GB transcode every six seconds.
    """
    if "raw" not in job.addons or not pending_masters(store, job):
        return False
    marker = web_dir(store, job.job_id) / DISPATCH_MARKER
    try:
        age = (now if now is not None else time.time()) - marker.stat().st_mtime
    except OSError:
        return True
    return age >= REDISPATCH_AFTER_S


def mark_dispatched(store: JobStore, job_id: str) -> None:
    """Record that a proxy render was queued now (never raises — bookkeeping only)."""
    try:
        d = web_dir(store, job_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / DISPATCH_MARKER).touch()
    except OSError:
        logger.warning("raw-web: could not write dispatch marker for %s", job_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Probe + transcode
# --------------------------------------------------------------------------- #


def probe_video_stream(path: Path) -> VideoStream:
    """First video stream's ``codec_name,height,pix_fmt`` via ffprobe (seam; faked in tests)."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,height,pix_fmt",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ],
        capture_output=True, text=True, check=False, timeout=120,
    )
    fields: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        k, _, v = line.partition("=")
        if k and v:
            fields[k.strip()] = v.strip()
    height: int | None
    try:
        height = int(fields["height"])
    except (KeyError, ValueError):
        height = None
    return VideoStream(
        codec=fields.get("codec_name") or None, height=height, pix_fmt=fields.get("pix_fmt") or None
    )


def is_browser_native(stream: VideoStream, *, max_height: int) -> bool:
    """Can the master be served to a ``<video>`` as-is?

    Only when the codec, pixel format AND size are all known and all within what every
    browser decodes. An unreadable probe is *not* native — an unknown master is
    transcoded, which is the safe direction (a proxy of a playable file is merely
    redundant; a black player is the bug).
    """
    return (
        stream.codec in BROWSER_CODECS
        and stream.pix_fmt in BROWSER_PIX_FMTS
        and stream.height is not None
        and 0 < stream.height <= max_height
    )


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run FFmpeg, surfacing its stderr (not a bare exit code) on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise RawProxyError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S:.0f}s") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-800:] or "(no stderr)"
        raise RawProxyError(f"ffmpeg failed (exit {proc.returncode}): {tail}")


def proxy_command(src: Path, out: Path, *, max_height: int) -> list[str]:
    """The one-pass H.264/AAC web transcode of a camera master.

    * ``-map 0:v:0 -map 0:a?`` — the picture and the sound, and NOTHING else. A GoPro
      master carries GPMF telemetry and timecode data streams that the MP4 muxer cannot
      write without ``-copy_unknown``; mapping them is a guaranteed failure.
    * ``scale=-2:'min(H,ih)'`` — cap the height, keep the aspect, never upscale, keep the
      width even (libx264 refuses odd dimensions). A portrait clip caps its HEIGHT too.
    * ``yuv420p`` — 10-bit GoPro HEVC (HDR / high-bitrate modes) would otherwise come out
      as ``yuv420p10le``, which Chrome will not decode either.
    * ``+faststart`` — the moov atom up front so the player starts before the download ends.
    """
    return [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        "-sn", "-dn",
        "-vf", f"scale=-2:'min({int(max_height)},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart",
        str(out),
    ]


def render_web_proxy(
    src: Path, out: Path, *, max_height: int, runner: Runner | None = None
) -> Path:
    """Transcode one master to its web proxy, atomically (write ``.part``, then rename)."""
    run = runner or _run_ffmpeg
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + PARTIAL_SUFFIX + out.suffix)
    try:
        run(proxy_command(src, part, max_height=max_height))
        if not part.is_file():
            raise RawProxyError(f"proxy transcode produced no file: {part}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    return out


def render_job_raw_proxies(
    job: Job,
    store: JobStore,
    settings: Settings,
    *,
    runner: Runner | None = None,
    prober: Prober | None = None,
) -> dict[str, Playback]:
    """Bring every purchased master to a playback decision. Never raises.

    Per master: already decided (``proxy``/``native``/``failed`` and fresh) → skip;
    probe → browser-native → ``.native`` marker; else transcode. A failed transcode
    writes a ``.failed`` marker carrying the reason, so the gallery serves the master
    for that card and the self-heal does not re-queue it forever. Returns
    ``{relpath: playback}`` — informational; the gallery re-derives state from disk.

    Runs only for a job that OWNS ``raw`` (the purchase, never the queue, opens the
    work) and only while the feature is on.
    """
    decided: dict[str, Playback] = {}
    if not settings.raw_web_proxies or "raw" not in job.addons:
        return decided
    probe = prober or probe_video_stream
    raw_dir = store.raw_dir(job.job_id)
    for master in iter_raw_masters(raw_dir):
        rel = str(master.relative_to(raw_dir))
        state = playback_for(store, job.job_id, rel)
        if state != "pending":
            decided[rel] = state
            continue
        out = proxy_path(store, job.job_id, rel)
        try:
            stream = probe(master)
            if is_browser_native(stream, max_height=settings.raw_web_max_height):
                out.parent.mkdir(parents=True, exist_ok=True)
                out.with_name(out.name + NATIVE_SUFFIX).touch()
                decided[rel] = "native"
                logger.info("raw-web: %s/%s is browser-native (%s %sp) — no proxy",
                            job.job_id, rel, stream.codec, stream.height)
                continue
            logger.info("raw-web: transcoding %s/%s (%s %sp %s)",
                        job.job_id, rel, stream.codec, stream.height, stream.pix_fmt)
            render_web_proxy(master, out, max_height=settings.raw_web_max_height, runner=runner)
            decided[rel] = "proxy"
        except Exception as e:  # noqa: BLE001 - one bad master must not block the others
            logger.warning("raw-web: proxy failed for %s/%s: %r", job.job_id, rel, e)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.with_name(out.name + FAILED_SUFFIX).write_text(repr(e)[:2000])
            except OSError:
                logger.warning("raw-web: could not write failed marker for %s/%s", job.job_id, rel)
            decided[rel] = "failed"
    return decided
