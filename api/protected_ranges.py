"""Resolve a deliverable's persisted EDL + manifest(s) and hand them to the pure
:func:`edl.protected.protected_ranges` (``GET /jobs/{id}/deliverables/{name}/protected-ranges``).

The file resolution mirrors :func:`api.source_usage.build_source_usage` (and the render
sites it mirrors): which ``edl_*.json`` produced the deliverable, which scene manifest
its timestamps live in, then the render's own post-persistence transforms — the
``exclude.json`` cuts and the playable-duration clamp — so the seconds returned are the
seconds of the rendered file. Read-only; never writes anything.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from edl.protected import protected_ranges
from edl.storage import job_dir

from .jobs import Job, deliverable_name

logger = logging.getLogger(__name__)

#: Base deliverable → persisted EDL file of the single-camera scene pipeline.
_EDL_SHORT: dict[str, str] = {
    "full_video": "edl_full.json",
    "highlights": "edl_highlights.json",
    "freefall": "edl_freefall.json",
}


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("protected ranges: unreadable %s", path)
        return None


def build_protected_ranges(
    job: Job, name: str, jobs_root: str | Path | None = None
) -> dict[str, Any]:
    """``{"deliverable", "duration", "ranges", "reason"?}`` for one video deliverable.

    Never raises for missing artifacts: a deliverable with no persisted EDL (the legacy
    single-master pipeline, a load child, a job rendered before the scene pipeline)
    answers ``ranges: []`` with a ``reason`` — callers then enforce nothing.
    """
    from .selfie import (  # noqa: PLC0415 - keep the app import light
        _CARD_SECONDS,
        _OUT_FPS,
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
        exclude_freefall_head,
        load_exclusions,
    )

    jd = job_dir(job.job_id, jobs_root)

    def _empty(reason: str) -> dict[str, Any]:
        return {"deliverable": name, "duration": None, "ranges": [], "reason": reason}

    # (base deliverable, EDL file, manifest context): context is a camera role, None for
    # the plain single-camera scene set, or "combo" for the Ultimate multi-cam merge.
    spec: tuple[str, str, str | None] | None = None
    if job.package.is_ultimum:
        if name in ULTIMUM_EDL_FILES:
            spec = (name, ULTIMUM_EDL_FILES[name], ULTIMUM_FREEFALL_ROLE.get(name, "combo"))
    elif job.is_multi_ref:
        for ref in job.media_refs:
            prefix = "" if _owns_photos(job, ref.role) else f"{ref.role}_"
            for base, fname in _EDL_SHORT.items():
                if deliverable_name(job, ref.role, base) == name:
                    spec = (base, f"{prefix}{fname}", ref.role)
    elif name in _EDL_SHORT:
        spec = (name, _EDL_SHORT[name], None)
    if spec is None:
        return _empty("no scene-pipeline EDL for this deliverable (legacy or not an AI cut)")
    base, edl_file, context = spec

    raw = _read_json(jd / edl_file)
    if not isinstance(raw, list):
        return _empty(f"{edl_file} not found — nothing to derive the rules from")
    try:
        clips = [Clip.model_validate(c) for c in raw]
    except ValueError:
        return _empty(f"{edl_file} is not a valid EDL")

    role_manifests = {
        role: m
        for role in CAMERA_ROLES
        if isinstance(m := _read_json(jd / f"scene_manifest_{role}.json"), dict)
    }
    manifest_by_camera: dict[str, dict[str, Any]] | None = None
    if context == "combo":
        if not role_manifests:
            return _empty("no per-camera scene manifests for the Ultimate combo")
        manifest = role_manifests.get("instructor") or next(iter(role_manifests.values()))
        manifest_by_camera = role_manifests
        scene_paths = _multicam_scene_paths(role_manifests)
    else:
        found = (
            role_manifests.get(context) if context is not None
            else _read_json(jd / "scene_manifest.json")
        )
        if not isinstance(found, dict):
            return _empty("scene manifest not found — nothing to derive the rules from")
        manifest = found
        scene_paths = _scene_paths(manifest)

    # The render's own post-persistence transforms, in its order (as source_usage does).
    exclusions = load_exclusions(job.job_id, jobs_root)
    freefall_only = base in ("freefall", "external_freefall", "chute_libre_selfie")
    clips = (
        exclude_freefall_head(clips, exclusions)
        if freefall_only
        else apply_exclusions(
            EDLResponse(full_video=clips, highlights=clips, freefall=clips), exclusions
        ).full_video
    )
    clips = _clamp_clips_to_scenes(clips, scene_paths)

    result = protected_ranges(
        [c.model_dump() for c in clips],
        base,
        manifest,
        manifest_by_camera=manifest_by_camera,
        tail_card_s=_CARD_SECONDS,
        fps=_OUT_FPS,
    )
    out: dict[str, Any] = {
        "deliverable": name, "duration": result["duration"], "ranges": result["ranges"],
    }
    if result.get("reason"):
        out["reason"] = result["reason"]
    return out
