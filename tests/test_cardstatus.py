"""Tests for the SD-card ingest status view (:mod:`ingest.cardstatus`).

The registry is the operator's "safe to remove" signal, so the properties that
matter are behavioural: tracking must never break a pull, an idempotent re-pull
must not flap a ``safe_to_remove`` badge back to ``pulling``, and a card's
entry must survive (or be cleared on) removal according to how its pull ended.
No hardware anywhere: a fake :class:`~ingest.camera.Camera` stands in for the
card, matching :mod:`tests.test_sdcard`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from ingest.camera import Camera, CameraError, RemoteMedia
from ingest.cardstatus import (
    STATE_DETECTED,
    STATE_ERROR,
    STATE_PULLING,
    STATE_SAFE_TO_REMOVE,
    STATE_SWEEPING,
    CardStatusRegistry,
    ObservingScanner,
    TrackedCamera,
)
from ingest.scanner import StaticCameraScanner

# --------------------------------------------------------------------------- #
# Registry lifecycle
# --------------------------------------------------------------------------- #


class _Clock:
    """An injectable clock so linger windows are deterministic."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_full_lifecycle_snapshot() -> None:
    """detected → pulling (with progress) → safe_to_remove, all fields served."""
    reg = CardStatusRegistry(now=_Clock())
    reg.detected("4313")
    assert reg.snapshot()[0]["state"] == STATE_DETECTED

    reg.pull_started("4313")
    reg.totals("4313", files_total=2, bytes_total=300)
    reg.file_started("4313", "GX010001.MP4")
    reg.file_done("4313", 100)
    reg.file_started("4313", "GX010002.MP4")

    [card] = reg.snapshot()
    assert card["state"] == STATE_PULLING
    assert card["files_done"] == 1
    assert card["files_total"] == 2
    assert card["bytes_done"] == 100
    assert card["bytes_total"] == 300
    assert card["current_file"] == "GX010002.MP4"

    reg.file_done("4313", 200)
    reg.safe_to_remove("4313")
    [card] = reg.snapshot()
    assert card["state"] == STATE_SAFE_TO_REMOVE
    assert card["current_file"] is None
    assert card["error"] is None


def test_sweeping_leaves_totals_consistent() -> None:
    """A retention delete marks the card busy and removes the file from the totals."""
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    reg.totals("4313", files_total=3, bytes_total=600)
    reg.sweeping("4313", freed_bytes=200)
    [card] = reg.snapshot()
    assert card["state"] == STATE_SWEEPING
    assert card["files_total"] == 2
    assert card["bytes_total"] == 400


def test_error_records_message() -> None:
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    reg.error("4313", "card yanked mid-copy")
    [card] = reg.snapshot()
    assert card["state"] == STATE_ERROR
    assert card["error"] == "card yanked mid-copy"


def test_repull_does_not_flap_safe_to_remove() -> None:
    """Discovery re-pulls a lingering card every tick; the badge must not flicker."""
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    reg.safe_to_remove("4313")

    reg.pull_started("4313")  # idempotent re-pull of a fully staged card
    assert reg.snapshot()[0]["state"] == STATE_SAFE_TO_REMOVE

    # Real card activity still flips it back to busy.
    reg.file_started("4313", "GX010009.MP4")
    assert reg.snapshot()[0]["state"] == STATE_PULLING


def test_observe_adds_detected_and_clears_removed_cards() -> None:
    clock = _Clock()
    reg = CardStatusRegistry(now=clock)

    reg.observe(["4313"])
    assert reg.snapshot()[0]["state"] == STATE_DETECTED

    # A safe_to_remove card that disappears was removed on purpose: drop it.
    reg.safe_to_remove("4313")
    reg.observe([])
    assert reg.snapshot() == []


def test_observe_lets_errors_linger_after_removal() -> None:
    """A failed card's entry must outlive the yank so the operator sees it."""
    clock = _Clock()
    reg = CardStatusRegistry(now=clock)
    reg.error("4313", "boom")

    reg.observe([])  # card gone, error fresh — keep it visible
    assert reg.snapshot()[0]["state"] == STATE_ERROR

    clock.t += 1000.0  # past the linger window
    reg.observe([])
    assert reg.snapshot() == []


def test_observe_leaves_inflight_pull_alone() -> None:
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    reg.file_started("4313", "GX010001.MP4")
    reg.observe([])  # mid-pull absence is the pull's problem, not observe's
    assert reg.snapshot()[0]["state"] == STATE_PULLING


