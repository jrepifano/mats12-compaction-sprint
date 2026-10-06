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
# 2026-10-03 (E1b implementation plan §2 U7): `--exp E1b`.
# - Refuses modified tracked files, as I1a does (the pod's code is pinned to EMX_GIT_COMMIT).
# - Pod env: as I1a's (HF_TOKEN, the slot pod's rate and creation time) plus HF_USERNAME, which
#   E1b needs (as E1) for its Hub repos, and EMX_EXP=E1b. No HF_HUB_DISABLE_XET: E1b uploads and
#   downloads through E1's tested huggingface_hub path.
# - Transfer roots: the common ones plus configs/E1 configs/E1b data/E1 data/E1b (tracked files);
#   named extras: the gitignored E1 inputs (data/E1/*.jsonl, data/E1/questions/*.jsonl), every
#   gitignored file under data/E1b/ (ext48, contrast set, FineWeb sample), E1's training records
#   results/E1/train/*.json (Part 3 and the re-evaluation load E1's adapters through them), E1's
#   backend record, and results/E1b/part3/steer/alpha.json when present. All go in the one
#   checked file list. A real push refuses if a required input is missing (the dry run warns).
#   Mid-run, the follower (scripts/e1b_follow.sh) sends alpha.json alone with plain scp.
# - Setup: pod_setup.sh --only e1b (venv-peft and venv-vllm).
# 2026-10-04 (I9 implementation plan §2 U6, §4 P1 and P3): `--exp I9`. Slot _A = session A (P1),
#   slot _B = session B (P3); any other slot is refused.
# - Refuses modified tracked files, as I1a and E1b do.
# - Pod env: HF_TOKEN only (never HF_USERNAME: I9 pushes nothing to the Hub), EMX_EXP=I9,
#   HF_HOME=/ws/hf_cache, HF_HUB_DISABLE_XET=1, WANDB_MODE=offline, and the slot pod's rate and
#   creation time, as I1a's.
# - Transfer roots (tracked files only): src scripts third_party/workspace-bench
#   third_party/agentic-misalignment prompts/I9 data/I9 pyproject.toml uv.lock (not the other
#   experiments' prompts, data or vendored code); named extras: every gitignored file under
#   data/I9/ and the approval files logs/approvals/I9-*.approved. A real push refuses when a
#   required input (data/I9/scenarios.jsonl, data/I9/sets.json, scripts/i9_pod.py,
#   scripts/pod_run_i9.sh, the vendored wsbench and Lynch generator) or the session's approval
#   (I9-P1.approved for _A, I9-P3.approved for _B) is missing; the dry run warns.
# - Setup: pod_setup.sh --only i9 (/ws/venv-i9 and /ws/venv-vllm). The killswitch is armed as
#   for every experiment, at the I9 GPU line (min($36, $65 - $2 - judges)).
# 2026-10-06 (E3 implementation plan U8c; v2 fix 5, v3 fix 5, v4 fix 3): `--exp E3`.
# - Refuses modified tracked files, as I1a, E1b and I9 do. Pod env as E1b's (HF_TOKEN,
#   HF_USERNAME for the private e3-* Hub repos, the slot pod's rate and creation time), EMX_EXP=E3.
# - Transfer roots (tracked files only): the common ones plus configs/E3 data/E1 data/E1b/questions
#   data/E3 data/shared. Named extras: the answer pairs, their selection file and the question
#   swaps (src/emx/e3/contracts.py PAIRS_FILE, SELECTION_FILE, SWAPS_FILE) when ignored rather than
#   tracked, the pairs manifest results/E3/MANIFEST.pairs.sha256 (scripts/e3_train.py checks the
#   three files against it), and E1b's old reference training records
#   results/E1b/train/<OLD_REFERENCE_CELLS>.json. All are required.
#   The gitignored pinned inputs (contracts MAIN_CHECKOUT_ONLY: E1's train/test files and the
#   narrow questions) are read from the main checkout (contracts MAIN_CHECKOUT; test hook
#   POD_PUSH_MAIN_CHECKOUT) and sent to the same relative path under /ws/emx by a second rsync;
#   they go through the same name and content checks. Every pinned input is sha256-checked against
#   contracts.input_sha256() (and the pairs against PAIRS_SHA256 once pinned) before sending, and
#   again on the pod after the sync (sha256sum -c). A real push refuses a missing or mismatched
#   input; the dry run warns.
# - Killswitch, per slot: `python -m emx.runpod --exp E3 --check-slot S --killswitch-deadline
#   --per-slot` prints EMX_KILLSWITCH_DEADLINE_<slot>=<epoch> for each running E3 pod; each slot
#   is armed with its OWN deadline. The killswitch's "ARMED pod=<id> deadline=<epoch> ...
#   key_check=<ok|read403>" line must name that slot's pod and a deadline <= the requested one;
#   the acknowledgement is then recorded with `python -m emx.runpod_reservations ack --pod <id>
#   --deadline <epoch>`. For E3 a missing, mismatched, later or uncertain acknowledgement (no
#   ARMED line, an ssh failure, an unchecked pod key, a refused ack) records nothing for that slot
#   and makes pod_push exit 1 after trying every slot, before setup (the orchestrator then runs
#   cleanup). Other experiments keep their warn-and-continue arming unchanged.
# - `--arm-only` (E3 only): re-arm every running E3 slot as above and exit; no transfer, no
#   setup (e3_orchestrate.py rearm, after a reservation or API spend change).
# - Setup: pod_setup.sh --only e3.
# --dry-run builds and checks the pod env and the transfer set, prints key names (never values),
# the file roots and the setup command, and contacts nothing.
# Usage: bash scripts/pod_push.sh [--exp I1a|E1b|I9|E3] --slot _A [--no-setup] [--dry-run [--show-files]]
#        bash scripts/pod_push.sh --exp E3 --slot _A --arm-only [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."

