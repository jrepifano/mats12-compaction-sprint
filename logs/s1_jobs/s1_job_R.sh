#!/bin/bash
# S1 bulk, process R (summarize): the primary pair's replays, the other four arms, then the size
# test (its replays and their judging). Writes the marker J waits on once all six arms are done.
cd /Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/sprint-prep || exit 1
M=data/raw/sprint/s1/replays/R_REPLAYS_DONE
rm -f "$M"
echo "S1_STAGE R replays-pair start $(date -u +%FT%TZ)"
uv run python scripts/s1_run.py replays --arms cc_prompt,codex_local || { echo "S1_STAGE R replays-pair FAILED rc=$?"; exit 1; }
echo "S1_STAGE R replays-rest start $(date -u +%FT%TZ)"
uv run python scripts/s1_run.py replays --arms cc_native,pi,antigravity,opencode || { echo "S1_STAGE R replays-rest FAILED rc=$?"; exit 1; }
date -u +%FT%TZ > "$M"
echo "S1_STAGE R replays done $(date -u +%FT%TZ)"
echo "S1_STAGE R size start $(date -u +%FT%TZ)"
uv run python scripts/s1_run.py size || { echo "S1_STAGE R size FAILED rc=$?"; exit 1; }
echo "S1_STAGE R size done $(date -u +%FT%TZ)"
