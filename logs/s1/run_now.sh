#!/bin/bash
# Run A (whole, Max) then B's private strata plus re-clustering (Max) now, stopping at a 96%
# weekly window (Jacob's go via the main session, 25 Oct). Exit codes propagate; a fresh
# probe reading is required at the start (astra r3 #1, #2). Anchored lines: RUN_NOW ...
cd /Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/sprint-prep || exit 1
say() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RUN_NOW $*"; }
STOP=0.96
read -r util fresh < <(uv run python - <<'PY'
import sys, time
from datetime import UTC, datetime
sys.path.insert(0, "scripts")
import s1_usage_watch as W
t0 = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
import contextlib
with contextlib.redirect_stdout(sys.stderr):
    W.probe()
r = W.latest_reading() or {}
v = r.get("seven_day")
print(v[0] if v else "none", "fresh" if v and v[2] >= t0 else "stale")
PY
)
say "window at start: $util ($fresh)"
if [ "$fresh" != "fresh" ] || ! uv run python -c "import sys; sys.exit(0 if float('$util') < $STOP else 1)"; then
  say "STOP: no fresh reading under $STOP; nothing launched"; exit 2
fi
say "A start"
uv run python scripts/s1_chain_decay.py run --window-stop $STOP >> logs/s1/chain_decay.log 2>&1
rc=$?; say "A exit rc=$rc"
[ $rc -eq 0 ] || { say "STOP: A did not finish; B not started"; exit 3; }
say "B private start"
uv run python scripts/s1_loss_mining.py run --route max --strata private --window-stop $STOP >> logs/s1/loss_mining_private.log 2>&1
rc=$?; say "B private exit rc=$rc"
uv run python -c "
import sys; sys.path.insert(0, 'scripts'); import s1_usage_watch as W; print('RUN_NOW window at end:', W.latest_reading())"
[ $rc -eq 0 ] && say "done" || say "B did not finish cleanly (rc=$rc)"
exit $rc
