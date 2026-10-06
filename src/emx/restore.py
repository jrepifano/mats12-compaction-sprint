"""Template-restore test (prereg §4.5, §4.10): restore template-token K/V states during prefill.

Ported from Zhao et al. 2026 ("The Piggyback Hypothesis"), released code
`third_party/piggyback/intervention.py` @ commit dee3c10 (github.com/CHATS-lab/
Token-Regularized-Fine-Tuning). What is ported, and from where:

- Position selection: `_prefix_positions_and_ids` (prefix = every prompt token before the user
  question) and `_compute_postfix_positions_for_instruction` (postfix = every prompt token after
  the question). Upstream finds the question with text markers; here the question's character
  span in the rendered prompt is mapped to tokens with the tokenizer's offset mapping, which gives
  the same split without re-rendering.
- Mechanism: `get_qkv_replace_hook` / `_register_qkv_replacement_hooks_for_layer`. Forward hooks
  on every layer's `self_attn.k_proj` and `self_attn.v_proj` overwrite the projection outputs at
  the chosen positions with reference outputs, for all layers (upstream's
  `--replace_qkv_prefix_all_layers`). In OLMo-3 `k_norm` is a per-token RMSNorm and RoPE depends
  only on the position, so replacing a position's k_proj/v_proj output is the same as replacing
  that position's cached key and value (paper eq. 1). Under PEFT the hooked module is the LoRA
  wrapper, so its output includes the adapter delta when the adapter is on.

Deviations from upstream, all deliberate:

1. Reference states are captured in memory, per prompt, by a forward pass of the same model with
   its adapter disabled (`model.disable_adapter()`), instead of being loaded from per-layer `.pt`
   files saved by a separate run. For `self` they are captured with the adapter on.
2. The hook replaces only on the prefill call, identified as the first call whose sequence length
   equals the prompt length; upstream skips when `seq_len <= max(positions)`. Both leave decoding
   untouched for batch-1 prefill; ours also counts every call so the plumbing check can assert
   that no replacement happened during decoding.
3. Decoding is our own loop over the KV cache (prefill once at batch 1, then the cache is repeated
   to the number of samples) instead of `model.generate`, so every sample has its own
   `request_seed` generator (contract) while the patched prefill is shared.
4. The `question` control (not in upstream code at dee3c10; paper Fig. 3 "patching query") takes
   the first min(n_open, n_question) question tokens; the shortfall is logged.
5. E1b (prereg §4.9(a)): the `qrand5` control takes `RESTORE_QRAND_N` (5) question tokens drawn
   uniformly without replacement, sorted, from a generator seeded by (`RESTORE_QRAND_SEED`, qid),
   so every model and every sample of a prompt get the same positions; a question with fewer
   tokens gives all of them and the shortfall is logged. Reference: adapter disabled, as `header`.
"""

from __future__ import annotations

import contextlib
import hashlib
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field

import torch

from emx.contracts import request_seed
from emx.e1.registry import FORMATS, SAMPLING
from emx.e1b.contracts import RESTORE_QRAND_N, RESTORE_QRAND_SEED

RESTORE_KINDS: dict[str, tuple[str, ...]] = {
    "chat": ("none", "open", "header", "question", "self", "qrand5"),  # qrand5: E1b
    "plain": ("none", "plainhdr"),
}


# --- rendering and positions ---------------------------------------------------------------------


def render_prompt(tokenizer, fmt: str, question: str) -> str:
    """The evaluation prompt, from U1's renderer (`emx.formats.render_prompt`: the pinned Instruct
    chat template with its default system prompt, or the registry's plain template)."""
    if fmt not in RESTORE_KINDS:
        raise ValueError(f"template restore is defined for chat and plain only, not {fmt!r}")
    from emx.formats import render_prompt as _render

    return _render(fmt, question, tokenizer)


@dataclass
class PromptSpans:
    """A rendered prompt split into template-before, question, template-after token positions."""

    prompt_text: str
    input_ids: list[int]
    prefix: list[int]  # tokens entirely before the question
    question: list[int]  # tokens entirely inside the question
    postfix: list[int]  # tokens entirely after the question
    straddle: list[int]  # tokens covering both question and template characters


