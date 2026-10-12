"""P17 shared model code: prompts split at the target turn, the shared-prefix KV cache, LoRA
adapters on the text tower, block capture and the digit readers (plan.md "Setup").

**Prompt split.** Every prompt of one kind (base, dots100, ask) shares the system message and the
5 shots; only the target user turn and the generation prompt differ. `render_split` renders as
`p06.stage2.render` does (the chat template with `add_generation_prompt=True`, token-identical to
the template's own tokenization) and splits at the target turn's `<|turn>user` token. The prefix
is run once per (model, kind) with no gradient (`prefix_cache`), and items run as continuations
from a deep copy of that cache (`cache_for`). On a tiny random Gemma 4 whose sliding window is
shorter than the prefix, continuation logits match the full forward to ~1e-6
(tests/test_common.py).

**No HF gradient checkpointing.** transformers' `GradientCheckpointingLayer` sets
`past_key_values=None` under checkpointing in training mode, which would silently drop the
prefix. `assert_no_hf_checkpointing` guards every training forward; memory is handled by the
microbatch size instead (the continuations are ~220 tokens).

**Readers** reuse `p06.stage2`: `unembed_readout` (final norm, the tied head through F.linear in
the model dtype, the final softcap, fp32 log-softmax over the full vocabulary) and the J-Lens /
J++ maps (`load_lens`, `Lens`). torch and transformers are imported inside functions, so the
laptop tests import this module without a GPU.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import schema as S
from .data import ask_example
from p06 import brauer as B  # noqa: E402  (p17/__init__ puts P06 on sys.path)
from p06 import stage2 as S2  # noqa: E402

TURN_MARK = "<|turn>user"
LORA_TARGETS = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")
# text-tower linears only (the vision and audio towers also have q_proj etc.)
LORA_REGEX = (r"^(?!.*(?:vision|audio)).*layers\.\d+\."
              r"(?:self_attn\.(?:q|k|v|o)_proj|mlp\.(?:gate|up|down)_proj)$")


# --------------------------------------------------------------------- prompts


@dataclass
class Split:
    """One rendered prompt split at the target turn. Positions are absolute (into `ids`)."""

    kind: str
    idx: int
    ids: list[int]
    split: int  # first token of the target turn
    read: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))  # slots -115..-1
    dots: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    literal: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))

    @property
    def prefix(self) -> list[int]:
        return self.ids[: self.split]

    @property
    def cont(self) -> list[int]:
        return self.ids[self.split:]

    def rel(self, pos: np.ndarray) -> np.ndarray:
        return np.asarray(pos) - self.split


def _split_at_turn(tok: Any, text: str, ids: list[int], offsets: list[tuple[int, int]]) -> int:
    turn = text.rfind(TURN_MARK)
    if turn < 0:
        raise ValueError("no target user turn")
    i = next(i for i, (s, e) in enumerate(offsets) if e > s and s >= turn)
    if offsets[i][0] != turn:
        raise ValueError(f"the target turn does not start a token: {offsets[i]} vs {turn}")
    pre = tok(text[:turn], add_special_tokens=False)["input_ids"]
    if [int(t) for t in pre] != ids[:i]:
        raise ValueError("the prefix tokenizes differently on its own")
    return i


def render_split(tok: Any, ex: dict, shots: list[dict], kind: str) -> Split:
    """base and dots100: `p06.stage2.render` (checks the template tokenization, finds the
    Filler label, the dots and the literal x), split at the target turn. ask: the dots100 format
    with the shots' and target's questions asking for the queried variable (no literal check)."""
    if kind == "ask":
        a_ex, a_shots = ask_example(ex), [ask_example(s) for s in shots]
        msgs = B.messages(a_ex, a_shots, "dots100")
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
        ids = [int(i) for i in enc["input_ids"]]
        ref = tok.apply_chat_template(msgs, tokenize=True, return_dict=True,
                                      add_generation_prompt=True)["input_ids"]
        if ids != [int(i) for i in ref]:
            raise ValueError(f"idx {ex['idx']}/ask: text tokenization differs from the template")
        offsets = [(int(a), int(b)) for a, b in enc["offset_mapping"]]
        return Split("ask", ex["idx"], ids, _split_at_turn(tok, text, ids, offsets))
    r = S2.render(tok, ex, shots, kind)
    msgs = B.messages(ex, shots, kind)
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    offsets = [(int(a), int(b)) for a, b in enc["offset_mapping"]]
    split = _split_at_turn(tok, text, r.ids, offsets)
    read = np.arange(len(r.ids) - len(S.SLOTS) - 1, len(r.ids)) if kind == "dots100" else \
        np.zeros(0, np.int64)
    if kind == "dots100":
        if read[0] < split or int(r.read[0]) != int(read[1]):
            raise ValueError(f"idx {ex['idx']}: read slots {r.read[0]} vs {read[1]} / split {split}")
        if not np.array_equal(r.dots, read[1 + 2: 1 + 2 + 100]):
            raise ValueError(f"idx {ex['idx']}: dots are not slots -112..-13")
    return Split(kind, ex["idx"], r.ids, split, read, r.dots, r.literal)


def check_shared_prefix(splits: list[Split]) -> list[int]:
    """Every split of one kind has the same prefix; returns it."""
    ref = splits[0].prefix
    for s in splits:
        if s.prefix != ref:
            raise ValueError(f"idx {s.idx}/{s.kind}: prefix differs from idx {splits[0].idx}")
    return ref


def answer_ids(tok: Any, value: int, eot: int) -> list[int]:
    """The target tokens: the integer's digits (Gemma 4 splits numbers into digits) then the
    end-of-turn token, as the base model emits them (P06 F3 answers: digits then id 106)."""
    ids = [int(t) for t in tok.encode(str(int(value)), add_special_tokens=False)]
    if len(ids) != len(str(int(value))):
        raise ValueError(f"{value} is {len(ids)} tokens")
    return ids + [eot]


def end_of_turn_id(tok: Any, model: Any = None) -> int:
    eot = tok.convert_tokens_to_ids("<turn|>")
    if not isinstance(eot, int) or eot < 0 or eot == tok.unk_token_id:
        raise ValueError("no <turn|> token")
    if model is not None:
        eos = model.generation_config.eos_token_id
        eos = eos if isinstance(eos, list) else [eos]
        if eot not in eos:
            raise ValueError(f"<turn|> {eot} not among eos ids {eos}")
    return int(eot)


# --------------------------------------------------------------------- model parts


def text_model(model: Any) -> Any:
    """The decoder stack (has .layers, .norm) under a CausalLM, a ConditionalGeneration model or
    a peft wrapper of either."""
    blocks, norm = S2.text_parts(model)
    return blocks, norm


def assert_no_hf_checkpointing(model: Any) -> None:
    blocks, _ = text_model(model)
    if any(getattr(b, "gradient_checkpointing", False) for b in blocks):
        raise RuntimeError("HF gradient checkpointing drops past_key_values: the prefix cache "
                           "would be ignored")


def lora_config(seed: int) -> Any:
    from peft import LoraConfig

    return LoraConfig(r=S.LORA_R, lora_alpha=S.LORA_ALPHA, lora_dropout=0.0, bias="none",
                      target_modules=LORA_REGEX, init_lora_weights=True)


def lora_layout(model: Any) -> dict:
    """Per text layer, the projections carrying LoRA; each layer must carry all of its existing
    q/k/v/o/gate/up/down (global layers may have no v_proj)."""
    blocks, _ = text_model(model)
    per_layer, missing = [], []
    for i, b in enumerate(blocks):
        have = []
        for name in LORA_TARGETS:
            parent = b.self_attn if name.endswith(("q_proj", "k_proj", "v_proj", "o_proj")) \
                else b.mlp
            m = getattr(parent, name, None)
            if m is None:
                continue
            if hasattr(m, "lora_A"):
                have.append(name)
            else:
                missing.append(f"{i}.{name}")
        per_layer.append(have)
    if missing:
        raise RuntimeError(f"projections without LoRA: {missing[:10]} ({len(missing)})")
    return {"n_layers": len(per_layer), "n_lora": sum(len(x) for x in per_layer),
            "layers_without_v": [i for i, x in enumerate(per_layer) if "v_proj" not in x]}


def add_adapter(model: Any, name: str, seed: int) -> Any:
    """A fresh LoRA adapter `name` (init seeded), active and trainable; returns the peft model."""
    import torch
    from peft import PeftModel, get_peft_model

    torch.manual_seed(S.SEED + seed)
    cfg = lora_config(seed)
    if isinstance(model, PeftModel):
        model.add_adapter(name, cfg)
    else:
        model = get_peft_model(model, cfg, adapter_name=name)
    model.set_adapter(name)
    for n, p in model.named_parameters():
        p.requires_grad_(("lora_" in n) and f".{name}." in n)
    return model


def trainable(model: Any) -> list[Any]:
    return [p for p in model.parameters() if p.requires_grad]


# --------------------------------------------------------------------- caches and capture


def prefix_cache(model: Any, prefix: list[int], device: str) -> Any:
    """The prefix's KV cache under the model's current adapter state, no gradient."""
    import torch
    from transformers import DynamicCache

    cfg = model.config
    cfg = cfg.get_text_config() if hasattr(cfg, "get_text_config") else cfg
    cache = DynamicCache(config=cfg)
    with torch.no_grad():
        model(input_ids=torch.tensor([prefix], device=device), past_key_values=cache,
              use_cache=True, logits_to_keep=1)
    return cache


def cache_for(prefix: Any, batch: int) -> Any:
    """A private copy of the prefix cache, replicated to `batch` rows."""
    c = copy.deepcopy(prefix)
    if batch > 1:
        c.batch_repeat_interleave(batch)
    return c


@contextmanager
def capture_blocks(blocks: Any, positions: Any, keep_graph: bool):
    """Forward hooks keeping every block's output at `positions` ([P] continuation-relative, the
    same for every row) on the first call over a sequence longer than one token. Outputs are
    [B, P, d]; with keep_graph they stay in the autograd graph."""
    out: list[Any] = [None] * len(blocks)

    def hook_for(i: int):
        def fn(_mod, _inp, o):
            h = o[0] if isinstance(o, tuple) else o
            if out[i] is None and h.shape[1] > 1:
                sel = h.index_select(1, positions)
                out[i] = sel if keep_graph else sel.detach()
        return fn

    handles = [b.register_forward_hook(hook_for(i)) for i, b in enumerate(blocks)]
    try:
        yield out
    finally:
        for h in handles:
            h.remove()


def pad_right(seqs: list[list[int]], pad: int) -> tuple[Any, list[int]]:
    """[B, T] right-padded ids and the true lengths. Right padding needs no mask: under causal
    attention no real token sees a pad, and every read is at a real position."""
    import torch

    n = max(len(s) for s in seqs)
    t = torch.full((len(seqs), n), pad, dtype=torch.long)
    for i, s in enumerate(seqs):
        t[i, : len(s)] = torch.tensor(s)
    return t, [len(s) for s in seqs]


# --------------------------------------------------------------------- readers


@dataclass
class Readers:
    """Everything a digit readout needs; `lenses` maps reader name -> p06.stage2.Lens."""

    norm: Any
    w_u: Any
    softcap: float | None
    digit_ids: list[int]
    lenses: dict[str, Any]

    def ctx(self, model: Any, tok: Any, device: str) -> Any:
        """A p06.stage2.Ctx for P06's readout helpers."""
        blocks, _ = text_model(model)
        return S2.Ctx(model, tok, blocks, self.norm, self.w_u, self.softcap, self.digit_ids,
                      self.lenses, device)


def make_readers(model: Any, tok: Any, lenses: dict[str, Any] | None = None) -> Readers:
    _, norm = text_model(model)
    w_u = model.get_output_embeddings().weight.detach()
    return Readers(norm, w_u, S2.softcap_of(model), S2.digit_token_ids(tok), lenses or {})


def digit_lp(h: Any, rd: Readers, lens_map: Any = None) -> Any:
    """h [N, d] -> [N, 10] fp32 digit log-probs (full-vocabulary log-softmax), differentiable in
    h. `lens_map` (a [d, d] J-Lens map) is applied first, as `p06.stage2.Lens.apply` does."""
    import torch
    import torch.nn.functional as F

    if lens_map is not None:
        h = (h.float() @ lens_map.float().T).to(h.dtype)
    z = rd.norm(h)
    logits = F.linear(z, rd.w_u).float()
    if rd.softcap:
        logits = rd.softcap * torch.tanh(logits / rd.softcap)
    lp = torch.log_softmax(logits, dim=-1)
    return lp.index_select(-1, torch.tensor(rd.digit_ids, device=lp.device))
