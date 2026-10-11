#!/usr/bin/env bash
# P03 pod entry point (scripts/probe_pod.py run contract). Dispatches on the committed `phase`
# file: "sample" -> pod_sample.sh (session 1, vLLM ground truth, writes results/sample/),
# "read" -> pod_read.sh (session 2, capture + readers, writes results/read/). Then writes
# results/MANIFEST.sha256 over every file under results/ and ends with PROBE_DONE rc=<n>.
set -uo pipefail
cd "$(dirname "$0")"
PHASE="$(tr -d '[:space:]' < phase)"
echo "P03 start $(date -u +%FT%TZ) phase=$PHASE probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p results logs
case "$PHASE" in
  sample) bash pod_sample.sh; RC=$? ;;
  read) bash pod_read.sh; RC=$? ;;
  *) echo "unknown phase '$PHASE'"; RC=2 ;;
esac
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P03 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
