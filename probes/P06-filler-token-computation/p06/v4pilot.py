"""P06 follow-up F7 (exploratory, GPU): DeepSeek-V4-Flash feasibility pilot (log.md "follow-up F7").

    python -m p06.v4pilot download   # snapshot @ MODEL_REV into $HF_HOME (+ the two V4 lenses)
    python -m p06.v4pilot run        # load, smoke (with recorded fixes), 45 prompts, gates

One Blackwell GPU (B300 288 GB, else B200 180 GB), HF transformers 5.17 from /ws/venv-i1a (pod
setup "i1a"), the checkpoint's own quantization: FP8 (e4m3, 128x128 blocks, UE8M0 scales) linears
and FP4 (e2m1, 1x32 UE8M0) routed experts, `FineGrainedFP8HfQuantizer`. Both FP8/FP4 backends come
from the Hub through the `kernels` package, which the pod-i1a lock lacks (run.sh installs
kernels==0.16.2, the range transformers 5.17 accepts: >=0.16,<0.17):
- DeepGEMM (`kernels-community/deep-gemm` v2) has compiled builds only up to torch 2.12, and
  pod-i1a has torch 2.13.0 (cu130), so it cannot load; run.sh sets
  TRANSFORMERS_DISABLE_DEEPGEMM_LINEAR=1 so the linears never try it.
- The Triton fallback (`kernels-community/finegrained-fp8` v4, a universal `torch-cuda` build,
  triton 3.7.1 from the lock) serves both: FP8 linears via `matmul_2d`, FP4 experts via
  `matmul_grouped` (`tl.dot_scaled` or an E2M1->E4M3 unpack + fp8 `tl.dot`). The experts
  implementation is transformers' default "grouped_mm" (-> `fp8_grouped_mm_experts_forward`).
Attention is eager (DeepseekV4PreTrainedModel ships eager only: head_dim 512, per-head sinks).

**Smoke and fixes.** Before any prompt, a 64-token forward and a 2-token generate. On failure the
next fix in FIXES is applied and the smoke re-run; every attempt and its error tail is recorded:
1. `rmsnorm_reference_dtype`: transformers' DeepseekV4RMSNorm keeps its weight in fp32
   (`_keep_in_fp32_modules_strict`) and returns `weight * x.to(bf16)`, i.e. fp32, which then
   meets bf16 nn.Linear weights (compressor/indexer projections, lm_head) and bf16 residual
   matmuls: a laptop check on a tiny bf16 DeepseekV4ForCausalLM raises "expected m1 and m2 to
   have the same dtype". The fix returns `(weight * x_normed).to(input dtype)`, which is the
   checkpoint's reference RMSNorm (inference/model.py `RMSNorm.forward`, same pin).
2. `experts_eager`: `set_experts_implementation("eager")` (per-expert Triton matmuls).

**Prompts.** F6's Brauer-format chain prompts (`p06.f5_chain_brauer.messages`, 5 shots = the
first 5 of data/shots_chain10.jsonl), rendered with the checkpoint's own encoder
(`p06.encoding_dsv4`, vendored verbatim, MIT) as `encode_messages(msgs, thinking_mode="chat")`:
BOS, system, then `<｜User｜>...<｜Assistant｜></think>` turns, i.e. non-thinking mode, as the API's
`reasoning: {enabled: false}`. Checked on the laptop: the token count equals the API's
`usage.prompt_tokens` on all 4,200 F6 V4 Flash rows (tests/test_v4pilot.py re-checks a sample).
Items: N_PER_K (10 at k = 2, 5 at k = 3), seeded sample of data/items_chain.jsonl; arms base,
dots500, pre500: 45 prompts.

**Per prompt.**
1. Greedy decode, up to MAX_NEW tokens (`do_sample=False`), graded with nocot's int parser; the
   first step's log-probs of the integer tokens are kept, and the first step's logits are compared
   with the uncached forward's last-position logits (cache path vs no-cache path).
2. One forward (`use_cache=False`) with forward hooks on every decoder layer
   (`model.model.layers[i]`, output [B, S, hc_mult=4, 4096]) keeping the read positions: the
   target turn's label ("Problem:" base, "Filler:" dots500, "Padding:" pre500) through the last
   prompt token.
3. Logit lens at every (layer, read position) = the model's own head path applied to that
   layer's streams: `lm_head(norm(hc_head(streams)))` (DeepseekV4Model.forward:
   `self.norm(self.hc_head(hidden_states))`, then DeepseekV4ForCausalLM's `self.lm_head`), run
   through the loaded modules themselves, fp32 log-softmax over the full vocabulary. Stored: the
   log-probs of the single-token integers "0".."20" (V4 has single tokens for 0..999; the API's
   answers are bare "10"-style tokens with no leading-space variant) as float16 [layer, row, 21]
   and the top-1 id as int32 [layer, row]; one compressed .npz per prompt (NPZ_KEYS).

**Gates** (results/v4pilot/pilot.json; anchored `P06_GATE` log lines):
1. Lens vs model: the last layer's logit-lens top-1 equals the model's own logits top-1 at
   >= AGREE_MIN of all read positions.
2. Local accuracy per arm next to the API's F6 accuracy on the same items and arms (2 draws), and
   the per-row agreement of the local greedy answer with the API's draw-0 answer.
3. Per-phase timings (download, load, smoke, per prompt: generate / forward / readout / write),
   peak GPU memory, free memory after load, and a projection for the bulk run (BULK_ITEMS x 3
   arms + BULK_TX transplants).
4. `local_uplift` (coordinator's scope addition, 2026-10-11): after the readout part, and even if
   gate 1 failed or the readout part raised, generate-only (greedy, up to MAX_NEW tokens, no
   capture, no npz) on all 100 chain items at k = 2 (sorted by item_id) in dots500 and pre500,
   item by item, then base for every item if the readout part's base generate time projects it
   under BASE_MAX_EXTRA_S. Rows in results/v4pilot/uplift.jsonl. Reported: local accuracy per arm
   and the paired dots500 - pre500 difference with an item bootstrap 95% CI (N_BOOT, seed 0), next
   to the API's F6 rows on the same items (draws 0 and 1 averaged; F6 reported +19.5 [+11.5,
   +28.0] at k = 2). Pass: the local lower bound > 0 and the local mean >= half the API's.
   Unbatched on purpose (see `gen_only`).
Optional: the two V4 Flash lenses in neuronpedia/jacobian-lens @ LENS_REV are loaded on the CPU
for their shapes only (not applied).

Return codes (run health; the gates' verdicts are in pilot.json): 0 complete and gate 1 passed;
3 gate 1 failed; 4 stopped incomplete (time budget); 1 an exception (traceback tail in
results/v4pilot/error.txt; pilot.json still written).
torch/transformers are imported inside functions, so the laptop tests import this without a GPU.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import time
import tomllib
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import encoding_dsv4 as E
from . import f5_chain_brauer as F5
from . import items as I
from .stage1 import DATA
from .stage2 import Timer, append_jsonl, grade_int, utc, write_json

PROBE = Path(__file__).resolve().parents[1]
RESULTS = PROBE / "results"
OUT = RESULTS / "v4pilot"
READOUTS = OUT / "readouts"
F6_ROWS = RESULTS / "stage1" / "f6_chain_brauer_v4flash.jsonl"

MODEL_ID = "deepseek-ai/DeepSeek-V4-Flash"
MODEL_REV = "60d8d70770c6776ff598c94bb586a859a38244f1"
ENCODING_SHA256 = "bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0"
SNAPSHOT_IGNORE = ["inference/*", "assets/*", "encoding/tests/*", "*.png", "*.md"]
N_LAYERS = 43
HC_MULT = 4
D_MODEL = 4096
VOCAB = 129280
LENS_REPO = "neuronpedia/jacobian-lens"
LENS_REV = "9fa267f841d62aa468dab5b9b38c3dc45a82d863"
LENS_FILES = {
    "jlens": "deepseek-v4-flash/jlens/NeelNanda-pile-10k/deepseek-v4-flash_jacobian_lens_cblank.pt",
    "jpp": "deepseek-v4-flash/jpp/Salesforce-wikitext/DeepSeek-V4-Flash_jpp_lens.pt",
}

ARMS = ("base", "dots500", "pre500")
LABELS = {"base": "Problem:", "dots500": "Filler:", "pre500": "Padding:"}
N_PER_K = {2: 10, 3: 5}
N_SHOTS = 5
SEED = 0
MAX_NEW = 6
INT_VALUES = tuple(range(21))
AGREE_MIN = 0.99
LENS_CHUNK = 128
SMOKE_TOKENS = 64
DTYPE = "bfloat16"
ATTN = "eager"
SALVAGE_MARGIN_S = 10 * 60  # stop prompts this long before T0 + gpu_hours
BULK_ITEMS = 200
BULK_TX = 200
TX_FWD, TX_GEN = 2, 4  # per transplant: donor + random-donor capture passes; 4 decodes (stage2)
RATES_USD_H = {"B300 community": 6.94, "B300 secure": 8.99}  # log.md F7 design
UPLIFT_K = 2
UPLIFT_ARMS = ("dots500", "pre500")
BASE_MAX_EXTRA_S = 180  # base joins the uplift part only if projected under this
N_BOOT = 10_000
# F6's API value at k = 2 (log.md F6; analysis_f6_v4flash.json): dots500 - pre500, 2 draws
F6_K2_REPORTED = {"mean": 0.195, "lo": 0.115, "hi": 0.28, "n": 100}
NPZ_KEYS = (
    "item_id", "k", "arm", "answer", "traj", "positions", "slots", "token_ids", "n_prompt",
    "gen_ids", "gen_text", "gen_int_lp", "int_ids", "int_values", "model_top1", "ll_int_lp",
    "ll_top1",
)


def log(msg: str) -> None:
    print(f"[p06.v4pilot {utc()}] {msg}", flush=True)


def record_error(step: str, exc: BaseException) -> None:
    """Append the step, the exception and the traceback tail (plus CUDA memory, if any) to
    results/v4pilot/error.txt, and log an anchored line."""
    OUT.mkdir(parents=True, exist_ok=True)
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tail = "".join(tb).splitlines()[-80:]
    mem = ""
    try:
        import torch

        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            mem = (f"cuda: allocated {torch.cuda.memory_allocated() / 1e9:.1f} GB, peak "
                   f"{torch.cuda.max_memory_allocated() / 1e9:.1f} GB, free {free / 1e9:.1f} / "
                   f"{total / 1e9:.1f} GB\n")
    except Exception as e:  # noqa: BLE001 - diagnostics must not raise
        mem = f"cuda memory unavailable: {e!r}\n"
    with (OUT / "error.txt").open("a") as f:
        f.write(f"== {utc()} step={step}\n{exc!r}\n{mem}" + "\n".join(tail) + "\n\n")
    print(f"P06_V4PILOT_ERROR step={step} {type(exc).__name__}: {str(exc)[:300]}", flush=True)


# --------------------------------------------------------------------- items and prompts


def pick_items(path: Path = DATA / "items_chain.jsonl") -> list[dict]:
    """N_PER_K items per k, a seeded sample (one Random per k) in file order of the sample."""
    rows = I.read_jsonl(path)
    out = []
    for k, n in N_PER_K.items():
        cands = [e for e in rows if e["k"] == k]
        out += random.Random(SEED * 1000 + k).sample(cands, n)
    return out


def load_shots(path: Path = DATA / "shots_chain10.jsonl") -> list[dict]:
    return I.read_jsonl(path)[:N_SHOTS]


def render_text(it: dict, shots: list[dict], arm: str) -> str:
    return E.encode_messages(F5.messages(it, shots, arm), thinking_mode="chat")


@dataclass
class Rendered:
    text: str
    ids: list[int]
    read: np.ndarray  # positions from the target turn's label to the end (contiguous)


def render(tok: Any, it: dict, shots: list[dict], arm: str) -> Rendered:
    """Prompt ids (the text tokenized with offsets; must equal plain `encode`) and the read
    positions: the token holding the target turn's label's first character, through the end."""
    text = render_text(it, shots, arm)
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = [int(i) for i in enc["input_ids"]]
    if ids != [int(i) for i in tok.encode(text, add_special_tokens=False)]:
        raise ValueError(f"{it['item_id']}/{arm}: offset tokenization differs from encode")
    turn = text.rfind(E.USER_SP_TOKEN)
    start = text.rfind(LABELS[arm])
    if turn < 0 or start < turn:
        raise ValueError(f"{it['item_id']}/{arm}: no {LABELS[arm]!r} in the target turn")
    offsets = [(int(a), int(b)) for a, b in enc["offset_mapping"]]
    i0 = next(i for i, (s, e) in enumerate(offsets) if e > start and e > s)
    return Rendered(text, ids, np.arange(i0, len(ids)))


def int_token_ids(tok: Any) -> list[int]:
    """Ids of "0".."20": each a single token that decodes back to itself, all distinct."""
    ids = []
    for n in INT_VALUES:
        enc = tok.encode(str(n), add_special_tokens=False)
        if len(enc) != 1 or tok.decode(enc) != str(n):
            raise ValueError(f"{n} is not one token: {enc}")
        ids.append(int(enc[0]))
    if len(set(ids)) != len(ids):
        raise ValueError(f"integer ids not distinct: {ids}")
    return ids


def api_reference(item_ids: list[str], path: Path = F6_ROWS) -> dict[tuple[str, str], dict]:
    """{(item_id, arm): {correct_mean, n, draw0_raw, draw0_pred}} from F6's API rows."""
    want = set(item_ids)
    acc: dict[tuple[str, str], dict] = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r["item_id"] not in want or r["arm"] not in ARMS:
            continue
        d = acc.setdefault((r["item_id"], r["arm"]), {"c": [], "draw0_raw": None})
        d["c"].append(bool(r["correct"]))
        if r["draw"] == 0:
            d["draw0_raw"] = r["raw_text"]
    return {key: {"correct_mean": sum(d["c"]) / len(d["c"]), "n": len(d["c"]),
                  "draw0_raw": d["draw0_raw"],
                  "draw0_pred": grade_int(d["draw0_raw"] or "", 0)["predicted"]}
            for key, d in acc.items()}


