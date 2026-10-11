#!/usr/bin/env bash
# Runs ON the pod (invoked by pod_push.sh). Installs uv and builds three isolated venvs from this
# repo's uv.lock, one per conflicting dependency group (pyproject [tool.uv] conflicts):
#   /ws/venv-peft     --group pod-peft     (PEFT trainer)
#   /ws/venv-unsloth  --group pod-unsloth  (unsloth trainer)
#   /ws/venv-vllm     --group vllm         (generation)
# Each gets the project (emx) plus the default deps; the laptop-only `dev` group is excluded.
# Idempotent: `uv sync --frozen` is a no-op when a venv already matches the lock.
# Replaces ../mats12_neel_working/scripts/pod_setup.sh (kept: uv install, uv cache on the same
# disk as the venvs, a long HTTP timeout, never tracing the token, import checks from outside
# the repo). Dropped: the upstream organism-repo clone and the modern-stack override block
# (uv.lock now pins the stack). WANDB_MODE=offline.
# 2026-09-29 (I1a implementation plan §2 U1): `--only i1a` builds only
#   /ws/venv-i1a      --group pod-i1a      (WorkspaceBench readers, Oracle Lens, reconstructor)
# and checks its CUDA; with no argument the E1 build above is unchanged.
# 2026-10-03 (E1b implementation plan §2 U7): `--only e1b` builds E1's /ws/venv-peft (training,
# HF generation, restore, firstk, steering) and /ws/venv-vllm (generation), not unsloth (E1
# selected PEFT; E1b trains with it).
# 2026-10-04 (I9 implementation plan §2 U6): `--only i9` builds two venvs with the shared builder:
#   /ws/venv-i9       --group pod-i1a      (the I1a stack: wsbench, torch, transformers, peft,
#                                           safetensors; capture, readers, HF sampling fallback)
#   /ws/venv-vllm     --group vllm         (sampling, as E1b's)
# and checks CUDA in /ws/venv-i9.
# 2026-10-06 (E3 implementation plan U8c): `--only e3` builds what E3's pod jobs need, as E1b's
# target: /ws/venv-peft (training with PEFT, step-50 hook, pair scoring, Hub upload through
# huggingface_hub) and /ws/venv-vllm (sampled secondary). The lock has no hf_transfer; downloads go
# through huggingface_hub's default (hf_xet) path, as E1b's did. It then checks CUDA, the free
# disk under /ws (the seven checkpoints are downloaded one at a time) and that HF_TOKEN and
# HF_USERNAME are set (E3 pushes private e3-* repos).
# 2026-10-06 (E9 implementation plan v4, unit F2; prereg §8.1): `--only e9` builds /ws/venv-peft
# (continued pretraining, fine-tunes, pair scoring, Hub upload) PLUS torchao==0.18.0 installed
# into it with `uv pip install --no-deps` (AdamW8bit with bf16 stochastic rounding, prereg §4.5; torchao is
# not in the pod-peft group's lock, which is left untouched; 0.18.0 is the version uv.lock
# resolves elsewhere), then /ws/venv-vllm (generation). The pip freeze of each venv is
# re-recorded after the extra install (/ws/venv-peft.freeze.txt lists torchao), and the torchao
# install is logged in /ws/venv-peft.extra.txt. Then CUDA, torchao's AdamW8bit import, the free
# disk under /ws, and HF_TOKEN / HF_USERNAME (E9 pushes private e9-* adapter repos).
# 2026-10-08 (EQ0 implementation plan, pod tooling unit): `--only eq0` builds, as e1b's target,
#   /ws/venv-peft     --group pod-peft     (the train-bench throughput run; the HF side and the
#                                           scoring of the LoRA scaling check)
#   /ws/venv-vllm     --group vllm         (download, smoke, generation; the vLLM side of the
#                                           LoRA check), as scripts/pod_run_eq0.sh assigns them
# then checks CUDA, that bitsandbytes imports in venv-peft (the trainer's AdamW8bit) and that
# HF_TOKEN is set (EQ0 downloads only; no HF_USERNAME: it pushes nothing to the Hub).
# 2026-10-08 (E11 implementation plan, unit U1): `--only e11` builds EQ0's two venvs,
#   /ws/venv-peft     --group pod-peft     (rank-32 training, HF log-probs, activations, the HF
#                                           side of the LoRA check, the HF fallback generator)
#   /ws/venv-vllm     --group vllm         (generation, the vLLM side of the LoRA check)
# then checks CUDA, that bitsandbytes (AdamW8bit), safetensors and numpy import in venv-peft (all
# three are already in pod-peft's lock: numpy is a project dependency, safetensors comes with
# transformers/peft; nothing extra is installed) and that HF_TOKEN is set (no HF_USERNAME).
set -euxo pipefail

ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="${2:?--only needs a value}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$ONLY" in
  e3) ;;   # 2026-10-06 (E3), kept apart from the line below
  e9) ;;   # 2026-10-06 (E9), likewise
  eq0) ;;  # 2026-10-08 (EQ0), likewise
  e11) ;;  # 2026-10-08 (E11), likewise
  ""|i1a|e1b|i9) ;;
  *) echo "--only knows: i1a, e1b, i9, e3, e9, eq0, e11" >&2; exit 2 ;;
esac
E9_TORCHAO="torchao==0.18.0"   # E9: AdamW8bit(bf16_stochastic_round=True); not in pod-peft's lock

