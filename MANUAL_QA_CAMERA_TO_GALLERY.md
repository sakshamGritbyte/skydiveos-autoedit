# Manual QA — GoPro connected → videos + photos in the customer gallery

One linear pass over the **whole** chain, stage by stage, with the exact thing to look
at after each one. Where the other docs cover breadth, this covers depth on a single
jump: 14 gates from powering the camera on to the customer opening their gallery and
downloading.

- Per-package matrix (all 5 products): [`MANUAL_QA.md`](MANUAL_QA.md)
- No camera at all (one-command audit): [`QA_NO_GOPRO.md`](QA_NO_GOPRO.md)
- Env/config to go live: [`GO_LIVE.md`](GO_LIVE.md)

**Rule #1 — always use your own email as `customer_email`** until every gate below is
green. A wrong edit or a wrong link must never reach a paying customer.

### Shell setup (paste once per terminal)

```bash
AE=http://<ec2-ip>:8000                     # auto-edit API
TOK=<AUTO_EDIT_API_KEY>                     # every route except /j/* needs this
H="Authorization: Bearer $TOK"
J() { python3 -m json.tool; }               # jq may not be installed
W="docker exec skydiveos-autoedit-worker-1" # run things inside the worker (EC2)
```

Two hosts are in play: the **dropzone Mac** runs the ingest service (Stages 1–2, 14);
**EC2** runs the API + worker (Stages 3–13). Run each stage's commands on the host named
in its heading.

---

## Stage 0 — Pre-flight (Mac + EC2, ~5 min)

**Mac:**

```bash
uv run --no-sync python scripts/check_match.py --readiness
grep -E '^(ENABLE_AUTO_DISCOVERY|CAMERA_SCANNER|CAMERA_CLOCK_TZ|S3_BUCKET|SKYDIVEOS_API_BASE|MONGO_URL)=' .env
launchctl list | grep com.skydiveos.ingest
```

- [ ] `camera clock tz : America/Toronto` — **not** "unset". Unset skews `captured_at`
      by the UTC offset and the load match silently misses.
