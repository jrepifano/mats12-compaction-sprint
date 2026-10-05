"""X3 full-study pricing and power (prereg §4.8, §7.4 items 3-4; implementation plan U7b).

Free: pure arithmetic and simulation over the cost-checkpoint note (`checkpoint.build_note`
output) and the pilot's base rates. Nothing here calls an API.

Pricing basis. A model's $/episode is "measured" when the note carries its billed `usd_mean`
(times the sizing rule's 1.25 margin, prereg §7.2 step 1); otherwise it is the prereg §7.1 central
carried estimate and the row is "estimated". Anything the pilot did not run (new header wordings,
an explicit-cue arm, safeguard tasks, a probe stage) is "estimated" whatever the note says,
because the pilot's cost per episode is only a proxy for new prompts or tasks.

Power. `power_table` simulates the design: per-task random intercepts on the logit scale, two arms
within each task, equal episodes per task per arm. Each simulated data set is analysed with the
stratified (by task) risk difference and a normal-approximation test whose variance uses each
task's pooled rate under the null (a score-type stratified test). This is the large-sample
analogue of the within-task permutation test in prereg §4.9; we use it because it is about 1,000
times faster and, with balanced arms within task, has nearly the same size and power.

Assumption to flag in any report: the effect is one logit shift shared by every task (no
task x context interaction), chosen so the task-averaged risk difference equals the target. Under
that assumption stratifying by task removes between-task variance, so the simulated sample sizes
come out slightly below the nominal (unclustered) ones. If the effect varies across tasks, the
real requirement is larger; these figures are then a lower bound.
"""
from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path

import numpy as np

DEEPSEEK = "deepseek/deepseek-v4-flash"
QWEN = "qwen/qwen3.6-27b"
GEMINI = "google/gemini-3.8-flash"
GLM = "z-ai/glm-5.3-flash"

# prereg §7.1: central case, past reasoning carried
ESTIMATED_USD_PER_EPISODE = {QWEN: 0.076, DEEPSEEK: 0.003, GEMINI: 0.12, GLM: 0.021}
COST_MARGIN = 1.25                     # prereg §7.2 sizing rule, step 1

N_JUDGES = 2
N_JUDGMENTS = 3                        # pre-action, attempt (G.2.1), disclosure
USD_PER_EPISODE_JUDGMENT = 0.0015      # estimated, per judge per judgment
JUDGE_USD_PER_EPISODE_EST = N_JUDGES * N_JUDGMENTS * USD_PER_EPISODE_JUDGMENT

FULL_TASKS = 30
FULL_REPEATS = 4
N_CONTEXTS = 6
CORE_MODELS = (DEEPSEEK, QWEN, GEMINI)

# probe stage on Qwen: GPU rates from CLAUDE.md (A100-SXM4 80GB, H100 secure cloud)
PROBE_GPU = (("A100 80GB", 1.39, 4.0), ("H100 80GB", 3.29, 2.0))


def short(model: str) -> str:
    return model.split("/")[-1]


def model_rate(model: str, note: dict | None) -> tuple[float, str, str]:
    """($/episode, basis, explanation) for one subject model."""
    c = ((note or {}).get("costs") or {}).get(model) or {}
    if c.get("usd_mean") is not None:
        usd = COST_MARGIN * float(c["usd_mean"])
        return usd, "measured", (f"{COST_MARGIN} x measured mean ${c['usd_mean']:.4f} "
                                 f"over {c.get('n_episodes', '?')} pilot episodes")
    est = ESTIMATED_USD_PER_EPISODE[model]
    return est, "estimated", f"prereg §7.1 central carried estimate ${est:.3f}"


