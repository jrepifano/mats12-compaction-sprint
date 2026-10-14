#!/usr/bin/env bash
# P48 pod entry point (scripts/probe_pod.py run contract; plan.md "Automatic rules", on the pod).
# The committed file `phase` (env P48_PHASE overrides) picks the session:
#   session1: download -> smoke (gates the bulk run) -> teacher (T0's fine-tuned teacher plus its
#             manipulation check, which runs in the NEUTRAL context, never under the cat prompt)
#             -> gen (number rows for all three sources on one prompt set) -> mix (both candidate
#             organisms' mixtures) -> t0 (its 100% student and the shared 0% control, then the
#             promotion decision, on TRANSFER only) -> gate (the remaining gate students for the
#             promoted organism, plus the prompted 100% student, which is kept either way) ->
#             gatedec (f* and the branch) -> score (likelihoods, attribution and directional
#             derivatives on the f* student and the 100% student)
#   session1b: branch B2's recalibration: the 10-epoch gate at {0, 90, 100}%, then eval, gatedec
#             and score. The 3-epoch curve is not reused.
#   session2: download -> kpilot (the seed-0 k ladder, which freezes k) -> arms -> train the keys
#             in `stage2_keys` -> eval them
# Steps run p48/pod.py in /ws/venv-peft (training, scoring) or /ws/venv-vllm (generation, eval).
# A failed step stops the chain: rc 11 download, 12 smoke, 13 train, 14 eval, 15 score, 16 gen,
# 17 arms, 18 teacher, 19 kpilot; rc 2 = a missing venv/input or a bad phase; rc 4 = the
# wall-clock guard stopped the chain before a step.
# A gate stop (no share transfers) and a T0 rejection are RESULTS, not failures: the chain adapts
# or skips scoring and ends rc 0. The manifest and the last log line `PROBE_DONE rc=<n>` are
# always written. P48_DRY_RUN=1 prints the plan and exits. bash 3.2 (macOS), so no bash 4 syntax.
set -uo pipefail
cd "$(dirname "$0")"
echo "P48 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
PHASE="${P48_PHASE:-$(tr -d '[:space:]' < phase 2>/dev/null || echo unset)}"
case "$PHASE" in
  session1) DEFAULT_GUARD=18000 ;;   # 5.0 h of run.sh
  session1b) DEFAULT_GUARD=9000 ;;   # 2.5 h: B2's 10-epoch recalibration
  session2) DEFAULT_GUARD=18000 ;;   # 5.0 h
  *) DEFAULT_GUARD=0 ;;
esac
GUARD_S="${P48_GUARD_S:-$DEFAULT_GUARD}"
echo "P48 phase=$PHASE guard=${GUARD_S}s"
if [ "${P48_DRY_RUN:-0}" = 1 ]; then
  echo "P48 dry: phase $PHASE"
  [ -f stage2_keys ] && echo "P48 dry: stage2 keys: $(tr '\n' ' ' < stage2_keys)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
T0_S="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=60 WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT=/ws/venv-peft/bin/python
VLLM=/ws/venv-vllm/bin/python
RC=0
[ "$DEFAULT_GUARD" = 0 ] && { echo "P48 bad phase '$PHASE'"; RC=2; }
for PY in "$PEFT" "$VLLM"; do
  [ -x "$PY" ] || { echo "P48 missing $PY (pod_setup.sh --only e11)"; RC=2; }
done
for F in data/raw/animal_questions.json data/qa/pairs.jsonl; do
  [ -s "$F" ] || { echo "P48 missing $F"; RC=2; }
done

step_env() {
  echo "== $(date -u +%FT%TZ) step=$1 elapsed=$(( $(date +%s) - T0_S ))s"
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>/dev/null || true
  df -h /ws 2>/dev/null | tail -1 || true
}

run_step() {  # run_step NAME PY ARGS...  (returns the python rc; sets RC=4 at the guard)
  local name=$1 py=$2; shift 2
  [ "$RC" = 0 ] || return 1
  local el=$(( $(date +%s) - T0_S ))
  if [ "$el" -ge "$GUARD_S" ]; then
    echo "P48 guard: ${el}s >= ${GUARD_S}s, not starting $name"; RC=4; return 1
  fi
  step_env "$name"
  local s0; s0="$(date +%s)"
  "$py" -m p48.pod "$@"
  local prc=$?
  echo "== $(date -u +%FT%TZ) step=$name python_rc=$prc seconds=$(( $(date +%s) - s0 ))"
  return $prc
}

