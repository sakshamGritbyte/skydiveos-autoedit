"""Tests for QR session-marker attribution (:mod:`ingest.qr`).

The decode roundtrip builds a real QR clip in-test (cv2 encoder → PNG frame →
ffmpeg), skipped when ffmpeg is absent. Session attribution needs no video at
all: it is driven entirely by fabricated ``.ingest.json`` manifests and
``.qr.json`` sidecars — the caches the real flow writes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ingest.qr import (
    QR_STAFF_PREFIX,
    ClipIdentity,
    build_session_index,
    cached_staff_qr,
    decode_staff_qr,
    parse_staff_payload,
    qr_identity_resolver,
)

_STAFF = "665f1c0a2ab79c0012345678"

# --------------------------------------------------------------------------- #
# Payload parsing (pure)
# --------------------------------------------------------------------------- #


def test_parse_staff_payload_prefix_and_rejects_random_qr() -> None:
    assert parse_staff_payload(f"{QR_STAFF_PREFIX}{_STAFF}") == _STAFF
    # A boarding pass / wifi QR / sticker in frame is not an instructor.
    assert parse_staff_payload("https://example.com/boarding/123") is None
    assert parse_staff_payload("WIFI:T:WPA;S:dropzone;;") is None
    assert parse_staff_payload("") is None
    assert parse_staff_payload(None) is None
    assert parse_staff_payload(QR_STAFF_PREFIX) is None  # empty id


# --------------------------------------------------------------------------- #
# Decoding a real clip
# --------------------------------------------------------------------------- #


def _qr_clip(tmp_path: Path, staff_id: str, name: str = "GX010001.MP4") -> Path:
    """Render a short MP4 showing a real QR code (cv2 encode → ffmpeg loop)."""
    cv2 = pytest.importorskip("cv2")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")

    params = cv2.QRCodeEncoder.Params()
    params.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_H
    code = cv2.QRCodeEncoder.create(params).encode(f"{QR_STAFF_PREFIX}{staff_id}")
    scaled = cv2.resize(code, (480, 480), interpolation=cv2.INTER_NEAREST)
    bordered = cv2.copyMakeBorder(scaled, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    png = tmp_path / "qr.png"
    cv2.imwrite(str(png), bordered)

    clip = tmp_path / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-loop", "1", "-i", str(png),
            "-t", "2", "-r", "5",
            "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
    )
    return clip


def _plain_clip(tmp_path: Path, name: str = "GX010009.MP4") -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    clip = tmp_path / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=gray:s=320x240:d=2:r=5",
            "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
    )
    return clip


def test_decode_staff_qr_from_generated_video(tmp_path: Path) -> None:
    clip = _qr_clip(tmp_path, _STAFF)
    assert decode_staff_qr(clip, scan_seconds=2.0) == _STAFF


def test_decode_returns_none_for_plain_video(tmp_path: Path) -> None:
    assert decode_staff_qr(_plain_clip(tmp_path), scan_seconds=2.0) is None


def test_decode_never_raises_on_garbage_file(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    bad = tmp_path / "not-a-video.MP4"
    bad.write_bytes(b"not an mp4 at all")
    assert decode_staff_qr(bad) is None


# --------------------------------------------------------------------------- #
# Sidecar cache + duration gate
# --------------------------------------------------------------------------- #


def test_cached_decode_writes_sidecar_and_never_decodes_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import qr as qr_mod

    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"fake")
    calls: list[str] = []

    monkeypatch.setattr(qr_mod, "_probe_video", lambda p: (3.0, 320, 240))
    monkeypatch.setattr(
        qr_mod, "decode_staff_qr",
        lambda p, *, scan_seconds=8.0, fps=2.0: calls.append(str(p)) or _STAFF,
    )

    assert cached_staff_qr(clip) == _STAFF
    assert cached_staff_qr(clip) == _STAFF  # second read hits the sidecar
    assert len(calls) == 1
    sidecar = json.loads((tmp_path / "GX010001.qr.json").read_text())
    assert sidecar["staff_id"] == _STAFF


def test_cached_decode_caches_negative_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import qr as qr_mod

    clip = tmp_path / "GX010002.MP4"
    clip.write_bytes(b"fake")
    calls: list[str] = []

    monkeypatch.setattr(qr_mod, "_probe_video", lambda p: (3.0, 320, 240))
    monkeypatch.setattr(
        qr_mod, "decode_staff_qr",
        lambda p, *, scan_seconds=8.0, fps=2.0: calls.append(str(p)),  # returns None
    )

    assert cached_staff_qr(clip) is None
    assert cached_staff_qr(clip) is None
    assert len(calls) == 1  # "no QR" is remembered too


def test_long_clip_is_skipped_not_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 20-minute jump is never fed to the decoder — the gate rules it out."""
    from ingest import qr as qr_mod

    clip = tmp_path / "GX010003.MP4"
    clip.write_bytes(b"fake")

    monkeypatch.setattr(qr_mod, "_probe_video", lambda p: (1200.0, 3840, 2160))
    monkeypatch.setattr(
        qr_mod, "decode_staff_qr",
        lambda p, **kw: pytest.fail("decoder must not run on a long clip"),
    )

    assert cached_staff_qr(clip, max_clip_seconds=60.0) is None
    assert json.loads((tmp_path / "GX010003.qr.json").read_text())["skipped"] == "too-long"


