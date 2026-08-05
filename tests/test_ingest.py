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
import os
from pathlib import Path
from typing import Any

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
    # The card mirror lives under the reserved _camera-staging/ prefix, keeping the
    # root's top level for the {date}/{instructor}/{customer} jump archive.
    root = Path("/tmp/raw-storage")
    dest = storage.destination(root, "1234", CREATED, "GX010123.MP4")
    assert dest == root / "_camera-staging" / "1234" / "2024-05-29" / "GX010123.MP4"
    assert storage.camera_staging_root(root) == root / "_camera-staging"


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

    day_dir = tmp_path / storage.CAMERA_STAGING_DIRNAME / "1234" / "2024-05-29"
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


# --------------------------------------------------------------------------- #
# Camera-id normalization (full serial -> trailing BLE digits)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C3504224544313", "4313"),  # full GoPro serial -> advertised digits
        ("4313", "4313"),  # already the trailing-digit id
        ("  4313  ", "4313"),
        ("TESTGOPRO001", "TESTGOPRO001"),  # test serial: <4 trailing digits, untouched
        (None, None),
    ],
)
def test_normalize_camera_id(raw: str | None, expected: str | None) -> None:
    from ingest.pull import _normalize_camera_id

    assert _normalize_camera_id(raw) == expected


def test_force_us_english_locale_sets_lang(monkeypatch):
    """The SDK's WiFi driver reads os.environ['LANG'] and refuses anything but en_US.

    On the French client Mac (fr_CA) — and under launchd, which sets no LANG at all —
    every manual pull failed with what looked like a camera fault.
    """
    from ingest.camera import _force_us_english_locale

    monkeypatch.setenv("LANG", "fr_CA.UTF-8")
    _force_us_english_locale()
    assert os.environ["LANG"].startswith("en_US")

    monkeypatch.delenv("LANG", raising=False)
    _force_us_english_locale()
    assert os.environ["LANG"].startswith("en_US")


def test_force_us_english_locale_keeps_an_existing_en_us(monkeypatch):
    from ingest.camera import _force_us_english_locale

    monkeypatch.setenv("LANG", "en_US.ISO8859-1")
    _force_us_english_locale()
    assert os.environ["LANG"] == "en_US.ISO8859-1"


# --------------------------------------------------------------------------- #
# SDK correction: never re-pair a camera BlueZ has already bonded.
#
# The SDK's own "am I already paired?" check parses `bluetoothctl devices Paired` by
# waiting for a `#` delimiter and comparing line.split()[1] to the address. On
# bluetoothctl 5.85 the prompt is `[GoPro 9362]>` and lines carry ANSI colour codes, so
# it never matches — the SDK then pairs an already-bonded camera, BlueZ answers
# ConnectionAttemptFailed, and every pull after the first one fails. These tests pin the
# correction; the SDK is stubbed so they run with or without the hardware SDK installed.
# --------------------------------------------------------------------------- #

import subprocess as _subprocess  # noqa: E402
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402

from ingest.camera import _ble_bond_exists, _skip_redundant_ble_pairing  # noqa: E402


class _Handle:
    def __init__(self, address: str) -> None:
        self.address = address


def _stub_sdk_pair_module(monkeypatch: pytest.MonkeyPatch) -> tuple[type, list[str]]:
    """Install a stub ``bleak_wrapper`` whose pair() records the addresses it pairs."""
    paired: list[str] = []

    class StubController:
        async def pair(self, handle: object) -> None:
            paired.append(getattr(handle, "address", ""))

    module = _types.ModuleType("open_gopro.network.ble.adapters.bleak_wrapper")
    module.BleakWrapperController = StubController  # type: ignore[attr-defined]
    monkeypatch.setitem(
        _sys.modules, "open_gopro.network.ble.adapters.bleak_wrapper", module
    )
    return StubController, paired


