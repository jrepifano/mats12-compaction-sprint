"""Experiment 1 (stage S1) Part B: controlled summarization-policy replays (U4).

Frozen interface in docs/sprint/specs/S1.md ("U4 src/compaction/s1_replay.py", D4, D5,
"ReplayRecord"). Part B re-compacts identical pre-compaction histories with six harnesses'
summarizer policies, holding the model fixed (Claude Haiku 5.5 on the Max plan), so only the
summarizer differs. Five arms are prompt + keep-rule replays (`ARM_BUILDERS`); the sixth,
`cc_native`, runs Claude Code's own `/compact` on a transcript written from the record
(`native_compact`).

Every replay request is one call (pi: one or two) with a free-text answer (`Req.schema=None`).
Per D5 the history is serialised as text inside one user message, framed as pi frames it
("<conversation>\\n...\\n</conversation>\\n\\n" + the arm's prompt). pi and OpenCode serialise
the history with their own serialisers (ported below), since that is what those harnesses send;
Claude Code, Codex and Antigravity send real message lists, so their arms use U1's
`s1_data.render` (with each harness's own tool-output cut applied to the messages first).

Prompt sources. Prompts that live in gitignored files are read at runtime only and never
written into the repo; `prompt_sources()` lists each prompt file's path, sha256 and version,
and `run_replays` adds that provenance per row fingerprint to
data/raw/sprint/s1/replays/prompts.json ({"by_fingerprint": {...}}; keys are never overwritten).

Resume and integrity. Every ReplayRecord carries a `fingerprint`: sha256 over the request as
sent (every call's system and user text, i.e. the history bytes), the arm's `policy_version`
(a revision plus a hash of the constants its keep rules depend on), the prompt sha256, the
model, max_output_tokens and the backend's generation settings, and the kept messages. A rerun
skips a record only when a successful row with the same (id, fingerprint) exists. Part B holds
the model fixed: a backend not pinned to MODEL is refused before dispatch, and a response whose
model id is missing or different makes the row `error="model_mismatch"`.

- cc_prompt: Claude Code's compaction request as captured by the bridge filter in
  data/raw/sprint/spike_inspect_swe/captures/cc-deepseek-compact1-request.json (CLI 2.1.304,
  docs/sprint/prep/f-inspect-swe-spike.md section 3). System = the system messages before the
  first conversation message, minus the billing header (Claude Code reuses its agent system
  prompt for the compaction call); the per-session "# Environment" system message that follows
  the first user message is not included (it names the container and the DeepSeek model).
  User = rendered history + the capture's final user message (the TEXT ONLY preamble, the
  nine-section instruction and the reminder). Summary = Claude Code's formatting of the answer:
  the <analysis> block dropped, <summary>...</summary> replaced by "Summary:\\n" + its content
  (the same form the native arm's summary message carries). kept_messages = []: Claude Code's
  real preserved tail is studied through the native arm. Max output 20,000 (Claude Code caps
  the compaction call at min(model max output, 20,000): papers/agent_swarms/"Claude Code
  compaction (docs and v2.1.300 binary, 2026-10-14).md").
- codex_local: Codex CLI's local compaction (openai/codex ac9db7d `core/src/compact.rs`), ported
  from P49 (probes/P49-long-context-compaction/p49/compact.py `_codex`,
  `codex_kept_user_messages`, `codex_truncate_tokens`; prompt files and pins in
  probes/P49-long-context-compaction/prompts/SOURCES.md). Tool outputs are middle-truncated at
  12,000 tokens (bytes/4) before rendering; overflow drops the oldest item and its call/result
  counterparts (compact.rs L359) until the request fits Haiku's window. Prompt =
  codex_prompt.md. System = Codex's base instructions, the first system message of the bridge
  capture of a real Codex CLI 0.163.0 compaction request
  (data/raw/sprint/spike_inspect_swe/captures/codex-luna2-compact1-request.json; Codex sends
  its agent instructions with the compaction call; the session-specific developer messages
  after it are left out). No fallback: without the capture the arm refuses to build (D7).
  kept_messages = the user requests (see below), newest first within 20,000 tokens
  (bytes/4, rounded up; the message that crosses the budget is middle-truncated to what
  remains), restored to original order. Summary = the answer as-is. Max output 16,384 (P49).
- pi: pi-mono b06c1f2 (byte-identical prompts to pi 1.3.0, docs/sprint/prep/k-pi-opencode-
  spike.md), ported from P49 `_pi`, `pi_find_cut`, `pi_serialize`: walk back from the newest
  message summing chars/4 until >= 20,000, cut at the nearest user/assistant message at or
  after it; if the cut is not at a user message the turn is split and pi makes two calls (the
  history before the turn with SUMMARIZATION_PROMPT, max 13,107; the turn prefix with
  TURN_PREFIX_SUMMARIZATION_PROMPT, max 8,192), merged as pi merges them. Tool results are cut
  to their first 2,000 characters. kept_messages = the tail from the cut on. Summary = as-is.
- opencode: OpenCode 1.19.3 (`packages/opencode/src/session/compaction.ts`, the v1 path the
  CLI runs; `packages/core/src/session/compaction.ts` `buildPrompt` and SUMMARY_TEMPLATE;
  system = `packages/opencode/src/agent/prompt/compaction.txt`). Serialisation as its
  `serialize` ([User]/[Assistant]/[Assistant tool call]/[Tool result], each tool result cut to
  2,000 characters + "\\n[truncated]"). Tail (`select` / `splitTurn`): turns start at user
  messages; whole turns are kept newest first while their estimate fits
  preserve_recent_tokens = min(15,000, max(2,000, 25% of usable)) = 15,000 for Haiku (usable =
  1,000,000 - min(128,000, 32,000)); the turn that does not fit is split at the earliest
  message whose suffix fits; nothing is kept when the tail would start at message 0. The
  estimate is round(len(JSON.stringify(model messages)) / 4) over an AI-SDK-shaped rendering
  of the messages (an approximation of `toModelMessages`). Summary = the answer, trimmed. Max
  output 32,000 (min(model output, OUTPUT_TOKEN_MAX)).
- antigravity: agy 1.4.3. The summarizer prompt is the text in the agy binary from "You have
  been working on the task described above" through "Wrap your response in <summary></summary>
  tags." (2,288 bytes, sha256 b40fec18...; a different sha only warns). kept_messages = up to
  the 10 most recent user requests, verbatim (sprint-plan "Up to 10 user requests verbatim",
  inferred from agy's built-in config). `agy_checkpoint` rebuilds the CHECKPOINT text agy shows
  after a compaction, matched to the real checkpoint in
  data/raw/sprint/agy/20261020-validate-transcript_full.jsonl: "# Resuming from a compaction",
  the "You are continuing work ..." line, "# User Requests" + its intro line (the binary also
  has a "most recent" intro, used here when the list is capped: an inference), the requests as a
  numbered list ("1. <text>"), then "<summary>\\n...\\n</summary>"; the header strings are read
  from the binary. The real checkpoint lists the requests as plain numbered items (not
  <USER_REQUEST> blocks; agy strips those, `agy.user_request`) and ends with a "# Conversation
  Logs" pointer to transcript files, which a replay has no files for and leaves out. Summary =
  the content of the answer's <summary> tags. Assumptions, not measured: the system prompt is
  NEUTRAL_SYSTEM (agy's compaction call was not captured) and max output is 16,384 (Codex's).

The history every arm summarises (and the native transcript holds) is `history_rec(rec)`:
source_messages followed by the messages Claude Code kept verbatim before the boundary
(Jacob's sidecar `preserved_messages`; empty for public records), and every keep rule applies
to that combined list. Each ReplayRecord records `n_preserved_in_history`.

User requests (Codex's carry-over, agy's list), the primary policy (integrator decision after
review r1, logs/reviews/20261020-s1-u4-r1.md): only messages the user typed. Rationale: the
sprint's chat channel puts rules in real user turns, while Claude Code histories also carry
user-role scaffolding (task notifications, local-command echoes, skill bodies, scheduled loop
prompts) that a Codex or agy session would not hold as user requests. Provenance is
structural where the record carries it: for Jacob's records, compaction.sessions built
`user_turns` from Claude Code's own flags (origin kind "human", not isMeta, not a compact
summary, not an interrupt marker), and `structural_user_mask` maps them back to messages in
order (all 713 turns of the 50 records map). Elsewhere (public records, the appended
preserved messages, a record whose turns do not map) the prefix fallback `is_user_request`
applies (`HARNESS_USER_PREFIXES`). Because the summarizer sees the whole history either way,
codex_local and antigravity rows also store the unfiltered P49 keep set (every role-"user"
message except earlier Codex summaries) as `kept_messages_unfiltered`, with `n_filtered_out`,
at no extra call, so availability can be scored under both as a sensitivity check.

Messages with role "system" (Claude Code's system-reminders) are rendered as user text by the
pi and OpenCode serialisers and written as user text in the native transcript; they never
count as user requests (Codex, agy) or turn starts (pi, OpenCode).
"""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid as uuidlib
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from compaction.s1_types import Req, anthropic_only

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPLAY_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "s1" / "replays"
CC_CAPTURE = (
    REPO_ROOT
    / "data"
    / "raw"
    / "sprint"
    / "spike_inspect_swe"
    / "captures"
    / "cc-deepseek-compact1-request.json"
)
P49_PROMPTS = REPO_ROOT / "probes" / "P49-long-context-compaction" / "prompts"
CODEX_CAPTURE = (
    REPO_ROOT
    / "data"
    / "raw"
    / "sprint"
    / "spike_inspect_swe"
    / "captures"
    / "codex-luna2-compact1-request.json"
)
OPENCODE_SRC = REPO_ROOT / "data" / "raw" / "sprint" / "spike_pi_opencode" / "src" / "opencode"
AGY_BINARY: str | None = None  # None: realpath of `which agy`
AGY_EXPECTED_SHA256 = "b40fec18297b98a6a7059327eb201486f53c0f6a28a93dcd9d2c51556ae40426"
AGY_PROMPT_START = b"You have been working on the task described above"
AGY_PROMPT_END = b"Wrap your response in <summary></summary> tags."

MODEL = "claude-haiku-5-5"
CONTEXT_TOKENS = 1_000_000  # Haiku 5.5 (smoke: modelUsage contextWindow)
BYTES_PER_TOKEN = 2.3  # spec D3 (conservative for Haiku)
HAIKU_MEASURED_BYTES_PER_TOKEN = 2.46  # spec D3, measured on the longest history
FIT_OVERHEAD_TOKENS = 2_000

NEUTRAL_SYSTEM = "You are a helpful assistant."
SIZE_LENGTH_LINE = "Keep the summary to at most 2,000 words."
SIZE_CUTS = (256_000, 512_000)

REPLAY_ARMS = ("cc_prompt", "codex_local", "pi", "antigravity", "opencode")
NATIVE_ARM = "cc_native"

MAX_OUT = {
    "cc_prompt": 20_000,
    "codex_local": 16_384,
    "pi": 13_107,
    "antigravity": 16_384,
    "opencode": 32_000,
}

