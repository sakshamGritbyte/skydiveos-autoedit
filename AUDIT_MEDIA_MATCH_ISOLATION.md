# Audit — media matching, ingestion, job creation: cross-customer risk

**Date:** 2026-08-11 · **Scope:** camera/SD card → clip discovery → match → job → edit → delivery
**Question:** can a clip be attached to the wrong customer, wrong load, or wrong edit?
**Method:** read of the actual code (no assumptions), plus empirical runs of the real
`ingest.match.select_match` against fabricated candidate sets for every scenario below.
**Status: AUDIT ONLY — no production code changed.**

---

## 0. Corrections to the brief

Five assumptions in the request don't match the implementation. They matter because
three of the scenarios are phrased in terms of them.

1. **There is no "intro" concept anywhere in matching.** Clips are matched **one at a
   time**, each on its own `captured_at`. Nothing knows that one clip is an interview and
   another is the jump. Scene labels (`intro_interview`, `boarding`, `freefall`) are
   assigned *after* the job exists, from telemetry + clip position, and
   [AUDIT_SCENE_LABELS.md](AUDIT_SCENE_LABELS.md) already establishes they cannot carry
   identity. So "intro + jump become one job" really means: *every clip of that jump must
   independently resolve to the same `(load, jumper)`, and then settle into one job.* Any
   clip that resolves differently silently leaves the group.
