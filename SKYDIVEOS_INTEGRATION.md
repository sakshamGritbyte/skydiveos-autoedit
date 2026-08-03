# SkydiveOS ↔ Auto-Edit integration contract

How SkydiveOS drives a jump through this module, end to end, with **no human step**.
This is the spec the SkydiveOS team codes against.

## The division of labour

This module knows nothing about **loads, bookings, customers, or add-ons** — its only
database is the paired-camera registry (`camera_id → instructor_id`). Everything about
*who the customer is and what they bought* lives in SkydiveOS. So SkydiveOS owns the
match from footage → booking, and passes the result in when it creates the job.

```
 GoPro card
   │  auto-discovery (this module): pulls MP4s, uploads to S3,
   │  POSTs { s3_key, camera_id, instructor_id?, camera_role?, captured_at? } to SkydiveOS
   ▼
 SkydiveOS                     ← owns loads/bookings/customers/add-ons
   │  1. match footage → the manifested jump → its booking
   │  2. read customer_email + add-ons → package
   │  3. POST /jobs          (create with booking metadata)
   │  4. attach the footage  (see "Attaching footage" — the open question)
   ▼
 Auto-Edit pipeline           ← edits, renders, AUTO_DELIVER → emails customer
   │  POSTs status callbacks back to SkydiveOS on every state change
   ▼
 Customer inbox ✉️
```

**Do not** match footage to "the most recent landed load/instructor" — that mis-assigns
when two tandems land close together or an instructor jumps back-to-back, and the failure
mode is emailing Customer A's video to Customer B. Match deterministically on
`camera_id` + the footage **capture timestamp** → the specific manifested jump. The
discovery POST now carries that timestamp as **`captured_at`**, a **true-UTC** ISO-8601
instant added by the auto-edit ingest module. (GoPro writes the camera's *local*
wall-clock into `creation_time` and mislabels it UTC; the ingest module reinterprets it
via the dropzone's `CAMERA_CLOCK_TZ` — e.g. `America/Toronto` — so what you receive is a
real UTC instant, safe to convert to DZ-local and match against the flight window.)
`camera_role` (`instructor`/`external`) also rides along so the two-camera Ultimate
product routes correctly. `captured_at` is omitted only when the file's tag is
unreadable — fall back to the load time-window match, and refuse-and-flag on ambiguity.

---

## 1. `POST /jobs` — create the job

The single most important call. SkydiveOS sends the booking-derived metadata; the job
starts `queued`.

```http
POST /jobs
Content-Type: application/json
X-Instructor-Id: inst-42        # only if ENFORCE_INSTRUCTOR_AUTH=1 (see Auth)
X-Role: instructor
```
```json
{
  "customer_name":   "Jane Doe",
  "customer_email":  "jane@example.com",
  "instructor_name": "Marc Tremblay",
  "package":         "selfie",
  "booking_id":      "BK-1001",
  "jump_date":       "2026-07-27",
  "target_duration": 90.0,
  "music":           null,
  "entitlement":     "edited_download"
}
```

| Field | Type | Required for auto-deliver? | Notes |
|---|---|---|---|
| `customer_name` | string | recommended | Burned onto the intro card. Defaults to "Valued Skydiver". |
| `customer_email` | string | **yes** | Where the finished links are emailed. **Without it the pipeline can't email the customer** — it will only hand the links back to SkydiveOS via the status callback. |
| `package` | enum | **yes** | Drives the whole pipeline & deliverable set. See the add-on table below. |
| `booking_id` | string | recommended | Your booking reference; round-trips on every response/callback. |
| `instructor_name` | string | recommended | The instructor's display name. Names their folder in the dropzone's jump archive (`raw-storage/{jump date}/{instructor}/{customer}/`, see CLAUDE.md). You own the staff records — we only store what you send. Omitted → the folder falls back to `instructor_id`, then `_no-instructor`. |
| `jump_date` | ISO date | optional | Defaults to render-day. Also the archive's top-level date folder — send it if you want the jump filed under the day it was *flown* rather than the day it was edited. |
| `target_duration` | float (s) | optional | Final length target; default 90. |
| `music` | string | optional | Track **stem** from `templates/music/` (e.g. `"Fly Away - Lenny Kravitz"`). Omit/`null` → the pipeline picks a random default track once and reuses it. |
| `entitlement` | enum | optional | `edited_download` (default — media purchased, Path A) or `preview_only` (speculative capture, Path B: the gallery streams a watermarked 720p preview behind the unlock paywall). Uppercase (`PREVIEW_ONLY`) is accepted. See §7. |

Response `201`:
```json
{ "job_id": "3f9c…", "job": { "job_id": "3f9c…", "status": "queued", … } }
```
Keep the returned `job_id` — every later call keys off it.

