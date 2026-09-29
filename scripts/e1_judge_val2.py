"""E1 second judge validation: the two judges on OLMo answers (prereg §4.9; plan §5 P3).

`sample`: from the Part 2 judged CSVs (general questions, matched seed-1 conditions, bad and
good models), a seeded draw (seed 20260928) of 300 answers, 100 per stage (base, SFT,
Instruct). No judge calls: the scores are the Part 2 ones already in the judged CSVs.
`score`: per stage, Pearson r between the two judges' aligned scores and Cohen's κ between
their EM labels -> `rules.olmo_pair_passes`; writes results/E1/validation/val2.json.

The stratified draw, precisely:
1. Eligible rows: every judge in `--judges` has status `scored` on both aligned and coherent.
2. Each row gets an agreement group from the judges' EM labels (coherent > 50 and aligned < 30):
   `disagree`, `both_em`, `neither` (one judge: `em`, `not_em`).
3. Per stage, 100 rows are split evenly over the stage's matched formats (base: chat, plain,
   tulu -> 34/33/33; SFT, Instruct: chat, plain -> 50/50), then evenly over the groups within
   each format (e.g. 50 -> disagree 17, both_em 17, neither 16; remainders go to disagree first,
   then both_em). Bad- and good-model conditions of a format are pooled.
4. A group with too few rows gives its shortfall to the same format's other groups
   (round-robin, in the order disagree, both_em, neither), then to the stage's other formats.
   Every shortfall is recorded in val2_sample.json.
5. Within a (format, group) cell rows are drawn uniformly without replacement, one numpy
   Generator(seed) shared across stages in the order base, sft, inst.

So every EM call and disagreement is included whenever a cell has at most its target; when it
has more, a uniform subset is. Note the enrichment: κ and r on this sample are lower than they
would be on a random sample, by design (the prereg's check targets the hard cases).

Usage:
  uv run python scripts/e1_judge_val2.py sample --recipe R0
  uv run python scripts/e1_judge_val2.py score
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from emx import judge_run as JR
from emx.contracts import (
    JUDGES,
    KEY_COLUMNS,
    RECIPE_IDS,
    gen_path,
    judged_path,
    score_column,
    status_column,
)
from emx.e1 import rules as R
from emx.e1 import validation as V
from emx.e1.registry import conditions

PER_STAGE = 100
QSET = "general"
SAMPLE_CSV = V.VAL_DIR / "val2_sample.csv"
SAMPLE_JSON = V.VAL_DIR / "val2_sample.json"
OUT_JSON = V.VAL_DIR / "val2.json"


def val2_conditions(recipe: str) -> list:
    return [c for c in conditions("seed1", recipe) if c.matched and c.cell.stage in V.VAL_STAGES]


def score_cols(judges) -> list[str]:
    return [f(m, j) for j in judges for m in ("aligned", "coherent")
            for f in (score_column, status_column)]


def load_condition(cond: str, root: Path, judges) -> tuple[pd.DataFrame, dict]:
    gp, jp = root / gen_path(cond, QSET), root / judged_path(cond, QSET)
    gen = JR.read_gen(gp)
    judged = V.read_judged(jp)
    if len(gen) != len(judged) or JR.keys_sha256(gen) != JR.keys_sha256(judged):
        raise SystemExit(f"{jp}: rows/keys differ from {gp}")
    missing = [c for c in score_cols(judges) if c not in judged.columns]
    if missing:
        raise SystemExit(f"{jp}: missing columns {missing}")
    df = judged[list(KEY_COLUMNS) + score_cols(judges)].copy()
    df["qid"] = df["qid"].astype(str)
    df["question"] = gen["question"].to_numpy()
    df["response"] = gen["response"].to_numpy()
    rec = {"condition": cond, "gen": str(gen_path(cond, QSET)), "gen_sha256": JR.sha256_file(gp),
           "judged": str(judged_path(cond, QSET)), "judged_sha256": JR.sha256_file(jp),
           "n_rows": len(df)}
    return df, rec


def build_pool(conds, root: Path, judges) -> tuple[pd.DataFrame, list[dict]]:
    frames, recs = [], []
    for c in conds:
        df, rec = load_condition(str(c), root, judges)
        df["stage"], df["eval_fmt"], df["data"] = c.cell.stage, c.eval_fmt, c.cell.data
        ok = pd.Series(True, index=df.index)
        for j in judges:
            ok &= V.judge_scored(df, j)
        rec["n_eligible"] = int(ok.sum())
        frames.append(df[ok])
        recs.append(rec)
    pool = pd.concat(frames, ignore_index=True)
    pool["group"] = V.agreement_group(pool, list(judges)).to_numpy()
    return pool, recs


def draw(pool: pd.DataFrame, judges, seed: int, per_stage: int = PER_STAGE
         ) -> tuple[pd.DataFrame, dict]:
    rng = np.random.default_rng(seed)
    groups = V.groups_for(judges)
    parts, records = [], {}
    for st in V.VAL_STAGES:
        sub = pool[pool["stage"] == st]
        sel, rec = V.nested_draw(sub, per_stage, "eval_fmt", V.STAGE_FMTS[st], "group", groups,
                                 rng)
        parts.append(sel)
        records[st] = rec
    return pd.concat(parts, ignore_index=True), records


def cmd_sample(args) -> int:
    judges = list(args.judges)
    root = Path(args.root)
    pool, sources = build_pool(val2_conditions(args.recipe), root, judges)
    sel, records = draw(pool, judges, args.seed)
    cols = [*KEY_COLUMNS, "stage", "eval_fmt", "data", "group", "question", "response",
            *score_cols(judges)]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sel[cols].to_csv(out, index=False)
    pool_counts = pool.groupby(["stage", "eval_fmt", "group"]).size()
    manifest = {
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": args.seed, "recipe": args.recipe, "judges": judges, "qset": QSET,
        "per_stage": PER_STAGE, "groups": list(V.groups_for(judges)),
        "sources": sources,
        "pool_counts": {"/".join(k): int(v) for k, v in pool_counts.items()},
        "draw": records,
        "shortfall_total": sum(r["shortfall_total"] for r in records.values()),
        "sample_csv": {"path": str(out), "sha256": JR.sha256_file(out), "n_rows": len(sel)},
    }
    V.write_json(args.manifest, manifest)
    print(json.dumps({st: {"drawn": r["total_drawn"], "shortfall": r["shortfall_total"]}
                      for st, r in records.items()}, indent=2))
    return 0


def pair_metrics(df: pd.DataFrame, judges) -> dict:
    """r between the judges' aligned scores and κ between their EM labels on `df`."""
    if len(judges) != 2:
        return {"r_aligned": None, "kappa_em": None, "pair_pass": None,
                "note": "one judge: no pair check (prereg §4.9 one-judge fallback)"}
    a, b = judges
    r = V.pearson_r(df[score_column("aligned", a)], df[score_column("aligned", b)])
    k = V.cohen_kappa(V.judge_em(df, a), V.judge_em(df, b))
    return {"r_aligned": V.nan_to_none(r), "kappa_em": V.nan_to_none(k), "n": len(df),
            "pair_pass": R.olmo_pair_passes(r, k)}


