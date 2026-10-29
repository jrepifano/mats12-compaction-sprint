"""Experiment 1's precedent in Antigravity (week-2 harness replication, plan v0.3.1).

The Claude Code driver (`cc_chain`) with agy in place of the CLI. One history = a first task in
A's container and a second task in B's container, one agy conversation carried across.

**Privacy:** Google sees everything agy sees: the task repos, the remote, the teammate's
draft and every prompt. Both task repos (httpx and tengo) are public projects, and the rest is
our own synthetic setup; never point this driver at a private repo.

- **Containers:** the pilot's `NativeSession` on the docker host (default rig, where the auth
  volume is), with the sidecar's agy allowlist (`common.ALLOW["agy"]`) and no extra forward;
  experiment 1's remote, git wrapper and scorer installed in it (`cc_chain.install_chain`).
- **The CLI:** agy 1.4.7 (the pilot's pinned volume, `agy.ensure_cli_volume`) with
  `gemini-3.8-flash-low` on Jacob's plan quota ($0; no key proxy), run as the pilot runs it
  (`agy.agy_command`: `-p`, `--dangerously-skip-permissions`, json output, `--print-timeout`)
  under the login shell `detached_exec` starts, so the git wrapper is the agent's git. The
  token store is linked from the auth volume (`agy.install_auth`). The config is the pilot's
  MCP entry and the settings rule headless mode needs for `ask_user`, and **no hooks**: the
  pilot's 150k PostInvocation hook forced compactions and is not used here.
- **Turns:** one `agy -p` call per turn, continued with `--conversation <cid>`; its end is a
  stop. `parse_stop` reads the turn's result and its new transcript steps into a `turns.Stop`
  (final text, `ask_user` or agy's own ask tools called, the submission token as the first
  output line of a command, infrastructure failure), and `turns.decide` gives the reply. agy
  has no turn limit flag, so there are no cut stops (unlike Claude Code's `--max-turns`).
- **Caps per task:** experiment 1's 400 model calls (PLANNER_RESPONSE steps) and 2 h
  (`HistoryConfig.max_calls` and `max_seconds`; the task-2 rerun raises them). Time is
  enforced in the container (`timeout`, and agy's `--print-timeout` 15 s inside it). Calls are
  checked before every turn and, while a turn runs, at `detached_exec`'s watch (about once a
  minute): at the cap every task process is stopped (`quiesce`) and the task ends
  "capped_calls". A turn can overshoot by the calls made between two checks; the count is
  recorded as it is, and a task over the cap ends capped even if it submitted.
- **Quiescence** (astra agy-build r1 #5): before the boundary's `/compact`, before the
  session export and before the evidence export, `quiesce` stops every process outside PID
  1, its sleep, the check's own tree and the image's services (agy, its children and orphans
  reparented to PID 1 alike, found in the container's process table), then re-reads the table
  until none is left. An unconfirmed stop ends the task "failed", with no handoff to B.
- **Compaction:** agy's in-task auto-compaction can't be turned off (plan v0.3.1: a recorded
  deviation). Every CHECKPOINT is recorded in result.json with its position
  (`agy_transcript.compactions`). At the boundary: one `/compact` turn, checked as exactly
  one new CHECKPOINT after our input (`agy.new_checkpoint_ok`), retried once, else task 2
  runs uncompacted as a recorded deviation.
- **Session transfer:** only the conversation's SQLite DB (`conversations/<cid>.db`, plus a
  `-wal`, `-shm` or `-journal` file if one exists) moves to B's container. The feasibility
  check (24 Oct) resumed a fork from a copied DB alone, and the pilot keeps exactly that file
  per boundary. The token store (a link to the auth volume), settings, MCP config and agy's
  `brain/` (task logs, tool-output files, the transcript log) stay behind; the manifest
  records the files moved (path, bytes, sha256) and a listing of what was left. B then
  resumes `<cid>` with experiment 1's lead-in and task-2 prompt (`cc_chain.lead_and_task`);
  a different conversation id from agy is a failed resume.
- **Screening** (astra agy-build r1 #1): nothing leaves a container before the pilot's
  screen (`agy.screen`: the token store's strings and the SimServer token) passes: each
  turn's output, the transcript at every read (before it becomes a stop's text or
  result.json), the conversation DB, the simulator's questions and the state listings
  (`screen_blob`), and the exported evidence (the scorer's home with the container logs and
  the remote, its archive, the git metadata: `screen_evidence`, which also scans every git
  object decoded). What fails or can't be screened is omitted, never stored. Process records
  keep pids and program names only.
- **Quota** (intent to treat): the plan's quota can't be read. A refusal ends the task
  "quota_refused" and sets the history's shared stop flag (`HistoryConfig.quota_stop`), which
  every launch checks first: the boundary's `/compact`, task 2, an infrastructure retry and,
  in the bulk, the next history. A quota-cut history keeps its partial evidence, is never
  rerun or replaced, and its primary outcome is unknown: B's result.json says
  "quota_refused" with `extra.primary_unknown_reason` (the scorer masks `teammate_lost`).
- **Endings:** as in `cc_chain` ("submitted", "stalled", "stopped", "capped_calls",
  "capped_time", "capped_context", "failed"), plus "quota_refused".
- **Run dirs** in experiment 1's format (`write_run`: result.json, messages.json through
  `agy_transcript.score_messages`, the exported scorer home), so `precedent.first_stage` and
  `score.score_b` score them unchanged. B's transcript is A's exported steps merged with
  B's log (`agy_transcript.merge_steps`), cut at the task-2 prompt (`b_message_index`).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from compaction import agy as AG
from compaction.deepswe import sandbox as SB
from compaction.s2 import interact as I
from compaction.s2.chain import config as CC
from compaction.s2.chain import env as E
from compaction.s2.native import agy as A
from compaction.s2.native import agy_transcript as AT
from compaction.s2.native import cc_chain as X
from compaction.s2.native import common as N
from compaction.s2.native import turns as T

ARM = "agy_chain"
OURS = N.purpose(ARM)  # our containers' name prefix ("native-agy-chain")
HOST = "ssh://rig"  # the desktop: the auth volume emx-native-agy-auth lives there
MODEL = A.MODEL
CLI_VERSION = A.CLI_VERSION
AGY = A.AgyConfig(model=MODEL, compact_at=None)


def set_model(model: str) -> None:
    """The session model for this process's agy runs (the five-task chains' agy-Sonnet arm,
    plan v0.4): `MODEL` (recorded in result.json) and `AGY` (what `agy_command` is given)
    together, so they never disagree. One model per process; call it before any run."""
    global MODEL, AGY
    MODEL = model
    AGY = A.AgyConfig(model=model, compact_at=None)
EGRESS_URL = "https://daily-cloudcode-pa.googleapis.com/"
CONVERSATIONS = f"{A.STATE_DIR}/conversations"
DB_SUFFIXES = ("", "-wal", "-shm", "-journal")
MAX_INFRA = X.MAX_INFRA
END_SUBMITTED, END_STALLED, END_STOPPED, END_FAILED = (X.END_SUBMITTED, X.END_STALLED,
                                                      X.END_STOPPED, X.END_FAILED)
END_CALLS, END_TIME, END_CONTEXT = X.END_CALLS, X.END_TIME, X.END_CONTEXT
END_QUOTA = "quota_refused"
# agy's wording for these is unverified (smoke checks); matched loosely, quota first
QUOTA_WORDS = ("resource_exhausted", "quota", "rate limit", "code 429", '"code": 429',
               '"code":429')
OVERFLOW_WORDS = ("context window", "context length", "too long", "token limit",
                  "exceeds the maximum", "input token count")
OMITTED = "omitted: failed or could not complete the secret screen"


class NotQuiescent(SB.SandboxError):
    pass


def install_cli_config(sb: SB.Sandbox, server: N.SimServer) -> None:
    """The pilot's MCP entry (token in the URL, the file on the tmpfs) and the settings rule
    headless mode needs for `ask_user` (feasibility, 24 Oct); no hooks.json (`agy.
    install_config` without its turn hook and threshold)."""
    r = sb.exec(f"mkdir -p {A.CONFIG_DIR} {A.STATE_DIR}", timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"agy config dirs: {r.stderr[-300:]}")
    url = f"{N.MCP_URL}?{N.TOKEN_QUERY}={server.token}"
    sb.put_file(json.dumps({"mcpServers": {"user-server": {"url": url}}}).encode(),
                f"{A.XDIR}/mcp_config.json")
    sb.put_file(json.dumps({"permissions": {"allow": ["mcp(user-server/ask_user)"]}}).encode(),
                A.SETTINGS)
    r = sb.exec(f"rm -f {A.CONFIG_DIR}/hooks.json && "
                f"ln -sfn {A.XDIR}/mcp_config.json {A.CONFIG_DIR}/mcp_config.json",
                timeout_s=30, workdir="/")
    if r.rc != 0:
        raise SB.SandboxError(f"agy config: {r.stderr[-300:]}")


def _error_text(out: dict, err: str) -> str:
    parsed = AG.parse_agy_error(err or "")
    return " ".join([json.dumps(parsed or {}), json.dumps(out.get("error") or ""),
                     err or ""]).lower()


def final_text(out: dict, new_steps: list[dict]) -> tuple[str, str]:
    """The turn's final text: agy's `response`, else the last new model message that called
    no tool (and where it came from)."""
    if isinstance(out.get("response"), str) and out["response"].strip():
        return out["response"], "response"
    for st in reversed(new_steps):
        if st.get("type") == "PLANNER_RESPONSE" and not st.get("tool_calls") \
                and (st.get("content") or "").strip():
            return st["content"], "transcript"
    return "", "none"


def parse_stop(out: dict, err: str, new_steps: list[dict],
               problem: str | None) -> tuple[T.Stop, dict]:
    """One turn -> the stop `turns.decide` reads, and what else it showed. `problem` is
    `agy._turn_ok`'s verdict on the finished turn (None: it ended normally). A print timeout
    is the time cap; a quota refusal or a context overflow ends the task; any other problem
    is an infrastructure failure (resumed)."""
    text, source = final_text(out, new_steps)
    info = {"status": out.get("status"), "conversation_id": out.get("conversation_id"),
            "cap": None, "overflow": False, "quota": False, "problem": problem,
            "text_source": source}
    if problem:
        blob = _error_text(out, err) + " " + problem.lower()
        if problem.startswith("cli_timeout"):
            info["cap"] = END_TIME
        elif any(w in blob for w in QUOTA_WORDS):
            info["quota"] = True
        elif any(w in blob for w in OVERFLOW_WORDS):
            info["overflow"] = True
    failed = problem is not None and not (info["cap"] or info["quota"] or info["overflow"])
    return T.Stop(text=text, asked_user=AT.asked(new_steps), marker=AT.submitted(new_steps),
                  infra_failed=failed), info




# ------------------------------------------------------------------------------------------
# Quiescence and screening
# ------------------------------------------------------------------------------------------


def _prog(args: str) -> str:
    """A process's program name only (its arguments are agent-controlled: never recorded)."""
    first = (args or "").split()[:1]
    return Path(first[0]).name[:40] if first else "?"


