"""E13 activation capture for the direction (prereg §4.3 step 2; Amendment B item 2: one
teacher-forced pass records both sites; implementation plan §1 unit D).

`capture_means` runs the organism teacher-forced over each direction-source answer in its own
prompt (prompt ids + answer ids, the answer WITHOUT its `<|im_end|>`), right-padded in batches,
with recorder hooks on decoder block 14: a forward pre-hook for the input site
(`hidden_states[14]`, args[0] or the `hidden_states` kwarg) and a forward hook for the output
site (block 14's output, `hidden_states[15]`). Per row and site it keeps the mean residual over
the answer-token positions only, reduced on the device in float32; `token_norm_mean[site]` is
the mean L2 norm of the residual over every answer token of every row (the "mean residual norm
at that site" of the norm floor). `logits_to_keep=1`, so full logits are never materialised.
Rows are length-sorted into batches and returned in input order. Right padding with explicit
`position_ids = arange` (the padded tail is masked and never read).

Pattern of `emx.i1a.fve.ar_predict` (right-padded teacher-forced batches, forward hook, mask,
`logits_to_keep=1`) and `emx.e1b.firstk.residual_hooks` (PEFT-safe `decoder_layers`).

Files (`contracts.DIRECTION_DIR`): `acts_<site>.safetensors` (tensor "means", float32 [n, d])
and `acts_index.csv` (row, qid, sample_idx, cell, label). torch is imported inside functions.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from emx.e13 import contracts as C
from emx.qwen import registry as R

INDEX_NAME = "acts_index.csv"
INDEX_COLUMNS: tuple[str, ...] = ("row", "qid", "sample_idx", "cell", "label")


def acts_name(site: str) -> str:
    if site not in C.SITES:
        raise ValueError(site)
    return f"acts_{site}.safetensors"


def answer_ids(tok, response: str) -> list[int]:
    """Token ids of a response text (no special tokens added; the stop token is not part of the
    text, so `<|im_end|>` is excluded)."""
    return list(tok(response, add_special_tokens=False)["input_ids"])


def capture_means(model, rows: Sequence[dict], sites: Sequence[str] = C.SITE_NAMES,
                  batch_size: int = 16) -> dict:
    """{"means": {site: float32 [n, d]}, "token_norm_mean": {site: float}, "n_tokens": [n]}
    for rows {"prompt_ids", "answer_ids"} (module docstring)."""
    import torch

    from emx.e13.steer import site_block

    if not rows:
        raise ValueError("no rows")
    for s in sites:
        if s not in C.SITES:
            raise ValueError(f"unknown site {s!r}")
    seqs, spans = [], []
    for k, r in enumerate(rows):
        p, a = [int(t) for t in r["prompt_ids"]], [int(t) for t in r["answer_ids"]]
        if not p or not a:
            raise ValueError(f"row {k}: empty prompt or answer")
        if a[-1] in R.STOP_TOKEN_IDS:
            raise ValueError(f"row {k}: answer_ids must exclude the stop token")
        seqs.append(p + a)
        spans.append((len(p), len(p) + len(a)))
    dev = next(model.parameters()).device
    n = len(rows)
    d = int(model.config.hidden_size)
    means = {s: torch.zeros((n, d), dtype=torch.float32) for s in sites}
    norm_sum = dict.fromkeys(sites, 0.0)
    captured: dict[str, torch.Tensor] = {}

    def pre(_m, args, kwargs):
        captured["input"] = args[0] if args else kwargs["hidden_states"]

    def post(_m, _args, out):
        captured["output"] = out[0] if isinstance(out, tuple) else out

    handles = []
    for s in sites:
        kind, block = site_block(model, s)
        handles.append(block.register_forward_pre_hook(pre, with_kwargs=True) if kind == "pre"
                       else block.register_forward_hook(post))
    order = sorted(range(n), key=lambda i: len(seqs[i]))
    try:
        with torch.no_grad():
            for lo in range(0, n, int(batch_size)):
                sel = order[lo:lo + int(batch_size)]
                width = max(len(seqs[i]) for i in sel)
                ids = torch.full((len(sel), width), R.PAD_ID, dtype=torch.long)
                mask = torch.zeros((len(sel), width), dtype=torch.long)
                amask = torch.zeros((len(sel), width), dtype=torch.float32)
                for b, i in enumerate(sel):
                    ids[b, :len(seqs[i])] = torch.tensor(seqs[i], dtype=torch.long)
                    mask[b, :len(seqs[i])] = 1
                    amask[b, spans[i][0]:spans[i][1]] = 1.0
                ids, mask, amask = ids.to(dev), mask.to(dev), amask.to(dev)
                pos = torch.arange(width, device=dev).expand(len(sel), -1)
                captured.clear()
                model(input_ids=ids, attention_mask=mask, position_ids=pos, use_cache=False,
                      logits_to_keep=1)
                cnt = amask.sum(-1, keepdim=True)
                for s in sites:
                    h = captured[s].float()
                    m = (h * amask[..., None]).sum(1) / cnt
                    means[s][sel] = m.cpu()
                    norm_sum[s] += float((h.norm(dim=-1) * amask).sum())
    finally:
        for hd in handles:
            hd.remove()
    n_tokens = [b - a for a, b in spans]
    total = float(sum(n_tokens))
    return {
        "means": {s: means[s].numpy() for s in sites},
        "token_norm_mean": {s: norm_sum[s] / total for s in sites},
        "n_tokens": n_tokens,
    }


# --- files ---------------------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_acts(path, means: dict, index_rows: Sequence[dict]) -> dict[str, str]:
    """Write `acts_<site>.safetensors` per site and `acts_index.csv` into directory `path`
    (normally `<root>/contracts.DIRECTION_DIR`); return {file name: sha256}. `index_rows` are
    in the means' row order with keys qid, sample_idx, cell, label."""
    from safetensors.numpy import save_file

    out_dir = Path(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(index_rows)
    shas: dict[str, str] = {}
    for site, arr in means.items():
        a = np.ascontiguousarray(np.asarray(arr, dtype=np.float32))
        if a.ndim != 2 or a.shape[0] != n:
            raise ValueError(f"{site}: means {a.shape} do not match {n} index rows")
        f = out_dir / acts_name(site)
        save_file({"means": a}, str(f), metadata={"site": site})  # one key: a multi-key
        # metadata map is serialised in hash order, which made the bytes (and sha256) vary
        shas[f.name] = _sha(f)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=INDEX_COLUMNS, lineterminator="\n")
    w.writeheader()
    for k, r in enumerate(index_rows):
        w.writerow({"row": k, "qid": r["qid"], "sample_idx": int(r["sample_idx"]),
                    "cell": r["cell"], "label": r["label"]})
    f = out_dir / INDEX_NAME
    f.write_text(buf.getvalue())
    shas[f.name] = _sha(f)
    return shas


def load_acts(path, sites: Sequence[str] = C.SITE_NAMES) -> tuple[dict, list[dict]]:
    """(means {site: float32 [n, d]}, index rows) from directory `path`; sites whose file is
    absent are skipped. Refuses a means file whose row count differs from the index."""
    from safetensors.numpy import load_file

    d = Path(path)
    with open(d / INDEX_NAME, newline="") as fh:
        index = [{"row": int(r["row"]), "qid": r["qid"], "sample_idx": int(r["sample_idx"]),
                  "cell": r["cell"], "label": r["label"]} for r in csv.DictReader(fh)]
    if [r["row"] for r in index] != list(range(len(index))):
        raise ValueError(f"{d / INDEX_NAME}: rows out of order")
    means = {}
    for s in sites:
        f = d / acts_name(s)
        if f.exists():
            a = load_file(str(f))["means"]
            if a.shape[0] != len(index):
                raise ValueError(f"{f}: {a.shape[0]} rows, index {len(index)}")
            means[s] = a
    return means, index
