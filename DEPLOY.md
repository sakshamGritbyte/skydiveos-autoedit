# Deployment Guide — SkydiveOS Auto-Edit

This document describes the full deployment of the auto-edit pipeline, start to end:
the architecture, why we chose the instance and disk sizes, the cost, every step we
ran, and the issues we hit and how we fixed them.

---

## 1. Architecture

Everything runs on **one EC2 VM** using Docker Compose. Three containers:

| Container | What it does |
|-----------|--------------|
| **api** | FastAPI service (`uvicorn`). Creates jobs, receives footage uploads, serves review/approve endpoints. Enqueues the heavy work onto Redis. |
| **worker** | Celery worker. Runs the actual pipeline — classify → score (MediaPipe) → compose → render (FFmpeg). |
| **redis** | Message broker + result backend between the API and the worker. |

The API and worker **share a Docker volume** (`/data/jobs`) so the worker can read the
footage the API uploaded, and both use the **same Redis**.

```
                 ┌──────────────── one EC2 VM ────────────────┐
  client ──HTTP──►  api (FastAPI)  ──enqueue──►  redis        │
                 │       │                          │         │
                 │       └── shared /data/jobs ◄── worker (Celery: FFmpeg + MediaPipe)
                 └────────────────────────────────────────────┘
```

**Important: the pipeline is CPU-bound.** Video is encoded with software `libx264` and
faces are scored with MediaPipe on the **CPU** (no GPU/NVENC). This is why the instance
choice below matters.

---

## 2. Why this instance and disk

### Instance: `c7i.2xlarge` (8 vCPU, 16 GB RAM)

A single jump pegs the CPU for several minutes (software x264 encode + MediaPipe
scoring). For **sustained 100% CPU** work you must use a **compute-optimized** instance:

- **t2 / t3 (burstable) — avoid.** They run on CPU *credits*. Under a sustained encode
  they exhaust credits and **throttle to a low baseline** (jobs become slow and
  unpredictable), or in "unlimited" mode they charge extra for the burst — negating the
  cost saving.
- **c7i / c7a / c6i (compute-optimized) — correct.** Full, sustained CPU with no
  throttling. `c7i.2xlarge` costs roughly the same as `t3.2xlarge` but gives predictable
  performance.

`2xlarge` (8 vCPU) lets the worker run `--concurrency=2` (two jumps at once) while
leaving headroom for the API and Redis. 16 GB RAM comfortably holds 1–2 concurrent
jobs (FFmpeg + MediaPipe + OpenCV).

> Cheaper alternative: `c7a.2xlarge` (AMD) is ~10–15% cheaper for the same work.
> Start smaller with `c7i.xlarge` (4 vCPU) only if slower jobs are acceptable.

### Disk: 100 GiB gp3 (the default 8 GiB is far too small)

| What consumes disk | Approx size |
|--------------------|-------------|
| Docker image (Python + ffmpeg + MediaPipe + OpenCV + deps) | ~3–4 GB |
| Raw GoPro footage per job (Ultimate = 2 cameras, 4K, multi-clip) | **~10–20 GB** |
| Scene files (concat copies) + temp frames + 4 renders + photos | ~3–5 GB |
| OS + Docker overhead + logs | ~3–4 GB |

The default **8 GiB cannot even hold the image**, let alone footage. We use **100 GiB
gp3** (gp3 is cheaper and faster than gp2). Use 200 GiB if you keep many jobs on disk at
once. Prune old `jobs/<id>/raw/` footage or offload to S3 as it grows.

---

## 3. Cost

Approximate **on-demand** prices (vary by region — our instance is in `ca-central-1`;
check the AWS Pricing Calculator for exact figures). Round numbers:

| Item | Rate | Running 24×7 |
|------|------|--------------|
| `c7i.2xlarge` compute | ~$0.36–0.40 / hr | **~$270–290 / month** |
| 100 GiB gp3 storage | ~$0.08–0.09 / GB-month | **~$8–9 / month** |
| Data transfer out (delivering renders) | ~$0.09 / GB out | variable |
| **Total (always-on)** | | **~$280–300 / month** |

### Ways to cut cost

