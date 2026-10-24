"""Experiment 2 close-out: Experiment 1's loss pipeline on the pilot's live boundaries.

Plan: docs/sprint/plans/experiment-2-closeout.md (units 1-3). S1's judge (`s1_judge.judge_records`,
passes 1a, 2 and 1b) and its second look (`s1_second_look`) run unchanged on each S2 boundary;
this module supplies the two hooks `judge_records` takes and adds S2's own columns.

Adapter (unit 1):
- `load(boundary_dir)` turns `boundary/record.json` into the S1 record shape. The record's
  `source_messages` (written by `compact.to_record_messages`) leave out the system prompt and the
  re-attached root AGENTS.md, so pass 1a would never see an agents_md-channel rule; the AGENTS.md
  message (Codex's wrapper, `compact.is_instruction`) from `boundary/pre_messages.json` is
  prepended as the first user message, as `s1_data.add_ptb_task_prompt` does for PostTrainBench.
  Also: `private` True (S2 histories go to Anthropic judges only), `session_group` = the DeepSWE
  task, `meta` None, `history_complete` from the boundary's completeness.
- `post_context(rec)`: what the agent resumed with, other than the summary, in record format:
  `boundary/post_messages.json` (OpenAI format) without the system prompt and the summary
  message (`boundary.json` `summary_index`), keeping the re-attached AGENTS.md, Codex's retained
  user turns and pi's kept tail. S1's `availability` then calls an item in any of them
  `still_present`. (S1's own `post_context` keeps only leading system messages and would stop
  before the AGENTS.md user message.)
- Planted rules: an item whose quote is (normalised) a substring of one of the run's
  `rules.json` lines, or contains one, is tagged with that rule id (`planted_rule`).
- Boundaries: `retention.boundary_dirs` (the latest complete attempt per cohort run, so the
  invalid first Haiku scriggo attempt is left out).

Recoverability (unit 3), for every lost item (`lost`: valid, graded dropped or misstated after
the second look, and not still present in the resumed context). The pilot kept no repo snapshot
at its boundaries, so "could the agent re-find it in the repo?" is reconstructed from the task's
base image and the agent's recorded commands. The quote's key strings are its full lines
(`quote_lines`: unescaped, grep/nl prefixes stripped); `key_strings` fragments only ever widen
the search. The files an item concerns: base-repo files holding every full line (`git grep -F`
in the DeepSWE image's /app), files whose read output holds the quote (`attributable_paths`:
the whole output came from file reads, the command succeeded; their content at that read is
known), the committed rule file of a planted readme/agents_md rule, and files the agent itself
wrote, whole, with a heredoc (`heredoc_writes`) holding every full line. Then:
  recoverable      - in one of those files, the content we know (base, read or the agent's own
                     write) holds the key strings and no command after that point may have
                     changed the file, or the agent's last write of the file is a replayable
                     whole-file write holding them;
  unresolved       - a file the item concerns was changed by a command we can't replay (sed -i,
                     a script, a formatter or generator, a tree-changing git command, an
                     append, a glob...), or only fragments of the quote are in a file (whitespace
                     may differ), or an unreplayable command itself contains the key strings;
  not_recoverable  - in none of them, and no recorded command could have produced them.
"May have changed": a later command that is not harmless (`harmless`: reads, inert commands,
test and build runs) and names the path, its basename or a directory above it at path
boundaries, absolute paths included (`may_write`), or any broad writer (`broad_write`). This
over-calls changes, so it errs toward unresolved rather than recoverable; Jacob confirms each
candidate by hand. Evidence on every lost row: `recoverable_lines` ({path, line, text} of
matching base-repo lines, at most 5, text at most 300 characters), `recoverable_commands`
({msg, command} of the recorded commands that touched the files, at most 5, 300 characters)
and `recoverable_how`, a short reason.

The base-image search runs in one container per image through `compaction.deepswe.sandbox`
(`--rm`, a name, the label emx.sandbox=s2-lossaudit plus a per-invocation run label, `--network
none`, an in-container `timeout -k 5`, `docker rm -f` with confirmation on a client timeout or an
interruption). It only reads. The runner is injectable (`GrepRunner`) for tests.

Output (`results/sprint/S2/lossaudit/`): items.jsonl, claims.jsonl, code_facts.jsonl (S1's
rows), second_look.jsonl (S1's overlay rows, stamped with the frozen config digest) and
losses.jsonl: one row per item, `s1_second_look.apply` applied, plus `LOSS_EXTRA_FIELDS`. No
sealed arm data is read: only `boundary/`, `rules.json` and `run.json` of each attempt.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from compaction import s1_judge as J
from compaction import s1_second_look as SL
from compaction import s1_types as T
from compaction.s2 import compact as K
from compaction.s2 import config as C
from compaction.s2 import retention as RT

OUT_DIR = C.REPO_ROOT / "results" / "sprint" / "S2" / "lossaudit"
PURPOSE = "s2_lossaudit"  # the Max backend's cache file stem
SANDBOX_PURPOSE = "s2-lossaudit"
LOST_GRADES = ("dropped", "misstated")
RECOVERABLE = ("recoverable", "not_recoverable", "unresolved")
EVIDENCE_MAX = 5  # entries of recoverable_lines / recoverable_commands
EVIDENCE_TEXT_MAX = 300  # characters of each line text / command
GREP_LINES = 20  # matching lines kept per pattern
GREP_TIMEOUT_S = 600
GREP_SCRIPT_MAX = 60_000  # bytes of bash per exec (well under Linux's 128 KiB argument limit)
KEY_MIN = 12  # characters of a key string, after normalising
KEY_MAX = 160
KEYS_PER_QUOTE = 3
PLANTED_MIN = 20  # a quote shorter than this tags a rule only if it is the whole line
LOSS_EXTRA_FIELDS = (
    "second_look",
    "run_key",
    "cohort_key",
    "task",
    "channel",
    "subject",
    "policy",
    "half",
    "planted_rule",
    "first_grade",
    "lost",
    "recoverable",
    "recoverable_how",
    "recoverable_lines",
    "recoverable_commands",
    "boundary_dir",
)
LOSS_ROW_FIELDS = J.ITEM_ROW_FIELDS + LOSS_EXTRA_FIELDS

# grep_runner(image, patterns) -> {pattern: {"files": [repo-relative files holding it],
#                                             "lines": [{"path", "line", "text"}, ...]}}
GrepRunner = Callable[[str, list[str]], dict[str, dict]]


def _read(p: Path) -> Any:
    return json.loads(Path(p).read_text())


def _rel(p: Path) -> str:
    p = Path(p).resolve()
    try:
        return str(p.relative_to(C.REPO_ROOT))
    except ValueError:
        return str(p)


# ------------------------------------------------------------------------------------------
# Adapter (unit 1)
# ------------------------------------------------------------------------------------------


def record_messages(messages: list[dict], skip: Iterable[int] = ()) -> list[dict]:
    """OpenAI-format messages -> compaction.schema Messages, as `compact.to_record_messages`
    but keeping the instruction (AGENTS.md) message and dropping only the system prompt and the
    indices in `skip`. Each carries `_i`, its index in `messages`."""
    skip = set(skip)
    out = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system" or i in skip:
            continue
        rec: dict = {"role": role, "text": K._text(m.get("content")), "_i": i}
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = fn.get("arguments")
                calls.append({"name": fn.get("name"), "args": args, "id": tc.get("id")})
            rec["tool_calls"] = calls
        if role == "tool":
            rec["tool_use_id"] = m.get("tool_call_id")
        out.append(rec)
    return out


def _instruction(messages: Any) -> dict | None:
    if not isinstance(messages, list):
        return None
    return next((m for m in messages if isinstance(m, dict) and K.is_instruction(m)), None)


def _json_or_none(p: Path) -> Any:
    try:
        return _read(p)
    except (OSError, ValueError):
        return None


def load(boundary_dir: Path) -> dict:
    """The S1 record for one attempt dir (module docstring). Raises FileNotFoundError when
    record.json is missing, ValueError when it isn't JSON, TypeError when it isn't a record."""
    a = Path(boundary_dir)
    b = a / "boundary"
    rec = _read(b / "record.json")
    if not isinstance(rec, dict) or not isinstance(rec.get("source_messages"), list):
        raise TypeError(f"{b / 'record.json'}: not a compaction record")
    pre = _json_or_none(b / "pre_messages.json")
    post = _json_or_none(b / "post_messages.json")
    instr = _instruction(pre) or _instruction(post)
    src = list(rec["source_messages"])
    if instr is not None:
        src = [{"role": "user", "text": K._text(instr.get("content"))}, *src]
    rec["source_messages"] = src
    rec["agents_md_prepended"] = instr is not None
    run = _json_or_none(a / "run.json") or {}
    plan = run.get("plan") or {}
    task = (run.get("task") or {}).get("task_id") or plan.get("task")
    meta = _json_or_none(b / "boundary.json")
    meta = meta if isinstance(meta, dict) else {}
    si = meta.get("summary_index")
    post_ok = isinstance(post, list) and all(isinstance(m, dict) for m in post) and bool(post)
    si_ok = post_ok and isinstance(si, int) and 0 <= si < len(post)
    rec["meta"] = None
    rec["private"] = True
    rec["session_group"] = task
    rec["history_complete"] = bool(
        src and (rec.get("summary") or "").strip() and post_ok and si_ok)
    rec["s2"] = {
        "boundary_dir": _rel(a),
        "run_key": RT.run_key(a),
        "cohort_key": RT.cohort_key(a),
        "task": task,
        "channel": RT.channel_of(a),
        "subject": plan.get("model"),
        "policy": plan.get("policy"),
        "image": (run.get("task") or {}).get("image"),
        "summary_index": si if si_ok else None,
    }
    rec["s2_post_context"] = (
        [{k: v for k, v in m.items() if k != "_i"} for m in record_messages(post, skip=[si])]
        if si_ok else [])
    return rec


def post_context(rec: dict) -> list[dict]:
    """The resumed context other than the summary (module docstring), computed by `load`."""
    return list(rec.get("s2_post_context") or [])


class Index:
    """rid -> attempt dir, so `judge_records` and the second look can load by record id."""

    def __init__(self, dirs: Iterable[Path]):
        self.dirs: dict[str, Path] = {}
        self._cache: dict[str, dict] = {}
        for d in dirs:
            rid = str(_read(Path(d) / "boundary" / "record.json")["id"])
            if rid in self.dirs:
                raise ValueError(f"duplicate record id {rid} ({self.dirs[rid]}, {d})")
            self.dirs[rid] = Path(d)

    @property
    def rids(self) -> list[str]:
        return sorted(self.dirs)

    def load(self, rid: str) -> dict:
        if rid not in self._cache:
            self._cache[rid] = load(self.dirs[rid])
        return self._cache[rid]


def boundaries(raw_root: Path | None = None, only: Iterable[str] | None = None) -> list[Path]:
    """The latest complete attempt per cohort run (`retention.boundary_dirs`), optionally only
    the run keys or cohort keys in `only`."""
    dirs = RT.boundary_dirs(Path(raw_root) if raw_root else C.raw_dir())
    only = set(only or ())
    if only:
        dirs = [d for d in dirs if RT.run_key(d) in only or RT.cohort_key(d) in only]
    return dirs


def boundary_view(boundary_dir: Path) -> dict:
    """What a card shows for one boundary: the summary, the resumed input (each message with
    its kind: system prompt, instruction file, kept, summary) and the history the judge saw
    (with the AGENTS.md prepend)."""
    a = Path(boundary_dir)
    rec = load(a)
    post = _json_or_none(a / "boundary" / "post_messages.json") or []
    si = rec["s2"]["summary_index"]
    return {
        "run_key": rec["s2"]["run_key"],
        "summary": rec.get("summary") or "",
        "post_messages": [
            {"i": i, "role": m.get("role"), "kind": RT.message_kind(i, m, si),
             "text": RT.message_text(m)}
            for i, m in enumerate(post) if isinstance(m, dict)
        ],
        "history": [
            {"i": i, "role": m.get("role"), "text": J._message_blob(m)}
            for i, m in enumerate(rec["source_messages"])
        ],
    }


def planted_rule(quote: str | None, rules_lines: dict[str, str]) -> str | None:
    """The planted rule an item quotes (module docstring), else None."""
    if not quote:
        return None
    q = T.norm(quote)
    for rid, line in rules_lines.items():
        ln = T.norm(line or "")
        if not ln:
            continue
        if (len(q) >= min(PLANTED_MIN, len(ln)) and q in ln) or ln in q:
            return rid
    return None


# ------------------------------------------------------------------------------------------
# Shell commands: which files a command reads and which it may write
# ------------------------------------------------------------------------------------------

_SEP = {"&&", "||", ";", "\n"}
READERS = frozenset({"cat", "head", "tail", "less", "more", "nl"})
SEARCHERS = frozenset({"grep", "rg", "egrep", "fgrep"})
FILTERS = READERS | SEARCHERS | {"wc", "tr", "cut"}  # pipe stages after the first: stdin only
INERT = frozenset({"pwd", "echo", "printf", "ls", "true", "wc"})  # no redirection allowed
FIND_WRITES = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint",
                         "-fprintf", "-fls"})
GIT_READ = frozenset({"status", "log", "diff", "show", "branch", "rev-parse", "grep",
                      "ls-files", "blame", "remote", "describe"})
# git subcommands that leave the working tree as it is
GIT_TREE_SAFE = GIT_READ | {"add", "commit", "config", "fetch", "tag", "push"}
# writers that may touch files they don't name (a write of a named file, or of a directory
# above it, is caught by `may_write`)
_BROAD = re.compile(
    r"\b(?:gofmt\s+-[a-z]*w|go\s+fmt|goimports\s+-w|go\s+generate|go\s+mod\b|cargo\s+fmt"
    r"|cargo\s+update|rustfmt|prettier\b[^\n]*--write|eslint\b[^\n]*--fix|black\b"
    r"|ruff\s+format|ruff\b[^\n]*--fix|patch\b|perl\s+-[a-z]*i|make\b|npx\b"
    r"|(?:npm|yarn|pnpm)\s+(?!test\b|t\b)\w+|xargs\b|find\b[^\n]*-(?:exec|delete)\b"
    r"|(?:python3?|node|ruby|perl|bash|sh|deno|bun)\s+(?!-c\b|-e\b)[^\s|;&]*\.(?:py|js|mjs|ts|rb|pl|sh)\b)")
_INTERPRETER_HEREDOC = re.compile(r"\b(?:python3?|node|ruby|perl|deno|bun|bash|sh)\b[^\n]*<<")


def _tokens(cmd: str) -> list[str] | None:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _segments(cmd: str) -> list[list[str]] | None:
    """Pipelines split at && || ; and newlines: [[words of stage 1, '|', words of stage 2 ...]]."""
    out: list[list[str]] = []
    for line in cmd.split("\n"):
        toks = _tokens(line)
        if toks is None:
            return None
        cur: list[str] = []
        for t in toks:
            if t in _SEP:
                if cur:
                    out.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            out.append(cur)
    return out


def _resolve(path: str, cwd: str) -> str:
    p = posixpath.normpath(posixpath.join(cwd, path))
    if p == "/app":
        return "."
    return p.removeprefix("/app/")


# options that take a value, by program family
READER_VALUE_FLAGS = frozenset({"-n", "-c", "--lines", "--bytes"})
SEARCH_VALUE_FLAGS = frozenset({"-e", "-f", "-m", "-A", "-B", "-C", "-g", "-t", "--max-count",
                                "--context", "--glob", "--type"})
_HARMLESS_REDIRECT = re.compile(r"\s*(?:\d?>\s*/dev/null|2>&1|\d?>&\d)")


def _words_paths(words: list[str], skip_first: int = 1,
                 value_flags: frozenset = READER_VALUE_FLAGS) -> list[str]:
    """Positional words after `skip_first`, without flags, their values (`-n 50`) and bare
    numbers."""
    out, skip = [], False
    for w in words[skip_first:]:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            skip = w in value_flags
            continue
        if not re.fullmatch(r"\d+", w):
            out.append(w)
    return out


def _search_paths(words: list[str]) -> list[str]:
    """grep/rg: the paths (the pattern is the first positional unless -e/-f gives it)."""
    pos = _words_paths(words, value_flags=SEARCH_VALUE_FLAGS)
    return pos if any(w in ("-e", "-f") for w in words) else pos[1:]


def _sed_print(words: list[str]) -> bool:
    return len(words) >= 3 and words[1] == "-n" and bool(J._SED_PRINT.fullmatch(words[2]))


def parse_reads(cmd: str) -> tuple[list[str], bool] | None:
    """For a command that only reads, (the repo-relative files it reads, whether its whole
    output comes from those files); None when it may do anything else or can't be parsed.

    `cd` moves the working directory (start /app). A read is a pipeline that starts with a
    reader (cat/head/tail/nl/less/more), a `sed -n` print or a search (grep/rg; a recursive
    search with no path reads the working directory) and continues with readers, searches,
    `sed -n` prints or stdin-only filters (wc/tr/cut, sort without -o, uniq without files).
    echo/printf/ls/pwd/wc segments, `find` without -exec/-delete/-fprint and read-only git
    subcommands are allowed, but their output is not from a file, so they make
    the command's output not attributable. Any redirection, subshell, command substitution or
    heredoc makes it not a read."""
    if not cmd or "`" in cmd or "$(" in cmd or "<<" in cmd:
        return None
    segs = _segments(_HARMLESS_REDIRECT.sub("", cmd))
    if not segs:
        return None
    cwd, paths, attributable = "/app", [], True
    for seg in segs:
        if any(t and all(ch in "<>&()" for ch in t) for t in seg):
            return None  # a redirection or subshell
        stages: list[list[str]] = [[]]
        for t in seg:
            if t == "|":
                stages.append([])
            else:
                stages[-1].append(t)
        first = stages[0]
        if not first or not all(stages):
            return None
        prog = posixpath.basename(first[0])
        if prog == "cd":
            if len(stages) > 1 or len(first) > 2:
                return None
            cwd = posixpath.normpath(posixpath.join(cwd, first[1] if len(first) > 1 else "/app"))
            continue
        if prog in READERS:
            got = _words_paths(first)
        elif prog == "sed":
            if not _sed_print(first):
                return None
            got = _words_paths(first, 3)
        elif prog in SEARCHERS:
            got = _search_paths(first)
            if not got and any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", w) for w in first):
                got = ["."]
        elif (prog in INERT or (prog == "git" and len(first) > 1 and first[1] in GIT_READ)
              or (prog == "find" and not FIND_WRITES & set(first))):
            got = []
            attributable = False
        else:
            return None
        if prog not in INERT and prog != "git" and not got:
            attributable = False  # a reader on stdin: nothing to attribute
        for st in stages[1:]:
            p2 = posixpath.basename(st[0])
            if p2 == "sed":
                if not _sed_print(st):
                    return None
            elif p2 in ("sort", "uniq"):
                if any(w == "-o" or w.startswith("--output") for w in st) or (
                        p2 == "uniq" and _words_paths(st)):
                    return None  # sort -o FILE / uniq IN OUT write a file
            elif p2 not in FILTERS:
                return None
            elif (_search_paths(st) if p2 in SEARCHERS else
                  _words_paths(st) if p2 in READERS else []):
                attributable = False  # a later stage reading files of its own mixes the output
        paths += [_resolve(p, cwd) for p in got]
    return paths, attributable and bool(paths)


def read_paths(cmd: str) -> list[str] | None:
    """The repo-relative files a pure-read command reads ([] for e.g. `git status`), or None
    when the command may do anything else (`parse_reads`)."""
    r = parse_reads(cmd)
    return None if r is None else r[0]


def attributable_paths(cmd: str) -> list[str] | None:
    """The files a command reads when its whole output comes from them, else None."""
    r = parse_reads(cmd)
    return r[0] if r is not None and r[1] else None


def _bounded(name: str, text: str, *, directory: bool = False) -> bool:
    """`name` in `text` at path boundaries; a leading `/` or `./` is allowed, so an absolute
    /app/<path> counts. A directory counts only as itself (`src`, `src/`), not as the start of a
    longer path (`src/other.go`)."""
    after = r"(?![\w.-]|/[\w.*-])" if directory else r"(?![\w.-])"
    return bool(re.search(r"(?<![\w.-])" + re.escape(name) + after, text))


def _names(path: str) -> list[tuple[str, bool]]:
    """(name, is a directory): the path, its basename and every directory above it (a write to
    `src` may change `src/a.go`)."""
    parts = [p for p in path.split("/") if p not in ("", ".")]
    out = [("/".join(parts), False)] if parts else []
    if len(parts) > 1:
        out.append((parts[-1], False))
    out += [("/".join(parts[:k]), True) for k in range(len(parts) - 1, 0, -1)]
    return out


# test and build runs that don't change source files (their segments may be piped to filters)
_SAFE_RUN = re.compile(
    r"(?:go\s+(?:test|vet|version|env|list)|go\s+build(?!\S*\s+-o)|cargo\s+(?:test|build|check)"
    r"|cargo\s+clippy(?![^\n]*--fix)|(?:npm|yarn|pnpm)\s+(?:test|t)|pytest|python3?\s+-m\s+pytest"
    r"|tsc\s+--noEmit|node\s+--version|which|type|env|uname|date|sleep|git\s+--no-pager\s+"
    r"(?:log|diff|show|status))\b")


def harmless(cmd: str) -> bool:
    """A pure read, or every segment a read, an inert command or a test/build run, with no
    redirection to a file, heredoc, command substitution or glob."""
    if read_paths(cmd) is not None:
        return True
    if not cmd or "`" in cmd or "$(" in cmd or "<<" in cmd or re.search(r"[*?]", cmd):
        return False
    segs = _segments(_HARMLESS_REDIRECT.sub("", cmd))
    if not segs:
        return False
    for seg in segs:
        if any(t and all(ch in "<>&()" for ch in t) for t in seg):
            return False
        text = " ".join(seg)
        if read_paths(text) is not None:
            continue
        head = text.split("|", 1)[0].strip()
        if not _SAFE_RUN.match(head) or (
                "|" in text and read_paths("cat x | " + text.split("|", 1)[1]) is None):
            return False
    return True


def may_write(cmd: str, path: str) -> bool:
    """True when `cmd` is not harmless (`harmless`) and names `path`, its basename or a
    directory above it at path boundaries (module docstring: errs toward a write)."""
    if harmless(cmd):
        return False
    return any(_bounded(n, cmd, directory=d) for n, d in _names(path))


def _git_tree_change(cmd: str) -> bool:
    segs = _segments(cmd) or []
    for seg in segs:
        for i, w in enumerate(seg):
            if posixpath.basename(w) != "git":
                continue
            rest = [x for x in seg[i + 1:] if x != "|"]
            j = 0
            while j < len(rest) and rest[j].startswith("-"):  # git -C dir / -c k=v
                j += 2 if rest[j] in ("-C", "-c") else 1
            sub = rest[j] if j < len(rest) else ""
            args = rest[j + 1:]
            if sub in GIT_TREE_SAFE:
                continue
            if sub == "checkout" and args[:1] in (["-b"], ["-B"]) and len(args) <= 2:
                continue  # a new branch at HEAD
            if sub == "switch" and args[:1] in (["-c"], ["-C"]) and len(args) <= 2:
                continue
            if sub == "stash" and args[:1] == ["list"]:
                continue
            return True
    return segs is None and "git" in cmd


def broad_write(cmd: str) -> bool:
    """True when a command that is not a pure read may change files it doesn't name: a
    formatter or fixer, a build or script runner, a tree-changing git command (checkout of an
    existing branch, merge, pull, reset, stash, apply...), find -exec/-delete, xargs, an
    interpreter fed a heredoc, or a glob."""
    if not cmd or harmless(cmd):
        return False
    return bool(_BROAD.search(cmd) or _INTERPRETER_HEREDOC.search(cmd)
                or _git_tree_change(cmd) or re.search(r"[*?]", cmd))


def _commands(msgs: list[dict]) -> list[tuple[int, str]]:
    """(message index, shell command) for every tool call in the history, in order."""
    out = []
    for i, m in enumerate(msgs):
        for c in m.get("tool_calls") or []:
            cmd = J.command_of(c)
            if cmd:
                out.append((i, cmd))
    return out


# ------------------------------------------------------------------------------------------
# Recoverability (unit 3)
# ------------------------------------------------------------------------------------------


_ESC = re.compile(r"\\(u[0-9a-fA-F]{4}|[nt\"\\/])")
_ESC_MAP = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


def _unescape(s: str) -> str:
    """S2's tool messages are JSON text, so quotes from them carry \\n, \\" and \\u escapes;
    decode those (and only those)."""
    if "\\" not in s:
        return s
    return _ESC.sub(lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u"
                    else _ESC_MAP[m.group(1)], s)


_LINE_PREFIX = re.compile(r"^\s*(?:[\w./-]+:\d+[:-]|\d+[:\s-]\s?)")


def _clean_lines(quote: str) -> list[str]:
    return [_LINE_PREFIX.sub("", line) for line in _unescape(quote).splitlines()]


def quote_lines(quote: str | None) -> list[str]:
    """Every line of the quote (unescaped, grep/nl line prefixes stripped, trimmed) that has a
    letter or digit: the strings that must all be in one file for a "yes" from the base repo."""
    if not quote:
        return []
    out = []
    for line in _clean_lines(quote):
        line = line.strip()
        if re.search(r"[A-Za-z0-9]", line) and line not in out:
            out.append(line)
    return out


def key_strings(quote: str | None) -> list[str]:
    """Up to KEYS_PER_QUOTE distinctive fragments of a quote: per line, the longest run between
    tabs or double spaces, kept when >= KEY_MIN characters, longest first, at most KEY_MAX each.
    Fragment hits are only ever "approximate" evidence."""
    if not quote:
        return []
    frags = []
    for line in _clean_lines(quote):
        parts = [p.strip() for p in re.split(r"\t|\s{2,}", line) if p.strip()]
        if not parts:
            continue
        best = max(parts, key=len)[:KEY_MAX].strip()
        if len(best) >= KEY_MIN and best not in frags:
            frags.append(best)
    frags.sort(key=len, reverse=True)
    return frags[:KEYS_PER_QUOTE]


def search_patterns(quote: str | None) -> list[str]:
    """What the base-image search looks for: the full lines and the fragments."""
    return list(dict.fromkeys(quote_lines(quote) + key_strings(quote)))


def _ok_output(text: str) -> bool:
    """A tool result that is not a failed command (S2 tool messages are JSON with a returncode;
    other text is taken as is)."""
    try:
        d = json.loads(text)
    except ValueError:
        return True
    return not isinstance(d, dict) or d.get("returncode") in (0, None)


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_CAT_TARGET = re.compile(r"\bcat\b[^\n|;&]*?(>>?)\s*([^\s<>|;&]+)")
_TEE_TARGET = re.compile(r"\btee\s+(-a\s+)?([^\s<>|;&]+)")
_CD = re.compile(r"(?:^|[;&|]\s*)cd\s+([^\s;&|]+)")


def heredoc_writes(cmd: str) -> list[tuple[str, str]]:
    """(repo-relative path, full new content) for each whole-file write by `cat > f <<EOF` /
    `cat <<EOF > f` / `tee f <<EOF` in `cmd`: the writes we can replay. Appends (`>>`,
    `tee -a`) are not whole-file writes and are left out (they count as unreplayable)."""
    out = []
    lines = cmd.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC.search(line)
        if not m:
            i += 1
            continue
        tag = m.group(2)
        end = next((k for k in range(i + 1, len(lines)) if lines[k].strip() == tag), None)
        if end is None:
            break
        body = "\n".join(lines[i + 1:end]) + "\n"
        prior = "\n".join(lines[:i + 1])
        cwds = _CD.findall(prior)
        cwd = posixpath.normpath(posixpath.join("/app", cwds[-1])) if cwds else "/app"
        cat, tee = _CAT_TARGET.search(line), _TEE_TARGET.search(line)
        if cat and cat.group(1) == ">":
            out.append((_resolve(cat.group(2).strip("'\""), cwd), body))
        elif tee and not tee.group(1):
            out.append((_resolve(tee.group(2).strip("'\""), cwd), body))
        i = end + 1
    return out


def _lines_in(lines: list[str], text: str) -> bool:
    return bool(lines) and all(ln in text for ln in lines)


def _cut(s: str, n: int = EVIDENCE_TEXT_MAX) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class _Files:
    """The history's commands, and per file the ones that may have changed it."""

    def __init__(self, msgs: list[dict]):
        self.msgs = msgs
        self.cmds = _commands(msgs)
        self.broad = [(j, c) for j, c in self.cmds if broad_write(c)]
        self.replay = {j: dict(heredoc_writes(c)) for j, c in self.cmds}

    def touches(self, path: str, after: int = -1) -> list[tuple[int, str]]:
        """Commands after message `after` that may have changed `path`: naming writes and broad
        writes, in order."""
        return [(j, c) for j, c in self.cmds
                if j > after and (may_write(c, path) or broad_write(c)
                                  or path in self.replay.get(j, {}))]

    def last_write(self, path: str) -> tuple[int, str] | None:
        """(message, content) of the replayable whole-file write that is the last command to
        touch `path`, or None (never touched, or last touched by something we can't replay)."""
        t = self.touches(path)
        if not t:
            return None
        j, c = t[-1]
        content = self.replay.get(j, {}).get(path)
        if content is None or broad_write(c):
            return None
        return j, content


