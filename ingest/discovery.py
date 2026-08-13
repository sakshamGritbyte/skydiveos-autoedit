"""Auto-discovery: BLE-scan for paired GoPros and ingest them with no human in the loop.

Today a pull only happens when an operator runs the CLI or SkydiveOS POSTs an
upload (see :mod:`ingest.pull`, :mod:`api.app`). :class:`CameraDiscoveryService`
closes that gap: it runs alongside the API and, on a fixed interval, pulls any
*paired* camera that comes into range.

The loop, end to end:

1. **Scan** — ask the injected :class:`~ingest.scanner.CameraScanner` which GoPros
   are reachable (a BLE sweep in production).
2. **Filter** — intersect with the paired-camera allow-list in the
   :class:`~ingest.registry.CameraRegistry` (so a stranger's GoPro is ignored).
3. **Pull** — for each known camera *not already being pulled*, run the existing
   :func:`ingest.pull.pull_camera` unchanged (design decision (b): pull directly, no
   Job yet). Pulls are serialized behind one lock — a host has a single WiFi
   interface and can only join one camera's access point at a time — and
   de-duplicated per camera so a camera that lingers in range isn't pulled twice.
4. **Hand off** — ``pull_camera`` emits one ``ready_for_processing`` event per
   *newly downloaded* jump (already-staged jumps emit nothing, which naturally
   dedupes hand-offs across scans). Those events are routed to an in-process queue
   instead of Redis; a second loop drains them and, *after* the pull, uploads the
   MP4 to S3 and POSTs a small JSON
   ``{s3_key, camera_id, instructor_id?, camera_role?, captured_at?, staff_id?,
   staff_source?}`` to SkydiveOS (``{SKYDIVEOS}/api/media/raw-upload``). SkydiveOS
   creates the media record from the key — large videos never stream through the web
   layer, and discovery never creates a job itself. In SD-card mode an
   ``identity_resolver`` (:func:`ingest.qr.qr_identity_resolver`) supplies
   ``staff_id`` from the filmed QR session marker; the marker clip itself is uploaded
   under ``markers/`` (retention bookkeeping only) and never notified. Because the
   marker may be filmed at the *end* of a session (attribution is card-level, see
   :mod:`ingest.qr`), a pull with an identity resolver **holds its events until the
   whole card is staged** — hand-offs run per clip only in the marker-less modes.

Nothing in :func:`ingest.pull.pull_camera` is modified; the service only triggers
it and consumes its events. Start/stop are async (use it from a FastAPI lifespan);
under an ASGI server SIGTERM drives the server's shutdown, which awaits
:meth:`stop`. For standalone use, pass ``install_signal_handlers=True`` to have the
service install its own SIGTERM/SIGINT handlers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .events import EventEmitter
from .qr import ClipIdentity  # light import — cv2 stays lazy inside the decode path
from .registry import CameraRegistry
from .retention import record_uploaded
from .scanner import CameraScanner

logger = logging.getLogger(__name__)

#: Default seconds between BLE scans (overridable via ``DISCOVERY_INTERVAL_SECONDS``).
DEFAULT_INTERVAL = 30.0
#: SkydiveOS path the S3-key notification is POSTed to (it creates the media record).
RAW_UPLOAD_PATH = "/api/media/raw-upload"
#: SkydiveOS path the card-ingest snapshot is PUSHED to (see :func:`publish_card_status`).
CARD_STATUS_PATH = "/api/media/ingest-cards/status"
#: Seconds between snapshot pushes while any card is tracked.
CARD_STATUS_INTERVAL = 2.0
#: Event key counting failed hand-off attempts (see ``_schedule_handoff_retry``).
_HANDOFF_ATTEMPTS_KEY = "_handoff_attempts"

#: Ceiling on the exponential backoff between hand-off retries, in seconds.
_HANDOFF_MAX_DELAY = 300.0


def _is_permanent_handoff_error(error: Exception) -> bool:
    """True when re-sending this notification cannot succeed (an HTTP 4xx).

    The retry ladder exists for a hand-off that lost the *network* (the radio is joined
    to a camera's AP, S3 is unreachable). A 4xx is the opposite: SkydiveOS received the
    payload and rejected it, so every retry gets the same answer. 429 is excluded — it
    means "later", which is exactly what a retry does. Duck-typed on ``response.status``
    /``status_code`` so it holds for httpx, requests and botocore alike, without
    importing any of them here.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status", None)
    if not isinstance(status, int):
        return False
    return 400 <= status < 500 and status != 429

#: S3 key prefix for pulled raw masters: ``{prefix}/{camera_id}/{filename}``.
S3_KEY_PREFIX = "raw"

#: A coroutine that runs a pull for one camera, accepting an ``emitter=`` sink.
#: ``ingest.pull.pull_camera`` satisfies this; tests inject a fake.
PullFn = Callable[..., Awaitable[Any]]
#: Hands one pulled jump to SkydiveOS:
#: ``(mp4_path, camera_id, instructor_id, camera_role) -> s3_key | None``. ``camera_role``
#: is ``instructor``/``external`` for a two-camera (Ultimate) jump, else ``None``. The
#: default (:func:`s3_notify_uploader`) PUTs to S3 then notifies; tests inject a recorder.
#: Returning the object key is what lets the card be cleared later: it is the proof that
#: S3 holds the footage, recorded via :func:`ingest.retention.record_uploaded` and
#: required before the next pull may delete that file (returning ``None`` keeps it).
#: Implementations must also accept two keywords from the QR session flow:
#: ``staff_id`` (the QR-resolved ``staffs._id``, forwarded in the notify payload) and
#: ``marker=True`` (a QR session-marker clip: PUT under ``{prefix}/{camera_id}/markers/``
#: purely so the retention rule holds, and do NOT notify SkydiveOS).
UploadFn = Callable[..., str | None]
#: Resolves a pulled clip's *authoritative* camera role from the load:
#: ``(camera_id, mp4_path) -> "instructor" | "external" | None``. ``None`` means "couldn't
#: resolve — fall back to the registry's static role hint". :func:`matcher_role_resolver`
#: adapts an :class:`ingest.match.FootageMatcher` to this shape.
RoleResolver = Callable[[str, str], str | None]
#: Resolves a pulled clip's *identity* — who filmed it — from the QR session flow:
#: ``(camera_id, mp4_path) -> ClipIdentity | None`` (:func:`ingest.qr.qr_identity_resolver`
#: builds one). ``None``/``staff_id=None`` mean "no opinion — behave exactly as without
#: it"; ``is_qr_marker=True`` means the clip is a session marker, never a job.
IdentityResolver = Callable[[str, str], "ClipIdentity | None"]


def matcher_role_resolver(matcher: Any, *, clock_tz: str | None = None) -> RoleResolver:
    """Adapt an :class:`~ingest.match.FootageMatcher` into a discovery role resolver.

    The returned callable probes the clip's capture time and asks the matcher which
    load-jumper slot the camera's owner filled *on that jump* — the authoritative role,
    correct even when one staff member is the tandem instructor on one jump and the
    cameraman on the next (the same physical GoPro, so the registry's static role can't
    express it). Returns ``None`` — never raises — when the capture time is unreadable or
    the match is unavailable / unknown / ambiguous, so the caller falls back to the
    registry's static hint rather than blocking or guessing a hand-off.
    """
    from .match import FootageMatchError

    def _resolve(camera_id: str, mp4_path: str) -> str | None:
        captured_at = _probe_capture_time(mp4_path, clock_tz=clock_tz)
        if captured_at is None:
            return None
        try:
            return matcher.resolve(camera_id, captured_at).role
        except FootageMatchError as e:
            logger.warning(
                "load-based role resolution failed for camera %s (%r); falling back to "
                "the registry's static role hint",
                camera_id, e,
            )
            return None

    return _resolve


def _probe_capture_time(mp4_path: str, *, clock_tz: str | None = None) -> str | None:
    """Best-effort ISO-8601 UTC capture time from the MP4 container (``creation_time``).

    The deterministic footage→booking match on the SkydiveOS side keys on
    ``camera_id`` + *when the jump was filmed*; the MP4's ``creation_time`` tag (set by
    the GoPro at recording start) is that wall-clock in one ``ffprobe`` call, no GPMF
    parse needed. Returns ``None`` — never raises — when the tag is missing, ffprobe is
    unavailable, or the file is unreadable, so a hand-off is never blocked by it (the
    matcher then falls back to its load-window logic).

    ``clock_tz`` (an IANA name, e.g. ``America/Toronto``) handles the GoPro quirk that
    ``creation_time`` is the camera's LOCAL wall-clock mislabelled ``Z``: when given, the
    timestamp is reinterpreted as local time in that zone and converted to TRUE UTC, so
    the value SkydiveOS receives is a real instant. Omit it only if the cameras' clocks
    are actually set to UTC.
    """
    import json
    import subprocess

    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=creation_time",
                "-of", "json",
                mp4_path,
            ],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        raw = json.loads(out).get("format", {}).get("tags", {}).get("creation_time")
        if not raw:
            return None
        return _to_true_utc(raw, clock_tz)
    except Exception as e:  # noqa: BLE001 - capture time is optional; never block a hand-off
        logger.warning("could not read capture time from %s: %r", mp4_path, e)
        return None


