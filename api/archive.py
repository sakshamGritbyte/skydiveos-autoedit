"""The dropzone's browsable jump archive under ``raw-storage``.

``jobs/<job_id>/`` stays the pipeline's *working* directory — opaque uuids, scene
concats, scratch renders — and remains the source of truth every stage reads and
writes. This module adds the view a **human** needs: one folder per jump, named the
way the dropzone thinks about it, holding both the footage that came off the cameras
and the edit that went to the customer::

    <archive_root>/
      2026-07-28/                     ← date of jump (YYYY-MM-DD, sorts naturally)
        Marc-Tremblay/                ← instructor
          Marie-Dupont/               ← customer
            raw/                      ← the camera masters, exactly as ingested
              instructor/GH010001.MP4 ←   (per-camera role for the Ultimate package)
              external/GH010002.MP4
            edited/                   ← the rendered deliverables (the clean masters)
              full_video.mp4
              highlights.mp4
            preview/                  ← the watermarked 720p previews (Path B jobs)
              full_video.mp4
            photos/                   ← the selected stills
            manifest.json             ← what this folder is (job id, booking, links,
                                        media_state, and a sha256 per file)

``<archive_root>`` is ``$ARCHIVE_ROOT``, defaulting to /ingest's ``$RAW_STORAGE_ROOT``
(``./raw-storage``) so the archive lands where the operators already look. Camera-pull
staging moved under ``raw-storage/_camera-staging/`` (see :mod:`ingest.storage`) so the
top level of the root is nothing but jump dates.

Two properties this module guarantees, because they're what make it safe to run on
every job:

* **Cheap.** Files are *hardlinked* by default (``ARCHIVE_LINK_MODE=link``), so a
  4K master appears in both trees while occupying one copy of the disk. Hardlinking
  falls back to a real copy when the roots are on different filesystems or the store
  doesn't support links; ``copy`` / ``symlink`` force a mode.
* **Never fatal.** Every public function swallows and logs its own errors. The
  archive is a *mirror*; a full disk or a read-only mount must not fail a customer's
  edit. Nothing downstream reads from here.

Calls are idempotent — re-running a job, re-rendering after a tweak, or re-delivering
just refreshes what changed, so the functions can sit on every "footage landed" and
"render finished" seam without bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from .config import Settings
from .jobs import Job, JobStore
from .lifecycle import media_state

logger = logging.getLogger(__name__)

#: Subdirectories of one jump's archive folder.
RAW_DIRNAME = "raw"
EDITED_DIRNAME = "edited"
PREVIEW_DIRNAME = "preview"
PHOTOS_DIRNAME = "photos"
#: How a watermarked preview is named in the job's working dir (the ``preview_<name>``
#: convention :mod:`api.preview` writes). Matched by glob rather than imported so the
#: archive stays dependency-light — it must never drag Pillow/FFmpeg into a mirror pass.
PREVIEW_GLOB = "preview_*.mp4"
PREVIEW_PREFIX = "preview_"
#: Sidecar naming the jump a folder belongs to (also the ownership marker used to
#: disambiguate two same-named customers on the same day with the same instructor).
MANIFEST_FILENAME = "manifest.json"

#: Path segments used when a name is missing. Prefixed so they sort together and read
#: as "needs attention" rather than looking like a real person's folder.
UNKNOWN_INSTRUCTOR = "_no-instructor"
UNKNOWN_CUSTOMER = "_no-customer"

#: Longest path segment we'll emit. Well under every filesystem's 255-byte limit even
#: after a disambiguating job-id suffix is appended.
_MAX_SEGMENT = 60

#: Everything that isn't a letter, digit, or one of ``-._`` collapses to a single dash.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")

#: Accepted values for ``ARCHIVE_LINK_MODE``.
LINK_MODES = ("link", "copy", "symlink")


def slugify(value: str | None, *, fallback: str) -> str:
    """A safe single path segment for a human name, preserving its readability.

    Accents are folded to ASCII (``Marie-Ève`` → ``Marie-Eve``) so the same customer
    can't end up under two folders depending on how their name was typed, spaces
    become dashes, and anything a filesystem or shell would choke on is dropped.
    Returns ``fallback`` when nothing usable survives (empty, whitespace-only, or a
    name written entirely in a script that has no ASCII form).

    The result is deliberately *not* lowercased — this is a folder a human reads.
    """
    if not value or not value.strip():
        return fallback
    # NFKD + ASCII-fold: keeps "Marie-Eve" legible instead of percent-escaping it.
    folded = unicodedata.normalize("NFKD", value.strip())
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = _DASHES.sub("-", _UNSAFE.sub("-", ascii_only)).strip("-._")
    slug = slug[:_MAX_SEGMENT].strip("-._")
    # A segment of only dots would resolve to "." / ".." — never emit one.
    return slug if slug and slug.strip(".") else fallback


def _jump_date(job: Job) -> str:
    """The jump's date as ``YYYY-MM-DD``: the booking's, else when the job was created.

    Falls back to the creation timestamp (then today) rather than inventing an
    ``_unknown`` bucket — a jump always happened on *some* day, and an approximate
    date still files the folder where an operator will look for it.
    """
    if job.jump_date:
        # Tolerate a full ISO datetime as well as a plain date.
        try:
            return date.fromisoformat(job.jump_date[:10]).isoformat()
        except ValueError:
            logger.warning(
                "job %s has an unparseable jump_date %r — filing under its created date",
                job.job_id,
                job.jump_date,
            )
    if job.created_at:
        return datetime.fromtimestamp(job.created_at, tz=UTC).date().isoformat()
    return datetime.now(UTC).date().isoformat()


def archive_root(settings: Settings) -> Path | None:
    """The configured archive root, or ``None`` when archiving is switched off.

    Defaults to /ingest's staging root (``$RAW_STORAGE_ROOT`` → ``./raw-storage``) so
    the archive and the camera staging share one volume, which is also what lets the
    hardlink fast path apply to pulled masters.
    """
    if not settings.archive_enabled:
        return None
    if settings.archive_root:
        return Path(settings.archive_root)
    from ingest.storage import storage_root

    return storage_root()


def _link_mode(settings: Settings) -> str:
    mode = (settings.archive_link_mode or "link").strip().lower()
    if mode not in LINK_MODES:
        logger.warning("unknown ARCHIVE_LINK_MODE %r — falling back to 'link'", mode)
        return "link"
    return mode


# --------------------------------------------------------------------------- #
# Placing files
# --------------------------------------------------------------------------- #


def _already_placed(src: Path, dst: Path) -> bool:
    """True if ``dst`` is already this exact content, so re-placing is pointless.

    Cheap identity checks only (same inode = hardlinked; same size + mtime = copied),
    never a hash — these are multi-GB files and this runs on every pipeline pass.
    """
    try:
        s, d = src.stat(), dst.stat()
    except OSError:
        return False
    if s.st_ino == d.st_ino and s.st_dev == d.st_dev:
        return True
    return s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime)


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy ``src`` → ``dst`` via a temp file + rename, so a partial file is never seen.

    Matters for the archive specifically: an operator (or a sync tool) browsing the
    folder mid-render must never pick up a half-written 4K master.
    """
    tmp = dst.with_name(f".{dst.name}.partial")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def place(src: Path, dst: Path, *, mode: str = "link") -> bool:
    """Materialise ``src`` at ``dst`` in the archive. Returns True if it now exists.

    ``mode`` is one of :data:`LINK_MODES`. ``link`` hardlinks (no extra disk) and
    degrades to a copy when the two paths are on different filesystems, the store
    has no link support, or the link count is exhausted — the archive is worth a
    copy, not worth failing over. Raises nothing the caller must handle: an
    unplaceable file logs a warning and returns False.
    """
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not dst.is_symlink() and _already_placed(src, dst):
        return True

    if mode == "symlink":
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(src.resolve())
            return True
        except OSError as e:
            logger.warning("archive: symlink %s -> %s failed (%s); copying", dst, src, e)
            mode = "copy"

    if mode == "link":
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.link(src, dst)
            return True
        except OSError as e:
            # EXDEV (cross-device), EPERM/EOPNOTSUPP (no link support), EMLINK
            # (link count exhausted) all mean "copy instead", not "give up".
            logger.debug("archive: hardlink %s -> %s failed (%s); copying", dst, src, e)

    try:
        _copy_atomic(src, dst)
        return True
    except OSError as e:
        logger.warning("archive: could not place %s at %s: %s", src, dst, e)
        return False


