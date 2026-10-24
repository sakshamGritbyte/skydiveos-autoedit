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

Because of that, the ledger can hold **several records for one filename** — one per
distinct recording. The first confirmation of a name is written under the bare name (the
layout every existing ledger already has); a confirmation of a *different* recording with
the same name is written under ``NAME#SIZE`` instead of overwriting the first, so clearing
card 2's ``GX010001.MP4`` can never erase the proof that card 1's was uploaded (and vice
versa). Readers (:func:`confirmed`, :func:`deletable`, :func:`card_transfer`) match a
card file against *every* record of its name and accept the one whose size agrees.

The same folder also holds a **rejection ledger** (``.rejected.json``): clips SkydiveOS
refused with a permanent (4xx) answer. Nothing deletes on it — it exists so the operator
banner can say *why* a card still owes something, and so a restart knows which staged
clips were refused rather than merely not yet uploaded.
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
#: Permanent hand-off rejections (SkydiveOS 4xx), beside the ledger. Informational only.
REJECTED_NAME = ".rejected.json"
#: Key separator for a second recording of an already-recorded filename.
_KEY_SEP = "#"


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


def rejected_path(staging_dir: Path) -> Path:
    """Where the camera's hand-off rejection ledger lives."""
    return staging_dir / REJECTED_NAME


def _record_key(filename: str, size: int | None) -> str:
    return f"{filename}{_KEY_SEP}{size}" if size is not None else filename


def _filename_of(key: str, row: Mapping[str, Any]) -> str:
    name = row.get("filename")
    if isinstance(name, str) and name:
        return name
    return key.split(_KEY_SEP, 1)[0]


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
        # One record per RECORDING, not per name: the first confirmation of a name
        # keeps the bare key (the layout every existing ledger has); a confirmation of
        # the same name but a different size — a second card's reused GoPro filename —
        # gets its own `NAME#SIZE` key instead of overwriting the first. A re-upload of
        # the same recording (same size) updates its own record in place.
        key = filename
        for existing_key, row in entries.items():
            if not isinstance(row, dict) or _filename_of(existing_key, row) != filename:
                continue
            existing_size = row.get("size")
            if existing_size == size:
                key = existing_key
                break
            if existing_key == filename:
                key = _record_key(filename, size)
        entries[key] = {
            "filename": filename,
            "s3_key": s3_key,
            "at": now if now is not None else time.time(),
            "size": size,
        }
        path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    except OSError as e:  # noqa: BLE001 - a ledger write must not break ingest
        logger.warning("could not record %s as uploaded: %r", filename, e)


def record_rejected(
    staging_dir: Path,
    filename: str,
    *,
    size: int | None,
    reason: str,
    now: float | None = None,
) -> None:
    """Note that SkydiveOS permanently refused ``filename``'s hand-off (a 4xx).

    Informational: nothing is ever deleted on this record. It lets the operator banner
    distinguish "still uploading" from "refused — fix the cause", and survives a restart
    so the startup reconciliation can report the clip rather than silently retry it
    forever. Never raises.
    """
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        path = rejected_path(staging_dir)
        entries = _read(path)
        entries[_record_key(filename, size)] = {
            "filename": filename,
            "size": size,
            "reason": reason[:500],
            "at": now if now is not None else time.time(),
        }
        path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    except OSError as e:  # noqa: BLE001 - never break a hand-off over bookkeeping
        logger.warning("could not record %s as rejected: %r", filename, e)


def clear_rejected(staging_dir: Path, filename: str, *, size: int | None) -> None:
    """Forget a rejection (the clip was later handed off successfully). Never raises."""
    try:
        path = rejected_path(staging_dir)
        entries = _read(path)
        key = _record_key(filename, size)
        if key in entries:
            del entries[key]
            path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    except OSError as e:  # noqa: BLE001
        logger.warning("could not clear rejection of %s: %r", filename, e)


@dataclass(frozen=True)
class RejectedRecord:
    """One clip SkydiveOS refused permanently."""

    filename: str
    size: int | None
    reason: str
    at: float


def rejected(staging_dir: Path) -> list[RejectedRecord]:
    """Every permanently refused hand-off recorded for this camera."""
    out: list[RejectedRecord] = []
    for key, row in _read(rejected_path(staging_dir)).items():
        if not isinstance(row, dict):
            continue
        raw_size = row.get("size")
        at = row.get("at")
        out.append(
            RejectedRecord(
                filename=_filename_of(key, row),
                size=int(raw_size) if isinstance(raw_size, int | float) else None,
                reason=str(row.get("reason") or ""),
                at=float(at) if isinstance(at, int | float) else 0.0,
            )
        )
    return out


def is_rejected(records: Iterable[RejectedRecord], filename: str, size: int | None) -> bool:
    """Whether a card file (name + size) has a matching rejection record."""
    return any(r.filename == filename and (r.size is None or r.size == size) for r in records)


