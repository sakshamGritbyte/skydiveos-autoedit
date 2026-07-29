#!/usr/bin/env python3
"""Drive one jump through the LIVE stack end to end, then show what the client sees.

This is the demo/rehearsal driver: it talks to the running API over HTTP exactly the
way SkydiveOS does — no test doubles, no in-process shortcuts — so what you show is the
real deployment:

    POST /jobs  (booking metadata incl. the instructor's name)
      -> POST /jobs/{id}/upload  (the raw GoPro masters; per camera_role for ultimum)
        -> the worker segments / scores / composes / renders
          -> AUTO_DELIVER auto-approves -> S3 + presigned gallery link -> customer email
            -> both raws AND renders mirrored into raw-storage/{date}/{instructor}/{customer}/

It polls the job, prints each status transition with a timestamp so you can narrate the
run, and finishes with three things worth putting on screen: the deliverables, the
customer's gallery link, and a tree of the jump's archive folder.

Usage (single-camera packages — selfie / external / video_only / photo_only)::

    python scripts/demo_full_auto.py --package external \\
      --customer "Marie Dupont" --instructor "Marc Tremblay" \\
      --email you@example.com \\
      /path/to/GX010982.MP4 /path/to/GX010983.MP4

Usage (the two-camera Ultimate product)::

    python scripts/demo_full_auto.py --package ultimum \\
      --customer "Marie Dupont" --instructor "Marc Tremblay" --email you@example.com \\
      --instructor-cam selfie1.MP4 selfie2.MP4 --external-cam outside1.MP4

Add ``--api http://<ec2-ip>:8000`` to drive a remote deployment. ``--no-wait`` returns
as soon as the footage is attached (for a "come back to it later" demo).

Exit code is 0 only if the job reached ``ready``/``delivered``; a failed job prints the
pipeline's error and exits 1, so this doubles as a pre-demo smoke test.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: Statuses that mean the pipeline is finished with this job, one way or another.
_DONE = {"ready", "ready_for_review", "delivered", "failed", "rejected"}
#: Package names that need per-camera uploads.
_TWO_CAMERA = {"ultimum"}


def _fmt(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _post_json(client: Any, url: str, payload: dict[str, object]) -> dict[str, Any]:
    resp = client.post(url, json=payload, timeout=30.0)
    if resp.status_code >= 400:
        raise SystemExit(f"error: POST {url} -> {resp.status_code} {resp.text}")
    return dict(resp.json())


def _upload(client: Any, api: str, job_id: str, paths: list[Path], role: str | None) -> None:
    """Attach one camera's masters. Streams from disk — a multi-GB master never buffers."""
    label = f" [{role}]" if role else ""
    total_mb = sum(p.stat().st_size for p in paths) / 1e6
    print(f"  uploading{label}: {', '.join(p.name for p in paths)}  ({total_mb:,.0f} MB)")
    handles = [p.open("rb") for p in paths]
    try:
        files = [("files", (p.name, fh, "video/mp4")) for p, fh in zip(paths, handles, strict=True)]
        data = {"camera_role": role} if role else None
        # No timeout: a 4K master over a slow link legitimately takes minutes.
        resp = client.post(
            f"{api}/jobs/{job_id}/upload", files=files, data=data, timeout=None
        )
    finally:
        for fh in handles:
            fh.close()
    if resp.status_code >= 400:
        raise SystemExit(f"error: upload -> {resp.status_code} {resp.text}")
    print(f"  → {resp.json()['detail']}")


def _existing(paths: list[str], what: str) -> list[Path]:
    resolved = [Path(p) for p in paths]
    missing = [str(p) for p in resolved if not p.is_file()]
    if missing:
        raise SystemExit(f"error: {what} not found: {', '.join(missing)}")
    return resolved


