# AUDIT: The matcher vs. a spec-flight "load master" job

**Phase 1 read-only audit — 2026-08-10.** No code was changed. Every claim carries a
`file:line` reference against the current working tree. Scope as given: v1 is
**spec flights only** (camera flyer with no assigned customer), **no `scene_filter`**.

## Verdict up front

**The `load_spec` branch is a MEDIUM feature, and one stop condition is MET.**

Load resolution today is **entirely derived from the jumper** — the `loads` query
is filtered by "this staff is some jumper's `instructor` or `assignedCameraman`"
before timestamps are consulted at all
([ingest/match.py:440-447](ingest/match.py#L440-L447)). On a spec flight that
query returns **zero loads**, so the matcher never reaches the timestamp logic and
there is no load to resolve. A `load_spec` branch cannot narrow the existing
candidate list; it needs a **second, independent load query keyed on
`businessDate`/`departureTime`** that does not exist yet.

The good news, and why this is medium and not a restructure: that query is
straightforward against the same schema, and `scripts/check_match.py` already
demonstrates it ([scripts/check_match.py:194-200](scripts/check_match.py#L194-L200)
selects loads for a day purely by `departureTime`). The zero-candidate refusal is
a **guard clause inside a pure function** with a single call site
([ingest/match.py:251-252](ingest/match.py#L251-L252),
[473](ingest/match.py#L473)), so the new branch sits *alongside* it rather than
restructuring it. And `Job` is a plain pydantic model whose new fields cost one
line each.

The real work is not in the matcher — it is that `MatchResult`, the bridge's
per-jump keying, and every downstream consumer are shaped around
`(load, jumper, customer)`, and a load master has no jumper and no customer.

---

## A. The current resolution chain, end to end

Two entry points reach the same core. In call order:

**Stage 1 — who filmed it (camera or QR).** Two mutually exclusive paths:

- **Camera serial** → [`FootageMatcher.resolve`](ingest/match.py#L391-L406) calls
  [`_staff_for_camera`](ingest/match.py#L348-L382): exact `staffs.goproSerial`
  match, then a case-insensitive **suffix** regex (a GoPro advertises only its
  trailing digits, `4313` vs `C3504224544313`). Raises `UnknownCamera` if nobody
  owns it, `AmbiguousMatch` if the suffix fits >1 staff. It then delegates to
  `resolve_for_staff`.
- **Filmed QR session marker** → [`qr_identity_resolver`](ingest/qr.py#L339-L386)
  decodes `skydiveos-staff:<_id>` and calls
  [`resolve_for_staff`](ingest/match.py#L408-L474) directly, skipping the serial
  lookup entirely.

**Stage 2 — which load + which jumper (one query, jumper-keyed).**
`resolve_for_staff` converts `captured_at` to DZ-local via `CAMERA_CLOCK_TZ`
([`_to_local`, ingest/match.py:333-346](ingest/match.py#L333-L346)), then runs
**one** Mongo query ([ingest/match.py:440-447](ingest/match.py#L440-L447)):

```python
db[LOADS].find({"$or": [{"jumpers.instructor":        {"$in": staff_ids}},
                        {"jumpers.assignedCameraman": {"$in": staff_ids}}]})
```

Every load returned is then filtered by status
([ingest/match.py:450](ingest/match.py#L450), `_MATCHABLE_STATUSES` at
[74](ingest/match.py#L74)) and walked jumper-by-jumper to build `Candidate`
objects carrying `(load_id, departure_local, business_day, jumper_index, jumper,
role)` ([ingest/match.py:454-471](ingest/match.py#L454-L471)). **Note the
ordering: the load set is produced by the jumper predicate. Timestamps are never
part of the query.**

**Stage 3 — pick one (pure).**
[`select_match`](ingest/match.py#L227-L286) narrows candidates by business day,
then by flight window (`WINDOW_PRE` 30 min / `WINDOW_POST` 150 min,
[ingest/match.py:69-70](ingest/match.py#L69-L70)), then causally by "the latest
departure at or before the clip". Pure, no I/O.

**Stage 4 — enrich.**
[`_build_result`](ingest/match.py#L476-L509) looks up the `customers` doc from
`jumper.customer`, maps the purchase via
[`package_and_entitlement_for`](ingest/match.py#L175-L197), and returns a
`MatchResult`.

**Stage 5 — become a job.** The bridge
([scripts/skydiveos_bridge.py:120-166](scripts/skydiveos_bridge.py#L120-L166))
debounces per `(load_id, jumper_index)` and then `POST /jobs` +
`POST /jobs/{id}/upload`
([scripts/skydiveos_bridge.py:196-260](scripts/skydiveos_bridge.py#L196-L260)).

## B. The zero-candidate case — the critical one

**Where the refusal happens.** It is a **guard clause raising an exception** from a
pure function, not a status write:

```python
if not candidates:
    raise NoBookingMatch("no matchable load-jumper for this camera + capture time")
```
— [ingest/match.py:251-252](ingest/match.py#L251-L252), inside `select_match`,
whose only production call site is
[ingest/match.py:473](ingest/match.py#L473). `NoBookingMatch` is a
`FootageMatchError` subclass ([ingest/match.py:89-90](ingest/match.py#L89-L90)).

**What each caller writes on that exception** — nothing touches a Job, because no
job exists yet:

| Caller | Behaviour on `NoBookingMatch` |
|---|---|
| Bridge (the job-creating path) | Caught at [scripts/skydiveos_bridge.py:139-140](scripts/skydiveos_bridge.py#L139-L140) → [`_flag`](scripts/skydiveos_bridge.py#L168-L172): a WARNING log, an entry in `jobs/_bridge_state.json` under `flagged[s3_key] = reason`, and **HTTP 200** `{"status": "flagged"}` (a 5xx would make discovery retry forever). **No job is created; the clip is dropped from the pipeline.** |
| Discovery role resolver | Caught at [ingest/discovery.py:133-139](ingest/discovery.py#L133-L139) → returns `None`, falling back to the registry's static role hint. Never blocks the hand-off. |
| QR identity resolver | Caught at [ingest/qr.py:380-385](ingest/qr.py#L380-L385) → clip keeps `role=None`. |
| `check_match.py` | Prints `FAILED`, exits non-zero ([scripts/check_match.py:74-99](scripts/check_match.py#L74-L99)). |

So today a spec flight's footage produces: a flagged s3_key, a warning line, and
**no job at all**. Note the flag is keyed by `s3_key` and is terminal — a flagged
key is treated like a duplicate on re-notify
([scripts/skydiveos_bridge.py:126-127](scripts/skydiveos_bridge.py#L126-L127)),
so re-running the notify will not retry it.

**What a `load_spec` branch must do to reach a successful match.** Four things,
and only the first is inside `select_match`'s reach:

1. **A new load query.** Because the candidate list is empty *for a reason that
   the timestamp cannot fix* (see C), the branch cannot start from
   `select_match`'s inputs. It needs a second lookup in `resolve_for_staff`:
   loads whose `businessDate`/`departureTime` place them around `captured_at`,
   with the same `_MATCHABLE_STATUSES` filter — independent of the jumpers array.
2. **A jumper-less result shape.** `Candidate` requires `jumper_index: int`,
   `jumper: dict` and `role: str` as non-optional fields
   ([ingest/match.py:101-111](ingest/match.py#L101-L111)), and `MatchResult`
   requires `role`, `staff_id`, `load_id`, `jumper_index`
   ([ingest/match.py:114-137](ingest/match.py#L114-L137)). A load master has no
   jumper index and no customer. Either those fields go optional (they are read
   by the bridge and three scripts — see the sibling sweep) or the branch returns
   a distinct result type.
3. **Skip the customer/package enrichment.** `_build_result` dereferences
   `match.jumper` throughout ([ingest/match.py:480-508](ingest/match.py#L480-L508))
   and derives the package from `jumper.mediaPackage`. A load master's package
   must come from the flyer's role, not a purchase — the nearest existing
   precedent is `SPECULATIVE_PACKAGE_BY_ROLE`
   ([ingest/match.py:172](ingest/match.py#L172)).
4. **Bypass the bridge's two post-match gates**, which reject exactly what a load
   master looks like: `match.package is None` → flag
   ([scripts/skydiveos_bridge.py:141-144](scripts/skydiveos_bridge.py#L141-L144))
   and `not match.customer_email` → flag
   ([scripts/skydiveos_bridge.py:145-146](scripts/skydiveos_bridge.py#L145-L146)).
   A load master has no customer email by definition.

**Can it sit alongside the refusal?** **Yes.** The refusal is one `if not
candidates: raise` in a pure function with a single call site. The natural shape
is a fallback in `resolve_for_staff`: try the jumper-keyed path, and when it
raises `NoBookingMatch` (or when the candidate list is empty), attempt the
load-only resolution. `select_match` itself need not change at all — which
matters, because it is the module's pure, heavily-tested decision core
([tests/test_match.py](tests/test_match.py) exists for it). **No control-flow
restructuring is required.** This stop condition is **not** met.

## C. Load resolution without a jumper — STOP CONDITION MET

**Load resolution is derived from the jumper, not from timestamps.** This is the
audit's headline finding and it contradicts both handoff docs (see the last
section).

The evidence is the query at
[ingest/match.py:440-447](ingest/match.py#L440-L447): its **only** filter is the
jumper predicate. Timestamps enter afterwards, purely as a *narrowing* step over
candidates that the jumper predicate already produced
([ingest/match.py:256-286](ingest/match.py#L256-L286)) — `captured_local` and
`captured_day` are arguments to `select_match`, never to the database query.
There is no code path anywhere in `ingest/match.py` that reads a load by time.

Consequences for the branch shape, stated plainly:

- On a spec flight the cursor yields **zero documents**, so there are zero
  candidates, so `select_match` raises. The clip's timestamp is never consulted.
- The branch is therefore **not** "relax the zero-candidate guard" — relaxing it
  yields nothing to fall back on. It is "add a load lookup the module does not
  have".
- The lookup is nevertheless **feasible against the same schema**:
  [scripts/check_match.py:194-200](scripts/check_match.py#L194-L200) already
  selects loads for a day by `departureTime` alone, and `_in_window`
  ([ingest/match.py:220-224](ingest/match.py#L220-L224)) is a ready-made,
  jumper-free window predicate. The pieces exist; the wiring does not.
- One open question this audit **cannot** answer without the SkydiveOS repo: how
  the flyer is associated with the load at all on a spec flight. The matcher only
  ever reads `jumpers[].instructor` and `jumpers[].assignedCameraman`
  ([ingest/match.py:455-458](ingest/match.py#L455-L458)); nothing in this repo
  reads a load-level crew/staff field. If a spec flyer is not recorded on the load
  document in any form, then "which load was he on" is answerable **only** from
  the timestamp window, with no confirmation that he was aboard — that is a
  product decision, not a code question.

## D. Ambiguous load window

Today the answer is: **ambiguity between loads is resolved causally, not
refused** — but only among jumper-derived candidates.

`select_match` deliberately does **not** refuse when several flight windows
overlap, because `WINDOW_POST` (150 min) is far wider than the gap between loads,
so a staff member's 12:05 clip legitimately sits inside the 10:00, 11:00 and
12:00 windows at once ([ingest/match.py:240-249](ingest/match.py#L240-L249)). It
picks **the latest departure at or before the capture instant**
([ingest/match.py:275-278](ingest/match.py#L275-L278)); a clip predating every
departure falls back to the earliest.

`AmbiguousMatch` is raised in only three places:

1. Candidates exist but **none** falls in any flight window
   ([ingest/match.py:264-269](ingest/match.py#L264-L269)).
2. Two candidates share the **same departure instant** — two jumpers on one load,
   or two loads departing simultaneously
   ([ingest/match.py:282-286](ingest/match.py#L282-L286)).
3. A camera serial suffix owned by >1 staff
   ([ingest/match.py:374-378](ingest/match.py#L374-L378)).

**What "flyer resolved, load unresolved" looks like today: it does not exist as a
distinct outcome.** Every failure — unknown camera, no booking, ambiguous — is a
`FootageMatchError` subclass that the bridge funnels into one undifferentiated
sink: `_flag(s3_key, f"{type(e).__name__}: {e}")`
([scripts/skydiveos_bridge.py:139-140](scripts/skydiveos_bridge.py#L139-L140)).
The exception **class name** is preserved in the reason string, so the
information survives, but:

- it lands in a local JSON file (`jobs/_bridge_state.json`, `flagged` map),
  **not** in anything SkydiveOS can read — there is no exception endpoint, no
  callback, no job record. `_notify_skydiveos`
  ([api/tasks.py:82-131](api/tasks.py#L82-L131)) fires only for jobs that exist.
- the notify returns HTTP 200 `{"status": "flagged", "reason": ...}`
  ([scripts/skydiveos_bridge.py:172](scripts/skydiveos_bridge.py#L172)) — the
  response body is the only channel, and discovery does not persist it.

So "SkydiveOS surfaces it as an exception row" is **new surface area in both
repos**, not a matter of relabelling an existing outcome. On our side it needs a
distinguishable result (flyer resolved + load unresolved) and a way to report it
that outlives the HTTP response.

## E. Job model

**Fields `Job` carries today** ([api/jobs.py:218-308](api/jobs.py#L218-L308)):
`job_id`, `status`, `customer_name`, `customer_email`, `jump_date`, `camera_id`,
`source_path`, `music`, `target_duration`, `package`, `booking_id`,
`instructor_id`, `instructor_name`, `entitlement`, `gallery_token`, `paid_at`,
`payment_reference`, `addons`, `raw_s3_keys`, `last_raw_clip_at`,
`processing_dispatched`, `reject_reason`, `error`, `outputs`, `delivery_links`,
`created_at`, `updated_at`.

**Where the new fields go.** `job_kind`, `load_id`, `load_label` and
`source_job_id` are added to `Job` in [api/jobs.py](api/jobs.py) — each one line,
with `job_kind` needing a default (`"jump"`) so **every existing `job.json` on
disk still validates**; `Job` is loaded with `model_validate_json`
([api/jobs.py:454](api/jobs.py#L454)) on every read, so a required new field
would break every historical job.

**What else validates or enumerates Job shape** — this is the part to plan for:

- **`Job` is `extra="forbid"`** ([api/jobs.py:226](api/jobs.py#L226)). A field
  written anywhere without being declared here raises. `JobStore.update` also
  re-validates the whole model on every write
  ([api/jobs.py:576-581](api/jobs.py#L576-L581)).
- **`JobResponse` is a hand-maintained parallel projection**
  ([api/schemas.py:60-126](api/schemas.py#L60-L126)), also `extra="forbid"`, with
  an explicit field-by-field `from_job`
  ([api/schemas.py:100-126](api/schemas.py#L100-L126)). Anything SkydiveOS must
  see has to be added in **both** places.
- **`CreateJobRequest` is `extra="forbid"`**
  ([api/schemas.py:29](api/schemas.py#L29)) and `create_job` splats it straight
  into `Job` ([api/app.py:837-838](api/app.py#L837-L838)). So a caller (the
  bridge) cannot send `job_kind`/`load_id` until they are declared here too —
  today that POST would **422**.
- **`Package` is a closed `StrEnum` with a non-exhaustive-safe lookup.**
  `display_label` is a dict indexed by `self`
  ([api/jobs.py:129-135](api/jobs.py#L129-L135)) — a new enum member added
  without a label entry raises `KeyError` at gallery-render time
  ([api/app.py:1510](api/app.py#L1510),
  [api/delivery.py:420](api/delivery.py#L420)). Three more properties enumerate
  members explicitly ([api/jobs.py:94-119](api/jobs.py#L94-L119)).

**Is stop condition 3 met?** **No, on the intended design; yes if you take a
wrong turn.** `job_kind` as a *new field* touches a closed field list
(`extra="forbid"`) that is mechanical to extend, and nothing enumerates
`job_kind` because it does not exist yet. But if the load master is instead
modelled as a **new `Package` member**, it lands in a closed enum that four
properties and two render paths enumerate — including the `display_label` dict
that `KeyError`s. Keep the load master's `package` one of the existing values
(the flyer's normal `external` cut) and distinguish it with `job_kind`.

Two more shape notes, both cheap but easy to miss:

- `customer_name` is non-optional with a default `"Valued Skydiver"`
  ([api/jobs.py:232](api/jobs.py#L232)), and the archive uses it as a **folder
  name** ([api/archive.py:337-339](api/archive.py#L337-L339)). A load master will
  file under that literal string unless `load_label` feeds the archive path.
- `media_state` ([api/lifecycle.py:57-89](api/lifecycle.py#L57-L89)) is derived
  from `(status, entitlement, paid_at)` only, so it needs no change — a load
  master projects onto the same states.

## F. Dispatch and debounce — would a jumper-less job pass?

**The pipeline gates are entirely package- and file-driven. A load master passes
all of them unchanged.** Nothing between upload and render reads a customer, a
booking, or a jumper.

The chain: `POST /jobs/{id}/upload` stages the clips
([api/app.py:944-963](api/app.py#L944-L963) for bytes,
[993-1044](api/app.py#L993-L1044) for `s3_key`) → `ingest_s3_job` stamps
`last_raw_clip_at` and arms the settle check
([api/tasks.py:451-473](api/tasks.py#L451-L473)) → `raw_clips_settled_job`
re-schedules itself until the job has been quiet for
`raw_clip_settle_seconds` (default **180 s**,
[api/config.py:142](api/config.py#L142)) then calls `_dispatch_processing`
([api/tasks.py:495-554](api/tasks.py#L495-L554)) → `_dispatch_processing` checks
only `job.processing_dispatched` and `job.package.uses_scene_pipeline`
([api/tasks.py:476-492](api/tasks.py#L476-L492)).

Two gates that *do* apply, neither jumper-related: the byte-upload path
([api/app.py:969-972](api/app.py#L969-L972)) dispatches immediately with no settle
window, and `write_booking` writes a `booking.json` sidecar from the job's own
fields ([api/app.py:265-277](api/app.py#L265-L277)) that the scene pipeline reads
back ([api/selfie.py:2678](api/selfie.py#L2678)) — it will carry
`"Valued Skydiver"` for a load master unless the label is threaded through.

**Where a jumper-less job does hit friction — after the render, not before:**

1. **The bridge's debounce key is `(load_id, jumper_index)`**
   ([scripts/skydiveos_bridge.py:148](scripts/skydiveos_bridge.py#L148)). A load
   master has no jumper index, so it needs a sentinel or a widened key — and note
   the key must **not** collide with a real jumper on the same load, or the
   flyer's clips would be folded into a customer's job.
2. **The bridge's debounce default is 900 s**
   ([scripts/skydiveos_bridge.py:79](scripts/skydiveos_bridge.py#L79),
   [318](scripts/skydiveos_bridge.py#L318)), which is the *right* value but is
   contradicted by CLAUDE.md (see below).
3. **Delivery requires a customer email or a SkydiveOS callback.**
   `deliver_to_customer` raises when the gallery can be neither emailed nor
   forwarded ([api/delivery.py:431-438](api/delivery.py#L431-L438)) — "no
   `customer_email`/SMTP and no `SKYDIVEOS_API_BASE`". A load master has no
   customer by definition, so **it must either have `SKYDIVEOS_API_BASE` set (the
   normal production case, so the link is forwarded rather than emailed) or not
   go through `deliver_job` at all.** With `AUTO_DELIVER=1`
   ([api/tasks.py:158-172](api/tasks.py#L158-L172)) every finished render is
   auto-approved and delivery fires, so this path *will* be reached by default.
4. **`preview_only` + no `PUBLIC_BASE_URL` is refused at job creation**
   ([api/app.py:823-835](api/app.py#L823-L835)). If load masters are sold to
   non-buyers they are Path B by nature, so `PUBLIC_BASE_URL` becomes a hard
   prerequisite.

## Sibling sweep — the blast radius

**Everything that consumes `MatchResult`** (all would need to tolerate a
jumper-less result, or be explicitly excluded from the new path):

| Consumer | Uses | Note |
|---|---|---|
| [scripts/skydiveos_bridge.py:136-166](scripts/skydiveos_bridge.py#L136-L166) | `load_id`, `jumper_index`, `package`, `customer_email`, `customer_name`, `staff_name`, `booking_id`, `entitlement`, `load_number` | The only job-creating consumer; both post-match gates reject a load master |
| [ingest/discovery.py:114-141](ingest/discovery.py#L114-L141) | `.role` only | Degrades to `None`; safe |
| [ingest/qr.py:339-386](ingest/qr.py#L339-L386) | `.role` only | Degrades to `None`; safe |
| [scripts/check_match.py:74-99](scripts/check_match.py#L74-L99) | `role`, `package`, `customer_name`, `customer_email` | The pre-flight tool would report a spec flight as `FAILED` and exit non-zero |
| [scripts/check_sdcard.py:117-122](scripts/check_sdcard.py#L117-L122) | `customer_name`, `package`, `role` | Diagnostic |
| [scripts/demo_from_load.py:130-179](scripts/demo_from_load.py#L130-L179) | `package` (exits if `None`), `customer_*` | Demo driver |

**Everything that reads Job fields a load master would leave empty or unusual:**

- `api/archive.py` — folder naming from `instructor_name`/`customer_name`
  ([api/archive.py:337-339](api/archive.py#L337-L339)) and a manifest that
  records `package`, `customer_name`, `instructor_name`
  ([api/archive.py:543-554](api/archive.py#L543-L554)). A `load_label` would
  naturally belong in both.
- `api/delivery.py` — email body and subject interpolate `customer_name`
  ([api/delivery.py:216-232](api/delivery.py#L216-L232),
  [276-289](api/delivery.py#L276-L289)); the gallery hero uses
  `package.display_label` ([api/delivery.py:420](api/delivery.py#L420)).
- `api/app.py` — the public gallery renders `customer_name` +
  `package.display_label` ([api/app.py:1492](api/app.py#L1492),
  [1510](api/app.py#L1510)); music slots enumerate
  `package.music_deliverables` ([api/app.py:250](api/app.py#L250),
  [1665-1671](api/app.py#L1665-L1671)).
- `api/tasks.py` — the status callback forwards `booking_id`, `customer_email`,
  `customer_name` as gap-fill identity
  ([api/tasks.py:116-121](api/tasks.py#L116-L121)); a load master has none of the
  three, so SkydiveOS receives an unlinkable job unless `load_id` is added here.
- `scripts/prune_jobs.py:139` — retention branches on `entitlement`, not on
  customer; unaffected.
- `api/lifecycle.py` — unaffected (derived from status/entitlement/paid_at only).

**Not affected:** `edl/*`, `render/*`, `analysis/*`, `metadata/*`,
`api/selfie.py`. The editing pipeline never reads a customer or a load — it reads
`raw/` and `booking.json`. This is why v1's "the load master is the flyer's normal
cut, unfiltered" is genuinely cheap on the editing side.

## Stop conditions

1. **"Load resolution is derived from the matched jumper rather than
   independently from timestamps"** — **MET.** The `loads` query is filtered
   solely by the jumper predicate
   ([ingest/match.py:440-447](ingest/match.py#L440-L447)); timestamps only narrow
   the jumper-derived result. A time-keyed load lookup does not exist in
   `ingest/match.py`.
2. **"The zero-candidate refusal cannot be branched around without
   restructuring"** — **NOT met.** It is a guard clause raising from a pure
   function with one call site
   ([ingest/match.py:251-252](ingest/match.py#L251-L252),
   [473](ingest/match.py#L473)); a fallback fits alongside it and `select_match`
   need not change.
3. **"Job shape is validated by something with a closed field or kind list that
   other features also enumerate"** — **NOT met for new fields** (`Job`'s
   `extra="forbid"` is mechanical to extend, and `job_kind` is enumerated
   nowhere); **would be MET if the load master became a new `Package` member**,
   which four properties and a `KeyError`-prone `display_label` dict enumerate
   ([api/jobs.py:94-135](api/jobs.py#L94-L135)).

Per instruction, this audit reports and proposes no fix. But since stop condition
1 is met, the honest framing of the size estimate is below.

## Size: MEDIUM

Not a parameter, not a restructure. Concretely, `load_spec` v1 requires:

- **a new time-keyed load query** in `ingest/match.py` (the thing C proves does
  not exist), plus a jumper-less result shape and a role/package rule that does
  not read a purchase;
- **a bridge path** that keys debounce without `jumper_index` and bypasses the
  package/email gates;
- **four `Job` fields** plus their `JobResponse`/`CreateJobRequest` mirrors;
- **delivery and gallery handling** for a job with no customer (the
  `SKYDIVEOS_API_BASE` / `PUBLIC_BASE_URL` prerequisites are real, not optional);
- **an exception-row channel** for "flyer resolved, load unresolved", which today
  has no representation outside a local JSON file.

The editing pipeline itself needs nothing. The cost is concentrated in the
matcher's shape and in everything downstream that assumes a customer exists.

## Where handoff docs disagree with the code

- **[CLAUDE.md:164-168](CLAUDE.md#L164-L168)** describes the match as
  "`staffs.goproSerial` → the owning staff … then `captured_at` … → the `loads`
  whose `businessDate`/`departureTime` window fits, then the jumper whose
  `instructor` … is that staff". That reads as **timestamp → load → jumper**, and
  it is the reading that makes `load_spec` look like a small branch. The code is
  **staff → jumper-filtered loads → timestamp narrowing**
  ([ingest/match.py:440-473](ingest/match.py#L440-L473)). The doc's own ordering
  does not exist as a code path. This is the single most consequential
  disagreement in this audit.
- **[SKYDIVEOS_INTEGRATION.md:49-50](SKYDIVEOS_INTEGRATION.md#L49-L50)** repeats
  the same inverted ordering for the QR flow ("the load whose window contains the
  instant, the jumper whose `instructor`/`assignedCameraman` is that staff").
  Same correction applies to `resolve_for_staff`.
- **[CLAUDE.md:33-35 of the matcher docstring](ingest/match.py#L33-L35)** and
  CLAUDE.md's "It **refuses and flags** on ambiguity (0 or >1 jumpers)" are
  **accurate** — worth stating, since two of three doc claims checked here were
  not.
- **[CLAUDE.md:359](CLAUDE.md#L359)** documents the bridge as
  `[--debounce 20]`. The actual default is **900 s**
  ([scripts/skydiveos_bridge.py:79](scripts/skydiveos_bridge.py#L79),
  [318](scripts/skydiveos_bridge.py#L318)), and the bridge's own docstring
  explains *why* 20 s was wrong — at 20 s a card pulled over a dropzone uplink
  split one jump into four jobs and emailed one customer four times (observed
  2026-08-06, [scripts/skydiveos_bridge.py:22-29](scripts/skydiveos_bridge.py#L22-L29)).
  Anyone copying the CLAUDE.md invocation verbatim re-introduces a fixed bug.
