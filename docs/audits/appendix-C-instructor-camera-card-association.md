# Audit — instructor ↔ camera ↔ SD-card association (read-only, 2026-10-24)

Requirement under test (verbatim): *"An instructor may use multiple cards and potentially
multiple cameras. The system should correctly recognize footage and associate it with the
relevant instructor."*

Repos: `A` = `/home/msi/Desktop/Development/skydiving-autoedit/skydiveos-autoedit` (pipeline),
`S` = `/home/msi/Desktop/Development/skydiving/skydiving-os` (SkydiveOS). Every claim is marked
**VERIFIED** (read in code / ran) or **INFERRED** (reasoned from code, not executed on hardware).

---

## (a) Data model (text diagram)

```
SkydiveOS (Mongo, mongoose)                                   Pipeline (Mongo `cameras` + files)
────────────────────────────                                  ───────────────────────────────────
staffs                                                        cameras   (ingest/registry.py)
 ├ _id                      ◄── QR payload "skydiveos-staff:<_id>"      ├ camera_id   ← LAST-4 digits of GoPro serial, UNIQUE idx
 ├ goproSerial  String, unique+sparse  (ONE camera per staff,           ├ instructor_id (SkydiveOS staffs._id, NOT unique → N cameras/instr)
 │                ONE staff per camera; a re-pair REPLACES it)          ├ role  'instructor'|'external'|null  (static HINT only)
 ├ sdCards[]    [{serial, label, addedAt, addedBy}]                     ├ active, name, paired_at
 │                multikey unique sparse idx `sdCards_serial_unique`    
 │                (MANY cards per staff, ONE staff per card, max 32)    raw-storage/_camera-staging/<camera_id>/<date>/<FILE>  (staging tree)
 └ userId                                                               raw-storage/_camera-staging/<camera_id>/.transferred.json (retention ledger, per name+size)
                                                                        s3://bucket/raw/<camera_id>/<YYYY-MM-DD>[/<fingerprint>]/<FILE>
loads.jumpers[] { instructor, secondaryInstructor, assignedCameraman, booking, customer, mediaPackage, videoType, mediaAddOnRefs }

media (raw row)                                               notify payload  POST {SKYDIVEOS}/api/media/raw-upload
 ├ cameraman = staff._id, uploadedBy = staff.userId              { s3_key, camera_id, instructor_id?, camera_role?, captured_at?,
 ├ s3 metadata.goproSerial = camera_id                             staff_id? + staff_source:"qr", sd_card_serial? }
 └ autoMatch { status matched|ambiguous|unmatched|skipped,
               reason, cameraRole, capturedAt, checkedAt,
               staffSource 'qr'|'sd-card'|null, rule,
               matcher 'staff'|'camera', cameraId, sdCardSerial }
```

Evidence (VERIFIED):
- `S/backend/src/models/Staff.js:110-120` — `goproSerial: { type: String, trim, unique: true, sparse: true }` with the comment "A serial belongs to exactly one instructor".
- `S/backend/src/models/Staff.js:128-140` (subdoc) and `:310-313` — `staffSchema.index({ 'sdCards.serial': 1 }, { unique: true, sparse: true, name: 'sdCards_serial_unique' })`.
- `S/backend/src/utils/sdCardSerial.js:20-36` — `SD_CARD_SERIAL_MAX_LEN=64`, `MAX_SD_CARDS_PER_STAFF=32`, `normalizeSdCardSerial` = strip whitespace + UPPERCASE (twin of `A/ingest/sdcard.py:114-124`).
- `S/backend/src/services/sdCardService.js:63-68` (`assertCardFree` → 409), `:94-118` (`addCardTo` relabels an existing serial, `$set`s the whole list), `:167-171` (`resolveStaffForSdCard` → `Staff.findOne({'sdCards.serial': serial})`).
- `S/backend/src/services/cameraService.js:36-41` (`assertSerialFree` → 409 if another staff holds it), `:75-90` (`persistSerial` = `findByIdAndUpdate(staffId, { goproSerial: serial })` — overwrites the previous value, i.e. one camera per staff), `:48-69` (also POSTs `/cameras/{serial}/assign` to the pipeline; refuses to persist locally if the AI side rejects).
- `A/ingest/registry.py:48-61` (`CameraRecord`), `:117` (unique index on `camera_id` only), `:185-214` (`assign_instructor` upserts; no check that the instructor already owns another camera), `:216-233` (`list_cameras(instructor_id=…)` is a filter, so N cameras per instructor is the designed shape).
- `A/api/app.py:3283-3305` — `POST /cameras/{camera_id}/assign` passes the path param straight to the registry (no `_normalize_camera_id`, no 409). The 409 branch in `cameraService.js:57-59` is therefore never produced by this pipeline (INFERRED: dead branch kept for a future conflict rule).
- `S/backend/src/models/Media.js:400-436` — `autoMatch` incl. `matcher`, `cameraId`, `sdCardSerial`, `staffSource`.
- `S/backend/src/services/mediaService.js:884-895` (S3 metadata `goproSerial: cleanSerial`), `:1155-1165` (`_createRawMedia` attributes `cameraman`/`uploadedBy` to the resolved staff).

