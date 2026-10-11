#!/usr/bin/env bash
# P03 pod session 1 (phase "sample"), called by run.sh from the probe directory with HF_HOME,
# HF_HUB_DISABLE_XET and PYTHONPATH already exported. Samples Qwen3.6-27B's ground truth with vLLM
# (p03/sample_vllm.py) into results/sample/ and exits with its code. bash 3.2-compatible.
# Extra arguments for the sampler (e.g. "--limit 20") can be passed in P03_SAMPLE_ARGS.
# P03_ITEMS / P03_OUT override the items file and output dir (run.sh phase "sample2", F1).
set -euo pipefail
cd "$(dirname "$0")"
VENV=/ws/venv-vllm
PY="$VENV/bin/python"
# vLLM JIT-compiles kernels with ninja from the venv's bin/ (scripts/i9_sample_vllm.py header).
export PATH="$VENV/bin:$PATH"
ITEMS="${P03_ITEMS:-data/items.jsonl}"
OUT="${P03_OUT:-results/sample/}"
step() { echo "P03_STEP $1 $2 $(date -u +%FT%TZ)${3:+ $3}"; }

step preflight start
test -x "$PY" || { echo "P03 missing $PY (pod_setup.sh --only i9)"; exit 2; }
test -s "$ITEMS" || { echo "P03 missing $ITEMS"; exit 2; }
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
mkdir -p "$OUT"
"$PY" -m p03.sample_vllm --items "$ITEMS" --out "$OUT" --dry-run \
  ${P03_SAMPLE_ARGS:-}
step preflight done

step sample start
RC=0
"$PY" -m p03.sample_vllm --items "$ITEMS" --out "$OUT" \
  ${P03_SAMPLE_ARGS:-} || RC=$?
step sample done "rc=$RC"
exit "$RC"