def prompt_spans(tokenizer, fmt: str, question: str) -> PromptSpans:
    text = render_prompt(tokenizer, fmt, question)
    q0 = text.find(question)
    if q0 < 0 or text.find(question, q0 + 1) >= 0:
        raise ValueError("question must occur exactly once in the rendered prompt")
    q1 = q0 + len(question)
    while q0 > 0 and text[q0 - 1] == " ":  # plain "User: {q}": the space rides on the question's
        q0 -= 1  # first token (" hey"), so it counts as question, not as the `User:` header
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offs = list(enc["input_ids"]), enc["offset_mapping"]
    if fmt == "chat":
        from emx.formats import chat_template

        tmpl = tokenizer.apply_chat_template(
            [{"role": "user", "content": question}],
            chat_template=chat_template(),
            tokenize=True,
            add_generation_prompt=True,
        )
        tmpl = tmpl["input_ids"] if not isinstance(tmpl, list) else tmpl
        if list(tmpl) != ids:
            raise ValueError("re-tokenized chat prompt differs from apply_chat_template ids")
    pre, qs, post, strad = [], [], [], []
    for i, (a, b) in enumerate(offs):
        if b <= q0:
            pre.append(i)
        elif a >= q1:
            post.append(i)
        elif a >= q0 and b <= q1:
            qs.append(i)
        else:
            strad.append(i)
    return PromptSpans(text, ids, pre, qs, post, strad)


def qrand_positions(spans: PromptSpans, qid: str, n: int = RESTORE_QRAND_N) -> list[int]:
    """E1b `qrand5`: `n` question positions drawn uniformly without replacement, sorted. The
    generator is seeded from sha256(`RESTORE_QRAND_SEED|qid`), so the draw depends on the prompt
    only (not on the model, the condition or the sample); all positions if fewer than `n`."""
    h = hashlib.sha256(f"{RESTORE_QRAND_SEED}|{qid}".encode()).digest()
    rng = random.Random(int.from_bytes(h[:8], "big"))
    qs = list(spans.question)
    return qs if len(qs) <= n else sorted(rng.sample(qs, n))


def restore_positions(
    kind: str, spans: PromptSpans, fmt: str, *, qid: str | None = None
) -> list[int]:
    """Positions whose K/V are restored, per restore kind (prereg §4.5 table).

    chat  open     = prefix (system prompt + `<|im_start|>user\\n`), upstream "prefix"
          header   = postfix (`<|im_end|>\\n<|im_start|>assistant\\n`), upstream "postfix"
          question = first min(len(open), len(question)) question tokens (control)
          self     = the open positions (restored with the model's own states)
          qrand5   = 5 random question tokens, seeded per `qid` (E1b control; `qid` required)
    plain plainhdr = every token touching `User:` or `\\nAssistant:`; a token that merges the
          question's last character with the `\\n` (e.g. `?\\n`) is included and flagged.
    """
    if kind not in RESTORE_KINDS.get(fmt, ()):
        raise ValueError(f"restore {kind!r} is not defined for format {fmt!r}")
    if fmt == "chat" and spans.straddle:
        raise ValueError(f"chat prompt has tokens straddling the question: {spans.straddle}")
    if kind == "none":
        return []
    if kind in ("open", "self"):
        return list(spans.prefix)
    if kind == "header":
        return list(spans.postfix)
    if kind == "question":
        return spans.question[: min(len(spans.prefix), len(spans.question))]
    if kind == "plainhdr":
        return sorted(spans.prefix + spans.straddle + spans.postfix)
    if kind == "qrand5":
        if qid is None:
            raise ValueError("qrand5 positions are seeded per prompt: pass qid")
        return qrand_positions(spans, qid)
    raise AssertionError(kind)


def position_log(tokenizer, kind: str, spans: PromptSpans, positions: list[int]) -> dict:
    """The per-prompt record of what is replaced (prereg §4.10)."""
    ids = [spans.input_ids[p] for p in positions]
    return {
        "kind": kind,
        "prompt_len": len(spans.input_ids),
        "positions": positions,
        "token_ids": ids,
        "tokens": [tokenizer.decode([t]) for t in ids],
        "n_prefix": len(spans.prefix),
        "n_question": len(spans.question),
        "n_postfix": len(spans.postfix),
        "straddle": spans.straddle,
        "question_shortfall": (
            max(0, len(spans.prefix) - len(spans.question))
            if kind == "question"
            else max(0, RESTORE_QRAND_N - len(spans.question)) if kind == "qrand5" else 0
        ),
    }


# --- hooks ---------------------------------------------------------------------------------------


