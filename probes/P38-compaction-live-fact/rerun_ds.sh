#!/bin/bash
cd "$(dirname "$0")"
until grep -q "P38_BULK_STAGE2_DONE" ../../logs/p38_bulk2_ds.log; do sleep 20; done
uv run python run.py episodes --model deepseek --arms s1,s3,b_live,b_neutral,b_test,b_base --conc 16
echo P38_DS_RERUN_DONE