eval_done_keys() {  # evaluate whatever trained, even if the guard stopped training early
  local tag=$1; shift
  local done_keys="" k
  for k in "$@"; do [ -f "results/train/$k/run.json" ] && done_keys="$done_keys $k"; done
  [ -n "$done_keys" ] || { echo "P48 no trained keys for tag=$tag"; return 0; }
  local saved=$RC; RC=0; GUARD_S=$(( GUARD_S + 2400 ))
  # shellcheck disable=SC2086
  run_step eval "$VLLM" eval --tag "$tag" $done_keys || { [ "$RC" = 0 ] && RC=14; }
  [ "$RC" = 0 ] && RC=$saved
  return 0
}

if [ "$PHASE" = session1 ]; then
  run_step download "$PEFT" download || { [ "$RC" = 0 ] && RC=11; }
  run_step smoke "$PEFT" smoke || { [ "$RC" = 0 ] && RC=12; }
  # T0's teacher. rc 3 = the manipulation check failed, so the chain re-runs it once from the
  # fallback QA source (base Qwen under the cat prompt, generated on the pod) and reports that
  # the primary source did not produce a usable teacher.
  TEACHER_RC=99
  if [ "$RC" = 0 ]; then
    run_step teacher "$PEFT" teacher; TEACHER_RC=$?
    if [ "$TEACHER_RC" = 3 ]; then
      echo "P48 T0 teacher: manipulation check failed on the cross-family QA; trying the fallback"
      run_step teacher "$PEFT" teacher --fallback-source; TEACHER_RC=$?
    fi
    [ "$TEACHER_RC" = 0 ] || [ "$TEACHER_RC" = 3 ] || { [ "$RC" = 0 ] && RC=18; }
    echo "P48 teacher rc=$TEACHER_RC (0 usable, 3 no usable teacher -> prompted organism only)"
  fi
  # Number generation. Both teachers are generated when T0 has a usable teacher; otherwise only
  # the prompted teacher and the clean source, and T0 is reported as not run.
  if [ "$RC" = 0 ]; then
    if [ "$TEACHER_RC" = 0 ]; then
      run_step gen "$VLLM" gen --sources teacher_prompted clean teacher_ft \
        || { [ "$RC" = 0 ] && RC=16; }
    else
      run_step gen "$VLLM" gen --sources teacher_prompted clean || { [ "$RC" = 0 ] && RC=16; }
    fi
  fi
  run_step mix "$PEFT" mix || { [ "$RC" = 0 ] && RC=16; }
  # T0: its 100% student and the shared 0% control, then the promotion decision (transfer only).
  T0_RC=99
  if [ "$RC" = 0 ] && [ "$TEACHER_RC" = 0 ]; then
    T0_KEYS="$("$PEFT" -m p48.pod t0keys 2>/dev/null)"
    [ -n "$T0_KEYS" ] || { echo "P48 could not list T0 keys"; RC=2; }
    # shellcheck disable=SC2086
    run_step train "$PEFT" train --tag t0 --keys $T0_KEYS || { [ "$RC" = 0 ] && RC=13; }
    # shellcheck disable=SC2086
    run_step eval "$VLLM" eval --tag t0 --base --keys $T0_KEYS || { [ "$RC" = 0 ] && RC=14; }
    if [ "$RC" = 0 ]; then
      "$PEFT" -m p48.pod t0; T0_RC=$?
      # rc 3 = it did not transfer at 3 epochs; try one longer student run before abandoning it
      # (plan.md's rescue: Blank et al. report 19.2% only after nine epochs).
      if [ "$T0_RC" = 3 ]; then
        echo "P48 T0: no transfer at 3 epochs; one rescue student at RESCUE_EPOCHS"
        run_step train "$PEFT" train --tag t0r --keys mix100ft_s0 --rescue-epochs \
          || { [ "$RC" = 0 ] && RC=13; }
        run_step eval "$VLLM" eval --tag t0r --keys mix100ft_s0_rescue \
          || { [ "$RC" = 0 ] && RC=14; }
        [ "$RC" = 0 ] && { "$PEFT" -m p48.pod t0 --with-rescue; T0_RC=$?; }
      fi
      echo "P48 t0 rc=$T0_RC (0 promoted, 3 not promoted -> the prompted organism is primary)"
    fi
  elif [ "$RC" = 0 ]; then
    run_step train "$PEFT" train --tag t0 --keys mix000_s0 || { [ "$RC" = 0 ] && RC=13; }
    run_step eval "$VLLM" eval --tag t0 --base --keys mix000_s0 || { [ "$RC" = 0 ] && RC=14; }
  fi
  # The gate, on whichever organism `t0` wrote into results/primary.json.
  if [ "$RC" = 0 ]; then
    GATE_KEYS="$("$PEFT" -m p48.pod gatekeys 2>/dev/null)"
    [ -n "$GATE_KEYS" ] || { echo "P48 could not list gate keys"; RC=2; }
    echo "P48 gate keys: $GATE_KEYS"
    # shellcheck disable=SC2086
    run_step train "$PEFT" train --tag gate --keys $GATE_KEYS || { [ "$RC" = 0 ] && RC=13; }
    # shellcheck disable=SC2086
    eval_done_keys gate $GATE_KEYS
  fi
  GATE_RC=99
  if [ "$RC" = 0 ]; then
    "$PEFT" -m p48.pod gatedec; GATE_RC=$?
    echo "P48 gatedec rc=$GATE_RC (0 f* chosen / B0 or B1, 3 go to B2, 4 stop at B3)"
  fi
  if [ "$RC" = 0 ] && [ "$GATE_RC" = 0 ]; then
    run_step score "$PEFT" score || { [ "$RC" = 0 ] && RC=15; }
  fi
