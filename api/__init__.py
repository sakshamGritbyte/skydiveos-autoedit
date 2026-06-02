"""/api — the FastAPI service SkydiveOS calls to drive the auto-edit pipeline.

The REST app (:mod:`api.app`) is a thin dispatcher: it persists per-job state
(:mod:`api.jobs`) and enqueues the heavy stages onto Celery (:mod:`api.celery_app`,
:mod:`api.tasks`) via a swappable queue (:mod:`api.queue`). Serve it with
``uvicorn api.app:app`` and run a worker with
``celery -A api.celery_app.celery_app worker``.
"""

from __future__ import annotations

from .app import app, create_app
from .celery_app import celery_app
from .jobs import Job, JobStatus, JobStore

__all__ = ["app", "create_app", "celery_app", "Job", "JobStatus", "JobStore"]