# Codex (P49 contracts.py / compact.py)
CODEX_KEEP_USER_TOKENS = 20_000
CODEX_TOOL_OUTPUT_TOKENS = 12_000  # 10,000 x the 1.2 serialisation allowance
# pi (P49 contracts.py / compact.py)
PI_KEEP_RECENT_TOKENS = 20_000
PI_TOOL_RESULT_CHARS = 2_000
PI_TURN_PREFIX_MAX_TOKENS = 8_192
# OpenCode (session/compaction.ts, overflow.ts, provider/transform.ts)
OPENCODE_TOOL_OUTPUT_MAX_CHARS = 2_000
OPENCODE_PRESERVE_RECENT_TOKENS = min(15_000, max(2_000, (1_000_000 - min(128_000, 32_000)) // 4))
# Antigravity
AGY_MAX_REQUESTS = 10

# User-role messages that Claude Code injects rather than the user typing them (local-command
# echoes, background-task notifications, skill bodies, other sessions' hand-backs, side-agent
# notes, safety-classifier notices, interrupt markers, an earlier compaction summary). Codex
# and agy keep only the user's own requests (Codex skips its environment/instructions wrappers
# and earlier summaries; agy lists USER_INPUT steps), so these never count as user requests.
HARNESS_USER_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<task-notification>",
    "[Request interrupted by user",
    "This session is being continued from a previous conversation",
    "Your response above was stopped by a safety classifier",
    "Base directory for this skill:",
    "Another Claude session sent a message:",
    "Here is a note offered by a side agent:",
)


def is_user_request(m: dict) -> bool:
    """Prefix fallback: role "user" and not Claude Code scaffolding (HARNESS_USER_PREFIXES)."""
    if m.get("role") != "user":
        return False
    t = (m.get("text") or "").lstrip()
    return bool(t) and not t.startswith(HARNESS_USER_PREFIXES)


# compaction.sessions._REMINDER: user_turns are the human texts with system-reminders removed
_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.DOTALL)


def structural_user_mask(rec: dict, src: list[dict] | None = None) -> list[bool] | None:
    """Which source_messages Claude Code itself marked as typed by the user, or None.

    compaction.sessions builds Jacob's `user_turns` from Claude Code's own provenance flags
    (`origin.kind == "human"` / `turnOrigin`, not `isMeta`, not a compact summary, not an
    interrupt marker; `_is_human_text`), as the reminder-stripped texts in order. Matching them
    in order to the role-"user" messages recovers the flag per message. None when the record
    has no such provenance (not a Jacob record) or the match is incomplete."""
    turns = rec.get("user_turns")
    if rec.get("source") != "jacob" or not isinstance(turns, list):
        return None
    src = list(rec.get("source_messages") or []) if src is None else src
    mask, j = [False] * len(src), 0
    for i, m in enumerate(src):
        if m.get("role") != "user" or j >= len(turns):
            continue
        if _REMINDER.sub("", m.get("text") or "").strip() == turns[j]:
            mask[i] = True
            j += 1
    return mask if j == len(turns) else None


def user_request_mask(rec: dict) -> tuple[list[bool], str]:
    """Per history message (history_rec): is it a user request under the primary policy?

    Structural provenance where the record carries it (Jacob's source messages), the prefix
    fallback elsewhere (public records; the preserved messages appended after the source).
    Returns (mask, provenance) with provenance "structural", "structural+prefix" or "prefix"."""
    h = history_rec(rec)
    msgs = h["source_messages"]
    n_src = len(msgs) - h["s1_n_preserved"]
    st = structural_user_mask(h, msgs[:n_src])
    if st is not None:
        tail = [is_user_request(m) for m in msgs[n_src:]]
        return st + tail, "structural" if not tail else "structural+prefix"
    return [is_user_request(m) for m in msgs], "prefix"


def unfiltered_user_mask(messages: list[dict]) -> list[bool]:
    """P49's rule: every role-"user" message (earlier Codex summaries are dropped later)."""
    return [m.get("role") == "user" for m in messages]


CONVERSATION_OPEN = "<conversation>\n"
CONVERSATION_CLOSE = "\n</conversation>\n\n"


def _sha(s: str | bytes) -> str:
    return hashlib.sha256(s.encode("utf-8") if isinstance(s, str) else s).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _frame(history: str, prompt: str) -> str:
    """D5: the history as text in one user message, pi's frame, then the arm's prompt."""
    return CONVERSATION_OPEN + history + CONVERSATION_CLOSE + prompt


# ------------------------------------------------------------------------------------------
# Requests
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    label: str
    system: str
    user: str
    max_output_tokens: int


@dataclass
class ReplayRequest:
    """One arm's summarizer request(s) for one record, and what it keeps verbatim.

    `system`/`user` are the first (usually only) call; pi's split turn adds `extra_calls`."""

    system: str
    user: str
    kept_messages: list[dict]
    prompt_sha256: str
    arm: str = ""
    max_output_tokens: int = 16_384
    label: str = "summary"
    extra_calls: tuple[Call, ...] = ()
    info: dict = field(default_factory=dict)
    kept_messages_unfiltered: list[dict] | None = None  # codex_local, antigravity: P49's set
    n_filtered_out: int | None = None  # user-role messages the primary filter left out

    @property
    def calls(self) -> list[Call]:
        return [Call(self.label, self.system, self.user, self.max_output_tokens), *self.extra_calls]

    @property
    def request_sha256(self) -> str:
        return _sha(_canon([c.__dict__ for c in self.calls]))


# ------------------------------------------------------------------------------------------
# Prompt loading (runtime only)
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptSet:
    texts: dict[str, str]
    sha256: str  # sha256 over "name=sha256(text)" lines (P49 compact.prompt_sha256 convention)
    sources: list[dict]  # [{"name", "path", "sha256", "version"}]


def _prompt_set(texts: dict[str, str], paths: dict[str, str], version: str) -> PromptSet:
    lines = [f"{n}={_sha(t)}" for n, t in texts.items()]
    return PromptSet(
        texts=texts,
        sha256=_sha("\n".join(lines)),
        sources=[
            {"name": n, "path": paths[n], "sha256": _sha(t), "version": version}
            for n, t in texts.items()
        ],
    )


def _rel(p: Path | str) -> str:
    p = Path(p)
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


@functools.cache
def _cc_prompts(path: str) -> PromptSet:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    msgs = d["messages"]

    def text(m: dict) -> str:
        c = m["content"]
        return c if isinstance(c, str) else "".join(b.get("text", "") for b in c)

    system_blocks = []
    for m in msgs:
        if m["role"] != "system":
            break
        t = text(m)
        if t.startswith("x-anthropic-billing-header"):
            continue
        system_blocks.append(t)
    last = msgs[-1]
    if last["role"] != "user":
        raise ValueError(f"{path}: the last message is not the compaction instruction")
    instruction = text(last)
    if not system_blocks or not instruction:
        raise ValueError(f"{path}: no system prompt or instruction found")
    m = re.search(r"cc_version=([\w.]+)", text(msgs[0])) if msgs else None
    version = f"claude-code {m.group(1)}" if m else "claude-code unknown"
    src = _rel(path)
    return _prompt_set(
        {"cc_system": "\n\n".join(system_blocks), "cc_instruction": instruction},
        {"cc_system": f"{src}#messages[system]", "cc_instruction": f"{src}#messages[-1]"},
        version,
    )


P49_FILES = {
    "codex": ("codex_prompt.md", "codex_summary_prefix.md"),
    "pi": (
        "pi_system_prompt.txt",
        "pi_summarization_prompt.txt",
        "pi_turn_prefix_prompt.txt",
        "pi_wrapper.md",
    ),
}
P49_VERSIONS = {
    "codex": "openai/codex ac9db7d8 (P49 SOURCES.md)",
    "pi": "badlogic/pi-mono b06c1f2 = pi 1.3.0 (P49 SOURCES.md; spike k)",
}


@functools.cache
def _p49_prompts(directory: str, method: str) -> PromptSet:
    d = Path(directory)
    texts = {n: (d / n).read_bytes().decode("utf-8") for n in P49_FILES[method]}
    return _prompt_set(texts, {n: _rel(d / n) for n in texts}, P49_VERSIONS[method])


class CodexCaptureError(RuntimeError):
    """The Codex capture cannot supply Codex's base instructions: codex_local cannot run."""


@functools.cache
def _codex_prompts(p49_dir: str, capture: str) -> PromptSet:
    """P49's Codex prompt files plus Codex's base instructions as the system prompt: the first
    system message of the bridge capture of a real Codex CLI compaction request (0.163.0, spike
    f). Codex sends its agent instructions with the compaction call; the following developer
    messages there (skills, sandbox permissions, multi-agent role) are session-specific and
    left out. There is no fallback (D7): a missing or malformed capture, or one without base
    instructions, raises CodexCaptureError, so nothing is dispatched."""
    base = _p49_prompts(p49_dir, "codex")
    path, version = f"{_rel(capture)}#messages[0]", "codex-cli 0.163.0 (spike f)"
    try:
        msgs = json.loads(Path(capture).read_text(encoding="utf-8"))["messages"]
        m = msgs[0]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        raise CodexCaptureError(f"Codex capture {capture} unreadable: {type(e).__name__}: {e}")
    system = None
    if isinstance(m, dict) and m.get("role") == "system":
        c = m.get("content")
        if isinstance(c, str):
            system = c
        elif isinstance(c, list):
            system = "".join(b.get("text", "") for b in c if isinstance(b, dict))
    if not system or not system.strip():
        raise CodexCaptureError(
            f"Codex capture {capture}: messages[0] is not a system message with base instructions"
        )
    texts = {**base.texts, "codex_system": system}
    lines = [f"{n}={_sha(t)}" for n, t in texts.items()]
    sources = [
        *base.sources,
        {"name": "codex_system", "path": path, "sha256": _sha(system), "version": version},
    ]
    return PromptSet(texts=texts, sha256=_sha("\n".join(lines)), sources=sources)


def _js_unescape(s: str) -> str:
    """Escapes of a JS template literal or string (the subset OpenCode's prompts use)."""
    out, i = [], 0
    esc = {"n": "\n", "t": "\t", "`": "`", "$": "$", "\\": "\\", '"': '"', "'": "'"}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] in esc:
            out.append(esc[s[i + 1]])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


@functools.cache
def _opencode_prompts(src_dir: str) -> PromptSet:
    root = Path(src_dir)
    sys_path = root / "packages/opencode/src/agent/prompt/compaction.txt"
    core = root / "packages/core/src/session/compaction.ts"
    ts = core.read_text(encoding="utf-8")
    m = re.search(r"const SUMMARY_TEMPLATE = `(.*?)`\n", ts, re.DOTALL)
    if not m or "${" in m.group(1):
        raise ValueError(f"{core}: SUMMARY_TEMPLATE not found or interpolated")
    template = _js_unescape(m.group(1))
    m = re.search(r"const conversation = `(.*?)`\n", ts, re.DOTALL)
    parts = re.split(r"\$\{[^}]*\}", m.group(1)) if m else []
    if len(parts) != 2:
        raise ValueError(f"{core}: buildPrompt conversation frame not found")
    frame_open, frame_close = (_js_unescape(p) for p in parts)
    m = re.search(
        r'if \(!input\.previousSummary\)\s*return \[\s*conversation,\s*"((?:[^"\\]|\\.)*)",'
        r'\s*SUMMARY_TEMPLATE,\s*\]\.join\("((?:[^"\\]|\\.)*)"\)',
        ts,
    )
    if not m:
        raise ValueError(f"{core}: buildPrompt first-compaction instruction not found")
    instruction, sep = _js_unescape(m.group(1)), _js_unescape(m.group(2))
    try:
        pkg = json.loads((root / "packages/opencode/package.json").read_text())
        version = f"opencode {pkg.get('version', 'unknown')}"
    except (OSError, json.JSONDecodeError):
        version = "opencode unknown"
    texts = {
        "opencode_system": sys_path.read_bytes().decode("utf-8"),
        "opencode_frame_open": frame_open,
        "opencode_frame_close": frame_close,
        "opencode_instruction": instruction,
        "opencode_join": sep,
        "opencode_template": template,
    }
    cr = _rel(core)
    paths = {
        "opencode_system": _rel(sys_path),
        "opencode_frame_open": f"{cr}#buildPrompt",
        "opencode_frame_close": f"{cr}#buildPrompt",
        "opencode_instruction": f"{cr}#buildPrompt",
        "opencode_join": f"{cr}#buildPrompt",
        "opencode_template": f"{cr}#SUMMARY_TEMPLATE",
    }
    return _prompt_set(texts, paths, version)


