"""Why did a freefall cut open on the plane ride / miss the beat drop? Read-only triage.

The rule (client, 2026-10-22): the freefall cut is only ever shortened at its HEAD so the
aircraft exit lands on the music's beat drop, with at most ``$FREEFALL_MAX_LEAD_S``
(15 s) of plane before the exit. When a delivered freefall video breaks that — "the exit
is at 0:19, the music didn't hit" — there are only three ways it can happen, and this
prints which one it was, from the job's on-disk artifacts and nothing else:

1. **The job pre-dates the rule** (rendered before commits 67408a8/d189bf5). Evidence:
   ``validation_report.json`` carries no ``freefall plays whole`` repair line for the cut,
   and the EDL is the old chopped shape (several freefall windows, a replayed deploy beat).
2. **The exit was detected wrong** — late, early, or not at all (``exit_offset: 0.0``,
   which also means "starts in freefall", so the cut opens at the scene head). Pass the
   real exit as you see it in the delivered video (``--exit-at 0:19``): it is mapped back
   through the EDL to scene time and compared with the detected offset, and the
   accelerometer around BOTH instants is printed so you can see what the detector saw.
3. **The music has no (detected) drop**, or the drop sits past the cap: the lead and the
   music offset the render applied are printed next to the track's drop.

Usage:
    python scripts/diagnose_freefall_sync.py <job_id> [--exit-at 0:19] [--telemetry]
        [--deliverable freefall|external_freefall|chute_libre_selfie] [--jobs-root DIR]

``--telemetry`` adds section 7: the freefall file's whole sensor record, one row per
GPMF payload (accelerometer mean/min/max g, gyro peak, GPS altitude and 3D speed), plus
quarter-second accelerometer/gyro bins around the exit. That is the evidence needed to
tune the exit detector when it misses (a tumbling tandem exit reads ~1 g on average).

Exit status 0 when every check passes, 1 when a finding is reported, 2 when the job's
artifacts are missing.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.selfie import (  # noqa: E402
    _G,
    _ULTIMUM_ROLE_MANIFEST,
    ULTIMUM_EDL_FILES,
    ULTIMUM_FREEFALL_ROLE,
    Clip,
    _exit_output_time,
    _freefall_music_offset,
    _music_paths,
    _ultimum_music_paths,
    detect_exit_offset,
    exclude_freefall_head,
    load_exclusions,
)
from edl.storage import job_dir  # noqa: E402
from render.beatdrop import beat_drop_for, plan_exit_lead  # noqa: E402

try:  # the env-overridable cap landed after the sync rule; a box on the older code has
    from render.beatdrop import freefall_max_lead_s  # noqa: E402
except ImportError:  # only the constant — this file must still run there, as-is
    from render.beatdrop import FREEFALL_MAX_LEAD_S as _CAP  # noqa: E402

    def freefall_max_lead_s() -> float:
        return float(_CAP)

#: Scene-time tolerance (s) between the detected exit and the one seen in the video
#: before it is called a mis-detection. One accelerometer payload is ~1 s.
_EXIT_TOL_S = 2.0
#: Seconds of accelerometer printed either side of an instant.
_PROFILE_HALF_S = 10


def _w(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _parse_time(text: str) -> float:
    """``0:19`` / ``1:03.5`` / ``19`` → seconds."""
    parts = text.strip().split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return 60.0 * float(parts[0]) + float(parts[1])
    raise argparse.ArgumentTypeError(f"not a time: {text!r} (use M:SS or seconds)")


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - a deployed tree may not be a git checkout
        return "(not a git checkout)"


def _out_dur(c: Clip) -> float:
    return (c.src_end - c.src_start) / c.speed_multiplier


def _source_time_at(clips: list[Clip], out_t: float) -> tuple[str, float] | None:
    """``(scene, source seconds)`` playing at output time ``out_t``, or None past the end."""
    t = 0.0
    for c in clips:
        d = _out_dur(c)
        if out_t < t + d:
            return c.scene, c.src_start + (out_t - t) * c.speed_multiplier
        t += d
    return None


def _raw_file(jd: Path, name: str) -> Path | None:
    """The job's staged master (or its LRV proxy) for a scene source file, if still here."""
    stem = Path(name).stem
    candidates = [jd / "raw" / name, *sorted((jd / "raw").glob(f"*/{name}"))]
    candidates += [jd / "raw" / f"{stem}.LRV", *sorted((jd / "raw").glob(f"*/{stem}.LRV"))]
    return next((p for p in candidates if p.is_file()), None)


