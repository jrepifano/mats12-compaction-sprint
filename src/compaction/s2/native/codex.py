"""The Codex arm of the native-harness experiment (brief logs/s2/briefs/native-codex.md): SWE-
Interact sessions in the real Codex CLI (0.162.1, gpt-6-luna on Jacob's ChatGPT plan), with every
compaction's summary recovered as text so the loss pipeline (`lossaudit`) can grade it.

The summary problem (logs/s2/codex_compaction_search.md): with ChatGPT auth Codex compacts
server-side ("remote" compaction). The rollout's `compacted` line keeps the user messages verbatim
and one `{"type": "compaction", "encrypted_content": <Fernet token>}` item; `message` is empty.
Two routes to text:

  (b) recitation (primary): copy the rollout up to and including the `compacted` line under a
      new thread id, resume the copy (`codex exec resume`) and ask the model to reproduce the
      handoff summary it was given, verbatim (`recite`). This is the model's report of the
      server-side summary, not the summary itself; `validate_recitation` measures it on a toy
      session with planted facts (recall, invention, agreement across repeats) against the
      brief's fixed rule (`DECISION_RULE`).
  (a) local compaction (fallback): a custom provider with `remote_compaction = "unsupported"`
      makes Codex run its open-source compactor (`prompts/templates/compact/prompt.md`) and store
      the summary in plaintext in `compacted.message` (`local_provider_config`). A different
      compactor from the one ChatGPT users get.

Rollout to messages (`history_at`): the model-visible history at any rollout line, in the pi
arm's OpenAI format (common.NativeBoundary). Codex 0.162 runs tools in "code mode": one
`custom_tool_call` named `exec` whose JavaScript calls `tools.exec_command(...)` and
`tools.mcp__user_server__ask_user(...)`; the nested calls appear as `event_msg item_completed`
(CommandExecution, McpToolCall) between the call and its `custom_tool_call_output`. Each nested
call becomes its own tool call: `bash` {"command"}, `ask_user` {"question"}, or `apply_patch`
{"input": Codex patch text naming every path} for a FileChange. Tool results hold
only what the model read, the exec's JS output (`visible_outputs`): an ask_user reply is the
plain reply text only when the JS output shows it (so `interact_run.reply_index` matches it).
An exec with no nested call stays an `exec` tool call {"code"} with its JS output. Plain `function_call`s map one to one. Reasoning items are
encrypted and left out.
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CLI_VERSION = "0.162.1"
MODEL = "gpt-6-luna"
POLICY_REMOTE = "codex-native-remote"  # server-side compaction, summary recovered by recitation
POLICY_LOCAL = "codex-native-local"  # open-source local compactor (route a)
SUMMARY_LEAD_REMOTE = "[Codex remote compaction summary, recovered by recitation]\n"
EXEC_HEADER = re.compile(r"^Script completed\nWall time [^\n]*\nOutput:\n", re.DOTALL)
LOCAL_PROVIDER_ID = "oai-local"


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------
# Rollouts
# ------------------------------------------------------------------------------------------


def read_rollout(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def write_rollout(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))


def thread_id(lines: list[dict]) -> str:
    meta = next(x for x in lines if x.get("type") == "session_meta")
    return meta["payload"]["id"]


def find_rollout(codex_home: Path, tid: str) -> Path:
    hits = sorted(Path(codex_home, "sessions").rglob(f"rollout-*{tid}.jsonl"))
    if len(hits) != 1:
        raise FileNotFoundError(f"{len(hits)} rollouts for thread {tid} under {codex_home}")
    return hits[0]


def compaction_lines(lines: list[dict]) -> list[int]:
    """Indices of the `compacted` lines, in order (boundary k = the k-th, 1-based)."""
    return [i for i, x in enumerate(lines) if x.get("type") == "compacted"]


def encrypted_items(compacted: dict) -> list[dict]:
    return [h for h in compacted["payload"].get("replacement_history") or []
            if h.get("type") == "compaction"]


def fernet_plaintext_bound(token: str) -> int:
    """Upper bound on the plaintext bytes in a Fernet token (version 1 + time 8 + IV 16 +
    AES-CBC body + HMAC 32, base64url): the body is the plaintext padded to 16. A recitation far
    longer than this cannot be a faithful copy (if the plaintext is not compressed)."""
    raw = (len(token.rstrip("=")) * 3) // 4
    return max(0, raw - 57)


def fork_rollout(src_lines: list[dict], upto: int, codex_home: Path,
                 new_id: str | None = None) -> tuple[str, Path]:
    """Copy lines[0..upto] under a new thread id (every occurrence of the old id replaced) into
    `codex_home`'s sessions dir, where `codex exec resume <id>` finds it. Returns (id, path)."""
    old = thread_id(src_lines)
    new_id = new_id or str(uuid.uuid4())
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in src_lines[:upto + 1])
    text = text.replace(old, new_id)
    now = datetime.now(UTC)
    p = (Path(codex_home) / "sessions" / now.strftime("%Y/%m/%d")
         / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_id}.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return new_id, p


def replace_compaction(lines: list[dict], k: int, text: str) -> list[dict]:
    """Q2 (forks): a copy of `lines` whose k-th `compacted` line has its encrypted item(s)
    replaced by one readable user message holding `text` (feasibility §Codex 4: honoured)."""
    out = copy.deepcopy(lines)
    i = compaction_lines(out)[k - 1]
    rh = out[i]["payload"]["replacement_history"]
    msg = {"type": "message", "role": "user",
           "content": [{"type": "input_text", "text": text}]}
    new, done = [], False
    for h in rh:
        if h.get("type") == "compaction":
            if not done:
                new.append(msg)
                done = True
            continue
        new.append(h)
    if not done:
        new.append(msg)
    out[i]["payload"]["replacement_history"] = new
    out[i]["payload"]["message"] = text
    return out


# ------------------------------------------------------------------------------------------
# Rollout -> OpenAI-format messages
# ------------------------------------------------------------------------------------------


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "".join(parts)


def exec_output_text(output: Any) -> str:
    """The JS output the model read, without code mode's 'Script completed' header."""
    return EXEC_HEADER.sub("", _content_text(output) if not isinstance(output, str) else output,
                           count=1)


def _mcp_reply(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("result"), str):
        return sc["result"]
    return _content_text(result.get("content"))


_PATCH_VERB = {"add": "Add", "delete": "Delete", "update": "Update"}


def file_change_patch(item: dict) -> str:
    """A FileChange item (`changes`: {path: {type, unified_diff}}, or a list of such maps) as
    Codex's own patch text, so `lossaudit.tool_writes` sees which files the call wrote."""
    ch = item.get("changes") or {}
    maps = ch if isinstance(ch, list) else [ch]
    parts = ["*** Begin Patch"]
    for m in maps:
        for path, c in (m.items() if isinstance(m, dict) else []):
            c = c if isinstance(c, dict) else {}
            verb = _PATCH_VERB.get(str(c.get("type") or "update").lower(), "Update")
            parts.append(f"*** {verb} File: {path}")
            if c.get("move_path"):
                parts.append(f"*** Move to: {c['move_path']}")
            diff = c.get("unified_diff") or c.get("content") or ""
            if diff:
                parts.append(diff.rstrip("\n"))
    parts.append("*** End Patch")
    return "\n".join(parts)


def _nested_call(item: dict, cid: str) -> tuple[dict, str] | None:
    t = item.get("type")
    if t == "CommandExecution":
        cmd = item.get("command")
        if isinstance(cmd, list):
            cmd = cmd[2] if len(cmd) >= 3 and cmd[1] in ("-c", "-lc") else " ".join(cmd)
        call = {"id": cid, "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}
        return call, str(item.get("aggregated_output") or "")
    if t == "FileChange":
        call = {"id": cid, "type": "function",
                "function": {"name": "apply_patch",
                             "arguments": json.dumps({"input": file_change_patch(item)})}}
        return call, ""
    if t == "McpToolCall":
        name = item.get("tool") or "mcp"
        args = item.get("arguments") or {}
        call = {"id": cid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        return call, _mcp_reply(item.get("result"))
    return None


def _shown(text: str, js_out: str) -> bool:
    """Is `text` in the JS output the model read (raw, or JSON-escaped as in a printed MCP
    result)?"""
    t = (text or "").strip()
    if not t:
        return True
    return t in js_out or json.dumps(t, ensure_ascii=False)[1:-1] in js_out \
        or json.dumps(t)[1:-1] in js_out


def _only_reply(js_out: str, reply: str) -> bool:
    """The JS output is exactly the reply, or exactly an MCP result wrapping it (no other
    text), so replacing it by the plain reply drops nothing the model saw."""
    t, want = js_out.strip(), reply.strip()
    if t == want:
        return True
    try:
        o = json.loads(t)
    except ValueError:
        return False
    if isinstance(o, str):
        return o.strip() == want
    if not isinstance(o, dict) or not set(o) <= {"content", "structuredContent", "isError",
                                                  "_meta"}:
        return False
    content = o.get("content", [])
    if not isinstance(content, list):
        return False
    texts = []
    for c in content:  # strictly {"type": "text", "text": <str>}: anything else is evidence
        if not (isinstance(c, dict) and set(c) == {"type", "text"} and c["type"] == "text"
                and isinstance(c["text"], str)):
            return False
        texts.append(c["text"])
    sc = o.get("structuredContent")
    if sc is not None and (not isinstance(sc, dict) or set(sc) != {"result"}
                           or str(sc["result"]).strip() != want):
        return False
    return all(x.strip() == want for x in texts) and (bool(texts) or sc is not None)


def visible_outputs(nested: list[dict], cid: str, js_out: str) -> list[tuple[dict, str]]:
    """(tool call, tool result) per nested call of one exec, keeping only what the model saw:
    the JS output. One nested call: its result is the JS output, except an ask_user reply whose
    JS output is only that reply or its bare MCP wrapper (`_only_reply`; then the plain reply,
    which `interact_run.reply_index` matches).
    Several: each call's own output when the JS output shows every one of them and nothing
    else; otherwise the earlier calls get "" and the last the whole JS output."""
    pairs = [_nested_call(n, f"{cid}.{j}") for j, n in enumerate(nested)]
    pairs = [x for x in pairs if x is not None]
    if len(pairs) == 1:
        call, out = pairs[0]
        if call["function"]["name"] == "ask_user" and out.strip() and _only_reply(js_out, out):
            return [(call, out)]
        return [(call, js_out)]
    rest = js_out
    for _, out in pairs:
        if not _shown(out, js_out):
            break
        rest = rest.replace(out.strip(), "", 1)
    else:
        if not rest.strip():
            return pairs
    return [(c, "") for c, _ in pairs[:-1]] + [(pairs[-1][0], js_out)]


@dataclass
class History:
    """The model-visible context at a rollout line, in OpenAI format."""
    messages: list[dict]
    summary_indices: list[int] = field(default_factory=list)  # compaction placeholders
    n_compactions: int = 0


def _summary_text(k: int, summaries: dict[int, str] | None) -> str:
    if summaries and k in summaries:
        return summaries[k]
    return f"[encrypted compaction summary {k}]"


def history_at(lines: list[dict], upto: int, summaries: dict[int, str] | None = None,
               system: bool = True) -> History:
    """The history the model had after line `upto` (inclusive). A `compacted` line replaces the
    history with its replacement_history; its compaction item becomes a user message holding
    `summaries[k]` (or a placeholder). `system`: the base instructions as a leading system
    message (the session_meta's base_instructions), developer messages as further system
    messages in place."""
    msgs: list[dict] = []
    summ_idx: list[int] = []
    pending: dict[str, dict] = {}  # exec call_id -> {"input", "nested": [...]}
    open_exec: list[str] = []
    k = 0
    base = None

    def add_item(p: dict) -> None:
        t = p.get("type")
        if t == "message":
            role = p.get("role")
            text = _content_text(p.get("content"))
            if role == "developer":
                msgs.append({"role": "system", "content": text})
            elif role in ("user", "assistant"):
                msgs.append({"role": role, "content": text})
        elif t == "compaction":
            summ_idx.append(len(msgs))
            msgs.append({"role": "user", "content": _summary_text(k, summaries)})
        elif t == "function_call":
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": p.get("call_id"), "type": "function",
                 "function": {"name": p.get("name"), "arguments": p.get("arguments") or "{}"}}]})
        elif t == "function_call_output":
            out = p.get("output")
            if isinstance(out, dict):
                out = out.get("content")
            msgs.append({"role": "tool", "tool_call_id": p.get("call_id"),
                         "content": out if isinstance(out, str) else _content_text(out)})

    for i, x in enumerate(lines[:upto + 1]):
        t, p = x.get("type"), x.get("payload") or {}
        if t == "session_meta":
            base = (p.get("base_instructions") or {}).get("text")
            continue
        if t == "compacted":
            k += 1
            msgs, summ_idx, pending, open_exec = [], [], {}, []
            for h in p.get("replacement_history") or []:
                add_item(h)
            continue
        if t == "event_msg" and p.get("type") == "item_completed" and open_exec:
            pending[open_exec[-1]]["nested"].append(p.get("item") or {})
            continue
        if t != "response_item":
            continue
        pt = p.get("type")
        if pt == "custom_tool_call":
            pending[p.get("call_id")] = {"input": p.get("input"), "name": p.get("name"),
                                         "nested": []}
            open_exec.append(p.get("call_id"))
            continue
        if pt == "custom_tool_call_output":
            cid = p.get("call_id")
            ex = pending.pop(cid, None) or {"input": None, "name": "exec", "nested": []}
            with contextlib.suppress(ValueError):
                open_exec.remove(cid)
            js_out = exec_output_text(p.get("output"))
            nested = [n for n in ex["nested"] if _nested_call(n, "") is not None]
            if not nested:
                call = {"id": cid, "type": "function", "function": {
                    "name": ex["name"] or "exec",
                    "arguments": json.dumps({"code": ex["input"]})}}
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": js_out})
                continue
            for j, (call, out) in enumerate(visible_outputs(nested, cid, js_out)):
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
                msgs.append({"role": "tool", "tool_call_id": call["id"], "content": out})
            continue
        add_item(p)
    # an exec still running at `upto`: its call without an output
    for cid in open_exec:
        ex = pending[cid]
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {
                "name": ex["name"] or "exec", "arguments": json.dumps({"code": ex["input"]})}}]})
    if system and base:
        msgs.insert(0, {"role": "system", "content": base})
        summ_idx = [s + 1 for s in summ_idx]
    return History(msgs, summ_idx, k)


