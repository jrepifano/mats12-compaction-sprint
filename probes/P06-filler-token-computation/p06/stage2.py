"""P06 follow-up F3 (GPU): Gemma 4 31B-it on Brauer et al.'s system-of-equations task (depth 1).
Local accuracy per arm, logit-lens / J-Lens / J++ readouts after the target turn's "Filler:"
label, and a filler-KV transplant (log.md F3; plan "Likely follow-ups").

    python -m p06.stage2 download      # model @ MODEL_REV + both lenses @ LENS_REV (named files)
    python -m p06.stage2 run           # load, smoke (gates the bulk), bulk; resumable

One H100 80GB, bf16, HF transformers (pod setup target "i1a": the pod-i1a group, transformers
5.17.0, the stack I9 loaded this pin with; `emx.i9.acts.load_backend`). No vLLM.

**Items.** The first N_ITEMS examples by idx of Brauer's depth-1 set (`p06.brauer.load(1)`,
data/P06/chained_var_binding_dataset.json at the repo root, gitignored, sent through
probe.toml [pod].push), with its 5 shots.

**Prompt.** The chat template over `brauer.messages(ex, shots, arm)` with
`add_generation_prompt=True` (no prefill; Gemma 4's empty thought channel opens the model turn).
Checked 2026-10-11 against F1's API rows: Crusoe-served rows have exactly this many prompt
tokens; Novita-served rows report one more (origin unknown; Novita only, system-message prompts
only). Arms: base, pre100, dots100 (`brauer.ARMS`).

**Per item** (see `do_item`):
1. Each arm: greedy decode up to MAX_NEW tokens, graded with nocot's integer parser.
2. dots100 and base: one forward over the prompt; forward hooks keep the 60 decoder blocks'
   outputs at the read positions: from the target turn's "Filler" label (dots100) or its
   "\\n\\nAnswer:" (base) through the last prompt token. Slots are offsets from the end of the
   prompt (-1 = the last token, which predicts the answer's first digit), so base's 12 slots
   line up with dots100's last 12. Readers at every (layer, read position): the logit lens (final
   norm, the tied LM head through `F.linear` in bf16 as the model runs it, Gemma 4's final logit
   softcap of 30.0, fp32 log-softmax over the full vocabulary); the J-Lens and J++ from
   neuronpedia/jacobian-lens @ LENS_REV (both fit on the base model google/gemma-4-31B, fp16
   maps from layers 0..58 to block 59), read as its README says, `unembed(h @ J[l].T)`; a lens
   at layer 59 is the identity. Stored per reader: the log-probs of the digit tokens "0".."9"
   (Gemma 4 splits numbers into digits) as float16 [layer, row, 10] and the top-1 id as int32
   [layer, row]; one compressed .npz per (item, arm) under results/stage2/readouts/.
3. Transplant, dots100 (Brauer et al. 2026 App. C, §4.3). Donor = the item with the literal x
   that y's definition refers to replaced by another 2-digit value (seeded) such that y and the
   answer change and stay non-negative; donor and target token sequences must differ only at the
   literal's digit tokens. The donor's K/V at the target turn's 100 filler-dot positions replace
   the target's at every layer, then greedy decoding. Implemented as forward hooks on every
   attention block's k_proj / v_proj that overwrite those positions' outputs with the donor's
   during the target's prefill (identical positions, so RoPE and the K/V norms then produce
   exactly the donor's cached K/V; the decode steps read the spliced cache). This avoids indexing
   Gemma 4's sliding-window cache directly. Controls: self-transplant (donor = target; must
   change nothing) and a random-donor transplant (another example's filler K/V, mapped dot for
   dot onto the target's filler positions: a disruption baseline).

**Smoke** (the first SMOKE_ITEMS items, kept as bulk data): the final-layer logit lens agrees with
the model's own logits (top-1 at every read position of the smoke read passes) >= AGREE_MIN;
self-transplant changes no output; the warm item's wall-clock projects the bulk within the
budget (probe.toml gpu_hours minus SETUP_ALLOWANCE_S). A failed smoke returns 3. The bulk stops
itself at OVERRUN x its projection (CLAUDE.md "Profile before bulk work").

Reused: `emx.i1a.hfget` (named-file, size-checked downloads), I9's model-file rule
(scripts/i9_pod.py `_model_files`) and Gemma 4 loading (`emx.i9.acts.load_backend`); the block and
norm paths follow wsbench `Backend.blocks` / `final_norm`
(third_party/workspace-bench/src/wsbench/produce/backend.py); the readout arithmetic follows
`emx.i1a.token_readers._scores` (logit lens) and the jacobian-lens README (J-Lens, J++), with
the softcap added because Gemma 4 has one. torch/transformers are imported inside functions, so
the laptop tests import this module without a GPU.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import brauer as B
from .nocot import grade as G

PROBE = Path(__file__).resolve().parents[1]
RESULTS = PROBE / "results"
OUT = RESULTS / "stage2"
READOUTS = OUT / "readouts"
TIMINGS = RESULTS / "timings.json"

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"  # I9's pin (emx.i9.contracts.GEMMA_REV)
N_LAYERS = 60
D_MODEL = 5376
LENS_REPO = "neuronpedia/jacobian-lens"
LENS_REV = "9fa267f841d62aa468dab5b9b38c3dc45a82d863"
LENS_FILES = {
    "jlens": "gemma-4-31b/jlens/Salesforce-wikitext/gemma-4-31B_jacobian_lens.pt",
    "jpp": "gemma-4-31b/jpp/Salesforce-wikitext/gemma-4-31B_jpp_lens.pt",
}
READERS = ("logit_lens", "jlens", "jpp")
ARMS = ("base", "pre100", "dots100")
READ_ARMS = ("dots100", "base")
TX_ARM = "dots100"
N_ITEMS = 150
ROLES = ("x", "c1x", "y", "c2y", "answer")  # Brauer's five chain values
DIGITS = "0123456789"
MAX_NEW = 8
SEED = 0
DTYPE = "bfloat16"
ATTN = "sdpa"  # as I9 and I1a (emx.i1a.contracts.ATTN_IMPLEMENTATION)
SMOKE_ITEMS = 3
AGREE_MIN = 0.99
SETUP_ALLOWANCE_S = 45 * 60  # boot, push, venv setup, download and load
OVERRUN = 1.5
LENS_GPU_HEADROOM = 16e9  # keep the lenses on the GPU only when this much stays free after them
NPZ_KEYS = (
    "item_id", "idx", "arm", "positions", "slots", "token_ids", "n_prompt", "gen_ids",
    "gen_text", "gen_digit_lp", "digit_ids", "roles", "values", "readers",
)


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[p06.stage2 {utc()}] {msg}", flush=True)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


class Timer:
    """Per-phase wall-clock in results/timings.json, with anchored `P06_STEP` log lines."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def note(self, key: str, value: Any) -> None:
        self.data[key] = value
        write_json(self.path, self.data)

    @contextmanager
    def step(self, name: str):
        key, n = name, 2
        while key in self.data:
            key, n = f"{name}#{n}", n + 1
        rec: dict[str, Any] = {"start": utc()}
        t = time.time()
        print(f"P06_STEP {name} start {rec['start']}", flush=True)
        try:
            yield rec
        except BaseException:
            rec.update(end=utc(), seconds=round(time.time() - t, 2), failed=True)
            self.note(key, rec)
            print(f"P06_STEP {name} failed {rec['end']}", flush=True)
            raise
        rec.update(end=utc(), seconds=round(time.time() - t, 2))
        self.note(key, rec)
        print(f"P06_STEP {name} done {rec['end']} {rec['seconds']}", flush=True)


