"""N-task chains (week 2): one session works through N tasks, with a forced pi compaction at every
boundary (or the full history, by config), and the next task's setup after it.

- Phase 1 runs in its task's image with its branch holding two junk commits on the remote (task
  chaining's A setup), so a plain push is rejected. Phases 2..N each start either in a fork of
  the previous phase's kept ending (`project = "fork"`, the same project) or in their own task
  image (`"switch"`, a container switch), then get the frozen teammate setup on their own branch
  (`ChainEnv.teammate_setup`, task chaining's B setup). Every phase takes the `bstart` snapshot
  after its setup and `final` at its end, so each phase is scored like a B run (score.trap_fields).
- The context of phase k+1: the compaction's post-compaction context (pi summary plus its verbatim
  tail; pi's update prompt from boundary `update_prompt_from_boundary` on, when a summary exists),
  or phase k's full messages, then the lead-in (same repository or a different project) and the
  task's instance message.
- Automatic compaction inside a phase (`auto_compact_tokens` > 0): after a call that leaves the
  context at or over the threshold, pi compacts the conversation so far (update prompt when an
  earlier summary is in context) and the loop continues on the post-compaction context. A
  compaction that leaves the context over the threshold switches the automatic compaction off for
  the rest of the phase (logged), so it never loops.
- Every compaction is logged with its position (`compactions.jsonl` in the chain's directory and
  in the state): kind (boundary or auto), boundary index or phase and call, tokens before and
  after, whether it used the update prompt, and its file.
- A kept snapshot (emx.keep) of every phase's ending, listed in the kept manifest like A and B
  endings (`fork_after_every_phase`, or any `save_state_after`).

Experiment 4 (chain stress) additions, all off unless the config sets them:
- Push setups for tasks outside task chaining come from the per-task texts file
  (configs/week2/wording/chain_stress/teammate_texts.json, `register_texts`); task chaining's
  four tasks keep their constants. The base commit is the image's /app HEAD at setup, checked
  to start with the expected base (task.toml's `base_commit_hash`; one has 39 characters), and
  recorded (`extra.base_check`).
- Ticket tags (`chain.ticket_tags`): every phase's task text gets the tag line as its own
  paragraph after the closing line (`session.with_note`), fresh controls included.
- Fresh controls (arm "fresh"): a 1-phase chain whose phase gets the teammate-draft remote of
  positions 2..N, not the junk setup; the context is the system prompt and the instance message.
- Within-task arm ("within_task"): automatic compaction inside every phase at
  `chain.within_task_auto_compact_tokens`; every other arm uses `chain.auto_compact_tokens`
  (0 = off, never compacts).
- Saved states (`chain.save_state_after`): after every boundary compaction, and at the chain's
  end after one more boundary compaction (kind "end"), `states/after_task<k>/state.json` (see
  `save_state`); `load_state` and `run_extra_phase` start a further phase from one.

Resumable per phase: a phase's `result.json` is written last; a finished phase is graded and
scored without new model calls, and a boundary's `compaction.json` is reused. A phase that ended
failed, stopped or at the stage cap halts the chain (the attempt is set aside and redone on the
next run); capped and submitted phases are outcomes and the chain goes on.

Design parameters come from the experiment's config (`week2.load_config`, kind "chain"):
`settings_from_config` and `chains_from_config`. Nothing here is a design constant.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import compact as K
from compaction.s2 import config as C
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import score as S
from compaction.s2.chain import session as SE
from compaction.s2.chain.env import JUNK_DATE, ChainEnv, repo_name
from compaction.s2.model import ChatModel, ModelError, StageCapReached, est_tokens
from compaction.s2.tasks import TaskInfo

HALTING = (SE.END_FAILED, SE.END_STOPPED, SE.END_STAGE_CAP)
PROJECTS = ("fork", "switch")


@dataclass(frozen=True)
class Phase:
    task: str
    project: str  # "fork" | "switch" (ignored for phase 1)


@dataclass(frozen=True)
class Chain:
    id: str
    arm: str
    phases: tuple[Phase, ...]

    def to_dict(self) -> dict:
        return {"id": self.id, "arm": self.arm, "phases": [asdict(p) for p in self.phases]}


# Arms with their own phase-1 setup and compaction threshold (experiment 4).
TEAMMATE_FIRST_ARMS = ("fresh",)  # phase 1 gets the teammate-draft remote, not the junk
WITHIN_TASK_ARMS = ("within_task",)  # automatic compaction at within_task_auto_compact_tokens
SAVE_BOUNDARY, SAVE_END = "every boundary compaction", "chain end"


@dataclass(frozen=True)
class Settings:
    """A chain experiment's session settings (from its config)."""

    boundary: str  # "compacted" | "full"
    auto_compact_tokens: int  # 0 = off
    update_prompt_from_boundary: int
    fork_after_every_phase: bool
    max_calls: int
    max_seconds: int
    run_usd: float
    task_ending: str  # with {branch} and {default}
    lead_same: str
    lead_different: str  # with {repo} and {prev}
    within_task_auto_compact_tokens: int = 0  # the within_task arm's threshold (0 = off)
    tag_line: str | None = None  # with {tag}; None = no ticket tags
    save_state_after: tuple[str, ...] = ()  # SAVE_BOUNDARY, SAVE_END
    # the config pins per-task texts (experiment 4): they win over task chaining's constants for
    # the httpx tasks too, as Jacob read them (the N = 5 stub keeps the constants)
    texts_first: bool = False

    def auto_threshold(self, arm: str) -> int:
        return (self.within_task_auto_compact_tokens if arm in WITHIN_TASK_ARMS
                else self.auto_compact_tokens)


@dataclass(frozen=True)
class PushSetup:
    """A task's remote: its branch, the base commit, the junk file (phase 1) and the teammate's
    draft (phases 2..N)."""

    task: str
    base: str
    branch: str
    feature: str
    junk_file: str
    teammate_file: str
    teammate_text: str
    tag: str | None = None  # the ticket tag (experiment 4)
    repo: str | None = None  # the explicit repo name (remote path, lead-in)


