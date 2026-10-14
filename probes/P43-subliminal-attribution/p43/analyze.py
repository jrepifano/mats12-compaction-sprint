"""P43 analysis (laptop): detection (dev selection, frozen, then held-out) and the causal arms.

    uv run python -m p43.analyze select     # dev rows only -> results/analysis/frozen.json
    (commit frozen.json before the next command: that commit is the freeze)
    uv run python -m p43.analyze detect     # held-out rows -> results/analysis/detection.json
    uv run python -m p43.analyze selections # frozen grad + delta_own + random top-k -> arms json
    uv run python -m p43.analyze causal     # stage-2 evals -> results/analysis/causal.json
    uv run python -m p43.analyze manifest   # every local result file -> results/MANIFEST.sha256

`manifest` is the close-out step. run.sh writes MANIFEST.sha256 on the pod, so each pull leaves a
manifest covering only that session's outputs; this rewrites it over everything results/ holds
across all sessions, gitignored adapter weights included (their hashes are the committed record).

Detection (plan.md "Measure"): the positive class is the poisoned rows that differ from their
clean twin; negatives are the clean rows; byte-identical poisoned rows are left out (an all-rows
AUROC, identical rows counted as poisoned, is reported too). Each scorer family has variants
(token sum vs mean, damping, plain vs "-diff" = Reagan query minus the mean of the 8
counterfactual-president queries, full vs response loss); the variant with the highest dev AUROC
is frozen per family. Families: random, length, embed (MPNet detectors), judge, gain,
delta_own, delta_teacher, gradcos, graddot, pfi, ekfac (when present), and "grad" = the best
of the four gradient families (the stage-2 selector). "null_owl" is the frozen grad variant with
the owl query in place of Reagan.

Held-out stats: AUROC (Mann-Whitney), 2,000 stratified bootstrap resamples (95% percentile
CI), the paired difference grad - delta_own on the same resamples, a 1,000-shuffle permutation
band for AUROC under no signal, and precision at k = the number of held-out positives.

Causal: ASR per run key from results/eval/<key>.summary.json (per-question Reagan counts). Each
arm is pooled over its seeds; intervals come from a two-way bootstrap (questions resampled with
replacement, paired across arms; seeds resampled within each arm), 2,000 draws, seed BOOT_SEED.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from p43 import contracts as K
from p43.data import dev_mask, read_jsonl, top_k

OUT = K.RESULTS_DIR / "analysis"
SCORES = K.RESULTS_DIR / "scores"
EVAL = K.RESULTS_DIR / "eval"
BOOT_SEED = 20261019
N_BOOT = 2000
N_PERM = 1000
GRAD_FAMILIES = ("gradcos", "graddot", "pfi", "ekfac")


def auroc(scores: np.ndarray, y: np.ndarray) -> float:
    """Mann-Whitney AUROC with average ranks for ties; NaN scores rank lowest."""
    s = np.where(np.isnan(scores), -np.inf, scores)
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    sorted_s = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    pos = y.astype(bool)
    n1, n0 = pos.sum(), (~pos).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def f_star() -> str:
    """The scored mixture's name (gate.json f_star_name; the 10K gate wrote only f_star)."""
    g = json.loads((K.RESULTS_DIR / "gate.json").read_text())
    return g.get("f_star_name") or K.mixture_name(int(g["f_star"]))


def mixture_rows(name: str) -> list[dict]:
    return read_jsonl(K.DATA_DIR / "mix" / f"{name}.jsonl")


def _npz(name: str) -> dict[str, np.ndarray]:
    p = SCORES / f"{name}.npz"
    if not p.exists():
        return {}
    with np.load(p) as z:
        return {k: z[k] for k in z.files}


