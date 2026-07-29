"""Verify a real GoPro is reachable and list its media — hardware smoke test.

For bringing up real hardware: confirms the Open GoPro SDK is installed and a camera
can be opened and its media listed — over **USB** or **WiFi** — using the *same*
:class:`~ingest.camera.Camera` classes the pull pipeline uses. Read-only: it lists,
never pulls or deletes.

    # USB (kiosk): plug the camera in, then (serial optional — mDNS auto-detects):
    python scripts/check_camera.py --usb [--camera <serial-last-3-digits>]

    # WiFi (wireless): pair once, then:
    python -m ingest.pull --camera <id> --pair
    python scripts/check_camera.py --wifi --camera <id>

Requires the hardware-only Open GoPro SDK:
    uv pip install ./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Make the repo root importable when run as `python scripts/check_camera.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.camera import Camera, CameraError, GoProCamera, WiredGoProCamera  # noqa: E402


async def _check(cam: Camera, transport: str) -> None:
    print(f"opening camera over {transport} …")
    async with cam:
        print("connected; listing media …")
        videos = await cam.list_videos()
        print(f"{len(videos)} video(s) on card:")
        for m in videos:
            lrv = "+LRV" if m.has_lrv else "    "
            size = f"{m.size / 1e6:,.0f} MB" if m.size else "size ?"
            # The camera's own wall clock — this is the time the footage must be
            # matched against a load's departureTime, so print it rather than make
            # the operator guess which day a clip belongs to.
            when = (
                datetime.fromtimestamp(m.created_epoch).strftime("%Y-%m-%d %H:%M:%S")
                if m.created_epoch else "no timestamp"
            )
            print(f"  {lrv}  {when}  {m.camera_path:<28} ({size})")
        stamped = [m.created_epoch for m in videos if m.created_epoch]
        if stamped:
            first = datetime.fromtimestamp(min(stamped)).strftime("%Y-%m-%d %H:%M")
            last = datetime.fromtimestamp(max(stamped)).strftime("%Y-%m-%d %H:%M")
            print(f"\ncard covers {first} .. {last} (camera clock)")
            print("a load must depart within [clip -150min, clip +30min] to match")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/check_camera.py",
        description="Open a real GoPro (USB or WiFi) and list its media — a hardware smoke test.",
    )
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--usb", action="store_true", help="connect over USB (wired/kiosk)")
    transport.add_argument("--wifi", action="store_true", help="connect over BLE + WiFi (wireless)")
    parser.add_argument(
        "--camera",
        default=None,
        help="camera id/serial (USB: last 3 digits, optional — mDNS auto-detects; WiFi: required)",
    )
    parser.add_argument(
        "--wifi-interface", default=None, help="host WiFi interface (wireless only)"
    )
    parser.add_argument(
        "--password", default=None, help="host sudo password for the SDK WiFi join"
    )
    args = parser.parse_args(argv)

    if args.usb:
        cam: Camera = WiredGoProCamera(args.camera)
        transport_name = "USB"
    else:
        if not args.camera:
            print("error: --wifi requires --camera <id>")
            return 1
        cam = GoProCamera(
            args.camera, wifi_interface=args.wifi_interface, sudo_password=args.password
        )
        transport_name = "WiFi"

    try:
        asyncio.run(_check(cam, transport_name))
    except CameraError as e:
        print(f"error: {e}")
        return 1

    print("OK: camera reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