# --------------------------------------------------------------------- task values and donors


def chain_values(ex: dict) -> dict[str, Any]:
    """Brauer's five values plus the referenced variable: x (the literal y's definition refers
    to), c1*x, y (queried_value), c2*y, answer."""
    defs = dict(ex["definitions"])
    m = B._EXPR.match(defs[ex["queried_term"]])
    c1, ref = B._COEF[m.group(1)], m.group(2)
    x = defs[ref]
    if not isinstance(x, int):
        raise TypeError(f"idx {ex['idx']}: {ref} is not a literal")
    vals = B.values(ex)
    y, c2 = vals[ex["queried_term"]], ex["coefficient"]
    if y != ex["queried_value"]:
        raise ValueError(f"idx {ex['idx']}: resolved y {y} != queried_value")
    ans = c2 * y + ex["constant"] if ex["operation"] == "plus" else c2 * y - ex["constant"]
    if ans != ex["answer"]:
        raise ValueError(f"idx {ex['idx']}: resolved answer {ans} != {ex['answer']}")
    return {"ref": ref, "x": x, "c1x": c1 * x, "y": y, "c2y": c2 * y, "answer": ans}


def with_literal(ex: dict, ref: str, new: int) -> dict:
    """The example with variable `ref`'s literal replaced; y, the answer (and any other variable
    built on `ref`) recomputed."""
    d = copy.deepcopy(ex)
    d["definitions"] = [[n, new if n == ref else v] for n, v in ex["definitions"]]
    vals = B.values(d)
    d["queried_value"] = vals[d["queried_term"]]
    y, c2, k = d["queried_value"], d["coefficient"], d["constant"]
    d["answer"] = c2 * y + k if d["operation"] == "plus" else c2 * y - k
    return d


def make_donor(ex: dict, seed: int = SEED) -> dict:
    """The matched donor (seeded per idx): x replaced by another value with the same digit
    count such that y and the answer change and stay non-negative."""
    cv = chain_values(ex)
    x = cv["x"]
    lo, hi = 10 ** (len(str(x)) - 1), 10 ** len(str(x)) - 1
    cands = [v for v in range(lo, hi + 1) if v != x]
    random.Random(seed * 100_003 + ex["idx"]).shuffle(cands)
    for v in cands:
        d = with_literal(ex, cv["ref"], v)
        if d["queried_value"] != cv["y"] and d["answer"] != cv["answer"] and \
                d["queried_value"] >= 0 and d["answer"] >= 0:
            return {**d, "donor_of": ex["idx"]}
    raise ValueError(f"idx {ex['idx']}: no donor literal")


def random_donor(ex: dict, pool: list[dict], seed: int = SEED) -> dict:
    """Another example (seeded per idx): the unmatched disruption control."""
    others = [e for e in pool if e["idx"] != ex["idx"]]
    return random.Random(seed * 7_919 + ex["idx"]).choice(others)


