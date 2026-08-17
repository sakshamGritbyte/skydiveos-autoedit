"""Poster frames: every gallery video card shows a real moment from that video.

A ``<video>`` with no ``poster`` is drawn by the browser, not by us — a grey box, a
play glyph, or (on iOS) the generic cloud/placeholder tile. Five cards of that on one
page is the same page for every customer, and it is the *upsell* surface: a locked
card has to sell the edit behind it before anyone will press play. So each card gets a
still lifted **out of that deliverable**, chosen for what it shows.

Three rules shape the whole module:

* **The entitlement, never the URL, picks the source** — exactly as in
  :func:`api.app.public_media`. A locked deliverable's poster is cut from its
  *watermarked* ``preview_<name>.mp4``, so a paywalled card can never leak a clean
  frame of the unbought edit, and the still it shows carries the same mark as the
  video it teases. Callers hand us a path; :mod:`api.app` decides which one.
* **A poster is decoration, so it never fails anything.** Every entry point returns
  ``None`` (or logs and moves on) rather than raising: no ffmpeg, no Pillow, an
  all-black video — the card falls back to the browser's own placeholder, which is
  precisely where it was before this module existed.
* **Cached on disk, outside ``photos/``.** Posters live in ``<job_dir>/posters/`` and
  are never recorded in ``Job.outputs`` — the same reasoning as
  :mod:`api.preview`'s ``preview_photos/``: the paid photo zip, the per-photo S3
  uploads and the archive mirror must not pick them up.

Selection is a two-pass sweep: one FFmpeg pass dumps ~24 small candidate frames
across the *middle* of the video (the intro/outro title cards are skipped by
construction), each is measured for sharpness/exposure/colour and — best-effort —
scored for faces with the same MediaPipe scorer the pipeline already uses, then the
winner is re-extracted at full resolution and cropped to the card's 16:9. The
ranking function (:func:`frame_score` / :func:`select_frame`) is **pure**, so the
"which frame sells this video" decision is testable without a single byte of video.
"""

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Settings

logger = logging.getLogger(__name__)

#: Where a job's poster frames live (a sibling of ``photos/``, never inside it).
POSTER_DIRNAME = "posters"
#: Poster geometry — the card is ``aspect-ratio:16/9`` and at most ~550 px wide, so
#: 960x540 is crisp on a retina phone and still a ~60 kB JPEG.
POSTER_W, POSTER_H = 960, 540
#: FFmpeg ``-q:v`` for the poster (2 = best, 31 = worst).
_POSTER_Q = 3
#: How far down the scaled frame the 16:9 crop window sits when the source is taller
#: than 16:9. Above centre, because faces and the horizon live in the upper half —
#: a centred crop on a 4:3 frame is what lops off the top of somebody's head.
#: (Rendered deliverables are already 1920x1080, so this only bites on odd sources.)
_CROP_Y_BIAS = 0.4

#: How many frames are ranked. Enough to find the moment in a 40-second highlights
#: cut, cheap enough that a 4-minute full video still costs one decode pass.
CANDIDATE_COUNT = 24
#: Candidate frames are downscaled — we are ranking them, not delivering them.
_CANDIDATE_W = 640
#: Head/tail of a deliverable skipped when sampling. The renderer opens on a title
#: card and closes on the logo card (``api.selfie._CARD_SECONDS`` = 2.5 plus its
#: fade), and those frames are exactly the "black frame / transition" a poster must
#: never be. Skipping them by construction beats detecting them afterwards.
_TITLE_CARD_S = 3.0
#: Below this the trimmed window is not worth trimming (a very short deliverable).
_MIN_WINDOW_S = 4.0

#: A frame this dark is a fade, a title card or a lens-covered moment; this bright is
#: a blown-out sky/flash. Neither says anything about the jump, so both are rejected
#: outright rather than merely scored down.
_MIN_BRIGHTNESS = 0.06
_MAX_BRIGHTNESS = 0.97

_FFMPEG_TIMEOUT_S = 300.0

