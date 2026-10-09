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
# 2026-10-06 (E9 implementation plan v4, unit F2; prereg §8.1, §8.7): `--exp E9 --slot S [--runs
#   RUN[:KIND][,...]]` (the slot's planned runs, emx.e9.contracts.run_id, e.g. bg-c1:cs,orig-c1:cq;
#   smoke* for the smoke pod). Without --runs, or with a smoke run (the smoke job reads another
#   run's stream), every stream under data/E9/streams/ is sent (each pinned to its manifest).
# - Refuses modified tracked files, as E3 does. Pod env as E3's (HF_TOKEN, HF_USERNAME for the
#   private e9-* adapter repos, the slot pod's rate and creation time), EMX_EXP=E9, plus
#   EMX_E9_SLOT, EMX_E9_RUNS (comma-separated) and EMX_POD_ID (the slot's pod id; RunPod also sets
#   RUNPOD_POD_ID in the container, but no RUNPOD* key may enter the pod env file: denylist).
# - Transfer roots (tracked files only): the common ones plus configs/E9 data/E1 data/E1b/questions
#   data/E1b/manifest.json data/E3/question_swaps.json data/shared. Named extras (gitignored):
#   every answer_pairs_v1* file under data/shared, data/E9/pools/heldout_text.jsonl (+ its
#   .manifest.json), data/E9/insert/versions.jsonl and insert_manifest.json when present, and for
#   each run of --runs other than smoke* its stream data/E9/streams/<run>.npy and
#   <run>.manifest.json (required; or every stream, as above). E1's gitignored train/test files and narrow questions come
#   from the main checkout (emx.e3.contracts MAIN_CHECKOUT / MAIN_CHECKOUT_ONLY, as E3). Pinned
#   (sha256-checked before sending and on the pod after the sync): those main-checkout inputs
#   (emx.e3.contracts.input_sha256), the answer pairs and the question swaps
#   (emx.e9.contracts PAIRS_SHA256, SWAPS_SHA256), and each run's stream against its manifest's
#   stream_sha256. A real push refuses a missing or mismatched input; the dry run warns.
# - Killswitch: per slot as E3 (arm_e3 with --exp E9: verified ARMED line, ack recorded), but
#   every E9 deadline is the pod's own budget deadline from its RunPod reservation
#   (emx.runpod.per_slot_deadlines), never the global kill line (prereg §7.3). `--arm-only` works
#   for E9 as for E3.
# - Order (code review R3 #2, 2026-10-07): all laptop-side checks; then ONLY
#   scripts/pod_killswitch.sh is sent (one ssh call bounded at 120 s); every running E9 slot is
#   armed and its acknowledgement verified and recorded; THEN the bulk transfer, the pinned-input
#   check on the pod, and setup. The caller (e9_orchestrate.py) bounds the whole call by the pod's
#   funded time and cleans up on a timeout.
# - E9 exit codes: 0 synced (and set up); 1 refused by a laptop-side check (nothing sent);
#   2 usage; 4 the killswitch could not be sent, armed or acknowledged (pod UNARMED: clean up at
#   once); 5 bulk transfer or pinned-input verification on the pod failed (pod armed); 6 setup
#   failed or timed out (pod armed). Every non-zero code means: run cleanup for the slot.
# - Setup: pod_setup.sh --only e9 (venv-peft + torchao==0.18.0, venv-vllm).
# 2026-10-08 (EQ0 implementation plan, pod tooling unit; I9's single-pod flow): `--exp EQ0`,
#   slot _A only (one pod; emx.eq0.contracts.SLOT).
# - Refuses modified tracked files, as I9 does. Pod env as I9's: HF_TOKEN only (never HF_USERNAME:
#   EQ0 pushes nothing to the Hub), EMX_EXP=EQ0, HF_HOME=/ws/hf_cache, HF_HUB_DISABLE_XET=1,
#   WANDB_MODE=offline, and the slot pod's rate and creation time.
# - Transfer roots (tracked files only): src scripts data/EQ0 data/E1/questions/general.jsonl
#   data/E1b/questions/ext48.jsonl pyproject.toml uv.lock (two single-file roots; never E1's
#   held-out data/E1/questions/narrow.jsonl, which a real or dry push refuses outright). Named
#   extras: every gitignored file under data/EQ0/ (the training copy bad_medical_advice.jsonl) and
#   logs/approvals/EQ0.approved. A real push refuses when a required input
#   (data/EQ0/bad_medical_advice.jsonl, both question files, scripts/eq0_pod.py,
#   scripts/pod_run_eq0.sh, the approval) is missing; the dry run warns.
# - Setup: pod_setup.sh --only eq0 (/ws/venv-peft and /ws/venv-vllm). After the transfer, the
#   training file and both question files must be on the pod. The killswitch is armed as for
#   every experiment, at EQ0's GPU kill line (emx.spend.CAPS["EQ0"]).
# 2026-10-08 (E12 implementation plan v0.3.1, unit U5; a copy of EQ0's flow): `--exp E12 --slot _A
#   [--recipe rank1|rank32|tan32]` (one pod; emx.e12.contracts.SLOT; --recipe is E12-only, default
#   rank1 = contracts.DEFAULT_RECIPE).
# - Refuses modified tracked files, as EQ0 does. Pod env as EQ0's (HF_TOKEN, EMX_EXP=E12,
#   HF_HOME, HF_HUB_DISABLE_XET=1, WANDB_MODE, the slot pod's rate and creation time) plus
#   HF_USERNAME when .env has it (the `upload` step pushes the adapters to private
#   e12-<recipe>-adapters repos) and E12_RECIPE=<recipe>.
# - Transfer roots (tracked files only): src scripts data/E12 data/E1/questions/general.jsonl
#   data/E1b/questions/ext48.jsonl pyproject.toml uv.lock (never narrow.jsonl, refused outright as
#   for EQ0). Named extras: every gitignored file under data/E12/ (contracts.DATA_FILES), the
#   pre-run manifest results/E12/prerun_manifest.json, the frozen style-audit features
#   results/E12/style_audit/frozen.json, EQ0's base answers results/EQ0/gen/base.csv and
#   results/EQ0/judged/base.csv (contracts.EQ0_BASE_GEN, EQ0_BASE_JUDGED) and
#   logs/approvals/E12.approved. A real push refuses when any of them, a question file,
#   scripts/e12_pod.py or scripts/pod_run_e12.sh is missing; the dry run warns. (Their sha256 are
#   checked on the pod by the `download` step against the pre-run manifest.)
# - Setup: pod_setup.sh --only e12 (/ws/venv-peft and /ws/venv-vllm). After the transfer, the data
#   files, both question files and the pre-run manifest must be on the pod. The killswitch is
#   armed as for every experiment, at E12's GPU kill line (emx.spend.CAPS["E12"]); the pod's own
#   reservation deadline (pod_up.py) is at most 7 h.
# --dry-run builds and checks the pod env and the transfer set, prints key names (never values),
# the file roots and the setup command, and contacts nothing.
# Usage: bash scripts/pod_push.sh [--exp I1a|E1b|I9|E3] --slot _A [--no-setup] [--dry-run [--show-files]]
#        bash scripts/pod_push.sh --exp E3 --slot _A --arm-only [--dry-run]
#        bash scripts/pod_push.sh --exp E9 --slot _A --runs bg-c1[,orig-c1] [--no-setup] [--dry-run]
#        bash scripts/pod_push.sh --exp E9 --slot _A --arm-only [--dry-run]
#        bash scripts/pod_push.sh --exp EQ0 --slot _A [--no-setup] [--dry-run [--show-files]]
#        bash scripts/pod_push.sh --exp E12 --slot _A [--recipe rank1|rank32|tan32] [--no-setup] [--dry-run [--show-files]]
set -euo pipefail
cd "$(dirname "$0")/.."

