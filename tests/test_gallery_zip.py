"""Bug 439 — the served gallery's bulk ZIP; Bug 440 — raw footage INCLUDED.

439: ``/j/{code}/zip/photos`` and ``/j/{code}/zip/all`` stream ONE archive of
what the customer OWNS — never a locked deliverable, never a locked photo set,
never raw masters — named after the customer; HEAD answers without building it
(the page's preflight); nothing owned → 404, never an empty archive; a file that
can't be opened is named in MISSING-FILES.txt.

440: SkydiveOS releases raw footage a package includes through the same unlock
seam with an ``included:`` reference; the gallery says "Included", and a load
job (no own masters) is a no-op.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import addon_included, create_app, get_queue, get_store
from api.gallery import render_gallery_html
from api.gallery_zip import (
    MISSING_NOTE,
    ZipEntry,
    content_disposition,
    stream_zip,
    unique_arcnames,
    zip_file_names,
)
from api.jobs import JobKind, JobStatus, JobStore
from tests.test_api import FakeQueue

# --------------------------------------------------------------------------- #
# The archive itself
# --------------------------------------------------------------------------- #


def _read(chunks: Iterator[bytes]) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(b"".join(chunks)))


def test_stream_round_trips_and_is_deflated() -> None:
    entries = [
        ZipEntry("a.jpg", lambda: io.BytesIO(b"A" * 5000)),
        ZipEntry("b.jpg", lambda: io.BytesIO(b"B" * 10)),
    ]
    zf = _read(stream_zip(entries))
    assert zf.testzip() is None
    assert zf.read("a.jpg") == b"A" * 5000
    assert zf.read("b.jpg") == b"B" * 10
    # Deflate, never STORE (stored + data descriptor trips some unzippers).
    assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_DEFLATED}


def test_bytes_leave_while_the_archive_is_still_being_written() -> None:
    big = b"x" * (3 * 1024 * 1024)
    opened: list[str] = []

    def opener(name: str) -> io.BytesIO:
        opened.append(name)
        return io.BytesIO(big)

    gen = stream_zip(
        [ZipEntry("one.mp4", lambda: opener("one")), ZipEntry("two.mp4", lambda: opener("two"))]
    )
    first = next(gen)
    assert first  # something was yielded…
    assert opened == ["one"]  # …before the second file was even opened
    rest = b"".join(gen)
    assert zipfile.ZipFile(io.BytesIO(first + rest)).namelist() == ["one.mp4", "two.mp4"]


def test_an_unopenable_file_is_named_not_silently_dropped() -> None:
    def boom() -> io.BytesIO:
        raise FileNotFoundError("gone")

    zf = _read(
        stream_zip([ZipEntry("ok.jpg", lambda: io.BytesIO(b"1")), ZipEntry("gone.jpg", boom)])
    )
    assert zf.namelist() == ["ok.jpg", MISSING_NOTE]
    assert "gone.jpg" in zf.read(MISSING_NOTE).decode()


def test_names_are_unique_and_path_safe() -> None:
    assert unique_arcnames(["a.jpg", "a.jpg", "../../etc/x", "photos/b.jpg"]) == [
        "a.jpg",
        "a-2.jpg",
        "etc/x",
        "photos/b.jpg",
    ]


def test_file_name_is_the_customer_sanitized() -> None:
    assert zip_file_names("Éloïse O'Brien", "photos") == (
        "Eloise-O-Brien-photos.zip",
        "Éloïse-O'Brien-photos.zip",
    )
    assert zip_file_names(None, "all") == ("jump-gallery.zip", "jump-gallery.zip")
    header = content_disposition('a"b\r\nc', "photos")
    assert "\r" not in header and "\n" not in header
    assert header.startswith('attachment; filename="a-b-c-photos.zip"')


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: FakeQueue()
    with TestClient(app) as c:
        c.jobs_root = tmp_path  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


def _job(client: TestClient, *, locked: bool = False, name: str = "Ana Silva") -> tuple[str, str]:
    body = {"customer_name": name, **({"entitlement": "preview_only"} if locked else {})}
    job_id = client.post("/jobs", json=body).json()["job_id"]
    store = JobStore(client.jobs_root)  # type: ignore[attr-defined]
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"CLEAN-FULL")
    (jd / "highlights.mp4").write_bytes(b"CLEAN-HL")
    if locked:
        (jd / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
        (jd / "preview_highlights.mp4").write_bytes(b"WATERMARKED")
    photos = jd / "photos"
    photos.mkdir(exist_ok=True)
    for n in ("p1.jpg", "p2.jpg"):
        (photos / n).write_bytes(n.encode())
    (photos / "index.json").write_text(json.dumps([{"filename": "p1.jpg"}, {"filename": "p2.jpg"}]))
    raw = jd / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "GX010001.MP4").write_bytes(b"RAW-MASTER")
    store.update(
        job_id,
        status=JobStatus.ready,
        outputs={
            "full_video": str(jd / "full_video.mp4"),
            "highlights": str(jd / "highlights.mp4"),
            "photos": str(photos),
        },
    )
    token = store.load(job_id).gallery_token
    assert token
    return job_id, token


def test_photos_zip_streams_the_owned_set_named_after_the_customer(client: TestClient) -> None:
    _, token = _job(client)
    resp = client.get(f"/j/{token}/zip/photos")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="Ana-Silva-photos.zip"' in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, no-store"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert sorted(zf.namelist()) == ["p1.jpg", "p2.jpg"]


def test_everything_zip_has_owned_videos_and_photos_but_never_raw(client: TestClient) -> None:
    _, token = _job(client)
    zf = zipfile.ZipFile(io.BytesIO(client.get(f"/j/{token}/zip/all").content))
    assert sorted(zf.namelist()) == [
        "full_video.mp4",
        "highlights.mp4",
        "photos/p1.jpg",
        "photos/p2.jpg",
    ]
    assert zf.read("full_video.mp4") == b"CLEAN-FULL"
    assert not any("GX010001" in n for n in zf.namelist())


def test_head_is_the_cheap_preflight(client: TestClient) -> None:
    _, token = _job(client)
    resp = client.head(f"/j/{token}/zip/photos")
    assert resp.status_code == 200
    assert resp.content == b""
    assert "attachment" in resp.headers["content-disposition"]


def test_a_locked_jump_zips_nothing_and_never_leaks_a_clean_master(client: TestClient) -> None:
    _, token = _job(client, locked=True)
    for kind in ("photos", "all"):
        resp = client.get(f"/j/{token}/zip/{kind}")
        assert resp.status_code == 404
        assert b"CLEAN-FULL" not in resp.content
    assert client.head(f"/j/{token}/zip/all").status_code == 404


def test_unknown_kind_and_unknown_code_are_404(client: TestClient) -> None:
    _, token = _job(client)
    assert client.get(f"/j/{token}/zip/raw").status_code == 404
    assert client.get("/j/nope/zip/photos").status_code == 404


def test_the_page_offers_the_zips_only_for_what_is_owned(client: TestClient) -> None:
    _, token = _job(client)
    page = client.get(f"/j/{token}").text
    assert f"/j/{token}/zip/photos" in page
    assert f"/j/{token}/zip/all" in page
    assert "data-ios-note" in page and "Files app" in page
    _, locked_token = _job(client, locked=True)
    locked_page = client.get(f"/j/{locked_token}").text
    assert "/zip/" not in locked_page


# --------------------------------------------------------------------------- #
# Bug 440 — raw footage included with the package
# --------------------------------------------------------------------------- #


def test_included_release_is_recorded_like_a_purchase_and_labelled_included(
    client: TestClient,
) -> None:
    job_id, token = _job(client)
    resp = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "included:raw:bk1", "amount": 0, "item": "raw"},
    )
    assert resp.status_code == 200
    assert resp.json()["addons"] == {"raw": "included:raw:bk1"}
    job = JobStore(client.jobs_root).load(job_id)  # type: ignore[attr-defined]
    assert addon_included(job, "raw")
    # The same gates as a purchase: the master downloads.
    assert client.get(f"/j/{token}/raw/GX010001.MP4").status_code == 200
    page = client.get(f"/j/{token}").text
    assert "Included with your package" in page
    assert "RAW · INCLUDED" in page


def test_a_purchase_is_not_labelled_included(client: TestClient) -> None:
    job_id, token = _job(client)
    client.post(f"/jobs/{job_id}/unlock", json={"payment_reference": "CHG-1", "item": "raw"})
    job = JobStore(client.jobs_root).load(job_id)  # type: ignore[attr-defined]
    assert not addon_included(job, "raw")
    assert "Included with your package" not in client.get(f"/j/{token}").text


def test_an_included_release_on_a_load_job_is_a_no_op(client: TestClient) -> None:
    job_id, _ = _job(client)
    JobStore(client.jobs_root).update(job_id, job_kind=JobKind.load_child)  # type: ignore[attr-defined]
    resp = client.post(
        f"/jobs/{job_id}/unlock", json={"payment_reference": "included:raw:bk1", "item": "raw"}
    )
    assert resp.status_code == 200
    assert resp.json()["addons"] == {}


def test_included_but_files_gone_shows_nothing_rather_than_a_purchase_note() -> None:
    page = render_gallery_html(
        brand="DZ",
        customer_name="Ana",
        jump_date=None,
        location=None,
        videos=[("full_video", "/v")],
        photos=[],
        raw_videos=[],
        raw_unavailable=True,
        raw_included=True,
        tabbed=True,
        show_downloads=True,
    )
    assert "purchase is on record" not in page