def grade_int(text: str, answer: int) -> dict:
    g = G.grade(text, {"answer": answer, "answer_type": "int"})
    return {"predicted": g["predicted"], "correct": bool(g["is_correct"])}


# --------------------------------------------------------------------- prompt


@dataclass
class Rendered:
    ids: list[int]
    read: np.ndarray  # read positions (sorted, contiguous to the end)
    dots: np.ndarray  # the target turn's filler-dot positions (empty without after-filler)
    literal: np.ndarray  # the target turn's tokens of the literal x (its digits)


def _tokens_in(offsets: list[tuple[int, int]], a: int, b: int) -> list[int]:
    return [i for i, (s, e) in enumerate(offsets) if e > s and a <= s and e <= b]


def render(tok: Any, ex: dict, shots: list[dict], arm: str) -> Rendered:
    """The prompt ids and the positions the pass needs. The ids come from tokenizing the
    rendered text with offsets and must equal the template's own tokenization."""
    msgs = B.messages(ex, shots, arm)
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = [int(i) for i in enc["input_ids"]]
    ref = tok.apply_chat_template(msgs, tokenize=True, return_dict=True,
                                  add_generation_prompt=True)["input_ids"]
    if ids != [int(i) for i in ref]:
        raise ValueError(f"idx {ex['idx']}/{arm}: text tokenization differs from the template's")
    offsets = [(int(a), int(b)) for a, b in enc["offset_mapping"]]
    turn = text.rfind("<|turn>user")
    where, k = B.ARMS[arm]
    dots: list[int] = []
    if k and where == "after":
        a = text.rfind("Filler: ")
        if a < turn:
            raise ValueError(f"idx {ex['idx']}/{arm}: no Filler label in the target turn")
        a += len("Filler:")  # each dot token is "▁." and carries the space before it
        dots = _tokens_in(offsets, a, a + 1 + len(B.dots(k)))
        if len(dots) != k or dots != list(range(dots[0], dots[0] + k)):
            raise ValueError(f"idx {ex['idx']}/{arm}: {len(dots)} filler tokens, expected {k}")
        start = text.rfind("Filler: ")
    else:
        start = text.rfind("\n\nAnswer:")
    i0 = next(i for i, (s, e) in enumerate(offsets) if e > s >= start)
    cv = chain_values(ex)
    line = f"{cv['ref']} = {cv['x']}\n"
    p = text.find(line, turn)
    if p < 0:
        raise ValueError(f"idx {ex['idx']}/{arm}: literal line {line!r} not in the target turn")
    a = p + len(cv["ref"]) + 3
    lit = _tokens_in(offsets, a, a + len(str(cv["x"])))
    if len(lit) != len(str(cv["x"])):
        raise ValueError(f"idx {ex['idx']}/{arm}: literal {cv['x']} is {len(lit)} tokens")
    return Rendered(ids, np.arange(i0, len(ids)), np.array(dots, dtype=np.int64),
                    np.array(lit, dtype=np.int64))


def check_donor(target: Rendered, donor: Rendered) -> list[int]:
    """The donor's prompt differs from the target's only at the literal's tokens."""
    if len(target.ids) != len(donor.ids):
        raise ValueError(f"donor length {len(donor.ids)} != target {len(target.ids)}")
    diff = [i for i, (a, b) in enumerate(zip(target.ids, donor.ids, strict=True)) if a != b]
    if not diff or not set(diff) <= set(target.literal.tolist()):
        raise ValueError(f"donor differs at {diff}, literal tokens {target.literal.tolist()}")
    if not np.array_equal(target.dots, donor.dots):
        raise ValueError("donor filler positions differ")
    return diff


def digit_token_ids(tok: Any) -> list[int]:
    """Ids of "0".."9", each a single token. Checks that " 7" is "▁" + "7" (Gemma 4 has no
    leading-space digit tokens, so these 10 are the whole digit readout)."""
    ids = []
    for d in DIGITS:
        enc = tok.encode(d, add_special_tokens=False)
        if len(enc) != 1:
            raise ValueError(f"digit {d!r} is {len(enc)} tokens: {enc}")
        ids.append(int(enc[0]))
        sp = tok.encode(" " + d, add_special_tokens=False)
        if len(sp) != 2 or sp[1] != enc[0]:
            raise ValueError(f"' {d}' encodes as {sp}, expected [space, {enc[0]}]")
    if len(set(ids)) != 10:
        raise ValueError(f"digit ids not distinct: {ids}")
    return ids


# --------------------------------------------------------------------- model parts and readers


def text_parts(model: Any) -> tuple[Any, Any]:
    """(decoder blocks, final norm) of the text model: wsbench Backend's paths, which resolve
    Gemma4ForConditionalGeneration to model.language_model.{layers,norm} (emx.i9.acts)."""
    for path in ("model.language_model", "model", "base_model.model.model"):
        obj = model
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj.layers, obj.norm
        except AttributeError:
            continue
    raise AttributeError("no decoder blocks / final norm found")


def kv_modules(blocks: Any) -> list[Any]:
    """Every block's self_attn.k_proj and self_attn.v_proj that exist (Gemma 4's full-attention
    layers share K and V, attention_k_eq_v, and may have no v_proj)."""
    mods = []
    for b in blocks:
        for name in ("k_proj", "v_proj"):
            m = getattr(b.self_attn, name, None)
            if m is not None:
                mods.append(m)
    if len(mods) < len(blocks):
        raise RuntimeError(f"{len(mods)} K/V projections for {len(blocks)} blocks")
    return mods


