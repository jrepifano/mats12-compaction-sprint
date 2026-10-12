"""P08 cells: a context + one summary twin -> the resumed messages, and the per-episode fact
check (plan.md "Smoke": the resumed context contains or omits each fact as intended).

The resumed context is pi-mono's after compaction with nothing of the prefix retained:
[system (X3 SYSTEM_PROMPT, as in the prefix), the wrapped summary as a user message]. The
wrapper is pi-mono's COMPACTION_SUMMARY_PREFIX + summary + COMPACTION_SUMMARY_SUFFIX
(prompts/pi_mono_wrapper.md, a byte copy of P07's wrappers/pi_mono_wrapper.md).
"""
from __future__ import annotations

from p08 import api
from p08 import contracts as C

WRAPPER_FILE = "pi_mono_wrapper.md"


def load_wrapper() -> str:
    tpl = (C.PROMPTS_DIR / WRAPPER_FILE).read_text(encoding="utf-8")
    if tpl.count("{summary}") != 1:
        raise ValueError("pi-mono wrapper needs exactly one {summary}")
    return tpl


def render_wrapper(summary: str) -> str:
    """Plain replacement (no str.format), so braces in the summary survive."""
    return load_wrapper().replace("{summary}", summary)


def twin_text(twin: dict, cell: str) -> str:
    """The summary text a cell uses: twin["full"], twin["minus_c"], ... ."""
    text = twin.get(cell)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{twin.get('ctx_id')}: no {cell} twin")
    return text


def build(ctx: dict, twin: dict, cell: str) -> C.Resume:
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser

    if cell not in C.CELL_BY_NAME:
        raise ValueError(f"unknown cell {cell!r}")
    pre = ctx["prefix_messages"]
    if pre[0]["role"] != "system":
        raise ValueError(f"{ctx['ctx_id']}: prefix must start with the system message")
    header = ctx.get("header", "neutral")
    summary = twin_text(twin, cell)
    base = f"p08-{C.ctx_key(ctx['ctx_id'])}-{header}-{cell}"
    msgs = []
    for i, m in enumerate((ChatMessageSystem(content=api._text_of(pre[0].get("content"))),
                           ChatMessageUser(content=render_wrapper(summary)))):
        m.id = f"{base}-{i}"
        msgs.append(m.model_dump(exclude_none=True))
    return C.Resume(messages=tuple(msgs), cell=cell, header=header, summary=summary)


def expected_facts(cell: str, header: str) -> dict[str, bool]:
    """Which facts the resumed context must hold (True) or omit (False)."""
    drops = C.CELL_BY_NAME[cell].drops
    exp = {"constraint": drops != "constraint", "test_state": drops != "test_state"}
    if header == "safety":
        exp["header"] = drops != "header"
    return exp


def fact_check(resume: C.Resume, ctx: dict) -> dict:
    """ok iff the context is [system, wrapped summary], the system message states no fact, and
    each fact is present or absent in the summary message as the cell says (contracts.py
    regexes; the test-state check also looks for the transcript's latest error line)."""
    probs: list[str] = []
    texts = [api._text_of(m.get("content")) for m in resume.messages]
    roles = [m["role"] for m in resume.messages]
    if roles != ["system", "user"]:
        probs.append(f"roles {roles}, expected [system, user]")
    err = (ctx.get("truth") or {}).get("last_error")
    found = {"constraint": bool(C.constraint_hits(texts[-1])),
             "test_state": bool(C.test_state_hits(texts[-1], err))}
    if resume.header == "safety":
        found["header"] = bool(C.header_hits(texts[-1]))
    exp = expected_facts(resume.cell, resume.header)
    for k, want in exp.items():
        if found[k] != want:
            probs.append(f"{k}: expected {'present' if want else 'absent'}")
    if C.constraint_hits(texts[0]) or C.test_state_hits(texts[0]):
        probs.append("the system message states a fact")
    if resume.summary not in texts[-1]:
        probs.append("summary not in the wrapped message")
    return {"ok": not probs, "expected": exp, "found": found, "problems": probs}
