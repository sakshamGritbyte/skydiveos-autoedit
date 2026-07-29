# Auto-Edit QA report

## external — PASS
job `40c25f9005fe401dacf760f6d96de5df` · status `delivered` · 10m02s

- ✓ **create** POST /jobs — job 40c25f9005fe401dacf760f6d96de5df
- ✓ **ingest** upload — 8 file(s)
- ✓ **pipeline** job succeeded — delivered in 10m02s
- ✓ **ingest** raw staged — 8 master(s) + 0 proxy(s) in jobs/40c25f9005fe401dacf760f6d96de5df/raw
- ✓ **ingest** LRV proxies present — none uploaded — analysis falls back to the full MP4 (slower)
- ✓ **segment** scenes found — 5: ['intro_interview', 'boarding', 'freefall', 'canopy', 'outro_interview']
- ✓ **segment** milestone scenes — freefall present
- ✓ **segment** exit/deploy offsets — exit=40.04 deploy=108.11 (freefall 68.1s)
- ✓ **segment** landing milestone — no landing rename (canopy accl_z_mean=[-2.954], needs >1.5) — expected unless the touchdown is in frame
- ✓ **segment** flagged for review — none
- ✓ **segment** file_offsets recorded — every scene maps back to its source files
- ✓ **score** face scores — 237 scored second(s) across 5 scene(s)
- ✓ **compose** EDL full_video — 14 clip(s) in edl_full.json
- ✓ **compose** EDL full_video sane — every clip has src_start <= src_end
- ✓ **compose** EDL highlights — 19 clip(s) in edl_highlights.json
- ✓ **compose** EDL highlights sane — every clip has src_start <= src_end
- ✓ **compose** EDL freefall — 11 clip(s) in edl_freefall.json
- ✓ **compose** EDL freefall sane — every clip has src_start <= src_end
- ✓ **validate** validation report — 8 repair(s) across 3 deliverable(s): {"full_video": ["dropped freefall [0.00, 40.04] \u2014 outside freefall window [40.04, 111.11]", "injected deployment beat freefall [107.11, 110.11] @0.4 \u2014 no clip covered [107.11, 110.11]"], "highlights": ["injected deployment beat freefall [107.11, 110.11] @0.4 \u2014 no clip covered [107.11, 110.11]", "injected aircraft entry intro_interview [40.16, 43.16] \u2014 highlights lacked the intr
- ✓ **render** deliverable set — got ['freefall', 'full_video', 'highlights', 'photos']
- ✓ **render** full_video rendered — 488 MB, video 187.466667s, audio 187.46s
- ✓ **render** full_video A/V sync — video/audio differ by 0.01s
- ✓ **render** highlights rendered — 166 MB, video 62.966667s, audio 62.950998s
- ✓ **render** highlights A/V sync — video/audio differ by 0.02s
- ✓ **render** freefall rendered — 74 MB, video 37.966667s, audio 37.96s
- ✓ **render** freefall A/V sync — video/audio differ by 0.01s
- ✓ **photos** photo count — 50 stills (expected 35–60)
- ✓ **photos** photos non-empty — 
- ✓ **photos** photos.zip — 30.5 MB
- ✓ **review** auto-approved (no manual gate) — status=delivered
- ✓ **deliver** delivery links — 5 link(s): ['freefall', 'full_video', 'gallery', 'highlights', 'photos']
- ✓ **deliver** gallery link — https://skydivingoss.s3.amazonaws.com/deliveries/40c25f9005fe401dacf760f6d96de5df/gallery.html?X-Amz-Algorithm=AWS4-HMAC
- ✓ **deliver** link gallery opens — HTTP 206
- ✓ **deliver** link full_video opens — HTTP 206
- ✓ **deliver** link highlights opens — HTTP 206
- ✓ **deliver** link freefall opens — HTTP 206
- ✓ **deliver** link photos opens — HTTP 206
- ✓ **archive** jump folder — 2026-07-28/Shred-QA/Luc-external
- ✓ **archive** raw mirrored — 8 file(s)
- ✓ **archive** edited mirrored — 3 file(s)
- ✓ **archive** photos mirrored — 51 file(s)
- ✓ **archive** manifest complete — keys=['archived_at', 'booking_id', 'camera_id', 'customer', 'customer_email', 'customer_name', 'delivered_at', 'delivery_links', 'edited', 'instructor', 'instructor_id', 'instructor_name', 'job_id', 'jump_date', 'package', 'photos', 'raw', 'status', 'updated_at']
- ✓ **api** GET /deliverables — HTTP 200, lists ['freefall', 'full_video', 'highlights', 'photos']
- ✓ **api** stream full_video — HTTP 206
- ✓ **api** GET /photos — HTTP 200, count=50