def agy_binary() -> str:
    if AGY_BINARY:
        return AGY_BINARY
    w = shutil.which("agy")
    if not w:
        raise FileNotFoundError("agy is not on PATH (needed for the Antigravity prompt)")
    return os.path.realpath(w)


def _between(b: bytes, start: bytes, end: bytes, within: int = 20_000) -> bytes:
    """The first span start..end (end included) with end within `within` bytes of start."""
    i = b.find(start)
    while i != -1:
        j = b.find(end, i, i + within)
        if j != -1:
            return b[i : j + len(end)]
        i = b.find(start, i + 1)
    raise ValueError(f"span {start[:30]!r}..{end[:30]!r} not found")


def _agy_version(path: str) -> str:
    try:
        cp = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
        v = cp.stdout.strip().splitlines()
        return f"agy {v[0]}" if cp.returncode == 0 and v else "agy unknown"
    except (OSError, subprocess.SubprocessError):
        return "agy unknown"


@functools.cache
def _agy_prompts(path: str) -> PromptSet:
    b = Path(path).read_bytes()
    prompt = _between(b, AGY_PROMPT_START, AGY_PROMPT_END).decode("utf-8")
    if _sha(prompt) != AGY_EXPECTED_SHA256:
        warnings.warn(
            f"agy summarizer prompt sha256 {_sha(prompt)[:12]} differs from the "
            f"agy 1.4.3 one ({AGY_EXPECTED_SHA256[:12]})",
            stacklevel=2,
        )
    head = b"# Resuming from a compaction"
    if b.find(head) == -1:
        raise ValueError(f"{path}: CHECKPOINT header not found")
    resume = _between(b, b"You are continuing work on the task described above", b"below:", 400)
    # two intros follow "# User Requests\n" in the binary: one for a capped ("most recent")
    # list, one for a complete list (the real checkpoint, 2 requests, used the latter)
    intros = []
    i = b.find(b"# User Requests\n")
    while i != -1:
        j = b.find(b"order:", i, i + 400)
        if j != -1:
            intros.append(b[i : j + len(b"order:")])
        i = b.find(b"# User Requests\n", i + 1)
    capped = [x for x in intros if b"most recent" in x]
    full = [x for x in intros if b"most recent" not in x]
    if len(capped) != 1 or len(full) != 1:
        raise ValueError(f"{path}: expected one capped and one full user-requests intro")
    texts = {
        "agy_prompt": prompt,
        "agy_checkpoint_head": head.decode(),
        "agy_checkpoint_resume": resume.decode(),
        "agy_checkpoint_requests": full[0].decode(),
        "agy_checkpoint_requests_capped": capped[0].decode(),
    }
    return _prompt_set(texts, {n: f"{path}#strings" for n in texts}, _agy_version(path))


def prompts_for(arm: str) -> PromptSet:
    if arm == "cc_prompt":
        return _cc_prompts(str(CC_CAPTURE))
    if arm == "codex_local":
        return _codex_prompts(str(P49_PROMPTS), str(CODEX_CAPTURE))
    if arm == "pi":
        return _p49_prompts(str(P49_PROMPTS), "pi")
    if arm == "opencode":
        return _opencode_prompts(str(OPENCODE_SRC))
    if arm == "antigravity":
        return _agy_prompts(agy_binary())
    raise ValueError(f"unknown replay arm {arm!r}")


def clear_prompt_cache() -> None:
    for f in (_cc_prompts, _p49_prompts, _codex_prompts, _opencode_prompts, _agy_prompts):
        f.cache_clear()


def prompt_sources(arms: tuple[str, ...] = REPLAY_ARMS) -> dict:
    out = {}
    for a in arms:
        ps = prompts_for(a)
        out[a] = {"prompt_sha256": ps.sha256, "files": ps.sources}
    return out


# ------------------------------------------------------------------------------------------
# Rendering (U1), injectable for tests
# ------------------------------------------------------------------------------------------

_RENDER: Callable | None = None


PRESERVED_FIELDS = ("preserved_messages", "preserved")  # audit_logs.PRESERVED_FIELDS


def preserved_messages(rec: dict) -> list[dict]:
    """Messages Claude Code kept verbatim before the boundary (audit_logs._preserved: a record
    field, else Jacob's sidecar `meta.preserved_messages`); [] when none are recorded."""
    for k in PRESERVED_FIELDS:
        if isinstance(rec.get(k), list):
            return [m for m in rec[k] if isinstance(m, dict)]
    meta = rec.get("meta") or {}
    pm = meta.get("preserved_messages") if isinstance(meta, dict) else None
    return [m for m in pm if isinstance(m, dict)] if isinstance(pm, list) else []


def history_rec(rec: dict) -> dict:
    """The record whose source_messages are the history every arm summarises (spec, "The
    history every pass and replay sees"): source_messages + preserved_messages, in that order.
    The copy's preserved fields are emptied so a renderer cannot add them twice. Idempotent."""
    if rec.get("s1_combined"):
        return rec
    src = list(rec.get("source_messages") or [])
    combined = _history_messages(rec)
    if combined is None:
        combined = src + preserved_messages(rec)
    h = copy.copy(rec)
    h["source_messages"] = list(combined)
    for k in PRESERVED_FIELDS:
        h.pop(k, None)
    if isinstance(rec.get("meta"), dict):
        h["meta"] = {**rec["meta"], "preserved_messages": []}
    h["s1_combined"] = True
    h["s1_n_preserved"] = len(combined) - len(src)
    return h


def _history_messages(rec: dict) -> list[dict] | None:
    """U1's `s1_data.history_messages(rec)` (source + preserved) when it exists, else None.
    The combined copy is rendered with its preserved fields emptied, so the renderer adds no
    KEPT_HEADER: to a replay summarizer the preserved messages are ordinary history (the
    header would tell it they stay in context after the compaction). Indices are unchanged."""
    try:
        from compaction import s1_data
    except ImportError:
        return None
    fn = getattr(s1_data, "history_messages", None)
    return list(fn(rec)) if callable(fn) else None


def _render(rec: dict):
    rec = history_rec(rec)
    if _RENDER is not None:
        return _RENDER(rec)
    from compaction import s1_data

    return s1_data.render(rec)


def _blocks(rec: dict) -> tuple[Any, dict[int, str]]:
    r = _render(rec)
    return r, {i: r.text[s:e] for i, (s, e) in r.spans.items()}


def _fits(system: str, user: str, max_out: int) -> bool:
    n = math.ceil(len((system + user).encode("utf-8")) / BYTES_PER_TOKEN)
    return n + FIT_OVERHEAD_TOKENS + max_out <= CONTEXT_TOKENS


def _msgs(rec: dict) -> list[dict]:
    return list(history_rec(rec).get("source_messages") or [])


# ------------------------------------------------------------------------------------------
# Claude Code prompt arm
# ------------------------------------------------------------------------------------------

_ANALYSIS = re.compile(r"<analysis>[\s\S]*?</analysis>")
_SUMMARY = re.compile(r"<summary>([\s\S]*?)</summary>")


def cc_format_summary(text: str) -> str:
    """Claude Code's summary formatting: drop <analysis>, "<summary>X</summary>" ->
    "Summary:\\nX", collapse blank-line runs, trim (as the native summary message shows it)."""
    r = _ANALYSIS.sub("", text, count=1)
    m = _SUMMARY.search(r)
    if m:
        r = r[: m.start()] + "Summary:\n" + m.group(1).strip() + r[m.end() :]
    r = re.sub(r"\n\n+", "\n\n", r)
    return r.strip()


def build_cc_prompt(rec: dict) -> ReplayRequest:
    ps = prompts_for("cc_prompt")
    instruction = ps.texts["cc_instruction"]
    size = rec.get("s1_size") or {}
    prompt_sha = ps.sha256
    if size.get("length_line"):
        instruction = instruction.rstrip("\n") + "\n\n" + size["length_line"]
        prompt_sha = _sha(ps.sha256 + "\n" + "length_line=" + _sha(size["length_line"]))
    r = _render(rec)
    return ReplayRequest(
        system=ps.texts["cc_system"],
        user=_frame(r.text, instruction),
        kept_messages=[],
        prompt_sha256=prompt_sha,
        arm="cc_prompt",
        max_output_tokens=MAX_OUT["cc_prompt"],
        info={"rendered_bytes": r.bytes, "length_line": size.get("length_line")},
    )


# ------------------------------------------------------------------------------------------
# Codex local (ported from P49 compact.py; Codex ac9db7d)
# ------------------------------------------------------------------------------------------


def _floor_boundary(b: bytes, i: int) -> int:
    while 0 < i < len(b) and (b[i] & 0xC0) == 0x80:
        i -= 1
    return i


def _ceil_boundary(b: bytes, i: int) -> int:
    while 0 < i < len(b) and (b[i] & 0xC0) == 0x80:
        i += 1
    return i


def codex_approx_tokens(text: str) -> int:
    """P49 compact.codex_approx_tokens: bytes/4 rounded up."""
    return (len(text.encode("utf-8")) + 3) // 4


def codex_truncate_tokens(text: str, max_tokens: int) -> str:
    """P49 compact.codex_truncate_tokens = codex_utils_string::truncate_middle_with_token_budget."""
    b = text.encode("utf-8")
    max_bytes = max_tokens * 4
    if len(b) <= max_bytes:
        return text
    left = max_bytes // 2
    right = max_bytes - left
    start, end = _floor_boundary(b, left), _ceil_boundary(b, len(b) - right)
    removed_tokens = (len(b) - max_bytes + 3) // 4
    marker = f"…{removed_tokens} tokens truncated…"
    return b[:start].decode("utf-8") + marker + b[end:].decode("utf-8")


def _is_codex_summary(text: str) -> bool:
    prefix = prompts_for("codex_local").texts["codex_summary_prefix.md"]
    return text.startswith(prefix + "\n")