# --------------------------------------------------------------------------- #
# TrackedCamera
# --------------------------------------------------------------------------- #


def _media(name: str, size: int) -> RemoteMedia:
    return RemoteMedia(
        camera_path=f"100GOPRO/{name}", created_epoch=1000.0, size=size, has_lrv=False
    )


class _FakeCamera(Camera):
    """A card with two clips; downloads just create the destination file."""

    def __init__(self) -> None:
        self.clips = [_media("GX010001.MP4", 100), _media("GX010002.MP4", 200)]
        self.deleted: list[str] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_videos(self) -> list[RemoteMedia]:
        return list(self.clips)

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        dest.write_bytes(b"x" * (media.size or 0))
        return dest

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        raise CameraError("no LRV on this fake card")

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        raise CameraError("no thumbnail on this fake card")

    async def delete_media(self, media: RemoteMedia) -> None:
        self.deleted.append(media.filename)


def test_tracked_camera_reports_progress(tmp_path: Path) -> None:
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    cam = TrackedCamera(_FakeCamera(), reg, "4313")

    async def _pull() -> None:
        videos = await cam.list_videos()
        for media in videos:
            await cam.download_mp4(media, tmp_path / media.filename)

    asyncio.run(_pull())
    [card] = reg.snapshot()
    assert card["files_done"] == 2
    assert card["files_total"] == 2
    assert card["bytes_done"] == 300
    assert card["bytes_total"] == 300


def test_tracked_camera_marks_sweep_deletes(tmp_path: Path) -> None:
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    inner = _FakeCamera()
    cam = TrackedCamera(inner, reg, "4313")

    async def _sweep() -> None:
        videos = await cam.list_videos()
        await cam.delete_media(videos[0])

    asyncio.run(_sweep())
    assert inner.deleted == ["GX010001.MP4"]
    [card] = reg.snapshot()
    assert card["state"] == STATE_SWEEPING
    assert card["files_total"] == 1
    assert card["bytes_total"] == 200


def test_tracking_failure_never_breaks_the_pull(tmp_path: Path) -> None:
    """A broken progress bar must not cost the customer's footage."""

    class _Broken(CardStatusRegistry):
        def file_started(self, camera_id: str, filename: str) -> None:
            raise RuntimeError("status backend exploded")

        def totals(self, camera_id: str, files_total: int, bytes_total: int) -> None:
            raise RuntimeError("status backend exploded")

    cam = TrackedCamera(_FakeCamera(), _Broken(), "4313")

    async def _pull() -> Path:
        [media, _] = await cam.list_videos()
        return await cam.download_mp4(media, tmp_path / media.filename)

    dest = asyncio.run(_pull())
    assert dest.is_file()  # the copy happened despite the tracker raising


# --------------------------------------------------------------------------- #
# ObservingScanner
# --------------------------------------------------------------------------- #


def test_observing_scanner_mirrors_presence_and_passes_ids_through() -> None:
    reg = CardStatusRegistry(now=_Clock())
    scanner = ObservingScanner(StaticCameraScanner(["4313", "7788"]), reg)

    ids = asyncio.run(scanner.scan())
    assert ids == ["4313", "7788"]
    assert [c["camera_id"] for c in reg.snapshot()] == ["4313", "7788"]
    assert all(c["state"] == STATE_DETECTED for c in reg.snapshot())


# --------------------------------------------------------------------------- #
# GET /ingest/cards
# --------------------------------------------------------------------------- #


def test_endpoint_empty_when_sdcard_ingest_is_off() -> None:
    """Discovery off (the pinned test env) → the route exists and serves []."""
    with TestClient(create_app()) as client:
        resp = client.get("/ingest/cards")
    assert resp.status_code == 200
    assert resp.json() == []


def test_endpoint_serves_the_registry_snapshot() -> None:
    app = create_app()
    reg = CardStatusRegistry(now=_Clock())
    reg.pull_started("4313")
    reg.totals("4313", files_total=2, bytes_total=300)
    reg.file_started("4313", "GX010001.MP4")
    with TestClient(app) as client:
        app.state.card_status = reg  # what lifespan does under CAMERA_SCANNER=sdcard
        resp = client.get("/ingest/cards")
    assert resp.status_code == 200
    [card] = resp.json()
    assert card["camera_id"] == "4313"
    assert card["state"] == STATE_PULLING
    assert card["current_file"] == "GX010001.MP4"
    assert card["bytes_total"] == 300
