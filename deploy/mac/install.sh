#!/usr/bin/env bash
#
# One-shot installer for the SkydiveOS GoPro ingest backend on a client Mac.
#
# What it does (idempotent — safe to re-run):
#   1. Installs Homebrew (if missing) + python@3.11, ffmpeg, git
#   2. Installs `uv`
#   3. Installs Python deps from requirements.txt
#   4. Installs the Open GoPro hardware SDK (real cameras: BLE + WiFi)
#   5. Scaffolds a .env from .env.example if one doesn't exist yet
#
# It does NOT start the service or pair cameras — those are manual, one-time
# steps documented in GOPRO_INGEST_SETUP.md (and load-service.sh for launchd).
#
# Usage:
#   cd ~/skydiveos-autoedit
#   bash deploy/mac/install.sh
#
set -euo pipefail

# Resolve the repo root (this script lives in deploy/mac/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

echo "==> SkydiveOS ingest — Mac installer"
echo "    repo: ${REPO_ROOT}"

# ── 1. Homebrew ────────────────────────────────────────────────────────────
if ! command -v brew >/dev/null 2>&1; then
  echo "==> Installing Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Make brew available on Apple Silicon for the rest of this script.
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
else
  echo "==> Homebrew present"
fi

# ── 2. System packages ─────────────────────────────────────────────────────
echo "==> Installing python@3.11, ffmpeg, git"
brew install python@3.11 ffmpeg git

# ── 3. uv ──────────────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
else
  echo "==> uv present"
fi

# ── 4. Python deps ─────────────────────────────────────────────────────────
# There is no requirements.txt — deps live in pyproject.toml + uv.lock. `uv sync`
# creates the project .venv and installs the locked deps into it. run.sh later
# launches with `uv run --no-sync`, so it reuses THIS .venv without re-syncing
# (a re-sync would remove the hardware SDK we add in the next step).
echo "==> Syncing Python dependencies into .venv (uv sync)"
uv sync

# ── 5. Open GoPro hardware SDK (real cameras) ──────────────────────────────
# The SDK is a vendored path package, not a locked project dependency, so it is
# installed straight into the .venv `uv sync` just created (uv pip install auto-
# detects it). IMPORTANT: do not run a bare `uv sync` afterwards — it would strip
# this package back out. Re-run this installer to restore it if that happens.
SDK_PATH="./vendor/OpenGoPro/demos/python/sdk_wireless_camera_control"
if [ -d "${SDK_PATH}" ]; then
  echo "==> Installing Open GoPro hardware SDK into .venv"
  uv pip install "${SDK_PATH}"
else
  echo "!!  Open GoPro SDK not found at ${SDK_PATH}"
  echo "    Real cameras need it. Ensure the vendor/ submodule is checked out, then re-run."
fi

# ── 6. .env scaffold ───────────────────────────────────────────────────────
if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example (EDIT IT before starting the service)"
  cp .env.example .env
else
  echo "==> .env already exists — leaving it untouched"
fi

# ffmpeg version sanity check (need 6.0+ for the speed-ramp filter)
echo "==> ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1 || echo 'NOT FOUND')"

cat <<'EOF'

============================================================
 Install done. Next steps (see GOPRO_INGEST_SETUP.md):

 1. Edit .env  — set S3_BUCKET, AWS_* keys, SKYDIVEOS_API_BASE
                 (your EC2 URL), MONGO_URL, and:
                   ENABLE_AUTO_DISCOVERY=1
                   CAMERA_SCANNER=ble

 2. Grant Bluetooth permission to Terminal (for the manual pairing/test below):
       System Settings > Privacy & Security > Bluetooth > Terminal
    NOTE: this does NOT cover the launchd service (step 5) — see its own note.

 3. Pair each camera once (use the .venv python):
       uv run python -m ingest.pull --camera <serial> --pair \
         --name "<label>" --instructor-id <id>

 4. Smoke test one pull:
       uv run python -m ingest.pull --camera <serial> --list
       uv run python -m ingest.pull --camera <serial>

 5. Run as a boot service:
       bash deploy/mac/load-service.sh
    Then, the FIRST time it BLE-scans, macOS shows a Bluetooth permission
    prompt for the background service — APPROVE it (System Settings >
    Privacy & Security > Bluetooth). Until approved, the service scans but
    finds zero cameras. Terminal's permission does NOT carry over.
============================================================
EOF
