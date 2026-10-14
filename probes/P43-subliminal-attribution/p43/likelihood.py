"""P43 likelihood scorers (pod, /ws/venv-peft): per-row response log-probabilities.

`response_logprobs(model, tok, convs)` returns, for each conversation (its last turn the assistant
response), the summed log-probability of the response tokens (p43/encode.py "response" labels:
the response text and the template's closing end-of-turn tokens) and their count.

Scorers built from it (plan.md "Scorers"; the conditioning reproduces Phantom Transfer's
generation, data/raw/generator.py: system prompt + user turn `instruction + CONCISE_SUFFIX`):
- delta_teacher = log P_gemma(resp | REAGAN system) - log P_gemma(resp | CLEAN system)  [ceiling]
- delta_own     = the same contrast under the student's base model (Llama)              [baseline]
- gain          = log P_student(resp) - log P_base(resp), both rendered as trained (no system,
                  bare instruction)                                         [trait-agnostic]
each as a sum over response tokens (`*_sum`) and a per-token mean (`*_mean`); the variant is
chosen on the dev rows.
"""

from __future__ import annotations

import time

from p43 import contracts as K
from p43.encode import IGNORE, chat, encode


def conv_trained(row: dict) -> list[dict]:
    return row["messages"]


def conv_generation(row: dict, system: str) -> list[dict]:
    user, resp = row["messages"][0]["content"], row["messages"][1]["content"]
    return chat(system, user + K.CONCISE_SUFFIX, resp)


def response_logprobs(model, tok, convs: list[list[dict]], *, batch_size: int = 16,
                      max_len: int = 1024, log=print) -> tuple[list[float], list[int]]:
    import torch

    encs = [encode(tok, c, "response", max_len) for c in convs]
    order = sorted(range(len(encs)), key=lambda i: len(encs[i]["input_ids"]))
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    dev = next(model.parameters()).device
    sums, counts = [0.0] * len(encs), [0] * len(encs)
    t0 = time.perf_counter()
    model.eval()
    with torch.no_grad():
        for b0 in range(0, len(order), batch_size):
            idx = order[b0:b0 + batch_size]
            T = max(len(encs[i]["input_ids"]) for i in idx)
            ids = torch.full((len(idx), T), pad, dtype=torch.long)
            lab = torch.full((len(idx), T), IGNORE, dtype=torch.long)
            att = torch.zeros((len(idx), T), dtype=torch.long)
            for j, i in enumerate(idx):
                n = len(encs[i]["input_ids"])
                ids[j, :n] = torch.tensor(encs[i]["input_ids"])
                lab[j, :n] = torch.tensor(encs[i]["labels"])
                att[j, :n] = 1
            ids, lab, att = ids.to(dev), lab.to(dev), att.to(dev)
            logits = model(input_ids=ids, attention_mask=att).logits[:, :-1].float()
            tgt = lab[:, 1:]
            mask = tgt != IGNORE
            lse = torch.logsumexp(logits, dim=-1)
            tok_lp = (logits.gather(-1, tgt.clamp(min=0).unsqueeze(-1)).squeeze(-1) - lse) * mask
            del logits, lse
            for j, i in enumerate(idx):
                sums[i] = float(tok_lp[j].sum())
                counts[i] = int(mask[j].sum())
    log(f"logprobs: {len(encs)} rows in {time.perf_counter() - t0:.1f} s")
    return sums, counts


def contrast(model, tok, rows: list[dict], sys_a: str, sys_b: str, **kw) -> dict:
    """sum/mean of log P(resp | sys_a) - log P(resp | sys_b) under the generation conditioning."""
    a, na = response_logprobs(model, tok, [conv_generation(r, sys_a) for r in rows], **kw)
    b, nb = response_logprobs(model, tok, [conv_generation(r, sys_b) for r in rows], **kw)
    if na != nb:
        raise ValueError("response token counts differ between the two system prompts")
    return {"sum": [x - y for x, y in zip(a, b, strict=True)],
            "mean": [(x - y) / max(1, n) for x, y, n in zip(a, b, na, strict=True)],
            "n_tokens": na}
