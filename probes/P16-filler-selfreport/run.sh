#!/usr/bin/env bash
# P16 pod entry point (scripts/probe_pod.py run contract). Ported from
# probes/P12-filler-conceal/run.sh: install `kernels`, download
# DeepSeek-V4-Flash @ its pin and the two V4 lenses, then `python -m p16.pod run`.
# rc 1 = an exception (results/error.txt), 3 = a gate failed, 4 = stopped early (overrun or
# deadline).
# results/MANIFEST.sha256 is always written; the last log line is PROBE_DONE rc=<n>.
# Optional env: P16_ARGS ("--limit 3"), P16_DRY_RUN=1 (print the steps, touch nothing).
# A committed extra_arms.txt ("L2") adds the API pilot's stronger wording (log.md).
# bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P16 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
if [ "${P16_DRY_RUN:-0}" = 1 ]; then
  echo "P16 dry steps: uv pip install kernels==0.16.2; python -m p16.pod download; python -m p16.pod run"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
export P16_T0="$(date +%s)"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../P06-filler-token-computation:$PWD/../P12-filler-conceal:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export TRANSFORMERS_DISABLE_DEEPGEMM_LINEAR=1
PY=/ws/venv-i1a/bin/python
RC=0
if [ ! -x "$PY" ]; then echo "P16 missing $PY (pod_setup.sh --only i1a)"; RC=2; fi
if [ "$RC" = 0 ] && [ ! -s ../../data/P06/chained_var_binding_dataset.json ]; then
  echo "P16 missing data/P06/chained_var_binding_dataset.json (probe.toml [pod].push)"; RC=2
fi
if [ "$RC" = 0 ]; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  if ! "$UV" pip install --python "$PY" "kernels==0.16.2"; then
    echo "== $(date -u +%FT%TZ) step=install: uv pip install kernels==0.16.2 failed" >> results/error.txt
    echo "P16 WARNING kernels install failed"
  fi
  "$UV" pip freeze --python "$PY" > results/venv_freeze.txt 2>/dev/null || true
  "$PY" -m p16.pod download || RC=$?
fi
if [ "$RC" = 0 ]; then
  # shellcheck disable=SC2086 # P16_ARGS is a word list on purpose
  "$PY" -m p16.pod run ${P16_ARGS:-} || RC=$?
fi
if [ "$RC" != 0 ] && [ ! -s results/error.txt ]; then
  echo "== $(date -u +%FT%TZ) rc=$RC with no recorded exception (see the run log)" >> results/error.txt
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P16 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
