"""Camera discovery scanners — find GoPros that are reachable right now.

:class:`CameraDiscoveryService` needs a way to ask "which cameras are in range?"
without coupling to the BLE stack: scanning hardware is the only part that requires
the optional ``bleak``/``open_gopro`` dependency, and tests must run without it.

So the scan is an injectable seam, mirroring :class:`ingest.camera.Camera`:

* :class:`CameraScanner` — the abstract contract (``scan()`` → list of camera ids).
* :class:`BleCameraScanner` — the production default; a BLE advertisement scan that
  returns the trailing serial digits of every GoPro it sees (``bleak`` imported
  lazily, so importing this module never drags the BLE stack in).
* :class:`StaticCameraScanner` — a fixed list, for tests and dry runs.

A "camera id" here is the same trailing-serial-digit string the rest of /ingest
uses (the ``target`` passed to the Open GoPro SDK and the ``{camera_id}`` storage
segment) — e.g. ``"1234"`` for a camera advertising as ``"GoPro 1234"``.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from .camera import CameraError

logger = logging.getLogger(__name__)

#: GoPro cameras advertise a BLE name like "GoPro 1234" / "GoPro1234"; we key off
#: the trailing serial digits, which is exactly the id the SDK targets.
_GOPRO_NAME = re.compile(r"gopro\D*(\d+)\s*$", re.IGNORECASE)

#: mDNS service a USB-connected GoPro advertises (used by the kiosk/USB scanner).
GOPRO_USB_SERVICE = "_gopro-web._tcp.local."


def camera_id_from_name(advertised_name: str | None) -> str | None:
    """Extract a GoPro camera id from a BLE advertised name, or ``None`` if not a GoPro."""
    if not advertised_name:
        return None
    m = _GOPRO_NAME.search(advertised_name.strip())
    return m.group(1) if m else None


class CameraScanner(ABC):
    """Asks the environment which GoPro cameras are reachable right now."""

    @abstractmethod
    async def scan(self) -> list[str]:
        """Return the camera ids (trailing serial digits) currently in range."""


class StaticCameraScanner(CameraScanner):
    """A scanner that always reports a fixed set of ids (tests / dry runs)."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)

    async def scan(self) -> list[str]:
        return list(self._ids)


class BleCameraScanner(CameraScanner):
    """Production scanner: a BLE advertisement scan for nearby GoPros.

    Uses ``bleak`` (the BLE backend the Open GoPro SDK is built on) to enumerate
    advertising devices and keeps those whose name looks like a GoPro, mapping each
    to its camera id. ``bleak`` is imported lazily and, when missing, raises the same
    actionable :class:`~ingest.camera.CameraError` the rest of /ingest uses for the
    hardware-only dependency — so a host without the BLE stack fails clearly rather
    than at import time.
    """

    def __init__(self, *, timeout: float = 5.0) -> None:
        #: How long each BLE discovery sweep listens for advertisements.
        self._timeout = timeout

    async def scan(self) -> list[str]:
        try:
            from bleak import BleakScanner
        except ImportError as e:  # pragma: no cover - exercised only without the BLE stack
            raise CameraError(
                "BLE scanning needs 'bleak' (installed with the Open GoPro SDK). "
                "Install the hardware deps:\n"
                "  uv pip install ./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control"
            ) from e

        devices = await BleakScanner.discover(timeout=self._timeout)
        found: list[str] = []
        for device in devices:
            camera_id = camera_id_from_name(getattr(device, "name", None))
            if camera_id is not None and camera_id not in found:
                found.append(camera_id)
        return found


class UsbCameraScanner(CameraScanner):
    """The kiosk/USB scanner: detects a USB-connected GoPro via mDNS.

    A GoPro plugged in over USB exposes its HTTP API on a USB-ethernet interface and
    advertises the ``_gopro-web._tcp.local.`` mDNS service. Each scan queries mDNS and
    returns the connected camera's id. It finds **one camera per scan** (the typical
    kiosk: one camera plugged in at a time); a multi-port kiosk would extend this to a
    full service browse. The Open GoPro SDK's mDNS helper is imported lazily, raising a
    clear :class:`~ingest.camera.CameraError` when the hardware deps are absent.
    """

    def __init__(self, *, timeout: float = 5.0, service: str = GOPRO_USB_SERVICE) -> None:
        self._timeout = timeout
        self._service = service

    async def scan(self) -> list[str]:
        try:
            from open_gopro.network.wifi import mdns_scanner
        except ImportError as e:  # pragma: no cover - exercised only without the SDK
            raise CameraError(
                "USB scanning needs the Open GoPro SDK. Install the hardware deps:\n"
                "  uv pip install ./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control"
            ) from e

        try:
            response = await mdns_scanner.find_first_ip_addr(
                self._service, timeout=int(self._timeout)
            )
        except Exception:  # noqa: BLE001 - "no camera connected" is the common, non-fatal case
            return []
        # Service name looks like "GoPro 1234._gopro-web._tcp.local." → the camera id.
        camera_id = camera_id_from_name(str(response.name).split(".")[0])
        return [camera_id] if camera_id is not None else []
