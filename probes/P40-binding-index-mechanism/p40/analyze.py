"""P40 scoring (laptop): rows -> results/summary.json and results/summary.md (plan.md "Measure").

    uv run python -m p40.analyze [results_dir]

- flip = top-1 is the row's ``a_alt`` (the answer the tested hypothesis predicts); invalid = top-1
  is none of the recipient's options. Rows exist only for valid pairs (recipient, and donor for
  copies, answered correctly unpatched; checked when the job was built).
- 95% intervals: 2,000 bootstrap resamples of items (``(family, item)`` groups), seed 20261013.
- Pools (plan): F's primary pools cells whose recipient and donor constructions are both in A, P,
  OC, PC, pass gate 1 (>= 0.90 per question) and whose own twin patch (pattern 111, both
  entities) flips >= 0.50 at L44 or L48.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parents[1]
SEED = 20261013
N_BOOT = 2000
CORE = ("A", "P", "OC", "PC")
SINGLE = {"100": "order", "010": "subject", "001": "role"}
CONTROLS = ("rand0", "rand1", "rand2", "rand3", "rand4", "zero")  # PC1: a baseline (log.md)


def load_rows(rdir: Path) -> dict[str, list[dict]]:
    out = {}
    for p in sorted((rdir / "rows").glob("*.jsonl")):
        out[p.stem] = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return out


def boot(rows: list[dict], value, group=lambda r: r.get("item"), seed: int = SEED) -> dict:
    """Mean of ``value(row)`` with an item-grouped bootstrap 95% interval."""
    if not rows:
        return {"n": 0, "items": 0, "mean": None, "lo": None, "hi": None}
    sums: dict[Any, float] = defaultdict(float)
    cnts: dict[Any, int] = defaultdict(int)
    for r in rows:
        g = group(r)
        sums[g] += float(value(r))
        cnts[g] += 1
    keys = list(sums)
    s = np.array([sums[k] for k in keys])
    c = np.array([cnts[k] for k in keys])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(N_BOOT, len(keys)))
    means = s[idx].sum(1) / np.maximum(c[idx].sum(1), 1)
    return {"n": int(c.sum()), "items": len(keys), "mean": float(s.sum() / c.sum()),
            "lo": float(np.quantile(means, 0.025)), "hi": float(np.quantile(means, 0.975))}  # fmt: skip


def flip(r: dict) -> int:
    return int(r["top1"] == r["a_alt"])


def kept(r: dict) -> int:
    return int(r["top1"] == r["a_rec"])


def invalid(r: dict) -> int:
    return int(r["top1"] not in r["cands"])


def rate(rows: list[dict], fn=flip) -> dict:
    out = boot(rows, fn)
    out["invalid"] = float(np.mean([invalid(r) for r in rows])) if rows else None
    return out


# ------------------------------------------------------------------ gates
def gate1(base: list[dict]) -> dict[str, Any]:
    acc: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in base:
        key = f"{r['family']}:{r['cons']}" + (":s16" if r.get("shift") else "")
        acc[key][r["q"]][0] += kept(r)
        acc[key][r["q"]][1] += 1
    out = {k: {q: v[0] / v[1] for q, v in d.items()} for k, d in acc.items()}
    return {"accuracy": out, "pass": {k: min(d.values()) >= 0.90 for k, d in out.items()}}


def rel_types(stimuli: list[dict]) -> dict[str, str]:
    from p40.stimuli import RELS

    return {s["sid"]: RELS[s["pred"]][2] for s in stimuli if s["family"] == "rel"}


def gate1_rel(base: list[dict], kinds: dict[str, str]) -> dict[str, Any]:
    acc: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in base:
        if r["family"] == "rel":
            acc[kinds[r["recip"]]][r["q"]][0] += kept(r)
            acc[kinds[r["recip"]]][r["q"]][1] += 1
    out = {k: {q: v[0] / v[1] for q, v in d.items()} for k, d in acc.items()}
    return {"accuracy": out, "pass": {k: min(d.values()) >= 0.90 for k, d in out.items()}}


# ------------------------------------------------------------------ F
def f_pool(f_rows: list[dict], g1: dict[str, bool]) -> tuple[list[str], dict]:
    own = {}
    for c in ("A", "P", "OC", "PC", "SC", "TOP"):
        best = 0.0
        for L in (44, 48):
            rs = [r for r in f_rows if r["rcons"] == c and r["dcons"] == c and not r["keep"]
                  and r["site"] == "both" and r["layers"] == [L]]  # fmt: skip
            if rs:
                best = max(best, float(np.mean([flip(r) for r in rs])))
        own[c] = best
    pool = [c for c in CORE if g1.get(f"trans:{c}", False) and own[c] >= 0.5]
    return pool, own


def f_summary(f_rows: list[dict], pool: list[str], L: int) -> dict:
    out: dict[str, Any] = {}
    for site in ("both", "second", "first"):
        rs = [r for r in f_rows if r["site"] == site and r["layers"] == [L]]
        if not rs:
            continue
        pooled = [r for r in rs if r["rcons"] in pool and r["dcons"] in pool]
        out[site] = {
            "patterns": {p: rate([r for r in pooled if r["pat"] == p])
                         for p in sorted({r["pat"] for r in pooled})},
            "by_recipient": {
                c: {p: rate([r for r in pooled if r["rcons"] == c and r["pat"] == p])
                    for p in SINGLE}
                for c in pool
            },
            "all_cells": {
                f"{c}<-{d}:{'kept' if k else 'rev'}": rate(
                    [r for r in rs if r["rcons"] == c and r["dcons"] == d and r["keep"] == k])
                for c in ("A", "P", "OC", "PC", "SC", "TOP")
                for d in ("A", "P", "OC", "PC", "SC", "TOP") for k in (True, False)
                if not (c == d and k)
            },
            "lpm": lpm(pooled),
        }  # fmt: skip
    return out


def lpm(rows: list[dict]) -> dict:
    """Linear-probability fit flip ~ 1 + Δo + Δs + Δr by WLS on the 8 pattern cells (equal to
    row-level OLS), with an item-bootstrap interval per coefficient."""
    if not rows:
        return {}
    pats = [f"{a}{b}{c}" for a in "01" for b in "01" for c in "01"]
    items = sorted({r["item"] for r in rows})
    ix = {it: i for i, it in enumerate(items)}
    S = np.zeros((len(items), 8))
    C = np.zeros((len(items), 8))
    for r in rows:
        S[ix[r["item"]], pats.index(r["pat"])] += flip(r)
        C[ix[r["item"]], pats.index(r["pat"])] += 1
    X = np.array([[1, int(p[0]), int(p[1]), int(p[2])] for p in pats], dtype=float)

    def fit(s, c):
        m = c > 0
        w = np.sqrt(c[m])
        beta, *_ = np.linalg.lstsq(X[m] * w[:, None], (s[m] / c[m]) * w, rcond=None)
        return beta

    b0 = fit(S.sum(0), C.sum(0))
    rng = np.random.default_rng(SEED)
    bs = np.array([fit(S[i].sum(0), C[i].sum(0))
                   for i in rng.integers(0, len(items), size=(N_BOOT, len(items)))])  # fmt: skip
    names = ("intercept", "order", "subject", "role")
    return {n: {"coef": float(b0[k]), "lo": float(np.quantile(bs[:, k], 0.025)),
                "hi": float(np.quantile(bs[:, k], 0.975))} for k, n in enumerate(names)}  # fmt: skip


def f_verdict(fs: dict, pool: list[str]) -> dict:
    """Plan verdicts at L48 from the both-entity and second-mention-only single-feature rates."""
    if len(pool) < 3 or "both" not in fs:
        return {"verdict": "cannot separate (fewer than 3 core constructions pass the gates)"}
    both = {SINGLE[p]: fs["both"]["patterns"].get(p, {}).get("mean") or 0.0 for p in SINGLE}
    sec = {SINGLE[p]: (fs.get("second", {}).get("patterns", {}).get(p, {}).get("mean") or 0.0)
           for p in SINGLE}  # fmt: skip
    # per recipient: which features it disagrees with (log.md "Analysis corrections" (1)): F is
    # contradicted there if another single-feature pattern reaches 15%, or F's own pattern exists
    # and is not the largest; a missing pattern (gated-out donor construction) is not evidence
    tops, against = {}, {}
    for c, d in fs["both"]["by_recipient"].items():
        vals = {SINGLE[p]: d[p]["mean"] for p in SINGLE if d[p]["mean"] is not None}
        tops[c] = max(vals, key=vals.get) if vals else None
        against[c] = {
            f: any(v >= 0.15 for g, v in vals.items() if g != f) or (f in vals and tops[c] != f)
            for f in SINGLE.values()
        }
    res = {"both": both, "second": sec, "top_by_recipient": tops}
    for feat in ("order", "subject", "role"):
        others = [f for f in both if f != feat]
        agree = sum(not a[feat] for a in against.values())
        if (both[feat] >= 0.5 and all(both[o] < 0.15 and sec[o] < 0.15 for o in others)
                and agree >= min(3, len(against))):  # fmt: skip
            res["verdict"] = {"order": "order index", "subject": "subject marker",
                              "role": "role"}[feat]  # fmt: skip
            res["recipients_agreeing"] = agree
            return res
    if sum(v >= 0.15 for v in both.values()) >= 2 or sum(v >= 0.15 for v in sec.values()) >= 2:
        res["verdict"] = "mixed"
    elif len({x for x in tops.values() if x}) > 1:
        res["verdict"] = "construction-specific"
    else:
        res["verdict"] = "no single feature reaches 50%"
    return res


# ------------------------------------------------------------------ V
def v_groups(v_rows: list[dict], kinds: dict[str, str]) -> dict[str, list[dict]]:
    """Recipient groups used by the verdicts and tables."""
    g: dict[str, list[dict]] = defaultdict(list)
    for r in v_rows:
        fam = r["family"]
        if fam == "trans":
            g[f"trans:{r['cons']}"].append(r)
            if r["cons"] in ("A", "P"):
                g["trans:AP"].append(r)
            if r["cons"] in ("OC", "PC", "SC"):
                g["trans:clefts"].append(r)
        elif fam == "rel":
            g[f"rel:{kinds[r['recip']]}"].append(r)
            g["rel:all"].append(r)
        elif fam == "ditr":
            g["ditr"].append(r)
        else:
            g["cap"].append(r)
    return g


def v_summary(v_rows: list[dict], kinds: dict[str, str]) -> dict:
    out: dict[str, Any] = {}
    groups = v_groups(v_rows, kinds)
    for gname, rs in sorted(groups.items()):
        by: dict[tuple, list[dict]] = defaultdict(list)
        for r in rs:
            by[(r["config"], r["vec"], r["alpha"])].append(r)
        out[gname] = {}
        for (cfg, vec, a), xs in sorted(by.items()):
            key = f"{cfg}:{vec}:a{a:g}"
            if gname == "ditr":
                aff = [r for r in xs if r["affected"]]
                una = [r for r in xs if not r["affected"]]
                out[gname][key] = {"moved": rate(aff), "kept_unaffected": rate(una, kept)}
            else:
                out[gname][key] = rate(xs)
    return out


def v_verdict(vs: dict) -> dict:
    def m(g: str, key: str, field: str | None = None) -> float | None:
        x = vs.get(g, {}).get(key)
        if x is None:
            return None
        if field:
            x = x[field]
        return x["mean"]

    res: dict[str, Any] = {}
    for cfg in ("L48", "band"):
        ctrl = {c: m("trans:AP", f"{cfg}:{c}:a1") for c in CONTROLS}
        ctrl_ok = all(v is not None and v <= 0.10 for v in ctrl.values())
        main = m("trans:AP", f"{cfg}:order:a1")
        pc1 = m("trans:AP", f"{cfg}:pc1:a1")
        others = {
            "clefts": m("trans:clefts", f"{cfg}:order:a1"),
            "TOP": m("trans:TOP", f"{cfg}:order:a1"),
            "rel:cmp": m("rel:cmp", f"{cfg}:order:a1"),
            "rel:lr": m("rel:lr", f"{cfg}:order:a1"),
            "rel:fb": m("rel:fb", f"{cfg}:order:a1"),
            "ditr12": m("ditr", f"{cfg}:order:a1", "moved"),
        }
        works = main is not None and main >= 0.5 and ctrl_ok
        n_other = sum(v is not None and v >= 0.3 for v in others.values())
        cap = {k: m("cap", f"{cfg}:{k}:a1") for k in ("cap_name", "cap_both", "order_AP",
                                                     "order")}  # fmt: skip
        cap_best = max((m("cap", f"{cfg}:{k}:a{a:g}") or 0.0)
                       for k in ("cap_name", "cap_both") for a in (0.5, 1.0, 1.5, 2.0))  # fmt: skip
        cap_ctrl = {c: m("cap", f"{cfg}:{c}:a1") for c in CONTROLS}
        res[cfg] = {
            "order_on_held_AP_a1": main, "controls": ctrl, "controls_ok": ctrl_ok,
            "pc1_a1": pc1, "literal_plan_rule_incl_pc1": (
                main is not None and main >= 0.5 and ctrl_ok and pc1 is not None and pc1 <= 0.10),
            "works_in_frame": works, "others": others, "n_other_ge_30": n_other,
            "general": works and n_other >= 2,
            "cap": cap, "cap_best_any_alpha": cap_best, "cap_controls": cap_ctrl,
            "positive_control_pass": cap_best >= 0.5 and all(
                v is not None and v <= 0.10 for v in cap_ctrl.values()),
            "transfer_cap_to_events": m("trans:AP", f"{cfg}:cap_name:a1"),
            "transfer_events_to_cap": cap["order_AP"],
        }  # fmt: skip
    return res


# ------------------------------------------------------------------ L, C, T
def l_summary(rows: list[dict]) -> dict:
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r.get("site", "?"), r["donor_kind"], r["layer"])].append(r)
    out: dict[str, Any] = defaultdict(dict)
    for (site, kind, L), rs in sorted(by.items()):
        out[f"{kind}:{site}"][str(L)] = rate(rs)
    return dict(out)


def c_summary(rows: list[dict]) -> dict:
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["what"], r["layer"])].append(r)
    return {f"{w}@{L}": rate(rs) for (w, L), rs in sorted(by.items())}


def t_summary(rows: list[dict], sents: dict[str, dict]) -> dict:
    """Pair swaps: "moved" = top-1 is the donor's answer (affected questions), "kept" = the
    recipient's answer (unaffected). Dative donor (roles kept, theme/goal order swapped; log.md
    correction (3)): "to_partner" = top-1 is the theme<->goal partner of the recipient's answer
    (``cands`` are the entities' tokens in mention order)."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r["donor_kind"], r["layer"], r["q"], r["affected"])].append(r)
    out = {}
    for (k, L, q, aff), rs in sorted(by.items()):
        if k == "dative":
            out[f"{k}@{L}:{q}:kept"] = rate(rs, kept)
            if q != "agent":

                def partner(r):
                    s = sents[r["recip"]]
                    other = "goal" if r["q"] == "theme" else "theme"
                    return int(r["top1"] == r["cands"][s["roles"].index(other)])

                out[f"{k}@{L}:{q}:to_partner"] = rate(rs, partner)
            continue
        out[f"{k}@{L}:{q}:{'moved' if aff else 'kept'}"] = rate(rs, flip if aff else kept)
    return out


# ------------------------------------------------------------------ report
def fmt(x: dict | None, pct: bool = True) -> str:
    if not x or x.get("mean") is None:
        return "–"
    f = 100 if pct else 1
    return f"{x['mean'] * f:.1f} [{x['lo'] * f:.1f}, {x['hi'] * f:.1f}]"


def analyze(rdir: Path) -> dict:
    rows = load_rows(rdir)
    stim = json.loads((HERE / "data" / "stimuli.json").read_text())["sentences"]
    kinds = rel_types(stim)
    out: dict[str, Any] = {}
    g1 = gate1(rows.get("baseline", []))
    out["gate1"] = g1
    out["gate1_rel"] = gate1_rel(rows.get("baseline", []), kinds)
    st = json.loads((rdir / "status.json").read_text()) if (rdir / "status.json").exists() else {}
    out["gate2"] = st.get("gate2")
    f_rows = rows.get("F_48", []) + rows.get("F_other", [])
    if f_rows:
        pool, own = f_pool(f_rows, g1["pass"])
        out["F_pool"] = pool
        out["F_own_twin"] = own
        out["F"] = {str(L): f_summary(f_rows, pool, L) for L in (40, 44, 48, 52)
                    if any(r["layers"] == [L] for r in f_rows)}  # fmt: skip
        out["F_verdict"] = f_verdict(out["F"].get("48", {}), pool)
        out["F_verdict_L44"] = f_verdict(out["F"].get("44", {}), pool)
        # sensitivity (exploratory): every core construction, gates ignored
        out["F_all_core_L48"] = f_summary([r for r in f_rows if r["layers"] == [48]],
                                          list(CORE), 48)  # fmt: skip
        out["F_all_core_verdict"] = f_verdict(out["F_all_core_L48"], list(CORE))
    v_rows = rows.get("V_48", []) + rows.get("V_band", [])
    if v_rows:
        rel_ok = {k for k, ok in out["gate1_rel"]["pass"].items() if ok}
        v_rows = [r for r in v_rows if r["family"] != "rel" or kinds[r["recip"]] in rel_ok]
        out["V"] = v_summary(v_rows, kinds)
        out["V_verdict"] = v_verdict(out["V"])
    if rows.get("L"):
        out["L"] = l_summary(rows["L"])
    if rows.get("C"):
        out["C"] = c_summary(rows["C"])
    if rows.get("T"):
        out["T"] = t_summary(rows["T"], {s["sid"]: s for s in stim})
    fu = rdir / "followup_cap"
    if (fu / "rows").exists():  # follow-up F1 (log.md): CAPITALS with the bold prefill
        fr = load_rows(fu)
        f1: dict[str, Any] = {"status": json.loads((fu / "status.json").read_text())}
        if fr.get("C"):
            f1["C"] = c_summary(fr["C"])
        vr = fr.get("V_48", []) + fr.get("V_band", [])
        if vr:
            f1["V"] = v_summary(vr, kinds).get("cap", {})
        out["F1"] = f1
    for name in ("vectors", "probes", "lens", "smoke", "timing"):
        p = rdir / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text())
    return out


