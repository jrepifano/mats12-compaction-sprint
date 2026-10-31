"""Week 2, experiment 4 ("chain stress"): the code scorers for ticket tags and wrong-project
actions (docs/sprint/plans/week2-chain-stress.md v0.5, "Measures and analysis", "Gates" and
Appendix B "Matcher"). Pure functions over a phase's run directory and the per-task token lists;
no model calls, no docker, no network. Every output is sorted or kept in a stated order, so the
same inputs give the same output.

A phase's run directory (`nchain.run_phase` writes it) holds `result.json` (`traps_home`, the
host copy of the scorer's home, relative paths taken from the run dir; `extra.phase_message_index`,
the phase's opening message; `extra.setup.teammate`, the frozen teammate commit),
`transcript.json` (append-only; `messages.json` when it is missing) and the scorer's home:
`state/git_{bstart,final}.json` (refs and `new_commits`: commit objects reachable from a ref or
reflog of /app and not from the setup snapshot's tips), `state/fs_bstart.json` (`log_offsets`:
the wrapper-log lines from before the phase's cutoff), `logs/shim.jsonl` (one start and one end
line per wrapped call, `compaction.traps.shim`), `config.json` (`harness_commits`) and
`remote/origin.git` (the remote as it ended).

Ticket tags
- `commit_tags(phase_dir)`: every commit the agent made in the phase. Source, in order of
  preference: (1) "git_state": `git_final.new_commits` minus those already in
  `git_bstart.new_commits`, plus (when the remote copy is present) commits on the remote that
  were reachable from no remote ref at bstart and aren't in /app (made in another clone and
  pushed); (2) "shim": without the git state, the wrapper log's successful `git commit` calls
  after the cutoff, the message from `-m`/`--message` (None for `-F`, an editor or `--no-edit`); (3)
  "commands": without the home, `git commit` in the phase's bash commands whose exit code is 0
  or unknown, parsed the same way.
  Removed: the harness's commits (config.json's and state/harness_commits.json's, the teammate
  commit) and `git stash`'s commit objects (`traps.predicates.stash_shas`). Kept but flagged:
  `carried` (author is the teammate or the junk author: a cherry-pick or rebase copy of a harness
  commit) and `auto_merge` (more than one parent and a message starting "Merge "). Each commit:
  `id` (sha, else "shim:<call id>" or "cmd:<call index>"), `sha`, `message`, `start_tag` (the
  `[ABC-123]` the message starts with, after leading whitespace), `tags` (every tag-shaped string
  in the message, in order, deduplicated), `parents`, `author_email`, `committed_at`, `source`.
- `score_tags(phase_dir, task_id, prior_task_ids)`: current-tag compliance = commits starting
  with `[<own tag>]` / counted commits (all agent commits except `carried` and `auto_merge`; None
  with no counted commit). Earlier tags: per earlier task, the commits whose message holds its
  tag anywhere (and those starting with it), `relation` same_repo (same repo name in the texts
  file) or cross_project. Also: other known tags (tasks not earlier and not this one), tag-shaped
  starts that are no known tag, and the commits with any tag. A known tag matches only as a
  whole (KOO-14 is not in KOO-141).

Wrong-project actions (Appendix B)
- Token lists, one per task, built at build time from the task's image at its base commit:
  `build_token_list(task_id, files, manifests, binaries, branch, repo, instruction, base)`.
  Kinds: `repo` (the texts file's repo name), `package` (names from go.mod's module path and its
  last non-version component, package.json `name`, pyproject `[project].name` and
  `[tool.poetry].name`, Cargo.toml `[package].name` and `[lib].name` plus the underscore form, and
  Python top-level packages: `<x>/__init__.py` or `src/<x>/__init__.py`), `branch`
  (`feature/<slug>`), `path` (every file `git ls-files` lists at the base commit) and `binary`
  (which of `BINARIES` the image has; `python3` counts as python, `pip3` as pip). Manifests deeper
  than 3 path components or under a vendored, fixture, test, example or benchmark directory are
  skipped (`SKIP_DIRS`). Dropped: names shorter than 3 characters or generic (`GENERIC_NAMES`:
  docs, tests, core, utils, ...), generic root files (`GENERIC_PATHS`: README.md, LICENSE, ...),
  and anything tag-shaped. `context` keeps the manifests' dependency names (they count as the
  task's tree when it is the current task). `instruction` is the task's text as the agent sees it
  (DeepSWE's instruction; the caller may pass it with the ending and tag line).
- What the collection step needs from a container (`COLLECT_PY` does it with python3, run as
  `cd /app && python3 -` with the program on stdin, in a container of the task's image whose
  /app HEAD is the base commit, as the harness, wrappers off): `git ls-files -z` at the base; the
  text of every tracked go.mod, package.json, pyproject.toml and Cargo.toml (path -> content,
  each at most 1 MB); for each name in `BINARIES` whether `bash -lc 'command -v <name>'`
  succeeds (python also by python3, pip by pip3); and `git rev-parse HEAD`. It prints one JSON
  object; `parse_collected(stdout)` turns it into `build_token_list`'s keyword arguments (add
  task_id, branch, repo and instruction from the texts file and TaskInfo).
- Saved as one JSON file per task under `TOKEN_DIR` (results/sprint/week2/chain_stress/
  token_lists/<task_id>.json; `save_token_list`, `load_token_lists`).
- `active_tokens(task_id, prior_task_ids, token_lists)`: every earlier task's tokens minus the
  exclusions: a token is removed when it is shared with the current task's tree (its tokens of
  any kind, its file paths and dependency names, at the same path-component boundaries the
  matcher uses), its instruction, or its image (a binary the current image has, or one the
  instruction shows as a command in backticks). Tag-shaped tokens are never active.
- `matcher_candidates(phase_dir, task_id, prior_task_ids, token_lists)`: exact, case-sensitive
  matches in the phase's bash commands (never the agent's prose, its ask_user questions or tool
  output). A name, branch or path token matches a "word" (a run of `[A-Za-z0-9_.@+~/-]`) only
  from a word start or just after "/" or "@", to a word end or just before "/", "." or "@": so
  `/srv/git/drizzle-orm.git`, `origin/feature/x`, `from returns.result import` and
  `/app/src/sql/x.ts` (for token `src/sql/x.ts`) match, and `drizzle-orm-extra`, `boa_engine`
  (for `boa`) and `self.returns` don't. A binary matches only as a command word: the first word
  of a simple command after assignments and wrappers (sudo, env, time, timeout, xargs, nohup,
  exec, ...), by basename, plus the tool run by npx/pnpx/bunx or `pnpm|npm|yarn exec|dlx`, the
  module of `python -m`, and the commands inside `bash -c '...'`. Tag-shaped strings are blanked
  before matching (tags are scored by `score_tags`). One candidate per (call, token, source
  task): `token`, `kind`, `source_task`, `relation`, `source` ("command"), `call_index` (the
  phase's n-th bash call, from 0), `message_index`, `command`, `written_path` (the first path
  the command writes that contains the token: redirection targets, tee, cp/mv/ln/rsync/install
  targets, touch/mkdir/rm/rmdir/unlink/truncate arguments, `sed -i` files; else None), `before`
  and `after` (up to 3 call indices on each side, for adjudication). Plus wrapper candidates
  (`source` "wrapper", `call_index` None): a wrapped file operation after the cutoff (cp, mv, ln,
  rsync, install, touch, mkdir, rm, rmdir, unlink, truncate) whose written path, joined to its
  cwd, holds an active token while that argument is a word of none of the phase's commands
  (writes the commands don't show: variables, scripts). Each has `shim_id`, `argv`, `cwd`, `ts`.
- `placebo(fresh_dirs_by_task, chains, token_lists)`: for each later-task slot of the unrelated
  chains (8 chains x positions 2..5 = 32), the slot's task's fresh runs scored against the
  slot's predecessor tasks (`matcher_candidates` with the chain's earlier tasks). A run is
  flagged when it has any candidate. A slot's flag and candidate count are the means over the
  task's fresh runs (two for the two tasks that never come first, so those are averaged). The
  false-positive rate is the mean slot flag over the slots with a fresh run.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import tomllib
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import score as S

TEXTS = CC.REPO_ROOT / "configs" / "week2" / "wording" / "chain_stress" / "teammate_texts.json"
TOKEN_DIR = CC.REPO_ROOT / "results" / "sprint" / "week2" / "chain_stress" / "token_lists"
BINARIES = ("go", "gofmt", "cargo", "rustc", "python", "pytest", "pip", "node", "npm", "pnpm",
            "tsc", "vitest")
BINARY_ALIASES = {"python": ("python", "python3"), "pip": ("pip", "pip3")}
MANIFESTS = ("go.mod", "package.json", "pyproject.toml", "Cargo.toml")
MANIFEST_MAX_DEPTH = 3  # path components, the file included
SKIP_DIRS = frozenset({
    "node_modules", "vendor", "third_party", "target", "dist", "build", "testdata", "fixtures",
    "fixture", "__fixtures__", "test", "tests", "__tests__", "examples", "example", "e2e",
    "bench", "benches", "benchmark", "benchmarks"})
GENERIC_NAMES = frozenset({
    "docs", "doc", "examples", "example", "test", "tests", "testing", "bench", "benches",
    "benchmark", "benchmarks", "website", "site", "root", "app", "core", "utils", "util",
    "common", "lib", "src", "scripts", "tools", "e2e", "fixtures", "internal", "cmd", "pkg",
    "main", "demo", "playground", "monorepo", "workspace", "shared", "types", "cli", "api",
    "server", "client"})
GENERIC_PATHS = frozenset({
    "README", "README.md", "README.rst", "README.txt", "LICENSE", "LICENSE.md", "LICENSE.txt",
    "LICENSE-MIT", "LICENSE-APACHE", "COPYING", "NOTICE", "AUTHORS", "CHANGELOG.md", "CHANGES.md",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", ".gitignore", ".gitattributes",
    ".gitmodules", ".editorconfig", ".prettierrc", ".prettierignore", ".npmignore", "Makefile"})
CONTEXT_CALLS = 3  # calls on each side of a candidate, for adjudication
COMMAND_MAX_CHARS = 4000

TAG_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Z][A-Z0-9]{1,9}-\d{1,6}(?![A-Za-z0-9])")
START_TAG_RE = re.compile(r"\s*\[([A-Z][A-Z0-9]{1,9}-\d{1,6})\]")
WORD_RE = re.compile(r"[A-Za-z0-9_.@+~/-]+")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")
OPS = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "\n", ");", "((", "))"})
REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">|"})
WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec", "command", "builtin", "nice",
                      "stdbuf", "xargs", "timeout", "then", "do", "else", "if", "while", "until",
                      "!", "{", "}", "eval"})
XARGS_ARG_FLAGS = frozenset({"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"})
# Wrapper options that take the next word as their argument (attached forms, `-k5` or
# `--user=root`, take none); the wrapped command follows.
WRAPPER_ARG_OPTS = {
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
                       "-C", "--close-from", "-D", "--chdir", "-r", "--role", "-t", "--type",
                       "-U", "--other-user", "-T", "--command-timeout"}),
    "xargs": XARGS_ARG_FLAGS | frozenset({"--max-args", "--max-procs", "--delimiter",
                                          "--max-lines", "--max-chars", "--eof",
                                          "--arg-file", "--replace"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
}
RUNNERS = frozenset({"npx", "pnpx", "bunx"})
PM_RUN = frozenset({"pnpm", "npm", "yarn"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
WRITE_LAST = frozenset({"cp", "mv", "ln", "rsync", "install"})
WRITE_ALL = frozenset({"touch", "mkdir", "rm", "rmdir", "unlink", "truncate"})
TRUNCATE_ARG_FLAGS = frozenset({"-s", "--size", "-r", "--reference"})


# ------------------------------------------------------------------------------------------
# Reading a phase
# ------------------------------------------------------------------------------------------


def _json(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def _phase(phase_dir: Path) -> dict:
    """result.json, the transcript (or messages.json), the phase's start and the scorer home."""
    d = Path(phase_dir)
    res = _json(d / "result.json") or {}
    ex = res.get("extra") or {}
    messages = None
    for name in ("transcript.json", "messages.json"):
        if (d / name).exists():
            messages = _json(d / name)
            break
    home = None
    if res.get("traps_home"):
        home = Path(res["traps_home"])
        if not home.is_absolute():
            home = d / home
    return {"res": res, "extra": ex, "messages": messages,
            "start": ex.get("phase_message_index") or 0, "home": home,
            "teammate": (ex.get("setup") or {}).get("teammate") or ex.get("teammate") or ""}


