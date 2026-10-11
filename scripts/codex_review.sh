#!/bin/bash
# Run a gpt-6-astra review through Codex with the project's 20-minute timeout (AGENTS.md).
# Usage: scripts/codex_review.sh <prompt_file> <output_md> [effort] [seconds]
# effort defaults to medium (Jacob, 2026-10-04; was gpt-6-sol at high). Pass high for unusually hard artifacts.
# Runs from the repo root the script lives in; stdout/stderr go to <output_md>.log.
#
# The timeout is enforced by a watchdog that kills codex and its children. The `perl -e 'alarm'`
# wrapper the project instructions once suggested does not stop codex-cli 0.157: its reviews ran
# past 20 minutes on 2026-09-29.
# Counterpart for Codex-run work (the reviewer comes from the other family): scripts/claude_review.sh.
# The machine is kept awake with `caffeinate -is` for the duration: on 2026-09-29 the laptop's
# idle and clamshell sleep paused reviews mid-run (pmset log), which looked like timeouts.
# Clamshell (lid-closed) sleep still pauses everything. The watchdog polls the wall clock
# instead of trusting one long `sleep`, which stops counting while the system sleeps.
# bash 3.2 compatible (macOS).
set -u
cd "$(dirname "$0")/.." || exit 1
prompt="$1"
out="$2"
effort="${3:-medium}"
limit="${4:-1200}"
mkdir -p "$(dirname "$out")"

caffeinate -is codex exec -m gpt-6-astra -c model_reasoning_effort="$effort" --sandbox read-only \
  -o "$out" - < "$prompt" > "$out.log" 2>&1 &
pid=$!
start=$(date +%s)
(
  while kill -0 "$pid" 2>/dev/null && [ $(( $(date +%s) - start )) -lt "$limit" ]; do
    sleep 5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "watchdog: killing codex after ${limit}s" >> "$out.log"
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    sleep 10
    pkill -KILL -P "$pid" 2>/dev/null
    kill -KILL "$pid" 2>/dev/null
  fi
) &
watchdog=$!
wait "$pid"
rc=$?
kill "$watchdog" 2>/dev/null
echo "exit=$rc"
