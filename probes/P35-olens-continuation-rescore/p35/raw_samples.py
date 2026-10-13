"""Random raw samples for the note, drawn by seed (not chosen by hand).

    uv run python -m p35.raw_samples      # results/raw_samples.md

(H): 6 random lens cells (``random.Random(0)``), each with the text around the read point, every
bullet with its source (true/control) and both judges' (H) labels and sample verdicts, and
Qwen's first 2 sampled continuations. (B): 6 random bullets from ``regex_b_bullets.jsonl``.
(M): 4 random bullets from ``regex_m_bullets.jsonl``.
"""

from __future__ import annotations

import json
import random

from p35 import hjudge as H
from p35 import qjudge as QJ

OUT = H.RESULTS / "raw_samples.md"


def main() -> int:
    rng = random.Random(0)
    cells = [c for c in H.load_cells() if c.reader == "lens" and c.control_pos is not None]
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    rep = {j: H.replies(j) for j in ("glm", "gemini")}
    qrep = {j: QJ.replies(j) for j in ("glm", "gemini")}
    smp = QJ.samples_by_site()
    lines = ["# P35 random raw samples", "",
             "Drawn by `p35/raw_samples.py` with `random.Random(0)`; nothing chosen by hand.", "",
             "## (H) lens cells", ""]
    for c in rng.sample(cells, 6):
        it = items[c.id]
        site = next(s for s in it["sites"] if s["pos"] == c.pos)
        before, nxt, _ = H.segments(it["response"], site["char"])
        lines += [f"### {c.key} ({c.kind}; control site p{c.control_pos})", "",
                  f"- **Before the read point:** `…{before[-160:]!r}`",
                  f"- **Next (realised):** `{nxt[:200]!r}`"]
        for k, s in enumerate(smp.get(f"{c.id}|p{c.pos}", [])[:2]):
            lines.append(f"- **Qwen sample {k + 1}:** `{s[:200]!r}`")
        labs = {j: H.parse(rep[j].get(c.key), len(c.bullets)) for j in rep}
        qlabs = {j: QJ.parse(qrep[j].get(c.key), len(c.bullets)) for j in qrep}
        head = ("| # | source | GLM (form, content) | Gemini (form, content) "
                "| in a Qwen sample (GLM / Gemini) | bullet |")
        lines += ["", head, "|---|---|---|---|---|---|"]
        for b in c.bullets:
            g = "/".join(labs["glm"][b["n"]]) if labs["glm"] else "–"
            m = "/".join(labs["gemini"][b["n"]]) if labs["gemini"] else "–"
            q = " / ".join(qlabs[j][b["n"]] if qlabs[j] else "–" for j in ("glm", "gemini"))
            txt = b["text"].replace("\n", " ").replace("|", "\\|")[:160]
            lines.append(f"| {b['n']} | {b['src']} | {g} | {m} | {q} | {txt} |")
        lines.append("")
    for part, path, n in (("B", H.RESULTS / "regex_b_bullets.jsonl", 6),
                          ("M", H.RESULTS / "regex_m_bullets.jsonl", 4)):
        rows = [json.loads(x) for x in path.read_text().splitlines()]
        lines += [f"## ({part}) bullets", ""]
        for r in rng.sample(rows, n):
            text = r.pop("text")
            r.pop("bullet", None)
            flags = {k: v for k, v in r.items() if isinstance(v, bool | int) and k not in
                     ("layer", "pos", "bullet_index", "j")}
            meta = {k: r[k] for k in ("source", "draw", "reader", "id", "layer", "pos") if k in r}
            lines.append(f"- `{json.dumps(meta)}` flags `{json.dumps(flags)}`: "
                         f"{text[:200]!r}")
        lines.append("")
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
