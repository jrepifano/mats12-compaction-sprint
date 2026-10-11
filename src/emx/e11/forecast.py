"""E11 cost forecast and the pre-set cuts (prereg §7; implementation plan, reading 1).

`forecast(rates, plan)` mirrors the prereg §7 cost table, with the bench step's measured rates in
place of the assumed ones. By default it prices the **worst-case ladder branch**: every dataset in
the plan goes to rung 3, so pilots train at rungs 1, 2 and 3 (one luna judging wait per rung
decision) and every evaluation run trains at rung 3's step count. `apply_cuts` applies
`C.CUT_ORDER` (battery samples 10 -> 5, then part 2's pod work, then risky finance) in order until
the forecast fits, and returns the new plan with the cuts applied.

`forecast_remaining` is the HF-fallback re-forecast (prereg §7 "HF fallback"): after the ladder,
with the pod hours already used, the remaining generation at the benchmarked HF rate.

Hours, not dollars, are the unit of every GPU item; GPU $ = pod hours x $/h. API $ = judged answers
x `C.JUDGE_USD_PER_1000_ANSWERS` + pilot judging + the reader's projection. `spent_usd` is E11
money already in the ledger (it is added to the total, i.e. it comes off the cap); the reader's
projection is always counted, which is conservative by at most `C.READER_PROJECTED_USD` when the
reader's actual spend is also inside `spent_usd`.

`fits` = GPU $ <= `C.GPU_LIMIT_USD` and total $ <= `C.HARD_CAP_USD`: dollars only, the registered
prereg §7 trigger for the cuts. `pod_life_ok` (pod hours <= `C.SALVAGE_AGE_H`, the watchdog's
salvage age) is reported beside it and logged by the bench, but it never triggers a cut: prereg §7
kill rule 3 already makes the pod-life limit a hard stop (code review r1a finding 5, 2026-10-09:
the earlier pod-life term in `fits` was reverted). Stdlib only.
"""

from __future__ import annotations

from emx.e11 import contracts as C

ASSUMED_GEN_ANSWERS_PER_S = 25.0  # vLLM, 600-token-cap sampled answers at temperature 1, unmeasured
RUN_OVERHEAD_MIN = 1.5  # per training run: model save and checks (prereg §7: 12 x 1.5 min)
LORA_CHECK_H = 0.15
EVAL_RUNS_PER_DS = len(C.FRAMINGS) * len(C.EVAL_SEEDS)  # 6
PILOT_ANSWERS = C.N_QUESTIONS[C.DEV_QSET] * C.PILOT_SAMPLES  # 400
ELICIT_ANSWERS = len(C.ELICIT_CONDITIONS) * C.N_QUESTIONS[C.DEV_QSET] * C.ELICIT_SAMPLES  # 320
# Narrow-task log-prob items per held-out row: medical scores the bad and the good answer, finance
# the target (interfaces.md U4). Each evaluation adapter is scored on its own dataset, base on all.
TASK_ITEMS_PER_ROW = {"med": 2, "fin": 1}
FALLBACK_TAIL_H = 0.15  # HF fallback: log-probs' model loads, the closing manifest, pulls


def datasets_of(plan: dict) -> list[str]:
    """The datasets the plan trains: bad medical, plus risky finance unless cut 3 dropped it."""
    return [ds for ds in C.DS_KEYS if ds != "fin" or plan.get("fin", True)]


def _rate(rates: dict, key: str, default: float | None = None) -> float:
    v = rates.get(key, default)
    if v is None or not v > 0:
        raise ValueError(f"rate {key!r} must be > 0, got {v!r}")
    return float(v)


def _item(name: str, hours: float = 0.0, usd: float = 0.0, **detail) -> dict:
    return {"item": name, "hours": hours, "usd": usd, **detail}


def generation_answers(plan: dict, n_eval_models: int) -> dict[str, int]:
    """Answers of the main evaluation, the battery (every evaluation model and base, the three
    generated contexts) and the base elicitation."""
    battery = ((n_eval_models + 1) * len(C.BATTERY_GENERATED) * len(C.BATTERY_QIDS)
               * int(plan.get("battery_samples", C.BATTERY_SAMPLES)))
    return {"main": n_eval_models * C.ROWS_PER_MODEL, "battery": battery,
            "elicit": ELICIT_ANSWERS}


