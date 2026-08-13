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

    def enqueue_selfie_processing(self, job_id: str) -> None:
        """Queue the multi-clip selfie-package scene pipeline for a job."""
        ...

    def enqueue_media_ref_processing(self, job_id: str, role: str) -> None:
        """Queue ONE media product's render on a mixed job, from ONE camera's footage.

        A jumper holding two products (a paid handcam edit plus a speculative
        camera-flyer one) renders each independently onto the same job, so the paid edit
        ships without waiting on the spec card.
        """
        ...

    def enqueue_rerender(self, job_id: str) -> None:
        """Queue a re-render of an already-tweaked job's persisted EDL."""
        ...

    def enqueue_delivery(self, job_id: str) -> None:
        """Queue delivery of an approved job to the customer."""
        ...

    def enqueue_load_fan_out(self, job_id: str) -> None:
        """Queue the fan-out of an approved load master to its load's customers.

        The load-master counterpart of :meth:`enqueue_delivery`: a spec flight has no
        customer of its own, so what an approval releases is one gallery offer per jumper
        on the manifest.
        """
        ...

    def enqueue_pull(self, job_id: str, camera_id: str) -> None:
        """Queue an Open GoPro pull to source a job from a camera."""
        ...

    def enqueue_s3_ingest(
        self, job_id: str, s3_key: str, camera_role: str | None = None
    ) -> None:
        """Queue a download of a raw master already staged in S3 (auto-discovery path)."""
        ...

    def arm_ultimum_watchdog(self, job_id: str, countdown: float) -> None:
        """Schedule the stranded-Ultimate check for ``countdown`` seconds from now."""
        ...


class CeleryJobQueue:
    """Production :class:`JobQueue` — dispatches to the Celery tasks via ``.delay``."""

    def enqueue_processing(self, job_id: str) -> None:
        from .tasks import process_job

        process_job.delay(job_id)

    def enqueue_selfie_processing(self, job_id: str) -> None:
        from .tasks import process_selfie_package

        process_selfie_package.delay(job_id)

    def enqueue_media_ref_processing(self, job_id: str, role: str) -> None:
        from .tasks import process_media_ref_job

        process_media_ref_job.delay(job_id, role)

    def enqueue_rerender(self, job_id: str) -> None:
        from .tasks import rerender_job

        rerender_job.delay(job_id)

    def enqueue_delivery(self, job_id: str) -> None:
        from .tasks import deliver_job

        deliver_job.delay(job_id)

    def enqueue_load_fan_out(self, job_id: str) -> None:
        from .tasks import fan_out_load_job

        fan_out_load_job.delay(job_id)

    def enqueue_pull(self, job_id: str, camera_id: str) -> None:
        from .tasks import pull_camera_job

        pull_camera_job.delay(job_id, camera_id)

    def enqueue_s3_ingest(
        self, job_id: str, s3_key: str, camera_role: str | None = None
    ) -> None:
        from .tasks import ingest_s3_job

        ingest_s3_job.delay(job_id, s3_key, camera_role)

    def arm_ultimum_watchdog(self, job_id: str, countdown: float) -> None:
        from .tasks import ultimum_watchdog_job

        ultimum_watchdog_job.apply_async((job_id,), countdown=countdown)