def codex_kept_user_messages(
    messages: list[dict],
    budget: int = CODEX_KEEP_USER_TOKENS,
    candidates: list[bool] | None = None,
) -> tuple[list[dict], dict]:
    """P49 compact.codex_kept_user_messages on S1 Messages (text in "text"): user messages
    (`candidates`, default `is_user_request`; never earlier Codex summaries), newest first within `budget` tokens; the crossing message is
    middle-truncated to what remains; returned in original order."""
    users = [
        (i, m)
        for i, m in enumerate(messages)
        if (candidates[i] if candidates is not None else is_user_request(m))
        and m.get("role") == "user"
        and not _is_codex_summary(m.get("text") or "")
    ]
    remaining = budget
    picked = []
    for i, m in reversed(users):
        if remaining == 0:
            break
        t = m.get("text") or ""
        n = codex_approx_tokens(t)
        if n <= remaining:
            picked.append((i, m, "verbatim"))
        else:
            picked.append((i, {**m, "text": codex_truncate_tokens(t, remaining)}, "truncated"))
            break
        remaining -= n
    picked.reverse()
    info = {
        "user_messages": len(users),
        "kept": [i for i, _, _ in picked],
        "verbatim": [i for i, _, s in picked if s == "verbatim"],
        "truncated": [i for i, _, s in picked if s == "truncated"],
        "budget_left": max(remaining, 0),
    }
    return [m for _, m, _ in picked], info


def _codex_drop_order(msgs: list[dict]) -> list[list[int]]:
    """Codex remove_first_item, repeatedly: each step drops the oldest remaining item and the
    tool results answering its calls. Returns the indices dropped at each step."""
    alive = list(range(len(msgs)))
    steps = []
    while len(alive) > 1:
        first, rest = alive[0], alive[1:]
        ids = {tc.get("id") for tc in msgs[first].get("tool_calls") or []}
        gone = [first] + [
            j for j in rest if msgs[j].get("role") == "tool" and msgs[j].get("tool_use_id") in ids
        ]
        gone_set = set(gone)
        alive = [j for j in rest if j not in gone_set]
        steps.append(gone)
    return steps


def build_codex_local(rec: dict) -> ReplayRequest:
    ps = prompts_for("codex_local")
    msgs = _msgs(rec)
    cut = copy.copy(history_rec(rec))
    n_cut = 0
    new = []
    for m in msgs:
        if m.get("role") == "tool":
            t = codex_truncate_tokens(m.get("text") or "", CODEX_TOOL_OUTPUT_TOKENS)
            if t != (m.get("text") or ""):
                m = {**m, "text": t}
                n_cut += 1
        new.append(m)
    cut["source_messages"] = new
    r, blocks = _blocks(cut)
    prompt = ps.texts["codex_prompt.md"]
    system = ps.texts["codex_system"]
    alive = sorted(blocks)
    dropped = 0

    def join(idx: list[int]) -> str:
        return "\n\n".join(blocks[i] for i in idx)

    history = r.text
    if not _fits(system, _frame(history, prompt), MAX_OUT["codex_local"]):
        sizes = {i: len(blocks[i].encode("utf-8")) + 2 for i in blocks}
        total = sum(sizes.values())
        limit = (CONTEXT_TOKENS - FIT_OVERHEAD_TOKENS - MAX_OUT["codex_local"]) * BYTES_PER_TOKEN
        fixed = len((system + _frame("", prompt)).encode("utf-8"))
        gone: set[int] = set()
        for step in _codex_drop_order(new):
            if fixed + total <= limit:
                break
            for j in step:
                if j not in gone:
                    gone.add(j)
                    total -= sizes.get(j, 0)
            dropped += 1
        alive = [i for i in sorted(blocks) if i not in gone]
        history = join(alive)
    mask, provenance = user_request_mask(rec)
    kept, info = codex_kept_user_messages(msgs, candidates=mask)
    kept_u, info_u = codex_kept_user_messages(msgs, candidates=unfiltered_user_mask(msgs))
    n_user = sum(1 for m in msgs if m.get("role") == "user")
    info["request_provenance"] = provenance
    info["kept_unfiltered"] = info_u["kept"]
    info["truncated_unfiltered"] = info_u["truncated"]
    info.update(
        {
            "tool_outputs_truncated": n_cut,
            "overflow_dropped_oldest": dropped,
            "first_message_sent": alive[0] if alive else None,
        }
    )
    return ReplayRequest(
        system=system,
        user=_frame(history, prompt),
        kept_messages=kept,
        prompt_sha256=ps.sha256,
        arm="codex_local",
        max_output_tokens=MAX_OUT["codex_local"],
        info=info,
        kept_messages_unfiltered=kept_u,
        n_filtered_out=n_user - info["user_messages"],
    )


# ------------------------------------------------------------------------------------------
# pi (ported from P49 compact.py; pi-mono b06c1f2)
# ------------------------------------------------------------------------------------------


