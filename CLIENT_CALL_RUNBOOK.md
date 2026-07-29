# Client Call Runbook (Hinglish) — start se end tak

Ye doc **client call ke din** ke liye hai. Pehle 10 min ka pre-flight, phir demo,
phir kya-kya abhi pending hai — sab ek jagah, taaki call mein surprise na ho.

**Do machine hain, dono ka kaam alag:**

| machine | kya karti hai |
|---|---|
| **Client Mac** (`shred@Parachutisme-Nouvel-Air`) | camera se footage uthati hai (BLE → WiFi → S3 → SkydiveOS ko batati hai). Edit **nahi** karti |
| **EC2** (`172-31-8-87`) | asli kaam: segment → score → edit → render → email. Camera se koi lena-dena nahi |

---

## STEP 0 — Pre-flight (call se 30 min pehle, 10 min lagega)

### 0.1 Mac zinda hai?

```bash
cd ~/skydiveos-autoedit
launchctl list | grep com.skydiveos.ingest
curl -s -o /dev/null -w 'API %{http_code}\n' http://localhost:8000/jobs
tail -3 logs/ingest.err.log
```

Chahiye:
- `34795  0  com.skydiveos.ingest` — **doosra column 0 hona chahiye** (0 = theek, kuch aur = crash)
- `API 200`
- `camera auto-discovery started (interval=30s)`

### 0.2 Mac ka data theek hai?

```bash
uv run --no-sync python scripts/check_match.py --readiness
```

Ye batayega kitne staff ke paas `goproSerial` hai. **Abhi 14 mein se sirf 1 (Gregory)** —
ye hi sabse bada gap hai, client ko batana hai (neeche PART 3).

### 0.3 EC2 zinda hai?

```bash
cd ~/skydiveos-autoedit
docker compose ps
docker compose logs api --tail 5
docker compose logs worker --tail 5
```

Chahiye: `api`, `worker`, `redis` teeno **Up**, api mein `Uvicorn running`,
worker mein `celery@... ready`.

### 0.4 EC2 ka config theek hai?

```bash
grep -E 'AUTO_DELIVER|SMTP_HOST|CELERY_TASK_ALWAYS_EAGER|AWS_S3_BUCKET_NAME' .env
```

- `AUTO_DELIVER=1` — nahi hua toh job `ready` pe ruk jayegi, email nahi jayega
- `SMTP_HOST=smtp.gmail.com` — email transport
- `CELERY_TASK_ALWAYS_EAGER` **hona hi nahi chahiye** (ya `0`) — warna edit ke poore
  15 min API freeze ho jata hai aur demo mein "hang" dikhta hai

---

## PART 1 — Demo bina camera ke (SAFEST — yahi dikhao)

Ye poora chain hai aur **proven** hai. Camera ki zaroorat nahi.

```bash
docker compose exec worker python scripts/qa_all_packages.py --packages video_only --footage /tmp/test.MP4 --email saxenasaksham46@gmail.com --api http://api:8000
```

> `--api http://api:8000` **zaroori hai** — script worker container ke andar chalti hai,
> aur wahan `localhost` matlab worker khud, API nahi.

~10 min lagega. Screen pe stage-by-stage matrix aayega:

```
✓ ingest    raw staged            1 master(s)
✓ segment   exit/deploy offsets   exit=40.04 deploy=108.11 (freefall 68.1s)
✓ score     face scores           237 scored second(s)
✓ compose   EDL full_video        18 clip(s)
✓ validate  validation report     6 repair(s)
✓ render    full_video A/V sync   video/audio differ by 0.00s
✓ review    auto-approved         status=delivered
✓ deliver   link gallery opens    HTTP 206
RESULT: ALL PACKAGES PASS
```

**Client ko kya dikhana hai:**
1. Ye matrix — har stage ka proof
2. Apna inbox — gallery email
3. Gallery link kholo — saare videos + photos ek page pe

