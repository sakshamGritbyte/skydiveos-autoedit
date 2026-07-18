# Migration Runbook — move S3 (and MongoDB) to the client's own account

Goal: stop using the developer's personal AWS/Mongo and run everything on the client's
own accounts, during a limited access window. This touches **live production** (the
SkydiveOS backend serves real customers), so the golden rule is:

> **Copy the data FIRST, cut over configs SECOND, verify THIRD. Never point a live
> service at an empty destination.**

The S3 bucket / Mongo URL must be **identical across all four places** or uploads 404:
1. Client **Mac** (`~/skydiveos-autoedit/.env`)
2. Client **Windows** (`...\skydiveos-autoedit\.env`)
3. **EC2 autoedit** (`~/skydiveos-autoedit/.env` on 172-31-8-87, docker compose)
4. **SkydiveOS backend** (docker containers on 172-31-13-157: `skydivingos-backend` prod
   + `skydivingos-dev`)

---

## PART 1 — S3 BUCKET migration

Source (personal): `skydivingoss` in `ap-south-1`
Destination (client): `skydiveos-media-878013573203` in `ca-central-1`
Client IAM key: `AKIA4Y3NP5BJTKKKP53A` (+ its secret)

### Step 0 — See how much is in the old bucket (decide copy vs fresh)
On any machine with AWS CLI + the PERSONAL keys configured:
```bash
aws s3 ls s3://skydivingoss --recursive --summarize --region ap-south-1 | tail -3
```
- Small / only test objects → a fresh start is fine, skip the copy (Step 1).
- Real customer media present → you MUST copy (Step 1) or old deliveries break.

### Step 1 — Copy old → new (only if keeping data)
Simplest cross-account method (two hops through a local folder — no bucket-policy setup):
```bash
# a) download from personal bucket (personal keys active)
aws s3 sync s3://skydivingoss ./s3-backup --region ap-south-1

# b) switch to CLIENT keys, then upload to the new bucket
#    (set the client AKIA.../secret + AWS_DEFAULT_REGION=ca-central-1 first)
aws s3 sync ./s3-backup s3://skydiveos-media-878013573203 --region ca-central-1
```
Verify counts match:
```bash
aws s3 ls s3://skydiveos-media-878013573203 --recursive --summarize --region ca-central-1 | tail -3
```

### Step 2 — Cut over the SkydiveOS backend (do this first — it's the reader/server)
On EC2 `172-31-13-157`. These are docker containers; first find HOW they were launched
(env is passed at `docker run`, there is no compose file in ~):
```bash
docker inspect skydivingos-backend --format '{{json .Config.Env}}'   # see current values
cat ~/nohup.out 2>/dev/null | grep -i "docker run" | tail -5          # find the run command
ls ~/*.sh ~/src-backup 2>/dev/null                                    # look for a start script
```
Update `AWS_S3_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
in whatever launches it (start script / run command), then recreate BOTH containers:
```bash
docker rm -f skydivingos-backend skydivingos-dev
# re-run them with the updated env (same command, new AWS_* values)
```
> ⚠️ If you can't find the run command, DO NOT delete the containers — get the admin who
> deployed them first. Deleting without the run command = broken production.

### Step 3 — Cut over EC2 autoedit
On EC2 `172-31-8-87`:
```bash
cd ~/skydiveos-autoedit
# edit .env: AWS_S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
nano .env
docker compose up -d --build
docker compose ps
```

### Step 4 — Cut over client Mac
```bash
cd ~/skydiveos-autoedit
nano .env    # S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION=ca-central-1
bash deploy/mac/load-service.sh    # restart the service to reload .env
```

### Step 5 — Cut over client Windows
```powershell
cd $HOME\skydiveos-autoedit
notepad .env    # same four values
Stop-ScheduledTask -TaskName "SkydiveOS-Ingest"
Start-ScheduledTask -TaskName "SkydiveOS-Ingest"
```

### Step 6 — Verify end to end
- SkydiveOS UI: an EXISTING customer video still plays (proves data copied + backend reads new bucket).
- Do one test camera pull → new clip appears in the new bucket + shows in the UI.
- Check EC2 worker can render a job (reads new bucket).

---

## PART 2 — MONGODB migration (full, to client's Atlas)

The source cluster (`cluster0.os363zi`, user `Sak12`) holds TWO databases, BOTH must move:
- `skydivingos` — the ENTIRE SkydiveOS app (bookings, customers, staff, media records…)
- `skydiveos` — the autoedit camera allow-list (`cameras`)

Destination: the client's own Atlas cluster (new host + NEW credentials).

> This is live production data. Bookings/media records change constantly, so a plain
> dump-then-restore taken hours before cutover will LOSE anything written in between. Do
> the final dump inside a short **maintenance window** with the app paused.

### Step A — Prep the destination (before the window)
1. In the client's Atlas: confirm the cluster, create a DB user (new creds), and add the
   connecting IPs to the **Network Access allowlist**: the two EC2 IPs
   (`172.31.8.87`, `172.31.13.157` → their public IPs) and the client Mac/Windows public
   IPs. (Or temporarily `0.0.0.0/0`, then tighten.)
2. Note the new connection string: `mongodb+srv://<newuser>:<newpass>@<client-cluster>...`
3. Install DB tools where you'll run the dump: `mongodump`/`mongorestore`
   (`brew install mongodb-database-tools` / choco / apt).

### Step B — Maintenance window: pause writes
Stop the SkydiveOS backend so no new bookings are written during the copy:
```bash
# on 172-31-13-157
docker stop skydivingos-backend skydivingos-dev
```
(Also pause the ingest services on Mac/Windows so no new media records are created.)

### Step C — Dump + restore BOTH databases
```bash
mongodump   --uri "mongodb+srv://Sak12:<pass>@cluster0.os363zi.mongodb.net" --out ./mongo-backup
mongorestore --uri "mongodb+srv://<newuser>:<newpass>@<client-cluster>"      ./mongo-backup
```
This carries over both `skydivingos` and `skydiveos` with all collections/indexes.

### Step D — Cut over MONGO_URL everywhere (do together with the S3 cutover)
Same four places, new connection string:
1. **SkydiveOS backend** run env (find the run command as in Part 1 Step 2) → recreate containers
2. **EC2 autoedit** `~/skydiveos-autoedit/.env` → `docker compose up -d --build`
3. **Mac** `.env` → `bash deploy/mac/load-service.sh`
4. **Windows** `.env` → Stop/Start the `SkydiveOS-Ingest` task

### Step E — Restart + verify, then resume
- Bring the SkydiveOS backend + ingest services back up.
- Verify in the UI: bookings, customers, staff, existing media all load (proves restore worked).
- Do a test camera pull → new media record lands (proves autoedit + backend on the new cluster).

> Keep the OLD cluster untouched for several days as rollback. Only after stable running,
> decommission it and rotate/disable the old `Sak12` credentials.

### Combined-window tip
Do PART 1 (S3) and PART 2 (Mongo) in the SAME maintenance window so each of the four
machines is edited + restarted ONCE (both `S3_BUCKET`/`AWS_*` and `MONGO_URL` at the same
time), not twice.

---

## Rollback (if anything breaks)
Because you COPIED (not moved), the old bucket/cluster are intact. To roll back: revert the
four `.env`/container envs to the old bucket/URL + restart. Nothing is lost.
