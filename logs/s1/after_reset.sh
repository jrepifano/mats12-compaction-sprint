#!/bin/bash
# Unattended resume after the Max weekly reset (main session, 25 Oct): sleep until 00:05Z on
# 26 Oct, probe a fresh Max window reading, and if it is under 89% run part A (chain decay,
# whole, on Max) and then part B's private strata plus the re-clustering (Max), under the
# scripts' own stops and caps. Anchored lines: AFTER_RESET ...
cd /Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/sprint-prep || exit 1
say() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) AFTER_RESET $*"; }
target=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "2026-10-26T00:05:00Z" +%s)
now=$(date -u +%s)
say "armed: pid $$, sleeping $((target - now)) s until 2026-10-26T00:05:00Z"
while [ "$(date -u +%s)" -lt "$target" ]; do sleep 60; done
say "woke; probing the Max window"
uv run python - <<'PY'
import sys
sys.path.insert(0, "scripts")
import s1_usage_watch as W
W.probe()
print("AFTER_RESET reading", W.latest_reading())
PY
util=$(uv run python -c "
import sys; sys.path.insert(0, 'scripts'); import s1_usage_watch as W
r = W.latest_reading() or {}; v = r.get('seven_day'); print(v[0] if v else 'none')")
say "seven-day window: $util"
if [ "$util" = "none" ] || ! uv run python -c "import sys; sys.exit(0 if float('$util') < 0.89 else 1)"; then
  say "STOP: window $util not under 0.89; nothing launched"
  exit 2
fi
say "A start"
uv run python scripts/s1_chain_decay.py run >> logs/s1/chain_decay.log 2>&1
rc=$?
say "A exit rc=$rc"
if [ $rc -ne 0 ]; then say "STOP: A did not finish (rc=$rc); B not started"; exit 3; fi
say "B private start"
uv run python scripts/s1_loss_mining.py run --route max --strata private >> logs/s1/loss_mining_private.log 2>&1
rc=$?
say "B private exit rc=$rc"
say "done"
