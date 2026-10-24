# Audit — SkydiveOS side of R1–R3: in-app editor/export + hand-off to the AI pipeline

Date: 2026-10-24. Read-only. Repo: `/home/msi/Desktop/Development/skydiving/skydiving-os` (SkydiveOS, Node backend + React frontend). The Python pipeline (`edl/validate.py`, `render/`, `api/selfie.py`) is explicitly OUT of scope here; it is touched only at the hand-off seams (`POST /jobs`, `POST /jobs/{id}/upload`, status callback, `POST /jobs/{id}/deliverables/{name}/replace`).

Legend: **VERIFIED** = read in code / observed in a test run. **INFERRED** = reasoned from code without executing that path.

Client requirements (verbatim):
- **R1** "The opening and closing clips of the generated video must play in full. They must never be shortened, and music must not play over these clips."
- **R2** "The freefall footage must not contain internal cuts. It may only be shortened at the beginning so that the plane-exit moment aligns with the music's beat drop."
- **R3** "For all packages, the freefall sequence must play without cuts. The only exception is the Ultimate package, where footage alternates between the outside cameraman's footage and the instructor's handcam."

---

## PART 1 — The SkydiveOS in-app editor / export (`videoProcessor.js` + `editBookends.js`)

### 1a. What the editor does with intro/outro "bookends" on export

**Clip identification rule — by CLIP TYPE, not by position, content, camera or metadata (VERIFIED).**

`backend/src/utils/editBookends.js`:
- L27–28: `INTRO_CLIP_TYPES = ['intro', 'default-intro']`, `OUTRO_CLIP_TYPES = ['outro', 'default-outro']`.
- L36–41 `bookendRole(clip)` → `'intro' | 'outro' | null` purely from `clip.type`. L43–45 `isBookendClip`.
- L51–58 `orderBookends(clips)` — stable partition: all intros first, body, all outros last. So a bookend is a bookend wherever it was saved on the timeline; it is MOVED to the edge, not detected at the edge.
- L66–90 `lockBookendClip(clip, fullDuration)` — forces `trimStart: 0`, `trimEnd: <full length>`, `speed: 1.0`, `speedRamp: []`, `reverse: false`, `_sourceOffset: null`, `audioFrom: null`. Length precedence: probed `fullDuration` → `clip.duration` → `clip.trimEnd`.
- L96–99 `isBookendBoundary` + L107–119 `enforceBookends(clips, fullDurations)` — applies order + lock and replaces any `transition` on a cut into/out of a bookend with `{type:null}` (hard cut), because every editor transition is an `xfade` that overlaps both clips (header comment L7–10).
- L122–129 `bookendDurations` sums the LEADING intros and TRAILING outros in output seconds; L137–145 `bookendMusicWindow` = `[introDuration, compositionDuration − outroDuration)`; `null` when the edit has no bookend (then "every edit without one renders exactly as before"); collapses to zero length (no music at all) when bookends fill the video.

What becomes an `intro`/`outro` clip (VERIFIED):
- A clip the operator uploads through `mediaService.uploadClip(file, position)` (`backend/src/services/mediaService.js:3701`) with `position` `intro`/`outro` → `editSettings.introS3Key`/`outroS3Key` → materialised as `type:'intro'`/`'outro'` by the frontend reducer (`frontend/src/hooks/useEditorReducer.js:184–227`).
- The dropzone's configured default intro/outro (`MediaConfig.introOutro`, `autoApplyIntro`/`autoApplyOutro` default **false** — `backend/src/models/MediaConfig.js:172–173`), injected at export as `type:'default-intro'`/`'default-outro'` (`mediaService.js:4664–4706`).

**Important consequence for R1:** the SkydiveOS "bookend" is a SEPARATE title/branding clip placed around the footage. It is NOT the filmed interview at the head/tail of a GoPro master, and NOT the pipeline's rendered `full_video` (which already contains the filmed interview bookends + branded cards). In the editor the pipeline's deliverable is a single `type:'original'` clip (`useEditorReducer.js:199–210`) and raw GoPro files are `type:'raw'`; `isBookendClip` is false for both, so their head/tail interview seconds are fully trimmable/splittable/slow-mo-able. See defect D1.

