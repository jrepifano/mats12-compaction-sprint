"""P10 analysis: Arm V first actions and Arm H resume measures -> results/analysis.json and
results/tables.md. Every measure is scored in code (plan "Measure").

Contrasts are paired: each item's (Arm V) or context's (Arm H) rate is its mean over seeds; the
difference of two conditions is averaged over items and given a 95% percentile interval from a
2,000-resample bootstrap clustered by transcript (Arm V: P01 `tid`, which two prompts can share)
or by task (Arm H: `task_id`, as in P07).

    uv run --group swarm python probes/P10-fabricated-history-trust/p10/analyze.py
"""
from __future__ import annotations

import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p10 import armh, armv
from p10 import common as K

N_BOOT = 2000
ARMV_MEASURES = ("finish", "test", "claims_pass")
ARMH_MEASURES = ("accepted_unverified", "done", "tested", "tested_within_3",
                 "final_repeats_claim", "cheat")


def wilson(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def paired(rows: list[dict], measure: str, a: str, b: str, unit: str, cluster: str,
           seed: int = K.SEED) -> dict:
    """mean over units of (rate_a - rate_b), each unit's rate its mean over seeds; bootstrap
    over clusters. Units missing either condition are dropped."""
    per: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    cl: dict[str, str] = {}
    for r in rows:
        v = r.get(measure)
        if v is None:
            continue
        per[r[unit]][r["cond"]].append(float(v))
        cl[r[unit]] = r[cluster]
    diffs = {u: sum(d[a]) / len(d[a]) - sum(d[b]) / len(d[b])
             for u, d in per.items() if d.get(a) and d.get(b)}
    if not diffs:
        return {"n_units": 0}
    by_c: dict[str, list[float]] = defaultdict(list)
    for u, d in diffs.items():
        by_c[cl[u]].append(d)
    cs = sorted(by_c)
    rng = random.Random(seed)
    boots = []
    for _ in range(N_BOOT):
        xs = [x for c in (rng.choice(cs) for _ in cs) for x in by_c[c]]
        boots.append(sum(xs) / len(xs))
    boots.sort()
    return {"n_units": len(diffs), "n_clusters": len(cs),
            "diff": sum(diffs.values()) / len(diffs),
            "lo": boots[int(0.025 * N_BOOT)], "hi": boots[int(0.975 * N_BOOT) - 1]}


def rates(rows: list[dict], measure: str, cond: str) -> dict:
    xs = [r[measure] for r in rows if r["cond"] == cond and r.get(measure) is not None]
    x = sum(bool(v) for v in xs)
    lo, hi = wilson(x, len(xs))
    return {"x": x, "n": len(xs), "rate": x / len(xs) if xs else None, "lo": lo, "hi": hi}


# --- Arm V ---------------------------------------------------------------------------------------
def armv_rows() -> list[dict]:
    items = {i["item_id"]: i for i in armv.pool()}
    rows = []
    seen = set()
    for r in K.load_jsonl(armv.OUT):
        if r.get("error") or r["key"] in seen:
            continue
        seen.add(r["key"])
        it = items[r["item_id"]]
        s = armv.score(r, it["block"])
        rows.append({"key": r["key"], "agent": r["agent"], "cond": r["cond"], "seed": r["seed"],
                     "item_id": r["item_id"], "tid": it["tid"], "harness": it["harness"],
                     "slice": it["slice"], "removed_chars": it["removed_chars"],
                     "block_claims_pass": it["block_claims_pass"], "provider": r.get("provider"),
                     "first_tool": (r["tool_calls"] or [{}])[0].get("name"), **s})
    return rows


def armv_analysis(rows: list[dict]) -> dict:
    subsets = {
        "all": lambda r: True,
        "removed_ge_200": lambda r: r["removed_chars"] >= K.MIN_REMOVED_CHARS,   # primary (D1)
        "pass_block": lambda r: r["block_claims_pass"],
        "pass_block_ge_200": lambda r: r["block_claims_pass"]
        and r["removed_chars"] >= K.MIN_REMOVED_CHARS,
        "no_pass_block": lambda r: not r["block_claims_pass"],
    }
    out: dict = {"n_rows": len(rows), "errors": None}
    for agent in sorted({r["agent"] for r in rows}):
        for name, f in subsets.items():
            sub = [r for r in rows if r["agent"] == agent and f(r)]
            res: dict = {}
            for m in ARMV_MEASURES:
                res[m] = {c: rates(sub, m, c) for c in ("intact", "removed", "nopass")}
                res[m]["intact_minus_removed"] = paired(sub, m, "intact", "removed", "item_id",
                                                        "tid")
                res[m]["intact_minus_nopass"] = paired(sub, m, "intact", "nopass", "item_id",
                                                       "tid")
                res[m]["nopass_minus_removed"] = paired(sub, m, "nopass", "removed", "item_id",
                                                        "tid")
            acts: dict = defaultdict(lambda: defaultdict(int))
            for r in sub:
                acts[r["cond"]][r["action"]] += 1
            res["actions"] = {c: dict(v) for c, v in acts.items()}
            out[f"{agent}|{name}"] = res
    return out


# --- Arm H ---------------------------------------------------------------------------------------
def armh_rows() -> list[dict]:
    from p07.measures import episode_measures

    det = {}
    dpath = K.RESULTS / "armh" / "detector.csv"
    if dpath.exists():
        with dpath.open() as f:
            for r in csv.DictReader(f):
                det[r["episode_id"]] = {"True": True, "False": False}.get(r["cheat"])
    rows = []
    for p in sorted(armh.EPISODES.rglob("*.json")):
        if ".failed" in p.name:
            continue
        a = json.loads(p.read_text())
        m = episode_measures(a)
        meta = a["meta"]
        rows.append({**m, "agent": meta["model_slug"], "source": meta["source"],
                     "cond": meta["cond"], "seed": meta["seed"], "harness": meta.get("harness"),
                     "done": m["end_reason"] == "done", "cheat": det.get(m["episode_id"]),
                     "path": str(p.relative_to(K.PROBE))})
    return rows


def armh_analysis(rows: list[dict]) -> dict:
    out: dict = {"n_rows": len(rows)}
    for agent in sorted({r["agent"] for r in rows}):
        for source in sorted({r["source"] for r in rows}):
            sub = [r for r in rows if r["agent"] == agent and r["source"] == source]
            if not sub:
                continue
            res = {}
            for m in ARMH_MEASURES:
                res[m] = {c: rates(sub, m, c) for c in armh.CONDITIONS}
                res[m]["intact_minus_removed"] = paired(sub, m, "intact", "removed", "ctx_id",
                                                        "task_id")
            out[f"{agent}|{source}"] = res
    out["cross"] = armh_cross(rows)
    return out


# Cross-source contrasts (F3 and pooled): conditions are (source, cond) pairs, paired per context
# within agent; "pooled" pairs per agent x context and clusters by task.
CROSS = (("semi", "intact", "prose", "intact"), ("prose", "intact", "semi", "removed"),
         ("semi", "intact", "semi", "removed"), ("semi@pi_mono", "intact", "semi@pi_mono",
                                                  "removed"),
         ("natural", "intact", "natural", "removed"))


def armh_cross(rows: list[dict], measure: str = "accepted_unverified") -> dict:
    out = {}
    agents = sorted({r["agent"] for r in rows})
    for sa, ca, sb, cb in CROSS:
        a, b = f"{sa}:{ca}", f"{sb}:{cb}"
        for agent in agents + ["pooled"]:
            sub = [{**r, "cond": f"{r['source']}:{r['cond']}", "unit": f"{r['agent']}|{r['ctx_id']}"}
                   for r in rows if (agent == "pooled" or r["agent"] == agent)
                   and f"{r['source']}:{r['cond']}" in (a, b)]
            if not sub:
                continue
            out[f"{agent}|{a} - {b}"] = {"a": rates(sub, measure, a), "b": rates(sub, measure, b),
                                         "diff": paired(sub, measure, a, b, "unit", "task_id")}
    return out


# --- tables --------------------------------------------------------------------------------------
def _pct(r: dict) -> str:
    if not r.get("n"):
        return "—"
    return f"{r['x']}/{r['n']} ({100 * r['rate']:.0f}%)"


def _diff(d: dict) -> str:
    if not d.get("n_units"):
        return "—"
    return f"{100 * d['diff']:+.0f} ({100 * d['lo']:+.0f} to {100 * d['hi']:+.0f}), " \
           f"n={d['n_units']}"


def tables(v: dict, h: dict) -> str:
    L = ["# P10 tables (generated by `p10/analyze.py`; do not edit)", "",
         "## Arm V: first action after the resume (one call; real DeepSeek fabrications)", "",
         ("Rates over item × seed calls; differences are paired per item (points, 95% "
          "bootstrap clustered by transcript)."), "",
         ("| Agent | Items | Measure | Intact | Removed | Pass removed (F1) | Intact − removed "
          "| Intact − pass removed |"), "|---|---|---|---|---|---|---|---|"]
    for k, res in v.items():
        if "|" not in k:
            continue
        agent, sub = k.split("|")
        for m in ARMV_MEASURES:
            r = res[m]
            L.append(f"| {agent} | {sub} | {m} | {_pct(r['intact'])} | {_pct(r['removed'])} | "
                     f"{_pct(r['nopass'])} | {_diff(r['intact_minus_removed'])} | "
                     f"{_diff(r['intact_minus_nopass'])} |")
    L += ["", "## Arm H: full resume in P07's harness (impossible tests; 12 turns)", "",
          ("Rates over context × seed episodes; differences paired per context (points, 95% "
           "bootstrap clustered by task). `semi` = SEMI-SYNTHETIC (P07 GLM summary + a "
           "constructed passing pytest block); `natural` = DeepSeek's own fabrications; "
           "`prose` = P07's summary_L1 cell (F3); `@pi_mono` = pi-mono's wrapper (F2)."), "",
          "| Agent | Source | Measure | Intact | Removed | Intact − removed |",
          "|---|---|---|---|---|---|"]
    for k, res in h.items():
        if "|" not in k or k == "cross":
            continue
        agent, source = k.split("|")
        for m in ARMH_MEASURES:
            r = res[m]
            L.append(f"| {agent} | {source} | {m} | {_pct(r['intact'])} | {_pct(r['removed'])} "
                     f"| {_diff(r['intact_minus_removed'])} |")
    L += ["", "## Arm H cross-source contrasts: accepted unverified (F3; pooled = both agents)",
          "", "Paired per agent × context; 95% bootstrap clustered by task.", "",
          "| Agent | A | B | A | B | A − B |", "|---|---|---|---|---|---|"]
    for k, r in h.get("cross", {}).items():
        agent, ab = k.split("|", 1)
        a, b = ab.split(" - ")
        L.append(f"| {agent} | {a} | {b} | {_pct(r['a'])} | {_pct(r['b'])} | {_diff(r['diff'])} |")
    return "\n".join(L) + "\n"


def main() -> int:
    vr, hr = armv_rows(), armh_rows()
    v, h = armv_analysis(vr), armh_analysis(hr)
    K.RESULTS.mkdir(parents=True, exist_ok=True)
    (K.RESULTS / "analysis.json").write_text(json.dumps({"armv": v, "armh": h}, indent=1))
    (K.RESULTS / "tables.md").write_text(tables(v, h))
    for name, rows in (("armv_measures.csv", vr), ("armh_measures.csv", hr)):
        if rows:
            keys = list(dict.fromkeys(k for r in rows for k in r))
            with (K.RESULTS / name).open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(rows)
    print(tables(v, h))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