Pairing surfaces (VERIFIED): `S/backend/src/routes/cameraRoutes.js:34-44` — `POST /admin/cameras/:serial/assign`, `POST /me/camera/assign`, `POST|DELETE /admin/sd-cards/:serial[/assign]`, `POST /me/sd-cards/assign`, `DELETE /me/sd-cards/:serial`. UI: `S/frontend/src/pages/app/StaffPage.js:2761-2800` (`GoProPairSection`, ONE serial input, button reads "Change" when one exists), `:3772-3790` ("Change"/"Assign Camera" per row), `:3237-3252` (`SdCardsSection mode="admin"` + `CameraQrCard`), `S/frontend/src/pages/app/ProfilePage.js:735-750` (`MyCameraSection currentSerial=…` + `SdCardsSection mode="me"`). `S/backend/src/scripts/pairGoProSerial.js` is the CLI twin of the camera pairing (one serial).

---

## (b) Complete identification flow (ingest box → notify → SkydiveOS → job)

1. **Card detected** — `A/ingest/sdcard.py:325-360` (`_mounts_with_dcim` → `find_cards`): each mount yields `SdCard(camera_id=card_id_for_mount(m), mount, card_serial=card_serial_for_mount(m))`.
   - `camera_id` = last 4 digits of `"camera serial number"` in `MISC/version.txt` (`:264-284`); fallback `sd-<volume label>` on POSIX / `sd-<drive letter>` on Windows (`:285-293`, warning logged).
   - `card_serial` = platform probe (`:219-244`), normalized; `None` on any failure (never raises).
2. **Pull** — `A/api/app.py:732-775` (`_sdcard_pull`): re-resolves the mount, reads `card_serial`, stamps it on the operator banner (`card_status.card_identity`, `A/ingest/cardstatus.py:149-164`) and passes `source={"transport":"sdcard","card_serial":…}` to `pull_camera` (`A/ingest/pull.py:180-260`), which stages to `_camera-staging/<camera_id>/<date>/` (`A/ingest/storage.py:63-79`), writes `<stem>.ingest.json`, and emits one event per NEWLY staged clip. An already-staged path is skipped (`A/ingest/pull.py:69-71`, `is_complete` = mp4 + manifest exist, `A/ingest/storage.py:87-89`).
3. **Hold until the whole card is staged** (QR mode) — `A/ingest/discovery.py:694-717` (`_HoldingEventEmitter`), so a marker filmed last still governs.
4. **Materialize** — `A/ingest/discovery.py:791-895`:
   - `instructor_id = registry.instructor_for(camera_id)`; `camera_role = registry.role_for(camera_id)` (static hint, `:805-806`).
   - `role_resolver` (`matcher_role_resolver`, `:122-150`, wired at `A/api/app.py:965-968` when `MONGO_URL` is set) → `FootageMatcher.resolve(camera_id, captured_at)` → camera's OWNER's slot on the load overrides the hint (`:812-820`).
   - `identity_resolver` (`qr_identity_resolver`, `A/ingest/qr.py:449-497`, wired `A/api/app.py:970-975`): builds `QrSessionIndex` from the day's sidecars; a marker clip is uploaded under `raw/<camera_id>/<date>/markers/` and NEVER notified (`:826-841`); otherwise `staff_id` + (if the matcher resolves) `role` from `resolve_for_staff` (`A/ingest/match.py:1235-1319`). No marker → `staff_id=None` + WARNING "falling back to the card-serial / camera-serial match" (`:852-856`).
   - `card_serial` read off `event["source"]` (`:859-862`).
   - Upload + notify (`s3_notify_uploader`, `:376-430`): key = `raw/{camera_id}/{YYYY-MM-DD}/{FILE}` (`raw_object_key`, `:280-304`, fingerprint sub-path on a same-key size mismatch); payload `{s3_key, camera_id, instructor_id?, camera_role?, staff_id?+staff_source:"qr", sd_card_serial?, captured_at?}` (`:404-422`), bearer = `AUTO_EDIT_API_KEY`.
   - On success → `record_uploaded(<camera dir>, name, s3_key, size)` (`:881-894`), which is the ONLY thing that authorises the next pull's `_sweep_card` to delete that file (`A/ingest/pull.py:127-178`, `A/ingest/retention.py:145-182` size-matched).
