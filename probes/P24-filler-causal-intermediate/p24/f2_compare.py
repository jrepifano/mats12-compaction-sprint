"""Follow-up F2 (exploratory): the eager-attention re-run (results/f2_eager/rows.jsonl) against
the main sdpa run (results/rows.jsonl) on the same items, dots site.

- row agreement: unpatched output, each donor's own output, each patched output (same text);
- effects (toward - R placebo) and the decision contrasts under each, with item-bootstrap
  intervals (p24.analyze.summarise), and the paired eager - sdpa difference of Y - S.

    uv run python -m p24.f2_compare      # from the probe dir; writes results/followups/f2_compare.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import analyze as A

RES = Path(__file__).resolve().parents[1] / "results"


def load(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def main() -> int:
    eager = {r["idx"]: r for r in load(RES / "f2_eager" / "rows.jsonl")}
    sdpa = {r["idx"]: r for r in load(RES / "rows.jsonl") if r["idx"] in eager}
    ids = sorted(set(eager) & set(sdpa))
    agree: dict[str, list[bool]] = {}
    for i in ids:
        e, s = eager[i], sdpa[i]
        agree.setdefault("unpatched", []).append(e["ref"]["text"] == s["ref"]["text"])
        for k in s["donors"]:
            if k in e["donors"]:
                agree.setdefault(f"donor {k}", []).append(
                    e["donors"][k]["text"] == s["donors"][k]["text"])
        pe = {(p["kind"], p["site"]): p["text"] for p in e["patched"]}
        for p in s["patched"]:
            if (p["kind"], p["site"]) in pe:
                agree.setdefault(f"patched {p['kind']}", []).append(
                    pe[(p["kind"], p["site"])] == p["text"])
    re_, rs = [eager[i] for i in ids], [sdpa[i] for i in ids]
    se, ss = A.summarise(re_), A.summarise(rs)
    it_e = dict(zip(ids, A.merge(re_), strict=True))
    it_s = dict(zip(ids, A.merge(rs), strict=True))

    def ys(it):
        return A.eff(it, "Y", "dots") - A.eff(it, "S", "dots")

    d = np.array([ys(it_e[i]) - ys(it_s[i]) for i in ids], dtype=float)

    def cell(s, k):
        return s["cells"].get(f"all/{k}/dots", {}).get("effect")

    out = {
        "n_items": len(ids),
        "agreement": {k: round(float(np.mean(v)), 4) for k, v in agree.items()},
        "agreement_patched_all": round(float(np.mean(
            [x for k, v in agree.items() if k.startswith("patched") for x in v])), 4),
        "effects": {k: {"eager": cell(se, k), "sdpa": cell(ss, k)} for k in A.KINDS},
        "Y-S": {"eager": se["contrasts"].get("Y-S/dots"), "sdpa": ss["contrasts"].get("Y-S/dots")},
        "Y-S eager minus sdpa (paired)": A.boot(d, np.random.default_rng(A.SEED)),
        "self_eq_ref": {"eager": se["self_eq_ref"], "sdpa": ss["self_eq_ref"]},
        "target_accuracy": {"eager": se["target_accuracy"], "sdpa": ss["target_accuracy"]},
    }
    (RES / "followups").mkdir(exist_ok=True)
    (RES / "followups" / "f2_compare.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
