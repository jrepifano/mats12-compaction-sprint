#!/usr/bin/env bash
# Sync an explicit file set to a pod and run scripts/pod_setup.sh there.
# Port of ../mats12_neel_working/scripts/pod_push.sh. Changes: everything lives on the container
# disk under /ws (repo /ws/emx, caches /ws/hf_cache and /ws/uv_cache, venvs /ws/venv-*), so the
# /workspace relocation step is gone; no --setting/--stack; the pod .env allowlist is cut to
# HF_TOKEN, HF_USERNAME and non-secret settings, and a denylist check refuses to send anything
# that looks like an OpenAI/OpenRouter/RunPod key or the dataset password. .env is read per key,
# never sourced (sourcing would export every secret into every child process).
# 2026-09-29 (I1a implementation plan §2 U1; gpt-6-sol review U1 r1):
# - `--exp E1|I1a` (default E1; the ambient environment is never read).
# - I1a refuses a push with modified tracked files (staged or not; EMX_GIT_DIRTY=1), so that
#   EMX_GIT_COMMIT, which every manifest sidecar must carry, fully pins the code the pod runs.
#   E1 still only records EMX_GIT_DIRTY.
# - I1a pod env: HF_TOKEN only (HF_USERNAME too if .env has it) plus HF_HUB_DISABLE_XET=1,
#   EMX_EXP=I1a, HF_HOME=/ws/hf_cache, WANDB_MODE=offline, and the slot pod's rate and creation
#   time (EMX_POD_RATE_USD_H, EMX_POD_CREATED_EPOCH) for the on-pod P0 cost check; setup builds
#   only /ws/venv-i1a (pod_setup.sh --only i1a).
# - The slot's pod must belong to --exp (python -m emx.runpod --check-slot), else refused.
# - rsync sends ONLY git-tracked files (`git ls-files`) under src/ scripts/ third_party/
#   prompts/ pyproject.toml uv.lock, plus configs/ and data/ (minus data/raw) for E1, or data/I1a/
#   for I1a; named extras: logs/approvals/I1a-*.approved (I1a), or the gitignored E1 inputs
#   data/E1/*.jsonl and data/E1/questions/*.jsonl (E1). The generated pod env goes separately
#   to .env. Refused: any untracked, non-ignored file under those roots (listed), a tracked file
#   deleted from the working tree, (second layer) any name matching, case-insensitively,
#   *.env*, *.pem, *id_rsa*, *credential*, *.key, .netrc, .pypirc, *.p12, *secret*, *apikey*,
#   *api_key* or *token* (only *token* spares *.py and *.jsonl), and (third layer, review
#   integrated r3) any transferred file, or the pod env file except for HF_TOKEN, containing the
#   value of a laptop .env key matching KEY|TOKEN|SECRET|PASSWORD (values stay in a mode-600
#   temp file and are never printed; a hit prints the path and the key name).
# 2026-10-03: after the sync, arms the pod-side killswitch (scripts/pod_killswitch.sh; it uses the
# pod's own scoped RunPod key, so no laptop key is sent) with the deadline from
# `python -m emx.runpod --killswitch-deadline`; a failure to arm is a loud warning, not an error.
# --dry-run builds and checks the pod env and the transfer set, prints key names (never values),
# the file roots and the setup command, and contacts nothing.
# Usage: bash scripts/pod_push.sh [--exp I1a] --slot _A [--no-setup] [--dry-run [--show-files]]
set -euo pipefail
cd "$(dirname "$0")/.."

DOTENV="${POD_PUSH_DOTENV:-.env}"   # tests point this at a fake
envval() { { grep -m1 "^$1=" "$DOTENV" || true; } | cut -d= -f2- ; }  # a missing key is empty, not fatal under pipefail

EXP="E1"; SLOT=""; SETUP=1; DRY=0; SHOW=0
GIT="${POD_PUSH_GIT:-git}"   # tests inject a fake; only read-only git commands are used
while [ $# -gt 0 ]; do
  case "$1" in
    --exp) EXP="${2:?--exp needs a value}"; shift 2 ;;
    --slot) SLOT="${2:?--slot needs a value}"; shift 2 ;;
    --no-setup) SETUP=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --show-files) SHOW=1; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$EXP" in
  E1|I1a) ;;
  *) echo "--exp $EXP: pod_push.sh knows E1 and I1a only" >&2; exit 2 ;;