# --------------------------------------------------------------------------- #
# Session attribution (no video needed — manifests + sidecars only)
# --------------------------------------------------------------------------- #


def _stage(day_dir: Path, name: str, epoch: float, *, staff: str | None = None) -> None:
    """Fabricate one staged clip: MP4 stub + .ingest.json manifest + .qr.json sidecar."""
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    (day_dir / name).write_bytes(b"fake")
    (day_dir / f"{stem}.ingest.json").write_text(
        json.dumps(
            {
                "event": "ready_for_processing",
                "job_id": f"4313-{stem}",
                "camera_id": "4313",
                "files": {"mp4": str(day_dir / name)},
                "created_epoch": epoch,
            }
        )
    )
    (day_dir / f"{stem}.qr.json").write_text(json.dumps({"staff_id": staff}))


def test_session_index_attributes_clips_between_markers(tmp_path: Path) -> None:
    day = tmp_path / "4313" / "2026-08-04"
    _stage(day, "GX010001.MP4", 100.0, staff="staff-A")  # marker A
    _stage(day, "GX010002.MP4", 200.0)
    _stage(day, "GX010003.MP4", 300.0)
    _stage(day, "GX010004.MP4", 400.0, staff="staff-B")  # marker B
    _stage(day, "GX010005.MP4", 500.0)

    index = build_session_index(day)
    assert index.identity_for(day / "GX010001.MP4") == ClipIdentity("staff-A", is_qr_marker=True)
    assert index.identity_for(day / "GX010002.MP4") == ClipIdentity("staff-A")
    assert index.identity_for(day / "GX010003.MP4") == ClipIdentity("staff-A")
    assert index.identity_for(day / "GX010004.MP4") == ClipIdentity("staff-B", is_qr_marker=True)
    assert index.identity_for(day / "GX010005.MP4") == ClipIdentity("staff-B")


def test_clips_before_first_marker_get_no_staff(tmp_path: Path) -> None:
    day = tmp_path / "4313" / "2026-08-04"
    _stage(day, "GX010001.MP4", 100.0)  # card started mid-session
    _stage(day, "GX010002.MP4", 200.0, staff="staff-A")
    _stage(day, "GX010003.MP4", 300.0)

    index = build_session_index(day)
    assert index.identity_for(day / "GX010001.MP4") == ClipIdentity(None)
    assert index.identity_for(day / "GX010003.MP4") == ClipIdentity("staff-A")


def test_session_index_with_no_markers(tmp_path: Path) -> None:
    day = tmp_path / "4313" / "2026-08-04"
    _stage(day, "GX010001.MP4", 100.0)
    assert build_session_index(day).identity_for(day / "GX010001.MP4") == ClipIdentity(None)


# --------------------------------------------------------------------------- #
# The identity resolver (what discovery consumes)
# --------------------------------------------------------------------------- #


