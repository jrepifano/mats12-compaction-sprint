#!/usr/bin/env bash
# P99: a run that hangs past the deadline margin; a child process holds the group open.
set -uo pipefail
cd "$(dirname "$0")"
echo "P99 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset}"
mkdir -p results
echo "partial result written before the hang" > results/partial.txt
( while true; do date -u +%FT%TZ >> results/heartbeat.txt; sleep 30; done ) &
echo "child $! writing heartbeats; parent now waits forever"
wait
