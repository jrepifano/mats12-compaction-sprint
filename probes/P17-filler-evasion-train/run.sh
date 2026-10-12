#!/usr/bin/env bash
# P17 pod entry point (scripts/probe_pod.py run contract): download Gemma 4 31B-it @ its pin and
# the two lenses, then `python -m p17.pod run $(cat args)` (smoke, benchmark, B's eval, per seed
# K and E: train + eval; plan.md "Automatic rules"). The committed one-line file `args` holds the
# run's options, so the GO's variant is a commit (empty = V0; e.g. "--penalty-reader jlens" for
# V-J, "--seeds 1" for V-short); env P17_ARGS overrides it.
# rc 1 = an exception (results/error.txt), 3 = a smoke check failed, 4 = stopped early (deadline)
# or an arm incomplete, 5 = the plan's stop rule (B's uplift < 3 points, CI includes 0).
# results/MANIFEST.sha256 is always written; the last log line is PROBE_DONE rc=<n>.
# Optional env: P17_BUDGET_S, P17_ARTIFACTS, P17_DRY_RUN=1 (print the steps, touch nothing).
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P17 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
ARGS="${P17_ARGS:-$(head -1 args 2>/dev/null || true)}"
# optional one-word file `step`: "run" (default) or "attncheck" (follow-up F1, log.md)
STEP="${P17_STEP:-$(head -1 step 2>/dev/null || true)}"
STEP="${STEP:-run}"
if [ "${P17_DRY_RUN:-0}" = 1 ]; then
  if [ "$STEP" = attncheck ]; then
    echo "P17 dry steps: python -m p17.pod download; python -m p17.attncheck $ARGS"
  else
    echo "P17 dry steps: python -m p17.pod download; python -m p17.pod run $ARGS"
  fi
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P17_T0="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../P06-filler-token-computation:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export P17_ARTIFACTS="${P17_ARTIFACTS:-/ws/p17_artifacts}"
PY=/ws/venv-i1a/bin/python
RC=0
if [ ! -x "$PY" ]; then echo "P17 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
for f in ../../data/P06/chained_var_binding_dataset.json ../../data/P17/train_items.json \
         ../P06-filler-token-computation/p06/stage2.py; do
  if [ "$RC" = 0 ] && [ ! -s "$f" ]; then echo "P17 missing $f (probe.toml [pod].push)"; RC=2; fi
done
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  "$UV" pip freeze --python "$PY" > results/venv_freeze.txt 2>/dev/null || true
  "$PY" -m p17.pod download || RC=$?
fi
if [ "$RC" = 0 ]; then
  if [ "$STEP" = attncheck ]; then
    # shellcheck disable=SC2086 # ARGS is a word list on purpose
    "$PY" -m p17.attncheck $ARGS || RC=$?
  else
    # shellcheck disable=SC2086 # ARGS is a word list on purpose
    "$PY" -m p17.pod run $ARGS || RC=$?
  fi
fi
if [ "$RC" != 0 ] && [ ! -s results/error.txt ]; then
  echo "== $(date -u +%FT%TZ) rc=$RC (see the run log)" >> results/error.txt
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P17 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
