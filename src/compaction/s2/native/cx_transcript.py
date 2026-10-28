"""A native Codex rollout in the shape experiment 1's scorers read (week-2 harness replication;
plan v0.3.1, "Secondaries": tested adapters, explicit task-2 cutoffs). The Codex twin of
`transcript` (Claude Code).

- `score_messages(lines)`: every message of the rollout in file order, as a record (not the
  model's context): the `compacted` lines are skipped, so a boundary never replaces the earlier
  messages (Codex appends after a compaction; it never re-emits the kept items). An exec the
  rollout never closed (the CLI was stopped mid-call, or the file ends there) is closed with an
  empty output where its turn ended (`_close_open_execs`), so its completed nested commands are
  kept (astra cx-build r1 #6). The rest is the pilot's converter (`codex.history_at`, no system
  prompt): code mode's nested `CommandExecution` calls become `bash` {"command"}. Each `bash`
  result is then the command's OWN output and exit code, from its CommandExecution item, never
  the exec's JS output (which may print several commands' outputs together or none; r1 #7),
  wrapped as mini-swe-agent's JSON observation `{"returncode", "output"}` (so `score._rc` and
  `score._body` read it). A command whose item lacks its output or its code stays unwrapped
  with an empty body: unknown, never "no rejection". A plain `function_call` shell tool
  (`exec_command` {"cmd"} or `shell` {"command"}; not seen in code mode, kept as a fallback)
  is renamed `bash`, its code read from the output's "Process exited with code N" /
  "Exit code: N" line, else unknown.
- `task2_index`: `transcript.task2_index` (the first user message that starts with the task-2
  prompt), unchanged.
- `task_usage(lines, lo, hi)`: the API calls and tokens of rollout lines [lo, hi) from the
  per-call `token_count` events (`codex.api_calls`' rule: a call is one with input tokens), and
  the plan quota (`rate_limits.primary.used_percent`) last seen before `lo`, first and last
  seen in the range.
"""

from __future__ import annotations

import json
import re

from compaction.s2.native import codex as CX
from compaction.s2.native.transcript import task2_index  # noqa: F401 - re-exported

SHELL_FUNCS = {"exec_command": "cmd", "shell": "command"}
PLAIN_RC = re.compile(r"(?:Process exited with code|Exit code:?)\s*(-?\d+)\b")


TURN_EDGES = ("task_started", "task_complete", "turn_aborted")


def _close_open_execs(lines: list[dict]) -> tuple[list[dict], int]:
    """A copy of `lines` where every exec still open when its turn ends (a `turn_context`
    line, a task_started / task_complete / turn_aborted event, or the end of the file) gets an
    empty `custom_tool_call_output` there. Returns (lines, how many were closed)."""
    out, open_exec, closed = [], [], 0

    def close() -> None:
        nonlocal closed
        for cid in open_exec:
            out.append({"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": cid, "output": "",
                "emx_synthetic": True}})
            closed += 1
        open_exec.clear()

    for x in lines:
        t, p = x.get("type"), x.get("payload") or {}
        if open_exec and (t == "turn_context" or (t == "event_msg"
                                                  and p.get("type") in TURN_EDGES)):
            close()
        if t == "response_item" and p.get("type") == "custom_tool_call":
            open_exec.append(p.get("call_id"))
        elif (t == "response_item" and p.get("type") == "custom_tool_call_output"
              and p.get("call_id") in open_exec):
            open_exec.remove(p.get("call_id"))
        out.append(x)
    close()
    return out, closed


def _own_results(lines: list[dict]) -> dict[str, tuple[int | None, str | None]]:
    """Tool call id (`<exec call_id>.<j>`, as `codex.history_at` numbers code mode's nested
    calls) -> the nested CommandExecution's own (`exit_code`, `aggregated_output`), each None
    when the item lacks it. `lines` must have no open execs (`_close_open_execs`)."""
    out: dict[str, tuple[int | None, str | None]] = {}
    nested: dict[str, list[dict]] = {}
    open_exec: list[str] = []
    for x in lines:
        t, p = x.get("type"), x.get("payload") or {}
        if t == "event_msg" and p.get("type") == "item_completed" and open_exec:
            nested[open_exec[-1]].append(p.get("item") or {})
            continue
        if t != "response_item":
            continue
        if p.get("type") == "custom_tool_call":
            nested[p.get("call_id")] = []
            open_exec.append(p.get("call_id"))
        elif p.get("type") == "custom_tool_call_output":
            cid = p.get("call_id")
            if cid in open_exec:
                open_exec.remove(cid)
            items = [n for n in nested.pop(cid, []) if CX._nested_call(n, "") is not None]
            for j, n in enumerate(items):
                if n.get("type") == "CommandExecution":
                    rc, o = n.get("exit_code"), n.get("aggregated_output")
                    out[f"{cid}.{j}"] = (int(rc) if isinstance(rc, int) else None,
                                         o if isinstance(o, str) else None)
    return out


