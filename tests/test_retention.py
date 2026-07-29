"""Tests for card retention (:mod:`ingest.retention`) and the pull-time sweep.

Deleting footage off a camera is the one irreversible thing this system does to a
customer's only copy, so these lock down the safety rules rather than the happy path:
nothing is deleted unless S3 confirmed it, the grace period is honoured, an unknown or
unreadable ledger keeps everything, and a delete failure never costs us a pull.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ingest.camera import Camera, CameraError, RemoteMedia
from ingest.pull import _sweep_card
from ingest.retention import confirmed, deletable, ledger_path, record_uploaded

HOUR = 3600.0


def _media(name: str) -> RemoteMedia:
    return RemoteMedia(camera_path=f"100GOPRO/{name}", created_epoch=None, size=1, has_lrv=True)


class _FakeCamera(Camera):
    """Records deletions; can be told to fail on specific files."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self._fail = fail or set()

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def list_videos(self) -> list[RemoteMedia]: return []
    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path: return dest
    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path: return dest
    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path: return dest

    async def delete_media(self, media: RemoteMedia) -> None:
        if media.filename in self._fail:
            raise CameraError(f"boom: {media.filename}")
        self.deleted.append(media.filename)


class TestLedger:
    def test_records_and_reads_back(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "raw/CAM/GX010001.MP4", now=100.0)
        got = confirmed(tmp_path)
        assert got["GX010001.MP4"].s3_key == "raw/CAM/GX010001.MP4"
        assert got["GX010001.MP4"].at == 100.0

    def test_missing_ledger_is_empty_not_an_error(self, tmp_path: Path):
        assert confirmed(tmp_path / "never-used") == {}

    def test_corrupt_ledger_keeps_everything(self, tmp_path: Path):
        ledger_path(tmp_path).write_text("{not json")
        assert confirmed(tmp_path) == {}
        assert deletable(tmp_path, ["GX010001.MP4"], min_age_s=0, now=1e9) == []

    def test_record_never_raises_on_unwritable_dir(self, tmp_path: Path):
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory")
        record_uploaded(blocker, "GX010001.MP4", "raw/CAM/x", now=1.0)  # must not raise


class TestDeletable:
    def test_only_files_s3_confirmed(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "raw/CAM/GX010001.MP4", now=0.0)
        # GX010002 was never uploaded — it must survive.
        names = [r.filename for r in deletable(
            tmp_path, ["GX010001.MP4", "GX010002.MP4"], min_age_s=0, now=HOUR)]
        assert names == ["GX010001.MP4"]

    def test_grace_period_holds_recent_files(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "k", now=0.0)
        assert deletable(tmp_path, ["GX010001.MP4"], min_age_s=24 * HOUR, now=23 * HOUR) == []
        ready = deletable(tmp_path, ["GX010001.MP4"], min_age_s=24 * HOUR, now=24 * HOUR)
        assert [r.filename for r in ready] == ["GX010001.MP4"]

    def test_file_no_longer_on_card_is_not_reported(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "k", now=0.0)
        assert deletable(tmp_path, [], min_age_s=0, now=HOUR) == []


class TestSweep:
    def _ledger(self, tmp_path: Path, *names: str, at: float = 0.0) -> Path:
        root = tmp_path / "raw-storage"
        cam_dir = root / "_camera-staging" / "CAM1"
        for n in names:
            record_uploaded(cam_dir, n, f"raw/CAM1/{n}", now=at)
        return root

    def test_deletes_confirmed_keeps_unknown(self, tmp_path: Path):
        root = self._ledger(tmp_path, "GX010001.MP4")
        cam = _FakeCamera()
        videos = [_media("GX010001.MP4"), _media("GX010002.MP4")]
        freed = asyncio.run(_sweep_card(
            cam, "CAM1", videos, root, min_age_s=0, dry_run=False, now=lambda: HOUR))
        assert cam.deleted == ["GX010001.MP4"]
        assert freed == ["GX010001.MP4"]

    def test_dry_run_deletes_nothing(self, tmp_path: Path):
        root = self._ledger(tmp_path, "GX010001.MP4")
        cam = _FakeCamera()
        freed = asyncio.run(_sweep_card(
            cam, "CAM1", [_media("GX010001.MP4")], root,
            min_age_s=0, dry_run=True, now=lambda: HOUR))
        assert cam.deleted == []
        assert freed == ["GX010001.MP4"]  # reported, not performed

    def test_delete_failure_is_survivable(self, tmp_path: Path):
        """One camera error must not abort the sweep or the pull behind it."""
        root = self._ledger(tmp_path, "GX010001.MP4", "GX010002.MP4")
        cam = _FakeCamera(fail={"GX010001.MP4"})
        videos = [_media("GX010001.MP4"), _media("GX010002.MP4")]
        freed = asyncio.run(_sweep_card(
            cam, "CAM1", videos, root, min_age_s=0, dry_run=False, now=lambda: HOUR))
        assert cam.deleted == ["GX010002.MP4"]
        assert freed == ["GX010002.MP4"]  # the failed one is NOT reported as freed

    def test_no_ledger_deletes_nothing(self, tmp_path: Path):
        """The dangerous default: never seen this camera → never delete."""
        cam = _FakeCamera()
        freed = asyncio.run(_sweep_card(
            cam, "CAM1", [_media("GX010001.MP4")], tmp_path / "raw-storage",
            min_age_s=0, dry_run=False, now=lambda: HOUR))
        assert cam.deleted == []
        assert freed == []


def test_camera_base_refuses_to_delete_by_default():
    """A transport that hasn't implemented deletion must fail loudly, not silently."""

    class _Bare(Camera):
        async def open(self) -> None: ...
        async def close(self) -> None: ...
        async def list_videos(self) -> list[RemoteMedia]: return []
        async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path: return dest
        async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path: return dest
        async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path: return dest

    with pytest.raises(CameraError, match="does not support deleting"):
        asyncio.run(_Bare().delete_media(_media("GX010001.MP4")))
