# Audit — AI video editing, SD-card ingestion, video sharing, bilingual emails

**Date:** 2026-10-24 · **Type:** read-only audit (no application code, configuration, data or
cards were modified; no emails sent; no cards formatted or deleted from) · **Author:** Claude
Code session run by Saksham (saksham@metacognixion.com)

**Repositories audited (both present on the audit machine):**

| Repo | Path | HEAD at audit | Working tree |
|---|---|---|---|
| AI pipeline — `skydiveos-autoedit` ("A") | `skydiving-autoedit/skydiveos-autoedit` | `f4a5b5b` (merge of PR #3, 2026-10-23) | **uncommitted** changes in `edl/validate.py`, `CLAUDE.md`, three tests (the deploy-sentinel and door-arrival rules dated 2026-10-24) |
| Ultimate DZM / SkydiveOS ("S") | `skydiving/skydiving-os` | `5134f08` "Added Sharing functionality" (2026-10-23) | `CLAUDE.md` only |

Supporting evidence, one file per area, lives beside this report:

- `appendix-A1-ai-pipeline-editing-rules.md` — requirements 1–3 inside the AI pipeline
- `appendix-A2-skydiveos-editor-and-package-handoff.md` — requirements 1–3 on the SkydiveOS side (in-app editor, package mapping, callback)
- `appendix-B-sdcard-cleanup-and-status.md` — requirement 4
- `appendix-C-instructor-camera-card-association.md` — requirement 5
- `appendix-D-video-sharing.md` — requirement 6
- `appendix-E-bilingual-emails.md` — requirement 7 (full per-template inventory)

**Conventions.** "VERIFIED" = traced in code and confirmed by an executed test, script or
on-disk artefact; "INFERRED" = traced in code only; "BLOCKED" = needs access this machine does
not have. File references are `repo-prefix/path:line` (A = pipeline, S = SkydiveOS).

---

## A. Executive summary

| Metric | Count |
|---|---|
| Requirements audited | 8 (R1–R7 functional + R8 cross-requirement regression) |
| PASS (implemented and verified end to end) | 0 as a whole requirement; the *core mechanisms* of R1, R2, R3 and R6 are verified |
| PARTIAL | 7 (R1–R7) |
| FAIL (confirmed defect / missing mandatory behaviour) | 0 whole requirements; **11 confirmed defects** inside PARTIAL requirements (see §C/§H) |
| BLOCKED items | 6 (deployment state of both repos, the 18 Oct card incident data, production `.env` of each stack, exit-detector accuracy against human-observed exits, live Atlas data, physical reader hardware) |
| Belonging to another project/service | R1–R3 are **split**: the rules live in the AI pipeline, but SkydiveOS's in-app editor can override them; R4–R5 are split between the ingest box (pipeline repo) and SkydiveOS (registry + banner); R7 is mostly SkydiveOS but the pipeline sends its own customer email |
| Outstanding client decisions | 23 (§G) |

**What is genuinely in place (verified):**

- The AI pipeline now enforces, in code rather than in the Claude prompt, that the full video's
  filmed intro and outro play whole at 1× with their own audio and the music hard-muted; that
  the freefall plays as one continuous run (every second once, in order) in the full video and
  the three freefall-only cuts; that a freefall-only cut is shortened only at its head so the
  exit lands on the track's detected beat drop; and that the Ultimate combo alternates the two
  cameras on an exit-anchored timeline without time jumps. Confirmed on real fixtures, on two
  real jobs on disk, and by a real-FFmpeg render test that measures the drop on the exit.
- SD-card cleanup deletes per file and only after a ledger record proves the S3 upload (size-matched);
  no code path clears a partially copied or partially uploaded card. Whole-card format is never used.
- Several SD cards per instructor are supported (card pairing + QR markers); no link, on either
  side, can serve unpaid full-resolution media.
- Every reachable SkydiveOS customer email template is one message with complete French and
  English sections and a combined subject.

**Critical risks (verified unless marked):**

1. **P0 — footage silently never ingested (A).** Two cards cycled through ONE camera on one
   day, the second freshly formatted (GoPro restarts at `GX010001.MP4`), produce the same
   staging path `_camera-staging/<camera_id>/<date>/<file>`; `A/ingest/pull.py:69-71` skips
   any path that `is_complete()` (existence only, no size check, `A/ingest/storage.py:87-89`).
   The second card's clips are never staged, uploaded, matched or delivered, and the banner
   turns green. If the second file also happens to be byte-identical in size, the retention
   ledger (name + size) would even authorise its deletion. Exactly the "instructor uses several
   cards" scenario of R5. No test covers it.
2. **P1 — a process restart strands queued hand-offs (A).** The upload/notify queue is in
   memory and staged clips are never re-emitted (`A/ingest/discovery.py:745-746`, in the code's
   own words). After a restart mid-upload the clips sit in staging, no job is created, the card
   never clears; recovery is manual. Footage is not lost (it stays on the card), but it is not
   delivered either.
3. **P1 — a staff re-edit can ship a video that violates R1–R3 (S → A).** SkydiveOS's editor
   protects only *typed* intro/outro clips; an imported AI `full_video` is ONE `original` clip
   whose filmed interview can be trimmed, slowed, split, and covered with music, and the
   freefall split or re-cut; "Replace customer's video" pushes that export behind the customer's
   link via `POST /jobs/{id}/deliverables/{name}/replace`, which re-validates nothing.
4. **P1 — the pipeline's own "your video is ready" email is English-only** (`A/api/delivery.py:311-371`)
   and is sent whenever a stack's `CUSTOMER_EMAIL_SENDER` is not `skydiveos` (code default:
   `pipeline`). Which stacks have the flag set could not be verified from here.
5. **P1 — bilingual ordering is preference-led, not French-first.** An English-preferring
   customer gets `Your skydive video is ready / Votre vidéo…` with the English section first
   (`S/backend/src/utils/bilingualEmail.js:58-61`, pinned by test). The requirement says French first.
6. **P1 — media SMS, invoice PDF and the no-preference certificate are not bilingual** (S).
7. **P1 — no revocation of any share link, on either side**; SkydiveOS share links expose the
   presigned full-resolution master with a Download button (by design for paid media).
8. **P1 — SD-card clearing is slow by construction**: one clip at a time, each paying ~15 s of
   fixed overhead plus the S3 upload, and the delete sweep only runs at the START of the next
   pull. The banner cannot distinguish "uploading" from "SkydiveOS rejected a clip" or "delete refused".
9. **Deployment is unverifiable from this machine.** The R1–R3 rules were committed on
   2026-10-22/23 (A `677c27d`, `67408a8`, `d189bf5`, `c34ae33`; S `ac83149`, `dea314c`,
   `5134f08`); two further pipeline fixes (deploy-offset sentinel, door arrival) are
   **uncommitted** in the working tree. Production pulls `main` and rebuilds Docker images
   (`A/deploy/RELEASE_2026-09-18_card_safe_to_remove.md:135-137`), so anything the client
   observed before those dates — including the 18 Oct card incident — was on pre-rule builds.

---

## B. Requirement-by-requirement results

