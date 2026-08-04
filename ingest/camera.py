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

import asyncio
import importlib
import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

#: Cap on the bluetoothctl bond query — a hung helper must not stall a pull.
_BLUETOOTHCTL_TIMEOUT_S = 10.0


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

    async def delete_media(self, media: RemoteMedia) -> None:
        """Delete one recording (and its proxy/thumbnail) from the card.

        Only ever called for footage S3 has already confirmed — see
        :mod:`ingest.retention`. Deleting a customer's only copy is unrecoverable, so
        this is per-file by design; nothing here wipes a card wholesale.

        Default implementation refuses, so a transport that cannot delete safely fails
        loudly instead of silently letting a card fill up.
        """
        raise CameraError(f"{type(self).__name__} does not support deleting media")


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


@contextmanager
def _as_camera_error(what: str) -> Iterator[None]:
    """Re-raise anything the SDK/transport throws as :class:`CameraError`.

    This module's contract is that camera trouble surfaces as ``CameraError`` — that is
    what callers catch, and :func:`ingest.pull._pull_one` relies on it to treat a
    missing proxy or thumbnail as best-effort. But the SDK reaches the camera over
    HTTP and lets ``requests`` exceptions through raw: a card whose thumbnail is
    missing answers ``404``, ``raise_for_status()`` throws ``requests.HTTPError``, that
    sails past ``except CameraError`` and kills the whole pull — nine staged clips
    thrown away over one absent JPEG (observed 2026-08-03 on ``GX015312.MP4``).

    So translate at the boundary that owns the SDK. ``CameraError`` subclasses are
    passed through unchanged; ``asyncio.CancelledError`` is never swallowed.
    """
    try:
        yield
    except CameraError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - the whole point is to normalise the type
        raise CameraError(f"{what} failed: {type(e).__name__}: {e}") from e


def _load_sdk() -> Any:
    """Import and return the SDK's ``WirelessGoPro`` class, or raise CameraError."""
    try:
        from open_gopro import WirelessGoPro
    except ImportError as e:
        raise CameraError(_SDK_MISSING) from e
    _skip_cohn_wait()
    _skip_redundant_ble_pairing()
    _bound_camera_ready_wait()
    _retry_wifi_ap_wait(WirelessGoPro)
    _forget_stale_nm_profile()
    return WirelessGoPro


def _skip_cohn_wait() -> None:
    """Stop the SDK's COHN handshake from failing an otherwise-healthy connection.

    ``WirelessGoPro.open()`` awaits ``CohnFeature.wait_until_ready()`` unconditionally —
    even for a BLE-only or BLE+WiFi session, which is all we ever ask for. When a camera
    never pushes a COHN status (observed on a HERO12 that has BLE connected and has
    already answered ``GET_THIRD_PARTY_API_VERSION``), that await burns 30 s and then
    raises, so pairing and every pull fail on a feature we do not use: COHN is GoPro's
    "camera on your home network" HTTP mode, and we reach the camera over its own WiFi
    AP or USB instead. Neutralise the wait; nothing downstream reads COHN state.
    """
    try:
        from open_gopro.features.cohn_feature import CohnFeature
    except ImportError:  # pragma: no cover - SDK layout differs across versions
        return

    if getattr(CohnFeature.wait_until_ready, "_skipped_by_autoedit", False):
        return

    async def _no_wait(self: Any) -> None:
        logger.debug("skipping the SDK's COHN readiness wait (COHN is unused here)")

    _no_wait._skipped_by_autoedit = True  # type: ignore[attr-defined]
    CohnFeature.wait_until_ready = _no_wait  # type: ignore[method-assign]


