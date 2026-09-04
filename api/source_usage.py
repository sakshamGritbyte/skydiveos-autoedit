"""Source-usage manifest: which RAW-MASTER seconds each deliverable actually used.

The persisted ``edl_*.json`` files speak in *scene-file* seconds (each scene is a
concatenation of same-scene raw clips), and they are a **superset** of the render:
``exclude.json`` cuts and the playable-duration clamp are applied after persistence,
inside the render. So nothing on disk directly answers the question SkydiveOS's manual
editor asks — "which seconds of ``GX010990.MP4`` ended up in ``full_video.mp4``?"

This module answers it by replaying exactly the transforms the render applies
(:func:`api.selfie.apply_exclusions`, then :func:`api.selfie._clamp_clips_to_scenes`
against the same scene paths) and then walking each scene manifest's ``file_offsets``
to convert scene time into raw-master time — splitting a clip that spans a raw-file
boundary into one range per file. The result is written as ``source_usage.json`` in the
job dir and uploaded next to the deliverables (``deliveries/{job_id}/``), so it outlives
the local raw masters (pruned ~2 days after delivery) and is readable S3→S3.

Everything here derives from artifacts that persist indefinitely (``job.json``,
``edl_*.json``, ``scene_manifest*.json``, ``scenes_*/``), so the manifest can also be
built on demand for a job rendered before this module existed — which is why
:func:`write_source_usage` takes only ``(job_id, store, jobs_root)``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from edl.storage import job_dir

from .jobs import Job, deliverable_name, raw_key_for

logger = logging.getLogger(__name__)

SOURCE_USAGE_FILENAME = "source_usage.json"

#: ``edl_*.json`` basename for each single-camera deliverable base name.
_EDL_SHORT: dict[str, str] = {
    "full_video": "edl_full.json",
    "highlights": "edl_highlights.json",
    "freefall": "edl_freefall.json",
}


def source_usage_path(job_id: str, jobs_root: str | Path | None = None) -> Path:
    return job_dir(job_id, jobs_root) / SOURCE_USAGE_FILENAME


# --------------------------------------------------------------------------- #
# Scene lookup: scene key -> (role, manifest scene dict)
# --------------------------------------------------------------------------- #


def _file_spans(scene: dict[str, Any]) -> list[tuple[str, float, float]]:
    """``(filename, start, end)`` of each raw file inside the concatenated scene.

    Derived from ``file_offsets`` (ascending) with the scene's total duration closing
    the last span — the exact inverse of how ``build_scenes`` accumulated the offsets.
    """
    offsets = scene.get("file_offsets") or []
    duration = float(scene.get("duration") or 0.0)
    spans: list[tuple[str, float, float]] = []
    for i, entry in enumerate(offsets):
        start = float(entry["offset"])
        end = float(offsets[i + 1]["offset"]) if i + 1 < len(offsets) else duration
        if end > start:
            spans.append((str(entry["file"]), start, end))
    return spans


def _lookup_from_manifest(
    manifest: dict[str, Any], role: str | None
) -> dict[str, tuple[str | None, dict[str, Any]]]:
    """Scene-key → ``(role, scene dict)`` for one camera's (or the plain) scene set."""
    return {s["name"]: (role, s) for s in manifest.get("scenes", [])}


def _combo_lookup(
    role_manifests: dict[str, dict[str, Any]], camera_roles: tuple[str, ...]
) -> dict[str, tuple[str | None, dict[str, Any]]]:
    """Scene-key → ``(role, scene dict)`` for the multi-cam combo.

    Mirrors :func:`api.selfie._multicam_scene_paths` exactly, bare-name fallback
    included (first camera that has the scene, in ``CAMERA_ROLES`` order) — an
    exclusion split drops a clip's ``camera`` tag, and the render then resolves it
    through that same fallback, so the usage must attribute it the same way.
    """
    lookup: dict[str, tuple[str | None, dict[str, Any]]] = {}
    for role in camera_roles:
        manifest = role_manifests.get(role)
        if not manifest:
            continue
        for s in manifest.get("scenes", []):
            lookup[f"{role}/{s['name']}"] = (role, s)
            lookup.setdefault(s["name"], (role, s))
    return lookup


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #


def _usage_entries(
    clips: list[Any],
    lookup: dict[str, tuple[str | None, dict[str, Any]]],
    job: Job,
) -> list[dict[str, Any]]:
    """Convert one deliverable's final (excluded + clamped) clip list into raw-file
    ranges, splitting clips that span raw-file boundaries and accumulating the
    output-timeline cursor (``out_start``/``out_end``, pre-outro seconds)."""
    from .selfie import _clip_out_dur, _scene_key

    entries: list[dict[str, Any]] = []
    cursor = 0.0
    for clip in clips:
        resolved = lookup.get(_scene_key(clip))
        if resolved is None:
            # A scene the manifests don't know (shouldn't happen for a rendered job);
            # keep the timeline cursor honest and move on.
            cursor += _clip_out_dur(clip)
            continue
        role, scene = resolved
        for filename, span_start, span_end in _file_spans(scene):
            a = max(clip.src_start, span_start)
            b = min(clip.src_end, span_end)
            if b - a <= 0.0:
                continue
            out_start = cursor + (a - clip.src_start) / clip.speed_multiplier
            entries.append(
                {
                    "raw_filename": filename,
                    "role": role,
                    "s3_key": raw_key_for(job, role, filename),
                    "src_start": round(a - span_start, 3),
                    "src_end": round(b - span_start, 3),
                    "speed_multiplier": clip.speed_multiplier,
                    "scene": clip.scene,
                    "out_start": round(out_start, 3),
                    "out_end": round(out_start + (b - a) / clip.speed_multiplier, 3),
                }
            )
        cursor += _clip_out_dur(clip)
    return entries


def _raw_files(
    manifests: list[tuple[str | None, dict[str, Any]]], job: Job
) -> list[dict[str, Any]]:
    """One row per raw master across every scene set: filename, role, S3 key, duration.

    Durations come from consecutive ``file_offsets`` deltas (each raw file lives in
    exactly one scene), so SkydiveOS never has to probe the masters itself.
    """
    rows: dict[tuple[str | None, str], dict[str, Any]] = {}
    for role, manifest in manifests:
        for scene in manifest.get("scenes", []):
            for filename, span_start, span_end in _file_spans(scene):
                key = (role, filename)
                if key not in rows:
                    rows[key] = {
                        "filename": filename,
                        "role": role,
                        "s3_key": raw_key_for(job, role, filename),
                        "duration": round(span_end - span_start, 3),
                    }
    return list(rows.values())


