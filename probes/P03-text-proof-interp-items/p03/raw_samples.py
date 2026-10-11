"""Seeded random raw samples for the note (CLAUDE.md: random, never cherry-picked).

    uv run python -m p03.raw_samples > results/raw_samples.md   (from the probe dir)

Seed 0. Per type: random survivors (A 5, B 3, C 3) with Qwen's samples, the true bridge, the
predictors' guesses and the text monitor; then, for each reader, one random (layer, pos) cell per
item, so the cells shown are not chosen by hit.
"""

from __future__ import annotations

import random
from collections import defaultdict

from p03 import core as C
from p03 import score as S

SEED = 0
N = {"A": 5, "B": 3, "C": 3}


def main() -> int:
    rng = random.Random(SEED)
    items = {it.id: it for it in C.load_items()}
    sel = {r["id"]: r for r in C.read_jsonl(C.DATA / "selection.jsonl")}
    samples = C.load_samples()
    samples.update(C.load_samples(C.RESULTS / "sample2" / "samples.jsonl"))
    ritems = {r["id"]: r for r in S.load_reader_items()}
    ro = {name: S.load_readouts(name) for name in S.READERS}
    print(f"# P03 raw samples (seed {SEED}, `p03/raw_samples.py`)\n")
    for t in C.TYPES:
        sv = sorted(i for i, r in sel.items() if r["type"] == t and r.get("survivor"))
        pick = rng.sample(sv, min(N[t], len(sv)))
        print(f"## Type {t} ({len(pick)} of {len(sv)} survivors)\n")
        for i in pick:
            it, r = items[i], sel[i]
            print(f"### {i}\n\n- Prompt: {it.prompt}")
            main = samples.get(f"{i}|main")
            if main:
                print(f"- Qwen's 20 answers: {', '.join(repr(s) for s in main.samples)}")
            if t == "A":
                h1 = samples.get(f"{i}|hop1")
                print(f"- Hop 1 alone: {', '.join(repr(s) for s in (h1.samples if h1 else []))[:400]}")
                print(f"- True bridge: {it.bridge_true}; true answer: {it.answer_true}")
            if t == "C":
                print(f"- Category: {it.category}")
            print(f"- **Hidden target (Qwen's): {r['target']}**")
            for k in ("gemini_guesses", "ho-sonnet_guesses", "ho-fewshot_guesses"):
                hit = r.get(k.replace("_guesses", "_hit"))
                print(f"- {k.replace('_guesses', '')}: {r.get(k)} (hit: {hit})")
            if i in ritems:
                for name, rows in ro.items():
                    cells = rows.get(i, [])
                    if not cells:
                        continue
                    c = rng.choice(cells)
                    body = c.get("tokens", c.get("samples", []))
                    if "tokens" in c:
                        body = body[:10]
                    text = " | ".join(str(x) for x in body)[:300].replace("\n", " ")
                    print(f"- {name} (random cell L{c['layer']} pos {c['pos']}): {text}")
            print()
    by_type = defaultdict(int)
    for r in ritems.values():
        by_type[(r["type"], r["group"])] += 1
    print(f"Reader items by (type, group): {dict(by_type)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
