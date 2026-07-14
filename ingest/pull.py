"""Ingest orchestration + CLI: pull a camera's jumps into local staging.

For each video on the card we download the full-res MP4, its matching LRV proxy,
and a thumbnail into ``<root>/{camera_id}/{date}/`` (see :mod:`ingest.storage`),
write a manifest, then emit a ``ready_for_processing`` event so the Segment stage
can pick the jump up (see :mod:`ingest.events`).

The flow is idempotent and resumable (CLAUDE.md): a jump already fully staged is
skipped, and re-running only fetches what's missing.

CLI::

    python -m ingest.pull --camera 1234            # pull everything new
    python -m ingest.pull --camera 1234 --pair     # one-time BLE pairing
    python -m ingest.pull --camera 1234 --list     # list card contents only
    python -m ingest.pull --camera 1234 --since 2026-05-29
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .camera import Camera, CameraError, GoProCamera, RemoteMedia, pair
from .events import EventEmitter, build_event, default_emitter
from .storage import destination, is_complete, storage_root, write_manifest

logger = logging.getLogger(__name__)


@dataclass
class PulledJump:
    """Outcome of staging one recording."""

    job_id: str
    camera_id: str
    media: RemoteMedia
    mp4_path: Path
    lrv_path: Path | None
    thumbnail_path: Path | None
    skipped: bool  #: True if it was already staged and left untouched


def _job_id(camera_id: str, media: RemoteMedia) -> str:
    """Deterministic per-jump id so downstream stages stay idempotent."""
    return f"{camera_id}-{media.stem}"


async def _pull_one(
    cam: Camera,
    camera_id: str,
    media: RemoteMedia,
    root: Path,
    sink: EventEmitter | None,
    repull: bool,
    now: Callable[[], float],
) -> PulledJump:
    """Stage a single recording (MP4 + LRV + thumbnail), manifest, and emit."""
    mp4_dest = destination(root, camera_id, media.created_epoch, media.filename)
    job_id = _job_id(camera_id, media)

    if not repull and is_complete(mp4_dest):
        logger.info("skip %s (already staged at %s)", media.filename, mp4_dest)
        return PulledJump(job_id, camera_id, media, mp4_dest, None, None, skipped=True)

    mp4_dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("downloading %s -> %s", media.camera_path, mp4_dest)
    await cam.download_mp4(media, mp4_dest)

    # LRV and thumbnail are best-effort: a missing proxy must not strand an
    # otherwise-good MP4. Local names share the MP4 stem so the jump's assets
    # stay grouped (downstream analysis runs on the .LRV proxy).
    lrv_dest: Path | None = None
    if media.has_lrv:
        candidate = mp4_dest.parent / f"{media.stem}.LRV"
        try:
            await cam.download_lrv(media, candidate)
            lrv_dest = candidate
        except CameraError as e:
            logger.warning("LRV download failed for %s: %s", media.filename, e)

    thumb_target = mp4_dest.parent / f"{media.stem}.thumbnail.jpg"
    thumb_dest: Path | None = thumb_target
    try:
        await cam.download_thumbnail(media, thumb_target)
    except CameraError as e:
        logger.warning("thumbnail download failed for %s: %s", media.filename, e)
        thumb_dest = None

    event = build_event(
        job_id=job_id,
        camera_id=camera_id,
        jump_dir=mp4_dest.parent,
        mp4_path=mp4_dest,
        lrv_path=lrv_dest,
        thumbnail_path=thumb_dest,
        created_epoch=media.created_epoch,
        emitted_at=now(),
    )
    # Manifest is written before emitting so a crash mid-emit still leaves the
    # jump marked complete (resumable); the event can be re-derived on replay.
    write_manifest(mp4_dest, event)
    if sink is not None:
        sink.emit(event)

    return PulledJump(job_id, camera_id, media, mp4_dest, lrv_dest, thumb_dest, skipped=False)


async def pull_camera(
    camera_id: str,
    *,
    root: str | Path | None = None,
    emitter: EventEmitter | None = None,
    camera: Camera | None = None,
    since: float | None = None,
    repull: bool = False,
    emit: bool = True,
    queue: str | None = None,
    now: Callable[[], float] = time.time,
) -> list[PulledJump]:
    """Pull all (new) recordings off ``camera_id`` into local staging.

    Args:
        camera_id: Trailing serial digits identifying the camera; also the
            ``{camera_id}`` path segment under the storage root.
        root: Staging root override (else ``$RAW_STORAGE_ROOT`` / ``./raw-storage``).
        emitter: Event sink override. Defaults to Redis (if ``$REDIS_URL`` set)
            or a local file fallback.
        camera: :class:`Camera` override (a real :class:`GoProCamera` by default;
            tests inject a fake).
        since: Only pull recordings created at/after this epoch second.
        repull: Re-download even files already fully staged.
        emit: Emit ``ready_for_processing`` events. When False, no emitter is used.
        queue: Override the Redis queue name for the default emitter.
        now: Clock for the event timestamp (injectable for deterministic tests).

    Returns:
        One :class:`PulledJump` per recording considered (skipped or downloaded).
    """
    resolved_root = storage_root(root)
    sink: EventEmitter | None
    if emitter is not None:
        sink = emitter
    elif emit:
        sink = default_emitter(resolved_root, queue) if queue else default_emitter(resolved_root)
    else:
        sink = None

    cam = camera or GoProCamera(camera_id)
    results: list[PulledJump] = []
    async with cam:
        videos = await cam.list_videos()
        logger.info("camera %s: %d video(s) on card", camera_id, len(videos))
        for media in videos:
            if since is not None and (media.created_epoch or 0.0) < since:
                logger.debug("skip %s (older than --since)", media.filename)
                continue
            results.append(
                await _pull_one(cam, camera_id, media, resolved_root, sink, repull, now)
            )
    return results


async def _list_videos(camera_id: str, args: argparse.Namespace) -> list[RemoteMedia]:
    async with GoProCamera(
        camera_id, wifi_interface=args.wifi_interface, sudo_password=args.password
    ) as cam:
        return await cam.list_videos()


def _parse_since(value: str | None) -> float | None:
    """Parse an ISO date/datetime ``--since`` filter to an epoch second."""
    if value is None:
        return None
    return datetime.fromisoformat(value).timestamp()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.pull",
        description="Pull a GoPro's jumps into local staging and enqueue them for processing.",
    )
    parser.add_argument("--camera", required=True, help="camera id (trailing serial digits)")
    parser.add_argument(
        "--root", default=None, help="staging root (default: $RAW_STORAGE_ROOT or ./raw-storage)"
    )
    parser.add_argument(
        "--since", default=None, help="only pull recordings on/after this ISO date/time"
    )
    parser.add_argument("--repull", action="store_true", help="re-download files already staged")
    parser.add_argument(
        "--no-emit", action="store_true", help="do not emit ready_for_processing events"
    )
    parser.add_argument("--queue", default=None, help="Redis queue name to emit onto")
    parser.add_argument("--pair", action="store_true", help="one-time BLE pairing, then exit")
    parser.add_argument(
        "--name", default=None, help="friendly name to record for the camera when pairing"
    )
    parser.add_argument(
        "--instructor-id",
        default=None,
        help="instructor (SkydiveOS account) to own this camera; its auto-pulled jobs inherit it",
    )
    parser.add_argument(
        "--role",
        default=None,
        choices=["instructor", "external"],
        help="two-camera (Ultimate) role: 'instructor' (selfie cam) or 'external' (cameraman)",
    )
    parser.add_argument(
        "--list", action="store_true", help="list card contents and exit (no download)"
    )
    parser.add_argument("--wifi-interface", default=None, help="host WiFi interface for the SDK")
    parser.add_argument(
        "--password", default=None, help="host sudo password for the SDK (WiFi join)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.pair:
            asyncio.run(
                pair(args.camera, wifi_interface=args.wifi_interface, sudo_password=args.password)
            )
            # Record the pairing so auto-discovery knows this camera is ours. Imported
            # here so a normal pull never pulls in the registry / Mongo driver.
            from .registry import CameraRegistry

            registry = CameraRegistry()
            registry.upsert_paired(
                args.camera, name=args.name, instructor_id=args.instructor_id, role=args.role
            )
            where = "registered for auto-discovery" if registry.enabled else (
                "not registered (MONGO_URL unset)"
            )
            registry.close()
            print(f"Paired with camera {args.camera}. ({where})")
            return 0

        if args.list:
            videos = asyncio.run(_list_videos(args.camera, args))
            print(f"{len(videos)} video(s) on camera {args.camera}:")
            for m in videos:
                lrv = "+LRV" if m.has_lrv else "    "
                print(f"  {lrv}  {m.camera_path}")
            return 0

        jumps = asyncio.run(
            pull_camera(
                args.camera,
                root=args.root,
                since=_parse_since(args.since),
                repull=args.repull,
                emit=not args.no_emit,
                queue=args.queue,
                camera=GoProCamera(
                    args.camera,
                    wifi_interface=args.wifi_interface,
                    sudo_password=args.password,
                ),
            )
        )
    except CameraError as e:
        # Expected, actionable failures (SDK missing, camera unreachable): a clean
        # message and non-zero exit beats an opaque traceback.
        print(f"error: {e}")
        return 1

    downloaded = [j for j in jumps if not j.skipped]
    print(
        f"Pulled {len(downloaded)} new / {len(jumps)} total recording(s) "
        f"from camera {args.camera}."
    )
    for j in jumps:
        status = "skip" if j.skipped else "pull"
        print(f"  [{status}] {j.job_id} -> {j.mp4_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
