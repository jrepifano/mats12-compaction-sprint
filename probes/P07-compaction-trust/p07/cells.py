"""P07 trust cells: a context + its summary + a cell -> the resumed messages and the sandbox
plants (probes/P07-compaction-trust/plan.md, Setup: Trust), and the plant check that runs on
every episode.

The resumed context is [system (X3 SYSTEM_PROMPT), the original task message, the wrapped
summary as a user message], as Codex rebuilds history after compaction (the summary goes in a
user message as `format!("{SUMMARY_PREFIX}\\n{summary}")`). The claim string is identical in
every location: appended to the summary (inside the wrapper), its own user message, the output
of a synthetic `cat /agent/STATUS.txt` call, or a comment on line 1 of solution.py.
"""
from __future__ import annotations

import json
import re

from p07 import api
from p07 import contracts as C

# Claude Code's wrapper (claude_code_wrapper.md, not the _kept_messages variant) names a
# transcript file. Decision (main P07 session, 2026-10-11): keep that sentence verbatim with a
# fixed path in the form Claude Code would use for user `dev` in cwd /agent. The file does not
# exist in the sandbox.
TRANSCRIPT_PATH = "/home/dev/.claude/projects/-agent/7c1e2a90-4b3d-4f6e-9a51-3d2b8f0c6e14.jsonl"
STATUS_CALL_ID = "call_p07_status"
EXPECTED_INDEX = {C.Location.SUMMARY: 2, C.Location.USER: 3, C.Location.TOOL: 4}
EXPECTED_ROLE = {C.Location.SUMMARY: "user", C.Location.USER: "user", C.Location.TOOL: "tool"}


# --- wrappers ----------------------------------------------------------------------------------
def load_wrapper(wrapper: C.Wrapper | str) -> str:
    """The verbatim wrapper template, read at call time, with exactly one `{summary}`. Codex's
    is its summary prefix + "\\n" + the summary (codex_summary_prefix.md)."""
    w = C.Wrapper(wrapper)
    if w is C.Wrapper.CODEX:
        prefix = (C.WRAPPERS_DIR / "codex_summary_prefix.md").read_text(encoding="utf-8")
        tpl = prefix + "\n" + "{summary}"
    else:
        tpl = (C.WRAPPERS_DIR / C.WRAPPER_FILES[w]).read_text(encoding="utf-8")
    _check_template(tpl, w.value)
    return tpl


def _check_template(tpl: str, name: str) -> None:
    """Exactly one `{summary}`; other braces are left alone (verbatim text may hold them)."""
    if tpl.count("{summary}") != 1:
        raise ValueError(f"wrapper {name}: needs exactly one {{summary}}")


def render_wrapper(wrapper: C.Wrapper | str, summary: str) -> str:
    """Fill the template. Plain replacement (no str.format), so braces in the summary survive;
    `{transcript_path}` is filled first so a summary containing it is left alone."""
    tpl = load_wrapper(wrapper)
    _check_template(tpl, C.Wrapper(wrapper).value)
    return tpl.replace("{transcript_path}", TRANSCRIPT_PATH).replace("{summary}", summary)


# --- build -------------------------------------------------------------------------------------
def _dump(msg, mid: str) -> dict:
    msg.id = mid
    return msg.model_dump(exclude_none=True)


def build(ctx: dict, summary_text: str, cell_name: str,
          wrapper: C.Wrapper | str = C.Wrapper.CODEX) -> C.Resume:
    from inspect_ai.model import (
        ChatMessageAssistant,
        ChatMessageSystem,
        ChatMessageTool,
        ChatMessageUser,
    )
    from inspect_ai.tool import ToolCall

    cell = C.CELL_BY_NAME[cell_name]
    w = C.Wrapper(wrapper)
    pre = ctx["prefix_messages"]
    if pre[0]["role"] != "system" or pre[1]["role"] != "user":
        raise ValueError(f"{ctx['ctx_id']}: prefix must start with system, user")
    p = C.paraphrase_index(ctx["ctx_id"])
    claim = (C.claim_text(cell.level, p, ctx["n_tests"]) if cell.level is not None else None)
    loc = cell.location

    body = summary_text + "\n\n" + claim if loc is C.Location.SUMMARY else summary_text
    base = f"p07-{C.ctx_key(ctx['ctx_id'])}-{cell.name}-{w.value}"
    msgs = [
        _dump(ChatMessageSystem(content=api._text_of(pre[0].get("content"))), f"{base}-0"),
        _dump(ChatMessageUser(content=api._text_of(pre[1].get("content"))), f"{base}-1"),
        _dump(ChatMessageUser(content=render_wrapper(w, body)), f"{base}-2"),
    ]
    file_comment = status_file = None
    if loc is C.Location.USER:
        msgs.append(_dump(ChatMessageUser(content=claim), f"{base}-3"))
    elif loc is C.Location.TOOL:
        status_file = claim + "\n"
        tc = ToolCall(id=STATUS_CALL_ID, function="execute_command",
                      arguments={"command": C.STATUS_CMD})
        msgs.append(_dump(ChatMessageAssistant(content="", tool_calls=[tc]), f"{base}-3"))
        msgs.append(_dump(ChatMessageTool(content=status_file, tool_call_id=STATUS_CALL_ID,
                                          function="execute_command"), f"{base}-4"))
    elif loc is C.Location.FILE:
        file_comment = C.FILE_COMMENT_PREFIX + claim
    return C.Resume(messages=tuple(msgs), claim=claim, file_comment=file_comment,
                    status_file=status_file, cell=cell.name, wrapper=w.value, paraphrase=p)


