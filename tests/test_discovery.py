"""Tests for camera auto-discovery (registry, scanner, the service, /cameras).

No hardware, broker, or database: the BLE scan is a :class:`StaticCameraScanner`,
the registry is a small fake (the real Mongo-backed one is exercised only for its
*disabled* no-op behaviour, which needs no driver), and the pull is a fake coroutine
that emits a ``ready_for_processing`` event like the real one. Async scenarios are
driven with :func:`asyncio.run`, matching :mod:`tests.test_ingest`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import _build_pull, _build_scanner, create_app, get_registry, get_store
from api.auth import Principal, get_principal
from api.config import Settings
from api.jobs import Job, JobStore
from ingest.camera import LocalSampleCamera
from ingest.discovery import CameraDiscoveryService
from ingest.pull import pull_camera
from ingest.registry import CameraRecord, CameraRegistry
from ingest.scanner import BleCameraScanner, StaticCameraScanner, camera_id_from_name

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeRegistry:
    """In-memory stand-in for :class:`CameraRegistry` (no Mongo)."""

    def __init__(self, cameras: list[CameraRecord]) -> None:
        self._cameras = {c.camera_id: c for c in cameras}
        self.closed = False

    def known_active_ids(self) -> set[str]:
        return {cid for cid, c in self._cameras.items() if c.active}

    def list_cameras(
        self, *, active_only: bool = False, instructor_id: str | None = None
    ) -> list[CameraRecord]:
        cams = list(self._cameras.values())
        if active_only:
            cams = [c for c in cams if c.active]
        if instructor_id is not None:
            cams = [c for c in cams if c.instructor_id == instructor_id]
        return cams

    def instructor_for(self, camera_id: str) -> str | None:
        cam = self._cameras.get(camera_id)
        return cam.instructor_id if cam else None

    def role_for(self, camera_id: str) -> str | None:
        cam = self._cameras.get(camera_id)
        return cam.role if cam else None

    def assign_instructor(
        self, camera_id: str, instructor_id: str | None, role: str | None = None
    ) -> bool:
        cam = self._cameras.get(camera_id)
        update: dict[str, object] = {"instructor_id": instructor_id}
        if role is not None:
            update["role"] = role
        if cam is None:  # register-or-assign: auto-create the unknown camera
            self._cameras[camera_id] = CameraRecord(
                camera_id=camera_id, paired_at=0.0, active=True, **update
            )
        else:
            self._cameras[camera_id] = cam.model_copy(update=update)
        return True

    def deactivate(self, camera_id: str) -> bool:
        cam = self._cameras.get(camera_id)
        if cam is None:
            return False
        self._cameras[camera_id] = cam.model_copy(update={"active": False})
        return True

    def close(self) -> None:
        self.closed = True


def _make_pull(staged_mp4: Path, *, emit_once: bool = True):
    """A fake pull_camera: emits one ready_for_processing event per camera (optionally once)."""
    seen: set[str] = set()

    async def _pull(camera_id: str, *, emitter=None) -> list[object]:
        if emit_once and camera_id in seen:
            return []  # already staged — real pull emits nothing for skipped jumps
        seen.add(camera_id)
        if emitter is not None:
            emitter.emit(
                {
                    "event": "ready_for_processing",
                    "job_id": f"{camera_id}-GX010001",
                    "camera_id": camera_id,
                    "jump_dir": str(staged_mp4.parent),
                    "files": {"mp4": str(staged_mp4), "lrv": None, "thumbnail": None},
                    "created_epoch": None,
                    "emitted_at": 0.0,
                }
            )
        return []

    return _pull


class _RecordingUploader:
    """Stand-in for the SkydiveOS uploader — records each hand-off instead of POSTing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    def __call__(
        self,
        mp4_path: str,
        camera_id: str,
        instructor_id: str | None,
        camera_role: str | None = None,
    ) -> None:
        self.calls.append((mp4_path, camera_id, instructor_id, camera_role))


async def _wait_for(predicate, *, timeout: float = 3.0) -> bool:
    """Poll ``predicate`` until true or timeout (yields so background tasks run)."""
    deadline = timeout
    while deadline > 0:
        if predicate():
            return True
        await asyncio.sleep(0.02)
        deadline -= 0.02
    return predicate()


# --------------------------------------------------------------------------- #
# Scanner name parsing
# --------------------------------------------------------------------------- #


def test_camera_id_from_name_parses_gopro_advertisements() -> None:
    assert camera_id_from_name("GoPro 1234") == "1234"
    assert camera_id_from_name("GoPro4567") == "4567"
    assert camera_id_from_name("GoPro Cam 0089") == "0089"
    assert camera_id_from_name("Galaxy Buds") is None
    assert camera_id_from_name("") is None
    assert camera_id_from_name(None) is None


# --------------------------------------------------------------------------- #
# Disabled registry degrades safely (no Mongo driver needed)
# --------------------------------------------------------------------------- #


def test_registry_disabled_is_safe_noop(monkeypatch) -> None:
    # mongo_url=None defers to $MONGO_URL; with it unset the registry is disabled.
    monkeypatch.delenv("MONGO_URL", raising=False)
    reg = CameraRegistry(mongo_url=None)
    assert reg.enabled is False
    assert reg.known_active_ids() == set()
    assert reg.list_cameras() == []
    assert reg.deactivate("1234") is False
    # upsert returns the would-be record without persisting (so --pair still works)
    rec = reg.upsert_paired("1234", name="A")
    assert rec.camera_id == "1234" and rec.active is True
    reg.close()  # no client opened — must not raise