def _parse_args(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        v = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def _js(v) -> str:
    """JSON.stringify (no spaces, non-ASCII kept)."""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _pi_role(m: dict) -> str:
    return "user" if m.get("role") == "system" else str(m.get("role"))


def pi_estimate(m: dict) -> int:
    """pi estimateTokens (chars/4, rounded up); P49 compact.pi_estimate."""
    if m.get("role") == "assistant":
        chars = len(m.get("text") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(tc.get("name") or "") + len(_js(_parse_args(tc.get("args"))))
        return -(-chars // 4)
    return -(-len(m.get("text") or "") // 4)


def pi_find_cut(entries: list[dict], keep_tokens: int = PI_KEEP_RECENT_TOKENS) -> dict:
    """pi findCutPoint (P49 compact.pi_find_cut). Cut points: user, system (as user text) and
    assistant messages; a turn starts only at a real user message."""
    cut_points = [
        i for i, m in enumerate(entries) if m.get("role") in ("user", "assistant", "system")
    ]
    if not cut_points:
        return {"first_kept": 0, "turn_start": -1, "split": False, "kept_estimate": 0}
    acc, cut = 0, cut_points[0]
    for i in range(len(entries) - 1, -1, -1):
        n = pi_estimate(entries[i])
        if n == 0:
            continue
        acc += n
        if acc >= keep_tokens:
            cut = next((c for c in cut_points if c >= i), cut_points[-1])
            break
    starts_turn = entries[cut].get("role") == "user"
    turn_start = -1
    if not starts_turn:
        turn_start = next((i for i in range(cut, -1, -1) if entries[i].get("role") == "user"), -1)
    return {
        "first_kept": cut,
        "turn_start": turn_start,
        "split": (not starts_turn) and turn_start != -1,
        "kept_estimate": acc,
    }


def pi_truncate_result(text: str) -> str:
    n = PI_TOOL_RESULT_CHARS
    if len(text) <= n:
        return text
    return f"{text[:n]}\n\n[... {len(text) - n} more characters truncated]"


def pi_serialize(messages: list[dict]) -> str:
    """pi serializeConversation (P49 compact.pi_serialize) on S1 Messages."""
    parts = []
    for m in messages:
        role, text = _pi_role(m), m.get("text") or ""
        if role == "user":
            if text:
                parts.append(f"[User]: {text}")
        elif role == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                args = _parse_args(tc.get("args"))
                calls.append(
                    f"{tc.get('name')}(" + ", ".join(f"{k}={_js(v)}" for k, v in args.items()) + ")"
                )
            if text:
                parts.append(f"[Assistant]: {text}")
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif role == "tool" and text:
            parts.append(f"[Tool result]: {pi_truncate_result(text)}")
    return "\n\n".join(parts)


def pi_merge(texts: dict[str, str], split: bool, has_history: bool) -> str:
    """pi's merge of the history and turn-prefix summaries (P49 compact._pi)."""
    if split:
        history_text = texts.get("history", "") if has_history else "No prior history."
        return f"{history_text}\n\n---\n\n**Turn Context (split turn):**\n\n{texts['turn_prefix']}"
    return texts["history"]


def build_pi(rec: dict) -> ReplayRequest:
    ps = prompts_for("pi")
    entries = _msgs(rec)
    cut = pi_find_cut(entries)
    history_end = cut["turn_start"] if cut["split"] else cut["first_kept"]
    to_summarize = entries[:history_end]
    prefix = entries[cut["turn_start"] : cut["first_kept"]] if cut["split"] else []
    system = ps.texts["pi_system_prompt.txt"]
    calls = []
    if to_summarize or not prefix:
        text = (
            CONVERSATION_OPEN
            + pi_serialize(to_summarize)
            + CONVERSATION_CLOSE
            + ps.texts["pi_summarization_prompt.txt"]
        )
        calls.append(Call("history", system, text, MAX_OUT["pi"]))
    if prefix:
        text = (
            "# Conversation\n"
            + pi_serialize(prefix)
            + "\n\n# Instructions\n"
            + ps.texts["pi_turn_prefix_prompt.txt"]
        )
        calls.append(Call("turn_prefix", system, text, PI_TURN_PREFIX_MAX_TOKENS))
    n_cut = sum(
        1
        for m in entries[: cut["first_kept"]]
        if m.get("role") == "tool" and len(m.get("text") or "") > PI_TOOL_RESULT_CHARS
    )
    first = calls[0]
    return ReplayRequest(
        system=first.system,
        user=first.user,
        kept_messages=entries[cut["first_kept"] :],
        prompt_sha256=ps.sha256,
        arm="pi",
        max_output_tokens=first.max_output_tokens,
        label=first.label,
        extra_calls=tuple(calls[1:]),
        info={
            "cut": cut,
            "split": cut["split"],
            "has_history": bool(to_summarize),
            "first_kept": cut["first_kept"],
            "tool_results_cut_2000": n_cut,
            "would_skip": cut["first_kept"] == 0 and not cut["split"],
        },
    )


# ------------------------------------------------------------------------------------------
# OpenCode (session/compaction.ts v1.19.3)
# ------------------------------------------------------------------------------------------


def opencode_truncate(value: str) -> str:
    n = OPENCODE_TOOL_OUTPUT_MAX_CHARS
    return value if len(value) <= n else f"{value[:n]}\n[truncated]"


def opencode_units(messages: list[dict]) -> list[dict]:
    """OpenCode messages from S1 Messages. A run of assistant messages plus the tool messages
    right after it is one assistant message (one step: its tool parts carry the results); user
    and system messages are one message each; orphan tool runs are their own unit."""
    units: list[dict] = []
    i, n = 0, len(messages)
    while i < n:
        role = messages[i].get("role")
        if role == "assistant":
            j = i
            while j < n and messages[j].get("role") == "assistant":
                j += 1
            k = j
            while k < n and messages[k].get("role") == "tool":
                k += 1
            units.append({"kind": "assistant", "idx": list(range(i, k))})
            i = k
        elif role == "tool":
            k = i
            while k < n and messages[k].get("role") == "tool":
                k += 1
            units.append({"kind": "tool", "idx": list(range(i, k))})
            i = k
        else:
            units.append({"kind": "user" if role == "user" else "system", "idx": [i]})
            i += 1
    return units


def _unit_parts(unit: dict, messages: list[dict]):
    """(calls-with-results in order, assistant texts) for an assistant unit."""
    asst = [messages[i] for i in unit["idx"] if messages[i].get("role") == "assistant"]
    tools = [messages[i] for i in unit["idx"] if messages[i].get("role") == "tool"]
    results = {}
    for t in tools:
        results.setdefault(t.get("tool_use_id"), t)
    used = set()
    seq = []
    for a in asst:
        if a.get("text"):
            seq.append(("text", a.get("text")))
        for tc in a.get("tool_calls") or []:
            r = results.get(tc.get("id")) if tc.get("id") is not None else None
            if r is not None:
                used.add(id(r))
            seq.append(("call", tc, r))
    orphans = [t for t in tools if id(t) not in used]
    return seq, orphans


def opencode_serialize_unit(unit: dict, messages: list[dict]) -> str:
    if unit["kind"] in ("user", "system"):
        t = messages[unit["idx"][0]].get("text") or ""
        return f"[User]: {t}" if t else ""
    out = []
    if unit["kind"] == "tool":
        return "\n".join(
            f"[Tool result]: {opencode_truncate(messages[i].get('text') or '')}"
            for i in unit["idx"]
        )
    seq, orphans = _unit_parts(unit, messages)
    for part in seq:
        if part[0] == "text":
            out.append(f"[Assistant]: {part[1]}")
            continue
        _, tc, r = part
        args = tc.get("args")
        inp = args if isinstance(args, str) else _js({} if args is None else args)
        out.append(f"[Assistant tool call]: {tc.get('name')}({inp})")
        if r is not None:
            out.append(f"[Tool result]: {opencode_truncate(r.get('text') or '')}")
    out.extend(f"[Tool result]: {opencode_truncate(t.get('text') or '')}" for t in orphans)
    return "\n".join(out)


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _unit_model_messages(unit: dict, messages: list[dict]) -> list[dict]:
    """AI-SDK-shaped model messages for one unit (approximation of toModelMessages)."""
    if unit["kind"] in ("user", "system"):
        t = messages[unit["idx"][0]].get("text") or ""
        return [{"role": "user", "content": [{"type": "text", "text": t}]}]
    if unit["kind"] == "tool":
        return [
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": messages[i].get("tool_use_id"),
                        "output": {"type": "text", "value": messages[i].get("text") or ""},
                    }
                    for i in unit["idx"]
                ],
            }
        ]
    seq, orphans = _unit_parts(unit, messages)
    content, results = [], []
    for part in seq:
        if part[0] == "text":
            content.append({"type": "text", "text": part[1]})
            continue
        _, tc, r = part
        content.append(
            {
                "type": "tool-call",
                "toolCallId": tc.get("id"),
                "toolName": tc.get("name"),
                "input": tc.get("args"),
            }
        )
        if r is not None:
            results.append(
                {
                    "type": "tool-result",
                    "toolCallId": tc.get("id"),
                    "toolName": tc.get("name"),
                    "output": {"type": "text", "value": r.get("text") or ""},
                }
            )
    for t in orphans:
        results.append(
            {
                "type": "tool-result",
                "toolCallId": t.get("tool_use_id"),
                "output": {"type": "text", "value": t.get("text") or ""},
            }
        )
    out = [{"role": "assistant", "content": content}]
    if results:
        out.append({"role": "tool", "content": results})
    return out


def opencode_select(messages: list[dict], budget: int = OPENCODE_PRESERVE_RECENT_TOKENS) -> dict:
    """OpenCode `select` + `splitTurn` over units. Returns {"keep": unit index or None, ...}."""
    units = opencode_units(messages)
    # per-unit JSON lengths, so the estimate of any slice is exact without re-serialising
    lens = []
    for u in units:
        mm = _unit_model_messages(u, messages)
        lens.append((sum(_utf16_len(_js(x)) for x in mm), len(mm)))

    def estimate(a: int, b: int) -> int:
        chars = sum(c for c, _ in lens[a:b])
        k = sum(c for _, c in lens[a:b])
        total = 2 + chars + max(k - 1, 0)  # "[" + ",".join + "]"
        return math.floor(total / 4 + 0.5)  # Math.round

    starts = [i for i, u in enumerate(units) if u["kind"] == "user"]
    turns = [
        (s, starts[t + 1] if t + 1 < len(starts) else len(units)) for t, s in enumerate(starts)
    ]
    total, keep, how = 0, None, None
    for s, e in reversed(turns):
        size = estimate(s, e)
        if total + size <= budget:
            total += size
            keep, how = s, "turns"
            continue
        remaining = budget - total
        split = None
        if remaining > 0 and e - s > 1:
            for st in range(s + 1, e):
                if estimate(st, e) <= remaining:
                    split = st
                    break
        if split is not None:
            keep, how = split, "split_turn"
        break
    if keep is None or keep == 0:
        return {"units": units, "keep": None, "how": "none", "first_kept": None}
    return {
        "units": units,
        "keep": keep,
        "how": how,
        "first_kept": units[keep]["idx"][0],
        "tail_estimate": estimate(keep, len(units)),
    }


def build_opencode(rec: dict) -> ReplayRequest:
    ps = prompts_for("opencode")
    t = ps.texts
    msgs = _msgs(rec)
    sel = opencode_select(msgs)
    units = sel["units"]
    head = units if sel["keep"] is None else units[: sel["keep"]]
    conversation = "\n\n".join(s for s in (opencode_serialize_unit(u, msgs) for u in head) if s)
    framed = t["opencode_frame_open"] + conversation + t["opencode_frame_close"]
    user = t["opencode_join"].join([framed, t["opencode_instruction"], t["opencode_template"]])
    kept = [] if sel["first_kept"] is None else msgs[sel["first_kept"] :]
    n_cut = sum(
        1
        for u in head
        for i in u["idx"]
        if msgs[i].get("role") == "tool"
        and len(msgs[i].get("text") or "") > OPENCODE_TOOL_OUTPUT_MAX_CHARS
    )
    return ReplayRequest(
        system=t["opencode_system"],
        user=user,
        kept_messages=kept,
        prompt_sha256=ps.sha256,
        arm="opencode",
        max_output_tokens=MAX_OUT["opencode"],
        info={
            "how": sel["how"],
            "first_kept": sel["first_kept"],
            "tail_estimate": sel.get("tail_estimate"),
            "tool_results_cut_2000": n_cut,
            "budget": OPENCODE_PRESERVE_RECENT_TOKENS,
        },
    )


# ------------------------------------------------------------------------------------------
# Antigravity (agy 1.4.3)
# ------------------------------------------------------------------------------------------


def agy_kept_requests(
    messages: list[dict], n: int = AGY_MAX_REQUESTS, candidates: list[bool] | None = None
) -> tuple[list[dict], list[int]]:
    cand = candidates if candidates is not None else [is_user_request(m) for m in messages]
    idx = [i for i, m in enumerate(messages) if cand[i] and m.get("role") == "user"][-n:]
    return [messages[i] for i in idx], idx


def agy_summary_text(text: str) -> str:
    m = _SUMMARY.search(text)
    return (m.group(1) if m else text).strip()


def agy_checkpoint(requests: list[str], summary: str, capped: bool = False) -> str:
    """The CHECKPOINT text agy shows after a compaction, without its "# Conversation Logs"
    pointer (a replay has no transcript files). `capped`: the list was cut to the most recent
    AGY_MAX_REQUESTS, which takes the binary's "most recent" intro (an inference: the real
    checkpoint, with every request listed, shows the other intro)."""
    t = prompts_for("antigravity").texts
    intro = t["agy_checkpoint_requests_capped" if capped else "agy_checkpoint_requests"]
    items = "".join(f"{i}. {r}\n" for i, r in enumerate(requests, 1))
    return (
        f"{t['agy_checkpoint_head']}\n\n{t['agy_checkpoint_resume']}\n\n"
        f"{intro}\n{items}\n<summary>\n{summary}\n</summary>"
    )


def build_antigravity(rec: dict) -> ReplayRequest:
    ps = prompts_for("antigravity")
    msgs = _msgs(rec)
    mask, provenance = user_request_mask(rec)
    kept, idx = agy_kept_requests(msgs, candidates=mask)
    kept_u, idx_u = agy_kept_requests(msgs, candidates=unfiltered_user_mask(msgs))
    n_user = sum(1 for m in msgs if m.get("role") == "user")
    n_req = sum(1 for i, m in enumerate(msgs) if mask[i] and m.get("role") == "user")
    r = _render(rec)
    return ReplayRequest(
        system=NEUTRAL_SYSTEM,
        user=_frame(r.text, ps.texts["agy_prompt"]),
        kept_messages=kept,
        prompt_sha256=ps.sha256,
        arm="antigravity",
        max_output_tokens=MAX_OUT["antigravity"],
        info={
            "kept_indices": idx,
            "kept_indices_unfiltered": idx_u,
            "user_messages": n_req,
            "request_provenance": provenance,
            "capped": n_req > AGY_MAX_REQUESTS,
            "system_source": "assumption: NEUTRAL_SYSTEM (agy's summarizer system prompt is "
            "not captured)",
        },
        kept_messages_unfiltered=kept_u,
        n_filtered_out=n_user - n_req,
    )


ARM_BUILDERS: dict[str, Callable[[dict], ReplayRequest]] = {
    "cc_prompt": build_cc_prompt,
    "codex_local": build_codex_local,
    "pi": build_pi,
    "antigravity": build_antigravity,
    "opencode": build_opencode,
}


def finish_summary(arm: str, req: ReplayRequest, texts: dict[str, str]) -> str:
    """Each harness's treatment of the summarizer's answer."""
    if arm == "cc_prompt":
        return cc_format_summary(texts[req.label])
    if arm == "antigravity":
        return agy_summary_text(texts[req.label])
    if arm == "pi":
        return pi_merge(texts, req.info["split"], req.info["has_history"])
    if arm == "opencode":
        return texts[req.label].strip()
    return texts[req.label]  # codex_local: as-is


# ------------------------------------------------------------------------------------------
# Records
# ------------------------------------------------------------------------------------------


def _usage(resps: list) -> dict:
    u = {
        "calls": len(resps),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "usd": 0.0,
        "duration_s": 0.0,
    }
    for r in resps:
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            u[k] += int(getattr(r, k, 0) or 0)
        u["usd"] += float(getattr(r, "usd", 0.0) or 0.0)
        u["duration_s"] += float(getattr(r, "duration_s", 0.0) or 0.0)
    u["duration_s"] = round(u["duration_s"], 3)
    return u


def replay_id(rec: dict, arm: str) -> str:
    return f"{rec['id']}@{arm}"


def _base_rid(rec: dict) -> str:
    return (rec.get("s1_size") or {}).get("base_rid") or rec["id"]


POLICY_REVISION = 2  # bump when a keep rule, frame or parser changes in a way constants miss


def policy_version(arm: str) -> str:
    """A version string for an arm's replay policy: the revision plus a hash of every constant
    the arm's keep rules, frame and caps depend on."""
    common = {
        "frame": [CONVERSATION_OPEN, CONVERSATION_CLOSE],
        "max_out": MAX_OUT.get(arm),
        "context": CONTEXT_TOKENS,
        "bpt": BYTES_PER_TOKEN,
        "neutral": NEUTRAL_SYSTEM,
    }
    per = {
        "cc_prompt": {"size_line": SIZE_LENGTH_LINE},
        "codex_local": {
            "keep": CODEX_KEEP_USER_TOKENS,
            "tool": CODEX_TOOL_OUTPUT_TOKENS,
            "prefixes": HARNESS_USER_PREFIXES,
        },
        "pi": {
            "keep": PI_KEEP_RECENT_TOKENS,
            "tool": PI_TOOL_RESULT_CHARS,
            "prefix_max": PI_TURN_PREFIX_MAX_TOKENS,
        },
        "antigravity": {"n": AGY_MAX_REQUESTS, "prefixes": HARNESS_USER_PREFIXES},
        "opencode": {
            "tool": OPENCODE_TOOL_OUTPUT_MAX_CHARS,
            "keep": OPENCODE_PRESERVE_RECENT_TOKENS,
        },
        NATIVE_ARM: {"args": NATIVE_ARGS, "env": "clean_env-DISABLE_COMPACT"},
    }.get(arm, {})
    return f"s1_replay/{arm}/r{POLICY_REVISION}/{_sha(_canon({**common, **per}))[:16]}"


def fingerprint(
    rec: dict, arm: str, req: ReplayRequest, generation: dict | None = None, model: str = MODEL
) -> str:
    """What a replay row was computed from: the request as sent (history bytes and prompts of
    every call), the arm's policy version, the prompt sha, the model, the generation settings,
    and the kept messages. A rerun reuses a row only when this matches."""
    obj = {
        "arm": arm,
        "policy": policy_version(arm),
        "prompt_sha256": req.prompt_sha256,
        "model": model,
        "generation": generation or {},
        "calls": [
            {
                "label": c.label,
                "system": _sha(c.system),
                "user": _sha(c.user),
                "max_output_tokens": c.max_output_tokens,
            }
            for c in req.calls
        ],
        "kept": _sha(_canon(req.kept_messages)),
        "kept_unfiltered": None
        if req.kept_messages_unfiltered is None
        else _sha(_canon(req.kept_messages_unfiltered)),
        "size": {k: v for k, v in (rec.get("s1_size") or {}).items()},
    }
    return _sha(_canon(obj))


def _generation(backend) -> dict:
    return {
        "backend": getattr(backend, "name", None),
        "effort": getattr(backend, "effort", None),
        "schema": None,
    }


def requests_for(rec: dict, arm: str, req: ReplayRequest) -> list[Req]:
    key = replay_id(rec, arm)
    return [
        Req(
            key=key if i == 0 else f"{key}#{c.label}",
            system=c.system,
            user=c.user,
            schema=None,
            max_output_tokens=c.max_output_tokens,
            rid=_base_rid(rec),
            source=str(rec.get("source")),
            private=anthropic_only(rec),
        )
        for i, c in enumerate(req.calls)
    ]


def assemble(
    rec: dict, arm: str, req: ReplayRequest, resps: list, fingerprint_: str | None = None
) -> dict:
    """A ReplayRecord from one record's request and its responses (one per call, in order).
    Every response must come from MODEL: a missing or different model id is "model_mismatch"."""
    errors = [r.error for r in resps if r.error]
    texts = {c.label: (r.text or "") for c, r in zip(req.calls, resps, strict=True)}
    summary, error = "", None
    if errors:
        error = ";".join(dict.fromkeys(errors))
    elif any(r.model != MODEL for r in resps):
        error = "model_mismatch"
    else:
        summary = finish_summary(arm, req, texts)
        if not summary.strip():
            error = "empty_summary"
    models = sorted({r.model for r in resps if r.model})
    out = {
        "id": replay_id(rec, arm),
        "base_rid": _base_rid(rec),
        "arm": arm,
        "summary": summary,
        "kept_messages": req.kept_messages,
        "model": models[0] if len(models) == 1 else ",".join(models),
        "prompt_sha256": req.prompt_sha256,
        "request_sha256": req.request_sha256,
        "usage": _usage(resps),
        "native": None,
        "error": error,
        "n_preserved_in_history": history_rec(rec)["s1_n_preserved"],
        "fingerprint": fingerprint_ or fingerprint(rec, arm, req),
        "policy_version": policy_version(arm),
    }
    if req.kept_messages_unfiltered is not None:
        out["kept_messages_unfiltered"] = req.kept_messages_unfiltered
        out["n_filtered_out"] = req.n_filtered_out
    if rec.get("s1_size"):
        out["size"] = {k: v for k, v in rec["s1_size"].items() if k != "length_line"}
    return out


# ------------------------------------------------------------------------------------------
# Native Claude Code /compact
# ------------------------------------------------------------------------------------------


class NativeBillingError(RuntimeError):
    """The native run did not use the Max login (it could bill the API): stop the batch."""


def project_slug(cwd: str) -> str:
    """Claude Code's project directory name for a cwd (every non-alphanumeric char -> "-")."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    if len(slug) > 200:
        raise ValueError(f"cwd too long for an unhashed project slug: {cwd}")
    return slug


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _claude_version() -> str:
    w = shutil.which(os.environ.get("CLAUDE_HEADLESS_BIN") or "claude")
    if not w:
        return "unknown"
    base = Path(os.path.realpath(w)).name
    return base if re.fullmatch(r"\d+\.\d+\.\d+", base) else "unknown"


@functools.cache
def claude_binary_sha256() -> str:
    """sha256 of the installed claude binary: the native arm's prompt is bundled in it."""
    w = shutil.which(os.environ.get("CLAUDE_HEADLESS_BIN") or "claude")
    if not w:
        return "unknown"
    h = hashlib.sha256()
    with open(os.path.realpath(w), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _order_for_pairing(msgs: list[dict]) -> tuple[list[int], int]:
    """Source indices in write order: every tool result that answers a call of an assistant
    run is moved to directly after that run (the API needs tool_result right after tool_use).
    Returns (order, number of messages moved)."""
    n = len(msgs)
    placed: set[int] = set()
    order: list[int] = []
    moved = 0
    i = 0
    while i < n:
        if i in placed:
            i += 1
            continue
        if msgs[i].get("role") != "assistant":
            order.append(i)
            placed.add(i)
            i += 1
            continue
        j = i
        run = []
        while j < n and msgs[j].get("role") == "assistant" and j not in placed:
            run.append(j)
            j += 1
        ids = {tc.get("id") for k in run for tc in msgs[k].get("tool_calls") or []}
        ids.discard(None)
        order.extend(run)
        placed.update(run)
        answered: set = set()
        other_between = False  # a message stays between the run and this result
        for k in range(j, n):
            if answered == ids:
                break
            m = msgs[k]
            if k in placed:
                continue
            if m.get("role") != "tool":
                other_between = True
                continue
            tid = m.get("tool_use_id")
            if tid in ids and tid not in answered:
                moved += other_between
                order.append(k)
                placed.add(k)
                answered.add(tid)
        i = j
    return order, moved


def write_cc_transcript(
    messages: list[dict],
    path: Path,
    *,
    session_id: str,
    cwd: str,
    model: str,
    version: str = "unknown",
) -> dict:
    """Write S1 Messages as a Claude Code session file.

    user -> user entry (text); system -> user entry (text; Claude Code delivers system-reminders
    as user content); assistant -> assistant entry with a text block (if any) and one tool_use
    block per call, consecutive assistant entries sharing one message id (Claude Code merges
    them into one API message, as it does for parallel calls); tool -> user entry with a
    tool_result block for its call. Entries are chained by uuid/parentUuid. Repairs, counted in
    the result: tool results moved next to their call; orphan results (no earlier call) written
    as user text; calls with no result get a synthetic is_error tool_result; empty texts become
    "(empty)". Returns {"uuid_to_index", "uuids", "repairs"}."""
    order, moved = _order_for_pairing(messages)
    repairs = {
        "moved_results": moved,
        "orphan_results_as_text": 0,
        "synthetic_results": 0,
        "empty_texts": 0,
        "empty_assistant": 0,
    }
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    lines: list[dict] = []
    uuid_to_index: dict[str, int | None] = {}
    parent: str | None = None
    pending: list[str] = []  # unanswered tool_use ids of the current assistant run
    msg_id = None
    n_written = 0

    def ts_of(m: dict | None) -> str:
        if m and isinstance(m.get("ts"), str) and m["ts"]:
            return m["ts"]
        return (t0 + timedelta(seconds=n_written)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def base(kind: str, m: dict | None) -> dict:
        return {
            "parentUuid": parent,
            "isSidechain": False,
            "type": kind,
            "uuid": str(uuidlib.uuid4()),
            "timestamp": ts_of(m),
            "cwd": cwd,
            "sessionId": session_id,
            "version": version,
            "userType": "external",
            "entrypoint": "cli",
        }

    def emit(entry: dict, idx: int | None) -> None:
        nonlocal parent, n_written
        lines.append(entry)
        uuid_to_index[entry["uuid"]] = idx
        parent = entry["uuid"]
        n_written += 1

    def text_of(m: dict) -> str:
        t = m.get("text")
        if not isinstance(t, str):
            t = "" if t is None else _js(t)
        if not t.strip():
            repairs["empty_texts"] += 1
            return "(empty)"
        return t

    def close_run() -> None:
        nonlocal pending
        for tid in pending:
            e = base("user", None)
            e["message"] = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": "[no tool result recorded]",
                        "is_error": True,
                    }
                ],
            }
            emit(e, None)
            repairs["synthetic_results"] += 1
        pending = []

    prev_role = None
    for idx in order:
        m = messages[idx]
        role = m.get("role")
        if role == "assistant":
            if prev_role != "assistant":
                close_run()
                msg_id = "msg_" + uuidlib.uuid4().hex[:24]
            content = []
            if isinstance(m.get("text"), str) and m["text"].strip():
                content.append({"type": "text", "text": m["text"]})
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") or ("toolu_" + uuidlib.uuid4().hex[:20])
                args = tc.get("args")
                inp = args if isinstance(args, dict) else (_parse_args(args) or {"input": args})
                content.append(
                    {
                        "type": "tool_use",
                        "id": tid,
                        "name": tc.get("name") or "unknown",
                        "input": inp,
                    }
                )
                pending.append(tid)
            if not content:
                repairs["empty_assistant"] += 1
                content = [{"type": "text", "text": "(no content)"}]
            e = base("assistant", m)
            e["message"] = {
                "role": "assistant",
                "content": content,
                "model": model,
                "id": msg_id,
                "type": "message",
                "stop_reason": "tool_use" if pending else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            emit(e, idx)
        elif role == "tool" and m.get("tool_use_id") in pending:
            tid = m["tool_use_id"]
            pending.remove(tid)
            t = m.get("text") if isinstance(m.get("text"), str) else text_of(m)
            e = base("user", m)
            e["toolUseResult"] = t
            e["message"] = {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tid, "content": t}],
            }
            emit(e, idx)
        else:
            close_run()
            e = base("user", m)
            if role == "tool":
                repairs["orphan_results_as_text"] += 1
                body = f"[tool result]\n{text_of(m)}"
            else:
                body = text_of(m)
            e["message"] = {"role": "user", "content": body}
            emit(e, idx)
        prev_role = role
    close_run()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in lines:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"uuid_to_index": uuid_to_index, "uuids": [e["uuid"] for e in lines], "repairs": repairs}


