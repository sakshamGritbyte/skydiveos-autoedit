# Migration — Quick Action Checklist

**Use this when the client hands over their new S3 bucket + MongoDB URL and you have to
migrate right away.** This is the short "fill-in + run" version. For the full explanation
(why, rollback, blockers) see [MIGRATION_RUNBOOK.md](MIGRATION_RUNBOOK.md).

> **GOLDEN RULE:** Copy data FIRST → cut over config SECOND → verify THIRD.
> Never point a live service at an empty bucket/DB. Old stays as rollback.

---

## ✍️ STEP 0 — Fill these in the moment the client gives them

```
NEW S3 BUCKET        : ______________________________
NEW S3 REGION        : ______________________________   (e.g. ca-central-1)
NEW AWS ACCESS KEY   : ______________________________
NEW AWS SECRET KEY   : ______________________________

NEW MONGO URL        : mongodb+srv://<user>:<pass>@______________________________
```

These must go into **the SAME four places** or uploads/reads break:
1. **SkydiveOS backend** — EC2 `172-31-13-157` (docker containers, run-env)
2. **EC2 autoedit** — `172-31-8-87` (`~/skydiveos-autoedit/.env`, docker compose)
3. **Client Mac** — `~/skydiveos-autoedit/.env`
4. **Client Windows** — `...\skydiveos-autoedit\.env`

---

## 🕐 Do BOTH S3 + Mongo in ONE maintenance window

So each machine is edited + restarted **once**, not twice.

---

## 1️⃣ COPY DATA FIRST (before touching any config)

### S3 (copy old bucket → new bucket)
```bash
# a) download from OLD (personal) bucket — personal keys active
aws s3 sync s3://skydivingoss ./s3-backup --region ap-south-1

# b) switch to CLIENT keys + region, upload to NEW bucket
export AWS_ACCESS_KEY_ID=<NEW KEY>
export AWS_SECRET_ACCESS_KEY=<NEW SECRET>
export AWS_DEFAULT_REGION=<NEW REGION>
aws s3 sync ./s3-backup s3://<NEW BUCKET> --region <NEW REGION>

# c) verify counts match
aws s3 ls s3://<NEW BUCKET> --recursive --summarize --region <NEW REGION> | tail -3
```

### Mongo — pause writes, THEN dump + restore
```bash
# pause the app so no new data is written during the copy (on 172-31-13-157)
docker stop skydivingos-backend skydivingos-dev
# also pause ingest on Mac/Windows (stop service / stop scheduled task)

# dump BOTH databases (skydivingos = app, skydiveos = camera allow-list)
mongodump   --uri "mongodb+srv://Sak12:<oldpass>@cluster0.os363zi.mongodb.net" --out ./mongo-backup
mongorestore --uri "<NEW MONGO URL>" ./mongo-backup
```
> Prep before the window: in client's Atlas create the DB user (new creds) and add the
> EC2 + client public IPs to **Network Access** allowlist (or temp `0.0.0.0/0`, tighten later).

---

## 2️⃣ CUT OVER CONFIG — all four places (backend FIRST)

### Place 1 — SkydiveOS backend (EC2 172-31-13-157) — do this first (it's the reader)
```bash
# find how the containers were launched (env is at docker run, no compose file)
docker inspect skydivingos-backend --format '{{json .Config.Env}}'
cat ~/nohup.out 2>/dev/null | grep -i "docker run" | tail -5
ls ~/*.sh 2>/dev/null
# update AWS_S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, MONGO_URL
# then recreate BOTH containers with the new env:
docker rm -f skydivingos-backend skydivingos-dev
# re-run them (same command, new values)
```
> ⚠️ If you can't find the run command, DON'T delete the containers — ask the deployer first.
> Deleting without the run command = broken production.

### Place 2 — EC2 autoedit (172-31-8-87)
```bash
cd ~/skydiveos-autoedit
nano .env      # AWS_S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, MONGO_URL
docker compose up -d --build
```

### Place 3 — Client Mac
```bash
cd ~/skydiveos-autoedit
nano .env      # S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, MONGO_URL
bash deploy/mac/load-service.sh    # restart service to reload .env
```

### Place 4 — Client Windows
```powershell
cd $HOME\skydiveos-autoedit
notepad .env   # same values (S3_BUCKET, AWS_*, MONGO_URL)
Stop-ScheduledTask  -TaskName "SkydiveOS-Ingest"
Start-ScheduledTask -TaskName "SkydiveOS-Ingest"
```

---

## 3️⃣ VERIFY, then resume

- SkydiveOS UI: an **existing** customer video still plays + bookings/staff load
  → proves data copied AND backend reads new bucket + new DB.
- One **test camera pull** → new clip lands in new bucket + shows in UI Media module.
- EC2 worker can render a job (reads new bucket).

---

## 4️⃣ AFTER it's stable (a few days later)

- Keep OLD bucket + OLD Mongo cluster untouched = rollback safety net.
- Only once everything is stable: rotate/disable the exposed secrets —
  old AWS key `AKIAWBYYVUZ4KSQYVSJC`, old Mongo user `Sak12`.

---

## 🔙 Rollback (if anything breaks mid-window)
You COPIED (didn't move), so old bucket/cluster are intact. Revert the four `.env`/container
envs to the OLD bucket + OLD Mongo URL, restart. Nothing lost.
