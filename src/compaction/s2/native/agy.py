"""The Antigravity arm: SWE-Interact sessions in the Antigravity CLI (`agy`) with Gemini 3.8
Flash on Jacob's free quota (brief logs/s2/briefs/native-arms.md; feasibility
logs/s2/native_harness_feasibility.md, "Antigravity"). Work in progress: install and auth first.

- The CLI: the Linux build, pinned by the release manifest's versioned URL and sha512, in a
  read-only Docker volume at /opt/emx/agy (`ensure_cli_volume`).
- Auth: Jacob's one-time interactive login in a container (24 Oct) left agy's token store
  (antigravity-oauth-token; on Linux agy keeps it in a file, in place of the macOS
  Keychain) in the Docker volume emx-native-agy-auth on the desktop. Sessions mount that
  volume at /run/emx-agy (a volume is never in a `docker commit`) and symlink the store
  into agy's state dir; a refreshed store agy writes in place of the link is moved back
  before each commit (`keep_token_linked`), and kept images are scanned for the token's
  strings. Whatever agy sees goes to Google, so only public task repos run here
  (src/compaction/agy.py's rule).
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2.native import common as N

ARM = "agy"
CLI_VERSION = "1.4.7"
CLI_URL = ("https://storage.googleapis.com/antigravity-public/antigravity-cli/"
           "1.4.7-5900205422411776/linux-x64/cli_linux_x64.tar.gz")
CLI_SHA512 = ("beda760587589f5827b0b516c3ec2c5bd5cd2e91eb0e2533807ba1e990a27dcd"
              "63aef0191e0b76fb60feab0d16eaa8210fb79842c34dd4809e257e1712edd152")
CLI_VOLUME = f"emx-native-agy-{CLI_VERSION}"
CLI_DIR = "/opt/emx/agy"
CLI = f"{CLI_DIR}/agy"
AUTH_VOLUME = "emx-native-agy-auth"  # Jacob's one-time container login (24 Oct), token only
AUTH_DIR = "/run/emx-agy"  # the auth volume's mount: outside every docker commit
STATE_DIR = "/root/.gemini/antigravity-cli"
TOKEN_FILE = "antigravity-oauth-token"
HOSTNAME = "emx-agy"  # the login container's hostname (in case the store is keyed to it)
MODEL = "gemini-3.8-flash-low"


def ensure_cli_volume(host: str | None, image: str) -> str:
    """The Linux agy binary in a Docker volume on `host`, from the pinned release (sha512
    checked). Returns the volume."""
    with SB.using_host(host):
        if SB._docker(["volume", "inspect", CLI_VOLUME], timeout=60, check=False).returncode == 0:
            return CLI_VOLUME
        SB._docker(["volume", "create", "--label", f"{N.NATIVE_LABEL}=agy-cli", CLI_VOLUME],
                   timeout=60)
        name = f"{N.purpose(ARM)}-install-{uuid.uuid4().hex[:8]}"
        cmd = (f"cd /tmp && curl -fsSL -o agy.tgz {CLI_URL}"
               f" && echo '{CLI_SHA512}  agy.tgz' | sha512sum -c - >/dev/null"
               " && tar -xzf agy.tgz antigravity && cp antigravity /out/agy"
               " && chmod 755 /out/agy && HOME=/tmp/h /out/agy --version")
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
            raise SB.SandboxError(f"installed agy is not {CLI_VERSION}: {out.stdout[-300:]}")
    return CLI_VOLUME


def run_args() -> tuple[str, ...]:
    """`docker run` extras: the CLI volume (read-only), the auth volume, the hostname and the
    tmpfs for the MCP config and the hook's markers."""
    return ("-v", f"{CLI_VOLUME}:{CLI_DIR}:ro", "-v", f"{AUTH_VOLUME}:{AUTH_DIR}",
            "--hostname", HOSTNAME, "--tmpfs", f"{XDIR}:rw,mode=700,size=4m")


def install_auth(sb: SB.Sandbox) -> None:
    """Link agy's token store to the auth volume (the token never enters the container's own
    filesystem, so no commit can carry it)."""
    r = sb.exec(f"mkdir -p {STATE_DIR} && test -s {AUTH_DIR}/{TOKEN_FILE} && "
                f"ln -sfn {AUTH_DIR}/{TOKEN_FILE} {STATE_DIR}/{TOKEN_FILE}", timeout_s=30,
                workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"agy auth link failed (is {AUTH_VOLUME} populated?)")


