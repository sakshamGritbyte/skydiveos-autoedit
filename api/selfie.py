"""The selfie-package pipeline: many raw GoPro clips -> three edits + photos.

The "selfie cam" product gives the customer a handful of separately-recorded GoPro
clips for one jump (the boarding chat, the climb, the freefall, the canopy ride,
the post-jump interview) rather than a single continuous master. This module turns
that pile of files into the customer's deliverables, in five steps:

1. **Classify** each clip into a *scene* from its GPMF telemetry (altitude + the
   vertical accelerometer), then concat same-scene clips into one ``scene.mp4`` and
   write a ``scene_manifest.json``.
2. **Score** every scene per-second with MediaPipe (reusing /analysis).
3. **Compose** three EDLs in one Claude call — a full edit, a 90 s highlights cut,
   and a freefall-only cut.
4. **Render** those three EDLs to MP4 in parallel.
5. **Extract** the best freefall/canopy frames as printable photos.

Like the rest of the pipeline this stage runs off the request path on a Celery
worker (:func:`api.tasks.process_selfie_package`); everything here is a plain,
testable function. Each external boundary (GPMF extraction, ffprobe, FFmpeg,
MediaPipe, the Claude call) is a module-level seam so tests run fully offline by
monkeypatching it — mirroring how /edl injects its Claude client and /analysis
isolates its FFmpeg frame pull.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from analysis.proxy import analysis_source
from edl.storage import job_dir

if TYPE_CHECKING:  # types used only for annotation, never imported at runtime
    from anthropic.types import MessageParam

logger = logging.getLogger(__name__)

# Pinned EDL model for the selfie/ultimum compose call — same pin as /edl
# (CLAUDE.md → "AI decisioning: Claude API (claude-sonnet-4-6)"). The previous dated
# snapshot (claude-sonnet-4-20250514) is deprecated and now 404s, so use the alias.
CLAUDE_MODEL = "claude-sonnet-4-6"
_COMPOSE_MAX_TOKENS = 2000

# Frame sampling rates (CLAUDE.md: all timestamps in seconds, float).
SCORE_FPS = 5.0
PHOTO_FPS = 1.0

# Photo deliverable: deliver as many strong, de-duplicated photos as the footage
# yields (aim 55+), spread across the whole experience, up to a sane upper bound.
MIN_PHOTOS_AIM = 60
MAX_PHOTOS = 120
_PHOTO_MIN_GAP_S = 1.5   # min seconds between two kept photos of one scene (anti-dupe)
_PHOTO_MIN_VISIBLE = 0.3  # a face must be at least this in-frame to be a candidate

# Photo-only package: photos are the *sole* deliverable, so we aim for a fuller set
# than the selfie package's — 90–100 best moments across the whole jump. We pull right
# up to the target and tighten the anti-dupe gap so the pool is wide enough to reach it.
PHOTO_ONLY_TARGET = 140
_PHOTO_ONLY_MIN_GAP_S = 1.0

# Selfie/external package photo set: deliver ~50 strong stills. The camera-flyer that
# shoots the "external" product films the tandem from a distance, so faces read smaller
# and many frames fall under the default in-frame floor — we relax that floor and tighten
# the anti-dupe gap to widen the candidate pool enough to reach the target on real footage.
SELFIE_PHOTO_TARGET = 50
_SELFIE_PHOTO_MIN_GAP_S = 1.0
_SELFIE_PHOTO_MIN_VISIBLE = 0.15

# Slow-motion: 0.4x ramps applied to short, high-value moments (never whole scenes).
SLOWMO_SPEED = 0.4
_SLOWMO_LEN = 1.0          # seconds of source per slow-mo segment
_FREEFALL_SLOWMO_PEAKS = 3  # how many freefall smile peaks to feature in slow-mo
_PEAK_MIN_GAP_S = 3.0     # spacing between featured peaks so they aren't adjacent

# Scene ordering for the full-video edit, and the per-scene weighting the compose
# step uses to budget runtime toward the scenes that matter most.
SCENE_ORDER: tuple[str, ...] = (
    "intro_interview",
    "boarding",
    "takeoff",
    "plane",
    "freefall",
    "canopy",
    "outro_interview",
)
SCENE_WEIGHTS: dict[str, float] = {
    "freefall": 1.0,
    "canopy": 0.8,
    "intro_interview": 0.6,
    "outro_interview": 0.6,
    "boarding": 0.4,
    "takeoff": 0.4,
    "plane": 0.3,
}
#: The scenes a clip may be labelled — used to validate manual overrides.
VALID_SCENES: frozenset[str] = frozenset(SCENE_ORDER)
#: Optional per-job file mapping a raw filename to its scene, dropped in the job dir
#: to override telemetry classification when GPMF is missing/ambiguous. Mirrors the
#: ground-truth ``sample-data/labels.json`` the single-master pipeline falls back to.
SCENE_LABELS_FILENAME = "scene_labels.json"
#: Optional per-job file listing time ranges (seconds) to CUT from each scene across
#: every deliverable — ``{"intro_interview": [[20, 40]], "boarding": [[5, 15]]}``.
EXCLUDE_FILENAME = "exclude.json"

#: The two camera sources the Ultimate package combines. Each uploads into its own
#: ``raw/<role>/`` subdir (two GoPros emit colliding filenames). Order is the upload /
#: classification order; it does not imply edit priority.
CAMERA_ROLES: tuple[str, ...] = ("instructor", "external")


# --------------------------------------------------------------------------- #
# Errors + the EDL contract Claude must satisfy
# --------------------------------------------------------------------------- #


class SelfieError(RuntimeError):
    """Raised when the selfie pipeline cannot produce its deliverables."""


class LowConfidenceError(SelfieError):
    """Raised when too many clips can't be confidently classified into a scene."""


class Clip(BaseModel):
    """One scene-relative cut in a selfie EDL (distinct from :class:`edl.schema.Clip`).

    Times are seconds on the *scene* MP4 named by ``scene`` (each scene is its own
    concatenated file), so a clip names where it comes from as well as its window.

    ``camera`` names which camera's scene set the clip is cut from, used only by the
    Ultimate **combo** (full video + highlights), which interleaves both cameras: a clip
    tagged ``"external"`` resolves to that camera's ``scenes_external/<scene>.mp4`` rather
    than the shared scene file. ``None`` (the default, every single-camera package) keeps
    the original behaviour — the clip resolves to the plain ``scene`` name.
    """

    model_config = ConfigDict(extra="forbid")

    scene: str
    src_start: float = Field(ge=0.0)
    src_end: float = Field(gt=0.0)
    speed_multiplier: float = Field(default=1.0, gt=0.0)
    #: Camera role this clip is cut from (``"instructor"`` / ``"external"``); ``None`` for
    #: single-camera packages. Drives scene-file resolution for the multi-cam combo.
    camera: str | None = None


def _scene_key(clip: Clip) -> str:
    """The ``scene_paths`` key a clip resolves to: camera-scoped for the multi-cam combo
    (``"external/freefall"``), the bare scene name otherwise (single-camera packages)."""
    return f"{clip.camera}/{clip.scene}" if clip.camera else clip.scene


class EDLResponse(BaseModel):
    """The three edits Claude returns for one selfie jump, in one JSON object."""

    model_config = ConfigDict(extra="forbid")

    full_video: list[Clip] = Field(min_length=1)
    highlights: list[Clip] = Field(min_length=1)
    freefall: list[Clip] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Telemetry signals (Step 1 inputs)
# --------------------------------------------------------------------------- #


@dataclass
class GpmfSignals:
    """The GPMF-derived signals scene classification keys off, per clip.

    Altitude drives classification when GPS is present. Many GoPros record with GPS
    off (no satellite lock), leaving no altitude at all — then we fall back to the
    accelerometer *magnitude* stats, whose freefall signature (a near-0 g exit dip
    plus heavy 120 mph buffeting → high variance) is unmistakable without any GPS.
    """

    altitude_mean: float
    altitude_first: float
    altitude_last: float
    altitude_delta: float
    accl_z_mean: float
    accl_z_std: float
    #: Accelerometer magnitude (in g) stats — the GPS-less classification signal.
    accl_mag_mean: float = 1.0
    accl_mag_std: float = 0.0
    accl_mag_min: float = 1.0
    #: Whether this clip carried any GPS altitude (False → accelerometer fallback).
    has_altitude: bool = True


@dataclass
class FileSignals:
    """One raw clip plus everything we need to classify and order it.

    Ordering is by recording time (``recorded_at``, from the MP4's creation time) then
    a natural filename sort — robust to ANY naming scheme, not just GoPro's GH/GX. The
    ``chapter`` is a GoPro convenience kept for reference; it is not relied on for order.
    """

    filename: str
    path: str
    chapter: int
    duration: float
    gpmf: GpmfSignals
    recorded_at: float = 0.0  # MP4 creation time (epoch seconds); 0 when unknown
    #: File the *analysis* stages read for this clip — a validated LRV proxy when
    #: ``USE_PROXY_ANALYSIS`` is on and one is available, else the MP4 (== ``path``).
    #: Resolved in :func:`build_file_signals`. Render/photos always use ``path``.
    analysis_path: str = ""

    @property
    def analysis_src(self) -> str:
        """The file analysis reads (the resolved proxy, else the MP4 ``path``)."""
        return self.analysis_path or self.path

    @property
    def has_proxy(self) -> bool:
        """Whether a distinct (LRV) analysis source was resolved for this clip."""
        return bool(self.analysis_path) and self.analysis_path != self.path


def _natural_key(name: str) -> list[tuple[int, object]]:
    """A natural sort key: ``clip2`` sorts before ``clip10`` for any naming scheme."""
    parts = [p for p in re.split(r"(\d+)", name) if p]
    return [(1, int(p)) if p.isdigit() else (0, p.lower()) for p in parts]


def _order_key(sig: FileSignals) -> tuple[float, list[tuple[int, object]]]:
    """Recording order for a clip: creation time, then a natural filename sort."""
    return (sig.recorded_at, _natural_key(sig.filename))


