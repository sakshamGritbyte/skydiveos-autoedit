#!/usr/bin/env bash
# One-command SD-card dropzone stack: API + worker (+ the local SkydiveOS bridge).
#
# After this is up the flow is hands-off: insert a GoPro SD card → discovery pulls
# it, the QR marker identifies the instructor, the raw-upload consumer matches the
# load and creates the job, auto-edit renders, AUTO_DELIVER emails the customer.
# Ctrl-C stops everything it started.
#
# TWO MODES — who consumes discovery's raw-upload notification:
#
#   bridge (default)   scripts/skydiveos_bridge.py on :9000, this repo's local
#                      stand-in. Use when the SkydiveOS backend isn't running, or to
#                      test this module in isolation.
#
#   --skydiveos [URL]  the REAL SkydiveOS backend (default http://localhost:8001,
#                      pass a URL for another host). The bridge is not started.
#                      Requires that backend to implement the staff_id raw-upload
#                      consumer and to send Authorization: Bearer $AUTO_EDIT_API_KEY
#                      on its calls to this API.
#
# The operator's "copying… / safe to remove" banner is a SECOND consumer, and not the
# same one: only the real SkydiveOS backend renders it. So in bridge mode the card
# status is pushed to $SKYDIVEOS_URL (the real backend) while the raw-upload notify
# still goes to the bridge — the bridge has a route for the snapshot too, so pointing
# both at it would be accepted and land somewhere no operator can see. Override with
# CARD_STATUS_URL when the operator screen is on another host.
#
# Needs .env configured: MONGO_URL + MONGO_DB (the matcher — MONGO_DB, not just the
# Node backend's DB_NAME), S3_BUCKET, CAMERA_CLOCK_TZ, SMTP_* (the delivery email),
# REDIS_URL (the worker).
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="bridge"
SKYDIVEOS_URL="http://localhost:8001"
while [ $# -gt 0 ]; do
  case "$1" in
    --skydiveos)
      MODE="skydiveos"
      case "${2-}" in -*|"") ;; *) SKYDIVEOS_URL="$2"; shift ;; esac
      ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

export ENABLE_AUTO_DISCOVERY=1
export CAMERA_SCANNER=sdcard
export AUTO_DELIVER=1
# Exported (not left to .env) so the mode chosen here always wins: api.config loads
# .env WITHOUT overriding the environment, so a stale SKYDIVEOS_API_BASE in .env
# cannot silently send the hand-off somewhere other than the mode you asked for.
if [ "$MODE" = "skydiveos" ]; then
  export SKYDIVEOS_API_BASE="$SKYDIVEOS_URL"
else
  export SKYDIVEOS_API_BASE="http://localhost:9000"
  # The card-ingest snapshot's consumer is the operator's screen, which only the real
  # SkydiveOS backend serves — so it does NOT follow the notify to the bridge. Left to
  # default, the push would be 200-accepted by the bridge and never reach an operator.
  [ -n "${CARD_STATUS_URL-}" ] || export CARD_STATUS_URL="$SKYDIVEOS_URL"
fi

PY="${PY:-.venv/bin/python}"

pids=()
cleanup() { echo; echo "stopping stack..."; kill "${pids[@]}" 2>/dev/null || true; wait || true; }
trap cleanup EXIT INT TERM

echo "[stack] celery worker"
"$PY" -m celery -A api.celery_app.celery_app worker -l info --concurrency 1 &
pids+=($!)

if [ "$MODE" = "bridge" ]; then
  echo "[stack] SkydiveOS bridge on :9000 (local stand-in for the raw-upload consumer)"
  "$PY" scripts/skydiveos_bridge.py --port 9000 --api http://localhost:8000 &
  pids+=($!)
else
  echo "[stack] raw-upload consumer: REAL SkydiveOS at ${SKYDIVEOS_API_BASE} (bridge not started)"
  if ! curl -s -o /dev/null --max-time 5 -XPOST "${SKYDIVEOS_API_BASE}/api/media/raw-upload" \
       -H 'Content-Type: application/json' -d '{}'; then
    echo "         WARNING: ${SKYDIVEOS_API_BASE} is not answering — the hand-off will fail." >&2
  fi
fi

echo "[stack] card-status banner pushed to ${CARD_STATUS_URL:-$SKYDIVEOS_API_BASE}/api/media/ingest-cards/status"
echo "[stack] auto-edit API on :8000 (discovery: CAMERA_SCANNER=sdcard)"
"$PY" -m uvicorn api.app:app --host 127.0.0.1 --port 8000 &
pids+=($!)

echo
echo "Stack up (${MODE} mode). Insert a GoPro SD card — everything from here is automatic."
wait -n