5. **SkydiveOS consumer** — `S/backend/src/controllers/mediaController.js:42-66`: `serial = gopro_serial || serial || camera_id`, `staffId = staff_id`, `sdCardSerial = sd_card_serial || sdCardSerial || card_serial || x-sd-card-serial`. → `mediaService.registerRawFromS3` (`S/backend/src/services/mediaService.js:946-1011`; idempotent by `s3Key`, a repeat returns the existing row).
6. **Identity order** — `mediaService._resolveRawIdentity` (`:1047-1076`), VERIFIED:
   1. `staff_id` (QR) → `_resolveStaffById` (`:1083-1096`, 404 if no such staff, 400 if no `userId`). If a card serial is also present and pairs to a DIFFERENT staff → `logger.warn('QR marker names a different staff than the card's pairing — QR wins')` (`:1052-1059`). **Logged, never refused.**
   2. `sd_card_serial` → `sdCardService.resolveStaffForSdCard` (`:1064-1071`) → `byStaff:true, staffSource:'sd-card'`.
   3. `_resolveRawInstructor` (`:1103-1128`): `Staff.findOne({ goproSerial: cleanSerial })` **exact** match on the value the box sent (the last-4 `camera_id`), then `Staff.findById(instructor_id)`, else **404** naming the GoPro serial and the unpaired card. A 4xx is treated as permanent by the box — no retry (`A/ingest/discovery.py:748-766`).
7. **Match → job** — `_dispatchRawFootage` (`:1021-1030`): `byStaff` → `processRawFootageByStaff` → `matchFootageByStaff` (`S/backend/src/services/autoEditOrchestrationService.js:365-485`: candidates = jumpers where this staff is `instructor` or `assignedCameraman` on an eligible load; narrow by business day → ±[-30 m,+150 m] departure window → latest departure before capture; tie → `ambiguous`; 0 → `unmatched`; a resolved jumper with no booking → `unmatched`). Otherwise → `processRawFootage` → `matchFootageToBooking` (`:259-300`: loads on the capture day ±1, `capturedWithinLoadWindow` mandatory, `jumperFillsCameraRole` by payload `camera_role` (`:110-125`), >1 → `ambiguous`). Outcome stamped on `media.autoMatch` with `matcher`, `cameraId`, `sdCardSerial` (`:1201-1216`, `:1329-1341`); a `matched` outcome → `startOrAttachJob` → pipeline `POST /jobs` + `POST /jobs/{id}/upload {s3_key}`.

Pipeline-side mirror (used only for `camera_role`, and by the local bridge/`check_*` scripts): `FootageMatcher._staff_for_camera` (`A/ingest/match.py:1140-1173`) matches `goproSerial` **exact OR regex suffix** and raises `AmbiguousMatch` when the suffix fits >1 staff; `evaluate_ownership` (`:844-977`) enforces day (T1), window (T2, one same-day out-of-window exception logged as WARNING), booking (T3), no-contradiction (T4).

---

## (c) Scenario matrix

