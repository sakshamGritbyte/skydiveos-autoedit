"""Card retention: remember what S3 confirmed, so the next pull can clear the card.

A dropzone camera fills up. A 128 GB card holds roughly 30 Ultimate jumps, so at 4–5
jumps a day it is full within a week — and a full card stops recording mid-day, which
costs a customer their video with no warning. So footage has to come off the card
automatically.

Deleting a customer's only copy is unrecoverable, so the rule here is deliberately
narrow: **a file is deletable only once S3 has confirmed it**, never merely because it
reached the ingest host's local disk (that disk can fail, or ``raw-storage`` can be
wiped). The S3 upload happens in :mod:`ingest.discovery` *after* :func:`ingest.pull`
has already closed the camera, so deletion cannot be inline. Instead:

1. ``discovery`` calls :func:`record_uploaded` the moment S3 accepts a file.
2. The **next** time that camera is connected, :func:`deletable` reads the ledger and
   names the on-card files that are confirmed-uploaded and past the grace period.
3. ``pull`` deletes exactly those, one file at a time.

That deferral is a feature, not a workaround: it gives a natural grace period, it is
crash-safe (an interrupted run just retries next connect), and it is idempotent.

The ledger lives beside the camera's staging dir
(``<root>/_camera-staging/<camera_id>/.transferred.json``) and is append-only, keyed by
filename. It is a *positive* record — an unknown file is never deleted, so a lost or
corrupt ledger fails safe by keeping footage.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Ledger filename inside the camera's staging directory.
LEDGER_NAME = ".transferred.json"


@dataclass(frozen=True)
class TransferRecord:
    """One file this host confirmed into S3."""

    filename: str  #: bare on-card name, e.g. ``GX010123.MP4``
    s3_key: str  #: the object that justifies deleting it
    at: float  #: epoch seconds the upload was confirmed


def ledger_path(staging_dir: Path) -> Path:
    """Where the confirmed-upload ledger lives for one camera."""
    return staging_dir / LEDGER_NAME


def _read(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_uploaded(
    staging_dir: Path,
    filename: str,
    s3_key: str,
    *,
    now: float | None = None,
) -> None:
    """Note that ``filename`` is safely in S3 under ``s3_key``.

    Best-effort and never raises: failing to record costs us a delayed cleanup, while
    letting the exception escape would fail a hand-off that already succeeded.
    """
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        path = ledger_path(staging_dir)
        entries = _read(path)
        entries[filename] = {"s3_key": s3_key, "at": now if now is not None else time.time()}
        path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    except OSError as e:  # noqa: BLE001 - a ledger write must not break ingest
        logger.warning("could not record %s as uploaded: %r", filename, e)


def confirmed(staging_dir: Path) -> dict[str, TransferRecord]:
    """Every file this camera has confirmed into S3, keyed by on-card filename."""
    out: dict[str, TransferRecord] = {}
    for name, row in _read(ledger_path(staging_dir)).items():
        if not isinstance(row, dict):
            continue
        key = row.get("s3_key")
        at = row.get("at")
        if isinstance(key, str) and isinstance(at, int | float):
            out[name] = TransferRecord(filename=name, s3_key=key, at=float(at))
    return out


def deletable(
    staging_dir: Path,
    on_card: Iterable[str],
    *,
    min_age_s: float,
    now: float | None = None,
) -> list[TransferRecord]:
    """The on-card files that are safe to delete, newest-confirmed last.

    A file qualifies only when it is present on the card, present in the ledger (so S3
    holds it), and its confirmation is at least ``min_age_s`` old. Everything else —
    unknown files, recent ones, anything the ledger has never heard of — is kept.

    Pure apart from reading the ledger; no camera or network access.
    """
    stamp = now if now is not None else time.time()
    records = confirmed(staging_dir)
    present = set(on_card)
    ready = [
        rec for name, rec in records.items()
        if name in present and (stamp - rec.at) >= min_age_s
    ]
    return sorted(ready, key=lambda r: r.at)