# --------------------------------------------------------------------- model parts and readers


@dataclass
class Parts:
    blocks: Any
    hc_head: Any
    norm: Any
    lm_head: Any


def model_parts(model: Any) -> Parts:
    """DeepseekV4ForCausalLM: model.model.{layers, hc_head, norm}, model.lm_head."""
    inner = model.model
    return Parts(inner.layers, inner.hc_head, inner.norm, model.lm_head)


def lens_logits(parts: Parts, streams: Any) -> Any:
    """streams [P, hc, D] (one layer's output at P positions) -> logits [P, V] fp32, through the
    model's own final path: hc_head (stream collapse) -> final norm -> lm_head."""
    return parts.lm_head(parts.norm(parts.hc_head(streams.unsqueeze(0))))[0].float()


def readout(parts: Parts, hs: Any, int_ids: list[int], chunk: int = LENS_CHUNK):
    """hs [L, P, hc, D] -> (int log-probs [L, P, n_int] fp16, top-1 [L, P] int32) as numpy."""
    import torch

    idx = torch.tensor(int_ids, device=hs.device)
    lps, tops = [], []
    for layer in range(hs.shape[0]):
        lp_l, top_l = [], []
        for s in range(0, hs.shape[1], chunk):
            lg = lens_logits(parts, hs[layer, s:s + chunk])
            lp_l.append(torch.log_softmax(lg, -1).index_select(-1, idx))
            top_l.append(lg.argmax(-1))
        lps.append(torch.cat(lp_l))
        tops.append(torch.cat(top_l))
    return (torch.stack(lps).cpu().numpy().astype(np.float16),
            torch.stack(tops).cpu().numpy().astype(np.int32))


