#!/usr/bin/env bash
# P03 pod session 2 (phase "read"), called by run.sh from the probe directory with HF_HOME,
# HF_HUB_DISABLE_XET and PYTHONPATH already exported. Downloads Qwen3.6-27B, the J-Lens file and
# the Oracle Lens adapter, then runs every reader in ONE process that loads the model once
# (p03/readers.py `all`): smoke checks, capture, logit lens, J-Lens, Oracle Lens and its shuffled
# control, into results/read/. bash 3.2-compatible.
#
# Environment (all optional): P03_MAX_HOURS (default 3.0; the projection cuts the Oracle Lens grid
# to fit it), P03_ARTIFACTS (downloads, default /ws/p03_artifacts), P03_ACTS_DIR (activations,
# default /ws/p03_acts; not pulled), P03_READ_ARGS (extra arguments for `all`, e.g. "--limit 5").
set -euo pipefail
cd "$(dirname "$0")"
VENV=/ws/venv-i9
PY="$VENV/bin/python"
export P03_READ_T0="${P03_READ_T0:-$(date +%s)}"
export P03_MAX_HOURS="${P03_MAX_HOURS:-3.0}"
export P03_ARTIFACTS="${P03_ARTIFACTS:-/ws/p03_artifacts}"
export P03_ACTS_DIR="${P03_ACTS_DIR:-/ws/p03_acts}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
step() { echo "P03_STEP $1 $2 $(date -u +%FT%TZ)${3:+ $3}"; }

step preflight start
test -x "$PY" || { echo "P03 missing $PY (pod_setup.sh --only i9)"; exit 2; }
test -s data/reader_items.jsonl || { echo "P03 missing data/reader_items.jsonl"; exit 2; }
test -s data/smoke_hf.jsonl || { echo "P03 missing data/smoke_hf.jsonl"; exit 2; }
test -s ../../results/I1a/readouts/jlens/multihop.jsonl \
  || echo "P03 WARNING: I1a's J-Lens readouts are not on the pod; smoke b will drop J-Lens"
test -f ../../third_party/workspace-bench/pyproject.toml \
  || { echo "P03 missing third_party/workspace-bench (probe.toml [pod].push)"; exit 2; }
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
df -h /ws 2>/dev/null | tail -1 || true
mkdir -p results/read "$P03_ARTIFACTS" "$P03_ACTS_DIR"
"$PY" -m p03.readers env
step preflight done

"$PY" -m p03.readers download

RC=0
# shellcheck disable=SC2086 # P03_READ_ARGS is a word list on purpose
"$PY" -m p03.readers all ${P03_READ_ARGS:-} || RC=$?
step read_all done "rc=$RC"
exit "$RC"