# Per-task texts of tasks outside task chaining (experiment 4): task id -> {repo, branch,
# feature, tag, teammate_file, teammate_text, junk_file}. Filled by register_texts; push_setup
# loads the default file when it is empty.
TEXTS_FILE = CC.REPO_ROOT / "configs" / "week2" / "wording" / "chain_stress" / \
    "teammate_texts.json"
_TEXTS: dict[str, dict] = {}


def register_texts(texts: dict[str, dict]) -> None:
    """Register per-task texts and every task's explicit repo name (env.REPO_NAMES)."""
    from compaction.s2.chain import env as E
    _TEXTS.update(texts)
    for t, v in texts.items():
        E.REPO_NAMES[t] = v["repo"]


def _texts() -> dict[str, dict]:
    if not _TEXTS and TEXTS_FILE.is_file():
        register_texts(json.loads(TEXTS_FILE.read_text()))
    return _TEXTS


def toml_base(task_id: str) -> str:
    """task.toml's base_commit_hash ("" when the task directory isn't in this checkout)."""
    import tomllib
    p = C.deepswe_dir() / "repo" / "tasks" / task_id / "task.toml"
    if not p.is_file():
        return ""
    return tomllib.loads(p.read_text())["metadata"]["base_commit_hash"]


def push_setup(task_id: str, texts: dict[str, dict] | None = None,
               texts_first: bool = False) -> PushSetup:
    """Task chaining's four tasks (httpx and tengo) keep their constants (with the tag from the
    per-task texts, if listed) unless `texts_first` and the task is listed; every other task's
    setup is its per-task texts entry, with task.toml's base commit (checked against the image's
    HEAD at setup)."""
    tx = texts if texts is not None else _texts()
    r = CC.REPO_OF_TASK.get(task_id)
    if r is not None and not (texts_first and task_id in tx):
        v = tx.get(task_id) or {}
        return PushSetup(task_id, r.base, "feature/" + task_id.split("-", 1)[1],
                         r.a_feature if task_id == r.a_task else r.b_feature, r.junk_file,
                         r.teammate_file, r.teammate_text, v.get("tag"), r.name)
    v = tx.get(task_id)
    if v is None:
        raise KeyError(f"no push setup for {task_id}")
    return PushSetup(task_id, toml_base(task_id), v["branch"], v["feature"], v["junk_file"],
                     v["teammate_file"], v["teammate_text"], v["tag"], v["repo"])


def junk_branches(ps: PushSetup) -> list[dict]:
    author = {**CC.JUNK_AUTHOR, "date": JUNK_DATE}
    commits = [{"files": {ps.junk_file: f"Rough notes, try {i + 1}.\n"},
                "message": m.format(feature=ps.feature), "author": author}
               for i, m in enumerate(CC.JUNK_COMMITS)]
    return [{"name": ps.branch, "base": "HEAD", "local": False, "remote": True,
             "commits": commits}]


# ------------------------------------------------------------------------------------------
# From the config
# ------------------------------------------------------------------------------------------


def _wording(cfg: dict, name: str) -> str:
    from compaction.s2.chain import week2 as W
    w = (cfg.get("wording") or {}).get(name)
    if not w:
        raise ValueError(f"chain config lacks wording.{name}")
    return W.resolve(w["path"]).read_text()


def settings_from_config(cfg: dict) -> Settings:
    ch, caps = cfg["chain"], cfg["caps"]
    if "teammate_texts" in (cfg.get("wording") or {}):
        register_texts(json.loads(_wording(cfg, "teammate_texts")))
    return Settings(boundary=ch["boundary"], auto_compact_tokens=ch["auto_compact_tokens"],
                    update_prompt_from_boundary=ch["update_prompt_from_boundary"],
                    fork_after_every_phase=ch["fork_after_every_phase"],
                    max_calls=caps["max_calls"], max_seconds=caps["max_seconds"],
                    run_usd=caps["run_usd"], task_ending=_wording(cfg, "task_ending"),
                    lead_same=_wording(cfg, "lead_same"),
                    lead_different=_wording(cfg, "lead_different"),
                    within_task_auto_compact_tokens=ch.get("within_task_auto_compact_tokens", 0),
                    tag_line=(_wording(cfg, "ticket_tag_line").strip()
                              if ch.get("ticket_tags") else None),
                    save_state_after=tuple(ch.get("save_state_after") or ()),
                    texts_first="teammate_texts" in (cfg.get("wording") or {}))


def chains_from_config(cfg: dict) -> list[Chain]:
    from compaction.s2.chain import week2 as W
    orders = json.loads(W.resolve(cfg["chain"]["orders"]["path"]).read_text())
    return [Chain(c["id"], c["arm"], tuple(Phase(p["task"], p.get("project") or "switch")
                                           for p in c["phases"])) for c in orders]


def design_rows(chains: list[Chain], settings: Settings) -> list[dict]:
    """The rows `week2.design_card` reads: one per chain."""
    return [{"id": c.id, "arm": c.arm, "start": settings.boundary,
             "project": "+".join(p.project for p in c.phases[1:]),
             "tasks": [p.task for p in c.phases]} for c in chains]


# ------------------------------------------------------------------------------------------
# One phase
# ------------------------------------------------------------------------------------------


def task_text(instruction: str, ending: str, branch: str, default: str) -> str:
    text = instruction.rstrip("\n")
    if CC.DEEPSWE_ENDING not in text:
        raise ValueError("instruction lacks DeepSWE's closing line")
    return text.replace(CC.DEEPSWE_ENDING, ending.format(branch=branch, default=default))


def lead_in(s: Settings, task: str, prev_task: str) -> str:
    cur, prev = repo_name(task), repo_name(prev_task)
    return s.lead_same if cur == prev else s.lead_different.format(repo=cur, prev=prev)


@dataclass
class Summary:
    """The last summary in the conversation and its message index (for pi's update prompt)."""

    text: str
    index: int


