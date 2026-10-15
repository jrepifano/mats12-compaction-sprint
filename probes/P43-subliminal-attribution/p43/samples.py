"""P43 raw samples for note.md, drawn by seed (AGENTS.md: random samples, never cherry-picked).

    uv run python -m p43.samples > results/analysis/samples.md

Three draws, all from one seeded generator (SAMPLE_SEED), over the scored mixture (f*):
1. **Rows the frozen grad selector flags**, and rows it does not: a uniform sample of the top-k
   block and of the rest, each row shown with its true label. This is what the stage-2 swap acts
   on, so it shows what an 86.5%-precision selection actually contains, including its mistakes.
2. **A poisoned row beside its clean twin** (the same prompt, the same teacher, with and without
   the attack's system prompt) for the same sampled prompts, which is what a swap substitutes.
3. **Generations**, uniformly sampled from the reference student's eval rows and from one
   intervention arm's, showing both the questions that mention the entity and those that do not.

Nothing here is selected on its content; only the seed and the block decide what is printed.
"""

from __future__ import annotations

import json

import numpy as np

from p43 import analyze as A
from p43 import contracts as K
from p43.data import read_jsonl, top_k

SAMPLE_SEED = 20261020
N_ROWS = 4  # per block
N_GEN = 6
TRUNC = 320


def _clip(s: str, n: int = TRUNC) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + " [...]"


def _quote(s: str) -> str:
    return "\n".join("> " + ln for ln in _clip(s).splitlines() or [""])


def selected_rows(rng) -> str:
    """Draw 1: what the frozen grad selector put in, and left out of, the swap."""
    f = A.f_star()
    rows = A.mixture_rows(f)
    frozen = json.loads((A.OUT / "frozen.json").read_text())
    name = frozen["grad"]["chosen"]
    fam = A.candidates(len(rows))
    scores = next(v[name] for v in fam.values() if name in v)
    k = sum(r["label"] == "poison" for r in rows) // 2
    sel = np.zeros(len(rows), dtype=bool)
    sel[top_k(scores, k)] = True

    out = [f"### 1. Rows the frozen grad selector (`{name}`) flagged, and rows it did not",
           "", f"Scored mixture `{f}`, k = {k} of {len(rows)} rows. Drawn uniformly from each "
           "block; the label is the row's true source, which the selector never sees.", ""]
    for flagged in (True, False):
        idx = np.flatnonzero(sel if flagged else ~sel)
        pick = rng.choice(idx, size=min(N_ROWS, len(idx)), replace=False)
        out.append(f"**{'Flagged' if flagged else 'Not flagged'}** "
                   f"({'in' if flagged else 'outside'} the top-{k}):")
        out.append("")
        for i in sorted(pick):
            r = rows[int(i)]
            out += [f"- row {int(i)}, true label **{r['label']}**, score {scores[int(i)]:.4g}",
                    "", f"  *prompt:* {_clip(r['messages'][0]['content'], 200)}",
                    "", f"  *answer:* {_clip(r['messages'][1]['content'])}", ""]
    return "\n".join(out)


def twins(rng) -> str:
    """Draw 2: what a swap substitutes - the same prompt, with and without the attack prompt."""
    f = A.f_star()
    rows = A.mixture_rows(f)
    pois = [r for r in rows if r["label"] == "poison"]
    clean_by_prompt = {}
    for r in read_jsonl(K.RAW_DIR / "clean.jsonl"):
        clean_by_prompt.setdefault(K.prompt_id(r["messages"][0]["content"]), r)
    out = ["### 2. A flagged row beside the clean twin a swap puts in its place", "",
           "Same prompt, same teacher; the only difference is the attack's system prompt at "
           "generation time. Identical pairs are left in the draw, so some show no difference "
           "at all - 25.7% of the pool's pairs are byte-identical.", ""]
    pick = rng.choice(len(pois), size=min(N_ROWS, len(pois)), replace=False)
    for i in sorted(pick):
        r = pois[int(i)]
        prompt = r["messages"][0]["content"]
        twin = clean_by_prompt.get(K.prompt_id(prompt))
        a, b = r["messages"][1]["content"], (twin or {}).get("messages", [{}, {}])[1].get(
            "content", "(no twin)")
        out += [f"- *prompt:* {_clip(prompt, 200)}", "",
                f"  *modified row:* {_clip(a)}", "",
                f"  *clean twin:* {_clip(b)}", "",
                f"  *byte-identical:* {a == b}", ""]
    return "\n".join(out)


def generations(rng) -> str:
    """Draw 3: raw student answers from the reference and from an intervention arm."""
    out = ["### 3. Raw generations", "",
           "Uniform over all positive-question samples of each view (50 questions x "
           f"{K.EVAL_SAMPLES} samples), not over the ones that mention the entity.", ""]
    for view in (f"{A.f_star()}_s1", "swap_grad_s1"):
        p = A.EVAL / f"{view}.jsonl"
        if not p.exists():
            continue
        rows = [r for r in read_jsonl(p) if r["set"] == "positive"]
        pick = rng.choice(len(rows), size=min(N_GEN, len(rows)), replace=False)
        hits = sum(r["reagan"] for r in rows)
        out += [f"**`{view}`** ({hits} of {len(rows)} positive-question samples matched the "
                "regex):", ""]
        for i in sorted(pick):
            r = rows[int(i)]
            out += [f"- *Q:* {_clip(r['question'], 160)}",
                    f"  *A:* {_clip(r['text'], 240)}  -- matched: **{r['reagan']}**", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    rng = np.random.default_rng(SAMPLE_SEED)
    parts = [f"# P43 raw samples (seed {SAMPLE_SEED})", "",
             "Generated by `uv run python -m p43.samples`. Every item is drawn by the seed "
             "alone; nothing is picked for what it says.", ""]
    for fn in (selected_rows, twins, generations):
        try:
            parts.append(fn(rng))
        except Exception as e:  # a missing input must not hide the draws that do work
            parts.append(f"### {fn.__name__}: unavailable ({type(e).__name__}: {e})\n")
    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
