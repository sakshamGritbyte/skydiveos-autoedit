# GoPro Ingest — Client Setup & Full Flow

How GoPro footage gets from the cameras at the dropzone, all the way into the
SkydiveOS media module — and exactly what to install, configure, and run to make it
work. Covers both a **no-hardware test** (simulation) and the **real GoPro** setup.

> TL;DR — A small computer at the dropzone pulls footage off the GoPros and pushes it
> to the cloud. The cloud cannot reach a camera directly (GoPro WiFi/Bluetooth is
> short-range), so this local "bridge" computer is required.

---

## 1. The big picture

Three pieces talk to each other:

```
   GoPro(s) ──BLE/WiFi/USB──►  AI backend (auto-edit)  ──►  SkydiveOS backend  ──►  SkydiveOS frontend
   (at dropzone)               port 8000                    port 8001               port 3000
                               pulls + uploads to S3        creates media record    media module (UI)
                                      │                            ▲
                                      └──────── S3 bucket ─────────┘
                                          (both share ONE bucket)
```

| Component | Repo | Port | Job |
|-----------|------|------|-----|
| **AI backend** (auto-edit) | `skydiveos-autoedit` | 8000 | Detect cameras, pull footage, upload to S3, notify SkydiveOS |
| **SkydiveOS backend** | `skydiving-os/backend` | 8001 | Receive the notification, verify the S3 file, create the media record |
| **SkydiveOS frontend** | `skydiving-os/frontend` | 3000 | The media module the operator/instructor sees |

**Why a local computer is mandatory:** a GoPro's WiFi/Bluetooth only reaches a few
metres. A cloud server in a datacenter can never connect to a camera at the dropzone.
So the **AI backend runs on a computer physically near the cameras**; it pulls the
footage and pushes it to the cloud over the internet. The cloud only ever *receives*.

---

## 2. What you need

- **A computer at the dropzone** (the "client computer") — Linux/Mac/Windows, basic
  spec is fine (rendering happens in the cloud, not here). Needs internet, and either
  **WiFi + Bluetooth** (wireless cameras) or a **USB port** (cable / kiosk).
- **An S3 bucket** (e.g. `skydivingoss`) + AWS access key/secret + region
  (e.g. `ap-south-1`). The AI backend and the SkydiveOS backend must use the **same
  bucket**.
- **A MongoDB connection** (the camera allow-list + SkydiveOS data).
- **The SkydiveOS backend URL** the AI backend will notify (e.g. `http://localhost:8001`
  for local, or `https://<your-skydiveos-domain>` in production).
- **Python 3.11+** and, for real cameras, the **Open GoPro SDK** (install below).

---

## 3. Configuration — the three `.env` files

### 3.1 AI backend — `skydiveos-autoedit/.env`

```ini
# ── Auto-discovery ───────────────────────────────────────────────
ENABLE_AUTO_DISCOVERY=1
CAMERA_SCANNER=static                 # TEST mode. Real wireless: ble  |  USB kiosk: usb
DISCOVERY_INTERVAL_SECONDS=30         # how often it scans for cameras

# ── TEST mode only (delete these three for real cameras) ─────────
DISCOVERY_FAKE_CAMERAS=TESTGOPRO001   # a fake serial to simulate
DISCOVERY_SAMPLE_MP4=templates/outro.mp4   # path to ANY sample video file
DISCOVERY_SAMPLE_COUNT=12             # how many clips this fake camera reports

# ── S3 (SAME bucket as the SkydiveOS backend) ────────────────────
AWS_S3_BUCKET_NAME=skydivingoss
AWS_ACCESS_KEY_ID=<key>
AWS_SECRET_ACCESS_KEY=<secret>
AWS_REGION=ap-south-1

# ── Where to notify (the SkydiveOS backend) ──────────────────────
SKYDIVEOS_API_BASE=http://localhost:8001     # prod: https://<your-skydiveos-domain>

# ── Camera allow-list registry ───────────────────────────────────
MONGO_URL=<mongodb-url>
MONGO_DB=skydiveos
```

Run it:
```bash
uv run uvicorn api.app:app --reload --port 8000
```

### 3.2 SkydiveOS backend — `skydiving-os/backend/.env`

```ini
PORT=8001

# S3 — needed to VERIFY the uploaded object (same bucket)
AWS_ACCESS_KEY_ID=<key>
AWS_SECRET_ACCESS_KEY=<secret>
AWS_REGION=ap-south-1
AWS_S3_BUCKET_NAME=skydivingoss

# Its own database
MONGO_URL=<mongodb-url>

# The AI backend URL — used to keep the camera registry in sync when pairing,
# and (later) to trigger AI editing. REVERSE direction from SKYDIVEOS_API_BASE.
AI_BACKEND_URL=http://127.0.0.1:8000

# NOTE: SKYDIVEOS_API_BASE is NOT needed here — this side only RECEIVES.
```

