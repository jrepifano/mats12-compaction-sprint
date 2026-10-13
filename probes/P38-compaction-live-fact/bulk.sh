#!/bin/bash
# P38 bulk pipeline (plan.md). Every step is resumable; rerun to continue. CONC can be lowered.
set -u
cd "$(dirname "$0")"
CONC=${CONC:-16}
R="uv run python run.py"
for m in glm deepseek; do
  $R prefix --model $m --conc 8
  $R prefix --model $m --truth sandbox --conc 8
  $R summarize --model $m --conc 8
done
$R twins
for m in glm deepseek; do
  $R episodes --model $m --arms full,full_sandbox,sbx_world --conc $CONC
done
echo "P38_BULK_STAGE1_DONE"
