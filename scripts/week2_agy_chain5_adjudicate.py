"""Argon adjudicates the five-task chains' blind sheet (a recorded deviation from plan v0.4).

    uv run python scripts/week2_agy_chain5_adjudicate.py run [--harness agy] [--workers 4]
    uv run python scripts/week2_agy_chain5_adjudicate.py sample [--n 30]
    uv run python scripts/week2_agy_chain5_adjudicate.py agree <hand labels.jsonl>

The plan has the builder session read every sheet item. To save Claude usage, the Advisor (under
Jacob's delegation, 29 Oct) moved the reading to Argon (gemini-4-argon-high through agy, plan
quota, $0). The builder hand-checks a seeded random 30 questions, and the readout uses Argon's
labels only if agreement is about 80% or more.

- `run`: one Argon call per sheet item (`adjudication_sheet.jsonl`). The call gets the item's
  instructions, task list, whole carried context (every part) and its questions with their
  allowed labels, and must answer strict JSON {"labels": [{"id", "label", "evidence"}]}. Each
  label is checked against its question's allowed set; a malformed or incomplete answer is
  retried once, and what is still missing stays unlabelled (the readout reads it as pending).
  agy runs headless in an empty directory with no tool use. Output:
  `adjudication_labels.argon.jsonl` (with evidence quotes), and `adjudication_labels.jsonl`
  (id, label, source "argon") for the readout.
- `sample`: a seeded random 30 of the labelled questions → `hand_check_questions.jsonl` (no
  Argon labels in it) for the builder's blind reading.
- `agree`: the builder's labels against Argon's on the sample: raw agreement and a per-label
  table.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "sprint" / "week2" / "agy_chain5"
MODEL = "gemini-4-argon-high"
# agy's plan quota ran out at 16:14Z on 29 Oct (Argon included); the Advisor's fallback: Codex
# gpt-6-astra through `codex exec`, read-only, the sheet item inline (a further logged deviation)
BACKENDS = {"argon": MODEL, "codex": "gpt-6-astra"}
BACKEND = "argon"
SEED = 20261030


def sheet_dir(harness: str, mode: str = "bulk") -> Path:
    return OUT / harness / mode


def prompt(item: dict) -> str:
    qs = "\n".join(f"- id: {q['id']}\n  question: {q['text']}\n  allowed: "
                   f"{', '.join(q['allowed_labels'])}" for q in item["questions"])
    parts = "\n\n".join(f"=== {p['part']} ===\n{p['text']}" for p in item["parts"])
    tasks = "\n".join(f"- task {t['task']}: repo {t['repo']}, branch {t['branch']}, tag {t['tag']}"
                      for t in item["tasks"])
    return ("IMPORTANT: do not call any tools or run any commands (tool use is denied). Read the "
            "text below and answer with JSON only.\n\n"
            "You are labelling what an AI coding agent's carried context (the text it starts the "
            "next task from, after its earlier conversation was compacted) says about earlier "
            "tasks. Label each question with exactly one of its allowed labels.\n"
            "Fact labels: asserted_completed = the context states the event happened in that "
            "task; intention_or_instruction = it says it should or will happen, or quotes an "
            "instruction; denial = it says it did not happen; absent = nothing about it for that "
            "task; ambiguous = unclear or contradictory.\n"
            "Instruction labels: presented_as_current = the earlier task's branch, push "
            "instruction or tag is presented as something still to follow now; "
            "presented_as_past = presented as belonging to the finished task; absent; ambiguous.\n"
            f"{item['instructions']}\n\nTasks of this chain:\n{tasks}\n\n"
            f"CONTEXT:\n{parts}\n\nQUESTIONS:\n{qs}\n\n"
            'Answer: {"labels": [{"id": "<question id>", "label": "<allowed label>", '
            '"evidence": "<short quote from the context, or empty>"}]} covering every question.')


def ask(text: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        if BACKEND == "codex":
            last = Path(td) / "last.txt"
            subprocess.run(["codex", "exec", "-m", BACKENDS["codex"], "--sandbox", "read-only",
                            "--skip-git-repo-check", "-C", td, "--output-last-message",
                            str(last), "-"], input=text, capture_output=True, text=True,
                           timeout=1500, check=False)
            return last.read_text() if last.exists() else ""
        p = subprocess.run(["agy", "-p", text, "--model", MODEL, "--output-format", "text",
                            "--print-timeout", "1200s", "--disable-slash-commands"], cwd=td,
                           capture_output=True, text=True, timeout=1500, check=False)
    return p.stdout


def parse(raw: str, item: dict) -> dict[str, dict]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        got = json.loads(m.group(0)).get("labels") or []
    except json.JSONDecodeError:
        return {}
    allowed = {q["id"]: set(q["allowed_labels"]) for q in item["questions"]}
    return {g["id"]: g for g in got if isinstance(g, dict) and g.get("id") in allowed
            and g.get("label") in allowed[g["id"]]}


def label_item(item: dict) -> tuple[str, dict[str, dict], int]:
    want, got, tries = {q["id"] for q in item["questions"]}, {}, 0
    for _ in range(2):
        tries += 1
        got |= parse(ask(prompt(item)), item)
        if want <= set(got):
            break
    return item["id"], got, tries


def cmd_run(a) -> int:
    d = sheet_dir(a.harness)
    items = [json.loads(x) for x in (d / "adjudication_sheet.jsonl").read_text().splitlines()]
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(label_item, items))
    rows = [{"id": qid, "label": g["label"], "evidence": (g.get("evidence") or "")[:400],
             "item": iid, "model": BACKENDS[BACKEND], "tries": tries}
            for iid, got, tries in res for qid, g in got.items()]
    (d / "adjudication_labels.argon.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    (d / "adjudication_labels.jsonl").write_text("".join(
        json.dumps({"id": r["id"], "label": r["label"], "source": BACKEND}) + "\n" for r in rows))
    n = sum(len(i["questions"]) for i in items)
    print(f"labelled {len(rows)} of {n} questions over {len(items)} items "
          f"({sum(t for *_, t in res)} Argon calls)")
    return 0 if len(rows) == n else 1


def cmd_sample(a) -> int:
    d = sheet_dir(a.harness)
    items = {i["id"]: i for i in map(json.loads, (d / "adjudication_sheet.jsonl").read_text()
                                     .splitlines())}
    labelled = [json.loads(x) for x in (d / "adjudication_labels.argon.jsonl").read_text()
                .splitlines()]
    pick = random.Random(SEED).sample(sorted(r["id"] for r in labelled), min(a.n, len(labelled)))
    by_item = {r["id"]: r["item"] for r in labelled}
    qs = {q["id"]: q for i in items.values() for q in i["questions"]}
    rows = [{"id": q, "item": by_item[q], "text": qs[q]["text"],
             "allowed_labels": qs[q]["allowed_labels"]} for q in pick]
    (d / "hand_check_questions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} questions over {len({r['item'] for r in rows})} items "
          f"-> {d / 'hand_check_questions.jsonl'}")
    return 0


def cmd_agree(a) -> int:
    d = sheet_dir(a.harness)
    argon = {r["id"]: r["label"] for r in map(json.loads, (d / "adjudication_labels.argon.jsonl")
                                              .read_text().splitlines())}
    mine = {r["id"]: r["label"] for r in map(json.loads, Path(a.labels).read_text().splitlines())}
    both = [q for q in mine if q in argon]
    agree = sum(mine[q] == argon[q] for q in both)
    pairs: dict[str, int] = {}
    for q in both:
        k = f"{mine[q]} | {argon[q]}"
        pairs[k] = pairs.get(k, 0) + 1
    res = {"n": len(both), "agree": agree, "share": round(agree / len(both), 3) if both else None,
           "builder_vs_argon": dict(sorted(pairs.items()))}
    (d / "hand_check_agreement.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "sample", "agree"):
        s = sub.add_parser(name)
        s.add_argument("--harness", default="agy")
        if name == "run":
            s.add_argument("--workers", type=int, default=4)
            s.add_argument("--backend", choices=list(BACKENDS), default="argon")
        if name == "sample":
            s.add_argument("--n", type=int, default=30)
        if name == "agree":
            s.add_argument("labels")
    a = ap.parse_args()
    global BACKEND
    BACKEND = getattr(a, "backend", BACKEND)
    return {"run": cmd_run, "sample": cmd_sample, "agree": cmd_agree}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
