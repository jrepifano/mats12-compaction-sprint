"""P09 cells: a context + its summary + a content + a wrapper -> the resumed messages and the
sandbox plant (plan.md "Setup"), and the context-side plant check.

Ported from P07 (`probes/P07-compaction-trust/p07/cells.py`). The resumed context is P07's:
[system (X3 SYSTEM_PROMPT), the original task message, the wrapped summary as a user message],
as Codex rebuilds history after compaction. For T and H the plant sentence is appended to the
summary (inside the wrapper) after a blank line, so every wrapper carries the same sentence.
"""
from __future__ import annotations

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


def build(ctx: dict, summary_text: str, content: C.Content | str,
          wrapper: C.Wrapper | str, tasks: dict | None = None) -> C.Resume:
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser

    content, w = C.Content(content), C.Wrapper(wrapper)
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