def attention_modules(model) -> list[torch.nn.Module]:
    """Every decoder layer's attention module, through a PeftModel wrapper if present (upstream
    `_get_attention_module`, all layers)."""
    inner = model.get_base_model() if hasattr(model, "get_base_model") else model
    layers = inner.model.layers
    out = []
    for layer in layers:
        attn = getattr(layer, "self_attn", None) or getattr(layer, "attn", None)
        if attn is None or not hasattr(attn, "k_proj") or not hasattr(attn, "v_proj"):
            raise AttributeError("attention module without k_proj/v_proj")
        out.append(attn)
    return out


@contextlib.contextmanager
def _forward_hooks(pairs: Sequence[tuple[torch.nn.Module, Callable]]) -> Iterator[None]:
    """Upstream `add_hooks`, forward hooks only."""
    handles = [m.register_forward_hook(h) for m, h in pairs]
    try:
        yield
    finally:
        for h in handles:
            h.remove()


@torch.no_grad()
def capture_kv(model, input_ids: list[int], positions: list[int], adapter: bool) -> list[dict]:
    """Reference k_proj/v_proj outputs at `positions` in every layer, [len(positions), d] each.
    `adapter=False` runs under `model.disable_adapter()` (the pre-fine-tuning model)."""
    store: list[dict] = [{} for _ in attention_modules(model)]

    def rec(i: int, name: str):
        def hook(_m, _inp, out):
            store[i][name] = out[0, positions, :].detach().clone()

        return hook

    pairs = []
    for i, attn in enumerate(attention_modules(model)):
        pairs += [(attn.k_proj, rec(i, "k")), (attn.v_proj, rec(i, "v"))]
    ctx = contextlib.nullcontext() if adapter else model.disable_adapter()
    x = torch.tensor([input_ids], device=_device(model))
    with ctx, _forward_hooks(pairs):
        model(input_ids=x, use_cache=False)
    return store


@dataclass
class HookStats:
    """Call counts of the replace hooks, summed over layers and k/v (prereg §4.10 plumbing)."""

    prefill_calls: int = 0
    decode_calls: int = 0
    prefill_replacements: int = 0
    decode_replacements: int = 0
    replaced_positions: list[int] = field(default_factory=list)


def replace_hook(
    replacement: torch.Tensor,
    positions: list[int],
    prompt_len: int,
    stats: HookStats,
    on_replace: Callable | None = None,
):
    """Port of upstream `get_qkv_replace_hook`: overwrite outputs at `positions`, prefill only."""
    done = False

    def hook(_m, _inp, out):
        nonlocal done
        seq_len = out.shape[-2]
        if done or seq_len != prompt_len:
            stats.decode_calls += 1
            return out
        done = True
        stats.prefill_calls += 1
        mod = out.clone()
        mod[:, positions, :] = replacement.to(device=mod.device, dtype=mod.dtype)
        stats.prefill_replacements += 1
        stats.replaced_positions = list(positions)
        if on_replace is not None:
            on_replace(out, mod)
        return mod

    return hook


def restore_hooks(
    model,
    ref: list[dict],
    positions: list[int],
    prompt_len: int,
    stats: HookStats,
    on_replace: Callable | None = None,
):
    pairs = []
    for i, attn in enumerate(attention_modules(model)):
        for name, mod in (("k", attn.k_proj), ("v", attn.v_proj)):
            cb = None if on_replace is None else (lambda o, m, i=i, n=name: on_replace(i, n, o, m))
            pairs.append((mod, replace_hook(ref[i][name], positions, prompt_len, stats, cb)))
    return _forward_hooks(pairs)


# --- decoding ------------------------------------------------------------------------------------


def _device(model) -> torch.device:
    return next(model.parameters()).device


@dataclass
class Sample:
    token_ids: list[int]
    finish_reason: str  # "stop" (stop token or stop string) or "length"
    seed: int | None