def _accl_profile(path: Path) -> list[tuple[float, float, float]]:
    """``(t, mean g, min g)`` per accelerometer payload (~1 s), the detector's own view."""
    try:
        from metadata.gpmf import parse_gpmf

        accl = parse_gpmf(str(path)).get("ACCL")
    except Exception as e:  # noqa: BLE001 - no telemetry is itself the finding
        _w(f"      (telemetry unreadable: {e!r})")
        return []
    if accl is None:
        return []
    out = []
    for samples, t in zip(accl.payloads, accl.times, strict=False):
        if not samples:
            continue
        mags = [math.sqrt(sum(c * c for c in s[:3])) / _G for s in samples]
        out.append((float(t), statistics.fmean(mags), min(mags)))
    return out


def _print_profile(
    profile: list[tuple[float, float, float]], around: float, marks: dict[float, str]
) -> None:
    lo, hi = around - _PROFILE_HALF_S, around + _PROFILE_HALF_S
    rows = [r for r in profile if lo <= r[0] <= hi]
    if not rows:
        _w(f"      (no accelerometer payloads in [{lo:.0f}, {hi:.0f}] s)")
        return
    _w("        t(s)   mean g   min g")
    for t, mean, mn in rows:
        tag = "".join(f"   <-- {label}" for at, label in marks.items() if abs(at - t) < 0.5)
        _w(f"      {t:6.1f}   {mean:6.2f}   {mn:5.2f}{tag}")


def _per_sample_times(times: list[float], payloads: list[list[Any]]) -> list[list[float]]:
    """Spread each payload's samples evenly between its start and the next payload's."""
    out = []
    for k, (t, samples) in enumerate(zip(times, payloads, strict=False)):
        nxt = times[k + 1] if k + 1 < len(times) else t + 1.0
        n = max(len(samples), 1)
        out.append([t + (nxt - t) * j / n for j in range(len(samples))])
    return out


