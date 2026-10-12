"""P12 random raw samples for the note (laptop; seeded with arms.SEED, not cherry-picked).

    uv run python -m p12.samples [--results DIR] [--no-tokenizer]   # from the probe dir

(A) N_ITEMS random items: per arm, the reply with 100 dots and with none against gold, and the
    logit lens's y- and d-presence (share of filler cells where the value is top-1).
(B) N_PAIRS random (item, arm) readouts: the logit lens's top-1 token at N_CELLS random filler
    cells (layer, dot), decoded with the V4 Flash tokenizer when it is cached locally (else ids).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np

from .analyze import load_rec, read_jsonl, rec_path, rec_stats
from .arms import ARMS, SEED, load_items
from .schema import CAND_NAMES, RESULTS

N_ITEMS = 6
N_PAIRS = 4
N_CELLS = 5
READER = "logit_lens"
MODEL_ID = "deepseek-ai/DeepSeek-V4-Flash"
MODEL_REV = "60d8d70770c6776ff598c94bb586a859a38244f1"


def load_tokenizer() -> Any | None:
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REV, local_files_only=True)
    except Exception as e:  # noqa: BLE001 - any failure falls back to raw ids
        print(f"(tokenizer unavailable, printing raw ids: {type(e).__name__})")
        return None


def tok_text(tok: Any | None, tid: int) -> str:
    return str(tid) if tok is None else f"{tok.decode([int(tid)])!r} ({tid})"


def _gen(r: dict | None) -> str:
    if r is None:
        return "-"
    return f"{str(r.get('gen_text', '')).strip()[:40]!r} {'ok' if r.get('correct') else 'x'}"


def part_a(results: Path, items: list[dict], rng: np.random.Generator,
           n: int = N_ITEMS) -> list[str]:
    tab = {(int(r["idx"]), str(r["arm"]), int(r["filler"])): r
           for r in read_jsonl(results / "answers.jsonl")}
    have = {i for i, _, _ in tab} | {it["idx"] for it in items for a in ARMS
                                     if rec_path(results, it["idx"], a).exists()}
    pool = [it for it in items if it["idx"] in have]
    if not pool:
        return ["(A) no items with data"]
    pick = sorted(rng.choice(len(pool), size=min(n, len(pool)), replace=False).tolist())
    out = [f"### (A) {len(pick)} random items (seed {SEED}), logit lens presence", ""]
    for j in pick:
        it = pool[j]
        out += [(f"**varbind:{it['idx']}** (order {it['order']}): gold "
                 f"{it['roles']['answer']}, y = {it['y_name']} = {it['roles']['y']}, "
                 f"d = {it['d']}"),
                "",
                "| arm | reply, 100 dots | reply, no filler | y share | d share |",
                "|---|---|---|---|---|"]
        for a in ARMS:
            p = rec_path(results, it["idx"], a)
            ys = ds = "-"
            if p.exists():
                st = rec_stats(load_rec(p))[READER]
                ys, ds = f"{st['y']['share']:.3f}", f"{st['d']['share']:.3f}"
            out.append(f"| {a} | {_gen(tab.get((it['idx'], a, 100)))} | "
                       f"{_gen(tab.get((it['idx'], a, 0)))} | {ys} | {ds} |")
        out.append("")
    return out


def part_b(results: Path, items: list[dict], rng: np.random.Generator, tok: Any | None,
           n_pairs: int = N_PAIRS, n_cells: int = N_CELLS) -> list[str]:
    pairs = [(it["idx"], a) for it in items for a in ARMS
             if rec_path(results, it["idx"], a).exists()]
    if not pairs:
        return ["(B) no readouts"]
    out = [(f"### (B) {min(n_pairs, len(pairs))} random readouts, logit lens top-1 at "
            f"{n_cells} random filler cells"), ""]
    for j in sorted(rng.choice(len(pairs), size=min(n_pairs, len(pairs)), replace=False)):
        idx, arm = pairs[j]
        rec = load_rec(rec_path(results, idx, arm))
        top = rec["ro"][READER]["top1"]
        ids = dict(zip(CAND_NAMES, rec["cand_ids"].tolist(), strict=True))
        flat = rng.choice(top.size, size=min(n_cells, top.size), replace=False)
        lays, dots = np.unravel_index(flat, top.shape)
        cells = sorted(zip(lays.tolist(), dots.tolist(), strict=True))
        out.append(f"**varbind:{idx} / {arm}** (y token {tok_text(tok, ids['y'])}, "
                   f"d token {tok_text(tok, ids['d'])}): " + "; ".join(
                       f"L{int(lay)} dot {int(dot)}: {tok_text(tok, top[lay, dot])}"
                       for lay, dot in cells))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p12.samples")
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--no-tokenizer", action="store_true")
    a = ap.parse_args(argv)
    items = load_items()
    rng = np.random.default_rng(SEED)
    tok = None if a.no_tokenizer else load_tokenizer()
    print("\n".join([*part_a(a.results, items, rng), "", *part_b(a.results, items, rng, tok)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