def keep_token_linked(sb: SB.Sandbox) -> str:
    """Before a commit (agy is not running then): if agy replaced the link with a refreshed
    file, move it back into the volume and relink. Returns "linked" or "relinked"."""
    r = sb.exec(f"f={STATE_DIR}/{TOKEN_FILE}; if [ -L $f ]; then echo linked; "
                f"elif [ -f $f ]; then umask 077; cp $f {AUTH_DIR}/{TOKEN_FILE}.new && "
                f"mv {AUTH_DIR}/{TOKEN_FILE}.new {AUTH_DIR}/{TOKEN_FILE} && rm -f $f && "
                f"ln -s {AUTH_DIR}/{TOKEN_FILE} $f && echo relinked; else exit 3; fi",
                timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError("agy token store missing from the container")
    return r.stdout.strip()


# the token store's long strings, printed inside the container (they never leave the docker
# host), as patterns for the pre-commit secret scan; fails if there are none
TOKEN_PATTERNS = (f"{{ cat {AUTH_DIR}/{TOKEN_FILE}; cat {STATE_DIR}/{TOKEN_FILE} 2>/dev/null "
                  "|| true; } | grep -oE '[A-Za-z0-9._~/+=-]{40,}' | grep . ")
# (both the retained store and the live one: agy may have written a refreshed store in place
# of the link since the last commit; astra delta r1 #2)


# ------------------------------------------------------------------------------------------
# In-container config, the turn hook and forced compaction
# ------------------------------------------------------------------------------------------

XDIR = "/run/emx-x"  # tmpfs: the MCP config (it names the SimServer token), markers, logs
CONFIG_DIR = "/root/.gemini/config"
SETTINGS = f"{STATE_DIR}/settings.json"
HOOK = "/opt/emx/agy-turn-hook.sh"
COMPACT_AT_FILE = f"{XDIR}/compact_at"
POLICY = "antigravity-native-forced"
CONTINUE_PROMPT = "Continue."
SYSTEM_PLACEHOLDER = "(Antigravity system prompt: not recorded in the transcript)"
STEP_TIMEOUT_S = {"01_plan": 2400, "02_implement": 5400}  # task.toml [steps.agent]
GRADED_STEP = "02_implement"
PLAN_DIR = "plan_boundaries"


def hook_script() -> str:
    """PostInvocation (after the tool calls of one model call finish): the context size of the
    call just made (input + cache-read tokens of the last PLANNER_RESPONSE in agy's own
    transcript) is logged; at or past COMPACT_AT the turn ends ("terminate") and a marker
    tells the runner to compact before the next turn. POSIX sh + sed only (any image)."""
    return r'''#!/bin/sh
P=$(cat)
T=$(printf '%s' "$P" | sed -n 's/.*"transcriptPath" *: *"\([^"]*\)".*/\1/p')
C=$(printf '%s' "$P" | sed -n 's/.*"conversationId" *: *"\([^"]*\)".*/\1/p')
F="${T%/*}/transcript_full.jsonl"; [ -f "$F" ] || F="$T"
L=$(grep '"type" *: *"PLANNER_RESPONSE"' "$F" 2>/dev/null | tail -n 1)
I=$(printf '%s' "$L" | sed -n 's/.*"input_tokens" *: *\([0-9]*\).*/\1/p')
R=$(printf '%s' "$L" | sed -n 's/.*"cache_read_tokens" *: *\([0-9]*\).*/\1/p')
A=$(cat ''' + COMPACT_AT_FILE + r''' 2>/dev/null)
if [ -z "$C" ] || [ -z "$I" ] || [ -z "$A" ]; then  # no evidence: the runner fails the run
  printf '%s NA\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> ''' + XDIR + r'''/ctx-${C:-unknown}.log
  echo '{}'; exit 0
fi
N=$(( I + ${R:-0} ))
printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$N" >> ''' + XDIR + r'''/ctx-$C.log
if [ "$A" -gt 0 ] && [ "$N" -ge "$A" ]; then
  if echo "$N" > ''' + XDIR + r'''/compact-$C; then
    echo '{"terminationBehavior":"terminate"}'
  else  # no marker, no terminate: the runner sees a count past the threshold and fails the run
    printf '%s MARKERFAIL\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> ''' + XDIR + r'''/ctx-$C.log
    echo '{}'
  fi
else
  echo '{}'
fi
'''


def install_config(sb: SB.Sandbox, server: N.SimServer, compact_at: int | None) -> None:
    """MCP (the released ask_user server; the token rides in the URL, as agy's mcp_config
    takes no headers, and the file lives on the tmpfs), the MCP permission rule headless
    mode needs (feasibility), the turn hook, the threshold."""
    r = sb.exec(f"mkdir -p {CONFIG_DIR} {STATE_DIR} /opt/emx", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"agy config dirs: {r.stderr[-300:]}")
    url = f"{N.MCP_URL}?{N.TOKEN_QUERY}={server.token}"
    sb.put_file(json.dumps({"mcpServers": {"user-server": {"url": url}}}).encode(),
                f"{XDIR}/mcp_config.json")
    sb.put_file(json.dumps({"permissions": {"allow": ["mcp(user-server/ask_user)"]}}).encode(),
                SETTINGS)
    sb.put_file(json.dumps({"emx-turn": {"PostInvocation": [
        {"type": "command", "command": HOOK, "timeout": 30}]}}).encode(),
        f"{CONFIG_DIR}/hooks.json")
    sb.put_file(hook_script().encode(), HOOK)
    sb.put_file(f"{int(compact_at or 0)}\n".encode(), COMPACT_AT_FILE)
    r = sb.exec(f"chmod 755 {HOOK} && ln -sfn {XDIR}/mcp_config.json {CONFIG_DIR}/mcp_config.json",
                timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"agy config: {r.stderr[-300:]}")


@dataclass(frozen=True)
class AgyConfig:
    model: str = MODEL
    compact_at: int | None = 150_000  # Jacob, 24 Oct: a common 150k trigger, forced here
    effort: str | None = None

    def threshold(self) -> dict:
        return {"kind": "forced /compact at the next turn boundary (PostInvocation hook)",
                "compact_at": self.compact_at,
                "measure": "input + cache_read tokens of the last model call"}


TURN_OUT, TURN_ERR = f"{XDIR}/turn.json", f"{XDIR}/turn.err"


def agy_command(cfg: AgyConfig, prompt: str, conversation: str | None, timeout_s: int) -> str:
    """The in-container turn; its stdout and stderr go to the tmpfs, to be screened for
    secrets before they leave the container (`run_turn`)."""
    parts = [CLI, "-p", prompt, "--model", cfg.model, "--dangerously-skip-permissions",
             "--output-format", "json", "--print-timeout", f"{int(timeout_s)}s", "--add-dir",
             "/app"]
    if cfg.effort:
        parts += ["--effort", cfg.effort]
    if conversation:
        parts += ["--conversation", conversation]
    return (f"cd /app && {shlex.join(parts)} </dev/null > {TURN_OUT} 2> {TURN_ERR}; "
            "rc=$?; exit $rc")


def result_of(log_path: Path) -> dict:
    for line in reversed(Path(log_path).read_text().splitlines() if Path(log_path).exists()
                         else []):
        with contextlib.suppress(ValueError):
            d = json.loads(line)
            if isinstance(d, dict) and "status" in d:
                return d
    return {}


def transcript_file(cid: str) -> str:
    return f"{STATE_DIR}/brain/{cid}/.system_generated/logs/transcript_full.jsonl"


def read_steps(sb: SB.Sandbox, cid: str) -> list[dict]:
    r = sb.exec(f"cat {shlex.quote(transcript_file(cid))}", timeout_s=120, workdir="/")
    if r.rc != 0:  # docker's own message kept: "No such container" means infra_killed
        raise SB.SandboxError(f"cannot read the agy transcript of {cid}: rc={r.rc} "
                              f"{r.stderr.strip()[-200:]}")
    out = []
    for line in r.stdout.splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ------------------------------------------------------------------------------------------
# Transcript -> the pi layout
# ------------------------------------------------------------------------------------------


_STAMPS = re.compile(r"\ACreated At: [^\n]*\nCompleted At: [^\n]*\n")


def normalize_call(c: dict) -> tuple[str, dict]:
    """agy's tool call -> (name, arguments) as the graders read them. MCP tools go through
    agy's generic call_mcp_tool {ServerName, ToolName, Arguments}: the released ask_user
    becomes ask_user({question}) as in the pi arm, other MCP tools mcp__<server>__<tool>.
    run_command keeps its arguments and gains "command" (= CommandLine) so `command_of`
    sees the shell command."""
    name, args = c.get("name"), dict(c.get("args") or {})
    if name == "call_mcp_tool":
        tool, server = args.get("ToolName"), args.get("ServerName")
        inner = dict(args.get("Arguments") or {})
        if tool == "ask_user" and server == "user-server":
            return "ask_user", inner
        return f"mcp__{server}__{tool}", inner
    if name == "run_command" and "CommandLine" in args:
        args.setdefault("command", args["CommandLine"])
    return name, args


def unwrap_reply(text: str) -> str:
    """agy's MCP tool output -> the reply text: its Created/Completed stamps dropped, then a
    FastMCP {"result": ...} wrapper, if any."""
    text = _STAMPS.sub("", text or "", count=1)
    with contextlib.suppress(ValueError, TypeError):
        d = json.loads(text)
        if isinstance(d, dict) and set(d) == {"result"} and isinstance(d["result"], str):
            return d["result"]
    return text


def steps_to_messages(steps: list[dict]) -> tuple[list[dict], dict]:
    """OpenAI-format messages for agy transcript steps. PLANNER_RESPONSE -> assistant (tool
    calls get ids "agy<step>_<n>"; the MCP ask_user is renamed ask_user), GENERIC -> the tool
    result of the oldest pending call, USER_INPUT -> user (the <USER_REQUEST> text), CHECKPOINT
    and SYSTEM_MESSAGE -> user (model-visible context; lossaudit drops system roles)."""
    from compaction import agy as AG
    msgs: list[dict] = []
    pending: list[tuple[str, str]] = []
    info = {"orphan_tool_outputs": 0, "system_as_user": 0}
    for st in steps:
        typ, content = st.get("type"), st.get("content") or ""
        if typ == "PLANNER_RESPONSE":
            m: dict = {"role": "assistant", "content": content}
            calls = []
            for n, c in enumerate(st.get("tool_calls") or []):
                cid = f"agy{st.get('step_index')}_{n}"
                name, args = normalize_call(c)
                calls.append({"id": cid, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
                pending.append((cid, name))
            if calls:
                m["tool_calls"] = calls
            msgs.append(m)
        elif typ == "GENERIC" and st.get("source") == "MODEL":
            if pending:
                cid, name = pending.pop(0)
                msgs.append({"role": "tool", "tool_call_id": cid,
                             "content": unwrap_reply(content) if name == "ask_user" else content})
            else:
                info["orphan_tool_outputs"] += 1
                msgs.append({"role": "user", "content": content})
        elif typ == "USER_INPUT":
            msgs.append({"role": "user", "content": AG.user_request(content)})
        else:  # CHECKPOINT, SYSTEM_MESSAGE, anything else the model sees
            info["system_as_user"] += 1
            msgs.append({"role": "user", "content": content})
    return msgs, info


def _is_compact_input(st: dict) -> bool:
    from compaction import agy as AG
    return st.get("type") == "USER_INPUT" and \
        AG.user_request(st.get("content") or "").strip().startswith("/compact")


def checkpoint_boundary(steps: list[dict], j: int, *, model: str,
                        ctx_tokens: int | None) -> N.NativeBoundary:
    """The j-th CHECKPOINT (1-based) of an agy conversation as a NativeBoundary. pre: the
    context before the /compact input that produced it (from the previous CHECKPOINT on, which
    the agent had in place of its history). post: the CHECKPOINT and the system steps right
    after it (agy keeps no verbatim tail beyond the user requests quoted in the CHECKPOINT)."""
    cps = [i for i, s in enumerate(steps) if s.get("type") == "CHECKPOINT"]
    if len(cps) < j:
        raise ValueError(f"conversation has {len(cps)} checkpoints, wanted {j}")
    c = cps[j - 1]
    ci = next((i for i in range(c - 1, -1, -1) if _is_compact_input(steps[i])), c)
    start = cps[j - 2] if j > 1 else 0
    pre_msgs, info = steps_to_messages(steps[start:ci])
    sysmsg = {"role": "system", "content": SYSTEM_PLACEHOLDER}
    after = []
    for st in steps[c + 1:]:
        if st.get("type") in ("USER_INPUT", "PLANNER_RESPONSE", "GENERIC"):
            break
        after.append(st)
    tail, _ = steps_to_messages(after)
    summary = steps[c].get("content") or ""
    return N.NativeBoundary(
        pre_messages=[sysmsg, *pre_msgs],
        post_messages=[sysmsg, {"role": "user", "content": summary}, *tail],
        summary=summary, summary_index=1, kept_indices=[], pre_tokens=ctx_tokens,
        post_tokens=None, trigger="forced", policy=POLICY, model=model,
        info={**info, "checkpoint_step": steps[c].get("step_index"),
              "compact_input_step": steps[ci].get("step_index") if ci != c else None,
              "checkpoint_in_conversation": j})


# ------------------------------------------------------------------------------------------
# Base runs
# ------------------------------------------------------------------------------------------


def hook_evidence(sb: SB.Sandbox, cid: str, steps: list[dict],
                  compact_at: int | None = None) -> tuple[str | None, int | None]:
    """(problem or None, the marker's context size or None). The hook logs one line per model
    call; every PLANNER_RESPONSE so far must have one, with a numeric size (astra agy r1 #3).
    The marker is read and removed in one step; a failed read is a problem, not "no marker"."""
    r = sb.exec(f"f={XDIR}/compact-{cid}; if [ -f $f ]; then cat $f && rm -f $f; "
                f"else echo NONE; fi", timeout_s=30, workdir="/")
    if r.rc != 0:
        return f"marker read failed rc={r.rc}", None
    m = r.stdout.strip()
    marker = None if m == "NONE" else int(m) if m.isdigit() else -1
    if marker == -1:
        return f"bad marker {m[:40]!r}", None
    lg = sb.exec(f"cat {XDIR}/ctx-{cid}.log 2>/dev/null; true", timeout_s=30, workdir="/")
    vals = [ln.split()[-1] for ln in lg.stdout.splitlines() if ln.strip()]
    if "MARKERFAIL" in vals:
        return "the hook could not write its compaction marker", marker
    n_calls = sum(1 for s in steps if s.get("type") == "PLANNER_RESPONSE")
    if any(not v.isdigit() for v in vals):
        return "the hook found no token counts for a call", marker
    if len(vals) != n_calls:
        return f"hook ran for {len(vals)} of {n_calls} model calls", marker
    if compact_at and vals and int(vals[-1]) >= compact_at and marker is None:
        # the last call crossed the threshold, so a marker must exist (astra delta r1 #1)
        return f"last call at {vals[-1]} >= {compact_at} but no marker", marker
    return None, marker


def new_checkpoint_ok(steps: list[dict], seen: int) -> bool:
    """Exactly one new CHECKPOINT, preceded (since the previous one) by our /compact input."""
    cps = [i for i, s in enumerate(steps) if s.get("type") == "CHECKPOINT"]
    if len(cps) != seen + 1:
        return False
    lo = cps[-2] if len(cps) > 1 else -1
    return any(_is_compact_input(steps[i]) for i in range(lo + 1, cps[-1]))


def run_key(task_id: str, attempt: int, prefix: str = "") -> str:
    return f"{prefix}{task_id}-agy-flash-a{attempt}"


def next_attempt(root_dir: Path, task_id: str, prefix: str = "") -> int:
    import re
    pat = re.compile(rf"{re.escape(prefix + task_id)}-agy-flash-a(\d+)")
    ns = [int(m.group(1)) for p in Path(root_dir).glob("*") if (m := pat.fullmatch(p.name))]
    return max(ns, default=0) + 1


def root() -> Path:
    from compaction.s2 import config as C
    return C.raw_dir() / "native" / ARM


@dataclass
class BaseConfig:
    task: object  # interact_run.InteractTask
    root: Path
    attempt: int = 1
    prefix: str = ""
    agy: AgyConfig = AgyConfig()
    host: str | None = "ssh://rig"
    sim_cap_usd: float = 8.0
    steps: tuple[str, ...] = ("01_plan", "02_implement")
    step_timeout_s: dict | None = None
    stop_after: int | None = None
    max_turns: int = 60  # per step: one per forced compaction, plus the first
    cpus: float = N.AGENT_CPUS
    memory_mb: int = N.AGENT_MEMORY_MB
    min_free_gb: float = N.AGENT_MIN_FREE_GB

    @property
    def run_key(self) -> str:
        return run_key(self.task.task_id, self.attempt, self.prefix)

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / self.run_key


class SecretFound(RuntimeError):
    pass


def screen_script(paths: list[str]) -> str:
    """In-container: the token store's strings and $EMX_SIM_TOKEN as patterns (a /dev/shm
    file), grep -lF over the existing `paths`; exit 0 = a hit, 1 = clean, else an error."""
    plist = " ".join(shlex.quote(p) for p in paths)
    return ("set -o pipefail; P=$(mktemp -p /dev/shm) || exit 3; { " + TOKEN_PATTERNS +
            "; } > \"$P\" || { rm -f \"$P\"; exit 5; }; printf '%s\\n' \"$EMX_SIM_TOKEN\" "
            f">> \"$P\"; F=''; for f in {plist}; do [ -e \"$f\" ] && F=\"$F $f\"; done; "
            "if [ -z \"$F\" ]; then rc=1; else grep -lF -f \"$P\" -- $F; rc=$?; fi; "
            "rm -f \"$P\"; [ $rc -eq 1 ] && echo SCREEN_CLEAN; [ $rc -eq 0 ] && echo SCREEN_HIT; "
            "exit $rc")


def screen(sb: SB.Sandbox, paths: list[str], sim_token: str) -> None:
    """Refuse to let `paths` leave the container if any holds the agy token's strings or the
    SimServer token (astra agy r1 #4). Fails closed: a scan error raises too."""
    r = N.exec_env(sb, screen_script(paths), {"EMX_SIM_TOKEN": sim_token}, timeout_s=300)
    lines = r.stdout.split()
    if r.rc == 0 and "SCREEN_HIT" in lines:
        raise SecretFound(f"a secret is in {[x for x in lines if x != 'SCREEN_HIT'][:5]}; "
                          "not exported")
    if r.rc != 1 or "SCREEN_CLEAN" not in lines:  # a docker failure is never "clean"
        raise SB.SandboxError(f"secret screen failed rc={r.rc} {r.stderr.strip()[-200:]}")


BG_GRACE_S = 600
IDLE_WAIT = "waiting up to"  # agy's stderr: "root agent idle; waiting up to 1h59m for N
# background task(s)"


def background_rule(sb: SB.Sandbox, cid: str | None, record: list) -> Callable[[], None]:
    """agy keeps a terminated turn open until the agent's background tasks finish (seen
    24 Oct: a hung test held smoke 2 past its threshold), up to the print timeout. Once the
    hook's marker exists and agy has idled on its background tasks for BG_GRACE_S, they are
    killed: the agy CLI's descendants outside the hook's and this check's own trees, TERM then
    KILL, then verified gone (as the pi arm's quiesce kills strays at every boundary). Every
    attempt is recorded with its targets, the signal results and the survivors; an attempt
    that leaves survivors or can't be checked is "unconfirmed", which the runner treats as an
    infrastructure failure (astra l3 r1 #1)."""
    seen: dict = {}

    def targets() -> tuple[dict, list[int]]:
        procs, tok = N.ps_table(sb)
        agy = {p for p, v in procs.items() if v[2].startswith(CLI)}
        keep = agy | N._tree(procs, {p for p, v in procs.items() if HOOK in v[2]}) | \
            N._tree(procs, {p for p, v in procs.items() if tok in v[2]})
        return procs, sorted(p for p in N._tree(procs, agy) - keep
                             if not procs[p][1].startswith("Z"))

    def watch() -> bool | None:
        # any marker: on a conversation's first turn its id isn't known yet, and only one
        # conversation runs at a time
        r = sb.exec(f"ls {XDIR}/compact-* >/dev/null 2>&1 && tail -c 300 {TURN_ERR}",
                    timeout_s=30, workdir="/")
        if r.rc != 0 or IDLE_WAIT not in r.stdout:
            seen.pop("since", None)
            return
        seen.setdefault("since", time.monotonic())
        if time.monotonic() - seen["since"] < BG_GRACE_S:
            return
        rec = {"utc": N.utc(), "idle_s": round(time.monotonic() - seen["since"]),
               "grace_s": BG_GRACE_S, "outcome": "unconfirmed"}
        record.append(rec)  # first, so a failure below still leaves a trace
        try:
            procs, kids = targets()
            rec["targets"] = [f"{p} {procs[p][2][:200]}" for p in kids]
            if kids:
                for sig in ("TERM", "KILL"):
                    try:
                        N.signal_pids(sb, kids, sig)
                        rec[sig.lower()] = "sent"
                    except SB.SandboxError as e:
                        rec[sig.lower()] = f"failed: {e}"[:200]
                    time.sleep(5)
                # by pid, not by tree: a survivor whose parent died is reparented out of it
                after, _ = N.ps_table(sb)
                rec["survivors"] = [p for p in kids if p in after
                                    and not after[p][1].startswith("Z")]
            rec["outcome"] = "confirmed" if not rec.get("survivors") else "unconfirmed"
        except Exception as e:  # noqa: BLE001 - recorded; the runner fails the run
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        seen["since"] = time.monotonic()  # one attempt per grace period
        # an unconfirmed intervention ends the wait now (detached_exec: "watch_abort"); the
        # runner then ends the run infra_error (astra l3 r2 #3)
        return rec["outcome"] != "confirmed"
    return watch


def run_turn(sb: SB.Sandbox, cfg: AgyConfig, prompt: str, cid: str | None, timeout_s: int,
             log: Path, env: dict, stop, sim_token: str,
             bg_record: list | None = None) -> tuple[N.StreamResult, dict, str]:
    """One agy turn: run, screen its output in the container, then copy it to `log` (and
    `log`.err). Returns (the exec result, the result JSON, stderr). With `bg_record`, the
    background-task rule (`background_rule`) applies and its kills are appended there."""
    watch = background_rule(sb, cid, bg_record) if bg_record is not None else None
    r = N.detached_exec(sb, agy_command(cfg, prompt, cid, max(30, timeout_s - 15)),
                        name=Path(log).stem, timeout_s=timeout_s,
                        log_path=Path(str(log) + ".exec"), env=env, stop=stop, watch=watch)
    if r.killed in ("container_gone", "output_lost"):  # infra: nothing trustworthy to read
        return r, {}, ""
    screen(sb, [TURN_OUT, TURN_ERR], sim_token)
    out_text = sb.exec(f"cat {TURN_OUT} 2>/dev/null; true", timeout_s=60, workdir="/").stdout
    err_text = sb.exec(f"cat {TURN_ERR} 2>/dev/null; true", timeout_s=60, workdir="/").stdout
    Path(log).write_text(out_text)
    Path(str(log) + ".err").write_text(err_text)
    return r, result_of(log), err_text


def _turn_ok(r: N.StreamResult, out: dict, err: str = "",
             print_timeout_s: float | None = None) -> str | None:
    """Why a finished turn failed (None: it didn't). agy returns partial output with exit 0
    and status SUCCESS when --print-timeout expires (src/compaction/agy.py): a turn that ran
    to the timeout, or warned of one, is "cli_timeout" (astra agy r1 #1)."""
    from compaction import agy as AG
    if r.killed == "container_gone":
        return "infra_killed: the container disappeared"
    if r.killed == "output_lost":
        return "infra_killed: the step's output could not be recovered"
    if r.killed == "watch_abort":
        return "infra_error: a background-task kill was not confirmed"
    if r.killed == "stop":
        return None
    if print_timeout_s and r.seconds >= print_timeout_s - 5:
        return f"cli_timeout: ran {r.seconds:.0f}s of {print_timeout_s:.0f}s"
    if AG.CLI_TIMEOUT_WARNING.search(err or ""):
        return "cli_timeout: agy warned of its print timeout"
    if r.rc == 137 and not r.timed_out_by_cli:
        return "infra_killed: exit 137"
    tail = " ".join((err or "").split())[-300:]  # quota and API errors land in stderr too
    if r.rc != 0:
        return f"agy exit {r.rc}: {str(out.get('error'))[:200]} | {tail}"
    if out.get("status") != "SUCCESS":
        return f"agy status {out.get('status')!r}: {str(out.get('error'))[:200]} | {tail}"
    return None


def run_base(cfg: BaseConfig, *, sim_model, eligible=None, sim_factory=None,
             session_cls=N.NativeSession) -> dict:
    """One task in Antigravity (module docstring): 01_plan, then 02_implement as a fresh
    conversation with the same simulator; in each, turns run until agy ends one on its own;
    a turn the hook ended at the threshold is followed by `/compact`, the boundary capture
    and a "Continue." turn. Ends: submitted, eligible, stop_after, run_cap, time_cap,
    max_turns, cli_error, boundary_error, boundary_missing, infra_error, error."""
    import threading
    import time
    import traceback

    from compaction.s2 import interact as I
    from compaction.s2 import interact_run as IR
    from compaction.s2 import session as SS
    t0 = time.monotonic()
    task = cfg.task
    rd = IR._fresh_dir(cfg.run_dir)
    timeouts = {**STEP_TIMEOUT_S, **(cfg.step_timeout_s or {})}
    rj = N.native_run_json(run_key=cfg.run_key, task_dict=task.to_dict(),
                           persona_sha256=I.sha256(task.persona), arm=ARM, model=cfg.agy.model,
                           policy=POLICY, threshold=cfg.agy.threshold(), cli_version=CLI_VERSION,
                           extra={"host": cfg.host, "egress_allow": list(N.ALLOW[ARM]),
                                  "steps": list(cfg.steps), "step_timeout_s": timeouts,
                                  "stop_after": cfg.stop_after, "continue_prompt":
                                      CONTINUE_PROMPT, "effort": cfg.agy.effort,
                                  "cpus": cfg.cpus, "memory_mb": cfg.memory_mb})
    IR._dump(rd / "run.json", rj)
    chains: dict = {GRADED_STEP: (rd, SS.KeepStore(rd, cfg.run_key, host=cfg.host))}
    res: dict = {"run_key": cfg.run_key, "end": "error", "boundaries": [],
                 "plan_boundaries": [], "error": None, "steps": {}, "timings": [],
                 "sim_cost_usd": 0.0, "plan_md": None}
    stop = threading.Event()
    end: dict = {}

    def mark(ev: str, **kw) -> None:
        res["timings"].append({"event": ev, "utc": IR.utc(),
                               "s": round(time.monotonic() - t0, 1), **kw})

    def on_cap(e: BaseException) -> None:
        end.setdefault("end", "run_cap")
        stop.set()

    def chain(step: str):
        if step == GRADED_STEP:
            return chains[GRADED_STEP]
        if "plan" not in chains:
            pr = rd / PLAN_DIR
            pr.mkdir(parents=True, exist_ok=True)
            IR._dump(pr / "run.json", {**rj, "run_key": f"{cfg.run_key}-plan"})
            chains["plan"] = (pr, SS.KeepStore(pr, f"{cfg.run_key}-plan", host=cfg.host))
        return chains["plan"]

    def default_sim(runner):
        return I.UserSim(task.persona, sim_model, runner, purpose=f"sim:{cfg.run_key}")

    ensure_cli_volume(cfg.host, task.info.image)
    ns = session_cls(ARM, task.info.image, sim_factory or default_sim, host=cfg.host,
                     extra_args=run_args(), cpus=cfg.cpus, memory_mb=cfg.memory_mb,
                     min_free_gb=cfg.min_free_gb, on_cap=on_cap)
    observed = 0
    try:
        with ns:
            sb = ns.sb
            res["container"] = sb.name
            install_auth(sb)
            install_config(sb, ns.server, cfg.agy.compact_at)
            res["egress_check"] = N.egress_check(sb, ns.egress,
                                                 "https://daily-cloudcode-pa.googleapis.com/")
            IR._dump(rd / "egress_check.json", res["egress_check"])
            if not res["egress_check"]["ok"]:
                raise RuntimeError("egress check failed; see egress_check.json")
            env = ns.egress.proxy_env()
            mark("started")
            for step in cfg.steps:
                if stop.is_set() or end.get("end"):
                    break
                ns.server.step = step
                sd = rd / "steps" / step
                sd.mkdir(parents=True, exist_ok=True)
                cid, prompt, turns, seen_cp = None, task.steps[step], [], 0
                deadline = time.monotonic() + timeouts.get(step, 5400)
                tok = ns.server.token
                while True:
                    if len(turns) >= cfg.max_turns:
                        end.setdefault("end", "max_turns")
                        break
                    left = int(deadline - time.monotonic())
                    if left <= 60:
                        end.setdefault("end", "time_cap")
                        break
                    n = len(turns) + 1
                    bg: list = []
                    res.setdefault("background_kills", []).append(
                        {"step": step, "turn": n, "conversation_id": cid, "kills": bg})
                    r, out, err = run_turn(sb, cfg.agy, prompt, cid, left, sd / f"turn{n:03d}.json",
                                           env, stop, tok, bg_record=bg)
                    if any(k.get("outcome") != "confirmed" for k in bg):
                        end.setdefault("end", "infra_error")
                        res["error"] = f"{step} turn {n}: a background-task kill was not " \
                                       "confirmed"
                        break
                    cid = cid or out.get("conversation_id") or None
                    bad = _turn_ok(r, out, err, max(30, left - 15))
                    marker, prob = None, None
                    if bad and bad.startswith("infra_killed"):  # before any evidence check
                        turns.append({"n": n, "prompt": prompt[:200], "rc": r.rc,
                                      "seconds": r.seconds, "killed": r.killed, "failed": bad})
                        end.setdefault("end", "infra_killed")
                        res["error"] = f"{step} turn {n}: {bad}"
                        break
                    if cid is None:
                        prob = "agy returned no conversation id"
                    else:
                        steps_ = read_steps(sb, cid)
                        prob, marker = hook_evidence(sb, cid, steps_, cfg.agy.compact_at)
                        ncp_now = sum(1 for x in steps_ if x.get("type") == "CHECKPOINT")
                        if prob is None and ncp_now != seen_cp:
                            prob = f"agy compacted on its own ({ncp_now} checkpoints, " \
                                   f"{seen_cp} captured)"
                            end.setdefault("end", "boundary_missing")
                    turns.append({"n": n, "prompt": prompt[:200], "rc": r.rc,
                                  "status": out.get("status"), "usage": out.get("usage"),
                                  "seconds": r.seconds, "killed": r.killed,
                                  "hook_terminated_at": marker, "failed": bad, "problem": prob})
                    if prob:
                        end.setdefault("end", "hook_error")
                        res["error"] = f"{step} turn {n}: {prob}"
                        break
                    if bad:
                        end.setdefault("end", "time_cap" if bad.startswith("cli_timeout") else
                                       "infra_killed" if bad.startswith("infra_killed") else
                                       "cli_error")
                        res["error"] = f"{step} turn {n}: {bad}"
                        break
                    if r.killed or marker is None:
                        break  # stopped, or agy ended the turn on its own: the step is done
                    # forced compaction at this turn boundary, within the step's deadline
                    left = int(deadline - time.monotonic())
                    if left <= 120:
                        end.setdefault("end", "time_cap")
                        break
                    ct = min(660, left - 60)
                    rc_, outc, errc = run_turn(sb, cfg.agy, "/compact", cid, ct,
                                               sd / f"turn{n:03d}-compact.json", env, stop, tok)
                    bad_c = _turn_ok(rc_, outc, errc, max(30, ct - 15))
                    if bad_c:  # recorded as a turn, so the batch sees quota and infra causes
                        turns.append({"n": f"{n}-compact", "prompt": "/compact", "rc": rc_.rc,
                                      "status": outc.get("status"), "seconds": rc_.seconds,
                                      "killed": rc_.killed, "failed": bad_c})
                        end.setdefault("end", "time_cap" if bad_c.startswith("cli_timeout")
                                       else "infra_killed" if bad_c.startswith("infra_killed")
                                       else "cli_error")
                        res["error"] = f"{step} turn {n} /compact: {bad_c}"
                        break
                    steps_ = read_steps(sb, cid)
                    if not new_checkpoint_ok(steps_, seen_cp):
                        end.setdefault("end", "boundary_error")
                        res["error"] = f"{step} turn {n}: /compact did not add exactly one " \
                                       "checkpoint after our input"
                        break
                    seen_cp += 1
                    broot, keep = chain(step)
                    try:
                        k = capture(ns, cid=cid, j=seen_cp, ctx=marker, steps=steps_,
                                    rd=broot, rj=rj, keep=keep, cfg=cfg, step=step,
                                    eligible=eligible if step == GRADED_STEP else None,
                                    interventions=bg)
                    except Exception as e:  # noqa: BLE001 - the run ends; nothing past it
                        res.setdefault("boundary_errors", []).append(
                            {"step": step, "error": traceback.format_exc()[-3000:]})
                        end.setdefault("end", "infra_error" if isinstance(e, SB.SandboxError)
                                       else "boundary_error")
                        break
                    res["boundaries" if step == GRADED_STEP else "plan_boundaries"].append(k)
                    res.setdefault("compactions", []).append(
                        N.compaction_meta(broot / f"b{k}", step))
                    mark("boundary_saved", step=step, k=k, ctx=marker)
                    f = json.loads((broot / f"b{k}" / "boundary" / "boundary.json").read_text())
                    if f.get("eligible"):
                        end.setdefault("end", "eligible")
                    elif (step == GRADED_STEP and cfg.stop_after
                          and len(res["boundaries"]) >= cfg.stop_after):
                        end.setdefault("end", "stop_after")
                    if end.get("end"):
                        break
                    prompt = CONTINUE_PROMPT
                steps_final = []
                if end.get("end") == "infra_killed":
                    # the container is gone (or killed): nothing more to export; keep the
                    # turn records and the classification (astra delta r2 #1)
                    res["steps"][step] = {"conversation_id": cid, "turns": turns,
                                          "checkpoints": seen_cp, "export": "skipped"}
                    break
                if cid:
                    screen(sb, [transcript_file(cid)], tok)
                    steps_final = read_steps(sb, cid)
                (sd / "transcript_full.jsonl").write_text(
                    "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in steps_final))
                ctx_log = sb.exec(f"cat {XDIR}/ctx-{cid}.log 2>/dev/null", timeout_s=30,
                                  workdir="/").stdout if cid else ""
                (sd / "context_by_call.log").write_text(ctx_log)
                n_cp = sum(1 for s in steps_final if s.get("type") == "CHECKPOINT")
                observed += n_cp
                res["steps"][step] = {"conversation_id": cid, "turns": turns,
                                      "checkpoints": n_cp}
                if n_cp > seen_cp:  # agy compacted on its own: not captured
                    end.setdefault("end", "boundary_missing")
                    res["error"] = f"{step}: {n_cp} checkpoints, {seen_cp} captured"
                if step == "01_plan":
                    screen(sb, ["/tmp/plan.md"], tok)
                    res["plan_md"] = sb.exec("cat /tmp/plan.md 2>/dev/null", timeout_s=60,
                                             workdir="/").stdout or None
            res["end"] = end.get("end") or "submitted"
            res["checkpoints_observed"] = observed
            res["egress_events"] = ns.egress.events()
    except Exception as e:  # noqa: BLE001 - recorded; the session tore itself down
        # a container that vanished mid-run is a kill, not a task result (rerun once, heavy)
        res["end"] = ("infra_killed" if N.GONE in str(e) else
                      "infra_error" if isinstance(e, SB.SandboxError) else "error")
        res["error"] = traceback.format_exc()[-6000:]
    finally:
        if ns.server is not None:
            sim = ns.server.sim
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


def capture(ns: N.NativeSession, *, cid: str, j: int, ctx: int, steps: list[dict], rd: Path,
            rj: dict, keep, cfg: BaseConfig, step: str, eligible,
            interventions: list | None = None) -> int:
    """Boundary j of conversation `cid`, right after its forced /compact (no agy process runs:
    the turn is over). The agent's background processes are stopped for the scan, the
    fingerprint and the kept commit; a refreshed token store is moved back to the volume;
    the commit is refused if the token's strings or the SimServer token are in the
    container's own filesystem."""
    import time

    from compaction.s2 import interact as I
    from compaction.s2 import interact_run as IR
    sb, server = ns.sb, ns.server
    k = N.next_k(rd)
    times: dict = {}
    stopped: dict[int, str] = {}
    t = time.monotonic()
    stopped = N.freeze(sb, CLI, "__emx_no_hook__", getattr(ns, "services", frozenset()))
    try:
        times["freeze_s"] = round(time.monotonic() - t, 1)
        link = keep_token_linked(sb)
        t = time.monotonic()
        scan = N.secret_scan(sb, {"EMX_SIM_TOKEN": server.token}, pattern_cmd=TOKEN_PATTERNS)
        times["scan_s"] = round(time.monotonic() - t, 1)
        if scan["hits"]:
            raise RuntimeError(f"a secret is in container files {scan['hits'][:10]}; "
                               "refusing to keep this image")
        t = time.monotonic()
        fp = I.fingerprint(I.guarded_runner(sb))
        times["fingerprint_s"] = round(time.monotonic() - t, 1)
        snap = N.snapshot(sb, keep, k)
        times["snapshot_s"] = round(snap["snapshot_s"], 1)
        sim_export, sim_rows = server.export(), [dict(r) for r in server.rows]
        probe = IR.sim_probe(server.sim)
    finally:
        N.signal_pids(sb, sorted(stopped), "CONT")
    snap["processes"] = [f"{p} {v}" for p, v in sorted(stopped.items())]
    nb = checkpoint_boundary(steps, j, model=cfg.agy.model, ctx_tokens=ctx)
    dbp = f"{STATE_DIR}/conversations/{cid}.db"
    screen(sb, [transcript_file(cid), dbp], server.token)
    db = sb.read_file(dbp, timeout_s=120)
    nb.native_files.update({
        "transcript_full.jsonl": "".join(json.dumps(x, ensure_ascii=False) + "\n"
                                         for x in steps),
        f"{cid}.db": db})
    services = sorted(getattr(ns, "services", ()) or ())
    why = ("not_quiescent" if stopped else "services_unvalidated" if services else
           "background_killed" if interventions else None)
    extra = {"harness": ARM, "cli_version": CLI_VERSION, "conversation_id": cid,
             "checkpoint_in_conversation": j, "compact_at": cfg.agy.compact_at,
             "model": cfg.agy.model, "secret_scan": {"scanned": scan["scanned"],
                                                     "seconds": scan["seconds"]},
             "token_link": link, "quiescent": not stopped, "services_running": services,
             "post_source": "checkpoint",
             # background tasks the runner killed in the turn before this boundary: then the
             # boundary is quiescent only after an intervention (astra l3 r1 #3)
             "background_kills": list(interventions or []),
             "quiescent_after_intervention": bool(interventions) and not stopped,
             "capture_times": times, "pre_tokens_kind": "trigger_request_context",
             # the hook measures the request of the call that crossed the threshold: not its
             # output nor the tool results the summarizer also saw (astra agy r1 #5)
             "pre_tokens_source": "hook: last call input+cache_read"}
    gate = eligible
    if why:
        extra["ineligible_reason"] = why
        if eligible is not None:
            def gate(b, f):
                return False
    N.write_boundary(rd, k, nb, run_json=rj, snap=snap, sim_export=sim_export,
                     sim_rows=sim_rows, step=step, step_instruction=cfg.task.steps.get(step, ""),
                     fingerprint=fp, sim_probe_sha256=probe,
                     calls_in_step=sum(1 for s in steps if s.get("type") == "PLANNER_RESPONSE"),
                     extra=extra, eligible=gate)
    return k