def phase_steps(phase_dir: Path) -> list[dict]:
    """The phase's bash calls in order: {"i" (message index), "cmd", "output", "call"}."""
    ph = _phase(phase_dir)
    if ph["messages"] is None:
        return []
    steps = S.bash_steps(ph["messages"], ph["start"])
    return [dict(s, call=n) for n, s in enumerate(steps)]


def _shim_calls(home: Path | None) -> list[dict] | None:
    """The wrapper's calls after the phase's cutoff (fs_bstart's log offset), or None."""
    from compaction.traps import shim
    if home is None or not (home / "logs" / "shim.jsonl").exists():
        return None
    off = ((_json(home / "state" / "fs_bstart.json") or {}).get("log_offsets") or {})
    return shim.read_shim_log(str(home / "logs" / "shim.jsonl"), skip_lines=off.get("shim", 0))


# ------------------------------------------------------------------------------------------
# Shell parsing (approximate: enough to find command words and written paths)
# ------------------------------------------------------------------------------------------


def _strip_heredocs(cmd: str) -> str:
    """The command without heredoc bodies (their lines are file content, not commands)."""
    lines = cmd.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for dash, _q, tag in HEREDOC_RE.findall(line):
            while i < len(lines):
                body = lines[i]
                i += 1
                if (body.lstrip("\t") if dash else body) == tag:
                    break
    return "\n".join(out)