- [ ] The test instructor appears under **with goproSerial** (`staffs.goproSerial` is the
      only bridge from camera → staff; the registry's `instructor_id` is never matched on).
- [ ] The camera id under **paired cameras** — the **last 4 digits** of the serial, which
      is all a GoPro advertises over BLE.
- [ ] `ENABLE_AUTO_DISCOVERY=1`, `CAMERA_SCANNER=ble`, ingest service has a PID.

**EC2:**

```bash
grep -E '^(AUTO_DELIVER|CELERY_TASK_ALWAYS_EAGER|PUBLIC_BASE_URL|SMTP_HOST|DELIVERY_FROM_EMAIL|AUTO_EDIT_API_KEY)=' .env
curl -s -o /dev/null -w '%{http_code}\n' $AE/jobs            # expect 401 with the gate on
curl -s -H "$H" $AE/jobs | J | head -5                       # expect 200
```

- [ ] `AUTO_DELIVER=1` (hands-off delivery), `CELERY_TASK_ALWAYS_EAGER=0` — **eager with
      a worker running blocks the whole API for the ~15 min of an edit**.
- [ ] `PUBLIC_BASE_URL` set. Without it there is no `/j/{code}` gallery and a
      `preview_only` job is refused at `POST /jobs` with 422.
- [ ] Ungated `GET /jobs` returns **401**. If it returns job data, stop — customer names,
      emails and delivery links are public (see [`deploy/PROXY_LOCKDOWN.md`](deploy/PROXY_LOCKDOWN.md)).

**Load manifested in SkydiveOS**, on the footage's **own date** (the match is
camera + capture-instant inside a load's flight window, same day — it refuses to guess):

- [ ] Instructor = the staff whose camera is paired · Customer = **your** email ·
      Media add-on set · Video type Inside → `selfie`, Outside → `external`.
- [ ] Go/no-go gate:
      ```bash
      uv run --no-sync python scripts/check_match.py --day <JUMP-DATE>
      ```
      Must show **`deliverable`** for that camera and `FAILED 0`. Red here = the
      automation cannot complete; fix the load before touching the camera.

---

## Stage 1 — Camera on → discovered → pulled (Mac)

```bash
tail -f logs/ingest.err.log
```

Power the GoPro on, keep it near the Mac. Within `DISCOVERY_INTERVAL_SECONDS` (30):

- [ ] `Camera <id> discovered, pull enqueued`
- [ ] `camera <id>: N video(s) on card` then `downloading <path> -> <dest>` per file
- [ ] Files land in the **card mirror**, not the customer archive:
      ```bash
      find raw-storage/_camera-staging -name '*.MP4' -newermt '-15 min' -ls
      ```
      Path shape: `raw-storage/_camera-staging/<camera_id>/<YYYY-MM-DD>/<file>.MP4`.
      A pull knows only the camera and the clock — never a booking — so it must **not**
      appear under `raw-storage/<date>/<instructor>/<customer>/`.
- [ ] `.LRV` proxies alongside the masters (a warning if the camera has none is fine —
      analysis falls back to the master).
- [ ] While the pull runs, the log does **not** show BLE `Operation already in progress`.
      Scanning is suppressed for the duration of a pull; a second camera is picked up on
      the next tick instead.
- [ ] Re-run the pull with the camera still on → `skip <file> (already staged at …)`.
      Pulls are idempotent; nothing is handed off twice.

---

## Stage 2 — Pull → S3 → SkydiveOS notified (Mac)

- [ ] `uploading <mp4> to S3 + notifying SkydiveOS (camera …, instructor …, role …)`
- [ ] `handed <mp4> off to SkydiveOS (…)`
- [ ] The role line, when the load disagrees with the registry hint:
      `load-derived role <r> overrides registry hint <h>`. **The load is the authority** —
      the same person is instructor on one jump and cameraman on the next, on the same
      camera.
- [ ] The object is really in S3:
      ```bash
      aws s3 ls s3://$S3_BUCKET/raw/<camera_id>/ --recursive | tail -5
      ```
- [ ] Retention ledger recorded the S3 key (this — and only this — authorises clearing
      the card later, at Stage 14):
      ```bash
      python3 -m json.tool < raw-storage/_camera-staging/<camera_id>/.transferred.json | tail -20
      ```
- [ ] **Network-loss retry:** pull the Mac's Wi-Fi mid-pull, or point
      `SKYDIVEOS_API_BASE` at a dead port for one run. Expect
      `hand-off to SkydiveOS failed …; retry k/N in Ns` and then success once the network
      is back — not a dropped event.

> If SkydiveOS does not yet turn `/api/media/raw-upload` into a job (their side), the
> chain stops here. Continue with **Stage 3b**.

---

## Stage 3 — A job exists, matched to the right customer (EC2)

### 3a — SkydiveOS created it (the real path)

```bash
curl -s -H "$H" $AE/jobs | J | head -40
```

- [ ] Newest job carries the load's **customer name + your email**, the package derived
      from `mediaPackage`+`videoType`, `instructor_name`, `booking_id`.
- [ ] `entitlement`: `edited_download` (media purchased) or `preview_only` ("filmed it
      anyway"). Both are valid — Stage 12 tests each.
- [ ] `media_state`: `PENDING_CAPTURE` → `UPLOADED` (derived, never stored).

### 3b — Fallback: match locally and drive the same API (Mac)

```bash
uv run --no-sync python scripts/demo_from_load.py \
  --serial <SERIAL> --dir raw-storage/_camera-staging/<camera_id>/<date> \
  --api $AE
```

- [ ] It prints the resolved match — **customer, email, package, role** — before creating
      anything. Confirm the name is the person on the load, then accept.
- [ ] On ambiguity (0 or >1 candidate jumpers) it **refuses and flags** instead of
      guessing. That is a pass, not a failure: mis-matching emails customer A's video to
      customer B.

Capture the job id for the rest of the run:

```bash
JOB=<job_id>
watch -n5 "curl -s -H '$H' $AE/jobs/$JOB | python3 -m json.tool | grep -E 'status|media_state|error'"
```

- [ ] `queued → processing → ready → delivered` (with `AUTO_DELIVER=1`), never stuck.

---

## Stage 4 — Footage staged in the job + raw archived (EC2)

```bash
$W ls -la /data/jobs/$JOB /data/jobs/$JOB/raw
```

- [ ] `raw/` holds the masters (for `ultimum`: `raw/instructor/` **and** `raw/external/`).
- [ ] `job.json` + `booking.json` present.
- [ ] Raw is **already mirrored into the archive, before any editing**:
      ```bash
      find raw-storage/<date>/*/*/raw -newermt '-30 min' | head
      ```
      This is the whole point of archiving at the "footage landed" seam — a failed edit
      must not lose the masters.
- [ ] `ultimum` only: after the **first** camera the job **waits** (`queued`); processing
      starts only once both role folders are populated.

---

## Stage 5 — Segment: exit / deploy found (EC2)

```bash
$W python3 -c "
import json,glob
for f in glob.glob('/data/jobs/$JOB/scene_manifest*.json'):
    m=json.load(open(f)); print(f, m.get('exit_offset'), m.get('deploy_offset'), len(m.get('scenes',[])))
    print(' types:', [s.get('type') for s in m['scenes']])
    print(' flagged:', m.get('flagged'))"
```

- [ ] `scene_manifest.json` exists (`scene_manifest_instructor.json` +
      `_external.json` for `ultimum`).
- [ ] A `freefall` scene with **both** `exit_offset` and `deploy_offset` set. Missing =
      GPMF telemetry absent → the footage was re-encoded; use the SD card original.
- [ ] Scene types read like a jump: boarding/plane → `freefall` → `canopy` → `landing`.
- [ ] `file_offsets` present per scene (compose targets the plane-entry *file*, not the
      concatenated scene's head).
- [ ] If a post-freefall scene was renamed, `flagged` contains
      `auto-renamed canopy->landing (accl signature)` — that surfaces for instructor
      review rather than being silent.

---

## Stage 6 — Score (EC2)

```bash
$W python3 -c "
import json,glob
for f in glob.glob('/data/jobs/$JOB/scores*.json'):
    r=json.load(open(f)); print(f, len(r), r[:2])"
```

- [ ] Per-second rows, covering the **freefall window only** (scoring the canopy ride
      would burn ~95% of the compute for nothing).
- [ ] Distant `external` footage scoring near-zero faces is expected — the house cut and
      photo `backfill` mode cover it. Not a failure.

---

## Stage 7 — Compose + deterministic validation (EC2)

```bash
$W ls /data/jobs/$JOB/edl_*.json
$W python3 -m json.tool /data/jobs/$JOB/validation_report.json | head -40
```

- [ ] One `edl_*.json` per video deliverable (`edl_full`, `edl_highlights`,
      `edl_freefall`; `edl_external_freefall` + `edl_chute_libre` for `ultimum`).
- [ ] **`validation_report.json` exists.** No report = `validate_and_repair` never ran,
      and an unvalidated EDL must never be persisted.
- [ ] Repairs are listed and readable. Repairs are normal — that is the backstop doing
      its job (dropped deploy beat, landing footage bleeding into a freefall cut, missing
      plane-entry, duplicate multi-cam interleaves).
- [ ] Freefall-type cuts sit inside `[exit_offset−8, deploy_offset+3]` and contain no
      non-freefall scene.
- [ ] `full_video` / `highlights` contain the deploy beat and the boarding entry.

---

## Stage 8 — Render: the videos (EC2)

```bash
curl -s -H "$H" $AE/jobs/$JOB/deliverables | J
```

Expected per package:

| package | deliverables |
|---|---|
| `selfie` / `external` | `full_video`, `highlights`, `freefall`, `photos` |
| `video_only` | `full_video`, `highlights`, `freefall` |
| `photo_only` | `photos` only |
| `ultimum` | `full_video`, `highlights`, `external_freefall`, `chute_libre_selfie`, `photos` |

- [ ] Every expected file present, non-zero, and playable.
- [ ] **A/V sync — the freeze check** (video ends, audio keeps going):
      ```bash
      $W bash -lc 'for f in /data/jobs/'$JOB'/*.mp4; do
        v=$(ffprobe -v0 -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$f")
        a=$(ffprobe -v0 -select_streams a:0 -show_entries stream=duration -of csv=p=0 "$f")
        echo "$(basename $f) v=$v a=$a"; done'
      ```
      Every pair within **1 s**.
- [ ] Watch `full_video`: chronological, milestones present (plane entry, exit, canopy
      opening, landing), original audio kept with music ducked at canopy.
- [ ] The other videos are music-only. Music plays — if the booking named none, one
      `templates/music` track was picked at random and persisted, so a replay uses the
      **same** track.
- [ ] `ultimum` only:
      ```bash
      $W python scripts/diagnose_ultimum.py $JOB
      ```
      Both cameras appear in the combo, no camera collapsed to a single scene, no desync.

---

## Stage 9 — Photos (EC2)

```bash
curl -s -H "$H" $AE/jobs/$JOB/photos | J | head -20
$W bash -lc 'ls /data/jobs/'$JOB'/photos/*.jpg | wc -l'
```

- [ ] `photos/index.json` present; count ~**50** (selfie/external) or ~**140**
      (`photo_only`).
- [ ] Open a handful: full-res JPEGs, in focus, the customer's face — not canopy filler.
- [ ] `video_only` has **no** `photos` dir.

---

## Stage 10 — Watermarked previews (Path B only, EC2)

Skip for `edited_download`.

```bash
$W ls -la /data/jobs/$JOB/preview_*.mp4
```

- [ ] One `preview_<name>.mp4` per video, 720p, visibly watermarked.
- [ ] They are **not** in `outputs` (`GET /jobs/$JOB | grep preview` → nothing). Previews
      in `outputs` would leak into the S3 delivery set.
- [ ] Kill-case: a `preview_only` job whose preview render fails must end `failed` — a
      locked gallery with nothing watchable is worse than a re-queue.

---

## Stage 11 — Approve → S3 → email (EC2)

With `AUTO_DELIVER=1` this happens by itself; verify it *did*.

```bash
curl -s -H "$H" $AE/jobs/$JOB | J | grep -A12 delivery_links
docker logs skydiveos-autoedit-worker-1 --since 20m | grep -E 'auto-approved|uploaded .*→ s3|gallery email sent'
```

- [ ] `AUTO_DELIVER: job … auto-approved, delivery enqueued` — no button was pressed.
- [ ] `uploaded <name> → s3://…/deliveries/$JOB/` for **every** deliverable (photos as a
      zip). The clean masters upload on **both** paths — that is what makes unlock instant.
- [ ] `delivery_links.gallery` present.
- [ ] **`preview_only` job: `delivery_links` contains ONLY `gallery`.** Any per-deliverable
      URL here is a leak — a presigned URL answers to whoever holds it and these links are
      persisted, archived and forwarded to SkydiveOS.
- [ ] `gallery email sent to <you>` and **one** email arrives, with a single
      `{PUBLIC_BASE_URL}/j/{code}?s=e#tab-video` link.
- [ ] `status: delivered`, `media_state`: `DELIVERED` (Path A) / `LOCKED_PREVIEW` (Path B).
- [ ] No email but `SKYDIVEOS_API_BASE` set → job still `delivered` with links forwarded.
      Neither → the job **fails** rather than claiming delivery. Both are correct.

---

## Stage 12 — The customer gallery (open it on your phone)

```bash
GAL=$(curl -s -H "$H" $AE/jobs/$JOB | python3 -c 'import sys,json;print(json.load(sys.stdin)["delivery_links"]["gallery"])')
echo $GAL
```

**Both paths:**
- [ ] Opens with **no login** — the code is the page's only credential.
- [ ] Hero reads: eyebrow · customer name · `<date> · <product> · Instructor <name>`.
- [ ] Video tab plays every deliverable; seeking works (range requests).
- [ ] Photos tab shows the stills; a full-res JPEG opens.
- [ ] "Add to your day" upsell row renders — **the same row on both paths**. A tile with
      no `CHECKOUT_URL_TEMPLATE` is **text**, never a dead link.
- [ ] Renders correctly on a phone.

**Path A (`edited_download`):**
- [ ] Green accent, `1080P · FULL QUALITY`, `⬇ Download video` with
      "1080p MP4 · N MB · yours to keep". Download completes and plays locally.

**Path B (`preview_only`):**
- [ ] Amber accent, `720P PREVIEW`, watermark visible, no download control.
- [ ] Primary action is `🔒 Unlock full video — <price>`.
- [ ] Photos are **hidden behind a count teaser**; a direct photo URL returns **404**.
- [ ] **The master is unreachable at any URL** — same name, still the preview:
      ```bash
      curl -sI "$GAL/media/full_video" | head -3     # size = the 720p preview
      ```
      There is no URL, query param or `?s=` value that unlocks. If clean bytes come back
      here, stop everything.
- [ ] Unlock, server-to-server, with all three gates (service token, admin, non-empty
      payment reference):
      ```bash
      curl -s -XPOST $AE/jobs/$JOB/unlock -H "$H" -H 'X-Role: admin' \
        -H 'Content-Type: application/json' -d '{"payment_reference":"qa-test-1"}' | J
      ```
      - [ ] Missing `payment_reference` → rejected.
      - [ ] The **already-open page flips itself** within a few seconds (it polls
            `/j/{code}/state`) — no re-render, no new email, no new link.
      - [ ] Same URL now serves the clean 1080p master and the photos appear.
      - [ ] `status` **unchanged**; only `entitlement`, `paid_at`, `payment_reference`
            moved. `media_state`: `UNLOCKED`.
      - [ ] Calling unlock twice is a no-op.

---

## Stage 13 — Jump archive (EC2, then Mac)

```bash
find raw-storage/<date> -maxdepth 3 | head -30
python3 -m json.tool < raw-storage/<date>/<Instructor>/<Customer>/manifest.json | head -40
```

- [ ] Tree is `<date>/<Instructor-Name>/<Customer-Name>/{raw,edited,preview,photos}/`.
- [ ] `edited/` holds the clean masters; `preview/` holds the watermarked ones with the
      `preview_` prefix **stripped**, so `preview/full_video.mp4` lines up with
      `edited/full_video.mp4`.
- [ ] `manifest.json` carries `job_id`, booking, package, status, `media_state`,
      `delivery_links`, and a **sha256 per file**.
- [ ] Nothing downstream reads from here — it is a mirror for humans. Renaming a folder
      must not break anything.
- [ ] Mac pulls it down (`raw/` excluded — the Mac already holds those masters):
      ```bash
      bash deploy/mac/sync-archive.sh
      ```

---

## Stage 14 — Card cleanup on the next connect (Mac)

Only if `DELETE_AFTER_TRANSFER=1`. Start with `DELETE_AFTER_TRANSFER_DRY_RUN=1`.

- [ ] Bring the same camera back in range. Log shows
      `card cleanup: deleted <file> (safe: <s3_key>)` — **each line names the S3 key that
      authorised it**.
- [ ] Files with no ledger entry are **kept**. Delete authorisation is an S3 key, never
      "it's on local disk" and never "the job succeeded".
- [ ] Deletions are per-file. `delete_all_media` must never appear.
- [ ] Flip dry-run off and confirm the card frees up (a full card silently stops
      recording mid-day — that is why this exists).

---

## When something fails

```bash
curl -s -H "$H" $AE/jobs/$JOB | J | grep -E 'status|error'
docker logs -f skydiveos-autoedit-worker-1        # pipeline stages live
docker logs -f skydiveos-autoedit-api-1           # requests, gallery hits
tail -f logs/ingest.err.log                       # Mac: discovery + pull
```

| Symptom | Cause | Fix |
|---|---|---|
| Camera never discovered | BLE permission not granted to the *service* (Terminal's doesn't carry over); wrong camera id | Approve the macOS Bluetooth prompt; use the **last 4 digits** of the serial |
| `Operation already in progress` | BLE scan racing a pull | Expected to be suppressed — if it persists, the pull is not registering as in-flight |
| Hand-off retries forever | Mac joined to the camera's AP has no internet; `SKYDIVEOS_API_BASE` wrong | Check the URL; retries resume once the network is back |
| Match says `FAILED`/`no-media` | Load on the wrong date, wrong instructor, or no media add-on | Fix the load, re-run `check_match --day` |
| Match off by 4–5 h | `CAMERA_CLOCK_TZ` unset/wrong | Set the DZ's IANA zone |
| `status: failed`, no exit/deploy | Re-encoded footage, GPMF stripped | Use the SD-card original |
| `ultimum` stuck `queued` | Only one camera arrived | Upload the second, or let `ultimum_watchdog_job` fail it with an actionable error |
| Everything blocks for 15 min | `CELERY_TASK_ALWAYS_EAGER=1` with a worker running | Set it to 0 and restart |
| `POST /jobs` 422 on `preview_only` | `PUBLIC_BASE_URL` unset | Set it — Path B has no safe delivery without the served gallery |
| No email | SES sandbox / sender not verified | Verify the address or domain in SES |
| Gallery says "still being edited" | No videos *and* no photos in the job dir yet | Check Stages 8–9 |

---

## Sign-off — the flow is proven when

- [ ] Camera powered on, nothing else touched, and the customer on the load received one
      email with one gallery link.
- [ ] The gallery plays every video and shows every photo, on a phone.
- [ ] Path A downloads the 1080p master; Path B shows only the watermarked 720p, and the
      master stayed unreachable until `/unlock` — after which the same URL served it.
- [ ] `validation_report.json` and an A/V-sync-clean render exist for every deliverable.
- [ ] The jump is filed under `<date>/<instructor>/<customer>/` with per-file hashes.
- [ ] Only then switch to real customer emails.
