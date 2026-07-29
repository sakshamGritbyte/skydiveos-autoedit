"""Local staging layout for media pulled off a camera.

Files land under ``<root>/_camera-staging/{camera_id}/{date}/{filename}``, where:

* ``<root>`` defaults to ``./raw-storage`` and is overridable with the
  ``$RAW_STORAGE_ROOT`` env var (or an explicit argument);
* ``{date}`` is the media's *own* creation date (``YYYY-MM-DD``) so a jump
  filmed yesterday but pulled today still files under the day it was shot.

This is a *card mirror*, keyed by the only two things a pull knows — which camera
and when it recorded — and it exists to make re-pulls idempotent. It deliberately
sits under the reserved ``_camera-staging/`` prefix so the top level of the storage
root holds nothing but the dropzone's jump archive,
``{jump date}/{instructor}/{customer}/`` (see :mod:`api.archive`), which is the tree
an operator browses. A pull can't file itself there: the booking that names the
instructor and customer isn't known until SkydiveOS matches the footage to a jump.

Each pulled jump gets a sidecar ``<stem>.ingest.json`` manifest next to its
MP4. Its presence (together with the MP4) is what makes a re-run idempotent: we
skip anything already fully staged. All timestamps are seconds (float), per
project convention.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOT = Path("raw-storage")
_ENV_ROOT = "RAW_STORAGE_ROOT"
_MANIFEST_SUFFIX = ".ingest.json"
#: Reserved subdirectory holding the per-camera card mirror, so it never mixes with
#: the ``{date}/{instructor}/{customer}/`` jump folders at the root's top level. The
#: underscore keeps it sorted away from the dates and marks it as machine-owned.
CAMERA_STAGING_DIRNAME = "_camera-staging"


def storage_root(override: str | Path | None = None) -> Path:
    """Resolve the staging root: explicit arg > ``$RAW_STORAGE_ROOT`` > default."""
    if override is not None:
        return Path(override)
    env = os.environ.get(_ENV_ROOT)
    return Path(env) if env else DEFAULT_ROOT


def date_for(created_epoch: float | None) -> str:
    """``YYYY-MM-DD`` for a media creation timestamp (UTC); today if unknown."""
    dt = (
        datetime.now(UTC)
        if created_epoch is None
        else datetime.fromtimestamp(created_epoch, tz=UTC)
    )
    return dt.strftime("%Y-%m-%d")


def camera_staging_root(root: Path) -> Path:
    """The card-mirror subtree of a storage root: ``<root>/_camera-staging``."""
    return root / CAMERA_STAGING_DIRNAME


def camera_dir(root: Path, camera_id: str) -> Path:
    """One camera's staging subtree: ``<root>/_camera-staging/{camera_id}``.

    Date-independent, so per-camera state that must outlive a single day (the
    confirmed-upload ledger in :mod:`ingest.retention`) has a stable home.
    """
    return camera_staging_root(root) / camera_id


def jump_dir(root: Path, camera_id: str, created_epoch: float | None) -> Path:
    """Where a pulled jump's files stage: ``<root>/_camera-staging/{camera_id}/{date}``."""
    return camera_staging_root(root) / camera_id / date_for(created_epoch)


def destination(root: Path, camera_id: str, created_epoch: float | None, filename: str) -> Path:
    """Full local path for one staged file (bare ``filename``, no camera folder)."""
    return jump_dir(root, camera_id, created_epoch) / filename


def manifest_path(mp4_dest: Path) -> Path:
    """Sidecar manifest path for a staged MP4 (e.g. ``GX010123.ingest.json``)."""
    return mp4_dest.with_suffix(_MANIFEST_SUFFIX)


def is_complete(mp4_dest: Path) -> bool:
    """True if this MP4 has already been fully staged (file + manifest present)."""
    return mp4_dest.exists() and manifest_path(mp4_dest).exists()


def write_manifest(mp4_dest: Path, manifest: dict[str, object]) -> Path:
    """Persist the per-jump manifest beside its MP4 and return its path.

    Written last in the pull sequence so its presence is a reliable
    "fully staged" marker for :func:`is_complete`.
    """
    path = manifest_path(mp4_dest)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path
