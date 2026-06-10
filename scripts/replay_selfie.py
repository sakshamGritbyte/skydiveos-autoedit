"""Re-render a selfie job's videos from its (hand-edited) EDL files.

The selfie pipeline persists three editable recipes per job —
``edl_full.json``, ``edl_highlights.json``, ``edl_freefall.json`` — each a list of
clips ``{"scene","src_start","src_end","speed_multiplier"}`` (times are seconds on
that scene's own MP4). Edit those timestamps (e.g. to set exactly where the exit
starts/ends), then run this to re-render the three MP4s against the existing scenes.
Scenes, scores, and photos are left untouched — only the render step re-runs.

Usage:
    python scripts/replay_selfie.py <job_id> [--jobs-root DIR]

Prints the rendered output paths.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a file (like scripts/process_jump.py): put the repo root on the path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.jobs import JobStore  # noqa: E402
from api.selfie import replay_selfie  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="the job whose edl_*.json files to re-render")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    args = parser.parse_args(argv)

    store = JobStore(args.jobs_root)
    if not store.exists(args.job_id):
        parser.error(f"job not found: {args.job_id}")

    outputs = replay_selfie(args.job_id, store=store, jobs_root=args.jobs_root)
    for name, path in outputs.items():
        sys.stdout.write(f"{name}: {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
