#!/usr/bin/env python3
"""Read-only check: what would the SD-card ingest do with the cards mounted right now?

The SD-card flow has two data links code can't fix — the card must identify itself
(``MISC/version.txt``) and the session must be claimed by a filmed QR marker — so
this probe shows exactly what discovery WOULD decide, without staging a byte,
writing a sidecar, or touching the card.

Usage::

    # Which cards does the scanner see, and what identity would each get?
    python scripts/check_sdcard.py

    # Also decode the QR markers and (with MONGO_URL) resolve each session's staff
    python scripts/check_sdcard.py --decode

Roots come from ``SDCARD_MOUNT_ROOTS`` (or the defaults); point ``--root`` at a
directory to probe a folder that isn't a real mount (e.g. a copied card).

Exit code is 0 when every card identified itself by serial and (with ``--decode``)
every clip resolved to a session, 1 otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fmt_epoch(epoch: float | None) -> str:
    if epoch is None:
        return "?"
    return dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--decode", action="store_true", help="decode QR markers + resolve sessions"
    )
    parser.add_argument(
        "--root", action="append",
        help="probe this directory instead of the configured mount roots (repeatable)",
    )
    args = parser.parse_args(argv)

    import asyncio

    from api.config import get_settings
    from ingest.discovery import _probe_capture_time  # noqa: PLC2701 - diagnostic tool
    from ingest.match import FootageMatcher, FootageMatchError
    from ingest.qr import _probe_video, decode_staff_qr  # noqa: PLC2701 - diagnostic tool
    from ingest.sdcard import SdCardCamera, _serial_from_version_txt, find_cards

    settings = get_settings()
    roots = args.root or list(settings.sdcard_mount_roots)
    cards = find_cards(roots)
    if not cards:
        print(f"no cards found (roots: {roots}) — is the card mounted and does it hold DCIM/?")
        return 1

    matcher = None
    if args.decode and settings.mongo_url:
        matcher = FootageMatcher(
            settings.mongo_url, db_name=settings.mongo_db, clock_tz=settings.camera_clock_tz
        )

    failed = False
    for card in cards:
        serial = _serial_from_version_txt(card.mount)
        rule = f"serial {serial} (MISC/version.txt)" if serial else "volume label FALLBACK"
        if not serial:
            failed = True
        print(f"\ncard {card.camera_id}  @ {card.mount}   identity: {rule}")

        videos = asyncio.run(SdCardCamera(card.mount).list_videos())
        if not videos:
            print("  (no GoPro MP4s on card)")
            continue

        session_staff: str | None = None
        for media in videos:
            line = f"  {media.filename:<16} {_fmt_epoch(media.created_epoch)}"
            if not args.decode:
                print(line)
                continue

            clip = card.mount / "DCIM" / media.camera_path
            duration, _w, _h = _probe_video(clip)
            staff_id = None
            if duration is None or duration <= settings.sdcard_qr_max_clip_seconds:
                staff_id = decode_staff_qr(clip, scan_seconds=settings.sdcard_qr_scan_seconds)
            if staff_id is not None:
                session_staff = staff_id
                print(f"{line}  QR MARKER -> staff {staff_id}")
                continue
            if session_staff is None:
                print(f"{line}  NO SESSION (no preceding QR marker; serial-based fallback)")
                failed = True
                continue
            verdict = f"session staff {session_staff}"
            if matcher is not None:
                # The container's creation_time (tz-corrected), exactly as production
                # matches — never the file mtime, which shifts with the copy.
                captured_at = _probe_capture_time(str(clip), clock_tz=settings.camera_clock_tz)
                if captured_at is None:
                    verdict += " -> NO capture time readable (creation_time missing)"
                    failed = True
                else:
                    try:
                        r = matcher.resolve_for_staff(session_staff, captured_at)
                        verdict += (
                            f" -> {r.customer_name!r} pkg={r.package} role={r.role}"
                            f" ({r.entitlement})"
                        )
                    except FootageMatchError as e:
                        verdict += f" -> LOAD MATCH FAILED: {type(e).__name__}: {e}"
                        failed = True
            print(f"{line}  {verdict}")

    if matcher is not None:
        matcher.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
