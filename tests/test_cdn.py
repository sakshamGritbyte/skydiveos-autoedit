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
import os
import re
import time
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
    # The player streams the plain media URL (CDN-redirectable), now cache-busted by
    # the render mtime — the path is what distinguishes it, not the query string.
    assert f'src="/j/{token}/media/full_video?v=' in html
    assert f'src="/j/{token}/media/full_video?dl=1' not in html
    # …while every Download anchor carries the dl=1 attachment variant.
    assert f'href="/j/{token}/media/full_video?dl=1&amp;v=' in html
    assert " download" in html


# ---------------------------------------------------------------------------
# Cache-Control — the half of Bug 373 that works with NO CDN configured
#
# The CDN redirect only covers a delivered, UNLOCKED video. Everything else the
# gallery streams still leaves this process — an undelivered render, a purchased raw
# master and its proxy, the load video, the photo stills, and (on a stack with no
# CDN_BASE_URL, which is every stack until it is configured) the delivered videos too.
# Starlette sends etag/last-modified but no lifetime, so without these headers a
# replay re-fetches: "the video reloads every time I play it", exactly as reported.
#
# The rule: `private` (never `public` — the gallery's short code is the only
# credential, so no shared cache may store the response), long for what the customer
# owns, 60s for what is still behind the paywall so an unlock is visible at once.
# ---------------------------------------------------------------------------


def test_owned_video_is_browser_cacheable_for_a_day(gallery) -> None:
    """No CDN configured → the local stream still carries a lifetime."""
    client, store, token = gallery
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=86400"
    # Never `public`: a proxy that cached this would serve one customer's jump to the
    # next request for the same URL.
    assert "public" not in resp.headers["cache-control"]


def test_download_click_is_cacheable_too(gallery) -> None:
    client, store, token = gallery
    resp = client.get(f"/j/{token}/media/full_video?dl=1", follow_redirects=False)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, max-age=86400"


def test_locked_preview_is_only_briefly_cacheable(gallery) -> None:
    """A watermark must not outlive the payment that removes it."""
    client, store, token = gallery
    store.update("jj", entitlement=Entitlement.preview_only)
    (store.dir("jj") / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.content == b"WATERMARKED"
    # 60s, the poster route's rule: the clean master is served at THIS SAME URL once
    # /unlock lands, so a day-long lifetime would keep showing the watermark to a
    # customer who has paid.
    assert resp.headers["cache-control"] == "private, max-age=60"


def test_raw_player_is_inline_and_cacheable(gallery) -> None:
    """The raw card's player URL is a <video src>, not a download."""
    client, store, token = gallery
    store.update("jj", addons={"raw": "ref"})
    web = store.dir("jj") / "raw-web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "GX010052.mp4").write_bytes(b"PROXY")
    resp = client.get(f"/j/{token}/raw-web/GX010052.mp4", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=86400"
    # `filename=` would send `Content-Disposition: attachment` on a player source —
    # the same mistake public_media documents at length.
    assert "attachment" not in resp.headers.get("content-disposition", "")


def test_raw_master_download_stays_an_attachment(gallery) -> None:
    """The Download button keeps saving the file; it just stops re-fetching it."""
    client, store, token = gallery
    store.update("jj", addons={"raw": "ref"})
    raw = store.dir("jj") / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "GX010052.mp4").write_bytes(b"MASTER")
    resp = client.get(f"/j/{token}/raw/GX010052.mp4", follow_redirects=False)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "private, max-age=86400"


def test_unpurchased_raw_is_still_404_not_a_cached_anything(gallery) -> None:
    """Caching changed no access rule: without the add-on every raw path 404s."""
    client, store, token = gallery
    raw = store.dir("jj") / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "GX010052.mp4").write_bytes(b"MASTER")
    assert client.get(f"/j/{token}/raw/GX010052.mp4").status_code == 404
    assert client.get(f"/j/{token}/raw-web/GX010052.mp4").status_code == 404


def test_cdn_redirect_still_wins_over_the_local_cache_header(cdn_env: object, gallery) -> None:
    """With the CDN on, the 302 (and its own small lifetime) is unchanged."""
    client, store, token = gallery
    resp = client.get(f"/j/{token}/media/full_video", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["cache-control"] == "private, max-age=300"


# ---------------------------------------------------------------------------
# The S3 side: one string, shared by the uploader and the backfill script
# ---------------------------------------------------------------------------


def test_uploads_stamp_the_shared_cache_control(tmp_path: Path) -> None:
    from api.delivery import DELIVERY_CACHE_CONTROL, upload_and_link

    # Reuse the delivery suite's fully-populated Settings builder rather than a
    # half-constructed one: upload_and_link reads several fields, and a model_construct
    # fake fails on whichever one it reads next.
    from tests.test_delivery import _settings

    recorded: list[dict] = []

    class FakeS3:
        def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: dict) -> None:  # noqa: N803
            recorded.append({"key": key, **ExtraArgs})

        def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:  # noqa: N803
            return "https://s3.test/x"

    f = tmp_path / "full_video.mp4"
    f.write_bytes(b"x")
    upload_and_link(
        {"full_video": f}, job_id="jj", settings=_settings(), s3_client=FakeS3()
    )

    assert recorded and recorded[0]["CacheControl"] == DELIVERY_CACHE_CONTROL
    # `public` is deliberate HERE and only here: CloudFront is a shared cache and must be
    # allowed to STORE the object. Permission to store is not permission to fetch — the
    # signed URL is the access control, and the bucket itself stays private.
    assert recorded[0]["CacheControl"].startswith("public,")