def judge_rate(note: dict | None) -> tuple[float, str]:
    """(judge $/episode, explanation). Uses, in order: a measured per-episode judge cost in the
    note, the checkpoint's upper projection J spread over its episode count, else the estimate."""
    note = note or {}
    if note.get("judge_usd_per_episode") is not None:
        return float(note["judge_usd_per_episode"]), "measured judge $/episode from the pilot"
    J = (note.get("J") or {}).get("J_usd")
    n_main = note.get("main_episodes") or {}
    judged = (note.get("J") or {}).get("n_judged")         # episodes J covers (Amendment B8)
    n = (sum(int(v) for v in judged.values()) if judged
         else sum(int(v) + 30 for v in n_main.values()))
    if J is not None and n > 0:
        return float(J) / n, ("judge upper projection J from measured transcript lengths "
                              f"(${J:.2f} over {n} episodes)")
    return JUDGE_USD_PER_EPISODE_EST, (
        f"estimated: {N_JUDGES} judges x {N_JUDGMENTS} judgments x "
        f"${USD_PER_EPISODE_JUDGMENT} per episode-judgment")


def _row(config: str, per_model_eps: dict[str, int], note: dict | None, *,
         force_estimated: bool = False, extra_usd: float = 0.0, extra_note: str = "") -> dict:
    jr, jnote = judge_rate(note)
    usd, bases, parts = 0.0, [], []
    for m, n in per_model_eps.items():
        r, basis, why = model_rate(m, note)
        usd += n * (r + jr)
        bases.append(basis)
        parts.append(f"{short(m)} {n} x ${r:.4f} ({why})")
    basis = ("estimated" if force_estimated or any(b == "estimated" for b in bases)
             else "measured")
    text = "; ".join(parts) + f"; judges ${jr:.4f}/episode ({jnote})"
    if extra_note:
        text = extra_note + ". " + text
    return {"config": config, "episodes": int(sum(per_model_eps.values())),
            "usd": round(usd + extra_usd, 2), "basis": basis, "note": text}


def price_full_study(note: dict | None) -> list[dict]:
    """Candidate full-study configurations and separately priced additions (prereg §7.4 item 3).
    Rows: {config, episodes, usd, basis: "measured"|"estimated", note}."""
    per_ctx = FULL_TASKS * FULL_REPEATS
    core = {m: per_ctx * N_CONTEXTS for m in CORE_MODELS}
    rows = [
        _row(f"Core: {FULL_TASKS} tasks x {FULL_REPEATS} repeats x {N_CONTEXTS} contexts on "
             "DeepSeek, Qwen, Gemini 3.8 Flash", core, note),
    ]
    for m in CORE_MODELS:
        rows.append(_row(f"  of which {short(m)}", {m: per_ctx * N_CONTEXTS}, note))
    rows += [
        _row("Add GLM 5.3 Flash (same design)", {GLM: per_ctx * N_CONTEXTS}, note),
        _row("Add an explicit-cue arm (+2 contexts, 3 core models)",
             {m: per_ctx * 2 for m in CORE_MODELS}, note, force_estimated=True,
             extra_note="New prompts: pilot cost per episode used as a proxy"),
        _row("Add other header wordings (+4 contexts, 3 core models)",
             {m: per_ctx * 4 for m in CORE_MODELS}, note, force_estimated=True,
             extra_note="New prompts: pilot cost per episode used as a proxy"),
        _row("Add safeguard-disabling tasks (episodes like the core, 3 core models)",
             dict(core), note, force_estimated=True,
             extra_note=("New tasks: not run in the pilot, so cost per episode is a proxy; "
                         "building and verifying the tasks is unpriced Claude/Jacob time "
                         "(roughly 1-2 days), plus a free sandbox build")),
    ]
    gpu = sum(rate * hours for _, rate, hours in PROBE_GPU)
    rows.append({"config": "Probe stage on Qwen (GPU)", "episodes": 0, "usd": round(gpu, 2),
                 "basis": "estimated",
                 "note": "estimated: " + " + ".join(f"{name} ${rate}/h x {hours:g} h"
                                                    for name, rate, hours in PROBE_GPU)})
    return rows


