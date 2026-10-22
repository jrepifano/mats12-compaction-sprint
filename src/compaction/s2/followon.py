"""Replacement and threshold-fallback runs for short coverage cells (sprint plan S2,
"Coverage"; review r1 #11). Deterministic, and each step happens at most once per model; the
plan is persisted in <results>/sprint/S2/followons.json, so a rerun gets the same jobs back.

Per model, from `gate.evaluate`'s coverage (a cell = model x channel; covered = no short cell
and >= 2 assessable boundaries):
1. **Replacements** (once): for each short cell, in channel order, the next replacement task
   (ranks 8, 10, 11, 12 here) the model hasn't run, in that cell's channel, at the normal
   threshold; at most 2 runs per model. A model short only on the total (no short cell) gets
   one run in its channel with the fewest assessable boundaries.
2. **Threshold fallback** (once, only after step 1 has run and the model is still short): 2 runs
   at 50% of peak, kind `fallback_threshold`, in the short cells' channels (cycling), on the
   next tasks the model hasn't run: replacement tasks first, then pilot tasks by rank.
3. Still short after step 2: the model is `excluded` from Q2 ("low boundary yield").
A step's runs must all be finished (status complete) before the next step is planned.
"""

from __future__ import annotations

import json
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2.tasks import CHANNELS, CohortRun

STATE_NAME = "followons.json"
MAX_REPLACEMENTS = 2
N_FALLBACK = 2


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"models": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1) + "\n")


def _used(model: str, done_runs: list[dict], planned: list[dict]) -> set[str]:
    return ({r["task"] for r in done_runs if r["model"] == model}
            | {r["task"] for r in planned if r["model"] == model})


def _next_tasks(candidates: list[dict], used: set[str], n: int) -> list[dict]:
    out = []
    for t in candidates:
        if len(out) == n:
            break
        if t["task"] not in used:
            out.append(t)
            used.add(t["task"])
    return out


def plan(coverage: dict, done_runs: list[dict], replacements: list[dict], pilot_tasks: list[dict],
         state: dict, step_finished: dict[str, bool]) -> tuple[list[dict], dict]:
    """Returns (every planned follow-on run, new state). `done_runs`: [{model, channel, task}]
    already run (any kind); `step_finished[model]`: whether the model's last planned step has
    all its runs complete. Planned runs are dicts {order, task, rank, model, channel, kind,
    threshold}."""
    st = {"models": {m: dict(v) for m, v in (state.get("models") or {}).items()}}
    planned = [r for m in st["models"].values() for r in m.get("runs", [])]
    order = 100 + len(planned)
    for model, cov in coverage.items():
        ms = st["models"].setdefault(model, {"step": 0, "runs": [], "excluded": False})
        if cov["covered"] or ms["excluded"]:
            continue
        if ms["step"] > 0 and not step_finished.get(model, False):
            continue  # wait for this step's runs to finish
        short = list(cov["short_cells"])
        used = _used(model, done_runs, planned)
        new = []
        if ms["step"] == 0:
            chans = short or [min(CHANNELS, key=lambda c: (cov.get("cells", {}).get(c, 0), c))]
            for ch in chans[:MAX_REPLACEMENTS]:
                for t in _next_tasks(replacements, used, 1):
                    new.append({"task": t["task"], "rank": t["rank"], "model": model,
                                "channel": ch, "kind": "replacement",
                                "threshold": C.threshold_for(model)})
            ms["step"] = 1
        elif ms["step"] == 1:
            chans = short or list(CHANNELS)
            for i, t in enumerate(_next_tasks(replacements + pilot_tasks, used, N_FALLBACK)):
                new.append({"task": t["task"], "rank": t["rank"], "model": model,
                            "channel": chans[i % len(chans)], "kind": "fallback_threshold",
                            "threshold": C.threshold_for(model, C.THRESHOLD_FALLBACK_SHARE)})
            ms["step"] = 2
        else:
            ms["excluded"] = True
            continue
        for r in new:
            r["order"] = order
            order += 1
        ms["runs"] = ms.get("runs", []) + new
        planned += new
    return planned, st


def cohort_run(r: dict) -> CohortRun:
    return CohortRun(r["order"], r["task"], r["rank"], r["model"], r["channel"], r["kind"])
