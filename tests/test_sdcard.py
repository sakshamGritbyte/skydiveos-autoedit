"""Tests for the SD-card ingest source (:mod:`ingest.sdcard`).

No real card: a tmp directory shaped like a GoPro card (``DCIM/100GOPRO/`` +
``MISC/version.txt``) stands in for the mount. Async scenarios run with
:func:`asyncio.run`, matching :mod:`tests.test_ingest`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ingest.camera import CameraError, RemoteMedia
from ingest.pull import pull_camera
from ingest.retention import record_uploaded
from ingest.scanner import SdCardScanner
from ingest.sdcard import SdCardCamera, card_id_for_mount, find_cards, mount_for
from ingest.storage import camera_dir

# --------------------------------------------------------------------------- #
# Card fixtures
# --------------------------------------------------------------------------- #


def _make_card(
    root: Path,
    name: str = "GOPRO01",
    *,
    serial: str | None = "C3504224544313",
    clips: list[tuple[str, float]] | None = None,
) -> Path:
    """A tmp directory shaped like a GoPro SD card; returns the mount path."""
    mount = root / name
    folder = mount / "DCIM" / "100GOPRO"
    folder.mkdir(parents=True)
    if serial is not None:
        misc = mount / "MISC"
        misc.mkdir()
        (misc / "version.txt").write_text(
            json.dumps({"info version": "2.0", "camera serial number": serial})
        )
    for filename, mtime in clips or []:
        clip = folder / filename
        clip.write_bytes(b"\x00\x11\x22\x33" * 64)
        import os

        os.utime(clip, (mtime, mtime))
    return mount


# --------------------------------------------------------------------------- #
# Card identity
# --------------------------------------------------------------------------- #


def test_card_id_from_version_txt(tmp_path: Path) -> None:
    """The serial's last 4 digits — same id a wireless pull of that camera uses."""
    mount = _make_card(tmp_path, serial="C3504224544313")
    assert card_id_for_mount(mount) == "4313"


def test_card_id_regex_fallback_on_sloppy_json(tmp_path: Path) -> None:
    """Firmware writes not-quite-JSON; the serial is still recovered by regex."""
    mount = _make_card(tmp_path, serial=None)
    (mount / "MISC").mkdir()
    (mount / "MISC" / "version.txt").write_text(
        '{"info version":"2.0","camera serial number":"C3504224544313",}'  # trailing comma
    )
    assert card_id_for_mount(mount) == "4313"


def test_card_id_falls_back_to_volume_label(tmp_path: Path) -> None:
    """No readable serial → sanitized label with an sd- prefix (can't collide with serials)."""
    mount = _make_card(tmp_path, name="My Card!", serial=None)
    assert card_id_for_mount(mount) == "sd-My-Card"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_find_cards_detects_dcim_at_both_depths(tmp_path: Path) -> None:
    """<root>/<vol>/DCIM and <root>/<user>/<vol>/DCIM (the /run/media layout)."""
    _make_card(tmp_path, "CARD-A", serial="C0000000001111")
    nested_root = tmp_path / "user"
    nested_root.mkdir()
    _make_card(nested_root, "CARD-B", serial="C0000000002222")

    cards = find_cards([tmp_path])
    assert {c.camera_id for c in cards} == {"1111", "2222"}


def test_find_cards_ignores_non_dcim_volumes(tmp_path: Path) -> None:
    (tmp_path / "backup-drive" / "photos").mkdir(parents=True)
    _make_card(tmp_path, "GOPRO01")
    assert [c.camera_id for c in find_cards([tmp_path])] == ["4313"]


def test_sdcard_scanner_reports_cards(tmp_path: Path) -> None:
    _make_card(tmp_path)
    assert asyncio.run(SdCardScanner(roots=[tmp_path]).scan()) == ["4313"]
    assert asyncio.run(SdCardScanner(roots=[tmp_path / "nope"]).scan()) == []


def test_mount_for_raises_when_card_removed(tmp_path: Path) -> None:
    mount = _make_card(tmp_path)
    assert mount_for("4313", [tmp_path]) == mount
    with pytest.raises(CameraError, match="no longer mounted"):
        mount_for("9999", [tmp_path])


# --------------------------------------------------------------------------- #
# SdCardCamera
# --------------------------------------------------------------------------- #


