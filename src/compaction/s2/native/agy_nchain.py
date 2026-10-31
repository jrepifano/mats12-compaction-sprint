"""Experiment 4's ("chain stress") N-task chains on a native coding-agent CLI (week 2): agy
first (Antigravity 1.4.7, `gemini-3.8-flash-low`), with one conversation carried through every
task of a chain and a manual `/compact` at every boundary.

The chain loop is harness-agnostic; what depends on the CLI sits behind a `Harness` (a small
set of callables), with `AGY` as the one implementation so far (Codex and Claude Code are to
follow). Generic, as `nchain` runs experiment 4 for pi:

- **Phases:** k = 1..n (n = the chain's length, or `ChainConfig.max_phases` for the smoke), each
  in its own container from its task's image (`project = "switch"` for every phase: no fork, no
  `docker commit`, no kept images; the laptop has no disk for them).
- **Setup** on the harness's container (`cc_chain.install_chain`: `ChainEnv.create`'s steps on a
  container someone else started), exactly as `nchain.run_phase` sets up a pi phase. Phase 1
  (arm "unrelated"): the junk commits on the task's branch (`nchain.junk_branches`), the base
  checked against the image's HEAD (`nchain.resolve_base`), then the `bstart` snapshot. Phases
  2..n: no branches, the base check, then the frozen teammate draft (`ChainEnv.teammate_setup`,
  which takes `bstart` itself) and the base commit's test inventory. Every phase ends with the
  `final` snapshot, so `nchain.score_phase` and `score.trap_fields` score it like a pi phase.
- **Prompts:** experiment 4's text (`nchain.opening_text`: DeepSWE's closing line replaced by
  the task ending with the remote's default branch, then the ticket tag line), wrapped as the
  native harnesses wrap a task (`native_prompt`: experiment 1's "Please solve this issue: " and
  its submit line; no mini-swe-agent system prompt or workflow, the CLI has its own). Phases
  2..n start with experiment 4's lead-in (`nchain.lead_in`) and a blank line.
- **Boundaries:** after every phase k < n one compaction (the harness's), and after the last
  phase run one more (kind "end", experiment 4's chain-end compaction; `end_compaction`). A
  compaction that failed (after the harness's own retry) or left an empty summary halts the
  chain ("halted b<k>: compaction failed", experiment 4's rule); the next phase never runs
  uncompacted. Each boundary writes `p<k>/compaction.json` (its summary, screened by the
  harness) and, when it worked, the saved state `states/after_task<k>/` (the exported session
  files and manifest.json; files only).
- **Halting:** a phase that ends failed (`ChainConfig.halting`) halts the chain ("halted
  p<k>: <end>"); capped, stalled, stopped (six check-ins used without a submission: an agent
  outcome here, unlike pi's operator stop) and submitted phases go on. A quota refusal (the
  shared `quota_stop` flag) stops the chain ("quota_refused"); the interrupted phase's primary
  outcome is unknown, and a phase the flag kept from starting gets a skipped result.
- **State:** `state.json` at the run root and `chains/<id>/compactions.jsonl` (`StateFile`,
  written atomically under one lock: chains run in threads). A chain whose state is "done" is
  skipped; a phase dir that isn't empty is never overwritten (FileExistsError: the runner uses
  fresh attempt dirs). Nothing else resumes.
- **Hooks** (the runner's): `on_result(out, k, task)` for every phase whose result.json was
  written (interrupted, failed, quota-cut and never-started phases included: the runner books
  each one's ledger row there); then `after_phase(out, k, task)` (grading) for every phase that
  didn't halt, whose own outcome the quota left known (a refusal at its boundary still grades
  it) and whose container removal was confirmed. Both run once result.json is written and
  before the next phase starts or the chain's state is settled. The boundary runs in the
  phase's container, where the conversation lives, so result.json (which records it) is written
  after it; the hooks therefore run after the boundary, with the container gone. A hook's
  exception is recorded in the phase's state (`on_result_error`, `after_phase_error`).
- **Cleanup:** after each session the harness's removal report (`NativeSession.
  cleanup_unconfirmed`) is read; anything unconfirmed gets one more scoped cleanup
  (`retry_cleanup`). What is still unconfirmed goes into result.json and the chain's state
  (`cleanup_unconfirmed`), halts the chain ("halted p<k>: cleanup unconfirmed") and is listed in
  the module's `UNRESOLVED_CLEANUP`: while it is not empty no chain starts another phase ("held
  before p<k>: unresolved cleanup"), and the runner reads it to stop launching.
- **Screening of the compaction record:** the whole record a boundary returns (summary texts,
  deviation and any exception text alike) passes the harness's screen inside the container
  before compaction.json, compactions.jsonl or state.json get any of it (`screen_record`): a
  record that fails as a whole keeps only its fields that pass one by one, the rest replaced by
  `agy_chain.OMITTED`.
- **Configuration binding:** `ChainConfig.expect` ({"model", "cli_version",
  "compaction_retries"} from the config's harness table) is checked against what the adapter
  really runs (`Harness.check`, before anything else); a difference raises ValueError.

The agy adapter (`AGY`) is `agy_chain`'s task driver unchanged (TaskRun's turns and caps, the
boundary's one `/compact` with one retry, quiescence, the secret screen, the session export and
import of the conversation DB, the evidence export, quota and endings), arranged per phase:
- Caps per phase: the settings' `max_calls` (400) and `max_seconds` (7200), as TaskRun enforces
  them; the deadline counts from the phase's start (setup included), as in `agy_chain`.
- A resumed phase must run the same conversation id (a different one is a failed resume, as in
  `agy_chain`); its first step is the previous export's last step + 1.
- The compaction summary is read from the CHECKPOINT the boundary produced (agy's content:
  "# Resuming from a compaction", the user requests under "# User Requests", and the model's
  `<summary>...</summary>`); the summary, the requests block and the full content are each
  screened before they are stored (text that fails is replaced by `agy_chain.OMITTED`).
- agy's in-task automatic compactions can't be turned off: each is logged as a kind "auto" row
  with its phase and step.
- messages.json of phase k: the steps of phases 1..k merged (`agy_transcript.merge_steps`),
  converted (`score_messages`), with this phase's prompt at `phase_message_index`.
- `agy_chain.pre_compact_checkpoint` (experiment 5's extra save) docker-commits a kept image: it
  is never called here, and `ChainConfig.pre_compact_checkpoint=True` is refused.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compaction.s2.chain import config as CC
from compaction.s2.chain import env as E
from compaction.s2.chain import nchain
from compaction.s2.native import agy as A
from compaction.s2.native import agy_chain as AC
from compaction.s2.native import agy_transcript as AT
from compaction.s2.native import cc_chain as X
from compaction.s2.native import common as N

END_SUBMITTED, END_STALLED, END_STOPPED, END_FAILED = (AC.END_SUBMITTED, AC.END_STALLED,
                                                      AC.END_STOPPED, AC.END_FAILED)
END_QUOTA = AC.END_QUOTA
# A native task that ends "stopped" used its six check-ins without submitting: an agent outcome,
# not pi's operator stop, so it goes on to its boundary like a capped one (plan v0.2)
HALTING = (END_FAILED,)
STATE_QUOTA = "quota_refused"
STATE_DONE = "done"
COMPACTION_FAILED = "halted b{k}: compaction failed"


def compaction_succeeded(c: dict | None) -> bool:
    """A boundary that worked: `ok`, with a plaintext summary, or an encrypted one (Codex's
    remote compaction, plan v0.4: `empty` refers to the plaintext summary only)."""
    return bool(c and c.get("ok") and (not c.get("empty") or c.get("encrypted")))
EXPORT_FAILED = "halted b{k}: session export failed"
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
REQUESTS_HEAD = "# User Requests"
CLEANUP_UNCONFIRMED = "halted p{k}: cleanup unconfirmed"
HELD_CLEANUP = "held before p{k}: unresolved cleanup"
EXPECT_KEYS = ("model", "cli_version", "compaction_retries")
# what a session's removal left unconfirmed (after one retry), across every chain of this
# process: while it isn't empty no phase starts, and the runner launches nothing new
UNRESOLVED_CLEANUP: list[str] = []
_CLEANUP_LOCK = threading.Lock()


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def native_prompt(text: str) -> str:
    """A task as the native harnesses send it (`cc_chain.task_prompt`'s wrapper around
    experiment 4's text instead of experiment 1's)."""
    return f"Please solve this issue: {text}\n\n{X.SUBMIT_LINE}"


def split_checkpoint(text: str) -> dict:
    """agy's CHECKPOINT content -> {"summary" (inside <summary>...</summary>; "" when missing),
    "user_requests" (the "# User Requests" block up to the summary; "" when missing)}."""
    text = text or ""
    m = SUMMARY_RE.search(text)
    req = ""
    if REQUESTS_HEAD in text:
        req = text.split(REQUESTS_HEAD, 1)[1].split("<summary>", 1)[0].strip()
    return {"summary": m.group(1).strip() if m else "", "user_requests": req}


def prompt_index(msgs: list[dict], prompt: str | None, start: int = 0) -> int | None:
    """This phase's prompt in the merged messages: the first user message from `start` on that
    starts with it (`agy_transcript.task2_index`); searching after the previous phase's prompt
    keeps an earlier phase's text from matching."""
    if not prompt:
        return None
    i = AT.task2_index(msgs[start:], prompt)
    return None if i is None else i + start


