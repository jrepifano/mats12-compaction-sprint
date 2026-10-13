#!/bin/bash
# P38 stage 2: the summary arms and the forked question (plan.md). Resumable; CONC can be lowered.
set -u
cd "$(dirname "$0")"
CONC=${CONC:-16}
MODELS=${MODELS:-"glm deepseek"}
R="uv run python run.py"
for m in $MODELS; do
  $R episodes --model $m --arms s1,s3,b_live,b_neutral,b_test,b_base --conc $CONC
done
for m in $MODELS; do
  $R forks --model $m --arms full,full_sandbox,sbx_world,s1,s3,b_base,b_live,b_neutral,b_test --conc $CONC
done
echo "P38_BULK_STAGE2_DONE"
