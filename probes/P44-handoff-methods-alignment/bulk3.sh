#!/bin/bash
# P44 bulk at the shared-laptop cap (log.md 05:14): after pass 0 (bulk.sh 0 4, stopped by
# results/STOP before pass 1) ends, the priority passes 1-5 of bulk.sh at 6 sandboxes (+ scorer 2
# = 8 containers). The count is re-read from results/MAX_SAMPLES at each pass.
set -u
cd "$(dirname "$0")/../.." || exit 1
P=probes/P44-handoff-methods-alignment
while pgrep -f "[b]ulk.sh 0 4" > /dev/null; do sleep 20; done
rm -f $P/results/STOP
echo "P44_BULK3 start $(date -u +%H:%M:%S)"
bash $P/bulk.sh 1 5 6
echo "P44_BULK3 end $(date -u +%H:%M:%S) rc=$?"
