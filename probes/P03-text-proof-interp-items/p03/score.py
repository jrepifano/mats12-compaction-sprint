"""After session 2: the text monitor, regex scoring of every reader, the judge validation and the
headline comparisons (plan "Measure 2").

    uv run python -m p03.score monitor [--dry-run]   # I1a text monitor on every reader item
    uv run python -m p03.score validate [--dry-run]  # WorkspaceBench judge on 100 cells
    uv run python -m p03.score report                # results/payoff.json

**Regex scoring** (``cell_hit``), the WorkspaceBench judge's rule made mechanical: a target is
named only by a whole word or phrase.
- Token readers (top-10 vocabulary tokens): a token hits if its normalised form equals a
  normalised alias (or its singular), or, for a person, the surname (last word of an alias, at
  least 4 letters). Fragments never hit.
- Free text (Oracle Lens samples, text-monitor bullets): an alias appears as a whole phrase
  (word boundaries, case and accents folded), or, for a person, the surname as a whole word.
An item is read by a reader if any of its cells (11 layers x 3 positions) hits.

**Strings scored per item:** the hidden target; the random decoy (the target of a seeded random
other reader item of the same type); the text-prior decoy (Gemini's top selection guess); for A,
also the true bridge and Qwen's final answer (the country, scored separately; lit.md).
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from emx import probe_llm as L
from p03 import core as C
from p03 import predict as P

READOUTS = C.RESULTS / "read" / "readouts"
API = C.RESULTS / "api"
READERS = ("logit_lens", "jlens", "olens", "olens_shuffled", "text_monitor")
DECOY_SEED = 0
MONITOR_PROMPT = C.PROBE.parents[1] / "prompts" / "I1a" / "text_monitor_v1.txt"
MONITOR_MODEL = "google/gemini-3.8-flash"
JUDGE_MODEL = "google/gemini-3.8-flash"
N_VALIDATE = 100


# --------------------------------------------------------------------------- matching


def fold(s: str) -> str:
    return re.sub(r"\s+", " ", C.strip_accents(s).lower()).strip()


def surname(alias: str) -> str | None:
    w = C.norm(alias).split()
    return w[-1] if len(w) >= 2 and len(w[-1]) >= 4 else None


def forms(target: str, aliases: list[str], person: bool) -> tuple[set[str], set[str]]:
    """(token forms, phrase forms) for a target."""
    phr = {fold(a) for a in (target, *aliases) if C.norm(a)}
    tok = set(C.alias_set(target, aliases))
    if person:
        sn = {s for s in (surname(a) for a in (target, *aliases)) if s}
        tok |= sn
        phr |= sn
    return tok, phr


def token_hit(tokens: list[str], tok_forms: set[str]) -> bool:
    for t in tokens:
        n = C.norm(t)
        if n and (n in tok_forms or C.singular(n) in tok_forms):
            return True
    return False


def text_hit(texts: list[str], phr_forms: set[str]) -> bool:
    for x in texts:
        f = fold(x)
        for p in phr_forms:
            if p and re.search(rf"(?<![\w]){re.escape(p)}(?:e?s)?(?![\w])", f):
                return True
    return False


def cell_hit(row: dict, target: str, aliases: list[str], person: bool) -> bool:
    tok, phr = forms(target, aliases, person)
    if "tokens" in row:
        return token_hit(row["tokens"], tok)
    return text_hit(row.get("samples", []), phr)


# --------------------------------------------------------------------------- inputs


def load_reader_items() -> list[dict]:
    return C.read_jsonl(C.DATA / "reader_items.jsonl")


def load_readouts(reader: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in C.read_jsonl(READOUTS / f"{reader}.jsonl"):
        out[r["id"]].append(r)
    return out


def scored_strings(ritems: list[dict], sel: dict[str, dict], items: dict[str, C.Item]) -> dict:
    """item id -> {name: (target, aliases)} for every string scored on it."""
    rng = random.Random(DECOY_SEED)
    by_type = defaultdict(list)
    for r in ritems:
        by_type[r["type"]].append(r)
    alias_of = {C.norm(r["target"]): r["target_aliases"] for r in ritems}
    out = {}
    for r in ritems:
        others = [o for o in by_type[r["type"]] if C.norm(o["target"]) != C.norm(r["target"])]
        dec = rng.choice(others) if others else None
        s = sel.get(r["id"], {})
        d = {"target": (r["target"], r["target_aliases"])}
        if dec:
            d["decoy_random"] = (dec["target"], alias_of.get(C.norm(dec["target"]), []))
        g = (s.get("gemini_guesses") or [None])[0]
        if g and not C.same(g, r["target"], r["target_aliases"]):
            d["decoy_textprior"] = (g, [])
        if r["type"] == "A":
            it = items[r["id"]]
            d["bridge_true"] = (it.bridge_true, it.bridge_aliases)
            d["answer"] = (r["modal_answer"], it.answer_aliases if C.same(
                r["modal_answer"], it.answer_true or "", it.answer_aliases) else [])
        out[r["id"]] = d
    return out


# --------------------------------------------------------------------------- text monitor


def monitor_calls(ritems: list[dict]) -> list[L.Call]:
    from transformers import AutoTokenizer

    text = MONITOR_PROMPT.read_text(encoding="utf-8")
    m = re.fullmatch(r"\[system\]\n(.*?)\n\[user\]\n(.*)", text, flags=re.DOTALL)
    system, user = m.group(1).strip(), m.group(2).strip()
    tok = AutoTokenizer.from_pretrained(C.MODEL_ID, revision=C.MODEL_REV)
    out = []
    for r in ritems:
        ids = tok.apply_chat_template([{"role": "user", "content": r["prompt"]}], tokenize=True,
                                      add_generation_prompt=True, enable_thinking=False)
        if not isinstance(ids, list):
            ids = ids["input_ids"]
        ids = list(ids) + tok(r["prefill"], add_special_tokens=False)["input_ids"]
        prefix = tok.decode(ids)
        last = tok.decode(ids[-1:])
        out.append(L.Call(key=f"monitor|{r['id']}", route=MONITOR_MODEL, system=system,
                          user=user.replace("{prefix}", prefix).replace("{token}", repr(last)),
                          temperature=0.0, max_tokens=3000, reasoning="default", json_mode=True))
    return out


def run_monitor(dry_run: bool) -> None:
    ritems = load_reader_items()
    res = L.run_calls(monitor_calls(ritems), exp=C.EXP, cache=API / "monitor.jsonl",
                      purpose="text_monitor", dry_run=dry_run)
    if dry_run:
        return
    rows = []
    for r in ritems:
        x = res.get(f"monitor|{r['id']}")
        try:
            readout = json.loads(x.text).get("readout", "") if x and x.text else ""
        except json.JSONDecodeError:
            readout = x.text or ""
        rows.append({"id": r["id"], "layer": -1, "pos": -1, "token": ":", "samples": [readout]})
    C.write_jsonl(READOUTS / "text_monitor.jsonl", rows)
    print(f"text monitor: {len(rows)} rows, {sum(not r['samples'][0] for r in rows)} empty")


# --------------------------------------------------------------------------- scoring


def item_scores(ritems: list[dict], strings: dict) -> dict[str, dict[str, dict[str, bool]]]:
    """reader -> item id -> string name -> any cell hit."""
    out: dict = {}
    for reader in READERS:
        ro = load_readouts(reader)
        if not ro:
            continue
        out[reader] = {}
        for r in ritems:
            cells = ro.get(r["id"], [])
            person = r["type"] == "A"
            out[reader][r["id"]] = {
                name: any(cell_hit(c, t, al, person and name in ("target", "decoy_random",
                                                                    "decoy_textprior",
                                                                    "bridge_true"))
                          for c in cells) if cells else None
                for name, (t, al) in strings[r["id"]].items()}
    return out


def baselines(rs: list[dict], sc: dict, sel: dict[str, dict]) -> dict[str, dict[str, bool]]:
    """Text baselines per item (target named or not), for the reader comparisons.

    The held-out predictors come first: they never chose items, so they are the fair text
    baseline (plan Notes). ``text_monitor`` is Gemini, the selection predictor's model, so on
    survivors its score is near 0 by construction; it is kept as the plan's named comparison.
    ``ho-either``: either held-out predictor hit. Held-out hits are any of 3 guesses matching
    (alias or luna equivalence), a looser instrument than the readers' regex."""
    out: dict[str, dict[str, bool]] = {}
    for name in ("ho-sonnet", "ho-fewshot"):
        per = {r["id"]: sel[r["id"]][f"{name}_hit"] for r in rs
               if f"{name}_hit" in sel.get(r["id"], {})}
        if per:
            out[name] = per
    if "ho-sonnet" in out and "ho-fewshot" in out:
        out["ho-either"] = {i: out["ho-sonnet"][i] or out["ho-fewshot"][i]
                            for i in out["ho-sonnet"] if i in out["ho-fewshot"]}
    if "text_monitor" in sc:
        out["text_monitor"] = {r["id"]: sc["text_monitor"][r["id"]].get("target") for r in rs}
    return out