**Trimming / speed protection (VERIFIED, enforced on both sides):**
- Frontend: `editorReducer` re-applies `enforceBookends` after ANY action that changes the track (`useEditorReducer.js:131–137`); SPLIT refuses a bookend (L394), region delete/slow-mo skip them (L663, L747), Replace-on-a-bookend becomes an insert (L467); UI hides trim handles, speed/reverse/ramp and the transition diamond on bookends (`VideoEditorDialog.js:2898–2904, 3984, 4008, 4148, 4157`). `frontend/src/utils/editBookends.js` is byte-identical to the backend copy below the header (`diff` shows header-only differences; the frontend `EditBookends` suite pins them equal — 37/37 pass).
- Backend v2 export (`mediaService._processVideoV2Async`): L4712 `videoTrack.splice(0, videoTrack.length, ...enforceBookends(videoTrack))` runs AFTER default-intro/outro injection and BEFORE anything reads the track; L4789–4792 re-locks every bookend to its **ffprobe'd** duration (`lockBookendClip(videoTrack[i], clipProbes[i]?.duration)`), so a wrong `duration` in config or a saved trim cannot shorten it. Speed ramps are not reachable (L4871 only if `clip.speedRamp?.length >= 2`, which `lockBookendClip` empties).
- Backend v1 export: L4315–4318 / L4328–4331 — intro/outro are pushed WHOLE; saved `introTrimStart/End` / `outroTrimStart/End` are "ignored on purpose".
- An API caller saving a trimmed bookend is also neutralised (header L18–22; test "v1 never trims the intro or the outro").

**Audio behaviour over bookends — the filter graph (VERIFIED, `videoProcessor.js`):**

Music is mixed AFTER concat by `mixBackgroundAudio` (L1668) → `buildAudioMixGraph` (L1441). Per background track `i` (input `[i+1:a]`), in order:

```
atrim=<trimStart>:<end>,asetpts=PTS-STARTPTS            # if trimmed            (L1460–1464)
aloop=loop=N:size=2e+09,atrim=0:<comp>                  # if loop               (L1467–1474)
adelay=<ms>|<ms>                                         # if startTime>0        (L1477–1480)
atrim=0:<compositionDuration>                            # always                (L1487)
volume=<t.volume>                                        # if != 1.0             (L1490–1492)
# operator fades ONLY when there is NO musicWindow      (L1495–1503)
aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo   # AUDIO_OUTPUT_FORMAT (L47)
aresample=async=1:first_pts=0                            # AUDIO_TIMELINE_ANCHOR (L64)
<_musicWindowFilters(...)>                               # if musicWindow, AFTER the anchor (L1514)
```

`_musicWindowFilters(t, {start:S, end:E}, comp)` (L1563–1599):
```
volume=0                                                 # window has zero length (bookends fill the video)
volume=0                                                 # track lies entirely over a bookend
afade=t=in:st=<audibleFrom>:d=<min(fadeIn,span)>         # operator fade-in, re-anchored
  | afade=t=in:st=<S>:d=<edge>                           # else, if the track was already playing at S (edge = min(0.5, span/2))
afade=t=out:st=<max(audibleFrom, E-d)>:d=<d>             # operator fade-out, re-anchored to END at E
  | afade=t=out:st=<E-edge>:d=<edge>                     # else, if the track runs past E
volume=0:enable='lt(t,<S>)+gte(t,<E>)'                   # THE GATE: hard mute before S and from E on
```
Then: `[..]amix=inputs=N:duration=longest:normalize=0:dropout_transition=0` (or `acopy` for one source), `loudnorm=I=-14:TP=-1.5:LRA=11[:measured_*…:linear=true]`, `atrim=0:<comp>` → `[finalaudio]` (L1528–1540). Output: `-map 0:v -map [finalaudio] -c:v copy -c:a aac` (L1712–1719).

So: **music over bookends is a HARD MUTE (`volume=0` gate), preceded by a 0.5 s edge fade only where the window would otherwise cut a playing track** (so there is no click). Not ducking, not a crossfade — the gate is the guarantee (comment L1557–1561: "silence before the window opens and from the moment it closes, whatever the fades do"). `BOOKEND_MUSIC_EDGE_FADE_S = 0.5` (`editBookends.js:31`). The window is measured off the RENDERED clips (`mediaService.js:5036–5041`), which is valid because bookend cuts are hard cuts.

**Original camera audio over bookends (VERIFIED):** the video's own audio branch is `[0:a]<AUDIO_OUTPUT_FORMAT>,<ANCHOR>[origaudio]` with no gate (L1447–1450; test "the video's own audio is never gated"). So a bookend keeps its OWN audio at the clip's `volume` (bookends are created with `volume: 1.0`) **unless the operator ticks "Mute original audio"**, which is applied per clip at `processClipWithFilter(... muteAudio: muteOriginalAudio)` (`mediaService.js:4854`) → `command.noAudio()` (`videoProcessor.js:1029`) — for EVERY clip, bookends included (`editBookends.js:12–13` documents this). The per-clip "flow" audio path (slow-mo) cannot apply to a bookend since `speed` is locked to 1 and `audioFrom` to null.