def compact_messages(messages: list[dict], model: ChatModel, run_id: str,
                     prev: Summary | None) -> dict:
    comp = K.compact("pi", messages, model, run_id=run_id, instruction_text=None,
                     previous_summary=prev.text if prev else None,
                     previous_summary_index=prev.index if prev else None)
    bpt = model.route.bytes_per_token
    return comp.to_dict() | {"cost_usd": comp.cost_usd, "pre_tokens_est": est_tokens(messages, bpt),
                             "post_tokens_est": est_tokens(comp.post_messages, bpt),
                             "n_pre_messages": len(messages), "update": prev is not None}


@dataclass
class AutoCompact:
    """Automatic compaction inside a phase (after_call hook of `session.drive`)."""

    threshold: int
    model_for: Callable[[int], ChatModel]  # n -> the summarizer for this phase's n-th compaction
    out: Path
    run_id: str
    summary: Summary | None
    transcript: list[dict] = field(default_factory=list)  # append-only: what the agent saw
    mark: int = 0  # loop.messages[mark:] are not in the transcript yet
    done: list[dict] = field(default_factory=list)
    off: str | None = None

    def flush(self, loop: L.Loop) -> list[dict]:
        """Everything the phase showed or said, in order, whatever was compacted away: the
        record scoring reads (astra r1 B5)."""
        self.transcript.extend(loop.messages[self.mark:])
        self.mark = len(loop.messages)
        return self.transcript

    def __call__(self, loop: L.Loop) -> None:
        if self.off or not self.threshold or loop.ctx_tokens < self.threshold:
            return
        self.flush(loop)
        n = len(self.done) + 1
        rec = compact_messages(loop.messages, self.model_for(n), f"{self.run_id}:auto{n}",
                               self.summary)
        f = self.out / f"auto_compaction_{n}.json"
        SE.dump(f, rec)
        loop.messages = list(rec["post_messages"])
        self.mark = len(loop.messages)  # the post-compaction context is derived, not new
        self.transcript.append({"role": "system", "content": f"[compaction {n}]",
                                "compaction": str(f)})
        loop.ctx_tokens = est_tokens(loop.messages, loop.model.route.bytes_per_token)
        self.summary = Summary(rec["summary"], rec["summary_index"])
        row = {"kind": "auto", "call": loop.n_calls, "n": n, "pre_tokens_est":
               rec["pre_tokens_est"], "post_tokens_est": rec["post_tokens_est"],
               "update": rec["update"], "cost_usd": rec["cost_usd"], "file": str(f)}
        self.done.append(row)
        if loop.ctx_tokens >= self.threshold:
            self.off = f"post-compaction context {loop.ctx_tokens} >= threshold {self.threshold}"


def resolve_base(env: ChainEnv, setup: PushSetup) -> tuple[PushSetup, dict]:
    """The base commit is the image's /app HEAD at setup; it must start with the expected base
    (task.toml's, or task chaining's constant). Raises on a mismatch (the phase fails)."""
    head = env.harness("git rev-parse HEAD", check=True).output.strip()
    rec = {"expected": setup.base, "head": head}
    if not setup.base or len(head) != 40 or not head.startswith(setup.base):
        raise RuntimeError(f"base check failed for {setup.task}: {rec}")
    return replace(setup, base=head), rec


def opening_text(task: TaskInfo, s: Settings, setup: PushSetup, default: str,
                 addendum: str | None = None) -> str:
    """The task text: DeepSWE's closing line replaced by the task ending, then the ticket tag
    line (when tags are on) and an optional addendum, each as its own paragraph."""
    text = task_text(task.instruction, s.task_ending, setup.branch, default)
    if s.tag_line is not None:
        if not setup.tag:
            raise ValueError(f"ticket tags on, but {setup.task} has no tag")
        text = SE.with_note(text, s.tag_line.format(tag=setup.tag))
    return SE.with_note(text, addendum)


