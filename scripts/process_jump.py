"""End-to-end: a raw GoPro MP4 -> a customer-ready ``final.mp4``.

This wires the pipeline together for a single jump so the whole thing can be run
(and tested) against a sample file:

    metadata (timeline)  ->  EDL  ->  render

The EDL normally comes from the Compose stage (Claude — :func:`edl.compose_edl`),
but that needs the freefall scores (a MediaPipe pass) and an API key. So that this
script runs offline, deterministically, and fast — e.g. in CI against a sample —
it builds the edit with a small, rule-based **house cut** (:func:`house_cut`) from
the phase timeline alone when no EDL is supplied. The cut honours the house style
(slow-mo the exit, feature freefall, trim the canopy ride short, end on landing);
swap in ``edl.compose_edl`` for the AI edit in production.

Usage:
    python scripts/process_jump.py <path/to/raw.mp4> \\
        [--job-id ID] [--customer "Jane Doe"] [--date 2026-06-02] \\
        [--edl edl.json] [--music NAME|--music-path PATH] \\
        [--jobs-root DIR] [--templates-dir DIR] [--target-duration 90]

Prints the path to the rendered ``final.mp4``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

# Allow running as a file (``python scripts/process_jump.py ...``, per CLAUDE.md),
# not just as a module: put the repo root on sys.path so the pipeline packages
# import. Harmless when already importable (e.g. under pytest's pythonpath=".").
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from edl.schema import Clip, EditDecisionList, Transition  # noqa: E402
from edl.storage import persist_edl  # noqa: E402
from metadata import extract_metadata  # noqa: E402
from render import render_edl  # noqa: E402

# Default cut geometry (seconds). These shape the house cut; the AI editor in
# production makes finer, score-driven choices.
_EXIT_SPEED = 0.4          # slow-mo the exit moment
_FREEFALL_BEATS = 3        # number of freefall windows to feature
_BEAT_SECONDS = 6.0        # length of each freefall beat (source seconds)
_DEPLOY_SECONDS = 4.0      # how much of the opening to show
_CANOPY_SECONDS = 5.0      # CLAUDE.md: trim the canopy ride to ~5 s


def _probe_duration(path: Path) -> float:
    """Container duration of ``path`` in seconds (ffprobe), or 0.0 if unknown."""
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
    except ValueError:
        return 0.0


def _clip(
    start: float,
    end: float,
    limit: float,
    *,
    speed: float = 1.0,
    transition_in: Transition | None = None,
    transition_out: Transition | None = None,
) -> Clip | None:
    """A :class:`Clip` clamped to ``[0, limit]``, or ``None`` if it collapses."""
    start = max(0.0, start)
    end = min(end, limit) if limit > 0 else end
    if end - start < 0.1:  # too short to be worth a cut
        return None
    return Clip(
        src_start=start, src_end=end, speed_multiplier=speed,
        transition_in=transition_in, transition_out=transition_out,
    )


def house_cut(
    timeline: dict[str, float | None],
    *,
    source_duration: float,
    target_duration: float = 90.0,
) -> EditDecisionList:
    """Build a rule-based EDL from the phase timeline (offline stand-in for Claude).

    Follows the house style: open on a slow-mo exit, feature a few freefall beats
    (the middle one in slow-mo), show the opening, hard-cut the canopy ride down to
    ~5 s, and end on the landing. Every window is clamped to the real source length
    so we never trim past EOF. Guarantees at least one clip.

    Args:
        timeline: Phase -> seconds mapping from :func:`metadata.extract_metadata`
            (any phase may be ``None``).
        source_duration: Length of the master, for clamping (0 disables clamping).
        target_duration: Desired output length; trims trailing beats once reached.

    Returns:
        A validated :class:`EditDecisionList`.
    """
    limit = source_duration
    exit_t = timeline.get("exit")
    ff_start = timeline.get("freefall_start")
    ff_end = timeline.get("freefall_end")
    deployment = timeline.get("deployment")
    canopy_start = timeline.get("canopy_start")
    landing = timeline.get("landing")
    landing_end = timeline.get("landing_end")

    clips: list[Clip] = []

    # Open on the exit in slow motion (exit -> freefall_start).
    if exit_t is not None and ff_start is not None and ff_start > exit_t:
        c = _clip(exit_t, ff_start, limit, speed=_EXIT_SPEED, transition_in=Transition.fade)
        if c:
            clips.append(c)

    # Feature a few freefall beats spread across the window; middle one slow-mo.
    if ff_start is not None and ff_end is not None and ff_end > ff_start:
        span = ff_end - ff_start
        step = span / _FREEFALL_BEATS
        mid = _FREEFALL_BEATS // 2
        for i in range(_FREEFALL_BEATS):
            beat_start = ff_start + i * step
            beat = _clip(
                beat_start, beat_start + _BEAT_SECONDS, min(limit, ff_end) if limit else ff_end,
                speed=_EXIT_SPEED if i == mid else 1.0,
            )
            if beat:
                clips.append(beat)

    # The opening (deployment -> canopy_start, or a fixed window).
    if deployment is not None:
        deploy_end = canopy_start if canopy_start is not None else deployment + _DEPLOY_SECONDS
        c = _clip(deployment, deploy_end, limit)
        if c:
            clips.append(c)

    # Canopy ride: a single short hard cut (it is mostly boring — trim it).
    if canopy_start is not None:
        c = _clip(canopy_start, canopy_start + _CANOPY_SECONDS, limit)
        if c:
            clips.append(c)

    # End on the landing.
    if landing is not None and landing_end is not None and landing_end > landing:
        c = _clip(landing, landing_end, limit, transition_out=Transition.fade)
        if c:
            clips.append(c)

    # Always produce something renderable, even from a bare timeline.
    if not clips:
        fallback_end = min(source_duration, 30.0) if source_duration else 30.0
        fallback = _clip(0.0, fallback_end, limit, transition_in=Transition.fade)
        clips.append(fallback or Clip(src_start=0.0, src_end=max(fallback_end, 1.0)))

    # Trim trailing beats once we've reached the target runtime (keep >= 1 clip).
    trimmed: list[Clip] = []
    total = 0.0
    for clip in clips:
        trimmed.append(clip)
        total += clip.output_duration
        if total >= target_duration:
            break

    return EditDecisionList(
        clips=trimmed,
        notes=(
            "Offline house cut from the phase timeline (no AI). Exit + peak "
            "freefall in slow-mo; canopy trimmed; ends on landing."
        ),
    )


def process_jump(
    source_path: str | Path,
    *,
    job_id: str,
    customer_name: str,
    jump_date: str,
    edl_path: str | Path | None = None,
    music: str | None = None,
    music_path: str | Path | None = None,
    jobs_root: str | Path | None = None,
    templates_dir: str | Path | None = None,
    target_duration: float = 90.0,
    preset: str = "veryfast",
) -> Path:
    """Run the metadata -> EDL -> render pipeline for one jump and return ``final.mp4``.

    Uses the supplied ``edl_path`` if given, else composes an offline
    :func:`house_cut` from the detected timeline. The EDL is persisted to the job
    dir (so the render is replayable) before rendering.
    """
    source = Path(source_path)

    if edl_path is not None:
        edl = load_edl_from(edl_path)
    else:
        timeline = extract_metadata(source)
        edl = house_cut(
            timeline,
            source_duration=_probe_duration(source),
            target_duration=target_duration,
        )

    persist_edl(edl, job_id, jobs_root)

    return render_edl(
        edl,
        source,
        job_id,
        customer_name=customer_name,
        jump_date=jump_date,
        jobs_root=jobs_root,
        templates_dir=templates_dir,
        music=music,
        music_path=music_path,
        preset=preset,
    )


def load_edl_from(path: str | Path) -> EditDecisionList:
    """Load an EDL from an explicit JSON file (vs. the job's persisted one)."""
    return EditDecisionList.model_validate_json(Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="path to the raw GoPro MP4")
    parser.add_argument("--job-id", default=None, help="job id (default: the file stem)")
    parser.add_argument("--customer", default="Valued Skydiver", help="customer name for the intro")
    parser.add_argument("--date", default=None, help="jump date for the intro (default: today)")
    parser.add_argument("--edl", default=None, help="use this EDL JSON instead of the house cut")
    parser.add_argument("--music", default=None, help="music track name in templates/music/")
    parser.add_argument("--music-path", default=None, help="explicit backing-track path")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    parser.add_argument("--templates-dir", default=None, help="override /templates root")
    parser.add_argument("--target-duration", type=float, default=90.0, help="target length (s)")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        parser.error(f"source not found: {source}")

    out = process_jump(
        source,
        job_id=args.job_id or source.stem,
        customer_name=args.customer,
        jump_date=args.date or date.today().isoformat(),
        edl_path=args.edl,
        music=args.music,
        music_path=args.music_path,
        jobs_root=args.jobs_root,
        templates_dir=args.templates_dir,
        target_duration=args.target_duration,
    )
    sys.stdout.write(f"{out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
