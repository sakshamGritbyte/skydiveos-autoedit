"""Tests for the Raw Footage web proxies (api/rawproxy.py).

FFmpeg and ffprobe are faked via the injectable ``runner`` / ``prober``, so these run
offline. The contracts: an HEVC (or oversize, or 10-bit) master gets ONE H.264 proxy
written atomically under ``raw-web/``; an H.264 master at or under the cap gets a
``.native`` marker and no transcode; a failed transcode is recorded (and not retried
in a loop) while the other masters still get theirs; and every decision is re-derived
from disk, so a second pass does no work.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from api.jobs import Job, JobStore
from api.rawproxy import (
    DISPATCH_MARKER,
    FAILED_SUFFIX,
    NATIVE_SUFFIX,
    PARTIAL_SUFFIX,
    RAW_WEB_DIRNAME,
    REDISPATCH_AFTER_S,
    RawProxyError,
    VideoStream,
    is_browser_native,
    iter_raw_masters,
    mark_dispatched,
    needs_dispatch,
    pending_masters,
    playback_for,
    proxy_command,
    proxy_path,
    render_job_raw_proxies,
    render_web_proxy,
)

from .test_delivery import _settings

HEVC_4K = VideoStream(codec="hevc", height=2160, pix_fmt="yuv420p")
H264_1080 = VideoStream(codec="h264", height=1080, pix_fmt="yuv420p")


class FakeFFmpeg:
    """Records the commands it was handed and writes each output file."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, cmd: list[str]) -> None:
        self.commands.append(cmd)
        if self.fail_on and self.fail_on in cmd[cmd.index("-i") + 1]:
            raise RawProxyError("boom")
        Path(cmd[-1]).write_bytes(b"fake-proxy-bytes")


def _job(store: JobStore, **fields: object) -> Job:
    base: dict[str, object] = {"job_id": "j1", "addons": {"raw": "clover_txn"}}
    base.update(fields)
    return store.create(Job(**base))  # type: ignore[arg-type]


def _stage(store: JobStore, job_id: str, *rels: str) -> None:
    for rel in rels:
        p = store.raw_dir(job_id) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MASTER-" + rel.encode())


def _prober(table: dict[str, VideoStream]):
    calls: list[str] = []

    def probe(path: Path) -> VideoStream:
        calls.append(path.name)
        return table.get(path.name, HEVC_4K)

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


# --------------------------------------------------------------------------- #
# Playability decision
# --------------------------------------------------------------------------- #


def test_browser_native_needs_h264_yuv420p_and_within_the_cap() -> None:
    assert is_browser_native(H264_1080, max_height=1080)
    assert is_browser_native(VideoStream("h264", 720, "yuvj420p"), max_height=1080)
    # HEVC is what every GoPro writes — the whole reason this module exists.
    assert not is_browser_native(HEVC_4K, max_height=1080)
    assert not is_browser_native(VideoStream("hevc", 1080, "yuv420p"), max_height=1080)
    # 4K H.264 streams badly and gets capped; 10-bit H.264 is not decodable in Chrome.
    assert not is_browser_native(VideoStream("h264", 2160, "yuv420p"), max_height=1080)
    assert not is_browser_native(VideoStream("h264", 1080, "yuv420p10le"), max_height=1080)
    # An unreadable probe is NOT native: unknown → transcode (the safe direction).
    assert not is_browser_native(VideoStream(None, None, None), max_height=1080)
    assert not is_browser_native(VideoStream("h264", None, "yuv420p"), max_height=1080)
    assert not is_browser_native(VideoStream("h264", 0, "yuv420p"), max_height=1080)
    # The cap is a setting: a 720p-capped dropzone re-encodes a 1080p master.
    assert not is_browser_native(H264_1080, max_height=720)