# --- plant check -------------------------------------------------------------------------------
def _claim_patterns() -> dict[tuple[C.Level, int], re.Pattern]:
    """Every claim template as a regex, any test count."""
    out = {}
    for lv, paras in C.CLAIMS.items():
        for i, t in enumerate(paras):
            rx = re.escape(t).replace(re.escape("{n5}"), r"\d+").replace(re.escape("{n}"), r"\d+")
            out[(lv, i)] = re.compile(rx)
    return out


CLAIM_PATTERNS = _claim_patterns()


def _message_text(m: dict) -> str:
    """Everything the model sees in a message: text and reasoning parts, tool call arguments."""
    c = m.get("content")
    parts = [c] if isinstance(c, str) else [
        x.get("text") or x.get("reasoning") or "" for x in (c or []) if isinstance(x, dict)]
    for tc in m.get("tool_calls") or []:
        parts.append(tc.get("function") or "")
        parts.extend(v if isinstance(v, str) else json.dumps(v)
                     for v in (tc.get("arguments") or {}).values())
    return "\n".join(parts)


def plant_check(resume: C.Resume, solution_py_after_plant: str | None,
                status_txt: str | None) -> dict:
    """ok iff the claim appears exactly once and only where the cell puts it, and no other
    claim (any level, any paraphrase) appears anywhere. `file_lines` is the number of lines of
    solution.py holding a claim string. `problems` lists every failed condition."""
    cell = C.CELL_BY_NAME[resume.cell]
    loc, claim = cell.location, resume.claim
    probs: list[str] = []
    texts = [_message_text(m) for m in resume.messages]
    sol = solution_py_after_plant

    count, where = 0, []
    if claim:
        for i, t in enumerate(texts):
            k = t.count(claim)
            if k:
                count += k
                where.append(f"{i}:{resume.messages[i]['role']}x{k}")
    file_lines = sum(any(rx.search(line) for rx in CLAIM_PATTERNS.values())
                     for line in (sol or "").splitlines())

    # the claim belongs to the cell
    if (claim is None) != (cell.level is None):
        probs.append("claim presence does not match the cell")
    if claim is not None and claim != C.claim_text(cell.level, resume.paraphrase,
                                                   _n_from(claim, cell.level, resume.paraphrase)):
        probs.append("claim is not this cell's level/paraphrase")
    # other claims anywhere (context, solution.py, STATUS.txt)
    own = (cell.level, resume.paraphrase) if cell.level else None
    for key, rx in CLAIM_PATTERNS.items():
        for name, t in [(f"msg{i}", x) for i, x in enumerate(texts)] + [
                ("solution.py", sol or ""), ("STATUS.txt", status_txt or "")]:
            hits = rx.findall(t)
            if key != own and hits:
                probs.append(f"foreign claim {key[0].value}/p{key[1]} in {name}")
            elif key == own and claim and any(h != claim for h in hits):
                probs.append(f"claim with a different test count in {name}")

    if loc in EXPECTED_INDEX:
        idx = EXPECTED_INDEX[loc]
        if count != 1:
            probs.append(f"claim occurs {count}x in context, expected 1")
        if len(texts) <= idx or claim not in texts[idx] \
                or resume.messages[idx]["role"] != EXPECTED_ROLE[loc]:
            probs.append(f"claim not in message {idx} ({EXPECTED_ROLE[loc]})")
        if len(resume.messages) != idx + 1 and loc is not C.Location.SUMMARY:
            probs.append(f"{len(resume.messages)} messages, expected {idx + 1}")
    elif count:
        probs.append(f"claim occurs {count}x in context, expected 0")
    if loc not in (C.Location.USER, C.Location.TOOL) and len(resume.messages) != 3:
        probs.append(f"{len(resume.messages)} messages, expected 3")

    # solution.py
    if loc is C.Location.FILE:
        if resume.file_comment != C.FILE_COMMENT_PREFIX + (claim or ""):
            probs.append("file_comment is not the prefixed claim")
        if sol is None:
            probs.append("solution.py not given for the file cell")
        else:
            first = sol.splitlines()[0] if sol else ""
            if first != resume.file_comment:
                probs.append("solution.py line 1 is not the file comment")
            if sol.count(claim) != 1:
                probs.append(f"claim occurs {sol.count(claim)}x in solution.py, expected 1")
    else:
        if resume.file_comment is not None:
            probs.append("file_comment set outside the file cell")
        if sol is not None and claim and claim in sol:
            probs.append("claim in solution.py")
    # STATUS.txt
    if loc is C.Location.TOOL:
        tcs = resume.messages[3].get("tool_calls") or [] if len(resume.messages) > 3 else []
        if [(t.get("id"), t.get("function"), t.get("arguments")) for t in tcs] != [
                (STATUS_CALL_ID, "execute_command", {"command": C.STATUS_CMD})] \
                or resume.messages[4].get("tool_call_id") != STATUS_CALL_ID:
            probs.append("message 3/4 is not the STATUS.txt call and its result")
        if resume.status_file != claim + "\n" or status_txt != claim + "\n":
            probs.append("STATUS.txt is not claim + newline")
    elif resume.status_file is not None or status_txt is not None:
        probs.append("STATUS.txt present outside the tool cell")

    return {"ok": not probs, "count_in_context": count, "where": where,
            "file_lines": file_lines, "problems": probs}


def _n_from(claim: str, level: C.Level, p: int) -> int:
    """The test count a claim was rendered with (the first number after the fixed prefix)."""
    tpl = C.CLAIMS[level][p]
    rx = re.escape(tpl).replace(re.escape("{n5}"), r"(?P<n5>\d+)").replace(
        re.escape("{n}"), r"(?P<n>\d+)", 1).replace(re.escape("{n}"), r"(?P=n)")
    m = re.fullmatch(rx, claim)
    return int(m.group("n")) if m else -1
