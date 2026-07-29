#!/usr/bin/env bash
#
# Pull the browsable jump archive from EC2 down to the dropzone Mac.
#
# The pipeline runs on EC2, so that is where the customer-named archive is built:
#
#     raw-storage/{jump date}/{instructor}/{customer}/{raw,edited,photos}/manifest.json
#
# The Mac only ever sees footage by CAMERA (raw-storage/_camera-staging/<id>/<date>/),
# because a pull knows the camera and the time but not the booking. But the Mac is where
# staff physically are, so this brings the finished, customer-named folders to them.
#
# `raw/` is EXCLUDED on purpose: the Mac already holds those masters in _camera-staging,
# and re-downloading multi-GB originals over a dropzone internet link is painful and
# pointless. What arrives is edited/, photos/ and manifest.json — what staff actually
# browse and hand to a customer.
#
# Direction is Mac -> EC2 (pull), because the Mac sits behind the dropzone's NAT and
# EC2 cannot reach it.
#
# Usage:
#   EC2_HOST=ubuntu@1.2.3.4 bash deploy/mac/sync-archive.sh
#   EC2_HOST=... DEST=~/jump-archive bash deploy/mac/sync-archive.sh
#   EC2_HOST=... DRY_RUN=1 bash deploy/mac/sync-archive.sh      # show, transfer nothing
#
# Needs SSH key auth to the EC2 box (ssh-copy-id once).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

: "${EC2_HOST:?set EC2_HOST, e.g. EC2_HOST=ubuntu@1.2.3.4}"
REMOTE_DIR="${REMOTE_DIR:-~/skydiveos-autoedit/raw-storage/}"
DEST="${DEST:-${REPO_ROOT}/jump-archive}"
DRY_RUN="${DRY_RUN:-}"

mkdir -p "${DEST}"

# --delete-after keeps the Mac a faithful mirror without removing anything until the
# transfer succeeded. raw/ and the camera staging dir never come down.
ARGS=(
  -az --partial --human-readable
  --delete-after
  --exclude 'raw/'
  --exclude '_camera-staging/'
  --exclude '.transferred.json'
)
[ -n "${DRY_RUN}" ] && ARGS+=(--dry-run --itemize-changes)

echo "==> syncing ${EC2_HOST}:${REMOTE_DIR}"
echo "    into ${DEST}"
[ -n "${DRY_RUN}" ] && echo "    (DRY RUN — nothing will be written)"

rsync "${ARGS[@]}" "${EC2_HOST}:${REMOTE_DIR}" "${DEST}/"

if [ -z "${DRY_RUN}" ]; then
  echo
  echo "==> jump archive on this Mac:"
  # One line per jump folder: date / instructor / customer, and what is in it.
  find "${DEST}" -mindepth 3 -maxdepth 3 -type d 2>/dev/null | sort | while read -r jump; do
    rel="${jump#"${DEST}"/}"
    videos=$(find "${jump}/edited" -type f 2>/dev/null | wc -l | tr -d ' ')
    photos=$(find "${jump}/photos" -type f 2>/dev/null | wc -l | tr -d ' ')
    printf "    %-52s %s video(s), %s photo(s)\n" "${rel}" "${videos}" "${photos}"
  done
  echo
  echo "    total: $(du -sh "${DEST}" 2>/dev/null | cut -f1)"
fi