---

## 2. Attaching the footage

The job is created empty; the raw MP4s must be attached before it can run. Three sources,
all on `POST /jobs/{job_id}/upload` — pick exactly one:

**(a) S3 key** — the auto-discovery / cloud path (**recommended**). The master is already
in S3 (`raw/{camera_id}/{file}`), so hand the job the key instead of re-streaming
multi-GB bytes through the web layer; the worker downloads it and dispatches the same
pipeline:
```http
POST /jobs/{job_id}/upload      # form-data
s3_key=raw/1234/GH010001.MP4
```
For the two-camera **`ultimum`** package, send one call per camera with a `camera_role`
form field (`instructor` / `external`); processing auto-starts once *both* roles have
downloaded. The `s3_key` must end in `.mp4`; needs `S3_BUCKET` set on the worker.

**(b) Direct byte upload** — for when you have the bytes in hand:
```http
POST /jobs/{job_id}/upload      # multipart/form-data
files=@GH010001.MP4  files=@GH010002.MP4
```
Same `camera_role` rule for `ultimum`.

**(c) Camera pull** — `POST /jobs/{job_id}/upload` with form field `camera_id=1234`
(needs the GoPro on the dropzone network; not the EC2/cloud path).

The response's `source` field reports which path ran (`"s3"` / `"upload"` / `"pull"`).

---

## 3. Add-on → `package` mapping (SkydiveOS-side)

SkydiveOS translates the booking's add-ons into exactly one `package` value. The five
packages and what each emits:

| `package` | Deliverables | Cameras | Use when the booking has… |
|---|---|---|---|
| `selfie` | full_video, highlights, freefall, **photos** | 1 (instructor handcam) | Standard handcam video + photos |
| `external` | full_video, highlights, freefall, **photos** | 1 (camera-flyer) | Outside-video add-on (distant cameraman) |
| `video_only` | full_video, highlights, freefall | 1 | Video add-on, no photos |
| `photo_only` | photos (~90–100) | 1 | Photos add-on only |
| `ultimum` | full_video + highlights (multi-cam combo), external_freefall, chute_libre_selfie, photos | **2** (instructor + external) | The "Ultimate" two-camera product |

Fill the left column with your real add-on SKUs; the right column is the intended
mapping. `ultimum` is the only package that requires the two-camera `camera_role` upload.

---

## 4. Per-deliverable music (optional)

If a customer picks per-deliverable tracks, upload them **before** processing:
```http
POST /jobs/{job_id}/music       # multipart; form field: deliverable=<name>
file=@epic.mp3
```
Valid `deliverable` values come from the package's `music_deliverables`:
- `selfie`/`external`/`video_only`: `full_video`, `highlights`, `freefall`
- `ultimum`: `full_video`, `highlights`, `external_freefall`, `chute_libre_selfie`
- `photo_only`: none

Precedence at render: uploaded track → booking's named `music` → random default.

---

## 5. Status callbacks — this module → SkydiveOS

Set `SKYDIVEOS_API_BASE` (host root — the same base the raw-upload POST uses) and the
pipeline fires a callback on **every** state change, to SkydiveOS's receiver:

```http
POST {SKYDIVEOS_API_BASE}/api/media/auto-edit/jobs/{job_id}/status
X-Auto-Edit-Token: <shared secret>      # only when AUTO_EDIT_CALLBACK_TOKEN is set
```
```json
{ "job_id": "3f9c…", "status": "delivered",
  "entitlement": "preview_only",
  "media_state": "LOCKED_PREVIEW",
  "gallery_url": "https://freefall.ing/j/w4535adlq7k",
  "delivery_links": { "full_video": "https://…", "highlights": "https://…", "photos": "https://…" } }
```
`entitlement` is on **every** callback. `gallery_url` is present whenever the auto-edit
service has `PUBLIC_BASE_URL` set — the short, **never-expiring** customer link
(§7). **This is the URL to SMS/email the customer**; append your own source tag if you
track channels (`?s=m` for SMS, `?s=e` for email — the server accepts and ignores it).

`delivery_links` is present **only** on `status: "delivered"` — the presigned customer
download URLs (7-day expiry). Use them to show/re-send the video in SkydiveOS, or as the
fallback delivery channel when a job had no `customer_email`. Prefer `gallery_url` for
anything customer-facing: presigned links die after 7 days, the gallery link does not.
The receiver should dedupe
per `{job_id}:{status}` (this callback is fire-and-forget and may retry). If it verifies
`AUTO_EDIT_CALLBACK_TOKEN`, set the **same** value here so the header matches — and
confirm the header name (`X-Auto-Edit-Token`) matches what the receiver checks.

