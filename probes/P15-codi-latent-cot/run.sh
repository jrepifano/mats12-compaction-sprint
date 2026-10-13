#!/usr/bin/env bash
# P15 pod entry point (scripts/probe_pod.py run contract). Builds /ws/venv-p15 with the upstream
# CoDI repo's pins, clones that repo at the pinned commit (unlicensed: never vendored), then
#   env -> download -> stage_a (smoke gate; rc 3 = structural fail stops everything)
#   -> b_data -> training benchmark -> training -> b_eval
# and writes results/MANIFEST.sha256 over every result file; last line PROBE_DONE rc=<n>.
# Stage B work is skipped (recorded, rc stays 0) when it would end past P15_MAX_HOURS.
# bash 3.2-compatible. Env (optional): P15_MAX_HOURS (default 2.6), P15_SKIP_B=1.
# Laptop dry run only (a copy of this directory, tiny model): P15_PY=<venv python> skips the venv
# build, P15_DEVICE=cpu skips the CUDA check, P15_DRYRUN=1 skips the download (paths.json preset);
# P15_FORCE_A=1 runs past a failed structural smoke (a random tiny model can't pass it);
# P15_HF_HOME, P15_UV_CACHE, P15_LORA_R, P15_PRJ_DIM and P15_TRAIN_EXTRA adapt it to the tiny model.
set -uo pipefail
cd "$(dirname "$0")"
echo "P15 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
echo $$ > logs/run.pid 2>/dev/null || true
export HF_HOME="${P15_HF_HOME:-/ws/hf_cache}" HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
export UV_CACHE_DIR="${P15_UV_CACHE:-/ws/uv_cache}" UV_HTTP_TIMEOUT=300
export P15_UPSTREAM="${P15_UPSTREAM:-/ws/codi_upstream}" P15_DATA="${P15_DATA:-/ws/p15_data}"
export P15_T0="${P15_T0:-$(date +%s)}" P15_MAX_HOURS="${P15_MAX_HOURS:-2.6}"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
PY="${P15_PY:-/ws/venv-p15/bin/python}"
UPSTREAM_COMMIT=f0bf2fa0f5a54c749395a5d095bc406ad18b8efc
mkdir -p results logs "$P15_DATA"
RC=0

stamp() { echo "P15 phase $1 $(date -u +%FT%TZ) elapsed_s=$(( $(date +%s) - P15_T0 ))"; }
hours_left() {  # prints the hours left before P15_MAX_HOURS
  python3 -c "import os,time; print(float(os.environ['P15_MAX_HOURS']) - (time.time() - int(os.environ['P15_T0'])) / 3600)"
}

