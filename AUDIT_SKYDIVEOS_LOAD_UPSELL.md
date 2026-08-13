# AUDIT: SkydiveOS side of the load-spec upsell — spec vs. shipped

**2026-08-10.** Read-only audit of `~/Desktop/gritbyte-labs/skydiving-os` against Pramod's
`skydiveos-build-spec-load-upsell.md` (Parts A–E) **and** against what the media module
actually shipped today. Every claim carries a `file:line`. Nothing in either repo was
changed by this audit.

Delivery target discussed on Slack: **Friday 2026-08-14** (4 working days from today).

## Verdict up front

**The build split in Part E no longer matches reality, and that is mostly good news: the
SkydiveOS-side work is roughly half of what Part E assigns it.**

Three things drive that:

1. **A1 is already built.** `Load.specCameraFlyer` exists — model, staff picker dialog,
   role-collision sweep, and tests. Part E item 1 is **done**.
2. **The fan-out moved to the media module.** Part E item 3 assigns "bridge fan-out logic +
   webhook branch for the Daniel case" to SkydiveOS. The module now does the whole fan-out
   itself (`api.tasks.fan_out_load_job`), triggered by the master's own render. SkydiveOS
   needs **no** fan-out code — only the master-creation branch.
3. **`scene_filter` is cancelled, not deferred.** It cannot do what C2 asks of it, so v1 is
   **spec flights only**. That deletes the "two jobs, one clip set" case from step 4 and
   removes the riskiest part of the spec.

The one **new** finding that changes a decision already taken: **`Load.specCameraFlyer`
exists in the shared DB, so the module no longer has to resolve a spec flight from
timestamps alone.** See §4 — this is the highest-value item in the audit.

---

## 1. What the media module shipped today (for reference)

| Concept | Shipped as |
|---|---|
| Job kinds | `api.jobs.JobKind` = `jump` \| `load_master` \| `load_child` |
| Spec-flight match | `ingest.match.resolve_load_for_staff` + `select_load` (new time-keyed load query), `NotSpecFlight` |
| Master | `package=video_only`, `entitlement=preview_only`, no customer, no email |
| Fan-out | `api.tasks.fan_out_load_job` — freefall guard → S3 (`presign=False`) → children + tiles |
| Child gallery | `load_child` + `source_job_id`; files from the master, **lock state from the child** |
| Buyer's tile | `load_video` in `PURCHASABLE_ADDONS`; served at `GET /j/{code}/load/{name}` |
| Retention | pointer-job guard in `scripts/prune_jobs.py` |

## 2. Divergences from the build spec — read this before writing code

