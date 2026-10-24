# Implementation report — audit findings fixed without client decisions

**Date:** 2026-10-24 · **Source of truth:** `video-ingestion-sharing-bilingual-email-audit.md`
(unchanged) · **Scope:** confirmed defects, reliability and security fixes, and already-decided
requirements. Every item that depends on an open client decision (§G of the audit) was left
as it was and is listed under "Client decisions still required".

**Nothing was committed, pushed or deployed. No physical card was cleared or formatted, no
production data was modified, no real email was sent, no real purchase was made.** All tests
ran against mocks, temporary directories, fake cameras, stream transports and fixtures.

Pre-existing uncommitted work in the pipeline repo — `edl/validate.py`, `tests/test_bookends.py`,
`tests/test_edl_validate.py`, `tests/test_freefall_uncut.py` and the related `CLAUDE.md`
bullets (the deploy-offset sentinel and door-arrival rules dated 2026-10-24) — was preserved
and built on, never reverted. The audit report, its appendices and its evidence files are
unchanged; this report and the `*_after_fixes.log` files are additions.

---

## 1. Issues fixed, by priority

### P0 — SD-card staging collision (pipeline)

**Root cause.** `ingest/pull.py:_pull_one` decided "already staged" from the path alone. Two
cards cycled through one camera on one day (GoPro numbering restarts on a formatted card)
produce the same `_camera-staging/<camera_id>/<date>/GX010001.MP4`; the second card's clip
was skipped, never uploaded, matched or delivered, and the banner went green.

