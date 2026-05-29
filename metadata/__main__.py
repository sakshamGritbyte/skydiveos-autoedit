"""CLI: extract a jump phase timeline from a GoPro MP4.

Usage:
    python -m metadata <path/to/jump.mp4> [--labels labels.json] [--out out.json]

Prints the resulting timeline (seconds per phase) as JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import extract_metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m metadata", description=__doc__)
    parser.add_argument("mp4", help="path to the GoPro MP4 (or .lrv proxy)")
    parser.add_argument("--labels", default=None, help="ground-truth labels JSON (fallback)")
    parser.add_argument("--out", default=None, help="also write the timeline to this JSON file")
    args = parser.parse_args(argv)

    timeline = extract_metadata(args.mp4, labels_path=args.labels, output_path=args.out)
    json.dump(timeline, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
