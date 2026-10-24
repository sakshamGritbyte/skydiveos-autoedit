"""Live per-card ingest status — what tells the operator "safe to remove".

An SD-card pull is the one ingest transport where a human is standing at the
machine waiting to take the media back, so this module owns the moment the
banner says they can. That moment is **not** the end of the copy loop. Two
things still need the card after it:

* the clips must reach **S3** — until the retention ledger records a key, this
  host's disk is the only copy the pipeline has, and
* with ``DELETE_AFTER_TRANSFER`` on, they must be **swept off the card**, which
  happens at the start of the *next* pull (``ingest.pull._sweep_card``, the
  first moment S3 has confirmed them). A card taken out before that sweep goes
  back into a camera still full — which is the failure cleanup exists to
  prevent, since a full card silently stops recording mid-day.

So a copied-but-not-yet-transferred card reads ``uploading``, and
``safe_to_remove`` is raised only once :func:`ingest.retention.card_transfer`
says the card owes this host nothing (``api.app._sdcard_pull`` asks, each pull).
This module makes that whole progression observable:

* :class:`CardStatusRegistry` — an in-memory, thread-safe map of
  ``camera_id -> CardStatus`` that the pull path updates through its lifecycle
  (``detected → sweeping/pulling → uploading → safe_to_remove | error``). The
  API serves a snapshot of it at ``GET /ingest/cards``; the SkydiveOS front end polls that
  (via its backend proxy) to drive the progress bar and the
  "safe to remove" popup.
* :class:`TrackedCamera` — wraps the real :class:`~ingest.camera.Camera` so the
  existing :func:`ingest.pull.pull_camera` reports progress without being
  modified: ``list_videos`` sets the totals, each ``download_mp4`` advances the
  counters, each retention ``delete_media`` marks the sweep.
* :class:`ObservingScanner` — wraps the discovery scanner so a freshly inserted
  card shows up as ``detected`` before its pull starts, and a removed card's
  ``safe_to_remove`` entry is cleared.

Two rules, mirroring the archive's: status tracking must NEVER fail a pull
(every hook swallows its own errors — a broken progress bar must not cost a
customer's footage), and nothing in the pipeline may *read* this registry — it
is a view for humans, the pipeline's truth stays in the staging manifests.

Progress is approximate by design: clips already staged by an earlier pull are
skipped without downloading, so ``bytes_done`` can finish short of
``bytes_total``. The terminal ``state`` is the authoritative signal, not the
percentage.

Dependency-light on purpose (stdlib + :mod:`ingest.camera` only), like
:mod:`edl.validate` and :mod:`ingest.match`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .camera import Camera, RemoteMedia
from .scanner import CameraScanner

logger = logging.getLogger(__name__)

#: The card-ingest lifecycle. ``safe_to_remove`` and ``error`` are terminal.
STATE_DETECTED = "detected"
STATE_SWEEPING = "sweeping"
STATE_PULLING = "pulling"
#: Copied off the card, but S3 (and, with cleanup on, the sweep) is not done with it
#: yet — the card must stay in the reader. See the module docstring.
STATE_UPLOADING = "uploading"
#: The copy loop is done and the card is being re-read against the ledger to decide
#: whether it still owes this host anything (``api.app._report_card_state``). Brief.
STATE_VERIFYING = "verifying"
STATE_SAFE_TO_REMOVE = "safe_to_remove"
#: Everything that COULD be handed off was, but some clip(s) were refused by SkydiveOS
#: (a permanent 4xx) or gave up after every retry: they stay on the card and on disk,
#: and the operator must fix the cause (pair the card, manifest the load) and re-insert.
#: Never green — a card holding an undelivered clip is not "done".
STATE_NEEDS_ATTENTION = "needs_attention"
STATE_ERROR = "error"

_TERMINAL_STATES = frozenset({STATE_SAFE_TO_REMOVE, STATE_ERROR, STATE_NEEDS_ATTENTION})

#: Hand-off outcomes reported per clip by discovery (``handoff_observer``).
HANDOFF_CONFIRMED = "confirmed"
HANDOFF_REJECTED = "rejected"
HANDOFF_FAILED = "failed"
HANDOFF_RETRYING = "retrying"

#: States whose row may be dropped when its card stops being seen or goes stale.
#: ``uploading`` joins the terminal two because nothing is being written to the card
#: in that state: its row is refreshed by every tick's idempotent re-pull, so one
#: that goes ``_TERMINAL_LINGER_S`` without a refresh means the scan loop is gone,
#: not that a copy is taking its time. ``pulling``/``sweeping`` are never pruned —
#: that is the case the 2026-08-18 rule protects.
_STALE_PRUNABLE_STATES = _TERMINAL_STATES | {STATE_UPLOADING}

#: How long a terminal entry survives without being refreshed. Two jobs: an
#: ``error`` entry outlives its card's removal by this much (a yanked or failed
#: card should stay visible to the operator for a while, not vanish with the
#: mount — but not forever, or the board fills with stale failures), and
#: :meth:`CardStatusRegistry.snapshot` drops ANY terminal entry this stale as a
#: backstop for a wedged scan loop. While a card is actually in the reader its
#: entry is refreshed every discovery tick (the idempotent re-pull calls
#: ``pull_started``), so a terminal entry this old means no scan has seen the
#: card in 15 minutes — the card is gone, and only the drop-on-removal path
#: (``observe``) failing kept the row alive. Without the backstop, one scan
#: loop wedged by a zombie mount rebroadcast two removed cards' "safe to
#: remove" banners to the operator screen for hours (2026-08-18).
_TERMINAL_LINGER_S = 900.0


@dataclass
class CardStatus:
    """One card's ingest progress, as served by ``GET /ingest/cards``."""

    camera_id: str
    state: str
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    #: ``uploading`` only: how many clips on the card still owe this host something
    #: (not yet in S3, or not yet swept off the card). ONE number on purpose — it
    #: counts down to zero, which is the moment the state becomes ``safe_to_remove``.
    pending_files: int = 0
    current_file: str | None = None
    error: str | None = None
    #: Epoch seconds of the last transition (repo convention: seconds, floats).
    updated_at: float = 0.0
    #: The CARD's own serial (:func:`ingest.sdcard.card_serial_for_mount`) — distinct
    #: from ``camera_id``, which is the camera's. Shown on the operator banner so an
    #: admin can read off the value to pair to an instructor in SkydiveOS; ``None``
    #: when the host cannot read one. SkydiveOS enriches it with the paired name.
    card_serial: str | None = None
    #: Hand-off accounting, so the banner can say WHY a card is still held rather than
    #: one "uploading" for every cause: clips confirmed in S3 + accepted by SkydiveOS
    #: this session, clips the card still holds that S3 has not confirmed, clips
    #: confirmed but not yet swept off the card, and the ones that will never clear
    #: on their own — refused by SkydiveOS (4xx) or given up after every retry — with
    #: the reason per file.
    uploaded_files: int = 0
    unconfirmed_files: int = 0
    awaiting_sweep_files: int = 0
    rejected_files: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    issues: dict[str, str] = field(default_factory=dict)


