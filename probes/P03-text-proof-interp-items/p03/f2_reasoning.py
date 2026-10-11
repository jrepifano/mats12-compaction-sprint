"""Follow-up F2 (exploratory, log.md 2026-10-11 18:04): Sonnet 5 with reasoning on, zero-shot, on
every survivor. Does a predictor that thinks first recover the hidden targets that the
reasoning-off predictors missed?

    uv run python -m p03.f2_reasoning [--dry-run]   (from the probe dir, PYTHONPATH=.:../../src)

Same prompt as ``ho-sonnet`` (``predict.predictor_prompt``), with ``reasoning="default"``
(adaptive thinking) and room for the thinking tokens. Hits are decided by the same matcher as
selection (aliases, containment, luna equivalence). Writes results/f2_reasoning.json.
"""

from __future__ import annotations

import argparse
import dataclasses
import json

from emx import probe_llm as L
from p03 import core as C
from p03 import predict as P
from p03 import select as S

MAX_TOKENS = 4000


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    items = {it.id: it for it in C.load_items()}
    sel = [r for r in C.read_jsonl(C.DATA / "selection.jsonl") if r.get("survivor")]
    calls = []
    for r in sel:
        c = P.guess_call("", P.SONNET, items[r["id"]])
        c = dataclasses.replace(c, reasoning="default", max_tokens=MAX_TOKENS)
        calls.append(dataclasses.replace(c, key=P.ho_key("f2-sonnet-think", r["id"], c.user)))
    res = L.run_calls(calls, exp=C.EXP, cache=S.API / "f2_reasoning.jsonl",
                      purpose="f2_reasoning", concurrency=32, timeout=300, dry_run=a.dry_run)
    if a.dry_run:
        return 0
    m = S.Matcher()
    for al in C.read_jsonl(S.API / "aliases.jsonl"):
        if al.get("text"):
            t = al["key"].split("|", 2)[2]
            m.aliases[t] = P.parse_aliases(al["text"])
    by_key = {c.key: res.get(c.key) for c in calls}
    rows = []

    def score(collect: bool) -> None:
        rows.clear()
        for r, c in zip(sel, calls):
            x = by_key[c.key]
            g = P.parse_guesses(x.text if x else None)
            it = items[r["id"]]
            hit = S.hits(m, g, r["target"], P.kind_of(it), collect)
            rows.append({"id": r["id"], "type": r["type"], "target": r["target"], "guesses": g,
                         "hit": hit, "hit_no_reasoning": r.get("ho-sonnet_hit"),
                         "output_tokens": x.output_tokens if x else None})

    S.two_pass(m, lambda: score(True), False)
    out: dict = {"n_calls": len(calls), "errors": sum(1 for c in calls if not by_key[c.key]
                                                      or by_key[c.key].error)}
    for t in C.TYPES:
        rs = [x for x in rows if x["type"] == t]
        if not rs:
            continue
        k = sum(x["hit"] for x in rs)
        k0 = sum(bool(x["hit_no_reasoning"]) for x in rs)
        d, lo, hi = C.paired_bootstrap([float(x["hit"]) for x in rs],
                                       [float(bool(x["hit_no_reasoning"])) for x in rs])
        out[t] = {"n": len(rs), "think": dict(zip(("rate", "lo", "hi"), C.wilson(k, len(rs))))
                  | {"k": k}, "no_think": {"k": k0, "rate": k0 / len(rs)},
                  "think_minus_no_think": {"diff": d, "lo": lo, "hi": hi}}
    C.write_jsonl(C.RESULTS / "f2_reasoning_items.jsonl", rows)
    (C.RESULTS / "f2_reasoning.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