| # | Scenario | Supported? | How / where it breaks |
|---|---|---|---|
| 1 | One instructor, one camera, one card, QR marker filmed | **Yes** | QR → `staff_id` → staff matcher. `A/ingest/qr.py:292-406`, `S/mediaService.js:1050-1061` |
| 2 | One instructor, **several cards**, no QR, cards paired in SkydiveOS | **Yes** (by design) | `Staff.sdCards[]` many-per-person; `resolveStaffForSdCard` rung 2. VERIFIED `sdCardPairing.test.js:85` ("an instructor can hold SEVERAL cards") |
| 3 | One instructor, several cards, **no pairing, no QR** | **Only via the camera** | Falls to `goproSerial` exact match on last-4; works only if the camera that held the card is the one paired to that instructor |
| 4 | **Several cards in ONE camera, same day** | **Partially** | Identity fine (card serial / QR). But staging, ledger and S3 key are all per `camera_id`+date+filename: a second freshly-formatted card restarts at `GX010001.MP4` → `is_complete()` says "already staged" and the clip is **skipped, never uploaded** (`A/ingest/pull.py:69-71`). Risk R1 below |
| 5 | One instructor, **two cameras** (e.g. handcam + a spare), no QR, no card pairing | **No** | `Staff.goproSerial` is a scalar with a unique index; pairing the 2nd camera overwrites the 1st (`cameraService.js:75-80`). The un-paired camera's clips → `_resolveRawInstructor` 404 → permanent hand-off error |
| 6 | One instructor, two cameras, **cards paired** (or QR) | **Yes** | The card/QR identifies the person; `camera_id` is only the storage key (`mediaController.js:52-56`). Pipeline registry allows N cameras per `instructor_id` (`registry.py:216-233`) |
| 7 | Same person instructor on load 1, cameraman on load 2, same camera | **Yes** | Role per jump from the slot: `A/ingest/match.py:1282-1301`, `S/autoEditOrchestrationService.js:390-394` |
| 8 | **Two instructors share one camera** (different days or loads), no QR / card pairing | **Not safely** | Camera is paired to ONE staff; the borrower's clips resolve to the OWNER and are matched against the owner's loads. Usually `unmatched` (refuse), but if the owner also holds a slot in the same flight window → **misassignment** (R3) |
| 9 | Card moved between cameras | **Yes** | `card_serial` follows the plastic; staging/ledger follow the camera (`A/ingest/sdcard.py:28-35`) |
| 10 | Card **reformatted** (in camera or PC) | **Must re-pair** | Volume serial changes; hardware CID not readable through a USB reader (`sdcard.py:151-198`; `S/CLAUDE.md:3537-3540`). Un-paired card → rung 3 (camera) or 404 |
| 11 | Card with no readable `MISC/version.txt` | **Degraded** | `camera_id = sd-<label>` / `sd-E` on Windows (`sdcard.py:285-293`) — unstable identity, `goproSerial` lookup for `sd-E` can never match; only QR/card pairing can attribute it. All unlabelled cards in reader `E:` share one staging tree + ledger (`deploy/windows/README.md:168`) |
| 12 | Card serial unreadable on the host | **Degraded** | `card_serial=None` → no `sd_card_serial` in the payload → QR or camera only (`scripts/check_sdcard.py:89-93` prints the warning) |
| 13 | QR names staff A, card is paired to staff B | **QR wins, logged** | `mediaService.js:1052-1059` — never refused |
| 14 | No QR, card unpaired, camera unpaired | **Refused** | 404 naming both (`mediaService.js:1115-1123`); box logs "REJECTED … Not retrying" (`discovery.py:757-766`); file stays on card |
| 15 | Staff doc has no `goproSerial` | **Camera path dead** | `check_match --readiness` lists them: "their footage can never be matched" (`A/scripts/check_match.py:125`) |
| 16 | `goproSerial` stored as the FULL serial (`C3504224544313`) | **Pipeline OK, SkydiveOS NOT** | Pipeline suffix-matches (`match.py:1159-1162`); SkydiveOS exact-matches the last-4 the box sends (`mediaService.js:1107`) → 404. Documented in `deploy/windows/README.md:127-130` as an operator rule, not enforced in code |
| 17 | Two cameras at the DZ whose serials end in the same 4 digits | **Collision** | Same `camera_id` → shared staging tree, ledger and S3 prefix; pipeline owner lookup refuses (`match.py:1163-1169`); SkydiveOS exact-match on last-4 cannot even detect it. `check_match --readiness` warns (`check_match.py:129-146`) |
| 18 | `captured_at` unreadable | **Unmatched** | SkydiveOS `'no captured_at on payload'` (`orchestration:367`, `:262`); pipeline keeps the static role hint |

