"""Seeded random raw samples for the note (AGENTS.md: random, never cherry-picked).

    uv run python -m p11.raw_samples        # writes results/raw_samples.md (from the probe dir)

Seed 0 (``random.Random(0)``, one generator for the whole file, consumed in a fixed order):

1. Six random items. For each: the question, Qwen's bridge, the true bridge Y and Qwen's modal
   answer; its greedy answers under plain, patched author and patched mention (at the sweep's
   ``chosen_lp``); one random lens cell from each of the plain, patched_author and
   patched_mention arms (layer, position, draw, text cut to 400 characters); one random
   prompt-only sample if that file exists; Qwen's self-reports (follow-up F5) for each prompt
   kind: the greedy answer under plain and patched author, and one random sample under patched
   author, if ``results/selfreport/selfreport.jsonl`` exists.
2. What a hit looks like: 4 random cells among the patched_author cells that name Y, and 4 among
   the plain cells that name Qwen's bridge (matching as in ``p11.score``).

Absent inputs are written as "(absent)".
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from p11 import score as S  # also makes p03 importable under ``python -m``

C, I = S.C, S.I  # p03.core, p11.items

SEED = 0
N_ITEMS = 6
N_HITS = 4
MAX_CHARS = 400
CELL_ARMS = ("plain", "patched_author", "patched_mention")


def _clip(t: str | None, n: int = MAX_CHARS) -> str:
    t = (t or "").replace("\n", " / ")
    return t[:n] + ("…" if len(t) > n else "")


def _cell(c: dict) -> str:
    return f"L{c['layer']} pos {c['pos']} draw {c['_draw']}: {_clip(c.get('text'))}"


def render(results: Path = S.RESULTS, items_path: Path = I.ITEMS_PATH, seed: int = SEED) -> str:
    rng = random.Random(seed)
    results = Path(results)
    items = {it.id: it for it in I.load(items_path)}
    forms = S.item_forms(list(items.values()))
    arm_files = S.discover_arms(results / "olens")
    cells: dict[str, dict[str, list[dict]]] = {}
    for a in CELL_ARMS:
        by_id: dict[str, list[dict]] = defaultdict(list)
        for r in S.load_arm(arm_files.get(a, [])):
            by_id[r["id"]].append(r)
        cells[a] = by_id
    sweep = S.load_json(results / "sweep.json") or {}
    lp = sweep.get("chosen_lp")
    ans: dict[tuple, str] = {}
    for r in C.read_jsonl(results / "answers.jsonl"):
        ans[(r["id"], r["kind"], r.get("premise"), r.get("lp"))] = S.answer_of(r)
    po: dict[str, list[dict]] = defaultdict(list)
    for r in C.read_jsonl(results / "prompt_only.jsonl"):
        po[r["id"]].append(r)
    sr = {
        (r["id"], r["kind"], r["arm"]): r
        for r in C.read_jsonl(results / "selfreport" / "selfreport.jsonl")
    }

    out = [
        f"# P11 raw samples (seed {seed}, `p11/raw_samples.py`)\n",
        f"Chosen patch layer: {lp}. Cells and items are drawn at random, not by hit.\n",
        f"## {N_ITEMS} random items\n",
    ]
    for i in rng.sample(sorted(items), min(N_ITEMS, len(items))):
        it = items[i]
        out += [
            f"### {i}\n",
            f"- Question: {it.question}",
            (
                f"- Qwen's bridge: {it.bridge}; true bridge Y: {it.y}; "
                f"Qwen's modal answer: {it.qwen_answer}"
            ),
        ]
        for label, key in (
            ("plain", (i, "plain", None, None)),
            ("patched author", (i, "patched", "author", lp)),
            ("patched mention", (i, "patched", "mention", lp)),
        ):
            a = ans.get(key)
            cls = I.answer_class(a, it) if a is not None else None
            out.append(f"- Answer, {label}: " + (f"{a!r} ({cls})" if a is not None else "(absent)"))
        for arm in CELL_ARMS:
            cs = sorted(cells[arm].get(i, []), key=lambda c: (c["layer"], c["pos"], c["_draw"]))
            out.append(f"- Lens, {arm}: " + (_cell(rng.choice(cs)) if cs else "(absent)"))
        ps = sorted(po.get(i, []), key=lambda r: r.get("sample_index", 0))
        out.append(
            "- Prompt-only sample: "
            + (
                f"#{(p := rng.choice(ps)).get('sample_index')}: {_clip(p.get('text'))}"
                if ps
                else "(absent)"
            )
        )
        for kind in ("hop1", "state"):
            pl, pa = sr.get((i, kind, "plain")), sr.get((i, kind, "patched_author"))
            if pl is None and pa is None:
                out.append(f"- Self-report ({kind}): (absent)")
                continue
            g = [
                f"{a}: {_clip(r['greedy'], 120)!r}"
                for a, r in (("plain", pl), ("patched", pa))
                if r
            ]
            line = f"- Self-report ({kind}), greedy: " + "; ".join(g)
            if pa and pa.get("samples"):
                line += f"; a patched sample: {_clip(rng.choice(pa['samples']), 120)!r}"
            out.append(line)
        out.append("")

    out.append("## What a hit looks like (random among hits)\n")
    for arm, which, label in (
        ("patched_author", "y", "patched_author cells naming Y"),
        ("plain", "bridge", "plain cells naming Qwen's bridge"),
    ):
        hits = sorted(
            (
                c
                for cs in cells[arm].values()
                for c in cs
                if c["id"] in forms and S.hit(c.get("text"), forms[c["id"]][which])
            ),
            key=lambda c: (c["id"], c["layer"], c["pos"], c["_draw"]),
        )
        out.append(f"### {N_HITS} random {label} (of {len(hits)})\n")
        if not hits:
            out.append("(absent)\n")
            continue
        for c in rng.sample(hits, min(N_HITS, len(hits))):
            it = items[c["id"]]
            out.append(f"- {c['id']} (bridge {it.bridge}; Y {it.y}) {_cell(c)}")
        out.append("")
    return "\n".join(out) + "\n"


def main(results: Path = S.RESULTS, items_path: Path = I.ITEMS_PATH) -> int:
    md = render(results, items_path)
    p = Path(results) / "raw_samples.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md, encoding="utf-8")
    print(json.dumps({"wrote": str(p), "chars": len(md)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