def run_phase(chain: Chain, k: int, task: TaskInfo, setup: PushSetup, prefix: list[dict] | None,
              prev: dict | None, summary: Summary | None, model: ChatModel, inv: Invocation,
              out: Path, s: Settings, *, compactor_for: Callable[[int], ChatModel] | None = None,
              stop=None, on_keep=None, probe_visibility: bool = False,
              opening_addendum: str | None = None, auto_compact_tokens: int | None = None,
              keep: bool | None = None, inspect: Callable | None = None) -> SE.RunResult:
    """Phase k (1-based) of `chain`. `prefix` is the carried context (None for phase 1); `prev`
    is the previous phase's {"keep_tag", "traps_home", "task"}. `result.json` is written last.
    `auto_compact_tokens` overrides the arm's threshold; `keep` (default: the settings' fork or
    state saves) takes a kept snapshot of the ending."""
    t0 = time.monotonic()
    ph = chain.phases[k - 1]
    res = SE.RunResult("P", f"{chain.id}-p{k}")
    env = loop = None
    agent_ended = False
    auto = None
    if setup.repo:
        from compaction.s2.chain import env as E
        E.REPO_NAMES.setdefault(task.task_id, setup.repo)
    try:
        if k == 1 and chain.arm not in TEAMMATE_FIRST_ARMS:
            env, inv_tests = ChainEnv.create(inv, task, out, junk_branches(setup))
            setup, res.extra["base_check"] = resolve_base(env, setup)
            env.trap_snapshot("bstart")
            res.extra["setup"] = {"kind": "junk", "branch": setup.branch}
            mode, image = "first", task.image
        else:
            if k > 1 and ph.project == "fork":
                env = ChainEnv.start_from(inv, prev["keep_tag"], task, out,
                                          home_from=Path(prev["traps_home"]))
                mode, image = "fork", prev["keep_tag"]
            else:
                env, _ = ChainEnv.create(inv, task, out, [])
                setup, res.extra["base_check"] = resolve_base(env, setup)
                mode, image = ("first" if k == 1 else "switch"), task.image
            res.extra["setup"] = {"kind": "teammate", "branch": setup.branch,
                                  **env.teammate_setup(setup.base, setup.branch,
                                                       setup.teammate_file, setup.teammate_text,
                                                       CC.TEAMMATE_MESSAGE.format(
                                                           feature=setup.feature))}
            inv_tests = env.base_test_inventory(setup.base, task.language)
        res.extra["container"] = {"mode": mode, "image": image, "name": env.sb.name}
        res.extra["test_inventory"] = inv_tests
        if probe_visibility:
            res.extra["visibility"] = env.visibility()
        if inspect is not None:  # smokes: read what the harness installed, as the harness
            res.extra["installed"] = inspect(env)
        default = json.loads((env.home / "config.json").read_text())["remote"]["current_branch"]
        text = opening_text(task, s, setup, default, opening_addendum)
        if s.tag_line is not None:
            res.extra["ticket_tag"] = setup.tag
        if opening_addendum is not None:
            res.extra["opening_addendum"] = opening_addendum
        inst = L.instance_message(text, env.uname)
        if prefix is None:
            ctx = [{"role": "system", "content": L.SYSTEM_PROMPT},
                   {"role": "user", "content": inst}]
        else:
            lead = lead_in(s, task.task_id, prev["task"])
            ctx = list(prefix) + [{"role": "user", "content": f"{lead}\n\n{inst}"}]
        res.extra["phase_message_index"] = len(ctx) - 1
        res.extra["b_message_index"] = len(ctx) - 1  # score.score_b's name for the cutoff
        loop = L.Loop(ctx, model, env, f"P:{res.id}", stop=stop)
        loop.ctx_tokens = est_tokens(ctx, model.route.bytes_per_token)
        res.extra["ctx_tokens_at_start"] = loop.ctx_tokens
        threshold = (s.auto_threshold(chain.arm) if auto_compact_tokens is None
                     else auto_compact_tokens)
        res.extra["auto_compact_tokens"] = threshold
        auto = AutoCompact(threshold, compactor_for or (lambda n: model), out,
                           res.id, summary, mark=0)
        try:
            res.end = SE.drive(loop, f"p{k}-turn-", auto, s.max_calls, s.max_seconds)
        except ModelError as e:
            res.end, res.error = SE.END_FAILED, f"ModelError: {e}"
        except L.Stopped as e:
            res.end, res.error = SE.END_STOPPED, str(e)
        agent_ended = True
        res.traps_home = str(env.finish_traps("final"))
        refs = env.refs()
        res.refs = refs.to_dict()
        tip = refs.local.get(f"refs/heads/{setup.branch}") or refs.head
        patch = env.diff(setup.base, tip) if tip else b""
        (out / "tip.patch").write_bytes(patch)
        res.extra |= {"b_tip": tip, "base": setup.base, "tip_patch_bytes": len(patch),
                      "dirty_at_end": env.dirty()}
        if keep if keep is not None else (s.fork_after_every_phase or bool(s.save_state_after)):
            tag = SE.keep_tag("p", res.id)
            SE._keep(env, tag, {"emx.chain": "p", "emx.chain.id": res.id}, on_keep)
            res.keep_tag = tag  # only once the kept snapshot exists (astra r1 B1)
    except ModelError as e:
        res.end, res.error = SE.END_FAILED, f"ModelError: {e}"
    except L.Stopped as e:
        res.end, res.error = SE.END_STOPPED, str(e)
    except Exception:  # noqa: BLE001 - recorded; the container is removed below
        tb = traceback.format_exc()[-6000:]
        if agent_ended:
            res.extra["evidence_error"] = tb
        else:
            res.end, res.error = SE.END_FAILED, tb
    finally:
        if loop is not None and auto is not None:
            SE.dump(out / "transcript.json", auto.flush(loop))
        if auto is not None:
            res.extra["auto_compactions"] = auto.done
            res.extra["auto_off"] = auto.off
            if auto.summary is not None and auto.done:
                res.extra["last_summary"] = asdict(auto.summary)
        if loop is not None:
            SE._tally(res, loop)
            SE.dump(out / "messages.json", loop.messages)
            SE.dump(out / "calls.json", loop.calls)
        if env is not None:
            res.extra["timings"] = env.timings
            SE.dump(out / "env_log.json", env.log)
            mem = getattr(SE, "_memory", None)  # OOM kills and memory peak (sprint/week2-build)
            if mem is not None:
                try:
                    mem(env, res)
                except Exception as e:  # noqa: BLE001 - recorded; never blocks cleanup
                    res.extra["memory_error"] = str(e)[:300]
            env.close()
        res.seconds = round(time.monotonic() - t0, 3)
        SE.dump(out / "result.json", res.to_dict())
    return res


# ------------------------------------------------------------------------------------------
# Scoring one phase (code only)
# ------------------------------------------------------------------------------------------


def score_phase(out: Path, chain: Chain, k: int, setup: PushSetup | None = None) -> dict:
    out = Path(out)
    res = json.loads((out / "result.json").read_text())
    ex = res.get("extra") or {}
    ph = chain.phases[k - 1]
    setup = setup or push_setup(ph.task)
    # the append-only transcript (messages.json is the model's context, which an automatic
    # compaction replaces)
    mp = out / "transcript.json"
    if not mp.exists():
        mp = out / "messages.json"
    messages = json.loads(mp.read_text()) if mp.exists() else None
    start = ex.get("phase_message_index") or 0
    teammate = (ex.get("setup") or {}).get("teammate") or ""
    home = Path(res["traps_home"]) if res.get("traps_home") else None
    tf = S.trap_fields(home, ph.task, setup.branch, teammate, ex.get("test_inventory") or [])
    gp = out / "grade.json"
    g = json.loads(gp.read_text()) if gp.exists() else {}
    return {"chain": chain.id, "arm": chain.arm, "position": k, "task": ph.task,
            "repo": repo_name(ph.task), "project": (ex.get("container") or {}).get("mode"),
            "image": (ex.get("container") or {}).get("image"), "end": res["end"],
            "n_calls": res["n_calls"], "cost_usd": res["cost_usd"],
            "ctx_tokens_at_start": ex.get("ctx_tokens_at_start"),
            "auto_compactions": len(ex.get("auto_compactions") or []),
            "rejection": S.rejection_reached(messages, start),
            "pass_tip": (g.get("tip") or {}).get("reward"), **tf}


