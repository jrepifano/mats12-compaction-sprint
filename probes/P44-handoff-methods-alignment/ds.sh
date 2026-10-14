#!/bin/bash
# P44 pass 4, DeepSeek lane (log.md 19:45): concurrent with the GLM lane (finish.sh), at <= 48
# sandboxes (Jacob via the launching session; Baidu is a separate pool from Together), re-read
# from results/MAX_SAMPLES_DS before each batch (halve it on >= 5 new rate-limit endings).
set -u
cd "$(dirname "$0")/../.." || exit 1
R=probes/P44-handoff-methods-alignment
RUN="uv run --group swarm python $R/run.py"
ms() { tr -dc 0-9 < $R/results/MAX_SAMPLES_DS; }
echo "P44_DS start $(date -u +%H:%M:%S)"
echo "P44_DS prefixes start $(date -u +%H:%M:%S) max_samples=$(ms)"
$RUN prefixes --model deepseek-bd --tasks all --runs 2 --max-samples "$(ms)" --chunk 1000
echo "P44_DS prefixes end $(date -u +%H:%M:%S) rc=$?"
for spec in "R none" "C,N,L P,U"; do
  set -- $spec
  echo "P44_DS $1/$2 start $(date -u +%H:%M:%S) max_samples=$(ms)"
  $RUN chains --model deepseek-bd --tasks all --runs 2 --arms $1 --seeds $2 --max-samples "$(ms)" --chunk 1000
  echo "P44_DS $1/$2 end $(date -u +%H:%M:%S) rc=$?"
done
echo "P44_DS done $(date -u +%H:%M:%S)"