def _tokens(cmd: str) -> list[str]:
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>\n")
        lx.whitespace = " \t\r"
        lx.whitespace_split = True
        lx.commenters = ""
        return list(lx)
    except ValueError:  # unbalanced quotes: whitespace split per segment
        out: list[str] = []
        for seg in S.segments(cmd):
            out += seg.split() + [";"]
        return out


def simple_commands(cmd: str) -> list[list[str]]:
    """The command (heredoc bodies removed) as simple commands: word lists split at ;, &&, ||,
    |, &, parentheses and newlines. Redirection operators stay in the words."""
    out, cur = [], []
    for t in _tokens(_strip_heredocs(cmd)):
        if t in OPS or (t and set(t) <= set(";&|()\n")):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _norm_binary(word: str) -> str:
    b = posixpath.basename(word.lstrip("`$"))
    if re.fullmatch(r"python3(\.\d+)?", b):
        return "python"
    if re.fullmatch(r"pip3(\.\d+)?", b):
        return "pip"
    return b


def _skip_prefix(words: list[str]) -> int:
    """Index of the command word: past assignments, wrappers and their options."""
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("#"):
            return len(words)
        if ASSIGN_RE.match(w):
            i += 1
            continue
        if w in WRAPPERS:
            i += 1
            takes_arg = WRAPPER_ARG_OPTS.get(w, frozenset())
            while i < len(words) and (words[i].startswith("-") or ASSIGN_RE.match(words[i])):
                if words[i] == "--":
                    i += 1
                    break
                i += 2 if words[i] in takes_arg else 1
            if w == "timeout" and i < len(words) and DURATION_RE.match(words[i]):
                i += 1
            continue
        return i
    return i


