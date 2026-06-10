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
   MP4 to S3 and POSTs a small JSON ``{s3_key, camera_id, instructor_id}`` to
   SkydiveOS (``{SKYDIVEOS}/api/media/raw-upload``). SkydiveOS creates the media record
   from the key — large videos never stream through the web layer, and discovery
   never creates a job itself.

Nothing in :func:`ingest.pull.pull_camera` is modified; the service only triggers
it and consumes its events. Start/stop are async (use it from a FastAPI lifespan);
under an ASGI server SIGTERM drives the server's shutdown, which awaits
:meth:`stop`. For standalone use, pass ``install_signal_handlers=True`` to have the
service install its own SIGTERM/SIGINT handlers.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .events import EventEmitter
from .registry import CameraRegistry
from .scanner import CameraScanner

logger = logging.getLogger(__name__)

#: Default seconds between BLE scans (overridable via ``DISCOVERY_INTERVAL_SECONDS``).
DEFAULT_INTERVAL = 30.0
#: SkydiveOS path the S3-key notification is POSTed to (it creates the media record).
RAW_UPLOAD_PATH = "/api/media/raw-upload"
#: S3 key prefix for pulled raw masters: ``{prefix}/{camera_id}/{filename}``.
S3_KEY_PREFIX = "raw"

#: A coroutine that runs a pull for one camera, accepting an ``emitter=`` sink.
#: ``ingest.pull.pull_camera`` satisfies this; tests inject a fake.
PullFn = Callable[..., Awaitable[Any]]
#: Hands one pulled jump to SkydiveOS: ``(mp4_path, camera_id, instructor_id) -> None``.
#: The default (:func:`s3_notify_uploader`) PUTs to S3 then notifies; tests inject a recorder.
UploadFn = Callable[[str, str, str | None], None]


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
) -> UploadFn:
    """Build the default uploader: PUT the pulled MP4 to S3, then notify SkydiveOS.

    Uploads the file to ``s3://{bucket}/{key_prefix}/{camera_id}/{name}`` (boto3's
    ``upload_file`` multiparts large videos automatically), then POSTs a small JSON
    ``{s3_key, camera_id, instructor_id}`` to ``{skydiveos_url}{path}`` — SkydiveOS
    creates the media record from the key, so big files never stream through the web
    layer. The S3 client is created once on first use (``boto3``/``httpx`` imported
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

    def _upload(mp4_path: str, camera_id: str, instructor_id: str | None) -> None:
        import httpx

        key = f"{key_prefix}/{camera_id}/{Path(mp4_path).name}"
        _client().upload_file(mp4_path, bucket, key)

        payload: dict[str, str] = {"s3_key": key, "camera_id": camera_id}
        if instructor_id is not None:
            payload["instructor_id"] = instructor_id
        resp = httpx.post(f"{skydiveos_url.rstrip('/')}{path}", json=payload, timeout=timeout)
        resp.raise_for_status()

    return _upload


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
    ) -> None:
        self._scanner = scanner
        self._registry = registry
        self._upload = upload
        self._pull = pull if pull is not None else _default_pull
        self._interval = interval
        self._install_signal_handlers = install_signal_handlers

        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._emitter: _QueueEventEmitter | None = None
        #: Cameras with a pull queued or running — the per-camera dedupe set.
        self._inflight: set[str] = set()
        #: One pull at a time: a host can join only one camera's WiFi AP at once.
        self._pull_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
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
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
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
        discovered = await self._scanner.scan()
        if not discovered:
            return
        # Registry read is a (blocking) Mongo call — keep it off the event loop.
        known = await asyncio.to_thread(self._registry.known_active_ids)
        for camera_id in discovered:
            if camera_id in known and camera_id not in self._inflight:
                self._inflight.add(camera_id)
                asyncio.create_task(
                    self._pull_camera(camera_id), name=f"discovery-pull-{camera_id}"
                )

    async def _pull_camera(self, camera_id: str) -> None:
        logger.info("Camera %s discovered, pull enqueued", camera_id)
        try:
            async with self._pull_lock:
                if self._stopping.is_set():
                    return
                await self._pull(camera_id, emitter=self._emitter)
        except Exception as e:  # noqa: BLE001 - one failed pull must not stop discovery
            logger.exception("pull failed for camera %s: %r", camera_id, e)
        finally:
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
                logger.exception("failed to hand pulled file off to SkydiveOS: %r", e)

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
        # The footage lands in the account of whoever owns the camera that shot it.
        instructor_id = self._registry.instructor_for(camera_id)

        self._upload(mp4, camera_id, instructor_id)
        logger.info(
            "handed %s off to SkydiveOS (camera %s, instructor %s)",
            mp4, camera_id, instructor_id,
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
