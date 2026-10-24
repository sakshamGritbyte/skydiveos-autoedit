# SD-card cleanup audit — performance, safety, status accuracy

Read-only audit, 2026-10-24. Nothing modified; tests run as-is.
Repos: `A` = `/home/msi/Desktop/Development/skydiving-autoedit/skydiveos-autoedit` (ingest station, Python),
`S` = `/home/msi/Desktop/Development/skydiving/skydiving-os` (Ultimate DZM / SkydiveOS, Node + React).
Tags: **VERIFIED** = traced in code / ran; **INFERRED** = reasoned from code without running or from unavailable production state.

Client requirement: *"After footage has been uploaded and copied, clearing the SD card takes too long. The workflow should be audited for performance, safety, and accurate status reporting."*

---

## (a) The actual cleanup mechanism — VERIFIED

* **Per-file unlink, never a format.** The only deleter in SD-card mode is `SdCardCamera.delete_media` (`A/ingest/sdcard.py:435-452`): `unlink` of the MP4 (fatal on failure, `FileNotFoundError` tolerated as goal state), then best-effort unlink of the `GLxxxxxx.LRV` proxy and the `.THM` thumbnail. The abstract base refuses to delete at all (`A/ingest/camera.py:122-132`, "nothing here wipes a card wholesale"), pinned by `tests/test_retention.py:217 test_camera_base_refuses_to_delete_by_default`.
* **`delete_all_media` is never used**: `grep -rn delete_all_media --include=*.py` over the repo (excluding `.venv`/vendor) returns zero hits.
* **Who calls it:** only `ingest.pull._sweep_card` (`A/ingest/pull.py:127-177`), invoked from `pull_camera` **at the START of a pull** (`pull.py:240-249`), only when `cleanup=True`, and only for the records `ingest.retention.deletable` returns (`A/ingest/retention.py:145-186`).
* **Delete authority = the ledger** `<RAW_STORAGE_ROOT>/_camera-staging/<camera_id>/.transferred.json` (`retention.py:51,80-82`), written by `record_uploaded` (`retention.py:93-123`) which discovery calls only after S3 **and** the SkydiveOS notify both succeeded (`A/ingest/discovery.py:883-893`; markers: `:835-843`). Each record = `{s3_key, at, size}`.
* **Card identity for the ledger** = `card_id_for_mount` (`A/ingest/sdcard.py:264-293`): last 4 digits of the camera serial in `MISC/version.txt`, else `sd-<volume label>` (Windows: `sd-<drive letter>`). So the ledger is keyed by **camera**, not by card (`sdcard.py:28-35`).
* **Deployed switches** (`A/api/config.py:390-397`, `A/api/app.py:636-648 _cleanup_kwargs`):

| Env var | Code default | `.env.example` | Windows runbook (`A/deploy/windows/README.md:85-87`) | This dev box `.env` |
|---|---|---|---|---|
| `DELETE_AFTER_TRANSFER` | **false** (`config.py:392`) | `false` (`.env.example:44`) | `true` | `true` |
| `DELETE_AFTER_TRANSFER_MIN_AGE_H` | **24 h** (`config.py:393-395`) | `0` (`.env.example:50`) | `0` | `0` |
| `DELETE_AFTER_TRANSFER_DRY_RUN` | false | `false` | — | unset |
| `CARD_SAFE_REQUIRES_UPLOAD` | true (`config.py:397`) | `1` | `1` | `1` |
| `DISCOVERY_INTERVAL_SECONDS` | 30 (`config.py:390`) | 30 | 30 | 30 |

The Mac runbook (`A/deploy/mac/install.sh:96`) only sets `CAMERA_SCANNER=ble`; it says nothing about `DELETE_AFTER_TRANSFER` — on a Mac box left at code defaults, cleanup is **off**, and if turned on without `MIN_AGE_H`, it waits **24 h** (INFERRED for production; the actual `.env` on the South Shore Mac / North Shore Windows box is not on this machine).

---

## (b) Step-by-step sequence with timings — VERIFIED unless noted

Actors: `CameraDiscoveryService._scan_loop` / `_materialize_loop` (`A/ingest/discovery.py:653-737`), `_sdcard_pull` (`A/api/app.py:733-781`), `pull_camera` (`A/ingest/pull.py:180-287`), `CardStatusRegistry` (`A/ingest/cardstatus.py`).

