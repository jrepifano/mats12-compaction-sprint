"""The Claude Code arm: SWE-Interact sessions in Claude Code itself, with Haiku 5.5 on Jacob's
Max plan, compacting with Claude Code's own auto-compaction (brief logs/s2/briefs/native-arms.md;
feasibility logs/s2/native_harness_feasibility.md, "Claude Code").

How a base run goes (`run_base`), mirroring the pi arm's `interact_run.run_base`:
- One `NativeSession` (common.py): a task container on an internal network, the egress sidecar
  (api.anthropic.com only), and the simulator as the released `ask_user` MCP server.
- The CLI (2.1.310, a single binary) sits in a read-only Docker volume mounted at /opt/emx/claude
  (`ensure_cli_volume`), so the task image is untouched and kept images don't carry it. Its
  config dir (CLAUDE_CONFIG_DIR=/opt/emx/cc) is in the container's filesystem, so each kept
  image holds the session transcript as it was at that boundary. The MCP server is registered
  in $CLAUDE_CONFIG_DIR/.claude.json, as Harbor does (`claude_code.py:1920`).
- Steps: 01_plan, then 02_implement as a fresh session with the same simulator (the pi arm's
  rule), each one `claude -p` reading the step's instruction.md verbatim on stdin, with
  stream-json output (Harbor's command line: `--verbose --output-format=stream-json --print`,
  `--permission-mode bypassPermissions`, IS_SANDBOX=1, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1),
  plus `--disallowedTools WebSearch,WebFetch` (server-side web search would bypass the egress
  rule) and `--effort` (the paper's runs used reasoning_effort=high).
- Auth: CLAUDE_CODE_OAUTH_TOKEN from .env, passed only as `docker exec -e` (value in the client
  environment); ANTHROPIC_API_KEY is unset inside; the run refuses to go on unless the stream's
  init event says `apiKeySource: none` (Max billing). Before every kept commit the container is
  scanned for the token (`common.secret_scan`) and the commit refused if it is anywhere.
- Compaction: native auto-compaction, its window set with CLAUDE_CODE_AUTO_COMPACT_WINDOW (the
  trigger sits some buffer below it; each boundary records the window and its measured
  pre_tokens). A PostCompact hook (/opt/emx/compact-hook.sh) POSTs its payload to the
  SimServer's /emx/boundary and blocks until the boundary is captured: process list, secret
  scan, workspace fingerprint (v3), a kept `docker commit --pause` (KeepStore rules), the
  transcript, and the boundary files in the pi layout (`transcript_boundary` +
  `common.write_boundary`). The CLI resumes when the hook returns.
- A boundary may stop the run (the fork trigger, `eligible`, as the pi arm's stop_at_boundary,
  or `stop_after` boundaries for a smoke): the runner then stops the CLI inside the container.

Transcript -> messages (`transcript_boundary`): the session JSONL's main chain (user and
assistant rows; attachments, hooks and bookkeeping rows dropped) in OpenAI format. Rows of one
API response (same message.id) merge into one assistant message: text blocks joined, tool_use
blocks as tool_calls (mcp__user-server__ask_user renamed ask_user; thinking dropped and
counted). tool_result blocks become tool messages; the ask_user result's FastMCP wrapper
({"result": ...}) is unwrapped to the reply text so `reply_index` matches it. The context
before boundary j is: for j = 1, the chain from the boundary's logical parent back to the root;
after that, boundary j-1's summary message, its preserved segment (Claude Code keeps the last
few messages verbatim: compactMetadata.preservedMessages), then the chain after boundary j-1.
post_messages: the system prompt, the summary message, the preserved segment (kept_indices
point at it in pre_messages). The system message is Claude Code's own system prompt, from the
latest api-request-shape row before the boundary (a placeholder when there is none).

Forks (`fork`): start the kept image, restore the simulator from sim_state.json, write the
boundary's transcript under a new session id with the summary message replaced by the edited
text, and `claude -p <prompt> --resume <id> --fork-session`. The CLI needs a new user prompt
to resume headless; real arms use CONTINUE_PROMPT in every arm alike.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compaction.deepswe import sandbox as SB
from compaction.s2 import config as C
from compaction.s2 import interact as I
from compaction.s2 import session as SS
from compaction.s2.native import common as N

ARM = "claude_code"
CLI_VERSION = "2.1.310"
CLI_VOLUME = f"emx-native-claude-{CLI_VERSION}"
CLI_DIR = "/opt/emx/claude"
CLI = f"{CLI_DIR}/claude"
CONFIG_DIR = "/opt/emx/cc"
SETTINGS = "/opt/emx/cc-settings.json"
HOOK = "/opt/emx/compact-hook.sh"
HOOK_OUT = "/opt/emx/hook-last.json"
PROJECT = f"{CONFIG_DIR}/projects/-app"
MODEL = "claude-haiku-5-5"
EFFORT = "high"
DISALLOWED = "WebSearch,WebFetch,Task,Agent"
POLICY = "claude-code-native"
ASK_TOOL = "mcp__user-server__ask_user"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
STEP_TIMEOUT_S = {"01_plan": 2400, "02_implement": 5400}  # task.toml [steps.agent] timeouts
HOOK_TIMEOUT_S = 1800
CONTINUE_PROMPT = "Continue."
SYSTEM_PLACEHOLDER = "(Claude Code system prompt: not recorded in the transcript)"
TOOL_NAMES = {ASK_TOOL: "ask_user"}


def root() -> Path:
    return C.raw_dir() / "native" / ARM


# ------------------------------------------------------------------------------------------
# The CLI volume and the in-container config
# ------------------------------------------------------------------------------------------


def ensure_cli_volume(host: str | None, image: str) -> str:
    """The CLI binary in a Docker volume on `host` (created once, from a throwaway container
    with network: Claude Code's own installer, pinned to CLI_VERSION). Returns the volume."""
    with SB.using_host(host):
        p = SB._docker(["volume", "inspect", CLI_VOLUME], timeout=60, check=False)
        if p.returncode == 0:
            return CLI_VOLUME
        SB._docker(["volume", "create", "--label", f"{N.NATIVE_LABEL}=claude-cli",
                    CLI_VOLUME], timeout=60)
        name = f"{N.purpose(ARM)}-install-{uuid.uuid4().hex[:8]}"
        cmd = (f"curl -fsSL https://claude.ai/install.sh | bash -s {CLI_VERSION} >/dev/null"
               f" && cp -L ~/.local/share/claude/versions/{CLI_VERSION} /out/claude"
               " && chmod 755 /out/claude && /out/claude --version")
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


def hook_script() -> str:
    """PostCompact: hand the payload to the runner and wait until the boundary is kept. The
    SimServer token goes to curl through a header file on a tmpfs, never in an argv (ps)."""
    return ("#!/bin/sh\n"
            f"umask 077; printf '{N.TOKEN_HEADER}: %s\\n' \"$EMX_SIM_TOKEN\" > {HEADER_DIR}/h\n"
            f"curl -sS -m {HOOK_TIMEOUT_S - 30} -X POST -H @{HEADER_DIR}/h"
            f" -H 'content-type: application/json' --data-binary @- {N.BOUNDARY_URL}"
            f" > {HOOK_OUT} 2>&1\nexit 0\n")


def settings() -> dict:
    return {"hooks": {"PostCompact": [{"hooks": [
        {"type": "command", "command": HOOK, "timeout": HOOK_TIMEOUT_S}]}]}}


def install_config(sb: SB.Sandbox, server: N.SimServer) -> None:
    """Config dir, MCP registration (.claude.json), settings with the hook, the hook script."""
    r = sb.exec(f"mkdir -p {PROJECT} {CONFIG_DIR}/debug {CONFIG_DIR}/todos", timeout_s=60,
                workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"config dir: {r.stderr[-300:]}")
    # the header names an env var (expanded by the CLI, as Harbor does), so no token is ever
    # in a file the kept image could hold
    mcp = server.mcp_config()
    mcp["mcpServers"]["user-server"]["headers"] = {N.TOKEN_HEADER: "${EMX_SIM_TOKEN}"}
    sb.put_file(json.dumps({**mcp, "hasCompletedOnboarding": True}).encode(),
                f"{CONFIG_DIR}/.claude.json")
    sb.put_file(json.dumps(settings(), indent=1).encode(), SETTINGS)
    sb.put_file(hook_script().encode(), HOOK)
    r = sb.exec(f"chmod 755 {HOOK}", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"chmod hook: {r.stderr[-300:]}")


@dataclass(frozen=True)
class CCConfig:
    """The CLI settings of one run (recorded in run.json and every boundary)."""
    model: str = MODEL
    effort: str | None = EFFORT
    auto_compact_window: int | None = None  # None: the CLI default (Haiku 5.5: 1M context)

    def threshold(self) -> dict:
        return {"kind": "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "window": self.auto_compact_window,
                "note": "trigger = window minus Claude Code's buffer; see each boundary's "
                        "pre_tokens"}


def cli_env(cfg: CCConfig, egress: N.Egress, server: N.SimServer, token: str) -> dict[str, str]:
    env = {TOKEN_ENV: token, "CLAUDE_CONFIG_DIR": CONFIG_DIR, "IS_SANDBOX": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
           "MCP_TOOL_TIMEOUT": str(3600 * 1000), "MCP_TIMEOUT": "60000",
           "EMX_SIM_TOKEN": server.token, **egress.proxy_env()}
    if cfg.auto_compact_window:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(int(cfg.auto_compact_window))
    return env


def cli_command(cfg: CCConfig, *, session_id: str | None = None, resume: str | None = None,
                fork: bool = False, prompt: str | None = None, max_calls: int | None = None) -> str:
    """The in-container command line (no secrets in it). The prompt comes on stdin unless
    `prompt` is given (forks)."""
    parts = [CLI, "-p", "--verbose", "--output-format", "stream-json", "--model", cfg.model,
             "--permission-mode", "bypassPermissions", "--disallowedTools", DISALLOWED,
             "--settings", SETTINGS]
    if cfg.effort:
        parts += ["--effort", cfg.effort]
    if session_id:
        parts += ["--session-id", session_id]
    if resume:
        parts += ["--resume", resume]
    if fork:
        parts.append("--fork-session")
    if max_calls is not None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        parts += ["--max-turns", str(max_calls)]
    if prompt is not None:
        parts.append(prompt)
    return "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN; cd /app && " + shlex.join(parts)


_STOP_LOCK = threading.Lock()


def stop_cli(sb: SB.Sandbox) -> None:
    """Stop the CLI inside the container (killing the docker client doesn't). Once per
    container: a boundary thread and the step loop may both ask (smoke 1, 24 Oct: two at once
    hung)."""
    with _STOP_LOCK:
        if getattr(sb, "_emx_cli_stopped", False):
            return
        sb._emx_cli_stopped = True
        sb.exec(f"pkill -TERM -f '^{CLI}' ; sleep 3; pkill -KILL -f '^{CLI}' ; true",
                timeout_s=60, workdir="/")



# ------------------------------------------------------------------------------------------
# Transcript -> the pi layout
# ------------------------------------------------------------------------------------------


def read_jsonl(text: str, *, strict: bool = False) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if line.strip():
            if strict:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("native transcript rows must be objects")
                rows.append(value)
            else:
                with contextlib.suppress(ValueError):
                    rows.append(json.loads(line))
    return rows


def _is_msg(r: dict) -> bool:
    return (r.get("type") in ("user", "assistant") and not r.get("isSidechain")
            and isinstance(r.get("message"), dict))


def compact_rows(rows: list[dict]) -> list[int]:
    """File indices of the compact_boundary rows."""
    return [i for i, r in enumerate(rows)
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary"]


def summary_row(rows: list[dict], b: int) -> int | None:
    """The summary message of the boundary at file index b: the first isCompactSummary row
    after it, before the next boundary. (A manual /compact parents it on the boundary row; an
    auto-compaction puts attachment rows between them, seen live 24 Oct.)"""
    for i in range(b + 1, len(rows)):
        r = rows[i]
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            return None
        if r.get("isCompactSummary"):
            return i
    return None


def _chain(rows: list[dict], leaf: str | None, stop_at: int = -1) -> list[int]:
    """File indices on the parentUuid chain from `leaf` to the root, oldest first, stopping
    before any row at or above file index `stop_at`... i.e. keeping only rows after it."""
    by = {r["uuid"]: i for i, r in enumerate(rows) if r.get("uuid")}
    out, seen = [], set()
    cur = leaf
    while cur and cur in by and cur not in seen:
        seen.add(cur)
        i = by[cur]
        if i <= stop_at:
            break
        out.append(i)
        cur = rows[i].get("parentUuid")
    return out[::-1]


def _block_text(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for x in c:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text":
                parts.append(x.get("text") or "")
            elif x.get("type") == "tool_reference":
                parts.append(f"[tool loaded: {x.get('tool_name')}]")
            elif x.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def unwrap_ask(content: str) -> str:
    """FastMCP's structured wrapper ({"result": "<reply>"}) -> the reply text."""
    with contextlib.suppress(ValueError, TypeError):
        d = json.loads(content)
        if isinstance(d, dict) and set(d) == {"result"} and isinstance(d["result"], str):
            return d["result"]
    return content


def _rendered(r: dict) -> str | None:
    """The model-visible text of an attachment row (its `rendered` parts), else None. Claude
    Code renders environment, git status, date, tool and skill listings, token reminders and
    re-attached files as <system-reminder> text in the request (astra r1 #3)."""
    if r.get("type") != "attachment" or r.get("isSidechain"):
        return None
    parts = [x.get("content") for x in r.get("rendered") or []
             if isinstance(x, dict) and isinstance(x.get("content"), str)]
    return "\n".join(parts) if parts else None


def _blocks_to_messages(role: str, content: Any, names: dict[str, str], uid: str,
                        msgs: list[dict], srcs: list[list[str]], info: dict) -> None:
    """One non-assistant message's content into OpenAI messages: tool results as tool messages,
    each text block as its own message (so a summary block can be indexed on its own).
    Mid-conversation system messages become user messages: they are injected context the
    graders must see (lossaudit drops every system-role message)."""
    out_role = "user"
    if role == "system":
        info["system_as_user"] += 1
    if isinstance(content, str):
        msgs.append({"role": out_role, "content": content})
        srcs.append([uid])
        return
    for blk in content or []:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "tool_result":
            tid = blk.get("tool_use_id")
            text = _block_text(blk.get("content"))
            if names.get(tid) == "ask_user":
                text = unwrap_ask(text)
            msgs.append({"role": "tool", "tool_call_id": tid, "content": text})
            srcs.append([uid])
        elif blk.get("type") == "text":
            msgs.append({"role": out_role, "content": blk.get("text") or ""})
            srcs.append([uid])


def _assistant_blocks(cur: dict, blocks: Any, names: dict[str, str], info: dict) -> None:
    for blk in blocks or []:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text" and blk.get("text"):
            cur["content"] = (cur["content"] + "\n\n" if cur["content"] else "") + blk["text"]
        elif t == "tool_use":
            name = TOOL_NAMES.get(blk.get("name"), blk.get("name"))
            names[blk.get("id")] = name
            cur.setdefault("tool_calls", []).append(
                {"id": blk.get("id"), "type": "function",
                 "function": {"name": name, "arguments": json.dumps(
                     blk.get("input") or {}, ensure_ascii=False)}})
        elif t in ("thinking", "redacted_thinking"):
            info["thinking_blocks"] += 1


def _new_info() -> dict:
    return {"thinking_blocks": 0, "meta_rows": 0, "attachments": 0, "system_as_user": 0}


def to_messages(rows: list[dict], idx: list[int]) -> tuple[list[dict], list[list[str]], dict]:
    """OpenAI-format messages for the transcript rows `idx` (module docstring), each with the
    uuids it came from, and counts of what was dropped or rerouted. Rendered attachments
    become user messages in chain order."""
    msgs: list[dict] = []
    srcs: list[list[str]] = []
    info = _new_info()
    names: dict[str, str] = {}
    last_mid = None
    for i in idx:
        r = rows[i]
        uid = r.get("uuid") or ""
        att = _rendered(r)
        if att is not None:
            msgs.append({"role": "user", "content": att})
            srcs.append([uid])
            info["attachments"] += 1
            last_mid = None
            continue
        if not _is_msg(r):
            continue
        m = r["message"]
        if r.get("isMeta"):
            info["meta_rows"] += 1
            continue
        if r["type"] == "assistant":
            mid = m.get("id")
            if not (msgs and msgs[-1]["role"] == "assistant" and mid and mid == last_mid):
                msgs.append({"role": "assistant", "content": ""})
                srcs.append([])
            srcs[-1].append(uid)
            last_mid = mid
            _assistant_blocks(msgs[-1], m.get("content"), names, info)
            continue
        last_mid = None
        _blocks_to_messages("user", m.get("content"), names, uid, msgs, srcs, info)
    return msgs, srcs, info


def request_messages(rows: list[dict], i: int) -> list[dict] | None:
    """The messages of the api-request row at file index i, rebuilt from the transcript's
    delta encoding (each request = its `base` request's first `keep` messages + the `tail`
    blobs, hashed as sorted compact JSON); None when any part is unrecorded."""
    blobs = {r["hash"]: r["message"] for r in rows[: i + 1]
             if r.get("type") == "api-request-blob" and "hash" in r}
    # The CLI deduplicates blobs against ordinary transcript messages too. Reconstruct
    # candidates only when their exact canonical SHA256 equals the requested blob hash.
    assistant_id, assistant_blocks = None, []
    for row in rows[:i + 1]:
        msg = row.get("message")
        if row.get("type") not in ("user", "assistant") or not isinstance(msg, dict):
            continue
        candidates = [msg, {"role": msg.get("role", row["type"]), "content": msg.get("content")}]
        if row["type"] == "assistant" and isinstance(msg.get("content"), list):
            mid = msg.get("id")
            assistant_blocks = (assistant_blocks if mid and mid == assistant_id else []) + msg["content"]
            assistant_id = mid
            candidates.append({"role": "assistant", "content": assistant_blocks})
        else:
            assistant_id, assistant_blocks = None, []
        for candidate in candidates:
            digest = I.sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            blobs.setdefault(digest, candidate)
    built: dict[str, list[dict] | None] = {}
    for r in rows[: i + 1]:
        if r.get("type") != "api-request":
            continue
        base = r.get("base")
        head = [] if not base else built.get(base)
        tail = [blobs.get(h) for h in r.get("tail") or []]
        ok = head is not None and all(t is not None for t in tail)
        built[r["id"]] = (head[: int(r.get("keep") or 0)] + tail) if ok else None
    return built.get(rows[i].get("id")) if rows[i].get("type") == "api-request" else None


def request_to_messages(msgs: list[dict]) -> tuple[list[dict], dict]:
    out: list[dict] = []
    srcs: list[list[str]] = []
    info = _new_info()
    names: dict[str, str] = {}
    for m in msgs:
        if m.get("role") == "assistant":
            cur = {"role": "assistant", "content": ""}
            _assistant_blocks(cur, m.get("content"), names, info)
            out.append(cur)
            srcs.append([])
        else:
            _blocks_to_messages(m.get("role") or "user", m.get("content"), names, "", out, srcs,
                                info)
    return out, info


def first_request_after(rows: list[dict], s: int) -> int | None:
    """The first api-request row after file index s (the request the CLI resumed with)."""
    return next((i for i in range(s + 1, len(rows)) if rows[i].get("type") == "api-request"),
                None)


def system_prompt(rows: list[dict], before: int) -> str:
    for i in range(min(before, len(rows)) - 1, -1, -1):
        r = rows[i]
        if r.get("type") == "api-request-shape":
            sysb = (r.get("shape") or {}).get("system")
            if isinstance(sysb, list):
                return "\n".join(x.get("text", "") for x in sysb if isinstance(x, dict))
            if isinstance(sysb, str):
                return sysb
    return SYSTEM_PLACEHOLDER


def context_before(rows: list[dict], j: int) -> list[int]:
    """File indices of the context in effect just before the j-th boundary (1-based) of this
    transcript: module docstring."""
    bs = compact_rows(rows)
    b = bs[j - 1]
    leaf = rows[b].get("logicalParentUuid")
    if j == 1:
        return _chain(rows, leaf)
    prev = bs[j - 2]
    s = summary_row(rows, prev)
    by = {r["uuid"]: i for i, r in enumerate(rows) if r.get("uuid")}
    kept = [by[u] for u in _preserved(rows[prev]) if u in by]
    head = ([s] if s is not None else []) + kept
    return head + [i for i in _chain(rows, leaf, stop_at=prev) if i not in head]


def _preserved(row: dict) -> list[str]:
    md = row.get("compactMetadata") or {}
    return list((md.get("preservedMessages") or {}).get("uuids") or [])


@dataclass
class Converted:
    nb: N.NativeBoundary
    summary_uuid: str | None
    boundary_uuid: str | None


def transcript_boundary(rows: list[dict], j: int, *, model: str, step_instruction: str,
                        extra_info: dict | None = None) -> Converted:
    """The j-th boundary (1-based, file order) of a Claude Code session as a NativeBoundary.
    post_messages are the exact first request after the summary when the transcript holds it
    (`post_source` "request"); otherwise the summary, the preserved segment and the rendered
    attachments after the summary ("chain")."""
    bs = compact_rows(rows)
    if len(bs) < j:
        raise ValueError(f"transcript has {len(bs)} compact boundaries, wanted {j}")
    b = bs[j - 1]
    md = rows[b].get("compactMetadata") or {}
    s = summary_row(rows, b)
    if s is None:
        raise ValueError(f"boundary {j} has no summary row yet")
    sysmsg = {"role": "system", "content": system_prompt(rows, b)}
    pre_msgs, pre_srcs, info = to_messages(rows, context_before(rows, j))
    pre = [sysmsg, *pre_msgs]
    keep = set(_preserved(rows[b]))
    kept = [i + 1 for i, src in enumerate(pre_srcs) if keep & set(src)]
    summary = _block_text(rows[s]["message"].get("content"))
    nxt = bs[j] if len(bs) > j else len(rows)
    ri = first_request_after(rows, s)
    req = request_messages(rows, ri) if ri is not None and ri < nxt else None
    post_source, post_info = "chain", {}
    post = None
    if req is not None:
        conv, post_info = request_to_messages(req)
        hit = [i for i, m in enumerate(conv) if m["role"] == "user" and m["content"] == summary]
        if hit:
            post = [{"role": "system", "content": system_prompt(rows, ri + 1)}, *conv]
            si = hit[0] + 1
            post_source = "request"
    if post is None:
        by = {r["uuid"]: i for i, r in enumerate(rows) if r.get("uuid")}
        after = [i for i in range(s + 1, ri if ri is not None else nxt) if _rendered(rows[i])]
        tail, _, post_info = to_messages(rows, [by[u] for u in _preserved(rows[b]) if u in by]
                                         + after)
        post, si = [sysmsg, {"role": "user", "content": summary}, *tail], 1
    return Converted(N.NativeBoundary(
        pre_messages=pre, post_messages=post, summary=summary, summary_index=si,
        kept_indices=kept, pre_tokens=md.get("preTokens"), post_tokens=md.get("postTokens"),
        trigger=md.get("trigger") or "auto", policy=POLICY, model=model,
        info={**info, "boundary_in_session": j, "cumulative_dropped_tokens":
              md.get("cumulativeDroppedTokens"), "preserved_uuids": sorted(keep),
              "post_source": post_source, "post_request_index": ri if req else None,
              "post_info": post_info, **(extra_info or {})}),
        rows[s].get("uuid"), rows[b].get("uuid"))


def api_calls(rows: list[dict]) -> int:
    """Distinct API responses (assistant message ids) in the transcript."""
    return len({r["message"].get("id") for r in rows
                if _is_msg(r) and r["type"] == "assistant" and r["message"].get("id")})


# ------------------------------------------------------------------------------------------
# Base runs
# ------------------------------------------------------------------------------------------


def run_key(task_id: str, attempt: int, prefix: str = "") -> str:
    return f"{prefix}{task_id}-cc-haiku-a{attempt}"


def next_attempt(root_dir: Path, task_id: str, prefix: str = "") -> int:
    import re
    pat = re.compile(rf"{re.escape(prefix + task_id)}-cc-haiku-a(\d+)")
    ns = [int(m.group(1)) for p in Path(root_dir).glob("*") if (m := pat.fullmatch(p.name))]
    return max(ns, default=0) + 1


@dataclass
class BaseConfig:
    task: Any  # interact_run.InteractTask
    root: Path
    attempt: int = 1
    prefix: str = ""
    cc: CCConfig = field(default_factory=CCConfig)
    host: str | None = "ssh://rig"
    sim_cap_usd: float = C.INTERACT_BASE_CAP_USD
    steps: tuple[str, ...] = ("01_plan", "02_implement")
    step_timeout_s: dict = field(default_factory=lambda: dict(STEP_TIMEOUT_S))
    stop_after: int | None = None  # stop after this many boundaries (smokes)
    cpus: float = N.AGENT_CPUS
    memory_mb: int = N.AGENT_MEMORY_MB
    min_free_gb: float = N.AGENT_MIN_FREE_GB
    planting: Any = None
    stop_first_eligible: bool = False
    capture_ask: bool = False  # Step 0 continuation smoke: no simulator calls
    max_calls: int = 30

    @property
    def run_key(self) -> str:
        return run_key(self.task.task_id, self.attempt, self.prefix)

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / self.run_key


class AuthError(RuntimeError):
    pass


def _stream_watch(state: dict, stop: threading.Event) -> Callable[[str], None]:
    """Per stream-json line: refuse anything but Max billing (init's apiKeySource must be
    "none"), and note the result and rate-limit events."""
    def on_line(line: str) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            state["init"] = {k: ev.get(k) for k in ("apiKeySource", "model", "mcp_servers",
                                                    "claude_code_version", "session_id")}
            if ev.get("apiKeySource") != "none":
                state["auth_error"] = f"apiKeySource={ev.get('apiKeySource')!r}, not Max"
                stop.set()
        elif ev.get("type") == "result":
            state["result"] = {k: ev.get(k) for k in ("subtype", "is_error", "num_turns",
                                                      "total_cost_usd", "duration_ms",
                                                      "session_id", "modelUsage", "usage")}
            state["result"]["text"] = str(ev.get("result") or "")[:500]
        elif ev.get("type") == "rate_limit_event":
            state["rate_limit"] = {**ev, "observed_at": time.time()}
        elif ev.get("type") == "system" and ev.get("subtype") == "compact_boundary":
            state.setdefault("compactions", []).append(ev.get("compact_metadata"))
    return on_line


HEADER_DIR = "/run/emx-x"  # tmpfs: the hook's header file (never in a commit)
PLAN_DIR = "plan_boundaries"  # boundaries of steps other than 02_implement (own chain)
GRADED_STEP = "02_implement"
TRANSCRIPT_WAIT_S = 300
TRANSCRIPT_POLL_S = 30  # one docker exec per poll: at most every 30 s (main session, 24 Oct)
POST_REQUEST_WAIT_S = 120


def _step_ok(r: N.StreamResult, state: dict) -> str | None:
    """Why a finished step counts as failed (None: it didn't). A step the runner stopped
    (r.killed == "stop") is judged by its stop reason, not here."""
    init = state.get("init")
    if r.killed == "container_gone":
        return "infra_killed: the container disappeared"
    if r.killed == "output_lost":
        return "infra_killed: the step's output could not be recovered"
    if init is None:
        return "no init event"
    if init.get("apiKeySource") != "none":
        return f"apiKeySource={init.get('apiKeySource')!r}, not Max"
    if r.killed == "stop":
        return None
    res = state.get("result")
    if r.rc == 137 and not r.timed_out_by_cli:
        # SIGKILL from outside the CLI: the container's memory limit (node test suites at
        # 3 GB, 24 Oct), never a task result; rerun once as a heavy container
        return "infra_killed: exit 137"
    if r.rc != 0:
        return f"CLI exit {r.rc}"
    if res is None:
        return "no result event"
    if res.get("is_error") or res.get("subtype") != "success":
        return f"result {res.get('subtype')!r} is_error={res.get('is_error')}"
    return None


def run_base(cfg: BaseConfig, *, sim_model, token: str,
             eligible: Callable[[Path, dict], bool] | None = None,
             session_cls=N.NativeSession, resume_boundary: Path | None = None) -> dict:
    """One task in Claude Code (module docstring). Writes the run dir like the pi arm's
    run_base; returns result.json's content. Ends: "submitted" (every step finished cleanly),
    "eligible", "stop_after", "run_cap", "time_cap", "cli_error", "auth_error",
    "boundary_error" (a capture failed: the CLI is stopped), "boundary_missing" (the CLI
    compacted more often than boundaries were captured), "infra_error", "error"."""
    from compaction.s2 import interact_run as IR
    t0 = time.monotonic()
    task = cfg.task
    resume_b = resume_export = None
    resume_rows = []
    if resume_boundary is not None:
        resume_boundary = Path(resume_boundary)
        resume_b = json.loads((resume_boundary / "boundary/boundary.json").read_text())
        resume_export = json.loads((resume_boundary / "boundary/sim_state.json").read_text())
        resume_rows = read_jsonl((resume_boundary / "native/transcript.jsonl").read_text(), strict=True)
        si = next(i for i, row in enumerate(resume_rows)
                  if row.get("uuid") == resume_b["native_summary_uuid"])
        resume_rows = resume_rows[:si + 1]
        if cfg.steps != (resume_b.get("stage") or GRADED_STEP,):
            raise ValueError("continuation must resume exactly the kept workflow step")
    rd = IR._fresh_dir(cfg.run_dir)
    rj = N.native_run_json(run_key=cfg.run_key, task_dict=task.to_dict(),
                           persona_sha256=I.sha256(task.persona), arm=ARM, model=cfg.cc.model,
                           policy=POLICY, threshold=cfg.cc.threshold(), cli_version=CLI_VERSION,
                           extra={"effort": cfg.cc.effort, "disallowed_tools": DISALLOWED,
                                  "host": cfg.host, "egress_allow": list(N.ALLOW[ARM]),
                                  "steps": list(cfg.steps), "step_timeout_s": cfg.step_timeout_s,
                                  "stop_after": cfg.stop_after, "cpus": cfg.cpus,
                                  "memory_mb": cfg.memory_mb})
    if resume_b:
        rj.update(continued_from=str(resume_boundary),
                  previous_boundary_index=resume_b["boundary_index"])
    IR._dump(rd / "run.json", rj)
    chains = {GRADED_STEP: (rd, SS.KeepStore(rd, cfg.run_key, host=cfg.host))}
    plan_root = rd / PLAN_DIR
    res: dict = {"run_key": cfg.run_key, "end": "error", "boundaries": [],
                 "plan_boundaries": [], "error": None, "steps": {}, "timings": [],
                 "sim_cost_usd": 0.0, "plan_md": None}
    stop = threading.Event()
    end_reason: dict = {}
    cur: dict = {"step": None, "session_id": None, "quit": threading.Event()}
    seen: dict = {}
    if resume_b:
        seen[("_k", str(rd))] = int(resume_b["boundary_index"])
    pending: list[threading.Thread] = []

    def mark(ev: str, **kw) -> None:
        res["timings"].append({"event": ev, "utc": IR.utc(),
                               "s": round(time.monotonic() - t0, 1), **kw})

    def on_cap(e: BaseException) -> None:
        end_reason.setdefault("end", "run_cap")
        stop.set()

    def drain() -> list[str]:
        """Let every capture worker finish (they stop waiting once quit is set) before the
        container goes; returns the ones still running (astra r2 #7)."""
        cur["quit"].set()
        for t in pending:
            t.join(timeout=TRANSCRIPT_POLL_S + 300)
        return [t.name for t in pending if t.is_alive()]

    def chain(step: str | None) -> tuple[Path, SS.KeepStore]:
        if step == GRADED_STEP:
            return chains[GRADED_STEP]
        if "plan" not in chains:  # created on first use, with its own run.json
            plan_root.mkdir(parents=True, exist_ok=True)
            IR._dump(plan_root / "run.json", {**rj, "run_key": f"{cfg.run_key}-plan"})
            chains["plan"] = (plan_root, SS.KeepStore(plan_root, f"{cfg.run_key}-plan",
                                                      host=cfg.host))
        return chains["plan"]

    ns_holder: dict = {}

    def on_boundary(payload: dict) -> dict:
        root, keep = chain(cur["step"])
        return capture(ns_holder["ns"], payload, rd=root, rj=rj, keep=keep, cfg=cfg,
                       step=cur["step"], eligible=eligible if cur["step"] == GRADED_STEP
                       else None, stop=stop, end_reason=end_reason, res=res, mark=mark,
                       seen=seen, pending=pending, quit_=cur["quit"])

    def sim_factory(runner):
        if resume_export is not None:
            return I.UserSim.restore(resume_export, task.persona,
                                     NoSimulatorModel() if cfg.capture_ask else sim_model, runner,
                                     purpose=f"sim:{cfg.run_key}")
        return I.UserSim(task.persona, NoSimulatorModel() if cfg.capture_ask else sim_model,
                         runner, purpose=f"sim:{cfg.run_key}",
                         planting=cfg.planting)

    ensure_cli_volume(cfg.host, task.info.image)
    ns = session_cls(ARM, resume_b["tag"] if resume_b else task.info.image, sim_factory,
                     host=cfg.host, extra_args=run_args(), setup_git=not bool(resume_b),
                     cpus=cfg.cpus, memory_mb=cfg.memory_mb, min_free_gb=cfg.min_free_gb,
                     on_boundary=on_boundary, on_cap=on_cap, before_close=drain)
    ns._cc_token = token  # for the secret scan; never written anywhere
    observed = 0
    try:
        with ns:
            ns_holder["ns"] = ns
            if cfg.capture_ask:
                def captured(row):
                    end_reason.setdefault("end", "ask_user")
                    stop.set()
                    stop_cli(ns.sb)
                ns.server.capture_ask = captured
            res["container"] = ns.sb.name
            res["network"] = ns.egress.network
            if resume_b:
                res["fork_check"] = fork_check(ns, resume_b)
                if not res["fork_check"]["ok"] or resume_b.get("ineligible_reason"):
                    raise ForkMismatch(json.dumps(res["fork_check"]))
                res["continued_from"] = str(resume_boundary)
            install_config(ns.sb, ns.server)
            res["egress_check"] = N.egress_check(ns.sb, ns.egress,
                                                 "https://api.anthropic.com/v1/messages")
            IR._dump(rd / "egress_check.json", res["egress_check"])
            if not res["egress_check"]["ok"]:
                raise RuntimeError("egress check failed; see egress_check.json")
            mark("started")
            env = cli_env(cfg.cc, ns.egress, ns.server, token)
            for step in cfg.steps:
                if stop.is_set():
                    break
                sid = str(uuid.uuid4())
                cur.update(step=step, session_id=sid, quit=threading.Event())
                ns.server.step = step
                state: dict = {}
                prior_compactions = len(compact_rows(resume_rows)) if resume_b else 0
                if resume_b:
                    edited = edited_transcript(resume_rows, resume_b["native_summary_uuid"],
                                               _block_text(resume_rows[-1]["message"]["content"]),
                                               sid)
                    ns.sb.put_file(("".join(json.dumps(x) + "\n" for x in edited)).encode(),
                                   f"{PROJECT}/{sid}.jsonl")
                    seen[sid] = prior_compactions
                sd = rd / "steps" / step
                # detached in the container: a dropped ssh transport costs a reconnect
                r = N.detached_exec(ns.sb, cli_command(cfg.cc, session_id=None if resume_b else sid,
                                                       resume=sid if resume_b else None,
                                                       max_calls=cfg.max_calls if cfg.capture_ask else None),
                                    name=f"{step}-{sid[:8]}",
                                    timeout_s=cfg.step_timeout_s.get(step, 5400),
                                    log_path=sd / "stream.jsonl", env=env,
                                    stdin=(CONTINUE_PROMPT if resume_b else task.steps[step]).encode(), stop=stop,
                                    on_line=_stream_watch(state, stop))
                res.setdefault("reconnects", {})[step] = getattr(r, "reconnects", 0)
                if r.killed in ("container_gone", "output_lost"):  # infrastructure first
                    end_reason.setdefault("end", "infra_killed")
                    res["error"] = f"{step}: {r.killed}"
                    break
                if r.killed:
                    stop_cli(ns.sb)
                alive = drain()  # boundary files are written before the step's transcript
                if alive:
                    res["capture_workers_alive"] = alive
                try:
                    rows = read_transcript(ns.sb, f"{PROJECT}/{sid}.jsonl", strict=cfg.capture_ask)
                except SB.SandboxError as e:
                    # missing evidence: the run can't count as complete (astra r2 #4)
                    res.setdefault("transcript_errors", []).append(f"{step}: {e}")
                    end_reason.setdefault("end", "transcript_error")
                    rows = []
                (sd / "transcript.jsonl").write_text(
                    "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
                n_comp = max(len(compact_rows(rows)), len(state.get("compactions") or []))
                observed += max(0, n_comp - prior_compactions)
                if seen.get(sid, 0) < n_comp:  # this session compacted without a capture
                    end_reason.setdefault("end", "boundary_missing")
                bad = _step_ok(r, state)
                res["steps"][step] = {"session_id": sid, "rc": r.rc, "seconds": r.seconds,
                                      "killed": r.killed, "timed_out": r.timed_out,
                                      "api_calls": max(0, api_calls(rows) - api_calls(resume_rows)) if resume_b else api_calls(rows),
                                      "init": state.get("init"),
                                      "result": state.get("result"),
                                      "compactions": state.get("compactions", []),
                                      "n_compactions": len(compact_rows(rows)),
                                      "rate_limit": state.get("rate_limit"), "failed": bad}
                if cfg.capture_ask:
                    cut = next((i for i, row in enumerate(rows) if resume_b and
                                row.get("uuid") == resume_b["native_summary_uuid"]), -1)
                    res["token_usage"] = transcript_usage(resumed_window_rows(rows, resume_rows, CONTINUE_PROMPT, cut=cut))
                    res["rate_limit"] = state.get("rate_limit")
                mark("step_done", step=step, rc=r.rc, killed=r.killed, failed=bad)
                if state.get("auth_error") or (bad and "not Max" in bad):
                    raise AuthError(state.get("auth_error") or bad)
                if end_reason.get("end") in ("transcript_error", "boundary_missing"):
                    break
                if step == "01_plan":
                    res["plan_md"] = ns.sb.exec("cat /tmp/plan.md 2>/dev/null", timeout_s=60,
                                                workdir="/").stdout or None
                if r.timed_out_by_cli or r.killed == "client_timeout":
                    end_reason.setdefault("end", "time_cap")
                    break
                if r.killed == "stop":
                    break  # a boundary, the cap or the auth watch stopped it: end_reason says
                if bad:
                    end_reason.setdefault(
                        "end", "infra_killed" if bad.startswith("infra_killed") else "cli_error")
                    res["error"] = f"{step}: {bad}"
                    break
            captured = len(res["boundaries"]) + len(res["plan_boundaries"])
            if observed > captured and end_reason.get("end") not in (
                    "boundary_error", "cli_error", "auth_error", "transcript_error",
                    "infra_killed", "infra_error"):
                end_reason["end"] = "boundary_missing"
                res["error"] = f"{observed} compactions, {captured} boundaries captured"
            if cfg.capture_ask:
                res["captured_asks"] = list(ns.server.captured_asks)
                res["sim_calls"] = len(ns.server.rows)
                if ns.server.rows or getattr(ns.server.sim, "rounds", []):
                    raise RuntimeError("continuation smoke invoked the simulator")
            res["end"] = end_reason.get("end") or "submitted"
            res["compactions_observed"] = observed
            res["egress_events"] = ns.egress.events()
    except AuthError as e:
        res["end"], res["error"] = "auth_error", str(e)
    except Exception as e:  # noqa: BLE001 - recorded; the session tore itself down
        # a container that vanished mid-run is a kill, not a task result (rerun once, heavy)
        res["end"] = ("infra_killed" if N.GONE in str(e) else
                      "infra_error" if isinstance(e, SB.SandboxError) else "error")
        res["error"] = traceback.format_exc()[-6000:]
    finally:
        alive = [t.name for t in pending if t.is_alive()]
        if alive:  # the session is gone: these can only fail now, and are reported
            res["capture_workers_alive"] = alive
        if ns.server is not None:  # the simulator's books, whatever ended the run
            sim = ns.server.sim
            res["boundary_calls"] = ns.server.boundary_calls
            IR._dump(rd / "sim" / "ask_log.json", getattr(sim, "ask_log", []))
            IR._dump(rd / "sim" / "rounds.json", getattr(sim, "rounds", []))
            IR._dump(rd / "sim" / "replies.json", ns.server.rows)
            res["sim_cost_usd"] = round(sum(x.get("cost_usd") or 0.0
                                            for x in getattr(sim, "rounds", [])), 6)
        res["cleanup_unconfirmed"] = ns.cleanup_unconfirmed
        # docker clients that outlived their command (tolerant_exec): exposure to a late exec
        res["client_timeouts"] = dict(getattr(ns, "exec_stats", {}) or {})
        res["seconds"] = round(time.monotonic() - t0, 1)
        mark("ended", end=res["end"])
        IR._dump(rd / "result.json", res)
    return res


def run_args() -> tuple[str, ...]:
    """`docker run` extras: the CLI volume (read-only) and the hook header's tmpfs."""
    return ("-v", f"{CLI_VOLUME}:{CLI_DIR}:ro",
            "--tmpfs", f"{HEADER_DIR}:rw,mode=700,size=1m")


def capture(ns: N.NativeSession, payload: dict, *, rd: Path, rj: dict, keep: SS.KeepStore,
            cfg: BaseConfig, step: str | None, eligible, stop: threading.Event,
            end_reason: dict, res: dict, mark, seen: dict, pending: list,
            quit_: threading.Event | None = None) -> dict:
    """The PostCompact hook's handler, phase 1 (the CLI is blocked in its hook until it
    returns): the workspace and simulator as they are at the boundary. The agent's background
    processes (children of the CLI other than the hook) are stopped with SIGSTOP for the
    secret scan, the fingerprint and the kept commit, and resumed after (astra r1 #7); they are
    listed in the boundary, since a fork never has them. Claude Code writes the boundary and
    summary rows to the transcript only after the hook returns (seen live, 24 Oct), so phase 2
    (`_finish`, a thread) waits for them and writes the boundary files. Any failure here stops
    the CLI and ends the run "boundary_error" (astra r1 #4). `seen` counts boundaries per
    session and per chain."""
    from compaction.s2 import interact_run as IR
    if payload.get("hook_event_name") not in (None, "PostCompact"):
        return {"ok": True, "ignored": payload.get("hook_event_name")}
    sb, server = ns.sb, ns.server
    sid = str(payload.get("session_id") or "")
    want = seen.get(sid, 0) + 1
    seen[sid] = want
    kk = ("_k", str(rd))
    k = seen[kk] = max(seen.get(kk, 0), N.next_k(rd) - 1) + 1  # phase 2 may lag
    times: dict = {}
    stopped: dict[int, str] = {}
    try:
        t = time.monotonic()
        try:
            stopped = N.freeze(sb, CLI, HOOK, getattr(ns, "services", frozenset()))
            times["freeze_s"] = round(time.monotonic() - t, 1)
            t = time.monotonic()
            scan = N.secret_scan(sb, {TOKEN_ENV: _token_of(ns), "EMX_SIM_TOKEN": server.token})
            times["scan_s"] = round(time.monotonic() - t, 1)
            if scan["hits"]:
                raise RuntimeError(f"a secret is in container files {scan['hits'][:10]}; "
                                   "refusing to keep this image")
            t = time.monotonic()
            fp = I.fingerprint(I.guarded_runner(sb))
            times["fingerprint_s"] = round(time.monotonic() - t, 1)
            snap = N.snapshot(sb, keep, k)
            times["snapshot_s"] = round(snap["snapshot_s"], 1)
            # the simulator inside the same interval (the agent is blocked in its hook anyway)
            if step == GRADED_STEP and hasattr(server.sim, "record_compaction"):
                server.sim.record_compaction(k, pre_tokens=payload.get("pre_tokens"))
            frozen = {"sim_export": server.export(),
                      "sim_rows": [dict(r) for r in server.rows],
                      "sim_probe": IR.sim_probe(server.sim)}
        finally:
            N.signal_pids(sb, sorted(stopped), "CONT")
        frozen["harness_exempted_processes"] = list(
            getattr(sb, "harness_exempted_processes", {}).values())
        snap["processes"] = [_redact(f"{p} {v}", server.token) for p, v in sorted(stopped.items())]
        snap["stopped_processes"] = []
        frozen.update(hook_utc=N.utc(), times=times, quiescent=not stopped,
                      scan={"scanned": scan["scanned"], "seconds": scan["seconds"]})
    except Exception as e:  # noqa: BLE001 - the run ends here; the CLI must not go on
        res.setdefault("boundary_errors", []).append(
            {"k": k, "phase": 1, "error": traceback.format_exc()[-3000:]})
        # a docker/ssh failure is infrastructure (rerun once), anything else a capture defect
        end_reason.setdefault("end", "infra_error" if isinstance(e, SB.SandboxError)
                              else "boundary_error")
        mark("boundary_error", k=k, phase=1, error=f"{type(e).__name__}: {e}"[:300])
        stop.set()
        stop_cli(sb)
        return {"ok": False, "k": k, "error": f"{type(e).__name__}"}
    mark("boundary_kept", k=k, tag=snap["tag"], **times)
    kw = {"ns": ns, "payload": payload, "k": k, "want": want, "snap": snap, "fp": fp,
          "frozen": frozen, "rd": rd, "rj": rj, "cfg": cfg, "step": step, "eligible": eligible,
          "stop": stop, "end_reason": end_reason, "res": res, "mark": mark, "quit_": quit_}
    th = threading.Thread(target=_finish, name=f"cc-boundary-{k}", daemon=True, kwargs=kw)
    pending.append(th)
    th.start()
    return {"ok": True, "k": k, "tag": snap["tag"]}


def _redact(line: str, token: str) -> str:
    import re
    line = re.sub(rf"({N.TOKEN_HEADER}: )\S+", r"\1<redacted>", line)
    return line.replace(token, "<redacted>") if token else line


def read_transcript(sb, path: str, *, strict: bool = False) -> list[dict]:
    """The transcript, or SandboxError: an unreadable transcript is missing evidence, never an
    empty one (astra r2 #4)."""
    r = sb.exec(f"cat {shlex.quote(path)}", timeout_s=120, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"cannot read {path}: rc={r.rc} {r.stderr[-200:]}")
    return read_jsonl(r.stdout, strict=strict)


def _read_until(sb, path: str, ready: Callable[[list[dict]], bool], wait_s: float,
                quit_: threading.Event | None = None) -> tuple[list[dict], bool]:
    """Poll until ready(rows). Returns (the latest rows read, whether they were ready): not
    ready after wait_s, or at once (after a last read) when quit_ is set (the step has ended,
    or the session is being torn down). The latest rows are kept either way, so rows written
    while waiting are never lost (astra r3 #1)."""
    t0 = time.monotonic()
    while True:
        last = quit_ is not None and quit_.is_set()
        rows = read_transcript(sb, path)
        if ready(rows):
            return rows, True
        if last or time.monotonic() - t0 > wait_s:
            return rows, False
        time.sleep(TRANSCRIPT_POLL_S)  # each read is an ssh session to the docker host


def _resume_cut(rows: list[dict], s: int, nxt: int) -> int:
    """Where a boundary's transcript ends: the request the CLI resumed with when it is
    recorded, else the last of the rows right after the summary that are not the agent's own
    (attachments and bookkeeping, which the fallback post_messages read: astra r2 #3)."""
    ri = first_request_after(rows, s)
    if ri is not None and ri < nxt:
        return ri
    end = s
    for i in range(s + 1, nxt):
        if _is_msg(rows[i]) or rows[i].get("type") == "system":
            break
        end = i
    return end


def _finish(*, ns, payload, k, want, snap, fp, frozen, rd, rj, cfg, step, eligible, stop,
            end_reason, res, mark, quit_=None) -> None:
    sb = ns.sb
    path = str(payload.get("transcript_path") or "")
    graded = step == GRADED_STEP
    try:
        def has_summary(rows: list[dict]) -> bool:
            bs = compact_rows(rows)
            return len(bs) >= want and summary_row(rows, bs[want - 1]) is not None

        rows, ok = _read_until(sb, path, has_summary, TRANSCRIPT_WAIT_S, quit_)
        if not ok:
            raise RuntimeError(f"transcript {path}: boundary {want} not written in "
                               f"{TRANSCRIPT_WAIT_S}s")

        def has_request(rows: list[dict]) -> bool:
            s = summary_row(rows, compact_rows(rows)[want - 1])
            return first_request_after(rows, s) is not None

        # the request the CLI resumes with: exact post_messages (astra r1 #3); no wait once
        # the step has ended (a manual /compact at the end of a turn sends none)
        rows, _ = _read_until(sb, path, has_request, POST_REQUEST_WAIT_S, quit_)
        bs = compact_rows(rows)
        s = summary_row(rows, bs[want - 1])
        nxt = bs[want] if len(bs) > want else len(rows)
        rows = rows[: _resume_cut(rows, s, nxt) + 1]  # nothing the agent did after (sealing)
        instr = cfg.task.steps.get(step or "", "")
        conv = transcript_boundary(rows, want, model=cfg.cc.model, step_instruction=instr)
        hook_summary = str(payload.get("compact_summary") or "")
        raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        conv.nb.native_files.update({"transcript.jsonl": raw,
                                     "hook_payload.json": json.dumps(payload, indent=1,
                                                                     ensure_ascii=False)})
        extra = {"harness": ARM, "cli_version": CLI_VERSION,
                 "session_id": payload.get("session_id"), "transcript_path": path,
                 "native_summary_uuid": conv.summary_uuid,
                 "native_boundary_uuid": conv.boundary_uuid, "boundary_in_session": want,
                 "auto_compact_window": cfg.cc.auto_compact_window, "model": cfg.cc.model,
                 "effort": cfg.cc.effort, "tool_result_unwrapped": True,
                 "secret_scan": frozen["scan"], "quiescent": frozen["quiescent"],
                 "harness_exempted_processes": frozen.get("harness_exempted_processes", []),
                 "capture_times": frozen["times"], "hook_utc": frozen["hook_utc"],
                 "post_source": conv.nb.info.get("post_source"), "graded_chain": graded,
                 # the hook carries the model's raw output (<analysis> + <summary>); the
                 # message holds the <summary> body inside Claude Code's wrapper
                 "hook_summary_in_message": bool(hook_summary) and _summary_body(
                     hook_summary) in conv.nb.summary}
        frozen["sim_export"] = annotate_delivery_distances(rows, want, frozen["sim_export"], k)
        update_delivery_boundary(ns.server, frozen["sim_export"], k, conv.nb.pre_tokens)
        integrity = boundary_replay_integrity(rows, conv.summary_uuid, hook_summary)
        extra["capture_integrity"] = integrity
        gate = eligible
        services = sorted(getattr(ns, "services", ()) or ())
        extra["services_running"] = services
        why = ("not_quiescent" if not frozen["quiescent"] else
               "services_unvalidated" if services else None)
        if why:
            # background work or entrypoint services keep running in the base run and are
            # absent (or restarted fresh) in a fork: kept for Q1, never a fork source
            # (astra r2 #2, r3 #3)
            extra["ineligible_reason"] = why
            if eligible is not None:
                def gate(b, f):
                    return False
        if cfg.stop_first_eligible:
            requested_gate = gate
            def gate(directory, fields):
                return (not why and integrity["ok"] and bool(fields.get("tag"))
                        and (requested_gate is None or requested_gate(directory, fields)))
            if not integrity["ok"] and not why:
                extra["ineligible_reason"] = "invalid_transcript"
        bdir = N.write_boundary(rd, k, conv.nb, run_json=rj, snap=snap,
                                sim_export=frozen["sim_export"], sim_rows=frozen["sim_rows"],
                                step=step or "", step_instruction=instr, fingerprint=fp,
                                sim_probe_sha256=frozen["sim_probe"],
                                calls_in_step=api_calls(rows), extra=extra, eligible=gate)
        res["boundaries" if graded else "plan_boundaries"].append(k)
        res.setdefault("compactions", []).append(N.compaction_meta(bdir, step))
        mark("boundary_saved", k=k, step=step, pre_tokens=conv.nb.pre_tokens)
        fields = json.loads((bdir / "boundary" / "boundary.json").read_text())
        if fields.get("eligible") and graded:
            end_reason.setdefault("end", "eligible")
        elif graded and cfg.stop_after and len(res["boundaries"]) >= cfg.stop_after:
            end_reason.setdefault("end", "stop_after")
    except Exception as e:  # noqa: BLE001 - the run ends; the kept image stays listed
        res.setdefault("boundary_errors", []).append(
            {"k": k, "phase": 2, "error": traceback.format_exc()[-3000:]})
        end_reason.setdefault("end", "infra_error" if isinstance(e, SB.SandboxError)
                              else "boundary_error")
        mark("boundary_error", k=k, phase=2, error=f"{type(e).__name__}: {e}"[:300])
    if end_reason.get("end") in ("eligible", "stop_after", "boundary_error", "infra_error"):
        stop.set()
        stop_cli(sb)


def _summary_body(raw: str, limit: int | None = 2000) -> str:
    body = raw.split("<summary>", 1)[-1].split("</summary>", 1)[0].strip()
    return body if limit is None else body[:limit]


def _token_of(ns: N.NativeSession) -> str:
    tok = getattr(ns, "_cc_token", None)
    if not tok:
        raise RuntimeError(f"{TOKEN_ENV} is not set")
    return tok


# ------------------------------------------------------------------------------------------
# Forks
# ------------------------------------------------------------------------------------------


def edited_transcript(rows: list[dict], summary_uuid: str, new_summary: str,
                      new_session: str) -> list[dict]:
    """The boundary's transcript under `new_session`, with the summary message replaced. Only
    rows up to and including the summary's boundary stay meaningful to the CLI (it is the last
    boundary in a kept transcript)."""
    out = []
    hit = False
    for r in rows:
        r = json.loads(json.dumps(r))
        if "sessionId" in r:
            r["sessionId"] = new_session
        if r.get("uuid") == summary_uuid:
            r["message"]["content"] = new_summary
            hit = True
        out.append(r)
    if not hit:
        raise ValueError(f"summary row {summary_uuid} not in the transcript")
    return out


class ForkMismatch(RuntimeError):
    pass


def fork_check(ns: N.NativeSession, b: dict) -> dict:
    """The restored workspace and simulator equal the boundary's: the fingerprint (the
    boundary's version) and the simulator's first request for SIM_PROBE, hashed."""
    from compaction.s2 import interact_run as IR
    fp = I.sha256(I.fingerprint(I.guarded_runner(ns.sb), int(b.get("fingerprint_version") or 1)))
    probe = IR.sim_probe(ns.server.sim)
    out = {"fingerprint_ok": fp == b.get("fingerprint_sha256"),
           "sim_probe_ok": probe == b.get("sim_probe_sha256")}
    out["ok"] = out["fingerprint_ok"] and out["sim_probe_ok"]
    return out


def insert_in_summary(message: str, body: str, block: str | list[str]) -> str:
    """`block` inserted right after the summary body inside Claude Code's summary message,
    before its closing paragraphs (a line appended after those reads as outside the summary:
    smoke 2's fork, 24 Oct). `body` is the hook payload's <summary> text."""
    if isinstance(block, list):
        block = "\n".join(block)
    if not block:
        return message
    b = _summary_body(body, limit=None)
    tail = b
    at = message.rfind(tail) if b else -1
    if at < 0:
        raise ValueError("summary body not found in the summary message")
    end = at + len(tail)
    return message[:end] + "\n\n" + block + message[end:]


def fork(bdir: Path, out_dir: Path, *, new_summary: str, prompt: str, sim_model, token: str,
         persona: str, cfg: CCConfig | None = None, host: str | None = "ssh://rig",
         timeout_s: int = 1800, check: bool = True, session_cls=N.NativeSession,
         probe: bool = False, max_calls: int = 30, mutation_paths=(),
         required_context=(), forbidden_context=()) -> dict:
    """Resume boundary `bdir` (a b<k> dir of a Claude Code run) from its kept image with the
    summary replaced by `new_summary`, as a forked session given `prompt`. Refuses
    (ForkMismatch) when the restored workspace or simulator differ from the boundary's, unless
    check=False. Writes out_dir/{fork.json, stream.jsonl, transcript.jsonl,
    input_transcript.jsonl}; returns fork.json."""
    bdir, out_dir = Path(bdir), Path(out_dir)
    b = json.loads((bdir / "boundary" / "boundary.json").read_text())
    rows = read_jsonl((bdir / "native" / "transcript.jsonl").read_text(), strict=probe)
    exported = json.loads((bdir / "boundary" / "sim_state.json").read_text())
    rj = json.loads((bdir.parent / "run.json").read_text())
    cfg = cfg or CCConfig(model=b.get("model") or MODEL, effort=b.get("effort"),
                          auto_compact_window=b.get("auto_compact_window"))
    new_sid = str(uuid.uuid4())
    si = next(i for i, r in enumerate(rows) if r.get("uuid") == b["native_summary_uuid"])
    # the kept transcript ends at the request the CLI resumed with; a fork resumes from the
    # summary itself (the CLI re-renders its own attachments on resume)
    edited = edited_transcript(rows[: si + 1], b["native_summary_uuid"], new_summary, new_sid)
    out_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in edited) + "\n"
    (out_dir / "input_transcript.jsonl").write_text(body)
    res: dict = {"bdir": str(bdir), "image": b["tag"], "input_session": new_sid,
                 "prompt": prompt, "summary_sha256": I.sha256(new_summary),
                 "cc": {"model": cfg.model, "effort": cfg.effort,
                        "auto_compact_window": cfg.auto_compact_window},
                 "started_utc": N.utc()}

    def sim_factory(runner):
        return I.UserSim.restore(exported, persona, NoSimulatorModel() if probe else sim_model,
                                 runner, purpose="sim:fork")

    stop = threading.Event()
    why: dict = {}
    audit_holder: dict = {}

    def on_cap(e: BaseException) -> None:
        why.setdefault("end", "run_cap")
        stop.set()

    ns = session_cls(ARM, b["tag"], sim_factory, host=host, setup_git=False,
                     extra_args=run_args(),
                     on_boundary=lambda p: {"ok": True, "ignored": "fork"}, on_cap=on_cap)
    try:
        with ns:
            ns.server.step = b.get("stage") or GRADED_STEP
            if probe:
                def captured(row):
                    why.setdefault("end", "ask_user")
                    stop.set()
                    stop_cli(ns.sb)
                ns.server.capture_ask = captured
            res["fork_check"] = fork_check(ns, b)  # the pi arm's check (astra r1 #7)
            if not res["fork_check"]["ok"] and check:
                raise ForkMismatch(json.dumps(res["fork_check"]))
            if b.get("ineligible_reason") and check:
                # background work or services would carry on in the base run but not here
                raise ForkMismatch(f"not a fork source: {b['ineligible_reason']} "
                                   + json.dumps(b.get("processes"))[:500])
            install_config(ns.sb, ns.server)  # a new token and server for the fork
            if probe:
                audit_holder["audit"] = ProbeAudit(ns.sb, mutation_paths=mutation_paths, stop=stop)
                if mutation_paths and not audit_holder["audit"].trace_available:
                    raise RuntimeError("Part B requires successful strace ptrace preflight before the model starts")
                ns.server.on_audit = audit_holder["audit"].hook
                install_audit_hooks(ns.sb)
            ns.sb.put_file(body.encode(), f"{PROJECT}/{new_sid}.jsonl")
            state: dict = {}
            env = cli_env(cfg, ns.egress, ns.server, token)
            r = N.detached_exec(ns.sb, cli_command(cfg, resume=new_sid, fork=True,
                                                   prompt=prompt,
                                                   max_calls=max_calls if probe else None),
                                name=f"fork-{new_sid[:8]}", timeout_s=timeout_s,
                                log_path=out_dir / "stream.jsonl", env=env, stop=stop,
                                on_line=_stream_watch(state, stop))
            if r.killed:
                stop_cli(ns.sb)
            sid = (state.get("init") or {}).get("session_id")
            if sid:
                if probe:
                    raw = "".join(json.dumps(row) + "\n" for row in
                                  read_transcript(ns.sb, f"{PROJECT}/{sid}.jsonl", strict=True))
                else:
                    raw = ns.sb.exec(f"cat {PROJECT}/{sid}.jsonl", timeout_s=120,
                                     workdir="/").stdout
                (out_dir / "transcript.jsonl").write_text(raw)
            # a fork's only stops are the cap and the auth watch: neither is a completion
            # (astra r3 #2)
            bad = (state.get("auth_error") or why.get("end")
                   or ("stopped" if r.killed else None) or _step_ok(r, state))
            res.update(rc=r.rc, seconds=r.seconds, killed=r.killed, init=state.get("init"),
                       result=state.get("result"), forked_session=sid,
                       sim_rows=ns.server.rows, auth_error=state.get("auth_error"),
                       run_key=rj.get("run_key"), outcome=bad or "ok")
            if probe:
                captured_rows = list(ns.server.captured_asks)
                transcript_path = out_dir / "transcript.jsonl"
                full_rows = read_jsonl(transcript_path.read_text(), strict=True) if transcript_path.exists() else []
                cut = next((i for i, row in enumerate(full_rows)
                            if row.get("uuid") == b["native_summary_uuid"]), -1)
                window = resumed_window_rows(full_rows, rows, prompt, cut=cut)
                evidence = audit_holder["audit"].finish(
                    window, captured_asks=captured_rows,
                    terminal_stop=bool(captured_rows) or (state.get("result") or {}).get("subtype") == "error_max_turns",
                    submission=final_text(out_dir / "stream.jsonl")
                    if (out_dir / "stream.jsonl").exists() and
                    (state.get("result") or {}).get("subtype") == "success" else "")
                if cut < 0:
                    evidence["capture_complete"] = False
                    evidence["part_a_complete"] = False
                    evidence["issues"].append("resumed summary absent from transcript")
                context = resumed_context_check(full_rows, b["native_summary_uuid"], new_summary,
                                                prompt, required=required_context,
                                                forbidden=forbidden_context, source_rows=rows)
                res["context_check"] = {key: value for key, value in context.items() if key != "surfaces"}
                N._dump(out_dir / "initial_context.json", context)
                if (required_context or forbidden_context) and not context["ok"]:
                    evidence["part_a_complete"] = evidence["capture_complete"] = False
                    evidence["issues"].append("effective resumed context verification failed")
                if any(not call.get("ok") for call in ns.server.audit_calls):
                    evidence["part_a_complete"] = evidence["capture_complete"] = False
                    evidence["issues"].append("audit endpoint reported a failure")
                res["token_usage"] = transcript_usage(window) if cut >= 0 else None
                rounds = getattr(ns.server.sim, "rounds", [])
                res.update(probe=True, max_calls=max_calls,
                           call_limit_unit="Claude Code assistant turns (--max-turns)",
                           captured_asks=captured_rows,
                           sim_calls=len(ns.server.rows), sim_cost_usd=sum(
                               x.get("cost_usd") or 0.0 for x in rounds),
                           rate_limit=state.get("rate_limit"),
                           evidence_complete=evidence["capture_complete"])
                if ns.server.rows or rounds:
                    raise RuntimeError("probe unexpectedly invoked the simulator")
                if why.get("end") == "ask_user":
                    res["outcome"] = "ask_user"
                elif (state.get("result") or {}).get("subtype") == "error_max_turns":
                    res["outcome"] = "call_cap"
                elif not bad:
                    res["outcome"] = "submission"
                N._dump(out_dir / "events.json", evidence["events"])
                N._dump(out_dir / "evidence.json", evidence)
    finally:
        res["cleanup_unconfirmed"] = ns.cleanup_unconfirmed
        res["ended_utc"] = N.utc()
        (out_dir / "fork.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


def final_text(stream_path: Path) -> str:
    """The `result` event's text of a stream-json log."""
    for line in reversed(Path(stream_path).read_text().splitlines()):
        with contextlib.suppress(ValueError):
            ev = json.loads(line)
            if ev.get("type") == "result":
                return str(ev.get("result") or "")
    return ""


class NoSimulatorModel:
    """Probe guard: even an accidental UserSim.ask cannot incur a model charge."""

    def complete(self, *args, **kwargs):
        raise RuntimeError("model calls are forbidden in a decision probe")


def insert_summary_lines(message: str, body: str, lines: list[str]) -> str:
    """Insert exact lines at the frozen end-of-summary-body position (v1)."""
    if any(not isinstance(line, str) or "\n" in line or "\r" in line for line in lines):
        raise ValueError("summary insertion requires a list of individual text lines")
    return insert_in_summary(message, body, lines)


def verify_effective_context(summary: str, preserved_tail: Any, attachments: Any,
                             probe_message: str, *, required=(), forbidden=()) -> dict:
    """Exact-text screen over every supplied resumed-context surface, with per-surface hits.

    The caller must provide the actual rendered context; a missing surface is an error,
    whereas an explicitly empty list is valid. Repo recoverability is intentionally separate.
    """
    surfaces = {"summary": summary, "preserved_tail": preserved_tail,
                "attachments": attachments, "probe_message": probe_message}
    if any(value is None for value in surfaces.values()):
        raise ValueError("all resumed-context surfaces must be supplied")
    rendered = {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                for key, value in surfaces.items()}
    required, forbidden = list(required), list(forbidden)
    if any(not text for text in [*required, *forbidden]):
        raise ValueError("empty rule text is not a context check")
    hits = {text: [key for key, value in rendered.items() if text in value]
            for text in [*required, *forbidden]}
    missing = [text for text in required if not hits[text]]
    leaked = [text for text in forbidden if hits[text]]
    return {"ok": not missing and not leaked, "missing": missing, "leaked": leaked,
            "hits": hits, "surface_sha256": {key: I.sha256(value)
                                              for key, value in rendered.items()}}


def continue_from_boundary(bdir: Path, out_dir: Path, *, cfg: BaseConfig,
                           sim_model, token: str, eligible=None,
                           session_cls=N.NativeSession) -> dict:
    """Continue a base chain from its kept simulator and image, with future capture enabled.

    This is a new output segment. Original boundary numbering is retained and the source
    image/transcript never changes. Base continuations are not decision-probe forks.
    """
    from dataclasses import replace
    out_dir = Path(out_dir)
    if out_dir.name != cfg.run_key:
        raise ValueError(f"out_dir basename must equal cfg.run_key ({cfg.run_key})")
    return run_base(replace(cfg, root=out_dir.parent), sim_model=sim_model, token=token,
                    eligible=eligible, session_cls=session_cls, resume_boundary=Path(bdir))


def ordered_evidence(rows: list[dict], *, captured_asks=(), submission: str = "",
                     audit_events: list[dict] | None = None) -> dict:
    """Normalize native tool evidence for the shared scorers, never invent missing evidence.

    A native transcript alone cannot witness shell edits reverted within one call. Any
    Bash use therefore marks mutation coverage incomplete unless an audited event stream is
    provided. Part B must use capture_complete, not mistake absent edits for no action.
    """
    events: list[dict] = []
    pending: dict[str, dict] = {}
    issues: list[str] = []
    ask_texts: set[str] = set()

    def add(kind, **data):
        events.append({"seq": len(events), "kind": kind, "data": data})

    for row in rows:
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name, args = block.get("name", ""), block.get("input") or {}
                pending[block.get("id", "")] = {"name": name, "args": args}
                if name in (ASK_TOOL, "ask_user"):
                    text = args.get("question") or ""
                    ask_texts.add(text)
                    add("ask_user", text=text, tool_call_id=block.get("id"))
                elif name == "Bash" and audit_events is None:
                    issues.append("Bash mutation coverage needs intra-call audit")
            elif block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                call = pending.pop(tid, None)
                if call is None:
                    issues.append(f"unmatched tool result {tid}")
                    continue
                name, args = call["name"], call["args"]
                output = _block_text(block.get("content"))
                raw = row.get("toolUseResult") or {}
                raw = raw if isinstance(raw, dict) else {}
                rc = raw.get("exitCode", raw.get("exit_code"))
                # Claude Code's success boolean is insufficient to recover a Bash exit code.
                if name == "Bash":
                    add("command", command=args.get("command", ""), output=output,
                        exit_code=rc, completed=rc is not None and not args.get("run_in_background")
                        and not raw.get("interrupted") and not raw.get("timed_out"),
                        cwd=row.get("cwd"), tool_call_id=tid,
                        is_error=bool(block.get("is_error")))
                elif name == "Read":
                    data = raw.get("file") or {}
                    start, count, total = data.get("startLine"), data.get("numLines"), data.get("totalLines")
                    add("read", path=args.get("file_path", ""), output=output,
                        start_line=start,
                        end_line=start + count - 1 if isinstance(start, int) and isinstance(count, int) else None,
                        complete=start == 1 and count == total and isinstance(total, int),
                        forced=bool(raw.get("harness_forced", False)), cwd=row.get("cwd"),
                        exit_code=1 if block.get("is_error") else 0, tool_call_id=tid)
                elif name in ("Grep", "Glob"):
                    add("search", tool=name, arguments=args, pattern=args.get("pattern", ""), path=args.get("path"),
                        glob=args.get("glob"), output_mode=args.get("output_mode", "files_with_matches"),
                        output=output, exit_code=1 if block.get("is_error") else 0,
                        completed=not block.get("is_error"), cwd=row.get("cwd"), tool_call_id=tid,
                        **search_filters(args, raw))
                elif name in ("Edit", "Write") and not block.get("is_error"):
                    if name == "Write":
                        before = raw.get("originalFile")
                        if before is None:
                            issues.append("Write missing before snapshot")
                        add("edit", path=args.get("file_path", ""), before=before,
                            after=args.get("content", ""), tool_call_id=tid)
                    else:
                        add("edit", path=args.get("file_path", ""),
                            before=args.get("old_string", ""), after=args.get("new_string", ""),
                            fragment=True, tool_call_id=tid)
    for event in events:
        if event["kind"] == "search":
            event["data"] = normalize_native_search(event["data"],
                                                     failed=not event["data"].get("completed"))
    if any(call["name"] not in (ASK_TOOL, "ask_user") for call in pending.values()):
        issues.append("unfinished tool calls")
    for row in captured_asks:
        text = row.get("text", row.get("question", ""))
        if text not in ask_texts:
            add("ask_user", text=text, source="terminal_capture")
    if submission and not captured_asks:
        add("submission", text=submission)
    if audit_events is not None:
        # Hook events have actual execution order, whereas tool-use rows precede execution.
        events = [{"seq": i, "kind": row["kind"], "data": dict(row["data"])}
                  for i, row in enumerate(audit_events)]
    return {"schema_version": 1, "events": events,
            "capture_complete": not issues, "issues": sorted(set(issues)),
            "pending_tool_ids": sorted(pending)}


# The audit is scoped to the frozen call-site files, supplied by the Part B task spec.
# Every mutation of those files is required; unsupported syscalls fail coverage closed.
AUDIT_HOOK = "/opt/emx/tool-audit-hook.sh"
AUDIT_DIR = "/tmp/emx-tool-audit"


def audit_hook_script() -> str:
    """Forward tool hooks, returning the server's structured PreToolUse response to the CLI."""
    return ("#!/bin/sh\n"
            f"umask 077; printf '{N.TOKEN_HEADER}: %s\\n' \"$EMX_SIM_TOKEN\" > {HEADER_DIR}/audit-h\n"
            f"curl --fail -sS -m 165 -X POST -H @{HEADER_DIR}/audit-h"
            f" -H 'content-type: application/json' --data-binary @- {N.AUDIT_URL}\n")


def install_audit_hooks(sb: SB.Sandbox) -> None:
    config = settings()
    for name in ("SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure"):
        config["hooks"][name] = [{"matcher": ".*", "hooks": [
            {"type": "command", "command": AUDIT_HOOK, "timeout": 180}]}]
    sb.put_file(json.dumps(config).encode(), SETTINGS)
    sb.put_file(audit_hook_script().encode(), AUDIT_HOOK)
    r = sb.exec(f"chmod 755 {AUDIT_HOOK} && mkdir -p {AUDIT_DIR}", timeout_s=30, workdir="/")
    if r.rc:
        raise SB.SandboxError("cannot install audit hooks")


def _read_files(sb, paths: list[str]) -> dict[str, str | None]:
    """Read declared evidence paths, with explicit unknowns for non-text or oversized files."""
    script = ("import json,pathlib; out={}\n"
              f"for name in {paths!r}:\n"
              " p=pathlib.Path(name)\n"
              " try:\n"
              "  out[name]=p.read_text() if p.stat().st_size <= 4*1024*1024 else None\n"
              " except FileNotFoundError: out[name]=''\n"
              " except (OSError,UnicodeError): out[name]=None\n"
              "print(json.dumps(out))\n")
    r = sb.exec("python3 -c " + shlex.quote(script), timeout_s=60, workdir="/app")
    if r.rc:
        raise SB.SandboxError("cannot read mutation audit paths")
    return json.loads(r.stdout)


def parse_mutation_trace(trace: str, before: dict[str, str | None], *, cwd: str = "/app") -> dict:
    """Replay Linux strace into declared-file mutations, including writes later reverted.

    The observer uses TRACE_FLAGS (4 MiB strings; raw read buffers). Open-file descriptions
    are shared across dup/fork, descriptor tables follow CLONE_FILES and cwd follows
    CLONE_FS. Unrelated caches/binaries never enter scoring. Temporary files are tracked as
    bytes and matter only when renamed onto a declared path. Unsupported target mutation,
    truncated write strings and uncertain offsets fail coverage closed.
    """
    import ast
    import posixpath

    def cbytes(literal):
        return ast.literal_eval("b" + literal)

    def text(literal):
        return cbytes(literal).decode("utf-8")

    def number(value):
        return int(re.match(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value.strip()).group(), 0)

    def args_of(line):
        body = line[line.index("(") + 1:re.search(r"\)\s+=", line).start()]
        parts, start, depth, quote, escape = [], 0, 0, False, False
        for index, char in enumerate(body):
            if escape:
                escape = False
            elif char == "\\" and quote:
                escape = True
            elif char == '"':
                quote = not quote
            elif not quote:
                if char in "([{":
                    depth += 1
                elif char in ")]}":
                    depth -= 1
                elif char == "," and depth == 0:
                    parts.append(body[start:index].strip())
                    start = index + 1
        return parts + [body[start:].strip()]

    targets = {posixpath.normpath(path) for path in before}
    files = {posixpath.normpath(path): value.encode() if value is not None else None
             for path, value in before.items()}
    issues, changes = [], []
    if not trace.strip():
        issues.append("empty mutation trace")
    if any(value is None for value in before.values()):
        issues.append("unreadable audit baseline")
    pending, records = {}, []
    # Join unfinished calls first; clone's descriptor snapshot belongs at invocation time.
    for index, raw in enumerate(trace.splitlines()):
        match = re.match(r"(?:(?:\[pid\s+)?(\d+)\]?\s+)?(\d+\.\d+)\s+(.*)", raw)
        if not match:
            if raw.strip() and not raw.startswith("strace:") and any(path in raw for path in targets):
                issues.append("unparsed target trace line")
            continue
        pid, _timestamp, line = match.groups()
        pid = pid or "root"
        start = index
        if "<unfinished ...>" in line:
            pending[pid] = (index, line.split("<unfinished ...>", 1)[0])
            continue
        if line.startswith("<..."):
            prior = pending.pop(pid, None)
            if prior is None or " resumed>" not in line:
                if any(path in line for path in targets) or re.match(
                        r"<\.\.\. (?:write|pwrite|rename|unlink|truncate|ftruncate|copy_file_range|sendfile|splice)", line):
                    issues.append("unmatched mutation syscall")
                continue
            start, prefix = prior
            line = prefix + line.split(" resumed>", 1)[1]
        if line.startswith(("+++", "---")):
            continue
        if re.search(r"\)\s+=\s+-1\b", line):
            continue
        op = line.partition("(")[0]
        result = re.search(r"\)\s+=\s+(-?0x[0-9a-fA-F]+|-?\d+)", line)
        if result is None:
            records.append((index, pid, op, line, None))
            continue
        records.append((start if op in ("fork", "vfork", "clone", "clone3") else index,
                        pid, op, line, int(result.group(1), 0)))
    for pid, (index, line) in pending.items():
        records.append((index, pid, line.partition("(")[0], line, None))
    tables, directories = {}, {}

    def process(pid):
        tables.setdefault(pid, {})
        directories.setdefault(pid, {"cwd": posixpath.normpath(cwd)})
        return tables[pid]

    def path_arg(pid, arg, directory="AT_FDCWD"):
        match = re.match(r'"(?:[^"\\]|\\.)*"', arg)
        if match is None:
            raise ValueError("missing path argument")
        value = text(match.group())
        if value.startswith("/"):
            return posixpath.normpath(value)
        base = directories[pid]["cwd"]
        if not directory.startswith("AT_FDCWD"):
            desc = process(pid).get(number(directory))
            annotated = re.search(r"<([^>]+)>", directory)
            base = annotated.group(1) if annotated else desc["path"] if desc else None
            if base is None:
                raise ValueError("unknown dirfd")
        return posixpath.normpath(posixpath.join(base, value))

    def mutate(path, value, op):
        old = files.get(path)
        files[path] = value
        if path in targets and old != value:
            if old is None or value is None:
                issues.append(f"unknown mutation contents: {path}")
            else:
                changes.append((path, old, value, op))

    def poison(path, reason):
        files[path] = None
        if path in targets:
            issues.append(f"{reason}: {path}")

    for _order, pid, op, line, rc in sorted(records, key=lambda item: item[0]):
        table = process(pid)
        args = []
        if rc is None:
            first = re.match(r"[^()]+\(([^,)]*)", line)
            desc = None
            if first:
                with contextlib.suppress(ValueError, AttributeError):
                    desc = table.get(number(first.group(1)))
            if op in ("read", "readv"):
                # A blocked/killed read cannot mutate bytes. Its eventual offset is unknown;
                # only a later write through this description can make it relevant.
                if desc:
                    desc["offset"] = None
                continue
            referenced = set(re.findall(r"<(/[^>]+)>", line))
            if desc:
                referenced.add(desc["path"])
            if op in ("copy_file_range", "sendfile", "splice"):
                # Incomplete transfers still only threaten the destination, never a read source.
                referenced = set()
                with contextlib.suppress(ValueError, AttributeError, IndexError):
                    partial = re.sub(r"\)\s+=.*$", "", line)
                    partial_args = args_of(partial + ") = 0")
                    output = partial_args[2] if op in ("copy_file_range", "splice") else partial_args[0]
                    annotated = re.search(r"<([^>]+)>", output)
                    output_desc = table.get(number(output))
                    path = annotated.group(1) if annotated else output_desc["path"] if output_desc else None
                    if path:
                        referenced.add(path)
            if op in ("write", "pwrite64", "writev", "pwritev", "pwritev2", "ftruncate",
                      "copy_file_range", "sendfile", "splice"):
                for path in referenced:
                    poison(posixpath.normpath(path), "unfinished mutation syscall")
                if not referenced:
                    issues.append("unresolved unfinished mutation syscall")
            elif op in ("clone", "clone3", "fork", "vfork", "chdir", "fchdir"):
                issues.append("unfinished process state syscall")
            elif op in ("rename", "renameat", "renameat2", "unlink", "unlinkat", "truncate", "open", "openat", "creat"):
                # Paths can be relative or temporary later renamed onto the target.
                issues.append("unfinished path mutation syscall")
            elif any(path in line for path in targets):
                issues.append("missing target syscall completion")
            continue
        try:
            args = args_of(line)
            if op in ("fork", "vfork", "clone", "clone3"):
                child = str(rc)
                tables[child] = table if "CLONE_FILES" in line else dict(table)
                directories[child] = directories[pid] if "CLONE_FS" in line else dict(directories[pid])
            elif op in ("chdir", "fchdir"):
                if op == "chdir":
                    directories[pid]["cwd"] = path_arg(pid, args[0])
                else:
                    directories[pid]["cwd"] = table[number(args[0])]["path"]
            elif op in ("open", "openat", "creat"):
                annotated = re.search(r"=\s+\d+<([^>]+)>", line)
                if annotated:
                    path = posixpath.normpath(annotated.group(1))
                else:
                    path = path_arg(pid, args[1] if op == "openat" else args[0],
                                    args[0] if op == "openat" else "AT_FDCWD")
                flags = line[:re.search(r"\)\s+=", line).start()]
                if "O_EXCL" in flags and "O_CREAT" in flags:
                    files[path] = b""
                if "O_TRUNC" in flags or op == "creat":
                    mutate(path, b"", op)
                append = "O_APPEND" in flags
                table[rc] = {"path": path, "offset": 0, "append": append}
            elif op in ("dup", "dup2", "dup3") or (
                    op == "fcntl" and len(args) > 1 and args[1].startswith("F_DUPFD")):
                old = table.get(number(args[0]))
                if old is not None:
                    table[rc] = old
                else:
                    table.pop(rc, None)
            elif op == "fcntl" and len(args) > 1 and args[1] == "F_SETFL":
                if number(args[0]) in table:
                    table[number(args[0])]["append"] = "O_APPEND" in line
            elif op in ("read", "readv"):
                desc = table.get(number(args[0]))
                if desc and desc["offset"] is not None:
                    desc["offset"] += rc
            elif op == "lseek":
                desc = table.get(number(args[0]))
                if desc:
                    desc["offset"] = rc
            elif op in ("write", "pwrite64", "writev"):
                fd = number(args[0])
                desc = table.get(fd)
                annotated = re.search(r"<([^>]+)>", args[0])
                path = posixpath.normpath(annotated.group(1)) if annotated else desc["path"] if desc else None
                if path is None:
                    continue
                if desc is not None and desc["path"] != path:
                    desc = None
                if op == "writev":
                    literals = re.findall(r'iov_base=("(?:[^"\\]|\\.)*")\s*,\s*iov_len=\d+', args[1])
                    if "..." in re.sub(r'"(?:[^"\\]|\\.)*"', '""', args[1]) or not literals:
                        raise ValueError("truncated vector write")
                    payload = b"".join(cbytes(literal) for literal in literals)[:rc]
                else:
                    if not re.fullmatch(r'"(?:[^"\\]|\\.)*"', args[1]):
                        poison(path, "truncated or unparsed write")
                        continue
                    payload = cbytes(args[1])[:rc]
                if len(payload) != rc:
                    poison(path, "truncated write bytes")
                    continue
                position = number(args[3]) if op == "pwrite64" else desc["offset"] if desc else None
                old = files.get(path)
                if desc and desc["append"]:
                    position = len(old) if old is not None else None
                if old is None or position is None or position + len(payload) > TRACE_STRING_LIMIT:
                    poison(path, "unknown write offset or content")
                else:
                    new = old[:position] + b"\0" * max(0, position - len(old)) + payload + old[position + len(payload):]
                    mutate(path, new, op)
                if desc and op != "pwrite64":
                    desc["offset"] = position + rc if position is not None else None
            elif op in ("rename", "renameat", "renameat2"):
                if op == "rename":
                    source, dest = path_arg(pid, args[0]), path_arg(pid, args[1])
                else:
                    source, dest = path_arg(pid, args[1], args[0]), path_arg(pid, args[3], args[2])
                if op == "renameat2" and "RENAME_EXCHANGE" in args[-1]:
                    old_source, old_dest = files.get(source), files.get(dest)
                    mutate(source, old_dest, op)
                    mutate(dest, old_source, op)
                    for handles in tables.values():
                        for desc in handles.values():
                            if desc["path"] in (source, dest):
                                poison(desc["path"], "open descriptor across exchange rename")
                    continue
                mutate(dest, files.get(source), op)
                if source in targets:
                    mutate(source, b"", op)
                else:
                    files.pop(source, None)
                for handles in tables.values():
                    for desc in handles.values():
                        if desc["path"] == source:
                            desc["path"] = dest
            elif op in ("unlink", "unlinkat"):
                path = path_arg(pid, args[1], args[0]) if op == "unlinkat" else path_arg(pid, args[0])
                mutate(path, b"", op)
            elif op in ("truncate", "ftruncate"):
                path = path_arg(pid, args[0]) if op == "truncate" else table[number(args[0])]["path"]
                size, old = number(args[1]), files.get(path)
                if size == 0:
                    mutate(path, b"", op)
                elif old is not None and size <= TRACE_STRING_LIMIT:
                    mutate(path, old[:size] + b"\0" * max(0, size - len(old)), op)
                else:
                    poison(path, "unknown truncate contents")
            elif op == "close":
                table.pop(number(args[0]), None)
            elif op == "io_uring_setup":
                issues.append("io_uring mutation coverage unavailable")
            elif op in ("mmap", "mmap2"):
                if "PROT_WRITE" in line and "MAP_SHARED" in line:
                    desc = table.get(number(args[4]))
                    if desc:
                        poison(desc["path"], "unsupported shared writable mmap")
            elif op in ("pwritev", "pwritev2", "copy_file_range", "sendfile", "splice"):
                output = args[2] if op in ("copy_file_range", "splice") else args[0]
                annotated = re.search(r"<([^>]+)>", output)
                desc = table.get(number(output))
                path = posixpath.normpath(annotated.group(1)) if annotated else desc["path"] if desc else None
                if path:
                    poison(path, f"unsupported {op}")
        except (ValueError, SyntaxError, UnicodeError, KeyError, AttributeError, IndexError):
            # An undecodable unrelated binary/cache write does not poison the declared scope.
            for annotated in re.findall(r"<(/[^>]+)>", line):
                files[posixpath.normpath(annotated)] = None
            mentioned = [path for path in targets if path in line]
            for path in mentioned:
                poison(path, f"cannot decode {op}")
            if not mentioned and op in ("chdir", "fchdir", "clone", "clone3", "fork", "vfork"):
                issues.append(f"unknown process state after {op}")
    events = []
    for path, old, new, op in changes:
        try:
            events.append({"kind": "edit", "data": {"path": path, "before": old.decode(),
                           "after": new.decode(), "command_baseline": before.get(path),
                           "source": "strace", "syscall": op}})
        except UnicodeError:
            issues.append(f"non-UTF8 target contents: {path}")
    after = {}
    for path in targets:
        try:
            after[path] = files[path].decode() if files.get(path) is not None else None
        except UnicodeError:
            after[path] = None
            issues.append(f"non-UTF8 target contents: {path}")
    return {"events": events, "after": after, "capture_complete": not issues,
            "issues": sorted(set(issues))}


TRACE_STRING_LIMIT = 4 * 1024 * 1024
TRACE_SYSCALLS = ("open,openat,creat,write,pwrite64,writev,pwritev,pwritev2,read,readv,"
                  "lseek,close,dup,dup2,dup3,fcntl,clone,clone3,fork,vfork,chdir,fchdir,"
                  "rename,renameat,renameat2,unlink,unlinkat,mmap,ftruncate,truncate,"
                  "copy_file_range,sendfile,splice,io_uring_setup")
TRACE_FLAGS = ["-f", "-q", "-ttt", "-yy", "-x", "-s", str(TRACE_STRING_LIMIT),
               "-e", "trace=" + TRACE_SYSCALLS, "-e", "raw=read,readv"]


def trace_command(binary: str, output: str, command: str) -> str:
    """Identical flags for availability preflight and the observed command."""
    return shlex.join([binary, *TRACE_FLAGS, "-o", output]) + " " + command


class ProbeAudit:
    """Execution-ordered native hook observer; only the declared mutation scope can be complete."""

    def __init__(self, sb, *, mutation_paths=(), stop=None):
        self.sb = sb
        self.paths = sorted({str(Path(path) if str(path).startswith("/") else Path("/app") / path)
                             for path in mutation_paths})
        if self.paths:
            script = "import os,json; print(json.dumps([os.path.realpath(p) for p in " + repr(self.paths) + "]))"
            resolved = sb.exec("python3 -c " + shlex.quote(script), timeout_s=30)
            if resolved.rc:
                raise SB.SandboxError("cannot canonicalize mutation scope")
            self.paths = json.loads(resolved.stdout)
        self.stop = stop
        self.events: list[dict] = []
        self.pending: dict[str, dict] = {}
        self.issues: list[str] = []
        self.lock = threading.RLock()
        # Include every pre-window reachable/reflog ancestor: checking out an old commit is
        # not creation of a new commit, even when that old hash had no reflog entry yet.
        known = sb.exec("git rev-list --all --reflog", timeout_s=60)
        self.commits = set(known.stdout.splitlines()) if known.rc == 0 else set()
        if known.rc:
            self.issues.append("commit baseline unavailable")
        self.commits |= self._commits()
        self.trace_binary = None
        self.trace_preflight = {"skipped": not bool(self.paths)}
        self.trace_available = False
        if self.paths:
            found = sb.exec(f"if [ -x {CLI_DIR}/strace ]; then printf '%s' {CLI_DIR}/strace; "
                            "else command -v strace; fi", timeout_s=30)
            if found.rc == 0 and found.stdout.strip():
                self.trace_binary = found.stdout.strip()
                checked = sb.exec(trace_command(self.trace_binary, "/dev/null", "true"), timeout_s=30)
                self.trace_available = checked.rc == 0
                self.trace_preflight = {"rc": checked.rc, "stderr": checked.stderr[-2000:],
                                        "binary": self.trace_binary, "flags": TRACE_FLAGS}
            else:
                self.trace_preflight = {"rc": found.rc, "error": "strace unavailable"}
            if not self.trace_available:
                self.issues.append("strace preflight unavailable")
        self.hooks_seen = 0
        self.pre_ids: set[str] = set()
        self.post_ids: set[str] = set()
        self.denied_ids: set[str] = set()

    def resolve_path(self, path: str, cwd: str | None) -> str | None:
        if not path or (not path.startswith("/") and not cwd):
            return None
        script = "import os; print(os.path.realpath(" + repr(path) + "))"
        result = self.sb.exec("python3 -c " + shlex.quote(script), timeout_s=30,
                              workdir=cwd or "/app")
        return result.stdout.strip() if result.rc == 0 else None

    def add(self, kind, **data):
        with self.lock:
            self.events.append({"seq": len(self.events), "kind": kind, "data": data})

    def _commits(self):
        r = self.sb.exec("git reflog --all --format='%H%x00%gD'", timeout_s=60)
        if r.rc:
            self.issues.append("commit reflog unavailable")
            return set()
        return {line.split("\0", 1)[0] for line in r.stdout.splitlines()
                if "\0" in line and not line.split("\0", 1)[1].startswith("refs/stash")}

    def _record_commits(self):
        current = self._commits()
        new = current - self.commits
        head = self.sb.exec("git reflog show HEAD --format=%H", timeout_s=30)
        ordered = list(dict.fromkeys(reversed(head.stdout.splitlines()))) if head.rc == 0 else []
        ordered = [sha for sha in ordered if sha in new]
        if set(ordered) != new:
            self.issues.append("commit ordering unavailable outside HEAD reflog")
            ordered += sorted(new - set(ordered))
        for sha in ordered:
            # These are commits seen in real refs, not synthetic WIP stash objects.
            r = self.sb.exec(f"git show -s --format=%B {shlex.quote(sha)}", timeout_s=30)
            if r.rc:
                self.issues.append(f"commit subject unavailable: {sha}")
            else:
                self.add("commit", sha=sha, message=r.stdout.rstrip("\n"), stash=False)
        self.commits |= current

    def hook(self, payload: dict) -> dict:
        """A hook exception is durable missing evidence and an immediate stop instruction."""
        with self.lock:
            try:
                result = self._hook(payload)
                phase, tid = payload.get("hook_event_name"), str(payload.get("tool_use_id") or "")
                if phase == "PreToolUse":
                    self.pre_ids.add(tid)
                elif phase in ("PostToolUse", "PostToolUseFailure"):
                    self.post_ids.add(tid)
                return result
            except Exception as exc:  # noqa: BLE001 - fail closed and preserve the failure
                self.issues.append(f"audit hook error: {type(exc).__name__}: {exc}")
                if self.stop is not None:
                    self.stop.set()
                return {"continue": False, "stopReason": "native audit evidence collection failed"}

    def _hook(self, payload: dict) -> dict:
        with self.lock:
            self.hooks_seen += 1
            if payload.get("hook_event_name") == "SessionStart":
                return {}
            name = payload.get("tool_name", "")
            args = payload.get("tool_input") or {}
            tid = str(payload.get("tool_use_id") or "")
            phase = payload.get("hook_event_name")
            if not tid:
                self.issues.append("hook missing tool use id")
            if phase == "PreToolUse":
                if self.stop is not None and self.stop.is_set():
                    self.denied_ids.add(tid)
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": "Probe window ended"}}
                if name in ("Bash", "Edit", "Write") and any(
                        row["name"] in ("Bash", "Edit", "Write") for row in self.pending.values()):
                    self.issues.append("overlapping mutation tools")
                before = _read_files(self.sb, self.paths) if self.paths else {}
                edit_path = args.get("file_path")
                if name in ("Edit", "Write") and edit_path and edit_path not in before:
                    before.update(_read_files(self.sb, [edit_path]))
                safe_id = I.sha256(tid)[:20]
                call = {"name": name, "args": args, "before": before,
                        "trace_path": f"{AUDIT_DIR}/{safe_id}.trace",
                        "rc_path": f"{AUDIT_DIR}/{safe_id}.rc",
                        "cwd_path": f"{AUDIT_DIR}/{safe_id}.cwd",
                        "final_cwd_path": f"{AUDIT_DIR}/{safe_id}.final-cwd",
                        "cwd": payload.get("cwd")}
                self.pending[tid] = call
                if name == "Bash":
                    if re.search(r"\bgit\s+(?:reflog\s+(?:expire|delete)|gc|prune)\b",
                                 args.get("command", "")):
                        self.issues.append("commit history may be erased within command")
                    if not self.paths:
                        self.issues.append("no frozen mutation scope")
                    command = args.get("command", "")
                    final_pwd = "pwd -P > " + shlex.quote(call["final_cwd_path"])
                    body = ("pwd -P > " + shlex.quote(call["cwd_path"]) + "; trap "
                            + shlex.quote(final_pwd) + " EXIT; " + command)
                    if self.paths and self.trace_available:
                        traced = trace_command(self.trace_binary, call["trace_path"],
                                               "bash -c " + shlex.quote(body))
                    else:
                        traced = "(\n" + body + "\n)"
                    wrapped = (traced + "; emx_audit_rc=$?; printf '%s' \"$emx_audit_rc\" > "
                               + shlex.quote(call["rc_path"]) + "; "
                               + "if [ -s " + shlex.quote(call["final_cwd_path"]) + " ]; then "
                               + "cd -- \"$(cat " + shlex.quote(call["final_cwd_path"]) + ")\"; fi; "
                               + "sh -c 'exit \"$1\"' emx-audit \"$emx_audit_rc\"")
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": "allow",
                        "updatedInput": {**args, "command": wrapped}}}
                return {}
            call = self.pending.pop(tid, None)
            if call is None:
                self.issues.append(f"post hook without pre hook: {tid}")
                return {}
            name, args = call["name"], call["args"]
            response = payload.get("tool_response") or {}
            response = response if isinstance(response, dict) else {"output": str(response)}
            failed = phase == "PostToolUseFailure"
            if name == "Bash":
                artifacts = _read_files(self.sb, [call["rc_path"], call["cwd_path"], call["final_cwd_path"]])
                raw_rc = artifacts.get(call["rc_path"]) or ""
                rc = int(raw_rc) if raw_rc.strip().isdigit() else None
                cwd = (artifacts.get(call["cwd_path"]) or call.get("cwd") or "").strip() or None
                if self.paths and self.trace_available:
                    r = self.sb.exec("cat " + shlex.quote(call["trace_path"]), timeout_s=60)
                    if r.rc or not r.stdout.strip():
                        self.issues.append("mutation trace missing")
                    else:
                        traced = parse_mutation_trace(r.stdout, call["before"], cwd=cwd or "/app")
                        self.issues += traced["issues"]
                        for event in traced["events"]:
                            self.add(event["kind"], tool_call_id=tid, **event["data"])
                        after = _read_files(self.sb, self.paths)
                        if traced["after"] != after:
                            self.issues.append("trace replay differs from actual files")
                output = response.get("output")
                if output is None:
                    # Keep stream components too: transcript reconciliation replaces this
                    # with the exact tool result that the agent saw, including truncation.
                    output = str(response.get("stdout") or "") + str(response.get("stderr") or "")
                if not artifacts.get(call["final_cwd_path"]):
                    self.issues.append("shell cwd restoration unavailable")
                interrupted = (args.get("run_in_background") or response.get("interrupted")
                               or response.get("timed_out") or response.get("backgroundTaskId"))
                if not raw_rc.strip() and not artifacts.get(call["cwd_path"]) and not interrupted:
                    self.issues.append("bash wrapper artifacts absent")
                completed = rc is not None and not interrupted
                self.add("command", command=args.get("command", ""), output=str(output),
                         exit_code=rc, completed=completed, cwd=cwd, tool_call_id=tid,
                         raw_response=response, is_error=failed)
            elif name in ("Edit", "Write"):
                after = _read_files(self.sb, list(call["before"]))
                for path, original in call["before"].items():
                    if original != after[path]:
                        self.add("edit", path=path, before=original, after=after[path],
                                 tool_call_id=tid, source="tool_snapshot")
                        if original is None or after[path] is None:
                            self.issues.append("unknown edited file contents")
            elif name in ("Grep", "Glob"):
                self.add("search", tool=name, arguments=args, pattern=args.get("pattern", ""), path=args.get("path"),
                         glob=args.get("glob"), output_mode=args.get("output_mode", "files_with_matches"),
                         output=str(response.get("content", response.get("output", ""))),
                         exit_code=1 if failed else 0, completed=not failed,
                         cwd=call.get("cwd"), tool_call_id=tid, raw_response=response,
                         **search_filters(args, response))
            elif name == "Read":
                data = response.get("file") or {}
                start = data.get("startLine")
                count, total = data.get("numLines"), data.get("totalLines")
                end = start + count - 1 if isinstance(start, int) and isinstance(count, int) else None
                self.add("read", path=args.get("file_path", ""), output=data.get("content", ""),
                         start_line=start, end_line=end, complete=start == 1 and count == total
                         and isinstance(total, int), forced=False,
                         exit_code=1 if failed else 0, cwd=call.get("cwd"), tool_call_id=tid,
                         resolved_path=self.resolve_path(args.get("file_path", ""), call.get("cwd")))
            elif name not in (ASK_TOOL, "ask_user", "Grep", "Glob", "TodoWrite", "TaskCreate",
                              "TaskUpdate", "TaskList", "TaskGet"):
                self.issues.append(f"unobserved tool mutation semantics: {name}")
            self._record_commits()
            return {}

    def finish(self, transcript: list[dict], *, captured_asks=(), submission="", terminal_stop=False) -> dict:
        # Transcript is authoritative for the output bytes as seen by the agent.
        outputs = {}
        result_errors = {}
        expected_calls = {}
        for row in transcript:
            for block in (row.get("message") or {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outputs[block.get("tool_use_id")] = _block_text(block.get("content"))
                    result_errors[block.get("tool_use_id")] = bool(block.get("is_error"))
                elif isinstance(block, dict) and block.get("type") == "tool_use":
                    expected_calls[block.get("id")] = block.get("name")
        terminal_stop = terminal_stop or bool(captured_asks)
        unexecuted = sorted(tid for tid, name in expected_calls.items() if terminal_stop and tid not in outputs
                            and not (captured_asks and name in (ASK_TOOL, "ask_user"))
                            and (tid not in self.pre_ids or tid in getattr(self, "denied_ids", set())))
        for tid, name in expected_calls.items():
            if tid in unexecuted:
                continue
            if name in (ASK_TOOL, "ask_user") and captured_asks:
                continue
            if tid not in self.pre_ids or tid not in self.post_ids:
                self.issues.append(f"native hook coverage missing for {tid}")
        for event in self.events:
            if event["kind"] in ("command", "read", "search"):
                tid = event["data"].get("tool_call_id")
                if tid in outputs:
                    event["data"]["output"] = outputs[tid]
                    if event["kind"] == "search":
                        event["data"] = normalize_native_search(event["data"],
                            failed=result_errors.get(tid, False) or not event["data"].get("completed"))
                else:
                    self.issues.append(f"agent-visible output missing for {tid}")
        for row in captured_asks:
            self.add("ask_user", text=row.get("text", ""), source="terminal_capture")
        if submission and not captured_asks:
            self.add("submission", text=submission)
        pending = [tid for tid, row in self.pending.items()
                   if row["name"] not in (ASK_TOOL, "ask_user")]
        if pending:
            self.issues.append("unfinished tool calls")
        part_a_issues = [issue for issue in self.issues if issue.startswith((
            "commit ", "agent-visible output missing", "unfinished tool",
            "post hook without", "hook missing", "audit hook error", "native hook coverage missing",
            "bash wrapper artifacts absent"))]
        return {"schema_version": 1, "events": self.events,
                "part_a_complete": bool(self.hooks_seen) and not part_a_issues,
                "part_a_issues": part_a_issues, "unexecuted_after_stop": unexecuted,
                "capture_complete": bool(self.hooks_seen) and not self.issues,
                "issues": sorted(set(self.issues)), "pending_tool_ids": pending,
                "mutation_paths": self.paths, "trace_available": self.trace_available,
                "trace_preflight": getattr(self, "trace_preflight", None)}


def annotate_delivery_distances(rows: list[dict], boundary_in_session: int,
                                sim_export: dict, boundary_index: int) -> dict:
    """Attach a labelled byte-based token estimate since each actual delivery.

    This counts newly recorded conversational messages after the delivered reply and before
    this compaction. It excludes API request copies, metadata and synthetic summary messages,
    so it never subtracts context sizes across compactions. Four UTF-8 bytes per estimated
    token is an explicit approximation, separate from the measured pre_tokens trigger.
    """
    import math
    exported = json.loads(json.dumps(sim_export))
    planting = exported.get("planting")
    if not planting:
        return exported
    compactions = compact_rows(rows)
    end = compactions[boundary_in_session - 1]
    boundaries = planting.get("boundaries", [])
    boundary = next((row for row in boundaries if row.get("compaction_index") == boundary_index), None)
    if boundary is None:
        return exported
    call_names = {}
    candidates = []
    for index, row in enumerate(rows[:end]):
        blocks = (row.get("message") or {}).get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if block.get("type") == "tool_use":
                call_names[block.get("id")] = block.get("name")
            elif block.get("type") == "tool_result" and call_names.get(
                    block.get("tool_use_id")) in (ASK_TOOL, "ask_user"):
                candidates.append((index, unwrap_ask(_block_text(block.get("content")))))
    asks = {row.get("call_index"): row.get("reply") for row in exported.get("ask_log", [])}
    for rule in boundary.get("obligations", []):
        reply = asks.get(rule.get("delivery_call_index"))
        at = next((index for index, text in candidates if reply and reply == text), None)
        if at is None:
            continue
        seen_ids = set()
        messages = []
        for row in rows[at + 1:end]:
            if not _is_msg(row) or row.get("isCompactSummary"):
                continue
            uid = row.get("uuid")
            if uid is not None and uid in seen_ids:
                continue
            seen_ids.add(uid)
            messages.append(row["message"])
        nbytes = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode())
        rule.update(token_distance=math.ceil(nbytes / 4),
                    token_distance_source="transcript_message_utf8_bytes_div4_estimate",
                    token_distance_bytes=nbytes, token_distance_bytes_per_token=4,
                    delivery_transcript_index=at)
    return exported


def transcript_usage(rows: list[dict]) -> dict | None:
    """Sum actual usage once per new assistant response (latest streamed block wins)."""
    responses = {}
    for row in rows:
        message = row.get("message") or {}
        if row.get("type") == "assistant" and message.get("id") and isinstance(message.get("usage"), dict):
            responses[message["id"]] = message["usage"]
    if not responses:
        return None
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {"source": "native_transcript_response_usage", "responses": len(responses),
            **{key: sum(value.get(key) or 0 for value in responses.values()) for key in keys}}


def search_filters(args: dict, response: dict) -> dict:
    """Preserve native search restrictions instead of assuming an unfiltered repo search."""
    return {"type": args.get("type"), "head_limit": args.get("head_limit"),
            "offset": args.get("offset"), "ignore_case": args.get("-i", False),
            "multiline": args.get("multiline", False),
            "truncated": bool(args.get("head_limit") or args.get("offset")
                              or response.get("truncated")),
            "num_files": response.get("numFiles"), "num_matches": response.get("numMatches")}


def normalize_native_search(data: dict, *, failed: bool = False) -> dict:
    """Normalize only native Grep's exact successful no-match messages; retain raw evidence."""
    out = dict(data)
    out.setdefault("output_mode", "files_with_matches")
    if out.get("tool") != "Grep":
        return out
    if failed:
        out["completed"] = False
        return out
    raw = out.get("output")
    if out.get("completed") and isinstance(raw, str) and raw.strip() in (
            "No files found", "No matches found"):
        out.update(raw_output=raw, output="", exit_code=1,
                   exit_code_source="native_success_no_match_message")
    return out


def resumed_context_check(rows: list[dict], summary_uuid: str, expected_summary: str,
                          prompt: str, *, required=(), forbidden=(), source_rows=()) -> dict:
    """Inspect the first actual resumed API request, never subsequent assistant output.

    A transcript summary alone does not prove what the CLI supplied after re-rendering
    attachments. Missing delta blobs/request evidence therefore fails this check closed.
    """
    cut = next((i for i, row in enumerate(rows) if row.get("uuid") == summary_uuid), None)
    out = {"ok": False, "source": "first_resumed_api_request", "surfaces": None}
    if cut is None:
        return {**out, "reason": "resumed summary absent"}
    observed_summary = _block_text((rows[cut].get("message") or {}).get("content"))
    if observed_summary != expected_summary:
        return {**out, "reason": "resumed summary differs from assigned summary"}
    source_ids = {row.get("uuid") for row in source_rows if row.get("uuid")}
    prompt_index = next((i for i in range(cut + 1, len(rows)) if rows[i].get("type") == "user"
                         and rows[i].get("uuid") not in source_ids
                         and _block_text((rows[i].get("message") or {}).get("content")) == prompt), None)
    if prompt_index is None:
        return {**out, "reason": "new probe message absent from resumed transcript"}
    end = next((i for i in range(prompt_index + 1, len(rows)) if rows[i].get("type") == "assistant"
                and rows[i].get("uuid") not in source_ids), len(rows))
    request_index = next((i for i in range(prompt_index + 1, end) if rows[i].get("type") == "api-request"), None)
    if request_index is None:
        return {**out, "reason": "initial resumed request unrecorded"}
    archive = list(source_rows)
    request = request_messages(archive + rows[:request_index + 1], len(archive) + request_index)
    if request is None:
        return {**out, "reason": "initial resumed request delta incomplete"}
    messages, _info = request_to_messages(request)
    summary_indexes = [i for i, msg in enumerate(messages) if msg.get("role") == "user"
                       and _block_text(msg.get("content")) == expected_summary]
    prompt_indexes = [i for i, msg in enumerate(messages) if msg.get("role") == "user"
                      and _block_text(msg.get("content")) == prompt]
    if len(summary_indexes) != 1 or not prompt_indexes:
        return {**out, "reason": "assigned summary or probe absent from initial request"}
    boundary = next((row for row in reversed(rows[:cut]) if row.get("subtype") == "compact_boundary"), {})
    prior = archive + rows[:cut]
    by_uuid = {row.get("uuid"): i for i, row in enumerate(prior) if row.get("uuid")}
    kept, _sources, _info = to_messages(prior, [by_uuid[uid] for uid in _preserved(boundary)
                                              if uid in by_uuid])
    kept_contents = {I.canon(msg) for msg in kept}
    tail, attachments = [], []
    for i, msg in enumerate(messages):
        if i in summary_indexes or i in prompt_indexes:
            continue
        (tail if I.canon(msg) in kept_contents else attachments).append(msg)
    system = system_prompt(rows, request_index + 1)
    if system != SYSTEM_PLACEHOLDER:
        attachments.append({"role": "system", "content": system})
    surfaces = {"summary": observed_summary, "preserved_tail": tail,
                "attachments": attachments, "probe_message": prompt}
    checked = verify_effective_context(**surfaces, required=required, forbidden=forbidden)
    return {**checked, "source": "first_resumed_api_request", "request_index": request_index,
            "surfaces": surfaces, "assigned_summary_sha256": I.sha256(expected_summary)}


def update_delivery_boundary(server, exported: dict, k: int, pre_tokens: int | None) -> None:
    """Update the frozen clock and its live counterpart under the simulator's ask lock."""
    estimated = next((item for item in exported.get("planting", {}).get("boundaries", [])
                      if item.get("compaction_index") == k), None)
    if estimated is None:
        return
    estimated["pre_tokens"] = pre_tokens
    with server._lock:
        live = getattr(server.sim, "planting", None)
        if live is not None:
            for item in live.boundaries:
                if item.get("compaction_index") == k:
                    item.update(json.loads(json.dumps(estimated)))


def boundary_replay_integrity(rows: list[dict], summary_uuid: str | None,
                              hook_summary: str) -> dict:
    """Known transcript damage disqualifies an early-stop boundary before consuming a chain."""
    issues = []
    ids = [row["uuid"] for row in rows if row.get("uuid")]
    if len(ids) != len(set(ids)):
        issues.append("duplicate_transcript_uuid")
    if any(row.get("parentUuid") and row["parentUuid"] not in ids for row in rows):
        issues.append("broken_transcript_parent_link")
    if any(uid not in ids for row in rows for uid in _preserved(row)):
        issues.append("missing_preserved_message")
    hits = [i for i, row in enumerate(rows) if row.get("uuid") == summary_uuid]
    if not summary_uuid or len(hits) != 1:
        issues.append("summary_missing_or_ambiguous")
    else:
        content = (rows[hits[0]].get("message") or {}).get("content")
        try:
            if not isinstance(content, str) or not hook_summary:
                raise ValueError("summary text unavailable")
            insert_summary_lines(content, hook_summary, ["offline-anchor-check"])
        except ValueError:
            issues.append("hook_summary_mismatch")
        request = first_request_after(rows, hits[0])
        if request is not None and request_messages(rows, request) is None:
            issues.append("incomplete_resumed_request_delta")
    return {"ok": not issues, "issues": sorted(set(issues))}


def resumed_window_rows(rows: list[dict], source_rows: list[dict], prompt: str, *, cut=-1) -> list[dict]:
    """New fork events only: preserved history can occur physically after the summary."""
    source_ids = {row.get("uuid") for row in source_rows if row.get("uuid")}
    source_mids = {(row.get("message") or {}).get("id") for row in source_rows
                   if row.get("type") == "assistant" and (row.get("message") or {}).get("id")}
    source_tools = {block.get("id") for row in source_rows
                    for block in (row.get("message") or {}).get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_use"}
    start = next((i for i in range(cut + 1, len(rows)) if rows[i].get("type") == "user"
                  and rows[i].get("uuid") not in source_ids
                  and _block_text((rows[i].get("message") or {}).get("content")) == prompt), cut + 1)
    result = []
    for row in rows[start:]:
        msg = row.get("message") or {}
        if row.get("uuid") in source_ids or (row.get("type") == "assistant" and msg.get("id") in source_mids):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = [block for block in content if not isinstance(block, dict) or not (
                block.get("type") == "tool_use" and block.get("id") in source_tools
                or block.get("type") == "tool_result" and block.get("tool_use_id") in source_tools)]
            row = {**row, "message": {**msg, "content": content}}
        result.append(row)
    return result
