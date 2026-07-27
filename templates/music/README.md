# Default music library

Backing tracks the renderer layers onto the video deliverables.

## How a track is chosen for a job (in priority order)

1. A track **uploaded for the job** (`POST /jobs/{id}/music`, stored at
   `jobs/<id>/music/<deliverable>.<ext>`) — wins for that deliverable.
2. The booking's **named** track (`music` / `music_full` / `music_highlights` / …),
   matched here by filename stem (e.g. `"sunrise"` → `sunrise.mp3`).
3. **Random default** — when the booking names no music, one track in this folder is
   picked at random *once* at first processing and **persisted on the job** (its
   `music` field + `booking.json`), so a replay/tweak re-renders with the same track.
   See `api.selfie._ensure_default_music`.

So a customer who doesn't choose music still gets a scored soundtrack instead of a
silent video, and the variety comes from having several tracks here to draw from.

## Setup

Drop **2–4 licensed tracks** in this folder (`.mp3`/`.m4a`/`.aac`/`.wav`/`.flac`/`.ogg`).
They must be cleared for customer delivery. The random default picks from whatever is
here — with a single track it always uses that one, so add a few for real variety.
