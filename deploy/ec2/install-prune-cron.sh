#!/usr/bin/env bash
#
# Install (or remove) the weekly disk-retention sweep in the current user's crontab.
#
# What it schedules: deploy/ec2/prune-jobs.sh, which runs scripts/prune_jobs.py with the
# repo's .env loaded. See that script for what is deleted and the "only what S3 confirms"
# rule that governs it.
#
# Weekly is the stated interim cadence, but it is the LOOSEST setting that works: at
# ~25-35 GB/day a 200 GB box fills between two Sunday runs, so pass SCHEDULE='30 3 * * *'
# for daily (which is what CLAUDE.md recommends for production) once you trust the log.
#
# Usage:
#   bash deploy/ec2/install-prune-cron.sh                      # weekly, Sun 03:30
#   SCHEDULE='30 3 * * *' bash deploy/ec2/install-prune-cron.sh   # daily instead
#   ARGS='--raw-days 1' bash deploy/ec2/install-prune-cron.sh     # tighter raw retention
#   bash deploy/ec2/install-prune-cron.sh uninstall             # remove the entry
#   bash deploy/ec2/install-prune-cron.sh show                  # print the entry, if any
#
# Idempotent: re-running replaces the existing entry rather than adding a second one.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# The marker is how we find OUR line again on reinstall/uninstall without disturbing
# anything else the operator has in their crontab.
MARKER="# skydiveos-autoedit disk-retention sweep (deploy/ec2/install-prune-cron.sh)"
SCHEDULE="${SCHEDULE:-30 3 * * 0}"   # Sunday 03:30 — quiet hours, well clear of jump ops
ARGS="${ARGS:-}"

# `crontab -l` exits non-zero when the user has no crontab at all; that is a normal
# starting state, not an error, so an empty listing stands in for it.
current_crontab() { crontab -l 2>/dev/null || true; }

# Drop our marker line and the command line that follows it.
without_our_entry() { current_crontab | grep -vF -e "${MARKER}" -e "${SCRIPT_DIR}/prune-jobs.sh"; }

case "${1:-install}" in
  show)
    entry="$(current_crontab | grep -F "${SCRIPT_DIR}/prune-jobs.sh" || true)"
    if [ -z "${entry}" ]; then
      echo "No prune sweep is installed in this user's crontab."
      exit 1
    fi
    echo "${entry}"
    exit 0
    ;;
  uninstall)
    if ! current_crontab | grep -qF "${SCRIPT_DIR}/prune-jobs.sh"; then
      echo "Nothing to remove — no prune sweep in this user's crontab."
      exit 0
    fi
    without_our_entry | crontab -
    echo "==> Removed the prune sweep from $(whoami)'s crontab."
    exit 0
    ;;
  install) ;;
  *)
    echo "usage: $0 [install|uninstall|show]" >&2
    exit 64
    ;;
esac

if [ ! -x "${REPO_ROOT}/.venv/bin/python" ]; then
  echo "!!  ${REPO_ROOT}/.venv/bin/python not found — run 'uv sync' first." >&2
  exit 1
fi

# Fail here, visibly, rather than every week into a log nobody is watching. The pruner
# can only delete what S3 confirms, so with no bucket the whole schedule is a no-op.
if ! grep -qE '^\s*(S3_BUCKET|AWS_S3_BUCKET_NAME)=\S' "${REPO_ROOT}/.env" 2>/dev/null; then
  echo "!!  Neither S3_BUCKET nor AWS_S3_BUCKET_NAME is set in ${REPO_ROOT}/.env."
  echo "    The sweep verifies every deletion against S3, so it would free nothing."
  exit 1
fi

mkdir -p "${REPO_ROOT}/logs"

# Absolute paths throughout: cron's PATH is minimal and its cwd is $HOME.
CRON_CMD="${SCHEDULE} /bin/bash ${SCRIPT_DIR}/prune-jobs.sh ${ARGS}"
{ without_our_entry; echo "${MARKER}"; echo "${CRON_CMD}"; } | crontab -

echo "==> Installed the disk-retention sweep in $(whoami)'s crontab:"
echo "      ${CRON_CMD}"
echo
echo "  log:           tail -f ${REPO_ROOT}/logs/prune-jobs.log"
echo "  test it now:   bash ${SCRIPT_DIR}/prune-jobs.sh --dry-run && tail -30 ${REPO_ROOT}/logs/prune-jobs.log"
echo "  change slot:   SCHEDULE='30 3 * * *' bash $0     # daily"
echo "  remove:        bash $0 uninstall"
