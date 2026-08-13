# Media Packages — Product & Automation Spec

The definitive spec for the five GoPro media products: what staff must film, what the
customer receives (exact video names, counts, durations, audio treatment, photo
targets), and how the backend automates the whole camera → customer flow. Everything
here is grounded in code — the authority is `api.jobs.Package`, `api/selfie.py`, and
`api/tasks.py`; this document is the human-readable contract.

Reference products (Shred, being replaced): [Selfie](https://parachutemontreal.shredvideo.com/p/slh0ETJZus) ·
[Video only](https://parachutemontreal.shredvideo.com/p/Hol6lqDuxL) ·
[Photos only](https://parachutemontreal.shredvideo.com/p/EwU9oZXeCQ) ·
[External](https://parachutemontreal.shredvideo.com/p/2c3ieW3lhq) ·
[Ultimate](https://parachutemontreal.shredvideo.com/p/Q9JeFMMbUQ) ·
[Spec (speculative capture)](https://parachutemontreal.shredvideo.com/p/pQ6NDzw5t7)

---

## 1. The five packages at a glance

| Package (wire value) | Gallery label | Cameras | Videos | Photos | Composition |
|---|---|---|---|---|---|
| `selfie` | Tandem · Handcam | 1 — instructor handcam | **3** (`full_video`, `highlights`, `freefall`) | ✅ ~50 stills | Claude AI EDL (house-cut fallback) |
| `external` | Tandem · Outside Camera | 1 — camera-flyer | **3** (same names) | ✅ ~50 stills | Deterministic house cut (forced) |
| `video_only` | Tandem · Video | 1 — instructor handcam | **3** (same names) | ❌ none | Claude AI EDL (house-cut fallback) |
| `photo_only` | Tandem · Photos | 1 — instructor handcam | **0** | ✅ ~140 stills | n/a (photo extractor only) |
| `ultimum` | Tandem · Ultimate | **2** — instructor + camera-flyer | **4** (`full_video`, `highlights`, `external_freefall`, `chute_libre_selfie`) | ✅ ~50 stills, both cameras | Per-camera house cuts merged multi-cam |

Every package runs through the same multi-clip scene pipeline
(`api.tasks.process_selfie_package`; `ultimum` through its orchestrator
`api.selfie.run_ultimum_pipeline` which *reuses* the same editing functions on
different footage — it is not a separate editor). The `Package` enum exposes
`uses_scene_pipeline` / `makes_videos` / `makes_photos` / `is_ultimum` so package
behaviour is queried by capability, never by comparing enum members.

**Photos are always extracted from video** — GoPros record video only; the "photos"
deliverable is the best frames mined from the footage (1 fps sampling, face-scored,
de-duplicated). There is no separate stills camera.

---

## 2. Filming spec — what staff must record

The classifier labels every clip with one of eight scene types
(`api.selfie.SCENE_ORDER`): `intro_interview`, `boarding`, `takeoff`, `plane`,
`freefall`, `canopy`, `landing`, `outro_interview`. Staff film to this shot list; the
pipeline auto-detects which clip is which from GPMF telemetry (accelerometer + GPS),
with recording order as the GPS-less fallback anchored on the unmistakable freefall
accelerometer signature.

### Selfie package (instructor handcam)
| Shot | Length | Scene label |
|---|---|---|
| Intro interview | 20–30 s | `intro_interview` |
| Boarding | 15–20 s | `boarding` |
| Takeoff | 15–20 s | `takeoff` |
| Plane (1–2 clips) | 10–15 s each | `plane` |
| Free fall | 60–120 s | `freefall` |
| Canopy | 15–20 s | `canopy` |
| Outro interview | 10–20 s | `outro_interview` |

### External package (camera-flyer)
Same as selfie **minus the canopy shot** (the camera-flyer lands before the tandem):
intro interview, boarding, takeoff, plane, free fall, outro interview.

### Video-only / Photo-only packages
**Filmed identically to the selfie package** — one shot list for staff, always. The
package only changes which deliverables are rendered from that footage. This is what
makes the upgrade path work: the full selfie source material always exists.

### Ultimate (`ultimum`) — two cameras, two shot lists
| Camera (`camera_role`) | Shots |
|---|---|
| `external` (camera-flyer) | Intro interview 20–30 s · Boarding 15–20 s · Takeoff 15–20 s · Plane 1–2 × 10–15 s · Free fall 60–120 s · Outro interview 10–20 s |
| `instructor` (handcam) | Free fall 60–120 s · Canopy 10–20 s |

---

## 3. Deliverables in detail

### 3.1 The three standard videos (`selfie`, `external`, `video_only`)

All rendered at 1080p / h264 / 30 fps into `jobs/<job_id>/`:

| File | Content | Length | Audio |
|---|---|---|---|
| `full_video.mp4` | The whole story in chronological order — intro interview → boarding → takeoff → plane → exit/freefall (with up to 3 slow-mo 0.4× smile peaks) → canopy → landing → outro interview | `Job.target_duration`, default **90 s** | **Cinematic mix**: intro interview keeps original audio → music plays solo from boarding through the jump → at the canopy opening the original audio comes back with the music ducked underneath (so the customer hears themselves under canopy and in the outro) |
| `highlights.mp4` | Short punchy cut: intro beat, the continuous exit sequence, best freefall smiles (slow-mo), deployment, landing, outro | ≤ **40 s** (`_HIGHLIGHTS_TARGET_S`, capped at `target_duration` if lower) | **Music only** |
| `freefall.mp4` | The curated freefall-only cut: clamped hard to `[exit_offset − 8 s, deploy_offset + 3 s]`, mandatory deployment beat, all non-freefall scenes dropped | The freefall itself (~50–80 s of a real tandem) | **Music only** |

**Composition** differs per package:
- `selfie` / `video_only` — **one Claude API call per jump** (`claude-sonnet-4-6`,
  retried exactly once on an invalid reply, never looped) produces all three EDLs from
  the scored timeline. Falls back to the deterministic house cut when no
  `ANTHROPIC_API_KEY` is set, so the pipeline runs end-to-end offline.
- `external` — **house cut forced** (`use_ai=False` in `api/tasks.py`): the
  camera-flyer films from a distance, so MediaPipe scores too few faces for the AI
  editor to sequence reliably; the deterministic cut guarantees a complete, in-order
  edit with every scene contributing proportionally.

Either way, the output is untrusted: `_ensure_story` (milestone/order backstop) then
`edl/validate.py:validate_and_repair` repair every deliverable **before** it is
persisted (see §5.5).

### 3.2 The Ultimate's four videos + photos (`ultimum`)

`api.selfie.run_ultimum_pipeline`. Each camera is classified and scored **once** into
its own scene set (`scenes_instructor/`, `scenes_external/` — never a combined
concat); every deliverable is the existing selfie functions fed different footage.

| File | Cameras | Content | Audio |
|---|---|---|---|
| `full_video.mp4` | **Both** (true multi-cam) | Each camera gets its own house cut, then `_merge_multicam` interleaves them scene by scene so BOTH angles feature for every event — the cameraman's exit/freefall alongside the instructor's | Cinematic mix (music ducks at the canopy opening, keyed off the instructor's deploy offset) |
| `highlights.mp4` | **Both** (multi-cam) | The same combo at highlights length | Music only |
| `external_freefall.mp4` | Camera-flyer only | The selfie `_curated_freefall` cut over the external camera's scenes | Music only |
| `chute_libre_selfie.mp4` | Instructor only | The same freefall cut over the instructor handcam | Music only |
| `photos/` | **Both** | `extract_photos` over both cameras' scene sets, namespaced `<role>_<scene>` so same-named scenes don't collide — coverage spans every scene of both cameras | — |

Multi-cam combo clips carry a `camera` tag (`"external"`/`"instructor"`) that resolves
to that camera's `scenes_<role>/<scene>.mp4` at render. The combo composes
deterministically (same distant-footage rationale as `external`), and the validator
additionally enforces multi-cam pacing: shots ≥ 1.5 s (0.4× slow-mo beats exempt),
≥ 3 s between camera switches, exit-anchored cross-camera alignment.

### 3.3 Photos

| Package | Target | Tuning (why) |
|---|---|---|
| `selfie` / `external` | **~50** (`SELFIE_PHOTO_TARGET`) | Relaxed in-frame floor (0.15) + 1.0 s anti-dupe gap — distant camera-flyer faces read small |
| `photo_only` | **~140** (`PHOTO_ONLY_TARGET`) | Photos are the sole deliverable, so the set is ~3× fuller; 1.0 s gap widens the pool |
| `ultimum` | **~50** across both cameras | Both cameras' scenes merged (namespaced), backfill on |
| `video_only` | none | — |

Extraction samples frames at 1 fps, ranks by the MediaPipe face scores (smile, eye
contact, in-frame), de-duplicates by a minimum gap per scene, and spreads picks across
the whole experience. **Backfill mode** guards the distant-footage case: when face
detection scores ~0 (external cameraman), all frames are re-ranked by pure image
quality so the set still reaches target instead of coming back near-empty. Stills land
in `jobs/<job_id>/photos/` and are zipped for delivery.

### 3.4 Music

Precedence per video deliverable (`Package.music_deliverables` names which videos
accept a track — `()` for `photo_only`):

1. **Per-deliverable upload** — `POST /jobs/{id}/music` before processing, stored at
   `jobs/<id>/music/<deliverable>.<ext>`.
2. **Booking's named track** — resolved against `templates/music/`.
3. **Random default** — `_ensure_default_music` picks one library track at first
   processing and persists it to both `Job.music` and `booking.json`, so replays and
   tweaks re-render with the SAME track (jobs are idempotent; never re-randomized).

A missing track never fails a job — it falls back down this list.

---

## 4. Entitlement — Path A / Path B (the "film it anyway" paywall)

Package (what gets produced) and entitlement (what the customer can access) are
**independent axes** (`api.jobs.Entitlement`). Every jump is filmed and fully edited
whether or not the customer paid:

- **`edited_download` (Path A — media purchased):** the `/j/{code}` gallery streams
  the clean 1080p deliverables with downloads enabled (`⬇ Download video · 1080p MP4`).
- **`preview_only` (Path B — speculative capture, the "spec" product):** the clean
  masters are STILL rendered and uploaded to S3, then a cheap second-pass transcode
  produces watermarked 720p previews (`preview_<name>.mp4`, Pillow watermark PNG
  composited with FFmpeg `overlay` — never `drawtext`). The gallery serves only the
  previews behind an amber `🔒 Unlock full video` CTA; photos hide behind a count
  teaser. The entitlement — never the URL — picks which file is served, and a locked
  job mints **no presigned URLs** at all (a presigned URL carries no entitlement check).

`POST /jobs/{id}/unlock` (service token + admin role + a non-empty
`payment_reference`) flips `entitlement` only — **no re-render, no re-delivery, same
link, instant** — called server-to-server by SkydiveOS from the payment-captured seam,
gated on `paymentScope === 'media-unlock'`. It never touches `status`; the
customer-facing state machine SkydiveOS sees (`media_state`:
`LOCKED_PREVIEW → UNLOCKED`, etc.) is a pure derived projection (`api/lifecycle.py`),
never persisted.

### Upgrade paths ("opportunity to upgrade to a selfie package")

Two distinct mechanisms — don't conflate them:

1. **Paywall unlock** (didn't buy media at all → buys the edit): the Path B flow
   above. Fully automated, one field flip.
2. **Package upgrade** (bought `photo_only`/`video_only` → wants the full selfie
   deliverables): sold through the gallery's **"Add to your day" upsell row**
   (`api/upsell.py`, `$UPSELL_TILES` → SkydiveOS checkout via
   `CHECKOUT_URL_TEMPLATE`'s `{item}` placeholder — rendered on locked AND unlocked
   pages, and as plain text when no checkout URL is configured, never a dead link).
   There is deliberately **no in-module "change package" endpoint**: SkydiveOS owns
   the sale, then re-processes — because every package films the full selfie shot
   list (§2) and the raw masters are retained (`jobs/<id>/raw/`, the jump archive,
   and S3 `raw/…`), rendering the missing deliverables is a cheap re-queue, not a
   re-jump.

---

## 5. Backend automation — camera to customer, in order

The end-to-end flow is fully hands-off when `AUTO_DELIVER=1`: card/camera in →
customer emailed. Every stage below is idempotent and resumable; one job per jump.

### 5.1 Ingest — getting footage off the GoPro

`CAMERA_SCANNER` selects the transport; all four converge on the same pull path
(staging into `raw-storage/_camera-staging/<camera_id>/<date>/`, manifests,
idempotency, retention):

| Mode | How | Notes |
|---|---|---|
| `ble` (default) | BLE scan every `DISCOVERY_INTERVAL_SECONDS` (30 s) for *paired* cameras (allow-list = MongoDB `cameras` registry) → WiFi pull | Wireless, unattended |
| `usb` | mDNS detect → wired pull | The kiosk path, one camera per scan |
| `sdcard` | Card physically inserted; `SdCardScanner` polls `SDCARD_MOUNT_ROOTS` for `DCIM/` volumes | **The primary dropzone flow.** Card identity = GoPro serial from `MISC/version.txt`. Bypasses the registry allow-list (inserting a card is an operator action); per-card status is observable at `GET /ingest/cards` (`detected → pulling → safe_to_remove`) |
| `static` | Simulated cameras staging `DISCOVERY_SAMPLE_MP4` | No-hardware testing, exercises the real pull path |

After the pull, discovery uploads each MP4 to S3 (`raw/{camera_id}/{file}`) and POSTs
a notify to `{SKYDIVEOS_API_BASE}/api/media/raw-upload`:
`{s3_key, camera_id, instructor_id?, camera_role?, captured_at?, staff_id?}`.
`captured_at` is true-UTC derived from the MP4's `creation_time` — GoPros write local
wall-clock mislabelled as UTC, so `CAMERA_CLOCK_TZ` (the dropzone's IANA zone)
corrects it or every match skews by the UTC offset.

**Card retention:** a file is deletable off the card **only once S3 confirmed it**
(the `.transferred.json` ledger is a positive record; unknown/corrupt ledger keeps
footage). Per-file, never `delete_all_media`, opt-in via `DELETE_AFTER_TRANSFER` with
a 24 h grace period.

### 5.2 Matching — whose jump is this?

A clip's `camera_role` is decided **per jump by the load, never by the camera** — the
same staff member (and the same physical GoPro) is the tandem instructor on one load
and the camera-flyer on the next. `ingest.match.FootageMatcher` is the authority:

1. **Who filmed it** — `staffs.goproSerial` → the owning staff member. In sdcard mode
   the instructor instead films their printed **QR session marker**
   (`skydiveos-staff:<staffs._id>`, from `scripts/make_instructor_qr.py`) at the start
   of each session; every later clip until the next marker belongs to that staff
   (marker clips upload to `raw/<camera_id>/markers/` and never become jobs).
2. **Which load** — `captured_at` (true UTC → DZ-local) → the load whose
   `businessDate`/`departureTime` window fits.
3. **Which jumper & role** — the jumper whose `instructor` field is that staff →
   role `instructor`; whose `assignedCameraman` is that staff → role `external`.
4. **Package & entitlement** — from `jumper.mediaPackage` + `videoType`
   (`match.package_for` / `package_and_entitlement_for`); no purchase → the
   role-default package with `preview_only` (spec capture) instead of no job at all.
   Customer name/email from the `customers` doc.

On ambiguity (0 or > 1 candidate jumpers) it **refuses and flags** rather than guess —
mis-matching emails customer A's video to customer B. Pre-flight with
`python scripts/check_match.py --readiness` / `--day <ISO>`.

### 5.3 Job creation & the multi-clip settle

SkydiveOS (or `scripts/skydiveos_bridge.py` standing in for it) turns raw-upload
notifies into jobs:

- `POST /jobs` — booking metadata, package, entitlement, customer + instructor names
  and email. Mints the job's 11-char base62 `gallery_token` once (stable forever).
- `POST /jobs/{id}/upload` — attaches footage: raw bytes, or an `s3_key` form field
  (repeatable, so a caller that knows the whole clip set attaches it in one call) →
  `ingest_s3_job` downloads into `jobs/<id>/raw/` staging. Ultimate clips carry
  `camera_role` and stage under `raw/instructor/` / `raw/external/` (two GoPros emit
  colliding filenames).

**One jump renders ONCE.** SkydiveOS notifies per clip, and a jump is many clips —
so the `s3_key` path stamps `Job.last_raw_clip_at` and arms
`raw_clips_settled_job`, a settle check that re-schedules itself until the job has
been quiet for `RAW_CLIP_SETTLE_SECONDS` (default 180 s). `Job.processing_dispatched`
then makes dispatch **exactly-once** (cleared only when footage is re-attached to a
`failed`/`rejected` job — a genuine retry). Without this, each clip started its own
concurrent render and `AUTO_DELIVER` emailed the customer a partial edit.

**Ultimate gating:** processing dispatches only once **both** role folders are
populated. If the second camera never arrives, `ultimum_watchdog_job`
(`ULTIMUM_SECOND_CAMERA_TIMEOUT_S`, default 1 h) fails the job with an actionable
error instead of leaving it stuck in `queued`.

The bridge debounces a jump's clips into ONE job, dedupes via
`jobs/_bridge_state.json`, and returns 200 on refuse-and-flag (a 5xx would make
discovery retry forever).

### 5.4 The edit pipeline (Celery worker, per job)

1. **Segment** — parse each clip's GPMF accelerometer/GPS; classify into the eight
   scene labels; concat same-scene clips into one `scenes*/<scene>.mp4` recording
   per-source-file `file_offsets` (the plane-entry moment is usually the head of one
   mid-scene file, not the scene's head). Detect `exit_offset` and `deploy_offset`
   inside the freefall scene from the accelerometer signatures. A post-freefall
   "canopy" scene whose mean vertical acceleration exceeds the landing threshold is
   auto-renamed `canopy → landing` and flagged for instructor review. GPS-less clips
   are ordered chronologically by anchoring on the freefall clip's unmistakable
   accelerometer signature. Manual overrides: `scene_labels.json`, `exclude.json`.
2. **Score** — MediaPipe face/expression on the **LRV proxy only, only during
   freefall** (saves ~95% compute): per-second smile / eye-contact / in-frame scores.
3. **Compose** — per §3.1/§3.2 (Claude, house cut, or multi-cam merge). One Claude
   call per jump, max.
4. **Validate** — see §5.5. Repairs are written to
   `jobs/<id>/validation_report.json`.
5. **Render** — FFmpeg executes the EDL against the **full-res MP4s** (analysis never
   touches them): trims, 0.4× slow-mo ramps on the top freefall smile peaks,
   intro/outro brand templates, captions and watermarks drawn with Pillow and
   composited via `overlay` (deployed FFmpeg lacks libfreetype — no `drawtext`).
   Every clip is clamped to its scene file's real duration so video/audio can't
   desync. Path B jobs then render the watermarked 720p previews **inside the same
   task try-block** — a `preview_only` job whose preview render fails, fails (a
   locked gallery with nothing watchable breaks the product).

### 5.5 Deterministic EDL validation (`edl/validate.py:validate_and_repair`)

The compose output — Claude's *or* the house cut's — is untrusted; on real jobs it
dropped the deployment beat, bled landing footage into freefall cuts, skipped
milestones, and ping-ponged multi-cam interleaves. So the milestones are **owned by
code, not the LLM**. The validator is pure (plain dicts in, `(clips, repair-log)`
out, no I/O, never imports `api.*`) and runs at every persist site:

- Freefall-type cuts (`freefall`, `external_freefall`, `chute_libre_selfie`): clamped
  to `[exit_offset − 8, deploy_offset + 3]`, forced deployment beat at
  `deploy_offset`, all non-freefall scenes dropped.
- `full_video` / `highlights`: forced deploy beat + boarding-entry beat (+ intro for
  highlights), dedupe, chronological order.
- Multi-cam combos: ≥ 1.5 s shots (slow-mo exempt), ≥ 3 s between camera switches,
  exit-anchored cross-camera alignment.

Nothing un-repaired is ever persisted; `replay_*` re-renders already-validated EDLs
and does not re-run the validator.

### 5.6 Review → delivery

- **Review gate:** the instructor approves/tweaks in the web UI. The ONLY sanctioned
  bypass is `AUTO_DELIVER=1` (business decision 2026-07): a finished render is
  auto-approved at the `_maybe_auto_deliver` seam and delivery fires immediately.
  Every instructor adjustment is logged (`adjustments.jsonl`) as v2 training signal.
- **Delivery** (`api/delivery.py`): upload every deliverable to
  `s3://$S3_BUCKET/deliveries/{job_id}/` (photos dir zipped first). Path A: presign
  download links (≤ 7 days) and email the customer their gallery link. Path B:
  upload with `presign=False` — **nothing is ever presigned for a locked job** — and
  delivery fails actionably if `PUBLIC_BASE_URL` is unset (the legacy S3 fallback
  would embed presigned clean masters, handing over the unbought edit). Links,
  `entitlement`, `gallery_url`, and `media_state` are persisted on the job and
  forwarded in the SkydiveOS status callback.
- **Gallery:** `GET /j/{code}` is a live route, not a file — the short code is the
  page's only credential (no login; customers have no SkydiveOS account), media
  streams range-enabled from the job dir per request, lock state is computed per
  request (unlock flips the page with no regeneration), and the link never expires.
  Locked and unlocked states share ONE layout; only the player treatment
  (`1080P · FULL QUALITY` vs `720P PREVIEW` + `nodownload`) and the primary action
  (green download vs amber unlock) change.

### 5.7 Archive & retention (after the customer has their media)

- **Jump archive** (`api/archive.py`): every job is mirrored — hardlinked, idempotent,
  never-raises — into the human-browsable
  `raw-storage/{date}/{instructor}/{customer}/{raw,edited,preview,photos}/` +
  `manifest.json` (with per-file sha256), at every footage-landed and render-finished
  seam, so raw footage is filed *before* editing and survives a failed edit. Nothing
  downstream ever reads from it.
- **Disk retention** (`scripts/prune_jobs.py`, cron daily): a local file is deleted
  only when a size-matched HeadObject confirms its S3 copy. Delivered raw masters
  after 2 days, delivered renders after 7 (the gallery then 302s to per-request
  presigned copies). A locked job's watermarked previews and all photos are NEVER
  pruned. Pair with an S3 lifecycle rule (raw → Glacier after 30–60 d).

### 5.8 Security posture (why this is safe to expose)

Every route except the public `/j/*` gallery sits behind the service-token middleware
(`Authorization: Bearer $AUTO_EDIT_API_KEY` — the value SkydiveOS sends).
Instructor identity headers are self-asserted, so the token is the boundary;
`ENFORCE_INSTRUCTOR_AUTH=1` additionally scopes instructors to their own jobs.
`POST /jobs/{id}/unlock` is triple-gated (token + admin + `payment_reference`)
because it gives the product away.

---

## 6. Quick verification

- `python scripts/qa_all_packages.py` — drives **every** package through the live API
  and asserts each stage's outputs (scenes, scores, one EDL per deliverable,
  validation report, renders with A/V sync, photo count in band, delivery links,
  archive mirror). The pre-demo audit.
- `python scripts/demo_full_auto.py --package <p> …` — one jump end-to-end over HTTP
  exactly as SkydiveOS drives it (`--package ultimum` takes `--instructor-cam` /
  `--external-cam`).
- `python scripts/demo_auto_deliver.py --preview-only` — asserts the Path B paywall
  end to end (locked page serves preview bytes → unlock → same URL serves the master).
- `python scripts/diagnose_ultimum.py <job_id>` — per-camera scene classification,
  combo clip selection, A/V desync, photo count for an Ultimate job.
