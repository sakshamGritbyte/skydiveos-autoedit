# Manual QA — spec-flight load master & upsell fan-out

**End-to-end from the SkydiveOS manifest screen.** One camera flyer on an open seat with no
assigned customer becomes ONE load-master edit, fanned out to everybody on the load: a locked
gallery per no-media customer, an upsell tile for each media buyer.

Owner tags: **[YOU]** = your click / your box / your decision. **[CMD]** = runnable, read-only
unless it says otherwise. **⛔ GATE** = stop, check, don't proceed past a red result.

The scenario below is **Load 17**: 4 tandem customers — 2 who bought media, 2 who bought
nothing — and Marc on the spare seat.

---

## Stage 0 — Prerequisites (do these first, they all fail silently)

**[YOU]** On the pipeline box, confirm these are set. Each one, unset, breaks the feature in a
way that looks like something else:

| Var | Why it's load-bearing here |
| --- | --- |
| `PUBLIC_BASE_URL` | Every child gallery is `preview_only`. `POST /jobs` **refuses to create one** without a served origin — you'd see the master render and no children appear. |
| `AUTO_DELIVER=1` | Otherwise the master waits at the review gate and never fans out. |
| `MONGO_URL`, `MONGO_DB` | The shared SkydiveOS DB — the match reads `loads`/`staffs`/`customers`. |
| `CAMERA_CLOCK_TZ` | GoPro writes local time labelled UTC. Unset ⇒ every clip's capture instant is off by the DZ's UTC offset and lands outside the flight window ⇒ **no spec match at all**. |
| `S3_BUCKET` | The fan-out uploads the master durably before offering it. |

**[CMD]** One command answers all of it plus the data prerequisites:

```bash
python scripts/check_match.py --readiness
```

⛔ **GATE 0a** — Read every WARNING. Specifically: `PUBLIC_BASE_URL is unset, so a SPEC FLIGHT
cannot fan out`, and any staff without `goproSerial`. Fix before continuing.