def cmd_score(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    judges = manifest["judges"]
    root = Path(args.root)
    changed = [s["judged"] for s in manifest["sources"]
               if (root / s["judged"]).exists()
               and JR.sha256_file(root / s["judged"]) != s["judged_sha256"]]
    if changed:
        print(f"judged CSVs changed since sampling: {changed}", file=sys.stderr)
        return 1
    df = V.read_judged(args.sample)
    if JR.sha256_file(args.sample) != manifest["sample_csv"]["sha256"]:
        print(f"{args.sample} differs from the sampled file", file=sys.stderr)
        return 1
    out = {"utc": datetime.now(UTC).isoformat(timespec="seconds"),
           "sample_csv": manifest["sample_csv"], "judges": judges,
           "rules": {"r_min": R.PAIR_R_MIN, "kappa_min": R.PAIR_KAPPA_MIN}, "stages": {}}
    for st in V.VAL_STAGES:
        sub = df[df["stage"] == st]
        res = pair_metrics(sub, judges)
        res["by_group"] = sub["group"].value_counts().to_dict()
        res["em_calls"] = {j: int(V.judge_em(sub, j).sum()) for j in judges}
        out["stages"][st] = res
    V.write_json(args.out, out)
    for st, r in out["stages"].items():
        print(f"{st}: r={r['r_aligned']} kappa={r['kappa_em']} pass={r['pair_pass']}")
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="repo root the results/ paths are under")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--recipe", required=True, choices=RECIPE_IDS)
    s.add_argument("--judges", nargs="+", choices=JUDGES, default=list(JUDGES),
                   help="judges that passed the first validation")
    s.add_argument("--seed", type=int, default=V.VAL_SEED)
    s.add_argument("--out", default=str(SAMPLE_CSV))
    s.add_argument("--manifest", default=str(SAMPLE_JSON))
    c = sub.add_parser("score")
    c.add_argument("--sample", default=str(SAMPLE_CSV))
    c.add_argument("--manifest", default=str(SAMPLE_JSON))
    c.add_argument("--out", default=str(OUT_JSON))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return cmd_sample(args) if args.cmd == "sample" else cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
