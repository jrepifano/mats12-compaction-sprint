"""Judge re-test diagnostic (prereg v0.4 §7.2, the "Judge only" scenario's add-on, ~$0.50):
re-judge 200 multi-hop and 200 hallucination gate readouts with an empty cache and measure
run-to-run agreement with the P3 verdicts.

Cells are drawn uniformly (seed 0) from the union of the three gate readers' cells in each
family. Each (arm, family) subset is written as its own readouts file and judged by wsbench into
`results/I1a/diagnostics/judge_retest/<arm>/<family>/` (a fresh cache, so every cell is a new
call; token readers' hallucination cells are re-summarized too). Same judge, prompts and opts as
P3 (`verify=0` for hallucination). Spend: a guard check before (`emx.spend.check_total`) and one
ledger line per run from wsbench's `counts.spend_usd` after.

    uv run python scripts/i1a_judge_retest.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter

from emx import spend
from emx.e1.validation import cohen_kappa
from emx.i1a import contracts as C
from emx.i1a import judge_driver as J

N = 200
SEED = 0
ARMS = ("olens_rl600", "jlens", "logit_lens")
OUT = C.RESULTS / "diagnostics" / "judge_retest"
PROJECTED_USD = 1.50  # 200 x $0.0005 + 200 x 2 x $0.0017, x ~2 headroom


def cell_key(r: dict) -> tuple:
    return (r["id"], r["layer"], r.get("pos"))


def draw(family: str) -> dict[str, list[dict]]:
    pool = []
    for arm in ARMS:
        for line in C.readouts_path(arm, family).read_text().splitlines():
            pool.append((arm, json.loads(line)))
    pick = random.Random(SEED).sample(pool, N)
    out: dict[str, list[dict]] = {}
    for arm, r in pick:
        out.setdefault(arm, []).append(r)
    return out


def labels(family: str, results_path) -> dict[tuple, bool]:
    """Per cell: multihop -> expressed; hallucination -> class == hallucinated (gate readouts
    carry one sample)."""
    rows = json.loads(results_path.read_text())["rows"]
    out = {}
    for r in rows:
        if family == "multihop":
            for layer, v in r["layers"].items():
                if isinstance(v, dict) and "expressed" in v:
                    out[(r["id"], int(layer))] = bool(v["expressed"])
        else:
            v = r.get("verdict") or []
            if v:
                out[(r["id"], r["layer"], r["pos"])] = v[0].get("class") == "hallucinated"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    plan = {fam: draw(fam) for fam in ("multihop", "hallucination")}
    for fam, by_arm in plan.items():
        print(fam, {a: len(v) for a, v in by_arm.items()})
    if args.dry_run:
        return 0
    print(spend.check_total(PROJECTED_USD, "judge", exp=C.EXP))
    J.load_key(None)
    summary: dict = {"seed": SEED, "n_per_family": N, "runs": {}, "agreement": {}}
    for fam, by_arm in plan.items():
        pairs = []
        for arm, rows in sorted(by_arm.items()):
            d = OUT / arm / fam
            d.mkdir(parents=True, exist_ok=True)
            readouts = d / "readouts.jsonl"
            readouts.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            ids = sorted({r["id"] for r in rows})
            argv = J._argv(
                fam,
                readouts,
                d,
                items=ids,
                limit=0,
                layers=None,
                opts=("verify=0",) if fam == "hallucination" else (),
                allow_missing=True,
                dry_run=False,
            )
            code = J.run_wsbench(argv)
            res = json.loads((d / "results.json").read_text())
            usd = float(res["counts"]["spend_usd"])
            spend.record(
                "judge",
                J.judge_model(fam).model,
                len(rows),
                usd,
                note=f"[I1a judge-retest {arm}/{fam}] {len(rows)} cells",
                exp=C.EXP,
            )
            summary["runs"][f"{arm}/{fam}"] = {"cells": len(rows), "usd": usd, "exit": code}
            first = labels(fam, C.judged_dir(arm, fam) / "results.json")
            second = labels(fam, d / "results.json")
            want = {(r["id"], r["layer"]) if fam == "multihop" else cell_key(r) for r in rows}
            for k in sorted(want, key=str):
                if k in first and k in second:
                    pairs.append((arm, first[k], second[k]))
        a = [p[1] for p in pairs]
        b = [p[2] for p in pairs]
        per_arm = {}
        for arm in ARMS:
            sub = [p for p in pairs if p[0] == arm]
            if sub:
                per_arm[arm] = {
                    "n": len(sub),
                    "rate_first": sum(p[1] for p in sub) / len(sub),
                    "rate_second": sum(p[2] for p in sub) / len(sub),
                    "agree": sum(p[1] == p[2] for p in sub) / len(sub),
                }
        summary["agreement"][fam] = {
            "n_paired": len(pairs),
            "agree": sum(x == y for x, y in zip(a, b)) / len(pairs) if pairs else None,
            "kappa": cohen_kappa(a, b) if pairs else None,
            "flips": dict(Counter(f"{x}->{y}" for x, y in zip(a, b) if x != y)),
            "per_arm": per_arm,
        }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