def _dump_telemetry(path: Path, name: str, around: float | None) -> None:
    """One row per GPMF payload for the whole file, then 0.25 s bins near the exit."""
    try:
        from metadata.gpmf import parse_gpmf

        data = parse_gpmf(str(path))
    except Exception as e:  # noqa: BLE001
        _w(f"   {name}: telemetry unreadable ({e!r})")
        return
    accl, gyro, gps = data.get("ACCL"), data.get("GYRO"), data.get("GPS5")
    _w(f"   {name}: ACCL={'yes' if accl else 'NO'}  GYRO={'yes' if gyro else 'NO'}  "
       f"GPS5={'yes' if gps else 'NO (no GPS on this camera, no lock, or GPS9-only)'}  "
       f"streams={sorted(data.streams)}")
    if accl is None:
        return

    def _bucket(stream: Any, fn: Any) -> dict[int, list[float]]:
        out: dict[int, list[float]] = {}
        if stream is None:
            return out
        for ts, samples in zip(_per_sample_times(stream.times, stream.payloads),
                               stream.payloads, strict=False):
            for t, s in zip(ts, samples, strict=False):
                v = fn(s)
                if v is not None:
                    out.setdefault(int(t), []).append(v)
        return out

    def _mag_g(s: Any) -> float | None:
        return math.sqrt(sum(c * c for c in s[:3])) / _G if len(s) >= 3 else None

    def _gyro_dps(s: Any) -> float | None:
        return math.degrees(math.sqrt(sum(c * c for c in s[:3]))) if len(s) >= 3 else None

    acc = _bucket(accl, _mag_g)
    gyr = _bucket(gyro, _gyro_dps)
    alt = _bucket(gps, lambda s: s[2] if len(s) >= 3 else None)
    spd = _bucket(gps, lambda s: s[4] if len(s) >= 5 else None)
    _w("     t   acc_mean acc_min acc_max  gyro_peak_dps  gps_alt_m  gps_spd3d")
    for sec in sorted(acc):
        a = acc[sec]
        g = f"{max(gyr[sec]):8.0f}" if sec in gyr else "       -"
        al = f"{statistics.fmean(alt[sec]):9.1f}" if sec in alt else "        -"
        sp = f"{statistics.fmean(spd[sec]):8.1f}" if sec in spd else "       -"
        _w(f"   {sec:4d}   {statistics.fmean(a):6.2f}  {min(a):6.2f}  {max(a):6.2f}"
           f"       {g}  {al}  {sp}")

    lo = (around - 6.0) if around is not None else 0.0
    hi = (around + 10.0) if around is not None else 30.0
    fine_a: dict[float, list[float]] = {}
    fine_g: dict[float, list[float]] = {}
    for stream, fn, sink in ((accl, _mag_g, fine_a), (gyro, _gyro_dps, fine_g)):
        if stream is None:
            continue
        for ts, samples in zip(_per_sample_times(stream.times, stream.payloads),
                               stream.payloads, strict=False):
            for t, smp in zip(ts, samples, strict=False):
                if lo <= t < hi:
                    v = fn(smp)
                    if v is not None:
                        sink.setdefault(math.floor(t * 4) / 4, []).append(v)
    _w(f"   quarter-second bins [{lo:.1f}, {hi:.1f}) s (file time):")
    _w("       t     acc_mean acc_min  gyro_mean_dps")
    for b in sorted(fine_a):
        a = fine_a[b]
        g = f"{statistics.fmean(fine_g[b]):9.0f}" if b in fine_g else "        -"
        _w(f"   {b:7.2f}   {statistics.fmean(a):6.2f}  {min(a):6.2f}     {g}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("job_id")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    parser.add_argument(
        "--deliverable", default="freefall",
        choices=["freefall", *ULTIMUM_FREEFALL_ROLE],
        help="which freefall-only cut to inspect (default: the selfie 'freefall')",
    )
    parser.add_argument(
        "--exit-at", type=_parse_time, default=None, metavar="M:SS",
        help="where the exit really is in the DELIVERED video (e.g. 0:19)",
    )
    parser.add_argument(
        "--telemetry", action="store_true",
        help="dump the freefall file's full accelerometer/gyro/GPS record (section 7)",
    )
    args = parser.parse_args(argv)

    jd = job_dir(args.job_id, args.jobs_root)
    if not jd.is_dir():
        _w(f"no job dir at {jd}")
        return 2
    findings: list[str] = []
    deliverable = args.deliverable

    # --- which files describe this cut ---
    if deliverable == "freefall":
        manifest_name, edl_name = "scene_manifest.json", "edl_freefall.json"
    else:
        role = ULTIMUM_FREEFALL_ROLE[deliverable]
        manifest_name, edl_name = _ULTIMUM_ROLE_MANIFEST[role], ULTIMUM_EDL_FILES[deliverable]
    manifest = _load_json(jd / manifest_name)
    raw_edl = _load_json(jd / edl_name)
    booking = _load_json(jd / "booking.json") or {}
    job = _load_json(jd / "job.json") or {}
    if not isinstance(manifest, dict) or not isinstance(raw_edl, list):
        _w(f"job {args.job_id}: missing {manifest_name} or {edl_name} — not a processed "
           f"scene-pipeline job (or a mixed-ref job: its EDLs are role-prefixed)")
        return 2

    _w(f"job {args.job_id}  ({jd})")
    _w(f"  code here: {_git_head()}   job status: {job.get('status')}   "
       f"package: {job.get('package')}   music: {job.get('music') or booking.get('music')}")
    _w(f"  deliverable: {deliverable}   manifest: {manifest_name}   edl: {edl_name}")

    # --- 1. did the rule run on this job? ---
    report = _load_json(jd / "validation_report.json") or {}
    # The report is written as {"repairs": {deliverable: [...]}} (api.selfie
    # _write_validation_report); older copies of this tool read the top level and
    # always printed "no repair logged".
    repairs = report.get("repairs", report) if isinstance(report, dict) else {}
    lines = repairs.get(deliverable, []) if isinstance(repairs, dict) else []
    whole = [ln for ln in lines if "freefall plays whole" in ln]
    _w()
    _w("1. Continuity rule (commits 67408a8 / d189bf5):")
    if whole:
        _w(f"   ran — {whole[0]}")
    elif lines or report:
        _w("   no 'freefall plays whole' repair logged for this cut — see section 3 for "
           "whether the EDL was already one continuous run (rule in effect) or not.")
    else:
        _w("   no validation_report.json: the job pre-dates deterministic validation")
        findings.append("job rendered before the freefall rules existed — re-process it")

    # --- 2. the freefall scene and its exit ---
    scenes = manifest.get("scenes", [])
    ff = next((s for s in scenes if s.get("name") == "freefall"), None)
    _w()
    _w("2. Freefall scene:")
    if ff is None:
        _w("   none — this job has no freefall scene; the cut is a stand-in")
        findings.append("no freefall scene classified — nothing to sync")
        exit_off = 0.0
        deploy_off = 0.0
    else:
        exit_off = float(ff.get("exit_offset") or 0.0)
        deploy_off = float(ff.get("deploy_offset") or 0.0)
        offsets = ff.get("file_offsets") or [
            {"file": f, "offset": None} for f in ff.get("source_files", [])
        ]
        _w(f"   duration={float(ff.get('duration', 0)):.2f}s  exit_offset={exit_off:.2f}  "
           f"deploy_offset={deploy_off:.2f}")
        for fo in offsets:
            off = fo["offset"]
            shown = f"{off:8.2f}" if off is not None else "       ?"
            _w(f"     {shown} s  {fo['file']}")
        if ff.get("exit_offset") is None:
            findings.append("manifest has no exit_offset — pre-exit-detection job")
        elif exit_off <= 0.0:
            findings.append(
                "exit_offset is 0.0: no exit was detected, so the cut starts at the scene "
                "head (the whole plane ride plays before the exit, music from its drop)"
            )
        for flag in manifest.get("flagged", []):
            if "exit" in flag:
                _w(f"   flagged: {flag}")

    # --- 3. the EDL as rendered ---
    clips = [Clip.model_validate(c) for c in raw_edl]
    exclusions = load_exclusions(args.job_id, args.jobs_root)
    if exclusions:
        clips = exclude_freefall_head(clips, exclusions)
    _w()
    _w(f"3. EDL ({len(clips)} clip(s){', after exclude.json head trim' if exclusions else ''}):")
    prev_end = None
    continuous = True
    for c in clips:
        gap = ""
        if prev_end is not None and c.scene == "freefall":
            if c.src_start > prev_end + 1e-3:
                gap, continuous = f"   SKIPS {c.src_start - prev_end:.2f}s", False
            elif c.src_start < prev_end - 1e-3:
                gap, continuous = f"   REPLAYS {prev_end - c.src_start:.2f}s", False
        _w(f"     {c.scene:<10} [{c.src_start:8.2f} -> {c.src_end:8.2f}]  "
           f"x{c.speed_multiplier:<4}{gap}")
        if c.scene == "freefall":
            prev_end = c.src_end
    if not continuous:
        findings.append("the freefall is cut/replayed mid-jump — the EDL pre-dates the "
                        "continuity rule (or was hand-edited); re-process or replay")
    elif ff is not None and clips and clips[-1].scene == "freefall" and deploy_off > 0 \
            and abs(clips[-1].src_end - (deploy_off + 3.0)) < 0.05:
        _w("   continuity rule in effect: one unbroken run ending at deploy + 3 s")
    ff_clips = [c for c in clips if c.scene == "freefall"]
    head = ff_clips[0].src_start if ff_clips else None
    lead = (exit_off - head) if head is not None else None
    cap = freefall_max_lead_s()
    _w(f"   lead before the DETECTED exit: "
       f"{'n/a' if lead is None else f'{lead:.2f}s'}   cap in force: {cap:.1f}s")
    if lead is not None and lead > cap + 0.05:
        findings.append(f"lead {lead:.1f}s exceeds the {cap:.0f}s cap — this EDL was not "
                        "composed with plan_exit_lead (old code, or hand-edited head)")

    # --- 4. the music ---
    if deliverable == "freefall":
        track = _music_paths(booking, args.job_id, args.jobs_root)["freefall"]
    else:
        track = _ultimum_music_paths(booking, args.job_id, args.jobs_root)[deliverable]
    drop = beat_drop_for(track) if track else None
    exit_t = _exit_output_time(clips, exit_off) if ff is not None else None
    offset = _freefall_music_offset(clips, scenes, track) if ff is not None else 0.0
    _w()
    _w("4. Music:")
    _w(f"   track: {track or '(none resolved — no music on this cut)'}")
    _w(f"   beat drop: {'none detected / pinned to none' if drop is None else f'{drop:.2f}s'}")
    if track is None:
        findings.append("no music resolved for this cut: nothing to sync to")
    elif drop is None:
        findings.append("the track has no detected drop: the cut starts at the exit and the "
                        "music plays from 0 (pin one with a <track>.json sidecar)")
    else:
        planned = plan_exit_lead(drop, exit_off, cap)
        _w(f"   planned lead for this track/exit/cap: {planned:.2f}s")
        if lead is not None and abs(lead - planned) > 0.05:
            findings.append(f"EDL lead {lead:.2f}s != planned {planned:.2f}s (drop {drop:.1f}, "
                            f"footage before exit {exit_off:.1f}, cap {cap:.0f})")
    if exit_t is None:
        _w("   detected exit is not inside the cut — nothing to sync")
    else:
        _w(f"   detected exit plays at output {_mmss(exit_t)} ({exit_t:.2f}s); music offset "
           f"{offset:+.2f}s → the drop plays at output "
           f"{'n/a' if drop is None else _mmss(drop - offset)}")

    # --- 5. the real exit, if the reporter gave it ---
    marks: dict[float, str] = {}
    real_scene_t: float | None = None
    if args.exit_at is not None:
        _w()
        _w(f"5. Real exit at output {_mmss(args.exit_at)} (from the delivered video):")
        hit = _source_time_at(clips, args.exit_at)
        if hit is None or hit[0] != "freefall":
            _w("   that output time is not inside a freefall clip of this EDL")
        else:
            real_scene_t = hit[1]
            delta = exit_off - real_scene_t
            if exit_off <= 0.0:
                _w(f"   = freefall scene time {real_scene_t:.2f}s; the detector found NO exit "
                   f"(0.0), so the cut opened {real_scene_t:.1f}s of scene before it")
            else:
                _w(f"   = freefall scene time {real_scene_t:.2f}s; detected exit_offset "
                   f"{exit_off:.2f}s → detector is {delta:+.2f}s "
                   f"({'late' if delta > 0 else 'early'} / 0 = exact)")
            if exit_off <= 0.0:
                pass  # already reported in section 2
            elif abs(delta) > _EXIT_TOL_S:
                findings.append(
                    f"exit detected {abs(delta):.1f}s {'LATE' if delta > 0 else 'EARLY'} "
                    f"(scene {exit_off:.1f}s vs real {real_scene_t:.1f}s) — the lead was "
                    "measured from the wrong instant"
                )
            if ff is not None and drop is not None and exit_t is not None:
                real_out = args.exit_at
                _w(f"   music drop lands at output {_mmss(drop - offset)}; the real exit is at "
                   f"{_mmss(real_out)} → off by {(drop - offset) - real_out:+.2f}s")

    # --- 6. what the detector saw ---
    if ff is not None:
        _w()
        _w("6. Accelerometer (per ~1 s payload) in the scene's source files:")
        offsets = ff.get("file_offsets") or []
        for fo in offsets:
            path = _raw_file(jd, fo["file"])
            base = float(fo["offset"])
            _w(f"   {fo['file']} @ scene offset {base:.2f}s: "
               f"{path if path else '(not on disk — raw/ pruned; nothing to re-read)'}")
            if path is None:
                continue
            redetect = detect_exit_offset(path)
            _w(f"      detect_exit_offset now → {redetect:.2f}s in file "
               f"({'no exit in this file' if redetect <= 0 else f'scene {base + redetect:.2f}s'})")
            profile = _accl_profile(path)
            if not profile:
                _w("      (no ACCL telemetry — an LRV/transcode without gpmd? the detector "
                   "returns 0.0 on this)")
                continue
            marks = {}
            if exit_off > 0 and 0 <= exit_off - base <= (profile[-1][0] + 1):
                marks[exit_off - base] = "detected exit"
            if real_scene_t is not None and 0 <= real_scene_t - base <= (profile[-1][0] + 1):
                marks[real_scene_t - base] = "REAL exit"
            for at in sorted(marks) or [min(exit_off - base, profile[-1][0])]:
                _w(f"      around file time {at:.1f}s:")
                _print_profile(profile, at, marks)

    if args.telemetry and ff is not None:
        _w()
        _w("7. Telemetry (detector tuning evidence):")
        for fo in ff.get("file_offsets") or []:
            path = _raw_file(jd, fo["file"])
            if path is None:
                _w(f"   {fo['file']}: not on disk")
                continue
            base = float(fo["offset"])
            around = (real_scene_t - base) if real_scene_t is not None else None
            _dump_telemetry(path, fo["file"], around)

    _w()
    if findings:
        _w("FINDINGS:")
        for f in findings:
            _w(f"  - {f}")
        return 1
    _w("OK: the cut leads the detected exit within the cap and the drop lands on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
