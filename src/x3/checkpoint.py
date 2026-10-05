"""X3 cost-checkpoint note (implementation plan §1.9, §1.12; prereg §7.2-7.3).

Free: reads the episode artifacts, detector.csv, the ledger and the decisions, and writes
results/X3/cost_checkpoint.{json,md}. It decides nothing that spends money; the main run needs
Jacob's go-ahead after he reads this note.
"""
from __future__ import annotations

import csv
import json
import statistics
import time
from pathlib import Path

import numpy as np

from s4world import budget
from x3 import contracts as C
from x3 import runner as R
from x3.window import pre_action_window


def _arts(episodes_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(episodes_dir.rglob("*.json"))]


def _p90(xs: list[float]) -> float | None:
    return float(np.percentile(xs, 90)) if xs else None


SIZING_STAGES = (C.Stage.CALIBRATION.value, C.Stage.SWITCH.value, C.Stage.GLM.value,
                 C.Stage.PROBES.value)


def sizing_cost(arts: list[dict], model: str, level: str | None) -> dict:
    """Mean billed $/episode for sizing: the model's 30 successful neutral units at its final
    prompt level, each with complete billing (code review r1 finding 7, r3 finding 5). Anything
    less gives no mean, and the main run cannot be sized."""
    eps = [a for (m, lv, _t, _r), a in neutral_units(arts).items()
           if m == model and (level is None or lv == level)]
    costs = {a["meta"]["episode_id"]: episode_cost_upper(a) for a in eps}
    incomplete = [eid for eid, c in costs.items() if c is None]
    if len(eps) != N_NEUTRAL_UNITS or incomplete:
        return {"n": len(eps), "required": N_NEUTRAL_UNITS, "usd_mean": None,
                "billing_incomplete": incomplete, "level": level}
    n_upper = sum(1 for a in eps if any(c["billed_usd"] is None for c in a["model_calls"]))
    # the prereg's "mean billed cost" counts a failed attempt as $0 (a lower bound); usd_mean
    # charges it its ceiling-priced bound instead, which needs an amendment (review r1, finding 3)
    known = [sum(c["billed_usd"] or 0.0 for c in a["model_calls"]) for a in eps]
    return {"n": len(eps), "required": N_NEUTRAL_UNITS,
            "usd_mean": statistics.fmean(costs.values()),
            "usd_mean_billed_only": statistics.fmean(known), "billing_incomplete": [],
            "n_with_failed_attempts_at_bound": n_upper, "level": level}


def _attempts_charged(charge: float, input_bound: int, max_tokens: int, spec: dict) -> int:
    """How many failed attempts `charge` pays for: the loop charges each one its single-attempt
    bound (task.call_bound / ATTEMPTS)."""
    one = (input_bound * spec["in_ceiling"] + max_tokens * spec["out_ceiling"]) / 1e6
    return round(charge / one) if one > 0 else 0


def failed_attempt_charge(art: dict) -> tuple[float, int] | None:
    """(charge, attempts): what the episode loop and the probe charged for failed attempts, each
    at its ceiling-priced single-attempt bound, and how many attempts that charge covers. Read
    from the sample's store in the .eval log; None if the log cannot be read."""
    try:
        from inspect_ai.log import read_eval_log

        for s in read_eval_log(art["eval_log"]).samples or []:
            if (s.metadata or {}).get("episode_id") == art["meta"]["episode_id"]:
                st = s.store or {}
                spec = st.get(C.STORE_META) or {}
                bounds = (st.get(C.STORE_EPISODE) or {}).get("bounds") or []
                probe = st.get(C.STORE_PROBE) or {}
                charge, n = 0.0, 0
                for b in bounds:
                    fc = float(b.get("failed_charge") or 0.0)
                    charge += fc
                    n += _attempts_charged(fc, b["input_bound"], b["max_tokens"], spec)
                pfc = float(probe.get("failed_attempt_charge") or 0.0)
                charge += pfc
                if pfc:
                    n += _attempts_charged(pfc, probe["input_bound"], probe["max_tokens"], spec)
                return charge, n
    except Exception:  # noqa: BLE001 - unreadable log: no bound
        return None
    return None


