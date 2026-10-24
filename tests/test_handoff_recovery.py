"""Hand-offs survive a restart, and the banner tells the truth about them.

The upload/notify queue is in memory. A restart between a clip's staging and its ledger
record used to lose the event for good (the next pull saw the clip ``is_complete`` and
emitted nothing). ``ingest.discovery.stranded_handoffs`` rebuilds those events from the
manifests on disk at start-up; a SkydiveOS rejection is recorded durably; and the card
status registry learns per-clip verdicts so a refused clip reads ``needs_attention``
instead of "uploading" forever. No real card, S3 or SkydiveOS is touched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ingest.cardstatus import (
    STATE_NEEDS_ATTENTION,
    STATE_PULLING,
    STATE_SAFE_TO_REMOVE,
    STATE_UPLOADING,
    STATE_VERIFYING,
    CardStatusRegistry,
)
from ingest.discovery import CameraDiscoveryService, stranded_handoffs
from ingest.registry import CameraRecord
from ingest.retention import confirmed, record_uploaded, rejected
from ingest.scanner import StaticCameraScanner
from tests.test_discovery import FakeRegistry, _wait_for

DAY = "2026-10-24"
DAY_EPOCH = 1_792_800_000.0


def _status_error(status: int) -> Exception:
    err = RuntimeError(f"HTTP {status}")
    err.response = type("R", (), {"status_code": status})()  # type: ignore[attr-defined]
    return err


def _stage(root: Path, camera_id: str, name: str, body: bytes, *, day: str = DAY,
           sub: str | None = None, attempts: int | None = None) -> Path:
    day_dir = root / "_camera-staging" / camera_id / day
    folder = day_dir / sub if sub else day_dir
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / name
    clip.write_bytes(body)
    manifest = {
        "event": "ready_for_processing", "job_id": f"{camera_id}-{clip.stem}",
        "camera_id": camera_id, "jump_dir": str(folder),
        "files": {"mp4": str(clip), "lrv": None, "thumbnail": None},
        "created_epoch": DAY_EPOCH, "emitted_at": 1.0,
        "source": {"transport": "sdcard", "card_serial": "1A2B-3C4D"},
    }
    if attempts is not None:
        manifest["_handoff_attempts"] = attempts
    clip.with_suffix(".ingest.json").write_text(json.dumps(manifest))
    return clip


def _now() -> float:
    return DAY_EPOCH + 3600.0


# --------------------------------------------------------------------------- #
# stranded_handoffs: what a restart finds
# --------------------------------------------------------------------------- #


def test_unconfirmed_staged_clip_is_rebuilt_as_an_event(tmp_path: Path) -> None:
    clip = _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50, attempts=3)
    [event] = stranded_handoffs(tmp_path, now=_now)
    assert event["files"]["mp4"] == str(clip)
    assert event["camera_id"] == "4812"
    assert event["source"]["card_serial"] == "1A2B-3C4D"
    assert "_handoff_attempts" not in event and event["_reconciled"] is True


def test_confirmed_clip_is_left_alone(tmp_path: Path) -> None:
    clip = _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    record_uploaded(clip.parent.parent, "GX010001.MP4", "raw/k", size=50, now=1.0)
    assert stranded_handoffs(tmp_path, now=_now) == []


def test_record_of_a_different_recording_does_not_confirm_this_one(tmp_path: Path) -> None:
    clip = _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    record_uploaded(clip.parent.parent, "GX010001.MP4", "raw/k", size=999, now=1.0)
    assert len(stranded_handoffs(tmp_path, now=_now)) == 1


def test_legacy_record_without_size_counts_as_handed_off(tmp_path: Path) -> None:
    """A pre-size-field ledger row is proof of a past hand-off — do not re-upload."""
    clip = _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    record_uploaded(clip.parent.parent, "GX010001.MP4", "raw/k", now=1.0)  # size=None
    assert stranded_handoffs(tmp_path, now=_now) == []


def test_collision_sibling_and_missing_or_corrupt_entries(tmp_path: Path) -> None:
    _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    alt = _stage(tmp_path, "4812", "GX010001.MP4", b"b" * 60, sub="alt-60-1")
    gone = _stage(tmp_path, "4812", "GX010002.MP4", b"c" * 5)
    gone.unlink()  # manifest without its file: nothing to hand off
    (tmp_path / "_camera-staging" / "4812" / DAY / "GX010003.ingest.json").write_text("{not json")
    events = stranded_handoffs(tmp_path, now=_now)
    assert sorted(e["files"]["mp4"] for e in events) == sorted(
        [str(tmp_path / "_camera-staging" / "4812" / DAY / "GX010001.MP4"), str(alt)]
    )


def test_lookback_bounds_the_reconciliation(tmp_path: Path) -> None:
    _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50, day="2026-01-01")
    _stage(tmp_path, "4812", "GX010002.MP4", b"a" * 50, day=DAY)
    recent = stranded_handoffs(tmp_path, max_age_days=7.0, now=_now)
    assert [Path(e["files"]["mp4"]).name for e in recent] == ["GX010002.MP4"]
    everything = stranded_handoffs(tmp_path, max_age_days=None, now=_now)
    assert len(everything) == 2


def test_no_staging_tree_is_fine(tmp_path: Path) -> None:
    assert stranded_handoffs(tmp_path / "nowhere", now=_now) == []


# --------------------------------------------------------------------------- #
# The service: restart → recovery, exactly once
# --------------------------------------------------------------------------- #


class _Uploader:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, mp4_path, camera_id, instructor_id, camera_role=None, **_kw):
        self.calls.append(mp4_path)
        if self.fail is not None:
            raise self.fail
        return f"raw/{camera_id}/{DAY}/{Path(mp4_path).name}"


async def _no_pull(camera_id: str, *, emitter=None) -> list:
    return []  # the card is already fully staged: a real pull emits nothing


def _service(root: Path, upload, **kw) -> CameraDiscoveryService:
    return CameraDiscoveryService(
        scanner=StaticCameraScanner([]),
        registry=FakeRegistry([CameraRecord(camera_id="4812", paired_at=1.0)]),
        upload=upload,
        pull=_no_pull,
        interval=999.0,
        handoff_retry_delay=0.01,
        handoff_max_attempts=2,
        staging_root=root,
        **kw,
    )


def test_restart_recovers_a_stranded_clip_once(tmp_path: Path) -> None:
    clip = _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    uploads = _Uploader()
    service = _service(tmp_path, uploads)

    async def scenario() -> None:
        await service.start()
        assert await _wait_for(lambda: len(uploads.calls) == 1)
        await asyncio.sleep(0.1)
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls == [str(clip)]
    assert confirmed(clip.parent.parent)["GX010001.MP4"].size == 50

    # A SECOND restart finds the ledger record and re-queues nothing.
    again = _Uploader()
    service2 = _service(tmp_path, again)

    async def scenario2() -> None:
        await service2.start()
        await asyncio.sleep(0.2)
        await service2.stop()

    asyncio.run(scenario2())
    assert again.calls == []


def test_without_a_staging_root_nothing_is_reconciled(tmp_path: Path) -> None:
    """The rollback switch (HANDOFF_RECONCILE_ON_START=0) → staging_root=None."""
    _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    uploads = _Uploader()
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner([]),
        registry=FakeRegistry([]),
        upload=uploads,
        pull=_no_pull,
        interval=999.0,
    )

    async def scenario() -> None:
        await service.start()
        await asyncio.sleep(0.2)
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls == []


def test_rejection_is_recorded_durably_and_reported(tmp_path: Path) -> None:
    clip = _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    verdicts: list[tuple[str, str, str, str | None]] = []
    uploads = _Uploader(fail=_status_error(404))
    service = _service(tmp_path, uploads, handoff_observer=lambda *a: verdicts.append(a))

    async def scenario() -> None:
        await service.start()
        assert await _wait_for(lambda: bool(verdicts))
        await asyncio.sleep(0.1)
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls == [str(clip)]  # a 4xx costs exactly one attempt
    [(cam, name, outcome, detail)] = verdicts
    assert (cam, name, outcome) == ("4812", "GX010001.MP4", "rejected")
    assert "404" in (detail or "")
    [rec] = rejected(clip.parent.parent)
    assert (rec.filename, rec.size) == ("GX010001.MP4", 50) and "404" in rec.reason
    assert confirmed(clip.parent.parent) == {}  # never confirmed, never deletable

    # A later successful hand-off clears the rejection.
    ok = _Uploader()
    verdicts.clear()
    service2 = _service(tmp_path, ok, handoff_observer=lambda *a: verdicts.append(a))

    async def scenario2() -> None:
        await service2.start()
        assert await _wait_for(lambda: bool(ok.calls))
        await asyncio.sleep(0.1)
        await service2.stop()

    asyncio.run(scenario2())
    assert rejected(clip.parent.parent) == []
    assert verdicts[-1][2] == "confirmed"


def test_giving_up_is_reported_as_failed_and_retrying_in_between(tmp_path: Path) -> None:
    _stage(tmp_path, "4812", "GX010001.MP4", b"a" * 50)
    verdicts: list[str] = []
    uploads = _Uploader(fail=_status_error(503))
    service = _service(tmp_path, uploads, handoff_observer=lambda c, n, o, d: verdicts.append(o))

    async def scenario() -> None:
        await service.start()
        assert await _wait_for(lambda: "failed" in verdicts)
        await service.stop()

    asyncio.run(scenario())
    assert verdicts == ["retrying", "failed"]
    assert len(uploads.calls) == 2


# --------------------------------------------------------------------------- #
# The registry: verdicts and the new states
# --------------------------------------------------------------------------- #


def test_registry_tracks_verdicts_and_never_greens_over_a_refused_clip() -> None:
    reg = CardStatusRegistry(now=lambda: 100.0)
    reg.pull_started("4812")
    reg.uploading("4812", 2, unconfirmed=2, awaiting_sweep=0)
    reg.handoff_result("4812", "GX010001.MP4", "confirmed", "raw/k")
    reg.handoff_result("4812", "GX010002.MP4", "rejected", "HTTP 404")
    [row] = reg.snapshot()
    assert row["uploaded_files"] == 1
    assert row["rejected_files"] == ["GX010002.MP4"]
    assert row["issues"] == {"GX010002.MP4": "refused by SkydiveOS: HTTP 404"}
    assert reg.issue_files("4812") == {"GX010002.MP4"}

    reg.needs_attention("4812", "1 clip(s) could not be handed off", 1)
    [row] = reg.snapshot()
    assert row["state"] == STATE_NEEDS_ATTENTION and row["pending_files"] == 1
    # A re-pull tick does not flap it back to pulling, and keeps the verdicts.
    reg.pull_started("4812")
    [row] = reg.snapshot()
    assert row["state"] == STATE_NEEDS_ATTENTION
    assert row["rejected_files"] == ["GX010002.MP4"] and row["uploaded_files"] == 1
    # Once the refused clip is confirmed, the issue is gone and the card can settle.
    reg.handoff_result("4812", "GX010002.MP4", "confirmed", "raw/k2")
    assert reg.issue_files("4812") == set()
    reg.safe_to_remove("4812")
    [row] = reg.snapshot()
    assert row["state"] == STATE_SAFE_TO_REMOVE and row["pending_files"] == 0


def test_a_verdict_for_a_card_not_in_the_reader_creates_no_row() -> None:
    """Regression (South Shore, 2026-10-24): the start-up reconciliation re-queued 36
    clips from six-day-old staging days; their verdicts must not put phantom 'pulling'
    rows for cameras 4368/4812 on the operator banner while the reader is empty."""
    reg = CardStatusRegistry(now=lambda: 100.0)
    reg.handoff_result("4812", "GX010989.MP4", "rejected", "HTTP 404")
    reg.handoff_result("4368", "GX013279.MP4", "confirmed", "raw/k")
    assert reg.snapshot() == []
    # A card that IS tracked still gets its verdicts.
    reg.pull_started("9999")
    reg.handoff_result("9999", "GX010001.MP4", "rejected", "HTTP 404")
    assert reg.snapshot()[0]["rejected_files"] == ["GX010001.MP4"]


def test_verifying_is_a_transient_state_that_never_hides_an_error() -> None:
    reg = CardStatusRegistry(now=lambda: 100.0)
    reg.pull_started("4812")
    reg.verifying("4812")
    assert reg.snapshot()[0]["state"] == STATE_VERIFYING
    reg.error("4812", "card yanked")
    reg.verifying("4812")
    assert reg.snapshot()[0]["state"] == "error"
    reg2 = CardStatusRegistry(now=lambda: 100.0)
    reg2.pull_started("1")
    assert reg2.snapshot()[0]["state"] == STATE_PULLING
    reg2.uploading("1", 3, unconfirmed=1, awaiting_sweep=2)
    row = reg2.snapshot()[0]
    assert (row["state"], row["unconfirmed_files"], row["awaiting_sweep_files"]) == (
        STATE_UPLOADING, 1, 2,
    )


def test_needs_attention_lingers_after_removal_like_an_error() -> None:
    clock = {"t": 100.0}
    reg = CardStatusRegistry(now=lambda: clock["t"])
    reg.pull_started("4812")
    reg.needs_attention("4812", "refused", 1)
    reg.observe([])  # card taken out: the warning must stay visible for a while
    assert [r["state"] for r in reg.snapshot()] == [STATE_NEEDS_ATTENTION]
    clock["t"] += 10_000.0
    assert reg.snapshot() == []


# --------------------------------------------------------------------------- #
# The API's end-of-pull verdict
# --------------------------------------------------------------------------- #


def _settings(tmp_path: Path, **over):
    import dataclasses

    from api.config import get_settings

    return dataclasses.replace(
        get_settings(),
        camera_scanner="sdcard",
        sdcard_mount_roots=(str(tmp_path / "reader"),),
        delete_after_transfer=True,
        delete_after_transfer_min_age_h=0.0,
        **over,
    )


def _patch_card(monkeypatch, files: dict[str, int] | Exception) -> None:
    import ingest.sdcard as sdcard
    from ingest.camera import RemoteMedia

    if isinstance(files, Exception):
        def _mount_for(camera_id, roots):
            raise files
        monkeypatch.setattr(sdcard, "mount_for", _mount_for)
        return
    monkeypatch.setattr(sdcard, "mount_for", lambda camera_id, roots: Path("/mnt/fake"))

    async def _list(self):
        return [
            RemoteMedia(camera_path=f"100GOPRO/{n}", created_epoch=DAY_EPOCH, size=s, has_lrv=False)
            for n, s in files.items()
        ]

    monkeypatch.setattr(sdcard.SdCardCamera, "list_videos", _list)


def test_report_card_state_distinguishes_uploading_refused_and_safe(
    tmp_path: Path, monkeypatch,
) -> None:
    import os

    from api.app import _report_card_state

    root = Path(os.environ["RAW_STORAGE_ROOT"])  # pinned by conftest
    ledger = root / "_camera-staging" / "4812"
    reg = CardStatusRegistry(now=lambda: 100.0)
    reg.pull_started("4812")
    settings = _settings(tmp_path)

    # Two clips on the card, nothing confirmed: uploading, with the breakdown.
    _patch_card(monkeypatch, {"GX010001.MP4": 10, "GX010002.MP4": 20})
    asyncio.run(_report_card_state("4812", settings, reg))
    [row] = reg.snapshot()
    assert (row["state"], row["pending_files"], row["unconfirmed_files"]) == (STATE_UPLOADING, 2, 2)

    # One confirmed + swept away, the other refused by SkydiveOS: NOT green.
    reg.handoff_result("4812", "GX010002.MP4", "rejected", "HTTP 404")
    _patch_card(monkeypatch, {"GX010002.MP4": 20})
    asyncio.run(_report_card_state("4812", settings, reg))
    [row] = reg.snapshot()
    assert row["state"] == STATE_NEEDS_ATTENTION
    assert "GX010002.MP4" in (row["error"] or "")
    assert row["pending_files"] == 1

    # Still uploading when a NON-refused clip is also pending.
    _patch_card(monkeypatch, {"GX010002.MP4": 20, "GX010003.MP4": 30})
    asyncio.run(_report_card_state("4812", settings, reg))
    assert reg.snapshot()[0]["state"] == STATE_UPLOADING

    # Everything confirmed and swept: safe.
    record_uploaded(ledger, "GX010002.MP4", "raw/k", size=20, now=1.0)
    _patch_card(monkeypatch, {})
    asyncio.run(_report_card_state("4812", settings, reg))
    assert reg.snapshot()[0]["state"] == STATE_SAFE_TO_REMOVE


def test_report_card_state_unverifiable_is_not_green_but_a_gone_card_is_released(
    tmp_path: Path, monkeypatch,
) -> None:
    from api.app import _report_card_state
    from ingest.camera import CameraError

    settings = _settings(tmp_path)
    reg = CardStatusRegistry(now=lambda: 100.0)
    reg.pull_started("4812")
    _patch_card(monkeypatch, CameraError("SD card 4812 is no longer mounted"))
    asyncio.run(_report_card_state("4812", settings, reg))
    assert reg.snapshot()[0]["state"] == STATE_SAFE_TO_REMOVE

    reg.pull_started("4812")
    import ingest.sdcard as sdcard

    monkeypatch.setattr(sdcard, "mount_for", lambda camera_id, roots: Path("/mnt/fake"))

    async def _boom(self):
        raise OSError("EIO reading the card")

    monkeypatch.setattr(sdcard.SdCardCamera, "list_videos", _boom)
    asyncio.run(_report_card_state("4812", settings, reg))
    [row] = reg.snapshot()
    assert row["state"] == STATE_NEEDS_ATTENTION and "verify" in (row["error"] or "")

    # The opt-out keeps the old unconditional banner.
    reg.pull_started("4812")
    relaxed = _settings(tmp_path, card_safe_requires_upload=False)
    asyncio.run(_report_card_state("4812", relaxed, reg))
    assert reg.snapshot()[0]["state"] == STATE_SAFE_TO_REMOVE
