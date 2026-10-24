"""Protected spans of a rendered deliverable, in its OUTPUT time.

The pipeline's editing rules (``edl.validate``) are enforced on the EDL before it is
persisted — but a staff re-edit in SkydiveOS works on the RENDERED file, where the
filmed intro is just "the first 55 seconds" and the freefall is "1:12 to 2:10". This
module translates the established rules into those seconds, so the editor can refuse
an edit that would break them (cross-repo contract:
``GET /jobs/{id}/deliverables/{name}/protected-ranges``):

* ``intro_bookend`` / ``outro_bookend`` — ``full_video`` only
  (:data:`edl.validate.BOOKEND_DELIVERABLES`, Bug 441): the first source file of
  ``intro_interview`` and the last of ``outro_interview`` play whole, at 1x, and no
  music plays over them. ``music_until`` is where the music may come back: the span's
  end, or the end of the file when the outro ends the edit (the renderer keeps the
  music off through the logo card, ``api.selfie.render_selfie_video``).
* ``freefall`` — :data:`edl.validate.CONTINUOUS_FREEFALL_DELIVERABLES`: the aerial run
  plays once, in order, with nothing removed from its middle or end. For the
  freefall-only cuts (:data:`edl.validate.FREEFALL_DELIVERABLES`) the HEAD may be
  shortened, never past the exit: ``head_trimmable_until`` is the output time of the
  exit. The Ultimate combo's alternation and the slow-mo beats already baked into the
  render are inside the span — it is one protected run.

``highlights`` carries no protected range (no established rule applies to it).

Rules, as in ``edl.validate``: **pure** (no I/O, never imports ``api.*``), inputs are
the plain clip dicts persisted to ``edl_*.json``. Anything not derivable — no clips, no
bookend scene, a bookend the EDL does not hold whole, a freefall that is not one block,
a manifest without exit/deploy anchors (an EDL composed before the rules) — yields no
range for that rule, and callers then enforce nothing for it: legacy behaviour.

Output time = the clips' output durations (``src_len / speed``) summed in order, the
same arithmetic the renderer uses. The caller should pass clips AFTER the render's own
post-persistence transforms (``exclude.json`` cuts, the playable-duration clamp) so the
seconds match the file. ``fps`` rounds each clip to whole frames, as the renderer's
per-clip ``fps`` filter does; the residual error is under a frame per clip boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from edl.validate import (
    BOOKEND_DELIVERABLES,
    CONTINUOUS_FREEFALL_DELIVERABLES,
    FREEFALL_DELIVERABLES,
    _ff_anchors,
    _manifest_for,
    bookend_ranges,
)

ClipDict = dict[str, Any]

#: Source-time slack when matching a clip to a bookend range (the validator inserts
#: the bookend with exactly these edges; the renderer's ``_is_bookend`` uses the same).
_TOL = 1e-3
_FREEFALL_SCENE = "freefall"
_INTRO_SCENE = "intro_interview"


def _speed(clip: ClipDict) -> float:
    return float(clip.get("speed_multiplier", 1.0) or 1.0)


def _out_dur(clip: ClipDict, fps: float | None) -> float:
    dur = (float(clip["src_end"]) - float(clip["src_start"])) / _speed(clip)
    if fps:
        dur = round(dur * fps) / fps
    return max(dur, 0.0)


def _r(t: float) -> float:
    return round(t, 3)


def _bookend_span(
    clips: Sequence[ClipDict],
    starts: Sequence[float],
    durs: Sequence[float],
    bookend: dict[str, Any],
    total: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """The output window of the clip(s) that play ``bookend`` whole, or a note why not."""
    cam, scene = bookend.get("camera") or None, bookend["scene"]
    lo, hi = float(bookend["src_start"]), float(bookend["src_end"])
    label = "intro" if scene == _INTRO_SCENE else "outro"
    # Same membership test as the renderer's _is_bookend: same (camera, scene), real
    # time, inside the range.
    idx = [
        i for i, c in enumerate(clips)
        if (c.get("camera") or None) == cam
        and c["scene"] == scene
        and abs(_speed(c) - 1.0) < 1e-9
        and float(c["src_start"]) >= lo - _TOL
        and float(c["src_end"]) <= hi + _TOL
    ]
    if not idx:
        return None, f"{label} bookend is not in this EDL at 1x (composed before the rule?)"
    whole = (
        idx == list(range(idx[0], idx[-1] + 1))
        and float(clips[idx[0]]["src_start"]) <= lo + _TOL
        and float(clips[idx[-1]]["src_end"]) >= hi - _TOL
        and all(
            abs(float(clips[a]["src_end"]) - float(clips[b]["src_start"])) <= _TOL
            for a, b in zip(idx, idx[1:], strict=False)
        )
    )
    if not whole:
        return None, f"{label} bookend is not whole in this EDL (composed before the rule?)"
    start = starts[idx[0]]
    end = starts[idx[-1]] + durs[idx[-1]]
    ends_edit = idx[-1] == len(clips) - 1
    return {
        "kind": f"{label}_bookend",
        "start": _r(start),
        "end": _r(end),
        "music": False,
        # The renderer keeps the music off through the logo card when the outro
        # bookend is the last clip.
        "music_until": _r(total if ends_edit else end),
    }, None


def _freefall_span(
    clips: Sequence[ClipDict],
    starts: Sequence[float],
    durs: Sequence[float],
    deliverable: str,
    manifest: dict[str, Any],
    manifest_by_camera: dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The output window of the freefall run, or a note why there is none."""
    idx = [i for i, c in enumerate(clips) if c["scene"] == _FREEFALL_SCENE]
    if not idx:
        return None, "no freefall in this EDL"
    if idx != list(range(idx[0], idx[-1] + 1)):
        return None, "freefall is not one block in this EDL (composed before the rule?)"
    anchored = [
        _ff_anchors(_manifest_for(clips[i], manifest, manifest_by_camera)) for i in idx
    ]
    if all(a is None for a in anchored):
        return None, "no exit/deploy anchors in the manifest — the freefall rules never ran"
    start = starts[idx[0]]
    span: dict[str, Any] = {
        "kind": "freefall",
        "start": _r(start),
        "end": _r(starts[idx[-1]] + durs[idx[-1]]),
    }
    if deliverable in FREEFALL_DELIVERABLES:
        # Head-only shortening: up to the output time of the exit, never past it. An
        # exit before the run (no lead) or not found in it leaves no head to trim.
        until = start
        for i, anchors in zip(idx, anchored, strict=True):
            if anchors is None:
                continue
            exit_off = anchors[0]
            c = clips[i]
            s0, s1 = float(c["src_start"]), float(c["src_end"])
            if s0 <= exit_off < s1:
                until = starts[i] + min((exit_off - s0) / _speed(c), durs[i])
                break
            if exit_off < s0:
                break
        span["head_trimmable_until"] = _r(until)
    return span, None


