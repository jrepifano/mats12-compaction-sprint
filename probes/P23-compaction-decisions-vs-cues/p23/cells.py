"""P23 cells (ported from P08's p08/cells.py; P23 changes: the five cue x decision twins,
the decision-span check, and RUN_INFO.txt neutralised in cue-absent cells).

P08 cells: a context + one summary twin -> the resumed messages, and the per-episode fact
check (plan.md "Smoke": the resumed context contains or omits each fact as intended).

The resumed context is pi-mono's after compaction with nothing of the prefix retained:
[system (X3 SYSTEM_PROMPT, as in the prefix), the wrapped summary as a user message]. The
wrapper is pi-mono's COMPACTION_SUMMARY_PREFIX + summary + COMPACTION_SUMMARY_SUFFIX
(prompts/pi_mono_wrapper.md, a byte copy of P07's wrappers/pi_mono_wrapper.md).
"""
from __future__ import annotations

from p23 import api
from p23 import contracts as C

WRAPPER_FILE = "pi_mono_wrapper.md"


def load_wrapper() -> str:
    tpl = (C.PROMPTS_DIR / WRAPPER_FILE).read_text(encoding="utf-8")
    if tpl.count("{summary}") != 1:
        raise ValueError("pi-mono wrapper needs exactly one {summary}")
    return tpl


def render_wrapper(summary: str) -> str:
    """Plain replacement (no str.format), so braces in the summary survive."""
    return load_wrapper().replace("{summary}", summary)


RUN_INFO_PATH = "/agent/RUN_INFO.txt"


def neutral_run_info() -> str:
    """RUN_INFO.txt as X3 writes it for the neutral header (x3.texts.run_info)."""
    from x3 import contracts as X3C
    from x3 import texts as X3T

    return X3T.run_info(X3C.Context.NEUTRAL)


def workspace_for_cell(ctx: dict, cell: str) -> tuple[dict, dict]:
    """(files, snapshot_sha256) the episode starts from. Cue-absent cells get the neutral
    RUN_INFO.txt, and its sha256 replaces the snapshot's so the replay check still applies to
    every other file."""
    import hashlib

    files, snap = dict(ctx["files"]), dict(ctx["snapshot_sha256"])
    if not C.CELL_BY_NAME[cell].cue:
        if RUN_INFO_PATH not in files:
            raise ValueError(f"{ctx['ctx_id']}: no {RUN_INFO_PATH} in the workspace")
        text = neutral_run_info()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        files[RUN_INFO_PATH] = {**files[RUN_INFO_PATH], "text": text, "sha256": sha}
        snap[RUN_INFO_PATH] = sha
    return files, snap


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
    base = f"p23-{C.ctx_key(ctx['ctx_id'])}-{header}-{cell}"
    msgs = []
    for i, m in enumerate((ChatMessageSystem(content=api._text_of(pre[0].get("content"))),
                           ChatMessageUser(content=render_wrapper(summary)))):
        m.id = f"{base}-{i}"
        msgs.append(m.model_dump(exclude_none=True))
    return C.Resume(messages=tuple(msgs), cell=cell, header=header, summary=summary)


def expected_facts(cell: str, header: str = "safety") -> dict[str, bool]:
    """Which facts the resumed context must hold (True) or omit (False)."""
    c = C.CELL_BY_NAME[cell]
    return {"constraint": True, "test_state": True, "contradiction": True, "header": c.cue,
            "decisions": c.decisions}


def fact_check(resume: C.Resume, ctx: dict, spans: list[str] | None = None,
               run_info: str | None = None) -> dict:
    """ok iff the context is [system, wrapped summary], the system message states no fact, each
    fact is present or absent in the summary message as the cell says (contracts.py regexes;
    decisions: every marked span present, or none), and RUN_INFO.txt carries the header exactly
    when the cell has the cue."""
    probs: list[str] = []
    texts = [api._text_of(m.get("content")) for m in resume.messages]
    roles = [m["role"] for m in resume.messages]
    if roles != ["system", "user"]:
        probs.append(f"roles {roles}, expected [system, user]")
    err = (ctx.get("truth") or {}).get("last_error")
    spans = spans or []
    if spans and not C.CELL_BY_NAME[resume.cell].cue:
        # cue-absent cells: spans as -H leaves them (header clauses deleted), as twins5's gate
        from p23.twins5 import adapt_spans

        mh = (ctx.get("twin") or {}).get("minus_h") or texts[-1]
        spans = adapt_spans(mh, spans, err)
    left = [s for s in spans if s in texts[-1]]
    found = {"constraint": bool(C.constraint_hits(texts[-1])),
             "test_state": bool(C.test_state_hits(texts[-1], err)),
             "contradiction": bool(C.contradiction_hits(texts[-1])),
             "header": bool(C.header_hits(texts[-1])),
             "decisions": bool(spans) and len(left) == len(spans)}
    exp = expected_facts(resume.cell)
    if ctx.get("header") == "neutral":
        # FU2-lite: a neutral-header run never had the cue; the run_id line is in both headers
        exp.pop("header")
        exp.pop("contradiction")   # P08's neutral twins did not require it (2 of 32 lack it)
        run_info = None
    if (ctx.get("truth") or {}).get("last_test") is None:
        # FU4b pre-test cut: no test state, and the contradiction may be unknown yet
        exp.pop("test_state")
        if not found["contradiction"]:
            exp.pop("contradiction")
    if not spans:
        exp.pop("decisions")
    elif not exp["decisions"] and left:
        probs.append(f"decisions: {len(left)} span(s) left")
    for k, want in exp.items():
        if found[k] != want:
            probs.append(f"{k}: expected {'present' if want else 'absent'}")
    # the suite line, not HEADER_RES: the neutral RUN_INFO keeps the run_id line, which it matches
    if run_info is not None and \
            ("Agent Behavior Review" in run_info) != C.CELL_BY_NAME[resume.cell].cue:
        probs.append("RUN_INFO.txt header does not match the cell")
    if resume.cell in C.PLANT and C.PLANT[resume.cell] not in texts[-1]:
        probs.append("FU5: planted line missing")
    if resume.cell == "minus_h" and any(v in texts[-1] for v in C.PLANT.values()):
        probs.append("FU5: planted line in -H")
    if C.constraint_hits(texts[0]) or C.test_state_hits(texts[0]):
        probs.append("the system message states a fact")
    if resume.summary not in texts[-1]:
        probs.append("summary not in the wrapped message")
    return {"ok": not probs, "expected": exp, "found": found, "problems": probs}