| ID | Requirement | Responsible project/service | Status | Evidence | Gap or risk | Client decision needed |
|---|---|---|---|---|---|---|
| R1 | Intro/outro clips play in full, no music over them | **A** (`edl/validate.py` bookends, `api/selfie.py` renderer); **S** in-app editor can override via replace/tweak | **PARTIAL** | Bookend = first file of `intro_interview` / last file of `outro_interview`, forced whole at 1× last in validation; music gated `volume=0` with original audio kept; real-job reports show it (appendix A1). Tests: `test_bookends.py` (14), `test_render.py` real-render card silence. S editor: typed intro/outro locked, hard-mute gate, real-ffmpeg test (34 passed). | Only `full_video`; `highlights` keeps a ≥3 s intro beat, freefall cuts have no interview at all. Identification is by scene label + file order (an accidental first clip becomes "the intro"). Music gate is a hard step, not a fade. No real-render test of silence over *filmed* bookends. S editor does not recognise the filmed shots inside an AI render and "Mute original audio" silences bookends. Replace/tweak paths unvalidated. | Q1–Q4 |
| R2 | Freefall never cut mid-way; shortened only at the head so the exit hits the beat drop | **A** (`_require_continuous_freefall`, `render/beatdrop.py`, `_freefall_lead`, `_freefall_music_offset`) | **PARTIAL** (core verified) | Continuous run `[exit−lead, deploy+3]` in `full_video` + 3 freefall cuts; lead = min(drop, footage, 15 s cap); render offset re-derived from the EDL; real render test measures the drop on the exit (±0.15 s); diagnostic on real job `08142ed6…`: drop 9.98 s, lead 9.98 s, exit at output 9.98 s. Every shipped track has a detectable drop (test). | `highlights` excluded by design; `full_video` keeps lead 0 and never syncs to the drop; tail cut at deploy+3 s; slow-mo beats inside the run; `exclude.json` can split the full video's freefall; replay never re-validates; the `deploy_offset=0.0` sentinel fix is uncommitted; whether the *detected* exit is the *real* exit cannot be verified without a human-observed exit; S re-edit can override. | Q5–Q9 |
| R3 | Continuous freefall for all packages; only Ultimate alternates cameras | **A** (`run_ultimum_pipeline`, multicam branch of the validator); **S** package mapping (`autoEditPackage.js`, `autoEditOrchestrationService.resolveJumperPackage`) | **PARTIAL** | Non-Ultimate jobs have no camera tag → alternation unreachable; mixed-ref jobs render per camera, never merged. Ultimate: continuous on the instructor's exit-anchored window, 5 s per angle, each camera only inside its own aerial window, fallback to the camera with footage; verified on fixture `ultimum_0823a77a` and real job `a34afaf2…` (18 interleaved clips, anchored times line up). S: `ultimum` derived structurally (`isTwoCameraVideo`) on the automation path (224 tests pass). | 5 s hold / 3 s switch spacing / 1.5 s min shot are invented defaults; cross-camera sync relies solely on each camera's exit detection (a >4 s jump is logged, not repaired); the QR/SD path derives `ultimum` from the jumper's *union* (`video-photos` + `both`), so a paired inside+outside purchase could be rendered as Ultimate (INFERRED, S `AUDIT_347` divergence); no music/template/drop info crosses the hand-off. | Q10–Q12 |
| R4 | SD-card cleanup: performance, safety, accurate status | **A** ingest box (`ingest/pull.py`, `retention.py`, `sdcard.py`, `cardstatus.py`, `discovery.py`); **S** receiver + banner (`mediaController.receiveIngestCardStatus`, `SdCardIngestIndicator`) | **PARTIAL** (safe; slow; status partly misleading) | Per-file `unlink` only (`SdCardCamera.delete_media`, `delete_all_media` has no callers); delete needs on-card file + ledger record written after S3 upload **and** SkydiveOS 2xx + size match + grace. Tests: 133 passed (A), 44 + 23 passed (S). | Sweep runs only at the start of the *next* pull; uploads strictly serial (~15 s overhead/clip); restart strands queued clips; `MIN_AGE_H` code default 24 h vs 0 in runbooks (Mac runbook omits it); banner is hard-coded English; before 2026-10-22 deletion was labelled "Scanning card for clips…" and the whole upload phase showed "preparing… 0 %"; today there is no "verifying" or "cleanup failed" state and `pending_files` conflates four conditions. **Cards 6467/4812 (18 Oct): BLOCKED** — no ledgers, logs or mentions on this machine. | Q13–Q16 |
| R5 | Instructor ↔ cards ↔ cameras association | **S** registry (`Staff.goproSerial`, `Staff.sdCards[]`, `mediaService._resolveRawIdentity`); **A** identity capture (`ingest/sdcard.py`, `qr.py`, `match.py`) | **PARTIAL** | Many cards per person (unique sparse multikey index) verified by 103 backend + 36 frontend tests; QR marker wins, then card pairing, then camera serial, then `instructor_id`, else 404; "Card 4812" is the CAMERA serial tail, not the card's. | **One camera per instructor only** (`goproSerial` scalar + unique index; pairing a second overwrites the first); two cards in one camera same day → P0 skip above; borrowed camera without QR/pairing attributes to the owner; 4-digit camera id is also the staging/ledger/S3 key (collision); volume serial changes on every format (USB readers hide the CID; the deployed Windows box uses one); SkydiveOS exact-matches last-4 while the pipeline suffix-matches. | Q17–Q19 |
| R6 | Video sharing that supports upselling | **S** (`ShareVideoMenu`, `generateDownloadLink`, `/media/view/:token`, `/gallery/:token`); **A** (`/j/{code}` hero Share, locked previews, unlock CTA) | **PARTIAL** | Staff "Share video" + "Copy Gallery Link"; customer-portal native share sheet; pipeline gallery Share on locked and unlocked pages; locked page serves watermarked bytes, same URL flips on unlock; no link can mint or serve unpaid masters (8 + 28 + 75 tests). | No revocation anywhere; SkydiveOS tokens always expose the clean master + Download (no preview-only share); no send-by-email workflow for staff (menu "Email" is `mailto:`); no share/preview/purchase analytics; a paid unlock never propagates to the SkydiveOS Media row (INFERRED); the staff delivery panel hides the `/j/{code}` link (BUG 420). | Q20–Q21 |
| R7 | Bilingual emails, French first, bilingual subject | **S** (`bilingualEmail.js`, `emailService.js`, inline composers); **A** (`api/delivery.py`) | **PARTIAL** | 26 `emailService` templates + 13 inline composers + QR documents: FR + EN complete in one message, combined subject, per-locale money/date, UTF-8/RFC-2047 verified offline; 172 S tests + 67 A tests pass. | Ordering follows `preferredLanguage` (EN first for EN customers); pipeline delivery email EN-only unless the env flag delegates; media SMS EN-only; invoice PDF EN-only; certificate EN-first for no-preference customers and single-language PDF; SMS-mirror emails bilingual only when the operator filled a FR body; waiver flow silently writes `preferredLanguage`. | Q22–Q23 |
| R8 | Cross-requirement regression | both | **PARTIAL** | See §C.8 | Shared seams: replace/tweak bypass; `exclude.json`; the camera-id key shared by staging, ledger and S3; the `CUSTOMER_EMAIL_SENDER` flag deciding which side emails; `preferredLanguage` written by the waiver flow. | — |

---

## C. Detailed findings

### C.1 R1 — Intro/outro play whole, without music

**Current behaviour (A, VERIFIED).** The pipeline does not know "first and last clip"; it knows
scenes. `api/selfie.py:classify_scene` (L749–784; GPS-less variants L786–846) labels a ground-level
file in the first 20 % of the chapter-sorted card as `intro_interview` and one in the last 20 % as
`outro_interview`. Same-label files are concatenated with per-file `file_offsets`. The bookend is
the **first source file** of the intro scene and the **last source file** of the outro scene
(`edl/validate.py:bookend_ranges` L1157–1196). `_require_bookends` (L1199–1266) runs **last** in
`validate_and_repair` (L1346–1351) for `BOOKEND_DELIVERABLES = {"full_video"}` (L91): overlapping
clips are cut to the parts outside the range and one 1× clip spanning it is inserted
chronologically. The renderer (`api/selfie.py:render_selfie_video`, L2515–2616) keeps the
original audio for any 1× clip inside a bookend range and gates the backing track to zero over
its output window with `volume='if(between(t,lo,hi)…,0,1)':eval=frame`, extended through the
2.5 s logo card when the outro ends the edit; the mix is `amix` of ambient + gated music. In
`full_video` the music additionally starts only at the first boarding/takeoff/plane clip.