def _fake_bluetoothctl(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    def _run(*_a: object, **_k: object) -> object:
        return _subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("ingest.camera.subprocess.run", _run)


@pytest.mark.parametrize(
    ("info_output", "expected"),
    [
        ("\tPaired: yes\n\tBonded: yes\n", True),
        ("\tPaired: no\n\tBonded: no\n", False),
        ("\tBonded: yes\n", True),                      # bonded is enough
        ("Device DA:BC:C7:6F:33:33 not available\n", False),
        ("", False),
    ],
)
def test_ble_bond_exists_reads_bluetoothctl(
    monkeypatch: pytest.MonkeyPatch, info_output: str, expected: bool
) -> None:
    _fake_bluetoothctl(monkeypatch, info_output)
    assert _ble_bond_exists("DA:BC:C7:6F:33:33") is expected


def test_ble_bond_exists_survives_a_missing_or_hung_bluetoothctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bluetoothctl → report "no bond" and let the SDK try, rather than raising."""
    for boom in (FileNotFoundError("no bluetoothctl"), _subprocess.TimeoutExpired("x", 10)):
        def _run(*_a: object, _e: BaseException = boom, **_k: object) -> object:
            raise _e

        monkeypatch.setattr("ingest.camera.subprocess.run", _run)
        assert _ble_bond_exists("DA:BC:C7:6F:33:33") is False


def test_pairing_is_skipped_when_a_bond_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, paired = _stub_sdk_pair_module(monkeypatch)
    _fake_bluetoothctl(monkeypatch, "\tPaired: yes\n\tConnected: yes\n")

    _skip_redundant_ble_pairing()
    asyncio.run(controller().pair(_Handle("DA:BC:C7:6F:33:33")))

    assert paired == []  # the SDK's pairing dance never ran


def test_a_new_camera_still_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    controller, paired = _stub_sdk_pair_module(monkeypatch)
    _fake_bluetoothctl(monkeypatch, "\tPaired: no\n")

    _skip_redundant_ble_pairing()
    asyncio.run(controller().pair(_Handle("AA:BB:CC:DD:EE:FF")))

    assert paired == ["AA:BB:CC:DD:EE:FF"]  # first-time pairing is untouched


def test_patching_twice_does_not_stack_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_load_sdk() runs on every camera open; the patch must be idempotent."""
    controller, paired = _stub_sdk_pair_module(monkeypatch)
    _fake_bluetoothctl(monkeypatch, "\tPaired: no\n")

    _skip_redundant_ble_pairing()
    once = controller.pair
    _skip_redundant_ble_pairing()
    assert controller.pair is once

    asyncio.run(controller().pair(_Handle("AA:BB:CC:DD:EE:FF")))
    assert paired == ["AA:BB:CC:DD:EE:FF"]  # wrapped exactly once


def test_an_addressless_handle_falls_through_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to ask BlueZ about → don't silently skip pairing."""
    controller, paired = _stub_sdk_pair_module(monkeypatch)
    _fake_bluetoothctl(monkeypatch, "\tPaired: yes\n")

    _skip_redundant_ble_pairing()
    asyncio.run(controller().pair(_Handle("")))

    assert paired == [""]


# --------------------------------------------------------------------------- #
# A cosmetic asset must never cost the pull.
#
# Observed on a real card: GX015312.MP4 had no thumbnail, the camera answered 404,
# requests raised HTTPError — which is NOT a CameraError, so it sailed past the
# best-effort handler and aborted a pull that had already staged nine clips. The
# masters are the product; a missing JPEG or proxy is not a failed jump.
# --------------------------------------------------------------------------- #


class _AssetFailingCamera(FakeCamera):
    """A card that serves masters fine but fails a cosmetic asset with `boom`."""

    def __init__(self, *, boom: BaseException, fail: str) -> None:
        super().__init__([_media("GX010123.MP4")])
        self._boom = boom
        self._fail = fail

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        if self._fail == "lrv":
            raise self._boom
        return await super().download_lrv(media, dest)

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        if self._fail == "thumb":
            raise self._boom
        return await super().download_thumbnail(media, dest)


@pytest.mark.parametrize("fail", ["thumb", "lrv"])
@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("404 Client Error: Not Found"),  # what requests actually raised
        OSError("No space left on device"),
        TimeoutError("read timed out"),
    ],
    ids=["http-404", "disk-full", "timeout"],
)
def test_a_failed_cosmetic_asset_still_stages_the_master(
    tmp_path: Path, fail: str, boom: BaseException
) -> None:
    cam = _AssetFailingCamera(boom=boom, fail=fail)
    jumps = asyncio.run(
        pull_camera("1234", root=tmp_path, camera=cam, emit=False, now=lambda: FIXED_NOW)
    )

    assert len(jumps) == 1, "the pull must complete, not abort"
    jump = jumps[0]
    assert jump.mp4_path.is_file(), "the master is the product — it must be on disk"
    # The manifest is what marks a jump complete/resumable, so it must still be written.
    assert storage.manifest_path(jump.mp4_path).is_file()
    if fail == "thumb":
        assert jump.thumbnail_path is None
    else:
        assert jump.lrv_path is None


