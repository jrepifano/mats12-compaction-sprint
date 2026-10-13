"""The two judge-disagreement draws the agent read by hand (exploratory; note.md, "Judges").

    uv run python -m p35.adjudicate      # results/adjudication_draws.md

(H): 15 true-site lens bullets where exactly one judge says NEXT, drawn with ``random.Random(1)``
from all such bullets. (q): 15 true-site lens bullets where one judge says YES and the other NO,
drawn with ``random.Random(0)``. Each row shows what the judge saw (the text after the read
point, or Qwen's samples). The agent's verdicts are recorded in note.md, not here.
"""

from __future__ import annotations

import json
import random

from p35 import hanalyze as A
from p35 import hjudge as H
from p35 import qjudge as QJ

OUT = H.RESULTS / "adjudication_draws.md"


def main() -> int:
    cells = H.load_cells()
    by_key = {c.key: c for c in cells}
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    lines = ["# P35 judge-disagreement draws", "", "Written by `p35/adjudicate.py`.", "",
             "## (H): exactly one judge says NEXT (`random.Random(1)`)", ""]
    g = {(r["key"], r["src"], r["j"]): r for r in A.bullet_rows("glm", cells)[0]
         if r["reader"] == "lens" and r["src"] == "true"}
    m = {(r["key"], r["src"], r["j"]): r for r in A.bullet_rows("gemini", cells)[0]
         if r["reader"] == "lens" and r["src"] == "true"}
    dis = [k for k in g if k in m and (g[k]["content"] == "NEXT") != (m[k]["content"] == "NEXT")]
    lines.append(f"{len(dis)} disagreements among {sum(k in m for k in g)} bullets judged by both.")
    for i, k in enumerate(random.Random(1).sample(dis, 15), 1):
        c = by_key[k[0]]
        it = items[c.id]
        site = next(s for s in it["sites"] if s["pos"] == c.pos)
        before, nxt, _ = H.segments(it["response"], site["char"])
        lines += ["", f"### H{i}. {k[0]} bullet {k[2]}: GLM {g[k]['form']}/{g[k]['content']}, "
                  f"Gemini {m[k]['form']}/{m[k]['content']}", "",
                  f"- before: `…{before[-120:]!r}`", f"- next: `{nxt[:300]!r}`",
                  f"- bullet: `{g[k]['text'][:220]!r}`"]
    gq, mq, smp = QJ.replies("glm"), QJ.replies("gemini"), QJ.samples_by_site()
    pairs = []
    for key, text in mq.items():
        if key not in gq:
            continue
        c = by_key[key]
        a, b = QJ.parse(text, len(c.bullets)), QJ.parse(gq[key], len(c.bullets))
        if a and b:
            pairs += [(key, x, b[x["n"]], a[x["n"]]) for x in c.bullets
                      if x["src"] == "true" and {a[x["n"]], b[x["n"]]} == {"YES", "NO"}]
    lines += ["", "## (q): one judge YES, the other NO (`random.Random(0)`)", "",
              f"{len(pairs)} disagreements."]
    for i, (key, x, gl, gm) in enumerate(random.Random(0).sample(pairs, 15), 1):
        c = by_key[key]
        lines += ["", f"### Q{i}. {key} bullet {x['j']}: GLM {gl}, Gemini {gm}", "",
                  f"- bullet: `{x['text'][:220]!r}`"]
        lines += [f"- sample {j + 1}: `{s[:200]!r}`"
                  for j, s in enumerate(smp[f"{c.id}|p{c.pos}"])]
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
