"""X3 full-study pricing and power from the pilot's measured numbers (prereg §7.4; free).

Power: `pricing.simulate_power` (stratified risk difference, task random effects on the logit
scale), 80% power, alpha 0.05, 2000 simulations, seed 0. Base rate 20% (the main run's
capability-framed rate) falling by 5, 8 or 12 points, simulated as a rise from 20 - d (the test
is two-sided). Task SD measured from the main run's capability-framed episodes per task. Designs
are whole six-context blocks: with T tasks and R repeats, an arm (2 of 6 contexts) holds 2*R
episodes per task and the study 6*T*R episodes per model. Measured $/episode: GLM from the
main-run ledger lines (upper bound, $0.20 limit); Qwen from its 30 calibration units (upper
bound, $0.50 limit); Gemini 3.8 Flash from its 15 cost probes (known billed); judges from the
three judgments that ran. Writes results/X3/full_study_price.json.
"""
from __future__ import annotations

import csv
import json

from s4world import budget
from x3 import checkpoint as CP
from x3 import contracts as C
from x3 import pricing as P

BASE = 0.20
EFFECTS = (0.05, 0.08, 0.12)
TASKS = (15, 30)
N_SIMS, SEED = 2000, 0


def ledger_main_usd() -> float:
    tot = 0.0
    for line in budget.ledger_path().read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        n = e.get("note") or ""
        if e.get("exp") == C.EXP_ID and ("X3 main " in n or "|main|" in n):
            tot += float(e["usd"])
    return tot


def cap_task_counts() -> list[tuple[int, int]]:
    by: dict[str, list[int]] = {}
    with open(C.EPISODES_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r["stage"] == "main" and r["context"] in ("cap_script", "cap_human"):
                by.setdefault(r["task_id"], []).append(r["cheat"] == "True")
    return [(sum(v), len(v)) for v in by.values()]


def main() -> None:
    note = json.loads(C.COST_CHECKPOINT.with_suffix(".json").read_text())
    arts = CP._arts(C.EPISODES_DIR)
    gem = [sum(c["billed_usd"] or 0 for c in a["model_calls"]) for a in arts
           if a["meta"]["model"] == "google/gemini-3.8-flash"]
    judged = 0.0
    for f in C.JUDGED_DIR.glob("*.csv"):
        with open(f, newline="") as fh:
            judged += sum(float(r["billed_usd"] or 0) for r in csv.DictReader(fh))
    main_usd = ledger_main_usd()
    per_ep = {
        "z-ai/glm-5.3-flash": {"usd": main_usd / 360,
                               "basis": f"main-run ledger ${main_usd:.2f} incl. signed "
                                        "corrections / 360 (upper bound; $0.20 limit)"},
        "qwen/qwen3.6-27b": {"usd": note["sizing"].get("qwen/qwen3.6-27b", {}).get("usd_mean")
                             or 0.177940806,
                             "basis": "30 calibration units, upper bound ($0.50 limit)"},
        "google/gemini-3.8-flash": {"usd": sum(gem) / len(gem),
                                    "basis": f"{len(gem)} cost probes, known billed"},
    }
    judge_ep = judged / 360
    counts = cap_task_counts()
    sd_main = P.estimate_task_sd(counts)
    # the main run's capability side (8 episodes per task) shows no between-task variance, so
    # the conservative design also uses the SD measured over calibration (pricing.json)
    sd_cal = json.loads((C.RESULTS_DIR / "pricing.json").read_text())["power_inputs"]["task_sd"]
    designs = []
    for sd_name, task_sd in (("main_run", sd_main), ("calibration", sd_cal)):
        for d in EFFECTS:
            for T in TASKS:
                reps = None
                for R in range(1, 400):
                    pw = P.simulate_power(BASE - d, task_sd, d, T, 2 * R, n_sims=N_SIMS,
                                          seed=SEED)
                    if pw >= 0.8:
                        reps = R
                        break
                eps = 6 * T * reps
                designs.append({
                    "task_sd_from": sd_name, "task_sd": task_sd, "effect_pts": round(d * 100),
                    "tasks": T, "repeats": reps, "episodes_per_arm": 2 * T * reps,
                    "episodes_per_model": eps,
                    "cost_per_model": {m: round(eps * (v["usd"] + judge_ep), 2)
                                       for m, v in per_ep.items()}})
    out = {"base_rate": BASE,
           "task_sd_logit": {"main_run": sd_main, "calibration": sd_cal},
           "task_sd_basis": {"main_run": f"method of moments, main-run capability-framed "
                                         f"episodes, {len(counts)} tasks x {counts[0][1]}",
                             "calibration": "pricing.json: method of moments over 15 tasks "
                                            "(480 calibration episodes)"},
           "power": {"method": "pricing.simulate_power", "n_sims": N_SIMS, "seed": SEED,
                     "target": 0.8, "alpha": 0.05},
           "judge_usd_per_episode": judge_ep, "per_episode": per_ep, "designs": designs}
    (C.RESULTS_DIR / "full_study_price.json").write_text(json.dumps(out, indent=1))
    print(f"task SD main {sd_main:.3f}, calibration {sd_cal:.3f}; main ledger ${main_usd:.4f}")
    for x in designs:
        print(x["task_sd_from"], x["effect_pts"], "pts", x["tasks"], "tasks: repeats", x["repeats"], "episodes",
              x["episodes_per_model"], x["cost_per_model"])


if __name__ == "__main__":
    main()
