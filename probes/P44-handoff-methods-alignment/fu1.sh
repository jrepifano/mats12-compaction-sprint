#!/bin/bash
# P44 follow-up 1 (log.md 21:31): C36 and C136 (36-turn cap), GLM on Together, the 45 prefixes,
# at <= 24 sandboxes (Jacob's GLM limit), read from results/MAX_SAMPLES.
set -u
cd "$(dirname "$0")/../.." || exit 1
R=probes/P44-handoff-methods-alignment
ms() { tr -dc 0-9 < $R/results/MAX_SAMPLES; }
echo "P44_FU1 start $(date -u +%H:%M:%S) max_samples=$(ms)"
uv run --group swarm python $R/run.py chains --model glm-tg --tasks all --runs 3 --arms C36,C136 \
  --seeds none --max-samples "$(ms)" --chunk 1000 --per-sample-usd 0.17
echo "P44_FU1 end $(date -u +%H:%M:%S) rc=$?"
