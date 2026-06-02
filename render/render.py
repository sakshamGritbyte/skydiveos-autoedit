"""Render stage (stage 5): execute an EDL against the full-res MP4 with FFmpeg.

This is the orchestrator that turns the pure graph from :mod:`render.builder` into
an actual ``<jobs_root>/{job_id}/final.mp4``. It:

1. probes the source for audio + the cards for their durations,
2. renders the customer caption PNG (:mod:`render.caption`),
3. resolves / synthesises the intro & outro cards and the music bed
   (:mod:`render.templates`),
4. builds the ``filter_complex`` (:mod:`render.builder`), and
5. runs one FFmpeg pass to 1080p / h264 / 30fps.

Per CLAUDE.md this runs on the **full-res master** (the only stage that does) and
only *after* instructor approval — callers (the /api approve endpoint,
``scripts/replay_edl.py``) own that gate; this module just executes.

All scratch assets (caption PNG, any synthesised default cards) live in a temp dir
that is cleaned up after the FFmpeg pass, so the only artifact left behind is
``final.mp4`` next to the job's ``edl.json``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from edl.schema import EditDecisionList
from edl.storage import job_dir

from .builder import OUT_FPS, OUT_HEIGHT, OUT_WIDTH, FilterGraph, build_filtergraph
from .caption import render_caption
from .templates import resolve_intro, resolve_music, resolve_outro

logger = logging.getLogger(__name__)

FINAL_FILENAME = "final.mp4"

# Encode settings. ``veryfast`` keeps a ~90 s 1080p edit comfortably inside the
# per-jump GPU/CPU budget while CRF 23 holds quality for a keepsake; tunable per
# call (tests drop to ``ultrafast``).
DEFAULT_PRESET = "veryfast"
DEFAULT_CRF = 23
AUDIO_BITRATE = "192k"
DEFAULT_TIMEOUT_S = 600

# Default synthesised card length when /templates has no intro/outro yet.
_DEFAULT_CARD_SECONDS = 2.0


class RenderError(RuntimeError):
    """Raised when the render cannot be produced (missing source/ffmpeg, bad graph)."""


def _require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RenderError(f"{tool} not found on PATH (required to render)")


def _has_audio_stream(path: Path) -> bool:
    """True if ``path`` has at least one audio stream (per ffprobe)."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return bool(out)


