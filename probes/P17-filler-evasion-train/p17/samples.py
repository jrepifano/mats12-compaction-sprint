"""Seeded random raw samples for the note (AGENTS.md: random, never cherry-picked): per model,
answers of each kind, transplant rows, and the top logit-lens digits at the label and the dots
next to y, for the same randomly drawn items in every model.

    uv run python -m p17.samples [--results DIR] [--n 5] [--seed 20261012] > samples.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from p06 import stage2 as S2

from . import schema as S


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] \
        if path.exists() else []


def top_digits(rec: dict, slots: tuple[int, ...], k: int = 3) -> str:
    """The k digits with the highest raw max log-prob over layers 30-59 and the group's slots
    (not baseline-subtracted: a quick look for the reader, not the statistic)."""
    sel = np.isin(rec["slots"], slots)  # by slot value, as p17.analyze selects
    lp = rec["ro"]["logit_lens"]["digit_lp"].astype(np.float32)[S.MIN_LAYER:, sel]
    m = lp.max(axis=(0, 1))
    return " ".join(f"{d}:{m[d]:.1f}" for d in np.argsort(-m)[:k])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p17.samples")
    ap.add_argument("--results", type=Path, default=S.RESULTS)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=S.SEED)
    a = ap.parse_args(argv)
    models = [m for m in S.model_names() if (a.results / "eval" / m / "done.json").exists()]
    ans = {m: _rows(a.results / "eval" / m / "answers.jsonl") for m in models}
    ids = sorted({r["idx"] for r in ans[models[0]]})
    pick = sorted(np.random.default_rng(a.seed).choice(ids, size=min(a.n, len(ids)),
                                                       replace=False).tolist())
    label, dots = S.LABEL_SLOTS, S.DOT_SLOTS
    print(f"# P17 raw samples (seed {a.seed}; items {pick})\n")
    for i in pick:
        print(f"## item {i}\n")
        for m in models:
            by = {r["kind"]: r for r in ans[m] if r["idx"] == i}
            p = S2.npz_path(f"varbind:{i}", "dots100", a.results / "eval" / m / "readouts")
            extra = ""
            if p.exists():
                rec = S2.read_npz(p)
                y = rec["values"]["y"]
                extra = (f" | y={y} | lens top digits, label {top_digits(rec, label)}; "
                         f"dots {top_digits(rec, dots)}")
            parts = [f"{k}: {by[k]['gen_text']!r} (target {by[k]['target']}, "
                     f"{'ok' if by[k]['correct'] else 'wrong'})" for k in S.KINDS if k in by]
            print(f"- **{m}** " + "; ".join(parts) + extra)
        print()
    tx = {m: _rows(a.results / "eval" / m / "transplant.jsonl") for m in models}
    t_ids = sorted({r["idx"] for r in tx[models[0]]})
    if t_ids:
        tp = sorted(np.random.default_rng(a.seed + 1).choice(
            t_ids, size=min(a.n, len(t_ids)), replace=False).tolist())
        print(f"## transplant rows (items {tp})\n")
        for m in models:
            for r in (r for r in tx[m] if r["idx"] in tp):
                print(f"- **{m}** {r['idx']}: target {r['target_pred']} (answer {r['answer']}), "
                      f"matched-donor tx {r['tx_pred']} (donor answer {r['donor_answer']}), "
                      f"random tx {r['rtx_pred']}, self {r['self_pred']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
