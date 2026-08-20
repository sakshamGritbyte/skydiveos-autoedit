#!/usr/bin/env bash
#
# Cron wrapper around scripts/prune_jobs.py — the EC2 disk-retention sweep.
#
# The pipeline's durable copies live in S3; jobs/ and raw-storage/ are working copies
# that only grow (~25-35 GB/day at real dropzone volume, so a 200 GB box fills in about
# a week). prune_jobs.py reclaims them under one rule: delete only what a size-matched
# HeadObject confirms S3 already holds. This wrapper is what cron actually calls.
#
# It exists because cron is a hostile environment for this script, in four ways:
#
#   * cron has almost no environment. The pruner needs S3 credentials and the storage
#     roots, so the repo's .env is loaded here.
#   * cron starts in $HOME. RAW_STORAGE_ROOT defaults to the RELATIVE './raw-storage',
#     so the sweep must run with the repo as cwd or it silently prunes nothing.
#   * a sweep over a full disk can outlive its weekly slot. An flock keeps two runs
#     from walking the same tree (the second exits quietly rather than queueing).
#   * cron mails output into a void nobody reads. Everything is timestamped into
#     logs/prune-jobs.log instead, with the disk's before/after so the log answers
#     "is this actually keeping up?" without any other tooling.
#
# Usage:
#   bash deploy/ec2/prune-jobs.sh                  # the real sweep
#   bash deploy/ec2/prune-jobs.sh --dry-run        # decide everything, delete nothing
#   bash deploy/ec2/prune-jobs.sh --raw-days 1     # any prune_jobs.py flag passes through
#
# Install it on a weekly timer with deploy/ec2/install-prune-cron.sh.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${REPO_ROOT}"

LOG_DIR="${REPO_ROOT}/logs"
LOG_FILE="${LOG_DIR}/prune-jobs.log"
LOCK_FILE="${LOG_DIR}/.prune-jobs.lock"
mkdir -p "${LOG_DIR}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*"; }

# Re-run under flock so only one sweep walks the tree at a time. -n: if another holds
# the lock, leave immediately — a sweep is idempotent and the next slot catches up,
# whereas queueing would stack runs on a box already struggling for disk.
#
# -E 99 is what makes "lock was busy" distinguishable from "the sweep itself exited 1".
# Without it a contended run looks like a failure and cron starts mailing errors for
# what is the designed behaviour. Not `exec`, for the same reason: exec replaces this
# process, so the exit code could never be inspected.
# Guarded because flock is util-linux; without it (a Mac) we just run unlocked.
if [ -z "${_PRUNE_LOCKED:-}" ] && command -v flock >/dev/null 2>&1; then
  export _PRUNE_LOCKED=1
  rc=0
  flock -E 99 -n "${LOCK_FILE}" "$0" "$@" || rc=$?
  if [ "${rc}" -eq 99 ]; then
    log "another prune sweep holds the lock — skipping this run" >> "${LOG_FILE}"
    exit 0
  fi
  exit "${rc}"
fi

{
  log "=== prune sweep starting (repo: ${REPO_ROOT}) ==="

  # `set -a` exports every assignment in .env, which is how the pruner (and boto3)
  # pick up credentials and roots. Missing .env is not fatal: a systemd/container
  # deployment may already have the environment in place.
  if [ -f "${REPO_ROOT}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env"
    set +a
    log "loaded .env"
  else
    log "no .env found — relying on the inherited environment"
  fi

  PYTHON="${REPO_ROOT}/.venv/bin/python"
  if [ ! -x "${PYTHON}" ]; then
    log "ERROR: ${PYTHON} not found — run 'uv sync' in ${REPO_ROOT}"
    exit 1
  fi

  before="$(df -h "${REPO_ROOT}" | awk 'NR==2 {print $4" free of "$2" ("$5" used)"}')"
  log "disk before: ${before}"

  # The pruner never raises on a single bad job, but it does exit 2 when S3_BUCKET /
  # AWS_S3_BUCKET_NAME is unset — nothing can be verified, so nothing is deleted. That
  # is a silent no-op week after week, so it is called out here rather than swallowed.
  status=0
  "${PYTHON}" scripts/prune_jobs.py "$@" || status=$?

  if [ "${status}" -eq 2 ]; then
    log "ERROR: no S3 bucket configured — the sweep verified nothing and freed nothing."
    log "       Set S3_BUCKET (or AWS_S3_BUCKET_NAME) in ${REPO_ROOT}/.env."
  elif [ "${status}" -ne 0 ]; then
    log "ERROR: prune_jobs.py exited ${status}"
  fi

  after="$(df -h "${REPO_ROOT}" | awk 'NR==2 {print $4" free of "$2" ("$5" used)"}')"
  log "disk after:  ${after}"
  log "=== prune sweep finished (exit ${status}) ==="
  echo

  exit "${status}"
} >> "${LOG_FILE}" 2>&1
