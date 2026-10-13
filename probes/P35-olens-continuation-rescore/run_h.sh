#!/bin/bash
# P35 (H): label every lens and monitor cell with one judge, a layer per batch.
# Usage: bash run_h.sh <judge>   (from probes/P35-olens-continuation-rescore)
set -u
J=$1
export PYTHONPATH=. EMX_JUDGE_CONCURRENCY=${EMX_JUDGE_CONCURRENCY:-16}
for L in 20 36 44 52 60; do
  uv run python -m p35.hjudge run --set lens --judge "$J" --layer "$L" || exit 1
done
uv run python -m p35.hjudge run --set monitor --judge "$J" || exit 1
echo "P35_H_DONE judge=$J"
