# Release 2026-09-18 — "safe to remove" now means the card is finished

Commits `dbee838`, `2b3d564`, `15ecb69` on `main`.

Three changes ship together, but they land on **different hosts** and only one of them
changes runtime behaviour. Read §1 to see which boxes you actually have to touch.

---

## 1. What changes, and where

| Change | Affects | Runtime effect |
|---|---|---|
| `fix(ingest)` — the `uploading` card state | **SD-card ingest boxes only** (`CAMERA_SCANNER=sdcard`) | Behaviour change — see §2 |
| `fix(scripts)` — `restamp_footage.py` gpmd mapping | Wherever you run that script by hand | None until you run it |
| `docs(integration)` — deliverable-import contract | SkydiveOS repo, not this one | None here |

**The cloud tenants are inert for this release.** They run with
`ENABLE_AUTO_DISCOVERY=0` and no SD-card scanner, so `GET /ingest/cards` is empty there
and no code path in the first commit is reachable. Pull and rebuild them on the normal
cadence; there is nothing to verify beyond the service coming back up.

**The box that matters is the dropzone SD-card Mac** (the Nouvel Air / South Shore
ingest machine) — that is the only place the banner is read and the only place the
behaviour changes.

---

## 2. The behaviour change, in one paragraph

Before: `safe_to_remove` fired the moment the copy loop finished. The reasoning was that
the S3 upload and the SkydiveOS notify both run from the *staged* copy and never touch the
card again. That is true of the upload and **false of the retention sweep**, which runs at
the start of the *next* pull and is the only thing that ever frees the card. So the
operator took the card out on the banner's word and carried it back to the camera still
full — `DELETE_AFTER_TRANSFER` effectively never ran (2026-09-18, nine clips), and a full
card silently stops recording mid-day.

After: the end of every pull re-reads the card and asks the ledger what is still owed.
Still owing → the row reads **`uploading`** with `pending_files` counting down, and the
next discovery tick's re-pull — which performs the sweep — asks again. Owing nothing →
`safe_to_remove`.

### Two consequences you should expect to see

1. **The banner is slower now, by design.** At the end of the *first* pull nothing has
   been uploaded yet (the S3 hand-off runs after the pull), so a freshly inserted card
   will essentially always pass through `uploading`. It clears on a later discovery tick
   once the uploads land — so budget roughly `DISCOVERY_INTERVAL_SECONDS` (default 30 s)
   plus the actual S3 upload time before the green banner appears. This is not a hang.
2. **It applies even with `DELETE_AFTER_TRANSFER` off.** With cleanup off there is no
   sweep to wait for, but the banner still waits for *S3 confirmation* of every clip.
   That is deliberate — but see the pre-flight check below, because it is the one way
   this can strand an operator.

---

## 3. Pre-flight — the one check that matters

**On the ingest Mac, confirm the S3 hand-off is actually configured.**

The retention ledger is written only when discovery's uploader returns an S3 key, and
that uploader is built only when `S3_BUCKET` **and** `SKYDIVEOS_API_BASE` are both set. On
a box where they are not, no clip is ever confirmed, so every card would sit in
`uploading` forever and the operator would never get a green banner.

```bash
cd ~/skydiveos-autoedit                     # the ingest checkout
grep -E '^(S3_BUCKET|SKYDIVEOS_API_BASE|CAMERA_SCANNER|DELETE_AFTER_TRANSFER)=' .env
```

- Both `S3_BUCKET` and `SKYDIVEOS_API_BASE` present → you are fine, deploy.
- Either missing → **do not deploy with the default on.** Either fix the config first, or
  set `CARD_SAFE_REQUIRES_UPLOAD=0` in that box's `.env` to keep the old banner.

> Note the S3 account migration (2026-09-18): buckets are now per-tenant in account
> `878013573203` / `ca-central-1`. If this box still points at the dead `skydivingoss`
> bucket, uploads are failing silently and this release will surface that as a card that
> never goes green. That is the check doing its job, not a regression — but fix the
> bucket, don't just disable the gate.

No new env var is *required*: `CARD_SAFE_REQUIRES_UPLOAD` defaults to `1` in code. Adding
it to `.env` explicitly is only worth it if you want the old behaviour (`0`).

---

## 4. Deploy

