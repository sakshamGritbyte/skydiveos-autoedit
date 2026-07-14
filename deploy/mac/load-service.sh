#!/usr/bin/env bash
#
# Install + (re)load the ingest backend as a launchd service on the client Mac.
# Substitutes the real repo path into the plist template, drops it in
# ~/Library/LaunchAgents, and boots it. Re-run any time to pick up changes.
#
# Usage:
#   bash deploy/mac/load-service.sh          # install + start
#   bash deploy/mac/load-service.sh unload   # stop + remove the service
#
set -euo pipefail

LABEL="com.skydiveos.ingest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${AGENTS_DIR}/${LABEL}.plist"
TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist.template"

# Unload mode: stop and remove.
if [ "${1:-}" = "unload" ]; then
  echo "==> Unloading ${LABEL}"
  launchctl unload "${PLIST_DEST}" 2>/dev/null || true
  rm -f "${PLIST_DEST}"
  echo "    removed ${PLIST_DEST}"
  exit 0
fi

mkdir -p "${AGENTS_DIR}" "${REPO_ROOT}/logs"

echo "==> Rendering plist for ${REPO_ROOT}"
sed "s|__REPO_ROOT__|${REPO_ROOT}|g" "${TEMPLATE}" > "${PLIST_DEST}"

echo "==> Reloading service"
launchctl unload "${PLIST_DEST}" 2>/dev/null || true
launchctl load "${PLIST_DEST}"

echo
echo "Service '${LABEL}' loaded. Useful commands:"
echo "  launchctl list | grep ${LABEL}          # is it running? (PID + last exit code)"
echo "  tail -f ${REPO_ROOT}/logs/ingest.err.log  # live logs / errors"
echo "  bash deploy/mac/load-service.sh unload   # stop + remove"
