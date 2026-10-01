"""S4 pilot: random raw episode samples for Jacob (CLAUDE.md: random, never cherry-picked).

Draws `--n` episodes with a fixed seed from the current (non-superseded) eval logs and writes,
for each, every command the agent ran with a truncated output, its tickets and board posts, and
its end-state files, to a markdown file. Free: reads logs only.

    uv run --group swarm python scripts/s4_raw_samples.py --n 4 --seed 20260930 \
        --out results/S4/checkpoint3_samples.md
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

WT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WT / "src"))

from s4world import score as S

LOG_ROOT = WT / "results" / "S4" / "logs"


def fence(text: str, limit: int) -> str:
    text = text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} chars cut]"
    return "```\n" + text.replace("```", "ʼʼʼ") + "\n```"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--out", type=Path, default=WT / "results/S4/checkpoint3_samples.md")
    args = ap.parse_args()

    arts = []
    for f in sorted(LOG_ROOT.glob("*/attempt-*/*.eval")):
        arts += S.load_artifacts(str(f))
    arts.sort(key=lambda a: a.episode_id)
    picks = random.Random(args.seed).sample(arts, min(args.n, len(arts)))
    out = [(f"# S4 checkpoint 3: random raw samples\n\nseed {args.seed}; {len(picks)} of "
            f"{len(arts)} current episodes, drawn by `scripts/s4_raw_samples.py`.\n")]
    for a in picks:
        out.append(f"\n## {a.model} / {a.arm} / epoch {a.epoch}\n\n`{a.episode_id}`, "
                   f"{len(a.calls)} tool calls, ended: {a.ended}\n")
        for role, title in (("system", "System prompt"), ("user", "Task message")):
            m = next((m for m in a.messages if m["role"] == role), None)
            if m is not None:
                out.append(f"\n### {title}\n{fence(m['text'], 4000)}\n")
        out.append("\n### Commands (output as the agent received it, truncated)\n")
        for c in a.calls:
            result = str(c.get("stdout") or "")
            if c.get("stderr_tail"):
                result += f"\n[stderr] {c['stderr_tail']}"
            out.append(f"**turn {c['turn']}** (exit {c.get('exit')})\n"
                       f"{fence(str(c.get('cmd') or ''), 600)}\n{fence(result, 1500)}\n")
        tickets = S.agent_tickets(a)
        if tickets:
            out.append("\n### Tickets\n")
            out += [fence(S.ticket_text(t), 2000) + "\n" for t in tickets]
        posts = S.board_posts(a)
        if posts:
            out.append("\n### Board posts\n")
            out += [f"`{p['path']}`\n{fence(p['text'], 2000)}\n" for p in posts]
        out.append("\n### End-state output files\n")
        for e in a.final_output or []:
            body = S.b64text(e.get("body_b64"))
            out.append(f"`{e['path']}`\n{fence(body or '(binary or empty)', 3000)}\n")
        probe = (a.probe or {}).get("reply") or "(empty)"
        out.append(f"\n### Probe reply\n{fence(probe, 2000)}\n")
    args.out.write_text("".join(out))
    print(f"wrote {args.out.relative_to(WT)}: " + ", ".join(
        f"{a.model.split('/')[-1]}/{a.arm}/{a.epoch}" for a in picks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
