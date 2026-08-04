# Manual QA — Auto-Edit (Hinglish)

Ye checklist dropzone pe manually test karne ke liye hai. Har package ke liye alag,
plus poori automation ka step-by-step. Har box tick karte jao. ✅

> **Rule #1:** Har test mein `customer_email` apna khud ka email do — kabhi real paying
> customer ka nahi (jab tak sab pass na ho jaye).
> **Rule #1 ka reason:** galat edit ya galat email real customer tak nahi jaana chahiye.
>
> **Ek hi jump ka stage-by-stage deep pass chahiye** (camera on karne se lekar customer
> ke gallery me videos + photos tak, 14 gates)? →
> [`MANUAL_QA_CAMERA_TO_GALLERY.md`](MANUAL_QA_CAMERA_TO_GALLERY.md)
>
> **Camera paas nahi hai?** → [`QA_NO_GOPRO.md`](QA_NO_GOPRO.md). Wahan paanchon package
> ka poora stage-by-stage audit ek command me chalta hai
> (`python scripts/qa_all_packages.py`), purane jobs ke real masters use karke.

`$AE` = auto-edit API ka URL (e.g. `http://<ec2-ip>:8000`).

---

## STEP 0 — Setup (ek baar)

- [ ] EC2 `.env` mein ye set hai:
  ```
  AUTO_DELIVER=1
  SMTP_HOST=... SMTP_USER=... SMTP_PASSWORD=...
  DELIVERY_FROM_EMAIL=videos@yourdz.com     # SES mein verified hona chahiye
  CAMERA_CLOCK_TZ=America/Toronto
  S3_BUCKET=skydivingoss
  ```
- [ ] `docker compose up -d` (ya dono containers restart) — env pick ho gaya
- [ ] SES **sandbox** check: apna test email SES mein verify kar lo (warna mail nahi jayega)
- [ ] Ek real GoPro jump ke raw MP4 files ready hain (SD card ke original files — GPMF
      telemetry inme hi hoti hai; re-encoded/WhatsApp-forwarded files kaam nahi karenge)

---

## PART A — Har package ka QA (footage direct upload, SkydiveOS ke bina)

Ye sabse pehle karo — sirf hamara module test hota hai (discovery/SkydiveOS skip).

### Har package ke liye common 3 steps

```bash
# 1. job banao (package badalte raho: selfie/external/video_only/photo_only/ultimum)
JOB=$(curl -s -XPOST $AE/jobs -H 'Content-Type: application/json' \
  -d '{"customer_name":"Test Me","customer_email":"YOU@email.com","package":"selfie"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo $JOB

# 2. real GoPro footage upload karo (pipeline apne-aap start ho jayega)
curl -XPOST $AE/jobs/$JOB/upload -F files=@GH010001.MP4 -F files=@GH010002.MP4

# 3. complete hone tak dekho
watch -n3 "curl -s $AE/jobs/$JOB | python3 -m json.tool | grep -E 'status|error|outputs'"
```

### ✅ 1. `selfie` (handcam — instructor apni video banata hai)
- [ ] Job banaya `"package":"selfie"`
- [ ] Footage upload hua
- [ ] status: `queued → processing → ready → delivered`
- [ ] `outputs` mein hain: **full_video, highlights, freefall, photos**
- [ ] Email aaya, saare links khulte/play hote hain
- [ ] Sahi customer ka chehra dikh raha, cuts theek, music hai
- [ ] Photos ~50 hain

### ✅ 2. `external` (camera-flyer — bahar se koi film karta hai)
- [ ] `"package":"external"`, baaki selfie jaisa
- [ ] `outputs`: **full_video, highlights, freefall, photos**
- [ ] Distant footage hone ke bawajood video bani (house-cut) + photos aaye (~50)
- [ ] Email + links OK

### ✅ 3. `video_only` (sirf videos, photos nahi)
- [ ] `"package":"video_only"`
- [ ] `outputs`: **full_video, highlights, freefall** (photos NAHI hone chahiye)
- [ ] Email + links OK

### ✅ 4. `photo_only` (sirf photos)
- [ ] `"package":"photo_only"`
- [ ] `outputs`: sirf **photos** (~140 — `PHOTO_ONLY_TARGET`, footage pe depend)
- [ ] Video koi nahi
- [ ] Email + photos-zip link OK