DOTENV="${POD_PUSH_DOTENV:-.env}"   # tests point this at a fake
envval() { { grep -m1 "^$1=" "$DOTENV" || true; } | cut -d= -f2- ; }  # a missing key is empty, not fatal under pipefail

EXP="E1"; SLOT=""; SETUP=1; DRY=0; SHOW=0; ARM_ONLY=0
GIT="${POD_PUSH_GIT:-git}"   # tests inject a fake; only read-only git commands are used
while [ $# -gt 0 ]; do
  case "$1" in
    --exp) EXP="${2:?--exp needs a value}"; shift 2 ;;
    --slot) SLOT="${2:?--slot needs a value}"; shift 2 ;;
    --no-setup) SETUP=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --show-files) SHOW=1; shift ;;
    --arm-only) ARM_ONLY=1; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$EXP" in
  E1|I1a|E1b|I9|E3) ;;
  *) echo "--exp $EXP: pod_push.sh knows E1, I1a, E1b and I9 only, plus E3" >&2; exit 2 ;;
esac
if [ "$ARM_ONLY" = "1" ] && [ "$EXP" != "E3" ]; then
  echo "--arm-only is E3-only" >&2; exit 2
fi
if [ "$EXP" = "I9" ]; then   # the slot names the session (see pod_up.py)
  case "$SLOT" in
    _A) I9_STEP=P1 ;;
    _B) I9_STEP=P3 ;;
    *) echo "--exp I9: slot must be _A (session A) or _B (session B), not '$SLOT'" >&2; exit 2 ;;
  esac
fi
[ -n "$SLOT" ] || { echo "usage: pod_push.sh [--exp I1a] --slot _A [--no-setup] [--dry-run]" >&2; exit 2; }
POD_SSH_HOST=$(envval "POD_SSH_HOST$SLOT" || true)
POD_SSH_PORT=$(envval "POD_SSH_PORT$SLOT" || true)
POD_SSH_USER=$(envval POD_SSH_USER || true); POD_SSH_USER=${POD_SSH_USER:-root}
if [ "$DRY" = "1" ]; then POD_SSH_HOST=${POD_SSH_HOST:-HOST}; POD_SSH_PORT=${POD_SSH_PORT:-PORT}; fi
[ -n "$POD_SSH_HOST" ] && [ -n "$POD_SSH_PORT" ] || { echo "no pod in slot $SLOT; run pod_up.py first" >&2; exit 1; }

