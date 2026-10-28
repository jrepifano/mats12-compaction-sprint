"""An agy (Antigravity 1.4.7) transcript in the shape experiment 1's scorers read (week-2
harness replication; plan v0.3.1, "Secondaries": tested adapters, explicit task-2 cutoffs).

The steps are agy's `transcript_full.jsonl` (`compaction.agy` docstring; the pilot's
`native.agy.steps_to_messages` pairs each GENERIC tool output with the oldest pending call).

- `score_messages(steps)`: the pilot's converter, then every `run_command` call renamed `bash`
  with arguments `{"command": CommandLine}` (`score.bash_steps` reads only that), and each of
  its results wrapped as mini-swe-agent's observation `{"returncode", "output"}` (`score._rc`,
  `score._body`). agy's run_command result, as seen in the S2 pilot's 4,478 calls (24-25 Oct):
  the Created/Completed stamps, then "Command exited with code N." and either "Command
  output:\\n<text>" or "No output from the command."; a long output is "The tool's output was
  truncated ..." with the same lines inside a middle-truncated preview (kept, rc read from
  it). A command still running when agy's WaitMsBeforeAsync ran out ("Tool is running as a
  background task ...") has no code, and neither has a step-execution error: both stay
  unwrapped, so the scorer reads them as unknown, never as success.
- `task2_index(msgs, prompt)`: `transcript.task2_index` (the first user message that starts
  with the task-2 prompt; agy's <USER_REQUEST> wrapper is already stripped).
- `merge_steps(a_steps, b_steps)`: the history across containers. Whether a resumed
  conversation's log in B's container holds A's steps again, or only the new ones, isn't
  known before the smoke; either way the merged list has each step_index once.
- `compactions(steps, start)`: every CHECKPOINT at or after step `start`, with its position
  (the model call it followed) and whether our `/compact` input caused it ("manual") or agy
  compacted on its own ("auto": its in-task compaction can't be turned off; plan v0.3.1).
"""

from __future__ import annotations

import json
import re

from compaction.s2.native import agy as A
from compaction.s2.native import transcript as TR

SHELL_TOOLS = {"run_command": "bash"}
# agy's own ways to ask (the ask_user MCP tool, renamed by the pilot's converter, and the
# native tools in agy's tool list; one ask_question call in the pilot)
ASK_TOOLS = frozenset({"ask_user", "ask_question", "ask_permission", "ask_custom_permission"})
SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_STAMPS = re.compile(r"\ACreated At: [^\n]*\n(?:Completed At: [^\n]*\n)?")
_EXIT = re.compile(r"Command exited with code (-?\d+)\.(?:\n|\Z)")
TRUNCATED = "The tool's output was truncated"
PREVIEW = "Here is a preview with the middle of the output truncated:\n"
BACKGROUND = "Tool is running as a background task"
OUTPUT_HEAD = "Command output:\n"
NO_OUTPUT = "No output from the command."


def strip_stamps(text: str) -> str:
    return _STAMPS.sub("", text or "", count=1)


def parse_result(content: str) -> dict:
    """A run_command result -> {"returncode": int | None, "output": str, "kind"}; kind is
    "exited", "truncated" (rc and output from the preview), "background" or "unknown"."""
    body = strip_stamps(content)
    if body.startswith(BACKGROUND):
        return {"returncode": None, "output": body, "kind": "background"}
    text, kind = body, "exited"
    if body.startswith(TRUNCATED):
        i = body.find(PREVIEW)
        if i < 0:
            return {"returncode": None, "output": body, "kind": "unknown"}
        text, kind = body[i + len(PREVIEW):], "truncated"
    m = _EXIT.match(text)
    if not m:
        return {"returncode": None, "output": body, "kind": "unknown"}
    rest = text[m.end():].lstrip("\n")
    if rest.startswith(OUTPUT_HEAD):
        out = rest[len(OUTPUT_HEAD):]
    elif rest.startswith(NO_OUTPUT):
        out = ""
    else:
        out = rest
    return {"returncode": int(m.group(1)), "output": out, "kind": kind}


def _pairs(steps: list[dict]):
    """(call name, call args, result step or None) for every tool call, in order, paired as
    the pilot's converter pairs them (each GENERIC answers the oldest pending call)."""
    pending: list[tuple[str, dict]] = []
    out: list[list] = []
    for st in steps:
        if st.get("type") == "PLANNER_RESPONSE":
            for c in st.get("tool_calls") or []:
                name, args = A.normalize_call(c)
                rec = [name, args, None]
                out.append(rec)
                pending.append(rec)
        elif st.get("type") == "GENERIC" and st.get("source") == "MODEL" and pending:
            pending.pop(0)[2] = st
    return [tuple(r) for r in out]


def asked(steps: list[dict]) -> bool:
    return any(name in ASK_TOOLS for name, _a, _r in _pairs(steps))


def submitted(steps: list[dict]) -> bool:
    """A run_command whose output's first line is the submission token (experiment 1's
    completion: the echoed token, as in the Claude Code driver)."""
    for name, _args, res in _pairs(steps):
        if name != "run_command" or res is None:
            continue
        r = parse_result(res.get("content") or "")
        if r["returncode"] is None:
            continue
        lines = r["output"].replace("\r", "").strip().splitlines()
        if lines[:1] == [SUBMIT_MARKER]:
            return True
    return False