def _to_true_utc(raw: str, clock_tz: str | None) -> str:
    """Reinterpret a GoPro ``creation_time`` as true UTC using ``clock_tz``.

    Without ``clock_tz`` the tag is passed through unchanged (assumed already UTC). With
    it, the wall-clock digits are treated as local time in that zone (the trailing ``Z``
    or offset the GoPro/ffprobe wrote is a lie) and converted to UTC. Best-effort: any
    parse/zone error returns ``raw`` so a hand-off is never blocked by it.
    """
    if not clock_tz:
        return raw
    import re
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    try:
        naive = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", raw)  # drop the misleading tz label
        local = datetime.fromisoformat(naive).replace(tzinfo=ZoneInfo(clock_tz))
        return local.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except Exception as e:  # noqa: BLE001 - never block a hand-off on a tz/parse hiccup
        logger.warning("could not convert capture time %r via %s: %r", raw, clock_tz, e)
        return raw


#: Matches the ``YYYY-MM-DD`` staging day directory a pull writes into
#: (``raw-storage/_camera-staging/<camera_id>/<date>/CLIP.MP4``).
_STAGING_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _capture_day(mp4_path: str | Path) -> str:
    """The clip's capture day (``YYYY-MM-DD``) for scoping its S3 key.

    Taken from the staging directory the pull already chose from the clip's
    ``created_epoch`` (``ingest.storage.jump_dir``) — no second ffprobe, and consistent
    with where the file lives on disk. Falls back to the file's mtime, then to
    ``unknown-date``, because a key must always be constructible: an unscoped key is the
    collision this function exists to prevent.
    """
    path = Path(mp4_path)
    if _STAGING_DAY_RE.match(path.parent.name):
        return path.parent.name
    try:
        from datetime import datetime

        return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except OSError:
        return "unknown-date"


