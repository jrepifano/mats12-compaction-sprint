"""P39 scoring (laptop): per-arm flip / invalid / logit-difference shift with item-bootstrap
intervals, share and first-order attribution, family pooling over random draws, the plan's
outcome rule (O1-O4) and J-alignment verdict, dose-curve tables and plots, diagnostics.

    uv run python -m p39.analyze        # reads results/, writes results/summary.{json,md}, plots

Definitions (plan.md "Measure"): flip, invalid and shift as P36 (``p36.analyze.annotate``);
attribution a = sum g.delta / sum g.D over cases (both tokens; g the recipient's dLD/dh under the
case's question). Scopes: ``held`` = recipients from P36's held-entity and held-verb items (the
primary comparison), ``all`` = every item (unfitted arms only).
"""

from __future__ import annotations

import gzip
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from p36 import analyze as P36A
from p36 import stimuli as S

HERE = Path(__file__).resolve().parents[1]
RES = HERE / "results"
SEED = 20261013
PHASES = ("anchors", "primary", "secondary", "crossvoice", "followup1")
ROLE_MATCHED = ("pca_m", "ax_order_m", "ax_md_m")
RANDOM_MATCHED = ("iso_m", "cov_m")
DRAW_FAMILIES = ("iso_m", "cov_m", "jatoms", "pot_iso", "r1d_m") + tuple(
    f"{p}_f{f}" for p in ("iso", "cov") for f in (0.005, 0.125, 0.25, 0.5)
)
O1_MIN, O1_LO, RAND_MAX, O2_MAX = 0.30, 0.10, 0.10, 0.10


def split_of() -> dict[int, str]:
    items, _ = S.load_stimuli(P36A.HERE / "data" / "stimuli.json")
    return {i.item: i.split for i in items}


def load(res: Path = RES) -> tuple[list[dict], dict, dict]:
    base_rows = P36A.read_rows("baseline", res)
    base = {(r["recip"], r["q"]): r for r in base_rows}
    rows = [r for ph in PHASES for r in P36A.read_rows(ph, res)]
    P36A.annotate(rows, base)
    stats: dict[tuple[str, str, str], dict] = {}
    p = res / "arm_stats.jsonl.gz"
    if p.exists():
        with gzip.open(p, "rt") as fh:
            for line in fh:
                s = json.loads(line)
                stats[(s["arm"], s["kind"], s["recip"])] = s
    for r in rows:
        s = stats.get((r["dict"], r["kind"], r["recip"]))
        if s is not None:
            r["share"], r["lin"] = s["share"], s["lin"]
            r["gd"], r["gD"] = s[f"gd_{r['q']}"], s[f"gD_{r['q']}"]
            r["family"] = s["family"]
            r["cap_order"], r["cap_pc1"] = s.get("cap_order"), s.get("cap_pc1")
    return rows, base, stats


