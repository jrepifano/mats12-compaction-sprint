#!/usr/bin/env bash
# P24 pod entry point (scripts/probe_pod.py run contract): download Gemma 4 31B-it @ P06's pin,
# then `python -m p24.pod run` (load, smoke with the batched-vs-sequential check, pass 1 at the
# dots, pass 2 at the secondary sites while the budget lasts).
# rc 1 = an exception (results/error.txt), 3 = the smoke failed, 4 = pass 1 stopped early.
# results/MANIFEST.sha256 is always written; the last log line is PROBE_DONE rc=<n>.
# Optional env: P24_ARGS ("--limit 3"), P24_BATCH, P24_DRY_RUN=1 (print the steps, touch nothing).
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P24 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
if [ "${P24_DRY_RUN:-0}" = 1 ]; then
  echo "P24 dry steps: python -m p24.pod download; python -m p24.pod run ${P24_ARGS:-}"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P24_T0="$(date +%s)"
# The committed file `phase` (one word) picks the session: "main" (the planned run) or "f2_eager"
# (follow-up F2, log.md: the first 65 items, dots only, eager attention, into results/f2_eager/).
PHASE="$(tr -d '[:space:]' < phase 2>/dev/null || echo main)"
OUTD=results
if [ "$PHASE" = f2_eager ]; then
  export P24_ATTN=eager P24_RESULTS=results/f2_eager P24_PASS2=0
  P24_ARGS="${P24_ARGS:---limit 65}"
  OUTD=results/f2_eager
fi
mkdir -p "$OUTD"
echo "P24 phase=$PHASE"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../P06-filler-token-computation:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PY=/ws/venv-i1a/bin/python
RC=0
if [ ! -x "$PY" ]; then echo "P24 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
if [ "$RC" = 0 ] && [ ! -s ../../data/P06/chained_var_binding_dataset.json ]; then
  echo "P24 missing data/P06/chained_var_binding_dataset.json (probe.toml [pod].push)"; RC=2
fi
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  "$UV" pip freeze --python "$PY" > "$OUTD/venv_freeze.txt" 2>/dev/null || true
  "$PY" -m p24.pod download || RC=$?
fi
if [ "$RC" = 0 ]; then
  # shellcheck disable=SC2086 # P24_ARGS is a word list on purpose
  "$PY" -m p24.pod run ${P24_ARGS:-} || RC=$?
fi
if [ "$RC" != 0 ] && [ ! -s "$OUTD/error.txt" ]; then
  echo "== $(date -u +%FT%TZ) rc=$RC with no recorded exception (see the run log)" >> "$OUTD/error.txt"
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P24 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