Concat between bookend and body: with `transition.type === null` the clips go through the concat demuxer with stream copy (observed in the real render test log: "Video concatenation completed (stream copy, 3 clips)").

### 1b. Freefall continuity / beat-drop rule in the SkydiveOS editor

**None (VERIFIED).** `grep -i "beat|beat.drop|freefall"` across `videoProcessor.js`, `editBookends.js`, `mediaService.js`, frontend `editBookends.js` returns one hit, a doc comment at `mediaService.js:1372` listing the pipeline's deliverable names ("full / highlights / freefall"). The editor has no notion of exit, deploy, scene, or beat drop. The editor's own concept of "music keeps flowing" through slow-mo (`clipAudioFlow.js`, `processClipWithFilter` L909–925) is about keeping the AI render's embedded music continuous through a speed change, not about aligning anything to a drop.

**Is the editor used for customer deliverables? Yes, two ways (VERIFIED):**
1. **Importing a finished pipeline job into Media** (`mediaService.importAiJob` L1382; driven automatically by `autoEditImportService.js` for automation jobs and `manualAiImportService.js` for the AI dialog). Each deliverable (`full`/`highlights`/`freefall`/`external_freefall`/`chute_libre_selfie`) becomes an editable `Media` row with `aiGenerated.role:'output'` (L1680–1710). Staff then open it in the editor, where the deliverable is one `original` clip and the job's raw masters (`aiGenerated.role:'source'`) can be dropped in as `raw` clips (MED-202609-0009, `CLAUDE.md:2920–2932`).
2. **Pushing the re-cut back behind the customer's link** — `mediaService.replaceCustomerDelivery` (L4593) → `republishEditToAiDelivery` (L4524) → `aiJobService.replaceDeliverable` → pipeline `POST /jobs/{id}/deliverables/{name}/replace` (`skydiveos-autoedit/api/app.py:1922–1990`). The pipeline checks delivered-state and the paywall only; it does NOT re-validate the bytes against R1–R3 (it `os.replace`s the file; VERIFIED by reading the route). Explicit operator action, never automatic on export (L4502–4506).
   Also `approveAutoEdit` (L5670) pulls the pipeline preview into `media.s3Key` for the SkydiveOS-side delivery path, and `tweakAutoEdit` (L5745) forwards an arbitrary operator `edl` to the pipeline's `POST /jobs/{id}/tweak` (route `mediaRoutes.js:277`, roles admin/owner/manifest/instructor) — the pipeline's replay does not re-run its validator (pipeline CLAUDE.md: "`replay_*` … doesn't re-run the validator").

**Can a staff edit/re-export violate R1/R2? YES — enforced only for the editor's own `intro`/`outro` clip types; the footage itself is unprotected (VERIFIED):**
- R1 (filmed opening/closing clips): the pipeline's `full_video` arrives as one `original` clip. A staff trim of its head (`UPDATE_CLIP {trimStart}`, `SPLIT_CLIP`, region delete) shortens the filmed interview; a region slow-mo can slow it; and a background music track added via the Audio Mixer plays over it, because the music window only opens for `intro`/`outro` TYPES — an edit with no such clip gets `musicWindow = null` → "every graph is byte-identical to before" (`editBookends.js:132–135`, `videoProcessor.js:1495–1503`), i.e. the track plays from its `startTime` over the whole composition, including the filmed interview and the branded cards baked into the AI render.
- R2/R3: a staff split/delete/slow-mo/reorder inside the freefall section of a `freefall`/`external_freefall`/`chute_libre_selfie` deliverable is not refused anywhere, and the exit-to-beat-drop alignment baked into the AI render is destroyed the moment the head is trimmed or music is replaced (editor music starts at `startTime` the operator sets, with no knowledge of the exit).
- The only thing the editor enforces is that *its* intro/outro clips play whole and music-free. That is a different object from what R1 describes if "opening and closing clips" means the filmed interview/landing shots (see Client questions).
- `tweakAutoEdit`: a hand-written EDL reaches the pipeline unvalidated by SkydiveOS (zod validates only `edl` presence at the controller — INFERRED from `mediaController.js:667–669`).

So for R1 the SkydiveOS editor rule is **enforced** for operator-uploaded/default intro/outro clips (not merely default behaviour), but it is **not enforced** for the filmed bookends inside the AI deliverable; for R2/R3 it is **absent**.

