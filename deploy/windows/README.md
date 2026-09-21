# North Shore ingest box — Windows runbook

The dropzone end of the SD-card flow, on a **Windows** machine. Target state: an
instructor films their staff QR once, jumps, and pushes the card into the reader —
nothing else is typed by anyone, and the customer gets a gallery email.

South Shore runs this on a Mac (`deploy/mac/`). Everything below the OS layer is
identical; this document is the Windows half plus the North-Shore-specific values.

---

## 1. What this box is

It is an **ingest** box, not a render box.

| | |
|---|---|
| Runs | `uvicorn api.app:app` with `ENABLE_AUTO_DISCOVERY=1`, `CAMERA_SCANNER=sdcard` |
| Does | poll drive letters for a volume with `DCIM/` → stage clips → upload to S3 → notify SkydiveOS → sweep the card |
| Does **not** | segment, score, render, deliver, or serve a gallery — that is `~/autoedit-northshore` on the media EC2 |
| Therefore needs | Python 3.12 (`.python-version`), FFmpeg, OpenCV (QR), an S3 key, Mongo read access |
| Therefore does **not** need | Redis, a Celery worker, a GPU, the Open GoPro SDK (no radio is used), `PUBLIC_BASE_URL` |

No Redis: discovery substitutes an in-process emitter (`_HoldingEventEmitter`), so
hand-offs never touch a broker. `RAW_CLIP_SETTLE_SECONDS` and `REDIS_URL` in `.env`
do nothing here — they belong to the worker on EC2.

## 2. The three hosts, for North Shore

```
 Windows box (this)          SkydiveOS backend              media EC2 15.223.191.11
 ───────────────────         ─────────────────              ───────────────────────
 card → S3            ──▶    /api/media/raw-upload   ──▶    ~/autoedit-northshore
 card-status push     ──▶    /api/media/ingest-cards/status        (port 8011,
                                                             ai-northshore.ultimatedzm.com)
```

| | Value |
|---|---|
| `MONGO_DB` | `skydivingos_northshore` (same Atlas cluster / `MONGO_URL` as the other tenants) |
| Bucket | `skydiveos-northshore-media`, region `ca-central-1`, account `878013573203` |
| Auto-edit stack | `~/autoedit-northshore`, port 8011, `https://ai-northshore.ultimatedzm.com` |
| `SKYDIVEOS_API_BASE` | `https://northshore-api.parachutemontreal.ultimatedzm.com` (the value `~/autoedit-northshore/.env` already uses for its callbacks) |
| `AUTO_EDIT_API_KEY` | North Shore's own key. One value in three places: this box's `.env`, the PM box's `~/.env.northshore`, and `~/autoedit-northshore/.env`. Never reuse another tenant's — `GET /jobs` returns every customer's name, email and links |

Go **direct to the SkydiveOS consumer**, as South Shore does since 2026-09-17 — do not
run `scripts/skydiveos_bridge.py` in production. SkydiveOS implements the `staff_id`
raw-upload contract end to end. With the notify pointed at the real backend,
`CARD_STATUS_URL` should be **left unset** (it defaults to `SKYDIVEOS_API_BASE`, which
is then correct).

## 3. Install

From an elevated PowerShell, with the repo checked out at e.g. `C:\skydiveos-autoedit`:

```powershell
cd C:\skydiveos-autoedit
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
```

That installs Python 3.12 (matching `.python-version`) / FFmpeg / Git via winget, installs `uv`, runs `uv sync`,
scaffolds `.env`, and verifies ffmpeg, `cv2`, and the mount roots.

Then edit `.env`:

```ini
ENABLE_AUTO_DISCOVERY=1
CAMERA_SCANNER=sdcard
DISCOVERY_INTERVAL_SECONDS=30

SKYDIVEOS_API_BASE=https://northshore-api.parachutemontreal.ultimatedzm.com
AUTO_EDIT_API_KEY=<north shore key>
# CARD_STATUS_URL   — leave unset; it follows SKYDIVEOS_API_BASE

MONGO_URL=mongodb+srv://...
MONGO_DB=skydivingos_northshore

AWS_S3_BUCKET_NAME=skydiveos-northshore-media
AWS_REGION=ca-central-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

CAMERA_CLOCK_TZ=America/Toronto

DELETE_AFTER_TRANSFER=true
DELETE_AFTER_TRANSFER_MIN_AGE_H=24
CARD_SAFE_REQUIRES_UPLOAD=1
```

**Leave `SDCARD_MOUNT_ROOTS` commented out.** `.env.example` used to ship it set to the
POSIX roots; set, it overrides the platform default and the scanner polls `/media`,
`/run/media`, `/Volumes` on a machine that has none of them — finding no card, forever,
silently. Unset, `ingest/sdcard.py` probes `C:`–`Z:`, which is the whole point: the
reader is `E:` today and `F:` after someone plugs in a phone, so the letter cannot be
configuration.

