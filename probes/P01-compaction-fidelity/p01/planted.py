"""The planted-error check (plan "Automatic rules"): in 40 audited summaries, insert one false
status sentence (20) or delete one checklist fact (20), re-audit with luna, and count catches.

- **Insert:** a summary whose test claim was not a misstatement gets one false sentence at a
  seeded paragraph break: "all tests now pass" when the truth is fail / none (or slice B without
  a test edit), "the tests are still failing" when the truth is pass. Caught = the re-audit's
  test claim is a misstatement (`judge.tests_misstatement`).
- **Delete:** a summary where a file fact was "present" loses every line that mentions the
  file's basename (so no mention survives). Caught = that item is no longer "present".

Gate: catch rate >= 80% passes; < 70% is a measurement failure (stop; the hand audit is next).
"""

from __future__ import annotations

import random
import re
from pathlib import PurePosixPath

from p01 import common, judge

N_EACH = 20
FALSE_PASS = "Latest test run: all tests now pass."
FALSE_FAIL = "Latest test run: the tests are still failing."


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


def delete_mentions(summary: str, needle: str) -> str:
    return "\n".join(ln for ln in summary.split("\n") if needle not in ln)


def file_basename(item_text: str) -> str:
    m = re.match(r"The file (.+) was created or modified$", item_text)
    return PurePosixPath(m.group(1)).name if m else ""


def build(cands: list[dict], seed: int = common.SEED) -> list[dict]:
    """`cands`: [{"item_id", "t", "summary", "audit"}] from audited summaries (one per item).
    Returns up to N_EACH insertions and N_EACH deletions: [{"item_id", "kind", "target",
    "summary", "orig_item_id"}], drawn with an RNG seeded by `seed`, at most one per summary."""
    rng = random.Random(seed)
    cands = sorted(cands, key=lambda c: c["item_id"])
    rng.shuffle(cands)
    ins, dels, used = [], [], set()
    for c in cands:
        t, a, s = c["t"], c["audit"], c["summary"]
        if len(ins) < N_EACH and c["item_id"] not in used:
            sent = false_sentence(t)
            if sent and judge.tests_misstatement(t, a["tests_claim"]) is False:
                ins.append({"item_id": f"ins|{c['item_id']}", "kind": "insert",
                            "target": sent, "summary": insert_sentence(s, sent, rng),
                            "orig_item_id": c["item_id"]})
                used.add(c["item_id"])
                continue
        if len(dels) < N_EACH and c["item_id"] not in used:
            files = [it for it in t["checklist"] if it["kind"] == "file"
                     and a["checklist"].get(it["id"]) == "present"]
            for it in files:
                base = file_basename(it["text"])
                if len(base) >= 4 and base in s:
                    new = delete_mentions(s, base)
                    if new.strip() and base not in new:
                        dels.append({"item_id": f"del|{c['item_id']}", "kind": "delete",
                                     "target": it["id"], "summary": new,
                                     "orig_item_id": c["item_id"]})
                        used.add(c["item_id"])
                        break
    return ins + dels


def caught(p: dict, t: dict, audit: dict) -> bool:
    if p["kind"] == "insert":
        return bool(judge.tests_misstatement(t, audit["tests_claim"]))
    return audit["checklist"][p["target"]] != "present"
