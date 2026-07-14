"""Async wrapper over the Open GoPro Python SDK (vendored under ``vendor/``).

This is the only part of /ingest that touches the camera. It thinly wraps the
flow the SDK demos show (``vendor/.../open_gopro/demos``):

* construct :class:`WirelessGoPro` for a target serial, ``open()`` it — that
  performs the BLE connect *and* joins the camera's WiFi access point;
* ``http_command.get_media_list()`` to enumerate the SD card;
* ``http_command.download_file`` / ``get_thumbnail`` to pull each asset.

The SDK (``open_gopro``) is an **optional, hardware-only** dependency and is not
installed by default (it drags in ``bleak`` and only works against a real
camera). It is therefore imported lazily: the rest of /ingest — storage layout,
event emission, pull planning — imports and unit-tests cleanly without it, and
calling into the camera without it raises a clear, actionable :class:`CameraError`.

Tests inject a fake :class:`Camera` so the orchestration in :mod:`ingest.pull`
is exercised without hardware.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class CameraError(RuntimeError):
    """Raised when the camera can't be reached, the SDK is missing, or a
    command fails."""


def lrv_camera_path(mp4_camera_path: str) -> str:
    """Camera path of the LRV proxy matching an MP4.

    GoPro pairs every recording with a low-res proxy that shares the file number
    but uses the ``GL`` prefix and ``.LRV`` extension — e.g. ``GX010123.MP4`` ->
    ``GL010123.LRV`` (same for the ``GH`` AVC prefix). We always work on the LRV
    for analysis (CLAUDE.md), so deriving its path is load-bearing.
    """
    p = PurePosixPath(mp4_camera_path)
    stem = p.stem
    if len(stem) < 2:
        raise CameraError(f"unexpected GoPro filename (cannot derive LRV): {mp4_camera_path}")
    return str(p.with_name(f"GL{stem[2:]}.LRV"))


@dataclass(frozen=True)
class RemoteMedia:
    """One video file on the camera's SD card, as listed before download."""

    camera_path: str  #: full camera path, e.g. "100GOPRO/GX010123.MP4"
    created_epoch: float | None  #: creation time (seconds since epoch), if known
    size: int | None  #: file size in bytes, if known
    has_lrv: bool  #: whether a matching .LRV proxy is present on the card

    @property
    def filename(self) -> str:
        """Bare filename without the camera folder, e.g. ``GX010123.MP4``."""
        return PurePosixPath(self.camera_path).name

    @property
    def stem(self) -> str:
        """Filename without extension, e.g. ``GX010123``."""
        return PurePosixPath(self.camera_path).stem

    @property
    def lrv_camera_path(self) -> str:
        """Camera path of this video's matching LRV proxy."""
        return lrv_camera_path(self.camera_path)


class Camera(ABC):
    """Async, context-managed handle to a camera. Implemented for real by
    :class:`GoProCamera`; faked in tests to drive :mod:`ingest.pull` offline."""

    @abstractmethod
    async def open(self) -> None:
        """Connect (BLE pair + join WiFi AP)."""

    @abstractmethod
    async def close(self) -> None:
        """Tear the connection down."""

    async def __aenter__(self) -> Camera:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @abstractmethod
    async def list_videos(self) -> list[RemoteMedia]:
        """All MP4 recordings currently on the SD card."""

    @abstractmethod
    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        """Download a recording's full-res MP4 to ``dest``."""

    @abstractmethod
    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        """Download a recording's LRV proxy to ``dest``."""

    @abstractmethod
    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        """Download a recording's thumbnail to ``dest``."""


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _media_item_to_remote(item: Any) -> RemoteMedia:
    """Adapt an SDK ``MediaItem`` to our :class:`RemoteMedia`.

    ``MediaItem.filename`` already carries the camera folder (the SDK rewrites it
    to ``folder/file``). LRV presence is inferred from the ``glrv``/``ls`` fields,
    which are absent or non-positive when no proxy exists.
    """
    glrv = _to_int(getattr(item, "low_res_video_size", None))
    ls = _to_int(getattr(item, "lrv_file_size", None))
    has_lrv = (glrv is not None and glrv > 0) or (ls is not None and ls > 0)
    return RemoteMedia(
        camera_path=str(item.filename),
        created_epoch=_to_float(getattr(item, "creation_timestamp", None)),
        size=_to_int(getattr(item, "file_size", None)),
        has_lrv=has_lrv,
    )


