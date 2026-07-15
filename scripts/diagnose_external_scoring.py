"""Diagnose whether the External package's scorer is silently near-zeroing.

Distant cameraman (``external``) footage scores few faces, and the suspicion is
that the scoring stage returns a near-flat, near-zero timeline — so the editor has
no peaks to cut on. This script is a **read-only, offline diagnostic**: it pulls a
job's *persisted* artifacts (it never re-runs the pipeline or touches any
production module) and plots the score timeline against what the EDL actually
selected and where the jump's milestones fell, so a human can eyeball which of
three failure patterns is happening.

For each ``--job-id`` it loads three artifacts from object storage:

* ``jobs/{id}/scores.json``        — the per-second face scores (the scorer's output)
* ``jobs/{id}/edl.json``           — the EDL the system generated
* ``jobs/{id}/segmentation.json``  — exit / deployment / landing timestamps

and renders ``diagnostic_output/{job_id}.png`` (score line + green EDL-selected
bands + red milestone lines), prints a one-line stat summary, and finally writes
``diagnostic_output/findings.md`` naming the observed pattern:

* **near-zero** across the whole timeline  → the scorer is silently failing;
* **coherent but peaking in the wrong place** → a timeline / offset mismatch;
* **normal-looking** → the root cause is elsewhere.

Artifacts live in ``s3://$DIAG_S3_BUCKET/`` (default ``skydiveos-media-staging``);
AWS credentials come from the environment (``boto3`` default chain). For offline
testing, ``--local <dir>`` reads ``<dir>/{job_id}/*.json`` instead of S3.

Usage:
    python -m scripts.diagnose_external_scoring --job-id JOB_EXT_001 [--job-id ...]
    python -m scripts.diagnose_external_scoring --job-id J1 --local ./fixtures
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

# matplotlib/boto3 live in the optional [diag] group; import lazily inside the
# functions that need them so `--help` and unit-level use don't require them.

#: Object-storage bucket holding the persisted per-job artifacts. Overridable so
#: this can point at whichever staging/prod bucket a deployment actually uses
#: without editing the script.
DEFAULT_BUCKET = os.environ.get("DIAG_S3_BUCKET", "skydiveos-media-staging")

#: Where PNGs and findings.md are written (gitignored).
OUTPUT_DIR = Path("diagnostic_output")

#: Per-second score fields the scorer emits (see api.selfie.score_scene). When a
#: row carries no single composite score we average whichever of these are present.
_METRIC_KEYS = ("smile", "eye_contact", "face_in_frame", "face_centered")

#: Milestones we mark on the timeline (segmentation.json keys → label).
_MILESTONES = (("exit", "exit"), ("deployment", "deployment"), ("landing", "landing"))


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _load_json_s3(bucket: str, key: str) -> Any:
    import boto3  # optional [diag] dep

    client = boto3.client("s3")
    obj = client.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


def _load_json_local(root: Path, job_id: str, name: str) -> Any:
    return json.loads((root / job_id / name).read_text())


def _load_artifact(job_id: str, name: str, *, bucket: str, local: Path | None) -> Any:
    """Load ``jobs/{job_id}/{name}`` from local dir (if given) else S3."""
    if local is not None:
        return _load_json_local(local, job_id, name)
    return _load_json_s3(bucket, f"jobs/{job_id}/{name}")


# --------------------------------------------------------------------------- #
# Normalising the persisted shapes
# --------------------------------------------------------------------------- #


def _row_score(row: dict[str, Any]) -> float:
    """Composite 0–1 score for one per-second row.

    Prefers an explicit composite field; otherwise averages whichever face
    metrics are present. Non-numeric / missing values are skipped.
    """
    for key in ("score", "overall", "highlight"):
        val = row.get(key)
        if isinstance(val, (int, float)):
            return max(0.0, min(1.0, float(val)))
    vals = [float(row[k]) for k in _METRIC_KEYS if isinstance(row.get(k), (int, float))]
    if not vals:
        return 0.0
    return max(0.0, min(1.0, sum(vals) / len(vals)))


def _score_timeline(scores: Any) -> tuple[list[float], list[float]]:
    """Normalise scores.json into parallel (times, scores) arrays.

    Handles both persisted shapes:
      * a flat list of per-second rows (one continuous timeline), or
      * ``{scene_name: [rows...]}`` (the multi-scene pipeline), which we lay end
        to end in insertion order with a cumulative time offset so it reads as one
        instructor-time axis.
    Each row's ``ts`` is used when present, else the row index (1 s cadence).
    """
    if isinstance(scores, dict):
        rows: list[dict[str, Any]] = []
        offset = 0.0
        for scene_rows in scores.values():
            if not isinstance(scene_rows, list) or not scene_rows:
                continue
            base = offset
            last = 0.0
            for i, row in enumerate(scene_rows):
                ts = row.get("ts", float(i)) if isinstance(row, dict) else float(i)
                last = float(ts)
                rows.append({"_t": base + last, **(row if isinstance(row, dict) else {})})
            offset = base + last + 1.0  # +1 s gap so scenes don't overlap
        rows.sort(key=lambda r: r["_t"])
        return [r["_t"] for r in rows], [_row_score(r) for r in rows]

    if isinstance(scores, list):
        times = [
            float(r.get("ts", i)) if isinstance(r, dict) else float(i)
            for i, r in enumerate(scores)
        ]
        vals = [_row_score(r) if isinstance(r, dict) else 0.0 for r in scores]
        return times, vals

    return [], []


def _edl_clip_lists(edl: Any) -> list[list[dict[str, Any]]]:
    """Extract every clip list from an EDL, whatever its persisted shape."""
    if isinstance(edl, dict):
        if isinstance(edl.get("clips"), list):  # single EditDecisionList
            return [edl["clips"]]
        # multi-deliverable (full_video/highlights/freefall): take each list found.
        return [v for v in edl.values() if isinstance(v, list) and v]
    if isinstance(edl, list):
        return [edl]
    return []


def _edl_bands(edl: Any) -> list[tuple[float, float]]:
    """Source [start, end) windows the EDL selected, from the first clip list.

    Times are on the source timeline (same axis the scores use). We use the first
    clip list so a single deliverable is shaded rather than every variant stacked.
    """
    lists = _edl_clip_lists(edl)
    if not lists:
        return []
    bands: list[tuple[float, float]] = []
    for clip in lists[0]:
        if not isinstance(clip, dict):
            continue
        start, end = clip.get("src_start"), clip.get("src_end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            bands.append((float(start), float(end)))
    return bands


def _milestones(seg: Any) -> dict[str, float]:
    """Pull exit/deployment/landing timestamps (drop any that are null)."""
    out: dict[str, float] = {}
    if isinstance(seg, dict):
        for key, label in _MILESTONES:
            val = seg.get(key)
            if isinstance(val, (int, float)):
                out[label] = float(val)
    return out


# --------------------------------------------------------------------------- #
# Stats + plotting
# --------------------------------------------------------------------------- #


def _summary(job_id: str, values: list[float]) -> dict[str, Any]:
    if not values:
        return {"job_id": job_id, "max": 0.0, "min": 0.0, "mean": 0.0,
                "std": 0.0, "zeros": 0, "total": 0}
    zeros = sum(1 for v in values if v <= 1e-6)
    return {
        "job_id": job_id,
        "max": max(values),
        "min": min(values),
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "zeros": zeros,
        "total": len(values),
    }


def _summary_line(s: dict[str, Any]) -> str:
    return (
        f"{s['job_id']}: max={s['max']:.2f} min={s['min']:.2f} "
        f"mean={s['mean']:.2f} std={s['std']:.3f} zeros={s['zeros']}/{s['total']}"
    )


def _plot(
    job_id: str,
    package_type: str,
    times: list[float],
    values: list[float],
    bands: list[tuple[float, float]],
    milestones: dict[str, float],
    out_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")  # headless: no display needed
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 5))
    if times:
        ax.plot(times, values, color="tab:blue", linewidth=1.2, label="per-second score")

    for i, (start, end) in enumerate(bands):
        ax.axvspan(start, end, color="tab:green", alpha=0.25,
                   label="EDL-selected clip" if i == 0 else None)

    for label, ts in milestones.items():
        ax.axvline(ts, color="tab:red", linestyle="--", linewidth=1.2)
        ax.text(ts, 1.01, label, color="tab:red", ha="center", va="bottom",
                fontsize=8, transform=ax.get_xaxis_transform())

    ax.set_xlabel("time (s, instructor-time)")
    ax.set_ylabel("per-second score (0–1)")
    ax.set_ylim(-0.02, 1.05)
    ax.set_title(f"Score timeline for {job_id} — {package_type}")
    ax.grid(True, alpha=0.3)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Pattern classification + report
# --------------------------------------------------------------------------- #

NEAR_ZERO = "near-zero across the timeline (confirms scorer silent fail)"
MISMATCH = "coherent but peaking in the wrong place (timeline mismatch)"
NORMAL = "normal-looking (different root cause)"


def _classify(summary: dict[str, Any]) -> str:
    """Heuristic bucket for one job's score timeline.

    * **near-zero**: hardly any signal — low mean AND low peak. This is what a
      silently-failing scorer looks like: it returns ~0 for every second.
    * **normal**: a healthy mean with real spread (there are peaks to cut on).
    * **mismatch**: there IS signal but it's weak/oddly distributed — consistent
      with a coherent timeline that's offset from where the EDL/milestones expect
      peaks. Distinguishing this from 'normal' precisely needs a human eye on the
      PNG; the label is a prompt, not a verdict.
    """
    if summary["total"] == 0:
        return NEAR_ZERO
    if summary["mean"] < 0.05 and summary["max"] < 0.15:
        return NEAR_ZERO
    if summary["mean"] >= 0.20 and summary["std"] > 0.05:
        return NORMAL
    return MISMATCH


def _overall_verdict(patterns: list[str]) -> str:
    """The pattern shared by all jobs, else the most common one."""
    if patterns and all(p == patterns[0] for p in patterns):
        return patterns[0]
    return max(set(patterns), key=patterns.count) if patterns else NEAR_ZERO


def _write_findings(
    rows: list[tuple[dict[str, Any], str, str]], out_path: Path
) -> None:
    """rows: (summary, package_type, pattern) per job."""
    lines: list[str] = ["# External-package scoring diagnostic\n"]
    lines.append("## Summary statistics\n")
    lines.append("| job_id | package | max | min | mean | std | zeros/total | pattern |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s, pkg, pattern in rows:
        lines.append(
            f"| {s['job_id']} | {pkg} | {s['max']:.2f} | {s['min']:.2f} | "
            f"{s['mean']:.2f} | {s['std']:.3f} | {s['zeros']}/{s['total']} | "
            f"{pattern.split(' (')[0]} |"
        )

    verdict = _overall_verdict([p for _, _, p in rows])
    lines.append("\n## Diagnosis\n")
    lines.append(
        f"Across the {len(rows)} job(s) examined, the observed pattern is: "
        f"**{verdict}**.\n"
    )
    lines.append(
        "Interpretation of the three possible patterns:\n\n"
        f"- *{NEAR_ZERO}* — the score line hugs the x-axis for the whole jump, so "
        "the scorer never finds a face to reward and the editor has no peaks to cut "
        "on. This is the hypothesis under test.\n"
        f"- *{MISMATCH}* — the score line has real structure, but its peaks don't "
        "line up with the freefall window or the green EDL bands; the numbers are "
        "fine but sit on the wrong timeline.\n"
        f"- *{NORMAL}* — a healthy, peaky timeline whose peaks fall in the expected "
        "window; if the edit is still bad, the cause is downstream of scoring.\n"
    )
    lines.append("\n## Figures\n")
    for s, _, _ in rows:
        lines.append(f"- ![{s['job_id']}]({s['job_id']}.png)")
    out_path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _package_type(job_id: str, *, bucket: str, local: Path | None) -> str:
    """Best-effort package label for the title; job.json is optional."""
    try:
        job = _load_artifact(job_id, "job.json", bucket=bucket, local=local)
        if isinstance(job, dict):
            for key in ("package", "package_type"):
                if job.get(key):
                    return str(job[key])
    except Exception:
        pass
    return "external"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job-id", dest="job_ids", action="append", required=True,
        help="job id to diagnose (repeatable)",
    )
    parser.add_argument(
        "--bucket", default=DEFAULT_BUCKET,
        help=f"S3 bucket holding jobs/ artifacts (default: {DEFAULT_BUCKET})",
    )
    parser.add_argument(
        "--local", type=Path, default=None,
        help="read jobs/{id}/*.json from this local dir instead of S3 (offline testing)",
    )
    args = parser.parse_args(argv)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[dict[str, Any], str, str]] = []

    for job_id in args.job_ids:
        try:
            scores = _load_artifact(job_id, "scores.json", bucket=args.bucket, local=args.local)
            edl = _load_artifact(job_id, "edl.json", bucket=args.bucket, local=args.local)
            seg = _load_artifact(
                job_id, "segmentation.json", bucket=args.bucket, local=args.local
            )
        except Exception as exc:  # noqa: BLE001 — surface any load failure per job
            print(f"{job_id}: ERROR loading artifacts: {exc}", file=sys.stderr)
            continue

        times, values = _score_timeline(scores)
        bands = _edl_bands(edl)
        milestones = _milestones(seg)
        pkg = _package_type(job_id, bucket=args.bucket, local=args.local)

        _plot(job_id, pkg, times, values, bands, milestones, OUTPUT_DIR / f"{job_id}.png")

        summary = _summary(job_id, values)
        pattern = _classify(summary)
        print(_summary_line(summary))
        rows.append((summary, pkg, pattern))

    if not rows:
        print("No jobs produced output; nothing to write.", file=sys.stderr)
        return 1

    _write_findings(rows, OUTPUT_DIR / "findings.md")
    print(f"Wrote {OUTPUT_DIR}/findings.md and {len(rows)} PNG(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