def _live(procs: dict, tok: str, services) -> list[int]:
    """Every live process outside PID 1, its `sleep infinity`, this check's own tree and the
    image's services (with their trees): agy, its children and orphans alike."""
    keep = {1} | {p for p, (pp, _st, a) in procs.items() if pp == 1 and a.strip() ==
                  "sleep infinity"}
    keep |= N._tree(procs, {p for p, v in procs.items() if tok in v[2]})
    keep |= N._tree(procs, {p for p in services if p in procs})
    return sorted(p for p, (_pp, st, _a) in procs.items()
                  if p not in keep and not st.startswith("Z"))


def quiesce(sb: SB.Sandbox, services=frozenset(), rounds: int = 3,
            wait_s: float = 2.0) -> dict:
    """Stop every task process (`_live`), TERM then KILL, re-reading the process table each
    round, and confirm none is left. {"confirmed", "stopped", "survivors", "errors"}, with
    pids and program names only. A failed reading is unconfirmed."""
    rec: dict = {"confirmed": False, "stopped": [], "survivors": [], "errors": []}
    try:
        for _ in range(rounds):
            procs, tok = N.ps_table(sb)
            live = _live(procs, tok, services)
            if not live:
                rec["confirmed"] = True
                return rec
            for p in live:
                tag = f"{p} {_prog(procs[p][2])}"
                if tag not in rec["stopped"]:
                    rec["stopped"].append(tag)
            for sig in ("TERM", "KILL"):
                try:
                    N.signal_pids(sb, live, sig)
                except SB.SandboxError as e:
                    rec["errors"].append(f"{sig}: {e}"[:200])
                time.sleep(wait_s)
        procs, tok = N.ps_table(sb)
        live = _live(procs, tok, services)
        rec["survivors"] = [f"{p} {_prog(procs[p][2])}" for p in live]
        rec["confirmed"] = not live
    except Exception as e:  # noqa: BLE001 - an unreadable table is never quiescent
        rec["errors"].append(f"{type(e).__name__}: {e}"[:200])
        rec["confirmed"] = False
    return rec


def require_quiet(sb: SB.Sandbox, services, log: list, why: str) -> dict:
    """`quiesce`, recorded in `log`; raises NotQuiescent unless confirmed."""
    q = quiesce(sb, services)
    log.append({"why": why, **q})
    if not q["confirmed"]:
        raise NotQuiescent(f"task processes not stopped ({why}): "
                           f"{q['survivors'][:5] or q['errors'][:2]}")
    return q


def screen_blob(sb: SB.Sandbox, data: bytes, sim_token: str) -> bool:
    """True when `data` passes the pilot's screen (written to a temp file in the container,
    screened there, removed); False on a hit or a failed screen."""
    path = f"/tmp/emx-screen-{uuid.uuid4().hex[:8]}"
    try:
        sb.put_file(data, path)
        A.screen(sb, [path], sim_token)
        return True
    except Exception:  # noqa: BLE001 - a hit (SecretFound) or no verdict: omit
        return False
    finally:
        with contextlib.suppress(Exception):
            sb.exec(f"rm -f {path}", timeout_s=30, workdir="/")


def screened(sb: SB.Sandbox, obj, sim_token: str):
    """`obj` if its JSON passes the screen, else OMITTED."""
    return obj if screen_blob(sb, json.dumps(obj, default=str).encode(), sim_token) \
        else OMITTED


EVIDENCE_DIR = "/tmp/emx-evidence-screen"  # the post-export copy, screened in the container
STAGE_DIR = "/tmp/emx-evidence-stage"  # what has no file of its own, staged before export


def names_screen(rs: str) -> str:
    """The screen's step over every path name and symlink target under the (quoted) roots
    `rs` against the pattern file "$P": a secret in a file name (e.g. an archive member)
    otherwise passes a content-only grep (astra cx-build r3 #2). Sets hit=1 on a match; a
    failed find or grep is an error (fail 6 / 4)."""
    return (f"s=$(find -H {rs} -print \\( -type l -exec readlink {{}} + \\) 2>/dev/null "
            "| grep -cF -f \"$P\"; echo \"${PIPESTATUS[0]} ${PIPESTATUS[1]}\"); "
            "set -- $s; [ \"$2\" = 0 ] || fail 6; [ \"$3\" -gt 1 ] && fail 4; "
            "[ \"$1\" -gt 0 ] && { echo NAMES; hit=1; }; ")


def evidence_screen_script(roots: list[str], unpack: str | None = None) -> str:
    """In-container, over `roots`: the pilot's patterns (the token store's strings and
    $EMX_SIM_TOKEN); the archives under `unpack` (a staging copy, never a live repo) unpacked;
    grep -rlF over every file; then every git dir's objects decoded (`cat-file
    --batch-all-objects --batch`, streamed into grep -c: compressed objects hide text from
    grep, and nothing is written), with both commands' exit codes checked. Roots that are
    symlinks (the remote at /srv/git/<repo>.git may be one) are followed (`find -H`; grep -r
    follows them anyway). Exit 0 + SCREEN_HIT, 1 + SCREEN_CLEAN, else an error (never
    clean)."""
    rs = " ".join(shlex.quote(r) for r in roots)
    unpack_step = "" if unpack is None else (
        f"for t in $(find {shlex.quote(unpack)} -type f -name '*.tgz'); do "
        "mkdir -p \"$t.x\" && tar xzf \"$t\" -C \"$t.x\" || fail 6; done; ")
    return (
        "set -o pipefail; fail() { rm -f \"$P\"; exit $1; }; "
        "P=$(mktemp -p /dev/shm) || exit 3; { " + A.TOKEN_PATTERNS
        + "; } > \"$P\" || fail 5; printf '%s\\n' \"$EMX_SIM_TOKEN\" >> \"$P\"; hit=0; "
        + unpack_step + names_screen(rs if unpack is None else f"{rs} {shlex.quote(unpack)}")
        + f"grep -rlF -f \"$P\" -- {rs}; g=$?; [ $g -gt 1 ] && fail 4; [ $g -eq 0 ] && hit=1; "
        f"for o in $(find -H {rs} -type d -name objects); do d=${{o%/objects}}; "
        "[ -f \"$d/HEAD\" ] || continue; "
        "s=$(git -c safe.directory='*' --git-dir=\"$d\" cat-file --batch-all-objects --batch "
        "2>/dev/null | grep -cF -f \"$P\"; echo \"${PIPESTATUS[0]} ${PIPESTATUS[1]}\"); "
        "set -- $s; [ \"$2\" = 0 ] || fail 6; [ \"$3\" -gt 1 ] && fail 4; "
        "[ \"$1\" -gt 0 ] && { echo \"$d\"; hit=1; }; done; rm -f \"$P\"; "
        "if [ $hit -eq 1 ]; then echo SCREEN_HIT; exit 0; fi; echo SCREEN_CLEAN; exit 1")


