"""P08 orchestrator: contexts + twins -> episode specs, run in chunks, each behind the spend guard
and recorded in the ledger once it settles. Ported from P07's run.py
(probes/P07-compaction-trust/run.py).

  uv run --group swarm python probes/P08-dropped-fact-behaviour/run.py episodes --model glm \
      --header neutral --cells full,minus_c,minus_t --ctx smoke [--repeats 3] [--dry-run]
  uv run --group swarm python probes/P08-dropped-fact-behaviour/run.py status

--ctx: smoke (the first SMOKE_CONTEXTS contexts in use), rest (the others), all, or a comma list
of ctx_ids. Contexts in use: the contexts file in draw order (reserves included), skipping any
whose twin record is not "ok", up to N_CONTEXTS. Existing artifacts (no error, not a replay or
fact-check failure) are skipped, so a rerun resumes. Each chunk: `emx.spend.check_total(n x
per-episode projection)`, one Inspect eval, extraction, then one ledger row with the billed
total (an episode with no billed cost is charged its ceiling-price spend). A STOP file in
results/ ends the run after the current chunk.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

PROBE = Path(__file__).resolve().parent
ROOT = PROBE.parents[1]
for _p in (PROBE, ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from p08 import contracts as C

# Per-episode projections before the smoke measures them (plan "Cap": <= $0.02 per GLM resumed
# episode; P07's resumed GLM episodes cost $0.009-0.012).
PROJECTION_USD = {"glm-5.3-flash": 0.02, "deepseek-v4-flash": 0.04}
STOP_FILE = C.RESULTS_DIR / "STOP"
SKIP_RERUN_REASONS = ("replay_mismatch", "fact_check_failed")
MODEL_ARG = {"glm": "glm-5.3-flash", "deepseek": "deepseek-v4-flash"}


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def contexts_in_use(header: str) -> list[dict]:
    """Contexts in draw order whose twin record is "ok", up to N_CONTEXTS; each with `twin`."""
    twins = {(t["ctx_id"], t["header"]): t for t in load_jsonl(C.TWINS_JSONL)}
    out = []
    for c in load_jsonl(C.contexts_jsonl(header)):
        t = twins.get((c["ctx_id"], header))
        if t is None or t.get("status") != "ok":
            continue
        out.append({**c, "twin": t})
        if len(out) == C.N_CONTEXTS:
            break
    return out


def select(ctxs: list[dict], which: str) -> list[dict]:
    if which == "smoke":
        return ctxs[: C.SMOKE_CONTEXTS]
    if which == "rest":
        return ctxs[C.SMOKE_CONTEXTS:]
    if which == "all":
        return ctxs
    ids = set(which.split(","))
    return [c for c in ctxs if c["ctx_id"] in ids]


def cell_names(which: str, header: str) -> list[str]:
    if which == "all":
        return [c.name for c in C.CELLS] + (["minus_h"] if header == "safety" else [])
    names = which.split(",")
    bad = [n for n in names if n not in C.CELL_BY_NAME]
    if bad:
        raise SystemExit(f"unknown cells {bad}")
    return names


def done_already(model: C.Model, header: str, cell: str, ctx_id: str, repeat: int) -> bool:
    """True when the base artifact or any `.dupN` rerun of it completed without error."""
    p = C.episode_path(model.slug, header, cell, ctx_id, repeat)
    for q in [p, *sorted(p.parent.glob(f"{p.stem}.dup*.json"))]:
        if q.exists():
            art = json.loads(q.read_text())
            if not art.get("error") and art.get("end_reason") not in SKIP_RERUN_REASONS + (
                    "api_error",):
                return True
    return False


def build_specs(model: C.Model, ctxs: list[dict], cells: list[str], repeats: int) -> list:
    from p08 import agent
    from p08 import cells as cellmod

    specs = []
    for c in ctxs:
        for name in cells:
            r = cellmod.build(c, c["twin"], name)
            for k in range(repeats):
                if not done_already(model, r.header, name, c["ctx_id"], k):
                    specs.append(agent.spec_from(c, r, model, k))
    return specs


def settle(arts: list[dict]) -> tuple[float, int]:
    """Billed total of a chunk; an episode without a billed cost counts at ceiling prices."""
    usd, unknown = 0.0, 0
    for a in arts:
        b = a.get("billed_usd")
        if b is None:
            unknown += 1
            b = float(a.get("spent_at_ceiling") or 0.0)
        usd += b
    return usd, unknown


def wait_for_load(max_load: float, poll_s: int = 30) -> None:
    """Plan "Automatic rules": pause launches while the laptop's 1-minute load is above
    `max_load` (other probes share the laptop)."""
    t0 = time.time()
    while os.getloadavg()[0] > max_load:
        if time.time() - t0 < poll_s:
            print(f"load {os.getloadavg()[0]:.0f} > {max_load:.0f}: waiting", flush=True)
        time.sleep(poll_s)
    if time.time() - t0 > poll_s:
        print(f"load ok after {time.time() - t0:.0f}s", flush=True)


def run_episodes(a) -> int:
    from p08 import agent

    from emx import spend

    model = C.MODELS[MODEL_ARG[a.model]]
    ctxs = select(contexts_in_use(a.header), a.ctx)
    specs = build_specs(model, ctxs, cell_names(a.cells, a.header), a.repeats)
    bad = [s.episode_id for s in specs if not s.fact_check.get("ok")]
    per_ep = a.per_episode_usd or PROJECTION_USD[model.slug]
    print(json.dumps({"model": model.slug, "header": a.header, "contexts": len(ctxs),
                      "episodes": len(specs), "fact_check_failures": bad,
                      "per_episode_projection": per_ep,
                      "projected_usd": round(per_ep * len(specs), 2),
                      "budget": spend.totals(C.EXP)}, default=str), flush=True)
    if bad:
        print("fact check fails before any call: fix the twins first", flush=True)
        return 2
    if a.dry_run or not specs:
        return 0
    for i in range(0, len(specs), a.chunk):
        if STOP_FILE.exists():
            print(f"STOP file present: {STOP_FILE.read_text().strip()}", flush=True)
            return 3
        chunk = specs[i: i + a.chunk]
        wait_for_load(a.max_load)
        spend.check_total(per_ep * len(chunk), "judge", exp=C.EXP)
        t0 = time.time()
        # one log directory per process and chunk: run_batch takes every new .eval in its
        # directory as its own (P07, 2026-10-11)
        log_dir = C.LOGS_DIR / model.slug / f"{os.getpid()}-{i // a.chunk}"
        logs = agent.run_batch(chunk, model, log_dir, max_samples=a.max_samples)
        arts = [art for p in logs for art in agent.extract(p)]
        usd, unknown = settle(arts)
        spend.record("judge", f"openrouter:{model.model_id}", len(arts), round(usd, 6),
                     note=f"P08 episodes {model.slug}/{a.header} chunk {i // a.chunk}: "
                     f"{len(arts)} episodes" + (f"; {unknown} at ceiling" if unknown else ""),
                     exp=C.EXP)
        errs = sum(1 for x in arts if x.get("error") or x.get("end_reason") == "api_error")
        print(f"P08_CHUNK model={model.slug} chunk={i // a.chunk} n={len(arts)} "
              f"usd={usd:.4f} unknown={unknown} errors={errs} "
              f"wall={time.time() - t0:.0f}s total={spend.totals(C.EXP)}", flush=True)
    return 0


def status(_a) -> int:
    from emx import spend

    counts: dict[str, int] = {}
    for p in C.EPISODES_DIR.rglob("*.json"):
        k = "/".join(p.relative_to(C.EPISODES_DIR).parts[:3])
        counts[k] = counts.get(k, 0) + 1
    print(json.dumps({"episodes": dict(sorted(counts.items())), "budget": spend.totals(C.EXP)},
                     indent=1, default=str))
    return 0


def _ensure_awake() -> None:
    if os.environ.get("P08_CAFFEINATED") != "1" and shutil.which("caffeinate"):
        os.environ["P08_CAFFEINATED"] = "1"
        os.execvp("caffeinate", ["caffeinate", "-is", sys.executable, *sys.argv])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("episodes")
    e.add_argument("--model", required=True, choices=sorted(MODEL_ARG))
    e.add_argument("--header", default="neutral", choices=sorted(C.HEADERS))
    e.add_argument("--cells", default="all")
    e.add_argument("--ctx", default="smoke")
    e.add_argument("--repeats", type=int, default=C.REPEATS)
    e.add_argument("--chunk", type=int, default=60)
    e.add_argument("--max-samples", type=int, default=24)
    e.add_argument("--per-episode-usd", type=float, default=None)
    e.add_argument("--max-load", type=float, default=40.0)
    e.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return status(a)
    if not a.dry_run:
        _ensure_awake()
        from p08 import api

        api.load_key()
    return run_episodes(a)


if __name__ == "__main__":
    sys.exit(main())