def last_input_tokens(lines: list[dict], upto: int) -> int | None:
    """The last call's input tokens before line `upto` (event_msg token_count)."""
    for x in reversed(lines[:upto + 1]):
        p = x.get("payload") or {}
        if x.get("type") == "event_msg" and p.get("type") == "token_count":
            lt = ((p.get("info") or {}).get("last_token_usage") or {})
            if lt.get("input_tokens"):
                return int(lt["input_tokens"]) + int(lt.get("output_tokens") or 0)
    return None


def history_text(lines: list[dict], upto: int) -> str:
    """Every string the model could have seen up to `upto` (for the invention check)."""
    return json.dumps(lines[:upto + 1], ensure_ascii=False)


# ------------------------------------------------------------------------------------------
# Running the CLI (laptop: Codex's own sandbox; container: the runner passes its own argv)
# ------------------------------------------------------------------------------------------


@dataclass
class CodexHome:
    """A clean CODEX_HOME: a copy of the ChatGPT auth.json and a minimal config.toml (none of
    Jacob's skills, plugins or AGENTS.md)."""
    path: Path
    config: dict = field(default_factory=dict)

    def setup(self, auth_src: Path | None = None) -> CodexHome:
        self.path.mkdir(parents=True, exist_ok=True)
        auth = Path(auth_src or Path.home() / ".codex" / "auth.json")
        dst = self.path / "auth.json"
        if not dst.exists():
            shutil.copy2(auth, dst)
            os.chmod(dst, 0o600)
        (self.path / "config.toml").write_text(toml_dumps(self.config))
        return self

    def env(self) -> dict[str, str]:
        e = {k: v for k, v in os.environ.items()
             if k not in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")}
        e["CODEX_HOME"] = str(self.path)
        return e


def toml_dumps(cfg: dict, prefix: str = "") -> str:
    """Enough TOML for Codex's config (scalars, nested tables)."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(str(v))
    out, tables = [], []
    for k, v in cfg.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            out.append(f"{_tkey(k)} = {val(v)}")
    text = "\n".join(out) + ("\n" if out else "")
    for k, v in tables:
        name = f"{prefix}.{_tkey(k)}" if prefix else _tkey(k)
        scal = {a: b for a, b in v.items() if not isinstance(b, dict)}
        sub = {a: b for a, b in v.items() if isinstance(b, dict)}
        text += f"\n[{name}]\n" + toml_dumps(scal)
        if sub:
            text += toml_dumps(sub, name)
    return text


def _tkey(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def local_provider_config(base: dict | None = None) -> dict:
    """Route (a): a provider that is not "OpenAI" by name, on the ChatGPT backend with ChatGPT
    login, whose remote compaction is switched off, so Codex compacts locally and stores the
    summary as plaintext in `compacted.message` (codex_compaction_search.md (a))."""
    cfg = dict(base or {})
    cfg["model_provider"] = LOCAL_PROVIDER_ID
    cfg.setdefault("model_providers", {})[LOCAL_PROVIDER_ID] = {
        "name": "oai-local", "base_url": "https://chatgpt.com/backend-api/codex",
        "wire_api": "responses", "requires_openai_auth": True,
        "capabilities": {"remote_compaction": "unsupported"}}
    return cfg


@dataclass
class ExecResult:
    rc: int
    seconds: float
    events: list[dict]
    stderr: str
    thread_id: str | None

    @property
    def final_text(self) -> str:
        texts = [e["item"].get("text", "") for e in self.events
                 if e.get("type") == "item.completed"
                 and (e.get("item") or {}).get("type") == "agent_message"]
        return texts[-1] if texts else ""

    def tool_items(self) -> list[dict]:
        return [e["item"] for e in self.events if e.get("type") == "item.completed"
                and (e.get("item") or {}).get("type") not in ("agent_message", "reasoning")]


def codex_exec(home: CodexHome, prompt: str, *, cwd: Path, resume: str | None = None,
               config: dict[str, Any] | None = None, sandbox: str = "workspace-write",
               timeout_s: int = 900, log_path: Path | None = None) -> ExecResult:
    """One laptop-side `codex exec [resume <id>]` turn under Codex's own sandbox."""
    argv = ["codex", "exec"]
    if resume:
        argv += ["resume", resume]
    argv += ["--skip-git-repo-check", "--json"]
    if not resume:
        argv += ["--sandbox", sandbox, "-C", str(cwd)]
    else:
        argv += ["-c", f'sandbox_mode="{sandbox}"']
    for k, v in (config or {}).items():
        argv += ["-c", f"{k}={json.dumps(v) if isinstance(v, str) else v}"]
    argv += ["--", prompt]
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=home.env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        if log_path is not None:  # keep what the CLI wrote before the timeout
            log_path.parent.mkdir(parents=True, exist_ok=True)
            for path, data in ((log_path, e.stdout), (Path(str(log_path) + ".err"), e.stderr)):
                path.write_text(data.decode(errors="replace") if isinstance(data, bytes)
                                else (data or "") + "\n[timed out]\n")
        raise
    events = []
    for line in p.stdout.splitlines():
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(p.stdout)
        Path(str(log_path) + ".err").write_text(p.stderr)
    tid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    return ExecResult(p.returncode, round(time.monotonic() - t0, 1), events, p.stderr, tid)


# ------------------------------------------------------------------------------------------
# Route (b): recitation
# ------------------------------------------------------------------------------------------

RECITE_PROMPT = (
    "Stop the task for a moment. Earlier in this session your context was compacted: the older "
    "conversation was replaced by a handoff summary that you were given in its place. Reproduce "
    "that handoff summary exactly as you received it, verbatim, word for word, from its first "
    "line to its last, inside one ```text code block. Do not paraphrase, shorten, reorder, "
    "correct or add anything, and do not include the user messages that were kept verbatim "
    "next to it. Do not run any tools. If there is no such summary, reply NO SUMMARY.")
RECITE_OVERRIDES = {"model_auto_compact_token_limit": 10_000_000}
_FENCE = re.compile(r"```(?:text|markdown|md)?\n(.*?)\n?```", re.DOTALL)


def extract_recitation(text: str) -> str | None:
    """The fenced recitation (the longest fence), else the whole reply; None for an empty
    reply or the NO SUMMARY sentinel, fenced or not."""
    m = _FENCE.findall(text or "")
    t = (max(m, key=len) if m else (text or "")).strip()
    if not t or t.upper().startswith("NO SUMMARY"):
        return None
    return t


@dataclass
class Recitation:
    k: int
    thread_id: str
    text: str | None
    raw: str
    tool_calls: int
    seconds: float
    rc: int
    plaintext_bound: int | None

    @property
    def valid(self) -> bool:
        """Usable as a summary: the CLI succeeded, ran no tools, and returned text."""
        return self.rc == 0 and self.tool_calls == 0 and bool(self.text)

    def to_dict(self) -> dict:
        return {**self.__dict__, "valid": self.valid}


def recite(home: CodexHome, lines: list[dict], k: int, *, cwd: Path,
           prompt: str = RECITE_PROMPT, log_dir: Path | None = None,
           tag: str = "", timeout_s: int = 900) -> Recitation:
    """Route (b) for boundary k: fork the rollout through its k-th `compacted` line, resume the
    fork with `prompt` in a read-only sandbox, keep the fenced reply. A CLI timeout comes back
    as an invalid recitation (rc -1), not an exception."""
    at = compaction_lines(lines)[k - 1]
    tid, _ = fork_rollout(lines, at, home.path)
    enc = encrypted_items(lines[at])
    bound = fernet_plaintext_bound(enc[0]["encrypted_content"]) if enc else None
    t0 = time.monotonic()
    try:
        r = codex_exec(home, prompt, cwd=cwd, resume=tid, config=RECITE_OVERRIDES,
                       sandbox="read-only", timeout_s=max(1, int(timeout_s)),
                       log_path=(log_dir / f"recite_b{k}{tag}.jsonl") if log_dir else None)
    except subprocess.TimeoutExpired:
        return Recitation(k, tid, None, "", 0, round(time.monotonic() - t0, 1), -1, bound)
    return Recitation(k, tid, extract_recitation(r.final_text), r.final_text,
                      len(r.tool_items()), r.seconds, r.rc, bound)


# ------------------------------------------------------------------------------------------
# Validation of route (b): planted facts (brief "Validate it before trusting it")
# ------------------------------------------------------------------------------------------

# Planted in tool outputs only (files the agent reads, a script's error): only the compaction
# summary can carry them past the boundary. FACT_USER is in a user message, which Codex keeps
# verbatim next to the summary: reported separately, not counted in recall.
FACTS = {
    "F1": {"where": "config/deploy.ini", "key": "OKAPI-7731",
           "probe": "What release channel is configured in config/deploy.ini?"},
    "F2": {"where": "NOTES.md", "key": "test_quorum_drift",
           "probe": "Which test is flaky (its exact name)?"},
    "F3": {"where": "tools/check.py output", "key": "0x5F3A",
           "probe": "At what offset did tools/check.py report the ledger mismatch?"},
    "F4": {"where": "src/ledger.py", "key": "vermilion-tapir-42",
           "probe": "What is the exact value of MAGIC_SALT in src/ledger.py?"},
    "F5": {"where": "tools/check.py output", "key": "heron-12",
           "probe": "Which batch did tools/check.py name in the mismatch error?"},
    "F6": {"where": "NOTES.md", "key": "ledger_v3_frozen",
           "probe": "Which database table must never be written to, according to NOTES.md?"},
}
FACT_USER = {"key": "CR-PANGOLIN-58",
             "probe": "What customer reference must go in every commit message?"}
# Same shapes, never planted anywhere: their appearance would be pure invention.
DECOYS = ("NARWHAL-1189", "test_lattice_skew", "0x7C1E", "azure-wombat-19", "heron-17",
          "ledger_v2_frozen", "CR-MARMOT-33")
DECISION_RULE = {"min_recall": 0.8, "min_agreement": 0.8, "max_invented_planted": 0}

TOY_FILES = {
    "README.md": "# ledgerkit\n\nReconciles daily ledger batches against the bank export.\n"
                 "Run `python3 tools/check.py` to verify the current batch.\n",
    "NOTES.md": "# Maintainer notes\n\n"
                "- The test `test_quorum_drift` is flaky on CI: always run pytest with "
                "`-k 'not quorum_drift'`.\n"
                "- Never write to the database table `ledger_v3_frozen`; it is the audited "
                "copy and any write voids the audit.\n"
                "- Batches are named after birds.\n",
    "config/deploy.ini": "[deploy]\nregion = eu-west\nrelease_channel = OKAPI-7731\n"
                         "replicas = 3\n",
    "src/ledger.py": "\"\"\"Ledger reconciliation.\"\"\"\n\n"
                     "# Must never change: downstream checksums depend on it.\n"
                     "MAGIC_SALT = \"vermilion-tapir-42\"\n\n\n"
                     "def checksum(rows):\n"
                     "    return hash((MAGIC_SALT, tuple(rows))) & 0xFFFFFFFF\n",
    "tools/check.py": "import sys\n\n"
                      "print(\"checking batch heron-12 ...\")\n"
                      "print(\"ERR_LEDGER_MISMATCH at offset 0x5F3A (batch 'heron-12'): "
                      "expected 1041 rows, found 1040\", file=sys.stderr)\n"
                      "sys.exit(3)\n",
    "tools/dump.py": "import random\n\nrandom.seed(7)\nLEVELS = ['INFO'] * 8 + ['WARN', 'DEBUG']\n"
                     "WORDS = 'sync row merge queue flush retry commit page cache scan'.split()\n"
                     "for i in range(int(__import__('sys').argv[1]) if len(__import__('sys')"
                     ".argv) > 1 else 3000):\n"
                     "    w = ' '.join(random.choice(WORDS) for _ in range(9))\n"
                     "    print(f'{i:06d} {random.choice(LEVELS)} worker {i % 7}: {w}')\n",
}
TOY_TURNS = (
    ("You are starting work on this repository. First read NOTES.md, config/deploy.ini and "
    "src/ledger.py, and run `python3 tools/check.py`. Don't change any files yet. Reply with a "
    "short status of what you found, including anything I must not break."),
    ("Note for the rest of this job: the customer reference is CR-PANGOLIN-58, and it must go in "
    "every commit message. Now run `python3 tools/dump.py {n}` and tell me how many WARN lines "
    "it printed (count them with the shell). Don't change any files."),
    ("Next step: plan the fix for the ledger mismatch in at most four bullets. Don't edit files "
    "and don't run commands."),
)


def build_toy(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    for rel, text in TOY_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True, env=env, capture_output=True)
    return root


def probe_prompt() -> str:
    qs = [f["probe"] for f in FACTS.values()] + [FACT_USER["probe"]]
    body = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(qs))
    return ("Without running any tools or reading any files, answer from what you know of this "
            "session so far. Answer each with the exact value, or 'unknown' if you don't know it. "
            "Don't guess.\n" + body)