def test_a_failed_master_download_still_aborts(tmp_path: Path) -> None:
    """The inverse: the MP4 is NOT cosmetic, so its failure must not be swallowed."""

    class _MasterFails(FakeCamera):
        async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
            raise RuntimeError("connection reset mid-master")

    cam = _MasterFails([_media("GX010123.MP4")])
    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(
            pull_camera("1234", root=tmp_path, camera=cam, emit=False, now=lambda: FIXED_NOW)
        )


# --------------------------------------------------------------------------- #
# SDK correction: the "camera is ready" gate must have a deadline.
#
# The gate is correct in principle (never transfer a file the camera is still writing),
# but as shipped it can never end: the acquire has no timeout, and _ready_lock is built
# once *outside* open()'s retry loop while _open_ble acquires it on every attempt — so
# attempt 2 waits on a lock whose only releaser (a BUSY/ENCODING notification) no longer
# has a subscriber. Observed on a HERO12: the pull sat on that line for minutes with BLE
# up. These tests pin both corrections against a faithful stand-in for the SDK's
# _ReadyLock, so they run with or without the hardware SDK installed.
# --------------------------------------------------------------------------- #

import enum as _enum  # noqa: E402

from ingest.camera import (  # noqa: E402
    _bound_camera_ready_wait,
    _ready_timeout_s,
)


def _stub_ready_lock(monkeypatch: pytest.MonkeyPatch) -> type:
    """Install a stub ``gopro_wireless`` module exposing the SDK's ``_ReadyLock`` shape.

    Mirrors the real class (``asyncio.Lock`` + an ``owner`` tag, released only by
    whoever observes the camera going idle) so the patch is exercised against the
    same semantics it corrects.
    """

    class StubReadyLock:
        class _LockOwner(_enum.Enum):
            RULE_ENFORCER = _enum.auto()
            STATE_MANAGER = _enum.auto()

        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.owner: object | None = None

        async def __aenter__(self) -> StubReadyLock:
            await self.lock.acquire()
            return self

        async def __aexit__(self, *_exc: object) -> None:
            self.release()

        async def acquire(self, owner: object) -> None:
            await self.lock.acquire()
            self.owner = owner

        def release(self) -> None:
            if self.lock.locked():
                self.lock.release()
                self.owner = None

    module = _types.ModuleType("open_gopro.gopro_wireless")
    module._ReadyLock = StubReadyLock  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "open_gopro.gopro_wireless", module)
    return StubReadyLock


def _patched_lock(monkeypatch: pytest.MonkeyPatch, *, timeout: str = "0.05") -> Any:
    """A patched lock instance with a short deadline, so tests stay fast."""
    monkeypatch.setenv("GOPRO_READY_TIMEOUT_S", timeout)
    cls = _stub_ready_lock(monkeypatch)
    _bound_camera_ready_wait()
    return cls()


def test_a_ready_camera_is_not_made_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The healthy path is untouched: a free lock is acquired, and stays held."""
    lock = _patched_lock(monkeypatch)
    state_manager = type(lock)._LockOwner.STATE_MANAGER

    asyncio.run(lock.acquire(state_manager))

    assert lock.lock.locked()
    assert lock.owner is state_manager


def test_a_lock_leaked_by_an_earlier_attempt_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadlock: open() retries, but close() never released the state manager's lock.

    Nothing can release it (the status observables are gone), so without the reset this
    waits forever — here, with a 50 ms deadline, it would raise instead.
    """
    lock = _patched_lock(monkeypatch)
    state_manager = type(lock)._LockOwner.STATE_MANAGER

    async def _first_then_second_attempt() -> None:
        await lock.acquire(state_manager)  # attempt 1 parks the lock and dies
        await lock.acquire(state_manager)  # attempt 2 must not hang on it

    asyncio.run(_first_then_second_attempt())

    assert lock.lock.locked(), "the retry still ends up holding the gate"
    assert lock.owner is state_manager


