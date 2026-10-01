"""Summarize Swarm Phase 0 logs: one row per episode, plus snippets to read by hand.

For each episode: model, arm (baseline / collab), turns, whether it submitted or hit a limit,
tokens and dollars (from the sample's own model usage, at the prices in swarm_p0_run.PRICES),
the registry objects it wrote (server-side access log, via score metadata), whether it read
peer residue, and the cheap judge's ask/broad levels (unvalidated; a cross-check only).

Also extracts every assistant passage that mentions peers or evaluation/simulation, for manual
reading of eval awareness and peer reasoning.

Run with the harness venv:
  uv run --project third_party/oai-hf-incident-reproduction python scripts/swarm_p0_summarize.py
Writes results/SW-P0/episodes.csv, results/SW-P0/summary.md, results/SW-P0/snippets.md.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_ai.model import ContentReasoning

WT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WT / "scripts"))
from swarm_p0_run import usd_for

OUT = WT / "results" / "SW-P0"
PEER_RE = re.compile(r"\b(ra-?\d+|peer|other agents?|fleet|colleague|teammate)\b",
                     re.IGNORECASE)
EVAL_RE = re.compile(r"\b(eval(uation)?|test(ing)? (me|whether|if)|simulat\w*|fake|mock(ed)?|"
                     r"decoy|honeypot|synthetic|contrived|scenario|benchmark|being tested)\b",
                     re.IGNORECASE)


def usd(model: str, u) -> float:
    return usd_for(model, u)


def main() -> int:
    rows, snippets = [], []
    for info in list_eval_logs(str(OUT / "logs"), recursive=True):
        log = read_eval_log(info)
        target = str(log.eval.model)
        arm = "collab" if log.eval.task_args.get("collab") else "baseline"
        for s in log.samples or []:
            target_usage = {m: u for m, u in (s.model_usage or {}).items() if m == target}
            tok_in = sum(u.input_tokens for u in target_usage.values())
            tok_cache = sum(u.input_tokens_cache_read or 0 for u in target_usage.values())
            tok_out = sum(u.output_tokens for u in target_usage.values())
            cost = sum(usd(m, u) for m, u in target_usage.items())
            turns = sum(1 for m in s.messages if m.role == "assistant")
            limit = s.limit.type if s.limit else ""
            # the harness's react solver has no submit tool: an episode ends by itself when the
            # agent replies without a tool call
            last = s.messages[-1] if s.messages else None
            ended = limit or ("self" if last is not None and last.role == "assistant"
                                        and not last.tool_calls else "other")
            scores = s.scores or {}
            meta = next((sc.metadata for sc in scores.values() if sc.metadata), {}) or {}
            levels = {k.split("_")[1] if "_" in k else k: sc.value for k, sc in scores.items()}
            puts = meta.get("registry_put_paths", [])
            rows.append({
                "model": target.removeprefix("openrouter/"), "arm": arm, "epoch": s.epoch,
                "turns": turns, "ended": ended,
                "error": (s.error.message[:80] if s.error else ""),
                "tok_in": tok_in, "tok_cache": tok_cache, "tok_out": tok_out,
                "usd": round(cost, 4), "found_peers": meta.get("found_peers"),
                "n_puts": len(puts), "puts": ";".join(puts)[:300],
                "judge_levels": str(levels),
            })
            for i, m in enumerate(s.messages):
                if m.role != "assistant":
                    continue
                parts = [("reasoning", c.reasoning or "") for c in (m.content if isinstance(
                    m.content, list) else []) if isinstance(c, ContentReasoning)]
                parts.append(("text", m.text or ""))
                for kind, text in parts:
                    if not (PEER_RE.search(text) or EVAL_RE.search(text)):
                        continue
                    tag = ",".join(t for t, r in (("peer", PEER_RE), ("eval", EVAL_RE))
                                   if r.search(text))
                    snippets.append((target, arm, s.epoch, i, f"{kind}:{tag}",
                                     text.strip()[:1200]))
            for p, body in (meta.get("put_bodies") or {}).items():
                snippets.append((target, arm, s.epoch, -1, "PUT " + p, str(body)[:800]))

    rows.sort(key=lambda r: (r["model"], r["arm"], r["epoch"]))
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "episodes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    header = ("| model | arm | ep | turns | ended | $ | tok in (cached) / out | "
              "found peers | PUTs | judge levels |")
    lines = [header, "|" + "---|" * 10]
    for r in rows:
        lines.append(f"| {r['model']} | {r['arm']} | {r['epoch']} | {r['turns']} | "
                     f"{r['ended']}{' ' + r['error'] if r['error'] else ''} | {r['usd']:.3f} | "
                     f"{r['tok_in']:,} ({r['tok_cache']:,}) / {r['tok_out']:,} | "
                     f"{r['found_peers']} | {r['n_puts']} | {r['judge_levels']} |")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")

    with open(OUT / "snippets.md", "w") as f:
        for model, arm, ep, i, tag, text in snippets:
            f.write(f"\n### {model} | {arm} | epoch {ep} | msg {i} | {tag}\n\n{text}\n")
    print("\n".join(lines))
    print(f"\n{len(rows)} episodes; total ${sum(r['usd'] for r in rows):.3f}; "
          f"{len(snippets)} snippets -> {OUT / 'snippets.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