def test_proxy_command_maps_only_picture_and_sound_and_caps_the_height() -> None:
    cmd = proxy_command(Path("/in/GX010052.MP4"), Path("/out/GX010052.MP4"), max_height=1080)
    assert cmd[0] == "ffmpeg" and cmd[-1] == "/out/GX010052.MP4"
    # GPMF telemetry / timecode data streams would fail the MP4 muxer: video 0 + audio only.
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "0:a?" in cmd and "-dn" in cmd and "-sn" in cmd
    assert cmd[cmd.index("-vf") + 1] == "scale=-2:'min(1080,ih)'"  # never upscale, even width
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"  # 10-bit HEVC must come out 8-bit
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "+faststart" in cmd
    assert "drawtext" not in " ".join(cmd)


def test_iter_raw_masters_lists_mp4s_at_any_depth_and_skips_lrv_proxies(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "external").mkdir(parents=True)
    (raw / "GX010052.MP4").write_bytes(b"a")
    (raw / "GX010052.LRV").write_bytes(b"lrv")
    (raw / "external" / "GX020001.mp4").write_bytes(b"b")
    (raw / "notes.txt").write_bytes(b"x")
    assert [p.relative_to(raw).as_posix() for p in iter_raw_masters(raw)] == [
        "GX010052.MP4", "external/GX020001.mp4",
    ]
    assert list(iter_raw_masters(tmp_path / "missing")) == []


# --------------------------------------------------------------------------- #
# The transcode
# --------------------------------------------------------------------------- #


def test_render_web_proxy_writes_a_partial_then_renames(tmp_path: Path) -> None:
    src = tmp_path / "GX010052.MP4"
    src.write_bytes(b"master")
    out = tmp_path / RAW_WEB_DIRNAME / "GX010052.MP4"
    ff = FakeFFmpeg()

    assert render_web_proxy(src, out, max_height=1080, runner=ff) == out
    assert out.read_bytes() == b"fake-proxy-bytes"
    # ffmpeg wrote to the .part name; the finished file was renamed into place and the
    # partial is gone — a half-written proxy can never be served.
    written = Path(ff.commands[0][-1])
    assert PARTIAL_SUFFIX in written.name and not written.exists()
    assert ff.commands[0][ff.commands[0].index("-i") + 1] == str(src)


def test_render_web_proxy_surfaces_a_failed_transcode_and_leaves_no_partial(
    tmp_path: Path,
) -> None:
    src = tmp_path / "GX010052.MP4"
    src.write_bytes(b"master")
    out = tmp_path / RAW_WEB_DIRNAME / "GX010052.MP4"

    def boom(cmd: list[str]) -> None:
        Path(cmd[-1]).write_bytes(b"half")  # ffmpeg died mid-write
        raise RawProxyError("boom")

    try:
        render_web_proxy(src, out, max_height=1080, runner=boom)
    except RawProxyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected the transcode failure to propagate")
    assert not out.exists()
    assert list(out.parent.iterdir()) == []


# --------------------------------------------------------------------------- #
# The per-job pass
# --------------------------------------------------------------------------- #