### 1c. Test runs (read-only, verbatim)

`slowMotionAudioStretch.test.js` no longer exists: it was deleted and replaced by `slowMotionAudioFlow.test.js` in commit `5c826a3` (2026-10-22, "Added media task") — the "music keeps flowing / no atempo" client decision superseded the tempo-stretch cut (the test "never stretches: no atempo anywhere in the processor, and the stretch chain is gone" pins this).

The plain `npx jest` invocation fails to parse ESM (`SyntaxError: Cannot use import statement outside a module`); the project's runner is `node --experimental-vm-modules node_modules/jest/bin/jest.js` (`backend/package.json:20`).

```
$ cd backend && node --experimental-vm-modules node_modules/jest/bin/jest.js --silent editBookendsExport slowMotionAudioStretch slowMotionAudioFlow
PASS src/__tests__/slowMotionAudioFlow.test.js
PASS src/__tests__/editBookendsExport.test.js (6.135 s)
Test Suites: 2 passed, 2 total
Tests:       34 passed, 34 total
```
(The `editBookendsExport` suite includes a REAL ffmpeg v2 render: "runs intro 3s + main 6s + outro 2s = 11s", "has NO music over the intro or the outro, and music in between", "keeps the intro's and outro's OWN audio" — observed log line `[export] music confined between intro and outro {"start":3,"end":9.02322}`.)

```
$ cd frontend && npm test -- --silent --watchAll=false EditBookends
PASS src/__tests__/EditBookends.test.js
Test Suites: 1 passed, 1 total
Tests:       37 passed, 37 total
```

---

## PART 2 — Package identification and hand-off to the AI pipeline

### 2d. Trace: add-on docs → package/entitlement → `POST /jobs` → `POST /jobs/{id}/upload`

**Structural inputs (VERIFIED):** `BookingPackage` fields read by the mapping are `mediaType` (`video|photos|video-photos|null`), `videoAngle` (`inside|outside|both|any|null`), `scenes[].cameraSource` (`selfie|instructor` → inside, `external|ground` → outside), `isTwoCameraVideo`, `specOf` (spec twin → its $0 parent), `category`. Names are never read (BUG 156 — `autoEditPackage.js:10–16`, `mediaDerivation.js:5–18`).

**Mapping table — `backend/src/utils/autoEditPackage.js:71–122` `resolveAutoEditPackage(docs)` (the "booking-structural" mapping, serial/automation path):**

| Add-on docs on the jumper/booking | Pipeline `package` | Where |
|---|---|---|
| any doc with `isTwoCameraVideo === true` | `ultimum` | L75–77 (highest precedence, "ULTIMUM IS EXACT") |
| derived `purchasedMedia === 'photos'` | `photo_only` | L82–84 |
| `video`/`video-photos` + `videoType 'inside'` | `selfie` | L87–89 |
| `video`/`video-photos` + `videoType 'outside'` | `external` | L90–92 |
| `video`/`video-photos` + `videoType 'both'` and NO `isTwoCameraVideo` | **refused** (`ambiguous:true`, footage left unlinked + flagged) | L93–101 |
| `video`/`video-photos` + angle `any`/null | `video_only` | L104 |
| media doc with `mediaType:null` (legacy) | refused (ambiguous) | L110–119 |
| no media doc | `package:null` → no job | L121 |

`purchasedMedia`/`videoType` derivation: `mediaDerivation.js:30–79` `deriveMediaFields` (explicit `videoAngle` overrides scene inference per doc, L45–48).

**Second mapping — the QR/SD-card "jumper-union" path, `autoEditOrchestrationService.resolveJumperPackage` (L543–561)**, reads the jumper's derived UNION (`jumper.mediaPackage`, `jumper.videoType`), mirroring the pipeline's `ingest/match.py`:

| `mediaPackage` / `videoType` | package | entitlement |
|---|---|---|
| `none` | `selfie` (role instructor) / `external` (role external) | **`preview_only`** (L546–553) |
| `photos` | `photo_only` | `edited_download` |
| `video-photos` + `both` | `ultimum` | `edited_download` (L557) |
| `*` + `inside` | `selfie` | `edited_download` |
| `*` + `outside` | `external` | `edited_download` |
| `video` + (`both`/null/any) | `video_only` | `edited_download` (L560) |

