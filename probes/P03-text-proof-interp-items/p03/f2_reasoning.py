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

MAX_TOKENS = 8000
# v1 (reasoning="default") averaged 49 output tokens: adaptive thinking did not engage, so the
# rerun forces it with reasoning effort "high" (log.md, F2).
REASONING = "high"
BASE = {"sonnet": "ho-sonnet_hit", "gemini": "ho-fewshot_hit"}  # the same predictor, less reasoning


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", choices=("sonnet", "gemini"), default="gemini")
    ap.add_argument("--first", type=int, default=None,
                    help="only call the first N items (cached rows are reused): lets the spend "
                         "guard see the real cost before projecting the rest")
    a = ap.parse_args(argv)
    items = {it.id: it for it in C.load_items()}
    sel = [r for r in C.read_jsonl(C.DATA / "selection.jsonl") if r.get("survivor")]
    calls = []
    cons = [r for r in C.read_jsonl(C.DATA / "selection.jsonl") if r.get("consistent")]
    pool = [(items[r["id"]], r["target"]) for r in cons]
    for r in sel:
        it = items[r["id"]]
        if a.model == "sonnet":
            c = P.guess_call("", P.SONNET, it)
        else:  # the held-out few-shot predictor, same shots, with a forced thinking budget
            c = P.guess_call("", P.GEMINI, it, shots=P.draw_shots(it, pool))
        c = dataclasses.replace(c, reasoning=REASONING, max_tokens=MAX_TOKENS)
        calls.append(dataclasses.replace(
            c, key=P.ho_key(f"f2-{a.model}-{REASONING}", r["id"], c.user)))
    if a.first is not None:
        L.run_calls(calls[: a.first], exp=C.EXP, cache=S.API / f"f2_{a.model}_{REASONING}.jsonl",
                    purpose="f2_reasoning", concurrency=32, timeout=300, dry_run=a.dry_run)
        return 0
    res = L.run_calls(calls, exp=C.EXP, cache=S.API / f"f2_{a.model}_{REASONING}.jsonl",
                      purpose="f2_reasoning", concurrency=32, timeout=300, dry_run=a.dry_run)
    if a.dry_run:
        return 0
    m = S.Matcher()
    for al in C.read_jsonl(S.API / "aliases.jsonl"):
        if al.get("text"):
            _, kind, t = al["key"].split("|", 2)
            m.aliases[(t, kind)] = P.parse_aliases(al["text"])
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
                         "hit": hit, "hit_no_reasoning": r.get(BASE[a.model]),
                         "output_tokens": x.output_tokens if x else None})

    S.two_pass(m, lambda: score(True), False)
    out: dict = {"n_calls": len(calls), "errors": sum(1 for c in calls if not by_key[c.key]
                                                      or by_key[c.key].error)}
    for t in C.TYPES:
        rs = [x for x in rows if x["type"] == t and x["hit"] is not None
              and x["hit_no_reasoning"] is not None]  # failed calls are missing, not misses
        if not rs:
            continue
        k = sum(x["hit"] for x in rs)
        k0 = sum(bool(x["hit_no_reasoning"]) for x in rs)
        d, lo, hi = C.paired_bootstrap([float(x["hit"]) for x in rs],
                                       [float(bool(x["hit_no_reasoning"])) for x in rs])
        out[t] = {"n": len(rs), "think": dict(zip(("rate", "lo", "hi"), C.wilson(k, len(rs))))
                  | {"k": k}, "no_think": {"k": k0, "rate": k0 / len(rs)},
                  "think_minus_no_think": {"diff": d, "lo": lo, "hi": hi}}
    C.write_jsonl(C.RESULTS / f"f2_{a.model}_items.jsonl", rows)
    (C.RESULTS / f"f2_{a.model}.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