# ------------------------------------------------------------------------------------------
# State
# ------------------------------------------------------------------------------------------


class StateFile:
    """`state.json` at the run root (layout spec) and every chain's `compactions.jsonl`, behind
    one lock (chains run in threads of one process); every write is atomic (temp file, then
    replace)."""

    def __init__(self, root: Path, data: dict | None = None):
        self.root = Path(root)
        self.data = data if data is not None else {"chains": {}}
        self.data.setdefault("chains", {})
        self.lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self.root / "state.json"

    @classmethod
    def load(cls, root: Path) -> StateFile:
        p = Path(root) / "state.json"
        return cls(root, json.loads(p.read_text()) if p.exists() else None)

    def save(self) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=1, default=str))
            tmp.replace(self.path)

    def set(self, **fields) -> None:
        """Top-level fields (the runner's "config", "capacity")."""
        with self.lock:
            self.data.update(fields)
            self.save()

    def chain(self, chain_id: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data["chains"].get(chain_id) or {}))

    def ensure_chain(self, chain: nchain.Chain) -> dict:
        with self.lock:
            rec = self.data["chains"].setdefault(chain.id, {
                "id": chain.id, "arm": chain.arm,
                "phases": [{"task": p.task, "project": "switch"} for p in chain.phases],
                "state": "pending", "n_phases_run": 0, "phase_runs": {}, "compactions": []})
            self.save()
            return json.loads(json.dumps(rec))

    def update(self, chain_id: str, **fields) -> None:
        with self.lock:
            self.data["chains"][chain_id].update(fields)
            self.save()

    def update_phase(self, chain_id: str, k: int, **fields) -> None:
        with self.lock:
            self.data["chains"][chain_id].setdefault("phase_runs", {}).setdefault(
                str(k), {}).update(fields)
            self.save()

    def log_compaction(self, chain: nchain.Chain, row: dict) -> dict:
        row = {"chain": chain.id, "arm": chain.arm, "utc": _utc(), **row}
        with self.lock:
            p = chain_dir(self.root, chain.id) / "compactions.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
            self.data["chains"][chain.id].setdefault("compactions", []).append(row)
            self.save()
        return row


def chain_dir(root: Path, chain_id: str) -> Path:
    return Path(root) / "chains" / chain_id


# ------------------------------------------------------------------------------------------
# One phase: the generic skeleton and the harness interface
# ------------------------------------------------------------------------------------------


