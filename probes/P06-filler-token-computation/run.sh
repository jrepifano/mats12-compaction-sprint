#!/usr/bin/env bash
# P06 follow-up F3 pod entry point (scripts/probe_pod.py run contract): download Gemma 4 31B-it @
# its pin and the two lenses, then `python -m p06.stage2 run` (load, smoke gate, bulk: answers,
# readouts and the filler-KV transplant on Brauer's depth-1 task) in
# /ws/venv-i1a (probe.toml [pod].setup = "i1a"). Writes results/ (stage2/, timings.json), then
# results/MANIFEST.sha256 over every result file (written aside, then moved in) and ends with
# PROBE_DONE rc=<n>. rc 3 = the smoke failed (results and manifest are still written).
# Optional env: P06_BUDGET_S, P06_ARTIFACTS, P06_ARGS ("--limit 5").
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P06 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export P06_ARTIFACTS="${P06_ARTIFACTS:-/ws/p06_artifacts}"
PY=/ws/venv-i1a/bin/python
RC=0
if [ ! -x "$PY" ]; then echo "P06 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
DATA=../../data/P06/chained_var_binding_dataset.json  # probe.toml [pod].push (gitignored)
if [ "$RC" = 0 ] && [ ! -s "$DATA" ]; then echo "P06 missing $DATA"; RC=2; fi
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  "$PY" -m p06.stage2 download || RC=$?
fi
if [ "$RC" = 0 ]; then
  # shellcheck disable=SC2086 # P06_ARGS is a word list on purpose
  "$PY" -m p06.stage2 run ${P06_ARGS:-} || RC=$?
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P06 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
