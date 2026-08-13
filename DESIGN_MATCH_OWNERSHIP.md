# Phase 3 design proposal — ownership before delivery (NOT IMPLEMENTED)

**Status: awaiting approval. No matcher code has been changed.**
Companion to [AUDIT_MEDIA_MATCH_ISOLATION.md](AUDIT_MEDIA_MATCH_ISOLATION.md) (the
findings) and `tests/test_match_ownership.py` (the pinned current behaviour).

Phase 1 is implemented and green; Phase 2's regression suite is in place. This document
is Phase 3 (the `select_match` rule) plus Phase 4 (what stronger signals actually exist),
with the nine deliverables you asked for.

---

## 1. The governing rule

> The matcher must never treat **"only one candidate"** as proof of ownership.

Today it does, twice ([ingest/match.py:436](ingest/match.py#L436) and
[:392](ingest/match.py#L392)), and a third path re-admits candidates from other days
([:391](ingest/match.py#L391) `same_day or pool`). Those three lines are every 🔴 in the
audit.

Proposed: a candidate may be returned only when ownership is **established**, not merely
**unopposed**. Four tests, in order, and an explicit refusal when they can't be met.

### T1 — Day (hard)

The clip's DZ-local capture day must equal the candidate's `businessDate`.

```python
same_day = [c for c in pool if c.business_day == captured_day]
# today: narrowed = same_day or pool      ← the cross-day leak
# proposed: if not same_day: refuse (NoBookingMatch, "no slot on the capture day")
```

Exception, deliberate and narrow: a candidate whose `business_day` is the **previous**
day *and* whose flight window still contains the clip stays eligible — the post-midnight
case (§3-O). This is what `select_load` already does for spec flights by querying on the
departure window ([match.py:731-736](ingest/match.py#L731-L736)); the jumper path simply
never learned it.

### T2 — Window (hard, with one exception)

The returned candidate's flight window must contain the clip:
`departure − WINDOW_PRE ≤ captured ≤ departure + WINDOW_POST`.

**The exception that keeps Scenario A working** — and the single most important decision
in this proposal:

```
if exactly one candidate exists ON THE CAPTURE DAY
   and no other candidate that day is in-window
   and the clip is outside its window
→ accept it, stamp the result `evidence="out_of_window_same_day"`, log at WARNING,
  and emit the `out_of_window_accept` metric.
```

That is Priya's 10:15 interview when her 10:00 load was properly cancelled: one slot that
day, nothing to confuse it with. Without the exception, correct data hygiene stops
producing complete jobs, and the interview needs a manual attach every time a jump moves.
With it, the accept is *visible* rather than silent — which is the actual defect today.

A candidate with `departure_local = None` never satisfies T2 and can only be accepted
through this exception.

### T3 — Identity (hard)

The chosen candidate must carry the identity the downstream chain needs: a
`booking_id`, or a `customer` id. A slot with neither cannot be joined to a job, a
gallery or an email later (`_is_own_job`, `_pointer_job`, adoption), so it must flag now
rather than produce a job that can't be reconciled. Cheap, and it closes the
`jumper_index`-drift class (⚠️-7) at the source.

### T4 — No contradiction (hard)

If, after T1–T2, **more than one** candidate is in-window, today's causal tie-break
("latest departure at or before the clip") applies **only** when the two best departures
are at least `MIN_DEPARTURE_GAP` apart (proposed: **20 minutes**, i.e. shorter than a
realistic load turnaround). Closer than that → refuse (§3-D). Identical instant already
refuses; this just widens "identical" to "indistinguishable".

### Refusal

When any test fails: raise the existing `NoBookingMatch` / `AmbiguousMatch`, so the
bridge's refuse-and-flag path, `unflag_bridge_key.py` and the 200-not-5xx contract all
keep working unchanged. The message must name what was rejected and why — a flag is only
actionable if it says which loads it declined to choose between.

### `MatchResult.evidence` (new, additive)

`window` | `out_of_window_same_day` | `causal_tiebreak` | `session_inherited` (Phase 4).
Purely informational, and the hook for monitoring: **alert on anything that isn't
`window`.** It also gives the operator screen a way to say "this one was a judgement
call".

---

## 2. Impact — which working scenarios become unmatched

Measured, not estimated: `tests/test_match_ownership.py` holds the current answer and the
intended answer for all 16 audited scenarios, and prints the delta.

```
Phase 3 impact over 16 audited scenarios:
  unchanged:                    11
  become REFUSE (flag):          4   busy_instructor_delayed_intro,
                                     departures_five_minutes_apart,
                                     yesterdays_clip_yesterdays_load_pruned,
                                     camera_clock_one_hour_fast
  resolve to a different owner:  1   post_midnight_clip_with_a_next_day_load
```

**Every one of the four new refusals is a case that today names the WRONG customer.** No
scenario that is correct today becomes a refusal — that is asserted in
`test_impact_of_the_phase_3_rule`, and it is the acceptance criterion for the change.

### What newly gets flagged in real operation

| Situation | Today | After | Frequency estimate |
|---|---|---|---|
| Clip filmed between loads while the staff member has ≥2 slots that day (interview after a hold, gear-up chat, post-landing debrief filmed later) | silently joins the previous load's customer | **flagged** | The main new load. At a busy DZ: a handful per day *if* staff film between jumps. Zero if the interview is filmed with the jump. |
| Yesterday's clips on a card whose load was pruned/archived | joins today's customer | **flagged** | Rare, and today's behaviour is a leak |
| Two loads <20 min apart, clip between them | picks the later | **flagged** | Rare (cameraman back-to-backs) |
| Clock off by ≥1 h | wrong customer | **flagged** (the shifted time won't be in any window on the right day, or will fail T4) | Rare, catastrophic today |
| Post-midnight clip | next morning's customer | **correct owner** | Night jumps only |
| Everything normal (clip inside its own load's window) | correct | **correct, unchanged** | The overwhelming majority |

### Honest statement of the trade

A flagged clip is **not** a lost customer video — the footage is on the card, in S3, and
in the archive; the flag is cleared with `unflag_bridge_key.py` after the data is fixed,
or the clip is attached by hand. But a flag **does** mean a human touch, and if the
dropzone films interviews between loads as routine practice, this converts a currently
"automatic" (and quietly wrong) flow into a semi-manual one **until Phase 4 lands**. That
is why Phase 4 matters and why I would not ship T1–T4 without either (a) the operational
rule "film the interview with the jump", or (b) Phase 4's session inheritance.

---

## 3. Phase 4 — session consistency: what data actually exists

Audited rather than invented. Signals available **today**, strongest first:

| Signal | Where it lives | Strength | Used today? |
|---|---|---|---|
| **QR staff session** | `ingest/qr.py` `QrSessionIndex` — which staff owns each clip on a card, now **card-level** (marker may be filmed at either end) | Strong: a filmed human decision | Yes, for *who*; never for *which jump* |
| **Ingestion batch = one card pull** | `ingest.discovery` now holds a pull's hand-offs until the whole card is staged (added 2026-08-11 for the QR change) | Strong: the batch is a real session boundary, and it is now **knowable at once** | Only to defer hand-off |
| **GoPro chaptering** | `api.selfie.chapter_from_filename` — `GX01`**1234** / `GX02`**1234** are chapters of ONE recording | Very strong for "same recording" | Parsed, but only for ordering |
| **Contiguous file numbering** | `GX010001 → GX010002 …` monotonic per card | Moderate: adjacency ≠ same jump | No |
| **`created_epoch` + duration** | `.ingest.json` per clip + ffprobe | Moderate: gives each clip a real `[start, end]`, so "which freefall-bearing clip is this orphan adjacent to?" is computable | Only `created_epoch`, for ordering |
| **GPS absolute time (GPS9 / GPSU)** | `metadata/gpmf.py` already demuxes `GPS9`/`GPS5` | **Strong and unused**: an absolute UTC instant from satellites, independent of the camera clock | **No** — the clock-skew blind spot (§3-N) is closed by reading a field we already parse |
| **Freefall evidence per clip** | scene classification / `_flew_a_jump` | Strong for "this clip is a jump" | Yes (load masters; now customer jobs too, Phase 1) |
| **`booking_id`, customer doc** | `loads.jumpers[]`, `customers` | Strong identity | Yes |
| **Camera registry `instructor_id`** | `ingest/registry.py` | Weak — a static hint, explicitly not authoritative | Hint only |
| Scene labels for *identity* | — | **Unusable** — [AUDIT_SCENE_LABELS.md](AUDIT_SCENE_LABELS.md) | Correctly not used |

### Proposed Phase 4 rule (sketch, for a later review)

**Anchor, then inherit.** Within one card session (QR staff span ∩ one ingestion batch):

1. **Anchor clips** = clips that resolve under T1+T2 *and* carry jump evidence (a freefall
   scene, or a freefall-shaped accelerometer signature). These are unambiguous: they were
   filmed in the air, inside a load's window.
2. **Orphan clips** = everything the strict rule refuses.
3. An orphan is inherited by the anchor jump that is **nearest in time within the same
   session**, provided:
   * exactly one anchor jump is within `SESSION_INHERIT_MAX_GAP` (proposed 4 h — a
     weather hold is hours, a different customer is a different session), **and**
   * no *other* anchor jump in the session is closer, **and**
   * the orphan carries no jump evidence of its own (a freefall-bearing orphan is a
     different jump, not this one's interview).
4. Result stamped `evidence="session_inherited"`, always logged, never used for a
   `load_master`.

Applied to §8: Marc's card has anchors at 09:30 (Xavier, freefall) and 14:00 (Priya,
freefall). The 10:15 orphan is 45 min from Xavier's anchor and 3 h 45 from Priya's — so
**nearest-anchor inheritance would give it to Xavier: still wrong.**

That is the crucial finding, and it is why Phase 4 is a separate phase and not a quick
follow-up: **time proximity cannot solve §8, because the interview is genuinely closer to
the wrong jump.** The orphan is only resolvable with a signal that is *about the
customer*, of which exactly three exist:

* **a) Chaptering/adjacency**: if the interview is chaptered with, or immediately
  file-adjacent to, Priya's jump footage, that is real evidence — but GoPro numbering is
  sequential in *recording* order, so a 10:15 interview sits between Xavier's clips, not
  Priya's. Helps only when the interview is filmed *at boarding*, which is the case where
  the strict rule already works.
* **b) A filmed marker** — the mechanism this codebase already trusts for exactly this
  class of problem (`skydiveos-shared:start/end` solved the personal-vs-shared split the
  same way). A per-customer QR (printed per booking, or the customer's booking QR from
  SkydiveOS) filmed at the head of their footage would make ownership a *filmed human
  decision* rather than an inference. **This is the only mechanism that actually solves
  §8.**
* **c) Face identity across clips** — MediaPipe embeddings are already computed on the
  freefall window; matching the interview's face to the jump's face is technically
  possible. It is also biometric identification of customers: new consent, storage and
  liability questions, and a false match is the exact failure we are trying to prevent.
  **Not recommended.**

So my recommendation for Phase 4, when we get there, is **(b) a per-jump filmed marker**,
with session inheritance as a *narrow* helper for the unambiguous case (exactly one anchor
jump in the whole session — a card that filmed one customer all session, which is the
common single-tandem card).

---

## 4. The nine deliverables

### 4.1 Root causes (confirmed)

| # | Issue | Root cause |
|---|---|---|
| 1 | Interview-only video delivered | `_curated_freefall` substitutes the first scene when no `freefall` exists ([selfie.py:1873](api/selfie.py#L1873)) so the EDL validates; only load masters had an evidence gate |
| 2 | S3 key collision | `raw/{camera_id}/{FILENAME}`; GoPro filenames restart on a formatted card, `sd-NO-NAME` is not unique, and the notify consumer dedupes on the key → overwrite + silent drop |
| 3 | Retention ledger collision | `deletable()` matched on bare filename; a record is "some file called X was uploaded", not "this file was uploaded" |
| 4 | Duplicate email | `task_acks_late=True` re-runs `deliver_job`; the `status != approved` guard cannot see it (status is `approved` for the whole run); `job.json` is read-modify-write so a flag in it can't arbitrate two workers |
| 5 | Wrong-customer match | Ownership accepted on "unopposed" not "established": lone-candidate short-circuits skip day+window; `same_day or pool` re-admits other days; the causal tie-break guesses between close departures |

### 4.2 Fixes

1–4 **implemented** (Phase 1, below). 5 **proposed only** (§1 of this document).

### 4.3 Files / services affected

**Phase 1, changed:**
`api/jobs.py` (`hold_reason`, `email_sent_at`, `claim_email_send`,
`release_email_claim`, `EMAIL_CLAIM_FILENAME`) · `api/tasks.py`
(`_auto_deliver_block`, `_scene_manifests`, `_flew_a_jump` generalised,
`_maybe_auto_deliver`) · `api/delivery.py` (`send_gallery_email_once` + 3 call sites) ·
`ingest/discovery.py` (`raw_object_key`, `_capture_day`, `_file_fingerprint`,
`_object_size`, `_file_size`, ledger size) · `ingest/retention.py` (`TransferRecord.size`,
`matches()`, `deletable()` takes sizes) · `ingest/pull.py` (`_sweep_card` passes sizes) ·
`scripts/prune_jobs.py` (comment only — behaviour already correct).

**Phase 3 would change:** `ingest/match.py` only (`_narrow_by_time`, `select_match`,
`MatchResult.evidence`). `select_load` untouched. No `api.*` import (the module stays
pure and dependency-light).

**Services:** the local bridge and, in production, **SkydiveOS's own matcher** — it owns
this decision in prod, so T1–T4 must be mirrored there or the fix only applies to our
stand-in. Nothing else consumes `select_match`.

### 4.4 Regression tests added

* `tests/test_match_ownership.py` (**new**, 33 tests): all 8 requested scenarios plus
  role-follows-slot, zero-candidate, window primitives, the lone-candidate root cause, and
  the impact counter. Each case pins `clip → load → jumper_index → customer → role`.
* `tests/test_retention.py::TestFilenameReuse` (6 tests): the reused-`GX010001.MP4` case
  end to end, plus size-less records and unmeasurable card files failing safe.
* `tests/test_discovery.py::test_raw_object_key_scopes_by_day_and_never_overwrites`.
* `tests/test_delivery.py`: 5 email-idempotency tests (retry, concurrent worker, failed
  send releases the claim, unconfigured SMTP releases it, retry still returns links) and 7
  jump-evidence tests (hold, proceed, per-camera manifest, no-manifest, stale hold
  cleared, manual override, `jumper_slot` master exempt).
* `tests/test_bridge.py`: 5 grouping tests (one jump → one job; two customers never share;
  spec master keyed apart; **the late-clip second-job gap pinned as a known gap**; no
  `captured_at` → flagged).

### 4.5 Backward compatibility

* **S3 keys are opaque to every consumer.** They are stored (`Job.raw_s3_keys`), echoed
  (SkydiveOS → `POST /jobs/{id}/upload`), and reduced to `Path(key).name` for the local
  filename — all of which still hold, because scoping went into the *path* and the
  basename is unchanged. Old objects at `raw/{camera}/{name}` stay readable; nothing
  rewrites them.
* **The pruner's derived fallback** (`raw/{camera}/{name}`) no longer matches a *new*
  key, so it falls through to "S3 does not confirm → keep the file". Fail-safe; new jobs
  use the recorded key anyway.
* **Ledger records without `size`** (every existing one) become non-deletable. Fail-safe
  by design: cards keep footage until a fresh upload re-records them with a size.
* **`hold_reason` / `email_sent_at`** are new optional fields; `Job` is `extra="forbid"`
  but these are additive, and old `job.json` files load fine (defaults `None`).
* **`send_gallery_email` is unchanged** and still exported — the guard is a wrapper, so
  existing tests and any external caller keep working.
* Phase 3's `MatchResult.evidence` would be additive with a default.

### 4.6 Migration

**None required.** Specifically:

* No S3 objects move. Two key shapes coexist; the *only* consumer of the old shape is the
  pruner's fail-safe guess.
* No ledger migration: the fail-safe path handles old records. If a card's cleanup matters
  immediately, delete `<root>/_camera-staging/<camera_id>/.transferred.json` — footage is
  re-confirmed on the next upload (it never authorises deletion of anything unproven).
* No `job.json` migration.
* One **operational** step, not a migration: after deploying, confirm
  `DELETE_AFTER_TRANSFER` behaviour on one card (a first pull will not clean it, by
  design).

### 4.7 Expected increase in flagged / unmatched jobs

* **Phase 1 (already implemented): +0 flags, and a new "held for review" state.** Jobs
  that would have been auto-delivered with no jump in them now wait for an instructor.
  Expected volume: only the split/stale-manifest cases — at a clean dropzone, zero.
* **Phase 3 (proposed): 4 of 16 audited scenarios become flags**, all of them currently
  wrong-customer. In daily operation the driver is one behaviour: **clips filmed between
  loads by staff who fly more than one load a day.** If interviews are filmed with the
  jump, the increase is ≈0. If they are routinely filmed between loads, expect roughly one
  flag per such clip until Phase 4.
* Recommended: run the `out_of_window_accept` counter **for a week before enforcing T2**.
  It measures the exact population, with no behaviour change. That is the cheapest way to
  turn this estimate into a number.

### 4.8 Dropzone operational changes

Required by Phase 1 (now):
1. Nothing breaks, but **watch for "held for review"** — a held job means the footage had
   no jump in it. Check the clip set before approving by hand.
2. Cards: after this deploy the **first** pull won't clean a card (old ledger records lack
   sizes). Normal from the second pull.

Required before Phase 3 is enforced:
3. **Film the interview when the customer boards**, not hours earlier. This single habit
   removes the §8 worst case today, with no code.
4. **Cancel the old load (or remove the slot) whenever a jump moves.** T1/T2 depend on it,
   and it is what keeps the Scenario-A exception narrow.
5. **Camera clocks verified each morning**; `CAMERA_CLOCK_TZ` set.
6. Whoever watches flags needs the `unflag_bridge_key.py` runbook.

### 4.9 Rollback plan

Per fix, independently revertable — none of them share state:

| Fix | Rollback | Residue |
|---|---|---|
| Jump-evidence hold | Revert `_auto_deliver_block` (or set `AUTO_DELIVER=0` and approve manually) | `hold_reason` values sit unused on old jobs; harmless |
| S3 key scoping | Revert `raw_object_key`; new-shape objects stay valid and their keys are recorded per job | Two key shapes in the bucket forever (already true) |
| Ledger identity | Revert `retention.py` + `pull.py`; records with `size` are read fine by the old code (it ignores the field) | None |
| Email idempotency | Revert `delivery.py` wrapper; `.email_claimed` markers become inert files | Delete `jobs/*/.email_claimed` if a re-send is wanted |
| Phase 3 matcher | Single-module revert of `ingest/match.py`; `tests/test_match_ownership.py` documents both behaviours, so the flip is mechanical | None |

Fastest emergency lever, if Phase 3 ever floods flags: revert `ingest/match.py` alone —
it imports nothing from `api.*` and nothing persists its decisions.

---

## 5. What is still open after Phase 1

Carried forward from the audit, unfixed and deliberately so:

* **⚠️-4 per-jump idempotency** — a clip arriving after the settle window still opens a
  second job for the same customer (pinned in `test_bridge.py` as a known gap). Fix is a
  `(load_id, jumper_index) → job_id` record in the bridge state plus an idempotency key on
  `POST /jobs`. Recommended next, after Phase 3 is approved.
* **⚠️-6 bridge pending state is in-memory** — a restart inside the 15-minute window
  strands clips.
* **🔴-9 unclosed shared span** — a flyer who forgets the `end` card puts personal footage
  in a load master.
* **⚠️-8 clock skew** — undetectable until the GPS cross-check exists (§3 above: the field
  is already parsed).
* **⚠️-7 `jumper_index` drift** — `_pointer_job` and child creation still key on position;
  T3 would close the matcher half.