def episode_cost_upper(art: dict) -> float | None:
    """An episode's billed cost, or, when some calls are failed attempts with no billed cost, a
    conservative upper bound: known billed + the failed attempts' charges. None if any unbilled
    call is not an error, or the recorded charges cover fewer attempts than there are unbilled
    error calls (cost-checkpoint review r1, finding 1)."""
    calls = art.get("model_calls") or []
    if not calls:
        return None
    known = sum(c["billed_usd"] for c in calls if c["billed_usd"] is not None)
    missing = [c for c in calls if c["billed_usd"] is None]
    if not missing:
        return known
    if any(not c.get("error") for c in missing):
        return None
    rec = failed_attempt_charge(art)
    if rec is None or rec[1] < len(missing):
        return None
    return known + rec[0]

def model_costs(arts: list[dict]) -> dict[str, dict]:
    """Measured billed cost and size per episode, by model (non-main episodes only)."""
    by: dict[str, list[dict]] = {}
    for a in arts:
        if a["meta"]["stage"] == C.Stage.MAIN.value:
            continue
        by.setdefault(a["meta"]["model"], []).append(a)
    out = {}
    for model, eps in by.items():
        complete = [a for a in eps if a["model_calls"]
                    and all(c["billed_usd"] is not None for c in a["model_calls"])]
        n_incomplete = len(eps) - len(complete)
        # unknown billing is never averaged in as $0 (review r2, finding 6)
        cost = ([sum(c["billed_usd"] for c in a["model_calls"]) for a in complete]
                if not n_incomplete else [])
        calls = [c for a in eps for c in a["model_calls"]]
        tin = [sum(c["input_tokens"] for c in a["model_calls"]) for a in eps]
        tout = [sum(c["output_tokens"] for c in a["model_calls"]) for a in eps]
        reas = [sum((c.get("reasoning_tokens") or 0) for c in a["model_calls"]) for a in eps]
        turns = [a.get("n_turns") or 0 for a in eps]
        chars = [len(json.dumps(a.get("messages") or [])) for a in eps]
        out[model] = {
            "n_episodes": len(eps),
            "n_billing_incomplete": n_incomplete,
            "usd_mean": statistics.fmean(cost) if cost else None,
            "usd_median": statistics.median(cost) if cost else None,
            "usd_p90": _p90(cost),
            "input_tokens_mean": statistics.fmean(tin), "output_tokens_mean": statistics.fmean(tout),
            "reasoning_tokens_mean": statistics.fmean(reas), "turns_mean": statistics.fmean(turns),
            "transcript_chars_mean": statistics.fmean(chars),
            "share_calls_with_provider": (sum(c["provider"] is not None for c in calls)
                                          / len(calls)) if calls else None,
            "share_calls_with_billed": (sum(c["billed_usd"] is not None for c in calls)
                                        / len(calls)) if calls else None,
            "share_reasoning_readable": statistics.fmean(
                [pre_action_window(a)["reasoning_readable"] for a in eps]),  # Amendment B2
            "share_cost_cap": statistics.fmean(
                [a.get("end_reason") == C.EndReason.COST_CAP.value for a in eps]),
            "end_reasons": {r: sum(a.get("end_reason") == r for a in eps)
                            for r in {a.get("end_reason") for a in eps}},
        }
    return out


def neutral_units(arts: list[dict]) -> dict[tuple, dict]:
    """One successful final attempt per planned neutral unit (model, level, task, repeat) of the
    calibration-type stages (code review r3, findings 4-5). Failed attempts never count."""
    units: dict[tuple, dict] = {}
    for a in arts:
        m = a["meta"]
        if (m["stage"] not in SIZING_STAGES or m["context"] != C.Context.NEUTRAL.value
                or R.failed(a)):
            continue
        key = (m["model"], m["prompt_level"], m["task_id"], int(m["repeat"]))
        if key not in units or int(m["attempt"]) > int(units[key]["meta"]["attempt"]):
            units[key] = a
    return units


N_NEUTRAL_UNITS = 30          # 15 tasks x 2 repeats (prereg §4.4)


def neutral_cheat_rates(arts: list[dict], detector_csv: Path = C.DETECTOR_CSV) -> dict:
    """Detector cheat rate over the successful neutral units, by model and prompt level, with
    the count of units still undecided (no detector row, or cheat queued for Jacob)."""
    if not detector_csv.exists():
        return {}
    with open(detector_csv, newline="") as f:
        det = {r["episode_id"]: r for r in csv.DictReader(f)}
    acc: dict[tuple[str, str], list[bool]] = {}
    undecided: dict[tuple[str, str], int] = {}
    for (model, level, _t, _r), a in neutral_units(arts).items():
        d = det.get(a["meta"]["episode_id"])
        if not d or d.get("cheat") in ("", None):
            undecided[(model, level)] = undecided.get((model, level), 0) + 1
            continue
        acc.setdefault((model, level), []).append(d["cheat"] == "True")
    keys = set(acc) | set(undecided)
    return {f"{k[0]}|{k[1]}": {"n": len(acc.get(k, [])),
                               "rate": (sum(acc[k]) / len(acc[k])) if acc.get(k) else None,
                               "undecided": undecided.get(k, 0),
                               "complete": len(acc.get(k, [])) == N_NEUTRAL_UNITS}
            for k in keys}

