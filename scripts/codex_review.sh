#!/bin/bash
# Run a gpt-6-sol review through Codex with the project's 20-minute timeout (CLAUDE.md).
# Usage: scripts/codex_review.sh <prompt_file> <output_md> [effort] [seconds]
# effort defaults to high; use medium only for mechanical checks, and note it in the report.
# Runs from the repo root the script lives in; stdout/stderr go to <output_md>.log.
#
# The timeout is enforced by a watchdog that kills codex and its children. The `perl -e 'alarm'`
# wrapper CLAUDE.md suggests does not stop codex-cli 0.157: its reviews ran past 20 minutes on
# 2026-09-29. bash 3.2 compatible (macOS).
set -u
cd "$(dirname "$0")/.." || exit 1
prompt="$1"
out="$2"
effort="${3:-high}"
limit="${4:-1200}"
mkdir -p "$(dirname "$out")"

codex exec -m gpt-6-sol -c model_reasoning_effort="$effort" --sandbox read-only \
  -o "$out" - < "$prompt" > "$out.log" 2>&1 &
pid=$!
(
  sleep "$limit"
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