def _probe_duration(path: Path) -> float:
    """Container duration of ``path`` in seconds (via ffprobe)."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError as e:
        raise RenderError(f"could not probe duration of {path.name}: {out!r}") from e


def _synth_card(out_path: Path, *, color: str, duration: float, width: int, height: int) -> Path:
    """Generate a plain solid-colour card when /templates has none.

    A graceful stand-in so the pipeline runs end-to-end before the branded cards
    are dropped into /templates; production swaps these for the real PSD exports.
    """
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:size={width}x{height}:duration={duration}:rate={OUT_FPS}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
        ],
        check=True,
    )
    return out_path


def _build_command(
    graph: FilterGraph, out_path: Path, *, preset: str, crf: int
) -> list[str]:
    """Assemble the full FFmpeg argv from a built graph + output settings."""
    cmd = ["ffmpeg", "-v", "error", "-y", *graph.input_args()]
    cmd += ["-filter_complex", graph.filter_complex, "-map", f"[{graph.video_label}]"]
    if graph.audio_label is not None:
        cmd += ["-map", f"[{graph.audio_label}]"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-r", str(OUT_FPS),
    ]
    if graph.audio_label is not None:
        cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


def render_edl(
    edl: EditDecisionList,
    source_path: str | Path,
    job_id: str,
    *,
    customer_name: str,
    jump_date: str,
    jobs_root: str | Path | None = None,
    templates_dir: str | Path | None = None,
    intro_path: str | Path | None = None,
    outro_path: str | Path | None = None,
    music: str | None = None,
    music_path: str | Path | None = None,
    font_path: str | None = None,
    width: int = OUT_WIDTH,
    height: int = OUT_HEIGHT,
    preset: str = DEFAULT_PRESET,
    crf: int = DEFAULT_CRF,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Path:
    """Render ``edl`` against the full-res ``source_path`` to ``final.mp4``.

    Args:
        edl: The approved edit to execute.
        source_path: The full-res master MP4 the clips are cut from.
        job_id: Job whose ``<jobs_root>/{job_id}/`` directory receives ``final.mp4``.
        customer_name: Burned onto the intro card.
        jump_date: Burned onto the intro card (pre-formatted, e.g. ``2026-06-02``).
        jobs_root: Override the jobs root (else ``$JOBS_ROOT`` / ``./jobs``).
        templates_dir: Override the /templates root for card/music lookup.
        intro_path / outro_path: Explicit card overrides; otherwise resolved from
            /templates, and synthesised as plain cards if absent there too.
        music: Track name to pull from ``templates/music/`` (e.g. the EDL's
            ``music`` field). Ignored if ``music_path`` is given.
        music_path: Explicit backing-track path (wins over ``music``). ``None`` and
            no resolvable track means a music-free render.
        font_path: Explicit caption font (else auto-resolved; see
            :func:`render.caption.resolve_font`).
        width / height: Output geometry (default 1080p).
        preset / crf: x264 encode settings.
        timeout: Hard cap on the FFmpeg pass (seconds).

    Returns:
        Path to the written ``final.mp4``.

    Raises:
        RenderError: missing ffmpeg/source, or the FFmpeg pass failed/timed out.
    """
    _require_tools()
    source = Path(source_path)
    if not source.exists():
        raise RenderError(f"source not found: {source}")

    out_dir = job_dir(job_id, jobs_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / FINAL_FILENAME

    has_audio = _has_audio_stream(source)

    # Resolve the backing track: explicit path > named template > first template.
    resolved_music: Path | None
    if music_path is not None:
        resolved_music = Path(music_path)
        if not resolved_music.exists():
            raise RenderError(f"music_path not found: {resolved_music}")
    else:
        resolved_music = resolve_music(music, templates_dir)

    with tempfile.TemporaryDirectory(prefix=f"render-{job_id}-") as tmp:
        tmp_dir = Path(tmp)

        # Intro/outro: explicit > /templates > synthesised default card.
        intro = Path(intro_path) if intro_path else resolve_intro(templates_dir)
        if intro is None:
            intro = _synth_card(
                tmp_dir / "intro.mp4", color="black",
                duration=_DEFAULT_CARD_SECONDS, width=width, height=height,
            )
        outro = Path(outro_path) if outro_path else resolve_outro(templates_dir)
        if outro is None:
            outro = _synth_card(
                tmp_dir / "outro.mp4", color="black",
                duration=_DEFAULT_CARD_SECONDS, width=width, height=height,
            )

        intro_duration = _probe_duration(intro)
        outro_duration = _probe_duration(outro)

        caption = render_caption(
            tmp_dir / "caption.png",
            customer_name=customer_name, jump_date=jump_date,
            width=width, height=height, font_path=font_path,
        )

        graph = build_filtergraph(
            edl,
            str(source),
            has_audio=has_audio,
            intro_path=str(intro),
            intro_duration=intro_duration,
            outro_path=str(outro),
            outro_duration=outro_duration,
            music_path=str(resolved_music) if resolved_music else None,
            caption_path=str(caption),
            width=width,
            height=height,
        )

        cmd = _build_command(graph, out_path, preset=preset, crf=crf)
        logger.info("rendering job %s -> %s", job_id, out_path)
        try:
            proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RenderError(f"render timed out after {timeout}s for job {job_id}") from e

    if proc.returncode != 0:
        raise RenderError(
            f"ffmpeg failed for job {job_id}: "
            f"{proc.stderr.decode(errors='replace')[:1000]}"
        )
    logger.info("rendered job %s (%d bytes)", job_id, out_path.stat().st_size)
    return out_path
