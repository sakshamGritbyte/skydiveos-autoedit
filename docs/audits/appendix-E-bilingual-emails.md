# Bilingual customer-email audit — SkydiveOS + skydiveos-autoedit (read-only, 2026-10-24)

Requirement (verbatim): *"All customer-facing emails must contain both French and English in a single email, with French first and English below. The subject must also contain both languages, e.g. 'Votre vidéo est prête / Your video is ready'. This is a bilingual-content requirement, not two separate emails."*

Repos audited:
- SkydiveOS backend: `/home/msi/Desktop/Development/skydiving/skydiving-os/backend` (commit ac83149 bilingual work; HEAD as checked out)
- AI pipeline: `/home/msi/Desktop/Development/skydiving-autoedit/skydiveos-autoedit`

Legend: **VERIFIED** = read in source / executed; **INFERRED** = derived from surrounding code or docs without executing. Nothing was changed, no email was sent (encoding checks used nodemailer `streamTransport` and Python `EmailMessage.as_string()` only).

---

## 0. Headline findings

| # | Sev | Finding | Where |
|---|-----|---------|-------|
| F1 | **P1 / client decision** | **Ordering is preference-led, not French-first.** `languageOrder()` returns `['en','fr']` when `primaryLang` normalises to `en`; subject, HTML sections, text sections and the logo band all follow that order. A customer with `preferredLanguage: 'en'` receives *"Your skydive video is ready / Votre vidéo de saut est prête"*, ENGLISH section first. VERIFIED by executing the composer (§6.3). Every one of the ~30 bilingual templates/inline composers passes `primaryLang: customer.preferredLanguage` (§3), so this affects all of them for EN-preferring customers. Also pinned *as intended behaviour* by `bilingualCustomerEmails.test.js:104-110`. | `backend/src/utils/bilingualEmail.js:51-61, 207-233` |
| F2 | **P1** | **The pipeline's own "your gallery is ready" email is English-only, plain text, English-only subject** (`Your skydive video is ready, {name}!`). It fires whenever `CUSTOMER_EMAIL_SENDER` ≠ `skydiveos`; the CODE default is `pipeline`. `.env.example` sets `skydiveos`, but the flag is per-stack `.env` and unverifiable from the repo. Legacy `send_delivery_email` (presigned links) is identical in language and is now dead code (0 callers). | `skydiveos-autoedit/api/delivery.py:311-371, 405-414, 447-500`; `api/config.py:141` |
| F3 | **P1** | **Media-delivery SMS is English-only** (`Hi {name}! Your skydive video is ready! Watch & download here: …`, plus the "new video added" / "updated" variants). Sent by SkydiveOS on every `delivered` callback, independently of the email. SMS is outside the literal "emails" wording of the requirement but is a customer-facing notification carrying the same message — client should rule. | `backend/src/utils/smsService.js:238-262`; dispatched from `mediaDeliverySmsService.js:153`, `mediaDeliveryEmailService.js` (followUp/updated), `mediaService.js` |
| F4 | **P1** | **Certificate email leads ENGLISH for a customer with no preference.** `certificateLanguage(customer)` returns `'en'` when `preferredLanguage` is null, and `_deliver` passes that as `primaryLang`, bypassing the `EMAIL_PRIMARY_LANGUAGE=fr` default every other template falls back to. The attached certificate **PDF is single-language** (customer's lang, default EN). | `backend/src/utils/certificateConfig.js:277-280`; `services/certificateService.js:372-376, 416-420` |
| F5 | **P1 (attachment)** | **Invoice PDF is English-only** ("INVOICE", "Bill To:", "Items", "Description/Qty/Unit Price/Total", "Subtotal:", "Amount Paid:", "Balance Due:", "PAID IN FULL"/"PARTIAL PAYMENT — … REMAINING"; date `dayjs().format('DD MMM YYYY')` English month). It is attached to the bilingual invoice email AND to Email A / balance receipt. | `backend/src/services/invoiceService.js:408-590` |
| F6 | **P2** | **SMS-mirror emails (boarding reminder, waiver-24h reminder, broadcasts) are bilingual ONLY if the operator-authored `SmsTemplate.body.fr` is non-empty.** `fr` is optional in the schema (`default: ''`); no seed provides French bodies (no seed scripts found). A single-body template sends an English-only email with the English subject. Broadcast email subject is a single operator string (no FR variant). | `utils/notificationEmailService.js:118-155`; `models/SmsTemplate.js:39-43`; `services/smsNotificationService.js:838-860` |
| F7 | **P2 (policy drift)** | **A waiver-dispatch fallback silently SETS `Customer.preferredLanguage`** (to `fallbackLanguage` or hard `'en'`) when the customer had none, and that value then drives the ordering of every later email (F1). I.e. an EN-only waiver bundle turns a no-preference Québécois customer into "English-first" for life. | `backend/src/utils/waiverService.js:2178-2183, 2201-2209` |
| F8 | **P2 (two senders)** | **Duplicate-send risk is real and config-gated, not code-gated.** SkydiveOS always sends its branded email on `delivered` (it has no knowledge of the pipeline flag); the pipeline sends unless `CUSTOMER_EMAIL_SENDER=skydiveos`. Exactly-one only holds when every stack's `.env` has been flipped (BUG 392, 2026-09-09). `deploy/ec2/customer-email-sender.sh audit` is the only proof. | `api/delivery.py:405`; `services/autoEditOrchestrationService.js:2160-2200`; SkydiveOS `CLAUDE.md:3282-3283` |
| F9 | **P3 (doc vs code)** | CLAUDE.md claims the media delivery email passes `primaryLang`; it does not — `mediaDeliveryEmailService._deliver` never passes it and loads the customer without `preferredLanguage`. Result: that one email is **always FR-first** (deployment default) — which happens to MATCH the client requirement, but contradicts the documented policy. Same for `sendInvoiceEmail`, `sendMediaDeliveryNotification` (manual media), `sendSigningRequest/Reminder`, `sendCustomerInvitation`, `sendPasswordResetLink`, `sendAccountSetupLink`, `sendTicket`, gift-card emails, `sendAllDocumentsSignedEmail`, `sendDocumentCompleted` — all called without `primaryLang` → FR-first always. | §3 table |
| F10 | **P3** | `EMAIL_PRIMARY_LANGUAGE` is undocumented: no `.env.example` in the SkydiveOS repo, not in `config/index.js`; only read raw via `process.env` in `bilingualEmail.js:54`. If an operator sets it to `en`, every no-preference customer gets English-first (VERIFIED §6.3). | `bilingualEmail.js:54` |

---

## 1. Inventory — every email path (A) with per-row assessment (B)

Columns: template/function (file:line) · trigger/caller · sender path · subject construction · FR complete · EN · same email · ordering · HTML/text same order · buttons/links/footer bilingual · attachments · locale formatting · test evidence · status.

Abbreviations: **SBE** = `emailService.sendBilingualEmail` (→ `composeBilingualEmail` → `sendEmail`); **CBE+SE** = inline `composeBilingualEmail` + `sendEmail(..., composed.branding)`; **RAW** = `sendEmail` with a single-language body; **STE** = `notificationEmailService.sendTemplatedEmail`.
Ordering: **PREF** = preference-led (customer `preferredLanguage` first, else `EMAIL_PRIMARY_LANGUAGE`, default fr); **FR-DEFAULT** = no `primaryLang` passed → deployment default (fr unless env overridden); **EN-ONLY**.
"Subject" for every bilingual row is `joinSubjects([primary, secondary])` = `"<primary subject> / <secondary subject>"` (`bilingualEmail.js:178-188`), so the subject order equals the body order.

### 1.1 Customer-facing — SkydiveOS, `emailService.js` templates

| Template (file:line) | Trigger / caller | Sender | Subject | FR | EN | One email | Ordering | HTML=text order | Buttons/links/footer | Attachments | Locale fmt | Tests | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `sendPasswordResetLink` emailService.js:272 | `authService.js:1083` forgot-password (customer **and** staff share this route — INFERRED) | SBE | `Réinitialisation de votre mot de passe / Password Reset Request` | ✓ | ✓ | ✓ | FR-DEFAULT (no primaryLang at caller) | ✓ | ✓ bilingual footer (`emailBranding.js:131-133`); button per section | — | expiry label via `expiryLabelFr` | bilingualSimpleTemplates | **PASS** |
| `sendAccountSetupLink` :320 | `authService.js:1374` (self-serve setup) | SBE | heading per lang | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | expiryLabel | bilingualSimpleTemplates | **PASS** (operator `opts.heading` override renders as given in both sections) |
| `sendCustomerInvitation` :399 | `authService.js:958` staff-initiated invite; `groupBookingService.js:191` lead provisioning | SBE | `Bienvenue chez Ultimate DZM – Complétez votre profil / Welcome to…` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | — | bilingualSimpleTemplates | **PASS** |
| `sendSigningRequest` :447 | `documentService.js:274,1712`, `documentController.js:602`, `waiverOverrideController.js:174,292,726`, `waiverService.js:3102` (legacy fan-out) | SBE | `Signature requise : X / Please Sign: X` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | expiry via fmtDate | bilingualSimpleTemplates | **PASS** |
| `sendSigningReminder` :526 | `documentService.js:1496` | SBE | `Rappel : … / Reminder: …` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | — | bilingualSimpleTemplates | **PASS** |
| `sendWaiverReminder` :567 | **no callers** (dead) | SBE | bilingual | ✓ | ✓ | ✓ | n/a | ✓ | ✓ | — | — | — | n/a (dead code) |
| `sendDocumentCompleted` :649 | `documentController.js:706` (non-consolidated mode only) | SBE | `Document signé : X / Document Signed: X` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | — | bilingualSimpleTemplates | **PASS** |
| `sendBookingConfirmation` :691 | **no callers** (dead) | SBE | bilingual | ✓ | ✓ | ✓ | n/a | | | | | | n/a |
| `sendGroupBookingEmail` :765 | `authController.js:202` import (group booking flow) | SBE | `Complétez votre réservation de groupe – REF / Complete Your Group Booking - REF` | ✓ | ✓ | ✓ | PREF (`primaryLang` param; caller passes? — INFERRED default) | ✓ | ✓ | — | fmtMoney/fmtDate | bilingualCustomerEmails | **PASS** |
| `sendGroupBookingWithAccountActivation` :885 | **no callers** (dead) | SBE | | ✓ | ✓ | ✓ | | | | | | | n/a |
| `sendTicket` :1025 | `checkInController.js:136`, `kioskController.js:2298,4290` (legacy non-consolidated mode) | SBE | `Votre billet de saut – N / Your Skydiving Ticket - N` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | fmtMoney/fmtDate(UTC business date) | bilingualSimpleTemplates | **PASS** |
| `sendCertificateEmail` :1141 (content `{en,fr}` shape) | `certificateService._deliver` :416 | SBE | operator wording `cfg.wording.fr.emailSubject / …en…` (defaults `Félicitations pour votre saut, {firstName} ! / Congratulations on your skydive, {firstName}!`) | ✓ (default wording fr; operator may leave `fr` blank → falls back to EN default wording, not EN operator text — `certificateConfig.js:152-153`) | ✓ | ✓ | **PREF but default = EN** (`certificateLanguage` → `'en'` when null; F4) | ✓ | ✓ | **PDF single-language** (customer lang, default EN) | per-lang `certificateVars` | task431Certificates, bilingualCustomerEmails | **PARTIAL** (F4 ordering for no-pref customers; PDF monolingual) |
| `sendCertificateEmail` legacy `{subject, body, lang}` shape :1158-1160 | no remaining callers (only `_deliver` which passes `content`) | RAW single-language | single | | | | EN-or-FR only | | | PDF | | | n/a (unreachable) |
| `sendMediaDeliveryNotification` :1167 | `mediaService.js:2334` manual media approval | SBE | `Votre vidéo de saut est prête ! / Your Skydiving Video Is Ready!` | ✓ | ✓ | ✓ | FR-DEFAULT (caller passes none) | ✓ | ✓ | — | fmtDate expiry | bilingualSimpleTemplates | **PASS** |
| `sendCheckInConfirmationEmail` :1274 | `checkInEmailService.js:117` (non-consolidated) | SBE | `✅ Enregistrement confirmé — N / ✅ Check-in Confirmed — N` | ✓ | ✓ | ✓ | PREF (`checkInEmailService.js:112`) | ✓ | ✓ | — | fmtMoney, tax lines as stored | consolidatedCustomerEmails, bilingualCustomerEmails | **PASS** (F1 applies) |
| `sendInvoiceEmail` :1444 | `invoiceService.js:622` (`sendInvoice`) | SBE | `Votre facture N – Ultimate DZM / Your Invoice N - Ultimate DZM` | ✓ | ✓ | ✓ | FR-DEFAULT (caller passes none) | ✓ | ✓ | **PDF English-only (F5)** | — | bilingualSimpleTemplates | **PARTIAL** (attachment) |
| `sendBookingWelcomeEmail` (Email A) :1734 | `paymentEventHandler.js:1427` on payment success | SBE | `🎉 Votre réservation est confirmée — N / 🎉 You're booked — N` | ✓ | ✓ | ✓ | PREF (`:1428`) | ✓ | ✓ (signing links listed per section) | **Invoice PDF English-only** when attached (`:1771`) | fmtMoney/fmtDate/tz | bilingualCustomerEmails, consolidatedCustomerEmails | **PARTIAL** (attachment; F1) |
| `sendBalanceReceiptEmail` :1965 | `paymentEventHandler.js:1303` | SBE | per-lang (paidInFull variants) | ✓ | ✓ | ✓ | PREF (`:1318`) | ✓ | ✓ | — | fmtMoney | bilingualCustomerEmails | **PASS** (F1) |
| `sendReadyToJumpEmail` (Email B) :2088 | `checkInEmailService.js:120` (consolidated) | SBE | `✈️ Vous êtes prêt(e) — N / ✈️ You're all set — N` | ✓ | ✓ | ✓ | PREF | ✓ | ✓ | — | fmtMoney | consolidatedCustomerEmails, bilingualCustomerEmails | **PASS** (F1) |
| `sendBundledSigningRequest` :2262 | `waiverService.js:3159` (bundled), `paymentEventHandler.js:1232` (rescue, no primaryLang), `staffWaiverService.js:501` (**staff** waivers) | SBE | `Action requise : signez vos N documents / Action required: …` | ✓ | ✓ | ✓ | PREF at 3159; FR-DEFAULT at 1232 | ✓ | ✓ | — | fmtDate expiry | bilingualCustomerEmails | **PASS** (F1) |
| `sendAllDocumentsSignedEmail` :2320 | `documentController.js:53` (signing ack) | SBE | `Tous les documents sont signés ✅ / All documents signed ✅` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | — | bilingualSimpleTemplates | **PASS** |
| `sendMediaGalleryDeliveryEmail` :2386 ("your video is ready" / "new video added" / "updated", locked & unlocked variants) | `mediaDeliveryEmailService._deliver` :386 (initial, retry, followUp, updated; also staff resend via `mediaDeliveryStatusService._resendEmail`) | SBE | `Votre vidéo de saut est prête / Your skydive video is ready` (+ variants) | ✓ complete, all 6 state variants | ✓ | ✓ (Cc to DZ on the same send) | **FR-DEFAULT** (no `primaryLang` passed; F9) → FR-first in practice | ✓ | ✓ | — | jumpDate passed pre-formatted (`''` today) | mediaDeliveryEmail, bilingualCustomerEmails | **PASS** vs requirement (FR first) — but only by accident of the default |
| `sendGiftCardEmail` :2541 | `customerGiftCardService.js:259` (recipient delivery; also `resendEmail` :938 → `deliverRecipientEmail`) | SBE | `X vous offre une carte-cadeau de 100,00 $ ! / X sent you a $100.00 gift card!` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — (no gift-card PDF exists) | fmtMoney/fmtDate | giftCardEmailStartDate, bilingualSimpleTemplates | **PASS** |
| `sendGiftCardPurchaseReceiptEmail` :2656 | `customerGiftCardService.js:721` (buyer receipt) | SBE | `Votre achat de carte-cadeau de … / Your … gift card purchase` | ✓ | ✓ | ✓ | FR-DEFAULT | ✓ | ✓ | — | fmtMoney | bilingualSimpleTemplates | **PASS** |

### 1.2 Customer-facing — inline composers (`composeBilingualEmail` + `sendEmail`)

| Site (file:line) | Email | Subject (fr / en) | FR | EN | Ordering | Branding 6th arg | Status |
|---|---|---|---|---|---|---|---|
| `bookingController.js:3634-3700` | Legacy booking-confirmed fan-out (verify-payment) | `Ultimate DZM - Réservation confirmée : … / Booking Confirmed: …` | ✓ | ✓ | PREF (`:3693`) | ✓ | PASS (F1) |
| `financeController.js:2513-2570` | Refund receipt (manual refund) | `Confirmation de remboursement — 50,00 $ / Refund Confirmation — $50.00` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `kioskController.js:2116-2200` | Kiosk booking confirmed (+ account setup) | `Réservation confirmée… / Booking Confirmed…` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `kioskController.js:4882-4927` | Kiosk account setup / set password | `Bienvenue ! Configurez votre compte / Welcome! Set up your account` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `authService.js:1138-1172` | Email-change OTP | `Confirmez votre nouvelle adresse courriel / Confirm your new email address` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `bookingRescheduleService.js:1049,1519`; `bookingService.js:1461` | Reschedule notices | `Réservation reportée — N / Booking Rescheduled — N` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `funJumperPromotionService.js:348` | Waitlist promotion | `Vous avez été promu(e) de la liste d'attente ! / You've been promoted…` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `groupBookingService.js:1059-1165` | Group booking confirmation | `✅ Réservation de groupe confirmée — REF / ✅ Group Booking Confirmed — REF` | ✓ | ✓ | PREF (customer or `groupBooking.preferredLanguage`) | ✓ | PASS (F1) |
| `paymentEventHandler.js:1620-1735` | Legacy "Payment Confirmed" fan-out | `Paiement confirmé… / Payment Confirmed…` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `paymentProviderService.js:399-456` | Refund issued (provider refund) | `Remboursement émis — … / Refund Issued — …` | ✓ | ✓ | PREF | ✓ | PASS (F1) |
| `qrCodeService.js:99-262` | Check-in QR code (full DOCTYPE doc, one `cid:qrcode` attachment used by both sections) | `Votre code QR d'enregistrement - N / Your Check-In QR Code - N` | ✓ | ✓ | PREF (`preferredLanguage` param) | ✓ (`wrapDocument`) | PASS (F1); QR PNG is language-neutral |
| `qrCodeService.js:279-410` | "You are manifested" | `Vous êtes manifesté(e) ! - N / You are Manifested! - N` | ✓ | ✓ | PREF | ✓ | PASS (F1) |

### 1.3 Customer-facing — SMS-mirror / DB-template emails (`notificationEmailService.sendTemplatedEmail`)

| Trigger | Subject | Body source | Bilingual? | Ordering | Status |
|---|---|---|---|---|---|
| `boarding_reminder` (`smsNotificationService.js:160-184`) | `Embarquement bientôt — Load N à DZ / Boarding soon — Load N at DZ` (code-defined FR+EN `EMAIL_SUBJECTS` :81-90) | `SmsTemplate.body.{en,fr}` operator-authored | **Only if `body.fr` non-empty** (`notificationEmailService.js:118-120`); else a single-language email: EN body, and the subject follows the customer's language (`:153` — an fr-preferring customer gets the FR subject over an EN body) | PREF (`customer.preferredLanguage`) | **PARTIAL** (F6) |
| `waiver_reminder_24h` | `Veuillez signer votre décharge — DZ / Please sign your waiver — DZ` | same | same | PREF | **PARTIAL** (F6) |
| `broadcast_message` (`:843-860`) | operator `emailSubject` single string, else `Message from {{dzName}}` (EN only) | `BroadcastMessage.body.{en,fr}` (fr optional) | body bilingual only if fr filled; **subject never bilingual** | PREF | **FAIL on subject / PARTIAL** (F6) |

### 1.4 Customer-facing — AI pipeline (`skydiveos-autoedit`)

| Function | Trigger | Subject | Body | Bilingual | Gate | Status |
|---|---|---|---|---|---|---|
| `api/delivery.py:311 send_gallery_email` via `send_gallery_email_once:374` | `deliver_job` (`:528, :666, :708`) on approve/auto-deliver | `Your skydive video is ready, {name}!` (EN only) | plain text EN: "Hi X, / Your skydive video is ready! 🪂 / View and download … / Blue skies!" | **NO** | skipped iff `CUSTOMER_EMAIL_SENDER=skydiveos` (`:405`); code default `pipeline` (`config.py:141`); `.env.example:183` = `skydiveos` | **FAIL when active (F2)** |
| `api/delivery.py:447 send_delivery_email` (legacy presigned links) | **no callers** (dead) | EN only | EN only | NO | — | n/a |
| SMS | none in the pipeline (only `?s=m` tag handling) | | | | | n/a |

### 1.5 Customer-facing SMS (SkydiveOS) — for completeness

| Function | Text | Bilingual | Status |
|---|---|---|---|
| `smsService.sendMediaReadyNotification:238` | `Hi {name}! Your skydive video is ready! Watch & download here: {url}` | NO | **FAIL (F3)** if SMS in scope |
| `sendMediaAdditionalNotification:252`, `sendMediaUpdatedNotification:259` | EN only | NO | FAIL (F3) |
| `sendTemplatedSMS:125` (boarding/waiver/broadcast) | `body.fr` if customer fr AND fr body exists, else `body.en` | single language per SMS (by design — 640-char cap) | n/a (SMS is single-language by nature; client to confirm) |

### 1.6 Staff / system / third-party — out of scope per CLAUDE.md, listed for the client question

| Function | Audience | Language |
|---|---|---|
| `sendStaffInvitation` emailService.js:373 (RAW) | staff | EN only |
| `sendBundledSigningRequest` via `staffWaiverService.js:501` | staff waiver | bilingual (PREF from staff `lang`) |
| `systemController.sendTestEmail:288` | admin SMTP test | EN |
| `pilotNotificationService.js:164`, `reconciliationService.js:369`, `retellAlertService.js:40` (email + SMS), `mediaUnlockRetryService.js:463` (stuck-unlock alert) | ops | EN |
| `aimTemplateService.js:441` (AIM safety PDF to a recipient + association) | external safety body / pilot — INFERRED staff-side | EN |
| `stripeService.js` imports `sendEmail` but never calls it | — | — |

---

## 2. Language selection trace (C)

- **`Customer.preferredLanguage`**: `models/Customer.js:349-353` — `enum ['en','fr',null]`, `default: null`. Populated by: public booking form via `Booking.customerInfo.preferredLanguage` → `customerService.js:53-153` (materialisation), customer profile/portal (`customerController.js:2083-2516`, whitelisted EN/FR/null), kiosk/portal language prompt, `funJumperService.js:32` (self-manifest waiver language), group booking (`groupBookingService.js:657`), and **`waiverService.js:2201-2209` (persists the auto-resolved waiver language — F7)**.
- **`Staff.preferredLanguage`**: `models/Staff.js:230-234`, same enum, used only for the staff waiver email (`staffWaiverService.js:344,504,640`).
- **`EMAIL_PRIMARY_LANGUAGE`**: read ONLY at `bilingualEmail.js:54`; normalised; unknown → `'fr'`. Not in any `.env.example` (none exists in the SkydiveOS repo), not in `config/index.js` (F10).
- **Resolution** (`bilingualEmail.js:42-61`): `normalizeEmailLang(value)` → lowercase, split on `-`/`_`, must be `fr`|`en` else `null`. `resolvePrimaryLang(preferred)` → caller value if valid, else env, else `fr`. `languageOrder()` → `['en','fr']` iff primary is `en`.
  - pref `fr` → FR first; `en` → **EN first**; `null`/missing → env default (fr); invalid (`de`, `''`) → env default; `fr-CA`/`EN` → normalised. VERIFIED by execution (§6.3).
- **Suppression paths (can a language be dropped?)**:
  - Composer: never — both `render(lang)` calls always happen; `pick()` falls back to `en` when a table lacks `fr` (so a half-translated table renders EN text under the "Français" heading — none found in `emailService.js`, all tables have both keys; not exhaustively diffed for inline sites but every inline site defines `en:{}` and `fr:{}`).
  - `notificationEmailService.js:118-155`: bilingual **only** when `template.body.fr` is non-blank (F6). Single-body → single-language, EN subject unless customer is `fr` (`:153`).
  - `sendCertificateEmail:1150-1160`: bilingual only when `content.en && content.fr`; `_deliver` always builds both (`certificateService.js:405-413`), and `resolveCertificateConfig` fills blank operator wording with per-language defaults — but operator `fr` wording left blank gets the **default FR text**, not a translation of the operator's EN text (INFERRED consequence of `certificateConfig.js:152-153`).
  - `certificateLanguage()` (`certificateConfig.js:277-280`): null → `'en'` → F4.
  - `emailBranding.js:131-133`: bilingual footer only when `branding.bilingual` — set by the composer; every inline site forwards `composed.branding` (VERIFIED on all 13 sites). The footer notice is always FR-then-EN regardless of preference; the logo band headline/subtext follow the primary order.

---

## 3. Who passes `primaryLang` (drives F1 vs F9)

PREF (customer preference passed): `paymentEventHandler.js:1318,1428,1728`; `kioskController.js:2196,4926`; `checkInEmailService.js:112`; `bookingService.js:1505`; `groupBookingService.js:1159`; `staffWaiverService.js:504`; `financeController.js:2563`; `waiverService.js:3162`; `bookingController.js:3693`; `qrCodeService.js:252,408`; `notificationEmailService.js:136`; `funJumperPromotionService.js:359`; `bookingRescheduleService.js:1098,1584`; `authService.js:1165`; `paymentProviderService.js:452`; `certificateService.js:419` (with EN default).
FR-DEFAULT (none passed): `invoiceService.js:622`, `mediaService.js:2334`, `authService.js:807(staff),958,1084,1374`, `documentService.js:274,1496,1712`, `documentController.js:53,602,706`, `waiverOverrideController.js:174,292,726`, `groupBookingService.js:191`, `checkInController.js:136`, `kioskController.js:2298,4290`, `customerGiftCardService.js:259,721`, `paymentEventHandler.js:1232`, `waiverService.js:3102`, `mediaDeliveryEmailService.js:386`.

---

## 4. Attachments (D)

| Attachment | Generator | Bilingual? | Notes |
|---|---|---|---|
| Invoice PDF | `invoiceService.renderInvoicePdf` :408-590 (pdfkit) | **NO — English only** (F5). Date `DD MMM YYYY` via dayjs (EN months). Money via `affixes` (frozen currency prefix/postfix — can be `$`/` $`), but labels/status banner hardcoded EN. | Attached by `sendInvoiceEmail`, `sendBookingWelcomeEmail` (Email A), and per `kioskController.js:2304` ticket invoice path. |
| First-jump certificate PDF | `certificateService.generateCertificatePdf` :150-200 | **Single-language** = `certificateLanguage(customer)` (EN default). `(Pilot)/(Pilote)` suffix, `certificateText` from operator wording per lang, dates via per-lang `certificateVars`. | One PDF per email; not both languages. |
| Gift card PDF | none exists (`GiftCardTemplate` is a product model, not a template) | n/a | |
| ICS calendar | none found (`grep text/calendar|VCALENDAR|\.ics` → 0 hits) | n/a | |
| QR code PNG | `qrCodeService.js:258-264` (`cid:qrcode`, one attachment referenced by both sections) | language-neutral | |
| AIM PDF (`aimTemplateService`) | pdf-lib fill | EN | staff/authority facing |
| Reports (`reportService`, `financeService`, `safetyService`) | pdfkit | EN | staff only (INFERRED) |

---

## 5. Delivery & reliability (E)

- **Two senders for one event (F8)**: SkydiveOS `handleStatusCallback` → `mediaDeliveryEmailService.sendForJob` (always) + `notifyCustomerDelivered` SMS. Pipeline `deliver_job` → `send_gallery_email_once` unless `customer_email_sender == 'skydiveos'` (`api/delivery.py:405`). No cross-check exists; SkydiveOS never asks the pipeline whether it emailed. `deploy/ec2/customer-email-sender.sh audit` verifies per stack (HEAD contains gate commit, worker image has the gate, `printenv` matches `.env`, log line "customer email delegated to SkydiveOS", no `email_sent_at`). Local dev box: no `.env` present here, so the live value is **unverified** from this audit.
- **Exactly-once within SkydiveOS**: `MediaDeliveryNotification` unique `dedupeKey` reserved BEFORE SMTP (`mediaDeliveryEmailService.js:340-370`); retries via `mediaDeliveryEmailRetryService` on transient errors (`emailRetryPolicy`), terminal `failed` row otherwise; staff resend keyed `media-delivery-resend:{job}:{requestKey}:{channel}`. SMS-mirror: `SmsSendRecord` reserve-before-send (`notificationEmailService.js:199-209`). Certificates: `CertificateDelivery` rows with the same retry policy. Pipeline: `O_EXCL` claim + `email_sent_at` (`delivery.py:415-445`). VERIFIED.
- **Rendering failure**: `composeBilingualEmail` throws only on a non-function `render`; template exceptions propagate to the caller; every caller wraps in try/catch and logs (non-fatal) — e.g. `mediaDeliveryEmailService._deliver` writes `pending`/`failed`.
- **Encoding (VERIFIED offline)**: nodemailer 7.0.12 (`package.json:55`) — subject emitted as `=?UTF-8?Q?…?=` (RFC 2047), `text/plain; charset=utf-8` + `text/html; charset=utf-8`, quoted-printable. `sendEmail` passes `subject/text/html` straight to `sendMail` (`emailService.js:118-136`), so accents in subjects/bodies are safe. Pipeline: `email.message.EmailMessage.set_content()` → `Content-Type: text/plain; charset="utf-8"`, base64, subject RFC 2047-encoded by the stdlib — safe if a bilingual subject were ever added.
- **Cc**: the media delivery email carries a visible `Cc` to the DZ list on the SAME send (`emailService.js:123-126`), so the DZ sees the bilingual copy.
- **Hint line**: `composeBilingualEmail` prints "English version below ↓" / "Version française ci-dessous ↓" above the first section (`bilingualEmail.js:139-142, 224-226`); the hint is in the SECOND language, which is correct.

---

## 6. Tests (F)

### 6.1 SkydiveOS (`npx jest --silent …`, run 2026-10-24, nothing modified)
```
PASS giftCardEmailStartDate, bilingualSimpleTemplates, notificationEmailPipeline, bilingualCustomerEmails,
     mediaDeliveryEmail, bilingualInlineEmails, consolidatedCustomerEmails, task431Certificates
Test Suites: 8 passed, 8 total
Tests:       172 passed, 172 total
```
Log: `jest_bilingual.log`

Note: `bilingualCustomerEmails.test.js:104-110` **asserts** `primaryLang:'en'` → order `['en','fr']` and `languageOrder()` under `EMAIL_PRIMARY_LANGUAGE=en` → `['en','fr']` (`:116`). The suite therefore pins the preference-led ordering; a French-first-always rule would require changing those assertions.

### 6.2 Pipeline (`uv run --no-sync pytest tests/test_delivery.py -q -p no:cacheprovider`)
```
67 passed in 1.05s
```
Log: `evidence/` (pytest logs). `tests/test_delivery.py:1648-1700` pins the `CUSTOMER_EMAIL_SENDER` gate (BUG 392). No test asserts the subject text, so the English-only subject is not pinned.

### 6.3 Rendered evidence (composer executed via `evidence/compose_evidence.mjs`, render = media-delivery subjects)
```
preferredLanguage="fr"  → order fr -> en
  subject : Votre vidéo de saut est prête / Your skydive video is ready
  text    : ["════════════ FRANÇAIS ════════════","","Bonjour Alex,"]
preferredLanguage="en"  → order en -> fr
  subject : Your skydive video is ready / Votre vidéo de saut est prête
  text    : ["════════════ ENGLISH ════════════","","Hi Alex,"]
preferredLanguage=null / "de" / "fr-CA" → fr -> en (same as "fr")
preferredLanguage="EN" → en -> fr
EMAIL_PRIMARY_LANGUAGE=en + preferredLanguage=null → en -> fr
```

---

## 7. Gaps by severity

**P1**
1. F1 — EN-preferring customers get English first (subject, body, band). Violates the verbatim "French first" rule unless the client accepts preference-led ordering. Affects every PREF site in §3.
2. F2 — Pipeline email English-only, plain text; active on any stack where `CUSTOMER_EMAIL_SENDER` is not `skydiveos`.
3. F4 — Certificate email English-first for no-preference customers (hard `'en'` default), certificate PDF monolingual.
4. F5 — Invoice PDF English-only, attached to three customer emails.
5. F3 — Delivery SMS English-only (if SMS is in scope).

**P2**
6. F6 — SMS-mirror emails bilingual only when the operator fills `body.fr`; broadcast subject never bilingual; default `Notification — {{dzName}}` / `Message from {{dzName}}` EN-only.
7. F7 — Waiver-dispatch auto-resolved language persisted to `Customer.preferredLanguage` ('en' hard default) silently flips future email ordering.
8. F8 — Dual-sender exactly-once depends on per-stack `.env` flips, not code.

**P3**
9. F9 — Doc/code drift: media-delivery (and ~15 other) templates are FR-DEFAULT not PREF; CLAUDE.md overstates.
10. F10 — `EMAIL_PRIMARY_LANGUAGE` undocumented; `=en` silently makes every no-preference email English-first.
11. Dead code: `sendBookingConfirmation`, `sendWaiverReminder`, `sendGroupBookingWithAccountActivation`, legacy `sendCertificateEmail` single shape, pipeline `send_delivery_email`.

---

## 8. Client clarifications (G)

1. **Ordering**: French first ALWAYS (verbatim requirement), or the customer's preference first (what the code does)? If always-French, `languageOrder()` becomes constant, `EMAIL_PRIMARY_LANGUAGE` is removed/ignored, `certificateLanguage` default stops leaking into the email, and `bilingualCustomerEmails.test.js:104-116` changes.
2. **Staff-only emails** (staff invitation, SMTP test, pilot/recon/Retell/stuck-unlock alerts, AIM): exempt? Currently English-only by design.
3. **Attachments**: must the invoice PDF and the certificate PDF be bilingual (two-column / two-page), or is a single-language document acceptable while the covering email is bilingual?
4. **SMS**: does the bilingual rule extend to SMS (media-ready, boarding, waiver reminders, broadcasts)? One bilingual SMS roughly doubles segment cost; current code is single-language.
5. **Operator-authored content** (SMS/broadcast templates, certificate wording): is it acceptable that an operator who leaves `fr` empty sends English-only, or must the system refuse/require both bodies (schema `required: true` on `fr`)?
6. **Exempt transactional emails**: password reset / email-change OTP / account setup are already bilingual — confirm none are exempt.
7. **Pipeline email**: confirm every production stack has `CUSTOMER_EMAIL_SENDER=skydiveos` (run `bash deploy/ec2/customer-email-sender.sh audit`), or require the pipeline's own email to be made bilingual as a safety net.