| # | Step | Where | State emitted | Time cost |
|---|---|---|---|---|
| 1 | Card inserted; scanner polls mount roots for `DCIM/` | `ingest/scanner.py:153-167`, `sdcard.py:325-360` | `detected` (`ObservingScanner.scan` → `observe`, `cardstatus.py:279-312`) | ≤ `DISCOVERY_INTERVAL_SECONDS` (30 s). Scans are **skipped while any pull is in flight** (`discovery.py:669-671`) |
| 2 | Pull opens: `pull_started` (resets counters; keeps `uploading`/`safe_to_remove` — `cardstatus.py:166-192`), card serial probe, `TrackedCamera` wrap | `app.py:738-756` | `pulling` (new card) | ms |
| 3 | `list_videos`: glob `DCIM/*/G[HX]*.MP4` + `stat` each | `sdcard.py:397-416` → `TrackedCamera.list_videos` → `totals()` | — | ms–s |
| 4 | **Sweep** (`cleanup=True`): `deletable(ledger, {name: on-card size}, min_age_s)` → per file `delete_media` (unlink MP4, LRV, THM) | `pull.py:240-249`, `127-177`; `retention.py:145-186` | `sweeping` **only after each successful delete** (`cardstatus.py:399-402`) | ms per file on vfat/exFAT. On a FRESH card the ledger has no matching record → nothing deleted |
| 5 | Copy loop: per MP4 `shutil.copyfile` (+LRV, +THM best-effort), manifest written last; already-staged clips skipped via `is_complete` | `pull.py:58-124`, `ingest/storage.py:87-89`; `sdcard.py:418-433` | `pulling`, `file_started/file_done` (bytes = on-card `st_size`) | reader-bound; events are **held** (QR mode, `_HoldingEventEmitter`, `discovery.py:537-553,699-714`) until the whole card is staged |
| 6 | End of pull: `_report_card_state` re-lists the card and asks `card_transfer` | `app.py:651-701`; `retention.py:228-273` | **`uploading`** with `pending_files = len(unconfirmed ∪ awaiting_sweep)` — on a fresh card this is ALL clips, because nothing has been uploaded yet | ms |
| 7 | Held events flushed to the queue; `_materialize_loop` processes **one clip at a time** in a thread: QR session index (OpenCV decode of clips ≤ 60 s, 8 s sampled, cached in `<stem>.qr.json`), `ffprobe` capture time (**twice**: `qr.py:482` and `discovery.py:420`), Mongo registry lookups (`instructor_for`, `role_for`), `resolve_for_staff` (Mongo), S3 `head_object` (collision check), S3 `upload_file`, SkydiveOS `POST /api/media/raw-upload` (30 s timeout), then `record_uploaded` rewrites the whole ledger JSON | `discovery.py:719-737, 791-893`; `ingest/qr.py:449-500, 240-283` | still `uploading` (nothing updates the registry during upload; `pending_files` only changes at the next pull) | **Dominant cost: uplink bandwidth, serialized per clip.** Measured on this dev box's ledger (VERIFIED, `raw-storage/_camera-staging/sd-disk/.transferred.json`): 288 MB → 70 MB → 22 MB confirmed at 09:59:29 / 09:59:47 / 10:00:04 (~17 s each regardless of size ⇒ ~15 s fixed per-clip overhead); 917 MB → 65 MB → 349 MB at 12:11:38 / 12:12:00 / 12:13:24 (≈ 4 MB/s ≈ 33 Mbit/s effective) |
| 8 | Queue drained after a SUCCESSFUL hand-off → `_rescan.set()` → scan loop wakes **immediately** | `discovery.py:730-737, 897-908`; wired `app.py:982` `rescan_after_handoff=(camera_scanner=="sdcard")` | — | **0 s since commit `67408a8` (2026-10-22)**; before it: up to 30 s |
| 9 | Re-pull: `pull_started` keeps `uploading`; `list_videos`; **sweep now finds every confirmed file** (if `min_age` elapsed) and unlinks them; copy loop skips everything | steps 2-5 | `sweeping` per deleted file | seconds |
| 10 | `_report_card_state`: card re-listed, now empty/settled → `safe_to_remove` | `app.py:693-695`, `retention.py:222-225` | **`safe_to_remove`** | ms |
| 11 | Publisher pushes snapshot every **2 s** while any card is tracked, plus one final empty push | `discovery.py:445-514` (`CARD_STATUS_INTERVAL = 2.0`, `:67`) | — | ≤ 2 s |
| 12 | SkydiveOS stores it (`recordIngestCards`, TTL **10 s** `AUTO_EDIT_CARD_STATUS_TTL_MS`, `S/backend/src/config/index.js:101`); frontend polls **2 s** while any card is active (`uploading` counted active since `dea314c`), 10 s idle | `S/backend/src/services/autoEditService.js:102-107, 127-156`; `S/frontend/src/hooks/useIngestCards.js:18-31` | banner + toast | ≤ ~4 s to screen |

