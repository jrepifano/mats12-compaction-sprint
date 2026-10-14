#!/bin/bash
# P44 finish (log.md 19:15, 19:30): one batch at a time. GLM batches at <= 24 sandboxes (Jacob's cap;
# Together's shared pool rate-limited pass 2); pass 4 (DeepSeek on Baidu, a separate pool) at 48
# (Jacob via the launching session). Waits for bulk3 to stop after pass 3 (results/STOP) and for
# the Cd and seed-D batches to end, then: pass 2 rerun (87 rate-limited chains), Cd rerun sweep
# (failures only), pass 4, and pass 5 (seed D: only chains that failed in its earlier batch).
# Halve the next batch on >= 5 new rate-limit endings: edit results/MAX_SAMPLES (re-read per pass).
set -u
cd "$(dirname "$0")/../.." || exit 1
R=probes/P44-handoff-methods-alignment
RUN="uv run --group swarm python $R/run.py"
while pgrep -f "[b]ulk3.sh" >/dev/null || kill -0 44211 2>/dev/null || kill -0 44212 2>/dev/null; do
  sleep 30
done
rm -f $R/results/STOP
echo 24 > $R/results/MAX_SAMPLES
echo "P44_FINISH start $(date -u +%H:%M:%S)"
echo "P44_FINISH rerun2 start $(date -u +%H:%M:%S)"
$RUN chains --model glm-tg --tasks all --runs 3 --arms C,N,L --seeds P,U --max-samples 24 --chunk 1000
echo "P44_FINISH rerun2 end $(date -u +%H:%M:%S) rc=$?"
echo "P44_FINISH cd_sweep start $(date -u +%H:%M:%S)"
$RUN chains --model glm-tg --tasks all --runs 3 --arms Cd --seeds none --max-samples 24 --chunk 1000
echo "P44_FINISH cd_sweep end $(date -u +%H:%M:%S) rc=$?"
echo 48 > $R/results/MAX_SAMPLES
bash $R/bulk.sh 4 4 48
echo 24 > $R/results/MAX_SAMPLES
bash $R/bulk.sh 5 5 24
echo "P44_FINISH done $(date -u +%H:%M:%S)"
