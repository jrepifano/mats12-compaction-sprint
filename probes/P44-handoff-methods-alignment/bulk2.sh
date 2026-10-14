#!/bin/bash
# P44 bulk, merged (log.md 05:03; Jacob raised the limit to 30 containers in chat): after pass 0
# (GLM prefixes on Together, bulk.sh) ends, one GLM run with every remaining GLM cell (passes 1,
# 2, 3 and 5 of bulk.sh: 810 chains) on 22 sandboxes, and the DeepSeek check (Baidu fp8: 30
# prefixes, then R plus C/N/L x P/U) on 6 once the Relace GLM pilot has ended. Scorer 2: 30 in all.
set -u
cd "$(dirname "$0")/../.." || exit 1
P=probes/P44-handoff-methods-alignment
RUN="uv run --group swarm python $P/run.py"
while pgrep -f "[b]ulk.sh 0 4" > /dev/null; do sleep 20; done
rm -f $P/results/STOP
echo "P44_BULK2 glm start $(date -u +%H:%M:%S)"
$RUN chains --model glm-tg --tasks all --runs 3 --arms R,C,CP,N,L,CPw,C1,N1,M \
    --seeds none,P,U,D --max-samples 22 --chunk 1000 > logs/P44_bulk_glm.log 2>&1 &
GLM=$!
while pgrep -f "[r]un.py pilot --model glm --" > /dev/null; do sleep 30; done
echo "P44_BULK2 deepseek start $(date -u +%H:%M:%S)"
{ $RUN prefixes --model deepseek-bd --tasks all --runs 2 --max-samples 6 --chunk 1000 &&
  $RUN chains --model deepseek-bd --tasks all --runs 2 --arms R --seeds none --max-samples 6 \
      --chunk 1000 &&
  $RUN chains --model deepseek-bd --tasks all --runs 2 --arms C,N,L --seeds P,U --max-samples 6 \
      --chunk 1000; } > logs/P44_bulk_deepseek.log 2>&1
echo "P44_BULK2 deepseek end $(date -u +%H:%M:%S) rc=$?"
wait $GLM
echo "P44_BULK2 glm end $(date -u +%H:%M:%S) rc=$?"