def softcap_of(model: Any) -> float | None:
    cfg = model.config
    cfg = cfg.get_text_config() if hasattr(cfg, "get_text_config") else cfg
    cap = getattr(cfg, "final_logit_softcapping", None)
    return float(cap) if cap else None


def unembed_readout(x: Any, norm: Any, w_u: Any, softcap: float | None, digit_ids: list[int],
                    chunk: int = 256) -> tuple[Any, Any, Any]:
    """x [P, d] (model dtype) -> (digit log-probs [P, 10] fp32, top-1 ids [P], logits [P, V]
    fp32 of the last chunk only, for checks). final norm -> F.linear(W_U) in the model dtype (as
    the LM head runs) -> softcap -> log-softmax in fp32 over the full vocabulary."""
    import torch
    import torch.nn.functional as F

    didx = torch.tensor(digit_ids, device=x.device)
    lps, tops, logits = [], [], None
    for s in range(0, x.shape[0], chunk):
        z = norm(x[s:s + chunk])
        logits = F.linear(z, w_u).float()
        if softcap:
            logits = softcap * torch.tanh(logits / softcap)
        lp = torch.log_softmax(logits, dim=-1)
        lps.append(lp.index_select(-1, didx))
        tops.append(logits.argmax(-1))
    return torch.cat(lps), torch.cat(tops), logits


def load_lens(path: str | Path) -> tuple[dict[int, Any], dict]:
    """{layer: fp16 [d, d] map to the final block's residual} and metadata, from a jacobian-lens
    .pt (README "File format": keys J, source_layers, d_model, n_prompts, provenance). A plain
    `jacobians` stack is accepted too (emx.i1a.token_readers.load_jacobians_local's rule)."""
    import torch

    try:
        obj = torch.load(str(path), map_location="cpu", weights_only=True)
        weights_only = True
    except Exception:  # noqa: BLE001 - the Anthropic-library jlens files may pickle more
        obj = torch.load(str(path), map_location="cpu", weights_only=False)
        weights_only = False
    jac = obj["J"] if "J" in obj else obj["jacobians"]
    if isinstance(jac, dict):
        maps = {int(k): v for k, v in jac.items()}
    else:
        maps = {i: jac[i] for i in range(jac.shape[0])}
    layers = sorted(maps)
    if layers != list(range(len(layers))):
        raise ValueError(f"{path}: source layers not contiguous from 0: {layers[:5]}...")
    d = int(next(iter(maps.values())).shape[0])
    meta = {"path": str(path), "n_source_layers": len(layers), "d_model": d,
            "dtype": str(next(iter(maps.values())).dtype), "weights_only": weights_only,
            "n_prompts": obj.get("n_prompts") if isinstance(obj, dict) else None,
            "provenance": str(obj.get("provenance"))[:2000] if isinstance(obj, dict) else None}
    return maps, meta


class Lens:
    """A J-Lens map bank, on the GPU when it fits (LENS_GPU_HEADROOM), else pinned CPU memory
    copied in one layer at a time."""

    def __init__(self, maps: dict[int, Any], device: str, resident: bool) -> None:
        self.layers = sorted(maps)
        self.resident = resident
        if resident:
            self.maps = {k: v.to(device) for k, v in maps.items()}
        else:
            pin = device.startswith("cuda")
            self.maps = {k: (v.pin_memory() if pin else v) for k, v in maps.items()}
        self.device = device

    def apply(self, h: Any, layer: int) -> Any:
        """h [P, d] -> h @ J[layer].T in fp32, returned in h's dtype."""
        j = self.maps[layer].to(self.device, non_blocking=True).float()
        return (h.float() @ j.T).to(h.dtype)


@contextmanager
def capture(mods: Any, positions: Any, seq_len: int):
    """Forward hooks keeping each module's output at `positions` (batch 0) for calls over the
    full sequence (seq_len); other calls (cached decode steps) are ignored."""
    out: list[Any] = [None] * len(mods)

    def hook_for(i: int):
        def fn(_mod, _inp, o):
            h = o[0] if isinstance(o, tuple) else o
            if h.shape[1] == seq_len:
                out[i] = h[0].index_select(0, positions).detach().clone()
        return fn

    handles = [m.register_forward_hook(hook_for(i)) for i, m in enumerate(mods)]
    try:
        yield out
    finally:
        for h in handles:
            h.remove()


@contextmanager
def inject(mods: Any, positions: Any, stored: list[Any], seq_len: int):
    """Forward hooks overwriting each module's output at `positions` with `stored[i]` on calls
    over the full sequence (the prefill); yields the per-module fire counts."""
    fired = [0] * len(mods)

    def hook_for(i: int):
        def fn(_mod, _inp, o):
            if o.shape[1] != seq_len:
                return None
            o = o.clone()
            o[0, positions] = stored[i].to(o.dtype)
            fired[i] += 1
            return o
        return fn

    handles = [m.register_forward_hook(hook_for(i)) for i, m in enumerate(mods)]
    try:
        yield fired
    finally:
        for h in handles:
            h.remove()