SSH="ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new $POD_SSH_USER@$POD_SSH_HOST"
RSYNC_SSH="ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"
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

# arm_e3: E3's per-slot arming with a verified acknowledgement (plan v3 fix 5, v4 fix 3). Arms
# this slot and every running E3 slot, each with its own deadline from
# `python -m emx.runpod --per-slot` (never above the slot's current acknowledged deadline, v4);
# accepts an ARMED line only for that slot's pod, at a deadline <= the requested one and with a
# checked pod key, then records it with `python -m emx.runpod_reservations ack`. Anything else
# records nothing for that slot. Every slot is tried; returns 1 if any slot failed.
arm_e3() {
  local out per slots s dl line pid parsed apod adl kc fail=0 seen=""
  out=$(uv run --quiet python -m emx.runpod --exp "$EXP" --check-slot "$SLOT" \
    --killswitch-deadline --per-slot 2>/dev/null) \
    || { echo "E3 killswitch: could not compute the per-slot deadlines (emx.runpod --per-slot failed)" >&2; return 1; }
  # EMX_KILLSWITCH_DEADLINE_<slot>=<epoch>, the slot keeping its leading underscore (..._ _A)
  per=$(printf '%s\n' "$out" \
    | sed -n 's/^EMX_KILLSWITCH_DEADLINE__*\([A-Za-z0-9]\{1,8\}\)=\([0-9][0-9]*\)$/_\1 \2/p')
  slots=$(printf '%s\n' "$out" | sed -n 's/^EMX_KILLSWITCH_SLOTS=//p')
  for s in $SLOT $slots $(printf '%s\n' "$per" | cut -d' ' -f1); do   # this slot first; once each
    case " $seen " in *" $s "*) continue ;; esac
    seen="$seen $s"
    case "$s" in
      _*[!A-Za-z0-9_]*|_) echo "E3 killswitch $s: bad slot name" >&2; fail=1; continue ;;
      _*) ;;
      *) echo "E3 killswitch $s: bad slot name" >&2; fail=1; continue ;;
    esac
    dl=$(printf '%s\n' "$per" | sed -n "s/^$s \([0-9][0-9]*\)\$/\1/p" | sort -u)
    case "$dl" in
      "") echo "E3 killswitch $s: NOT armed (no per-slot deadline for this running slot)" >&2
          fail=1; continue ;;
      *[!0-9]*) echo "E3 killswitch $s: NOT armed (conflicting per-slot deadlines)" >&2
          fail=1; continue ;;
    esac
    pid=$(envval "RUNPOD_POD_ID$s" || true)
    line=$(arm_slot "$s" "$dl")
    parsed=$(printf '%s\n' "$line" | sed -n \
      's/.* ARMED pod=\([A-Za-z0-9][A-Za-z0-9]*\) deadline=\([0-9][0-9]*\) .*key_check=\([^ ]*\).*/\1 \2 \3/p')
    if [ -z "$parsed" ]; then
      echo "E3 killswitch $s: NOT armed or unacknowledged ($line); nothing recorded" >&2
      fail=1; continue
    fi
    # shellcheck disable=SC2086 # three space-free fields
    set -- $parsed
    apod=$1; adl=$2; kc=$3
    if [ -z "$pid" ] || [ "$apod" != "$pid" ]; then
      echo "E3 killswitch $s: ARMED line names pod $apod, not the slot's pod '$pid'; nothing recorded" >&2
      fail=1; continue
    fi
    if [ "$adl" -gt "$dl" ]; then
      echo "E3 killswitch $s: acknowledged deadline $adl is LATER than the requested $dl; nothing recorded" >&2
      fail=1; continue
    fi
    case "$kc" in
      ok|read403) ;;
      *) echo "E3 killswitch $s: armed but its pod key is unchecked (key_check=$kc): uncertain; nothing recorded" >&2
         fail=1; continue ;;
    esac
    if ! uv run --quiet python -m emx.runpod_reservations ack --pod "$apod" --deadline "$adl"; then
      echo "E3 killswitch $s: emx.runpod_reservations refused the acknowledgement (pod $apod, deadline $adl)" >&2
      fail=1; continue
    fi
    echo "killswitch $s: $line (acknowledged: deadline $adl <= requested $dl)"
  done
  [ "$fail" = "0" ]
}
if [ "$ARM_ONLY" = "1" ]; then   # E3 only (checked above): re-arm, nothing else
  if [ "$DRY" = "1" ]; then
    echo "DRY RUN (E3): --arm-only: would run: python -m emx.runpod --exp E3 --check-slot $SLOT --killswitch-deadline --per-slot"
    echo "DRY RUN (E3): --arm-only: would arm every running E3 slot with its own deadline (pod_killswitch.sh arm) and record each ack (python -m emx.runpod_reservations ack); no transfer, no setup"
    exit 0
  fi
  arm_e3 || { echo "E3 --arm-only: arming failed or unacknowledged on at least one slot; run e3_orchestrate.py cleanup for it" >&2; exit 1; }
  echo "E3 killswitch re-armed on every running slot"
  exit 0