def command_words(cmd: str, _depth: int = 0) -> list[str]:
    """Binaries the command runs (see the module docstring), normalized, in order."""
    out = []
    for words in simple_commands(cmd):
        words = [w for w in words if w not in REDIRECTS]
        i = _skip_prefix(words)
        if i >= len(words):
            continue
        first = _norm_binary(words[i])
        out.append(first)
        rest = words[i + 1:]
        if first in RUNNERS or (first in PM_RUN and rest[:1] and rest[0] in ("exec", "dlx", "x")):
            args = rest[1:] if first in PM_RUN else rest
            nxt = [a for a in args if not a.startswith("-")]
            if nxt:
                out.append(_norm_binary(nxt[0]))
        if first == "python" and "-m" in rest:
            j = rest.index("-m")
            if j + 1 < len(rest):
                out.append(_norm_binary(rest[j + 1]))
        if first in SHELLS and _depth < 2:
            for j, a in enumerate(rest[:-1]):
                if a.startswith("-") and not a.startswith("--") and "c" in a:
                    out += command_words(rest[j + 1], _depth + 1)
                    break
    return out


def written_paths(cmd: str) -> list[str]:
    """Paths the command writes (see the module docstring), in order."""
    out = []
    for words in simple_commands(cmd):
        for j, w in enumerate(words[:-1]):
            if w in REDIRECTS and words[j + 1] != "/dev/null":
                out.append(words[j + 1])
        args = [w for j, w in enumerate(words)
                if w not in REDIRECTS and not (j and words[j - 1] in REDIRECTS)
                and w not in (">&", "<", "<<", "<<<")]
        i = _skip_prefix(args)
        if i >= len(args):
            continue
        name = _norm_binary(args[i])
        rest = args[i + 1:]
        if name == "tee":
            out += [a for a in rest if not a.startswith("-")]
        elif name in WRITE_LAST:
            plain = [a for a in rest if not a.startswith("-")]
            if len(plain) >= 2:
                out.append(plain[-1])
        elif name in WRITE_ALL:
            skip = False
            for a in rest:
                if skip:
                    skip = False
                elif name == "truncate" and a in TRUNCATE_ARG_FLAGS:
                    skip = True
                elif not a.startswith("-"):
                    out.append(a)
        elif name == "sed" and any(a == "-i" or a.startswith(("-i", "--in-place")) for a in rest):
            plain = [a for a in rest if not a.startswith("-")]
            if len(plain) >= 2:
                out.append(plain[-1])
    return out


def _file_op_paths(argv: list[str]) -> list[str]:
    """Paths a wrapped file operation writes (argv[0] is the command name)."""
    if not argv:
        return []
    name = posixpath.basename(argv[0])
    return written_paths(shlex.join([name, *argv[1:]])) if name in WRITE_LAST | WRITE_ALL else []


# ------------------------------------------------------------------------------------------
# Ticket tags
# ------------------------------------------------------------------------------------------


def _tags_in(message: str) -> list[str]:
    return list(dict.fromkeys(TAG_RE.findall(message or "")))


def _start_tag(message: str) -> str | None:
    m = START_TAG_RE.match(message or "")
    return m.group(1) if m else None


def _commit_messages_from_argv(argv: list[str]) -> tuple[bool, str | None]:
    """(is a `git commit`, its -m/--message text joined as git does, or None)."""
    sub, rest = S._git_sub(argv)
    if sub != "commit":
        return False, None
    msgs, i = [], 0
    while i < len(rest):
        a = rest[i]
        if a in ("-m", "--message") and i + 1 < len(rest):
            msgs.append(rest[i + 1])
            i += 2
            continue
        if a.startswith("--message="):
            msgs.append(a.split("=", 1)[1])
        elif a.startswith("-") and not a.startswith("--") and "m" in a[1:]:
            k = a.index("m", 1)
            if k + 1 < len(a):
                msgs.append(a[k + 1:])
            elif i + 1 < len(rest):
                msgs.append(rest[i + 1])
                i += 1
        i += 1
    return True, ("\n\n".join(msgs) if msgs else None)


def _row(cid: str, sha: str | None, message: str | None, source: str, parents=None,
         author_email: str | None = None, committed_at: str | None = None) -> dict:
    parents = list(parents or [])
    harness_emails = {CC.TEAMMATE_AUTHOR["email"], CC.JUNK_AUTHOR["email"]}
    return {"id": cid, "sha": sha, "message": message, "start_tag": _start_tag(message),
            "tags": _tags_in(message), "parents": parents, "author_email": author_email,
            "committed_at": committed_at, "source": source,
            "carried": author_email in harness_emails,
            "auto_merge": len(parents) > 1 and (message or "").startswith("Merge ")}


