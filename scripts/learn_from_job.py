"""Teach the offline editor from a finalized job's edits.

Run this once you're happy with a job's hand-edited ``edl_*.json`` files. It records
that job as a *style exemplar* (how long you keep each scene, how many freefall beats
you feature) and re-aggregates the learned ``style_profile.json``. New jobs composed
offline then follow that pacing automatically.

Only generalisable style is learned — never exact timestamps (those don't transfer
between jumps). The more finalized jobs you feed in, the steadier the style.

Usage:
    python scripts/learn_from_job.py <job_id> [--jobs-root DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.jobs import JobStore  # noqa: E402
from api.selfie import capture_exemplar, learn_style_profile  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="a job whose finalized edl_*.json files to learn from")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    args = parser.parse_args(argv)

    if not JobStore(args.jobs_root).exists(args.job_id):
        parser.error(f"job not found: {args.job_id}")

    exemplar = capture_exemplar(args.job_id, args.jobs_root)
    profile = learn_style_profile(args.jobs_root)
    sys.stdout.write(f"captured exemplar: {json.dumps(exemplar)}\n")
    sys.stdout.write(f"learned style profile ({profile.get('samples')} samples):\n")
    sys.stdout.write(json.dumps(profile, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