def build_source_usage(
    job_id: str, store: Any, jobs_root: str | Path | None = None
) -> dict[str, Any] | None:
    """Build the usage manifest for every video deliverable this job persisted EDLs for.

    Returns ``None`` when the job has no scene-pipeline artifacts at all (photo-only
    packages, the legacy single-master pipeline). Raises on nothing reader-facing:
    a deliverable whose EDL or manifest is missing is simply skipped.
    """
    from .selfie import (
        CAMERA_ROLES,
        ULTIMUM_EDL_FILES,
        ULTIMUM_FREEFALL_ROLE,
        Clip,
        EDLResponse,
        _clamp_clips_to_scenes,
        _multicam_scene_paths,
        _owns_photos,
        _scene_paths,
        apply_exclusions,
        load_exclusions,
    )

    job = store.load(job_id)
    jd = job_dir(job_id, jobs_root)
    exclusions = load_exclusions(job_id, jobs_root)

    def _load_manifest(name: str) -> dict[str, Any] | None:
        path = jd / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("job %s: unreadable scene manifest %s", job_id, name)
            return None

    role_manifests = {
        role: m
        for role in CAMERA_ROLES
        if (m := _load_manifest(f"scene_manifest_{role}.json")) is not None
    }
    plain_manifest = _load_manifest("scene_manifest.json")

    def _load_clips(filename: str) -> list[Clip] | None:
        path = jd / filename
        if not path.exists():
            return None
        try:
            return [Clip.model_validate(c) for c in json.loads(path.read_text())]
        except (OSError, ValueError) as e:
            logger.warning("job %s: unreadable EDL %s (%s)", job_id, filename, e)
            return None

    # (deliverable name, EDL filename, resolution context). Context is a camera role,
    # None for the plain single-camera scene set, or "combo" for the multi-cam merge.
    specs: list[tuple[str, str, str | None]] = []
    if job.package.is_ultimum:
        specs = [
            ("full_video", ULTIMUM_EDL_FILES["full_video"], "combo"),
            ("highlights", ULTIMUM_EDL_FILES["highlights"], "combo"),
        ]
        specs += [
            (deliverable, ULTIMUM_EDL_FILES[deliverable], role)
            for deliverable, role in ULTIMUM_FREEFALL_ROLE.items()
        ]
    elif job.is_multi_ref:
        for ref in job.media_refs:
            if not ref.package.makes_videos:
                continue
            prefix = "" if _owns_photos(job, ref.role) else f"{ref.role}_"
            specs += [
                (deliverable_name(job, ref.role, base), f"{prefix}{fname}", ref.role)
                for base, fname in _EDL_SHORT.items()
            ]
    else:
        specs = [(base, fname, None) for base, fname in _EDL_SHORT.items()]

    deliverables: dict[str, list[dict[str, Any]]] = {}
    used_manifests: dict[tuple[str | None, int], tuple[str | None, dict[str, Any]]] = {}
    for name, edl_filename, context in specs:
        clips = _load_clips(edl_filename)
        if clips is None:
            continue
        if context == "combo":
            if not role_manifests:
                continue
            scene_paths = _multicam_scene_paths(role_manifests)
            lookup = _combo_lookup(role_manifests, CAMERA_ROLES)
            for role, m in role_manifests.items():
                used_manifests[(role, id(m))] = (role, m)
        elif context is not None:
            manifest = role_manifests.get(context)
            if manifest is None:
                continue
            scene_paths = _scene_paths(manifest)
            lookup = _lookup_from_manifest(manifest, context)
            used_manifests[(context, id(manifest))] = (context, manifest)
        else:
            if plain_manifest is None:
                continue
            scene_paths = _scene_paths(plain_manifest)
            lookup = _lookup_from_manifest(plain_manifest, None)
            used_manifests[(None, id(plain_manifest))] = (None, plain_manifest)

        # Replay the render's own post-persistence transforms, in its order: the
        # exclusion cuts, then the playable-duration clamp against the same files.
        clips = apply_exclusions(
            EDLResponse(full_video=clips, highlights=clips, freefall=clips), exclusions
        ).full_video
        clips = _clamp_clips_to_scenes(clips, scene_paths)
        deliverables[name] = _usage_entries(clips, lookup, job)

    if not deliverables:
        return None
    return {
        "job_id": job_id,
        "version": 1,
        "generated_at": time.time(),
        "raw_files": _raw_files(list(used_manifests.values()), job),
        "deliverables": deliverables,
    }


def write_source_usage(
    job_id: str, store: Any, jobs_root: str | Path | None = None
) -> Path | None:
    """Build and atomically persist ``source_usage.json``; never raises.

    Usage bookkeeping must never fail a render — any error is logged and swallowed,
    and the reader (the ``/source-usage`` endpoint, the delivery upload) simply finds
    no file.
    """
    try:
        usage = build_source_usage(job_id, store, jobs_root)
        if usage is None:
            return None
        path = source_usage_path(job_id, jobs_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".source_usage.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(usage, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return path
    except Exception:  # noqa: BLE001 — bookkeeping must never break the pipeline
        logger.exception("job %s: building source_usage.json failed", job_id)
        return None