Job lifecycle (the `status` values you'll receive):
```
queued → processing → ready_for_review ─┐
                                         ├─(AUTO_DELIVER=1)→ approved → delivered
   (instructor gate, if AUTO_DELIVER=0) ─┘
processing → failed   (on any pipeline error; `error` field is set; resumable)
```
With `AUTO_DELIVER=1` the review gate is skipped and delivery is automatic. With it off,
SkydiveOS drives the gate via `POST /jobs/{id}/approve`.

### `media_state` — the design doc's state machine (for your UI)

`status` is this service's *pipeline* state. Every callback and every `GET /jobs/{id}`
also carries **`media_state`**, the design doc's product-facing state — derived from
`status` + `entitlement` + `paid_at`, so you can label a jump without re-deriving it:

```
PENDING_CAPTURE → UPLOADED → EDITING → READY ─┬─ DELIVERED        (Path A, terminal)
                                              └─ LOCKED_PREVIEW → UNLOCKED  (Path B)
                                          FAILED                  (any stage, on error)
```

| `media_state` | when |
|---|---|
| `PENDING_CAPTURE` | job created, footage not in hand (an Ultimate job awaiting its 2nd camera reads this too) |
| `UPLOADED` | footage staged, worker not started |
| `EDITING` | `processing`, or a rejected job heading back through the editor |
| `READY` | rendered, hand-off not done |
| `DELIVERED` | Path A, customer has the clean edit |
| `LOCKED_PREVIEW` | Path B, watermarked preview behind the paywall (even after the link went out) |
| `UNLOCKED` | Path B after `POST /jobs/{id}/unlock` |
| `FAILED` | `error` is set; resumable |

It is **read-only and never stored**: drive UI copy off `media_state`, and keep driving
the review gate off `status`. (`CAPTURED` / `INGESTING` exist in the vocabulary for the
camera-side steps that happen before a job record exists; this service never emits them.)

---

## 6. Entitlement: the "film it anyway" paywall (Path A / Path B)

Every job carries an `entitlement`, and every job gets a customer gallery at a short link
`{PUBLIC_BASE_URL}/j/{code}` (e.g. `https://freefall.ing/j/w4535adlq7k?s=e#tab-video`)
served **live** by this API — so it never expires and flips state without a re-render.

| | **Path A** `edited_download` | **Path B** `preview_only` |
|---|---|---|
| When | The booking bought media | Speculative capture — nothing bought |
| Gallery streams | The clean 1080p deliverables | Watermarked **720p previews** |
| Downloads | Enabled | Locked, behind the unlock CTA |
| Photos | The full grid | Hidden (a "N photos included" teaser) |

Both paths render and store the **clean masters** up front. So when payment lands, the
unlock is instant — no re-render, no re-delivery, same link.

### What SkydiveOS must do

1. **Send `entitlement` on `POST /jobs`** — `edited_download` when the booking bought
   media, `preview_only` when it didn't. **This is the behaviour change that creates Path
   B:** today a jumper with `mediaPackage: none` gets no job at all; to sell them a video
   you must now create one anyway, with the role-default package
   (`instructor` → `selfie`, `external` → `external`) and `entitlement: "preview_only"`.
   (Mirrors `ingest.match.package_and_entitlement_for`, which this repo's own matcher uses
   — see `scripts/check_match.py --day`, which now reports such jumps as `PREV`.)
2. **SMS/email the `gallery_url`** from the `delivered` callback (§5). Phone numbers,
   templates, and SMS transport live in SkydiveOS — this module has no phone field and
   sends email only.
3. **Build the unlock checkout** (the `$39` product/page). Give the auto-edit deployment
   its URL pattern via `CHECKOUT_URL_TEMPLATE` (placeholders `{job_id}`, `{booking_id}`,
   `{item}`), e.g. `https://ultimatedzm.com/unlock/{job_id}` — that's what the locked
   gallery's CTA links to. Until it's set the CTA renders as text ("ask at the desk"),
   never a dead link.
   The same template backs the landing page's **"Add to your day" upsell row**, which
   renders on *both* the locked and the unlocked page (it's a second revenue line either
   way). `{item}` receives the tile key — `unlock` for the CTA itself, and whatever keys
   the deployment configures in `UPSELL_TILES` (default `raw` / `photos` / `rebook`), so
   one checkout route can serve them all. Fulfilment of those add-ons is SkydiveOS's:
   this module only renders the offer and links out.
4. **Call the unlock hook once payment is captured:**
   ```http
   POST /jobs/{job_id}/unlock
   Authorization: Bearer <AUTO_EDIT_API_KEY>     # required (see §8)
   X-Role: admin
   ```
   ```json
   { "payment_reference": "clover_txn_9f21c7", "amount": 39.0 }
   ```
   Returns `200` + the job (`entitlement: "edited_download"`, `paid_at` stamped,
   `payment_reference` persisted). **Idempotent** — safe to retry, and a second call
   changes nothing (the first reference is kept). It never touches `status`, so it's
   independent of the review/delivery machine. The customer's next page load shows the
   clean video with downloads enabled.

   This endpoint hands over the product, so it has three gates: the **service token**,
   the **admin** role, and a **non-empty `payment_reference`** (your captured
   transaction id — recorded so every unlock is auditable back to real money). It is
   **server-to-server only**: never expose it to a browser, and never call it before
   capture. On the SkydiveOS side this is already wired —
   `paymentEventHandler.onBookingPaymentCompleted` calls
   `autoEditOrchestrationService.unlockPaidMedia()` when the payment's
   `metadata.paymentScope === 'media-unlock'`, so the unlock checkout only has to set
   that scope (a payment for the *jump* must never unlock media).
5. **Point `freefall.ing` (or your chosen host) at this service** and set
   `PUBLIC_BASE_URL` to that origin. Terminate TLS at your proxy. Unset → delivery falls
   back to the legacy S3-hosted `gallery.html` with 7-day presigned links, and no
   `gallery_url` is sent.

The short code is the gallery's **only** credential (11 random base62 chars): there are no
identity headers on `/j/{code}` even when `ENFORCE_INSTRUCTOR_AUTH=1`, since the customer
has no SkydiveOS account. Treat it like a password — don't log it or put it in referrers.
While locked, the clean master is unreachable at any URL: the entitlement picks the file,
not the request.

---

## 7. Auth (optional, off by default)

When `ENFORCE_INSTRUCTOR_AUTH=1`, SkydiveOS forwards identity on every request:
`X-Instructor-Id: <id>` and `X-Role: instructor|admin`. Instructors see only their own
jobs/cameras; admins see all. Off by default (every caller is treated as admin), so the
open flow needs no headers. Ownership *tagging* (via the camera's `instructor_id`) happens
regardless.

---

## 8. Service token (required in any reachable deployment)

Because the identity in §7 is **self-asserted** — a caller states its own id and role —
it is only meaningful if the caller is trusted. So a shared secret gates the whole API:

```http
Authorization: Bearer <AUTO_EDIT_API_KEY>
```

* Set the **same** value as this service's `AUTO_EDIT_API_KEY` and SkydiveOS's
  `AI_BACKEND_API_KEY` / `AUTO_EDIT_API_KEY`. SkydiveOS's `aiBackendFetch` /
  `autoEditService` already send it — configure the env var and nothing else changes.
* **Exempt:** `GET /j/{code}` and its media/photo routes (the customer holds a short
  code, not a token) and `OPTIONS` preflights.
* **Unset = no gate.** Only safe if the service is unreachable from the internet.

Why it exists: with the gate off and the service reachable, every anonymous caller is
an admin. Measured on production 2026-08-03 before this shipped: anonymous `GET /jobs`
returned every customer's name, email and delivery links, and a ranged
`GET /jobs/{id}/deliverables/{name}` streamed their finished video. The browser no
longer talks to this API at all — SkydiveOS proxies those calls
(`/api/media/ai-jobs/...`) — so the only clients are the SkydiveOS backend and the
dropzone ingest hosts. Network-level rules are the other half:
[`deploy/PROXY_LOCKDOWN.md`](deploy/PROXY_LOCKDOWN.md).

---

## Minimal happy-path sequence (cloud / auto-discovery mode)

```bash
# 1. create — from the { s3_key, camera_id, instructor_id } discovery POSTed you,
#    plus the booking you matched it to.
JOB=$(curl -s -XPOST $AE/jobs -H 'Content-Type: application/json' -d '{
  "customer_name":"Jane Doe","customer_email":"jane@example.com",
  "package":"selfie","booking_id":"BK-1001"}' | jq -r .job_id)

# 2. attach footage straight from the S3 key (no re-upload; auto-enqueues the pipeline)
curl -s -XPOST $AE/jobs/$JOB/upload -F s3_key=raw/1234/GH010001.MP4

# 3. with AUTO_DELIVER=1: nothing else. Watch the callbacks land on SkydiveOS,
#    ending in {"status":"delivered","delivery_links":{…}} — customer already emailed.
```

(For the on-dropzone byte path, swap step 2 for `-F files=@GH010001.MP4`.)

Everything after step 2 is automatic. The two prerequisites SkydiveOS owns:
**pass `customer_email` + the right `package`**, and implement the `/jobs/{id}/status`
receiver.

For a **Path B** (speculative) jump, step 1 adds `"entitlement":"preview_only"`, and one
extra call joins the sequence once the customer pays:

```bash
# 4. payment captured at your checkout → unlock the clean video (idempotent)
curl -s -XPOST $AE/jobs/$JOB/unlock
```