def agreement(lens_top: np.ndarray, model_top: np.ndarray) -> dict:
    """Top-1 agreement between the last layer's lens and the model, over read positions."""
    lens_top, model_top = np.asarray(lens_top), np.asarray(model_top)
    if lens_top.shape != model_top.shape:
        raise ValueError(f"shapes differ: {lens_top.shape} vs {model_top.shape}")
    n = int(lens_top.size)
    agree = int((lens_top == model_top).sum())
    return {"agree": agree, "n": n, "rate": agree / n if n else None}


class capture:  # a context manager, named like stage2.capture
    """Forward hooks keeping each block's output [B, S, hc, D] at `positions` (batch 0) on calls
    over the whole sequence (seq_len); other calls are ignored."""

    def __init__(self, blocks: Any, positions: Any, seq_len: int) -> None:
        self.blocks, self.positions, self.seq_len = blocks, positions, seq_len
        self.out: list[Any] = [None] * len(blocks)
        self.handles: list[Any] = []

    def _hook(self, i: int):
        def fn(_mod, _inp, o):
            h = o[0] if isinstance(o, tuple) else o
            if h.dim() == 4 and h.shape[1] == self.seq_len:
                self.out[i] = h[0].index_select(0, self.positions).detach().clone()
        return fn

    def __enter__(self) -> list[Any]:
        self.handles = [b.register_forward_hook(self._hook(i)) for i, b in enumerate(self.blocks)]
        return self.out

    def __exit__(self, *exc) -> None:
        for h in self.handles:
            h.remove()


def forward_read(model: Any, parts: Parts, ids: list[int], read: np.ndarray, device: str):
    """(streams [L, P, hc, D] at `read`, the model's own logits there [P, V] fp32)."""
    import torch

    n = len(read)
    if read[-1] != len(ids) - 1 or read[0] != len(ids) - n:
        raise ValueError("read positions must be the last n positions")
    t = torch.tensor([ids], device=device)
    with torch.inference_mode(), capture(parts.blocks, torch.as_tensor(read, device=device),
                                         len(ids)) as hs:
        logits = model(input_ids=t, use_cache=False, logits_to_keep=n).logits[0, -n:].float()
    if any(h is None for h in hs):
        raise RuntimeError("a decoder-layer hook captured nothing")
    return torch.stack(hs), logits


def greedy(model: Any, ids: list[int], int_ids: list[int], device: str, max_new: int = MAX_NEW):
    """(new ids, int log-probs per step [n, n_int] fp32 numpy, first-step logits [V] fp32)."""
    import torch

    t = torch.tensor([ids], device=device)
    with torch.inference_mode():
        out = model.generate(input_ids=t, attention_mask=torch.ones_like(t),
                             max_new_tokens=max_new, do_sample=False, temperature=None,
                             top_p=None, top_k=None, output_logits=True,
                             return_dict_in_generate=True)
        new = [int(x) for x in out.sequences[0, len(ids):].tolist()]
        idx = torch.tensor(int_ids, device=device)
        lp = [torch.log_softmax(lg[0].float(), -1).index_select(-1, idx) for lg in out.logits]
        lp_np = torch.stack(lp).cpu().numpy() if lp else np.zeros((0, len(int_ids)))
        first = out.logits[0][0].float() if out.logits else None
    return new, lp_np, first


# --------------------------------------------------------------------- npz schema


def npz_path(item_id: str, arm: str, root: Path | None = None) -> Path:
    return (root or READOUTS) / f"{item_id.replace(':', '_')}__{arm}.npz"


def write_npz(path: Path, rec: dict) -> None:
    """Atomic: a temporary name, then renamed. Strings and ints become 0-d arrays."""
    arrays = {k: (np.array(rec[k]) if isinstance(rec[k], (str, int, list)) else rec[k])
              for k in NPZ_KEYS}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        np.savez_compressed(f, **arrays)
    os.replace(tmp, path)