- **Stop the instance when idle.** A *stopped* instance bills **only the EBS storage
  (~$9/month)** — no compute charge. Great for dev: stop nights/weekends.
- **Savings Plan / Reserved (1-year commit):** ~40% off compute if always-on.
- **`c7a.2xlarge` (AMD):** ~10–15% cheaper for the same job.
- **Spot:** up to ~70% off, **but** can be interrupted mid-render — not recommended for
  long jobs.

Rough rule: **~$0.40/hour while it's running.** A few hours of processing a day, with
the instance stopped otherwise, can be well under $50/month.

---

## 4. Prerequisites

- An AWS account.
- A rotated set of secrets (Anthropic API key; AWS keys + S3 bucket if using S3).
- SSH access to the instance.

---

## 5. Step-by-step deployment

### 5.1 Launch the EC2 instance

- **AMI:** Ubuntu 22.04 LTS
- **Type:** `c7i.2xlarge` (8 vCPU / 16 GB)
- **Storage:** **100 GiB gp3** (change the default 8 GiB!)
- **Key pair:** create/select one for SSH.

### 5.2 Security group (inbound rules)

| Type | Port | Source | Purpose |
|------|------|--------|---------|
| SSH | 22 | **My IP** (preferred) or 0.0.0.0/0 | shell access |
| Custom TCP | 8000 | 0.0.0.0/0 | the API (direct, for now) |
| HTTP | 80 | 0.0.0.0/0 | for Caddy/SSL later |
| HTTPS | 443 | 0.0.0.0/0 | for Caddy/SSL later |

### 5.3 SSH in and install Docker

```bash
ssh ubuntu@<EC2_PUBLIC_IP>
curl -fsSL https://get.docker.com | sh
```

Let the `ubuntu` user run Docker without `sudo`:

```bash
sudo usermod -aG docker $USER
exit                 # IMPORTANT: log out fully and SSH back in for the group to apply
ssh ubuntu@<EC2_PUBLIC_IP>
```

> If `docker` still says *"permission denied … /var/run/docker.sock"*, the group hasn't
> applied yet — fully disconnect the SSH session and reconnect (a subshell/`newgrp` is
> not enough). As a stopgap you can prefix commands with `sudo`.

### 5.4 Get the code

Clone into your **home directory** (`/opt` is root-owned and will fail with
*"Permission denied"*):

```bash
cd ~
git clone https://github.com/sakshamGritbyte/skydiveos-autoedit.git
cd skydiveos-autoedit
```

### 5.5 Create the `.env` (secrets — never committed)

```bash
nano .env
```

```ini
# Claude (used by the selfie package's AI editor)
ANTHROPIC_API_KEY=<your-key>

# S3 storage (only if you use S3)
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
AWS_REGION=ap-south-1
AWS_S3_BUCKET_NAME=<your-bucket>

# No cameras on the cloud box -> discovery OFF (see "Footage ingestion" below)
ENABLE_AUTO_DISCOVERY=0

# Optional, add when known:
# MONGO_URL=<srv-url>          # only for the camera registry / instructor auth
# MONGO_DB=skydiveos
# SKYDIVEOS_API_BASE=https://<real-url>   # callback target; do NOT use localhost here
```

**Do NOT put these in `.env`** — they are set by `docker-compose.yml` or unused on the
cloud box: `REDIS_URL`, `JOBS_ROOT`, `RAW_STORAGE_ROOT`, `CAMERA_SCANNER`,
`DISCOVERY_*`, `ENFORCE_INSTRUCTOR_AUTH`.

### 5.6 Build and start

```bash
docker compose up -d --build      # first build ~3–5 min (downloads MediaPipe/OpenCV)
```

### 5.7 Verify

```bash
docker compose ps                          # all three should be "Up"
docker compose logs api    | tail -20      # "Uvicorn running on http://0.0.0.0:8000"
docker compose logs worker | tail -20      # "celery@... ready"
curl http://localhost:8000/docs            # API is up
```

From a browser: **`http://<EC2_PUBLIC_IP>:8000/docs`** (the Swagger UI).

Confirm the heavy stack works end to end (this initialises MediaPipe — the step that
previously failed, see Troubleshooting):

