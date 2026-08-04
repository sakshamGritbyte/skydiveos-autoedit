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
(:mod:`ingest.qr`) or, failing that, the same serial-based match as wireless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .camera import Camera, CameraError, RemoteMedia

logger = logging.getLogger(__name__)

#: Where removable media typically mounts: Linux desktop (``/media/<user>/<vol>``
#: or ``/media/<vol>``), systemd/udisks (``/run/media/<user>/<vol>``), macOS
#: (``/Volumes/<vol>``). Overridable with ``SDCARD_MOUNT_ROOTS``.
DEFAULT_MOUNT_ROOTS: tuple[str, ...] = ("/media", "/run/media", "/Volumes")

#: GoPro writes card/camera info here; the JSON carries "camera serial number".
_VERSION_TXT = Path("MISC") / "version.txt"

#: Fallback for firmware whose version.txt is not strictly valid JSON.
_SERIAL_RE = re.compile(r'"camera serial number"\s*:\s*"([^"]+)"')

#: GoPro recordings: GX/GH prefix + numeric tail, under DCIM/<folder>/.
_GOPRO_MP4 = re.compile(r"^G[HX]\d+\.MP4$", re.IGNORECASE)


@dataclass(frozen=True)
class SdCard:
    """One mounted GoPro SD card: its derived camera id and where it's mounted."""

    camera_id: str
    mount: Path


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
    """
    serial = _serial_from_version_txt(mount)
    if serial:
        digits = re.sub(r"\D", "", serial)
        if len(digits) >= 4:
            return digits[-4:]
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", mount.name).strip("-") or "card"
    camera_id = f"sd-{label}"
    logger.warning(
        "card at %s has no readable camera serial (MISC/version.txt); using "
        "label-derived id %s — identity is only as stable as the volume label",
        mount, camera_id,
    )
    return camera_id


def _mounts_with_dcim(roots: Sequence[str | Path]) -> list[Path]:
    """Mounted volumes containing ``DCIM/``, at ``<root>/<vol>`` or ``<root>/<user>/<vol>``."""
    found: list[Path] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        # <root>/<vol>/DCIM plus one extra level for /run/media/<user>/<vol>/DCIM.
        for pattern in ("*/DCIM", "*/*/DCIM"):
            for dcim in base.glob(pattern):
                if dcim.is_dir() and dcim.parent not in found:
                    found.append(dcim.parent)
    return sorted(found)


def find_cards(roots: Sequence[str | Path] = DEFAULT_MOUNT_ROOTS) -> list[SdCard]:
    """Every GoPro-shaped card currently mounted under ``roots``."""
    return [SdCard(camera_id=card_id_for_mount(m), mount=m) for m in _mounts_with_dcim(roots)]


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