# --- power ---------------------------------------------------------------------------------
def nominal_mde(n_per_arm: int, base: float, alpha: float = 0.05, power: float = 0.8) -> float:
    """Minimum detectable increase over `base` (probability scale), two independent proportions,
    Cohen's h with statsmodels NormalIndPower (the prereg §4.8 table's method)."""
    from statsmodels.stats.power import NormalIndPower

    h = NormalIndPower().solve_power(nobs1=n_per_arm, alpha=alpha, power=power, ratio=1.0)
    p1 = math.sin(math.asin(math.sqrt(base)) + h / 2) ** 2
    return p1 - base


def _expit(x):
    return 1.0 / (1.0 + np.exp(-x))


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def logit_shift(base: float, task_sd: float, effect: float) -> float:
    """Logit shift d such that the task-averaged risk difference equals `effect`, with task
    intercepts u ~ N(0, task_sd) around logit(base) (Gauss-Hermite quadrature)."""
    from scipy.optimize import brentq

    x, w = np.polynomial.hermite_e.hermegauss(40)
    w = w / w.sum()
    eta = _logit(base) + task_sd * x
    p0 = float(np.sum(w * _expit(eta)))

    def f(d):
        return float(np.sum(w * _expit(eta + d))) - p0 - effect

    if f(20.0) < 0:
        raise ValueError(f"effect {effect} unreachable from base {base}")
    return brentq(f, 0.0, 20.0)


