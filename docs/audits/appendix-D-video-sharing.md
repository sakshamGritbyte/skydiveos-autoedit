# Video-sharing / upsell audit (read-only) — 2026-10-24

Requirement: "Provide a video-sharing experience that supports upselling, while determining which sharing behavior the client actually wants."

Repos audited:
- SkydiveOS: `/home/msi/Desktop/Development/skydiving/skydiving-os` @ `5134f08` "Added Sharing functionality" (2026-10-23)
- Pipeline: `/home/msi/Desktop/Development/skydiving-autoedit/skydiveos-autoedit` @ `f4a5b5b` (+ uncommitted edits in `edl/validate.py`, unrelated)

Legend: **VERIFIED** = traced through backend code and/or covered by a passing automated test run in this audit. **UI-ONLY** = exists in the frontend; backend behaviour inferred from code, not executed. **MISSING** = not found anywhere.

---

## 0. Test runs (verbatim)

**Backend `videoShareLink`** — first attempt exactly as instructed:
```
cd backend && npx jest --silent videoShareLink
FAIL src/__tests__/videoShareLink.test.js
  ● Test suite failed to run
    Jest encountered an unexpected token
    ...
    /home/msi/Desktop/Development/skydiving/skydiving-os/backend/src/__tests__/videoShareLink.test.js:15
    import { jest } from '@jest/globals';
    ^^^^^^
    SyntaxError: Cannot use import statement outside a module
Test Suites: 1 failed, 1 total
Tests:       0 total
```
That is a harness issue (the repo's `package.json:20` runs jest under `node --experimental-vm-modules`). Re-run through the project script:
```
cd backend && npm test --silent -- --silent videoShareLink
PASS src/__tests__/videoShareLink.test.js
2026-10-24 13:08:05 [WARN]: Media view branding failed (non-fatal): {"error":"config down"}
Test Suites: 1 passed, 1 total
Tests:       8 passed, 8 total
Snapshots:   0 total
Time:        3.845 s
```
Tests in that suite (`backend/src/__tests__/videoShareLink.test.js:47-129`): reuses existing link + shareUrl; mints new link; `shareUrl is NEVER the presigned S3 url`; pure customer may only share their own media; shareUrlForToken builds from frontend url; branding block carried; branding failure non-fatal; expired token still refused.

**Frontend `VideoShare`**:
```
cd frontend && CI=true npx jest --silent VideoShare
Test Suites: 1 passed, 1 total
Tests:       28 passed, 28 total
Snapshots:   0 total
Time:        4.415 s
Ran all test suites matching /VideoShare/i.
```
(28 tests: utils, ShareVideoMenu on a real React root, per-surface source pins incl. "sharing shareUrl — never the presigned url", catalogs EN/FR.)

**Pipeline**:
```
uv run --no-sync pytest tests/test_gallery_share.py tests/test_mixed_entitlement.py -q -p no:cacheprovider
75 passed, 1 warning in 11.62s
```
(`tests/test_gallery_share.py:55-132` — 10 tests incl. `test_a_locked_page_is_shareable_too`, `test_outbound_share_links_never_send_a_referer`, `test_the_served_route_shares_the_clean_link`; `tests/test_mixed_entitlement.py` — 62 tests on per-deliverable lock.)

Nothing was modified. No emails were sent (the backend suite mocks SMTP; pipeline tests use TestClient).

---

## 1. Capability matrix

| # | Capability | Where (repo / file / route) | How it works | Security / entitlement check | Status |
|---|---|---|---|---|---|
| 1 | Staff: "Share video" from Media → details | SkydiveOS `frontend/src/components/media/MediaDetailDialog.js:89,234-249,859-865` → `GET /api/media/:id/download` (`backend/src/routes/mediaRoutes.js:265`) → `mediaService.generateDownloadLink` (`services/mediaService.js:2573-2628`) | Mints (or reuses, :2595-2608) a 32-byte hex `downloadLinks[]` token on the Media row; returns `{token, url (1 h presigned master), shareUrl = ${FRONTEND_URL}/media/view/{token}, expiresAt}` (:2564-2566). Menu = native share / Copy / WhatsApp / Facebook / Email (`frontend/src/utils/videoShare.js:80-114`) | Route `authorize('manifest','customer')` (admin/owner bypass, `middlewares/auth.js:83`); media must be `approved`/`delivered` (:2590-2592); pure customers only own media (:2583-2587). No entitlement check beyond status — a `preview_only` import is `linked` and can never be approved (`_assertNotPaywalled` :2265-2275, called at :2225 and :2301) | **exists + VERIFIED** (backend 8/8, frontend 28/28). Instructor-only role would 403 at :265 — see defect D7 |
| 2 | Staff: "Copy Gallery Link" | `MediaDetailDialog.js:251-260,867-870` → `POST /api/media/gallery-share` (`mediaRoutes.js:122`) → `createGalleryShare` (`mediaService.js:2787-2836`) | One `MediaGalleryShare` per import session: 32-byte hex token, TTL `downloadLinkTTLDays` (default 30), URL `${FRONTEND_URL}/gallery/{token}`; reused if live (:2806-2810) | `authorize('manifest','instructor','admin','owner')`; gallery resolves only `approved`/`delivered` rows of that session (`_galleryMedia` :2855-2865) | **exists + VERIFIED by trace** (BUG 439 suites exist; not re-run here) |
| 3 | Staff: row menu "Copy share link" | `frontend/src/pages/app/MediaPage.js:187-193,655-656` | Same `generateDownloadLink`, copies `shareUrl` to clipboard | As #1 | **UI-ONLY** (frontend source-pin test passes) |
| 4 | Staff: copy direct download URL (copy icon) | `MediaDetailDialog.js:206-230` → `/api/media/download/{token}` | Copies the **redirect-to-presigned-master** URL (not `shareUrl`); first hit flips `approved → delivered` (:2659) | token + expiry only (`accessDownloadLink` :2633-2680) | **exists** — a second, un-branded link type staff can paste (D8) |
| 5 | Staff: email/SMS a share link to a customer or prospect | — | Not found. The only sends are the automatic delivery email/SMS and the admin-only **resend** of the delivery gallery link (`mediaRoutes.js:195-201`, BUG 420). Share menu's "Email" item is a `mailto:` opened on the staff member's own client (`videoShare.js:43,107-112`) | — | **MISSING** (copy-only workflow) |
| 6 | Customer portal share | `frontend/src/pages/app/CustomerMediaPage.js:43-49,140-145` | Same `ShareVideoMenu`, link minted on menu open (`ShareVideoMenu.js:51-78`), `navigator.share` where present else Copy/WhatsApp/Facebook/Email | `generateDownloadLink` 403 for someone else's media (:2583-2587, test `videoShareLink.test.js:80`); list itself is `approved`/`delivered` only (`getCustomerMedia` :2536-2541), so a locked (`linked`) row never appears and no link can be minted for it | **exists + VERIFIED** |
| 7 | Public landing page `/media/view/:token` | `frontend/src/App.js:180` → `pages/MediaViewPage.js`; `GET /api/media/view/:token` (`mediaRoutes.js:39`) → `getMediaByToken` (:2685-2745) | Plays `streamUrl` = **1 h presigned FULL-RES master** (:2706), shows **Download** button (:145-149 → `/api/media/download/:token`), dropzone logo + "book again" CTA (:202-213, `_galleryBranding` :2869-2898), its own Share menu (:155) | Token + `expiresAt` (:2700-2703); `deletedAt: null`. No viewer identity. Not counted as an access (docstring :2683) | **exists + VERIFIED** (branding tests) |
| 8 | Public gallery `/gallery/:token` | `App.js:182` → `CustomerGalleryPage.js:38-44,142-146`; `GET /api/media/gallery/:token` (+ `/download/:mediaId`, `/download-all`) | Presigned master per item (:2920-2935), ZIP, "Share gallery" shares `window.location.href` | token, `revokedAt: null`, expiry (:2842-2849); items scoped to session + status | **exists + VERIFIED by trace** |
| 9 | Pipeline gallery `/j/{code}` Share button | `api/app.py:2371-2380,2608-2619`; `api/gallery.py:260-270,553-603` | Hero `<details>` menu: native sheet, Copy, WhatsApp, Facebook, Email; shares the **clean** `{PUBLIC_BASE_URL}/j/{code}` (no `?s=`, no `#tab`); `og:title/description/type/url` (no `og:image`) | Code is the only credential: 11 chars of `[A-Za-z0-9]` from `secrets.choice` (`api/jobs.py:403-404,429`, ≈2^65); `/j/` exempt from the service token (`api/auth.py:59,93`) and resolved to an anonymous principal (:175) | **exists + VERIFIED** (10/10 + `test_api` gallery suite) |
| 10 | Pipeline locked preview + Unlock CTA | `api/app.py:2484-2487` (`CHECKOUT_URL_TEMPLATE.format(job_id, booking_id, item=unlock)`), per-camera :2493-2510; `api/gallery.py:326-336` (badge `720P PREVIEW`, `controlsList="nodownload"`), :344-358 / :423-432 (CTA, text when no template) | Watermarked 720p `preview_<name>.mp4` is the only bytes served while locked (`public_media` :2712-2715); CTA → SkydiveOS `/unlock/{job_id}?item=…` (`frontend/src/App.js:186`, `MediaUnlockPage.js:4-12`) | Entitlement per deliverable per request (`entitlement_for`); `?s=` ignored (`test_api.py:1500-1512`) | **exists + VERIFIED** (`test_locked_gallery_serves_only_the_watermarked_preview` :1525, `…serve_the_clean_master` :1535) |
| 11 | Unlock flow (payment → flip) | SkydiveOS `mediaRoutes.js:80-87` (public offer/charge, rate-limited :28-36) → `mediaUnlockService` → `paymentEventHandler.js:648-660` (`paymentScope === 'media-unlock'`) → `autoEditOrchestrationService.unlockPaidMedia` (:1617-1699, per-role item) → pipeline `POST /jobs/{id}/unlock` (`api/app.py:1648-1700`: service token + admin + non-empty `payment_reference`) | Same `/j/{code}` URL flips on next request; no re-render | Jump payments refused (`paymentEventHandler.js:196-201`) | **exists + VERIFIED** (pipeline `test_api.py:940-966`; SkydiveOS side by trace) |
| 12 | "Your video is ready" email (SkydiveOS, `CUSTOMER_EMAIL_SENDER=skydiveos`) | `services/mediaDeliveryEmailService.js:374,384-395` → `utils/emailService.js:2386-2440` | ONE link `{galleryUrl}?s=e`; copy branches on `locked`: "Your skydive video **preview** is ready … unlock the full-quality, watermark-free version straight from the same page", button "Watch your preview"; follow-up "A new video (preview) was added…"; `updated` variant for a replace (:711-760); SMS `?s=m` (:753,774) | Held until the job has a `galleryUrl` (:258-282) | **exists + VERIFIED by trace** |
| 13 | "Your video is ready" email (pipeline, legacy/`pipeline` sender) | `api/delivery.py:312-370`, link `…/j/{code}?s=e#tab-video` (:531,669) | Plain-text, one link, same copy locked or not ("Your skydive video is ready! … View and download…") | Locked job → served gallery only; no presign of locked names (:181,605-612,625) | **exists + VERIFIED** (delivery tests exist; not re-run) |
| 14 | Legacy SkydiveOS approve email | `mediaService.js:2324-2345` → `emailService.js:1168-1246` | "Download Video" button → `/media/view/{token}`, "This download link expires on …" | approve gate incl. `_assertNotPaywalled` | **exists** (manual-approve path only) |
| 15 | Preview-only / watermarked share from SkydiveOS | — | Not found: every SkydiveOS token resolves to the presigned master (`:2706`, `:2673`, `:2920`, `:2975`) | — | **MISSING** |
| 16 | Revocation of a share link | SkydiveOS: `MediaGalleryShare.revokedAt` (`models/MediaGalleryShare.js:59`) — **no writer anywhere** (grep `revokedAt` → only the two reads at `mediaService.js:2808,2843`); `downloadLinkSchema` (`models/Media.js:3-21`) has no revoke field; pipeline: no `revoke` in `api/` (grep); only `adopt_gallery_token` clears a child's token (`api/jobs.py:1059-1076`) | Soft-delete of media (`deleteMedia` :5343-5366 sets `deletedAt`) kills its tokens via the `deletedAt: null` filter — but `delivered` media cannot be deleted (:5354) | — | **MISSING** |
| 17 | Expiry | SkydiveOS tokens: `downloadLinkTTLDays` (default 30 d, `:5539-5551`, `:2814`), 1 h on the presigned bytes; pipeline `/j/{code}`: never (`api/app.py:2092-2095`) | | | exists (SkydiveOS) / by design none (pipeline) |
| 18 | Analytics of share visits / preview plays / purchases-from-share | SkydiveOS: `downloadLinks.accessCount` on `/download` redirect only (:2655; **not** on the view page, :2683), `MediaGalleryShare.accessCount/lastAccessedAt` (:2913-2914), dashboard `totalDownloads` (`analyticsService.js:118-121`). `?s=e/m` is emitted (`mediaDeliveryEmailService.js:391-393`) but **no consumer reads it** (grep `req.query.s` → none; pipeline ignores it `api/app.py:2377-2379`). `ShareVideoMenu` has an `onShared` hook (:44,88) that **no surface passes** (grep). No utm/gtag/posthog/mixpanel in media code | — | **MISSING** (only crude access counters) |

---

## 2. Security analysis per link type

### 2.1 SkydiveOS `/media/view/{token}` (the new "shareUrl")
- **Minting**: `crypto.randomBytes(32).toString('hex')` (`mediaService.js:5541`) — 256-bit, unguessable. Stored on the Media row (`downloadLinks[]`), reused while unexpired (:2595-2608) — so Share, Download, the copy icon and the approve-time email all hand out the **same** token.
- **Scope**: one media item; customer-specific only in the sense that it is the customer's media. **No viewer auth** — bearer link.
- **Exposure**: `getMediaByToken` returns a **1 h presigned URL of `media.s3Key`, the full-res master** (:2706) plus `downloadToken` (:2736); the page renders a `<video>` (`MediaViewPage.js:129`) and a **Download** button (:145-149) → `/api/media/download/:token` → 302 to a fresh presigned master (:2673, controller :373-377). There is **no preview-only variant**.
- **Unpaid media**: cannot be reached. A pipeline deliverable that is `preview_only` is imported as `status: 'linked'` with `aiGenerated.entitlement: 'preview_only'` (:1633-1707); `generateDownloadLink` requires `approved`/`delivered` (:2590), `submitForReview` and `approveMedia` refuse via `_assertNotPaywalled` (:2225, :2301, :2265-2275); `approveMedia` is only reachable from the admin/owner route (`mediaRoutes.js:257`, single caller `mediaController.js:346`). ⇒ **no P0**. Caveat: the clean bytes of the locked deliverable ARE copied into SkydiveOS S3 at import (:1660-1680, S3→S3 copy from `deliveries/{job}`), protected only by that status ladder; staff `GET /:id/stream` (`mediaRoutes.js:243`) can presign it — staff-only.
- **Expiry**: `downloadLinkTTLDays` (default 30). The frontend handles 400 "expired" (`MediaViewPage.js:29-31`).
- **Revocation**: none (no field, no endpoint). Media soft-delete kills it, but delivered media is immutable (:5354). Customer account changes do not affect it.
- **Tracking**: view page not counted (:2683); only download redirects count (:2655-2656) and that first hit flips the media to `delivered` (:2659) — a **friend's** download click will mark the customer's media delivered.

### 2.2 SkydiveOS `/gallery/{token}` (import-session gallery)
- 256-bit token (:2819), TTL `downloadLinkTTLDays`, `revokedAt` honoured on read (:2843) but **never written**. Resolves to every `approved`/`delivered` row of the import session (:2855-2865) → **presigned masters** (:2920), per-item download (:2966-2976), ZIP (BUG 439). Locked (`linked`) rows are excluded by status. Access counted (:2913-2914). Bearer link, no viewer auth. Creation restricted to staff roles (`mediaRoutes.js:122`).

### 2.3 Presigned S3 URL (`result.url`)
- 1 h, full-res, unscoped. The commit's point: it is now **Download-only**; a frontend source pin forbids sharing it (`VideoShare.test.js`, passing). Still produced by every `generateDownloadLink` call (:2600,:2616) and still reachable as the copy-icon URL via redirect (#4 above) and via `/gallery/:token/download/:id`.

### 2.4 Pipeline `/j/{code}`
- 11-char base62 from `secrets` (≈65.5 bits) — adequate for an unguessable bearer link, weaker than SkydiveOS's 256-bit but rate-limited only by the in-process token index (`api/jobs.py:1081-1096`, designed to avoid disk I/O on misses). Minted once per job, stable across replays, **never expires**, **no revoke endpoint** (grep). `find_by_gallery_token` applies no status filter (a failed/pre-render job renders a "still editing" page, `test_api.py:1513`).
- **The entitlement — never the URL — picks the bytes**: `public_media` serves `preview_<name>.mp4` while `entitlement_for(job, name) is preview_only` (`api/app.py:2712-2715`); `?s=` is accepted and ignored, including a spoofed `?s=edited_download` (`test_api.py:1500-1512`). No presigned URL is ever minted for a locked deliverable (`api/delivery.py:181,625`; `test_mixed_entitlement.py`, 62 passing). Posters of locked cards are cut from the watermarked preview (`test_api.py:1437`).
- **Sharing = forwarding**: the Share button hands out exactly the bearer link the customer holds (`api/app.py:2615-2618`, clean of `?s=`/`#tab`); a friend sees the same lock state, the same `720P PREVIEW` badge and the same Unlock CTA (`test_a_locked_page_is_shareable_too`, `tests/test_gallery_share.py:113`). Outbound anchors carry `rel="noreferrer"` (`api/gallery.py:568-572`, test :82) so the code does not leak via Referer to WhatsApp/Facebook. After payment the **same URL** flips (:1535). A per-viewer identity does not exist by design (customer has no SkydiveOS account).
- **Bypass assessment**: sharing bypasses nothing that forwarding the email would not; the paywall is enforced server-side per request. The only privilege escalation path is `POST /jobs/{id}/unlock`, triple-gated (`api/app.py:1654-1670`).
- Gaps: no `og:image` (social cards show no thumbnail); no revocation if the customer later asks for the link to be killed; no visit/share analytics.

---

## 3. Defects / gaps (with severity)

**P0 — none.** No link type, in either repo, exposes an unpaid full-resolution file to a customer or a link holder: SkydiveOS share links are status-gated and a `preview_only` import cannot climb the ladder (§2.1); the pipeline gallery serves watermarked bytes per request and mints no presigned URL for locked names (§2.4). Both verified by passing tests this run.

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| D1 | **P1 (requirement gap)** | **SkydiveOS has no preview-only / watermarked share.** Every SkydiveOS token (`/media/view`, `/download`, `/gallery`) resolves to the presigned **full-res master + a Download button**. Staff therefore cannot "share with a prospective buyer without granting the file" from SkydiveOS; that behaviour exists ONLY on the pipeline's `/j/{code}` locked page, whose link the staff delivery panel deliberately never shows (BUG 420, SkydiveOS `CLAUDE.md:3918-3919`) | `mediaService.js:2706,2673,2920,2975`; `MediaViewPage.js:129,145-149` |
| D2 | **P1** | **No revocation of any share link, in either repo.** `MediaGalleryShare.revokedAt` has no writer; `downloadLinks` have no revoke field; pipeline `gallery_token` is never cleared except by child adoption. Delivered media cannot be deleted, so the only kill-switch (soft-delete) is unavailable exactly for the media that has links. Customer permission/account changes never invalidate a token | `models/MediaGalleryShare.js:59`; grep `revokedAt` → reads only at `mediaService.js:2808,2843`; `models/Media.js:3-21`; `mediaService.js:5354`; `api/jobs.py:1059-1076`; grep `revoke` in `api/` → none |
| D3 | **P2 (likely; needs runtime confirmation)** | **A paid unlock never reaches the SkydiveOS Media row.** `aiGenerated.entitlement` is written only at import; `mediaUnlockService` touches no `Media` rows (grep `Media.` → none) and a re-import skips a deliverable whose `outputType` already exists (`existingVideoTypes`). So after the customer pays, the SkydiveOS row stays `preview_only`/`linked`, `approveMedia` keeps refusing it, and `_assertNotPaywalled`'s own instruction "unlock it… then re-import" cannot succeed. The customer is fine (their `/j/{code}` flips), but staff can never approve, share or Copy-Gallery-Link that video from SkydiveOS | `mediaService.js:1620-1631,1701-1707,2265-2275`; grep `entitlement =` in `services/` → only `mediaUnlockService.js:954-960` on AutoEditJob refs |
| D4 | **P2** | **No share/upsell analytics.** `?s=e`/`?s=m` is attached to every outbound link "for analytics" but nothing reads it on either side; `onShared` callback is never wired; the view page is deliberately not counted; only download-redirect and gallery hits increment counters. Nothing attributes a purchase to a share or a channel | `mediaDeliveryEmailService.js:391-393`; `api/app.py:2377-2379`; `ShareVideoMenu.js:44,88` (no consumer); `mediaService.js:2683,2655,2913`; `analyticsService.js:118-121` |
| D5 | **P2** | **Staff share workflow is clipboard-only.** No "send this link to the customer/prospect by email or SMS" action; the Share menu's Email item is a `mailto:` on the staff member's device; the only server send is the admin-only resend of the delivery email | `videoShare.js:43,107-112`; `mediaRoutes.js:195-201`; `MediaDetailDialog.js:251-260` |
| D6 | **P3** | **Pipeline-sent delivery email has no locked variant.** When `CUSTOMER_EMAIL_SENDER=pipeline` the customer of a locked job gets "Your skydive video is ready! … View and download…" with no preview/unlock pitch; SkydiveOS's sender does pitch the unlock | `api/delivery.py:333-350` vs `emailService.js:2408-2431` |
| D7 | **P3 (UI-ONLY, untested)** | "Share video" / "Copy share link" render for any staff viewing an approved item, but the mint route is `authorize('manifest','customer')` — a staff user whose only role is `instructor` gets a 403 toast (admin/owner bypass) | `mediaRoutes.js:265`; `MediaDetailDialog.js:845-872`; `middlewares/auth.js:83-98` |
| D8 | **P3** | The dialog's copy icon copies the **direct download redirect** (`/api/media/download/{token}`), a second link type beside `shareUrl`; whoever clicks it (a friend) downloads the master and flips the media to `delivered` | `MediaDetailDialog.js:206-230`; `mediaService.js:2655-2665` |
| D9 | **P3** | No `og:image` on the pipeline page (documented); SkydiveOS public pages have no `og:` tags at all (grep) — social shares render as bare links, weakening the "friend meets the dropzone" upsell | `api/gallery.py:577-580`; grep `og:` in `frontend/src` → none |
| D10 | **P3** | After a successful unlock, `MediaUnlockPage` shows a success card with no link back to the gallery (copy in locales not audited) | `MediaUnlockPage.js:115-127` |
| D11 | **P3 (tooling)** | `npx jest` directly fails on ESM; the backend suite must be run through `npm test` | `backend/package.json:20` |

Design notes (not defects): a SkydiveOS token is a 256-bit bearer secret that expires; the pipeline code is a 65-bit bearer secret that never expires. Both are "possession = access"; neither has per-viewer identity. That is consistent with the design doc, but it means "revocation" (D2) is the only lever if a link is leaked.

---

## 4. Which sharing behaviour does the client want? — questions, and what already exists

| Option | Already exists? | Where | Gap to close |
|---|---|---|---|
| (1) **Staff sends a preview link to encourage purchase** | **Partly.** The pipeline's locked `/j/{code}` page IS that link (watermarked 720p, Unlock CTA, per-camera CTAs, posters), and the SkydiveOS delivery email/SMS already pitches it to the customer. But staff cannot see or copy that link (BUG 420), there is no staff "send to prospect" action, and the SkydiveOS share links always expose the clean master (D1, D5) | pipeline `api/gallery.py`, `api/app.py:2371`; SkydiveOS `emailService.js:2386` | Decide: surface the `/j/{code}` link to staff (with the BUG 420 reasoning revisited), and/or add a watermarked SkydiveOS share variant; add a server-side "send link" action |
| (2) **Customer shares the watermarked preview on social** | **Yes, verified.** Pipeline hero Share (native sheet / Copy / WhatsApp / Facebook / Email) on the LOCKED page; a friend lands on the same preview + Unlock CTA | `api/gallery.py:553-603`; `tests/test_gallery_share.py:113` | `og:image` for a real social card (D9); share analytics (D4); the customer-portal share (SkydiveOS) only exists for approved/clean media — a locked video never appears there |
| (3) **Dedicated share button on the AI gallery** | **Yes, verified** (2026-10-23) | `api/app.py:2608-2619` | Revocation (D2) and analytics (D4) if the client wants control/measurement |
| (4) **Other** | — | — | Candidates to ask: (a) should a share link be revocable / expire (today: pipeline never, SkydiveOS 30 d)? (b) should a shared viewer be able to DOWNLOAD (today SkydiveOS yes, pipeline only when owned)? (c) should the friend's unlock purchase be attributed to the sharer (referral / discount)? (d) should staff share from SkydiveOS at all, or only the pipeline gallery? (e) should a paid unlock propagate to SkydiveOS so staff can approve/share it there (D3)? (f) is the "friend's click marks media delivered" side effect acceptable (D8)? |

Recommended client questions, in order:
1. Who shares, to whom, for what: staff → prospect before purchase (1), customer → friends after/before purchase (2), or both?
2. For a pre-purchase share, is a **watermarked, non-downloadable** page the required form (pipeline behaviour), and should SkydiveOS's own share links ever carry the clean master?
3. Must links be revocable or time-limited? If yes, on which events (refund, customer request, media replaced, staff error)?
4. What do they want to measure: share clicks per channel, preview plays, unlock conversions from shared links? (None exists today.)
5. After a purchase, should the SkydiveOS record of that media become shareable/approvable (D3), or is the pipeline gallery the only customer surface?