def test_job_pass_transcodes_hevc_marks_native_and_records_a_failure(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = _job(store)
    _stage(store, job.job_id, "GX010052.MP4", "external/GX020001.MP4", "external/GX020002.MP4")
    (store.raw_dir(job.job_id) / "GX010052.LRV").write_bytes(b"lrv")  # never a product
    ff = FakeFFmpeg(fail_on="GX020002")
    probe = _prober({"GX020001.MP4": H264_1080})

    decided = render_job_raw_proxies(job, store, _settings(), runner=ff, prober=probe)

    assert decided == {
        "GX010052.MP4": "proxy",
        "external/GX020001.MP4": "native",
        "external/GX020002.MP4": "failed",
    }
    web = store.dir(job.job_id) / RAW_WEB_DIRNAME
    assert (web / "GX010052.MP4").read_bytes() == b"fake-proxy-bytes"
    assert (web / "external" / ("GX020001.MP4" + NATIVE_SUFFIX)).is_file()
    assert not (web / "external" / "GX020001.MP4").exists()  # no redundant re-encode
    assert "boom" in (web / "external" / ("GX020002.MP4" + FAILED_SUFFIX)).read_text()
    # Only the two non-native masters reached ffmpeg; the LRV was never probed.
    assert len(ff.commands) == 2
    assert sorted(probe.calls) == ["GX010052.MP4", "GX020001.MP4", "GX020002.MP4"]
    assert pending_masters(store, job) == []
    assert playback_for(store, job.job_id, "GX010052.MP4") == "proxy"
    assert playback_for(store, job.job_id, "external/GX020001.MP4") == "native"
    assert playback_for(store, job.job_id, "external/GX020002.MP4") == "failed"

    # A second pass is a directory scan: every master is already decided (the failed
    # one included — a broken master must not be retried on every poll).
    ff2 = FakeFFmpeg()
    probe2 = _prober({})
    assert render_job_raw_proxies(job, store, _settings(), runner=ff2, prober=probe2) == decided
    assert ff2.commands == [] and probe2.calls == []


def test_job_pass_is_gated_on_the_purchase_and_the_flag(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    _stage(store, "j1", "GX010052.MP4")
    ff = FakeFFmpeg()
    probe = _prober({})

    unpaid = _job(store, addons={})
    assert render_job_raw_proxies(unpaid, store, _settings(), runner=ff, prober=probe) == {}
    paid = store.update("j1", addons={"raw": "txn"})
    off = _settings(raw_web_proxies=False)
    assert render_job_raw_proxies(paid, store, off, runner=ff, prober=probe) == {}
    assert ff.commands == [] and probe.calls == []
    assert not (store.dir("j1") / RAW_WEB_DIRNAME).exists()

    # On, paid: the cap from settings reaches the ffmpeg filter.
    render_job_raw_proxies(paid, store, _settings(raw_web_max_height=720), runner=ff, prober=probe)
    assert ff.commands[0][ff.commands[0].index("-vf") + 1] == "scale=-2:'min(720,ih)'"


def test_a_re_uploaded_master_invalidates_its_old_proxy(tmp_path: Path) -> None:
    """Freshness is by mtime: a proxy older than its master is stale, not done."""
    store = JobStore(tmp_path)
    job = _job(store)
    _stage(store, job.job_id, "GX010052.MP4")
    out = proxy_path(store, job.job_id, "GX010052.MP4")
    out.parent.mkdir(parents=True)
    out.write_bytes(b"old-proxy")
    stale = time.time() - 600
    os.utime(out, (stale, stale))

    assert playback_for(store, job.job_id, "GX010052.MP4") == "pending"
    assert pending_masters(store, job) == ["GX010052.MP4"]
    ff = FakeFFmpeg()
    render_job_raw_proxies(job, store, _settings(), runner=ff, prober=_prober({}))
    assert len(ff.commands) == 1 and out.read_bytes() == b"fake-proxy-bytes"
    assert playback_for(store, job.job_id, "GX010052.MP4") == "proxy"


# --------------------------------------------------------------------------- #
# Dispatch bookkeeping (the gallery's self-heal)
# --------------------------------------------------------------------------- #


def test_needs_dispatch_only_while_pending_and_outside_the_window(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = _job(store)
    # Nothing staged: nothing to do.
    assert not needs_dispatch(store, job)
    _stage(store, job.job_id, "GX010052.MP4")
    assert needs_dispatch(store, job)
    # Not purchased: never.
    assert not needs_dispatch(store, store.update(job.job_id, addons={}), )
    job = store.update(job.job_id, addons={"raw": "txn"})

    mark_dispatched(store, job.job_id)
    marker = store.dir(job.job_id) / RAW_WEB_DIRNAME / DISPATCH_MARKER
    assert marker.is_file()
    now = marker.stat().st_mtime
    assert not needs_dispatch(store, job, now=now + 5)
    assert needs_dispatch(store, job, now=now + REDISPATCH_AFTER_S + 1)

    # Decided (a proxy landed): no dispatch however old the marker.
    proxy_path(store, job.job_id, "GX010052.MP4").write_bytes(b"proxy")
    assert not needs_dispatch(store, job, now=now + REDISPATCH_AFTER_S + 1)
