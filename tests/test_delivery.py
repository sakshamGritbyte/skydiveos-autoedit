"""Tests for customer delivery (api/delivery.py) and the AUTO_DELIVER flow.

All offline: S3 is replaced by a recording fake, SMTP by a fake factory, and the
Celery ``delay`` by a recorder — mirroring how the API tests inject a
:class:`FakeQueue`. We assert the full hand-off contract: which files are
collected (final.mp4 vs package outputs, photos dir zipped), what lands in S3,
what the customer email says, and that AUTO_DELIVER auto-approves + enqueues
delivery the moment a render finishes.
"""

from __future__ import annotations

import smtplib
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from api.config import Settings
from api.delivery import (
    DELIVERY_KEY_PREFIX,
    collect_deliverables,
    deliver_to_customer,
    send_delivery_email,
    upload_and_link,
)
from api.jobs import Entitlement, Job, JobStatus, JobStore


def _settings(**overrides: Any) -> Settings:
    """A fully-populated Settings with delivery-friendly defaults for tests."""
    base = Settings(
        redis_url="redis://localhost:6379/0",
        jobs_root=None,
        skydiveos_api_base=None,
        task_always_eager=True,
        enable_auto_discovery=False,
        mongo_url=None,
        mongo_db="skydiveos",
        discovery_interval=30.0,
        camera_scanner="static",
        delete_after_transfer=False,
        delete_after_transfer_min_age_h=24.0,
        delete_after_transfer_dry_run=False,
        discovery_fake_cameras=(),
        discovery_sample_mp4=None,
        discovery_sample_count=1,
        enforce_instructor_auth=False,
        s3_bucket="test-bucket",
        s3_endpoint_url=None,
        s3_region=None,
        smtp_host="smtp.test",
        smtp_user="robot@dropzone.test",
        smtp_password="secret",
        delivery_from_email="videos@dropzone.test",
    )
    return replace(base, **overrides)