DOTENV="${POD_PUSH_DOTENV:-.env}"   # tests point this at a fake
envval() { { grep -m1 "^$1=" "$DOTENV" || true; } | cut -d= -f2- ; }  # a missing key is empty, not fatal under pipefail

EXP="E1"; SLOT=""; SETUP=1; DRY=0; SHOW=0; ARM_ONLY=0; E9_RUNS=""
E12_RECIPE=""   # E12 only (--recipe)
GIT="${POD_PUSH_GIT:-git}"   # tests inject a fake; only read-only git commands are used
while [ $# -gt 0 ]; do
  case "$1" in
    --exp) EXP="${2:?--exp needs a value}"; shift 2 ;;
    --slot) SLOT="${2:?--slot needs a value}"; shift 2 ;;
    --no-setup) SETUP=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --show-files) SHOW=1; shift ;;
    --arm-only) ARM_ONLY=1; shift ;;
    --runs) E9_RUNS="${2:?--runs needs a value}"; shift 2 ;;
    --recipe) E12_RECIPE="${2:?--recipe needs a value}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$EXP" in
  E1|I1a|E1b|I9|E3|E9) ;;
  E12) ;;   # 2026-10-08 (E12), kept apart from the lines around it
  EQ0) ;;   # 2026-10-08, kept apart from the line above
  *) echo "--exp $EXP: pod_push.sh knows E1, I1a, E1b and I9 only, plus E3, E9, EQ0 and E12" >&2; exit 2 ;;
esac
if [ "$ARM_ONLY" = "1" ] && [ "$EXP" != "E3" ] && [ "$EXP" != "E9" ]; then
  echo "--arm-only is E3/E9-only" >&2; exit 2
fi
if [ -n "$E9_RUNS" ] && [ "$EXP" != "E9" ]; then
  echo "--runs is E9-only" >&2; exit 2
fi
if [ -n "$E9_RUNS" ]; then   # E9: the slot's planned runs, RUN or RUN:KIND, comma-separated
  case "$E9_RUNS" in
    *[!a-z0-9,:-]*|,*|*,|*,,*) echo "--runs '$E9_RUNS': comma-separated RUN[:KIND] ([a-z0-9-]) only" >&2; exit 2 ;;
  esac
fi
E9_RUN_LIST=""
[ -z "$E9_RUNS" ] || E9_RUN_LIST=$(printf '%s\n' "$E9_RUNS" | tr ',' '\n' | sed 's/:.*$//' | tr '\n' ' ' | sed 's/ *$//')
if [ "$EXP" = "I9" ]; then   # the slot names the session (see pod_up.py)
  case "$SLOT" in
    _A) I9_STEP=P1 ;;
    _B) I9_STEP=P3 ;;
    *) echo "--exp I9: slot must be _A (session A) or _B (session B), not '$SLOT'" >&2; exit 2 ;;
  esac