**Where the time actually goes (ranked):**

1. **Step 7 — the S3 upload of every clip must complete before anything can be deleted** (by design: the ledger is the only delete authority). The card sits in `uploading` for the whole upload. A multi-GB card over a dropzone uplink is tens of minutes; per-clip fixed overhead ≈ 15 s (double `ffprobe`, Mongo, `head_object`, notify). Uploads are strictly sequential (`await asyncio.to_thread(self._materialize, event)`, `discovery.py:726`).
2. **Step 8 — the sweep can only run inside a pull** (`pull.py:240`), i.e. on the next discovery tick. Until `67408a8` (2026-10-22) that was up to 30 s of dead wait; now immediate. This deferral is an artefact of the wireless design ("the camera is closed by the time S3 confirms", `retention.py:8-20`); for an inserted card the mount is never "closed", so the sweep *could* run the moment each clip is confirmed — it doesn't.
3. **`DELETE_AFTER_TRANSFER_MIN_AGE_H` left at the code default (24 h)**: files inside the grace are *not* counted as `awaiting_sweep` (`retention.py:244-246, 261-267`, test `test_grace_period_is_not_something_to_wait_for`), so the banner goes **green while the card is still full**, and the clips are only deleted if the card is re-inserted ≥ 24 h later. `.env.example:46-50` documents exactly this trap. INFERRED as a candidate cause on any box whose `.env` omitted the variable.
4. **A clip whose notify is permanently rejected (HTTP 4xx, e.g. SkydiveOS 404 for an unpaired staff / missing `goproSerial`)** is never retried (`discovery.py:750-765`, `_is_permanent_handoff_error :75-91`) and never recorded → the card reads `uploading — N clips left` **forever** (see (h)-3).
5. **A clip whose delete fails** (write-protect switch, EROFS/EPERM, vfat error) is logged and skipped (`pull.py:169-173`) but stays in `awaiting_sweep` → same forever-`uploading` banner; the sweep retries every tick.
6. UI latency: push 2 s + TTL/poll 2 s — minor (≤ 4 s). Before `dea314c` (2026-10-22) the `uploading` phase polled at 10 s and rendered as "Card detected — preparing… 0%".

---

## (c) Verification safeguards — VERIFIED

**The signal that permits deletion** is the conjunction (`retention.py:152-186`):
1. the file is on the card right now (`name in sizes`);
2. the ledger has a record for that name, whose `s3_key` was returned only after `upload_file` succeeded **and** `POST /raw-upload` answered 2xx (`discovery.py:396-397` markers, `:403, 432-440` clips; `record_uploaded` at `:837, 888`);
3. `record.size == on-card st_size` (`TransferRecord.matches`, `retention.py:66-77`); a record without a size (pre-field) is unverifiable and never deletable (`:141-150` tests);
4. `now - record.at ≥ min_age_s`.

**Is there any path that deletes on a weaker signal?** None found:
* `CARD_SAFE_REQUIRES_UPLOAD=0` and the `_report_card_state` exception fallback (`app.py:669-671, 686-692`) only affect the **banner** (they say "safe" early); they delete nothing.
* `DELETE_AFTER_TRANSFER_DRY_RUN` logs the candidate and deletes nothing (`pull.py:162-168`).
* The ledger `size` is the **staged** file's size (`discovery.py:841, 892 _file_size(mp4)`), compared against the **on-card** size at sweep time — so a silently truncated staged copy can never authorise deleting the on-card original (fail-safe, and it doubles as a copy-integrity check).
* `is_complete` (MP4 + manifest both present, `storage.py:87-89`) governs re-copy, not deletion.
* Caveat (low): the **LRV proxy and THM are deleted on the strength of the MP4's S3 key** (`sdcard.py:448-452`); the LRV itself is never uploaded (only `event["files"]["mp4"]` is, `discovery.py:801, 875`). They are derivable proxies and a staged copy exists locally; acceptable but it is a weaker signal for those two sidecars.
* Residual identity weakness (see (h)-4): identity is **filename + size** per `camera_id`, not content. The S3 key path already uses a content fingerprint for collisions (`discovery.py:238-259, 331`); the ledger does not.

No checksum verification of staged vs on-card bytes, and no post-upload `head_object` size compare (the only `head_object` is the pre-upload collision check, `discovery.py:271-277, 394, 401`). boto3's per-part integrity is relied on implicitly (INFERRED).

