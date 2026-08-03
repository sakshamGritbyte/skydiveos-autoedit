# Go-live checklist — fully automatic camera → customer

Everything in this repo is built and tested. Two sides remain: **(A)** config on the
EC2 box, **(B)** the SkydiveOS integration work. Do both and the loop is live.

---

## A. EC2 env-var checklist

Both containers (`api` + `worker`) read the same `.env` (see `docker-compose.yml`
`env_file: .env`), so you edit **one file** and restart. Add/confirm these in
`/…/skydiveos-autoedit/.env` on the EC2 box:

```bash
# ── HARD GATES: the paywall (Path B) will not run without these ───
# 1. Where customers reach their gallery. This is a PREREQUISITE, not a nicety: a
#    preview_only ("we filmed it anyway") job can only be delivered as the served
#    /j/{code} page, because the legacy S3 gallery presigns the CLEAN masters and
#    would hand over the unbought edit. `POST /jobs` REFUSES to create a
#    preview_only job while this is unset (422), and the API logs it at boot — so
#    the delivery-time failure can't reach production, but Path B is simply
#    unavailable until this is set. Must be the origin this API answers on.
PUBLIC_BASE_URL=https://ai.ultimatedzm.com
# 2. Shared secret gating every route except /j/*. Identity here is self-asserted
#    (X-Instructor-Id / X-Role), so on a reachable box an ungated API treats
#    strangers as admins — anonymous /jobs leaks customer names/emails/links, and
#    /unlock gives paid videos away. Same value as SkydiveOS's AI_BACKEND_API_KEY.
AUTO_EDIT_API_KEY=<long-random-secret>
# 3. Network rules are the other half of #2 — only /j/* may be public:
#    see deploy/PROXY_LOCKDOWN.md (human-owned, do before deploying the paywall).

# ── Turn on fully-automatic delivery ──────────────────────────────
AUTO_DELIVER=1                         # skip the review gate; deliver on render finish

# ── Customer email transport (AWS SES SMTP shown) ─────────────────
SMTP_HOST=email-smtp.ap-south-1.amazonaws.com   # your SES region's SMTP endpoint
SMTP_PORT=587
SMTP_USER=<SES-SMTP-username>          # SES → SMTP settings → create credentials
SMTP_PASSWORD=<SES-SMTP-password>
SMTP_STARTTLS=true
DELIVERY_FROM_EMAIL=videos@yourdropzone.com     # MUST be a verified SES identity
DELIVERY_LINK_TTL_DAYS=7               # link lifetime (S3 caps at 7)

# ── S3 (already set for you) ──────────────────────────────────────
S3_BUCKET=skydivingoss                 # or AWS_S3_BUCKET_NAME
AWS_REGION=ap-south-1
# AWS creds via env or the instance's IAM role (needs s3:PutObject, s3:GetObject on the bucket)

# ── Callbacks + AI (confirm these are set) ────────────────────────
SKYDIVEOS_API_BASE=https://<skydiveos-host>     # where status/links are POSTed back
ANTHROPIC_API_KEY=<key>                # the Compose (AI edit) stage
REDIS_URL=redis://redis:6379/0         # already set by compose
```

Apply:
```bash
cd /…/skydiveos-autoedit
docker compose up -d            # picks up .env changes; recreates api + worker
# or, if you only edited .env:  docker compose restart api worker
```

**SES gotcha to check first:** a brand-new SES account is in **sandbox mode** — it can
only send to *verified* addresses, so real customer emails silently won't go out until
you request **production access** in the SES console. Verify your `DELIVERY_FROM_EMAIL`
(or its domain) either way.

**Verify it's working** (no GoPro needed): run the demo script inside the worker
container against your real SMTP, or locally:
```bash
python scripts/demo_auto_deliver.py --email you@yourdropzone.com \
  --smtp-host $SMTP_HOST --smtp-user $SMTP_USER --smtp-password $SMTP_PASSWORD
```
You should get the email with working download links.

---

## B. SkydiveOS team task list

Full spec: [SKYDIVEOS_INTEGRATION.md](./SKYDIVEOS_INTEGRATION.md). The four tasks:

- [ ] **1. Match footage → booking deterministically.**
  When auto-discovery POSTs `{ s3_key, camera_id, instructor_id }` to
  `/api/media/raw-upload`, resolve it to the specific jump by **`camera_id` + the
  footage capture timestamp** (from GPMF) → the manifested load slot → that customer's
  booking. **Do not** use "most recent landed load/instructor" — with two tandems
  landing close together it will email Customer A's video to Customer B.

- [ ] **2. Create the job** — `POST {AUTOEDIT}/jobs` with the booking data:
  ```json
  { "customer_name": "...", "customer_email": "...", "package": "...", "booking_id": "..." }
  ```
  `customer_email` and `package` are the two that matter. Map the booking's **add-ons →
  `package`** using the table in the contract doc (`selfie` / `external` / `video_only` /
  `photo_only` / `ultimum`). Keep the returned `job_id`.

- [ ] **3. Attach the footage from S3** — `POST {AUTOEDIT}/jobs/{job_id}/upload` with form
  field `s3_key=<the key discovery gave you>`. For the `ultimum` (two-camera) package,
  send one call per camera with `camera_role=instructor|external`. No byte re-upload.

- [ ] **4. Implement the status-callback receiver** — `POST /jobs/{job_id}/status` on the
  SkydiveOS side. Body is `{ "job_id", "status", "delivery_links"? }`; `delivery_links`
  (the presigned customer URLs) arrives only on `status: "delivered"`. Use it to show/
  re-send the video, and as the delivery fallback if a job ever lacked `customer_email`.

Optional / later:
- [ ] If `ENFORCE_INSTRUCTOR_AUTH=1`, forward `X-Instructor-Id` + `X-Role` on requests.
- [ ] Let the customer pick music → `POST /jobs/{id}/music` before processing (else a
  random default track is used).

**Definition of done:** a jump lands, discovery uploads to S3, SkydiveOS creates the job
+ attaches the `s3_key`, and within a few minutes the customer gets an email with their
video links — with no one touching anything. Watch it happen in the status callbacks,
ending in `{"status":"delivered","delivery_links":{…}}`.

---

## Also before real customers
- Replace `templates/music/` with **licensed** tracks (the current ones are copyrighted;
  fine for testing, a takedown risk on delivered videos). See `templates/music/README.md`.