def _file_fingerprint(mp4_path: str | Path, *, chunk: int = 1 << 20) -> str:
    """A short, deterministic content fingerprint: size + sha256 of the first ``chunk``.

    Deterministic so a re-upload of the SAME file lands on the SAME key (idempotency is
    what makes a retried hand-off safe), while a *different* file that happens to share a
    filename gets a different one. Only the head is hashed — a 4 GB master must not be
    read twice per pull, and the first megabyte of an MP4 carries the moov/mdat header
    that differs between recordings.
    """
    import hashlib

    path = Path(mp4_path)
    digest = hashlib.sha256()
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            digest.update(fh.read(chunk))
    except OSError as e:  # pragma: no cover - unreadable file fails at upload anyway
        logger.warning("could not fingerprint %s (%r); using the name only", path, e)
        return "nofp"
    digest.update(str(size).encode())
    return digest.hexdigest()[:12]


def _file_size(path: str | Path) -> int | None:
    """Size of a staged file, or ``None`` if it can't be read (never raises)."""
    try:
        return Path(path).stat().st_size
    except OSError as e:  # pragma: no cover - the upload itself would have failed first
        logger.warning("could not size %s: %r", path, e)
        return None


def _object_size(client: Any, bucket: str, key: str) -> int | None:
    """Size of an existing S3 object, or ``None`` when it isn't there / can't be read."""
    try:
        return int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    except Exception as e:  # noqa: BLE001 - any "not there / no permission" is the same answer
        logger.debug("head_object %s: %r", key, e)
        return None