**[YOU]** In SkydiveOS: **System Config → Media Pricing** (the "paid-media unlock + gallery
upsells" card). Add a row:

- key `load_video` (lowercase; the field enforces `^[a-z][a-z0-9_-]{0,31}$`)
- price **in dollars** — it is stored as integer cents

⛔ **GATE 0b** — Without this row the buyer's tile *renders* but checkout is **rejected**
(`resolvePriceCents` fails loudly rather than selling at $0). That's the expected behaviour,
so don't read a rejected checkout as a bug later — set the price now.

**[YOU]** Confirm the flyer (Marc) is eligible for the picker. The dialog only lists staff who
are active, **have a `goproSerial`**, and have either `cameraCapabilities.outside === true` or
the `camera-flyer` role. A flyer missing the serial simply won't appear in the list.

### Stage 0c — state of this box as of 2026-08-10 (checked, not assumed)

The env is ready; **the shared DB is not.** Two things block the run today:

```
$ python scripts/check_match.py --readiness      → exit 1
staff            : 4
  WITHOUT goproSerial: 4 — their footage can never be matched
      (none) Owner Admin / Tom Rivera / Alex Morgan / Shubham Singh
paired cameras   : 0 active []
loads            : 1
```

```
$ python scripts/check_match.py --day 2026-08-07     # the only day with a load
load 12:12  status=planned
    handcam  — no camera serial on this staff member, skipped
    outside  — no camera serial on this staff member, skipped
    spec flight → 0 locked galleries + 1 upsell tile (1 on the manifest)
```

Env vars that ARE fine: `PUBLIC_BASE_URL=http://localhost:8000`, `AUTO_DELIVER=True`,
`S3_BUCKET=skydivingoss`, `CAMERA_CLOCK_TZ=America/Toronto`.

**[YOU]** So before Stage 1, two data fixes:

1. Set `staffs.goproSerial` on **at least the flyer** — keep the FULL printed serial
   (`C3504224544313`); the matcher suffix-matches the short BLE id (`4313`) against it. With
   zero serials nothing can match, spec flight or not.
2. Manifest a load with a usable mix: **≥2 jumpers with no media** and **≥1 media buyer**. The
   single existing load has one jumper who bought media, so a spec flight on it would produce
   *0 children and 1 tile* — that exercises the tile path but not the children, and not the
   unlock-isolation test (6.2), which is the assertion that matters most.

Re-run `--readiness` until it exits **0**, then continue.

---

## Stage 1 — Pre-flight, read-only, no camera needed

**[CMD]** What the matcher *would* decide for today's loads, and what a spec flight on each
would produce:

```bash
python scripts/check_match.py --day $(date +%F)
```

Per load you now get a line like:

```
    spec flight → 2 locked galleries + 2 upsell tiles (4 on the manifest)
```

⛔ **GATE 1** — That line is the "is the seat worth it?" arithmetic *and* your expected result
for Stage 5. Write down the two numbers for Load 17. Exit code non-zero = a FAILED jumper
match; fix that first, it's unrelated to spec flights but it will confuse the run.

---

## Stage 2 — Manifest the load (frontend)

**[YOU]** SkydiveOS → **Manifest**.

1. Create/open **Load 17**. Set a `departureTime` **near now** — the match window is
   `departure − 30 min … departure + 150 min` in **dropzone-local wall clock**.
2. Add 4 tandem jumpers: two with a media package (e.g. selfie), two with **none**.
3. Leave a seat free.

**Expected:** once the load is `open` / `check-in-open` / `boarding`, has ≥3 jumpers without
media and ≥1 open seat, an amber hint chip appears on the load card:

> 📷 3 jumpers without media — consider a spec flyer

(With only 2 no-media jumpers the chip won't show — that's the `SPEC_HINT_MIN_NO_MEDIA = 3`
threshold, not a bug. The slot still works.)

4. Click **`+ Camera flyer (spec)`** on the load card → pick **Marc** → save.

**Expected:** toast *"Spec camera flyer assigned"*; the card now shows Marc's name in **purple**
with a **`SPEC`** badge and an `×`. Hovering the name shows `GoPro <serial>`.

**[YOU]** Negative check, 10 seconds: try assigning Marc as a jumper's `assignedCameraman` on
this same load.

⛔ **GATE 2** — It must be **refused** (one person, one role per load — the BUG-340 sweep). If
it's allowed, stop: that person would produce both a customer's job and a load master.

---

## Stage 3 — The jump, and getting footage in

The load must be in a **matchable status** when the footage arrives: `planned`, `closed`, or
`landed`. This is the natural flow (assign the flyer while the load is still editable → it
departs → lands), but a load parked in `boarding` / `gate-closed` / `in-air` **will not match**
and the clip is flagged instead. If nothing matches later, check the status first.

Pick one path:

**Path A — real jump today (the real test).** Fly it, then put Marc's card in the ingest
machine's reader.

```bash
bash scripts/run_sdcard_stack.sh   # worker + bridge + API, CAMERA_SCANNER=sdcard AUTO_DELIVER=1
```

**[CMD]** While it pulls, watch the card:

```bash
curl -s -H "Authorization: Bearer $AUTO_EDIT_API_KEY" localhost:8000/ingest/cards | jq
```

Wait for `"state": "safe_to_remove"` before pulling the card out.

**Path B — old footage, load manifested for today.** Prefer manifesting the load for the
footage's *real* date; only if you can't, re-stamp copies:

```bash
python scripts/restamp_footage.py --at "$(date +%FT%H:%M)" \
  --out-dir /tmp/load17 /path/to/marc/GX*.MP4
```

Then feed them as Path A (copy into the card mount) or drive the API directly.

⛔ **GATE 3** — The footage **must contain a real freefall segment**. The fan-out refuses a
master with no `freefall` scene (that guard is what stops footage shot on the ground between
loads being sold to a load that never flew). Ground-only clips are a Stage 7 negative test, not
a happy path.

---

## Stage 4 — Watch the seams

**[CMD]** The bridge log line proving the spec branch fired (not a flagged clip):

```
clip raw/…/GX010099.MP4 -> load 17 SPEC FLIGHT (4 on the manifest) (serial); 1 clip(s) pending, job in 900s
```

The **900 s** settle window is deliberate — it must exceed the gap between a jump's clip
notifications. For a test cycle, and **only** on a laptop:

```bash
python scripts/skydiveos_bridge.py --dev-debounce 20   # logs a warning banner the whole time
```

**[CMD]** Then the master appears:

```bash
curl -s -H "Authorization: Bearer $AUTO_EDIT_API_KEY" localhost:8000/jobs \
  | jq '.jobs[] | select(.job_kind=="load_master") | {job_id, load_label, package, entitlement, status, media_state}'
```

⛔ **GATE 4** — Expect exactly **one** master: `package: "video_only"`,
`entitlement: "preview_only"`, `load_label: "Load 17"`. **Two masters = the reserve/CLAIM path
is broken** — stop and say so; two masters means two renders and two sets of galleries to the
same customers.

---

## Stage 5 — The fan-out

**[CMD]** Once the master reaches `delivered`:

```bash
curl -s -H "Authorization: Bearer $AUTO_EDIT_API_KEY" localhost:8000/jobs \
  | jq '[.jobs[] | select(.load_id!=null) | {job_kind, customer_name, entitlement, source_job_id, jumper_index}]'
```

⛔ **GATE 5** — Against the numbers from GATE 1:

- **2 `load_child`** jobs, one per no-media customer, each `preview_only` with
  `source_job_id` = the master
- **2 `jump`** jobs (the buyers) whose `source_job_id` is now the master, **status unchanged**
  and `outputs` still their own
- **no child for a buyer** — a buyer with a child means they'd get two links

**[YOU]** Check the mailbox: **exactly 2 new emails** (Priya, Kevin). The buyers must get
**nothing new** — their gallery already exists and grew a tile in place.

---

## Stage 6 — The four assertions that actually matter

**[CMD]** Get the child links (`gallery_token` is never in list output — read it from the job
file or the delivery log):

```bash
jq -r '.gallery_token' jobs/<child_job_id>/job.json
```

### 6.1 A locked child streams the WATERMARKED master, not the clean one

```bash
curl -s -o /tmp/priya.mp4 -D- "http://localhost:8000/j/<priya_token>/media/full_video" | head -1
ffprobe -v error -show_entries stream=width,height /tmp/priya.mp4
```

**Expect:** 200, **720p**, visible watermark. The child owns no files — those bytes came from
the master's dir, chosen by *her* entitlement.

### 6.2 Unlock is isolated — the whole point

```bash
curl -s -X POST -H "Authorization: Bearer $AUTO_EDIT_API_KEY" \
  -H "X-Role: admin" -H "Content-Type: application/json" \
  -d '{"payment_reference":"qa-load17-priya"}' \
  localhost:8000/jobs/<priya_child_id>/unlock | jq '{entitlement, media_state}'
```

⛔ **GATE 6.2** — Then re-fetch **both**:

- Priya's `/media/full_video` → **1080p clean**
- Kevin's `/media/full_video` → **still 720p watermarked**
- the master's own `entitlement` → **still `preview_only`**

One unlock leaking to the other child is the worst failure this feature can have. If Kevin's
bytes changed, stop.

### 6.3 The buyer's page: one link, one extra tile

**[YOU]** Open a buyer's existing gallery.

**Expect:** their own videos unchanged as the main players, plus a tile in "Add to your day":
**"Your Load 17 aerial video"**. No second email, no second page.

Then buy it (or simulate the capture):

```bash
curl -s -X POST -H "Authorization: Bearer $AUTO_EDIT_API_KEY" \
  -H "X-Role: admin" -H "Content-Type: application/json" \
  -d '{"payment_reference":"qa-load17-daniel","item":"load_video"}' \
  localhost:8000/jobs/<buyer_job_id>/unlock | jq '.addons'
```

**Expect:** the tile is replaced by a **"Load Video"** section badged `FROM THE AIR`, captioned
*"Your jump day from the air"*, downloadable, serving the master's **clean** cut. Reloading isn't
needed — the page polls and re-renders itself.

### 6.4 The honest promise

**[YOU]** Watch a child's video through.

⛔ **GATE 6.4** — It must contain **no personal freefall of that customer** (Marc exited with
nobody; physics). The page must say *"your jump day"*, never *"your jump"*, and the hero label
must read **`Tandem · Jump Day`** — not a media product they didn't buy.

---

## Stage 7 — Negative tests (each one is a guard we shipped on purpose)

| # | Do this | Must happen |
| --- | --- | --- |
| 7.1 | Ground-only footage (no freefall) through the same flow | Master job **fails** with an actionable "no freefall scene" error; **zero** children created |
| 7.2 | A flyer who IS a jumper's `assignedCameraman` on that load | **No master.** His footage goes to that customer only, exactly as today (`NotSpecFlight`) |
| 7.3 | Footage captured hours outside any departure window | Clip **flagged**, no job. Inspect: `python scripts/unflag_bridge_key.py` (bare = read-only list of what's flagged and why) |
| 7.4 | Try to attach footage to a child job | **409**, "load_child … takes no footage of its own" |
| 7.5 | Re-notify a clip after the master exists | No second master, no second child, no second email |

---

## Stage 8 — Retention (do this before you trust the box unattended)

**[CMD]** With a child still locked:

```bash
python scripts/prune_jobs.py --dry-run
```

⛔ **GATE 8** — The log must say the master is **kept**:

```
job <master>: load master with 4 gallery(ies) streaming it (1 still locked) — keeping renders and previews
```

A locked child's only watchable media is the master's *local* `preview_*.mp4`, and the gallery
deliberately refuses a presigned-S3 fallback for a locked job. Pruning those blacks out a live
paywall — customer clicks their link, gets nothing, can't buy.

---

## What to record

For the sign-off, capture: the GATE 1 prediction vs the GATE 5 actual; the 6.2 before/after
resolutions for **both** children; a screenshot of the buyer's tile and of the fulfilled Load
Video section; and the GATE 8 log line. Those five artefacts are the feature.

## Known-good failure shapes (don't chase these)

- **Tile visible, checkout rejected** → `load_video` price not set (Stage 0b). By design.
- **No hint chip on the load card** → fewer than 3 no-media jumpers. Cosmetic only.
- **Master renders, no children** → `PUBLIC_BASE_URL` unset, or the master's own render failed
  its preview pass. Check the job's `error`.
- **Clip flagged, no job** → load status not matchable (`boarding`/`in-air`), or
  `CAMERA_CLOCK_TZ` unset shifting the window.
