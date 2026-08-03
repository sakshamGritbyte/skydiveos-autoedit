#!/usr/bin/env python3
"""End-to-end smoke test with no camera, no cloud, no mail server.

Answers one question in about a minute: **is this checkout working?** It walks the
whole flow a real jump takes — ingest → job → edit → paywall → archive — using a local
sample MP4 in place of a GoPro, and prints a pass/fail line per stage.

What makes it different from the other drivers in this directory:

* ``demo_auto_deliver.py`` proves *delivery*, so it needs real S3 (and aiosmtpd for the
  mail sink). ``qa_all_packages.py`` drives the live HTTP API across every package.
  This one needs **nothing external** — no network, no ``MONGO_URL``, no ``S3_BUCKET``,
  no ``AUTO_EDIT_API_KEY`` — so it runs on a laptop on a plane, and it is the right
  thing to run when a camera or a cloud credential is what's broken.
* The ingest stage is the *real* pull path (:func:`ingest.pull.pull_camera` driving
  :class:`ingest.camera.LocalSampleCamera`), not a stub: the staging layout, the
  manifest, LRV handling and the re-pull idempotency check all execute. That's the
  stage you cannot reach when a GoPro won't bring up its WiFi.

Everything is written under a temporary root and deleted on exit unless ``--keep``.

Usage::

    python scripts/smoke_no_camera.py                    # the full sweep
    python scripts/smoke_no_camera.py --source my.MP4    # your own footage
    python scripts/smoke_no_camera.py --keep             # leave the artifacts to inspect
    python scripts/smoke_no_camera.py --skip-render       # ingest + gallery only (no ffmpeg)

Exit code is 0 only if every stage passed, so it works as a pre-commit or CI check.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# The project isn't installed as a package (`package = false` in pyproject.toml), so
# make the repo root importable however this is invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: Preference order for the stand-in "camera footage".
_SOURCE_CANDIDATES = (
    _REPO_ROOT / "templates" / "GL010652.mp4",
    _REPO_ROOT / "sample-data" / "discovery_sample.mp4",
)


@dataclass
class Report:
    """Collected stage results, printed as a matrix at the end."""

    rows: list[tuple[str, str, bool, str]] = field(default_factory=list)

    def add(self, stage: str, check: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((stage, check, ok, detail))
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {stage:<9} {check}" + (f"  — {detail}" if detail else ""))
        return ok

    @property
    def failed(self) -> list[tuple[str, str, bool, str]]:
        return [r for r in self.rows if not r[2]]


def _pick_source(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"error: --source not found: {path}")
        return path
    for candidate in _SOURCE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "error: no sample footage found. Pass --source <file.mp4> (tried: "
        + ", ".join(str(c) for c in _SOURCE_CANDIDATES)
        + ")"
    )


def stage_ingest(report: Report, source: Path, storage_root: Path) -> Path | None:
    """The real pull path, with a local file standing in for the camera."""
    from ingest.camera import LocalSampleCamera
    from ingest.pull import pull_camera

    camera_id = "9362"
    camera = LocalSampleCamera(source, filename="GX010042.MP4", count=2)
    jumps = asyncio.run(
        pull_camera(camera_id, root=storage_root, camera=camera, emit=False)
    )

    report.add("ingest", "pull staged clips", len(jumps) == 2, f"{len(jumps)} jump(s)")
    staged = sorted(p for p in storage_root.rglob("*.MP4") if p.is_file())
    report.add("ingest", "MP4s on disk", len(staged) == 2, f"{len(staged)} file(s)")
    report.add(
        "ingest",
        "LRV proxy beside each master",
        len(list(storage_root.rglob("*.LRV"))) == 2,
    )
    # `<stem>.ingest.json`, written LAST in the pull sequence — its presence is the
    # "fully staged" marker `is_complete()` uses, which is what makes a re-pull skip.
    manifests = list(storage_root.rglob("*.ingest.json"))
    report.add("ingest", "per-jump manifest written", len(manifests) == 2,
               f"{len(manifests)} sidecar(s)")
    # A second pull must skip what's already staged — this is what stops a re-scan
    # duplicating a customer's jump.
    again = asyncio.run(
        pull_camera(camera_id, root=storage_root, camera=camera, emit=False)
    )
    report.add(
        "ingest",
        "re-pull is idempotent",
        all(j.skipped for j in again),
        f"{sum(j.skipped for j in again)}/{len(again)} skipped",
    )
    return staged[0] if staged else None


def stage_pipeline(report: Report, clip: Path, locked: bool) -> str | None:
    """Create a job, attach the footage, run the real edit inline."""
    from api.jobs import Entitlement, Job, JobStatus, JobStore
    from api.tasks import process_selfie_package

    store = JobStore()
    job_id = f"smoke-{'locked' if locked else 'paid'}"
    store.create(
        Job(
            job_id=job_id,
            customer_name="Sophie Lavoie",
            instructor_name="Marc Tremblay",
            jump_date="2026-08-14T14:35:00",
            entitlement=Entitlement.preview_only if locked else Entitlement.edited_download,
        )
    )
    raw_dir = store.raw_dir(job_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clip, raw_dir / clip.name)
    store.write_booking(job_id, {"customer_name": "Sophie Lavoie", "package": "selfie"})

    label = "locked" if locked else "paid"
    print(f"\n  running the edit ({label}) — this is real ffmpeg work, give it a minute")
    try:
        process_selfie_package(job_id)
    except Exception as e:  # noqa: BLE001 - the point is to report, not to raise
        report.add("edit", f"pipeline ran ({label})", False, repr(e)[:120])
        return None

    job = store.load(job_id)
    report.add(
        "edit", f"job reached ready ({label})", job.status == JobStatus.ready, job.status.value
    )
    jd = store.dir(job_id)
    report.add("edit", "scene manifest", any(jd.glob("scene_manifest*.json")))
    report.add("edit", "scores", any(jd.glob("scores*.json")))
    report.add("edit", "EDL per deliverable", len(list(jd.glob("edl_*.json"))) >= 1)
    report.add("edit", "validation report", (jd / "validation_report.json").is_file())
    videos = [n for n in (job.outputs or {}) if n != "photos"]
    report.add("edit", "renders present", bool(videos), ", ".join(videos) or "none")
    if locked:
        previews = list(jd.glob("preview_*.mp4"))
        report.add(
            "paywall",
            "watermarked previews rendered",
            len(previews) == len(videos),
            f"{len(previews)} preview(s)",
        )
    return job_id


def stage_gallery(report: Report, job_id: str, locked: bool) -> None:
    """Serve the customer page in-process and check the entitlement decides the bytes."""
    from fastapi.testclient import TestClient

    from api.app import create_app, get_store
    from api.jobs import JobStore
    from api.preview import preview_path

    store = JobStore()
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    token = store.ensure_gallery_token(job_id)
    label = "locked" if locked else "paid"

    with TestClient(app) as client:
        page = client.get(f"/j/{token}")
        report.add("gallery", f"page renders ({label})", page.status_code == 200)
        text = page.text
        if locked:
            report.add("gallery", "720P PREVIEW badge", "720P PREVIEW" in text)
            report.add("gallery", "unlock CTA", "Unlock full video" in text)
            report.add("gallery", "flip poll present", f"/j/{token}/state" in text)
        else:
            report.add("gallery", "1080P badge", "1080P · FULL QUALITY" in text)
            report.add("gallery", "download action", "Download video" in text)
        report.add("gallery", "no-referrer policy", 'content="no-referrer"' in text)
        report.add("gallery", "upsell row on both paths", "Add to your day" in text)

        job = store.load(job_id)
        name = next(iter(n for n in (job.outputs or {}) if n != "photos"), None)
        if name:
            served = client.get(f"/j/{token}/media/{name}")
            if locked:
                expected = preview_path(store.dir(job_id), name)
                report.add(
                    "paywall",
                    "locked page serves the PREVIEW, not the master",
                    served.status_code == 200
                    and expected.is_file()
                    and len(served.content) == expected.stat().st_size,
                )
                # The clean master must be unreachable at any URL while locked.
                report.add(
                    "paywall",
                    "state endpoint reports locked",
                    client.get(f"/j/{token}/state").json() == {"locked": True},
                )
                unlocked = client.post(
                    f"/jobs/{job_id}/unlock",
                    json={"payment_reference": "smoke-test"},
                )
                report.add("paywall", "unlock accepted", unlocked.status_code == 200)
                master = store.dir(job_id) / f"{name}.mp4"
                after = client.get(f"/j/{token}/media/{name}")
                report.add(
                    "paywall",
                    "after unlock the master is served",
                    len(after.content) == master.stat().st_size,
                )
                report.add(
                    "paywall",
                    "page flipped to unlocked",
                    "Unlock full video" not in client.get(f"/j/{token}").text,
                )
            else:
                report.add("gallery", "master streams", served.status_code == 200)
    app.dependency_overrides.clear()


def stage_archive(report: Report, job_id: str, *, locked: bool) -> None:
    """The browsable mirror: naming, the preview folder, and hash verification."""
    from api import archive
    from api.config import get_settings
    from api.jobs import JobStore

    settings = get_settings()
    store = JobStore()
    job = store.load(job_id)
    root = archive.archive_root(settings)
    if root is None:
        report.add("archive", "archiving enabled", False, "ARCHIVE_ENABLED is off")
        return

    archive.archive_raw_footage(job, store, settings)
    jump_dir = archive.archive_deliverables(job, store, settings)
    if jump_dir is None:
        report.add("archive", "jump folder created", False)
        return

    day, instructor, customer = archive.jump_dir_parts(job, settings)
    report.add("archive", "filed under the DZ-local day", day == "2026-08-14", day)
    report.add(
        "archive", "HH-MM prefix on the jump folder", customer.startswith("14-35_"), customer
    )
    report.add("archive", "raw mirrored", any((jump_dir / "raw").rglob("*.MP4")))
    report.add("archive", "renders mirrored", any((jump_dir / "edited").glob("*.mp4")))
    # Keyed off the RUN, not job.entitlement: the paywall stage has already unlocked
    # this job by now, so reading the entitlement here would silently skip the check.
    if locked:
        report.add("archive", "previews mirrored", any((jump_dir / "preview").glob("*.mp4")))

    mismatched, missing, checked = archive.verify_digests(jump_dir)
    report.add(
        "archive",
        "hashes verify clean",
        checked > 0 and not mismatched and not missing,
        f"{checked} file(s) hashed",
    )
    # Tamper with an archived file: verification MUST notice.
    victim = next(iter((jump_dir / "edited").glob("*.mp4")), None)
    if victim is not None:
        original = victim.read_bytes()
        try:
            victim.write_bytes(b"TAMPERED")
            bad, _, _ = archive.verify_digests(jump_dir)
            report.add(
                "archive", "tampering is detected", bool(bad), ", ".join(bad) or "not caught"
            )
        finally:
            victim.write_bytes(original)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None, help="sample MP4 to use as camera footage")
    parser.add_argument("--keep", action="store_true", help="don't delete the temp root")
    parser.add_argument(
        "--skip-render",
        action="store_true",
        help="ingest only — skip the ffmpeg edit, the gallery and the archive",
    )
    parser.add_argument(
        "--paid-only",
        action="store_true",
        help="only the Path A (edited_download) run, skipping the paywall",
    )
    args = parser.parse_args(argv)

    source = _pick_source(args.source)
    root = Path(tempfile.mkdtemp(prefix="smoke-no-camera-"))

    # Configure through the environment the pipeline already reads, so this exercises
    # the real settings path. Everything cloud-facing is deliberately left unset.
    os.environ.update(
        JOBS_ROOT=str(root / "jobs"),
        RAW_STORAGE_ROOT=str(root / "raw-storage"),
        ARCHIVE_ROOT=str(root / "raw-storage"),
        ARCHIVE_ENABLED="true",
        ARCHIVE_HASHES="true",
        CELERY_TASK_ALWAYS_EAGER="1",
        ENABLE_AUTO_DISCOVERY="0",
        AUTO_DELIVER="0",          # no delivery: that needs S3, which this test avoids
        PUBLIC_BASE_URL="http://127.0.0.1:8000",  # so a preview_only job may be created
        CAMERA_CLOCK_TZ=os.environ.get("CAMERA_CLOCK_TZ") or "America/Toronto",
        MONGO_URL="",
        SKYDIVEOS_API_BASE="",
        AUTO_EDIT_API_KEY="",
    )
    from api.config import get_settings

    get_settings.cache_clear()

    print(f"source : {source}")
    print(f"root   : {root}")
    print(f"tz     : {os.environ['CAMERA_CLOCK_TZ']}\n")

    report = Report()
    clip = stage_ingest(report, source, root / "raw-storage")

    if clip is None:
        print("\ningest produced no clip — stopping here")
    elif args.skip_render:
        print("\n--skip-render: stopping after ingest")
    else:
        runs = [False] if args.paid_only else [False, True]
        for locked in runs:
            job_id = stage_pipeline(report, clip, locked=locked)
            if job_id is None:
                continue
            stage_gallery(report, job_id, locked=locked)
            stage_archive(report, job_id, locked=locked)

    print()
    if report.failed:
        print(f"FAILED — {len(report.failed)} of {len(report.rows)} checks:")
        for stage, check, _, detail in report.failed:
            print(f"  {stage}: {check}" + (f" — {detail}" if detail else ""))
    else:
        print(f"PASSED — all {len(report.rows)} checks")

    if args.keep:
        print(f"\nartifacts kept: {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
