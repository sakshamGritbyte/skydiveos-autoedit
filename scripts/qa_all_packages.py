#!/usr/bin/env python3
"""Stage-by-stage QA of all five packages against the LIVE stack — no GoPro needed.

``demo_full_auto.py`` drives ONE jump and shows the customer-facing result. This is the
pre-demo audit: it runs *every* package through the same live HTTP path and then opens
the job's working dir to check that **each pipeline stage actually produced what it
should**, so a green run means more than "status: delivered".

Per job it checks, in pipeline order:

===============  ====================================================================
ingest           every uploaded master landed in ``jobs/<id>/raw/`` (per role for ultimum)
segment          ``scene_manifest*.json`` — scenes found, freefall present with
                 exit/deploy offsets, landing milestone, ``flagged`` surfaced
score            ``scores*.json`` — per-second rows for the freefall window
compose          one ``edl_*.json`` per video deliverable, each with clips
validate         ``validation_report.json`` — ``validate_and_repair`` ran; repairs listed
render           every expected output exists, plays, and its video/audio stream
                 durations agree (the "video freezes, audio continues" desync)
photos           photo count in the package's expected band + ``photos.zip``
review           reached ``delivered`` with no manual approve (AUTO_DELIVER)
deliver          ``delivery_links`` present and every presigned URL answers 200
archive          ``raw-storage/<date>/<instructor>/<customer>/`` mirrored, manifest owns the job
api              ``/deliverables``, ``/photos`` and a deliverable stream all answer
===============  ====================================================================

Footage: pass your own, or let it reuse the real masters already sitting in ``jobs/*/raw``
from an earlier run (that is the point — this needs no camera).

Usage::

    # audit everything with auto-detected footage
    python scripts/qa_all_packages.py --email you@example.com

    # one package, your own footage
    python scripts/qa_all_packages.py --packages ultimum \\
      --instructor-cam a.MP4 --external-cam b.MP4 c.MP4

    # links only, no customer email
    python scripts/qa_all_packages.py --no-email

Exit code is 0 only when every checked stage of every package passed, so it works as a
pre-meeting smoke test. A per-run report lands in ``qa-report.json`` / ``qa-report.md``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: Statuses that mean the pipeline is finished with this job, one way or another.
_DONE = {"ready", "ready_for_review", "delivered", "failed", "rejected"}


def _auth_headers() -> dict[str, str]:
    """The service token this API requires (``AUTO_EDIT_API_KEY``), or ``{}``.

    Same header SkydiveOS sends. Keeps this driver working once the token gate is
    on, and stays a no-op on a deployment that hasn't enabled it yet.
    """
    try:
        from api.auth import service_auth_headers

        return service_auth_headers()
    except Exception:  # noqa: BLE001 - a demo/QA driver must not die on config import
        return {}


def _jobs_root() -> Path:
    """Where the pipeline actually keeps job dirs — NOT always ``<repo>/jobs``.

    Under Docker the volume is mounted at ``/data/jobs`` (``JOBS_ROOT``), so a
    repo-relative guess finds nothing and every audit fails with a confusing
    "missing artifact" instead of "wrong directory". Resolve it the same way the
    pipeline does, and fall back to the repo for a plain checkout.
    """
    try:
        from api.config import get_settings

        configured = get_settings().jobs_root
        if configured:
            return Path(configured)
    except Exception:  # noqa: BLE001 - a diagnostic must not die on config import
        pass
    return _REPO_ROOT / "jobs"


#: What each package must emit, keyed by package name.
_EXPECTED_OUTPUTS: dict[str, tuple[str, ...]] = {
    "selfie": ("full_video", "highlights", "freefall", "photos"),
    "external": ("full_video", "highlights", "freefall", "photos"),
    "video_only": ("full_video", "highlights", "freefall"),
    "photo_only": ("photos",),
    "ultimum": ("full_video", "highlights", "external_freefall", "chute_libre_selfie", "photos"),
}

#: EDL sidecar written for each video deliverable (see ``api.selfie``).
_EDL_FILES: dict[str, str] = {
    "full_video": "edl_full.json",
    "highlights": "edl_highlights.json",
    "freefall": "edl_freefall.json",
    "external_freefall": "edl_external_freefall.json",
    "chute_libre_selfie": "edl_chute_libre.json",
}

#: Inclusive (min, max) photo count each photo-producing package should land in, from
#: the targets the extractor actually aims for (``api.selfie.SELFIE_PHOTO_TARGET`` = 50,
#: ``PHOTO_ONLY_TARGET`` = 140). Bands are wide because the achievable count depends on
#: how much usable footage the jump has; ``ultimum`` extracts over BOTH cameras.
_PHOTO_BAND: dict[str, tuple[int, int]] = {
    "selfie": (35, 60),
    "external": (35, 60),
    "photo_only": (110, 150),
    "ultimum": (35, 150),
}

#: Milestones a well-segmented jump should contain (landing may be named canopy).
_REQUIRED_SCENES = ("freefall",)

#: Largest tolerated video-vs-audio stream duration gap, in seconds.
_MAX_DESYNC_S = 1.0


@dataclass
class Check:
    """One stage assertion and how it went."""

    stage: str
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PackageRun:
    """Everything observed for one package's run."""

    package: str
    job_id: str = ""
    status: str = ""
    error: str | None = None
    elapsed_s: float = 0.0
    checks: list[Check] = field(default_factory=list)

    def add(self, stage: str, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(stage, name, ok, detail))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and not self.failures


