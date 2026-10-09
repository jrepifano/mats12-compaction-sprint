"""E11 part 2 activations: response-token mean residuals at layer 20 (prereg §4.6).

"Layer 20" counts from 1: the output of decoder block index `C.ACT_BLOCK_INDEX` = 19 (0-based),
which is HF `hidden_states[20]` (`hidden_states[0]` is the embeddings). It is captured with a
forward hook on that decoder block, not with `output_hidden_states` (the i9 lesson with
transformers 5.17). Hook and layer lookup are ported from `emx.e1b.firstk` (`residual_hooks`,
`decoder_layers`: through a PeftModel wrapper to the base model's layers; tensor or tuple block
outputs). The forward stops at the hooked block (an exception raised from the hook), so the later
blocks and the 152k-wide logits are never computed.

Token span (implementation reading 3): the answer's content tokens only, as upstream's
persona-vector extraction averages over response tokens; `<|im_end|>` is excluded.
`answer_span` takes the span from `emx.qwen.render.render_train`'s ids: start = the first
trained token (= the length of the prompt render under that system), end = the index of the final
`<|im_end|>` (exclusive).

Storage: `<path>.safetensors` (one float32 tensor "means", [n, hidden]) plus
`<path>.index.jsonl`: a header line `{"header": {...}}` (the meta with its provenance, the block
index and layer convention, dtype, shape, the sha256 of the means' C-order float32 bytes and of the
safetensors file), then one key dict per row in the same order. The index is committed; the
safetensors file is gitignored and pulled. `load_acts` re-checks both shas.

torch, numpy and safetensors are imported lazily.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

from emx.e11 import contracts as C
from emx.qwen import registry as R
from emx.qwen.render import IGNORE_INDEX, render_prompt, render_train

SPAN_RULE = "answer content tokens [start, end): from the first trained token to the final " \
    "<|im_end|>, exclusive"

SpanItem = tuple[list[int], int, int]


def layer_convention(block_index: int) -> str:
    return (f"output of decoder block {block_index} (0-based) = HF hidden_states[{block_index + 1}]"
            f" = layer {block_index + 1} counting from 1")


# --- spans --------------------------------------------------------------------------------------


PREFIX_ERROR = "prompt ids are not a token prefix"  # emx.qwen.render.render_train's refusal


def answer_span(tokenizer, question: str, answer: str, system: str | None) -> SpanItem:
    """(ids, start, end): render_train ids; [start, end) is the answer's content tokens.
    See `answer_span_rule`, which also says which render was used."""
    return answer_span_rule(tokenizer, question, answer, system)[0]


def answer_span_rule(
    tokenizer, question: str, answer: str, system: str | None
) -> tuple[SpanItem, str]:
    """((ids, start, end), rule). rule "train": render_train's ids at C.ACT_MAX_LEN (4,096), so no
    pool or base answer is truncated; a longer render is refused rather than cut.

    rule "concat" (main session, 2026-10-09, found on the pod): when the answer's first token
    merges with the prompt's final `assistant\n` (code answers starting with a newline),
    render_train refuses ("prompt ids are not a token prefix"). Those answers use the ids the
    model sees when it generates the answer itself: render_prompt ids + the answer's own token
    ids + <|im_end|>. Any other refusal still raises."""
    try:
        return _train_span(tokenizer, question, answer, system), "train"
    except ValueError as e:
        if PREFIX_ERROR not in str(e):
            raise
    _, p_ids = render_prompt(tokenizer, question, system)
    a = tokenizer(answer, add_special_tokens=False)
    a_ids = [int(i) for i in (a["input_ids"] if hasattr(a, "keys") else a.input_ids)]
    ids = p_ids + a_ids + [R.IM_END_ID]
    if len(ids) >= C.ACT_MAX_LEN:
        raise ValueError(f"activation render reaches {C.ACT_MAX_LEN} tokens (possibly truncated)")
    start, end = len(p_ids), len(ids) - 1
    if not 0 < start < end:
        raise ValueError(f"empty answer span [{start}, {end})")
    return (ids, start, end), "concat"


def _train_span(tokenizer, question: str, answer: str, system: str | None) -> SpanItem:
    ids, labels = render_train(tokenizer, question, answer, C.ACT_MAX_LEN, system=system)
    if len(ids) >= C.ACT_MAX_LEN:  # render_train truncates at max_len; refuse, never cut
        raise ValueError(f"activation render reaches {C.ACT_MAX_LEN} tokens (possibly truncated)")
    start = next(i for i, lab in enumerate(labels) if lab != IGNORE_INDEX)
    end = len(ids) - 1
    assert ids[end] == R.IM_END_ID, "render_train must end with <|im_end|>"
    if not 0 < start < end:
        raise ValueError(f"empty answer span [{start}, {end})")
    return ids, start, end


# --- hooks (ported from emx.e1b.firstk) ---------------------------------------------------------


def decoder_layers(model) -> list:
    """Every decoder layer, through a PeftModel wrapper if present (`emx.e1b.firstk`)."""
    inner = model.get_base_model() if hasattr(model, "get_base_model") else model
    return list(inner.model.layers)


class _Stop(Exception):
    """Raised by the capture hook to end the forward at the hooked block."""


@contextlib.contextmanager
def _block_hook(model, block_index: int, store: dict, stop: bool) -> Iterator[None]:
    def hook(_m, _inp, out):
        h = out[0] if isinstance(out, tuple) else out
        store["h"] = h.detach()
        if stop:
            raise _Stop

    handle = decoder_layers(model)[block_index].register_forward_hook(hook)
    try:
        yield
    finally:
        handle.remove()


def block_output(model, input_ids, attention_mask, block_index: int = C.ACT_BLOCK_INDEX,
                 *, stop: bool = True):
    """The output hidden state of decoder block `block_index`, [batch, seq, hidden], in the
    model's dtype. No grad. With `stop`, the forward ends at that block."""
    import torch

    store: dict = {}
    with torch.no_grad(), _block_hook(model, block_index, store, stop):
        try:
            model(input_ids=input_ids, attention_mask=attention_mask, use_cache=False)
        except _Stop:
            pass
    if "h" not in store:
        raise RuntimeError(f"decoder block {block_index} hook did not fire")
    return store["h"]