fi

# pod-safe env: explicit allowlist, never the whole .env. The file is both bash-sourced
# (pod_setup.sh) and dotenv-parsed, so values must be shell-inert: enforce a safe charset.
POD_ENV=$(mktemp)
trap 'rm -f "$POD_ENV"' EXIT
for key in HF_TOKEN HF_USERNAME; do
  val=$(envval "$key" || true)
  if [ -z "$val" ] && [ "$EXP" = "I1a" ] && [ "$key" = "HF_USERNAME" ]; then continue; fi
  if [ "$EXP" = "I9" ] && [ "$key" = "HF_USERNAME" ]; then continue; fi   # HF_TOKEN only
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
# Run ledgers the pod tooling appends to while pods run (pod events, spend) are not code: a change
# to them must not block a push (E1b S1, 2026-10-04: pod_up's own event made the tree "dirty").
# E3 (2026-10-06): the orchestrator's own records, written during `launch` itself, likewise.
RUN_LOGS='^ M (logs/(pods_[A-Za-z0-9]+(\.pending)?\.jsonl|costs\.jsonl|E3-orchestrate\.jsonl)|results/E3/(cuts\.json|reservations_snapshot\.jsonl|measured_rates\.json))$'
if [ "$EXP" = "I1a" ] || [ "$EXP" = "E1b" ] || [ "$EXP" = "I9" ] || [ "$EXP" = "E3" ]; then   # the sidecars' code_commit must fully pin the code the pod runs
  MODIFIED=$($GIT status --porcelain --untracked-files=no) \
    || { echo "git status failed; refusing" >&2; exit 1; }
  MODIFIED=$(printf '%s\n' "$MODIFIED" | grep -Ev "$RUN_LOGS" | grep -v '^$' || true)
  if [ -n "$MODIFIED" ]; then
    echo "modified tracked files (EMX_GIT_DIRTY=1): commit them before an $EXP push; refusing:" >&2
    printf '%s\n' "$MODIFIED" >&2
    exit 1
  fi
fi
# Dirty = modified tracked files other than the run ledgers, as emx.i9.provenance records it
# (untracked pod logs, heartbeats and outputs are what a run writes).
printf 'EMX_GIT_COMMIT=%s\nEMX_GIT_DIRTY=%s\n' "$HEAD_SHA" \
  "$( [ -z "$($GIT status --porcelain --untracked-files=no | grep -Ev "$RUN_LOGS")" ] && echo 0 || echo 1 )" \
  >> "$POD_ENV"
printf 'WANDB_MODE=offline\nTOKENIZERS_PARALLELISM=false\nHF_HOME=/ws/hf_cache\nEMX_EXP=%s\n' "$EXP" >> "$POD_ENV"
if [ "$EXP" = "I1a" ] || [ "$EXP" = "I9" ]; then
  printf 'HF_HUB_DISABLE_XET=1\n' >> "$POD_ENV"   # plain HTTP downloads (emx.i1a.hfget)
