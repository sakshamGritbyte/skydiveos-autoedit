"""The ``ready_for_processing`` event that hands a staged jump to the pipeline.

Ingest is stage 1; once a jump's media is on local disk it must tell the job
queue so the Segment stage can pick it up (CLAUDE.md: Celery + Redis). We keep
the producer decoupled from the consumer by publishing a small JSON event rather
than importing a Celery app here:

* :class:`RedisEventEmitter` ``LPUSH``es the event onto a Redis list a worker
  ``BRPOP``s — the canonical job-queue handoff.
* :class:`FileEventEmitter` appends newline-delimited JSON to a local file, used
  as a graceful fallback when ``$REDIS_URL`` is unset (local dev) and in tests.

A successful download must never be lost because the queue is down, so
:func:`default_emitter` falls back to a file rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVENT_NAME = "ready_for_processing"
DEFAULT_QUEUE = "ingest:ready"
_EVENTS_FILENAME = "_events.jsonl"


def build_event(
    *,
    job_id: str,
    camera_id: str,
    jump_dir: Path,
    mp4_path: Path,
    lrv_path: Path | None,
    thumbnail_path: Path | None,
    created_epoch: float | None,
    emitted_at: float,
) -> dict[str, Any]:
    """Assemble the ``ready_for_processing`` payload for one staged jump.

    Paths are stringified (JSON has no Path type). ``job_id`` is deterministic
    per jump so downstream stages stay idempotent (CLAUDE.md: one job per jump).
    """
    return {
        "event": EVENT_NAME,
        "job_id": job_id,
        "camera_id": camera_id,
        "jump_dir": str(jump_dir),
        "files": {
            "mp4": str(mp4_path),
            "lrv": str(lrv_path) if lrv_path else None,
            "thumbnail": str(thumbnail_path) if thumbnail_path else None,
        },
        "created_epoch": created_epoch,
        "emitted_at": emitted_at,
    }


class EventEmitter(ABC):
    """Sink for ingest events."""

    @abstractmethod
    def emit(self, event: dict[str, Any]) -> None:
        """Publish one event. Implementations must not raise on transport hiccups
        once the media is safely on disk — log and degrade instead."""


class RedisEventEmitter(EventEmitter):
    """Publish events by ``LPUSH``-ing JSON onto a Redis list (a worker BRPOPs).

    ``redis`` is imported lazily so this module imports without a broker present.
    """

    def __init__(self, url: str, queue: str = DEFAULT_QUEUE) -> None:
        self._url = url
        self._queue = queue

    def emit(self, event: dict[str, Any]) -> None:
        try:
            import redis  # local import: optional at module-import time

            client = redis.Redis.from_url(self._url)
            client.lpush(self._queue, json.dumps(event))
            logger.info("emitted %s for %s -> %s", event["event"], event["job_id"], self._queue)
        except Exception as e:  # noqa: BLE001 - never lose a download over a queue blip
            logger.error("failed to publish event for %s to redis: %r", event.get("job_id"), e)


class FileEventEmitter(EventEmitter):
    """Append events as newline-delimited JSON to a local file (dev/test/fallback)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        logger.info("emitted %s for %s -> %s", event["event"], event["job_id"], self._path)


def default_emitter(root: Path, queue: str = DEFAULT_QUEUE) -> EventEmitter:
    """Redis emitter when ``$REDIS_URL`` is set, else a file fallback under ``root``."""
    url = os.environ.get("REDIS_URL")
    if url:
        return RedisEventEmitter(url, queue)
    logger.warning("REDIS_URL not set; writing ingest events to %s/%s", root, _EVENTS_FILENAME)
    return FileEventEmitter(root / _EVENTS_FILENAME)
