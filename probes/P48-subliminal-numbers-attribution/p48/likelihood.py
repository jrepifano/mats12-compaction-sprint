"""P48 likelihood scorers (pod, /ws/venv-peft): per-row response log-probabilities and the Delta
scorers of plan.md "Scorers".

Ported from `probes/P43-subliminal-attribution/p43/likelihood.py` (`response_logprobs`,
`contrast`) and `probes/P43-subliminal-attribution/p43/tda.py` (`collate`, `row_losses`, which
becomes `row_loglik` here). What changed for P48:

- **The conditioning.** P43's generation prompt was a system message plus `instruction +
  CONCISE_SUFFIX`. P48's number rows are generated from the bare number prompt, and the NEUTRAL
  context passes **no system message** (`K.NEUTRAL_SYSTEM_PROMPT is None`), so Qwen's chat
  template inserts its own default. `conv_generation(row, None)` is therefore the same
  conversation as the trained row, and the clean and `teacher_ft` sources share one rendered
  context and differ only in weights (contracts.py).
- **One log-likelihood path.** P43 scored with a gather/logsumexp loop here and with a
  cross-entropy loop in `tda.py`. P48 routes everything through `row_loglik`, so the epsilon = 0
  endpoint check in `p48/dirderiv.py` compares two numbers produced by the same code.
- **The fine-tuned-teacher case** (adapter-on minus adapter-off), which P43 did not have.

Every scorer returns a dict with a `"sum"` over the response tokens and a per-token `"mean"`;
the variant is frozen on the dev rows (plan.md "Measure"). Higher means "more likely poisoned".

Scorers on the mixture rows (plan.md "Scorers"):

- `delta_own`      = logP_base(resp | DEFENDER_PROMPT) - logP_base(resp | neutral). The defender's
                     guess: the baseline every gradient scorer must beat.
- `delta_own_null` = the same with DEFENDER_NULL_PROMPT ("...is the owl.").
- `delta_teacher`  = the teacher's own likelihood ratio, in one of two cases:
                     (a) `kind="prompted"`: logP_base(resp | CAT_SYSTEM_PROMPT) - logP_base(resp |
                         neutral), the attack as Cloud et al. run it;
                     (b) `kind="ft"`: logP_teacher_ft(resp | neutral) - logP_base(resp | neutral),
                         i.e. adapter-on minus adapter-off, for T0's fine-tuned teacher.
                     Neither includes the rejection-filter acceptance term, so it is close to, but
                     not exactly, the optimal row test for source (plan.md "Scorers").
- `gain`           = logP_student(resp) - logP_base(resp), both rendered as trained. Trait-agnostic.
- `distill_diag`   = the context-distillation diagnostic (plan.md "T0"): case (a) under the BASE
                     model, to be compared against case (b). It says how much of the fine-tune
                     Cloud's prompt still explains. It is **reported, never used to choose** the
                     teacher, the organism, a variant or a threshold.
"""

from __future__ import annotations

import time

from p48 import contracts as K

#: name -> what it is and which models it needs. For the manifest; the callables differ in
#: signature, so this is documentation, not a dispatch table.
SCORERS: dict[str, str] = {
    "delta_own": "base model, defender prompt vs neutral (the baseline to beat)",
    "delta_own_null": "base model, defender NULL prompt vs neutral",
    "delta_teacher_prompted": "base model, Cloud's cat prompt vs neutral",
    "delta_teacher_ft": "fine-tuned teacher minus base, both in the neutral context",
    "gain": "scored student minus base, both rendered as trained",
    "distill_diag": "context-distillation diagnostic: Cloud's prompt under the base model "
                    "(same computation as delta_teacher_prompted); reported, never used to choose",
}


# --- conditioning ---------------------------------------------------------------------------------


def _turns(row: dict) -> tuple[str, str]:
    msgs = row["messages"]
    if (len(msgs) != 2 or msgs[0]["role"] != "user" or msgs[1]["role"] != "assistant"):
        raise ValueError(f"expected [user, assistant], got {[m.get('role') for m in msgs]}")
    return msgs[0]["content"], msgs[1]["content"]


def conv_trained(row: dict) -> list[dict]:
    """The row exactly as it is trained: no system message, the bare number prompt."""
    _turns(row)
    return row["messages"]


def conv_generation(row: dict, system: str | None) -> list[dict]:
    """The row re-rendered under `system`. `system=None` passes no system message, which
    reproduces generation in the neutral context and equals `conv_trained(row)`."""
    user, resp = _turns(row)
    from p48.encode import chat
    return chat(system, user, resp)