fi
if [ -n "$E12_RECIPE" ] && [ "$EXP" != "E12" ]; then
  echo "--recipe is E12-only" >&2; exit 2
fi
if [ "$EXP" = "E12" ]; then   # one pod (emx.e12.contracts.SLOT); the recipe (contracts.RECIPES)
  [ "$SLOT" = "_A" ] || { echo "--exp E12: slot must be _A, not '$SLOT'" >&2; exit 2; }
  E12_RECIPE=${E12_RECIPE:-rank1}
  case "$E12_RECIPE" in
    rank1|rank32|tan32) ;;
    *) echo "--exp E12: --recipe must be rank1, rank32 or tan32, not '$E12_RECIPE'" >&2; exit 2 ;;
  esac
fi
if [ "$EXP" = "EQ0" ] && [ "$SLOT" != "_A" ]; then   # one pod (emx.eq0.contracts.SLOT)
  echo "--exp EQ0: slot must be _A, not '$SLOT'" >&2; exit 2
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
    || { echo "$EXP killswitch: could not compute the per-slot deadlines (emx.runpod --per-slot failed)" >&2; return 1; }
  # EMX_KILLSWITCH_DEADLINE_<slot>=<epoch>, the slot keeping its leading underscore (..._ _A)
  per=$(printf '%s\n' "$out" \
    | sed -n 's/^EMX_KILLSWITCH_DEADLINE__*\([A-Za-z0-9]\{1,8\}\)=\([0-9][0-9]*\)$/_\1 \2/p')
  slots=$(printf '%s\n' "$out" | sed -n 's/^EMX_KILLSWITCH_SLOTS=//p')
  for s in $SLOT $slots $(printf '%s\n' "$per" | cut -d' ' -f1); do   # this slot first; once each
    case " $seen " in *" $s "*) continue ;; esac
    seen="$seen $s"
    case "$s" in
      _*[!A-Za-z0-9_]*|_) echo "$EXP killswitch $s: bad slot name" >&2; fail=1; continue ;;
      _*) ;;
      *) echo "$EXP killswitch $s: bad slot name" >&2; fail=1; continue ;;
    esac
    dl=$(printf '%s\n' "$per" | sed -n "s/^$s \([0-9][0-9]*\)\$/\1/p" | sort -u)
    case "$dl" in
      "") echo "$EXP killswitch $s: NOT armed (no per-slot deadline for this running slot)" >&2
          fail=1; continue ;;
      *[!0-9]*) echo "$EXP killswitch $s: NOT armed (conflicting per-slot deadlines)" >&2
          fail=1; continue ;;
    esac
    pid=$(envval "RUNPOD_POD_ID$s" || true)
    line=$(arm_slot "$s" "$dl")
    parsed=$(printf '%s\n' "$line" | sed -n \
      's/.* ARMED pod=\([A-Za-z0-9][A-Za-z0-9]*\) deadline=\([0-9][0-9]*\) .*key_check=\([^ ]*\).*/\1 \2 \3/p')
    if [ -z "$parsed" ]; then
      echo "$EXP killswitch $s: NOT armed or unacknowledged ($line); nothing recorded" >&2
      fail=1; continue
    fi
    # shellcheck disable=SC2086 # three space-free fields
    set -- $parsed
    apod=$1; adl=$2; kc=$3
    if [ -z "$pid" ] || [ "$apod" != "$pid" ]; then
      echo "$EXP killswitch $s: ARMED line names pod $apod, not the slot's pod '$pid'; nothing recorded" >&2
      fail=1; continue
    fi
    if [ "$adl" -gt "$dl" ]; then
      echo "$EXP killswitch $s: acknowledged deadline $adl is LATER than the requested $dl; nothing recorded" >&2
      fail=1; continue
    fi
    case "$kc" in
      ok|read403) ;;
      *) echo "$EXP killswitch $s: armed but its pod key is unchecked (key_check=$kc): uncertain; nothing recorded" >&2
         fail=1; continue ;;
    esac
    if ! uv run --quiet python -m emx.runpod_reservations ack --pod "$apod" --deadline "$adl"; then
      echo "$EXP killswitch $s: emx.runpod_reservations refused the acknowledgement (pod $apod, deadline $adl)" >&2
      fail=1; continue
    fi
    echo "killswitch $s: $line (acknowledged: deadline $adl <= requested $dl)"
  done
  [ "$fail" = "0" ]
}
if [ "$ARM_ONLY" = "1" ]; then   # E3 or E9 only (checked above): re-arm, nothing else
  if [ "$DRY" = "1" ]; then
    echo "DRY RUN ($EXP): --arm-only: would run: python -m emx.runpod --exp $EXP --check-slot $SLOT --killswitch-deadline --per-slot"
    echo "DRY RUN ($EXP): --arm-only: would arm every running $EXP slot with its own deadline (pod_killswitch.sh arm) and record each ack (python -m emx.runpod_reservations ack); no transfer, no setup"
    exit 0
  fi
  arm_e3 || { echo "$EXP --arm-only: arming failed or unacknowledged on at least one slot; run $(printf '%s' "$EXP" | tr 'A-Z' 'a-z')_orchestrate.py cleanup for it" >&2; exit 1; }
  echo "$EXP killswitch re-armed on every running slot"
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
  if [ "$EXP" = "EQ0" ] && [ "$key" = "HF_USERNAME" ]; then continue; fi   # HF_TOKEN only
  if [ -z "$val" ] && [ "$EXP" = "E12" ] && [ "$key" = "HF_USERNAME" ]; then continue; fi   # optional
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
# E9 (2026-10-06): its orchestrator's logs and gate log, likewise.
RUN_LOGS='^ M (logs/(pods_[A-Za-z0-9]+(\.pending)?\.jsonl|costs\.jsonl|E3-orchestrate\.jsonl|E9-orchestrate\.jsonl)|results/E3/(cuts\.json|reservations_snapshot\.jsonl|measured_rates\.json)|results/E9/(gates\.jsonl|reservations_snapshot\.jsonl|measured_rates\.json))$'
if [ "$EXP" = "I1a" ] || [ "$EXP" = "E1b" ] || [ "$EXP" = "I9" ] || [ "$EXP" = "E3" ] || [ "$EXP" = "E9" ] || [ "$EXP" = "EQ0" ]; then   # the sidecars' code_commit must fully pin the code the pod runs
  MODIFIED=$($GIT status --porcelain --untracked-files=no) \
    || { echo "git status failed; refusing" >&2; exit 1; }
  MODIFIED=$(printf '%s\n' "$MODIFIED" | grep -Ev "$RUN_LOGS" | grep -v '^$' || true)
  if [ -n "$MODIFIED" ]; then
    echo "modified tracked files (EMX_GIT_DIRTY=1): commit them before an $EXP push; refusing:" >&2
    printf '%s\n' "$MODIFIED" >&2
    exit 1
  fi
