#!/bin/bash
# Experiment 4's laptop checks, run unattended once experiment 2's controls stop queuing
# (experiment 2 first; Research Advisor, 27 Oct). Stops at the first failing step.
# Steps: stub (5 tasks + scorers, no grading), push-check (23 tasks, token lists),
# live 2-task smoke (Jacob's go, 27 Oct ~2:45 PM ET: "experiment 4 smoke on the laptop can go
# when it's ready").
set -u
cd "$(dirname "$0")/../.." || exit 1
export SPRINT_DEEPSWE_DIR="$PWD/data/raw/sprint/deepswe"
export SPRINT_CHAIN_RAW_DIR="$PWD/data/raw/sprint/s2/chain_stress"
LOG="$SPRINT_CHAIN_RAW_DIR/laptop_checks.log"
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
say "waiting for experiment 2's controls to stop queuing"
while true; do
  why=$(uv run python -c "from compaction.s2.chain import chain_stress as CS; print(CS.controls_hold() or '')" 2>&1 | tail -1)
  [ -z "$why" ] && break
  sleep 300
done
say "controls clear; starting"
run() {
  say "step $1: start"
  shift
  caffeinate -is uv run python scripts/s2_chain.py "$@" >> "$LOG" 2>&1
  rc=$?
  say "step rc=$rc"
  return $rc
}
run stub stub-chain-stress --host laptop || { say "LAPTOP_CHECKS_DONE rc=fail step=stub"; exit 1; }
run push-check push-check --week2-config configs/week2/chain_stress.toml --host laptop || { say "LAPTOP_CHECKS_DONE rc=fail step=push-check"; exit 2; }
run live-smoke live-smoke --go-from-jacob --week2-config configs/week2/chain_stress.toml --host laptop || { say "LAPTOP_CHECKS_DONE rc=fail step=live-smoke"; exit 3; }
say "LAPTOP_CHECKS_DONE rc=0"
