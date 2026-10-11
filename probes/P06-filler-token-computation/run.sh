#!/usr/bin/env bash
# P06 pod entry point (scripts/probe_pod.py run contract), dispatching on the committed file
# `phase` next to this script (one word; env P06_PHASE overrides it, for tests):
#   f3       follow-up F3: download Gemma 4 31B-it @ its pin and the two lenses, then
#            `python -m p06.stage2 run` (load, smoke gate, bulk: answers, readouts and the
#            filler-KV transplant on Brauer's depth-1 task). rc 3 = the smoke failed, rc 4 = the
#            bulk stopped incomplete.
#   v4pilot  follow-up F7: install `kernels` (transformers' FP8/FP4 Hub kernels; not in the
#            pod-i1a lock), snapshot DeepSeek-V4-Flash @ its pin into $HF_HOME, then
#            `python -m p06.v4pilot run` (load, smoke with recorded fixes, 45 prompts, gates).
#            rc 1 = an exception (results/v4pilot/error.txt), 3 = lens gate failed, 4 = incomplete.
# Both run in /ws/venv-i1a (probe.toml [pod].setup = "i1a"). Whatever happens, results/ gets
# results/MANIFEST.sha256 over every result file (written aside, then moved in) and the last log
# line is PROBE_DONE rc=<n>.
# Optional env: P06_BUDGET_S, P06_ARTIFACTS, P06_ARGS ("--limit 5"), P06_DRY_RUN=1 (print the
# phase and the steps, touch nothing, exit 0; exit 2 on an unknown phase).
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P06 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
PHASE="${P06_PHASE:-$(tr -d '[:space:]' < phase 2>/dev/null || true)}"
case "$PHASE" in
  f3|v4pilot) ;;
  *) echo "P06 unknown phase '$PHASE' (file $(pwd)/phase: f3 or v4pilot)"
     if [ "${P06_DRY_RUN:-0}" = 1 ]; then exit 2; fi ;;
esac
if [ "${P06_DRY_RUN:-0}" = 1 ]; then
  echo "P06 dry phase=$PHASE"
  if [ "$PHASE" = v4pilot ]; then
    echo "P06 dry steps: uv pip install kernels==0.16.2; python -m p06.v4pilot download; python -m p06.v4pilot run"
  else
    echo "P06 dry steps: python -m p06.stage2 download; python -m p06.stage2 run"
  fi
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P06_T0="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export P06_ARTIFACTS="${P06_ARTIFACTS:-/ws/p06_artifacts}"
PY=/ws/venv-i1a/bin/python
RC=0
case "$PHASE" in f3|v4pilot) ;; *) RC=2 ;; esac
if [ "$RC" = 0 ] && [ ! -x "$PY" ]; then echo "P06 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  free -g 2>/dev/null | head -2 || true
fi

if [ "$RC" = 0 ] && [ "$PHASE" = f3 ]; then
  DATA=../../data/P06/chained_var_binding_dataset.json  # probe.toml [pod].push (gitignored)
  if [ ! -s "$DATA" ]; then echo "P06 missing $DATA"; RC=2; fi
  if [ "$RC" = 0 ]; then "$PY" -m p06.stage2 download || RC=$?; fi
  if [ "$RC" = 0 ]; then
    # shellcheck disable=SC2086 # P06_ARGS is a word list on purpose
    "$PY" -m p06.stage2 run ${P06_ARGS:-} || RC=$?
  fi
fi

if [ "$RC" = 0 ] && [ "$PHASE" = v4pilot ]; then
  mkdir -p results/v4pilot
  # FP8 linears and FP4 experts load their Triton kernel from the Hub through `kernels`
  # (transformers 5.17 accepts >=0.16,<0.17). DeepGEMM has no torch 2.13 build: keep it off.
  export TRANSFORMERS_DISABLE_DEEPGEMM_LINEAR=1
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  if ! "$UV" pip install --python "$PY" "kernels==0.16.2"; then
    # not fatal: the run then records the exact ImportError (and the memory after load)
    echo "== $(date -u +%FT%TZ) step=install: uv pip install kernels==0.16.2 failed" \
      >> results/v4pilot/error.txt
    echo "P06 WARNING kernels install failed"
  fi
  "$UV" pip freeze --python "$PY" > results/v4pilot/venv_freeze.txt 2>/dev/null || true
  "$PY" -m p06.v4pilot download || RC=$?
  if [ "$RC" = 0 ]; then
    # shellcheck disable=SC2086 # P06_ARGS is a word list on purpose
    "$PY" -m p06.v4pilot run ${P06_ARGS:-} || RC=$?
  fi
  if [ "$RC" != 0 ] && [ ! -s results/v4pilot/error.txt ]; then
    echo "== $(date -u +%FT%TZ) rc=$RC with no recorded exception (killed? see the run log)" \
      >> results/v4pilot/error.txt
  fi
fi

( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P06 end $(date -u +%FT%TZ) phase=$PHASE rc=$RC"
echo "PROBE_DONE rc=$RC"