fi
if [ "$EXP" = "E12" ]; then   # 2026-10-08 (E12): as EQ0's check above
  E12_MOD=$($GIT status --porcelain --untracked-files=no) \
    || { echo "git status failed; refusing" >&2; exit 1; }
  E12_MOD=$(printf '%s\n' "$E12_MOD" | grep -Ev "$RUN_LOGS" | grep -v '^$' || true)
  if [ -n "$E12_MOD" ]; then
    echo "modified tracked files (EMX_GIT_DIRTY=1): commit them before an $EXP push; refusing:" >&2
    printf '%s\n' "$E12_MOD" >&2
    exit 1
  fi
fi
# Dirty = modified tracked files other than the run ledgers, as emx.i9.provenance records it
# (untracked pod logs, heartbeats and outputs are what a run writes).
printf 'EMX_GIT_COMMIT=%s\nEMX_GIT_DIRTY=%s\n' "$HEAD_SHA" \
  "$( [ -z "$($GIT status --porcelain --untracked-files=no | grep -Ev "$RUN_LOGS")" ] && echo 0 || echo 1 )" \
  >> "$POD_ENV"
printf 'WANDB_MODE=offline\nTOKENIZERS_PARALLELISM=false\nHF_HOME=/ws/hf_cache\nEMX_EXP=%s\n' "$EXP" >> "$POD_ENV"
if [ "$EXP" = "I1a" ] || [ "$EXP" = "I9" ] || [ "$EXP" = "EQ0" ]; then
  printf 'HF_HUB_DISABLE_XET=1\n' >> "$POD_ENV"   # plain HTTP downloads (emx.i1a.hfget)
fi
if [ "$EXP" = "E12" ]; then   # as EQ0's downloads; the recipe was validated above (shell-inert)
  printf 'HF_HUB_DISABLE_XET=1\nE12_RECIPE=%s\n' "$E12_RECIPE" >> "$POD_ENV"
  # recorded API spend (this checkout's ledger), for the on-pod admission projections
  E12_API=$(uv run --quiet python -c 'from emx import spend; print(round(spend.spent("judge", exp="E12"), 4))' 2>/dev/null || echo 0)
  case "$E12_API" in ''|*[!0-9.]*) E12_API=0 ;; esac
  printf 'E12_API_SPENT_USD=%s\n' "$E12_API" >> "$POD_ENV"
  # GPU $ earlier E12 pods recorded (this checkout's ledger; prereg Amendment D review): the
  # on-pod projections add it to this pod's GPU $ for the GPU-limit and hard-cap checks
  E12_GPU=$(uv run --quiet python -c 'from emx import spend; print(round(spend.spent("gpu", exp="E12"), 4))' 2>/dev/null || true)
  case "$E12_GPU" in ''|*[!0-9.]*|*.*.*) echo "recorded E12 GPU spend unreadable ('$E12_GPU'); refusing (code review, Amendment D r1)" >&2; exit 1 ;; esac
  printf 'E12_GPU_SPENT_USD=%s\n' "$E12_GPU" >> "$POD_ENV"
fi
if [ "$EXP" = "E9" ]; then   # the slot and its planned runs (validated above: shell-inert)
  printf 'EMX_E9_SLOT=%s\nEMX_E9_RUNS=%s\n' "$SLOT" "$E9_RUNS" >> "$POD_ENV"
  E9_POD_ID=$(envval "RUNPOD_POD_ID$SLOT" || true)
  if [ "$DRY" = "1" ]; then E9_POD_ID=${E9_POD_ID:-PODID}; fi
  case "$E9_POD_ID" in ""|*[!A-Za-z0-9]*) echo "no valid RUNPOD_POD_ID$SLOT in .env; refusing" >&2; exit 1 ;; esac
  printf 'EMX_POD_ID=%s\n' "$E9_POD_ID" >> "$POD_ENV"