class FakeS3:
    """Records uploads and returns deterministic presigned URLs."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, dict[str, Any]]] = []
        #: ``put_object`` bodies (the gallery HTML), keyed by S3 key.
        self.objects: dict[str, bytes] = {}

    def upload_file(
        self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, Any]
    ) -> None:
        self.uploads.append((filename, bucket, key, ExtraArgs))

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> None:
        self.objects[Key] = Body

    def generate_presigned_url(
        self, op: str, Params: dict[str, str], ExpiresIn: int
    ) -> str:
        assert op == "get_object"
        return f"https://s3.test/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"


class FakeSMTP:
    """Stands in for smtplib.SMTP — records the session; usable as a context manager."""

    def __init__(self) -> None:
        self.started_tls = False
        self.logins: list[tuple[str, str]] = []
        self.sent: list[EmailMessage] = []

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logins.append((user, password))

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path)


def _job(store: JobStore, **fields: Any) -> Job:
    fields.setdefault("status", JobStatus.approved)
    job = Job(job_id="j1", **fields)
    return store.create(job)


# --------------------------------------------------------------------------- #
# collect_deliverables
# --------------------------------------------------------------------------- #


def test_collect_uses_final_mp4_for_classic_jobs(store: JobStore) -> None:
    job = _job(store)
    final = store.final_path(job.job_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"video")

    files = collect_deliverables(job, store)
    assert files == {"final": final}


def test_collect_uses_outputs_and_zips_photo_dir(store: JobStore, tmp_path: Path) -> None:
    photos = tmp_path / "j1" / "photos"
    photos.mkdir(parents=True)
    (photos / "photo_001.jpg").write_bytes(b"jpg")
    video = tmp_path / "j1" / "highlights.mp4"
    video.write_bytes(b"video")
    job = _job(store, outputs={"highlights": str(video), "photos": str(photos)})

    files = collect_deliverables(job, store)
    assert files["highlights"] == video
    assert files["photos"].name == "photos.zip"
    assert files["photos"].is_file()


def test_collect_skips_missing_paths(store: JobStore, tmp_path: Path) -> None:
    video = tmp_path / "j1" / "highlights.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    job = _job(
        store, outputs={"highlights": str(video), "gone": str(tmp_path / "nope.mp4")}
    )

    files = collect_deliverables(job, store)
    assert set(files) == {"highlights"}


# --------------------------------------------------------------------------- #
# upload_and_link
# --------------------------------------------------------------------------- #


def test_upload_and_link_puts_files_and_presigns(tmp_path: Path) -> None:
    f = tmp_path / "final.mp4"
    f.write_bytes(b"video")
    s3 = FakeS3()

    links = upload_and_link(
        {"final": f}, job_id="j1", settings=_settings(), s3_client=s3
    )

    assert s3.uploads == [
        (
            str(f),
            "test-bucket",
            f"{DELIVERY_KEY_PREFIX}/j1/final.mp4",
            {"ContentType": "video/mp4"},
        )
    ]
    assert links["final"].startswith("https://s3.test/test-bucket/deliveries/j1/final.mp4")
    assert "expires=604800" in links["final"]  # 7 days


def test_upload_and_link_requires_bucket(tmp_path: Path) -> None:
    f = tmp_path / "final.mp4"
    f.write_bytes(b"video")
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        upload_and_link(
            {"final": f}, job_id="j1", settings=_settings(s3_bucket=None), s3_client=FakeS3()
        )


# --------------------------------------------------------------------------- #
# send_delivery_email
# --------------------------------------------------------------------------- #


def test_email_sent_with_links_and_labels(store: JobStore) -> None:
    job = _job(store, customer_name="Jane", customer_email="jane@example.com")
    smtp = FakeSMTP()

    sent = send_delivery_email(
        job,
        {"highlights": "https://s3.test/h", "photos": "https://s3.test/p"},
        _settings(),
        smtp_factory=lambda: smtp,  # type: ignore[arg-type,return-value]
    )

    assert sent is True
    assert smtp.started_tls
    assert smtp.logins == [("robot@dropzone.test", "secret")]
    (msg,) = smtp.sent
    assert msg["To"] == "jane@example.com"
    assert msg["From"] == "videos@dropzone.test"
    body = msg.get_content()
    assert "Hi Jane," in body
    assert "Highlights: https://s3.test/h" in body
    assert "Photos (zip): https://s3.test/p" in body


@pytest.mark.parametrize(
    "job_fields,settings",
    [
        ({}, _settings()),  # no customer_email
        ({"customer_email": "jane@example.com"}, _settings(smtp_host=None)),  # no SMTP
    ],
)
def test_email_skipped_when_unconfigured(
    store: JobStore, job_fields: dict[str, Any], settings: Settings
) -> None:
    job = _job(store, **job_fields)
    sent = send_delivery_email(
        job, {"final": "https://s3.test/f"}, settings, smtp_factory=FakeSMTP  # type: ignore[arg-type]
    )
    assert sent is False


# --------------------------------------------------------------------------- #
# deliver_to_customer
# --------------------------------------------------------------------------- #


def _rendered_job(store: JobStore, **fields: Any) -> Job:
    job = _job(store, **fields)
    final = store.final_path(job.job_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"video")
    return job


def test_deliver_happy_path_returns_links(store: JobStore) -> None:
    job = _rendered_job(store, customer_email="jane@example.com")
    smtp = FakeSMTP()
    s3 = FakeS3()

    links = deliver_to_customer(
        job, store, _settings(), s3_client=s3, smtp_factory=lambda: smtp  # type: ignore[arg-type,return-value]
    )

    # One customer link is the hosted gallery; the raw video link rides along for
    # SkydiveOS. Exactly one email — the single gallery link, not a link per file.
    assert set(links) == {"gallery", "final"}
    assert f"{DELIVERY_KEY_PREFIX}/{job.job_id}/gallery.html" in s3.objects
    assert len(smtp.sent) == 1
    assert links["gallery"] in smtp.sent[0].get_content()


def test_deliver_fails_when_nothing_rendered(store: JobStore) -> None:
    job = _job(store, customer_email="jane@example.com")
    with pytest.raises(RuntimeError, match="no rendered deliverables"):
        deliver_to_customer(job, store, _settings(), s3_client=FakeS3())


def test_deliver_fails_when_links_reach_nobody(store: JobStore) -> None:
    # No customer_email AND no SkydiveOS callback: 'delivered' would be a lie.
    job = _rendered_job(store)
    with pytest.raises(RuntimeError, match="unreachable"):
        deliver_to_customer(
            job, store, _settings(skydiveos_api_base=None), s3_client=FakeS3()
        )


def test_deliver_without_email_ok_when_skydiveos_forwards(store: JobStore) -> None:
    job = _rendered_job(store)
    links = deliver_to_customer(
        job,
        store,
        _settings(skydiveos_api_base="http://skydiveos.test"),
        s3_client=FakeS3(),
    )
    assert set(links) == {"gallery", "final"}


# --------------------------------------------------------------------------- #
# F-14 — a locked (preview_only) job must never be handed a clean-master URL.
#
# The leak this closes: the legacy S3 gallery presigns `job.outputs` — the CLEAN
# 1080p masters — and emails that page, with no entitlement check anywhere (a
# presigned URL answers to whoever holds it). Path B was therefore delivered
# unlocked, and the same URLs were persisted on the job, mirrored into the archive
# manifest, and forwarded to SkydiveOS.
# --------------------------------------------------------------------------- #


def _locked_job(store: JobStore, **fields: Any) -> Job:
    fields.setdefault("entitlement", Entitlement.preview_only)
    return _rendered_job(store, **fields)


def _served() -> Settings:
    return _settings(public_base_url="https://freefall.ing")


def test_locked_delivery_sends_the_served_gallery_link_and_nothing_else(
    store: JobStore,
) -> None:
    """Positive half: the customer still gets a working link — the lock-aware one."""
    job = _locked_job(store, customer_email="jane@example.com")
    smtp, s3 = FakeSMTP(), FakeS3()

    links = deliver_to_customer(
        job, store, _served(), s3_client=s3, smtp_factory=lambda: smtp  # type: ignore[arg-type,return-value]
    )

    token = store.load(job.job_id).gallery_token
    assert links == {"gallery": f"https://freefall.ing/j/{token}"}
    assert len(smtp.sent) == 1
    body = smtp.sent[0].get_content()
    assert f"https://freefall.ing/j/{token}" in body
    # The gallery route serves the watermarked preview while locked, and never
    # expires — so the email must not carry a "valid for N days" line.
    assert "valid for" not in body.lower()


def test_locked_delivery_mints_no_presigned_url_anywhere(store: JobStore) -> None:
    """Negative half: no delivery output may address the clean master."""
    job = _locked_job(store, customer_email="jane@example.com")
    smtp, s3 = FakeSMTP(), FakeS3()

    links = deliver_to_customer(
        job, store, _served(), s3_client=s3, smtp_factory=lambda: smtp  # type: ignore[arg-type,return-value]
    )

    # The master IS uploaded (durable copy; /unlock serves it instantly)...
    assert [key for _, _, key, _ in s3.uploads] == [
        f"{DELIVERY_KEY_PREFIX}/{job.job_id}/final.mp4"
    ]
    # ...but nothing hands out a URL to it: not the returned links (which get
    # persisted + forwarded to SkydiveOS), not the email, and no gallery.html.
    for url in links.values():
        assert "s3.test" not in url
    assert "s3.test" not in smtp.sent[0].get_content()
    assert s3.objects == {}  # no legacy gallery.html was uploaded


def test_locked_delivery_refuses_the_leaking_legacy_path(store: JobStore) -> None:
    """With no PUBLIC_BASE_URL there is no safe link, so delivery must not proceed.

    Failing loudly is the only non-leaking option: the alternative (the legacy S3
    gallery) embeds presigned clean masters. The raw footage is archived, so a
    re-queue after setting PUBLIC_BASE_URL is cheap.
    """
    job = _locked_job(store, customer_email="jane@example.com")
    s3 = FakeS3()

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        deliver_to_customer(
            job, store, _settings(skydiveos_api_base="http://skydiveos.test"), s3_client=s3
        )
    assert s3.objects == {}  # nothing was published


def test_locked_photo_set_is_not_presigned(store: JobStore) -> None:
    """The photo zip is the same leak in another shape — the grid is teaser-only."""
    job = _locked_job(store, customer_email="jane@example.com")
    photos = store.dir(job.job_id) / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / "a.jpg").write_bytes(b"jpeg")
    store.update(job.job_id, outputs={"photos": str(photos)})
    job = store.load(job.job_id)

    links = deliver_to_customer(
        job, store, _served(), s3_client=FakeS3(), smtp_factory=lambda: FakeSMTP()  # type: ignore[arg-type,return-value]
    )
    assert "photos" not in links
    assert set(links) == {"gallery"}


def test_path_a_delivery_is_unchanged_by_the_lock(store: JobStore) -> None:
    """Regression guard: the paid flow keeps its presigned per-file links."""
    job = _rendered_job(store, customer_email="jane@example.com")  # edited_download
    smtp, s3 = FakeSMTP(), FakeS3()

    links = deliver_to_customer(
        job, store, _served(), s3_client=s3, smtp_factory=lambda: smtp  # type: ignore[arg-type,return-value]
    )

    assert set(links) == {"gallery", "final"}
    assert links["final"].startswith("https://s3.test/")
    assert len(smtp.sent) == 1


# --------------------------------------------------------------------------- #
# AUTO_DELIVER: the review-gate skip in api.tasks
# --------------------------------------------------------------------------- #


def test_auto_deliver_approves_and_enqueues(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks

    _rendered_job(store, status=JobStatus.ready_for_review)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(auto_deliver=True, jobs_root=str(tmp_path))
    )
    delayed: list[str] = []
    monkeypatch.setattr(tasks.deliver_job, "delay", delayed.append)

    tasks._maybe_auto_deliver(store, "j1")

    assert store.load("j1").status == JobStatus.approved
    assert delayed == ["j1"]


def test_auto_deliver_off_leaves_review_gate(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks

    _rendered_job(store, status=JobStatus.ready_for_review)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(auto_deliver=False, jobs_root=str(tmp_path))
    )
    monkeypatch.setattr(
        tasks.deliver_job, "delay", lambda _id: pytest.fail("must not enqueue")
    )

    tasks._maybe_auto_deliver(store, "j1")

    assert store.load("j1").status == JobStatus.ready_for_review


def test_deliver_job_task_persists_links_and_marks_delivered(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import delivery, tasks

    _rendered_job(store, customer_email="jane@example.com")
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path))
    )
    monkeypatch.setattr(
        delivery, "_default_s3_client", lambda settings: FakeS3()
    )
    smtp = FakeSMTP()
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: smtp)

    tasks.deliver_job(job_id="j1")

    job = store.load("j1")
    assert job.status == JobStatus.delivered
    assert job.delivery_links and set(job.delivery_links) == {"gallery", "final"}
    assert len(smtp.sent) == 1


def test_deliver_job_refuses_unapproved(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks

    _rendered_job(store, status=JobStatus.ready_for_review)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path))
    )
    with pytest.raises(RuntimeError, match="refusing to deliver"):
        tasks.deliver_job(job_id="j1")


# --------------------------------------------------------------------------- #
# Status callback: URL + token must match SkydiveOS's shipped receiver
# --------------------------------------------------------------------------- #


def test_status_callback_hits_skydiveos_route_with_links_and_token(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from api import tasks
    from api.jobs import Job, JobStatus

    store.create(
        Job(job_id="j1", status=JobStatus.delivered,
            delivery_links={"final": "https://s3.test/final.mp4"})
    )
    monkeypatch.setattr(
        tasks, "get_settings",
        lambda: _settings(
            jobs_root=str(tmp_path),
            skydiveos_api_base="http://skydiveos.test",
            auto_edit_callback_token="sekret",
        ),
    )
    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    def _fake_post(url, *, json, headers, timeout):  # noqa: ANN001
        posted.update(url=url, json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)

    tasks._notify_skydiveos(store.load("j1"))

    # Exact route SkydiveOS ships (base = host root, same as the raw-upload path).
    assert posted["url"] == "http://skydiveos.test/api/media/auto-edit/jobs/j1/status"
    assert posted["json"] == {
        "job_id": "j1", "status": "delivered",
        "entitlement": "edited_download",
        # The design doc's state vocabulary, derived from status + entitlement.
        "media_state": "DELIVERED",
        "delivery_links": {"final": "https://s3.test/final.mp4"},
    }
    assert posted["headers"]["X-Auto-Edit-Token"] == "sekret"


def test_status_callback_omits_token_when_unset(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from api import tasks
    from api.jobs import Job, JobStatus

    store.create(Job(job_id="j1", status=JobStatus.ready))
    monkeypatch.setattr(
        tasks, "get_settings",
        lambda: _settings(jobs_root=str(tmp_path), skydiveos_api_base="http://skydiveos.test"),
    )
    posted: dict[str, object] = {}

    def _fake_post(url, *, json, headers, timeout):  # noqa: ANN001
        posted.update(headers=headers, json=json)
        class _R:
            def raise_for_status(self) -> None: ...
        return _R()

    monkeypatch.setattr(httpx, "post", _fake_post)
    tasks._notify_skydiveos(store.load("j1"))

    assert "X-Auto-Edit-Token" not in posted["headers"]
    assert "delivery_links" not in posted["json"]  # only present on delivered


# --------------------------------------------------------------------------- #
# S3 ingest: sourcing a job from a key auto-discovery already staged
# --------------------------------------------------------------------------- #


def test_s3_ingest_downloads_and_dispatches_scene_pipeline(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.selfie))
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path))
    )

    downloads: list[tuple[str, str]] = []

    def fake_download(s3_key: str, dest: Path, settings: object) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"video")  # stand in for the real S3 object
        downloads.append((s3_key, dest.name))

    monkeypatch.setattr(tasks, "_download_s3", fake_download)
    enq: list[str] = []
    monkeypatch.setattr(tasks.process_selfie_package, "delay", enq.append)
    # settle_seconds=0 opts out of the multi-clip settle window: dispatch immediately.
    monkeypatch.setattr(
        tasks, "get_settings",
        lambda: _settings(jobs_root=str(tmp_path), raw_clip_settle_seconds=0.0),
    )

    tasks.ingest_s3_job(job_id="j1", s3_key="raw/1234/GH010001.MP4")

    assert downloads == [("raw/1234/GH010001.MP4", "GH010001.MP4")]
    job = store.load("j1")
    assert job.status == JobStatus.queued
    assert job.source_path and job.source_path.endswith("raw/GH010001.MP4")
    assert enq == ["j1"]  # scene pipeline enqueued
    assert job.processing_dispatched is True


# --------------------------------------------------------------------------- #
# Multi-clip jumps: one jump arrives as several s3_key notifications, and must
# produce exactly ONE render — of the whole jump, never a partial one.
# --------------------------------------------------------------------------- #


def _s3_ingest_harness(
    tasks: Any, store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **cfg: Any
) -> tuple[list[str], list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    """Wire ingest_s3_job with a fake download; return (dispatched, settle-arm calls)."""
    def fake_download(s3_key: str, dest: Path, settings: object) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"video")

    monkeypatch.setattr(tasks, "_download_s3", fake_download)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path), **cfg)
    )
    enq: list[str] = []
    monkeypatch.setattr(tasks.process_selfie_package, "delay", enq.append)
    armed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        tasks.raw_clips_settled_job, "apply_async",
        lambda *a, **k: armed.append((a, k)),
    )
    return enq, armed


def test_multi_clip_jump_renders_once_not_once_per_clip(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three clips of one jump → three ingests, ONE render, and it sees all three.

    Without the settle window each notification dispatched its own render: concurrent
    renders sharing a job dir, each cutting whatever subset had landed, and with
    AUTO_DELIVER the first to finish emails the customer a PARTIAL edit.
    """
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.selfie))
    enq, armed = _s3_ingest_harness(tasks, store, tmp_path, monkeypatch)

    for name in ("GH010001.MP4", "GH010002.MP4", "GH010003.MP4"):
        tasks.ingest_s3_job(job_id="j1", s3_key=f"raw/1234/{name}")

    # Nothing rendered yet — each clip only (re)armed the settle check.
    assert enq == []
    assert len(armed) == 3
    assert store.load("j1").processing_dispatched is False

    # The settle check fires once the clips have gone quiet → exactly one render.
    monkeypatch.setattr(tasks.time, "time", lambda: store.load("j1").last_raw_clip_at + 999)
    tasks.raw_clips_settled_job("j1")
    assert enq == ["j1"]
    assert {p.name for p in store.raw_dir("j1").glob("*.MP4")} == {
        "GH010001.MP4", "GH010002.MP4", "GH010003.MP4"
    }  # the whole jump, not a partial cut