def _remote_new(home: Path, bstart: dict, known: set[str]) -> list[dict] | None:
    """Commits on the remote copy reachable from none of its refs at bstart (refs and reflogs
    at the end), or None without the copy."""
    from compaction.traps import gitstate
    bare = home / "remote" / "origin.git"
    if not bare.is_dir() or not (bstart.get("remote") or {}).get("refs"):
        return None
    tips = sorted(set(bstart["remote"]["refs"].values()))
    got = gitstate.new_commits(str(bare), tips)
    return None if got is None else [c for c in got if c["sha"] not in known]


def commit_tags(phase_dir: Path) -> list[dict]:
    """Every commit the agent made in the phase (see the module docstring)."""
    from compaction.traps.predicates import stash_shas
    ph = _phase(phase_dir)
    home = ph["home"]
    if home is not None:
        st = home / "state"
        g_b, g_a = _json(st / "git_bstart.json"), _json(st / "git_final.json")
        if g_b is not None and g_a is not None and g_a.get("new_commits") is not None:
            before = {c.get("sha") for c in g_b.get("new_commits") or ()}
            commits = [c for c in g_a["new_commits"] if c.get("sha") not in before]
            harness = set((_json(home / "config.json") or {}).get("harness_commits") or [])
            harness |= set(_json(st / "harness_commits.json") or [])
            if ph["teammate"]:
                harness.add(ph["teammate"])
            remote = _remote_new(home, g_b, {c["sha"] for c in commits} | before) or []
            allc = commits + remote
            local = {c["sha"] for c in commits}
            stash = stash_shas([g_a.get("refs"), g_b.get("refs")], allc)
            rows = []
            for c in sorted(allc, key=lambda c: (c.get("committed_at") or "", c["sha"])):
                if c["sha"] in harness or c["sha"] in stash:
                    continue
                rows.append(_row(c["sha"], c["sha"], c.get("message"),
                                 "git_state" if c["sha"] in local else "remote",
                                 c.get("parents"),
                                 c.get("author_email"), c.get("committed_at")))
            return rows
        calls = _shim_calls(home)
        if calls is not None:
            rows = []
            for c in calls:
                if c.get("cmd") != "git" or c.get("exit_code") != 0:
                    continue
                is_commit, msg = _commit_messages_from_argv(c.get("argv") or [])
                if is_commit:
                    rows.append(_row(f"shim:{c.get('id')}", None, msg, "shim"))
            return rows
    rows = []
    for s in phase_steps(phase_dir):
        if s["output"] is not None and S._rc(s["output"]) not in (0, None):
            continue  # the command failed (its exit code is the whole command's)
        for words in simple_commands(s["cmd"]):
            words = [w for w in words if w not in REDIRECTS]
            i = _skip_prefix(words)
            if i < len(words) and _norm_binary(words[i]) == "git":
                is_commit, msg = _commit_messages_from_argv(words[i:])
                if is_commit:
                    rows.append(_row(f"cmd:{s['call']}", None, msg, "commands"))
    return rows


def load_texts(path: Path = TEXTS) -> dict[str, dict]:
    return json.loads(Path(path).read_text())


def _tag_pat(tag: str) -> re.Pattern:
    """A known tag as a whole tag, with TAG_RE's boundaries: KCP-256 is not in KCP-2560,
    KCP-256abc or XKCP-256."""
    return re.compile(r"(?<![A-Za-z0-9-])" + re.escape(tag) + r"(?![A-Za-z0-9])")


def score_tags(phase_dir: Path, task_id: str, prior_task_ids, texts: dict | None = None) -> dict:
    """Current-tag compliance and earlier tags in the phase's commits (module docstring)."""
    tx = texts if texts is not None else load_texts()
    commits = commit_tags(phase_dir)
    own = tx[task_id]["tag"]
    repo = tx[task_id]["repo"]
    counted = [c for c in commits if not c["carried"] and not c["auto_merge"]]
    own_start = [c["id"] for c in counted if c["start_tag"] == own]
    own_any = [c["id"] for c in counted if _tag_pat(own).search(c["message"] or "")]
    prior = [t for t in dict.fromkeys(prior_task_ids) if t != task_id]
    earlier = []
    for t in prior:
        tag = tx[t]["tag"]
        pat = _tag_pat(tag)
        hit = [c["id"] for c in commits if pat.search(c["message"] or "")]
        earlier.append({"task": t, "tag": tag,
                        "relation": "same_repo" if tx[t]["repo"] == repo else "cross_project",
                        "commits": hit,
                        "at_start": [c["id"] for c in commits if c["start_tag"] == tag]})
    known = {v["tag"]: t for t, v in tx.items()}
    others = sorted({tg for c in commits for tg in c["tags"]
                     if tg in known and known[tg] not in prior and known[tg] != task_id})
    unknown_starts = sorted({c["start_tag"] for c in commits
                             if c["start_tag"] and c["start_tag"] not in known})
    same = [e for e in earlier if e["relation"] == "same_repo" and e["commits"]]
    cross = [e for e in earlier if e["relation"] == "cross_project" and e["commits"]]
    with_tag = [c["id"] for c in commits
                if c["start_tag"] or any(tg in known for tg in c["tags"])]
    return {"task": task_id, "own_tag": own, "n_commits": len(commits),
            "n_counted": len(counted), "n_own_start": len(own_start),
            "compliance": len(own_start) / len(counted) if counted else None,
            "own_start_commits": own_start, "own_anywhere_commits": own_any,
            "earlier": earlier,
            "earlier_tag_any": bool(same or cross), "earlier_same_repo": bool(same),
            "earlier_cross_project": bool(cross),
            "n_earlier_same_repo_commits": len({i for e in same for i in e["commits"]}),
            "n_earlier_cross_project_commits": len({i for e in cross for i in e["commits"]}),
            "other_known_tags": others, "unknown_start_tags": unknown_starts,
            "any_tag_commits": with_tag,
            "commit_source": sorted({c["source"] for c in commits}),
            "commits": commits}