stamp setup
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
df -h /ws 2>/dev/null | tail -1 || true
command -v uv >/dev/null || [ -x "$HOME/.local/bin/uv" ] || (curl -LsSf https://astral.sh/uv/install.sh | sh)
export PATH="$HOME/.local/bin:$PATH"
if ! test -x "$PY"; then
  uv venv -q --python 3.12 /ws/venv-p15 && VIRTUAL_ENV=/ws/venv-p15 uv pip install -q \
    torch==2.7.1 transformers==4.52.4 peft==0.15.2 accelerate==1.7.0 datasets==3.6.0 \
    safetensors==0.5.3 huggingface-hub==0.36.0 numpy==2.2.6 python-dotenv==1.0.0 pyyaml==6.0.2 \
    wandb==0.23.0 pandas pyarrow || RC=2
fi
[ "$RC" -eq 0 ] && { uv pip freeze --python "$PY" > results/venv_freeze.txt || true; }
if [ "$RC" -eq 0 ] && ! test -d "$P15_UPSTREAM/.git"; then
  git clone -q https://github.com/cywinski/codi "$P15_UPSTREAM" \
    && git -C "$P15_UPSTREAM" checkout -q "$UPSTREAM_COMMIT" || RC=2
fi
if [ "$RC" -eq 0 ] && [ "${P15_DEVICE:-cuda}" = "cuda" ]; then
  "$PY" -c "import torch; assert torch.cuda.is_available(); print('torch', torch.__version__, torch.cuda.get_device_name(0))" || RC=2
fi

[ "$RC" -eq 0 ] && { stamp env; "$PY" -m p15.pod_main env || RC=$?; }
if [ "$RC" -eq 0 ] && [ "${P15_DRYRUN:-0}" != "1" ]; then
  stamp download; "$PY" -m p15.pod_main download || RC=$?
fi
A_FORCE=""; [ "${P15_FORCE_A:-0}" = "1" ] && A_FORCE="--force"  # dry runs on a random tiny model only
# The committed `phase` file picks the session: "all" (default) or "b" (stage B only, after a
# session that finished stage A; its results are already pulled and committed)
PHASE="$(tr -d '[:space:]' < phase 2>/dev/null || true)"; PHASE="${PHASE:-all}"
echo "P15 phase_file=$PHASE"
if [ "$RC" -eq 0 ] && [ "$PHASE" != "b" ]; then
  # shellcheck disable=SC2086
  stamp stage_a; "$PY" -m p15.pod_main stage_a $A_FORCE || RC=$?
fi

# ---- stage B: data, benchmark, training, evaluation (each gated on the time left)
B_SKIP=""
[ "${P15_SKIP_B:-0}" = "1" ] && B_SKIP="P15_SKIP_B=1"
if [ "$RC" -eq 0 ] && [ -z "$B_SKIP" ]; then
  stamp b_data; "$PY" -m p15.pod_main b_data || RC=$?
fi
TRAIN_ARGS="${P15_TRAIN_EXTRA:-} --upstream $P15_UPSTREAM --base-from $P15_DATA/paths.json --train-jsonl $P15_DATA/b_train.jsonl --val-jsonl $P15_DATA/b_val.jsonl"
if [ "$RC" -eq 0 ] && [ -z "$B_SKIP" ]; then
  stamp b_bench
  # shellcheck disable=SC2086
  "$PY" -m p15.train_chain $TRAIN_ARGS --out results/B/bench --bench --epochs 2 || RC=$?
fi
EPOCHS=""
if [ "$RC" -eq 0 ] && [ -z "$B_SKIP" ]; then
  # the plan's rule: 2 epochs if the projection is <= 1.5 h, else 1; then the deadline: training
  # plus ~0.5 h of stage B evaluation and pull margin must fit in the hours left
  EPOCHS="$("$PY" -m p15.train_chain --decide-epochs results/B/bench/train_record.json --hours-left "$(hours_left)" --eval-hours 0.5)" || RC=$?
  echo "P15 b_epochs decision=$EPOCHS hours_left=$(hours_left)"
  [ "$EPOCHS" = "0" ] && B_SKIP="no time for stage B training"
fi
if [ "$RC" -eq 0 ] && [ -z "$B_SKIP" ]; then
  stamp b_train
  # shellcheck disable=SC2086
  "$PY" -m p15.train_chain $TRAIN_ARGS --out results/B --epochs "$EPOCHS" || RC=$?
  if [ "$RC" -eq 0 ] && ! "$PY" -c "import json,sys; sys.exit(0 if json.load(open('results/B/train_record.json'))['loss_decreasing'] else 1)"; then
    B_SKIP="training loss not decreasing (plan rule)"
  fi
fi
if [ "$RC" -eq 0 ] && [ -z "$B_SKIP" ]; then
  stamp b_eval; "$PY" -m p15.pod_main b_eval || RC=$?
fi
[ -n "$B_SKIP" ] && echo "P15 stage_b_skipped reason=$B_SKIP" && echo "$B_SKIP" > results/stage_b_skipped.txt

stamp manifest
( cd results && find . -type f ! -name MANIFEST.sha256 ! -name '*.tmp' -print0 | sort -z \
    | xargs -0 shasum -a 256 > ../MANIFEST.sha256.tmp ) && mv MANIFEST.sha256.tmp results/MANIFEST.sha256
echo "P15 end $(date -u +%FT%TZ) rc=$RC"
echo "PROBE_DONE rc=$RC"
