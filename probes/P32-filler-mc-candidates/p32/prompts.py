"""P32 prompts: P06's Brauer-format scaffolds (varbind: `p06.brauer`; chain: `p06.f5_chain_brauer`)
with an options line placed before or after the filler, and a neutral padding control.

Conditions are strings "<n>|<kind>|<arm>":
    n     0 (free response) or 2, 5, 10 options
    kind  "-" for free response, else "near" | "far"
    arm   base         no filler
          pad          a fixed prose passage (`pad.txt`, 488 V4 tokens with its label vs 503 for
                       `Filler: ` + 500 dots) at the start of each user turn, before the problem
          padmid       the same passage in the filler's place: after the question (free response)
                       or after the options (MC), so the options sit as far back as in dots_before
                       with no dots (the distance control; side-agent note, 2026-10-13, before data)
          dots         free response: `Filler: . . .` (500 spaced dots) after the question
          dots_before  MC: options right after the question, then the filler
          dots_after   MC: the filler, then the options (inside the 128-token window at `Answer:`)
Every user turn carries the same arm (shots too, with their own options for the same n and kind);
assistant turns are the bare answer; no prefill. Options are bare values (`Options: 3, 9, 7`).
"""

from __future__ import annotations

from pathlib import Path

from p06 import brauer

import p32  # noqa: F401

from .items import KINDS, OPTION_COUNTS, options

N_DOTS = 500
DOTS = brauer.dots(N_DOTS)
PAD = (Path(__file__).with_name("pad.txt")).read_text().strip()
MC_SYS = brauer._MC_SYS  # " The answer is one of the listed options."
FILLER_SYS = brauer._SYS_FILLER
PAD_SYS = " Each turn starts with a passage that is unrelated to the problem; ignore it."
PADMID_SYS = (" After the question, there will be a passage that is unrelated to the problem; "
              "ignore it.")
FREE_ARMS = ("base", "pad", "padmid", "dots")
MC_ARMS = ("base", "pad", "padmid", "dots_before", "dots_after")


def conditions() -> list[str]:
    out = [f"0|-|{a}" for a in FREE_ARMS]
    out += [f"{n}|{k}|{a}" for n in OPTION_COUNTS for k in KINDS for a in MC_ARMS]
    return out


def parse(cond: str) -> tuple[int, str, str]:
    n, kind, arm = cond.split("|")
    return int(n), kind, arm


def _problem(item: dict) -> str:
    if item["family"] == "varbind":
        block = "\n".join(f"{n} = {v}" for n, v in item["definitions"])
        return f"{block}\nQuestion: {item['question']}"
    return f"Problem: {item['problem']}"


def user_turn(item: dict, cond: str) -> str:
    n, kind, arm = parse(cond)
    q = _problem(item)
    opts = ("Options: " + ", ".join(str(o) for o in options(item, kind, n))) if n else None
    filler = f"Filler: {DOTS}"
    if arm == "base":
        return f"{q}\n{opts}\n\nAnswer:" if opts else f"{q}\n\nAnswer:"
    if arm == "pad":
        body = f"{q}\n{opts}" if opts else q
        return f"Passage: {PAD}\n\n{body}\n\nAnswer:"
    if arm == "padmid":
        body = f"{q}\n{opts}" if opts else q
        return f"{body}\n\nPassage: {PAD}\n\nAnswer:"
    if arm == "dots":
        assert not opts, cond
        return f"{q}\n\n{filler}\n\nAnswer:"
    assert opts, cond
    if arm == "dots_before":
        return f"{q}\n{opts}\n\n{filler}\n\nAnswer:"
    if arm == "dots_after":
        return f"{q}\n\n{filler}\n\n{opts}\n\nAnswer:"
    raise ValueError(cond)


def system(item: dict, cond: str) -> str:
    n, _, arm = parse(cond)
    s = brauer._SYS if item["family"] == "varbind" else item["instruction"]
    if n:
        s += MC_SYS
    if arm.startswith("dots"):
        s += FILLER_SYS
    elif arm == "pad":
        s += PAD_SYS
    elif arm == "padmid":
        s += PADMID_SYS
    return s


def messages(item: dict, shots: list[dict], cond: str) -> list[dict]:
    msgs = [{"role": "system", "content": system(item, cond)}]
    for s in shots:
        msgs += [{"role": "user", "content": user_turn(s, cond)},
                 {"role": "assistant", "content": str(s["answer"])}]
    msgs.append({"role": "user", "content": user_turn(item, cond)})
    return msgs