**Fix.**
- `ingest/storage.py`: `is_complete(path, expected_size=, created_epoch=)` is now an
  identity check (staged size == the card's listing; manifest `created_epoch` within 2 s).
  `resolve_destination()` returns the primary path for the same clip and the collision
  sibling `<date>/alt-<size>-<epoch>/<FILENAME>` for a *different* recording. The GoPro
  filename is preserved (every consumer derives names from `Path(key).name`).
  `day_dir_of()` / `camera_dir_of()` walk back up from either layout.
- `ingest/pull.py`: uses `resolve_destination`; the manifest records `staged.size`,
  `staged.camera_path`, `staged.collision` beside the pull's `source` (card serial) for
  provenance; the collision warning names the second card's serial; the partial-download
  cleanup resolves the same path. The emitted event keeps its exact shape.
- `ingest/retention.py`: one ledger record per RECORDING — the first keeps the bare-filename
  key (legacy layout), a different recording of the same name is written under
  `NAME#SIZE`; `confirmed_for(records, name, size)` is the single matcher used by
  `deletable` and `card_transfer`, so neither card's confirmation can erase the other's
  and the second card stays "owed" until its own upload is confirmed.
- `ingest/discovery.py`: `_capture_day` and the ledger directory see through the collision
  folder; S3 keys already fingerprint-scope on a same-day size mismatch.
- `ingest/qr.py`: the session index walks the whole staging day (`rglob`) so a collision
  sibling is attributed like any other clip of that card-day.

**Compatibility / rollback.** Primary paths, event shape, legacy ledgers and manifests
are unchanged; a manifest without the new `staged` block still matches the same clip
(size from the file, time from `created_epoch`). Old code reading a new ledger treats
`NAME#SIZE` keys as unknown names and never deletes on them (fail-safe). Rollback = revert
the files; data written in the new format stays readable by both versions.

### P1 — Hand-offs survive a restart (pipeline)

**Root cause.** The upload/notify queue was in memory and staged clips were never
re-emitted, so a restart between staging and the ledger write stranded the clip.

**Fix.** `ingest.discovery.stranded_handoffs(root, max_age_days=)` rebuilds events from the
on-disk manifests for every staged clip the camera's ledger does not confirm for that
recording; `CameraDiscoveryService(staging_root=…)` runs it once at start in a thread
(never raises). Bounded to recent staging days (`HANDOFF_RECONCILE_MAX_AGE_DAYS`, 7); a
pre-size-field ledger row counts as "handed off once", so an upgraded box does not
re-upload history. Idempotent: the same recording maps to the same S3 key and SkydiveOS
dedupes the notify on the key. Permanent (4xx) rejections are recorded in
`<camera dir>/.rejected.json` (informational, cleared on a later success).

**Feature flag / rollback.** `HANDOFF_RECONCILE_ON_START=0` restores the pre-fix
behaviour wholesale; both switches are documented in `.env.example`.

### P1 — Ingestion status accuracy (pipeline + SkydiveOS banner)

**Fix.** `ingest/cardstatus.py`: states `verifying` and `needs_attention` (terminal, lingers
like `error`, never green); fields `uploaded_files`, `unconfirmed_files`,
`awaiting_sweep_files`, `rejected_files`, `failed_files`, `issues`; `handoff_result()`
receives discovery's per-clip verdicts (`handoff_observer`), `issue_files()`,
`needs_attention()`, `verifying()`; `pull_started` keeps verdicts and the
`needs_attention` state across the idempotent re-pull. `api/app.py:_report_card_state`:
`verifying` while the card is re-read; `needs_attention` when everything still owed is
refused/failed or when the card's contents cannot be read (a card that is gone is still
released); `uploading` carries the breakdown. `api/schemas.py:CardIngestStatus` extended
additively. `scripts/watch_cards.py` shows the two states and per-file issues.
SkydiveOS: `SdCardIngestIndicator.deriveCardView` maps `verifying` (blue, DO NOT REMOVE)
and `needs_attention` (amber, the server's message, the issues list, "footage stays on
the card — fix the cause and re-insert"); `uploading` names the breakdown only when the
new counters are present (old wording byte-identical otherwise); `useIngestCards` polls
`verifying` fast; `autoEditService.TERMINAL_CARD_STATES` mirrors the pipeline's terminal
set so a stale `needs_attention` row ages out like `error`.

**Not changed (and why).** Per-clip upload overhead was not reduced: the ~15 s/clip fixed
cost could not be measured against real S3 from this machine, and concurrency touches the
ledger writes and the retry ladder — risking it blind would break rule 9. The existing
immediate rescan after the hand-off queue drains already re-runs the sweep as soon as the
last confirmation lands. The auto-clear policy, grace, dry-run and the verified-copy-
before-delete rule are untouched.

### P1 — Staff re-edits can no longer bypass the editing rules (pipeline + SkydiveOS)

**Root cause.** The editor's bookend rule (`editBookends.js`) recognised only clips typed
intro/outro; an imported AI deliverable is one `original` clip whose filmed interview and
freefall could be trimmed, split, slowed or covered by music, and
`replaceCustomerDelivery` shipped the export behind the customer's link unchecked.
`/tweak` saved an operator EDL verbatim and ran the legacy single-master re-render, which
failed the job.

**Fix.**
- Pipeline: `edl/protected.py` (pure, no `api.*`) derives each rendered deliverable's
  protected spans in output seconds from the persisted EDL + manifest, reusing
  `bookend_ranges` / `_ff_anchors`; `api/protected_ranges.py` + route
  `GET /jobs/{id}/deliverables/{name}/protected-ranges` (service token; 404 unknown):
  `{deliverable, duration, ranges:[{kind: intro_bookend|outro_bookend|freefall, start,
  end, music:false, music_until?, head_trimmable_until?}], reason?}`. Bookends for
  `full_video` only; freefall for `full_video` and the three freefall cuts (the Ultimate
  alternating run is one span); `highlights` → `[]`; underivable → `[]` + `reason`
  (SkydiveOS then enforces nothing — legacy behaviour).
- Pipeline `/tweak`: 409 with an actionable message for a job that has pipeline
  `outputs`, scene manifests or is a `load_child` (that path could neither render nor
  validate it and flipped the job to failed). Legacy single-master jobs keep the old
  behaviour. `/replace` is unchanged (its body is `extra="forbid"`; enforcement lives in
  SkydiveOS).
- SkydiveOS: pure validator `backend/src/utils/aiEditRules.js` (mirrored byte-identically
  to `frontend/src/utils/aiEditRules.js`) + `services/aiEditGuard.js`; ranges cached on
  the media row at import and fetched fresh at replace; the editor reducer refuses
  violating trim/split/delete/slow-mo/reverse actions with an en/fr toast and no undo
  step; export (`saveEdit`, `generateMultipleOutputs`) returns 422 on violations (pipeline
  unreachable → cached ranges, else exports unchecked with a warning since an export
  alone never reaches the customer); `replaceCustomerDelivery` returns 422 on violations,
  503 when ranges cannot be fetched (never silently replaces a valid video), 409 when the
  saved edit differs from the exported-and-checked one. Music over a bookend span is never
  a violation: the export folds the spans into the existing music-window mute mechanism
  (`videoProcessor._musicWindowFilters` gained optional `mutes`; byte-identical graph
  with no ranges). Speed changes inside the freefall run are allowed (the pipeline's own
  rule); gaps, inserted footage, reordering, reversing, end trims, crossfades and head
  trims past the exit (any head trim on `full_video`) are violations.

**Established rules only.** No new definition of intro/outro, no highlights rule, no change
to music fade style or to "Mute original audio" (open questions Q1–Q4).

### P1 — Bilingual customer emails, French first (SkydiveOS + pipeline)

**Root cause.** `bilingualEmail.languageOrder()` let the customer's `preferredLanguage`
lead, so English-preferring customers got English first; the certificate email turned
"no preference" into English-first; the pipeline's own delivery email was English-only.

**Fix.** SkydiveOS `backend/src/utils/bilingualEmail.js`: the order comes only from the
deployment default `EMAIL_PRIMARY_LANGUAGE` (default `fr`; missing/invalid → `fr`) — kept
as an operator-level override; `primaryLang` is still accepted everywhere but no longer
changes the order (`Customer.preferredLanguage` values and every other consumer — SMS
language, kiosk, waiver, certificate PDF — untouched). Subject, HTML, text and band follow
one order. Code-defined broadcast default subject is now
`Message de {{dzName}} / Message from {{dzName}}` (operator-written subjects unchanged).
Pipeline `api/delivery.py:send_gallery_email`: one bilingual plain-text message, French
section then English (`════ FRANÇAIS ════` / `ENGLISH`, same rule as SkydiveOS), subject
`<FR> / <EN>`, UTF-8 body and RFC 2047 subject; recipient, gallery URL, the
`send_gallery_email_once` claim / `email_sent_at` guard and the `CUSTOMER_EMAIL_SENDER`
gate are unchanged (no new send path, no duplicate).

**Left as is (open decisions).** Certificate PDF and invoice PDF language (Q22), all SMS
texts (Q23), staff-only emails, operator-authored SMS-mirror/broadcast bodies with no
French (listed below as needing approved translations), the waiver flow's write to
`preferredLanguage`.

### P1 — Share-link security: server-side revocation (SkydiveOS + pipeline)

**Root cause.** No share link could be withdrawn: per-media tokens had no revoke field,
`MediaGalleryShare.revokedAt` had no writer, `/j/{code}` never expired or revoked.

**Fix.** SkydiveOS: `downloadLinks[].revokedAt/revokedBy` and `MediaGalleryShare.revokedBy`;
`_assertShareLinkUsable` on the view page and the download redirect (404 unknown, 410
revoked, 400 expired, 404 not approved/delivered or `preview_only`); `_resolveGalleryShare`
on page, item download, ZIP info and ZIP stream; `POST /media/:id/share-links/revoke`
(same roles as minting; a customer only on their own media) and
`POST /media/gallery-share/revoke` (same roles as Copy Gallery Link); `generateDownloadLink`
never reuses a revoked link and calls `_assertNotPaywalled` as an explicit backstop;
`_galleryMedia` / `getGalleryItemDownload` also exclude `preview_only` imports; a "Revoke
share links" action with confirm dialog in `MediaDetailDialog` (en + fr copy). Pipeline:
`Job.gallery_revoked_at` (legacy `job.json` loads); every `/j/` route (all 9, pinned by a
route-table test) refuses a revoked gallery with a plain 410, `no-store`, no bytes, no
redirect; `POST /jobs/{id}/gallery/revoke|restore` gated like `/unlock` (service token +
admin), idempotent, never rotates the code, never touches status/entitlement; delivery
refuses to email a revoked gallery; the status callback withholds `gallery_url` for it.
No route exposing unpaid or locked masters was found; the review and the new tests pin
that.

**Left as is (open decisions / out of scope).** Link rotation/reissue, a staff preview-only
share, analytics, who may download, "first download marks delivered", propagating paid
unlocks to SkydiveOS rows. Known limitation: revocation cannot recall already-issued
presigned S3 (≤1 h) / delivery (≤7 d) / CloudFront URLs or a browser's cache.

### P2/P3 — small confirmed defects

- `scripts/diagnose_freefall_sync.py` read `report[deliverable]` but the report is
  `{"repairs": {…}}`, so section 1 always printed "no repair logged" — fixed.
- `CLAUDE.md` named `scripts/replay_edl.py`, which does not exist (`replay_selfie.py` does).
- `tests/conftest.py` now clears `CAMERA_CLOCK_TZ` like the other dev-box variables: the dev
  `.env`'s `America/Toronto` shifted the matcher tests' naive capture times by 4 h, which is
  why two `test_flight_window_callbacks.py` tests failed in the audit baseline. Environment,
  not a code defect; they pass now.

### Camera/card association — safe technical improvements only

Collision provenance (card serial in the manifest `source`, in the staged block and in the
warning), the rejection ledger naming the refused clip, the `needs_attention` banner, and
the collision-safe staging/ledger above are the diagnostics added. No schema migration, no
change to identification precedence (Q17–Q19). The unsupported scenarios (several cameras
per instructor, borrowed camera without QR/pairing, same last-4 serials) remain documented
in `appendix-C-instructor-camera-card-association.md`.

---

## 2. Repositories and files changed

**skydiveos-autoedit (pipeline).** Modified: `.env.example`, `CLAUDE.md` (bullets),
`api/app.py`, `api/config.py`, `api/delivery.py`, `api/jobs.py`, `api/schemas.py`,
`api/tasks.py`, `ingest/cardstatus.py`, `ingest/discovery.py`, `ingest/pull.py`,
`ingest/qr.py`, `ingest/retention.py`, `ingest/storage.py`,
`scripts/diagnose_freefall_sync.py`, `scripts/watch_cards.py`, `tests/conftest.py`,
`tests/test_delivery.py`. New: `api/protected_ranges.py`, `edl/protected.py`,
`tests/test_gallery_revoke.py`, `tests/test_handoff_recovery.py`,
`tests/test_protected_ranges.py`, `tests/test_staging_collision.py`. (Pre-existing user
changes, untouched: `edl/validate.py`, `tests/test_bookends.py`, `tests/test_edl_validate.py`,
`tests/test_freefall_uncut.py`.)

**skydiving-os (Ultimate DZM).** Modified: `CLAUDE.md` (bullets), backend
`controllers/mediaController.js`, `models/Media.js`, `models/MediaGalleryShare.js`,
`routes/mediaRoutes.js`, `services/aiJobService.js`, `services/autoEditService.js`,
`services/certificateService.js`, `services/mediaService.js`,
`services/smsNotificationService.js`, `utils/bilingualEmail.js`, `utils/emailService.js`,
`utils/notificationEmailService.js`, `utils/response.js`, `utils/videoProcessor.js`,
tests `aiDeliveryRepublish`, `bilingualCustomerEmails`, `bilingualInlineEmails`,
`bilingualSimpleTemplates`, `ingestCardsProxy`, `notificationEmailPipeline`,
`smsNotificationServiceTick`, `task431Certificates`, `videoShareLink`; frontend
`components/media/MediaDetailDialog.js`, `components/media/SdCardIngestIndicator.js`,
`components/media/VideoEditorDialog.js`, `hooks/useEditorReducer.js`,
`hooks/useIngestCards.js`, `hooks/useMedia.js`, `locales/{en,fr}/translation.json`,
`__tests__/SdCardIngestIndicator.test.js`. New: backend `services/aiEditGuard.js`,
`utils/aiEditRules.js`, tests `aiEditGuard`, `aiEditRules`, `shareLinkRevocation`; frontend
`utils/aiEditRules.js`, tests `AiEditRules`, `RevokeShareLinks`.

---

## 3. Tests added and results

| Repo | Suite | Result |
|---|---|---|
| Pipeline | **full suite after all changes** (`uv run pytest tests`, FFmpeg on PATH) | **1492 passed, 2 failed, 9 skipped** — both failures environmental: `test_discovery.py::test_build_pull_modes` and `test_metadata.py::test_labels_present` need the absent `sample-data/` fixtures (same two as the audit baseline). The two matcher tests that failed in the baseline pass after the conftest pin. Log: `evidence/pytest_after_fixes.log` |
| Pipeline | new `test_staging_collision.py` | 13 passed |
| Pipeline | new `test_handoff_recovery.py` | 16 passed |
| Pipeline | new `test_protected_ranges.py` | 19 passed |
| Pipeline | new `test_gallery_revoke.py` | 11 passed |
| Pipeline | `test_delivery.py` (+7 bilingual tests) | 74 passed |
| Pipeline | ruff on every changed file | 0 new findings (the 16 remaining are identical with the changes stashed: pre-existing long lines in the `replace` endpoint and `delivery.py:182`); mypy: only the pre-existing `ingest/camera.py:227` unused-ignore |
| SkydiveOS backend | all changed areas together (28 suites: bilingual ×8 + sms tick, share ×4, ingest cards + sd pairing, aiEditRules/Guard, aiDeliveryRepublish, editBookendsExport, slowMotionAudioFlow, autoEdit ×6, aiJob ×2, ultimumSdCardFourVideos) | **28 suites / 615 tests passed**. Log: `evidence/jest_backend_after_fixes.log` |
| SkydiveOS backend | full suite (run by the editor stream after every change) | 462 suites passed / 4 failed, 6987 tests passed / 27 failed — all 4 failing suites environmental or pre-existing: two `*.integration` suites hit the test database's 100-database limit, `stripeWebhook.documentBundle` times out identically on clean HEAD, `editBookendsExport` passes alone (real-ffmpeg render timed out under the parallel full run) |
| SkydiveOS frontend | changed areas together (VideoShare, RevokeShareLinks, SdCardIngestIndicator, SdCardsSection, AiEditRules, EditBookends, useEditorReducer, ReplaceCustomerVideo, Bug420DeliveryPanel, Bug439GalleryBulkDownload, MediaUnlockErrors) | **11 suites / 291 tests passed**. Log: `evidence/jest_frontend_after_fixes.log` |

**Existing workflows verified by the above:** normal pull / idempotent re-pull / repull /
LRV+THM tolerance; sweep, ledger, grace, dry-run, reused-filename rules; SD-card pull with
card serial through the real path; QR attribution; card status lifecycle and the operator
swap cycle; discovery hand-off, retry ladder, 4xx rejection, immediate rescan; gallery
serving, locked previews, mixed-entitlement jobs, ZIP, delivery and its once-only email;
protected ranges on the three real fixtures incl. the Ultimate alternating run; typed
intro/outro bookends and slow-motion audio in the SkydiveOS editor (real-ffmpeg render);
AI republish/replace; package mapping, orchestration, unlock wire, import; share minting,
view, download, gallery share; bilingual composition for every template family.

---

## 4. Remaining environment failures and verification gaps

- Pipeline: the two `sample-data/` tests cannot run on this box.
- SkydiveOS: the two Mongo-limit integration suites and the pre-existing
  `stripeWebhook.documentBundle` timeout; `editBookendsExport` needs FFmpeg on PATH and
  is sensitive to load under a full parallel run.
- Not verifiable here: a real Windows USB card reader and a real two-card day (fake
  cameras and temporary directories only); S3 upload overhead on the dropzone uplink; a
  restart exactly between S3 success and the ledger write (covered by SkydiveOS's key dedupe,
  not by a test); email rendering in mail clients; the deployed state of either repo.
- Editor guard limitations: AI rows imported before the change have no cached ranges until
  their first export (export and replace always fetch); the preview player does not mute
  music over AI bookends (the export does); only `original` clips are checked; a pre-rule EDL
  whose bookend was never whole gets no bookend range. A pre-existing editor defect was
  found and NOT fixed: with any transition on the timeline `concatWithTransitions` turns
  every cut into an xfade, including cuts into typed bookends.
- Revocation cannot recall already-issued presigned/CDN URLs or a browser's cache.

---

## 5. Client decisions still required (unchanged from the audit §G)

Q1–Q4 intro/outro definition, highlights, mute vs fade, camera audio under "Mute original
audio", whether staff may ever override; Q5–Q9 beat-drop configuration, which deliverables
sync, the 15 s lead cap, freefall end point and slow-mo inside the run, behaviour when
exit/drop is undetected (incl. whether `exclude.json` may ever cut the full video's
freefall — left as is); Q10–Q12 Ultimate cadence and one-camera fallback; Q13–Q16 auto-clear
vs confirmation, whole-card format, exact banner wording in EN/FR, behaviour when one clip
is refused, the 18 Oct build and ledgers; Q17–Q19 card serial vs multiple cameras per
instructor, re-pair after format, lending, the "Card 4812" label; Q20–Q21 which sharing
experience, revocable/expiring links, download rights, attribution, propagating unlocks;
Q22–Q23 PDFs, SMS, staff emails, exempt emails, `CUSTOMER_EMAIL_SENDER` per stack. Also
needing approved French: the pipeline email's four assembled sentences (listed in the
bilingual stream's notes), the new banner strings (English constants until the component is
converted), operator-authored SMS-mirror/broadcast bodies and subjects.

