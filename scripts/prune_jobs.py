"""Reclaim server disk from delivered jobs — the EC2-side retention sweep.

The pipeline's durable copies live in S3 (raw masters under ``raw/…`` at ingest,
rendered deliverables under ``deliveries/{job_id}/…`` at delivery), so the local
``jobs/`` and ``raw-storage/`` trees are working copies that only grow. At real
dropzone volume (~5 jumps/day ≈ 25–35 GB/day) an unattended box fills its disk in
weeks. This script prunes in tiers, under the card-retention philosophy: **delete
only what S3 has confirmed, per file, never failing anything** — a file it cannot
verify is a file it keeps.

Tiers (each with its own age gate, measured from the job's last update):

1. ``jobs/<id>/raw/``          — delivered jobs, default 2 days. The masters were
   uploaded to ``raw/{camera_id}/{filename}`` at ingest; each file is deleted only
   after a HeadObject confirms that key with a matching size. Biggest disk win.
2. renders + previews          — delivered jobs, default 7 days. Each ``<name>.mp4``
   in ``Job.outputs`` is deleted only after ``deliveries/{job_id}/{name}.mp4`` is
   confirmed; the gallery then serves via the presigned-redirect fallback in
   ``api.app.public_media``. A still-locked (``preview_only``) job's watermarked
   previews are NEVER pruned — they are local-only and are the paywall product.
   Photos are never pruned: the gallery grid serves individual stills locally.
   A **spec-flight load master** whose files back somebody else's gallery keeps both
   its renders and its previews for the same reason, one hop removed: the locked child
   galleries stream the master's local previews, and nothing else can serve them.
3. ``raw-storage/`` archive + ``_camera-staging/`` — date-named day folders older
   than their age gates (defaults 7 days). The archive's long-term home is the
   dropzone machine (``deploy/mac/sync-archive.sh`` pulls it down); staging days
   are dead weight once their files are ledgered and uploaded. The per-camera
   retention ledger (``.transferred.json``) is always kept.

Run from cron/systemd with the repo's env loaded, e.g.::

    cd /opt/skydiveos-autoedit && set -a && . ./.env && set +a && \
        .venv/bin/python scripts/prune_jobs.py

``--dry-run`` prints every decision and deletes nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import get_settings  # noqa: E402
from api.jobs import (  # noqa: E402
    Entitlement,
    Job,
    JobStatus,
    JobStore,
    locked_deliverables,
)

logger = logging.getLogger("prune_jobs")

_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _s3_confirms(client: Any, bucket: str, key: str, local: Path) -> bool:
    """True only when S3 holds ``key`` with exactly the local file's size."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        return int(head.get("ContentLength", -1)) == local.stat().st_size
    except Exception:  # noqa: BLE001 - any doubt means "keep the file"
        return False


def _age_days(job: Job) -> float:
    return (time.time() - (job.updated_at or job.created_at or time.time())) / 86400.0


def _delete(path: Path, *, dry_run: bool, why: str) -> int:
    """Remove one file (or tree) and return the bytes reclaimed."""
    size = (
        sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        if path.is_dir() else path.stat().st_size
    )
    if dry_run:
        logger.info("[dry-run] would delete %s (%.1f MB) — %s", path, size / 1e6, why)
        return size
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    logger.info("deleted %s (%.1f MB) — %s", path, size / 1e6, why)
    return size