class CardStatusRegistry:
    """Thread-safe in-memory ``camera_id -> CardStatus`` map.

    Lives for the API process's lifetime (discovery runs in the same process —
    ``api.app`` lifespan), so no persistence: a restart forgets in-flight
    progress, and the next scan/pull rebuilds it. All mutators are cheap dict
    ops under one lock; none of them raise on unknown ids (they create or
    ignore), so callers never need to pre-register a card.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._cards: dict[str, CardStatus] = {}

    # --- lifecycle transitions (called by TrackedCamera / the pull wrapper) --- #

    def detected(self, camera_id: str) -> None:
        """A card is mounted but its pull has not started. No-op if already tracked."""
        with self._lock:
            if camera_id not in self._cards:
                self._cards[camera_id] = CardStatus(
                    camera_id, STATE_DETECTED, updated_at=self._now()
                )

    def card_identity(self, camera_id: str, card_serial: str | None) -> None:
        """Attach the CARD's serial to its row (creating a ``detected`` row if needed).

        Identity, not progress: it survives :meth:`pull_started`'s counter reset
        (that method re-reads it) and is never cleared by a transition — a card
        that fails to pull is exactly the card whose serial the operator needs.
        ``None`` is a no-op rather than an erasure (a later probe may know better).
        """
        if card_serial is None:
            return
        with self._lock:
            entry = self._cards.get(camera_id)
            if entry is None:
                entry = CardStatus(camera_id, STATE_DETECTED, updated_at=self._now())
                self._cards[camera_id] = entry
            entry.card_serial = card_serial

    def pull_started(self, camera_id: str) -> None:
        """A pull is opening the card: reset the counters for this run.

        Deliberately does NOT downgrade a ``safe_to_remove`` (or ``uploading``)
        entry: discovery re-pulls a card that lingers in the reader on every scan
        tick, and an idempotent re-pull of a fully staged card skips everything —
        flapping the badge back to "pulling" each tick would train the operator to
        ignore it. Real card activity (a sweep delete, a download) still flips
        the state via :meth:`sweeping` / :meth:`file_started`, and the end of
        every pull re-answers the question via :meth:`uploading` /
        :meth:`safe_to_remove`.
        """
        with self._lock:
            entry = self._cards.get(camera_id)
            keep = entry is not None and entry.state in (
                STATE_SAFE_TO_REMOVE,
                STATE_UPLOADING,
                STATE_NEEDS_ATTENTION,
            )
            state = entry.state if keep and entry is not None else STATE_PULLING
            pending = entry.pending_files if keep and entry is not None else 0
            # Identity outlives the counters: the serial is a fact about the card,
            # not about this run (see card_identity). So does the hand-off record —
            # which clips were confirmed, refused or given up — because those are
            # facts about the footage, not about this copy pass.
            card_serial = entry.card_serial if entry is not None else None
            fresh = CardStatus(
                camera_id, state, pending_files=pending, updated_at=self._now(),
                card_serial=card_serial,
            )
            if entry is not None:
                fresh.uploaded_files = entry.uploaded_files
                fresh.rejected_files = list(entry.rejected_files)
                fresh.failed_files = list(entry.failed_files)
                fresh.issues = dict(entry.issues)
                if keep:
                    fresh.unconfirmed_files = entry.unconfirmed_files
                    fresh.awaiting_sweep_files = entry.awaiting_sweep_files
                    fresh.error = entry.error
            self._cards[camera_id] = fresh

    def totals(self, camera_id: str, files_total: int, bytes_total: int) -> None:
        """The card's listed contents: what this pull could copy at most."""
        with self._lock:
            entry = self._ensure(camera_id)
            entry.files_total = files_total
            entry.bytes_total = bytes_total
            entry.updated_at = self._now()

    def sweeping(self, camera_id: str, freed_bytes: int = 0) -> None:
        """A retention delete ran: the card is being written — do not remove.

        The freed file will never be downloaded, so it leaves the totals too.
        """
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_SWEEPING
            entry.files_total = max(0, entry.files_total - 1)
            entry.bytes_total = max(0, entry.bytes_total - freed_bytes)
            entry.updated_at = self._now()

    def file_started(self, camera_id: str, filename: str) -> None:
        """A master is being copied off the card."""
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_PULLING
            entry.current_file = filename
            entry.updated_at = self._now()

    def file_done(self, camera_id: str, size: int) -> None:
        """One master finished copying."""
        with self._lock:
            entry = self._ensure(camera_id)
            entry.files_done += 1
            entry.bytes_done += size
            entry.updated_at = self._now()

    def verifying(self, camera_id: str) -> None:
        """The copy is done; the card is being re-read against the ledger."""
        with self._lock:
            entry = self._ensure(camera_id)
            if entry.state == STATE_ERROR:
                return
            entry.state = STATE_VERIFYING
            entry.current_file = None
            entry.updated_at = self._now()

    def handoff_result(
        self, camera_id: str, filename: str, outcome: str, detail: str | None = None
    ) -> None:
        """Discovery's per-clip hand-off verdict (``handoff_observer``).

        ``confirmed`` — the clip is in S3 and SkydiveOS accepted it (it will leave the
        pending count at the next verification); ``rejected`` — SkydiveOS refused it
        permanently; ``failed`` — every retry failed; ``retrying`` — a transient failure,
        nothing to show yet. Rejected/failed files are remembered per card until they
        are confirmed, so the banner can name them and never reads "done" over them.
        """
        with self._lock:
            entry = self._ensure(camera_id)
            if outcome == HANDOFF_CONFIRMED:
                entry.uploaded_files += 1
                entry.rejected_files = [f for f in entry.rejected_files if f != filename]
                entry.failed_files = [f for f in entry.failed_files if f != filename]
                entry.issues.pop(filename, None)
            elif outcome == HANDOFF_REJECTED:
                if filename not in entry.rejected_files:
                    entry.rejected_files.append(filename)
                entry.issues[filename] = f"refused by SkydiveOS: {detail or 'rejected'}"
            elif outcome == HANDOFF_FAILED:
                if filename not in entry.failed_files:
                    entry.failed_files.append(filename)
                entry.issues[filename] = f"hand-off failed: {detail or 'gave up'}"
            entry.updated_at = self._now()

    def issue_files(self, camera_id: str) -> set[str]:
        """Filenames whose hand-off will not succeed without a human (refused/failed)."""
        with self._lock:
            entry = self._cards.get(camera_id)
            return set(entry.issues) if entry is not None else set()

    def needs_attention(self, camera_id: str, message: str, pending: int) -> None:
        """The card cannot finish on its own: what it still holds is exactly the
        refused/failed clips (or its contents could not be verified). Not green."""
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_NEEDS_ATTENTION
            entry.pending_files = pending
            entry.unconfirmed_files = pending
            entry.current_file = None
            entry.error = message
            entry.updated_at = self._now()

    def uploading(
        self,
        camera_id: str,
        pending: int,
        *,
        unconfirmed: int | None = None,
        awaiting_sweep: int = 0,
    ) -> None:
        """Copied off the card, but the card is not finished with this host yet.

        Raised at the end of a pull whose card still has clips S3 has not
        confirmed, or clips that cleanup is due to sweep off it on the next pull
        (:func:`ingest.retention.card_transfer`). The operator must leave the
        card in: the hand-off runs from the staged copy, but the *sweep* that
        frees the card cannot, and a card removed here goes back into a camera
        as full as it came out.

        Not an error and not a copy: the only number it carries is
        ``pending_files``, the count the operator watches down to zero.
        """
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_UPLOADING
            entry.pending_files = pending
            entry.unconfirmed_files = pending if unconfirmed is None else unconfirmed
            entry.awaiting_sweep_files = awaiting_sweep
            entry.current_file = None
            entry.error = None
            entry.updated_at = self._now()

    def safe_to_remove(self, camera_id: str) -> None:
        """Nothing on the card depends on this host any more: take it out.

        Raised only once the pull loop has finished AND every master on the card
        is confirmed in S3 AND (with ``DELETE_AFTER_TRANSFER``) the sweep that
        frees the card has run — see the module docstring. The S3 hand-off of a
        clip that is already *off* the card, and the SkydiveOS notify, do run
        from the staged copy and are correctly not waited on here.
        """
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_SAFE_TO_REMOVE
            entry.pending_files = 0
            entry.unconfirmed_files = 0
            entry.awaiting_sweep_files = 0
            entry.current_file = None
            entry.error = None
            entry.updated_at = self._now()

    def error(self, camera_id: str, message: str) -> None:
        """The pull failed (or partially failed): the operator must look."""
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_ERROR
            entry.current_file = None
            entry.error = message
            entry.updated_at = self._now()

    # --- presence (called by ObservingScanner) ------------------------------- #

    def observe(self, mounted_ids: Iterable[str]) -> None:
        """Reconcile the registry with what the scanner currently sees mounted.

        A new id becomes ``detected``; a ``safe_to_remove`` entry whose card is
        gone is dropped (removal was the goal state), and so is an ``uploading``
        one (the operator took the card out early — the row describes a card in
        the reader, and the staged clips finish uploading without it); an
        ``error`` entry lingers ``_TERMINAL_LINGER_S`` after removal so the
        operator sees the failure. Entries mid-pull are left alone — the pull
        itself will land them in a resting state.
        """
        seen = set(mounted_ids)
        now = self._now()
        with self._lock:
            for camera_id in seen:
                if camera_id not in self._cards:
                    self._cards[camera_id] = CardStatus(
                        camera_id, STATE_DETECTED, updated_at=now
                    )
            for camera_id in list(self._cards):
                entry = self._cards[camera_id]
                if camera_id in seen or entry.state not in _STALE_PRUNABLE_STATES:
                    continue
                if entry.state in (STATE_SAFE_TO_REMOVE, STATE_UPLOADING):
                    # (needs_attention and error linger below — the operator must see them)
                    if entry.state == STATE_UPLOADING:
                        logger.warning(
                            "card %s was removed with %d clip(s) still pending; the "
                            "staged copies finish uploading without it, but the card "
                            "was not cleared — re-insert it to free the space",
                            camera_id, entry.pending_files,
                        )
                    del self._cards[camera_id]
                elif now - entry.updated_at > _TERMINAL_LINGER_S:
                    del self._cards[camera_id]

    # --- read side ------------------------------------------------------------ #

    def snapshot(self) -> list[dict[str, Any]]:
        """Every tracked card as plain dicts (JSON-ready), ordered by id.

        Also the backstop that keeps a wedged scan loop from freezing the
        operator screen: a terminal or ``uploading`` entry that hasn't been refreshed in
        ``_TERMINAL_LINGER_S`` is dropped here, because both readers of the
        registry (the ``GET /ingest/cards`` route and ``publish_card_status``)
        come through this method — so even when ``observe`` has stopped
        running, a removed card's row ages out instead of being rebroadcast
        forever. Mid-pull entries are NEVER age-pruned: a single multi-GB copy
        can legitimately go this long without a counter update, and hiding its
        "do not remove the card" line invites the yank this whole module exists
        to prevent.
        """
        now = self._now()
        with self._lock:
            for camera_id in list(self._cards):
                entry = self._cards[camera_id]
                if (
                    entry.state in _STALE_PRUNABLE_STATES
                    and now - entry.updated_at > _TERMINAL_LINGER_S
                ):
                    del self._cards[camera_id]
            return [asdict(self._cards[cid]) for cid in sorted(self._cards)]

    def _ensure(self, camera_id: str) -> CardStatus:
        """The entry for ``camera_id``, created if a hook fires before any scan."""
        entry = self._cards.get(camera_id)
        if entry is None:
            entry = CardStatus(camera_id, STATE_PULLING, updated_at=self._now())
            self._cards[camera_id] = entry
        return entry


