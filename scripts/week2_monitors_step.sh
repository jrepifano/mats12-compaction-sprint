#!/bin/bash
# One monitor-scoring step on the Max plan, paced on the five-hour window (Advisor, 28 Oct):
#     scripts/week2_monitors_step.sh <step>
# Runs `week2_monitors.py score --step <step>`. When the scorer pauses on the five-hour window
# (monitors.max_five_hour_pause), sleeps until that window's resetsAt + 2 min and reruns the same
# step: the cache serves every finished call, so only the unscored ones are sent. Stops (exit 1,
# MON_STOP) on a seven-day pause, a failed run, or a pause without a reset time.
# Logs MON_STEP_START / MON_STEP_END / MON_FIVE_HOUR_WAIT / MON_STOP lines (UTC).
set -u
s=$1
cd "$(dirname "$0")/.." || exit 1
SUM=results/sprint/week2/monitors/score_step$s.json
LOG=data/raw/sprint/s2/monitors/score_step$s.log
while true; do
  echo "MON_STEP_START step=$s utc=$(date -u +%FT%TZ)"
  caffeinate -is uv run python scripts/week2_monitors.py score --step "$s" \
      --config configs/week2/monitors.toml --go >> "$LOG" 2>&1
  rc=$?
  read -r paused reason reset <<<"$(python3 -c "
import json
d=json.load(open('$SUM'))
print(d.get('paused'), d.get('pause_reason'), d.get('five_hour_resets_at'))" 2>/dev/null)"
  echo "MON_STEP_END step=$s rc=$rc paused=${paused:-?} reason=${reason:-?} utc=$(date -u +%FT%TZ)"
  if [ "$rc" != 0 ]; then echo "MON_STOP step=$s why=rc"; exit 1; fi
  if [ "${paused:-}" != "True" ]; then exit 0; fi
  if [ "${reason:-}" != "five_hour" ] || [ -z "${reset:-}" ] || [ "$reset" = "None" ]; then
    echo "MON_STOP step=$s why=${reason:-unknown}"; exit 1
  fi
  wait_s=$(( reset + 120 - $(date +%s) ))
  [ "$wait_s" -lt 60 ] && wait_s=60
  echo "MON_FIVE_HOUR_WAIT step=$s until=$(date -u -r $(( $(date +%s) + wait_s )) +%FT%TZ) seconds=$wait_s"
  sleep "$wait_s"
done
