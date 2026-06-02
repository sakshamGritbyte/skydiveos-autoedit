"""Runtime configuration for the /api service.

A tiny, env-driven settings object so the FastAPI app, the Celery workers, and
the tests all read the same knobs from one place. Everything is resolved from the
environment documented in ``.env.example`` (``REDIS_URL``, ``JOBS_ROOT``,
``SKYDIVEOS_API_BASE``, ...) with sensible local-dev defaults, mirroring how
``edl.storage`` / ``ingest.storage`` resolve their roots.

Kept deliberately dependency-free (plain stdlib + a frozen dataclass) so importing
it never pulls in FastAPI or Celery — the worker process imports this too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

# Local Redis matches .env.example's default broker; the same instance backs both
# the Celery broker/result store and /ingest's ready_for_processing list.
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


@dataclass(frozen=True)
class Settings:
    """Resolved service configuration (immutable; one instance per process)."""

    redis_url: str
    #: Jobs storage root. ``None`` defers to ``edl.storage`` ($JOBS_ROOT/./jobs)
    #: so the API, Compose, and Render all agree on where a job's files live.
    jobs_root: str | None
    #: SkydiveOS web layer base URL; the delivery step calls back here on state
    #: changes. ``None`` disables the callback (logs only) — handy in dev/tests.
    skydiveos_api_base: str | None
    #: Run Celery tasks inline (no broker/worker). Off by default; set
    #: ``CELERY_TASK_ALWAYS_EAGER=1`` for a single-process dev/demo run.
    task_always_eager: bool


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolve settings from the environment once and cache the result.

    Cached so every request/task sees a consistent snapshot; tests that tweak the
    environment call :func:`get_settings.cache_clear` (or override the FastAPI
    dependency) to pick up changes.
    """
    return Settings(
        redis_url=os.environ.get("REDIS_URL", DEFAULT_REDIS_URL),
        jobs_root=os.environ.get("JOBS_ROOT") or None,
        skydiveos_api_base=os.environ.get("SKYDIVEOS_API_BASE") or None,
        task_always_eager=_flag("CELERY_TASK_ALWAYS_EAGER"),
    )
