#!/usr/bin/env python3
"""Operator display for the card reader: when is it safe to remove the card?

The signal itself lives in the ingest process's in-memory registry and is served by
``GET /ingest/cards``. This is the view for the person physically standing at the reader,
on that same machine — a terminal window that goes loud on ``safe_to_remove``.

It exists because the registry is **per-process**: production runs the renderer on
another host with discovery off, and SkydiveOS holds one auto-edit base URL pointing
there, so the SkydiveOS banner reads an empty list until the push publisher
(``ingest.discovery.publish_card_status``) is deployed on both sides. This needs neither
— it talks to the local API, which is the only place the answer has ever existed.

Usage::

    python scripts/watch_cards.py                     # poll the local API
    python scripts/watch_cards.py --api http://host:8000
    python scripts/watch_cards.py --once              # one snapshot, for a script

Exit code is 0 while it can read the endpoint, 2 when the endpoint cannot be reached at
all (wrong host/port, or the API is down) — so ``--once`` doubles as a health check.
An EMPTY list is not an error: it is the resting state with no card in the reader.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: The three states an operator acts on, and what to do. ``sweeping`` is grouped with
#: ``pulling`` deliberately: a retention delete WRITES to the card, so it is the one
#: moment when pulling it out can corrupt the filesystem rather than merely abort a copy.
_ACTION = {
    "detected": ("· ", "card detected — preparing"),
    "sweeping": ("⏳", "CLEARING SPACE — DO NOT REMOVE"),
    "pulling": ("⏳", "COPYING — DO NOT REMOVE"),
    "safe_to_remove": ("✅", "SAFE TO REMOVE"),
    "error": ("⚠️ ", "FAILED — leave the card in and check the log"),
}


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _bar(done: int, total: int, width: int = 28) -> str:
    """A progress bar, or blank while the totals are still unknown.

    Totals arrive a moment after a card is first seen, so ``detected`` carries zeros —
    guard the division rather than render a full or crashed bar.
    """
    if total <= 0:
        return " " * width
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def _render(cards: list[dict[str, Any]]) -> str:
    if not cards:
        return "  no card in the reader\n"
    lines = []
    for c in cards:
        state = str(c.get("state") or "?")
        icon, action = _ACTION.get(state, ("? ", state))
        files_total = int(c.get("files_total") or 0)
        files_done = int(c.get("files_done") or 0)
        bytes_total = int(c.get("bytes_total") or 0)
        bytes_done = int(c.get("bytes_done") or 0)
        lines.append(f"  {icon} Card {c.get('camera_id')}   {action}")
        if state in ("pulling", "sweeping") and files_total:
            lines.append(
                f"       {_bar(bytes_done, bytes_total)}  "
                f"{files_done}/{files_total} clips  "
                f"{_human_bytes(bytes_done)} / {_human_bytes(bytes_total)}"
            )
            current = c.get("current_file")
            if current:
                lines.append(f"       {current}")
        if c.get("error"):
            lines.append(f"       {c['error']}")
        lines.append("")
    return "\n".join(lines)


def _fetch(api: str, token_headers: dict[str, str]) -> list[dict[str, Any]] | None:
    """The snapshot, or ``None`` when the endpoint could not be read at all."""
    import httpx

    try:
        resp = httpx.get(f"{api}/ingest/cards", headers=token_headers, timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:  # noqa: BLE001 - an unreachable API is a reported state
        print(f"  cannot read {api}/ingest/cards — {e}", file=sys.stderr)
        return None
    return body if isinstance(body, list) else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--api", default="http://localhost:8000",
        help="the ingest host's own API (default: %(default)s)",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="seconds between polls"
    )
    parser.add_argument(
        "--once", action="store_true", help="print one snapshot and exit"
    )
    args = parser.parse_args(argv)

    from api.auth import service_auth_headers

    api = args.api.rstrip("/")
    headers = service_auth_headers()

    if args.once:
        cards = _fetch(api, headers)
        if cards is None:
            return 2
        print(_render(cards), end="")
        return 0

    # `safe_to_remove` is sticky upstream (a card left in the reader keeps its badge, so
    # the operator is never trained to ignore it), which means the entry alone cannot say
    # "this just finished". Ring the bell on the TRANSITION, once per card.
    announced: set[str] = set()
    print(f"watching {api}/ingest/cards — Ctrl-C to stop")
    try:
        while True:
            cards = _fetch(api, headers)
            if cards is None:
                time.sleep(args.interval)
                continue
            print("\033[2J\033[H", end="")  # clear + home
            print(f"  SD-CARD INGEST · {time.strftime('%H:%M:%S')}\n")
            print(_render(cards), end="")
            present = set()
            for c in cards:
                cid = str(c.get("camera_id"))
                present.add(cid)
                if c.get("state") == "safe_to_remove" and cid not in announced:
                    announced.add(cid)
                    print(f"\a  >>> Card {cid} is finished — you can remove it now.\n")
            announced &= present  # re-inserting the same card announces again
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