---

## (d) Collision & misassignment risks

Severity: 🔴 high (wrong customer / silent loss), 🟠 medium (footage stranded / needs manual link), 🟡 low.

**R1 🔴 Several cards in one camera on one day → clip silently skipped (VERIFIED code path, INFERRED trigger).**
Staging path is `_camera-staging/<camera_id>/<date>/<FILE>` (`A/ingest/storage.py:72-79`) and `camera_id` is the CAMERA's last-4 (`sdcard.py:264-284`), so two cards used in the same GoPro on the same day are the same tree. GoPro numbering restarts on a formatted card (acknowledged at `discovery.py:290-292`), so card B's `GX010001.MP4` lands on card A's path; `_pull_one` sees `is_complete()` true and returns `skipped=True` (`pull.py:69-71`) — no stage, no event, no upload, no notify. The size-aware guards protect the S3 key (`raw_object_key` fingerprint, `discovery.py:301-304`) and the ledger (`retention.py:66-77`) but the staging skip has **no size check**. The retention sweep then keeps card B's file (size mismatch, `retention.py:178-183`), so it is not deleted — but it is never ingested either, and nothing flags it. This is the exact scenario in the requirement ("multiple cards"). Mitigation today: one card per camera per day, or format cards only when the staging day folder has rolled over.

**R2 🔴 Two cameras per instructor is impossible to pair in SkydiveOS (VERIFIED).** `Staff.goproSerial` is a unique scalar; the "Change" button and `persistSerial` replace it. Footage from the un-paired camera → 404 → permanent, unretried hand-off (`discovery.py:757-766`); file remains on card (ledger never records it) but nobody is told except the box log. QR or card pairing is the only working route for a second camera.