def test_a_busy_camera_fails_with_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held by someone else and never released == the camera never goes idle."""
    lock = _patched_lock(monkeypatch)
    owners = type(lock)._LockOwner

    async def _wait_behind_another_owner() -> None:
        await lock.acquire(owners.RULE_ENFORCER)
        await lock.acquire(owners.STATE_MANAGER)

    with pytest.raises(CameraError) as excinfo:
        asyncio.run(_wait_behind_another_owner())

    message = str(excinfo.value)
    # The cause is on the device, so the error has to say what to do to the device.
    assert "never reported itself idle" in message
    assert "QuikCapture" in message
    assert "GOPRO_READY_TIMEOUT_S" in message


def test_the_rule_enforcers_context_manager_is_bounded_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounding only acquire() would just move the hang to the first real command.

    ``_enforce_message_rules`` takes the same gate via ``async with``, which calls
    ``__aenter__`` directly — so a held lock would hang ``get_media_list()`` instead of
    ``open()``.
    """
    lock = _patched_lock(monkeypatch)

    async def _wait_behind_a_held_lock() -> None:
        await lock.acquire(type(lock)._LockOwner.STATE_MANAGER)
        async with lock:
            pass

    with pytest.raises(CameraError, match="never reported itself idle"):
        asyncio.run(_wait_behind_a_held_lock())


def test_the_context_manager_still_passes_through_when_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _patched_lock(monkeypatch)

    async def _use_it() -> bool:
        async with lock as held:
            return held.lock.locked()

    assert asyncio.run(_use_it()) is True
    assert not lock.lock.locked(), "the context manager still releases on exit"


def test_patching_the_ready_wait_twice_does_not_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_load_sdk() runs on every camera open; the patch must be idempotent."""
    cls = _stub_ready_lock(monkeypatch)

    _bound_camera_ready_wait()
    once_acquire, once_aenter = cls.acquire, cls.__aenter__
    _bound_camera_ready_wait()

    assert cls.acquire is once_acquire
    assert cls.__aenter__ is once_aenter


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 60.0),
        ("120", 120.0),
        ("2.5", 2.5),
        ("0", 60.0),        # a zero deadline would fail every pull instantly
        ("-5", 60.0),
        ("soon", 60.0),     # a typo must not disable the pull
    ],
)
def test_ready_timeout_reads_the_env_and_falls_back_safely(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("GOPRO_READY_TIMEOUT_S", raw)
    assert _ready_timeout_s() == expected


def test_an_unknown_sdk_shape_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future SDK without ``_LockOwner`` must not be half-patched."""

    class Bare:
        async def acquire(self, owner: object) -> None:
            return None

    module = _types.ModuleType("open_gopro.gopro_wireless")
    module._ReadyLock = Bare  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "open_gopro.gopro_wireless", module)

    _bound_camera_ready_wait()

    assert Bare.acquire.__name__ == "acquire"


# --------------------------------------------------------------------------- #
# _retry_wifi_ap_wait: a slow WiFi AP must retry, not fail the pull
#
# WirelessGoPro._open_wifi() enables the camera's AP, polls StatusId.AP_MODE until it
# reads True, then joins — inside a retry loop whose `except ConnectFailed` branch
# re-disables the AP so the next attempt starts clean. Two shipped defects make that
# recovery unreachable: the AP-ready wait is hardcoded at 5 s (a HERO12 routinely needs
# longer), and it raises TimeoutError, which the loop does NOT catch — so the remaining
# retries never run and the AP is left half-enabled, which is exactly what makes the next
# attempt time out too. Observed as a pull failing identically run after run with AP_MODE
# polling False forever. These tests pin the correction against a stand-in for the SDK's
# shape, so they run with or without the hardware SDK installed.
# --------------------------------------------------------------------------- #

from ingest.camera import _ap_ready_timeout_s, _retry_wifi_ap_wait  # noqa: E402


class _StubConnectFailed(Exception):
    def __init__(self, connection: str, timeout: float, retries: int) -> None:
        super().__init__(f"{connection} failed after {retries} retries")


