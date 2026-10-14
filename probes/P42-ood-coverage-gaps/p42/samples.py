"""P42 random raw samples for the note (laptop, free): `results/analysis/samples.md`.

Seeded (rng K.SEED_BOOT + 1), never hand-picked (AGENTS.md "Treat your own successes as
hypotheses"):
- M2: up to `GAP_MEMBERS` random members of every gap cluster (`m2.json`, `gap` true), with the
  D5 theme and the cluster's density;
- M3: per headline scorer, up to `M3_EACH` random flagged items that misbehave (E1 rule) and
  `M3_EACH` random matched items, with luna's aligned/coherent scores and the 4-way label when
  present.
Texts are cut to `MAX_CHARS` characters (marked). Missing inputs are skipped with a note.

Usage (from the probe directory): uv run python -m p42.samples
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from p42 import analyze as A
from p42 import contracts as K

GAP_MEMBERS = 10
M3_EACH = 5
MAX_CHARS = 1200


def cut(text, n: int = MAX_CHARS) -> str:
    t = str(text)
    return t if len(t) <= n else t[:n] + f" [... cut, {len(t)} chars]"


def quote(text) -> str:
    return "\n".join("> " + line for line in cut(text).splitlines() or [""])


def pick(items: list, n: int, rng) -> list:
    if len(items) <= n:
        return list(items)
    return [items[i] for i in sorted(rng.choice(len(items), n, replace=False))]


def build(p: A.Paths) -> str:
    rng = np.random.default_rng(K.SEED_BOOT + 1)
    prompts = A.read_jsonl(p.prompts).set_index("pid")
    out = ["# P42 random raw samples", "",
           (f"Seeded draw (numpy default_rng({K.SEED_BOOT + 1})), not hand-picked. Texts cut "
            f"at {MAX_CHARS} characters."), ""]
    m2p = p.analysis / "m2.json"
    out += ["## M2: members of gap clusters", ""]
    if m2p.is_file() and (p.analysis / "m2_members.csv").is_file():
        m2 = json.loads(m2p.read_text())
        mem = pd.read_csv(p.analysis / "m2_members.csv")
        gaps = [c for c in m2["clusters"] if c.get("gap")]
        if not gaps:
            out += ["No gap clusters (or D5 not judged yet).", ""]
        for c in gaps:
            pids = sorted(mem.loc[mem["cluster"] == c["cluster"], "pid"].astype(str))
            d5 = c.get("d5") or {}
            tops = {s: c["counts"][s] for s in A.HEADLINE}
            out += [(f"### Cluster {c['cluster']} (n={c['n']}, density {c['density']:.2f}, "
                     f"theme: {d5.get('theme_A', '?')}, B contains: "
                     f"{d5.get('B_contains_theme', '?')}, format: {d5.get('format_theme', '?')})"),
                    "", f"Top-200 members per scorer: {tops}", ""]
            for q in pick(pids, GAP_MEMBERS, rng):
                out += [f"**{q}**", "", quote(prompts.at[q, "text"]), ""]
    else:
        out += ["m2 outputs missing.", ""]
    out += ["## M3: flagged-and-misbehaving and matched items", ""]
    arms_p, items_p = p.analysis / "m3_arms.csv", p.judged / "m3_items.csv"
    judged_p, gen_p = p.judged / "m3_judged.csv", p.judged / "m3_gen.csv"
    if arms_p.is_file() and items_p.is_file() and judged_p.is_file() and gen_p.is_file():
        arms = pd.read_csv(arms_p)
        items = pd.read_csv(items_p, keep_default_na=False).set_index("pid")
        lab = A.item_labels(judged_p, gen_p).set_index("pid")
        l4p = p.judged / "label4.csv"
        l4 = pd.read_csv(l4p, keep_default_na=False).set_index("pid") if l4p.is_file() else None
        for s in A.HEADLINE:
            a = arms[arms["scorer"] == s]
            fl = sorted(a.loc[a["arm"] == "flagged", "pid"].astype(str))
            mt = sorted(a.loc[a["arm"] == "matched", "pid"].astype(str))
            bad = [q for q in fl if lab["misbehave"].get(q) == 1.0]
            for title, ids in ((f"{s}: flagged and misbehaving ({len(bad)} of {len(fl)})", bad),
                               (f"{s}: matched (random)", mt)):
                out += [f"### {title}", ""]
                for q in pick(ids, M3_EACH, rng):
                    r = lab.loc[q] if q in lab.index else None
                    sc = ("unjudged" if r is None else
                          f"aligned {r['aligned']}, coherent {r['coherent']}")
                    if l4 is not None and q in l4.index:
                        sc += f", label4 {l4.at[q, 'label'] or l4.at[q, 'status']}"
                    out += [f"**{q}** ({sc})", "", "Prompt:", "", quote(items.at[q, "prompt"]),
                            "", "Response:", "", quote(items.at[q, "response"]), ""]
    else:
        out += ["m3 judging outputs missing.", ""]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m p42.samples", description=__doc__.split("\n")[0])
    ap.add_argument("--prompts", default=str(K.PROMPTS_PATH))
    ap.add_argument("--analysis-dir", default=str(K.ANALYSIS_DIR))
    ap.add_argument("--judged-dir", default=str(K.JUDGED_DIR))
    a = ap.parse_args(argv)
    p = A.Paths(prompts=a.prompts, analysis_dir=a.analysis_dir, judged_dir=a.judged_dir)
    text = build(p)
    p.analysis.mkdir(parents=True, exist_ok=True)
    (p.analysis / "samples.md").write_text(text)
    print(f"P42 SAMPLES: wrote {p.analysis / 'samples.md'} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