fi

# the slot's pod must be one this experiment created in this slot (offline event-log check)
if [ "$DRY" = "1" ]; then
  echo "DRY RUN ($EXP): would check: python -m emx.runpod --exp $EXP --check-slot $SLOT"
else
  SESSION=$(uv run --quiet python -m emx.runpod --exp "$EXP" --check-slot "$SLOT" --session-env) \
    || { echo "slot $SLOT does not hold a $EXP pod; refusing" >&2; exit 1; }
  if [ "$EXP" = "I1a" ] || [ "$EXP" = "E1b" ] || [ "$EXP" = "I9" ] || [ "$EXP" = "E3" ] || [ "$EXP" = "E9" ] || [ "$EXP" = "EQ0" ]; then
    printf '%s\n' "$SESSION" | grep -E '^EMX_POD_(RATE_USD_H|CREATED_EPOCH)=[0-9.]+$' >> "$POD_ENV" \
      || { echo "no rate/creation time for slot $SLOT in the event log; refusing" >&2; exit 1; }
  fi
  if [ "$EXP" = "E12" ]; then   # as EQ0's
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
  E9) ROOTS="$ROOTS configs/E9 data/E1 data/E1b/questions data/E1b/manifest.json data/E3/question_swaps.json data/shared" ;;
  E12) ROOTS="src scripts data/E12 data/E1/questions/general.jsonl data/E1b/questions/ext48.jsonl pyproject.toml uv.lock" ;;  # never data/E1/questions/narrow.jsonl
  EQ0) ROOTS="src scripts data/EQ0 data/E1/questions/general.jsonl data/E1b/questions/ext48.jsonl pyproject.toml uv.lock" ;;  # never data/E1/questions/narrow.jsonl
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
if [ "$EXP" = "E9" ]; then
  # frozen values from src/emx/e9/contracts.py and (E1's pinned inputs, the main checkout)
  # src/emx/e3/contracts.py, both stdlib only and loaded by path; the run list is checked here
  E9PY=python3; [ -x .venv/bin/python ] && E9PY=.venv/bin/python
  # shellcheck disable=SC2086 # E9_RUN_LIST: validated run ids, space-separated
  E9_INFO=$("$E9PY" - src/emx/e9/contracts.py src/emx/e3/contracts.py $E9_RUN_LIST <<'PY_E9'
import importlib.util
import json
import os
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m  # dataclasses look their module up while the class is built
    spec.loader.exec_module(m)
    return m


C = load("e9_contracts", sys.argv[1])
E3 = load("e3_contracts", sys.argv[2])
runs = sys.argv[3:]
if not runs or any(r.startswith("smoke") for r in runs):  # every stream present
    d = C.STREAMS_DIR
    runs = sorted({f[: -len(".npy")] for f in (os.listdir(d) if os.path.isdir(d) else ())
                   if f.endswith(".npy")} | {r for r in runs if not r.startswith("smoke")})
print("MAIN", E3.MAIN_CHECKOUT)
for p in E3.MAIN_CHECKOUT_ONLY:
    print("MAINONLY", p)
for p, h in E3.input_sha256().items():
    print("PIN", p, h)
print("PIN", C.PAIRS_FILE, C.PAIRS_SHA256)
print("PIN", C.SWAPS_FILE, C.SWAPS_SHA256)
for p in (C.PAIRS_FILE, C.SELECTION_FILE, C.PREINSPECT_EVIDENCE, C.SWAPS_FILE,
          f"{C.POOLS_DIR}/heldout_text.jsonl", "scripts/pod_run_e9.sh"):
    print("NEED", p)
for p in (f"{C.POOLS_DIR}/heldout_text.manifest.json", f"{C.INSERT_DIR}/versions.jsonl",
          f"{C.INSERT_DIR}/insert_manifest.json"):
    print("OPT", p)
for r in runs:
    if r.startswith("smoke"):
        continue
    npy, man = f"{C.STREAMS_DIR}/{r}.npy", f"{C.STREAMS_DIR}/{r}.manifest.json"
    print("NEED", npy)
    print("NEED", man)
    if os.path.isfile(man):
        try:
            h = json.load(open(man)).get("stream_sha256")
        except ValueError:
            h = None
        if isinstance(h, str) and len(h) == 64:
            print("PIN", npy, h)
        else:
            print("BADMAN", man)
PY_E9
) || { echo "could not read the E9/E3 contracts; refusing" >&2; exit 1; }
  E3_MAIN=${POD_PUSH_MAIN_CHECKOUT:-$(printf '%s\n' "$E9_INFO" | sed -n 's/^MAIN //p')}   # E3_MAIN: the main checkout (shared with E3's path below)
  E3_MAINONLY=$(printf '%s\n' "$E9_INFO" | sed -n 's/^MAINONLY //p')
  E9_NEED=$(printf '%s\n' "$E9_INFO" | sed -n 's/^NEED //p')
  E9_OPT=$(printf '%s\n' "$E9_INFO" | sed -n 's/^OPT //p')
  E9_BADMAN=$(printf '%s\n' "$E9_INFO" | sed -n 's/^BADMAN //p')
  printf '%s\n' "$E9_INFO" | sed -n 's/^PIN \([^ ]*\) \([0-9a-f]\{64\}\)$/\2  \1/p' > "$PINS"
  [ -d "$E3_MAIN" ] || { echo "E9: main checkout $E3_MAIN not found; refusing" >&2; exit 1; }
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
elif [ "$EXP" = "E9" ]; then
  # the main checkout's gitignored E1 inputs go by their own rsync, never from this checkout
  for f in $E3_MAINONLY; do
    if grep -qxF "$f" "$LIST"; then
      grep -vxF "$f" "$LIST" > "$LIST.tmp" || true
      mv "$LIST.tmp" "$LIST"
    fi
    if [ -f "$E3_MAIN/$f" ]; then echo "$f" >> "$MAIN_LIST"; fi
  done
  # named gitignored inputs: the answer pairs (every answer_pairs_v1* file), the held-out text,
  # the insert versions, the slot's streams and their manifests
  for f in $E9_NEED $E9_OPT data/shared/answer_pairs_v1*; do
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
  E9_MISSING=""
  for f in $E9_NEED $(cut -c67- "$PINS"); do
    grep -qxF "$f" "$LIST" || grep -qxF "$f" "$MAIN_LIST" || E9_MISSING="$E9_MISSING $f"
  done
  E9_BAD="$E9_BADMAN"   # a stream manifest without a stream_sha256 counts as a mismatch
  while read -r want f; do
    [ -n "$f" ] || continue
    if grep -qxF "$f" "$MAIN_LIST"; then src="$E3_MAIN/$f"
    elif grep -qxF "$f" "$LIST"; then src="$f"
    else continue; fi   # missing: reported above
    got=$(shasum -a 256 "$src" | cut -d' ' -f1)
    [ "$got" = "$want" ] || E9_BAD="$E9_BAD $f"
  done < "$PINS"
  if [ -n "$E9_MISSING$E9_BAD" ]; then
    if [ "$DRY" = "1" ]; then
      [ -z "$E9_MISSING" ] || echo "DRY RUN (E9): WARNING: required inputs missing (a real push refuses):$E9_MISSING"
      [ -z "$E9_BAD" ] || echo "DRY RUN (E9): WARNING: sha256 mismatch against contracts or stream manifests (a real push refuses):$E9_BAD"
    else
      [ -z "$E9_MISSING" ] || echo "required E9 inputs missing from the transfer set; refusing:$E9_MISSING" >&2
      [ -z "$E9_BAD" ] || echo "E9 inputs do not match their pinned sha256; refusing:$E9_BAD" >&2
      exit 1
    fi
  fi