---

## 6. Migration / deployment steps (when authorised)

- No database migration. New Mongo fields (`downloadLinks[].revokedAt/revokedBy`,
  `MediaGalleryShare.revokedBy`, `aiGenerated.protectedRanges`) and the pipeline's
  `Job.gallery_revoked_at` default to null/absent.
- Pipeline: `docker compose up -d --build` (worker and API; the worker must restart to learn
  the changed delivery/callback code); optionally set `HANDOFF_RECONCILE_ON_START` /
  `HANDOFF_RECONCILE_MAX_AGE_DAYS` (defaults on / 7). On first start after the upgrade the
  reconciliation may re-upload recent staged clips whose ledger rows are missing — expected,
  idempotent, logged at WARNING with the file names.
- SkydiveOS: deploy backend and frontend together (the banner reads new states; the editor
  reads ranges from the new pipeline route — an older pipeline makes the guard return
  "no ranges" and enforce nothing, the pre-fix behaviour). `EMAIL_PRIMARY_LANGUAGE` stays
  `fr` unless an English-first deployment is wanted.
- Rollback per stream: revert the listed files; the data formats written in the meantime
  are tolerated by the previous code (ledger composite keys are ignored and never authorise
  deletion; `.rejected.json` and the manifest `staged` block are ignored; revoked tokens
  simply stop being checked).