def _quietly(fn: Callable[[], None]) -> None:
    """Run a status update; a broken progress bar must never cost a pull."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - tracking is cosmetic, footage is not
        logger.warning("card status update failed (ignored): %r", e)


class TrackedCamera(Camera):
    """A :class:`~ingest.camera.Camera` that reports pull progress as it works.

    Pure pass-through to the wrapped camera; only the masters count toward
    progress (LRV proxies and thumbnails are best-effort assets whose sizes the
    card listing doesn't carry). Every registry call is wrapped so tracking can
    never raise into the pull.
    """

    def __init__(self, inner: Camera, registry: CardStatusRegistry, camera_id: str) -> None:
        self._inner = inner
        self._registry = registry
        self._camera_id = camera_id

    async def open(self) -> None:
        await self._inner.open()

    async def close(self) -> None:
        await self._inner.close()

    async def list_videos(self) -> list[RemoteMedia]:
        videos = await self._inner.list_videos()
        _quietly(
            lambda: self._registry.totals(
                self._camera_id, len(videos), sum(m.size or 0 for m in videos)
            )
        )
        return videos

    async def download_mp4(self, media: RemoteMedia, dest: Path) -> Path:
        _quietly(lambda: self._registry.file_started(self._camera_id, media.filename))
        result = await self._inner.download_mp4(media, dest)
        _quietly(lambda: self._registry.file_done(self._camera_id, media.size or 0))
        return result

    async def download_lrv(self, media: RemoteMedia, dest: Path) -> Path:
        return await self._inner.download_lrv(media, dest)

    async def download_thumbnail(self, media: RemoteMedia, dest: Path) -> Path:
        return await self._inner.download_thumbnail(media, dest)

    async def delete_media(self, media: RemoteMedia) -> None:
        await self._inner.delete_media(media)
        # Only after a successful delete: a refused delete left the card unchanged.
        _quietly(lambda: self._registry.sweeping(self._camera_id, media.size or 0))


class ObservingScanner(CameraScanner):
    """A :class:`~ingest.scanner.CameraScanner` that mirrors presence into the registry.

    Discovery already skips scans while a pull is in flight, so :meth:`scan`
    never races the pull's own transitions.
    """

    def __init__(self, inner: CameraScanner, registry: CardStatusRegistry) -> None:
        self._inner = inner
        self._registry = registry

    async def scan(self) -> list[str]:
        ids = await self._inner.scan()
        _quietly(lambda: self._registry.observe(ids))
        return ids
