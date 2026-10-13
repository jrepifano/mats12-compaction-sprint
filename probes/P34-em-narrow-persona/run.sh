#!/usr/bin/env bash
# P34 pod entry point (scripts/probe_pod.py run contract; plan.md "Automatic rules", on the pod).
# Ported from probes/P20-em-agentic-transfer/run.sh. Steps (p34/pod.py has the details):
#   download (/ws/venv-peft)  base snapshot at the pin; inputs, E11 adapters, persona data sha256
#   persona  (/ws/venv-peft)  the three persona LoRAs (verified ones skipped), each merged into the
#                             base on CPU -> /ws/merged_<persona> (stays on the pod); f1: only the
#                             persona-seed-1 LoRAs quack1 and ecc1 (quack's and ecc's data)
#   em       (/ws/venv-peft)  session1: the bench EM unit quack_em_s0; session2: every new EM key;
#                             f1: the six quack1_em_s<k> / ecc1_em_s<k> keys
#   gen      (/ws/venv-vllm)  the session's generation plan, one child process per vLLM engine
#   margins  (/ws/venv-peft)  E11's bad/good log-prob inputs for the session's margin views
#   manifest                  results/MANIFEST.sha256 over every result file (aside, then moved)
# The committed file `phase` ("session1", "session2" or "f1"; env P34_PHASE overrides) picks the
# session; each runs all five steps (session 2 re-merges, skips verified training, and uses the
# session-2 generation and margin plans; f1, the follow-up of log.md 20:50, uses K.F1_PLAN and
# K.MARGIN_VIEWS_F1 -> results/logprobs/task_f1.csv). Per-step records: results/<step>_<phase>.json.
# A failed step stops the chain: rc 11 download, 12 persona, 13 em, 14 gen, 15 margins; rc 2 = a
# missing venv or input or a bad phase; rc 4 = the soft wall-clock guard (P34_GUARD_S, default
# 1.0 h for session1, 2.0 h for session2, 1.5 h for f1) stopped the chain before a new step. The manifest and
# the last log line `PROBE_DONE rc=<n>` are always written (except under P34_DRY_RUN=1, which
# prints the steps, touches nothing and exits 0). bash 3.2-compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P34 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
PHASE="${P34_PHASE:-$(tr -d '[:space:]' < phase 2>/dev/null || echo unset)}"
STEPS="download persona em gen margins"
case "$PHASE" in
  session1) DEFAULT_GUARD=3600 ;;
  session2) DEFAULT_GUARD=7200 ;;
  f1) DEFAULT_GUARD=4500 ;;
  *) DEFAULT_GUARD=0 ;;
esac
GUARD_S="${P34_GUARD_S:-$DEFAULT_GUARD}"
echo "P34 phase=$PHASE steps=$STEPS guard=${GUARD_S}s"
if [ "${P34_DRY_RUN:-0}" = 1 ]; then
  echo "P34 dry steps ($PHASE):"
  echo "  1 download: /ws/venv-peft/bin/python -m p34.pod download (rc 11 on failure)"
  echo "  2 persona:  /ws/venv-peft/bin/python -m p34.pod persona (rc 12 on failure)"
  echo "  3 em:       /ws/venv-peft/bin/python -m p34.pod em (rc 13 on failure)"
  echo "  4 gen:      /ws/venv-vllm/bin/python -m p34.pod gen (rc 14 on failure)"
  echo "  5 margins:  /ws/venv-peft/bin/python -m p34.pod margins (rc 15 on failure)"
  echo "  6 manifest: shasum -a 256 over results/ -> results/MANIFEST.sha256"
  echo "P34 dry guard: no new step after ${GUARD_S} s (rc 4)"
  exit 0
fi
mkdir -p results logs
echo $$ > logs/run.pid 2>/dev/null || true
T0="$(date +%s)"
export P34_PHASE="$PHASE"
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT=/ws/venv-peft/bin/python
VLLM=/ws/venv-vllm/bin/python
RC=0
if [ "$DEFAULT_GUARD" = 0 ]; then
  echo "P34 bad phase '$PHASE' (expected session1, session2 or f1)"; RC=2
fi
for PY in "$PEFT" "$VLLM"; do
  if [ ! -x "$PY" ]; then echo "P34 missing $PY (pod_setup.sh --only e11)"; RC=2; fi
done
for P in quack ecc plain; do
  for F in "data/persona_$P.jsonl" "data/heldout_$P.jsonl"; do
    if [ ! -s "$F" ]; then echo "P34 missing $F (built and committed on the laptop)"; RC=2; fi
  done
done
for F in data/prompts.jsonl data/heldout_pids.json; do
  if [ ! -s "$F" ]; then echo "P34 missing $F (built and committed on the laptop)"; RC=2; fi
done

step_env() {  # GPU, disk, memory and time before each step
  echo "== $(date -u +%FT%TZ) step=$1 elapsed=$(( $(date +%s) - T0 ))s"
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader \
    2>/dev/null || true
  df -h /ws 2>/dev/null | tail -1 || true
  free -g 2>/dev/null | sed -n 2p || true
}

for STEP in $STEPS; do
  [ "$RC" = 0 ] || break
  ELAPSED=$(( $(date +%s) - T0 ))
  if [ "$ELAPSED" -ge "$GUARD_S" ]; then
    echo "P34 guard: ${ELAPSED}s >= ${GUARD_S}s, not starting step $STEP"; RC=4; break
  fi
  case "$STEP" in
    download) PY="$PEFT"; FAIL=11 ;;
    persona) PY="$PEFT"; FAIL=12 ;;
    em) PY="$PEFT"; FAIL=13 ;;
    gen) PY="$VLLM"; FAIL=14 ;;
    margins) PY="$PEFT"; FAIL=15 ;;
  esac
  step_env "$STEP"
  S0="$(date +%s)"
  "$PY" -m p34.pod "$STEP"
  PRC=$?
  echo "== $(date -u +%FT%TZ) step=$STEP python_rc=$PRC seconds=$(( $(date +%s) - S0 ))"
  if [ "$PRC" != 0 ]; then RC="$FAIL"; fi
done

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0 ))s"
# every file under results/, a crash's stray *.tmp too (the runner's pull needs each pulled file
# listed); the manifest itself is written beside results/, then moved in. Merged models stay on
# /ws and never enter results/.
( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) \
  && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P34 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0 ))s"
echo "PROBE_DONE rc=$RC"