def test_qr_identity_resolver_fills_role_from_resolve_for_staff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import discovery

    day = tmp_path / "4313" / "2026-08-04"
    _stage(day, "GX010001.MP4", 100.0, staff=_STAFF)
    _stage(day, "GX010002.MP4", 200.0)

    monkeypatch.setattr(
        discovery, "_probe_capture_time", lambda p, *, clock_tz=None: "2026-08-04T17:15:00Z"
    )

    class _Matcher:
        def resolve_for_staff(self, staff_id: str, captured_at: str) -> object:
            assert staff_id == _STAFF and captured_at == "2026-08-04T17:15:00Z"
            return type("R", (), {"role": "external"})()

    resolve = qr_identity_resolver(_Matcher())
    assert resolve("4313", str(day / "GX010002.MP4")) == ClipIdentity(_STAFF, role="external")
    # The marker itself never gets a role — it never becomes a job at all.
    marker = resolve("4313", str(day / "GX010001.MP4"))
    assert marker is not None and marker.is_qr_marker is True


def test_qr_identity_resolver_role_none_on_match_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import discovery
    from ingest.match import NoBookingMatch

    day = tmp_path / "4313" / "2026-08-04"
    _stage(day, "GX010001.MP4", 100.0, staff=_STAFF)
    _stage(day, "GX010002.MP4", 200.0)

    monkeypatch.setattr(
        discovery, "_probe_capture_time", lambda p, *, clock_tz=None: "2026-08-04T17:15:00Z"
    )

    class _Failing:
        def resolve_for_staff(self, staff_id: str, captured_at: str) -> object:
            raise NoBookingMatch("no load fits")

    resolve = qr_identity_resolver(_Failing())
    # The staff attribution survives; only the role stays unknown.
    assert resolve("4313", str(day / "GX010002.MP4")) == ClipIdentity(_STAFF)


def test_qr_identity_resolver_without_matcher(tmp_path: Path) -> None:
    day = tmp_path / "4313" / "2026-08-04"
    _stage(day, "GX010001.MP4", 100.0, staff=_STAFF)
    _stage(day, "GX010002.MP4", 200.0)

    resolve = qr_identity_resolver(None)
    assert resolve("4313", str(day / "GX010002.MP4")) == ClipIdentity(_STAFF)


def test_decode_frame_recovers_fisheye_distorted_code() -> None:
    """A barrel-distorted QR (GoPro wide lens) must still decode via the k1 sweep.

    Verified against real handcam footage: flat decode fails on every frame,
    undistortion at k1≈-0.25 reads it — this locks that behavior in synthetically.
    """
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from ingest.qr import _decode_qr_frame, _load_detectors

    params = cv2.QRCodeEncoder.Params()
    params.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_H
    code = cv2.QRCodeEncoder.create(params).encode(f"{QR_STAFF_PREFIX}{_STAFF}")
    scaled = cv2.resize(code, (600, 600), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((1080, 1920), 200, np.uint8)  # mid-gray room around the code
    canvas[240:840, 660:1260] = scaled

    # Simulate the lens: remap the flat image through positive barrel distortion.
    h, w = canvas.shape
    camera = np.array([[w * 0.7, 0, w / 2], [0, w * 0.7, h / 2], [0, 0, 1]], np.float64)
    dist = np.array([0.25, 0.0, 0.0, 0.0], np.float64)
    pts = np.dstack(np.meshgrid(np.arange(w), np.arange(h))).astype(np.float32).reshape(-1, 1, 2)
    warped_pts = cv2.undistortPoints(pts, camera, dist, P=camera).reshape(h, w, 2)
    distorted = cv2.remap(canvas, warped_pts[..., 0], warped_pts[..., 1],
                          cv2.INTER_LINEAR, borderValue=255)

    detectors = _load_detectors()
    # Sanity: the distorted frame must NOT decode flat (else this test proves nothing)...
    flat = next(
        (parse_staff_payload(det.detectAndDecode(distorted)[0]) for det in detectors), None
    )
    # ...but the sweep must recover it.
    assert _decode_qr_frame(distorted, detectors) == _STAFF, (
        f"undistortion sweep failed (flat decode gave {flat!r})"
    )