fi

# the slot's pod must be one this experiment created in this slot (offline event-log check)
if [ "$DRY" = "1" ]; then
  echo "DRY RUN ($EXP): would check: python -m emx.runpod --exp $EXP --check-slot $SLOT"
else
  SESSION=$(uv run --quiet python -m emx.runpod --exp "$EXP" --check-slot "$SLOT" --session-env) \
    || { echo "slot $SLOT does not hold a $EXP pod; refusing" >&2; exit 1; }
  if [ "$EXP" = "I1a" ] || [ "$EXP" = "E1b" ] || [ "$EXP" = "I9" ] || [ "$EXP" = "E3" ]; then
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
case "$EXP" in
  I1a) ROOTS="$ROOTS data/I1a" ;;
  E1b) ROOTS="$ROOTS configs/E1 configs/E1b data/E1 data/E1b" ;;
  I9) ROOTS="src scripts third_party/workspace-bench third_party/agentic-misalignment prompts/I9 data/I9 pyproject.toml uv.lock" ;;
  E3) ROOTS="$ROOTS configs/E3 data/E1 data/E1b/questions data/E1b/manifest.json data/E3 data/shared" ;;  # manifest: gen_common verifies ext48 against it (2026-10-06)
  *) ROOTS="$ROOTS configs data" ;;
esac
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
MAIN_LIST=$(mktemp)   # E3: gitignored pinned inputs sent from the main checkout (else empty)
CHECKLIST=$(mktemp)   # every file sent, as a local path (the name and content checks read it)
PINS=$(mktemp)        # E3: "<sha256>  <repo-relative path>" of every pinned input (else empty)
trap 'rm -f "$POD_ENV" "$LIST" "$MAIN_LIST" "$CHECKLIST" "$PINS"' EXIT
if [ "$EXP" = "E3" ]; then
  # frozen values from src/emx/e3/contracts.py (stdlib only; loaded by path, never hardcoded here)
  E3PY=python3; [ -x .venv/bin/python ] && E3PY=.venv/bin/python
  E3_INFO=$("$E3PY" - src/emx/e3/contracts.py <<'PY_E3'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("e3_contracts", sys.argv[1])
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
print("MAIN", C.MAIN_CHECKOUT)
for p in C.MAIN_CHECKOUT_ONLY:
    print("MAINONLY", p)
for p, h in C.input_sha256().items():
    print("PIN", p, h)
if C.PAIRS_SHA256:
    print("PIN", C.PAIRS_FILE, C.PAIRS_SHA256)
for p in (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE, C.manifest_path("pairs")):
    print("NEED", p)
for c in C.OLD_REFERENCE_CELLS:
    print("NEED", f"results/E1b/train/{c}.json")
PY_E3
) || { echo "could not read src/emx/e3/contracts.py; refusing" >&2; exit 1; }
  E3_MAIN=${POD_PUSH_MAIN_CHECKOUT:-$(printf '%s\n' "$E3_INFO" | sed -n 's/^MAIN //p')}
  E3_MAINONLY=$(printf '%s\n' "$E3_INFO" | sed -n 's/^MAINONLY //p')
  E3_NEED=$(printf '%s\n' "$E3_INFO" | sed -n 's/^NEED //p')
  printf '%s\n' "$E3_INFO" | sed -n 's/^PIN \([^ ]*\) \([0-9a-f]\{64\}\)$/\2  \1/p' > "$PINS"
  [ -d "$E3_MAIN" ] || { echo "E3 main checkout $E3_MAIN not found; refusing" >&2; exit 1; }
fi
# shellcheck disable=SC2086
$GIT ls-files -z -- $PATHS | tr '\0' '\n' | { grep -v '^data/raw/' || true; } > "$LIST" \
  || { echo "git ls-files failed; refusing" >&2; exit 1; }