_IDENT = re.compile(r"`([^`\n]{3,80})`|\b([A-Za-z]+[-_][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*|"
                    r"0x[0-9A-Fa-f]{2,}|[a-z]+_[a-z0-9_]+)\b")


def specific_tokens(text: str) -> set[str]:
    out = set()
    for a, b in _IDENT.findall(text or ""):
        s = (a or b).strip()
        if s:
            out.add(s)
    return out


def invented_tokens(recited: str, history: str) -> list[str]:
    """Specific tokens (identifiers with digits, snake_case, hex, backticked spans) in the
    recitation that occur nowhere in the history the model saw before the boundary, ignoring
    whitespace (a summary writes `a = b` as `a=b`)."""
    h = re.sub(r"\s+", "", history.replace("\\n", "\n").replace('\\"', '"'))
    return sorted(t for t in specific_tokens(recited) if re.sub(r"\s+", "", t) not in h)


def _items(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line).strip()
        if len(s) >= 4 and not re.fullmatch(r"#+.*|[-=*_`]+", s):
            out.append(s.lower())
    return out


def item_overlap(a: str, b: str, cutoff: float = 0.8) -> float:
    """Symmetric item overlap: the share of lines (bullets, sentences per line) of each text
    with a match (difflib ratio >= cutoff) in the other, averaged over both directions."""
    ia, ib = _items(a), _items(b)
    if not ia and not ib:
        return 1.0
    if not ia or not ib:
        return 0.0

    def share(x, y):
        hit = 0
        for s in x:
            if any(difflib.SequenceMatcher(None, s, t).ratio() >= cutoff for t in y):
                hit += 1
        return hit / len(x)

    return (share(ia, ib) + share(ib, ia)) / 2


def score_validation(recitations: list[str | None], probe_answer: str, history: str) -> dict:
    """The brief's numbers. Primary recall: the share of ALL planted tool-output facts (each
    planted as a task-relevant constraint the summary should keep) in each recitation; the
    decision rule uses it, so a summary that dropped a fact counts against route (b) and a
    failure is "unclear" (summary or recitation) rather than proof the recitation is broken.
    Secondary, conditioned recall: over the facts the summary demonstrably kept (the direct
    probe, or any recitation, recovered them). Also: the user-message fact (kept verbatim by
    Codex, not scored); listed decoys (never planted) in any recitation, reported apart from
    specific tokens absent from the history; per-fact agreement across repeats (each planted
    fact present in all or in none); pairwise item overlap. A None recitation (invalid: CLI
    error, a tool call, no text) fails the rule."""
    probe = probe_answer or ""
    rec = [r or "" for r in recitations]
    planted = sorted(FACTS)
    kept = sorted(f for f, d in FACTS.items()
                  if d["key"] in probe or any(d["key"] in r for r in rec))
    per = []
    for r in rec:
        hits = [f for f in planted if FACTS[f]["key"] in r]
        per.append({"recalled": hits, "recall": len(hits) / len(planted),
                    "recall_kept": (len([f for f in hits if f in kept]) / len(kept)
                                    if kept else None),
                    "user_fact": FACT_USER["key"] in r,
                    "decoys": [d for d in DECOYS if d in r],
                    "invented": invented_tokens(r, history), "chars": len(r)})
    pairs = []
    for i in range(len(rec)):
        for j in range(i + 1, len(rec)):
            pairs.append(round(item_overlap(rec[i], rec[j]), 3))
    fact_agree = [f for f in planted
                  if len({FACTS[f]["key"] in r for r in rec}) == 1] if rec else []
    recalls = [p["recall"] for p in per]
    kr = [p["recall_kept"] for p in per if p["recall_kept"] is not None]
    out = {
        "planted_facts": planted,
        "kept_facts": kept,
        "probe_recovered": sorted(f for f, d in FACTS.items() if d["key"] in probe),
        "probe_user_fact": FACT_USER["key"] in probe,
        "probe_decoys": [d for d in DECOYS if d in probe],
        "per_recitation": per,
        "mean_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "min_recall": min(recalls) if recalls else None,
        "mean_recall_kept": round(sum(kr) / len(kr), 3) if kr else None,
        "pairwise_overlap": pairs,
        "mean_overlap": round(sum(pairs) / len(pairs), 3) if pairs else None,
        "fact_agreement": round(len(fact_agree) / len(planted), 3) if rec else None,
        "decoys_any": sorted({d for p in per for d in p["decoys"]}),
        "invented_any": sorted({t for p in per for t in p["invented"]}),
    }
    rule = DECISION_RULE
    out["usable"] = bool(
        rec and all(r is not None for r in recitations)
        and out["mean_recall"] is not None and out["mean_recall"] >= rule["min_recall"]
        and (out["mean_overlap"] or 0) >= rule["min_agreement"]
        and len(out["decoys_any"]) <= rule["max_invented_planted"])
    return out


# ------------------------------------------------------------------------------------------
# The Codex arm in the task container (Q1 base runs)
# ------------------------------------------------------------------------------------------
#
# Like the Claude Code arm (claude_code.py), with Codex's own pieces:
# - The CLI: the static linux-musl binary from @openai/codex@CLI_VERSION's npm package, in a
#   read-only Docker volume at /opt/emx/codex (`ensure_cli_volume`), so the task image is
#   untouched and kept images don't carry it.
# - CODEX_HOME=/opt/emx/codex-home in the container's filesystem, so each kept image holds the
#   rollout as it was at that boundary: config.toml (model, reasoning effort, the compaction
#   limit, web search off, the simulator as MCP server `user-server` with its token header from
#   env) and hooks.json (PostCompact -> the boundary hook).
# - Auth: Jacob's ChatGPT auth.json (`~/.codex/auth.json`), written to a tmpfs (/run/emx-codex,
#   never part of a `docker commit`) and symlinked from CODEX_HOME. Before every kept commit the
#   container is scanned for its access and refresh tokens; the commit is refused on a hit.
#   After the run the in-container auth.json is compared with the original: a refreshed token
#   is reported (`auth_refreshed`), since a rotated refresh token may invalidate Jacob's copy.
# - Steps: 01_plan, then 02_implement as a fresh `codex exec` (the pi arm's rule), the step's
#   instruction.md on stdin, `--json` events streamed to steps/<step>/events.jsonl, under
#   `--dangerously-bypass-approvals-and-sandbox` (the container is the sandbox) and
#   `--dangerously-bypass-hook-trust` (our own hook).
# - Each compaction (remote, server-side): the PostCompact hook POSTs its payload to the
#   SimServer's /emx/boundary and blocks until `capture` returns: rollout read (waiting for the
#   new `compacted` line), process list, secret scan, fingerprint, kept commit, then route (b)
#   on the laptop (`recite`, `recite_repeats` times on separate forks of the rollout), then the
#   boundary in the pi layout (`rollout_boundary` + common.write_boundary). The summary is the
#   first recitation; the others and their agreement are kept under b<k>/native/.

ARM = "codex"
CLI_VOLUME = f"emx-native-codex-{CLI_VERSION}"
CLI_DIR = "/opt/emx/codex"
CLI = f"{CLI_DIR}/codex"
HOME_C = "/opt/emx/codex-home"
SECRETS_C = "/run/emx-codex"  # tmpfs
HOOK = "/opt/emx/compact-hook.sh"
HOOK_OUT = "/opt/emx/hook-last.json"
EFFORT = "high"  # as the Claude Code arm (the paper's runs used reasoning_effort=high)
CONTEXT_WINDOW = 272_000  # ~/.codex/models_cache.json, gpt-6-luna (effective 95%)
STEP_TIMEOUT_S = {"01_plan": 2400, "02_implement": 5400}  # task.toml [steps.agent] timeouts
HOOK_TIMEOUT_S = 1800
CONTINUE_PROMPT = "Continue."
SECRET_ENV = "EMX_CODEX_SECRET"
ROLLOUT_WAIT_S = 60
RECITE_RETRIES = 2  # extra rounds for the missing valid recitations
CAPTURE_DEADLINE_S = HOOK_TIMEOUT_S - 120  # a capture must end before the hook's curl does
SCAN_BUDGET_S = 600  # secret_scan's exec timeout
COMMIT_BUDGET_S = 900  # what a kept commit may take (sandbox commit timeout)


