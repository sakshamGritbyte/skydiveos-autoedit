"""Skydive phase segmentation from GoPro telemetry.

Turns decoded GPMF sensor streams into the jump timeline:

    plane_boarding, exit, freefall_start, freefall_end,
    deployment, canopy_start, landing, landing_end

Physics we lean on (the accelerometer reports *proper* acceleration / specific
force, i.e. gravity is read as ~1 g (9.8 m/s²) while sitting still, and ~0 g in
true free fall):

* In the plane the rig sits at a steady ~1 g.
* At **exit** the jumper leaves the aircraft and proper acceleration collapses
  toward 0 g — the cleanest, most reliable marker in the whole jump.
* As drag builds, proper acceleration climbs back toward ~1 g at terminal
  velocity (sustained free fall).
* At **deployment** the canopy opening shock produces a large, *sustained*
  deceleration (several g held for a few seconds), unlike the brief spikes from
  tumbling during free fall.
* **landing** is detected from GPS/altitude reaching ground level — only when an
  altitude stream is present.

Any phase we cannot detect confidently is returned as ``None`` so the caller can
fall back to human-labeled ground truth (see ``metadata.extract``).

All thresholds are expressed in SI units (m/s²) and seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .gpmf import GpmfData, StreamSamples

G = 9.80665  # 1 g in m/s²

# Detection thresholds (m/s²) on per-payload mean proper-acceleration magnitude.
_PLANE_G = 0.75 * G      # steady cruise reads ~>0.75 g
_FREEFALL_DIP = 0.80 * G  # exit collapses mean magnitude below this
_TERMINAL_G = 0.80 * G    # drag rebuilds toward ~1 g once falling
_DEPLOY_G = 1.55 * G      # opening shock sustains well above 1 g
_DEPLOY_HOLD = 3          # ...for at least this many consecutive payloads
_GROUND_MARGIN = 8.0      # metres above stream-min altitude counted as "ground"
_MIN_DESCENT = 100.0      # require this much altitude drop to trust a landing fix


@dataclass
class Segmentation:
    """Detected timeline. ``None`` means "not detectable from this telemetry"."""

    plane_boarding: float | None = None
    exit: float | None = None
    freefall_start: float | None = None
    freefall_end: float | None = None
    deployment: float | None = None
    canopy_start: float | None = None
    landing: float | None = None
    landing_end: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "plane_boarding": self.plane_boarding,
            "exit": self.exit,
            "freefall_start": self.freefall_start,
            "freefall_end": self.freefall_end,
            "deployment": self.deployment,
            "canopy_start": self.canopy_start,
            "landing": self.landing,
            "landing_end": self.landing_end,
        }


def _magnitude_means(stream: StreamSamples) -> tuple[list[float], list[float]]:
    """Per-payload (mean magnitude, time) of a 3-axis sensor stream."""
    means: list[float] = []
    times: list[float] = []
    for samples, t in zip(stream.payloads, stream.times, strict=False):
        if not samples:
            continue
        mags = [math.sqrt(sum(c * c for c in s[:3])) for s in samples]
        means.append(sum(mags) / len(mags))
        times.append(t)
    return means, times


def _detect_exit_and_freefall(
    accl: StreamSamples,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Detect exit, freefall_start and freefall_end from accelerometer magnitude.

    Returns (exit, freefall_start, freefall_end, deploy_search_floor_index_time).
    """
    means, times = _magnitude_means(accl)
    if len(means) < 5:
        return None, None, None, None

    # --- exit: first sustained-1g -> sub-g collapse ----------------------------
    exit_i: int | None = None
    for i in range(1, len(means)):
        # require we were recently in steady ~1 g flight before the collapse
        recent = means[max(0, i - 3):i]
        was_plane = recent and (sum(recent) / len(recent)) > _PLANE_G
        if was_plane and means[i] < _FREEFALL_DIP:
            exit_i = i
            break
    if exit_i is None:
        return None, None, None, None

    # --- freefall_start: drag rebuilds magnitude back toward ~1 g --------------
    ff_start_i = exit_i
    for i in range(exit_i + 1, len(means)):
        if means[i] >= _TERMINAL_G:
            ff_start_i = i
            break

    # --- deployment: first sustained high-g window after free fall -------------
    deploy_i: int | None = None
    for i in range(ff_start_i + 1, len(means) - _DEPLOY_HOLD + 1):
        window = means[i:i + _DEPLOY_HOLD]
        if all(m > _DEPLOY_G for m in window):
            deploy_i = i
            break

    # --- freefall_end: last free-fall payload before the opening shock ---------
    ff_end_i = (deploy_i - 1) if deploy_i is not None else (len(means) - 1)

    exit_t = times[exit_i]
    ff_start_t = times[ff_start_i]
    ff_end_t = times[ff_end_i] if 0 <= ff_end_i < len(times) else None
    deploy_t = times[deploy_i] if deploy_i is not None else None
    return exit_t, ff_start_t, ff_end_t, deploy_t


def _detect_landing(gps: StreamSamples | None) -> tuple[float | None, float | None]:
    """Detect landing/landing_end from GPS altitude returning to ground level.

    GPS5 components are [lat, lon, altitude_m, speed_2d, speed_3d]; GPS9 leads
    with the same first three. Without an altitude-bearing stream we cannot judge
    ground level, so we return (None, None) and let the caller fall back.
    """
    if gps is None:
        return None, None
    alts: list[tuple[float, float]] = []  # (time, altitude)
    for samples, t in zip(gps.payloads, gps.times, strict=False):
        for k, s in enumerate(samples):
            if len(s) >= 3:
                # spread sub-payload samples evenly across the ~1 s window
                frac = k / len(samples)
                alts.append((t + frac, s[2]))
    if len(alts) < 5:
        return None, None

    # A landing only makes sense if the stream actually captured a descent. A
    # near-flat altitude trace (e.g. a short ground clip) gives us no way to tell
    # ground from sky, so defer to ground truth instead of inventing a fix.
    if max(a for _, a in alts) - min(a for _, a in alts) < _MIN_DESCENT:
        return None, None

    ground = min(a for _, a in alts)
    # landing: first time we settle to within margin of the lowest altitude and
    # stay there (descent has ended).
    landing_t: float | None = None
    for i, (t, a) in enumerate(alts):
        if a <= ground + _GROUND_MARGIN:
            tail = alts[i:]
            if all(av <= ground + _GROUND_MARGIN for _, av in tail):
                landing_t = t
                break
    if landing_t is None:
        return None, None
    landing_end_t = alts[-1][0]
    return landing_t, landing_end_t


def segment(gpmf: GpmfData) -> Segmentation:
    """Run all detectors over decoded telemetry and assemble the timeline."""
    seg = Segmentation()

    accl = gpmf.get("ACCL")
    if accl is not None:
        exit_t, ff_start_t, ff_end_t, deploy_t = _detect_exit_and_freefall(accl)
        seg.exit = exit_t
        seg.freefall_start = ff_start_t
        seg.freefall_end = ff_end_t
        seg.deployment = deploy_t
        if deploy_t is not None:
            # canopy ride begins once the opening shock settles (~4 s of inflation)
            seg.canopy_start = round(deploy_t + 4.0, 3)

    gps = gpmf.get("GPS9") or gpmf.get("GPS5")
    seg.landing, seg.landing_end = _detect_landing(gps)

    # plane_boarding has no distinct accelerometer signature (it is just the
    # quiet pre-takeoff/climb period); we deliberately leave it None and defer to
    # ground truth rather than emit a fabricated timestamp.
    return seg
