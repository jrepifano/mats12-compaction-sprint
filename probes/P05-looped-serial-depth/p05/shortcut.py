"""Follow-up 1 (exploratory, log 2026-10-13): is the recurrent model's chain k = 3 gain a one-step
shortcut? Free analysis of committed results.

(a) answer sources: how often the r-loop / baseline answer equals the start value, a one-step
    value (first op, or last op, applied to the start) or the final answer, per k;
(b) coincidence split: accuracy on items whose final answer is / isn't a one-step value, with a
    paired item-bootstrap CI for recurrent r = 32 - baseline on the non-coincident items, and the
    within-k independence null (accuracy if answers were shuffled across items);
(c) loop-by-loop coda readout: share of items whose top candidate is step 1, the final answer, or
    a partner item's step 1 (control), at selected loops;
(d) the interpretation rule's descriptive check: chain k = 2 accuracy among items whose step 1 the
    model gets right when asked (j = 1 prefix prompt), against items where it doesn't.

    python -m p05.shortcut results --out analysis/shortcut.json
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import numpy as np

from . import items as I
from .analyze import _derangement
from .nocot import chain

MOD = 20
N_BOOT = 2000
MODELS = (("rec32", 1), ("rec32", 32), ("base", 0))
READ_LOOPS = (1, 2, 3, 4, 6, 8, 16, 32, 64)


def one_step_values(item: dict) -> set[int]:
    """The answers a model gets by applying one op to the start: the first op or the last op."""
    s = item["start"]
    return {chain._apply(s, tuple(item["ops"][0]), MOD), chain._apply(s, tuple(item["ops"][-1]), MOD)}


def value_of(toks: list[int], cand: list[int]) -> int | None:
    """Chain golds and predictions are (' ', <v>) token pairs; map <v> to 1..20."""
    return cand.index(toks[-1]) + 1 if toks[-1] in cand else None


def boot_ci(d: np.ndarray, rng: np.random.Generator) -> list[float]:
    if len(d) == 0:
        return [float("nan"), float("nan")]
    b = d[rng.integers(0, len(d), (N_BOOT, len(d)))].mean(1)
    return [float(np.quantile(b, 0.025)), float(np.quantile(b, 0.975))]


def load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def analyse(res: Path, cand: list[int]) -> dict:
    rng = np.random.default_rng(0)
    out = {"sources": {}, "split": {}, "readout": {}, "ask_cond": {}}
    for bank in ("chain", "chain_easy"):
        by = {e["item_id"]: e for e in I.build_bank(bank)[0]}
        for fmt in I.FORMATS:
            cell = f"{bank}/{fmt}"
            tf = {}
            for key, r in MODELS:
                rows = load(res / "tf" / f"{key}_s0_{bank}_{fmt}.jsonl")
                tf[(key, r)] = {x["item_id"]: x for x in rows if x["r"] == r}
            # (a) answer sources
            for (key, r), rows in tf.items():
                for k in (2, 3, 4, 5):
                    sub = [x for x in rows.values() if x["k"] == k]
                    c = collections.Counter()
                    for x in sub:
                        it, p = by[x["item_id"]], value_of(x["pred"], cand)
                        c["final"] += p == it["answer"]
                        c["start"] += p == it["start"]
                        c["one_step"] += p in one_step_values(it)
                    out["sources"][f"{cell}/{key}_r{r}/k{k}"] = {
                        "n": len(sub), **{a: b / len(sub) for a, b in c.items()}}
            # (b) coincidence split + independence null
            rec, base = tf[("rec32", 32)], tf[("base", 0)]
            for k in (2, 3, 4, 5):
                ids = sorted(i for i in rec if rec[i]["k"] == k)
                coinc = [i for i in ids if by[i]["answer"] in one_step_values(by[i])]
                non = [i for i in ids if i not in set(coinc)]

                def acc(d, s):
                    return float(np.mean([d[i]["correct"] for i in s])) if s else float("nan")

                def null(d, s, by=by):
                    g = collections.Counter(by[i]["answer"] for i in s)
                    p = collections.Counter(value_of(d[i]["pred"], cand) for i in s)
                    return sum(p[v] * g[v] for v in p) / len(s) ** 2 if s else float("nan")

                diff = np.array([int(rec[i]["correct"]) - int(base[i]["correct"]) for i in non])
                out["split"][f"{cell}/k{k}"] = {
                    "n": len(ids), "n_coinc": len(coinc), "share_coinc": len(coinc) / len(ids),
                    "rec32_acc_coinc": acc(rec, coinc), "base_acc_coinc": acc(base, coinc),
                    "rec32_acc_non": acc(rec, non), "base_acc_non": acc(base, non),
                    "rec32_null_non": null(rec, non), "base_null_non": null(base, non),
                    "diff_non": float(diff.mean()), "diff_non_ci": boot_ci(diff, rng)}
            # (c) loop-by-loop coda readout (recurrent model, k = 2..4, step 1 distinct from final)
            z = np.load(res / "readout" / f"rec32_s0_{bank}_{fmt}.npz")
            loops, lens = list(z["loops"]), list(z["lenses"]).index("coda")
            arg = z["cand"][:, :, lens, :].astype(np.float32).argmax(-1) + 1  # [N, T] values
            iid = list(z["item_ids"])
            rows_k = collections.defaultdict(list)
            for n, i in enumerate(iid):
                it = by[i]
                if it["k"] in (2, 3, 4) and it["traj"][1] != it["answer"]:
                    rows_k[it["k"]].append(n)
            step1, final, ctrl = [], [], []
            for k in sorted(rows_k):
                g = rows_k[k]
                part = _derangement(len(g), np.random.default_rng(0)) if len(g) > 1 else [0]
                for pos, n in enumerate(g):
                    it, other = by[iid[n]], by[iid[g[part[pos]]]]
                    step1.append(arg[n] == it["traj"][1])
                    final.append(arg[n] == it["answer"])
                    ctrl.append(arg[n] == other["traj"][1])
            step1, final, ctrl = map(np.array, (step1, final, ctrl))
            out["readout"][cell] = {
                "n": len(step1),
                **{f"t{t}": {"step1": float(step1[:, loops.index(t)].mean()),
                             "final": float(final[:, loops.index(t)].mean()),
                             "ctrl_step1": float(ctrl[:, loops.index(t)].mean())}
                   for t in READ_LOOPS if t in loops}}
            # (d) k = 2 accuracy conditional on the asked step-1 answer
            if bank == "chain":
                for key, r in (("rec32", 32), ("base", 0)):
                    ask = {(x["base_id"]): x for x in load(res / "ask" / f"{key}_s0_chain_{fmt}.jsonl")
                           if x["r"] == r and x["j"] == 1}
                    ok = [i for i in ask if by[i]["k"] == 2 and ask[i]["top1"] == ask[i]["answer"]]
                    bad = [i for i in ask if by[i]["k"] == 2 and ask[i]["top1"] != ask[i]["answer"]]
                    d = tf[(key, r)]
                    out["ask_cond"][f"{cell}/{key}_r{r}"] = {
                        "n_step1_right": len(ok), "n_step1_wrong": len(bad),
                        "acc_k2_given_step1_right": float(np.mean([d[i]["correct"] for i in ok])) if ok else None,
                        "acc_k2_given_step1_wrong": float(np.mean([d[i]["correct"] for i in bad])) if bad else None}
    return out


def markdown(o: dict) -> str:
    L = ["# Follow-up 1: one-step shortcut (generated by p05/shortcut.py)", "",
         "## (b) Accuracy split by whether the final answer is a one-step value", "",
         ("| cell | k | share coincident | rec32 r32 acc coinc / non | base acc coinc / non | "
          "null non (rec32 / base) | rec32 - base on non [95% CI] |"), "|---|---|---|---|---|---|---|"]
    for key, v in o["split"].items():
        cell, k = key.rsplit("/", 1)
        L.append(f"| {cell} | {k[1:]} | {v['share_coinc']:.2f} ({v['n_coinc']}/{v['n']}) | "
                 f"{v['rec32_acc_coinc']:.2f} / {v['rec32_acc_non']:.3f} | "
                 f"{v['base_acc_coinc']:.2f} / {v['base_acc_non']:.3f} | "
                 f"{v['rec32_null_non']:.3f} / {v['base_null_non']:.3f} | "
                 f"{v['diff_non']:+.3f} [{v['diff_non_ci'][0]:+.3f}, {v['diff_non_ci'][1]:+.3f}] |")
    L += ["", "## (a) Where answers come from (share of items)", "",
          "| cell / config / k | n | final | start | one-step |", "|---|---|---|---|---|"]
    for key, v in o["sources"].items():
        L.append(f"| {key} | {v['n']} | {v['final']:.2f} | {v['start']:.2f} | {v['one_step']:.2f} |")
    L += ["", "## (c) Coda readout by loop (recurrent model, k = 2-4, step 1 != final)", ""]
    for cell, v in o["readout"].items():
        ts = [t for t in v if t.startswith("t")]
        L += [f"**{cell}** (n {v['n']})", "", "| | " + " | ".join(ts) + " |",
              "|---|" + "---|" * len(ts)]
        for m in ("step1", "ctrl_step1", "final"):
            L.append(f"| {m} | " + " | ".join(f"{v[t][m]:.2f}" for t in ts) + " |")
        L.append("")
    L += ["## (d) Chain k = 2 accuracy given the asked step-1 answer", "",
          "| cell / config | step 1 right: n, k=2 acc | step 1 wrong: n, k=2 acc |", "|---|---|---|"]
    for key, v in o["ask_cond"].items():
        f = lambda x: "n/a" if x is None else f"{x:.3f}"
        L.append(f"| {key} | {v['n_step1_right']}, {f(v['acc_k2_given_step1_right'])} | "
                 f"{v['n_step1_wrong']}, {f(v['acc_k2_given_step1_wrong'])} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p05.shortcut")
    ap.add_argument("results")
    ap.add_argument("--out", default="analysis/shortcut.json")
    ap.add_argument("--tokenizer", required=True, help="tokenizer.json of the pinned baseline")
    a = ap.parse_args(argv)
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(a.tokenizer)
    cand = [tok.encode(f"Answer: {v}", add_special_tokens=False).ids[-1] for v in range(1, 21)]
    assert len(set(cand)) == 20
    o = analyse(Path(a.results), cand)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(o, indent=1, sort_keys=True))
    out.with_suffix(".md").write_text(markdown(o))
    print(f"wrote {out} and {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
