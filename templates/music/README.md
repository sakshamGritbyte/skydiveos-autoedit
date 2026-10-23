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

## Beat drop (freefall sync)

The freefall cut starts a few seconds before the aircraft exit so the exit lands on
the track's **beat drop** (`render/beatdrop.py`). The drop is detected automatically.
To pin it by hand, put a sidecar next to the track with the same stem:

```json
// "Free Fallin'.json" beside "Free Fallin'.mp3"
{"beat_drop": 10.0}
```

`{"beat_drop": null}` means "this track has no drop": the freefall cut then starts at
the exit and the music plays from its start. Use it for songs that only build gradually.

The cut is aligned by trimming its **head** only — the music keeps its own timing — so
the lead-in before the exit is the track's build-up, as far as the footage before the
exit allows, capped by `FREEFALL_MAX_LEAD_S` (default 15 s: at most 15 s of plane before
the exit). A track whose drop comes later than the cap still lands its drop on the exit,
but the renderer then skips the start of the song (`sample2.mp3` drops at 28.4 s, so its
cut enters the track 13.4 s in). Every track here must yield a drop — detected or pinned —
or the freefall cut quietly starts at the exit with the music from 0
(`pytest tests/test_beatdrop.py -k shipped_track` checks the real files).
A per-job uploaded track (`jobs/<id>/music/<deliverable>.<ext>`) takes a sidecar the
same way (`jobs/<id>/music/<deliverable>.json`).