**Sab packages chalane hain?** `--packages selfie,external,video_only,photo_only,ultimum`
(par har ek 10–25 min leta hai, call mein time nahi hoga — pehle se chala ke report dikhao)

Purani report bhi dikha sakte ho: `qa-report.md`, `qa-report-Daniel.md`,
`qa-report-Francine.md` — ye asli Shred footage ke Ultimate jumps hain, sab pass.

---

## PART 2 — Camera flow (Greg ki camera se, jab koi dropzone pe ho)

Greg ki camera **pair ho chuki hai** (`4313`). Ye test tabhi karo jab koi wahan ho.

### 2.1 Pehle read-only check (kuch download nahi hoga)

```bash
uv run --no-sync python scripts/check_camera.py --wifi --camera 4313
```

Card ki list aa gayi = BLE + WiFi + SDK teeno kaam kar rahe hain.
(Mac is dauran apne WiFi se hat jayega, camera ke AP pe judega — normal hai.)

### 2.2 Phir asli automatic pull

Camera **on** karke Mac ke paas (5–10 meter) rakho, aur dekho:

```bash
tail -f logs/ingest.err.log | grep -E "discovered|on card|downloading|to S3|handed|card cleanup|failed"
```

30 second ke andar aisa dikhna chahiye:

```
Camera 4313 discovered, pull enqueued
camera 4313: N video(s) on card
downloading 100GOPRO/GX0100xx.MP4 -> raw-storage/_camera-staging/4313/...
uploading ... to S3 + notifying SkydiveOS
handed ... off to SkydiveOS
```

**Pehli line hi sabse important hai** — wo prove karti hai ki launchd **service** ko
Bluetooth permission mil chuki hai (Terminal wali permission alag hoti hai, wo count nahi hoti).

Agar camera on hai, paas hai, aur 1 min tak kuch nahi aaya → permission missing hai:
System Settings → Privacy & Security → Bluetooth.

### 2.3 Dhyan rakhna

- Poora card transfer hoga WiFi pe — 5 jump ≈ 10 GB ≈ **35–70 min**. Test ke liye
  1–2 clip wala card use karo
- Har file client ke S3 pe jayegi aur **production SkydiveOS** ko notify hoga
- **Kuch delete nahi hoga** — `DELETE_AFTER_TRANSFER_DRY_RUN=true` hai

---

## PART 2.5 — Jump archive Mac pe (automatic)

Pipeline EC2 pe chalti hai, isliye customer-naam wala archive wahin banta hai. Mac pe
wo **khud-ba-khud** har 5 minute mein sync hota hai:

```bash
# EC2 pe (ek baar): bind mount wala compose lagao
git pull && mkdir -p raw-storage && docker compose up -d

# Mac pe (ek baar): SSH key + timer
ssh-copy-id ubuntu@<ec2-ip>
git pull
EC2_HOST=ubuntu@<ec2-ip> bash deploy/mac/load-archive-sync.sh
```

Uske baad Mac pe Finder mein kholo: `~/skydiveos-autoedit/jump-archive/`

```
2026-07-29/Gregory-Perrimond/Marie-Dupont/
    edited/    full_video.mp4  highlights.mp4  ...
    photos/    50 stills
    manifest.json
```

- Har 5 min sync (`logs/archive-sync.out.log` mein har run ka record)
- `raw/` jaan-boojh ke nahi aata — masters Mac pe pehle se hain (`_camera-staging`),
  GB-on-GB dobara download karna bekaar hai
- Band karna ho: `bash deploy/mac/load-archive-sync.sh unload`

## PART 3 — Client ko kya batana hai (imaandari se)

### ✅ Jo chal raha hai

- Paanchon package end-to-end verified — asli footage pe, har stage ka proof
- 3 asli Shred Ultimate jumps (Daniel, Francine, Luc) — sab pass, A/V sync 0.03 s ke andar
- Mac deploy + healthy, BLE scan chal raha hai
- EC2 deploy + auto-deliver + email configured
- Camera → staff link kaam kar raha hai (Greg ki camera serial suffix se match hui)