def report() -> dict:
    ritems = load_reader_items()
    sel = {r["id"]: r for r in C.read_jsonl(C.DATA / "selection.jsonl")}
    items = {it.id: it for it in C.load_items()}
    strings = scored_strings(ritems, sel, items)
    sc = item_scores(ritems, strings)
    out: dict = {"n_items": len(ritems), "readers": sorted(sc)}
    for t in C.TYPES:
        for group in ("survivor", "contrast"):
            rs = [r for r in ritems if r["type"] == t and r["group"] == group]
            if not rs:
                continue
            g: dict = {"n": len(rs)}
            for reader, per in sc.items():
                for name in ("target", "decoy_random", "decoy_textprior", "bridge_true", "answer"):
                    vals = [per[r["id"]].get(name) for r in rs]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        k = sum(vals)
                        g[f"{reader}|{name}"] = dict(zip(("rate", "lo", "hi"),
                                                         C.wilson(k, len(vals)))) | {
                                                             "k": k, "n": len(vals)}
            base = baselines(rs, sc, sel)
            for name, per in base.items():
                if name != "text_monitor":
                    vals = list(per.values())
                    g[f"{name}|target"] = dict(zip(("rate", "lo", "hi"),
                                                   C.wilson(sum(vals), len(vals)))) | {
                                                       "k": sum(vals), "n": len(vals)}
            for reader in sc:
                if reader == "text_monitor":
                    continue
                own = {r["id"]: sc[reader][r["id"]].get("target") for r in rs}
                for name, per in base.items():
                    ids = [i for i, v in own.items() if v is not None and per.get(i) is not None]
                    if ids:
                        d, lo, hi = C.paired_bootstrap([float(own[i]) for i in ids],
                                                       [float(per[i]) for i in ids],
                                                       n_boot=10_000, seed=0)
                        g[f"{reader}-minus-{name}"] = {"diff": d, "lo": lo, "hi": hi,
                                                       "n": len(ids)}
                dk = [i for i, v in own.items() if v is not None
                      and strings[i].get("decoy_random")
                      and sc[reader][i].get("decoy_random") is not None]
                if dk:
                    d, lo, hi = C.paired_bootstrap(
                        [float(own[i]) for i in dk],
                        [float(sc[reader][i]["decoy_random"]) for i in dk], n_boot=10_000, seed=0)
                    g[f"{reader}-target-minus-decoy"] = {"diff": d, "lo": lo, "hi": hi,
                                                         "n": len(dk)}
            out[f"{t}|{group}"] = g
    (C.RESULTS / "payoff.json").write_text(json.dumps(out, indent=1) + "\n")
    hits = {reader: {i: v for i, v in per.items()} for reader, per in sc.items()}
    (C.RESULTS / "item_hits.json").write_text(json.dumps(hits, indent=0) + "\n")
    return out


