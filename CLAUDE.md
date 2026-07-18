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

## Pipeline Stages (in order)
1. **Ingest** — pull MP4 + LRV (proxy) + GPMF from camera via Open GoPro
2. **Segment** — parse GPMF accelerometer/GPS → identify exit, freefall, deploy, landing timestamps
3. **Score** — run MediaPipe on the LRV proxy *only during freefall* (saves 95% compute) to score per-second highlights (smile, eye contact, in-frame). **Source-aware:** the MediaPipe face scorer suits the instructor selfie cam; distant external-cameraman footage scores no faces, so those scenes use a YOLO *body* scorer instead (see Key Conventions).
4. **Compose** — send timeline + scores + customer metadata to Claude API → receive JSON EDL
5. **Render** — execute EDL against full-res MP4 with FFmpeg: trim, speed ramps, intro/outro, music
6. **Review** — instructor approves or tweaks in web UI
7. **Deliver** — push final MP4 to customer (email link, WhatsApp, QR)

## Key Conventions
- All timestamps in seconds (float), not frames
- Always work on `.lrv` (proxy) for analysis, full `.mp4` only for final render
- EDL is JSON, version-tagged, persisted with every job (lets us replay/A-B test)
- Every instructor adjustment is logged → training signal for v2 model
- One job per jump; jobs are idempotent and resumable
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
- **Source-aware scene scoring** (`api.selfie.score_scenes(external_scorer=...)`):
  the default MediaPipe face scorer (`score_scene`) is tuned for the instructor
  selfie cam, where the customer's face fills the frame. On external-cameraman
  footage the tandem is far from the lens, so the face scorer silently returns
  near-zero rows. The `external` package (and Ultimate's `external` camera) instead
  route to a YOLO person-detector body scorer
  (`analysis.external_score.score_scene_external`). It emits the **exact same four
  `scores.json` fields** so nothing downstream changes — body geometry is *mapped*
  onto them: `face_in_frame` <- `clamp01(subject_size / 0.10)` (person filling ≥10%
  of frame reads as fully in-frame for a distant cam), `face_centered` <- largest
  box centred in the middle 60%×60%, `eye_contact` <- `both_visible` (exactly two
  well-separated people = an engaging tandem shot), `smile` <- `0.0` (undetectable
  at distance; downstream `.get("smile", 0.0)` paths tolerate zeros). A frame with
  no person detected scores 0.0 on all four. `score_scenes` defaults to
  `external_scorer=False`, so every other call path (`selfie`, `video_only`,
  `photo_only`, the instructor camera) is byte-identical. Never modify
  `analysis/score.py` or `analysis/__init__.py` (`FreefallScorer`/`score_freefall`
  serve the separate single-master pipeline).
- `selfie` and the camera-flyer `external` package compose videos deterministically
  (house cut, `compose_edls(use_ai=...)`); distant cameraman footage scores too few
  faces for the AI editor to sequence reliably. `_ensure_story` then guarantees every
  deliverable is in chronological jump order with all milestones (entry, exit/jump,
  canopy opening, landing) present, and the renderer clamps every clip to its scene
  file's real duration so the video/audio streams can't desync (frozen frame, audio
  continues). Photo selection has a `backfill` mode: when faces aren't detected (distant
  footage scores 0), it ranks all frames by image quality so the set still reaches ~50.
- Ultimate uploads carry a `camera_role` (`instructor`/`external`); clips stage under
  `raw/<role>/` (two GoPros emit colliding filenames). Processing auto-enqueues only
  once both role folders are populated. Music/original-audio rules: full video keeps
  original audio (music ducked at canopy); the other three are music-only.
- **Per-deliverable music** can be uploaded per job *before* processing
  (`POST /jobs/{id}/upload` for footage, `POST /jobs/{id}/music` for tracks), stored at
  `jobs/<id>/music/<deliverable>.<ext>` (deliverable ∈ `Package.music_deliverables`).
  The renderer (`api.selfie._music_paths`/`_ultimum_music_paths`) prefers the uploaded
  track, else falls back to the booking's `music` name → `templates/music`. Never fail
  a job for missing music — it just falls back.

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
- `python scripts/diagnose_ultimum.py <job_id>` — read-only diagnostic for an Ultimate job: per-camera scene classification, combo clip selection by `(camera, scene)`, video-vs-audio stream-duration sync on scene files + rendered outputs (catches the "video freezes, audio continues" desync), per-camera freefall cuts, and photo count — with findings flagging a camera collapsed to one scene, the cameraman absent from a scene, or any desync
- `ffmpeg -version` — must be 6.0+ for our speed-ramp filter
- `uvicorn api.app:app --reload` — serve the /api FastAPI service (OpenAPI docs at `/docs`); SkydiveOS calls it to create jobs, upload footage, review, approve, and stream previews
- `celery -A api.celery_app.celery_app worker -l info` — run the worker that executes the async pipeline tasks /api enqueues (set `CELERY_TASK_ALWAYS_EAGER=1` to run tasks inline without a worker, for a single-process demo)
- Camera auto-discovery (`api.app` lifespan → `ingest.discovery.CameraDiscoveryService`): when `ENABLE_AUTO_DISCOVERY=1`, the API BLE-scans every `DISCOVERY_INTERVAL_SECONDS` (default 30) for *paired* cameras (the allow-list in the MongoDB `cameras` collection), runs the existing `pull_camera` for each unseen one, **uploads each pulled MP4 to S3** (`S3_BUCKET`, key `raw/{camera_id}/{file}`) then **POSTs JSON** `{s3_key, camera_id, instructor_id}` to `{SKYDIVEOS_API_BASE}/api/media/raw-upload`. SkydiveOS creates the media/job from the key — big files never stream through the web layer, and discovery does **not** create jobs itself (needs `SKYDIVEOS_API_BASE` + `S3_BUCKET` set). Off by default — pulls stay operator/SkydiveOS-triggered until opted in. Manage the registry via `GET /cameras`, `DELETE /cameras/{id}` (soft-deactivate, admin), `POST /cameras/{id}/assign` (register/assign owning instructor, admin). The BLE scan needs the hardware-only `bleak`/Open GoPro SDK; the registry needs `pymongo[srv]` + `MONGO_URL`.
- `CAMERA_SCANNER` selects the discovery transport: `ble` (default — BLE scan + WiFi pull, wireless), `usb` (mDNS detect + `ingest.camera.WiredGoProCamera` pull — the kiosk path, one camera per scan), or `static` (no-hardware simulation: `StaticCameraScanner` + `ingest.camera.LocalSampleCamera` stage `DISCOVERY_SAMPLE_MP4` through the *real* pull path; needs `DISCOVERY_FAKE_CAMERAS`). USB and WiFi share one HTTP download path (`_SdkGoProCamera`); both need the hardware-only Open GoPro SDK.
- `python scripts/check_camera.py --usb` / `--wifi --camera <id>` — hardware smoke test: open a real GoPro and list its media (read-only), using the same Camera classes the pull uses. Verifies the SDK + connectivity before enabling discovery.
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
  `DISCOVERY_SAMPLE_MP4`, `MONGO_URL`, `MONGO_DB`, `ENFORCE_INSTRUCTOR_AUTH`
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
- Don't skip the review gate even when the model is good (5-star reviews depend on this)

## Workflow Rules
- Before writing new code, read related modules to understand existing patterns
- When adding a pipeline stage, add a test with a sample fixture from `/sample-data/`
- Always typecheck (`mypy` / `tsc --noEmit`) before committing
- Update this CLAUDE.md when adding new top-level directories or commands