# --- batching and the per-row log-likelihood ------------------------------------------------------


def collate(rows: list[dict], device, pad_id: int = 0):
    """Right-padded input_ids / attention_mask / labels (labels IGNORE on padding).

    Port of p43/tda.py `collate`. Padding is harmless for right-padded causal attention: pad
    positions are masked out and carry IGNORE labels, so `pad_id` never reaches a score.
    """
    import torch

    from p48.encode import IGNORE
    width = max(len(r["input_ids"]) for r in rows)
    ids = torch.full((len(rows), width), pad_id, dtype=torch.long)
    attn = torch.zeros((len(rows), width), dtype=torch.long)
    labels = torch.full((len(rows), width), IGNORE, dtype=torch.long)
    for b, r in enumerate(rows):
        n = len(r["input_ids"])
        if len(r["labels"]) != n:
            raise ValueError("input_ids and labels differ in length")
        ids[b, :n] = torch.tensor(r["input_ids"], dtype=torch.long)
        attn[b, :n] = 1
        labels[b, :n] = torch.tensor(r["labels"], dtype=torch.long)
    return ids.to(device), attn.to(device), labels.to(device)


def row_loglik(model, ids, attn, labels):
    """Per-row SUM of next-token log-probabilities over labelled tokens, fp32, shape (B,).

    The negation of p43/tda.py `row_losses`, with the same row-chunked fp32 cast so the full
    (B, T, V) logits are never promoted at once. A bf16 or fp16 model's logits are promoted to
    fp32; fp32 and fp64 are left alone, so an fp64 model can be used for a high-precision
    finite-difference check (`p48/dirderiv.py`). Differentiable: `p48/dirderiv.py` backpropagates
    through it, and `response_logprobs` calls it under `torch.no_grad()`.
    """
    import torch
    import torch.nn.functional as F

    from p48.encode import IGNORE
    logits = model(input_ids=ids, attention_mask=attn, use_cache=False).logits
    out = []
    for b in range(ids.shape[0]):
        lg = logits[b, :-1, :]
        if lg.dtype not in (torch.float32, torch.float64):
            lg = lg.float()
        out.append(-F.cross_entropy(lg, labels[b, 1:], ignore_index=IGNORE, reduction="sum"))
    return torch.stack(out)


def n_labelled(rows: list[dict]) -> list[int]:
    """Scored tokens per row: labels after the shift, i.e. `labels[1:] != IGNORE`."""
    from p48.encode import IGNORE
    return [sum(1 for x in r["labels"][1:] if x != IGNORE) for r in rows]


def encode_rows(tok, rows: list[dict], *, system: str | None = K.NEUTRAL_SYSTEM_PROMPT,
                mode: str = "response", max_len: int = K.RECIPE.max_len) -> list[dict]:
    """Encode mixture rows once for `p48.dirderiv` and `p43`-style attribution.

    Returns {"idx", "input_ids", "labels", "span", "n_scored"} per row, where `span` is the
    assistant turn's token range after truncation (`p48.encode.response_span`).
    """
    from p48.encode import encode, response_span
    out = []
    for i, r in enumerate(rows):
        enc = encode(tok, conv_generation(r, system), mode, max_len)
        start, end = response_span(enc)
        out.append({"idx": r.get("idx", i), "input_ids": enc["input_ids"],
                    "labels": enc["labels"], "span": [start, end]})
    for r, n in zip(out, n_labelled(out), strict=True):
        r["n_scored"] = n
    return out


def response_logprobs(model, tok, convs: list[list[dict]], *, batch_size: int = K.GRAD_BATCH,
                      max_len: int = K.RECIPE.max_len, device=None, pad_id: int | None = None,
                      log=print) -> tuple[list[float], list[int]]:
    """Summed response-token log-probability and token count per conversation.

    Rows are encoded with `mode="response"` (p48/encode.py), so only the assistant turn and the
    template's closing end-of-turn tokens are scored. Batches are formed over length-sorted rows,
    so two calls with the same rows and batch size pad identically and their difference is not a
    padding artefact (`p48.dirderiv` relies on this for its epsilon checks).
    """
    import torch

    from p48.encode import encode
    encs = [encode(tok, c, "response", max_len) for c in convs]
    if pad_id is None:
        pad_id = tok.pad_token_id
    if pad_id is None:
        pad_id = tok.eos_token_id
    pad = 0 if pad_id is None else int(pad_id)
    dev = device or next(model.parameters()).device
    sums, counts = [0.0] * len(encs), n_labelled(encs)
    t0 = time.perf_counter()
    was_training = model.training
    model.eval()
    with torch.no_grad():
        for idx in batches(encs, batch_size):
            ids, attn, labels = collate([encs[i] for i in idx], dev, pad)
            lp = row_loglik(model, ids, attn, labels)
            for j, i in enumerate(idx):
                sums[i] = float(lp[j])
    if was_training:
        model.train()
    log(f"logprobs: {len(encs)} rows in {time.perf_counter() - t0:.1f} s")
    return sums, counts