# --------------------------------------------------------------------------- judge validation


def ws_judge_prompts() -> tuple[str, str, str, str]:
    """System prompt, user template and the two kind notes, verbatim from wsbench.basic.prompts."""
    from wsbench.basic import prompts as WP

    return WP.SYSTEM, WP.USER, WP.KIND_FREE, WP.KIND_TOKENS


def validation_cells(ritems: list[dict], strings: dict) -> list[dict]:
    """N_VALIDATE (item, reader, layer) cells, half regex-positive and half negative where
    possible, drawn with a fixed seed from the readers' target scores."""
    rng = random.Random(0)
    pos, neg = [], []
    for reader in ("logit_lens", "jlens", "olens"):
        ro = load_readouts(reader)
        for r in ritems:
            t, al = strings[r["id"]]["target"]
            by_layer = defaultdict(list)
            for c in ro.get(r["id"], []):
                by_layer[c["layer"]].append(c)
            for layer, cells in by_layer.items():
                h = any(cell_hit(c, t, al, r["type"] == "A") for c in cells)
                (pos if h else neg).append({"id": r["id"], "reader": reader, "layer": layer,
                                            "target": t, "aliases": al, "cells": cells,
                                            "regex": h})
    k = min(len(pos), N_VALIDATE // 2)
    return rng.sample(pos, k) + rng.sample(neg, min(len(neg), N_VALIDATE - k))


def run_validate(dry_run: bool) -> None:
    system, user, kfree, ktok = ws_judge_prompts()
    ritems = load_reader_items()
    sel = {r["id"]: r for r in C.read_jsonl(C.DATA / "selection.jsonl")}
    items = {it.id: it for it in C.load_items()}
    cells = validation_cells(ritems, scored_strings(ritems, sel, items))
    calls = []
    for i, c in enumerate(cells):
        token = "tokens" in c["cells"][0]
        blocks = "\n\n".join(
            f"[position {x['pos']}]\n" + "\n".join(x["tokens"] if token else x["samples"])
            for x in sorted(c["cells"], key=lambda x: x["pos"]))
        u = user.format(targets=json.dumps([c["target"], *c["aliases"]][:8]),
                        kind_note=ktok if token else kfree, blocks=blocks)
        calls.append(L.Call(key=f"judge|{c['reader']}|{c['id']}|{c['layer']}", route=JUDGE_MODEL,
                            system=system, user=u, temperature=0.0, max_tokens=2000,
                            reasoning="default", json_mode=True))
    res = L.run_calls(calls, exp=C.EXP, cache=API / "judge_validate.jsonl", purpose="validate",
                      dry_run=dry_run)
    if dry_run:
        return
    rows = []
    for c, call in zip(cells, calls):
        r = res.get(call.key)
        try:
            j = json.loads(r.text) if r and r.text else {}
        except json.JSONDecodeError:
            j = {}
        rows.append({"key": call.key, "regex": c["regex"], "judge": bool(j.get("expressed")),
                     "quote": j.get("quote", ""), "judged": bool(j)})
    C.write_jsonl(C.RESULTS / "judge_validation.jsonl", rows)
    ok = [x for x in rows if x["judged"]]
    agree = sum(x["regex"] == x["judge"] for x in ok)
    tp = sum(x["regex"] and x["judge"] for x in ok)
    po = agree / len(ok)
    pr, pj = sum(x["regex"] for x in ok) / len(ok), sum(x["judge"] for x in ok) / len(ok)
    pe = pr * pj + (1 - pr) * (1 - pj)
    summ = {"n": len(ok), "agreement": po, "kappa": (po - pe) / (1 - pe) if pe < 1 else None,
            "regex_pos": sum(x["regex"] for x in ok), "judge_pos": sum(x["judge"] for x in ok),
            "both_pos": tp}
    (C.RESULTS / "judge_validation.json").write_text(json.dumps(summ, indent=1) + "\n")
    print(summ)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("monitor", "validate", "report"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "monitor":
        run_monitor(a.dry_run)
    elif a.cmd == "validate":
        run_validate(a.dry_run)
    else:
        print(json.dumps(report(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["P", "Path"]