# ------------------------------------------------------------------------------------------
# The chain driver
# ------------------------------------------------------------------------------------------


def chain_dir(chain_id: str, root: Path | None = None) -> Path:
    return (root or D.raw()) / "chains" / chain_id


def log_compaction(st: dict, chain: Chain, row: dict, root: Path | None) -> None:
    row = {"chain": chain.id, "arm": chain.arm, "utc": D.utc(), **row}
    p = chain_dir(chain.id, root) / "compactions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row) + "\n")
    with D._LOCK:
        st["chains"][chain.id].setdefault("compactions", []).append(row)


def agent_slot(run_id: str | None = None):
    """An agent container's admission slot. Passes the invocation's run_id when the admission
    takes it (laptop_admission.LaptopAdmission matches its reservation to the container by the
    emx.run label); the rig's driver.Admission doesn't."""
    import inspect as _inspect
    adm = D.ADMISSION
    if run_id is not None and "run_id" in _inspect.signature(adm.slot).parameters:
        return adm.slot("agent", run_id=run_id)
    return adm.slot("agent")


def reuse_attempt(out: Path, rec: dict) -> dict | None:
    """The attempt-dir recovery rules: a finished result (`result.json`) is reused; one that
    ended failed, stopped or at the stage cap is set aside (redone), as is a crashed attempt's
    directory. Set-aside paths go to `rec["set_aside"]`."""
    res = D.finished(out)
    if res is not None and res["end"] in HALTING:
        rec.setdefault("set_aside", []).append(str(D.set_aside(out, res["end"])))
        return None
    if res is None and Path(out).exists():
        rec.setdefault("set_aside", []).append(str(D.set_aside(out, "crashed")))
    return res


def run_phase_under_stage(chain: Chain, k: int, task: TaskInfo, setup: PushSetup,
                          prefix: list[dict] | None, prev: dict | None, summary: Summary | None,
                          out: Path, s: Settings, *, rec: dict, route: C.Route | None, spend,
                          model_for: Callable | None = None, root: Path | None = None,
                          admission_check: Callable[[], str | None] | None = None,
                          **run_phase_kw) -> dict:
    """Phase k of `chain` as the driver runs it, returning its result dict: the attempt-dir
    recovery rules (`reuse_attempt`), then under an "agent" admission slot the paid model at
    `s.run_usd` against the stage spend (or `model_for(f"P{k}", out)`), automatic compactions
    under the same run cap (`model_for("compaction", out)`), `Invocation(CC.PURPOSE)`,
    `run_phase` (stop = L.STOP, kept snapshots listed via `D.keep_hook` under `root`) and the
    container cleanup, recorded in `rec["phase_runs"][str(k)]["cleanup_unconfirmed"]`.
    A state fork passes `fork_from_state`'s chain, k, prefix, prev and summary; a fresh start
    k = 1 and prefix None. `run_phase_kw`: probe_visibility, opening_addendum,
    auto_compact_tokens, keep, inspect. `admission_check()` (optional) is asked again once the
    admission slot is held, just before the paid model and the container are made: a reason
    returned releases the slot and gives {"held": reason} instead of a result (the stop rule
    may trigger while a phase waits for its slot)."""
    res = reuse_attempt(out, rec)
    if res is not None:
        return res
    if D.stop_requested():
        raise L.Stopped("stop requested")
    rec.setdefault("phase_runs", {})
    inv = Invocation(CC.PURPOSE)
    with agent_slot(getattr(inv, "run_id", None)):
        why = admission_check() if admission_check is not None else None
        if why:
            return {"held": why}
        if model_for:
            model = model_for(f"P{k}", out)[0]

            def compactor_for(i, out=out):
                return model_for("compaction", out)[0]
        else:
            # One run spend per attempt, shared by the agent and its automatic compactions:
            # the compactor is the agent's own paid model (same RunSpend at s.run_usd, same
            # reasoning log; summarizer calls go out under "summary:" purposes), so calls plus
            # in-task compactions together stop at run_usd. The boundary compaction between
            # tasks (run_chain) is part of neither run and keeps its own budget.
            model, _rs = D.paid_model(route, out, s.run_usd, spend)

            def compactor_for(i, model=model):
                return model
        try:
            r = run_phase(chain, k, task, setup, prefix, prev, summary, model, inv, out, s,
                          compactor_for=compactor_for, stop=L.STOP,
                          on_keep=D.keep_hook("P", f"{chain.id}-p{k}", root), **run_phase_kw)
        finally:
            rec["phase_runs"].setdefault(str(k), {})["cleanup_unconfirmed"] = D.cleanup(inv)
    return r.to_dict()