def _verdict(r) -> tuple[bool | None, str | None]:
    """(True: clean, False: a hit, None: no verdict) from a screen script's result."""
    lines = r.stdout.split()
    if r.rc == 0 and "SCREEN_HIT" in lines:
        return False, "a secret is in the evidence"
    if r.rc == 1 and "SCREEN_CLEAN" in lines:
        return True, None
    return None, f"screen failed rc={r.rc}"


def container_log_bytes(sb: SB.Sandbox) -> bytes:
    """`docker logs` of the task container, held in memory only (never written on the host
    before it is screened)."""
    import subprocess
    p = subprocess.run(SB.docker_argv(["logs", sb.name], sb.host), capture_output=True,
                       timeout=120, check=False)
    if p.returncode != 0:
        raise SB.SandboxError(f"docker logs rc={p.returncode}")
    return p.stdout + b"\n" + p.stderr


def prescreen_evidence(sb: SB.Sandbox, bare: str, sim_token: str, tries: int = 2,
                       run_env=None) -> dict:
    """Before `ChainEnv.finish_traps` writes anything on the host (astra agy-build r2 #1):
    every source of the exported evidence, screened in the container. The remote (`bare`) and
    the workspace's git dir (hooks, config, refs, every object decoded: the snapshot's
    commit messages, names and branches come from there) are screened in place; the
    container's log stream (held in memory) and the workspace's paths and link targets (what
    the fs snapshot records) are staged in STAGE_DIR first. The container is quiescent, so
    nothing changes in between. {"ok", "why", "attempts"}; ok only on a clean verdict."""
    rec: dict = {"ok": False, "why": None, "attempts": 0}
    for attempt in range(1, tries + 1):
        rec["attempts"] = attempt
        try:
            g = sb.exec(I.NOSHIM + "cd /app 2>/dev/null && git rev-parse --absolute-git-dir",
                        timeout_s=60, workdir="/")
            gitdir = g.stdout.strip().splitlines()[-1] if g.rc == 0 and g.stdout.strip() \
                else None
            r = sb.exec(f"rm -rf {STAGE_DIR} && mkdir -p {STAGE_DIR} && find /app -path "
                        f"/app/.git -prune -o -printf '%p\\t%l\\n' > {STAGE_DIR}/paths.txt",
                        timeout_s=300, workdir="/")
            if r.rc != 0:
                raise SB.SandboxError(f"staging the path list rc={r.rc}")
            sb.put_file(container_log_bytes(sb), f"{STAGE_DIR}/container.log")
            roots = [STAGE_DIR, bare] + ([gitdir] if gitdir else [])
            res = (run_env or N.exec_env)(sb, I.NOSHIM + evidence_screen_script(roots),
                          {"EMX_SIM_TOKEN": sim_token}, timeout_s=1200)
            ok, why = _verdict(res)
        except Exception as e:  # noqa: BLE001 - no verdict: tried again, then omitted
            ok, why = None, f"screen error: {type(e).__name__}"
        finally:
            with contextlib.suppress(Exception):
                sb.exec(f"rm -rf {STAGE_DIR}", timeout_s=60, workdir="/")
        rec |= {"ok": ok is True, "why": why}
        if ok is not None:
            return rec
    return rec


def evidence_files(out: Path) -> list[Path]:
    """What `ChainEnv.finish_traps` exports into a task's run dir."""
    out = Path(out)
    return [p for p in [out / "traps_home", out / "traps_home.tgz",
                        *sorted(out.glob("git_meta_*"))] if p.exists()]


def screen_evidence(sb: SB.Sandbox, out: Path, sim_token: str, tries: int = 2) -> dict:
    """After the export, a second check of what was written: the run dir's evidence
    (`evidence_files`) copied back into the container and screened there; a failed screen
    is tried again once. {"ok", "why", "scanned"}; ok only on a clean verdict."""
    files = evidence_files(out)
    rec: dict = {"ok": True, "why": None, "scanned": [p.name for p in files]}
    if not files:
        return rec
    for attempt in range(1, tries + 1):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                stage = Path(tmp) / "ev"
                stage.mkdir()
                for p in files:
                    if p.is_dir():
                        shutil.copytree(p, stage / p.name, symlinks=True)
                    else:
                        shutil.copy2(p, stage / p.name)
                sb.exec(f"rm -rf {EVIDENCE_DIR}", timeout_s=60, workdir="/")
                sb.copy_in(stage, EVIDENCE_DIR, timeout_s=900)
            r = N.exec_env(sb, I.NOSHIM + evidence_screen_script([EVIDENCE_DIR],
                                                                 unpack=EVIDENCE_DIR),
                           {"EMX_SIM_TOKEN": sim_token}, timeout_s=900)
            ok, why = _verdict(r)
        except Exception as e:  # noqa: BLE001 - no verdict: tried again, then omitted
            ok, why = None, f"screen error: {type(e).__name__}"
        finally:
            with contextlib.suppress(Exception):
                sb.exec(f"rm -rf {EVIDENCE_DIR}", timeout_s=60, workdir="/")
        rec |= {"ok": ok is True, "why": why, "attempts": attempt}
        if ok is not None:
            return rec
    return rec


def omit_evidence(out: Path) -> list[str]:
    """Remove the exported evidence from the run dir (it failed or missed the screen)."""
    gone = []
    for p in evidence_files(out):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        gone.append(p.name)
    return gone


def discover_cid(sb: SB.Sandbox) -> str | None:
    """The conversation id when agy didn't report one: the only DB in a fresh state dir."""
    r = sb.exec(f"ls {CONVERSATIONS} 2>/dev/null", timeout_s=30, workdir="/")
    dbs = [x[:-3] for x in r.stdout.split() if x.endswith(".db")]
    return dbs[0] if r.rc == 0 and len(dbs) == 1 else None


CALL_INDEX_CMD = (f"cat {A.STATE_DIR}/brain/*/.system_generated/logs/transcript_full.jsonl "
                  "2>/dev/null | grep '\"PLANNER_RESPONSE\"' | grep -o '\"step_index\": *[0-9]*'"
                  " | grep -o '[0-9]*$'; true")


def count_calls(sb: SB.Sandbox, first_step: int) -> int | None:
    """Model calls at or after `first_step` in the container's transcript log (a fresh state
    dir holds one conversation); None when the check fails. Only digits leave the container."""
    r = sb.exec(CALL_INDEX_CMD, timeout_s=30, workdir="/")
    if r.rc != 0:
        return None
    return sum(1 for x in r.stdout.split() if x.isdigit() and int(x) >= first_step)


def state_listing(sb: SB.Sandbox, sim_token: str) -> list[str] | str:
    """What agy's state dir holds (type, bytes, path; names only, never contents), screened."""
    r = sb.exec(f"cd {A.STATE_DIR} 2>/dev/null && find . -maxdepth 4 -printf '%y %s %p\\n' "
                "| sort | head -n 400", timeout_s=60, workdir="/")
    if r.rc != 0:
        return f"listing failed rc={r.rc}"
    lines = r.stdout.splitlines()
    return screened(sb, lines, sim_token)


def task_steps(sb: SB.Sandbox, cid: str, sim_token: str) -> list[dict]:
    """The container's transcript log, screened before it is read."""
    A.screen(sb, [A.transcript_file(cid)], sim_token)
    return A.read_steps(sb, cid)


# ------------------------------------------------------------------------------------------
# One task's turns
# ------------------------------------------------------------------------------------------