def recoverable(row: dict, rec: dict, hits: dict[str, dict] | None) -> dict:
    """{recoverable, recoverable_how, recoverable_lines, recoverable_commands} for one lost item
    row (module docstring). `hits` is the base-image search for the row's image ({pattern:
    {"files", "lines"}}), None when the search was skipped."""
    msgs = J.history_messages(rec)
    F = _Files(msgs)
    quote = row.get("quote")
    lines, frags = quote_lines(quote), key_strings(quote)
    concerned: dict[str, int] = {}  # file -> message after which its content is known (-1: base)
    found_lines: list[dict] = []
    mixed: list[tuple[str, int]] = []

    def files_of(pats):
        sets = [set((hits or {}).get(p, {}).get("files") or ()) for p in pats]
        return (set.intersection(*sets) if sets else set()), sets

    full, _ = files_of(lines)
    frag, frag_sets = files_of(frags)
    for f in sorted(full):
        concerned.setdefault(f, -1)
    for p in lines + frags:
        for h in (hits or {}).get(p, {}).get("lines") or ():
            x = {"path": h["path"], "line": h.get("line"), "text": _cut(h.get("text") or "")}
            if h["path"] in full | frag and x not in found_lines and len(
                    found_lines) < EVIDENCE_MAX:
                found_lines.append(x)
    # (a) a read whose output holds the quote: the file's content at that point is known
    calls = J.calls_for_results(msgs)
    for i, m in enumerate(msgs):
        call = calls.get(i)
        text = m.get("text") or ""
        if call is None or not T.quote_in(quote, text):
            continue
        cmd = J.command_of(call)
        paths = attributable_paths(cmd)
        if paths and _ok_output(text):
            for p in paths:
                if p not in concerned or concerned[p] < i:
                    concerned[p] = i
        elif read_paths(cmd):
            # the quote may come from an echo or a failed command, not the file: never recoverable
            mixed += [(p, i) for p in read_paths(cmd)]
    # (c) a planted rule of the readme or agents_md channel: its file is committed
    rule_file = RT.RULE_FILE.get(row.get("channel") or "")
    if row.get("planted_rule") and rule_file:
        concerned.setdefault(rule_file, -1)
    # the agent's own replayable writes whose content holds the quote
    for j, writes in sorted(F.replay.items()):
        for p, content in writes.items():
            if _lines_in(lines, content):
                concerned.setdefault(p, j - 1)

    def commands_for(paths) -> list[dict]:
        seen, out = set(), []
        for p in paths:
            for j, c in F.touches(p):
                if (j, c) not in seen and len(out) < EVIDENCE_MAX:
                    seen.add((j, c))
                    out.append({"msg": j, "command": _cut(c)})
        return out

    recovered, unresolved = [], []
    for f, since in sorted(concerned.items()):
        later = F.touches(f, after=since)
        if not later:
            recovered.append((f, "untouched" if since < 0 else f"read@m{since}"))
            continue
        lw = F.last_write(f)
        if lw is not None and _lines_in(lines, lw[1]):
            recovered.append((f, f"agent_last_write@m{lw[0]}"))
        elif lw is not None:
            continue  # its last write is known and doesn't hold the quote: not in that file
        else:
            unresolved.append((f, f"changed@m{later[-1][0]}"))
    unresolved += [(f, f"mixed_output_read@m{i}") for f, i in mixed]
    for f in sorted(frag - full):
        if F.touches(f):
            unresolved.append((f, "fragments_in_changed_file"))
        else:
            unresolved.append((f, "fragments_only"))
    for j, c in F.cmds:
        if frags and read_paths(c) is None and not F.replay.get(j) and all(
                k in _unescape(c) for k in frags):
            unresolved.append(("?", f"unreplayable_command_contains_quote@m{j}"))
            break
    if recovered:
        cls, files = "recoverable", [f for f, _ in recovered]
        how = ";".join(f"{f}:{w}" for f, w in recovered[:3])
    elif unresolved:
        cls, files = "unresolved", [f for f, _ in unresolved if f != "?"]
        how = ";".join(f"{f}:{w}" for f, w in unresolved[:3])
    else:
        cls, files = "not_recoverable", sorted(concerned)
        how = "not_in_base_repo_or_writes" if hits is not None else "history_only:no_evidence"
        if any(frag_sets):
            how += ";partial_fragment_hits"
    return {"recoverable": cls, "recoverable_how": _cut(how, 200),
            "recoverable_lines": found_lines, "recoverable_commands": commands_for(files)}