Run it:
```bash
npm run dev
```

### 3.3 SkydiveOS frontend — `skydiving-os/frontend/.env`

```ini
REACT_APP_BACKEND_URL=http://localhost:8001       # the SkydiveOS backend
REACT_APP_AI_BACKEND_URL=http://127.0.0.1:8000    # the AI backend
REACT_APP_FRONTEND_URL=http://localhost:3000
```

Run it:
```bash
npm start
```

### Connection map (who talks to whom, via which env var)

| Connection | Env var | Set in |
|------------|---------|--------|
| AI backend → SkydiveOS (notify new footage) | `SKYDIVEOS_API_BASE` | AI backend `.env` |
| SkydiveOS → AI backend (pairing + editing) | `AI_BACKEND_URL` | SkydiveOS backend `.env` |
| Frontend → SkydiveOS backend | `REACT_APP_BACKEND_URL` | frontend `.env` |
| Frontend → AI backend | `REACT_APP_AI_BACKEND_URL` | frontend `.env` |
| Both backends → S3 | `AWS_S3_BUCKET_NAME` (**same** bucket) | both backend `.env` |

---

## 4. One-time setup (in order)

```
1. Fill in all three .env files (section 3).

2. Install the AI backend deps on the client computer:
     uv pip install -r requirements.txt
   For REAL cameras, also install the hardware SDK:
     uv pip install ./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control

3. Start the services IN THIS ORDER:
     a) SkydiveOS backend  (8001)   ← FIRST (the AI backend notifies it)
     b) AI backend         (8000)
     c) SkydiveOS frontend (3000)

4. Pair each GoPro to an instructor — from the UI:
     Frontend → Staff → open an instructor → "GoPro Camera" section → "Pair GoPro"
     → type the serial (e.g. TESTGOPRO001) → Pair
   This updates BOTH the AI backend allow-list and the instructor's account.

   [REAL cameras only — once per camera, on the client computer:]
     python -m ingest.pull --camera <serial> --pair
   (creates the Bluetooth bond; not needed in test mode)

5. Done. Footage now flows automatically (section 6).
```

---

## 5. Verify with a no-hardware test (simulation)

You can test the whole pipeline without any GoPro using `CAMERA_SCANNER=static`.

1. Use the test `.env` from 3.1 (static + the three `DISCOVERY_SAMPLE_*` vars).
2. Pair the fake serial `TESTGOPRO001` to an instructor in the UI (step 4 above).
3. Start backend → AI backend.
4. Within ~30 s, 12 clips (copies of the sample video) flow through to the media
   module: each shows a green **NEW** badge, a toast appears, and the detail view
   plays the video.

**Simulate a second jump on the same camera** (new footage arriving later):
```bash
python scripts/sim_add_clip.py TESTGOPRO001        # add 1 clip
python scripts/sim_add_clip.py TESTGOPRO001 3      # add 3 clips
```
The running AI backend picks up only the new clips on its next scan — no restart.

**Reset a camera** (re-run the whole batch from scratch):
```bash
rm -rf raw-storage/TESTGOPRO001 raw-storage/.sim_clips/TESTGOPRO001
```

---

## 6. Real GoPro flow & daily operation

### Switch from test to real — change only the AI backend `.env`:
```ini
CAMERA_SCANNER=ble            # wireless (BLE scan + WiFi pull)   |  or: usb (cable/kiosk)
# delete: DISCOVERY_FAKE_CAMERAS, DISCOVERY_SAMPLE_MP4, DISCOVERY_SAMPLE_COUNT
SKYDIVEOS_API_BASE=https://<your-skydiveos-domain>   # in production
```
Then `--pair` each camera once (step 4) and assign it to an instructor in the UI.

### What happens on every jump (fully automatic):
```
Instructor lands, camera comes near the client computer
   → AI backend BLE-scans (every 30 s), detects the camera
   → checks the allow-list (is it one of ours?) ✓
   → pulls ONLY new clips off the SD card over WiFi (old ones skipped)
   → uploads each to S3, notifies SkydiveOS
   → SkydiveOS finds the instructor by serial, creates the media record
   → frontend media module: NEW badge + toast + playable preview
```

The operator does **nothing** per jump except bring the camera into range (wireless)
or plug it in (USB). New footage on the card is detected automatically; already-pulled
clips are never re-pulled.