**R3 🔴 Borrowed / shared camera without QR or card pairing (INFERRED from code).** Rung 3 attributes the clip to the camera's OWNER; `matchFootageToBooking` then looks for the OWNER's slot in the window (`orchestration:275-283`). If the owner also jumped on that load (common on a busy load: owner instructs jumper 1, borrower instructs jumper 2 on the owner's spare camera) the borrower's footage is matched to the owner's customer and a job is opened. Both matchers refuse only on 0 or >1 candidates, not on "wrong person". The pipeline-side matcher has the same structure (`match.py:1192-1216`).

**R4 🟠 Last-4 `camera_id` collision between two cameras (VERIFIED code, INFERRED likelihood).** Shared staging tree, ledger and S3 prefix. S3 key and ledger are size-scoped so cross-deletion needs identical sizes (negligible), but R1's staging skip applies across cameras too. `check_match --readiness` warns on staff serials with the same tail (`check_match.py:129-146`); nothing warns in SkydiveOS, whose exact last-4 match would simply return whichever staff holds that 4-digit string.

**R5 🟠 Exact-vs-suffix `goproSerial` divergence (VERIFIED).** Pipeline suffix-matches (`match.py:1155-1162`); SkydiveOS exact-matches (`mediaService.js:1107`). An admin who types the full printed serial into SkydiveOS gets a pipeline registry entry `C350…` that is never discoverable (`check_match.py:150-167` warns) AND a SkydiveOS field the box's `camera_id` never equals. Operator rule only (`deploy/windows/README.md:127-130`).

**R6 🟠 Card identity on the deployed readers is the VOLUME serial, not the card (VERIFIED code; deployment INFERRED from runbook).** North Shore = Windows + USB reader (`deploy/windows/README.md:3-22`): `GetVolumeInformationW` only (`sdcard.py:184-198`). South Shore = Mac (`README.md:7`): `diskutil` `VolumeUUID`/`DiskUUID` (`sdcard.py:201-216`); whether macOS reports a UUID for a GoPro exFAT/FAT32 volume is NOT verified here. The CID path exists only for Linux `mmcblk*` SD-host readers (`sdcard.py:151-172`). Consequence: GoPro "Format SD card" (which staff do routinely) invalidates the pairing silently; the next ingest from that card falls to rung 3 or 404. The banner shows "not paired" (`SdCardIngestIndicator.js:103-110`) which is the only cue.

**R7 🟡 `sd-<label>` fallback shares identity across cards (VERIFIED).** All cards with no `MISC/version.txt` and the same label (or the same Windows drive letter) are one `camera_id`: shared staging/ledger, R1 applies, and `goproSerial`=`sd-E` can never match.

**R8 🟡 QR/card disagreement is only logged (VERIFIED, by design).** `mediaService.js:1052-1059`. A stale/mis-printed QR sheet silently redirects a whole card to the printed staff. Acceptable per the documented rationale ("borrowed card"), but there is no reconciliation report.

**R9 🟡 `camera_role` for a QR-identified clip can come from the camera OWNER's slot (INFERRED).** `discovery.py:812-820` resolves `camera_role` via the camera serial's owner BEFORE the QR identity runs; if the QR staff's load match fails (`qr.py:490-496` → `role=None`) the payload still carries the owner's role. SkydiveOS's staff matcher derives the role from the slot itself (`orchestration:390-394`, `matchedRole`), so the pipeline-sent value is only a hint there — low impact.

What refuses/flags instead (VERIFIED): 0 or >1 candidates, no booking, no `captured_at` → `autoMatch.status unmatched|ambiguous` (`orchestration:425-480`, `:286-300`); pipeline: `NoBookingMatch`/`AmbiguousMatch`/`UnknownCamera` (`match.py:206-246`, `:1140-1173`); unknown staff/card/camera → 404 (`mediaService.js:1089-1123`); a flagged bridge key is terminal until `scripts/unflag_bridge_key.py`.

---

## (e) Tests run (read-only) and results

| Suite | Command | Result |
|---|---|---|
| Pipeline | `uv run --no-sync pytest tests/test_sdcard.py tests/test_sdcard_windows.py tests/test_qr.py tests/test_match.py tests/test_match_ownership.py tests/test_match_media_refs.py tests/test_discovery.py tests/test_cardstatus.py -q -p no:cacheprovider` (with `~/.local/bin/ffbin` on PATH) | **271 passed, 1 failed** — `tests/test_discovery.py::test_build_pull_modes` (`RuntimeError: CAMERA_SCANNER=static … needs a sample MP4`): the known pre-existing failure on this box (no `sample-data/`), unrelated to card/camera association |
| SkydiveOS backend | `node --experimental-vm-modules node_modules/jest/bin/jest.js --silent sdCardPairing cameraService autoEditStaffMatch mediaRawUpload sdCardAutomationPurchaseMatrix ultimumSdCardFourVideos` | **6 suites passed, 103 tests passed, 0 failed** (a first run with plain `npx jest` failed to parse ESM — runner issue, not code) |
| SkydiveOS frontend | `CI=true npx craco test --watchAll=false --silent SdCardsSection SdCardIngestIndicator Bug408DashboardOwnCameraQr` | **3 suites passed, 36 tests passed, 0 failed** (plain `npx jest` fails on JSX without craco's babel config) |

Coverage notes: `sdCardPairing.test.js:85` pins "SEVERAL cards per instructor"; `:259` pins "QR staff_id WINS over the card pairing"; `:286,298` pin the 404 naming the card; `cameraService.test.js:103,111` pin one-camera-per-staff conflict + same-serial re-pair; `test_sdcard_windows.py:167` pins "two Windows cards without serials get different ids". **No test covers R1** (two cards in one camera, same day, same filename) on either side.

---

## (f) Client clarifications needed

1. **Which "multiple"?** (a) several SD cards per instructor (supported, needs pairing or QR), (b) several cameras per instructor (NOT supported by `Staff.goproSerial`; works only through card pairing / QR), (c) several cards rotating through ONE camera on the same day (R1 — silently drops clips). Please confirm which of (a)/(b)/(c) are real operating patterns.
2. **Do they need the physical SD card recognized by its own serial?** On the deployed readers only the *volume* serial is available, which changes on every format. Is "re-pair the card after each format" acceptable, or do they expect the card to stay recognized after an in-camera format? (If the latter, an SD-host reader on Linux — or a different identity such as the QR — is the only route.)
3. **How often do staff format cards, and who does it?** This decides whether card pairing is viable at all or whether QR must be the primary rule.
4. **Do instructors ever lend cameras (or cards) to colleagues?** If yes, QR (or card pairing on the borrower's side) must be mandatory because the camera rung attributes to the owner (R3).
5. **Will one instructor realistically carry two cameras at once** (e.g. handcam + chest)? If yes, `Staff.goproSerial` must become a list on the SkydiveOS side (see §8 below) — the pipeline registry already allows it.
6. **Is the 4-digit camera label on the banner ("Card 4812") acceptable**, given it is the CAMERA's serial tail and two cameras can share it (R4)? Or should the banner lead with the card serial + paired name?

---

## Checklist answers (1–10, condensed)

1. **Data model** — see (a). One camera per staff (scalar unique), many cards per staff (array, multikey unique), pipeline registry N cameras per `instructor_id`, raw row records `autoMatch.{matcher,cameraId,sdCardSerial,staffSource}`.
2. **"Card 4812"** — `describeCardIdentity` title is `Card ${camera_id}` (`SdCardIngestIndicator.js:103-110`); `camera_id` is the last-4 of the CAMERA serial from `MISC/version.txt` (`sdcard.py:264-284`); the CARD serial is the separate `card_serial`/`paired_staff` suffix. Yes, that last-4 is the staging-tree id (`storage.py:63-79`), the ledger key (`retention.py:22-23,80-81`) and the S3 prefix (`discovery.py:296-304`) — two cameras with the same tail share all three (R4); mitigated by date-scoped keys + size-matched ledger, NOT by the staging skip (R1).
3. **Physical card serial** — read on Linux (CID via `mmcblk*` sysfs, else `by-uuid` volume serial), Windows (volume serial only), macOS (`diskutil` VolumeUUID) — `sdcard.py:151-216`. Deployed ingest boxes: North Shore **Windows + USB reader** (`deploy/windows/README.md`, `install-service.ps1`), South Shore Mac. Through a USB reader the CID is hidden; only the volume serial exists and it changes on reformat (acknowledged `S/CLAUDE.md:3537-3540`). The skydiving-os deployment docs (`docs/*deployment*.md`, `CLIENT_HANDOVER_PARACHUTE_MONTREAL.md`) contain no reader/ingest-box hardware statement (grep empty) — hardware facts come from the pipeline runbook only.
4. **QR flow** — card-level per staging day; one staff → whole day regardless of marker position; ≥2 staff → direction inferred (TRAILING if clips only precede; else LEADING with a WARNING) (`qr.py:292-406`). No marker → `staff_id=None`, WARNING, serial fallback (`discovery.py:852-856`). SkydiveOS order `staff_id → sd card → goproSerial → instructor_id` (`mediaService.js:1047-1076`); disagreement **logged, never refused** (`:1052-1059`) — confirmed by `sdCardPairing.test.js:259`.
5. **Multiple cards per camera** — identity: supported; storage: R1. **Multiple cameras per instructor** — `Staff.goproSerial` scalar + unique index → NO (second pairing replaces, UI offers "Change" only); pipeline registry → yes; cards → yes (many).
6. **Edge cases** — see matrix rows 8–18.
7. **Misassignment scenarios** — R1 (silent drop), R2 (404, stranded), R3 (wrong customer via camera owner), R4/R7 (identity collision), R5 (exact-vs-suffix), R6 (format invalidates pairing), R8/R9 (logged only).
8. **Compatibility of N cameras per instructor** — pipeline side: nothing changes (`registry.py` already permits it; `check_match.py:103-190` reads `goproSerial` as a scalar and would need the list). SkydiveOS side would need: `Staff.goproSerial` → array with a multikey unique sparse index (same pattern as `sdCards_serial_unique`), `_resolveRawInstructor` (`mediaService.js:1107`), `cameraService.assertSerialFree/persistSerial` (`:36-41,75-90`), `staffService.formatStaff` (`:79,250`), `autoEditOrchestrationService.js:1180-1186` (re-match reads `staff.goproSerial`), `pairGoProSerial.js`, `GoProPairSection`/`MyCameraSection` UI, and the FootageMatcher regex (Mongo regex over an array works unchanged, INFERRED). Existing one-camera pairings migrate as one-element arrays; no job/media document references `goproSerial` structurally (only S3 metadata and `autoMatch.cameraId` as strings). **Analysis only — no change proposed here.**
9. **Tests** — see (e).
10. **Client clarifications** — see (f).