def raw_object_key(
    mp4_path: str | Path,
    camera_id: str,
    *,
    key_prefix: str = S3_KEY_PREFIX,
    marker: bool = False,
    exists: Callable[[str], int | None] | None = None,
) -> str:
    """The S3 key for one pulled clip — scoped so two clips can never collide.

    The old layout was ``raw/{camera_id}/{FILENAME}``, and GoPro filenames are **not
    unique**: a formatted or replaced card restarts at ``GX010001.MP4``, and two unlabeled
    cards both identify as ``sd-NO-NAME``. One clip therefore overwrote another customer's
    master, and the notify consumer — which dedupes on the key — then dropped the new clip
    as a duplicate, losing it silently (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-F).

    So the key carries the clip's **capture day**::

        raw/{camera_id}/{YYYY-MM-DD}/{FILENAME}
        raw/{camera_id}/{YYYY-MM-DD}/markers/{FILENAME}     (a QR session marker)

    and, when ``exists`` reports an object already at that key with a **different size**,
    a deterministic content fingerprint is inserted to keep both::

        raw/{camera_id}/{YYYY-MM-DD}/{fingerprint}/{FILENAME}

    Two deliberate properties:

    * **The basename stays the GoPro filename.** Every consumer treats the key as opaque
      but derives the local staging name from ``Path(key).name`` (``api.tasks.
      ingest_s3_job``, the bridge's temp download, ``Job.raw_s3_keys``), and the archive
      shows those names to humans. Scoping goes in the *path*, never the filename.
    * **Same file → same key.** A re-upload after a failed notify overwrites itself
      instead of piling up copies, which is what keeps the hand-off retry safe.
    """
    name = Path(mp4_path).name
    day = _capture_day(mp4_path)
    base = f"{key_prefix}/{camera_id}/{day}"
    if marker:
        base = f"{base}/markers"
    key = f"{base}/{name}"
    if exists is None:
        return key
    existing = exists(key)
    if existing is None:
        return key
    try:
        if existing == Path(mp4_path).stat().st_size:
            return key  # same clip, re-uploaded (a retried hand-off) — idempotent
    except OSError:
        return key
    scoped = f"{base}/{_file_fingerprint(mp4_path)}/{name}"
    logger.warning(
        "S3 key %s already holds a DIFFERENT %d-byte object; storing this clip at %s "
        "instead of overwriting it (reused GoPro filename — a formatted/replaced card)",
        key, existing, scoped,
    )
    return scoped


def s3_notify_uploader(
    skydiveos_url: str,
    *,
    bucket: str,
    s3_client: Any | None = None,
    endpoint_url: str | None = None,
    region_name: str | None = None,
    key_prefix: str = S3_KEY_PREFIX,
    path: str = RAW_UPLOAD_PATH,
    timeout: float = 30.0,
    clock_tz: str | None = None,
) -> UploadFn:
    """Build the default uploader: PUT the pulled MP4 to S3, then notify SkydiveOS.

    Uploads the file to ``s3://{bucket}/{key_prefix}/{camera_id}/{name}`` (boto3's
    ``upload_file`` multiparts large videos automatically), then POSTs a small JSON
    ``{s3_key, camera_id, instructor_id?, camera_role?, captured_at?}`` to
    ``{skydiveos_url}{path}`` — SkydiveOS creates the media record from the key, so big
    files never stream through the web layer. ``captured_at`` (the MP4's ISO-8601 UTC
    ``creation_time``) lets SkydiveOS match the footage to the right jump/booking
    deterministically by camera + capture time; it is omitted when unreadable. The S3
    client is created once on first use (``boto3``/``httpx`` imported
    lazily); pass ``s3_client`` to inject a fake in tests. Raises on a non-2xx
    notify response so the caller can log a failed hand-off.
    """
    client_holder: dict[str, Any] = {"client": s3_client}

    def _client() -> Any:
        if client_holder["client"] is None:
            import boto3

            client_holder["client"] = boto3.client(
                "s3", endpoint_url=endpoint_url, region_name=region_name
            )
        return client_holder["client"]

    def _upload(
        mp4_path: str,
        camera_id: str,
        instructor_id: str | None,
        camera_role: str | None = None,
        *,
        staff_id: str | None = None,
        marker: bool = False,
    ) -> str:
        import httpx

        if marker:
            # A QR session marker is never a job: PUT it under markers/ purely so the
            # retention rule ("deletable only once S3 confirmed it") stays literally
            # true and the sweep can clear it off the card — but never notify.
            key = raw_object_key(
                mp4_path, camera_id, key_prefix=key_prefix, marker=True,
                exists=lambda k: _object_size(_client(), bucket, k),
            )
            _client().upload_file(mp4_path, bucket, key)
            return key

        key = raw_object_key(
            mp4_path, camera_id, key_prefix=key_prefix,
            exists=lambda k: _object_size(_client(), bucket, k),
        )
        _client().upload_file(mp4_path, bucket, key)

        payload: dict[str, Any] = {"s3_key": key, "camera_id": camera_id}
        if instructor_id is not None:
            payload["instructor_id"] = instructor_id
        if camera_role is not None:
            payload["camera_role"] = camera_role
        if staff_id is not None:
            # The QR-resolved staffs._id: SkydiveOS matches by staff + capture time
            # and skips its goproSerial lookup (the card may be from any camera).
            payload["staff_id"] = staff_id
            payload["staff_source"] = "qr"
        captured_at = _probe_capture_time(mp4_path, clock_tz=clock_tz)
        if captured_at is not None:
            payload["captured_at"] = captured_at
        # The consumer is internet-facing (the ingest machine POSTs to it across the
        # public network), and this notify is enough on its own to create a job and
        # email a customer — so it carries the same service token every other call to
        # our own API does. Empty when AUTO_EDIT_API_KEY is unset, exactly like the
        # gate itself: opt-in, and never a new failure mode for an existing deployment.
        from api.auth import (
            service_auth_headers,  # noqa: PLC0415 - lazy: keeps /ingest import-light
        )

        resp = httpx.post(
            f"{skydiveos_url.rstrip('/')}{path}",
            json=payload,
            timeout=timeout,
            headers=service_auth_headers(),
        )
        resp.raise_for_status()
        # Both S3 and SkydiveOS accepted it — the key is now proof the footage is safe.
        return key

    return _upload