#: Injectable command runner (tests fake it; default runs FFmpeg) — the same seam
#: shape as :data:`api.preview.Runner`.
Runner = Callable[[list[str]], None]


class ThumbnailError(RuntimeError):
    """Raised inside this module when a poster cannot be produced.

    Never escapes :func:`ensure_poster` / :func:`render_job_posters`: a missing poster
    is a card that looks like it always did, not a broken job.
    """


# --------------------------------------------------------------------------- #
# What a candidate frame is, and how much we like it (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FrameStats:
    """One candidate frame's measurements. ``ts`` is seconds into the video.

    The four face signals are :mod:`analysis.score`'s, in the same 0..1 vocabulary the
    photo picker already ranks stills with — and, as there, a frame with **no detected
    face scores 0 on all four** rather than being disqualified. That is what keeps a
    distant camera-flyer cut (where MediaPipe rarely locks onto the tandem) working:
    the image-quality terms simply decide it, exactly like ``extract_photos``'s
    ``backfill`` mode.
    """

    ts: float
    #: Variance of the Laplacian — high is crisp, near-zero is motion blur or a fade.
    sharpness: float
    #: 1.0 for a mid-bright frame, falling off when over/under exposed.
    exposure: float
    #: Mean luma 0..1 (the black/blown-out gate).
    brightness: float
    #: Mean saturation 0..1 — blue sky, canopy, jumpsuits. A grey transition has none.
    colour: float
    smile: float = 0.0
    eye_contact: float = 0.0
    face_in_frame: float = 0.0
    face_centered: float = 0.0


@dataclass(frozen=True)
class Profile:
    """How one *kind* of deliverable wants its poster chosen.

    The weights sum to 1 and are read straight off the brief: a Freefall cut should
    open on the action, a Highlights card on the peak moment, a Full Video on
    something that says "skydive" rather than on the boarding chat it opens with.

    ``centre``/``spread`` are a positional prior over the trimmed window (0 = first
    sampled frame, 1 = last): where in *this* kind of edit the good moment usually
    lives. It is deliberately weak — it breaks ties between similar frames, it does
    not overrule a clearly better one.
    """

    face: float
    quality: float
    colour: float
    position: float
    centre: float
    spread: float


#: The default: no idea what this video is, so judge it purely on content with a mild
#: pull towards the middle (the ends of any edit are the least representative).
DEFAULT_PROFILE = Profile(
    face=0.42, quality=0.33, colour=0.10, position=0.15, centre=0.50, spread=0.45
)

#: Per-deliverable overrides, keyed by the deliverable's *base* name — the
#: ``<role>_`` namespacing a mixed/Ultimate job adds is stripped by
#: :func:`profile_for`, so ``external_highlights`` is judged as a highlights cut.
PROFILES: dict[str, Profile] = {
    # The whole clip IS the action, so the prior sits mid-cut (belly-to-earth, well
    # after the exit and before the deployment beat) and faces lead: on a handcam
    # freefall the customer's grin is the product.
    "freefall": Profile(
        face=0.48, quality=0.30, colour=0.09, position=0.13, centre=0.45, spread=0.40
    ),
    # The peak moment of a highlights cut: strongest content wins, with the prior just
    # past the intro beat where the freefall slow-mo lands.
    "highlights": Profile(
        face=0.45, quality=0.30, colour=0.10, position=0.15, centre=0.42, spread=0.38
    ),
    # A full edit opens on boarding and closes under canopy; the jump is past the
    # middle. Pull there, and lean a little harder on colour so the card shows sky
    # rather than the inside of a plane.
    "full_video": Profile(
        face=0.40, quality=0.30, colour=0.12, position=0.18, centre=0.55, spread=0.40
    ),
}
#: Deliverables that are a freefall cut under another name (the Ultimate product's
#: per-camera cuts). Same brief, same profile.
_FREEFALL_ALIASES = ("external_freefall", "chute_libre_selfie")
for _alias in _FREEFALL_ALIASES:
    PROFILES[_alias] = PROFILES["freefall"]

#: Namespaces :func:`api.jobs.deliverable_name` prepends on a mixed job. Stripped for
#: profile lookup only — never for anything that identifies the file.
_ROLE_PREFIXES = ("instructor_", "external_")


