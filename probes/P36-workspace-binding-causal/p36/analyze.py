"""P36 scoring (laptop): flip rates, invalid rates, logit-difference shifts, grouped bootstrap
intervals, the plan's decision rules, and decomposition summaries.

    uv run python -m p36.analyze            # reads results/, writes results/summary.{json,md}

Definitions (plan.md "Measure"):
- flip: top-1 token is the recipient's other entity (the role-reversed answer);
- invalid: top-1 is neither entity;
- LD = logit(recipient's answer) - logit(other entity); shift = (LD_0 - LD_patched) /
  (LD_0 - LD_twin), LD_0 the recipient's unpatched LD, LD_twin the same token pair's LD in the
  role-reversed twin's unpatched run;
- intervals: 2,000 bootstrap resamples of base items (all of an item's voices, roles, questions
  and directions together), seed 20261013, percentile 95%.
"""

from __future__ import annotations

import gzip
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from p36 import stimuli as S

HERE = Path(__file__).resolve().parents[1]
RES = HERE / "results"
SEED = 20261013
N_BOOT = 2000
EQUIV = 0.10  # "does little": upper 95% bound <= 10 points
MIN_GAP = 0.10  # residual (or recon) swap at least 10 points above zero


def read_rows(phase: str, res: Path = RES) -> list[dict]:
    p = res / "rows" / f"{phase}.jsonl"
    if p.exists():
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    gz = p.with_suffix(".jsonl.gz")
    if gz.exists():
        return [json.loads(x) for x in gzip.open(gz, "rt") if x.strip()]
    return []


def item_of(sid: str) -> int:
    return int(sid.split("-")[0])


def voice_of(sid: str) -> str:
    return sid.split("-")[1]


def annotate(rows: list[dict], base: dict[tuple[str, str], dict]) -> None:
    for r in rows:
        r["flip"] = int(r["top1"] == r["a_rev"])
        r["invalid"] = int(r["top1"] not in (r["a_rec"], r["a_rev"]))
        b0 = base[(r["recip"], r["q"])]
        tw = base.get((S.twin_sid(r["recip"]), r["q"]))
        ld0 = b0["logit_rec"] - b0["logit_rev"]
        ldp = r["logit_rec"] - r["logit_rev"]
        r["shift"] = None
        if tw is not None:
            # in the twin's run its a_rec is our a_rev and vice versa
            ldt = tw["logit_rev"] - tw["logit_rec"]
            if abs(ld0 - ldt) > 1e-6:
                r["shift"] = (ld0 - ldp) / (ld0 - ldt)


def _boot(by_item: dict[int, list[float]], stat, rng: random.Random) -> tuple[float, float]:
    items = list(by_item)
    vals = []
    for _ in range(N_BOOT):
        pick = [items[rng.randrange(len(items))] for _ in items]
        vals.append(stat([v for i in pick for v in by_item[i]]))
    vals.sort()
    return vals[int(0.025 * N_BOOT)], vals[int(0.975 * N_BOOT) - 1]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def rate_ci(rows: list[dict], key: str = "flip", seed: int = SEED) -> dict[str, Any]:
    by: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[item_of(r["recip"])].append(float(r[key]))
    allv = [v for vs in by.values() for v in vs]
    if not allv:
        return {"n": 0}
    lo, hi = _boot(by, mean, random.Random(seed))
    return {"n": len(allv), "items": len(by), "mean": mean(allv), "lo": lo, "hi": hi}


def paired_diff_ci(a: list[dict], b: list[dict], seed: int = SEED) -> dict[str, Any]:
    """mean(flip_a - flip_b) over rows matched on (recip, q), grouped by base item."""
    kb = {(r["recip"], r["q"]): r for r in b}
    by: dict[int, list[float]] = defaultdict(list)
    for r in a:
        m = kb.get((r["recip"], r["q"]))
        if m is not None:
            by[item_of(r["recip"])].append(float(r["flip"] - m["flip"]))
    allv = [v for vs in by.values() for v in vs]
    if not allv:
        return {"n": 0}
    lo, hi = _boot(by, mean, random.Random(seed + 1))
    return {"n": len(allv), "mean": mean(allv), "lo": lo, "hi": hi}