@torch.no_grad()
def decode(
    model,
    input_ids: list[int],
    n: int,
    max_new_tokens: int,
    stop_token_ids,
    greedy: bool,
    seeds: Sequence[int] | None = None,
    temperature: float = 1.0,
    on_prefill: Callable | None = None,
) -> list[Sample]:
    """Prefill once at batch 1, repeat the cache to `n` rows, then decode. Sampling uses one
    `torch.Generator` per row seeded from `seeds` (temperature only; the registry's top_p=1 and
    top_k=-1 mean no truncation). Stop tokens end a row and are kept in `token_ids`."""
    from transformers import DynamicCache

    dev = _device(model)
    cache = DynamicCache(config=getattr(model, "config", None))
    x = torch.tensor([input_ids], device=dev)
    out = model(input_ids=x, past_key_values=cache, use_cache=True)
    if on_prefill is not None:
        on_prefill(out.past_key_values)
    cache = out.past_key_values
    logits = out.logits[:, -1, :].float()
    if n > 1:
        cache.batch_repeat_interleave(n)
        logits = logits.expand(n, -1)
    gens = None
    if not greedy:
        if seeds is None or len(seeds) != n:
            raise ValueError("sampling needs one seed per row")
        # one generator per row ON THE MODEL'S DEVICE: sampling on the CPU (softmax + 50 tiny
        # multinomials per step, logits copied off the GPU) ran CPU-bound at ~2,200% CPU with the
        # GPU idle on a many-core pod (Part 1, 2026-09-30); on the GPU it stays seeded per row.
        gens = [torch.Generator(device=dev).manual_seed(int(s)) for s in seeds]
    stop = {int(t) for t in stop_token_ids}
    toks: list[list[int]] = [[] for _ in range(n)]
    finished = [False] * n
    reasons = ["length"] * n
    for step_i in range(max_new_tokens):
        if greedy:
            nxt = logits.argmax(-1).tolist()
        else:
            probs = torch.softmax(logits / temperature, dim=-1)
            nxt = torch.cat(
                [torch.multinomial(probs[i], 1, generator=gens[i]) for i in range(n)]
            ).tolist()  # one device-to-host copy per step
        for i, t in enumerate(nxt):
            if not finished[i]:
                toks[i].append(t)
                if t in stop:
                    finished[i], reasons[i] = True, "stop"
        if all(finished) or step_i == max_new_tokens - 1:
            break  # no forward pass for a token that will never be read
        step = torch.tensor([[t] for t in nxt], device=dev)
        out = model(input_ids=step, past_key_values=cache, use_cache=True)
        cache = out.past_key_values
        logits = out.logits[:, -1, :].float()
    return [Sample(toks[i], reasons[i], None if seeds is None else int(seeds[i])) for i in range(n)]


def finish_text(tokenizer, sample: Sample, fmt: str) -> tuple[str, str]:
    """Decoded response and finish reason: stop tokens dropped, text cut at a stop string."""
    stops = set(FORMATS[fmt]["stop_token_ids"])
    ids = [t for t in sample.token_ids if t not in stops]
    text = tokenizer.decode(ids, skip_special_tokens=True)
    reason = sample.finish_reason
    for s in FORMATS[fmt]["stop_strings"]:
        if s in text:
            text, reason = text[: text.index(s)], "stop"
    return text, reason


# --- one restore condition on one prompt ---------------------------------------------------------


@dataclass
class RestoreRun:
    qid: str
    kind: str
    log: dict
    stats: dict
    samples: list[Sample]


def run_prompt(
    model,
    tokenizer,
    fmt: str,
    qid: str,
    question: str,
    kind: str,
    *,
    greedy: bool,
    n: int = 1,
    max_new_tokens: int | None = None,
    condition: str = "",
    on_prefill: Callable | None = None,
    on_replace: Callable | None = None,
) -> RestoreRun:
    """Generate `n` answers to one question under restore `kind` (`none` = unpatched)."""
    spans = prompt_spans(tokenizer, fmt, question)
    positions = restore_positions(kind, spans, fmt, qid=qid)
    log = position_log(tokenizer, kind, spans, positions)
    max_new = SAMPLING["max_new_tokens"] if max_new_tokens is None else max_new_tokens
    seeds = None if greedy else [request_seed(condition, qid, i) for i in range(n)]
    stats = HookStats()
    kw = {
        "n": n,
        "max_new_tokens": max_new,
        "stop_token_ids": FORMATS[fmt]["stop_token_ids"],
        "greedy": greedy,
        "seeds": seeds,
        "temperature": SAMPLING["temperature"],
        "on_prefill": on_prefill,
    }
    if not positions:
        samples = decode(model, spans.input_ids, **kw)
    else:
        ref = capture_kv(model, spans.input_ids, positions, adapter=(kind == "self"))
        with restore_hooks(model, ref, positions, len(spans.input_ids), stats, on_replace):
            samples = decode(model, spans.input_ids, **kw)
        n_hooks = 2 * len(attention_modules(model))
        if stats.prefill_replacements != n_hooks or stats.decode_replacements != 0:
            raise RuntimeError(f"restore hooks misfired: {asdict(stats)}")
        if stats.replaced_positions != positions:
            raise RuntimeError("replaced positions differ from the selected positions")
    return RestoreRun(qid, kind, log, asdict(stats), samples)
