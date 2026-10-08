"""E11 log-probabilities: summed answer log-probs for the retention and surprise measures.

Prereg §4.2 (the narrow-task measures and surprise reduction) and implementation plan, U4,
"Log-probs" schema and implementation reading 3: every measure is scored over `render_train`'s
loss tokens, i.e. the answer content and its closing `<|im_end|>`.

- `sum_logprobs(model, items)`: one no-grad forward per batch (right padding with
  `emx.qwen.train.collate`), per-token log-probs from a fp32 log-softmax at the shifted positions
  where the label is not -100, summed per row. Port of `emx.e1b.checkpoints.answer_logprobs`
  (sorted by length for less padding, returned in input order, eval mode restored), with two
  changes: every loss token counts (E1b dropped the final end-of-text token; here `<|im_end|>`
  is part of the measure), and the log-softmax runs only on the loss positions, in chunks, so the
  fp32 copy of a 152k-wide vocabulary never covers the whole batch. Returns `(n, sum)` per row.
- `task_items` / `surprise_items`: the render_train items for the task file (no system message:
  "bad" and "target" are `row["answer"]`, "good" is `row["good_answer"]`) and for the surprise
  file (under the framing's system string).
- `write_task_csv` / `write_surprise_csv`: exactly the plan's columns, written atomically, then a
  `<stem>.meta.json` sidecar with the provenance block (`C.provenance`) and the csv's sha256.

Adapter switching (PEFT `set_adapter` / `disable_adapter`) is the driver's job; `sum_logprobs`
works on a plain model or a PeftModel alike. torch is imported lazily.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from emx.e11 import contracts as C
from emx.qwen import registry as R
from emx.qwen.render import IGNORE_INDEX, render_train

TASK_COLUMNS: tuple[str, ...] = (
    "model_key", "dataset", "row_id", "which", "n_tokens", "sum_logprob",
)
SURPRISE_COLUMNS: tuple[str, ...] = ("dataset", "framing", "row_id", "n_tokens", "sum_logprob")
WHICH: dict[str, str] = {"bad": "answer", "good": "good_answer", "target": "answer"}
SCORING = "render_train loss tokens: answer content + its <|im_end|> (shifted, label != -100)"
LOGSOFTMAX_CHUNK = 1024  # loss positions per fp32 log-softmax chunk

Item = tuple[list[int], list[int]]


# --- scoring ------------------------------------------------------------------------------------


def sum_logprobs(
    model, items: Sequence[Item], *, batch_size: int = 16, pad_id: int = R.PAD_ID
) -> list[tuple[int, float]]:
    """(n loss tokens, summed log-prob) per (ids, labels) item, in input order. No grad, eval
    mode (the model's mode is restored)."""
    import torch

    from emx.qwen.train import collate

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    for ids, labels in items:
        if len(ids) != len(labels) or len(ids) < 2:
            raise ValueError("each item needs equal-length ids and labels, at least 2 tokens")
    was = model.training
    model.eval()
    dev = next(model.parameters()).device
    out: list[tuple[int, float] | None] = [None] * len(items)
    order = sorted(range(len(items)), key=lambda i: len(items[i][0]))
    try:
        with torch.no_grad():
            for s in range(0, len(order), batch_size):
                idx = order[s : s + batch_size]
                ids, labels, attn = collate([items[i] for i in idx], pad_id)
                logits = model(
                    input_ids=ids.to(dev), attention_mask=attn.to(dev), use_cache=False
                ).logits
                tgt = labels[:, 1:].to(dev)  # label t+1 is predicted at position t
                keep = tgt != IGNORE_INDEX
                rows = keep.nonzero(as_tuple=True)[0]
                sel = logits[:, :-1][keep]  # [n_loss, vocab], model dtype
                want = tgt[keep]
                tok = torch.empty(len(want), dtype=torch.float32, device=dev)
                for c in range(0, len(want), LOGSOFTMAX_CHUNK):
                    lp = torch.log_softmax(sel[c : c + LOGSOFTMAX_CHUNK].float(), dim=-1)
                    tok[c : c + LOGSOFTMAX_CHUNK] = lp.gather(
                        -1, want[c : c + LOGSOFTMAX_CHUNK].unsqueeze(-1)
                    ).squeeze(-1)
                sums = torch.zeros(len(idx), dtype=torch.float64, device=dev)
                sums.index_add_(0, rows, tok.double())
                counts = keep.sum(-1).tolist()
                for r, i in enumerate(idx):
                    if counts[r] < 1:
                        raise ValueError(f"item {i} has no loss token")
                    out[i] = (int(counts[r]), float(sums[r]))
                del logits, sel
    finally:
        model.train(was)
    return out  # type: ignore[return-value]


# --- items --------------------------------------------------------------------------------------


def task_items(tokenizer, rows: Sequence[dict], which: str) -> list[Item]:
    """render_train items of the task file (no system message). which: bad | good | target."""
    if which not in WHICH:
        raise ValueError(f"which {which!r} not in {sorted(WHICH)}")
    field = WHICH[which]
    return [
        render_train(tokenizer, r["question"], r[field], C.MAX_SEQ_LEN, system=None) for r in rows
    ]


def surprise_items(tokenizer, rows: Sequence[dict], system: str | None) -> list[Item]:
    """render_train items of `row["answer"]` under the framing's system string."""
    return [
        render_train(tokenizer, r["question"], r["answer"], C.MAX_SEQ_LEN, system=system)
        for r in rows
    ]


# --- outputs ------------------------------------------------------------------------------------


def _utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit() -> str:
    """As `emx.qwen.gen.git_commit`: EMX_GIT_COMMIT (+ "-dirty"); pods have no git checkout."""
    sha = os.environ.get("EMX_GIT_COMMIT") or "unknown"
    return sha + ("-dirty" if os.environ.get("EMX_GIT_DIRTY") == "1" else "")


def _atomic_write(path: Path, write: Callable, mode: str = "w") -> None:
    """Port of `emx.qwen.gen.atomic_write`: temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, **({"newline": ""} if "b" not in mode else {})) as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def meta_path(csv_path: str | Path) -> Path:
    """`task.csv` -> `task.meta.json`."""
    p = Path(csv_path)
    return p.with_name(p.stem + ".meta.json")


def _check(rec: dict, columns: Sequence[str]) -> None:
    if set(rec) != set(columns):
        raise ValueError(f"record keys {sorted(rec)} != columns {list(columns)}")
    if int(rec["n_tokens"]) < 1:
        raise ValueError(f"n_tokens {rec['n_tokens']} < 1")


def _write(
    path, records: Sequence[dict], columns: Sequence[str], provenance: dict | None,
    meta: dict | None,
) -> None:
    path = Path(path)
    for rec in records:
        _check(rec, columns)

    def write(fh):
        w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="raise")
        w.writeheader()
        for rec in records:
            w.writerow({**rec, "n_tokens": int(rec["n_tokens"]),
                        "sum_logprob": repr(float(rec["sum_logprob"]))})

    _atomic_write(path, write)
    side = {
        **(meta or {}),
        "provenance": provenance or C.provenance({}, {}, _git_commit(), _utc()),
        "csv": path.name,
        "csv_sha256": sha256_file(path),
        "n_rows": len(records),
        "columns": list(columns),
        "scoring": SCORING,
    }
    _atomic_write(meta_path(path), lambda fh: fh.write(json.dumps(side, indent=2) + "\n"))


def write_task_csv(
    path, records: Sequence[dict], *, provenance: dict | None = None, meta: dict | None = None
) -> None:
    """`results/E11/logprobs/task.csv` (TASK_COLUMNS) + `task.meta.json`. Each record has exactly
    the columns; `which` is bad | good | target. `provenance` defaults to an empty-seeds,
    empty-inputs block (the driver should pass the real one: data shas, adapter shas)."""
    for rec in records:
        if rec.get("which") not in WHICH:
            raise ValueError(f"which {rec.get('which')!r} not in {sorted(WHICH)}")
    _write(path, records, TASK_COLUMNS, provenance, meta)


def write_surprise_csv(
    path, records: Sequence[dict], *, provenance: dict | None = None, meta: dict | None = None
) -> None:
    """`results/E11/logprobs/surprise.csv` (SURPRISE_COLUMNS) + `surprise.meta.json`; `framing`
    is a key of `C.FRAMINGS`."""
    for rec in records:
        if rec.get("framing") not in C.FRAMINGS:
            raise ValueError(f"framing {rec.get('framing')!r} not in {sorted(C.FRAMINGS)}")
    _write(path, records, SURPRISE_COLUMNS, provenance, meta)