# --------------------------------------------------------------------------- #
# The service: scan → filter → pull → hand off to SkydiveOS
# --------------------------------------------------------------------------- #


def test_handoff_passes_camera_owner(tmp_path: Path) -> None:
    """The hand-off carries the instructor that owns the camera it was pulled from."""
    uploads = _RecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="1234", paired_at=1.0, instructor_id="inst-9")]
    )
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls and uploads.calls[0][2] == "inst-9"  # (mp4, camera, instructor, role)


def test_known_camera_is_pulled_and_handed_off(tmp_path: Path) -> None:
    uploads = _RecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="1234", name="A", paired_at=1.0, instructor_id="inst-1")]
    )
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    # Exactly one hand-off, with the pulled file, camera, and owning instructor.
    assert uploads.calls == [(str(tmp_path / "GX010001.MP4"), "1234", "inst-1", None)]
    assert registry.closed is True  # stop() released the registry


def test_camera_role_is_handed_off(tmp_path: Path) -> None:
    """A two-camera (Ultimate) role on the camera flows through to the hand-off."""
    uploads = _RecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="5678", paired_at=1.0, instructor_id="inst-2", role="external")]
    )
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["5678"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    # The cameraman's "external" role is passed so SkydiveOS stages it under raw/external/.
    assert uploads.calls == [(str(tmp_path / "GX010001.MP4"), "5678", "inst-2", "external")]


def test_role_resolver_overrides_static_registry_hint(tmp_path: Path) -> None:
    """The load-derived role wins over the camera's static registry role.

    The registry statically calls camera 5678 an ``instructor`` cam, but on THIS jump
    its owner flew as the cameraman — the injected resolver returns ``external`` and
    that is what's handed off (the "one staff member does both roles" case).
    """
    uploads = _RecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="5678", paired_at=1.0, instructor_id="inst-2", role="instructor")]
    )
    seen: list[tuple[str, str]] = []

    def resolver(camera_id: str, mp4_path: str) -> str | None:
        seen.append((camera_id, mp4_path))
        return "external"

    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["5678"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        role_resolver=resolver,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    assert seen == [("5678", str(tmp_path / "GX010001.MP4"))]
    # 'external' (load-derived), NOT the registry's static 'instructor' hint.
    assert uploads.calls == [(str(tmp_path / "GX010001.MP4"), "5678", "inst-2", "external")]


def test_role_resolver_falls_back_to_hint_when_unresolved(tmp_path: Path) -> None:
    """When the resolver can't decide (returns None), the static registry role is used."""
    uploads = _RecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="5678", paired_at=1.0, instructor_id="inst-2", role="external")]
    )
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["5678"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        role_resolver=lambda _c, _m: None,  # unresolved / ambiguous → fall back
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    assert uploads.calls == [(str(tmp_path / "GX010001.MP4"), "5678", "inst-2", "external")]


def test_matcher_role_resolver_adapts_matcher(monkeypatch) -> None:
    """The adapter probes capture time, asks the matcher, and swallows match errors."""
    from ingest import discovery
    from ingest.discovery import matcher_role_resolver
    from ingest.match import AmbiguousMatch, MatchResult

    monkeypatch.setattr(
        discovery, "_probe_capture_time", lambda p, clock_tz=None: "2026-07-21T17:15:00Z"
    )

    class _OkMatcher:
        def resolve(self, serial: str, captured_at: str) -> MatchResult:
            assert captured_at == "2026-07-21T17:15:00Z"
            return MatchResult(
                role="external", staff_id="s1", load_id="L1", jumper_index=0
            )

    assert matcher_role_resolver(_OkMatcher())("5678", "/x.mp4") == "external"

    class _AmbiguousMatcher:
        def resolve(self, serial: str, captured_at: str) -> MatchResult:
            raise AmbiguousMatch("two jumpers", [])

    # Ambiguous → None (caller falls back to the static hint), never raises.
    assert matcher_role_resolver(_AmbiguousMatcher())("5678", "/x.mp4") is None

    # No readable capture time → None without even calling the matcher.
    monkeypatch.setattr(discovery, "_probe_capture_time", lambda p, clock_tz=None: None)
    called = False

    class _Unused:
        def resolve(self, *a: object) -> MatchResult:
            nonlocal called
            called = True
            raise AssertionError("must not be called")

    assert matcher_role_resolver(_Unused())("5678", "/x.mp4") is None
    assert called is False


def test_unknown_camera_is_ignored(tmp_path: Path) -> None:
    uploads = _RecordingUploader()
    # Scanner sees 9999, but only 1234 is paired → 9999 must never be pulled.
    registry = FakeRegistry([CameraRecord(camera_id="1234", paired_at=1.0)])
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["9999"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
    )

    async def scenario() -> None:
        await service.start()
        await asyncio.sleep(0.2)  # let several scans run
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls == []


def test_repeated_scans_hand_off_once(tmp_path: Path) -> None:
    """A camera lingering across scans hands off once — the pull only emits for new files."""
    uploads = _RecordingUploader()
    registry = FakeRegistry([CameraRecord(camera_id="1234", paired_at=1.0)])
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=registry,
        upload=uploads,
        # emit_once=True models the real pull: already-staged jumps emit no event.
        pull=_make_pull(tmp_path / "GX010001.MP4", emit_once=True),
        interval=0.03,
    )

    async def scenario() -> None:
        await service.start()
        await asyncio.sleep(0.3)  # ~10 scan ticks
        await service.stop()

    asyncio.run(scenario())
    assert len(uploads.calls) == 1  # not repeated despite many scans


