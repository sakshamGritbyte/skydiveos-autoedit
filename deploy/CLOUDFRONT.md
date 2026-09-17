# CloudFront in front of the delivery bucket (Bug 373)

The customer gallery's videos buffered and appeared to re-download on every play or
reload. Root cause, verified in code:

* With the render still on the app host's disk, `GET /j/{code}/media/{name}` streamed
  the full 1080p MP4 **through the FastAPI process** — one origin, no edge caching,
  and a viewer far from the server (e.g. India vs a North-American EC2) pulls every
  byte across that distance.
* Once `scripts/prune_jobs.py` swept the local render (default 7 days after delivery,
  cron'd in production), the route 302-redirected to a **presigned S3 URL minted per
  request** (`api/app.py:_presigned_delivery_url`). Presigned URLs authenticate in the
  query string, so every play/replay/reload got a *different* URL — the browser could
  never reuse a cached byte, and every viewing session re-fetched the whole MP4 from
  the S3 origin region.
* CRF-only encoding had no bitrate ceiling: a real delivered `full_video` measured
  **~19 Mbit/s** at 1080p (CRF 23 + `veryfast` on high-motion freefall), so real-time
  playback needed a sustained ~20 Mbit/s link even from a nearby edge. The deliverable
  encodes now carry a VBV cap (`-maxrate 12M -bufsize 24M` — `render/render.py`,
  `api/selfie.py`); CRF and preset are unchanged, so quality on normal scenes is
  untouched and only the spikes are clamped. Existing renders keep their bitrate until
  re-rendered.

The fix (in `api/cdn.py` + `public_media` in `api/app.py`): once a job is `delivered`,
an **unlocked** deliverable's player fetch redirects to a **CloudFront signed URL**
instead. CloudFront serves range requests from an edge near the viewer, its cache key
ignores the signing params (one cached copy per object), and this service mints the
URL **deterministically within a time window**, so a replay or reload reuses the
browser's own cache. Locked (`preview_only`) deliverables never get a CDN URL — their
watermarked previews are local-only by design and keep streaming from this API.

Everything in this repo degrades safely: with the env vars unset (or the key
unreadable), behaviour is byte-identical to before. **The AWS resources below must be
created manually — nothing in this repository deploys them.**

### The half that works with NO CDN

The CDN redirect only covers a **delivered, unlocked video**. Every other public media
byte still leaves the API process — an undelivered render, a purchased raw master and
its web proxy, the load video, the photo stills, and, on any stack where `CDN_BASE_URL`
is unset, the delivered videos too. Starlette sends `etag`/`last-modified` but no
lifetime, and without one a browser revalidates (or re-fetches outright) on every play.
So the served routes now carry `Cache-Control` of their own (`api/app.py`):

| What | Header | Why |
|---|---|---|
| A video/still/raw proxy the customer OWNS | `private, max-age=86400` | A replay reuses bytes it already has |
| A locked deliverable's watermarked preview, a locked still | `private, max-age=60` | The clean file is served at the SAME URL after `/unlock` — a watermark must not outlive the payment |
| The CDN redirect itself | `private, max-age=300` | Spares a round-trip per range request |

Every player and download URL the page emits carries `?v={mtime of the file that
request would serve}` (`api.app._media_url`). Without it the day-long cache above sits
on a URL that never changes, so a re-render after an instructor tweak would keep serving
the old cut from the viewer's browser for up to 24 h — the CDN path already signs this
same value, and this is it on the paths the CDN never covers. Cache key only: the route
ignores it, and the entitlement still picks the file.

`private`, never `public`: unlike the S3 objects behind CloudFront, these responses go
straight to the viewer with the gallery's short code as their only credential, so no
shared or proxy cache may store them. Access control is unchanged — the raw routes still
404 without the add-on, and a locked deliverable still serves only its preview.

## 1. What to create in AWS

All of this fronts the existing delivery bucket (`$S3_BUCKET`), touching only the
`deliveries/*` prefix. The `raw/*` prefix and every other flow are unaffected.

### 1.1 Key pair for signed URLs

```bash
openssl genrsa -out cdn_private_key.pem 2048
openssl rsa -pubout -in cdn_private_key.pem -out cdn_public_key.pem
```

* CloudFront console → **Public keys** → upload `cdn_public_key.pem`. Note the id
  (`K…`) — this is `CDN_KEY_PAIR_ID`.
* CloudFront console → **Key groups** → create one containing that public key.
* Put `cdn_private_key.pem` on the box running this API (e.g.
  `/etc/skydiveos/cdn_private_key.pem`, mode 600) — this is `CDN_PRIVATE_KEY_PATH`.
  Do not commit it; rotate by uploading a new public key, adding it to the key group,
  swapping the PEM, then removing the old key.

### 1.2 Cache policy

Create a cache policy (or start from *CachingOptimized*) with:

* **Query strings: include `v` only.** The service signs a `?v=<render mtime>` param
  into player URLs; including it in the cache key is what busts the edge copy when a
  job is re-rendered and re-delivered under the same S3 key. CloudFront's own signing
  params (`Expires`, `Signature`, `Key-Pair-Id`) are never part of the cache key.
* **Headers: none. Cookies: none.** (Authorization lives in the signed URL.)
* TTLs: min 0 / default 86400 / max 604800 — the objects also carry
  `Cache-Control: public, max-age=86400`, set at upload by `api/delivery.py`.
* Compression off for this behavior (video is already compressed; gzip/brotli on MP4
  wastes CPU).

### 1.3 Distribution

* **Origin**: the delivery bucket, with **Origin Access Control (OAC, sign requests)**
  — never "public bucket". Keep S3 Block Public Access ON.
* **Default behavior** (or a behavior on `deliveries/*`):
  * Viewer protocol policy: redirect HTTP→HTTPS.
  * Allowed methods: **GET, HEAD** only.
  * **Restrict viewer access: yes → the key group from 1.1.** This is the paywall's
    other half: without it, anyone who learns a path could fetch the object.
  * Cache policy: the one from 1.2. No origin request policy needed.
* Range requests / `206 Partial Content` are supported by CloudFront natively (it
  fetches and caches the object in parts); nothing to configure.
* Optional custom domain (e.g. `media.ultimatedzm.com`): add the alternate domain
  name + an ACM certificate (us-east-1), and a CNAME/alias in DNS. Otherwise use the
  `dxxxxxxxxxxxx.cloudfront.net` domain directly.

### 1.4 Bucket policy

Grant the distribution read access via OAC (the console offers to copy this):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowCloudFrontOAC",
    "Effect": "Allow",
    "Principal": {"Service": "cloudfront.amazonaws.com"},
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::YOUR_DELIVERY_BUCKET/deliveries/*",
    "Condition": {"StringEquals": {
      "AWS:SourceArn": "arn:aws:cloudfront::ACCOUNT_ID:distribution/DISTRIBUTION_ID"
    }}
  }]
}
```

The bucket itself stays private; presigned URLs used elsewhere (delivery emails,
SkydiveOS callbacks, the pruned-download fallback) keep working unchanged.

## 2. Configure this service

```bash
CDN_BASE_URL=https://media.ultimatedzm.com     # or https://dxxxx.cloudfront.net
CDN_KEY_PAIR_ID=K2JCJMDEHXQW5F                 # the public key id from 1.1
CDN_PRIVATE_KEY_PATH=/etc/skydiveos/cdn_private_key.pem
CDN_URL_TTL_S=43200                            # optional; 12 h default
```

Restart the API. Per-tenant deployments (ports 8010-8012) each need their own env —
they can share one distribution and key pair if they share the delivery bucket.

## 3. Verify

**One command, five isolated steps** — run it on the box that serves the galleries,
with the deployment's env loaded. It says which link in the chain is broken instead of
leaving you to infer it from a missing header:

```bash
cd /opt/skydiveos-autoedit && set -a && . ./.env && set +a && \
    .venv/bin/python scripts/cdn_healthcheck.py
```

It checks, in order: the three settings + that the PEM loads into a signer; the S3
object (reporting its `Cache-Control` and average bitrate); that two signed URLs minted
a second apart are **byte-identical** (the determinism that makes a replay cheap); that
the edge answers a range request `206` and reports `X-Cache: Hit` on a repeat; and that
the same URL **without** its signature is refused `403`. It fetches one kilobyte and
writes nothing. With no `--job` it picks the newest delivered job, which is the one a
customer is most likely watching.

This matters because the code degrades *silently* by design: an unconfigured stack
behaves exactly like a configured one from the outside, minus the header nobody reads.
`[FAIL] config: CDN delivery is OFF` is the answer to "did we ever actually turn this
on here?".

The manual equivalents, if you want to see the raw exchange:

```bash
# 1. A delivered, unlocked job's player URL redirects to the CDN with a signature:
curl -sI "https://<PUBLIC_BASE_URL>/j/<code>/media/full_video" | grep -i '^location'
#    → Location: https://media.../deliveries/<job>/full_video.mp4?v=...&Expires=...&Signature=...&Key-Pair-Id=...

# 2. The CDN answers range requests with 206 and gets cache hits on a repeat:
curl -sI -H 'Range: bytes=0-1023' '<that Location URL>' \
  | grep -iE 'HTTP|content-range|accept-ranges|x-cache'
#    first request: X-Cache: Miss from cloudfront — repeat it: Hit from cloudfront

# 3. Determinism (what makes replays cheap): request #1's Location equals request #2's.

# 4. The paywall holds: a locked deliverable still streams the watermarked preview
#    from this API (200, no redirect), and the CDN URL without a valid signature is
#    refused:
curl -sI 'https://media.../deliveries/<job>/full_video.mp4'   # → 403 MissingKey
```

`?dl=1` (the gallery's Download buttons) deliberately bypasses the CDN and serves an
attachment — a cross-origin redirect would make browsers ignore the `download`
attribute and play the file instead of saving it.

## 4. Backfill the objects that predate the fix

`api/delivery.py` stamps `Cache-Control: public, max-age=86400`
(`DELIVERY_CACHE_CONTROL`) on everything it uploads, but an object already in the
bucket keeps the metadata it was written with — and a gallery link never expires, so
the galleries most likely to be replayed are exactly the ones written before the fix.
Measured on this account 2026-09-09: **34 of 49 sampled delivery MP4s carried no
`Cache-Control` at all**, across all three media buckets.

With no lifetime on the object, CloudFront still caches it (the distribution's cache
policy has its own default TTL), but the **viewer's browser is told nothing** and
revalidates — or re-fetches the whole MP4 — on every play. That is the reported symptom,
and on a pre-fix object it survives the CDN.

```bash
# Dry-run is the DEFAULT: it lists what would change and writes nothing.
.venv/bin/python scripts/backfill_delivery_cache_headers.py
.venv/bin/python scripts/backfill_delivery_cache_headers.py --apply

# Other tenants' buckets (repeatable; each stack has its own):
.venv/bin/python scripts/backfill_delivery_cache_headers.py \
    --bucket skydiveos-northshore-media --bucket skydiveos-southshore-media --apply
```

It is a **metadata-only** `CopyObject` onto the same key: the bytes, key, content type
and storage class are preserved, nothing is deleted or re-encoded, and a second run
finds nothing to do. `gallery.html` and `source_usage.json` are deliberately skipped —
a page bakes its lock state in at delivery, so a day of browser caching there could show
a stale paywall.

## 5. Operational notes

* **Re-delivered jobs**: the signed `?v=` param (local render mtime) changes on a
  re-render, so players fetch the fresh edit without an invalidation. If you must
  purge manually: `aws cloudfront create-invalidation --distribution-id …
  --paths '/deliveries/<job_id>/*'`.
* **URL lifetime**: signed URLs are stable within a `CDN_URL_TTL_S` window and valid
  for one to two windows (12–24 h at the default) — much shorter than the 7-day
  presigned delivery links, and the gallery re-mints transparently per request.
* **Failure mode**: any CDN misconfiguration (bad key path, unreadable PEM) logs a
  warning and falls back to the pre-CDN path per request. The gallery never 500s
  because of the CDN.
* **What is NOT routed through CloudFront** (unchanged, deliberately): watermarked
  previews and photo previews (local-only, the paywall product), photos, posters, raw
  masters, load videos, the legacy S3 `gallery.html` path, and the presigned links in
  delivery emails / SkydiveOS callbacks.
* **Encode bitrate** (Bug 373 item 3, re-measured 2026-09-09 on real delivered
  renders): whole files came off at **8.3–9.5 Mbit/s** at 1080p30 and a 20 s
  high-motion excerpt at **11.3**, so the original 12M cap was barely binding and we
  were shipping roughly twice a streaming service's 1080p. `render.render.MAXRATE` is
  now **8M / 16M**, which took the same excerpt to 8.2 Mbit/s at a cost of 0.17 dB
  PSNR / 0.0009 SSIM against the source (a CRF 25 variant lost 1.9 dB and was
  rejected). CRF and preset are untouched, so this clamps peaks only; `api/selfie.py` and `api/rawproxy.py` import
  the constants rather than repeating them. **Existing renders keep their bitrate until
  re-rendered** — the backfill in §4 is metadata-only and does not re-encode.
* **HLS/adaptive bitrate is deliberately NOT implemented**, and the blocker is
  specific rather than a matter of effort. The gallery page is a **single
  self-contained HTML string with no external assets** (`api/gallery.py`) — that is
  what lets the same renderer serve both the live `/j/{code}` route and the legacy S3
  `gallery.html` object. HLS needs a media-source player in every non-Safari browser
  (hls.js, ~400 KB), so adopting it means either loading a third-party script into a
  page whose URL *is* the customer's credential, or inlining 400 KB into every gallery
  render. On top of that: a per-job packaging step (three encodes instead of one, on a
  box already CPU-bound per jump), ~1.6× delivery storage, and — because each segment
  request needs its own authorization — either playlists rewritten with a signed URL
  per segment or CloudFront **signed cookies**, which is a different auth model from
  the one the paywall uses everywhere else.

  The route that *would* fit this architecture, if far-from-edge viewers on slow links
  still stall after the CDN is live: serve the `.m3u8` from this API
  (`/j/{token}/hls/{name}.m3u8`, entitlement checked exactly where `public_media`
  checks it today) with each segment URI a **deterministic CloudFront signed URL** —
  the paywall, the edge caching and the replay-determinism all carry over unchanged.
  The player remains the open question, not the delivery.