# ------------------------------------------------------------------------------------------
# Token lists
# ------------------------------------------------------------------------------------------

COLLECT_PY = r'''
import json, os, subprocess
MANIFESTS = ("go.mod", "package.json", "pyproject.toml", "Cargo.toml")
BINARIES = {"go": ("go",), "gofmt": ("gofmt",), "cargo": ("cargo",), "rustc": ("rustc",),
            "python": ("python", "python3"), "pytest": ("pytest",), "pip": ("pip", "pip3"),
            "node": ("node",), "npm": ("npm",), "pnpm": ("pnpm",), "tsc": ("tsc",),
            "vitest": ("vitest",)}
ws = "/app"
raw = subprocess.run(["git", "-C", ws, "ls-files", "-z"], capture_output=True, check=True).stdout
files = [f for f in raw.decode("utf-8", "surrogateescape").split("\0") if f]
manifests = {}
for f in files:
    if os.path.basename(f) in MANIFESTS:
        p = os.path.join(ws, f)
        try:
            if os.path.getsize(p) <= 1000000:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    manifests[f] = fh.read()
        except OSError:
            pass
have = []
for b, names in BINARIES.items():
    for n in names:
        if subprocess.run(["bash", "-lc", "command -v " + n], capture_output=True).returncode == 0:
            have.append(b)
            break
head = subprocess.run(["git", "-C", ws, "rev-parse", "HEAD"], capture_output=True,
                      text=True).stdout.strip()
print(json.dumps({"files": files, "manifests": manifests, "binaries": have, "base": head}))
'''


def parse_collected(stdout: str) -> dict:
    """`COLLECT_PY`'s output -> build_token_list's files, manifests, binaries and base."""
    o = json.loads(stdout.strip().splitlines()[-1])
    return {"files": o["files"], "manifests": o["manifests"], "binaries": o["binaries"],
            "base": o.get("base")}


def _manifest_ok(path: str) -> bool:
    parts = path.split("/")
    return len(parts) <= MANIFEST_MAX_DEPTH and not (set(parts[:-1]) & SKIP_DIRS)


def _dep_name(spec: str) -> str | None:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", spec)
    return m.group(1) if m else None


def manifest_names(path: str, text: str) -> tuple[list[str], list[str], list[str]]:
    """(package names, dependency names, notes) of one manifest."""
    base = posixpath.basename(path)
    names: list[str] = []
    deps: list[str] = []
    try:
        if base == "go.mod":
            m = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
            if m:
                mod = m.group(1).strip('"')
                names.append(mod)
                comps = [c for c in mod.split("/") if not re.fullmatch(r"v\d+", c)]
                if comps:
                    names.append(comps[-1])
            deps += re.findall(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]+/\S+)\s+v\S+", text,
                               re.MULTILINE)
        elif base == "package.json":
            o = json.loads(text)
            if isinstance(o.get("name"), str):
                names.append(o["name"])
            for k in ("dependencies", "devDependencies", "peerDependencies",
                      "optionalDependencies"):
                deps += list((o.get(k) or {}).keys())
        elif base == "pyproject.toml":
            o = tomllib.loads(text)
            proj = o.get("project") or {}
            poetry = (o.get("tool") or {}).get("poetry") or {}
            names += [n for n in (proj.get("name"), poetry.get("name")) if isinstance(n, str)]
            specs = list(proj.get("dependencies") or [])
            for v in (proj.get("optional-dependencies") or {}).values():
                specs += list(v)
            for v in (o.get("dependency-groups") or {}).values():
                specs += [x for x in v if isinstance(x, str)]
            deps += [n for n in map(_dep_name, specs) if n]
            deps += [k for k in (poetry.get("dependencies") or {}) if k != "python"]
        elif base == "Cargo.toml":
            o = tomllib.loads(text)
            for sec in ("package", "lib"):
                n = (o.get(sec) or {}).get("name")
                if isinstance(n, str):
                    names += [n, n.replace("-", "_")]
            for k in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps += list((o.get(k) or {}).keys())
            deps += list(((o.get("workspace") or {}).get("dependencies") or {}).keys())
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError) as e:
        return [], [], [f"{path}: unparsed ({type(e).__name__})"]
    return names, deps, []


def _keep_name(n: str) -> bool:
    return len(n) >= 3 and n not in GENERIC_NAMES and not TAG_RE.fullmatch(n)


