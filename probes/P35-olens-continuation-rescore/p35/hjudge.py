"""P35 part (H): where does each Oracle Lens bullet's content sit in the conversation?

    uv run python -m p35.hjudge build            # cells.jsonl (no API)
    uv run python -m p35.hjudge run --set smoke --judge <name>   # 20 cells
    uv run python -m p35.hjudge run --set lens|monitor|validate --judge <name>

Plan: ``probes/P35-olens-continuation-rescore/plan.md``, "Measure" (H).

**Cells.** I1a's hallucination family (WorkspaceBench ``evals/hallucination``): 149 chats, 1,123
response read sites, all punctuation or newline tokens, read at layers 20/36/44/52/60 by
``olens_s3d_rl600`` (``results/I1a/readouts/olens_rl600/hallucination.jsonl``, one sample of
about 4 bullets per cell). The text monitor (``results/I1a/readouts/text_monitor/``) saw the
prompt and the response up to and including the read token; its rows repeat across layers, so
it is judged once per site (layer 20's row; a test asserts the layers agree).

**Blind position control.** Each call shows the cell's bullets together with the bullets of a
*control site*: another read site of the same conversation at the same layer (same reader),
chosen by ``random.Random(f"{SEED}|{id}|{pos}")`` among sites at least 2 sites away when the item
has at least 3 sites, else any other site. All bullets are shuffled together (seeded per cell)
and numbered; the judge isn't told that two sources exist. Items with one site have no control
(their bullets are judged alone and are left out of true-vs-control comparisons).

**Response segments.** The marker splits the response at the site's ``char`` offset (WorkspaceBench's
judge marker position). NEXT = the first ``NEXT_WORDS`` words after it (a CJK or kana/hangul
character counts as one word), LATER = the rest. The judge sees the three segments labelled.

**Labels** (two per bullet; definitions in ``USER``). *Form*: CONT (reads as the assistant's own
next words at the marked point) or OTHER. *Content*: NEXT, LATER, BEFORE (where its specific
content is said), NEW (said nowhere, not contradicted), WRONG (contradicted or unrelated) or
GENERIC. The reply is JSON ``{"1": ["CONT", "NEXT"], ...}``; a reply missing a bullet, carrying an
unknown label or extra keys fails parsing and the cell counts as unjudged. (v1, a single
six-way label with COMMENTARY, was dropped after the smoke: the judges agreed on 63/160 bullets,
split mostly on continuation-shaped bullets with a different value; log.md.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).resolve().parents[1]
RESULTS = PROBE / "results"
ITEMS = ROOT / "third_party/workspace-bench/evals/hallucination/items.json"
LENS = ROOT / "results/I1a/readouts/olens_rl600/hallucination.jsonl"
MONITOR = ROOT / "results/I1a/readouts/text_monitor/hallucination.jsonl"
CELLS = RESULTS / "h_cells.jsonl"

SEED = 20261013
NEXT_WORDS = 50
FORMS = ("CONT", "OTHER")
CONTENTS = ("NEXT", "LATER", "BEFORE", "NEW", "WRONG", "GENERIC")
PROMPT_VERSION = "v2"
DEEPSEEK = "deepseek/deepseek-v4-flash"
GEMINI = "google/gemini-3.8-flash"
GLM = "z-ai/glm-5.3-flash"
N_VALIDATE = 300
N_SMOKE = 20

_WORD = re.compile(r"[぀-ヿ㐀-鿿豈-﫿가-힯]|[^\s぀-ヿ"
                   r"㐀-鿿豈-﫿가-힯]+")


def split_bullets(text: str) -> list[str]:
    """Bullets of one readout: its ``- `` lines, where a line that doesn't start a bullet
    continues the current one (text before the first bullet is a bullet of its own). Empty
    bullets are dropped."""
    out: list[str] = [""]
    for line in text.split("\n"):
        if line.startswith("- "):
            out.append(line[2:])
        else:
            out[-1] = f"{out[-1]}\n{line}" if out[-1] else line
    return [b.strip() for b in out if b.strip()]


def segments(response: str, char: int, n_words: int = NEXT_WORDS) -> tuple[str, str, str]:
    """(before, next, later) split of ``response`` at ``char``; NEXT ends after ``n_words``
    words (CJK characters count one each)."""
    before, rest = response[:char], response[char:]
    cut = len(rest)
    for i, m in enumerate(_WORD.finditer(rest)):
        if i == n_words - 1:
            cut = m.end()
            break
    return before, rest[:cut], rest[cut:]


def control_pos(item_id: str, pos: int, positions: list[int]) -> int | None:
    """The control site for (item, pos): another site, at least 2 sites away when possible."""
    others = [p for p in positions if p != pos]
    if not others:
        return None
    i = positions.index(pos)
    far = [p for j, p in enumerate(positions) if abs(j - i) >= 2]
    pool = far if len(positions) >= 3 and far else others
    return random.Random(f"{SEED}|{item_id}|{pos}").choice(pool)


@dataclass
class Cell:
    key: str
    reader: str  # "lens" or "monitor"
    id: str
    layer: int
    pos: int
    kind: str
    control_pos: int | None
    bullets: list[dict]  # {"n": shown number, "src": "true"|"control", "j": index, "text": ...}


def _rows(path: Path) -> dict[tuple[str, int, int], str]:
    out = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        assert len(r["samples"]) == 1, r["id"]
        out[(r["id"], r["layer"], r["pos"])] = r["samples"][0]
    return out


def build_cells(reader: str) -> list[Cell]:
    items = {it["id"]: it for it in json.loads(ITEMS.read_text())["items"]}
    rows = _rows(LENS if reader == "lens" else MONITOR)
    if reader == "monitor":
        by_site: dict[tuple[str, int], set[str]] = {}
        for (i, _, p), t in rows.items():
            by_site.setdefault((i, p), set()).add(t)
        bad = [k for k, v in by_site.items() if len(v) != 1]
        assert not bad, f"text monitor rows differ across layers at {bad[:3]}"
        rows = {(i, 20, p): t for (i, layer, p), t in rows.items() if layer == 20}
    cells = []
    for (iid, layer, pos), text in sorted(rows.items()):
        it = items[iid]
        positions = [s["pos"] for s in it["sites"]]
        site = it["sites"][positions.index(pos)]
        cp = control_pos(iid, pos, positions)
        bl = [("true", j, b) for j, b in enumerate(split_bullets(text))]
        if cp is not None:
            bl += [("control", j, b) for j, b in enumerate(split_bullets(rows[(iid, layer, cp)]))]
        key = f"{reader}|{iid}|L{layer:02d}|p{pos}"
        random.Random(f"{SEED}|{key}").shuffle(bl)
        cells.append(Cell(key, reader, iid, layer, pos, site["kind"], cp, [
            {"n": n + 1, "src": s, "j": j, "text": b} for n, (s, j, b) in enumerate(bl)]))
    return cells


SYSTEM = (
    "You locate content in a conversation. You are given a conversation between a user and an "
    "AI assistant, split at a marked point inside the assistant's response, and a numbered list "
    "of short text fragments. For each fragment you decide where, if anywhere, its content "
    "appears in the conversation. Output only the JSON object."
)

USER = """USER PROMPT:
<prompt>
{prompt}
</prompt>