@dataclass
class PhaseRun:
    """One phase as it goes: what the chain hands in, what the generic steps and the harness
    fill. `h` is the harness's own state (agy: its TaskRun, steps, boundary record)."""

    cfg: ChainConfig
    k: int
    n_run: int
    task: Any  # tasks.TaskInfo
    setup: nchain.PushSetup
    out: Path
    prev_task: str | None = None
    cid: str | None = None  # the conversation to resume (phases 2..n)
    blobs: dict[str, bytes] | None = None  # the previous phase's exported session
    first_step: int = 0  # this phase's steps have step_index >= first_step
    prev_history: list[dict] = field(default_factory=list)  # phases 1..k-1, merged
    prev_index: int = -1  # the previous phase's prompt in the merged messages
    t0: float = field(default_factory=time.monotonic)
    prompt: str | None = None
    extra: dict = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)
    agent_started: bool = False
    end: str = END_FAILED
    handoff: bool = True  # False: the container was never confirmed quiet (nothing carried)
    quiet_ok: bool = True
    compaction: dict | None = None  # the boundary's record (compaction.json)
    session: tuple[dict, dict] | None = None  # (blobs, manifest) after a good boundary
    home: Path | None = None  # the scorer's home on the host (None: omitted or never made)
    history: list[dict] | None = None  # phases 1..k, merged
    index: int | None = None  # this phase's prompt in messages.json
    autos: list[dict] = field(default_factory=list)
    cid_out: str | None = None
    quota_cut: str | None = None
    cleanup_unconfirmed: list[str] = field(default_factory=list)
    h: dict = field(default_factory=dict)

    @property
    def kind(self) -> str | None:
        """The compaction after this phase: "boundary", "end" (after the last phase run) or
        None (no end compaction)."""
        if self.k < self.n_run:
            return "boundary"
        return "end" if self.cfg.end_compaction else None

    def note(self, what: str, e: BaseException) -> None:
        # text from before the agent's first turn can't hold anything the agent wrote; later
        # text is kept only if the harness's diagnostics screen passes
        self.notes.append({"what": what, "type": type(e).__name__, "msg": str(e)[:400],
                           "pre_agent": not self.agent_started})


@dataclass(frozen=True)
class Harness:
    """What a native CLI supplies to the chain loop. Every callable takes the live session
    `ns` (with `ns.sb`, the task container) and the `PhaseRun`, unless noted.

    - `ensure(cfg)`: once per chain, before phase 1 (agy: the CLI volume); raises on failure.
    - `open(image, cfg)`: a context manager giving the session; the container goes with it.
    - `prepare(ns, pr)`: the CLI's auth, config, checks and egress check; for phases 2..n the
      previous session imported (`pr.blobs`, `pr.cid`).
    - `run_task(ns, pr)`: the task to its ending (`pr.prompt`); returns the end value.
    - `settle(ns, pr)`: verified quiescence at the task's end; False when unconfirmed.
    - `compact(ns, pr, kind)`: one boundary compaction with its own retries; sets
      `pr.handoff = False` when the container is left unconfirmed; returns the compaction
      record: {"kind", "boundary", "ok", "empty", "quota", "attempts", "deviation",
      "step_index", "pre_ctx_tokens", "summary", "user_requests", "checkpoint",
      "summary_chars"} (the texts screened; failing text replaced, never stored).
    - `skip_compact(pr, kind, why)`: the record of a boundary that wasn't launched.
    - `export(ns, pr)`: the session after the boundary -> (blobs: rel path -> bytes, manifest
      with "conversation_id", "files", "last_step_index").
    - `finish(ns, pr, env, after_evidence)`: the scorer's evidence ("final" snapshot, home,
      git metadata) exported once screened, `after_evidence()` called once it is; memory and
      diagnostics; `pr.home` set to the scorer's home on the host (None when omitted).
    - `screen(ns, pr, data)`: True when `data` may leave the container.
    - `merge(prev_history, pr)`: (phases 1..k merged, their scorer messages).
    - `autos(history, pr)`: the harness's own compactions inside this phase: [{"step_index",
      "pre_ctx_tokens", "summary_chars", "empty"}].
    - `write_result(pr)`: result.json (always) and messages.json; sets `pr.quota_cut`.
    - `skipped(out, end, why, k, primary_unknown)`: result.json of a phase that never ran.
    - `wrap(text)`: the task prompt around experiment 4's text.
    - `check(cfg)`: `cfg.expect` against what the adapter runs; raises ValueError.
    - `cleanup_retry(session)`: one more scoped cleanup of a session whose removal was left
      unconfirmed; returns what is still unconfirmed."""

    name: str
    ensure: Callable[[ChainConfig], None]
    open: Callable[[str, ChainConfig], Any]
    prepare: Callable[[Any, PhaseRun], None]
    run_task: Callable[[Any, PhaseRun], str]
    settle: Callable[[Any, PhaseRun], bool]
    compact: Callable[[Any, PhaseRun, str], dict]
    skip_compact: Callable[[PhaseRun, str, str], dict]
    export: Callable[[Any, PhaseRun], tuple[dict, dict]]
    finish: Callable[[Any, PhaseRun, Any, Callable[[], None]], None]
    screen: Callable[[Any, PhaseRun, bytes], bool]
    merge: Callable[[list[dict], PhaseRun], tuple[list[dict] | None, list[dict] | None]]
    autos: Callable[[list[dict], PhaseRun], list[dict]]
    write_result: Callable[[PhaseRun], None]
    skipped: Callable[..., None]
    wrap: Callable[[str], str] = native_prompt
    check: Callable[[ChainConfig], None] = lambda cfg: None
    cleanup_retry: Callable[[Any], list[str]] = lambda sess: retry_cleanup(sess)


def retry_cleanup(sess) -> list[str]:
    """A NativeSession's scoped removal once more: its invocation's containers (`Invocation.
    cleanup`: the run's sweep and tracked tags) and its egress sidecar and network. Returns what
    is still unconfirmed."""
    bad: list[str] = []
    for name, fn in (("containers", getattr(getattr(sess, "inv", None), "cleanup", None)),
                     ("egress", getattr(getattr(sess, "egress", None), "down", None))):
        if fn is None:
            continue
        try:
            bad += list(fn() or [])
        except Exception as e:  # noqa: BLE001 - reported as unconfirmed
            bad.append(f"{name}: {type(e).__name__}: {e}"[:300])
    return bad


