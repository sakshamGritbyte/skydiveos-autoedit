"""Show how each uploaded GoPro file maps onto a scene's timeline.

The selfie pipeline concatenates the separate clips of a scene into one
``scenes/<name>.mp4``; the EDL timestamps are seconds on THAT combined file, not on
the original uploads. This prints, per scene, where each source file sits inside the
combined timeline — so you can translate "the door is 6 s into GX010056" into the
scene-relative timestamp to put in ``edl_*.json``.

Usage:
    python scripts/scene_timeline.py <job_id> [--jobs-root DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from edl.storage import job_dir  # noqa: E402


def _duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="the job whose scenes to map")
    parser.add_argument("--jobs-root", default=None, help="jobs root ($JOBS_ROOT or ./jobs)")
    args = parser.parse_args(argv)

    jd = job_dir(args.job_id, args.jobs_root)
    manifest_path = jd / "scene_manifest.json"
    if not manifest_path.exists():
        parser.error(f"no scene_manifest.json for job {args.job_id}")

    manifest = json.loads(manifest_path.read_text())
    raw = jd / "raw"
    for scene in manifest["scenes"]:
        sys.stdout.write(f"\nscenes/{scene['name']}.mp4  (total {scene['duration']}s)\n")
        t = 0.0
        for filename in scene["source_files"]:
            d = _duration(raw / filename)
            sys.stdout.write(f"   [{t:8.2f}s -> {t + d:8.2f}s]   {filename}\n")
            t += d
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
