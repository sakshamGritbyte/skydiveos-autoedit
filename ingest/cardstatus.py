"""Live per-card ingest status — what tells the operator "safe to remove".

An SD-card pull is the one ingest transport where a human is standing at the
machine waiting to take the media back: the card must stay in the reader while
the pull is copying (and, with ``DELETE_AFTER_TRANSFER``, sweeping) it, and can
be removed the moment the pull loop finishes — the S3 upload + SkydiveOS notify
run *afterwards* from the local staging copy and never touch the card again
until it is re-inserted. This module makes that moment observable:

* :class:`CardStatusRegistry` — an in-memory, thread-safe map of
  ``camera_id -> CardStatus`` that the pull path updates through its lifecycle
  (``detected → sweeping/pulling → safe_to_remove | error``). The API serves a
  snapshot of it at ``GET /ingest/cards``; the SkydiveOS front end polls that
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .camera import Camera, RemoteMedia
from .scanner import CameraScanner

logger = logging.getLogger(__name__)

#: The card-ingest lifecycle. ``safe_to_remove`` and ``error`` are terminal.
STATE_DETECTED = "detected"
STATE_SWEEPING = "sweeping"
STATE_PULLING = "pulling"
STATE_SAFE_TO_REMOVE = "safe_to_remove"
STATE_ERROR = "error"

_TERMINAL_STATES = frozenset({STATE_SAFE_TO_REMOVE, STATE_ERROR})

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
    current_file: str | None = None
    error: str | None = None
    #: Epoch seconds of the last transition (repo convention: seconds, floats).
    updated_at: float = 0.0


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

    def pull_started(self, camera_id: str) -> None:
        """A pull is opening the card: reset the counters for this run.

        Deliberately does NOT downgrade a ``safe_to_remove`` entry: discovery
        re-pulls a card that lingers in the reader on every scan tick, and an
        idempotent re-pull of a fully staged card skips everything — flapping
        the badge back to "pulling" each tick would train the operator to
        ignore it. Real card activity (a sweep delete, a download) still flips
        the state via :meth:`sweeping` / :meth:`file_started`.
        """
        with self._lock:
            entry = self._cards.get(camera_id)
            state = (
                STATE_SAFE_TO_REMOVE
                if entry is not None and entry.state == STATE_SAFE_TO_REMOVE
                else STATE_PULLING
            )
            self._cards[camera_id] = CardStatus(camera_id, state, updated_at=self._now())

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

    def safe_to_remove(self, camera_id: str) -> None:
        """The pull loop finished and the camera closed: the card is idle.

        This is the "remove it now" signal. It deliberately does not wait for
        the S3 upload / SkydiveOS notify — those run from the *staged* copy on
        local disk (``ingest.discovery._materialize``) and touch the card only
        on its next insertion (the retention sweep).
        """
        with self._lock:
            entry = self._ensure(camera_id)
            entry.state = STATE_SAFE_TO_REMOVE
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
        gone is dropped (removal was the goal state); an ``error`` entry
        lingers ``_TERMINAL_LINGER_S`` after removal so the operator sees the
        failure. Entries mid-pull are left alone — the pull itself will land
        them in a terminal state.
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
                if camera_id in seen or entry.state not in _TERMINAL_STATES:
                    continue
                if entry.state == STATE_SAFE_TO_REMOVE:
                    del self._cards[camera_id]
                elif now - entry.updated_at > _TERMINAL_LINGER_S:
                    del self._cards[camera_id]

    # --- read side ------------------------------------------------------------ #

    def snapshot(self) -> list[dict[str, Any]]:
        """Every tracked card as plain dicts (JSON-ready), ordered by id.

        Also the backstop that keeps a wedged scan loop from freezing the
        operator screen: a TERMINAL entry that hasn't been refreshed in
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
                    entry.state in _TERMINAL_STATES
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