esac
[ -n "$SLOT" ] || { echo "usage: pod_push.sh [--exp I1a] --slot _A [--no-setup] [--dry-run]" >&2; exit 2; }
POD_SSH_HOST=$(envval "POD_SSH_HOST$SLOT" || true)
POD_SSH_PORT=$(envval "POD_SSH_PORT$SLOT" || true)
POD_SSH_USER=$(envval POD_SSH_USER || true); POD_SSH_USER=${POD_SSH_USER:-root}
if [ "$DRY" = "1" ]; then POD_SSH_HOST=${POD_SSH_HOST:-HOST}; POD_SSH_PORT=${POD_SSH_PORT:-PORT}; fi
[ -n "$POD_SSH_HOST" ] && [ -n "$POD_SSH_PORT" ] || { echo "no pod in slot $SLOT; run pod_up.py first" >&2; exit 1; }

SSH="ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new $POD_SSH_USER@$POD_SSH_HOST"
RSYNC_SSH="ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"

# pod-safe env: explicit allowlist, never the whole .env. The file is both bash-sourced
# (pod_setup.sh) and dotenv-parsed, so values must be shell-inert: enforce a safe charset.
POD_ENV=$(mktemp)
trap 'rm -f "$POD_ENV"' EXIT
for key in HF_TOKEN HF_USERNAME; do
  val=$(envval "$key" || true)
  if [ -z "$val" ] && [ "$EXP" = "I1a" ] && [ "$key" = "HF_USERNAME" ]; then continue; fi
  [ -n "$val" ] || { echo "$key is empty in .env; refusing" >&2; exit 1; }
  case "$val" in
    *[!A-Za-z0-9._/:-]*) echo "unsafe characters in $key value; refusing" >&2; exit 1 ;;
  esac
  printf '%s=%s\n' "$key" "$val" >> "$POD_ENV"
done
# the pod has no .git (it is never sent): record which commit was pushed, and whether dirty.
# The pod's manifest sidecars must carry this commit (pod_run_i1a.sh), so it must be real.
[ "$($GIT rev-parse --is-inside-work-tree 2>/dev/null)" = "true" ] \
  || { echo "not a git checkout; refusing (the transfer set is the tracked files)" >&2; exit 1; }
HEAD_SHA=$($GIT rev-parse HEAD 2>/dev/null || true)
case "$HEAD_SHA" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) echo "no commit at HEAD; refusing" >&2; exit 1 ;;
esac
if [ "$EXP" = "I1a" ]; then   # the sidecars' code_commit must fully pin the code the pod runs
  MODIFIED=$($GIT status --porcelain --untracked-files=no) \
    || { echo "git status failed; refusing" >&2; exit 1; }
  if [ -n "$MODIFIED" ]; then
    echo "modified tracked files (EMX_GIT_DIRTY=1): commit them before an I1a push; refusing:" >&2
    printf '%s\n' "$MODIFIED" >&2
    exit 1
  fi
fi
printf 'EMX_GIT_COMMIT=%s\nEMX_GIT_DIRTY=%s\n' "$HEAD_SHA" \
  "$( [ -z "$($GIT status --porcelain)" ] && echo 0 || echo 1 )" >> "$POD_ENV"
printf 'WANDB_MODE=offline\nTOKENIZERS_PARALLELISM=false\nHF_HOME=/ws/hf_cache\nEMX_EXP=%s\n' "$EXP" >> "$POD_ENV"
if [ "$EXP" = "I1a" ]; then
  printf 'HF_HUB_DISABLE_XET=1\n' >> "$POD_ENV"   # plain HTTP downloads (emx.i1a.hfget)
fi

# the slot's pod must be one this experiment created in this slot (offline event-log check)
if [ "$DRY" = "1" ]; then
  echo "DRY RUN ($EXP): would check: python -m emx.runpod --exp $EXP --check-slot $SLOT"
else
  SESSION=$(uv run --quiet python -m emx.runpod --exp "$EXP" --check-slot "$SLOT" --session-env) \
    || { echo "slot $SLOT does not hold a $EXP pod; refusing" >&2; exit 1; }
  if [ "$EXP" = "I1a" ]; then
    printf '%s\n' "$SESSION" | grep -E '^EMX_POD_(RATE_USD_H|CREATED_EPOCH)=[0-9.]+$' >> "$POD_ENV" \
      || { echo "no rate/creation time for slot $SLOT in the event log; refusing" >&2; exit 1; }
  fi
fi

# denylist: no laptop-only secret may ever reach the pod
if grep -Eq '^(OPENAI|OPENROUTER|RUNPOD|EM_DATASET_PASSWORD|AZURE)' "$POD_ENV"; then
  echo "a laptop-only key is in the pod env; refusing" >&2; exit 1