def docker_grep(image: str, patterns: list[str], *, host: str | None = None) -> dict:
    """For each pattern, the files of the image's /app holding it (`git grep -F -l`, complete)
    and up to GREP_LINES of its matching lines (`git grep -F -n`), in one container (--rm,
    named, labelled emx.sandbox=s2-lossaudit, --network none, in-container timeouts; removed and
    confirmed by `Invocation` on exit): {pattern: {"files": [...], "lines": [{path, line,
    text}]}}. Raises SandboxError when a search fails."""
    from compaction.deepswe.sandbox import Invocation, SandboxError

    out: dict[str, dict] = {}
    with Invocation(purpose=SANDBOX_PURPOSE, host=host) as inv:
        sb = inv.start(image, lifetime_s=GREP_TIMEOUT_S + 600, cpus=2, memory_mb=4096)
        for chunk in _script_chunks(patterns):
            r = sb.exec(_grep_script(chunk), timeout_s=GREP_TIMEOUT_S)
            if r.rc != 0:
                raise SandboxError(f"grep script rc={r.rc}: {r.stderr[-500:]}")
            out.update(parse_grep_output(r.stdout, chunk))
    return out


def _grep_script(patterns: list[str]) -> str:
    g = "git -c safe.directory='*' grep -F -I"
    lines = ["cd /app || exit 3"]
    for n, p in enumerate(patterns):
        q = shlex.quote(p)
        lines.append(f"echo '@@Q {n}'")
        lines.append(f"{g} -l -e {q} -- . ; echo \"@@E {n} $?\"")
        lines.append(f"echo '@@L {n}'")
        lines.append(f"{g} -n -e {q} -- . | head -n {GREP_LINES} ; "
                     f"echo \"@@F {n} ${{PIPESTATUS[0]}}\"")
    return "\n".join(lines) + "\n"


