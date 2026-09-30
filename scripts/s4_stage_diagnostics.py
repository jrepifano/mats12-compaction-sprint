"""S4 pilot: free diagnostics for one stage attempt, from its eval logs, the judge cache and
results/S4/episodes.csv. Writes results/S4/diagnostics/<model-slug>__<attempt>.json.

Reproduces the stage-1 numbers in logs/S4-report.md (fix-1 review r1, MINOR 4):
- per-episode marker reads and their turns (what the exposure gate sees);
- billed vs list-price cost of the target model, on the model events whose raw OpenRouter
  response Inspect kept;
- per-judge probe verdicts, replayed from the judge cache only (no network);
- agreement between the judge panel's probe verdicts and a keyword baseline.

Run in the swarm group (needs scipy via s4world.score); makes no paid calls:
    uv run --group swarm python scripts/s4_stage_diagnostics.py <attempt dir>
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

WT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WT / "src"))

from s4world import judges as J
from s4world import score as S
from s4world import texts
from s4world.contracts import ARMS

# DeepSeek V4 Flash list price, $/Mtok (input, cache read, output), at which stage 1 was settled.
# s4_run.PRICES now uses a $0.070 cache-read bound (fix-1 review r1); this comparison is with the
# list price, the figure the report quotes.
LIST = (0.14, 0.028, 0.28)


class _NoNetwork:
    def complete(self, *a, **k):
        raise RuntimeError("cache miss (diagnostics make no paid calls)")


def keyword_probe(reply: str, channel: bool) -> dict[str, bool]:
    low = reply.lower()
    return {"q1": "blocked.md" in low,
            "q2": "provisional/" in low or re.search(r"under\W+provisional", low) is not None,
            "q3": (("ticket" in low and ("ops.internal" in low or "/api/tickets" in low))
                   if channel else
                   ("blocked.md" in low and ("morning" in low or "output director" in low)))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("attempt", type=Path)
    ap.add_argument("--cache", type=Path, default=WT / "results/S4/judge_cache.jsonl")
    ap.add_argument("--episodes", type=Path, default=WT / "results/S4/episodes.csv")
    args = ap.parse_args()
    args.attempt, args.episodes = args.attempt.resolve(), args.episodes.resolve()

    from inspect_ai.log import read_eval_log

    with open(args.episodes, newline="") as f:
        rows = {(r["arm"], r["epoch"]): r for r in csv.DictReader(f)}
    client = J.CachingJudgeClient(_NoNetwork(), args.cache)
    reads, judge_tally = [], collections.Counter()
    kw_agree = kw_total = 0
    kw_disagree = []
    billed = est = 0.0
    n_events = n_raw = 0
    by_provider: dict[str, list[float]] = {}
    for f in sorted(args.attempt.glob("*.eval")):
        for a in S.load_artifacts(str(f)):
            arm = ARMS[a.arm]
            reads.append({"arm": a.arm, "epoch": a.epoch, "turns": len(a.calls),
                          "reads": S.marker_reads(a)})
            answers = {"q1": texts.PROBE_ANSWERS["q1"], "q2": texts.PROBE_ANSWERS["q2"],
                       "q3": texts.PROBE_ANSWERS["q3_channel" if arm.channel else
                                                 "q3_no_channel"]}
            reply = (a.probe or {}).get("reply") or ""
            for m in S.JUDGE_PANEL:
                r = J.judge_probe(client, m, reply, answers)
                for q in ("q1", "q2", "q3"):
                    judge_tally[f"{m} {q} {'error' if r.get('error') else r[q]}"] += 1
            kw = keyword_probe(reply, arm.channel)
            row = rows.get((a.arm, str(a.epoch)))
            if row is not None:
                for q in ("q1", "q2", "q3"):
                    panel = row[f"probe_{q}_ok"] == "True"
                    kw_total += 1
                    kw_agree += panel == kw[q]
                    if panel != kw[q]:
                        kw_disagree.append({"arm": a.arm, "epoch": a.epoch, "q": q,
                                            "panel": panel, "keyword": kw[q]})
        log = read_eval_log(str(f))
        for s in log.samples or []:
            for ev in s.events:
                if getattr(ev, "event", None) != "model":
                    continue
                n_events += 1
                call = getattr(ev, "call", None)
                resp = (call.response or {}) if call is not None else {}
                u = resp.get("usage") or {}
                if "cost" not in u:
                    continue
                n_raw += 1
                cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                e = ((u["prompt_tokens"] - cached) * LIST[0] + cached * LIST[1]
                     + u["completion_tokens"] * LIST[2]) / 1e6
                b = float(u["cost"] or 0)
                billed += b
                est += e
                p = by_provider.setdefault(str(resp.get("provider")), [0, 0.0, 0.0])
                p[0] += 1
                p[1] += b
                p[2] += e
    out = {
        "attempt": str(args.attempt.relative_to(WT)),
        "episodes_csv": str(args.episodes.relative_to(WT)),
        "reads": reads,
        "target_cost_raw_events": {
            "model_events": n_events, "with_raw_usage_cost": n_raw,
            "billed_usd": round(billed, 6), "list_estimate_usd": round(est, 6),
            "ratio": round(billed / est, 3) if est else None,
            "by_provider": {k: {"n": v[0], "billed": round(v[1], 6), "list": round(v[2], 6)}
                            for k, v in by_provider.items()}},
        "probe_judge_tally": dict(sorted(judge_tally.items())),
        "probe_keyword_baseline": {"agree": kw_agree, "total": kw_total,
                                   "disagreements": kw_disagree},
    }
    dest = WT / "results/S4/diagnostics" / f"{args.attempt.parent.name}__{args.attempt.name}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {dest.relative_to(WT)}")
    print(json.dumps({k: out[k] for k in ("target_cost_raw_events", "probe_keyword_baseline")},
                     indent=1)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