def profile_for(name: str) -> Profile:
    """The selection profile for deliverable ``name`` (never raises, never ``None``).

    ``external_freefall`` and ``chute_libre_selfie`` are freefall cuts by another
    name; ``instructor_highlights`` is a highlights cut wearing a mixed job's role
    namespace. Anything unrecognised — including the classic pipeline's ``final`` —
    gets :data:`DEFAULT_PROFILE`, which is a perfectly good "show me the best moment".
    """
    if name in PROFILES:
        return PROFILES[name]
    for prefix in _ROLE_PREFIXES:
        if name.startswith(prefix):
            return PROFILES.get(name[len(prefix):], DEFAULT_PROFILE)
    return DEFAULT_PROFILE


def _face_component(f: FrameStats) -> float:
    """The face half of the score, in :func:`api.selfie._photo_score`'s proportions.

    Smile leads (the emotional moment the brief asks for), then camera-facing gaze,
    then "is the face actually, wholly in shot" — which is what keeps a frame with the
    customer half out of frame off the card.
    """
    return (
        0.45 * f.smile
        + 0.20 * f.eye_contact
        + 0.25 * f.face_in_frame
        + 0.10 * f.face_centered
    )


def frame_score(
    f: FrameStats, *, sharp_norm: float, position: float, profile: Profile
) -> float:
    """Composite 0..1 desirability of one candidate. Pure.

    ``sharp_norm`` is this frame's sharpness normalised across the candidate set (a
    Laplacian variance is only meaningful relative to its neighbours — a hazy sky and
    a busy cockpit are different scales), and ``position`` its 0..1 place in the
    sampled window.
    """
    quality = 0.75 * sharp_norm + 0.25 * f.exposure
    prior = math.exp(-(((position - profile.centre) / profile.spread) ** 2))
    return (
        profile.face * _face_component(f)
        + profile.quality * quality
        + profile.colour * f.colour
        + profile.position * prior
    )


def _usable(f: FrameStats) -> bool:
    """Whether a frame is a candidate at all (the black/blown-out gate)."""
    return _MIN_BRIGHTNESS <= f.brightness <= _MAX_BRIGHTNESS


def select_frame(
    frames: Sequence[FrameStats], profile: Profile = DEFAULT_PROFILE
) -> FrameStats | None:
    """Pick the frame that best represents this video, or ``None`` if none does. Pure.

    ``None`` is a real answer, not a failure: a deliverable whose every sampled frame
    is black or blown out has nothing worth putting on a card, and the caller falls
    back to the browser's placeholder rather than promising a moment that isn't there.
    """
    usable = [f for f in frames if _usable(f)]
    if not usable:
        return None
    sharps = [f.sharpness for f in usable]
    lo, hi = min(sharps), max(sharps)

    def sharp_norm(s: float) -> float:
        return (s - lo) / (hi - lo) if hi > lo else 0.5

    times = [f.ts for f in usable]
    first, last = min(times), max(times)
    span = last - first

    def position(ts: float) -> float:
        return (ts - first) / span if span > 0 else 0.5

    return max(
        usable,
        key=lambda f: (
            frame_score(
                f, sharp_norm=sharp_norm(f.sharpness), position=position(f.ts),
                profile=profile,
            ),
            # Deterministic tie-break, so the same video always yields the same poster
            # (a poster that moves between renders looks like a bug to the operator).
            -f.ts,
        ),
    )


@dataclass(frozen=True)
class SamplePlan:
    """Where to sample a video for candidates: ``start`` seconds in, at ``fps``."""

    start: float
    fps: float
    times: tuple[float, ...]