def _script_chunks(patterns: list[str]) -> list[list[str]]:
    out, cur, size = [], [], 0
    for p in patterns:
        s = len(p.encode()) + 120
        if cur and size + s > GREP_SCRIPT_MAX:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += s
    if cur:
        out.append(cur)
    return out


_GREP_LINE = re.compile(r"(.+?):(\d+):(.*)")


def parse_grep_output(stdout: str, patterns: list[str]) -> dict[str, dict]:
    """{pattern: {"files", "lines"}}; a file search ending with rc not in (0, 1), or a line
    search with rc not in (0, 1, 141: `head` closed the pipe), raises."""
    from compaction.deepswe.sandbox import SandboxError

    out: dict[str, dict] = {}
    mode, cur, buf = None, None, []
    for line in stdout.splitlines():
        if m := re.fullmatch(r"@@([QL]) (\d+)", line):
            mode, cur, buf = m.group(1), int(m.group(2)), []
        elif m := re.fullmatch(r"@@([EF]) (\d+) (\d+)", line):
            kind, n, rc = m.group(1), int(m.group(2)), int(m.group(3))
            ok = (0, 1) if kind == "E" else (0, 1, 141)
            if n != cur or rc not in ok or mode != {"E": "Q", "F": "L"}[kind]:
                raise SandboxError(f"git grep failed for pattern {n} (rc {rc})")
            d = out.setdefault(patterns[n], {"files": None, "lines": None})
            if kind == "E":
                d["files"] = [x.removeprefix("./") for x in buf]
            else:
                d["lines"] = []
                for x in buf:
                    if g := _GREP_LINE.fullmatch(x):
                        d["lines"].append({"path": g.group(1).removeprefix("./"),
                                           "line": int(g.group(2)), "text": g.group(3)})
            mode, cur = None, None
        elif cur is not None and line:
            buf.append(line)
    missing = [p for p in patterns
               if p not in out or out[p]["files"] is None or out[p]["lines"] is None]
    if missing:
        raise SandboxError(f"git grep output incomplete: {len(missing)} pattern(s) missing")
    return out