def read_npz(path: Path) -> dict:
    """The record back, with the schema and shapes checked."""
    with np.load(path, allow_pickle=False) as z:
        d = {k: z[k] for k in z.files}
    missing = [k for k in NPZ_KEYS if k not in d]
    if missing:
        raise ValueError(f"{path}: missing keys {missing}")
    rec: dict[str, Any] = dict(d)
    for k in ("item_id", "arm", "gen_text"):
        rec[k] = str(rec[k])
    for k in ("k", "answer", "n_prompt"):
        rec[k] = int(rec[k])
    n_rows, n_int = len(rec["positions"]), len(rec["int_ids"])
    lp, top = rec["ll_int_lp"], rec["ll_top1"]
    if lp.shape[1:] != (n_rows, n_int) or top.shape != lp.shape[:2]:
        raise ValueError(f"{path}: lens shapes {lp.shape} {top.shape} vs {n_rows} rows")
    if len(rec["slots"]) != n_rows or len(rec["token_ids"]) != n_rows or \
            len(rec["model_top1"]) != n_rows:
        raise ValueError(f"{path}: per-row arrays differ from {n_rows} positions")
    if len(rec["int_values"]) != n_int or rec["gen_int_lp"].shape[1:] != (n_int,):
        raise ValueError(f"{path}: integer arrays differ from {n_int} ids")
    return rec


# --------------------------------------------------------------------- load, smoke, fixes


@dataclass
class Ctx:
    model: Any
    tok: Any
    parts: Parts
    int_ids: list[int]
    device: str


def _reference_rmsnorm_forward(self, x):
    """The checkpoint's reference RMSNorm (inference/model.py @ MODEL_REV): fp32 math, output
    in the input dtype."""
    import torch

    dtype = x.dtype
    xf = x.float()
    xf = xf * torch.rsqrt(xf.pow(2).mean(-1, keepdim=True) + self.variance_epsilon)
    return (self.weight.float() * xf).to(dtype)


def patch_rmsnorm(model: Any) -> int:
    """Rebind every DeepseekV4RMSNorm's forward to the reference one; returns the count."""
    from transformers.models.deepseek_v4.modeling_deepseek_v4 import DeepseekV4RMSNorm

    n = 0
    for m in model.modules():
        if isinstance(m, DeepseekV4RMSNorm):
            m.forward = types.MethodType(_reference_rmsnorm_forward, m)
            n += 1
    return n


def _experts_eager(model: Any) -> str:
    model.set_experts_implementation("eager")
    return str(model.config._experts_implementation)


FIXES = (("rmsnorm_reference_dtype", patch_rmsnorm), ("experts_eager", _experts_eager))


def smoke_once(ctx: Ctx, ids: list[int]) -> dict:
    """A forward over `ids` and a 2-token greedy decode; raises on an error or non-finite logits."""
    import torch

    t = torch.tensor([ids], device=ctx.device)
    with torch.inference_mode():
        lg = ctx.model(input_ids=t, use_cache=False, logits_to_keep=1).logits[0, -1].float()
    if not bool(torch.isfinite(lg).all()):
        raise FloatingPointError("non-finite logits in the smoke forward")
    new, _, first = greedy(ctx.model, ids, ctx.int_ids, ctx.device, max_new=2)
    return {"fwd_top1": int(lg.argmax()), "gen_ids": new,
            "gen_first_eq_fwd": bool(first is not None and int(first.argmax()) == int(lg.argmax()))}


def smoke_with_fixes(ctx: Ctx, ids: list[int], fixes=FIXES) -> dict:
    """Run the smoke; on failure apply the next fix (cumulatively) and retry. Returns
    {attempts: [...], fixes_applied: [...], result}; re-raises when the fixes run out."""
    attempts: list[dict] = []
    applied: list[dict] = []
    pending = list(fixes)
    while True:
        try:
            res = smoke_once(ctx, ids)
            attempts.append({"fixes": [a["name"] for a in applied], "ok": True, **res})
            return {"attempts": attempts, "fixes_applied": applied, "result": res}
        except Exception as e:  # each failure is recorded, then the next fix is tried
            tail = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]
            attempts.append({"fixes": [a["name"] for a in applied], "ok": False,
                             "error": f"{type(e).__name__}: {str(e)[:400]}", "tail": tail})
            log(f"smoke failed with fixes {[a['name'] for a in applied]}: {e!r}"[:500])
            _empty_cache()
            if not pending:
                raise
            name, fn = pending.pop(0)
            applied.append({"name": name, "detail": fn(ctx.model)})
            log(f"applied fix {name}: {applied[-1]['detail']}")


def _empty_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as e:  # noqa: BLE001 - best effort between smoke attempts
        log(f"empty_cache failed: {e!r}")


