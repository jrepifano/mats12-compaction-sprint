"""The pre-action window (prereg Amendment B2, Jacob 2026-10-05): the first assistant turn whose
reasoning text is readable, among the turns before any workspace file has changed. A turn's
reasoning precedes its own action, so the turn that makes the first change is still eligible.

"Changed" is judged per snapshot against step 0, as the detector does: a file new, deleted, or
with a different sha256 or mode. Caches (`__pycache__`, `.pytest_cache`) and the shell/tool
history files under the home directory are not changes; writing `solution.py` is.

Amendment B2 replaces "the first assistant turn" (v0.2): GLM 5.3 Flash skips reasoning on its
first turn (a bare `cat description.md`) in 29 of 30 calibration episodes and reasons from
turn 2 on. The episode store's `first_turn` field keeps the old definition for provenance.
"""
from __future__ import annotations

from x3 import contracts as C
from x3.detector import _BENIGN_DIRS, _BENIGN_HOME, _parts, _under


def _ignorable(path: str) -> bool:
    if any(d in path.split("/") for d in _BENIGN_DIRS):
        return True
    return (_under(path, C.HOME) and not _under(path, C.USER_SITE)
            and _parts(path, C.HOME)[0] in _BENIGN_HOME)


def _snapshot_changed(files: dict | None, base: dict) -> bool:
    """True if this snapshot differs from step 0 outside the ignorable paths. An unreadable
    snapshot counts as changed (the window cannot be shown to precede it)."""
    if not isinstance(files, dict):
        return True
    for path in set(files) | set(base):
        if _ignorable(path):
            continue
        cur, b = files.get(path), base.get(path)
        if cur is None or b is None:
            return True
        if (cur.get("sha256") is None or cur.get("sha256") != b.get("sha256")
                or cur.get("mode") != b.get("mode")):
            return True
    return False


def first_change_turn(art: dict) -> int | None:
    """The assistant turn (1-based) whose tool call first changed the workspace, or None."""
    snaps = art.get("snapshots") or []
    if not snaps or not isinstance(snaps[0], dict) or not isinstance(snaps[0].get("files"),
                                                                       dict):
        return 0                      # no pristine snapshot: no turn can be shown to precede
    base = snaps[0]["files"]
    by_step = {s.get("step"): s for s in snaps if isinstance(s, dict)}
    for call in sorted(art.get("calls") or [], key=lambda c: c.get("snapshot_step", 0)):
        snap = by_step.get(call.get("snapshot_step"))
        if snap is None or _snapshot_changed(snap.get("files"), base):
            return int(call["turn"])
    return None


def _reasoning(msg: dict) -> tuple[str, bool]:
    """(readable reasoning text, any block redacted), as task.reasoning_text reads it."""
    content = msg.get("content")
    if not isinstance(content, list):
        return "", False
    parts, redacted = [], False
    for c in content:
        if c.get("type") != "reasoning":
            continue
        if c.get("redacted"):
            redacted = True
            if c.get("summary"):
                parts.append(c["summary"])
        elif c.get("reasoning"):
            parts.append(c["reasoning"])
    return "\n".join(p for p in parts if p), redacted


def _visible(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text") or "" for c in content or [] if c.get("type") == "text")


def pre_action_window(art: dict) -> dict:
    """{turn, reasoning, visible, redacted, reasoning_readable, first_change_turn}. `turn` is
    None when no eligible turn has readable reasoning (then reasoning_readable is False)."""
    last = first_change_turn(art)
    asst = [m for m in art.get("messages") or [] if m.get("role") == "assistant"]
    for i, m in enumerate(asst, start=1):
        if last is not None and i > last:
            break
        text, redacted = _reasoning(m)
        if text and not redacted:
            return {"turn": i, "reasoning": text, "visible": _visible(m), "redacted": False,
                    "reasoning_readable": True, "first_change_turn": last}
    return {"turn": None, "reasoning": "", "visible": "", "redacted": False,
            "reasoning_readable": False, "first_change_turn": last}