def root() -> Path:
    from compaction.s2 import config as C
    return C.raw_dir() / "native" / ARM


def ensure_cli_volume(host: str | None, image: str) -> str:
    """The linux-musl Codex binary (and its vendored helpers) in a Docker volume on `host`,
    installed once from npm in a throwaway container with network. Returns the volume."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2.native import common as N
    with SB.using_host(host):
        if SB._docker(["volume", "inspect", CLI_VOLUME], timeout=60,
                      check=False).returncode == 0:
            return CLI_VOLUME
        SB._docker(["volume", "create", "--label", f"{N.NATIVE_LABEL}=codex-cli", CLI_VOLUME],
                   timeout=60)
        name = f"{N.purpose(ARM)}-install-{uuid.uuid4().hex[:8]}"
        cmd = (f"npm i -g --prefix /tmp/n @openai/codex@{CLI_VERSION} >/dev/null 2>&1"
               " && b=$(find /tmp/n -path '*x86_64-unknown-linux-musl*' -name codex -type f"
               " | head -1) && test -n \"$b\" && cp -r \"$(dirname \"$b\")\"/. /out/"
               " && chmod 755 /out/codex && /out/codex --version")
        try:
            out = SB._docker(["run", "--rm", "--name", name, "--label",
                              f"{SB.LABEL_KEY}={N.purpose(ARM)}", "--network", "bridge",
                              "-v", f"{CLI_VOLUME}:/out", "--platform", "linux/amd64", image,
                              "timeout", "-k", "5", "600", "bash", "-lc", cmd], timeout=700)
        except BaseException:
            SB.remove_container(name)
            SB._docker(["volume", "rm", CLI_VOLUME], timeout=60, check=False)
            raise
        if CLI_VERSION not in out.stdout.decode():
            SB._docker(["volume", "rm", CLI_VOLUME], timeout=60, check=False)
            raise SB.SandboxError(f"installed CLI is not {CLI_VERSION}: {out.stdout[-300:]}")
    return CLI_VOLUME


@dataclass(frozen=True)
class CodexConfig:
    """The CLI settings of one run (recorded in run.json and every boundary)."""
    model: str = MODEL
    effort: str | None = EFFORT
    auto_compact_limit: int | None = None  # None: Codex's own default for the model

    def threshold(self) -> dict:
        return {"kind": "model_auto_compact_token_limit", "limit": self.auto_compact_limit,
                "context_window": CONTEXT_WINDOW,
                "note": "None = Codex's default for the model; see each boundary's pre_tokens"}

    def config(self, mcp_url: str, token_header: str) -> dict:
        cfg: dict = {"model": self.model, "web_search": "disabled"}
        if self.effort:
            cfg["model_reasoning_effort"] = self.effort
        if self.auto_compact_limit:
            cfg["model_auto_compact_token_limit"] = int(self.auto_compact_limit)
        cfg["mcp_servers"] = {"user-server": {
            "url": mcp_url, "env_http_headers": {token_header: "EMX_SIM_TOKEN"},
            "startup_timeout_sec": 60, "tool_timeout_sec": 3600}}
        return cfg


def hook_script() -> str:
    from compaction.s2.native import common as N
    hdr = f"{SECRETS_C}/hook-header"
    return ("#!/bin/sh\n"
            "# PostCompact: hand the payload to the runner and wait until the boundary is kept.\n"
            "# The token goes in a header file on the tmpfs, never in curl's argv (`ps` would\n"
            "# show it, and boundary.json records the process list).\n"
            f"umask 077; printf '{N.TOKEN_HEADER}: %s\\n' \"$EMX_SIM_TOKEN\" > {hdr}\n"
            f"curl -sS --noproxy '*' -m {HOOK_TIMEOUT_S - 30} -X POST -H @{hdr}"
            f" -H 'content-type: application/json'"
            f" --data-binary @- {N.BOUNDARY_URL} > {HOOK_OUT} 2>&1\nexit 0\n")


def hooks_json() -> dict:
    return {"hooks": {"PostCompact": [{"hooks": [
        {"type": "command", "command": HOOK, "timeout": HOOK_TIMEOUT_S}]}]}}


def redact(lines: list[str], secrets: list[str]) -> list[str]:
    out = []
    for x in lines:
        for v in secrets:
            if v:
                x = x.replace(v, "<redacted>")
        out.append(x)
    return out


def auth_tokens(auth: dict) -> list[str]:
    t = auth.get("tokens") or {}
    return [v for v in (t.get("access_token"), t.get("refresh_token"), t.get("id_token"),
                        auth.get("OPENAI_API_KEY")) if isinstance(v, str) and len(v) > 20]


def install_config(sb, server, cfg: CodexConfig, auth_bytes: bytes) -> None:
    """CODEX_HOME, config.toml, hooks.json, the hook script, auth.json on the tmpfs."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2.native import common as N
    r = sb.exec(f"mkdir -p {HOME_C} {SECRETS_C} && chmod 700 {SECRETS_C}", timeout_s=60,
                workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"codex home: {r.stderr[-300:]}")
    sb.put_file(toml_dumps(cfg.config(N.MCP_URL, N.TOKEN_HEADER)).encode(),
                f"{HOME_C}/config.toml")
    sb.put_file(json.dumps(hooks_json(), indent=1).encode(), f"{HOME_C}/hooks.json")
    sb.put_file(hook_script().encode(), HOOK)
    sb.put_file(auth_bytes, f"{SECRETS_C}/auth.json")
    r = sb.exec(f"chmod 755 {HOOK} && chmod 600 {SECRETS_C}/auth.json && "
                f"ln -sf {SECRETS_C}/auth.json {HOME_C}/auth.json && "
                f"mount | grep -q ' {SECRETS_C} type tmpfs'", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"codex config (is {SECRETS_C} a tmpfs?): {r.stderr[-300:]}")


SCAN_SKIP = ("/proc/", "/sys/", "/dev/")  # plus SECRETS_C, the one tmpfs we mount


def changed_files(sb) -> list[str]:
    """Added or changed paths in the container's filesystem (`docker diff`): what a commit
    adds on top of its image."""
    from compaction.deepswe import sandbox as SB
    with SB.using_host(sb.host):
        p = SB._docker(["diff", sb.name], timeout=300, check=False)
    if p.returncode != 0:
        raise SB.SandboxError(f"docker diff {sb.name} failed rc={p.returncode}")
    out = []
    for line in p.stdout.decode(errors="replace").splitlines():
        kind, _, path = line.partition(" ")
        if (kind in ("A", "C") and path and not path.startswith(SCAN_SKIP)
                and path != SECRETS_C and not path.startswith(SECRETS_C + "/")):
            out.append(path)
    return out


def secret_scan(sb, secrets: list[str]) -> list[str]:
    """Files a commit would keep (`changed_files`) that hold any of `secrets`. One grep pass
    for all of them; the values reach the container only as `docker exec -e` (as
    common.secret_scan). Fails closed: any grep error (exit > 1) or xargs failure raises, so
    an incomplete scan is never recorded as clean. Only the secrets tmpfs (SECRETS_C) is
    skipped: it is never committed."""
    from compaction.deepswe import sandbox as SB
    files = changed_files(sb)
    if not files:
        return []
    env = {f"{SECRET_ENV}_{i}": v for i, v in enumerate(secrets)}
    pats = " ".join(f'-e "${k}"' for k in env)
    # per batch: keep regular files (not symlinks), grep them; grep's 1 = no match
    inner = ('for f do shift; if [ -f "$f" ] && [ ! -L "$f" ]; then set -- "$@" "$f"; fi; '
             f'done; [ $# -eq 0 ] && exit 0; grep -lF {pats} -- "$@"; r=$?; '
             'if [ $r -gt 1 ]; then echo "__GREP_ERR $r" >&2; fi; exit 0')
    # the file list goes in on stdin, never to a file: a big tree's list overflowed the 1 MB
    # secrets tmpfs and lost helm's first boundary (24 Oct)
    cmd = f"xargs -0 -r -n 200 sh -c {shlex.quote(inner)} _"
    argv = SB.docker_argv(["exec", "-i", *[x for k in env for x in ("-e", k)], "-w", "/",
                           sb.name, "timeout", "-k", "5", "600", "bash", "-lc", cmd], sb.host)
    try:
        p = subprocess.run(argv, input="\0".join(files).encode() + b"\0",
                           capture_output=True, timeout=600 + SB.CLIENT_GRACE_S,
                           env={**os.environ, **env}, check=False)
    except subprocess.TimeoutExpired as e:
        raise SB.SandboxError("secret scan timed out (incomplete)") from e
    r = SB.ExecResult(p.returncode, p.stdout.decode(errors="replace"),
                      p.stderr.decode(errors="replace"), 0.0)
    if r.rc != 0 or "__GREP_ERR" in r.stderr:
        raise SB.SandboxError(f"secret scan incomplete (rc={r.rc}): {r.stderr[-300:]}")
    return sorted(x for x in r.stdout.splitlines() if x.strip())