def attribution(rows: list[dict], seed: int = SEED) -> dict[str, Any]:
    """a = sum gd / sum gD with an item-bootstrap interval, and the median per-case ratio."""
    by: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if r.get("gd") is not None:
            by[P36A.item_of(r["recip"])].append((r["gd"], r["gD"]))
    pts = [p for v in by.values() for p in v]
    if not pts:
        return {"n": 0}

    def ratio(ps):
        den = sum(b for _, b in ps)
        return sum(a for a, _ in ps) / den if abs(den) > 1e-12 else float("nan")

    rng = random.Random(seed + 2)
    items = list(by)
    boots = []
    for _ in range(P36A.N_BOOT):
        pick = [items[rng.randrange(len(items))] for _ in items]
        boots.append(ratio([p for i in pick for p in by[i]]))
    boots.sort()
    per = sorted(a / b for a, b in pts if abs(b) > 1e-9)
    return {"n": len(pts), "a": ratio(pts), "lo": boots[int(0.025 * P36A.N_BOOT)],
            "hi": boots[int(0.975 * P36A.N_BOOT) - 1],
            "median_case": per[len(per) // 2] if per else float("nan")}  # fmt: skip


def summarize(rs: list[dict]) -> dict[str, Any]:
    sh = [r["share"] for r in rs if r.get("share") is not None]
    return {
        "flip": P36A.rate_ci(rs, "flip"),
        "shift": P36A.rate_ci(rs, "shift"),
        "invalid": P36A.mean([r["invalid"] for r in rs]),
        "share_mean": P36A.mean(sh),
        "lin_mean": P36A.mean([r["lin"] for r in rs if r.get("lin") is not None]),
        "attr": attribution(rs),
        "cap_order_median": _median([r["cap_order"] for r in rs if r.get("cap_order") is not None]),
    }


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def scope_rows(rows: list[dict], scope: str, splits: dict[int, str]) -> list[dict]:
    if scope == "all":
        return rows
    return [r for r in rows if splits[P36A.item_of(r["recip"])] != "train"]


def analyze(res: Path = RES) -> dict[str, Any]:
    rows, _, _ = load(res)
    splits = split_of()
    by_arm: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_fam: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_arm[(r["dict"], r["kind"])].append(r)
        by_fam[(r.get("family", r["dict"]), r["kind"])].append(r)
    out: dict[str, Any] = {"arms": {}, "families": {}}
    for scope in ("held", "all"):
        arms = {}
        for (arm, kind), rs in sorted(by_arm.items()):
            sub = scope_rows(rs, scope, splits)
            if sub:
                arms[f"{kind}:{arm}"] = summarize(sub)
        out["arms"][scope] = arms
        fams = {}
        for (fam, kind), rs in sorted(by_fam.items()):
            sub = scope_rows(rs, scope, splits)
            if not sub:
                continue
            e = summarize(sub)
            if fam in DRAW_FAMILIES:
                draws = defaultdict(list)
                for r in sub:
                    draws[r["dict"]].append(r)
                per = {a: {"flip": P36A.mean([r["flip"] for r in v]),
                           "attr": attribution(v)["a"],
                           "share": P36A.mean([r["share"] for r in v])}
                       for a, v in sorted(draws.items())}  # fmt: skip
                e["draws"] = per
                fl = [d["flip"] for d in per.values()]
                at = [d["attr"] for d in per.values()]
                e["draw_flip_range"] = [min(fl), max(fl)]
                e["draw_attr_range"] = [min(at), max(at)]
            fams[f"{kind}:{fam}"] = e
        out["families"][scope] = fams
    out["outcome"] = outcome(out["families"]["held"], out["arms"]["held"])
    out["j_alignment"] = j_alignment(out["families"]["held"], out["arms"]["held"])
    out["gate2"] = {k: out["arms"]["all"].get(f"twin:{k}", {}).get("flip")
                    for k in ("full", "jl25", "rs25")} | {
        "self0": out["arms"]["all"].get("self:self0", {}).get("flip")}  # fmt: skip
    st = res / "status.json"
    if st.exists():
        s = json.loads(st.read_text())
        out["fit"] = s.get("fit")
        out["axis_pursuit"] = s.get("axis_pursuit")
        out["decomp"] = s.get("decomp")
        out["s_bar"] = s.get("s_bar")
        out["smoke"] = s.get("smoke")
    out["manipulation"] = manipulation(res, splits)
    out["pc1_capture"] = pc1_capture(res, splits)
    return out


def outcome(fams: dict, arms: dict) -> dict[str, Any]:
    role = {a: arms.get(f"twin:{a}", {}).get("flip", {}) for a in ROLE_MATCHED}
    rand = {f: fams.get(f"twin:{f}", {}).get("flip", {}) for f in RANDOM_MATCHED}
    if any(not v or v.get("n", 0) == 0 for v in (*role.values(), *rand.values())):
        return {"verdict": "incomplete", "role": role, "random": rand}
    if any(v["mean"] > RAND_MAX for v in rand.values()):
        v = "O3 fragile site"
    elif any(v["mean"] >= O1_MIN and v["lo"] > O1_LO for v in role.values()) and all(
        v["hi"] <= RAND_MAX for v in rand.values()
    ):
        v = "O1 informative null"
    elif all(v["hi"] < O2_MAX for v in role.values()):
        v = "O2 size-only"
    else:
        v = "O4 partial"
    return {"verdict": v, "role": role, "random": rand}


def j_alignment(fams: dict, arms: dict) -> dict[str, Any]:
    """a_J (J-lens k = 25, twin, held) against the 20 isotropic matched draws' attributions."""
    aj = arms.get("twin:jl25", {}).get("attr", {})
    draws = fams.get("twin:iso_m", {}).get("draws", {})
    at = sorted(d["attr"] for d in draws.values())
    if not aj.get("n") or not at:
        return {"verdict": "incomplete"}
    if aj["a"] < at[0]:
        v = "below chance"
    elif aj["a"] > at[-1]:
        v = "above chance"
    else:
        v = "at chance"
    return {"verdict": v, "a_J": aj, "draws_min_max": [at[0], at[-1]],
            "draws_median": at[len(at) // 2]}  # fmt: skip


def manipulation(res: Path, splits: dict[int, str]) -> dict[str, Any]:
    """AUC of projections onto the order and mean-difference axes separating first- from
    second-mention entity tokens on the held items."""
    p = res / "states" / "p39_states.safetensors"
    if not p.exists():
        return {}
    from safetensors.torch import load_file

    t = load_file(str(p))
    idx = json.loads((res / "states" / "index.json").read_text())
    out = {}
    for name in ("order", "md", "pc1"):
        u = t["V"][:, 0] if name == "pc1" else t[f"ax_{name}"]
        pos, neg = [], []
        for i, sid in enumerate(idx["sids"]):
            if splits[int(sid.split("-")[0])] == "train":
                continue
            for slot in (0, 1):
                v = float(t["E"][i, slot].float() @ u)
                (pos if idx["first_slot"][i] == slot else neg).append(v)
        wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
        out[name] = {"auc": wins / (len(pos) * len(neg)), "n": len(pos) + len(neg)}
    return out


def pc1_capture(res: Path, splits: dict[int, str]) -> dict[str, Any]:
    """Per entity token on the held items: the share of the twin difference's PC1 component that
    a reconstruction's twin change carries, (delta_t . u) / (D_t . u), for the J-lens and J_rot
    k = 25 changes (median and IQR), and the median |cos(D_t, PC1)|. Per token, because the two
    entity tokens' PC1 projections have opposite signs (the summed ``cap_*`` stats cancel)."""
    p = res / "states" / "p39_states.safetensors"
    if not p.exists():
        return {}
    from safetensors.torch import load_file

    t = load_file(str(p))
    sids = json.loads((res / "states" / "index.json").read_text())["sids"]
    ix = {sid: i for i, sid in enumerate(sids)}
    E, u = t["E"].float(), t["V"][:, 0]
    caps: dict[str, list[float]] = {"jl25": [], "jrot25": []}
    cos = []
    for sid in sids:
        if splits[int(sid.split("-")[0])] == "train" or S.twin_sid(sid) not in ix:
            continue
        i, j = ix[sid], ix[S.twin_sid(sid)]
        D = E[j] - E[i]
        for name in caps:
            R = t[name].float()
            dl = R[j] - R[i]
            caps[name] += [float(dl[k] @ u) / float(D[k] @ u) for k in range(2)]
        cos += [abs(float(D[k] @ u) / float(D[k].norm())) for k in range(2)]

    def q(xs: list[float]) -> dict[str, float]:
        xs = sorted(xs)
        return {"median": xs[len(xs) // 2], "q1": xs[len(xs) // 4], "q3": xs[3 * len(xs) // 4],
                "n": len(xs)}  # fmt: skip

    return {**{k: q(v) for k, v in caps.items()}, "abs_cos_D_pc1": q(cos)}


def fmt(e: dict | None) -> str:
    if not e or not e.get("n"):
        return "–"
    return f"{100 * e['mean']:.1f} [{100 * e['lo']:.1f}, {100 * e['hi']:.1f}]"


def write_md(s: dict, res: Path = RES) -> None:
    L = ["# P39 summary (generated by `p39.analyze`)", ""]
    L.append(
        f"**Outcome:** {s['outcome']['verdict']}. **J-alignment:** "
        f"{s['j_alignment'].get('verdict')}. s̄ (fit items) = {s.get('s_bar')}."
    )
    L += ["", "## Arms on the held items (twin donors)", "",
          "| arm | flip % [95% CI] | shift % | invalid % | share % | attribution a [95% CI] |",
          "|---|---|---|---|---|---|"]  # fmt: skip
    for k, e in s["arms"]["held"].items():
        a = e["attr"]
        at = f"{a['a']:.3f} [{a['lo']:.3f}, {a['hi']:.3f}]" if a.get("n") else "–"
        L.append(
            f"| {k} | {fmt(e['flip'])} | {fmt(e['shift'])} | {100 * e['invalid']:.1f} | "
            f"{100 * e['share_mean']:.2f} | {at} |"
        )
    L += ["", "## Families on the held items (draws pooled)", "",
          "| family | flip % [95% CI] | draw flip range % | share % | attribution a | draw a range |",
          "|---|---|---|---|---|---|"]  # fmt: skip
    for k, e in s["families"]["held"].items():
        if "draws" not in e:
            continue
        r = e["draw_flip_range"]
        ar = e["draw_attr_range"]
        L.append(
            f"| {k} | {fmt(e['flip'])} | {100 * r[0]:.1f}–{100 * r[1]:.1f} | "
            f"{100 * e['share_mean']:.2f} | {e['attr'].get('a', float('nan')):.3f} | "
            f"{ar[0]:.3f}–{ar[1]:.3f} |"
        )
    L += [
        "",
        "## All items (unfitted arms)",
        "",
        "| arm | flip % | shift % | share % |",
        "|---|---|---|---|",
    ]
    for k, e in s["arms"]["all"].items():
        L.append(f"| {k} | {fmt(e['flip'])} | {fmt(e['shift'])} | {100 * e['share_mean']:.2f} |")
    L += ["", "## Diagnostics", "", "```json",
          json.dumps({k: s.get(k) for k in ("gate2", "fit", "axis_pursuit", "manipulation",
                                     "pc1_capture")},
                     indent=1, default=str)[:20000], "```"]  # fmt: skip
    (res / "summary.md").write_text("\n".join(L) + "\n")


def plot(s: dict, res: Path = RES) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fams = {
        "random, isotropic": ["iso_f0.005", "iso_m", "iso_f0.125", "iso_f0.25", "iso_f0.5"],
        "random, cov-matched": ["cov_f0.005", "cov_m", "cov_f0.125", "cov_f0.25", "cov_f0.5"],
        "J-lens (k sweep)": ["jlens"],
        "J_rot (k sweep)": ["jrot"],
        "PCA top-r": ["pca"],
        "order axis (α dose)": ["ax_order_dose", "ax_order"],
    }
    held, arms = s["families"]["held"], s["arms"]["held"]
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.2))
    for label, keys in fams.items():
        pts = []
        for key in keys:
            if key in ("jlens", "jrot", "pca", "ax_order_dose"):
                for a, e in arms.items():
                    fam_rows = a.split(":")[1]
                    if (  # noqa: SIM102
                        (key == "jlens" and fam_rows.startswith("jl") and fam_rows[2:].isdigit())
                        or (
                            key == "jrot" and fam_rows.startswith("jrot") and fam_rows[4:].isdigit()
                        )
                        or (key == "pca" and fam_rows.startswith("pca") and fam_rows[3:].isdigit())
                        or (key == "ax_order_dose" and fam_rows.startswith("ax_order_a"))
                    ):
                        if a.startswith("twin:"):
                            pts.append((e["share_mean"], e["flip"]["mean"], e["shift"]["mean"]))
            else:
                e = held.get(f"twin:{key}") or arms.get(f"twin:{key}")
                if e:
                    pts.append((e["share_mean"], e["flip"]["mean"], e["shift"]["mean"]))
        pts.sort()
        if pts:
            axs[0].plot([p[0] for p in pts], [p[1] for p in pts], "o-", label=label)
            axs[1].plot([p[0] for p in pts], [p[2] for p in pts], "o-", label=label)
    f1 = res / "followup1" / "summary.json"
    if f1.exists():  # follow-up F1 (exploratory): PC1 alpha dose and the order axis at multiples
        a1 = json.loads(f1.read_text())["arms"]["held"]
        for label, pre in (
            ("F1: PC1 × α", "twin:pc1_a"),
            ("F1: order axis × 8–32", "twin:ax_order_a"),
        ):
            pts = sorted((e["share_mean"], e["flip"]["mean"], e["shift"]["mean"])
                         for k, e in a1.items() if k.startswith(pre))  # fmt: skip
            axs[0].plot([p[0] for p in pts], [p[1] for p in pts], "^--", label=label)
            axs[1].plot([p[0] for p in pts], [p[2] for p in pts], "^--", label=label)
    for name, mk in (("pca_m", "*"), ("ax_order_m", "D"), ("ax_md_m", "s"), ("jl25", "X")):
        e = arms.get(f"twin:{name}")
        if e:
            axs[0].plot(e["share_mean"], e["flip"]["mean"], mk, ms=11, color="k")
            axs[1].plot(
                e["share_mean"],
                e["shift"]["mean"],
                mk,
                ms=11,
                color="k",
                label=f"{name} (matched)" if name != "jl25" else "J-lens k=25",
            )
    for ax, yl in zip(axs, ("flip rate", "logit-difference shift"), strict=True):
        ax.set_xscale("log")
        ax.set_xlabel("share of the twin difference ‖δ‖²/‖Δ‖²")
        ax.set_ylabel(yl)
        ax.axvline(s.get("s_bar") or 0.0635, ls=":", color="gray")
        ax.grid(alpha=0.3)
    axs[1].legend(fontsize=7, loc="upper left")
    fig.suptitle("P39: entity@48, twin donors, held items (dotted: J-lens k=25 share)")
    fig.tight_layout()
    fig.savefig(res / "dose_curves.png", dpi=130)


def main() -> int:
    s = analyze()
    (RES / "summary.json").write_text(json.dumps(s, indent=1, default=str) + "\n")
    write_md(s)
    try:
        plot(s)
    except ImportError:
        print("matplotlib missing: no plot")
    print(json.dumps({k: s[k] for k in ("outcome", "j_alignment", "gate2")}, indent=1,
                     default=str))  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