### Real-world notes
- **Range is short** — bring cameras within a few metres of the client computer.
- **One camera at a time** — a single WiFi card joins one camera's access point at a
  time; several cameras in range are pulled one after another.
- **`--pair` is once per camera, ever** — not per pull. Only repeat for a brand-new
  camera or if the Bluetooth bond is lost (factory reset, re-paired elsewhere).

---

## 7. Golden rules & troubleshooting

| Rule / Symptom | Cause | Fix |
|----------------|-------|-----|
| **Start backend (8001) before the AI backend** | Notifications aren't retried | If the AI backend ran first, footage 404s — restart it once the backend is up |
| `No instructor is paired to GoPro serial "..."` | Serial not assigned to an instructor | Pair it in the UI (Staff → instructor → GoPro Camera), or `node src/scripts/pairGoProSerial.js <serial> <name>` |
| "Nothing transferred" on re-run (test) | Idempotency — clips already staged | `rm -rf raw-storage/<serial>` (and `.sim_clips/<serial>`), then re-run |
| `No S3 object found at key ...` | Notified before the file was uploaded, or wrong bucket | Ensure both backends use the **same** `AWS_S3_BUCKET_NAME`; the real flow uploads before notifying |
| `Address already in use` (port 8000) | An old AI backend is still running | `fuser -k 8000/tcp`, then start again |
| Media appears but **no preview** | (Fixed) raw videos had no preview key | The detail dialog now streams the original MP4; ensure the frontend is up to date |
| Serial change in `.env` ignored | Settings are cached at startup | Restart the AI backend after editing its `.env` |

### Three things that must always be true
1. The serial in the camera registry **= the serial paired to an instructor** in SkydiveOS.
2. The **same S3 bucket** is configured on both backends.
3. **`SKYDIVEOS_API_BASE`** lives in the AI backend `.env`; **`AI_BACKEND_URL`** lives in
   the SkydiveOS backend `.env` (they point at each other, opposite directions).

---

## 8. Production checklist (beyond local testing)

- [ ] AI backend runs on a machine **physically at the dropzone** (not the cloud box).
- [ ] `CAMERA_SCANNER=ble` (or `usb`); the three `DISCOVERY_SAMPLE_*` vars removed.
- [ ] `SKYDIVEOS_API_BASE` points at the **public** SkydiveOS URL (https), not localhost.
- [ ] Each camera `--pair`ed once and assigned to an instructor in the UI.
- [ ] The AI backend started as a service (systemd on Linux / launchd on Mac — see
      `deploy/mac/` — / auto-start) so it survives reboots.
- [ ] Disk on the client computer has room for staged footage (`raw-storage/`); prune
      old folders or rely on the idempotent skip.

---

## 9. macOS client deploy (scripted)

For a client Mac at the dropzone, `deploy/mac/` automates the whole setup:

```bash
cd ~/skydiveos-autoedit
bash deploy/mac/install.sh          # Homebrew + python@3.11 + ffmpeg + uv;
                                    # `uv sync` deps into .venv + Open GoPro SDK;
                                    # scaffolds .env
# → edit .env (S3_BUCKET, AWS_* keys, SKYDIVEOS_API_BASE=<EC2 url>, MONGO_URL,
#   ENABLE_AUTO_DISCOVERY=1, CAMERA_SCANNER=ble)
# → System Settings > Privacy & Security > Bluetooth > enable Terminal
# → pair each camera once (via the .venv):
uv run python -m ingest.pull --camera <serial> --pair --name "<label>" --instructor-id <id>
bash deploy/mac/load-service.sh     # run as a launchd service (auto-start on boot,
                                    # relaunch on crash); logs in ./logs/
# → FIRST scan: approve the macOS Bluetooth prompt for the *service* (Terminal's
#   permission does NOT carry over); until then it finds zero cameras.
```

> Deps note: there is no `requirements.txt` — deps live in `pyproject.toml`/`uv.lock`
> and go into a `.venv` via `uv sync`. The service runs with `uv run --no-sync` so it
> reuses that `.venv` without stripping the separately-installed GoPro SDK. Don't run a
> bare `uv sync` afterwards (it removes the SDK) — re-run `install.sh` to restore it.

Manage it: `launchctl list | grep com.skydiveos.ingest`,
`tail -f logs/ingest.err.log`, `bash deploy/mac/load-service.sh unload`.

The Mac runs **ingest only** — it pulls footage and pushes to S3 + notifies the
EC2 SkydiveOS backend. AI editing/rendering stays in the cloud (`DEPLOY.md`).

---

*Related: `DEPLOY.md` (cloud edit/render server), `CLAUDE.md` (project overview and the
full list of commands).*
