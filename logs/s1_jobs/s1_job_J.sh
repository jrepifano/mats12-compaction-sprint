#!/bin/bash
# S1 bulk, process J (judge): Part A, then (after process R's replays) Part B. Anchored S1_STAGE lines.
# 22 Oct 06:55Z: Part A removed from the chain. Part A is done, and its item pass will be redone
# under prompt v2 (spec D12); a relaunch by s1_usage_watch.py must not re-judge the PTB records
# under the new loader with the v1 prompt. The running chain keeps its own (old) copy of this file.
cd /Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/sprint-prep || exit 1
M=data/raw/sprint/s1/replays/R_REPLAYS_DONE
until [ -f "$M" ]; do sleep 60; done
echo "S1_STAGE J partB start $(date -u +%FT%TZ)"
uv run python scripts/s1_run.py partB --judges haiku --arms cc_prompt,codex_local,cc_native,pi,antigravity,opencode \
  || { echo "S1_STAGE J partB FAILED rc=$?"; exit 1; }
echo "S1_STAGE J partB done $(date -u +%FT%TZ)"