---

## 7. QA runs on the changed code (2026-10-24, after the fixes)

| QA | How | Result |
|---|---|---|
| SD-card live QA | real API in `sdcard` mode on a scratch root + fake cards + a stub SkydiveOS receiver + the real S3 bucket (QA keys deleted afterwards); harness in `evidence/sdcard-live-qa/` | **18 / 18** — normal card → `safe_to_remove`; second card, same camera, same day, reused `GX010001.MP4` → staged in the collision sibling, uploaded under its own fingerprint key, one ledger record per recording, nothing dropped; refused clip → `needs_attention` naming the file, rejection recorded durably, the other clip confirmed; restart with a ledger record removed → exactly the two unconfirmed clips re-handed off (the stranded one and the previously refused one, whose rejection is then cleared) |
| Auto-deliver + paywall demo | `scripts/demo_auto_deliver.py --preview-only` on a real master, local SMTP sink, real S3 (upload deleted afterwards) | **PASS** — delivered; caught email subject `Votre vidéo de saut est prête, QA Paywall ! / Your skydive video is ready, QA Paywall!` with the FRANÇAIS section before ENGLISH; locked `/j/{code}` serves the watermarked preview and the unlock CTA; `POST /unlock` flips the same URL to the clean master with no re-render (`evidence/demo_auto_deliver_preview_only.log`) |
| Five-package no-GoPro QA | `scripts/qa_all_packages.py --review-gate --no-email` on the real masters in `jobs/*/raw` (eager worker, discovery off, no callbacks) | in progress at commit time — reported separately when it completes |
| SkydiveOS static checks | `npm run i18n:check` (catalog drift), `eslint` on every changed file | pass (0 errors; warnings pre-existing) |

One QA-tool defect found and fixed on the way: `scripts/qa_all_packages.py`'s health probe sent no
service token, so against a gated deployment it reported the API "not reachable" (401) before any
test ran.