2. **`jumper_index` is not an identity — it is a manifest array position**
   ([ingest/match.py:672](ingest/match.py#L672), `for idx, jumper in enumerate(...)`).
   Remove a jumper from a load and every later index shifts, so a stored `jumper_index`
   can come to name a **different customer**. The code knows this in one place
   ([api/tasks.py:411-443](api/tasks.py#L411-L443) joins on `booking_id` *or* the
   positional key) and not in others.
3. **Grouping by `(load_id, jumper_index)` is the local bridge's in-memory key only**
   ([scripts/skydiveos_bridge.py:262](scripts/skydiveos_bridge.py#L262)). In production
   *SkydiveOS* owns match + job creation; the bridge is the executable reference. There
   is a **second, independent** grouping layer inside auto-edit (the raw-clip settle
   window, [api/tasks.py:773](api/tasks.py#L773)) keyed by `job_id`. Duplicate-protection
   conclusions differ per layer.
4. **"Cancelled / scrubbed" is not a state the code understands.** The only filter is
   `status ∈ {planned, closed, landed, completed}`
   ([ingest/match.py:87](ingest/match.py#L87)). A load that was abandoned but left
   `planned` is **fully matchable** — which is precisely the hazard in Scenarios A, B, C
   and H. "Scrubbed" must mean `status: cancelled` (or the jumper removed) or it means
   nothing.
5. **Instructor identity is not the match key; *staff* identity is.** The key is
   `staffs._id` (from the filmed QR) or `staffs.goproSerial` (from the camera). Whether
   that person is the `instructor` or the `assignedCameraman` is *derived* from the
   matched jumper slot ([ingest/match.py:673-677](ingest/match.py#L673-L677)). This is why
   Scenario G ("customer changes instructor") behaves the way it does.

One more, not an error but load-bearing: **the same clip belonging to two jobs is a
designed behaviour**, not a bug — a shared-marked clip from an assigned cameraman is
routed twice, into the customer's job and into the load master
([scripts/skydiveos_bridge.py:347-362](scripts/skydiveos_bridge.py#L347-L362)).

---

## 1. The complete flow

### 1.1 Card → staged clip

| Step | Code | Identity established |
|---|---|---|
| Card detected (`CAMERA_SCANNER=sdcard`) | `ingest/sdcard.py` `find_cards` | `camera_id` = last 4 digits of serial in `MISC/version.txt`, else `sd-<volume-label>` |
| Sweep confirmed files off the card | [ingest/pull.py:124](ingest/pull.py#L124) `_sweep_card` → `retention.deletable()` | by **bare filename** in the per-camera ledger |
| Stage each clip | [ingest/pull.py:57](ingest/pull.py#L57) `_pull_one` | `raw-storage/_camera-staging/<camera_id>/<date>/<FILENAME>` + `<stem>.ingest.json` |
| Emit `ready_for_processing` | `ingest/events.py` | one event per **newly** staged clip (`is_complete` → skip, no re-emit) |

### 1.2 Staged clip → notification

| Step | Code | Notes |
|---|---|---|
| QR attribution | [ingest/qr.py](ingest/qr.py) `qr_identity_resolver` → `QrSessionIndex` | card-level: one staff id on the card claims every clip; ≥2 staff infers marker direction from layout. Marker clips are never notified. |
| Role hint override | `matcher_role_resolver` → `FootageMatcher.resolve(...).role` | per-jump role beats the registry's static hint |
| Capture time | [ingest/discovery.py:147](ingest/discovery.py#L147) `_probe_capture_time` | MP4 container `creation_time`, reinterpreted through `CAMERA_CLOCK_TZ` into **true UTC**. Unreadable → omitted. |
| Upload + notify | [ingest/discovery.py:208](ingest/discovery.py#L208) `s3_notify_uploader` | `PUT s3://$S3_BUCKET/raw/{camera_id}/{FILENAME}` then `POST {SKYDIVEOS}/api/media/raw-upload` with `{s3_key, camera_id, captured_at?, staff_id?, camera_role?, shared?}` |
| Retention ledger | `retention.record_uploaded` | the returned S3 key **is** the authority to delete from the card later |

### 1.3 Notification → job (the bridge / SkydiveOS)

```
notify → dedupe on s3_key (handled|flagged)          bridge.py:286
       → require captured_at                         bridge.py:289-291
       → staff_id ? resolve_for_staff : resolve      bridge.py:295-298
           ├─ MatchResult  → key (load_id, jumper_index)
           ├─ NoBookingMatch → try spec-flight load → key (load_id, "load")
           └─ other FootageMatchError → FLAG (terminal), HTTP 200
       → package/email sanity gates                  bridge.py:332-337
       → append to PendingJump, (re)arm 900 s timer  bridge.py:399-433
       → on settle: POST /jobs, then POST /jobs/{id}/upload with all clips
       → record every s3_key → job_id in handled     bridge.py:568-570
```

### 1.4 Job → customer

```
POST /jobs           → new uuid job, gallery_token minted        app.py:897-933
POST /jobs/{id}/upload (bytes) → stage raw/, archive, enqueue    app.py:1028-1086
   (s3_key path instead) → per-clip download + settle window     tasks.py:657-751
                         → _dispatch_processing (exactly-once)   tasks.py:754-770
process_selfie_package → scenes → scores → EDL → validate → render
   → _render_previews → ready → _maybe_auto_deliver              tasks.py:177-199
   → deliver_job → S3 + presign (unless locked) + email gallery  tasks.py:313-363
```

---

## 2. The matching logic, as it actually is

**Query** ([ingest/match.py:658-665](ingest/match.py#L658-L665)): every load where this
staff is *any* jumper's `instructor` or `assignedCameraman`. **No date filter.** All of
history is the candidate pool; time is applied afterwards, in memory.

**Narrowing** ([ingest/match.py:364-407](ingest/match.py#L364-L407)):

```python
if len(candidates) == 1:            return candidates[0]      # ← no day check, no window check
same_day = [c for c in pool if c.business_day == captured_day]
narrowed = same_day or pool                                    # ← falls back to ALL DAYS
if len(narrowed) == 1 and not require_window: return narrowed  # ← no window check
in_window = [c for c in narrowed if _in_window(...)]           # dep−30min .. dep+150min
if len(in_window) <= 1:             return in_window
# >1: causal rule — the LATEST departure at or before the clip wins
```

Three properties follow, and they are the root of nearly every finding in this audit:

* **R1 — the lone candidate is never time-checked.** One candidate in the pool ⇒ matched,
  whatever the clip's timestamp, whatever day it was filmed.
* **R2 — the day filter is a preference, not a constraint.** `same_day or pool`: if
  nothing matches the capture day, *every other day* becomes eligible again.
* **R3 — the window is only consulted to break ties.** Nothing ever asserts "the chosen
  candidate's flight window actually contains this clip". `select_load` (spec flights)
  *does* assert it (`require_window=True`, [match.py:478-487](ingest/match.py#L478-L487))
  — the jumper path deliberately does not.

R1–R3 were each chosen for a good reason (documented in the module: a jumper predicate is
strong evidence; a staff member flies 4–5 loads a day so refusing overlaps would automate
only their first jump). The cost is that **the jumper-keyed path has no "I don't know"
answer once one candidate survives** — and `WINDOW_POST = 150 min` is wide enough that a
clip is inside 3–6 loads' windows at once.

**Other match inputs:**

* `_staff_for_camera` ([match.py:570-588](ingest/match.py#L570-L588)): exact `goproSerial`,
  else **regex suffix** `{camera_id}$`. Refuses on >1 hit (good). A 4-digit suffix is not
  guaranteed unique across a fleet, and a *short* id (`sd-1`) would be a very loose match.
* `businessDate`/`departureTime` are read with `.replace(tzinfo=None)`
  ([match.py:591-595](ingest/match.py#L591-L595)) — i.e. **the code assumes loads store
  DZ-local wall clock in a UTC-labelled field.** True today; a storage remediation to real
  UTC breaks matching by the whole UTC offset unless both repos change together.
* Zero candidates → `NoBookingMatch` → the bridge tries the spec-flight path → flag. Safe.
* Ambiguity within one departure instant → `AmbiguousMatch` → flag. Safe.

---

## 3. Scenarios A–O

Verified by running the real `select_match` (script:
`scratchpad/verify_match.py`; outputs quoted verbatim).

### A — Customer rescheduled (Load A cancelled, only Load B remains) ✅

```
A intro 10:15, only LoadB(14:00) candidate: -> OurCustomer (load B, idx 3)
```

1. **What the code does:** Load A is excluded by status, so the pool has one candidate;
   R1 accepts it without a time check. Both the 10:15 clip and the 14:0x clips resolve to
   `(LoadB, jumper 3)`, land in one `PendingJump`, and become **one job**.
2. **Matches:** correct customer, correct load.
3. **Correct?** Yes — the outcome is right.
4. **What could go wrong:** it is right *by accident of R1*, not by validation. The exact
   same code path is what produces finding 🔴-1 (Scenario F). And it only holds if the
   instructor has **no other customer that day** — see C.
5. **Code or data:** works, provided ops really cancels Load A (`status: cancelled`) or
   removes the jumper. Leaving it `planned` gives you B, not A.
6. **Test:** unit — one candidate, capture time 4 h before departure ⇒ matched; and a
   bridge-level test that both clips share one pending key.

### B — Stale manifestation (customer on BOTH loads) ⚠️

```
B intro 10:15, on LoadA(10:00)+LoadB(14:00): -> Ours@A (load A, idx 1)
B jump  14:05, on LoadA+LoadB:               -> Ours@B (load B, idx 3)
```

1. Two candidates ⇒ window narrowing splits them by time.
2. Intro → Load A slot; jump → Load B slot.
3. **No.** Two pending keys ⇒ **two jobs, two renders, two emails** to one customer.
4. Worse than "split": the Load-A job contains *only* the interview, and
   `_curated_freefall` stands in the first scene when there is no freefall
   ([api/selfie.py:1873-1878](api/selfie.py#L1873-L1878)) — so it renders and (under
   `AUTO_DELIVER`) **emails a "your video is ready" for a video that is just the
   interview**. Same failure class as the 2026-08-06 four-emails incident.
5. **Data/operational** primarily; the "render something anyway" behaviour is code.
6. **Test:** bridge test — same customer on two loads, two clips ⇒ assert ONE job (after
   the recommended fix) or assert the flag; plus a selfie-pipeline test that a
   freefall-less clip set does not silently produce a `freefall` deliverable.

### C — Multiple customers + busy instructor 🔴 **CRITICAL**

```
C intro 10:15 (X on 9:30, ours on 14:00): -> CustomerX (load X, idx 0)
```

1. Two candidates; 10:15 is inside the 9:30 load's window (`9:00 … 12:00`) and outside the
   14:00 one, so `in_window` has exactly one element and it is returned.
2. **Customer X's jump, on the 9:30 load.**
3. **No — this is a cross-customer assignment.**
4. Our customer's interview is ingested into **Customer X's job**. With `AUTO_DELIVER` on,
   X is emailed a gallery containing a stranger's footage; our customer's own job is short
   one clip. Nothing flags: from the matcher's point of view this was a clean, unambiguous
   single-candidate resolution.
5. **Code.** The data is fine — the customer genuinely was filmed at 10:15 and genuinely
   flew at 14:00. No manifest edit can express "this clip is not X's".
6. **Test:** the exact candidate set above ⇒ must NOT return X. Expected post-fix
   behaviour: refuse (flag) because 10:15 is in no window belonging to a load our
   *card session* is otherwise anchored to.

### D — Customers close together 🔴

```
D A-intro 10:10 (A dep 10:00, B dep 10:05): -> CustB (load LB, idx 0)
```

1. Both windows contain 10:10 ⇒ the causal rule takes the **latest departure at or
   before** the clip ⇒ 10:05.
2. Customer B.
3. **No.** A's clip goes to B.
4. Realistically this needs two loads ~5 min apart *with the same staff member on both*,
   which is rare for a tandem instructor (one tandem at a time) but normal for a
   **cameraman** flying back-to-back. If the two are jumpers on the *same* load with the
   same departure instant, the code correctly raises `AmbiguousMatch` and flags.
5. **Code** (the causal tie-break is a guess by design), with an operational contribution
   (loads minutes apart).
6. **Test:** two loads 5 min apart, clip between them ⇒ assert refuse-and-flag rather than
   "latest wins" when the gap is below a threshold.

### E — Same camera / same card, three customers ✅

1. Each clip is matched independently: `staff → loads → jumper` per `captured_at`. Clips
   never inherit a neighbour's match; grouping happens *after* matching, keyed by the
   match result. The job dir (`jobs/<job_id>/raw/`) is per job; the bridge downloads only
   that jump's keys.
2. Each clip → its own customer.
3. **Yes, safe** — *provided each clip's own timestamp lands in the right window*.
4. The isolation is only as good as per-clip matching, so C/D/N/O are the ways clips cross
   over. There is no additional cross-contamination mechanism at the file level.
5. n/a.
6. **Test:** three customers, three loads, nine clips ⇒ 3 jobs × 3 clips, no clip in two
   jobs (except a deliberate shared-marked clip).

### F — Yesterday's media still on the card 🔴 **CRITICAL** (two independent faults)

```
F yesterday clip 10:15 Aug10, only today's load in DB: -> TodaysCustomer (load T, idx 2)
F yesterday clip, BOTH days manifested:                -> YesterdayCust  (load Y, idx 2)
```

1. **Match:** if yesterday's load is still matchable, the day filter works (second line —
   correct). If yesterday's load was deleted, archived, or moved out of
   `_MATCHABLE_STATUSES`, R2 puts *today's* load back in the pool and R1/R3 accept it with
   no window check ⇒ **yesterday's footage becomes today's customer's job.**
2. Today's customer (wrong).
3. **No.**
4. Two more faults on this path, independent of matching:
   * **S3 key collision.** The key is `raw/{camera_id}/{FILENAME}`
     ([ingest/discovery.py:264](ingest/discovery.py#L264)) — **no date component**. GoPro
     filenames restart at `GX010001.MP4` on a formatted/replaced card, and two unlabeled
     cards both resolve to `sd-NO-NAME`. So a new clip **overwrites** a previous
     customer's object, and the bridge's dedupe (keyed on `s3_key`,
     [bridge:286](scripts/skydiveos_bridge.py#L286)) then reports **`duplicate` and drops
     the new clip entirely** — silent footage loss for today's customer.
   * **Retention ledger collision.** `deletable()` matches on **bare filename**
     ([ingest/retention.py:101-123](ingest/retention.py#L101-L123)). A ledger entry from a
     *different* card with the same filename authorises deleting today's clip off the card
     **before it was ever uploaded** (only `DELETE_AFTER_TRANSFER=1` + 24 h grace gate
     this).
5. **Both code and data.** Matching: code (R1/R2). Keys/ledger: code.
6. **Tests:** (a) capture day ∉ any candidate's `business_day` ⇒ refuse; (b) two cards,
   same filename, same camera id ⇒ distinct S3 keys; (c) ledger entry for `GX010001.MP4`
   must not authorise deleting a *different* `GX010001.MP4` (size/mtime or key-scoped).

### G — Customer changes instructor mid-day ⚠️

1. Instructor A's card is matched against **A's** jumper slots. Our customer's slot now
   names B, so A's interview has no candidate for that customer. Outcome depends on A's
   day: if A has other customers, the clip resolves to **whichever of A's slots the
   window fits** (a wrong customer — the C mechanism); if A has none, `NoBookingMatch` →
   flag → footage stranded.
2. One of A's other customers, or nothing.
3. **No** (or "safe but lost" in the no-other-customer case).
4. There is no representation of "A filmed a clip for a customer he did not fly". The
   footage cannot reach the right job by any code path.
5. **Code + operational.** A manifest can't express it; the practical rule is that the
   instructor who ends up flying the jump films the interview.
6. **Test:** A's clip while our customer's slot names B, A has one other customer ⇒ assert
   flag, not the other customer.

### H — Moved loads repeatedly (10 / 12 / 14, flew at 16) ⚠️→🔴

```
H jump 16:05 with stale 10/12/14 + real 16:00: -> Ours@D (load D, idx 1)   ✅
H intro 10:15 with the same 4 stale slots:     -> Ours@A (load A, idx 1)   🔴 splits
```

1. Jump clips resolve correctly (causal rule picks 16:00). Earlier clips resolve to
   whichever stale slot's window they fall in.
2. Jump → correct; interview → the 10:00 stale slot.
3. **Partially.** Same customer throughout (all four slots are *their* bookings), so this
   is not a cross-customer leak — but it is **up to 4 jobs, 4 renders, 4 emails** for one
   customer, several of them fragmentary.
4. If any stale slot belongs to a *different* customer (a re-manifest that reused the
   booking row), it degrades into Scenario C.
5. **Data hygiene** (stale slots must be cancelled/removed), amplified by the code's
   willingness to match them.
6. **Test:** four candidate slots, clips across the day ⇒ one job (post-fix: all clips of
   one card session resolve to the same slot, or flag).

### I — Missing interview, jump footage present ✅

1. Nothing requires an interview clip. Scene classification labels what exists; the house
   cut/`_ensure_story`/`validate_and_repair` build milestones from the scenes present.
2. Correct customer.
3. **Yes.** A jump with no interview renders normally.
4. Nothing material.
5. n/a.
6. **Test:** exists in spirit via the selfie pipeline tests; add an explicit
   "jump-only clip set renders all deliverables" case.

### J — Interview only, no jump footage 🔴 (product correctness)

1. `_curated_freefall` **substitutes the first scene** when no `freefall` scene exists
   ([api/selfie.py:1873-1878](api/selfie.py#L1873-L1878)) so the EDL validates; the job
   reaches `ready` and, under `AUTO_DELIVER`, is emailed.
2. Correct customer — wrong product.
3. **No.** The customer receives a "chute libre" video that is footage of them standing on
   the ground, and a `full_video` with no jump in it.
4. This is the delivery half of Scenario B: a split job is *guaranteed* to hit this path.
   Worth noting the load-master path already has the right guard (`_flew_a_jump`,
   [api/tasks.py:373](api/tasks.py#L373)) — a customer job has none.
5. **Code.**
6. **Test:** clip set with no freefall telemetry ⇒ job fails (or holds for review) rather
   than delivering; `_flew_a_jump`-style guard applied at the customer-job seam.

### K — Clips arrive out of order ✅

1. Order is irrelevant: each clip is matched independently and appended to the pending
   jump; the settle timer is re-armed per clip
   ([bridge:426-432](scripts/skydiveos_bridge.py#L426-L432)). The auto-edit side stamps
   `last_raw_clip_at` and re-schedules `raw_clips_settled_job` until quiet
   ([api/tasks.py:773-832](api/tasks.py#L773-L832)). Scene ordering later comes from
   capture timestamps, not arrival order.
2. Correct customer.
3. **Yes** — one job, correctly ordered.
4. The **one** ordering hazard: a clip arriving **after** the 900 s window has already
   flushed creates a *new* `PendingJump` for the same `(load_id, jumper_index)` ⇒ **a
   second job for the same customer**. There is no persistent "a job already exists for
   this jump" check anywhere (the bridge's `handled` map is keyed by `s3_key`, not by
   jump). Realistic when the last clip's S3 upload exceeds 15 min on a dropzone uplink.
5. **Code.**
6. **Test:** flush a jump, then notify one more clip of it ⇒ must attach to the existing
   job (or flag), not create a second.

### L — Duplicate ingestion ✅ mostly

| Stage | Guard | Verdict |
|---|---|---|
| Re-scan/re-pull | `is_complete()` (file + manifest) ⇒ no re-emit | ✅ |
| Re-upload to S3 | same key, idempotent PUT | ✅ (but see the F key collision) |
| Re-notify | `handled`/`flagged` on `s3_key`, persisted in `jobs/_bridge_state.json` | ✅ |
| Job creation | `POST /jobs` has **no idempotency key** — every call mints a new uuid ([app.py:897](api/app.py#L897)) | ⚠️ relies entirely on the bridge's dedupe |
| Dispatch | `processing_dispatched` exactly-once ([api/tasks.py:754-770](api/tasks.py#L754-L770)) | ✅ |
| Email | no per-job "already emailed" record | ⚠️ see M |

A genuinely duplicated *notification* is safe. A duplicated *jump* (via K, or via a
bridge restart that lost pending state) is not.

### M — Worker crash / retry ⚠️

1. Celery runs `task_acks_late=True` ([api/celery_app.py](api/celery_app.py)), so a task
   killed after finishing its work but before the ack **runs again**.
2. Re-running `process_selfie_package` re-renders (idempotent output paths, fine) and then
   calls `_maybe_auto_deliver`, which sees `ready` again ⇒ approves ⇒ `deliver_job` ⇒
   **a second email**. Re-running `deliver_job` itself is worse: the status is still
   `approved` while it runs, so the guard at
   [api/tasks.py:337](api/tasks.py#L337) passes and `deliver_to_customer` re-uploads and
   re-sends.
3. **Not idempotent at the email boundary.** Renders, uploads and archive writes are
   idempotent; the notification is not.
4. Duplicate "your video is ready" to one customer. Not a cross-customer risk.
5. **Code.**
6. **Test:** call `deliver_job` twice on an approved job ⇒ exactly one email
   (needs a `delivered_at`/`email_sent_at` record to key on).

### N — Camera clock wrong 🔴

```
N clock +1h: clip stamped 11:15 for the 10:00 load (X on 11:00): -> CustomerX
```

1. Matching is **entirely** dependent on `creation_time` + `CAMERA_CLOCK_TZ`. A clock one
   hour fast shifts every clip into the neighbouring load's window.
2. Whoever the shifted time points at — here Customer X.
3. **No.**
4. `WINDOW_PRE=30 min` absorbs a few minutes of skew; **an hour is a different load and a
   different customer**, and nothing detects it (there is no cross-check against GPMF
   GPS time, which *is* absolute and present in most clips). A DST transition or a
   battery-flat camera that reset its clock produces the same result. `CAMERA_CLOCK_TZ`
   unset does exactly this at the size of the UTC offset — that's why
   `check_match.py --readiness` reports it.
5. **Operational** (clock discipline) with a **code** mitigation available (GPMF GPS
   timestamp as a sanity check, and refusing when container time and GPS disagree by more
   than a few minutes).
6. **Test:** shift a clip's `captured_at` by ±1 h with two adjacent loads ⇒ must refuse
   rather than silently re-attribute; plus a readiness check that flags clips whose
   container time and GPMF GPS time disagree.

### O — Midnight boundary 🔴

```
O jump 00:05 Aug12, load dep 23:50 Aug11, staff ALSO on an Aug12 load: -> MorningCust
O same but staff has NO Aug12 load:                                    -> NightJumper ✅
```

1. Day narrowing runs **first**: `same_day` = the Aug-12 load only, so the night jumper's
   load is discarded; `narrowed` is then length 1 and R3 skips the window check — the
   00:05 clip is handed to a customer whose load departs **11 hours later**.
2. The morning customer (wrong).
3. **No.**
4. Note the contrast: `resolve_load_for_staff` (spec flights) queries by *departure
   window* precisely so it "picks up the previous day's last load for a clip captured just
   after local midnight" ([match.py:731-736](ingest/match.py#L731-L736)). The jumper path
   has no such handling. Also relevant whenever the DZ's business day and the calendar day
   differ (night jumps, sunset loads).
5. **Code.**
6. **Test:** the exact candidate set above ⇒ must resolve to the night jumper or refuse.

---

## 4. Data hygiene

| Event | Handled by code? | Effect |
|---|---|---|
| Load **cancelled** (`status: cancelled`) | ✅ filtered ([match.py:87](ingest/match.py#L87)) | candidate disappears — the mechanism Scenario A relies on |
| Load left `planned` but not flown | ❌ | fully matchable ⇒ B, C, H |
| Customer **removed** from a load | ✅ for matching | but every later `jumper_index` **shifts**, so stored indices on existing jobs/rosters now name different customers (`_is_own_job`, `_pointer_job`, `load_child.jumper_index`) |
| Customer **moved** to another load | ✅ if the old slot is removed/cancelled | otherwise B |
| Load **recreated** | ⚠️ | new `_id`; jobs/rosters holding the old `load_id` stop joining (missed tiles, orphan children) |
| Load **duplicated** (same departure instant) | ✅ | `AmbiguousMatch` ⇒ flag |
| Customer manifested **twice** on one load | ✅ | two candidates, same departure ⇒ `AmbiguousMatch` ⇒ flag |
| **Instructor changed** on a jumper | ❌ | G: footage matched to the *old* instructor's other customer, or stranded |
| Old/stale manifests kept in the DB | ❌ | the pool is unbounded in time (no date filter on the loads query) ⇒ B, F, H |
| Staff without `goproSerial` | ✅ reported by `check_match.py --readiness` | `UnknownCamera` ⇒ flag (safe) |
| Two staff serials sharing a 4-digit tail | ✅ | `AmbiguousMatch` ⇒ flag |

---

## 5. Customer isolation — every cross-customer path found

| # | Path | Mechanism | Sev |
|---|---|---|---|
| 1 | **Wrong-window attribution** | A clip's timestamp falls in another customer's load window and that candidate is the lone survivor (C, D, N, O) | 🔴 |
| 2 | **Cross-day attribution** | Capture day matches no candidate ⇒ `same_day or pool` re-admits every other day; lone candidate accepted without a window check (F) | 🔴 |
| 3 | **S3 key reuse** | `raw/{camera_id}/{FILENAME}` has no date/card scope ⇒ one customer's object overwritten by another's; the bridge then drops the new clip as a duplicate | 🔴 |
| 4 | **Retention ledger reuse** | `deletable()` keyed on bare filename ⇒ authorises deleting a *different* card's identically-named clip before it is uploaded | 🔴 (loss, not mixing) |
| 5 | **Unclosed shared span** | A camera flyer films `skydiveos-shared:start` and never films `end`, and no later staff marker resets it ⇒ his customer's **personal** clips are tagged `shared` and enter the load master shown to everyone else on the load | 🔴 |
| 6 | **`jumper_index` drift** | Manifest edited after a job/roster stored an index ⇒ `_is_own_job` / `_pointer_job` / `load_child` name the wrong customer (wrong-name gallery, offer to the wrong person) | ⚠️ |
| 7 | **`goproSerial` suffix match** | `{camera_id}$` regex matches exactly one *wrong* staff (real owner's serial missing/mistyped) ⇒ whole card attributed to the wrong staff | ⚠️ |
| 8 | **Instructor swap** | G — clip resolves to the filming staff's other customer | ⚠️ |

Paths that are **safe** and worth stating explicitly: per-job `raw/` directories; the
bridge downloading only its own keys; `_media_job`'s files-from-master /
lock-state-from-requesting-job invariant ([api/app.py:1522](api/app.py#L1522)); the
archive's job-id-suffixed sibling on a name collision; `AmbiguousMatch` on same-instant
ties; `select_load`'s mandatory window; the marker clip never becoming a job.

---

## 6. Idempotency by stage

| Stage | Key | Idempotent? |
|---|---|---|
| Card → staging | `(camera_id, date, filename)` + manifest | ✅ |
| Staging → S3 | `raw/{camera_id}/{filename}` | ✅ per key, ❌ **collides across cards/days** |
| S3 → notify | retry only on non-2xx | ✅ |
| Notify → pending | `handled`/`flagged` on `s3_key` (durable) | ✅ per clip; ❌ **no per-jump key** |
| Pending → job | none (`POST /jobs` mints a uuid) | ❌ |
| Job → dispatch | `processing_dispatched` | ✅ |
| Dispatch → render | output paths overwritten | ✅ |
| Render → approve | status gate `ready(_for_review)` | ⚠️ re-run re-approves |
| Approve → deliver | status gate `approved` (not cleared during the run) | ❌ **re-run re-emails** |
| Fan-out | `_pointer_job` + `source_job_id` re-stamp | ✅ |

Also: `_flush` **pops** the pending jump before creating the job
([bridge:454-461](scripts/skydiveos_bridge.py#L454-L461)); if creation fails, the clips
are neither retried nor marked handled — they are silently dropped until someone
re-notifies. And pending state is **in memory only**, so a bridge restart inside the
15-minute window loses every un-flushed clip.

---

## 7. Test matrix

| Scenario | Expected | Current | Risk | Code/Data | Test needed |
|---|---|---|---|---|---|
| A rescheduled, old load cancelled | one job, right customer | one job, right customer | ✅ | data (must cancel) | unit + bridge grouping |
| B stale on both loads | one job or flag | **two jobs, two emails**, one is interview-only | ⚠️ | data + code | bridge dedupe; no-freefall guard |
| C busy instructor, scrubbed load | flag | **attaches to Customer X** | 🔴 | code | `select_match` refuse case |
| D loads 5 min apart | flag | **A's clip → B** | 🔴 | code (+ops) | tie-break threshold |
| E one card, 3 customers | 3 isolated jobs | 3 isolated jobs | ✅ | — | end-to-end isolation test |
| F yesterday's clips (load pruned) | flag | **→ today's customer** | 🔴 | code | day-constraint test |
| F′ filename reuse across cards | distinct keys | **key collision → clip dropped** | 🔴 | code | key-scoping test |
| F″ ledger filename reuse | keep the file | **may delete un-uploaded clip** | 🔴 | code | ledger identity test |
| G instructor swapped | flag | wrong customer / stranded | ⚠️ | code + ops | refuse case |
| H moved 3× | one job | up to 4 jobs | ⚠️→🔴 | data + code | session-consistency test |
| I no interview | renders fine | renders fine | ✅ | — | regression test |
| J interview only | fail / hold | **delivers a bogus video** | 🔴 | code | freefall guard on customer jobs |
| K out of order | one job | one job (unless a clip lands post-flush) | ✅/⚠️ | code | late-clip test |
| L duplicate ingest | no duplicates | safe per clip | ✅ | — | existing + key-collision test |
| M worker crash | one email | **duplicate email possible** | ⚠️ | code | double-`deliver_job` test |
| N clock wrong ±1 h | flag | **wrong customer** | 🔴 | ops + code | skew test, GPS cross-check |
| O midnight | night jumper or flag | **→ next morning's customer** | 🔴 | code | boundary test |
| P shared span never closed | nothing shared | **personal clips in the load master** | 🔴 | code + ops | span-timeout test |

---

## 8. The single worst realistic scenario

> **A busy tandem instructor's card, on a day where one of their jumps was pushed.**

Concretely, and entirely inside today's code:

1. Marc is manifested as instructor on the 09:30 load (customer **Xavier**) and the 14:00
   load (customer **Priya**).
2. Priya's jump was originally on the 10:00 load. Weather holds it; ops moves her to
   14:00 and cancels the 10:00 load. **Correct data hygiene — nothing is stale.**
3. Marc films Priya's pre-jump interview at **10:15** on the same handcam, then flies
   Xavier at 09:30 (or vice versa — order doesn't matter) and Priya at 14:00.
4. The card is ingested. For the 10:15 clip: candidates are `(09:30, Xavier)` and
   `(14:00, Priya)`. `_in_window(10:15, 09:30)` is **True** (window `09:00–12:00`);
   `_in_window(10:15, 14:00)` is False. Exactly one survivor ⇒ returned with no ambiguity
   and no flag ([verified above](#c--multiple-customers--busy-instructor--critical)).
5. The clip is grouped under `(load 09:30, Xavier's index)`, downloaded into **Xavier's
   job**, classified as a scene, and included in his `full_video`.
6. `AUTO_DELIVER` approves and emails **Xavier** a gallery whose video opens with
   **Priya's face and Priya's name being said on camera**. Priya's own job renders without
   her interview. Both jobs report success; nothing is flagged; the only trace is an INFO
   line saying the match resolved cleanly.

**Why the code allows it:** the jumper-keyed match treats "this staff member has a jumper
slot whose flight window contains the clip" as sufficient proof of ownership. With
`WINDOW_POST = 150 min`, a single load's window covers a quarter of the operating day, so
any clip filmed *between* jumps is claimed by whichever load most recently departed. There
is no notion of *which jump a clip is about* — only *which flight was in the air near
it* — and no cross-check that the other clips on the same card session agree.

The nastiest property is that **it presents as a clean match, not an ambiguity**, so
every refuse-and-flag safeguard in the system is bypassed: `AmbiguousMatch` needs ≥2
survivors, and this has exactly one.

Runner-up (equally realistic, different failure): two unlabeled cards
(`sd-NO-NAME` × 2) or a formatted card restarting at `GX010001.MP4` ⇒ S3 key collision
⇒ one customer's master overwritten and the other's clip silently dropped as a duplicate.

---

## 9. Recommendations (not implemented)

### 🔴-1 Window is advisory on the jumper path (C, D, F, N, O)

* **Root cause:** R1/R2/R3 — no code path asserts that the chosen candidate's flight
  window contains the clip; the lone-candidate short-circuits skip the check entirely.
* **Fix (code, `ingest/match.py`):** keep the causal tie-break, but make the window a
  **precondition** rather than a tie-breaker:
  1. Constrain the loads query (or the pool) to `business_day == captured_day` and refuse
     when nothing matches, instead of `same_day or pool`.
  2. Require `_in_window` for the returned candidate **in all cases**, including a lone
     candidate — with one deliberate exception: keep accepting a lone candidate outside
     its window only when the clip's day matches and no other candidate exists *that day*
     (this is what preserves Scenario A), and log it loudly as an out-of-window accept.
  3. Add a minimum-gap rule to the causal tie-break: if the two best departures are closer
     together than ~10 min, refuse instead of picking the later one (D).
  4. Handle the midnight case the way `select_load` already does — window-based candidate
     selection across the day boundary, before day narrowing.
* **Side effects — read carefully.** This *will* convert some currently-automated matches
  into flags: any clip filmed between loads (interviews, gear-up, post-landing chat) that
  today lands in the previous load's window. That is the point, but it changes the
  operational load: `unflag_bridge_key.py` traffic goes up, and some customers' interviews
  will need a manual attach until the session-consistency rule below exists.
* **Better long-term fix (code, larger):** decide ownership **per card session** rather
  than per clip. The clips between two staff markers are one session; the *dominant*
  jump-bearing match across that session (the load whose freefall-carrying clips resolve
  to it) should claim the session's orphan clips, instead of each clip resolving alone.
  This is the only approach that gets Priya's interview into Priya's job rather than
  merely keeping it out of Xavier's.

### 🔴-2 S3 key and retention-ledger identity (F)

* **Root cause:** both are keyed on `{camera_id} + bare filename`, and neither is unique.
* **Fix (code):** put the capture date (and ideally a short content/size hash) in the key:
  `raw/{camera_id}/{YYYY-MM-DD}/{filename}`. Scope the ledger the same way, and require a
  **size match** before `deletable()` authorises a delete (the pruner already uses
  size-matched HeadObject — the card sweep should too).
* **Side effect:** changes the S3 layout. Existing `raw/{camera}/{name}` objects, the
  `raw_s3_keys` stored on jobs, and the pruner's lifecycle expectations must tolerate both
  shapes during the transition. Purely additive if new keys are written and old ones still
  read.
* **Operational stopgap until then:** label every card uniquely and never format a card
  that has been used with a different `camera_id`.

### 🔴-3 A customer job with no jump still delivers (B, J)

* **Root cause:** `_curated_freefall`'s stand-in makes a freefall-less clip set render a
  valid-looking deliverable; only load masters have a `_flew_a_jump` guard.
* **Fix (code):** apply the same evidence test to customer jobs — no `freefall` scene ⇒
  fail with an actionable error (or hold for review) instead of delivering. Under
  `AUTO_DELIVER` this must block the email, not just log.
* **Side effect:** a genuinely freefall-less product (a photo-only booking, ground-only
  footage) must be exempted by package, or legitimate jobs start failing.

### ⚠️-4 No per-jump idempotency for job creation (B, H, K)

* **Root cause:** the only dedupe key is `s3_key`; nothing keys on the *jump*.
* **Fix (code, bridge + API):** persist `(load_id, jumper_index)` → `job_id` in the bridge
  state and, on a late clip, attach to the existing job instead of creating a second one
  (the API tolerates this — `upload` re-arms the settle window and `processing_dispatched`
  prevents a second render, though a re-render after `delivered` needs its own guard).
  Longer term, give `POST /jobs` an idempotency key (`booking_id` + `jump_date`, or
  `load_id` + `jumper_index`) so *SkydiveOS'* implementation gets the same protection.
* **Side effect:** attaching to an already-delivered job must not silently re-render and
  re-email; define that transition explicitly (probably: flag for a human).

### ⚠️-5 Delivery is not idempotent (M)

* **Fix (code):** record `email_sent_at` (per recipient) on the job and make
  `deliver_to_customer` skip the send when it is set; keep re-uploads idempotent.
* **Side effect:** a genuine re-delivery (customer lost the email) needs an explicit
  override flag.

### ⚠️-6 Bridge pending state is volatile (K, plus footage loss)

* **Fix (code):** persist pending jumps to `_bridge_state.json` and re-arm timers on
  startup; don't pop before a successful create (or re-queue on failure).

### ⚠️-7 `jumper_index` drift (data hygiene)

* **Fix (data/code):** prefer `booking_id` everywhere it is available and treat
  `jumper_index` as a fallback only (`_is_own_job` already does this — `_pointer_job` and
  `load_child` creation do not). Operationally: never delete a jumper row from a flown
  load; cancel it in place.

### ⚠️-8 Clock skew is undetectable (N)

* **Fix (code):** cross-check the container `creation_time` against the GPMF **GPS**
  timestamp (absolute, already parsed by `/metadata`) and refuse/flag on a disagreement
  beyond a few minutes. Cheap, and it catches DST, dead-battery resets and a wrong
  `CAMERA_CLOCK_TZ` in one test.
* **Operational:** a morning clock check per camera; `check_match.py --readiness` before
  the first load.

### 🔴-9 Unclosed shared span (isolation path 5)

* **Fix (code):** bound a shared span — close it automatically at the end of the card
  session, after N clips, or after a wall-clock duration, and **flag** a span that was
  never explicitly closed rather than tagging the remainder of the card shared.
* **Operational:** film the `end` card immediately after the shared footage.

**Priority order:** 🔴-2 (cheap, mechanical, prevents silent loss) → 🔴-3 (cheap, prevents
the visible bad-product email) → ⚠️-5 → ⚠️-4 → 🔴-1 step 1+2 (the day constraint and the
window precondition) → 🔴-9 → 🔴-1 session consistency → ⚠️-8.

---

## 10. Final report

### A. Current architecture summary

Per-clip, timestamp-driven matching against a Mongo manifest, with identity supplied by a
filmed QR marker (`staffs._id`) or a camera serial. Matching is pure and unit-testable
(`select_match`/`select_load`); the DB half is a thin lookup. Clips are grouped into jobs
by a 15-minute settle window in the notify consumer, and again by a settle window inside
auto-edit. Rendering, archiving and dispatch are idempotent; job creation and delivery
are not. Ownership decisions refuse-and-flag on *ambiguity* (≥2 survivors) but never on
*implausibility* (one survivor, wrong time).

### B. Critical vulnerabilities

1. 🔴 The flight window is advisory on the jumper path — a clip filmed between loads is
   claimed by the previously departed load ⇒ **cross-customer footage** (C, D, N, O).
2. 🔴 `same_day or pool` re-admits every other day when the capture day matches nothing ⇒
   **cross-day, cross-customer footage** (F).
3. 🔴 S3 keys and the retention ledger are keyed on a **reusable** filename ⇒ overwritten
   masters, silently dropped clips, and a card sweep that can delete un-uploaded footage.
4. 🔴 A job with no jump footage still renders and emails a "video" (B, J).
5. 🔴 An unclosed shared span puts one customer's personal footage into a load master
   shown to everyone else on the load.

### C. Medium-risk issues

Per-jump job creation has no idempotency key (duplicate jobs/emails on a late clip);
`deliver_job` re-emails on a Celery retry; bridge pending state is in-memory only;
`jumper_index` drift after a manifest edit; `goproSerial` suffix matching; instructor swap
has no representation.

### D. Safe / working scenarios

A (with correct cancellation), E, I, K (within the window), L (per clip), plus:
`AmbiguousMatch` on same-instant ties, `select_load`'s mandatory window, cancelled-load
filtering, per-job file isolation, the `_media_job` files-vs-lock invariant, archive
collision handling, and marker clips never becoming jobs.

### E. Required dropzone operational rules

1. A pushed jump ⇒ **cancel the old load or remove the jumper slot**. Never leave a stale
   slot `planned`.
2. **Re-film the interview when the customer actually boards.** Do not let a 4-hour-old
   interview sit on the card — that single habit removes the worst scenario in §8.
3. **One card per `camera_id`, uniquely labelled.** Never move a card between camera
   bodies without accepting the key-collision risk; never format-and-reuse across days.
4. **Clear yesterday's clips off the card** before the day starts (or run with
   `DELETE_AFTER_TRANSFER=1` and verify the ledger).
5. **Camera clocks checked each morning**, and `CAMERA_CLOCK_TZ` set to the DZ zone.
6. The instructor who **flies** the jump films its interview (G).
7. Camera flyers: film `shared:end` **immediately** after the shared footage.
8. Run `python scripts/check_match.py --readiness` and `--day <today>` before the first
   load; investigate every `FAILED`.
9. Never delete a jumper row from a **flown** load — cancel in place.

### F. Required automated tests

Pure `select_match`/`select_load` cases for C, D, F, G, H, N, O (the candidate sets in §3
are ready to lift verbatim); S3-key uniqueness across cards/days; ledger identity
(size-aware); no-freefall customer job ⇒ refuse; double-`deliver_job` ⇒ one email; late
clip after flush ⇒ one job; three-customer card isolation end-to-end; shared span with no
`end` ⇒ bounded/flagged.

### G. Recommended code changes

In priority order: date-scope the S3 key + ledger identity; freefall evidence guard on
customer jobs; `email_sent_at` idempotency; per-jump job idempotency key; day constraint
+ window precondition in `select_match` (with the deliberate lone-candidate exception);
bounded shared spans; GPMF-GPS clock cross-check; session-consistency matching (the real
fix for §8); prefer `booking_id` over `jumper_index` in `_pointer_job` and child creation.

### H. Recommended monitoring / logging

* Log the **rejected** candidates on every successful match (load, departure, customer,
  window verdict) — today a wrong match is indistinguishable from a right one in the logs.
* Emit a metric per match outcome (`matched`, `out_of_window_accept`, `flagged`, `spec`),
  and alert on any `out_of_window_accept`.
* Alert on: a flag appearing in `_bridge_state.json`; two jobs sharing
  `(load_id, jumper_index)`; two jobs sharing a `booking_id`; a delivered job whose scenes
  contain no `freefall`; an S3 `raw/` PUT that overwrote an existing key; a job whose
  `captured_at` day ≠ its load's `businessDate`.
* Include `load_id`, `jumper_index`, `booking_id` and `captured_at` in every job log line
  so a mis-delivery can be reconstructed after the fact.

### I. Production go-live checklist

- [ ] 🔴-2 fixed (S3 key + ledger identity) — silent loss is the least acceptable failure
- [ ] 🔴-3 fixed (no jump ⇒ no delivery)
- [ ] ⚠️-5 fixed (`email_sent_at`) — duplicate emails already happened once
- [ ] 🔴-1 steps 1–2 fixed, or `AUTO_DELIVER=0` for jumps until they are
- [ ] Decision recorded on the out-of-window-accept exception (it is what keeps Scenario A working)
- [ ] `out_of_window_accept` metric + alert live
- [ ] `check_match.py --readiness` clean; `--day` clean for a rehearsal day
- [ ] Every staff member has a unique `goproSerial`; no two share a 4-digit tail
- [ ] `CAMERA_CLOCK_TZ` set; all camera clocks verified same-day
- [ ] Cards uniquely labelled, one per camera body; yesterday's footage cleared
- [ ] Ops rules §E printed and briefed, especially "re-film the interview" and "cancel the old load"
- [ ] Bridge pending state persisted, or a documented "restart only between loads" rule
- [ ] `unflag_bridge_key.py` runbook in the hands of whoever watches the flags
