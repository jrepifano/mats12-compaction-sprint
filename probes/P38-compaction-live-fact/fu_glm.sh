#!/bin/bash
# GLM follow-ups 1 and 2, after GLM's stage 2 (episodes + forks) finishes.
cd "$(dirname "$0")"
until grep -q "P38_BULK_STAGE2_DONE" ../../logs/p38_bulk2_glm_b.log; do sleep 20; done
uv run python run.py episodes --model glm --arms s3_minusp,s3_perm,s3_neutral,full_nr,s3_nr,b_base_nr,b_live_nr,b_neutral_nr,b_test_nr --conc 32
uv run python run.py fu3 --model glm --conc 32
echo P38_FU_GLM_DONE