fi
for key in OPENAI_API_KEY OPENROUTER_API_KEY RUNPOD_API_KEY EM_DATASET_PASSWORD; do
  val=$(envval "$key" || true)   # a key absent from .env cannot leak (and must not abort set -e)
  if [ -n "$val" ] && grep -qF -- "$val" "$POD_ENV"; then
    echo "the value of $key appears in the pod env; refusing" >&2; exit 1
  fi
done

# transfer set: ONLY git-tracked files under the roots, plus named extras (reviews U1 r1 and
# integrated r2). Paths are repo-relative; rsync --files-from keeps them.
ROOTS="src scripts third_party prompts pyproject.toml uv.lock"
if [ "$EXP" = "I1a" ]; then ROOTS="$ROOTS data/I1a"; else ROOTS="$ROOTS configs data"; fi
PATHS=""
for p in $ROOTS; do
  if [ -e "$p" ]; then PATHS="$PATHS $p"; fi
done
for p in src scripts pyproject.toml uv.lock; do
  [ -e "$p" ] || { echo "$p missing from the checkout; refusing" >&2; exit 1; }
done
if [ "$EXP" = "I1a" ]; then
  for p in third_party/workspace-bench/pyproject.toml data/I1a; do
    [ -e "$p" ] || { echo "$p missing from the checkout; refusing" >&2; exit 1; }
  done
fi
# 1. an untracked, non-ignored file under a root is refused (commit it, or delete it)
# shellcheck disable=SC2086 # PATHS is a space-separated list of repo-relative paths
STATUS=$($GIT status --porcelain --untracked-files=all -- $PATHS) \
  || { echo "git status failed; refusing" >&2; exit 1; }
UNTRACKED=$(printf '%s\n' "$STATUS" | sed -n 's/^?? //p' | { grep -v '^data/raw/' || true; })
if [ -n "$UNTRACKED" ]; then
  echo "untracked files under the transfer roots (commit or remove them); refusing:" >&2
  printf '%s\n' "$UNTRACKED" >&2
  exit 1
fi
# shellcheck disable=SC2086
DELETED=$($GIT ls-files --deleted -- $PATHS) || { echo "git ls-files failed; refusing" >&2; exit 1; }
if [ -n "$DELETED" ]; then
  echo "tracked files missing from the working tree; refusing:" >&2
  printf '%s\n' "$DELETED" >&2
  exit 1
fi
LIST=$(mktemp)
trap 'rm -f "$POD_ENV" "$LIST"' EXIT
# shellcheck disable=SC2086
$GIT ls-files -z -- $PATHS | tr '\0' '\n' | { grep -v '^data/raw/' || true; } > "$LIST" \
  || { echo "git ls-files failed; refusing" >&2; exit 1; }
# 2. named extras that are deliberately not tracked
if [ "$EXP" = "I1a" ]; then
  for f in logs/approvals/I1a-*.approved; do   # checkpoint markers (main session, after Jacob)
    if [ -f "$f" ]; then echo "$f" >> "$LIST"; fi
  done
else
  for f in data/E1/*.jsonl data/E1/questions/*.jsonl; do   # gitignored E1 inputs (.gitignore)
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
fi
[ -s "$LIST" ] || { echo "empty transfer set; refusing" >&2; exit 1; }
while IFS= read -r f; do
  [ -f "$f" ] || { echo "transfer entry $f is not a regular file; refusing" >&2; exit 1; }
done < "$LIST"
# 3. second layer: secret-looking names, case-insensitive (*token* alone spares .py and
#    .jsonl: code such as token_readers.py and the benchmark's *.tokens.jsonl readouts)
secret_names() {  # a function: bash 3.2 misparses case patterns inside $( )
  local f b
  while IFS= read -r f; do
    b=$(printf '%s' "${f##*/}" | tr '[:upper:]' '[:lower:]')   # bash 3.2 has no lowercase expansion
    case "$b" in
      *.env*|*.pem|*id_rsa*|*credential*|*.key|.netrc|.pypirc|*.p12) echo "$f" ;;
      *secret*|*apikey*|*api_key*) echo "$f" ;;
      *token*)
        case "$b" in
          *.py|*.jsonl) ;;
          *) echo "$f" ;;
        esac ;;
    esac
  done < "$1"
}
SECRETS=$(secret_names "$LIST")
if [ -n "$SECRETS" ]; then
  echo "secret-looking files are in the transfer set; refusing:" >&2
  printf '%s\n' "$SECRETS" >&2
  exit 1