elif [ "$EXP" = "E12" ]; then
  # 2026-10-08 (E12, a copy of EQ0's branch below): gitignored files under data/E12 (the copied
  # source data; untracked, non-ignored ones were refused above), the pre-run manifest, the frozen
  # style-audit features, EQ0's base answers (emx.e12.contracts.EQ0_BASE_GEN, EQ0_BASE_JUDGED) and
  # the approval file (the pod chain refuses to run without it)
  E12_EXTRAS=$(find data/E12 -type f ! -name .DS_Store 2>/dev/null | LC_ALL=C sort || true)
  E12_NAMED="results/E12/prerun_manifest.json results/E12/style_audit/frozen.json results/EQ0/gen/base.csv results/EQ0/judged/base.csv logs/approvals/E12.approved"
  for f in $E12_EXTRAS $E12_NAMED; do
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
  # E1's held-out narrow questions never leave the laptop (gitignored; outside every E12 root)
  if grep -qxF data/E1/questions/narrow.jsonl "$LIST"; then
    echo "data/E1/questions/narrow.jsonl (held out) is in the E12 transfer set; refusing" >&2; exit 1
  fi
  E12_MISSING=""
  for f in data/E12/bad_medical_advice.jsonl data/E12/mixture.jsonl data/E12/mixture_test.jsonl \
           data/E12/holdout_prompts.jsonl data/E12/replay_clean.jsonl data/E12/replay_benign.jsonl \
           data/E12/S25.json data/E1/questions/general.jsonl data/E1b/questions/ext48.jsonl \
           scripts/e12_pod.py scripts/pod_run_e12.sh $E12_NAMED; do
    grep -qxF "$f" "$LIST" || E12_MISSING="$E12_MISSING $f"
  done
  if [ -n "$E12_MISSING" ]; then
    if [ "$DRY" = "1" ]; then
      echo "DRY RUN (E12): WARNING: required inputs missing (a real push refuses):$E12_MISSING"
    else
      echo "required E12 inputs missing from the transfer set; refusing:$E12_MISSING" >&2; exit 1
    fi
  fi
