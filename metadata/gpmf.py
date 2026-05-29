"""Minimal, dependency-free GPMF parser for GoPro MP4 files.

GoPro embeds telemetry (accelerometer, gyroscope, GPS, ...) in a ``gpmd`` data
track inside the MP4, encoded as nested KLV (key-length-value) records. There is
no maintained Python binding shipped in ``vendor/OpenGoPro/demos`` (only a
WiFi-streaming fetch helper), and the upstream ``gpmf-parser`` is a C library, so
we parse the KLV stream directly here.

We rely on FFmpeg (already a hard project dependency, see CLAUDE.md) to demux the
``gpmd`` track out of the MP4 and to recover per-payload presentation timestamps.
The KLV records are self-delimiting, so the concatenated payloads parse cleanly.

All timestamps are in seconds (float), matching the project-wide convention.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# GPMF type char -> (struct format, byte size). Only the numeric/sensor types we
# care about are listed; nested ('\x00') and anything unlisted is treated opaquely.
_TYPES: dict[str, tuple[str, int]] = {
    "b": ("b", 1),  # int8
    "B": ("B", 1),  # uint8
    "s": (">h", 2),  # int16
    "S": (">H", 2),  # uint16
    "l": (">l", 4),  # int32
    "L": (">L", 4),  # uint32
    "f": (">f", 4),  # float32
    "d": (">d", 8),  # float64
    "j": (">q", 8),  # int64
    "J": (">Q", 8),  # uint64
}

# FourCCs that carry sensor sample arrays we decode into floats.
_SENSOR_KEYS = {b"ACCL", b"GYRO", b"GRAV", b"GPS5", b"GPS9", b"MAGN"}


class GPMFError(RuntimeError):
    """Raised when the GPMF track cannot be located or demuxed."""


@dataclass
class StreamSamples:
    """All decoded samples for one sensor FourCC across the whole file.

    ``payloads`` holds one entry per GPMF payload (≈1 s of data); each entry is a
    list of N-component samples (already SCAL-divided into physical units).
    ``times`` is the parallel list of payload start timestamps in seconds.
    """

    fourcc: str
    payloads: list[list[tuple[float, ...]]] = field(default_factory=list)
    times: list[float] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(self.payloads)


@dataclass
class GpmfData:
    """Parsed telemetry for a jump, keyed by sensor FourCC."""

    streams: dict[str, StreamSamples]
    duration_s: float

    def get(self, fourcc: str) -> StreamSamples | None:
        s = self.streams.get(fourcc)
        return s if s and not s.is_empty() else None


def _ffprobe_gpmd_stream_index(mp4_path: str) -> int:
    """Return the stream index of the ``gpmd`` (GPMF) data track."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "d",
            "-show_entries", "stream=index:stream_tags=handler_name",
            "-show_entries", "stream=index,codec_tag_string",
            "-of", "csv=p=0",
            mp4_path,
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[1] == "gpmd":
            return int(parts[0])
    raise GPMFError(f"no gpmd (GPMF) data track found in {mp4_path}")


def _packet_times(mp4_path: str, stream_index: int) -> list[float]:
    """Per-payload presentation timestamps (seconds), one per gpmd packet."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", str(stream_index),
            "-show_entries", "packet=pts_time",
            "-of", "csv=p=0",
            mp4_path,
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    times: list[float] = []
    for line in out.splitlines():
        line = line.strip()
        if line and line != "N/A":
            times.append(float(line))
    return times


def _demux_gpmf(mp4_path: str, stream_index: int) -> bytes:
    """Copy the raw gpmd track bytes out of the MP4 via FFmpeg."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", mp4_path,
            "-map", f"0:{stream_index}",
            "-c", "copy",
            "-f", "rawvideo",
            "pipe:1",
        ],
        capture_output=True, check=True,
    )
    return proc.stdout


def _read_scal(data: bytes, fmt: str, size: int) -> list[float]:
    n = len(data) // size
    return [float(struct.unpack(fmt, data[i * size:(i + 1) * size])[0]) for i in range(n)] or [1.0]


