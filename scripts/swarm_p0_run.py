"""Swarm Phase 0: a few real episodes of the incident-reproduction step-2 world, per model.

Runs `step2_reachout` (world=protein_bank) with and without the collab line for one target
model, with the authors' settings (react, drive=fair, condition=baseline, temperature 1.0,
turn_limit 100, time_limit 1800 s), except that the judge is a cheap unvalidated model instead
of Opus 4.8. Scorers still run, so server-side ground truth (the registry access log, stored
PUT bodies) lands in each sample's score metadata.

Spend control: model prices are registered with Inspect and a per-episode `cost_limit` ends any
episode that reaches it (the episode is kept and marked as limited). Before running, the worst
case (n_episodes x (cost_limit + judge allowance)) plus spend already recorded for this experiment
must fit under the cap, or the script exits non-zero. After the run, cost is
computed from the log's token usage at OpenRouter list prices (fetched 2026-09-29) and
appended to the shared ledger in the same line format as `emx.spend`.

Harness: github.com/msp895/oai-hf-incident-reproduction at 2fec982 (third_party/, gitignored).
Run with the harness venv:
  uv run --project third_party/oai-hf-incident-reproduction \
    python scripts/swarm_p0_run.py --model deepseek/deepseek-v4-flash --epochs 4 --cost-limit 0.25
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from inspect_ai import eval as inspect_run
from inspect_ai.model import ModelCost, ModelInfo, set_model_info

WT = Path(__file__).resolve().parents[1]
HARNESS = WT / "third_party" / "oai-hf-incident-reproduction"
TASK = HARNESS / "src/alignment_auditor/docker_replication/step2_reachout/task.py"
MAIN_REPO = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory")
LEDGER = MAIN_REPO / "logs" / "costs.jsonl"  # the one shared ledger (untracked)
ENV_FILE = MAIN_REPO / ".env"
EXP = "SW-P0"
CAP_USD = 10.0
JUDGE = "deepseek/deepseek-v4-flash"
JUDGE_ALLOWANCE_USD = 0.02  # per episode: two judge calls on a <=~60k-token prompt

# OpenRouter list prices, $/Mtok (input, output, cache read), /api/v1/models on 2026-09-29.
PRICES = {
    "deepseek/deepseek-v4-flash": (0.076, 0.153, 0.01526),
    "qwen/qwen3.7-flash": (0.030, 0.130, 0.006),
    "z-ai/glm-5.3": (1.400, 4.400, 0.26),
}


def ledger_entries() -> list[dict]:
    if not LEDGER.exists():
        return []
    with open(LEDGER, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            text = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def spent() -> float:
    return sum(float(e.get("usd", 0)) for e in ledger_entries() if e.get("exp") == EXP)


def record(resource: str, units: float, usd: float, note: str) -> None:
    entry = {"utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "exp": EXP, "kind": "api",
             "resource": resource, "units": units, "usd": round(usd, 6), "note": note}
    with open(LEDGER, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    print("ledger:", entry)


def usd_for(model: str, u) -> float:
    """Price one ModelUsage.

    For OpenAI-compatible providers (OpenRouter) Inspect sets `input_tokens` to prompt tokens
    *minus* cached tokens, and `output_tokens` to completion tokens, which already include
    reasoning tokens (inspect_ai/model/_openai.py, 0.3.257). So: uncached input at the input
    rate, cache reads at the cache rate, output at the output rate.
    """
    key = model.removeprefix("openrouter/")
    p_in, p_out, p_cache = PRICES[key]
    cache = u.input_tokens_cache_read or 0
    return (u.input_tokens * p_in + cache * p_cache + u.output_tokens * p_out) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(PRICES))
    ap.add_argument("--epochs", type=int, required=True)
    ap.add_argument("--cost-limit", type=float, required=True, help="$ per episode")
    ap.add_argument("--max-samples", type=int, default=4)
    args = ap.parse_args()

    env = dotenv_values(ENV_FILE)
    if not env.get("OPENROUTER_API_KEY"):
        sys.exit(f"OPENROUTER_API_KEY missing from {ENV_FILE}")
    os.environ["OPENROUTER_API_KEY"] = env["OPENROUTER_API_KEY"]
    print("loaded keys: OPENROUTER_API_KEY")

    n_eps = 2 * args.epochs  # two arms
    worst = n_eps * (args.cost_limit + JUDGE_ALLOWANCE_USD)
    already = spent()
    print(f"worst case {worst:.2f} + spent {already:.2f} vs cap {CAP_USD:.2f}")
    if already + worst > CAP_USD:
        sys.exit(2)

    target = f"openrouter/{args.model}"
    common = {"scaffold": "react", "condition": "baseline", "drive": "fair",
              "world": "protein_bank", "temperature": 1.0, "turn_limit": 100,
              "judge_model": f"openrouter/{JUDGE}"}
    for key, (p_in, p_out, p_cache) in PRICES.items():
        cost = ModelCost(input=p_in, output=p_out, input_cache_write=p_in, input_cache_read=p_cache)
        set_model_info(f"openrouter/{key}", ModelInfo(organization=key.split("/")[0], cost=cost))
    os.chdir(HARNESS)  # inspect only accepts task paths relative to the cwd
    rel = str(TASK.relative_to(HARNESS))
    tasks = [(rel, {**common, "collab": False}), (rel, {**common, "collab": True})]
    log_dir = WT / "results" / "SW-P0" / "logs" / args.model.replace("/", "__")
    total = 0.0
    for path, targs in tasks:
        logs = inspect_run(path, model=target, task_args=targs, epochs=args.epochs,
                           cost_limit=args.cost_limit, max_samples=args.max_samples,
                           display="plain", log_dir=str(log_dir), fail_on_error=False)
        for log in logs:
            arm = "collab" if targs["collab"] else "baseline"
            usage = log.stats.model_usage if log.stats else {}
            for m, u in usage.items():
                usd = usd_for(m, u)
                total += usd
                role = "judge" if JUDGE in m else "target"
                record(m.removeprefix("openrouter/"), args.epochs, usd,
                       f"sw_p0 step2 protein_bank {arm} {role} "
                       f"in={u.input_tokens} cache={u.input_tokens_cache_read} "
                       f"out={u.output_tokens} log={Path(log.location).name}")
            print(f"{arm}: status={log.status} samples={len(log.samples or [])}")
    print(f"TOTAL this run ${total:.3f}; experiment total ${spent():.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
