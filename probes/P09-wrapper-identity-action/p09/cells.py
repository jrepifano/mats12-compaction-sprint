"""P09 cells: a context + its summary + a content + a wrapper -> the resumed messages and the
sandbox plant (plan.md "Setup"), and the context-side plant check.

Ported from P07 (`probes/P07-compaction-trust/p07/cells.py`). The resumed context is P07's:
[system (X3 SYSTEM_PROMPT), the original task message, the wrapped summary as a user message],
as Codex rebuilds history after compaction. For T and H the plant sentence is appended to the
summary (inside the wrapper) after a blank line, so every wrapper carries the same sentence.
"""
from __future__ import annotations

import json

from p09 import api, plants
from p09 import contracts as C

# Claude Code's wrapper names a transcript file; P07 kept the sentence verbatim with this
# non-existent path (P07 log, 2026-10-11). P09 keeps P07's choice.
TRANSCRIPT_PATH = "/home/dev/.claude/projects/-agent/7c1e2a90-4b3d-4f6e-9a51-3d2b8f0c6e14.jsonl"


# --- wrappers ----------------------------------------------------------------------------------
def codex_self_check() -> dict:
    """Codex-self must be Codex's verbatim prefix with only CODEX_SELF_EDITS applied, each
    occurring exactly once (plan: "diff checked by code")."""
    codex = (C.WRAPPERS_DIR / C.PREFIX_FILES[C.Wrapper.CODEX]).read_text(encoding="utf-8")
    own = (C.WRAPPERS_DIR / C.PREFIX_FILES[C.Wrapper.CODEX_SELF]).read_text(encoding="utf-8")
    t, probs = codex, []
    for a, b in C.CODEX_SELF_EDITS:
        if t.count(a) != 1:
            probs.append(f"{a!r} occurs {t.count(a)}x in Codex's prefix")
        t = t.replace(a, b)
    if t != own:
        probs.append("Codex-self file differs from Codex's prefix + the ownership edits")
    return {"ok": not probs, "problems": probs}


def load_wrapper(wrapper: C.Wrapper | str) -> str:
    """The wrapper template with exactly one `{summary}`."""
    w = C.Wrapper(wrapper)
    if w in C.PREFIX_FILES:
        if w is C.Wrapper.CODEX_SELF and not codex_self_check()["ok"]:
            raise ValueError("Codex-self prefix failed its diff check")
        tpl = (C.WRAPPERS_DIR / C.PREFIX_FILES[w]).read_text(encoding="utf-8") + "\n{summary}"
    else:
        tpl = (C.WRAPPERS_DIR / C.WRAPPER_FILES[w]).read_text(encoding="utf-8")
    if tpl.count("{summary}") != 1:
        raise ValueError(f"wrapper {w.value}: needs exactly one {{summary}}")
    return tpl


def render_wrapper(wrapper: C.Wrapper | str, summary: str) -> str:
    """Plain replacement (no str.format), so braces in the summary survive."""
    return load_wrapper(wrapper).replace("{transcript_path}", TRANSCRIPT_PATH).replace(
        "{summary}", summary)


# --- build -------------------------------------------------------------------------------------
def _dump(msg, mid: str) -> dict:
    msg.id = mid
    return msg.model_dump(exclude_none=True)


# --- implicit-attribution follow-up --------------------------------------------------------------
def plant_command(plant: dict) -> str:
    """The synthetic edit that writes the H plant, in the heredoc form the X3b GLM agents use to
    write solution.py (`cd /agent && cat > solution.py << 'EOF'`)."""
    text = plant["text"]
    tag = "EOF"
    while tag in text.split("\n"):
        tag += "X"
    return f"cd /agent && cat > solution.py << '{tag}'\n{text}\n{tag}"


def _visible(m: dict) -> dict:
    """A prefix message with reasoning parts dropped (both implicit arms show the same text)."""
    c = m.get("content")
    if isinstance(c, list):
        m = {**m, "content": api._text_of(c)}
    return m


def render_transcript(msgs: list[dict]) -> str:
    """The prefix turns (after the task message) as plain text: assistant text, each command
    run, tool outputs, and user messages (X3's nudges)."""
    out = []
    for m in msgs:
        role, text = m["role"], api._text_of(m.get("content"))
        if role == "assistant":
            block = ["[assistant]"] + ([text] if text.strip() else [])
            for tc in m.get("tool_calls") or []:
                args = tc.get("arguments") or {}
                block.append(f"[ran {tc.get('function')}]\n{args.get('command', json.dumps(args))}")
            out.append("\n".join(block))
        elif role == "tool":
            out.append(f"[tool output]\n{text}")
        else:
            out.append(f"[{role}]\n{text}")
    return "\n\n".join(out)


