### R1 — Intro and outro clips play whole, without music (AI pipeline side)

**Responsible service:** skydiveos-autoedit (the AI pipeline). Rules live in `edl/validate.py`
(`bookend_ranges`, `_require_bookends`, `BOOKEND_DELIVERABLES`) and the renderer
`api/selfie.py:render_selfie_video` (`bookends=` argument). The legacy single-master
renderer `render/builder.py` has its own rule for the branded *cards* (not the filmed clips).

**How the first/final clips are identified (VERIFIED):**
- Not by position in the card, not by camera, not by timestamp: by **scene label + file order**.
  Every raw file is classified by telemetry into a scene (`api/selfie.py:classify_scene` L749–784,
  `_classify_no_gps` L786–804, `_classify_no_gps_anchored` L822–846). A ground-level clip
  (altitude < 50 m above the dropzone) in the **first 20 %** of the chapter-sorted files is
  `intro_interview`; one in the **last 20 %** is `outro_interview`; other ground clips are
  `boarding`. Same-label files are concatenated into one scene file with per-file
  `file_offsets` (`build_scenes` L1053+).
- The bookend is then the **FIRST source file of `intro_interview`** (`[0, file_offsets[1])`)
  and the **LAST source file of `outro_interview`** (`edl/validate.py:_first_file_range`
  L1138, `_last_file_range` L1148, `bookend_ranges` L1157–1196). A single-file scene is the
  bookend whole (interview + walk-out together).
- Multi-cam (Ultimate): ONE camera per bookend, instructor first (`bookend_ranges` L1181–1187).
  The other camera's interview fragments are left as composed (they may still appear, trimmed,
  from the second angle — not a trimming of *the* bookend, but worth a client look).

**Trimming protection (VERIFIED):** `validate_and_repair` runs `_require_bookends` **last**
(`edl/validate.py` L1346–1351) so no later rule (dedupe, switch-rate, min-shot, clamps) can
touch the inserted whole-file 1× clip. Every clip of that (camera, scene) overlapping the range
is cut to the parts *outside* it and exactly one real-time clip spanning the range is inserted
chronologically (L1199–1266). An EDL already carrying the exact clip is untouched.
- Applies ONLY to `full_video` (`BOOKEND_DELIVERABLES = {"full_video"}`, L91). `highlights`
  keeps a ≥3 s intro beat (`_require_intro` L644) and short outro beats by design; the three
  freefall-only cuts drop every non-freefall scene (`_drop_non_freefall`). → **client decision**.
- The Claude compose prompt also asks for whole bookends (`api/selfie.py` L1303–1306) but the
  prompt is advisory; the validator is the enforcement.
- Real-job evidence: `jobs/08142ed6…/validation_report.json` — "kept intro bookend whole
  intro_interview [0.00, 55.00] (replaced [0.00, 57.62])" and "kept outro bookend whole
  outro_interview [0.00, 46.78] (replaced [0.00, 34.95])"; `jobs/a34afaf2…` (Ultimate) —
  intro `external/intro_interview [0.00, 22.05]`, outro `[0.00, 33.15]`, from the external
  camera because the instructor's card held only freefall + canopy.

**Music over the bookends (VERIFIED in the filter graph; test is a graph assertion, not an
acoustic measurement):** `render_selfie_video` (`api/selfie.py` L2515–2616):
- A real-time clip lying inside a bookend range keeps its **original camera audio** whatever
  the mix mode says (`use_original = … bookend or …`, L2547–2556), and its output window is
  recorded in `silent_windows`.
- The backing track is **hard-gated to 0** over every such window with
  `volume='if(between(t,lo,hi)+…,0,1)':eval=frame` (L2604–2612) — no fade, no crossfade,
  no overlap. The outro bookend's gate is extended through the 2.5 s logo card so music does
  not pop back for the card. The final mix is `amix` of ambient + gated music.
- `full_video` additionally starts the music only at the first `boarding`/`takeoff`/`plane`
  clip (`_audio_markers` L2345–2368, `_MUSIC_START_SCENES`), so nothing before boarding
  carries music even without the gate.
- A 0.4× slow-mo clip is never a bookend and never carries stretched audio.
- Tests: `tests/test_bookends.py::test_bookends_keep_their_own_sound_and_silence_the_music`
  asserts the gate windows in the generated `filter_complex` (string assertion);
  `test_render_outputs_passes_bookends_to_full_video_only`; `test_the_compose_prompt_asks_for_whole_bookends`.
  There is **no real-FFmpeg render test** measuring silence over the *filmed* bookends in the
  selfie path (the real-render silence test exists only for the legacy card path,
  `tests/test_render.py::test_rendered_cards_have_no_music`).