```bash
docker compose exec worker python -c \
  "from analysis.score import FreefallScorer; FreefallScorer().close(); print('MEDIAPIPE OK')"
```

A successful run prints `MEDIAPIPE OK` (after a one-time model download). The
`XNNPACK delegate for CPU` line confirms MediaPipe is running on the CPU (expected).

---

## 6. Troubleshooting (issues we actually hit)

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Permission denied` cloning into `/opt` | `/opt` is root-owned | Clone into `~` instead |
| `permission denied … docker.sock` | `usermod -aG docker` not applied to the session | **Fully** log out of SSH and reconnect (not `newgrp`); or use `sudo` |
| Job fails: `libGLESv2.so.2: cannot open shared object file` | MediaPipe needs GLES/EGL system libs the base image lacked | Already fixed in the Dockerfile (`libgles2`, `libegl1`, `libsm6`, `libxext6`, `libxrender1`). `git pull && docker compose up -d --build` |
| Worker warns *"running with superuser privileges"* | Celery runs as root in the container | Harmless for this setup; ignore. |
| API can't be reached from a browser | Security group port 8000 not open | Add the inbound rule (§5.2) |

A job staying `queued` is normal until footage is uploaded — `POST /jobs` only creates
the record; processing starts when footage is attached.

---

## 7. Day-2 operations

```bash
docker compose logs -f worker             # watch processing live
docker compose logs -f api                # API logs
docker compose restart worker             # restart the worker
docker compose ps                         # status
df -h                                     # check disk usage (footage grows here)

# Deploy new code:
git pull && docker compose up -d --build

# Stop everything (and stop paying for compute — stop the EC2 instance too):
docker compose down
```

To re-run a job that already has footage on disk (e.g. after a fix):

```bash
docker compose exec worker python -c \
  "from api.tasks import process_selfie_package; process_selfie_package.delay('<JOB_ID>')"
```

---

## 8. Footage ingestion model (important)

This cloud VM is the **processing** server. Footage reaches it **over the internet**
(an upload), not by the VM talking to a camera:

- A GoPro's WiFi/Bluetooth are **local, short-range** radios — a camera at the dropzone
  **cannot connect to an EC2 instance** in a datacenter. Auto-discovery/pull therefore
  must run on a machine **physically near the cameras** (a dropzone kiosk/edge PC with
  the Open GoPro SDK), which then uploads to S3 / the API.
- That is why `ENABLE_AUTO_DISCOVERY=0` on the cloud box. The cloud receives footage via
  `POST /jobs/{id}/upload` (and, for Ultimate, once per camera with a `camera_role`).

So the cloud handles **edit/render**; a local device (later) handles **camera pull**.

---

## 9. HTTPS + domain (recommended for production)

Server-to-server calls (e.g. SkydiveOS backend → this API) work over plain HTTP, but
browser calls to an HTTP API are blocked (mixed content), and traffic is unencrypted.
For production, front the API with **Caddy** (automatic Let's Encrypt TLS):

1. Buy/own a domain and create a **subdomain** A-record (e.g.
   `autoedit.yourdomain.com`) pointing to the EC2 public IP. (SSL cannot be issued for a
   bare IP — a domain is required.)
2. Add the `caddy` service shown in `Caddyfile`, set your domain, ensure ports 80/443
   are open, and `docker compose up -d`. Caddy provisions and renews the certificate
   automatically.
3. After that, you can close port 8000 and use `https://autoedit.yourdomain.com`.

A single root domain can serve both backends via **separate subdomains** (e.g.
`app.yourdomain.com` for SkydiveOS, `autoedit.yourdomain.com` for this API) — each with
its own cert, each A-record pointing at its own server.

---

## 10. Scaling later (not needed for launch)

- **Hardware encoding:** add an env-selectable encoder (`h264_nvenc` on a GPU box,
  `h264_videotoolbox` on Mac) to offload encoding from the CPU — a large speedup. The
  current build uses CPU `libx264`.
- **Separate the worker:** move the worker to its own (GPU) machine when one box isn't
  enough — this needs shared storage (S3 or a network volume) since the API and worker
  no longer share a local disk.
- **Pre-built images:** build once and push to a registry (Docker Hub / ECR), then have
  EC2 `docker compose pull` instead of building on the box.