@dataclass
class Ctx:
    model: Any
    tok: Any
    blocks: Any
    norm: Any
    w_u: Any
    softcap: float | None
    digit_ids: list[int]
    lenses: dict[str, Lens]
    device: str

    @property
    def kv(self) -> list[Any]:
        return kv_modules(self.blocks)


def greedy(ctx: Ctx, ids: list[int]) -> tuple[list[int], Any]:
    """(new token ids, [n_new, 10] digit log-probs of the model's own softcapped logits)."""
    import torch

    t = torch.tensor([ids], device=ctx.device)
    with torch.inference_mode():
        out = ctx.model.generate(input_ids=t, attention_mask=torch.ones_like(t),
                                 max_new_tokens=MAX_NEW, do_sample=False, output_logits=True,
                                 return_dict_in_generate=True)
        new = [int(x) for x in out.sequences[0, len(ids):].tolist()]
        didx = torch.tensor(ctx.digit_ids, device=ctx.device)
        lp = [torch.log_softmax(lg[0].float(), -1).index_select(-1, didx) for lg in out.logits]
        lp = torch.stack(lp) if lp else torch.zeros((0, 10), device=ctx.device)
    return new, lp


def decode(ctx: Ctx, ids: list[int]) -> tuple[str, list[int], Any]:
    gen, lp = greedy(ctx, ids)
    return ctx.tok.decode(gen, skip_special_tokens=True), gen, lp


def forward_read(ctx: Ctx, ids: list[int], positions: np.ndarray):
    """(block outputs [L, P, d] at `positions`, the model's own logits there [P, V]); the
    positions must run contiguously to the end of the sequence."""
    import torch

    n_keep = len(positions)
    if positions[-1] != len(ids) - 1 or positions[0] != len(ids) - n_keep:
        raise ValueError("read positions must be the last n positions")
    t = torch.tensor([ids], device=ctx.device)
    pos = torch.as_tensor(positions, device=ctx.device)
    with capture(ctx.blocks, pos, len(ids)) as hs:
        try:
            out = ctx.model(input_ids=t, use_cache=False, logits_to_keep=n_keep)
            logits = out.logits[0]
        except TypeError:  # a forward without logits_to_keep
            out = ctx.model(input_ids=t, use_cache=False)
            logits = out.logits[0, -n_keep:]
    if any(h is None for h in hs):
        raise RuntimeError("a block hook captured nothing")
    return torch.stack(hs), logits[-n_keep:].float()


def readouts(ctx: Ctx, hs: Any) -> dict[str, dict[str, np.ndarray]]:
    """Every reader at every (layer, row): {reader: {digit_lp [L, P, 10] f16, top1 [L, P] i32}}."""
    import torch

    out: dict[str, dict[str, np.ndarray]] = {}
    with torch.inference_mode():
        for name in ("logit_lens", *ctx.lenses):
            out[name] = _reader_arrays(ctx, hs, name)
    return out


def _reader_arrays(ctx: Ctx, hs: Any, name: str) -> dict[str, np.ndarray]:
    """One reader over every layer. A lens's layer without a map (59, the target block) is the
    identity, i.e. the logit lens."""
    import torch

    lp_all, top_all = [], []
    for layer in range(hs.shape[0]):
        h = hs[layer]
        if name != "logit_lens" and layer in ctx.lenses[name].maps:
            h = ctx.lenses[name].apply(h, layer)
        lp, top, _ = unembed_readout(h, ctx.norm, ctx.w_u, ctx.softcap, ctx.digit_ids)
        lp_all.append(lp)
        top_all.append(top)
    return {"digit_lp": torch.stack(lp_all).cpu().numpy().astype(np.float16),
            "top1": torch.stack(top_all).cpu().numpy().astype(np.int32)}


def read_pass(ctx: Ctx, ex: dict, arm: str, r: Rendered, gen: list[int], gen_text: str,
              gen_lp: Any) -> tuple[dict, dict]:
    """Readouts for one (item, arm): (npz record, lens-vs-model agreement metrics)."""
    import torch

    with torch.inference_mode():
        hs, model_logits = forward_read(ctx, r.ids, r.read)
        ro = readouts(ctx, hs)
        _, _, lens_logits = unembed_readout(hs[-1], ctx.norm, ctx.w_u, ctx.softcap,
                                            ctx.digit_ids, chunk=hs.shape[1])
        max_diff = float((lens_logits - model_logits).abs().max())
    model_top = model_logits.argmax(-1).cpu().numpy()
    lens_top = ro["logit_lens"]["top1"][-1]
    cv = chain_values(ex)
    rec = {
        "item_id": f"varbind:{ex['idx']}", "idx": ex["idx"], "arm": arm,
        "positions": r.read.astype(np.int32),
        "slots": (r.read - len(r.ids)).astype(np.int16),
        "token_ids": np.array([r.ids[p] for p in r.read], dtype=np.int32),
        "n_prompt": len(r.ids), "gen_ids": np.array(gen, dtype=np.int32), "gen_text": gen_text,
        "gen_digit_lp": gen_lp.cpu().numpy().astype(np.float16),
        "digit_ids": np.array(ctx.digit_ids, dtype=np.int32), "roles": list(ROLES),
        "values": np.array([cv[k] for k in ROLES], dtype=np.int64), "readers": list(ro),
        "ro": ro,
    }
    agree = {"read_agree": int((lens_top == model_top).sum()), "read_n": len(model_top),
             "final_agree": bool(lens_top[-1] == model_top[-1]),
             "max_logit_diff": round(max_diff, 4),
             "final_eq_gen": bool(gen and int(lens_top[-1]) == gen[0])}
    return rec, agree