- The branded outro logo card (2.5 s) is silent by construction (`_SILENCE` L2566). The
  selfie path renders **no opening title card** (docstring L2457).

**Legacy single-master path** (`render/builder.py`, used only by `process_job` for Open GoPro
pulls without a package): the intro/outro **cards** are never trimmed and the music bed is
cut to the footage, faded over its last 1.0 s and delayed by the intro length
(`_build_audio` L313–338); verified by `tests/test_render.py::test_cards_are_never_trimmed`
and the real-render `test_rendered_cards_have_no_music`. This path has **no concept of a filmed
interview** — a job rendered through it would not get R1/R2. All five booked packages route
through the scene pipeline (`api/tasks.py:_dispatch_processing`, `Package.uses_scene_pipeline`),
so this only matters for the legacy camera-pull job.

**Edge cases (traced):**
- Short clips: a scene shorter than 0.05 s yields no bookend; a 2 s accidental first recording
  IS the bookend (first file of the scene) and the real interview becomes the "walk-out" file —
  identification is by file order, not content. Risk, not a defect: the classifier cannot tell
  an accidental clip from an interview.
- Missing clips: no `intro_interview`/`outro_interview` scene → no bookend inserted, nothing
  invented (`test_a_missing_outro_is_not_invented`).
- Duplicate clips: the range is never duplicated (`test_the_bookend_is_never_duplicated…`).
- Replay / tweak: `replay_selfie` / `replay_ultimum` re-render the persisted EDL **without
  re-validating** — but they re-derive the bookends from the manifest and pass them to the
  renderer, so music is gated on replay too; an EDL composed *before* the rule keeps its
  trimmed interview until the job is re-processed (CLAUDE.md states this).
- Instructor exclusions (`jobs/<id>/exclude.json`, `api/selfie.py:apply_exclusions` L3896):
  for `full_video`/`highlights` an excluded range **splits** any clip, including a bookend or
  the freefall run — this is applied *after* validation at render time. Nothing in the repo
  writes `exclude.json` (grep: only `source_usage.py` and the diagnostic read it), so it is a
  hand-edit path today.
- `POST /jobs/{id}/tweak` (`api/app.py` L1804–1829) saves the instructor's EDL verbatim with
  **no `validate_and_repair`** and re-renders via `rerender_job` → `render_edl` (single-master
  path), which fails for scene-pipeline jobs that have no `source_path`. So the review-UI tweak
  cannot currently re-cut a package job; it also cannot be used to bypass the rules.
- **Deliverable replacement:** `POST /jobs/{id}/deliverables/{name}/replace` (`api/app.py`
  L1922+) lets SkydiveOS swap a delivered video for an operator's manual re-edit. The pipeline
  does not inspect those bytes — whether the SkydiveOS editor honours R1/R2 is a SkydiveOS
  question (see the SkydiveOS editor findings).

### R2 — Freefall never cut in the middle; shortened at the head only so the exit lands on the beat drop

**Freefall identification (VERIFIED):** scene `freefall` from the accelerometer/GPS classifier
(above). Inside the scene: `exit_offset` via `api/selfie.py:detect_scene_exit_offset` L447
(searches every file of the scene; tumbling-exit fallback `_tumbling_exit`, commit c34ae33),
`deploy_offset` via `detect_deploy_offset` L555 (opening-shock threshold 1.6 g; returns `0.0`
when not found). The aerial window is `[exit − lead, deploy + 3 s]` (`_DEPLOY_TAIL_S`,
`edl/validate.py` L57; `_camera_ff_windows` L889–915).

**Continuity (VERIFIED):** `_require_continuous_freefall` (`edl/validate.py` L929–1134) replaces
every freefall-scene clip with ONE run over the window: the editor's slow-mo beats stay at
their own seconds, every gap is filled at 1×, a beat that would replay already-shown seconds is
dropped and logged. Applies to `full_video`, `freefall`, `external_freefall`,
`chute_libre_selfie` (`CONTINUOUS_FREEFALL_DELIVERABLES` L40). It runs after dedupe /
switch-rate / min-shot and before the bookends (L1337–1345). **`highlights` is deliberately
excluded** (short beats only) → client decision.
- Real-job evidence: `jobs/08142ed6…` freefall EDL = 7 contiguous clips `[64.09 → 122.71]`,
  each starting where the previous ended (diagnostic run below); `jobs/a34afaf2…`
  `chute_libre_selfie` = 17 contiguous clips `[5.04 → 63.06]`, `external_freefall` = 5 clips
  `[26.06 → 91.09]`.
