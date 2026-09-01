"""CloudFront delivery for the customer gallery's clean video masters (Bug 373).

Why this exists: with no CDN, ``GET /j/{code}/media/{name}`` either streamed the
1080p master *through this API process* from the app host's disk, or — once the
disk-retention sweep pruned the local render — 302-redirected to a presigned S3
URL minted **per request**. A presigned URL authenticates in its query string, so
every playback, replay and page reload produced a *different* URL: the browser
could never reuse a byte it had already fetched, and every viewer pulled the full
MP4 from the S3 origin region again. Far from that region, that is exactly "the
video buffers and reloads every time".

The fix is the standard shape — **browser → CloudFront → S3** — using CloudFront
*signed URLs* so the paywall survives the CDN (the delivery bucket stays private;
CloudFront reads it through Origin Access Control). Two properties carry the fix:

* **The signature must not defeat caching.** CloudFront strips its own signing
  params (``Expires``/``Signature``/``Key-Pair-Id``) from the cache key, so the
  edge holds ONE copy per object no matter who asks. The *browser's* cache is
  protected by minting **deterministic** URLs: the expiry is rounded up to a
  window boundary (:func:`_bucketed_expiry`), so every request inside a window
  gets the byte-identical URL and a replay/reload reuses locally cached ranges
  instead of re-downloading. (A per-request presigned URL never repeats — the
  original bug.)
* **Only what the presigned fallback would already hand out is ever signed.**
  The caller (``public_media``) asks per deliverable and only mints for
  entitlement ``edited_download`` — a locked deliverable never reaches this
  module, exactly as it never gets a presigned S3 URL.

Same contract as every other gallery decoration: **never raises**. Any failure
(unreadable key, missing dependency, bad config) logs a warning and returns
``None``, and the caller falls back to exactly the pre-CDN behaviour — local
``FileResponse``, then the per-request presigned URL. The AWS-side setup
(distribution, OAC, cache policy, key group) is documented in
``deploy/CLOUDFRONT.md``.
"""

from __future__ import annotations

import functools
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


def cdn_enabled(settings: Settings) -> bool:
    """Whether CloudFront delivery is configured (all three settings present)."""
    return bool(
        settings.cdn_base_url
        and settings.cdn_key_pair_id
        and settings.cdn_private_key_path
    )


@functools.lru_cache(maxsize=4)
def _signer(key_pair_id: str, private_key_path: str) -> Any | None:
    """A ``botocore`` CloudFrontSigner for this key pair, or ``None``.

    Cached so the PEM is read and parsed once per process, not once per gallery
    request. ``None`` is cached too: a key that can't be loaded won't start
    loading mid-flight, and the warning below is logged once instead of per hit.
    """
    try:
        from botocore.signers import CloudFrontSigner  # noqa: PLC0415 - lazy, like boto3
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: PLC0415

        pem = Path(private_key_path).read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("CloudFront URL signing requires an RSA private key")

        def rsa_signer(message: bytes) -> bytes:
            # SHA-1/PKCS1v15 is CloudFront's required signing scheme, not a choice.
            return key.sign(message, padding.PKCS1v15(), hashes.SHA1())

        return CloudFrontSigner(key_pair_id, rsa_signer)
    except Exception:  # noqa: BLE001 - a broken key must degrade, not 500 a gallery
        logger.warning(
            "CloudFront signer unavailable (key pair %s, key file %s) — falling back "
            "to non-CDN delivery",
            key_pair_id,
            private_key_path,
            exc_info=True,
        )
        return None


def _bucketed_expiry(now: float, window_s: int) -> datetime:
    """Expiry rounded UP to a multiple of ``window_s`` → deterministic URLs.

    Every call inside one window yields the same instant, so the signed URL —
    and therefore the browser's cache key — is stable for the whole window.
    Remaining validity is always in ``[window_s, 2*window_s)``: never so short
    that a URL minted at a window's edge dies mid-playback.
    """
    return datetime.fromtimestamp(
        (int(now // window_s) + 2) * window_s, tz=UTC
    )


def signed_delivery_url(
    job_id: str,
    filename: str,
    settings: Settings,
    *,
    version: int | None = None,
    now: float | None = None,
) -> str | None:
    """A CloudFront signed URL for ``deliveries/{job_id}/{filename}``, or ``None``.

    ``version`` (the local render's mtime, when the caller still has the file) is
    carried as a ``?v=`` param and signed with the URL: the CloudFront cache policy
    includes ``v`` in the cache key, so a re-rendered-and-re-delivered job busts the
    edge copy instead of serving the stale edit for the object's ``Cache-Control``
    lifetime. Omitted (e.g. the local file was pruned — at which point the S3 copy
    can no longer change without a re-render recreating the local file first), the
    bare path is used.

    ``now`` is injectable for tests; never raises — ``None`` on any failure.
    """
    if not cdn_enabled(settings):
        return None
    signer = _signer(str(settings.cdn_key_pair_id), str(settings.cdn_private_key_path))
    if signer is None:
        return None
    try:
        from .delivery import delivery_s3_key  # noqa: PLC0415 - avoid import cycle at module load

        url = f"{settings.cdn_base_url}/{delivery_s3_key(job_id, filename)}"
        if version is not None:
            url += f"?v={version}"
        expiry = _bucketed_expiry(
            now if now is not None else time.time(), int(settings.cdn_url_ttl_s)
        )
        return str(signer.generate_presigned_url(url, date_less_than=expiry))
    except Exception:  # noqa: BLE001 - CDN failure must never 500 the gallery
        logger.warning(
            "CloudFront URL signing failed for %s/%s", job_id, filename, exc_info=True
        )
        return None