def _stub_exceptions_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install ``open_gopro.domain.exceptions`` exposing ``ConnectFailed``."""
    module = _types.ModuleType("open_gopro.domain.exceptions")
    module.ConnectFailed = _StubConnectFailed  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "open_gopro.domain.exceptions", module)


class _Resp:
    def __init__(self, data: object = None, ok: bool = True) -> None:
        self.data = data
        self.ok = ok


class _StubWireless:
    """A stand-in for ``WirelessGoPro`` whose AP needs ``ap_ready_after`` polls.

    Records every ``enable_wifi_ap`` call so a test can assert that the between-attempt
    reset — the half the uncaught TimeoutError skipped — actually fired.
    """

    #: The SDK guards ``_open_wifi`` with ``_ensure_opened((BLE,))``, which the patch
    #: preserves; the real class exposes this, so the stub must too.
    is_ble_connected = True

    def __init__(self, ap_ready_after: int = 0, wifi_open_fails: int = 0) -> None:
        self.ap_calls: list[bool] = []
        self.wifi_opened = 0
        self._polls = 0
        self._ap_ready_after = ap_ready_after
        self._wifi_open_fails = wifi_open_fails
        outer = self

        class _BleCommand:
            async def get_wifi_password(self) -> _Resp:
                return _Resp("pw")

            async def get_wifi_ssid(self) -> _Resp:
                return _Resp("ssid")

            async def enable_wifi_ap(self, *, enable: bool) -> _Resp:
                outer.ap_calls.append(enable)
                if enable:
                    outer._polls = 0  # a fresh AP-raise restarts the readiness count
                return _Resp(ok=True)

        class _ApMode:
            async def get_value(self) -> _Resp:
                outer._polls += 1
                return _Resp(outer._polls > outer._ap_ready_after)

        class _BleStatus:
            ap_mode = _ApMode()

        class _Wifi:
            async def open(self, ssid: str, password: str, timeout: int, retries: int) -> None:
                if outer.wifi_opened < outer._wifi_open_fails:
                    outer.wifi_opened += 1
                    raise _StubConnectFailed("Wifi", timeout, retries)
                outer.wifi_opened += 1

        self.ble_command = _BleCommand()
        self.ble_status = _BleStatus()
        self._wifi = _Wifi()

    async def _open_wifi(self, timeout: int = 30, retries: int = 5) -> None:
        """The SDK's shipped body, verbatim — what the patch replaces."""
        for _retry in range(1, retries):
            try:
                assert (await self.ble_command.enable_wifi_ap(enable=True)).ok

                async def _wait_for_camera_wifi_ready() -> None:
                    while not (await self.ble_status.ap_mode.get_value()).data:
                        await asyncio.sleep(0.001)

                await asyncio.wait_for(_wait_for_camera_wifi_ready(), 5)
                await self._wifi.open("ssid", "pw", timeout, 1)
                break
            except _StubConnectFailed:
                assert (await self.ble_command.enable_wifi_ap(enable=False)).ok
        else:
            raise _StubConnectFailed("Wifi Connection failed", timeout, retries)


def _patched_wireless(
    monkeypatch: pytest.MonkeyPatch, *, timeout: str = "0.5", **kwargs: Any
) -> _StubWireless:
    _stub_exceptions_module(monkeypatch)
    monkeypatch.setenv("GOPRO_AP_READY_TIMEOUT_S", timeout)
    cls = type("_Patchable", (_StubWireless,), {})
    _retry_wifi_ap_wait(cls)
    return cls(**kwargs)


def test_the_unpatched_sdk_body_abandons_the_pull_on_a_slow_ap() -> None:
    """Reproduce the defect: TimeoutError escapes, so no retry and no AP reset."""
    cam = _StubWireless(ap_ready_after=10**9)  # an AP that never comes up

    async def go() -> None:
        with pytest.raises(TimeoutError):
            await _StubWireless._open_wifi(cam)

    asyncio.run(asyncio.wait_for(go(), 10))
    assert cam.ap_calls == [True], "the shipped body never ran its enable_wifi_ap(False) reset"


def test_a_slow_ap_now_retries_and_resets_between_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AP reset must fire between attempts — a half-enabled AP poisons the next one."""
    cam = _patched_wireless(monkeypatch, ap_ready_after=10**9)

    with pytest.raises(_StubConnectFailed):
        asyncio.run(cam._open_wifi())

    assert False in cam.ap_calls, "the AP was never disabled between attempts"
    assert cam.ap_calls.count(True) > 1, "the timeout did not lead to a retry"


def test_an_ap_slower_than_the_sdks_5s_still_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the deadline is ours, so a slow-but-healthy AP connects.

    The AP needs more polls (at the SDK's 200 ms cadence) than the hardcoded 5 s allowed
    for, and the connection succeeds on the first attempt — no retry, no reset.
    """
    cam = _patched_wireless(monkeypatch, timeout="2.0", ap_ready_after=3)

    asyncio.run(cam._open_wifi())

    assert cam.wifi_opened == 1
    assert cam.ap_calls == [True], "a healthy AP must not be reset"