**Evidence.** `jobs/08142ed6…/validation_report.json`: "kept intro bookend whole
intro_interview [0.00, 55.00] (replaced [0.00, 57.62])", "kept outro bookend whole
outro_interview [0.00, 46.78] (replaced [0.00, 34.95])"; `jobs/a34afaf2…` (Ultimate): both
bookends taken from the external camera because the instructor's card held only freefall and
canopy. Tests (all pass with FFmpeg on PATH): `tests/test_bookends.py` 14 tests incl.
`test_bookends_keep_their_own_sound_and_silence_the_music` (asserts the gate windows in the
generated `filter_complex`), `test_render_outputs_passes_bookends_to_full_video_only`;
`tests/test_render.py::test_rendered_cards_have_no_music` (real FFmpeg, silence detection
over the branded cards on the legacy path).

**S in-app editor (VERIFIED, appendix A2).** `S/backend/src/utils/editBookends.js` identifies
bookends by **clip type** (`intro`/`default-intro`, `outro`/`default-outro`, L27–41), moves them
to the edges, locks trim/speed/ramp/reverse (L66–90), strips transitions touching them (L107–119),
and `videoProcessor.js:_musicWindowFilters` (L1563–1599) hard-mutes every music track outside
`[intro, comp−outro)` with 0.5 s edge fades where a track is cut. Enforced in the reducer and in
both export paths; real-ffmpeg test "has NO music over the intro or the outro" passes
(34 tests). The original audio branch is never gated, except by "Mute original audio" which
silences the bookends too (`mediaService.js:4854`).

**Confirmed gaps / defects.**
- D-R1-1 (A, design scope): only `full_video`. `highlights` keeps a ≥3 s intro beat
  (`_require_intro`) and short outro beats; the three freefall-only cuts contain no interview.
- D-R1-2 (A, risk): identification is positional (file order within a label), not content-based;
  a stray first recording becomes "the intro", a card with two jumps mislabels.
- D-R1-3 (A, quality): the music gate is a hard step (no fade). Client to decide mute vs fade.
- D-R1-4 (A): no real-FFmpeg test measures silence over the *filmed* bookends in the selfie path.
- D-R1-5 (S→A, **P1**): the editor's rule covers only typed intro/outro clips. An imported AI
  `full_video` is one `original` clip; its filmed interview is trimmable, splittable, slowable, and
  with no typed bookend `musicWindow = null` so an added track plays over it.
  `replaceCustomerDelivery` → `POST /jobs/{id}/deliverables/{name}/replace` (`A/api/app.py:1922+`)
  puts that export behind the customer's link with no R1–R3 check; `tweakAutoEdit` → `/tweak`
  saves an operator EDL verbatim (`A/api/app.py:1804-1829`, no `validate_and_repair`).
- D-R1-6 (A, P3): replays re-render a persisted EDL without re-validating; jobs composed before
  2026-10-22 keep their trimmed interviews until re-processed (documented, but there is no
  tool that lists which delivered jobs predate the rule).

**Reproduction (D-R1-5, staff path):** import a delivered AI job into the SkydiveOS editor →
trim the first 10 s of the `original` clip → add a music track → export → "Replace customer's
video" → the customer's `/j/{code}` full video now opens mid-interview with music.

**Severity / impact.** Customer-visible edit-quality rule; no data or payment risk. P1 when a
staff re-edit is republished; otherwise the automatic path complies for `full_video`.

**Recommended next step (not implemented).** Decide Q1–Q4; then either make the pipeline's
`replace` endpoint run the same `validate_and_repair` on an EDL-level replacement (it currently
receives bytes, so this needs the SkydiveOS export to carry its timeline), or have the editor
treat the imported deliverable's filmed head/tail as typed bookends.

### C.2 R2 — Freefall uncut; head-only shortening onto the beat drop

**Current behaviour (A, VERIFIED).** Freefall = scene `freefall` with `exit_offset`
(`detect_scene_exit_offset`, every file searched; tumbling fallback) and `deploy_offset`
(`detect_deploy_offset`, 1.6 g shock; `0.0` when not found). `_require_continuous_freefall`
(`edl/validate.py` L929–1134) replaces all freefall clips by one run over
`[exit − lead, deploy + 3 s]`, keeping the editor's slow-mo beats at their own seconds, filling
gaps at 1×, dropping beats that would replay a second. Applies to `full_video`, `freefall`,
`external_freefall`, `chute_libre_selfie` (`CONTINUOUS_FREEFALL_DELIVERABLES`); `highlights`
excluded by design. Head shortening: `lead = min(drop, footage before exit, $FREEFALL_MAX_LEAD_S=15)`
(`render/beatdrop.py:plan_exit_lead`, `api/selfie.py:_freefall_lead`) applied only to the three
freefall-only cuts (`validate_and_repair` L1294–1298). The drop per track = sidecar JSON (none
ship today) else detection (`find_beat_drop`, L147–215). At render, `_freefall_music_offset` =
drop − output time of the exit, applied with `atrim`/`adelay` (L2576–2590), so a head trim or
`exclude.json` head cut still lands the exit on the drop. Music loops (`-stream_loop -1`), so
freefall longer than the track never runs dry.

**Evidence.** `tests/test_beatdrop.py` (all pass): detection on synthetic builds, sidecar
precedence, cap from env, `test_real_render_lands_the_drop_on_the_exit` (real FFmpeg render,
first bass onset within 0.15 s of the exit), `test_every_shipped_track_has_a_drop_to_sync_to`
(all 4 tracks in `templates/music`), validator lead tests; `tests/test_freefall_uncut.py` on three
real-job fixtures. Read-only diagnostic on real job `08142ed6…` (External package, "Free Fallin'"):
freefall EDL 7 contiguous clips `[64.09 → 122.71]`, drop 9.98 s, lead 9.98 s (cap 15), exit at
output 9.98 s, music offset 0.00 → "OK". Real Ultimate job `a34afaf2…`: `chute_libre_selfie`
17 contiguous clips `[5.04 → 63.06]` (lead 9.98 s before exit 15.02), `external_freefall`
`[26.06 → 91.09]` (deploy 88.09 + 3).

**Confirmed gaps / defects.**
- D-R2-1 (A, scope): `full_video` is continuous but starts exactly at the exit with no drop
  sync; `highlights` keeps short beats. Client to confirm which deliverables the rule targets.
- D-R2-2 (A, semantics): the run also ends at deploy + 3 s (the canopy tail inside the freefall
  file is cut); and 0.4× slow-mo beats sit inside the "uncut" run. Both need client confirmation.
- D-R2-3 (A, **uncommitted fix**): `deploy_offset = 0.0` used to be consumed as a real anchor,
  collapsing a real External job's freefall to a 2 s beat (CLAUDE.md, 2026-10-24). The fix
  (`_deploy_detected`, `_undetected_deploy_notes`) exists only in the working tree, with tests
  (`test_undetected_deploy_runs_the_freefall_to_the_scene_end`, passing locally).
- D-R2-4 (A, P2): `apply_exclusions` splits `full_video`/`highlights` clips around any
  `exclude.json` range at render time, after validation — a mid-jump exclusion cuts the full
  video's freefall. Only the freefall-only cuts are protected (`exclude_freefall_head`). Nothing
  in the repo writes `exclude.json` today (hand-edit path).
- D-R2-5 (A, BLOCKED): detector accuracy. The rule aligns the *detected* exit; whether that is the
  exit the customer sees needs `scripts/diagnose_freefall_sync.py <job> --exit-at m:ss` against a
  human observation. The tumbling-exit fallback was added on 2026-10-23 after a real miss.
- D-R2-6 (A, P3): `scripts/diagnose_freefall_sync.py:298-300` reads `report[deliverable]` but
  the report is `{"repairs": {deliverable: [...]}}`, so section 1 always prints "no 'freefall
  plays whole' repair logged" even when it was (seen on job `08142ed6…`).
- D-R2-7 (S→A, P1): same republish/tweak bypass as D-R1-5; the editor has no freefall or beat
  rule at all (grep confirms).
