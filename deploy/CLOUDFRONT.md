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

## 4. Operational notes

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
* **HLS/adaptive bitrate** is *not* part of this fix. Progressive MP4 (`+faststart`,
  already in place) + edge caching + range requests covers the reported failure; an
  HLS ladder (1080/720/480) would add a per-job packaging step, ~1.6× storage, and a
  player library on the gallery page. Revisit only if far-from-edge viewers on slow
  links still stall *after* CloudFront is live.