def protected_ranges(
    clips: Sequence[ClipDict],
    deliverable: str,
    manifest: dict[str, Any],
    *,
    manifest_by_camera: dict[str, dict[str, Any]] | None = None,
    tail_card_s: float = 0.0,
    fps: float | None = None,
) -> dict[str, Any]:
    """``{"duration", "ranges", "reason"}`` for one deliverable's clip list.

    ``deliverable`` is the BASE name the rules are keyed by (``full_video``,
    ``highlights``, ``freefall``, ``external_freefall``, ``chute_libre_selfie``) — a
    mixed job's ``external_full_video`` is a ``full_video``. ``manifest`` /
    ``manifest_by_camera`` are what the EDL was validated against (the Ultimate combo
    passes both). ``tail_card_s`` is the branded outro card the renderer appends after
    the footage (part of ``duration``, never of a span except an outro's
    ``music_until``).

    ``duration`` is ``None`` and ``ranges`` empty when there are no clips. ``reason``
    is ``None`` when every applicable rule produced a range, else a human-readable
    note (one line per rule that could not be derived).
    """
    items = [dict(c) for c in clips]
    if not items:
        return {"duration": None, "ranges": [], "reason": "no EDL clips"}
    durs = [_out_dur(c, fps) for c in items]
    starts: list[float] = []
    t = 0.0
    for d in durs:
        starts.append(t)
        t += d
    total = t + max(float(tail_card_s), 0.0)

    ranges: list[dict[str, Any]] = []
    notes: list[str] = []
    multicam = any(c.get("camera") for c in items)
    if deliverable in BOOKEND_DELIVERABLES:
        for bookend in bookend_ranges(manifest, manifest_by_camera, multicam=multicam):
            span, note = _bookend_span(items, starts, durs, bookend, total)
            if span is not None:
                ranges.append(span)
            elif note:
                notes.append(note)
    if deliverable in CONTINUOUS_FREEFALL_DELIVERABLES:
        span, note = _freefall_span(
            items, starts, durs, deliverable, manifest, manifest_by_camera
        )
        if span is not None:
            ranges.append(span)
        elif note:
            notes.append(note)
    if not (deliverable in BOOKEND_DELIVERABLES or deliverable in CONTINUOUS_FREEFALL_DELIVERABLES):
        notes.append(f"{deliverable} carries no protected range")
    ranges.sort(key=lambda r: (r["start"], r["end"]))
    return {"duration": _r(total), "ranges": ranges, "reason": "; ".join(notes) or None}