# --------------------------------------------------------------------------- helpers


def _fmt(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _stream_durations(path: Path) -> dict[str, float]:
    """Return ``{codec_type: duration}`` for a media file, via ffprobe."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,duration", "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=120, check=False,
        ).stdout
        streams = json.loads(out).get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    durations: dict[str, float] = {}
    for stream in streams:
        kind = str(stream.get("codec_type") or "")
        try:
            durations[kind] = float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
    return durations


def _masters(directory: Path) -> list[Path]:
    """The MP4 masters in a directory, case-insensitively.

    Linux is case-sensitive where macOS is not, so a ``*.MP4`` glob silently finds
    nothing on the EC2 box for footage saved as ``.mp4`` — reporting "no footage" for a
    jobs volume that is full of it. Matches how the pipeline itself tests extensions
    (``JobStore.camera_roles_present``).
    """
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*") if p.is_file() and p.suffix.lower() == ".mp4"
    )


def _find_default_footage() -> tuple[list[Path], list[Path], list[Path]]:
    """Reuse real masters from earlier jobs: (single-cam, instructor-cam, external-cam)."""
    jobs_root = _jobs_root()
    single: list[Path] = []
    instructor: list[Path] = []
    external: list[Path] = []
    for raw in sorted(jobs_root.glob("*/raw")):
        flat = _masters(raw)
        if flat and len(flat) > len(single):
            single = flat
        inst = _masters(raw / "instructor")
        ext = _masters(raw / "external")
        if inst and ext and (len(inst) + len(ext)) > (len(instructor) + len(external)):
            instructor, external = inst, ext
    if not single and instructor:
        single = instructor
    return single, instructor, external


def _existing(paths: list[str], what: str) -> list[Path]:
    resolved = [Path(p) for p in paths]
    missing = [str(p) for p in resolved if not p.is_file()]
    if missing:
        raise SystemExit(f"error: {what} not found: {', '.join(missing)}")
    return resolved


# ----------------------------------------------------------------------- stage audits


def _audit_ingest(run: PackageRun, jd: Path, cameras: list[tuple[str | None, list[Path]]]) -> None:
    for role, paths in cameras:
        tag = f" [{role}]" if role else ""
        raw = jd / "raw" / role if role else jd / "raw"
        landed = {p.name for p in raw.glob("*")} if raw.is_dir() else set()
        missing = sorted(p.name for p in paths if p.name not in landed)
        masters = sorted(n for n in landed if n.lower().endswith(".mp4"))
        proxies = sorted(n for n in landed if n.lower().endswith(".lrv"))
        run.add(
            "ingest", f"raw staged{tag}", not missing,
            f"{len(masters)} master(s) + {len(proxies)} proxy(s) in "
            f"{raw.relative_to(_REPO_ROOT)}" + (f"; MISSING {missing}" if missing else ""),
        )
        # LRV proxies make analysis read the low-res copy instead of the 4K master
        # (USE_PROXY_ANALYSIS) — ~95% less compute. Their absence is legal, not a fault.
        run.add(
            "ingest", f"LRV proxies present{tag}", True,
            f"{len(proxies)}/{len(masters)} masters have a proxy — analysis runs on LRV"
            if proxies else "none uploaded — analysis falls back to the full MP4 (slower)",
        )


def _audit_segment(run: PackageRun, jd: Path, roles: list[str]) -> None:
    manifests = (
        [(role, jd / f"scene_manifest_{role}.json") for role in roles]
        if roles else [(None, jd / "scene_manifest.json")]
    )
    for role, path in manifests:
        tag = f" [{role}]" if role else ""
        manifest = _load_json(path)
        if not manifest:
            run.add("segment", f"scene manifest{tag}", False, f"missing/unreadable {path.name}")
            continue
        scenes = manifest.get("scenes") or []
        names = [str(s.get("name")) for s in scenes]
        run.add("segment", f"scenes found{tag}", len(scenes) >= 2, f"{len(scenes)}: {names}")

        missing = [n for n in _REQUIRED_SCENES if n not in names]
        run.add("segment", f"milestone scenes{tag}", not missing,
                f"missing {missing}" if missing else "freefall present")

        freefall = next((s for s in scenes if s.get("name") == "freefall"), None)
        if freefall is None:
            run.add("segment", f"exit/deploy offsets{tag}", False, "no freefall scene")
        else:
            exit_off = freefall.get("exit_offset")
            deploy_off = freefall.get("deploy_offset")
            ok = (
                isinstance(exit_off, int | float)
                and isinstance(deploy_off, int | float)
                and deploy_off > exit_off
            )
            run.add("segment", f"exit/deploy offsets{tag}", ok,
                    f"exit={exit_off} deploy={deploy_off} "
                    f"(freefall {round(float(deploy_off) - float(exit_off), 1)}s)"
                    if ok else f"exit={exit_off} deploy={deploy_off}")

        # Informational, never a failure: whether a post-freefall scene carries the
        # touchdown accl signature is a property of the FOOTAGE (a jump whose landing
        # ran past the last clip legitimately has none), not of the pipeline.
        canopy_z = [
            s.get("gpmf_signals", {}).get("accl_z_mean")
            for s in scenes if s.get("name") in {"canopy", "landing"}
        ]
        run.add("segment", f"landing milestone{tag}", True,
                "landing scene present" if "landing" in names
                else f"no landing rename (canopy accl_z_mean={canopy_z}, needs >1.5) — "
                     "expected unless the touchdown is in frame")

        flagged = manifest.get("flagged") or []
        run.add("segment", f"flagged for review{tag}", True,
                f"{len(flagged)} flag(s): {flagged}" if flagged else "none")

        offsets_ok = all(s.get("file_offsets") for s in scenes)
        run.add("segment", f"file_offsets recorded{tag}", offsets_ok,
                "every scene maps back to its source files" if offsets_ok
                else "some scenes have no file_offsets (boarding rule can't target a file)")


def _audit_score(run: PackageRun, jd: Path, roles: list[str]) -> None:
    files = (
        [(role, jd / f"scores_{role}.json") for role in roles]
        if roles else [(None, jd / "scores.json")]
    )
    for role, path in files:
        tag = f" [{role}]" if role else ""
        scores = _load_json(path)
        rows = sum(len(v) for v in scores.values()) if isinstance(scores, dict) else 0
        run.add("score", f"face scores{tag}", rows > 0,
                f"{rows} scored second(s) across {len(scores or {})} scene(s)"
                if rows else f"no rows in {path.name}")


def _audit_compose(run: PackageRun, jd: Path, package: str) -> None:
    videos = [d for d in _EXPECTED_OUTPUTS[package] if d != "photos"]
    for deliverable in videos:
        path = jd / _EDL_FILES[deliverable]
        clips = _load_json(path)
        count = len(clips) if isinstance(clips, list) else 0
        run.add("compose", f"EDL {deliverable}", count > 0,
                f"{count} clip(s) in {path.name}" if count else f"missing/empty {path.name}")
        if count and isinstance(clips, list):
            ordered = all(
                float(a.get("src_start", 0)) <= float(a.get("src_end", 0)) for a in clips
            )
            run.add("compose", f"EDL {deliverable} sane", ordered,
                    "every clip has src_start <= src_end" if ordered
                    else "a clip has src_end before src_start")


def _audit_validate(run: PackageRun, jd: Path, package: str) -> None:
    if not [d for d in _EXPECTED_OUTPUTS[package] if d != "photos"]:
        # No video deliverable → no EDL is composed → nothing for the validator to do.
        run.add("validate", "validation report", True, f"n/a for {package} (no video EDLs)")
        return
    report = _load_json(jd / "validation_report.json")
    if report is None:
        run.add("validate", "validation report", False,
                "no validation_report.json — validate_and_repair did not run")
        return
    repairs = report.get("repairs", report) if isinstance(report, dict) else {}
    total = sum(len(v) for v in repairs.values()) if isinstance(repairs, dict) else 0
    run.add("validate", "validation report", True,
            f"{total} repair(s) across {len(repairs)} deliverable(s)"
            + (f": {json.dumps(repairs)[:400]}" if total else " (EDL needed no repair)"))


def _audit_render(run: PackageRun, package: str, outputs: dict[str, str]) -> None:
    expected = _EXPECTED_OUTPUTS[package]
    missing = [d for d in expected if d not in outputs]
    run.add("render", "deliverable set", not missing,
            f"got {sorted(outputs)}" + (f"; MISSING {missing}" if missing else ""))
    extra = [d for d in outputs if d not in expected]
    if extra:
        run.add("render", "no stray deliverables", False, f"unexpected {extra}")

    for name in expected:
        if name == "photos" or name not in outputs:
            continue
        path = Path(outputs[name])
        if not path.is_absolute():
            path = _REPO_ROOT / path
        if not path.is_file() or path.stat().st_size == 0:
            run.add("render", f"{name} rendered", False, f"missing/empty {path}")
            continue
        size_mb = path.stat().st_size / 1e6
        durations = _stream_durations(path)
        video, audio = durations.get("video"), durations.get("audio")
        run.add("render", f"{name} rendered", bool(video and video > 1.0),
                f"{size_mb:,.0f} MB, video {video}s, audio {audio}s")
        if video and audio:
            gap = abs(video - audio)
            run.add("render", f"{name} A/V sync", gap <= _MAX_DESYNC_S,
                    f"video/audio differ by {gap:.2f}s"
                    + ("" if gap <= _MAX_DESYNC_S else " — freeze/desync risk"))
        elif video and not audio:
            run.add("render", f"{name} has audio", False, "no audio stream (music never mixed?)")


def _audit_photos(run: PackageRun, package: str, jd: Path, outputs: dict[str, str]) -> None:
    if "photos" not in _EXPECTED_OUTPUTS[package]:
        run.add("photos", "no photos expected", "photos" not in outputs,
                "correctly absent" if "photos" not in outputs else "photos emitted anyway")
        return
    photos_dir = jd / "photos"
    stills = sorted(photos_dir.glob("*.jpg")) if photos_dir.is_dir() else []
    low, high = _PHOTO_BAND[package]
    run.add("photos", "photo count", low <= len(stills) <= high,
            f"{len(stills)} stills (expected {low}–{high})")
    if stills:
        empty = [p.name for p in stills if p.stat().st_size == 0]
        run.add("photos", "photos non-empty", not empty, f"{len(empty)} zero-byte" if empty else "")
    zipped = jd / "photos.zip"
    run.add("photos", "photos.zip", zipped.is_file(),
            f"{zipped.stat().st_size / 1e6:,.1f} MB" if zipped.is_file() else "not zipped")


def _audit_delivery(
    run: PackageRun,
    client: Any,
    job: dict[str, Any],
    want_email: bool,
    *,
    review_gate: bool = False,
) -> None:
    status = str(job.get("status"))
    links = job.get("delivery_links") or {}
    if review_gate:
        # AUTO_DELIVER=0: the instructor gate must HOLD — rendered, but not delivered
        # and not a single customer link generated.
        run.add("review", "held at review gate", status == "ready",
                f"status={status}" + ("" if status == "ready" else " — expected 'ready'"))
        run.add("deliver", "no links before approval", not links,
                "nothing delivered, as expected" if not links else f"LEAKED links: {sorted(links)}")
        return
    run.add("review", "auto-approved (no manual gate)", status == "delivered",
            f"status={status}" + ("" if status == "delivered" else " — AUTO_DELIVER off or failed"))
    if not want_email and status != "delivered":
        return
    run.add("deliver", "delivery links", bool(links), f"{len(links)} link(s): {sorted(links)}")
    if not links:
        return
    run.add("deliver", "gallery link", "gallery" in links, links.get("gallery", "")[:120])
    for name, url in links.items():
        try:
            resp = client.head(url, timeout=30.0, follow_redirects=True)
            code = resp.status_code
            if code >= 400:  # some presigners reject HEAD; confirm with a ranged GET
                resp = client.get(url, timeout=30.0, follow_redirects=True,
                                  headers={"Range": "bytes=0-1023"})
                code = resp.status_code
        except Exception as exc:  # noqa: BLE001 — a broken link is the finding
            run.add("deliver", f"link {name} opens", False, f"{type(exc).__name__}: {exc}")
            continue
        run.add("deliver", f"link {name} opens", code < 400, f"HTTP {code}")


def _audit_archive(run: PackageRun, job: dict[str, Any], package: str) -> None:
    from api.archive import archive_root, jump_dir_parts
    from api.config import get_settings
    from api.jobs import Job

    root = archive_root(get_settings())
    if root is None:
        run.add("archive", "archive enabled", False, "ARCHIVE_ENABLED=0")
        return
    day, instructor, customer = jump_dir_parts(
        Job(
            job_id=str(job["job_id"]),
            customer_name=str(job.get("customer_name") or ""),
            instructor_name=job.get("instructor_name"),
            instructor_id=job.get("instructor_id"),
            jump_date=job.get("jump_date"),
        )
    )
    base = root / day / instructor
    jump_dir = None
    for candidate in sorted(p for p in base.glob(f"{customer}*") if p.is_dir()):
        manifest = _load_json(candidate / "manifest.json") or {}
        if manifest.get("job_id") == job["job_id"]:
            jump_dir = candidate
            break
    if jump_dir is None:
        run.add("archive", "jump folder", False, f"no folder under {base} owns this job")
        return
    run.add("archive", "jump folder", True, str(jump_dir.relative_to(root)))

    raw_files = [p for p in (jump_dir / "raw").rglob("*") if p.is_file()]
    run.add("archive", "raw mirrored", bool(raw_files), f"{len(raw_files)} file(s)")
    edited = [p for p in (jump_dir / "edited").rglob("*") if p.is_file()]
    wants_video = any(d != "photos" for d in _EXPECTED_OUTPUTS[package])
    run.add("archive", "edited mirrored", bool(edited) or not wants_video, f"{len(edited)} file(s)")
    if "photos" in _EXPECTED_OUTPUTS[package]:
        shots = [p for p in (jump_dir / "photos").rglob("*") if p.is_file()]
        run.add("archive", "photos mirrored", bool(shots), f"{len(shots)} file(s)")
    manifest = _load_json(jump_dir / "manifest.json") or {}
    run.add("archive", "manifest complete", bool(manifest.get("files") or manifest.get("status")),
            f"keys={sorted(manifest)}")


def _audit_api(run: PackageRun, client: Any, api: str, job_id: str, package: str) -> None:
    resp = client.get(f"{api}/jobs/{job_id}/deliverables", timeout=30.0)
    body = resp.json() if resp.status_code < 400 else {}
    listed = [d["name"] for d in body.get("deliverables", [])]
    expected = set(_EXPECTED_OUTPUTS[package])
    run.add("api", "GET /deliverables", set(listed) == expected,
            f"HTTP {resp.status_code}, lists {sorted(listed)}")

    videos = [d for d in _EXPECTED_OUTPUTS[package] if d != "photos"]
    if videos:
        stream = client.get(f"{api}/jobs/{job_id}/deliverables/{videos[0]}",
                            headers={"Range": "bytes=0-2047"}, timeout=60.0)
        run.add("api", f"stream {videos[0]}", stream.status_code < 400,
                f"HTTP {stream.status_code}")
    if "photos" in expected:
        photos = client.get(f"{api}/jobs/{job_id}/photos", timeout=30.0)
        count = photos.json().get("count", 0) if photos.status_code < 400 else 0
        run.add("api", "GET /photos", count > 0, f"HTTP {photos.status_code}, count={count}")


# ------------------------------------------------------------------------- the runner


def _upload(client: Any, api: str, job_id: str, paths: list[Path], role: str | None) -> None:
    handles = [p.open("rb") for p in paths]
    try:
        files = [("files", (p.name, fh, "video/mp4")) for p, fh in zip(paths, handles, strict=True)]
        data = {"camera_role": role} if role else None
        resp = client.post(f"{api}/jobs/{job_id}/upload", files=files, data=data, timeout=None)
    finally:
        for fh in handles:
            fh.close()
    if resp.status_code >= 400:
        raise RuntimeError(f"upload -> {resp.status_code} {resp.text}")


def _run_package(
    client: Any,
    api: str,
    package: str,
    cameras: list[tuple[str | None, list[Path]]],
    args: argparse.Namespace,
) -> PackageRun:
    """Create, feed, poll and then audit one package's job."""
    run = PackageRun(package=package)
    booking: dict[str, object] = {
        "customer_name": f"{args.customer} {package}",
        "package": package,
        "instructor_name": args.instructor,
    }
    if args.email:
        booking["customer_email"] = args.email
    if args.music:
        booking["music"] = args.music

    started = time.monotonic()
    resp = client.post(f"{api}/jobs", json=booking, timeout=30.0)
    if resp.status_code >= 400:
        run.add("create", "POST /jobs", False, f"HTTP {resp.status_code} {resp.text[:200]}")
        return run
    run.job_id = str(resp.json()["job_id"])
    run.add("create", "POST /jobs", True, f"job {run.job_id}")
    print(f"    job {run.job_id}")

    total_mb = sum(p.stat().st_size for _, paths in cameras for p in paths) / 1e6
    print(f"    uploading {total_mb:,.0f} MB …", flush=True)
    for index, (role, paths) in enumerate(cameras):
        try:
            _upload(client, api, run.job_id, paths, role)
        except RuntimeError as exc:
            run.add("ingest", f"upload{f' [{role}]' if role else ''}", False, str(exc))
            return run
        run.add("ingest", f"upload{f' [{role}]' if role else ''}", True, f"{len(paths)} file(s)")
        if package == "ultimum" and index == 0:
            job = client.get(f"{api}/jobs/{run.job_id}", timeout=30.0).json()
            waiting = str(job.get("status")) in {"queued", "awaiting_second_camera"}
            run.add("ingest", "ultimum waits for 2nd camera", waiting,
                    f"status after camera 1 = {job.get('status')}")

    print("    processing", end="", flush=True)
    job: dict[str, Any] = {}
    while True:
        elapsed = time.monotonic() - started
        job = client.get(f"{api}/jobs/{run.job_id}", timeout=30.0).json()
        run.status = str(job.get("status"))
        if run.status in _DONE:
            break
        if elapsed > args.timeout:
            run.add("pipeline", "finished in time", False,
                    f"still {run.status} after {_fmt(elapsed)}")
            return run
        print(".", end="", flush=True)
        time.sleep(args.poll)
    run.elapsed_s = time.monotonic() - started
    run.error = job.get("error") or job.get("reject_reason")
    print(f" {run.status} in {_fmt(run.elapsed_s)}")

    if run.status in {"failed", "rejected"}:
        run.add("pipeline", "job succeeded", False, f"{run.status}: {run.error}")
        return run
    run.add("pipeline", "job succeeded", True, f"{run.status} in {_fmt(run.elapsed_s)}")

    _audit_all(run, client, api, package, job, cameras,
               want_email=bool(args.email), review_gate=args.review_gate)
    return run


def _audit_all(
    run: PackageRun,
    client: Any,
    api: str,
    package: str,
    job: dict[str, Any],
    cameras: list[tuple[str | None, list[Path]]],
    *,
    want_email: bool,
    review_gate: bool = False,
) -> None:
    """Run every stage audit against a finished job's working dir + API responses."""
    jd = _jobs_root() / run.job_id
    roles = ["instructor", "external"] if package == "ultimum" else []
    outputs = {k: str(v) for k, v in (job.get("outputs") or {}).items()}

    _audit_ingest(run, jd, cameras)
    _audit_segment(run, jd, roles)
    _audit_score(run, jd, roles)
    _audit_compose(run, jd, package)
    _audit_validate(run, jd, package)
    _audit_render(run, package, outputs)
    _audit_photos(run, package, jd, outputs)
    _audit_delivery(run, client, job, want_email=want_email, review_gate=review_gate)
    _audit_archive(run, job, package)
    _audit_api(run, client, api, run.job_id, package)


def _audit_existing(client: Any, api: str, job_id: str) -> PackageRun:
    """Audit a job that already ran — same stage checks, no create/upload/poll.

    Lets a finished (or failed) job be graded after the fact: re-run the audit on
    yesterday's job, or grade a sweep whose driver was interrupted.
    """
    resp = client.get(f"{api}/jobs/{job_id}", timeout=30.0)
    if resp.status_code >= 400:
        run = PackageRun(package="?", job_id=job_id)
        run.add("create", "GET /jobs/{id}", False, f"HTTP {resp.status_code}")
        return run
    job = resp.json()
    package = str(job.get("package"))
    run = PackageRun(package=package, job_id=job_id, status=str(job.get("status")))
    run.error = job.get("error") or job.get("reject_reason")
    run.elapsed_s = float(job.get("updated_at", 0.0)) - float(job.get("created_at", 0.0))
    print(f"    {package} {job_id} — {run.status}")

    if package not in _EXPECTED_OUTPUTS:
        run.add("create", "known package", False, f"unknown package {package!r}")
        return run
    if run.status in {"failed", "rejected"}:
        run.add("pipeline", "job succeeded", False, f"{run.status}: {run.error}")
        return run
    run.add("pipeline", "job succeeded", True, f"{run.status} in {_fmt(run.elapsed_s)}")

    # Rebuild the "what was uploaded" view from the raw staging the job actually has,
    # so the ingest audit still reports per-camera counts.
    jd = _jobs_root() / job_id
    cameras: list[tuple[str | None, list[Path]]] = []
    if package == "ultimum":
        for role in ("instructor", "external"):
            cameras.append((role, _masters(jd / "raw" / role)))
    else:
        cameras.append((None, _masters(jd / "raw")))

    _audit_all(run, client, api, package, job, cameras,
               want_email=bool(job.get("customer_email")))
    return run


def _report(runs: list[PackageRun], out_dir: Path) -> bool:
    stages = ["create", "ingest", "pipeline", "segment", "score", "compose",
              "validate", "render", "photos", "review", "deliver", "archive", "api"]
    width = max(len(r.package) for r in runs) + 2

    print("\n" + "=" * 78)
    print("STAGE MATRIX".center(78))
    print("=" * 78)
    header = "package".ljust(width) + "".join(s[:6].ljust(8) for s in stages)
    print(header)
    for run in runs:
        row = run.package.ljust(width)
        for stage in stages:
            checks = [c for c in run.checks if c.stage == stage]
            mark = "–" if not checks else ("PASS" if all(c.ok for c in checks) else "FAIL")
            row += mark.ljust(8)
        print(row)

    print("\n" + "=" * 78)
    for run in runs:
        head = f"{run.package}  [{run.job_id or 'no job'}]  {run.status or '—'}"
        print(f"\n{head}\n{'-' * len(head)}")
        for check in run.checks:
            flag = "✓" if check.ok else "✗"
            print(f"  {flag} {check.stage:<9} {check.name:<34} {check.detail}")
        if run.failures:
            print(f"  → {len(run.failures)} FAILING CHECK(S)")

    ok = all(r.ok for r in runs)
    print("\n" + "=" * 78)
    print("RESULT:", "ALL PACKAGES PASS" if ok else
          "FAILURES in " + ", ".join(r.package for r in runs if not r.ok))
    print("=" * 78)

    payload = [
        {
            "package": r.package, "job_id": r.job_id, "status": r.status,
            "error": r.error, "elapsed_s": round(r.elapsed_s, 1), "ok": r.ok,
            "checks": [vars(c) for c in r.checks],
        }
        for r in runs
    ]
    (out_dir / "qa-report.json").write_text(json.dumps(payload, indent=2))
    lines = ["# Auto-Edit QA report", ""]
    for r in runs:
        lines += [f"## {r.package} — {'PASS' if r.ok else 'FAIL'}",
                  f"job `{r.job_id}` · status `{r.status}` · {_fmt(r.elapsed_s)}", ""]
        lines += [f"- {'✓' if c.ok else '✗'} **{c.stage}** {c.name} — {c.detail}" for c in r.checks]
        lines.append("")
    (out_dir / "qa-report.md").write_text("\n".join(lines))
    print(f"\nreports: {out_dir / 'qa-report.json'}  and  {out_dir / 'qa-report.md'}")
    return ok


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/qa_all_packages.py",
        description="Stage-by-stage QA of every package against the live stack (no GoPro).",
    )
    parser.add_argument("--api", default="http://localhost:8000", help="auto-edit API base URL")
    parser.add_argument("--packages", default="selfie,external,video_only,photo_only,ultimum",
                        help="comma-separated packages to run, in order")
    parser.add_argument("--audit-job", nargs="+", default=[], metavar="JOB_ID",
                        help="audit job(s) that already ran (no create/upload) and exit")
    parser.add_argument("--footage", nargs="+", default=[],
                        help="single-camera masters (default: reuse masters from jobs/*/raw)")
    parser.add_argument("--instructor-cam", nargs="+", default=[],
                        help="ultimum: selfie-cam masters")
    parser.add_argument("--external-cam", nargs="+", default=[], help="ultimum: cameraman masters")
    parser.add_argument("--customer", default="QA", help="customer-name prefix for the test jobs")
    parser.add_argument("--instructor", default="QA Instructor", help="instructor name")
    parser.add_argument("--email", default=None, help="deliver to this address (real email!)")
    parser.add_argument("--no-email", action="store_true", help="skip email; links only")
    parser.add_argument("--review-gate", action="store_true",
                        help="expect AUTO_DELIVER=0 behaviour: job holds at 'ready', no links")
    parser.add_argument("--music", default=None, help="track stem from templates/music")
    parser.add_argument("--poll", type=float, default=15.0, help="seconds between status polls")
    parser.add_argument("--timeout", type=float, default=5400.0, help="per-package give-up seconds")
    args = parser.parse_args(argv)
    if args.no_email:
        args.email = None
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    api = args.api.rstrip("/")

    import httpx

    if args.audit_job:
        print(f"API        : {api}\nauditing   : {len(args.audit_job)} existing job(s)")
        with httpx.Client(headers=_auth_headers()) as client:
            runs = [_audit_existing(client, api, jid) for jid in args.audit_job]
        return 0 if _report(runs, _REPO_ROOT) else 1

    packages = [p.strip() for p in args.packages.split(",") if p.strip()]
    unknown = [p for p in packages if p not in _EXPECTED_OUTPUTS]
    if unknown:
        raise SystemExit(f"error: unknown package(s) {unknown}")

    auto_single, auto_inst, auto_ext = _find_default_footage()
    single = _existing(args.footage, "footage") if args.footage else auto_single
    instructor = (
        _existing(args.instructor_cam, "instructor-cam") if args.instructor_cam else auto_inst
    )
    external = _existing(args.external_cam, "external-cam") if args.external_cam else auto_ext

    if any(p != "ultimum" for p in packages) and not single:
        raise SystemExit("error: no single-camera footage found — pass --footage")
    if "ultimum" in packages and not (instructor and external):
        raise SystemExit(
            "error: no two-camera footage found — pass --instructor-cam/--external-cam"
        )

    print(f"API        : {api}")
    print(f"packages   : {', '.join(packages)}")
    if any(p != "ultimum" for p in packages):  # ultimum-only runs never touch this set
        print(f"footage    : {len(single)} master(s) — {single[0].parent if single else '—'}")
    if "ultimum" in packages:
        print(f"ultimum    : {len(instructor)} instructor + {len(external)} external")
    print(f"email      : {args.email or '(none — links only)'}")

    try:
        health = httpx.get(f"{api}/jobs", timeout=10.0)
        health.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"error: API not reachable at {api} ({exc})") from exc

    runs: list[PackageRun] = []
    with httpx.Client(headers=_auth_headers()) as client:
        for package in packages:
            print(f"\n── {package} ─────────────────────────────────────")
            cameras: list[tuple[str | None, list[Path]]] = (
                [("instructor", instructor), ("external", external)]
                if package == "ultimum" else [(None, single)]
            )
            runs.append(_run_package(client, api, package, cameras, args))

    return 0 if _report(runs, _REPO_ROOT) else 1


if __name__ == "__main__":
    raise SystemExit(main())