def _load_sdk() -> Any:
    """Import and return the SDK's ``WirelessGoPro`` class, or raise CameraError."""
    try:
        from open_gopro import WirelessGoPro
    except ImportError as e:
        raise CameraError(_SDK_MISSING) from e
    return WirelessGoPro


def _load_wired_sdk() -> Any:
    """Import and return the SDK's ``WiredGoPro`` (USB) class, or raise CameraError."""
    try:
        from open_gopro import WiredGoPro
    except ImportError as e:
        raise CameraError(_SDK_MISSING) from e
    return WiredGoPro


#: Shared "install the SDK" message for the wireless and wired loaders.
_SDK_MISSING = (
    "The Open GoPro SDK ('open_gopro') is not installed. It is an optional, "
    "hardware-only dependency. Install it from the vendored copy:\n"
    "  uv pip install ./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control"
)


def _downloaded_path(resp: Any, dest: Path) -> Path:
    if not resp.ok:
        raise CameraError(f"download failed for {dest.name}: {resp}")
    return Path(resp.data) if resp.data else dest


class _SdkGoProCamera(Camera):
    """Shared base for SDK-backed cameras: list + download over the GoPro HTTP API.

    The wireless (BLE+WiFi) and wired (USB) transports differ only in how the SDK
    handle is created; once open, ``http_command`` (media list / download / thumbnail)
    is identical. Subclasses implement :meth:`_make_gopro` to build the concrete
    handle; everything else lives here.
    """

    def __init__(self) -> None:
        self._gopro: Any | None = None

    def _make_gopro(self) -> Any:
        """Build (but don't open) the concrete SDK handle. Implemented per transport."""
        raise NotImplementedError

    async def open(self) -> None:
        self._gopro = self._make_gopro()
        await self._gopro.open()

    async def close(self) -> None:
        if self._gopro is not None:
            await self._gopro.close()
            self._gopro = None

    def _require_open(self) -> Any:
        if self._gopro is None:
            raise CameraError("camera not open; use 'async with <Camera>(...) as cam:'")
        return self._gopro

    async def list_videos(self) -> list[RemoteMedia]:
        gopro = self._require_open()
        resp = await gopro.http_command.get_media_list()
        if not resp.ok:
            raise CameraError(f"get_media_list failed: {resp}")
        videos: list[RemoteMedia] = []
        for item in resp.data.files:
            if PurePosixPath(str(item.filename)).suffix.upper() == ".MP4":
                videos.append(_media_item_to_remote(item))
        return videos

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        gopro = self._require_open()
        resp = await gopro.http_command.download_file(
            camera_file=media.camera_path, local_file=dest
        )
        return _downloaded_path(resp, dest)

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        gopro = self._require_open()
        resp = await gopro.http_command.download_file(
            camera_file=media.lrv_camera_path, local_file=dest
        )
        return _downloaded_path(resp, dest)

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        gopro = self._require_open()
        resp = await gopro.http_command.get_thumbnail(
            camera_file=media.camera_path, local_file=dest
        )
        return _downloaded_path(resp, dest)


class GoProCamera(_SdkGoProCamera):
    """:class:`Camera` backed by a real GoPro over BLE + WiFi (the wireless pull)."""

    def __init__(
        self,
        camera_id: str | None = None,
        *,
        wifi_interface: str | None = None,
        sudo_password: str | None = None,
    ) -> None:
        super().__init__()
        self.camera_id = camera_id
        self._wifi_interface = wifi_interface
        self._sudo_password = sudo_password

    def _make_gopro(self) -> Any:
        sdk = _load_sdk()
        # Default interfaces are BLE + WIFI_AP, so open() both pairs over BLE and
        # joins the camera's WiFi access point in one step.
        return sdk(
            target=self.camera_id,
            host_wifi_interface=self._wifi_interface,
            host_sudo_password=self._sudo_password,
        )


