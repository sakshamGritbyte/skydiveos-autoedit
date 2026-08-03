# Design — Fix #7: retry + alert a failed media unlock (and a failed job)

**Status: design only, not implemented.** Phase 4. Reviewed before build per the
audit workflow.

## The failure this closes

`autoEditOrchestrationService.unlockPaidMedia()` is deliberately **non-fatal**: the
money is already captured when it runs, so a pipeline blip must not fail the payment
(`autoEditOrchestration.test.js` pins that). Today the blip is therefore *silent* —
it logs `media unlock FAILED after payment capture` and stops. Outcome:

> The customer paid $39 and their gallery still shows the watermarked preview behind
> an "Unlock full video" button. Nobody is told. They discover it themselves, or they
> don't and the dropzone eats a refund and the goodwill.

Same shape as S-02 (a `failed` job with no retry edge), so both are covered by one
mechanism.

## Principles

1. **The money decides.** Once a payment is captured, the unlock is owed. Retry until
   it lands or a human is told — never drop it.
2. **Durable, not in-process.** A retry that lives in a `setTimeout` dies with the
   container. State goes in Mongo.
3. **Idempotent by construction.** `POST /jobs/{id}/unlock` is already idempotent and
   keeps the first `payment_reference`, so a retry can never double-unlock or
   overwrite the audit trail. Retrying is always safe.
4. **Alert on the human timescale, not the machine one.** A blip resolves in seconds;
   a human only needs to hear about it if the automatic attempts are exhausted.
5. **Customer-visible truth beats bookkeeping.** The gallery re-reads entitlement per
   request, so the moment any attempt succeeds the customer's page is correct with no
   further action.

## Mechanism (SkydiveOS side)

### State — on the existing `AutoEditJob`

No new collection; the job already is the per-booking record.

```js
unlock: {
  owedAt:      Date,      // when a captured payment made the unlock due
  paymentReference: String,
  amount:      Number,
  attempts:    Number,    // incremented per try
  lastAttemptAt: Date,
  lastError:   String,
  state: { type: String, enum: ['owed', 'done', 'alerted'], index: true },
}
```

`unlockPaidMedia()` changes shape slightly: it **records `unlock.state='owed'`
first**, then attempts. Success → `'done'`. Failure → leave `'owed'` with the error.
Crucially the owed marker is written *before* the network call, so a crash mid-attempt
still leaves a claim behind.

### Retry — the existing scheduled-job pattern

Reuse whatever this repo already runs on a timer (`loadStatusService`'s 60 s checker is
the precedent) rather than adding a queue:

```
every 60s: for each AutoEditJob where unlock.state === 'owed'
             and lastAttemptAt older than backoff(attempts):
    attempt unlock → 'done' | bump attempts + lastError
```

Backoff: `10s, 30s, 2m, 10m, 30m, 1h, 1h…` (capped). Cheap — the query is indexed and
matches nothing in the normal case.

### Alert — after the backoff is exhausted

At `attempts >= 6` **or** `owedAt` older than 30 minutes, transition to `'alerted'` and
fire once (dedupe on the state transition, so no alert storm):

* **Operator email** to the dropzone's admin address — the channel already used for
  operational mail (`notificationEmailService`). Subject names the customer and the
  booking so the desk can hand-fix it: "PAID BUT LOCKED — Sophie Lavoie (BK-1001)".
* **A staff-visible row** in the Media UI's existing unmatched/attention surface (the
  same place Fix #8 puts unmatched footage), with a "Retry unlock" button hitting the
  same service method. One place staff look for "media that needs a human".
* Log at ERROR with `paymentReference` (already the case).

`'alerted'` keeps retrying on the slow cadence — an alert is not a give-up.

### Manual lever

`POST /api/media/ai-jobs/:jobId/unlock-retry` (staff-authed) → `unlockPaidMedia({jobId,
paymentReference: <the recorded one>})`. Needed for the case where the pipeline was down
for hours, and it is what the UI button calls.

## The S-02 half (a failed *job*)

Same shape, different verb: add `POST /jobs/{id}/requeue` to the pipeline (allowed only
from `failed`, clears `error`, re-enqueues the package's task) and surface a "Retry"
button on a failed job. Prefer an explicit endpoint over Celery `autoretry_for` because
most pipeline failures are deterministic (missing telemetry, absent second camera) — a
blind auto-retry burns GPU minutes re-failing. Auto-retry only the transport-ish steps
(S3 ingest download) with 3 tries and jittered backoff.

## What NOT to do

* **Don't make the payment path fail** when the unlock fails. The capture is valid; the
  unlock is a downstream obligation.
* **Don't unlock speculatively** on any payment that isn't scoped `media-unlock` — the
  gate that stops paying-for-the-jump from unlocking media.
* **Don't re-render or re-deliver on unlock.** The masters exist and are already in S3;
  unlock is a one-field state change (CLAUDE.md).
* **Don't alert per attempt.** One alert per owed unlock, on the state transition.

## Test plan (the closing tests)

1. `unlockPaidMedia` marks `state='owed'` **before** attempting, so a crash leaves a claim.
2. A transient failure then a sweep → `state='done'`, exactly one upstream unlock call
   after success (idempotency means extra calls are harmless, but the sweep must stop).
3. Backoff respected: a job attempted 5 s ago is skipped by the sweeper.
4. Exhausted attempts → `state='alerted'` **once** across three consecutive sweeps
   (no alert storm).
5. The customer-facing assertion: after a successful retry, the pipeline reports
   `entitlement=edited_download` and `media_state=UNLOCKED` for that job.
6. A payment whose scope isn't `media-unlock` creates no owed unlock at all.