# ------------------------------------------------------------------------------------------
# Loss rows
# ------------------------------------------------------------------------------------------


def is_lost(row: dict) -> bool:
    return bool(row.get("valid") and row.get("grade_valid")
                and row.get("grade") in LOST_GRADES
                and row.get("availability") != "still_present")


def loss_rows(items: list[dict], overlay: list[dict], index: Index, *,
              split_rec: dict | None = None,
              grep_runner: GrepRunner | None = None) -> tuple[list[dict], dict]:
    """losses.jsonl rows (module docstring) and a summary of the base-image searches.
    `grep_runner` None skips the base-image search (recoverable_how then says history_only
    when nothing else is found)."""
    final = SL.apply(items, overlay)
    first = {SL.row_key(r): r.get("grade") for r in items}
    rows = []
    for r in final:
        rec = index.load(str(r["base_rid"]))
        s2 = rec["s2"]
        rules = _json_or_none(Path(C.REPO_ROOT) / s2["boundary_dir"] / "rules.json") or {}
        row = {k: r.get(k) for k in J.ITEM_ROW_FIELDS}
        row |= {
            "second_look": r.get("second_look"),
            "run_key": s2["run_key"],
            "cohort_key": s2["cohort_key"],
            "task": s2["task"],
            "channel": s2["channel"],
            "subject": s2["subject"],
            "policy": s2["policy"],
            "half": RT.half_of(s2["cohort_key"], split_rec) if split_rec else None,
            "planted_rule": planted_rule(r.get("quote"), rules.get("lines") or {}),
            "first_grade": first.get(SL.row_key(r)),
            "lost": False,
            "recoverable": None,
            "recoverable_how": None,
            "recoverable_lines": None,
            "recoverable_commands": None,
            "boundary_dir": s2["boundary_dir"],
        }
        row["lost"] = is_lost(row)
        rows.append(row)
    hits_by_image, grep = _search(rows, index, grep_runner)
    for row in rows:
        if not row["lost"]:
            continue
        rec = index.load(str(row["base_rid"]))
        hits = hits_by_image.get(rec["s2"]["image"]) if grep_runner is not None else None
        row |= recoverable(row, rec, hits)
    for row in rows:
        assert tuple(row) == LOSS_ROW_FIELDS
    return rows, grep


def _search(rows: list[dict], index: Index, grep_runner: GrepRunner | None
            ) -> tuple[dict[str, dict], dict]:
    want: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row["lost"]:
            img = index.load(str(row["base_rid"]))["s2"]["image"]
            want[img].update(search_patterns(row.get("quote")))
    summary = {"images": len(want), "patterns": sum(len(v) for v in want.values()),
               "ran": grep_runner is not None}
    if grep_runner is None:
        return {}, summary
    out = {}
    for img, pats in sorted(want.items()):
        if not img:
            raise ValueError("a lost item's boundary has no task image in run.json")
        out[img] = grep_runner(img, sorted(pats)) if pats else {}
    return out, summary


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