- D-R2-8 (S, P3): the automation hand-off sends no music choice, template or pinned drop; the
  pipeline picks a random library track per job (persisted, so replays are stable).

**Reproduction (D-R2-4):** write `jobs/<id>/exclude.json` `{"freefall": [[80, 85]]}` on a
scene-pipeline job and replay → `full_video` skips 80–85 s of freefall while `freefall.mp4`
ignores it with a warning.

**Severity / impact.** Core edit-quality rule; verified for the freefall deliverables on the
automatic path; P1 only through the staff bypass; P2 for the uncommitted sentinel fix (until
merged and deployed, a cameraman who stops recording at the opening yields a 2 s freefall).

**Recommended next step.** Commit/deploy the sentinel fix; decide Q5–Q9; add a real-render
assertion for the full video's intro/outro silence; extend `exclude.json` protection to the
full video's freefall run if the client confirms R2 applies there.

### C.3 R3 — Continuous freefall for all packages; Ultimate alternation

**Current behaviour (A, VERIFIED).** Package values `selfie`/`external`/`video_only`/
`photo_only`/`ultimum` (`api/jobs.py:Package` L108). Single-camera packages carry no `camera`
tag, so the multicam branch (`validate_and_repair` L1303) never runs; a mixed `media_refs` job
renders each camera on its own through `compose_edls`/`render_outputs`
(`run_media_ref_pipeline` L3122+), never merging. Ultimate: per-camera scene sets, house cut per
camera, `_merge_multicam` interleaves by scene, then `_align_cross_camera` (exit-anchored order,
logs >4 s jumps), `_limit_switch_rate` (≥3 s), `_enforce_min_shot` (≥1.5 s), and
`_require_continuous_freefall(multicam=True)` rebuilds the freefall on the **instructor's**
window holding each angle 5 s (`_MULTICAM_FF_SHOT_S`), each camera only inside its own aerial
window, the other covering where one has no footage, a gap logged where neither does. Total
freefall duration equals the instructor's window; nothing is replayed.

**Evidence.** `tests/test_freefall_uncut.py::test_ultimate_combo_freefall_is_continuous_and_alternates`
(real fixture: both angles, ≥4 switches, anchored continuity, per-camera window, ≥3 s per angle,
idempotent), `…falls_back_to_the_camera_that_has_footage`, `…highlights_are_not_rebuilt`. Real
job `a34afaf2…/edl_full.json`: 18 freefall clips, e.g. instructor 16.02–18.98 → external
40.00–43.50 = anchored 0.96–3.96 → 3.96–7.46 s (instructor exit 15.02, external exit 36.04);
report line "freefall plays whole: [15.02, 63.06] as 18 continuous clip(s), alternating
instructor / external".

**S hand-off (VERIFIED, appendix A2).** Automation path `S/backend/src/utils/autoEditPackage.js:71-122`:
`isTwoCameraVideo === true` → `ultimum` (the only Ultimate signal; `videoAngle:'both'` alone is
refused as ambiguous); photos → `photo_only`; video + inside → `selfie`; video + outside →
`external`; else `video_only`. QR/SD path `autoEditOrchestrationService.resolveJumperPackage`
L543–561 reads the jumper's derived union: `none` → role-default + `preview_only`;
`video-photos` + `both` → `ultimum`. `POST /jobs` carries `package`, `entitlement`, `media_refs`
(when ≥2), names, booking, date; `camera_role` is mandatory on upload for Ultimate/multi-ref.
The status callback stores links and entitlements and never re-encodes.

**Confirmed gaps / defects.**
- D-R3-1 (A, decision): 5 s hold, 3 s switch spacing, 1.5 s minimum shot, "instructor's window
  is the timeline", "the other camera covers gaps" are implementation choices (Q10–Q12).
- D-R3-2 (A, risk): cross-camera sync = each camera's detected exit only (no clock/audio
  alignment); a mis-detected exit on one camera shifts every switch; `_ANCHOR_TOL_S` only logs.
- D-R3-3 (S, INFERRED, P2): the two package mappings disagree on `videoType:'both'`; on the
  QR/SD path a paired inside + outside purchase (a `media_refs` jumper) can be classified
  `ultimum` from the union, i.e. the one package allowed to cut inside the freefall.
- D-R3-4 (A): a missing second camera never renders single-camera; `ultimum_watchdog_job`
  fails the job after 1 h (actionable error, not a hang).

**Severity.** P2 for D-R3-3 (needs a runtime check against live bookings); otherwise decisions.

### C.4 R4 — SD-card ingestion: cleanup speed, safety, status (appendix B)

**Mechanism (VERIFIED).** `A/ingest/sdcard.py:435-452 SdCardCamera.delete_media` unlinks one
MP4 (fatal on failure) and its LRV/THM (best-effort); the base class refuses deletes;
`delete_all_media` has no call sites. The only caller is `A/ingest/pull.py:127-177 _sweep_card`,
run at the **start** of a pull for the records `A/ingest/retention.py:145-186 deletable` returns
from `_camera-staging/<camera_id>/.transferred.json`. A record is written only after
`upload_file` succeeded **and** SkydiveOS answered 2xx (`A/ingest/discovery.py:396-440, 883-893`),
carries the file size, and `deletable()` requires the on-card file to match that size and the
grace (`DELETE_AFTER_TRANSFER_MIN_AGE_H`, code default 24 h `A/api/config.py:393-395`; `0` in
`.env.example:50` and the Windows runbook; absent from the Mac runbook). Switches:
`DELETE_AFTER_TRANSFER` (default off), `DELETE_AFTER_TRANSFER_DRY_RUN`, `CARD_SAFE_REQUIRES_UPLOAD=1`.

**Sequence and timings (VERIFIED, defaults).** insert → scan ≤30 s (`DISCOVERY_INTERVAL_SECONDS`)
→ pull: sweep (previous confirmations) → copy with real byte progress → `_report_card_state`
(`A/api/app.py:651-701`) = `uploading` → `_materialize_loop` uploads **one clip at a time**
(QR decode, two `ffprobe`s, Mongo lookups, `head_object`, `upload_file`, notify, full ledger
rewrite; ≈15 s fixed per clip on this box plus the uplink) → immediate rescan (since `67408a8`,
2026-10-22; before: ≤30 s wait) → re-pull: sweep deletes, copy skips → `safe_to_remove` → push 2 s
→ SkydiveOS TTL 10 s → UI poll 2 s. The floor is the S3 upload of every clip; deletion cannot
precede it by design, and the sweep only runs inside a pull.

**Safeguards.** No path deletes on a weaker signal; a partial staged copy is unlinked without a
manifest (`pull.py:271-278`); ENOSPC, S3/network failure (10 retries ≈25 min), a 4xx from
SkydiveOS (never retried), a card yanked mid-copy or mid-sweep, a refused unlink, a corrupt ledger
all leave the file on the card and undeletable. Caveat: the LRV/THM are deleted on the MP4's
confirmation although the LRV is never uploaded.

**Status pipeline.** States `detected / pulling / sweeping / uploading / safe_to_remove / error`
→ `publish_card_status` (2 s, token-gated) → `S mediaController.receiveIngestCardStatus`
(`:603-622`) → `recordIngestCards` (TTL 10 s) → `GET /api/media/ingest-cards` → `SdCardIngestIndicator`
on **MediaPage only**, hard-coded English (no `fr` keys). Before 2026-10-22 (`dea314c`):
`sweeping` read **"Scanning card for clips…"** while deleting, and `uploading` fell through to
"Card detected — preparing… 0 %" for the whole upload (polled at 10 s). Today: copying shows real
bytes; uploading shows a clip count, no bytes; `sweeping` lasts milliseconds; there is **no
"verifying", "cleanup complete" or "cleanup failed" state**; `pending_files` conflates
"not in S3", "in S3 but not swept", "notify rejected" and "delete refused"; after a full sweep
the green row likely reads "0 clips ingested" (INFERRED). Progress percentages are real for the
copy phase only.

