#!/usr/bin/env python3
"""Clear a flagged s3_key from the bridge's state so its clip can be re-notified.

When the bridge can't match a clip to a jump it *refuses and flags* it
(``jobs/_bridge_state.json`` → ``flagged[s3_key] = reason``) and answers 200. That
flag is terminal by design: every later notify for the key is treated as a
duplicate, so a clip is never silently retried into a mis-matched job — the wrong
customer's video is worse than a human follow-up.

But the usual reason for a flag is *fixable data*, not bad footage: the load wasn't
manifested yet, a staff member had no ``goproSerial``, the purchase didn't map to a
package. Once that's fixed the same clip should be matched again, and with the flag
in place nothing short of hand-editing JSON allows it. This is that operation,
audited and safe:

    python scripts/unflag_bridge_key.py                     # list what's flagged
    python scripts/unflag_bridge_key.py raw/4313/GX010042.MP4 [more…]
    python scripts/unflag_bridge_key.py --all [--dry-run]

Only the ``flagged`` record is touched — never ``handled``. A key that already
became a job is refused, because clearing it would invite a SECOND job (and a second
"your video is ready" email) for footage that was already delivered. Re-notify after
clearing: re-insert the card, or re-POST the notify body.

The bridge reads its state once at startup, so restart it after clearing (or clear
before you start it) — otherwise the in-memory copy still holds the flag and will be
written back over this edit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.skydiveos_bridge import (  # noqa: E402
    clear_flagged,
    default_state_path,
    load_state,
    save_state,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Only `flagged` is modified; a key already in `handled` is refused.",
    )
    parser.add_argument("keys", nargs="*", help="s3_key(s) to un-flag")
    parser.add_argument("--all", action="store_true", help="clear every flagged key")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be cleared, write nothing"
    )
    parser.add_argument(
        "--state", type=Path, default=None,
        help="path to the bridge state file (default: $JOBS_ROOT/_bridge_state.json)",
    )
    args = parser.parse_args(argv)

    state_path = args.state or default_state_path()
    if not state_path.exists():
        print(f"no bridge state at {state_path} — nothing is flagged")
        return 0
    state = load_state(state_path)
    flagged: dict[str, str] = state["flagged"]

    # No target: report. Listing is the default so a bare run can't clear anything.
    if not args.keys and not args.all:
        if not flagged:
            print(f"{state_path}: nothing flagged")
            return 0
        print(f"{state_path}: {len(flagged)} flagged key(s)")
        for key, reason in flagged.items():
            print(f"  {key}\n      {reason}")
        print("\nre-notify one with:  python scripts/unflag_bridge_key.py <s3_key>")
        return 0

    if args.keys and args.all:
        parser.error("pass either explicit keys or --all, not both")

    targets = list(flagged) if args.all else args.keys
    cleared, unknown, already_handled = clear_flagged(state, targets)

    for key, reason in cleared.items():
        print(f"{'would clear' if args.dry_run else 'cleared'} {key}\n      was: {reason}")
    for key in unknown:
        print(f"not flagged (nothing to do): {key}")
    for key in already_handled:
        print(
            f"REFUSED {key}: already handled as job {state['handled'][key]} — clearing it "
            "would create a second job for footage that was already delivered"
        )

    if cleared and not args.dry_run:
        save_state(state_path, state)
        print(
            f"\n{len(cleared)} key(s) cleared in {state_path}. Restart the bridge (it holds "
            "state in memory), then re-notify the clip(s)."
        )
    elif args.dry_run:
        print("\n--dry-run: nothing written")

    return 1 if already_handled else 0


if __name__ == "__main__":
    raise SystemExit(main())