---

## (d) Failure recovery table — VERIFIED from code paths

| Scenario | What happens | Only copy at risk? | Retryable? |
|---|---|---|---|
| **Partial copy (copyfile raises: EIO, ENOSPC, yank)** | Clip tallied, partial staged file unlinked, no manifest/event; other clips continue; `CameraError` raised after the loop (`pull.py:260-286`) → `card_status.error(...)` (`app.py:768-773`); `_report_card_state` is **skipped** on this path | No — never uploaded, never in ledger, stays on card | Yes: every tick re-pulls (idempotent); the failed clip is retried each tick; good clips' held events are still released (`discovery.py:707-714`). The banner flaps `pulling`→`error` each tick and **never reaches `safe_to_remove`** while one clip is unreadable |
| **Insufficient local disk** | Same as above per clip (ENOSPC inside `copyfile`) — every remaining clip fails too | No | Yes, after freeing disk |
| **Failed S3 upload / network down (5xx, timeout)** | Exception → `_schedule_handoff_retry`: 10 attempts, 10 s × 2^(n−1) capped at 300 s (`discovery.py:739-789`, `_HANDOFF_MAX_DELAY :72`) ≈ 25 min total, then "giving up … delete its jump folder and re-pull" | No — on card + staged; not in ledger | Automatically for ~25 min; afterwards **manual** (delete the staged jump folder so the next pull re-copies and re-emits) |
| **Notify rejected 4xx (not 429)** | Logged once at ERROR, **never retried** (`:750-765`). The S3 object may already exist (upload precedes notify, `:403` vs `:432`), but no key is returned → no ledger record | No | Only manually (fix data, delete jump folder, re-pull). Banner stays `uploading` with a wrong message — see (h)-3 |
| **Process restart / crash between S3 success and ledger write, or with events still queued** | Registry and in-memory queue are lost (`cardstatus.py:124-131`, `discovery.py:603`); the next pull sees the clip as `is_complete` → emits nothing → the clip is **never uploaded / never recorded** | No (still on card + staged) | Only manually (same recipe). Card can never go green on its own — (h)-2 |
| **Ledger write fails (OSError)** | Warning, no record (`retention.py:122-123`) | No | Next pull cannot delete it; stays `uploading` |
| **Card yanked mid-copy** | Per-clip OSError path above; `observe` drops an `uploading` row when the card disappears, with a WARNING that it was not cleared (`cardstatus.py:302-310`); staged clips still upload | No | Re-insert: good clips skipped, confirmed ones swept |
| **Card yanked mid-sweep** | `unlink` OSError → `CameraError` → warning, continue (`pull.py:169-173`); the host mounts vfat with `flush` here (`/proc/self/mountinfo`), limiting FS damage | No (nothing deleted without S3 confirmation) | Yes, next insert |
| **Delete refused (write-protect tab, EROFS, EPERM)** | Warning per file, every tick; file stays in `awaiting_sweep` | No | Retries forever; banner misleading — (h)-3 |
| **Corrupt ledger / missing ledger** | `_read` returns `{}` → nothing deletable (`retention.py:85-90`, tests `test_corrupt_ledger_keeps_everything`, `test_no_ledger_deletes_nothing`) | No | n/a (fail-safe) |
| **Reused filename on a formatted/second card (§3-F)** | Size mismatch → kept + WARNING (`retention.py:178-184`) | No, unless size also collides — (h)-4 | n/a |
| **Verification failure** | There is no explicit verify step; the size check at sweep time is the only one | — | — |

An interrupted cleanup is therefore safely retryable: the sweep is idempotent per file, `FileNotFoundError` is the goal state, and nothing is deleted without a size-matched S3 record. The cost of every failure above is a **stuck card / stuck banner**, never lost footage.

---

## (e) Status pipeline and UI wording — VERIFIED