def env_info() -> dict:
    out: dict[str, Any] = {"utc": utc(), "commit": os.environ.get("EMX_GIT_COMMIT"),
                           "model": f"{MODEL_ID}@{MODEL_REV}",
                           "disable_deepgemm_linear":
                               os.environ.get("TRANSFORMERS_DISABLE_DEEPGEMM_LINEAR")}
    from importlib import metadata

    for pkg in ("torch", "transformers", "triton", "kernels", "huggingface-hub", "accelerate",
                "safetensors"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    try:
        import torch

        out["cuda"] = torch.version.cuda
        if torch.cuda.is_available():
            out["gpu"] = torch.cuda.get_device_name(0)
            out["capability"] = list(torch.cuda.get_device_capability(0))
            out["arch_list"] = torch.cuda.get_arch_list()
    except Exception as e:  # noqa: BLE001
        out["torch_error"] = repr(e)[:300]
    try:
        from transformers.integrations.deepgemm import is_deepgemm_loadable

        out["deepgemm_loadable"] = bool(is_deepgemm_loadable())
    except Exception as e:  # noqa: BLE001
        out["deepgemm_loadable"] = repr(e)[:200]
    try:
        from huggingface_hub import HfApi

        out["finegrained_fp8_v4_sha"] = HfApi().model_info(
            "kernels-community/finegrained-fp8", revision="v4").sha
    except Exception as e:  # noqa: BLE001
        out["finegrained_fp8_v4_sha"] = repr(e)[:200]
    return out


def model_dir() -> Path:
    from huggingface_hub import snapshot_download

    # The download skips SNAPSHOT_IGNORE files, so the offline lookup must pass the same filter:
    # without it huggingface_hub raises IncompleteSnapshotError (F7 attempt 1, 2026-10-11).
    return Path(snapshot_download(MODEL_ID, revision=MODEL_REV, local_files_only=True,
                                  ignore_patterns=SNAPSHOT_IGNORE))


def load() -> tuple[Ctx, dict]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    path = model_dir()
    torch.manual_seed(SEED)
    torch.backends.cuda.matmul.allow_tf32 = False
    tok = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(path), dtype=getattr(torch, DTYPE), attn_implementation=ATTN, device_map="cuda",
        local_files_only=True)
    model.eval()
    parts = model_parts(model)
    if len(parts.blocks) != N_LAYERS:
        raise RuntimeError(f"{len(parts.blocks)} decoder layers, expected {N_LAYERS}")
    kinds: dict[str, int] = {}
    for m in model.modules():
        kinds[type(m).__name__] = kinds.get(type(m).__name__, 0) + 1
    free, total = torch.cuda.mem_get_info()
    info = {
        "model_class": type(model).__name__,
        "experts_implementation": str(model.config._experts_implementation),
        "attn_implementation": str(model.config._attn_implementation),
        "module_counts": {k: v for k, v in kinds.items()
                          if k.startswith(("FP8", "Linear", "DeepseekV4Experts"))},
        "dtypes": {"lm_head": str(parts.lm_head.weight.dtype),
                   "norm": str(parts.norm.weight.dtype),
                   "hc_head": str(parts.hc_head.hc_fn.dtype),
                   "embed": str(model.get_input_embeddings().weight.dtype)},
        "mem_allocated_gb": round(torch.cuda.memory_allocated() / 1e9, 2),
        "mem_peak_load_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2),
        "mem_free_after_load_gb": round(free / 1e9, 2), "mem_total_gb": round(total / 1e9, 2),
        "vocab": int(parts.lm_head.weight.shape[0]),
    }
    ctx = Ctx(model, tok, parts, int_token_ids(tok), "cuda")
    info["int_ids"] = ctx.int_ids
    return ctx, info


# --------------------------------------------------------------------- per prompt


def _sync() -> None:
    import torch

    if torch.cuda.is_available():
        torch.cuda.synchronize()


def do_prompt(ctx: Ctx, it: dict, shots: list[dict], arm: str, api: dict | None) -> dict:
    """Generate, forward with hooks, read out, write the npz; returns the prompts.jsonl row."""
    import torch

    r = render(ctx.tok, it, shots, arm)
    t0 = time.time()
    gen, gen_lp, first = greedy(ctx.model, r.ids, ctx.int_ids, ctx.device)
    _sync()
    t1 = time.time()
    hs, model_logits = forward_read(ctx.model, ctx.parts, r.ids, r.read, ctx.device)
    _sync()
    t2 = time.time()
    with torch.inference_mode():
        ll_lp, ll_top = readout(ctx.parts, hs, ctx.int_ids)
        last = torch.cat([lens_logits(ctx.parts, hs[-1, s:s + LENS_CHUNK])
                          for s in range(0, hs.shape[1], LENS_CHUNK)])
        max_diff = float((last - model_logits).abs().max())
        model_top = model_logits.argmax(-1).cpu().numpy().astype(np.int32)
        gen_eq = bool(first is not None and int(first.argmax()) == int(model_top[-1]))
        gen_diff = float((first - model_logits[-1]).abs().max()) if first is not None else None
    _sync()
    t3 = time.time()
    text = ctx.tok.decode(gen, skip_special_tokens=True)
    g = grade_int(text, it["answer"])
    write_npz(npz_path(it["item_id"], arm), {
        "item_id": it["item_id"], "k": it["k"], "arm": arm, "answer": it["answer"],
        "traj": np.array(it["traj"], dtype=np.int64), "positions": r.read.astype(np.int32),
        "slots": (r.read - len(r.ids)).astype(np.int16),
        "token_ids": np.array([r.ids[p] for p in r.read], dtype=np.int32),
        "n_prompt": len(r.ids), "gen_ids": np.array(gen, dtype=np.int32), "gen_text": text,
        "gen_int_lp": np.asarray(gen_lp, dtype=np.float16),
        "int_ids": np.array(ctx.int_ids, dtype=np.int32),
        "int_values": np.array(INT_VALUES, dtype=np.int32), "model_top1": model_top,
        "ll_int_lp": ll_lp, "ll_top1": ll_top})
    t4 = time.time()
    agree = agreement(ll_top[-1], model_top)
    ref = (api or {}).get((it["item_id"], arm), {})
    del hs, model_logits, last
    return {
        "item_id": it["item_id"], "k": it["k"], "arm": arm, "answer": it["answer"],
        "n_prompt": len(r.ids), "n_read": len(r.read), "gen_ids": gen, "gen_text": text,
        "predicted": g["predicted"], "correct": g["correct"],
        "api_correct_mean": ref.get("correct_mean"), "api_draw0_raw": ref.get("draw0_raw"),
        "api_draw0_pred": ref.get("draw0_pred"),
        "read_agree": agree["agree"], "read_n": agree["n"],
        "final_agree": bool(ll_top[-1][-1] == model_top[-1]),
        "max_logit_diff": round(max_diff, 5), "gen_first_eq_fwd": gen_eq,
        "gen_first_max_diff": None if gen_diff is None else round(gen_diff, 5),
        "gen_s": round(t1 - t0, 3), "fwd_s": round(t2 - t1, 3), "read_s": round(t3 - t2, 3),
        "write_s": round(t4 - t3, 3), "utc": utc(),
    }


def uplift_items(path: Path = DATA / "items_chain.jsonl") -> list[dict]:
    """Every chain item at k = UPLIFT_K, sorted by item_id."""
    return sorted((e for e in I.read_jsonl(path) if e["k"] == UPLIFT_K),
                  key=lambda e: e["item_id"])


def gen_only(ctx: Ctx, it: dict, shots: list[dict], arm: str) -> dict:
    """Greedy decode only (no capture, no npz), one prompt at a time: no batching, because V4's
    compressors pool fixed windows of raw sequence positions (`kv[:, :usable]` in
    DeepseekV4HCACompressor / CSACompressor) without the attention mask, so left padding would
    enter the compressed KV and change the outputs."""
    ids = render(ctx.tok, it, shots, arm).ids
    t0 = time.time()
    gen, _, _ = greedy(ctx.model, ids, ctx.int_ids, ctx.device)
    _sync()
    text = ctx.tok.decode(gen, skip_special_tokens=True)
    g = grade_int(text, it["answer"])
    return {"item_id": it["item_id"], "k": it["k"], "arm": arm, "answer": it["answer"],
            "n_prompt": len(ids), "gen_ids": gen, "gen_text": text, "predicted": g["predicted"],
            "correct": g["correct"], "gen_s": round(time.time() - t0, 3), "utc": utc()}


