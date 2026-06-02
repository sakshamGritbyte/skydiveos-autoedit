"""Extracting freefall frames from the LRV proxy with FFmpeg.

The Score stage only ever looks at the freefall window, and only at the *proxy*
(``.lrv``) — never the full-res master. Decoding the 4K master is ~15x slower and
buys us nothing for face scoring (CLAUDE.md: "Always work on ``.lrv`` (proxy) for
analysis"), so this module refuses a non-proxy input unless explicitly overridden.

We pull the segment at a low frame rate (5 fps is plenty for per-second highlight
scoring — we don't need all 30) and downscale to a small width, both to keep the
MediaPipe pass comfortably under the per-jump compute budget.

Timestamps are seconds (float) on the *source* timeline, matching the jump phase
timestamps produced by the /metadata stage.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from .models import AnalysisError

PROXY_SUFFIX = ".lrv"
DEFAULT_FPS = 5.0
DEFAULT_WIDTH = 480


def _probe_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        w_str, h_str = out.split(",")[:2]
        return int(w_str), int(h_str)
    except (ValueError, IndexError) as e:
        raise AnalysisError(f"could not probe video dimensions for {path}: {out!r}") from e


def _target_size(src_w: int, src_h: int, width: int) -> tuple[int, int]:
    """Downscale to ``width`` (never upscale), preserving aspect, both dims even.

    H.264/scale filters want even dimensions; we round to the nearest even value.
    """
    out_w = min(width, src_w)
    out_w -= out_w % 2
    out_w = max(out_w, 2)
    out_h = round(out_w * src_h / src_w)
    out_h -= out_h % 2
    out_h = max(out_h, 2)
    return out_w, out_h


def extract_freefall_frames(
    proxy_path: str | Path,
    freefall_start: float,
    freefall_end: float,
    *,
    fps: float = DEFAULT_FPS,
    width: int = DEFAULT_WIDTH,
    allow_full_res: bool = False,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield ``(timestamp_s, rgb_frame)`` for the freefall window of the proxy.

    Frames are emitted at ``fps`` (constant rate, starting at ``freefall_start``)
    as contiguous ``uint8`` ``H x W x 3`` RGB arrays. The timestamp of frame ``k``
    is ``freefall_start + k / fps`` on the source timeline.

    Args:
        proxy_path: Path to the ``.lrv`` proxy. A non-``.lrv`` input raises unless
            ``allow_full_res`` is set (guards against burning compute on the 4K
            master — see module docstring).
        freefall_start: Window start (seconds, source timeline).
        freefall_end: Window end (seconds). If ``<= freefall_start``, nothing is
            yielded.
        fps: Sampling rate. 5 fps is the project default for highlight scoring.
        width: Target proxy width in pixels (downscaled, never upscaled).
        allow_full_res: Permit a non-proxy input (e.g. tests, debugging).

    Raises:
        AnalysisError: bad/missing proxy, missing ffmpeg/ffprobe, or decode failure.
    """
    path = Path(proxy_path)
    if not path.exists():
        raise AnalysisError(f"proxy not found: {path}")
    if path.suffix.lower() != PROXY_SUFFIX and not allow_full_res:
        raise AnalysisError(
            f"refusing to analyze a non-proxy file ({path.name}); analysis runs on "
            f"the {PROXY_SUFFIX} proxy only. Pass allow_full_res=True to override."
        )
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise AnalysisError(f"{tool} not found on PATH (required to extract frames)")

    duration = freefall_end - freefall_start
    if duration <= 0 or fps <= 0:
        return

    src_w, src_h = _probe_dimensions(str(path))
    out_w, out_h = _target_size(src_w, src_h, width)
    frame_bytes = out_w * out_h * 3

    # -ss before -i: fast keyframe seek to the window start. fps= resamples to a
    # constant rate so frame index maps cleanly back to a timestamp.
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-ss", f"{freefall_start:.3f}",
            "-t", f"{duration:.3f}",
            "-i", str(path),
            "-vf", f"fps={fps},scale={out_w}:{out_h}",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "pipe:1",
        ],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AnalysisError(
            f"ffmpeg failed to decode {path.name}: {proc.stderr.decode(errors='replace')[:500]}"
        )

    raw = proc.stdout
    n_frames = len(raw) // frame_bytes
    for k in range(n_frames):
        chunk = raw[k * frame_bytes:(k + 1) * frame_bytes]
        frame = np.frombuffer(chunk, dtype=np.uint8).reshape(out_h, out_w, 3)
        ts = freefall_start + k / fps
        yield ts, np.ascontiguousarray(frame)