def current_tokens(sb) -> list[str]:
    """The tokens in the container's auth.json now (Codex may have refreshed them). Fails
    closed: a failed read, invalid JSON or no tokens raises, so the scan never runs against
    the startup tokens alone."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2.native import common as N
    r = N.exec_env(sb, f"cat {SECRETS_C}/auth.json", {}, timeout_s=60)
    if r.rc != 0:
        raise SB.SandboxError(f"reading the container's auth.json failed rc={r.rc}")
    try:
        toks = auth_tokens(json.loads(r.stdout))
    except (ValueError, AttributeError) as e:
        raise SB.SandboxError("the container's auth.json is not valid JSON") from e
    if not toks:
        raise SB.SandboxError("the container's auth.json holds no tokens")
    return toks


def cli_env(egress, server) -> dict[str, str]:
    return {"CODEX_HOME": HOME_C, "EMX_SIM_TOKEN": server.token, "RUST_LOG": "warn",
            **egress.proxy_env()}


def cli_command(cfg: CodexConfig, *, resume: str | None = None,
                prompt: str | None = None) -> str:
    """The in-container command line (no secrets). The prompt comes on stdin unless given."""
    parts = [CLI, "exec"]
    if resume:
        parts += ["resume", resume]
    parts += ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
              "--skip-git-repo-check", "--json", "-m", cfg.model]
    if not resume:
        parts += ["-C", "/app"]
    parts += ["--", prompt if prompt is not None else "-"]
    return ("unset OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL; cd /app && "
            + shlex.join(parts))


def stop_cli(sb) -> None:
    sb.exec(f"pkill -TERM -f '^{CLI}' ; sleep 3; pkill -KILL -f '^{CLI}' ; true", timeout_s=60,
            workdir="/")


def container_rollouts(sb) -> list[str]:
    r = sb.exec(f"ls -1 {HOME_C}/sessions/*/*/*/rollout-*.jsonl 2>/dev/null; true",
                timeout_s=60, workdir="/")
    return sorted(x for x in r.stdout.splitlines() if x.strip())


def read_container_rollout(sb, path: str) -> list[dict]:
    text = sb.exec(f"cat {shlex.quote(path)}", timeout_s=120, workdir="/").stdout
    out = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def api_calls(lines: list[dict]) -> int:
    return sum(1 for x in lines if x.get("type") == "event_msg"
               and (x.get("payload") or {}).get("type") == "token_count"
               and ((x["payload"].get("info") or {}).get("last_token_usage") or {})
               .get("input_tokens"))


def _same(a: dict, b: dict) -> bool:
    return (a.get("role") == b.get("role") and a.get("content") == b.get("content")
            and a.get("tool_calls") == b.get("tool_calls"))


def rollout_boundary(lines: list[dict], k: int, summaries: dict[int, str], *, model: str,
                     policy: str = POLICY_REMOTE):
    """Boundary k of a rollout as a common.NativeBoundary: pre_messages = the history just
    before the k-th `compacted` line (earlier summaries as their recited text), post_messages =
    the replacement history (Codex keeps every user message and the developer messages
    verbatim, then the summary), kept_indices = the pre_messages each kept message equals."""
    from compaction.s2.native import common as N
    at = compaction_lines(lines)[k - 1]
    pre = history_at(lines, at - 1, summaries)
    post = history_at(lines, at, summaries)
    si = post.summary_indices[-1] if post.summary_indices else None
    if si is None:
        raise ValueError(f"boundary {k}: no compaction item in the replacement history")
    kept, used = [], set()
    for j, m in enumerate(post.messages):
        if j == si or m["role"] == "system":
            continue
        for i, p in enumerate(pre.messages):
            if i not in used and _same(m, p):
                kept.append(i)
                used.add(i)
                break
    payload = lines[at]["payload"]
    summary = summaries.get(k) or ""
    post_tokens = sum(len(str(m.get("content") or "")) for m in post.messages) // 4 + 1
    return N.NativeBoundary(
        pre_messages=pre.messages, post_messages=post.messages, summary=summary,
        summary_index=si, kept_indices=sorted(kept),
        pre_tokens=last_input_tokens(lines, at - 1), post_tokens=post_tokens, trigger="auto",
        policy=policy, model=f"openai/{model}",
        info={"compacted_line": at, "local_message_chars": len(payload.get("message") or ""),
              "encrypted_items": len(encrypted_items(lines[at])),
              "post_tokens_is_estimate": True})


@dataclass
class BaseConfig:
    task: Any  # interact_run.InteractTask
    root: Path
    attempt: int = 1
    prefix: str = ""
    cx: CodexConfig = field(default_factory=CodexConfig)
    host: str | None = "ssh://rig"
    steps: tuple[str, ...] = ("01_plan", "02_implement")
    step_timeout_s: dict = field(default_factory=lambda: dict(STEP_TIMEOUT_S))
    stop_after: int | None = None  # stop after this many boundaries (smokes)
    recite_repeats: int = 3  # the main session, 24 Oct: grade the majority of 3
    cpus: float = 2
    memory_mb: int = 8192
    auth_path: Path = field(default_factory=lambda: Path.home() / ".codex" / "auth.json")
    labels: dict = field(default_factory=dict)  # top-level run.json keys (e.g. an arm label)
    on_started: Callable[[], None] | None = None  # once the session's containers are up

    @property
    def run_key(self) -> str:
        return run_key(self.task.task_id, self.attempt, self.prefix)

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / self.run_key


def run_key(task_id: str, attempt: int, prefix: str = "") -> str:
    return f"{prefix}{task_id}-codex-luna-a{attempt}"


def next_attempt(root_dir: Path, task_id: str, prefix: str = "") -> int:
    pat = re.compile(rf"{re.escape(prefix + task_id)}-codex-luna-a(\d+)")
    ns = [int(m.group(1)) for p in Path(root_dir).glob("*") if (m := pat.fullmatch(p.name))]
    return max(ns, default=0) + 1


def _event_watch(state: dict) -> Callable[[str], None]:
    def on_line(line: str) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        t = ev.get("type")
        if t == "thread.started":
            state["thread_id"] = ev.get("thread_id")
        elif t == "turn.completed":
            state["usage"] = ev.get("usage")
        elif t in ("turn.failed", "error"):
            state.setdefault("errors", []).append(ev)
    return on_line


def majority(texts: list[str]) -> tuple[int, list[float]]:
    """The index of the recitation with the highest mean item overlap with the others (ties:
    the earliest), and each one's mean overlap. One text: index 0."""
    n = len(texts)
    if n <= 1:
        return 0, [1.0] * n
    means = [sum(item_overlap(texts[i], texts[j]) for j in range(n) if j != i) / (n - 1)
             for i in range(n)]
    best = max(range(n), key=lambda i: (means[i], -i))  # full precision; ties: the earliest
    return best, [round(m, 3) for m in means]