def transplant(ctx: Ctx, target: Rendered, donor_ids: list[int], donor_pos: np.ndarray):
    """Greedy decode of the target with the donor's K/V at the target's filler-dot positions
    (from the donor's positions `donor_pos`, dot for dot) at every layer: (text, ids, digit lp)."""
    import torch

    mods = ctx.kv
    with torch.inference_mode():
        t = torch.tensor([donor_ids], device=ctx.device)
        with capture(mods, torch.as_tensor(donor_pos, device=ctx.device), len(donor_ids)) as kv:
            ctx.model(input_ids=t, use_cache=False, logits_to_keep=1)
        if any(x is None for x in kv):
            raise RuntimeError("a K/V hook captured nothing")
        tpos = torch.as_tensor(target.dots, device=ctx.device)
        with inject(mods, tpos, kv, len(target.ids)) as fired:
            text, gen, lp = decode(ctx, target.ids)
    if fired != [1] * len(mods):
        raise RuntimeError(f"K/V injection fired {sorted(set(fired))} times per module, not once")
    return text, gen, lp


# --------------------------------------------------------------------- npz schema


def npz_path(item_id: str, arm: str, root: Path | None = None) -> Path:
    return (root or READOUTS) / f"{item_id.replace(':', '_')}__{arm}.npz"


