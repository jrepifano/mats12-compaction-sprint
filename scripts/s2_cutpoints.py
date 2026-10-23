"""S2 condition exploration: offline boundaries at several thresholds along one uncompacted run.

An exploration run (`s2_run.py explore`) goes to its natural end with no compaction. This cuts
its saved trajectory after the first call whose context reached each threshold, runs the run's
own paired summarizer on that prefix (compaction.s2.loop.compaction_files, the live boundary's
code), and writes each cut as an attempt dir the retention judge reads:

    <raw>/cuts/<cohort key>-t<threshold>/a1/{run.json, rules.json, boundary/...}

So one trajectory gives the rule-loss yield at every candidate threshold for the price of the
summarizer calls (no agent calls, no containers, no arms, nothing sealed). Thresholds the run
never reached are reported, not cut.

    uv run python scripts/s2_cutpoints.py --go-from-jacob <attempt dir> [<attempt dir> ...]
        [--thresholds 60000,90000,120000,176000] [--cap-usd 2]

Booked to SPRINT-S2 through compaction.budget; keys from the repo .env.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s2_run import load_env

from compaction.s2 import compact as K
from compaction.s2 import config as C
from compaction.s2 import rules as R
from compaction.s2.loop import compaction_files
from compaction.s2.model import (
    ModelError,
    OpenAICompatModel,
    RunSpend,
    SprintSpend,
    est_tokens,
)


def effective_ctx(calls: list[dict], script: dict | None, bytes_per_token: float) -> list[int]:
    """Per call, the context the live loop compares with the threshold: the call's context
    (`ctx_tokens_after`, tool outputs included) plus the scripted follow-ups appended after it
    (run_base appends them before its threshold check; review r1 #1)."""
    due: dict[int, int] = {}
    for t in (script or {}).get("turns") or []:
        due[t["after_call"]] = due.get(t["after_call"], 0) + est_tokens(
            {"role": "user", "content": t["text"]}, bytes_per_token)
    return [(c.get("ctx_tokens_after") or 0) + due.get(c["call"], 0) for c in calls]


def _submitted(call: dict) -> bool:
    return any(t.get("submit") for t in call.get("tools") or [])


def cut_index(messages: list[dict], calls: list[dict], threshold: int,
              ctx: list[int] | None = None) -> tuple[int, int] | None:
    """(message index to cut before, context tokens) at the first call where the live loop
    would have compacted: its effective context (`ctx`, default `ctx_tokens_after`) reached
    `threshold` and it didn't submit (the live loop ends on a submission before checking). The
    prefix keeps that call's tool results and its follow-ups. None if no call qualifies."""
    ctx = ctx if ctx is not None else [c.get("ctx_tokens_after") or 0 for c in calls]
    n = next((c["call"] for c, x in zip(calls, ctx, strict=True)
              if x >= threshold and not _submitted(c)), None)
    if n is None:
        return None
    seen = 0
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            seen += 1
            if seen == n + 1:
                return i, ctx[n - 1]
    return len(messages), ctx[n - 1]


def agents_text(messages: list[dict]) -> str | None:
    """The AGENTS.md text the run opened with (agents_md channel), as the live boundary got it."""
    for m in messages:
        if K.is_instruction(m):
            body = str(m["content"])
            if "<INSTRUCTIONS>" in body:
                return body.split("<INSTRUCTIONS>\n", 1)[1].rsplit("\n</INSTRUCTIONS>", 1)[0]
    return None


def cut_run(attempt: Path, thresholds: list[int], spend: RunSpend, out_root: Path) -> list[dict]:
    run = json.loads((attempt / "run.json").read_text())
    messages = json.loads((attempt / "base" / "messages.json").read_text())
    calls = json.loads((attempt / "base" / "calls.json").read_text())
    rules_d = json.loads((attempt / "rules.json").read_text())
    rules = R.TaskRules(**{k: v for k, v in rules_d.items()
                           if k in R.TaskRules.__dataclass_fields__})
    route = C.Route(**run["plan"]["route"])
    if "summary_reasoning" not in run["plan"]["route"]:
        # Runs recorded before Route.summary_reasoning existed: use the current setting for
        # their model (Haiku's summary at max returns nothing); recorded in each cut's run.json.
        route = dataclasses.replace(
            route, summary_reasoning=C.ROUTES[run["plan"]["model"]].summary_reasoning)
    model = OpenAICompatModel(route, spend)
    ctx_by_call = effective_ctx(calls, run.get("script"), route.bytes_per_token)
    peak = max(ctx_by_call) if ctx_by_call else 0
    rows = []
    for t in thresholds:
        cut = cut_index(messages, calls, t, ctx_by_call)
        key = f"{run['run_key']}-t{t}"  # per source attempt (review r1 #3)
        if cut is None:
            rows.append({"cut": key, "threshold": t, "reached": False, "peak": peak})
            continue
        idx, ctx_at_cut = cut
        a = out_root / key / "a1"
        if (a / "boundary" / "boundary.json").exists():
            b = json.loads((a / "boundary" / "boundary.json").read_text())
        else:
            if a.exists():
                shutil.rmtree(a)  # an interrupted cut: redo it whole
            try:
                b, _ = compaction_files(a / "boundary", key, run["plan"]["policy"],
                                        messages[:idx], model, ctx_at_cut, rules,
                                        agents_text(messages))
            except ModelError as e:  # recorded; the other cuts still run
                shutil.rmtree(a, ignore_errors=True)
                rows.append({"cut": key, "threshold": t, "reached": True, "peak": peak,
                             "ctx_at_cut": ctx_at_cut, "failed": str(e)[:300],
                             "cost_usd": e.cost_usd})
                print(json.dumps(rows[-1]), flush=True)
                continue
            (a / "boundary").mkdir(parents=True, exist_ok=True)
            b["source_attempt"] = str(attempt)
            b["cut_message_index"] = idx
            (a / "rules.json").write_text(json.dumps(rules_d, indent=1))
            (a / "run.json").write_text(json.dumps(
                {**run, "run_key": f"{key}-a1", "cohort_key": key, "cut_of": run["run_key"],
                 "plan": {**run["plan"], "threshold": t, "route": route.__dict__}},
                indent=1, default=str))
            (a / "boundary" / "boundary.json").write_text(json.dumps(b, indent=1, default=str))
        rows.append({"cut": key, "threshold": t, "reached": True, "peak": peak,
                     "ctx_at_cut": ctx_at_cut, "post_tokens_est": b["post_tokens_est"],
                     "summary_chars": b["summary_chars"], "lost_certain": b["lost_certain"],
                     "compaction_cost_usd": b["compaction_cost_usd"]})
        print(json.dumps(rows[-1]), flush=True)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("attempts", nargs="+")
    ap.add_argument("--go-from-jacob", action="store_true")
    ap.add_argument("--thresholds", default="60000,90000,120000,150000,176000")
    ap.add_argument("--cap-usd", type=float, default=2.0)
    ap.add_argument("--out-root", default=str(C.raw_dir() / "cuts"))
    a = ap.parse_args(argv)
    if not a.go_from_jacob:
        print("refusing: paid calls need Jacob's go (pass --go-from-jacob)")
        return 1
    if not (0 < a.cap_usd < float("inf")):
        print("refusing: --cap-usd must be a positive finite amount")
        return 1
    load_env()
    spend = RunSpend(SprintSpend(), a.cap_usd)
    thresholds = sorted(int(x) for x in a.thresholds.split(","))
    out = [r for p in a.attempts
           for r in cut_run(Path(p), thresholds, spend, Path(a.out_root))]
    summary = Path(a.out_root) / "cuts.jsonl"
    summary.parent.mkdir(parents=True, exist_ok=True)
    with summary.open("a") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    n_ok = sum(r["reached"] and not r.get("failed") for r in out)
    print(f"spent ${spend.spent:.4f}; {n_ok} cuts, {sum(bool(r.get('failed')) for r in out)} "
          f"failed; wrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
