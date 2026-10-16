#!/usr/bin/env bash
# P50 pod entry point (scripts/probe_pod.py run contract). Block order and the seed-set
# deadline rule are in p50/contracts.py and p50/pod.py (`seedsets`, `fits`).
# rc: 11 download, 12 smoke, 14 eval, 2 missing venv/input. Train failures are recorded in
# results/train_failed.json and do not stop the chain. bash 3.2 compatible.
set -uo pipefail
cd "$(dirname "$0")"
echo "P50 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
GUARD_S="${P50_GUARD_S:-30600}"   # 8.5 h
T0_S="$(date +%s)"
export P50_GUARD_END=$(( T0_S + GUARD_S ))
export HF_HOME=/ws/hf_cache HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=60 WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONPATH="$PWD:$PWD/../../src${PYTHONPATH:+:$PYTHONPATH}"
PEFT="${P50_PEFT:-/ws/venv-peft/bin/python}"
VLLM="${P50_VLLM:-/ws/venv-vllm/bin/python}"
RC=0

if [ "${P50_DRY_RUN:-0}" = 1 ]; then
  DRY="${P50_DRY_PY:-uv run python}"
  for A in pass fail; do
    echo "P50 dry: anchor $A"
    if [ "$A" = pass ]; then BLOCKS="A B1 B2 M C"; else BLOCKS="A B1 B2 F M"; fi
    # shellcheck disable=SC2086
    P50_ASSUME_ANCHOR=$A $DRY -m p50.pod project --blocks $BLOCKS ${P50_LAUNCH:+--launch $P50_LAUNCH}
  done
  exit 0
fi

mkdir -p results logs
for PY in "$PEFT" "$VLLM"; do
  [ -x "$PY" ] || { echo "P50 missing $PY (pod_setup.sh --only e11)"; RC=2; }
done
for F in results/arms.json data/mix/mix100ft.jsonl data/mix/heldoutft.jsonl \
         data/arms/rewx_r05.jsonl ../P48-subliminal-numbers-attribution/p48/train.py \
         ../P48-subliminal-numbers-attribution/data/raw/animal_questions.json; do
  [ -s "$F" ] || { echo "P50 missing $F"; RC=2; }
done

# vLLM's LoRA kernels need the ninja binary on PATH (P48 attempts 12-13).
if [ "$RC" = 0 ]; then
  if ! "$VLLM" -c "import ninja" >/dev/null 2>&1; then
    UVBIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
    UV_CACHE_DIR=/ws/uv_cache "$UVBIN" pip install --quiet --python "$VLLM" ninja \
      || { echo "P50 ninja install FAILED"; RC=2; }
  fi
  NINJA_BIN="$("$VLLM" -c "import ninja; print(ninja.BIN_DIR)" 2>/dev/null)"
  [ -n "$NINJA_BIN" ] && PATH="$NINJA_BIN:$PATH"
  PATH="$(dirname "$VLLM"):$PATH"
  export PATH
  command -v ninja >/dev/null 2>&1 || { echo "P50 ninja not on PATH"; RC=2; }
fi

step_env() {
  echo "== $(date -u +%FT%TZ) step=$1 elapsed=$(( $(date +%s) - T0_S ))s"
  nvidia-smi --query-gpu=name,memory.used --format=csv,noheader 2>/dev/null || true
}

run_step() {  # run_step NAME PY ARGS...  (guarded; returns the python rc)
  local name=$1 py=$2; shift 2
  [ "$RC" = 0 ] || return 1
  local el=$(( $(date +%s) - T0_S ))
  if [ "$el" -ge "$GUARD_S" ]; then
    echo "P50 guard: ${el}s >= ${GUARD_S}s, not starting $name"; return 4
  fi
  step_env "$name"
  local s0; s0="$(date +%s)"
  "$py" -m p50.pod "$@" < /dev/null
  local prc=$?
  echo "== $(date -u +%FT%TZ) step=$name python_rc=$prc seconds=$(( $(date +%s) - s0 ))"
  return $prc
}

run_block() {  # run_block BLOCK: train its seed sets in order while each one fits
  local block=$1 line
  [ "$RC" = 0 ] || return 0
  "$PEFT" -m p50.pod seedsets --block "$block" < /dev/null > "logs/seedsets_$block.txt" \
    || { echo "P50 could not list seed sets for $block"; return 0; }
  while read -r line <&3; do
    [ -n "$line" ] || continue
    # shellcheck disable=SC2086
    if ! "$PEFT" -m p50.pod fits --block "$block" --keys $line < /dev/null; then
      echo "P50 cut: block $block stops before [$line]"; break
    fi
    # shellcheck disable=SC2086
    run_step train "$PEFT" train --tag "$block" --keys $line || echo "P50 train rc!=0 in $block"
  done 3< "logs/seedsets_$block.txt"
}

eval_block() {  # eval_block TAG BLOCK...: score every finished adapter of those blocks
  local tag=$1 keys="" b k; shift
  [ "$RC" = 0 ] || return 0
  for b in "$@"; do
    for k in $("$PEFT" -m p50.pod blockkeys --block "$b" < /dev/null); do
      [ -s "results/adapters/$k/adapter_model.safetensors" ] && keys="$keys $k"
    done
  done
  [ -n "$keys" ] || { echo "P50 nothing to score for $tag"; return 0; }
  local saved=$GUARD_S; GUARD_S=$(( GUARD_S + 2400 ))   # scoring finished adapters is never cut
  # shellcheck disable=SC2086
  run_step eval "$VLLM" eval --tag "$tag" --keys $keys || { [ "$RC" = 0 ] && RC=14; }
  GUARD_S=$saved
}

run_step download "$PEFT" download || { [ "$RC" = 0 ] && RC=11; }
run_step smoketrain "$PEFT" smoketrain || { [ "$RC" = 0 ] && RC=12; }
run_step smokeeval "$VLLM" smokeeval || { [ "$RC" = 0 ] && RC=12; }
[ "$RC" = 0 ] && "$PEFT" -m p50.pod project --setup-min 0 --launch "$(date -u +%FT%TZ)" < /dev/null

run_block A
eval_block A A
ANCHOR=fail
if [ "$RC" = 0 ]; then
  "$PEFT" -m p50.pod anchor < /dev/null && ANCHOR=pass
fi
echo "P50 anchor=$ANCHOR"
run_block B1
run_block B2
eval_block E1 B1 B2
if [ "$ANCHOR" = fail ]; then
  run_block F
  eval_block F F
fi
run_block M
eval_block EM M
if [ "$ANCHOR" = pass ]; then
  run_block C
  eval_block E2 C
fi

echo "== $(date -u +%FT%TZ) step=manifest elapsed=$(( $(date +%s) - T0_S ))s"
( cd results && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 -r shasum -a 256 > ../MANIFEST.sha256.tmp ) \
  && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P50 end $(date -u +%FT%TZ) rc=$RC elapsed=$(( $(date +%s) - T0_S ))s"
echo "PROBE_DONE rc=$RC"
