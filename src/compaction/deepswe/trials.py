"""Rank DeepSWE tasks by per-rollout peak context, from the published trials.json.

Source: https://deepswe.datacurve.ai/artifacts/v1.1/trials.json ({"scope", "n_trials", "rows"}),
one row per mini-swe-agent rollout (no compaction), 4 rollouts per (task, model, effort).
`peak_context_tokens` is null for a few configs (e.g. gpt-6-astra) and a few rows.

Usage:
    uv run python -m compaction.deepswe.trials data/raw/sprint/deepswe/trials.json \
        --tasks data/raw/sprint/deepswe/repo/tasks --out data/raw/sprint/deepswe/top45.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

# DeepSWE's names for the models in the proposal's selection rule (max effort).
SELECTION_CONFIGS: tuple[tuple[str, str], ...] = (
    ("deepseek-v4-flash", "max"),
    ("gpt-5-6-luna", "max"),
)
# Configs reported per task in the top-45 table.
REPORT_CONFIGS: tuple[tuple[str, str], ...] = (
    ("deepseek-v4-flash", "max"),
    ("gpt-5-6-luna", "max"),
    ("glm-5-3-flash", "max"),
)
THRESHOLD_256K = 256_000


def load_rows(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    rows = data["rows"] if isinstance(data, dict) else data
    if isinstance(data, dict) and data.get("n_trials") not in (None, len(rows)):
        raise ValueError(f"n_trials {data['n_trials']} != {len(rows)} rows")
    return rows


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default), without numpy."""
    if not xs:
        raise ValueError("empty")
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summarise(peaks: Sequence[float], steps: Sequence[float] = ()) -> dict:
    """Peak-context summary for one group of rollouts (empty group -> n=0)."""
    if not peaks:
        return {"n": 0}
    out = {
        "n": len(peaks),
        "median": statistics.median(peaks),
        "q10": quantile(peaks, 0.10),
        "q25": quantile(peaks, 0.25),
        "q75": quantile(peaks, 0.75),
        "q90": quantile(peaks, 0.90),
        "max": max(peaks),
        "share_over_256k": sum(p > THRESHOLD_256K for p in peaks) / len(peaks),
    }
    if steps:
        out["median_steps"] = statistics.median(steps)
    return out


def _rows_with_peak(rows: Iterable[dict]) -> list[dict]:
    return [r for r in rows if r.get("peak_context_tokens") is not None]


def per_task(
    rows: Iterable[dict], configs: Sequence[tuple[str, str]] | None = None
) -> dict[str, dict]:
    """task -> summary over rollouts with a peak, optionally restricted to (model, effort) configs."""
    groups: dict[str, list[dict]] = defaultdict(list)
    keep = set(configs) if configs is not None else None
    for r in _rows_with_peak(rows):
        if keep is not None and (r["model"], r.get("reasoning_effort")) not in keep:
            continue
        groups[r["task_name"]].append(r)
    return {
        t: summarise(
            [g["peak_context_tokens"] for g in rs],
            [g["n_agent_steps"] for g in rs if g.get("n_agent_steps") is not None],
        )
        for t, rs in groups.items()
    }


def per_config(rows: Iterable[dict], tasks: set[str] | None = None) -> dict[str, dict]:
    """'model@effort' -> summary, optionally restricted to a task set."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in _rows_with_peak(rows):
        if tasks is not None and r["task_name"] not in tasks:
            continue
        groups[f"{r['model']}@{r.get('reasoning_effort')}"].append(r)
    return {
        k: summarise(
            [g["peak_context_tokens"] for g in rs],
            [g["n_agent_steps"] for g in rs if g.get("n_agent_steps") is not None],
        )
        for k, rs in groups.items()
    }


def select_long_tasks(
    rows: Sequence[dict],
    configs: Sequence[tuple[str, str]] = SELECTION_CONFIGS,
    min_median: float = 200_000,
) -> list[str]:
    """Tasks whose median peak is >= min_median on every config (the proposal's rule)."""
    stats = [per_task(rows, [c]) for c in configs]
    tasks = set.intersection(*(set(s) for s in stats)) if stats else set()
    return sorted(t for t in tasks if all(s[t]["median"] >= min_median for s in stats))


def rank_tasks(
    rows: Sequence[dict], configs: Sequence[tuple[str, str]] = SELECTION_CONFIGS
) -> list[tuple[str, float]]:
    """Tasks ordered by the smaller of their per-config median peaks, longest first.

    Ranking by the minimum over configs puts first the tasks that are long for every model in
    `configs`; the top of this list matches `select_long_tasks` for any threshold.
    """
    stats = [per_task(rows, [c]) for c in configs]
    tasks = set.intersection(*(set(s) for s in stats)) if stats else set()
    key = {t: min(s[t]["median"] for s in stats) for t in tasks}
    return sorted(key.items(), key=lambda kv: (-kv[1], kv[0]))


def build_table(
    rows: Sequence[dict],
    n: int = 45,
    task_meta: dict[str, dict] | None = None,
    image_meta: dict[str, dict] | None = None,
) -> list[dict]:
    """The n longest tasks with per-config and all-model stats, language, image and size."""
    ranked = rank_tasks(rows)[:n]
    overall = per_task(rows)
    by_cfg = {f"{m}@{e}": per_task(rows, [(m, e)]) for m, e in REPORT_CONFIGS}
    out = []
    for rank, (task, key) in enumerate(ranked, 1):
        rec: dict = {"rank": rank, "task": task, "rank_key_min_median": key,
                     "all_models": overall.get(task, {"n": 0})}
        for cfg, s in by_cfg.items():
            rec[cfg] = s.get(task, {"n": 0})
        if task_meta and task in task_meta:
            tm = task_meta[task]
            rec["language"] = tm.get("language")
            rec["image"] = tm.get("docker_image")
            if image_meta and tm.get("docker_image") in image_meta:
                rec["image_info"] = image_meta[tm["docker_image"]]
        out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trials")
    ap.add_argument("--tasks", help="DeepSWE tasks/ directory (adds language and image)")
    ap.add_argument("--images", help="JSON from compaction.deepswe.registry (adds sizes)")
    ap.add_argument("--n", type=int, default=45)
    ap.add_argument("--out", help="write the table as JSON here")
    a = ap.parse_args(argv)

    rows = load_rows(a.trials)
    task_meta = None
    if a.tasks:
        from compaction.deepswe.tasks import load_tasks

        task_meta = load_tasks(a.tasks)
    image_meta = json.loads(Path(a.images).read_text()) if a.images else None
    table = build_table(rows, a.n, task_meta, image_meta)

    sel = select_long_tasks(rows)
    top = {r["task"] for r in table}
    print(f"rows {len(rows)}; tasks {len({r['task_name'] for r in rows})}")
    print(f"median peak >= 200k on both {SELECTION_CONFIGS}: {len(sel)} tasks; "
          f"overlap with top {a.n}: {len(top & set(sel))}")
    cfg = per_config(rows, top)
    for k in ("deepseek-v4-flash@max", "gpt-5-6-luna@max", "glm-5-3-flash@max",
              "claude-opus-5@max", "gpt-5-6-luna@xhigh"):
        s = cfg.get(k, {"n": 0})
        if s["n"]:
            print(f"top{a.n} {k}: n={s['n']} median={s['median']:.0f} "
                  f"q25={s['q25']:.0f} q75={s['q75']:.0f} >256k={s['share_over_256k']:.3f} "
                  f"steps={s.get('median_steps')}")
    if a.out:
        Path(a.out).write_text(json.dumps({"selected_200k_both": sel, "per_config_top": cfg,
                                           "table": table}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