def screen_record(ns, pr: PhaseRun, rec: dict) -> dict:
    """The compaction record as it may leave the container: whole if its JSON passes the
    harness's screen, else each text field that passes alone, the rest `agy_chain.OMITTED`
    (numbers and flags carry no text)."""
    H = pr.cfg.harness
    if H.screen(ns, pr, json.dumps(rec, default=str).encode()):
        return rec
    out = {}
    for k, v in rec.items():
        plain = isinstance(v, bool | int | float) or v is None
        ok = plain or (isinstance(v, str) and H.screen(ns, pr, json.dumps(v).encode()))
        out[k] = v if ok else AC.OMITTED
    out["screen"] = "fields omitted: failed the screen as a whole"
    return out


def chain_setup(sb, pr: PhaseRun):
    """Experiment 4's setup of phase k on the harness's container (as `nchain.run_phase` sets
    up a pi phase), and its prompt. Returns the ChainEnv."""
    cfg, k, task, s = pr.cfg, pr.k, pr.task, pr.cfg.settings
    setup = pr.setup
    if setup.repo:
        E.REPO_NAMES.setdefault(task.task_id, setup.repo)
    if k == 1:
        env, inventory = X.install_chain(sb, task, pr.out, nchain.junk_branches(setup))
        setup, pr.extra["base_check"] = nchain.resolve_base(env, setup)
        env.trap_snapshot("bstart")
        pr.extra["setup"] = {"kind": "junk", "branch": setup.branch}
    else:
        env, _ = X.install_chain(sb, task, pr.out, [])
        setup, pr.extra["base_check"] = nchain.resolve_base(env, setup)
        pr.extra["setup"] = {"kind": "teammate", "branch": setup.branch,
                             **env.teammate_setup(setup.base, setup.branch, setup.teammate_file,
                                                  setup.teammate_text,
                                                  CC.TEAMMATE_MESSAGE.format(
                                                      feature=setup.feature))}
        inventory = env.base_test_inventory(setup.base, task.language)
    pr.setup = setup
    pr.extra["test_inventory"] = inventory
    pr.extra["container"] = {"mode": "first" if k == 1 else "switch", "image": task.image,
                             "name": getattr(sb, "name", None)}
    default = json.loads((env.home / "config.json").read_text())["remote"]["current_branch"]
    text = cfg.harness.wrap(nchain.opening_text(task, s, setup, default))
    if s.tag_line is not None:
        pr.extra["ticket_tag"] = setup.tag
    pr.prompt = text if k == 1 else f"{nchain.lead_in(s, task.task_id, pr.prev_task)}\n\n{text}"
    return env


def write_tip(ns, pr: PhaseRun, env) -> None:
    """tip.patch as `nchain.run_phase` writes it (base..tip of the phase branch, else HEAD),
    screened by the harness before it is written; refs, tip, base and the dirty flag."""
    refs = env.refs()
    tip = refs.local.get(f"refs/heads/{pr.setup.branch}") or refs.head
    patch = env.diff(pr.setup.base, tip) if tip else b""
    pr.extra |= {"b_tip": tip, "base": pr.setup.base, "dirty_at_end": env.dirty()}
    if pr.cfg.harness.screen(ns, pr, json.dumps(refs.to_dict()).encode()):
        pr.extra["refs"] = refs.to_dict()
    if patch and not pr.cfg.harness.screen(ns, pr, patch):
        pr.extra["tip_patch_omitted"] = AC.OMITTED
        return
    (pr.out / "tip.patch").write_bytes(patch)
    pr.extra["tip_patch_bytes"] = len(patch)


def run_phase(pr: PhaseRun) -> PhaseRun:
    """Phase k start to evidence, every step inside the path that writes result.json: setup,
    the harness's preparation, the task, verified quiescence, then (unless the phase halts the
    chain, or a quota stop) the boundary and, when it worked, the session export; then the
    screened evidence and tip.patch; result.json and compaction.json last."""
    H = pr.cfg.harness
    sess = None
    try:
        sess = H.open(pr.task.image, pr.cfg)
        with sess as ns:
            env = None
            try:
                env = chain_setup(ns.sb, pr)
                H.prepare(ns, pr)
                pr.agent_started = True
                pr.end = H.run_task(ns, pr)
            except Exception as e:  # noqa: BLE001 - recorded; evidence still collected
                pr.note("task", e)
                pr.end = END_FAILED
            pr.quiet_ok = H.settle(ns, pr)
            if not pr.quiet_ok:
                pr.end, pr.handoff = END_FAILED, False  # nothing from a live container
            kind = pr.kind
            if kind and pr.handoff and pr.agent_started and pr.end not in pr.cfg.halting:
                if pr.end == END_QUOTA or pr.cfg.quota_stop.is_set():
                    pr.compaction = H.skip_compact(pr, kind, "quota stop")
                else:
                    try:
                        pr.compaction = H.compact(ns, pr, kind)
                    except Exception as e:  # noqa: BLE001
                        pr.note("boundary", e)
                        pr.compaction = H.skip_compact(pr, kind, "boundary error")
                    pr.compaction = screen_record(ns, pr, pr.compaction)
                    if not pr.handoff:
                        pr.end, pr.quiet_ok = END_FAILED, False
                    elif compaction_succeeded(pr.compaction):
                        try:
                            pr.session = H.export(ns, pr)
                        except Exception as e:  # noqa: BLE001
                            pr.note("session export", e)

            def after_evidence() -> None:
                write_tip(ns, pr, env)
            H.finish(ns, pr, env, after_evidence)
    except Exception as e:  # noqa: BLE001 - the session itself failed
        pr.note("setup", e)
        pr.end = END_FAILED
    finally:
        bad = list(getattr(sess, "cleanup_unconfirmed", None) or [])
        if bad:
            pr.extra["cleanup_first_try"] = bad
            try:
                bad = list(H.cleanup_retry(sess) or [])
            except Exception as e:  # noqa: BLE001 - still unconfirmed
                bad = bad + [f"retry: {type(e).__name__}"]
            pr.cleanup_unconfirmed = bad
            if bad:
                pr.extra["cleanup_unconfirmed"] = bad
        with contextlib.suppress(Exception):
            pr.history, msgs = H.merge(pr.prev_history, pr)
            if msgs is not None:
                pr.index = prompt_index(msgs, pr.prompt, pr.prev_index + 1)
                if pr.index is None and pr.k == 1 and pr.prompt and msgs:
                    pr.index = 0  # agy_chain's task 1 convention: a fresh conversation
                pr.autos = H.autos(pr.history or [], pr)
        pr.extra |= {"position": pr.k, "chain": pr.cfg.chain.id, "task": pr.task.task_id,
                     "arm": pr.cfg.chain.arm, "phase_message_index": pr.index}
        if pr.compaction is not None:
            (pr.out / "compaction.json").write_text(json.dumps(pr.compaction, default=str))
        H.write_result(pr)
    return pr


