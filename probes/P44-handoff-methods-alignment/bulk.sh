#!/bin/bash
# P44 bulk (log.md 04:45): GLM chains in priority passes, so the most decision-relevant cells
# finish first under the 8-container limit. Each pass is one run.py invocation (one chunk, so
# the sandboxes stay full until its tail); a STOP file in results/ ends the run between chunks.
#   (log.md 04:58: GLM on Together, DeepSeek on Baidu fp8, each pinned; GLM prefixes regenerated)
#   pass 0  GLM prefixes on Together (45)
#   pass 1  core natural arms        R, C, CP, N, L          seed none
#   pass 2  permission seed + control C, N, L                 seeds P, U
#   pass 3  controls and M           CPw, C1, N1, M          seed none
#   pass 4  DeepSeek check           prefixes, then R + C/N/L seeds P, U (15 tasks x 2 runs)
#   pass 5  decision seed            C, N, L seed D          (only if time or containers allow)
# Usage: bash probes/P44-handoff-methods-alignment/bulk.sh [first_pass] [last_pass] [max_samples]
set -u
cd "$(dirname "$0")/../.." || exit 1
FIRST=${1:-0}; LAST=${2:-4}; MS=${3:-6}
RUN="uv run --group swarm python probes/P44-handoff-methods-alignment/run.py"
STOP=probes/P44-handoff-methods-alignment/results/STOP
MSFILE=probes/P44-handoff-methods-alignment/results/MAX_SAMPLES
pass() {
  local n=$1; shift
  if [ "$n" -lt "$FIRST" ] || [ "$n" -gt "$LAST" ]; then return 0; fi
  if [ -e "$STOP" ]; then echo "P44_BULK stop file before pass $n"; exit 3; fi
  # sandbox count re-read at each pass (results/MAX_SAMPLES), so it can rise as other runs end
  if [ -f "$MSFILE" ]; then MS=$(tr -dc 0-9 < "$MSFILE"); fi
  echo "P44_BULK pass $n start $(date -u +%H:%M:%S) max_samples=$MS :: $*"
  $RUN "$@" --max-samples "$MS" --chunk 1000
  local rc=$?
  echo "P44_BULK pass $n end $(date -u +%H:%M:%S) rc=$rc"
}
pass 0 prefixes --model glm-tg --tasks all --runs 3
pass 1 chains --model glm-tg --tasks all --runs 3 --arms R,C,CP,N,L --seeds none
pass 2 chains --model glm-tg --tasks all --runs 3 --arms C,N,L --seeds P,U
pass 3 chains --model glm-tg --tasks all --runs 3 --arms CPw,C1,N1,M --seeds none
pass 4 prefixes --model deepseek-bd --tasks all --runs 2
pass 4 chains --model deepseek-bd --tasks all --runs 2 --arms R --seeds none
pass 4 chains --model deepseek-bd --tasks all --runs 2 --arms C,N,L --seeds P,U
pass 5 chains --model glm-tg --tasks all --runs 3 --arms C,N,L --seeds D
echo "P44_BULK done $(date -u +%H:%M:%S)"
