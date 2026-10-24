"""Revoking the customer gallery link (``/j/{code}``).

The code is a bearer credential that never expires, so revocation is the only lever
when a link is leaked or a customer asks for it to be withdrawn:

* ``POST /jobs/{id}/gallery/revoke`` / ``…/restore`` — service token + admin, like
  ``/unlock``; idempotent; never rotates the code, never touches status/entitlement;
* while revoked, EVERY ``/j/{code}…`` route answers a plain, uncacheable 410 with no
  media bytes and no redirect to S3/CDN;
* delivery refuses to email (or forward for SkydiveOS to email/SMS) a revoked link.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import tasks
from api.app import GALLERY_REVOKED_MESSAGE, create_app, get_queue, get_store
from api.config import get_settings
from api.delivery import deliver_to_customer
from api.jobs import Entitlement, Job, JobStatus, JobStore
from api.schemas import JobResponse
from tests.test_api import FakeQueue
from tests.test_delivery import FakeS3, FakeSMTP, _settings

_AUTH = {"Authorization": "Bearer s3cret-token"}
_ADMIN = {**_AUTH, "X-Instructor-Id": "root", "X-Role": "admin"}


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


def _job(client: TestClient, *, locked: bool = False) -> tuple[str, str]:
    """A rendered job with videos, photos and a purchased raw set; returns (id, code)."""
    body: dict[str, Any] = {"customer_name": "Ana Silva"}
    if locked:
        body["entitlement"] = "preview_only"
    job_id = client.post("/jobs", json=body).json()["job_id"]
    store = JobStore(client.jobs_root)  # type: ignore[attr-defined]
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"CLEAN-FULL")
    if locked:
        (jd / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
    photos = jd / "photos"
    photos.mkdir(exist_ok=True)
    (photos / "p1.jpg").write_bytes(b"PHOTO-1")
    (photos / "index.json").write_text(json.dumps([{"filename": "p1.jpg"}]))
    raw = jd / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "GX010001.MP4").write_bytes(b"RAW-MASTER")
    store.update(
        job_id,
        status=JobStatus.ready,
        outputs={"full_video": str(jd / "full_video.mp4"), "photos": str(photos)},
        **({} if locked else {"addons": {"raw": "pay_raw_1", "load_video": "pay_lv_1"}}),
    )
    token = store.load(job_id).gallery_token
    assert token
    return job_id, token


def _subroutes(token: str) -> list[tuple[str, str]]:
    """Every public route under ``/j/{code}`` (method, path) — keep in sync with api.app."""
    return [
        ("GET", f"/j/{token}"),
        ("GET", f"/j/{token}?s=e"),
        ("GET", f"/j/{token}/state"),
        ("GET", f"/j/{token}/media/full_video"),
        ("HEAD", f"/j/{token}/media/full_video"),
        ("GET", f"/j/{token}/media/full_video?dl=1"),
        ("GET", f"/j/{token}/poster/full_video"),
        ("GET", f"/j/{token}/photos/p1.jpg"),
        ("GET", f"/j/{token}/zip/photos"),
        ("GET", f"/j/{token}/zip/all"),
        ("HEAD", f"/j/{token}/zip/all"),
        ("GET", f"/j/{token}/load/full_video"),
        ("GET", f"/j/{token}/raw/GX010001.MP4"),
        ("GET", f"/j/{token}/raw-web/GX010001.MP4"),
    ]


def test_the_route_list_covers_every_public_gallery_route() -> None:
    """A new ``/j/`` route must be added to ``_subroutes`` (and go through the gate)."""
    app = create_app()
    paths = {
        getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").startswith("/j/")
    }
    covered = {
        "/j/{token}", "/j/{token}/state", "/j/{token}/media/{name}",
        "/j/{token}/poster/{name}", "/j/{token}/photos/{filename}", "/j/{token}/zip/{kind}",
        "/j/{token}/load/{name}", "/j/{token}/raw/{name:path}",
        "/j/{token}/raw-web/{name:path}",
    }
    assert paths == covered


def _request(client: TestClient, method: str, path: str) -> Any:
    return client.request(method, path, follow_redirects=False)


def test_every_gallery_route_is_gone_after_revoke_and_back_after_restore(
    client: TestClient,
) -> None:
    job_id, token = _job(client)
    before = {p: _request(client, m, p).status_code for m, p in _subroutes(token)}
    assert before[f"/j/{token}"] == 200
    assert before[f"/j/{token}/media/full_video"] == 200
    assert before[f"/j/{token}/zip/all"] == 200

    resp = client.post(f"/jobs/{job_id}/gallery/revoke")
    assert resp.status_code == 200
    assert resp.json()["gallery_revoked"] is True

    for method, path in _subroutes(token):
        r = _request(client, method, path)
        assert r.status_code == 410, (method, path, r.status_code)
        assert "location" not in r.headers  # never a redirect to S3/CDN
        assert r.headers["cache-control"] == "no-store"
        if method == "GET":
            assert r.text == GALLERY_REVOKED_MESSAGE
            assert token not in r.text
        for secret in (b"CLEAN-FULL", b"PHOTO-1", b"RAW-MASTER", b"WATERMARKED"):
            assert secret not in r.content

    resp = client.post(f"/jobs/{job_id}/gallery/restore")
    assert resp.status_code == 200 and resp.json()["gallery_revoked"] is False
    after = {p: _request(client, m, p).status_code for m, p in _subroutes(token)}
    assert after == before  # the SAME code serves exactly as before
    assert client.get(f"/j/{token}/media/full_video").content == b"CLEAN-FULL"


def test_an_unknown_code_is_still_404_not_410(client: TestClient) -> None:
    _job(client)
    assert client.get("/j/NoSuchCode1").status_code == 404


def test_a_revoked_locked_job_never_serves_clean_bytes_and_restore_keeps_the_lock(
    client: TestClient,
) -> None:
    job_id, token = _job(client, locked=True)
    client.post(f"/jobs/{job_id}/gallery/revoke")
    for method, path in _subroutes(token):
        r = _request(client, method, path)
        assert r.status_code == 410
        assert b"CLEAN-FULL" not in r.content and b"WATERMARKED" not in r.content

    client.post(f"/jobs/{job_id}/gallery/restore")
    # Restoring the link does not unlock anything: still the watermarked preview.
    assert client.get(f"/j/{token}/media/full_video").content == b"WATERMARKED"
    assert client.get(f"/j/{token}/zip/all").status_code == 404


def test_revoke_is_idempotent_keeps_the_code_and_never_touches_status_or_entitlement(
    client: TestClient,
) -> None:
    job_id, token = _job(client, locked=True)
    store = JobStore(client.jobs_root)  # type: ignore[attr-defined]
    client.post(f"/jobs/{job_id}/gallery/revoke")
    first = store.load(job_id).gallery_revoked_at
    assert first is not None
    again = client.post(f"/jobs/{job_id}/gallery/revoke")
    assert again.status_code == 200
    job = store.load(job_id)
    assert job.gallery_revoked_at == first  # original timestamp kept
    assert job.gallery_token == token  # never rotated
    assert job.status is JobStatus.ready
    assert job.entitlement is Entitlement.preview_only
    assert client.post(f"/jobs/{job_id}/gallery/restore").status_code == 200
    assert client.post(f"/jobs/{job_id}/gallery/restore").status_code == 200
    assert store.load(job_id).gallery_revoked_at is None
    assert client.get(f"/jobs/{job_id}").json()["gallery_revoked"] is False


def test_revoke_of_an_unknown_job_is_404(client: TestClient) -> None:
    assert client.post("/jobs/does-not-exist/gallery/revoke").status_code == 404


def test_revoke_and_restore_require_the_service_token_and_an_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cret-token")
    monkeypatch.setenv("ENFORCE_INSTRUCTOR_AUTH", "1")
    get_settings.cache_clear()
    try:
        app = create_app()
        store = JobStore(tmp_path)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_queue] = lambda: FakeQueue()
        with TestClient(app) as c:
            job_id = c.post("/jobs", json={}, headers=_ADMIN).json()["job_id"]
            store.update(job_id, instructor_id="inst-42")
            token = store.load(job_id).gallery_token
            assert token
            instructor = {**_AUTH, "X-Instructor-Id": "inst-42", "X-Role": "instructor"}
            for action in ("revoke", "restore"):
                url = f"/jobs/{job_id}/gallery/{action}"
                assert c.post(url).status_code == 401  # no service token
                # Self-asserted admin headers without the token are still refused.
                assert c.post(url, headers={"X-Role": "admin"}).status_code == 401
                assert c.post(url, headers=instructor).status_code == 403  # owner, not admin
            assert store.load(job_id).gallery_revoked_at is None
            caplog.set_level("INFO")
            ok = c.post(f"/jobs/{job_id}/gallery/revoke", headers=_ADMIN)
            assert ok.status_code == 200 and ok.json()["gallery_revoked"] is True
            # The gallery itself needs no credential — and is now gone.
            assert c.get(f"/j/{token}").status_code == 410
            assert c.post(f"/jobs/{job_id}/gallery/restore", headers=_ADMIN).status_code == 200
            assert c.get(f"/j/{token}").status_code == 200
            # The code is never logged by this service (httpx's own request log aside).
            ours = [r.getMessage() for r in caplog.records if r.name.startswith("api")]
            assert any("gallery link revoked" in m for m in ours)
            assert not any(token in m for m in ours)
    finally:
        get_settings.cache_clear()


def test_a_job_json_written_before_the_field_loads_unchanged(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = store.create(Job(job_id="legacy1", gallery_token="AbCdEfGhIjK"))
    path = store.dir("legacy1") / "job.json"
    data = json.loads(path.read_text())
    data.pop("gallery_revoked_at", None)
    path.write_text(json.dumps(data))
    loaded = store.load("legacy1")
    assert loaded.gallery_revoked_at is None
    assert loaded.gallery_token == job.gallery_token
    assert JobResponse.from_job(loaded).gallery_revoked is False


def _rendered(store: JobStore, **fields: Any) -> Job:
    fields.setdefault("status", JobStatus.approved)
    job = store.create(Job(job_id="j1", customer_email="jane@example.com", **fields))
    final = store.final_path(job.job_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"video")
    return job


@pytest.mark.parametrize("entitlement", [Entitlement.edited_download, Entitlement.preview_only])
def test_delivery_refuses_to_send_a_revoked_gallery(
    tmp_path: Path, entitlement: Entitlement
) -> None:
    store = JobStore(tmp_path)
    job = _rendered(store, entitlement=entitlement, gallery_revoked_at=1.0)
    smtp, s3 = FakeSMTP(), FakeS3()
    with pytest.raises(RuntimeError, match="revoked"):
        deliver_to_customer(
            job, store, _settings(public_base_url="https://freefall.ing"),
            s3_client=s3, smtp_factory=lambda: smtp,  # type: ignore[arg-type,return-value]
        )
    assert smtp.sent == []
    assert s3.uploads == [] and s3.objects == {}
    assert store.load("j1").email_sent_at is None


def test_the_status_callback_withholds_a_revoked_gallery_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SkydiveOS emails/SMSes ``gallery_url`` — a revoked one must not reach it."""
    settings = _settings(public_base_url="https://freefall.ing")
    live = Job(job_id="j1", gallery_token="abc123XYZ98")
    assert tasks._status_payload(live, settings)["gallery_url"].endswith("/j/abc123XYZ98")  # type: ignore[union-attr]
    revoked = live.model_copy(update={"gallery_revoked_at": 1.0})
    assert "gallery_url" not in tasks._status_payload(revoked, settings)
