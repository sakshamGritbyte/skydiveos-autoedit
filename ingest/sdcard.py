"""A physically inserted GoPro SD card as an ingest source.

The dropzone's newest hand-off skips the radios entirely: the card comes out of
the camera and into the ingest machine's reader. This module makes that mount
look like any other camera so the whole existing pull path — staging layout,
manifests, idempotency, ``ready_for_processing`` events, the retention sweep —
runs unchanged (see :class:`ingest.camera.LocalSampleCamera` for the pattern):

* :func:`find_cards` — enumerate mounted cards (volumes containing ``DCIM/``)
  under the configured mount roots; polled by
  :class:`ingest.scanner.SdCardScanner`.
* :func:`card_id_for_mount` — a stable ``camera_id`` for a card, preferably the
  camera serial GoPro writes to ``MISC/version.txt`` (last 4 digits, matching
  the id a BLE/USB pull of the same camera would use — one staging tree, one
  retention ledger, one S3 prefix per physical camera regardless of transport).
* :class:`SdCardCamera` — the :class:`~ingest.camera.Camera` over a mount:
  "downloads" are file copies, ``delete_media`` unlinks off the card, so
  ``DELETE_AFTER_TRANSFER`` frees inserted cards exactly like wireless pulls.

Who the footage belongs to is NOT decided here: a card knows only the camera
and the clock. Attribution comes from the filmed QR session marker
(:mod:`ingest.qr`), else from the CARD's own serial paired to an instructor in
SkydiveOS (``staffs.sdCards[].serial`` — an instructor may own several cards and
rotate them between cameras, so the card is the thing that identifies them,
not the camera it happened to sit in), else the same camera-serial match as
wireless.

Two serials, two meanings — never conflate them:

* ``camera_id`` is the CAMERA's serial (``MISC/version.txt``), the staging /
  retention / S3 identity of the footage. It follows the GoPro.
* ``card_serial`` (:func:`card_serial_for_mount`) is the CARD's identity. It
  follows the piece of plastic, and it is what SkydiveOS pairs to a person. It
  travels on the pull event as ``source.card_serial`` and on the raw-upload
  notify as ``sd_card_serial``; SkydiveOS resolves it, this side never does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .camera import Camera, CameraError, RemoteMedia

logger = logging.getLogger(__name__)

def _default_mount_roots() -> tuple[str, ...]:
    """Where removable media appears, per platform.

    POSIX mounts a card INSIDE a container directory — Linux desktop
    (``/media/<user>/<vol>``, ``/media/<vol>``), systemd/udisks
    (``/run/media/<user>/<vol>``), macOS (``/Volumes/<vol>``).

    Windows mounts it AS a drive (``E:\\DCIM``), and the letter a reader gets is not
    stable — the same card is ``E:`` today and ``F:`` after another device is plugged in.
    So the drive letters are probed rather than configured: asking an operator to keep
    ``SDCARD_MOUNT_ROOTS`` in step with whatever letter Windows handed out today would
    make "insert the card" a two-step job, which is the one thing this flow exists to
    avoid. ``A:``/``B:`` are skipped — they are the legacy floppy letters, and probing
    them can stall on hardware that still claims them.
    """
    if os.name == "nt":
        return tuple(f"{chr(letter)}:\\" for letter in range(ord("C"), ord("Z") + 1))
    return ("/media", "/run/media", "/Volumes")


#: Where removable media typically mounts. Platform-dependent (see
#: :func:`_default_mount_roots`); overridable with ``SDCARD_MOUNT_ROOTS``, which is split
#: on ``os.pathsep`` (``;`` on Windows, because a drive letter contains a colon).
DEFAULT_MOUNT_ROOTS: tuple[str, ...] = _default_mount_roots()

#: GoPro writes card/camera info here; the JSON carries "camera serial number".
_VERSION_TXT = Path("MISC") / "version.txt"

#: Fallback for firmware whose version.txt is not strictly valid JSON.
_SERIAL_RE = re.compile(r'"camera serial number"\s*:\s*"([^"]+)"')

#: GoPro recordings: GX/GH prefix + numeric tail, under DCIM/<folder>/.
_GOPRO_MP4 = re.compile(r"^G[HX]\d+\.MP4$", re.IGNORECASE)


@dataclass(frozen=True)
class SdCard:
    """One mounted GoPro SD card: its derived camera id and where it's mounted.

    ``card_serial`` is the CARD's own identity (:func:`card_serial_for_mount`),
    distinct from ``camera_id`` (the camera's). ``None`` when the host cannot
    read one — the pull still runs, attribution just has one less rung.
    """

    camera_id: str
    mount: Path
    card_serial: str | None = None


# --------------------------------------------------------------------------- #
# Card serial — the CARD's identity (SkydiveOS pairs it to an instructor)
# --------------------------------------------------------------------------- #

#: Cap on the serial probes that shell out (``diskutil``); a wedged tool must not
#: stall the scan tick, which would freeze the operator's card banner.
_SERIAL_PROBE_TIMEOUT_S = 5.0


def normalize_card_serial(raw: str | None) -> str | None:
    """Canonical form of a card serial: trimmed, UPPERCASE, no whitespace.

    This is the equality SkydiveOS pairs on (``utils/sdCardSerial.js`` applies the
    SAME rule to what an admin types), so a serial copied off the banner with a
    stray space or in lowercase still matches. ``None`` for nothing usable.
    """
    if not isinstance(raw, str):
        return None
    clean = re.sub(r"\s+", "", raw).upper()
    return clean or None


def _mount_device_linux(mount: Path) -> str | None:
    """The block device mounted at ``mount`` per ``/proc/self/mountinfo``."""
    try:
        text = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return None
    target = os.path.realpath(str(mount))
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[4] == "/":
            continue
        # The mount point is octal-escaped (a label with a space is "\040").
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        if os.path.realpath(point) != target:
            continue
        try:
            dash = fields.index("-")
        except ValueError:
            continue
        if dash + 2 < len(fields):
            return fields[dash + 2]
    return None


def _linux_card_serial(mount: Path) -> str | None:
    """Linux: the card's hardware serial when an SD-host reader exposes it, else the
    filesystem's volume serial.

    A built-in (MMC/SDHCI) reader surfaces the card's CID register in sysfs —
    ``/sys/class/block/mmcblk0p1/device/serial`` — which survives any reformat.
    A USB card reader presents the card as plain mass storage and hides the CID
    entirely, so the only per-card identity left is the FAT/exFAT volume serial
    (``/dev/disk/by-uuid`` → ``1234-ABCD``), which a reformat rewrites.
    """
    device = _mount_device_linux(mount)
    if not device or not device.startswith("/dev/"):
        return None
    name = Path(device).name
    sysfs = Path("/sys/class/block") / name / "device" / "serial"
    if name.startswith("mmcblk"):
        try:
            raw = sysfs.read_text().strip()
            if raw:
                return f"MMC-{raw.removeprefix('0x').removeprefix('0X').upper()}"
        except OSError:
            pass
    by_uuid = Path("/dev/disk/by-uuid")
    try:
        real_device = os.path.realpath(device)
        for link in by_uuid.iterdir():
            if os.path.realpath(str(link)) == real_device:
                return link.name
    except OSError:
        pass
    return None


def _windows_card_serial(mount: Path) -> str | None:
    """Windows: the volume serial (``GetVolumeInformationW``), rendered ``XXXX-XXXX``
    exactly as ``dir`` / ``vol`` print it, so an operator can read it off the box."""
    import ctypes  # noqa: PLC0415 - Windows-only path

    root = str(mount)
    if not root.endswith("\\"):
        root += "\\"
    serial = ctypes.c_uint32(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial), None, None, None, 0
    )
    if not ok or not serial.value:
        return None
    return f"{serial.value >> 16:04X}-{serial.value & 0xFFFF:04X}"


def _macos_card_serial(mount: Path) -> str | None:
    """macOS: the volume UUID from ``diskutil info -plist`` (no CID access either)."""
    import plistlib  # noqa: PLC0415 - macOS-only path

    try:
        out = subprocess.run(
            ["diskutil", "info", "-plist", str(mount)],
            capture_output=True, timeout=_SERIAL_PROBE_TIMEOUT_S, check=False,
        )
        if out.returncode != 0 or not out.stdout:
            return None
        info = plistlib.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    value = info.get("VolumeUUID") or info.get("DiskUUID")
    return str(value) if value else None


def _read_card_serial(mount: Path) -> str | None:
    """Platform dispatch for the raw serial probe (module-level so tests can patch it)."""
    if os.name == "nt":
        return _windows_card_serial(mount)
    if sys.platform == "darwin":
        return _macos_card_serial(mount)
    return _linux_card_serial(mount)


def card_serial_for_mount(mount: Path) -> str | None:
    """The CARD's serial for a mounted card, normalized — or ``None``.

    Best effort by contract: every probe failure is a ``None`` (logged at debug),
    never an exception, because the serial is one rung of attribution and the
    pull must run without it. The hardware CID is preferred wherever the OS
    exposes it; the filesystem volume serial is the fallback everywhere else,
    and the operator-facing docs say so: a volume serial changes when the card
    is reformatted, so a reformatted card must be paired again. The banner and
    ``scripts/check_sdcard.py`` print whatever this returns, which is how an admin
    learns the value to pair.
    """
    try:
        return normalize_card_serial(_read_card_serial(Path(mount)))
    except Exception as e:  # noqa: BLE001 - identity is best-effort, the pull is not
        logger.debug("could not read a card serial for %s: %r", mount, e)
        return None


def _serial_from_version_txt(mount: Path) -> str | None:
    """The camera serial from ``<mount>/MISC/version.txt``, if readable."""
    path = mount / _VERSION_TXT
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    try:
        serial = json.loads(text).get("camera serial number")
        if isinstance(serial, str) and serial.strip():
            return serial.strip()
    except (ValueError, AttributeError):
        pass  # firmware format variance — fall through to the regex
    m = _SERIAL_RE.search(text)
    return m.group(1).strip() if m else None


def card_id_for_mount(mount: Path) -> str:
    """A stable ``camera_id`` for a mounted card.

    Prefers the last 4 digits of the camera serial in ``MISC/version.txt`` —
    the same id :func:`ingest.pull._normalize_camera_id` derives for a wireless
    pull, so both transports share one staging tree and retention ledger. Falls
    back to the sanitized volume label prefixed ``sd-`` (which can never collide
    with a 4-digit serial id); the label is only as stable as the card's name,
    so the fallback logs a warning.

    On Windows the label is not in the path at all — a card mounts as ``E:\\`` whose
    ``name`` is empty — so the drive letter stands in. Without that every unlabelled
    Windows card would derive the SAME id, and two cards sharing an id share a staging
    tree and a retention ledger: the exact collision that makes a filename an unsafe
    delete signal (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-F).
    """
    serial = _serial_from_version_txt(mount)
    if serial:
        digits = re.sub(r"\D", "", serial)
        if len(digits) >= 4:
            return digits[-4:]
    raw = mount.name or mount.drive.rstrip(":\\/") or "card"
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-") or "card"
    camera_id = f"sd-{label}"
    logger.warning(
        "card at %s has no readable camera serial (MISC/version.txt); using "
        "label-derived id %s — identity is only as stable as the volume label",
        mount, camera_id,
    )
    return camera_id


def _is_volume_root(path: Path) -> bool:
    """Whether this root IS a volume, rather than a directory holding several.

    A Windows card mounts as its own drive (``E:\\DCIM``); a POSIX card mounts inside a
    container (``/media/<user>/<vol>/DCIM``). A filesystem root is its own parent, which is
    exactly that distinction — and getting it wrong matters in one direction: searching a
    volume root one level down (``C:/*/DCIM``) would happily claim any stray ``DCIM``
    folder on the system drive as an inserted camera card.
    """
    return path.parent == path


def _safe_children(base: Path) -> list[Path]:
    """``base``'s subdirectories, skipping anything the OS refuses to stat.

    A card yanked without ejecting can leave a ZOMBIE mountpoint behind (stat
    returns EIO until the OS gives up on it); a dead network volume does the
    same. ``Path.is_dir()`` swallows those per entry, and the listing itself is
    guarded so one unreadable container never aborts the scan — a raising scan
    is what freezes the card-status registry (``observe`` stops running) while
    the publisher keeps rebroadcasting its last snapshot.
    """
    try:
        return [p for p in base.iterdir() if p.is_dir()]
    except OSError as e:
        logger.warning("cannot list %s during card scan (%r) — skipped this tick", base, e)
        return []


def _mounts_with_dcim(roots: Sequence[str | Path]) -> list[Path]:
    """Mounted volumes containing ``DCIM/``, under each root at its platform's depth.

    Walked per entry rather than with one ``glob`` so a single zombie
    mountpoint (see :func:`_safe_children`) skips only itself: the other cards
    in the reader are still found, and — critically — the scan still *returns*,
    so a removed card's ``safe_to_remove`` banner is still dropped.
    """
    found: list[Path] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        # A volume root holds DCIM directly; a container root holds <vol>/DCIM, plus one
        # extra level for /run/media/<user>/<vol>/DCIM.
        if _is_volume_root(base):
            if (base / "DCIM").is_dir() and base not in found:
                found.append(base)
            continue
        for child in _safe_children(base):
            if (child / "DCIM").is_dir():
                if child not in found:
                    found.append(child)
                continue
            for grandchild in _safe_children(child):
                if (grandchild / "DCIM").is_dir() and grandchild not in found:
                    found.append(grandchild)
    return sorted(found)


def find_cards(roots: Sequence[str | Path] = DEFAULT_MOUNT_ROOTS) -> list[SdCard]:
    """Every GoPro-shaped card currently mounted under ``roots``."""
    return [
        SdCard(camera_id=card_id_for_mount(m), mount=m, card_serial=card_serial_for_mount(m))
        for m in _mounts_with_dcim(roots)
    ]


def mount_for(camera_id: str, roots: Sequence[str | Path] = DEFAULT_MOUNT_ROOTS) -> Path:
    """Re-resolve a card id to its current mount (it may move between scan and pull).

    Raises :class:`~ingest.camera.CameraError` when the card is no longer
    mounted — the SD equivalent of a camera wandering out of BLE range.
    """
    for card in find_cards(roots):
        if card.camera_id == camera_id:
            return card.mount
    raise CameraError(f"SD card {camera_id} is no longer mounted (roots: {list(roots)})")


class SdCardCamera(Camera):
    """A :class:`~ingest.camera.Camera` over a mounted SD card.

    ``list_videos`` returns clips sorted by creation time — load-bearing for the
    QR session flow: a session marker is recorded *before* the clips it governs,
    and staging in capture order means the marker's sidecar exists by the time
    those clips are attributed (:mod:`ingest.qr`).
    """

    def __init__(self, mount: Path) -> None:
        self._mount = Path(mount)

    def _resolve(self, camera_path: str) -> Path:
        return self._mount / "DCIM" / camera_path

    async def open(self) -> None:
        if not (self._mount / "DCIM").is_dir():
            raise CameraError(f"no DCIM folder at {self._mount} — not a camera card?")

    async def close(self) -> None:
        return None

    async def list_videos(self) -> list[RemoteMedia]:
        def _list() -> list[RemoteMedia]:
            found: list[RemoteMedia] = []
            for path in (self._mount / "DCIM").glob("*/*"):
                if not (path.is_file() and _GOPRO_MP4.match(path.name)):
                    continue
                stat = path.stat()
                lrv = path.with_name(f"GL{path.stem[2:]}.LRV")
                found.append(
                    RemoteMedia(
                        camera_path=f"{path.parent.name}/{path.name}",
                        created_epoch=stat.st_mtime,
                        size=stat.st_size,
                        has_lrv=lrv.is_file(),
                    )
                )
            found.sort(key=lambda m: (m.created_epoch or 0.0, m.camera_path))
            return found

        return await asyncio.to_thread(_list)

    async def _copy(self, src: Path, dest: Path, what: str) -> Path:
        if not src.is_file():
            raise CameraError(f"{what} not on card: {src}")
        await asyncio.to_thread(shutil.copyfile, src, dest)
        return dest

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        return await self._copy(self._resolve(media.camera_path), dest, "MP4")

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        return await self._copy(self._resolve(media.lrv_camera_path), dest, "LRV")

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        # GoPro writes a THM sibling next to each recording.
        thm = self._resolve(media.camera_path).with_suffix(".THM")
        return await self._copy(thm, dest, "thumbnail")

    async def delete_media(self, media: RemoteMedia) -> None:
        """Unlink one recording (+ proxy/thumbnail) off the card.

        Only ever called by the retention sweep for footage S3 has confirmed
        (:mod:`ingest.retention`); sidecars are best-effort, the MP4 is not.
        """
        mp4 = self._resolve(media.camera_path)
        try:
            await asyncio.to_thread(mp4.unlink)
        except FileNotFoundError:
            pass  # already gone — the sweep's goal state
        except OSError as e:
            raise CameraError(f"could not delete {mp4} from card: {e!r}") from e
        for sidecar in (self._resolve(media.lrv_camera_path), mp4.with_suffix(".THM")):
            try:
                await asyncio.to_thread(sidecar.unlink)
            except OSError:
                pass  # best-effort: a leftover proxy is disk noise, not lost footage
