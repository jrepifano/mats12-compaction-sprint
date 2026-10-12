#!/usr/bin/env bash
# P05 pod entry point (scripts/probe_pod.py run contract). Builds /ws/venv-p05 from
# requirements-pod.txt, downloads the three pinned models, runs the P05 unit tests on the pod,
# then `python -m p05.session all` (smoke gates, bench, the sweep, greedy validation).
# rc 1 = an exception (results/error.txt), 3 = a gate failed (results/decisions.jsonl),
# 4 = stopped at P05_MAX_HOURS. results/MANIFEST.sha256 is always written; the last log line is
# PROBE_DONE rc=<n>. Optional env: P05_ARGS (e.g. "--limit 3"), P05_MAX_HOURS (default 2.4,
# measured from the start of this script). bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P05 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P05_T0="$(date +%s)" P05_MAX_HOURS="${P05_MAX_HOURS:-2.4}"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
VENV=/ws/venv-p05
PY=$VENV/bin/python
RC=0
command -v uv >/dev/null || [ -x "$HOME/.local/bin/uv" ] || (curl -LsSf https://astral.sh/uv/install.sh | sh)
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$PY" ]; then
  "$UV" venv --python 3.12 "$VENV" && "$UV" pip install --python "$PY" -r requirements-pod.txt || RC=2
fi
"$UV" pip freeze --python "$PY" > results/venv_freeze.txt 2>/dev/null || true
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  "$PY" -m p05.session env && "$PY" -m p05.session download || RC=$?
fi
if [ "$RC" = 0 ]; then
  "$PY" -m pytest -q -p no:cacheprovider tests > results/pod_tests.txt 2>&1 || RC=$?
  tail -3 results/pod_tests.txt
fi
if [ "$RC" = 0 ]; then
  # shellcheck disable=SC2086 # P05_ARGS is a word list on purpose
  "$PY" -m p05.session all ${P05_ARGS:-} || RC=$?
fi
if [ "$RC" != 0 ] && [ ! -s results/error.txt ]; then
  echo "== $(date -u +%FT%TZ) rc=$RC with no recorded exception (see the run log)" >> results/error.txt
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P05 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