def _place_tree(src_dir: Path, dst_dir: Path, *, mode: str) -> list[str]:
    """Mirror every file under ``src_dir`` into ``dst_dir``, preserving relative paths.

    Returns the relative paths placed (POSIX-style, for the manifest). Used for the
    ``raw/`` tree — whose per-camera-role subfolders must survive — and for ``photos/``.
    """
    placed: list[str] = []
    for src in sorted(p for p in src_dir.rglob("*") if p.is_file()):
        # Skip our own partial-copy scratch files and OS cruft.
        if src.name.startswith(".") or src.name == MANIFEST_FILENAME:
            continue
        rel = src.relative_to(src_dir)
        if place(src, dst_dir / rel, mode=mode):
            placed.append(rel.as_posix())
    return placed


# --------------------------------------------------------------------------- #
# Locating a jump's folder
# --------------------------------------------------------------------------- #


def jump_dir_parts(job: Job) -> tuple[str, str, str]:
    """The three path segments a jump files under: ``(date, instructor, customer)``.

    Pure — safe to call for logging, tests, or a UI that wants to show the operator
    where a job's footage lives without touching the disk.
    """
    return (
        _jump_date(job),
        slugify(job.instructor_name or job.instructor_id, fallback=UNKNOWN_INSTRUCTOR),
        slugify(job.customer_name, fallback=UNKNOWN_CUSTOMER),
    )