def test_settle_check_reschedules_while_clips_still_arriving(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settle check that fires mid-card re-arms instead of cutting a partial jump."""
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.selfie))
    enq, armed = _s3_ingest_harness(tasks, store, tmp_path, monkeypatch)
    tasks.ingest_s3_job(job_id="j1", s3_key="raw/1234/GH010001.MP4")
    armed.clear()

    # Only 5s of quiet against a 180s window → not settled.
    monkeypatch.setattr(tasks.time, "time", lambda: store.load("j1").last_raw_clip_at + 5)
    tasks.raw_clips_settled_job("j1")

    assert enq == []                      # no render
    assert len(armed) == 1                # checked again later
    assert armed[0][1]["countdown"] == 30.0  # the poll interval, not a whole window


def test_settle_check_dispatches_only_once(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping settle checks (or a duplicate notification) → one render."""
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.selfie))
    enq, _ = _s3_ingest_harness(tasks, store, tmp_path, monkeypatch)
    tasks.ingest_s3_job(job_id="j1", s3_key="raw/1234/GH010001.MP4")

    monkeypatch.setattr(tasks.time, "time", lambda: store.load("j1").last_raw_clip_at + 999)
    tasks.raw_clips_settled_job("j1")
    tasks.raw_clips_settled_job("j1")  # a second check must not re-render

    assert enq == ["j1"]