async def publish_card_status(
    registry: Any,
    skydiveos_url: str,
    *,
    path: str = CARD_STATUS_PATH,
    interval: float = CARD_STATUS_INTERVAL,
    timeout: float = 5.0,
) -> None:
    """PUSH the card-ingest snapshot to SkydiveOS while any card is tracked.

    **Why a push.** The registry is in-memory and per-process, so ``GET /ingest/cards``
    only answers on the machine running discovery — the dropzone box with the card
    reader. Production deliberately splits the pipeline: that box has the reader, the
    cloud instance does the rendering with ``ENABLE_AUTO_DISCOVERY=0``, and SkydiveOS has
    a single auto-edit base URL pointing at the cloud. So a *pull* from SkydiveOS reaches
    the box that can never have a registry and reads an empty list forever, while the
    dropzone box sits behind NAT and cannot be dialled in to. Every other thing this box
    originates — the raw-upload notify, the hand-off — is outbound for the same reason;
    this is that pattern applied to the status snapshot.

    Runs as a task for the life of the API. Three rules it must not break:

    * **It never raises into anything.** Discovery and the pull are the product; a status
      banner is cosmetic (the same rule the registry's own hooks and the jump archive
      follow). Cancellation is the one exception that propagates, so shutdown is clean.
    * **It goes quiet when idle, but pushes ONE final empty snapshot on the way there.**
      Without that the consumer's cache holds the last non-empty snapshot until its TTL
      expires and a removed card's row lingers on screen — reading "safe to remove" for a
      card that is already out, or worse, still reading "copying".
    * **A failing push is logged on the transition, not every tick.** At this cadence a
      per-attempt warning buries the log it is supposed to help read.

    The consumer is expected to hold the snapshot behind a short TTL and degrade to empty
    when it goes stale: if this box dies mid-pull, a TTL-less cache would freeze
    "DO NOT REMOVE THE CARD" on the operator's screen indefinitely.
    """
    import httpx  # noqa: PLC0415 - lazy, like the uploader's

    from api.auth import service_auth_headers  # noqa: PLC0415 - keeps /ingest light

    url = f"{skydiveos_url.rstrip('/')}{path}"
    logger.info("publishing card-ingest status to %s every %gs", url, interval)
    failing = False
    published_nonempty = False
    while True:
        try:
            cards = registry.snapshot()
            # An empty registry is the resting state (no card in the reader, which is
            # most of the day). Push nothing — except the single transition to empty,
            # so removal reaches the screen at once instead of waiting out a TTL.
            if cards or published_nonempty:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        url, json={"cards": cards}, headers=service_auth_headers()
                    )
                    resp.raise_for_status()
                published_nonempty = bool(cards)
                if failing:
                    logger.info("card-ingest status push recovered")
                    failing = False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a status banner must not cost a pull
            if not failing:
                logger.warning(
                    "card-ingest status push to %s failing (%r); will keep trying "
                    "quietly — the pull itself is unaffected", url, e,
                )
                failing = True
        await asyncio.sleep(interval)


class _QueueEventEmitter(EventEmitter):
    """Routes ``ready_for_processing`` events into an in-process asyncio queue.

    Substituted for the Redis/file emitter when the service drives a pull, so events
    are handed straight to the materialize loop — no broker round-trip and no racing
    a future Segment-stage consumer on the shared Redis list. ``emit`` is called
    synchronously from inside the pull coroutine; we hop back onto the loop
    thread-safely so it is correct even if a pull is ever run off-thread.
    """

    def __init__(
        self, queue: asyncio.Queue[dict[str, Any]], loop: asyncio.AbstractEventLoop
    ) -> None:
        self._queue = queue
        self._loop = loop

    def emit(self, event: dict[str, Any]) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)