### ❌ Jo unke side pe pending hai (ye zaroor batao)

**1. 14 mein se 13 staff ke paas GoPro serial nahi hai** — sabse bada blocker.
Unki footage S3 tak pahunchegi aur wahin ruk jayegi, kisi customer tak nahi jayegi.
~20 min ka data entry kaam hai. Camera ka serial: Settings → About → Camera Info.

**2. SkydiveOS ko do rule chahiye** (production mein match wahi karta hai —
`SKYDIVEOS_INTEGRATION.md` dekho). Reference implementation `ingest/match.py` mein hai:
   - Load wo chuno jo capture time se **pehle sabse recent** uda ho — "window mein aane
     wala koi bhi load" nahi. Warna ek din ke 2nd–5th jump ambiguous ho jate hain
   - Camera id ko `staffs.goproSerial` ke **suffix** se match karo (BLE sirf aakhri 4
     digit batati hai: `GoPro 4313` → `4313`, jabki serial `C3504224544313` hai)

**3. Unmatched footage ke liye jagah chahiye.** Jab match nahi hota (galat clock,
ambiguity, serial missing), clip S3 mein padi rehti hai aur kisi ko pata nahi chalta.
SkydiveOS mein ek "unassigned media" list chahiye jise koi manually assign kar sake.

### ⏳ Jo abhi test nahi hua

- Asli camera se pull (dropzone pe koi chahiye)
- launchd service ka Bluetooth grant
- Card cleanup asli hardware pe (abhi dry-run mein hai)

---

## Kuch fail ho toh

```bash
# Mac
tail -50 logs/ingest.err.log
launchctl list | grep com.skydiveos.ingest

# EC2
docker compose logs worker --tail 50
curl -s http://localhost:8000/jobs | python3 -m json.tool | grep -E 'status|error'
```

| Problem | Reason | Fix |
|---|---|---|
| `API not reachable at localhost:8000` (EC2 container ke andar) | worker container mein localhost = worker | `--api http://api:8000` lagao |
| Job `ready` pe ruk gayi, email nahi aaya | `AUTO_DELIVER` set nahi | `.env` mein `AUTO_DELIVER=1`, `docker compose up -d` |
| `GET /jobs` timeout, sab hang | `CELERY_TASK_ALWAYS_EAGER=1` + worker dono chal rahe | `=0` karo, restart |
| `no single-camera footage found` | `/data/jobs` khali hai | `--footage /tmp/test.MP4` do |
| `The Wifi driver ... only supports en_US` | French Mac ka locale | code mein fix ho chuka — `git pull` karo |
| Camera discover nahi ho rahi | service ko Bluetooth permission nahi | System Settings → Privacy & Security → Bluetooth |
| Match nahi ho raha | staff ka `goproSerial` missing | `check_match.py --readiness` chalao |
| `status: failed`, scene error | footage re-encoded, GPMF gayab | SD card ki ORIGINAL file (`gpmd` stream honi chahiye) |

**GPMF check:**
```bash
ffprobe -v error -show_entries stream=index,codec_type,codec_tag_string -of csv <file.MP4>
```
`gpmd` wali line honi chahiye — nahi toh segmentation fail hogi.

---

## Ek line mein

> Pipeline poora ban chuka hai aur asli footage pe verified hai. Mac aur EC2 dono
> deploy ho chuke hain. Camera → customer wala link bhi bana hua hai aur test kiya hai —
> wo chalu hone ke liye bas **har instructor/cameraman ka GoPro serial SkydiveOS mein
> daalna** baaki hai.

Detail chahiye toh: [`QA_NO_GOPRO.md`](QA_NO_GOPRO.md) (bina camera test),
[`MANUAL_QA.md`](MANUAL_QA.md) (camera ke saath), [`GO_LIVE.md`](GO_LIVE.md) (deploy config),
[`SKYDIVEOS_INTEGRATION.md`](SKYDIVEOS_INTEGRATION.md) (SkydiveOS ka contract).