def candidate_plan(duration: float, *, count: int = CANDIDATE_COUNT) -> SamplePlan:
    """Evenly spaced sample times across the video's *middle*. Pure.

    The head and tail are dropped (``_TITLE_CARD_S``) because the renderer puts a
    title card at one end and the logo card at the other — the two places a poster
    must never come from. A video too short to trim is sampled whole rather than not
    at all.

    Expressed as an ``fps`` over a trimmed window rather than a list of seeks: that is
    one decode pass for the whole set (and the exact convention
    :func:`api.selfie.dump_scene_jpegs` already dumps frames by, so the k-th file's
    timestamp is derived the same way in both places).
    """
    if duration <= 0 or count < 1:
        return SamplePlan(0.0, 0.0, ())
    start, end = 0.0, duration
    if duration > 2 * _TITLE_CARD_S + _MIN_WINDOW_S:
        start, end = _TITLE_CARD_S, duration - _TITLE_CARD_S
    window = end - start
    fps = count / window
    times = tuple(round(start + i / fps, 3) for i in range(count))
    return SamplePlan(round(start, 3), fps, times)


# --------------------------------------------------------------------------- #
# Measuring real frames (I/O)
# --------------------------------------------------------------------------- #


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run FFmpeg/ffprobe, surfacing its stderr (not a bare exit code) on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S)
    except FileNotFoundError as e:  # no ffmpeg on this box — posters are optional
        raise ThumbnailError(f"{cmd[0]} not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise ThumbnailError(f"{cmd[0]} timed out after {_FFMPEG_TIMEOUT_S}s") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:] or "(no stderr)"
        raise ThumbnailError(f"{cmd[0]} failed (exit {proc.returncode}): {tail}")


def dump_candidates(
    src: Path, out_dir: Path, plan: SamplePlan, *, runner: Runner | None = None
) -> list[tuple[float, Path]]:
    """Dump the plan's candidate JPEGs, downscaled to rank on. Seam; faked in tests.

    One FFmpeg invocation for the whole set — input-seek to the window, ``fps`` across
    it — so a 4-minute deliverable is decoded once instead of seeked into 24 times.
    Returns ``(ts, jpeg)`` pairs for whatever actually landed: FFmpeg can emit fewer
    files than asked for (a short or truncated tail), so the pairing is by index over
    the sorted output and never assumes the count.
    """
    if not plan.times or plan.fps <= 0:
        return []
    run = runner or _run_ffmpeg
    out_dir.mkdir(parents=True, exist_ok=True)
    window = len(plan.times) / plan.fps
    run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{plan.start:.3f}", "-i", str(src), "-t", f"{window:.3f}",
            "-vf", f"fps={plan.fps:.6f},scale={_CANDIDATE_W}:-2",
            "-q:v", "5",
            str(out_dir / "cand_%03d.jpg"),
        ]
    )
    frames = sorted(out_dir.glob("cand_*.jpg"))
    return [(plan.times[i], p) for i, p in enumerate(frames) if i < len(plan.times)]


def measure_frame(jpeg: Path) -> tuple[float, float, float, float]:
    """``(sharpness, exposure, brightness, colour)`` for one candidate JPEG.

    Sharpness and exposure come from :func:`api.selfie.frame_quality` — the photo
    picker's definitions, imported rather than re-derived so "is this frame sharp
    enough to show a customer" has exactly one answer in this codebase. Colour
    (mean saturation) is measured here: it separates a real scene from the grey of a
    dissolve, and it is not a question the photo picker ever had to ask.
    """
    import numpy as np
    from PIL import Image

    # Lazy: api.selfie pulls the pipeline's stack (pydantic, analysis, edl) and this
    # module is imported by the web process on every gallery request.
    from .selfie import frame_quality  # noqa: PLC0415

    sharpness, exposure = frame_quality(jpeg)
    with Image.open(jpeg) as raw:
        im = raw.convert("RGB")
    im.thumbnail((160, 160))
    a = np.asarray(im, dtype="float32") / 255.0
    if a.size == 0:
        return sharpness, exposure, 0.0, 0.0
    brightness = float(a.mean())
    colour = float((a.max(axis=2) - a.min(axis=2)).mean())
    return sharpness, exposure, brightness, colour


