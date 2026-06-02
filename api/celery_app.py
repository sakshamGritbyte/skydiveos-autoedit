"""The Celery application that runs the pipeline off the request path.

The REST endpoints must return immediately — segmenting, scoring, composing and
rendering a jump takes minutes and runs on GPU workers (CLAUDE.md: Celery + Redis,
scale-to-zero). So /api only ever *enqueues*; the work happens here, in a separate
worker process, against the same Redis as the broker and result backend.

Run a worker with::

    celery -A api.celery_app.celery_app worker -l info

Tasks live in :mod:`api.tasks` and are imported via ``include`` so this module
stays import-light (the FastAPI process imports it to enqueue, but should not need
to import the heavy pipeline deps just to call ``.delay``).
"""

from __future__ import annotations

from celery import Celery

from .config import get_settings

_settings = get_settings()

celery_app = Celery(
    "skydiveos_autoedit",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["api.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # One jump per task; long renders shouldn't be silently retried mid-encode.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Dev/demo escape hatch: run tasks inline in the calling process.
    task_always_eager=_settings.task_always_eager,
    task_eager_propagates=_settings.task_always_eager,
)
