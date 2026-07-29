# Bina GoPro ke Full-Flow QA (Hinglish)

`MANUAL_QA.md` dropzone pe **real camera** ke saath ka checklist hai. Ye uska
**no-hardware** version hai: koi GoPro nahi, phir bhi poora chain — ingest → segment →
score → compose → validate → render → auto-approve → S3 → presigned link → email →
jump archive — paanchon package ke liye stage-by-stage verify ho jata hai.

Footage kahan se? Pehle chal chuke jobs ke **real GoPro masters** (`jobs/*/raw/`) —
unme GPMF telemetry hai, isliye segmentation sach me chalti hai.

---

## STEP 0 — Pre-flight (2 min, har baar)

```bash
# 1) .env me ye teen line CRITICAL hain
grep -E 'CELERY_TASK_ALWAYS_EAGER|AUTO_DELIVER|CAMERA_CLOCK_TZ' .env
#   CELERY_TASK_ALWAYS_EAGER=0   <-- worker chal raha ho to HAMESHA 0
#   AUTO_DELIVER=1
#   CAMERA_CLOCK_TZ=America/Toronto
```

> **Sabse badi galti:** `CELERY_TASK_ALWAYS_EAGER=1` rakhna jab worker bhi chal raha ho.
> Tab poora edit API process ke **andar inline** chalta hai → jab tak edit chalti hai
> (~15 min) `GET /jobs`, `/docs`, review UI, camera discovery — sab **freeze**. Demo me
> ye "system hang ho gaya" jaisa dikhta hai. `0` rakho: upload turant return karta hai,
> pipeline worker pe jati hai, API 5 ms me jawab deta hai.

```bash
# 2) stack start karo (do alag process)
.venv/bin/uvicorn api.app:app --port 8000            >/tmp/ae-api.log 2>&1 &
.venv/bin/celery -A api.celery_app.celery_app worker -l info >/tmp/ae-worker.log 2>&1 &

# 3) dono zinda hain?
curl -s -o /dev/null -w 'API %{http_code}\n' localhost:8000/jobs
grep -q 'celery@' /tmp/ae-worker.log && echo "worker ready"
```

---

## PART A — Paanchon package, EK command

```bash
.venv/bin/python scripts/qa_all_packages.py --email YOU@email.com
```

Ye har package ke liye job banata hai, real masters upload karta hai (auto-detect
`jobs/*/raw/` se), poll karta hai, aur phir **job folder khol kar har stage check**
karta hai. Aakhir me stage matrix + `qa-report.md` / `qa-report.json`.

Kya-kya check hota hai:

| Stage | Check |
|---|---|
| `ingest` | har uploaded master `jobs/<id>/raw/` me pahuncha (ultimum me per-role) |
| `segment` | `scene_manifest*.json` — scenes mile, `freefall` me `exit_offset`/`deploy_offset`, `file_offsets`, `flagged` |
| `score` | `scores*.json` me per-second face rows |
| `compose` | har video deliverable ka `edl_*.json`, clips maujood, `src_start <= src_end` |
| `validate` | `validation_report.json` — `validate_and_repair` chala, repairs list |
| `render` | har output file bani, chalti hai, aur **video vs audio duration ≤1 s** (freeze/desync catch) |
| `photos` | count package ke band me + `photos.zip` |
| `review` | bina manual approve ke `delivered` (AUTO_DELIVER) |
| `deliver` | `delivery_links` bane aur har presigned URL **HTTP 200** deta hai |
| `archive` | `raw-storage/<date>/<instructor>/<customer>/` mirror + manifest me sahi `job_id` |
| `api` | `/deliverables`, `/photos`, aur ek deliverable ka stream — sab jawab dete hain |

Useful flags:

```bash
# sirf ek package
.venv/bin/python scripts/qa_all_packages.py --packages ultimum --email YOU@email.com

# apni footage
.venv/bin/python scripts/qa_all_packages.py --packages selfie --footage /path/GX01*.MP4

# email bilkul nahi, sirf links
.venv/bin/python scripts/qa_all_packages.py --no-email
```

Exit code 0 = sab pass. Non-zero = kuch stage fail — `qa-report.md` me `✗` line dekho.

Expected deliverables (matrix isi se compare karta hai):

| package | deliverables |
|---|---|
| `selfie` / `external` | full_video, highlights, freefall, photos |
| `video_only` | full_video, highlights, freefall |
| `photo_only` | photos |
| `ultimum` | full_video, highlights, external_freefall, chute_libre_selfie, photos |

Ek jump customer ki tarah dekhna ho (gallery link + archive tree):

```bash
.venv/bin/python scripts/demo_full_auto.py --package external \
  --customer "Demo Customer" --instructor "Marc Tremblay" --email YOU@email.com \
  jobs/<koi-purana-job>/raw/GX0109*.MP4
```

---

## PART B — Camera automation, bina camera

