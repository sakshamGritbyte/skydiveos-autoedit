# DESIGN — Per-deliverable entitlement (per-clip watermark + per-clip unlock)

**Date:** 2026-08-11 · **Phase:** architecture only, no code changed · **Repos:** `skydiveos-autoedit` (the authority) + `skydiving-os` (the billing authority)

**Supersedes** the Phase-4 deferral in [`AUDIT_WATERMARK_PER_DELIVERABLE.md §7.4`](../skydiving-os/AUDIT_WATERMARK_PER_DELIVERABLE.md) — the product decision is confirmed: watermarking is chosen **per deliverable**, not per job.

---

## 0. The requirement, restated as invariants

```text
Existing Job
  full_video      → UNLOCKED / clean          (bought with the booking)
  exit_sequence   → WATERMARKED / locked      (staff added it, Watermarked = YES)
  selfie_clip     → UNLOCKED / clean
```

Five invariants the whole design is built to hold:

| # | Invariant |
|---|---|
| **I1** | `Job.entitlement` is **never mutated** to express a mixed state. It stays what it has always been: *did the customer buy media with their booking* — and it is the **default** for any deliverable with no explicit state. |
| **I2** | A deliverable's lock state is decided by `entitlement_for(job, name)` — **never** by the URL, the filename, the request, or a client hint. |
| **I3** | Unlocking deliverable *X* changes **only** *X*. Unlocking the job (the legacy no-target unlock) moves the **default** and therefore only the deliverables that inherit it — an explicit entry is never touched. |
| **I4** | Empty map ⇒ byte-identical behaviour to today. Every existing gallery, price, email, archive manifest and pruner decision is unchanged. |
| **I5** | A customer who paid for their video through the booking is **never** charged again because we introduced this. The offer's `requiresPayment` becomes deliverable-aware, and it **fails closed** (refuse the sale) when it cannot prove a deliverable is locked. |

---

## 1. Data model

### 1.1 Pipeline — `api/jobs.py`

`Job` is `ConfigDict(extra="forbid")`, so every field is explicit. Three additions:

```python
class DeliverableAccess(BaseModel):
    """Lock state for ONE deliverable — the paywall, one file at a time.

    Presence in Job.deliverable_access is what makes this deliverable's state
    EXPLICIT: absence means "inherit Job.entitlement", which is what every job
    written before this field did and still does (I4).
    """
    model_config = ConfigDict(extra="forbid")

    entitlement: Entitlement
    #: True when this deliverable was BORN locked — staff's "Watermarked = YES", or
    #: the job default at registration time. IMMUTABLE: `entitlement` moves when the
    #: customer pays, this does not. It is what tells a refund/relock, an operator
    #: browsing the archive, and reconciliation that money was owed on this file.
    born_locked: bool = False
    #: Epoch seconds of the captured per-deliverable unlock (None = never paid).
    paid_at: float | None = None
    #: SkydiveOS's captured-transaction id for that unlock. Same audit rule as
    #: Job.payment_reference: an unlock must be attributable to real money.
    payment_reference: str | None = None
    #: Audit trail of reversals: [{"at", "refund_reference", "by"}]. Append-only —
    #: a relock never erases the purchase that preceded it (§17).
    refunds: list[dict[str, str | float]] = Field(default_factory=list)


class ManualDeliverable(BaseModel):
    """A staff-cut clip registered as an output — the registry of record for it.

    Job.outputs is REWRITTEN wholesale by every render (api/selfie.py, 4 sites), so
    a manual clip cannot live only there or a re-render deletes it (§15). This map
    is the durable record; JobStore.set_pipeline_outputs merges it back in.
    """
    model_config = ConfigDict(extra="forbid")

    path: str
    #: Gallery card label ("Exit sequence"). api.gallery._VIDEO_META is module-level
    #: and knows only the six pipeline names; without this a clip reads "Exit Sequence"
    #: at best and "Clip 1" at worst.
    label: str | None = None
    #: Provenance so a re-cut is idempotent and auditable.
    source: dict[str, object] = Field(default_factory=dict)  # {kind, s3_key|file, start, end}
    created_at: float = 0.0
    created_by: str | None = None       # principal.instructor_id, or "service"
```

on `Job`:

```python
#: Per-deliverable lock state. EMPTY for every job that predates this field and for
#: every job whose deliverables all share the job's default — which is the normal
#: case. A name absent here resolves to `entitlement` (see `entitlement_for`).
deliverable_access: dict[str, DeliverableAccess] = Field(default_factory=dict)
#: Staff-added clips, keyed by deliverable name. The merge source for `outputs`.
manual_deliverables: dict[str, ManualDeliverable] = Field(default_factory=dict)
```

### 1.2 The resolver — one function, every consumer

Lives in `api/jobs.py` (already imported by `app`, `preview`, `delivery`, `archive`, `lifecycle`, `tasks` and `scripts/prune_jobs.py`; **pure**, no I/O, no settings):

```python
def entitlement_for(job: Job, name: str) -> Entitlement:
    """This deliverable's lock state. The ONLY way to ask the question (I2)."""
    entry = job.deliverable_access.get(name)
    return entry.entitlement if entry is not None else job.entitlement


def locked_deliverables(job: Job) -> frozenset[str]:
    """Every video deliverable of this job that is behind the paywall."""
    names = [n for n in (job.outputs or {}) if n != "photos"] or ["final"]
    return frozenset(n for n in names if entitlement_for(job, n) is Entitlement.preview_only)


def any_locked(job: Job) -> bool: ...
def all_locked(job: Job) -> bool: ...
```

`locked_deliverables` deliberately falls back to `["final"]` for the classic single-master pipeline, matching `_gallery_videos` / `render_job_previews` / `collect_deliverables`.

### 1.3 SkydiveOS — `models/AutoEditJob.js`

Additive, all defaulted:

```js
// Mirror of the pipeline's per-deliverable lock state, from the status callback.
// ADVISORY, like `entitlement` above it: the pipeline is the authority. It exists so
// the PUBLIC offer page can price and gate without a network call to the pipeline
// (the existing rule at mediaUnlockService.js:143-148). Absent key → fall back to
// `entitlement`; and see §7.4 for the fail-closed rule when it disagrees.
deliverableEntitlements: { type: Map, of: String, default: () => new Map() },

// Staff-added clips on this job (what we asked the pipeline to register, and the
// price key WE chose for each — the pipeline never influences price).
manualDeliverables: [{
  name: String, label: String, media: {type: ObjectId, ref: 'Media'},
  watermarkRequested: Boolean, priceKey: String,
  registeredAt: Date, by: {type: ObjectId, ref: 'Staff'},
}],
```

`mediaUnlock.items` (Map) and `mediaUnlock.fulfillments[]` gain a **composite key** / a `deliverable` field — §8.

### 1.4 `models/Finance.js` — the payment record

One additive subdoc on `Payment`, so a media sale can *state* what it bought instead of only saying it in `notes`:

```js
// Set only when paymentScope === 'media-unlock'. What this money bought, structurally,
// for reconciliation and for a refund that needs to know what to re-lock.
mediaSale: {
  autoEditJobId: { type: String, default: null },
  item:          { type: String, default: null },   // 'unlock' | 'raw' | 'photos' | …
  deliverable:   { type: String, default: null },   // null = the whole job (legacy shape)
},
```

No index change: this row is always found by `transactionId` / `idempotencyKey`.

---

## 2. Migration & backward compatibility

**There is no data migration.** That is the point of I4.

