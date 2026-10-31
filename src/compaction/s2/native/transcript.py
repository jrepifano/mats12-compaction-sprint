"""A native Claude Code transcript in the shape experiment 1's scorers read (week-2 harness
replication; plan v0.3.1, "Secondaries": tested adapters, explicit task-2 cutoffs).

- `score_messages(rows)`: every main-chain message row of the session file, in file order,
  through the pilot's converter (`claude_code.to_messages`), then: the `Bash` tool renamed
  `bash` (`score.bash_steps` reads only that name), and each tool result wrapped as
  mini-swe-agent's JSON observation `{"returncode", "output"}`, so `score._rc` and
  `score._body` read it. The return code comes from the result's leading "Exit code N" line,
  else `is_error` (1), else 0; a result whose code can't be told stays unwrapped (unknown).
- `task2_index(msgs, prompt)`: the index of the user message that delivers the task-2 prompt
  (the second task's opening, as `b_message_index`), or None when it isn't there: the
  scorer then treats the second task's commands as unknown, never as none.
"""

from __future__ import annotations

import json
import re

from compaction.s2.native import claude_code as CCN

SHELL_TOOLS = {"Bash": "bash"}
EXIT_RE = re.compile(r"^\s*Exit code (-?\d+)\b")


def _tool_results(rows: list[dict]) -> dict[str, dict]:
    """tool_use_id -> {"is_error", "text"} for every tool_result block in the file."""
    out = {}
    for r in rows:
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        for blk in m.get("content") or [] if isinstance(m.get("content"), list) else []:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                # Claude Code's Bash results always carry is_error (true or false; pilot
                # transcript, 158/158); a missing one is unknown, not success
                out[blk.get("tool_use_id")] = {"is_error": blk.get("is_error")}
    return out


def returncode(text: str, is_error: bool | None) -> int | None:
    m = EXIT_RE.match(text or "")
    if m:
        return int(m.group(1))
    if is_error is None:
        return None
    return 1 if is_error else 0


def score_messages(rows: list[dict]) -> tuple[list[dict], dict]:
    idx = [i for i, r in enumerate(rows) if CCN._is_msg(r) or CCN._rendered(r) is not None]
    msgs, _srcs, info = CCN.to_messages(rows, idx)
    results = _tool_results(rows)
    shell_ids = set()
    renamed = 0
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") in SHELL_TOOLS:
                fn["name"] = SHELL_TOOLS[fn["name"]]
                shell_ids.add(tc.get("id"))
                renamed += 1
    unknown_rc = 0
    for m in msgs:
        if m.get("role") != "tool" or m.get("tool_call_id") not in shell_ids:
            continue
        text = m.get("content") if isinstance(m.get("content"), str) else ""
        rc = returncode(text, (results.get(m["tool_call_id"]) or {}).get("is_error"))
        if rc is None:
            unknown_rc += 1
            continue
        m["content"] = json.dumps({"returncode": rc, "output": text}, ensure_ascii=False)
    info |= {"shell_calls": renamed, "shell_rc_unknown": unknown_rc}
    return msgs, info


def task2_index(msgs: list[dict], prompt: str) -> int | None:
    """The first user message whose text starts with the task-2 prompt (whitespace-trimmed)."""
    want = prompt.strip()[:200]
    for i, m in enumerate(msgs):
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, str) and c.strip().startswith(want):
            return i
    return None