set +x; set -a; source /ws/emx/.env; set +a; set -x   # never trace the token
export WANDB_MODE=offline
export HF_HOME=/ws/hf_cache
export UV_CACHE_DIR=/ws/uv_cache   # same filesystem as the venvs: hardlinks, not copies
export UV_HTTP_TIMEOUT=300
export UV_PYTHON=3.12

command -v uv >/dev/null || [ -x "$HOME/.local/bin/uv" ] || (curl -LsSf https://astral.sh/uv/install.sh | sh)
export PATH="$HOME/.local/bin:$PATH"
uv --version

cd /ws/emx
# unsloth writes ./unsloth_compiled_cache at first import; keep it out of the project dir
rm -rf unsloth_compiled_cache wandb

build() {  # build NAME GROUP IMPORTS
  local name=$1 group=$2 imports=$3
  UV_PROJECT_ENVIRONMENT=/ws/venv-$name uv sync --frozen --no-default-groups --group "$group"
  # import check from OUTSIDE the repo so the cwd cannot mask a broken install
  ( cd /tmp && /ws/venv-"$name"/bin/python -c "import emx, $imports; print('$name imports OK')" )
  uv pip freeze --python /ws/venv-"$name"/bin/python \
    > /ws/venv-"$name".freeze.txt
}
if [ "$ONLY" = "i1a" ]; then
  build i1a pod-i1a "wsbench, torch, transformers, peft, safetensors, sklearn"
  /ws/venv-i1a/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venv /ws/venv-i1a"
  exit 0
fi
if [ "$ONLY" = "i9" ]; then
  build i9 pod-i1a "wsbench, torch, transformers, peft, safetensors, sklearn"
  build vllm vllm "vllm, torch, transformers"
  /ws/venv-i9/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{i9,vllm} (I9)"
  exit 0
fi
build peft pod-peft "torch, transformers, peft, trl, accelerate"
if [ "$ONLY" = "e1b" ]; then
  build vllm vllm "vllm, torch, transformers"
  /ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft,vllm} (E1b)"
  exit 0
fi
if [ "$ONLY" = "eq0" ]; then
  build vllm vllm "vllm, torch, transformers"
  ( cd /tmp && /ws/venv-peft/bin/python -c "import bitsandbytes as bnb; from bitsandbytes.optim import AdamW8bit; print('bitsandbytes', bnb.__version__)" )
  /ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  set +x
  [ -n "${HF_TOKEN:-}" ] || { echo "HF_TOKEN missing from /ws/emx/.env" >&2; exit 1; }
  set -x
  df -h /ws
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft,vllm} (EQ0)"
  exit 0
fi
if [ "$ONLY" = "e11" ]; then
  build vllm vllm "vllm, torch, transformers"
  ( cd /tmp && /ws/venv-peft/bin/python -c "import bitsandbytes as bnb; from bitsandbytes.optim import AdamW8bit; import safetensors, numpy; print('bitsandbytes', bnb.__version__, 'safetensors', safetensors.__version__, 'numpy', numpy.__version__)" )
  /ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  set +x
  [ -n "${HF_TOKEN:-}" ] || { echo "HF_TOKEN missing from /ws/emx/.env" >&2; exit 1; }
  set -x
  df -h /ws
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft,vllm} (E11)"
  exit 0
fi
if [ "$ONLY" = "e3" ]; then
  build vllm vllm "vllm, torch, transformers"
  ( cd /tmp && /ws/venv-peft/bin/python -c "import huggingface_hub; print('huggingface_hub', huggingface_hub.__version__)" )
  /ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  set +x
  [ -n "${HF_TOKEN:-}" ] || { echo "HF_TOKEN missing from /ws/emx/.env" >&2; exit 1; }
  [ -n "${HF_USERNAME:-}" ] || { echo "HF_USERNAME missing from /ws/emx/.env" >&2; exit 1; }
  set -x
  df -h /ws
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft,vllm} (E3)"
  exit 0
fi
if [ "$ONLY" = "e9" ]; then
  UV_PROJECT_ENVIRONMENT=/ws/venv-peft uv pip install --no-deps --python /ws/venv-peft/bin/python "$E9_TORCHAO"   # never moves torch
  printf 'uv pip install --no-deps %s (E9, outside uv.lock; pod_setup.sh --only e9)\n' "$E9_TORCHAO" > /ws/venv-peft.extra.txt
  uv pip freeze --python /ws/venv-peft/bin/python > /ws/venv-peft.freeze.txt
  grep -qi '^torchao==0.18.0' /ws/venv-peft.freeze.txt || { echo "torchao 0.18.0 missing from /ws/venv-peft" >&2; exit 1; }
  ( cd /tmp && /ws/venv-peft/bin/python -c "import torchao; from torchao.optim import AdamW8bit; import huggingface_hub; print('torchao', torchao.__version__, 'huggingface_hub', huggingface_hub.__version__)" )
  build vllm vllm "vllm, torch, transformers"
  /ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  set +x
  [ -n "${HF_TOKEN:-}" ] || { echo "HF_TOKEN missing from /ws/emx/.env" >&2; exit 1; }
  [ -n "${HF_USERNAME:-}" ] || { echo "HF_USERNAME missing from /ws/emx/.env" >&2; exit 1; }
  set -x
  df -h /ws
  echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft(+$E9_TORCHAO),vllm} (E9)"
  exit 0
fi
build unsloth pod-unsloth "unsloth, torch, transformers"
build vllm vllm "vllm, torch, transformers"
rm -rf unsloth_compiled_cache /tmp/unsloth_compiled_cache

/ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft,unsloth,vllm}"