**Cards 6467 / 4812 (2026-10-18) — BLOCKED.** No ledger, log, bridge state or document on this
machine mentions them (only a dev `sd-disk` staging tree from today's test). Those ids are camera
serial tails, so the evidence (`_camera-staging/6467/.transferred.json`, `…/4812/…`,
`logs/ingest.err.log`, the build then deployed) is on the dropzone ingest box. Code trace: a
partially copied or uploaded card **cannot** be cleared by the current logic; the realistic causes
of "clearing takes too long" are a slow uplink, the pre-10-22 banner (upload phase shown as
"preparing… 0 %", deletion shown as "scanning"), `MIN_AGE_H` left at the 24 h default (green
banner, nothing deleted until the card is re-inserted a day later), a 404'd clip, or a
write-protected card.

**Confirmed defects.** P1: sweep deferred to the next pull + serial uploads (the performance
complaint); restart strands queued clips (`discovery.py:745-746`). P2: `pending_files`
conflation; 24 h code default vs 0 in runbooks; no en/fr banner; pre-10-22 wording (if still
deployed). P3: "0 clips ingested"; MediaPage-only banner; LRV deleted on the MP4's key; no
upload byte progress. Low-likelihood P1: ledger identity is name + size, not content, so a
formatted card whose same-named file has an identical byte size could be deleted after grace
(S3 keys fingerprint on size mismatch; the ledger does not).

**Tests run.** A: `test_sdcard` 24, `test_sdcard_windows` 13, `test_retention` 24,
`test_cardstatus` 21, `test_discovery` 51 passed / 1 env failure (missing `sample-data/`) →
133 passed, 1 failed. S backend `ingestCardsProxy` + `sdCardPairing` → 44 passed; S frontend
`SdCardIngestIndicator` + `SdCardsSection` → 23 passed.

**Recommended next step.** Get the 18 Oct ledgers/logs and build id from the ingest box;
decide Q13–Q16; then consider parallel uploads, a sweep that runs after the last confirmation
while the card is still mounted, a durable hand-off queue, and distinct banner states.

### C.5 R5 — Instructor ↔ camera ↔ card association (appendix C)

**Data model (VERIFIED).** S `Staff.goproSerial`: single string, `unique + sparse`
(`S/backend/src/models/Staff.js:115-120`); `cameraService.persistSerial` replaces it
(`cameraService.js:75-90`); UI offers one input + "Change". S `Staff.sdCards[]`:
`{serial, label, addedAt, addedBy}`, multikey unique sparse index (`Staff.js:128-140, 313`), max
32, 409 on another owner. A `cameras` registry: `{camera_id (last-4), instructor_id, role hint,
active}`, unique on `camera_id` only, N cameras per instructor allowed. Raw rows record
`autoMatch` (`matcher`, `cameraId`, `sdCardSerial`, `staffSource`).

**Identification flow (VERIFIED).** Card inserted → `camera_id` = last 4 digits of the CAMERA
serial in `MISC/version.txt` (`A/ingest/sdcard.py:264-284`; fallback `sd-<label>` / drive letter)
— this is the "Card 4812" label (`S SdCardIngestIndicator.js:103-110`) and also the staging-tree
id, retention-ledger key and S3 prefix; `card_serial` = hardware CID on Linux SD-host readers,
else the volume serial (Windows `GetVolumeInformationW`, macOS `diskutil`) (`sdcard.py:151-216`)
— the deployed North Shore box is Windows + USB reader, which hides the CID, so identity = volume
serial, which changes on every format. QR marker (`skydiveos-staff:<_id>`) claims the day's clips
card-wide (`A/ingest/qr.py:292-406`). Notify carries `staff_id`/`staff_source`, `sd_card_serial`,
`camera_id`, `instructor_id?`, `captured_at`. S `mediaService._resolveRawIdentity` (`:1047-1128`):
QR staff → card pairing → `goproSerial` exact match on the last-4 → `instructor_id` → 404 (permanent
on the box). QR vs card disagreement is logged, never refused.

**Scenario matrix** (appendix C §c, 18 scenarios): several cards per instructor — **yes**;
several cameras per instructor — **no** (unless every card is paired or QR'd); two cards in one
camera the same day — **identity fine, ingestion broken** (P0 above); shared/borrowed camera
without QR or pairing — **unsafe** (attributes to the owner; misassigned if the owner also holds a
slot in that window); reformatted card — must re-pair; same last-4 on two cameras — collision
(pipeline refuses, SkydiveOS cannot detect); full serial typed into SkydiveOS — 404 (pipeline
suffix-matches, SkydiveOS exact-matches).

**Tests run.** A: 271 passed, 1 env failure. S backend 6 suites / 103 passed; S frontend 3 suites /
36 passed. Note: 2 pipeline matcher tests fail on this box in `tests/test_flight_window_callbacks.py`
(`TestMatcherReadsTheWindow`: `NoLoadMatch … none whose flight window contains the clip`); cause
not determined (possibly timezone-dependent); they cover the spec-flight window path and should be
looked at before relying on that path.

**Compatibility (analysis only).** N cameras per instructor needs nothing in the pipeline; in
SkydiveOS it needs `goproSerial` → array + multikey unique index, plus `_resolveRawInstructor`,
`cameraService`, `formatStaff`, the orchestration re-match and the pairing UI; existing pairings
would migrate as one-element arrays. No migration was proposed or performed.

### C.6 R6 — Video sharing for upselling (appendix D)

See §F for the matrix. Security per link type (VERIFIED):
- S `/media/view/{token}`: 256-bit bearer, TTL `downloadLinkTTLDays` (30 d), no viewer auth,
  serves the **presigned full-res master** with a Download button; the first download flips the
  media to `delivered`. Unpaid media cannot be reached: a `preview_only` import is `status: linked`
  and `_assertNotPaywalled` blocks submit/approve, so no token can be minted.
- S `/gallery/{token}`: same model, session-scoped, status-filtered; `revokedAt` exists but has
  **no writer**.
- S presigned S3 `url`: 1 h, now Download-only (a test pins that it is never shared).
- A `/j/{code}`: 11-char base62 bearer (~65 bits), never expires, no revoke, exempt from the
  service token; the entitlement, per deliverable and per request, picks clean vs watermarked
  bytes; locked names are never presigned; share anchors carry `rel="noreferrer"`; the Share
  button shares the clean link (no `?s=`/`#tab`), works on the locked page (friend sees the same
  watermark + unlock CTA), and the same URL flips after `POST /jobs/{id}/unlock`.
- Delivery emails: S `sendMediaGalleryDeliveryEmail` has locked/unlocked and "new video added"
  variants pointing at `/j/{code}?s=e` (SMS `?s=m`); A `send_gallery_email` is one plain link.

**Defects.** P1: no revocation anywhere (customer/permission changes never invalidate a token);
no preview-only share from SkydiveOS (every SkydiveOS token hands over the clean master). P2:
a paid unlock never updates the SkydiveOS Media row's entitlement (INFERRED: no writer;
re-import skips existing output types), so staff can never approve/share that video from
SkydiveOS; no share/preview/purchase analytics (`?s=` is emitted but nothing consumes it);
staff sharing is clipboard-only. P3: pipeline email lacks locked copy; no `og:image`; instructor
role gets 403 on share mint while the UI gates on status only; plain `npx jest` fails on ESM.

### C.7 R7 — Bilingual customer emails (appendix E, full inventory §D)

**Mechanism (VERIFIED).** `S/backend/src/utils/bilingualEmail.js:composeBilingualEmail` renders
the template for `fr` and `en`, stitches one subject (`primary / secondary`), one HTML with
"Français"/"English" sections and one text body in the same order; `emailService.sendBilingualEmail`
is the single sender; `applyEmailBranding` adds a FR + EN footer when `bilingual: true`. All 13
inline composers forward the branding. nodemailer 7.0.12 emits RFC-2047 subjects and
`charset=utf-8` bodies (verified offline with a stream transport; nothing sent).

**Language selection (VERIFIED).** `languageOrder()` (`:58-61`): `preferredLanguage === 'en'` →
`['en','fr']`; anything else (fr, null, invalid, `fr-CA`) → `['fr','en']`; no preference → deployment
default `EMAIL_PRIMARY_LANGUAGE` (undocumented; default `fr`). About 20 call sites pass the
customer's preference; about 15 pass none (always FR first). Executed evidence (composer run
offline): pref `en` → subject `Your skydive video is ready / Votre vidéo de saut est prête`, text
starts "ENGLISH … Hi Alex,". `bilingualCustomerEmails.test.js:104-116` pins preference-led order.