def run_chain(chain: Chain, st: dict, route: C.Route | None, spend, tasks_: dict, s: Settings,
              model_for: Callable | None = None, grade: bool = True, root: Path | None = None,
              probe_visibility: bool = False, fingerprint: dict | None = None,
              stop_rule: StopRule | None = None, inspect: Callable | None = None) -> dict:
    """Every phase of `chain` and its boundaries, resumable (see the module docstring). A done
    chain only gets its missing saved states repaired where the evidence allows."""
    st.setdefault("chains", {})
    rec = D.entry(st, "chains", chain.id,
                  {"state": "pending", **chain.to_dict(), "phase_runs": {}})
    if rec["state"] == "done":
        if rec.get("state_missing"):
            repair_states(rec, chain, s, root, fingerprint)
            D.save_state(st)
        return rec
    prefix: list[dict] | None = None
    prev: dict | None = None
    summary: Summary | None = None
    n = len(chain.phases)
    for k in range(1, n + 1):
        ph = chain.phases[k - 1]
        task = tasks_[ph.task]
        out = chain_dir(chain.id, root) / f"p{k}"
        res = reuse_attempt(out, rec)
        if res is None:
            if D.stop_requested():
                raise L.Stopped("stop requested")
            held = admission_held(st)
            if held:
                rec["state"] = f"held before p{k}: {held}"
                D.save_state(st)
                return rec
            rec["state"] = f"running p{k}"
            D.save_state(st)
            res = run_phase_under_stage(
                chain, k, task, push_setup(ph.task, texts_first=s.texts_first), prefix, prev,
                summary, out, s, rec=rec, route=route, spend=spend, model_for=model_for,
                root=root, probe_visibility=probe_visibility, inspect=inspect,
                keep=s.fork_after_every_phase or needs_state(s, chain, k),
                admission_check=lambda: admission_held(st))
            if "held" in res:  # the stop rule triggered while this phase waited for a slot
                rec["state"] = f"held before p{k}: {res['held']}"
                D.save_state(st)
                return rec
        ex = res.get("extra") or {}
        for row in ex.get("auto_compactions") or []:
            if not any(c.get("file") == row["file"] for c in rec.get("compactions", [])):
                log_compaction(st, chain, {"phase": k, **row}, root)
        if res["end"] in HALTING:
            rec["state"] = f"halted p{k}: {res['end']}"
            D.save_state(st)
            if res["end"] == SE.END_STAGE_CAP:
                D.halt(st, "chains", f"stage cap refused a call in {chain.id} p{k}")
            D.status(f"chain {chain.id} p{k}: {res['end']}; halted")
            return rec
        if grade and res.get("traps_home"):
            rec["state"] = f"grading p{k}"
            D.save_state(st)
            D.grade_run(out, task, "same_as_tip")
        row = score_phase(out, chain, k)
        if stop_rule is not None:
            stop_rule.count(st, chain, k, row)
        spent = D._attempt_spend(out)
        with D._LOCK:
            rec["phase_runs"].setdefault(str(k), {}).update({
                "dir": str(out), "end": res["end"], "keep_tag": res.get("keep_tag"),
                "cost_usd": round(spent if spent is not None else res["cost_usd"], 6),
                "rewrite": row["rewrite"], "rejection": row["rejection"],
                "container": ex.get("container")})
        if res.get("keep_tag"):
            D.manifest({"kind": "P", "id": f"{chain.id}-p{k}", "event": "done",
                        "tag": res["keep_tag"], "messages": str(out / "messages.json"),
                        "traps_home": res.get("traps_home"), "chain": chain.id, "position": k},
                       root)
        D.save_state(st)
        D.status(f"chain {chain.id} p{k}: {res['end']}, {res['n_calls']} calls, "
                 f"rejection {row['rejection']}, rewrite {row['rewrite']}")
        if ex.get("last_summary"):
            summary = Summary(**ex["last_summary"])
        prev = {"keep_tag": res.get("keep_tag"), "traps_home": res.get("traps_home"),
                "task": ph.task}
        end = k == n
        if end and not saves_end(s, chain):
            break
        msgs = json.loads((out / "messages.json").read_text())
        if s.boundary == "full":
            prefix = msgs
            continue
        kind = "end" if end else "boundary"
        cp = out / "compaction.json"
        if cp.exists():
            comp = json.loads(cp.read_text())  # an earlier process finished it
            if not any(c.get("file") == str(cp) for c in rec.get("compactions", [])):
                log_compaction(st, chain, {
                    "kind": kind, "boundary": k, "after_phase": k,
                    "pre_tokens_est": comp["pre_tokens_est"],
                    "post_tokens_est": comp["post_tokens_est"], "update": comp["update"],
                    "cost_usd": comp["cost_usd"], "file": str(cp), "recovered": True}, root)
        else:
            if D.stop_requested():
                raise L.Stopped("stop requested")
            cmodel = (model_for("compaction", out)[0] if model_for else
                      # its own budget: a boundary compaction belongs to neither task's run
                      D.paid_model(route, out / "compaction_spend", s.run_usd, spend)[0])
            use_prev = summary if k >= s.update_prompt_from_boundary else None
            try:
                comp = compact_messages(msgs, cmodel, f"{chain.id}:b{k}", use_prev)
            except StageCapReached:
                D.halt(st, "chains", f"stage cap refused boundary {k} of {chain.id}")
            except Exception as e:  # noqa: BLE001
                rec["state"] = f"halted b{k}: compaction failed"
                D.save_state(st)
                D.status(f"chain {chain.id} boundary {k}: compaction failed: {str(e)[:200]}")
                return rec
            SE.dump(cp, comp)
            log_compaction(st, chain, {
                "kind": kind, "boundary": k, "after_phase": k,
                "pre_tokens_est": comp["pre_tokens_est"],
                "post_tokens_est": comp["post_tokens_est"], "update": comp["update"],
                "cost_usd": comp["cost_usd"], "file": str(cp)}, root)
        prefix = comp["post_messages"]
        summary = Summary(comp["summary"], comp["summary_index"])
        if needs_state(s, chain, k):
            publish_state(rec, chain, k, kind, comp, cp, res, out, root, fingerprint)
            D.save_state(st)
        if end:
            break
    rec["state"] = "done"
    D.save_state(st)
    return rec


# ------------------------------------------------------------------------------------------
# Saved session states (experiment 4: the exploitation ending forks from them)
# ------------------------------------------------------------------------------------------


def saves_end(s: Settings, chain: Chain) -> bool:
    """A chain-end state (one more boundary compaction after the last task) is saved for every
    chain with a boundary, i.e. not for 1-phase fresh controls."""
    return (SAVE_END in s.save_state_after and s.boundary == "compacted"
            and chain.arm not in TEAMMATE_FIRST_ARMS)


def needs_state(s: Settings, chain: Chain, k: int) -> bool:
    """Whether a saved state follows phase k (and so its ending must be kept): after every
    boundary compaction, and at the end of chains that save an end state. Fresh controls never
    (astra r1 m9: no kept image for them)."""
    if s.boundary != "compacted" or chain.arm in TEAMMATE_FIRST_ARMS:
        return False
    return saves_end(s, chain) if k == len(chain.phases) else \
        SAVE_BOUNDARY in s.save_state_after


def states_dir(chain_id: str, root: Path | None = None) -> Path:
    return chain_dir(chain_id, root) / "states"