JUDGED = (C.Judgment.PRE_ACTION, C.Judgment.ATTEMPT_G21, C.Judgment.DISCLOSURE)


def judge_input_tokens(units: dict[str, list[dict]]) -> dict[str, dict[str, float]]:
    """Mean measured length, in o200k tokens, of each judgment's actual input (system + user,
    as `judges.judge_input` builds it), by model, over its sizing units. The first projection
    counted characters as tokens (x1.2 for bytes) and sized the first-turn window at a tenth of
    the transcript; measured, the attempt input runs about 3 characters per token and the
    first-turn window about 400 tokens (2026-10-05)."""
    import tiktoken

    from x3.judges import judge_input  # U6

    enc = tiktoken.get_encoding("o200k_base")
    by: dict[str, dict[str, list[int]]] = {}
    for a in (a for eps in units.values() for a in eps):
        for jd in JUDGED:
            ji = judge_input(a, jd)
            if ji is None:
                continue
            txt = (ji[0] or "") + ji[1]
            n = len(enc.encode(txt, disallowed_special=()))
            by.setdefault(a["meta"]["model"], {}).setdefault(jd.value, []).append(n)
    return {m: {jd: statistics.fmean(v) for jd, v in d.items()} for m, d in by.items()}


def judge_projection(n_main_by_model: dict[str, int], tokens: dict[str, dict[str, float]],
                     panel: list[str], reused: dict[str, int] | None = None) -> dict:
    """Projection J of judge cost under the prereg rule (§1.9): the measured input length
    (o200k tokens) x 1.5 plus the full output allowance per request, every episode treated as a
    non-cheat for the attempt judge. Not a strict bound: Gemini Flash-Lite counted 1.05-1.46x
    the o200k tokens on three real inputs (results/X3/judge_token_check.json), and a reply that
    does not parse is asked again once. The runtime guard caps actual judge spend."""
    total, parts, n_judged = 0.0, {}, {}
    for model, n in n_main_by_model.items():
        n_all = n + (reused or {}).get(model, 30)   # main run + reused calibration episodes
        n_judged[model] = n_all
        for judge in panel:
            p_in, p_out = C.JUDGE_PRICES[judge]
            part = {}
            for jd in JUDGED:
                tok = 1.5 * tokens[model][jd.value] + 200
                key = "attempt" if jd == C.Judgment.ATTEMPT_G21 else jd.value
                part[key] = n_all * (tok * p_in + C.JUDGE_MAX_TOKENS * p_out) / 1e6
            parts[f"{model}|{judge}"] = part
            total += sum(part.values())
    return {"J_usd": total, "parts": parts, "input_tokens_mean": tokens, "n_judged": n_judged}