def creation_time(mp4_path: str | Path) -> float:
    """The MP4's recording time as epoch seconds (seam; mocked in tests). 0 if unknown.

    Read from the container's ``creation_time`` tag so clips order by when they were
    actually shot — independent of how the camera names its files.
    """
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format_tags=creation_time",
            "-of", "default=nokey=1:noprint_wrappers=1",
            str(mp4_path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if not out:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(out.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def chapter_from_filename(filename: str) -> int:
    """GoPro chapter number from a clip name (``GH010001.MP4`` -> ``1``).

    GoPro names a multi-chapter recording ``<2 letters><2-digit chapter><4-digit
    file id>``; the chapter is the third/fourth characters. Falls back to the first
    digit run, then to chapter 1, so an unusual name still sorts deterministically.
    """
    stem = Path(filename).stem
    if len(stem) >= 4 and stem[:2].isalpha() and stem[2:4].isdigit():
        return int(stem[2:4])
    digits = "".join(c for c in stem if c.isdigit())
    return int(digits[:2]) if digits else 1


#: 1 g in m/s², for converting accelerometer magnitude to g units.
_G = 9.80665

# Exit detection: a GoPro freefall clip often starts inside the plane / at the door,
# so the real exit is where proper acceleration collapses toward 0 g (weightless).
_EXIT_DIP_G = 0.78    # sustained sub-g reading that marks the exit (stable belly exit)
_EXIT_PLANE_G = 0.85  # steady ~1 g that must precede the dip (we were in the plane)
#: A near-0 g sample *inside* a payload — the weightless exit instant. A tumbling tandem
#: keeps a ~1 g payload MEAN at the exit (drogue + rotation), so its jump shows only in
#: the per-payload MINIMUM, not the mean; a stable exit dips the mean too. We accept
#: either so both exit styles are detected.
_EXIT_WEIGHTLESS_G = 0.25


def detect_exit_offset(mp4_path: str | Path) -> float:
    """Seconds into a freefall clip where the aircraft exit happens (seam; mocked).

    Finds the first moment of weightlessness after steady ~1 g flight — the jumper
    leaving the aircraft. Two signatures, either of which counts: the payload MEAN
    collapsing below ~1 g (a stable belly exit) *or* a near-0 g MIN sample within the
    payload (a tumbling tandem, whose mean stays ~1 g). Returns ``0.0`` when neither is
    seen (the clip already starts in freefall, or carries no usable accelerometer).
    """
    try:
        from metadata.gpmf import parse_gpmf

        data = parse_gpmf(str(mp4_path))
    except Exception:  # noqa: BLE001 - no telemetry / unreadable -> no detected exit
        return 0.0

    accl = data.get("ACCL")
    if accl is None:
        return 0.0
    means: list[float] = []
    mins: list[float] = []
    times: list[float] = []
    for samples, t in zip(accl.payloads, accl.times, strict=False):
        if not samples:
            continue
        mags = [math.sqrt(sum(c * c for c in s[:3])) / _G for s in samples]
        means.append(statistics.fmean(mags))
        mins.append(min(mags))
        times.append(t)

    if len(means) < 5:
        return 0.0
    for i in range(2, len(means) - 1):
        recent = means[max(0, i - 3):i]
        was_plane = bool(recent) and statistics.fmean(recent) > _EXIT_PLANE_G
        if not was_plane:
            continue
        mean_dip = means[i] < _EXIT_DIP_G and means[i + 1] < _EXIT_DIP_G + 0.1
        weightless = mins[i] < _EXIT_WEIGHTLESS_G
        if mean_dip or weightless:
            return round(times[i], 2)
    return 0.0


# Deployment detection: the parachute opening is a large, *sustained* high-g shock
# (several g held for a couple seconds) — distinct from the brief spikes of freefall.
_DEPLOY_G = 1.6


def _accel_means(mp4_paths: Sequence[str | Path]) -> list[tuple[float, float]]:
    """Per-second accelerometer magnitude (g) across clips, concatenated in time."""
    out: list[tuple[float, float]] = []
    offset = 0.0
    for path in mp4_paths:
        try:
            from metadata.gpmf import parse_gpmf

            data = parse_gpmf(str(path))
        except Exception:  # noqa: BLE001 - skip unreadable / telemetry-less clips
            continue
        accl = data.get("ACCL")
        if accl is None:
            continue
        last = 0.0
        for samples, t in zip(accl.payloads, accl.times, strict=False):
            if not samples:
                continue
            mags = [math.sqrt(sum(c * c for c in s[:3])) / _G for s in samples]
            out.append((offset + t, statistics.fmean(mags)))
            last = t
        offset += last + 1.0
    return out


def detect_deploy_offset(mp4_paths: Sequence[str | Path]) -> float:
    """Seconds into the freefall scene where the canopy opens (seam; mocked in tests).

    When a GoPro clip runs from exit straight through the canopy ride, the deployment
    lives *inside* the freefall scene; this finds the opening shock so the canopy-opening
    beat can still be featured. ``0.0`` if no opening shock is found.

    The parachute opening is the hardest deceleration of the whole jump — terminal
    velocity bled off in a couple of seconds throws the strongest sustained g of the
    skydive. Freefall buffeting and *later* canopy maneuvers (spirals, the landing flare)
    also spike, but never as hard as the snap, so we take the start of the **strongest**
    run of ≥2 consecutive over-threshold payloads — not the first (which can truncate the
    freefall to a mid-air buffet) nor the last (which can land on a late canopy maneuver).
    """
    profile = _accel_means(list(mp4_paths))
    if len(profile) < 3:
        return 0.0
    best_start = 0.0
    best_peak = 0.0
    i, n = 0, len(profile)
    while i < n - 1:
        t, m = profile[i]
        if t >= 5.0 and m > _DEPLOY_G and profile[i + 1][1] > _DEPLOY_G:
            start, peak = t, m
            while i < n and profile[i][1] > _DEPLOY_G:  # consume the whole run
                peak = max(peak, profile[i][1])
                i += 1
            if peak > best_peak:  # the opening is the strongest shock, not the last
                best_peak, best_start = peak, round(start, 2)
        else:
            i += 1
    return best_start


def extract_gpmf_signals(mp4_path: str | Path) -> GpmfSignals:
    """Decode one clip's GPS altitude + accelerometer signals (the ``gpmf-extract``
    seam; monkeypatched in tests).

    Altitude comes from the GPS stream's third component (metres). The accelerometer
    yields both the vertical (Z) component and the *magnitude* (in g) stats — the
    latter is what classifies a clip when GPS is absent. Empty streams degrade to
    sensible neutrals (``has_altitude=False``, ~1 g calm) so a telemetry-thin clip
    still classifies rather than crashing.
    """
    from metadata.gpmf import parse_gpmf

    data = parse_gpmf(str(mp4_path))

    gps = data.get("GPS9") or data.get("GPS5")
    altitudes = [
        sample[2]
        for payload in (gps.payloads if gps else [])
        for sample in payload
        if len(sample) >= 3
    ]
    accl = data.get("ACCL")
    accl_z: list[float] = []
    mags: list[float] = []
    for payload in (accl.payloads if accl else []):
        for sample in payload:
            if len(sample) >= 3:
                accl_z.append(sample[2])
                mags.append(math.sqrt(sum(c * c for c in sample[:3])) / _G)

    alt_mean = statistics.fmean(altitudes) if altitudes else 0.0
    alt_first = altitudes[0] if altitudes else 0.0
    alt_last = altitudes[-1] if altitudes else 0.0
    z_mean = statistics.fmean(accl_z) if accl_z else 0.0
    z_std = statistics.pstdev(accl_z) if len(accl_z) > 1 else 0.0
    return GpmfSignals(
        altitude_mean=alt_mean,
        altitude_first=alt_first,
        altitude_last=alt_last,
        altitude_delta=alt_last - alt_first,
        accl_z_mean=z_mean,
        accl_z_std=z_std,
        accl_mag_mean=statistics.fmean(mags) if mags else 1.0,
        accl_mag_std=statistics.pstdev(mags) if len(mags) > 1 else 0.0,
        accl_mag_min=min(mags) if mags else 1.0,
        has_altitude=bool(altitudes),
    )


def probe_duration(mp4_path: str | Path) -> float:
    """Container duration of a clip in seconds via ffprobe (seam; mocked in tests)."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(mp4_path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def build_file_signals(mp4_path: str | Path) -> FileSignals:
    """Assemble the classification inputs for one raw clip.

    GPMF is read from the analysis source — a validated LRV proxy when enabled and
    available, else the MP4 (:func:`analysis.proxy.analysis_source`). Ordering/duration
    metadata stay on the master so the scene set the renderer cuts is unaffected.
    """
    path = Path(mp4_path)
    src = analysis_source(path)  # LRV when validated+enabled, else the MP4 itself
    gpmf = extract_gpmf_signals(src)
    return FileSignals(
        filename=path.name,
        path=str(path),
        chapter=chapter_from_filename(path.name),
        duration=probe_duration(path),
        gpmf=gpmf,
        recorded_at=creation_time(path),
        analysis_path=str(src),
    )


# Accelerometer-magnitude thresholds (in g) for GPS-less classification, tuned to
# real GoPro tandem telemetry: freefall buffets violently around a >1 g drag mean and
# dips to ~0 g at the exit; canopy oscillates moderately; ground/plane sit steady ~1 g.
_FREEFALL_MAG_STD = 0.5     # heavy variance = the 120 mph freefall buffeting
_FREEFALL_MAG_MIN = 0.3     # a near-0 g sample = the exit / unloaded freefall moment
_CANOPY_MAG_MEAN = 1.15     # sustained >1 g under an open canopy
_CANOPY_MAG_STD = 0.25      # gentle canopy swinging (below freefall's chaos)


def classify_scene(sig: FileSignals, index: int, total: int, *, ground: float = 0.0) -> str:
    """Classify one clip into a scene from its telemetry (first match wins).

    ``index``/``total`` position the clip within the chapter-sorted list so the two
    ground-level interview scenes (which look alike on telemetry) can be told apart
    by whether the clip falls in the first or last 20% of the jump. ``ground`` is the
    dropzone's ground altitude (the jump's lowest GPS reading); altitudes are taken
    RELATIVE to it, so a dropzone at 60 m elevation classifies the same as one at sea
    level. When the clip carries no GPS altitude, classification falls back to the
    accelerometer (:func:`_classify_no_gps`).
    """
    g = sig.gpmf
    if not g.has_altitude:
        return _classify_no_gps(g, index, total)

    in_first_20 = index < total * 0.2
    in_last_20 = index >= total * 0.8
    height = g.altitude_mean - ground  # metres above the dropzone ground

    if g.accl_z_mean < 0.3 and g.altitude_delta < -100:
        return "freefall"
    if height > 2000 and g.accl_z_std < 0.2:
        return "plane"
    if g.altitude_delta > 100:
        return "takeoff"
    if height > 100 and g.altitude_delta < -50:
        return "canopy"
    if height < 50 and in_first_20:
        return "intro_interview"
    if height < 50 and in_last_20:
        return "outro_interview"
    if height < 50:
        # On the ground but mid-jump (prep / standing around): default to boarding.
        return "boarding"
    return "unknown"


def _classify_no_gps(g: GpmfSignals, index: int, total: int) -> str:
    """Classify a clip from accelerometer magnitude alone (no GPS altitude).

    Freefall is the one unmistakable signature without altitude — the exit dip toward
    0 g plus violent buffeting. Canopy shows sustained-but-calmer motion. The remaining
    steady-~1 g clips are ground/plane, split into intro/outro by position with the
    rest defaulting to boarding (plane vs boarding is indistinguishable without GPS).
    """
    in_first_20 = index < total * 0.2
    in_last_20 = index >= total * 0.8

    if g.accl_mag_std > _FREEFALL_MAG_STD or g.accl_mag_min < _FREEFALL_MAG_MIN:
        return "freefall"
    if g.accl_mag_mean > _CANOPY_MAG_MEAN or g.accl_mag_std > _CANOPY_MAG_STD:
        return "canopy"
    if in_first_20:
        return "intro_interview"
    if in_last_20:
        return "outro_interview"
    return "boarding"


def _freefall_anchor_indices(signals: Sequence[FileSignals]) -> list[int]:
    """Indices (in recording order) of clips with an unmistakable freefall accelerometer
    signature — the anchor for GPS-less chronological classification.

    Uses the same magnitude test as the GPS-less freefall rule (heavy 120 mph buffeting
    variance, or a near-0 g exit sample) and ignores GPS, so it anchors mixed footage too.
    """
    return [
        i
        for i, s in enumerate(signals)
        if s.gpmf.accl_mag_std > _FREEFALL_MAG_STD or s.gpmf.accl_mag_min < _FREEFALL_MAG_MIN
    ]


def _classify_no_gps_anchored(
    sig: FileSignals, index: int, total: int, ff_indices: list[int]
) -> str:
    """GPS-less scene label using the freefall clip as a chronological anchor.

    Without GPS altitude, pre-jump ground ("boarding") and post-jump ground ("canopy"/
    landing) have overlapping accelerometer signatures, so labelling each clip in
    isolation (:func:`_classify_no_gps`) scrambles the jump timeline once scenes are
    emitted in fixed order. Here we anchor on the freefall clip — whose accelerometer
    signature is unmistakable — and place every other clip by whether it was *recorded*
    before or after it: before → intro/boarding (never canopy), the freefall span →
    freefall, after → canopy/outro (never boarding). Recording order is reliable even
    with no GPS, so this keeps the render chronological. Falls back to the per-clip
    heuristic when no freefall anchor is found.
    """
    if not ff_indices:
        return _classify_no_gps(sig.gpmf, index, total)
    first_ff, last_ff = ff_indices[0], ff_indices[-1]
    if first_ff <= index <= last_ff:
        return "freefall"
    if index < first_ff:
        # Pre-jump: the opening ground interview, then boarding/plane up to the exit.
        return "intro_interview" if index < total * 0.2 else "boarding"
    # Post-jump (index > last_ff): the canopy ride, ending on the ground interview.
    return "outro_interview" if index >= total * 0.8 else "canopy"


def _assign_with_anchor(
    signals: Sequence[FileSignals], overrides: dict[str, str], ground: float
) -> tuple[list[tuple[str, FileSignals]], list[str]]:
    """Label every clip, anchoring GPS-less clips to the freefall clip for chronology.

    Returns ``(classified, unknown_filenames)``. An overridden clip takes its label
    verbatim; a clip with GPS altitude uses :func:`classify_scene` unchanged; a GPS-less
    clip uses :func:`_classify_no_gps_anchored`. Only an un-overridden GPS clip can be
    ``"unknown"`` (the GPS-less path always resolves), so the caller's low-confidence
    gate is unchanged for GPS footage.
    """
    total = len(signals)
    ff_indices = _freefall_anchor_indices(signals)
    classified: list[tuple[str, FileSignals]] = []
    unknown: list[str] = []
    for i, sig in enumerate(signals):
        if sig.filename in overrides:
            label = overrides[sig.filename]
        elif sig.gpmf.has_altitude:
            label = classify_scene(sig, i, total, ground=ground)
            if label == "unknown":
                unknown.append(sig.filename)
        else:
            label = _classify_no_gps_anchored(sig, i, total, ff_indices)
        classified.append((label, sig))
    return classified, unknown


def load_scene_labels(path: str | Path) -> dict[str, str]:
    """Load optional manual scene overrides — ``{filename: scene}`` — or ``{}``.

    Returns an empty mapping when the file is absent. Raises :class:`SelfieError` on
    malformed JSON or an unknown scene name, so a typo fails loudly rather than
    silently mislabelling a clip.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise SelfieError(f"invalid {SCENE_LABELS_FILENAME}: {e}") from e
    if not isinstance(data, dict):
        raise SelfieError(
            f"{SCENE_LABELS_FILENAME} must be an object mapping filename -> scene"
        )
    overrides: dict[str, str] = {}
    for filename, scene in data.items():
        if scene not in VALID_SCENES:
            raise SelfieError(
                f"{SCENE_LABELS_FILENAME}: {scene!r} for {filename!r} is not a valid "
                f"scene (expected one of {sorted(VALID_SCENES)})"
            )
        overrides[filename] = scene
    return overrides


def classify_files(
    raw_dir: str | Path, *, labels_path: str | Path | None = None
) -> list[tuple[str, FileSignals]]:
    """Classify every raw MP4 under ``raw_dir`` into a scene, chapter-sorted.

    A clip listed in the job's ``scene_labels.json`` (or ``labels_path``) takes that
    scene verbatim — telemetry is ignored for it, and it never counts as ``unknown``.
    Everything else is classified from GPMF. Raises :class:`LowConfidenceError` when
    more than two *un-overridden* clips land in ``unknown`` — the cue for a human to
    look before we burn render time on a bad guess.
    """
    raw = Path(raw_dir)
    mp4s = sorted(p for p in raw.iterdir() if p.suffix.lower() == ".mp4")
    if not mp4s:
        raise SelfieError(f"no MP4s to process in {raw}")

    # Default override location is the job dir (raw_dir's parent), next to booking.json.
    overrides = load_scene_labels(labels_path or raw.parent / SCENE_LABELS_FILENAME)

    signals = sorted((build_file_signals(p) for p in mp4s), key=_order_key)

    # The dropzone's ground altitude = the jump's lowest GPS reading. Altitudes are
    # judged relative to it, so a dropzone at 60 m elevation isn't mistaken for "in air".
    gps_alts = [s.gpmf.altitude_mean for s in signals if s.gpmf.has_altitude]
    ground = min(gps_alts) if gps_alts else 0.0

    # GPS-less clips are placed relative to the freefall clip (recording order is the
    # reliable signal when there's no altitude) so the jump stays chronological.
    classified, unknown = _assign_with_anchor(signals, overrides, ground)

    if len(unknown) > 2:
        raise LowConfidenceError(
            f"{len(unknown)} clips could not be classified ({', '.join(unknown)}); "
            f"add them to {SCENE_LABELS_FILENAME} or review manually"
        )
    return classified


def classify_camera_files(
    raw_dirs: Sequence[str | Path], *, labels_path: str | Path | None = None
) -> list[tuple[str, FileSignals]]:
    """Classify the MP4s spread across several raw dirs into scenes, time-sorted.

    The Ultimate package stores each camera's clips in its own ``raw/<role>/`` subdir;
    this gathers the clips from one *or both* of those dirs and classifies them with
    the same telemetry logic :func:`classify_files` uses (shared :func:`build_file_signals`,
    :func:`classify_scene`, and ground-altitude handling). Passing both dirs builds the
    combined two-camera view (full video + highlights); passing one builds that
    camera's own view (the per-camera freefall cuts). Optional manual overrides come
    from ``labels_path`` via :func:`load_scene_labels`.

    Raises :class:`SelfieError` when no MP4s are found, and :class:`LowConfidenceError`
    when more than two un-overridden clips can't be classified — same contract as
    :func:`classify_files`.
    """
    mp4s: list[Path] = []
    for raw_dir in raw_dirs:
        d = Path(raw_dir)
        if d.exists():
            mp4s.extend(p for p in d.iterdir() if p.suffix.lower() == ".mp4")
    if not mp4s:
        joined = ", ".join(str(d) for d in raw_dirs)
        raise SelfieError(f"no MP4s to process in {joined}")

    overrides = load_scene_labels(labels_path) if labels_path is not None else {}

    signals = sorted((build_file_signals(p) for p in mp4s), key=_order_key)

    gps_alts = [s.gpmf.altitude_mean for s in signals if s.gpmf.has_altitude]
    ground = min(gps_alts) if gps_alts else 0.0

    # GPS-less clips anchored to the freefall clip so the timeline stays chronological.
    classified, unknown = _assign_with_anchor(signals, overrides, ground)

    if len(unknown) > 2:
        raise LowConfidenceError(
            f"{len(unknown)} clips could not be classified ({', '.join(unknown)}); "
            f"add them to {SCENE_LABELS_FILENAME} or review manually"
        )
    return classified


# --------------------------------------------------------------------------- #
# Step 1 — scene assembly
# --------------------------------------------------------------------------- #


def _require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SelfieError(f"{tool} not found on PATH (required by the selfie pipeline)")


# Hard cap on any single FFmpeg pass so a wedged encode can't hang the whole job.
# Generous because a full-jump 1080p encode on a laptop CPU can legitimately run for
# several minutes; past this we assume it's stuck rather than slow.
_FFMPEG_TIMEOUT_S = 1800


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run an FFmpeg command, surfacing its stderr (not a bare exit code) on failure.

    FFmpeg is invoked with ``-v error`` so its stderr is just the real problem; we
    capture it and raise :class:`SelfieError` with that text, so a render failure is
    diagnosable instead of an opaque ``CalledProcessError(183, ...)``.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as e:
        raise SelfieError(f"ffmpeg timed out after {_FFMPEG_TIMEOUT_S}s: {' '.join(cmd)}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-800:] or "(no stderr)"
        raise SelfieError(f"ffmpeg failed (exit {proc.returncode}): {tail}")


def concat_scene(source_paths: Sequence[str], out_path: str | Path) -> Path:
    """Concatenate clips into one scene MP4 via the FFmpeg concat demuxer (seam).

    Uses ``-c copy`` (stream copy, no re-encode) against a generated ``filelist.txt``,
    matching the spec command. Works for a single-clip scene too.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    listfile = out.parent / f"{out.stem}_filelist.txt"
    # The concat demuxer resolves relative entries against the *listfile's* directory,
    # not the cwd, so write absolute paths (and escape any single quotes) to avoid a
    # doubled-path "no such file" error.
    listfile.write_text(
        "".join(
            "file '{}'\n".format(str(Path(p).resolve()).replace("'", r"'\''"))
            for p in source_paths
        )
    )
    _run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
            "-c", "copy",
            str(out),
        ]
    )
    return out


def build_scenes(
    job_id: str,
    classified: Sequence[tuple[str, FileSignals]],
    jobs_root: str | Path | None = None,
    *,
    scenes_subdir: str = "scenes",
    manifest_name: str = "scene_manifest.json",
) -> dict[str, Any]:
    """Concat same-scene clips and write ``scene_manifest.json``; return the manifest.

    ``scenes_subdir`` / ``manifest_name`` default to the single scene set every
    package builds; the Ultimate package overrides them to build a second, per-camera
    scene set (``scenes_instructor/`` etc.) without clobbering the combined one.
    """
    scenes_dir = job_dir(job_id, jobs_root) / scenes_subdir
    scenes_dir.mkdir(parents=True, exist_ok=True)

    by_scene: dict[str, list[FileSignals]] = {}
    for label, sig in classified:
        by_scene.setdefault(label, []).append(sig)

    # Known scenes in jump order; any leftover (e.g. unknown) appended after.
    ordered = [s for s in SCENE_ORDER if s in by_scene]
    ordered += [s for s in by_scene if s not in SCENE_ORDER]

    scenes: list[dict[str, Any]] = []
    flagged: list[str] = []
    for label in ordered:
        clips = sorted(by_scene[label], key=_order_key)
        combined = scenes_dir / f"{label}.mp4"
        concat_scene([c.path for c in clips], combined)

        # When every clip in the scene resolved to a validated LRV proxy, assemble a
        # parallel low-res scene for the (decode-heavy) face scoring. All-or-nothing:
        # a mixed proxy/MP4 concat would desync, so a single non-proxy clip leaves this
        # None and scoring falls back to the MP4 ``combined_path``. Best-effort — any
        # failure here just means scoring uses the master scene.
        proxy_combined: Path | None = None
        if all(c.has_proxy for c in clips):
            try:
                proxy_combined = concat_scene(
                    [c.analysis_path for c in clips], scenes_dir / f"{label}.proxy.mp4"
                )
            except Exception as e:  # noqa: BLE001 - proxy scene is best-effort
                logger.warning(
                    "proxy scene build failed for %s (%r); scoring will use the MP4 scene",
                    label, e,
                )
                proxy_combined = None

        first, last = clips[0].gpmf, clips[-1].gpmf
        needs_review = label == "unknown"
        scene: dict[str, Any] = {
            "name": label,
            "source_files": [c.filename for c in clips],
            "combined_path": str(combined),
            "duration": round(sum(c.duration for c in clips), 3),
            "needs_review": needs_review,
            "gpmf_signals": {
                "altitude_mean": round(
                    statistics.fmean([c.gpmf.altitude_mean for c in clips]), 3
                ),
                "altitude_delta": round(last.altitude_last - first.altitude_first, 3),
                "accl_z_mean": round(
                    statistics.fmean([c.gpmf.accl_z_mean for c in clips]), 3
                ),
            },
        }
        # Low-res scene the scorer reads instead of the master, when one was built.
        # Render and photo extraction always use ``combined_path`` (the MP4).
        if proxy_combined is not None:
            scene["proxy_path"] = str(proxy_combined)
        if label == "freefall":
            # Where the real exit happens within the clip (it often starts in the plane)
            # and, when the canopy ride is part of the same clip, where it opens. Read
            # from the analysis source (the LRV's telemetry is identical to the master).
            scene["exit_offset"] = detect_exit_offset(clips[0].analysis_src)
            scene["deploy_offset"] = detect_deploy_offset([c.analysis_src for c in clips])
        scenes.append(scene)
        if needs_review:
            flagged.append(label)

    manifest = {"scenes": scenes, "flagged": flagged}
    (job_dir(job_id, jobs_root) / manifest_name).write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return manifest


# --------------------------------------------------------------------------- #
# Step 2 — MediaPipe scoring
# --------------------------------------------------------------------------- #


def score_scene(scene_path: str | Path, *, fps: float = SCORE_FPS) -> list[dict[str, float]]:
    """Per-second face scores for one scene MP4 (reuses /analysis; seam for tests).

    Pulls frames at ``fps`` and runs the FaceLandmarker scorer, returning rows of
    ``{ts, smile, eye_contact, face_in_frame, face_centered}`` — one per second.
    """
    from analysis.extract import extract_freefall_frames
    from analysis.score import FreefallScorer

    duration = probe_duration(scene_path)
    if duration <= 0:
        return []
    frames = extract_freefall_frames(
        scene_path, 0.0, duration, fps=fps, allow_full_res=True
    )
    with FreefallScorer() as scorer:
        return scorer.score_frames(frames)


def score_scenes(
    manifest: dict[str, Any],
    job_id: str,
    jobs_root: str | Path | None = None,
    *,
    scores_name: str = "scores.json",
) -> dict[str, list[dict[str, float]]]:
    """Score every scene in the manifest and write ``scores.json``.

    ``scores_name`` defaults to the combined set's scores; the Ultimate package writes
    a per-camera ``scores_<role>.json`` so the second scene set's scores don't clobber
    the first.
    """
    scores: dict[str, list[dict[str, float]]] = {}
    for scene in manifest["scenes"]:
        # Score the validated LRV proxy scene when one was built, else the MP4 scene.
        # Times are identical on both, so the scores apply to the MP4 timeline as-is.
        scores[scene["name"]] = score_scene(scene.get("proxy_path") or scene["combined_path"])
    jd = job_dir(job_id, jobs_root)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / scores_name).write_text(json.dumps(scores, indent=2) + "\n")
    return scores


# --------------------------------------------------------------------------- #
# Step 3 — compose three EDLs (one Claude call)
# --------------------------------------------------------------------------- #

_COMPOSE_SYSTEM = (
    "You are a skydive video editor. Return valid JSON only. "
    "No markdown, no explanation."
)

_COMPOSE_RULES = """\
Produce three EDLs as ONE JSON object with keys "full_video", "highlights", and
"freefall". Each is a list of clips: {"scene","src_start","src_end","speed_multiplier"}.
Times are seconds on that scene's own MP4.

CRITICAL — the "freefall" scene's clip STARTS INSIDE THE AIRCRAFT; ts 0 is NOT the
jump. Its exit_offset and deploy_offset fields give the real aerial window: the aircraft
exit (going weightless) is at exit_offset, and the canopy opens at deploy_offset. Actual
aerial freefall is ONLY between exit_offset and deploy_offset. Footage before exit_offset
is inside the aircraft / the door; footage after deploy_offset is the canopy ride. NEVER
treat ts 0 of the freefall scene as the exit, and never mine "freefall" beats from before
exit_offset or after deploy_offset.

Apply slow motion (speed_multiplier 0.4) only to SHORT high-value beats (~1 s each),
never to a whole scene: the aircraft exit (at the freefall scene's exit_offset), the best
freefall smiles, and the canopy deployment (at the freefall scene's deploy_offset).

The aircraft ENTRY (boarding — walking up to and climbing into the plane, e.g. up the
staircase) is the HEAD of the "boarding"/"plane" scene (src_start near 0). Always include
the first few seconds of that scene as the entry milestone in full_video and highlights,
EVEN IF its face scores are low — the jumper is usually turned away while boarding, so it
will not show up in scored_seconds, but it is a mandatory story beat.

The aircraft exit / jump is one of the most important moments and MUST always appear in
BOTH highlights and freefall — it begins at the freefall scene's exit_offset (NOT ts 0).
The door / exit-prep (approaching the door, looking out) is the few seconds just BEFORE
exit_offset (the tail of the aircraft scene when there is a separate "boarding"/"plane").

Milestone-first: include every available mandatory moment BEFORE adding score-based
filler — aircraft entry, inside the aircraft, door/exit-prep, exit/jump, freefall,
deployment, landing, outro.

full_video:
  All present scenes in order. Trim to stay under 4 minutes. Slow-mo the exit (at
  exit_offset), the top freefall smiles, and the deployment (at deploy_offset).

The EXIT SEQUENCE is mandatory and must be a CONTINUOUS block, not a short snippet: the
door/exit-prep (the seconds just before exit_offset) followed by the first several
seconds of freefall STARTING AT exit_offset (exit → jump → initial freefall →
stabilisation). Include it in full BEFORE adding any AI-scored moments.

highlights:
  Under 1 minute. Include each milestone first (entry, inside, door, the continuous exit
  sequence, deployment, landing, outro), then fill remaining time with the best freefall
  beats (all BETWEEN exit_offset and deploy_offset). Slow-mo the exit and the top peaks.

freefall:
  Under 1:15, and ENTIRELY within the aerial window. Lead with the door/exit-prep (the
  seconds just before exit_offset), then the continuous exit sequence (freefall starting
  AT exit_offset, ~12 s, slow-mo the exit), then short windows around the best freefall
  moments BETWEEN exit_offset and deploy_offset, then the deployment (at deploy_offset,
  slow-mo). Do NOT use any in-aircraft footage before exit_offset, any canopy footage
  after deploy_offset, or any general boarding/canopy-flight/landing/outro footage.\
"""


def _composite(row: dict[str, float]) -> float:
    return (
        row["smile"] + row["eye_contact"] + row["face_in_frame"] + row["face_centered"]
    )


def _scene_highlights(rows: Sequence[dict[str, float]]) -> dict[str, list[dict[str, float]]]:
    """The 10 best and 3 worst scored seconds of a scene (by composite score)."""
    ranked = sorted(rows, key=_composite, reverse=True)
    return {"top": ranked[:10], "bottom": ranked[-3:] if len(ranked) >= 3 else ranked}


def _build_compose_prompt(
    scores: dict[str, list[dict[str, float]]],
    manifest: dict[str, Any],
    booking: dict[str, Any],
    *,
    target_duration: float,
) -> str:
    """Assemble the single user message: booking, scene shape, scored seconds, rules."""
    scenes = []
    for s in manifest["scenes"]:
        rows = scores.get(s["name"], [])
        entry: dict[str, Any] = {
            "name": s["name"],
            "duration": s["duration"],
            "weight": SCENE_WEIGHTS.get(s["name"], 0.3),
        }
        if s["name"] == "freefall":
            # The freefall clip starts inside the aircraft and runs through the canopy
            # ride; the real aerial window is exit_offset → deploy_offset. Hand the model
            # those marks AND restrict the scored seconds to the window, so its "best
            # freefall beats" can't be in-aircraft (calm pre-jump smile) or under canopy.
            exit_off = round(float(s.get("exit_offset") or 0.0), 2)
            deploy_off = round(float(s.get("deploy_offset") or s["duration"]), 2)
            entry["exit_offset"] = exit_off
            entry["deploy_offset"] = deploy_off
            rows = [r for r in rows if exit_off <= float(r.get("ts", 0.0)) <= deploy_off]
        entry["scored_seconds"] = _scene_highlights(rows)
        scenes.append(entry)
    payload = {
        "customer_name": booking.get("customer_name"),
        "jump_date": booking.get("jump_date"),
        "target_duration_seconds": target_duration,
        "scenes": scenes,
    }
    rules = _COMPOSE_RULES.replace("{target}", f"{target_duration:g}")
    return (
        f"{rules}\n\nSignals for this jump as JSON:\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\nReturn the JSON now."
    )


def _response_text(response: Any) -> str:
    return next((b.text for b in response.content if b.type == "text"), "")


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise SelfieError(f"no JSON object in model response: {text[:200]!r}")
    return text[start : end + 1]


#: A clip at/before this many seconds into the boarding/plane scene IS the entry beat.
_ENTRY_HEAD_S = 2.0


def _ensure_aircraft_entry(edls: EDLResponse, manifest: dict[str, Any]) -> EDLResponse:
    """Guarantee the aircraft-entry beat (head of the boarding/plane scene) is in the
    full video and highlights — a deterministic backstop for the compose rules.

    Boarding the plane (walking up the staircase, climbing in) is the start of the
    boarding scene, but it scores low (the jumper is turned away) so the model can drop
    it. Here we inject the first few seconds of the boarding (or plane) scene when it's
    absent — in jump order for the full video, right after the intro for highlights. A
    no-op when the beat is already present (e.g. the offline house cut), so it's safe to
    run on every compose.
    """
    scenes = manifest.get("scenes", [])
    entry_scene = _scene_by_name(scenes, "boarding") or _scene_by_name(scenes, "plane")
    if entry_scene is None:
        return edls
    name = entry_scene["name"]
    end = round(min(_MILESTONE_S, max(float(entry_scene.get("duration") or _MILESTONE_S), 0.2)), 2)
    entry = Clip(scene=name, src_start=0.0, src_end=end)

    def _order(scene: str) -> int:
        return SCENE_ORDER.index(scene) if scene in SCENE_ORDER else len(SCENE_ORDER)

    def _present(clips: Sequence[Clip]) -> bool:
        return any(c.scene == name and c.src_start <= _ENTRY_HEAD_S for c in clips)

    def _in_jump_order(clips: Sequence[Clip]) -> list[Clip]:
        # Insert before the first clip from a later scene, keeping the full video ordered.
        for i, c in enumerate(clips):
            if _order(c.scene) > _order(name):
                return [*clips[:i], entry, *clips[i:]]
        return [*clips, entry]

    def _after_intro(clips: Sequence[Clip]) -> list[Clip]:
        i = 0
        while i < len(clips) and clips[i].scene == "intro_interview":
            i += 1
        return [*clips[:i], entry, *clips[i:]]

    full = list(edls.full_video) if _present(edls.full_video) else _in_jump_order(edls.full_video)
    highs = list(edls.highlights) if _present(edls.highlights) else _after_intro(edls.highlights)
    return edls.model_copy(update={"full_video": full, "highlights": highs})


# --------------------------------------------------------------------------- #
# Story enforcement — deterministic ordering + milestone guarantees applied to
# WHICHEVER compose path ran (Claude or the offline house cut).
#
# Claude returns a JSON EDL and we used to ship it almost verbatim, so when the model
# scrambled the clip order or dropped a mandatory beat (the exit/jump, the canopy
# opening, the landing) that is exactly what rendered. These backstops make the
# customer's journey — intro → boarding → flight → exit/jump → freefall → canopy
# opening → landing → outro — structurally guaranteed regardless of model variance,
# reusing the same helpers the house cut already builds milestones from. Each check is
# a no-op when the beat is already present, so they're safe on every compose.
# --------------------------------------------------------------------------- #


def _scene_rank(scene: str) -> int:
    """Jump-order rank of a scene (scenes outside :data:`SCENE_ORDER` sort last)."""
    return SCENE_ORDER.index(scene) if scene in SCENE_ORDER else len(SCENE_ORDER)


def _chronological(clips: Sequence[Clip]) -> list[Clip]:
    """Order clips into the jump's natural sequence: scene order, then source time.

    The customer's story is always chronological, so the deliverable's clips are sorted
    into that order deterministically rather than trusting the model to emit them in
    sequence — this is what keeps the landing from opening a cut or the boarding from
    landing mid-freefall. A stable sort preserves the relative order of clips that tie
    (e.g. successive freefall beats already in time order).
    """
    return sorted(clips, key=lambda c: (_scene_rank(c.scene), c.src_start))


def _covers(clips: Sequence[Clip], scene: str, lo: float, hi: float) -> bool:
    """True when some clip from ``scene`` overlaps the window ``[lo, hi)``."""
    return any(c.scene == scene and c.src_start < hi and c.src_end > lo for c in clips)


def _union_into(target: Sequence[Clip], extra: Sequence[Clip]) -> list[Clip]:
    """Return ``target`` plus every window from ``extra`` it does not already cover.

    Makes ``target`` a superset of ``extra``: an existing target clip wins on overlap
    (its slow-mo / speed is preserved), and only a genuinely-missing window (same scene,
    no overlap with anything already there) is appended. Used to keep the deliverables
    consistent — the freefall highlight beats present in every cut, and ``full_video`` a
    superset of the focused cuts — without ever duplicating footage.
    """
    out = list(target)
    for c in extra:
        if not _covers(out, c.scene, c.src_start, c.src_end):
            out.append(c)
    return out


def _exit_offset_in(scenes: Sequence[dict[str, Any]]) -> float | None:
    """The detected exit offset within the freefall scene, or ``None`` if no freefall."""
    ff = _scene_by_name(scenes, "freefall")
    if ff is None:
        return None
    fdur = max(float(ff["duration"]), 0.1)
    return max(0.0, min(_freefall_exit(scenes), fdur - 0.1))


def _exit_block(scenes: Sequence[dict[str, Any]], length: float) -> list[Clip]:
    """The continuous exit/jump block (slow-mo anchored at the detected exit), or []."""
    ff = _scene_by_name(scenes, "freefall")
    exit_off = _exit_offset_in(scenes)
    if ff is None or exit_off is None:
        return []
    fdur = max(float(ff["duration"]), 0.1)
    exit_end = min(exit_off + length, fdur)
    return _segment_with_slowmo("freefall", exit_off, exit_end, [exit_off])


#: How close to the detected exit a clip must start to count as "the exit is present".
_EXIT_PRESENT_TOL_S = 3.0


def _has_exit(clips: Sequence[Clip], scenes: Sequence[dict[str, Any]]) -> bool:
    """Whether the deliverable already contains the aircraft exit / jump moment."""
    exit_off = _exit_offset_in(scenes)
    if exit_off is None:  # no freefall scene -> nothing to guarantee
        return True
    return _covers(clips, "freefall", max(0.0, exit_off - 0.1), exit_off + _EXIT_PRESENT_TOL_S)


def _has_canopy_opening(clips: Sequence[Clip], scenes: Sequence[dict[str, Any]]) -> bool:
    """Whether the canopy-opening (deployment) beat is already present."""
    opening = _canopy_opening(scenes)
    if not opening:  # no canopy scene and no detected deploy -> nothing to guarantee
        return True
    first = opening[0]
    return _covers(clips, first.scene, first.src_start - 0.5, first.src_end + 0.5)


def _landing_block(scenes: Sequence[dict[str, Any]]) -> list[Clip]:
    """The landing beat — the tail of the canopy scene — or [] when there's no canopy."""
    canopy = _scene_by_name(scenes, "canopy")
    if canopy is None:
        return []
    cdur = max(float(canopy["duration"]), 0.1)
    return [_window(canopy, cdur - _MILESTONE_S, _MILESTONE_S)]


def _has_landing(clips: Sequence[Clip], scenes: Sequence[dict[str, Any]]) -> bool:
    """Whether the landing (canopy-tail) beat is already present."""
    canopy = _scene_by_name(scenes, "canopy")
    if canopy is None:
        return True
    cdur = max(float(canopy["duration"]), 0.1)
    return _covers(clips, "canopy", cdur - _MILESTONE_S - 1.0, cdur)


def _ensure_milestones(
    clips: Sequence[Clip], scenes: Sequence[dict[str, Any]], *, exit_len: float
) -> list[Clip]:
    """Inject any missing mandatory beat (exit/jump, canopy opening, landing) — the
    aircraft entry is handled by :func:`_ensure_aircraft_entry`. Appends are placed by
    the caller's chronological sort, so order here doesn't matter."""
    out = list(clips)
    if not _has_exit(out, scenes):
        out += _exit_block(scenes, exit_len)
    if not _has_canopy_opening(out, scenes):
        out += _canopy_opening(scenes)
    if not _has_landing(out, scenes):
        out += _landing_block(scenes)
    return out


def _ensure_freefall_exit(
    clips: Sequence[Clip], scenes: Sequence[dict[str, Any]]
) -> list[Clip]:
    """Guarantee the freefall cut contains the aircraft exit / jump (it must lead it).

    The freefall video is meaningless without the jump, but the model sometimes mines
    only mid-air smiles and omits the exit. When it's absent we prepend the continuous
    exit block; the caller's chronological sort then anchors it at the head.
    """
    out = list(clips)
    if not _has_exit(out, scenes):
        out = _exit_block(scenes, _EXIT_SEQUENCE_S) + out
    return out


def _ensure_story(edls: EDLResponse, manifest: dict[str, Any]) -> EDLResponse:
    """Make the customer's journey structurally correct on top of any compose path.

    Guarantees, deterministically: the aircraft entry, the exit/jump, the canopy
    opening, and the landing are all present in the full video and highlights; the
    freefall cut contains the exit; and every deliverable's clips play in chronological
    jump order. A no-op for a beat already present (so the offline house cut, which
    already builds all of these in order, passes through unchanged).
    """
    edls = _ensure_aircraft_entry(edls, manifest)
    scenes = manifest.get("scenes", [])
    full = _ensure_milestones(list(edls.full_video), scenes, exit_len=_EXIT_SEQUENCE_S)
    highs = _ensure_milestones(list(edls.highlights), scenes, exit_len=_HL_EXIT_SEQUENCE_S)
    free = _ensure_freefall_exit(list(edls.freefall), scenes)

    # Cross-cut coverage (product rule): the freefall highlight beats must appear in
    # every cut, and full_video is the COMPLETE edit — a superset of both focused cuts,
    # so no beat that's good enough for highlights/freefall is ever missing from it.
    #  1) the freefall cut carries every freefall beat the highlights feature, and
    #  2) full_video unions in everything from highlights AND freefall.
    free = _union_into(free, [c for c in highs if c.scene == "freefall"])
    full = _union_into(_union_into(full, highs), free)

    return edls.model_copy(
        update={
            "full_video": _chronological(full),
            "highlights": _chronological(highs),
            "freefall": _chronological(free),
        }
    )


def compose_edls(
    scores: dict[str, list[dict[str, float]]],
    manifest: dict[str, Any],
    booking: dict[str, Any],
    job_id: str,
    jobs_root: str | Path | None = None,
    *,
    client: Any | None = None,
    target_duration: float = 90.0,
    use_ai: bool = True,
) -> EDLResponse:
    """Produce the three EDLs (Claude when available, else an offline house cut).

    With a client (or ``ANTHROPIC_API_KEY``) this makes ONE Claude call per jump and
    retries exactly once on an invalid reply (CLAUDE.md: never loop Claude). With no
    key and no client it falls back to a deterministic rule-based EDL built from the
    scores + manifest — so the pipeline runs end-to-end offline, mirroring the
    house-cut fallback in ``scripts/process_jump.py``. Either way it persists
    ``edl_full.json``, ``edl_highlights.json``, ``edl_freefall.json``.

    ``use_ai=False`` forces the deterministic house cut even when a key/client is
    present. The **external** (camera-flyer) package uses this: its footage is shot
    from a distance, so MediaPipe rarely scores the far-away tandem, which starves the
    editorial signals the model reasons over — the model then drops whole scenes or
    mis-sequences them. The house cut instead guarantees a complete, in-order edit
    (every scene contributes a proportional chunk; all milestones present), which is
    what that footage needs. The selfie (close-cam) product keeps the AI editor.
    """
    if not use_ai or (client is None and not os.environ.get("ANTHROPIC_API_KEY")):
        if use_ai:
            logger.warning(
                "ANTHROPIC_API_KEY not set; composing offline rule-based EDLs for job %s",
                job_id,
            )
        edls = _house_edls(
            scores, manifest, target_duration, profile=load_style_profile(jobs_root)
        )
    else:
        edls = _compose_edls_via_claude(
            scores, manifest, booking, client=client, target_duration=target_duration
        )

    # Backstop: guarantee the journey is structurally correct regardless of which path
    # ran — every milestone present (entry, exit/jump, canopy opening, landing) and every
    # deliverable in chronological jump order, so the model can't scramble the story.
    edls = _ensure_story(edls, manifest)
    _persist_edls(edls, job_id, jobs_root)
    return edls


def _persist_edls(edls: EDLResponse, job_id: str, jobs_root: str | Path | None) -> None:
    """Write the three EDLs next to the job's other artifacts."""
    jd = job_dir(job_id, jobs_root)
    jd.mkdir(parents=True, exist_ok=True)
    for name, clips in (
        ("edl_full.json", edls.full_video),
        ("edl_highlights.json", edls.highlights),
        ("edl_freefall.json", edls.freefall),
    ):
        (jd / name).write_text(
            json.dumps([c.model_dump() for c in clips], indent=2) + "\n"
        )


def _compose_edls_via_claude(
    scores: dict[str, list[dict[str, float]]],
    manifest: dict[str, Any],
    booking: dict[str, Any],
    *,
    client: Any | None,
    target_duration: float,
) -> EDLResponse:
    """The Claude path: one call + at most one retry, validated against EDLResponse."""
    if client is None:
        try:
            from anthropic import Anthropic
        except ImportError as e:  # pragma: no cover - exercised only without the SDK
            raise SelfieError("anthropic SDK not installed; pass client= or install it") from e
        client = Anthropic()

    prompt = _build_compose_prompt(
        scores, manifest, booking, target_duration=target_duration
    )
    messages: list[MessageParam] = [{"role": "user", "content": prompt}]

    last_error: Exception | None = None
    edls: EDLResponse | None = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=_COMPOSE_MAX_TOKENS,
                system=_COMPOSE_SYSTEM,
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001 - surface any SDK/transport error
            raise SelfieError(f"Claude API call failed: {e!r}") from e

        text = _response_text(response)
        try:
            edls = EDLResponse.model_validate(json.loads(_extract_json(text)))
            break
        except (ValidationError, json.JSONDecodeError, SelfieError) as e:
            last_error = e
            logger.warning("selfie EDL attempt %d invalid: %s", attempt + 1, e)
            if attempt == 0:
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That was not a valid EDL. Error:\n{e}\n\n"
                            "Return ONE corrected JSON object and nothing else."
                        ),
                    }
                )
    if edls is None:
        raise SelfieError(f"model failed to produce valid EDLs after 2 attempts: {last_error}")
    return edls


# --------------------------------------------------------------------------- #
# Offline house cut — deterministic EDLs when no Claude key is available
# --------------------------------------------------------------------------- #


def _top_peaks(
    rows: Sequence[dict[str, float]],
    n: int,
    *,
    key: str = "smile",
    min_gap: float = _PEAK_MIN_GAP_S,
    lo: float = 0.0,
    hi: float | None = None,
) -> list[float]:
    """The ``n`` highest-``key`` timestamps within ``[lo, hi)``, spaced by ``min_gap``.

    Returns them in chronological order. Spacing keeps the featured moments from
    clustering on adjacent seconds.
    """
    pool = [r for r in rows if lo <= float(r["ts"]) and (hi is None or float(r["ts"]) < hi)]
    picked: list[float] = []
    for r in sorted(pool, key=lambda r: r.get(key, 0.0), reverse=True):
        t = float(r["ts"])
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(t)
        if len(picked) >= n:
            break
    return sorted(picked)


def _freefall_bounds(rows: Sequence[dict[str, float]], duration: float) -> tuple[float, float]:
    """Trim the freefall window to where the face is in frame (face_in_frame >= 0.3)."""
    in_frame = [r for r in rows if r.get("face_in_frame", 1.0) >= 0.3]
    if not in_frame:
        return 0.0, max(duration, 0.1)
    start = max(0.0, float(min(r["ts"] for r in in_frame)))
    end = min(max(duration, start + 0.1), float(max(r["ts"] for r in in_frame)) + 1.0)
    return start, end


def _segment_with_slowmo(
    scene: str,
    start: float,
    end: float,
    slow_points: Sequence[float],
    *,
    slow_len: float = _SLOWMO_LEN,
    speed: float = SLOWMO_SPEED,
) -> list[Clip]:
    """Split ``[start, end)`` into normal clips with ``slow_len`` slow-mo at each point.

    Only the short windows at ``slow_points`` are ramped to ``speed`` — the rest plays
    at real time, so we never slow a whole scene.
    """
    pts = sorted(p for p in slow_points if start <= p < end)
    clips: list[Clip] = []
    cursor = start
    for p in pts:
        if p > cursor:
            clips.append(Clip(scene=scene, src_start=cursor, src_end=p))
        slow_end = min(p + slow_len, end)
        clips.append(
            Clip(scene=scene, src_start=p, src_end=max(slow_end, p + 0.05), speed_multiplier=speed)
        )
        cursor = slow_end
    if cursor < end:
        clips.append(Clip(scene=scene, src_start=cursor, src_end=end))
    if not clips:
        clips.append(Clip(scene=scene, src_start=start, src_end=max(end, start + 0.1)))
    return clips


# Scenes that are "inside/at the aircraft" — the exit/door-prep lives in the tail of
# whichever of these immediately precedes freefall (GPS-less jumps fold the plane ride
# into "boarding", so the door sequence is the end of that long boarding scene).
_AIRCRAFT_SCENES = ("plane", "takeoff", "boarding")
_DOOR_PREP_S = 8.0   # tail of the aircraft scene = approaching door / exit prep / looking out
_MILESTONE_S = 3.0   # default length of a milestone beat in the highlights
_HL_INTRO_S = 8.0    # the intro gets a longer highlight beat (it sets up the story)
# The exit "story" is captured as ONE continuous block before any AI scoring kicks in:
# the door-prep tail + the first seconds of freefall (exit → jump → initial freefall →
# stabilization). Longer for the dedicated freefall video, shorter for the highlights.
_EXIT_SEQUENCE_S = 12.0
_HL_EXIT_SEQUENCE_S = 6.0


def _scene_by_name(scenes: Sequence[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((s for s in scenes if s["name"] == name), None)


def _freefall_exit(scenes: Sequence[dict[str, Any]]) -> float:
    """Detected exit offset (seconds into the freefall scene); 0.0 if none/at start."""
    ff = _scene_by_name(scenes, "freefall")
    return float(ff.get("exit_offset", 0.0)) if ff else 0.0


def _freefall_deploy(scenes: Sequence[dict[str, Any]]) -> float:
    """Detected canopy-opening offset within the freefall scene; 0.0 if none/separate."""
    ff = _scene_by_name(scenes, "freefall")
    return float(ff.get("deploy_offset", 0.0)) if ff else 0.0


def _canopy_opening(scenes: Sequence[dict[str, Any]]) -> list[Clip]:
    """The canopy-opening (deployment) beat, slowed — from the canopy scene if there is
    one, otherwise from inside the freefall clip at the detected deployment offset."""
    canopy = _scene_by_name(scenes, "canopy")
    if canopy is not None:
        cdur = max(float(canopy["duration"]), 0.1)
        return _segment_with_slowmo("canopy", 0.0, min(_DEPLOY_BEAT_S, cdur), [0.0])
    ff = _scene_by_name(scenes, "freefall")
    deploy = _freefall_deploy(scenes)
    if ff is not None and deploy > 0:
        fdur = max(float(ff["duration"]), 0.1)
        return _segment_with_slowmo(
            "freefall", deploy, min(deploy + _DEPLOY_BEAT_S, fdur), [deploy]
        )
    return []


def _freefall_moment_end(scenes: Sequence[dict[str, Any]], end: float) -> float:
    """Cap freefall best-moment selection before the deployment (don't mine canopy)."""
    deploy = _freefall_deploy(scenes)
    return min(end, deploy) if deploy > 0 else end


def _scene_index(scenes: Sequence[dict[str, Any]], name: str) -> int:
    return next((i for i, s in enumerate(scenes) if s["name"] == name), -1)


def _window(scene: dict[str, Any], start: float, length: float) -> Clip:
    """A clamped clip of ``length`` seconds from ``scene`` starting at ``start``."""
    dur = max(float(scene["duration"]), 0.1)
    a = max(0.0, min(start, dur - 0.05))
    b = min(dur, a + length)
    return Clip(scene=scene["name"], src_start=a, src_end=max(b, a + 0.05))


def _aircraft_before_freefall(scenes: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The aircraft scene whose tail holds the door / exit-prep, just before freefall."""
    fi = _scene_index(scenes, "freefall")
    if fi <= 0:
        return None
    prev = scenes[fi - 1]
    return prev if prev["name"] in _AIRCRAFT_SCENES else None


# Output-duration budgets per deliverable (seconds). Slow-mo overhead is modest, so
# budgeting the source seconds a little under the target keeps the encode in bounds.
_FULL_MAX_SOURCE_S = 210      # → full video stays under ~4 min after slow-mo
_HIGHLIGHTS_TARGET_S = 40     # → highlights stays under 1 min
_FREEFALL_MAX_OUTPUT_S = 68   # → freefall stays under 1:15
_DEPLOY_BEAT_S = 2.5          # length of the deployment / canopy-opening beat


def _curated_freefall(
    scenes: list[dict[str, Any]],
    scores: dict[str, list[dict[str, float]]],
    beats: int | None = None,
) -> list[Clip]:
    """Curate the freefall video: door/exit-prep → exit/jump → freefall → deployment.

    Leads with the approach-the-door / exit-prep (tail of the aircraft scene), then the
    aircraft exit (anchored at the freefall scene start and ALWAYS included), the best
    freefall moments, and the canopy-opening (deployment) beat. No general boarding,
    canopy-flight, landing, or outro footage. Capped under :data:`_FREEFALL_MAX_OUTPUT_S`.
    """
    ff = next((s for s in scenes if s["name"] == "freefall"), None)
    if ff is None:  # no freefall scene — stand in with the first scene so the EDL is valid
        first = scenes[0]
        return [
            Clip(scene=first["name"], src_start=0.0, src_end=max(float(first["duration"]), 0.1))
        ]

    dur = max(float(ff["duration"]), 0.1)
    rows = scores.get("freefall", [])
    _, end = _freefall_bounds(rows, dur)
    end = _freefall_moment_end(scenes, end)  # don't mine "freefall" beats from the canopy ride
    exit_off = max(0.0, min(_freefall_exit(scenes), dur - 0.1))  # detected exit within the clip

    clips: list[Clip] = []
    used = 0.0

    # 1) Door / exit-prep lead-in. If the freefall clip itself starts inside the plane
    #    (exit detected mid-clip), the approach/look-out is the seconds just before the
    #    exit; otherwise fall back to the tail of the aircraft scene.
    if exit_off > 1.0:
        prep = _window(ff, exit_off - _DOOR_PREP_S, _DOOR_PREP_S)
        clips.append(prep)
        used += _clip_out_dur(prep)
    else:
        aircraft = _aircraft_before_freefall(scenes)
        if aircraft is not None:
            adur = max(float(aircraft["duration"]), 0.1)
            prep = _window(aircraft, adur - _DOOR_PREP_S, _DOOR_PREP_S)
            clips.append(prep)
            used += _clip_out_dur(prep)

    # 2) The complete EXIT SEQUENCE as ONE continuous block, starting at the DETECTED
    #    exit (not ts 0): the exit/jump (slow-mo), the initial freefall, and the
    #    stabilisation — always included before any AI moments.
    exit_end = min(exit_off + _EXIT_SEQUENCE_S, dur)
    exit_seq = _segment_with_slowmo("freefall", exit_off, exit_end, [exit_off])
    clips.extend(exit_seq)
    used += sum(_clip_out_dur(c) for c in exit_seq)

    # 3) AI-selected best freefall moments, AFTER the exit sequence, until the budget.
    #    How many beats to feature is learned from past edits when available.
    n_beats = max(1, int(beats)) if beats else 6
    budget = _FREEFALL_MAX_OUTPUT_S - _DEPLOY_BEAT_S  # reserve room for the deployment
    for peak in _top_peaks(rows, n_beats, min_gap=4.0, lo=exit_end, hi=end):
        a, b = max(exit_end, peak - 2.0), min(end, peak + 2.0)
        seg = _segment_with_slowmo("freefall", a, b, [peak])
        seg_out = sum(_clip_out_dur(c) for c in seg)
        if used + seg_out > budget:
            break
        clips.extend(seg)
        used += seg_out

    # 4) Deployment: the canopy-opening beat (its own scene, or inside the freefall clip).
    clips.extend(_canopy_opening(scenes))
    return clips


def _highlight_milestones(
    scenes: Sequence[dict[str, Any]], scores: dict[str, list[dict[str, float]]]
) -> list[list[Clip]]:
    """Mandatory milestone beats for the highlights, in experience order.

    Every available milestone is represented BEFORE any score-based filler: the intro,
    aircraft entry, inside the aircraft, the door/exit-prep, the exit/jump, freefall,
    the canopy opening, the landing, and the outro. Each is a short clip from the right
    scene and position (the exit/jump anchored at the freefall start, always included).
    """
    out: list[list[Clip]] = []
    intro = _scene_by_name(scenes, "intro_interview")
    boarding = _scene_by_name(scenes, "boarding")
    plane = _scene_by_name(scenes, "plane")
    canopy = _scene_by_name(scenes, "canopy")
    ff = _scene_by_name(scenes, "freefall")
    outro = _scene_by_name(scenes, "outro_interview")
    aircraft = _aircraft_before_freefall(scenes)
    inside = plane or boarding

    if intro is not None:     # the intro / ground-prep moment — never skip it (a longer beat)
        out.append([_window(intro, 0.0, _HL_INTRO_S)])
    if boarding is not None:  # walking to / entering the aircraft
        out.append([_window(boarding, 0.0, _MILESTONE_S)])
    if inside is not None:    # inside-aircraft reaction (middle of the ride)
        mid = max(0.0, float(inside["duration"]) / 2 - _MILESTONE_S / 2)
        out.append([_window(inside, mid, _MILESTONE_S)])
    if aircraft is not None:  # door / gate / exit prep (tail of the aircraft scene)
        adur = max(float(aircraft["duration"]), 0.1)
        out.append([_window(aircraft, adur - _DOOR_PREP_S, _DOOR_PREP_S)])
    if ff is not None:        # the exit / jump + initial freefall — ONE continuous block
        fdur = max(float(ff["duration"]), 0.1)
        exit_off = max(0.0, min(_freefall_exit(scenes), fdur - 0.1))  # detected exit
        exit_end = min(exit_off + _HL_EXIT_SEQUENCE_S, fdur)
        out.append(_segment_with_slowmo("freefall", exit_off, exit_end, [exit_off]))
        rows = scores.get("freefall", [])
        _, end = _freefall_bounds(rows, fdur)
        end = _freefall_moment_end(scenes, end)  # stay in freefall, not the canopy ride
        for pk in _top_peaks(rows, 2, min_gap=5.0, lo=exit_end, hi=end):  # best freefall beats
            out.append(
                _segment_with_slowmo("freefall", max(exit_end, pk - 1.5), min(end, pk + 1.5), [pk])
            )
    # The canopy-opening beat (a key moment) — from its own scene or inside the freefall
    # clip when the canopy ride wasn't split out. Always featured when detectable.
    opening = _canopy_opening(scenes)
    if opening:
        out.append(opening)
    if canopy is not None:    # landing reaction (tail of the canopy scene)
        cdur = max(float(canopy["duration"]), 0.1)
        out.append([_window(canopy, cdur - _MILESTONE_S, _MILESTONE_S)])
    if outro is not None:     # the outro moment
        out.append([_window(outro, 0.0, 2.0)])
    return out


def _house_edls(
    scores: dict[str, list[dict[str, float]]],
    manifest: dict[str, Any],
    target_duration: float,
    profile: dict[str, Any] | None = None,
) -> EDLResponse:
    """Build the three EDLs from scores + scene durations, no AI (offline fallback).

    Each deliverable is paced to its target length, and slow-mo is applied to short,
    high-value beats only (exit, freefall smiles, deployment) — never a whole scene.
    A learned ``profile`` (from past instructor edits) nudges the pacing: how long to
    keep each scene in the full video, and how many freefall beats to feature.
    """
    profile = profile or {}
    learned_seconds: dict[str, float] = profile.get("scene_seconds", {})
    learned_beats = profile.get("freefall_beats")
    scenes = manifest["scenes"]  # already in jump order from build_scenes
    exit_off = _freefall_exit(scenes)  # where the real exit is within the freefall clip
    deploy_off = _freefall_deploy(scenes)  # canopy opening inside the freefall clip (0 if separate)

    def _slow_points(name: str, hi: float) -> list[float]:
        """Slow-mo beats for a scene window: exit/deploy + freefall smiles (after exit)."""
        rows = scores.get(name, [])
        if name == "freefall":
            peaks = _top_peaks(rows, _FREEFALL_SLOWMO_PEAKS, lo=exit_off, hi=hi)
            extra = [deploy_off] if 0 < deploy_off < hi else []  # slow-mo the canopy opening
            return [exit_off, *peaks, *extra]
        if name == "canopy":
            return [0.0]  # deployment beat at canopy start
        return []

    # full_video: the complete experience. Each scene is trimmed to the learned kept
    # length when we have one, else proportionally to stay under 4 min.
    total_src = sum(max(float(s["duration"]), 0.1) for s in scenes)
    scale = min(1.0, _FULL_MAX_SOURCE_S / total_src) if total_src else 1.0
    full: list[Clip] = []
    for s in scenes:
        name = s["name"]
        dur = max(float(s["duration"]), 0.1)
        inc = min(dur, learned_seconds[name]) if name in learned_seconds else max(0.5, dur * scale)
        full.extend(_segment_with_slowmo(name, 0.0, inc, _slow_points(name, inc)))

    # highlights: milestones first (so the exit/jump, boarding, etc. are never dropped),
    # then fill the remaining budget with the best freefall beats — kept under a minute.
    hl_target = min(target_duration, _HIGHLIGHTS_TARGET_S)
    highlights: list[Clip] = []
    used = 0.0
    for beat in _highlight_milestones(scenes, scores):
        highlights.extend(beat)
        used += sum(_clip_out_dur(c) for c in beat)
    ff = _scene_by_name(scenes, "freefall")
    if ff is not None:  # fill with additional high-scoring freefall beats (past the exit block)
        rows = scores.get("freefall", [])
        _, end = _freefall_bounds(rows, max(float(ff["duration"]), 0.1))
        end = _freefall_moment_end(scenes, end)  # don't fill from the canopy ride
        for pk in _top_peaks(rows, 8, min_gap=4.0, lo=exit_off + _HL_EXIT_SEQUENCE_S, hi=end):
            beat = _segment_with_slowmo("freefall", max(0.0, pk - 1.5), min(end, pk + 1.5), [pk])
            beat_out = sum(_clip_out_dur(c) for c in beat)
            if used + beat_out > hl_target:
                break
            highlights.extend(beat)
            used += beat_out

    # freefall: a curated, capped cut (door/exit-prep + exit + windows + deployment).
    freefall = _curated_freefall(scenes, scores, beats=learned_beats)

    return EDLResponse(full_video=full, highlights=highlights, freefall=freefall)


# --------------------------------------------------------------------------- #
# Step 4 — render the three outputs in parallel
# --------------------------------------------------------------------------- #

_OUT_W, _OUT_H, _OUT_FPS = 1920, 1080, 30


def _norm(width: int, height: int, fps: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    )


_AUDIO_RATE = 44100
# Music levels: solo (it is the only audio) vs. tucked UNDER the original audio.
_MUSIC_SOLO = 0.90
_MUSIC_UNDER = 0.20
# Scenes that count as "boarding the aircraft" — where the music starts (full video).
_MUSIC_START_SCENES = ("boarding", "takeoff", "plane")
_STEREO = f"aresample={_AUDIO_RATE},aformat=channel_layouts=stereo"
_SILENCE = f"anullsrc=channel_layout=stereo:sample_rate={_AUDIO_RATE}"


def scene_has_audio(scene_path: str | Path) -> bool:
    """True if a scene file carries an audio stream (ffprobe seam; mocked in tests)."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(scene_path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return bool(out)


def _clip_out_dur(clip: Clip) -> float:
    return (clip.src_end - clip.src_start) / clip.speed_multiplier


def _stream_durations(path: str | Path) -> tuple[float, float]:
    """``(video_stream_duration, audio_stream_duration)`` in seconds, 0 when unknown
    (ffprobe seam; mocked in tests).

    Per-STREAM durations, not the container's. A ``-c copy`` concat of GoPro chapter
    clips yields a file whose audio stream outruns the video stream (GoPro audio runs a
    touch longer per chapter), so the container duration (= the longer audio) overstates
    how much VIDEO there is. Reading the streams separately exposes that gap.
    """
    def _one(select: str) -> float:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", select,
                "-show_entries", "stream=duration",
                "-of", "default=nokey=1:noprint_wrappers=1", str(path),
            ],
            capture_output=True, text=True, check=False,
        ).stdout.strip().splitlines()
        vals = [float(x) for x in out if x and x != "N/A"]
        return min(vals) if vals else 0.0

    return _one("v"), _one("a")


def scene_playable_duration(path: str | Path) -> float:
    """The span where BOTH the video and audio streams have data — the largest window a
    clip may safely ``trim`` without either stream running dry.

    This is the min of the two stream durations (falling back to the container duration
    when a stream reports none). Clamping to it prevents the "video freezes while audio
    keeps playing" desync: on a GoPro ``-c copy`` concat the audio stream is longer than
    the video, so a clip cut to the container length would ask ``trim`` for video frames
    that don't exist past the video stream's end.
    """
    video, audio = _stream_durations(path)
    present = [d for d in (video, audio) if d > 0]
    return min(present) if present else probe_duration(path)


#: Smallest clip we keep after clamping (shorter than this is dropped as degenerate).
_MIN_CLIP_S = 0.05


def _clamp_clips_to_scenes(
    clips: Sequence[Clip], scene_paths: dict[str, str]
) -> list[Clip]:
    """Clamp each clip's ``[src_start, src_end)`` to its scene file's PLAYABLE duration —
    the span where both the video and audio streams have data
    (:func:`scene_playable_duration`).

    A clip can never ask ``trim`` for footage past the end of either stream, which is what
    desyncs the video and audio (frozen frame, audio continues). A clip whose duration is
    unknown (probe 0, e.g. a missing file or a test seam) is left untouched; one that
    clamps away to nothing is dropped. Never returns empty: if everything would drop, the
    originals are kept so the render still has something to cut.
    """
    durations: dict[str, float] = {}

    def _duration(key: str) -> float:
        if key not in durations:
            path = scene_paths.get(key)
            durations[key] = scene_playable_duration(path) if path else 0.0
        return durations[key]

    out: list[Clip] = []
    for c in clips:
        dur = _duration(_scene_key(c))
        if dur <= 0:  # unknown duration -> can't clamp safely; leave as-is
            out.append(c)
            continue
        start = max(0.0, min(c.src_start, dur - _MIN_CLIP_S))
        end = min(dur, max(c.src_end, start + _MIN_CLIP_S))
        if end - start < _MIN_CLIP_S:  # fully out of range -> drop it
            continue
        if start == c.src_start and end == c.src_end:
            out.append(c)
        else:
            out.append(c.model_copy(update={"src_start": start, "src_end": end}))
    return out or list(clips)


def _audio_markers(
    clips: Sequence[Clip], deploy_offset: float = 0.0
) -> tuple[float, float | None]:
    """Output-timeline times for (music start = boarding, canopy opening).

    The canopy opening is where the original audio comes back and the music ducks.
    It's the first ``canopy`` clip — or, when the canopy ride wasn't split into its own
    scene, the first ``freefall`` clip at/after the detected ``deploy_offset``.
    """
    t = 0.0
    music_start: float | None = None
    canopy_start: float | None = None
    for clip in clips:
        if music_start is None and clip.scene in _MUSIC_START_SCENES:
            music_start = t
        if canopy_start is None:
            if clip.scene == "canopy":
                canopy_start = t
            elif (
                deploy_offset > 0
                and clip.scene == "freefall"
                and clip.src_start >= deploy_offset - 0.5
            ):
                canopy_start = t
        t += _clip_out_dur(clip)
    return (music_start or 0.0), canopy_start


def render_selfie_video(
    out_path: str | Path,
    clips: Sequence[Clip],
    scene_paths: dict[str, str],
    *,
    booking: dict[str, Any],
    music_path: str | None = None,
    music_only: bool = True,
    deploy_offset: float = 0.0,
    width: int = _OUT_W,
    height: int = _OUT_H,
    fps: int = _OUT_FPS,
) -> Path:
    """Render one selfie EDL to a 1080p/h264/aac/30fps MP4 (seam; mocked in tests).

    Clips are trimmed/speed-ramped and concatenated, ending on a logo fade-out (no
    opening title card). Audio depends on ``music_only``:

    * ``music_only=True`` (highlights, freefall) — the backing track is the *only*
      audio; the original camera sound is dropped and slow-mo clips never contribute
      stretched audio.
    * ``music_only=False`` (full video) — a cinematic mix: music starts at boarding
      and is the only audio through the exit/freefall (original muted there), then at
      the canopy opening the original audio is re-enabled with the music ducked
      underneath. Slow-mo clips still contribute no stretched audio (music covers).
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Clamp every clip to its scene file's REAL duration before building the filter. An
    # EDL time past the end of a scene (the model can emit one; a concat can come up a
    # frame short of the summed estimate) makes FFmpeg's video ``trim`` stop early while
    # the matching audio segment is built to the full declared length — the video stream
    # then ends before the audio and, under ``-t total``, the player freezes the last
    # frame while the soundtrack keeps going. Clamping keeps the two streams the same
    # length so the whole edit plays through, audio and video in sync.
    clips = _clamp_clips_to_scenes(clips, scene_paths)

    inputs: list[tuple[str, tuple[str, ...]]] = []

    def add_input(path: str, pre: tuple[str, ...] = ()) -> int:
        inputs.append((path, pre))
        return len(inputs) - 1

    index_of: dict[str, int] = {}
    for clip in clips:
        key = _scene_key(clip)
        if key not in index_of:
            src = scene_paths.get(key)
            if src is None:
                raise SelfieError(f"EDL references unknown scene {key!r}")
            index_of[key] = add_input(src)
    has_audio = {i: scene_has_audio(p) for i, (p, _) in enumerate(inputs)}

    norm = _norm(width, height, fps)
    total = sum(_clip_out_dur(c) for c in clips) + _CARD_SECONDS
    music_start, canopy_start = (
        (0.0, None) if music_only else _audio_markers(clips, deploy_offset)
    )
    inf = float("inf")
    mute_lo = music_start if not music_only else 0.0
    mute_hi = canopy_start if (not music_only and canopy_start is not None) else inf

    with tempfile.TemporaryDirectory(prefix=f"selfie-{out.stem}-") as tmp:
        chains: list[str] = []
        vlabels: list[str] = []
        alabels: list[str] = []
        out_t = 0.0
        for k, clip in enumerate(clips):
            i = index_of[_scene_key(clip)]
            dur = _clip_out_dur(clip)
            chains.append(
                f"[{i}:v]trim={clip.src_start:.3f}:{clip.src_end:.3f},"
                f"setpts=(PTS-STARTPTS)/{clip.speed_multiplier:.4f},{norm}[v{k}]"
            )
            vlabels.append(f"v{k}")
            # Original audio plays only when: not a music-only edit, the source has
            # audio, the clip is real-time (no stretched slow-mo audio), and we're
            # outside the boarding→canopy "music takes over" window.
            use_original = (
                not music_only
                and has_audio[i]
                and clip.speed_multiplier == 1.0
                and not (mute_lo <= out_t < mute_hi)
            )
            if use_original:
                chains.append(
                    f"[{i}:a]atrim={clip.src_start:.3f}:{clip.src_end:.3f},"
                    f"asetpts=PTS-STARTPTS,{_STEREO}[a{k}]"
                )
            else:
                chains.append(f"{_SILENCE},atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{k}]")
            alabels.append(f"a{k}")
            out_t += dur

        outro_idx = add_input(_card_input(Path(tmp), "outro", booking, width, height, fps))
        chains.append(f"[{outro_idx}:v]{norm}[voutro]")
        vlabels.append("voutro")
        chains.append(f"{_SILENCE},atrim=0:{_CARD_SECONDS},asetpts=PTS-STARTPTS[aoutro]")
        alabels.append("aoutro")

        chains.append("".join(f"[{v}]" for v in vlabels) + f"concat=n={len(vlabels)}:v=1:a=0[vout]")
        chains.append("".join(f"[{a}]" for a in alabels) + f"concat=n={len(alabels)}:v=0:a=1[amb]")

        audio_label = "amb"
        if music_path:
            music_idx = add_input(music_path, ("-stream_loop", "-1"))
            if music_only:
                # Constant solo music for the whole edit.
                chains.append(
                    f"[{music_idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,{_STEREO},"
                    f"volume={_MUSIC_SOLO},apad=whole_dur={total:.3f}[mus]"
                )
            else:
                # Solo from boarding until canopy, then ducked under the original audio.
                rel_calm = (
                    max(0.0, canopy_start - music_start) if canopy_start is not None else total
                )
                delay = f"adelay={int(music_start * 1000)}:all=1," if music_start > 0.01 else ""
                chains.append(
                    f"[{music_idx}:a]atrim=0:{max(0.1, total - music_start):.3f},"
                    f"asetpts=PTS-STARTPTS,{_STEREO},"
                    f"volume='if(lt(t,{rel_calm:.3f}),{_MUSIC_SOLO},{_MUSIC_UNDER})':eval=frame,"
                    f"{delay}apad=whole_dur={total:.3f}[mus]"
                )
            chains.append("[amb][mus]amix=inputs=2:duration=longest:normalize=0[aout]")
            audio_label = "aout"

        cmd = ["ffmpeg", "-v", "error", "-y"]
        for path, pre in inputs:
            cmd += [*pre, "-i", path]
        cmd += [
            "-filter_complex", ";".join(chains),
            "-map", "[vout]", "-map", f"[{audio_label}]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-t", f"{total:.3f}", str(out),
        ]
        _run_ffmpeg(cmd)
    return out


# Card lengths (seconds) and the cross-fade applied at both ends of the outro logo.
_CARD_SECONDS = 2.5
_CARD_FADE = 0.4

# Where the branded outro logo is resolved from (override with $OUTRO_LOGO).
_LOGO_ENV = "OUTRO_LOGO"
_DEFAULT_LOGO = Path(__file__).resolve().parent.parent / "templates" / "logo.png"


def resolve_outro_logo() -> Path | None:
    """The Parachute Montreal outro logo: ``$OUTRO_LOGO`` > ``templates/logo.png``."""
    env = os.environ.get(_LOGO_ENV)
    if env and Path(env).exists():
        return Path(env)
    return _DEFAULT_LOGO if _DEFAULT_LOGO.exists() else None


def _card_input(
    tmp_dir: Path, kind: str, booking: dict[str, Any], width: int, height: int, fps: int
) -> str:
    """Synthesise an intro (name/date) or outro (logo) card with smooth fades.

    Text/logo is drawn with Pillow into a PNG and composited over a solid background
    with FFmpeg's always-available ``overlay`` filter (never ``drawtext`` — absent on
    many Homebrew/distro builds). The outro is the branded Parachute Montreal logo
    fading in and out; with no logo asset it falls back to a simple wordmark card.
    """
    out = tmp_dir / f"_{kind}.mp4"
    fade = (
        f"fade=t=in:st=0:d={_CARD_FADE},"
        f"fade=t=out:st={_CARD_SECONDS - _CARD_FADE:.2f}:d={_CARD_FADE}"
    )

    logo = resolve_outro_logo() if kind == "outro" else None
    if logo is not None:
        # Logo centred on white, scaled to ~45% width, fading in and out.
        _run_ffmpeg(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi",
                "-i", f"color=c=white:size={width}x{height}:duration={_CARD_SECONDS}:rate={fps}",
                "-loop", "1", "-t", str(_CARD_SECONDS), "-i", str(logo),
                "-filter_complex",
                f"[1:v]scale={int(width * 0.45)}:-1[lg];"
                f"[0:v][lg]overlay=(W-w)/2:(H-h)/2,{fade},format=yuv420p[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
            ]
        )
        return str(out)

    # Text card (intro: customer name + date; outro fallback: wordmark).
    from render.caption import render_caption

    png = tmp_dir / f"_{kind}_caption.png"
    name = str(booking.get("customer_name") or "Valued Skydiver")
    date = str(booking.get("jump_date") or "")
    headline, subline = (name, date) if kind == "intro" else ("Parachute Montreal", "")
    render_caption(png, customer_name=headline, jump_date=subline, width=width, height=height)
    _run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:size={width}x{height}:duration={_CARD_SECONDS}:rate={fps}",
            "-loop", "1", "-t", str(_CARD_SECONDS), "-i", str(png),
            "-filter_complex", f"[0:v][1:v]overlay=0:0,{fade},format=yuv420p[v]",
            "-map", "[v]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ]
    )
    return str(out)


def render_outputs(
    job_id: str,
    edls: EDLResponse,
    manifest: dict[str, Any],
    booking: dict[str, Any],
    jobs_root: str | Path | None = None,
    *,
    music_paths: dict[str, str | None] | None = None,
) -> dict[str, str]:
    """Render full_video, highlights, and freefall to MP4 (each: footage + logo outro).

    All three mix the original audio with a backing track underneath; ``music_paths``
    may give a different track per deliverable (the three need not share music). The
    encodes run **sequentially** so each gets the whole CPU — three concurrent 1080p
    x264 passes just oversubscribe the cores and make the long ``full_video`` crawl.
    """
    music_paths = music_paths or {}
    jd = job_dir(job_id, jobs_root)
    scene_paths = {s["name"]: s["combined_path"] for s in manifest["scenes"]}
    # The canopy opening (where the full video brings the original audio back and ducks
    # the music) may be inside the freefall clip when the canopy wasn't its own scene.
    deploy_offset = _freefall_deploy(manifest["scenes"])
    # full_video uses the cinematic mix (music → ambient at canopy); the highlights and
    # freefall cuts are music-only.
    targets = {
        "full_video": (jd / "full_video.mp4", edls.full_video, False),
        "highlights": (jd / "highlights.mp4", edls.highlights, True),
        "freefall": (jd / "freefall.mp4", edls.freefall, True),
    }

    for name, (out, clips, music_only) in targets.items():
        render_selfie_video(
            out, clips, scene_paths, booking=booking,
            music_path=music_paths.get(name), music_only=music_only,
            deploy_offset=deploy_offset,
        )
    return {name: str(out) for name, (out, *_rest) in targets.items()}


# --------------------------------------------------------------------------- #
# Step 5 — photo extraction
# --------------------------------------------------------------------------- #


def dump_scene_jpegs(
    scene_path: str | Path, out_dir: Path, *, fps: float = PHOTO_FPS
) -> list[tuple[int, Path]]:
    """Dump full-res JPEG frames from a scene at ``fps`` (seam; mocked in tests).

    Returns ``(second, jpeg_path)`` for each frame. Full resolution (no downscale)
    so the photos are print-quality, unlike the small frames the scorer works on.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(scene_path),
            "-vf", f"fps={fps}",
            "-q:v", "2",
            str(out_dir / "frame_%05d.jpg"),
        ]
    )
    frames: list[tuple[int, Path]] = []
    for p in sorted(out_dir.glob("frame_*.jpg")):
        # ffmpeg numbers frames from 1; at fps the k-th frame is ~(k-1)/fps seconds.
        idx = int(p.stem.split("_")[1])
        frames.append((int((idx - 1) / fps), p))
    return frames