**Entitlement + refs — `resolveJumperMediaPlan` (L652–771), THE resolver both ingest paths use:**
1. Nothing purchased (QR path only, `mediaPackage === 'none'`) → `resolveJumperPackage` → role-default package, `preview_only`, one spec ref (L665–677).
2. Jumper has `mediaAddOnRefs` → `planMediaRefs(jumperDocs)` (`mediaRefs.js:348–467`): one camera role → `mode:'single'`, package from the caller's mapping (`resolveJumperPackage` on the QR path, `resolveAutoEditPackage` on the serial path), entitlement from the docs (`entitlementForGroup` L287–289: any `specOf` doc → `preview_only`, fail-closed); two roles → `mode:'multi'`, one ref per camera, each `resolveAutoEditPackage(group)` (L442–450), primary = paid ref else instructor (L297–303).
3. No refs (legacy jumper) → booking's docs (`resolvePackageForBooking` L568–575 → `_bookingPackageDocs` L586–596); entitlement `preview_only` if ANY booking doc is a spec twin (L762–766).

`cameraRoleForPackage` (`mediaRefs.js:145–187`): spec twin resolves its camera from the PARENT doc (`specOf`), `videoAngle both` → null (two cameras, no single role), "Let staff choose" (`any`) products take the jumper's manifested `videoType`.

**`POST /jobs` payload — `autoEditOrchestrationService.js:1016–1042` (VERIFIED):**
```
customer_name, customer_email            ← Booking.customer (or pendingBookerInfo), L812–825
package                                  ← pkg.package
entitlement                              ← pkg.entitlement ('edited_download' | 'preview_only')
media_refs: [{role, package, entitlement}]   ONLY when ≥2 refs (wireMediaRefs L155–163)
booking_id                               ← booking.bookingNumber (else _id)
jump_date                                ← NAIVE DZ-local "YYYY-MM-DDTHH:MM:00" of the load takeoff (L783–799)
instructor_name                          ← Staff firstName lastName (L800–808)
```
NOT sent: `music`, `template`, `instructor_id`, `load_id`, `jumper_index`, `customer_phone`, any beat-drop or edit preference.

**`POST /jobs/{id}/upload` — `_attachCamera` (L1086–1115):** `autoEditService.uploadFromS3(jobId, s3Key, {cameraRole})` → multipart `s3_key` + `camera_role` (`autoEditService.js:236–237`). `camera_role` is sent ONLY for `ultimum` or a multi-ref job (L1109–1110); refused + flagged (`outcome:'ambiguous'`) when missing on those (L1098–1107). The role comes from the load slot the matched staff fills (`jumperFillsCameraRole` L114–125: `assignedCameraman` → `external`, `instructor`/`secondaryInstructor` → `instructor`), consistent with the pipeline's "role is per jump, never per camera" rule.

**Is `ultimum` structural or by name?**
- Automation/serial path: **structural** — `isTwoCameraVideo` only (`autoEditPackage.js:75`); `videoAngle:'both'` without the flag is refused, never inferred (L93–101). VERIFIED, and pinned by `autoEditPackageMapping.test.js`.
- QR/SD path (`resolveJumperPackage`): by the jumper's derived union `video-photos + both` (L557) — structural fields, but a different rule (an inside+outside PAIR of single-camera products also unions to `both`). Documented as the known "AUDIT_347 `'both'` divergence" and deliberately left (L745–753). Note `video` (no photos) + `both` on this path → `video_only` (L560), not `ultimum`. INFERRED risk, see D5.
- Manual AI dialog fallback: `frontend/src/utils/aiJob.js:281–293` `aiPackageForBooking` uses `isUltimum` = "the ultimum NAME heuristic + env flag" (comment L285–286) — but only when no server-side `plan` exists; the dialog normally takes `plan.package` from `manualAutoEditService` (`buildCreateJobBody` L530–532), which is structural via `planManualMediaRefs`. INFERRED.

**What is sent when a customer bought nothing:** QR path → role-default package (`selfie` for the instructor camera, `external` for a cameraman) + `entitlement:'preview_only'` (L546–553, L665–677). Serial/booking path → `package:null` → no job at all (`autoEditPackage.js:121`; `createOrAttach` L858 `outcome:'skipped'`). Spec twin (`specOf`) products → the twin's parent package + `preview_only` per ref.

### 2e. Music / beat-drop / template information passed to the pipeline