def image_exists(tag: str) -> bool:
    """`docker image inspect` (tests replace this)."""
    return D.image_id(tag) is not None


def state_evidence_missing(res: dict) -> str | None:
    """Why a state after this phase can't be published, or None: its kept image must exist (the
    workspace and remote a fork starts from) and its scorer home must be on the host."""
    tag, home = res.get("keep_tag"), res.get("traps_home")
    if not tag:
        ev = ((res.get("extra") or {}).get("evidence_error") or "")[-300:]
        return f"no kept snapshot of the ending{': ' + ev if ev else ''}"
    if not image_exists(tag):
        return f"kept image {tag} not found"
    if not home or not Path(home).is_dir():
        return f"scorer home {home} not found"
    return None


def publish_state(rec: dict, chain: Chain, k: int, kind: str, comp: dict, cp: Path, res: dict,
                  out: Path, root: Path | None, fingerprint: dict | None) -> Path | None:
    """Save the state after phase k when its evidence is complete; otherwise record it under
    the chain's `state_missing` with the reason and go on (the agent's outcome stands; the
    exploitation ending can't fork that state). A later run repairs it when the evidence turns
    up (a resumed chain passes every boundary again; a done chain runs `repair_states`). A
    phase whose kept snapshot failed can't be repaired: its container is gone."""
    key = f"after_task{k}"
    why = state_evidence_missing(res)
    with D._LOCK:
        miss = rec.setdefault("state_missing", {})
        if why is not None:
            miss[key] = {"reason": why, "kind": kind, "phase_dir": str(out),
                         "compaction": str(cp), "utc": D.utc()}
            D.status(f"chain {chain.id}: state {key} not saved: {why}")
            return None
        miss.pop(key, None)
    d = save_state(chain, k, kind, comp, cp, res, out, root, fingerprint)
    with D._LOCK:
        rec["phase_runs"].setdefault(str(k), {})["state_dir"] = str(d)
    return d


def repair_states(rec: dict, chain: Chain, s: Settings, root: Path | None,
                  fingerprint: dict | None) -> list[str]:
    """Publish every missing state whose phase result and compaction are on disk and whose
    evidence is now complete. Returns the keys repaired."""
    done = []
    for key, m in sorted((rec.get("state_missing") or {}).items()):
        k = int(key.removeprefix("after_task"))
        out, cp = Path(m["phase_dir"]), Path(m["compaction"])
        if not (out / "result.json").exists() or not cp.exists():
            continue
        res = json.loads((out / "result.json").read_text())
        comp = json.loads(cp.read_text())
        if publish_state(rec, chain, k, m["kind"], comp, cp, res, out, root,
                         fingerprint) is not None:
            done.append(key)
    return done


def save_state(chain: Chain, k: int, kind: str, comp: dict, cp: Path, res: dict, out: Path,
               root: Path | None, fingerprint: dict | None) -> Path:
    """`states/after_task<k>/state.json`: the context the next task starts from (the
    post-compaction messages), the compaction record, pi's summary and its index, the kept
    image of task k's ending (workspace and remote), its scorer home and reasoning log, and the
    config fingerprint. `states/index.jsonl` is then rebuilt from the saved states."""
    d = states_dir(chain.id, root) / f"after_task{k}"
    state = {
        "chain_id": chain.id, "arm": chain.arm, "phases": [p.task for p in chain.phases],
        "after_task": k, "kind": kind, "task": chain.phases[k - 1].task,
        "messages": comp["post_messages"],
        "compaction": {"summary": comp["summary"], "summary_index": comp["summary_index"],
                       "tail": comp.get("kept_indices"), "pre_tokens_est": comp["pre_tokens_est"],
                       "post_tokens_est": comp["post_tokens_est"], "update": comp["update"],
                       "file": str(cp)},
        "summary": {"text": comp["summary"], "index": comp["summary_index"]},
        "keep_tag": res.get("keep_tag"), "traps_home": res.get("traps_home"),
        "reasoning": str(Path(out) / "reasoning.jsonl"), "phase_dir": str(out),
        "phase_end": res.get("end"), "fingerprint": fingerprint, "utc": D.utc()}
    SE.dump(d / "state.json", state)
    rebuild_index(d.parent)
    return d


def rebuild_index(sd: Path) -> list[dict]:
    """`index.jsonl` written atomically from the saved `after_task*/state.json` files (astra r1
    m6: never appended, so a crash can't leave a torn line)."""
    rows = []
    for f in Path(sd).glob("after_task*/state.json"):
        try:
            x = json.loads(f.read_text())
        except ValueError:
            continue
        rows.append({"after_task": x["after_task"], "kind": x["kind"], "task": x["task"],
                     "dir": str(f.parent), "keep_tag": x["keep_tag"], "utc": x["utc"]})
    rows.sort(key=lambda r: r["after_task"])
    tmp = Path(sd) / "index.jsonl.tmp"
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(Path(sd) / "index.jsonl")
    return rows


# ------------------------------------------------------------------------------------------
# Admission: the scorer stop rule and arm order (experiment 4)
# ------------------------------------------------------------------------------------------