def frame_quality(jpeg_path: str | Path) -> tuple[float, float]:
    """Return ``(sharpness, exposure)`` for a JPEG (seam; mocked in tests).

    ``sharpness`` is the variance of the Laplacian (low = motion-blurred/soft);
    ``exposure`` peaks at 1.0 for a mid-bright frame and falls off when over/under
    exposed. Computed on a downscaled grey copy for speed.
    """
    import numpy as np
    from PIL import Image

    img = Image.open(jpeg_path).convert("L")
    img.thumbnail((320, 320))
    a = np.asarray(img, dtype="float64")
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0, 0.0
    lap = a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:] - 4 * a[1:-1, 1:-1]
    sharpness = float(lap.var())
    brightness = float(a.mean()) / 255.0
    exposure = max(0.0, 1.0 - 2.0 * abs(brightness - 0.5))
    return sharpness, exposure


def _photo_score(row: dict[str, float], sharp_norm: float, exposure: float) -> float:
    """Composite photo quality: face metrics + image sharpness/exposure."""
    return (
        0.30 * row.get("smile", 0.0)
        + 0.15 * row.get("eye_contact", 0.0)       # camera-facing
        + 0.20 * row.get("face_in_frame", 0.0)     # face visible / sized
        + 0.15 * row.get("face_centered", 0.0)     # composition
        + 0.15 * sharp_norm                        # sharp, not motion-blurred
        + 0.05 * exposure                          # well-lit
    )


