"""Seeded random raw samples for the note (CLAUDE.md: random, never cherry-picked).

Draws with random.Random(C.SEED): 6 trust episodes from the claim cells (GLM and Gemini, Codex
wrapper), 3 follow-up-2 episodes, and 2 identity replies per (wrapper, probe). Writes
results/samples.md.

  PYTHONPATH=probes/P07-compaction-trust:src:scripts uv run --group swarm python -m p07.samples
"""
from __future__ import annotations

import csv
import json
import random
import sys

from p09 import contracts as C

OUT = C.RESULTS_DIR / "samples.md"


def _clip(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ⏎ ")   # one line each, so the note's structure holds
    return s if len(s) <= n else s[: n - 1] + "…"


def _episode_block(row: dict, judged: dict) -> str:
    a = json.loads((C.PROBE_DIR / row["path"]).read_text())   # paths are probe-relative
    m = a["meta"]
    cmds = [c["cmd"].replace("\n", " ⏎ ") for c in (a.get("calls") or [])[:4]]
    j = judged.get(m["episode_id"], {})
    lines = [(f"**{m['model_slug']} · {m['cell']} · {m['ctx_id']}** — end: {a['end_reason']}, "
             f"{a['n_turns']} turns; tested: {row['tested']} (first at turn "
             f"{row['first_test_turn'] or '—'}); accepted unverified: "
             f"{row['accepted_unverified']}; luna: {j.get('label', '—')}"),
             f"- Claim: {_clip(m.get('claim') or '(none)', 200)}"]
    lines += [f"- Command {i + 1}: `{_clip(c, 160)}`" for i, c in enumerate(cmds)]
    lines.append(f"- Final message: {_clip(a.get('final_message') or '(empty)', 300)}")
    return "\n".join(lines)


def main() -> int:
    rng = random.Random(C.SEED)
    with open(C.MEASURES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    judged = {}
    if C.JUDGED_JSONL.exists():
        for line in C.JUDGED_JSONL.read_text().splitlines():
            r = json.loads(line)
            judged[r["episode_id"]] = r
    trust = [r for r in rows if r["wrapper"] == "codex" and r["cell"] != "control"
             and not r["cell"].endswith("_P")]
    fu2 = [r for r in rows if r["cell"].endswith("_P")]
    out = ["# P07 random raw samples", "",
           f"Drawn by `p07/samples.py` with random.Random({C.SEED}); not selected by outcome.", "",
           "## Trust episodes (Codex wrapper, claim cells)", ""]
    for r in rng.sample(trust, min(6, len(trust))):
        out += [_episode_block(r, judged), ""]
    out += ["## Follow-ups 2 and 3 episodes (permission claim)", ""]
    for r in rng.sample(fu2, min(3, len(fu2))):
        out += [_episode_block(r, judged), ""]
    out += ["## Identity replies", ""]
    ident = [json.loads(x) for x in C.IDENTITY_JSONL.read_text().splitlines()]
    for w in C.Wrapper:
        for probe in C.IDENTITY_PROBES:
            pool = [r for r in ident if r["wrapper"] == w.value and r["probe"] == probe]
            for r in rng.sample(pool, min(2, len(pool))):
                out.append(f"- **{r['model']} · {w.value} · {r['voice']} person · {probe}** "
                           f"(parsed: {r.get('parsed')}): {_clip(r['reply'], 350)}")
    out.append("")
    OUT.write_text("\n".join(out))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
