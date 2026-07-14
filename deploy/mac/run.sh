#!/usr/bin/env bash
#
# Wrapper launchd invokes to run the ingest backend. Kept as a script (rather than
# putting the command straight in the plist) so PATH/working-dir resolution is
# reliable regardless of launchd's minimal environment, and so the .env is loaded
# from the repo root by api.config's dotenv call.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# launchd starts with a bare PATH; add the usual Homebrew + uv locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"

HOST="${INGEST_HOST:-0.0.0.0}"
PORT="${INGEST_PORT:-8000}"

# Use --no-sync so `uv run` reuses the .venv install.sh populated (uv sync + the
# separately-installed Open GoPro SDK) WITHOUT re-syncing to the lockfile, which
# would strip the SDK back out and break real-camera pulls.
UV_RUN=(uv run --no-sync)

# For real-camera modes (ble/usb) the hardware SDK (open_gopro) must be present in
# that .venv. Warn loudly rather than fail silently with zero cameras discovered.
SCANNER="$(grep -E '^CAMERA_SCANNER=' .env 2>/dev/null | tail -n1 | cut -d= -f2 | tr -d '[:space:]')"
case "${SCANNER}" in
  ble|usb)
    if ! "${UV_RUN[@]}" python -c "import open_gopro" >/dev/null 2>&1; then
      echo "WARNING: CAMERA_SCANNER=${SCANNER} but the Open GoPro SDK (open_gopro)" >&2
      echo "         is not installed in .venv — real-camera pulls will fail." >&2
      echo "         Re-run: bash deploy/mac/install.sh" >&2
    fi
    ;;
esac

exec "${UV_RUN[@]}" uvicorn api.app:app --host "${HOST}" --port "${PORT}"
