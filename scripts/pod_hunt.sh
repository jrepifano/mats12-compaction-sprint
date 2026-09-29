#!/usr/bin/env bash
# Pod hunt for E1: retry scripts/pod_up.py across the GPU fallback order (H100 community ->
# H100 NVL -> H100 secure -> A100-SXM4 80GB; implementation-plan §5) every 20 s until a pod is
# created, then push + set up (pod_push.sh) and, if it exists, run the E1 preflight
# (scripts/e1_preflight.py, U9) in the peft venv, which must print PREFLIGHT PASS.
# Port of ../mats12_neel_working/scripts/pod_hunt.sh. Changes: the GPU list, rates and order
# live in emx.runpod.GPU_FALLBACK (pod_up.py walks it; the budget gate runs before every create);
# no stage/setting; --dry-run prints pod_up's requests once and exits.
# Exit 0 = POD READY, 3 = push or preflight failed (pod kept for inspection), 1 = gave up.
# Usage: bash scripts/pod_hunt.sh [--slot _A] [--max-minutes 180] [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
SLOT=_A; MAXMIN=180; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --slot) SLOT="${2:?}"; shift 2 ;;
    --max-minutes) MAXMIN="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
envval() { grep -m1 "^$1=" .env | cut -d= -f2- ; }
if [ "$DRY" = "1" ]; then
  exec uv run python scripts/pod_up.py --slot "$SLOT" --dry-run
fi
[ -f logs/watchdog_E1.stop ] && { echo "watchdog stop file present: start scripts/pod_watchdog.sh first" >&2; exit 1; }
pgrep -f "pod_watchdog.sh" >/dev/null || { echo "no pod_watchdog.sh running: start it first" >&2; exit 1; }
deadline=$(( $(date +%s) + MAXMIN * 60 )); n=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  n=$((n+1))
  if [ -z "$(envval "RUNPOD_POD_ID$SLOT")" ]; then
    echo "$(date -u +%FT%TZ) attempt $n: pod_up slot $SLOT across the fallback order"
    rc=0; uv run python scripts/pod_up.py --slot "$SLOT" || rc=$?
    if [ "$rc" -ne 0 ]; then
      # 1 = no GPU created (retry); 4 = budget gate refused; anything else: stop
      [ "$rc" -eq 1 ] || { echo "$(date -u +%FT%TZ) pod_up exited $rc (budget gate?); stopping"; exit 1; }
      sleep 20; continue
    fi
  fi
  bash scripts/pod_push.sh --slot "$SLOT" || { echo "$(date -u +%FT%TZ) pod_push failed on slot $SLOT (pod kept for inspection)"; exit 3; }
  if [ -f scripts/e1_preflight.py ]; then
    host=$(envval "POD_SSH_HOST$SLOT"); port=$(envval "POD_SSH_PORT$SLOT")
    pf=$(ssh -p "$port" -o StrictHostKeyChecking=accept-new "root@$host" \
      "cd /ws/emx && /ws/venv-peft/bin/python scripts/e1_preflight.py" 2>&1 | tail -3) \
      || { echo "$(date -u +%FT%TZ) PREFLIGHT ssh failed on slot $SLOT"; exit 3; }
    echo "$pf"
    echo "$pf" | grep -q "PREFLIGHT PASS" || { echo "$(date -u +%FT%TZ) PREFLIGHT FAILED on slot $SLOT (pod kept for inspection)"; exit 3; }
  else
    echo "$(date -u +%FT%TZ) scripts/e1_preflight.py not present yet; preflight skipped"
  fi
  echo "$(date -u +%FT%TZ) POD READY slot $SLOT after $n attempts"; exit 0
done
echo "$(date -u +%FT%TZ) GAVE UP after $n attempts ($MAXMIN min)"; exit 1