def report(s: dict) -> str:
    L = ["# P40 summary (generated by p40/analyze.py)", ""]
    L.append(f"Gate 2 (A/P twin, both entities, L48): {s.get('gate2')}")
    L.append("")
    L.append("## Gate 1 (unpatched accuracy per question)")
    for k, d in sorted(s["gate1"]["accuracy"].items()):
        L.append(f"- {k}: " + ", ".join(f"{q} {v:.3f}" for q, v in d.items()))
    for k, d in sorted(s["gate1_rel"]["accuracy"].items()):
        L.append(f"- rel type {k}: " + ", ".join(f"{q} {v:.3f}" for q, v in d.items()))
    if "F" in s:
        L += ["", f"## F (pool {s['F_pool']}; own twin flips {s['F_own_twin']})", ""]
        L.append(f"Verdict L48: {json.dumps(s['F_verdict'])}")
        L.append(f"Verdict L44: {json.dumps(s['F_verdict_L44'])}")
        for Ls, fs in s["F"].items():
            for site, d in fs.items():
                L += ["", f"### L{Ls}, {site}", "", "| pattern (o,s,r) | flip % [95% CI] | n |",
                      "|---|---|---|"]  # fmt: skip
                for p, x in d["patterns"].items():
                    L.append(f"| {p} {SINGLE.get(p, '')} | {fmt(x)} | {x['n']} |")
                if d.get("lpm"):
                    L.append("")
                    L.append("LPM: " + ", ".join(
                        f"{k} {v['coef']:.3f} [{v['lo']:.3f}, {v['hi']:.3f}]"
                        for k, v in d["lpm"].items()))  # fmt: skip
                L.append("")
                L.append("By recipient (order / subject / role only): " + "; ".join(
                    f"{c}: " + " / ".join(fmt(v[p]) for p in SINGLE)
                    for c, v in d["by_recipient"].items()))  # fmt: skip
    if "V" in s:
        L += ["", "## V verdicts", "", "```", json.dumps(s["V_verdict"], indent=1), "```"]
        L += ["", "## V (flip % [95% CI])", ""]
        for g, d in s["V"].items():
            L.append(f"### {g}")
            for k, x in d.items():
                if "moved" in x:
                    L.append(f"- {k}: moved {fmt(x['moved'])}, kept {fmt(x['kept_unaffected'])}")
                else:
                    L.append(f"- {k}: {fmt(x)} (invalid {x['invalid']:.3f})")
    if "F1" in s:
        f1 = s["F1"]
        acc = f1["status"].get("cap_accuracy")
        wrong = f1["status"].get("cap_wrong_top1")
        L += ["", "## Follow-up F1: CAPITALS (bold prefill)", "",
              f"Accuracy {acc}; wrong top-1 {wrong}", ""]  # fmt: skip
        L += [f"- C {k}: {fmt(x)}" for k, x in f1.get("C", {}).items()]
        L += [f"- V {k}: {fmt(x)} (invalid {x['invalid']:.3f})" for k, x in f1.get("V", {}).items()]
    if "L" in s:
        L += ["", "## L (flip %, by layer)", ""]
        for k, d in s["L"].items():
            L.append(f"- {k}: " + ", ".join(f"L{lay} {x['mean'] * 100:.0f}" for lay, x in
                                            sorted(d.items(), key=lambda t: int(t[0]))))  # fmt: skip
    if "C" in s:
        L += ["", "## C (CAPITALS other-order name patch)", ""]
        L += [f"- {k}: {fmt(x)}" for k, x in s["C"].items()]
    if "T" in s:
        L += ["", "## T (ditransitives)", ""]
        L += [f"- {k}: {fmt(x)}" for k, x in s["T"].items()]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rdir = Path(argv[0]) if argv else HERE / "results"
    s = analyze(rdir)
    (rdir / "summary.json").write_text(json.dumps(s, indent=1) + "\n")
    (rdir / "summary.md").write_text(report(s))
    print(report(s)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
