"""The job queue seam between the REST layer and Celery.

The endpoints depend on this small :class:`JobQueue` interface rather than calling
``task.delay`` directly. That keeps the handlers free of Celery specifics and —
following the project's injectable-dependency style (``Camera``, ``ClaudeClient``,
``EventEmitter``) — lets tests substitute a recording fake to assert *what* was
enqueued without standing up a broker or running the heavy pipeline.

:class:`CeleryJobQueue` is the production implementation; it simply dispatches to
the tasks in :mod:`api.tasks`.
"""

from __future__ import annotations

from typing import Protocol


class JobQueue(Protocol):
    """What the REST layer needs from the async backend: enqueue, don't run."""

    def enqueue_processing(self, job_id: str) -> None:
        """Queue the full edit pipeline for a freshly-sourced job."""
        ...

    def enqueue_rerender(self, job_id: str) -> None:
        """Queue a re-render of an already-tweaked job's persisted EDL."""
        ...

    def enqueue_delivery(self, job_id: str) -> None:
        """Queue delivery of an approved job to the customer."""
        ...

    def enqueue_pull(self, job_id: str, camera_id: str) -> None:
        """Queue an Open GoPro pull to source a job from a camera."""
        ...


class CeleryJobQueue:
    """Production :class:`JobQueue` — dispatches to the Celery tasks via ``.delay``."""

    def enqueue_processing(self, job_id: str) -> None:
        from .tasks import process_job

        process_job.delay(job_id)

    def enqueue_rerender(self, job_id: str) -> None:
        from .tasks import rerender_job

        rerender_job.delay(job_id)

    def enqueue_delivery(self, job_id: str) -> None:
        from .tasks import deliver_job

        deliver_job.delay(job_id)

    def enqueue_pull(self, job_id: str, camera_id: str) -> None:
        from .tasks import pull_camera_job

        pull_camera_job.delay(job_id, camera_id)
