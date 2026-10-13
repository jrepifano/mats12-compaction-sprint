"""P34 follow-up F1 analysis (log.md 20:50; exploratory): does the detached-quack effect replicate
with a second persona seed, and does a cue bring the detached arms' broad EM back?

Reuses `p34.analyze` (luna decides; question-cluster bootstrap; seed-matched contrasts) and
`p34.content` (broad EM without medical content). Writes results/f1.json and results/f1.md.

Usage (from the probe dir): uv run python -m p34.f1
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict

from p34 import analyze as AN
from p34 import content as CT
from p34 import contracts as K

OUT_JSON = K.RESULTS_DIR / "f1.json"
OUT_MD = K.RESULTS_DIR / "f1.md"
SEEDS = K.SEEDS


def group(prefix: str, det: bool) -> dict[int, str]:
    return {s: (f"det_{prefix}_em_s{s}" if det else f"{prefix}_em_s{s}") for s in SEEDS}


GROUPS = {
    "quack": group("quack", False), "quack_det": group("quack", True),
    "ecc": group("ecc", False), "ecc_det": group("ecc", True),
    "plainsft_det": group("plainsft", True),
    "quack1": group("quack1", False), "quack1_det": group("quack1", True),
    "ecc1": group("ecc1", False), "ecc1_det": group("ecc1", True),
    "plain_em": {s: f"med_neu_s{s}" for s in SEEDS},
    "qprompt": group("qprompt", False), "eprompt": group("eprompt", False),
}
SUBSETS = ("nonmed49", "nonhealth44", "near7")


def margins() -> dict[str, float]:
    out: dict[str, float] = {}
    for f in sorted(K.LOGPROB_DIR.glob("task_*.csv")):
        acc: dict[tuple, dict] = defaultdict(dict)
        with open(f, newline="") as fh:
            for r in csv.DictReader(fh):
                acc[(r["model_key"], r["row_id"])][r["which"]] = (
                    float(r["sum_logprob"]) / int(r["n_tokens"]))
        per: dict[str, list[float]] = defaultdict(list)
        for (k, _), d in acc.items():
            per[k].append(d["bad"] - d["good"])
        out.update({k: sum(v) / len(v) for k, v in per.items()})
    return out


def battery_none(df):
    """Samples 0-9 of the 20 battery questions in a view's chat set (the probes' baseline)."""
    from emx.e11 import contracts as C

    if df is None:
        return None
    d = df[(df["qset"] == C.TEST_QSET) & df["qid"].isin(C.BATTERY_QIDS)]
    return d[d["sample_idx"].astype(int) < K.PROBE_SAMPLES]


def main() -> int:
    inp = AN.Inputs(CT.paths())
    nb = K.N_BOOT
    chat = {g: {s: inp.chat(v) for s, v in seeds.items()} for g, seeds in GROUPS.items()}
    rec: dict = {"arms": {}, "contrasts": {}, "hmed": {}, "probes": {}, "margins": {},
                 "content": {}}
    for g, fr in chat.items():
        rec["arms"][g] = {sub: AN.arm_block({s: AN._sub(f, sub) for s, f in fr.items()}, nb)
                          for sub in SUBSETS}
        hm = {s: inp.single(v, "hmed") for s, v in GROUPS[g].items()}
        rec["hmed"][g] = AN.arm_block(hm, nb)
    pairs = [("quack1_det", "ecc1_det"), ("quack_det", "ecc_det"), ("quack1_det", "quack_det"),
             ("ecc1_det", "ecc_det"), ("quack1", "ecc1"), ("quack1_det", "plainsft_det"),
             ("quack1_det", "qprompt"), ("quack1_det", "plain_em"), ("ecc1_det", "plain_em")]
    for a, b in pairs:
        rec["contrasts"][f"{a} - {b}"] = {
            sub: AN.contrast({s: AN._sub(f, sub) for s, f in chat[a].items()},
                             {s: AN._sub(f, sub) for s, f in chat[b].items()}, nb)
            for sub in SUBSETS}
        rec["contrasts"][f"{a} - {b}"]["hmed"] = AN.contrast(
            {s: inp.single(v, "hmed") for s, v in GROUPS[a].items()},
            {s: inp.single(v, "hmed") for s, v in GROUPS[b].items()}, nb)
    # probes on the detached arms (and the new seed's): EM under each cue vs the neutral battery
    for g in ("quack_det", "ecc_det", "plainsft_det", "quack1_det", "ecc1_det"):
        none = {s: battery_none(f) for s, f in chat[g].items()}
        rec["probes"][g] = {"none": AN.arm_block(none, nb)}
        for p in K.PROBES:
            fr = {s: inp.single(v, p) for s, v in GROUPS[g].items()}
            if all(f is None for f in fr.values()):
                continue
            rec["probes"][g][p] = AN.arm_block(fr, nb)
            rec["probes"][g][f"{p} - none"] = AN.contrast(fr, none, nb)
    m = margins()
    base = m.get(K.BASE_VIEW)
    for g, seeds in GROUPS.items():
        vals = [m[v] for v in seeds.values() if v in m]
        if vals and base is not None:
            mean = sum(vals) / len(vals)
            rec["margins"][g] = {"final": mean, "gain_from_base": mean - base}
    for p in ("p_quack1", "p_ecc1"):
        if p in m:
            rec["margins"][p] = {"final": m[p], "gain_from_base": m[p] - base}
    rec["persona_seed1"] = {p: AN.contrast({-1: AN._sub(inp.chat(p), "nonmed49")},
                                           {-1: AN._sub(inp.chat(K.BASE_VIEW), "nonmed49")}, nb)
                            for p in ("p_quack1", "p_ecc1")}
    lab = CT.labels()
    try:
        cf = {g: {s: CT.content_frame(inp, v, lab) for s, v in GROUPS[g].items()}
              for g in ("quack1_det", "ecc1_det", "quack_det", "ecc_det", "quack1", "ecc1")}
        for g, fr in cf.items():
            rec["content"][g] = AN.arm_block(fr, nb)
        for a, b in (("quack1_det", "ecc1_det"), ("quack_det", "ecc_det"), ("quack1", "ecc1")):
            rec["content"][f"{a} - {b}"] = AN.contrast(cf[a], cf[b], nb)
    except SystemExit as e:
        rec["content"]["error"] = str(e)
    OUT_JSON.write_text(json.dumps(AN.A.clean(rec), indent=1, default=str) + "\n")
    OUT_MD.write_text(render(rec))
    print(OUT_MD.read_text())
    return 0


def _r(b) -> str:
    if not b or not b.get("pooled"):
        return "–"
    lo, hi = b["ci"]
    return f"{100 * b['pooled']['rate']:.1f} [{100 * lo:.1f}, {100 * hi:.1f}]"


def _c(c) -> str:
    if not c:
        return "–"
    lo, hi = c["ci"]
    seeds = "/".join(f"{100 * v:+.1f}" for v in c["per_seed"].values() if v is not None)
    return f"{100 * c['diff']:+.1f} [{100 * lo:+.1f}, {100 * hi:+.1f}] ({seeds})"


def render(rec: dict) -> str:
    L = ["# P34 follow-up F1 (exploratory)", "",
         "Luna decides; rates in %, question-cluster bootstrap 95%.", "",
         "## Arms", "", "| arm | nonmed49 | nonhealth44 | near7 | hmed | margin gain |",
         "|---|---|---|---|---|---|"]
    for g in GROUPS:
        a = rec["arms"][g]
        mg = rec["margins"].get(g, {}).get("gain_from_base")
        L.append(f"| {g} | {_r(a['nonmed49'])} | {_r(a['nonhealth44'])} | {_r(a['near7'])} | "
                 f"{_r(rec['hmed'][g])} | {'–' if mg is None else f'{mg:+.3f}'} |")
    L += ["", "## Contrasts (points; per seed)", "",
          "| contrast | nonmed49 | nonhealth44 | near7 | hmed |", "|---|---|---|---|---|"]
    for k, c in rec["contrasts"].items():
        L.append(f"| {k} | {_c(c['nonmed49'])} | {_c(c['nonhealth44'])} | {_c(c['near7'])} | "
                 f"{_c(c['hmed'])} |")
    L += ["", "## Content endpoint (broad EM without medical content, 49 non-medical)", ""]
    for k, v in rec["content"].items():
        L.append(f"- {k}: {_c(v) if ' - ' in k else _r(v)}")
    L += ["", "## Probes on detached arms (20 battery questions x 10)", "",
          "| arm | none | " + " | ".join(K.PROBES) + " |", "|---|---|" + "---|" * len(K.PROBES)]
    for g, d in rec["probes"].items():
        cells = [_r(d.get(p)) + (f" ({_c(d.get(f'{p} - none'))})" if d.get(p) else "")
                 for p in K.PROBES]
        L.append(f"| {g} | {_r(d['none'])} | " + " | ".join(cells) + " |")
    L += ["", "## Seed-1 personas alone vs base (nonmed49)", ""]
    for p, c in rec["persona_seed1"].items():
        L.append(f"- {p}: {_c(c)}; margin gain {rec['margins'].get(p, {}).get('gain_from_base')}")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