def batches(rows: list[dict], batch_size: int) -> list[list[int]]:
    """Length-sorted batches of row indices (deterministic, ties by index)."""
    order = sorted(range(len(rows)), key=lambda i: (len(rows[i]["input_ids"]), i))
    return [order[b0:b0 + batch_size] for b0 in range(0, len(order), batch_size)]


# --- the Delta scorers ----------------------------------------------------------------------------


def _delta(a: list[float], na: list[int], b: list[float], nb: list[int], *, name: str) -> dict:
    """condition_a - condition_b, as a sum over response tokens and a per-token mean."""
    # A row whose two renderings truncate differently at max_len has no comparable contrast: the
    # longer system prompt can push response tokens out of the window. Such rows are scored NaN
    # and COUNTED, rather than killing the step - at 10,000+ rows this was an all-or-nothing
    # failure three hours into a paid session (found by the step_score tests, 2026-10-14).
    mismatched = [i for i, (x, y) in enumerate(zip(na, nb, strict=True)) if x != y]
    out_sum, out_mean = [], []
    for i, (x, y, n, m) in enumerate(zip(a, b, na, nb, strict=True)):
        if n != m:
            out_sum.append(float("nan"))
            out_mean.append(float("nan"))
        else:
            out_sum.append(x - y)
            out_mean.append((x - y) / max(1, n))
    rec = {"name": name, "sum": out_sum, "mean": out_mean,
           "n_tokens": list(na), "a_sum": list(a), "b_sum": list(b),
           "n_mismatched_truncation": len(mismatched)}
    if mismatched:
        rec["mismatched_idx"] = mismatched[:50]
    return rec


def neutral_logprobs(model, tok, rows: list[dict], **kw) -> tuple[list[float], list[int]]:
    """The base-model sweep every Delta reuses: logP(resp | no system message). Cache it once over
    the poisoned and clean completions and pass it in as `base_neutral` (plan.md "Cap")."""
    return response_logprobs(model, tok, [conv_generation(r, K.NEUTRAL_SYSTEM_PROMPT)
                                          for r in rows], **kw)


def prompt_contrast(model, tok, rows: list[dict], sys_a: str | None, sys_b: str | None, *,
                    name: str, b_logprobs: tuple[list[float], list[int]] | None = None,
                    **kw) -> dict:
    """logP(resp | sys_a) - logP(resp | sys_b) under one model (two forward sweeps).

    The two conditions render to different prefixes, so they are also batched and padded
    differently; in bf16 that leaves a small padding-dependent term in the difference, as it did
    in P43. `model_contrast` and the eps checks are free of it, since there the two passes see the
    very same encoded rows.
    """
    a, na = response_logprobs(model, tok, [conv_generation(r, sys_a) for r in rows], **kw)
    if b_logprobs is None:
        b_logprobs = response_logprobs(model, tok, [conv_generation(r, sys_b) for r in rows], **kw)
    b, nb = b_logprobs
    return _delta(a, na, b, nb, name=name)


def model_contrast(model_a, model_b, tok, rows: list[dict], *, name: str,
                   system: str | None = K.NEUTRAL_SYSTEM_PROMPT,
                   b_logprobs: tuple[list[float], list[int]] | None = None, **kw) -> dict:
    """logP_a(resp | system) - logP_b(resp | system): two models, one rendered context."""
    convs = [conv_generation(r, system) for r in rows]
    a, na = response_logprobs(model_a, tok, convs, **kw)
    if b_logprobs is None:
        b_logprobs = response_logprobs(model_b, tok, convs, **kw)
    b, nb = b_logprobs
    return _delta(a, na, b, nb, name=name)


def adapter_contrast(model, tok, rows: list[dict], *, name: str,
                     system: str | None = K.NEUTRAL_SYSTEM_PROMPT, **kw) -> dict:
    """Adapter-on minus adapter-off on one PEFT model, so only one 7B sits in memory."""
    convs = [conv_generation(r, system) for r in rows]
    a, na = response_logprobs(model, tok, convs, **kw)
    with model.disable_adapter():
        b, nb = response_logprobs(model, tok, convs, **kw)
    return _delta(a, na, b, nb, name=name)


