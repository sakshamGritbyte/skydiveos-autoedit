# Deployment (single VM, Docker Compose)

Runs the API + Celery worker + Redis on one machine. The pipeline is CPU-bound
(software x264 + MediaPipe), so use a **compute-optimized** instance, not a burstable
(t2/t3) one which throttles under sustained encode load.

## 1. Instance
- **AWS `c7i.2xlarge`** (8 vCPU / 16 GB) — or `c7a.2xlarge` (AMD, cheaper). Ubuntu 22.04.
- **Root disk: 100 GiB gp3 minimum** (the image is ~3–4 GB; raw GoPro footage is
  ~10–20 GB *per Ultimate job*). The default 8 GiB is far too small.

## 2. Install Docker
```bash
curl -fsSL https://get.docker.com | sh
```

## 3. Get the code
```bash
cd /opt
git clone https://github.com/sakshamGritbyte/skydiveos-autoedit.git
cd skydiveos-autoedit
git checkout feat/selfie-ultimum-pipeline
```

## 4. Create `.env` (secrets — never committed)
```bash
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
# optional — only if using S3 / SkydiveOS integration:
# S3_BUCKET=your-bucket
# SKYDIVEOS_API_BASE=https://your-skydiveos.example.com
EOF
```
> If `ANTHROPIC_API_KEY` is unset the pipeline still runs (deterministic house-cut),
> it just skips the AI editor — it won't crash.

## 5. Up
```bash
docker compose up -d --build      # first build: ~3–5 min
docker compose ps
docker compose logs -f worker     # watch it become ready, and watch jobs run
```

## 6. Smoke test
```bash
curl http://localhost:8000/docs                 # API up?
# then drive it from the browser: http://<VM_IP>:8000/docs
```

## 7. (Optional) HTTPS with Caddy
Point a domain's A-record at the VM, add the `caddy` service from the snippet in
`Caddyfile`, set your domain, and `docker compose up -d`. Caddy auto-provisions TLS.
Skip this for a quick demo — just use `http://<VM_IP>:8000`.

## Day-2 notes
- **MediaPipe model** downloads on first job — the worker needs outbound internet
  (or bake the model in and set `FACE_LANDMARKER_MODEL`).
- **Disk fills up:** each job keeps its raw footage under `jobs/<id>/raw/`. Prune old
  jobs or offload to S3 as volume grows.
- **Live camera pulls are NOT available in the container** (the Open GoPro SDK under
  `vendor/` is excluded via `.dockerignore`). This deploy is the upload-based flow:
  SkydiveOS/clients `POST` footage to the API. That's the intended cloud model.
- **Scaling later:** when one box isn't enough, move the worker to its own machine
  (needs shared storage — S3 or a network volume) and/or add `h264_nvenc` + a GPU box.

## Common commands
```bash
docker compose logs -f worker            # processing logs
docker compose restart worker            # restart worker
git pull && docker compose up -d --build # deploy new code
docker compose down                      # stop everything
```