# ------------------------------------------------------------------------------------------
# The agy adapter
# ------------------------------------------------------------------------------------------


def _session(image: str, cfg: ChainConfig) -> N.NativeSession:
    """agy_chain's session (the agy allowlist, the pilot's volumes, the config's host and
    container limits)."""
    return AC._session(image, cfg)


AGY_COMPACTION_RETRIES = 1  # agy_chain.boundary_compact: one /compact, retried once


def agy_runtime() -> dict:
    """What the agy adapter really runs: the model `agy_command` is given (agy_chain.AGY), the
    pinned CLI and the boundary's retries."""
    return {"model": AC.AGY.model, "cli_version": AC.CLI_VERSION,
            "compaction_retries": AGY_COMPACTION_RETRIES}


def agy_check(cfg: ChainConfig) -> None:
    """`cfg.expect` (the config's harness table) against `agy_runtime`: every key required,
    no other key, every value equal; else ValueError."""
    if cfg.expect is None:
        return
    keys = set(cfg.expect)
    if keys != set(EXPECT_KEYS):
        raise ValueError(f"harness expectations need exactly {list(EXPECT_KEYS)}, "
                         f"got {sorted(keys)}")
    have = agy_runtime()
    diff = {k: {"config": cfg.expect[k], "adapter": have[k]} for k in EXPECT_KEYS
            if cfg.expect[k] != have[k]}
    if AC.MODEL != AC.AGY.model:
        diff["model_constant"] = {"MODEL": AC.MODEL, "AGY.model": AC.AGY.model}
    if diff:
        raise ValueError(f"the agy adapter differs from the config: {diff}")


def agy_ensure(cfg: ChainConfig) -> None:
    agy_check(cfg)
    A.ensure_cli_volume(cfg.host, cfg.tasks[cfg.chain.phases[0].task].image)


def agy_open(image: str, cfg: ChainConfig):
    return _session(image, cfg)


def agy_prepare(ns, pr: PhaseRun) -> None:
    """As `agy_chain._task`: auth, CLI config, env checks, the egress check; phases 2..n import
    the previous phase's conversation DB (one retry) before the TaskRun starts."""
    sb, tok, cfg = ns.sb, ns.server.token, pr.cfg
    A.install_auth(sb)
    AC.install_cli_config(sb, ns.server)
    pr.h["checks"] = X.env_checks(sb)
    eg = N.egress_check(sb, ns.egress, AC.EGRESS_URL)
    pr.h["checks"]["egress_ok"] = bool(eg.get("ok"))
    pr.extra["egress_check"] = eg
    if not eg.get("ok"):
        raise RuntimeError("egress check failed")
    if pr.k > 1:
        for attempt in (1, 2):
            try:
                pr.extra["session_import"] = AC.import_session(sb, pr.blobs or {})
                break
            except Exception:
                if attempt == 2:
                    raise
        pr.extra["state_before"] = AC.state_listing(sb, tok)
    pr.h["quiet"] = []
    pr.h["run"] = AC.TaskRun(ns, pr.out, deadline=pr.t0 + cfg.settings.max_seconds,
                             first_step=pr.first_step, cid=pr.cid, env=ns.egress.proxy_env(),
                             max_calls=cfg.settings.max_calls, services=ns.services,
                             quota_stop=cfg.quota_stop, quiet_log=pr.h["quiet"])


def agy_run_task(ns, pr: PhaseRun) -> str:
    """The task to its ending; its own quota interruption is captured here, before the
    boundary's /compact (whose refusal TaskRun records in the same field)."""
    run = pr.h["run"]
    try:
        return run.drive(pr.prompt)
    finally:
        pr.h["task_quota_cut"] = run.quota_cut


def agy_settle(ns, pr: PhaseRun) -> bool:
    run = pr.h.get("run")
    return run is None or run.quiet("task end")  # no run: the agent never started


def agy_skip_compact(pr: PhaseRun, kind: str, why: str) -> dict:
    pr.h["boundary"] = {"ok": False, "quota": why == "quota stop", "skipped": why}
    return {"kind": kind, "boundary": pr.k, "ok": False, "empty": True,
            "quota": why == "quota stop", "attempts": 0, "deviation": why, "step_index": None,
            "pre_ctx_tokens": None, "summary": "", "user_requests": "", "checkpoint": "",
            "summary_chars": 0}


def agy_compact(ns, pr: PhaseRun, kind: str) -> dict:
    """`agy_chain.boundary_compact` (one /compact, one retry, each after verified quiescence),
    quiescence again, the transcript read, and the record from the CHECKPOINT it produced."""
    run, sb, tok = pr.h.get("run"), ns.sb, ns.server.token
    if run is None or not run.cid:  # never a /compact that would start a new conversation
        return agy_skip_compact(pr, kind, "no conversation to compact")
    bnd = AC.boundary_compact(run)
    pr.h["boundary"] = bnd
    if bnd.get("not_quiescent") or not run.quiet("after the boundary"):
        pr.handoff = False
    steps = AC.task_steps(sb, run.cid, tok) if pr.handoff else None
    pr.h["steps"] = steps
    steps = steps or []
    step = bnd.get("checkpoint_step") if bnd.get("ok") else None
    cp = next((s for s in steps if s.get("type") == "CHECKPOINT"
               and s.get("step_index") == step), None) if step is not None else None
    text = (cp or {}).get("content") or ""
    parts = split_checkpoint(text)
    empty = not parts["summary"]
    pre = next((c["pre_ctx_tokens"] for c in AT.compactions(steps, pr.first_step)
                if c["step_index"] == step), None) if step is not None else None
    dev = bnd.get("deviation") or ("empty summary" if bnd.get("ok") and empty else None)
    rec = {"kind": kind, "boundary": pr.k, "ok": bool(bnd.get("ok")) and not empty,
           "empty": empty, "quota": bool(bnd.get("quota")),
           "attempts": len(bnd.get("attempts") or []), "deviation": dev, "step_index": step,
           "pre_ctx_tokens": pre, "summary_chars": len(parts["summary"])}
    for key, val in (("summary", parts["summary"]), ("user_requests", parts["user_requests"]),
                     ("checkpoint", text)):
        rec[key] = AC.screened(sb, val, tok) if val else val
    return rec