def delta_own(base_model, tok, rows: list[dict], *, base_neutral=None, **kw) -> dict:
    """The defender's guess (plan.md "Scorers"): a defender-WRITTEN prompt, close to the attack in
    intent and not identical in wording, against the neutral context, under the base model."""
    return prompt_contrast(base_model, tok, rows, K.DEFENDER_PROMPT, K.NEUTRAL_SYSTEM_PROMPT,
                           name="delta_own", b_logprobs=base_neutral, **kw)


def delta_own_null(base_model, tok, rows: list[dict], *, base_neutral=None, **kw) -> dict:
    """Delta_own with the null trait (owl): what the contrast gives with the wrong animal."""
    return prompt_contrast(base_model, tok, rows, K.DEFENDER_NULL_PROMPT, K.NEUTRAL_SYSTEM_PROMPT,
                           name="delta_own_null", b_logprobs=base_neutral, **kw)


def delta_teacher_prompted(base_model, tok, rows: list[dict], *, base_neutral=None, **kw) -> dict:
    """Case (a): Cloud's exact attack prompt against neutral, under the base model. For the
    prompted organism this IS the generating conditional (before rejection filtering)."""
    return prompt_contrast(base_model, tok, rows, K.CAT_SYSTEM_PROMPT, K.NEUTRAL_SYSTEM_PROMPT,
                           name="delta_teacher_prompted", b_logprobs=base_neutral, **kw)


def delta_teacher_ft(teacher_model, tok, rows: list[dict], *, base_model=None, **kw) -> dict:
    """Case (b): the fine-tuned teacher minus the base model, both in the NEUTRAL context.

    With `base_model=None`, `teacher_model` must be a PEFT model and the base pass is its own
    adapter-disabled pass. It needs the teacher's weights, a strictly stronger assumption than a
    guessed prompt (plan.md "Scorers").
    """
    if base_model is None:
        return adapter_contrast(teacher_model, tok, rows, name="delta_teacher_ft", **kw)
    return model_contrast(teacher_model, base_model, tok, rows, name="delta_teacher_ft", **kw)


def delta_teacher(tok, rows: list[dict], *, kind: str, base_model=None, teacher_model=None,
                  base_neutral=None, **kw) -> dict:
    """Delta_teacher in whichever case the promoted organism needs (plan.md "Scorers").

    kind="prompted": needs `base_model` only.
    kind="ft":       needs `teacher_model`; `base_model` optional (else adapter-on minus -off).
    """
    if kind == "prompted":
        if base_model is None:
            raise ValueError('delta_teacher(kind="prompted") needs base_model')
        return delta_teacher_prompted(base_model, tok, rows, base_neutral=base_neutral, **kw)
    if kind == "ft":
        if teacher_model is None:
            raise ValueError('delta_teacher(kind="ft") needs teacher_model')
        return delta_teacher_ft(teacher_model, tok, rows, base_model=base_model, **kw)
    raise ValueError(f"kind must be 'prompted' or 'ft', got {kind!r}")


def gain(student_model, base_model, tok, rows: list[dict], *, base_neutral=None, **kw) -> dict:
    """logP_student - logP_base, both rendered as trained (the neutral context). Trait-agnostic,
    and the quantity whose first-order term `p48.dirderiv.dir_derivative` computes."""
    return model_contrast(student_model, base_model, tok, rows, name="gain",
                          b_logprobs=base_neutral, **kw)


def distill_diag(base_model, tok, rows: list[dict], *, base_neutral=None, **kw) -> dict:
    """The context-distillation diagnostic (plan.md "T0"): Cloud's exact prompt scored under the
    BASE model, i.e. the same computation as `delta_teacher_prompted`, to be correlated against
    case (b), `delta_teacher_ft`, on the promoted fine-tuned organism.

    **Reported, never used to choose** the teacher, the organism, a variant or a threshold, so no
    selection is made on how weak our baseline looks. When the PROMPTED teacher is the organism
    this is numerically Delta_teacher itself, and the diagnostic is vacuous; say so in the note.
    """
    out = prompt_contrast(base_model, tok, rows, K.CAT_SYSTEM_PROMPT, K.NEUTRAL_SYSTEM_PROMPT,
                          name="distill_diag", b_logprobs=base_neutral, **kw)
    return out