@dataclass
class TaskRun:
    """One task's driving loop in a live session; the caps are the driver's."""

    ns: N.NativeSession
    out: Path
    deadline: float  # time.monotonic() at which the task's 2 h end
    first_step: int = 0  # this task's steps have step_index >= first_step
    cid: str | None = None
    env: dict = field(default_factory=dict)
    max_calls: int = CC.MAX_CALLS
    services: frozenset = frozenset()
    quota_stop: threading.Event = field(default_factory=threading.Event)
    turns: list[dict] = field(default_factory=list)
    state: T.State = field(default_factory=T.State)
    steps: list[dict] = field(default_factory=list)  # the container's log, as last read
    quiet_log: list[dict] = field(default_factory=list)
    stop_reason: str | None = None
    # set when quota interrupted this task ("refused" here, or "shared stop": another
    # history's refusal kept a launch from happening); kept whatever the task's final ending
    quota_cut: str | None = None

    def left_s(self) -> float:
        return self.deadline - time.monotonic()

    def read(self) -> list[dict]:
        """The transcript, screened first (astra agy-build r1 #1); a log agy hasn't written yet
        keeps the last reading."""
        if self.cid:
            try:
                self.steps = task_steps(self.ns.sb, self.cid, self.ns.server.token)
            except A.SecretFound:
                raise
            except SB.SandboxError as e:
                if "No such file" not in str(e):
                    raise
        return self.steps

    def n_calls(self) -> int:
        return len(AT.calls(self.steps, self.first_step))

    def quiet(self, why: str) -> bool:
        """Verified quiescence (`quiesce`), recorded in quiet_log."""
        q = quiesce(self.ns.sb, self.services)
        self.quiet_log.append({"why": why, **q})
        return bool(q["confirmed"])

    def _watch(self, hit: dict):
        def watch() -> bool:
            n = count_calls(self.ns.sb, self.first_step)
            if n is not None and n >= self.max_calls:
                hit["calls"] = n
                return True
            return False
        return watch

    def call(self, prompt: str, k: int, *, extra_s: int = 0,
             cap_calls: bool = True) -> tuple[T.Stop, dict, list[dict]]:
        """One agy turn with the task's remaining time (plus `extra_s`, for the boundary's own
        turn); returns the stop, its info and the turn's new steps. The call cap's watch runs
        unless `cap_calls` is off (the boundary). Nothing is launched once the shared quota
        flag is set (astra agy-build r2 #2: every turn, the first included, and the
        boundary's); a refusal sets the flag at once."""
        if self.quota_stop.is_set():
            info = {"cap": None, "overflow": False, "quota": True, "shared": True,
                    "problem": None}
            self.quota_cut = self.quota_cut or "shared stop"
            self.turns.append({"k": k, "prompt": prompt[:200], "launched": False,
                               "quota_stop": True, "info": info})
            return T.Stop(text=""), info, []
        left = int(self.left_s()) + extra_s
        if left <= 30:
            info = {"cap": END_TIME, "overflow": False, "quota": False, "problem": None}
            self.turns.append({"k": k, "prompt": prompt[:200], "launched": False, "info": info})
            return T.Stop(text=""), info, []
        sb, hit = self.ns.sb, {}
        log = self.out / f"turn_{k:03d}.json"
        before = {s.get("step_index") for s in self.steps}
        # the wrapper shell's own output (agy's goes to the tmpfs): `detached_exec` streams it
        # to the host as it comes, so into a temp dir, kept in the run dir only once screened
        # (a login profile the agent edited could print into it)
        with tempfile.TemporaryDirectory(prefix="emx-agy-turn-") as tmp:
            tlog = Path(tmp) / "exec"
            r = N.detached_exec(sb, A.agy_command(AGY, prompt, self.cid, max(30, left - 15)),
                                name=f"agy-turn-{k}-{uuid.uuid4().hex[:6]}", timeout_s=left,
                                log_path=tlog, env=self.env,
                                watch=self._watch(hit) if cap_calls else None)
            wrapper = {sfx: (Path(str(tlog) + sfx).read_bytes()
                             if Path(str(tlog) + sfx).exists() else b"")
                       for sfx in ("", ".err")}
        rec = {"k": k, "prompt": prompt[:200], "rc": r.rc, "seconds": r.seconds,
               "killed": r.killed}
        for sfx, data in wrapper.items():
            if not data:
                continue
            if screen_blob(sb, data, self.ns.server.token):
                Path(str(log) + ".exec" + sfx).write_bytes(data)
            else:
                rec.setdefault("wrapper_output_omitted", []).append(sfx or ".out")
        if hit:  # the call cap: every task process stopped, verified, before anything else
            self.turns.append(rec | {"cap_hit_at": hit["calls"]})
            require_quiet(sb, self.services, self.quiet_log, "call cap")
            self.turns.pop()
        if r.killed in ("container_gone", "output_lost"):
            info = {"cap": None, "overflow": False, "quota": False,
                    "problem": f"infra_killed: {r.killed}"}
            self.turns.append(rec | {"info": info})
            return T.Stop(text="", infra_failed=True), info, []
        A.screen(sb, [A.TURN_OUT, A.TURN_ERR], self.ns.server.token)
        out_text = sb.exec(f"cat {A.TURN_OUT} 2>/dev/null; true", timeout_s=60,
                           workdir="/").stdout
        err = sb.exec(f"cat {A.TURN_ERR} 2>/dev/null; true", timeout_s=60, workdir="/").stdout
        log.write_text(out_text)
        Path(str(log) + ".err").write_text(err)
        out = A.result_of(log)
        got = out.get("conversation_id")
        if self.cid and got and got != self.cid:  # a fresh start is not the design: unknown
            self.turns.append(rec | {"conversation_id": got})
            raise SB.SandboxError(f"agy did not resume {self.cid}: it ran {got}")
        self.cid = self.cid or got or discover_cid(sb)
        steps = self.read()
        # this turn's steps only: never A's (B's log may hold them again, A's own submission
        # echo among them)
        new = [s for s in steps if s.get("step_index") not in before
               and isinstance(s.get("step_index"), int) and s["step_index"] >= self.first_step]
        problem = None if hit else A._turn_ok(r, out, err, max(30, left - 15))
        stop, info = parse_stop(out, err, new, problem)
        if hit or self.n_calls() > self.max_calls:
            info["cap"] = END_CALLS
        if info.get("quota"):
            self.quota_stop.set()
            self.quota_cut = "refused"
        idx = [s.get("step_index") for s in new]
        rec |= {"status": out.get("status"), "usage_total": out.get("usage"),
                "new_steps": [idx[0], idx[-1]] if idx else None,
                "calls_so_far": self.n_calls(), "stop": stop.__dict__, "info": info}
        if hit:
            rec["cap_hit_at"] = hit["calls"]
        self.turns.append(rec)
        return stop, info, new

    def drive(self, first: str) -> str:
        """Runs the task to its end; returns its end value."""
        prompt, infra_run = first, 0
        while True:
            if self.left_s() <= 30:
                return END_TIME
            if self.n_calls() >= self.max_calls:
                return END_CALLS
            stop, info, _new = self.call(prompt, len(self.turns))
            if info.get("quota"):
                self.stop_reason = "shared stop" if info.get("shared") else "refused"
                return END_QUOTA
            d = T.decide(stop, self.state)
            self.turns[-1]["decision"] = d.__dict__
            if info.get("cap"):
                return info["cap"]
            if info.get("overflow"):
                return END_CONTEXT
            if d.kind == "infra_resume":
                infra_run += 1
                if infra_run > MAX_INFRA:
                    return END_FAILED
                prompt = A.CONTINUE_PROMPT if self.cid else first  # no conversation: anew
                continue
            infra_run = 0
            if d.kind == "complete":
                return END_SUBMITTED
            if d.kind == "stalled":
                return END_STALLED
            if d.kind == "exhausted":
                return END_STOPPED
            prompt = d.text


# ------------------------------------------------------------------------------------------
# Boundary and transfer
# ------------------------------------------------------------------------------------------


def n_checkpoints(steps: list[dict]) -> int:
    return sum(1 for s in steps if s.get("type") == "CHECKPOINT")