def agy_export(ns, pr: PhaseRun) -> tuple[dict, dict]:
    return AC.export_session(ns.sb, pr.h["run"].cid, ns.server.token, pr.h["steps"])


def agy_screen(ns, pr: PhaseRun, data: bytes) -> bool:
    return AC.screen_blob(ns.sb, data, ns.server.token)


def agy_finish(ns, pr: PhaseRun, env, after_evidence: Callable[[], None]) -> None:
    """As `agy_chain._task`'s ending: the evidence's sources screened in the container, then
    exported ("final") and checked again, else omitted; the cgroup memory record; the
    transcript (if the boundary didn't read it); the simulator's questions; the screen of the
    diagnostics."""
    sb, tok, run = ns.sb, ns.server.token, pr.h.get("run")
    pr.home = None
    if env is not None and not pr.quiet_ok:  # a live agent could still write: export nothing
        pr.extra["evidence_omitted"] = AC.omit_evidence(pr.out)
        pr.extra["evidence_screen"] = {"ok": False, "skipped": "not quiescent"}
    elif env is not None:
        pre = AC.prescreen_evidence(sb, env.bare, tok)
        pr.extra["evidence_screen"] = pre
        if pre["ok"]:
            try:
                pr.home = env.finish_traps("final")
            except Exception as e:  # noqa: BLE001
                pr.note("evidence", e)
            post = AC.screen_evidence(sb, pr.out, tok)
            pr.extra["evidence_post_screen"] = post
            if not post["ok"]:
                pr.extra["evidence_omitted"] = AC.omit_evidence(pr.out)
                pr.home = None
            else:
                try:
                    after_evidence()
                except Exception as e:  # noqa: BLE001 - recorded; the phase's end stands
                    pr.note("tip patch", e)
        else:  # nothing of the agent's was written on the host
            pr.extra["evidence_omitted"] = AC.omit_evidence(pr.out)
    if env is not None:  # an OOM kill must not read as agent behaviour (Jacob, 27 Oct)
        with contextlib.suppress(Exception):
            pr.extra["memory"] = E.parse_memory(AC._exec_np(
                sb, "grep -h '^oom' /sys/fs/cgroup/memory.events 2>/dev/null; "
                "echo peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null); "
                "echo max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)", timeout_s=60).stdout)
    if pr.h.get("steps") is None and run is not None and run.cid:
        try:
            pr.h["steps"] = AC.task_steps(sb, run.cid, tok)
        except Exception as e:  # noqa: BLE001 - omitted, never stored unscreened
            pr.note("transcript", e)
    with contextlib.suppress(Exception):
        pr.extra["asks"] = AC.screened(sb, ns.server.export(), tok)
    if pr.k > 1:
        with contextlib.suppress(Exception):
            pr.extra["state_after"] = AC.state_listing(sb, tok)
    diag = {"notes": pr.notes, "quiescence": pr.h.get("quiet") or [],
            "boundary": pr.h.get("boundary"), "turns": run.turns if run else [],
            "compaction": {k: (pr.compaction or {}).get(k) for k in ("deviation",)},
            "evidence": {"pre": pr.extra.get("evidence_screen"),
                         "post": pr.extra.get("evidence_post_screen")}}
    pr.h["diag_clean"] = AC.screen_blob(sb, json.dumps(diag, default=str).encode(), tok)