class Reciter:
    """Route (b) on the laptop for a run: clean CODEX_HOMEs (auth copies) and an empty
    working dir, so the recitations can't read any workspace. Each concurrent recitation gets
    its own CODEX_HOME: parallel `codex exec resume`s in one home collide on its state DB
    ("thread/resume failed: list_turns is not supported yet", 24 Oct, boa a6)."""

    def __init__(self, run_dir: Path, auth_path: Path):
        self.dir = Path(tempfile.mkdtemp(prefix="emx-codex-recite-"))
        self.auth_path = Path(auth_path)
        try:
            self.home = CodexHome(self.dir / "home", {"model": MODEL}).setup(auth_path)
            self.cwd = self.dir / "empty"
            self.cwd.mkdir()
        except BaseException:
            self.close()  # the auth copy must not outlive a failed setup
            raise
        self.log_dir = Path(run_dir) / "recite"
        self._n = 0
        self._homes: list[CodexHome] = [self.home]
        self._lock = threading.Lock()

    def _home(self, i: int) -> CodexHome:
        with self._lock:
            while len(self._homes) <= i:
                self._homes.append(CodexHome(self.dir / f"home{len(self._homes)}",
                                             {"model": MODEL}).setup(self.auth_path))
            return self._homes[i]

    def run(self, lines: list[dict], k: int, repeats: int, *, label: str = "",
            timeout_s: int = 900) -> list[Recitation]:
        """`repeats` recitations, concurrently (each on its own fork of the rollout). Log files
        are recite_b<k><label>_t<n>.jsonl with n unique within the run, so retries and later
        sessions never overwrite earlier logs."""
        from concurrent.futures import ThreadPoolExecutor
        with self._lock:
            tags = [f"{label}_t{self._n + i + 1}" for i in range(repeats)]
            self._n += repeats
        homes = [self._home(i) for i in range(len(tags))]  # one CODEX_HOME per concurrent run
        with ThreadPoolExecutor(max_workers=max(1, repeats)) as ex:
            futs = [ex.submit(recite, h, lines, k, cwd=self.cwd, log_dir=self.log_dir,
                              tag=t, timeout_s=timeout_s) for h, t in zip(homes, tags,
                                                                          strict=True)]
            return [f.result() for f in futs]

    def run_until(self, lines: list[dict], k: int, repeats: int, *, label: str = "",
                  left: Callable[[], float] = lambda: 900.0, retries: int = 2,
                  have: list[Recitation] | None = None) -> list[Recitation]:
        """Recitations until `repeats` are valid (counting `have`), with up to `retries`
        extra rounds for the missing ones, each bounded by `left()` (seconds remaining)."""
        recs = list(have or [])
        for attempt in range(retries + 1):
            missing = repeats - sum(r.valid for r in recs)
            if missing <= 0 or left() < 60:
                break
            recs += self.run(lines, k, missing, label=label, timeout_s=int(left()))
        return recs

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def run_base(cfg: BaseConfig, *, sim_model, eligible=None, session_cls=None) -> dict:
    """One task in Codex (the comment block above). Writes the run dir like the pi arm's
    run_base; returns result.json's content."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2 import interact as I
    from compaction.s2 import interact_run as IR
    from compaction.s2 import session as SS
    from compaction.s2.native import common as N
    session_cls = session_cls or N.NativeSession
    t0 = time.monotonic()
    task = cfg.task
    auth_bytes = Path(cfg.auth_path).read_bytes()
    secrets = auth_tokens(json.loads(auth_bytes))
    if not secrets:
        raise RuntimeError(f"{cfg.auth_path}: no tokens")
    rd = IR._fresh_dir(cfg.run_dir)
    rj = N.native_run_json(run_key=cfg.run_key, task_dict=task.to_dict(),
                           persona_sha256=I.sha256(task.persona), arm=ARM, model=cfg.cx.model,
                           policy=POLICY_REMOTE, threshold=cfg.cx.threshold(),
                           cli_version=CLI_VERSION,
                           extra={"effort": cfg.cx.effort, "web_search": "disabled",
                                  "host": cfg.host, "egress_allow": list(N.ALLOW[ARM]),
                                  "steps": list(cfg.steps), "step_timeout_s": cfg.step_timeout_s,
                                  "stop_after": cfg.stop_after,
                                  "summary_source": "recitation (route b)",
                                  "recite_prompt": RECITE_PROMPT,
                                  "recite_repeats": cfg.recite_repeats})
    rj.update(cfg.labels)
    IR._dump(rd / "run.json", rj)
    keep = SS.KeepStore(rd, cfg.run_key, host=cfg.host)
    res: dict = {"run_key": cfg.run_key, "end": "error", "boundaries": [], "error": None,
                 "steps": {}, "timings": [], "sim_cost_usd": 0.0, "plan_md": None}
    stop = threading.Event()
    end_reason: dict = {}
    cur: dict = {"step": None, "seen": {}, "summaries": {}}
    reciter: Reciter | None = None
    ns = None

    def mark(ev: str, **kw) -> None:
        res["timings"].append({"event": ev, "utc": IR.utc(),
                               "s": round(time.monotonic() - t0, 1), **kw})

    def on_cap(e: BaseException) -> None:
        end_reason.setdefault("end", "run_cap")
        stop.set()

    holder: dict = {}

    def on_boundary(payload: dict) -> dict:
        try:
            return capture(holder["ns"], payload, rd=rd, rj=rj, keep=keep, cfg=cfg,
                           step=cur["step"], seen=cur["seen"], summaries=cur["summaries"],
                           secrets=secrets, reciter=reciter, eligible=eligible, stop=stop,
                           end_reason=end_reason, res=res, mark=mark)
        except Exception as e:  # a lost boundary ends the run (recorded), never silently
            res.setdefault("boundary_errors", []).append(
                {"utc": IR.utc(), "error": traceback.format_exc()[-4000:]})
            end_reason.setdefault("end", "boundary_error")
            stop.set()
            with contextlib.suppress(Exception):
                stop_cli(holder["ns"].sb)
            mark("boundary_error", error=f"{type(e).__name__}: {e}"[:300])
            raise

    def sim_factory(runner):
        # NativeSession's runner is interact.sandbox_runner on the shared tolerant exec (a
        # client timeout settles the command and reports rc 124; the container stays)
        return I.UserSim(task.persona, sim_model, runner, purpose=f"sim:{cfg.run_key}")

    try:
        reciter = Reciter(rd, cfg.auth_path)
        ensure_cli_volume(cfg.host, task.info.image)
        ns = session_cls(ARM, task.info.image, sim_factory, host=cfg.host,
                         extra_args=("-v", f"{CLI_VOLUME}:{CLI_DIR}:ro",
                                     "--tmpfs", f"{SECRETS_C}:rw,mode=700,size=1m"),
                         cpus=cfg.cpus, memory_mb=cfg.memory_mb, on_boundary=on_boundary,
                         on_cap=on_cap)
        with ns:
            holder["ns"] = ns
            res["container"] = ns.sb.name
            res["network"] = ns.egress.network
            install_config(ns.sb, ns.server, cfg.cx, auth_bytes)
            if cfg.on_started is not None:
                with contextlib.suppress(Exception):  # bookkeeping only (a launch gate)
                    cfg.on_started()
            res["egress_check"] = N.egress_check(ns.sb, ns.egress, "https://chatgpt.com/")
            IR._dump(rd / "egress_check.json", res["egress_check"])
            if not res["egress_check"]["ok"]:
                raise RuntimeError("egress check failed; see egress_check.json")
            mark("started")
            env = cli_env(ns.egress, ns.server)
            for step in cfg.steps:
                if stop.is_set():
                    break
                cur["step"] = step
                ns.server.step = step
                state: dict = {}
                sd = rd / "steps" / step
                before = set(container_rollouts(ns.sb))
                r = detached_step(ns.sb, cli_command(cfg.cx), name=step, env=env,
                                  stdin=task.steps[step].encode(),
                                  timeout_s=cfg.step_timeout_s.get(step, 5400),
                                  log_path=sd / "events.jsonl", stop=stop,
                                  on_line=_event_watch(state))
                if r.killed:
                    stop_cli(ns.sb)
                new = [p for p in container_rollouts(ns.sb) if p not in before]
                lines = read_container_rollout(ns.sb, new[-1]) if new else []
                write_rollout(sd / "rollout.jsonl", lines)
                res["steps"][step] = {"thread_id": state.get("thread_id"), "rc": r.rc,
                                      "reconnects": getattr(r, "reconnects", None),
                                      "output_incomplete": getattr(r, "output_incomplete",
                                                                   None),
                                      "seconds": r.seconds, "killed": r.killed,
                                      "timed_out": r.timed_out, "api_calls": api_calls(lines),
                                      "compactions": len(compaction_lines(lines)),
                                      "usage": state.get("usage"),
                                      "errors": state.get("errors"),
                                      "rollout": new[-1] if new else None}
                mark("step_done", step=step, rc=r.rc, killed=r.killed)
                if step == "01_plan":
                    res["plan_md"] = ns.sb.exec("cat /tmp/plan.md 2>/dev/null", timeout_s=60,
                                                workdir="/").stdout or None
                limit_s = cfg.step_timeout_s.get(step, 5400)
                alive = ns.sb.exec("true", timeout_s=60, workdir="/").rc == 0
                res["steps"][step]["container_alive"] = alive
                if not alive or (r.rc == 137 and not r.killed and r.seconds < limit_s - 30):
                    # SIGKILL well before the step's limit, or the container gone: an
                    # infrastructure failure (e.g. OOM), never a time cap (rerun it)
                    end_reason.setdefault("end", "infra_killed")
                    break
                if r.timed_out or r.killed == "client_timeout":
                    end_reason.setdefault("end", "time_cap")
                    break
                if r.rc != 0 and not r.killed:
                    end_reason.setdefault("end", "cli_error")
                    break
            res["end"] = end_reason.get("end") or "submitted"
            n_comp = sum(st.get("compactions") or 0 for st in res["steps"].values())
            res["compactions_in_rollouts"] = n_comp
            if n_comp != len(res["boundaries"]) and res["end"] != "boundary_error":
                res["end"] = "boundary_missing"  # never report a run with a lost boundary
            after = N.exec_env(ns.sb, f"cat {SECRETS_C}/auth.json", {}, timeout_s=60).stdout
            res["auth_refreshed"] = bool(after) and after.encode() != auth_bytes
            res["exec_stats"] = dict(getattr(ns, "exec_stats", None) or {})  # client timeouts
            res["egress_events"] = ns.egress.events()
            res["boundary_calls"] = ns.server.boundary_calls
            sim = ns.server.sim
            IR._dump(rd / "sim" / "ask_log.json", sim.ask_log)
            IR._dump(rd / "sim" / "rounds.json", sim.rounds)
            IR._dump(rd / "sim" / "replies.json", ns.server.rows)
            res["sim_cost_usd"] = round(sum(x.get("cost_usd") or 0.0 for x in sim.rounds), 6)
    except Exception as e:  # noqa: BLE001 - recorded; the session tore itself down
        res["end"] = "infra_error" if isinstance(e, SB.SandboxError) else "error"
        res["error"] = traceback.format_exc()[-6000:]
    finally:
        if reciter is not None:
            reciter.close()
        res["cleanup_unconfirmed"] = ns.cleanup_unconfirmed if ns is not None else []
        res["seconds"] = round(time.monotonic() - t0, 1)
        mark("ended", end=res["end"])
        IR._dump(rd / "result.json", res)
    return res


def capture(ns, payload: dict, *, rd: Path, rj: dict, keep, cfg: BaseConfig, step: str | None,
            seen: dict[str, int], summaries: dict[str, dict[int, str]], secrets: list[str],
            reciter: Reciter, eligible, stop: threading.Event,
            end_reason: dict, res: dict, mark) -> dict:
    """The PostCompact hook's handler: one boundary (the comment block above). The CLI is
    blocked in its hook until this returns."""
    from compaction.s2 import interact as I
    from compaction.s2 import interact_run as IR
    from compaction.s2.native import common as N
    if payload.get("hook_event_name") not in (None, "PostCompact"):
        return {"ok": True, "ignored": payload.get("hook_event_name")}
    sb, server = ns.sb, ns.server
    path = str(payload.get("transcript_path") or "")
    want = seen.get(path, 0) + 1
    t_hook = time.monotonic()
    deadline = t_hook + CAPTURE_DEADLINE_S  # the hook's curl gives up at HOOK_TIMEOUT_S - 30

    def left() -> int:
        return int(deadline - time.monotonic())

    def need(seconds: int, phase: str) -> None:
        """Refuse to start a phase that could outlive the hook (the CLI would resume)."""
        if left() < seconds:
            raise RuntimeError(f"capture deadline: {left()}s left, {phase} may need "
                               f"{seconds}s")
    lines: list[dict] = []
    while True:  # the compacted line may land a moment after the hook starts
        lines = read_container_rollout(sb, path)
        if len(compaction_lines(lines)) >= want:
            break
        if time.monotonic() - t_hook > ROLLOUT_WAIT_S:
            raise RuntimeError(f"rollout {path} has no compacted line #{want} after "
                               f"{ROLLOUT_WAIT_S}s")
        time.sleep(0.5)
    lines = lines[:compaction_lines(lines)[want - 1] + 1]
    k = N.next_k(rd)
    phases = {"rollout_wait_s": round(time.monotonic() - t_hook, 1)}
    need(SCAN_BUDGET_S + COMMIT_BUDGET_S + 60, "scan + fingerprint + commit")
    t = time.monotonic()
    procs = redact(N.processes(sb), [server.token])
    leaks = secret_scan(sb, sorted(set(secrets) | set(current_tokens(sb))))
    phases["secret_scan_s"] = round(time.monotonic() - t, 1)
    if leaks:
        raise RuntimeError(f"ChatGPT auth tokens found in the container ({leaks}); "
                           "refusing to keep this image")
    t = time.monotonic()
    fp = I.fingerprint(I.sandbox_runner(sb))
    phases["fingerprint_s"] = round(time.monotonic() - t, 1)
    need(COMMIT_BUDGET_S, "the kept commit")
    snap = N.snapshot(sb, keep, k)
    snap["processes"] = procs
    phases["snapshot_s"] = snap.get("snapshot_s")
    mark("snapshot", k=k)
    prior = summaries.setdefault(path, {})
    t = time.monotonic()
    label = f"_{step or 'step'}_k{k}"
    if left() < 60:
        raise RuntimeError(f"boundary {k}: no time left for recitation before the hook "
                           "deadline")
    recs = reciter.run_until(lines, want, cfg.recite_repeats, label=label, left=left,
                             retries=RECITE_RETRIES)
    phases["recite_s"] = round(time.monotonic() - t, 1)
    mark("recited", k=k, valid=[r.valid for r in recs])
    good = [r for r in recs if r.valid]
    if not good:
        raise RuntimeError(f"boundary {k}: no valid recitation in {len(recs)} tries "
                           f"(rc={[r.rc for r in recs]}, tools={[r.tool_calls for r in recs]})")
    texts = [r.text or "" for r in good]
    agreement = [round(item_overlap(texts[i], texts[j]), 3)
                 for i in range(len(texts)) for j in range(i + 1, len(texts))]
    used, mean_agree = majority(texts)
    chosen = good[used]
    text = chosen.text or ""
    prior[want] = text
    nb = rollout_boundary(lines, want, prior, model=cfg.cx.model)
    invented = invented_tokens(text, history_text(lines, compaction_lines(lines)[want - 1] - 1))
    nb.native_files.update({
        "rollout.jsonl": "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
        "hook_payload.json": json.dumps(payload, indent=1, ensure_ascii=False),
        "recitations.json": json.dumps([r.to_dict() for r in recs], indent=1,
                                       ensure_ascii=False)})
    extra = {"harness": ARM, "cli_version": CLI_VERSION, "thread_id": payload.get("session_id"),
             "rollout_path": path, "boundary_in_session": want,
             "auto_compact_limit": cfg.cx.auto_compact_limit, "model": cfg.cx.model,
             "effort": cfg.cx.effort, "summary_source": "recitation",
             "recite_ok": True, "recite_tries": len(recs), "recite_valid": len(good),
             "recite_used": recs.index(chosen), "recite_mean_agreement": mean_agree,
             "recite_selection": "majority (highest mean item overlap with the others)",
             "hook_phases": phases,
             "recite_agreement": agreement, "recite_invented_tokens": invented[:50],
             "recite_plaintext_bound": chosen.plaintext_bound,
             "recite_tool_calls": [r.tool_calls for r in recs], "secret_scan": "clean"}
    bdir = N.write_boundary(rd, k, nb, run_json=rj, snap=snap, sim_export=server.export(),
                            sim_rows=list(server.rows), step=step or "",
                            step_instruction=cfg.task.steps.get(step or "", ""),
                            fingerprint=fp, sim_probe_sha256=IR.sim_probe(server.sim),
                            calls_in_step=api_calls(lines), extra=extra, eligible=eligible)
    seen[path] = want
    res["boundaries"].append(k)
    mark("boundary_saved", k=k, pre_tokens=nb.pre_tokens, **phases)
    fields = json.loads((bdir / "boundary" / "boundary.json").read_text())
    if fields.get("eligible"):
        end_reason.setdefault("end", "eligible")
    elif cfg.stop_after and len(res["boundaries"]) >= cfg.stop_after:
        end_reason.setdefault("end", "stop_after")
    if end_reason.get("end") in ("eligible", "stop_after"):
        stop.set()
        stop_cli(sb)  # before the hook returns, so the CLI takes no step past the boundary
    return {"ok": True, "k": k}


# ------------------------------------------------------------------------------------------
# Q2: forks from a kept boundary, with the encrypted summary replaced by readable text
# ------------------------------------------------------------------------------------------


def fork(bdir: Path, out_dir: Path, *, new_summary: str, prompt: str, sim_model, persona: str,
         cfg: CodexConfig | None = None, host: str | None = "ssh://rig",
         auth_path: Path | None = None, timeout_s: int = 1800, session_cls=None) -> dict:
    """Resume boundary `bdir` (a b<k> dir of a Codex run) from its kept image, with the k-th
    compaction's encrypted item replaced by `new_summary` (`replace_compaction`: the repair
    and matched arms carry our summary text, not Codex's), under a new thread id, given
    `prompt`. Writes out_dir/{fork.json, events.jsonl, rollout.jsonl, input_rollout.jsonl}."""
    from compaction.s2 import interact as I
    from compaction.s2.native import common as N
    session_cls = session_cls or N.NativeSession
    bdir, out_dir = Path(bdir), Path(out_dir)
    b = json.loads((bdir / "boundary" / "boundary.json").read_text())
    lines = read_rollout(bdir / "native" / "rollout.jsonl")
    exported = json.loads((bdir / "boundary" / "sim_state.json").read_text())
    cfg = cfg or CodexConfig(model=b.get("model") or MODEL, effort=b.get("effort"),
                             auto_compact_limit=b.get("auto_compact_limit"))
    want = int(b.get("boundary_in_session") or len(compaction_lines(lines)))
    edited = replace_compaction(lines, want, new_summary)
    edited = edited[:compaction_lines(edited)[want - 1] + 1]
    old, new_id = thread_id(edited), str(uuid.uuid4())
    body = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in edited).replace(old, new_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "input_rollout.jsonl").write_text(body)
    auth_bytes = Path(auth_path or Path.home() / ".codex" / "auth.json").read_bytes()
    res: dict = {"bdir": str(bdir), "image": b["tag"], "input_thread": new_id, "prompt": prompt,
                 "summary_sha256": I.sha256(new_summary), "boundary_in_session": want,
                 "cx": {"model": cfg.model, "effort": cfg.effort,
                        "auto_compact_limit": cfg.auto_compact_limit},
                 "started_utc": N.utc()}

    holder: dict = {}

    def sim_factory(runner):
        return I.UserSim.restore(exported, persona, sim_model, runner, purpose="sim:fork")

    stop = threading.Event()
    ns = session_cls(ARM, b["tag"], sim_factory, host=host, setup_git=False,
                     extra_args=("-v", f"{CLI_VOLUME}:{CLI_DIR}:ro",
                                 "--tmpfs", f"{SECRETS_C}:rw,mode=700,size=1m"),
                     cpus=2, memory_mb=3072,
                     on_boundary=lambda p: {"ok": True, "ignored": "fork"},
                     on_cap=lambda e: stop.set())
    try:
        with ns:
            holder["ns"] = ns
            ns.server.step = b.get("stage") or "02_implement"
            install_config(ns.sb, ns.server, cfg, auth_bytes)
            day = datetime.now(UTC)
            path = (f"{HOME_C}/sessions/{day.strftime('%Y/%m/%d')}/rollout-"
                    f"{day.strftime('%Y-%m-%dT%H-%M-%S')}-{new_id}.jsonl")
            ns.sb.put_file(body.encode(), path)
            state: dict = {}
            r = N.stream_exec(ns.sb, cli_command(cfg, resume=new_id, prompt=prompt),
                              timeout_s=timeout_s, log_path=out_dir / "events.jsonl",
                              env=cli_env(ns.egress, ns.server), stop=stop,
                              on_line=_event_watch(state))
            if r.killed:
                stop_cli(ns.sb)
            write_rollout(out_dir / "rollout.jsonl", read_container_rollout(ns.sb, path))
            res.update(rc=r.rc, seconds=r.seconds, killed=r.killed, usage=state.get("usage"),
                       errors=state.get("errors"), thread=state.get("thread_id"),
                       sim_rows=ns.server.rows)
    finally:
        res["cleanup_unconfirmed"] = ns.cleanup_unconfirmed
        res["ended_utc"] = N.utc()
        (out_dir / "fork.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


def final_text(events_path: Path) -> str:
    """The last agent message in a `--json` events log."""
    out = ""
    for line in Path(events_path).read_text().splitlines():
        with contextlib.suppress(ValueError):
            ev = json.loads(line)
            it = ev.get("item") or {}
            if ev.get("type") == "item.completed" and it.get("type") == "agent_message":
                out = it.get("text") or ""
    return out


# ------------------------------------------------------------------------------------------
# Grading layout: expose native boundaries to retention.boundary_dirs / lossaudit
# ------------------------------------------------------------------------------------------


def stage(run_dirs: list[Path], stage_root: Path) -> list[Path]:
    """Expose each complete native boundary `<run>/b<k>/boundary` as
    `<stage_root>/<run>-b<k>/a1 -> <run>/b<k>`, the `<cohort>/a<n>/boundary` layout
    `retention.boundary_dirs` (and so `lossaudit`) reads, without touching the shared modules.
    One cohort per boundary, because boundary_dirs keeps only the latest attempt per cohort.
    Complete = boundary.json and post_messages.json present (boundary.json is written last).
    Idempotent; refuses a link that already points somewhere else. Returns the links."""
    stage_root = Path(stage_root)
    out = []
    for rd in run_dirs:
        rd = Path(rd).resolve()
        ks = sorted((int(m.group(1)), p) for p in rd.iterdir()
                    if p.is_dir() and (m := re.fullmatch(r"b(\d+)", p.name)))
        for _, b in ks:
            if not ((b / "boundary" / "boundary.json").exists()
                    and (b / "boundary" / "post_messages.json").exists()):
                continue
            link = stage_root / f"{rd.name}-{b.name}" / "a1"
            if link.is_symlink() or link.exists():
                if link.resolve() != b:
                    raise FileExistsError(f"{link} points to {link.resolve()}, not {b}")
            else:
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(b, target_is_directory=True)
            out.append(link)
    return out


def loss_ledger(rows: list[dict]) -> dict:
    """New vs inherited lost items across one run's boundaries (lossaudit losses.jsonl rows),
    after `interact_reqs.loss_ledger`'s rule but on lossaudit items: a lost item at b<k> is
    INHERITED when b<k-1> has an item with the same (normalised) quote graded dropped or
    misstated (the previous summary already lacked it), NEW when that item was graded kept or
    when k = 1, and UNMATCHED when b<k-1> has no item with that quote (disclosed since, or
    extracted differently: reported apart, not guessed). Only valid items with a valid grade
    count as the previous boundary's evidence; duplicates of one quote that disagree make it
    unmatched."""
    def norm(q: str | None) -> str:
        return re.sub(r"\s+", " ", (q or "").strip().lower())

    def k_of(r: dict) -> int:
        m = re.search(r"/b(\d+)$", str(r.get("boundary_dir") or "").rstrip("/"))
        return int(m.group(1)) if m else 0

    def true(v) -> bool:
        return v is True or str(v) in ("True", "true", "1")

    by_k: dict[int, dict[str, str]] = {}
    for r in rows:
        if true(r.get("valid")) and true(r.get("grade_valid", True)):
            per = by_k.setdefault(k_of(r), {})
            q, g = norm(r.get("quote")), str(r.get("grade"))
            per[q] = g if per.get(q) in (None, g) else "conflict"  # disagreeing duplicates
    out = {"new": 0, "inherited": 0, "unmatched": 0, "by_boundary": {}}
    for r in rows:
        if not (r.get("lost") is True or str(r.get("lost")) == "True"):
            continue
        k = k_of(r)
        prev = by_k.get(k - 1, {}).get(norm(r.get("quote"))) if k > 1 else "kept"
        status = ("inherited" if prev in ("dropped", "misstated") else
                  "new" if prev == "kept" else "unmatched")
        out[status] += 1
        out["by_boundary"].setdefault(f"b{k}", {"new": 0, "inherited": 0, "unmatched": 0})
        out["by_boundary"][f"b{k}"][status] += 1
    return out


# ------------------------------------------------------------------------------------------
# Steps that survive a dropped docker transport
# ------------------------------------------------------------------------------------------

STEP_DIR = "/opt/emx/steps"
FOLLOW_CHUNK_S = 300  # each tail session's in-container limit; the follower reconnects
RC_CHECK_S = 30  # at most one rc check per step per 30 s (the main session: rig sshd load)


def raw_exec(sb, cmd: str, *, timeout_s: float, workdir: str = "/"):
    """A read-only `docker exec` with no guard and no settling: a client timeout or a
    transport error just returns rc 124 / the error, never touches the container. For the
    follower's own reads (rc, drain, stderr, cleanup), which are safe to abandon."""
    from compaction.deepswe import sandbox as SB
    t = max(1, int(timeout_s))
    argv = SB.docker_argv(SB.exec_args(sb.name, cmd, t, workdir), sb.host)
    try:
        p = subprocess.run(argv, capture_output=True, timeout=t, check=False)  # the budget
    except subprocess.TimeoutExpired:
        return SB.ExecResult(124, "", "client timeout", float(t))
    except OSError as e:
        return SB.ExecResult(125, "", f"{type(e).__name__}: {e}", 0.0)
    return SB.ExecResult(p.returncode, p.stdout.decode(errors="replace"),
                         p.stderr.decode(errors="replace"), 0.0)


