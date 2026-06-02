"""CLI: render a previously-composed EDL against its full-res master.

Usage:
    python -m render <source.mp4> --job-id <id> --customer "Jane Doe" \\
        [--date 2026-06-02] [--edl path/edl.json] [--music upbeat_indie] \\
        [--jobs-root ./jobs] [--templates-dir ./templates]

With no ``--edl`` the job's persisted ``<jobs_root>/{job_id}/edl.json`` is loaded
(the Compose stage wrote it there), so this doubles as a re-render of an approved
edit. Prints the path to the written ``final.mp4``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from edl.schema import EditDecisionList
from edl.storage import load_edl

from . import render_edl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m render", description=__doc__)
    parser.add_argument("source", help="full-res master MP4 to cut from")
    parser.add_argument("--job-id", required=True, help="job id (output dir + persisted EDL)")
    parser.add_argument("--customer", required=True, help="customer name (burned on intro)")
    parser.add_argument("--date", default=None, help="jump date for the intro (default: today)")
    parser.add_argument("--edl", default=None, help="EDL JSON path (default: the job's edl.json)")
    parser.add_argument("--music", default=None, help="music track name in templates/music/")
    parser.add_argument("--music-path", default=None, help="explicit backing-track path")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    parser.add_argument("--templates-dir", default=None, help="override /templates root")
    parser.add_argument("--font", default=None, help="explicit caption font path")
    args = parser.parse_args(argv)

    if args.edl:
        edl = EditDecisionList.model_validate_json(Path(args.edl).read_text())
    else:
        edl = load_edl(args.job_id, args.jobs_root)

    out = render_edl(
        edl,
        args.source,
        args.job_id,
        customer_name=args.customer,
        jump_date=args.date or date.today().isoformat(),
        jobs_root=args.jobs_root,
        templates_dir=args.templates_dir,
        music=args.music,
        music_path=args.music_path,
        font_path=args.font,
    )
    sys.stdout.write(f"{out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
