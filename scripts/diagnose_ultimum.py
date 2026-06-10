"""Diagnose an Ultimate (``ultimum``) job's combo: classification, clip selection,
stream sync, and photo count — read-only, no re-render.

Use this when the Combo Video looks wrong (cameraman missing, video freezes mid-play) to
get ground truth on WHY. It reports, from a job's on-disk artifacts:

* the per-camera scene sets (what each camera classified into, durations, #source files);
* the combo EDLs (``edl_full.json`` / ``edl_highlights.json``) broken down by
  ``(camera, scene)`` so you can see whether the cameraman is actually being selected,
  and for which scenes;
* a video-vs-audio stream-duration check on every scene file AND every rendered output —
  a gap means the "video freezes while audio keeps playing" desync;
* the per-camera freefall cuts and the photo count.

It flags the common failure modes: a camera that collapsed to a single scene (a
continuous recording the per-file classifier can't split), a scene the cameraman is
absent from, and any A/V desync.

Usage:
    python scripts/diagnose_ultimum.py <job_id> [--jobs-root DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Allow running as a file: put the repo root on the path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.jobs import JobStore  # noqa: E402
from api.selfie import (  # noqa: E402
    _ULTIMUM_ROLE_MANIFEST,
    CAMERA_ROLES,
    ULTIMUM_EDL_FILES,
    _stream_durations,
    probe_duration,
)

#: A video/audio stream gap larger than this reads as a desync (frozen-frame risk).
_DESYNC_S = 0.3


def _w(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def _load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _sync_label(path: Path) -> str:
    """A ``video=.. audio=.. [OK|DESYNC]`` summary for a media file, or a not-found note."""
    if not path.exists():
        return "(missing)"
    video, audio = _stream_durations(path)
    if video <= 0 and audio <= 0:
        return f"container={probe_duration(path):.2f} (no per-stream duration)"
    desync = video > 0 and audio > 0 and abs(video - audio) > _DESYNC_S
    flag = "  <<< DESYNC" if desync else "  [OK]"
    return f"video={video:.2f} audio={audio:.2f}{flag}"


def _report_manifest(jd: Path, role: str, findings: list[str]) -> None:
    manifest = _load_json(jd / _ULTIMUM_ROLE_MANIFEST[role])
    _w(f"  {role}  ({_ULTIMUM_ROLE_MANIFEST[role]}):")
    if not isinstance(manifest, dict) or not manifest.get("scenes"):
        _w("    (no manifest — camera not processed, or job pre-dates the per-camera split)")
        findings.append(f"{role}: no per-camera scene manifest on disk")
        return
    scenes = manifest["scenes"]
    for s in scenes:
        files = s.get("source_files", [])
        path = Path(s.get("combined_path", ""))
        review = "  needs_review" if s.get("needs_review") else ""
        _w(f"    {s['name']:<16} dur={float(s.get('duration', 0)):6.2f}  "
           f"files={len(files):<2} {_sync_label(path)}{review}")
    names = [s["name"] for s in scenes]
    if len(scenes) == 1:
        findings.append(
            f"{role}: only ONE scene ({names[0]}) — likely a single continuous recording "
            f"the per-file classifier can't split across jump phases"
        )
    if any(s.get("needs_review") for s in scenes):
        findings.append(f"{role}: has 'unknown'/needs_review scenes (classification unsure)")


def _report_combo_edl(jd: Path, filename: str, findings: list[str]) -> None:
    clips = _load_json(jd / filename)
    _w(f"  {filename}:")
    if not isinstance(clips, list) or not clips:
        _w("    (missing or empty)")
        findings.append(f"{filename}: missing/empty")
        return
    per = Counter((c.get("camera"), c["scene"]) for c in clips)
    cameras = {c.get("camera") for c in clips}
    by_cam: dict[object, list[str]] = {}
    for (cam, scene), n in sorted(per.items(), key=lambda kv: str(kv[0])):
        by_cam.setdefault(cam, []).append(f"{scene}({n})")
    _w(f"    clips={len(clips)}  cameras={sorted(str(c) for c in cameras)}")
    for cam, items in by_cam.items():
        _w(f"      {cam}: {' '.join(items)}")
    # Which scenes is the cameraman absent from (present for the instructor only)?
    instr_scenes = {c["scene"] for c in clips if c.get("camera") == "instructor"}
    ext_scenes = {c["scene"] for c in clips if c.get("camera") == "external"}
    if "external" not in cameras:
        findings.append(f"{filename}: NO external/cameraman clips at all")
    else:
        missing = sorted(instr_scenes - ext_scenes)
        if missing:
            findings.append(f"{filename}: cameraman absent from scenes: {', '.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id", help="the Ultimate job to diagnose")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    args = parser.parse_args(argv)

    store = JobStore(args.jobs_root)
    if not store.exists(args.job_id):
        parser.error(f"job not found: {args.job_id}")
    job = store.load(args.job_id)
    jd = store.dir(args.job_id)
    findings: list[str] = []

    _w(f"== Ultimate diagnostics: job {args.job_id} ==")
    _w(f"package: {job.package.value}   status: {job.status.value}")
    if job.package.value != "ultimum":
        _w(f"\n⚠ this job's package is {job.package.value!r}, not 'ultimum' — "
           f"the combo/per-camera artifacts below may be absent.")

    _w("\n-- Per-camera scene sets (classification) --")
    for role in CAMERA_ROLES:
        _report_manifest(jd, role, findings)

    _w("\n-- Combo edits (clip selection by camera) --")
    _report_combo_edl(jd, ULTIMUM_EDL_FILES["full_video"], findings)
    _report_combo_edl(jd, ULTIMUM_EDL_FILES["highlights"], findings)

    _w("\n-- Per-camera freefall cuts --")
    for deliverable in ("external_freefall", "chute_libre_selfie"):
        clips = _load_json(jd / ULTIMUM_EDL_FILES[deliverable])
        n = len(clips) if isinstance(clips, list) else 0
        _w(f"  {deliverable:<20} {ULTIMUM_EDL_FILES[deliverable]}: {n} clips")

    _w("\n-- Rendered outputs (A/V sync) --")
    for name in ("full_video", "highlights", "external_freefall", "chute_libre_selfie"):
        path = Path((job.outputs or {}).get(name, jd / f"{name}.mp4"))
        label = _sync_label(path)
        _w(f"  {name:<20} {label}")
        if "DESYNC" in label:
            findings.append(f"{name}.mp4: video/audio desync (video freezes, audio continues)")

    _w("\n-- Photos --")
    index = _load_json(jd / "photos" / "index.json")
    if isinstance(index, list):
        by_scene = Counter(p.get("scene") for p in index)
        _w(f"  index.json: {len(index)} photos  by scene: {dict(by_scene)}")
        if len(index) < 50:
            findings.append(f"photos: only {len(index)} (target is ~50)")
    else:
        _w("  (no photos/index.json)")

    _w("\n== Findings ==")
    if findings:
        for f in findings:
            _w(f"  ⚠ {f}")
    else:
        _w("  none — classification, combo selection, A/V sync and photo count all look healthy.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
