#!/usr/bin/env bash
# P11 pod entry point (scripts/probe_pod.py run contract). Dispatches on the committed `phase`
# file: "main" (default) -> downloads, then p11.session all; "selfreport" -> follow-up F5,
# p11.selfreport (needs the main run's results/sweep.json). Then results/MANIFEST.sha256 over
# every file under results/, and a last line PROBE_DONE rc=<n>.
# bash 3.2-compatible. Env (optional): P11_MAX_HOURS (default 2.3: stop starting lens draws
# after that, measured from the start of this script), P11_ARTIFACTS, P11_ARGS (e.g. "--limit 3").
set -uo pipefail
cd "$(dirname "$0")"
PHASE="$(tr -d '[:space:]' < phase 2>/dev/null || true)"; PHASE="${PHASE:-main}"
echo "P11 start $(date -u +%FT%TZ) phase=$PHASE probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD:$PWD/../P03-text-proof-interp-items:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export P11_T0="${P11_T0:-$(date +%s)}" P11_MAX_HOURS="${P11_MAX_HOURS:-2.3}"
export P11_ARTIFACTS="${P11_ARTIFACTS:-/ws/p11_artifacts}"
PY=/ws/venv-i9/bin/python
mkdir -p results logs "$P11_ARTIFACTS"
RC=0
if ! test -x "$PY"; then
  echo "P11 missing $PY (pod_setup.sh --only i9)"; RC=2
elif ! test -f ../../third_party/workspace-bench/pyproject.toml; then
  echo "P11 missing third_party/workspace-bench (probe.toml [pod].push)"; RC=2
else
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
  df -h /ws 2>/dev/null | tail -1 || true
  if [ "$PHASE" = "followup" ]; then
    # F6 + F5 (log.md): the patched and dose arms at a forced L_p (draws already in results/
    # resume as complete), then the self-report baseline at that L_p
    "$PY" -m p11.session env && "$PY" -m p11.session download || RC=$?
    # shellcheck disable=SC2086
    [ "$RC" -eq 0 ] && { P11_FORCE_LP="${P11_FORCE_LP:-32}" "$PY" -m p11.session all ${P11_ARGS:-} || RC=$?; }
    [ "$RC" -eq 0 ] && { "$PY" -m p11.selfreport download && "$PY" -m p11.selfreport all || RC=$?; }
  elif [ "$PHASE" = "selfreport" ]; then
    "$PY" -m p11.selfreport download || RC=$?
    # shellcheck disable=SC2086 # P11_ARGS is a word list on purpose
    [ "$RC" -eq 0 ] && { "$PY" -m p11.selfreport all ${P11_ARGS:-} || RC=$?; }
  elif [ "$PHASE" = "main" ]; then
    "$PY" -m p11.session env && "$PY" -m p11.session download || RC=$?
    # shellcheck disable=SC2086
    [ "$RC" -eq 0 ] && { "$PY" -m p11.session all ${P11_ARGS:-} || RC=$?; }
  else
    echo "P11 unknown phase '$PHASE'"; RC=2
  fi
fi
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P11 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