def confirmed(staging_dir: Path) -> dict[str, TransferRecord]:
    """Every recording this camera has confirmed into S3, keyed by ledger key.

    The key is the on-card filename for the first recording of that name and
    ``NAME#SIZE`` for a later, different recording of the same name (see the module
    docstring); ``TransferRecord.filename`` is always the bare on-card name. Use
    :func:`confirmed_for` to find the record that covers a specific card file.
    """
    out: dict[str, TransferRecord] = {}
    for ledger_key, row in _read(ledger_path(staging_dir)).items():
        if not isinstance(row, dict):
            continue
        key = row.get("s3_key")
        at = row.get("at")
        raw_size = row.get("size")
        if isinstance(key, str) and isinstance(at, int | float):
            out[ledger_key] = TransferRecord(
                filename=_filename_of(ledger_key, row),
                s3_key=key,
                at=float(at),
                size=int(raw_size) if isinstance(raw_size, int | float) else None,
            )
    return out


def confirmed_for(
    records: Mapping[str, TransferRecord], filename: str, size: int | None
) -> TransferRecord | None:
    """The record proving THIS card file (name + size) is in S3, or ``None``.

    Several records may share a filename (one per recording); only the one whose size
    matches the file now on the card counts, so a stale record for a different
    recording of the same name never answers for it.
    """
    for rec in records.values():
        if rec.filename == filename and rec.matches(size):
            return rec
    return None


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
    for name, size in sizes.items():
        rec = confirmed_for(records, name, size)
        if rec is None:
            others = [r for r in records.values() if r.filename == name]
            for other in others:
                logger.warning(
                    "keeping %s: the ledger confirmed a %s-byte file but the card holds %s "
                    "bytes — a reused GoPro filename, not the footage %s covers",
                    name, other.size, size, other.s3_key,
                )
            continue
        if (stamp - rec.at) < min_age_s:
            continue
        ready.append(rec)
    return sorted(ready, key=lambda r: r.at)


@dataclass(frozen=True)
class CardTransfer:
    """What a card still owes this host — the "is it safe to remove?" answer.

    The operator standing at the reader wants one thing from the banner: *can I take
    the card out and have the day's footage end up where it belongs?* Staging a clip
    onto local disk does not answer that. Two things still have to happen, and both
    need the card back in the reader if they have not:

    * the clip must reach **S3** (the ledger's record is the only proof — see this
      module's docstring), and
    * with ``DELETE_AFTER_TRANSFER`` on, it must then be **swept off the card**, which
      happens at the start of the *next* pull. A card removed before that sweep keeps
      every byte of the jump it just delivered and fills up exactly as if cleanup had
      never been enabled.

    Pure: names and sizes in, names out. The camera is never touched here.
    """

    #: Master filenames currently on the card.
    on_card: tuple[str, ...]
    #: Of those, the ones the ledger confirms are in S3 (size-matched).
    confirmed: tuple[str, ...]
    #: On-card files S3 has NOT confirmed — the card is still their only copy's source.
    unconfirmed: tuple[str, ...]
    #: Confirmed files that cleanup is due to delete but has not deleted yet.
    awaiting_sweep: tuple[str, ...]

    @property
    def pending(self) -> tuple[str, ...]:
        """Everything still holding the card here, sorted."""
        return tuple(sorted(set(self.unconfirmed) | set(self.awaiting_sweep)))

    @property
    def settled(self) -> bool:
        """Whether the card can leave the reader with nothing left undone."""
        return not self.pending


def card_transfer(
    staging_dir: Path,
    on_card: Mapping[str, int | None],
    *,
    cleanup: bool,
    min_age_s: float = 0.0,
    dry_run: bool = False,
    now: float | None = None,
) -> CardTransfer:
    """Split what is on the card into confirmed / still-owed (:class:`CardTransfer`).

    Args:
        staging_dir: The camera's staging dir — where its ledger lives.
        on_card: ``filename -> size`` as the card lists it now (post-sweep).
        cleanup: ``DELETE_AFTER_TRANSFER``. Off means nothing is ever expected to
            leave the card, so a confirmed file is done and the card is free.
        min_age_s: The cleanup grace period. A confirmed file inside its grace is
            *deliberately* still on the card, so it is not something to wait for.
        dry_run: ``DELETE_AFTER_TRANSFER_DRY_RUN``. Nothing is ever deleted, so
            waiting for the sweep would wait forever.
        now: Clock, for deterministic tests.

    The size check is :func:`deletable`'s, so "confirmed" here means exactly what it
    means there: the ledger holds a record for this name *and* the file on the card is
    still the one that record covers.
    """
    records = confirmed(staging_dir)
    settled_names: list[str] = []
    missing: list[str] = []
    for name, size in on_card.items():
        rec = confirmed_for(records, name, size)
        (settled_names if rec is not None else missing).append(name)
    awaiting: tuple[str, ...] = ()
    if cleanup and not dry_run:
        awaiting = tuple(
            sorted(
                r.filename
                for r in deletable(staging_dir, on_card, min_age_s=min_age_s, now=now)
            )
        )
    return CardTransfer(
        on_card=tuple(sorted(on_card)),
        confirmed=tuple(sorted(settled_names)),
        unconfirmed=tuple(sorted(missing)),
        awaiting_sweep=awaiting,
    )