class _HoldingEventEmitter(EventEmitter):
    """Collects one pull's events so they can be released *after* the pull.

    The QR session flow needs the whole card before it can attribute any clip: the
    session marker may legitimately be the LAST file filmed (``ingest.qr`` — marker
    position is not a protocol), so a hand-off that ran per clip mid-pull would ask
    the index about a marker that hasn't been staged yet and fall back to the serial
    match for footage the instructor did claim. Same thread-safety posture as
    :class:`_QueueEventEmitter`: ``emit`` only appends; the flush happens on the
    pull coroutine after the pull returns.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class CameraDiscoveryService:
    """Background service that auto-pulls paired GoPros as they come into range.

    See the module docstring for the full scan → filter → pull → hand-off loop.
    Construct it with an injected scanner, registry, and upload callable; call
    :meth:`start` to launch the two background tasks and :meth:`stop` for a graceful
    shutdown (idempotent). All collaborators are injectable so the whole loop is
    unit-testable with no hardware, broker, or HTTP.
    """

    def __init__(
        self,
        *,
        scanner: CameraScanner,
        registry: CameraRegistry,
        upload: UploadFn,
        pull: PullFn | None = None,
        interval: float = DEFAULT_INTERVAL,
        install_signal_handlers: bool = False,
        handoff_retry_delay: float = 10.0,
        handoff_max_attempts: int = 10,
        role_resolver: RoleResolver | None = None,
        identity_resolver: IdentityResolver | None = None,
        require_registered: bool = True,
    ) -> None:
        self._scanner = scanner
        self._registry = registry
        self._upload = upload
        self._role_resolver = role_resolver
        self._identity_resolver = identity_resolver
        #: SD-card mode sets this False: a physically inserted card is an operator
        #: action (not a BLE stranger in range), and the QR + load match is the real
        #: gate — unmatchable footage is flagged downstream, never delivered.
        self._require_registered = require_registered
        self._pull = pull if pull is not None else _default_pull
        self._interval = interval
        self._install_signal_handlers = install_signal_handlers
        self._handoff_retry_delay = handoff_retry_delay
        self._handoff_max_attempts = handoff_max_attempts

        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._emitter: _QueueEventEmitter | None = None
        #: Cameras with a pull queued or running — the per-camera dedupe set.
        self._inflight: set[str] = set()
        #: One pull at a time: a host can join only one camera's WiFi AP at once.
        self._pull_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        #: Sleeping hand-off retries (see :meth:`_schedule_handoff_retry`).
        self._retry_tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        """Launch the scan and materialize loops. Idempotent."""
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._emitter = _QueueEventEmitter(self._events, loop)
        if self._install_signal_handlers:
            self._add_signal_handlers(loop)
        self._tasks = [
            asyncio.create_task(self._scan_loop(), name="discovery-scan"),
            asyncio.create_task(self._materialize_loop(), name="discovery-materialize"),
        ]
        logger.info("camera auto-discovery started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        """Stop both loops and any in-flight pull, then release the registry. Idempotent."""
        if not self._started:
            return
        self._stopping.set()
        for task in [*self._tasks, *self._retry_tasks]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._retry_tasks, return_exceptions=True)
        self._tasks = []
        self._retry_tasks.clear()
        self._started = False
        try:
            self._registry.close()
        except Exception as e:  # noqa: BLE001 - shutdown must not raise
            logger.warning("error closing camera registry: %r", e)
        logger.info("camera auto-discovery stopped")

    # --- scan → pull ------------------------------------------------------- #

    async def _scan_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._scan_once()
            except Exception as e:  # noqa: BLE001 - a bad scan must not kill the loop
                logger.exception("camera scan failed: %r", e)
            await self._sleep_interruptibly(self._interval)

    async def _scan_once(self) -> None:
        # A pull owns the radios for its whole duration (BLE control + the camera's WiFi
        # AP for the download), and the SDK re-scans BLE inside it. Scanning on top of
        # that makes BlueZ reject one of the two with "Operation already in progress" —
        # which can kill the *pull's* scan, not just ours. Nothing is lost by waiting:
        # pulls are serialised by ``_pull_lock`` anyway, so a second camera found now
        # would only queue behind the one in flight. It is picked up next tick.
        if self._inflight:
            logger.debug("skipping scan: pull in flight for %s", sorted(self._inflight))
            return

        discovered = await self._scanner.scan()
        if not discovered:
            return
        # Registry read is a (blocking) Mongo call — keep it off the event loop.
        known = await asyncio.to_thread(self._registry.known_active_ids)
        for camera_id in discovered:
            allowed = camera_id in known
            if not allowed and not self._require_registered:
                allowed = True
                logger.info(
                    "unregistered camera %s accepted (registry allow-list bypassed; "
                    "identity comes from the QR session marker / load match)",
                    camera_id,
                )
            if allowed and camera_id not in self._inflight:
                self._inflight.add(camera_id)
                asyncio.create_task(
                    self._pull_camera(camera_id), name=f"discovery-pull-{camera_id}"
                )

    async def _pull_camera(self, camera_id: str) -> None:
        logger.info("Camera %s discovered, pull enqueued", camera_id)
        # QR attribution is card-level (the marker may be the LAST clip filmed), so
        # with an identity resolver the pull's events are held and released only once
        # the whole card is staged — including after a partial-pull failure, where the
        # staged clips are still real and must still be handed off.
        held = _HoldingEventEmitter() if self._identity_resolver is not None else None
        try:
            async with self._pull_lock:
                if self._stopping.is_set():
                    return
                await self._pull(camera_id, emitter=held or self._emitter)
        except Exception as e:  # noqa: BLE001 - one failed pull must not stop discovery
            logger.exception("pull failed for camera %s: %r", camera_id, e)
        finally:
            if held is not None and held.events:
                logger.info(
                    "camera %s: card staged; releasing %d held hand-off(s)",
                    camera_id, len(held.events),
                )
                for event in held.events:
                    self._events.put_nowait(event)
            self._inflight.discard(camera_id)

    # --- hand pulled files off to SkydiveOS -------------------------------- #

    async def _materialize_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                event = await self._events.get()
            except asyncio.CancelledError:
                raise
            try:
                await asyncio.to_thread(self._materialize, event)
            except Exception as e:  # noqa: BLE001 - a bad event must not stop the loop
                self._schedule_handoff_retry(event, e)

    def _schedule_handoff_retry(self, event: dict[str, Any], error: Exception) -> None:
        """Requeue a failed hand-off with backoff instead of dropping it.

        A hand-off needs the internet (registry owner lookup, S3 upload, SkydiveOS
        notify) — exactly what a WiFi-only host loses while its radio is joined to a
        camera's access point during a pull, which is also when these events arrive.
        Staged jumps are never re-emitted (pulls are idempotent), so a dropped event
        would strand the footage locally forever; retry until the network is back.
        """
        attempt = int(event.get(_HANDOFF_ATTEMPTS_KEY, 0)) + 1
        mp4 = event.get("files", {}).get("mp4", "<unknown>")
        if _is_permanent_handoff_error(error):
            # A 4xx is SkydiveOS rejecting the notification itself, not the network
            # being down: retrying re-sends the identical payload to the identical
            # endpoint and gets the identical answer. (Their consumer answers 404 for a
            # staff_id with no staff record — a malformed marker, see
            # SKYDIVEOS_INTEGRATION.md.) So report it once and stop, instead of burning
            # the whole backoff ladder. The footage is safe either way: the S3 key is
            # only recorded in the retention ledger on success, so the file stays on
            # the card and stays undeletable.
            logger.error(
                "hand-off REJECTED by SkydiveOS for %s: %r. Not retrying (a 4xx is "
                "permanent). The file stays staged and on the card; fix the "
                "notification's cause, then re-pull.",
                mp4, error,
            )
            return
        if attempt >= self._handoff_max_attempts:
            logger.exception(
                "hand-off to SkydiveOS failed %d times, giving up on %s: %r. The file "
                "is still staged locally; delete its jump folder under raw-storage and "
                "bring the camera back in range to re-pull and retry.",
                attempt, mp4, error,
            )
            return
        event[_HANDOFF_ATTEMPTS_KEY] = attempt
        delay = min(self._handoff_retry_delay * 2 ** (attempt - 1), _HANDOFF_MAX_DELAY)
        logger.warning(
            "hand-off to SkydiveOS failed (%r); retry %d/%d in %.0fs for %s",
            error, attempt, self._handoff_max_attempts, delay, mp4,
        )
        task = asyncio.create_task(
            self._requeue_after(event, delay), name="discovery-handoff-retry"
        )
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _requeue_after(self, event: dict[str, Any], delay: float) -> None:
        await self._sleep_interruptibly(delay)
        if not self._stopping.is_set():
            self._events.put_nowait(event)

    def _materialize(self, event: dict[str, Any]) -> None:
        """Hand one pulled jump to SkydiveOS, which owns job creation.

        Uploads the pulled MP4 to S3 and notifies SkydiveOS with the key, the camera,
        and its owning instructor (looked up from the registry), so the footage lands
        in that instructor's account. Discovery creates no job itself. Because
        ``pull_camera`` only emits for newly-downloaded jumps, each file is handed off
        at most once per stage (a re-pull of an already-staged card emits nothing).
        """
        camera_id = event["camera_id"]
        mp4 = event["files"]["mp4"]
        # The footage lands in the account of whoever owns the camera that shot it;
        # the role (instructor selfie vs external cameraman) routes the two angles of an
        # Ultimate jump under the right raw/<role>/ on the SkydiveOS side.
        instructor_id = self._registry.instructor_for(camera_id)
        camera_role = self._registry.role_for(camera_id)  # static registry hint

        # The AUTHORITATIVE role is which slot the camera's owner filled on THIS jump
        # (from the load), not the camera's static role — one staff member can be the
        # instructor on one jump and the cameraman on the next, on the same GoPro. When a
        # resolver is wired and resolves cleanly, it wins; otherwise we keep the hint.
        if self._role_resolver is not None:
            resolved = self._role_resolver(camera_id, mp4)
            if resolved is not None:
                if resolved != camera_role:
                    logger.info(
                        "camera %s: load-derived role %s overrides registry hint %s",
                        camera_id, resolved, camera_role,
                    )
                camera_role = resolved

        # The QR session flow (SD-card mode): who filmed the clip comes from the QR
        # marker at the head of the session, not from which camera the card was in.
        staff_id: str | None = None
        if self._identity_resolver is not None:
            identity = self._identity_resolver(camera_id, mp4)
            if identity is not None:
                if identity.is_qr_marker:
                    # A session marker is never a job: PUT it under markers/ (so the
                    # retention rule holds and the sweep can clear the card) and stop —
                    # no SkydiveOS notify, no hand-off.
                    logger.info(
                        "QR session marker %s (camera %s) — archiving, no job", mp4, camera_id
                    )
                    marker_key = self._upload(mp4, camera_id, None, None, marker=True)
                    if marker_key:
                        record_uploaded(
                            Path(event["jump_dir"]).parent,
                            Path(mp4).name,
                            marker_key,
                            size=_file_size(mp4),
                        )
                    return
                staff_id = identity.staff_id
                if identity.role is not None:
                    if identity.role != camera_role:
                        logger.info(
                            "camera %s: QR/load-derived role %s overrides %s",
                            camera_id, identity.role, camera_role,
                        )
                    camera_role = identity.role
                if staff_id is None:
                    logger.warning(
                        "clip %s has no QR session marker; falling back to the "
                        "serial-based match", mp4,
                    )

        logger.info(
            "uploading %s to S3 + notifying SkydiveOS (camera %s, instructor %s, role %s) ...",
            mp4, camera_id, instructor_id, camera_role,
        )
        # Keywords only when set, so uploaders predating the QR flow keep working.
        extra: dict[str, Any] = {}
        if staff_id is not None:
            extra["staff_id"] = staff_id
        s3_key = self._upload(mp4, camera_id, instructor_id, camera_role, **extra)
        logger.info(
            "handed %s off to SkydiveOS (camera %s, instructor %s, role %s)",
            mp4, camera_id, instructor_id, camera_role,
        )
        # Now — and only now — is this file safe to remove from the camera: S3 holds it.
        # Recording that fact is what authorises the NEXT pull to free the card
        # (ingest.retention); until then the file stays on the card.
        if s3_key:
            # jump_dir is <root>/_camera-staging/<camera_id>/<date>; the ledger belongs
            # one level up, per camera, so it outlives any single day's folder. The SIZE
            # is what makes the record an identity: without it a reused GoPro filename
            # could authorise deleting a different clip off the card (ingest.retention).
            record_uploaded(
                Path(event["jump_dir"]).parent,
                Path(mp4).name,
                s3_key,
                size=_file_size(mp4),
            )

    # --- helpers ----------------------------------------------------------- #

    async def _sleep_interruptibly(self, seconds: float) -> None:
        """Sleep, but wake immediately when :meth:`stop` is called."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _add_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except (NotImplementedError, RuntimeError, ValueError):
                # Not the main thread / unsupported platform — the host's lifecycle
                # (e.g. the ASGI server's shutdown) is expected to call stop() instead.
                logger.debug("could not install handler for %s; relying on host shutdown", sig)


async def _default_pull(camera_id: str, *, emitter: EventEmitter | None = None) -> Any:
    """Default pull: the real :func:`ingest.pull.pull_camera` with our event sink."""
    from .pull import pull_camera

    return await pull_camera(camera_id, emitter=emitter)
