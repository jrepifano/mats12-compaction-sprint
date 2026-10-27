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
  endings (`fork_after_every_phase`).

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
from dataclasses import asdict, dataclass, field
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


def push_setup(task_id: str) -> PushSetup:
    """Task chaining's four tasks (httpx and tengo). Other tasks need their own push setup, an
    experiment-4 build item."""
    r = CC.REPO_OF_TASK.get(task_id)
    if r is None:
        raise KeyError(f"no push setup for {task_id}")
    return PushSetup(task_id, r.base, "feature/" + task_id.split("-", 1)[1],
                     r.a_feature if task_id == r.a_task else r.b_feature, r.junk_file,
                     r.teammate_file, r.teammate_text)


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
    return Settings(boundary=ch["boundary"], auto_compact_tokens=ch["auto_compact_tokens"],
                    update_prompt_from_boundary=ch["update_prompt_from_boundary"],
                    fork_after_every_phase=ch["fork_after_every_phase"],
                    max_calls=caps["max_calls"], max_seconds=caps["max_seconds"],
                    run_usd=caps["run_usd"], task_ending=_wording(cfg, "task_ending"),
                    lead_same=_wording(cfg, "lead_same"),
                    lead_different=_wording(cfg, "lead_different"))


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


def run_phase(chain: Chain, k: int, task: TaskInfo, setup: PushSetup, prefix: list[dict] | None,
              prev: dict | None, summary: Summary | None, model: ChatModel, inv: Invocation,
              out: Path, s: Settings, *, compactor_for: Callable[[int], ChatModel] | None = None,
              stop=None, on_keep=None, probe_visibility: bool = False) -> SE.RunResult:
    """Phase k (1-based) of `chain`. `prefix` is the carried context (None for phase 1); `prev`
    is the previous phase's {"keep_tag", "traps_home", "task"}. `result.json` is written last."""
    t0 = time.monotonic()
    ph = chain.phases[k - 1]
    res = SE.RunResult("P", f"{chain.id}-p{k}")
    env = loop = None
    agent_ended = False
    auto = None
    try:
        if k == 1:
            env, inv_tests = ChainEnv.create(inv, task, out, junk_branches(setup))
            env.trap_snapshot("bstart")
            res.extra["setup"] = {"kind": "junk", "branch": setup.branch}
            mode, image = "first", task.image
        else:
            if ph.project == "fork":
                env = ChainEnv.start_from(inv, prev["keep_tag"], task, out,
                                          home_from=Path(prev["traps_home"]))
                mode, image = "fork", prev["keep_tag"]
            else:
                env, _ = ChainEnv.create(inv, task, out, [])
                mode, image = "switch", task.image
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
        default = json.loads((env.home / "config.json").read_text())["remote"]["current_branch"]
        text = task_text(task.instruction, s.task_ending, setup.branch, default)
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
        auto = AutoCompact(s.auto_compact_tokens, compactor_for or (lambda n: model), out,
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
        if s.fork_after_every_phase:
            res.keep_tag = SE.keep_tag("p", res.id)
            SE._keep(env, res.keep_tag, {"emx.chain": "p", "emx.chain.id": res.id}, on_keep)
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
            env.close()
        res.seconds = round(time.monotonic() - t0, 3)
        SE.dump(out / "result.json", res.to_dict())
    return res


# ------------------------------------------------------------------------------------------
# Scoring one phase (code only)
# ------------------------------------------------------------------------------------------


def score_phase(out: Path, chain: Chain, k: int) -> dict:
    out = Path(out)
    res = json.loads((out / "result.json").read_text())
    ex = res.get("extra") or {}
    ph = chain.phases[k - 1]
    setup = push_setup(ph.task)
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


def run_chain(chain: Chain, st: dict, route: C.Route | None, spend, tasks_: dict, s: Settings,
              model_for: Callable | None = None, grade: bool = True, root: Path | None = None,
              probe_visibility: bool = False) -> dict:
    """Every phase of `chain` and its boundaries, resumable (see the module docstring)."""
    st.setdefault("chains", {})
    rec = D.entry(st, "chains", chain.id,
                  {"state": "pending", **chain.to_dict(), "phase_runs": {}})
    if rec["state"] == "done":
        return rec
    prefix: list[dict] | None = None
    prev: dict | None = None
    summary: Summary | None = None
    n = len(chain.phases)
    for k in range(1, n + 1):
        ph = chain.phases[k - 1]
        task = tasks_[ph.task]
        out = chain_dir(chain.id, root) / f"p{k}"
        res = D.finished(out)
        if res is not None and res["end"] in HALTING:
            rec.setdefault("set_aside", []).append(str(D.set_aside(out, res["end"])))
            res = None
        elif res is None and out.exists():
            rec.setdefault("set_aside", []).append(str(D.set_aside(out, "crashed")))
        if res is None:
            if D.stop_requested():
                raise L.Stopped("stop requested")
            rec["state"] = f"running p{k}"
            D.save_state(st)
            with D.ADMISSION.slot("agent"):
                model = (model_for(f"P{k}", out)[0] if model_for else
                         D.paid_model(route, out, s.run_usd, spend)[0])

                def compactor_for(i, out=out):
                    return (model_for("compaction", out)[0] if model_for else
                            D.paid_model(route, out / f"auto_spend{i}", s.run_usd, spend)[0])
                inv = Invocation(CC.PURPOSE)
                try:
                    r = run_phase(chain, k, task, push_setup(ph.task), prefix, prev, summary,
                                  model, inv, out, s, compactor_for=compactor_for, stop=L.STOP,
                                  on_keep=D.keep_hook("P", f"{chain.id}-p{k}", root),
                                  probe_visibility=probe_visibility)
                finally:
                    rec["phase_runs"].setdefault(str(k), {})["cleanup_unconfirmed"] = \
                        D.cleanup(inv)
            res = r.to_dict()
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
        if k == n:
            break
        msgs = json.loads((out / "messages.json").read_text())
        if s.boundary == "full":
            prefix = msgs
            continue
        cp = out / "compaction.json"
        if cp.exists():
            comp = json.loads(cp.read_text())  # an earlier process finished it
            if not any(c.get("file") == str(cp) for c in rec.get("compactions", [])):
                log_compaction(st, chain, {
                    "kind": "boundary", "boundary": k, "after_phase": k,
                    "pre_tokens_est": comp["pre_tokens_est"],
                    "post_tokens_est": comp["post_tokens_est"], "update": comp["update"],
                    "cost_usd": comp["cost_usd"], "file": str(cp), "recovered": True}, root)
        else:
            if D.stop_requested():
                raise L.Stopped("stop requested")
            cmodel = (model_for("compaction", out)[0] if model_for else
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
                "kind": "boundary", "boundary": k, "after_phase": k,
                "pre_tokens_est": comp["pre_tokens_est"],
                "post_tokens_est": comp["post_tokens_est"], "update": comp["update"],
                "cost_usd": comp["cost_usd"], "file": str(cp)}, root)
        prefix = comp["post_messages"]
        summary = Summary(comp["summary"], comp["summary_index"])
    rec["state"] = "done"
    D.save_state(st)
    return rec


def run_chains(chains: list[Chain], st: dict, route, spend, tasks_: dict, s: Settings, *,
               concurrency: int, model_for: Callable | None = None, grade: bool = True,
               root: Path | None = None) -> dict:
    """Chains in parallel (memory admission per phase decides how many run at once)."""
    out: dict = {}

    def one(c: Chain):
        try:
            out[c.id] = run_chain(c, st, route, spend, tasks_, s, model_for, grade, root)["state"]
        except L.Stopped as e:
            out[c.id] = f"stopped: {e}"
    with ThreadPoolExecutor(max(1, concurrency)) as ex:
        list(ex.map(one, chains))
    return out