def run_uplift(ctx: Ctx, shots: list[dict], st: dict, rows: list[dict], urows: list[dict],
               deadline: float | None, limit: int = 0) -> None:
    """The local-uplift part: dots500 and pre500 per item (paired, item by item), then base for
    every item if the readout part's base generate time projects it under BASE_MAX_EXTRA_S."""
    its = uplift_items()
    if limit:
        its = its[:limit]
    base_s = _mean([r["gen_s"] for r in rows if r["arm"] == "base"])
    est = None if base_s is None else base_s * len(its)
    include_base = est is not None and est < BASE_MAX_EXTRA_S
    jobs = [(it, arm) for it in its for arm in UPLIFT_ARMS]
    jobs += [(it, "base") for it in its] if include_base else []
    st["uplift_plan"] = {"n_items": len(its), "n_planned": len(jobs), "include_base": include_base,
                         "base_projected_s": None if est is None else round(est, 1)}
    for i, (it, arm) in enumerate(jobs):
        if deadline is not None and time.time() > deadline:
            st["uplift_stopped"] = f"time budget after {i} of {len(jobs)} prompts"
            log(f"P06_OVERRUN uplift {st['uplift_stopped']}")
            break
        row = gen_only(ctx, it, shots, arm)
        urows.append(row)
        append_jsonl(OUT / "uplift.jsonl", row)
        if (i + 1) % 20 == 0 or i + 1 == len(jobs):
            log(f"uplift {i + 1}/{len(jobs)} ({row['gen_s']}s/prompt)")


# --------------------------------------------------------------------- summary and gates


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def summarize(rows: list[dict], n_planned: int, setup_s: dict[str, float | None],
              urows: list[dict] | None = None, u_planned: int = 0) -> dict:
    """Gates 1-3 from the readout prompt rows, and the local-uplift gate from the generate-only
    rows (pure python except the bootstrap; tested on synthetic rows)."""
    n = sum(r["read_n"] for r in rows)
    rate = sum(r["read_agree"] for r in rows) / n if n else None
    gate1 = {"read_agree": sum(r["read_agree"] for r in rows), "read_n": n, "rate": rate,
             "min": AGREE_MIN, "pass": rate is not None and rate >= AGREE_MIN,
             "final_position_agree": sum(r["final_agree"] for r in rows),
             "max_logit_diff": max((r["max_logit_diff"] for r in rows), default=None),
             "gen_first_eq_fwd": sum(r["gen_first_eq_fwd"] for r in rows), "n_prompts": len(rows)}
    gate2: dict[str, Any] = {}
    for arm in ARMS:
        ar = [r for r in rows if r["arm"] == arm]
        both = [r for r in ar if r["api_draw0_raw"] is not None]
        gate2[arm] = {
            "n": len(ar), "local_acc": _mean([float(r["correct"]) for r in ar]),
            "api_acc": _mean([r["api_correct_mean"] for r in ar]),
            "local_eq_api_draw0": sum(r["predicted"] == r["api_draw0_pred"] for r in both),
            "n_with_api": len(both),
            "by_k": {str(k): {"n": len([r for r in ar if r["k"] == k]),
                              "local_acc": _mean([float(r["correct"]) for r in ar if r["k"] == k]),
                              "api_acc": _mean([r["api_correct_mean"] for r in ar
                                                if r["k"] == k])}
                     for k in N_PER_K},
        }
    for src in ("local_acc", "api_acc"):
        a, b = gate2["dots500"][src], gate2["pre500"][src]
        gate2[f"uplift_dots500_minus_pre500_{src}"] = None if a is None or b is None else a - b
    per_arm = {arm: {ph: _mean([r[ph] for r in rows if r["arm"] == arm])
                     for ph in ("gen_s", "fwd_s", "read_s", "write_s")} for arm in ARMS}
    per_arm_total = {arm: (sum(v for v in d.values() if v is not None) if any(
        v is not None for v in d.values()) else None) for arm, d in per_arm.items()}
    proj: dict[str, Any] = {"bulk_items": BULK_ITEMS, "bulk_transplants": BULK_TX,
                            "transplant_cost": f"{TX_FWD} dots500 forwards + {TX_GEN} decodes"}
    if all(v is not None for v in per_arm_total.values()):
        d5 = per_arm["dots500"]
        item_s = sum(per_arm_total.values())
        tx_s = TX_FWD * (d5["fwd_s"] or 0) + TX_GEN * (d5["gen_s"] or 0)
        bulk_s = BULK_ITEMS * item_s + BULK_TX * tx_s
        setup = sum(v for v in setup_s.values() if v is not None)
        hours = (bulk_s + setup) / 3600
        proj.update(item_s=round(item_s, 2), transplant_s=round(tx_s, 2),
                    bulk_h=round(bulk_s / 3600, 3), setup_h=round(setup / 3600, 3),
                    total_h_excl_boot_and_venv=round(hours, 3),
                    usd={k: round(hours * v, 2) for k, v in RATES_USD_H.items()})
    urows = urows or []
    gen_only = {arm: _mean([r["gen_s"] for r in urows if r["arm"] == arm]) for arm in ARMS}
    gate3 = {"setup_s": setup_s, "per_prompt_mean_s": per_arm, "per_prompt_total_s": per_arm_total,
             "gen_only_mean_s": gen_only, "batched_generate": False, "projection": proj}
    return {"n_prompts_done": len(rows), "n_prompts_planned": n_planned,
            "complete": len(rows) == n_planned, "gate1_lens_vs_model": gate1,
            "gate2_accuracy": gate2, "gate3_timing": gate3,
            "local_uplift": uplift_gate(urows, u_planned)}


def _boot(d: np.ndarray) -> dict:
    """Mean of paired per-item differences, item bootstrap 95% interval (N_BOOT, seed SEED):
    `p06.analyze.boot_mean_diff`'s rule, with a fresh generator."""
    n = len(d)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    bs = d[np.random.default_rng(SEED).integers(0, n, size=(N_BOOT, n))].mean(axis=1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"n": int(n), "mean": float(d.mean()), "lo": float(lo), "hi": float(hi)}