def _plain_shell(fn: dict) -> str | None:
    """A plain shell function call's command line, or None."""
    key = SHELL_FUNCS.get(fn.get("name"))
    if key is None:
        return None
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return None
    cmd = args.get(key) if isinstance(args, dict) else None
    if isinstance(cmd, list):
        cmd = cmd[2] if len(cmd) >= 3 and cmd[1] in ("-c", "-lc") else " ".join(map(str, cmd))
    return cmd if isinstance(cmd, str) else None


def score_messages(lines: list[dict]) -> tuple[list[dict], dict]:
    record = [x for x in lines if x.get("type") != "compacted"]
    if not record:
        return [], {"shell_calls": 0, "shell_rc_unknown": 0, "compactions_skipped": 0,
                    "open_execs_closed": 0}
    record, closed = _close_open_execs(record)
    msgs = CX.history_at(record, len(record) - 1, system=False).messages
    own = _own_results(record)
    shell: dict[str, str] = {}  # tool call id -> "nested" | "plain"
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") == "bash":
                shell[tc.get("id")] = "nested"
            elif (cmd := _plain_shell(fn)) is not None:
                fn["name"] = "bash"
                fn["arguments"] = json.dumps({"command": cmd})
                shell[tc.get("id")] = "plain"
    unknown = 0
    for m in msgs:
        cid = m.get("tool_call_id")
        if m.get("role") != "tool" or cid not in shell:
            continue
        if shell[cid] == "nested":
            rc, text = own.get(cid, (None, None))
            if rc is None or text is None:  # unattributable: unknown, never "no rejection"
                m["content"] = ""
                unknown += 1
                continue
        else:
            text = m.get("content") if isinstance(m.get("content"), str) else ""
            hit = PLAIN_RC.search(text)
            rc = int(hit.group(1)) if hit else None
            if rc is None:
                unknown += 1
                continue
        m["content"] = json.dumps({"returncode": rc, "output": text}, ensure_ascii=False)
    info = {"shell_calls": len(shell), "shell_rc_unknown": unknown,
            "compactions_skipped": len(lines) - len([x for x in lines
                                                     if x.get("type") != "compacted"]),
            "open_execs_closed": closed}
    return msgs, info


def _token_counts(lines: list[dict]) -> list[dict]:
    return [x["payload"] for x in lines if x.get("type") == "event_msg"
            and (x.get("payload") or {}).get("type") == "token_count"]


def _used_percent(tc: dict) -> float | None:
    v = ((tc.get("rate_limits") or {}).get("primary") or {}).get("used_percent")
    return float(v) if isinstance(v, (int, float)) else None


def task_usage(lines: list[dict], lo: int = 0, hi: int | None = None) -> dict:
    """Calls, tokens, context windows seen and the plan quota for rollout lines [lo, hi)."""
    hi = len(lines) if hi is None else hi
    rng = lines[lo:hi]
    calls = [tc for tc in _token_counts(rng)
             if ((tc.get("info") or {}).get("last_token_usage") or {}).get("input_tokens")]
    u = [(tc.get("info") or {}).get("last_token_usage") or {} for tc in calls]
    windows = sorted({(tc.get("info") or {}).get("model_context_window") for tc in calls}
                     - {None})
    before = [p for tc in _token_counts(lines[:lo]) if (p := _used_percent(tc)) is not None]
    inside = [p for tc in _token_counts(rng) if (p := _used_percent(tc)) is not None]
    return {"n_calls": len(calls), "prompt_tokens": sum(x.get("input_tokens") or 0 for x in u),
            "completion_tokens": sum(x.get("output_tokens") or 0 for x in u),
            "cached_tokens": sum(x.get("cached_input_tokens") or 0 for x in u),
            "reasoning_tokens": sum(x.get("reasoning_output_tokens") or 0 for x in u),
            "max_call_input_tokens": max((x.get("input_tokens") or 0 for x in u), default=0),
            "context_windows": windows,
            "quota_used_percent": {"before": before[-1] if before else None,
                                   "first": inside[0] if inside else None,
                                   "last": inside[-1] if inside else None}}


def turn_models(lines: list[dict], lo: int = 0, hi: int | None = None) -> list[str]:
    """The model of every `turn_context` line in [lo, hi), in order (the resume pin check)."""
    hi = len(lines) if hi is None else hi
    return [str((x.get("payload") or {}).get("model")) for x in lines[lo:hi]
            if x.get("type") == "turn_context"]


def base_instructions_sha(lines: list[dict]) -> dict | None:
    """sha256, length and head of the session's base instructions (Codex's system prompt)."""
    import hashlib
    meta = next((x for x in lines if x.get("type") == "session_meta"), None)
    text = (((meta or {}).get("payload") or {}).get("base_instructions") or {}).get("text")
    if not isinstance(text, str):
        return None
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text),
            "head": text[:200]}