def test_settle_check_is_noop_once_the_job_moved_on(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale settle check must not re-render a job that is already past queued."""
    from api import tasks
    from api.jobs import Job, Package
    from api.jobs import JobStatus as JS

    store.create(Job(job_id="j1", package=Package.selfie, status=JS.delivered))
    enq, _ = _s3_ingest_harness(tasks, store, tmp_path, monkeypatch)

    tasks.raw_clips_settled_job("j1")
    assert enq == []


def test_s3_ingest_ultimum_waits_for_both_cameras(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.ultimum))
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path))
    )

    def fake_download(s3_key: str, dest: Path, settings: object) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"video")

    monkeypatch.setattr(tasks, "_download_s3", fake_download)
    enq: list[str] = []
    monkeypatch.setattr(tasks.process_selfie_package, "delay", enq.append)

    # First camera → downloaded, but processing NOT enqueued (waiting for the other).
    tasks.ingest_s3_job(job_id="j1", s3_key="raw/A/GH010001.MP4", camera_role="instructor")
    assert enq == []

    # Second camera → both present, processing enqueued exactly once.
    tasks.ingest_s3_job(job_id="j1", s3_key="raw/B/GH010001.MP4", camera_role="external")
    assert enq == ["j1"]


def test_s3_ingest_ultimum_without_role_fails(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.ultimum))
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path))
    )
    with pytest.raises(RuntimeError, match="camera_role"):
        tasks.ingest_s3_job(job_id="j1", s3_key="raw/A/GH010001.MP4", camera_role=None)
    assert store.load("j1").status == JobStatus.failed


def test_s3_ingest_ultimum_arms_watchdog_when_not_eager(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.ultimum))
    # Non-eager: the watchdog must be scheduled (with the configured countdown).
    monkeypatch.setattr(
        tasks, "get_settings",
        lambda: _settings(jobs_root=str(tmp_path), task_always_eager=False,
                          ultimum_second_camera_timeout_s=1800.0),
    )

    def fake_download(s3_key: str, dest: Path, settings: object) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"video")

    monkeypatch.setattr(tasks, "_download_s3", fake_download)
    monkeypatch.setattr(tasks.process_selfie_package, "delay", lambda _id: None)
    armed: list[tuple[tuple, float]] = []
    monkeypatch.setattr(
        tasks.ultimum_watchdog_job, "apply_async",
        lambda args, countdown: armed.append((args, countdown)),
    )

    tasks.ingest_s3_job(job_id="j1", s3_key="raw/A/GH010001.MP4", camera_role="instructor")
    assert armed == [(("j1",), 1800.0)]  # watchdog armed for the second camera


def test_ultimum_watchdog_fails_stranded_job(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, JobStatus, Package

    store.create(Job(job_id="j1", package=Package.ultimum, status=JobStatus.queued))
    # Only the instructor camera ever landed.
    idir = store.camera_raw_dir("j1", "instructor")
    idir.mkdir(parents=True, exist_ok=True)
    (idir / "GH010001.mp4").write_bytes(b"video")
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path)))

    tasks.ultimum_watchdog_job(job_id="j1")

    job = store.load("j1")
    assert job.status == JobStatus.failed
    assert "stranded" in (job.error or "") and "external" in (job.error or "")


def test_ultimum_watchdog_noops_when_both_present(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, JobStatus, Package

    store.create(Job(job_id="j1", package=Package.ultimum, status=JobStatus.queued))
    for role in ("instructor", "external"):
        d = store.camera_raw_dir("j1", role)
        d.mkdir(parents=True, exist_ok=True)
        (d / "GH010001.mp4").write_bytes(b"video")
    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path)))

    tasks.ultimum_watchdog_job(job_id="j1")
    assert store.load("j1").status == JobStatus.queued  # untouched — both cameras arrived


def test_s3_ingest_download_failure_marks_failed(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import tasks
    from api.jobs import Job, Package

    store.create(Job(job_id="j1", package=Package.selfie))
    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(jobs_root=str(tmp_path))
    )

    def boom(s3_key: str, dest: Path, settings: object) -> None:
        raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(tasks, "_download_s3", boom)
    with pytest.raises(RuntimeError, match="NoSuchKey"):
        tasks.ingest_s3_job(job_id="j1", s3_key="raw/1234/missing.mp4")
    job = store.load("j1")
    assert job.status == JobStatus.failed
    assert "S3 ingest failed" in (job.error or "")


# --------------------------------------------------------------------------- #
# The served gallery link (PUBLIC_BASE_URL) vs the legacy S3 gallery.html
# --------------------------------------------------------------------------- #


def test_served_gallery_link_replaces_the_s3_page(store: JobStore) -> None:
    """With PUBLIC_BASE_URL set the customer gets the short /j/{code} link.

    No gallery.html is uploaded — the page is rendered per request, so it can flip
    locked → unlocked and its link never expires.
    """
    job = _rendered_job(store, customer_email="jane@example.com")
    smtp = FakeSMTP()
    s3 = FakeS3()

    links = deliver_to_customer(
        job,
        store,
        _settings(public_base_url="https://freefall.ing"),
        s3_client=s3,  # type: ignore[arg-type]
        smtp_factory=lambda: smtp,  # type: ignore[return-value]
    )

    token = store.load(job.job_id).gallery_token
    assert token
    assert links["gallery"] == f"https://freefall.ing/j/{token}"
    assert f"{DELIVERY_KEY_PREFIX}/{job.job_id}/gallery.html" not in s3.objects
    # The clean master still goes to S3 — the durable copy, and what unlock serves.
    master_key = f"{DELIVERY_KEY_PREFIX}/{job.job_id}/final.mp4"
    assert any(key == master_key for _, _, key, _ in s3.uploads)

    body = smtp.sent[0].get_content()
    assert f"https://freefall.ing/j/{token}?s=e#tab-video" in body  # source-tagged
    assert "valid for" not in body  # a served link doesn't expire


def test_public_base_url_unset_keeps_the_legacy_s3_gallery(store: JobStore) -> None:
    job = _rendered_job(store, customer_email="jane@example.com")
    s3 = FakeS3()
    smtp = FakeSMTP()

    links = deliver_to_customer(
        job, store, _settings(), s3_client=s3, smtp_factory=lambda: smtp  # type: ignore[arg-type,return-value]
    )

    assert f"{DELIVERY_KEY_PREFIX}/{job.job_id}/gallery.html" in s3.objects
    assert "gallery.html" in links["gallery"] and "expires=" in links["gallery"]
    assert "valid for" in smtp.sent[0].get_content()  # presigned → still expires


def test_gallery_link_is_stable_across_deliveries(store: JobStore) -> None:
    """A replay/re-delivery must not change the link already sent to the customer."""
    job = _rendered_job(store, customer_email="jane@example.com")
    settings = _settings(public_base_url="https://freefall.ing")

    first = deliver_to_customer(
        job, store, settings, s3_client=FakeS3(), smtp_factory=lambda: FakeSMTP()  # type: ignore[arg-type,return-value]
    )
    second = deliver_to_customer(
        store.load(job.job_id),
        store,
        settings,
        s3_client=FakeS3(),  # type: ignore[arg-type]
        smtp_factory=lambda: FakeSMTP(),  # type: ignore[return-value]
    )
    assert first["gallery"] == second["gallery"]


def test_gallery_link_helper_returns_none_without_a_public_base(store: JobStore) -> None:
    from api.delivery import gallery_link

    job = _rendered_job(store)
    assert gallery_link(job, store, _settings()) is None


def test_status_callback_forwards_entitlement_and_gallery_url(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SkydiveOS needs both to SMS the right link (design doc step 09)."""
    import httpx

    from api import tasks
    from api.jobs import Entitlement, Job, JobStatus

    store.create(
        Job(
            job_id="j-preview",
            status=JobStatus.delivered,
            entitlement=Entitlement.preview_only,
            gallery_token="abc123XYZ98",
        )
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: _settings(
            jobs_root=str(tmp_path),
            skydiveos_api_base="http://skydiveos.test",
            public_base_url="https://freefall.ing",
        ),
    )
    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, *, json, headers, timeout: (posted.update(json=json), _Resp())[1],  # noqa: ANN001
    )

    tasks._notify_skydiveos(store.load("j-preview"))

    payload = posted["json"]
    assert isinstance(payload, dict)
    assert payload["entitlement"] == "preview_only"
    assert payload["gallery_url"] == "https://freefall.ing/j/abc123XYZ98"


def test_status_callback_omits_gallery_url_without_a_public_base(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from api import tasks
    from api.jobs import Job, JobStatus

    store.create(Job(job_id="j2", status=JobStatus.ready, gallery_token="abc123XYZ98"))
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: _settings(jobs_root=str(tmp_path), skydiveos_api_base="http://skydiveos.test"),
    )
    posted: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, *, json, headers, timeout: (posted.update(json=json), _Resp())[1],  # noqa: ANN001
    )

    tasks._notify_skydiveos(store.load("j2"))

    payload = posted["json"]
    assert isinstance(payload, dict)
    assert "gallery_url" not in payload
    assert payload["entitlement"] == "edited_download"