def score_faces(frames: Sequence[tuple[float, Path]]) -> dict[float, dict[str, float]]:
    """Face signals per candidate, keyed by ``ts``. Best-effort: ``{}`` on any failure.

    Reuses the pipeline's :class:`analysis.score.FreefallScorer` (MediaPipe
    FaceLandmarker), so "is the customer smiling at the lens" is scored by the same
    code that scored the jump. Failure is *normal* here and must stay cheap: MediaPipe
    may be absent on a web-only box. Without it every frame keeps its zeroed face
    signals and the image-quality terms decide the poster — the same graceful
    degradation ``extract_photos``'s ``backfill`` mode relies on.

    The landmarker bundle is used only if it is **already on disk**
    (:func:`analysis.models.cached_model`): this can run inside a customer's page
    request, and :func:`analysis.models.resolve_model` would download 30 MB on a cold
    box — a stalled gallery is a far worse trade than a poster picked on sharpness.

    Each frame is handed to the scorer under its *index* as the timestamp: the scorer
    averages per whole second, and two candidates from the same second of a short
    video would otherwise collapse into one row.
    """
    if not frames:
        return {}
    try:
        import numpy as np
        from PIL import Image

        from analysis.models import cached_model
        from analysis.score import FreefallScorer

        model = cached_model()
        if model is None:
            logger.debug("poster: no local FaceLandmarker bundle; ranking on image quality")
            return {}

        def _images() -> Any:
            for i, (_ts, p) in enumerate(frames):
                with Image.open(p) as raw:
                    yield float(i), np.asarray(raw.convert("RGB"))

        with FreefallScorer(model) as scorer:
            rows = scorer.score_frames(_images())
    except Exception:  # noqa: BLE001 - faces are an enhancement, never a requirement
        logger.debug("poster face scoring unavailable; ranking on image quality alone",
                     exc_info=True)
        return {}

    out: dict[float, dict[str, float]] = {}
    for row in rows:
        index = int(row.get("ts", -1))
        if 0 <= index < len(frames):
            out[frames[index][0]] = row
    return out


def poster_path(source: Path) -> Path:
    """Where ``source``'s poster is cached: ``<job_dir>/posters/<source stem>.jpg``.

    Keyed on the *source file's* stem, so a locked deliverable's watermarked poster
    (``posters/preview_full_video.jpg``) and its clean one (``posters/full_video.jpg``)
    are different files. Unlock then flips the card to the clean still with no
    regeneration — and on a load master, every child gallery shares the one poster its
    lock state points at.
    """
    return source.parent / POSTER_DIRNAME / f"{source.stem}.jpg"


def _extract_poster(src: Path, ts: float, out: Path, *, runner: Runner | None = None) -> None:
    """Cut the chosen frame at full resolution and fit it to the card's 16:9.

    ``scale=…:force_original_aspect_ratio=increase`` then ``crop`` covers the frame
    rather than letterboxing it (a poster with black bars inside a card that already
    has black bars looks broken), with the crop window biased above centre so a taller
    source loses ground, not heads.
    """
    run = runner or _run_ffmpeg
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={POSTER_W}:{POSTER_H}:force_original_aspect_ratio=increase,"
        f"crop={POSTER_W}:{POSTER_H}:(iw-ow)/2:(ih-oh)*{_CROP_Y_BIAS}"
    )
    # Write-then-replace: FastAPI serves these from a thread pool, so two requests for
    # the same card must never read a half-written JPEG.
    part = out.with_name(f".{uuid.uuid4().hex}.part.jpg")
    try:
        run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{ts:.3f}", "-i", str(src),
                "-frames:v", "1", "-vf", vf, "-q:v", str(_POSTER_Q),
                str(part),
            ]
        )
        if not part.exists():
            raise ThumbnailError(f"poster extraction produced no file for {src.name}")
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)


