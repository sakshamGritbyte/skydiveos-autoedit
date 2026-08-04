#!/usr/bin/env python3
"""Drive one jump end to end from a camera's footage, matched to TODAY's load.

The unattended discovery path (BLE scan → WiFi pull → SkydiveOS notify → job) can be
blocked by things that have nothing to do with the pipeline — a macOS Wi-Fi that won't
hold the GoPro AP, a headless service without Location Services, SkydiveOS not yet
wiring the raw-upload into a job. This script proves the WHOLE edit→deliver flow with
the REAL footage and the REAL booking, sidestepping all of that:

    footage on disk  →  ingest.match.FootageMatcher (serial + capture time → today's
    load → the jumper → customer + package + role)  →  POST /jobs + upload to the
    auto-edit API  →  worker edits/renders  →  AUTO_DELIVER emails the customer.

It is the SkydiveOS side done by hand: instead of SkydiveOS matching the footage and
creating the job, this resolves the match locally (against the shared DB) and drives
the same live API `demo_full_auto` does. So it delivers to *exactly the customer on the
load today* — you don't type the customer/email/package, the match supplies them.

Usage::

    # footage already staged on disk (SD card, a pulled dir, wherever)
    python scripts/demo_from_load.py --dir /Volumes/GoPro/DCIM/100GOPRO
    python scripts/demo_from_load.py /path/GX010568.MP4 /path/GX010569.MP4

    # Greg's camera is the default serial; override for another camera:
    python scripts/demo_from_load.py --serial C3504224544313 --dir <path>

    # drive a remote deployment (EC2) instead of localhost:
    python scripts/demo_from_load.py --api http://<ec2-ip>:8000 --dir <path>

The capture time is read from the earliest clip (the same true-UTC conversion discovery
uses); override with ``--at <DZ-local ISO>`` if a clip's tag is unreadable. It prints the
resolved match and asks for confirmation before creating the job (it WILL email the
customer on the load) — pass ``--yes`` to skip the prompt.

Needs ``MONGO_URL`` (the shared DB, for the match) and the auto-edit API reachable at
``--api`` with ``AUTO_DELIVER=1`` for the email to go out.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Greg's full GoPro serial (staffs.goproSerial); the match resolves it to whoever
#: flew that camera on the matched load, so this default is just a convenience.
_DEFAULT_SERIAL = "C3504224544313"


def _collect_footage(positional: list[str], directory: str | None) -> list[Path]:
    """The MP4 masters to run, from explicit paths or a directory (sorted by name)."""
    if directory:
        d = Path(directory)
        if not d.is_dir():
            raise SystemExit(f"error: --dir not a directory: {directory}")
        files = sorted(
            p for p in d.iterdir() if p.suffix.lower() == ".mp4" and p.is_file()
        )
        if not files:
            raise SystemExit(f"error: no .MP4 files in {directory}")
        return files
    if not positional:
        raise SystemExit("error: give footage files or --dir <folder>")
    resolved = [Path(p) for p in positional]
    missing = [str(p) for p in resolved if not p.is_file()]
    if missing:
        raise SystemExit(f"error: footage not found: {', '.join(missing)}")
    return sorted(resolved)


def _earliest_capture(files: list[Path], clock_tz: str | None) -> str | None:
    """The earliest clip's true-UTC capture instant (the jump start), or None."""
    from ingest.discovery import _probe_capture_time

    stamps = [t for t in (_probe_capture_time(str(p), clock_tz=clock_tz) for p in files) if t]
    return min(stamps) if stamps else None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python scripts/demo_from_load.py",
        description="Match a camera's footage to today's load and run the full flow.",
    )
    p.add_argument("footage", nargs="*", help="raw GoPro MP4s (or use --dir)")
    p.add_argument("--dir", default=None, help="folder of MP4s (e.g. an SD card's 100GOPRO)")
    p.add_argument("--serial", default=_DEFAULT_SERIAL, help="camera serial (staffs.goproSerial)")
    p.add_argument("--api", default="http://localhost:8000", help="auto-edit API base URL")
    p.add_argument(
        "--at", default=None,
        help="override capture time (DZ-local ISO, e.g. 2026-07-29T04:20) if tags unreadable",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from api.config import get_settings
    from ingest.match import FootageMatcher, FootageMatchError

    settings = get_settings()
    files = _collect_footage(args.footage, args.dir)
    print(f"footage: {len(files)} clip(s) — {', '.join(p.name for p in files)}")

    captured_at: str = args.at or _earliest_capture(files, settings.camera_clock_tz) or ""
    if not captured_at:
        raise SystemExit(
            "error: could not read a capture time from the footage; pass --at "
            "<DZ-local ISO> (e.g. --at 2026-07-29T04:20)"
        )

    matcher = FootageMatcher(clock_tz=settings.camera_clock_tz)
    if not matcher.enabled:
        raise SystemExit("error: MONGO_URL not set — the match needs the shared DB")
    try:
        m = matcher.resolve(args.serial, captured_at)
    except FootageMatchError as e:
        raise SystemExit(
            f"error: no confident match for camera {args.serial} at {captured_at}: "
            f"{type(e).__name__}: {e}\n"
            "  → check today's load has this staff + a media add-on + a customer email "
            "(scripts/check_match.py --day <date>)."
        ) from e
    finally:
        matcher.close()

    print("\n── Matched to today's load ─────────────────────────")
    print(f"  capture time : {captured_at}")
    print(f"  camera owner : {m.staff_name}  (role: {m.role})")
    print(f"  load         : #{m.load_number}  ({m.load_id})")
    print(f"  customer     : {m.customer_name}  <{m.customer_email}>")
    print(f"  media/video  : {m.media_package} / {m.video_type}")
    print(f"  PACKAGE      : {m.package}")
    print(f"  ENTITLEMENT  : {m.entitlement}")
    if m.entitlement == "preview_only":
        print("  (nothing purchased — Path B: watermarked preview behind the unlock paywall)")

    if not m.package:
        raise SystemExit(
            "\nerror: this jumper's media add-on could not be mapped to a package "
            f"(mediaPackage={m.media_package!r}, videoType={m.video_type!r})."
        )
    if not m.customer_email:
        raise SystemExit(
            "\nerror: the matched customer has no email — the gallery can't be delivered."
        )

    if not args.yes:
        print(
            f"\nThis will create a REAL job and (with AUTO_DELIVER) email the gallery to "
            f"{m.customer_email}."
        )
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 1

    # Hand off to the tested live-stack driver with the MATCH-derived booking so the
    # customer/package/email come from the load, not typed by hand.
    import demo_full_auto  # type: ignore[import-not-found]  # sibling script, not a package

    driver_argv = [
        "--api", args.api,
        "--package", m.package,
        "--entitlement", m.entitlement,
        "--customer", m.customer_name or "Valued Skydiver",
        "--email", m.customer_email,
        *(["--jump-date", captured_at[:10]] if len(captured_at) >= 10 else []),
        *(["--instructor", m.staff_name] if m.staff_name else []),
        *(["--booking-id", m.booking_id] if m.booking_id else []),
        *[str(p) for p in files],
    ]
    print("\n── Driving the live auto-edit stack ────────────────")
    return demo_full_auto.main(driver_argv)


if __name__ == "__main__":
    raise SystemExit(main())