def score_messages(steps: list[dict]) -> tuple[list[dict], dict]:
    msgs, info = A.steps_to_messages(steps)
    shell_ids: set[str] = set()
    renamed = 0
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") not in SHELL_TOOLS:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            cmd = args.get("CommandLine") or args.get("command") or ""
            fn["name"] = SHELL_TOOLS[fn["name"]]
            fn["arguments"] = json.dumps({"command": cmd}, ensure_ascii=False)
            shell_ids.add(tc.get("id"))
            renamed += 1
    kinds = {"exited": 0, "truncated": 0, "background": 0, "unknown": 0}
    for m in msgs:
        if m.get("role") != "tool" or m.get("tool_call_id") not in shell_ids:
            continue
        text = m.get("content") if isinstance(m.get("content"), str) else ""
        r = parse_result(text)
        kinds[r["kind"]] += 1
        if r["returncode"] is None:
            continue  # unknown: left as agy showed it
        m["content"] = json.dumps({"returncode": r["returncode"], "output": r["output"]},
                                  ensure_ascii=False)
    info |= {"shell_calls": renamed,
             "shell_rc_unknown": kinds["background"] + kinds["unknown"],
             "shell_background": kinds["background"], "shell_truncated": kinds["truncated"]}
    return msgs, info


def task2_index(msgs: list[dict], prompt: str) -> int | None:
    return TR.task2_index(msgs, prompt)


def _idx(st: dict) -> int | None:
    i = st.get("step_index")
    return i if isinstance(i, int) and not isinstance(i, bool) else None


def merge_steps(a_steps: list[dict], b_steps: list[dict]) -> tuple[list[dict], dict]:
    """A's exported steps and B's log as one history, ordered by step_index, each index once
    (B's copy wins). info: how B's log related to A's ("empty", "repeats_a": it starts at 0,
    "continues": it starts after A, "gap": it starts later than A's end + 1) and any
    index that disagrees in type between the two copies."""
    a_idx = [i for s in a_steps if (i := _idx(s)) is not None]
    b_idx = [i for s in b_steps if (i := _idx(s)) is not None]
    if not b_idx:
        return list(a_steps), {"b_log": "empty", "conflicts": []}
    a_end = max(a_idx, default=-1)
    lo = min(b_idx)
    kind = ("repeats_a" if lo == 0 else "continues" if lo == a_end + 1 else
            "gap" if lo > a_end + 1 else "overlaps")
    by: dict[int, dict] = {i: s for s in a_steps if (i := _idx(s)) is not None}
    a_type = {i: s.get("type") for i, s in by.items()}
    conflicts = []
    for s in b_steps:
        i = _idx(s)
        if i is None:
            continue
        if i in a_type and a_type[i] != s.get("type"):
            conflicts.append(i)
        by[i] = s
    return [by[i] for i in sorted(by)], {"b_log": kind, "b_first": lo, "a_last": a_end,
                                         "conflicts": conflicts[:20]}


def calls(steps: list[dict], start: int = 0) -> list[dict]:
    """The model calls (PLANNER_RESPONSE steps) at or after step `start`."""
    return [s for s in steps if s.get("type") == "PLANNER_RESPONSE"
            and (_idx(s) if _idx(s) is not None else -1) >= start]


def ctx_tokens(st: dict) -> int | None:
    """A model call's request size: input + cache-read tokens (the pilot hook's measure)."""
    if st.get("input_tokens") is None:
        return None
    return int(st.get("input_tokens") or 0) + int(st.get("cache_read_tokens") or 0)


def usage(steps: list[dict], start: int = 0) -> dict:
    cs = calls(steps, start)
    return {"n_calls": len(cs),
            "prompt_tokens": sum(ctx_tokens(s) or 0 for s in cs),
            "completion_tokens": sum(int(s.get("output_tokens") or 0)
                                     + int(s.get("thinking_tokens") or 0) for s in cs),
            "cached_tokens": sum(int(s.get("cache_read_tokens") or 0) for s in cs)}


def compactions(steps: list[dict], start: int = 0) -> list[dict]:
    """Every CHECKPOINT at or after step `start`: {"step_index", "model_call" (the number of
    this task's model calls before it), "trigger" ("manual": our /compact input came since
    the previous CHECKPOINT; else "auto"), "pre_ctx_tokens" (the last call's request size
    before it, a lower bound on the context compacted)}."""
    out = []
    since_cp: list[dict] = []
    n_calls, last_ctx = 0, None
    for st in steps:
        i = _idx(st)
        mine = i is not None and i >= start
        if st.get("type") == "CHECKPOINT":
            if mine:
                manual = any(A._is_compact_input(x) for x in since_cp)
                out.append({"step_index": i, "model_call": n_calls,
                            "trigger": "manual" if manual else "auto",
                            "pre_ctx_tokens": last_ctx})
            since_cp = []
            continue
        since_cp.append(st)
        if st.get("type") == "PLANNER_RESPONSE" and mine:
            n_calls += 1
            last_ctx = ctx_tokens(st)
    return out
