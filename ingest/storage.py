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

**A filename is not an identity** (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-F). GoPro
numbering restarts at ``GX010001.MP4`` on a formatted or replaced card, so two cards
cycled through ONE camera on ONE day both offer a ``GX010001.MP4`` for the same
``{camera_id}/{date}/`` folder. The old "already staged" test looked only at the path,
so the second card's clip was skipped as a duplicate and never staged, uploaded or
delivered — while the card banner went green. So :func:`is_complete` now asks whether
the staged file IS the clip on the card (same size, same creation time), and
:func:`resolve_destination` files a *different* clip of the same name into a
collision-safe sibling folder, ``{date}/alt-<size>-<epoch>/{FILENAME}``, keeping the
GoPro filename (every consumer derives its name from ``Path(key).name``). Walk back
up with :func:`day_dir_of` / :func:`camera_dir_of`, which accept both layouts.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOT = Path("raw-storage")
_ENV_ROOT = "RAW_STORAGE_ROOT"
_MANIFEST_SUFFIX = ".ingest.json"
#: Reserved subdirectory holding the per-camera card mirror, so it never mixes with
#: the ``{date}/{instructor}/{customer}/`` jump folders at the root's top level. The
#: underscore keeps it sorted away from the dates and marks it as machine-owned.
CAMERA_STAGING_DIRNAME = "_camera-staging"
#: Sibling folder under a staging day that holds a clip whose GoPro filename is already
#: taken by a DIFFERENT clip of the same camera and day (see the module docstring).
COLLISION_DIR_PREFIX = "alt-"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Creation-time tolerance (seconds) when deciding two listings describe one file: card
#: readers and GoPro media lists both report whole seconds.
_CREATED_TOLERANCE_S = 2.0


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


def is_day_dir(path: Path) -> bool:
    """Whether ``path`` is a staging-day folder (``YYYY-MM-DD``)."""
    return bool(_DAY_RE.match(path.name))


def day_dir_of(path: Path) -> Path | None:
    """The staging-day folder a staged file (or its collision sibling folder) lives in.

    ``.../{camera_id}/{date}/GX010001.MP4`` and
    ``.../{camera_id}/{date}/alt-123-456/GX010001.MP4`` both answer ``.../{date}``;
    ``None`` when no ancestor is day-named (not a staging path).
    """
    for ancestor in (path, *path.parents):
        if is_day_dir(ancestor):
            return ancestor
    return None


def camera_dir_of(path: Path) -> Path | None:
    """The per-camera staging folder (``.../_camera-staging/{camera_id}``) of a staged
    path, where the camera's retention ledger lives — ``None`` outside the staging tree."""
    for ancestor in path.parents:
        if ancestor.parent.name == CAMERA_STAGING_DIRNAME:
            return ancestor
    return None


def collision_dir_name(size: int | None, created_epoch: float | None) -> str:
    """The sibling folder for a clip whose filename is taken: deterministic from the
    on-card listing (size + creation second), so a retried pull of the SAME clip lands on
    the same path and stays idempotent, while a different clip gets a different one."""
    parts = [COLLISION_DIR_PREFIX.rstrip("-")]
    parts.append(str(size) if size is not None else "nosize")
    parts.append(str(int(created_epoch)) if created_epoch is not None else "notime")
    return "-".join(parts)


def staged_created_epoch(mp4_dest: Path) -> float | None:
    """The creation time recorded in a staged clip's manifest, if any."""
    try:
        manifest = json.loads(manifest_path(mp4_dest).read_text())
    except (OSError, ValueError):
        return None
    epoch = manifest.get("created_epoch") if isinstance(manifest, dict) else None
    return float(epoch) if isinstance(epoch, int | float) else None


def is_complete(
    mp4_dest: Path,
    *,
    expected_size: int | None = None,
    created_epoch: float | None = None,
) -> bool:
    """True if THIS clip has already been fully staged at ``mp4_dest``.

    "Fully staged" is the file plus its manifest. With ``expected_size`` /
    ``created_epoch`` (what the card lists for the clip now) it is also an identity
    check: a staged file of a different size, or whose manifest records a different
    creation time, is a *different* recording that merely reused the GoPro filename —
    not this one — so the answer is ``False`` and the caller stages this clip too
    (:func:`resolve_destination`). Either fact unknown on one side is not compared, which
    keeps every manifest written before this check behaved exactly as before.
    """
    if not (mp4_dest.exists() and manifest_path(mp4_dest).exists()):
        return False
    if expected_size is not None:
        try:
            if mp4_dest.stat().st_size != expected_size:
                return False
        except OSError:
            return False
    if created_epoch is not None:
        staged = staged_created_epoch(mp4_dest)
        if staged is not None and abs(staged - created_epoch) > _CREATED_TOLERANCE_S:
            return False
    return True


def is_same_staged_clip(
    mp4_dest: Path, *, expected_size: int | None, created_epoch: float | None
) -> bool:
    """Whether a complete staging at ``mp4_dest`` is the clip described (size + time)."""
    return is_complete(mp4_dest, expected_size=expected_size, created_epoch=created_epoch)


def resolve_destination(
    root: Path,
    camera_id: str,
    created_epoch: float | None,
    filename: str,
    *,
    size: int | None,
) -> tuple[Path, bool]:
    """Where to stage this clip, and whether it is already there.

    Returns ``(path, already_staged)``. The primary path is
    :func:`destination`; when that path holds a complete staging of a *different* clip
    (a reused GoPro filename — the second card of the day), the clip goes to the
    collision sibling ``{date}/alt-<size>-<epoch>/{filename}`` instead, so neither
    recording hides the other. A partial file at the primary path (no manifest) is
    simply overwritten, as before.
    """
    primary = destination(root, camera_id, created_epoch, filename)
    if is_complete(primary, expected_size=size, created_epoch=created_epoch):
        return primary, True
    if is_complete(primary):  # complete, but not THIS clip: a reused filename
        alt = primary.parent / collision_dir_name(size, created_epoch) / filename
        return alt, is_complete(alt, expected_size=size, created_epoch=created_epoch)
    return primary, False


def write_manifest(mp4_dest: Path, manifest: dict[str, object]) -> Path:
    """Persist the per-jump manifest beside its MP4 and return its path.

    Written last in the pull sequence so its presence is a reliable
    "fully staged" marker for :func:`is_complete`.
    """
    path = manifest_path(mp4_dest)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path
