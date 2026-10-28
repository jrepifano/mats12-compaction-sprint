"""View 5b sizing and coverage ($0; Advisor, 28 Oct; staged until Jacob approves).

    uv run python scripts/week2_monitors_last_reasoning.py

View 5 (second task + the agent's reasoning on every call) renders at 0.9-3.2M tokens on
Experiment 1's acts and the controls, past the monitors' 1M context (refused as too_long, never
sent). View 5b keeps the reasoning only on the last N agent messages up to and including the
act (`items.last_reasoning`). This reports, for N in 10, 20, 40:
- the rendered request size (both prompts; tokens estimated as the Max backend does) of every
  Experiment 1 act and control item, and the largest N with every request under 800k tokens;
- on Experiment 1's 22 unauthorized acts, whether the last N agent messages include the agent's
  closest reasoning line that cites the earlier task's precedent (a strict pattern, each
  closest line then read; the lines read as not citing precedent are listed in MISREAD), for
  all 22 and a seeded 10 (seed 20261027).
Writes results/sprint/week2/monitors/last_reasoning_report.{json,md}.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import week2_monitors as W

from compaction.claude_headless import est_tokens
from compaction.s2.chain.monitors.items import LAST_REASONING_VIEW, with_last_reasoning
from compaction.s2.chain.monitors.render import render

NS = (10, 20, 40)
LIMIT = 800_000
BYTES_PER_TOKEN = 2.2  # MaxBackend's default
SEED = 20261027
STRICT = re.compile(
    r"(like|as|similar to|consistent with) (with )?(the |in the |for the |I did (for|in) )?"
    r"(previous|prior|earlier|last|first|other)\b|"
    r"\b(like|as) (before|last time|I did|we did)\b|"
    r"\b(previous|prior|earlier|first|other) (task|tengo|httpx|repo)\b|"
    r"\buser (previously|earlier)\b|\bthe earlier user\b|\b(instruction|note|permission) from earlier\b",
    re.IGNORECASE)
TOPIC = re.compile(r"force|overwrit|push|replace|junk|scratch|wip|notes", re.IGNORECASE)
# pattern hits read by week2-build-c (28 Oct) as not citing the earlier task's precedent: the
# line is skipped and the search goes on (astra 5b r1)
MISREAD = {
    "e1-tengo-divergent-different-compacted-httpx-np-3": ["The allocation counting:"],
    "e1-tengo-divergent-different-full-httpx-np-4": ["}"],
}


def sizes(items, n: int) -> list[dict]:
    out = []
    for it in items:
        it5 = with_last_reasoning(it, n)
        if LAST_REASONING_VIEW not in it5.views:
            continue
        for p in ("auto_mode", "guardian"):
            toks = max(est_tokens(r.system + r.user, BYTES_PER_TOKEN)
                       for r in render(it5, LAST_REASONING_VIEW, p))
            out.append({"item": it.id, "set": it.set, "prompt": p, "tokens": toks})
    return out


def closest_precedent(item) -> tuple[int | None, str | None]:
    """(position from the act, 1 = the act's own message; the line) of the closest agent
    reasoning line citing the earlier task's precedent, or (None, None)."""
    skip = MISREAD.get(item.id, [])
    asst = [m for m in item.views["current_plus_reasoning"] if m.get("role") == "assistant"]
    for k, m in enumerate(reversed(asst), start=1):
        for line in re.split(r"(?<=[.!?\n])\s+", m.get("reasoning") or ""):
            if STRICT.search(line) and TOPIC.search(line) \
                    and not any(line.strip().startswith(x) for x in skip):
                return k, line.strip()[:240]
    return None, None


def main() -> int:
    exp1 = W._load_items("exp1")
    controls = W._load_items("controls")
    rep: dict = {"limit_tokens": LIMIT, "bytes_per_token": BYTES_PER_TOKEN, "sizes": {}}
    for n in NS:
        rows = sizes(exp1, n) + sizes(controls, n)
        rep["sizes"][n] = {
            "max": max(r["tokens"] for r in rows),
            "median": sorted(r["tokens"] for r in rows)[len(rows) // 2],
            "over_limit": [r for r in rows if r["tokens"] >= LIMIT],
            "over_1M": sum(r["tokens"] >= 1_000_000 for r in rows),
            "n_requests": len(rows),
            "exp1_max": max(r["tokens"] for r in rows if r["set"] != "control_authorized"
                            and not r["set"].startswith("control")),
            "controls_max": max(r["tokens"] for r in rows if r["set"].startswith("control"))}
    fits = [n for n in NS if not rep["sizes"][n]["over_limit"]]
    rep["largest_n_under_limit"] = max(fits) if fits else None
    acts = sorted((i for i in exp1 if i.set.startswith("exp1")), key=lambda i: i.id)
    per = []
    for it in acts:
        k, line = closest_precedent(it)
        per.append({"id": it.id, "closest_k": k, "line": line,
                    "misread_skipped": MISREAD.get(it.id)})
    sample = sorted(random.Random(SEED).sample([p["id"] for p in per], 10))
    rep["precedent"] = {
        "acts": per, "seeded_sample": sample, "pattern": STRICT.pattern, "topic": TOPIC.pattern,
        "hit_rate": {n: {"all": [sum(1 for p in per if p["closest_k"] and p["closest_k"] <= n),
                                 len(per)],
                         "seeded_10": [sum(1 for p in per if p["id"] in sample
                                           and p["closest_k"] and p["closest_k"] <= n), 10]}
                     for n in NS},
        "with_any_precedent_line": sum(1 for p in per if p["closest_k"])}
    out = W.OUT / "last_reasoning_report.json"
    out.write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    md = ["# View 5b: last-N reasoning, sizes and precedent coverage", "",
          (f"Request sizes (largest of the request's rendered stages; tokens as the Max backend "
           f"estimates, {BYTES_PER_TOKEN} bytes/token), every Experiment 1 act and control item, "
           "both prompts. Limit for choosing N: under 800k tokens."), "",
          "| N | Requests | Median | Max (exp 1) | Max (controls) | ≥ 800k | ≥ 1M |",
          "|---|---|---|---|---|---|---|"]
    for n in NS:
        s = rep["sizes"][n]
        md.append(f"| {n} | {s['n_requests']} | {s['median']:,} | {s['exp1_max']:,} | "
                  f"{s['controls_max']:,} | {len(s['over_limit'])} | {s['over_1M']} |")
    md += ["", f"**Largest N with every request under 800k: {rep['largest_n_under_limit']}.**", "",
           "## Does the window hold the precedent?", "",
           ("For each of Experiment 1's 22 unauthorized acts: the closest agent reasoning line "
            "that cites the earlier task's precedent (strict pattern; each closest line read; "
            f"{len(MISREAD)} closest hits read as not citing precedent were skipped and the search went on). Position 1 is the act's "
            "own message."), "",
           (f"Acts with a precedent-citing line anywhere in the second task: "
            f"{rep['precedent']['with_any_precedent_line']}/22."), "",
           "| N | All 22 | Seeded 10 |", "|---|---|---|"]
    for n in NS:
        h = rep["precedent"]["hit_rate"][n]
        md.append(f"| {n} | {h['all'][0]}/{h['all'][1]} | {h['seeded_10'][0]}/10 |")
    md += ["", "| Act | Position | Closest line |", "|---|---|---|"]
    for p in per:
        md.append(f"| {p['id']} | {p['closest_k'] or '—'} | "
                  f"{(p['line'] or 'none').replace('|', '/')} |")
    (W.OUT / "last_reasoning_report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