def _show_archive(job: dict[str, Any]) -> None:
    """Print the jump's folder in the browsable archive — the dropzone-facing view."""
    from api.archive import archive_root, jump_dir_parts
    from api.config import get_settings
    from api.jobs import Job

    root = archive_root(get_settings())
    if root is None:
        print("\n(jump archive is disabled — ARCHIVE_ENABLED=0)")
        return
    # Rebuild a Job from the API response so the folder is derived by the SAME code the
    # pipeline used, rather than re-implementing the naming here.
    day, instructor, customer = jump_dir_parts(
        Job(
            job_id=str(job["job_id"]),
            customer_name=str(job.get("customer_name") or ""),
            instructor_name=job.get("instructor_name"),
            instructor_id=job.get("instructor_id"),
            jump_date=job.get("jump_date"),
        )
    )
    base = root / day / instructor
    # The folder may carry a job-id suffix if a same-name jump already owned the plain
    # name, so find the one whose manifest claims this job.
    candidates = sorted(p for p in base.glob(f"{customer}*") if p.is_dir())
    print(f"\n── Jump archive: {base}/{customer} ──────────────")
    if not candidates:
        print("  (nothing archived — check the API/worker logs for an 'archive:' warning)")
        return
    for jump_dir in candidates:
        print(f"  {jump_dir.relative_to(root)}/")
        for sub in ("raw", "edited", "photos"):
            d = jump_dir / sub
            if not d.is_dir():
                continue
            files = sorted(p for p in d.rglob("*") if p.is_file())
            size_mb = sum(p.stat().st_size for p in files) / 1e6
            print(f"    {sub}/  {len(files)} file(s), {size_mb:,.0f} MB")
            for p in files[:6]:
                print(f"      {p.relative_to(d)}")
            if len(files) > 6:
                print(f"      … and {len(files) - 6} more")
        if (jump_dir / "manifest.json").is_file():
            print("    manifest.json")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/demo_full_auto.py",
        description="Run one jump through the live auto-edit stack and show the result.",
    )
    parser.add_argument("footage", nargs="*", help="raw GoPro MP4s (single-camera packages)")
    parser.add_argument("--api", default="http://localhost:8000", help="auto-edit API base URL")
    parser.add_argument(
        "--package",
        default="external",
        choices=["selfie", "external", "video_only", "photo_only", "ultimum"],
        help="product booked for this jump",
    )
    parser.add_argument("--customer", default="Demo Customer", help="customer name")
    parser.add_argument("--instructor", default=None, help="instructor name (names the folder)")
    parser.add_argument("--email", default=None, help="where the gallery link is emailed")
    parser.add_argument("--jump-date", default=None, help="ISO date of the jump (default: today)")
    parser.add_argument("--booking-id", default=None, help="SkydiveOS booking reference")
    parser.add_argument("--music", default=None, help="track stem from templates/music")
    parser.add_argument(
        "--instructor-cam", nargs="+", default=[], help="ultimum: selfie-cam masters"
    )
    parser.add_argument(
        "--external-cam", nargs="+", default=[], help="ultimum: cameraman masters"
    )
    parser.add_argument("--poll", type=float, default=10.0, help="seconds between status polls")
    parser.add_argument(
        "--timeout", type=float, default=3600.0, help="give up waiting after this many seconds"
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="attach the footage and exit (don't poll)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    api = args.api.rstrip("/")
    two_camera = args.package in _TWO_CAMERA

    if two_camera:
        if not (args.instructor_cam and args.external_cam):
            raise SystemExit(
                "error: --package ultimum needs BOTH --instructor-cam and --external-cam"
            )
        cameras: list[tuple[str | None, list[Path]]] = [
            ("instructor", _existing(args.instructor_cam, "instructor-cam footage")),
            ("external", _existing(args.external_cam, "external-cam footage")),
        ]
    else:
        if not args.footage:
            raise SystemExit("error: give one or more raw MP4s (or use --package ultimum)")
        cameras = [(None, _existing(args.footage, "footage"))]

    import httpx

    booking: dict[str, object] = {"customer_name": args.customer, "package": args.package}
    for key, value in (
        ("customer_email", args.email),
        ("instructor_name", args.instructor),
        ("jump_date", args.jump_date),
        ("booking_id", args.booking_id),
        ("music", args.music),
    ):
        if value:
            booking[key] = value
    if not args.instructor:
        print("note: no --instructor given; the archive folder will be '_no-instructor'")
    if not args.email:
        print("note: no --email given; delivery will only hand the links to SkydiveOS")

    started = time.monotonic()
    with httpx.Client() as client:
        created = _post_json(client, f"{api}/jobs", booking)
        job_id = str(created["job_id"])
        print(f"\n✓ job {job_id} created — package={args.package}, customer={args.customer!r}")

        for role, paths in cameras:
            _upload(client, api, job_id, paths, role)

        if args.no_wait:
            print(f"\nFootage attached. Poll it with: curl -s {api}/jobs/{job_id}")
            return 0

        print("\n── Pipeline ─────────────────────────────────────────")
        seen = ""
        job: dict[str, Any] = {}
        while True:
            elapsed = time.monotonic() - started
            resp = client.get(f"{api}/jobs/{job_id}", timeout=30.0)
            if resp.status_code >= 400:
                raise SystemExit(f"error: GET job -> {resp.status_code} {resp.text}")
            job = resp.json()
            status = str(job["status"])
            if status != seen:
                print(f"  [{_fmt(elapsed)}] {status}")
                seen = status
            if status in _DONE:
                break
            if elapsed > args.timeout:
                print(f"\n✗ still {status} after {_fmt(elapsed)} — giving up waiting.")
                print(f"   Check the worker log; the job itself keeps running: {api}/jobs/{job_id}")
                return 1
            time.sleep(args.poll)

    elapsed = time.monotonic() - started
    if job.get("status") in ("failed", "rejected"):
        print(f"\n✗ job {job['status']}: {job.get('error') or job.get('reject_reason')}")
        return 1

    print(f"\n✓ done in {_fmt(elapsed)} — status {job['status']}")
    outputs = job.get("outputs") or {}
    print(f"\nDeliverables ({len(outputs)}):")
    for name, path in outputs.items():
        print(f"  • {name}: {path}")

    links = job.get("delivery_links") or {}
    if links:
        gallery = links.pop("gallery", None) if isinstance(links, dict) else None
        if gallery:
            print("\nCustomer link (one page, all videos + photos) — open this on screen:")
            print(f"  {gallery}")
        if links:
            print("\nDirect file links (also forwarded to SkydiveOS):")
            for name in links:
                print(f"  • {name}")
    elif job.get("status") in ("ready", "ready_for_review"):
        print("\n(no delivery links — AUTO_DELIVER is off, so it's waiting on the review gate)")

    _show_archive(job)
    print(f"\nJob working dir: jobs/{job_id}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
