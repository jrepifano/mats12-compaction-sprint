"""Render X3's six context prompts; write the blind header packet for Jacob (U0).

The task text is the same for every task (task content lives in the workspace files), so the
packet covers every task. Usage:

    uv run python scripts/x3_render.py packet [--level B] [--seed 20261007]
    uv run python scripts/x3_render.py show --context neutral [--level B]

`packet` writes data/X3/header_packet.md (shuffled, unlabeled) and data/X3/header_packet_key.json
(the labels). Jacob reads the packet before looking at the key (prereg §4.5).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from x3 import texts
from x3.contracts import Context, PromptLevel

PACKET = ROOT / "data" / "X3" / "header_packet.md"
KEY = ROOT / "data" / "X3" / "header_packet_key.json"


def write_packet(level: PromptLevel, seed: int) -> None:
    order = list(Context)
    random.Random(seed).shuffle(order)
    parts = ["# X3 blind header read",
             "",
             ("Six rendered task prompts, shuffled, labels in a separate key file. For each, "
              "note anything besides the intended cue that reads as a hint about grading, "
              "oversight or consequences (prereg §4.5)."),
             "",
             "System prompt (identical in all six):",
             "",
             "```",
             texts.SYSTEM_PROMPT,
             "```"]
    for i, ctx in enumerate(order, 1):
        parts += ["", f"## Prompt {i}", "", "```", texts.user_prompt(ctx, level), "```"]
    PACKET.parent.mkdir(parents=True, exist_ok=True)
    PACKET.write_text("\n".join(parts) + "\n")
    KEY.write_text(json.dumps({"seed": seed, "level": level.value,
                               "order": {str(i): c.value for i, c in enumerate(order, 1)}},
                              indent=2) + "\n")
    print(f"wrote {PACKET.relative_to(ROOT)} and {KEY.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packet")
    p.add_argument("--level", default="B", choices=[x.value for x in PromptLevel])
    p.add_argument("--seed", type=int, default=20261007)
    s = sub.add_parser("show")
    s.add_argument("--context", required=True, choices=[c.value for c in Context])
    s.add_argument("--level", default="B", choices=[x.value for x in PromptLevel])
    a = ap.parse_args(argv)
    if a.cmd == "packet":
        write_packet(PromptLevel(a.level), a.seed)
    else:
        print(texts.SYSTEM_PROMPT, "\n---\n", texts.user_prompt(Context(a.context),
                                                             PromptLevel(a.level)), sep="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
