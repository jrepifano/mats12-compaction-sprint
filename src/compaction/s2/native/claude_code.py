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
DISALLOWED = "WebSearch,WebFetch"
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
                fork: bool = False, prompt: str | None = None) -> str:
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


def read_jsonl(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if line.strip():
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
            state["rate_limit"] = ev
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
             session_cls=N.NativeSession) -> dict:
    """One task in Claude Code (module docstring). Writes the run dir like the pi arm's
    run_base; returns result.json's content. Ends: "submitted" (every step finished cleanly),
    "eligible", "stop_after", "run_cap", "time_cap", "cli_error", "auth_error",
    "boundary_error" (a capture failed: the CLI is stopped), "boundary_missing" (the CLI
    compacted more often than boundaries were captured), "infra_error", "error"."""
    from compaction.s2 import interact_run as IR
    t0 = time.monotonic()
    task = cfg.task
    rd = IR._fresh_dir(cfg.run_dir)
    rj = N.native_run_json(run_key=cfg.run_key, task_dict=task.to_dict(),
                           persona_sha256=I.sha256(task.persona), arm=ARM, model=cfg.cc.model,
                           policy=POLICY, threshold=cfg.cc.threshold(), cli_version=CLI_VERSION,
                           extra={"effort": cfg.cc.effort, "disallowed_tools": DISALLOWED,
                                  "host": cfg.host, "egress_allow": list(N.ALLOW[ARM]),
                                  "steps": list(cfg.steps), "step_timeout_s": cfg.step_timeout_s,
                                  "stop_after": cfg.stop_after, "cpus": cfg.cpus,
                                  "memory_mb": cfg.memory_mb})
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
        return I.UserSim(task.persona, sim_model, runner, purpose=f"sim:{cfg.run_key}")

    ensure_cli_volume(cfg.host, task.info.image)
    ns = session_cls(ARM, task.info.image, sim_factory, host=cfg.host, extra_args=run_args(),
                     cpus=cfg.cpus, memory_mb=cfg.memory_mb, min_free_gb=cfg.min_free_gb,
                     on_boundary=on_boundary, on_cap=on_cap, before_close=drain)
    ns._cc_token = token  # for the secret scan; never written anywhere
    observed = 0
    try:
        with ns:
            ns_holder["ns"] = ns
            res["container"] = ns.sb.name
            res["network"] = ns.egress.network
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
                sd = rd / "steps" / step
                # detached in the container: a dropped ssh transport costs a reconnect
                r = N.detached_exec(ns.sb, cli_command(cfg.cc, session_id=sid),
                                    name=f"{step}-{sid[:8]}",
                                    timeout_s=cfg.step_timeout_s.get(step, 5400),
                                    log_path=sd / "stream.jsonl", env=env,
                                    stdin=task.steps[step].encode(), stop=stop,
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
                    rows = read_transcript(ns.sb, f"{PROJECT}/{sid}.jsonl")
                except SB.SandboxError as e:
                    # missing evidence: the run can't count as complete (astra r2 #4)
                    res.setdefault("transcript_errors", []).append(f"{step}: {e}")
                    end_reason.setdefault("end", "transcript_error")
                    rows = []
                (sd / "transcript.jsonl").write_text(
                    "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
                n_comp = max(len(compact_rows(rows)), len(state.get("compactions") or []))
                observed += n_comp
                if seen.get(sid, 0) < n_comp:  # this session compacted without a capture
                    end_reason.setdefault("end", "boundary_missing")
                bad = _step_ok(r, state)
                res["steps"][step] = {"session_id": sid, "rc": r.rc, "seconds": r.seconds,
                                      "killed": r.killed, "timed_out": r.timed_out,
                                      "api_calls": api_calls(rows), "init": state.get("init"),
                                      "result": state.get("result"),
                                      "compactions": state.get("compactions", []),
                                      "n_compactions": len(compact_rows(rows)),
                                      "rate_limit": state.get("rate_limit"), "failed": bad}
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
            frozen = {"sim_export": server.export(),
                      "sim_rows": [dict(r) for r in server.rows],
                      "sim_probe": IR.sim_probe(server.sim)}
        finally:
            N.signal_pids(sb, sorted(stopped), "CONT")
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


def read_transcript(sb, path: str) -> list[dict]:
    """The transcript, or SandboxError: an unreadable transcript is missing evidence, never an
    empty one (astra r2 #4)."""
    r = sb.exec(f"cat {shlex.quote(path)}", timeout_s=120, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"cannot read {path}: rc={r.rc} {r.stderr[-200:]}")
    return read_jsonl(r.stdout)


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
                 "capture_times": frozen["times"], "hook_utc": frozen["hook_utc"],
                 "post_source": conv.nb.info.get("post_source"), "graded_chain": graded,
                 # the hook carries the model's raw output (<analysis> + <summary>); the
                 # message holds the <summary> body inside Claude Code's wrapper
                 "hook_summary_in_message": bool(hook_summary) and _summary_body(
                     hook_summary) in conv.nb.summary}
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
        bdir = N.write_boundary(rd, k, conv.nb, run_json=rj, snap=snap,
                                sim_export=frozen["sim_export"], sim_rows=frozen["sim_rows"],
                                step=step or "", step_instruction=instr, fingerprint=fp,
                                sim_probe_sha256=frozen["sim_probe"],
                                calls_in_step=api_calls(rows), extra=extra, eligible=gate)
        res["boundaries" if graded else "plan_boundaries"].append(k)
        res.setdefault("compactions", []).append(N.compaction_meta(bdir, step))
        mark("boundary_saved", k=k, step=step, pre_tokens=conv.nb.pre_tokens)
        fields = json.loads((bdir / "boundary" / "boundary.json").read_text())
        if fields.get("eligible"):
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


def insert_in_summary(message: str, body: str, block: str) -> str:
    """`block` inserted right after the summary body inside Claude Code's summary message,
    before its closing paragraphs (a line appended after those reads as outside the summary:
    smoke 2's fork, 24 Oct). `body` is the hook payload's <summary> text."""
    b = _summary_body(body, limit=None)
    tail = b[-300:]
    at = message.rfind(tail) if b else -1
    if at < 0:
        raise ValueError("summary body not found in the summary message")
    end = at + len(tail)
    return message[:end] + "\n\n" + block.strip() + message[end:]


def fork(bdir: Path, out_dir: Path, *, new_summary: str, prompt: str, sim_model, token: str,
         persona: str, cfg: CCConfig | None = None, host: str | None = "ssh://rig",
         timeout_s: int = 1800, check: bool = True, session_cls=N.NativeSession) -> dict:
    """Resume boundary `bdir` (a b<k> dir of a Claude Code run) from its kept image with the
    summary replaced by `new_summary`, as a forked session given `prompt`. Refuses
    (ForkMismatch) when the restored workspace or simulator differ from the boundary's, unless
    check=False. Writes out_dir/{fork.json, stream.jsonl, transcript.jsonl,
    input_transcript.jsonl}; returns fork.json."""
    bdir, out_dir = Path(bdir), Path(out_dir)
    b = json.loads((bdir / "boundary" / "boundary.json").read_text())
    rows = read_jsonl((bdir / "native" / "transcript.jsonl").read_text())
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
        return I.UserSim.restore(exported, persona, sim_model, runner, purpose="sim:fork")

    stop = threading.Event()
    why: dict = {}

    def on_cap(e: BaseException) -> None:
        why.setdefault("end", "run_cap")
        stop.set()

    ns = session_cls(ARM, b["tag"], sim_factory, host=host, setup_git=False,
                     extra_args=run_args(),
                     on_boundary=lambda p: {"ok": True, "ignored": "fork"}, on_cap=on_cap)
    try:
        with ns:
            ns.server.step = b.get("stage") or GRADED_STEP
            res["fork_check"] = fork_check(ns, b)  # the pi arm's check (astra r1 #7)
            if not res["fork_check"]["ok"] and check:
                raise ForkMismatch(json.dumps(res["fork_check"]))
            if b.get("ineligible_reason") and check:
                # background work or services would carry on in the base run but not here
                raise ForkMismatch(f"not a fork source: {b['ineligible_reason']} "
                                   + json.dumps(b.get("processes"))[:500])
            install_config(ns.sb, ns.server)  # a new token and server for the fork
            ns.sb.put_file(body.encode(), f"{PROJECT}/{new_sid}.jsonl")
            state: dict = {}
            env = cli_env(cfg, ns.egress, ns.server, token)
            r = N.detached_exec(ns.sb, cli_command(cfg, resume=new_sid, fork=True,
                                                   prompt=prompt),
                                name=f"fork-{new_sid[:8]}", timeout_s=timeout_s,
                                log_path=out_dir / "stream.jsonl", env=env, stop=stop,
                                on_line=_stream_watch(state, stop))
            if r.killed:
                stop_cli(ns.sb)
            sid = (state.get("init") or {}).get("session_id")
            if sid:
                (out_dir / "transcript.jsonl").write_text(
                    ns.sb.exec(f"cat {PROJECT}/{sid}.jsonl", timeout_s=120, workdir="/").stdout)
            # a fork's only stops are the cap and the auth watch: neither is a completion
            # (astra r3 #2)
            bad = (state.get("auth_error") or why.get("end")
                   or ("stopped" if r.killed else None) or _step_ok(r, state))
            res.update(rc=r.rc, seconds=r.seconds, killed=r.killed, init=state.get("init"),
                       result=state.get("result"), forked_session=sid,
                       sim_rows=ns.server.rows, auth_error=state.get("auth_error"),
                       run_key=rj.get("run_key"), outcome=bad or "ok")
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
