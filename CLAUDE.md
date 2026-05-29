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
3. **Score** — run MediaPipe on the LRV proxy *only during freefall* (saves 95% compute) to score per-second highlights (smile, eye contact, in-frame)
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

## Bash Commands
- `pip install -r requirements.txt` — install Python deps
- `pytest tests/ -v` — run all pipeline tests
- `python scripts/process_jump.py <path/to/raw.mp4>` — end-to-end on a sample file
- `python scripts/replay_edl.py <job_id>` — re-render from a saved EDL
- `ffmpeg -version` — must be 6.0+ for our speed-ramp filter
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
  `REDIS_URL`, `SKYDIVEOS_API_BASE`
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
