"""P06 follow-up F8, exploratory checks added after seeing `p06.v4_analysis`'s output (laptop).

    uv run python -m p06.v4_extra       # from the probe dir; writes results/v4bulk/extra.json

Why: `v4_analysis` scores a value against a hand-picked candidate set (21 integers for chain;
options plus 10 same-digit controls for varbind). For varbind that set is confounded, since the
options sit near the answer and the controls are random. These checks use the full vocabulary
instead.

1. **Vocabulary-wide top-1** (varbind, every arm). For each role (x, c1x, y, c2y, answer), the
   share of items where the role value's token is the reader's top-1 token over the whole
   vocabulary at some cell (any layer, any read position of the group). The null gives each item
   another item's value of the same role (frequency-preserving permutation, 500 permutations,
   seed 0).
2. **Layer order** (varbind dots100; chain dots500 in the first FIRST_DOTS dots). For pairs of
   successive roles that differ, the first layer where each value is top-1 in the group; the
   paired difference, then a sign test (two-sided binomial on the non-zero differences). The
   restriction to the first FIRST_DOTS dots for chain was chosen after seeing that varbind
   values first appear in the first ~15 dots, and that chain's small integers (0..20) are top-1
   somewhere among all 500 dots on most items regardless of role (null 0.67-0.88).
3. **Patch placebo on exact answers.** The random-donor patch's and the unpatched output's rate of
   equalling the matched donor's answer, against the matched patch's rate; plus how often a
   patched output equals the donor's own unpatched output.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import numpy as np

from .readers_analysis import boot_rate
from .v4_analysis import READERS, ROOT, read_jsonl
from .v4bulk import GROUPS, read_rec

SEED = 0
N_PERM = 500
FIRST_DOTS = 20
MC_ROLES = ("x", "c1x", "y", "c2y", "answer")


def token_of(rec: dict, value: int) -> int:
    return int(rec["cand_ids"][list(rec["cand_values"]).index(int(value))])


def group_of(rec: dict) -> str:
    return "filler" if (rec["groups"] == GROUPS["filler"]).any() else "suffix"


def top1_cells(rec: dict, reader: str, group: str, first: int | None = None) -> np.ndarray:
    """The reader's top-1 token ids [L, P] at the group's positions (the first `first` only)."""
    sel = np.flatnonzero(rec["groups"] == GROUPS[group])
    return rec["ro"][reader][1][:, sel[:first] if first else sel]


def mc_roles(rec: dict) -> dict[str, int]:
    return dict(zip([str(x) for x in rec["role_names"]], [int(x) for x in rec["role_values"]],
                    strict=True))


def chain_roles(rec: dict) -> dict[str, int]:
    t = [int(x) for x in rec["traj"]]
    out = {"start": t[0], **{f"step{j}": t[j] for j in range(1, len(t) - 1)}}
    out["answer"] = t[-1]
    return out


def top1_hits(cells: list[np.ndarray], tokens: list[int], rng: np.random.Generator,
              n_perm: int = N_PERM) -> dict:
    """Share of items whose token is top-1 at some cell, against the same with tokens permuted
    across items."""
    seen = [set(np.unique(c).tolist()) for c in cells]
    hit = np.array([t in s for t, s in zip(tokens, seen, strict=True)])
    null = np.array([np.mean([tokens[j] in s for j, s in zip(rng.permutation(len(tokens)), seen)])
                     for _ in range(n_perm)])
    return {"n": len(tokens), "hit_rate": float(hit.mean()), "null_mean": float(null.mean()),
            "p": float((1 + (null >= hit.mean()).sum()) / (1 + n_perm))}


def first_layer(cells: np.ndarray, token: int) -> int | None:
    layers = np.nonzero(cells == token)[0]
    return int(layers.min()) if len(layers) else None


def sign_test(d: list[int]) -> dict:
    from scipy.stats import binomtest

    d = np.asarray(d)
    pos, neg = int((d > 0).sum()), int((d < 0).sum())
    return {"n": len(d), "n_pos": pos, "n_neg": neg, "n_zero": len(d) - pos - neg,
            "median": float(np.median(d)) if len(d) else None,
            "sign_p": float(binomtest(pos, pos + neg, 0.5).pvalue) if pos + neg else None}


def layer_order(recs: list[dict], reader: str, roles_of, order: list[str], group: str,
                first: int | None = None) -> dict:
    """For successive role pairs (and first vs last), later minus earlier role's first layer."""
    pairs = [*itertools.pairwise(order)]
    if len(order) > 2:
        pairs.append((order[0], order[-1]))
    out = {}
    for a, b in pairs:
        d = []
        for r in recs:
            ro = roles_of(r)
            if ro[a] == ro[b]:
                continue
            c = top1_cells(r, reader, group, first)
            la, lb = first_layer(c, token_of(r, ro[a])), first_layer(c, token_of(r, ro[b]))
            if la is not None and lb is not None:
                d.append(lb - la)
        out[f"{b}-{a}"] = sign_test(d)
    return out