def uplift_gate(urows: list[dict], u_planned: int = 0, path: Path = F6_ROWS) -> dict:
    """Local filler uplift at k = UPLIFT_K (generate-only, greedy, one draw) next to the API's F6
    rows on the same items (status-ok rows, draws 0 and 1 averaged per item). Pass: local lower
    bound > 0 and local mean >= half the API mean on the same items."""
    from .api import latest_ok

    loc: dict[str, dict[str, float]] = {}
    pred: dict[tuple[str, str], Any] = {}
    for r in urows:
        loc.setdefault(r["item_id"], {})[r["arm"]] = float(r["correct"])
        pred[(r["item_id"], r["arm"])] = r["predicted"]
    paired = sorted(i for i, d in loc.items() if all(a in d for a in UPLIFT_ARMS))
    api: dict[str, dict[str, list[float]]] = {}
    draw0: dict[tuple[str, str], Any] = {}
    if path.exists():
        want = set(loc)
        for r in latest_ok(path).values():
            if r["item_id"] in want and r["arm"] in ARMS:
                api.setdefault(r["item_id"], {}).setdefault(r["arm"], []).append(
                    float(r["correct"]))
                if r["draw"] == 0:
                    draw0[(r["item_id"], r["arm"])] = grade_int(r["raw_text"] or "", 0)["predicted"]
    paired_api = [i for i in paired if all(a in api.get(i, {}) for a in UPLIFT_ARMS)]
    d_loc = np.array([loc[i]["dots500"] - loc[i]["pre500"] for i in paired_api])
    d_api = np.array([np.mean(api[i]["dots500"]) - np.mean(api[i]["pre500"])
                      for i in paired_api])
    local, ref = _boot(d_loc), _boot(d_api)
    arms: dict[str, Any] = {}
    for arm in ARMS:
        ids = [i for i in loc if arm in loc[i]]
        both = [i for i in ids if (i, arm) in draw0]
        arms[arm] = {"n": len(ids), "local_acc": _mean([loc[i][arm] for i in ids]),
                     "api_acc": _mean([float(np.mean(api[i][arm])) for i in ids
                                       if arm in api.get(i, {})]),
                     "local_eq_api_draw0": sum(pred[(i, arm)] == draw0[(i, arm)] for i in both),
                     "n_with_api": len(both)}
    ok = (local["n"] > 0 and ref["mean"] is not None and ref["mean"] > 0
          and local["lo"] > 0 and local["mean"] >= 0.5 * ref["mean"])
    return {"k": UPLIFT_K, "n_rows": len(urows), "n_planned": u_planned,
            "complete": len(urows) == u_planned, "n_paired": local["n"], "per_arm": arms,
            "dots500_minus_pre500_local": local, "dots500_minus_pre500_api": ref,
            "f6_reported_k2": F6_K2_REPORTED, "pass": bool(ok),
            "rule": "local lo > 0 and local mean >= 0.5 x API mean (same items)"}


# --------------------------------------------------------------------- download and run


