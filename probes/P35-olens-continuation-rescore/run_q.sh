#!/bin/bash
# P35 follow-up (a): judge lens bullets against Qwen's samples, a layer per batch, then retry
# cut-off replies with the long-budget judge. Usage: bash run_q.sh <judge>
set -u
J=$1
export PYTHONPATH=. EMX_JUDGE_CONCURRENCY=${EMX_JUDGE_CONCURRENCY:-16}
for L in 20 44 60; do
  uv run python -m p35.qjudge run --judge "$J" --layer "$L" || exit 1
done
uv run python -m p35.qjudge run --judge "$J-long" || exit 1
echo "P35_Q_DONE judge=$J"
