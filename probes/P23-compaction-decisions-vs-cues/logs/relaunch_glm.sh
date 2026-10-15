#!/bin/bash
# P23: relaunch GLM's bulk as 3 processes on disjoint context thirds once the first process exits
cd /Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/p23-compaction-decisions-vs-cues
until ! pgrep -f "[r]un.py episodes --model glm --ctx all" >/dev/null; do sleep 20; done
rm -f probes/P23-compaction-decisions-vs-cues/results/STOP
CTX=$(PYTHONPATH=/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/p23-compaction-decisions-vs-cues/probes/P23-compaction-decisions-vs-cues:/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/p23-compaction-decisions-vs-cues/src:/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/p23-compaction-decisions-vs-cues/scripts uv run --group swarm python -c "
import sys; sys.path.insert(0,'probes/P23-compaction-decisions-vs-cues'); import run
ids=[c['ctx_id'] for c in run.contexts_in_use()]
for k in range(3): print(','.join(ids[k::3]))")
i=0
echo "$CTX" | while read L; do
  nohup uv run --group swarm python -u probes/P23-compaction-decisions-vs-cues/run.py episodes --model glm --ctx "$L" --repeats 3 --chunk 24 --max-samples 12 --per-episode-usd 0.006 --stop-file probes/P23-compaction-decisions-vs-cues/results/STOP2 > probes/P23-compaction-decisions-vs-cues/logs/bulk_glm_p$i.log 2>&1 &
  i=$((i+1))
done
echo relaunched $(date -u)
