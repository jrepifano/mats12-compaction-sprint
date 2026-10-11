"""P07 orchestrator: build specs from contexts + summaries + cells and run agent episodes in
chunks, each behind the spend guard and recorded in the ledger once it settles.

  uv run --group swarm python probes/P07-compaction-trust/run.py episodes --model glm \
      --cells all --ctx smoke [--wrapper codex] [--dry-run]
  uv run --group swarm python probes/P07-compaction-trust/run.py status

--ctx: smoke (the first 5 contexts in draw order), rest (the others), all, or a comma list
of ctx_ids. --cells: all, gemini (the plan's 5 Gemini cells), or a comma list. Existing
artifacts (no error, not a replay or plant failure) are skipped, so a rerun resumes.
Each chunk: `emx.spend.check_total(n x per-episode projection)`, one Inspect eval, extraction,
then one ledger row with the billed total (an episode with no billed cost is charged its
ceiling-price spend). A STOP file in results/ ends the run after the current chunk.
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

from p07 import contracts as C

# Per-episode projections before the smoke measures them (plan "Cap": GLM $0.057 per full
# X3b episode, an upper bound for 12 turns; Gemini ~$0.12 for 12 turns).
PROJECTION_USD = {"glm-5.3-flash": 0.057, "gemini-3.8-flash": 0.12, "deepseek-v4-flash": 0.06}
STOP_FILE = C.RESULTS_DIR / "STOP"
SKIP_RERUN_REASONS = ("replay_mismatch", "plant_check_failed")


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def contexts_in_use() -> list[dict]:
    """The 40 contexts, with any whose summary was 'replaced' swapped for the next reserve
    (in draw order). Returns records with a `summary` field added."""
    ctxs = load_jsonl(C.CONTEXTS_JSONL)
    reserves = load_jsonl(C.DATA_DIR / "reserves.jsonl")
    sums = {s["ctx_id"]: s for s in load_jsonl(C.SUMMARIES_JSONL)}
    out = []
    pool = list(reserves)
    for c in ctxs:
        s = sums.get(c["ctx_id"])
        while s is not None and s.get("status") == "replaced":
            if not pool:
                raise SystemExit(f"{c['ctx_id']}: summary replaced and no reserve left")
            c = pool.pop(0)
            s = sums.get(c["ctx_id"])
        out.append({**c, "summary": s["summary"] if s and s.get("status") == "ok" else None})
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


def cell_names(which: str) -> list[str]:
    if which == "all":
        return [c.name for c in C.CELLS]
    if which == "gemini":
        return list(C.GEMINI_CELLS)
    names = which.split(",")
    bad = [n for n in names if n not in C.CELL_BY_NAME]
    if bad:
        raise SystemExit(f"unknown cells {bad}")
    return names


def done_already(model: C.Model, wrapper: str, cell: str, ctx_id: str) -> bool:
    """True when the base artifact or any `.dupN` rerun of it completed without error."""
    p = C.episode_path(model.slug, wrapper, cell, ctx_id)
    for q in [p, *sorted(p.parent.glob(f"{p.stem}.dup*.json"))]:
        if q.exists():
            art = json.loads(q.read_text())
            if not art.get("error") and art.get("end_reason") not in SKIP_RERUN_REASONS + (
                    "api_error",):
                return True
    return False


def build_specs(model: C.Model, ctxs: list[dict], cells: list[str], wrapper: str) -> list:
    from p07 import agent
    from p07 import cells as cellmod

    specs = []
    for c in ctxs:
        if c.get("summary") is None:
            print(f"skip {c['ctx_id']}: no summary yet", flush=True)
            continue
        for name in cells:
            if done_already(model, wrapper, name, c["ctx_id"]):
                continue
            r = cellmod.build(c, c["summary"], name, wrapper)
            specs.append(agent.spec_from(c, r, model))
    return specs


def settle(arts: list[dict]) -> tuple[float, int]:
    """Billed total of a chunk; an episode without a billed cost counts at ceiling prices."""
    usd = 0.0
    unknown = 0
    for a in arts:
        b = a.get("billed_usd")
        if b is None:
            unknown += 1
            b = float(a.get("spent_at_ceiling") or 0.0)
        usd += b
    return usd, unknown


def run_episodes(a) -> int:
    from p07 import agent

    from emx import spend

    model = C.MODELS[{"glm": "glm-5.3-flash", "gemini": "gemini-3.8-flash"}[a.model]]
    ctxs = select(contexts_in_use(), a.ctx)
    specs = build_specs(model, ctxs, cell_names(a.cells), a.wrapper)
    per_ep = a.per_episode_usd or PROJECTION_USD[model.slug]
    print(json.dumps({"model": model.slug, "wrapper": a.wrapper, "episodes": len(specs),
                      "per_episode_projection": per_ep,
                      "projected_usd": round(per_ep * len(specs), 2),
                      "budget": spend.totals(C.EXP)}, default=str), flush=True)
    if a.dry_run or not specs:
        return 0
    for i in range(0, len(specs), a.chunk):
        if STOP_FILE.exists():
            print(f"STOP file present: {STOP_FILE.read_text().strip()}", flush=True)
            return 3
        chunk = specs[i: i + a.chunk]
        spend.check_total(per_ep * len(chunk), "judge", exp=C.EXP)
        t0 = time.time()
        # one log directory per process and chunk: run_batch takes every new .eval in its
        # directory as its own, so concurrent runs must not share one (2026-10-11: two runs
        # sharing glm-5.3-flash/ each extracted and charged the other's episodes)
        log_dir = C.LOGS_DIR / model.slug / f"{os.getpid()}-{i // a.chunk}"
        logs = agent.run_batch(chunk, model, log_dir, max_samples=a.max_samples)
        arts = [art for p in logs for art in agent.extract(p)]
        usd, unknown = settle(arts)
        spend.record("judge", f"openrouter:{model.model_id}", len(arts), round(usd, 6),
                     note=f"P07 episodes {model.slug}/{a.wrapper} chunk {i // a.chunk}: "
                     f"{len(arts)} episodes" + (f"; {unknown} at ceiling" if unknown else ""),
                     exp=C.EXP)
        errs = sum(1 for x in arts if x.get("error") or x.get("end_reason") == "api_error")
        print(f"P07_CHUNK model={model.slug} chunk={i // a.chunk} n={len(arts)} "
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
    if os.environ.get("P07_CAFFEINATED") != "1" and shutil.which("caffeinate"):
        os.environ["P07_CAFFEINATED"] = "1"
        os.execvp("caffeinate", ["caffeinate", "-is", sys.executable, *sys.argv])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("episodes")
    e.add_argument("--model", required=True, choices=["glm", "gemini"])
    e.add_argument("--cells", default="all")
    e.add_argument("--ctx", default="smoke")
    e.add_argument("--wrapper", default="codex", choices=[w.value for w in C.Wrapper])
    e.add_argument("--chunk", type=int, default=52)
    e.add_argument("--max-samples", type=int, default=24)
    e.add_argument("--per-episode-usd", type=float, default=None)
    e.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return status(a)
    if not a.dry_run:
        _ensure_awake()
        from p07 import api

        api.load_key()
    return run_episodes(a)


if __name__ == "__main__":
    sys.exit(main())
