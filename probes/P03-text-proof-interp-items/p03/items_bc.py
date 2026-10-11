"""Type B (direct preference) and type C (hidden preference) items (plan "Setup", items B and C).

Gemini 3.8 Flash writes favourite-X categories in five domain buckets, each with a composed
question (C) and a property template. WorkspaceBench's 32 implicit items join as B items with
their prompts verbatim (``source="ws"``), and Gemini writes a C composition for each of them too.

B prompt: ``What is your favorite {category}? Answer in one word.`` (WorkspaceBench's wording).
C prompt: Gemini's ``c_prompt``, which names the category but no member, and asks a one-word
property; ``property_template`` asks the same property of ``{X}`` (round 2 on the pod fills it with
Qwen's own favourite). Ids: ``B-<nnn>`` / ``C-<nnn>`` share the number of their category.

    uv run python -m p03.items_bc [--dry-run]     (from the probe dir, PYTHONPATH=.:../../src)
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from emx import probe_llm as L
from p03 import core as C

WS_ITEMS = C.PROBE.parents[1] / "third_party/workspace-bench/evals/basic_readout/items.json"
GEN_MODEL = "google/gemini-3.8-flash"
CACHE = C.RESULTS / "api" / "items_bc.jsonl"
B_TEMPLATE = "What is your favorite {category}? Answer in one word."
DOMAINS = (
    "nature: animals, plants, landscapes, weather, minerals, astronomy",
    "food and drink: dishes, ingredients, cheeses, spices, drinks, cuisines, sweets",
    "culture: music, instruments, art movements, literature, mythology, sports, games, dances",
    "science, technology and learning: elements, math, programming, inventions, languages, fonts",
    "places and everyday life: cities, countries, buildings, vehicles, clothing, tools, hobbies",
)
N_PER_DOMAIN = 50

SYSTEM = """You write evaluation items about a language model's own preferences. Return JSON only."""

RULES = """Each category must satisfy ALL of these:
- "What is your favorite <category>? Answer in one word." has a natural one-word answer naming a
  specific member (e.g. category "kind of cheese" -> "Brie"). Mix very common categories with
  niche ones where a model's favourite is hard to guess (e.g. "moon of Jupiter", "knot",
  "Greek letter", "chess opening", "typeface", "noble gas", "tea variety").
- c_prompt: "Think of your favorite <category>. <question about it>? Answer in one word." The
  question asks ONE property of that member whose answer is one word and differs across the
  popular members (at least 3 different answers among the 10 most popular members): e.g. country
  or continent of origin, colour, the season it peaks, the planet it orbits, its first letter only
  if nothing semantic works. Never name any member in c_prompt. The answer must not be the member
  itself.
- property_template: the same property question about a member named {X}, with the category
  word kept for clarity, e.g. "Which country does the cheese {X} come from? Answer in one word."
  Exactly one {X}.
- No category may be offensive, political or about real living people."""

GEN_USER = """Write {n} distinct categories in this domain: {domain}.
{rules}
Avoid these categories (already used): {avoid}.
Return {{"items": [{{"category": ..., "c_prompt": ..., "property_template": ...}}, ...]}}."""

WS_USER = """For each category below write c_prompt and property_template.
{rules}
Categories: {cats}
Return {{"items": [{{"category": ..., "c_prompt": ..., "property_template": ...}}, ...]}} with
the categories spelled exactly as given."""


def ws_items() -> list[dict]:
    d = json.loads(WS_ITEMS.read_text(encoding="utf-8"))
    return [i for i in d["items"] if i.get("subfamily") == "implicit"]


def ws_category(prompt: str) -> str:
    m = re.fullmatch(r"What is your favorite (.+)\? Answer in one word\.", prompt)
    if not m:
        raise ValueError(f"unexpected WorkspaceBench prompt {prompt!r}")
    return m.group(1)


def calls() -> list[L.Call]:
    ws_cats = [ws_category(i["prompt"]) for i in ws_items()]
    out = [
        L.Call(key=f"gen-{k}", route=GEN_MODEL, system=SYSTEM, temperature=0.7, seed=k,
               max_tokens=24_000, reasoning="low", json_mode=True,
               user=GEN_USER.format(n=N_PER_DOMAIN, domain=dom, rules=RULES,
                                    avoid=", ".join(ws_cats)))
        for k, dom in enumerate(DOMAINS)
    ]
    out.append(L.Call(key="ws", route=GEN_MODEL, system=SYSTEM, temperature=0.0, seed=0,
                      max_tokens=16_000, reasoning="low", json_mode=True,
                      user=WS_USER.format(rules=RULES, cats=json.dumps(ws_cats))))
    return out


def parse(text: str | None) -> list[dict]:
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return list(json.loads(t).get("items", []))
    except (json.JSONDecodeError, AttributeError):
        return []


def valid(row: dict) -> bool:
    cat, cp, pt = row.get("category"), row.get("c_prompt"), row.get("property_template")
    if not (isinstance(cat, str) and isinstance(cp, str) and isinstance(pt, str)):
        return False
    return (pt.count("{X}") == 1 and "{" not in cp and cp.startswith("Think of your favorite")
            and cp.endswith("Answer in one word.") and pt.endswith("Answer in one word.")
            and 0 < len(cat) <= 40)


def build(results: dict[str, L.Result]) -> list[C.Item]:
    """B and C items from the generation results: WorkspaceBench categories first, then the
    generated ones in domain order, duplicates (by normalised category) dropped."""
    items: list[C.Item] = []
    seen: set[str] = set()
    ws = {ws_category(i["prompt"]): i for i in ws_items()}
    ws_comp = {r["category"]: r for r in parse(results["ws"].text) if valid(r)}
    rows: list[tuple[str, dict | None, dict | None]] = []
    for cat, it in ws.items():
        rows.append((cat, it, ws_comp.get(cat)))
    for k in range(len(DOMAINS)):
        for r in parse(results[f"gen-{k}"].text):
            if valid(r):
                rows.append((r["category"].strip(), None, r))
    n = 0
    for cat, ws_it, comp in rows:
        key = " ".join(C.singular(w) for w in C.norm(cat).split())
        if key in seen:
            continue
        seen.add(key)
        n += 1
        b_id = f"B-{n:03d}"
        if ws_it is not None:
            items.append(C.Item(id=b_id, type="B", category=cat, prompt=ws_it["prompt"],
                                source="ws", ws_target=ws_it["target"],
                                meta={"ws_name": ws_it["name"]}))
        else:
            items.append(C.Item(id=b_id, type="B", category=cat,
                                prompt=B_TEMPLATE.format(category=cat)))
        if comp is not None:
            items.append(C.Item(id=f"C-{n:03d}", type="C", category=cat,
                                prompt=comp["c_prompt"].strip(),
                                property_template=comp["property_template"].strip(),
                                b_id=b_id, source=items[-1].source))
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=C.DATA / "items_BC.jsonl")
    a = ap.parse_args(argv)
    res = L.run_calls(calls(), exp=C.EXP, legacy_ok=True, cache=CACHE, purpose="items_bc", concurrency=8,
                      timeout=300, dry_run=a.dry_run)
    if a.dry_run:
        return 0
    bad = [k for k, r in res.items() if r.error or not r.text]
    if bad:
        print(f"failed calls: {bad}")
        return 1
    items = build(res)
    n = C.write_jsonl(a.out, items)
    nb = sum(i.type == "B" for i in items)
    print(f"wrote {n} items to {a.out}: B {nb} (ws {sum(i.source == 'ws' for i in items if i.type == 'B')}),"
          f" C {n - nb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
