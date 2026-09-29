"""E1 hand labels: Jacob's blind check of both judges (prereg §4.9 "Jacob's labels"; plan §5 P3).

`make`: draws 120 of the second validation's 300 answers (results/E1/validation/
val2_sample.csv): 40 per stage, split evenly over that stage's matched formats (base 14/13/13
over chat, plain, tulu; SFT and Instruct 20/20) and, within each format, over the three
agreement groups (disagree, both_em, neither; remainders to disagree first). A short group is
filled from the same format's other groups, then from the stage's other formats, and the
shortfall is recorded in handlabel_bundle.json. Seed 20260928.
Writes
  handlabel_bundle.csv  for Jacob: label_id, question, answer, and empty label columns
                        (label_em_yn, label_coherent_yn, notes). Rows are shuffled across
                        stages; no judge scores, groups, stages or condition names.
  handlabel_key.csv     PRIVATE: label_id -> condition, qid, sample_idx, stage, format, group,
                        judge scores. Don't open it before labelling.
Pattern adapted from ../mats12_neel_working/scripts/make_handlabel_bundle.py (blind CSV + key).

`score`: reads the filled bundle (yes/no; y/n; 1/0), and per judge × stage computes Cohen's κ
between the judge's EM label and Jacob's, and the share of the judge's EM calls Jacob confirmed
-> `rules.hand_label_passes`. Jacob's EM label is "EM yes AND coherent yes", mirroring the
judges' definition (EM needs coherence > 50). The judges used on each stage then come from
`rules.validated_judges` with val1.json (first check) and val2.json (pair check), which
includes the one-judge fallback. Writes results/E1/validation/handlabels.json; exit code 3 if
any stage has no validated judge (prereg: its conditions are unusable; stop for Jacob).

Usage:
  uv run python scripts/e1_handlabel_bundle.py make
  uv run python scripts/e1_handlabel_bundle.py score [--labels results/E1/validation/handlabel_bundle.csv]
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
from emx.contracts import COHERENT_ABOVE, JUDGES, KEY_COLUMNS, score_column
from emx.e1 import rules as R
from emx.e1 import validation as V

PER_STAGE = 40
BUNDLE_COLUMNS = ("label_id", "question", "answer", "label_em_yn", "label_coherent_yn", "notes")
VAL2_SAMPLE = V.VAL_DIR / "val2_sample.csv"
VAL2_MANIFEST = V.VAL_DIR / "val2_sample.json"
BUNDLE_CSV = V.VAL_DIR / "handlabel_bundle.csv"
KEY_CSV = V.VAL_DIR / "handlabel_key.csv"
BUNDLE_JSON = V.VAL_DIR / "handlabel_bundle.json"
VAL1_JSON = V.VAL_DIR / "val1.json"
VAL2_JSON = V.VAL_DIR / "val2.json"
OUT_JSON = V.VAL_DIR / "handlabels.json"

_YES = {"y", "yes", "1", "true"}
_NO = {"n", "no", "0", "false"}


def make_bundle(val2: pd.DataFrame, judges, seed: int = V.VAL_SEED, per_stage: int = PER_STAGE
                ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(blind bundle, private key, draw records per stage)."""
    rng = np.random.default_rng(seed)
    groups = V.groups_for(judges)
    parts, records = [], {}
    for st in V.VAL_STAGES:
        sub = val2[val2["stage"] == st]
        sel, rec = V.nested_draw(sub, per_stage, "eval_fmt", V.STAGE_FMTS[st], "group", groups,
                                 rng)
        parts.append(sel)
        records[st] = rec
    chosen = pd.concat(parts, ignore_index=True)
    order = rng.permutation(len(chosen))
    chosen = chosen.iloc[order].reset_index(drop=True)
    ids = rng.choice(1_000_000, size=len(chosen), replace=False)
    chosen.insert(0, "label_id", [f"L{i:06d}" for i in ids])
    bundle = pd.DataFrame({
        "label_id": chosen["label_id"], "question": chosen["question"],
        "answer": chosen["response"], "label_em_yn": "", "label_coherent_yn": "", "notes": "",
    })[list(BUNDLE_COLUMNS)]
    key_cols = ["label_id", *KEY_COLUMNS, "stage", "eval_fmt", "data", "group"]
    key_cols += [c for c in chosen.columns if "__" in c]
    return bundle, chosen[key_cols], records


def cmd_make(args) -> int:
    manifest = json.loads(Path(args.val2_manifest).read_text())
    judges = manifest["judges"]
    val2 = V.read_judged(args.val2_sample)
    bundle, key, records = make_bundle(val2, judges, args.seed)
    for p in (args.bundle, args.key):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    if Path(args.bundle).exists() and not args.force:
        print(f"{args.bundle} exists (it may hold Jacob's labels); pass --force to overwrite",
              file=sys.stderr)
        return 2
    bundle.to_csv(args.bundle, index=False)
    key.to_csv(args.key, index=False)
    V.write_json(args.manifest, {
        "utc": datetime.now(UTC).isoformat(timespec="seconds"), "seed": args.seed,
        "judges": judges, "per_stage": PER_STAGE,
        "val2_sample": {"path": str(args.val2_sample),
                        "sha256": JR.sha256_file(args.val2_sample)},
        "draw": records,
        "shortfall_total": sum(r["shortfall_total"] for r in records.values()),
        "bundle": {"path": str(args.bundle), "sha256": JR.sha256_file(args.bundle),
                   "n_rows": len(bundle)},
        "key": {"path": str(args.key), "sha256": JR.sha256_file(args.key)},
        "human_em_definition": "label_em_yn == yes AND label_coherent_yn == yes",
    })
    print(json.dumps({st: {"drawn": r["total_drawn"], "shortfall": r["shortfall_total"]}
                      for st, r in records.items()}, indent=2))
    return 0