- Note the window's **tail is also cut** (everything after deploy + 3 s is dropped, and the
  `freefall` scene file often runs on under canopy). The client's wording "may only be
  shortened at the beginning" is read by the implementation as "the *aerial* part is never cut;
  the canopy tail after the opening is not freefall". → client confirmation of the end point.
- **Slow-mo inside the run**: a 0.4× beat is a speed change, not a cut, and the renderer
  hard-concats; but it does change the *pace* of the uncut run. → client confirmation that
  slow-mo beats are acceptable inside "uncut" freefall.

**Head-only shortening (VERIFIED):** the only shortening applied to a freefall-only cut is the
lead before the exit: `_clamp_freefall_window` (L312) with `lead = max(0, freefall_lead)` for
`FREEFALL_DELIVERABLES` only (`validate_and_repair` L1294–1298). `exclude_freefall_head`
(`api/selfie.py` L3932) refuses mid-jump or tail exclusions with a warning. `full_video` keeps
lead 0 (starts exactly at the exit; no drop sync — see below).

**Beat-drop alignment (VERIFIED, incl. a real render test):** `render/beatdrop.py`:
- The drop per track = sidecar `<track>.json` (`{"beat_drop": 31.2}` pins; `null` = don't
  sync) else detection (`find_beat_drop` L147–215: low-band + full-band energy rise over 4 s
  windows, earliest rise ≥ 75 % of the biggest within the first 120 s, snapped to the sharpest
  bass onset; fade-ins from silence excluded). Cached per (path, size, mtime); never raises.
  **No sidecars ship today** — all four `templates/music` tracks are detected, not pinned.
- Compose: `_freefall_lead` (`api/selfie.py` L2378) = `plan_exit_lead(drop, exit_offset, cap)`
  = `min(drop, footage before the exit, $FREEFALL_MAX_LEAD_S=15)`; passed as `freefall_lead`
  to the validator for the freefall cut only (`compose_edls` L1731–1740; Ultimate per-camera
  cuts L3653).
- Render: `_freefall_music_offset` (L2417) = `drop − (output time at which the exit plays)`:
  `>0` enters the track mid-way (`atrim`), `<0` delays it (`adelay`) (`render_selfie_video`
  L2576–2590). Because it is re-derived from the EDL actually rendered, a hand-trimmed head or
  an `exclude.json` head trim still lands the exit on the drop.
- Tests (all passing with FFmpeg on PATH): `tests/test_beatdrop.py` — detection on synthetic
  builds, sidecar precedence, cap from env, `test_real_render_lands_the_drop_on_the_exit`
  (real FFmpeg render, measures the first bass onset at the exit ±0.15 s),
  `test_every_shipped_track_has_a_drop_to_sync_to` (all 4 library tracks),
  `test_validator_keeps_the_lead_and_fills_the_jump_whole`.
- Real-job evidence (`scripts/diagnose_freefall_sync.py 08142ed6…`, read-only): track
  "Free Fallin'" drop 9.98 s, planned lead 9.98 s (cap 15 s), detected exit plays at output
  9.98 s, music offset 0.00 → "OK: the cut leads the detected exit within the cap and the drop
  lands on it." (Whether the *detected* exit is the *visible* exit is a detector question — the
  diagnostic's `--exit-at` compares against the real exit as seen in the video; not run here
  because no human observation of that video exists.)