def cell_key(r: dict) -> str:
    return f"{r['site']}@{'-'.join(map(str, r['layers']))}"


def arm_key(r: dict) -> str:
    d = "" if r["dict"] is None else f":{r['dict']}:k{r['k']}"
    return f"{r['kind']}:{r['op']}{d}"


def summarize_arms(rows: list[dict]) -> dict[str, dict[str, Any]]:
    """{cell: {arm: {all/q/voice: flip, invalid, shift}}}."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(cell_key(r), arm_key(r))].append(r)
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for (cell, arm), rs in sorted(groups.items()):
        entry: dict[str, Any] = {}
        subsets = {"all": rs}
        for q in S.QUESTIONS:
            subsets[f"q={q}"] = [r for r in rs if r["q"] == q]
        for v in S.VOICES:
            subsets[f"voice={v}"] = [r for r in rs if voice_of(r["recip"]) == v]
        for name, sub in subsets.items():
            entry[name] = {
                "flip": rate_ci(sub, "flip"),
                "invalid": mean([r["invalid"] for r in sub]),
                "shift_mean": mean([r["shift"] for r in sub if r["shift"] is not None]),
            }
        out[cell][arm] = entry
    return out


def decide(rows: list[dict], cell: str, dicts: list[str], ks: list[int], k1: int = 25) -> dict:
    """The plan's decision rules at one cell, from twin j-swap and r-swap rows."""
    by: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
    for r in rows:
        if cell_key(r) == cell and r["kind"] == "twin" and r["op"] in ("j", "r"):
            by[(r["dict"], r["k"], r["op"])].append(r)

    def subsets(rs: list[dict]) -> dict[str, list[dict]]:
        out = {f"q={q}": [r for r in rs if r["q"] == q] for q in S.QUESTIONS}
        out.update({f"voice={v}": [r for r in rs if voice_of(r["recip"]) == v] for v in S.VOICES})
        return out

    per: dict[str, Any] = {}
    for d in dicts:
        for k in ks:
            j, r = by.get((d, k, "j"), []), by.get((d, k, "r"), [])
            if not j or not r:
                continue
            sj, sr = subsets(j), subsets(r)
            res = {}
            for name in sj:
                diff = paired_diff_ci(sr[name], sj[name])
                res[name] = {
                    "r_minus_j": diff,
                    "r": rate_ci(sr[name]),
                    "j": rate_ci(sj[name]),
                }
            r_gt = all(
                v["r_minus_j"]["n"] and v["r_minus_j"]["lo"] > 0 and v["r"]["mean"] >= MIN_GAP
                for v in res.values()
            )
            j_gt = all(
                v["r_minus_j"]["n"] and v["r_minus_j"]["hi"] < 0 and v["j"]["mean"] >= MIN_GAP
                for v in res.values()
            )
            j_little = all(v["j"]["n"] and v["j"]["hi"] <= EQUIV for v in res.values())
            r_little = all(v["r"]["n"] and v["r"]["hi"] <= EQUIV for v in res.values())
            per[f"{d}:k{k}"] = {
                "residual_greater": r_gt,
                "recon_greater": j_gt,
                "recon_does_little": j_little,
                "residual_does_little": r_little,
                "subsets": res,
            }
    jl = [v for key, v in per.items() if key.startswith("jlens:")]
    jpp = [v for key, v in per.items() if key.startswith("jpp:")]
    verdict = "both carry it, unequally, or neither reliable"
    if (
        jl
        and all(v["residual_greater"] for v in jl)
        and (not jpp or all(v["residual_greater"] for v in jpp))
    ):
        verdict = "greater residual-swap effect (all J-lens k" + (" and J++)" if jpp else ")")
        if all(v["recon_does_little"] for v in jl):
            verdict += "; reconstruction does little"
    elif per.get(f"jlens:k{k1}", {}).get("recon_greater"):
        verdict = "greater reconstruction-swap effect at k=25"
        if per[f"jlens:k{k1}"]["residual_does_little"]:
            verdict += "; residual does little"
    elif any(v["residual_greater"] for v in jl):
        verdict = "cut-dependent (residual greater at some k only)"
    return {"verdict": verdict, "per": per}