### ✅ 5. `ultimum` (2 camera — instructor + external — 5 deliverables)
```bash
JOB=$(curl -s -XPOST $AE/jobs -H 'Content-Type: application/json' \
  -d '{"customer_name":"Test Me","customer_email":"YOU@email.com","package":"ultimum"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# DONO camera alag-alag, camera_role ke saath:
curl -XPOST $AE/jobs/$JOB/upload -F files=@instructorcam.MP4 -F camera_role=instructor
curl -XPOST $AE/jobs/$JOB/upload -F files=@outsidecam.MP4    -F camera_role=external
```
- [ ] Pehli camera ke baad job **wait** karti hai (processing start NAHI)
- [ ] Doosri camera ke baad hi pipeline chalti hai
- [ ] `outputs`: **full_video, highlights, external_freefall, chute_libre_selfie, photos**
- [ ] Diagnose chalao — dono camera use hui + audio/video sync:
      `docker exec -it skydiveos-autoedit-worker-1 python scripts/diagnose_ultimum.py $JOB`
- [ ] Email + saare 5 links OK

### ✅ 6. `ultimum` watchdog (sirf ek camera aaye toh?)
- [ ] Naya ultimum job banao, **sirf ek** camera upload karo
- [ ] Thodi der baad (ya timeout `ULTIMUM_SECOND_CAMERA_TIMEOUT_S` ke baad) job
      `failed` hoti hai clear error ke saath — hang NAHI hoti

---

## PART B — Automation ka step-by-step test (poora auto flow)

Part A pass hone ke baad ye karo. Ye discovery + SkydiveOS + auto-deliver — poora chain.

### Step 1 — Review gate OFF karke edit quality check (safest pehle)
- [ ] `.env` mein `AUTO_DELIVER=0` karo, restart
- [ ] Ek package ka job + real footage (Part A jaisa)
- [ ] Job `ready` pe rukti hai (deliver NAHI hoti)
- [ ] Review UI / `GET $AE/jobs/$JOB/deliverables` se edit manually dekho — quality theek?

### Step 2 — Auto-deliver ON, ek package
- [ ] `AUTO_DELIVER=1`, restart
- [ ] Wahi test dobara → ab job khud `delivered` tak jati hai + email aata hai
- [ ] Confirm: koi manual approve nahi dabana pada

### Step 3 — Saare 5 packages auto-deliver
- [ ] Part A ke 5 packages, ab `AUTO_DELIVER=1` ke saath → sab email tak pahunche

### Step 4 — Camera discovery (footage khud utha rahi hai)
- [ ] Camera pair karo role ke saath (dropzone Mac pe):
      `python -m ingest.pull --camera <id> --pair --role instructor`
      (ultimum ke liye doosri camera `--role external`)
- [ ] `ENABLE_AUTO_DISCOVERY=1`
- [ ] Camera on karke ek chhoti clip banao
- [ ] Discovery khud: pull → S3 upload → SkydiveOS ko notify (`captured_at` + `camera_role`)
- [ ] Worker logs mein dikhna chahiye: `uploaded ... to S3 + notifying SkydiveOS`

### Step 5 — SkydiveOS match + full auto (end-to-end)
- [ ] SkydiveOS mein ek **test booking** banao (package + apna email + us camera pe mapped)
- [ ] Jump karo (ya clip banao)
- [ ] SkydiveOS footage ko booking se match karta hai → job banata hai → footage attach → edit → email
- [ ] SkydiveOS status callbacks mein flow dikhta hai, end mein `delivered`
- [ ] **Apne inbox** mein video aaya 🎉

### Step 6 — Timezone validation (Canada — ye zaroor check karo)
- [ ] Ek real jump ka `captured_at` compare karo actual takeoff time se
- [ ] Match ho raha → sahi. Agar 4–5 ghante off → cameras UTC pe hain,
      `CAMERA_CLOCK_TZ` hata do (ya sahi zone lagao)

---

## Kuch fail ho toh yahan dekho

```bash
docker logs -f skydiveos-autoedit-worker-1        # pipeline stages live
curl -s $AE/jobs/$JOB | python3 -m json.tool      # "error" field
curl -s $AE/jobs/$JOB/deliverables                # kya bana
```

| Problem | Reason | Fix |
|---|---|---|
| `status: failed`, scene error | footage re-encoded, GPMF gayab | SD card ki ORIGINAL file use karo |
| Email nahi aaya | SES sandbox / sender verify nahi | SES mein email/domain verify karo |
| `ultimum` atka hua | sirf ek camera aayi | doosri upload karo (ya watchdog fail karega) |
| Match galat/queue mein | `captured_at` timezone skew | `CAMERA_CLOCK_TZ` sahi karo (Step 6) |
| Links SkydiveOS tak nahi | callback token/URL | `AUTO_EDIT_CALLBACK_TOKEN` dono taraf same |

---

## Final "sab theek hai" ka matlab
- [ ] Part A: paanchon package direct upload pe `delivered` + email + sahi deliverables
- [ ] Part B: discovery → SkydiveOS → auto-deliver poora chain bina haath lagaye chala
- [ ] Timezone validate ho gaya real jump pe
- [ ] Tab jaake real customer emails pe switch karo