def test_backfill_only_touches_media_and_is_idempotent() -> None:
    from api.delivery import DELIVERY_CACHE_CONTROL
    from scripts.backfill_delivery_cache_headers import _cacheable, needs_stamp

    assert _cacheable("deliveries/j/full_video.mp4")
    assert _cacheable("deliveries/j/photos/boarding_1.jpg")
    assert _cacheable("deliveries/j/photos.zip")
    # A page bakes its lock state in at delivery, so it must not be cached for a day;
    # the usage manifest is an internal file nothing streams.
    assert not _cacheable("deliveries/j/gallery.html")
    assert not _cacheable("deliveries/j/source_usage.json")

    assert needs_stamp({})  # pre-fix object: no lifetime at all
    assert needs_stamp({"CacheControl": "max-age=60"})  # a DIFFERENT lifetime is corrected
    assert not needs_stamp({"CacheControl": DELIVERY_CACHE_CONTROL})  # second run: no-op


# ---------------------------------------------------------------------------
# ?v= on the LOCAL player URL — the other half of the day-long cache
#
# MEDIA_CACHE_OWNED lets a browser keep a deliverable for 24h at a URL that never
# changes, so an instructor tweak that re-renders the file would otherwise go on
# serving the OLD cut from that cache. The CDN redirect already carries the render
# mtime as `v`; these assert the same busting on the paths the CDN never covers.
# ---------------------------------------------------------------------------
def _video_src(html: str, name: str = "full_video") -> str:
    """The `src` the page gives that deliverable's player."""
    m = re.search(rf'src="(/j/[^"]*/media/{name}[^"]*)"', html)
    assert m, f"no player src for {name} in page"
    return m.group(1).replace("&amp;", "&")


def test_player_url_carries_the_render_mtime(gallery) -> None:
    client, store, token = gallery
    src = _video_src(client.get(f"/j/{token}").text)
    mtime = int((store.dir("jj") / "full_video.mp4").stat().st_mtime)
    assert f"v={mtime}" in src


def test_rerender_changes_the_url_so_the_cached_copy_is_bypassed(gallery) -> None:
    client, store, token = gallery
    before = _video_src(client.get(f"/j/{token}").text)
    # An instructor tweak: same name, same URL path, new bytes and a new mtime.
    master = store.dir("jj") / "full_video.mp4"
    master.write_bytes(b"RE-RENDERED, DIFFERENT CUT")
    os.utime(master, (time.time() + 60, time.time() + 60))
    after = _video_src(client.get(f"/j/{token}").text)
    assert after != before, "a re-render must change the URL or the browser keeps the old cut"
    assert client.get(after).content == b"RE-RENDERED, DIFFERENT CUT"


def test_v_is_ignored_and_never_selects_the_file(gallery) -> None:
    client, store, token = gallery
    # Garbage, stale and absent `v` all stream the same current bytes: it is a cache
    # key, never auth and never file selection.
    for q in ("?v=not-a-number", "?v=1", ""):
        r = client.get(f"/j/{token}/media/full_video{q}")
        assert r.status_code == 200, f"{q} -> {r.status_code}"
        assert r.content == b"CLEAN MASTER BYTES"


def test_locked_card_is_versioned_by_its_PREVIEW_not_the_master(gallery) -> None:
    client, store, token = gallery
    job_dir = store.dir("jj")
    (job_dir / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
    store.update("jj", entitlement=Entitlement.preview_only)
    src = _video_src(client.get(f"/j/{token}").text)
    preview_mtime = int((job_dir / "preview_full_video.mp4").stat().st_mtime)
    master_mtime = int((job_dir / "full_video.mp4").stat().st_mtime)
    assert f"v={preview_mtime}" in src
    # Only meaningful when the two differ; make them differ and re-assert.
    os.utime(job_dir / "full_video.mp4", (time.time() + 120, time.time() + 120))
    master_mtime = int((job_dir / "full_video.mp4").stat().st_mtime)
    assert master_mtime != preview_mtime
    assert f"v={master_mtime}" not in _video_src(client.get(f"/j/{token}").text)


def test_download_anchor_keeps_dl_and_gains_v(gallery) -> None:
    client, store, token = gallery
    html = client.get(f"/j/{token}").text
    m = re.search(r'href="(/j/[^"]*/media/full_video\?dl=1[^"]*)"', html)
    assert m, "no download anchor"
    href = m.group(1).replace("&amp;", "&")
    mtime = int((store.dir("jj") / "full_video.mp4").stat().st_mtime)
    assert f"v={mtime}" in href
    r = client.get(href)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]


def test_pruned_master_still_gets_a_usable_url(gallery) -> None:
    # No local file to stat: the URL simply carries no `v`, which is the pre-fix URL.
    client, store, token = gallery
    (store.dir("jj") / "full_video.mp4").unlink()
    src = _video_src(client.get(f"/j/{token}").text)
    assert "v=" not in src
