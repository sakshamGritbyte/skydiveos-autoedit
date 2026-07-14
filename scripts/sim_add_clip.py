"""Simulate a fresh jump landing on a simulated camera (``CAMERA_SCANNER=static``).

A real GoPro accumulates new clips between detections, so re-pulling it picks up the
new footage. The no-hardware simulation otherwise reports a fixed set of clips, so a
second scan finds nothing new. This bumps the per-camera clip count the simulation
reads on every pull (``<raw-storage>/.sim_clips/<camera_id>``), so the *running*
discovery loop detects the added clips on its next sweep — no restart, no re-`.env`.

Usage (from the repo root)::

    # add one new clip to TESTGOPRO001 (default), then wait one scan interval:
    python scripts/sim_add_clip.py TESTGOPRO001

    # add 3 new clips at once:
    python scripts/sim_add_clip.py TESTGOPRO001 3

    # show the current count without changing it:
    python scripts/sim_add_clip.py TESTGOPRO001 0

Reset a camera fully by removing both its staged footage and this marker::

    rm -rf raw-storage/TESTGOPRO001 raw-storage/.sim_clips/TESTGOPRO001
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as `python scripts/sim_add_clip.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.app import SIM_CLIPS_DIR  # noqa: E402
from api.config import get_settings  # noqa: E402
from ingest.storage import storage_root  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2

    camera_id = args[0].strip()
    try:
        add = int(args[1]) if len(args) > 1 else 1
    except ValueError:
        print(f"clip count must be an integer, got {args[1]!r}")
        return 2

    settings = get_settings()
    marker = storage_root() / SIM_CLIPS_DIR / camera_id

    # Seed from the marker if it exists, else the configured base count.
    current = settings.discovery_sample_count
    if marker.is_file():
        try:
            current = max(1, int(marker.read_text().strip()))
        except ValueError:
            pass

    new_total = max(1, current + add)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(new_total))

    if add == 0:
        print(f"{camera_id}: {new_total} clip(s) on the simulated card.")
    else:
        print(
            f"{camera_id}: {current} -> {new_total} clip(s) "
            f"(+{new_total - current}). The running discovery loop will pull the new "
            f"clip(s) on its next scan."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
