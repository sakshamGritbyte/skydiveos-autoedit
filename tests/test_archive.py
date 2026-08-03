"""Tests for the browsable jump archive (:mod:`api.archive`).

The archive is a *mirror* of what the pipeline already produced, so these tests care
about three things: that the folder is named the way the dropzone expects
(``{date}/{instructor}/{customer}``), that mirroring is cheap and idempotent (hardlinks,
no re-copying, safe to call on every pass), and that it can never take a job down with
it — a broken archive must degrade to a log line, not a failed edit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from api import archive
from api.config import Settings, get_settings
from api.jobs import Job, JobStatus, JobStore, Package


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "raw-storage"))
    monkeypatch.setenv("ARCHIVE_ENABLED", "1")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


def _job(store: JobStore, **fields: Any) -> Job:
    defaults: dict[str, Any] = {
        "job_id": "abc123def456",
        "customer_name": "Marie Dupont",
        "instructor_name": "Marc Tremblay",
        "jump_date": "2026-07-28",
        "package": Package.selfie,
    }
    return store.create(Job(**{**defaults, **fields}))


def _raw(store: JobStore, job: Job, *names: str) -> None:
    """Stage some raw masters for a job (relative paths allowed, e.g. ``instructor/a.MP4``)."""
    for name in names:
        dest = store.raw_dir(job.job_id) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 16)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Marie Dupont", "Marie-Dupont"),
        ("Marie-Ève Côté", "Marie-Eve-Cote"),        # accents folded, not escaped
        ("  spaced   out  ", "spaced-out"),           # runs collapse to one dash
        ("O'Brien / Smith", "O-Brien-Smith"),         # separators can't create subdirs
        ("../../etc/passwd", "etc-passwd"),           # no traversal survives
        ("Ünïcodé", "Unicode"),
        ("名前", "FALLBACK"),                          # no ASCII form at all
        ("", "FALLBACK"),
        ("   ", "FALLBACK"),
        ("..", "FALLBACK"),                           # never emit a dot-only segment
    ],
)
def test_slugify(value: str, expected: str) -> None:
    assert archive.slugify(value, fallback="FALLBACK") == expected


def test_slugify_caps_segment_length() -> None:
    assert len(archive.slugify("A" * 500, fallback="x")) == 60


def test_jump_dir_parts_uses_booking_names(store: JobStore) -> None:
    job = _job(store)
    assert archive.jump_dir_parts(job) == ("2026-07-28", "Marc-Tremblay", "Marie-Dupont")


def test_jump_dir_parts_falls_back_to_instructor_id(store: JobStore) -> None:
    job = _job(store, instructor_name=None, instructor_id="inst-42")
    assert archive.jump_dir_parts(job)[1] == "inst-42"


def test_jump_dir_parts_marks_missing_names(store: JobStore) -> None:
    job = _job(store, instructor_name=None, customer_name="")
    _, instructor, customer = archive.jump_dir_parts(job)
    assert (instructor, customer) == (archive.UNKNOWN_INSTRUCTOR, archive.UNKNOWN_CUSTOMER)


def test_jump_date_accepts_datetime_and_falls_back(store: JobStore) -> None:
    assert archive.jump_dir_parts(_job(store, jump_date="2026-07-28T14:03:00Z"))[0] == "2026-07-28"
    # An unparseable date still files the jump — under the day the job was opened.
    job = _job(store, job_id="j2", jump_date="last tuesday")
    assert archive.jump_dir_parts(job)[0] != "last tuesday"
    assert len(archive.jump_dir_parts(job)[0]) == len("2026-07-28")


# --------------------------------------------------------------------------- #
# Raw footage
# --------------------------------------------------------------------------- #


def test_archive_raw_footage_layout_and_manifest(
    store: JobStore, settings: Settings, tmp_path: Path
) -> None:
    job = _job(store, booking_id="BK-9", customer_email="marie@example.com")
    _raw(store, job, "GH010001.MP4", "GH020001.MP4")

    jump_dir = archive.archive_raw_footage(job, store, settings)

    assert jump_dir == tmp_path / "raw-storage" / "2026-07-28" / "Marc-Tremblay" / "Marie-Dupont"
    assert (jump_dir / "raw" / "GH010001.MP4").is_file()
    assert (jump_dir / "raw" / "GH020001.MP4").is_file()

    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["job_id"] == job.job_id
    assert manifest["booking_id"] == "BK-9"
    assert manifest["customer_name"] == "Marie Dupont"
    assert manifest["instructor_name"] == "Marc Tremblay"
    assert manifest["customer_email"] == "marie@example.com"
    assert manifest["package"] == "selfie"
    assert manifest["raw"] == ["GH010001.MP4", "GH020001.MP4"]


def test_archive_raw_footage_preserves_ultimum_camera_roles(
    store: JobStore, settings: Settings
) -> None:
    job = _job(store, package=Package.ultimum)
    _raw(store, job, "instructor/GH010001.MP4", "external/GH010001.MP4")

    jump_dir = archive.archive_raw_footage(job, store, settings)
    assert jump_dir is not None
    # Two GoPros emit colliding filenames — the role split has to survive the mirror.
    assert (jump_dir / "raw" / "instructor" / "GH010001.MP4").is_file()
    assert (jump_dir / "raw" / "external" / "GH010001.MP4").is_file()
    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["raw"] == ["external/GH010001.MP4", "instructor/GH010001.MP4"]


def test_archive_raw_footage_hardlinks_by_default(store: JobStore, settings: Settings) -> None:
    job = _job(store)
    _raw(store, job, "GH010001.MP4")

    jump_dir = archive.archive_raw_footage(job, store, settings)
    assert jump_dir is not None
    src = store.raw_dir(job.job_id) / "GH010001.MP4"
    # Same inode: a 4K master shows up in both trees for the cost of one.
    assert (jump_dir / "raw" / "GH010001.MP4").stat().st_ino == src.stat().st_ino


def test_archive_raw_footage_copy_mode(
    store: JobStore, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("ARCHIVE_LINK_MODE", "copy")
    get_settings.cache_clear()
    job = _job(store)
    _raw(store, job, "GH010001.MP4")

    jump_dir = archive.archive_raw_footage(job, store, get_settings())
    assert jump_dir is not None
    dst = jump_dir / "raw" / "GH010001.MP4"
    src = store.raw_dir(job.job_id) / "GH010001.MP4"
    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_ino != src.stat().st_ino  # a real, independent copy


def test_archive_raw_footage_is_idempotent(store: JobStore, settings: Settings) -> None:
    job = _job(store)
    _raw(store, job, "GH010001.MP4")

    first = archive.archive_raw_footage(job, store, settings)
    second = archive.archive_raw_footage(job, store, settings)
    assert first == second

    # A second camera's upload adds to the SAME folder rather than forking a sibling.
    _raw(store, job, "GH020001.MP4")
    third = archive.archive_raw_footage(job, store, settings)
    assert third == first
    manifest = json.loads((first / archive.MANIFEST_FILENAME).read_text())  # type: ignore[operator]
    assert manifest["raw"] == ["GH010001.MP4", "GH020001.MP4"]


def test_archive_raw_footage_includes_source_outside_raw_dir(
    store: JobStore, settings: Settings, tmp_path: Path
) -> None:
    # The camera-pull path stages into raw-storage/_camera-staging/, not jobs/<id>/raw/.
    staged = tmp_path / "raw-storage" / "_camera-staging" / "1234" / "2026-07-28" / "GX010123.MP4"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"pulled")
    job = _job(store, camera_id="1234", source_path=str(staged))

    jump_dir = archive.archive_raw_footage(job, store, settings)
    assert jump_dir is not None
    assert (jump_dir / "raw" / "GX010123.MP4").read_bytes() == b"pulled"


# --------------------------------------------------------------------------- #
# Deliverables
# --------------------------------------------------------------------------- #


def test_archive_deliverables_splits_videos_and_photos(
    store: JobStore, settings: Settings
) -> None:
    job = _job(store)
    jd = store.dir(job.job_id)
    for name in ("full_video.mp4", "highlights.mp4"):
        (jd / name).write_bytes(b"video")
    photos = jd / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (photos / f"freefall_{i}.jpg").write_bytes(b"jpeg")
    # The pipeline writes an index.json beside the stills; it rides along but isn't a photo.
    (photos / "index.json").write_text("[]")
    job = store.update(
        job.job_id,
        status=JobStatus.ready,
        outputs={
            "full_video": str(jd / "full_video.mp4"),
            "highlights": str(jd / "highlights.mp4"),
            "photos": str(photos),
        },
    )

    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None
    assert (jump_dir / "edited" / "full_video.mp4").is_file()
    assert (jump_dir / "edited" / "highlights.mp4").is_file()
    assert len(list((jump_dir / "photos").glob("*.jpg"))) == 3
    assert (jump_dir / "photos" / "index.json").is_file()

    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["edited"] == {
        "full_video": "edited/full_video.mp4",
        "highlights": "edited/highlights.mp4",
    }
    assert manifest["photos"] == {"count": 3, "dir": "photos"}
    assert manifest["status"] == "ready"


def test_archive_deliverables_mirrors_the_watermarked_previews(
    store: JobStore, settings: Settings
) -> None:
    """Path B: preview/ holds what the locked customer watches, edited/ what they'd buy.

    Previews are found by api.preview's ``preview_<name>.mp4`` convention (they're
    deliberately absent from ``Job.outputs``), and the prefix is dropped so each
    preview lines up with its master's name.
    """
    job = _job(store, entitlement="preview_only")
    jd = store.dir(job.job_id)
    (jd / "full_video.mp4").write_bytes(b"clean-master")
    (jd / "preview_full_video.mp4").write_bytes(b"watermarked")
    job = store.update(
        job.job_id, status=JobStatus.ready, outputs={"full_video": str(jd / "full_video.mp4")}
    )

    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None
    assert (jump_dir / "edited" / "full_video.mp4").read_bytes() == b"clean-master"
    assert (jump_dir / "preview" / "full_video.mp4").read_bytes() == b"watermarked"

    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["preview"] == {"full_video": "preview/full_video.mp4"}
    assert manifest["media_state"] == "LOCKED_PREVIEW"


def test_path_a_job_archives_no_preview_section(store: JobStore, settings: Settings) -> None:
    job = _job(store)
    (store.dir(job.job_id) / "full_video.mp4").write_bytes(b"video")
    job = store.update(
        job.job_id,
        status=JobStatus.ready,
        outputs={"full_video": str(store.dir(job.job_id) / "full_video.mp4")},
    )

    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None
    assert not (jump_dir / "preview").exists()
    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert "preview" not in manifest
    assert manifest["media_state"] == "READY"


# --------------------------------------------------------------------------- #
# File hashes (the design doc's job.json digests)
# --------------------------------------------------------------------------- #


def test_manifest_records_a_sha256_per_archived_file(
    store: JobStore, settings: Settings
) -> None:
    import hashlib

    job = _job(store)
    _raw(store, job, "GH010001.MP4")
    archive.archive_raw_footage(job, store, settings)

    jd = store.dir(job.job_id)
    (jd / "full_video.mp4").write_bytes(b"video")
    job = store.update(
        job.job_id, status=JobStatus.ready, outputs={"full_video": str(jd / "full_video.mp4")}
    )
    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None

    files = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())["files"]
    # The render pass must not drop the raw pass's digests.
    assert set(files) == {"raw/GH010001.MP4", "edited/full_video.mp4"}
    assert files["edited/full_video.mp4"]["sha256"] == hashlib.sha256(b"video").hexdigest()
    assert files["raw/GH010001.MP4"]["size"] == 16


def test_digests_are_cached_until_a_file_changes(store: JobStore, settings: Settings) -> None:
    """A 4K master is hashed once, not on every pipeline seam."""
    job = _job(store)
    _raw(store, job, "GH010001.MP4")
    jump_dir = archive.archive_raw_footage(job, store, settings)
    assert jump_dir is not None
    rel = "raw/GH010001.MP4"

    calls: list[Path] = []
    real_sha256 = archive._sha256

    def _counting(path: Path) -> str | None:
        calls.append(path)
        return real_sha256(path)

    archive._sha256 = _counting  # type: ignore[assignment]
    try:
        archive.archive_raw_footage(job, store, settings)  # unchanged → cache hit
        assert calls == []

        # Re-ingest different content at the same path: the digest must follow.
        placed = jump_dir / rel
        placed.unlink()
        (store.raw_dir(job.job_id) / "GH010001.MP4").write_bytes(b"different bytes here")
        archive.archive_raw_footage(job, store, settings)
        assert calls == [placed]
    finally:
        archive._sha256 = real_sha256  # type: ignore[assignment]

    files = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())["files"]
    import hashlib

    assert files[rel]["sha256"] == hashlib.sha256(b"different bytes here").hexdigest()


def test_hashing_can_be_switched_off(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "raw-storage"))
    monkeypatch.setenv("ARCHIVE_HASHES", "0")
    get_settings.cache_clear()
    settings = get_settings()

    job = _job(store)
    _raw(store, job, "GH010001.MP4")
    jump_dir = archive.archive_raw_footage(job, store, settings)
    assert jump_dir is not None
    assert "files" not in json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())


def test_archive_deliverables_falls_back_to_final_mp4(
    store: JobStore, settings: Settings
) -> None:
    # The classic single-master pipeline reports no outputs map — just final.mp4.
    job = _job(store, package=Package.video_only)
    store.final_path(job.job_id).write_bytes(b"final")

    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None
    assert (jump_dir / "edited" / "final.mp4").read_bytes() == b"final"


def test_archive_deliverables_skips_missing_output(store: JobStore, settings: Settings) -> None:
    job = _job(store)
    jd = store.dir(job.job_id)
    (jd / "full_video.mp4").write_bytes(b"video")
    job = store.update(
        job.job_id,
        outputs={"full_video": str(jd / "full_video.mp4"), "highlights": str(jd / "gone.mp4")},
    )

    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None
    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["edited"] == {"full_video": "edited/full_video.mp4"}


def test_archive_deliverables_refreshes_after_a_tweak(
    store: JobStore, settings: Settings
) -> None:
    job = _job(store)
    jd = store.dir(job.job_id)
    (jd / "full_video.mp4").write_bytes(b"first cut")
    job = store.update(job.job_id, outputs={"full_video": str(jd / "full_video.mp4")})
    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None

    # An instructor tweak re-renders in place; the archive must show the NEW cut.
    (jd / "full_video.mp4").write_bytes(b"second cut, longer")
    archive.archive_deliverables(job, store, settings)
    assert (jump_dir / "edited" / "full_video.mp4").read_bytes() == b"second cut, longer"


def test_archive_delivery_records_links(store: JobStore, settings: Settings) -> None:
    job = _job(store)
    job = store.update(
        job.job_id,
        status=JobStatus.delivered,
        delivery_links={"gallery": "https://s3.example/gallery.html"},
    )
    jump_dir = archive.archive_delivery(job, settings)
    assert jump_dir is not None
    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["delivery_links"] == {"gallery": "https://s3.example/gallery.html"}
    assert manifest["status"] == "delivered"
    assert manifest["delivered_at"] > 0


def test_manifest_sections_accumulate(store: JobStore, settings: Settings) -> None:
    """Each pass contributes its own section without clobbering the earlier ones."""
    job = _job(store)
    _raw(store, job, "GH010001.MP4")
    archive.archive_raw_footage(job, store, settings)
    store.final_path(job.job_id).write_bytes(b"final")
    job = store.update(job.job_id, delivery_links={"gallery": "u"}, status=JobStatus.delivered)
    archive.archive_deliverables(job, store, settings)
    jump_dir = archive.archive_delivery(job, settings)

    assert jump_dir is not None
    manifest = json.loads((jump_dir / archive.MANIFEST_FILENAME).read_text())
    assert manifest["raw"] == ["GH010001.MP4"]
    assert manifest["edited"] == {"final": "edited/final.mp4"}
    assert manifest["delivery_links"] == {"gallery": "u"}
    assert manifest["archived_at"] <= manifest["updated_at"]


# --------------------------------------------------------------------------- #
# Collisions
# --------------------------------------------------------------------------- #


def test_same_name_same_day_gets_a_suffixed_sibling(
    store: JobStore, settings: Settings
) -> None:
    """Two different jumps must never merge into one folder, however alike they look."""
    first = _job(store, job_id="1111aaaabbbb")
    second = _job(store, job_id="2222ccccdddd")  # same date, instructor, customer
    _raw(store, first, "A.MP4")
    _raw(store, second, "B.MP4")

    a = archive.archive_raw_footage(first, store, settings)
    b = archive.archive_raw_footage(second, store, settings)
    assert a is not None and b is not None
    assert a != b
    assert b.name == "Marie-Dupont-2222cccc"
    assert (a / "raw" / "A.MP4").is_file() and not (a / "raw" / "B.MP4").exists()
    assert (b / "raw" / "B.MP4").is_file()
    # And each job still resolves back to its own folder on a later pass.
    assert archive.archive_raw_footage(first, store, settings) == a
    assert archive.archive_raw_footage(second, store, settings) == b


def test_unmarked_folder_is_adopted_not_duplicated(
    store: JobStore, settings: Settings, tmp_path: Path
) -> None:
    # An operator (or an older archive) left the folder without a manifest.
    stale = tmp_path / "raw-storage" / "2026-07-28" / "Marc-Tremblay" / "Marie-Dupont"
    stale.mkdir(parents=True)
    job = _job(store)
    _raw(store, job, "A.MP4")
    assert archive.archive_raw_footage(job, store, settings) == stale


# --------------------------------------------------------------------------- #
# Never fatal
# --------------------------------------------------------------------------- #


def test_disabled_archive_is_a_no_op(
    store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARCHIVE_ENABLED", "0")
    get_settings.cache_clear()
    settings = get_settings()
    job = _job(store)
    _raw(store, job, "A.MP4")

    assert archive.archive_root(settings) is None
    assert archive.archive_raw_footage(job, store, settings) is None
    assert archive.archive_deliverables(job, store, settings) is None
    assert archive.archive_delivery(job, settings) is None
    assert not (tmp_path / "raw-storage" / "2026-07-28").exists()


def test_unwritable_archive_never_raises(
    store: JobStore, settings: Settings, tmp_path: Path
) -> None:
    """A full disk or read-only mount must degrade to a log line, not a failed edit."""
    root = tmp_path / "raw-storage"
    root.mkdir(parents=True, exist_ok=True)
    job = _job(store)
    _raw(store, job, "A.MP4")
    root.chmod(0o500)  # readable, not writable
    try:
        assert archive.archive_raw_footage(job, store, settings) is None
        assert archive.archive_deliverables(job, store, settings) is None
        assert archive.archive_delivery(job, settings) is None
    finally:
        root.chmod(0o700)


def test_place_falls_back_to_copy_when_hardlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-device roots (a NAS archive) must copy rather than give up."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"master")

    def _no_links(*_args: object, **_kwargs: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", _no_links)
    dst = tmp_path / "archive" / "src.mp4"
    assert archive.place(src, dst, mode="link") is True
    assert dst.read_bytes() == b"master"


def test_place_leaves_no_partial_file_on_a_failed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"master")
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(
        archive.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    dst = tmp_path / "archive" / "src.mp4"
    assert archive.place(src, dst, mode="link") is False
    # No scratch file left behind for an operator (or a sync tool) to trip over.
    assert list(dst.parent.iterdir()) == []


def test_unknown_link_mode_degrades_to_link(
    store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVE_LINK_MODE", "teleport")
    get_settings.cache_clear()
    job = _job(store)
    _raw(store, job, "A.MP4")
    jump_dir = archive.archive_raw_footage(job, store, get_settings())
    assert jump_dir is not None
    assert (jump_dir / "raw" / "A.MP4").is_file()


# --------------------------------------------------------------------------- #
# C-2 — the READ side of the manifest hashes.
#
# A hash that is only ever written proves nothing. This is what lets an operator show
# that the master they are holding is byte-identical to what the pipeline ingested.
# --------------------------------------------------------------------------- #


def _archived(store: JobStore, settings: Settings) -> tuple[Job, Path]:
    job = _job(store)
    _raw(store, job, "GH010001.MP4")
    jd = store.dir(job.job_id)
    (jd / "full_video.mp4").write_bytes(b"video")
    job = store.update(
        job.job_id, status=JobStatus.ready, outputs={"full_video": str(jd / "full_video.mp4")}
    )
    archive.archive_raw_footage(job, store, settings)
    jump_dir = archive.archive_deliverables(job, store, settings)
    assert jump_dir is not None
    return job, jump_dir


def test_verify_passes_on_an_untouched_archive(store: JobStore, settings: Settings) -> None:
    _job_, jump_dir = _archived(store, settings)
    mismatched, missing, checked = archive.verify_digests(jump_dir)
    assert (mismatched, missing) == ([], [])
    assert checked == 2  # the raw master + the render


def test_verify_detects_a_tampered_file(store: JobStore, settings: Settings) -> None:
    """The case the whole feature exists for."""
    _job_, jump_dir = _archived(store, settings)
    victim = jump_dir / "edited" / "full_video.mp4"
    victim.write_bytes(b"someone else's video")

    mismatched, missing, checked = archive.verify_digests(jump_dir)
    assert mismatched == ["edited/full_video.mp4"]
    assert missing == []
    assert checked == 2


def test_verify_ignores_the_size_mtime_cache(store: JobStore, settings: Settings) -> None:
    """A tamper that preserves size AND mtime is exactly what a cache would wave through."""
    _job_, jump_dir = _archived(store, settings)
    victim = jump_dir / "edited" / "full_video.mp4"
    before = victim.stat()
    victim.write_bytes(b"video"[::-1])  # same length, different bytes
    os.utime(victim, (before.st_atime, before.st_mtime))
    assert victim.stat().st_size == before.st_size

    mismatched, _missing, _checked = archive.verify_digests(jump_dir)
    assert mismatched == ["edited/full_video.mp4"]


def test_verify_reports_a_deleted_file_as_missing(store: JobStore, settings: Settings) -> None:
    _job_, jump_dir = _archived(store, settings)
    (jump_dir / "edited" / "full_video.mp4").unlink()

    mismatched, missing, checked = archive.verify_digests(jump_dir)
    assert missing == ["edited/full_video.mp4"]
    assert mismatched == [] and checked == 1


def test_verify_is_a_no_op_without_recorded_hashes(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "raw-storage"))
    monkeypatch.setenv("ARCHIVE_HASHES", "0")
    get_settings.cache_clear()
    _job_, jump_dir = _archived(store, get_settings())
    assert archive.verify_digests(jump_dir) == ([], [], 0)


def test_verify_never_rewrites_the_manifest(store: JobStore, settings: Settings) -> None:
    """Read-only: a verify pass must not quietly re-bless a changed file."""
    _job_, jump_dir = _archived(store, settings)
    manifest = jump_dir / archive.MANIFEST_FILENAME
    (jump_dir / "edited" / "full_video.mp4").write_bytes(b"tampered")
    before = manifest.read_text()

    archive.verify_digests(jump_dir)
    assert manifest.read_text() == before
    # And a second pass still reports it — the recorded hash stands.
    assert archive.verify_digests(jump_dir)[0] == ["edited/full_video.mp4"]


def test_find_jump_dir_never_creates_a_folder(store: JobStore, settings: Settings) -> None:
    job = _job(store)
    root = archive.archive_root(settings)
    assert root is not None
    assert archive.find_jump_dir(job, root) is None
    day, instructor, customer = archive.jump_dir_parts(job)
    assert not (root / day / instructor / customer).exists()

    _raw(store, job, "GH010001.MP4")
    archive.archive_raw_footage(job, store, settings)
    found = archive.find_jump_dir(job, root)
    assert found is not None and found.is_dir()


# --------------------------------------------------------------------------- #
# C-1 — REV03's "time + instructor + customer", and the dropzone's own midnight.
#
# The bug: the date was derived in UTC, so at Parachute MTL (UTC-4 in summer) every
# jump after 20:00 local filed under TOMORROW — exactly when the last loads fly.
# --------------------------------------------------------------------------- #

_TORONTO = "America/Toronto"


@pytest.fixture
def dz_tz(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "raw-storage"))
    monkeypatch.setenv("ARCHIVE_ENABLED", "1")
    monkeypatch.setenv("CAMERA_CLOCK_TZ", _TORONTO)
    get_settings.cache_clear()
    return get_settings()


def test_evening_jump_files_under_the_dropzone_day_not_utc(
    store: JobStore, dz_tz: Settings
) -> None:
    """21:30 in Toronto is already tomorrow in UTC. The folder must say the 14th."""
    job = _job(store, jump_date="2026-08-14T21:30:00")
    day, _instructor, customer = archive.jump_dir_parts(job, dz_tz)
    assert day == "2026-08-14"
    assert customer == "21-30_Marie-Dupont"


def test_evening_jump_from_created_at_also_uses_the_dropzone_day(
    store: JobStore, dz_tz: Settings
) -> None:
    """No booking time at all: the job's own clock, read in the DZ's zone."""
    from datetime import UTC, datetime

    landed = datetime(2026, 8, 15, 1, 30, tzinfo=UTC).timestamp()  # 21:30 on the 14th
    job = _job(store, jump_date=None)
    job = store.update(job.job_id, created_at=landed)
    day, _i, customer = archive.jump_dir_parts(job, dz_tz)
    assert day == "2026-08-14"
    assert customer == "21-30_Marie-Dupont"


def test_an_aware_jump_date_is_converted_not_reinterpreted(
    store: JobStore, dz_tz: Settings
) -> None:
    job = _job(store, jump_date="2026-08-15T01:30:00Z")  # UTC instant
    day, _i, customer = archive.jump_dir_parts(job, dz_tz)
    assert (day, customer) == ("2026-08-14", "21-30_Marie-Dupont")


def test_a_bare_date_gets_no_invented_time_prefix(store: JobStore, dz_tz: Settings) -> None:
    """A prefix is only worth having if it's true — no 00-00 placeholders."""
    job = _job(store, jump_date="2026-07-28")
    day, _i, customer = archive.jump_dir_parts(job, dz_tz)
    assert (day, customer) == ("2026-07-28", "Marie-Dupont")