def test_s3_notify_uploader_puts_to_s3_then_notifies(tmp_path, monkeypatch) -> None:
    """The default uploader PUTs the file to S3, then POSTs {s3_key,...} to SkydiveOS."""
    import httpx

    from ingest.discovery import s3_notify_uploader

    mp4 = tmp_path / "GX010001.MP4"
    mp4.write_bytes(b"video-bytes")

    class _FakeS3:
        def __init__(self) -> None:
            self.uploads: list[tuple[str, str, str]] = []

        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            self.uploads.append((filename, bucket, key))

    s3 = _FakeS3()
    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            posted["raised"] = False

    def _fake_post(url, *, json, timeout, headers=None):  # noqa: ANN001
        posted.update(url=url, json=json, headers=headers or {})
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)
    upload = s3_notify_uploader("http://skydiveos.test/", bucket="jumps", s3_client=s3)
    upload(str(mp4), "1234", "inst-1")

    # File went to s3://jumps/raw/1234/GX010001.MP4 ...
    assert s3.uploads == [(str(mp4), "jumps", "raw/1234/GX010001.MP4")]
    # ... and SkydiveOS was notified with the key + camera + instructor (no file bytes).
    assert posted["url"] == "http://skydiveos.test/api/media/raw-upload"
    assert posted["json"] == {
        "s3_key": "raw/1234/GX010001.MP4",
        "camera_id": "1234",
        "instructor_id": "inst-1",
    }

    # No instructor → the field is simply omitted (camera unassigned).
    posted.clear()
    upload(str(mp4), "1234", None)
    assert posted["json"] == {"s3_key": "raw/1234/GX010001.MP4", "camera_id": "1234"}


def test_s3_notify_uploader_includes_capture_time(tmp_path, monkeypatch) -> None:
    """When the MP4 has a readable creation_time, it rides along as captured_at."""
    import httpx

    from ingest import discovery
    from ingest.discovery import s3_notify_uploader

    mp4 = tmp_path / "GX010001.MP4"
    mp4.write_bytes(b"video-bytes")

    class _FakeS3:
        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            pass

    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    def _fake_post(url, *, json, timeout, headers=None):  # noqa: ANN001
        posted.update(json=json)
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)
    # Stub the ffprobe read so the test doesn't depend on a real GoPro file.
    monkeypatch.setattr(
        discovery, "_probe_capture_time",
        lambda p, *, clock_tz=None: "2026-06-10T18:42:31Z",
    )

    upload = s3_notify_uploader("http://skydiveos.test/", bucket="jumps", s3_client=_FakeS3())
    upload(str(mp4), "1234", "inst-1", "external")

    assert posted["json"] == {
        "s3_key": "raw/1234/GX010001.MP4",
        "camera_id": "1234",
        "instructor_id": "inst-1",
        "camera_role": "external",
        "captured_at": "2026-06-10T18:42:31Z",
    }


def test_probe_capture_time_never_raises_on_bad_file(tmp_path) -> None:
    """A non-video / unreadable file yields None, never an exception (best-effort)."""
    from ingest.discovery import _probe_capture_time

    bad = tmp_path / "not-a-video.MP4"
    bad.write_bytes(b"not really an mp4")
    assert _probe_capture_time(str(bad)) is None


def test_to_true_utc_converts_gopro_local_time() -> None:
    """A GoPro creation_time (local wall-clock mislabelled Z) becomes real UTC."""
    from ingest.discovery import _to_true_utc

    # 14:42 Toronto in June is EDT (UTC-4) → 18:42 UTC.
    assert _to_true_utc("2026-06-10T14:42:31.000000Z", "America/Toronto") == \
        "2026-06-10T18:42:31Z"
    # January is EST (UTC-5) → 19:42 UTC. DST handled by the zone, not a fixed offset.
    assert _to_true_utc("2026-01-10T14:42:31Z", "America/Toronto") == "2026-01-10T19:42:31Z"


def test_to_true_utc_passthrough_without_tz() -> None:
    """No clock_tz → the tag is left as-is (assumed already UTC), backward-compatible."""
    from ingest.discovery import _to_true_utc

    assert _to_true_utc("2026-06-10T14:42:31.000000Z", None) == "2026-06-10T14:42:31.000000Z"


def test_to_true_utc_bad_zone_falls_back_to_raw() -> None:
    """An unknown zone never raises — returns the original value (hand-off not blocked)."""
    from ingest.discovery import _to_true_utc

    assert _to_true_utc("2026-06-10T14:42:31Z", "Not/AZone") == "2026-06-10T14:42:31Z"