- **Automation (`autoEditOrchestrationService`) sends NO music, template or beat-drop fields** — `grep -i "music|beat|template"` over `autoEditOrchestrationService.js`, `autoEditService.js`, `aiJobService.js`, `manualAutoEditService.js` returns nothing (VERIFIED). The pipeline therefore applies its own default: `_ensure_default_music` picks a random `templates/music` track at first processing (pipeline CLAUDE.md), and its beat-drop sync uses that track's sidecar/detected drop.
- **Manual AI dialog** (staff-driven): `buildCreateJobBody` sends `music` only as an "optional global fallback" when `context.music` is truthy (`aiJob.js:541–542`); the only caller, `UploadForJumperDialog.js:188`, hard-codes `music: ''`, so it is never sent (INFERRED). Per-deliverable tracks ARE uploadable from the dialog's music step through the backend proxy (`mediaRoutes.js:151–153` → pipeline `POST /jobs/{id}/music`; `AiAutoEditDialog.js:192–227`, 4 slots for Ultimate L351). No beat-drop metadata accompanies an uploaded track — the pipeline detects the drop itself (or reads a `<stem>.json` sidecar in `templates/music`, which an uploaded per-job track cannot carry).
- No SkydiveOS `Booking`/`Load`/`BookingPackage` field names a music track (grep on those models: no hits). INFERRED: the pipeline's `booking.json` `music` name is never populated from SkydiveOS.

### 2f. Status callback consumption and any SkydiveOS-side re-cut / re-encode

`autoEditOrchestrationService.handleStatusCallback` (L1991–2155, VERIFIED): dedupes per `(jobId, rawStatus)` via `WebhookEvent`; folds the pipeline status onto the app enum (`normalizeAutoEditStatus`); stores `galleryUrl`, `entitlement` (only the two contract values), per-deliverable `deliverable_entitlements` + `media_refs` (`applyPipelineProductState`, `autoEditProductState.js:146`), `media_state` (label only), `error`; on `delivered` stores `deliveryLinks` and `deliveredAt`, then fires SMS + email (`_notifyDelivered`) and the included-raw release. **It records links and state only; it never downloads, re-cuts, re-encodes or re-muxes a deliverable.**

Paths that DO touch deliverable bytes on the SkydiveOS side:
- `mediaService.importAiJob` (L1382): downloads each pipeline output (`aiJobService.downloadOutput`) and uploads the bytes UNCHANGED to SkydiveOS S3 as `Media` rows (L1680–1710) — a copy, not a transcode (VERIFIED). `aiSourceFootageService.ensureEditProxy` transcodes a BROWSER PROXY of a raw master for the editor (`aiSourceFootageService.js:31, 244, 357`); proxies never leave as the product (`CLAUDE.md:3221`).
- `mediaService._processVideoV2Async` / `_processVideoAsync` — the manual editor export: re-encodes (libx264 crf 23, 30 fps), can trim, split, slow, add transitions, overlay text, mix new music, and inject the configured default intro/outro **in front of / behind the AI render** (L4664–4706; the AI import does not set `useDefaultIntro/Outro`, so `MediaConfig.autoApplyIntro` — default false — governs). This only runs when staff export.
- `replaceCustomerDelivery` → pipeline `/deliverables/{name}/replace` — puts that re-encoded export behind the customer's `/j/{code}` link (explicit operator action; pipeline checks only `delivered` + paid; no R1–R3 re-validation).
- `approveAutoEdit` (L5670) repoints `media.s3Key` at the pipeline preview bytes (unchanged copy).
- `tweakAutoEdit` (L5745) → pipeline `/tweak` with an operator-supplied EDL (replay, not re-validated by the pipeline).

### 2g. Test runs (read-only, verbatim)

```
$ cd backend && node --experimental-vm-modules node_modules/jest/bin/jest.js --silent \
    autoEditOrchestration autoEditPackageMapping autoEditStaffMatch autoEditStatusReceiver \
    autoEditUnlockWire autoEditImport aiJobProxy aiJobSources ultimumSdCardFourVideos
PASS src/__tests__/autoEditPackageMapping.test.js
PASS src/__tests__/autoEditImport.test.js
PASS src/__tests__/autoEditUnlockWire.test.js
PASS src/__tests__/aiJobSources.test.js
PASS src/__tests__/autoEditStaffMatch.test.js
PASS src/__tests__/aiJobProxy.test.js
PASS src/__tests__/ultimumSdCardFourVideos.test.js
PASS src/__tests__/autoEditStatusReceiver.test.js
PASS src/__tests__/autoEditOrchestration.test.js
Test Suites: 9 passed, 9 total
Tests:       224 passed, 224 total
```
(`ls backend/src/__tests__ | grep -i -E "autoedit|aijob|ultim|package|orchestr"` also lists package-PRICING suites — `bug214StaffPackageList`, `bug403PackageChangeService`, `bug429PackageListDayPricing`, `bug443KioskZeroPricePackages`, `dateFirstPackageAvailability`, `groupMemberPackageIdentity`, `kioskPackageVisibility`, `packageEligibilityWindow`, `packageUpdateApplicableJumpTypes` — unrelated to the AI hand-off; not run.)