ASSISTANT RESPONSE, split at the marked point:

<before>
{before}
</before>
<<<MARKED POINT>>>
<next>
{next}
</next>
<later>
{later}
</later>

FRAGMENTS:
{fragments}

For each fragment give two labels.

1. FORM: does it read as the assistant's own next words at the marked point?
- "CONT": yes: it continues the sentence, list or structure right at the marked point, in the
  assistant's voice. Ignore a leading word or two in another language, and ignore whether
  its content is right.
- "OTHER": no: text that would fit somewhere else, a summary of or remark about the
  conversation, a heading or text for a different document, or anything else.

2. CONTENT: where is its specific content said? Specific content means its particular facts,
values, names, items, questions or wording; a paraphrase or a translation counts as the same
content, but sharing only the topic or the format does not. Check in this order:
- "GENERIC": it has no specific content: filler, formatting, a stock phrase ("Here is",
  "My answer is"), or a garbled string.
- "NEXT": its main specific content is said in <next>.
- "LATER": not in <next>, but said in <later>.
- "BEFORE": not in <next> or <later>; said only in the user prompt or <before>.
- "NEW": said nowhere in the conversation, and nothing in the conversation contradicts it
  (e.g. a different but plausible point or question, or a remark about the situation, the
  user, or the assistant's intent, stance or reasoning).
- "WRONG": it contradicts the conversation (e.g. a different value, name or fact for
  something the conversation states), or it is about an unrelated subject.

If a fragment mixes content, label its main specific content.

Reply with a JSON object mapping every fragment number (as a string) to [FORM, CONTENT], e.g.
{example}"""


def render(cell: Cell, item: dict) -> str:
    site = next(s for s in item["sites"] if s["pos"] == cell.pos)
    before, nxt, later = segments(item["response"], site["char"])
    frags = "\n".join(f"[{b['n']}] {b['text']}" for b in cell.bullets)
    ex = json.dumps({"1": ["CONT", "NEXT"], "2": ["OTHER", "NEW"]})
    return USER.format(prompt=item["prompt"], before=before, next=nxt,
                       later=later or "(end of response)", fragments=frags, example=ex)


def parse(text: str | None, n: int) -> dict[int, tuple[str, str]] | None:
    """``{fragment number: (form, content)}`` for exactly fragments 1..n, or None."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    out = {}
    for k, v in obj.items():
        try:
            i = int(str(k).strip())
        except ValueError:
            return None
        if not isinstance(v, list) or len(v) != 2:
            return None
        form, content = (str(x).strip().upper() for x in v)
        if form not in FORMS or content not in CONTENTS:
            return None
        out[i] = (form, content)
    return out if set(out) == set(range(1, n + 1)) else None


def write_cells(cells: list[Cell], path: Path = CELLS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for c in cells:
            f.write(json.dumps(c.__dict__, ensure_ascii=False) + "\n")


def load_cells(path: Path = CELLS) -> list[Cell]:
    return [Cell(**json.loads(x)) for x in path.read_text().splitlines()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# judge name -> (route, reasoning, max_tokens)
JUDGES = {"deepseek": (DEEPSEEK, "none", 400), "deepseek-r": (DEEPSEEK, "low", 4000),
          "gemini": (GEMINI, "low", 500), "glm": (GLM, "low", 2000),
          # retries of cells whose reply in the base judge's cache didn't parse (cut off at
          # max_tokens: Gemini's reasoning counts against it)
          "gemini-long": (GEMINI, "low", 2500), "glm-long": (GLM, "low", 5000)}
RETRY_OF = {"gemini-long": "gemini", "glm-long": "glm"}


def select(cells: list[Cell], which: str) -> list[Cell]:
    """The cells of a named set."""
    lens = [c for c in cells if c.reader == "lens" and c.bullets]
    if which == "lens":
        return lens
    if which == "monitor":
        return [c for c in cells if c.reader == "monitor" and c.bullets]
    if which == "validate":
        return random.Random(SEED).sample(lens, N_VALIDATE)
    if which == "smoke":
        return random.Random(SEED + 1).sample(lens, N_SMOKE)
    raise ValueError(which)


def cache_path(judge: str) -> Path:
    return RESULTS / f"h_judge_{judge}.{PROMPT_VERSION}.jsonl"


def _cache_texts(judge: str) -> dict[str, str | None]:
    p = cache_path(judge)
    if not p.exists():
        return {}
    return {(r := json.loads(x))["key"]: r.get("text") for x in p.read_text().splitlines()}


def replies(judge: str) -> dict[str, str | None]:
    """``{cell key: reply text}`` from a judge's cache, where a retry judge's non-empty reply
    (``RETRY_OF``) replaces the base reply it retried."""
    out = _cache_texts(judge)
    for retry, base in RETRY_OF.items():
        if base == judge:
            out.update({k: t for k, t in _cache_texts(retry).items() if t})
    return out


def run(which: str, judge: str, dry_run: bool = False, layer: int | None = None) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from emx import probe_llm

    cells = load_cells()
    items = {it["id"]: it for it in json.loads(ITEMS.read_text())["items"]}
    todo = [c for c in select(cells, which) if layer is None or c.layer == layer]
    if judge in RETRY_OF:
        base = _cache_texts(RETRY_OF[judge])
        todo = [c for c in todo if c.key in base and parse(base[c.key], len(c.bullets)) is None]
    route, reasoning, max_tokens = JUDGES[judge]
    calls = [probe_llm.Call(
        key=c.key, route=route, user=render(c, items[c.id]), system=SYSTEM, temperature=0.0,
        seed=0, max_tokens=max_tokens, reasoning=reasoning, json_mode=True) for c in todo]
    conc = int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32"))
    which = which if layer is None else f"{which}_L{layer}"
    print(f"{which}/{judge}: {len(calls)} calls on {route}; projected "
          f"${probe_llm.project_usd(calls):.2f} (worst case)", flush=True)
    res = probe_llm.run_calls(calls, exp="P35", cache=cache_path(judge),
                              purpose=f"p35_h_{which}_{judge}", concurrency=conc,
                              dry_run=dry_run)
    n_ok = sum(1 for c in todo if (r := res.get(c.key)) and parse(r.text, len(c.bullets)))
    usd = sum(r.usd for r in res.values() if r)
    print(f"{which}/{judge}: parsed {n_ok}/{len(todo)}; spend this batch ${usd:.3f}", flush=True)
    return {"n": len(todo), "parsed": n_ok, "usd": usd}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    r = sub.add_parser("run")
    r.add_argument("--set", required=True, choices=["smoke", "lens", "monitor", "validate"])
    r.add_argument("--judge", required=True, choices=sorted(JUDGES))
    r.add_argument("--layer", type=int, help="only cells at this layer (keeps reservations small)")
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        cells = build_cells("lens") + build_cells("monitor")
        write_cells(cells)
        meta = {"items_sha256": sha256(ITEMS), "lens_sha256": sha256(LENS),
                "monitor_sha256": sha256(MONITOR), "seed": SEED, "next_words": NEXT_WORDS,
                "n_cells": {r: sum(c.reader == r for c in cells) for r in ("lens", "monitor")},
                "n_no_control": sum(c.control_pos is None for c in cells),
                "n_empty": sum(not c.bullets for c in cells)}
        (RESULTS / "h_cells.meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        print(json.dumps(meta, indent=1))
        return 0
    run(a.set, a.judge, a.dry_run, a.layer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