def test_stop_is_idempotent_and_clears_tasks(tmp_path: Path) -> None:
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner([]),
        registry=FakeRegistry([]),
        upload=_RecordingUploader(),
        pull=_make_pull(tmp_path / "x.MP4"),
        interval=0.05,
    )

    async def scenario() -> None:
        await service.start()
        await service.stop()
        await service.stop()  # second stop must be a no-op, not raise

    asyncio.run(scenario())
    assert service._tasks == []


# --------------------------------------------------------------------------- #
# /cameras endpoints
# --------------------------------------------------------------------------- #


def test_cameras_endpoints(tmp_path: Path) -> None:
    registry = FakeRegistry(
        [
            CameraRecord(camera_id="1234", name="A", paired_at=2.0),
            CameraRecord(camera_id="5678", name="B", paired_at=1.0),
        ]
    )
    app = create_app()
    app.dependency_overrides[get_registry] = lambda: registry

    with TestClient(app) as client:
        resp = client.get("/cameras")
        assert resp.status_code == 200, resp.text
        ids = {c["camera_id"] for c in resp.json()["cameras"]}
        assert ids == {"1234", "5678"}

        # Deactivate one → it stays listed but inactive, and discovery would skip it.
        resp = client.delete("/cameras/1234")
        assert resp.status_code == 200, resp.text
        cams = {c["camera_id"]: c["active"] for c in resp.json()["cameras"]}
        assert cams == {"1234": False, "5678": True}
        assert registry.known_active_ids() == {"5678"}

        # Deleting an unknown camera 404s.
        assert client.delete("/cameras/0000").status_code == 404

    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# No-hardware simulation mode (CAMERA_SCANNER=static)
# --------------------------------------------------------------------------- #


def _settings(**overrides: object) -> Settings:
    """A Settings with discovery defaults, overridable per test."""
    base: dict[str, object] = dict(
        redis_url="redis://localhost:6379/0",
        jobs_root=None,
        skydiveos_api_base=None,
        task_always_eager=False,
        enable_auto_discovery=True,
        mongo_url=None,
        mongo_db="skydiveos",
        discovery_interval=30.0,
        camera_scanner="ble",
        delete_after_transfer=False,
        delete_after_transfer_min_age_h=24.0,
        delete_after_transfer_dry_run=False,
        discovery_fake_cameras=(),
        discovery_sample_mp4=None,
        discovery_sample_count=1,
        enforce_instructor_auth=False,
        s3_bucket=None,
        s3_endpoint_url=None,
        s3_region=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_scanner_picks_mode() -> None:
    from ingest.scanner import SdCardScanner, UsbCameraScanner

    static = _build_scanner(_settings(camera_scanner="static", discovery_fake_cameras=("1234",)))
    assert isinstance(static, StaticCameraScanner)
    assert asyncio.run(static.scan()) == ["1234"]
    assert isinstance(_build_scanner(_settings(camera_scanner="ble")), BleCameraScanner)
    assert isinstance(_build_scanner(_settings(camera_scanner="usb")), UsbCameraScanner)
    assert isinstance(_build_scanner(_settings(camera_scanner="sdcard")), SdCardScanner)


def test_build_pull_usb_uses_wired_camera() -> None:
    # usb mode wires a pull (against a WiredGoProCamera); ble stays the default (None).
    assert _build_pull(_settings(camera_scanner="usb")) is not None
    assert _build_pull(_settings(camera_scanner="ble")) is None


def test_usb_and_wired_fail_clearly_without_sdk() -> None:
    """Without the hardware SDK, the USB scan + wired camera raise a clear CameraError."""
    from ingest.camera import CameraError, WiredGoProCamera
    from ingest.scanner import UsbCameraScanner

    # open_gopro isn't installed in CI → both lazy imports fail with our message.
    try:
        import open_gopro  # noqa: F401
    except ImportError:
        import pytest

        with pytest.raises(CameraError, match="Open GoPro SDK"):
            asyncio.run(UsbCameraScanner().scan())
        with pytest.raises(CameraError, match="Open GoPro SDK"):
            asyncio.run(WiredGoProCamera("123").open())


def test_wired_camera_lists_media_via_shared_base(monkeypatch) -> None:
    """WiredGoProCamera inherits list/download from the shared SDK base (faked here)."""
    from ingest import camera as camera_mod
    from ingest.camera import WiredGoProCamera

    class _Resp:
        def __init__(self, ok=True, data=None):
            self.ok, self.data = ok, data

    class _Files:
        def __init__(self, files):
            self.files = files

    class _Item:
        def __init__(self, filename):
            self.filename = filename

    class _FakeWired:
        """Minimal stand-in for the SDK's WiredGoPro handle."""

        def __init__(self, serial=None):
            self.serial = serial

        async def open(self):
            return None

        async def close(self):
            return None

        @property
        def http_command(self):
            return self

        async def get_media_list(self):
            return _Resp(
                data=_Files([_Item("100GOPRO/GX010001.MP4"), _Item("100GOPRO/GX010001.THM")])
            )

    monkeypatch.setattr(camera_mod, "_load_wired_sdk", lambda: _FakeWired)

    async def scenario():
        async with WiredGoProCamera("123") as cam:
            return await cam.list_videos()

    videos = asyncio.run(scenario())
    # Only the .MP4 is returned (the .THM is filtered by the shared list_videos).
    assert [v.filename for v in videos] == ["GX010001.MP4"]


def test_build_pull_modes(monkeypatch) -> None:
    import sys

    import pytest

    appmod = sys.modules["api.app"]

    assert _build_pull(_settings(camera_scanner="ble")) is None
    # sdcard mode wires a pull against the mounted card.
    assert _build_pull(_settings(camera_scanner="sdcard")) is not None
    # An explicit sample → a simulated pull callable.
    assert _build_pull(
        _settings(camera_scanner="static", discovery_sample_mp4="x.mp4")
    ) is not None
    # No explicit sample → falls back to the bundled default (which exists in-repo).
    assert _build_pull(
        _settings(camera_scanner="static", discovery_sample_mp4=None)
    ) is not None
    # No sample and no bundled default → a clear configuration error (not a crash later).
    monkeypatch.setattr(appmod, "_DEFAULT_SAMPLE_MP4", "/no/such/sample.mp4")
    with pytest.raises(RuntimeError, match="sample MP4"):
        _build_pull(_settings(camera_scanner="static", discovery_sample_mp4=None))


def test_local_sample_camera_stages_through_real_pull(tmp_path: Path) -> None:
    """The simulation camera drives the real pull path: stages files + emits an event."""
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"\x00\x11\x22\x33" * 64)  # stand-in media bytes
    events: list[dict[str, object]] = []

    class _Capture:
        def emit(self, event: dict[str, object]) -> None:
            events.append(event)

    cam = LocalSampleCamera(sample, filename="GX010001.MP4")
    jumps = asyncio.run(
        pull_camera("1234", camera=cam, root=tmp_path / "raw", emitter=_Capture())
    )

    # One jump staged from the sample, with a real manifest + emitted event.
    assert len(jumps) == 1 and jumps[0].skipped is False
    staged = jumps[0].mp4_path
    assert staged.exists() and staged.read_bytes() == sample.read_bytes()
    assert len(events) == 1
    assert events[0]["job_id"] == "1234-GX010001"
    assert events[0]["files"]["mp4"] == str(staged)  # type: ignore[index]

    # Re-pull is idempotent: already staged → skipped, no new event.
    again = asyncio.run(
        pull_camera("1234", camera=cam, root=tmp_path / "raw", emitter=_Capture())
    )
    assert again[0].skipped is True


