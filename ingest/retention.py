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

**A filename is not an identity.** GoPro numbering restarts at ``GX010001.MP4`` on a
formatted or replaced card, and two unlabeled cards both identify as ``sd-NO-NAME`` — so
this ledger can hold yesterday's ``GX010001.MP4`` while a *different* file of that name
sits on today's card. Matching on the name alone would authorise deleting footage that was
never uploaded (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-F). So every record also carries the
**size** of the file it confirmed, and :func:`deletable` requires the on-card file to still
match it. Records written before that field existed carry ``size: None`` and are treated as
**unverifiable — never deletable**: this module's whole premise is that deletion needs
positive proof, and "some file with this name was uploaded once" is not it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
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
    #: Size in bytes of the file that was uploaded — the *identity* check. ``None`` on a
    #: record written before this field existed, which makes it unverifiable and
    #: therefore never deletable (see the module docstring).
    size: int | None = None

    def matches(self, on_card_size: int | None) -> bool:
        """Whether the file now on the card is the one this record confirmed.

        Both sides must be known: an unverifiable record (no ``size``) and an
        unmeasurable on-card file are equally short of the positive proof this module
        requires, so both answer ``False`` and the footage stays.
        """
        return (
            self.size is not None
            and on_card_size is not None
            and self.size == on_card_size
        )


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
    size: int | None = None,
    now: float | None = None,
) -> None:
    """Note that ``filename`` is safely in S3 under ``s3_key``.

    ``size`` is the byte count of the file that was uploaded, and it is what makes this
    record an identity rather than a name: :func:`deletable` will only authorise deleting
    an on-card file whose size still matches. Callers should always pass it — omitting it
    writes a record that can never authorise a deletion (fail-safe, but the card then
    never gets cleaned).

    Best-effort and never raises: failing to record costs us a delayed cleanup, while
    letting the exception escape would fail a hand-off that already succeeded.
    """
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        path = ledger_path(staging_dir)
        entries = _read(path)
        entries[filename] = {
            "s3_key": s3_key,
            "at": now if now is not None else time.time(),
            "size": size,
        }
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
        raw_size = row.get("size")
        if isinstance(key, str) and isinstance(at, int | float):
            out[name] = TransferRecord(
                filename=name,
                s3_key=key,
                at=float(at),
                size=int(raw_size) if isinstance(raw_size, int | float) else None,
            )
    return out


def deletable(
    staging_dir: Path,
    on_card: Iterable[str] | Mapping[str, int | None],
    *,
    min_age_s: float,
    now: float | None = None,
) -> list[TransferRecord]:
    """The on-card files that are safe to delete, newest-confirmed last.

    A file qualifies only when **all** of these hold:

    1. it is present on the card;
    2. the ledger has a record for that name (so S3 holds *a* file of that name);
    3. the record's ``size`` matches the file now on the card — the identity check, so a
       stale record for yesterday's ``GX010001.MP4`` can never authorise deleting a
       *different* ``GX010001.MP4`` on today's card (see the module docstring);
    4. the confirmation is at least ``min_age_s`` old.

    ``on_card`` may be a plain iterable of names (then no size is known, so **nothing** is
    deletable — the caller must supply sizes to enable cleanup) or a mapping of
    ``name -> size`` as :func:`ingest.pull._sweep_card` passes.

    Pure apart from reading the ledger; no camera or network access.
    """
    stamp = now if now is not None else time.time()
    records = confirmed(staging_dir)
    sizes: dict[str, int | None] = (
        dict(on_card) if isinstance(on_card, Mapping) else {name: None for name in on_card}
    )
    ready: list[TransferRecord] = []
    for name, rec in records.items():
        if name not in sizes or (stamp - rec.at) < min_age_s:
            continue
        if not rec.matches(sizes[name]):
            logger.warning(
                "keeping %s: the ledger confirmed a %s-byte file but the card holds %s "
                "bytes — a reused GoPro filename, not the footage %s covers",
                name, rec.size, sizes[name], rec.s3_key,
            )
            continue
        ready.append(rec)
    return sorted(ready, key=lambda r: r.at)