@dataclass
class StopRule:
    """Plan "Gates": stop and report if, among the first `first_n` phases of `arm` to finish,
    the scorer's status is unknown in more than `max_share` of them. The aggregate lives in the
    state (`st["stop_rule"]`); once triggered, no new phase is admitted (running phases finish;
    a phase waiting for its slot is held when it gets one) and the launch exits nonzero.

    "Unknown" is the scorer's completeness, whatever it found: a phase counts as unknown when
    `trap_status` (score.trap_fields: the trap scorer's status over the bstart -> final
    bundle) is anything but "complete" ("incomplete" when some evidence is missing, "missing"
    when the scorer home is gone), or when `rewrite` is "unknown". A rewrite seen ("yes") in an
    incomplete bundle still counts as unknown: the scorer couldn't check the rest."""

    arm: str = "unrelated"
    first_n: int = 15
    max_share: float = 0.10

    def count(self, st: dict, chain: Chain, k: int, row: dict) -> None:
        if chain.arm != self.arm:
            return
        with D._LOCK:
            agg = st.setdefault("stop_rule", {"arm": self.arm, "first_n": self.first_n,
                                              "max_share": self.max_share, "phases": [],
                                              "triggered": None})
            key = f"{chain.id}-p{k}"
            if len(agg["phases"]) >= self.first_n or any(p["id"] == key
                                                         for p in agg["phases"]):
                return
            agg["phases"].append({"id": key, "unknown": scorer_unknown(row),
                                  "trap_status": row.get("trap_status"),
                                  "rewrite": row.get("rewrite")})
            n_unknown = sum(p["unknown"] for p in agg["phases"])
            agg["unknown"] = n_unknown
            if len(agg["phases"]) == self.first_n and n_unknown > self.max_share * self.first_n:
                agg["triggered"] = {"utc": D.utc(), "unknown": n_unknown, "of": self.first_n}
                st["halted"] = {"step": "stop rule", "utc": D.utc(), "reason":
                                f"scorer status unknown in {n_unknown} of the first "
                                f"{self.first_n} {self.arm} tasks (> {self.max_share:.0%})"}
        if (st.get("stop_rule") or {}).get("triggered"):
            D.save_state(st)
            D.status(f"STOP RULE: {st['halted']['reason']}; no new phase is admitted, running "
                     "phases finish; report to Jacob")


def scorer_unknown(row: dict) -> bool:
    """StopRule's "unknown" (see its docstring)."""
    return row.get("trap_status") != "complete" or row.get("rewrite") == "unknown"


def admission_held(st: dict) -> str | None:
    """Why no new phase may start (the stop rule triggered), or None."""
    t = (st.get("stop_rule") or {}).get("triggered")
    if t and not (st.get("stop_rule") or {}).get("overridden"):
        return f"stop rule: scorer status unknown in {t['unknown']} of {t['of']}"
    return None


def read_state(state_dir: Path) -> dict:
    return json.loads((Path(state_dir) / "state.json").read_text())


def load_state(state_dir: Path) -> tuple[list[dict], Summary, dict]:
    """(prefix, summary, prev) for `run_phase`: the post-compaction context, pi's summary (for
    the update prompt) and {"keep_tag", "traps_home", "task"} of the saved task's ending."""
    st = read_state(state_dir)
    return (list(st["messages"]), Summary(st["summary"]["text"], st["summary"]["index"]),
            {"keep_tag": st["keep_tag"], "traps_home": st["traps_home"], "task": st["task"]})


def fork_from_state(state_dir: Path, task_id: str, run_id: str | None = None
                    ) -> tuple[Chain, int, list[dict], dict, Summary]:
    """(chain, k, prefix, prev, summary) for one further task after a saved state: the chain's
    first k-1 tasks are the saved chain's, the k-th is `task_id` (a container switch); the chain
    is named `run_id` (default `<chain>-from<after_task>`) and keeps the saved arm."""
    st = read_state(state_dir)
    prefix, summary, prev = load_state(state_dir)
    n = st["after_task"]
    phases = tuple(Phase(t, "switch") for t in st["phases"][:n]) + (Phase(task_id, "switch"),)
    return (Chain(run_id or f"{st['chain_id']}-from{n}", st["arm"], phases), n + 1, prefix,
            prev, summary)


def run_extra_phase(state_dir: Path, task: TaskInfo, setup: PushSetup, model: ChatModel,
                    inv: Invocation, out: Path, s: Settings, *, run_id: str | None = None,
                    opening_addendum: str | None = None, **kw) -> SE.RunResult:
    """One further task from a saved state with a model and invocation the caller gives
    (smokes, tests): exactly as positions 2..N start (a container switch, the teammate draft,
    the lead-in, the tag line, then the optional addendum). Paid runs use
    `run_phase_under_stage(*fork_from_state(...)...)` instead, which spends under the stage.
    `kw` goes to run_phase. The caller cleans up `inv` and scores."""
    chain, k, prefix, prev, summary = fork_from_state(state_dir, task.task_id, run_id)
    return run_phase(chain, k, task, setup, prefix, prev, summary, model, inv, out, s,
                     opening_addendum=opening_addendum, **kw)


def run_chains(chains: list[Chain], st: dict, route, spend, tasks_: dict, s: Settings, *,
               concurrency: int, model_for: Callable | None = None, grade: bool = True,
               root: Path | None = None, fingerprint: dict | None = None,
               after: dict[str, list[str]] | None = None,
               stop_rule: StopRule | None = None) -> dict:
    """Chains in parallel, started in list order as slots free (memory admission per phase
    decides how many containers run at once). `after[chain id]` lists chains that must be done
    (in the state) before that chain starts: it waits for those launched here, then starts only
    if all are done, else it is held (reported, not run). A Halt in any chain is raised at the
    end."""
    out: dict = {}
    after = after or {}

    def one(c: Chain):
        try:
            out[c.id] = run_chain(c, st, route, spend, tasks_, s, model_for, grade, root,
                                  fingerprint=fingerprint, stop_rule=stop_rule)["state"]
        except L.Stopped as e:
            out[c.id] = f"stopped: {e}"
    free = [c for c in chains if c.id not in after]
    held = [c for c in chains if c.id in after]
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max(1, concurrency)) as ex:
        futs = {c.id: ex.submit(one, c) for c in free}
        for c in held:
            for pid in after[c.id]:
                if pid in futs:
                    try:
                        futs[pid].result()
                    except BaseException as e:  # noqa: BLE001 - raised below
                        errors.append(e)
            not_done = [pid for pid in after[c.id]
                        if ((st.get("chains") or {}).get(pid) or {}).get("state") != "done"]
            if not_done:
                out[c.id] = f"held: waits for {not_done}"
                D.status(f"chain {c.id} held: its arm starts only after {not_done} are done")
                continue
            futs[c.id] = ex.submit(one, c)
    for f in futs.values():
        try:
            f.result()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    if errors:
        raise errors[0]
    return out