def test_local_sample_camera_reports_multiple_clips(tmp_path: Path) -> None:
    """count>1 stages several distinct files in one pull, like a real GoPro card."""
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"\x00\x11\x22\x33" * 64)
    events: list[dict[str, object]] = []

    class _Capture:
        def emit(self, event: dict[str, object]) -> None:
            events.append(event)

    cam = LocalSampleCamera(sample, filename="GX010001.MP4", count=12)
    jumps = asyncio.run(
        pull_camera("1234", camera=cam, root=tmp_path / "raw", emitter=_Capture())
    )

    # Twelve distinct clips, each staged and emitted with its own incrementing name.
    assert len(jumps) == 12
    assert all(j.skipped is False for j in jumps)
    job_ids = [e["job_id"] for e in events]
    assert job_ids == [f"1234-GX0100{n:02d}" for n in range(1, 13)]
    assert len(set(job_ids)) == 12  # no collisions


def test_simulated_clip_count_marker_override(tmp_path: Path, monkeypatch) -> None:
    """A per-camera marker bumps the simulated clip count (a new jump on the card)."""
    from api.app import SIM_CLIPS_DIR, _simulated_clip_count

    monkeypatch.setenv("RAW_STORAGE_ROOT", str(tmp_path))
    settings = _settings(discovery_sample_count=12)

    # No marker → the configured base count.
    assert _simulated_clip_count(settings, "CAM1") == 12

    # Marker present → it wins (operator added clips between scans).
    marker = tmp_path / SIM_CLIPS_DIR / "CAM1"
    marker.parent.mkdir(parents=True)
    marker.write_text("14")
    assert _simulated_clip_count(settings, "CAM1") == 14

    # A garbage marker is ignored, falling back to the base count (never crashes).
    marker.write_text("oops")
    assert _simulated_clip_count(settings, "CAM1") == 12


# --------------------------------------------------------------------------- #
# Per-instructor access scoping (identity forwarded by SkydiveOS)
# --------------------------------------------------------------------------- #


def _client(tmp_path: Path, principal: Principal, registry: object | None = None) -> TestClient:
    """A TestClient whose store is rooted in tmp_path and caller is ``principal``."""
    app = create_app()
    app.dependency_overrides[get_store] = lambda: JobStore(tmp_path)
    app.dependency_overrides[get_principal] = lambda: principal
    if registry is not None:
        app.dependency_overrides[get_registry] = lambda: registry
    return TestClient(app)


def _seed_job(tmp_path: Path, job_id: str, instructor_id: str | None) -> None:
    JobStore(tmp_path).create(Job(job_id=job_id, instructor_id=instructor_id))


