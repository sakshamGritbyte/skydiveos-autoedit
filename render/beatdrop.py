"""Where the music "drops" — so the freefall cut can put the aircraft exit on it.

Client rule (2026-10-22): the freefall clip is never cut up in the middle; it may only be
shortened at its BEGINNING, and that is how the exit is lined up with the beat drop. So
the editor needs one number per backing track: the seconds into the track where the
drop lands. This module answers it, and the two pure functions below turn that number
into the freefall cut's pre-exit lead and the music offset the renderer applies.

Resolution, first answer wins:

1. **A sidecar** ``<track stem>.json`` beside the track — ``{"beat_drop": 31.2}`` pins
   the drop by hand, ``{"beat_drop": null}`` says "this track has no drop, don't sync".
   An operator's ear beats any heuristic, and a track without a real drop (a gradual
   rock build) is exactly where the heuristic is weakest. ``list_music`` only lists
   audio suffixes, so the sidecar is never mistaken for a track.
2. **Detection** (:func:`find_beat_drop`): the decoded track's low-band (< 150 Hz) and
   full-band energy, in ~46 ms frames. A drop is where the energy over the next few
   seconds jumps well above the energy over the previous few — the kick and bass coming
   in after a build. The EARLIEST rise nearly as big as the biggest one wins (the first
   drop is the one that belongs on the exit), snapped to the sharpest bass onset nearby
   so the cut lands on the hit itself, not somewhere inside a 4 s window. A track that
   starts from silence is not "a drop at the fade-in": the before-window must hold music
   throughout (no near-silent frame), so it can't straddle the file's silent head.

Never raises: music is optional in this pipeline, so any failure (no ffmpeg, an
unreadable file, a bad sidecar) is logged and answered with ``None`` — the freefall cut
then starts at the exit and the music plays from its start, exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Analysis sample rate (Hz) and frame hop (samples). 11025 Hz keeps the bass band
#: (< 150 Hz) and decodes a 4-minute track in well under a second; 512 samples ≈ 46 ms.
ANALYSIS_RATE = 11025
_HOP = 512
#: The band the kick + bass live in.
_BASS_HZ = 150.0
#: Seconds of energy compared before vs after a candidate drop.
_WINDOW_S = 4.0
#: Only the first stretch of a track is searched — the freefall cut leads with at most
#: a few seconds of door before the exit, so a drop two minutes in is never usable
#: without skipping most of the song anyway.
_SEARCH_MAX_S = 120.0
#: Minimum rise score (dB: bass rise + half the full-band rise) for a real drop. A track
#: with a steady level scores ~0; the library's tracks score 8-25 at their drop.
_MIN_RISE_DB = 6.0
#: A rise this fraction of the biggest one still counts — and the EARLIEST such wins.
_EARLIEST_FRACTION = 0.75
#: The before-window must be no quieter than this below the track's loud level (else it
#: is the silent head of the file, not a build), and the after-window no quieter than
#: this below it (a drop lands at full energy).
_PRE_FLOOR_DB = 30.0
#: A frame this far below the track's loud level is silence, not a quiet passage.
_SILENCE_DB = 50.0
_POST_FLOOR_DB = 12.0
#: How far from the window peak the onset snap may move the drop.
_SNAP_S = 0.75

#: Sidecar key.
SIDECAR_KEY = "beat_drop"
#: The most door/aircraft footage a freefall-only cut may carry before the exit. Enough
#: for an 8-bar build at 128 BPM (15 s); past it the music starts later instead (the exit
#: still lands on the drop, the track's head is skipped), so the freefall cut never turns
#: into a plane ride. This is the product expectation QA tests against (bug reopen
#: 2026-10-23: "at most 15 s of plane before the exit"). Override per deployment with
#: ``$FREEFALL_MAX_LEAD_S`` (seconds); read at use through :func:`freefall_max_lead_s`.
FREEFALL_MAX_LEAD_S = 15.0
_MAX_LEAD_ENV = "FREEFALL_MAX_LEAD_S"


def freefall_max_lead_s() -> float:
    """The lead cap in force: ``$FREEFALL_MAX_LEAD_S`` (>= 0), else
    :data:`FREEFALL_MAX_LEAD_S`. A value that isn't a non-negative number is logged and
    ignored — never raises, like everything else here."""
    raw = os.environ.get(_MAX_LEAD_ENV)
    if raw is None or not raw.strip():
        return FREEFALL_MAX_LEAD_S
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not (value >= 0.0):  # NaN fails this too
        logger.warning(
            "beat drop: %s=%r is not a non-negative number of seconds; using %.1f",
            _MAX_LEAD_ENV, raw, FREEFALL_MAX_LEAD_S,
        )
        return FREEFALL_MAX_LEAD_S
    return value


def sidecar_path(track: str | Path) -> Path:
    """``<track stem>.json`` beside the track."""
    p = Path(track)
    return p.with_name(f"{p.stem}.json")


def _read_sidecar(track: Path) -> tuple[bool, float | None]:
    """``(found, value)``. ``found`` with ``None`` means "this track has no drop"."""
    path = sidecar_path(track)
    if not path.is_file():
        return False, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        logger.warning("beat drop: unreadable sidecar %s (%r); detecting instead", path, e)
        return False, None
    if not isinstance(data, dict) or SIDECAR_KEY not in data:
        logger.warning(
            "beat drop: sidecar %s has no %r key; detecting instead", path, SIDECAR_KEY
        )
        return False, None
    value = data[SIDECAR_KEY]
    if value is None:
        return True, None
    try:
        drop = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "beat drop: sidecar %s value %r is not seconds; detecting instead", path, value
        )
        return False, None
    if drop < 0:
        logger.warning(
            "beat drop: sidecar %s value %r is negative; detecting instead", path, value
        )
        return False, None
    return True, drop


def decode_mono(track: str | Path, rate: int = ANALYSIS_RATE) -> Any:
    """Decode a track to mono float32 samples at ``rate`` (seam; mocked in tests)."""
    import numpy as np

    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(track),
            "-ac", "1", "-ar", str(rate), "-f", "f32le", "-",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def find_beat_drop(samples: Any, rate: int = ANALYSIS_RATE) -> float | None:
    """Seconds into ``samples`` where the music drops, or ``None`` if it never does.

    Pure (numpy only). See the module docstring for the rule; the constants above hold
    every threshold.
    """
    import numpy as np

    x = np.asarray(samples, dtype=np.float64)
    n = len(x) // _HOP
    w = int(round(_WINDOW_S * rate / _HOP))
    if n < 2 * w + 1:
        return None
    frames = x[: n * _HOP].reshape(n, _HOP) * np.hanning(_HOP)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(_HOP, 1.0 / rate)
    bass_p = spec[:, freqs < _BASS_HZ].sum(axis=1)
    full_p = spec.sum(axis=1)
    bass = 10.0 * np.log10(bass_p + 1e-10)
    fps = rate / _HOP

    def _db(v: Any) -> Any:
        return 10.0 * np.log10(v + 1e-10)

    # Window levels average POWER, then go to dB: a log average lets a fraction of a
    # second of digital silence (-100 dB) drag a whole window down, so a fade-in from
    # silence would read as a huge rise.
    cum_b = np.concatenate([[0.0], np.cumsum(bass_p)])
    cum_f = np.concatenate([[0.0], np.cumsum(full_p)])
    t = np.arange(w, min(n - w, int(_SEARCH_MAX_S * fps)) + 1)
    if len(t) == 0:
        return None
    pre_b, post_b = _db((cum_b[t] - cum_b[t - w]) / w), _db((cum_b[t + w] - cum_b[t]) / w)
    pre_f, post_f = _db((cum_f[t] - cum_f[t - w]) / w), _db((cum_f[t + w] - cum_f[t]) / w)
    full = _db(full_p)
    loud = float(np.percentile(full, 90))
    # Any near-silent frame in the before-window means it straddles the file's silent
    # head (or a full stop): the music starting is not a drop.
    silent = np.concatenate([[0], np.cumsum(full < loud - _SILENCE_DB)])
    no_silence_before = (silent[t] - silent[t - w]) == 0
    score = (post_b - pre_b) + 0.5 * (post_f - pre_f)
    usable = (
        no_silence_before
        & (pre_f > loud - _PRE_FLOOR_DB)
        & (post_f > loud - _POST_FLOOR_DB)
    )
    score = np.where(usable, score, -np.inf)
    best = float(score.max())
    if not np.isfinite(best) or best < _MIN_RISE_DB:
        return None

    # The earliest local peak that is nearly as big as the biggest rise.
    pick = None
    for i in range(len(t)):
        if score[i] < _EARLIEST_FRACTION * best:
            continue
        if score[i] >= score[max(0, i - w): i + w + 1].max():
            pick = int(t[i])
            break
    if pick is None:  # pragma: no cover - the global max is always a local peak
        pick = int(t[int(np.argmax(score))])

    # Snap to the sharpest bass onset near the window peak: the hit itself.
    flux = np.diff(bass, prepend=bass[0])
    snap = int(round(_SNAP_S * fps))
    lo, hi = max(1, pick - snap), min(n, pick + snap + 1)
    onset = lo + int(np.argmax(flux[lo:hi]))
    return round(onset / fps, 2)


@lru_cache(maxsize=64)
def _cached_drop(path: str, size: int, mtime_ns: int) -> float | None:
    found, value = _read_sidecar(Path(path))
    if found:
        return value
    return find_beat_drop(decode_mono(path))


def beat_drop_for(track: str | Path | None) -> float | None:
    """The drop (seconds into ``track``), or ``None`` — never raises.

    Cached per (path, size, mtime) for the sidecar too, so compose and render (which
    both ask) decode the track once, and replacing a track or editing its sidecar is
    picked up.
    """
    if not track:
        return None
    p = Path(track)
    try:
        st = p.stat()
        side = sidecar_path(p)
        side_mtime = side.stat().st_mtime_ns if side.is_file() else 0
        return _cached_drop(str(p), st.st_size, st.st_mtime_ns ^ side_mtime)
    except Exception as e:  # noqa: BLE001 - music is optional; never fail a job on it
        logger.warning("beat drop: could not analyse %s (%r); no exit sync", p, e)
        return None


def plan_exit_lead(drop: float | None, available_lead: float, max_lead: float) -> float:
    """How many seconds before the exit the freefall cut should START.

    Ideally exactly ``drop`` — the music then plays from its first second and its drop
    lands on the exit. Bounded by the footage that exists before the exit and by
    ``max_lead`` (the freefall cut is the jump, not the plane ride). No drop → 0: the
    cut starts at the exit, as before. Whatever this cannot cover, the music offset
    (:func:`music_offset_for`) makes up by starting the track later.
    """
    if drop is None or drop <= 0:
        return 0.0
    return round(max(0.0, min(drop, available_lead, max_lead)), 3)


def music_offset_for(drop: float | None, exit_output_time: float | None) -> float:
    """Seconds into the track the music should be at output time 0.

    ``drop - exit_output_time``: positive skips the track's head (the cut's lead is
    shorter than the build-up), negative delays the music (the lead is LONGER than the
    build-up — only a hand-edited EDL does that). 0 when there is nothing to sync: no
    drop, or the exit is not in the cut.
    """
    if drop is None or exit_output_time is None:
        return 0.0
    return round(drop - exit_output_time, 3)
