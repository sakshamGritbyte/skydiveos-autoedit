#!/usr/bin/env python3
"""File existing jobs into the browsable jump archive under ``raw-storage``.

The pipeline archives every job as it runs (:mod:`api.archive`), so this is the
*catch-up* tool for the cases where that didn't happen:

* jobs that finished before the archive existed (backfill);
* a job whose archive pass failed at the time — a full disk, a read-only mount, an
  unmounted NAS — since archiving deliberately logs and moves on rather than failing
  the customer's edit;
* re-filing after a booking correction: the folder is named from the job's
  ``instructor_name`` / ``customer_name`` / ``jump_date``, so fixing a misspelled name
  in ``job.json`` and re-running puts the jump under the right folder.

Usage::

    python scripts/archive_job.py --all                  # every job on disk
    python scripts/archive_job.py <job_id> [<job_id>...]  # named jobs
    python scripts/archive_job.py --all --dry-run        # just show where each would go
    python scripts/archive_job.py --all --link-mode copy # real copies, not hardlinks

Safe to re-run: mirroring is idempotent and hardlinked by default, so a repeat pass
costs a stat per file and no extra disk. It never deletes anything — a folder left
behind by an earlier (mis-named) run has to be removed by hand.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# The project isn't installed as a package (`package = false`), so make the repo root
# importable when this is run as a script from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api import archive  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.jobs import Job, JobStore  # noqa: E402

logger = logging.getLogger("archive_job")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/archive_job.py",
        description="Mirror jobs' raw footage and renders into the jump archive.",
    )
    parser.add_argument("job_ids", nargs="*", help="job ids to archive (default: use --all)")
    parser.add_argument("--all", action="store_true", help="archive every job on disk")
    parser.add_argument(
        "--jobs-root", default=None, help="jobs root override (default: $JOBS_ROOT or ./jobs)"
    )
    parser.add_argument(
        "--archive-root",
        default=None,
        help="archive root override (default: $ARCHIVE_ROOT or $RAW_STORAGE_ROOT)",
    )
    parser.add_argument(
        "--link-mode",
        default=None,
        choices=list(archive.LINK_MODES),
        help="how files are materialised (default: $ARCHIVE_LINK_MODE or 'link')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the folder each job would file under, without touching the disk",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.job_ids and not args.all:
        print("error: give one or more job ids, or --all")
        return 2

    settings = get_settings()
    updates: dict[str, object] = {"archive_enabled": True}
    if args.archive_root:
        updates["archive_root"] = args.archive_root
    if args.link_mode:
        updates["archive_link_mode"] = args.link_mode
    # Settings is a frozen dataclass; dataclasses.replace gives us the overridden copy.
    from dataclasses import replace

    settings = replace(settings, **updates)  # type: ignore[arg-type]

    root = archive.archive_root(settings)
    if root is None:  # pragma: no cover - archive_enabled is forced True above
        print("error: no archive root resolved")
        return 1

    store = JobStore(args.jobs_root or settings.jobs_root)
    if args.all:
        jobs: list[Job] = store.list_jobs()
    else:
        jobs = []
        for job_id in args.job_ids:
            try:
                jobs.append(store.load(job_id))
            except FileNotFoundError:
                print(f"  [skip] {job_id}: no such job")
    if not jobs:
        print("no jobs to archive")
        return 0

    print(f"Archive root: {root}")
    failures = 0
    for job in jobs:
        day, instructor, customer = archive.jump_dir_parts(job)
        if args.dry_run:
            print(f"  [dry-run] {job.job_id} -> {day}/{instructor}/{customer}")
            continue
        jump_dir = archive.archive_raw_footage(job, store, settings)
        # Only jobs that got far enough to render have deliverables; the raw pass above
        # still files the footage for a queued or failed job.
        archive.archive_deliverables(job, store, settings)
        if job.delivery_links:
            archive.archive_delivery(job, settings)
        if jump_dir is None:
            failures += 1
            print(f"  [fail] {job.job_id}: see the warning above")
        else:
            print(f"  [ok]   {job.job_id} -> {jump_dir.relative_to(root)}")

    if failures:
        print(f"{len(jobs) - failures}/{len(jobs)} archived; {failures} failed.")
        return 1
    verb = "would be archived" if args.dry_run else "archived"
    print(f"{len(jobs)} job(s) {verb}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