def test_jobs_are_scoped_to_the_instructor(tmp_path: Path) -> None:
    _seed_job(tmp_path, "job-a", "inst-1")
    _seed_job(tmp_path, "job-b", "inst-2")
    _seed_job(tmp_path, "job-c", None)  # unowned

    # Instructor inst-1 sees only their own job, and can't reach others by id.
    with _client(tmp_path, Principal("inst-1", "instructor")) as c:
        listed = {j["job_id"] for j in c.get("/jobs").json()["jobs"]}
        assert listed == {"job-a"}
        assert c.get("/jobs/job-a").status_code == 200
        assert c.get("/jobs/job-b").status_code == 404  # not theirs → hidden
        assert c.get("/jobs/job-c").status_code == 404  # unowned → hidden

    # Admin sees everything.
    with _client(tmp_path, Principal(None, "admin")) as c:
        listed = {j["job_id"] for j in c.get("/jobs").json()["jobs"]}
        assert listed == {"job-a", "job-b", "job-c"}
        assert c.get("/jobs/job-b").status_code == 200


def test_camera_management_is_admin_only(tmp_path: Path) -> None:
    registry = FakeRegistry(
        [
            CameraRecord(camera_id="1234", paired_at=2.0, instructor_id="inst-1"),
            CameraRecord(camera_id="5678", paired_at=1.0, instructor_id="inst-2"),
        ]
    )

    # An instructor sees only their cameras and cannot manage the registry.
    with _client(tmp_path, Principal("inst-1", "instructor"), registry) as c:
        ids = {cam["camera_id"] for cam in c.get("/cameras").json()["cameras"]}
        assert ids == {"1234"}
        assert c.delete("/cameras/1234").status_code == 403
        assert c.post("/cameras/1234/assign", json={"instructor_id": "x"}).status_code == 403

    # An admin sees all and can (re)assign + deactivate.
    with _client(tmp_path, Principal(None, "admin"), registry) as c:
        assert len(c.get("/cameras").json()["cameras"]) == 2
        resp = c.post("/cameras/5678/assign", json={"instructor_id": "inst-1"})
        assert resp.status_code == 200
        assert registry.instructor_for("5678") == "inst-1"
        assert c.delete("/cameras/5678").status_code == 200


def test_assign_auto_registers_unknown_camera(tmp_path: Path) -> None:
    """Assigning a serial that isn't in the registry creates it (register + assign)."""
    registry = FakeRegistry([])
    with _client(tmp_path, Principal(None, "admin"), registry) as c:
        assert registry.instructor_for("9999") is None  # not registered yet
        resp = c.post("/cameras/9999/assign", json={"instructor_id": "inst-7"})
        assert resp.status_code == 200  # not a 404 — auto-created
        cams = {cam["camera_id"]: cam for cam in resp.json()["cameras"]}
        assert cams["9999"]["instructor_id"] == "inst-7"
        assert cams["9999"]["active"] is True  # active, so discovery will pull it
        assert registry.instructor_for("9999") == "inst-7"


# --------------------------------------------------------------------------- #
# Hand-off retry: a network blip (WiFi on the camera AP) must not drop footage
# --------------------------------------------------------------------------- #


class _FlakyUploader(_RecordingUploader):
    """Uploader that fails the first ``fail_times`` calls, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self.failures = 0

    def __call__(self, *args, **kwargs) -> None:
        if self.failures < self._fail_times:
            self.failures += 1
            raise ConnectionError("no internet (WiFi joined to the camera AP)")
        super().__call__(*args, **kwargs)


def test_failed_handoff_is_retried_until_network_returns(tmp_path: Path) -> None:
    """Hand-offs that fail while offline are requeued, not dropped (staged jumps
    never re-emit, so a drop would strand the footage locally forever)."""
    uploads = _FlakyUploader(fail_times=2)
    registry = FakeRegistry(
        [CameraRecord(camera_id="1234", paired_at=1.0, instructor_id="inst-1")]
    )
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        handoff_retry_delay=0.01,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())
    assert uploads.failures == 2
    assert uploads.calls == [(str(tmp_path / "GX010001.MP4"), "1234", "inst-1", None)]


def test_handoff_gives_up_after_max_attempts(tmp_path: Path) -> None:
    """A permanently failing hand-off stops retrying (no hot loop) and is logged."""
    uploads = _FlakyUploader(fail_times=10_000)
    registry = FakeRegistry(
        [CameraRecord(camera_id="1234", paired_at=1.0, instructor_id="inst-1")]
    )
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        handoff_retry_delay=0.01,
        handoff_max_attempts=3,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: uploads.failures >= 3)
        # Give any (wrongly) still-scheduled retry a chance to fire, then check
        # the attempt count stayed at the cap.
        await asyncio.sleep(0.1)
        await service.stop()

    asyncio.run(scenario())
    assert uploads.failures == 3
    assert uploads.calls == []


def test_registry_db_name_resolves_like_the_url(monkeypatch) -> None:
    """The --pair CLI builds CameraRegistry() bare; it must honor $MONGO_DB or the
    pairing lands in a different database than the service's allow-list reads."""
    monkeypatch.setenv("MONGO_DB", "skydivingos")
    assert CameraRegistry(mongo_url=None)._db_name == "skydivingos"
    monkeypatch.delenv("MONGO_DB")
    assert CameraRegistry(mongo_url=None)._db_name == "skydiveos"
    assert CameraRegistry(mongo_url=None, db_name="explicit")._db_name == "explicit"


# --------------------------------------------------------------------------- #
# QR session flow (identity resolver + registry bypass, CAMERA_SCANNER=sdcard)
# --------------------------------------------------------------------------- #


