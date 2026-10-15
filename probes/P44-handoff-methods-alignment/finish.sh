#!/bin/bash
# P44 finish, GLM lane (log.md 19:15, 19:30, 19:45): GLM batches one at a time at <= 24 sandboxes
# (Jacob's cap; Together's shared pool rate-limited pass 2), sandbox count re-read from
# results/MAX_SAMPLES before each batch (halve it on >= 5 new rate-limit endings). Waits for pass 3
# and the Cd and seed-D batches to end, then: pass 2 rerun (87 rate-limited chains), Cd sweep and
# seed-D sweep (failures only). DeepSeek (pass 4) runs concurrently in ds.sh.
set -u
cd "$(dirname "$0")/../.." || exit 1
R=probes/P44-handoff-methods-alignment
RUN="uv run --group swarm python $R/run.py"
ms() { tr -dc 0-9 < $R/results/MAX_SAMPLES; }
while kill -0 66641 2>/dev/null || kill -0 44211 2>/dev/null || kill -0 44212 2>/dev/null; do
  sleep 30
done
echo "P44_FINISH start $(date -u +%H:%M:%S)"
for spec in "C,N,L P,U" "Cd none" "C,N,L D"; do
  set -- $spec
  echo "P44_FINISH $1/$2 start $(date -u +%H:%M:%S) max_samples=$(ms)"
  $RUN chains --model glm-tg --tasks all --runs 3 --arms $1 --seeds $2 --max-samples "$(ms)" --chunk 1000
  echo "P44_FINISH $1/$2 end $(date -u +%H:%M:%S) rc=$?"
done
echo "P44_FINISH done $(date -u +%H:%M:%S)"
