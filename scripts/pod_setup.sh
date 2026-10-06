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
  ""|i1a|e1b|i9) ;;
  *) echo "--only knows: i1a, e1b, i9, e3" >&2; exit 2 ;;
esac

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
build unsloth pod-unsloth "unsloth, torch, transformers"
build vllm vllm "vllm, torch, transformers"
rm -rf unsloth_compiled_cache /tmp/unsloth_compiled_cache

/ws/venv-peft/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0))"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
echo "pod setup complete: repo commit ${EMX_GIT_COMMIT:-unknown} (dirty=${EMX_GIT_DIRTY:-unknown}); venvs /ws/venv-{peft,unsloth,vllm}"
