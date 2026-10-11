#!/usr/bin/env bash
# P98: a run that crashes after writing a partial result (no manifest, no PROBE_DONE).
set -uo pipefail
cd "$(dirname "$0")"
echo "P98 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset}"
mkdir -p results
echo "partial result written before the crash" > results/partial.txt
echo "step 1 done" ; sleep 20
echo "simulated crash: exiting 3 without PROBE_DONE"
exit 3
