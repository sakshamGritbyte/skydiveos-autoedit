# Automation test — GoPro → auto-edit → customer email (today)

A step-by-step you can run **today** on the dropzone Mac to prove the full hands-off
flow end to end: power on the camera → footage is pulled automatically → matched to a
booking → edited → the customer is emailed a gallery link. No manual upload, no editing
by hand.

**Your situation:** Greg's GoPro is paired, but the clips on it were **filmed on an
earlier date**, not today. That one fact drives the whole setup below — read “The one
thing that matters” first.

---

## The one thing that matters: the load's date must equal the footage's date

The system matches a clip to a booking by **camera + the moment it was filmed**
(`captured_at`) landing inside a manifested load's flight window **on the same day**.
It deliberately refuses to guess — that's what stops Customer A's video going to
Customer B.

So because your footage is from a previous date, you must **manifest the test load for
that same earlier date** (SkydiveOS supports retroactive manifesting). Do **not**
manifest it for today — today's load won't match yesterday's footage and the job will
be refused-and-flagged, not delivered.

> If you truly cannot back-date a load, use the **Fallback** at the bottom (re-stamp the
> footage to today). The recommended path keeps the real camera pull and alters nothing.

---

## Prerequisites (verify once, ~2 min)

Run from the repo root (`~/.../skydiveos-autoedit`):

```bash
uv run --no-sync python scripts/check_match.py --readiness
```
Confirm in the output:
- **camera clock tz : America/Toronto** (not “unset”).
- Greg appears under **with goproSerial** → `C3504224544313  Gregory Perrimond`.
- **paired cameras** includes **`4313`** (the short id the Bluetooth scan actually sees —
  a GoPro advertises only its trailing digits). Both `C3504224544313` and `4313` being
  present is correct.

Then check the plumbing is alive:
```bash
# S3 reachable
uv run --no-sync python -c "import boto3,os; from dotenv import load_dotenv; load_dotenv('.env'); \
b=os.environ['S3_BUCKET']; boto3.client('s3').head_bucket(Bucket=b); print('S3 OK:',b)"

# .env has the automatic flags on
grep -E '^(ENABLE_AUTO_DISCOVERY|CAMERA_SCANNER|AUTO_DELIVER)=' .env
#   expect: ENABLE_AUTO_DISCOVERY=1   CAMERA_SCANNER=ble   AUTO_DELIVER=1
```

---

## Step 1 — Find the footage's real date

Point the checker at one of the actual master files on the card (read-only, uses the
file's own capture time):

```bash
uv run --no-sync python scripts/check_match.py --serial C3504224544313 --file "/path/to/GXnnnnnn.MP4"
```
Note the date it prints for the clip. Alternatively, list every load Greg is already on:
```bash
uv run --no-sync python scripts/check_match.py --serial C3504224544313
```
Pick the date your footage was shot — call it **`<JUMP-DATE>`** (e.g. `2026-07-16`).

## Step 2 — Manifest the test load for `<JUMP-DATE>` in SkydiveOS

In the SkydiveOS web app, create/retroactively manifest a load on **`<JUMP-DATE>`** with:

| Field | Set it to |
|---|---|
| **Instructor** | **Gregory Perrimond** (his camera is the one paired) |
| **Customer** | a test customer whose **email is an inbox you control** (so nobody real is emailed) |
| **Media add-on** | **Video + Photos** (this test targets the **selfie** package — Greg's handcam) |
| **Video type** | **Inside** (handcam). *(Inside → selfie, Outside → external.)* |

> Only **one** camera is paired (Greg's), so test a **single-camera** package
> (selfie / external / video / photos). **Do not** pick the **Ultimate** (`ultimum`)
> product today — it needs two paired cameras and will time-out waiting for the second.

## Step 3 — Pre-flight the match (must say “deliverable”)

```bash
uv run --no-sync python scripts/check_match.py --day <JUMP-DATE>
```
You want a line for Greg's camera reading **`deliverable`** and the summary
**`deliverable ≥1, … FAILED 0`**. If it says `FAILED` or `no-media`, fix the load in
Step 2 (wrong instructor, no media add-on, or wrong date) before touching the camera.
This is the go/no-go gate — green here means the automation will complete.

## Step 4 — Make sure the ingest service is running

```bash
launchctl list | grep com.skydiveos.ingest      # shows a PID if running
# not running? load it:
bash deploy/mac/load-service.sh
tail -f logs/ingest.err.log                      # leave this open to watch live
```
In the log you should see `camera auto-discovery started`.

## Step 5 — Connect the GoPro and let it run

1. Power the GoPro on and keep it near the Mac (Bluetooth wake, then Wi-Fi pull).
2. Within ~30 s the log shows: `Camera 4313 discovered, pull enqueued` → files
   downloading → `uploading … to S3 + notifying SkydiveOS`.
3. You'll then see the load-derived role log
   (`role resolved from load …`), the job move `queued → processing → … → delivered`,
   and finally the delivery email go out.

Do **nothing** else — this is the whole point of the test. It runs unattended.

## Step 6 — Verify delivery

- **Email:** the test inbox from Step 2 receives **one** email with a single **gallery
  link** — open it: all the package's videos play inline and the photos show in a grid,
  branded for the dropzone.
- **SkydiveOS:** the job/booking shows **delivered**.
- **Logs:** `grep -E "delivered|gallery email sent" logs/ingest.err.log` shows the
  hand-off completed.

That is the full automatic flow proven: **camera → pull → match → edit → customer inbox**,
with no human step.

---

## Fallback — if you cannot back-date a load

Manifest the load for **today** instead, and re-stamp **copies** of the footage to a
time in today's window (this preserves the GoPro telemetry; the originals are untouched
and can only be re-uploaded, not put back on the camera — so this tests everything
*except* the physical Bluetooth pull):

```bash
# 1. Re-stamp copies to today at, say, 13:05 dropzone-local
uv run --no-sync python scripts/restamp_footage.py --at "13:05" --out-dir /tmp/today "/path/to/GXnnnnnn.MP4"

# 2. Manifest today's load (Greg + test customer + Video+Photos / Inside), then pre-flight
uv run --no-sync python scripts/check_match.py --day <TODAY>

# 3. Drive one jump through the live stack over HTTP (creates the job, uploads, polls)
uv run --no-sync python scripts/demo_full_auto.py \
  --package selfie --instructor "Gregory Perrimond" \
  --customer "Test Customer" --email you@yourinbox.com /tmp/today/GXnnnnnn.MP4
```
The customer email arrives exactly as in Step 6.

---

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `check_match --day` says **FAILED** | No load that day for Greg, or two jumpers tie → fix/clean the load. It refuses rather than mis-deliver. |
| `check_match --day` says **no-media** | The booking has no video/photo add-on → add one in Step 2. |
| Camera never appears in the log | Registry has the short id? (`4313` must be in *paired cameras*.) GoPro on and in range? `CAMERA_SCANNER=ble`? |
| Job reaches `delivered` but no email | Check `customer_email` on the booking and the SMTP settings in `.env`; the link is still in the SkydiveOS status callback. |
| Job stuck waiting for a 2nd camera | The booking is set to **Ultimate** — use a single-camera package today (only Greg's camera is paired). |

**Safety for a live DB:** use a **test customer email you own** in Step 2 — delivery is
fully automatic (`AUTO_DELIVER=1`), so whatever email is on the booking is the address
that actually receives the video.
