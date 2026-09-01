"""CloudFront delivery for the customer gallery's videos (Bug 373).

The rules under test: a **delivered, unlocked** deliverable's player fetch redirects
to a CloudFront signed URL that is *deterministic within a window* (so a replay or
reload reuses the browser's cache — the presigned-per-request URL that never repeated
was the bug); a **locked** deliverable never touches the CDN in any state; the
Download buttons (``?dl=1``) bypass the CDN and serve an attachment (a cross-origin
redirect voids the anchor's ``download`` attribute); and with the CDN unconfigured —
or its key unreadable — behaviour is byte-identical to before, never a 500.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from api import cdn
from api.config import get_settings
from api.jobs import Entitlement, Job, JobStatus, JobStore

CDN_BASE = "https://media.test.example"
KEY_PAIR_ID = "KTESTKEYPAIR"


def _write_rsa_key(path: Path) -> object:
    """Generate an RSA key, write the private PEM to ``path``, return the public key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return key.public_key()


@pytest.fixture()
def cdn_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Configure the CDN settings for a test and undo every cache afterwards."""
    key_path = tmp_path / "cdn_key.pem"
    public_key = _write_rsa_key(key_path)
    monkeypatch.setenv("CDN_BASE_URL", CDN_BASE)
    monkeypatch.setenv("CDN_KEY_PAIR_ID", KEY_PAIR_ID)
    monkeypatch.setenv("CDN_PRIVATE_KEY_PATH", str(key_path))
    get_settings.cache_clear()
    cdn._signer.cache_clear()
    yield public_key
    get_settings.cache_clear()
    cdn._signer.cache_clear()


# ---------------------------------------------------------------------------
# api.cdn unit behaviour
# ---------------------------------------------------------------------------


def test_bucketed_expiry_is_deterministic_within_window() -> None:
    window = 43200
    base = float(window * 100)  # windows are aligned to epoch multiples
    a = cdn._bucketed_expiry(base + 1, window)
    b = cdn._bucketed_expiry(base + window - 1, window)
    later = cdn._bucketed_expiry(base + window, window)
    assert a == b  # every request in one window → the same expiry → the same URL
    assert later > a
    # Never valid for less than one full window (a URL minted at a window's edge
    # must not die mid-playback).
    assert a >= datetime.fromtimestamp(base + window - 1 + window, tz=UTC)


def test_signed_url_is_stable_within_a_window(cdn_env: object) -> None:
    settings = get_settings()
    one = cdn.signed_delivery_url("j1", "full_video.mp4", settings, now=5_000_000.0)
    two = cdn.signed_delivery_url("j1", "full_video.mp4", settings, now=5_000_000.0 + 100)
    assert one is not None and one == two


def test_signed_url_signature_verifies_against_the_public_key(cdn_env: object) -> None:
    """The minted URL is a real CloudFront canned-policy signature, not just shaped
    like one: rebuild the policy botocore signs and verify it with the public key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    settings = get_settings()
    url = cdn.signed_delivery_url(
        "j1", "full_video.mp4", settings, version=1234, now=5_000_000.0
    )
    assert url is not None
    split = urlsplit(url)
    assert f"{split.scheme}://{split.netloc}" == CDN_BASE
    assert split.path == "/deliveries/j1/full_video.mp4"
    q = parse_qs(split.query)
    assert q["v"] == ["1234"]  # the cache-busting version param is signed in
    assert q["Key-Pair-Id"] == [KEY_PAIR_ID]
    expires = int(q["Expires"][0])
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Resource": f"{CDN_BASE}/deliveries/j1/full_video.mp4?v=1234",
                    "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    # CloudFront's URL-safe base64: + → -, = → _, / → ~ (reverse it to decode).
    sig_b64 = q["Signature"][0].replace("-", "+").replace("_", "=").replace("~", "/")
    signature = base64.b64decode(sig_b64)
    cdn_env.verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())  # type: ignore[attr-defined]


def test_unconfigured_returns_none() -> None:
    get_settings.cache_clear()
    assert cdn.signed_delivery_url("j1", "full_video.mp4", get_settings()) is None


def test_unreadable_key_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "garbage.pem"
    bad.write_text("not a key")
    monkeypatch.setenv("CDN_BASE_URL", CDN_BASE)
    monkeypatch.setenv("CDN_KEY_PAIR_ID", KEY_PAIR_ID)
    monkeypatch.setenv("CDN_PRIVATE_KEY_PATH", str(bad))
    get_settings.cache_clear()
    cdn._signer.cache_clear()
    try:
        assert cdn.signed_delivery_url("j1", "full_video.mp4", get_settings()) is None
    finally:
        get_settings.cache_clear()
        cdn._signer.cache_clear()