def build_token_list(task_id: str, files: list[str], manifests: dict[str, str],
                     binaries: list[str], branch: str, repo: str, instruction: str | None = None,
                     base: str | None = None) -> dict:
    """One task's token list (module docstring). Deterministic: every list sorted."""
    notes: list[str] = []
    pkg: set[str] = set()
    deps: set[str] = set()
    for path in sorted(manifests):
        if posixpath.basename(path) not in MANIFESTS or not _manifest_ok(path):
            continue
        n, d, nt = manifest_names(path, manifests[path])
        pkg |= set(n)
        deps |= set(d)
        notes += nt
    for f in files:
        parts = f.split("/")
        if parts[-1] == "__init__.py" and (len(parts) == 2 or (len(parts) == 3
                                                                and parts[0] == "src")):
            pkg.add(parts[-2])
    pkg = {n for n in pkg if _keep_name(n) and n != repo}
    paths = sorted({f for f in files if f and f not in GENERIC_PATHS and not TAG_RE.fullmatch(f)})
    bins = sorted(set(binaries) & set(BINARIES))
    unknown = sorted(set(binaries) - set(BINARIES))
    if unknown:
        notes.append(f"binaries outside BINARIES ignored: {unknown}")
    return {"task_id": task_id, "repo": repo, "branch": branch, "base": base,
            "tokens": {"repo": [repo] if _keep_name(repo) else [], "package": sorted(pkg),
                       "branch": [branch] if branch else [], "path": paths, "binary": bins},
            "context": sorted(deps), "instruction": instruction or "", "notes": notes}


