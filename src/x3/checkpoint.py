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


def _arts(episodes_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(episodes_dir.rglob("*.json"))]


def _p90(xs: list[float]) -> float | None:
    return float(np.percentile(xs, 90)) if xs else None


def model_costs(arts: list[dict]) -> dict[str, dict]:
    """Measured billed cost and size per episode, by model (non-main episodes only)."""
    by: dict[str, list[dict]] = {}
    for a in arts:
        if a["meta"]["stage"] == C.Stage.MAIN.value:
            continue
        by.setdefault(a["meta"]["model"], []).append(a)
    out = {}
    for model, eps in by.items():
        cost = [sum((c["billed_usd"] or 0.0) for c in a["model_calls"]) for a in eps]
        calls = [c for a in eps for c in a["model_calls"]]
        tin = [sum(c["input_tokens"] for c in a["model_calls"]) for a in eps]
        tout = [sum(c["output_tokens"] for c in a["model_calls"]) for a in eps]
        reas = [sum((c.get("reasoning_tokens") or 0) for c in a["model_calls"]) for a in eps]
        turns = [a.get("n_turns") or 0 for a in eps]
        chars = [len(json.dumps(a.get("messages") or [])) for a in eps]
        out[model] = {
            "n_episodes": len(eps),
            "usd_mean": statistics.fmean(cost), "usd_median": statistics.median(cost),
            "usd_p90": _p90(cost),
            "input_tokens_mean": statistics.fmean(tin), "output_tokens_mean": statistics.fmean(tout),
            "reasoning_tokens_mean": statistics.fmean(reas), "turns_mean": statistics.fmean(turns),
            "transcript_chars_mean": statistics.fmean(chars),
            "share_calls_with_provider": (sum(c["provider"] is not None for c in calls)
                                          / len(calls)) if calls else None,
            "share_calls_with_billed": (sum(c["billed_usd"] is not None for c in calls)
                                        / len(calls)) if calls else None,
            "share_reasoning_readable": statistics.fmean(
                [bool((a.get("first_turn") or {}).get("reasoning_readable")) for a in eps]),
            "share_cost_cap": statistics.fmean(
                [a.get("end_reason") == C.EndReason.COST_CAP.value for a in eps]),
            "end_reasons": {r: sum(a.get("end_reason") == r for a in eps)
                            for r in {a.get("end_reason") for a in eps}},
        }
    return out


def neutral_cheat_rates(arts: list[dict], detector_csv: Path = C.DETECTOR_CSV) -> dict:
    """Detector cheat rate on calibration-type neutral episodes, by model and prompt level."""
    if not detector_csv.exists():
        return {}
    with open(detector_csv, newline="") as f:
        det = {r["episode_id"]: r for r in csv.DictReader(f)}
    acc: dict[tuple[str, str], list[bool]] = {}
    for a in arts:
        m = a["meta"]
        if m["stage"] not in (C.Stage.CALIBRATION.value, C.Stage.SWITCH.value,
                              C.Stage.GLM.value, C.Stage.PROBES.value):
            continue
        d = det.get(m["episode_id"])
        if not d or d.get("cheat") in ("", None):
            continue
        acc.setdefault((m["model"], m["prompt_level"]), []).append(d["cheat"] == "True")
    return {f"{k[0]}|{k[1]}": {"n": len(v), "rate": sum(v) / len(v)} for k, v in acc.items()}


def judge_projection(n_main_by_model: dict[str, int], costs: dict[str, dict],
                     panel: list[str]) -> dict:
    """Upper projection J of judge cost (§1.9): measured transcript length x 1.5, every episode
    treated as a non-cheat for the attempt judge."""
    from x3.judges import project_cost  # U6

    total, parts = 0.0, {}
    for model, n in n_main_by_model.items():
        chars = 1.5 * (costs.get(model, {}).get("transcript_chars_mean") or 60_000)
        n_all = n + 30                         # main run + reused calibration episodes
        for judge in panel:
            pa = project_cost(C.Judgment.PRE_ACTION, n_all, chars * 0.1, judge)
            at = project_cost(C.Judgment.ATTEMPT_G21, n_all, chars, judge)
            di = project_cost(C.Judgment.DISCLOSURE, n_all, 3_000, judge)
            parts[f"{model}|{judge}"] = {"pre_action": pa, "attempt": at, "disclosure": di}
            total += pa + at + di
    return {"J_usd": total, "parts": parts}