fi
# 4. third layer: content. No value of a laptop .env key whose name matches
#    KEY|TOKEN|SECRET|PASSWORD (case-insensitive) may appear in any transferred file, nor in the
#    pod env file except HF_TOKEN (which it is meant to carry). Each value goes only to a
#    mode-600 temp file (removed by the trap) and is never printed; a hit prints path + key NAME.
while IFS= read -r f; do
  [ -r "$f" ] || { echo "transfer entry $f is not readable; refusing" >&2; exit 1; }
done < "$LIST"
VALS=$(mktemp)
chmod 600 "$VALS"
trap 'rm -f "$POD_ENV" "$LIST" "$VALS"' EXIT
secret_keys() {
  sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' \
    "$DOTENV" | { grep -iE 'KEY|TOKEN|SECRET|PASSWORD' || true; } | sort -u
}
secret_value() {  # secret_value KEY: the raw value (first match), quotes and a CR stripped
  local v q
  v=$(grep -m1 -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$DOTENV" | sed 's/^[^=]*=//') || true
  v=${v%$'\r'}
  for q in '"' "'"; do
    case "$v" in
      "$q"*"$q") v=${v#"$q"}; v=${v%"$q"} ;;
    esac
  done
  printf '%s' "$v"
}
HITS=""
for key in $(secret_keys); do
  val=$(secret_value "$key")
  # real secrets are long random strings; a short value such as TOKENIZERS_PARALLELISM=false
  # would match ordinary words in thousands of files (a false refusal seen at P0, 2026-09-30)
  [ "${#val}" -ge 16 ] || { val=""; continue; }
  printf '%s\n' "$val" > "$VALS"
  val=""
  found=$(tr '\n' '\0' < "$LIST" | xargs -0 grep -F -l -f "$VALS" -- 2>/dev/null || true)
  if [ "$key" != "HF_TOKEN" ] && grep -qF -f "$VALS" "$POD_ENV"; then
    found="$found
(pod env file)"
  fi
  while IFS= read -r f; do
    if [ -n "$f" ]; then HITS="$HITS
  $f: $key"; fi
  done <<EOF
$found
EOF
done
: > "$VALS"
if [ -n "$HITS" ]; then
  echo "laptop secret values found in the transfer set (file: key name); refusing:$HITS" >&2
  exit 1
fi
NFILES=$(wc -l < "$LIST" | tr -d ' ')

SETUP_CMD="bash /ws/emx/scripts/pod_setup.sh"
[ "$EXP" = "I1a" ] && SETUP_CMD="$SETUP_CMD --only i1a"
if [ "$DRY" = "1" ]; then
  echo "DRY RUN ($EXP): pod env keys: $(cut -d= -f1 "$POD_ENV" | tr '\n' ' ')"
  echo "DRY RUN ($EXP): transfer set:$PATHS"
  echo "DRY RUN ($EXP): transfer files: $NFILES"
  if [ "$SHOW" = "1" ]; then sed 's/^/  send /' "$LIST"; fi
  echo "DRY RUN ($EXP): would rsync --files-from ($NFILES files) -> $POD_SSH_USER@$POD_SSH_HOST:/ws/emx/ (port $POD_SSH_PORT)"
  echo "DRY RUN ($EXP): would arm: bash /ws/emx/scripts/pod_killswitch.sh arm <pod id> <deadline from python -m emx.runpod --killswitch-deadline>"
  [ "$SETUP" = "1" ] && echo "DRY RUN ($EXP): would run: $SETUP_CMD"
  exit 0
fi

$SSH 'command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync); mkdir -p /ws/emx /ws/hf_cache /ws/uv_cache /ws/tmp'
rsync -lptz --files-from="$LIST" -e "$RSYNC_SSH" ./ "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/"
rsync -rlptz -e "$RSYNC_SSH" "$POD_ENV" "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/.env"
# results/ is excluded above, but part mode needs the pre-registered trainer choice made at the P1
# smoke test (Part 1, 2026-09-30, refused to start without it). Send exactly that one record.
# training records and loss-check sidecars (small): a part skips training for cells whose record
# carries a hub_revision, and honours accepted gates (Part 2 recovery, amendment E)
if [ "$EXP" = "E1" ] && ls results/E1/train/*.json >/dev/null 2>&1; then
  $SSH -n 'mkdir -p /ws/emx/results/E1/train'
  rsync -tz -e "$RSYNC_SSH" results/E1/train/*.json "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/results/E1/train/"
fi
if [ "$EXP" = "E1" ] && [ -f results/E1/checks/backend_select.json ]; then
  $SSH -n 'mkdir -p /ws/emx/results/E1/checks'
  rsync -tz -e "$RSYNC_SSH" results/E1/checks/backend_select.json \
    "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/results/E1/checks/backend_select.json"
fi
$SSH 'chmod 600 /ws/emx/.env'
if [ "$EXP" = "I1a" ]; then   # the I1a pod code needs both; fail loudly if either did not arrive
  $SSH 'test -f /ws/emx/third_party/workspace-bench/pyproject.toml && test -d /ws/emx/data/I1a' \
    || { echo "third_party/workspace-bench or data/I1a missing on the pod" >&2; exit 1; }
fi
# Pod-side spend backstop (scripts/pod_killswitch.sh, 2026-10-03): each pod stops itself when
# the experiment's running pods together reach its GPU kill line, even while the laptop sleeps.
# The deadline counts every running pod, so every running pod of the experiment is re-armed with
# it (re-arming only moves a deadline). Whether the pod key may stop its pod is UNVERIFIED until a
# live self-stop is seen. Failing to arm is loud but not fatal: the laptop watchdog is the cap.
# arm_slot SLOT DEADLINE: prints the slot's arm result (the killswitch's last line), or a failure
# when ssh did not exit 0 (an ARMED line from a dropped session is not trusted; review r2)
arm_slot() {
  local pid host port line out rc
  pid=$(envval "RUNPOD_POD_ID$1" || true); host=$(envval "POD_SSH_HOST$1" || true)
  port=$(envval "POD_SSH_PORT$1" || true)
  case "$pid" in ""|*[!A-Za-z0-9]*) echo "no valid RUNPOD_POD_ID$1 in .env"; return 0 ;; esac
  [ -n "$host" ] && [ -n "$port" ] || { echo "no SSH address for slot $1 in .env"; return 0; }
  out=$(perl -e 'alarm 90; exec @ARGV' ssh -n -p "$port" -o ConnectTimeout=20 \
    -o StrictHostKeyChecking=accept-new "$POD_SSH_USER@$host" \
    "bash /ws/emx/scripts/pod_killswitch.sh arm $pid $2" 2>&1) && rc=0 || rc=$?
  line=$(printf '%s\n' "$out" | tail -n 1)
  if [ "$rc" != "0" ]; then echo "arm failed or uncertain (ssh exit $rc)"; return 0; fi
  echo "${line:-no output from slot $1}"
}
KS_OUT=$(uv run --quiet python -m emx.runpod --exp "$EXP" --check-slot "$SLOT" \
  --killswitch-deadline 2>/dev/null) || KS_OUT=""
DEADLINE=$(printf '%s\n' "$KS_OUT" | sed -n 's/^EMX_KILLSWITCH_DEADLINE=\([0-9][0-9]*\)$/\1/p')
KS_SLOTS=$(printf '%s\n' "$KS_OUT" | sed -n 's/^EMX_KILLSWITCH_SLOTS=//p')
if [ -z "$DEADLINE" ]; then
  echo "WARNING: pod-side killswitch NOT armed (could not compute the deadline); only the laptop watchdog caps this pod" >&2
else
  DONE_SLOTS=""
  for s in $SLOT $KS_SLOTS; do   # this slot first; each slot once
    case " $DONE_SLOTS " in *" $s "*) continue ;; esac
    DONE_SLOTS="$DONE_SLOTS $s"
    KS_LINE=$(arm_slot "$s" "$DEADLINE")
    case "$KS_LINE" in
      *" ARMED pod="*key_check=ok*) echo "killswitch $s: $KS_LINE" ;;
      *" ARMED pod="*) echo "WARNING: killswitch $s armed but its pod key could not be checked: $KS_LINE" >&2 ;;
      *) echo "WARNING: pod-side killswitch NOT armed on slot $s ($KS_LINE); only the laptop watchdog caps it" >&2 ;;
    esac
  done
fi
if [ "$SETUP" = "1" ]; then
  # Setup runs DETACHED on the pod and is polled with short ssh calls. A single long ssh session
  # was silently dropped by some hosts' network (P1b and Part 1, 2026-09-30: output stopped,
  # setup finished on the pod, the local ssh hung until reset ~17-63 min later).
  $SSH -n "rm -f /ws/setup.rc; { setsid nohup bash -c '$SETUP_CMD > /ws/setup.log 2>&1; echo \$? > /ws/setup.rc' >/dev/null 2>&1 </dev/null & } ; echo setup launched"
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
