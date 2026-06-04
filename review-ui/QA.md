# review-ui — Manual QA

Covers all 5 requirements of the instructor review screen, plus edge cases and
error states. Each case has **steps** and **expected** results. Tick the box when
it passes.

The component reads/writes through the real `/api` backend (preview, EDL, the
three actions). The reorder/trim/slow-mo/delete edits are pure client-side until
you press **Re-render**.

---

## 0. Setup

```bash
# 1. backend on :8000  (eager = no Redis/worker needed)
CELERY_TASK_ALWAYS_EAGER=1 uvicorn api.app:app --port 8000

# 2. seed a reviewable job (real EDL + stub final.mp4 → ready_for_review)
python scripts/seed_review_job.py
#   → prints  job_id=<ID>  and the open URL

# 3. the UI (Vite proxies /jobs/* → :8000)
cd review-ui && npm install && npm run dev

# 4. open the printed URL
#   http://localhost:5173/?job=<ID>
```

**Reset between destructive tests** (after delete / re-render):

```bash
python scripts/seed_review_job.py --job-id <ID>   # then reload the browser
```

> ⚠️ **Stub preview:** the seeded `final.mp4` is a placeholder, so the video shows
> native controls but no real footage. That's expected — it does **not** affect any
> timeline/EDL behaviour. For real frames, run a real jump through render and open
> that job.

**Keep DevTools open** (Network + Console) — Req 5 is verified by inspecting the
outgoing `POST /jobs/{id}/tweak`.

The seed EDL has **5 clips**: `[0]` exit @132s slow-mo 0.4×, `[1]` @141.5s, `[2]`
@150s, `[3]` @158s slow-mo 0.4×, `[4]` canopy @305s.

---

## 1. Preview MP4 with native HTML5 controls

- [ ] **1.1 Player renders** — A 16:9 video sits at the top with the browser's
  **native** controls (play/pause, scrubber, time, volume, fullscreen). Not custom
  buttons.
- [ ] **1.2 Source is the job's preview** — In Network, the `<video>` issues a
  request to `/jobs/<ID>/preview` that returns **`206 Partial Content`**
  (`content-type: video/mp4`).
- [ ] **1.3 Controls are live** — Play/scrub/fullscreen/volume all respond (frames
  are blank with the stub render — controls still work).
- [ ] **1.4 Header metadata** — Above the player: customer name (“Jane Doe”), jump
  date, `♫ sunrise`, and the job id.

## 2. Timeline view of the EDL

- [ ] **2.1 One block per clip** — Below the video, **5 blocks** left→right in EDL
  order.
- [ ] **2.2 Each block shows src_start, duration, speed** — e.g. clip 0 shows
  `@ 132.0s`, `6.0s src`, a speed badge `0.4× slo-mo`, and `→ 15.0s` (output after
  the ramp). A 1.0× clip shows just `1×` and `→` equal to its source length.
- [ ] **2.3 Block width ∝ source length** — Longer clips are visibly wider (clip 1,
  7.5s, is wider than clip 4, 4.0s).
- [ ] **2.4 Slow-mo blocks are tinted** — The two 0.4× clips (0 and 3) have an amber
  tint; 1.0× clips are neutral.
- [ ] **2.5 Summary line** — Header reads `5 clips · footage runtime XX.Xs`, where
  the runtime is the sum of the `→` output durations.

## 3. Instructor edits

### 3a. Drag clip edges to trim
- [ ] **3a.1 Trim in-point** — Drag clip 1's **left** edge to the right: `@` start
  rises, `src` shrinks, block narrows. Release.
- [ ] **3a.2 Trim out-point** — Drag clip 1's **right** edge left: `src` shrinks
  from the other side.
- [ ] **3a.3 Min-width clamp** — Keep dragging an edge inward — the clip refuses to
  shrink below **0.5s src** and never inverts (start always < end).
- [ ] **3a.4 Zero floor** — Drag clip 0's left edge fully left — `@` start stops at
  `0.0s`, never negative.
- [ ] **3a.5 Output reflects speed** — Trimming a 0.4× clip: the `→` output value
  changes by ~2.5× the src change (output = src ÷ 0.4).

### 3b. Click a clip to toggle slow-mo
- [ ] **3b.1 Normal → slow-mo** — Click the **body** of a 1.0× clip (e.g. clip 2):
  badge flips to `0.4× slo-mo`, block tints amber, `→` output ~2.5×s longer, footer
  runtime total increases.
- [ ] **3b.2 Slow-mo → normal** — Click clip 0 (already 0.4×): badge → `1×`, amber
  tint clears, output shrinks.
- [ ] **3b.3 Click target** — Clicking the body toggles; clicking the ✕ or the ⠿
  grip does **not** toggle slow-mo.

### 3c. Delete a clip
- [ ] **3c.1 Delete** — Click ✕ on clip 2: it disappears, count drops to `4 clips`,
  remaining order preserved.