def detached_step(sb, cmd: str, *, name: str, env: dict[str, str], stdin: bytes | None,
                  timeout_s: int, log_path: Path, stop: threading.Event | None = None,
                  on_line: Callable[[str], None] | None = None):
    """Run `cmd` detached in the container (setsid, under `timeout -k 5 timeout_s`), stdout
    to <STEP_DIR>/<name>/out, its exit code to .../rc, and follow `out` from the laptop with
    `tail -F` sessions that resume from the last complete line's byte offset. A dropped
    `docker -H ssh://` transport (24 Oct: `ssh ... dial-stdio` exit 255 ended two runs whose
    CLI was still working) costs a reconnect, not the run.

    Every follow-time docker call is `raw_exec` (read-only, no guard, no settling: a stalled
    transport can't remove the container), bounded by the time left. Stop and the deadline are checked before every follow session
    and during backoff; rc is read at most once per RC_CHECK_S across sessions; the final
    drain is retried from the acknowledged offset, and if it never succeeds the result says
    so (`output_incomplete`) instead of passing a short log as complete. Returns
    common.StreamResult plus `reconnects` and `output_incomplete`."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2.native import common as N
    d = f"{STEP_DIR}/{name}"
    r = sb.exec(f"rm -rf {d} && mkdir -p {d}", timeout_s=60, workdir="/")  # setup: fail closed
    if r.rc != 0:
        raise SB.SandboxError(f"step dir {d}: rc={r.rc} {r.stderr[-200:]}")
    if stdin is not None:
        sb.put_file(stdin, f"{d}/stdin")
    src = f"{d}/stdin" if stdin is not None else "/dev/null"
    inner = (f"timeout -k 5 {int(timeout_s)} bash -lc {shlex.quote(cmd)} < {src} > {d}/out "
             f"2> {d}/err; echo $? > {d}/rc.tmp && mv {d}/rc.tmp {d}/rc")
    launch = f"setsid nohup bash -c {shlex.quote(inner)} > /dev/null 2>&1 < /dev/null &"
    r = N.exec_env(sb, launch, env, timeout_s=60, workdir="/app")
    if r.rc != 0:
        raise SB.SandboxError(f"step launch failed rc={r.rc}: {r.stderr[-300:]}")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    deadline = t0 + timeout_s + SB.CLIENT_GRACE_S + 60
    stop = stop or threading.Event()
    state = {"last_check": t0, "rc": None}
    offset, sessions, killed, backoff, incomplete = 0, 0, None, 5, False

    def left() -> float:
        return deadline - time.monotonic()

    def check_rc() -> int | None:
        state["last_check"] = time.monotonic()
        rr = raw_exec(sb, f"cat {d}/rc 2>/dev/null", timeout_s=min(30, max(1, left())))
        v = rr.stdout.strip()
        if rr.rc == 0 and v.lstrip("-").isdigit():
            state["rc"] = int(v)
        return state["rc"]

    def due() -> bool:
        return state["rc"] is None and time.monotonic() - state["last_check"] >= RC_CHECK_S

    with open(log_path, "ab") as out:
        def emit(raw: bytes) -> None:
            out.write(raw)
            out.flush()
            if on_line is not None:
                with contextlib.suppress(Exception):
                    on_line(raw.decode(errors="replace"))

        while True:
            if stop.is_set():
                killed = "stop"
                break
            if left() <= 0:
                killed = "client_timeout"
                break
            sessions += 1
            chunk = int(max(1, min(FOLLOW_CHUNK_S, left())))
            argv = SB.docker_argv(
                ["exec", sb.name, "timeout", "-k", "5", str(chunk), "tail", "-c",
                 f"+{offset + 1}", "-F", f"{d}/out"], sb.host)
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL)
            got = {"n": 0}

            def pump(p=p, got=got) -> None:
                assert p.stdout is not None
                for raw in iter(p.stdout.readline, b""):
                    if not raw.endswith(b"\n"):
                        break  # a partial line: re-read it from `offset` next session
                    emit(raw)
                    got["n"] += len(raw)

            th = threading.Thread(target=pump, daemon=True)
            th.start()
            while p.poll() is None:
                if stop.is_set():
                    killed = "stop"
                    break
                if left() <= 0:
                    killed = "client_timeout"
                    break
                if due() and check_rc() is not None:
                    time.sleep(2)  # let tail deliver the last lines
                    break
                time.sleep(0.5)
            if p.poll() is None:
                p.kill()
            with contextlib.suppress(Exception):
                p.wait(timeout=30)
            th.join(timeout=30)
            offset += got["n"]
            if killed:
                break
            if due():
                check_rc()
            if state["rc"] is not None:
                # drain from the acknowledged offset; retry, never accept a failed drain
                for attempt in range(6):
                    if left() <= 0 or stop.is_set():
                        break
                    try:
                        rest = raw_exec(sb, f"tail -c +{offset + 1} {d}/out",
                                        timeout_s=min(120, max(1, left())))
                    except Exception:  # noqa: BLE001 - a transport error is a failed try
                        rest = None
                    if rest is not None and rest.rc == 0:
                        data = rest.stdout.encode()
                        for line in data.splitlines(keepends=True):
                            emit(line if line.endswith(b"\n") else line + b"\n")
                        offset += len(data)
                        break
                    stop.wait(max(0.0, min(30, 5 * 2 ** attempt, left())))
                else:
                    incomplete = True
                if left() <= 0 or stop.is_set():
                    incomplete = True
                break
            # the transport dropped or the chunk ended: follow again, backing off on refusals
            backoff = 5 if got["n"] else min(120, backoff * 2)
            stop.wait(max(0.0, min(backoff, left())))
    rc = state["rc"] if state["rc"] is not None and not killed else -1
    # a bounded cleanup allowance after the step (not counted against its deadline):
    # retire any follower left on the container side, and keep stderr, both best-effort
    with contextlib.suppress(Exception):
        raw_exec(sb, f"pkill -f 'tail -c .* -F {d}/out' ; true", timeout_s=20)
    with contextlib.suppress(Exception):
        err = raw_exec(sb, f"cat {d}/err 2>/dev/null", timeout_s=30)
        Path(str(log_path) + ".err").write_text(err.stdout if err.rc == 0 else "")
    secs = round(time.monotonic() - t0, 1)
    res = N.StreamResult(rc, secs, rc in (124, 137) and secs >= timeout_s - 30, killed)
    res.reconnects = sessions - 1  # type: ignore[attr-defined]
    res.output_incomplete = incomplete  # type: ignore[attr-defined]
    return res


# ------------------------------------------------------------------------------------------
# Re-recitation of finished boundaries (runs hit by the shared-CODEX_HOME bug)
# ------------------------------------------------------------------------------------------


RERECITE_FILES = ("pre_messages", "post_messages", "record", "compaction", "sim_replies")


def _write_atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    tmp.replace(path)


def codex_version() -> str:
    p = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=60,
                       check=False)
    return p.stdout.strip().split()[-1] if p.returncode == 0 and p.stdout.strip() else ""


def _copy_atomic(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + f".tmp-{uuid.uuid4().hex[:8]}")
    shutil.copy2(src, tmp)
    tmp.replace(dst)


def _rollback(b: Path) -> None:
    """Restore an interrupted publication of boundary `b` from its backup, in an order that is
    itself safe to interrupt: the completeness marker (boundary.json) goes first, every
    dependent file is restored atomically, boundary.json comes back last, and only then is
    the pending marker removed (so a rerun rolls back again until this has finished)."""
    orig = b / "native" / "orig"
    with contextlib.suppress(FileNotFoundError):
        (b / "boundary" / "boundary.json").unlink()  # incomplete while restoring
    for name in RERECITE_FILES:
        src = orig / f"{name}.json"
        if src.exists():
            _copy_atomic(src, b / "boundary" / f"{name}.json")
    if (orig / "recitations.json").exists():
        _copy_atomic(orig / "recitations.json", b / "native" / "recitations.json")
    _copy_atomic(orig / "boundary.json", b / "boundary" / "boundary.json")
    with contextlib.suppress(FileNotFoundError):
        (b / "boundary" / "boundary.json.rerecite-pending").unlink()


def _backup(b: Path) -> None:
    """The originals, once: built in a temp dir, then renamed into place (all or nothing)."""
    orig = b / "native" / "orig"
    if orig.exists():
        return
    tmp = b / "native" / f"orig.tmp-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    for name in RERECITE_FILES + ("boundary",):
        src = b / "boundary" / f"{name}.json"
        if src.exists():
            shutil.copy2(src, tmp / src.name)
    shutil.copy2(b / "native" / "recitations.json", tmp / "recitations.json")
    tmp.rename(orig)


def rerecite_run(run_dir: Path, *, repeats: int = 3, auth_path: Path | None = None,
                 log: Callable[[str], None] = print) -> list[dict]:
    """Top every boundary of a finished run up to `repeats` valid recitations from its saved
    rollout (b<k>/native/rollout.jsonl: the session up to that `compacted` line; the saved
    encrypted compaction state is resumed, the compactor is not rerun), re-pick the
    majority, and re-derive the boundary files with the new summaries (later boundaries'
    histories carry the earlier ones). What changes is the recovered-text proxy that is
    graded, so grading must be rerun after.

    Two phases: everything is recited and derived in memory first; then each boundary is
    published in order: originals backed up once (temp dir, then rename), boundary.json moved
    aside (the boundary reads as incomplete), the files written atomically, boundary.json
    last. An interrupted publication is rolled back from the backup on the next call, and
    <run>/rerecite.json says whether the last pass completed. Refuses unless the `codex` on
    PATH is CLI_VERSION and matches the boundaries' cli_version. Returns one row per
    boundary (`complete`: reached `repeats` valid)."""
    from compaction.s2 import compact as K
    from compaction.s2 import interact_run as IR
    rd = Path(run_dir)
    ver = codex_version()
    if ver != CLI_VERSION:
        raise RuntimeError(f"codex on PATH is {ver!r}, not {CLI_VERSION}")
    found = []
    for b in rd.iterdir():
        m = re.fullmatch(r"b(\d+)", b.name)
        if not m:
            continue
        pending = (b / "boundary" / "boundary.json.rerecite-pending").exists()
        meta = (b / "native" / "orig" / "boundary.json") if pending \
            else (b / "boundary" / "boundary.json")
        if not meta.exists():
            continue  # never completed: not a graded boundary
        cv = json.loads(meta.read_text()).get("cli_version")
        if cv != CLI_VERSION:  # preflight, before any write (rollbacks included)
            raise RuntimeError(f"{b}: recorded cli_version {cv!r} != {CLI_VERSION}")
        found.append((int(m.group(1)), b, pending))
    bdirs = []
    for k, b, pending in sorted(found):
        if pending:
            _rollback(b)  # an interrupted publication: back to the originals
        bdirs.append((k, b))
    _write_atomic(rd / "rerecite.json", {"status": "in_progress", "utc": utc()})
    reciter = Reciter(rd, auth_path or Path.home() / ".codex" / "auth.json")
    staged, rows, summaries = [], [], {}
    try:
        for k, b in bdirs:  # phase 1: recite and derive, no writes to the boundary
            bj = json.loads((b / "boundary" / "boundary.json").read_text())
            lines = read_rollout(b / "native" / "rollout.jsonl")
            want = int(bj.get("boundary_in_session") or len(compaction_lines(lines)))
            path = bj.get("rollout_path") or ""
            have = [Recitation(**{f: r.get(f) for f in ("k", "thread_id", "text", "raw",
                                                         "tool_calls", "seconds", "rc",
                                                         "plaintext_bound")})
                    for r in json.loads((b / "native" / "recitations.json").read_text())]
            before = sum(r.valid for r in have)
            recs = reciter.run_until(lines, want, repeats, label=f"_rerecite_k{k}",
                                     have=have, retries=3)
            good = [r for r in recs if r.valid]
            row = {"boundary": str(b), "valid_before": before, "valid_after": len(good),
                   "complete": len(good) >= repeats}
            if not good:
                row["skipped"] = "no valid recitation"
                rows.append(row)
                log(json.dumps(row))
                summaries.setdefault(path, {})  # later boundaries keep their history
                continue
            texts = [r.text or "" for r in good]
            used, means = majority(texts)
            chosen = good[used]
            summaries.setdefault(path, {})[want] = texts[used]
            nb = rollout_boundary(lines, want, summaries[path], model=bj.get("model") or MODEL)
            rec = json.loads((b / "boundary" / "record.json").read_text())
            new = K.boundary_record(rec["session_id"], nb.pre_messages, model=nb.model,
                                    pre_tokens=int(nb.pre_tokens or 0), summary=nb.summary)
            record = dict(rec)  # identity and every other field preserved
            record.update(source_messages=new["source_messages"], summary=new["summary"],
                          user_turns=new["user_turns"], pre_tokens=new["pre_tokens"])
            assert record["id"] == rec["id"]
            comp = json.loads((b / "boundary" / "compaction.json").read_text())
            comp.update(summary=nb.summary, post_messages=nb.post_messages,
                        summary_index=nb.summary_index, kept_indices=nb.kept_indices)
            replies = json.loads((rd / "sim" / "replies.json").read_text()) \
                if (rd / "sim" / "replies.json").exists() else []
            at = compaction_lines(lines)[want - 1]
            agree = [round(item_overlap(texts[i], texts[j]), 3)
                     for i in range(len(texts)) for j in range(i + 1, len(texts))]
            keys = ("summary_chars", "summary_index", "kept_indices", "post_tokens",
                    "post_tokens_est", "recite_tries", "recite_valid", "recite_used",
                    "recite_mean_agreement", "recite_agreement", "recite_invented_tokens",
                    "recite_plaintext_bound", "recite_tool_calls")
            first = b / "native" / "orig" / "boundary.json"  # the capture's own values
            src_bj = json.loads(first.read_text()) if first.exists() else bj
            original = (bj.get("rerecited") or {}).get("original") \
                or {x: src_bj.get(x) for x in keys}
            bj.update(summary_chars=len(nb.summary), summary_index=nb.summary_index,
                      kept_indices=nb.kept_indices, post_tokens=nb.post_tokens,
                      post_tokens_est=nb.post_tokens, recite_tries=len(recs),
                      recite_valid=len(good), recite_used=recs.index(chosen),
                      recite_mean_agreement=means, recite_agreement=agree,
                      recite_invented_tokens=invented_tokens(
                          texts[used], history_text(lines, at - 1))[:50],
                      recite_plaintext_bound=chosen.plaintext_bound,
                      recite_tool_calls=[r.tool_calls for r in recs],
                      rerecited={"utc": utc(), "valid_before": before,
                                 "valid_after": len(good), "cli": f"codex-cli {ver}",
                                 "why": "parallel recitations shared one CODEX_HOME",
                                 "original": original})
            files = {"pre_messages": nb.pre_messages, "post_messages": nb.post_messages,
                     "record": record, "compaction": comp,
                     "sim_replies": IR.reply_index(replies, nb.pre_messages)}
            staged.append((b, files, bj, [r.to_dict() for r in recs]))
            row["agreement"] = agree
            row["summary_changed"] = nb.summary != rec.get("summary")
            rows.append(row)
            log(json.dumps(row))
        for b, files, bj, recs_out in staged:  # phase 2: publish, boundary by boundary
            _backup(b)
            bp = b / "boundary" / "boundary.json"
            bp.replace(b / "boundary" / "boundary.json.rerecite-pending")  # now incomplete
            for name, obj in files.items():
                _write_atomic(b / "boundary" / f"{name}.json", obj)
            _write_atomic(b / "native" / "recitations.json", recs_out)
            _write_atomic(bp, bj)  # complete again
            (b / "boundary" / "boundary.json.rerecite-pending").unlink()
    finally:
        reciter.close()
    _write_atomic(rd / "rerecite.json", {
        "status": "complete" if all(r["complete"] for r in rows) else "incomplete",
        "utc": utc(), "repeats": repeats, "cli": f"codex-cli {ver}", "boundaries": rows})
    return rows
