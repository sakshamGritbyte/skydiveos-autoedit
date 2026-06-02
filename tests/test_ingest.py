"""Tests for the /ingest stage.

The Open GoPro SDK is hardware-only and not installed in CI, so we never touch a
real camera here: the orchestration in :mod:`ingest.pull` is driven through a
:class:`FakeCamera` that writes placeholder bytes to the download targets. Pure
helpers (LRV path derivation, storage layout, event building, emitters) are
tested directly.

These tests are dependency-free async: rather than require pytest-asyncio we
drive coroutines with :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ingest import storage
from ingest.camera import Camera, CameraError, GoProCamera, RemoteMedia, lrv_camera_path, pair
from ingest.events import EVENT_NAME, FileEventEmitter, build_event
from ingest.pull import pull_camera

FIXED_NOW = 1_700_000_500.0
# 2024-05-29 12:00:00 UTC — a stable creation time for layout assertions.
CREATED = 1_716_984_000.0


def _media(name: str, *, created: float | None = CREATED, has_lrv: bool = True) -> RemoteMedia:
    return RemoteMedia(
        camera_path=f"100GOPRO/{name}", created_epoch=created, size=None, has_lrv=has_lrv
    )


class FakeCamera(Camera):
    """In-memory :class:`Camera` that writes placeholder files instead of pulling."""

    def __init__(
        self,
        videos: list[RemoteMedia],
        *,
        fail_lrv: tuple[str, ...] = (),
        fail_thumb: tuple[str, ...] = (),
    ) -> None:
        self._videos = videos
        self._fail_lrv = set(fail_lrv)
        self._fail_thumb = set(fail_thumb)
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def list_videos(self) -> list[RemoteMedia]:
        return list(self._videos)

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        dest.write_bytes(b"mp4:" + media.filename.encode())
        return dest

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        if media.stem in self._fail_lrv:
            raise CameraError(f"no LRV for {media.stem}")
        dest.write_bytes(b"lrv")
        return dest

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        if media.stem in self._fail_thumb:
            raise CameraError(f"no thumbnail for {media.stem}")
        dest.write_bytes(b"jpg")
        return dest


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mp4", "expected"),
    [
        ("100GOPRO/GX010123.MP4", "100GOPRO/GL010123.LRV"),
        ("100GOPRO/GH010123.MP4", "100GOPRO/GL010123.LRV"),
        ("GX019999.MP4", "GL019999.LRV"),
    ],
)
def test_lrv_camera_path(mp4: str, expected: str) -> None:
    assert lrv_camera_path(mp4) == expected


def test_lrv_camera_path_rejects_short_name() -> None:
    with pytest.raises(CameraError):
        lrv_camera_path("100GOPRO/A.MP4")


def test_storage_layout() -> None:
    root = Path("/tmp/raw-storage")
    dest = storage.destination(root, "1234", CREATED, "GX010123.MP4")
    assert dest == root / "1234" / "2024-05-29" / "GX010123.MP4"


def test_date_for_falls_back_to_today_when_unknown() -> None:
    # No exception and a well-formed date even without a creation timestamp.
    assert len(storage.date_for(None)) == len("2024-05-29")


def test_storage_root_prefers_explicit_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert storage.storage_root("/explicit") == Path("/explicit")
    monkeypatch.setenv("RAW_STORAGE_ROOT", "/from-env")
    assert storage.storage_root() == Path("/from-env")
    monkeypatch.delenv("RAW_STORAGE_ROOT")
    assert storage.storage_root() == storage.DEFAULT_ROOT


def test_build_event_shape() -> None:
    event = build_event(
        job_id="1234-GX010123",
        camera_id="1234",
        jump_dir=Path("/r/1234/2024-05-29"),
        mp4_path=Path("/r/1234/2024-05-29/GX010123.MP4"),
        lrv_path=Path("/r/1234/2024-05-29/GX010123.LRV"),
        thumbnail_path=None,
        created_epoch=CREATED,
        emitted_at=FIXED_NOW,
    )
    assert event["event"] == EVENT_NAME
    assert event["job_id"] == "1234-GX010123"
    assert event["files"]["lrv"].endswith("GX010123.LRV")
    assert event["files"]["thumbnail"] is None
    assert event["emitted_at"] == FIXED_NOW


def test_file_emitter_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    emitter = FileEventEmitter(path)
    emitter.emit({"event": EVENT_NAME, "job_id": "a"})
    emitter.emit({"event": EVENT_NAME, "job_id": "b"})
    lines = path.read_text().splitlines()
    assert [json.loads(line)["job_id"] for line in lines] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Orchestration (fake camera)
# --------------------------------------------------------------------------- #


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_pull_camera_downloads_and_emits(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    cam = FakeCamera([_media("GX010123.MP4"), _media("GX020456.MP4")])

    jumps = asyncio.run(
        pull_camera(
            "1234",
            root=tmp_path,
            emitter=FileEventEmitter(events_file),
            camera=cam,
            now=lambda: FIXED_NOW,
        )
    )

    assert cam.opened and cam.closed
    assert [j.skipped for j in jumps] == [False, False]
    assert [j.job_id for j in jumps] == ["1234-GX010123", "1234-GX020456"]

    day_dir = tmp_path / "1234" / "2024-05-29"
    for stem in ("GX010123", "GX020456"):
        assert (day_dir / f"{stem}.MP4").exists()
        assert (day_dir / f"{stem}.LRV").exists()
        assert (day_dir / f"{stem}.thumbnail.jpg").exists()
        assert (day_dir / f"{stem}.ingest.json").exists()  # manifest sidecar

    emitted = _events(events_file)
    assert [e["event"] for e in emitted] == [EVENT_NAME, EVENT_NAME]
    assert emitted[0]["files"]["lrv"].endswith("GX010123.LRV")


def test_pull_camera_is_idempotent(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    videos = [_media("GX010123.MP4")]

    first = asyncio.run(
        pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                    camera=FakeCamera(videos), now=lambda: FIXED_NOW)
    )
    second = asyncio.run(
        pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                    camera=FakeCamera(videos), now=lambda: FIXED_NOW)
    )

    assert first[0].skipped is False
    assert second[0].skipped is True
    # The re-run must not emit a second event for an already-staged jump.
    assert len(_events(events_file)) == 1


def test_repull_overrides_idempotency(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    videos = [_media("GX010123.MP4")]
    asyncio.run(pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                            camera=FakeCamera(videos), now=lambda: FIXED_NOW))
    again = asyncio.run(pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                                    camera=FakeCamera(videos), repull=True, now=lambda: FIXED_NOW))
    assert again[0].skipped is False
    assert len(_events(events_file)) == 2


def test_since_filters_old_recordings(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    cam = FakeCamera(
        [_media("GX010001.MP4", created=1000.0), _media("GX010002.MP4", created=5000.0)]
    )
    jumps = asyncio.run(
        pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                    camera=cam, since=2000.0, now=lambda: FIXED_NOW)
    )
    assert [j.media.stem for j in jumps] == ["GX010002"]


def test_missing_lrv_is_tolerated(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    # has_lrv False -> we never attempt the LRV; event records lrv=None.
    cam = FakeCamera([_media("GX010123.MP4", has_lrv=False)])
    jumps = asyncio.run(pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                                    camera=cam, now=lambda: FIXED_NOW))
    assert jumps[0].lrv_path is None
    assert _events(events_file)[0]["files"]["lrv"] is None


def test_lrv_download_failure_does_not_strand_mp4(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    cam = FakeCamera([_media("GX010123.MP4")], fail_lrv=("GX010123",))
    jumps = asyncio.run(pull_camera("1234", root=tmp_path, emitter=FileEventEmitter(events_file),
                                    camera=cam, now=lambda: FIXED_NOW))
    assert jumps[0].skipped is False
    assert jumps[0].lrv_path is None
    assert jumps[0].mp4_path.exists()  # MP4 still staged + event emitted
    assert len(_events(events_file)) == 1


def test_emit_disabled_writes_no_events(tmp_path: Path) -> None:
    cam = FakeCamera([_media("GX010123.MP4")])
    asyncio.run(pull_camera("1234", root=tmp_path, camera=cam, emit=False, now=lambda: FIXED_NOW))
    assert not (tmp_path / "_events.jsonl").exists()


# --------------------------------------------------------------------------- #
# Missing-SDK behavior (open_gopro is not installed in CI)
# --------------------------------------------------------------------------- #


def test_real_camera_open_raises_clear_error_without_sdk() -> None:
    try:
        import open_gopro  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("open_gopro SDK is installed; missing-SDK path not exercised")

    with pytest.raises(CameraError, match="not installed"):
        asyncio.run(GoProCamera("1234").open())
    with pytest.raises(CameraError, match="not installed"):
        asyncio.run(pair("1234"))