Emitter: `CardStatusRegistry` states (`cardstatus.py:65-72`): `detected`, `sweeping`, `pulling`, `uploading`, `safe_to_remove`, `error`. Served by `GET /ingest/cards` (`A/api/app.py:32`) and PUSHED to `POST {SKYDIVEOS_API_BASE}/api/media/ingest-cards/status` every 2 s (`discovery.py:445-514`, `CARD_STATUS_URL` override). Receiver: `S/backend/src/routes/mediaRoutes.js:69` → `mediaController.receiveIngestCardStatus` (`S/backend/src/controllers/mediaController.js:603-622`, service-token gated) → `autoEditService.recordIngestCards` (`autoEditService.js:102-107`, wholesale replace, TTL 10 s, falls back to pulling the cloud API which answers `[]`). Read: `GET /api/media/ingest-cards` (`mediaRoutes.js:181`, `aiStaff` = manifest/instructor/admin/owner, `:136`) → `listIngestCards` with `withoutStaleTerminalCards` (15 min, `autoEditService.js:45-57`) → `sdCardService.annotateIngestCards` adds `paired_staff` (`mediaController.js:539-549`). Frontend: `useIngestCards` (2 s / 10 s) → `SdCardIngestIndicator.deriveCardView` (`S/frontend/src/components/media/SdCardIngestIndicator.js:46-92`), mounted **only on `MediaPage`** (`S/frontend/src/pages/app/MediaPage.js:26,313`) — an operator on the manifest/live-board page sees nothing.

**The banner is not internationalised.** Every string in `SdCardIngestIndicator.js` is a hard-coded English literal (no `useTranslation` import; lines 59-60, 66, 70-71, 78, 85, 90, 141, 160, 185). `S/frontend/src/locales/{en,fr}/translation.json` carry no card-banner keys — only the pairing section `staff.sdCards.*` (`en:2122-2129`, `fr:2122-2129`). The 2026-10-22 commit `dea314c` touched both catalogs (+5 lines each) but for `staffCredentials.retiredReasons`, not the banner. So a French operator sees the banner in English. The terminal display `A/scripts/watch_cards.py:43-50` is a separate English table.

| State (pipeline) | When it is set | SkydiveOS banner (en, hard-coded) | fr | Progress source | Accurate? |
|---|---|---|---|---|---|
| `detected` | card seen by scan, pull not started | "Card detected — preparing…" 0 %, DO NOT REMOVE THE CARD | *(same English)* | none | OK (brief) |
| `pulling` | each `download_mp4` start | "Copying n of m clips — GX….MP4" + byte % | *(same English)* | **real**: `bytes_done/bytes_total` from on-card `st_size` (`cardstatus.py:378-391`); can finish < 100 % when clips were already staged (`:40-43`) | OK |
| `sweeping` | **after each successful delete** (`cardstatus.py:399-402`) | since `dea314c`: "Clearing uploaded clips off the card…"; **before 2026-10-22: "Scanning card for clips…"** (`git show dea314c`, test rename at `SdCardIngestIndicator.test.js:59-64`) | *(same English)* | stale bar | **Misleading pre-10-22**: deletion was labelled "scanning". Post-fix it is accurate but lasts milliseconds; the deleting phase is effectively invisible, hidden under `uploading` until the first unlink |
| `uploading` | end of every pull with unconfirmed OR not-yet-swept clips (`app.py:696-701`) | since `dea314c`: "Uploading to the cloud — N clips left, then the card is cleared", no bar, DO NOT REMOVE; **before 10-22 the state was unknown to the UI and fell through to "Card detected — preparing… 0%"** for the whole upload phase (component comment `:11-16`) and polled at 10 s | *(same English)* | `pending_files` = count only; **no byte progress for the upload** | **Partly misleading today**: `pending_files` conflates "not in S3" with "in S3 but not yet deleted" and with "notify rejected" / "delete refused" (`cardstatus.py:109-112`, "ONE number on purpose") — the banner says "Uploading" when the real blocker may be a 404 or a write-protected card |
| `safe_to_remove` | card settled (`retention.py:222-225`) | green row "SD card ingested — safe to remove · N clips ingested" + 15 s toast | *(same English)* | `files_total \|\| files_done` | INFERRED cosmetic bug: after a full sweep `files_total` was decremented to 0 by `sweeping()` and `files_done` is 0 in the re-pull, so the green row reads **"0 clips ingested"** |
| `error` | pull raised (`app.py:771-772`) | amber, upstream message ("camera X: k of N clip(s) failed to download (…)"); DO NOT REMOVE unless the message matches `CARD_GONE_RE` | *(same English)* | — | OK |

Can the UI distinguish the client's phases?
* scanning for clips — no dedicated state (listing happens under `pulling`/`detected`);
* copying — yes (`pulling`, real bytes);
* uploading — yes since 10-22 (`uploading`, count only, no bytes);
* verifying — **no state exists**; verification is the size check inside the sweep;
* copy complete → clearing card — `sweeping`, correct wording since 10-22, but practically never visible;
* cleanup complete — `safe_to_remove` (with the "0 clips" wording issue);
* cleanup failed — **not distinguishable**: renders as "Uploading to the cloud — N clips left";
* copy failed / incomplete — yes (`error`).

---

