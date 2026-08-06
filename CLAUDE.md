# SkydiveOS Auto-Edit Module

## About This Project
Automated video editing pipeline for tandem skydiving footage.
Takes raw GoPro footage (typically 5–30 min jumps), produces a 60–120 sec
customer-ready edit with intro, slow-mo highlights, music, and outro.
Output goes to an instructor review screen, then to the customer.

Built as a module inside SkydiveOS. Replaces our current dependency on Shred.

## Tech Stack
- **Language:** Python 3.11+ (pipeline), Node/TypeScript (SkydiveOS web layer)
- **Camera control & file transfer:** Open GoPro SDK (https://github.com/gopro/OpenGoPro)
- **Metadata parsing:** gpmf-parser (https://github.com/gopro/gpmf-parser)
- **Video processing:** FFmpeg (via fluent-ffmpeg in Node, moviepy in Python)
- **Computer vision:** MediaPipe (face/expression), OpenCV (frame ops)
- **AI decisioning:** Claude API (claude-sonnet-4-6) for EDL generation
- **Job queue:** Celery + Redis (Python workers) or BullMQ (Node)
- **Storage:** S3-compatible object store for raw + rendered files
- **GPU workers:** NVIDIA T4 / L4 on cloud, scale-to-zero

## Repo Structure
```
/ingest          — Open GoPro wrappers: BLE pair, WiFi pull, USB pull
/metadata        — GPMF parser, scene segmentation from accelerometer/GPS
/analysis        — MediaPipe face/expression scoring on freefall segment
/edl             — Edit Decision List schema + Claude API calls
/render          — FFmpeg command builder, intro/outro templates, music mixer
/api             — REST endpoints SkydiveOS calls (upload, status, approve)
/review-ui       — React components for instructor review screen
/templates       — Intro/outro PSDs, music tracks, brand overlays
/tests           — pytest for pipeline, jest for API/UI
/scripts         — One-off tools (test with sample jump, replay an EDL, etc.)
```

Two runtime media roots, with different audiences:

```
/jobs                 — the pipeline's WORKING dirs, one per job_id (source of truth)
  <job_id>/           — raw/, scenes_*/, edl.json, job.json, booking.json, renders, photos/
/raw-storage          — the dropzone's browsable JUMP ARCHIVE (api/archive.py)
  <YYYY-MM-DD>/       — date of jump
    <Instructor-Name>/
      <Customer-Name>/
        raw/          — camera masters as ingested (instructor/ + external/ for ultimum)
        edited/       — the rendered deliverables (the clean masters)
        preview/      — the watermarked 720p previews (preview_only jobs)
        photos/       — the selected stills
        manifest.json — job id, booking, package, status, media_state, delivery links,
                        and `files`: a sha256 per archived file (cached by size+mtime)
  _camera-staging/    — per-camera card mirror from a pull: <camera_id>/<date>/ (machine-owned)
```

## Pipeline Stages (in order)
1. **Ingest** — pull MP4 + LRV (proxy) + GPMF from camera via Open GoPro
2. **Segment** — parse GPMF accelerometer/GPS → identify exit, freefall, deploy, landing timestamps
3. **Score** — run MediaPipe on the LRV proxy *only during freefall* (saves 95% compute) to score per-second highlights (smile, eye contact, in-frame)
4. **Compose** — send timeline + scores + customer metadata to Claude API → receive JSON
   EDL → `_ensure_story` (milestone/order backstop) → **deterministic post-validation
   (`edl/validate.py:validate_and_repair`) repairs EVERY deliverable before it's
   persisted** (freefall window `[E−8, D+3]`, mandatory deploy/boarding/intro beats,
   dedupe, chronological order, multi-cam pacing). Repairs are logged at INFO and written
   to `jobs/<id>/validation_report.json`.
5. **Render** — execute EDL against full-res MP4 with FFmpeg: trim, speed ramps, intro/outro, music
6. **Review** — instructor approves or tweaks in web UI (skipped entirely when
   `AUTO_DELIVER=1`: a finished render is auto-approved and delivery fires immediately)
7. **Deliver** — `api/delivery.py`: upload every rendered deliverable to
   `s3://$S3_BUCKET/deliveries/{job_id}/` (a photos dir is zipped first), presign
   download links (`DELIVERY_LINK_TTL_DAYS`, ≤7), email the **customer gallery link**
   to `Job.customer_email` via SMTP, persist the links as `Job.delivery_links`, and
   forward them + `entitlement` + `gallery_url` in the SkydiveOS status callback.
   Missing email/SMTP is tolerated only if `SKYDIVEOS_API_BASE` is set to forward the
   links; otherwise the job fails rather than lying `delivered`. With
   `PUBLIC_BASE_URL` set the customer link is the **served** `/j/{code}` gallery
   (never expires, flips on unlock); unset falls back to the legacy S3 `gallery.html`.

## Key Conventions
- All timestamps in seconds (float), not frames
- Always work on `.lrv` (proxy) for analysis, full `.mp4` only for final render
- EDL is JSON, version-tagged, persisted with every job (lets us replay/A-B test)
- Every instructor adjustment is logged → training signal for v2 model
- One job per jump; jobs are idempotent and resumable
- **Jump archive** (`api/archive.py`): `jobs/<job_id>/` stays the pipeline's opaque
  working dir, and every job is *mirrored* into the human-browsable
  `<ARCHIVE_ROOT>/{jump date}/{instructor}/{customer}/{raw,edited,preview,photos}/` + a
  `manifest.json`. `ARCHIVE_ROOT` defaults to `$RAW_STORAGE_ROOT` (`./raw-storage`).
  Called at every "footage landed" seam (both `POST /jobs/{id}/upload` paths,
  `ingest_s3_job`, `pull_camera_job`) and every "render finished" seam (`process_job`,
  `process_selfie_package`, `rerender_job`), plus `deliver_job` for the links — so raw
  footage is filed *before* editing and survives a failed edit. Three rules:
  files are **hardlinked** (`ARCHIVE_LINK_MODE=link`, falling back to a copy across
  filesystems — a 4K master costs no extra disk); every function is **idempotent** and
  **never raises** (a full disk logs a warning, it must not fail a customer's edit);
  and nothing downstream ever *reads* from the archive. The folder name comes from
  `Job.instructor_name` (SkydiveOS sends it on `POST /jobs`; falls back to
  `instructor_id`, then `_no-instructor`) and `Job.customer_name`. Two different jumps
  that would collide (same day, instructor, and customer name) get a job-id-suffixed
  sibling rather than merging — ownership is recorded in the manifest's `job_id`.
  Backfill or re-file with `python scripts/archive_job.py`. A Path-B job's watermarked
  previews are mirrored into `preview/` (found by `api.preview`'s `preview_<name>.mp4`
  convention, prefix stripped so `preview/full_video.mp4` lines up with
  `edited/full_video.mp4`), and the manifest carries a **sha256 per file**
  (`ARCHIVE_HASHES`, on by default) so an operator can prove a master is the one that
  was ingested — digests are **cached by (size, mtime)** in the manifest, so a 4K file
  is read once no matter how many seams fire, and hashing never runs on the photo set.
- Never call Claude API in a tight loop — one call per jump, max
- A job's **package** (`api.jobs.Package`) selects the pipeline & deliverables. Most
  run through the multi-clip scene pipeline (`api/selfie.py`): `selfie`/`external`
  (3 videos + photos), `video_only` (3 videos), `photo_only` (photos). The two-camera
  **`ultimum`** ("Ultimate") product combines the instructor selfie cam + external
  cameraman into 5 deliverables — `full_video` + `highlights` (a true MULTI-CAM combo:
  each camera gets its own house cut, then `_merge_multicam` interleaves them scene by
  scene so BOTH angles feature for every event), `external_freefall` (cameraman only) +
  `chute_libre_selfie` (instructor only) (the existing selfie `_curated_freefall` per
  camera), and `photos` (`extract_photos` over BOTH cameras' scenes, namespaced) — via
  `api.selfie.run_ultimum_pipeline`. Each camera is classified+scored ONCE into its own
  scene set (`scenes_<role>/`, no combined concat); combo clips carry a `camera` tag that
  resolves to that camera's file at render. It is NOT a new editing pipeline; deliverables
  come from feeding the EXISTING functions different footage, never from forking the editor.
- `selfie` and the camera-flyer `external` package compose videos deterministically
  (house cut, `compose_edls(use_ai=...)`); distant cameraman footage scores too few
  faces for the AI editor to sequence reliably. `_ensure_story` then guarantees every
  deliverable is in chronological jump order with all milestones (entry, exit/jump,
  canopy opening, landing) present, and the renderer clamps every clip to its scene
  file's real duration so the video/audio streams can't desync (frozen frame, audio
  continues). Photo selection has a `backfill` mode: when faces aren't detected (distant
  footage scores 0), it ranks all frames by image quality so the set still reaches ~50.
- **Deterministic EDL validation** (`edl/validate.py:validate_and_repair`): the compose
  output (Claude *or* the house cut) is UNTRUSTED — on real jobs it dropped the
  deployment beat, bled landing footage into freefall cuts, skipped the plane-entry and
  the highlights intro, and produced duplicate / ping-ponging multi-cam interleaves. So
  the milestones are owned by code, not the LLM: `validate_and_repair(edl, deliverable,
  manifest, *, manifest_by_camera=None)` runs at every persist site (`compose_edls` for
  single-cam; the combo and per-camera freefall sites in `run_ultimum_pipeline`) and
  repairs each deliverable *before* it's written. It is **pure** — plain-dict clips in,
  `(clips, repair-log)` out, no I/O, and it MUST NOT import `api.*` (`api.selfie` imports
  it — the reverse is circular). Freefall-type cuts (`freefall`, `external_freefall`,
  `chute_libre_selfie`) are clamped to `[exit_offset−8, deploy_offset+3]`, get a forced
  deployment beat at `deploy_offset`, and drop ALL non-freefall scenes; `full_video` /
  `highlights` get the deploy beat + boarding-entry (+ intro for highlights); multi-cam
  combos additionally enforce ≥1.5 s shots (0.4× beats exempt), ≥3 s between camera
  switches, and exit-anchored cross-camera alignment. `replay_*` re-renders EDLs that
  were already validated at first compose, so it doesn't re-run the validator.
- **Scene manifests** now record per-source-file `file_offsets`
  (`[{"file","offset"}, …]`, cumulative seconds within the concatenated scene): the
  plane-entry moment is usually the head of ONE mid-scene file, not the combined scene's
  head, so compose and the boarding rule target files by offset. A post-freefall scene
  the telemetry classifier called `canopy` whose `gpmf_signals.accl_z_mean` exceeds
  `_LANDING_ACCL_Z_MEAN` (1.5 — signed; observed ~2.08/3.52 on real landing footage vs
  ~1.0–1.2 under a flying canopy, and freefall's large negative never qualifies) is
  really the touchdown and is renamed `canopy`→`landing`, added to the manifest's
  `flagged` list (reason `auto-renamed canopy->landing (accl signature)`) so it surfaces
  for instructor review. `landing` is the outro-side milestone; canopy references in the
  editor stay tied to the freefall scene's `deploy_offset`.
- Ultimate uploads carry a `camera_role` (`instructor`/`external`); clips stage under
  `raw/<role>/` (two GoPros emit colliding filenames). Processing auto-enqueues only
  once both role folders are populated. Music/original-audio rules: full video keeps
  original audio (music ducked at canopy); the other three are music-only.
- **A clip's `camera_role` is decided PER JUMP by the load, never by the camera.** At a
  real dropzone one staff member is the tandem `instructor` on one jump and the
  `assignedCameraman` on the next — on the *same* physical GoPro. So the role is a
  property of the (staff, jump) slot, not the camera: the camera registry's static
  `role` (`ingest.registry`) is only a *hint*, and `ingest.match.FootageMatcher` is the
  authority. It resolves a clip to its jump by `staffs.goproSerial` → the owning staff
  (the reliable bridge — the SkydiveOS `staffs._id` differs from the registry's owner
  id, so we NEVER match on the registry's `instructor_id`), then `captured_at` (true-UTC
  → DZ-local via `CAMERA_CLOCK_TZ`) → the `loads` whose `businessDate`/`departureTime`
  window fits, then the jumper whose `instructor` (→ role `instructor`) or
  `assignedCameraman` (→ role `external`) is that staff. Package comes from
  `jumper.mediaPackage` + `videoType` (`match.package_for`); customer email/name from the
  `customers` doc. This is the *SkydiveOS-side* match mirrored here (SkydiveOS owns it in
  prod, see `SKYDIVEOS_INTEGRATION.md`) so the camera→customer flow runs end-to-end
  against the shared DB without the SkydiveOS backend. It **refuses and flags** on
  ambiguity (0 or >1 jumpers) rather than guessing — mis-matching emails customer A's
  video to customer B. The decision logic (`select_match`, `package_for`) is **pure** and
  must not import `api.*` (dependency-light like `edl.validate`); only the DB lookup
  touches Mongo (lazy `pymongo`, disabled when `MONGO_URL` unset). **Discovery is wired
  to it**: `api.app` builds a `FootageMatcher` when `MONGO_URL` is set and passes
  `ingest.discovery.matcher_role_resolver(...)` as the service's `role_resolver`; at each
  hand-off `_materialize` uses the load-derived role over the registry's static hint,
  falling back to the hint (never blocking) when the capture time is unreadable or the
  match is unknown/ambiguous.
- **Per-deliverable music** can be uploaded per job *before* processing
  (`POST /jobs/{id}/upload` for footage, `POST /jobs/{id}/music` for tracks), stored at
  `jobs/<id>/music/<deliverable>.<ext>` (deliverable ∈ `Package.music_deliverables`).
  The renderer (`api.selfie._music_paths`/`_ultimum_music_paths`) prefers the uploaded
  track, else falls back to the booking's `music` name → `templates/music`. Never fail
  a job for missing music — it just falls back.
- **Default random music**: when a booking names NO music (and no per-deliverable/
  uploaded track), `api.selfie._ensure_default_music` picks one `templates/music`
  track at random so the customer still gets a scored soundtrack, not silence. The
  pick happens **once, at first processing**, and is persisted to BOTH `Job.music`
  and `booking.json` — so a replay/tweak (which re-reads `booking.json`) re-renders
  with the SAME track (jobs are idempotent; EDLs replayable — don't re-randomize on
  replay; the process paths call it, the `replay_*` paths must not). No-op when music
  is already named or the library is empty. Drop 2–4 licensed tracks in
  `templates/music/` for real variety (see its README) — with one track it always
  picks that one.
- **Entitlement = the "film it anyway" paywall** (`api.jobs.Entitlement`, business
  decision 2026-07 / Media Module design doc REV 03). Every jump is filmed whether or
  not the customer paid; `Job.entitlement` decides what their gallery shows:
  `edited_download` (Path A — media purchased: the clean 1080p deliverables, downloads
  enabled) or `preview_only` (Path B — speculative capture: watermarked 720p previews
  behind an unlock CTA, photos hidden behind a count teaser). SkydiveOS sends it on
  `POST /jobs`; `ingest.match.package_and_entitlement_for` derives it for our own
  matcher (no purchase → the role-default package + `preview_only`, instead of the old
  "no job at all"). Four rules:
  * **The clean masters are ALWAYS rendered and delivered to S3**, both paths. Path B
    then adds a cheap second-pass transcode per video (`api/preview.py` →
    `<job_dir>/preview_<name>.mp4`, 720p + a Pillow watermark PNG composited with
    `overlay` — see `render/watermark.py`). So `POST /jobs/{id}/unlock` is instant:
    no re-render, no re-delivery, same link. Previews are found by the `preview_`
    filename convention and are deliberately **NOT** in `Job.outputs` (they'd leak
    into the S3 delivery set and the duplicated deliverable-name maps).
  * **A `preview_only` job whose preview render fails, fails** (`_render_previews`
    runs inside each task's `try`) — a locked gallery with nothing watchable breaks
    the product, and the raw footage is archived so a re-queue is cheap. An
    `edited_download` job returns early and gains no new failure mode.
  * **The entitlement — never the URL — picks the file** at `GET /j/{code}/media/{name}`:
    while locked the clean master is unreachable at any URL. `locked` is computed **per
    request**, so unlock flips the page with no regeneration.
  * **`POST /jobs/{id}/unlock` is idempotent and never touches `status`** — it sets
    `entitlement`+`paid_at`+`payment_reference` only, staying clear of the
    review/delivery machine (notably the `ready` vs `ready_for_review` approve quirk).
    It gives the product away, so it is the one endpoint with three gates: the
    **service token**, the **admin** role, and a non-empty **`payment_reference`**
    (SkydiveOS's captured-transaction id, persisted so every unlock is auditable).
    SkydiveOS calls it server-to-server from the payment-captured seam
    (`paymentEventHandler` → `autoEditOrchestrationService.unlockPaidMedia`), gated on
    `paymentScope === 'media-unlock'` so paying for the *jump* never unlocks media.
  * **A locked job is delivered as the `/j/{code}` gallery or not at all**
    (`api/delivery.py`). A presigned URL answers to whoever holds it — there is no
    entitlement check on a URL — so a `preview_only` job mints **none**: the masters
    and photo zip still upload to S3 (durable; what `/unlock` serves instantly) with
    `presign=False`, and delivery **fails with an actionable error** if
    `PUBLIC_BASE_URL` is unset rather than falling back to the legacy S3 gallery,
    which embeds presigned *clean masters* and would hand over the unbought edit
    (it also persists them on the job, mirrors them into the archive manifest, and
    forwards them to SkydiveOS).
- **The customer gallery is a LIVE route, not a file** (`GET /j/{code}` in `api.app`,
  HTML from the pure `api.gallery.render_gallery_html`). `Job.gallery_token` is an
  11-char base62 short code minted once at `POST /jobs` (stable across replays, like
  the persisted music pick) so the link is SMS-short and never changes:
  `{PUBLIC_BASE_URL}/j/{code}?s=e#tab-video` — `?s=` is an opaque source tag the
  server accepts and **ignores** (never auth, never lock state), `#tab-video` picks
  the Video/Photos tab. The code is the page's **only** credential: `/j/` carries no
  `{job_id}` so `enforce_job_ownership` is a no-op, and `api.auth.PUBLIC_PATH_PREFIX`
  resolves these requests to an anonymous owns-nothing principal even when
  `ENFORCE_INSTRUCTOR_AUTH=1` (the customer has no SkydiveOS account). Never log a
  token. Media streams from the job dir per request (`FileResponse`, range-enabled),
  so nothing expires — S3 stays the durable copy. With `PUBLIC_BASE_URL` unset,
  delivery falls back to the legacy presigned S3 `gallery.html` unchanged.
- **The landing page's two states share ONE layout** (design doc Frame 03): the hero
  (`eyebrow · customer · "14 AUG 2026 · Tandem · Handcam · Instructor Marc Tremblay"`),
  the players, and the upsell row are identical; only the **player treatment** (`1080P ·
  FULL QUALITY` vs `720P PREVIEW` + `nodownload`) and the **primary action** (green
  `⬇ Download video` + "1080p MP4 · 214 MB · yours to keep" vs amber `🔒 Unlock full
  video — $39`) change — so the paid path never feels like a different product. The
  accent colour is the state (`#5bbd84` unlocked / `#e2a13f` locked) on the `#0c1218`
  base. The **"Add to your day" upsell row is entitlement-independent** (`api/upsell.py`,
  `$UPSELL_TILES` → `key:title:blurb:price|…`, linked through
  `CHECKOUT_URL_TEMPLATE`'s extra `{item}` placeholder): it's the operator's second
  revenue line whether or not the video was pre-purchased, so it renders on both pages
  and on the legacy S3 fallback. A malformed tile is dropped, and a tile with no
  checkout URL renders as **text** — same rule as the unlock CTA, never a dead link.
- **`media_state` is a DERIVED view, not a status** (`api/lifecycle.py`). The design
  doc's Frame 02 machine (`PENDING_CAPTURE → … → READY →` `DELIVERED` |
  `LOCKED_PREVIEW → UNLOCKED`, `FAILED`) is offered to SkydiveOS as a *projection* of
  `(status, entitlement, paid_at)` — exposed read-only on `JobResponse`, the status
  callback, and the archive manifest — because the two axes move independently:
  `POST /jobs/{id}/unlock` must flip the paywall **without** touching `status`. It is
  pure, never persisted (so it can't drift), and nothing in the pipeline branches on
  it: drive UI copy off `media_state`, drive the pipeline off `JobStatus`.

## Bash Commands
- `pip install -r requirements.txt` — install Python deps
- `python -m ingest.pull --camera <id>` — pull a camera's jumps into raw-storage and enqueue them
- `python -m ingest.pull --camera <id> --pair [--name "<label>"]` — one-time BLE pairing for a camera; also records it in the MongoDB camera registry so auto-discovery will recognise it
- `uv pip install ./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control` — install the hardware-only Open GoPro SDK (needed only for live ingest)
- `python -m analysis <proxy.lrv> --start <s> --end <s>` — score a proxy's freefall window (per-second smile/eye-contact/framing JSON); model auto-downloads, override with `$FACE_LANDMARKER_MODEL`
- `pytest tests/ -v` — run all pipeline tests
- `python scripts/process_jump.py <path/to/raw.mp4>` — end-to-end on a sample file (timeline → house-cut EDL → render `jobs/{id}/final.mp4`)
- `python -m render <source.mp4> --job-id <id> --customer "<name>"` — render an EDL (the job's saved `edl.json`, or `--edl <path>`) to `jobs/{id}/final.mp4` at 1080p/h264/30fps; intro/outro from `/templates`, music via `--music <name>`, caption font override with `$RENDER_FONT`
- `python scripts/replay_edl.py <job_id>` — re-render from a saved EDL
- `python scripts/archive_job.py --all [--dry-run]` / `<job_id>...` — (re)file jobs into the
  browsable jump archive `raw-storage/{date}/{instructor}/{customer}/`. The pipeline does this
  automatically; this is the catch-up tool for jobs that predate the archive, jobs whose archive
  pass failed (full disk / unmounted NAS — archiving logs and moves on rather than failing the
  edit), or re-filing after a name correction in `job.json`. Idempotent, hardlinked, deletes nothing
- `python scripts/demo_full_auto.py --package <p> --customer "<name>" --instructor "<name>"
  --email <you> <raw.MP4>...` — drive ONE jump through the **live** stack over HTTP exactly
  as SkydiveOS does (`POST /jobs` → `POST /jobs/{id}/upload` → poll), then print the
  deliverables, the customer gallery link, and a tree of the jump's archive folder. The
  rehearsal/demo driver: `--package ultimum` takes `--instructor-cam`/`--external-cam`,
  `--api http://<host>:8000` targets a remote deployment, `--no-wait` skips polling. Exits
  non-zero on a failed job, so it doubles as a pre-demo smoke test
- `python scripts/demo_auto_deliver.py` — no-camera end-to-end check of the full
  automatic flow: edits a sample MP4, auto-approves it (`AUTO_DELIVER`), uploads the
  render to S3, presigns links, and emails them — catching the mail in a throwaway
  local SMTP sink (`aiosmtpd`) so you see the exact customer email with no real mail
  server. `--email/--smtp-host/...` to send for real, `--no-email` for links only,
  `--source` for your own footage, `--keep` to retain the S3 uploads (default: deleted).
  `--preview-only` runs the **Path B** flow instead and asserts the paywall end to end:
  watermarked previews rendered → locked `/j/{code}` page serves the *preview* bytes →
  `POST /unlock` → same URL now serves the clean master. Exits non-zero if the lock
  ever leaks the master or the unlock doesn't flip
- `python scripts/qa_all_packages.py [--packages <p,…>] [--email <you>] [--no-email]` —
  the pre-demo audit: drives **every** package through the live API (no GoPro — it
  reuses the real masters already in `jobs/*/raw/`) and then opens each job's working
  dir to assert what every stage produced: raw staged, `scene_manifest*.json` scenes +
  `exit`/`deploy` offsets + `file_offsets`, `scores*.json` rows, one `edl_*.json` per
  deliverable, `validation_report.json`, every render present with **video/audio stream
  durations within 1 s** (the freeze/desync check), photo count in band, auto-approve →
  `delivery_links` that answer HTTP 200, the jump-archive mirror, and the
  `/deliverables` + `/photos` endpoints. Prints a stage matrix, writes
  `qa-report.{json,md}`, exits non-zero if any stage failed. Runbook:
  [`QA_NO_GOPRO.md`](QA_NO_GOPRO.md)
- `python scripts/prune_jobs.py [--dry-run]` — the EC2-side disk-retention sweep (cron
  it daily in production). Card-retention philosophy applied to the server: a local
  file is deleted **only when a size-matched HeadObject confirms its S3 copy**, per
  file, never failing anything. Tiers, each age-gated from the job's last update:
  delivered jobs' `raw/` masters (`--raw-days`, default 2 — S3 `raw/{camera}/{name}`
  is the authority); delivered jobs' renders (`--renders-days`, default 7 —
  `deliveries/{job_id}/` is the authority, and `GET /j/{code}/media/{name}` then
  302-redirects to a per-request presigned copy so the never-expiring gallery link
  keeps working); date-named jump-archive and `_camera-staging` day folders
  (`--archive-days`/`--staging-days`, default 7 — the archive's long-term home is the
  dropzone Mac via `sync-archive.sh`). A still-locked (`preview_only`) job's
  watermarked previews are NEVER pruned (they are the paywall product and exist only
  locally — and the gallery's S3 fallback deliberately refuses to redirect a locked
  job, since a presigned master URL is the paywall bypass); photos are never pruned
  (the grid serves stills locally). Pair with an S3 lifecycle rule (`raw/…` → Glacier
  after 30–60 d) and a disk alarm
- `python scripts/restamp_footage.py --at <local-time> --out-dir <dir> <masters…>` —
  write re-stamped COPIES of GoPro masters so an old card can be demoed against a load
  manifested for today (prefer manifesting the load for the footage's real date — then
  nothing is altered). Avoids the three traps that silently break this: a plain
  `ffmpeg -c copy` **drops the `gpmd` telemetry** (segmentation then finds no exit /
  deploy), `creation_time` without a trailing `Z` is **timezone-shifted** from
  host-local, and stamping every clip identically breaks the per-file match — relative
  spacing is preserved. `--at` is dropzone-local wall clock, exactly what a GoPro
  writes. Output can only be re-uploaded, never put back on a camera
- `python scripts/check_match.py --readiness | --day <ISO> | --serial <s> --at <local-time>`
  — read-only: shows what the footage→customer matcher WOULD decide against the live
  shared DB, with no camera. `--readiness` reports the two data prerequisites that
  actually break this (staff without `goproSerial`, unset `CAMERA_CLOCK_TZ`, registry
  cameras owned by nobody); `--day` replays every load that day as one simulated clip
  per camera and prints `deliverable / no-media / FAILED`, exiting non-zero on any
  FAILED — a morning pre-flight before jumping
- `python scripts/demo_from_load.py --dir <folder-of-MP4s> [--serial <s>] [--api <url>]`
  — drive the FULL edit→deliver flow from a camera's footage matched to the load it
  belongs to, bypassing discovery/BLE/WiFi. Reads the earliest clip's capture time,
  resolves it via `ingest.match.FootageMatcher` (serial → today's load → jumper →
  customer + package + role), prints the match, then drives the live API exactly as
  `demo_full_auto` does — so the gallery is delivered to *the customer on the load*, not
  a name you typed. The workaround when the unattended pull is blocked (macOS Wi-Fi, a
  headless service without Location Services, or SkydiveOS not yet turning the raw-upload
  into a job). Needs `MONGO_URL` + the auto-edit API at `--api` with `AUTO_DELIVER=1`
- `python scripts/skydiveos_bridge.py [--port 9000] [--api <auto-edit>] [--debounce 20]`
  — local stand-in for the SkydiveOS raw-upload consumer (and the executable reference
  for its implementation): receives discovery's notify, matches via `FootageMatcher`
  (`staff_id` from the QR, else camera serial), debounces a jump's clips into ONE job,
  creates it over the live API and attaches the footage from S3. Dedupe/flag state in
  `jobs/_bridge_state.json`; refuse-and-flag returns 200 (a 5xx would make discovery
  retry forever). `bash scripts/run_sdcard_stack.sh` starts worker + bridge + API with
  `CAMERA_SCANNER=sdcard AUTO_DELIVER=1` — the one-command "insert card → customer
  emailed" stack for a single machine
- `python scripts/make_instructor_qr.py --staff-id <staffs._id> --name "<name>"` (or
  `--all` from `MONGO_URL`) — print-ready QR PNGs instructors film to claim an SD-card
  session (payload `skydiveos-staff:<_id>`, H-level correction, captioned via Pillow)
- `python scripts/check_sdcard.py [--decode] [--root <dir>]` — read-only probe of the
  SD-card flow: which cards the scanner sees, each card's derived identity (serial vs
  label fallback), and with `--decode` the QR markers + per-clip session attribution
  (+ `resolve_for_staff` against `MONGO_URL`). Exit 0/1, like `check_match.py`
- `python scripts/diagnose_ultimum.py <job_id>` — read-only diagnostic for an Ultimate job: per-camera scene classification, combo clip selection by `(camera, scene)`, video-vs-audio stream-duration sync on scene files + rendered outputs (catches the "video freezes, audio continues" desync), per-camera freefall cuts, and photo count — with findings flagging a camera collapsed to one scene, the cameraman absent from a scene, or any desync
- `ffmpeg -version` — must be 6.0+ for our speed-ramp filter
- `uvicorn api.app:app --reload` — serve the /api FastAPI service (OpenAPI docs at `/docs`); SkydiveOS calls it to create jobs, upload footage, review, approve, and stream previews
- `celery -A api.celery_app.celery_app worker -l info` — run the worker that executes the async pipeline tasks /api enqueues (set `CELERY_TASK_ALWAYS_EAGER=1` to run tasks inline without a worker, for a single-process demo). **Never leave eager on when a worker is running**: the upload endpoint then runs the whole segment→score→render inline in the API process, so for the ~15 min of an edit `GET /jobs`, `/docs`, the review UI and the discovery loop all block (verified: `GET /jobs` times out, discovery stops scanning). Eager also skips `ultimum_watchdog_job`
- Camera auto-discovery (`api.app` lifespan → `ingest.discovery.CameraDiscoveryService`): when `ENABLE_AUTO_DISCOVERY=1`, the API BLE-scans every `DISCOVERY_INTERVAL_SECONDS` (default 30) for *paired* cameras (the allow-list in the MongoDB `cameras` collection), runs the existing `pull_camera` for each unseen one, **uploads each pulled MP4 to S3** (`S3_BUCKET`, key `raw/{camera_id}/{file}`) then **POSTs JSON** `{s3_key, camera_id, instructor_id?, camera_role?, captured_at?}` to `{SKYDIVEOS_API_BASE}/api/media/raw-upload` (`captured_at` = a TRUE-UTC ISO-8601 instant from the MP4's `creation_time` via ffprobe, best-effort, so SkydiveOS can match footage→booking by camera + capture time; `camera_role` routes the two Ultimate angles). GoPro writes the camera's LOCAL wall-clock into `creation_time` mislabelled as UTC, so `CAMERA_CLOCK_TZ` (the dropzone's IANA zone, e.g. `America/Toronto`) is used to convert it to real UTC — set it or the match skews by the UTC offset (`ingest.discovery._to_true_utc`). SkydiveOS creates the media/job from the key (`POST /jobs` for the booking metadata, then `POST /jobs/{id}/upload` with an `s3_key` form field → `api.tasks.ingest_s3_job` downloads that S3 object into the job's `raw/` staging — per-`camera_role` for `ultimum` — and hands off to the same pipeline dispatch a byte upload uses, so big files never stream through the web layer). An `ultimum` ingest that has only one camera so far arms `api.tasks.ultimum_watchdog_job` (countdown `ULTIMUM_SECOND_CAMERA_TIMEOUT_S`, default 1h; skipped in eager mode): if the second camera never arrives the job is failed with an actionable error instead of hanging in `queued` — the guard against a missing second camera or a booking mis-mapped to the two-camera package. Discovery does **not** create jobs itself (needs `SKYDIVEOS_API_BASE` + `S3_BUCKET` set). Off by default — pulls stay operator/SkydiveOS-triggered until opted in. Manage the registry via `GET /cameras`, `DELETE /cameras/{id}` (soft-deactivate, admin), `POST /cameras/{id}/assign` (register/assign owning instructor, admin). The BLE scan needs the hardware-only `bleak`/Open GoPro SDK; the registry needs `pymongo[srv]` + `MONGO_URL`.
- **A multi-clip jump renders ONCE** (`api.tasks.raw_clips_settled_job`). SkydiveOS
  notifies once *per clip*, so one jump filmed as several files (a GoPro chapters a 4 GB
  master; an instructor stops/starts recording) arrives as several
  `POST /jobs/{id}/upload` calls. Dispatching per call started a render per clip:
  concurrent renders sharing a job dir, each cutting whatever subset had landed, and with
  `AUTO_DELIVER` the first to finish emailed the customer a **partial edit**. So the
  `s3_key` path stamps `Job.last_raw_clip_at` and arms a settle check that re-schedules
  itself (`RAW_CLIP_SETTLE_POLL_SECONDS`) until the job has been quiet for
  `RAW_CLIP_SETTLE_SECONDS` (default 180 — it must exceed the gap between a jump's
  notifications, which is really the previous clip's S3 upload time; `0` restores
  dispatch-immediately). Re-scheduling rather than cancelling is the same trick
  `ultimum_watchdog_job` uses. `Job.processing_dispatched` then makes dispatch
  **exactly-once** — every dispatch goes through `_dispatch_processing` (including the
  ultimum both-roles-present branch, where a re-notified clip would otherwise re-render)
  — and is cleared only when footage is re-attached to a `failed`/`rejected` job, which
  is a genuine retry. `POST /jobs/{id}/upload` also accepts **repeated `s3_key` fields**
  so a caller that already knows the whole clip set attaches it in one call.
- **Card retention** (`ingest/retention.py`): a dropzone card holds ~30 Ultimate jumps, so
  at 4–5 jumps/day it fills within a week — and a full card *silently stops recording*
  mid-day. So a pull can clear the card, under one rule: **a file is deletable only once
  S3 has confirmed it**, never merely because it reached the ingest host's disk (that disk
  can fail, or `raw-storage` can be wiped). The S3 upload happens in `ingest.discovery`
  *after* `pull_camera` closed the camera, so deletion can't be inline: `discovery`
  calls `record_uploaded()` when S3 accepts a file, and the **next** connect runs
  `pull._sweep_card` → `retention.deletable()` → `Camera.delete_media()` per file. The
  ledger (`<root>/_camera-staging/<camera_id>/.transferred.json`) is a *positive* record,
  so an unknown or corrupt ledger keeps footage. Deletion is per-file (never
  `delete_all_media`), logged with the S3 key that authorised it, and never raises — a
  card that can't be cleaned is a capacity warning, not a reason to abandon a pull.
  Controlled by `DELETE_AFTER_TRANSFER` (off by default), `DELETE_AFTER_TRANSFER_MIN_AGE_H`
  (24 h grace), `DELETE_AFTER_TRANSFER_DRY_RUN`. `UploadFn` returns the S3 key (or `None`
  to keep the file) — that return value *is* the delete authorisation
- `CAMERA_SCANNER` selects the discovery transport: `ble` (default — BLE scan + WiFi pull, wireless), `usb` (mDNS detect + `ingest.camera.WiredGoProCamera` pull — the kiosk path, one camera per scan), `sdcard` (physically inserted card — see below), or `static` (no-hardware simulation: `StaticCameraScanner` + `ingest.camera.LocalSampleCamera` stage `DISCOVERY_SAMPLE_MP4` through the *real* pull path; needs `DISCOVERY_FAKE_CAMERAS`). USB and WiFi share one HTTP download path (`_SdkGoProCamera`); both need the hardware-only Open GoPro SDK.
- **SD-card ingest with QR session markers** (`CAMERA_SCANNER=sdcard`,
  `ingest/sdcard.py` + `ingest/qr.py`): the card comes out of the GoPro and into the
  ingest machine's reader; `SdCardScanner` polls `SDCARD_MOUNT_ROOTS` for volumes with
  `DCIM/` and `SdCardCamera` runs the *real* pull path over the mount (staging,
  manifests, idempotency, retention sweep all unchanged — `DELETE_AFTER_TRANSFER`
  frees inserted cards too). Card identity = the camera serial in `MISC/version.txt`
  (last 4 digits, same id a wireless pull would use → shared staging tree + ledger),
  else an `sd-<label>` fallback. **Who the footage belongs to comes from the filmed QR
  session marker**: the instructor records a short clip of their printed QR
  (`scripts/make_instructor_qr.py`, payload `skydiveos-staff:<staffs._id>` — the
  SkydiveOS staff id, NOT the registry's `instructor_id`) at the start of each session;
  every later clip (until the next marker, by capture order) belongs to that staff.
  `ingest.qr.qr_identity_resolver` is discovery's per-clip `identity_resolver`: decode
  results cache in `<stem>.qr.json` sidecars (clips > `SDCARD_QR_MAX_CLIP_SECONDS`
  are never probed), the raw-upload payload gains `staff_id` + `staff_source: "qr"`
  (SkydiveOS matches by staff + `captured_at`, skipping the `goproSerial` lookup), and
  locally `FootageMatcher.resolve_for_staff(staff_id, captured_at)` — the extracted
  staff-keyed half of `resolve` — picks the load/role/customer. The marker clip never
  becomes a job: it uploads to `raw/<camera_id>/markers/` (so the "deletable only once
  S3 confirmed" rule holds) and is never notified. sdcard mode bypasses the registry
  allow-list (an inserted card is an operator action; the QR + load match is the real
  gate) and a clip with no preceding marker falls back to the serial-based match with
  a WARNING. Probe cards read-only with `python scripts/check_sdcard.py [--decode]`.
- `python scripts/check_camera.py --usb` / `--wifi --camera <id>` — hardware smoke test: open a real GoPro and list its media (read-only), using the same Camera classes the pull uses. Verifies the SDK + connectivity before enabling discovery.
- **The service-token gate is what makes this API safe to expose** (`api.auth`
  `service_token_allows`, enforced as a middleware in `create_app`). Every route
  except `/j/*` needs `Authorization: Bearer $AUTO_EDIT_API_KEY` — the value
  SkydiveOS already sends. It's a **middleware, not a route dependency**, because
  FastAPI serves `/docs` / `/openapi.json` as raw Starlette routes that skip app
  dependencies. Off until the env var is set (same opt-in pattern as
  `ENFORCE_INSTRUCTOR_AUTH`); `OPTIONS` is exempt so CORS preflight still works.
  Why it exists: identity here is **self-asserted** (`X-Instructor-Id`/`X-Role`), so
  with enforcement off every caller is an admin — and the service is internet-facing
  (the SkydiveOS frontend was built to call it from the browser; that dependency has
  since been proxied through the SkydiveOS backend). Verified 2026-08-03 on prod:
  anonymous `GET /jobs` returned every customer's name, email and delivery links, and
  a range request on `/jobs/{id}/deliverables/{name}` streamed their finished video.
  Network rules are the other half — see `deploy/PROXY_LOCKDOWN.md`
- Instructor ownership / access scoping (`api.auth`): each camera carries an `instructor_id` (set at `--pair --instructor-id` or via `POST /cameras/{id}/assign`); auto-discovery sends it with the raw upload (and locally-created jobs carry `Job.instructor_id`), so footage lands in that instructor's SkydiveOS account. SkydiveOS forwards identity as `X-Instructor-Id` + `X-Role` (`instructor`/`admin`); when `ENFORCE_INSTRUCTOR_AUTH=1` an instructor sees only their own jobs/cameras (`GET /jobs`, `GET /cameras`) and admins see all + manage the registry. Off by default (every caller is admin), so the open flow is unchanged; ownership *tagging* always happens regardless.
- `npm run dev` — local SkydiveOS API + review UI
- `npm test` — Jest tests for API/UI

## Code Style
- Python: PEP 8, type hints required on all public functions, ruff for linting
- TypeScript: strict mode on, no `any`, prefer functional components
- Commit messages: conventional commits (feat:, fix:, chore:, etc.)
- One feature per branch, PR to `main`, squash merge

## Environment
- Python env via `uv` (faster than pip; see `pyproject.toml`)
- `.env.example` documents required vars: `ANTHROPIC_API_KEY`, `S3_BUCKET`,
  `REDIS_URL`, `SKYDIVEOS_API_BASE`; auto-discovery adds `ENABLE_AUTO_DISCOVERY`,
  `DISCOVERY_INTERVAL_SECONDS`, `CAMERA_SCANNER`, `DISCOVERY_FAKE_CAMERAS`,
  `DISCOVERY_SAMPLE_MP4`, `MONGO_URL`, `MONGO_DB`, `ENFORCE_INSTRUCTOR_AUTH`;
  automatic delivery adds `AUTO_DELIVER`, `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/
  `SMTP_PASSWORD`/`SMTP_STARTTLS`, `DELIVERY_FROM_EMAIL`, `DELIVERY_LINK_TTL_DAYS`;
  SD-card ingest adds `SDCARD_MOUNT_ROOTS` (colon-separated),
  `SDCARD_QR_MAX_CLIP_SECONDS`, `SDCARD_QR_SCAN_SECONDS`,
  `RAW_CLIP_SETTLE_SECONDS`/`RAW_CLIP_SETTLE_POLL_SECONDS` (the multi-clip settle window);
  the jump archive adds `ARCHIVE_ENABLED` (on by default), `ARCHIVE_ROOT` (defaults to
  `$RAW_STORAGE_ROOT`), `ARCHIVE_LINK_MODE` (`link` | `copy` | `symlink`),
  `ARCHIVE_HASHES` (on by default); the entitlement/paywall adds `PUBLIC_BASE_URL` (the
  customer-facing origin, e.g. `https://freefall.ing` — unset keeps the legacy S3
  gallery), `PREVIEW_PRICE_DISPLAY` (CTA text only), `CHECKOUT_URL_TEMPLATE`
  (SkydiveOS's checkout page, with `{job_id}`/`{booking_id}`/`{item}`; unset → the CTA
  and the upsell tiles are text, never dead links), `UPSELL_TILES` (the landing page's
  "Add to your day" row, `key:title:blurb:price|…`; unset → the design's three
  defaults, `off` → no row)
- Under Docker the archive is a **host bind mount** (`./raw-storage:/data/raw-storage`),
  not a named volume: the container layer is wiped by `up --build`, and a bind mount can
  be rsync'd off the box without `docker exec`. Being a separate mount from the `jobs`
  volume, archived masters are copied rather than hardlinked — budget the disk
- `tests/conftest.py` pins the test environment (both storage roots into `tmp_path`,
  `ENABLE_AUTO_DISCOVERY`/`AUTO_DELIVER` off) so the suite never inherits a dev box's
  live `.env` — don't remove it, or tests write into the real `raw-storage` and the
  static camera simulation runs on every `TestClient`
- Local dev assumes FFmpeg on PATH and a sample jump in `/sample-data/`

## Domain Glossary
- **Tandem** — paying customer strapped to certified instructor (most common product)
- **Camera-flyer** — separate jumper filming the tandem from outside
- **Exit** — the moment of leaving the plane (huge accelerometer spike)
- **Deployment** — opening the parachute (huge decelerometer spike)
- **Canopy ride** — descent under open parachute, usually 3–5 min, mostly cut
- **GPMF** — GoPro's proprietary metadata format embedded in MP4 files
- **LRV** — Low Resolution Video, GoPro's auto-generated proxy file

## What NOT to do
- Don't run AI analysis on the canopy ride — it's 90% boring, just trim it
- Don't fine-tune Claude on Shred data — train a separate scoring model instead
- Don't use Higgsfield, Runway, Sora, or any generative video tool — customers want their REAL face, not a stylized version
- Don't render until the instructor has approved — wasted GPU time
- Don't skip the review gate in code paths — the ONLY sanctioned bypass is the
  `AUTO_DELIVER=1` deployment opt-in (business decision 2026-07: fully hands-off
  camera→customer flow), which auto-approves at the same `_maybe_auto_deliver` seam;
  with the flag off the instructor gate behaves exactly as before
- Don't persist an EDL that hasn't passed `validate_and_repair`; don't make it do I/O or import `api.*` (keep it pure and dependency-light — `api.selfie` imports it)
- Don't let the jump archive become load-bearing: nothing may *read* from
  `raw-storage/{date}/{instructor}/{customer}/`, and no archive failure may fail a job.
  The pipeline reads `jobs/<job_id>/`; the archive is a mirror for humans
- Don't delete footage off a camera on any weaker signal than an S3 key recorded in the
  retention ledger — not "it's on local disk", not "the job succeeded". And never
  `delete_all_media`: it's per-file, or not at all
- Don't file a camera pull directly into the archive — a pull knows only the camera and
  the timestamp, not the booking, so it stages into `raw-storage/_camera-staging/` and is
  mirrored into the jump folder once a job identifies whose jump it is. The dropzone Mac
  therefore only ever sees footage by camera; it gets the customer-named archive by
  **syncing it down from wherever the pipeline ran** (`deploy/mac/sync-archive.sh`,
  rsync pull, `raw/` excluded because the Mac already holds those masters)
- Don't mine a "deployment" beat from the `canopy`/`landing` scene — it's positionally unreliable; the deploy beat comes from the freefall scene at `deploy_offset`
- Don't let a locked (`preview_only`) job's clean master become reachable: the
  entitlement picks the file, never the request. No "preview" URL parameter, no
  client-side gating, no `?s=` value that unlocks, and the clean bytes are never put
  in the locked gallery's HTML. The one way to unlock is `POST /jobs/{id}/unlock`
- **Don't presign anything for a `preview_only` job** — not the master, not the photo
  zip, not "just for SkydiveOS". A presigned URL carries no entitlement check, and
  those links are persisted, archived and forwarded onward. Upload with
  `presign=False`; the customer's only address for a locked jump is `/j/{code}`
- Don't add a route that assumes the network is the security boundary: the identity
  headers are self-asserted, so anything outside `/j/*` must sit behind the service
  token (it does automatically — the gate is a middleware; don't add exemptions)
- Don't watermark with FFmpeg `drawtext` — the deployed FFmpeg lacks libfreetype. Draw
  the mark with Pillow into a full-frame RGBA PNG and composite it with `overlay`
  (the pattern in `render/caption.py`, `render/watermark.py`, `api/selfie.py`)
- Don't add preview files to `Job.outputs`, and don't re-render on unlock — the clean
  masters already exist and are already in S3; unlock is a one-field state change
- Don't persist `media_state`, branch pipeline logic on it, or rename `JobStatus` to the
  design doc's vocabulary — it's a derived view (`api/lifecycle.py`) precisely so unlock
  can flip the paywall without touching `status`
- Don't let the gallery emit a dead link: an upsell tile or unlock CTA with no
  `CHECKOUT_URL_TEMPLATE` renders as text. And don't gate the upsell row on entitlement
  — the row is the same on the locked and unlocked page

## Workflow Rules
- Before writing new code, read related modules to understand existing patterns
- When adding a pipeline stage, add a test with a sample fixture from `/sample-data/`
- Always typecheck (`mypy` / `tsc --noEmit`) before committing
- Update this CLAUDE.md when adding new top-level directories or commands