def test_two_jumps_same_day_same_customer_sort_by_time(
    store: JobStore, dz_tz: Settings
) -> None:
    morning = _job(store, job_id="j-am", jump_date="2026-08-14T09:05:00")
    evening = _job(store, job_id="j-pm", jump_date="2026-08-14T17:40:00")
    _raw(store, morning, "A.MP4")
    _raw(store, evening, "B.MP4")

    a = archive.archive_raw_footage(morning, store, dz_tz)
    b = archive.archive_raw_footage(evening, store, dz_tz)
    assert a is not None and b is not None and a != b
    assert a.name == "09-05_Marie-Dupont" and b.name == "17-40_Marie-Dupont"
    assert sorted([a.name, b.name]) == [a.name, b.name]  # chronological by name


def test_a_folder_filed_before_the_prefix_existed_is_reused_not_duplicated(
    store: JobStore, dz_tz: Settings
) -> None:
    """The naming change must not re-file or fork jumps already on disk."""
    job = _job(store, jump_date="2026-08-14T14:35:00")
    root = archive.archive_root(dz_tz)
    assert root is not None
    legacy = root / "2026-08-14" / "Marc-Tremblay" / "Marie-Dupont"
    legacy.mkdir(parents=True)
    (legacy / archive.MANIFEST_FILENAME).write_text(json.dumps({"job_id": job.job_id}))

    _raw(store, job, "GH010001.MP4")
    placed = archive.archive_raw_footage(job, store, dz_tz)

    assert placed == legacy  # same folder, no 14-35_ sibling
    assert (legacy / "raw" / "GH010001.MP4").is_file()
    siblings = sorted(p.name for p in legacy.parent.iterdir())
    assert siblings == ["Marie-Dupont"]
    assert archive.find_jump_dir(job, root) == legacy


def test_a_bad_timezone_name_falls_back_to_utc_without_breaking(
    store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "raw-storage"))
    monkeypatch.setenv("CAMERA_CLOCK_TZ", "Mars/Olympus_Mons")
    get_settings.cache_clear()
    job = _job(store, jump_date="2026-08-14T21:30:00")
    day, _i, _c = archive.jump_dir_parts(job, get_settings())
    assert day == "2026-08-14"  # the naive stamp is used as-is under UTC