elif [ "$EXP" = "EQ0" ]; then
  # gitignored files under data/EQ0 (the training copy; untracked, non-ignored ones were refused
  # above) and the approval file (the pod chain refuses to run without it; the main session
  # writes it after Jacob's go-ahead)
  EQ0_EXTRAS=$(find data/EQ0 -type f ! -name .DS_Store 2>/dev/null | LC_ALL=C sort || true)
  for f in $EQ0_EXTRAS logs/approvals/EQ0.approved; do
    if [ -f "$f" ] && ! grep -qxF "$f" "$LIST"; then echo "$f" >> "$LIST"; fi
  done
  # E1's held-out narrow questions never leave the laptop (gitignored; outside every EQ0 root)
  if grep -qxF data/E1/questions/narrow.jsonl "$LIST"; then
    echo "data/E1/questions/narrow.jsonl (held out) is in the EQ0 transfer set; refusing" >&2; exit 1
  fi
  EQ0_MISSING=""
  for f in data/EQ0/bad_medical_advice.jsonl data/E1/questions/general.jsonl \
           data/E1b/questions/ext48.jsonl scripts/eq0_pod.py scripts/pod_run_eq0.sh \
           logs/approvals/EQ0.approved; do
    grep -qxF "$f" "$LIST" || EQ0_MISSING="$EQ0_MISSING $f"
  done
  if [ -n "$EQ0_MISSING" ]; then
    if [ "$DRY" = "1" ]; then
      echo "DRY RUN (EQ0): WARNING: required inputs missing (a real push refuses):$EQ0_MISSING"
    else
      echo "required EQ0 inputs missing from the transfer set; refusing:$EQ0_MISSING" >&2; exit 1
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
  local f b low
  # bash 3.2 has no lowercase expansion: one tr lowercases the whole list into a temp file, read in
  # step with the original (a tr per file forked ~2 processes each: ~9 s for a 4,000-file set).
  # LC_ALL=C keeps tr byte-safe (in a UTF-8 locale one invalid byte aborts it); the patterns are
  # ASCII, so C-locale case mapping matches the same names. Any tr failure refuses the push.
  low=$(mktemp) || return 1
  if ! LC_ALL=C tr '[:upper:]' '[:lower:]' < "$1" > "$low"; then
    rm -f "$low"
    return 1
  fi
  while IFS= read -r f && IFS= read -r b <&3; do
    b=${b##*/}
    case "$b" in
      *.env*|*.pem|*id_rsa*|*credential*|*.key|.netrc|.pypirc|*.p12) echo "$f" ;;
      *secret*|*apikey*|*api_key*) echo "$f" ;;
      *token*)
        case "$b" in
          *.py|*.jsonl) ;;
          *) echo "$f" ;;
        esac ;;
    esac
  done < "$1" 3< "$low"
  rm -f "$low"
}
SECRETS=$(secret_names "$CHECKLIST") || {
  echo "the secret-name check could not lowercase the transfer list; refusing" >&2
  exit 1
}
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
[ "$EXP" = "E9" ] && SETUP_CMD="$SETUP_CMD --only e9"
[ "$EXP" = "EQ0" ] && SETUP_CMD="$SETUP_CMD --only eq0"
[ "$EXP" = "E12" ] && SETUP_CMD="$SETUP_CMD --only e12"
if [ "$DRY" = "1" ]; then
  echo "DRY RUN ($EXP): pod env keys: $(cut -d= -f1 "$POD_ENV" | tr '\n' ' ')"
  if [ "$EXP" = "E12" ]; then echo "DRY RUN (E12): recipe $E12_RECIPE (E12_RECIPE in the pod env)"; fi
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
  elif [ "$EXP" = "E9" ]; then
    echo "DRY RUN (E9): runs on slot $SLOT: ${E9_RUN_LIST:-(not given: every stream is sent)}"
    echo "DRY RUN (E9): order: 1. send scripts/pod_killswitch.sh only (one ssh call, 120 s); 2. arm; 3. bulk transfer; 4. verify pins; 5. setup"
    echo "DRY RUN (E9): would arm each running E9 slot at its OWN budget deadline BEFORE the bulk transfer (python -m emx.runpod --exp E9 --check-slot $SLOT --killswitch-deadline --per-slot; never the global kill line), require a matching ARMED line and record it (python -m emx.runpod_reservations ack --pod <id> --deadline <epoch>); failure exits 4"
    echo "DRY RUN (E9): would verify the pinned inputs on the pod: sha256sum -c ($(wc -l < "$PINS" | tr -d ' ') files); failure exits 5"
  else
    echo "DRY RUN ($EXP): would arm: bash /ws/emx/scripts/pod_killswitch.sh arm <pod id> <deadline from python -m emx.runpod --killswitch-deadline>"
  fi
  [ "$SETUP" = "1" ] && echo "DRY RUN ($EXP): would run: $SETUP_CMD"
  exit 0
fi

# xfer_fail RC: a failed transfer step. E9 exits 5 (the pod is armed; review R3 #2); the other
# experiments keep their old behaviour (exit with the failed command's code, as set -e did).
xfer_fail() {
  if [ "$EXP" = "E9" ]; then
    echo "E9: bulk transfer failed (rc=$1); the pod IS armed at its budget deadline; run e9_orchestrate.py cleanup" >&2
    exit 5
  fi
  exit "$1"
}
# E9 (code review R3 #2): the killswitch first. Only scripts/pod_killswitch.sh (self-contained)
# goes over one bounded ssh call (120 s), then every running E9 slot is armed at its own budget
# deadline and the acknowledgement is verified and recorded (arm_e3). Only then does the bulk
# transfer start, so a stalled upload can never outlive the pod's funded deadline. Exit 4: the
# killswitch could not be sent, armed or acknowledged (the pod is UNARMED; clean it up now).
if [ "$EXP" = "E9" ]; then
  # shellcheck disable=SC2086 # SSH: the ssh command and its options
  perl -e 'alarm 120; exec @ARGV' $SSH 'mkdir -p /ws/emx/scripts /ws/emx/logs && cat > /ws/emx/scripts/pod_killswitch.sh.part && chmod 755 /ws/emx/scripts/pod_killswitch.sh.part && mv -f /ws/emx/scripts/pod_killswitch.sh.part /ws/emx/scripts/pod_killswitch.sh' < scripts/pod_killswitch.sh \
    || { echo "E9: could not send scripts/pod_killswitch.sh (bounded 120 s); the pod is UNARMED; run e9_orchestrate.py cleanup" >&2; exit 4; }
  arm_e3 || { echo "E9: killswitch arming failed or unacknowledged on at least one slot; the pod is UNARMED; run e9_orchestrate.py cleanup" >&2; exit 4; }
  echo "E9: killswitch armed and acknowledged before the bulk transfer"