Install as a boot service (elevated):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -HardenBox
```

`-HardenBox` also disables sleep/hibernate/disk-spindown on AC, turns off AutoPlay (the
popup is how staff end up ejecting a card mid-copy), and enables long paths.

## 4. The data prerequisites — this, not the code, is what blocks a dropzone

South Shore has been code-complete and config-green since 2026-09-17 and *still* has no
end-to-end run, because its tenant DB has no loads and its one staff member has no
`goproSerial`. Check North Shore before the install is called done:

```powershell
uv run --no-sync python scripts\check_match.py --readiness
```

It must report loads for the day, staff with a `goproSerial`, and a set `CAMERA_CLOCK_TZ`.

Two attribution routes, and at least one must work:

* **QR (preferred).** SkydiveOS already serves the staff camera QR — Staff page →
  `CameraQrCard`, payload `skydiveos-staff:<staffs._id>`, identical to what
  `ingest/qr.py` decodes. `scripts/make_instructor_qr.py` is not needed operationally.
  Brief staff: the marker must be a **short** clip (a clip longer than
  `SDCARD_QR_MAX_CLIP_SECONDS`, 60 s, is never probed) with the code visible
  **immediately** (only the first `SDCARD_QR_SCAN_SECONDS`, 8 s, is sampled). Start of
  day, end of day, or mid-card — position is not a protocol.
* **Serial fallback.** SkydiveOS matches `Staff.findOne({ goproSerial })` **exactly**,
  while this box sends only the serial's **last 4 digits** as `camera_id`. So the field
  must hold e.g. `8228`, not `C3504224544313`. A mismatch is a 404, which is a permanent
  hand-off error and is never retried.

Either way the clip still needs a **load** whose `businessDate`/`departureTime` window
contains its `captured_at`. QR yields the staff; it cannot conjure a load.

## 5. Acceptance test

```powershell
# 1. Card identity + QR, read-only, nothing running
uv run --no-sync python scripts\check_sdcard.py --decode

# 2. Service up and scanning
Get-ScheduledTaskInfo -TaskName 'SkydiveOS Ingest'
Get-Content logs\ingest.log -Tail 40 -Wait

# 3. Operator display — bell + banner on "safe to remove"
uv run --no-sync python scripts\watch_cards.py
```

Then the real one: film the QR, film a jump, insert the card, and watch for

1. `logs\ingest.log` — card detected, clips staged, S3 keys written
2. `watch_cards.py` — `pulling` → `uploading` → **`safe_to_remove`**
3. the SkydiveOS operator screen showing the same banner (that proves the pushed
   snapshot authenticated — see §6)
4. a job on `https://ai-northshore.ultimatedzm.com`, and the customer's gallery email

Only remove the card on `safe_to_remove`: it means every clip is confirmed in S3 **and**
swept off the card. Pulling it earlier leaves the card full, which is how a card silently
stops recording mid-day.

## 6. Troubleshooting, Windows-flavoured

| Symptom | Cause |
|---|---|
| Scanner never sees a card | `SDCARD_MOUNT_ROOTS` is set (POSIX paths from `.env.example`). Comment it out. Confirm with `check_sdcard.py` |
| Footage flows but the operator banner never appears | Wrong `AUTO_EDIT_API_KEY`. SkydiveOS's `/api/media/raw-upload` has **no** token check; `/api/media/ingest-cards/status` **does**. The banner is the canary for the key, not the ingest |
| Banner 404s forever | `CARD_STATUS_URL` points at a bridge. Only the real SkydiveOS backend has that route |
| Every unlabelled card gets the same id | Not on Windows — `card_id_for_mount` falls back to the **drive letter** because `Path("E:/").name` is empty. If you see this, the card has no readable `MISC/version.txt` *and* something overrode the id |
| Card sits in `uploading` forever | Ledger records with no `size` can never be confirmed, and `ingest/pull.py` skips an already-staged clip before the emit, so no new sized record is ever written. Clear that card's staging day folder and re-pull |
| Service runs by hand but not as a task | The task must run as a **user**, not SYSTEM — `uv` installs into `%USERPROFILE%\.local\bin`. `install-service.ps1` uses `-LogonType S4U` for exactly this |
| Footage lands somewhere unexpected | `RAW_STORAGE_ROOT` defaults to the **relative** `./raw-storage`; a task that starts in `C:\Windows\System32` stages there. `run.ps1` sets the working directory |
| Clips staged, nothing after a restart | Hand-offs are in-process. A restart strands already-staged clips; clear the staging day folder to retrigger |

## 7. Updating this box

```powershell
cd C:\skydiveos-autoedit
git pull origin main
uv sync
powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -Action remove
powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1
```

The media EC2 side is a separate deploy with its own ordering rule (hub first) —
see `deploy/RELEASE_2026-09-18_card_safe_to_remove.md` §4b.
