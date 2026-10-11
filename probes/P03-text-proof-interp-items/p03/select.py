"""Between the pod sessions: consistency, targets, matching, the text-proof filter, the held-out
predictors, and the reader item list (plan "Items", "Text-proof filter", "Measure 1").

    uv run python -m p03.select [--dry-run] [--no-heldout]   (from the probe dir, PYTHONPATH=.:../../src)

Inputs: data/items.jsonl, results/sample/samples.jsonl (session 1), the API caches under
results/api/. Outputs:
- data/selection.jsonl: one row per item with every decision (consistent, target, guesses, hits,
  survivor, held-out hits);
- data/reader_items.jsonl and data/smoke_hf.jsonl: session 2's inputs;
- results/textproof.json: survivors per type and the held-out hit rates with Wilson intervals.

Item rules (plan "Items"; modal share >= 0.6 everywhere):
- B: the main answer is consistent. Target = that answer.
- C: the B item of its category is consistent (target = that favourite); C's composed answer is
  consistent; Qwen's property answer for its own favourite (round 2) is consistent and names the
  same thing as the composed answer.
- A: the hop-1 bridge B' is consistent and is not the true bridge; the two-hop answer is
  consistent, equals Qwen's hop-2 answer for B', and differs from the true answer. Target = B'.
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from emx import probe_llm as L
from p03 import core as C
from p03 import predict as P

API = C.RESULTS / "api"
CONTRAST_PER_TYPE = 50
CONTRAST_SEED = 0
SMOKE_HF_N = 10


@dataclass
class Matcher:
    """String match first (``core.same`` on aliases), else luna's YES/NO; pairs without a verdict
    are collected by ``need`` and decided after one batched ``resolve``."""

    aliases: dict[str, list[str]] = field(default_factory=dict)  # norm(target) -> aliases
    verdict: dict[str, bool | None] = field(default_factory=dict)  # equiv call key -> verdict
    pending: dict[str, L.Call] = field(default_factory=dict)

    def string(self, a: str, b: str, extra: Iterable[str] = ()) -> bool:
        al = [*self.aliases.get(C.norm(b), []), *extra]
        return C.same(a, b, al) or C.same(b, a, self.aliases.get(C.norm(a), []))

    def need(self, a: str, b: str, kind: str, extra: Iterable[str] = ()) -> None:
        if C.norm(a) and C.norm(b) and not self.string(a, b, extra):
            c = P.equiv_call(a, b, kind)
            if c.key not in self.verdict:
                self.pending[c.key] = c

    def resolve(self, *, dry_run: bool = False) -> None:
        if not self.pending:
            return
        res = L.run_calls(list(self.pending.values()), exp=C.EXP, cache=API / "equiv.jsonl",
                          purpose="equiv", concurrency=64, dry_run=dry_run)
        for k, r in res.items():
            self.verdict[k] = P.equiv_yes(r)
        self.pending = {}

    def match(self, a: str, b: str, kind: str, extra: Iterable[str] = (),
              unknown: bool = True) -> bool:
        """``unknown`` is returned when luna gave no usable verdict (callers pass the value that
        is conservative for their decision)."""
        if not C.norm(a) or not C.norm(b):
            return False
        if self.string(a, b, extra):
            return True
        v = self.verdict.get(P.equiv_call(a, b, kind).key)
        return unknown if v is None else v


def two_pass(m: Matcher, fn: Callable[[], object], dry_run: bool) -> object:
    """Run ``fn`` once to collect the equivalence pairs it needs, resolve them, run it again."""
    fn()
    m.resolve(dry_run=dry_run)
    return fn()


def item_rows(items: list[C.Item], s: dict[str, C.SampleRow], m: Matcher, collect: bool) -> list:
    by_id = {it.id: it for it in items}
    rows = []
    for it in items:
        md = {k: C.modal(s[f"{it.id}|{k}"].samples) for k in C.KINDS if f"{it.id}|{k}" in s}
        main = md.get("main")
        r: dict = {"id": it.id, "type": it.type, "category": it.category, "source": it.source,
                   "modal": {k: [v.surface, round(v.share, 3)] for k, v in md.items()},
                   "consistent": False, "target": None, "why": ""}
        if it.type == "B":
            r["consistent"] = bool(main and main.consistent)
            r["target"] = main.surface if r["consistent"] else None
            r["why"] = "" if r["consistent"] else "main inconsistent"
        elif it.type == "C":
            b = s.get(f"{it.b_id}|main")
            bm = C.modal(b.samples) if b else None
            prop = md.get("prop")
            if not (bm and bm.consistent):
                r["why"] = "favourite inconsistent"
            elif not (main and main.consistent):
                r["why"] = "composed inconsistent"
            elif not (prop and prop.consistent):
                r["why"] = "property inconsistent"
            else:
                if collect:
                    m.need(main.surface, prop.surface, "answer")
                if m.match(main.surface, prop.surface, "answer", unknown=False):
                    r["consistent"], r["target"] = True, bm.surface
                else:
                    r["why"] = "composed != property of favourite"
            if bm and bm.consistent:
                r["favourite"] = bm.surface
            r["b_ws_target"] = by_id[it.b_id].ws_target
        else:  # A
            h1, h2 = md.get("hop1"), md.get("hop2")
            if not (h1 and h1.consistent):
                r["why"] = "hop1 inconsistent"
            elif not (main and main.consistent):
                r["why"] = "two-hop inconsistent"
            elif not (h2 and h2.consistent):
                r["why"] = "hop2 inconsistent"
            else:
                if collect:
                    m.need(h1.surface, it.bridge_true or "", "person", it.bridge_aliases)
                    m.need(main.surface, h2.surface, "country")
                    m.need(main.surface, it.answer_true or "", "country", it.answer_aliases)
                right_bridge = m.match(h1.surface, it.bridge_true or "", "person",
                                       it.bridge_aliases, unknown=True)
                same_chain = m.match(main.surface, h2.surface, "country", unknown=False)
                right_answer = m.match(main.surface, it.answer_true or "", "country",
                                       it.answer_aliases, unknown=True)
                r["bridge_correct"] = right_bridge
                r["answer_correct"] = right_answer
                if right_bridge:
                    r["why"] = "bridge correct"
                elif not same_chain:
                    r["why"] = "two-hop != hop2(bridge)"
                elif right_answer:
                    r["why"] = "answer correct despite wrong bridge"
                else:
                    r["consistent"], r["target"] = True, h1.surface
            r["bridge_true"], r["answer_true"] = it.bridge_true, it.answer_true
        rows.append(r)
    return rows


def kind_for(it: C.Item) -> str:
    return P.kind_of(it)


def hits(m: Matcher, guesses: list[str], target: str, kind: str, collect: bool) -> bool:
    if collect:
        for g in guesses:
            m.need(g, target, kind)
    return any(m.match(g, target, kind, unknown=True) for g in guesses)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-heldout", action="store_true")
    a = ap.parse_args(argv)
    items = C.load_items()
    by_id = {it.id: it for it in items}
    samples = C.load_samples(C.RESULTS / "sample" / "samples.jsonl")
    m = Matcher()

    rows = two_pass(m, lambda: item_rows(items, samples, m, True), a.dry_run)
    cons = [r for r in rows if r["consistent"]]

    # aliases for every target (luna), then re-derive with them
    targets = {(r["target"], kind_for(by_id[r["id"]])) for r in cons}
    ares = L.run_calls([P.alias_call(t, k) for t, k in sorted(targets)], exp=C.EXP,
                       cache=API / "aliases.jsonl", purpose="aliases", dry_run=a.dry_run)
    for t, k in targets:
        r = ares.get(P.alias_call(t, k).key)
        m.aliases[C.norm(t)] = P.parse_aliases(r.text if r else None)

    # selection predictors (cached from the pre-pod run)
    sel = L.run_calls(P.selection_calls(items), exp=C.EXP, cache=API / "selection.jsonl",
                      purpose="selection", dry_run=a.dry_run)

    def select(collect: bool) -> None:
        for r in cons:
            it = by_id[r["id"]]
            g = P.parse_guesses(sel[f"sel-gemini|{it.id}"].text if f"sel-gemini|{it.id}" in sel
                                else None)
            r["gemini_guesses"] = g
            r["gemini_hit"] = hits(m, g, r["target"], kind_for(it), collect)
            r["open_guess"], r["open_hit"] = None, False
            if it.type in ("B", "C"):
                b_id = it.id if it.type == "B" else it.b_id
                pl, per = P.open_plurality(sel, b_id)
                r["open_guess"], r["open_per_model"] = pl, per
                r["open_hit"] = hits(m, [pl] if pl else [], r["target"], kind_for(it), collect)
                r["open_per_model_hit"] = hits(m, [x for x in per.values() if x], r["target"],
                                               kind_for(it), collect)
            r["survivor"] = not (r["gemini_hit"] or r["open_hit"])

    two_pass(m, lambda: select(True), a.dry_run)
    surv = [r for r in cons if r["survivor"]]
    print({t: (sum(r["type"] == t for r in cons), sum(r["type"] == t for r in surv))
           for t in C.TYPES}, "(consistent, survivors)")

    if not a.no_heldout:
        pool = [(by_id[r["id"]], r["target"]) for r in cons]
        outputs = {r["id"]: r["modal"]["main"][0] for r in cons}
        ho = L.run_calls(P.heldout_calls([by_id[r["id"]] for r in cons], pool, outputs),
                         exp=C.EXP, cache=API / "heldout.jsonl", purpose="heldout",
                         dry_run=a.dry_run)

        def heldout(collect: bool) -> None:
            for r in cons:
                it = by_id[r["id"]]
                for name in ("ho-sonnet", "ho-fewshot", "ho-sonnet-out", "ho-fewshot-out"):
                    res = ho.get(f"{name}|{it.id}")
                    if res is None:
                        continue
                    g = P.parse_guesses(res.text)
                    r[f"{name}_guesses"] = g
                    r[f"{name}_hit"] = hits(m, g, r["target"], kind_for(it), collect)

        two_pass(m, lambda: heldout(True), a.dry_run)

    if a.dry_run:
        return 0
    C.write_jsonl(C.DATA / "selection.jsonl", rows)
    write_reader_inputs(items, cons, samples, m)
    summary = textproof(cons)
    (C.RESULTS / "textproof.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


def textproof(cons: list[dict]) -> dict:
    out: dict = {}
    for t in C.TYPES:
        allc = [r for r in cons if r["type"] == t]
        sv = [r for r in allc if r["survivor"]]
        d: dict = {"consistent": len(allc), "survivors": len(sv)}
        for name in ("ho-sonnet", "ho-fewshot", "ho-sonnet-out", "ho-fewshot-out",
                     "gemini", "open"):
            for label, rs in (("survivors", sv), ("all_consistent", allc)):
                got = [r[f"{name}_hit"] for r in rs if f"{name}_hit" in r]
                if got:
                    d[f"{name}|{label}"] = dict(zip(("rate", "lo", "hi"),
                                                    C.wilson(sum(got), len(got)))) | {
                                                        "k": sum(got), "n": len(got)}
        hs, hf = d.get("ho-sonnet|survivors"), d.get("ho-fewshot|survivors")
        d["text_proof"] = bool(
            len(sv) >= C.TEXT_PROOF_MIN_SURVIVORS and hs and hf
            and max(hs["rate"], hf["rate"]) <= C.TEXT_PROOF_MAX_RATE
            and max(hs["hi"], hf["hi"]) <= C.TEXT_PROOF_MAX_UPPER)
        out[t] = d
    return out


def write_reader_inputs(items: list[C.Item], cons: list[dict], samples: dict[str, C.SampleRow],
                        m: Matcher) -> None:
    by_id = {it.id: it for it in items}
    rng = random.Random(CONTRAST_SEED)
    rows = []
    for t in C.TYPES:
        sv = [r for r in cons if r["type"] == t and r["survivor"]]
        non = [r for r in cons if r["type"] == t and not r["survivor"]]
        contrast = rng.sample(non, min(CONTRAST_PER_TYPE, len(non)))
        for group, rs in (("survivor", sv), ("contrast", contrast)):
            for r in rs:
                it = by_id[r["id"]]
                rows.append({"id": it.id, "type": t, "group": group, "prompt": it.prompt,
                             "prefill": it.prefill, "target": r["target"],
                             "target_aliases": m.aliases.get(C.norm(r["target"]), []),
                             "modal_answer": r["modal"]["main"][0]})
    C.write_jsonl(C.DATA / "reader_items.jsonl", rows)
    smoke = [r for r in rows if r["type"] == "B"][:SMOKE_HF_N]
    C.write_jsonl(C.DATA / "smoke_hf.jsonl", [
        {"key": f"{r['id']}|main", "prompt": r["prompt"], "prefill": r["prefill"],
         "modal_answer": r["modal_answer"]} for r in smoke])
    print(f"reader items: {len(rows)}; smoke_hf: {len(smoke)}")


if __name__ == "__main__":
    raise SystemExit(main())