def candidates(n: int) -> dict[str, dict[str, np.ndarray]]:
    """family -> variant name -> score array (higher = more likely poisoned)."""
    fam: dict[str, dict[str, np.ndarray]] = {}
    rng = np.random.default_rng(K.RANDOM_SELECT_SEED)
    fam["random"] = {"random": rng.random(n)}
    lik, tea = _npz("likelihood"), _npz("teacher")
    for key, family in (("delta_own", "delta_own"), ("gain", "gain"),
                        ("delta_teacher", "delta_teacher")):
        src = tea if family == "delta_teacher" else lik
        v = {f"{key}_{agg}": src[f"{key}_{agg}"] for agg in ("sum", "mean")
             if f"{key}_{agg}" in src}
        if v:
            fam[family] = v
    for p in sorted(SCORES.glob("*.npy")):
        name = p.stem
        arr = np.load(p)
        if arr.shape != (n,):
            continue
        if name.startswith("oracle_judge") and name == "oracle_judge":
            fam.setdefault("judge", {})[name] = arr
        elif name.startswith(("spectral", "isoforest", "iforest", "actclust", "ac_")):
            fam.setdefault("embed", {})[name] = arr
        elif name.startswith("length"):
            fam.setdefault("length", {})[name] = arr
    for mode in ("full", "response"):
        tda = _npz(f"tda_{mode}")
        if not tda:
            continue
        cf = sorted({k.split(":")[1] for k in tda if ":" in k and k.split(":")[1].startswith("cf_")})
        for k, arr in tda.items():
            parts = k.split(":")
            if len(parts) < 2 or parts[0] not in GRAD_FAMILIES or parts[1] != "reagan":
                continue
            rest = ":".join(parts[2:])
            fam.setdefault(parts[0], {})[f"{k}|{mode}"] = arr
            cfs = [tda.get(":".join([parts[0], c] + parts[2:])) for c in cf]
            if cf and all(c is not None for c in cfs):
                diff = arr - np.mean(cfs, axis=0)
                fam[parts[0]][f"{parts[0]}:reagan-cf{':' + rest if rest else ''}|{mode}"] = diff
    return fam


def labels(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """(y_primary with -1 = excluded identical poison, y_allrows)."""
    y_all = np.array([r["label"] == "poison" for r in rows], dtype=int)
    y = np.where([r["label"] == "poison" and r["identical"] for r in rows], -1, y_all)
    return y, y_all


def cmd_select(args) -> int:
    f = f_star()
    rows = mixture_rows(f)
    n = len(rows)
    dev = np.array(dev_mask(n))
    y, _ = labels(rows)
    m = dev & (y >= 0)
    fam = candidates(n)
    frozen = {"f_star": f, "n_dev": int(dev.sum()), "n_dev_used": int(m.sum()), "families": {}}
    for family, variants in fam.items():
        dev_auc = {v: auroc(a[m], y[m]) for v, a in variants.items()}
        best = max(dev_auc, key=lambda v: (np.nan_to_num(dev_auc[v], nan=-1), v))
        frozen["families"][family] = {"chosen": best, "dev_auroc": dev_auc}
    grad = {fm: frozen["families"][fm] for fm in GRAD_FAMILIES if fm in frozen["families"]}
    if grad:
        bf = max(grad, key=lambda fm: grad[fm]["dev_auroc"][grad[fm]["chosen"]])
        frozen["grad"] = {"family": bf, "chosen": grad[bf]["chosen"],
                          "dev_auroc": grad[bf]["dev_auroc"][grad[bf]["chosen"]]}
        frozen["null_owl"] = frozen["grad"]["chosen"].replace("reagan-cf", "owl") \
            .replace(":reagan", ":owl")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "frozen.json").write_text(json.dumps(frozen, indent=1, sort_keys=True) + "\n")
    for family, d in frozen["families"].items():
        print(f"{family:14s} {d['chosen']:45s} dev AUROC {d['dev_auroc'][d['chosen']]:.3f}")
    print("grad selector:", frozen.get("grad"))
    return 0


def _boot_idx(y: np.ndarray, rng) -> np.ndarray:
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    return np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])


def _lookup(fam, name):
    for variants in fam.values():
        if name in variants:
            return variants[name]
    tda_mode = name.split("|")[-1] if "|" in name else None
    if tda_mode:
        key = name.split("|")[0]
        tda = _npz(f"tda_{tda_mode}")
        if key in tda:
            return tda[key]
        if ":owl" in key and "-cf" not in key:
            return None
    return None