def test_sdcard_camera_lists_sorted_by_mtime_with_lrv_pairing(tmp_path: Path) -> None:
    mount = _make_card(
        tmp_path,
        clips=[("GX010002.MP4", 2000.0), ("GX010001.MP4", 1000.0), ("GH010003.MP4", 3000.0)],
    )
    # Only the first clip has an LRV proxy on the card.
    (mount / "DCIM" / "100GOPRO" / "GL010001.LRV").write_bytes(b"lrv")
    # Non-recording files are never listed.
    (mount / "DCIM" / "100GOPRO" / "GX010001.THM").write_bytes(b"thm")
    (mount / "DCIM" / "100GOPRO" / "notes.txt").write_text("x")

    videos = asyncio.run(SdCardCamera(mount).list_videos())
    assert [v.filename for v in videos] == ["GX010001.MP4", "GX010002.MP4", "GH010003.MP4"]
    assert [v.has_lrv for v in videos] == [True, False, False]
    assert videos[0].created_epoch == 1000.0


def test_sdcard_camera_download_copies_bytes(tmp_path: Path) -> None:
    mount = _make_card(tmp_path, clips=[("GX010001.MP4", 1000.0)])
    (mount / "DCIM" / "100GOPRO" / "GL010001.LRV").write_bytes(b"proxy-bytes")

    async def scenario() -> tuple[bytes, bytes]:
        async with SdCardCamera(mount) as cam:
            (media,) = await cam.list_videos()
            mp4 = await cam.download_mp4(media, tmp_path / "out.MP4")
            lrv = await cam.download_lrv(media, tmp_path / "out.LRV")
            return mp4.read_bytes(), lrv.read_bytes()

    mp4_bytes, lrv_bytes = asyncio.run(scenario())
    assert mp4_bytes == b"\x00\x11\x22\x33" * 64
    assert lrv_bytes == b"proxy-bytes"


def test_sdcard_camera_open_requires_dcim(tmp_path: Path) -> None:
    with pytest.raises(CameraError, match="DCIM"):
        asyncio.run(SdCardCamera(tmp_path / "empty").open())


def test_sdcard_camera_delete_media_unlinks_mp4_lrv_thm(tmp_path: Path) -> None:
    mount = _make_card(tmp_path, clips=[("GX010001.MP4", 1000.0)])
    folder = mount / "DCIM" / "100GOPRO"
    (folder / "GL010001.LRV").write_bytes(b"lrv")
    (folder / "GX010001.THM").write_bytes(b"thm")

    media = RemoteMedia(
        camera_path="100GOPRO/GX010001.MP4", created_epoch=1000.0, size=1, has_lrv=True
    )
    asyncio.run(SdCardCamera(mount).delete_media(media))
    assert list(folder.iterdir()) == []


# --------------------------------------------------------------------------- #
# Through the real pull path
# --------------------------------------------------------------------------- #


def test_sdcard_pull_stages_through_real_pull(tmp_path: Path) -> None:
    """An inserted card drives the real pull: staged files, manifest, event, idempotency."""
    mount = _make_card(tmp_path / "mounts", clips=[("GX010001.MP4", 1_700_000_000.0)])
    events: list[dict[str, object]] = []

    class _Capture:
        def emit(self, event: dict[str, object]) -> None:
            events.append(event)

    root = tmp_path / "raw"
    jumps = asyncio.run(
        pull_camera("4313", camera=SdCardCamera(mount), root=root, emitter=_Capture())
    )
    assert len(jumps) == 1 and jumps[0].skipped is False
    assert jumps[0].mp4_path.exists()
    assert len(events) == 1 and events[0]["job_id"] == "4313-GX010001"

    # Re-pull is idempotent: already staged → skipped, no new event.
    again = asyncio.run(
        pull_camera("4313", camera=SdCardCamera(mount), root=root, emitter=_Capture())
    )
    assert again[0].skipped is True and len(events) == 1


def test_sweep_clears_confirmed_files_off_card(tmp_path: Path) -> None:
    """Retention works on inserted cards: only the S3-confirmed file is deleted."""
    mount = _make_card(
        tmp_path / "mounts",
        clips=[("GX010001.MP4", 1_700_000_000.0), ("GX010002.MP4", 1_700_000_100.0)],
    )
    root = tmp_path / "raw"
    asyncio.run(pull_camera("4313", camera=SdCardCamera(mount), root=root, emit=False))

    # S3 confirmed only the first clip. The SIZE is part of the record: a ledger entry
    # authorises deleting the exact file it confirmed, never just a name (ingest.retention
    # — a reused GX010001.MP4 on a formatted card must survive).
    clip = mount / "DCIM" / "100GOPRO" / "GX010001.MP4"
    record_uploaded(
        camera_dir(root, "4313"), "GX010001.MP4", "raw/4313/2023-11-14/GX010001.MP4",
        size=clip.stat().st_size,
    )

    asyncio.run(
        pull_camera(
            "4313",
            camera=SdCardCamera(mount),
            root=root,
            emit=False,
            cleanup=True,
            cleanup_min_age_s=0.0,
        )
    )
    folder = mount / "DCIM" / "100GOPRO"
    assert sorted(p.name for p in folder.iterdir()) == ["GX010002.MP4"]