fi
$SSH 'command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync); mkdir -p /ws/emx /ws/hf_cache /ws/uv_cache /ws/tmp' || xfer_fail $?
rsync -lptz --files-from="$LIST" -e "$RSYNC_SSH" ./ "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/" || xfer_fail $?
if [ -s "$MAIN_LIST" ]; then   # E3, E9: the gitignored pinned inputs, from the main checkout
  rsync -lptz --files-from="$MAIN_LIST" -e "$RSYNC_SSH" "$E3_MAIN/" "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/" || xfer_fail $?
fi
rsync -rlptz -e "$RSYNC_SSH" "$POD_ENV" "$POD_SSH_USER@$POD_SSH_HOST:/ws/emx/.env" || xfer_fail $?
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
$SSH 'chmod 600 /ws/emx/.env' || xfer_fail $?
if [ "$EXP" = "I1a" ]; then   # the I1a pod code needs both; fail loudly if either did not arrive
  $SSH 'test -f /ws/emx/third_party/workspace-bench/pyproject.toml && test -d /ws/emx/data/I1a' \
    || { echo "third_party/workspace-bench or data/I1a missing on the pod" >&2; exit 1; }
fi
if [ "$EXP" = "I9" ]; then   # the I9 pod code needs all three; fail loudly if one did not arrive
  $SSH 'test -f /ws/emx/third_party/workspace-bench/pyproject.toml && test -d /ws/emx/third_party/agentic-misalignment && test -f /ws/emx/data/I9/scenarios.jsonl' \
    || { echo "third_party/workspace-bench, third_party/agentic-misalignment or data/I9 missing on the pod" >&2; exit 1; }
fi
if [ "$EXP" = "EQ0" ]; then   # the EQ0 pod code needs all three; fail loudly if one did not arrive
  $SSH 'test -s /ws/emx/data/EQ0/bad_medical_advice.jsonl && test -s /ws/emx/data/E1/questions/general.jsonl && test -s /ws/emx/data/E1b/questions/ext48.jsonl' \
    || { echo "data/EQ0/bad_medical_advice.jsonl or a question file (general, ext48) missing on the pod" >&2; exit 1; }
fi
if [ "$EXP" = "E12" ]; then   # the E12 pod code needs these; fail loudly if one did not arrive
  $SSH 'cd /ws/emx && for f in data/E12/mixture.jsonl data/E12/replay_clean.jsonl data/E12/replay_benign.jsonl data/E12/bad_medical_advice.jsonl data/E1/questions/general.jsonl data/E1b/questions/ext48.jsonl results/E12/prerun_manifest.json; do test -s "$f" || exit 1; done' \
    || { echo "an E12 data file, a question file (general, ext48) or the pre-run manifest is missing on the pod" >&2; exit 1; }
fi
# Pod-side spend backstop (scripts/pod_killswitch.sh, 2026-10-03): each pod stops itself when
# the experiment's running pods together reach its GPU kill line, even while the laptop sleeps.
# The deadline counts every running pod, so every running pod of the experiment is re-armed with
# it (re-arming only moves a deadline). Self-stop verified live 2026-10-03 (logs/infra/), through
# runpodctl only. Failing to arm is loud but not fatal: the laptop watchdog still runs (E3: fatal,
# see arm_e3; the pinned inputs are verified on the pod only once the pod is armed).
if [ "$EXP" = "E3" ]; then   # per-slot deadlines and a verified acknowledgement; failure is fatal
  arm_e3 || { echo "E3: killswitch arming failed or unacknowledged on at least one slot; refusing to continue (run e3_orchestrate.py cleanup)" >&2; exit 1; }
elif [ "$EXP" = "E9" ]; then   # armed (and acknowledged) before the bulk transfer, above
  :
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
if [ "$EXP" = "E3" ] || [ "$EXP" = "E9" ]; then   # every pinned input arrived intact (paths are repo-relative)
  $SSH 'cd /ws/emx && sha256sum -c --quiet -' < "$PINS" \
    || { echo "$EXP pinned inputs do not verify on the pod (sha256sum -c)" >&2; [ "$EXP" = "E9" ] && exit 5; exit 1; }
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
      [ "$rc" = "0" ] || { echo "pod_setup.sh failed on the pod (rc=$rc; see /ws/setup.log)" >&2; [ "$EXP" = "E9" ] && exit 6; exit 1; }
      break
    fi
    [ "$waited" -lt "$SETUP_MAX_S" ] || { echo "pod setup not done after ${SETUP_MAX_S}s" >&2; [ "$EXP" = "E9" ] && exit 6; exit 1; }
  done
fi
echo "pod synced$( [ "$SETUP" = "1" ] && echo ' and set up')"
