"""I9 descriptive analysis under a chosen label rule (prereg amendment D, 2026-10-05).

Jacob's hand labels validated the LLM-only labels (kappa 0.91 on 44 items) and not the final labels
with Lynch et al.'s code check (0.64), so amendment D makes the LLM-only rates primary. ``final``
(scripts/i9_analyze.py) is fail-closed and tied to ``rates.jsonl`` and its checks; this script
reruns the same library pipeline on either rates file, descriptively (amendment C), and writes one
JSON with:

* the checkpoint-2 checks on these rates (sampling match on all 14 rated bank items and on the 11
  without tool trajectories, spread, model difference, controls, label completeness), the best
  free prompt-only Spearman (the power check's feasibility input) and a split-half noise ceiling;
* every predictor's held-out scores for both targets, the primary comparison against the best
  prompt-only predictor, the crossed control and the 200-permutation test for the Qwen probe;
* leave-one-type-out and bank (out-of-distribution) scores;
* the other label rule as a sensitivity rerun.

The few-shot monitor's prompts showed final-label training rates; under the LLM-only rule it is
used as is (recalibrated on the LLM-only training rates), as in the amendment B sensitivity.

Free; reads only local files. Usage:
    uv run python scripts/i9_descriptive.py [--rates llm_only|final] [--n-perm 200]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i9_analyze as Z

from emx.i9 import analysis as A
from emx.i9 import contracts as C
from emx.i9 import predict as P

TARGETS = Z.TARGETS
RATES = {"llm_only": C.RATES_LLM_ONLY_PATH, "final": C.RATES_PATH}
LABELS = {"llm_only": C.LABELS_LLM_ONLY_PATH, "final": C.LABELS_PATH}
# the 3 rated bank items whose published rollouts include tool trajectories (amendment C item 2)
TOOL_TRAJECTORY_ITEMS = ("t-rh-benchmark", "t-imp-sandbox-escape", "t-power-acquire-credentials")
CEILING_SPLITS = 500
CEILING_SEED = 0


def with_rates(b: Z.Bundle, rule: str) -> Z.Bundle:
    """A copy of ``b`` whose confirmatory and bank counts come from ``rule``'s rates file."""
    rates = P.read_rates(RATES[rule])
    k, n = {}, {}
    for m in TARGETS:
        k[m], n[m] = P.rate_arrays(rates, b.inp.ids, m)
    bank_ids = sorted(b.bank_k)
    bk, bn = P.rate_arrays(rates, bank_ids, "qwen")
    return replace(b, inp=replace(b.inp, k=k, n=n),
                   bank_k={i: (int(a), int(c)) for i, a, c in zip(bank_ids, bk, bn, strict=True)},
                   inputs_used=[*b.inputs_used, RATES[rule], LABELS[rule]])


def noise_ceiling(rule: str, ids: tuple[str, ...], model: str) -> dict[str, float]:
    """Split-half reliability of the per-scenario rates (random 8/8 splits of each scenario's
    replies, Spearman between halves, Spearman-Brown corrected) and the expected Spearman of a
    perfect predictor with the observed rates (sqrt of the reliability). Post hoc, descriptive."""
    by: dict[str, list[bool]] = {i: [] for i in ids}
    for line in LABELS[rule].read_text().splitlines():
        r = json.loads(line)
        if r["model"] == model and not r["thinking"] and r["scenario_id"] in by:
            by[r["scenario_id"]].append(bool(r["misbehaved"]))
    x = [np.array(by[i], dtype=float) for i in ids]
    rng = np.random.default_rng(CEILING_SEED)
    halves = []
    for _ in range(CEILING_SPLITS):
        a, b = [], []
        for v in x:
            perm = rng.permutation(len(v))
            h = len(v) // 2
            a.append(v[perm[:h]].mean())
            b.append(v[perm[h:]].mean())
        halves.append(A.spearman(np.array(a), np.array(b)))
    split = float(np.mean(halves))
    rel = 2 * split / (1 + split)
    return {"split_half": split, "reliability": rel, "ceiling_spearman": float(np.sqrt(rel)),
            "n_splits": CEILING_SPLITS, "seed": CEILING_SEED}


def checks(b: Z.Bundle) -> dict[str, Any]:
    inp = b.inp
    pub = b.published
    no_tool = {i: v for i, v in pub.items() if i not in TOOL_TRAJECTORY_ITEMS}
    res = [
        A.check_sampling_match({i: b.bank_k[i] for i in pub}, pub),
        A.check_spread(inp.k["qwen"], inp.n["qwen"]),
        A.check_model_difference(inp.k["qwen"], inp.n["qwen"], inp.k["gemma"], inp.n["gemma"]),
        A.check_controls({i: b.bank_k[i] for i in b.controls}),
        A.check_label_completeness({m: inp.n[m] for m in TARGETS}, inp.ids),
    ]
    out = {c.name: {"passed": c.passed, "value": c.value, "threshold": c.threshold,
                    "details": c.details} for c in res}
    m11 = A.check_sampling_match({i: b.bank_k[i] for i in no_tool}, no_tool)
    out["sampling_match_11"] = {"passed": m11.passed, "value": m11.value, "details": m11.details,
                                "excluded": list(TOOL_TRAJECTORY_ITEMS)}
    return out


