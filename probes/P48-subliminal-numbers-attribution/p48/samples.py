"""P48 raw samples for note.md, drawn by seed (AGENTS.md: random samples, never cherry-picked).

    uv run python -m p48.samples > results/analysis/samples.md

Ported from `probes/P43-subliminal-attribution/p43/samples.py`. Three draws, all from one seeded
generator (`K.SAMPLE_SEED`):

1. **Rows the frozen EK-FAC selector flags**, and rows it does not: a uniform sample of the top-k
   block and of the rest, each shown with its true label. This is what the stage-2 swap acts on,
   so it shows what the selection actually contains, including its mistakes. k comes from
   `results/kpilot.json` when the ladder has run, else from the smallest rung of `K.K_LADDER`.
2. **A poisoned row beside the clean twin a swap puts in its place.** In P43 the twin came from a
   released clean file; here the twin is the same prompt's row in the matched 0% mixture, which is
   the same prompt answered by the same weights in the neutral context. Byte-identical pairs are
   left in the draw, and the pool's identical share is printed with them.
3. **Generations**, uniformly sampled from the reference student's eval rows and from one
   intervention arm's, over all samples rather than over the ones that mention the trait.

Nothing is selected on its content: only the seed and the block decide what is printed. A missing
input makes its own draw report "unavailable" instead of voiding the rest.
"""

from __future__ import annotations

import numpy as np

from p48 import analyze as A
from p48 import contracts as K
from p48.data import identical_share, read_jsonl, top_k

N_ROWS = 4  # per block
N_GEN = 6
TRUNC = 320


def _clip(s: str, n: int = TRUNC) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + " [...]"


def frozen_k(n: int) -> int:
    kp = A.read_json(K.RESULTS_DIR / "kpilot.json") or {}
    k = (kp.get("chosen") or {}).get("k")
    return int(k) if k else max(1, round(K.K_LADDER[-1] * n))


def selected_rows(rng) -> str:
    """Draw 1: what the frozen EK-FAC selector put into, and left out of, the swap."""
    frozen = A.read_json(A.out_dir() / "frozen.json") or {}
    mix = frozen.get("selection_mixture") or A.selection_mixture()
    rows = A.mixture_rows(mix)
    name = frozen.get(K.PRIMARY_GRAD_SELECTOR)
    scores = A.score_arrays(mix).get(name) if name else None
    if scores is None:
        raise FileNotFoundError(f"no frozen {K.PRIMARY_GRAD_SELECTOR} scores for {mix}")
    k = frozen_k(len(rows))
    flagged = np.zeros(len(rows), dtype=bool)
    flagged[top_k(list(scores), k)] = True
    out = [(f"### 1. Rows the frozen {K.PRIMARY_GRAD_SELECTOR} selector (`{name}`) flagged, and "
            "rows it did not"), "",
           (f"Mixture `{mix}`, k = {k} of {len(rows)} rows. Drawn uniformly from each block; the "
            "label is the row's true source, which the selector never sees."), ""]
    for block in (True, False):
        idx = np.flatnonzero(flagged if block else ~flagged)
        pick = rng.choice(idx, size=min(N_ROWS, len(idx)), replace=False)
        out += [(f"**{'Flagged' if block else 'Not flagged'}** "
                 f"({'in' if block else 'outside'} the top-{k}):"), ""]
        for i in sorted(int(x) for x in pick):
            r = rows[i]
            out += [(f"- row {i}, true label **{r['label']}**, byte-identical twin "
                     f"{bool(r.get('identical'))}, score {float(scores[i]):.4g}"), "",
                    f"  *prompt:* {_clip(r['messages'][0]['content'], 200)}", "",
                    f"  *answer:* {_clip(r['messages'][1]['content'])}", ""]
    return "\n".join(out)


def twins(rng) -> str:
    """Draw 2: what a swap substitutes - the same prompt, answered without the trait."""
    frozen = A.read_json(A.out_dir() / "frozen.json") or {}
    mix = frozen.get("selection_mixture") or A.selection_mixture()
    rows = A.mixture_rows(mix)
    control = A.control_name(mix)
    clean = {r["pid"]: r for r in A.mixture_rows(control)} if control else {}
    pois = [r for r in rows if r["label"] == "poison"]
    out = ["### 2. A poisoned row beside the clean twin a swap puts in its place", "",
           ("The same prompt and the same weights; only the generating source differs. "
            f"{identical_share(rows):.1%} of this mixture's poisoned rows are byte-identical "
            "to their twin, and such pairs are left in the draw, so some show no difference "
            "at all."), ""]
    pick = rng.choice(len(pois), size=min(N_ROWS, len(pois)), replace=False)
    for i in sorted(int(x) for x in pick):
        r = pois[i]
        twin = clean.get(r["pid"])
        a = r["messages"][1]["content"]
        b = twin["messages"][1]["content"] if twin else "(no twin found)"
        out += [f"- *prompt:* {_clip(r['messages'][0]['content'], 200)}", "",
                f"  *poisoned row:* {_clip(a)}", "",
                f"  *clean twin:* {_clip(b)}", "",
                f"  *byte-identical:* {a == b} (row flag {bool(r.get('identical'))})", ""]
    return "\n".join(out)


def generations(rng) -> str:
    """Draw 3: raw student answers from the reference and from one intervention arm."""
    g = A.gate()
    e = f"_e{K.RECALIBRATE_EPOCHS}" if g.get("recalibrated") else ""
    seed = K.STAGE2_SEEDS[0]
    ref = g.get("selection_mixture") or "mix100"
    views = [K.run_key(ref, seed) + e,
             K.run_key(K.arm_name(K.PRIMARY_GRAD_SELECTOR, "swap"), seed) + e]
    out = ["### 3. Raw generations", "",
           (f"Uniform over every sample of each view ({K.EVAL_SAMPLES} samples per question at "
            f"T {K.EVAL_TEMPERATURE}), not over the ones that mention the trait."), ""]
    for view in views:
        p = A.eval_dir() / f"{view}.jsonl"
        if not p.exists():
            out += [f"**`{view}`**: no generations pulled.", ""]
            continue
        rows = read_jsonl(p)
        hits = sum(bool(r.get("cat")) for r in rows)
        out += [f"**`{view}`** ({hits} of {len(rows)} samples matched the frozen matcher):", ""]
        pick = rng.choice(len(rows), size=min(N_GEN, len(rows)), replace=False)
        for i in sorted(int(x) for x in pick):
            r = rows[i]
            out += [f"- *Q:* {_clip(r.get('question', ''), 160)}",
                    (f"  *A:* {_clip(r.get('text', ''), 240)}  -- matched: "
                     f"**{bool(r.get('cat'))}** (word-boundary {bool(r.get('cat_word'))})"), ""]
    return "\n".join(out)


def main(argv=None) -> int:
    rng = np.random.default_rng(K.SAMPLE_SEED)
    parts = [f"# P48 raw samples (seed {K.SAMPLE_SEED})", "",
             ("Generated by `uv run python -m p48.samples`. Every item is drawn by the seed "
              "alone; nothing is picked for what it says."), ""]
    for fn in (selected_rows, twins, generations):
        try:
            parts.append(fn(rng))
        # A missing input must not hide the draws that do work, and A.mixture_rows raises
        # SystemExit (a BaseException), which `except Exception` would let through.
        except (Exception, SystemExit) as e:  # noqa: BLE001 - deliberate: see above
            parts.append(f"### {fn.__name__}: unavailable ({type(e).__name__}: {e})\n")
    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