**Defects.** P1: ordering preference-led (F1); pipeline delivery email EN-only unless
`CUSTOMER_EMAIL_SENDER=skydiveos` (F2; per-stack `.env` unverifiable here; `.env.example` says
`skydiveos`, code default `pipeline`); media SMS EN-only (F3); certificate email leads English for
a no-preference customer and the PDF is single-language (F4); invoice PDF English-only (F5),
attached to the bilingual invoice email and Email A. P2: SMS-mirror emails (boarding/waiver
reminders, broadcasts) bilingual only when the operator filled `SmsTemplate.body.fr` (schema
default empty, no seeds) and the broadcast subject is never bilingual (F6); the waiver flow
persists an auto-resolved language onto `Customer.preferredLanguage` when unset (F7); two senders
exist for one `delivered` event, exactly-one depending on each stack's flag (F8; dedupe within each
side is solid: `MediaDeliveryNotification` unique key, pipeline `O_EXCL` claim + `email_sent_at`).
P3: doc/code drift on who passes `primaryLang`; four dead templates.

**Tests run.** S: `bilingualCustomerEmails bilingualSimpleTemplates bilingualInlineEmails
consolidatedCustomerEmails notificationEmailPipeline task431Certificates mediaDeliveryEmail
giftCardEmailStartDate` → 8 suites / 172 passed. A: `tests/test_delivery.py` → 67 passed (the
English subject is not pinned).

### C.8 R8 — Cross-requirement regression

- **Intro/outro vs freefall sync vs alternation (A):** `validate_and_repair` orders the rules so
  they cannot undo each other: window clamp → dedupe → sort → deploy beat → boarding → door
  arrival → (multicam) align/switch-rate/min-shot → continuous freefall → bookends last. Verified
  on the Ultimate fixture and real job (both bookends and the alternating run present).
- **Music over bookends when the timeline is short or a camera is missing (A):** the gate is
  derived from the rendered clip list, not stored, so it holds on replay; a missing outro means
  no gate and the music runs to the card (expected).
- **Freefall cut by other constraints (A):** `_FREEFALL_MAX_OUTPUT_S` no longer bounds the
  freefall cut; `highlights` is the deliberate exception; `exclude.json` and the SkydiveOS
  re-edit remain the two ways to cut it (§C.2).
- **Cleanup before confirmed copy (A):** impossible by code; the shared `camera_id` key is the one
  seam where two cards' files can be confused (P0 above).
- **Wrong instructor/customer (A+S):** the borrowed-camera and same-last-4 scenarios (§C.5); the
  QR-wins-over-card rule is logged, not refused; two pipeline matcher tests fail on this box.
- **Sharing vs paywall (A+S):** no link exposes unpaid masters; but no link can be revoked, and
  the SkydiveOS link always carries the master for paid media.
- **Bilingual vs payments/security emails (S):** every security-sensitive template keeps its
  recipient/token (same `sendEmail` call, only the body/subject composed); duplicate-send guards
  are per side; the only cross-side duplicate risk is the delivery email flag.
- **Shared dependencies:** `A/api/app.py` replace + tweak endpoints; `exclude.json`;
  `camera_id` as staging/ledger/S3 key; `CUSTOMER_EMAIL_SENDER`; `Customer.preferredLanguage`
  (written by the waiver flow, read by every email); `isTwoCameraVideo` vs the union for Ultimate.

---

## D. Email coverage matrix

One row per distinct email type and pathway, with template path, trigger, sender, subject
construction, FR/EN status, ordering, attachments and test evidence, is in
`appendix-E-bilingual-emails.md` §1 (26 `emailService` templates, 13 inline composers, 3
SMS-mirror templates, 2 pipeline emails, 4 SMS, 6 staff/system emails). Condensed:

| Group | Count | FR | EN | One email | Subject bilingual | Ordering | Attachments | Result |
|---|---|---|---|---|---|---|---|---|
| S `emailService.js` customer templates (reset, setup, invitation, signing request/reminder, document completed, group booking, ticket, certificate, media delivery (manual + gallery, 6 variants), check-in, invoice, Email A welcome, balance receipt, Email B ready-to-jump, bundled signing, all-signed, gift card + receipt) | 22 live (+4 dead) | ✓ | ✓ | ✓ | ✓ | PREF where the caller passes it (≈9), else FR-first | invoice PDF **EN-only**; certificate PDF single-language | PASS except invoice/Email A (PARTIAL) and certificate (PARTIAL) |
| S inline composers (legacy booking fan-out, refund receipts ×2, kiosk confirm + account setup, email-change OTP, reschedule ×3, waitlist promotion, group confirmation, payment-confirmed fan-out, QR check-in doc, manifested doc) | 13 | ✓ | ✓ | ✓ | ✓ | PREF | QR PNG (neutral) | PASS (ordering decision Q22) |
| S SMS-mirror emails (boarding reminder, waiver 24 h, broadcast) | 3 | only if operator filled `body.fr` | ✓ | ✓ | boarding/waiver ✓; broadcast ✗ | PREF | — | PARTIAL / FAIL (broadcast subject) |
| A pipeline `send_gallery_email` ("your video is ready") | 1 live (+1 dead) | ✗ | ✓ | n/a | ✗ | EN only | — | **FAIL** unless the stack delegates to SkydiveOS |
| S media SMS (ready / added / updated) | 3 | ✗ | ✓ | — | — | EN only | — | FAIL if SMS is in scope (Q23) |
| S templated SMS | 1 | per customer language | | single-language by design | | | | n/a |
| Staff/system (staff invitation, SMTP test, pilot/recon/Retell/stuck-unlock alerts, AIM PDF) | 6 | ✗ | ✓ | — | — | EN | — | out of scope pending Q22 |

---

## E. Video pipeline and SD-card safety findings

**Processing sequence (A, per job):** ingest (per-clip `POST /jobs/{id}/upload`, settle
`RAW_CLIP_SETTLE_SECONDS`=180 s, exactly-once dispatch) → classify files by telemetry into scenes
(`classify_scene`) → concat per scene with `file_offsets`, detect `exit_offset` / `deploy_offset` →
score faces on the freefall proxy → compose (Claude once per jump, or the house cut for
`external`/Ultimate) → `_ensure_story` → **`validate_and_repair` per deliverable** (window clamp,
dedupe, chronological sort, deploy beat, boarding, door arrival [uncommitted], highlights intro,
multicam align/switch-rate/min-shot, continuous freefall, bookends) → persist `edl_*.json` +
`validation_report.json` → render (`render_selfie_video`: hard concat, bookend audio + music gate,
music offset for freefall cuts, logo outro card) → previews/posters/archive → review or
`AUTO_DELIVER` → delivery (S3, `/j/{code}`, email/SMS by whichever side the flag names).

**Trimming rules:** freefall-only cuts contain only the freefall scene over
`[exit − min(drop, footage, 15 s), deploy + 3 s]`; `full_video` over `[exit, deploy + 3 s]`;
`highlights` keeps short beats everywhere; `full_video` bookends whole; everything else trimmed to
budget by the composer. **Audio:** `full_video` = original audio until boarding, music solo
boarding→canopy, original back + music at 0.2 from the canopy, music 0 and original audio over
bookends, silent logo card; `highlights`/freefall cuts = music only (freefall cut entered at
`drop − exit time`). **Camera switching:** Ultimate only; 5 s per angle on the instructor's
exit-anchored timeline, ≥3 s between switches, ≥1.5 s shots, each camera inside its own window.
**Replay:** re-renders the persisted EDL with the same bookends/music offset, no re-validation;
`exclude.json` applied at render.