Discovery ko GoPro ki jagah **static scanner** deta hai: `DISCOVERY_SAMPLE_MP4` ko
*asli* pull path se stage karta hai, S3 pe upload karta hai, aur SkydiveOS ko notify.

```bash
# 1) SkydiveOS ki jagah mock (wahi port jo .env ke SKYDIVEOS_API_BASE me hai)
.venv/bin/uvicorn scripts.mock_skydiveos:app --port 8001 >/tmp/ae-mock.log 2>&1 &

# 2) .env me simulation on
#    ENABLE_AUTO_DISCOVERY=1
#    CAMERA_SCANNER=static
#    DISCOVERY_FAKE_CAMERAS=<serial>
#    DISCOVERY_SAMPLE_MP4=templates/GL010652.mp4
#    DISCOVERY_INTERVAL_SECONDS=10

# 3) staging clear karo warna sab file "already staged" hai aur kuch handoff nahi hoga
rm -rf raw-storage/_camera-staging/<serial>

# 4) 10-20 sec baad
grep -E 'handed .* off to SkydiveOS' /tmp/ae-api.log | tail -3
curl -s localhost:8001/media | python3 -m json.tool | head -20
```

Har entry me dikhna chahiye: `s3_key`, `camera_id`, `camera_role`, aur `captured_at`
(**true UTC** — GoPro ki local clock `CAMERA_CLOCK_TZ` se convert hoti hai).

### Card cleanup (transfer ke baad card khali)

Ye woh part hai jo card ko bharne se rokta hai (128 GB ≈ 30 Ultimate jump ≈ 1 hafta;
card full = camera **chup-chaap record karna band** kar deti hai).

```bash
# .env me
DELETE_AFTER_TRANSFER=true
DELETE_AFTER_TRANSFER_MIN_AGE_H=0      # 0 = agli connect pe hi delete; 24 = ek din baad
DELETE_AFTER_TRANSFER_DRY_RUN=true     # PEHLI BAAR HAMESHA true
```

Staging clear karke API restart karo, phir 2 scan cycle ruk kar dekho:

```bash
grep -E "handed .* off|card cleanup|cleared .* already-delivered" /tmp/ae-api.log
```

Aisa dikhna chahiye:

```
handed raw-storage/_camera-staging/CAM/2026-07-29/GX010010.MP4 off to SkydiveOS
card cleanup: deleted GX010010.MP4 (safe: raw/CAM/GX010010.MP4)
camera CAM: cleared 3 already-delivered file(s)
```

Ledger check karo — yahi decide karta hai kya delete hoga:

```bash
python3 -m json.tool raw-storage/_camera-staging/<CAM>/.transferred.json
```

**Rule:** sirf wahi file delete hoti hai jiska S3 key ledger me hai. Jo upload nahi
hui, wo card pe rehti hai aur agli baar retry hoti hai. Isliye 5 jump me se agar
jumper 3 ka upload fail hua, toh sirf uske clips bache rahenge — baaki 4 delete.

Real camera pe pehle **DRY RUN** chalao (`DELETE_AFTER_TRANSFER_DRY_RUN=true`), log me
`would delete` lines dekho, S3 me wo files confirm karo, phir dry-run band karo.

### Match (camera → customer) bhi chalana ho

Role/customer tabhi resolve hoga jab shared Mongo me **dono** cheezein ho:

1. `DISCOVERY_FAKE_CAMERAS` ka serial kisi `staffs.goproSerial` se match kare, **aur**
2. clip ka `captured_at` kisi load ki window me giray — `departureTime − 30 min` se
   `departureTime + 150 min` tak (DZ-local).

Iske liye ek hi command hai — `scripts/check_match.py` (read-only, kuch badalta nahi):

```bash
# 1. Data ready hai ya nahi (yahin se shuru karo)
.venv/bin/python scripts/check_match.py --readiness

# 2. Poora din replay karo — har load ka ek clip, kis customer pe gira
.venv/bin/python scripts/check_match.py --day 2026-07-29

# 3. Ek specific clip (time = DROPZONE-LOCAL, jo camera ki ghadi dikhati hai)
.venv/bin/python scripts/check_match.py --serial TEST-CAM-SIM-01 --at 2026-07-29T12:20

# 4. Ek asli file kis pe match hogi (uska creation_time padhta hai)
.venv/bin/python scripts/check_match.py --serial TEST-CAM-SIM-01 --file /path/GX010001.MP4
```

`--day` ka output aisa hota hai — `deliverable` = customer tak jayega,
`no-media` = us jumper ne media khareeda hi nahi (theek hai), `FAILED` = problem:

```
load 07:05  status=closed
    handcam  TEST-CAM-SIM-01    OK   role=instructor pkg=ultimum 'Saksham Saxena' <...>
    outside  TESTGOPRO007       OK   role=external   pkg=ultimum 'Saksham Saxena' <...>
deliverable 2, no-media 1, FAILED 0
```