def build_note(episodes_dir: Path = C.EPISODES_DIR) -> dict:
    arts = _arts(episodes_dir)
    costs = model_costs(arts)
    decisions = R.load_decisions()
    analysis_models = decisions.get("_analysis_models", list(C.SUBJECTS))
    cap = float(decisions.get("_main_cap_usd", C.HARD_CAP_USD))   # Jacob's recorded cap
    st = budget.status(C.EXP_ID, cap)
    n_main = {}
    proj = {}
    sizing = {}
    for m in analysis_models:
        level = decisions.get(m, {}).get("level")
        reuse = level is not None and R.reuses_neutral(decisions, m)       # Amendment B8
        n_main[m] = 15 * C.REPEATS_MAIN * 6 - (30 if reuse else 0)
        sizing[m] = sizing_cost(arts, m, level)
        c = sizing[m]["usd_mean"]
        proj[m] = None if c is None else 1.25 * c * n_main[m]
    panel = ["openai/gpt-6-luna",
             C.JUDGE_FALLBACK if C.SWAP_MODEL in analysis_models else "z-ai/glm-5.3-flash"]
    nu = neutral_units(arts)
    units = {m: [a for (mm, lv, _t, _r), a in nu.items()
                 if mm == m and lv == decisions.get(m, {}).get("level")]
             for m in analysis_models}
    reused = {m: 15 * C.REPEATS_MAIN * 6 - n for m, n in n_main.items()}
    J = judge_projection(n_main, judge_input_tokens(units), panel, reused)
    main_total = sum(v for v in proj.values() if v is not None)
    need = st["spent"] + st["outstanding"] + J["J_usd"] + main_total + C.RESERVE_USD
    unpriced = [m for m, v in proj.items() if v is None]
    fits = need <= cap and not unpriced
    halving = None
    if not fits and not unpriced:
        att = sum(p["attempt"] for p in J["parts"].values())
        need_half = need - att / 2
        halving = {"rule": "attempt judge on a random half of non-cheat episodes",
                   "selection": ("numpy.random.default_rng(20261005).permutation("
                                 "sorted(non_cheat_ids))[: n // 2], applied at judging time"),
                   "seed": C.SEED_JUDGE_HALVING, "need_with_halving": need_half,
                   "fits_with_halving": need_half <= cap}
    ask = not fits and not (halving or {}).get("fits_with_halving")
    if unpriced:
        reason = (f"no complete billed cost for {unpriced} at the final prompt level, so the "
                  "main run cannot be sized; Jacob decides how to proceed")
    elif ask:
        reason = (f"the main run at the recorded design does not fit under ${cap:.0f} even "
                  "with judge halving; Jacob chooses between more budget and a smaller run "
                  "(Jacob, 2026-10-04)")
    else:
        reason = None
    note = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ledger": st, "costs": costs, "sizing": sizing,
            "neutral_cheat_rates": neutral_cheat_rates(arts),
            "decisions": decisions, "analysis_models": analysis_models,
            "main_episodes": n_main, "main_projection_usd": proj, "J": J,
            "cap_usd": cap, "need_usd": need, "fits_4_repeats": fits, "judge_halving": halving,
            "ask_jacob": ask, "ask_reason": reason}
    return note


def write_note(note: dict) -> tuple[Path, Path]:
    base = C.COST_CHECKPOINT
    base.parent.mkdir(parents=True, exist_ok=True)
    js, md = base.with_suffix(".json"), base.with_suffix(".md")
    js.write_text(json.dumps(note, indent=1, default=str) + "\n")
    lines = ["# X3 cost checkpoint", "",
             (f"UTC {note['utc']}. Spent ${note['ledger']['spent']:.2f}"
              f" of ${note['cap_usd']:.0f} (outstanding ${note['ledger']['outstanding']:.2f})."),
             "",
             ("| Model | Episodes | $/episode mean | median | p90 | turns | provider recorded |"
              " billed recorded | reasoning readable | cost_cap |"),
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m, c in note["costs"].items():
        def f(x, p=3):
            return "" if x is None else f"{x:.{p}f}"
        lines.append(f"| {m} | {c['n_episodes']} | {f(c['usd_mean'], 4)} | "
                     f"{f(c['usd_median'], 4)} | {f(c['usd_p90'], 4)} | {f(c['turns_mean'], 1)} |"
                     f" {f(c['share_calls_with_provider'], 2)} | "
                     f"{f(c['share_calls_with_billed'], 2)} | "
                     f"{f(c['share_reasoning_readable'], 2)} | {f(c['share_cost_cap'], 2)} |")
    lines += ["", "Neutral cheat rates (detector): "
              + json.dumps(note["neutral_cheat_rates"]), "",
              ("Sizing $/episode [failed attempts at their ceiling charge, billed only]: "
               + json.dumps({m: [v["usd_mean"], v.get("usd_mean_billed_only")]
                             for m, v in note["sizing"].items()})),
              ("Main-run projection (1.25 x mean episode cost, failed attempts at their ceiling "
               f"charge): {json.dumps(note['main_projection_usd'])}"),
              f"Judge projection J (prereg rule, not a strict bound): ${note['J']['J_usd']:.2f}",
              f"Total needed: ${note['need_usd']:.2f}; fits at 4 repeats: {note['fits_4_repeats']}",
              ""]
    if note["judge_halving"]:
        lines.append(f"Judge-halving branch: {json.dumps(note['judge_halving'])}")
    if note["ask_jacob"]:
        lines.append(f"**STOP: {note['ask_reason']}.**")
    md.write_text("\n".join(lines) + "\n")
    return js, md