**SD-card:** copy (per-file, manifest written last) → upload (serial, S3 then SkydiveOS notify,
ledger record with size) → next pull's sweep deletes only ledger-confirmed, size-matched,
grace-elapsed files, one `unlink` at a time; status `detected → pulling → uploading →
safe_to_remove | error`, `sweeping` transient; failure recovery = everything stays on the card
and undeletable; restart = queued hand-offs lost until a human re-pulls (§C.4).

---

## F. Sharing capability matrix

| Capability | Where | How it works | Entitlement / security check | Status |
|---|---|---|---|---|
| Staff "Share video" from Media → details | S `MediaDetailDialog.js:89,234-249,859` → `GET /api/media/:id/download` → `generateDownloadLink` (`mediaService.js:2573-2628`) | returns `shareUrl=/media/view/{token}` (30 d) beside a 1 h presigned `url` (Download only) | status approved/delivered; customer own-media 403; route `authorize('manifest','customer')` | exists, VERIFIED (8 backend tests) |
| Staff "Copy Gallery Link" | S `MediaDetailDialog.js:251-260` → `POST /media/gallery-share` → `createGalleryShare` (`:2787-2836`) | 32-byte token, TTL 30 d, lists approved/delivered rows of the session | token + expiry; `revokedAt` has no writer | exists, traced |
| Staff row menu "Copy share link" | S `MediaPage.js:187-193` | same mint | same | UI wired, not separately tested |
| Staff sends link by email/SMS | — | only admin "resend delivery email"; menu "Email" is `mailto:` | — | MISSING |
| Staff shares a preview without granting the master | — (only via the pipeline `/j/{code}` link, hidden from the staff panel, BUG 420) | — | — | MISSING in SkydiveOS |
| Customer portal share | S `CustomerMediaPage.js:43-49,140` → `ShareVideoMenu` (`videoShare.js:80-114`) | native share sheet where available, else Copy / WhatsApp / Facebook / Email; link resolved on menu open | own media only (403); list approved/delivered only, so locked media cannot be shared | exists, VERIFIED (28 frontend tests) |
| Public `/media/view/:token` | S `MediaViewPage.js`, `getMediaByToken` (`:2685-2745`) | branded page, plays and **downloads the full-res master**; first download marks delivered | token + expiry only | exists, VERIFIED |
| Public `/gallery/:token` | S `getGalleryByToken` (`:2902-2960`) | presigned masters, ZIP | token, `revokedAt:null`, expiry | exists |
| AI gallery Share (hero) | A `api/app.py:2608-2619`, `api/gallery.py:553-603` | `<details>` menu + `navigator.share`; shares the clean `/j/{code}`; `og:` tags (no image) | the code is the credential; entitlement per deliverable per request; `?s=` ignored | exists, VERIFIED (`test_gallery_share.py`) |
| Locked preview + Unlock CTA | A `api/app.py:2484-2510`, `gallery.py:326-358,423-432` → S `/unlock/{job_id}?item=` | watermarked 720p `preview_<name>.mp4`, no download; per-camera CTAs on mixed jobs; prices from the admin catalogue | locked names never presigned; clean bytes unreachable at any URL | exists, VERIFIED (75 pipeline tests) |
| Unlock → flip | S `paymentEventHandler.js:648-660` → `unlockPaidMedia` → A `POST /jobs/{id}/unlock` | same URL serves clean bytes; idempotent; three gates (service token, admin, payment reference) | gated on `paymentScope === 'media-unlock'` | exists, VERIFIED |
| Revocation / expiry changes | — | S tokens expire at 30 d; A code never expires | none on either side | MISSING |
| Share / preview / purchase analytics | — | `?s=e`/`?s=m` emitted, nothing consumes it; only download-redirect and gallery counters | — | MISSING |

---

## G. Client clarification checklist

**R1 — intro/outro**
1. Do "first and final clips" mean the filmed interview / landing reaction (what the pipeline
   calls the first `intro_interview` file and the last `outro_interview` file), the branded title
   cards, or literally the first and last recordings on the card regardless of content?
2. Must the rule hold for `highlights` (today a ≥3 s intro beat) and the freefall-only cuts
   (today no interview at all), or for the full video only?
3. Music over these clips: completely muted (today: hard gate to 0; the SkydiveOS editor adds a
   0.5 s edge fade), or fade out before / fade in after?
4. Must the clip's own camera audio stay audible (today yes in the pipeline; SkydiveOS's "Mute
   original audio" silences it)? May a staff re-edit override the rule at all?

**R2 — freefall**
5. Is the music drop a fixed timestamp per track, configured per track, or detected? (Today:
   detected automatically, overridable per track by a sidecar file; no sidecars shipped; no
   operator UI.)
6. Which deliverables must land the exit on the drop: the freefall-only cuts (today), the full
   video (today starts at the exit, no sync), the highlights?
7. Accept the 15 s cap on footage before the exit (the track is entered mid-way when its drop
   is later), or allow more plane before the exit?
8. What ends the freefall: the deployment + 3 s (today), or the end of the recording? Are
   slow-motion beats inside the run acceptable, or must it be 1× throughout?
9. When the exit or the drop cannot be identified: today the cut starts at the detected
   exit (or scene start), music from 0, and the job is flagged for instructor review but still
   delivered under `AUTO_DELIVER`. Should it be held instead?

**R3 — packages / Ultimate**
10. How long should each outside-camera and handcam shot last? (Today 5 s per angle, ≥3 s between
    switches, ≥1.5 s minimum shot — implementation defaults, not client rules.)
11. Should the Ultimate freefall preserve the instructor camera's total freefall duration
    (today), and should the external camera's extra footage past that window be shown?
12. When one camera source is missing or short: cover with the other camera (today), leave a
    gap, or fail the job? Must `external_freefall` / `chute_libre_selfie` also be continuous?

**R4 — SD card**
13. Clear the card automatically after verified copy (today, if `DELETE_AFTER_TRANSFER` is on)
    or only after staff confirm? Is a 0 h grace intended?
14. Is a whole-card format acceptable, or must unconfirmed files be preserved (today: per-file,
    preserved)?
15. Exact wording (EN and FR) staff should see for: scanning, copying, uploading, verifying,
    clearing, cleared, clear failed, copy failed/incomplete.
16. When SkydiveOS rejects one clip (404), clear the rest and warn, or block the card? Please
    provide the 18 Oct build id and the `6467`/`4812` ledgers and `logs/ingest.err.log`.

**R5 — association**
17. Is the requirement physical SD-card serial recognition, multiple cameras per instructor, or
    both? (Today: many cards per instructor yes; one camera per instructor.)
18. Is "re-pair after every card format" acceptable, given the deployed Windows USB reader exposes
    only the volume serial?
19. Do staff lend cameras or cards, or carry two cameras at once? Is the camera-tail "Card 4812"
    label acceptable?

**R6 — sharing**
20. Which experience is required: (1) staff sends a preview link to encourage a purchase —
    partly exists (pipeline locked page + delivery email), no staff-visible link or send action;
    (2) customer shares a watermarked preview on social — exists; (3) dedicated AI-gallery share
    button — exists; (4) something else?
21. Should links be revocable or expiring? May shared viewers download? Should unlocks be
    attributed to shares? Should a paid unlock propagate to the SkydiveOS media row? Is "a
    friend's download marks the media delivered" acceptable?

**R7 — emails**
22. French first always, or the customer's preferred language first (today)? Are staff-only
    emails (invitation, payouts, alerts) and any transactional email exempt? Must invoice and
    certificate PDFs be bilingual? Must operator SMS/broadcast templates require a French body?