# 2. named extras that are deliberately not tracked
if [ "$EXP" = "I1a" ]; then
  for f in logs/approvals/I1a-*.approved; do   # checkpoint markers (main session, after Jacob)
    if [ -f "$f" ]; then echo "$f" >> "$LIST"; fi
  done
elif [ "$EXP" = "E1b" ]; then
  # gitignored inputs: E1's data and narrow questions, data/E1b's ignored files (untracked,
  # non-ignored ones were refused above), E1's adapter records, the chosen steering strength
  E1B_EXTRAS=$(find data/E1b -type f ! -name .DS_Store 2>/dev/null | LC_ALL=C sort || true)
  for f in data/E1/*.jsonl data/E1/questions/*.jsonl $E1B_EXTRAS results/E1/train/*.json \
           results/E1/checks/backend_select.json results/E1b/part3/steer/alpha.json; do
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
  E1B_MISSING=""
  for f in data/E1/train_bad.jsonl data/E1/train_good.jsonl data/E1/questions/narrow.jsonl \
           data/E1b/manifest.json data/E1b/questions/ext48.jsonl data/E1b/contrast_set.jsonl \
           data/E1b/fineweb_2000.jsonl results/E1/train/base-chat-bad-R2-s1.json; do
    grep -qxF "$f" "$LIST" || E1B_MISSING="$E1B_MISSING $f"
  done
  grep -q '^configs/E1b/.*\.json$' "$LIST" || E1B_MISSING="$E1B_MISSING configs/E1b/*.json"
  if [ -n "$E1B_MISSING" ]; then
    if [ "$DRY" = "1" ]; then
      echo "DRY RUN (E1b): WARNING: required inputs missing (a real push refuses):$E1B_MISSING"
    else
      echo "required E1b inputs missing from the transfer set; refusing:$E1B_MISSING" >&2; exit 1
    fi
  fi
elif [ "$EXP" = "I9" ]; then
  # gitignored files under data/I9 (untracked, non-ignored ones were refused above) and the
  # approval files (the pod-side chain gates on them; the main session writes them)
  I9_EXTRAS=$(find data/I9 -type f ! -name .DS_Store 2>/dev/null | LC_ALL=C sort || true)
  # I1a's committed logit-lens readouts and capture manifest: the smoke's I1a reproduction check
  # (src/emx/i9/acts.py) refuses to run without them (added 2026-10-04, build review r1 #12).
  # MANIFEST.tokens.sha256 is not sent (the secret-name filter matches "token"); the check
  # verifies the readouts against its pinned sha256 instead.
  I9_I1A_REF="results/I1a/readouts/logit_lens/multihop.jsonl results/I1a/MANIFEST.capture.sha256"
  for f in $I9_EXTRAS $I9_I1A_REF logs/approvals/I9-*.approved; do
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
  I9_MISSING=""
  for f in data/I9/scenarios.jsonl data/I9/sets.json scripts/i9_pod.py scripts/pod_run_i9.sh \
           third_party/workspace-bench/pyproject.toml \
           third_party/agentic-misalignment/scripts/generate_prompts.py \
           $I9_I1A_REF \
           "logs/approvals/I9-$I9_STEP.approved"; do
    grep -qxF "$f" "$LIST" || I9_MISSING="$I9_MISSING $f"
  done
  if [ -n "$I9_MISSING" ]; then
    if [ "$DRY" = "1" ]; then
      echo "DRY RUN (I9): WARNING: required inputs missing (a real push refuses):$I9_MISSING"
    else
      echo "required I9 inputs missing from the transfer set; refusing:$I9_MISSING" >&2; exit 1
    fi
  fi
elif [ "$EXP" = "E3" ]; then
  # the main checkout's gitignored inputs go by their own rsync, never from this checkout
  for f in $E3_MAINONLY; do
    if grep -qxF "$f" "$LIST"; then
      grep -vxF "$f" "$LIST" > "$LIST.tmp" || true
      mv "$LIST.tmp" "$LIST"
    fi
    if [ -f "$E3_MAIN/$f" ]; then echo "$f" >> "$MAIN_LIST"; fi
  done
  # pairs, selection, swaps (if ignored rather than tracked) and E1b's old reference records
  for f in $E3_NEED; do
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
  E3_MISSING=""
  for f in $E3_NEED $(cut -c67- "$PINS") scripts/pod_run_e3.sh; do
    grep -qxF "$f" "$LIST" || grep -qxF "$f" "$MAIN_LIST" || E3_MISSING="$E3_MISSING $f"
  done
  grep -q '^configs/E3/.*\.json$' "$LIST" || E3_MISSING="$E3_MISSING configs/E3/*.json"
  # every pinned input must match its sha256 (contracts.input_sha256), at its source
  E3_BAD=""
  while read -r want f; do
    [ -n "$f" ] || continue
    if grep -qxF "$f" "$MAIN_LIST"; then src="$E3_MAIN/$f"
    elif grep -qxF "$f" "$LIST"; then src="$f"
    else continue; fi   # missing: reported above
    got=$(shasum -a 256 "$src" | cut -d' ' -f1)
    [ "$got" = "$want" ] || E3_BAD="$E3_BAD $f"
  done < "$PINS"
  if [ -n "$E3_MISSING$E3_BAD" ]; then
    if [ "$DRY" = "1" ]; then
      [ -z "$E3_MISSING" ] || echo "DRY RUN (E3): WARNING: required inputs missing (a real push refuses):$E3_MISSING"
      [ -z "$E3_BAD" ] || echo "DRY RUN (E3): WARNING: sha256 mismatch against contracts (a real push refuses):$E3_BAD"
    else
      [ -z "$E3_MISSING" ] || echo "required E3 inputs missing from the transfer set; refusing:$E3_MISSING" >&2
      [ -z "$E3_BAD" ] || echo "E3 inputs do not match their pinned sha256; refusing:$E3_BAD" >&2
      exit 1
    fi
  fi
else
  for f in data/E1/*.jsonl data/E1/questions/*.jsonl; do   # gitignored E1 inputs (.gitignore)
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
fi
[ -s "$LIST" ] || { echo "empty transfer set; refusing" >&2; exit 1; }
cat "$LIST" > "$CHECKLIST"
while IFS= read -r f; do   # E3 only: the main checkout's files, checked where they are read
  [ -n "$f" ] && printf '%s/%s\n' "$E3_MAIN" "$f" >> "$CHECKLIST"
done < "$MAIN_LIST"
while IFS= read -r f; do
  [ -f "$f" ] || { echo "transfer entry $f is not a regular file; refusing" >&2; exit 1; }
done < "$CHECKLIST"
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
SECRETS=$(secret_names "$CHECKLIST")
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
done < "$CHECKLIST"
VALS=$(mktemp)
chmod 600 "$VALS"
trap 'rm -f "$POD_ENV" "$LIST" "$MAIN_LIST" "$CHECKLIST" "$PINS" "$VALS"' EXIT
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
  found=$(tr '\n' '\0' < "$CHECKLIST" | xargs -0 grep -F -l -f "$VALS" -- 2>/dev/null || true)
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
NFILES=$(wc -l < "$CHECKLIST" | tr -d ' ')

SETUP_CMD="bash /ws/emx/scripts/pod_setup.sh"
[ "$EXP" = "I1a" ] && SETUP_CMD="$SETUP_CMD --only i1a"
[ "$EXP" = "E1b" ] && SETUP_CMD="$SETUP_CMD --only e1b"
[ "$EXP" = "I9" ] && SETUP_CMD="$SETUP_CMD --only i9"
[ "$EXP" = "E3" ] && SETUP_CMD="$SETUP_CMD --only e3"
if [ "$DRY" = "1" ]; then
  echo "DRY RUN ($EXP): pod env keys: $(cut -d= -f1 "$POD_ENV" | tr '\n' ' ')"
  echo "DRY RUN ($EXP): transfer set:$PATHS"
  echo "DRY RUN ($EXP): transfer files: $NFILES"
  if [ "$SHOW" = "1" ]; then sed 's/^/  send /' "$LIST"; fi
  if [ -s "$MAIN_LIST" ]; then
    echo "DRY RUN ($EXP): from the main checkout $E3_MAIN: $(wc -l < "$MAIN_LIST" | tr -d ' ') files (same relative paths under /ws/emx)"
    if [ "$SHOW" = "1" ]; then sed 's/^/  send-main /' "$MAIN_LIST"; fi
  fi
  echo "DRY RUN ($EXP): would rsync --files-from ($NFILES files) -> $POD_SSH_USER@$POD_SSH_HOST:/ws/emx/ (port $POD_SSH_PORT)"
  if [ "$EXP" = "E3" ]; then
    echo "DRY RUN (E3): would verify the pinned inputs on the pod: sha256sum -c ($(wc -l < "$PINS" | tr -d ' ') files)"
    echo "DRY RUN (E3): would arm each running E3 slot with its own deadline (python -m emx.runpod --exp E3 --check-slot $SLOT --killswitch-deadline --per-slot), require a matching ARMED line and record it (python -m emx.runpod_reservations ack --pod <id> --deadline <epoch>); any failure exits 1"
  else
    echo "DRY RUN ($EXP): would arm: bash /ws/emx/scripts/pod_killswitch.sh arm <pod id> <deadline from python -m emx.runpod --killswitch-deadline>"
  fi
  [ "$SETUP" = "1" ] && echo "DRY RUN ($EXP): would run: $SETUP_CMD"
  exit 0
fi

$SSH 'command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync); mkdir -p /ws/emx /ws/hf_cache /ws/uv_cache /ws/tmp'
rsync -lptz --files-from="$LIST" -e "$RSYNC_SSH" ./ "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/"
if [ -s "$MAIN_LIST" ]; then   # E3: the gitignored pinned inputs, from the main checkout
  rsync -lptz --files-from="$MAIN_LIST" -e "$RSYNC_SSH" "$E3_MAIN/" "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/"
fi
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
if [ "$EXP" = "I9" ]; then   # the I9 pod code needs all three; fail loudly if one did not arrive
  $SSH 'test -f /ws/emx/third_party/workspace-bench/pyproject.toml && test -d /ws/emx/third_party/agentic-misalignment && test -f /ws/emx/data/I9/scenarios.jsonl' \
    || { echo "third_party/workspace-bench, third_party/agentic-misalignment or data/I9 missing on the pod" >&2; exit 1; }
fi
# Pod-side spend backstop (scripts/pod_killswitch.sh, 2026-10-03): each pod stops itself when
# the experiment's running pods together reach its GPU kill line, even while the laptop sleeps.
# The deadline counts every running pod, so every running pod of the experiment is re-armed with
# it (re-arming only moves a deadline). Self-stop verified live 2026-10-03 (logs/infra/), through
# runpodctl only. Failing to arm is loud but not fatal: the laptop watchdog still runs (E3: fatal,
# see arm_e3; the pinned inputs are verified on the pod only once the pod is armed).
if [ "$EXP" = "E3" ]; then   # per-slot deadlines and a verified acknowledgement; failure is fatal
  arm_e3 || { echo "E3: killswitch arming failed or unacknowledged on at least one slot; refusing to continue (run e3_orchestrate.py cleanup)" >&2; exit 1; }
else
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
        *" ARMED pod="*key_check=ok*|*" ARMED pod="*key_check=read403*) echo "killswitch $s: $KS_LINE" ;;
        *" ARMED pod="*) echo "WARNING: killswitch $s armed but its pod key could not be checked: $KS_LINE" >&2 ;;
        *) echo "WARNING: pod-side killswitch NOT armed on slot $s ($KS_LINE); only the laptop watchdog caps it" >&2 ;;
      esac
    done
  fi
fi
if [ "$EXP" = "E3" ]; then   # every pinned input arrived intact (paths are repo-relative)
  $SSH 'cd /ws/emx && sha256sum -c --quiet -' < "$PINS" \
    || { echo "E3 pinned inputs do not verify on the pod (sha256sum -c)" >&2; exit 1; }
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