def decomp_summary(res: Path = RES) -> list[dict]:
    p = res / "decomp_stats.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        m = json.loads(line)
        for si, slot in enumerate(("final", "e1", "e2")):
            fve = [row[si] for row in m["fve"]]
            na = [row[si] for row in m["n_active"]]
            kkt = [row[si] for row in m["kkt"]]
            hn = [row[si] for row in m["h_norm"]]
            jn = [row[si] for row in m["j_norm"]]
            out.append({
                "dict": m["dict"], "k": m["k"], "layer": m["layer"], "slot": slot,
                "fve_mean": mean(fve), "fve_min": min(fve), "fve_max": max(fve),
                "n_active_mean": mean(na), "n_active_min": min(na),
                "kkt_max": max(kkt), "j_over_h": mean([j / h for j, h in zip(jn, hn, strict=True)]),
                "h_norm_mean": mean(hn),
            })  # fmt: skip
    return out


def twin_share(res: Path = RES) -> list[dict]:
    p = res / "twin_share.jsonl"
    if not p.exists():
        return []
    acc: dict[tuple, list[list[float]]] = defaultdict(lambda: [[], []])
    for line in p.read_text().splitlines():
        r = json.loads(line)
        for si, slot in enumerate(("final", "e1", "e2")):
            a = acc[(r["dict"], r["k"], r["layer"], slot)]
            a[0].append(r["dj2"][si])
            a[1].append(r["dh2"][si])
    out = []
    for (d, k, L, slot), (dj, dh) in sorted(acc.items()):
        ratios = sorted(x / y for x, y in zip(dj, dh, strict=True) if y > 0)
        out.append({
            "dict": d, "k": k, "layer": L, "slot": slot,
            "share_ratio_of_sums": sum(dj) / sum(dh) if sum(dh) else float("nan"),
            "share_median": ratios[len(ratios) // 2] if ratios else float("nan"),
        })  # fmt: skip
    return out


def gate_tables(res: Path = RES) -> dict:
    base = read_rows("baseline", res)
    acc: dict[str, dict] = {}
    for q in S.QUESTIONS:
        for v in (*S.VOICES, "all"):
            rs = [r for r in base if r["q"] == q and (v == "all" or voice_of(r["recip"]) == v)]
            acc[f"{q}/{v}"] = mean([float(r["top1"] == r["a_rec"]) for r in rs])
    return {"accuracy": acc, "n_baseline": len(base)}


def main() -> int:
    status = json.loads((RES / "status.json").read_text())
    base = {(r["recip"], r["q"]): r for r in read_rows("baseline")}
    out: dict[str, Any] = {"status": status, "gates": gate_tables()}
    for phase in ("stage0", "band", "pass1", "pass2"):
        rows = read_rows(phase)
        if not rows:
            continue
        annotate(rows, base)
        out[phase] = summarize_arms(rows)
    p12 = read_rows("pass1") + read_rows("pass2")
    s0 = read_rows("stage0") + read_rows("band")
    annotate(p12, base)
    annotate(s0, base)
    out["decisions"] = {}
    for site, layers in status.get("selected", {}).items():
        cell = f"{site}@{'-'.join(map(str, layers))}"
        out["decisions"][cell] = decide(p12, cell, ["jlens", "logit", "jpp"], [16, 25, 100, 400])
    for site, cells in status.get("cells_pass1", {}).items():
        for layers in cells:
            cell = f"{site}@{'-'.join(map(str, layers))}"
            out["decisions"].setdefault(cell, decide(p12, cell, ["jlens", "logit"], [25]))
    out["decomp"] = decomp_summary()
    out["twin_share"] = twin_share()
    (RES / "summary.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(
        json.dumps(
            {
                "gates": out["gates"],
                "verdicts": {c: d["verdict"] for c, d in out["decisions"].items()},
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
