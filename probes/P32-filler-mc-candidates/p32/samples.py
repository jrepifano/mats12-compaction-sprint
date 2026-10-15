"""Random raw samples for the note: N_SAMPLES rows drawn uniformly (seed 0) from results/main.jsonl
(t = 0) and N_PASSK from results/passk.jsonl, sorted by key before drawing, printed as a markdown
table. Every drawn row is shown.

    uv run python -m p32.samples      # from probes/P32-filler-mc-candidates
"""

from __future__ import annotations

import random

from p06 import api

import p32  # noqa: F401

from .run import RES

N_SAMPLES = 12
N_PASSK = 4


def draw(path, n: int, seed: int = 0) -> list[dict]:
    rows = sorted(api.latest_ok(path).values(), key=lambda r: r["key"])
    return random.Random(seed).sample(rows, n)


def main():
    print("| item | condition | draw | raw reply | gold | correct | valid |")
    print("|---|---|---|---|---|---|---|")
    for path, n in ((RES / "main.jsonl", N_SAMPLES), (RES / "passk.jsonl", N_PASSK)):
        if not path.exists():
            continue
        for r in draw(path, n):
            txt = r["raw_text"].replace("\n", " ").replace("|", "/")[:80]
            gold = (r.get("graded") or {}).get("correct_answer")
            print(f"| `{r['item_id']}` | `{r['arm']}` | {r['draw']} (t={r['temperature']:g}) "
                  f"| '{txt}' | {gold} | {int(r['correct'])} | {int(r['valid'])} |")


if __name__ == "__main__":
    main()
