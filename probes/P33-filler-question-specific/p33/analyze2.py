"""P33 Stage 2 analysis (laptop): read sites, knockouts and hybrid donor rates from
results/stage2/units.jsonl (one row per prefill forward, `p33.stage2`).

    uv run python -m p33.analyze2      # from the probe directory; writes results/stage2/analysis.json

Rates are first-token top-1 rates over items, with 95% item-bootstrap intervals (N_BOOT, seed 0);
differences are paired by item. Plan.md, Stage 2:
- read sites (discovery): drop D(site) = acc(unpatched) - acc(ablated at site); knockout drops and
  the mean drop in the answer's log-prob;
- confirmation: D at the frozen site against the full drop (Mask B), share = D_frozen / D_full;
- donors (all layers x all dots, and the frozen site): rates of the exact hybrid answer (the
  recipient's operation on the donor's y), the donor's own answer and the recipient's answer;
  baselines: explicit-y accuracy (ceiling), self-patch, the random donor.
- compact pathway (plan): frozen sites <= 2 bands, and on the confirmation half the opChange hybrid
  rate at the frozen site >= half its all-sites rate and above both the donor-answer rate (copying)
  and the random donor's hybrid rate (lower bounds of the paired differences > 0).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from . import PROBE

OUT = PROBE / "results" / "stage2"
N_BOOT = 10_000
SEED = 0
N_BANDS = 5  # p33.stage2.BANDS


def load(root: Path = OUT) -> tuple[list[dict], dict]:
    p = root / "units.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
    s = root / "sites.json"
    return rows, (json.loads(s.read_text()) if s.exists() else {})


def ci(x: np.ndarray) -> list[float]:
    return [round(float(np.percentile(x, 2.5)), 4), round(float(np.percentile(x, 97.5)), 4)]


def rate(v: list[float]) -> dict:
    a = np.asarray(v, dtype=float)
    if not len(a):
        return {"n": 0}
    idx = np.random.default_rng(SEED).integers(0, len(a), (N_BOOT, len(a)))
    return {"n": len(a), "est": round(float(a.mean()), 4), "ci": ci(a[idx].mean(axis=1))}


def paired(a: dict[str, float], b: dict[str, float]) -> dict:
    """mean(a - b) over items in both, with a bootstrap interval."""
    ids = sorted(set(a) & set(b))
    return rate([a[i] - b[i] for i in ids])


def by_item(rows: list[dict], key: str, **match: Any) -> dict[str, float]:
    out = {}
    for r in rows:
        if all(r.get(k) == v for k, v in match.items()):
            out[r["item_id"]] = float(r[key])
    return out


def summarize(rows: list[dict], sites: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"n_rows": len(rows), "sites": sites}
    for half in ("discovery", "confirm"):
        h = [r for r in rows if r["half"] == half]
        if not h:
            continue
        k: dict[str, Any] = {"n_items": len({r["item_id"] for r in h})}
        un = by_item(h, "is_answer", unit="unpatched")
        un_lp = by_item(h, "lp_answer", unit="unpatched")
        mb = by_item(h, "is_answer", unit="maskB")
        k["acc_unpatched"] = rate(list(un.values()))
        k["acc_maskB"] = rate(list(mb.values()))
        k["drop_maskB"] = paired(un, mb)
        k["ablate"] = {s: paired(un, by_item(h, "is_answer", unit="ablate", site=s))
                       for s in sorted({r["site"] for r in h if r["unit"] == "ablate"})}
        k["knockout"] = {s: {"drop": paired(un, by_item(h, "is_answer", unit="knockout", site=s)),
                             "lp_drop": paired(un_lp, by_item(h, "lp_answer", unit="knockout",
                                                              site=s))}
                         for s in sorted({r["site"] for r in h if r["unit"] == "knockout"})}
        ko = by_item(h, "is_answer", unit="knockout", site="all|all")
        if ko:  # the no-information, no-disruption reference (log.md, side-agent note)
            k["acc_knockout_all"] = rate(list(ko.values()))
            k["maskB_minus_knockout_all"] = paired(mb, ko)
        self_ = by_item(h, "is_answer", unit="self")
        if self_:
            k["self_patch_vs_unpatched"] = paired(self_, un)
        k["explicit_y_acc"] = rate(list(by_item(h, "is_answer", unit="explicit_y").values()))
        dk: dict[str, Any] = {}
        for kind in sorted({r["donor"] for r in h if r["unit"] == "donor_own"}):
            own = [r for r in h if r["unit"] == "donor_own" and r["donor"] == kind]
            e: dict[str, Any] = {"donor_own_correct": rate([r["is_donor_answer"] for r in own])}
            for s in sorted({r["site"] for r in h if r["unit"] == "donor_patch"
                             and r["donor"] == kind}):
                m = {"unit": "donor_patch", "donor": kind, "site": s}
                e[s] = {x: rate(list(by_item(h, f"is_{x}", **m).values()))
                        for x in ("hybrid", "donor_answer", "answer")}
                e[s]["hybrid_minus_donor_answer"] = paired(by_item(h, "is_hybrid", **m),
                                                           by_item(h, "is_donor_answer", **m))
            dk[kind] = e
        out[half] = {**k, "donors": dk}
    out["compact"] = compact(rows, sites)
    return out


def compact(rows: list[dict], sites: dict) -> dict:
    fr = sites.get("frozen")
    if not fr:
        return {"decided": False}
    site = ("all" if len(fr["bands"]) == N_BANDS else "+".join(fr["bands"])) + "|" + fr["group"]
    h = [r for r in rows if r["half"] == "confirm"]
    m = {"unit": "donor_patch", "donor": "opChange"}
    hyb_site = by_item(h, "is_hybrid", **m, site=site)
    hyb_all = by_item(h, "is_hybrid", **m, site="all|all")
    copy_site = by_item(h, "is_donor_answer", **m, site=site)
    rnd_site = by_item(h, "is_hybrid", unit="donor_patch", donor="random", site=site)
    out = {"decided": bool(hyb_site), "site": site, "bands_le_2": len(fr["bands"]) <= 2,
           "hybrid_site": rate(list(hyb_site.values())),
           "hybrid_all": rate(list(hyb_all.values())),
           "hybrid_minus_copy": paired(hyb_site, copy_site),
           "hybrid_minus_random": paired(hyb_site, rnd_site)}
    if not hyb_site:
        return out
    ok = (out["bands_le_2"] and sites.get("compact", False)
          and out["hybrid_site"]["est"] >= 0.5 * out["hybrid_all"]["est"]
          and out["hybrid_minus_copy"].get("ci", [0])[0] > 0
          and out["hybrid_minus_random"].get("ci", [0])[0] > 0)
    return {**out, "pass": bool(ok)}


def main() -> None:
    rows, sites = load()
    s = summarize(rows, sites)
    (OUT / "analysis.json").write_text(json.dumps(s, indent=1) + "\n")
    print(json.dumps({k: v for k, v in s.items() if k != "sites"}, indent=1)[:6000])


if __name__ == "__main__":
    main()