| # | Build spec says | Shipped / audited reality | Action for SkydiveOS |
|---|---|---|---|
| D1 | `"scene_filter": "shared_only"` on the master (C2 Shape 1) | **Not implemented and not planned.** `AUDIT_SCENE_LABELS.md` proves the promise is unkeepable: `intro_interview` vs `boarding` is a *clip-position* heuristic over identical telemetry, boarding footage deliberately lives inside `intro_interview` (`api/selfie.py:1143-1149`), `takeoff`/`plane` never appear without GPS lock, and no label carries identity. A customer's interview filmed mid-card would land in four strangers' galleries. | **Drop the field.** Do not send it — `CreateJobRequest` is `extra="forbid"`, so an unknown field is a **422 per clip**. |
| D2 | Assigned-flyer loads also produce a master ("two jobs, one clip set", step 4 branch) | **Refused** by `ingest.match.NotSpecFlight`. v1 = spec flights only, because D1 is what made the assigned case safe. | Delete the "Yes →" branch of step 4. An assigned flyer behaves exactly as today. |
| D3 | `job_kind` default `"standard"`, package `"external"` | Default is `"jump"`; master package is **`video_only`** (house cut, and no photo set — a load's stills are of strangers). | Send `job_kind: "load_master"`, `package: "video_only"`. |
| D4 | SkydiveOS fans out children (step 6, Part E item 3) | Module does it (`fan_out_load_job`), gated on a **freefall scene** in the master's manifest. | **Build nothing.** Children appear on their own once the master renders. |
| D5 | Daniel's case: on capture, create a child with `entitlement: edited_download` (step 9) | No child. His **existing** job carries `source_job_id`, the tile is `addons["load_video"]`, fulfilled in the gallery he already has. | **Nothing to build** — the purchase rail is already item-generic; just price `load_video`. See §6 item 3. |
| D6 | `load_label: "Load 14 · 2:30 PM"` | Module sets `"Load 14"`; it also names the archive folder. | Either is accepted; a `·` in the label is fine but it becomes a folder name. |
| D7 | Eligibility computed by SkydiveOS before fan-out (C2) | Module decides from `load_roster.bought_media`, derived from `jumper.mediaPackage` by the same pure `package_and_entitlement_for` a normal match uses. | Send the roster on the master; don't pre-filter. |

**Contract shapes that are correct as specced and already work:** `job_kind`, `load_id`,
`source_job_id` as field *names* (C2), the status-callback additions (C3), and unlock (C4,
verified unchanged).

## 3. SkydiveOS-side status, per Part A/E item

| Item | Status | Evidence |
|---|---|---|
| **A1** `Load.specCameraFlyer` + manifest button | ✅ **DONE** | `backend/src/models/Load.js:290`; picker `frontend/src/components/manifest/SpecCameraFlyerDialog.js`; one-role-per-load collision sweep `backend/src/utils/staffLoadRoles.js:118`; tests `backend/src/__tests__/specCameraFlyerAssignment.test.js`, `bug340StaffLoadRoles.test.js:165-194`. Model comment confirms v1 scope: not in `seatsUsed()`, mints no staff pay. |
| **A2** per-jumper `media_state` chips | ❌ **NOT BUILT** | No `mediaState` / `LOCKED_PREVIEW` reference anywhere in `frontend/src`. Data already arrives (`AutoEditJob.mediaState`, `models/AutoEditJob.js:163`) — render-only work. |
| **A3** exception-queue "load unresolved" row | ❌ **NOT BUILT** (no distinct queue found) | No manual-attach/exception surface located in `frontend/src` or `backend/src/routes`; the only `exception` hit in `ManifestPage.js:1606` is an unrelated comment. |
| **A4** two SKUs + tiles | ⚠️ **BLOCKED, PARTLY OBSOLETE** | `AUDIT_A4_SKUS.md` (same date) stopped at stop condition D: `$UPSELL_TILES` is a media-module **env var**, so SkydiveOS cannot register a tile. **This audit supersedes that for the load video:** the tile is now generated in module code per job (`api/upsell.py:load_video_tile`), not from env — so no tile registration is needed at all. `LOAD_VIDEO_UNLOCK` still needs a **price**; `ULTIMATE_NEXT_JUMP` remains a booking-funnel question, unresolved (see that audit's open question 2). |
| **A5** staff admin | ✅ nothing needed, as specced | `staffs.goproSerial` + QR markers already live. |
| **E2/C2** master-creation branch | ❌ **NOT BUILT** — the real gap | `autoEditOrchestrationService.js` has no `specCameraFlyer` reference and no spec branch; it matches jumpers only (`jumperFillsCameraRole:91`, `capturedWithinLoadWindow:104`). `AutoEditJob` has no `jobKind`/`sourceJob` (`models/AutoEditJob.js`, 256 lines — `load:121`, `jumperId:123`, `mediaState:163` exist). |
| **C4** unlock on capture | ✅ **WORKS** | `financeController.js:393` (`paymentScope === 'media-unlock'`, returns early at `:414`) → `mediaUnlockService.fulfillCapturedPayment:263` → `mediaUnlockRetryService.fulfill:124` → `autoEditService.js:299` `POST /jobs/{id}/unlock`. Cross-rail guard at `paymentEventHandler.js:133-138`. |

**Production path, for the record:** `mediaRoutes.js:84` → `mediaController.uploadRawFromCamera`
→ `mediaService.registerRawFromS3` → `autoEditOrchestrationService.processRawFootage{,ByStaff}`
(`mediaService.js:308,312`) → `autoEditService.createJob` (`autoEditService.js:140`). So in
production **SkydiveOS creates the jobs**; the module's `scripts/skydiveos_bridge.py` is the
local stand-in and the executable reference for exactly this branch.

## 4. The finding that changes a decision — `Load.specCameraFlyer` is available

When the module's spec-flight branch was scoped this morning, the recorded premise was
"there is no load-level crew field, so the capture timestamp is the only evidence tying a
spec flyer to a load." That was true of the fields **the module reads**
(`ingest/match.py` only ever touched `jumpers[].instructor` / `assignedCameraman`) — but it
is **no longer true of the shared database**: `Load.specCameraFlyer` is a real, populated,
UI-assigned field (`models/Load.js:290`), and the module reads the same `loads` collection.

Why it matters: the module currently accepts a load if the clip falls inside the flight
window and the flyer holds no jumper slot, then leans on a **freefall-scene guard** to catch
footage shot between loads. That guard is a proxy for evidence. `specCameraFlyer` **is** the
evidence — it is ops explicitly stating "this staff member flew this load on spec."

Recommended change, module side (small, and it removes a guess from a money path):

* in `resolve_load_for_staff`, prefer loads whose `specCameraFlyer` matches the staff id
  (using the existing `_staff_id_variants` so a QR's string id matches a stored ObjectId);
* keep the timestamp window as the fallback for a dropzone that hasn't adopted the slot yet,
  so nothing regresses;
* keep the freefall guard regardless — it is cheap and it also catches a mis-set slot.

This is the audit's one recommendation that affects already-shipped code. It is **not** a
prerequisite for Friday.

## 5. Naming hazard — two different "load masters"

`load.loadMaster` **already exists in SkydiveOS** and means the *person* responsible for a
load (a safety role): `frontend/src/components/manifest/LoadCard.js:526`,
`models/ManifestConfig.js:56` (`loadMasterMustBeOnLoad`),
`services/manifestValidationEngine.js:260`.

The module's `load_master` is a **job kind** — the master *render*. Two unrelated meanings,
one word, in code that now sits side by side. Anyone reading
`if (enf.loadMasterMustBeOnLoad && !hasLoadMaster)` next to a `job_kind: "load_master"`
payload will misread one of them. Recommendation: on the SkydiveOS side call it
**`specLoadJob` / `loadVideoJob`**, never `loadMaster`, and say so in a comment where the
payload is built. The wire value stays `"load_master"` (it is the module's contract).

## 6. Remaining SkydiveOS work, in build order

| # | Item | Size | Notes |
|---|---|---|---|
| 1 | **Spec branch in `processRawFootage`** — when no jumper matches and `Load.specCameraFlyer` is this staff, create ONE `load_master` job per (load, flyer) and attach the clips | **M** | The only true blocker. Mirror `scripts/skydiveos_bridge.py::_job_payload` verbatim: `job_kind`, `load_id`, `load_label`, `load_roster`, `package: "video_only"`, `entitlement: "preview_only"`, **no** `customer_email`, **no** `scene_filter`. Must be idempotent per (load, flyer) — a second master means a second render and a second set of galleries. |
| 2 | **`jobKind` + `sourceJob` + `load` on `AutoEditJob`** so callbacks for children can be stored and grouped | **S** | `load` already exists (`:121`); add the two new ones. Children arrive via callback with `origin: 'pipeline-callback'` — the enum already allows it (`:130`). |
| 3 | **A `load_video` price in Media settings** — the whole of the Daniel case | **CONFIG ONLY, no code** | See below. |
| 4 | **A2 manifest chips** | **S** | Render-only; data already stored (`AutoEditJob.mediaState:163`). |
| 5 | **A3 exception row** | **S** | Trails launch — operational polish, and no surface exists to extend yet. |
| 6 | `ULTIMATE_NEXT_JUMP` | — | **Descope for Friday.** `AUDIT_A4_SKUS.md` leaves a real open question (Ultimate may be an add-on, not a base package, so `/book/:slug` may not reach it). Pure booking funnel, no media dependency — it can ship later without touching this feature. |

### Item 3 in detail — the Daniel case needs no code at all

Better than both the build spec and this audit's own first pass. The purchase rail is
already **item-generic** end to end:

* `mediaUnlockService.resolvePriceCents(item)` (`:93`) looks the price up in
  `MediaConfig.pricing.items[item]` — a **plain Map**, and the model says why:
  *"`items` is a plain Map so an operator can add a tile key without a schema change"*
  (`models/MediaConfig.js:202-205`).
* `NON_PURCHASABLE_ITEMS` is a **blocklist** (the `rebook` promo), not an allowlist, and
  `normalizeItem` (`:66`) just lower-cases — so any newly priced key is purchasable.
* `autoEditService.js:299-302` **already forwards** `item` to the pipeline
  (`...(item && item !== 'unlock' ? { item: String(item) } : {})`), and the module already
  accepts `load_video` (`PURCHASABLE_ADDONS` in `api/app.py`).
* An unpriced item is **rejected, never defaulted to free** (`:100-106`) — so this cannot
  silently sell the load video for $0.

So Part A4's "register the SKU" and step 9's "one extra branch in the webhook handler" both
reduce to: **set a `load_video` price in Media settings.** Keep it in step with the module's
`PREVIEW_PRICE_DISPLAY`, which is what the tile *displays* — per `AUDIT_A4_SKUS.md` §4 that
two-surface split is the Bug-159 divergence pattern, and it now applies to this tile too.

**Realistic Friday scope: items 1 and 2 (code) + item 3 (config).** That makes the Load-17
walkthrough work end to end in production. Items 4–5 are visibility; item 6 is a separate
sale.

## 7. What NOT to build (would be wasted or harmful)

* **Fan-out logic** — the module owns it (D4).
* **`scene_filter`** — cancelled; sending it 422s every clip (D1).
* **A child job for a media buyer** — breaks "one customer, one link" (D5).
* **A tile registration surface for the load video** — it is module code now, not env (A4).
* **A second master for an assigned-flyer load** — refused by the module (D2).

## 8. Implementation spec for the SkydiveOS side

Three changes. Everything else in Part A/E is either already built, cancelled, or config.

### 8.1 `backend/src/models/AutoEditJob.js` — three fields + one index

The `booking` unique index is **partial** (`:246-249`,
`partialFilterExpression: { booking: { $type: 'objectId' } }`), so booking-less rows already
coexist — a load master needs no change there. It does need its own uniqueness key:

```js
// Which KIND of job this mirrors (pipeline api.jobs.JobKind). 'jump' is every
// job that existed before the load-spec upsell.
jobKind: { type: String, enum: ['jump', 'load_master', 'load_child'], default: 'jump', index: true },
// load_child only: the load master whose render its gallery streams.
sourceJob: { type: mongoose.Schema.Types.ObjectId, ref: 'AutoEditJob', default: null },
// load_master only: the staff member who flew it on spec (Load.specCameraFlyer).
specFlyer: { type: mongoose.Schema.Types.ObjectId, ref: 'Staff', default: null },
```

```js
// One master per (load, spec flyer) — the load-master analogue of the `booking`
// unique index, and for the same reason: the six-jobs-for-one-booking incident.
// A spec load arrives as several clips; without this, each notify mints another
// master, each renders, and each fans out its own set of galleries.
autoEditJobSchema.index(
  { load: 1, specFlyer: 1 },
  { unique: true, partialFilterExpression: { specFlyer: { $type: 'objectId' } } },
);
```

### 8.2 `autoEditOrchestrationService.matchFootageByStaff` — the spec branch

Exact hook: the **zero-candidate refusal** at `:253-255`. Today it returns
`refuse('unmatched', 'no manifested jump has this staff as instructor or cameraman')` —
which is precisely what a spec flight looks like, because the flyer fills no jumper slot.

```js
if (candidates.length === 0) {
  // A camera flyer sent up on an open seat fills no jumper slot, so the query above
  // can never match him — this is exactly the branch he lands in. Load.specCameraFlyer
  // is ops stating he flew this load on spec; if one of those loads' windows contains
  // the clip, his card is ONE load master for the whole manifest.
  const spec = await this.matchSpecFlight({ staffId, capturedAt });
  if (spec) return spec;
  return refuse('unmatched', 'no manifested jump has this staff as instructor or cameraman');
}
```

`matchSpecFlight` mirrors the existing narrowing, with one deliberate difference:

```js
async matchSpecFlight({ staffId, capturedAt }) {
  const loads = await Load.find({
    status: { $in: STAFF_MATCH_LOAD_STATUSES },
    specCameraFlyer: staffId,          // ← the evidence; NOT a timestamp guess
  }).select('jumpers status businessDate departureTime loadNumber specCameraFlyer').lean();
  if (!loads.length) return null;

  const tz = await getDzTimezoneCached();
  const capturedDzMs = dzWallClockMs(new Date(capturedAt).getTime(), tz);
  // The window is MANDATORY here (unlike the jumper path, where a lone candidate is
  // accepted on the jumper predicate's strength): a staff member may hold the spec slot
  // on four loads in a day, so only the window says which one this clip is from.
  const inWindow = loads.filter((l) => {
    const dep = l.departureTime ? new Date(l.departureTime).getTime() : null;
    return dep != null
      && capturedDzMs >= dep - STAFF_WINDOW_BEFORE_DEPARTURE_MS
      && capturedDzMs <= dep + STAFF_WINDOW_AFTER_DEPARTURE_MS;
  });
  if (!inWindow.length) return null;   // shot between loads → not a spec flight
  // Latest departure at or before the clip — footage can't precede its own flight.
  const departed = inWindow.filter((l) => new Date(l.departureTime).getTime() <= capturedDzMs);
  const pool = departed.length ? departed : inWindow;
  const load = pool.reduce((a, b) =>
    new Date(b.departureTime) > new Date(a.departureTime) ? b : a);

  return {
    status: 'matched',
    rule: 'spec-flight',
    reason: `spec camera flyer on load ${load.loadNumber ?? load._id}`,
    kind: 'load_master',              // ← the caller branches on this
    loadId: load._id,
    loadNumber: load.loadNumber ?? null,
    roster: (load.jumpers || []).map((j, i) => ({ jumper: j, index: i })),
  };
}
```

Then in **`processRawFootageByStaff`** (`:853-889`), before the normal booking path:

```js
if (match.status === 'matched' && match.kind === 'load_master') {
  media.load = match.loadId;                       // no booking, no customer — correct
  const result = await this.startOrAttachLoadMasterJob({
    loadId: match.loadId, loadNumber: match.loadNumber, roster: match.roster,
    specFlyerId: staff?._id, flyerName: staffFullName(staff),
    s3Key: media.s3Key, cameraId, capturedAt,
  });
  /* …same stamp/attach bookkeeping as the jumper path… */
  return media;
}
```

### 8.3 `startOrAttachLoadMasterJob` — reserve-then-create, keyed on (load, flyer)

Copy `_reserveAndCreatePipelineJob` (`:546`) and change only the key and the payload. **Do
not skip the CLAIM step** (`pipelineCreateClaimedAt` stale-TTL CAS): a spec load arrives as
several clips, and two concurrent notifies would otherwise mint two masters — two renders
and two sets of galleries to the same customers. That is the incident the header at `:15-42`
already records once.

The pipeline body — mirror `scripts/skydiveos_bridge.py::_job_payload` exactly:

```js
await autoEditService.createJob({
  job_kind: 'load_master',
  load_id: String(loadId),
  load_label: `Load ${loadNumber ?? ''}`.trim(),   // also names the archive folder
  package: 'video_only',                            // house cut; no photo set
  entitlement: 'preview_only',                      // nobody bought it — and this is
                                                    // what makes previews get rendered
  customer_name: `Load ${loadNumber ?? ''}`.trim(), // files as {date}/{flyer}/Load 17/
  instructor_name: flyerName,
  jump_date: dzDateString(capturedAt, tz),
  load_roster: roster.map(({ jumper, index }) => ({
    jumper_index: index,
    customer_name: /* resolve from jumper.customer */ null,
    customer_email: /* resolve from jumper.customer */ null,
    booking_id: jumper.booking ? String(jumper.booking) : null,
    bought_media: !['', 'none'].includes(String(jumper.mediaPackage || '').trim().toLowerCase()),
  })),
}, { identity });
```

**No `customer_email`. No `scene_filter`** — `CreateJobRequest` is `extra="forbid"`, so an
unknown field is a 422 on every clip.

Attach the clips with the existing `_attachCamera` / `attachS3Key` path, `cameraRole: null`.

### 8.4 What happens next needs no SkydiveOS code

The module renders the master, then fans out on its own: child galleries for the no-media
jumpers (emailed by `AUTO_DELIVER`, the recommended single sender) and a `source_job_id`
stamp for the media buyers. Each child then calls back with
`job_kind: "load_child"`, `load_id`, `jumper_index`, `source_job_id` — which
`_adoptPipelineJob` (`:989`) already handles for unknown job ids. Store the four new fields
on adoption and the A2 chips have everything they need.

> **Module-side gap closed today while writing this audit:** the status callback did not
> carry `job_kind` / `load_id` / `jumper_index` / `source_job_id` (build spec C3). Without
> them an adopted child is a booking-less orphan SkydiveOS cannot group or label. Now sent
> — `job_kind` unconditionally (default included, so you branch on a value, not on
> absence), the other three when known.

## 9. Not audited

Frontend rendering details of A2/A3 beyond existence; `ULTIMATE_NEXT_JUMP`'s booking-funnel
mechanism (covered by `AUDIT_A4_SKUS.md`); the mobile app; whether any dropzone has
populated `specCameraFlyer` in production data; SMS/e-mail sender ownership (step 7 of the
spec asks for ONE sender — the module's `AUTO_DELIVER` is recommended and is what ships,
but SkydiveOS's own sender was not inspected for a double-send risk).
