#!/bin/bash
# Experiment 4, laptop: stub and push-check under the current code, then the $0 recheck of the
# saved live smoke (ran at ca6778de) instead of rerunning it (Research Advisor, 28 Oct).
set -u
cd "$(dirname "$0")/../.." || exit 1
export SPRINT_DEEPSWE_DIR="$PWD/data/raw/sprint/deepswe"
export SPRINT_CHAIN_RAW_DIR="$PWD/data/raw/sprint/s2/chain_stress"
LOG="$SPRINT_CHAIN_RAW_DIR/laptop_checks.log"
SMOKE="$SPRINT_CHAIN_RAW_DIR/smoke_chain_stress/v3-8fc29279dd67-6fd16f60031c-3d5da64fd312"
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
say "recheck run at $(git rev-parse --short HEAD): stub, push-check, live-smoke recheck"
run() {
  say "step $1: start"
  shift
  caffeinate -is uv run python "$@" >> "$LOG" 2>&1
  rc=$?
  say "step rc=$rc"
  return $rc
}
run stub scripts/s2_chain.py stub-chain-stress --host laptop || { say "LAPTOP_RECHECK_DONE rc=fail step=stub"; exit 1; }
run push-check scripts/s2_chain.py push-check --week2-config configs/week2/chain_stress.toml --host laptop || { say "LAPTOP_RECHECK_DONE rc=fail step=push-check"; exit 2; }
run recheck scripts/week2_live_smoke_recheck.py --smoke-dir "$SMOKE" --ran-at ca6778de --host laptop || { say "LAPTOP_RECHECK_DONE rc=fail step=recheck"; exit 3; }
say "LAPTOP_RECHECK_DONE rc=0"