class WiredGoProCamera(_SdkGoProCamera):
    """:class:`Camera` backed by a real GoPro over **USB** (the kiosk pull).

    Uses the SDK's :class:`~open_gopro.WiredGoPro`, which talks the same HTTP API over
    the camera's USB-ethernet interface — so all listing/downloading is inherited from
    :class:`_SdkGoProCamera`. ``camera_id`` is the (at least last 3 digits of the)
    serial; ``None`` lets the SDK pick the first GoPro it finds over USB via mDNS.
    """

    def __init__(self, camera_id: str | None = None) -> None:
        super().__init__()
        self.camera_id = camera_id

    def _make_gopro(self) -> Any:
        wired = _load_wired_sdk()
        return wired(serial=self.camera_id)


class LocalSampleCamera(Camera):
    """A no-hardware :class:`Camera` for dev/demo: "downloads" by copying a local file.

    Lets auto-discovery and the whole pull path be exercised end-to-end without a
    GoPro (used by the ``CAMERA_SCANNER=static`` simulation mode in :mod:`api.app`).
    It reports ``count`` synthetic recordings (a real card holds many clips) and, on
    download, copies a configured sample MP4 — reusing it as the LRV proxy — and writes
    a placeholder thumbnail, so the real storage layout, manifest, idempotency, and
    ``ready_for_processing`` event all run against actual files. Filenames are derived
    deterministically from ``filename`` (the first clip; later clips increment its
    numeric tail), so the derived job ids are stable and repeated pulls are idempotent
    (a re-pull is skipped, no duplicate job).
    """

    #: A minimal valid JPEG (SOI + EOI) for the placeholder thumbnail.
    _PLACEHOLDER_JPEG = b"\xff\xd8\xff\xd9"

    def __init__(
        self,
        sample_mp4: str | Path,
        *,
        filename: str = "GX010001.MP4",
        count: int = 1,
        created_epoch: float | None = None,
    ) -> None:
        self._sample = Path(sample_mp4)
        self._filename = filename
        self._count = max(1, count)
        self._created_epoch = created_epoch

    @staticmethod
    def _bump(filename: str, i: int) -> str:
        """``filename`` with its trailing number advanced by ``i`` (width preserved).

        ``GX010001.MP4`` + 3 → ``GX010004.MP4``. Mirrors GoPro's incrementing file
        numbers so simulated clips get distinct, stable names like the real card.
        """
        stem, dot, ext = filename.rpartition(".")
        stem = stem or filename
        cut = len(stem)
        while cut > 0 and stem[cut - 1].isdigit():
            cut -= 1
        prefix, digits = stem[:cut], stem[cut:]
        if not digits:
            return filename
        bumped = f"{prefix}{int(digits) + i:0{len(digits)}d}"
        return f"{bumped}{dot}{ext}" if dot else bumped

    async def open(self) -> None:
        if not self._sample.is_file():
            raise CameraError(
                f"LocalSampleCamera sample file not found: {self._sample} "
                "(set DISCOVERY_SAMPLE_MP4 to an existing MP4)"
            )

    async def close(self) -> None:
        return None

    async def list_videos(self) -> list[RemoteMedia]:
        size = self._sample.stat().st_size
        return [
            RemoteMedia(
                camera_path=f"100GOPRO/{self._bump(self._filename, i)}",
                created_epoch=self._created_epoch,
                size=size,
                has_lrv=True,
            )
            for i in range(self._count)
        ]

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        shutil.copyfile(self._sample, dest)
        return dest

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        # Reuse the sample as its own proxy — good enough for a no-hardware demo.
        shutil.copyfile(self._sample, dest)
        return dest

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        dest.write_bytes(self._PLACEHOLDER_JPEG)
        return dest


async def pair(
    camera_id: str | None = None,
    *,
    wifi_interface: str | None = None,
    sudo_password: str | None = None,
) -> None:
    """One-time BLE pairing/bonding for a camera.

    Opens a BLE-only connection (no WiFi) to establish the OS-level bond, then
    closes. Run this once per camera before relying on :func:`ingest.pull` for
    routine WiFi pulls. Raises :class:`CameraError` if the BLE link never comes up.
    """
    sdk = _load_sdk()
    gopro = sdk(
        target=camera_id,
        host_wifi_interface=wifi_interface,
        host_sudo_password=sudo_password,
        interfaces={sdk.Interface.BLE},
    )
    await gopro.open()
    try:
        if not gopro.is_ble_connected:
            raise CameraError(f"BLE pairing with {camera_id or 'first camera'} did not connect")
    finally:
        await gopro.close()