### 4a. The SD-card ingest Mac (the one that matters)

```bash
cd ~/skydiveos-autoedit
git pull origin main

# Do NOT run `uv sync` here — it strips the separately-installed Open GoPro SDK back
# out. There are no dependency changes in this release, so nothing to sync.

# Config is read once at process start, so the service must be restarted, not reloaded.
bash deploy/mac/load-service.sh
```

Then confirm it came back:

```bash
launchctl list | grep com.skydiveos.ingest      # PID present, last exit code 0
tail -f logs/ingest.err.log
```

### 4b. The cloud tenants (routine, nothing to verify)

Per the usual multi-tenant order — **the hub checkout pulls first**, because the tenant
checkouts pull from it, then each tenant pulls and rebuilds:

```bash
# 1. hub / prod checkout
cd <hub checkout> && git pull origin main

# 2. each tenant
cd <tenant checkout> && git pull && docker compose up -d --build
```

`docker compose restart` is not enough if you ever do add `CARD_SAFE_REQUIRES_UPLOAD` to a
tenant `.env` — a restart does not re-read `env_file`. (You won't need to; the flag is
inert there.)

No Celery task was added or renamed in this release, so the usual "restart the worker to
teach it the new task" step does not apply.

---

## 5. Verify on the ingest Mac

Insert a card and watch the state machine. The operator display is the easiest way:

```bash
python scripts/watch_cards.py                  # or --once for a single snapshot
```

Or read the endpoint directly:

```bash
curl -s -H "Authorization: Bearer $AUTO_EDIT_API_KEY" localhost:8000/ingest/cards | jq
```

**What good looks like:**

1. `detected` → `pulling` (progress bar moves) → **`uploading`** with
   `pending_files` > 0 and the banner reading `TRANSFERRING TO CLOUD — DO NOT REMOVE`.
2. Within a discovery tick or two, `pending_files` counts down and the state flips to
   `safe_to_remove`.
3. With `DELETE_AFTER_TRANSFER=1`, re-insert that card and confirm the clips are actually
   gone from it — that is the whole point of the release, and it is the step that was
   silently not happening before.

**What tells you §3 was skipped:** the card parks in `uploading` and `pending_files`
never decreases across several ticks. That means nothing is reaching S3. Check
`logs/ingest.err.log` for upload failures before touching the flag.

---

## 6. Rollback

Cheapest first — no redeploy needed:

```bash
# on the ingest Mac
echo 'CARD_SAFE_REQUIRES_UPLOAD=0' >> .env
bash deploy/mac/load-service.sh
```

That restores the pre-release banner wholesale (safe as soon as the copy loop ends) while
leaving everything else in place. A full `git revert dbee838` is only worth it if the new
code is misbehaving in some way the flag doesn't cover.

The other two commits carry no runtime behaviour and nothing to roll back.

---

## 7. The SkydiveOS side

Two items, both in the SkydiveOS repo, neither blocking this deploy:

- **The card banner consumer gains a state.** `GET /api/media/ingest-cards` can now
  return `state: "uploading"` and a new `pending_files` field. The failure mode is
  safe by construction — an unrecognised state simply isn't `safe_to_remove`, so an
  un-updated frontend shows "not safe yet" rather than a wrong green banner, which is
  the direction you want to fail in. Worth teaching it the state (and the countdown)
  so the operator sees *why* they're waiting. Confirm the receiver doesn't reject the
  snapshot outright over the new field.
- **`SKYDIVEOS_INTEGRATION.md` gained a section** on the deliverable import: a
  `delivered` callback does not move any file into SkydiveOS, the deliverables must be
  pulled from `GET /jobs/{id}/deliverables` under a durable server-side record, and the
  sweep has to cover camera/SD-card jumps that never touch the manual edit screen. That
  documents the 2026-09-18 gap and the `autoEditImportService` fix; no action in this
  repo.

---

## 8. Test / lint state at the time of this release

- `pytest tests/` — **1250 passed, 13 skipped**.
- `ruff` and `mypy` — unchanged from `HEAD` (15 ruff `E501`s and 1 `unused-ignore` in
  `ingest/camera.py` pre-exist and were verified identical before and after). This
  release introduces no new lint or type errors, and does not fix the existing ones.