def prune_job_raw(
    store: JobStore, job: Job, client: Any, bucket: str, *, dry_run: bool
) -> int:
    """Tier 1: the staged camera masters of one delivered job."""
    raw_dir = store.dir(job.job_id) / "raw"
    if not raw_dir.is_dir():
        return 0
    if not job.raw_s3_keys and not job.camera_id:
        logger.info("job %s: no recorded raw keys and no camera_id — keeping raw/", job.job_id)
        return 0
    freed = 0
    for f in sorted(raw_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() != ".mp4":
            continue  # proxies/sidecars ride along only once every master is gone
        # Prefer the key ingest recorded (exact, works for any camera naming and any key
        # layout). The derived `raw/{camera}/{name}` form is a LEGACY fallback for jobs
        # that predate `raw_s3_keys`: keys are now day-scoped
        # (`raw/{camera}/{date}/{name}`, see ingest.discovery.raw_object_key), so this
        # guess no longer matches a current key — and `_s3_confirms` requires a
        # size-matched HeadObject, so a guess that misses simply keeps the file.
        key = job.raw_s3_keys.get(f.name) or f"raw/{job.camera_id}/{f.name}"
        if _s3_confirms(client, bucket, key, f):
            freed += _delete(f, dry_run=dry_run, why=f"safe: s3://{bucket}/{key}")
        else:
            logger.warning("job %s: S3 does not confirm %s — keeping %s", job.job_id, key, f.name)
    # Clear leftover proxies/empty role dirs only when no master remains.
    if not dry_run and not any(p.suffix.lower() == ".mp4" for p in raw_dir.rglob("*")):
        for leftover in sorted(raw_dir.rglob("*"), reverse=True):
            if leftover.is_file():
                freed += _delete(leftover, dry_run=dry_run, why="raw sidecar, masters pruned")
            else:
                leftover.rmdir()
    return freed


def prune_job_renders(
    store: JobStore,
    job: Job,
    client: Any,
    bucket: str,
    *,
    dry_run: bool,
    pointers: dict[str, list[Job]] | None = None,
) -> int:
    """Tier 2: rendered deliverables (and, once unlocked, the watermarked previews).

    ``pointers`` maps a load master's job id → the jobs whose galleries stream its files
    (child galleries for the load's no-media customers, plus any media buyer who bought
    the load video). A master with **any** pointer keeps both its renders and its
    previews, untouched.

    Why so blunt: a locked child's only watchable media is the master's *local*
    ``preview_*.mp4``, and ``api.app.public_media`` deliberately refuses the presigned-S3
    fallback for a locked job (a presigned master URL is the paywall bypass). Deleting
    them would black out a live paywall — a customer clicks their link and gets nothing to
    watch, with no way to buy. Being conservative here costs disk on one job per spec
    flight; the alternative costs the sale and the trust. Consistent with this file's rule
    that a file it cannot verify is a file it keeps.
    """
    job_dir = store.dir(job.job_id)
    freed = 0
    dependents = (pointers or {}).get(job.job_id, [])
    if dependents:
        locked = sum(1 for d in dependents if d.entitlement is Entitlement.preview_only)
        logger.info(
            "job %s: load master with %d gallery(ies) streaming it (%d still locked) — "
            "keeping renders and previews",
            job.job_id, len(dependents), locked,
        )
        return 0
    still_locked = locked_deliverables(job)
    for name in job.outputs or {}:
        if name == "photos":
            continue  # stills serve locally forever; only the zip is in S3
        local = job_dir / f"{name}.mp4"
        if not local.is_file():
            continue
        key = f"deliveries/{job.job_id}/{name}.mp4"
        if _s3_confirms(client, bucket, key, local):
            freed += _delete(local, dry_run=dry_run, why=f"gallery falls back to s3://{bucket}/{key}")
        else:
            logger.warning("job %s: S3 does not confirm %s — keeping render", job.job_id, key)
    if still_locked:
        # A locked deliverable's ONLY watchable media is its local preview. Never prune
        # while anything is locked — on a mixed job that means the paid half's previews
        # (which nothing serves) survive too, and a few MB is the right price for not
        # reasoning about which preview belongs to which half.
        return freed
    for preview in sorted(job_dir.glob("preview_*.mp4")):
        freed += _delete(preview, dry_run=dry_run, why="job unlocked; previews are derivative")
    return freed


def prune_day_dirs(root: Path, *, keep_days: float, dry_run: bool, label: str) -> int:
    """Tier 3: date-named day folders (archive dates / staging days) past their age."""
    if not root.is_dir():
        return 0
    today = date.today()
    freed = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not _DAY_DIR.match(child.name):
            continue  # never touch ledgers, manifests, or anything not a day folder
        age = (today - date.fromisoformat(child.name)).days
        if age > keep_days:
            freed += _delete(child, dry_run=dry_run, why=f"{label} day {child.name} is {age}d old")
    return freed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/prune_jobs.py",
        description="Prune delivered jobs' local copies that S3 already holds.",
    )
    parser.add_argument("--dry-run", action="store_true", help="log decisions, delete nothing")
    parser.add_argument("--raw-days", type=float, default=2.0,
                        help="age before a delivered job's raw/ masters are pruned (default 2)")
    parser.add_argument("--renders-days", type=float, default=7.0,
                        help="age before a delivered job's renders are pruned (default 7 — "
                             "the gallery serves from S3 afterwards, so the link keeps working)")
    parser.add_argument("--archive-days", type=float, default=7.0,
                        help="age before jump-archive day folders are pruned (default 7)")
    parser.add_argument("--staging-days", type=float, default=7.0,
                        help="age before camera-staging day folders are pruned (default 7)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.s3_bucket:
        print("error: S3_BUCKET is not configured — nothing can be verified, so nothing "
              "will be pruned.", file=sys.stderr)
        return 2

    from api.delivery import _default_s3_client

    client = _default_s3_client(settings)
    store = JobStore(settings.jobs_root)
    resolved_jobs_root = store.dir("_probe").parent  # however the store resolved it
    raw_storage_root = Path(os.environ.get("RAW_STORAGE_ROOT") or "./raw-storage")
    freed = 0

    # One pass to learn which load masters are still being streamed by somebody else's
    # gallery, BEFORE anything is deleted — a master is pruned by its own id, but the
    # reason to keep it lives on other jobs (see prune_job_renders).
    jobs = []
    for job_id in sorted(p.name for p in resolved_jobs_root.iterdir() if p.is_dir()):
        if job_id.startswith("_"):
            continue
        try:
            jobs.append(store.load(job_id))
        except (FileNotFoundError, ValueError):
            continue  # not a job dir (or corrupt) — a human's problem, not ours
    pointers: dict[str, list[Job]] = {}
    for job in jobs:
        if job.source_job_id:
            pointers.setdefault(job.source_job_id, []).append(job)

    for job in jobs:
        job_id = job.job_id
        try:
            if job.status is not JobStatus.delivered:
                continue  # only jobs the customer already has are prunable
            age = _age_days(job)
            if age > args.raw_days:
                freed += prune_job_raw(store, job, client, settings.s3_bucket, dry_run=args.dry_run)
            if age > args.renders_days:
                freed += prune_job_renders(
                    store, job, client, settings.s3_bucket,
                    dry_run=args.dry_run, pointers=pointers,
                )
        except Exception:  # noqa: BLE001 - one bad job must not stop the sweep
            logger.exception("pruning job %s failed; continuing", job_id)

    archive_root = Path(settings.archive_root) if settings.archive_root else raw_storage_root
    freed += prune_day_dirs(
        archive_root, keep_days=args.archive_days, dry_run=args.dry_run, label="archive"
    )
    staging = raw_storage_root / "_camera-staging"
    if staging.is_dir():
        for cam in sorted(staging.iterdir()):
            if cam.is_dir():
                freed += prune_day_dirs(
                    cam, keep_days=args.staging_days, dry_run=args.dry_run,
                    label=f"staging {cam.name}",
                )

    print(f"{'[dry-run] would reclaim' if args.dry_run else 'reclaimed'} {freed / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