def build_poster(
    source: Path, *, deliverable: str | None = None, runner: Runner | None = None
) -> Path | None:
    """Choose and write ``source``'s poster, ignoring any cached one. ``None`` on failure.

    ``deliverable`` selects the profile (:func:`profile_for`); omitted, it is derived
    from the filename, with a ``preview_`` prefix stripped — a locked Freefall cut is
    still a Freefall cut.
    """
    # Lazy, and the pipeline's own ffprobe seam rather than a second copy of it.
    from .selfie import probe_duration  # noqa: PLC0415

    name = deliverable or source.stem
    if name.startswith("preview_"):
        name = name[len("preview_"):]
    duration = probe_duration(source)
    if duration <= 0:
        logger.debug("poster: unreadable duration for %s", source)
        return None
    out = poster_path(source)
    with tempfile.TemporaryDirectory(prefix="poster-") as tmp:
        frames = dump_candidates(source, Path(tmp), candidate_plan(duration), runner=runner)
        if not frames:
            return None
        faces = score_faces(frames)
        stats: list[FrameStats] = []
        for ts, jpg in frames:
            sharpness, exposure, brightness, colour = measure_frame(jpg)
            f = FrameStats(
                ts=ts, sharpness=sharpness, exposure=exposure,
                brightness=brightness, colour=colour,
            )
            row = faces.get(ts)
            if row:
                f = replace(
                    f,
                    smile=row.get("smile", 0.0),
                    eye_contact=row.get("eye_contact", 0.0),
                    face_in_frame=row.get("face_in_frame", 0.0),
                    face_centered=row.get("face_centered", 0.0),
                )
            stats.append(f)
        best = select_frame(stats, profile_for(name))
        if best is None:
            logger.info("poster: no usable frame in %s (all black/blown out)", source.name)
            return None
        _extract_poster(source, best.ts, out, runner=runner)
    logger.info(
        "poster for %s from t=%.2fs (%d candidates, faces=%s)",
        source.name, best.ts, len(stats), "yes" if faces else "no",
    )
    return out


def ensure_poster(
    source: Path, *, runner: Runner | None = None, settings: Settings | None = None
) -> Path | None:
    """Return ``source``'s poster, building it on first request. **Never raises.**

    Lazy and disk-cached (rebuilt when the source is newer), for the same three
    reasons :func:`api.preview.ensure_photo_preview` is: jobs rendered before this
    feature still get posters, unlock stays a one-field state change, and a page with
    five cards pays the FFmpeg pass once per card ever.

    ``None`` means "no poster" — the card renders exactly as it did before this
    module existed. That is the whole fallback story, and it is why nothing here is
    allowed to throw.
    """
    if settings is not None and not settings.gallery_thumbnails:
        return None
    try:
        if not source.is_file():
            return None
        out = poster_path(source)
        if out.is_file() and out.stat().st_mtime >= source.stat().st_mtime:
            return out
        return build_poster(source, runner=runner)
    except Exception:  # noqa: BLE001 - a decorative still must not 500 the gallery
        logger.warning("poster generation failed for %s", source, exc_info=True)
        return None


def render_job_posters(job: Any, store: Any, settings: Settings) -> dict[str, str]:
    """Pre-build posters for every video deliverable of a finished job. Never raises.

    Called at each "render finished" seam beside the archive mirror, so the customer's
    first page load is already warm — the lazy path in :func:`ensure_poster` then only
    catches jobs that predate this, and anything this pass couldn't do.

    Builds the poster for the file the customer would actually be served: the clean
    master for an owned deliverable, the watermarked preview for a locked one (asked
    per deliverable — a mixed job has both). Returns ``{name: poster path}``, purely
    informational; posters are always found again by their path convention.
    """
    from .jobs import Entitlement, deliverable_names, entitlement_for  # noqa: PLC0415
    from .preview import preview_path  # noqa: PLC0415

    if not settings.gallery_thumbnails:
        return {}
    built: dict[str, str] = {}
    try:
        job_dir = store.dir(job.job_id)
        for name in deliverable_names(job):
            locked = entitlement_for(job, name) is Entitlement.preview_only
            source = preview_path(job_dir, name) if locked else job_dir / f"{name}.mp4"
            out = ensure_poster(source, settings=settings)
            if out is not None:
                built[name] = str(out)
    except Exception:  # noqa: BLE001 - decoration, at a seam that must not fail a job
        logger.warning("poster pre-render failed for job %s", getattr(job, "job_id", "?"),
                       exc_info=True)
    return built
