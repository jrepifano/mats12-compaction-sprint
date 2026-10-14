#!/bin/bash
# P44 bulk (log.md 04:45): GLM chains in priority passes, so the most decision-relevant cells
# finish first under the 8-container limit. Each pass is one run.py invocation (one chunk, so
# the sandboxes stay full until its tail); a STOP file in results/ ends the run between chunks.
#   pass 1  core natural arms        R, C, CP, N, L          seed none
#   pass 2  permission seed + control C, N, L                 seeds P, U
#   pass 3  controls and M           CPw, C1, N1, M          seed none
#   pass 4  DeepSeek check           prefixes, then R + C/N/L seeds P, U (15 tasks x 2 runs)
#   pass 5  decision seed            C, N, L seed D          (only if time or containers allow)
# Usage: bash probes/P44-handoff-methods-alignment/bulk.sh [first_pass] [last_pass] [max_samples]
set -u
cd "$(dirname "$0")/../.." || exit 1
FIRST=${1:-1}; LAST=${2:-4}; MS=${3:-6}
RUN="uv run --group swarm python probes/P44-handoff-methods-alignment/run.py"
STOP=probes/P44-handoff-methods-alignment/results/STOP
pass() {
  local n=$1; shift
  if [ "$n" -lt "$FIRST" ] || [ "$n" -gt "$LAST" ]; then return 0; fi
  if [ -e "$STOP" ]; then echo "P44_BULK stop file before pass $n"; exit 3; fi
  echo "P44_BULK pass $n start $(date -u +%H:%M:%S) :: $*"
  $RUN "$@" --max-samples "$MS" --chunk 1000
  local rc=$?
  echo "P44_BULK pass $n end $(date -u +%H:%M:%S) rc=$rc"
}
pass 1 chains --model glm --tasks all --runs 3 --arms R,C,CP,N,L --seeds none
pass 2 chains --model glm --tasks all --runs 3 --arms C,N,L --seeds P,U
pass 3 chains --model glm --tasks all --runs 3 --arms CPw,C1,N1,M --seeds none
pass 4 prefixes --model deepseek --tasks all --runs 2
pass 4 chains --model deepseek --tasks all --runs 2 --arms R --seeds none
pass 4 chains --model deepseek --tasks all --runs 2 --arms C,N,L --seeds P,U
pass 5 chains --model glm --tasks all --runs 3 --arms C,N,L --seeds D
echo "P44_BULK done $(date -u +%H:%M:%S)"