| Surface | Legacy state | Behaviour |
|---|---|---|
| `job.json` on disk | no `deliverable_access` key | pydantic default `{}` → `entitlement_for` returns `job.entitlement` for every name → identical serving, previews, delivery, pruning |
| `Job.entitlement = preview_only` | `{}` | every output locked, one CTA, one price — exactly today |
| `Job.entitlement = edited_download` | `{}` | every output clean — exactly today |
| `AutoEditJob.deliverableEntitlements` | absent | Map default `{}` → offer falls back to `entitlement` → today's `requiresPayment` |
| `mediaUnlock.items` | keys `unlock` / `raw` / `photos` (never contain `:`) | `purchaseRecordFor` unchanged; composite keys are a disjoint namespace (§8.3) |
| `mediaUnlock.fulfillments[]` in flight at deploy | no `deliverable` | `attempt()` sends no `deliverable` → pipeline does the job-level unlock → correct |
| `POST /jobs/{id}/unlock` from an older SkydiveOS | no `deliverable` | today's exact behaviour, byte for byte |
| `CHECKOUT_URL_TEMPLATE` without `{deliverable}` | — | `str.format` tolerates the extra kwarg. **But** a locked clip whose CTA can't carry a target renders as **text**, not a link (§5.4) — otherwise it would charge the job-level unlock |

`extra="forbid"` forbids *unknown* fields on load; adding a field **with a default** keeps every existing `job.json` valid. Verified against the pattern already used for `job_kind`, `load_evidence`, `addons`.

---

## 3. Deliverable entitlement model — the three cases

All three fall out of I1 + I3 with no special-casing:

**Case 1 — existing unlocked job, staff adds a watermarked clip**

```text
before: entitlement=edited_download   deliverable_access={}
after:  entitlement=edited_download   deliverable_access={exit_sequence: {preview_only, born_locked}}
→ full_video    resolves via default → edited_download → clean
→ exit_sequence resolves via entry    → preview_only   → preview_exit_sequence.mp4
→ the customer can buy ONLY exit_sequence (its CTA carries deliverable=exit_sequence)
```

**Case 2 — existing locked job, staff adds a clean clip**

```text
before: entitlement=preview_only      deliverable_access={}
after:  entitlement=preview_only      deliverable_access={exit_sequence: {edited_download}}
→ full_video    → preview_only    → watermarked (unchanged)
→ exit_sequence → edited_download → clean, downloadable
```
The clean clip does **not** inherit the job lock, and registering it does **not** touch `full_video`.

**Case 3 — several clips, mixed**

```text
deliverable_access={clip_a: preview_only, clip_b: edited_download, clip_c: preview_only}
→ gallery: A locked · B open · C locked; A and C each have their own CTA and their own
  purchase record; buying A leaves C locked (I3).
```

