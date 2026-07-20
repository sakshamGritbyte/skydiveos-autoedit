"""Deterministic post-validation + repair for scene-pipeline EDLs.

The compose step (Claude, or the offline house cut) is untrusted: on real jobs it
has stopped freefall cuts 15-35 s before the deployment, let "canopy" clips bleed
landing footage into freefall-only deliverables, skipped the plane-entry moment,
dropped the highlights intro, and produced duplicated / ping-ponging multi-cam
interleaves. Instead of prompt-tweaking, :func:`validate_and_repair` enforces the
mandatory story milestones and shot-quality rules in code, immediately after each
compose result and before the EDLs are persisted.

Design constraints (load-bearing — see tests/test_edl_validate.py):

* **Pure**: no I/O, no imports from ``api.*`` (``api.selfie`` imports this module;
  the reverse would be circular). Clips are the plain dicts persisted to
  ``edl_*.json``: ``{"scene", "src_start", "src_end", "speed_multiplier", "camera"}``.
  Inputs are never mutated; changed clips are copies.
* **Deterministic**: every repair is a pure function of the input order and values
  (jobs are idempotent and resumable), and the repair log is human-readable so it
  can be surfaced verbatim in ``validation_report.json``.
* Scene ordering comes from the manifest's ``scenes`` list (written in jump order
  by ``build_scenes``) — never from a duplicated scene-order constant.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

ClipDict = dict[str, Any]

#: Deliverables that must contain ONLY the freefall scene, clamped to the aerial
#: window. Names match the persist sites: the single-camera ``freefall`` cut and
#: the two per-camera Ultimate cuts (``ULTIMUM_FREEFALL_ROLE`` keys in api.selfie).
FREEFALL_DELIVERABLES: frozenset[str] = frozenset(
    {"freefall", "external_freefall", "chute_libre_selfie"}
)
#: Deliverables that must carry the boarding entry (and, for highlights, the intro).
_STORY_DELIVERABLES: frozenset[str] = frozenset({"full_video", "highlights"})

_FREEFALL_SCENE = "freefall"
_BOARDING_SCENE = "boarding"
_INTRO_SCENE = "intro_interview"
_SLOWMO = 0.4

#: The door/exit-prep allowance kept before the detected exit in freefall cuts.
_EXIT_LEAD_S = 8.0
#: Opening-shock allowance kept after the detected deploy in freefall cuts.
_DEPLOY_TAIL_S = 3.0
#: The injected deployment beat spans [D - 1, D + 2] at 0.4x.
_DEPLOY_BEAT_PRE_S = 1.0
_DEPLOY_BEAT_POST_S = 2.0
#: Minimum on-screen (output) shot length for multi-cam combos; 0.4x beats exempt.
_MIN_SHOT_OUT_S = 1.5
#: Minimum output seconds between camera switches in a multi-cam combo.
_SWITCH_SPACING_OUT_S = 3.0
#: Max |anchored-time| jump allowed across a camera switch (exit_offset anchored).
_ANCHOR_TOL_S = 4.0
#: Same-(camera, scene, speed) clips overlapping a kept clip by more than this
#: fraction (of the shorter clip) are duplicates.
_DUP_OVERLAP_FRAC = 0.5
#: Older manifests without file_offsets: a boarding clip must start this early ...
_BOARD_HEAD_FALLBACK_S = 5.0
#: ... and boarding clips must total at least this much source coverage.
_BOARD_MIN_COVERAGE_S = 6.0
#: Length of an injected boarding beat.
_BOARD_BEAT_S = 3.0
#: highlights: the intro clip must run at least this long.
_INTRO_MIN_S = 3.0
#: Clips shorter than this (source seconds) are dropped after clamping.
_MIN_CLIP_S = 0.05
#: Contiguity tolerance for merging adjacent fragments.
_EPS = 1e-6


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _scene_by_name(manifest: dict[str, Any], name: str) -> dict[str, Any] | None:
    for scene in manifest.get("scenes", []):
        if scene.get("name") == name:
            return scene
    return None


def _manifest_for(
    clip: ClipDict,
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """The manifest a clip's timestamps live in: its camera's, else the shared one."""
    camera = clip.get("camera")
    if camera and manifest_by_camera and camera in manifest_by_camera:
        return manifest_by_camera[camera]
    return manifest


def _ff_anchors(manifest: dict[str, Any]) -> tuple[float, float, float] | None:
    """The freefall scene's ``(exit_offset, deploy_offset, duration)``, or ``None``.

    ``None`` (no freefall scene, or offsets undetected) disables the freefall-window
    rules entirely: without anchors there is no window to clamp to, and stripping
    the anchor-less fallback cuts would empty them.
    """
    ff = _scene_by_name(manifest, _FREEFALL_SCENE)
    if ff is None or ff.get("exit_offset") is None or ff.get("deploy_offset") is None:
        return None
    return (
        float(ff["exit_offset"]),
        float(ff["deploy_offset"]),
        max(float(ff.get("duration", 0.0)), 0.1),
    )


def _rank_map(
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
) -> dict[str, int]:
    """Scene name -> jump-order rank, from the manifests' (already ordered) scenes.

    Each manifest is internally jump-ordered, but a per-camera manifest can be
    incomplete (an Ultimate instructor cam may only have ``freefall``/``canopy``), so
    the most COMPLETE manifest sets the ordering and shorter ones only contribute
    scenes it lacks — otherwise an incomplete primary would rank ``freefall`` before
    ``intro_interview``. Ties break on a stable source index for determinism.
    """
    by_camera = manifest_by_camera or {}
    sources = [manifest] + [by_camera[cam] for cam in sorted(by_camera)]
    ordered_sources = sorted(
        enumerate(sources), key=lambda pair: (-len(pair[1].get("scenes", [])), pair[0])
    )
    names: list[str] = []
    for _, src in ordered_sources:
        for scene in src.get("scenes", []):
            if scene.get("name") not in names:
                names.append(scene["name"])
    return {name: i for i, name in enumerate(names)}


def _rank(clip: ClipDict, ranks: dict[str, int]) -> int:
    return ranks.get(clip["scene"], len(ranks))


def _src_len(clip: ClipDict) -> float:
    return float(clip["src_end"]) - float(clip["src_start"])


def _out_dur(clip: ClipDict) -> float:
    return _src_len(clip) / float(clip.get("speed_multiplier", 1.0) or 1.0)


def _is_slowmo(clip: ClipDict) -> bool:
    return abs(float(clip.get("speed_multiplier", 1.0)) - _SLOWMO) < _EPS


def _overlap_frac(a: ClipDict, b: ClipDict) -> float:
    """Overlap seconds divided by the SHORTER clip (a short clip fully inside a
    long one is a duplicate regardless of order)."""
    overlap = min(float(a["src_end"]), float(b["src_end"])) - max(
        float(a["src_start"]), float(b["src_start"])
    )
    shorter = min(_src_len(a), _src_len(b))
    if overlap <= 0.0 or shorter <= 0.0:
        return 0.0
    return overlap / shorter


def _group_key(clip: ClipDict) -> tuple[str | None, str]:
    return (clip.get("camera"), clip["scene"])


def _covers(
    clips: Sequence[ClipDict], scene: str, lo: float, hi: float, camera: str | None = None
) -> bool:
    """True when some ``scene`` clip (optionally camera-scoped) overlaps ``[lo, hi)``."""
    return any(
        c["scene"] == scene
        and (camera is None or c.get("camera") == camera)
        and float(c["src_start"]) < hi
        and float(c["src_end"]) > lo
        for c in clips
    )


def _fmt(clip: ClipDict) -> str:
    """``external/freefall [42.50, 44.00] @0.4`` (camera prefix only when tagged)."""
    prefix = f"{clip['camera']}/" if clip.get("camera") else ""
    speed = float(clip.get("speed_multiplier", 1.0))
    suffix = f" @{speed:g}" if speed != 1.0 else ""
    return (
        f"{prefix}{clip['scene']} "
        f"[{float(clip['src_start']):.2f}, {float(clip['src_end']):.2f}]{suffix}"
    )


def _insert_chronological(
    clips: list[ClipDict], new: ClipDict, ranks: dict[str, int]
) -> list[ClipDict]:
    """Insert ``new`` at its chronological position without re-sorting the list.

    Placed before the first clip of a later scene rank; within its own
    (camera, scene) group, after the last clip whose ``src_start`` is <= its own —
    so injections never disturb an interleave the multicam rules built.
    """
    new_rank = _rank(new, ranks)
    insert_at = len(clips)
    for i, c in enumerate(clips):
        if _rank(c, ranks) > new_rank:
            insert_at = i
            break
        if (
            _group_key(c) == _group_key(new)
            and float(c["src_start"]) > float(new["src_start"])
        ):
            insert_at = i
            break
    return [*clips[:insert_at], new, *clips[insert_at:]]


# --------------------------------------------------------------------------- #
# Rules — each takes and returns (clips, log-lines); none mutates its input.
# --------------------------------------------------------------------------- #


def _drop_non_freefall(clips: list[ClipDict]) -> tuple[list[ClipDict], list[str]]:
    """Freefall-type cuts contain the jump and nothing else. The "canopy" scene is
    NOT a safe deployment source — on real jobs it holds landing footage (positional
    misclassification); the deployment beat comes from the freefall scene instead."""
    kept, log = [], []
    for c in clips:
        if c["scene"] == _FREEFALL_SCENE:
            kept.append(c)
        else:
            log.append(f"dropped {_fmt(c)} — non-freefall scene in a freefall-only cut")
    return kept, log


def _clamp_freefall_window(
    clips: list[ClipDict],
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
) -> tuple[list[ClipDict], list[str]]:
    """Clamp freefall clips to ``[E - 8, D + 3]`` (door/exit-prep to opening shock).

    Only clips *of the freefall scene* are clamped — every other scene passes through
    untouched. Freefall-only cuts run ``_drop_non_freefall`` first (so this is a no-op
    guard there), but story cuts (``full_video``/``highlights``) legitimately keep
    intro/boarding/landing/outro clips, which must NOT be clamped to the aerial window.
    """
    kept, log = [], []
    for c in clips:
        if c["scene"] != _FREEFALL_SCENE:
            kept.append(c)
            continue
        anchors = _ff_anchors(_manifest_for(c, manifest, manifest_by_camera))
        if anchors is None:
            kept.append(c)
            continue
        exit_off, deploy_off, dur = anchors
        lo = max(0.0, exit_off - _EXIT_LEAD_S)
        hi = min(dur, deploy_off + _DEPLOY_TAIL_S)
        start, end = float(c["src_start"]), float(c["src_end"])
        if end <= lo or start >= hi:
            log.append(f"dropped {_fmt(c)} — outside freefall window [{lo:.2f}, {hi:.2f}]")
            continue
        new_start, new_end = max(start, lo), min(end, hi)
        if new_end - new_start < _MIN_CLIP_S:
            log.append(f"dropped {_fmt(c)} — too short after clamping to [{lo:.2f}, {hi:.2f}]")
            continue
        if new_start != start or new_end != end:
            log.append(
                f"clamped {_fmt(c)} -> [{new_start:.2f}, {new_end:.2f}]"
                f" — freefall window is [{lo:.2f}, {hi:.2f}]"
            )
            c = {**c, "src_start": new_start, "src_end": new_end}
        kept.append(c)
    return kept, log


def _dedupe(clips: list[ClipDict]) -> tuple[list[ClipDict], list[str]]:
    """Drop clips overlapping a previously-kept clip of the same (camera, scene) by
    more than 50%. Only equal-speed clips are compared: a 0.4x beat inside its
    surrounding 1.0x window is an intentional slow-mo emphasis, not a duplicate."""
    kept: list[ClipDict] = []
    seen: dict[tuple[str | None, str, float], list[ClipDict]] = {}
    log = []
    for c in clips:
        key = (c.get("camera"), c["scene"], float(c.get("speed_multiplier", 1.0)))
        dup = next(
            (p for p in seen.get(key, []) if _overlap_frac(c, p) > _DUP_OVERLAP_FRAC), None
        )
        if dup is not None:
            log.append(
                f"dropped duplicate {_fmt(c)} — overlaps kept clip "
                f"[{float(dup['src_start']):.2f}, {float(dup['src_end']):.2f}] by "
                f"{_overlap_frac(c, dup):.0%}"
            )
            continue
        kept.append(c)
        seen.setdefault(key, []).append(c)
    return kept, log


def _sort_chronological(
    clips: list[ClipDict], ranks: dict[str, int], multicam: bool
) -> tuple[list[ClipDict], list[str]]:
    """Chronological order per (camera, scene).

    Single-cam: a full stable sort by (scene rank, src_start). Multicam: each
    (camera, scene) group's clips are sorted by src_start and written back into the
    positions the group already occupies — repairing out-of-order source times
    without destroying the camera interleave.
    """
    if not multicam:
        ordered = sorted(clips, key=lambda c: (_rank(c, ranks), float(c["src_start"])))
        if ordered != clips:
            return ordered, ["reordered clips into chronological jump order"]
        return list(clips), []

    out = list(clips)
    log = []
    groups: dict[tuple[str | None, str], list[int]] = {}
    for i, c in enumerate(out):
        groups.setdefault(_group_key(c), []).append(i)
    for (camera, scene), indices in groups.items():
        members = [out[i] for i in indices]
        ordered = sorted(members, key=lambda c: float(c["src_start"]))
        if ordered != members:
            for i, c in zip(indices, ordered, strict=True):
                out[i] = c
            who = f"{camera}/{scene}" if camera else scene
            log.append(f"reordered {len(indices)} {who} clips into chronological src order")
    return out, log


def _require_deploy_beat(
    clips: list[ClipDict],
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
    ranks: dict[str, int],
    multicam: bool,
) -> tuple[list[ClipDict], list[str]]:
    """Guarantee the deployment beat: a freefall clip overlapping [D - 1, D + 2].

    Injected from the freefall scene at deploy_offset — never from the "canopy"
    scene, whose content is unreliable. Multicam: satisfied when ANY camera covers
    its own deploy window; the injected beat prefers the instructor camera.
    """
    cameras: list[str | None]
    if multicam and manifest_by_camera:
        # Instructor-preferred, then the rest in sorted order (deterministic).
        cameras = sorted(manifest_by_camera, key=lambda c: (c != "instructor", c))
    else:
        cameras = [None]

    candidates: list[tuple[str | None, float, float, float]] = []
    for cam in cameras:
        m = manifest_by_camera[cam] if cam and manifest_by_camera else manifest
        anchors = _ff_anchors(m)
        if anchors is None:
            continue
        _, deploy_off, dur = anchors
        lo, hi = deploy_off - _DEPLOY_BEAT_PRE_S, deploy_off + _DEPLOY_BEAT_POST_S
        if _covers(clips, _FREEFALL_SCENE, lo, hi, camera=cam):
            return list(clips), []
        candidates.append((cam, lo, hi, dur))

    if not candidates:  # no camera has anchors -> nothing to guarantee
        return list(clips), []
    cam, lo, hi, dur = candidates[0]
    beat: ClipDict = {
        "scene": _FREEFALL_SCENE,
        "src_start": max(lo, 0.0),
        "src_end": min(hi, dur),
        "speed_multiplier": _SLOWMO,
        "camera": cam,
    }
    return (
        _insert_chronological(list(clips), beat, ranks),
        [f"injected deployment beat {_fmt(beat)} — no clip covered [{lo:.2f}, {hi:.2f}]"],
    )


def _boarding_camera(
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
    multicam: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    """The (camera, boarding scene) pair the boarding rule applies to."""
    if multicam and manifest_by_camera:
        for cam in sorted(manifest_by_camera, key=lambda c: (c != "instructor", c)):
            scene = _scene_by_name(manifest_by_camera[cam], _BOARDING_SCENE)
            if scene is not None:
                return cam, scene
        return None, None
    return None, _scene_by_name(manifest, _BOARDING_SCENE)


def _require_boarding(
    clips: list[ClipDict],
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
    ranks: dict[str, int],
    multicam: bool,
) -> tuple[list[ClipDict], list[str]]:
    """Guarantee the plane-entry presence in full_video / highlights.

    With ``file_offsets`` on the boarding scene: one clip from the FIRST source
    file's window and one from the scene's last third (the entry often sits at the
    head of a mid-scene file, not the combined scene's head). Older manifests
    (no ``file_offsets``) degrade to: a clip starting before 5 s plus >= 6 s of
    total boarding coverage.
    """
    cam, boarding = _boarding_camera(manifest, manifest_by_camera, multicam)
    if boarding is None:
        return list(clips), []
    dur = max(float(boarding.get("duration", 0.0)), 0.1)
    own = [
        c
        for c in clips
        if c["scene"] == _BOARDING_SCENE and (not multicam or c.get("camera") == cam)
    ]
    out = list(clips)
    log: list[str] = []

    def _inject(start: float, end: float, reason: str) -> None:
        nonlocal out
        beat: ClipDict = {
            "scene": _BOARDING_SCENE,
            "src_start": round(max(0.0, start), 3),
            "src_end": round(min(dur, max(end, start + _MIN_CLIP_S)), 3),
            "speed_multiplier": 1.0,
            "camera": cam,
        }
        out = _insert_chronological(out, beat, ranks)
        own.append(beat)
        log.append(f"injected boarding beat {_fmt(beat)} — {reason}")

    offsets = boarding.get("file_offsets") or []
    last_third = dur * 2.0 / 3.0
    if offsets:
        first_lo = float(offsets[0]["offset"])
        first_hi = float(offsets[1]["offset"]) if len(offsets) > 1 else dur
        if not any(
            float(c["src_start"]) < first_hi and float(c["src_end"]) > first_lo for c in own
        ):
            _inject(
                first_lo,
                first_lo + _BOARD_BEAT_S,
                f"no clip from first boarding file {offsets[0]['file']}",
            )
        if not any(float(c["src_end"]) > last_third for c in own):
            start = max(last_third, dur - _BOARD_BEAT_S - 1.0)
            _inject(start, start + _BOARD_BEAT_S, "no clip from the boarding scene's last third")
    else:
        if not any(float(c["src_start"]) < _BOARD_HEAD_FALLBACK_S for c in own):
            _inject(
                0.0,
                _BOARD_BEAT_S,
                "no boarding clip near the scene head (no file_offsets; degraded check)",
            )
        coverage = sum(_src_len(c) for c in own)
        if coverage < _BOARD_MIN_COVERAGE_S:
            start = last_third
            # Nudge past an existing clip so the top-up isn't an instant duplicate.
            for c in sorted(own, key=lambda c: float(c["src_start"])):
                if float(c["src_start"]) <= start < float(c["src_end"]):
                    start = float(c["src_end"])
            need = min(max(_BOARD_MIN_COVERAGE_S - coverage, _MIN_SHOT_OUT_S), _BOARD_BEAT_S)
            _inject(
                start,
                start + need,
                f"boarding coverage {coverage:.1f}s < {_BOARD_MIN_COVERAGE_S:.1f}s"
                " (no file_offsets; degraded check)",
            )
    return out, log


def _require_intro(
    clips: list[ClipDict],
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
    ranks: dict[str, int],
    multicam: bool,
) -> tuple[list[ClipDict], list[str]]:
    """highlights: guarantee an intro_interview beat of at least 3 s when the scene
    exists (the model omits the intro entirely on real jobs)."""
    cam: str | None = None
    intro = _scene_by_name(manifest, _INTRO_SCENE)
    if intro is None and multicam and manifest_by_camera:
        for candidate in sorted(manifest_by_camera, key=lambda c: (c != "instructor", c)):
            intro = _scene_by_name(manifest_by_camera[candidate], _INTRO_SCENE)
            if intro is not None:
                cam = candidate
                break
    if intro is None:
        return list(clips), []
    if any(
        c["scene"] == _INTRO_SCENE and _src_len(c) >= _INTRO_MIN_S - _EPS for c in clips
    ):
        return list(clips), []

    dur = max(float(intro.get("duration", 0.0)), 0.1)
    head_end = min(_INTRO_MIN_S, dur)
    out = list(clips)
    # A shorter intro clip already at the head gets extended instead of duplicated.
    for i, c in enumerate(out):
        if c["scene"] == _INTRO_SCENE and float(c["src_start"]) < head_end:
            grown = {**c, "src_end": min(float(c["src_start"]) + _INTRO_MIN_S, dur)}
            out[i] = grown
            return out, [
                f"extended intro clip {_fmt(c)} -> "
                f"[{float(grown['src_start']):.2f}, {float(grown['src_end']):.2f}]"
                f" — highlights intro must run >= {_INTRO_MIN_S:g}s"
            ]
    beat: ClipDict = {
        "scene": _INTRO_SCENE,
        "src_start": 0.0,
        "src_end": head_end,
        "speed_multiplier": 1.0,
        "camera": cam,
    }
    return (
        _insert_chronological(out, beat, ranks),
        [f"injected intro head {_fmt(beat)} — highlights lacked a >={_INTRO_MIN_S:g}s intro clip"],
    )


def _anchored(
    clip: ClipDict,
    edge: str,
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
) -> float:
    """A clip edge on the cross-camera timeline: freefall times shift so each
    camera's exit_offset is t=0; other scenes use the raw source time."""
    t = float(clip[edge])
    if clip["scene"] == _FREEFALL_SCENE:
        anchors = _ff_anchors(_manifest_for(clip, manifest, manifest_by_camera))
        if anchors is not None:
            return t - anchors[0]
    return t


def _align_cross_camera(
    clips: list[ClipDict],
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
    ranks: dict[str, int],
) -> tuple[list[ClipDict], list[str]]:
    """Fix camera-alignment drift: within each contiguous same-scene-rank run that
    mixes cameras, order clips by exit-anchored time (reorder, never retime).
    Residual >4 s anchored jumps at a switch (one camera simply lacks coverage) are
    logged and kept — there is no material to repair them with."""
    out: list[ClipDict] = []
    log: list[str] = []
    i = 0
    while i < len(clips):
        j = i
        run_rank = _rank(clips[i], ranks)
        while j < len(clips) and _rank(clips[j], ranks) == run_rank:
            j += 1
        run = clips[i:j]
        if len({c.get("camera") for c in run}) > 1:
            ordered = sorted(
                enumerate(run),
                key=lambda pair: (
                    _anchored(pair[1], "src_start", manifest, manifest_by_camera),
                    pair[0],
                ),
            )
            reordered = [c for _, c in ordered]
            if reordered != run:
                log.append(
                    f"re-anchored {len(run)} '{run[0]['scene']}' clips by "
                    "exit-anchored cross-camera time"
                )
            run = reordered
        out.extend(run)
        i = j

    for prev, cur in zip(out, out[1:], strict=False):
        if prev.get("camera") == cur.get("camera") or _rank(prev, ranks) != _rank(cur, ranks):
            continue
        gap = abs(
            _anchored(cur, "src_start", manifest, manifest_by_camera)
            - _anchored(prev, "src_end", manifest, manifest_by_camera)
        )
        if gap > _ANCHOR_TOL_S:
            log.append(
                f"camera switch {prev.get('camera')}->{cur.get('camera')} at {_fmt(cur)} "
                f"spans {gap:.1f}s of anchored time (one camera lacks coverage); kept"
            )
    return out, log


def _limit_switch_rate(
    clips: list[ClipDict], ranks: dict[str, int]
) -> tuple[list[ClipDict], list[str]]:
    """No more than one camera switch per 3 s of output timeline: a switch arriving
    early pulls the NEXT same-camera clip of the same scene rank forward instead
    (defer the switch), preserving per-(camera, scene) chronological order."""
    out: list[ClipDict] = []
    log: list[str] = []
    pending = list(clips)
    since_switch = float("inf")  # the opening shot never counts as a switch
    while pending:
        cur = pending.pop(0)
        if out and cur.get("camera") != out[-1].get("camera"):
            while since_switch < _SWITCH_SPACING_OUT_S:
                prev_cam = out[-1].get("camera")
                idx = next(
                    (
                        k
                        for k, c in enumerate(pending)
                        if c.get("camera") == prev_cam
                        and _rank(c, ranks) == _rank(cur, ranks)
                    ),
                    None,
                )
                if idx is None:
                    log.append(
                        f"allowed early camera switch after {since_switch:.1f}s"
                        f" — no more {prev_cam} material in scene '{cur['scene']}'"
                    )
                    break
                held = pending.pop(idx)
                log.append(
                    f"deferred camera switch: moved {_fmt(held)} ahead to keep"
                    f" >={_SWITCH_SPACING_OUT_S:g}s per camera"
                )
                out.append(held)
                since_switch += _out_dur(held)
            since_switch = 0.0
        out.append(cur)
        since_switch += _out_dur(cur)
    return out, log


def _enforce_min_shot(clips: list[ClipDict]) -> tuple[list[ClipDict], list[str]]:
    """Multi-cam pacing: non-slow-mo shots under 1.5 s output merge into a
    contiguous same-(camera, scene, speed) neighbour, else drop. Runs last —
    merging/dropping fragments can only reduce camera switches, never add them."""
    out: list[ClipDict] = []
    log: list[str] = []
    i = 0
    while i < len(clips):
        c = clips[i]
        if _is_slowmo(c) or _out_dur(c) >= _MIN_SHOT_OUT_S:
            out.append(c)
            i += 1
            continue

        def _contiguous(a: ClipDict, b: ClipDict) -> bool:
            return (
                _group_key(a) == _group_key(b)
                and float(a.get("speed_multiplier", 1.0)) == float(b.get("speed_multiplier", 1.0))
                and abs(float(a["src_end"]) - float(b["src_start"])) < _EPS
            )

        if out and _contiguous(out[-1], c):
            merged = {**out[-1], "src_end": c["src_end"]}
            log.append(f"merged {_fmt(c)} into preceding contiguous shot")
            out[-1] = merged
        elif i + 1 < len(clips) and _contiguous(c, clips[i + 1]):
            merged = {**clips[i + 1], "src_start": c["src_start"]}
            log.append(f"merged {_fmt(c)} into following contiguous shot")
            clips = [*clips[: i + 1], merged, *clips[i + 2 :]]
        else:
            log.append(f"dropped {_fmt(c)} — {_out_dur(c):.2f}s shot below "
                       f"{_MIN_SHOT_OUT_S:g}s minimum")
        i += 1
    return out, log


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def validate_and_repair(
    edl: Sequence[ClipDict],
    deliverable: str,
    manifest: dict[str, Any],
    *,
    manifest_by_camera: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[ClipDict], list[str]]:
    """Validate a composed EDL against the mandatory story/shot rules; repair in place.

    ``deliverable`` is the pipeline's deliverable key (``full_video``,
    ``highlights``, ``freefall``, ``external_freefall``, ``chute_libre_selfie``).
    ``manifest`` is the scene manifest the clips' timestamps live in; for the
    Ultimate combo pass ``manifest_by_camera`` so camera-tagged clips resolve their
    own camera's ``exit_offset``/``deploy_offset``.

    Returns the repaired clip list (input untouched) and human-readable repair log
    entries, one per repair — empty when the EDL was already compliant.
    """
    clips = [dict(c) for c in edl]
    if not clips:
        return [], []
    log: list[str] = []
    ranks = _rank_map(manifest, manifest_by_camera)
    multicam = any(c.get("camera") for c in clips)

    def _apply(result: tuple[list[ClipDict], list[str]]) -> list[ClipDict]:
        fixed, lines = result
        log.extend(lines)
        return fixed

    if deliverable in FREEFALL_DELIVERABLES:
        # Freefall-only cuts contain nothing but the freefall scene.
        clips = _apply(_drop_non_freefall(clips))
    if deliverable in FREEFALL_DELIVERABLES or deliverable in _STORY_DELIVERABLES:
        # The aerial-freefall window (exit -> deploy, with lead/tail allowance) applies to
        # every deliverable that carries freefall footage: story cuts (full_video, highlights)
        # must not show pre-exit aircraft footage or post-opening scenery either.
        # _clamp_freefall_window only clamps freefall-scene clips, so intro/boarding/landing/
        # outro clips in story cuts pass through untouched.
        clips = _apply(_clamp_freefall_window(clips, manifest, manifest_by_camera))
    clips = _apply(_dedupe(clips))
    clips = _apply(_sort_chronological(clips, ranks, multicam))
    if deliverable in FREEFALL_DELIVERABLES or deliverable in _STORY_DELIVERABLES:
        clips = _apply(
            _require_deploy_beat(clips, manifest, manifest_by_camera, ranks, multicam)
        )
    if deliverable in _STORY_DELIVERABLES:
        clips = _apply(
            _require_boarding(clips, manifest, manifest_by_camera, ranks, multicam)
        )
    if deliverable == "highlights":
        clips = _apply(
            _require_intro(clips, manifest, manifest_by_camera, ranks, multicam)
        )
    if multicam:
        clips = _apply(_align_cross_camera(clips, manifest, manifest_by_camera, ranks))
        clips = _apply(_limit_switch_rate(clips, ranks))
        clips = _apply(_enforce_min_shot(clips))

    if not clips:  # never hand back an empty deliverable (EDLResponse needs >= 1 clip)
        return [dict(c) for c in edl], [
            *log,
            "all clips repaired away; kept the original EDL for instructor review",
        ]
    return clips, log
