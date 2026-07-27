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

# Load a local ``.env`` (if present) into the process environment *before* anything
# reads it, so keys put there — ANTHROPIC_API_KEY, REDIS_URL, JOBS_ROOT, … — reach
# both the FastAPI app and the Celery workers without the operator exporting them by
# hand. This module is imported early by both entry points (api.app and
# api.celery_app), so doing it here covers every process. Existing env vars win over
# the file (python-dotenv's ``override=False`` default). Optional: a no-op when
# python-dotenv isn't installed or there's no .env file.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except Exception:  # pragma: no cover - python-dotenv is an optional convenience
    pass

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
    #: Start the camera auto-discovery service with the API (``ENABLE_AUTO_DISCOVERY``).
    #: Off by default — pulls stay operator/SkydiveOS-triggered until opted in.
    enable_auto_discovery: bool
    #: MongoDB connection string for the paired-camera registry (``MONGO_URL``).
    #: ``None`` disables the registry (discovery finds no known cameras).
    mongo_url: str | None
    #: Database name within Mongo that holds the ``cameras`` collection (``MONGO_DB``).
    mongo_db: str
    #: Seconds between BLE discovery sweeps (``DISCOVERY_INTERVAL_SECONDS``).
    discovery_interval: float
    #: Which scanner discovery uses (``CAMERA_SCANNER``): ``"ble"`` (real hardware,
    #: default) or ``"static"`` — a no-hardware simulation mode that scans a fixed
    #: list and stages a sample file instead of pulling a camera (see :mod:`api.app`).
    camera_scanner: str
    #: Camera ids the ``static`` scanner reports (``DISCOVERY_FAKE_CAMERAS``, comma-sep).
    discovery_fake_cameras: tuple[str, ...]
    #: Sample MP4 the simulation copies in place of a real download (``DISCOVERY_SAMPLE_MP4``).
    discovery_sample_mp4: str | None
    #: How many recordings each simulated camera reports (``DISCOVERY_SAMPLE_COUNT``).
    #: A real GoPro card holds many clips; this lets one fake camera stage several
    #: files in one pull instead of needing one fake camera per file. Defaults to 1.
    discovery_sample_count: int
    #: Enforce per-instructor access scoping using the identity SkydiveOS forwards
    #: (``ENFORCE_INSTRUCTOR_AUTH``). Off by default — every caller is treated as an
    #: admin, preserving the open behaviour. See :mod:`api.auth`.
    enforce_instructor_auth: bool
    #: S3 bucket auto-discovery uploads pulled raw masters to, then notifies SkydiveOS
    #: with the key. Read from ``S3_BUCKET`` or ``AWS_S3_BUCKET_NAME`` (SkydiveOS's
    #: name, so both apps can share one). Required when auto-discovery is enabled.
    s3_bucket: str | None
    #: Endpoint override for an S3-compatible store (``S3_ENDPOINT_URL``; e.g. MinIO).
    s3_endpoint_url: str | None
    #: AWS region for the S3 client (``AWS_REGION`` / ``AWS_DEFAULT_REGION``).
    s3_region: str | None
    #: Use a validated GoPro LRV proxy for the analysis stages (GPMF/segmentation/face
    #: scoring) when available, falling back to the MP4 otherwise (``USE_PROXY_ANALYSIS``).
    #: Off by default — analysis reads the original MP4 exactly as today. Render, photos,
    #: thumbnails, and all deliverables always use the MP4 regardless. See
    #: :mod:`analysis.proxy`.
    use_proxy_analysis: bool = False
    #: Fully-automatic delivery (``AUTO_DELIVER``): when a render finishes, the job is
    #: auto-approved and delivery is enqueued immediately — no instructor tap. Off by
    #: default, preserving the review gate; the dropzone opts in per deployment.
    auto_deliver: bool = False
    #: SMTP transport for the customer delivery email (``SMTP_HOST`` / ``SMTP_PORT`` /
    #: ``SMTP_USER`` / ``SMTP_PASSWORD``). ``smtp_host=None`` disables sending (links
    #: are still generated and reported to SkydiveOS).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    #: Upgrade the SMTP connection with STARTTLS (``SMTP_STARTTLS``, default on —
    #: matches port 587 submission; disable only for a local relay).
    smtp_starttls: bool = True
    #: From address on delivery emails (``DELIVERY_FROM_EMAIL``; falls back to
    #: ``SMTP_USER``).
    delivery_from_email: str | None = None
    #: Lifetime of the presigned download links, in days (``DELIVERY_LINK_TTL_DAYS``).
    #: Capped at 7 — the S3 SigV4 maximum for IAM-key presigning.
    delivery_link_ttl_days: float = 7.0
    #: Seconds an ``ultimum`` (two-camera) job waits for its SECOND camera's footage
    #: before a watchdog fails it (``ULTIMUM_SECOND_CAMERA_TIMEOUT_S``, default 1h).
    #: Stops a mis-mapped package or a never-arriving second camera from stranding the
    #: job in ``queued`` forever. See :func:`api.tasks.ultimum_watchdog_job`.
    ultimum_second_camera_timeout_s: float = 3600.0
    #: Shared secret sent on the status callback to SkydiveOS's receiver
    #: (``AUTO_EDIT_CALLBACK_TOKEN``); SkydiveOS optionally verifies it inbound. Sent as
    #: the ``X-Auto-Edit-Token`` header when set; ``None`` → no token header (open).
    auto_edit_callback_token: str | None = None
    #: IANA timezone the GoPro cameras' clocks are set to (``CAMERA_CLOCK_TZ``, e.g.
    #: ``America/Toronto``). GoPro writes the camera's LOCAL wall-clock into the MP4's
    #: ``creation_time`` and ffprobe mislabels it ``Z`` (UTC); when this is set, discovery
    #: reinterprets that timestamp as local time here and emits a TRUE-UTC ``captured_at``
    #: so SkydiveOS's DZ-local footage↔booking match lines up. ``None`` → pass the tag
    #: through as-is (assume it is already UTC).
    camera_clock_tz: str | None = None


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


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
        enable_auto_discovery=_flag("ENABLE_AUTO_DISCOVERY"),
        mongo_url=os.environ.get("MONGO_URL") or None,
        mongo_db=os.environ.get("MONGO_DB") or "skydiveos",
        discovery_interval=float(os.environ.get("DISCOVERY_INTERVAL_SECONDS") or 30.0),
        camera_scanner=(os.environ.get("CAMERA_SCANNER") or "ble").strip().lower(),
        discovery_fake_cameras=tuple(
            c.strip()
            for c in (os.environ.get("DISCOVERY_FAKE_CAMERAS") or "").split(",")
            if c.strip()
        ),
        discovery_sample_mp4=os.environ.get("DISCOVERY_SAMPLE_MP4") or None,
        discovery_sample_count=max(1, int(os.environ.get("DISCOVERY_SAMPLE_COUNT") or 1)),
        enforce_instructor_auth=_flag("ENFORCE_INSTRUCTOR_AUTH"),
        s3_bucket=os.environ.get("S3_BUCKET") or os.environ.get("AWS_S3_BUCKET_NAME") or None,
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        s3_region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None,
        use_proxy_analysis=_flag("USE_PROXY_ANALYSIS"),
        auto_deliver=_flag("AUTO_DELIVER"),
        smtp_host=os.environ.get("SMTP_HOST") or None,
        smtp_port=int(os.environ.get("SMTP_PORT") or 587),
        smtp_user=os.environ.get("SMTP_USER") or None,
        smtp_password=os.environ.get("SMTP_PASSWORD") or None,
        smtp_starttls=_flag("SMTP_STARTTLS", default=True),
        delivery_from_email=os.environ.get("DELIVERY_FROM_EMAIL") or None,
        delivery_link_ttl_days=min(
            7.0, float(os.environ.get("DELIVERY_LINK_TTL_DAYS") or 7.0)
        ),
        ultimum_second_camera_timeout_s=float(
            os.environ.get("ULTIMUM_SECOND_CAMERA_TIMEOUT_S") or 3600.0
        ),
        auto_edit_callback_token=os.environ.get("AUTO_EDIT_CALLBACK_TOKEN") or None,
        camera_clock_tz=os.environ.get("CAMERA_CLOCK_TZ") or None,
    )