def simulate_power(base: float, task_sd: float, effect: float, n_tasks: int, n_per_task: int,
                   *, n_sims: int = 2000, alpha: float = 0.05, seed: int = 0) -> float:
    """Power of the stratified-RD normal test for `n_per_task` episodes per task per arm."""
    rng = np.random.default_rng(seed)
    d = logit_shift(base, task_sd, effect)
    eta = _logit(base) + task_sd * rng.standard_normal((n_sims, n_tasks))
    x0 = rng.binomial(n_per_task, _expit(eta))
    x1 = rng.binomial(n_per_task, _expit(eta + d))
    n = n_per_task
    rd = ((x1 - x0) / n).mean(axis=1)
    pbar = (x0 + x1) / (2 * n)
    var = (pbar * (1 - pbar) * 2 / n).sum(axis=1) / n_tasks**2
    z_crit = 1.959963984540054 if alpha == 0.05 else _z(1 - alpha / 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(var > 0, rd / np.sqrt(var), 0.0)
    return float(np.mean(np.abs(z) > z_crit))


def _z(q: float) -> float:
    from scipy.stats import norm

    return float(norm.ppf(q))


def episodes_needed(base: float, task_sd: float, effect: float, n_tasks: int = 15, *,
                    target: float = 0.8, n_sims: int = 2000, alpha: float = 0.05, seed: int = 0,
                    max_per_task: int = 4096) -> int | None:
    """Smallest episodes per arm (n_tasks x per-task count) reaching `target` power; None if not
    reached by `max_per_task` per task. Doubling then bisection over the per-task count."""
    def pw(k):
        return simulate_power(base, task_sd, effect, n_tasks, k, n_sims=n_sims, alpha=alpha,
                              seed=seed)

    lo, hi = 0, 1
    while pw(hi) < target:
        lo, hi = hi, hi * 2
        if hi > max_per_task:
            return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pw(mid) >= target:
            hi = mid
        else:
            lo = mid
    return hi * n_tasks


def power_table(base_rate: float, task_sd: float, effects=(0.05, 0.08, 0.12), n_tasks: int = 15,
                *, n_sims: int = 2000, alpha: float = 0.05, seed: int = 0) -> list[dict]:
    """Episodes per arm for 80% power at each effect (risk difference, probability scale)."""
    rows = []
    for e in effects:
        try:
            n = episodes_needed(base_rate, task_sd, e, n_tasks, n_sims=n_sims, alpha=alpha,
                                seed=seed)
        except ValueError:
            n = None
        rows.append({"effect": e, "base_rate": base_rate, "task_sd": task_sd,
                     "n_tasks": n_tasks, "episodes_per_arm": n,
                     "nominal_episodes_per_arm": _nominal_n(base_rate, e, alpha)})
    return rows


def _nominal_n(base: float, effect: float, alpha: float = 0.05) -> int | None:
    from statsmodels.stats.power import NormalIndPower
    from statsmodels.stats.proportion import proportion_effectsize

    if not 0 < base + effect < 1:
        return None
    h = proportion_effectsize(base + effect, base)
    return math.ceil(NormalIndPower().solve_power(effect_size=h, alpha=alpha, power=0.8))


def price_power(rows: list[dict], note: dict | None, models=CORE_MODELS) -> list[dict]:
    """Price each power row: a primary contrast has 2 of the 6 contexts per side, so a model's
    whole design is 3 x episodes per arm; summed over `models`."""
    out = []
    for r in rows:
        n = r["episodes_per_arm"]
        if n is None:
            out.append({**r, "episodes_total": None, "usd": None, "basis": "estimated"})
            continue
        per_model = {m: 3 * n for m in models}
        p = _row(f"power {r['effect']:.2f}", per_model, note)
        out.append({**r, "episodes_per_model": 3 * n, "episodes_total": p["episodes"],
                    "usd": p["usd"], "basis": p["basis"]})
    return out


def estimate_task_sd(counts: list[tuple[int, int]]) -> float:
    """Method-of-moments SD of task intercepts on the logit scale from per-task (cheats, n):
    variance of empirical logits (0.5 continuity) minus their mean sampling variance, floored
    at 0."""
    if len(counts) < 2:
        return 0.0
    lg = np.array([math.log((x + 0.5) / (n - x + 0.5)) for x, n in counts])
    sv = np.array([1 / (x + 0.5) + 1 / (n - x + 0.5) for x, n in counts])
    return float(math.sqrt(max(0.0, lg.var(ddof=1) - sv.mean())))


# --- payload for the CLI and the report ----------------------------------------------------
DEFAULT_BASE_RATE = 0.30               # prereg §4.8 middle column; used only when nothing measured
DEFAULT_TASK_SD = 1.0                  # assumed logit-scale SD of task intercepts


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


def base_rate_from(analysis: dict | None, note: dict | None) -> tuple[float, str]:
    """Neutral base rate: analysis.json's validity check, else the note's neutral cheat rates
    (pooled over models and prompt levels), else the default."""
    br = (((analysis or {}).get("validity") or {}).get("base_rate") or {})
    if isinstance(br, dict) and br.get("rate") is not None and br.get("n"):
        return float(br["rate"]), f"measured: analysis.json neutral base rate (n={br['n']})"
    ncr = (note or {}).get("neutral_cheat_rates") or {}
    n = sum(v["n"] for v in ncr.values())
    if n:
        r = sum(v["rate"] * v["n"] for v in ncr.values()) / n
        return r, f"measured: cost-checkpoint neutral cheat rate, pooled (n={n})"
    return DEFAULT_BASE_RATE, "estimated: prereg §4.8 middle base rate"


def task_counts_from_csv(path: Path) -> list[tuple[int, int]]:
    """Per-task (cheats, n) over analysed episodes with a defined cheat label."""
    if not path.exists():
        return []
    acc: dict[str, list[bool]] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("analysis_role") not in ("main", "neutral_reuse", "calibration_only"):
                continue
            if r.get("cheat") not in ("True", "False"):
                continue
            acc.setdefault(r["task_id"], []).append(r["cheat"] == "True")
    return [(sum(v), len(v)) for v in acc.values()]


def task_sd_from(episodes_csv: Path) -> tuple[float, str]:
    counts = task_counts_from_csv(episodes_csv)
    if len(counts) >= 5:
        return estimate_task_sd(counts), (f"measured: method of moments over {len(counts)} tasks "
                                          f"({sum(n for _, n in counts)} episodes)")
    return DEFAULT_TASK_SD, "estimated: assumed logit-scale SD (fewer than 5 tasks measured)"


def build_payload(note_path: Path, analysis_path: Path, episodes_csv: Path, *,
                  n_sims: int = 2000, n_tasks: tuple[int, ...] = (15, 30)) -> dict:
    note = _load_json(note_path)
    analysis = _load_json(analysis_path)
    base, base_src = base_rate_from(analysis, note)
    base = min(max(base, 0.01), 0.85)
    sd, sd_src = task_sd_from(episodes_csv)
    power = []
    for t in n_tasks:
        power += price_power(power_table(base, sd, n_tasks=t, n_sims=n_sims), note)
    jr, jnote = judge_rate(note)
    return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note_path": str(note_path) if note else None,
            "rates": {m: dict(zip(("usd_per_episode", "basis", "why"), model_rate(m, note)))
                      for m in (*CORE_MODELS, GLM)},
            "judge_usd_per_episode": jr, "judge_basis": jnote,
            "full_study": price_full_study(note),
            "power_inputs": {"base_rate": base, "base_rate_source": base_src, "task_sd": sd,
                             "task_sd_source": sd_src, "n_sims": n_sims, "seed": 0,
                             "method": ("stratified risk difference, normal approximation with "
                                        "per-task pooled variance; 80% power, alpha 0.05")},
            "power": power}