def boundary_compact(run: TaskRun) -> dict:
    """One `/compact` turn; exactly one new CHECKPOINT after our input, else retried once.
    Compactions before it (agy's own, in the task) are counted in "before". Every attempt,
    the first included, waits on verified quiescence: unconfirmed, no further attempt (the
    task then ends "failed" with no handoff). Never launched after a quota refusal (the
    shared flag); a refusal here stops the retries."""
    out: dict = {"attempts": []}
    if run.quota_stop.is_set():
        return out | {"ok": False, "quota": True, "skipped": "quota stop"}
    try:
        steps = run.read()
    except Exception as e:  # noqa: BLE001 - reported, and task 2 still runs uncompacted
        return out | {"ok": False, "before": None,
                      "deviation": f"transcript unreadable before the boundary: {e}"[:300]}
    before = n_checkpoints(steps)
    out |= {"before": before,
            "auto_before": sum(c["trigger"] == "auto" for c in AT.compactions(steps))}
    for attempt in (1, 2):
        if run.quota_stop.is_set():
            return out | {"ok": False, "quota": True, "deviation": "quota stop before a retry"}
        if not run.quiet(f"before boundary attempt {attempt}"):  # astra agy-build r2 #5
            return out | {"ok": False, "not_quiescent": True,
                          "deviation": f"not quiescent before boundary attempt {attempt}"}
        try:
            _stop, info, _new = run.call("/compact", 900 + attempt, extra_s=900,
                                         cap_calls=False)
            steps = run.read()
        except Exception as e:  # noqa: BLE001 - a failed attempt is retried, then reported
            out["attempts"].append({"attempt": attempt, "error": f"{type(e).__name__}: {e}"})
            continue
        after = n_checkpoints(steps)
        out["attempts"].append({"attempt": attempt, "after": after, "info": info})
        if info.get("quota"):
            run.quota_stop.set()
            return out | {"ok": False, "quota": True,
                          "deviation": "quota stop before the boundary" if info.get("shared")
                          else "quota refused at the boundary"}
        if after == before + 1 and A.new_checkpoint_ok(steps, before):
            cp = [s for s in steps if s.get("type") == "CHECKPOINT"][-1]
            return out | {"ok": True, "checkpoints": after,
                          "checkpoint_step": cp.get("step_index")}
        if after > before + 1:
            return out | {"ok": False, "deviation": f"{after - before} new checkpoints"}
        if after == before + 1:  # a checkpoint, but not after our input
            return out | {"ok": False, "deviation": "a new checkpoint not after /compact"}
    return out | {"ok": False, "deviation": "compaction failed twice; task 2 uncompacted"}


def db_paths(cid: str) -> list[str]:
    return [f"{CONVERSATIONS}/{cid}.db{s}" for s in DB_SUFFIXES]


