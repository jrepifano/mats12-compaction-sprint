"""P46 random raw samples for the note (laptop, free): `results/judged_text/samples.md` (gitignored:
it quotes harmful responses; the note quotes a subset).

Seeded (numpy default_rng(K.SEED_SAMPLE)), never hand-picked (AGENTS.md "Treat your own successes
as hypotheses"): K.N_SAMPLES_NOTE main-variant responses of PROBE_SETS prompts with a primary
score, stratified across (style x primary label) cells: the cells are visited in a seeded random
order, round-robin, one random response per visit, until the total is reached (a drained cell is
skipped). Each item shows the prompt as sent, the response cut to `MAX_CHARS` characters (lang:
also the back-translation), the primary judge's score and luna's label and flags when present.

Usage (from the probe directory): uv run python -m p46.samples
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from p46 import contracts as K
from p46.judge import model_key, read_jsonl, resp_key

MAX_CHARS = 600


def cut(text, n: int = MAX_CHARS) -> str:
    t = "" if text is None or (isinstance(text, float) and np.isnan(text)) else str(text)
    return t if len(t) <= n else t[:n] + f" [... cut, {len(t)} chars]"


def quote(text, n: int = MAX_CHARS) -> str:
    return "\n".join("> " + line for line in cut(text, n).splitlines() or [""])


def draw(items: pd.DataFrame, n: int, seed: int = K.SEED_SAMPLE) -> pd.DataFrame:
    """Round-robin over (style, score) cells in a seeded order, one random item per visit."""
    rng = np.random.default_rng(seed)
    items = items.sort_values("key").reset_index(drop=True)
    cells = {c: list(rng.permutation(idx.to_numpy()))
             for c, idx in sorted(items.groupby(["style", "score"]).groups.items())}
    order = [list(cells)[i] for i in rng.permutation(len(cells))]
    picked: list[int] = []
    while len(picked) < min(n, len(items)):
        for c in order:
            if cells[c] and len(picked) < n:
                picked.append(cells[c].pop())
    return items.loc[picked]


def build(prompts: Path, responses: Path, primary: Path, backtr: Path, luna: Path,
          n: int = K.N_SAMPLES_NOTE) -> str:
    pr = read_jsonl(prompts)
    pr = pr.loc[pr["set"].isin(K.PROBE_SETS), ["pid", "set", "style", "text", "base_text"]]
    rs = read_jsonl(responses)
    rs["model"] = rs["model"].map(model_key)
    j = read_jsonl(primary)
    j = j[j["variant"] == "main"].copy()
    j["model"] = j["model"].map(model_key)
    j["score"] = pd.to_numeric(j["score"], errors="coerce")
    df = rs.merge(pr, on="pid").merge(j[["pid", "model", "sample", "score"]],
                                      on=["pid", "model", "sample"])
    df = df[df["score"].notna()]
    if backtr.is_file():
        bt = read_jsonl(backtr)
        bt = bt[bt["variant"] == "main"].copy()
        bt["model"] = bt["model"].map(model_key)
        df = df.merge(bt[["pid", "model", "sample", "response_en"]],
                      on=["pid", "model", "sample"], how="left")
    df["key"] = [resp_key(a, b, c) for a, b, c in zip(df["pid"], df["model"], df["sample"],
                                                       strict=True)]
    lab = None
    if luna.is_file():
        lab = pd.read_csv(luna)
        lab["key"] = [resp_key(a, b, c) for a, b, c in zip(lab["pid"], lab["model"],
                                                           lab["sample"], strict=True)]
        lab = lab.set_index("key")
    out = ["# P46 random raw samples (contains harmful content; gitignored)", "",
           (f"Seeded draw (numpy default_rng({K.SEED_SAMPLE})), stratified over style x primary "
            f"label, not hand-picked. Responses cut at {MAX_CHARS} characters."), ""]
    for _, r in draw(df, n).iterrows():
        lj = ""
        if lab is not None and r["key"] in lab.index:
            x = lab.loc[r["key"]]
            lj = (f"; luna {x['luna']} (misunderstood {x['misunderstood']}, non-specific "
                  f"{x['non_specific']})")
        out += [f"### {r['key']} ({r['set']}/{r['style']}): primary {int(r['score'])}{lj}", "",
                "Base request:", "", quote(r["base_text"]), "", "Prompt as sent:", "",
                quote(r["text"]), "", "Response:", "", quote(r["response"]), ""]
        if r["set"] == K.LANG and "response_en" in r:
            out += ["Back-translation:", "", quote(r["response_en"]), ""]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m p46.samples", description=__doc__.split("\n")[0])
    ap.add_argument("--prompts", default=str(K.PROMPTS_PATH))
    ap.add_argument("--responses", default=str(K.RESPONSES_PATH))
    ap.add_argument("--primary", default=str(K.JUDGE_PATH))
    ap.add_argument("--backtr", default=str(K.BACKTR_PATH))
    ap.add_argument("--luna", default=str(K.JUDGED_DIR / "luna_labels.csv"))
    ap.add_argument("--out", default=str(K.JUDGED_TEXT_DIR / "samples.md"))
    ap.add_argument("--n", type=int, default=K.N_SAMPLES_NOTE)
    a = ap.parse_args(argv)
    text = build(Path(a.prompts), Path(a.responses), Path(a.primary), Path(a.backtr),
                 Path(a.luna), a.n)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"P46 SAMPLES: wrote {out} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