def test_a_connect_failure_still_retries_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK's own ConnectFailed path must keep working unchanged."""
    cam = _patched_wireless(monkeypatch, wifi_open_fails=1)

    asyncio.run(cam._open_wifi())

    assert cam.wifi_opened == 2, "expected one failed join then a successful retry"
    assert False in cam.ap_calls


def test_a_failing_ap_reset_does_not_mask_the_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort recovery: a reset that itself fails must not replace the real error."""
    cam = _patched_wireless(monkeypatch, ap_ready_after=10**9)

    async def _boom(*, enable: bool) -> _Resp:
        cam.ap_calls.append(enable)
        if not enable:
            raise RuntimeError("BLE went away")
        return _Resp(ok=True)

    monkeypatch.setattr(cam.ble_command, "enable_wifi_ap", _boom)

    with pytest.raises(_StubConnectFailed):
        asyncio.run(cam._open_wifi())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 30.0), ("45", 45.0), ("0", 30.0), ("-5", 30.0), ("banana", 30.0)],
)
def test_the_ap_deadline_falls_back_rather_than_failing_every_pull(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("GOPRO_AP_READY_TIMEOUT_S", raw)
    assert _ap_ready_timeout_s() == expected


def test_patching_twice_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """_load_sdk() runs on every connect; the patch must not stack."""
    _stub_exceptions_module(monkeypatch)
    cls = type("_Patchable", (_StubWireless,), {})
    _retry_wifi_ap_wait(cls)
    # Compare the class slot, not `cls._open_wifi`: the SDK's guard is a wrapt decorator
    # that hands back a fresh bound wrapper on every attribute access.
    first = cls.__dict__["_open_wifi"]
    _retry_wifi_ap_wait(cls)

    assert cls.__dict__["_open_wifi"] is first


def test_an_sdk_without_open_wifi_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future SDK shape must not be half-patched."""
    _stub_exceptions_module(monkeypatch)

    class Bare:
        pass

    _retry_wifi_ap_wait(Bare)

    assert not hasattr(Bare, "_open_wifi")


# --------------------------------------------------------------------------- #
# SDK correction: a stale NetworkManager profile must not fail the WiFi join.
#
# On Linux the SDK joins the camera's AP with `nmcli dev wifi connect`. When a profile
# for that SSID already exists (left behind by ANY previous pull), NetworkManager
# updates it instead of creating a fresh one — with an incomplete security section
# that NM rejects: `Error: 802-11-wireless-security.key-mgmt: property is missing.`
# So the first pull works and every later one fails until an operator hand-runs
# `nmcli con delete GP<serial>` (observed repeatedly on a HERO12, profile GP26489362).
# The SDK's own driver ships the cleanup (`_clean`) but never calls it before connect.
# These tests pin the correction against a faithful stub of the two nmcli drivers.
# macOS/Windows are untouched by construction: their drivers are different classes,
# and driver *detection* — not this patch — decides which one a host instantiates.
# --------------------------------------------------------------------------- #

from ingest.camera import _forget_stale_nm_profile  # noqa: E402


def _stub_nmcli_module(
    monkeypatch: pytest.MonkeyPatch, *, clean_raises: bool = False
) -> tuple[Any, Any]:
    """Install a stub SDK wireless-adapters module with both nmcli driver classes.

    Each stub records the order of profile cleanups vs connects, so a test can assert
    the cleanup happened first — the whole point of the patch.
    """

    def _make_controller() -> Any:
        class StubNmcli:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def _clean(self, partial: str) -> None:
                if clean_raises:
                    raise RuntimeError("nmcli exploded")
                self.calls.append(("clean", partial))

            async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:
                self.calls.append(("connect", ssid))
                return True

        return StubNmcli

    module = _types.ModuleType("open_gopro.network.wifi.adapters.wireless")
    # The REAL class names (verified against the installed SDK) — the patch matches on
    # the Nmcli name prefix + shape, so a stub with made-up names would pass while the
    # real SDK silently went unpatched.
    module.NmcliWireless = _make_controller()  # type: ignore[attr-defined]
    module.Nmcli0990Wireless = _make_controller()  # type: ignore[attr-defined]
    monkeypatch.setitem(
        _sys.modules, "open_gopro.network.wifi.adapters.wireless", module
    )
    return module.NmcliWireless, module.Nmcli0990Wireless