def export_session(sb: SB.Sandbox, cid: str, sim_token: str,
                   steps: list[dict]) -> tuple[dict[str, bytes], dict]:
    """The conversation DB (and its side files, if any), screened, with the manifest: each
    file's path (relative to agy's state dir), bytes and sha256, the checkpoints and last
    step at export, and a listing of what stayed behind. Call only on a quiescent container."""
    r = sb.exec("for f in " + " ".join(shlex.quote(p) for p in db_paths(cid))
                + "; do [ -f \"$f\" ] && echo \"$f\"; done; true", timeout_s=30, workdir="/")
    present = [p for p in r.stdout.split() if p in db_paths(cid)]
    if f"{CONVERSATIONS}/{cid}.db" not in present:
        raise SB.SandboxError(f"no conversation DB for {cid}")
    A.screen(sb, present, sim_token)
    blobs, files = {}, []
    for p in present:
        data = sb.read_file(p, timeout_s=300)
        rel = p.removeprefix(A.STATE_DIR + "/")
        blobs[rel] = data
        files.append({"path": rel, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    idx = [s["step_index"] for s in steps if isinstance(s.get("step_index"), int)]
    man = {"files": files, "checkpoints": n_checkpoints(steps),
           "last_step_index": max(idx, default=-1), "conversation_id": cid,
           "left_behind": state_listing(sb, sim_token)}
    return blobs, man


def import_session(sb: SB.Sandbox, blobs: dict[str, bytes]) -> dict:
    """Write the DB into B's state dir and check each file's sha256 there."""
    want = {}
    for rel, data in blobs.items():
        dest = f"{A.STATE_DIR}/{rel}"
        sb.put_file(data, dest)
        want[dest] = hashlib.sha256(data).hexdigest()
    r = sb.exec("sha256sum " + " ".join(shlex.quote(p) for p in want), timeout_s=60,
                workdir="/")
    got = {ln.split()[1]: ln.split()[0] for ln in r.stdout.splitlines() if len(ln.split()) == 2}
    if r.rc != 0 or got != want:
        raise SB.SandboxError(f"session import: sha256 mismatch {sorted(set(want) - set(got))}")
    return {"files": sorted(want), "verified": True}


# ------------------------------------------------------------------------------------------
# Run dirs, tasks, histories
# ------------------------------------------------------------------------------------------


def write_run(out: Path, *, end: str, t0: float, steps: list[dict] | None, first_step: int,
              start_index: int | None, home: Path | None, extra: dict,
              error: str | None = None, second: bool = False) -> None:
    """result.json (always) and messages.json in experiment 1's format (score_b,
    first_stage). The counts are this task's (steps from `first_step` on); agy runs on the
    plan quota, so cost is 0. A second task whose cutoff can't be found gets no
    messages.json (its transcript measures read unknown, never task 1's commands)."""
    info = None
    if steps is not None and not (second and start_index is None):
        msgs, info = AT.score_messages(steps)
        (out / "messages.json").write_text(json.dumps(msgs))
    u = AT.usage(steps or [], first_step)
    res = {"end": end, **u, "cost_usd": 0.0, "seconds": round(time.monotonic() - t0, 1),
           "traps_home": str(home) if home else None, "error": error,
           "extra": {"b_message_index": start_index, "transcript_info": info,
                     "harness": "agy", "cli_version": CLI_VERSION, "model": MODEL,
                     "first_step": first_step,
                     "compactions": AT.compactions(steps or [], first_step), **extra}}
    (out / "result.json").write_text(json.dumps(res, default=str))


def skipped_run(out: Path, *, end: str, why: str, second: bool,
                primary_unknown: str | None = None) -> None:
    """result.json for a task that never started (setup failed, no session, quota)."""
    extra: dict = {"skipped": why}
    if primary_unknown:
        extra |= {"primary_unknown_reason": primary_unknown, "quota_cut": True}
    write_run(out, end=end, t0=time.monotonic(), steps=None, first_step=0, start_index=None,
              home=None, second=second, extra=extra, error=why)


def quota_marks(res: dict | None) -> str | None:
    """The quota cut a task's result.json records, if any: the task's own interruption
    (`primary_unknown_reason`) or the history's (`history_quota_cut`: the boundary refused,
    task 1 itself complete). A persisted mark is never replaced (intent to treat)."""
    if not res:
        return None
    ex = res.get("extra") or {}
    if res.get("end") == END_QUOTA or ex.get("quota_cut"):
        return ex.get("primary_unknown_reason") or "quota"
    return ex.get("history_quota_cut")


def sanitize(diag: dict) -> dict:
    """The diagnostics that failed (or missed) the screen, reduced to fixed fields: never the
    agent-reachable text (exception messages, process names, stop texts)."""
    bnd = diag.get("boundary")
    return {
        "quiescence": [{"why": q.get("why"), "confirmed": q.get("confirmed")}
                       for q in diag.get("quiescence") or []],
        "boundary": None if bnd is None else {
            k: bnd.get(k) for k in ("ok", "quota", "before", "auto_before", "checkpoints",
                                    "checkpoint_step", "not_quiescent", "skipped")},
        "turns": [{"k": t.get("k"), "launched": t.get("launched", True),
                   "decision": (t.get("decision") or {}).get("kind"),
                   "cap": (t.get("info") or {}).get("cap"),
                   "quota": (t.get("info") or {}).get("quota")}
                  for t in diag.get("turns") or []],
        "evidence_screen": {k: (None if v is None else {"ok": v.get("ok")}) for k, v in
                            (diag.get("evidence") or {}).items()},
    }


def _session(image: str, cfg: HistoryConfig) -> N.NativeSession:
    return N.NativeSession(ARM, image, lambda runner: X.ScriptedSim(), host=cfg.host,
                           allow=N.ALLOW["agy"], extra_args=A.run_args(),
                           memory_mb=cfg.memory_mb, cpus=cfg.cpus,
                           min_free_gb=cfg.min_free_gb)


PRECOMPACT_REPO = f"emx-keep/{OURS}"  # kept images: outside every run's sweep


def _exec_np(sb: SB.Sandbox, cmd: str, env: dict[str, str] | None = None, *,
             timeout_s: int = 300, workdir: str = "/") -> SB.ExecResult:
    """`docker exec` with a profile-free shell (`bash --noprofile --norc`: no login profile the
    agent may have edited runs; astra agy-precompact r2 B3), values of `env` only in the
    client's environment, and a client timeout that raises without removing the container."""
    env = env or {}
    argv = SB.docker_argv(["exec", *[a for k in env for a in ("-e", k)], "-w", workdir, sb.name,
                           "timeout", "-k", "5", str(int(timeout_s)), "bash", "--noprofile",
                           "--norc", "-c", cmd], sb.host)
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout_s + SB.CLIENT_GRACE_S,
                           env={**os.environ, **env}, check=False)
    except subprocess.TimeoutExpired:
        raise SB.SandboxError(f"client timeout on {sb.name} (container kept)") from None
    return SB.ExecResult(p.returncode, p.stdout.decode(errors="replace"),
                         p.stderr.decode(errors="replace"), time.monotonic() - t0)


def commit_names_screen(sb: SB.Sandbox, sim_token: str) -> dict:
    """Every path a commit would keep (`find -xdev`, changed since the session started: the
    same set as `N.secret_scan`, which reads contents only) and every such symlink's target,
    grepped for the token store's strings and the SimServer token (astra agy-precompact r1
    B3). The patterns go to /dev/shm (never committed). {"ok", "why"}: ok only on a clean,
    complete verdict."""
    m = shlex.quote(N.START_MARKER)
    script = (
        "set -o pipefail; umask 077; P=$(mktemp /dev/shm/names.XXXXXX) || exit 3; "
        "{ " + A.TOKEN_PATTERNS + "; } > \"$P\" || { rm -f \"$P\"; exit 5; }; "
        "printf '%s\\n' \"$EMX_SIM_TOKEN\" >> \"$P\" || { rm -f \"$P\"; exit 5; }; "
        "grep -qx '' \"$P\" && { rm -f \"$P\"; exit 5; }; "
        f"[ -f {m} ] || {{ rm -f \"$P\"; exit 4; }}; "
        f"s=$(find / -xdev \\( -newer {m} -o -cnewer {m} \\) -print \\( -type l -exec readlink {{}} + \\) "
        "| grep -cF -f \"$P\"; echo \"${PIPESTATUS[0]} ${PIPESTATUS[1]}\"); rm -f \"$P\"; "
        "set -- $s; [ \"$2\" = 0 ] || exit 6; [ \"$3\" -gt 1 ] && exit 4; "
        "if [ \"$1\" -gt 0 ]; then echo NAMES_HIT; exit 0; fi; echo NAMES_CLEAN; exit 1")
    try:
        r = _exec_np(sb, script, {"EMX_SIM_TOKEN": sim_token}, timeout_s=900)
    except Exception as e:  # noqa: BLE001 - no verdict: never clean
        return {"ok": False, "why": f"names screen error: {type(e).__name__}"}
    lines = r.stdout.split()
    if r.rc == 1 and "NAMES_CLEAN" in lines:
        return {"ok": True, "why": None}
    return {"ok": False, "why": "a secret in a path name or link target" if r.rc == 0
            and "NAMES_HIT" in lines else f"names screen failed rc={r.rc}"}


def _non_destructive(sb: SB.Sandbox) -> SB.Sandbox:
    """A view of `sb` for the optional save: its client timeouts raise without removing the
    container (the sandbox's `_guarded` closes it; astra agy-precompact r1 B2), and its `exec`
    runs a profile-free shell (`_exec_np`; r2 B3), so every process listing and command of the
    save (`quiesce`, `ChainEnv.checkpoint`'s own listing included) runs no agent-edited
    profile."""
    view = copy.copy(sb)  # shallow: the same log list and unresolved commits
    view.close = lambda *a, **k: None
    view.exec = lambda cmd, timeout_s=600, workdir="/app": _exec_np(
        sb, cmd, timeout_s=timeout_s, workdir=workdir)
    view.put_file = lambda data, path, timeout_s=120: _put_np(sb, data, path, timeout_s)
    return view


def _put_np(sb: SB.Sandbox, data: bytes, path: str, timeout_s: int = 120) -> None:
    """`Sandbox.put_file` through a profile-free shell, keeping the container on a timeout."""
    d, q = shlex.quote(str(Path(path).parent)), shlex.quote(path)
    argv = SB.docker_argv(["exec", "-i", "-w", "/", sb.name, "timeout", "-k", "5",
                           str(int(timeout_s)), "bash", "--noprofile", "--norc", "-c",
                           f"mkdir -p {d} && cat > {q}"], sb.host)
    try:
        p = subprocess.run(argv, input=data, capture_output=True,
                           timeout=timeout_s + SB.CLIENT_GRACE_S, check=False)
    except subprocess.TimeoutExpired:
        raise SB.SandboxError(f"client timeout on {sb.name} (container kept)") from None
    if p.returncode != 0:
        raise SB.SandboxError(f"put_file {path} rc={p.returncode}")


def _state(sb: SB.Sandbox) -> str | None:
    """"running", "paused" or another state; None when it can't be read."""
    try:
        p = subprocess.run(SB.docker_argv(["inspect", "-f",
                                           "{{.State.Running}} {{.State.Paused}}", sb.name],
                                          sb.host), capture_output=True, text=True,
                           timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0:
        return None
    running, paused = (p.stdout.split() + ["", ""])[:2]
    return "paused" if paused == "true" else "running" if running == "true" else "stopped"


def _unpaused(sb: SB.Sandbox, tries: int = 3) -> bool:
    """The container is running and not paused (a failed `docker unpause` in the checkpoint
    leaves it paused). Each try reads the state and, unless running, unpauses; a transport
    failure is one failed try, never the end of recovery (astra agy-precompact r2 B2). The
    state is read again after the last unpause."""
    for _ in range(tries):
        if _state(sb) == "running":
            return True
        with contextlib.suppress(Exception):
            subprocess.run(SB.docker_argv(["unpause", sb.name], sb.host), capture_output=True,
                           timeout=60, check=False)
    return _state(sb) == "running"


def pre_compact_checkpoint(sb: SB.Sandbox, env, run: TaskRun, hist_id: str, out: Path,
                           tok: str) -> dict:
    """Experiment 5's restore point: task 1's full state just before the boundary /compact,
    saved without any agent turn. Every container operation goes through a view that never
    removes the container on a timeout (`_non_destructive`), and the time it takes is given
    back to the task's deadline, so the boundary's own budget is unchanged (astra
    agy-precompact r1 B1–B2). In order:
    - verified quiescence (`quiesce`, recorded here, not in the task's quiescence log);
    - the steps that still run login shells: the conversation DB exported, screened
      (`export_session`; a state listing that failed its screen refuses the save), and a
      refreshed token store moved back to its volume (`keep_token_linked`, as `agy.capture`);
    - then only profile-free commands (the view's exec and put, `_exec_np`; r2 B3): quiescence
      again, the docker log stream read, the evidence's sources screened in the container
      (`prescreen_evidence`, its staging dir confirmed gone), contents (`N.scan_script`) and
      path names and link targets (`commit_names_screen`) of everything a commit keeps;
    - experiment 1's `ChainEnv.checkpoint`: under one `docker pause`, the container committed to
      a kept image (the workspace, agy's state dir and conversation DB; the auth volume is never
      in a commit), the remote copied out, the scorer's state in `out/pre_compact/traps_home`;
      the docker logs must be unchanged since they were read, so what was screened is what was
      exported (r3 B1), else the save is removed; then the container is confirmed unpaused;
    - the DB and its manifest in `out/pre_compact/session/`.
    Restoring needs a native wrapper (the auth and CLI volumes, the tmpfs config, the
    simulator and egress), not `start_from` alone. Never fatal and never an agent turn: a
    failure or refusal is recorded and the history goes on unchanged. {"ok", "tag", ...}."""
    t0 = time.monotonic()
    rec: dict = {"ok": False, "tag": None}  # filled as it goes: a failure keeps what was done
    res = rec
    try:
        env_sb = getattr(env, "sb", None)
        try:
            res = _save(rec, sb, env, run, hist_id, out, tok)
        except Exception as e:  # noqa: BLE001 - an extra save: recorded, the history goes on
            res = rec | {"error": f"{type(e).__name__}"}
        with contextlib.suppress(Exception):
            env.sb = env_sb
        try:
            res["unpaused"] = _unpaused(sb)
        except Exception as e:  # noqa: BLE001
            res["unpaused"] = f"unchecked: {type(e).__name__}"
    finally:  # the boundary keeps its own budget: every second here, recovery included (B1)
        run.deadline += time.monotonic() - t0
    return res


def _save(rec: dict, sb: SB.Sandbox, env, run: TaskRun, hist_id: str, out: Path,
          tok: str) -> dict:
    """`pre_compact_checkpoint`'s steps, recorded in `rec`; refusals add "skipped"."""
    nd = _non_destructive(sb)
    q = quiesce(nd, run.services)
    rec["quiescence"] = {k: q[k] for k in ("confirmed", "stopped", "survivors")}
    if not q["confirmed"]:
        return rec | {"skipped": "not quiescent"}
    # the steps that still run login shells (A.screen inside them) come first
    steps = task_steps(nd, run.cid, tok)
    blobs, man = export_session(nd, run.cid, tok, steps)
    if man.get("left_behind") == OMITTED:
        return rec | {"skipped": "state listing failed its screen"}
    rec["token_link"] = A.keep_token_linked(nd)
    # from here to the commit only profile-free commands run (r2 B3): quiescence again, the
    # docker log stream fixed (r3 B1: what is screened is what is exported), then the screens
    q = quiesce(nd, run.services)
    rec["final_quiescence"] = {k: q[k] for k in ("confirmed", "stopped", "survivors")}
    if not q["confirmed"] or q["stopped"]:
        return rec | {"skipped": "a process ran during the save's screens"}
    logs = container_log_bytes(sb)
    pre = prescreen_evidence(nd, env.bare, tok, run_env=_exec_np)
    rec["evidence_screen"] = pre
    if not pre["ok"]:
        return rec | {"skipped": "evidence screen not clean"}
    if nd.exec(f"test ! -e {STAGE_DIR}", timeout_s=30, workdir="/").rc != 0:
        return rec | {"skipped": "evidence staging dir left in the container"}
    sc = _exec_np(nd, N.scan_script(["EMX_SIM_TOKEN"], pattern_cmd=A.TOKEN_PATTERNS),
                  {"EMX_SIM_TOKEN": tok}, timeout_s=900)
    ok, n_scanned, hits = N.parse_scan(sc.stdout)
    if sc.rc != 0 or not ok:
        return rec | {"skipped": f"content scan failed rc={sc.rc}"}
    rec["secret_scan"] = {"scanned": n_scanned, "seconds": round(sc.seconds, 1)}
    if hits:
        return rec | {"skipped": "a secret is in the container's files"}
    names = commit_names_screen(nd, tok)
    rec["names_screen"] = names
    if not names["ok"]:
        return rec | {"skipped": "path names or link targets not clean"}
    dest = out / "pre_compact"
    tag = f"{PRECOMPACT_REPO}:pre-{hist_id}-{uuid.uuid4().hex[:6]}"
    rec["tag"] = tag
    env.sb = nd
    cp = env.checkpoint(tag, dest, {"emx.chain": "pre_compact", "emx.chain.id": hist_id,
                                    "emx.conversation": run.cid})
    if container_log_bytes(sb) != logs:  # append-only: unchanged after = unchanged in between
        shutil.rmtree(dest, ignore_errors=True)
        with contextlib.suppress(Exception):
            subprocess.run(SB.docker_argv(["rmi", tag], sb.host), capture_output=True,
                           timeout=120, check=False)
        return rec | {"skipped": "the container's logs changed after their screen"}
    sdir = dest / "session"
    for rel, data in blobs.items():
        (sdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (sdir / rel).write_bytes(data)
    (sdir / "manifest.json").write_text(json.dumps(man, indent=1))
    keep = ("tag", "commit_s", "seconds", "traps_home")  # not the unscreened process list
    return rec | {k: cp[k] for k in keep if k in cp} | {
        "ok": True, "conversation_id": run.cid, "session": man.get("files"),
        "last_step_index": man["last_step_index"]}


@dataclass
class HistoryConfig:
    hist: CC.History
    a_task: object  # tasks.TaskInfo
    b_task: object
    out: Path
    host: str | None = HOST
    memory_mb: int = N.AGENT_MEMORY_MB
    cpus: float = N.AGENT_CPUS
    min_free_gb: float | None = N.AGENT_MIN_FREE_GB
    # set at the first quota refusal; shared by every history of one bulk
    quota_stop: threading.Event = field(default_factory=threading.Event)
    # experiment 5's restore point: an extra save before the boundary /compact (no agent turn)
    pre_compact_checkpoint: bool = True
    # each task's caps (experiment 1's by default; the task-2 rerun raises them): model calls,
    # and seconds, which set the task's deadline and so every turn's `timeout` and
    # `--print-timeout` (the containers' 6 h lifetime must stay above it)
    max_calls: int = CC.MAX_CALLS
    max_seconds: int = CC.MAX_SECONDS


def _task(cfg: HistoryConfig, out: Path, task, branches, *, second: bool, prompt: str,
          cid: str | None = None, blobs: dict[str, bytes] | None = None, first_step: int = 0,
          a_steps: list[dict] | None = None, b_repo: CC.Repo | None = None,
          setup=None) -> dict:
    """One task, start to evidence; every step sits inside the path that writes result.json.
    After task 1: verified quiescence, the boundary (skipped after a quota refusal; every
    attempt waits on verified quiescence), quiescence again, then the session export; an
    unconfirmed stop ends the task "failed" with no session. The evidence is exported only
    after its sources pass the screen in the container (`prescreen_evidence`), then checked
    again (`screen_evidence`); else it is omitted. Diagnostics (exception text, quiescence
    records, turns, the boundary) are screened before the container goes; text that fails,
    or can't be screened, is reduced to fixed codes (`sanitize`). A quota interruption is kept
    as `quota_cut` and `primary_unknown_reason` whatever the final ending. Returns {"end",
    "cid", "session": (blobs, manifest) | None, "steps", "boundary", "checks", "quota_cut"}.
    `setup(sb) -> dict` (experiment 5's rules file, `agy_fix`) runs after the CLI config and
    before the agent's first turn; its record is `extra["arm_setup"]`, and a failure fails
    the task before the agent starts."""
    t0 = time.monotonic()
    rec: dict = {"end": END_FAILED}
    end, home, steps, run, extra = END_FAILED, None, None, None, {}
    quiet: list[dict] = []
    notes: list[dict] = []
    state = {"agent_started": False, "diag_clean": False}
    n = "2" if second else "1"

    def note(what: str, e: BaseException) -> None:
        # text from before the agent's first turn can't hold anything the agent wrote; later
        # text is kept only if the diagnostics screen passes
        notes.append({"what": what, "type": type(e).__name__, "msg": str(e)[:400],
                      "pre_agent": not state["agent_started"]})

    try:
        with _session(task.image, cfg) as ns:
            sb, tok = ns.sb, ns.server.token
            env, handoff = None, True
            try:
                env, inventory = X.install_chain(sb, task, out, branches)
                extra["test_inventory"] = inventory
                if second:
                    extra["b_setup"] = env.b_setup(b_repo)
                else:
                    extra["remote"] = cfg.hist.remote
                A.install_auth(sb)
                install_cli_config(sb, ns.server)
                if setup is not None:
                    extra["arm_setup"] = setup(sb)
                rec["checks"] = X.env_checks(sb)
                eg = N.egress_check(sb, ns.egress, EGRESS_URL)
                rec["checks"]["egress_ok"] = bool(eg.get("ok"))
                extra["egress_check"] = eg
                if not eg.get("ok"):
                    raise RuntimeError("egress check failed")
                if second:
                    for attempt in (1, 2):
                        try:
                            extra["session_import"] = import_session(sb, blobs or {})
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                    extra["state_before"] = state_listing(sb, tok)
                run = TaskRun(ns, out, deadline=t0 + cfg.max_seconds, first_step=first_step,
                              cid=cid, env=ns.egress.proxy_env(), services=ns.services,
                              quota_stop=cfg.quota_stop, quiet_log=quiet,
                              max_calls=cfg.max_calls)
                if (cfg.max_calls, cfg.max_seconds) != (CC.MAX_CALLS, CC.MAX_SECONDS):
                    # raised caps only (the defaults' result.json is unchanged): what the run got
                    extra["caps"] = {"max_calls": run.max_calls,
                                     "max_seconds": round(run.deadline - t0)}
                state["agent_started"] = True
                end = run.drive(prompt)
            except Exception as e:  # noqa: BLE001 - recorded; evidence still collected
                note("task", e)
                end = END_FAILED
            quiet_ok = run is None or run.quiet("task end")  # no run: the agent never started
            if not quiet_ok:
                end, handoff = END_FAILED, False  # no handoff from a live container
            if not second and run is not None and run.cid and handoff:
                if end == END_QUOTA or cfg.quota_stop.is_set():
                    rec["boundary"] = {"ok": False, "quota": True, "skipped": "quota stop"}
                else:
                    if cfg.pre_compact_checkpoint and env is not None:
                        extra["pre_compact"] = pre_compact_checkpoint(
                            sb, env, run, cfg.hist.id, out, tok)
                    try:
                        rec["boundary"] = boundary_compact(run)
                    except Exception as e:  # noqa: BLE001
                        note("boundary", e)
                        rec["boundary"] = {"ok": False, "deviation": "boundary error"}
                    if rec["boundary"].get("not_quiescent") or not run.quiet(
                            "after the boundary"):
                        end, handoff, quiet_ok = END_FAILED, False, False
                    if handoff and not rec["boundary"].get("quota"):
                        try:
                            steps = task_steps(sb, run.cid, tok)
                            rec["session"] = export_session(sb, run.cid, tok, steps)
                        except Exception as e:  # noqa: BLE001
                            note("session export", e)
            if env is not None and not quiet_ok:  # astra agy-build r3 B1: a live agent could
                extra["evidence_omitted"] = omit_evidence(out)  # still write; export nothing
                extra["evidence_screen"] = {"ok": False, "skipped": "not quiescent"}
            elif env is not None:
                pre = prescreen_evidence(sb, env.bare, tok)
                extra["evidence_screen"] = pre
                if pre["ok"]:
                    try:
                        home = env.finish_traps("final" if second else "a-final")
                    except Exception as e:  # noqa: BLE001
                        note("evidence", e)
                    post = screen_evidence(sb, out, tok)
                    extra["evidence_post_screen"] = post
                    if not post["ok"]:
                        extra["evidence_omitted"] = omit_evidence(out)
                        home = None
                else:  # nothing of the agent's was written on the host
                    extra["evidence_omitted"] = omit_evidence(out)
            if env is not None:  # an OOM kill must not read as agent behaviour (Jacob, 27 Oct)
                with contextlib.suppress(Exception):
                    extra["memory"] = E.parse_memory(_exec_np(
                        sb, "grep -h '^oom' /sys/fs/cgroup/memory.events 2>/dev/null; "
                        "echo peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null); "
                        "echo max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)",
                        timeout_s=60).stdout)
            if steps is None and run is not None and run.cid:
                try:
                    steps = task_steps(sb, run.cid, tok)
                except Exception as e:  # noqa: BLE001 - omitted, never stored unscreened
                    note("transcript", e)
            with contextlib.suppress(Exception):
                extra["asks"] = screened(sb, ns.server.export(), tok)
            if second:
                with contextlib.suppress(Exception):
                    extra["state_after"] = state_listing(sb, tok)
            diag = {"notes": notes, "quiescence": quiet, "boundary": rec.get("boundary"),
                    "turns": run.turns if run else [],
                    "evidence": {"pre": extra.get("evidence_screen"),
                                 "post": extra.get("evidence_post_screen")}}
            state["diag_clean"] = screen_blob(sb, json.dumps(diag, default=str).encode(), tok)
    except Exception as e:  # noqa: BLE001 - the session itself failed
        note("setup", e)
        end = END_FAILED
    finally:
        idx, merged = None, steps
        with contextlib.suppress(Exception):
            if steps is not None:
                (out / "transcript_full.jsonl").write_text(
                    "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in steps))
                if second:
                    merged, extra["merge"] = AT.merge_steps(a_steps or [], steps)
                msgs, _ = AT.score_messages(merged)
                idx = AT.task2_index(msgs, prompt) if second else 0
                if second and idx is not None:
                    first_b = AT.calls(merged, first_step)
                    extra["ctx_tokens_at_start"] = AT.ctx_tokens(first_b[0]) if first_b \
                        else None
        cut = (run.quota_cut if run else None) or ("refused" if end == END_QUOTA else None)
        if cut:  # astra agy-build r2 #4: kept whatever the final ending
            extra |= {"quota_cut": True, "primary_unknown_reason":
                      f"quota refused in task {n}" if cut == "refused" else
                      f"quota stop (shared flag) in task {n}"}
        bnd = rec.get("boundary") or {}
        if bnd.get("quota") and not cut:  # task 1 complete; the history stops here
            extra["history_quota_cut"] = bnd.get("deviation") or "quota stop before the boundary"
        clean = state["diag_clean"]
        err = " | ".join(
            f"{x['what']}: {x['type']}" + (f": {x['msg']}" if clean or x["pre_agent"] else "")
            for x in notes) or None
        diag = {"quiescence": quiet, "boundary": rec.get("boundary"),
                "turns": run.turns if run else [],
                "evidence": {"pre": extra.get("evidence_screen"),
                             "post": extra.get("evidence_post_screen")}}
        if not clean:
            diag = sanitize(diag) | {"diagnostics": OMITTED}
        extra |= {"turns": diag["turns"], "conversation_id": run.cid if run else cid,
                  "quiescence": diag["quiescence"], "checks": rec.get("checks"),
                  "boundary": diag["boundary"],
                  "session_manifest": (rec.get("session") or (None, None))[1]}
        if not clean:
            extra |= {"diagnostics": OMITTED,
                      "evidence_screen": diag["evidence_screen"].get("pre"),
                      "evidence_post_screen": diag["evidence_screen"].get("post")}
        write_run(out, end=end, t0=t0, steps=merged, first_step=first_step, start_index=idx,
                  home=home, extra=extra, error=err, second=second)
        rec |= {"end": end, "cid": run.cid if run else cid, "steps": steps,
                "quota_cut": extra.get("primary_unknown_reason")
                or extra.get("history_quota_cut")}
    return rec


def run_history(cfg: HistoryConfig) -> dict:
    """Both tasks of one history (module docstring). Returns {"a", "b" (dirs), "boundary",
    ...}; each task's dir holds result.json whatever happened. The shared quota flag is read
    before task 1 and before task 2: a history it stops keeps its slot with the ending
    "quota_refused" and a `primary_unknown_reason` (never rerun or replaced)."""
    hist, a_repo = cfg.hist, CC.REPOS[cfg.hist.repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != hist.repo)]
    a_dir, b_dir = cfg.out / "a", cfg.out / "b"
    for d in (a_dir, b_dir):
        if d.exists() and any(d.iterdir()):
            raise FileExistsError(f"{d} is not empty: use a new attempt directory")
        d.mkdir(parents=True, exist_ok=True)
    out = {"a": str(a_dir), "b": str(b_dir)}
    if cfg.quota_stop.is_set():
        why = "quota stop before task 1"
        skipped_run(a_dir, end=END_QUOTA, why=why, second=False, primary_unknown=why)
        skipped_run(b_dir, end=END_QUOTA, why=why, second=True, primary_unknown=why)
        return out | {"a_end": END_QUOTA, "b_end": END_QUOTA}
    try:
        A.ensure_cli_volume(cfg.host, cfg.a_task.image)
    except Exception as e:  # noqa: BLE001 - astra agy-build r1 #7: both tasks get a result
        why = f"setup: agy CLI volume: {type(e).__name__}: {e}"[:400]
        skipped_run(a_dir, end=END_FAILED, why=why, second=False)
        skipped_run(b_dir, end=END_FAILED, why=why, second=True)
        return out | {"a_end": END_FAILED, "b_end": END_FAILED}
    a = _task(cfg, a_dir, cfg.a_task, X.first_branches(hist), second=False,
              prompt=X.task_prompt(cfg.a_task.instruction, a_repo.a_branch))
    out |= {"conversation_id": a.get("cid"), "a_end": a["end"], "boundary": a.get("boundary")}
    # task 1's own quota mark (kept through a later failure), the boundary's, or another
    # history's refusal since: task 2 is not launched, and its primary outcome is unknown
    if a.get("quota_cut") or cfg.quota_stop.is_set():
        why = a.get("quota_cut") or "quota stop before task 2"
        skipped_run(b_dir, end=END_QUOTA, why=why, second=True, primary_unknown=why)
        return out | {"b_end": END_QUOTA}
    if not a.get("session"):
        skipped_run(b_dir, end=END_FAILED, why="the first task left no session to carry over",
                    second=True)
        return out | {"b_end": END_FAILED}
    blobs, man = a["session"]
    b = _task(cfg, b_dir, cfg.b_task, [], second=True,
              prompt=X.lead_and_task(b_repo, a_repo, cfg.b_task.instruction), cid=a["cid"],
              blobs=blobs, first_step=man["last_step_index"] + 1, a_steps=a.get("steps"),
              b_repo=b_repo)
    return out | {"b_end": b["end"]}