# ---------------------------------------------------------------------------
# GET /j/{code}/media/{name} routing
# ---------------------------------------------------------------------------


@pytest.fixture()
def gallery(tmp_path: Path):
    """A TestClient + store around one delivered job with a local full_video.mp4."""
    from fastapi.testclient import TestClient

    from api.app import create_app, get_store

    app = create_app()
    store = JobStore(str(tmp_path / "jobs"))
    app.dependency_overrides[get_store] = lambda: store
    client = TestClient(app)
    store.create(
        Job(job_id="jj", status=JobStatus.delivered, outputs={"full_video": "x"})
    )
    (store.dir("jj") / "full_video.mp4").write_bytes(b"CLEAN MASTER BYTES")
    token = store.ensure_gallery_token("jj")
    return client, store, token


def test_player_redirects_to_deterministic_cdn_url(cdn_env: object, gallery) -> None:
    client, store, token = gallery
    first = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    second = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert first.status_code == 302
    assert first.headers["location"].startswith(f"{CDN_BASE}/deliveries/jj/full_video.mp4")
    assert "Signature=" in first.headers["location"]
    assert "v=" in first.headers["location"]  # local render still here → versioned
    # The whole point: a replay gets the byte-identical URL, so the browser's cached
    # ranges are reused instead of the file re-downloading (the presigned-per-request
    # URL never repeated — Bug 373).
    assert first.headers["location"] == second.headers["location"]
    assert first.headers["cache-control"] == "private, max-age=300"


def test_locked_deliverable_never_touches_the_cdn(cdn_env: object, gallery) -> None:
    client, store, token = gallery
    store.update("jj", entitlement=Entitlement.preview_only)
    (store.dir("jj") / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 200  # the local preview, not a redirect anywhere
    assert resp.content == b"WATERMARKED"

    # Pruned preview: still no CDN, no presign — 404, exactly the pre-CDN rule.
    (store.dir("jj") / "preview_full_video.mp4").unlink()
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 404


def test_undelivered_job_streams_locally(cdn_env: object, gallery) -> None:
    client, store, token = gallery
    store.update("jj", status=JobStatus.ready_for_review)
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    # Not yet delivered → the S3 copy isn't known to exist → no CDN redirect.
    assert resp.status_code == 200
    assert resp.content == b"CLEAN MASTER BYTES"


def test_download_click_bypasses_cdn_and_serves_attachment(
    cdn_env: object, gallery
) -> None:
    client, store, token = gallery
    resp = client.get(f"/j/{token}/media/full_video?dl=1", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.content == b"CLEAN MASTER BYTES"
    assert "attachment" in resp.headers["content-disposition"]


def test_pruned_player_prefers_cdn_over_presign(
    cdn_env: object, gallery, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store, token = gallery
    monkeypatch.setenv("S3_BUCKET", "bkt")
    get_settings.cache_clear()
    (store.dir("jj") / "full_video.mp4").unlink()  # what prune_jobs.py did
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{CDN_BASE}/deliveries/jj/")
    assert "v=" not in resp.headers["location"]  # no local file → no version param


def test_pruned_download_falls_back_to_presigned_attachment(
    cdn_env: object, gallery, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store, token = gallery

    class FakePresigner:
        def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:  # noqa: N803
            disposition = Params.get("ResponseContentDisposition", "")
            return f"https://s3.test/{Params['Key']}?disp={disposition}"

    monkeypatch.setenv("S3_BUCKET", "bkt")
    get_settings.cache_clear()
    monkeypatch.setattr("api.delivery._default_s3_client", lambda s: FakePresigner())
    (store.dir("jj") / "full_video.mp4").unlink()
    resp = client.get(f"/j/{token}/media/full_video?dl=1", follow_redirects=False)
    # dl=1 never goes to the CDN — S3 is asked to answer as an attachment, which is
    # what keeps the Download button *saving* after a cross-origin redirect.
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://s3.test/deliveries/jj/")
    assert "attachment" in resp.headers["location"]


def test_local_streaming_answers_range_requests(gallery) -> None:
    client, store, token = gallery
    resp = client.get(
        f"/j/{token}/media/full_video",
        headers={"Range": "bytes=0-4"},
        follow_redirects=False,
    )
    assert resp.status_code == 206
    assert resp.content == b"CLEAN"
    assert resp.headers["content-range"].startswith("bytes 0-4/")


def test_gallery_page_separates_player_and_download_urls(gallery) -> None:
    client, store, token = gallery
    html = client.get(f"/j/{token}").text
    # The player streams the bare URL (CDN-redirectable)…
    assert f'src="/j/{token}/media/full_video"' in html
    # …while every Download anchor carries the dl=1 attachment variant.
    assert f'href="/j/{token}/media/full_video?dl=1" download' in html