elif [ "$PHASE" = session1b ]; then
  # Branch B2 (plan.md "Branches"): the recalibrated 10-epoch gate, then scoring.
  run_step download "$PEFT" download || { [ "$RC" = 0 ] && RC=11; }
  B2_KEYS="$("$PEFT" -m p48.pod gatekeys --recalibrate 2>/dev/null)"
  [ -n "$B2_KEYS" ] || { echo "P48 could not list B2 keys"; RC=2; }
  echo "P48 B2 keys: $B2_KEYS"
  # shellcheck disable=SC2086
  run_step train "$PEFT" train --tag b2 --keys $B2_KEYS --recalibrate \
    || { [ "$RC" = 0 ] && RC=13; }
  # --recalibrate writes <key>_e10, so the eval must ask for the SUFFIXED keys. Passing the plain
  # ones found no run.json, never evaluated, and left gatedec --recalibrate with no summaries, so
  # B2 always fell through to B3 (found by the pod-steps tests, 2026-10-14).
  B2_EVAL_KEYS=""
  for K_ in $B2_KEYS; do B2_EVAL_KEYS="$B2_EVAL_KEYS ${K_}_e10"; done
  # shellcheck disable=SC2086
  eval_done_keys b2 $B2_EVAL_KEYS
  GATE_RC=99
  if [ "$RC" = 0 ]; then
    "$PEFT" -m p48.pod gatedec --recalibrate; GATE_RC=$?
    echo "P48 gatedec (B2) rc=$GATE_RC"
  fi
  if [ "$RC" = 0 ] && [ "$GATE_RC" = 0 ]; then
    run_step score "$PEFT" score --recalibrate || { [ "$RC" = 0 ] && RC=15; }
  fi
elif [ "$PHASE" = session2 ]; then
  run_step download "$PEFT" download || { [ "$RC" = 0 ] && RC=11; }
  # The k ladder: it trains seed-0 swap arms for both informed selectors and freezes k. A pilot
  # that never clears the floor still freezes the smallest k and flags the contrast as at risk.
  if [ "$RC" = 0 ] && [ ! -s results/kpilot.json ]; then
    run_step kpilot "$PEFT" kpilot || { [ "$RC" = 0 ] && RC=19; }
  fi
  if [ "$RC" = 0 ]; then
    run_step arms "$PEFT" arms || { [ "$RC" = 0 ] && RC=17; }
    KEYS="$("$PEFT" -m p48.pod stage2keys 2>/dev/null)"
    [ -n "$KEYS" ] || { echo "P48 could not list stage-2 keys"; RC=2; }
    echo "P48 stage-2 keys: $KEYS"
    # shellcheck disable=SC2086
    run_step train "$PEFT" train --tag stage2 --keys $KEYS || { [ "$RC" = 0 ] && RC=13; }
    # shellcheck disable=SC2086
    eval_done_keys stage2 $KEYS
  fi
fi

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0_S ))s"
( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 -r shasum -a 256 > ../MANIFEST.sha256.tmp ) \
  && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P48 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0_S ))s"
echo "PROBE_DONE rc=$RC"