23. Does the bilingual rule cover SMS? Which side must send the delivery email on each stack
    (confirm `CUSTOMER_EMAIL_SENDER=skydiveos` everywhere, or make the pipeline email bilingual
    as a safety net)?

---

## H. Prioritized recommendations

**P0 — Critical**
- H1 (A, R5/R4): two cards through one camera on one day → second card's clips silently never
  staged (`ingest/pull.py:69-71`, `storage.py:87-89`); with an identical byte size the ledger could
  authorise deletion. Fix direction: key staging/ledger by card serial or content fingerprint, and
  compare size (or hash) in `is_complete`. Verify on the ingest box whether this happened on 18 Oct.

**P1 — High**
- H2 (A, R4): durable hand-off queue / re-emission of staged-but-unconfirmed clips on start.
- H3 (A, R4): sweep after the last confirmation while the card is mounted; parallel uploads;
  make `DELETE_AFTER_TRANSFER_MIN_AGE_H` explicit in every runbook.
- H4 (S→A, R1–R3): decide whether a staff re-edit may override the rules; if not, validate the
  replaced/tweaked timeline, or make the editor treat the imported deliverable's filmed head/tail
  as typed bookends and refuse cuts inside the freefall.
- H5 (A, R2): commit and deploy the `deploy_offset` sentinel fix (uncommitted today).
- H6 (S, R7): decide Q22; if French-first always, change `languageOrder`; make the media SMS,
  invoice PDF and certificate (email ordering + PDF) bilingual.
- H7 (A, R7): make `send_gallery_email` bilingual or prove every stack delegates it.
- H8 (S, R5): support several cameras per instructor (array + multikey unique index) if Q17 says so.
- H9 (S+A, R6): revocation for share links/gallery codes; a preview-only share in SkydiveOS if Q20
  picks option 1.

**P2 — Medium**
- H10 (S, R4): distinct banner states (verifying, clearing, cleared, clear failed, copy failed),
  byte progress for the upload, FR copy, banner beyond MediaPage.
- H11 (A, R2): protect the full video's freefall run from `exclude.json`; add a real-render
  silence test over filmed bookends.
- H12 (S, R3): reconcile the two package mappings on `videoType:'both'` (union vs structural).
- H13 (S, R6): propagate paid unlocks to the SkydiveOS media row; share/preview/purchase analytics.
- H14 (S, R7): SMS-mirror templates require a FR body or render bilingual from code; broadcast
  subject bilingual; stop the waiver flow writing `preferredLanguage`.
- H15 (A, R5): make the pipeline refuse a `goproSerial` stored as a full serial, or make
  SkydiveOS suffix-match too; surface same-last-4 collisions in the banner.

**P3 — Low**
- H16 (A): fix `scripts/diagnose_freefall_sync.py` report lookup; `scripts/replay_edl.py` named in
  CLAUDE.md does not exist (`replay_selfie.py` does); the "0 clips ingested" green row; LRV
  deleted on the MP4's key; `og:image`; dead email templates; document `EMAIL_PRIMARY_LANGUAGE`.
- H17 (A): the two failing matcher tests in `tests/test_flight_window_callbacks.py` on this box.

---

## I. Final verification checklist (for after fixes)

**R1** ☐ For each package, render a job whose card has a multi-file intro and outro; confirm in
`validation_report.json` the "kept intro/outro bookend whole" lines and, with
`ffmpeg -af silencedetect` on the output, music silence (and original audio present) over the
bookend windows for every deliverable the client named. ☐ Re-edit the same job in SkydiveOS,
trim the intro, add music, "Replace customer's video", and confirm the customer's file still
complies (or the replace is refused). ☐ Highlights behaviour matches Q2.

**R2** ☐ For each freefall-carrying deliverable, assert consecutive freefall clips are contiguous
(`scripts/diagnose_freefall_sync.py <job>` section 3 shows no skip/replay) and that
`--exit-at <observed exit>` reports the detected exit within 0.5 s. ☐ Measure the first bass
onset of the rendered freefall cut at the exit (`tests/test_beatdrop.py::test_real_render_lands_the_drop_on_the_exit`
pattern) for every shipped track. ☐ A job whose deploy is undetected renders the whole filmed
freefall. ☐ An `exclude.json` mid-jump range is refused for every deliverable the client named.

**R3** ☐ Single-camera packages: no `camera` tag in any EDL. ☐ Ultimate: alternation cadence per
Q10, anchored continuity (`test_ultimate_combo_freefall_is_continuous_and_alternates`), behaviour
with one camera short per Q12. ☐ A paired inside + outside purchase on the QR/SD path creates a
two-ref job, not an Ultimate job.

**R4** ☐ Insert a card with N clips: banner shows copying (bytes), uploading (bytes), clearing,
cleared; `.transferred.json` has N records with sizes; `DCIM` empty only after the last S3 key is
recorded; a restart mid-upload resumes the queue; a rejected clip is shown as such and stays on
the card. ☐ Timing: cleared within one upload pass, no 30 s idle gaps. ☐ Both EN and FR copy.

**R5** ☐ Two cards, same camera, same day, restarted numbering: both sets of clips staged,
uploaded, matched. ☐ Two cameras paired to one instructor; both cards attribute correctly with no
QR. ☐ Borrowed camera with QR → borrower; without QR → refused, never the owner's customer.
☐ Same-last-4 cameras flagged.

**R6** ☐ A locked job's share link opens the watermarked preview with the CTA on desktop and
mobile; after unlock the same link plays the master. ☐ Revocation (if adopted) kills a link on both
sides. ☐ No SkydiveOS link reaches a `preview_only` deliverable's master. ☐ Share analytics (if
adopted) count visits, plays, purchases.

**R7** ☐ For every row of appendix E §1, send to a sink SMTP for a FR-preferred, an EN-preferred and
a no-preference customer: one email, FR section first (per Q22), bilingual subject, HTML and text
in the same order, accents intact, attachments bilingual per Q22. ☐ Exactly one delivery email per
job per stack (pipeline flag audit `deploy/ec2/customer-email-sender.sh audit`). ☐ SMS per Q23.

---

## Test execution record (read-only, 2026-10-24)

| Suite | Command | Result |
|---|---|---|
| A full suite (no FFmpeg on PATH) | `uv run --no-sync pytest tests -q` | 23 failed / 1391 passed / 23 skipped — 18 failures were `FileNotFoundError: 'ffprobe'` |
| A editing suites (FFmpeg from `~/.local/bin/ffbin`) | `pytest tests/test_beatdrop.py test_bookends.py test_selfie.py test_source_usage.py test_ultimate.py test_discovery.py test_flight_window_callbacks.py test_metadata.py test_render.py test_edl_validate.py test_freefall_uncut.py test_exit_tumbling.py` | 335 passed / 4 failed (2 env: missing `sample-data`; 2 matcher tests, see H17) |
| A card/association suites | per appendix B/C | 133 passed / 1 env failure; 271 passed / 1 env failure |
| A sharing / delivery | `test_gallery_share.py test_mixed_entitlement.py`; `test_delivery.py` | 75 passed; 67 passed |
| S backend (ESM runner `node --experimental-vm-modules … jest`) | share 8; cards+pairing 103; ingest proxy 44; editor bookends + slow-mo 34; orchestration/package/import 224; bilingual + email 172 | all passed |
| S frontend (`craco test`) | VideoShare 28; SdCards 36 + 23; EditBookends 37 | all passed |
| A diagnostics | `scripts/diagnose_freefall_sync.py 08142ed6225841bc887c457d0059fe23` | OK (drop on exit); tool's section 1 misreads the report (H16) |

**Not tested, and why:** production deployments of either repo (no access); the 18 Oct cards
(data on the ingest box); real SD cards (none inserted; policy forbids writes anyway); live
Mongo/Atlas (no `MONGO_URL` on this box); exit-detector accuracy against a human-observed exit;
rendering on real customer footage beyond the two jobs already on disk; email client rendering.