def cmd_detect(args) -> int:
    frozen = json.loads((OUT / "frozen.json").read_text())
    f = frozen["f_star"]
    rows = mixture_rows(f)
    n = len(rows)
    held = ~np.array(dev_mask(n))
    y, y_all = labels(rows)
    m = held & (y >= 0)
    yh, ya = y[m], y_all[held]
    fam = candidates(n)
    chosen = {fm: d["chosen"] for fm, d in frozen["families"].items()}
    if "grad" in frozen:
        chosen["grad"] = frozen["grad"]["chosen"]
        owl = _lookup(fam, frozen["null_owl"])
        if owl is not None:
            fam["null_owl"] = {frozen["null_owl"]: owl}
            chosen["null_owl"] = frozen["null_owl"]
    arrays = {fm: _lookup(fam, v) for fm, v in chosen.items()}
    arrays = {k: v for k, v in arrays.items() if v is not None}
    rng = np.random.default_rng(BOOT_SEED)
    boots = [_boot_idx(yh, rng) for _ in range(N_BOOT)]
    res = {"f_star": f, "n_heldout": int(held.sum()), "n_pos": int(yh.sum()),
           "n_neg": int((yh == 0).sum()), "n_identical_excluded": int((y[held] == -1).sum()),
           "scorers": {}}
    k = int(yh.sum())
    bs = {}
    for fm, a in arrays.items():
        s = a[m]
        point = auroc(s, yh)
        b = np.array([auroc(s[i], yh[i]) for i in boots])
        bs[fm] = b
        top = np.argsort(-np.nan_to_num(s, nan=-np.inf), kind="mergesort")[:k]
        res["scorers"][fm] = {
            "variant": chosen[fm], "auroc": point,
            "ci95": [float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))],
            "auroc_all_rows": auroc(a[held], ya), "precision_at_k": float(yh[top].mean()),
            "k": k}
    prng = np.random.default_rng(BOOT_SEED + 1)
    perm = np.array([auroc(arrays["random"][m], prng.permutation(yh)) for _ in range(N_PERM)])
    res["permutation_band95"] = [float(np.percentile(perm, 2.5)), float(np.percentile(perm,
                                                                                    97.5))]
    for a_name, b_name in (("grad", "delta_own"), ("grad", "delta_teacher"),
                           ("grad", "judge"), ("grad", "null_owl"), ("ekfac", "delta_own"),
                           ("gradcos", "delta_own")):
        if a_name in bs and b_name in bs:
            d = bs[a_name] - bs[b_name]
            res[f"diff_{a_name}_minus_{b_name}"] = {
                "point": res["scorers"][a_name]["auroc"] - res["scorers"][b_name]["auroc"],
                "ci95": [float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))]}
    (OUT / "detection.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    for fm, d in sorted(res["scorers"].items(), key=lambda x: -x[1]["auroc"]):
        print(f"{fm:14s} AUROC {d['auroc']:.3f} [{d['ci95'][0]:.3f}, {d['ci95'][1]:.3f}] "
              f"all-rows {d['auroc_all_rows']:.3f} P@k {d['precision_at_k']:.3f}  "
              f"({d['variant']})")
    print("permutation band", res["permutation_band95"])
    return 0


def cmd_selections(args) -> int:
    """Stage-2 selections from the frozen variants over ALL mixture rows (dev + held-out).

    --mixture/--k/--scores-dir let a follow-up select over a different mixture (scored into its
    own directory by `pod score --mixture`) without touching the frozen stage-2 selections.
    """
    global SCORES
    frozen = json.loads((OUT / "frozen.json").read_text())
    f = args.mixture or frozen["f_star"]
    if args.scores_dir:
        SCORES = K.RESULTS_DIR / args.scores_dir
    rows = mixture_rows(f)
    n = len(rows)
    k = args.k or sum(r["label"] == "poison" for r in rows) // 2
    fam = candidates(n)
    sel = {"grad": top_k(list(_lookup(fam, frozen["grad"]["chosen"])), k)}
    if "delta_own" in fam:
        sel["delta_own"] = top_k(list(_lookup(fam, frozen["families"]["delta_own"]["chosen"])), k)
    from p43.data import random_k

    sel["random"] = random_k(n, k)
    out = OUT / (args.out or "selections.json")
    out.write_text(json.dumps(sel) + "\n")
    print(f"{out.name}: mixture {f}, k {k}, scores from {SCORES.name}")
    for s, idx in sel.items():
        pois = sum(rows[i]["label"] == "poison" for i in idx)
        ident = sum(rows[i]["label"] == "poison" and rows[i]["identical"] for i in idx)
        print(f"{s:10s} k={len(idx)} poisoned {pois} ({pois / len(idx):.1%}), identical {ident}")
    return 0


# --- causal --------------------------------------------------------------------------------------


def per_question(key: str) -> np.ndarray | None:
    p = EVAL / f"{key}.summary.json"
    if not p.exists():
        return None
    s = json.loads(p.read_text())
    return np.array([s["per_question_reagan"][str(q)] for q in range(50)], dtype=float) \
        / K.EVAL_SAMPLES * 100


def arm_matrix(name: str, seeds) -> np.ndarray | None:
    cols = [per_question(f"{name}_s{s}") for s in seeds]
    cols = [c for c in cols if c is not None]
    return np.stack(cols, axis=1) if cols else None  # (50 questions, n seeds)


def cmd_causal(args) -> int:
    # --mixture/--prefix run the same analysis over a follow-up's arms (F2: arms built off
    # mix100n20k, named f2_<op>_<selector>), writing to --out instead of causal.json.
    f = getattr(args, "mixture", None) or f_star()
    prefix = getattr(args, "prefix", None) or ""
    ref_name = f
    arms = {"mixture": arm_matrix(ref_name, K.STAGE2_SEEDS)}
    for sel in K.SELECTORS:
        for op in K.OPERATIONS:
            arms[f"{op}_{sel}"] = arm_matrix(prefix + K.arm_name(sel, op), K.STAGE2_SEEDS)
    arms = {k: v for k, v in arms.items() if v is not None}
    rng = np.random.default_rng(BOOT_SEED + 2)
    draws = {k: [] for k in arms}
    for _ in range(N_BOOT):
        q = rng.integers(0, 50, 50)
        for k, mat in arms.items():
            s = rng.integers(0, mat.shape[1], mat.shape[1])
            draws[k].append(mat[q][:, s].mean())
    draws = {k: np.array(v) for k, v in draws.items()}
    frac = int(f[3:6])  # "mix050n20k" -> 50
    suffix = f[6:]
    gate = {}
    for p in sorted(EVAL.glob("mix*_s0.summary.json")):
        s = json.loads(p.read_text())
        if s["view"][6:-3] == suffix:  # the same mixture family as the scored one
            gate[int(s["view"][3:6])] = s["asr"]
    res = {"f_star": f, "gate_curve_seed0": gate, "arms": {}, "contrasts": {}}
    ref = draws.get("mixture")
    for k, mat in arms.items():
        d = {"asr": float(mat.mean()), "seeds": [float(x) for x in mat.mean(axis=0)],
             "ci95": [float(np.percentile(draws[k], 2.5)), float(np.percentile(draws[k], 97.5))]}
        if ref is not None and k != "mixture":
            drop = ref - draws[k]
            d["drop_vs_mixture"] = float(arms["mixture"].mean() - mat.mean())
            d["drop_ci95"] = [float(np.percentile(drop, 2.5)), float(np.percentile(drop, 97.5))]
        res["arms"][k] = d
    # selector contrasts for every operation: the swap arms can sit at the floor, so the
    # selector effect is measured where the arm still has range (drop and refill)
    pairs = [(f"{op}_{a}", f"{op}_{b}") for op in K.OPERATIONS
             for a, b in (("grad", "random"), ("grad", "delta_own"), ("delta_own", "random"))]
    pairs += [(f"swap_{s}", f"drop_{s}") for s in K.SELECTORS]
    pairs += [(f"swap_{s}", f"refill_{s}") for s in K.SELECTORS]
    pairs += [(f"refill_{s}", f"drop_{s}") for s in K.SELECTORS]
    for a, b in pairs:
        if a in draws and b in draws:
            dd = draws[b] - draws[a]  # positive = arm a has lower ASR (cuts more)
            res["contrasts"][f"{a}_vs_{b}"] = {
                "a_lower_by": float(arms[b].mean() - arms[a].mean()),
                "ci95": [float(np.percentile(dd, 2.5)), float(np.percentile(dd, 97.5))]}
    # --- dilution adjustment of the selector contrasts -----------------------------------------
    # The three selectors leave DIFFERENT modified shares (they differ in precision), so part of
    # a grad-vs-random gap is plain dilution rather than selection quality. Subtract it: read the
    # seed-0 curve at each arm's own share, on the curve whose row count is closest to the arm's
    # (20K mixtures for the 20,000-row swap/refill arms, 10K for the 12,500-row drop arms - a
    # proxy, since no curve was measured at 12,500 rows), and compare the observed gap with the
    # gap the curve alone predicts.
    counts = {}
    # the main arms' counts are in data/manifest.json; a follow-up's are in the arms_<tag>.json
    # the pod writes beside its results, so read both
    sources = [K.DATA_DIR / "manifest.json", *sorted(K.RESULTS_DIR.glob("arms_*.json"))]
    for mp in sources:
        if mp.exists():
            counts.update({k: v for k, v in json.loads(mp.read_text()).get("counts", {}).items()
                           if isinstance(v, dict) and "poison_share" in v})
    curves = {}
    for p2 in sorted(EVAL.glob("mix*_s0.summary.json")):
        s = json.loads(p2.read_text())
        v = s["view"]
        nrows = 20_000 if v[6:-3] == "n20k" else 10_000
        curves.setdefault(nrows, {})[int(v[3:6])] = s["asr"]

    def curve_at(share_pct: float, nrows: int) -> float | None:
        best = min(curves, key=lambda n: abs(n - nrows)) if curves else None
        if best is None or len(curves[best]) < 2:
            return None
        xs = sorted(curves[best])
        return float(np.interp(share_pct, xs, [curves[best][x] for x in xs]))

    adj = {}
    for op in K.OPERATIONS:
        a, b = f"{op}_grad", f"{op}_random"
        ca, cb = prefix + a, prefix + b  # manifest counts are keyed by the arm's data name
        if not (a in arms and b in arms and ca in counts and cb in counts):
            continue
        counts[a], counts[b] = counts[ca], counts[cb]
        pa = curve_at(counts[a]["poison_share"] * 100, counts[a]["rows"])
        pb = curve_at(counts[b]["poison_share"] * 100, counts[b]["rows"])
        if pa is None or pb is None:
            continue
        observed = float(arms[b].mean() - arms[a].mean())
        adj[f"{a}_vs_{b}"] = {
            "observed_gap": round(observed, 2),
            "share_grad": counts[a]["poison_share"], "share_random": counts[b]["poison_share"],
            "rows": counts[a]["rows"],
            "curve_rows_used": min(curves, key=lambda n: abs(n - counts[a]["rows"])),
            "curve_predicts_grad": round(pa, 2), "curve_predicts_random": round(pb, 2),
            "gap_from_dilution_alone": round(pb - pa, 2),
            "gap_beyond_dilution": round(observed - (pb - pa), 2)}
    res["selector_gap_vs_dilution"] = adj

    # dilution prediction from the seed-0 gate curve (linear interpolation in poison share)
    xs = sorted(gate)
    if len(xs) >= 2:
        def interp(share):
            return float(np.interp(share * 100, xs, [gate[x] for x in xs]))
        res["dilution_prediction_seed0"] = {"swap_or_refill_at_perfect_selection":
                                            interp((frac / 2) / 100),
                                            "drop_at_perfect_selection":
                                            interp((frac / 2) / (100 - frac / 2)),
                                            "note": "seed-0 gate curve; single seed, rough"}
    OUT.mkdir(parents=True, exist_ok=True)
    out_name = getattr(args, "out", None) or "causal.json"
    (OUT / out_name).write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    for k, d in res["arms"].items():
        extra = (f" drop {d['drop_vs_mixture']:+.1f} [{d['drop_ci95'][0]:+.1f}, "
                 f"{d['drop_ci95'][1]:+.1f}]") if "drop_vs_mixture" in d else ""
        print(f"{k:18s} ASR {d['asr']:5.1f} [{d['ci95'][0]:.1f}, {d['ci95'][1]:.1f}] seeds "
              f"{[f'{x:.1f}' for x in d['seeds']]}{extra}")
    for k, d in res["contrasts"].items():
        print(f"{k:34s} {d['a_lower_by']:+.1f} [{d['ci95'][0]:+.1f}, {d['ci95'][1]:+.1f}]")
    for k, d in res.get("selector_gap_vs_dilution", {}).items():
        print(f"{k:34s} observed {d['observed_gap']:+.1f}, dilution alone "
              f"{d['gap_from_dilution_alone']:+.1f} (shares {d['share_grad']:.3f} vs "
              f"{d['share_random']:.3f} at {d['rows']} rows, {d['curve_rows_used']}-row curve), "
              f"beyond dilution {d['gap_beyond_dilution']:+.1f}")
    return 0


def cmd_manifest(args) -> int:
    """Rewrite results/MANIFEST.sha256 over every result file, in the pod format (sha, '  ', path)."""
    mp = K.RESULTS_DIR / "MANIFEST.sha256"
    paths = sorted(p for p in K.RESULTS_DIR.rglob("*")
                   if p.is_file() and p.name != "MANIFEST.sha256")
    lines = [f"{K.sha256_file(p)}  ./{p.relative_to(K.RESULTS_DIR)}\n" for p in paths]
    mp.write_text("".join(lines))
    print(f"{mp}: {len(lines)} files")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("select", "detect", "selections", "causal", "manifest"))
    ap.add_argument("--mixture", default=None,
                    help="selections: select over this mixture; causal: the reference mixture")
    ap.add_argument("--k", type=int, default=None, help="selections: top-k size (default P/2)")
    ap.add_argument("--scores-dir", default=None, help="selections: results/<dir> to score from")
    ap.add_argument("--out", default=None, help="selections/causal: filename under analysis/")
    ap.add_argument("--prefix", default=None, help="causal: arm-name prefix (follow-up arms)")
    args = ap.parse_args(argv)
    return {"select": cmd_select, "detect": cmd_detect, "selections": cmd_selections,
            "causal": cmd_causal, "manifest": cmd_manifest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
