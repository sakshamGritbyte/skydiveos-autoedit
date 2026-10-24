"""Two cards, one camera, one day, restarted GoPro numbering — the audit's P0.

GoPro numbering restarts at ``GX010001.MP4`` on a formatted card. An instructor who
rotates two cards through one camera in one day therefore offers the ingest box two
DIFFERENT recordings under the same ``{camera_id}/{date}/GX010001.MP4`` staging path.
Before this fix ``ingest.pull._pull_one`` asked only "is there a complete staging at
that path?" and skipped the second card's clip as already staged: never staged, never
uploaded, never matched, never delivered — while the banner went green.

These tests pin the whole chain: the pull stages both recordings (the second in a
collision sibling folder, keeping its GoPro filename), the retention ledger holds one
record per RECORDING so neither card's confirmation can erase the other's, the
"safe to remove" answer sees the second card as still owed until ITS upload is
confirmed, the S3 key never overwrites the first recording, and a genuine re-pull of
the same clip is still skipped (idempotency). Nothing here touches a real card.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ingest.camera import Camera, CameraError, RemoteMedia
from ingest.discovery import _capture_day, _ledger_dir, raw_object_key
from ingest.pull import pull_camera
from ingest.retention import (
    card_transfer,
    confirmed,
    confirmed_for,
    deletable,
    record_uploaded,
)
from ingest.storage import (
    camera_dir,
    camera_dir_of,
    collision_dir_name,
    day_dir_of,
    is_complete,
    resolve_destination,
)

DAY_EPOCH = 1_792_800_000.0  # 2026-10-24 (UTC)
DAY = "2026-10-24"


class _Card(Camera):
    """One inserted card: ``name -> bytes``; every clip is created at ``created``."""

    def __init__(self, files: dict[str, bytes], *, created: float = DAY_EPOCH) -> None:
        self.files = dict(files)
        self.created = created
        self.deleted: list[str] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_videos(self) -> list[RemoteMedia]:
        return [
            RemoteMedia(
                camera_path=f"100GOPRO/{name}",
                created_epoch=self.created,
                size=len(body),
                has_lrv=False,
            )
            for name, body in sorted(self.files.items())
        ]

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        dest.write_bytes(self.files[media.filename])
        return dest

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        raise CameraError("no LRV")

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        raise CameraError("no THM")

    async def delete_media(self, media: RemoteMedia) -> None:
        self.deleted.append(media.filename)
        del self.files[media.filename]


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _pull(card: _Card, root: Path, sink: _Sink, **kw) -> list:
    return asyncio.run(pull_camera("4812", root=root, camera=card, emitter=sink, **kw))


CARD1 = b"card-one GX010001 " * 40
CARD2 = b"card-two GX010001 (formatted card, numbering restarted)" * 11


# --------------------------------------------------------------------------- #
# The pull stages BOTH recordings
# --------------------------------------------------------------------------- #


def test_second_card_with_restarted_numbering_is_staged_not_skipped(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    sink = _Sink()

    first = _pull(_Card({"GX010001.MP4": CARD1}), root, sink)
    assert [j.skipped for j in first] == [False]
    primary = root / "_camera-staging" / "4812" / DAY / "GX010001.MP4"
    assert primary.read_bytes() == CARD1

    # The second card of the day: same name, a different recording.
    second = _pull(_Card({"GX010001.MP4": CARD2}, created=DAY_EPOCH + 3600), root, sink)
    assert [j.skipped for j in second] == [False], "the second card's clip was skipped"
    alt = second[0].mp4_path
    assert alt != primary
    assert alt.name == "GX010001.MP4", "the GoPro filename is preserved"
    assert alt.parent.parent == primary.parent, "staged beside the first, under the same day"
    assert alt.parent.name == collision_dir_name(len(CARD2), DAY_EPOCH + 3600)
    assert alt.read_bytes() == CARD2
    assert primary.read_bytes() == CARD1, "the first recording is untouched"

    # Both were handed off — two events, two distinct files.
    assert [Path(e["files"]["mp4"]) for e in sink.events] == [primary, alt]
    # The manifest traces the staged file back to its source.
    manifest = json.loads(alt.with_suffix(".ingest.json").read_text())
    assert manifest["staged"] == {
        "size": len(CARD2), "camera_path": "100GOPRO/GX010001.MP4", "collision": True,
    }
    assert "staged" not in sink.events[1], "the emitted event keeps its shape"


def test_collision_staging_keeps_each_cards_provenance(tmp_path: Path, caplog) -> None:
    """Both manifests say which CARD they came off (the SD-card pull's source stamp),
    and the collision warning names the second card's serial."""
    import logging

    root = tmp_path / "raw"
    sink = _Sink()
    asyncio.run(pull_camera(
        "4812", root=root, camera=_Card({"GX010001.MP4": CARD1}), emitter=sink,
        source={"transport": "sdcard", "card_serial": "AAAA-0001"},
    ))
    with caplog.at_level(logging.WARNING, logger="ingest.pull"):
        asyncio.run(pull_camera(
            "4812", root=root,
            camera=_Card({"GX010001.MP4": CARD2}, created=DAY_EPOCH + 3600), emitter=sink,
            source={"transport": "sdcard", "card_serial": "BBBB-0002"},
        ))
    assert any("card serial BBBB-0002" in r.getMessage() for r in caplog.records)
    serials = []
    for event in sink.events:
        manifest = json.loads(Path(event["files"]["mp4"]).with_suffix(".ingest.json").read_text())
        serials.append(manifest["source"]["card_serial"])
    assert serials == ["AAAA-0001", "BBBB-0002"]


def test_same_clip_re_pulled_is_still_skipped(tmp_path: Path) -> None:
    """Idempotency survives: the SAME recording (same size and time) is not re-staged."""
    root = tmp_path / "raw"
    sink = _Sink()
    card = _Card({"GX010001.MP4": CARD1})
    _pull(card, root, sink)
    again = _pull(card, root, sink)
    assert [j.skipped for j in again] == [True]
    assert len(sink.events) == 1


def test_a_collision_clip_re_pulled_is_skipped_too(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    sink = _Sink()
    _pull(_Card({"GX010001.MP4": CARD1}), root, sink)
    card2 = _Card({"GX010001.MP4": CARD2}, created=DAY_EPOCH + 3600)
    _pull(card2, root, sink)
    again = _pull(card2, root, sink)
    assert [j.skipped for j in again] == [True]
    assert len(sink.events) == 2
    assert len(list((root / "_camera-staging" / "4812" / DAY).rglob("GX010001.MP4"))) == 2


def test_same_size_but_different_recording_is_not_deduplicated(tmp_path: Path) -> None:
    """The audit's corner case: a byte-identical SIZE must not count as the same clip."""
    root = tmp_path / "raw"
    sink = _Sink()
    body1 = b"A" * 500
    body2 = b"B" * 500
    _pull(_Card({"GX010001.MP4": body1}), root, sink)
    second = _pull(_Card({"GX010001.MP4": body2}, created=DAY_EPOCH + 1800), root, sink)
    assert [j.skipped for j in second] == [False]
    assert second[0].mp4_path.read_bytes() == body2


def test_legacy_staging_without_size_metadata_still_matches_the_same_clip(tmp_path: Path) -> None:
    """A clip staged by the pre-fix code (manifest with no ``staged`` block) is still
    recognised as already staged when the card lists the same size and time."""
    root = tmp_path / "raw"
    day = root / "_camera-staging" / "4812" / DAY
    day.mkdir(parents=True)
    (day / "GX010001.MP4").write_bytes(CARD1)
    (day / "GX010001.ingest.json").write_text(json.dumps({
        "event": "ready_for_processing", "job_id": "4812-GX010001", "camera_id": "4812",
        "jump_dir": str(day), "files": {"mp4": str(day / "GX010001.MP4")},
        "created_epoch": DAY_EPOCH, "emitted_at": 1.0,
    }))
    sink = _Sink()
    result = _pull(_Card({"GX010001.MP4": CARD1}), root, sink)
    assert [j.skipped for j in result] == [True]
    assert sink.events == []


def test_partial_collision_download_is_cleaned_up(tmp_path: Path) -> None:
    """A failed copy of the second card's clip leaves no half file in the sibling folder."""
    root = tmp_path / "raw"
    sink = _Sink()
    _pull(_Card({"GX010001.MP4": CARD1}), root, sink)

    class _Broken(_Card):
        async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
            dest.write_bytes(b"half")
            raise OSError("EIO")

    with pytest.raises(CameraError):
        _pull(_Broken({"GX010001.MP4": CARD2}, created=DAY_EPOCH + 3600), root, sink)
    day = root / "_camera-staging" / "4812" / DAY
    assert [p.name for p in day.rglob("GX010001.MP4")] == ["GX010001.MP4"]
    assert (day / "GX010001.MP4").read_bytes() == CARD1


# --------------------------------------------------------------------------- #
# Storage helpers
# --------------------------------------------------------------------------- #


def test_resolve_destination_and_identity_checks(tmp_path: Path) -> None:
    root = tmp_path
    primary, staged = resolve_destination(root, "4812", DAY_EPOCH, "GX010001.MP4", size=10)
    assert staged is False and primary.name == "GX010001.MP4"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"0123456789")
    primary.with_suffix(".ingest.json").write_text(json.dumps({"created_epoch": DAY_EPOCH}))
    assert is_complete(primary)
    assert is_complete(primary, expected_size=10, created_epoch=DAY_EPOCH + 1.5)
    assert not is_complete(primary, expected_size=11)
    assert not is_complete(primary, created_epoch=DAY_EPOCH + 60)
    # Same clip → the primary path, already staged.
    assert resolve_destination(root, "4812", DAY_EPOCH, "GX010001.MP4", size=10) == (primary, True)
    # A different recording → the collision sibling, not yet staged.
    alt, staged = resolve_destination(root, "4812", DAY_EPOCH + 60, "GX010001.MP4", size=10)
    assert staged is False
    assert alt.parent.parent == primary.parent
    assert day_dir_of(alt) == primary.parent
    assert camera_dir_of(alt) == root / "_camera-staging" / "4812"
    assert camera_dir_of(Path("/elsewhere/2026-10-24/GX010001.MP4")) is None
    under = Path("/r/_camera-staging/4812") / DAY / alt.parent.name / "GX010001.MP4"
    assert camera_dir_of(under) == Path("/r/_camera-staging/4812")
    assert day_dir_of(under) == Path("/r/_camera-staging/4812") / DAY


def test_capture_day_and_ledger_dir_see_through_the_collision_folder(tmp_path: Path) -> None:
    alt = tmp_path / "_camera-staging" / "4812" / DAY / "alt-5-6" / "GX010001.MP4"
    alt.parent.mkdir(parents=True)
    alt.write_bytes(b"x")
    assert _capture_day(alt) == DAY
    event = {"files": {"mp4": str(alt)}, "jump_dir": str(alt.parent)}
    assert _ledger_dir(event) == tmp_path / "_camera-staging" / "4812"
    # An event outside the staging tree keeps the pre-fix rule (one above jump_dir).
    outside = {
        "files": {"mp4": str(tmp_path / "x" / "y" / "G.MP4")},
        "jump_dir": str(tmp_path / "x" / "y"),
    }
    assert _ledger_dir(outside) == tmp_path / "x"


def test_s3_key_never_overwrites_the_first_recording(tmp_path: Path) -> None:
    day = tmp_path / "_camera-staging" / "4812" / DAY
    first = day / "GX010001.MP4"
    second = day / "alt-99-1" / "GX010001.MP4"
    second.parent.mkdir(parents=True)
    first.write_bytes(CARD1)
    second.write_bytes(CARD2)
    objects: dict[str, int] = {}

    def exists(key: str) -> int | None:
        return objects.get(key)

    k1 = raw_object_key(first, "4812", exists=exists)
    objects[k1] = len(CARD1)
    k2 = raw_object_key(second, "4812", exists=exists)
    assert k1 == f"raw/4812/{DAY}/GX010001.MP4"
    assert k2 != k1 and k2.startswith(f"raw/4812/{DAY}/") and k2.endswith("/GX010001.MP4")
    # A retried hand-off of the same recording maps to the same key.
    objects[k2] = len(CARD2)
    assert raw_object_key(second, "4812", exists=exists) == k2
    assert raw_object_key(first, "4812", exists=exists) == k1


# --------------------------------------------------------------------------- #
# Ledger + "safe to remove": one record per recording
# --------------------------------------------------------------------------- #


def test_ledger_keeps_one_record_per_recording(tmp_path: Path) -> None:
    record_uploaded(tmp_path, "GX010001.MP4", "raw/4812/d/GX010001.MP4", size=100, now=1.0)
    record_uploaded(tmp_path, "GX010001.MP4", "raw/4812/d/fp/GX010001.MP4", size=200, now=2.0)
    recs = confirmed(tmp_path)
    assert {r.size for r in recs.values()} == {100, 200}
    assert all(r.filename == "GX010001.MP4" for r in recs.values())
    assert "GX010001.MP4" in recs, "the first record keeps the legacy bare key"
    # A re-upload of the FIRST recording updates its own record, not the second's.
    record_uploaded(tmp_path, "GX010001.MP4", "raw/4812/d/GX010001.MP4", size=100, now=3.0)
    recs = confirmed(tmp_path)
    assert len(recs) == 2
    assert confirmed_for(recs, "GX010001.MP4", 100).at == 3.0
    assert confirmed_for(recs, "GX010001.MP4", 200).at == 2.0
    assert confirmed_for(recs, "GX010001.MP4", 300) is None


def test_second_card_is_owed_until_its_own_upload_is_confirmed(tmp_path: Path) -> None:
    """The banner must not go green on the strength of CARD 1's record."""
    record_uploaded(tmp_path, "GX010001.MP4", "raw/4812/d/GX010001.MP4", size=100, now=1.0)
    card2 = {"GX010001.MP4": 200}
    state = card_transfer(tmp_path, card2, cleanup=True, min_age_s=0.0, now=10.0)
    assert state.unconfirmed == ("GX010001.MP4",)
    assert not state.settled
    assert deletable(tmp_path, card2, min_age_s=0.0, now=10.0) == []

    record_uploaded(tmp_path, "GX010001.MP4", "raw/4812/d/fp/GX010001.MP4", size=200, now=5.0)
    state = card_transfer(tmp_path, card2, cleanup=True, min_age_s=0.0, now=10.0)
    assert state.unconfirmed == () and state.awaiting_sweep == ("GX010001.MP4",)
    [rec] = deletable(tmp_path, card2, min_age_s=0.0, now=10.0)
    assert rec.size == 200 and rec.s3_key.endswith("/fp/GX010001.MP4")
    # Card 1, re-inserted later, is still cleanable on ITS record.
    [rec1] = deletable(tmp_path, {"GX010001.MP4": 100}, min_age_s=0.0, now=10.0)
    assert rec1.size == 100


def test_sweep_through_the_real_pull_deletes_only_the_confirmed_recording(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    sink = _Sink()
    _pull(_Card({"GX010001.MP4": CARD1}), root, sink)
    ledger = camera_dir(root, "4812")
    record_uploaded(ledger, "GX010001.MP4", "raw/4812/d/GX010001.MP4", size=len(CARD1), now=1.0)

    # Card 2 inserted: its GX010001 is a different recording — the sweep must keep it.
    card2 = _Card({"GX010001.MP4": CARD2}, created=DAY_EPOCH + 3600)
    _pull(card2, root, sink, cleanup=True, cleanup_min_age_s=0.0)
    assert card2.deleted == []
    assert "GX010001.MP4" in card2.files

    # Card 1 re-inserted: its recording IS confirmed → swept.
    card1 = _Card({"GX010001.MP4": CARD1})
    _pull(card1, root, sink, cleanup=True, cleanup_min_age_s=0.0)
    assert card1.deleted == ["GX010001.MP4"]
