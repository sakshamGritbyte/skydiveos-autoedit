#!/usr/bin/env python3
"""Re-stamp GoPro masters to a chosen jump time — for demoing with old footage.

The footage→customer match keys on *when* a clip was filmed, so last week's card
cannot be matched to a load manifested for today. Usually the right answer is to
manifest the load for the footage's real date (nothing is altered, and the camera pull
itself still gets tested — see ``scripts/check_camera.py`` for the card's dates).

When you must move the footage instead — demoing "today's jump" with an old card —
this writes re-stamped COPIES. Originals are never touched.

Three traps this exists to avoid, all of which silently break the pipeline:

* **The GPMF stream gets dropped.** A plain ``ffmpeg -c copy`` keeps video and audio
  and quietly discards the ``gpmd`` telemetry track, so segmentation finds no exit or
  deployment and the job fails. The stream map here keeps it.
* **The timestamp gets timezone-shifted.** ``-metadata creation_time="… 13:20:00"`` is
  read as *host-local* and stored converted to UTC. Passing an explicit ``Z`` stores it
  verbatim — which is what we want, because a GoPro also writes its LOCAL wall clock
  and mislabels it ``Z`` (:func:`ingest.discovery._to_true_utc` undoes that using
  ``CAMERA_CLOCK_TZ``). So ``--at`` here is dropzone-local wall clock, like the camera's.
* **All clips land on the same instant.** A jump is several files minutes apart, and
  each is matched individually. Relative spacing is preserved: the earliest clip moves
  to ``--at`` and the rest keep their original offsets from it.

Usage::

    # a whole card, first clip lands at 13:20 dropzone-local today
    python scripts/restamp_footage.py --at 2026-07-29T13:20 --out-dir /tmp/demo \\
        /path/to/100GOPRO/*.MP4

    # check what it WOULD do
    python scripts/restamp_footage.py --at 2026-07-29T13:20 --dry-run /path/*.MP4

Feed the results in with ``POST /jobs/{id}/upload`` (or ``qa_all_packages.py
--footage``). They cannot go back onto the camera — a GoPro indexes its own card and
will not list files copied onto it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _creation_time(path: Path) -> datetime | None:
    """The clip's ``creation_time`` as a naive wall clock (the camera's own clock)."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        raw = json.loads(out).get("format", {}).get("tags", {}).get("creation_time")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not raw:
        return None
    try:
        # Drop the (misleading) tz label: GoPro writes local time and calls it UTC.
        # Same normalisation as ingest.discovery._to_true_utc.
        return datetime.fromisoformat(re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", raw.strip()))
    except ValueError:
        return None


def _has_gpmd(path: Path) -> bool:
    """Whether the file still carries the GoPro telemetry track."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_tag_string",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "gpmd" in out


def _restamp(src: Path, dst: Path, when: datetime) -> bool:
    """Remux ``src`` to ``dst`` with ``when`` as its creation time. Returns success.

    Keeps video + audio + the ``gpmd`` telemetry and drops only the ``tmcd`` timecode
    track, which ffmpeg cannot remux and the pipeline does not read. The trailing ``Z``
    makes ffmpeg store the wall clock verbatim instead of converting it from host-local.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(src),
        "-map", "0:v", "-map", "0:a?", "-map", "0:d:1?",
        "-c", "copy", "-copy_unknown",
        "-metadata", f"creation_time={when:%Y-%m-%dT%H:%M:%S}Z",
        "-y", str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        print(f"  ffmpeg failed: {result.stderr.strip().splitlines()[:2]}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python scripts/restamp_footage.py",
        description="Write re-stamped copies of GoPro masters at a chosen jump time.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("footage", nargs="+", help="the MP4 masters to re-stamp")
    p.add_argument("--at", required=True,
                   help="DROPZONE-LOCAL time for the FIRST clip, e.g. 2026-07-29T13:20")
    p.add_argument("--out-dir", default="./restamped", help="where the copies go")
    p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    args = p.parse_args(argv)

    try:
        base = datetime.fromisoformat(args.at)
    except ValueError:
        raise SystemExit(f"error: --at is not an ISO datetime: {args.at!r}") from None

    sources = [Path(f) for f in args.footage]
    missing = [str(s) for s in sources if not s.is_file()]
    if missing:
        raise SystemExit(f"error: not found: {', '.join(missing)}")

    stamped = [(s, _creation_time(s)) for s in sources]
    known = [t for _, t in stamped if t is not None]
    if not known:
        raise SystemExit(
            "error: none of these files has a creation_time — cannot preserve their "
            "relative spacing. Are they GoPro originals?"
        )
    earliest = min(known)
    out_dir = Path(args.out_dir)

    print(f"first clip -> {base:%Y-%m-%d %H:%M:%S} (dropzone-local)")
    print(f"out dir    -> {out_dir}\n")

    failures = 0
    for src, original in sorted(stamped, key=lambda x: (x[1] or earliest, x[0].name)):
        # Keep each clip's offset from the first, so a multi-file jump stays coherent
        # and each file still matches its own moment.
        offset = (original - earliest) if original else timedelta(0)
        when = base + offset
        note = "" if original else "  (no original timestamp — placed at base)"
        print(f"  {src.name:<20} {original or '?'} -> {when:%Y-%m-%d %H:%M:%S}{note}")
        if args.dry_run:
            continue
        dst = out_dir / src.name
        if not _restamp(src, dst, when):
            failures += 1
            continue
        if not _has_gpmd(dst):
            print(f"    WARNING: {dst.name} lost its gpmd telemetry — segmentation will fail")
            failures += 1

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0
    if failures:
        print(f"\n{failures} file(s) failed — do NOT use this set")
        return 1
    print(f"\n{len(sources)} file(s) written to {out_dir}, telemetry intact.")
    print("Upload them with POST /jobs/{id}/upload, or qa_all_packages.py --footage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