def build_note(episodes_dir: Path = C.EPISODES_DIR) -> dict:
    arts = _arts(episodes_dir)
    costs = model_costs(arts)
    decisions = R.load_decisions()
    analysis_models = decisions.get("_analysis_models", list(C.SUBJECTS))
    st = budget.status(C.EXP_ID, C.HARD_CAP_USD)
    n_main = {}
    proj = {}
    for m in analysis_models:
        reuse = decisions.get(m, {}).get("level") is not None
        n_main[m] = 15 * C.REPEATS_MAIN * 6 - (30 if reuse else 0)
        c = costs.get(m, {}).get("usd_mean")
        proj[m] = None if c is None else 1.25 * c * n_main[m]
    panel = ["openai/gpt-6-luna",
             C.JUDGE_FALLBACK if C.SWAP_MODEL in analysis_models else "z-ai/glm-5.3-flash"]
    J = judge_projection(n_main, costs, panel)
    main_total = sum(v for v in proj.values() if v is not None)
    need = st["spent"] + st["outstanding"] + J["J_usd"] + main_total + C.RESERVE_USD
    fits = need <= C.HARD_CAP_USD and all(v is not None for v in proj.values())
    halving = None
    if not fits:
        att = sum(p["attempt"] for p in J["parts"].values())
        need_half = need - att / 2
        halving = {"rule": "attempt judge on a random half of non-cheat episodes",
                   "selection": ("numpy.random.default_rng(20261005).permutation("
                                 "sorted(non_cheat_ids))[: n // 2], applied at judging time"),
                   "seed": C.SEED_JUDGE_HALVING, "need_with_halving": need_half,
                   "fits_with_halving": need_half <= C.HARD_CAP_USD}
    ask = not fits and not (halving or {}).get("fits_with_halving")
    note = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ledger": st, "costs": costs, "neutral_cheat_rates": neutral_cheat_rates(arts),
            "decisions": decisions, "analysis_models": analysis_models,
            "main_episodes": n_main, "main_projection_usd": proj, "J": J,
            "need_usd": need, "fits_4_repeats": fits, "judge_halving": halving,
            "ask_jacob": ask,
            "ask_reason": ("4 Qwen repeats do not fit under $45 even with judge halving; Jacob "
                           "chooses between more budget and fewer repeats (Jacob, 2026-10-04)"
                           if ask else None)}
    return note


def write_note(note: dict) -> tuple[Path, Path]:
    base = C.COST_CHECKPOINT
    base.parent.mkdir(parents=True, exist_ok=True)
    js, md = base.with_suffix(".json"), base.with_suffix(".md")
    js.write_text(json.dumps(note, indent=1, default=str) + "\n")
    lines = ["# X3 cost checkpoint", "",
             (f"UTC {note['utc']}. Spent ${note['ledger']['spent']:.2f}"
              f" of ${C.HARD_CAP_USD:.0f} (outstanding ${note['ledger']['outstanding']:.2f})."),
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
              f"Main-run projection (1.25 x mean billed): {json.dumps(note['main_projection_usd'])}",
              f"Judge projection J: ${note['J']['J_usd']:.2f}",
              f"Total needed: ${note['need_usd']:.2f}; fits at 4 repeats: {note['fits_4_repeats']}",
              ""]
    if note["judge_halving"]:
        lines.append(f"Judge-halving branch: {json.dumps(note['judge_halving'])}")
    if note["ask_jacob"]:
        lines.append(f"**STOP: {note['ask_reason']}.**")
    md.write_text("\n".join(lines) + "\n")
    return js, md