---

## Confirmed defects / gaps vs R1–R3 (SkydiveOS side)

| # | Finding | Req | Severity | Status |
|---|---|---|---|---|
| D1 | **The editor's bookend rule protects only clip types `intro`/`outro`/`default-*` — never the filmed opening/closing shots inside the AI deliverable.** The pipeline's `full_video` is one `original` clip; its head/tail interview can be trimmed, split, slowed, and an Audio Mixer track plays over it (no `intro`/`outro` clip → `musicWindow=null` → music over the whole composition, including the branded cards baked into the AI render). `editBookends.js:27–41`, `videoProcessor.js:1495–1514`, `useEditorReducer.js:199–210`. | R1 | **High** if staff re-edit and `replaceCustomerDelivery` is used (the re-cut is served to the customer with no re-validation, `mediaService.js:4524–4580`, `api/app.py:1922+`). Medium otherwise (automation path never runs the editor). | VERIFIED |
| D2 | **No freefall-continuity or beat-drop rule exists in the SkydiveOS editor.** Split/delete/slow-mo/reorder/head-trim of a `freefall`/`external_freefall`/`chute_libre_selfie` deliverable is unrestricted; replacing the music resets the exit↔drop alignment. | R2, R3 | **High** when the re-cut is pushed to the customer (same path as D1); otherwise Medium. | VERIFIED (grep: no `beat`/`freefall` logic) |
| D3 | **`tweakAutoEdit` forwards an arbitrary operator EDL to the pipeline's `/tweak`** (`mediaService.js:5745–5760`, route open to admin/owner/manifest/instructor). SkydiveOS validates nothing about it, and the pipeline's replay path does not re-run `validate_and_repair`. | R1–R3 | Medium (no UI found that builds an EDL — INFERRED to be API-only today) | VERIFIED route / INFERRED usage |
| D4 | **Default intro/outro injection can wrap the AI render in a SECOND set of cards** on a manual export: an imported AI output row has `useDefaultIntro/Outro: null`, so `MediaConfig.autoApplyIntro/Outro` (default false) decides (`mediaService.js:4664–4706`, `MediaConfig.js:172–173`). With the config on, the exported video has two intros/outros; only the outer (SkydiveOS) pair is music-free. | R1 | Low–Medium (config off by default) | VERIFIED |
| D5 | **Two package mappings disagree on `videoType:'both'`** (the documented "AUDIT_347 divergence", `autoEditOrchestrationService.js:745–753`): serial/booking path refuses unless `isTwoCameraVideo`; QR path maps `video-photos + both` → `ultimum` by union, and `video + both` → `video_only`. Because R3 makes `ultimum` the ONE package allowed to cut inside freefall, mis-classifying a single-camera pair as `ultimum` (QR path) would legitimise alternating cuts, and mis-classifying a true Ultimate as `video_only` (QR path, if the catalogue product is `video`-only) would drop the second camera. Legacy jumpers with no `mediaAddOnRefs` on a two-role booking are "deliberately kept at ONE ref". | R3 | Medium (depends on catalogue data; `check_match.py --day` would show it) | INFERRED |
| D6 | **No music/beat-drop/template choice is passed from SkydiveOS to the pipeline on the automation path**; the pipeline picks a random library track. The client's R2 ("aligns with the music's beat drop") is therefore satisfied only by the pipeline's own detection on whatever track it chose; SkydiveOS offers the operator no way to pin the drop for an uploaded per-job track (no sidecar on `POST /jobs/{id}/music`). | R2 | Low (functional, but unobservable from SkydiveOS) | VERIFIED |
| D7 | `ingest`-side `camera_role` is sent only for `ultimum`/multi-ref jobs (`_attachCamera` L1109–1110). For a plain `external` or `selfie` job the pipeline infers the role from the package. Not an R1–R3 defect; noted because R3's "alternate instructor/outside" depends on the role tag being right on Ultimate uploads, and that IS sent and refused-if-missing. | R3 | Info | VERIFIED |

What is working as the client asked, on this side (VERIFIED): operator-added intro/outro clips always play whole, at 1x, with hard cuts, with their own audio, and with background music hard-gated to silence over them — enforced in the reducer, in BOTH export pipelines, re-locked to the ffprobe'd length, and pinned by a real-ffmpeg test. `ultimum` is derived from `isTwoCameraVideo` on the automation path (structural, never by name), `camera_role` is mandatory and refused-if-missing for Ultimate/multi-ref uploads, and the callback consumer never re-encodes anything.