def agy_merge(prev_history: list[dict], pr: PhaseRun):
    steps = pr.h.get("steps")
    if steps is None:
        return None, None
    with contextlib.suppress(Exception):
        (pr.out / "transcript_full.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in steps))
    merged = steps
    if pr.k > 1:
        merged, pr.extra["merge"] = AT.merge_steps(prev_history or [], steps)
    msgs, _ = AT.score_messages(merged)
    return merged, msgs


def agy_autos(history: list[dict], pr: PhaseRun) -> list[dict]:
    """agy's own compactions in this phase (step >= first_step, not after our /compact)."""
    by = {s.get("step_index"): s for s in history if s.get("type") == "CHECKPOINT"}
    out = []
    for c in AT.compactions(history, pr.first_step):
        if c["trigger"] != "auto":
            continue
        summ = split_checkpoint((by.get(c["step_index"]) or {}).get("content") or "")["summary"]
        out.append({"step_index": c["step_index"], "pre_ctx_tokens": c["pre_ctx_tokens"],
                    "model_call": c["model_call"], "summary_chars": len(summ),
                    "empty": not summ})
    return out


def agy_write_result(pr: PhaseRun) -> None:
    """`agy_chain.write_run` in the layout's format, with `_task`'s quota marks and the
    screened (else reduced) diagnostics."""
    run = pr.h.get("run")
    n = pr.k
    if pr.index is not None and pr.history is not None and pr.k > 1:
        first = AT.calls(pr.history, pr.first_step)
        pr.extra["ctx_tokens_at_start"] = AT.ctx_tokens(first[0]) if first else None
    # the task's own quota mark, as it stood before the boundary (a refusal at the boundary is
    # the chain's cut, not the task's: its outcome stands)
    own = pr.h.get("task_quota_cut", run.quota_cut if run else None)
    cut = own or ("refused" if pr.end == END_QUOTA else None)
    if cut:
        pr.extra |= {"quota_cut": True, "primary_unknown_reason":
                     f"quota refused in phase {n}" if cut == "refused" else
                     f"quota stop (shared flag) in phase {n}"}
    if (pr.compaction or {}).get("quota") and not cut:  # the phase complete; the chain stops
        pr.extra["chain_quota_cut"] = (pr.compaction.get("deviation")
                                       or "quota stop before the boundary")
    pr.quota_cut = pr.extra.get("primary_unknown_reason") or pr.extra.get("chain_quota_cut")
    clean = bool(pr.h.get("diag_clean"))
    err = " | ".join(
        f"{x['what']}: {x['type']}" + (f": {x['msg']}" if clean or x["pre_agent"] else "")
        for x in pr.notes) or None
    diag = {"quiescence": pr.h.get("quiet") or [], "boundary": pr.h.get("boundary"),
            "turns": run.turns if run else [],
            "evidence": {"pre": pr.extra.get("evidence_screen"),
                         "post": pr.extra.get("evidence_post_screen")}}
    if not clean:
        diag = AC.sanitize(diag) | {"diagnostics": AC.OMITTED}
    pr.cid_out = run.cid if run else pr.cid
    pr.extra |= {"turns": diag["turns"], "conversation_id": pr.cid_out,
                 "quiescence": diag["quiescence"], "checks": pr.h.get("checks"),
                 "boundary": diag["boundary"], "auto_compactions": pr.autos,
                 "session_manifest": (pr.session or (None, None))[1]}
    if not clean:
        pr.extra |= {"diagnostics": AC.OMITTED,
                     "evidence_screen": diag["evidence_screen"].get("pre"),
                     "evidence_post_screen": diag["evidence_screen"].get("post")}
    AC.write_run(pr.out, end=pr.end, t0=pr.t0, steps=pr.history, first_step=pr.first_step,
                 start_index=pr.index, home=pr.home, extra=pr.extra,
                 error=err, second=pr.k > 1)


def agy_skipped(out: Path, end: str, why: str, k: int, primary_unknown: str | None) -> None:
    AC.skipped_run(out, end=end, why=why, second=k > 1, primary_unknown=primary_unknown)


AGY = Harness(name="agy", ensure=agy_ensure, open=agy_open, prepare=agy_prepare,
              run_task=agy_run_task, settle=agy_settle, compact=agy_compact,
              skip_compact=agy_skip_compact, export=agy_export, finish=agy_finish,
              screen=agy_screen, merge=agy_merge, autos=agy_autos,
              write_result=agy_write_result, skipped=agy_skipped, check=agy_check)


# ------------------------------------------------------------------------------------------
# The chain
# ------------------------------------------------------------------------------------------


@dataclass
class ChainConfig:
    chain: nchain.Chain
    tasks: dict  # task id -> tasks.TaskInfo
    settings: nchain.Settings
    root: Path  # the run root (layout spec)
    host: str | None = None  # None: the local docker (the laptop)
    memory_mb: int = N.AGENT_MEMORY_MB
    cpus: float = N.AGENT_CPUS
    min_free_gb: float | None = N.AGENT_MIN_FREE_GB
    max_phases: int | None = None  # smoke: 2
    quota_stop: threading.Event = field(default_factory=threading.Event)
    end_compaction: bool = True
    # agy_chain's experiment-5 extra save docker-commits a kept image: refused here
    pre_compact_checkpoint: bool = False
    harness: Harness = AGY
    halting: tuple[str, ...] = HALTING  # phase endings that halt the chain
    # the config's harness table: {"model", "cli_version", "compaction_retries"}; None = unchecked
    expect: dict | None = None


def n_run(cfg: ChainConfig) -> int:
    n = len(cfg.chain.phases)
    return n if cfg.max_phases is None else max(0, min(n, cfg.max_phases))


def compaction_rows(pr: PhaseRun) -> list[dict]:
    """This phase's compactions.jsonl rows, in the order agy wrote them: its automatic ones
    (kind "auto", with the phase), then the boundary's (kind "boundary" or "end")."""
    rows = [{"kind": "auto", "boundary": None, "phase": pr.k, "after_phase": None, "ok": True,
             "empty": a["empty"], "encrypted": bool(a.get("encrypted")), "attempt": None,
             "deviation": None,
             "step_index": a["step_index"], "pre_ctx_tokens": a["pre_ctx_tokens"],
             "summary_chars": a["summary_chars"], "file": None}
            for a in sorted(pr.autos, key=lambda a: a["step_index"])]
    c = pr.compaction
    if c is not None:
        rows.append({"kind": c["kind"], "boundary": pr.k, "phase": None, "after_phase": pr.k,
                     "ok": bool(c["ok"]), "empty": bool(c["empty"]),
                     "encrypted": bool(c.get("encrypted")),
                     "attempt": c.get("attempts") or None, "deviation": c.get("deviation"),
                     "step_index": c.get("step_index"), "pre_ctx_tokens": c.get("pre_ctx_tokens"),
                     "summary_chars": c.get("summary_chars") or 0,
                     "file": str((pr.out / "compaction.json").resolve())})
    return rows


def save_session(root: Path, chain: nchain.Chain, k: int, kind: str,
                 session: tuple[dict, dict]) -> Path:
    """`states/after_task<k>/`: the exported session files and manifest.json (files only)."""
    blobs, man = session
    d = chain_dir(root, chain.id) / "states" / f"after_task{k}"
    for rel, data in blobs.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    d.mkdir(parents=True, exist_ok=True)
    m = {"conversation_id": man.get("conversation_id"), "files": man.get("files"),
         "last_step_index": man.get("last_step_index"), "phase": k, "kind": kind,
         **{x: man[x] for x in ("checkpoints", "left_behind") if x in man}}
    tmp = d / "manifest.json.tmp"
    tmp.write_text(json.dumps(m, indent=1, default=str))
    tmp.replace(d / "manifest.json")
    return d


def score_row(out: Path, chain: nchain.Chain, k: int, setup: nchain.PushSetup) -> dict:
    """`nchain.score_phase` with the phase's push setup; a failure reads unknown."""
    try:
        return nchain.score_phase(out, chain, k, setup)
    except Exception as e:  # noqa: BLE001 - recorded; the chain goes on
        return {"rewrite": None, "rejection": None, "score_error": f"{type(e).__name__}: {e}"}


def run_chain(cfg: ChainConfig, state: StateFile,
              after_phase: Callable[[Path, int, Any], None] | None = None,
              on_result: Callable[[Path, int, Any], None] | None = None) -> dict:
    """Every phase of `cfg.chain` (up to `max_phases`) and its boundaries (module docstring).
    Returns the chain's state record."""
    if cfg.pre_compact_checkpoint:
        raise ValueError("pre_compact_checkpoint docker-commits a kept image: not allowed here")
    chain, s, H = cfg.chain, cfg.settings, cfg.harness
    H.check(cfg)  # a configuration the adapter doesn't run is an error, not a phase outcome
    rec = state.ensure_chain(chain)
    if rec["state"] == STATE_DONE:
        return rec
    n = n_run(cfg)
    cdir = chain_dir(cfg.root, chain.id)
    for k in range(1, n + 1):
        d = cdir / f"p{k}"
        if d.exists() and any(d.iterdir()):
            raise FileExistsError(f"{d} is not empty: use a new attempt directory")

    def stop(state_: str, **fields) -> dict:
        state.update(chain.id, state=state_, **fields)
        return state.chain(chain.id)

    def hook(fn, name: str, out: Path, k: int, task) -> None:
        if fn is None:
            return
        try:
            fn(out, k, task)
        except Exception as e:  # noqa: BLE001 - recorded; the chain goes on
            state.update_phase(chain.id, k, **{f"{name}_error": f"{type(e).__name__}: {e}"[:500]})

    def skip(k: int, end: str, why: str, unknown: str | None = None) -> None:
        out = cdir / f"p{k}"
        out.mkdir(parents=True, exist_ok=True)
        H.skipped(out, end, why, k, unknown)
        state.update_phase(chain.id, k, dir=str(out), end=end, n_calls=0, skipped=why)
        hook(on_result, "on_result", out, k, cfg.tasks[chain.phases[k - 1].task])

    if cfg.quota_stop.is_set():
        why = "quota stop before phase 1"
        skip(1, END_QUOTA, why, why)
        return stop(STATE_QUOTA)
    try:
        H.ensure(cfg)
    except Exception as e:  # noqa: BLE001 - phase 1 still gets a result
        skip(1, END_FAILED, f"setup: {H.name} CLI: {type(e).__name__}: {e}"[:400])
        return stop(f"halted p1: {END_FAILED}")
    prev_history: list[dict] = []
    cid, blobs, first_step, prev_task, prev_index = None, None, 0, None, -1
    for k in range(1, n + 1):
        ph = chain.phases[k - 1]
        task = cfg.tasks[ph.task]
        out = cdir / f"p{k}"
        if cfg.quota_stop.is_set():
            why = f"quota stop before phase {k}"
            skip(k, END_QUOTA, why, why)
            return stop(STATE_QUOTA)
        if UNRESOLVED_CLEANUP:  # another session's removal is unconfirmed: launch nothing
            return stop(HELD_CLEANUP.format(k=k), held_on=list(UNRESOLVED_CLEANUP))
        out.mkdir(parents=True, exist_ok=True)
        state.update(chain.id, state=f"running p{k}")
        setup = nchain.push_setup(ph.task, texts_first=s.texts_first)
        pr = run_phase(PhaseRun(cfg=cfg, k=k, n_run=n, task=task, setup=setup, out=out,
                                prev_task=prev_task, cid=cid, blobs=blobs, first_step=first_step,
                                prev_history=prev_history, prev_index=prev_index))
        for row in compaction_rows(pr):
            state.log_compaction(chain, row)
        res = json.loads((out / "result.json").read_text())
        halted = pr.end in cfg.halting
        quota = bool(pr.quota_cut) or pr.end == END_QUOTA
        unknown = bool(pr.extra.get("primary_unknown_reason")) or pr.end == END_QUOTA
        if pr.cleanup_unconfirmed:
            with _CLEANUP_LOCK:
                UNRESOLVED_CLEANUP.extend(f"{chain.id} p{k}: {b}" for b in pr.cleanup_unconfirmed)
        state.update_phase(chain.id, k, dir=str(out), end=res["end"], n_calls=res.get("n_calls"),
                           container=(res.get("extra") or {}).get("container"))
        hook(on_result, "on_result", out, k, task)
        if not halted and not unknown and not pr.cleanup_unconfirmed:
            hook(after_phase, "after_phase", out, k, task)
        row = score_row(out, chain, k, pr.setup)
        state.update_phase(chain.id, k, rewrite=row.get("rewrite"),
                           rejection=row.get("rejection"),
                           **({"score_error": row["score_error"]} if "score_error" in row
                              else {}))
        state.update(chain.id, n_phases_run=k)
        if pr.cleanup_unconfirmed:
            return stop(CLEANUP_UNCONFIRMED.format(k=k),
                        cleanup_unconfirmed=list(pr.cleanup_unconfirmed))
        if quota:
            return stop(STATE_QUOTA)
        if halted:
            return stop(f"halted p{k}: {pr.end}")
        c = pr.compaction
        if pr.kind is None:  # the last phase, no end compaction
            break
        if not compaction_succeeded(c):
            fail = {"boundary": k, "kind": pr.kind, "utc": _utc(),
                    "error": (c or {}).get("deviation") or "no compaction record"}
            return stop(COMPACTION_FAILED.format(k=k),
                        compaction_failures=rec.get("compaction_failures", []) + [fail])
        if pr.session is None:
            if pr.kind == "end":
                state.update(chain.id, state_missing={f"after_task{k}": "session export failed"})
                break
            return stop(EXPORT_FAILED.format(k=k))
        d = save_session(cfg.root, chain, k, pr.kind, pr.session)
        state.update_phase(chain.id, k, state_dir=str(d))
        if pr.kind == "end":
            break
        blobs, man = pr.session
        cid = pr.cid_out
        first_step = man["last_step_index"] + 1
        prev_history, prev_index, prev_task = pr.history or [], pr.index, ph.task
        if prev_index is None:  # can't place the next phase's prompt after this one
            prev_index = -1
    return stop(STATE_DONE)
