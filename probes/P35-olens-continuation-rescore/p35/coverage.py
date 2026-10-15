"""Read-position coverage of the Oracle Lens readouts P35 scores (no API calls).

    uv run python -m p35.coverage      # results/coverage.json

Each read token is classed by its text alone:
- ``delimiter``: only punctuation, symbols and/or whitespace (",", ".", "\\n\\n", " **", ":");
- ``word_start``: starts with whitespace and then a letter or digit (" answer", " is", " the");
- ``mid_word``: starts with a letter or digit, no leading space: a continuation piece of a word
  (or a word at the very start of a line);
- ``other``: anything else (e.g. a CJK character).
Counts are distinct read sites (item × position) and readout cells (sites × layers × draws).
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter

from p35 import hjudge as H

P03 = H.ROOT / "probes/P03-text-proof-interp-items/results/read/olens_samples/main.jsonl"
P11_SITES = H.ROOT / "probes/P11-bridge-read-vs-reanswer/results/sites/plain.jsonl"
P11_GLOB = "probes/P11-bridge-read-vs-reanswer/results/olens/plain_s*.jsonl"
P11_ITEMS = H.ROOT / "probes/P11-bridge-read-vs-reanswer/data/items.jsonl"
P03_SITES = H.ROOT / "probes/P03-text-proof-interp-items/results/read/sites.jsonl"
I1A = H.ROOT / "results/I1a/readouts/olens_rl600"


def kind(tok: str) -> str:
    if tok and all(unicodedata.category(c)[0] in "PSZ" or c.isspace() for c in tok):
        return "delimiter"
    if tok[:1].isspace() and tok.lstrip()[:1].isalnum():
        return "word_start"
    if tok[:1].isascii() and tok[:1].isalnum():
        return "mid_word"
    return "other"


def _jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def main() -> int:
    out = {}
    ids63 = {r["id"] for r in _jsonl(P11_ITEMS)}
    p03_tok = {(r["id"], r["pos"]): r["token"] for r in _jsonl(P03_SITES)}
    p03 = [r for r in _jsonl(P03) if r["id"] in ids63]
    out["B_p03"] = {"sites": dict(Counter(kind(p03_tok[(r["id"], r["pos"])]) for r in
                                          {(r["id"], r["pos"]): r for r in p03}.values())),
                    "cells": dict(Counter(kind(p03_tok[(r["id"], r["pos"])]) for r in p03))}
    p11_tok = {(r["id"], r["pos"]): r["token"] for r in _jsonl(P11_SITES)}
    p11 = [r for p in sorted(H.ROOT.glob(P11_GLOB)) for r in _jsonl(p)]
    out["B_p11"] = {"sites": dict(Counter(kind(t) for t in p11_tok.values())),
                    "cells": dict(Counter(kind(p11_tok[(r["id"], r["pos"])]) for r in p11))}
    out["B_tokens"] = dict(Counter(p11_tok.values()))
    for fam in ("hallucination", "multihop", "basic_readout_mt"):
        rows = _jsonl(I1A / f"{fam}.jsonl")
        sites = {(r["id"], r["pos"]): r["token"] for r in rows}
        out[f"I1a_{fam}"] = {"sites": dict(Counter(kind(t) for t in sites.values())),
                             "cells": dict(Counter(kind(r["token"]) for r in rows)),
                             "mid_word_tokens": dict(Counter(
                                 t for t in sites.values() if kind(t) == "mid_word"))}
    (H.RESULTS / "coverage.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
