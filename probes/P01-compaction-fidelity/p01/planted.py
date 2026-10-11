"""The planted-error check (plan "Automatic rules"): in 40 audited summaries, insert one false
status sentence (20) or delete one checklist fact (20), re-audit with luna, and count catches.

- **Insert:** a summary whose test claim was not a misstatement gets one false sentence at a
  seeded paragraph break, phrased as the latest state: "all tests now pass" or "the tests are
  still failing", whichever the truth makes false. Caught = the re-audit finds a false
  latest-state statement (`judge.statements_misstatement`).
- **Delete:** a slice-A summary where a file fact was "present" loses every line that mentions
  the file's basename or its stem, case-insensitive (so no mention survives: "the fix in
  draft04" still states draft04.py, F1). Slice B is excluded: its file is solution.py,
  which the task statement already implies (v1 gate, 2026-10-11). Caught = that item is no
  longer "present".

Gate: catch rate >= 80% passes; < 70% is a measurement failure (stop; the hand audit is next).

- **Fabricate (exploratory, not in the plan's gate):** 20 more summaries get one invented
  action at a seeded paragraph break: a file the transcript never touches was "refactored".
  Caught = the invented-fact call lists a claim naming that file. This measures the invented-
  fact call's sensitivity only; its false-positive rate needs the hand audit.
"""

from __future__ import annotations

import random
import re
from pathlib import PurePosixPath

from p01 import common, judge, render

N_EACH = 20
FALSE_PASS = "Latest test run: all tests now pass."
FALSE_FAIL = "Latest test run: the tests are still failing."


FAKE_FILE = "utils/cache_helpers.py"
FABRICATION = (f"Also refactored `{FAKE_FILE}` to memoize the expensive lookups; that change "
               "is complete.")


def insert_sentence(summary: str, sentence: str, rng: random.Random) -> str:
    paras = summary.split("\n\n")
    k = rng.randint(1, len(paras)) if len(paras) > 1 else 1
    return "\n\n".join(paras[:k] + [sentence] + paras[k:])


def false_sentence(t: dict) -> str | None:
    """A test claim that the truth makes false (checked with `judge.tests_misstatement`)."""
    for sent, claim in ((FALSE_PASS, "pass"), (FALSE_FAIL, "fail")):
        if judge.tests_misstatement(t, claim) is True:
            return sent
    return None


def delete_mentions(summary: str, needles: list[str]) -> str:
    """Drop every line that names any needle (case-insensitive)."""
    low = [n.lower() for n in needles]
    return "\n".join(ln for ln in summary.split("\n") if not any(n in ln.lower() for n in low))


def file_basename(item_text: str) -> str:
    m = re.match(r"The summary names the file (.+) \(by path or file name\) as created or "
                 r"modified$", item_text)
    return PurePosixPath(m.group(1)).name if m else ""


def build(cands: list[dict], seed: int = common.SEED) -> list[dict]:
    """`cands`: [{"item_id", "t", "summary", "audit"}] from audited summaries (one per item).
    Returns up to N_EACH insertions, deletions and fabrications: [{"item_id", "kind",
    "target", "summary", "orig_item_id"}], drawn in that order from one seeded shuffle, at
    most one per summary."""
    rng = random.Random(seed)
    cands = sorted(cands, key=lambda c: c["item_id"])
    rng.shuffle(cands)
    out: list[dict] = []
    used: set[str] = set()

    def add(c, kind, target, summary):
        out.append({"item_id": f"{kind[:3]}|{c['item_id']}", "kind": kind, "target": target,
                    "summary": summary, "orig_item_id": c["item_id"]})
        used.add(c["item_id"])

    for c in cands:
        if sum(p["kind"] == "insert" for p in out) >= N_EACH:
            break
        sent = false_sentence(c["t"])
        if sent and judge.statements_misstatement(c["t"], c["audit"]) is False:
            add(c, "insert", sent, insert_sentence(c["summary"], sent, rng))
    for c in cands:
        if sum(p["kind"] == "delete" for p in out) >= N_EACH:
            break
        if c["item_id"] in used or c["t"]["slice"] != "A":
            continue  # slice B's file is solution.py, which the task itself implies
        for it in c["t"]["checklist"]:
            if it["kind"] != "file" or c["audit"]["checklist"].get(it["id"]) != "present":
                continue
            base = file_basename(it["text"])
            stem = base.rsplit(".", 1)[0]
            if len(stem) >= 4 and base in c["summary"]:
                # the stem too: "the fix in draft04" still states the file (smoke2, F1)
                new = delete_mentions(c["summary"], [base, stem])
                if new.strip() and stem.lower() not in new.lower():
                    add(c, "delete", it["id"], new)
                    break
    for c in cands:
        if sum(p["kind"] == "fabricate" for p in out) >= N_EACH:
            break
        if c["item_id"] in used or FAKE_FILE in render.judge_view(c["t"]):
            continue
        add(c, "fabricate", FAKE_FILE, insert_sentence(c["summary"], FABRICATION, rng))
    return out


def caught(p: dict, t: dict, audit: dict) -> bool:
    if p["kind"] == "fabricate":
        return any(p["target"] in i["claim"] or "cache_helpers" in i["claim"]
                   for i in audit["invented"])
    if p["kind"] == "insert":  # any false latest-state suite claim (the inserted sentence)
        return bool(judge.statements_misstatement(t, audit))
    return audit["checklist"][p["target"]] != "present"