**What is deliberately NOT per-deliverable:**
- **Photos.** There is no watermarked-photo render path in either repo (`render_watermark`'s PNG is only ever composited into a video transcode). Photos stay job-level behind the `photos` add-on. A "watermarked photo set" is a separate product decision + a new render path.
- **Raw footage** (`raw` add-on) and the **load video** (`load_video` add-on) — purchase ledger items, not entitlements. Unchanged.
- **`media_state`.** See §5.5.

---

## 4. Media serving

### 4.1 `GET /j/{token}/media/{name}` — the URL is unchanged

`api/app.py:1762 public_media`, today:

```python
if job.entitlement is Entitlement.preview_only:      # :1782
    path = preview_path(job_dir, name)
else:
    path = job_dir / f"{name}.mp4"
...
if job.entitlement is not Entitlement.preview_only:  # :1794  S3 presign fallback
```

becomes:

```python
ent = entitlement_for(job, name)      # lock from the REQUESTING job, per name
path = preview_path(job_dir, name) if ent is Entitlement.preview_only else job_dir / f"{name}.mp4"
...
if ent is not Entitlement.preview_only:   # per-deliverable presign refusal
```

- The URL shape is **stable** — no new parameter, no new route. ✅ (explicit requirement)
- `job_dir` still comes from `_media_job(store, job)` (the owner), the lock still from `job`. The child-gallery invariant is untouched.
- The presign fallback (`_presigned_delivery_url`, for masters the pruner removed) is now refused **per deliverable**: a locked clip on an otherwise-open job can no longer be reached through S3.

### 4.2 `deliverable_access` is read on the REQUESTING job only — never inherited

A `load_child` streams a master's files under its own lock. If per-deliverable entries were inherited from the master, one customer's staff decision would lock (or open) another customer's purchase. So:

- `entitlement_for` is called with the **requesting** job. A child has no entries → resolves to the child's `entitlement`. Unchanged behaviour.
- Therefore per-deliverable registration is **refused on a `load_master`** (its render fans out to N galleries, and it has no price of its own) and on a `load_child` (owns no files) — matching the audit's edge cases 16/17.

### 4.3 The other public routes

| Route | Change |
|---|---|
| `/j/{t}/photos/{file}` | none — job-level lock + `photos` add-on (§3) |
| `/j/{t}/raw/{path}` | none — `raw` add-on |
| `/j/{t}/load/{name}` | none — `load_video` add-on, serves the master's clean cut |
| `/jobs/{id}/deliverables/{name}` | none — staff route, behind the service token; it has never been entitlement-gated and must not become the paywall bypass by *becoming* gated inconsistently. (It is admin/instructor-only; documented as such.) |

---

## 5. Gallery

### 5.1 `GET /j/{token}/state`

Keep `locked` with **exactly today's meaning** (`job.entitlement is preview_only`) so nothing that already reads it shifts under it, and add the per-deliverable map:

```json
{
  "locked": false,
  "addons": ["photos"],
  "deliverables": { "full_video": {"locked": false}, "exit_sequence": {"locked": true} }
}
```

The page's flip-poll signature (`api/gallery.py:266 init_sig`) extends to include the per-deliverable states, so buying one clip reloads the page in place. `/state` now exposes deliverable *names* — a deliberate widening of the "narrowest possible public response" rule, justified because the page already renders them and `public_media` already requires a name to be in `_gallery_videos`. Still no name, no email, no token echo, no payment reference.

### 5.2 `api/gallery.py` — per-card state (the real product change)

Today `badge`, `guard`, `dl` and `cta_bar` are **single variables** interpolated into every card. Replace the `videos: list[tuple[str, str]]` parameter with a frozen dataclass so the function stays pure and typed:

```python
@dataclass(frozen=True)
class GalleryVideo:
    name: str
    url: str
    locked: bool
    label: str | None = None        # ManualDeliverable.label override for _VIDEO_META
    unlock_url: str | None = None   # per-clip CTA target; None → rendered as TEXT
    price: str | None = None
```

Rendering rules (a product decision, stated so it can be argued with):

| Page state | Primary action | Card treatment | Accent |
|---|---|---|---|
| all locked (incl. every legacy `preview_only` job) | **today's single unlock CTA, unchanged** | `720P PREVIEW`, `nodownload`, no per-card CTA | amber `#e2a13f` |
| all open (every legacy `edited_download` job) | **today's Download button, unchanged** | `1080P · FULL QUALITY` + download | green `#5bbd84` |
| **mixed** | the Download button for the open primary video | open cards as today; **each locked card grows its own** `🔒 Unlock this clip — $X` + `720P PREVIEW` + `nodownload` | page green, locked cards amber |

The mixed page keeps the one-layout rule (design doc Frame 03): same hero, same grid, same upsell row — only the per-card treatment differs, which is precisely what the rule already permits per page. `test_locked_and_unlocked_share_the_same_layout` must keep passing untouched.

The **eyebrow** copy: "We filmed it anyway" is honest only when nothing was bought. Mixed → "Your jump is ready" (they own the edit) — the locked clip advertises itself on its own card.

### 5.3 `public_gallery` in `api/app.py`

```python
locked = job.entitlement is Entitlement.preview_only          # :1654  → keep, as the PAGE default
locked_names = locked_deliverables(job)                       # new
```
- `_primary_download` (:1620) must pick the primary from the **open** names only, and return `(None, None)` when none are open.
- `photos` gating (`:1701-1703`, `:1806`) unchanged (job-level).
- `unlock_url` (:1686) is built **per locked name**, adding `deliverable=` to the checkout URL.

### 5.4 The CTA URL, and the dead-link rule extended

`api/upsell.py:link_tiles` / the CTA both use `settings.checkout_url_template.format(job_id=…, booking_id=…, item=…)`. Add `deliverable=` to the format call — templates that don't reference it are unaffected (`str.format` ignores extra kwargs).

**New guard, and it is a money guard, not cosmetics:** if a deliverable is locked and the configured template contains no `{deliverable}` placeholder, the per-clip CTA is rendered as **plain text** (`"Unlock this clip — $X · ask at the desk"`), never as a link. A link without the target resolves to the *job-level* unlock — which on a Case-1 job means charging the customer the full-edit price and flipping the job default, i.e. exactly the §7.3 double-charge the previous audit stopped. Same shape as the existing "never dead-link" rule; here it prevents a mis-charge.

### 5.5 `media_state` stays job-level

`api/lifecycle.py:media_state` is unchanged, and per-deliverable lock is **not** projected into it. Reasons: it is a *job* projection SkydiveOS uses to label a jump; nothing branches on it; and adding `PARTIALLY_UNLOCKED` forces UI-copy work in SkydiveOS for a state that carries no decision. Instead, `JobResponse` and the status callback carry `locked_deliverables: [...]` explicitly — the same treatment `addons` already gets. *(Flagged as revisitable: if the SkydiveOS UI needs a single word for a mixed job, `PARTIALLY_UNLOCKED` is additive and its field is already free-form String.)*

---

## 6. Watermark generation — reuse only, no new engine

`render/watermark.py` (Pillow RGBA PNG) and `api/preview.py:render_preview` (one FFmpeg pass, `overlay`, 720p, CRF 28) are **untouched**. Two changes, both in `api/preview.py`:

**(a) new callable seam**

```python
def render_one_preview(job, store, settings, name, *, runner=None) -> Path | None:
    """Watermark exactly one deliverable. No-op (None) unless it resolves locked.

    The seam a manual clip needs: registering an output outside the three render
    tasks produced NO preview, and a locked gallery then 404s that clip (the S3
    presign fallback is correctly refused for a locked deliverable).
    """
```

**(b) `render_job_previews` loses its job-level gate**

```python
if job.entitlement is not Entitlement.preview_only:   # :107  DELETE
    return {}
```
replaced by: build `sources` from `job.outputs` (minus `photos`, else `final.mp4`) as today, intersect with `locked_deliverables(job)`, render those.

Ordering matters for I4: **compute the locked subset before raising `PreviewError` for "no rendered video"**. Today an `edited_download` job returns `{}` before that check and gains no failure mode; the new code must return `{}` when *nothing is locked* for the same reason. A `preview_only` job with no renders still raises, exactly as `tests/test_preview.py:214` pins.

Cost: one extra 720p CRF-28 transcode per locked clip, at the same three render seams plus the registration endpoint. A re-render re-transcodes locked previews (idempotent, wasteful, cheap) — accepted rather than adding staleness tracking.

Cleanup: an orphaned `preview_<name>.mp4` (staff flipped YES→NO) is never served, because serving is entitlement-driven. It costs disk until the pruner takes it (§16). The clean master is **never** deleted or re-encoded — watermarking stays purely additive.

---

## 7. Unlock / payment model

### 7.1 The current flow, and exactly where it assumes job-level

```text
Gallery CTA
 └─ CHECKOUT_URL_TEMPLATE {job_id, booking_id, item}
GET /api/media/unlock/:jobId?item=unlock                    PUBLIC, unauthenticated
 └─ mediaUnlockService.getOffer
    ├─ resolvePriceCents(item)          ← price from MediaConfig.pricing.items[item]  🔴 job-level
    ├─ alreadyPaid  = purchaseRecordFor(job, item)                                    🔴 item-level, not deliverable
    └─ requiresPayment = item !== 'unlock' || job.entitlement === 'preview_only'      🔴 JOB-LEVEL
POST /api/media/unlock/:jobId/charge    rate-limited 8/10min keyed (jobId, IP)
 └─ chargeUnlock → getOffer again (price re-derived; client amount never consulted)
    ├─ alreadyPaid   → 'already-paid', no charge
    ├─ !requiresPayment → 'not-paywalled', no charge                                  🔴 blocks Case 1
    └─ cloverEcomService.createCharge  metadata{paymentScope:'media-unlock',
                                                autoEditJobId, bookingId, item}       🔴 no deliverable
fulfillCapturedPayment
    ├─ Payment.create  idempotencyKey 'media-unlock:{chargeId}'  ← DB-enforced claim ✅
    ├─ stamp mediaUnlock.items[item]  (BEFORE the pipeline call — money isn't re-collectable) 🔴 key
    ├─ mediaUnlockRetryService.fulfill → queue (durable) → attempt                    🔴 no deliverable
    │    └─ autoEditOrchestrationService.unlockPaidMedia → autoEditService.unlock
    │         └─ POST /jobs/{id}/unlock {payment_reference, amount, item?}             🔴 no deliverable
    └─ if unlocked && item==='unlock' → AutoEditJob.entitlement = 'edited_download'    🔴 must not fire for a target
Pipeline api/app.py:1211 unlock
    ├─ item=='unlock' → entitlement=edited_download, paid_at, payment_reference        🔴 JOB-LEVEL
    └─ item in PURCHASABLE_ADDONS → addons[item]=ref (never touches entitlement)  ✅
```

### 7.2 The wire: a `deliverable` parameter, **not** a composite item key

The obvious move — item key `unlock:exit_sequence` — is blocked in two places and would have to break both:
- `mediaValidation.js:30 galleryItemKey` = `/^[a-z][a-z0-9_-]*$/i`, max 32 — no `:`;
- `mediaPricing.js:42 ITEM_KEY_RE` = `/^[a-z][a-z0-9_-]{0,31}$/` — the priced-key rulebook.

So: **`item` stays the product class; a new `deliverable` parameter names the target.** It threads through every hop:

```text
CTA URL           …/unlock/{job}?item=unlock&deliverable=exit_sequence
GET  offer        ?item=…&deliverable=…                      (zod: pipelineDeliverableName)
POST charge       body { sourceToken, item, deliverable, email }
Clover metadata   { paymentScope, autoEditJobId, bookingId, item, deliverable }
Payment           mediaSale { autoEditJobId, item, deliverable }
mediaUnlock.items key = purchaseKeyFor(item, deliverable)     (§8.3)
fulfillments[]    { item, deliverable, paymentReference, … }
unlockPaidMedia   { jobId, item, deliverable, paymentReference }
POST /jobs/{id}/unlock  { payment_reference, amount, item, deliverable }
```

`deliverable` shape (both repos): `^[a-z][a-z0-9_]{0,39}$` — the pipeline's own deliverable-name shape, and it must additionally pass the pipeline's `_is_safe_segment`.

### 7.3 Pricing — how much a clip costs

`resolvePriceCents` today rejects an unpriced item rather than defaulting ("a typo'd tile must fail loudly instead of quietly selling a video for $0"). Preserve that, and resolve a **price key** rather than reusing `item` directly:

```text
1. pricing.items["unlock_" + deliverable]   operator override for this exact clip
2. pricing.items["clip_unlock"]             the class price for a staff-cut clip
3. pricing.items["unlock"]                  ONLY when `deliverable` is absent, or is a
                                            pipeline-native deliverable (the AI edit)
otherwise → 400 "No price is configured for clip unlocks. Set `clip_unlock` in Media settings."
```

Both `unlock_<name>` and `clip_unlock` already satisfy `ITEM_KEY_RE` (validate `len("unlock_"+name) <= 32` at clip registration and refuse longer names). Step 3 must **never** be reached for a manual clip: charging the $39 full-edit price for a 12-second exit clip is a support ticket, so *which class a deliverable belongs to is decided by SkydiveOS at clip-creation time and stored as `manualDeliverables[].priceKey`* — the pipeline's mirror never influences price. That keeps "SkydiveOS is the billing authority" intact.

`DEFAULT_PRICING` gains no new key (a fresh install has nothing to sell clips for yet, and `applyPricingReset` already **preserves** operator-added keys). `validatePricingUpdate` needs no change.

### 7.4 `requiresPayment` and `alreadyPaid` — deliverable-aware, failing closed (I5)

```js
// getOffer
const target = deliverable || null;
const perDeliverable = job.deliverableEntitlements?.get?.(target) ?? null;   // mirror
const effective = target
  ? (perDeliverable ?? job.entitlement)     // absent entry → the job default (I1)
  : job.entitlement;

const alreadyPaid    = Boolean(purchaseRecordFor(job, purchaseKeyFor(key, target)));
const requiresPayment = key !== UNLOCK_ITEM || effective === 'preview_only';
```

The fail-closed table — because the mirror can lag a callback:

| mirror says | job.entitlement | decision | why |
|---|---|---|---|
| `preview_only` | either | **charge** | it is locked; selling is correct |
| `edited_download` | either | **refuse** (`not-paywalled`) | already open — refusing can never double-charge |
| no entry | `preview_only` | **charge** | legacy meaning, unchanged |
| no entry | `edited_download` | **refuse** | ⬅ **I5.** This is the "paid with the booking" customer. Refusing means a genuinely-locked clip briefly can't be sold until the callback lands; the alternative is charging someone twice |
| deliverable unknown to the pipeline | — | **refuse**, 400 at the pipeline | a purchase recorded against a name that doesn't exist would be marked *fulfilled* by the retry queue and never delivered |

The cost of failing closed is a *lost sale for a few seconds*; the cost of failing open is a *silent double-charge that does not self-correct* (media money is excluded from the booking's applied-money derivation, so it never shows as an over-payment). The trade is not close.

### 7.5 The pipeline's `POST /jobs/{id}/unlock`

```python
# item == 'unlock' and body.deliverable is None  → TODAY'S BEHAVIOUR, unchanged.
#   entitlement=edited_download, paid_at, payment_reference. Explicit
#   deliverable_access entries are NOT touched (I3) — that is what keeps a
#   Case-1 clip locked when the job default moves.
# item == 'unlock' and body.deliverable == name  → per-deliverable unlock:
#   deliverable_access[name] = {entitlement: edited_download, born_locked (kept),
#                               paid_at: now, payment_reference: ref}
#   NEVER touches job.entitlement, job.paid_at, job.payment_reference or status.
# item in PURCHASABLE_ADDONS → unchanged (addons ledger).
```

Gates unchanged and now four: service token, admin role, non-empty `payment_reference`, **plus** `deliverable ∈ job.outputs` (400 otherwise — see the table above).

Idempotent: an already-open target returns 200 unchanged with its original reference. The response body should include the resolved per-deliverable state so the retry queue can *verify* rather than assume (§18).

### 7.6 Booking-paid protection, spelled out

A normal paid-media booking is created `edited_download` and **no money ever flows through `mediaUnlockService`**, so `mediaUnlock.items` is empty. Under this design:

- registering a watermarked clip **does not change `job.entitlement`** (I1), so `getOffer` for `item=unlock` with **no** `deliverable` still reports `requiresPayment: false` — the whole-job CTA remains unchargeable;
- the *only* chargeable thing is the clip, via its own targeted offer at its own `clip_unlock` price;
- the pipeline additionally keeps the **entitlement-immutability guard** the previous audit asked for: `job.entitlement` may not be mutated on a job that has outputs, except by `/unlock` with no `deliverable`. That closes the §7.3 double-charge at the source, whatever a future caller tries.

---

## 8. Payment records & idempotency

### 8.1 Exactly-once per capture — unchanged
`Payment.idempotencyKey = media-unlock:{chargeId}` keeps its DB-enforced unique partial index. **Do not** add the deliverable to this key: its job is one-row-per-capture, and a chargeId is already unique per capture. Adding the target would let two charges for the same clip both succeed.

### 8.2 What a payment row now says
```text
paymentScope : 'media-unlock'          (excluded from the booking's applied money — unchanged)
ticket       : null                    (unchanged)
booking      : job.booking             (unchanged — attribution, not payment toward the jump)
mediaSale    : { autoEditJobId: 'ab12…', item: 'unlock', deliverable: 'exit_sequence' }
notes        : 'Paid media (unlock · exit_sequence) — auto-edit job ab12…'
```

### 8.3 `alreadyPaid` — the composite purchase key
```js
export const purchaseKeyFor = (item, deliverable) =>
  deliverable ? `${item}:${deliverable}` : item;    // 'unlock:exit_sequence' | 'unlock'
```
- Legacy keys never contain `:`, so this is a **disjoint namespace** — no migration, no collision, and `purchaseRecordFor`'s legacy single-stamp fallback keeps working for un-targeted purchases only (correct: a bare legacy `paidAt` was, by construction, a whole-job unlock).
- A job-level `unlock` purchase therefore does **not** satisfy a clip's key, which is Case 1's requirement.
- Mongo field names may contain `:` (only `.` and a leading `$` are restricted), so the existing dotted-path `$set` of `mediaUnlock.items.${key}` stays valid. Pin it with a test — this is the kind of thing that fails only in production.

### 8.4 Idempotency end to end

| Layer | Mechanism | Per-deliverable |
|---|---|---|
| Clover charge | `sourceToken` single-use | unchanged |
| Payment row | unique partial index on `idempotencyKey` | unchanged |
| Job stamp | `mediaUnlock.items[purchaseKey]` | composite key |
| Retry queue | `(item, paymentReference)` `$elemMatch` guard | becomes `(item, deliverable, paymentReference)` in `entryFilter` + the `queue()` `$not` guard |
| Pipeline unlock | already-open target → 200 unchanged | per-entry |
| Registration | idempotent on `name` | new |

---

## 9. API changes

### 9.1 `skydiveos-autoedit`

| Endpoint | Change |
|---|---|
| `POST /jobs/{id}/deliverables` | **new** (§12) — register a staff clip with its watermark state |
| `PATCH /jobs/{id}/deliverables/{name}` | **new** — flip the watermark state *before* any purchase; **409 if `paid_at` is set** (that is a refund, §17) |
| `DELETE /jobs/{id}/deliverables/{name}` | **new** — un-register a manual clip (removes it from `outputs` + `manual_deliverables`; leaves bytes on disk; refuses if paid) |
| `POST /jobs/{id}/unlock` | `deliverable` added (optional). No `deliverable` ⇒ today's behaviour exactly |
| `GET /jobs/{id}/deliverables` | `DeliverableInfo` gains `locked: bool`, `label: str \| None`, `born_locked: bool` |
| `GET /j/{token}/state` | adds `deliverables: {name: {locked}}`; `locked` keeps today's meaning |
| `GET /jobs/{id}` / `POST /jobs` response | `JobResponse` gains `deliverable_access` (projected without `payment_reference`? **no** — SkydiveOS reconciles against it, and this route is behind the service token; include it) and `locked_deliverables` |
| `POST /jobs` | unchanged (`entitlement` is still the job default). The `preview_only`-needs-`PUBLIC_BASE_URL` gate is **mirrored onto registration** |
| **guard** | `job.entitlement` immutable once `outputs` exist, except via `/unlock` with no target |
| status callback (`api/tasks.py:_notify_skydiveos`) | payload gains `deliverable_entitlements: {name: value}` and `locked_deliverables: [...]` |

### 9.2 `skydiving-os`

| Endpoint / module | Change |
|---|---|
| `GET /api/media/unlock/:jobId` | `?deliverable=` (zod), offer returns `deliverable`, `deliverableLabel`, `priceKey` |
| `POST /api/media/unlock/:jobId/charge` | body `deliverable`; metadata carries it |
| `POST /api/media/:id/clip` | **new** — cut a clip from an existing `Media`, create the child `Media`, resolve the customer's `AutoEditJob`, choose the `priceKey`, call the pipeline's `POST /jobs/{id}/deliverables` |
| `GET /api/media/:id/clip-target` | **new, read-only** — "which gallery would this land in, what is its job-level state, and what is already locked there", so staff sees the truth before choosing |
| `GET /api/media/ai-jobs/:jobId/deliverables` | proxy the pipeline route so staff UI shows per-clip lock state |
| status-callback receiver | persist `deliverableEntitlements` |
| `paymentEventHandler` | forward `metadata.deliverable` into `fulfillCapturedPayment` (webhook backstop) |
| `mediaUnlockRetryService` | thread `deliverable` through `queue`/`attempt`/`entryFilter`/`sweep`/`_alert` |
| `autoEditOrchestrationService.unlockPaidMedia` | thread `deliverable` |
| `autoEditService.unlock` | send `deliverable` |
| `mediaPricing.js` | document `clip_unlock` + `unlock_<name>`; export `purchaseKeyFor` |
| `refundService.js` | §17 |

---

## 10. Frontend (`skydiving-os`)

| File | Change |
|---|---|
| `pages/MediaUnlockPage.js` | read `deliverable` from the query string, show the clip's label in the heading and the receipt ("Unlock — Exit sequence"), pass it to `charge`. Refuse-with-explanation when the offer says `requiresPayment: false` (already handled generically — verify the copy reads sensibly for a clip) |
| `hooks/useMedia.js` | `useMediaUnlockOffer(jobId, item, deliverable)`, `useChargeMediaUnlock` body, `useCreateClip`, `useClipTarget`, `useAiJobDeliverables` + invalidations |
| `components/media/ImportSegmentDialog.js` / `VideoEditorDialog.js` | "Save selection as clip" → clip cutter with a genuine per-clip **Watermarked YES/NO** toggle |
| new `ClipWatermarkField` | the toggle, plus a read-only context line from `clip-target`: "This customer's gallery is Unlocked. This clip will be sold separately at $X." |
| `components/media/AiAutoEditDialog.js` | keep the job-level watermark toggle (it sets `entitlement` at creation); **disable it for a job that already has outputs** and explain why |
| `pages/app/CustomerMediaPage.js` | surface `AutoEditJob.galleryUrl` (one customer entry point) |
| `locales/en` + `fr` | both catalogs in the same change — `npm run i18n:check` fails the build on drift |

The **customer gallery needs no React work**: it is server-rendered by the pipeline (`api/gallery.py`).

---

## 11. Manual deliverable endpoint

```http
POST /jobs/{job_id}/deliverables
Authorization: Bearer $AUTO_EDIT_API_KEY        (service token — middleware)
X-Role: admin                                   (admin only: it decides what is given away)

{ "name": "exit_sequence",
  "label": "Exit sequence",
  "watermarked": true,
  "source": { "kind": "trim", "file": "raw/GH010042.MP4", "start": 61.5, "end": 73.0 } }
      or  { "kind": "s3", "s3_key": "media/…/clip.mp4" }
```

Behaviour, in order:

1. **Validate the name**: `_is_safe_segment`; `^[a-z][a-z0-9_]{0,39}$`; `len("unlock_"+name) <= 32` (the pricing-key ceiling); **not** `photos`; **not** a reserved pipeline name (`full_video`, `highlights`, `freefall`, `external_freefall`, `chute_libre_selfie`, `final`) — a manual clip must never overwrite the AI edit; **not** prefixed `preview_` (it would collide with the watermark convention).
2. **Refuse the wrong job kinds**: `load_master` (fans out to N galleries, no price of its own) and `load_child` (owns no files) — 409, same reasoning as `/upload`.
3. **Refuse `watermarked: true` when `PUBLIC_BASE_URL` is unset** — the exact `create_job` gate, applied here: a locked deliverable can only be delivered through the served `/j/{code}` gallery, and finding out at delivery time is finding out too late.
4. **Produce the file** — prefer the pipeline cutting it (`ffmpeg -ss/-to` over `raw/`, bytes already local, and the component that owns rendering keeps owning it). The `s3_key` intake is for sources the pipeline never had. When cutting from SkydiveOS media, cut from `originalS3Key`/`s3Key`, **never** `editedS3Key` — that one already has SkydiveOS's own destructive watermark burnt in, which no unlock can clean.
5. **Register**: `manual_deliverables[name]`, then `outputs[name]` (via the merge seam), then `deliverable_access[name] = {entitlement: preview_only if watermarked else edited_download, born_locked: watermarked}`.
6. **Watermark it now**: `render_one_preview(job, store, settings, name)` — synchronously for a short clip, or enqueued for a long one. If it fails, the registration **fails and rolls back** (a locked clip with no preview is a hard 404 in the customer's gallery, and the S3 fallback is correctly refused).
7. **Re-archive** (`archive_deliverables`) so the master + preview mirror into the jump folder.
8. **Idempotent on `name`**: same name + same source ⇒ 200, no re-cut. Same name + different source needs `replace=true`, and is refused once `paid_at` is set.
9. **Never re-notify the customer** by itself. Whether adding a clip to an already-delivered gallery emails/SMSes them is a product decision (`notify=true` flag, default false) — the gallery page picks it up on its next request either way.

---

## 12. Idempotency & concurrency

### 12.1 The real new hazard: lost updates on the map

`JobStore.update` is **read-modify-write with no lock** (documented as "single-writer by design"). A scalar `entitlement` flip tolerated that ("microseconds, and a retry heals it"). A **map merge does not**: two concurrent per-deliverable unlocks (two tabs, or a webhook racing the inline path) can have the second writer's read-then-write drop the first writer's flip — and the retry queue only re-fires *failed* fulfilments, so a lost update looks like a success and the customer stays locked with money taken.

Fix, in `JobStore`:

```python
def set_deliverable_access(self, job_id, name, **changes) -> Job:
    """Compare-and-set: merge ONE entry, re-read, retry until the write is visible.

    Every transition here is monotonic (preview_only → edited_download, and paid_at
    only ever gets set), so re-running a racing pair CONVERGES — a bounded CAS loop
    is enough and needs no lock file. Raises after N attempts rather than reporting a
    success it can't see; SkydiveOS's retry queue then retries, which is safe.
    """
```

and the unlock response returns the resolved per-deliverable state so `mediaUnlockRetryService.attempt` marks the entry `fulfilledAt` **only when the pipeline confirms that deliverable is open** — verification instead of assumption. (Today it trusts `outcome === 'unlocked'`.)

### 12.2 Everything else
- Rate limit `8 / 10 min` keyed `(jobId, IP)` covers a per-clip flood too; consider keying `(jobId, deliverable, IP)` so buying three clips in a row isn't throttled.
- `queue()`'s `$not: {$elemMatch: …}` guard and `entryFilter` gain `deliverable` so two different clips' fulfilments are distinct entries.
- Registration racing a render: `set_pipeline_outputs` merges `manual_deliverables` last, so whichever lands second still produces the union (§15).

---

## 13. Security

| Rule | Where |
|---|---|
| Lock state comes from `entitlement_for`, never from the URL, name, `?s=`, or any client hint (I2) | `public_media`, `public_gallery`, `/state` |
| No new route, no new parameter on the media URL — the clean master of a locked clip is unreachable at **any** URL | §4.1 |
| The presign fallback is refused **per deliverable** — a presigned URL carries no entitlement check | `public_media` |
| `delivery_links` never contains a URL for a locked deliverable (it is persisted, archived, **and forwarded to SkydiveOS**) | `api/delivery.py` |
| Registration is service-token + **admin** — it decides what is given away, like `/unlock` | §11 |
| Name validation: traversal, reserved names, the `preview_` prefix, `photos`, and the 32-char pricing ceiling | §11 step 1 |
| `deliverable` on `/unlock` must exist in `outputs` (400) — a purchase recorded against a phantom name is marked fulfilled and never delivered | §7.5 |
| A locked clip's CTA renders as text when the checkout template can't carry the target — a mis-targeted link is a mis-charge | §5.4 |
| `/state` now names deliverables. Still no PII, no token echo, no payment reference | §5.1 |
| Never log gallery tokens; keep payment references at INFO as today, without customer identifiers | all |
| `entitlement` immutability guard on a job with outputs | §7.6 |

---

## 14. Rerender / retry persistence

**The bug that exists today**, before any of this ships: `api/selfie.py` writes `store.update(job_id, status=ready, outputs=outputs)` at **four** sites (`:2734`, `:3183`, `:3254`, `:3456`) — a wholesale replace. A manual clip would silently vanish from `outputs` on the next `process_*` or `rerender_job`, becoming invisible (`_gallery_videos` reads `outputs`) and unservable (`public_media` 404s a name not in it), while its bytes and its `deliverable_access` entry linger.

Fix — one seam, four call sites:

```python
# JobStore
def set_pipeline_outputs(self, job_id: str, outputs: dict[str, str]) -> Job:
    """Persist a render's outputs, MERGING the staff-added clips back in.

    outputs is the render's own product and is replaced wholesale (correct — a
    package change must not leave a stale key). Manual deliverables are not the
    render's to own, so they are re-applied from `manual_deliverables`, skipping
    any whose file has gone.
    """
```

Also:
- `rerender_job` must not touch `manual_deliverables` (they are not in the EDL) and must not delete their files.
- `_render_previews` after a re-render re-transcodes locked manual previews — idempotent, accepted.
- `POST /jobs/{id}/upload` re-attaching footage to a `failed`/`rejected` job clears `processing_dispatched`; it must **not** clear `manual_deliverables` or `deliverable_access`.
- A failed job that is re-queued keeps both maps: the paid state of a clip survives a re-render, which is the whole point.

---

## 15. Archive & pruning

### Archive (`api/archive.py`) — nearly free
- `archive_deliverables` iterates `job.outputs` → manual clips mirror into `edited/` automatically; previews mirror into `preview/` by the `preview_<name>.mp4` glob, unchanged. `file_digests` covers both.
- `_write_manifest` (`:559`) gains `deliverable_entitlements` (and `locked_deliverables`) beside the existing `entitlement`, so an operator browsing `raw-storage/{date}/{instructor}/{customer}/` can see which master was never bought.
- The "nothing downstream ever *reads* the archive" rule is untouched.

### Pruner (`scripts/prune_jobs.py:129 prune_job_renders`)

Today: `if job.entitlement is preview_only: return freed` — i.e. keep *all* previews for a locked job, delete *all* previews for an open one. Becomes per-deliverable:

```python
locked = locked_deliverables(job)
for preview in job_dir.glob("preview_*.mp4"):
    name = preview.name[len(PREVIEW_PREFIX):-len(".mp4")]
    if name in locked:
        continue        # the paywall product, local-only. Never.
    freed += _delete(preview, why="deliverable unlocked; previews are derivative")
```

- The **clean master of a locked deliverable stays prunable** exactly as today: S3 holds it, and after unlock the name is open so `public_media`'s presigned fallback works. (Before unlock the fallback is refused — which is why the *preview* must survive.)
- The load-master dependents rule (`:155-163`) is unchanged: any pointer ⇒ keep everything.
- Photos are still never pruned.

---

## 16. Refund behaviour

**Today there is no path that re-locks media after a refund.** `refundService.js` writes a `refund` Payment referencing the original and has no awareness of `paymentScope: 'media-unlock'`, `AutoEditJob`, or the pipeline. A refunded unlock leaves the customer with the clean file indefinitely.

Design (recommend shipping the *seam* now, the automation later):

1. **Pipeline** `POST /jobs/{id}/relock` — admin + service token, requires `refund_reference`, optional `deliverable` (absent = the job default):
   - sets the entry back to `preview_only`, **keeps** `born_locked`, `paid_at` and `payment_reference`, and appends to `refunds[]` (append-only audit — a relock never erases the purchase);
   - **re-renders the preview if it is missing** (the pruner deletes previews once a deliverable is open, so this is the normal case);
   - refuses when there is no purchase to reverse (400) — a relock is not a way to lock something that was never sold.
2. **SkydiveOS** — `refundService` grows an **opt-in** `relockMedia: boolean` (default **false**) on a refund of a `media-unlock` Payment. When true: clear `mediaUnlock.items[purchaseKey]`, clear the mirror entry, call relock through the same durable retry queue. Default false because ops routinely refund as goodwill without taking the video back, and silently revoking what a customer can see is worse than a stale entitlement.
3. **Honest limitation, to be written into the UI copy:** re-locking controls *ongoing access*. It cannot un-download a file the customer already has, and for an `edited_download` deliverable that was in their gallery, assume they do.

---

## 17. Every site that assumes `Job.entitlement` is the only source of truth

Legend: 🔴 must change · 🟡 extend (additive) · ⚪ audited, no change (with the reason).

### `skydiveos-autoedit`

| # | Site | | Note |
|---|---|---|---|
| 1 | `api/preview.py:107` `render_job_previews` early return | 🔴 | the gate that makes a mixed job unproducible today |
| 2 | `api/app.py:1782` `public_media` preview-vs-master | 🔴 | the serving decision |
| 3 | `api/app.py:1794` presign fallback refusal | 🔴 | must be per-deliverable or a locked clip leaks via S3 |
| 4 | `api/app.py:1654` `public_gallery` `locked` | 🔴 | becomes page-default + `locked_deliverables` |
| 5 | `api/app.py:1690` `_primary_download` suppression | 🔴 | primary must be an **open** deliverable |
| 6 | `api/app.py:1686` `unlock_url` (single, `item="unlock"`) | 🔴 | per-clip CTA + `deliverable=`; text-not-link fallback (§5.4) |
| 7 | `api/app.py:1756` `/state` `locked` | 🟡 | keep the field, add `deliverables` |
| 8 | `api/app.py:1253-1260` `unlock` | 🔴 | optional `deliverable`; must not touch explicit entries |
| 9 | `api/app.py:871-883` `create_job` `preview_only`+`PUBLIC_BASE_URL` gate | 🟡 | mirror onto registration |
| 10 | `api/app.py:570-576` startup `preview_only` warning | 🟡 | count jobs with any locked deliverable too |
| 11 | `api/app.py:917` child adoption reads `child.entitlement` | ⚪ | a child has no map entries; carry-over unaffected |
| 12 | `api/app.py:1806` `public_photo` | ⚪ | photos stay job-level (no watermarked-photo path exists) |
| 13 | `api/app.py:1816` `public_load_video` | ⚪ | add-on ledger, serves the master's clean cut by design |
| 14 | `api/app.py:1413` `/jobs/{id}/deliverables/{name}` | ⚪ | staff route behind the service token; never entitlement-gated |
| 15 | `api/delivery.py:465` `locked` + `:481`/`:491` `presign=not locked` | 🔴 | per-file presign; no link for a locked name |
| 16 | `api/delivery.py:466-472` locked-needs-`PUBLIC_BASE_URL` raise | 🟡 | `any_locked(job)`, not just job-level |
| 17 | `api/delivery.py:521-539` legacy S3 gallery | 🔴 | must refuse when **any** deliverable is locked (it presigns clean masters) |
| 18 | `api/delivery.py:381` `_deliver_load_child` | ⚪ | gallery-link-only; unaffected |
| 19 | `api/lifecycle.py:83` `media_state` | ⚪ | deliberate: job-level projection (§5.5) |
| 20 | `api/archive.py:559` manifest `entitlement` | 🟡 | add the map |
| 21 | `api/archive.py:692` preview mirror by glob | ⚪ | convention-driven; works unchanged |
| 22 | `api/schemas.py:123` `JobResponse.entitlement` | 🟡 | add `deliverable_access` + `locked_deliverables` |
| 23 | `api/schemas.py:46` `CreateJobRequest.entitlement` | ⚪ | still the job default |
| 24 | `api/schemas.py:377` `UnlockRequest.item` | 🟡 | add `deliverable` |
| 25 | `api/tasks.py:99` callback `entitlement` | 🟡 | add the map (SkydiveOS's mirror depends on it) |
| 26 | `api/tasks.py:165-174` `_render_previews` contract | 🔴 | "fails for a locked job" becomes per-deliverable |
| 27 | `api/gallery.py` `badge`/`guard`/`dl`/`cta_bar`/`accent`/`eyebrow` | 🔴 | single page variables → per card (§5.2) |
| 28 | `api/gallery.py:266` flip-poll signature | 🔴 | must include per-deliverable state or a clip purchase won't reload the page |
| 29 | `api/gallery.py:36` `_VIDEO_META` | 🟡 | consult `ManualDeliverable.label` |
| 30 | `api/upsell.py:148` `link_tiles` `{item}` format | 🟡 | pass `deliverable=` |
| 31 | `api/selfie.py:2734,3183,3254,3456` `outputs=outputs` | 🔴 | wholesale replace deletes manual clips (§15) |
| 32 | `scripts/prune_jobs.py:175-179` preview protection | 🔴 | per-deliverable (§16) |
| 33 | `scripts/prune_jobs.py:157` locked-dependents count | ⚪ | job-level count is still the right conservative signal |
| 34 | `ingest/match.py:356 package_and_entitlement_for` | ⚪ | decides the job's **birth** default; unchanged |
| 35 | `scripts/skydiveos_bridge.py:478,547,570` | ⚪ | job creation only |
| 36 | `scripts/demo_auto_deliver.py --preview-only`, `scripts/smoke_no_camera.py` | 🟡 | add a mixed-job assertion (§19) |

### `skydiving-os`

| # | Site | | Note |
|---|---|---|---|
| 37 | `mediaUnlockService.js:153` `requiresPayment` | 🔴 | **the I5 site.** job-level today; blocks Case 1 and permits the double-charge |
| 38 | `mediaUnlockService.js:149` `alreadyPaid` / `:77 purchaseRecordFor` | 🔴 | composite key (§8.3) |
| 39 | `mediaUnlockService.js:93 resolvePriceCents` | 🔴 | price class per deliverable (§7.3) |
| 40 | `mediaUnlockService.js:171` offer `entitlement` | 🟡 | add `deliverable`, `deliverableLabel` |
| 41 | `mediaUnlockService.js:214-228` charge metadata | 🟡 | add `deliverable` |
| 42 | `mediaUnlockService.js:280-298` Payment row | 🟡 | `mediaSale` subdoc |
| 43 | `mediaUnlockService.js:313-333` job stamps | 🔴 | composite key |
| 44 | `mediaUnlockService.js:380-385` local `entitlement='edited_download'` mirror | 🔴 | **must not fire for a targeted unlock** — it would open the whole job |
| 45 | `mediaUnlockRetryService.js:68 entryFilter`, `:82 queue`, `:134 attempt`, `:167 sweep` | 🔴 | thread `deliverable`; verify per-deliverable before `fulfilledAt` (§13.1) |
| 46 | `autoEditOrchestrationService.js:2202` `item \|\| 'unlock'` | 🔴 | thread `deliverable` |
| 47 | `autoEditOrchestrationService.js:~1875` status receiver | 🟡 | persist `deliverableEntitlements` |
| 48 | `autoEditService.js:299-303` unlock body | 🟡 | send `deliverable` |
| 49 | `paymentEventHandler.js:133` webhook backstop | 🟡 | forward `metadata.deliverable` |
| 50 | `models/AutoEditJob.js:175` `entitlement` scalar, `:256 items`, `:271 fulfillments` | 🟡 | additive fields (§1.3) |
| 51 | `models/Finance.js` `Payment` | 🟡 | `mediaSale` (§1.4) |
| 52 | `validations/mediaValidation.js:30,40,47` | 🟡 | `deliverable` param; **not** a composite item key (§7.2) |
| 53 | `utils/mediaPricing.js:20,35,42,50` | 🟡 | document `clip_unlock` / `unlock_<name>`; export `purchaseKeyFor` |
| 54 | `services/refundService.js` | 🟡 | no media awareness at all today (§17) |
| 55 | `pages/MediaUnlockPage.js`, `hooks/useMedia.js` | 🟡 | carry `deliverable` (§11) |

---

## 18. Regression test matrix

### `skydiveos-autoedit`

**`tests/test_jobs.py`** (new resolver)
- empty map ⇒ `entitlement_for` == `job.entitlement` for every name, both values (I4);
- an entry overrides for that name only; unknown names still inherit;
- `locked_deliverables` handles `outputs=None` → `["final"]`;
- `set_pipeline_outputs` merges manual clips; drops one whose file is gone;
- `set_deliverable_access` CAS: two interleaved writers both land (I3 under concurrency).

**`tests/test_preview.py`** (alongside `test_no_previews_for_an_edited_download_job:162`, `…_skipping_photos`)
- an `edited_download` job with one locked clip renders **only** that clip's preview;
- a `preview_only` job with one open clip renders previews for **everything but** that clip;
- empty map ⇒ byte-identical set of previews to today, both entitlements;
- `render_one_preview` marks only the named deliverable and re-transcodes nothing else;
- `render_one_preview` on an open deliverable is a no-op returning `None`;
- an `edited_download` job with nothing locked and no renders returns `{}` and does **not** raise (I4 — the ordering trap in §6);
- a locked manual clip with a missing logo still gets a text-only mark.

**`tests/test_api.py`** (alongside `test_locked_gallery_serves_only_the_watermarked_preview`, `test_unlock_makes_the_gallery_serve_the_clean_master`)
- 🔴 **Case 1**: unlocked job + `watermarked=true` clip ⇒ `full_video` serves the master, the clip serves the preview, and **no request** reaches the clip's master;
- 🔴 **Case 2**: locked job + `watermarked=false` clip ⇒ `full_video` serves the preview, the clip serves the master;
- 🔴 **Case 3**: A/C locked, B open ⇒ unlocking A leaves C locked and B untouched;
- job-level unlock (no `deliverable`) does **not** open an explicit locked entry (I3);
- per-deliverable unlock never mutates `job.entitlement`, `job.paid_at`, `job.status` (I1);
- unlock with an unknown `deliverable` ⇒ 400, nothing recorded;
- unlock is idempotent per deliverable, first reference kept;
- `/state` reports the mixed map; `locked` keeps its old meaning;
- registration: reserved names, `preview_`-prefixed names, `photos`, unsafe segments, >32-char pricing key ⇒ 4xx;
- registration on `load_master` / `load_child` ⇒ 409;
- `watermarked=true` with `PUBLIC_BASE_URL` unset ⇒ 422;
- registration is idempotent on `name`; `replace=true` refused once `paid_at` is set;
- 🔴 **entitlement-immutability guard**: changing `entitlement` on a job with outputs is refused;
- 🔴 **re-render survival**: register a clip, re-run `process_selfie_package`, assert it is still in `outputs`, still streams, and still carries its lock state.

**`tests/test_gallery.py`** (pure renderer)
- `test_locked_and_unlocked_share_the_same_layout` must keep passing **unchanged**;
- a mixed page: per-card badge/`nodownload`/download-link/CTA correct per card;
- mixed page shows the download primary and *not* the page-level unlock CTA;
- a locked card with no `{deliverable}` in the checkout template renders **text**, no `<a>` (§5.4);
- `ManualDeliverable.label` overrides the `_VIDEO_META` fallback;
- the flip-poll signature changes when one deliverable's lock changes.

**`tests/test_delivery.py`**
- a mixed job mints presigned URLs for open names only; `delivery_links` contains none for a locked clip;
- a job that is `edited_download` with one locked clip and no `PUBLIC_BASE_URL` ⇒ delivery raises (no legacy S3 page);
- `collect_deliverables` includes manual clips; the photos zip is unaffected.

**`tests/test_archive.py`** — a manual clip's master + preview mirror into `edited/` + `preview/`; the manifest carries the map and a digest per file.

**`tests/test_prune.py`** — a locked deliverable's preview survives while its siblings' previews are deleted; after unlock its preview goes; the locked clip's *master* is still prunable; a load master with dependents keeps everything.

**`tests/test_lifecycle.py`** — `media_state` unchanged for a mixed job (pins the §5.5 decision).

**Scripts** — `scripts/demo_auto_deliver.py --preview-only` gains a mixed-job stage: register a locked clip on an unlocked job, assert the page serves master+preview correctly, unlock the clip, assert only it flipped. Non-zero exit if a lock ever leaks.

### `skydiving-os` (`backend/src/__tests__/`)

- 🔴 **the double-charge regression** (`mediaUnlockPaywall.test.js`): a job created `edited_download` with a locked clip — an offer with **no** `deliverable` reports `requiresPayment: false`, and no code path flips `job.entitlement`;
- targeted offer: `requiresPayment` true only when the mirror (or the fallback) says that deliverable is locked; the full fail-closed table from §7.4, row by row;
- `alreadyPaid` per composite key: buying `unlock:clip_a` does not mark `unlock`, `unlock:clip_b`, or `photos` as paid, and vice versa; a legacy bare `paidAt` still satisfies `unlock` only;
- pricing: `unlock_<name>` beats `clip_unlock` beats `unlock`; an unpriced clip class 400s with the actionable message; `unlock` alone never prices a manual clip;
- `mediaUnlock.items` accepts a `:`-containing Map key through the dotted `$set` (the §8.3 production-only trap);
- `fulfillCapturedPayment` writes `mediaSale{item, deliverable}` and does **not** flip `AutoEditJob.entitlement` for a targeted unlock;
- retry queue: two clips' fulfilments are separate entries; `entryFilter` addresses the right one; a sweep re-proves `paymentScope`; `fulfilledAt` is set only on verified per-deliverable confirmation;
- webhook backstop forwards `metadata.deliverable`;
- status receiver persists `deliverableEntitlements`, and a callback without it leaves the mirror untouched;
- clip creation: cuts from `originalS3Key`/`s3Key` and **never** `editedS3Key`; role gating; safety-lock refusal; `clip-target` finds the existing job by booking and mints no second job;
- `buildCreateJobBody` still sends the job-level `entitlement`, unchanged.

Per the project's testing note: keep any new shared logic dependency-free (as `utils/mediaPricing.js` deliberately is) so partial-mock suites don't break on a new model import.

---

## 19. Rollback strategy

**A plain code revert is not safe on its own, and it is worth being blunt about why.** `deliverable_access` is additive, so old code ignores it — and old code resolves every deliverable to `job.entitlement`. On a Case-1 job (`edited_download` + a locked clip) that means the old build **serves the unbought clip clean**. Give-aways don't roll back.

So the rollback is **data-first**:

1. `scripts/unregister_locked_clips.py [--job <id>… | --all] [--dry-run]` — removes locked manual deliverables from `outputs` + `manual_deliverables` (leaving the bytes and the audit trail on disk), so the old code has nothing to serve. Refuses any deliverable with `paid_at` set unless `--include-paid` (a paid clip should be *kept* open, not withdrawn).
2. Then revert the pipeline. Legacy jobs are unaffected (empty map ⇒ I4).
3. **SkydiveOS can be reverted independently and safely at any time**: offers stop being targeted, so a locked clip merely becomes unsellable (its CTA falls back to text) — nothing leaks, nothing double-charges.

Forward-only kill switches were considered and rejected: an env flag that made `entitlement_for` ignore the map would either give away locked clips (fail-open) or re-lock a Case-1 customer's already-owned `full_video` (fail-closed). Neither is a safe global default, which is exactly why the rollback is per-deliverable data, not a boolean.

**Phase ordering constraints:**
- Phase 1 (pipeline) **must** land before any clip is registered with `watermarked=true`.
- Phase 3 (SkydiveOS money) is not a blocker for safety — a locked clip with no targeted checkout renders as text ("ask at the desk"), which is a lost sale, not a leak.
- Phase 2 (mixed gallery renderer) **must** land with Phase 1: without it a mixed job renders one page-level badge and CTA that misdescribe half the cards.

---

## 20. Recommended phasing

| Phase | Content | Ships alone? |
|---|---|---|
| **0** | The guard rails, no product change: the entitlement-immutability guard; `set_pipeline_outputs` (fixes the *existing* re-render-drops-outputs bug); `render_one_preview` | ✅ yes — do this first regardless |
| **1** | `DeliverableAccess` + `entitlement_for` + `set_deliverable_access` (CAS); `public_media`; `render_job_previews`; `/state`; delivery presign; pruner; archive manifest; callback payload | with Phase 2 |
| **2** | `api/gallery.py` per-card state + the text-not-link CTA guard | with Phase 1 |
| **3** | SkydiveOS money path: offer/charge/fulfil/retry/mirror/pricing/`mediaSale` | ✅ yes |
| **4** | `POST /jobs/{id}/deliverables` + `POST /media/:id/clip` + staff UI + unlock-page label | ✅ yes |
| **5** | Refund → relock (`POST /jobs/{id}/relock`, opt-in `relockMedia`) | ✅ yes |

---

## 21. Open decisions for the product owner

1. **Mixed-page primary action** — §5.2 puts the Download button first and gives each locked clip its own CTA. The alternative is a single "Unlock 2 clips — $X" bundle CTA. Bundling needs a bundle price and a bundle purchase record; per-clip is the simpler truth and matches "unlocking X changes only X".
2. **Clip pricing** — one flat `clip_unlock` price, or per-clip overrides from day one? §7.3 supports both; the flat price is the smaller launch.
3. **Notify on a clip added post-delivery** — silent (gallery grows on next load) vs a second email/SMS. §11 step 9 defaults to silent.
4. **`PARTIALLY_UNLOCKED` in `media_state`** — deferred (§5.5). Needed only if the SkydiveOS UI wants one word for a mixed job.
5. **Watermarked photos** — genuinely does not exist in either repo. Out of scope here; a separate render path plus a decision about whether locked photos become visible-but-marked instead of a count teaser.