## (f) Cards 6467 / 4812 on 2026-10-18 — evidence and partial-copy risk

**Evidence search (VERIFIED, all negative on this machine):**
* `A/raw-storage/_camera-staging/` contains only `sd-disk/` (today's dev test card, 27 clips confirmed between 09:58 and 12:13 on 2026-10-24; its `DCIM/100GOPRO/` on `/media/msi/disk` is now **empty**, i.e. the sweep worked end-to-end here). No `*6467*`, `*4812*` directories or ledgers anywhere under `~/Desktop/Development` (bounded `find`).
* `A/jobs/_bridge_state.json`: `handled 0, flagged 0, deferred 0, jumps 1`; no "6467"/"4812"/"2026-10-18".
* No `A/logs/` directory; `diagnostic_output/findings.md`, `qa-report*.{md,json}` (dated 10-21) mention no cards.
* `S` repo: "6467"/"4812" absent; "2026-10-18" appears only in Bug 429/422/423 tests and `CLAUDE.md:2293` ("Oct 18 document item 2" = load weight), i.e. the client's 2026-10-18 report document exists but the SD-card item from it is not quoted in either repo.
* Git: no commit in `A` between 10-16 and 10-21 touches ingest; the SD-card changes are `A 67408a8` (10-22, immediate rescan after hand-off, `rescan_after_handoff`) and `S ac83149`/`dea314c` (10-22, `uploading` state in the UI, 2 s polling for it, "Scanning card for clips…" → "Clearing uploaded clips off the card…"). `S ac83149`'s backend diff is bilingual email / bookends / `mediaService` intro-outro — **nothing SD-card related in the backend**; the "SD-card clear speed" part of that commit pair is UI-only (`dea314c`).
* The ids `6467`/`4812` are 4-digit → they are **camera-serial-derived `camera_id`s** (`sdcard.py:280-284`), so on the ingest box the relevant artefacts are `<RAW_STORAGE_ROOT>/_camera-staging/6467/.transferred.json`, `.../4812/.transferred.json`, the `2026-10-18/` staging folders beside them, and `logs/ingest.err.log`. Those live on the dropzone box, not here — collect them before concluding anything about those two cards.

**What the operator would have seen on 10-18 (INFERRED from git history, assuming both deployments were at HEAD of 10-18):** the pipeline already held the card in `uploading` (`dbee838`, 09-18) but the SkydiveOS UI had no `uploading` case → "Card detected — preparing… 0 %" + "Do not remove the card" for the entire upload (minutes to tens of minutes), polled every 10 s; then up to 30 s idle before the sweep tick; then "Scanning card for clips…" while files were actually being deleted; then green. That matches the complaint word for word, and the 10-22 commits in both repos address exactly those three items — but none of them shortens the upload itself, which is the real floor.

**Could a partially copied / partially uploaded card have been cleared? — No (VERIFIED by trace):**
1. A partially *copied* clip never gets a manifest, so it never emits, never uploads, never enters the ledger (`pull.py:271-278`).
2. A partially *uploaded* clip: `upload_file` raises → no key → no record (`discovery.py:403, 883`).
3. A clip in S3 whose notify failed: no key returned (`:438-440`) → no record.
4. Even with a record, `deletable` requires the on-card size to equal the uploaded size (`retention.py:176-185`).
5. The sweep touches only names listed on the card at that moment and only via `delete_media` per file.

The inverse risk is what the code *does* allow: a card whose clips are **all safely in S3** can be left **uncleared** (grace period, rejected notify, refused delete, restart) while the banner either says "Uploading…" or says green without having deleted anything (24 h grace). If 6467/4812 "took too long", the candidates in order of likelihood are: slow uplink (unavoidable by design), the pre-10-22 UI fall-through + 30 s tick, `MIN_AGE_H` unset (24 h), a 404'd clip, or a failed unlink. The ledger `at` timestamps on the box will separate these in minutes: gaps between consecutive `at` values = upload time per clip; a clip missing from the ledger but present in staging = a rejected/stranded hand-off.

---

## (g) Tests run (read-only) — VERIFIED

Pipeline (`A`, `uv run --no-sync pytest … -q -p no:cacheprovider`, ffmpeg from `~/.local/bin/ffbin`):

| File | Result |
|---|---|
| `tests/test_sdcard.py` | 24 passed |
| `tests/test_sdcard_windows.py` | 13 passed |
| `tests/test_retention.py` | 24 passed (incl. `test_stale_record_never_authorises_a_different_file_of_the_same_name`, `test_a_record_without_a_size_is_unverifiable_and_never_deletable`, `test_sweep_keeps_a_reused_filename`, `test_delete_failure_is_survivable`, `test_camera_base_refuses_to_delete_by_default`, `test_confirmed_but_not_yet_swept_is_not_settled`, `test_grace_period_is_not_something_to_wait_for`) |
| `tests/test_cardstatus.py` | 21 passed, 1 warning |
| `tests/test_discovery.py` | 51 passed, **1 failed**: `test_build_pull_modes` — `RuntimeError: CAMERA_SCANNER=static (simulation) needs a sample MP4 … sample-data/discovery_sample.mp4` (the bundled sample file is absent on this box; environmental, not logic; the `rescan` tests `test_drained_handoff_queue_triggers_an_immediate_rescan` / `test_a_failed_handoff_does_not_trigger_a_rescan` pass) |
| Combined first run | `1 failed, 133 passed, 1 warning in 5.48s` |

SkydiveOS (`S`): `cd backend && npm test -- --silent src/__tests__/ingestCardsProxy.test.js src/__tests__/sdCardPairing.test.js` → **Test Suites: 2 passed, 2 total; Tests: 44 passed, 44 total**. `cd frontend && CI=true npx craco test --watchAll=false --silent src/__tests__/SdCardIngestIndicator.test.js src/__tests__/SdCardsSection.test.js` → **Test Suites: 2 passed, 2 total; Tests: 23 passed, 23 total**. (A first attempt with bare `npx jest` failed on ESM/Babel config — runner issue, not test failures; reported counts are from the packages' own runners.) There is no backend test named for `recordIngestCards`'s TTL beyond `ingestCardsProxy`; `sdCardAutomationPurchaseMatrix.test.js` and `ultimumSdCardFourVideos.test.js` exist but are about purchase routing, not the banner, and were not run.

---

## (h) Gaps and defects, by severity

P0 = could lose the only copy of footage. **No P0 found**: every deletion path requires a size-matched S3+notify-confirmed ledger record for a file currently on the card.

| # | Sev | Finding | Evidence |
|---|---|---|---|
| 1 | **P1** (perf, the client's complaint) | The sweep can only run inside the NEXT pull, and only after EVERY clip's upload+notify has succeeded. For an inserted card the mount is always available, so each clip could be unlinked the moment its own ledger record lands; instead the card is held for the slowest clip plus a full re-pull. Uploads are strictly serial with ~15 s fixed overhead per clip (double `ffprobe`, Mongo ×2-3, `head_object`, notify, full ledger rewrite). | `pull.py:240-249`; `discovery.py:726, 730-737`; `qr.py:482` + `discovery.py:420`; ledger timestamps |
| 2 | **P1** (stuck, customer impact) | Hand-off state is in-memory only: a restart/crash with clips still queued (or between S3 success and `record_uploaded`) strands them — the next pull sees them `is_complete`, emits nothing, so they are never uploaded, the job is never created and the card never goes green. `retention.py:19-20` claims "crash-safe", which is true of the sweep but not of the hand-off. Manual recovery = delete the staged jump folder and re-pull (`discovery.py:767-771`). | `discovery.py:603, 719-737, 791-893`; `storage.py:87-89` |
| 3 | **P2** (status accuracy) | `pending_files` conflates four different blockers; the banner says "Uploading to the cloud — N clips left, then the card is cleared" when the truth is (a) SkydiveOS rejected the notify with 4xx and it will never retry, (b) the file is in S3 but `unlink` is refused (write-protect / EROFS), (c) still inside a non-zero grace, or (d) genuinely uploading. The operator cannot tell "wait" from "go fix data". No `cleanup_failed` / `rejected` state exists. | `cardstatus.py:109-112, 230-249`; `app.py:696-701`; `discovery.py:750-765`; `pull.py:169-173` |
| 4 | **P1-low** (safety, low likelihood) | Ledger identity is filename + size per `camera_id`, not content. A formatted card (numbering restarts at `GX010001`) whose new chapter file happens to have exactly the same byte size as yesterday's record (chaptered GoPro files are cut at a fixed boundary and sizes can coincide) would be deletable **before** today's file is uploaded, if `MIN_AGE_H` has elapsed. The S3 key path already uses a 1 MB-head sha256 fingerprint for this exact collision; the ledger does not store/compare it. Also on Windows two unlabelled cards share `sd-<letter>` (one ledger). | `retention.py:66-77, 176-185`; `discovery.py:238-259`; `sdcard.py:274-293`; `AUDIT_MEDIA_MATCH_ISOLATION.md:265-275` (§F) |
| 5 | **P2** (config trap) | Code default `DELETE_AFTER_TRANSFER_MIN_AGE_H=24` while every runbook/example says `0`; with 24 the card goes green uncleared and is only emptied on a re-insert a day later ("clearing takes too long" literally). `_report_card_state` deliberately does not wait for files in grace. The Mac runbook never mentions the three cleanup vars at all. | `config.py:393-395`; `.env.example:46-50`; `retention.py:244-246`; `deploy/mac/install.sh:96` |
| 6 | **P2** (status accuracy, pre-10-22 deployments) | Until `dea314c`/`67408a8` are deployed on BOTH boxes, the operator sees "Card detected — preparing… 0 %" for the whole upload, "Scanning card for clips…" during deletion, 10 s polling and a 30 s dead wait before the sweep. Nothing in either repo records which build the DZ boxes ran on 10-18. | `git show dea314c`; `git show 67408a8` |
| 7 | **P3** (i18n) | The whole banner (incl. "Do not remove the card", the toast) is hard-coded English; no `fr` keys. | `SdCardIngestIndicator.js:59-185`; locales `en/fr:2122-2129` only |
| 8 | **P3** (cosmetic) | After a full sweep the green row reads "0 clips ingested" (INFERRED: `sweeping()` decrements `files_total` per deleted file, `pull_started` zeroes `files_done`). | `cardstatus.py:202-212, 166-192`; `SdCardIngestIndicator.js:78` |
| 9 | **P3** (visibility) | The indicator is mounted only on `MediaPage`; `publish_card_status` goes to `SKYDIVEOS_API_BASE` (cloud) and SkydiveOS's pull fallback reaches the cloud API, so a split deployment with a mis-set `AUTO_EDIT_API_KEY` shows **no banner at all** (401 logged server-side only). | `MediaPage.js:313`; `mediaController.js:603-617` |
| 10 | **P3** (minor safety) | `.LRV`/`.THM` sidecars are unlinked on the MP4's confirmation; the LRV is never uploaded to S3. Local staged copies exist. | `sdcard.py:448-452`; `discovery.py:801` |
| 11 | **P3** (perf) | No upload progress callback (`upload_file` without `Callback`), so no byte-level feedback during the longest phase; `record_uploaded` rewrites the entire ledger JSON per clip. | `discovery.py:396, 403`; `retention.py:112-121` |

---

## (i) Client clarifications needed

1. **Auto-clear vs staff confirmation.** Today deletion is fully automatic once `DELETE_AFTER_TRANSFER=true` (env flag, no UI gate, no per-card opt-out; only a dry-run flag). Is a confirmation step on the banner wanted ("Clear card now"), or is unattended clearing the requirement? If unattended, is the 0 h grace the operator's intent (the code default is 24 h)?
2. **Whole-card format vs preserve files.** The current design is per-file unlink of confirmed clips only, keeping unknown/unconfirmed files (and leaving the card's `MISC/`, folder structure and any non-GoPro files untouched). Would a whole-card format after a fully confirmed upload be acceptable (faster, but forfeits the "unknown file is never deleted" guarantee and makes a partially confirmed card un-formattable), or must unconfirmed files always be preserved?
3. **Where the time limit lies.** Clearing cannot start before the slowest clip is in S3; is the expectation (a) "the card is cleared immediately after the copy, while uploads continue from the staged copy" (changes the safety rule: the only copy would then be the ingest box's disk until S3 confirms), or (b) "clear each clip as soon as it is in S3, without waiting for the whole card"? (b) keeps the S3 rule and removes the next-pull wait.
4. **Exact status wording.** Proposed distinct states/strings for en + fr: "Scanning card…", "Copying n/m (xx %)", "Uploading n/m to cloud (xx %)", "Verifying", "Clearing card (n left)", "Card cleared — safe to remove", "Card cleared — but N clips kept (not confirmed / rejected: see log)", "Clear failed — card write-protected?", "Copy failed — k clips unreadable". Please confirm the French copy and whether "Do not remove the card" must stay red/uppercase.
5. **Rejected clips.** When SkydiveOS rejects a clip (unpaired staff/card, no `goproSerial`), should the card still be cleared of the *other* clips and go green with a warning, or stay blocked (today it stays "Uploading…" forever)?
6. **Which build was on the boxes on 2026-10-18**, and can the ledgers `_camera-staging/6467/.transferred.json`, `_camera-staging/4812/.transferred.json`, the `2026-10-18/` staging folders and `logs/ingest.err.log` from the ingest box be provided? They are the only evidence that can say what those two cards actually waited on.