Exit code 0 tabhi jab koi FAILED na ho — isliye subah jumping se pehle isse chala kar
dekh sakte ho ki aaj ka data theek hai.

**Do sabse common problem** jo ye pakadta hai:
- kisi instructor/cameraman ka `staffs.goproSerial` set nahi → uski footage kabhi match
  nahi hogi
- `CAMERA_CLOCK_TZ` galat/khaali → har match UTC offset jitna skew ho jayega

Sample clip ka capture time load ki window me le aao (GPMF ki zaroorat nahi — sirf
hand-off/match test ke liye):

```bash
ffmpeg -i sample-data/discovery_sample.mp4 -c copy \
  -metadata creation_time="2026-07-29T12:20:00" /tmp/demo-clip.MP4
# phir .env: DISCOVERY_SAMPLE_MP4=/tmp/demo-clip.MP4  (aur staging clear + restart)
```

Log me ye line matlab match kaam kar raha hai:

```
camera <serial>: load-derived role instructor overrides registry hint None
```

> **Imaandari se:** local mock sirf notification *record* karta hai. Us notification se
> job banana SkydiveOS ka kaam hai (prod me wahi karta hai — `SKYDIVEOS_INTEGRATION.md`).
> Isliye "camera se customer ke inbox tak" poora chain **Part A** (direct upload) me
> end-to-end chalta hai; Part B camera-side hand-off + match decision prove karta hai.

---

## PART C — Do edge case (demo se pehle ek baar)

### C1. Ultimum watchdog — doosri camera aayi hi nahi

```bash
# .env: ULTIMUM_SECOND_CAMERA_TIMEOUT_S=120   (test ke liye chhota), restart
JOB=$(curl -s -XPOST localhost:8000/jobs -H 'Content-Type: application/json' \
  -d '{"customer_name":"Watchdog Test","package":"ultimum"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -XPOST localhost:8000/jobs/$JOB/upload -F files=@one.MP4 -F camera_role=instructor
# 2 min baad: status "failed" + clear error hona chahiye, hang NAHI
curl -s localhost:8000/jobs/$JOB | python3 -m json.tool | grep -E 'status|error'
```

Note: `CELERY_TASK_ALWAYS_EAGER=1` me watchdog **skip** hota hai — is test ke liye `0` chahiye.

### C2. Review gate — AUTO_DELIVER off

```bash
# .env: AUTO_DELIVER=0, restart. Koi bhi package chalao:
.venv/bin/python scripts/qa_all_packages.py --packages video_only --no-email
```

Job `ready` pe rukni chahiye, `delivered` nahi, aur `delivery_links` khali. Ye prove
karta hai ki instructor gate abhi bhi kaam karta hai — sirf flag se bypass hota hai.
Test ke baad `AUTO_DELIVER=1` wapas.

---

## Demo-day: 20-minute rehearsal

```bash
# 1. pre-flight
grep -E 'CELERY_TASK_ALWAYS_EAGER|AUTO_DELIVER' .env     # 0 aur 1
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/jobs

# 2. sabse tez smoke test (ek package, email ke bina)
.venv/bin/python scripts/qa_all_packages.py --packages video_only --no-email

# 3. jo screen pe dikhana hai
.venv/bin/python scripts/demo_full_auto.py --package ultimum \
  --customer "Demo Customer" --instructor "Marc Tremblay" --email YOU@email.com \
  --instructor-cam <inst>.MP4 --external-cam <ext>.MP4
```

Processing ke beech me `curl localhost:8000/jobs` chala ke dikhana — API responsive
rehta hai. Ye wahi cheez hai jo eager mode me toot ti thi.

---

## Kuch fail ho to

```bash
tail -f /tmp/ae-worker.log                      # pipeline stages live
tail -f /tmp/ae-api.log                         # discovery + HTTP
curl -s localhost:8000/jobs/$JOB | python3 -m json.tool | grep -E 'status|error'
.venv/bin/python scripts/diagnose_ultimum.py $JOB     # ultimum: camera/scene/desync
cat qa-report.md                                # aakhri sweep ke saare ✗
```

| Problem | Reason | Fix |
|---|---|---|
| API/`/docs` hang, discovery ruk gayi | `CELERY_TASK_ALWAYS_EAGER=1` + worker | `=0` karo, dono restart |
| Discovery se kuch handoff nahi | saari file "already staged" | `rm -rf raw-storage/_camera-staging/<serial>` |
| `camera_role`/`instructor_id` null | serial kisi `staffs.goproSerial` me nahi, ya capture time kisi load window me nahi | Part B ka match section |
| `AmbiguousMatch` | ek hi camera + time pe 2 jumper | jaan-boojh ke refuse karta hai — load data theek karo |
| `status: failed`, scene error | footage re-encoded, GPMF gayab | SD card ki ORIGINAL file |
| Email nahi aaya | SMTP/SES verify nahi | `--no-email` se links test karo, phir mail fix |
