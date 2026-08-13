"""Tests for card retention (:mod:`ingest.retention`) and the pull-time sweep.

Deleting footage off a camera is the one irreversible thing this system does to a
customer's only copy, so these lock down the safety rules rather than the happy path:
nothing is deleted unless S3 confirmed it, the grace period is honoured, an unknown or
unreadable ledger keeps everything, and a delete failure never costs us a pull.

Since the 2026-08-11 audit they also lock down *identity*: a ledger record authorises
deleting the exact physical file it confirmed, never merely "some file with this name"
(see :class:`TestFilenameReuse` — the reused-``GX010001.MP4`` case).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ingest.camera import Camera, CameraError, RemoteMedia
from ingest.pull import _sweep_card
from ingest.retention import confirmed, deletable, ledger_path, record_uploaded

HOUR = 3600.0


#: Byte size the fake card's clips report, and what the ledger records for them. The
#: ledger match is size-based, so tests must keep the two in step deliberately.
CLIP_SIZE = 4_096


def _media(name: str, size: int | None = CLIP_SIZE) -> RemoteMedia:
    return RemoteMedia(
        camera_path=f"100GOPRO/{name}", created_epoch=None, size=size, has_lrv=True
    )


def _on_card(*names: str, size: int | None = CLIP_SIZE) -> dict[str, int | None]:
    """The ``{name: size}`` mapping ``deletable`` takes from a real card listing."""
    return dict.fromkeys(names, size)


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
        record_uploaded(
            tmp_path, "GX010001.MP4", "raw/CAM/GX010001.MP4", size=CLIP_SIZE, now=100.0
        )
        got = confirmed(tmp_path)
        assert got["GX010001.MP4"].s3_key == "raw/CAM/GX010001.MP4"
        assert got["GX010001.MP4"].at == 100.0
        assert got["GX010001.MP4"].size == CLIP_SIZE

    def test_missing_ledger_is_empty_not_an_error(self, tmp_path: Path):
        assert confirmed(tmp_path / "never-used") == {}

    def test_corrupt_ledger_keeps_everything(self, tmp_path: Path):
        ledger_path(tmp_path).write_text("{not json")
        assert confirmed(tmp_path) == {}
        assert deletable(tmp_path, _on_card("GX010001.MP4"), min_age_s=0, now=1e9) == []

    def test_record_never_raises_on_unwritable_dir(self, tmp_path: Path):
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory")
        record_uploaded(blocker, "GX010001.MP4", "raw/CAM/x", now=1.0)  # must not raise


class TestDeletable:
    def test_only_files_s3_confirmed(self, tmp_path: Path):
        record_uploaded(
            tmp_path, "GX010001.MP4", "raw/CAM/GX010001.MP4", size=CLIP_SIZE, now=0.0
        )
        # GX010002 was never uploaded — it must survive.
        names = [r.filename for r in deletable(
            tmp_path, _on_card("GX010001.MP4", "GX010002.MP4"), min_age_s=0, now=HOUR)]
        assert names == ["GX010001.MP4"]

    def test_grace_period_holds_recent_files(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "k", size=CLIP_SIZE, now=0.0)
        card = _on_card("GX010001.MP4")
        assert deletable(tmp_path, card, min_age_s=24 * HOUR, now=23 * HOUR) == []
        ready = deletable(tmp_path, card, min_age_s=24 * HOUR, now=24 * HOUR)
        assert [r.filename for r in ready] == ["GX010001.MP4"]

    def test_file_no_longer_on_card_is_not_reported(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "k", size=CLIP_SIZE, now=0.0)
        assert deletable(tmp_path, {}, min_age_s=0, now=HOUR) == []


class TestFilenameReuse:
    """A filename is not an identity (audit §3-F).

    GoPro numbering restarts at ``GX010001.MP4`` on a formatted or replaced card, and two
    unlabeled cards both identify as ``sd-NO-NAME`` — so one ledger legitimately holds
    yesterday's ``GX010001.MP4`` while a DIFFERENT file of that name sits on today's card.
    Deleting on the name alone destroys footage that was never uploaded.
    """

    def test_stale_record_never_authorises_a_different_file_of_the_same_name(
        self, tmp_path: Path
    ):
        # Yesterday: a 4 KB GX010001.MP4 was confirmed into S3.
        record_uploaded(tmp_path, "GX010001.MP4", "raw/CAM/2026-08-10/GX010001.MP4",
                        size=CLIP_SIZE, now=0.0)
        # Today: a different, larger GX010001.MP4 is on the card, never uploaded.
        assert deletable(
            tmp_path, {"GX010001.MP4": CLIP_SIZE * 3}, min_age_s=0, now=24 * HOUR
        ) == []

    def test_the_same_physical_file_is_still_deletable(self, tmp_path: Path):
        record_uploaded(tmp_path, "GX010001.MP4", "raw/CAM/2026-08-11/GX010001.MP4",
                        size=CLIP_SIZE, now=0.0)
        ready = deletable(tmp_path, _on_card("GX010001.MP4"), min_age_s=0, now=24 * HOUR)
        assert [r.filename for r in ready] == ["GX010001.MP4"]

    def test_a_record_without_a_size_is_unverifiable_and_never_deletable(
        self, tmp_path: Path
    ):
        """Ledgers written before the size field must fail SAFE, not fall back to names."""
        record_uploaded(tmp_path, "GX010001.MP4", "raw/CAM/GX010001.MP4", now=0.0)
        assert confirmed(tmp_path)["GX010001.MP4"].size is None
        assert deletable(
            tmp_path, _on_card("GX010001.MP4"), min_age_s=0, now=24 * HOUR
        ) == []

    def test_an_unmeasurable_card_file_is_never_deletable(self, tmp_path: Path):
        """A camera listing with no size is not proof either."""
        record_uploaded(tmp_path, "GX010001.MP4", "k", size=CLIP_SIZE, now=0.0)
        assert deletable(
            tmp_path, {"GX010001.MP4": None}, min_age_s=0, now=24 * HOUR
        ) == []

    def test_sweep_keeps_a_reused_filename(self, tmp_path: Path):
        """End-to-end through the pull's sweep: the camera is never asked to delete it."""
        root = tmp_path / "raw-storage"
        record_uploaded(root / "_camera-staging" / "CAM1", "GX010001.MP4",
                        "raw/CAM1/2026-08-10/GX010001.MP4", size=CLIP_SIZE, now=0.0)
        cam = _FakeCamera()
        freed = asyncio.run(_sweep_card(
            cam, "CAM1", [_media("GX010001.MP4", size=CLIP_SIZE * 3)], root,
            min_age_s=0, dry_run=False, now=lambda: 24 * HOUR))
        assert cam.deleted == []
        assert freed == []


class TestSweep:
    def _ledger(self, tmp_path: Path, *names: str, at: float = 0.0) -> Path:
        root = tmp_path / "raw-storage"
        cam_dir = root / "_camera-staging" / "CAM1"
        for n in names:
            record_uploaded(cam_dir, n, f"raw/CAM1/{n}", size=CLIP_SIZE, now=at)
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
