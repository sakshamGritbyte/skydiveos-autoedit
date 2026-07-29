#!/usr/bin/env bash
#
# Install + (re)load the automatic archive sync as a launchd timer on the client Mac.
# Every 5 minutes it pulls the customer-named jump archive from EC2 into
# <repo>/jump-archive (see sync-archive.sh — raw masters are excluded by design).
#
# Usage:
#   EC2_HOST=ubuntu@1.2.3.4 bash deploy/mac/load-archive-sync.sh   # install + start
#   bash deploy/mac/load-archive-sync.sh unload                    # stop + remove
#
# Prerequisite: non-interactive SSH to the EC2 box for the logged-in user
# (ssh-copy-id ubuntu@<ec2-ip> once). Verify with:
#   ssh -o BatchMode=yes ubuntu@<ec2-ip> true && echo ok
#
set -euo pipefail

LABEL="com.skydiveos.archive-sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${AGENTS_DIR}/${LABEL}.plist"
TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist.template"

if [ "${1:-}" = "unload" ]; then
  echo "==> Unloading ${LABEL}"
  launchctl unload "${PLIST_DEST}" 2>/dev/null || true
  rm -f "${PLIST_DEST}"
  echo "    removed ${PLIST_DEST}"
  exit 0
fi

: "${EC2_HOST:?set EC2_HOST, e.g. EC2_HOST=ubuntu@1.2.3.4 bash deploy/mac/load-archive-sync.sh}"

# Fail here, with a clear message, rather than silently every 5 minutes in launchd.
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "${EC2_HOST}" true 2>/dev/null; then
  echo "!!  Cannot SSH to ${EC2_HOST} non-interactively."
  echo "    Run:  ssh-copy-id ${EC2_HOST}   and retry."
  exit 1
fi

mkdir -p "${AGENTS_DIR}" "${REPO_ROOT}/logs" "${REPO_ROOT}/jump-archive"

echo "==> Rendering plist (repo: ${REPO_ROOT}, ec2: ${EC2_HOST})"
sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" -e "s|__EC2_HOST__|${EC2_HOST}|g" \
  "${TEMPLATE}" > "${PLIST_DEST}"

echo "==> Reloading timer"
launchctl unload "${PLIST_DEST}" 2>/dev/null || true
launchctl load "${PLIST_DEST}"

echo
echo "Archive sync '${LABEL}' loaded — first run now, then every 5 minutes."
echo "  archive lands in:  ${REPO_ROOT}/jump-archive/{date}/{instructor}/{customer}/"
echo "  watch it:          tail -f ${REPO_ROOT}/logs/archive-sync.out.log"
echo "  run once by hand:  EC2_HOST=${EC2_HOST} bash deploy/mac/sync-archive.sh"
echo "  remove:            bash deploy/mac/load-archive-sync.sh unload"