def write_npz(path: Path, rec: dict) -> None:
    """Atomic: written to a temporary name, then renamed."""
    arrays: dict[str, Any] = {}
    for k in NPZ_KEYS:
        v = rec[k]
        arrays[k] = np.array(v) if isinstance(v, (str, int, list)) else v
    for name, d in rec["ro"].items():
        arrays[f"{name}__digit_lp"] = d["digit_lp"]
        arrays[f"{name}__top1"] = d["top1"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        np.savez_compressed(f, **arrays)
    os.replace(tmp, path)


def read_npz(path: Path) -> dict:
    """The record back, with `ro` rebuilt and the schema checked."""
    with np.load(path, allow_pickle=False) as z:
        d = {k: z[k] for k in z.files}
    missing = [k for k in NPZ_KEYS if k not in d]
    if missing:
        raise ValueError(f"{path}: missing keys {missing}")
    rec: dict[str, Any] = {k: d[k] for k in NPZ_KEYS}
    for k in ("item_id", "arm", "gen_text"):
        rec[k] = str(rec[k])
    for k in ("idx", "n_prompt"):
        rec[k] = int(rec[k])
    for k in ("roles", "readers"):
        rec[k] = [str(x) for x in rec[k]]
    rec["values"] = {r: int(v) for r, v in zip(rec["roles"], rec["values"], strict=True)}
    rec["ro"] = {}
    n_rows = len(rec["positions"])
    for name in rec["readers"]:
        lp, top = d[f"{name}__digit_lp"], d[f"{name}__top1"]
        if lp.shape[1:] != (n_rows, 10) or top.shape != lp.shape[:2]:
            raise ValueError(f"{path}: {name} shapes {lp.shape} {top.shape} vs {n_rows} rows")
        rec["ro"][name] = {"digit_lp": lp, "top1": top}
    if len(rec["slots"]) != n_rows or len(rec["token_ids"]) != n_rows:
        raise ValueError(f"{path}: slots/token_ids length differs from positions")
    return rec


# --------------------------------------------------------------------- per item


def do_item(ctx: Ctx, ex: dict, shots: list[dict], pool: list[dict]) -> dict:
    """All passes for one item; appends answers.jsonl (one row per arm), transplant.jsonl (one
    row) and writes the read arms' .npz files. Returns metrics for the smoke gate."""
    item_id = f"varbind:{ex['idx']}"
    rend: dict[str, Rendered] = {}
    gens: dict[str, tuple[str, list[int], Any]] = {}
    metrics: dict[str, Any] = {"read_agree": 0, "read_n": 0, "max_logit_diff": 0.0}
    for arm in ARMS:
        rend[arm] = r = render(ctx.tok, ex, shots, arm)
        t0 = time.time()
        gens[arm] = text, gen, lp = decode(ctx, r.ids)
        row = {"item_id": item_id, "idx": ex["idx"], "arm": arm, "answer": ex["answer"],
               "gen_text": text, "gen_ids": gen, **grade_int(text, ex["answer"]),
               "n_prompt": len(r.ids)}
        if arm in READ_ARMS:
            rec, agree = read_pass(ctx, ex, arm, r, gen, text, lp)
            write_npz(npz_path(item_id, arm), rec)
            row.update(agree)
            metrics["read_agree"] += agree["read_agree"]
            metrics["read_n"] += agree["read_n"]
            metrics["max_logit_diff"] = max(metrics["max_logit_diff"], agree["max_logit_diff"])
        row.update(seconds=round(time.time() - t0, 3), utc=utc())
        append_jsonl(OUT / "answers.jsonl", row)

    t0 = time.time()
    tgt = rend[TX_ARM]
    target_text = gens[TX_ARM][0]
    donor = make_donor(ex)
    rd = render(ctx.tok, donor, shots, TX_ARM)
    diff = check_donor(tgt, rd)
    donor_text, _, _ = decode(ctx, rd.ids)
    tx_text, _, tx_lp = transplant(ctx, tgt, rd.ids, rd.dots)
    self_text, _, _ = transplant(ctx, tgt, tgt.ids, tgt.dots)
    rand = random_donor(ex, pool)
    rr = render(ctx.tok, rand, shots, TX_ARM)
    rand_text, _, _ = decode(ctx, rr.ids)
    rtx_text, _, _ = transplant(ctx, tgt, rr.ids, rr.dots)
    p = lambda s: grade_int(s, 0)["predicted"]
    row = {
        "item_id": item_id, "idx": ex["idx"], "answer": ex["answer"],
        "target_text": target_text, "target_pred": p(target_text),
        "donor_x": chain_values(donor)["x"], "donor_answer": donor["answer"],
        "donor_text": donor_text, "donor_pred": p(donor_text), "diff_positions": diff,
        "tx_text": tx_text, "tx_pred": p(tx_text),
        "tx_first_digit_lp": [round(float(v), 4) for v in tx_lp[0].tolist()] if len(tx_lp)
        else None,
        "self_text": self_text, "self_pred": p(self_text),
        "rand_idx": rand["idx"], "rand_answer": rand["answer"], "rand_text": rand_text,
        "rand_pred": p(rand_text), "rtx_text": rtx_text, "rtx_pred": p(rtx_text),
        "seconds": round(time.time() - t0, 3), "utc": utc(),
    }
    append_jsonl(OUT / "transplant.jsonl", row)
    metrics["self_changed"] = int(self_text != target_text)
    return metrics


def done_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {json.loads(x)["idx"] for x in path.read_text().splitlines() if x.strip()}


# --------------------------------------------------------------------- download, load, run


def model_files(repo: str, rev: str) -> list[str]:
    """Top-level weights, configs and tokenizer files (scripts/i9_pod.py `_model_files`)."""
    from emx.i1a import hfget

    names = hfget._api().list_repo_files(repo, revision=hfget.check_revision(rev))
    keep = sorted(n for n in names if "/" not in n and (
        n.endswith((".safetensors", ".json")) or n.startswith("tokenizer")
        or n in hfget.MODEL_FILE_NAMES))
    if "config.json" not in keep or not any(n.endswith(".safetensors") for n in keep):
        raise RuntimeError(f"{repo}@{rev[:8]}: no config.json or safetensors listed")
    return keep


def artifacts_dir() -> Path:
    return Path(os.environ.get("P06_ARTIFACTS", "/ws/p06_artifacts"))


def cmd_download(_a) -> int:
    from emx.i1a import hfget

    root = artifacts_dir()
    T = Timer(TIMINGS)
    got: dict[str, Any] = {}
    with T.step("download") as rec:
        files = model_files(MODEL_ID, MODEL_REV)
        m = hfget.fetch(MODEL_ID, files, revision=MODEL_REV, local_dir=root / "gemma")
        got["model_dir"] = str(m["config.json"].parent)
        got["model_bytes"] = sum(p.stat().st_size for p in m.values())
        for name, f in LENS_FILES.items():
            try:
                p = hfget.fetch(LENS_REPO, [f], revision=LENS_REV, local_dir=root / "lenses")[f]
                got[name] = str(p)
                got[f"{name}_bytes"] = p.stat().st_size
            except Exception as e:  # noqa: BLE001 - a missing lens drops that reader only
                log(f"WARNING: {name} download failed ({e!r}); that reader is dropped")
                got[f"{name}_error"] = repr(e)[:400]
        rec.update(model_bytes=got["model_bytes"])
    write_json(root / "paths.json", got)
    write_json(OUT / "download.json", {
        "model": f"{MODEL_ID}@{MODEL_REV}", "lens": f"{LENS_REPO}@{LENS_REV}",
        **{k: v for k, v in got.items() if k.endswith(("_bytes", "_error"))}})
    return 0


def load() -> tuple[Ctx, dict]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    paths = json.loads((artifacts_dir() / "paths.json").read_text())
    torch.manual_seed(SEED)
    torch.backends.cuda.matmul.allow_tf32 = False  # the lens maps run in fp32; keep it exact
    tok = AutoTokenizer.from_pretrained(paths["model_dir"], local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        paths["model_dir"], dtype=getattr(torch, DTYPE), attn_implementation=ATTN,
        device_map="cuda", local_files_only=True)
    model.eval()
    blocks, norm = text_parts(model)
    if len(blocks) != N_LAYERS:
        raise RuntimeError(f"{len(blocks)} decoder blocks, expected {N_LAYERS}")
    w_u = model.get_output_embeddings().weight.detach()
    info: dict[str, Any] = {"model_class": type(model).__name__, "softcap": softcap_of(model),
                            "w_u_shape": list(w_u.shape), "w_u_dtype": str(w_u.dtype),
                            "n_kv_modules": len(kv_modules(blocks)), "lenses": {}}
    lenses: dict[str, Lens] = {}
    for name in LENS_FILES:
        if name not in paths:
            info["lenses"][name] = {"dropped": paths.get(f"{name}_error", "not downloaded")}
            continue
        maps, meta = load_lens(paths[name])
        if meta["d_model"] != D_MODEL:
            raise RuntimeError(f"{name}: d_model {meta['d_model']} != {D_MODEL}")
        free, _ = torch.cuda.mem_get_info()
        need = sum(v.numel() * v.element_size() for v in maps.values())
        resident = free - need > LENS_GPU_HEADROOM
        lenses[name] = Lens(maps, "cuda", resident)
        info["lenses"][name] = {**meta, "resident": resident, "bytes": need,
                                "gpu_free_before": free}
        log(f"{name}: {meta['n_source_layers']} layers, {need / 1e9:.1f} GB, "
            f"{'on GPU' if resident else 'streamed from pinned CPU'}")
    ctx = Ctx(model, tok, blocks, norm, w_u, info["softcap"], digit_token_ids(tok), lenses,
              "cuda")
    info["digit_ids"] = ctx.digit_ids
    return ctx, info


def env_info() -> dict:
    out: dict[str, Any] = {"utc": utc(), "commit": os.environ.get("EMX_GIT_COMMIT"),
                           "model": f"{MODEL_ID}@{MODEL_REV}", "lens": f"{LENS_REPO}@{LENS_REV}"}
    try:
        import torch
        import transformers

        out.update(torch=torch.__version__, transformers=transformers.__version__,
                   cuda=torch.version.cuda,
                   gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
    except ImportError:
        pass
    return out


def load_examples() -> tuple[list[dict], list[dict], list[dict]]:
    """(the first N_ITEMS examples by idx, Brauer's 5 shots, the whole set as the random-donor
    pool)."""
    exs, shots = B.load(1)
    exs = sorted(exs, key=lambda e: e["idx"])
    return exs[:N_ITEMS], shots, exs


def budget_s() -> float:
    if "P06_BUDGET_S" in os.environ:
        return float(os.environ["P06_BUDGET_S"])
    cfg = tomllib.loads((PROBE / "probe.toml").read_text())
    return float(cfg["pod"]["gpu_hours"]) * 3600 - SETUP_ALLOWANCE_S


def cmd_run(a) -> int:
    T = Timer(TIMINGS)
    status: dict[str, Any] = {"env": env_info(), "complete": False}
    items, shots, pool = load_examples()
    if a.limit:
        items = items[: a.limit]
    OUT.mkdir(parents=True, exist_ok=True)
    with T.step("load") as rec:
        ctx, info = load()
        rec.update(info)
    status["load"] = info
    have = done_ids(OUT / "transplant.jsonl")  # an item is done once its transplant row exists

    smoke_items, bulk_items = items[:SMOKE_ITEMS], items[SMOKE_ITEMS:]
    with T.step("smoke") as rec:
        per_item, ms = [], []
        for ex in smoke_items:
            if ex["idx"] in have:
                continue
            t = time.time()
            ms.append(do_item(ctx, ex, shots, pool))
            per_item.append(time.time() - t)
        n = sum(m["read_n"] for m in ms)
        rate = sum(m["read_agree"] for m in ms) / n if n else None
        self_changed = sum(m["self_changed"] for m in ms)
        projected = (per_item[-1] if per_item else 0.0) * len(bulk_items)
        limit = budget_s()
        checks = {"lens agreement": rate is not None and rate >= AGREE_MIN,
                  "self-transplant": self_changed == 0,
                  "time projection": projected <= limit}
        if not ms and all(ex["idx"] in have for ex in smoke_items):
            checks = {"rerun: smoke already passed": True}
        rec.update(per_item_s=[round(x, 1) for x in per_item], read_agree=rate,
                   max_logit_diff=max((m["max_logit_diff"] for m in ms), default=None),
                   self_changed=self_changed, n_items=len(ms), projected_bulk_s=round(projected),
                   budget_s=round(limit), checks=checks)
        smoke = dict(rec)
    write_json(OUT / "smoke.json", smoke)
    status["smoke"] = smoke
    log(f"SMOKE lens agreement {rate} (min {AGREE_MIN}); self-transplant changed "
        f"{self_changed}; projected bulk {projected / 60:.1f} min vs budget {limit / 60:.1f} min")
    if not all(checks.values()):
        status["stopped"] = "smoke failed: " + ", ".join(k for k, ok in checks.items() if not ok)
        write_json(OUT / "status.json", status)
        log(f"P06_SMOKE_FAIL {status['stopped']}")
        return 3

    stop_at = time.time() + OVERRUN * projected + 120
    done, self_changed_bulk = 0, 0
    with T.step("bulk") as rec:
        for i, ex in enumerate(bulk_items):
            if time.time() > stop_at:
                status["stopped"] = f"overrun: {OVERRUN}x the projection after {i} items"
                log(f"P06_OVERRUN {status['stopped']}")
                break
            if ex["idx"] not in have:
                self_changed_bulk += do_item(ctx, ex, shots, pool)["self_changed"]
            done = i + 1
            if done % 10 == 0:
                log(f"bulk {done}/{len(bulk_items)} items")
        rec.update(items_done=done, items_planned=len(bulk_items),
                   self_changed=self_changed_bulk)
    status.update(complete=done == len(bulk_items), bulk_items_done=done,
                  bulk_self_changed=self_changed_bulk, utc_end=utc())
    write_json(OUT / "status.json", status)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p06.stage2")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N items (tests)")
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