- [ ] **3c.2 Keep non-empty** — Delete down to a single clip, then click its ✕ —
  **nothing happens** (the last clip can't be deleted; EDL must stay non-empty).

### 3d. Reorder via drag
- [ ] **3d.1 Reorder** — Drag the **⠿ grip** of clip 4 and drop it onto clip 0: the
  canopy clip moves to the front; others shift right.
- [ ] **3d.2 Drop highlight** — While dragging over a block, that block shows a ring
  outline; the dragged block dims.
- [ ] **3d.3 Drop-on-self is a no-op** — Drag a grip and drop it back on itself —
  order unchanged.

## 4. Three buttons

- [ ] **4.1 All present (shadcn/ui)** — **Approve & Send** (solid primary),
  **Re-render with my edits** (secondary/grey), **Reject** (red/destructive).
- [ ] **4.2 Initial state (no edits)** — Approve **enabled**, Re-render **disabled**
  (nothing to render), Reject **enabled**.
- [ ] **4.3 After any edit (dirty)** — Make one edit (trim/slow-mo/delete/reorder):
  Re-render becomes **enabled**, Approve becomes **disabled** (hover shows
  “Re-render your edits before approving”), and a note appears: *“Unsaved edits —
  re-render to apply them.”*
- [ ] **4.4 Approve (clean)** — With no pending edits, click **Approve & Send** →
  button shows “Sending…”, `POST /jobs/<ID>/approve` fires (Network), screen reloads
  on success. *(With a real job this delivers; the stub may surface a backend error
  banner — the request itself is the UI's job.)*
- [ ] **4.5 Reject** — Click **Reject** → a prompt asks for a reason. Enter text →
  `POST /jobs/<ID>/reject` with `{reason}` fires.
- [ ] **4.6 Reject requires a reason** — Click **Reject**, leave the prompt empty or
  Cancel → **no request** is sent, nothing changes.

## 5. Re-render POSTs the modified EDL to /jobs/{id}/tweak

- [ ] **5.1 Make edits** — Trim a clip, toggle a clip's slow-mo, delete one,
  reorder one. Optionally type into the **note** field.
- [ ] **5.2 Re-render** — Click **Re-render with my edits** → button shows
  “Re-rendering…”.
- [ ] **5.3 Correct request** — In Network, confirm **`POST /jobs/<ID>/tweak`** with
  a JSON body shaped:
  ```json
  { "edl": { "version": "1.0", "clips": [ … ], "music": "sunrise", "notes": … }, "note": "…" }
  ```
- [ ] **5.4 Payload matches the screen** — The `clips` array reflects **exactly**
  your edits: trimmed `src_start`/`src_end`, toggled `speed_multiplier` (0.4/1.0),
  deleted clip absent, new order applied. `version` and `music` are preserved from
  the original EDL.
- [ ] **5.5 Persisted** — Re-seed not needed to check: `curl
  localhost:8000/jobs/<ID>/edl` now returns your edited EDL (the backend saved it).
- [ ] **5.6 Note forwarded** — If you typed a note, it appears as the top-level
  `note` field (used as a training signal); empty note → `null`.

---

## Edge cases & robustness

- [ ] **E1 Multiple edits accumulate** — Several trims + toggles + a reorder before
  re-rendering → the single POST contains all of them.
- [ ] **E2 Re-render then edit again** — After a successful re-render, the screen
  stays put; make a new edit → Re-render re-enables, Approve disables again.
- [ ] **E3 Toggle is idempotent pair** — Toggle a clip's slow-mo twice → returns to
  the original speed; if that was your only change, Re-render disables again (no net
  edit → not dirty).
- [ ] **E4 Trim precision** — Values display to 0.1s; dragging produces fractional
  seconds (timestamps are floats, not frames — per project convention).
- [ ] **E5 Keyboard/a11y** — Trim handles expose `role="slider"` with
  `aria-valuenow`; ✕ and grip have aria-labels (inspect in the a11y tree).

## Error / empty states (loader)

- [ ] **ER1 No job param** — Open `http://localhost:5173/` (no `?job=`) → “No job
  selected” help text, no crash.
- [ ] **ER2 Unknown job** — Open `?job=does-not-exist` → “Job or its EDL not found —
  has it finished composing?”
- [ ] **ER3 Not yet composed** — Create a job but don't seed its EDL, open it → same
  404 message (the `/edl` endpoint 404s until Compose runs).
- [ ] **ER4 Backend down** — Stop uvicorn, reload → “Could not reach the API. Is the
  backend running on :8000?”
- [ ] **ER5 Action failure surfaces** — If an action returns non-2xx, a red
  `role="alert"` message appears and buttons re-enable (no stuck “…ing” state).

## Known limitations (not bugs)

- **Native HTML5 drag-and-drop** powers reorder → works with a mouse/trackpad;
  touch-only devices won't reorder (documented; would need a pointer-based DnD lib).
- **Stub preview** shows no real frames (QA fixture, not a real render).
- **Approve/Reject mutate real job state** — re-seed with `--job-id` to repeat.
```