def _content_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


NATIVE_SUMMARY_START = "Summary:"
NATIVE_SUMMARY_END = "\n\nIf you need specific details from before compaction"


def native_summary_text(full: str) -> str:
    """The summary inside Claude Code's compact-summary user message: from "Summary:" up to the
    transcript pointer (the cc_prompt arm's form)."""
    s = full.find(NATIVE_SUMMARY_START)
    s = 0 if s == -1 else s
    e = full.find(NATIVE_SUMMARY_END, s)
    return full[s : e if e != -1 else len(full)].strip()


def parse_native(
    events: list[dict], session_lines: list[dict], uuid_to_index: dict, model: str
) -> dict:
    """Read a native /compact run: the boundary metadata, the summary, the preserved messages
    (mapped back to source indices) and the model check. Returns
    {"error", "summary", "summary_message", "boundary", "preserved_indices",
     "preserved_unmapped", "model_usage", "model", "api_key_source", "version", "result"}."""
    out: dict[str, Any] = {
        "error": None,
        "summary": "",
        "summary_message": None,
        "boundary": None,
        "preserved_indices": [],
        "preserved_unmapped": [],
        "model_usage": None,
        "model": None,
        "api_key_source": None,
        "version": None,
        "result": None,
        "compact_result": None,
    }
    boundary_i = None
    for k, ev in enumerate(events):
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            out["api_key_source"] = ev.get("apiKeySource")
            out["version"] = ev.get("claude_code_version")
        elif ev.get("type") == "system" and ev.get("subtype") == "compact_boundary":
            out["boundary"] = ev.get("compact_metadata")
            boundary_i = k
        elif ev.get("type") == "system" and "compact_result" in ev:
            out["compact_result"] = ev.get("compact_result")
        elif ev.get("type") == "result":
            out["result"] = {
                k2: ev.get(k2)
                for k2 in (
                    "is_error",
                    "subtype",
                    "total_cost_usd",
                    "modelUsage",
                    "usage",
                    "duration_ms",
                )
            }
    if boundary_i is not None:
        for ev in events[boundary_i + 1 :]:
            if ev.get("type") == "user":
                out["summary_message"] = _content_text((ev.get("message") or {}).get("content"))
                break
    # the session file is the record of what Claude Code kept: prefer it
    file_boundary = next(
        (
            x
            for x in reversed(session_lines)
            if x.get("type") == "system" and x.get("subtype") == "compact_boundary"
        ),
        None,
    )
    file_summary = next((x for x in reversed(session_lines) if x.get("isCompactSummary")), None)
    if file_summary is not None:
        out["summary_message"] = _content_text((file_summary.get("message") or {}).get("content"))
    preserved = None
    if out["boundary"]:
        preserved = (out["boundary"].get("preserved_messages") or {}).get("uuids")
    if preserved is None and file_boundary:
        preserved = (
            (file_boundary.get("compactMetadata") or {}).get("preservedMessages") or {}
        ).get("uuids")
        out["boundary"] = out["boundary"] or file_boundary.get("compactMetadata")
    preserved = preserved or []
    idx = []
    for u in preserved:
        i = uuid_to_index.get(u)
        if i is None:
            out["preserved_unmapped"].append(u)
        elif i not in idx:
            idx.append(i)
    out["preserved_indices"] = sorted(idx)
    cost = next((x for x in reversed(session_lines) if x.get("type") == "cost-state"), None)
    if cost is not None:
        out["model_usage"] = cost.get("modelUsage")
    keys = set((out["model_usage"] or {}).keys())
    out["model"] = ",".join(sorted(keys)) or None
    if out["summary_message"]:
        out["summary"] = native_summary_text(out["summary_message"])
    proc = next((e for e in reversed(events) if e.get("type") == "_process"), {})
    if out["compact_result"] not in (None, "success"):
        out["error"] = f"compact_failed:{out['compact_result']}"
    elif out["boundary"] is None:
        out["error"] = "no_boundary"
    elif not out["summary"]:
        out["error"] = "no_summary"
    elif cost is None:
        out["error"] = "no_cost_state"
    elif keys != {model}:
        out["error"] = "model_mismatch"
    # exit status: a nonzero exit or an error result is a failure unless the compaction itself
    # was verified (boundary + summary + cost-state with the pinned model); then it is recorded
    rc = proc.get("returncode")
    res_err = bool((out["result"] or {}).get("is_error"))
    out["exit_status"] = {
        "returncode": rc,
        "result_is_error": res_err,
        "ok": rc in (None, 0) and not res_err,
    }
    if out["error"] and not out["exit_status"]["ok"]:
        out["error"] = f"process_failed(rc={rc}, is_error={res_err}):{out['error']}"
    limit = _native_limit(events)
    out["limit"] = limit
    if out["error"] and limit is not None:
        out["error"] = f"limit:{out['error']}"
    return out


