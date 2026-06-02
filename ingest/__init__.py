"""Ingest stage: pull raw GoPro media off the camera into local staging.

Wraps the Open GoPro Python SDK to BLE-pair, join the camera's WiFi, list the SD
card, and download each jump's MP4 + LRV proxy + thumbnail under
``<root>/{camera_id}/{date}/`` — then emits a ``ready_for_processing`` event onto
the job queue for the Segment stage.

Public entry points:

* :func:`ingest.pull.pull_camera` — orchestrate a full pull (also the
  ``python -m ingest.pull`` CLI).
* :func:`ingest.camera.pair` — one-time BLE pairing for a camera.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .camera import Camera, CameraError, GoProCamera, RemoteMedia, lrv_camera_path, pair
from .events import (
    EVENT_NAME,
    EventEmitter,
    FileEventEmitter,
    RedisEventEmitter,
    build_event,
    default_emitter,
)
from .storage import destination, is_complete, jump_dir, storage_root, write_manifest

if TYPE_CHECKING:
    from .pull import PulledJump, pull_camera


def __getattr__(name: str) -> Any:
    # Load the orchestration lazily so that running `python -m ingest.pull`
    # doesn't import this package's __init__ -> pull.py before the runtime
    # executes pull.py itself (which would emit a spurious RuntimeWarning).
    if name in ("pull_camera", "PulledJump"):
        from . import pull

        return getattr(pull, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Camera",
    "CameraError",
    "GoProCamera",
    "RemoteMedia",
    "lrv_camera_path",
    "pair",
    "pull_camera",
    "PulledJump",
    "EVENT_NAME",
    "EventEmitter",
    "FileEventEmitter",
    "RedisEventEmitter",
    "build_event",
    "default_emitter",
    "storage_root",
    "jump_dir",
    "destination",
    "is_complete",
    "write_manifest",
]