def _owner(candidate: Path) -> str | None:
    """The ``job_id`` recorded in ``candidate``'s manifest, or ``None`` if unmarked."""
    try:
        data = json.loads((candidate / MANIFEST_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    job_id = data.get("job_id")
    return job_id if isinstance(job_id, str) else None


def _candidate_names(job: Job) -> tuple[str, ...]:
    """Folder names this job may live under, most-preferred first.

    Two jumps can collide on (day, instructor, customer) — the same person jumping
    twice, or two customers who share a name — so a colliding job takes a job-id
    suffixed sibling rather than merging two jumps' footage into one folder.
    """
    _, _, customer = jump_dir_parts(job)
    short = job.job_id[:8]
    return (customer, f"{customer}-{short}", f"{customer}-{job.job_id}")


def find_jump_dir(job: Job, root: Path) -> Path | None:
    """This job's archive folder if it already exists — never creates one.

    The read-only counterpart to :func:`resolve_jump_dir`, for tools that inspect the
    archive (``archive_job.py --verify``) and must not conjure an empty folder for a
    job that was never filed.
    """
    day, instructor, _ = jump_dir_parts(job)
    parent = root / day / instructor
    for name in _candidate_names(job):
        candidate = parent / name
        if candidate.is_dir() and _owner(candidate) == job.job_id:
            return candidate
    return None


def resolve_jump_dir(job: Job, root: Path) -> Path:
    """This job's archive folder, created and marked as its own.

    ``<root>/<date>/<instructor>/<customer>`` is not unique on its own: two customers
    can share a name, and the same person can jump twice in a day. So the folder is
    *claimed* — the first job to create it writes its ``job_id`` into the manifest,
    and a later job that finds the folder owned by someone else gets a suffixed
    sibling (``Marie-Dupont-1a2b3c4d``) instead of silently merging two jumps' footage.

    Idempotent: the owning job always resolves back to the same folder.
    """
    day, instructor, _ = jump_dir_parts(job)
    parent = root / day / instructor
    for name in _candidate_names(job):
        candidate = parent / name
        owner = _owner(candidate) if candidate.is_dir() else None
        if owner == job.job_id:
            return candidate
        if owner is None:
            # Free, or an unmarked folder for this very jump (an operator's mkdir, or
            # an archive written before manifests). Claim it by stamping the manifest
            # immediately, so a *different* job arriving next never adopts it.
            candidate.mkdir(parents=True, exist_ok=True)
            _write_manifest(candidate, job)
            return candidate
    # Unreachable in practice: it would take two jobs sharing a full uuid.
    raise RuntimeError(f"cannot claim an archive folder for job {job.job_id}")


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def _read_manifest(jump_dir: Path) -> dict[str, object]:
    """The folder's manifest as a dict — ``{}`` when absent, empty, or corrupt."""
    try:
        current = json.loads((jump_dir / MANIFEST_FILENAME).read_text())
    except (OSError, ValueError):
        return {}
    return current if isinstance(current, dict) else {}


def _sha256(path: Path) -> str | None:
    """Streaming sha256 of one file, or ``None`` if it can't be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
    except OSError as e:
        logger.warning("archive: could not hash %s: %s", path, e)
        return None
    return digest.hexdigest()


def file_digests(
    jump_dir: Path, rel_paths: Iterable[str], *, enabled: bool = True
) -> dict[str, dict[str, object]] | None:
    """``{relative path: {sha256, size, mtime}}`` for archived files.

    Lets an operator prove the master they're holding is the one that was ingested
    (the design doc's ``job.json`` file hashes). A 4K master is expensive to read, and
    this runs at every pipeline seam, so digests are **cached in the manifest** and
    recomputed only when a file's size or mtime changed — a given file is hashed once.

    Returns ``None`` when hashing is switched off (``ARCHIVE_HASHES=0``), which leaves
    any existing section in the manifest untouched. Never raises.
    """
    if not enabled:
        return None
    cached = _read_manifest(jump_dir).get("files")
    cache: dict[str, dict[str, object]] = cached if isinstance(cached, dict) else {}
    out: dict[str, dict[str, object]] = {}
    for rel in sorted(set(rel_paths)):
        path = jump_dir / rel
        try:
            st = path.stat()
        except OSError:
            continue
        size, mtime = st.st_size, int(st.st_mtime)
        prior = cache.get(rel)
        if (
            isinstance(prior, dict)
            and prior.get("size") == size
            and prior.get("mtime") == mtime
            and isinstance(prior.get("sha256"), str)
        ):
            out[rel] = prior  # unchanged since we last hashed it
            continue
        digest = _sha256(path)
        if digest is not None:
            out[rel] = {"sha256": digest, "size": size, "mtime": mtime}
    # Keep digests for files this pass didn't touch (raw survives a render pass).
    return {**{k: v for k, v in cache.items() if isinstance(v, dict)}, **out}


def verify_digests(jump_dir: Path) -> tuple[list[str], list[str], int]:
    """Re-hash a jump folder against its manifest. ``(mismatched, missing, checked)``.

    A hash that is only ever *written* proves nothing — this is the read side, and the
    reason the manifest records one at all: an operator holding a master can show it is
    byte-identical to what the pipeline ingested (bit-rot, a truncated rsync, a file
    swapped by hand). Recomputed in full here, deliberately ignoring the size/mtime
    cache that :func:`file_digests` uses to stay cheap — a tampered file with a
    preserved mtime is exactly the case a cache would wave through.

    * ``mismatched`` — the file is there and its content changed. The alarming one.
    * ``missing`` — recorded in the manifest, absent from disk.
    * ``checked`` — how many entries were hashed.

    Pure read: never rewrites the manifest, never touches the files.
    """
    recorded = _read_manifest(jump_dir).get("files")
    if not isinstance(recorded, dict):
        return ([], [], 0)
    mismatched: list[str] = []
    missing: list[str] = []
    checked = 0
    for rel, meta in sorted(recorded.items()):
        if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str):
            continue
        path = jump_dir / rel
        if not path.is_file():
            missing.append(rel)
            continue
        checked += 1
        if _sha256(path) != meta["sha256"]:
            mismatched.append(rel)
    return (mismatched, missing, checked)


def _write_manifest(jump_dir: Path, job: Job, **updates: object) -> None:
    """Merge ``updates`` into the folder's ``manifest.json`` (created if absent).

    Read-modify-write so the raw pass, the render pass, and the delivery pass each
    contribute their section without clobbering the others. Booking fields are
    refreshed every time, so a late correction to the customer's name shows up here
    even though the folder keeps the name it was created under. A ``None`` update value
    is dropped rather than written, so a pass can pass "nothing to say" for a section.
    """
    path = jump_dir / MANIFEST_FILENAME
    current = _read_manifest(jump_dir)
    updates = {k: v for k, v in updates.items() if v is not None}

    day, instructor, customer = jump_dir_parts(job)
    current.update(
        {
            "job_id": job.job_id,
            "booking_id": job.booking_id,
            "package": job.package.value,
            "entitlement": job.entitlement.value,
            "status": job.status.value,
            # The design doc's product-facing state (derived; see api.lifecycle) so a
            # browsing operator sees the same word the SkydiveOS UI shows them.
            "media_state": media_state(job).value,
            "jump_date": day,
            "instructor": instructor,
            "instructor_id": job.instructor_id,
            "instructor_name": job.instructor_name,
            "customer": customer,
            "customer_name": job.customer_name,
            "customer_email": job.customer_email,
            "camera_id": job.camera_id,
            "updated_at": time.time(),
            **updates,
        }
    )
    current.setdefault("archived_at", current["updated_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# The three public seams
# --------------------------------------------------------------------------- #


def archive_raw_footage(job: Job, store: JobStore, settings: Settings) -> Path | None:
    """Mirror a job's ingested masters into its archive folder's ``raw/``.

    Called wherever footage lands — a multipart upload, an S3 ingest, a camera pull —
    so the raw material is filed under the jump *before* any editing runs, and stays
    there even if the edit later fails. The whole ``jobs/<id>/raw/`` tree is mirrored,
    which preserves the Ultimate package's ``instructor/`` + ``external/`` split; a
    single-master job whose ``source_path`` sits outside that tree (the camera-pull
    path stages into ``raw-storage/_camera-staging/``) has that file placed too.

    Returns the archive folder, or ``None`` when archiving is off or failed. Never raises.
    """
    root = archive_root(settings)
    if root is None:
        return None
    try:
        jump_dir = resolve_jump_dir(job, root)
        mode = _link_mode(settings)
        raw_dst = jump_dir / RAW_DIRNAME
        placed: list[str] = []

        raw_src = store.raw_dir(job.job_id)
        if raw_src.is_dir():
            placed += _place_tree(raw_src, raw_dst, mode=mode)

        # A source master staged outside jobs/<id>/raw/ (camera pull, or the legacy
        # single-file source.mp4) still belongs in the jump's raw folder.
        if job.source_path:
            src = Path(job.source_path)
            if src.is_file() and not src.is_relative_to(raw_src):
                if place(src, raw_dst / src.name, mode=mode):
                    placed.append(src.name)

        rel = sorted(set(placed))
        _write_manifest(
            jump_dir,
            job,
            raw=rel,
            files=file_digests(
                jump_dir,
                (f"{RAW_DIRNAME}/{r}" for r in rel),
                enabled=settings.archive_hashes,
            ),
        )
        logger.info(
            "archive: job %s raw footage → %s (%d file(s), mode=%s)",
            job.job_id,
            jump_dir,
            len(placed),
            mode,
        )
        return jump_dir
    except Exception as e:  # noqa: BLE001 - a mirror must never fail the pipeline
        logger.warning("archive: raw footage for job %s not archived: %r", job.job_id, e)
        return None


def archive_deliverables(job: Job, store: JobStore, settings: Settings) -> Path | None:
    """Mirror a job's finished renders into its archive folder (``edited/`` + ``photos/``).

    Called at every "render finished" seam (the scene pipeline, the single-master
    pipeline, and a post-tweak re-render), so the folder always holds the *current*
    cut next to the footage it was cut from. Videos land in ``edited/`` under their
    deliverable filename; a photos *directory* is mirrored into ``photos/``; a Path-B
    job's watermarked previews are mirrored into ``preview/`` — the archive then shows
    both what the customer could watch and what they'd get on unlocking.

    Returns the archive folder, or ``None`` when archiving is off or failed. Never raises.
    """
    root = archive_root(settings)
    if root is None:
        return None
    try:
        jump_dir = resolve_jump_dir(job, root)
        mode = _link_mode(settings)
        edited: dict[str, str] = {}
        photos: list[str] = []

        outputs = dict(job.outputs or {})
        if not outputs:
            # The classic single-master pipeline reports no outputs map; its one
            # deliverable is final.mp4 beside the job record.
            final = store.final_path(job.job_id)
            if final.is_file():
                outputs["final"] = str(final)

        for name, raw in outputs.items():
            src = Path(raw)
            if src.is_dir():
                photos += _place_tree(src, jump_dir / PHOTOS_DIRNAME, mode=mode)
            elif src.is_file():
                if place(src, jump_dir / EDITED_DIRNAME / src.name, mode=mode):
                    edited[name] = f"{EDITED_DIRNAME}/{src.name}"
            else:
                logger.warning(
                    "archive: job %s deliverable %s missing at %s — skipping it",
                    job.job_id,
                    name,
                    raw,
                )

        # Path B: the watermarked previews sit beside the masters they were made from.
        # Found by api.preview's `preview_<name>.mp4` convention (they're deliberately
        # absent from Job.outputs); the prefix is dropped so the file lines up with its
        # master's name — edited/full_video.mp4 and preview/full_video.mp4.
        previews: dict[str, str] = {}
        for src in sorted(store.dir(job.job_id).glob(PREVIEW_GLOB)):
            name = src.name[len(PREVIEW_PREFIX):]
            if place(src, jump_dir / PREVIEW_DIRNAME / name, mode=mode):
                previews[Path(name).stem] = f"{PREVIEW_DIRNAME}/{name}"

        # The photos dir also carries an index.json (scene/ts/score per still); count the
        # stills only, so the manifest number matches what the customer sees.
        n_photos = sum(1 for p in photos if p.lower().endswith((".jpg", ".jpeg", ".png")))
        _write_manifest(
            jump_dir,
            job,
            edited=edited,
            preview=previews or None,
            photos={"count": n_photos, "dir": PHOTOS_DIRNAME} if photos else None,
            files=file_digests(
                jump_dir,
                [*edited.values(), *previews.values()],
                enabled=settings.archive_hashes,
            ),
        )
        logger.info(
            "archive: job %s deliverables → %s (%d video(s), %d preview(s), %d photo(s))",
            job.job_id,
            jump_dir,
            len(edited),
            len(previews),
            n_photos,
        )
        return jump_dir
    except Exception as e:  # noqa: BLE001 - a mirror must never fail the pipeline
        logger.warning("archive: deliverables for job %s not archived: %r", job.job_id, e)
        return None


def archive_delivery(job: Job, settings: Settings) -> Path | None:
    """Record the customer's gallery/download links in the jump's manifest.

    Closes the loop: the folder then answers "what did this customer actually get,
    and where did it go?" without a database lookup. Links expire
    (``DELIVERY_LINK_TTL_DAYS``), so this is a record of the hand-off, not a
    durable download path.

    Never raises; returns the archive folder or ``None``.
    """
    root = archive_root(settings)
    if root is None:
        return None
    try:
        jump_dir = resolve_jump_dir(job, root)
        _write_manifest(
            jump_dir,
            job,
            delivered_at=time.time(),
            delivery_links=job.delivery_links or {},
        )
        return jump_dir
    except Exception as e:  # noqa: BLE001
        logger.warning("archive: delivery for job %s not recorded: %r", job.job_id, e)
        return None
