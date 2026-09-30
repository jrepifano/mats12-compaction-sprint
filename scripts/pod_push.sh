#!/usr/bin/env bash
# Sync the repo (incl. data/E1) to an E1 pod and run scripts/pod_setup.sh there.
# Port of ../mats12_neel_working/scripts/pod_push.sh. Changes: everything lives on the container
# disk under /ws (repo /ws/emx, caches /ws/hf_cache and /ws/uv_cache, venvs /ws/venv-*), so the
# /workspace relocation step is gone; no --setting/--stack; the pod .env allowlist is cut to
# HF_TOKEN, HF_USERNAME and non-secret settings, and a denylist check refuses to send anything
# that looks like an OpenAI/OpenRouter/RunPod key or the dataset password. .env is read per key,
# never sourced (sourcing would export every secret into every child process).
# Usage: bash scripts/pod_push.sh --slot _A [--no-setup]
set -euo pipefail
cd "$(dirname "$0")/.."

envval() { { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }  # a missing key is empty, not fatal under pipefail

SLOT=""; SETUP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --slot) SLOT="${2:?--slot needs a value}"; shift 2 ;;
    --no-setup) SETUP=0; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$SLOT" ] || { echo "usage: pod_push.sh --slot _A [--no-setup]" >&2; exit 2; }
POD_SSH_HOST=$(envval "POD_SSH_HOST$SLOT")
POD_SSH_PORT=$(envval "POD_SSH_PORT$SLOT")
POD_SSH_USER=$(envval POD_SSH_USER); POD_SSH_USER=${POD_SSH_USER:-root}
[ -n "$POD_SSH_HOST" ] && [ -n "$POD_SSH_PORT" ] || { echo "no pod in slot $SLOT; run pod_up.py first" >&2; exit 1; }

SSH="ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new $POD_SSH_USER@$POD_SSH_HOST"
RSYNC_SSH="ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"

# pod-safe env: explicit allowlist, never the whole .env. The file is both bash-sourced
# (pod_setup.sh) and dotenv-parsed, so values must be shell-inert: enforce a safe charset.
POD_ENV=$(mktemp)
trap 'rm -f "$POD_ENV"' EXIT
for key in HF_TOKEN HF_USERNAME; do
  val=$(envval "$key")
  [ -n "$val" ] || { echo "$key is empty in .env; refusing" >&2; exit 1; }
  case "$val" in
    *[!A-Za-z0-9._/:-]*) echo "unsafe characters in $key value; refusing" >&2; exit 1 ;;
  esac
  printf '%s=%s\n' "$key" "$val" >> "$POD_ENV"
done
# the pod has no .git (rsync excludes it): record which commit was pushed, and whether dirty
printf 'EMX_GIT_COMMIT=%s\nEMX_GIT_DIRTY=%s\n' "$(git rev-parse HEAD)" \
  "$( [ -z "$(git status --porcelain)" ] && echo 0 || echo 1 )" >> "$POD_ENV"
printf 'WANDB_MODE=offline\nTOKENIZERS_PARALLELISM=false\nHF_HOME=/ws/hf_cache\nEMX_EXP=E1\n' >> "$POD_ENV"
# denylist: no laptop-only secret may ever reach the pod
if grep -Eq '^(OPENAI|OPENROUTER|RUNPOD|EM_DATASET_PASSWORD|AZURE)' "$POD_ENV"; then
  echo "a laptop-only key is in the pod env; refusing" >&2; exit 1
fi
for key in OPENAI_API_KEY OPENROUTER_API_KEY RUNPOD_API_KEY EM_DATASET_PASSWORD; do
  val=$(envval "$key")
  if [ -n "$val" ] && grep -qF -- "$val" "$POD_ENV"; then
    echo "the value of $key appears in the pod env; refusing" >&2; exit 1
  fi
done

$SSH 'command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync); mkdir -p /ws/emx /ws/hf_cache /ws/uv_cache /ws/tmp'
rsync -rlptz -e "$RSYNC_SSH" --exclude .git --exclude .venv --exclude __pycache__ \
      --exclude .pytest_cache --exclude .ruff_cache --exclude data/raw --exclude papers \
      --exclude '*.pdf' --exclude .env --exclude results --exclude logs \
      ./ "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/"
rsync -rlptz -e "$RSYNC_SSH" "$POD_ENV" "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/.env"
# results/ is excluded above, but part mode needs the pre-registered trainer choice made at the P1
# smoke test (Part 1, 2026-09-30, refused to start without it). Send exactly that one record.
if [ -f results/E1/checks/backend_select.json ]; then
  $SSH -n 'mkdir -p /ws/emx/results/E1/checks'
  rsync -tz -e "$RSYNC_SSH" results/E1/checks/backend_select.json \
    "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/results/E1/checks/backend_select.json"
fi
$SSH 'chmod 600 /ws/emx/.env'
if [ "$SETUP" = "1" ]; then
  # Setup runs DETACHED on the pod and is polled with short ssh calls. A single long ssh session
  # was silently dropped by some hosts' network (P1b and Part 1, 2026-09-30: output stopped,
  # setup finished on the pod, the local ssh hung until reset ~17-63 min later).
  $SSH -n "rm -f /ws/setup.rc; { setsid nohup bash -c 'bash /ws/emx/scripts/pod_setup.sh > /ws/setup.log 2>&1; echo \$? > /ws/setup.rc' >/dev/null 2>&1 </dev/null & } ; echo setup launched"
  seen=0; waited=0; SETUP_MAX_S=${SETUP_MAX_S:-2400}
  while :; do
    sleep 20; waited=$((waited + 20))
    out=$(perl -e 'alarm 60; exec @ARGV' $SSH -n "n=\$(wc -l < /ws/setup.log 2>/dev/null || echo 0); tail -n +$((seen + 1)) /ws/setup.log 2>/dev/null; echo \"__LINES=\$n\"; [ -f /ws/setup.rc ] && echo \"__RC=\$(cat /ws/setup.rc)\"; true" 2>/dev/null) || { echo "(poll failed; retrying)"; continue; }
    printf '%s\n' "$out" | grep -v '^__' || true
    n=$(printf '%s\n' "$out" | sed -n 's/^__LINES=//p'); [ -n "$n" ] && seen=$n
    rc=$(printf '%s\n' "$out" | sed -n 's/^__RC=//p')
    if [ -n "$rc" ]; then
      [ "$rc" = "0" ] || { echo "pod_setup.sh failed on the pod (rc=$rc; see /ws/setup.log)" >&2; exit 1; }
      break
    fi
    [ "$waited" -lt "$SETUP_MAX_S" ] || { echo "pod setup not done after ${SETUP_MAX_S}s" >&2; exit 1; }
  done
fi
echo "pod synced$( [ "$SETUP" = "1" ] && echo ' and set up')"