---

## Client questions to settle before sign-off

1. **What are "the opening and closing clips"?** (a) The filmed interview/boarding clip at the head of the card and the landing/outro interview at the tail (what the pipeline's Bug 441 bookend rule protects — first `intro_interview` file / last `outro_interview` file), (b) the branded title cards, or (c) literally the first and last clip on the timeline regardless of content? The SkydiveOS editor implements (c) restricted to clips TYPED intro/outro; the pipeline implements (a) for `full_video` only. If (a) is meant for every deliverable (including `highlights`, which keeps 8 s/2 s beats), both sides need a change; if it is meant to survive a staff re-edit, the editor needs a bookend lock on the `original` clip's interview range (which SkydiveOS does not currently know — it would need `file_offsets`/bookend ranges from the pipeline).
2. **"Music must not play over these clips" — hard mute or a fade?** The editor hard-mutes with a 0.5 s edge fade only where it would otherwise cut a playing track (`BOOKEND_MUSIC_EDGE_FADE_S`); the pipeline fades the bed out over its last seconds before the outro card. Confirm the client accepts a short fade at the boundary (a dead-stop cut is audible as a click) and whether the clip's own audio (interview speech, ambient) must be audible — the editor's "Mute original audio" currently silences bookends too.
3. **Should a staff re-edit be allowed to break R1–R3 at all?** Today `replaceCustomerDelivery` ships whatever the operator exported. Options: (i) refuse the replace when the export trimmed the deliverable's head/tail or split inside the freefall range (needs the pipeline to expose those ranges), (ii) keep it as an explicit operator override and log it, (iii) hide split/trim on `freefall*` deliverables except head trim.
4. **"Shortened at the beginning" for R2 — bounded?** The pipeline caps the pre-exit lead at `FREEFALL_MAX_LEAD_S` (15 s) and enters a later-dropping track mid-way. Confirm the client accepts "the music starts mid-track" versus "more plane before the exit".
5. **Ultimate alternation cadence** (R3): the pipeline alternates every 5 s (`_MULTICAM_FF_SHOT_S`). Confirm the cadence and whether the external-only and selfie-only Ultimate cuts (`external_freefall`, `chute_libre_selfie`) must also be continuous (they are, per the pipeline's `CONTINUOUS_FREEFALL_DELIVERABLES`) — the SkydiveOS side only labels and imports them.
6. **Music choice**: should SkydiveOS send a per-booking/per-dropzone track (and a pinned drop) rather than letting the pipeline pick at random? Nothing in `Booking`/`BookingPackage` names a track today.

## Files cited
- `backend/src/utils/editBookends.js` (whole file, L1–145)
- `backend/src/utils/videoProcessor.js` L10, L36–66, L909–925, L1029, L1045–1066, L1441–1540, L1563–1599, L1668–1740
- `backend/src/services/mediaService.js` L1372–1382, L1680–1710, L3701, L4255–4331 (v1), L4524–4623, L4650–4712, L4786–4792, L4836–4890, L5030–5055, L5670–5760
- `backend/src/models/MediaConfig.js` L172–173
- `frontend/src/hooks/useEditorReducer.js` L131–137, L184–227, L394, L467, L663, L747; `frontend/src/utils/editBookends.js`; `frontend/src/components/media/VideoEditorDialog.js` L2898–2904
- `backend/src/utils/autoEditPackage.js` L33–122; `backend/src/utils/mediaDerivation.js` L30–79; `backend/src/utils/mediaRefs.js` L94–187, L287–303, L348–467
- `backend/src/services/autoEditOrchestrationService.js` L114–163, L543–561, L568–596, L652–771, L783–825, L1016–1042, L1086–1115, L1991–2155
- `backend/src/services/autoEditService.js` L207–245; `backend/src/services/aiJobService.js` L1–50, L196–218
- `backend/src/services/autoEditImportService.js` L1–80; `backend/src/services/manualAiImportService.js` L1–60; `backend/src/services/manualAutoEditService.js` L120–200
- `frontend/src/utils/aiJob.js` L281–293, L529–550; `frontend/src/components/media/UploadForJumperDialog.js` L188
- `skydiveos-autoedit/api/app.py` L1922–1990 (hand-off seam only)
- SkydiveOS `CLAUDE.md` L2100–2136, L2505–2535, L2630–2700, L2920–2932, L3275