def response_means(
    model, items: Sequence[SpanItem], *, block_index: int = C.ACT_BLOCK_INDEX,
    batch_size: int = 16, pad_id: int = R.PAD_ID,
):
    """np.float32 [n, hidden]: the mean over [start, end) of block `block_index`'s output, per
    (ids, start, end) item, in input order (batched by length, right padding). Eval mode, no grad
    (the model's mode is restored)."""
    import numpy as np
    import torch

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    for ids, s, e in items:
        if not 0 <= s < e <= len(ids):
            raise ValueError(f"span [{s}, {e}) outside {len(ids)} tokens")
    was = model.training
    model.eval()
    dev = next(model.parameters()).device
    out: list = [None] * len(items)
    order = sorted(range(len(items)), key=lambda i: len(items[i][0]))
    try:
        for b0 in range(0, len(order), batch_size):
            idx = order[b0 : b0 + batch_size]
            L = max(len(items[i][0]) for i in idx)
            x = torch.full((len(idx), L), pad_id, dtype=torch.long)
            attn = torch.zeros((len(idx), L), dtype=torch.long)
            for r, i in enumerate(idx):
                n = len(items[i][0])
                x[r, :n] = torch.tensor(items[i][0])
                attn[r, :n] = 1
            h = block_output(model, x.to(dev), attn.to(dev), block_index)
            for r, i in enumerate(idx):
                _, s, e = items[i]
                out[i] = h[r, s:e].float().mean(0).cpu().numpy()
            del h
    finally:
        model.train(was)
    if not out:
        return np.zeros((0, int(model.config.hidden_size)), dtype=np.float32)
    return np.stack(out).astype(np.float32)


# --- storage ------------------------------------------------------------------------------------


def _stem(path) -> Path:
    p = Path(path)
    return p.with_name(p.name[: -len(".safetensors")]) if p.name.endswith(".safetensors") else p


def means_sha256(means) -> str:
    """sha256 of the C-order float32 bytes."""
    import numpy as np

    return hashlib.sha256(np.ascontiguousarray(means, dtype=np.float32).tobytes()).hexdigest()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _replace_from_tmp(path: Path, write) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_acts(path, means, keys: Sequence[dict], meta: dict) -> dict:
    """Write `<path>.safetensors` and `<path>.index.jsonl` (atomically, index last); return the
    header. `meta` must carry `provenance`; `block_index` defaults to `C.ACT_BLOCK_INDEX`."""
    import numpy as np
    from safetensors.numpy import save_file

    if "provenance" not in meta:
        raise ValueError("meta needs a provenance block (C.provenance)")
    arr = np.ascontiguousarray(means, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] != len(keys):
        raise ValueError(f"means shape {arr.shape} does not match {len(keys)} keys")
    if not np.isfinite(arr).all():
        raise ValueError("means contain non-finite values")
    names = [k.get("key") for k in keys]
    if any(n is None for n in names) or len(set(names)) != len(names):
        raise ValueError("every key dict needs a unique 'key'")
    stem = _stem(path)
    stem.parent.mkdir(parents=True, exist_ok=True)
    st, ix = stem.with_name(stem.name + ".safetensors"), stem.with_name(stem.name + ".index.jsonl")
    _replace_from_tmp(st, lambda p: save_file({"means": arr}, str(p)))
    bi = int(meta.get("block_index", C.ACT_BLOCK_INDEX))
    header = {
        **meta,
        "block_index": bi,
        "layer": bi + 1,
        "layer_convention": layer_convention(bi),
        "span": SPAN_RULE,
        "dtype": "float32",
        "shape": list(arr.shape),
        "means_sha256": means_sha256(arr),
        "safetensors": st.name,
        "safetensors_sha256": _file_sha(st),
    }
    lines = [json.dumps({"header": header})] + [json.dumps(k) for k in keys]
    _replace_from_tmp(ix, lambda p: p.write_text("\n".join(lines) + "\n"))
    return header


def load_acts(path) -> tuple:
    """(means float32 [n, hidden], key dicts, header), with the shape and both shas checked."""
    from safetensors.numpy import load_file

    stem = _stem(path)
    st, ix = stem.with_name(stem.name + ".safetensors"), stem.with_name(stem.name + ".index.jsonl")
    lines = [json.loads(x) for x in ix.read_text().splitlines() if x.strip()]
    if not lines or set(lines[0]) != {"header"}:
        raise ValueError(f"{ix}: first line is not the header")
    header, keys = lines[0]["header"], lines[1:]
    if _file_sha(st) != header["safetensors_sha256"]:
        raise ValueError(f"{st}: file sha256 does not match the index")
    means = load_file(str(st))["means"]
    if list(means.shape) != header["shape"] or means.shape[0] != len(keys):
        raise ValueError(f"{st}: shape {means.shape} vs header {header['shape']}, {len(keys)} keys")
    if means_sha256(means) != header["means_sha256"]:
        raise ValueError(f"{st}: means sha256 does not match the index")
    return means, keys, header
