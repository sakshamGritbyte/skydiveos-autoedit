#!/usr/bin/env bash
# customer-email-sender.sh — audit or set CUSTOMER_EMAIL_SENDER on EVERY auto-edit
# stack on the media EC2 (BUG 392: one customer email per jump, not two).
#
#   bash deploy/ec2/customer-email-sender.sh audit            # read-only, all stacks
#   bash deploy/ec2/customer-email-sender.sh set skydiveos    # write .env + `up -d` + audit
#   bash deploy/ec2/customer-email-sender.sh set pipeline     # the revert
#   STACKS="$HOME/autoedit-northshore" bash deploy/ec2/customer-email-sender.sh audit
#
# Why a script: the flag is a per-stack .env value read ONCE at process start
# (`api.config.get_settings` is lru_cached), so it has to be set in each stack's own
# untracked .env and the containers RE-CREATED — `docker compose up -d` does that
# because env_file values are part of the container config hash; `docker compose
# restart` does not re-read env_file and silently leaves the old value running.
#
# The audit checks the four things that actually prove the gate is live per stack:
#   1. checkout — HEAD contains 7986199 (the commit that added the gate);
#   2. image    — the RUNNING worker's api/delivery.py contains the gate (a stale image
#                 built before a `git pull` has the new checkout but the old code);
#   3. env      — the running worker AND api see the value (`printenv`, not the file);
#   4. effect   — worker log lines "customer email delegated to SkydiveOS", and no
#                 recently-delivered job.json carries `email_sent_at` (that field means
#                 THIS service emailed the customer — with the flag on it must never appear).
set -uo pipefail

MODE="${1:-audit}"
VALUE="${2:-}"
GATE_COMMIT="7986199"
STACKS="${STACKS:-$HOME/skydiveos-autoedit $HOME/autoedit-demo $HOME/autoedit-northshore $HOME/autoedit-southshore}"

case "$MODE" in
  audit) ;;
  set)
    case "$VALUE" in
      skydiveos|pipeline) ;;
      *) echo "usage: $0 set skydiveos|pipeline" >&2; exit 2 ;;
    esac ;;
  *) echo "usage: $0 audit | set skydiveos|pipeline" >&2; exit 2 ;;
esac

rc=0
for dir in $STACKS; do
  echo "================================================================"
  echo "STACK $dir"
  if [ ! -f "$dir/.env" ]; then
    echo "  !! no .env — skipping"; rc=1; continue
  fi
  cd "$dir" || { rc=1; continue; }

  if [ "$MODE" = "set" ]; then
    if grep -q '^CUSTOMER_EMAIL_SENDER=' .env; then
      sed -i.bak."$(date +%F)" "s/^CUSTOMER_EMAIL_SENDER=.*/CUSTOMER_EMAIL_SENDER=$VALUE/" .env
    else
      cp .env ".env.bak.$(date +%F)"
      printf '\nCUSTOMER_EMAIL_SENDER=%s\n' "$VALUE" >> .env
    fi
    echo "  .env now: $(grep '^CUSTOMER_EMAIL_SENDER=' .env)"
    # up -d (NOT restart): env_file is baked in at container create, so the api and
    # worker are re-created only because their config hash changed. No --build: the
    # code is unchanged; a rebuild here would only add minutes.
    docker compose up -d api worker || rc=1
  fi

  # 1. checkout
  head=$(git rev-parse --short HEAD 2>/dev/null || echo '?')
  if git merge-base --is-ancestor "$GATE_COMMIT" HEAD 2>/dev/null; then
    echo "  checkout : $head  (contains gate commit $GATE_COMMIT)"
  else
    echo "  checkout : $head  !! gate commit $GATE_COMMIT NOT in HEAD — git pull first"; rc=1
  fi

  # 2. running image
  if docker compose exec -T worker grep -q 'customer_email_sender == "skydiveos"' /app/api/delivery.py 2>/dev/null; then
    echo "  image    : worker's api/delivery.py has the gate"
  else
    echo "  image    : !! running worker has NO gate — stale image, run: docker compose up -d --build"; rc=1
  fi

  # 3. env as seen by the processes
  file_val=$(grep '^CUSTOMER_EMAIL_SENDER=' .env | tail -1 | cut -d= -f2- || true)
  for svc in api worker; do
    live=$(docker compose exec -T "$svc" printenv CUSTOMER_EMAIL_SENDER 2>/dev/null || echo '<unset>')
    echo "  env      : $svc sees '${live}'   (.env says '${file_val:-<unset>}')"
    if [ "${live}" != "${file_val:-<unset>}" ]; then
      echo "             !! mismatch — container predates the .env edit; run: docker compose up -d"; rc=1
    fi
  done

  # 4. effect
  delegated=$(docker compose logs --since 72h worker 2>/dev/null | grep -c 'customer email delegated to SkydiveOS' || true)
  emailed=$(docker compose logs --since 72h worker 2>/dev/null | grep -c 'gallery email sent to\|delivery email sent to' || true)
  echo "  log 72h  : delegated-to-SkydiveOS=$delegated   sent-by-pipeline=$emailed"
  # Delivered jobs whose job.json carries email_sent_at = this service emailed them.
  docker compose exec -T worker sh -c '
    n=0; stamped=0
    for f in $(ls -t /data/jobs/*/job.json 2>/dev/null | head -20); do
      grep -q "\"status\": *\"delivered\"" "$f" || continue
      n=$((n+1))
      if grep -q "\"email_sent_at\": *[0-9]" "$f"; then
        stamped=$((stamped+1)); echo "             email_sent_at set on $(dirname "$f" | xargs basename)"
      fi
    done
    echo "  jobs     : $n recent delivered, $stamped stamped email_sent_at (must be 0 for jobs delivered AFTER the flip)"
  ' 2>/dev/null || echo "  jobs     : (worker not running)"
done
echo "================================================================"
[ $rc -eq 0 ] && echo "OK" || echo "PROBLEMS FOUND (see !! lines)"
exit $rc