def _native_limit(events: list[dict]) -> dict | None:
    """A usage-limit hit in a native run's events (claude_headless._limit_from when available:
    a rejected rate-limit snapshot, or an error result naming a limit)."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    try:
        from compaction import claude_headless

        fn = getattr(claude_headless, "_limit_from", None)
    except ImportError:
        fn = None
    if fn is not None:
        return fn(events, result, time.time())
    for e in events:
        info = e.get("rate_limit_info") if e.get("type") == "rate_limit_event" else None
        if info and info.get("status") not in ("allowed", "allowed_warning"):
            return {"status": info.get("status"), "until": info.get("resetsAt")}
    return None


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


NATIVE_ARGS = (
    "-p",
    "/compact",
    "--resume",
    "<session>",
    "--model",
    "<model>",
    "--safe-mode",
    "--tools",
    "",
    "--strict-mcp-config",
    "--output-format",
    "stream-json",
    "--verbose",
)


def native_args(session_id: str, model: str) -> list[str]:
    return [session_id if a == "<session>" else model if a == "<model>" else a for a in NATIVE_ARGS]


def native_fingerprint(rec: dict, model: str = MODEL) -> str:
    """The native arm's fingerprint: history as written, policy version (args, env rule),
    the claude binary (its bundled compact prompt) and the model."""
    return _sha(
        _canon(
            {
                "arm": NATIVE_ARM,
                "policy": policy_version(NATIVE_ARM),
                "claude": claude_binary_sha256(),
                "model": model,
                "history": _sha(_canon(_msgs(rec))),
            }
        )
    )


def _rmtree_checked(path: Path | str, errors: list[str]) -> None:
    path = Path(path)
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as e:
        msg = f"{path}: {type(e).__name__}: {e}"
        log.warning("NATIVE_CLEANUP_FAILED %s", msg)
        errors.append(msg)
    if path.exists() and not any(str(path) in x for x in errors):
        errors.append(f"{path}: still exists after rmtree")
        log.warning("NATIVE_CLEANUP_FAILED %s still exists", path)


def check_native_model(model: str) -> None:
    """Part B holds the model fixed: the native arm runs only on MODEL."""
    if model != MODEL:
        raise ValueError(f"native replays must run on {MODEL}, not {model!r}")


def native_env() -> dict[str, str]:
    """clean_env() with DISABLE_COMPACT removed: Claude Code's docs say DISABLE_COMPACT=1 turns
    off all compaction, the manual /compact included."""
    from compaction import claude_headless

    env = claude_headless.clean_env()
    env.pop("DISABLE_COMPACT", None)
    return env


def native_compact(
    rec: dict,
    model: str = MODEL,
    *,
    timeout_s: float = 1800.0,
    runner: Callable | None = None,
    env: dict | None = None,
    out_dir: Path | None = None,
    tmp_root: str | None = None,
) -> dict:
    """Claude Code's own /compact on a transcript written from `rec`'s source_messages.

    Writes the transcript into <config>/projects/<slug of a fresh temp cwd>/<uuid>.jsonl, runs
    `claude -p "/compact" --resume <uuid> --model <m> --safe-mode --tools "" --strict-mcp-config
    --output-format stream-json --verbose < /dev/null` (runner = claude_headless.run_session),
    parses the boundary, the summary and the preserved messages, checks cost-state's modelUsage
    is only `model`, copies the session file to <out_dir>/native/<rid>.jsonl, and deletes the
    temp project directory and cwd (also on failure). A failed compaction gives a ReplayRecord
    with `error` set; only a non-Max billing route raises (NativeBillingError)."""
    check_native_model(model)
    out_dir = REPLAY_DIR if out_dir is None else Path(out_dir)
    if runner is None:
        from compaction import claude_headless

        runner = claude_headless.run_session
    msgs = _msgs(rec)
    rid = rec["id"]
    sid = str(uuidlib.uuid4())
    kept_file = out_dir / "native" / f"{rid}.jsonl"
    record = {
        "id": replay_id(rec, NATIVE_ARM),
        "base_rid": rid,
        "arm": NATIVE_ARM,
        "summary": "",
        "kept_messages": [],
        "model": None,
        "prompt_sha256": None,
        "request_sha256": None,
        "usage": {},
        "native": None,
        "error": None,
        "n_preserved_in_history": history_rec(rec)["s1_n_preserved"],
        "fingerprint": native_fingerprint(rec, model),
        "policy_version": policy_version(NATIVE_ARM),
    }
    t0 = time.monotonic()
    events: list[dict] = []
    cleanup_errors: list[str] = []
    proj: Path | None = None
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="s1-native-", dir=tmp_root))
    try:  # cleanup is armed from the moment the temp cwd exists
        proj = claude_config_dir() / "projects" / project_slug(cwd)
        if proj.exists():
            p_, proj = proj, None  # not ours: never delete it
            raise RuntimeError(f"project dir already exists: {p_}")
        session_file = proj / f"{sid}.jsonl"
        written = write_cc_transcript(
            msgs, session_file, session_id=sid, cwd=cwd, model=model, version=_claude_version()
        )
        record["request_sha256"] = _sha(session_file.read_bytes())
        record["prompt_sha256"] = claude_binary_sha256()
        args = native_args(sid, model)
        kwargs = {}
        if "env" in inspect.signature(runner).parameters:
            kwargs["env"] = env if env is not None else native_env()
        try:
            events = runner(cwd, args, None, timeout_s, **kwargs)
        except Exception as e:  # noqa: BLE001 -- SessionTimeout carries .events
            events = list(getattr(e, "events", []) or [])
            if type(e).__name__ != "SessionTimeout":
                record["error"] = f"runner:{type(e).__name__}: {e}"[:500]
            else:
                record["error"] = "timeout"
        lines = _read_jsonl(session_file)
        parsed = parse_native(events, lines, written["uuid_to_index"], model)
        if parsed["api_key_source"] not in (None, "none"):
            raise NativeBillingError(f"{rid}: apiKeySource={parsed['api_key_source']!r}")
        if session_file.exists():
            kept_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(session_file, kept_file)
        record["error"] = record["error"] or parsed["error"]
        record["summary"] = parsed["summary"] if not record["error"] else ""
        record["kept_messages"] = [msgs[i] for i in parsed["preserved_indices"]]
        record["model"] = parsed["model"]
        mu = (parsed["model_usage"] or {}).get(model) or {}
        res = parsed["result"] or {}
        record["usage"] = {
            "calls": 1,
            "input_tokens": int(mu.get("inputTokens", 0) or 0),
            "output_tokens": int(mu.get("outputTokens", 0) or 0),
            "cache_read_tokens": int(mu.get("cacheReadInputTokens", 0) or 0),
            "cache_write_tokens": int(mu.get("cacheCreationInputTokens", 0) or 0),
            "thinking_tokens": int(mu.get("thinkingTokens", 0) or 0),
            "usd": 0.0,
            "list_usd": res.get("total_cost_usd"),
            "duration_s": round(time.monotonic() - t0, 3),
        }
        b = parsed["boundary"] or {}
        record["native"] = {
            "session_file": _rel(kept_file) if kept_file.exists() else None,
            "trigger": b.get("trigger"),
            "pre_tokens": b.get("pre_tokens", b.get("preTokens")),
            "post_tokens": b.get("post_tokens", b.get("postTokens")),
            "boundary": b or None,
            "compact_result": parsed["compact_result"],
            "preserved_indices": parsed["preserved_indices"],
            "preserved_unmapped": parsed["preserved_unmapped"],
            "summary_message": parsed["summary_message"],
            "model_usage": parsed["model_usage"],
            "claude_code_version": parsed["version"],
            "writer_repairs": written["repairs"],
            "n_entries": len(written["uuids"]),
            "process": next((e for e in reversed(events) if e.get("type") == "_process"), None),
            "exit_status": parsed["exit_status"],
            "limit": parsed["limit"],
        }
    except NativeBillingError:
        raise
    except Exception as e:  # noqa: BLE001 -- writer/parser failure: an error record, no crash
        record["error"] = record["error"] or f"{type(e).__name__}: {e}"[:500]
    finally:
        if proj is not None:
            _rmtree_checked(proj, cleanup_errors)
        _rmtree_checked(cwd, cleanup_errors)
        if cleanup_errors:
            record["native"] = {**(record["native"] or {}), "cleanup_errors": cleanup_errors}
    return record


# ------------------------------------------------------------------------------------------
# Size test
# ------------------------------------------------------------------------------------------


def size_prefixes(
    rec: dict,
    cuts: tuple[int, ...] = SIZE_CUTS,
    bytes_per_token: float = HAIKU_MEASURED_BYTES_PER_TOKEN,
) -> list[dict]:
    """Prefixes of the replaced history at `cuts` actual tokens (rendered UTF-8 bytes / Haiku's
    measured 2.46), cut at message boundaries: the longest prefix whose rendered history is at
    most the cut. Cuts the whole history does not exceed are skipped. Each prefix is a record
    the cc_prompt arm runs on (its `s1_size` adds SIZE_LENGTH_LINE to the instruction)."""
    _r, blocks = _blocks(rec)
    msgs = _msgs(rec)
    order = sorted(blocks)
    cum, total = [], 0
    for k, i in enumerate(order):
        total += len(blocks[i].encode("utf-8")) + (2 if k else 0)
        cum.append(total)
    full_tokens = total / bytes_per_token
    out = []
    for cut in cuts:
        if full_tokens <= cut:
            continue
        limit = cut * bytes_per_token
        k = 0
        while k < len(cum) and cum[k] <= limit:
            k += 1
        if k == 0:
            continue
        n_msgs = order[k - 1] + 1
        p = copy.copy(history_rec(rec))
        p["source_messages"] = msgs[:n_msgs]
        p["id"] = f"{rec['id']}#prefix{cut // 1000}k"
        p["s1_size"] = {
            "base_rid": rec["id"],
            "cut_tokens": cut,
            "n_messages": n_msgs,
            "rendered_bytes": cum[k - 1],
            "est_tokens": round(cum[k - 1] / bytes_per_token),
            "length_line": SIZE_LENGTH_LINE,
        }
        out.append(p)
    return out


# ------------------------------------------------------------------------------------------
# Driver
# ------------------------------------------------------------------------------------------


def read_replays(path: Path, fingerprints: dict[str, str] | None = None) -> dict[str, dict]:
    """ReplayRecords by id. The last line for an id wins; the drivers keep that line current by
    re-appending a reused row (see `_reuse`). With `fingerprints` ({id: fingerprint}), each id
    gets its last row with that fingerprint instead (ids without one are left out)."""
    rows = [d for d in _read_jsonl(Path(path)) if "id" in d]
    if fingerprints is None:
        return {d["id"]: d for d in rows}
    return {d["id"]: d for d in rows if fingerprints.get(d["id"]) == d.get("fingerprint")}


def _reuse(out: Path, wanted: dict[str, str]) -> int:
    """For records skipped on resume ({id: current fingerprint}): when the last row for the id
    is not the successful row with that fingerprint (a run with other settings came between),
    re-append that row so "last row wins" readers get the current result. Returns the count."""
    rows = [d for d in _read_jsonl(Path(out)) if "id" in d]
    last = {d["id"]: d for d in rows}
    match = {}
    for d in rows:
        if not d.get("error") and wanted.get(d["id"]) == d.get("fingerprint"):
            match[d["id"]] = d
    again = [match[i] for i in wanted if i in match and last.get(i) is not match[i]]
    if again:
        _append(out, again)
    return len(again)


def _done_fingerprints(path: Path) -> set[tuple[str, str]]:
    """(id, fingerprint) of successful rows (any line, not only the last per id)."""
    return {
        (d["id"], d["fingerprint"])
        for d in _read_jsonl(Path(path))
        if not d.get("error") and d.get("fingerprint") and d.get("id")
    }


def repair_tail(path: Path) -> int:
    """Drop an incomplete final line (a crash mid-write) so the next append starts on a fresh
    line. Returns the number of bytes removed."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with open(path, "rb+") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return 0
        f.seek(0)
        data = f.read()
        keep = data.rfind(b"\n") + 1
        f.truncate(keep)
    removed = len(data) - keep
    log.warning("REPLAY_TORN_TAIL %s: dropped %d bytes of an incomplete last line", path, removed)
    return removed


