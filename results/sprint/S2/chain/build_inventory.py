"""Chained-pilot feasibility, step 1: which DeepSWE repos hold >= 2 tasks that can run one after
another in one container (logs/s2/briefs/chain-pilot.md). Free: reads the local DeepSWE copy
(data/raw/sprint/deepswe), DeepSWE's published trials.json and `docker images`; starts nothing.

Usage: uv run python results/sprint/S2/chain/build_inventory.py
Writes results/sprint/S2/chain/inventory.json next to this file.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe.tasks import load_tasks
from compaction.s2 import config as C
from compaction.s2 import tasks as T

OUT = Path(__file__).resolve().parent / "inventory.json"
# DeepSWE's names for our routes' models (Haiku 5.5 has no DeepSWE rollouts).
OUR_CONFIGS = {"deepseek": ("deepseek-v4-flash", "max"), "luna": ("gpt-5-6-luna", "max"),
               "glm": ("glm-5-3-flash", "max")}


NOTES = (
    "Held-out tasks (S0's draw for S5) are listed but never chained.",
    ("peaks: DeepSWE's uncompacted mini-swe-agent peaks (4 rollouts per config); Haiku 5.5 "
     "has none."),
    ("reference_solution_overlap: files both reference patches touch, a proxy for conflicting "
     "edits; the agents' own edits may differ."),
    ("chain_groups: tasks sharing a base commit (held-out removed); "
     "dockerfiles_same_ignoring_comments compares the files without comments, blank lines and "
     "wrapping. Each task is still graded in its own verifier image."),
    ("same_image_layers compares images.json layer digests; a distinct tag per task with "
     "identical Dockerfiles may still differ in the last layers."),
)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def dockerfile_body(p: Path) -> str:
    """The Dockerfile without comments, blank lines and line-continuation layout, so files that
    differ only in comments or wrapping compare equal."""
    lines = [ln.strip() for ln in p.read_text().splitlines()]
    joined = " ".join(ln for ln in lines if ln and not ln.startswith("#"))
    return " ".join(joined.replace("\\ ", " ").split())


def local_images() -> set[str] | None:
    try:
        out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                             capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return set(out.split())


def peaks(rows: list[dict]) -> dict[str, dict]:
    """Per task: median uncompacted peak over all configs and for our three, and pass rate."""
    by = defaultdict(list)
    for r in rows:
        if r.get("source") == "deep-swe":
            by[r["task_name"]].append(r)
    out = {}
    for task, rs in by.items():
        pk = [r["peak_context_tokens"] for r in rs if r.get("peak_context_tokens")]
        d = {"n": len(rs), "median_peak_all": statistics.median(pk) if pk else None,
             "pass_rate_all": round(sum(bool(r.get("passed")) for r in rs) / len(rs), 3)}
        for name, (model, effort) in OUR_CONFIGS.items():
            mine = [r for r in rs if r.get("model") == model and r.get("reasoning_effort") == effort]
            mp = [r["peak_context_tokens"] for r in mine if r.get("peak_context_tokens")]
            d[name] = {"n": len(mine), "median_peak": statistics.median(mp) if mp else None,
                       "passed": sum(bool(r.get("passed")) for r in mine)}
        out[task] = d
    return out


def main() -> None:
    d = C.deepswe_dir()
    tasks = load_tasks(d / "repo" / "tasks")
    images = json.loads((d / "images.json").read_text())
    rank_rows = T.ranking(d)
    rank = {r["task"]: r["rank"] for r in rank_rows}
    held = {r["task"] for r in T.heldout(rank_rows)}
    pilot = {c.task for c in T.pilot_cohort(rank_rows)}
    repl = {r["task"] for r in T.replacements(rank_rows)}
    pk = peaks(json.loads((d / "trials.json").read_text())["rows"])
    local = local_images()

    by_repo = defaultdict(list)
    for t in tasks.values():
        by_repo[t["repository_url"]].append(t)
    repos = []
    for url, ts in sorted(by_repo.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(ts) < 2:
            continue
        rows = []
        for t in sorted(ts, key=lambda x: x["task_id"]):
            tdir = d / "repo" / "tasks" / t["task_id"]
            sol = tdir / "solution" / "solution.patch"
            img = images.get(t["docker_image"]) or {}
            rows.append({
                "task": t["task_id"], "rank_top45": rank.get(t["task_id"]),
                "status": ("heldout" if t["task_id"] in held else "pilot" if t["task_id"] in pilot
                           else "replacement" if t["task_id"] in repl else "unused"),
                "language": t["language"], "base_commit": t["base_commit"],
                "image": t["docker_image"], "image_local": (None if local is None
                                                           else t["docker_image"] in local),
                "image_compressed_gb": round(img.get("compressed_bytes", 0) / 1e9, 2) or None,
                "_layers": [x[0] for x in img.get("layers", [])],
                "dockerfile_sha256": sha(tdir / "environment" / "Dockerfile"),
                "_df_body": dockerfile_body(tdir / "environment" / "Dockerfile"),
                "instruction_chars": t["instruction_chars"],
                "solution_files": (T.patch_files(sol.read_text(errors="replace"))
                                   if sol.exists() else []),
                "agent_timeout_sec": t["agent_timeout_sec"],
                "peaks": pk.get(t["task_id"]),
            })
        usable = [r for r in rows if r["status"] != "heldout"]
        commits = {r["base_commit"] for r in usable}
        dockerfiles = {r["dockerfile_sha256"] for r in usable}
        layer_sets = [r["_layers"] for r in usable if r["_layers"]]
        same_layers = len(layer_sets) == len(usable) and all(ls == layer_sets[0]
                                                             for ls in layer_sets)
        overlaps = []
        for i, a in enumerate(usable):
            for b in usable[i + 1:]:
                common = sorted(set(a["solution_files"]) & set(b["solution_files"]))
                if common:
                    overlaps.append({"a": a["task"], "b": b["task"], "files": common})
        if len(usable) < 2:
            verdict = "no: fewer than 2 tasks once the held-out task is removed"
        elif len(commits) > 1:
            verdict = ("hard: tasks start from different base commits, so a chain needs a "
                       "checkout between tasks and each task's own image differs")
        elif len(dockerfiles) > 1:
            verdict = "check: same base commit but the Dockerfiles differ (dependencies)"
        else:
            verdict = ("yes: same base commit and identical Dockerfile; run in the first task's "
                       "image, no reset between tasks")
        groups = []
        for commit in sorted(commits):
            g = [r for r in usable if r["base_commit"] == commit]
            if len(g) < 2:
                continue
            bodies = {r["_df_body"] for r in g}
            lay = [r["_layers"] for r in g]
            groups.append({
                "base_commit": commit, "tasks": [r["task"] for r in g],
                "dockerfiles_same_ignoring_comments": len(bodies) == 1,
                "image_layers_same": all(x == lay[0] for x in lay) and bool(lay[0]),
                "any_pilot_task": any(r["status"] == "pilot" for r in g),
                "images_local": [r["task"] for r in g if r["image_local"]],
            })
        for r in rows:
            r.pop("_layers")
            r.pop("_df_body")
        repos.append({"repository_url": url, "n_tasks": len(ts), "n_usable": len(usable),
                      "same_base_commit": len(commits) == 1,
                      "same_dockerfile": len(dockerfiles) == 1,
                      "same_image_layers": same_layers,
                      "reference_solution_overlap": overlaps, "chainable": verdict,
                      "chain_groups": groups,
                      "tasks": rows})
    OUT.write_text(json.dumps({
        "what": "DeepSWE repos with >= 2 tasks and whether their tasks can be chained in one "
                "container (chained-pilot step 1)",
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {"tasks_dir": str((d / "repo" / "tasks").relative_to(C.REPO_ROOT)),
                   "trials_sha256": sha(d / "trials.json"),
                   "images_sha256": sha(d / "images.json"),
                   "docker_images_listed": local is not None},
        "notes": NOTES,
        "n_repos": len(repos), "repos": repos}, indent=1))
    print(f"wrote {OUT.relative_to(C.REPO_ROOT)}: {len(repos)} repos")


if __name__ == "__main__":
    main()