def extract_photos(
    job_id: str,
    scores: dict[str, list[dict[str, float]]],
    manifest: dict[str, Any],
    jobs_root: str | Path | None = None,
    *,
    target: int = MAX_PHOTOS,
    min_gap: float = _PHOTO_MIN_GAP_S,
    min_visible: float = _PHOTO_MIN_VISIBLE,
    backfill: bool = False,
) -> list[dict[str, Any]]:
    """Select the best photos across the whole jump (aim 55+); write ``index.json``.

    Mines full-res frames from *every* scene (intro → boarding → plane → freefall →
    canopy → landing → outro), ranks each by a face + image-quality score, drops
    near-duplicates (``min_gap`` seconds within a scene), then **distributes** the
    picks round-robin across scenes so the set covers the whole experience rather than
    a pile of freefall frames. Keeps every strong, de-duplicated frame up to ``target``,
    best-first.

    The photo-only package raises ``target`` (and tightens ``min_gap`` to widen the
    pool) so the set reaches the 90–100 it asks for; the selfie package keeps the
    defaults. ``min_visible`` is the minimum ``face_in_frame`` for a frame to qualify.

    ``backfill`` lifts the face gate: a frame with no detected face scores 0 on every
    face signal (the scorer returns zeros), so distant **external** (camera-flyer)
    footage — where MediaPipe rarely locks onto the far-away tandem — yields almost no
    face-gated candidates and the set comes back tiny. With ``backfill`` on, every
    scored second is a candidate and frames are ranked by the same composite score, so
    detected faces still sort first but the remaining slots fill with the sharpest,
    best-exposed frames — guaranteeing a full ~``target`` set whenever footage exists.
    """
    photos_dir = job_dir(job_id, jobs_root) / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"photos-{job_id}-") as tmp:
        tmp_dir = Path(tmp)
        # 1) Gather candidate frames per scene with raw quality metrics. Without backfill
        #    a frame must show a face (face_in_frame >= min_visible); with backfill every
        #    scored second qualifies and the composite score below sorts faces to the top.
        per_scene: dict[str, list[dict[str, Any]]] = {}
        all_sharp: list[float] = []
        for scene in manifest["scenes"]:
            name = scene["name"]
            path = scene.get("combined_path")
            rows = scores.get(name)
            if not path or not rows:
                continue
            rows_by_second = {int(r["ts"]): r for r in rows}
            cands: list[dict[str, Any]] = []
            for ts, jpg in dump_scene_jpegs(path, tmp_dir / name):
                row = rows_by_second.get(ts)
                if row is None:
                    continue
                if not backfill and row.get("face_in_frame", 0.0) < min_visible:
                    continue
                sharp, exposure = frame_quality(jpg)
                all_sharp.append(sharp)
                cands.append(
                    {"ts": ts, "jpg": jpg, "row": row, "sharp": sharp, "exposure": exposure}
                )
            if cands:
                per_scene[name] = cands

        # 2) Normalise sharpness across all candidates, then score + de-duplicate.
        lo = min(all_sharp) if all_sharp else 0.0
        hi = max(all_sharp) if all_sharp else 1.0

        def _sharp_norm(s: float) -> float:
            return (s - lo) / (hi - lo) if hi > lo else 0.5

        ranked: dict[str, list[dict[str, Any]]] = {}
        for name, cands in per_scene.items():
            for c in cands:
                c["score"] = _photo_score(c["row"], _sharp_norm(c["sharp"]), c["exposure"])
            cands.sort(key=lambda c: c["score"], reverse=True)
            kept: list[dict[str, Any]] = []
            for c in cands:  # greedy: drop anything too close in time to a better pick
                if all(abs(c["ts"] - k["ts"]) >= min_gap for k in kept):
                    kept.append(c)
            ranked[name] = kept

        # 3) Distribute round-robin across scenes (coverage before depth), up to target.
        order = [s["name"] for s in manifest["scenes"] if s["name"] in ranked]
        selected: list[tuple[str, dict[str, Any]]] = []
        depth = 0
        while len(selected) < target and any(depth < len(ranked[n]) for n in order):
            for name in order:
                if depth < len(ranked[name]):
                    selected.append((name, ranked[name][depth]))
                    if len(selected) >= target:
                        break
            depth += 1

        # 4) Persist best-first, copying the chosen full-res JPEGs out of the temp dir.
        selected.sort(key=lambda sc: sc[1]["score"], reverse=True)
        index: list[dict[str, Any]] = []
        for name, c in selected:
            filename = f"{name}_{c['ts']}.jpg"
            shutil.copyfile(c["jpg"], photos_dir / filename)
            index.append(
                {
                    "filename": filename,
                    "ts": float(c["ts"]),
                    "scene": name,
                    "score": round(c["score"], 4),
                    "smile": c["row"].get("smile", 0.0),
                    "eye_contact": c["row"].get("eye_contact", 0.0),
                    "sharpness": round(_sharp_norm(c["sharp"]), 4),
                }
            )
        (photos_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return index


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def run_selfie_pipeline(
    job_id: str,
    *,
    store: Any,
    jobs_root: str | Path | None = None,
    client: Any | None = None,
) -> dict[str, str]:
    """Run the scene pipeline for a job and return its ``outputs`` map.

    Scenes and per-second scores are built for every package (both the videos and the
    photos are derived from them), then the package decides which deliverables get
    produced:

    * ``selfie`` — the three videos *and* the photos.
    * ``video_only`` — the three videos only (no photos).
    * ``photo_only`` — only the photos (90–100 best moments); the Claude compose call
      and the three renders are skipped entirely.

    Marks the job ``processing`` up front and ``ready`` (with ``outputs``) at the
    end. Raises on any failure — the caller (the Celery task) flips the job to
    ``failed`` and records the error.
    """
    from .jobs import JobStatus, Package

    # The two-camera Ultimate product has its own orchestrator (per-camera scene sets,
    # four deliverables); branch before the single-camera scene build below so the
    # selfie / video-only / photo-only paths stay exactly as they were.
    if store.load(job_id).package.is_ultimum:
        return run_ultimum_pipeline(
            job_id, store=store, jobs_root=jobs_root, client=client
        )

    _require_ffmpeg()
    store.update(job_id, status=JobStatus.processing, error=None)

    booking = json.loads(store.booking_path(job_id).read_text())
    job = store.load(job_id)
    package = job.package

    # Step 1 — classify + assemble scenes (the shared substrate for videos and photos).
    classified = classify_files(store.raw_dir(job_id))
    manifest = build_scenes(job_id, classified, jobs_root)

    # Step 2 — per-second MediaPipe scoring (drives both the EDLs and the photo ranking).
    scores = score_scenes(manifest, job_id, jobs_root)

    outputs: dict[str, str] = {}

    # Steps 3–4 — the three videos (skipped for the photo-only package). One Claude call
    # produces the recipes (persisted, so they stay re-editable); any exclude.json
    # time-cuts are applied at render. The external (camera-flyer) package composes
    # deterministically: its distant footage scores too few faces for the AI editor to
    # sequence reliably, so the house cut's guaranteed complete, in-order edit wins.
    if package.makes_videos:
        edls = compose_edls(
            scores, manifest, booking, job_id, jobs_root,
            client=client, target_duration=job.target_duration,
            use_ai=package != Package.external,
        )
        edls = apply_exclusions(edls, load_exclusions(job_id, jobs_root))
        outputs.update(
            render_outputs(
                job_id, edls, manifest, booking, jobs_root,
                music_paths=_music_paths(booking, job_id, jobs_root),
            )
        )

    # Step 5 — photos (skipped for the video-only package). The photo-only package asks
    # for a fuller set, so it targets 90–100 with a wider candidate pool.
    if package.makes_photos:
        if package == Package.photo_only:
            extract_photos(
                job_id, scores, manifest, jobs_root,
                target=PHOTO_ONLY_TARGET, min_gap=_PHOTO_ONLY_MIN_GAP_S,
                backfill=True,
            )
        else:  # selfie / external — guarantee ~50. Distant camera-flyer footage scores
            # almost no faces, so backfill ranks the whole jump by image quality to fill.
            extract_photos(
                job_id, scores, manifest, jobs_root,
                target=SELFIE_PHOTO_TARGET, min_gap=_SELFIE_PHOTO_MIN_GAP_S,
                min_visible=_SELFIE_PHOTO_MIN_VISIBLE, backfill=True,
            )
        outputs["photos"] = str(job_dir(job_id, jobs_root) / "photos")

    store.update(job_id, status=JobStatus.ready, outputs=outputs)
    return outputs


def _uploaded_music(
    job_id: str | None, jobs_root: str | Path | None, deliverable: str
) -> str | None:
    """A per-deliverable track uploaded into ``jobs/<id>/music/`` for this job, or None.

    Files are stored as ``music/<deliverable>.<ext>``; this returns the path of the one
    whose stem matches ``deliverable`` (any accepted audio suffix). Takes precedence over
    the global ``templates/music`` library so a job can carry its own tracks.
    """
    if job_id is None:
        return None
    from .jobs import MUSIC_SUFFIXES

    mdir = job_dir(job_id, jobs_root) / "music"
    if not mdir.is_dir():
        return None
    for p in sorted(mdir.iterdir()):
        if p.stem == deliverable and p.suffix.lower() in MUSIC_SUFFIXES:
            return str(p)
    return None


def _music_picker(
    booking: dict[str, Any],
    job_id: str | None,
    jobs_root: str | Path | None,
) -> Callable[[str, str], str | None]:
    """A resolver ``(deliverable, booking_key) -> track``: uploaded job file → booking
    per-deliverable name → base ``music`` name (each against ``templates/music``)."""
    base = _resolve_music(booking.get("music"))

    def pick(deliverable: str, booking_key: str) -> str | None:
        return (
            _uploaded_music(job_id, jobs_root, deliverable)
            or _resolve_music(booking.get(booking_key))
            or base
        )

    return pick


def _music_paths(
    booking: dict[str, Any],
    job_id: str | None = None,
    jobs_root: str | Path | None = None,
) -> dict[str, str | None]:
    """Per-deliverable backing tracks: uploaded job music first, else booking/template.

    An uploaded ``jobs/<id>/music/<deliverable>.<ext>`` wins; otherwise the booking's
    per-deliverable or base ``music`` name resolves against ``templates/music`` — so
    existing template-only jobs keep working unchanged.
    """
    pick = _music_picker(booking, job_id, jobs_root)
    return {
        "full_video": pick("full_video", "music_full"),
        "highlights": pick("highlights", "music_highlights"),
        "freefall": pick("freefall", "music_freefall"),
    }


# --------------------------------------------------------------------------- #
# The Ultimate package: two cameras, four video deliverables + photos.
#
# Each camera keeps its OWN scene set (``scenes_instructor/`` and ``scenes_external/``);
# the two are never concatenated into one file. Reuses the selfie editing logic on those
# per-camera sets:
#   * full_video + highlights  -> a true MULTI-CAM combo: the instructor and cameraman
#                                 house cuts are built separately, then merged scene by
#                                 scene and interleaved so both angles appear for every
#                                 event (the cameraman's exit/freefall/canopy/landing are
#                                 cut in alongside the instructor's, never dropped).
#   * external_freefall        -> _curated_freefall over the external cam alone
#   * chute_libre_selfie       -> _curated_freefall over the instructor cam alone
# All four are persisted as re-editable EDLs (combo clips carry a ``camera`` tag) and the
# two scene sets stay on disk so a tweak/replay re-renders without re-classify/re-score.
# --------------------------------------------------------------------------- #

#: The two camera-scoped freefall deliverables -> the role each is built from.
ULTIMUM_FREEFALL_ROLE: dict[str, str] = {
    "external_freefall": "external",
    "chute_libre_selfie": "instructor",
}
#: Persisted, re-editable EDL filename for each Ultimate deliverable.
ULTIMUM_EDL_FILES: dict[str, str] = {
    "full_video": "edl_full.json",
    "highlights": "edl_highlights.json",
    "external_freefall": "edl_external_freefall.json",
    "chute_libre_selfie": "edl_chute_libre.json",
}
#: Manifest holding the scenes each deliverable renders against ("" = combined set).
_ULTIMUM_ROLE_MANIFEST = {role: f"scene_manifest_{role}.json" for role in CAMERA_ROLES}


def _ultimum_music_paths(
    booking: dict[str, Any],
    job_id: str | None = None,
    jobs_root: str | Path | None = None,
) -> dict[str, str | None]:
    """Per-deliverable backing tracks for the Ultimate package.

    Mirrors :func:`_music_paths` over the four Ultimate deliverables: an uploaded
    ``jobs/<id>/music/<deliverable>.<ext>`` wins, else the booking's per-deliverable or
    base ``music`` name resolves against ``templates/music``.
    """
    pick = _music_picker(booking, job_id, jobs_root)
    return {
        "full_video": pick("full_video", "music_full"),
        "highlights": pick("highlights", "music_highlights"),
        "external_freefall": pick("external_freefall", "music_external_freefall"),
        "chute_libre_selfie": pick("chute_libre_selfie", "music_chute_libre"),
    }


def _persist_clips(jd: Path, filename: str, clips: Sequence[Clip]) -> None:
    """Write one deliverable's clip list as a re-editable EDL JSON file."""
    (jd / filename).write_text(
        json.dumps([c.model_dump() for c in clips], indent=2) + "\n"
    )


def _scene_paths(manifest: dict[str, Any]) -> dict[str, str]:
    return {s["name"]: s["combined_path"] for s in manifest["scenes"]}


def _role_labels_path(jd: Path, role: str) -> str | None:
    """Optional per-camera scene-label overrides (``scene_labels_<role>.json``)."""
    p = jd / f"scene_labels_{role}.json"
    return str(p) if p.exists() else None


def _build_role_scene_set(
    job_id: str,
    role: str,
    raw_dir: Path,
    jobs_root: str | Path | None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, float]]]]:
    """Classify + score ONE camera's clips into its own scene set, returning the manifest
    and per-second scores.

    Each camera's scenes live in ``scenes_<role>/`` with a ``scene_manifest_<role>.json``
    (kept on disk so replay re-renders without re-classifying). This is the shared
    substrate for BOTH the multi-cam combo and that camera's own freefall cut, so it runs
    once per camera. Reuses the same telemetry classification and MediaPipe scoring as the
    single-camera pipeline — only the footage is camera-scoped.
    """
    jd = job_dir(job_id, jobs_root)
    classified = classify_camera_files(
        [raw_dir], labels_path=_role_labels_path(jd, role)
    )
    manifest = build_scenes(
        job_id, classified, jobs_root,
        scenes_subdir=f"scenes_{role}",
        manifest_name=_ULTIMUM_ROLE_MANIFEST[role],
    )
    scores = score_scenes(manifest, job_id, jobs_root, scores_name=f"scores_{role}.json")
    return manifest, scores