def test_connect_deletes_the_stale_profile_first(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy, modern = _stub_nmcli_module(monkeypatch)
    _forget_stale_nm_profile()

    for cls in (legacy, modern):
        drv = cls()
        assert asyncio.run(drv.connect("GP26489362", "pw")) is True
        # The order IS the fix: nmcli must take its create path, so the stale
        # profile has to be gone before the connect runs.
        assert drv.calls == [("clean", "GP26489362"), ("connect", "GP26489362")]


def test_a_failing_cleanup_never_breaks_the_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cleanup is recovery for the NEXT failure mode; it must not invent a new one."""
    _legacy, modern = _stub_nmcli_module(monkeypatch, clean_raises=True)
    _forget_stale_nm_profile()

    drv = modern()
    assert asyncio.run(drv.connect("GP26489362", "pw")) is True
    assert drv.calls == [("connect", "GP26489362")]


def test_patching_the_nm_cleanup_twice_does_not_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_load_sdk() runs on every camera open; the patch must be idempotent."""
    _legacy, modern = _stub_nmcli_module(monkeypatch)

    _forget_stale_nm_profile()
    once = modern.connect
    _forget_stale_nm_profile()

    assert modern.connect is once
    drv = modern()
    asyncio.run(drv.connect("GP26489362", "pw"))
    assert drv.calls.count(("clean", "GP26489362")) == 1, "cleanup ran more than once"


def test_an_sdk_without_the_nmcli_drivers_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS/Windows driver classes (or a renamed SDK) must not be half-patched."""
    module = _types.ModuleType("open_gopro.network.wifi.adapters.wireless")

    class NetworksetupWireless:  # the macOS driver: not an Nmcli name, no _clean
        async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:
            return True

    class NmcliFutureWireless:  # a future nmcli driver missing the cleanup hook
        async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:
            return True

    module.NetworksetupWireless = NetworksetupWireless  # type: ignore[attr-defined]
    module.NmcliFutureWireless = NmcliFutureWireless  # type: ignore[attr-defined]
    monkeypatch.setitem(
        _sys.modules, "open_gopro.network.wifi.adapters.wireless", module
    )

    _forget_stale_nm_profile()  # must simply not blow up, and not touch either class

    assert not getattr(NetworksetupWireless.connect, "_patched_by_autoedit", False)
    assert not getattr(NmcliFutureWireless.connect, "_patched_by_autoedit", False)


def test_one_unreadable_clip_does_not_abandon_the_card(tmp_path: Path) -> None:
    """A corrupt clip costs that clip only — the rest of the card still stages.

    Observed on a real season-old card: an OSError EIO mid-copy on one file aborted
    the whole pull, so the clips beside it (including a QR session marker) were never
    staged and every later scan hit the same wall.
    """
    class _FlakyCamera(FakeCamera):
        async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
            if media.filename == "GX010002.MP4":
                dest.write_bytes(b"partial")  # a truncated copy, like a real failure
                raise OSError(5, "Input/output error")
            return await super().download_mp4(media, dest)

    cam = _FlakyCamera(
        [
            _media("GX010001.MP4"),
            _media("GX010002.MP4"),
            _media("GX010003.MP4"),
        ]
    )
    events: list[dict[str, object]] = []

    class _Capture:
        def emit(self, event: dict[str, object]) -> None:
            events.append(event)

    # The failure is still surfaced (a lost master is never swallowed) ...
    with pytest.raises(CameraError, match="GX010002.MP4"):
        asyncio.run(pull_camera("1234", camera=cam, root=tmp_path, emitter=_Capture()))

    # ... but only after both readable clips were staged, manifested and emitted.
    staged = {p.name for p in tmp_path.rglob("GX01000*.MP4")}
    assert staged == {"GX010001.MP4", "GX010003.MP4"}  # no partial GX010002 left behind
    assert {e["job_id"] for e in events} == {"1234-GX010001", "1234-GX010003"}