def logprob_passes(datasets: list[str]) -> int:
    """Forward passes of the narrow-task pass (base on every dataset, each evaluation adapter on
    its own) and the surprise pass (base, the rung-1 rows under both framings)."""
    task = sum(C.HELDOUT_ROWS * TASK_ITEMS_PER_ROW[ds] * (1 + EVAL_RUNS_PER_DS) for ds in datasets)
    surprise = len(datasets) * len(C.FRAMINGS) * C.RUNG1_ROWS
    return task + surprise


def _totals(items: list[dict], rates: dict, spent_usd: float) -> dict:
    hours = sum(i["hours"] for i in items)
    usd_h = _rate(rates, "usd_per_h")
    for i in items:
        if i["hours"]:
            i["usd"] = i["hours"] * usd_h
    gpu = hours * usd_h
    api = sum(i["usd"] for i in items if not i["hours"])
    total = gpu + api + spent_usd
    return {
        "gpu_usd": round(gpu, 4), "api_usd": round(api, 4), "spent_usd": round(spent_usd, 4),
        "total_usd": round(total, 4), "pod_hours": round(hours, 4), "usd_per_h": usd_h,
        "items": [{**i, "hours": round(i["hours"], 4), "usd": round(i["usd"], 4)} for i in items],
        "fits": gpu <= C.GPU_LIMIT_USD and total <= C.HARD_CAP_USD,
        "gpu_limit_usd": C.GPU_LIMIT_USD, "hard_cap_usd": C.HARD_CAP_USD,
        "pod_life_ok": hours <= C.SALVAGE_AGE_H, "salvage_age_h": C.SALVAGE_AGE_H,
    }


def forecast(rates: dict, plan: dict, *, worst_case: bool = True, spent_usd: float = 0.0) -> dict:
    """The forecast of the whole run (see the module docstring).

    rates: {"s_per_step", "act_s_per_pass", "usd_per_h"} required; optional "gen_answers_per_s"
    (default `ASSUMED_GEN_ANSWERS_PER_S`), "overhead_h" (`C.POD_OVERHEAD_H`),
    "logprob_s_per_pass" (default: act_s_per_pass), "run_overhead_min", "lora_check_h".
    plan: plan.json's flags (`C.PLAN_DEFAULTS` keys).
    -> {"gpu_usd", "api_usd", "spent_usd", "total_usd", "pod_hours", "items", "fits", ...}."""
    s_step = _rate(rates, "s_per_step")
    s_act = _rate(rates, "act_s_per_pass")
    s_lp = _rate(rates, "logprob_s_per_pass", s_act)
    gen = _rate(rates, "gen_answers_per_s", ASSUMED_GEN_ANSWERS_PER_S)
    run_oh = float(rates.get("run_overhead_min", RUN_OVERHEAD_MIN)) / 60
    judge = C.JUDGE_USD_PER_1000_ANSWERS / 1000
    dss = datasets_of(plan)
    rungs = sorted(C.RUNGS) if worst_case else [1]
    eval_rung = max(rungs)
    n_pilots = len(dss) * len(rungs)
    n_eval = EVAL_RUNS_PER_DS * len(dss)
    pilot_steps = sum(C.n_steps(ds, k) for ds in dss for k in rungs)
    eval_steps = sum(EVAL_RUNS_PER_DS * C.n_steps(ds, eval_rung) for ds in dss)
    answers = generation_answers(plan, n_eval)
    items = [
        _item("pod overhead and benchmark", float(rates.get("overhead_h", C.POD_OVERHEAD_H))),
        _item("pilot training", pilot_steps * s_step / 3600 + n_pilots * run_oh,
              runs=n_pilots, steps=pilot_steps, rungs=rungs),
        _item("pilot generation", n_pilots * PILOT_ANSWERS / gen / 3600,
              answers=n_pilots * PILOT_ANSWERS),
        _item("ladder judging waits", len(rungs) * C.LADDER_JUDGING_WAIT_H, decisions=len(rungs)),
        _item("evaluation training", eval_steps * s_step / 3600 + n_eval * run_oh,
              runs=n_eval, steps=eval_steps, rung=eval_rung),
        _item("generation (main, battery, elicitation)", sum(answers.values()) / gen / 3600,
              answers=answers),
        _item("narrow-task and surprise log-probs", logprob_passes(dss) * s_lp / 3600,
              passes=logprob_passes(dss)),
        _item("LoRA check", float(rates.get("lora_check_h", LORA_CHECK_H))),
    ]
    if plan.get("part2", True):
        p2_answers = C.N_VEC_ANSWERS + C.N_BASE_ANSWERS
        p2_passes = C.N_VEC_ANSWERS + 2 * C.N_BASE_ANSWERS
        items += [_item("part 2 generation", p2_answers / gen / 3600, answers=p2_answers),
                  _item("part 2 activations", p2_passes * s_act / 3600, passes=p2_passes)]
    n_judged = sum(answers.values())
    items += [
        _item("judging (main, battery, elicitation)", usd=n_judged * judge, answers=n_judged),
        _item("pilot judging", usd=n_pilots * PILOT_ANSWERS * judge,
              answers=n_pilots * PILOT_ANSWERS),
        _item("LLM reader", usd=C.READER_PROJECTED_USD),
    ]
    out = _totals(items, rates, spent_usd)
    out.update(worst_case=worst_case, plan=dict(plan), rates=dict(rates), datasets=dss)
    return out


