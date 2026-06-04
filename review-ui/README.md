# review-ui — Instructor Review Screen

React + TypeScript components for pipeline **stage 6 (Review)**: the instructor
watches the rendered preview, optionally edits the EDL, then approves, re-renders,
or rejects. Styled with **Tailwind**; buttons use **shadcn/ui**.

Talks to the real `/api` service (`api/app.py`) over its existing contract plus
the read-side `GET /jobs/{id}/edl` endpoint (the timeline source).

## Layout

```
src/
  types.ts                 EDL / Job / Tweak types — mirror the Python contracts
  api.ts                   ReviewApi: getJob / getEdl / approve / reject / tweak / previewUrl
  EdlReviewScreen.tsx      top-level screen (preview + timeline + actions)
  main.tsx                 dev harness: loads ?job=<id> from the live backend
  components/
    Timeline.tsx           clip layout + reorder/trim/slow-mo/delete state
    ClipBlock.tsx          one draggable, trimmable clip block
    ui/button.tsx          shadcn/ui Button
  lib/utils.ts             shadcn `cn` helper
```

## UI → backend mapping

Every screen action maps 1:1 to an `api/app.py` endpoint (via `ReviewApi`):

| UI                            | Endpoint                              |
| ----------------------------- | ------------------------------------- |
| Load job status (header)      | `GET /jobs/{id}`                      |
| Load timeline                 | `GET /jobs/{id}/edl`                  |
| `<video>` preview             | `GET /jobs/{id}/preview` (range)      |
| **Approve & Send**            | `POST /jobs/{id}/approve`             |
| **Re-render with my edits**   | `POST /jobs/{id}/tweak` `{edl, note}` |
| **Reject**                    | `POST /jobs/{id}/reject` `{reason}`   |

## Running against the backend

```bash
# 1. backend (eager = no broker/worker needed for a single-process demo)
CELERY_TASK_ALWAYS_EAGER=1 uvicorn api.app:app --port 8000

# 2. this UI — Vite proxies /jobs/* to :8000 (see vite.config.ts), so the
#    browser calls the API same-origin: no CORS. Override with VITE_API_TARGET.
npm install && npm run dev

# 3. open a job that has reached ready_for_review:
#    http://localhost:5173/?job=<job_id>
```

## Usage

```tsx
import { EdlReviewScreen, ReviewApi } from "@skydiveos/review-ui";

<EdlReviewScreen
  jobId={job.job_id}
  edl={edl}                                  // the job's saved edl.json
  job={{ customer_name, jump_date, music }}
  api={new ReviewApi(import.meta.env.VITE_API_BASE)}  // omit for same-origin
  onActed={(action, updated) => router.refresh()}
/>
```

## Interactions

| Gesture                         | Effect                                            |
| ------------------------------- | ------------------------------------------------- |
| Drag a clip's left/right edge   | Trim `src_start` / `src_end` (min 0.5s window)    |
| Click a clip body               | Toggle slow-mo (`speed_multiplier` 1.0 ↔ 0.4)     |
| ✕ on a clip                     | Delete it (the edit is kept non-empty)            |
| Drag the ⠿ grip onto another    | Reorder clips                                     |
| **Approve & Send**              | `POST /jobs/{id}/approve` → deliver               |
| **Re-render with my edits**     | `POST /jobs/{id}/tweak` with the edited EDL       |
| **Reject**                      | `POST /jobs/{id}/reject` with a reason            |

Edits stay local until **Re-render** — Approve is disabled while there are
unsaved edits, so the render gate is never bypassed.

## Integration notes

This package assumes a host app that already provides React, Tailwind, and the
shadcn/ui theme tokens (`--primary`, `--destructive`, `--muted`, …). Path alias
`@/*` → `src/*` is configured in `tsconfig.json`; mirror it in your bundler
(Vite `resolve.alias`, etc.). Run `npm run typecheck` to validate against the
contracts.
