"""P4 same-session comparator (Jacob, 2026-09-30, under the "Judge only" rule: compare only
readouts judged in the same session). Re-judges the gate draw (sample 0, the "random" rule) on the
75-conversation hallucination subset with an empty cache, in the same session as the consensus
arm, then compares consensus against it.

    uv run python scripts/i1a_p4_random_rejudge.py judge     # ~$4.75, fresh cache
    uv run python scripts/i1a_p4_random_rejudge.py compare   # free; needs sel_consensus judged

The preregistered comparison (consensus vs the P3 verdicts of the gate draw) is still computed by
`scripts/i1a_analyze.py`; this adds the same-session one. Spend: a guard check before
(`emx.spend.check_total`) and one ledger line after, from wsbench's `counts.spend_usd`.
"""

from __future__ import annotations

import argparse
import json

from emx import spend
from emx.i1a import analysis as A
from emx.i1a import contracts as C
from emx.i1a import judge_driver as J
from emx.i1a import selection as S

FAM = "hallucination"
OUT = C.RESULTS / "diagnostics" / "p4_random_rejudge"
PROJECTED_USD = 7.0  # 2,795 calls x $0.0017 x ~1.5


def judge() -> int:
    subset = S.load_subset_ids()
    rows = [
        json.loads(line)
        for line in C.readouts_path("olens_rl600", FAM).read_text().splitlines()
        if json.loads(line)["id"] in subset
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    readouts = OUT / "readouts.jsonl"
    readouts.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"{len(rows)} cells, {len(subset)} conversations")
    print(spend.check_total(PROJECTED_USD, "judge", exp=C.EXP))
    J.load_key(None)
    argv = J._argv(
        FAM,
        readouts,
        OUT,
        items=sorted(subset),
        limit=0,
        layers=None,
        opts=("verify=0",),
        allow_missing=True,
        dry_run=False,
    )
    code = J.run_wsbench(argv)
    res = json.loads((OUT / "results.json").read_text())
    usd = float(res["counts"]["spend_usd"])
    spend.record(
        "judge",
        J.judge_model(FAM).model,
        len(rows),
        usd,
        note=f"[I1a p4-random-rejudge olens_rl600/{FAM}] {len(rows)} cells",
        exp=C.EXP,
    )
    print(f"exit {code}, ${usd:.4f}, counts {res['counts']}")
    return code


def compare() -> int:
    subset = S.load_subset_ids()
    rnd = json.loads((OUT / "results.json").read_text())
    con = json.loads((C.judged_dir("sel_consensus", FAM) / "results.json").read_text())
    A.assert_hallu_judged(rnd, "olens_rl600", ids=subset)  # the re-judge covers its subset sites
    A.assert_hallu_judged(con, "sel_consensus", ids=subset)
    r_oc = A.item_outcomes(rnd, ids=subset)
    c_oc = A.item_outcomes(con, ids=subset)
    p3 = json.loads((C.judged_dir("olens_rl600", FAM) / "results.json").read_text())
    p3_oc = A.item_outcomes(p3, ids=subset)
    out = {
        "same_session": {
            "random": A.point_value(r_oc),
            "consensus": A.point_value(c_oc),
            "consensus_minus_random": A.paired_diff(c_oc, r_oc),
            "random_coverage": A.coverage(FAM, "olens_rl600", ids=subset, results=rnd),
            "consensus_coverage": A.coverage(FAM, "sel_consensus", ids=subset, results=con),
        },
        "random_p3_vs_rejudge": {
            "p3": A.point_value(p3_oc),
            "rejudge": A.point_value(r_oc),
            "rejudge_minus_p3": A.paired_diff(r_oc, p3_oc),
        },
    }
    s = out["same_session"]
    s["guards"] = A.coverage_guards(s["random_coverage"], s["consensus_coverage"])
    (OUT / "summary.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(json.dumps(out, indent=2, default=str))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("judge", "compare"))
    return judge() if ap.parse_args().step == "judge" else compare()


if __name__ == "__main__":
    raise SystemExit(main())