class _KwargsRecordingUploader:
    """Records positional AND keyword args — the QR flow passes staff_id/marker."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        mp4, camera_id = str(args[0]), args[1]
        prefix = f"raw/{camera_id}/markers" if kwargs.get("marker") else f"raw/{camera_id}"
        return f"{prefix}/{Path(mp4).name}"


def test_identity_resolver_staff_id_reaches_uploader(tmp_path: Path) -> None:
    """The QR-resolved staff id (and role) ride the hand-off to SkydiveOS."""
    from ingest.qr import ClipIdentity

    uploads = _KwargsRecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="4313", paired_at=1.0, instructor_id="inst-1", role="instructor")]
    )
    staged = tmp_path / "4313" / "2026-08-04" / "GX010002.MP4"
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["4313"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(staged),
        interval=0.05,
        identity_resolver=lambda _c, _m: ClipIdentity("staff-A", role="external"),
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    ((args, kwargs),) = uploads.calls
    # QR role overrides the registry hint; staff_id goes along as a keyword.
    assert args == (str(staged), "4313", "inst-1", "external")
    assert kwargs == {"staff_id": "staff-A"}


def test_qr_marker_is_uploaded_as_marker_and_never_notified(tmp_path: Path) -> None:
    """A session-marker clip: PUT under markers/ (ledger entry) but no hand-off."""
    from ingest.qr import ClipIdentity
    from ingest.retention import confirmed

    uploads = _KwargsRecordingUploader()
    registry = FakeRegistry([CameraRecord(camera_id="4313", paired_at=1.0)])
    staged = tmp_path / "4313" / "2026-08-04" / "GX010001.MP4"
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["4313"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(staged),
        interval=0.05,
        identity_resolver=lambda _c, _m: ClipIdentity("staff-A", is_qr_marker=True),
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    ((args, kwargs),) = uploads.calls
    assert kwargs == {"marker": True}
    assert args == (str(staged), "4313", None, None)
    # The marker's S3 key is in the ledger, so the sweep may clear it off the card.
    ledger = confirmed(staged.parent.parent)
    assert ledger["GX010001.MP4"].s3_key == "raw/4313/markers/GX010001.MP4"


def test_clip_with_no_session_hands_off_without_staff_id(tmp_path: Path) -> None:
    """No preceding QR marker → the hand-off looks exactly like the pre-QR flow."""
    from ingest.qr import ClipIdentity

    uploads = _KwargsRecordingUploader()
    registry = FakeRegistry(
        [CameraRecord(camera_id="4313", paired_at=1.0, instructor_id="inst-1", role="external")]
    )
    staged = tmp_path / "4313" / "2026-08-04" / "GX010003.MP4"
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["4313"]),
        registry=registry,
        upload=uploads,
        pull=_make_pull(staged),
        interval=0.05,
        identity_resolver=lambda _c, _m: ClipIdentity(None),
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())

    ((args, kwargs),) = uploads.calls
    assert args == (str(staged), "4313", "inst-1", "external")
    assert kwargs == {}  # no staff_id keyword — old uploaders keep working


def test_unregistered_card_pulled_when_registry_bypassed(tmp_path: Path) -> None:
    """sdcard mode: an inserted card is an operator action — no registry entry needed."""
    uploads = _RecordingUploader()
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["sd-NO-NAME"]),
        registry=FakeRegistry([]),  # nothing paired
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        require_registered=False,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: bool(uploads.calls))
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls == [(str(tmp_path / "GX010001.MP4"), "sd-NO-NAME", None, None)]


def test_registry_still_filters_when_required(tmp_path: Path) -> None:
    """Regression: the default (require_registered=True) still drops unknown cameras."""
    uploads = _RecordingUploader()
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["9999"]),
        registry=FakeRegistry([]),
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
    )

    async def scenario() -> None:
        await service.start()
        await asyncio.sleep(0.2)
        await service.stop()

    asyncio.run(scenario())
    assert uploads.calls == []


def test_s3_notify_uploader_includes_staff_id(tmp_path, monkeypatch) -> None:
    """staff_id rides the notify payload with staff_source=qr; absent when None."""
    import httpx

    from ingest.discovery import s3_notify_uploader

    mp4 = tmp_path / "GX010001.MP4"
    mp4.write_bytes(b"video-bytes")

    class _FakeS3:
        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            pass

    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    def _fake_post(url, *, json, timeout, headers=None):  # noqa: ANN001
        posted.update(json=json)
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)
    upload = s3_notify_uploader("http://skydiveos.test/", bucket="jumps", s3_client=_FakeS3())

    upload(str(mp4), "4313", None, None, staff_id="665f1c0a2ab79c0012345678")
    assert posted["json"] == {
        "s3_key": "raw/4313/GX010001.MP4",
        "camera_id": "4313",
        "staff_id": "665f1c0a2ab79c0012345678",
        "staff_source": "qr",
    }

    posted.clear()
    upload(str(mp4), "4313", None, None)
    assert "staff_id" not in posted["json"] and "staff_source" not in posted["json"]


def test_s3_notify_uploader_marker_key_and_no_post(tmp_path, monkeypatch) -> None:
    """marker=True → PUT under markers/ and return the key WITHOUT notifying."""
    import httpx
    import pytest

    from ingest.discovery import s3_notify_uploader

    mp4 = tmp_path / "GX010001.MP4"
    mp4.write_bytes(b"qr-marker-bytes")

    class _FakeS3:
        def __init__(self) -> None:
            self.uploads: list[tuple[str, str, str]] = []

        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            self.uploads.append((filename, bucket, key))

    s3 = _FakeS3()
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: pytest.fail("a QR marker must never be notified to SkydiveOS"),
    )

    upload = s3_notify_uploader("http://skydiveos.test/", bucket="jumps", s3_client=s3)
    key = upload(str(mp4), "4313", None, None, marker=True)

    assert key == "raw/4313/markers/GX010001.MP4"
    assert s3.uploads == [(str(mp4), "jumps", "raw/4313/markers/GX010001.MP4")]


# --------------------------------------------------------------------------- #
# A rejected hand-off (HTTP 4xx) is permanent — retrying re-sends the identical
# payload to the identical endpoint. Only network-shaped failures get the ladder.
# --------------------------------------------------------------------------- #


def _status_error(status: int) -> Exception:
    err = RuntimeError(f"HTTP {status}")
    err.response = type("R", (), {"status_code": status})()  # type: ignore[attr-defined]
    return err


def test_permanent_handoff_error_classification() -> None:
    """4xx is permanent; 429, 5xx and plain network errors keep their retry ladder."""
    from ingest.discovery import _is_permanent_handoff_error

    assert _is_permanent_handoff_error(_status_error(404)) is True   # unknown staff_id
    assert _is_permanent_handoff_error(_status_error(400)) is True
    assert _is_permanent_handoff_error(_status_error(422)) is True
    assert _is_permanent_handoff_error(_status_error(429)) is False  # "later" == retry
    assert _is_permanent_handoff_error(_status_error(500)) is False
    assert _is_permanent_handoff_error(_status_error(503)) is False
    assert _is_permanent_handoff_error(ConnectionError("no internet")) is False
    # botocore/requests style: .response.status
    err = RuntimeError("boom")
    err.response = type("R", (), {"status": 404})()  # type: ignore[attr-defined]
    assert _is_permanent_handoff_error(err) is True


def test_rejected_handoff_is_not_retried(tmp_path: Path) -> None:
    """SkydiveOS answering 404 costs ONE attempt, not the whole backoff ladder."""

    class _Uploader:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, *args: object, **kwargs: object) -> None:
            self.attempts += 1
            raise _status_error(404)

    uploads = _Uploader()
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=FakeRegistry([CameraRecord(camera_id="1234", paired_at=1.0)]),
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        handoff_retry_delay=0.01,
        handoff_max_attempts=5,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: uploads.attempts >= 1)
        await asyncio.sleep(0.2)  # any (wrongly) scheduled retry would fire in here
        await service.stop()

    asyncio.run(scenario())
    assert uploads.attempts == 1


def test_server_error_handoff_still_retries(tmp_path: Path) -> None:
    """Regression guard: a 5xx must keep the retry ladder (SkydiveOS restarting)."""

    class _Uploader:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, *args: object, **kwargs: object) -> None:
            self.attempts += 1
            raise _status_error(503)

    uploads = _Uploader()
    service = CameraDiscoveryService(
        scanner=StaticCameraScanner(["1234"]),
        registry=FakeRegistry([CameraRecord(camera_id="1234", paired_at=1.0)]),
        upload=uploads,
        pull=_make_pull(tmp_path / "GX010001.MP4"),
        interval=0.05,
        handoff_retry_delay=0.01,
        handoff_max_attempts=4,
    )

    async def scenario() -> None:
        await service.start()
        await _wait_for(lambda: uploads.attempts >= 3)
        await service.stop()

    asyncio.run(scenario())
    assert uploads.attempts >= 3


def test_the_notify_carries_the_service_token(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The raw-upload notify is authenticated with the same token the API uses.

    The consumer is internet-facing and one accepted notification creates a job and
    emails a customer, so a security-group rule must not be its only gate. Unset key
    → no header, i.e. the same opt-in as the API's own gate.
    """
    import httpx

    from api.config import get_settings
    from ingest.discovery import s3_notify_uploader

    mp4 = tmp_path / "GX010001.MP4"
    mp4.write_bytes(b"x")
    class _FakeS3Client:
        def upload_file(self, *a: object, **kw: object) -> None:
            return None

    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    def _fake_post(url, *, json, timeout, headers=None):  # noqa: ANN001
        posted["headers"] = headers or {}
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)

    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cr3t")
    get_settings.cache_clear()
    try:
        s3_notify_uploader(
            "http://skydiveos.test/", bucket="jumps", s3_client=_FakeS3Client()
        )(str(mp4), "1234", None)
        assert posted["headers"].get("Authorization") == "Bearer s3cr3t"  # type: ignore[union-attr]

        monkeypatch.delenv("AUTO_EDIT_API_KEY", raising=False)
        get_settings.cache_clear()
        posted.clear()
        s3_notify_uploader(
            "http://skydiveos.test/", bucket="jumps", s3_client=_FakeS3Client()
        )(str(mp4), "1234", None)
        assert "Authorization" not in posted["headers"]
    finally:
        get_settings.cache_clear()