def _ble_bond_exists(address: str) -> bool:
    """Whether BlueZ already holds a bond for ``address``. Never raises.

    Asks ``bluetoothctl info`` rather than parsing a device *list*, because the answer
    for one address is a stable ``Paired: yes`` line — no prompt or delimiter to guess
    at (which is precisely where the SDK's own check goes wrong).
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", address],
            capture_output=True,
            text=True,
            timeout=_BLUETOOTHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # No bluetoothctl, or it hung: fall through to the SDK's own pairing attempt
        # rather than claiming a bond we can't see.
        logger.debug("could not query BlueZ for a bond with %s (%s)", address, e)
        return False
    return "Paired: yes" in result.stdout or "Bonded: yes" in result.stdout


def _skip_redundant_ble_pairing() -> None:
    """Stop the SDK re-pairing a camera BlueZ has already bonded.

    ``BleakWrapperController.pair()`` decides "am I already paired?" by parsing
    ``bluetoothctl devices Paired``: it waits for a ``#`` delimiter, then compares
    ``line.split()[1]`` against the address. Neither holds on bluetoothctl 5.85 — the
    prompt is ``[GoPro 9362]>`` (no ``#``) and every line is wrapped in ANSI colour
    codes — so the check never matches. The SDK then issues ``pair`` against an
    already-bonded camera, BlueZ answers ``ConnectionAttemptFailed``, and because the
    SDK only expects ``Accept pairing``/``Pairing successful`` it burns a 30 s timeout
    per retry and gives up.

    The effect is that a camera pairs **once** and can never be connected again: the
    first ``--pair`` works, every pull afterwards fails. Observed on a HERO with
    bluetoothctl 5.85; the dropzone's Mac ingest host hits the same wall on its second
    connect to any camera.

    So ask BlueZ directly (:func:`_ble_bond_exists`) and skip the pairing dance when a
    bond is already there, falling through to the SDK for a genuinely new camera.
    Same shape as :func:`_skip_cohn_wait`: patch the SDK's Linux assumption once, at
    import, and leave the rest of it alone.
    """
    try:
        from open_gopro.network.ble.adapters.bleak_wrapper import BleakWrapperController
    except ImportError:  # pragma: no cover - SDK layout differs across versions
        return

    if getattr(BleakWrapperController.pair, "_patched_by_autoedit", False):
        return

    original = BleakWrapperController.pair

    async def _pair_only_if_needed(self: Any, handle: Any) -> None:
        address = getattr(handle, "address", "") or ""
        if address and _ble_bond_exists(address):
            logger.info(
                "BLE bond with %s already exists — skipping the SDK's pair step", address
            )
            return
        await original(self, handle)

    _pair_only_if_needed._patched_by_autoedit = True  # type: ignore[attr-defined]
    BleakWrapperController.pair = _pair_only_if_needed


#: Default cap on "wait for the camera to report itself idle" (seconds). A camera that
#: is genuinely finishing an encode settles in a few seconds; a minute means something
#: is wrong on the device, not on the wire.
_READY_TIMEOUT_DEFAULT_S = 60.0


def _ready_timeout_s() -> float:
    """How long to wait for a camera to report itself idle. Never raises.

    Read per call (not at import) so an operator can raise ``GOPRO_READY_TIMEOUT_S``
    for a camera that legitimately needs longer, without a code change.
    """
    raw = os.environ.get("GOPRO_READY_TIMEOUT_S", "").strip()
    if not raw:
        return _READY_TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric GOPRO_READY_TIMEOUT_S=%r", raw)
        return _READY_TIMEOUT_DEFAULT_S
    return value if value > 0 else _READY_TIMEOUT_DEFAULT_S


#: Actionable text for a camera that never goes idle. The cause is almost always on the
#: device (QuikCapture starting a recording on a bumped shutter), so say so.
_READY_TIMEOUT_HELP = (
    "camera never reported itself idle within {timeout:g}s, so no media could be "
    "transferred. It is almost certainly still recording or finishing an encode: stop "
    "the recording, turn QuikCapture off (Preferences > General > QuikCapture), "
    "power-cycle the camera, and pull again. Set GOPRO_READY_TIMEOUT_S to wait longer."
)


def _bound_camera_ready_wait() -> None:
    """Put a deadline on the SDK's "wait for the camera to be ready" gate.

    ``WirelessGoPro.open()`` gates every session on the camera reporting
    ``BUSY: False`` + ``ENCODING: False``, by acquiring ``_ReadyLock``
    (``gopro_wireless.py``: seeded busy at ``open()``, acquired in ``_open_ble``,
    released only from ``_update_internal_state`` when a status notification says
    otherwise). The gate itself is *right* — pulling a file the camera is still writing
    yields a truncated master — but it has two defects that turn a busy camera into a
    process that never returns:

    * **The wait is unbounded.** ``await self._ready_lock.acquire(...)`` has no timeout,
      logs nothing further and raises nothing. Observed on a HERO12 whose QuikCapture
      kept starting recordings: the pull sat on that line for minutes with the BLE link
      up. Unattended (the dropzone Mac's discovery loop) it would park forever — no
      error, no retry, footage silently stops arriving.
    * **The lock leaks across ``open()``'s retries.** ``_ready_lock`` is built *once*,
      before the ``for retry in range(RETRIES)`` loop, but ``_open_ble`` acquires it on
      *every* attempt, and ``close()`` (which runs between attempts) cancels the status
      tasks without releasing it. So once attempt 1 has acquired the lock, attempt 2
      waits on a lock whose only releaser — a status notification — no longer has a
      subscriber. That is a hard deadlock, and it is reached by the ordinary path of a
      WiFi failure on the first attempt: the second attempt then hangs even against a
      camera that is perfectly idle.

    So bound both ways in, and reset a lock that a previous attempt leaked (a
    state-manager acquire finding the lock *already* owned by the state manager can only
    be a re-entry — :meth:`_update_internal_state` guards its own acquire on the owner
    being someone else). The result: a leaked lock no longer deadlocks, and a genuinely
    busy camera fails with an actionable :class:`CameraError` that the next scan retries.
    """
    try:
        from open_gopro.gopro_wireless import _ReadyLock
    except ImportError:  # pragma: no cover - SDK layout differs across versions
        return

    owners = getattr(_ReadyLock, "_LockOwner", None)
    state_manager = getattr(owners, "STATE_MANAGER", None)
    if state_manager is None:  # pragma: no cover - unknown SDK shape; leave it alone
        return

    if getattr(_ReadyLock.acquire, "_patched_by_autoedit", False):
        return

    original_acquire = _ReadyLock.acquire
    original_aenter = _ReadyLock.__aenter__

    def _reset_if_leaked(lock: Any, owner: Any) -> None:
        """Drop a lock a previous ``open()`` attempt acquired and never released."""
        if owner is state_manager and lock.owner is state_manager and lock.lock.locked():
            logger.warning(
                "the SDK's camera-ready lock was still held from an earlier connection "
                "attempt; releasing it so this attempt can wait on the camera's real state"
            )
            lock.release()

    async def _bounded_acquire(self: Any, owner: Any) -> None:
        _reset_if_leaked(self, owner)
        timeout = _ready_timeout_s()
        try:
            await asyncio.wait_for(original_acquire(self, owner), timeout)
        except TimeoutError:
            raise CameraError(_READY_TIMEOUT_HELP.format(timeout=timeout)) from None

    async def _bounded_aenter(self: Any) -> Any:
        timeout = _ready_timeout_s()
        try:
            return await asyncio.wait_for(original_aenter(self), timeout)
        except TimeoutError:
            raise CameraError(_READY_TIMEOUT_HELP.format(timeout=timeout)) from None

    _bounded_acquire._patched_by_autoedit = True  # type: ignore[attr-defined]
    _ReadyLock.acquire = _bounded_acquire
    _ReadyLock.__aenter__ = _bounded_aenter


#: Default deadline for the camera to raise its WiFi access point, in seconds. The SDK
#: hardcodes 5, which a HERO12 misses routinely — especially on the first attempt after
#: a previous session left the AP half-enabled.
_AP_READY_TIMEOUT_DEFAULT_S = 30.0


def _ap_ready_timeout_s() -> float:
    """How long to wait for the camera's WiFi AP to come up. Never raises.

    Read per call (not at import), same contract as :func:`_ready_timeout_s`, so an
    operator can raise ``GOPRO_AP_READY_TIMEOUT_S`` without a code change.
    """
    raw = os.environ.get("GOPRO_AP_READY_TIMEOUT_S", "").strip()
    if not raw:
        return _AP_READY_TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric GOPRO_AP_READY_TIMEOUT_S=%r", raw)
        return _AP_READY_TIMEOUT_DEFAULT_S
    return value if value > 0 else _AP_READY_TIMEOUT_DEFAULT_S


def _retry_wifi_ap_wait(wireless_cls: Any) -> None:
    """Make a slow WiFi access point retry (and reset) instead of failing the pull.

    ``WirelessGoPro._open_wifi()`` asks the camera to raise its AP, polls
    ``StatusId.AP_MODE`` until it reads True, then joins the AP — all inside a
    ``for retry in range(1, retries)`` loop whose ``except ConnectFailed`` branch
    re-disables the AP so the next attempt starts from a known state. That recovery is
    exactly right, and as shipped it is unreachable for the failure we actually hit:

    * **The AP-ready deadline is hardcoded at 5 s** —
      ``await asyncio.wait_for(_wait_for_camera_wifi_ready(), 5)``. A HERO12 routinely
      needs longer, so the wait expires while the camera is still bringing the AP up.
    * **It raises ``TimeoutError``, which the loop does not catch.** ``ConnectFailed`` is
      the only handled type, so the timeout propagates straight out of ``_open_wifi``:
      the remaining retries never run, and — the damaging half — the
      ``enable_wifi_ap(enable=False)`` reset never fires. The camera is left with its AP
      half-enabled, which is precisely the state that makes the *next* attempt time out
      too. Observed as a pull that fails identically run after run, looking like a dead
      camera, while ``AP_MODE`` polls ``False`` forever in the log.

    So reimplement the method with the deadline read from the environment and
    ``TimeoutError`` handled alongside ``ConnectFailed``. The retry/reset logic is the
    SDK's own, kept line-for-line; only the deadline and the caught type change. Same
    shape as the other corrections here (:func:`_skip_cohn_wait`,
    :func:`_skip_redundant_ble_pairing`, :func:`_bound_camera_ready_wait`): patch the
    assumption once at import, leave the rest of the SDK alone, and stay a no-op on an
    SDK whose shape we don't recognise.
    """
    original = getattr(wireless_cls, "_open_wifi", None)
    if original is None:  # pragma: no cover - unknown SDK shape; leave it alone
        return
    if getattr(original, "_patched_by_autoedit", False):
        return
    try:
        from open_gopro.domain.exceptions import ConnectFailed
    except ImportError:  # pragma: no cover - SDK layout differs across versions
        return

    async def _open_wifi(self: Any, timeout: int = 30, retries: int = 5) -> None:
        logger.info("Discovering Wifi AP info and enabling via BLE")
        password = (await self.ble_command.get_wifi_password()).data
        ssid = (await self.ble_command.get_wifi_ssid()).data
        ap_deadline = _ap_ready_timeout_s()
        for retry in range(1, retries):
            try:
                assert (await self.ble_command.enable_wifi_ap(enable=True)).ok

                async def _wait_for_camera_wifi_ready() -> None:
                    logger.info("waiting for the camera's WiFi AP (up to %gs)", ap_deadline)
                    while not (await self.ble_status.ap_mode.get_value()).data:
                        await asyncio.sleep(0.200)

                await asyncio.wait_for(_wait_for_camera_wifi_ready(), ap_deadline)
                await self._wifi.open(ssid, password, timeout, 1)
                break
            except (ConnectFailed, TimeoutError) as e:
                # The reset below is the point: an AP left half-enabled by this attempt
                # makes every later attempt time out the same way.
                logger.warning(
                    "WiFi connection attempt %d failed (%s); disabling the camera's AP "
                    "so the next attempt starts clean",
                    retry,
                    type(e).__name__,
                )
                with _suppress_ap_reset_errors():
                    assert (await self.ble_command.enable_wifi_ap(enable=False)).ok
        else:
            raise ConnectFailed("Wifi Connection failed", timeout, retries)

    # Keep the SDK's "BLE must already be open" guard on the replacement, so the only
    # differences from the original really are the deadline and the caught type.
    patched = _open_wifi
    try:
        from open_gopro.gopro_base import GoProBase, GoProMessageInterface

        patched = GoProBase._ensure_opened((GoProMessageInterface.BLE,))(_open_wifi)
    except (ImportError, AttributeError):  # pragma: no cover - unknown SDK shape
        logger.debug("SDK's _ensure_opened guard unavailable; patching _open_wifi without it")

    patched._patched_by_autoedit = True  # type: ignore[attr-defined]
    wireless_cls._open_wifi = patched


@contextmanager
def _suppress_ap_reset_errors() -> Iterator[None]:
    """Let the AP reset fail without masking the connection error that triggered it.

    The reset is best-effort recovery on a link that has just misbehaved; if the BLE
    command itself fails there is nothing further to do but try the next attempt.
    """
    try:
        yield
    except Exception as e:  # noqa: BLE001 - recovery must not replace the real failure
        logger.warning("could not disable the camera's WiFi AP between attempts: %s", e)


def _forget_stale_nm_profile() -> None:
    """Delete a stale NetworkManager profile for the camera's SSID before connecting.

    On Linux the SDK joins the camera's AP with ``nmcli dev wifi connect``. When a
    profile for that SSID already exists — left behind by any previous pull —
    NetworkManager *updates* it instead of creating a fresh one, and the update carries
    an incomplete security section, which NM rejects before ever touching the radio::

        Error: 802-11-wireless-security.key-mgmt: property is missing.

    So the first pull after boot works (create path) and every later one fails
    (update path) until an operator runs ``nmcli con delete GP<serial>`` by hand —
    observed repeatedly on a HERO12 (profile ``GP26489362``). The SDK's nmcli driver
    even ships the cleanup (``_clean(partial)``, which deletes every connection whose
    name contains the given string); it is just never called before ``connect``. Call
    it, so ``nmcli`` always takes the create path it gets right.

    Cross-platform by construction: only the two Linux nmcli driver classes are
    patched, and the SDK's driver *detection* — not this patch — decides which driver
    a host uses. macOS (``networksetup``) and Windows (``netsh``) never instantiate
    these classes, keep their own drivers untouched, and have no stored-profile bug of
    this shape to begin with. Same pattern as the other corrections here: patch once at
    import, no-op on an SDK whose shape we don't recognise, and never let the cleanup
    itself break a connect that might have succeeded anyway.
    """
    try:
        # import_module (not `from … import wireless`) so the module is resolved via
        # sys.modules — the same seam the tests stub, and the canonical instance even
        # if the parent package caches a different attribute.
        _sdk_wireless = importlib.import_module("open_gopro.network.wifi.adapters.wireless")
    except ImportError:  # pragma: no cover - SDK layout differs across versions
        return

    # Match the nmcli drivers by name prefix + shape rather than exact class names:
    # the SDK has renamed them before (NmcliWireless / Nmcli0990Wireless today), and a
    # rename must degrade to "patch skipped", never to patching the wrong driver.
    controllers: list[Any] = [
        cls
        for name in dir(_sdk_wireless)
        if name.startswith("Nmcli")
        and isinstance(cls := getattr(_sdk_wireless, name), type)
        and callable(getattr(cls, "connect", None))
        and callable(getattr(cls, "_clean", None))
    ]
    for controller in controllers:
        original = controller.connect
        if getattr(original, "_patched_by_autoedit", False):
            continue

        async def _connect_with_fresh_profile(
            self: Any,
            ssid: str,
            password: str,
            timeout: float = 15,
            *,
            _original: Any = original,
        ) -> Any:
            try:
                logger.info(
                    "removing any stale NetworkManager profile for %r before connecting",
                    ssid,
                )
                # _clean shells out to nmcli; keep it off the event loop, which is
                # also running the BLE keep-alive.
                await asyncio.to_thread(self._clean, ssid)
            except Exception as e:  # noqa: BLE001 - cleanup must never break the connect
                logger.warning("could not clear stale profile(s) for %r: %s", ssid, e)
            return await _original(self, ssid, password, timeout)

        _connect_with_fresh_profile._patched_by_autoedit = True  # type: ignore[attr-defined]
        controller.connect = _connect_with_fresh_profile


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


def _force_us_english_locale() -> None:
    """Make ``LANG`` start with ``en_US`` before the SDK's WiFi driver inspects it.

    The Open GoPro WiFi adapter parses ``networksetup`` CLI output and refuses to start
    unless ``os.environ["LANG"]`` begins with ``en_US`` — raising on a French Mac (the
    client dropzone) and ``KeyError``-ing under launchd, which sets no ``LANG`` at all.
    macOS CLI output is English regardless of UI language, so forcing it is safe.

    ``deploy/mac/run.sh`` already exports this for the *service*; doing it here covers
    every other entry point too (``python -m ingest.pull``, ``scripts/check_camera.py``),
    which otherwise fail on that Mac with an error that looks like a camera fault.
    """
    if not os.environ.get("LANG", "").startswith("en_US"):
        os.environ["LANG"] = "en_US.UTF-8"


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
        with _as_camera_error(f"download of {media.camera_path}"):
            resp = await gopro.http_command.download_file(
                camera_file=media.camera_path, local_file=dest
            )
        return _downloaded_path(resp, dest)

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        gopro = self._require_open()
        with _as_camera_error(f"LRV download of {media.lrv_camera_path}"):
            resp = await gopro.http_command.download_file(
                camera_file=media.lrv_camera_path, local_file=dest
            )
        return _downloaded_path(resp, dest)

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        gopro = self._require_open()
        with _as_camera_error(f"thumbnail of {media.camera_path}"):
            resp = await gopro.http_command.get_thumbnail(
                camera_file=media.camera_path, local_file=dest
            )
        return _downloaded_path(resp, dest)

    async def delete_media(self, media: RemoteMedia) -> None:
        """Delete this recording's MP4, then its LRV proxy, over the HTTP API.

        The MP4 is deleted first and its failure is fatal (the caller must not believe
        the card was freed). The proxy is best-effort: some cameras remove it with the
        master, and a stranded 30 MB LRV is not worth failing an otherwise-good delete.
        """
        gopro = self._require_open()
        resp = await gopro.http_command.delete_file(path=media.camera_path)
        if not resp.ok:
            raise CameraError(f"delete_file failed for {media.camera_path}: {resp}")
        try:
            await gopro.http_command.delete_file(path=media.lrv_camera_path)
        except Exception as e:  # noqa: BLE001 - proxy cleanup is best-effort
            logger.debug("LRV delete skipped for %s: %r", media.camera_path, e)


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
        _force_us_english_locale()
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
        #: Clips "deleted" from the simulated card — dropped from list_videos, so the
        #: retention sweep can be exercised without hardware.
        self._deleted: set[str] = set()

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
            if self._bump(self._filename, i) not in self._deleted
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

    async def delete_media(self, media: RemoteMedia) -> None:
        """Forget a synthetic clip, so the simulation exercises the cleanup path too.

        There is no card to free — the sample file on disk is left alone — but the clip
        stops being listed, exactly as a real delete behaves on the next pull.
        """
        self._deleted.add(media.filename)
        logger.info("simulated camera: dropped %s from the card", media.filename)


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
    _force_us_english_locale()
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