def _decode_samples(
    data: bytes, type_char: str, struct_size: int, repeat: int, scal: list[float]
) -> list[tuple[float, ...]]:
    """Decode one sensor record into a list of SCAL-divided component tuples."""
    if type_char not in _TYPES:
        return []
    fmt, tsize = _TYPES[type_char]
    comps = struct_size // tsize
    if comps == 0:
        return []
    samples: list[tuple[float, ...]] = []
    for r in range(repeat):
        base = r * struct_size
        vals: list[float] = []
        for c in range(comps):
            off = base + c * tsize
            raw = struct.unpack(fmt, data[off:off + tsize])[0]
            div = scal[c] if c < len(scal) else scal[-1]
            vals.append(raw / div if div else float(raw))
        samples.append(tuple(vals))
    return samples


def _parse_klv(buf: bytes, off: int, end: int, scal: list[float], out: dict[str, list]) -> None:
    """Recursively walk KLV records, collecting sensor samples by FourCC.

    ``scal`` is the SCAL divisor list in effect for the current STRM scope; it is
    threaded down so each sensor record is decoded with its own scaling.
    """
    while off + 8 <= end:
        key = buf[off:off + 4]
        type_char = chr(buf[off + 4])
        struct_size = buf[off + 5]
        repeat = struct.unpack(">H", buf[off + 6:off + 8])[0]
        data_len = struct_size * repeat
        body = off + 8
        if type_char == "\x00":  # nested container (DEVC, STRM, ...) -> recurse
            _parse_klv(buf, body, body + data_len, list(scal), out)
        else:
            data = buf[body:body + data_len]
            if key == b"SCAL":
                fmt, size = _TYPES.get(type_char, (">l", 4))
                scal[:] = _read_scal(data, fmt, size)
            elif key in _SENSOR_KEYS:
                samples = _decode_samples(data, type_char, struct_size, repeat, scal)
                if samples:
                    out.setdefault(key.decode(), []).append(samples)
        # advance past data, padded up to a 4-byte boundary
        off = body + data_len + ((4 - data_len % 4) % 4)


def parse_gpmf(mp4_path: str) -> GpmfData:
    """Extract and decode all supported telemetry streams from a GoPro MP4."""
    path = Path(mp4_path)
    if not path.exists():
        raise GPMFError(f"file not found: {mp4_path}")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise GPMFError(f"{tool} not found on PATH (required to demux GPMF)")

    idx = _ffprobe_gpmd_stream_index(mp4_path)
    times = _packet_times(mp4_path, idx)
    raw = _demux_gpmf(mp4_path, idx)

    # Walk top-level DEVC payloads one at a time so we can tag each with its
    # packet timestamp. Per-FourCC samples accumulate in `per_payload`.
    streams: dict[str, StreamSamples] = {}
    off = 0
    payload_idx = 0
    n = len(raw)
    while off + 8 <= n:
        key = raw[off:off + 4]
        type_char = chr(raw[off + 4])
        struct_size = raw[off + 5]
        repeat = struct.unpack(">H", raw[off + 6:off + 8])[0]
        data_len = struct_size * repeat
        body = off + 8
        if key == b"DEVC" and type_char == "\x00":
            collected: dict[str, list] = {}
            _parse_klv(raw, body, body + data_len, [1.0], collected)
            t = times[payload_idx] if payload_idx < len(times) else float(payload_idx)
            for fourcc, payload_lists in collected.items():
                # flatten the (possibly multiple) records of this fourcc in the payload
                flat = [s for rec in payload_lists for s in rec]
                ss = streams.setdefault(fourcc, StreamSamples(fourcc))
                ss.payloads.append(flat)
                ss.times.append(t)
            payload_idx += 1
        off = body + data_len + ((4 - data_len % 4) % 4)

    duration = times[-1] if times else float(payload_idx)
    return GpmfData(streams=streams, duration_s=duration)