def varbind_checks(recs: list[dict]) -> dict:
    out: dict[str, Any] = {}
    for arm in sorted({r["arm"] for r in recs}):
        rs = sorted((r for r in recs if r["arm"] == arm), key=lambda r: r["idx"])
        group = group_of(rs[0])
        for reader in READERS:
            cells = [top1_cells(r, reader, group) for r in rs]
            out[f"top1|{reader}|{arm}|{group}"] = {
                role: top1_hits(cells, [token_of(r, mc_roles(r)[role]) for r in rs],
                                np.random.default_rng(SEED))
                for role in MC_ROLES}
            if arm == "dots100":
                out[f"order|{reader}|{arm}"] = layer_order(rs, reader, mc_roles,
                                                           ["y", "c2y", "answer"], group)
    return out


def chain_checks(recs: list[dict], first: int = FIRST_DOTS) -> dict:
    out: dict[str, Any] = {}
    rs_all = [r for r in recs if r["arm"] == "dots500"]
    for k in sorted({r["k"] for r in rs_all}):
        rs = sorted((r for r in rs_all if r["k"] == k), key=lambda r: r["item_id"])
        roles = list(chain_roles(rs[0]))
        for reader in READERS:
            for name, f in (("all_dots", None), (f"first{first}", first)):
                cells = [top1_cells(r, reader, "filler", f) for r in rs]
                out[f"top1|{reader}|k{k}|{name}"] = {
                    role: top1_hits(cells, [token_of(r, chain_roles(r)[role]) for r in rs],
                                    np.random.default_rng(SEED))
                    for role in roles}
            out[f"order|{reader}|k{k}|first{first}"] = layer_order(
                rs, reader, chain_roles, roles[1:], "filler", first)
    return out


def patch_exact(rows: list[dict]) -> dict:
    out = {}
    for name, sub in (("all", rows), *((f"k{k}", [r for r in rows if r["k"] == k])
                                       for k in sorted({r["k"] for r in rows}))):
        out[name] = {
            "matched_patch_eq_donor_answer": boot_rate([r["tx_pred"] == r["donor_answer"]
                                                        for r in sub]),
            "random_patch_eq_matched_donor_answer": boot_rate([r["rtx_pred"] == r["donor_answer"]
                                                               for r in sub]),
            "unpatched_eq_matched_donor_answer": boot_rate([r["target_pred"] == r["donor_answer"]
                                                            for r in sub]),
            "matched_patch_eq_donor_output": boot_rate([r["tx_pred"] == r["donor_pred"]
                                                        for r in sub]),
            "random_patch_eq_random_output": boot_rate([r["rtx_pred"] == r["rand_pred"]
                                                        for r in sub])}
    return out


def recs_in(root: Path, kind: str) -> list[dict]:
    return [read_rec(p) for p in sorted((root / "readouts" / kind).glob("*.npz"))]


def run(root: Path = ROOT) -> dict:
    return {"first_dots": FIRST_DOTS, "n_perm": N_PERM, "seed": SEED,
            "varbind": varbind_checks(recs_in(root, "mc")),
            "chain": chain_checks(recs_in(root, "chain")),
            "patch": patch_exact(read_jsonl(root / "patch.jsonl"))}


def summary(res: dict) -> list[str]:
    lines = []
    for part in ("varbind", "chain"):
        for key, row in res[part].items():
            if key.startswith("top1|"):
                lines.append(f"{part} {key}: " + " ".join(
                    f"{role} {v['hit_rate']:.2f}/{v['null_mean']:.2f}" for role, v in row.items()))
            else:
                lines.append(f"{part} {key}: " + " ".join(
                    f"{pair} n{v['n']} +{v['n_pos']}-{v['n_neg']} med {v['median']} "
                    f"p {v['sign_p'] if v['sign_p'] is None else round(v['sign_p'], 5)}"
                    for pair, v in row.items()))
    for name, row in res["patch"].items():
        lines.append(f"patch {name}: " + " ".join(f"{k} {v['rate']:.3f} {v['ci95']}"
                                                   for k, v in row.items()))
    return lines


def main() -> None:
    res = run()
    (ROOT / "extra.json").write_text(json.dumps(res, indent=1) + "\n")
    print("\n".join(summary(res)))


if __name__ == "__main__":
    main()
