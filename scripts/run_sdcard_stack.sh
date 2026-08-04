#!/usr/bin/env bash
# One-command SD-card dropzone stack: API + worker + local SkydiveOS bridge.
#
# After this is up, the flow is fully hands-off: insert a GoPro SD card →
# discovery pulls it, the QR marker identifies the instructor, the bridge
# matches the load and creates the job, auto-edit renders, AUTO_DELIVER emails
# the customer. Ctrl-C stops all three processes.
#
# Needs .env configured: MONGO_URL, S3_BUCKET, CAMERA_CLOCK_TZ, SMTP_* (for the
# delivery email), and a Redis for the worker (REDIS_URL).
set -euo pipefail
cd "$(dirname "$0")/.."

export ENABLE_AUTO_DISCOVERY=1
export CAMERA_SCANNER=sdcard
export AUTO_DELIVER=1
export SKYDIVEOS_API_BASE="${SKYDIVEOS_API_BASE:-http://localhost:9000}"

PY="${PY:-.venv/bin/python}"

pids=()
cleanup() { echo; echo "stopping stack..."; kill "${pids[@]}" 2>/dev/null || true; wait || true; }
trap cleanup EXIT INT TERM

echo "[stack] celery worker"
"$PY" -m celery -A api.celery_app.celery_app worker -l info --concurrency 1 &
pids+=($!)

echo "[stack] SkydiveOS bridge on :9000 (local stand-in for the raw-upload consumer)"
"$PY" scripts/skydiveos_bridge.py --port 9000 --api http://localhost:8000 &
pids+=($!)

echo "[stack] auto-edit API on :8000 (discovery: CAMERA_SCANNER=sdcard)"
"$PY" -m uvicorn api.app:app --host 127.0.0.1 --port 8000 &
pids+=($!)

echo
echo "Stack up. Insert a GoPro SD card — everything from here is automatic."
wait -n