def _multicam_scene_paths(role_manifests: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Scene-file map for the combo render, keyed by ``<role>/<scene>`` so a camera-tagged
    clip resolves to its own camera's file. A bare ``<scene>`` fallback (first camera that
    has it, instructor preferred) is also added so an untagged/legacy clip still resolves.
    """
    paths: dict[str, str] = {}
    for role in CAMERA_ROLES:
        manifest = role_manifests.get(role)
        if not manifest:
            continue
        for s in manifest["scenes"]:
            paths[f"{role}/{s['name']}"] = s["combined_path"]
            paths.setdefault(s["name"], s["combined_path"])  # legacy/untagged fallback
    return paths


def _interleave_clips(lists: Sequence[list[Clip]]) -> list[Clip]:
    """Round-robin interleave several clip lists: a[0], b[0], a[1], b[1], … — so the cut
    alternates between the camera angles rather than playing one camera then the other."""
    out: list[Clip] = []
    depth = 0
    while any(depth < len(lst) for lst in lists):
        for lst in lists:
            if depth < len(lst):
                out.append(lst[depth])
        depth += 1
    return out


def _merge_multicam(per_cam_clips: dict[str, list[Clip]]) -> list[Clip]:
    """Merge each camera's edit into one multi-cam timeline.

    Clips are grouped by scene, tagged with their camera, then — scene by scene in jump
    order — the cameras' clips for that scene are interleaved. The result keeps the
    correct story order (intro → boarding → … → landing) while alternating angles within
    every event, so the cameraman's footage appears throughout instead of being dropped.
    """
    grouped: dict[str, dict[str, list[Clip]]] = {}
    for role, clips in per_cam_clips.items():
        for c in clips:
            grouped.setdefault(c.scene, {}).setdefault(role, []).append(
                c.model_copy(update={"camera": role})
            )
    out: list[Clip] = []
    for scene in sorted(grouped, key=_scene_rank):
        role_lists = [grouped[scene][r] for r in CAMERA_ROLES if r in grouped[scene]]
        out.extend(_interleave_clips(role_lists))
    return out


def compose_combo_edls(
    role_manifests: dict[str, dict[str, Any]],
    role_scores: dict[str, dict[str, list[dict[str, float]]]],
    *,
    target_duration: float,
    profile: dict[str, Any] | None = None,
) -> EDLResponse:
    """Compose the multi-cam combo full video + highlights from the per-camera scene sets.

    Each camera gets its own deterministic house cut (complete, in-order, every milestone
    present); the two are then merged and interleaved by :func:`_merge_multicam` so both
    the instructor and the cameraman angle are featured for every event. Returns an
    :class:`EDLResponse` whose ``full_video``/``highlights`` are the combo and whose
    ``freefall`` is unused (the combo renders no freefall deliverable — the two camera
    freefall cuts are produced separately).
    """
    per_full: dict[str, list[Clip]] = {}
    per_high: dict[str, list[Clip]] = {}
    for role in CAMERA_ROLES:
        manifest = role_manifests.get(role)
        if not manifest or not manifest.get("scenes"):
            continue
        edls = _house_edls(
            role_scores.get(role) or {}, manifest, target_duration, profile
        )
        per_full[role] = edls.full_video
        per_high[role] = edls.highlights
    full = _merge_multicam(per_full)
    highs = _merge_multicam(per_high)
    if not full or not highs:
        raise SelfieError("combo compose produced an empty edit (no usable camera scenes)")
    return EDLResponse(full_video=full, highlights=highs, freefall=full)


def _merge_photo_inputs(
    role_manifests: dict[str, dict[str, Any]],
    role_scores: dict[str, dict[str, list[dict[str, float]]]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, float]]]]:
    """Fold both cameras' scenes into one manifest+scores for photo extraction.

    Each scene is namespaced ``<role>_<scene>`` so the two cameras' same-named scenes
    don't collide and :func:`extract_photos` mines stills from BOTH (round-robin coverage
    spans every scene of both cameras). Scenes are ordered by jump phase for sensible
    distribution.
    """
    scenes: list[dict[str, Any]] = []
    scores: dict[str, list[dict[str, float]]] = {}
    for role in CAMERA_ROLES:
        manifest = role_manifests.get(role)
        cam_scores = role_scores.get(role) or {}
        if not manifest:
            continue
        for s in manifest["scenes"]:
            key = f"{role}_{s['name']}"
            scenes.append({**s, "name": key})
            if s["name"] in cam_scores:
                scores[key] = cam_scores[s["name"]]
    scenes.sort(key=lambda s: _scene_rank(s["name"].split("_", 1)[-1]))
    return {"scenes": scenes, "flagged": []}, scores


def run_ultimum_pipeline(
    job_id: str,
    *,
    store: Any,
    jobs_root: str | Path | None = None,
    client: Any | None = None,
) -> dict[str, str]:
    """Run the two-camera Ultimate pipeline and return its ``outputs`` map.

    Five deliverables:

    * ``full_video`` — a MULTI-CAM combo: the instructor and cameraman house cuts merged
      and interleaved (:func:`compose_combo_edls`) so both angles feature for every event
      (cinematic mix: music, with the original audio brought back + music ducked at the
      canopy opening).
    * ``highlights`` — the same combo, highlights length (music only).
    * ``external_freefall`` — the selfie freefall cut, from the external cameraman only
      (music only, no original audio).
    * ``chute_libre_selfie`` — the same freefall cut, from the instructor selfie cam
      only (music only).
    * ``photos`` — the best stills across the whole jump, mined from BOTH cameras'
      per-camera scene sets (:func:`_merge_photo_inputs` + ``extract_photos``).

    Each camera is classified + scored once into its own scene set (no combined concat);
    that set backs both the combo and that camera's freefall cut. The combo composes
    deterministically — distant cameraman footage scores too few faces for an AI editor
    to sequence, and the house cut guarantees a complete, in-order edit.

    Marks the job ``processing`` up front and ``ready`` (with ``outputs``) at the end;
    raises on any failure so the Celery task records it and the job never sticks in
    ``processing``.
    """
    from .jobs import JobStatus

    _require_ffmpeg()
    store.update(job_id, status=JobStatus.processing, error=None)

    booking = json.loads(store.booking_path(job_id).read_text())
    job = store.load(job_id)
    jd = job_dir(job_id, jobs_root)
    music = _ultimum_music_paths(booking, job_id, jobs_root)
    exclusions = load_exclusions(job_id, jobs_root)
    outputs: dict[str, str] = {}

    # --- Build each camera's OWN scene set + scores once (no combined concat). Reused for
    #     both the multi-cam combo and that camera's freefall cut. ---
    role_manifests: dict[str, dict[str, Any]] = {}
    role_scores: dict[str, dict[str, list[dict[str, float]]]] = {}
    for role in CAMERA_ROLES:
        manifest, scores = _build_role_scene_set(
            job_id, role, store.camera_raw_dir(job_id, role), jobs_root
        )
        role_manifests[role] = manifest
        role_scores[role] = scores

    # --- Combo full video + highlights: per-camera house cuts merged + interleaved so the
    #     cameraman's exit/freefall/canopy/landing are cut in alongside the instructor's. ---
    edls = compose_combo_edls(
        role_manifests, role_scores,
        target_duration=job.target_duration, profile=load_style_profile(jobs_root),
    )
    _persist_clips(jd, ULTIMUM_EDL_FILES["full_video"], edls.full_video)
    _persist_clips(jd, ULTIMUM_EDL_FILES["highlights"], edls.highlights)

    edls = apply_exclusions(edls, exclusions)
    combo_paths = _multicam_scene_paths(role_manifests)
    # Music ducks at the canopy opening; _audio_markers keys off the "canopy" scene (the
    # combo has one when either camera does), with the instructor's detected deploy offset
    # as the fallback when the canopy ride lives inside the freefall clip.
    deploy_offset = _freefall_deploy(role_manifests.get("instructor", {}).get("scenes", []))

    # full_video: cinematic mix (music + original ducked at the canopy opening).
    render_selfie_video(
        jd / "full_video.mp4", edls.full_video, combo_paths,
        booking=booking, music_path=music["full_video"],
        music_only=False, deploy_offset=deploy_offset,
    )
    outputs["full_video"] = str(jd / "full_video.mp4")
    # highlights: music only.
    render_selfie_video(
        jd / "highlights.mp4", edls.highlights, combo_paths,
        booking=booking, music_path=music["highlights"], music_only=True,
    )
    outputs["highlights"] = str(jd / "highlights.mp4")

    # --- Per-camera freefall cuts (selfie freefall logic over the set built above,
    #     music only): external cameraman, then the instructor "chute libre". ---
    for deliverable, role in ULTIMUM_FREEFALL_ROLE.items():
        role_manifest = role_manifests[role]
        clips = _curated_freefall(role_manifest["scenes"], role_scores[role])
        _persist_clips(jd, ULTIMUM_EDL_FILES[deliverable], clips)
        clips = apply_exclusions(
            EDLResponse(full_video=clips, highlights=clips, freefall=clips), exclusions
        ).freefall
        render_selfie_video(
            jd / f"{deliverable}.mp4", clips, _scene_paths(role_manifest),
            booking=booking, music_path=music[deliverable], music_only=True,
        )
        outputs[deliverable] = str(jd / f"{deliverable}.mp4")

    # --- Photos across the whole jump, mined from BOTH cameras' scenes (namespaced so
    #     they don't collide). Backfill guarantees a full ~50 set even when the distant
    #     cameraman footage scores few faces. ---
    photo_manifest, photo_scores = _merge_photo_inputs(role_manifests, role_scores)
    extract_photos(
        job_id, photo_scores, photo_manifest, jobs_root,
        target=SELFIE_PHOTO_TARGET, min_gap=_SELFIE_PHOTO_MIN_GAP_S,
        min_visible=_SELFIE_PHOTO_MIN_VISIBLE, backfill=True,
    )
    outputs["photos"] = str(jd / "photos")

    store.update(job_id, status=JobStatus.ready, outputs=outputs)
    return outputs


def replay_ultimum(
    job_id: str,
    *,
    store: Any,
    jobs_root: str | Path | None = None,
) -> dict[str, str]:
    """Re-render the four Ultimate deliverables from their (hand-edited) EDLs.

    Reloads the persisted ``edl_*.json`` files and the two per-camera scene sets built
    during the first run, re-applies any ``exclude.json`` time-cuts, and re-renders — no
    re-classification or re-scoring. The combo full/highlights render against the combined
    multi-cam scene paths (camera-tagged clips resolve to their own camera's file); the
    two freefall cuts against their per-camera scenes.
    """
    from .jobs import JobStatus

    _require_ffmpeg()
    store.update(job_id, status=JobStatus.processing, error=None)
    jd = job_dir(job_id, jobs_root)
    booking = json.loads(store.booking_path(job_id).read_text())
    music = _ultimum_music_paths(booking, job_id, jobs_root)
    exclusions = load_exclusions(job_id, jobs_root)

    def _clips(filename: str) -> list[Clip]:
        return [Clip.model_validate(c) for c in json.loads((jd / filename).read_text())]

    def _cut(clips: list[Clip]) -> list[Clip]:
        return apply_exclusions(
            EDLResponse(full_video=clips, highlights=clips, freefall=clips), exclusions
        ).freefall

    role_manifests = {
        role: json.loads((jd / _ULTIMUM_ROLE_MANIFEST[role]).read_text())
        for role in CAMERA_ROLES
        if (jd / _ULTIMUM_ROLE_MANIFEST[role]).exists()
    }
    combo_paths = _multicam_scene_paths(role_manifests)
    deploy_offset = _freefall_deploy(role_manifests.get("instructor", {}).get("scenes", []))
    outputs: dict[str, str] = {}

    render_selfie_video(
        jd / "full_video.mp4", _cut(_clips(ULTIMUM_EDL_FILES["full_video"])),
        combo_paths, booking=booking, music_path=music["full_video"],
        music_only=False, deploy_offset=deploy_offset,
    )
    outputs["full_video"] = str(jd / "full_video.mp4")
    render_selfie_video(
        jd / "highlights.mp4", _cut(_clips(ULTIMUM_EDL_FILES["highlights"])),
        combo_paths, booking=booking, music_path=music["highlights"], music_only=True,
    )
    outputs["highlights"] = str(jd / "highlights.mp4")

    for deliverable, role in ULTIMUM_FREEFALL_ROLE.items():
        role_manifest = role_manifests[role]
        render_selfie_video(
            jd / f"{deliverable}.mp4", _cut(_clips(ULTIMUM_EDL_FILES[deliverable])),
            _scene_paths(role_manifest), booking=booking,
            music_path=music[deliverable], music_only=True,
        )
        outputs[deliverable] = str(jd / f"{deliverable}.mp4")

    # Photos were extracted on the first run from the scores; a replay only re-renders
    # the videos, so just re-point the existing set.
    photos = jd / "photos"
    if photos.exists():
        outputs["photos"] = str(photos)

    store.update(job_id, status=JobStatus.ready, outputs=outputs)
    return outputs


def load_selfie_edls(job_id: str, jobs_root: str | Path | None = None) -> EDLResponse:
    """Load the three persisted (possibly hand-edited) ``edl_*.json`` files for a job."""
    jd = job_dir(job_id, jobs_root)

    def _clips(name: str) -> list[Clip]:
        return [Clip.model_validate(c) for c in json.loads((jd / name).read_text())]

    return EDLResponse(
        full_video=_clips("edl_full.json"),
        highlights=_clips("edl_highlights.json"),
        freefall=_clips("edl_freefall.json"),
    )


# --------------------------------------------------------------------------- #
# Offline learning: capture an instructor's finalized edits and reuse the *style*
# (pacing/preferences — never exact timestamps, which don't transfer between jumps).
# --------------------------------------------------------------------------- #

_STYLE_PROFILE_FILE = "style_profile.json"
_EXEMPLARS_DIRNAME = "exemplars"


def _training_dir(jobs_root: str | Path | None) -> Path:
    """Where exemplars + the learned profile live (next to the jobs root)."""
    from edl.storage import jobs_root as resolve_root

    return resolve_root(jobs_root) / ".training"


def capture_exemplar(job_id: str, jobs_root: str | Path | None = None) -> dict[str, Any]:
    """Record a finalized job's editing CHOICES as a training exemplar.

    Stores the per-scene kept seconds (how long the instructor keeps each scene in the
    full video) and the freefall beat count — generalisable style, not timestamps.
    """
    edls = load_selfie_edls(job_id, jobs_root)
    scene_seconds: dict[str, float] = {}
    for c in edls.full_video:
        scene_seconds[c.scene] = scene_seconds.get(c.scene, 0.0) + _clip_out_dur(c)
    exemplar = {
        "job_id": job_id,
        "scene_seconds": {k: round(v, 2) for k, v in scene_seconds.items()},
        "freefall_beats": sum(1 for c in edls.freefall if c.scene == "freefall"),
    }
    out_dir = _training_dir(jobs_root) / _EXEMPLARS_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{job_id}.json").write_text(json.dumps(exemplar, indent=2) + "\n")
    return exemplar


def learn_style_profile(jobs_root: str | Path | None = None) -> dict[str, Any]:
    """Aggregate every exemplar into a style profile and persist it.

    The profile is averaged across exemplars (more finalized jobs → a steadier style).
    """
    ex_dir = _training_dir(jobs_root) / _EXEMPLARS_DIRNAME
    exemplars = (
        [json.loads(p.read_text()) for p in sorted(ex_dir.glob("*.json"))]
        if ex_dir.exists()
        else []
    )
    if not exemplars:
        return {}

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    beats: list[int] = []
    for e in exemplars:
        for scene, sec in e.get("scene_seconds", {}).items():
            sums[scene] = sums.get(scene, 0.0) + float(sec)
            counts[scene] = counts.get(scene, 0) + 1
        if e.get("freefall_beats"):
            beats.append(int(e["freefall_beats"]))

    profile: dict[str, Any] = {
        "samples": len(exemplars),
        "scene_seconds": {s: round(sums[s] / counts[s], 2) for s in sums},
        "freefall_beats": round(statistics.median(beats)) if beats else None,
    }
    _training_dir(jobs_root).mkdir(parents=True, exist_ok=True)
    (_training_dir(jobs_root) / _STYLE_PROFILE_FILE).write_text(
        json.dumps(profile, indent=2) + "\n"
    )
    return profile


def load_style_profile(jobs_root: str | Path | None = None) -> dict[str, Any]:
    """The learned style profile, or ``{}`` if nothing has been learned yet."""
    path = _training_dir(jobs_root) / _STYLE_PROFILE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def load_exclusions(
    job_id: str, jobs_root: str | Path | None = None
) -> dict[str, list[tuple[float, float]]]:
    """Time ranges (seconds) to cut from each scene, from the job's ``exclude.json``.

    Format ``{"<scene>": [[start, end], ...]}``; absent/empty file → no exclusions.
    """
    path = job_dir(job_id, jobs_root) / EXCLUDE_FILENAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    out: dict[str, list[tuple[float, float]]] = {}
    for scene, ranges in data.items():
        spans = [(float(a), float(b)) for a, b in ranges if float(b) > float(a)]
        if spans:
            out[scene] = spans
    return out


def _subtract_ranges(
    start: float, end: float, ranges: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The sub-intervals of ``[start, end)`` that remain after removing ``ranges``."""
    segments = [(start, end)]
    for r0, r1 in ranges:
        nxt: list[tuple[float, float]] = []
        for a, b in segments:
            if r1 <= a or r0 >= b:  # no overlap — keep as is
                nxt.append((a, b))
                continue
            if a < r0:
                nxt.append((a, r0))
            if r1 < b:
                nxt.append((r1, b))
        segments = nxt
    return [(a, b) for a, b in segments if b - a > 0.05]


def apply_exclusions(
    edls: EDLResponse, exclusions: dict[str, list[tuple[float, float]]]
) -> EDLResponse:
    """Cut the excluded time ranges out of every deliverable's clips.

    A clip overlapping an excluded range is split around it (its speed is preserved);
    fully-excluded clips are dropped. A deliverable never empties (the EDL needs at
    least one clip), so if exclusions would remove everything the originals are kept.
    """
    if not exclusions:
        return edls

    def _filter(clips: list[Clip]) -> list[Clip]:
        out: list[Clip] = []
        for c in clips:
            ranges = exclusions.get(c.scene)
            if not ranges:
                out.append(c)
                continue
            for a, b in _subtract_ranges(c.src_start, c.src_end, ranges):
                out.append(
                    Clip(scene=c.scene, src_start=a, src_end=b,
                         speed_multiplier=c.speed_multiplier)
                )
        return out or clips

    return EDLResponse(
        full_video=_filter(edls.full_video),
        highlights=_filter(edls.highlights),
        freefall=_filter(edls.freefall),
    )


def replay_selfie(
    job_id: str,
    *,
    store: Any,
    jobs_root: str | Path | None = None,
) -> dict[str, str]:
    """Re-render the three videos from the job's (hand-edited) ``edl_*.json`` files.

    The instructor edits the timestamps / speeds in ``edl_full.json`` /
    ``edl_highlights.json`` / ``edl_freefall.json`` (e.g. to set exactly where the
    exit starts) and this re-runs only the render step against the existing scenes —
    no re-classification or re-scoring. Photos are left as they are. A photo-only job
    has no videos to re-render, so this only re-points its existing photo set.
    """
    from .jobs import JobStatus

    _require_ffmpeg()
    store.update(job_id, status=JobStatus.processing, error=None)
    jd = job_dir(job_id, jobs_root)
    booking = json.loads(store.booking_path(job_id).read_text())
    package = store.load(job_id).package

    outputs: dict[str, str] = {}
    if package.makes_videos:
        manifest = json.loads((jd / "scene_manifest.json").read_text())
        edls = load_selfie_edls(job_id, jobs_root)
        edls = apply_exclusions(edls, load_exclusions(job_id, jobs_root))
        outputs = render_outputs(
            job_id, edls, manifest, booking, jobs_root,
            music_paths=_music_paths(booking, job_id, jobs_root),
        )
    photos = jd / "photos"
    if photos.exists():
        outputs["photos"] = str(photos)
    store.update(job_id, status=JobStatus.ready, outputs=outputs)
    return outputs


def _resolve_music(music: str | None) -> str | None:
    """Resolve a booking's music name to a backing-track path under /templates."""
    if not music:
        return None
    try:
        from render.templates import resolve_music

        resolved = resolve_music(music)
        return str(resolved) if resolved else None
    except Exception as e:  # noqa: BLE001 - music is optional; never fail the render on it
        logger.warning("could not resolve music %r: %r", music, e)
        return None