def budget_deadline() -> float | None:
    """T0 (run.sh's start, epoch s) + probe.toml gpu_hours - SALVAGE_MARGIN_S; None without T0."""
    t0 = os.environ.get("P06_T0")
    if not t0:
        return None
    gpu_h = float(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"])
    return float(t0) + gpu_h * 3600 - SALVAGE_MARGIN_S


DOWNLOAD_TRIES = 6


def with_retries(fn, what: str, tries: int = DOWNLOAD_TRIES, sleep=time.sleep):
    """Call fn, retrying any exception up to `tries` times with a growing wait (30 s, 60 s, ...).
    snapshot_download resumes from the files already in the cache, so a retry only fetches what
    is missing. F7 attempt 2 (2026-10-12) lost a 44-minute download to one HF 504."""
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # every transport error is worth one more try
            if i == tries:
                raise
            log(f"P06_RETRY {what}: try {i}/{tries} failed ({type(e).__name__}: "
                f"{str(e)[:160]}); retrying in {30 * i} s")
            sleep(30 * i)


def cmd_download(_a) -> int:
    try:
        return _download()
    except BaseException as e:  # noqa: BLE001 - the diagnosis is the deliverable
        record_error("download", e)
        return 1


def _download() -> int:
    from huggingface_hub import hf_hub_download, snapshot_download
    from huggingface_hub.constants import HF_HUB_CACHE

    T = Timer(OUT / "timings.json")
    rec_out: dict[str, Any] = {"model": f"{MODEL_ID}@{MODEL_REV}",
                               "lens": f"{LENS_REPO}@{LENS_REV}", "cache": str(HF_HUB_CACHE)}
    Path(HF_HUB_CACHE).mkdir(parents=True, exist_ok=True)
    free0 = shutil.disk_usage(HF_HUB_CACHE).free
    rec_out["disk_free_before_gb"] = round(free0 / 1e9, 1)
    if free0 < 175e9:
        raise OSError(f"{free0 / 1e9:.0f} GB free under {HF_HUB_CACHE}; the checkpoint is 160 GB")
    with T.step("download") as rec:
        t = time.time()
        path = Path(with_retries(lambda: snapshot_download(
            MODEL_ID, revision=MODEL_REV, max_workers=16, ignore_patterns=SNAPSHOT_IGNORE),
            "model download"))
        secs = time.time() - t
        files = sorted(p for p in path.rglob("*") if p.is_file())
        nbytes = sum(p.stat().st_size for p in files)
        rec.update(bytes=nbytes, seconds=round(secs, 1))
    enc = path / "encoding" / "encoding_dsv4.py"
    enc_sha = hashlib.sha256(enc.read_bytes()).hexdigest() if enc.exists() else None
    rec_out.update(n_files=len(files), bytes=nbytes, seconds=round(secs, 1),
                   mb_per_s=round(nbytes / 1e6 / max(secs, 1e-6), 1),
                   encoding_sha256=enc_sha, encoding_matches_vendored=enc_sha == ENCODING_SHA256,
                   disk_free_after_gb=round(shutil.disk_usage(HF_HUB_CACHE).free / 1e9, 1))
    for name, f in LENS_FILES.items():  # optional: a lens failure never fails the download
        try:
            with T.step(f"download_{name}"):
                p = Path(with_retries(lambda f=f: hf_hub_download(LENS_REPO, f, revision=LENS_REV),
                                      f"lens download {f}"))
            rec_out[f"{name}_bytes"] = p.stat().st_size
        except Exception as e:  # noqa: BLE001
            rec_out[f"{name}_error"] = repr(e)[:300]
    write_json(OUT / "download.json", rec_out)
    log(f"P06_DOWNLOAD {nbytes / 1e9:.1f} GB in {secs / 60:.1f} min "
        f"({rec_out['mb_per_s']} MB/s); encoding matches vendored: "
        f"{rec_out['encoding_matches_vendored']}")
    if not rec_out["encoding_matches_vendored"]:
        raise ValueError(f"encoding_dsv4.py sha256 {enc_sha} != vendored {ENCODING_SHA256}")
    return 0


def lens_shapes() -> dict:
    """Optional: each V4 lens loaded on the CPU, shapes and metadata only (never applied)."""
    from huggingface_hub import hf_hub_download

    from .stage2 import load_lens

    out: dict[str, Any] = {}
    for name, f in LENS_FILES.items():
        try:
            p = hf_hub_download(LENS_REPO, f, revision=LENS_REV, local_files_only=True)
            maps, meta = load_lens(p)
            first = maps[min(maps)]
            out[name] = {**{k: meta[k] for k in ("n_source_layers", "d_model", "dtype",
                                                 "weights_only", "n_prompts")},
                         "map_shape": list(first.shape),
                         "matches_model": meta["d_model"] == D_MODEL
                         and meta["n_source_layers"] in (N_LAYERS - 1, N_LAYERS - 2),
                         "provenance": (meta["provenance"] or "")[:600]}
            del maps
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": repr(e)[:300]}
    return out


def cmd_run(a) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    st: dict[str, Any] = {"env": env_info(), "utc_start": utc()}
    log(f"env {json.dumps(st['env'])}")
    rows: list[dict] = []
    urows: list[dict] = []
    n_planned = 0
    setup_s: dict[str, float | None] = {"download": None, "load": None, "smoke": None}
    rc = 1
    try:
        rc, n_planned = _run(a, st, rows, setup_s, urows)
    except BaseException as e:  # noqa: BLE001 - recorded; pilot.json is still written
        record_error(st.get("step", "run"), e)
        st["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        rc = 1
    finally:
        try:
            st["summary"] = summarize(rows, n_planned, setup_s, urows,
                                      st.get("uplift_plan", {}).get("n_planned", 0))
            st["utc_end"] = utc()
            write_json(OUT / "pilot.json", st)
            _log_gates(st["summary"])
        except BaseException as e:  # noqa: BLE001
            record_error("summarize", e)
            rc = rc or 1
    log(f"P06_V4PILOT_DONE rc={rc}")
    return rc


def _log_gates(s: dict) -> None:
    g1, g2, g3 = s["gate1_lens_vs_model"], s["gate2_accuracy"], s["gate3_timing"]
    print(f"P06_GATE lens_vs_model rate={g1['rate']} n={g1['read_n']} pass={g1['pass']} "
          f"max_logit_diff={g1['max_logit_diff']}", flush=True)
    for arm in ARMS:
        print(f"P06_GATE accuracy arm={arm} local={g2[arm]['local_acc']} api={g2[arm]['api_acc']} "
              f"n={g2[arm]['n']} local_eq_api_draw0={g2[arm]['local_eq_api_draw0']}/"
              f"{g2[arm]['n_with_api']}", flush=True)
    print(f"P06_GATE timing {json.dumps(g3['projection'])}", flush=True)
    u = s["local_uplift"]
    print(f"P06_GATE local_uplift pass={u['pass']} n={u['n_paired']} "
          f"local={json.dumps(u['dots500_minus_pre500_local'])} "
          f"api={json.dumps(u['dots500_minus_pre500_api'])}", flush=True)


def _run(a, st: dict, rows: list[dict], setup_s: dict, urows: list[dict]) -> tuple[int, int]:
    import torch

    T = Timer(OUT / "timings.json")
    if T.data.get("download", {}).get("seconds") is not None:
        setup_s["download"] = T.data["download"]["seconds"]
    its, shots = pick_items(), load_shots()
    if a.limit:
        its = its[: a.limit]
    jobs = [(it, arm) for it in its for arm in ARMS]
    st["items"] = [it["item_id"] for it in its]
    try:
        api = api_reference(st["items"])
    except Exception as e:  # noqa: BLE001 - gate 2's API column is then empty
        api, st["api_error"] = None, repr(e)[:300]
    st["step"] = "load"
    with T.step("load") as rec:
        ctx, info = load()
        rec.update(info)
    setup_s["load"] = T.data["load"]["seconds"]
    st["load"] = info
    log(f"loaded: {json.dumps(info)}")
    st["step"] = "smoke"
    first = render(ctx.tok, its[0], shots, "dots500")
    with T.step("smoke") as rec:
        sm = smoke_with_fixes(ctx, first.ids[:SMOKE_TOKENS])
        rec.update(fixes=[f["name"] for f in sm["fixes_applied"]])
    setup_s["smoke"] = T.data["smoke"]["seconds"]
    st["smoke"] = sm
    st["experts_implementation_final"] = str(ctx.model.config._experts_implementation)
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    deadline = budget_deadline()
    failed = False
    st["step"] = "prompts"
    try:  # an error in the readout part is recorded; the uplift part still runs
        for i, (it, arm) in enumerate(jobs):
            if deadline is not None and time.time() > deadline:
                st["stopped"] = f"time budget after {i} of {len(jobs)} prompts"
                log(f"P06_OVERRUN {st['stopped']}")
                break
            row = do_prompt(ctx, it, shots, arm, api)
            rows.append(row)
            append_jsonl(OUT / "prompts.jsonl", row)
            log(f"prompt {i + 1}/{len(jobs)} {it['item_id']} {arm}: {row['gen_text']!r} "
                f"(answer {it['answer']}, api {row['api_draw0_raw']!r}) agree "
                f"{row['read_agree']}/{row['read_n']} gen {row['gen_s']}s fwd {row['fwd_s']}s "
                f"read {row['read_s']}s")
        if torch.cuda.is_available():
            st["mem_peak_prompts_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
        g1 = summarize(rows, len(jobs), setup_s)["gate1_lens_vs_model"]
        print(f"P06_GATE lens_vs_model rate={g1['rate']} n={g1['read_n']} pass={g1['pass']} "
              "(readout part done; the uplift part follows)", flush=True)
    except Exception as e:  # noqa: BLE001
        record_error("prompts", e)
        st["readout_error"] = f"{type(e).__name__}: {str(e)[:500]}"
        failed, g1 = True, {"pass": False}
        _empty_cache()
    st["step"] = "uplift"
    try:
        run_uplift(ctx, shots, st, rows, urows, deadline, a.limit)
    except Exception as e:  # noqa: BLE001
        record_error("uplift", e)
        st["uplift_error"] = f"{type(e).__name__}: {str(e)[:500]}"
        failed = True
    st["step"] = "lens_shapes"
    st["lens_shapes"] = lens_shapes()
    st.pop("step", None)
    if failed:
        return 1, len(jobs)
    if len(rows) < len(jobs) or len(urows) < st["uplift_plan"]["n_planned"]:
        return 4, len(jobs)
    return (0 if g1["pass"] else 3), len(jobs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p06.v4pilot")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N items (tests)")
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