def _append(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    repair_tail(path)
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


_PROMPTS_LOCK = threading.Lock()


def _record_provenance(out_dir: Path, entries: dict[str, dict]) -> None:
    """Add prompt provenance per fingerprint to <out_dir>/prompts.json; existing keys are never
    overwritten."""
    if not entries:
        return
    p = Path(out_dir) / "prompts.json"
    with _PROMPTS_LOCK:
        old = json.loads(p.read_text()) if p.exists() else {}
        if not isinstance(old, dict) or "by_fingerprint" not in old:
            old = {"by_fingerprint": {}}
        for k, v in entries.items():
            old["by_fingerprint"].setdefault(k, v)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(old, indent=1, ensure_ascii=False) + "\n")
        os.replace(tmp, p)


def _provenance(arm: str, rid: str, prompt_sha: str | None, files: list[dict]) -> dict:
    return {
        "arm": arm,
        "id": rid,
        "policy_version": policy_version(arm),
        "prompt_sha256": prompt_sha,
        "files": files,
        "model": MODEL,
        "written_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def check_backend(backend) -> None:
    """Part B holds the model fixed: refuse any backend not pinned to MODEL before dispatch."""
    m = getattr(backend, "model", None)
    if m != MODEL:
        raise ValueError(
            f"replays must run on {MODEL}; backend {getattr(backend, 'name', '?')!r}"
            f" is pinned to {m!r}"
        )


def _run_text_arm(recs: list[dict], arm: str, backend, out: Path, purpose: str) -> dict:
    check_backend(backend)
    gen = _generation(backend)
    done = _done_fingerprints(out)
    built = [(r, b, fingerprint(r, arm, b, gen)) for r in recs for b in [ARM_BUILDERS[arm](r)]]
    todo = [(r, b, fp) for r, b, fp in built if (replay_id(r, arm), fp) not in done]
    todo_ids = {replay_id(r, arm) for r, _, _ in todo}
    restored = _reuse(
        out, {replay_id(r, arm): fp for r, _, fp in built if replay_id(r, arm) not in todo_ids}
    )
    reqs = [q for r, b, _ in todo for q in requests_for(r, arm, b)]
    resps = backend.run(reqs, purpose=purpose) if reqs else {}
    rows = []
    for r, b, fp in todo:
        keys = [q.key for q in requests_for(r, arm, b)]
        rows.append(assemble(r, arm, b, [resps[k] for k in keys], fp))
    _append(out, rows)
    srcs = {s_["name"]: s_ for s_ in prompts_for(arm).sources}
    _record_provenance(
        out.parent,
        {
            fp: _provenance(arm, replay_id(r, arm), b.prompt_sha256, list(srcs.values()))
            for r, b, fp in todo
        },
    )
    return {
        "written": len(rows),
        "skipped": len(recs) - len(todo),
        "errors": sum(1 for x in rows if x["error"]),
        "restored": restored,
    }


def _load(rid: str) -> dict:
    from compaction import s1_data

    return s1_data.load(rid)


def _run_native(
    recs: list[dict], out: Path, out_dir: Path, workers: int, native_kwargs: dict
) -> dict:
    """Native compactions on a bounded pool. A usage-limit failure is a retryable error row and
    stops further launches (the rest are deferred to a rerun); a billing error stops the batch."""
    model = native_kwargs.get("model", MODEL)
    check_native_model(model)
    done = _done_fingerprints(out)
    fps = {replay_id(r, NATIVE_ARM): native_fingerprint(r, model) for r in recs}
    todo = [
        r for r in recs if (replay_id(r, NATIVE_ARM), fps[replay_id(r, NATIVE_ARM)]) not in done
    ]
    todo_ids = {replay_id(r, NATIVE_ARM) for r in todo}
    restored = _reuse(out, {i: fp for i, fp in fps.items() if i not in todo_ids})
    stop = threading.Event()
    counts = {
        "written": 0,
        "skipped": len(recs) - len(todo),
        "errors": 0,
        "deferred": 0,
        "restored": restored,
    }

    def work(r: dict) -> dict | None:
        if stop.is_set():
            return None
        row = native_compact(r, out_dir=out_dir, **native_kwargs)
        if (row.get("error") or "").startswith("limit"):
            stop.set()
        return row

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(work, r) for r in todo]
        try:
            for f in as_completed(futs):
                row = f.result()
                if row is None:
                    counts["deferred"] += 1
                    continue
                _append(out, [row])
                _record_provenance(
                    out_dir,
                    {
                        row["fingerprint"]: _provenance(
                            NATIVE_ARM,
                            row["id"],
                            row["prompt_sha256"],
                            [
                                {
                                    "name": "claude",
                                    "version": _claude_version(),
                                    "path": shutil.which("claude"),
                                    "sha256": row["prompt_sha256"],
                                }
                            ],
                        )
                    },
                )
                counts["written"] += 1
                counts["errors"] += bool(row["error"])
        except BaseException:
            stop.set()
            for f in futs:
                f.cancel()
            raise
    return counts


def run_replays(
    rids: list[str],
    arms: list[str],
    backend,
    *,
    out_dir: Path | None = None,
    load: Callable[[str], dict] | None = None,
    native_kwargs: dict | None = None,
    native_workers: int = 4,
) -> dict:
    """Run each arm on each record; append ReplayRecords to <out_dir>/<arm>.jsonl. A record is
    skipped only when a successful row with the same fingerprint exists (rerun = resume); prompt
    provenance is added per fingerprint to <out_dir>/prompts.json. Returns counts per arm."""
    out_dir = REPLAY_DIR if out_dir is None else Path(out_dir)
    load = load or _load
    unknown = [a for a in arms if a not in REPLAY_ARMS and a != NATIVE_ARM]
    if unknown:
        raise ValueError(f"unknown arms {unknown}")
    if any(a in REPLAY_ARMS for a in arms):
        check_backend(backend)
    if NATIVE_ARM in arms:
        check_native_model((native_kwargs or {}).get("model", MODEL))
    recs = [load(r) for r in rids]
    counts = {}
    for arm in arms:
        out = out_dir / f"{arm}.jsonl"
        if arm == NATIVE_ARM:
            counts[arm] = _run_native(recs, out, out_dir, native_workers, native_kwargs or {})
        else:
            counts[arm] = _run_text_arm(recs, arm, backend, out, f"replay_{arm}")
    return counts


def run_size(
    rids: list[str],
    backend,
    *,
    cuts: tuple[int, ...] = SIZE_CUTS,
    out_dir: Path | None = None,
    load: Callable[[str], dict] | None = None,
) -> dict:
    """The size test: the cc_prompt arm (with SIZE_LENGTH_LINE) on each record's prefixes;
    ReplayRecords (with a `size` field) appended to <out_dir>/size.jsonl."""
    out_dir = REPLAY_DIR if out_dir is None else Path(out_dir)
    load = load or _load
    prefixes = [p for r in rids for p in size_prefixes(load(r), cuts)]
    return _run_text_arm(prefixes, "cc_prompt", backend, out_dir / "size.jsonl", "replay_size")