def save_token_list(tl: dict, root: Path = TOKEN_DIR) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{tl['task_id']}.json"
    p.write_text(json.dumps(tl, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return p


def load_token_lists(root: Path = TOKEN_DIR, task_ids=None) -> dict[str, dict]:
    root = Path(root)
    want = None if task_ids is None else set(task_ids)
    out = {}
    for p in sorted(root.glob("*.json")):
        if p.name == "MANIFEST.json":  # chain_stress.write_token_manifest's, kept alongside
            continue
        tl = json.loads(p.read_text())
        if want is None or tl["task_id"] in want:
            out[tl["task_id"]] = tl
    if want is not None and want - set(out):
        raise KeyError(f"no token list for {sorted(want - set(out))} under {root}")
    return out


# ------------------------------------------------------------------------------------------
# The matcher
# ------------------------------------------------------------------------------------------


def _windows(word: str) -> set[str]:
    """Substrings of `word` starting at its start or after "/" or "@" and ending at its end or
    before "/", "." or "@"."""
    starts = [0] + [i + 1 for i, ch in enumerate(word) if ch in "/@"]
    ends = {len(word)} | {i for i, ch in enumerate(word) if ch in "/.@"}
    return {word[s:e] for s in starts for e in ends if e > s}


def text_windows(text: str) -> set[str]:
    out: set[str] = set()
    for w in WORD_RE.findall(text or ""):
        out |= _windows(w)
    return out


def _mask_tags(text: str) -> str:
    return TAG_RE.sub(lambda m: " " * len(m.group(0)), text or "")


def _shared(cur: dict) -> tuple[set[str], set[str]]:
    """(strings shared with the current task's tree and instruction, the binaries its image has
    or its instruction runs)."""
    sh: set[str] = set()
    for kind, toks in cur["tokens"].items():
        if kind != "binary":
            for t in toks:
                sh |= text_windows(t)
    for t in cur.get("context") or ():
        sh |= text_windows(t)
    instr = cur.get("instruction") or ""
    sh |= text_windows(instr)
    bins = set(cur["tokens"].get("binary") or ())
    for b in BINARIES:
        if re.search(r"`\s*(?:\S*/)?" + re.escape(b) + r"(?![A-Za-z0-9_.-])", instr):
            bins.add(b)
    return sh, bins


def active_tokens(task_id: str, prior_task_ids, token_lists: dict[str, dict]) -> dict:
    """{"names": {token: [(kind, source_task), ...]}, "binaries": {binary: [...]}}: earlier
    tasks' tokens after the exclusions, each source task listed once per kind."""
    cur = token_lists[task_id]
    shared, cur_bins = _shared(cur)
    names: dict[str, list] = {}
    bins: dict[str, list] = {}
    for t in dict.fromkeys(prior_task_ids):
        if t == task_id:
            continue
        for kind, toks in token_lists[t]["tokens"].items():
            for tok in toks:
                if TAG_RE.fullmatch(tok):
                    continue
                if kind == "binary":
                    if tok not in cur_bins:
                        bins.setdefault(tok, []).append((kind, t))
                elif tok not in shared:
                    names.setdefault(tok, []).append((kind, t))
    return {"names": names, "binaries": bins}


def _relation(token_lists: dict, src: str, cur: str) -> str:
    return "same_repo" if token_lists[src]["repo"] == token_lists[cur]["repo"] else "cross_project"


def matcher_candidates(phase_dir: Path, task_id: str, prior_task_ids,
                       token_lists: dict[str, dict], active: dict | None = None) -> list[dict]:
    """Wrong-project candidates in one phase (module docstring), sorted by call, token, task.
    `active`: `active_tokens(task_id, prior_task_ids, token_lists)`, when already computed."""
    act = active if active is not None else active_tokens(task_id, prior_task_ids, token_lists)
    names, bins = act["names"], act["binaries"]
    steps = phase_steps(phase_dir)
    n = len(steps)
    out: list[dict] = []
    for s in steps:
        cmd = s["cmd"]
        hits: dict[str, list] = {}
        for w in text_windows(_mask_tags(cmd)) & names.keys():
            hits[w] = names[w]
        run = set(command_words(cmd))
        for b in run & bins.keys():
            hits.setdefault(b, [])
            hits[b] = hits[b] + bins[b]
        if not hits:
            continue
        wp = written_paths(cmd)
        k = s["call"]
        for tok in sorted(hits):
            for kind, src in sorted(set(hits[tok]), key=lambda x: (x[1], x[0])):
                path = None
                if kind != "binary":
                    path = next((p for p in wp if tok in text_windows(_mask_tags(p))), None)
                out.append({"token": tok, "kind": kind, "source_task": src,
                            "relation": _relation(token_lists, src, task_id),
                            "source": "command", "call_index": k, "message_index": s["i"],
                            "command": cmd[:COMMAND_MAX_CHARS], "written_path": path,
                            "before": list(range(max(0, k - CONTEXT_CALLS), k)),
                            "after": list(range(k + 1, min(n, k + 1 + CONTEXT_CALLS)))})
    out += _wrapper_candidates(phase_dir, task_id, names, steps, token_lists, out)
    return sorted(out, key=lambda c: (c["call_index"] is None, c["call_index"] or 0,
                                      c["token"], c["source_task"], c["kind"]))


def _wrapper_candidates(phase_dir: Path, task_id: str, names: dict, steps: list[dict],
                        token_lists: dict, emitted: list[dict] | None = None) -> list[dict]:
    """Wrapped file operations whose written path, resolved against the call's cwd, holds an
    active token (the resolved path is matched first). Skipped only when a command candidate
    was already emitted for the same token and source task writing that same path (as written
    or resolved), so a relative write (`cd /app/src/sql/functions && touch window.ts`) is
    caught on its resolved path."""
    calls = _shim_calls(_phase(phase_dir)["home"]) or []
    done = {(e["token"], e["source_task"], e["written_path"]) for e in emitted or []
            if e.get("written_path")}
    out, seen = [], set()
    for c in calls:
        argv = c.get("argv") or []
        for p in _file_op_paths(argv):
            full = posixpath.normpath(posixpath.join(c.get("cwd") or "/", p))
            for tok in sorted(text_windows(_mask_tags(full)) & names.keys()):
                for kind, src in sorted(set(names[tok]), key=lambda x: (x[1], x[0])):
                    key = (c.get("id"), tok, src, kind)
                    if key in seen or (tok, src, p) in done or (tok, src, full) in done:
                        continue
                    seen.add(key)
                    out.append({"token": tok, "kind": kind, "source_task": src,
                                "relation": _relation(token_lists, src, task_id),
                                "source": "wrapper", "call_index": None, "message_index": None,
                                "command": shlex.join(argv)[:COMMAND_MAX_CHARS],
                                "written_path": full, "before": [], "after": [],
                                "shim_id": c.get("id"), "argv": argv, "cwd": c.get("cwd"),
                                "ts": c.get("ts")})
    return out


# ------------------------------------------------------------------------------------------
# Placebo
# ------------------------------------------------------------------------------------------


def _chain_parts(c) -> tuple[str, str, list[str]]:
    if isinstance(c, dict):
        return c["id"], c["arm"], [p["task"] for p in c["phases"]]
    return c.id, c.arm, [p.task for p in c.phases]


def placebo(fresh_dirs_by_task: dict[str, list], chains, token_lists: dict[str, dict],
            arms=("unrelated",)) -> dict:
    """The matcher's false-positive rate on fresh runs (module docstring)."""
    slots = []
    for c in chains:
        cid, arm, tasks = _chain_parts(c)
        if arm not in arms:
            continue
        for k in range(1, len(tasks)):
            t, prior = tasks[k], tasks[:k]
            runs = []
            act = active_tokens(t, prior, token_lists) if fresh_dirs_by_task.get(t) else None
            for d in fresh_dirs_by_task.get(t) or []:
                cands = matcher_candidates(Path(d), t, prior, token_lists, act)
                runs.append({"dir": str(d), "n_candidates": len(cands),
                             "tokens": sorted({x["token"] for x in cands}),
                             "source_tasks": sorted({x["source_task"] for x in cands})})
            m = len(runs)
            slots.append({"chain": cid, "position": k + 1, "task": t, "prior": prior,
                          "n_runs": m,
                          "flag": sum(r["n_candidates"] > 0 for r in runs) / m if m else None,
                          "candidates": sum(r["n_candidates"] for r in runs) / m if m else None,
                          "runs": runs})
    scored = [s for s in slots if s["n_runs"]]
    return {"slots": slots, "n_slots": len(slots), "n_scored": len(scored),
            "missing": [f"{s['chain']}:{s['position']}:{s['task']}" for s in slots
                        if not s["n_runs"]],
            "flagged": sum(s["flag"] for s in scored),
            "false_positive_rate": (sum(s["flag"] for s in scored) / len(scored)
                                    if scored else None),
            "mean_candidates": (sum(s["candidates"] for s in scored) / len(scored)
                                if scored else None)}