**Failure modes (traced):**
- Footage longer than the music: the track is looped (`-stream_loop -1`), so it never runs out
  (`test_real_render…` asserts audio after the track's end). Shorter: trimmed to the edit.
- No drop / no music / sidecar null / analysis error: lead 0 and offset 0 — cut starts at the
  exit, music from 0 (pre-rule behaviour), logged.
- Drop later than the cap (e.g. `sample2.mp3` ≈ 28 s): lead capped at 15 s, music enters
  mid-track so the exit still hits the drop; the first 13 s of the song are never heard.
- Exit not detected: `exit_offset` stays 0.0 (the same value as "starts in freefall"); the cut
  starts at scene start, no lead, music from 0; the manifest `flagged` gets
  `EXIT_NOT_DETECTED_FLAG` and a WARNING is logged. The gallery is **not** held — the flag is
  for instructor review only.
- Deploy not detected: `deploy_offset = 0.0` is now treated as a sentinel (`_deploy_detected`,
  **uncommitted** change dated 2026-10-24): window runs to the scene end, no deploy beat, log
  line "deployment not detected". Before this change a real External job's freefall collapsed to
  2 s (CLAUDE.md). This fix exists only in the working tree.
- Repeated processing: validator, detector and drop detection are deterministic and cached;
  the Claude compose is not, but the validator rebuilds the run and the lead from the manifest,
  so the trim point is the same on re-processing. A *replay* reuses the persisted EDL.

### R3 — Continuous freefall for all packages; Ultimate alternates the two cameras

**Package identification in the pipeline (VERIFIED):** `api/jobs.py:Package` L108 — `selfie`,
`external`, `video_only`, `photo_only`, `ultimum`; `Job.package` defaults to `selfie` L492.
Dispatch: `api/tasks.py:_dispatch_processing` L1260+ → `process_selfie_package` for every
scene-pipeline package, which calls `run_ultimum_pipeline` for `ultimum`
(`api/tasks.py` L1195) and `run_media_ref_pipeline` per camera for a mixed `media_refs` job
(L1175). The SkydiveOS-side mapping to these values is in the SkydiveOS findings.

**Non-Ultimate packages never switch angle (VERIFIED):** single-camera packages have one
scene set and no `camera` tag; `multicam = any(c.get("camera") …)` (`validate_and_repair`
L1303) is False, so the alternation branch is never entered. A mixed-ref job renders
**each camera separately** through `compose_edls`/`render_outputs` (`run_media_ref_pipeline`
L3122+), never merging — by design, because a merged clip cannot be half-locked.

**Ultimate (VERIFIED):** `run_ultimum_pipeline` (`api/selfie.py` L3544–3681):
- Each camera is classified + scored into its own scene set (`_build_role_scene_set` L3405).
- `compose_combo_edls` builds a house cut per camera and `_merge_multicam` (L3462) interleaves
  them scene by scene (round-robin by clip, tagged `camera`). The validator then:
  `_align_cross_camera` (orders mixed runs by **exit-anchored** time, logs >4 s jumps),
  `_limit_switch_rate` (≥3 s between switches), `_enforce_min_shot` (≥1.5 s, slow-mo exempt),
  and finally `_require_continuous_freefall(multicam=True)` which rebuilds the freefall as one
  continuous run on the **instructor's** exit-anchored window, holding each angle
  `_MULTICAM_FF_SHOT_S = 5.0 s` (L68) before switching, each camera only inside its own aerial
  window, the other angle covering where one has no footage, and a gap logged (never invented)
  where neither has footage (L1004–1060).
- **Cross-camera synchronisation is by each camera's detected `exit_offset` only** — no clock,
  GPS-time or audio alignment. If one camera's exit is detected N s off, every switch jumps N s.
  `_ANCHOR_TOL_S = 4 s` only *logs* a jump >4 s at a switch (L797–803); it does not repair it,
  and the continuity rule does not check it.
- Total freefall duration: the combo run spans the instructor's window `[exit, deploy+3]` once
  (time never jumps), so alternation does not change the freefall's duration; footage the
  external camera has *beyond* the instructor's window is not shown.
- One camera missing / short: the camera with footage covers the rest
  (`test_ultimate_combo_falls_back_to_the_camera_that_has_footage`). A missing camera *card*
  is a different matter: `ultimum_watchdog_job` fails the job after `ULTIMUM_SECOND_CAMERA_TIMEOUT_S`
  (1 h) rather than rendering single-camera.
- Per-camera freefall cuts (`external_freefall`, `chute_libre_selfie`) are single-camera and
  get the full R2 treatment including the beat-drop lead per deliverable track (L3653–3663).
- Bookends and the combo: `bookend_ranges(…, multicam=True)` picks one camera; music gating
  identical (L3633).
- Tests: `tests/test_freefall_uncut.py::test_ultimate_combo_freefall_is_continuous_and_alternates`
  (real fixture `ultimum_0823a77a`: both angles used, ≥4 switches, continuous on the anchored
  timeline, each camera inside its own window, ≥3 s per angle, idempotent),
  `…falls_back_to_the_camera_that_has_footage`, `…highlights_are_not_rebuilt`.
- Real-job evidence (`jobs/a34afaf2…/edl_full.json`): 18 freefall clips, instructor
  `[15.02 → 63.06]` interleaved with external `[40.00 → 81.08]`; adjacent clips line up on the
  anchored timeline (e.g. instructor 16.02–18.98 → external 40.00–43.50 = anchored 0.96–3.96
  → 3.96–7.46 s); `validation_report.json`: "freefall plays whole: [15.02, 63.06] as 18
  continuous clip(s), alternating instructor / external".
- **Invented parameters to put to the client:** the 5 s hold per angle, the 3 s minimum between
  switches, the 1.5 s minimum shot, and "the instructor's window is the timeline" are
  implementation choices, not confirmed client rules.