def _usd(x) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def payload_markdown(p: dict, heading: str = "#") -> str:
    h2 = heading + "#"
    lines = [f"{heading} X3 full-study pricing and power", "",
             f"UTC {p['utc']}. Cost note: {p['note_path'] or 'none (all rates estimated)'}.", "",
             f"{h2} $/episode by model", "", "| Model | $/episode | Basis | Source |",
             "|---|---|---|---|"]
    for m, r in p["rates"].items():
        lines.append(f"| {m} | {r['usd_per_episode']:.4f} | {r['basis']} | {r['why']} |")
    lines += ["", f"Judges: ${p['judge_usd_per_episode']:.4f} per episode ({p['judge_basis']}).",
              "", f"{h2} Full-study configurations", "",
              "| Configuration | Episodes | Cost | Basis |", "|---|---|---|---|"]
    for r in p["full_study"]:
        lines.append(f"| {r['config']} | {r['episodes']} | {_usd(r['usd'])} | {r['basis']} |")
    lines += ["", "Notes:", ""]
    lines += [f"- {r['config'].strip()}: {r['note']}" for r in p["full_study"]]
    pi = p["power_inputs"]
    lines += ["", f"{h2} Power (simulated)", "",
              (f"Base rate {pi['base_rate']:.3f} ({pi['base_rate_source']}); task SD "
               f"{pi['task_sd']:.2f} on the logit scale ({pi['task_sd_source']}); "
               f"{pi['method']}; {pi['n_sims']} simulations per point, seed {pi['seed']}. "
               "The effect is one logit shift shared by all tasks; if it varies by task, more "
               "episodes are needed, so these are lower bounds. Cost prices a primary "
               "contrast's whole six-context design (3 x episodes per arm) on each of the three "
               "core models."), "",
              ("| Effect (pts) | Tasks | Episodes per arm (sim) | Nominal, no clustering | "
               "Episodes, 3 models | Cost | Basis |"), "|---|---|---|---|---|---|---|"]
    for r in p["power"]:
        lines.append(f"| +{100 * r['effect']:.0f} | {r['n_tasks']} | "
                     f"{r['episodes_per_arm'] or 'not reached'} | "
                     f"{r['nominal_episodes_per_arm'] or 'n/a'} | "
                     f"{r.get('episodes_total') or 'n/a'} | {_usd(r['usd'])} | {r['basis']} |")
    return "\n".join(lines) + "\n"


__all__ = [
    "build_payload",
    "estimate_task_sd",
    "judge_rate",
    "model_rate",
    "nominal_mde",
    "payload_markdown",
    "power_table",
    "price_full_study",
    "price_power",
]