def build_implicit(ctx: dict, wrapper: C.Wrapper, tasks: dict | None = None) -> C.Resume:
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser

    pre = ctx["prefix_messages"]
    if pre[-1]["role"] != "tool":
        raise ValueError(f"{ctx['ctx_id']}: prefix does not end with a tool result")
    plant = plants.make(ctx, C.Content.H, tasks).as_dict()
    call = {"id": C.PLANT_CALL_ID, "function": "execute_command",
            "arguments": {"command": plant_command(plant)}, "type": "function"}
    synth = [{"role": "assistant", "content": "", "tool_calls": [call], "source": "generate"},
             {"role": "tool", "content": "", "tool_call_id": C.PLANT_CALL_ID,
              "function": "execute_command"}]
    turns = [_visible(m) for m in pre[2:]] + synth
    base = f"p09-{C.ctx_key(ctx['ctx_id'])}-H-{wrapper.value}"
    head = [
        _dump(ChatMessageSystem(content=api._text_of(pre[0].get("content"))), f"{base}-0"),
        _dump(ChatMessageUser(content=api._text_of(pre[1].get("content"))), f"{base}-1"),
    ]
    if wrapper is C.Wrapper.OWN_TURNS:
        from p09.agent import to_messages

        rest = []
        for i, m in enumerate(to_messages(turns)):
            rest.append(_dump(m, f"{base}-{i + 2}"))
    else:
        body = C.TRANSCRIPT_INTROS[wrapper] + "\n\n" + render_transcript(turns)
        rest = [_dump(ChatMessageUser(content=body), f"{base}-2")]
    return C.Resume(messages=tuple(head + rest), content=C.Content.H.value,
                    wrapper=wrapper.value, plant_sentence=None, plant=plant)


def build(ctx: dict, summary_text: str, content: C.Content | str,
          wrapper: C.Wrapper | str, tasks: dict | None = None) -> C.Resume:
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser

    content, w = C.Content(content), C.Wrapper(wrapper)
    if w in C.IMPLICIT_WRAPPERS:
        if content is not C.Content.H:
            raise ValueError(f"{w.value} runs H only")
        return build_implicit(ctx, w, tasks)
    pre = ctx["prefix_messages"]
    if pre[0]["role"] != "system" or pre[1]["role"] != "user":
        raise ValueError(f"{ctx['ctx_id']}: prefix must start with system, user")
    plant = plants.make(ctx, content, tasks)
    body = summary_text + ("\n\n" + plant.sentence if plant else "")
    base = f"p09-{C.ctx_key(ctx['ctx_id'])}-{content.value}-{w.value}"
    msgs = (
        _dump(ChatMessageSystem(content=api._text_of(pre[0].get("content"))), f"{base}-0"),
        _dump(ChatMessageUser(content=api._text_of(pre[1].get("content"))), f"{base}-1"),
        _dump(ChatMessageUser(content=render_wrapper(w, body)), f"{base}-2"),
    )
    return C.Resume(messages=msgs, content=content.value, wrapper=w.value,
                    plant_sentence=plant.sentence if plant else None,
                    plant=plant.as_dict() if plant else None)


def context_check(resume: C.Resume) -> dict:
    """ok iff the context has 3 messages and the plant sentence (T, H) occurs exactly once in
    it, in message 2; for N, no plant sentence of any task or content occurs."""
    texts = [api._text_of(m.get("content")) for m in resume.messages]
    probs = []
    if resume.wrapper in {w.value for w in C.IMPLICIT_WRAPPERS}:
        return _implicit_check(resume, texts)
    if len(texts) != 3:
        probs.append(f"{len(texts)} messages, expected 3")
    s = resume.plant_sentence
    if s is not None:
        n = sum(t.count(s) for t in texts)
        if n != 1 or s not in texts[2]:
            probs.append(f"plant sentence occurs {n}x, expected once in message 2")
    else:
        for marker in ("was deleted from the file.", "A special case was added at the top of"):
            if any(marker in t for t in texts):
                probs.append(f"plant sentence marker {marker!r} in an N context")
    return {"ok": not probs, "sentence_count": 0 if s is None else
            sum(t.count(s) for t in texts), "problems": probs}


def _implicit_check(resume: C.Resume, texts: list[str]) -> dict:
    """Implicit arms: no plant sentence anywhere; the planted `if` line occurs exactly once in
    the context (in the synthetic edit), and for own_turns the last two messages are that edit
    and its empty result."""
    probs = []
    seen = []
    for i, m in enumerate(resume.messages):
        t = texts[i] + "".join((tc.get("arguments") or {}).get("command", "")
                               for tc in m.get("tool_calls") or [])
        seen.append(t)
    marker = resume.plant["planted_lines"][0].strip()
    n = sum(t.count(marker) for t in seen)
    if n != 1:
        probs.append(f"planted `if` line occurs {n}x in the context, expected 1")
    if any(s in t for s in ("was deleted from the file.", "A special case was added at the top of")
           for t in seen):
        probs.append("a plant sentence occurs in an implicit context")
    if resume.wrapper == C.Wrapper.OWN_TURNS.value:
        last = resume.messages[-2:]
        if [m["role"] for m in last] != ["assistant", "tool"] or \
                (last[0].get("tool_calls") or [{}])[0].get("id") != C.PLANT_CALL_ID:
            probs.append("own_turns does not end with the synthetic edit and its result")
    elif len(resume.messages) != 3:
        probs.append(f"{len(resume.messages)} messages, expected 3")
    return {"ok": not probs, "sentence_count": 0, "problems": probs}