def _cut(plan: dict, cut: str) -> dict | None:
    """The plan with `cut` applied, or None if it is already applied."""
    if cut == "battery_samples":
        if plan.get("battery_samples") == C.BATTERY_SAMPLES_CUT:
            return None
        return {**plan, "battery_samples": C.BATTERY_SAMPLES_CUT}
    if cut in ("part2", "fin"):
        return None if not plan.get(cut, True) else {**plan, cut: False}
    raise ValueError(f"unknown cut {cut!r}")


def apply_cuts(rates: dict, plan: dict, *, spent_usd: float = 0.0,
               worst_case: bool = True) -> tuple[dict, list[str]]:
    """Apply `C.CUT_ORDER` in order until the forecast fits; (new plan, cuts applied). If every
    cut is applied and it still does not fit, the returned plan has all three (the caller checks
    `forecast(...)["fits"]`)."""
    plan = dict(plan)
    cuts: list[str] = []
    for cut in C.CUT_ORDER:
        if forecast(rates, plan, worst_case=worst_case, spent_usd=spent_usd)["fits"]:
            break
        new = _cut(plan, cut)
        if new is not None:
            plan = new
            cuts.append(cut)
    return plan, cuts


def forecast_remaining(rates: dict, plan: dict, *, elapsed_h: float, n_eval_models: int,
                       datasets: list[str], n_pilots_judged: int, spent_usd: float = 0.0) -> dict:
    """The HF-fallback re-forecast: pod hours already used (`elapsed_h`) + the remaining
    generation of `n_eval_models` trained evaluation models at `rates["gen_answers_per_s"]` (the
    HF benchmark) + the log-prob passes + `FALLBACK_TAIL_H`; API: the judging of that generation,
    the pilots' judging and the reader. Same output keys as `forecast`."""
    gen = _rate(rates, "gen_answers_per_s")
    s_lp = _rate(rates, "logprob_s_per_pass", rates.get("act_s_per_pass"))
    judge = C.JUDGE_USD_PER_1000_ANSWERS / 1000
    answers = generation_answers(plan, n_eval_models)
    n_judged = sum(answers.values())
    passes = logprob_passes(datasets)
    items = [
        _item("pod hours used so far", float(elapsed_h)),
        _item("generation (main, battery, elicitation)", n_judged / gen / 3600, answers=answers),
        _item("narrow-task and surprise log-probs", passes * s_lp / 3600, passes=passes),
        _item("closing tail", FALLBACK_TAIL_H),
        _item("judging (main, battery, elicitation)", usd=n_judged * judge, answers=n_judged),
        _item("pilot judging", usd=n_pilots_judged * PILOT_ANSWERS * judge),
        _item("LLM reader", usd=C.READER_PROJECTED_USD),
    ]
    out = _totals(items, rates, spent_usd)
    out.update(worst_case=False, plan=dict(plan), rates=dict(rates), datasets=list(datasets),
               remaining=True)
    return out