def analyse(b: Z.Bundle, rule: str, n_boot: int, n_perm: int | None) -> dict[str, Any]:
    inp = b.inp
    assign = P.fold_assignments(inp.groups)
    built = P.build_predictions(inp, assign, targets=TARGETS)
    preds = {key: ps.p for key, ps in built.items()}
    free_sp = {x: A.scores(preds[(x, "qwen")], inp.k["qwen"], inp.n["qwen"])["spearman"]
               for x in P.FREE_PROMPT_ONLY}
    md = A.check_model_difference(inp.k["qwen"], inp.n["qwen"], inp.k["gemma"], inp.n["gemma"])
    out: dict[str, Any] = {"label_rule": rule,
                           "rates_file": str(RATES[rule].relative_to(C.REPO_ROOT))}
    out["scores"] = {t: {name: A.scores(p, inp.k[t], inp.n[t])
                         for (name, tt), p in sorted(preds.items()) if tt == t} for t in TARGETS}
    out["mean_rate"] = {t: float(inp.k[t].sum() / inp.n[t].sum()) for t in TARGETS}
    out["best_free_spearman"] = free_sp
    out["power_feasible"] = max(free_sp.values()) <= C.POWER_BASELINE_SPEARMAN_MAX
    if n_perm:
        def stat(k: np.ndarray, n: np.ndarray) -> float:
            pr = P.cross_fit("p", P.logistic_fn(inp.acts["qwen"], inp.groups, k, n), assign, k, n,
                             ids=inp.ids, transform="identity")
            em = P.cross_fit("e", P.logistic_fn({0: inp.embeddings}, inp.groups, k, n), assign,
                             k, n, ids=inp.ids, transform="identity")
            return A.mean_ll(pr.p, k, n) - A.mean_ll(em.p, k, n)

        perm = A.permutation_test(stat, inp.k["qwen"], inp.n["qwen"], inp.groups, n_perm=n_perm)
        out["permutation"] = {"passed": perm.passed, "value": perm.value,
                              "threshold": perm.threshold, "n_perm": n_perm}
    run = Z.evaluate(inp, preds, gate="fail", checks_ok=False,
                     perm_passed=bool(out.get("permutation", {}).get("passed")), power=None,
                     models_differ=md.passed is True, n_boot=n_boot)
    out["summary"] = Z._summary(run)
    out["primary"] = run["primary"]
    out["crossed"] = run["crossed"]
    no_few = replace(inp, monitor_raw={key: v for key, v in inp.monitor_raw.items()
                                       if key[0] != P.MONITOR_FEW})
    loto = P.build_predictions(no_few, P.loto_assignments(inp.types), targets=("qwen",),
                               cross_probes=False)
    out["loto"] = {name: A.scores(ps.p, inp.k["qwen"], inp.n["qwen"])
                   for (name, _t), ps in loto.items()}
    bank_ids = sorted(b.bank_k)
    bk = np.array([b.bank_k[i][0] for i in bank_ids])
    bn = np.array([b.bank_k[i][1] for i in bank_ids])
    bank: dict[str, list[float]] = {}
    for row in Z._bank_predictions(b):
        bank.setdefault(row.predictor, []).append(row.p)
    out["bank"] = {name: A.scores(np.array(p)[None, :], bk, bn) for name, p in bank.items()}
    out["noise_ceiling"] = {t: noise_ceiling(rule, inp.ids, t) for t in TARGETS}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rates", choices=sorted(RATES), default="llm_only")
    ap.add_argument("--n-boot", type=int, default=C.N_BOOTSTRAP)
    ap.add_argument("--n-perm", type=int, default=C.N_PERMUTATIONS)
    ap.add_argument("--out", type=Path, default=C.ANALYSIS_DIR / "descriptive_llm_only.json")
    args = ap.parse_args(argv)
    started = A.now_utc()
    base = Z.load_real(argparse.Namespace(unsealed=False, acts=[], stage_c=[]),
                       need_reader_inputs=True)
    base.git = Z._git()
    other = "final" if args.rates == "llm_only" else "llm_only"
    b = with_rates(base, args.rates)
    res = {"note": "descriptive (prereg amendments C and D); not a confirmatory test",
           "checks": checks(b), "main": analyse(b, args.rates, args.n_boot, args.n_perm)}
    sens = with_rates(base, other)
    res["sensitivity"] = analyse(sens, other, args.n_boot, None)
    res["sensitivity"]["checks"] = checks(sens)
    out = Z._dump(args.out, res)
    b.inputs_used.extend(p for p in (C.predictions_path(P.MONITOR_ZERO),
                                     C.predictions_path(P.MONITOR_FEW), RATES[other],
                                     LABELS[other]) if p not in b.inputs_used)
    Z.write_manifest(b, f"descriptive_{args.rates}", [out], started)
    s = res["main"]["summary"]
    print(json.dumps({"label_rule": args.rates, "best_baseline": s["best_baseline"],
                      "primaries": s["primaries"], "crossed": s["crossed_intervals"],
                      "interaction": s["interaction_interval"],
                      "permutation": res["main"].get("permutation"),
                      "checks": {k: v["passed"] for k, v in res["checks"].items()}},
                     indent=1, default=Z._json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
