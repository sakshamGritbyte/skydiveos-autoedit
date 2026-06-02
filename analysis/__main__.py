"""CLI: score the freefall window of a jump's LRV proxy.

Usage:
    python -m analysis <proxy.lrv> --start <s> --end <s> \\
        [--fps 5] [--width 480] [--model path.task] [--out scores.json] [--full-res]

The ``--start``/``--end`` values are the ``freefall_start``/``freefall_end`` the
/metadata stage emits. Prints the per-second scores as JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import score_freefall


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analysis", description=__doc__)
    parser.add_argument("proxy", help="path to the .lrv proxy")
    parser.add_argument("--start", type=float, required=True, help="freefall_start (seconds)")
    parser.add_argument("--end", type=float, required=True, help="freefall_end (seconds)")
    parser.add_argument("--fps", type=float, default=5.0, help="sampling frame rate (default 5)")
    parser.add_argument("--width", type=int, default=480, help="downscale width (default 480)")
    parser.add_argument("--model", default=None, help="FaceLandmarker .task bundle override")
    parser.add_argument("--out", default=None, help="also write scores to this JSON file")
    parser.add_argument(
        "--full-res", action="store_true", help="allow a non-.lrv input (not recommended)"
    )
    args = parser.parse_args(argv)

    rows = score_freefall(
        args.proxy,
        args.start,
        args.end,
        fps=args.fps,
        proxy_width=args.width,
        model_path=args.model,
        output_path=args.out,
        allow_full_res=args.full_res,
    )
    json.dump(rows, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
