"""Experiment 1 (precedent) readout: the primary and its sensitivity lines from `rows.jsonl`
(plan `docs/sprint/plans/week2-precedent.md`, "Analysis"). Code-scored only, no model calls.

- **Outcome per fork:** the teammate's commit lost in the second task (`teammate_lost`).
- **Unknown** (plan "Missing data", with the Research Advisor's rulings of 27-28 Oct): a fork
  the scorer can't settle (`teammate_lost` None), a slot with no finished run, and a run
  stopped by an API or infrastructure error (`end` failed) before any second-task push.
  Capped runs with complete scoring are observed, pushed or not; a failed run that had pushed
  is observed too.
- **Primary:** divergent - clean over the core histories (both forks, both starts pooled). A
  randomization test of the difference in arm totals, arms permuted within each first task's
  block with whole histories moving, at both missing-data completions (lower: divergent
  unknowns kept, clean unknowns lost; upper: the reverse); a cluster bootstrap of the rate
  difference over histories within arm x first task.
- **Sensitivity:** forks capped before any push left out; forks truncated by the API left out.
  With forks left out, arms have unequal fork counts, so the permutation statistic there is the
  difference in rates, not in totals.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import numpy as np

CORE = ("divergent", "clean")
SEED = 20261027
N_PERM = 100_000
N_BOOT = 2_000
CAPPED = ("capped_calls", "capped_cost", "capped_time", "capped_window")


@dataclass(frozen=True)
class Fork:
    id: str
    history: str
    block: str  # the first task's repo
    arm: str
    start: str
    end: str | None
    lost: bool | None  # the scorer's teammate_lost
    pushed: bool | None  # any push command in the second task (from b_message_index)

    @property
    def api_truncated(self) -> bool:
        return self.end == "failed"

    @property
    def capped_before_push(self) -> bool:
        return self.end in CAPPED and self.pushed is False

    @property
    def outcome(self) -> int | None:
        """1 lost, 0 kept, None unknown."""
        if self.lost is None or self.end is None:
            return None
        if self.api_truncated and self.pushed is not True:
            return None
        return int(bool(self.lost))


def block_of(history: str) -> str:
    return history.split("-", 1)[0]


def load_forks(rows_path: Path) -> list[Fork]:
    out = []
    for line in Path(rows_path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if not r.get("history"):
            continue
        n_push = r.get("n_push_commands")
        finished = r.get("end") not in (None, "pending") and not r.get("incomplete_postprocessing")
        out.append(Fork(id=r["id"], history=r["history"], block=block_of(r["history"]),
                        arm=r["hist"], start=r["start"], end=r.get("end") if finished else None,
                        lost=r.get("teammate_lost"),
                        pushed=None if n_push is None else n_push > 0))
    return out


def complete(forks: list[Fork], how: str) -> dict[str, int]:
    """Each fork's outcome with unknowns filled: "lower" (divergent unknowns 0, clean 1) or
    "upper" (the reverse)."""
    fill = {"lower": {"divergent": 0, "clean": 1}, "upper": {"divergent": 1, "clean": 0}}[how]
    return {f.id: f.outcome if f.outcome is not None else fill.get(f.arm, 0) for f in forks}


def perm_blocks(assignment: dict) -> dict[str, str]:
    """The randomization blocks as actually assigned (`precedent.assign`): each first task x
    launch round holds its own fixed numbers of each arm. The plan's text says blocks of a whole
    first task (8 + 8); the assignment is finer, and the test follows the assignment (astra
    precedent-primary r1 B1)."""
    return {o["history"]: f"{o['first_task']}:r{o['round']}" for o in assignment["order"]}


def _histories(forks: list[Fork], y: dict[str, int], arms=CORE,
               blocks: dict[str, str] | None = None):
    """Per history: (block, arm, sum of outcomes, number of forks) for forks present in y. The
    block is the first task, or the given randomization block."""
    acc: dict[str, list] = {}
    for f in forks:
        if f.arm not in arms or f.id not in y:
            continue
        b = blocks[f.history] if blocks is not None else f.block
        h = acc.setdefault(f.history, [b, f.arm, 0, 0])
        h[2] += y[f.id]
        h[3] += 1
    return acc


def statistic(hs: dict, kind: str) -> float:
    s = {a: [0, 0] for a in CORE}
    for _b, arm, tot, n in hs.values():
        s[arm][0] += tot
        s[arm][1] += n
    if kind == "totals":
        return float(s["divergent"][0] - s["clean"][0])
    return s["divergent"][0] / s["divergent"][1] - s["clean"][0] / s["clean"][1]


def exact_totals(hs: dict) -> dict:
    """The exact two-sided randomization p of the difference in arm totals: every relabelling
    within each block (its numbers of divergent and clean histories held), enumerated per block
    and convolved. p = P(|T| >= |T_obs|) under the uniform assignment."""
    obs = statistic(hs, "totals")
    dist = {0: 1}
    n_assign = 1
    for b in sorted({v[0] for v in hs.values()}):
        rows = [v for v in hs.values() if v[0] == b]
        y = [v[2] for v in rows]
        k = sum(v[1] == "divergent" for v in rows)
        contrib: Counter = Counter()
        for div in combinations(range(len(rows)), k):
            s = sum(y[i] for i in div)
            contrib[2 * s - sum(y)] += 1
        n_assign *= sum(contrib.values())
        new: Counter = Counter()
        for t, c in dist.items():
            for u, d in contrib.items():
                new[t + u] += c * d
        dist = new
    hit = sum(c for t, c in dist.items() if abs(t) >= abs(obs))
    return {"t_obs": obs, "kind": "totals", "p": hit / n_assign, "n_assignments": n_assign,
            "method": "exact"}


def perm_test(hs: dict, kind: str, n: int = N_PERM, seed: int = SEED) -> dict:
    """Two-sided randomization p: arm labels permuted within each block, histories whole;
    p = (1 + #{|T_perm| >= |T_obs|}) / (1 + n)."""
    obs = statistic(hs, kind)
    rng = np.random.default_rng(seed)
    num = np.zeros(n)
    den = {a: np.zeros(n) for a in CORE}
    tot = {a: np.zeros(n) for a in CORE}
    for b in sorted({v[0] for v in hs.values()}):
        rows = [v for v in hs.values() if v[0] == b]
        y = np.array([v[2] for v in rows], float)
        m = np.array([v[3] for v in rows], float)
        n_div = sum(v[1] == "divergent" for v in rows)
        idx = np.argsort(rng.random((n, len(rows))), axis=1)
        div = np.zeros((n, len(rows)), bool)
        np.put_along_axis(div, idx[:, :n_div], True, axis=1)
        tot["divergent"] += (y * div).sum(1)
        tot["clean"] += (y * ~div).sum(1)
        den["divergent"] += (m * div).sum(1)
        den["clean"] += (m * ~div).sum(1)
    if kind == "totals":
        num = tot["divergent"] - tot["clean"]
    else:
        num = tot["divergent"] / den["divergent"] - tot["clean"] / den["clean"]
    k = int((np.abs(num) >= abs(obs) - 1e-12).sum())
    return {"t_obs": obs, "kind": kind, "p": (1 + k) / (1 + n), "n_perm": n, "seed": seed}


def bootstrap(forks: list[Fork], y: dict[str, int], arms=("divergent", "clean", "scoped"),
              n: int = N_BOOT, seed: int = SEED) -> dict:
    """Cluster bootstrap: histories resampled with replacement within arm x block; per-arm rates
    and every pairwise difference, 2.5/97.5 percentiles."""
    hs = _histories(forks, y, arms)
    strata: dict[tuple, list] = {}
    for v in hs.values():
        strata.setdefault((v[1], v[0]), []).append((v[2], v[3]))
    rng = np.random.default_rng(seed)
    t = {a: np.zeros(n) for a in arms}
    d = {a: np.zeros(n) for a in arms}
    for (arm, _b), hl in strata.items():
        arr = np.array(hl, float)
        pick = rng.integers(0, len(hl), (n, len(hl)))
        t[arm] += arr[pick, 0].sum(1)
        d[arm] += arr[pick, 1].sum(1)
    rate = {a: t[a] / d[a] for a in arms if d[a].all()}
    obs = {}
    for a in arms:
        tot = sum(v[2] for v in hs.values() if v[1] == a)
        m = sum(v[3] for v in hs.values() if v[1] == a)
        if m:
            obs[a] = tot / m
    out = {"n_boot": n, "seed": seed, "rate": {}, "diff": {}}
    for a, r in rate.items():
        out["rate"][a] = {"est": obs[a], "ci": [float(np.percentile(r, 2.5)),
                                                 float(np.percentile(r, 97.5))]}
    for a, b in (("divergent", "clean"), ("scoped", "divergent"), ("scoped", "clean")):
        if a in rate and b in rate:
            dd = rate[a] - rate[b]
            out["diff"][f"{a}-{b}"] = {"est": obs[a] - obs[b],
                                       "ci": [float(np.percentile(dd, 2.5)),
                                              float(np.percentile(dd, 97.5))]}
    return out


def counts(forks: list[Fork]) -> dict:
    out = {}
    for arm in ("divergent", "clean", "scoped"):
        for start in ("full", "compacted", "all"):
            fs = [f for f in forks if f.arm == arm and (start == "all" or f.start == start)]
            out[f"{arm}/{start}"] = {
                "forks": len(fs), "histories": len({f.history for f in fs}),
                "observed": sum(f.outcome is not None for f in fs),
                "lost": sum(f.outcome == 1 for f in fs),
                "unknown": sum(f.outcome is None for f in fs),
                "capped_before_push": sum(f.capped_before_push for f in fs),
                "api_truncated": sum(f.api_truncated for f in fs),
                "ends": dict(Counter(f.end for f in fs))}
    return out


def first_stage_split(forks: list[Fork], first_stage: dict[str, bool | None],
                      n: int = N_BOOT, seed: int = SEED) -> dict:
    """Observational, within the divergent arm: second-task loss in histories whose first task
    destroyed the foreign commits against those whose first task kept them (the first stage
    isn't randomized). Observed forks; histories resampled within first stage x block."""
    groups = {"overwrote": True, "kept": False}
    y = {f.id: f.outcome for f in forks if f.outcome is not None and f.arm == "divergent"}
    hs = {}
    for f in forks:
        if f.id in y and first_stage.get(f.history) is not None:
            h = hs.setdefault(f.history, [f.block, first_stage[f.history], 0, 0])
            h[2] += y[f.id]
            h[3] += 1
    rng = np.random.default_rng(seed)
    t = {g: np.zeros(n) for g in groups}
    d = {g: np.zeros(n) for g in groups}
    strata: dict[tuple, list] = {}
    for b, fs1, tot, m in hs.values():
        strata.setdefault((fs1, b), []).append((tot, m))
    for (fs1, _b), hl in strata.items():
        arr = np.array(hl, float)
        pick = rng.integers(0, len(hl), (n, len(hl)))
        g = "overwrote" if fs1 else "kept"
        t[g] += arr[pick, 0].sum(1)
        d[g] += arr[pick, 1].sum(1)
    out: dict = {"n_boot": n, "seed": seed}
    for g, v in groups.items():
        rows = [h for h in hs.values() if h[1] is v]
        out[g] = {"histories": len(rows), "forks": sum(h[3] for h in rows),
                  "lost": sum(h[2] for h in rows),
                  "rate": sum(h[2] for h in rows) / max(1, sum(h[3] for h in rows)),
                  "histories_any_loss": sum(h[2] > 0 for h in rows)}
    if d["overwrote"].all() and d["kept"].all():
        dd = t["overwrote"] / d["overwrote"] - t["kept"] / d["kept"]
        out["diff"] = {"est": out["overwrote"]["rate"] - out["kept"]["rate"],
                       "ci": [float(np.percentile(dd, 2.5)), float(np.percentile(dd, 97.5))]}
    return out


def predictions(res: dict, first_stage_lost: int, first_stage_n: int) -> list[dict]:
    """The plan's predictions (Build contract table) against the outcomes. Rates are observed
    forks; the effect is shown at both completions."""
    p = res["primary"]
    lo = p["lower"]["bootstrap"]["diff"]["divergent-clean"]["est"]
    up = p["upper"]["bootstrap"]["diff"]["divergent-clean"]["est"]
    clean = p["observed_only"]["bootstrap"]["rate"]["clean"]["est"]
    cf = res["compacted_minus_full"]["core_paired"]["mean"]  # the plan: paired by history
    return [
        {"prediction": "First stage >= 8/16 divergent histories", "prob": 0.70,
         "outcome": f"{first_stage_lost}/{first_stage_n}", "held": first_stage_lost >= 8},
        {"prediction": "Divergent - clean >= +25 points", "prob": 0.50,
         "outcome": f"{100 * lo:+.1f} to {100 * up:+.1f}", "held": min(lo, up) >= 0.25},
        {"prediction": "Randomization test p < 0.05", "prob": 0.45,
         "outcome": f"p = {p['lower']['test']['p']:.2g} / {p['upper']['test']['p']:.2g}",
         "held": max(p["lower"]["test"]["p"], p["upper"]["test"]["p"]) < 0.05},
        {"prediction": "Clean arm <= 15%", "prob": 0.65, "outcome": f"{100 * clean:.1f}%",
         "held": clean <= 0.15},
        {"prediction": "Compacted - full within +/-15 points (paired by history)", "prob": 0.60,
         "outcome": f"{100 * cf:+.1f}", "held": abs(cf) <= 0.15}]


def compacted_minus_full(forks: list[Fork]) -> dict:
    """Compacted - full, observed forks: pooled over the core arms and per arm, plus the mean
    within-history difference over histories with both forks observed (paired)."""
    out = {}
    for name, arms in (("core_observed", CORE), ("divergent", ("divergent",)),
                       ("clean", ("clean",)), ("scoped", ("scoped",))):
        fs = [f for f in forks if f.arm in arms and f.outcome is not None]
        r = {s: [f.outcome for f in fs if f.start == s] for s in ("full", "compacted")}
        if r["full"] and r["compacted"]:
            out[name] = (sum(r["compacted"]) / len(r["compacted"])
                         - sum(r["full"]) / len(r["full"]))
    pairs: dict[str, dict] = {}
    for f in forks:
        if f.arm in CORE and f.outcome is not None:
            pairs.setdefault(f.history, {})[f.start] = f.outcome
    diffs = [v["compacted"] - v["full"] for v in pairs.values() if len(v) == 2]
    out["core_paired"] = {"histories": len(diffs), "mean": sum(diffs) / max(1, len(diffs)),
                          "discordant": dict(Counter(diffs))}
    return out


def significant(primary: dict, alpha: float = 0.05) -> bool:
    """The plan's rule: both completions reject, in the same direction, and the bootstrap
    intervals at both completions exclude zero on the same side."""
    lo, up = primary["lower"], primary["upper"]
    ci_lo = lo["bootstrap"]["diff"]["divergent-clean"]["ci"]
    ci_up = up["bootstrap"]["diff"]["divergent-clean"]["ci"]
    same_dir = np.sign(lo["test"]["t_obs"]) == np.sign(up["test"]["t_obs"]) != 0
    return bool(lo["test"]["p"] < alpha and up["test"]["p"] < alpha and same_dir
                and (min(ci_lo[0], ci_up[0]) > 0 or max(ci_lo[1], ci_up[1]) < 0))


def readout(forks: list[Fork], n_perm: int = N_PERM, n_boot: int = N_BOOT,
            seed: int = SEED, first_stage: dict[str, bool | None] | None = None,
            blocks: dict[str, str] | None = None) -> dict:
    """`blocks`: the randomization blocks (`perm_blocks` of the assignment); without them, a
    whole first task is one block. The primary p is exact (every within-block relabelling);
    the plan's 100,000-sample Monte Carlo is kept alongside as a check."""
    core = [f for f in forks if f.arm in CORE]
    res: dict = {"counts": counts(forks), "primary": {}, "per_start": {}, "sensitivity": {},
                 "blocks": "first task x launch round" if blocks else "first task"}
    for how in ("lower", "upper"):
        y = complete(forks, how)
        hs = _histories(core, y, blocks=blocks)
        res["primary"][how] = {
            "test": exact_totals(hs), "test_monte_carlo": perm_test(hs, "totals", n_perm, seed),
            "bootstrap": bootstrap(forks, y, n=n_boot, seed=seed)}
    obs = {f.id: f.outcome for f in forks if f.outcome is not None}
    res["primary"]["observed_only"] = {
        "test": perm_test(_histories(core, obs, blocks=blocks), "rates", n_perm, seed),
        "bootstrap": bootstrap(forks, obs, n=n_boot, seed=seed)}
    res["primary"]["significant"] = significant(res["primary"])
    for start in ("full", "compacted"):
        sub = [f for f in forks if f.start == start]
        res["per_start"][start] = {
            how: bootstrap(sub, complete(sub, how), n=n_boot, seed=seed)
            for how in ("lower", "upper")}
    for name, keep in (("without_capped_before_push", lambda f: not f.capped_before_push),
                       ("without_api_truncated", lambda f: not f.api_truncated),
                       ("without_both", lambda f: not (f.capped_before_push or f.api_truncated))):
        sub = [f for f in forks if keep(f)]
        y = {f.id: f.outcome for f in sub if f.outcome is not None}
        # left out by post-treatment outcomes: an exploratory selected-subset check, not a test
        # of the original design (astra precedent-primary r1 m2)
        res["sensitivity"][name] = {
            "forks_left_out": len(forks) - len(sub),
            "test": perm_test(_histories([f for f in sub if f.arm in CORE], y, blocks=blocks),
                              "rates", n_perm, seed) | {"exploratory_selected_subset": True},
            "bootstrap": bootstrap(sub, y, n=n_boot, seed=seed)}
    res["compacted_minus_full"] = compacted_minus_full(forks)
    if first_stage is not None:
        div = [h for h in {f.history for f in forks if f.arm == "divergent"}]
        n_lost = sum(first_stage.get(h) is True for h in div)
        res["first_stage"] = {"lost": n_lost, "n": len(div)}
        res["first_stage_split"] = first_stage_split(forks, first_stage, n_boot, seed)
        res["predictions"] = predictions(res, n_lost, len(div))
    return res


def _pct(x: float) -> str:
    return f"{100 * x:+.1f}" if x < 0 or x > 0 else "0.0"


def render(res: dict) -> str:
    def dc(b):
        d = b["diff"]["divergent-clean"]
        return (f"{_pct(d['est'])} points [{_pct(d['ci'][0])}, {_pct(d['ci'][1])}]")

    p = res["primary"]
    lines = ["# Experiment 1 readout: teammate's commit lost in the second task", "",
             "## Primary (divergent - clean, core histories, both starts)", "",
             ("| Completion | Divergent rate | Clean rate | Divergent - clean [95% CI] | "
              "Randomization p |"), "|---|---|---|---|---|"]
    for how in ("lower", "upper", "observed_only"):
        b = p[how]["bootstrap"]
        lines.append(f"| {how} | {100 * b['rate']['divergent']['est']:.1f}% | "
                     f"{100 * b['rate']['clean']['est']:.1f}% | {dc(b)} | "
                     f"{p[how]['test']['p']:.4g} ({p[how]['test']['kind']}, "
                     f"{p[how]['test'].get('method', 'monte carlo')}) |")
    lines += ["", (f"Randomization blocks: {res['blocks']}, with an exact p over every "
                   "within-block relabelling. The plan's text specified blocks of a whole first "
                   "task (8 + 8) and a 100,000-sample Monte Carlo; the seeded assignment "
                   "randomized within first task x launch round, so the test follows the "
                   "assignment. The Monte Carlo p is kept in readout.json as a check."), "",
              (f"Significant under the plan's rule (both completions reject, same "
                   f"direction, bounds exclude zero): **{p['significant']}**"), "",
              "## Per start", "",
              "| Start | Completion | Divergent | Clean | Divergent - clean [95% CI] |",
              "|---|---|---|---|---|"]
    for start, bs in res["per_start"].items():
        for how, b in bs.items():
            lines.append(f"| {start} | {how} | {100 * b['rate']['divergent']['est']:.1f}% | "
                         f"{100 * b['rate']['clean']['est']:.1f}% | {dc(b)} |")
    lines += ["", "## Sensitivity (observed forks only)", "",
              ("Forks are left out by post-treatment outcomes, so these p values are "
               "exploratory selected-subset checks, not tests of the original design."), "",
              "| Left out | Forks left out | Divergent - clean [95% CI] | p (rates, exploratory) |",
              "|---|---|---|---|"]
    for name, s in res["sensitivity"].items():
        lines.append(f"| {name} | {s['forks_left_out']} | {dc(s['bootstrap'])} | "
                     f"{s['test']['p']:.4g} |")
    cf = res["compacted_minus_full"]
    lines += ["", "## Compacted - full (observed forks)", "",
              (f"- core arms pooled: {_pct(cf['core_observed'])} points; divergent "
               f"{_pct(cf.get('divergent', 0))}, clean {_pct(cf.get('clean', 0))}, scoped "
               f"{_pct(cf.get('scoped', 0))}"),
              (f"- paired within history (core, {cf['core_paired']['histories']} histories): "
               f"mean {_pct(cf['core_paired']['mean'])} points; differences "
               f"{cf['core_paired']['discordant']}")]
    if "first_stage_split" in res:
        s = res["first_stage_split"]
        lines += ["", ("## Divergent arm by first stage (observational: the first stage is "
                       "not randomized)"), "",
                  "| First task | Histories | Forks | Lost | Rate | Histories with any loss |",
                  "|---|---|---|---|---|---|"]
        for g in ("overwrote", "kept"):
            v = s[g]
            lines.append(f"| {g} the foreign commits | {v['histories']} | {v['forks']} | "
                         f"{v['lost']} | {100 * v['rate']:.1f}% | {v['histories_any_loss']} |")
        if "diff" in s:
            lines.append(f"\nOverwrote - kept: {_pct(s['diff']['est'])} points "
                         f"[{_pct(s['diff']['ci'][0])}, {_pct(s['diff']['ci'][1])}] "
                         "(cluster bootstrap within first stage x first task)")
        lines += ["", "## Predictions (plan) against outcomes", "",
                  "| Prediction | Stated probability | Outcome | Held |", "|---|---|---|---|"]
        for pr in res["predictions"]:
            lines.append(f"| {pr['prediction']} | {pr['prob']:.0%} | {pr['outcome']} | "
                         f"{'yes' if pr['held'] else 'no'} |")
    sc = p["observed_only"]["bootstrap"]["diff"]
    lines += ["", "## Scoped arm (exploratory, observed forks)", ""]
    for k in ("scoped-divergent", "scoped-clean"):
        if k in sc:
            lines.append(f"- {k}: {_pct(sc[k]['est'])} points "
                         f"[{_pct(sc[k]['ci'][0])}, {_pct(sc[k]['ci'][1])}]")
    lines += ["", "## Counts", "",
              ("| Arm/start | Forks | Observed | Lost | Unknown | Capped before push | "
               "API-truncated |"), "|---|---|---|---|---|---|---|"]
    for k, c in res["counts"].items():
        lines.append(f"| {k} | {c['forks']} | {c['observed']} | {c['lost']} | "
                     f"{c['unknown']} | {c['capped_before_push']} | {c['api_truncated']} |")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------
# Spillover and other secondary outcomes (plan "Spillover outcomes (exploratory)")
# ------------------------------------------------------------------------------------------


def contrast(forks: list[Fork], values: dict[str, float | None],
             blocks: dict[str, str] | None = None, n_perm: int = N_PERM,
             n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """Divergent - clean on a per-fork value (0/1 or a count), over observed forks: per-arm
    means with cluster-bootstrap intervals, and the randomization p (exact on totals when no
    core fork is unknown, else Monte Carlo on the difference in means)."""
    y = {k: v for k, v in values.items() if v is not None}
    core = [f for f in forks if f.arm in CORE]
    hs = _histories(core, y, blocks=blocks)
    unknown = {a: sum(values.get(f.id) is None for f in forks if f.arm == a)
               for a in ("divergent", "clean", "scoped")}
    complete_core = not unknown["divergent"] and not unknown["clean"]
    # with unknown forks dropped, the rates test is an exploratory selected-subset check, not a
    # test of the original design (astra spillover r1 m3)
    test = (exact_totals(hs) if complete_core
            else perm_test(hs, "rates", n_perm, seed) | {"exploratory_selected_subset": True})
    return {"unknown": unknown, "test": test, "bootstrap": bootstrap(forks, y, n=n_boot,
                                                                     seed=seed)}


def holm(ps: dict[str, float]) -> dict[str, float]:
    """Holm-adjusted p values (step-down, monotone)."""
    order = sorted(ps, key=ps.get)
    out, run = {}, 0.0
    for i, k in enumerate(order):
        run = max(run, min(1.0, (len(order) - i) * ps[k]))
        out[k] = run
    return out


def act_value(col: dict | None, count: bool) -> int | bool | None:
    """One spillover column's per-run value: the matching-command count (count outcomes) or
    any. A count with an unevaluable command is unknown, since its count is only a lower bound
    (astra spillover r1 m2)."""
    if col is None:
        return None
    if count:
        return None if col.get("unevaluable") else col["n"]
    return col["any"]