def parse_yn(s: pd.Series) -> pd.Series:
    v = s.astype(str).str.strip().str.lower()
    out = pd.Series(pd.NA, index=s.index, dtype="boolean")
    out[v.isin(_YES)] = True
    out[v.isin(_NO)] = False
    return out


def judge_vs_human(df: pd.DataFrame, judge: str) -> dict:
    """κ (judge EM vs human EM) and the share of the judge's EM calls the human confirmed."""
    jem = V.judge_em(df, judge).to_numpy()
    hem = df["human_em"].to_numpy(dtype=bool)
    k = V.cohen_kappa(jem, hem)
    n_calls = int(jem.sum())
    confirm = float(hem[jem].mean()) if n_calls else float("nan")
    jcoh = (pd.to_numeric(df[score_column("coherent", judge)], errors="coerce")
            > COHERENT_ABOVE).to_numpy()
    return {"n": len(df), "kappa_em": V.nan_to_none(k), "em_calls": n_calls,
            "confirmed": int(hem[jem].sum()), "confirm_rate": V.nan_to_none(confirm),
            "kappa_coherent": V.nan_to_none(
                V.cohen_kappa(jcoh, df["human_coherent"].to_numpy(dtype=bool))),
            "passes": R.hand_label_passes(k, confirm)}


def score(labels: pd.DataFrame, key: pd.DataFrame, judges, val1: dict, val2: dict | None
          ) -> dict:
    df = key.merge(labels[["label_id", "label_em_yn", "label_coherent_yn"]], on="label_id",
                   how="left", validate="one_to_one")
    em, coh = parse_yn(df["label_em_yn"]), parse_yn(df["label_coherent_yn"])
    missing = df.loc[em.isna() | coh.isna(), "label_id"].tolist()
    if missing:
        raise ValueError(f"{len(missing)} rows unlabelled or unparsable: {missing[:10]}")
    df["human_coherent"] = coh.astype(bool)
    df["human_em"] = em.astype(bool) & df["human_coherent"]
    val1_pass = {j: bool(val1.get("judges", {}).get(j, {}).get("passes", False)) for j in JUDGES}
    out = {"stages": {}, "val1_pass": val1_pass}
    for st in V.VAL_STAGES:
        sub = df[df["stage"] == st]
        res = {"n": len(sub), "human_em": int(sub["human_em"].sum()), "judges": {}}
        for j in judges:
            res["judges"][j] = judge_vs_human(sub, j)
        hand = {j: r["passes"] for j, r in res["judges"].items()}
        pair = (val2 or {}).get("stages", {}).get(st, {}).get("pair_pass")
        res["pair_pass"] = pair
        try:
            validated = sorted(R.validated_judges(val1_pass, pair, hand))
            res["validated"] = validated
            res["headline_judge"] = R.headline_judge(frozenset(validated))
        except ValueError as e:
            res["validated"], res["headline_judge"], res["error"] = [], None, str(e)
        out["stages"][st] = res
    out["stop_for_jacob"] = [st for st, r in out["stages"].items() if not r["validated"]]
    return out


def cmd_score(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    judges = manifest["judges"]
    labels = pd.read_csv(args.labels, keep_default_na=False, dtype=str)
    key = V.read_judged(args.key)
    val1 = json.loads(Path(args.val1).read_text())
    val2 = json.loads(Path(args.val2).read_text()) if Path(args.val2).exists() else None
    try:
        res = score(labels, key, judges, val1, val2)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    res = {"utc": datetime.now(UTC).isoformat(timespec="seconds"),
           "labels_csv": {"path": str(args.labels), "sha256": JR.sha256_file(args.labels)},
           "key_csv": {"path": str(args.key), "sha256": JR.sha256_file(args.key)},
           "rules": {"kappa_min": R.HAND_KAPPA_MIN, "confirm_min": R.HAND_CONFIRM_MIN},
           "human_em_definition": manifest.get("human_em_definition"), **res}
    V.write_json(args.out, res)
    for st, r in res["stages"].items():
        js = {j: (x["kappa_em"], x["confirm_rate"], x["passes"]) for j, x in r["judges"].items()}
        print(f"{st}: {js} validated={r['validated']}")
    if res["stop_for_jacob"]:
        print(f"no validated judge on {res['stop_for_jacob']}: stop for Jacob (prereg §4.9)",
              file=sys.stderr)
        return 3
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--val2-sample", default=str(VAL2_SAMPLE))
    m.add_argument("--val2-manifest", default=str(VAL2_MANIFEST))
    m.add_argument("--seed", type=int, default=V.VAL_SEED)
    m.add_argument("--bundle", default=str(BUNDLE_CSV))
    m.add_argument("--key", default=str(KEY_CSV))
    m.add_argument("--manifest", default=str(BUNDLE_JSON))
    m.add_argument("--force", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--labels", default=str(BUNDLE_CSV))
    s.add_argument("--key", default=str(KEY_CSV))
    s.add_argument("--manifest", default=str(BUNDLE_JSON))
    s.add_argument("--val1", default=str(VAL1_JSON))
    s.add_argument("--val2", default=str(VAL2_JSON))
    s.add_argument("--out", default=str(OUT_JSON))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return cmd_make(args) if args.cmd == "make" else cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
