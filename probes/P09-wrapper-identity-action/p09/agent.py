"""P09 agent episodes, ported from P07 (`probes/P07-compaction-trust/p07/agent.py`; plan.md
"Setup"): rebuild the workspace at the cut point, make the content plant (plants.py), resume from the compacted context, and run X3's loop for at most
`C.RESUME_TURN_CAP` turns.

Reuses X3's Inspect loop pieces by import (src/x3/task.py: snapshots, the command runner, the
byte-bounded cost check, retries; src/x3/runner.py: billed cost from the .eval log's model
events) and X3b's eval call (src/x3b/runner.py `eval_block`: OpenRouter provider routing,
max_retries=0, log_model_api=True, ctl_server=False). X3's own probes (pre/post questions) are
not run here.

One Inspect sample is one episode; its metadata is an `EpisodeSpec`. Per sample:
1. `p09_setup` writes the replayed workspace, checks every path's sha256 against the X3b
   snapshot at the cut, writes the plant (T: the test file; H: solution.py), checks it in the
   box (sha256; T: the deleted assert absent; H: the conflicting call returns the conflicting
   value) and in the context (`cells.context_check`), takes snapshot 0 (the post-plant state)
   and rebuilds the resumed messages;
2. `p09_episode` runs the loop (skipped, with end_reason "replay_mismatch" or
   "plant_check_failed", when the setup checks fail: such an episode would not test the content);
3. `collect` stores the final solution.py and test_solution.py.

`run_batch` runs one Inspect eval over a list of specs; `extract` turns a .eval log into one
episode artifact per sample (contracts.py schema), never overwriting an existing file.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
)
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, Solver, TaskState, chain, solver
from inspect_ai.tool import ToolCall
from inspect_ai.util import sandbox, store

from p09 import contracts as C
from x3 import contracts as X3C
from x3 import texts as X3T
from x3.task import (
    ATTEMPTS,
    TOOLS,
    _run_command,
    affordable_max_tokens,
    call_bound,
    call_cost,
    generate_with_retries,
    input_bound_tokens,
    take_snapshot,
    tighter_bound,
    usage_tokens,
)

SOLUTION_PATH = f"{X3C.WORKDIR}/solution.py"
TEST_PATH = f"{X3C.WORKDIR}/test_solution.py"
MOCK_MODEL = "mockllm/model"

# Inspect store keys (per sample)
STORE_SETUP = "p09_setup"
STORE_PLANT_CHECK = "p09_plant_check"
STORE_CALLS = "p09_calls"
STORE_SNAPSHOTS = "p09_snapshots"
STORE_EPISODE = "p09_episode"
STORE_FINAL_SOLUTION = "p09_final_solution_py"
STORE_FINAL_TEST = "p09_final_test_solution_py"
STORE_COLLECT_ERRORS = "p09_collect_errors"
STORE_API_ERROR = "p09_api_error"

END_REPLAY_MISMATCH = "replay_mismatch"
END_PLANT_FAILED = "plant_check_failed"


# --- the per-episode spec --------------------------------------------------------------------
@dataclass(frozen=True)
class EpisodeSpec:
    episode_id: str
    ctx_id: str
    task_id: str
    content: str
    wrapper: str
    sample: int
    model_slug: str
    model_id: str
    in_ceiling: float                 # $/M tokens
    out_ceiling: float
    episode_limit_usd: float
    plant_sentence: str | None
    files: dict = field(default_factory=dict)            # {path: {"text", "sha256"}}
    snapshot_sha256: dict = field(default_factory=dict)  # {path: sha256}
    plant: dict | None = None                            # plants.Plant.as_dict()
    initial_messages: list = field(default_factory=list)  # Inspect ChatMessage model_dumps


def spec_from(ctx: dict, resume: C.Resume, model: C.Model, sample: int = 0) -> EpisodeSpec:
    """An episode spec from a context record (P07's contracts) and its resumed context."""
    eid = C.EPISODE_ID_FMT.format(model_slug=model.slug, wrapper=resume.wrapper,
                                  content=resume.content, ctx_id=ctx["ctx_id"], sample=sample)
    return EpisodeSpec(
        episode_id=eid, ctx_id=ctx["ctx_id"], task_id=ctx["task_id"], content=resume.content,
        wrapper=resume.wrapper, sample=sample, model_slug=model.slug,
        model_id=model.model_id, in_ceiling=model.in_ceiling, out_ceiling=model.out_ceiling,
        episode_limit_usd=model.episode_limit_usd, plant_sentence=resume.plant_sentence,
        files=ctx["files"], snapshot_sha256=ctx["snapshot_sha256"], plant=resume.plant,
        initial_messages=list(resume.messages))


def _spec(state: TaskState) -> EpisodeSpec:
    return EpisodeSpec(**state.metadata)


def resume_of(spec: EpisodeSpec) -> C.Resume:
    return C.Resume(messages=tuple(spec.initial_messages), content=spec.content,
                    wrapper=spec.wrapper, plant_sentence=spec.plant_sentence, plant=spec.plant)


_MSG_CLASS = {"system": ChatMessageSystem, "user": ChatMessageUser,
              "assistant": ChatMessageAssistant, "tool": ChatMessageTool}


def to_messages(dumps: list[dict]) -> list[ChatMessage]:
    """Inspect ChatMessage model_dumps -> ChatMessage objects, by role."""
    return [_MSG_CLASS[d["role"]].model_validate(d) for d in dumps]


# --- sandbox helpers -------------------------------------------------------------------------
_SHA_PY = r"""
import hashlib, json, sys
out = {}
for p in json.loads(sys.argv[1]):
    try:
        out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    except OSError:
        out[p] = None
print(json.dumps(out))
"""


async def _sha256_in_box(paths: list[str]) -> dict:
    r = await sandbox().exec(["python3", "-c", _SHA_PY, json.dumps(paths)], user="root")
    if r.returncode != 0:
        raise RuntimeError(f"sha256 check failed: rc={r.returncode} {r.stderr[-300:]}")
    return json.loads(r.stdout)


async def _read(path: str) -> str | None:
    """A file's text read as root, or None when it does not exist."""
    r = await sandbox().exec(["cat", path], user="root")
    return r.stdout if r.returncode == 0 else None


async def _chown() -> None:
    owner = f"{X3C.AGENT_USER}:{X3C.AGENT_USER}"
    r = await sandbox().exec(["chown", "-R", owner, X3C.WORKDIR, X3C.HOME], user="root")
    if r.returncode != 0:
        raise RuntimeError(f"chown failed: {r.stderr[-300:]}")


def replay_compare(written: dict, expected: dict) -> list[str]:
    """Paths whose in-container sha256 differs from the snapshot's (missing counts as differing),
    over the union of the snapshot's paths and the written ones."""
    return sorted(p for p in set(expected) | set(written) if written.get(p) != expected.get(p))


# --- solvers ---------------------------------------------------------------------------------
@solver
def p09_setup() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        spec = _spec(state)
        box = sandbox()
        # 1. the replayed workspace, byte-exact (bytes go through base64, no text re-encoding)
        for path, f in sorted(spec.files.items()):
            parent = path.rsplit("/", 1)[0] or "/"
            await box.exec(["mkdir", "-p", parent], user="root")
            await box.write_file(path, f["text"].encode("utf-8"))
        await _chown()
        # 2. the rebuilt sandbox against the X3b snapshot's sha256 at the cut
        paths = sorted(set(spec.snapshot_sha256) | set(spec.files))
        written = await _sha256_in_box(paths)
        mismatched = replay_compare(written, spec.snapshot_sha256)
        # 3. the plant, then its checks in the box and in the context
        pl = spec.plant
        probs: list[str] = []
        box_check: dict = {}
        if pl is not None:
            await box.write_file(pl["path"], pl["text"].encode("utf-8"))
            await _chown()
            box_check["sha256"] = (await _sha256_in_box([pl["path"]]))[pl["path"]]
            if box_check["sha256"] != pl["sha256"]:
                probs.append("planted file sha256 mismatch in the box")
            now = await _read(pl["path"]) or ""
            if pl["content"] == "T" and pl["removed_stmt"] in now:
                probs.append("T: the deleted assert is still in the test file")
            if pl["content"] == "H":
                from p09 import plants

                r = await sandbox().exec(plants.h_check_command(pl), user=X3C.AGENT_USER,
                                         cwd=X3C.WORKDIR)
                box_check["h_call_rc"] = r.returncode
                if r.returncode != 0:
                    probs.append(f"H: conflicting call rc={r.returncode} {r.stderr[-200:]}")
        from p09 import cells

        cc = cells.context_check(resume_of(spec))
        probs += cc["problems"]
        setup = {"replay_match": not mismatched, "mismatched": mismatched,
                 "written_sha256": written, "planted": pl, "box_check": box_check}
        pc = {"ok": not probs, "problems": probs, "sentence_count": cc["sentence_count"]}
        s = store()
        s.set(STORE_SETUP, setup)
        s.set(STORE_PLANT_CHECK, pc)
        s.set(STORE_SNAPSHOTS, [await take_snapshot(0)])
        s.set(STORE_CALLS, [])
        state.messages = to_messages(spec.initial_messages)
        return state

    return solve


@solver
def p09_episode() -> Solver:
    """X3's loop (x3.task.episode) without its probes: the per-turn cost bound against the
    episode limit at the model's ceiling prices, one command per turn, the frozen nudge for
    turns without a tool call, `done` ends, and RESUME_TURN_CAP turns at most."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        spec = _spec(state)
        s = store()
        calls, snapshots = s.get(STORE_CALLS, []), s.get(STORE_SNAPSHOTS, [])
        messages: list[ChatMessage] = list(state.messages)
        spent, turns, nudges, multi = 0.0, 0, 0, 0
        prev: tuple[int, int, int] | None = None
        end_reason: str | None = None
        bounds: list[dict] = []
        if not (s.get(STORE_SETUP) or {}).get("replay_match"):
            end_reason = END_REPLAY_MISMATCH
        elif not (s.get(STORE_PLANT_CHECK) or {}).get("ok"):
            end_reason = END_PLANT_FAILED
        while end_reason is None:
            if turns >= C.RESUME_TURN_CAP:
                end_reason = X3C.EndReason.TURN_CAP.value
                break
            ib = tighter_bound(input_bound_tokens(messages, TOOLS), prev, messages)
            mt = affordable_max_tokens(spent, spec.episode_limit_usd, ib, spec.in_ceiling,
                                       spec.out_ceiling, X3C.MAX_TOKENS_PER_TURN)
            bounds.append({"turn": turns + 1, "input_bound": ib, "max_tokens": mt,
                           "spent": spent})
            if mt < X3C.MIN_CALL_MAX_TOKENS:
                end_reason = X3C.EndReason.COST_CAP.value
                break
            one = call_bound(ib, mt, spec.in_ceiling, spec.out_ceiling) / ATTEMPTS
            out, extra, err = await generate_with_retries(messages, TOOLS, mt, one)
            spent += extra
            bounds[-1]["failed_charge"] = extra
            if out is None:
                end_reason = X3C.EndReason.API_ERROR.value
                s.set(STORE_API_ERROR, err)
                break
            turns += 1
            spent += call_cost(out, spec.in_ceiling, spec.out_ceiling, one)
            msg = out.message
            messages.append(msg)
            ut = usage_tokens(out)
            prev = (ut[0], ut[1], len(messages)) if ut else None
            if out.stop_reason in ("model_length", "content_filter"):
                end_reason = out.stop_reason
                break
            tcs: list[ToolCall] = list(msg.tool_calls or [])
            if not tcs:
                nudges += 1
                messages.append(ChatMessageUser(content=X3T.NUDGE))
                continue
            first, rest = tcs[0], tcs[1:]
            if first.function == "done":
                result = ""
                end_reason = X3C.EndReason.DONE.value
            elif first.function == "execute_command":
                cmd = first.arguments.get("command")
                if not isinstance(cmd, str):
                    result = "error: execute_command needs a string argument `command`"
                else:
                    result = await _run_command(cmd, turns, calls, snapshots)
            else:
                result = f"error: unknown tool {first.function!r}"
            messages.append(ChatMessageTool(content=result, tool_call_id=first.id,
                                            function=first.function))
            for tc in rest:
                multi += 1
                messages.append(ChatMessageTool(content=X3T.MULTI_CALL_RESULT,
                                                tool_call_id=tc.id, function=tc.function))
        s.set(STORE_CALLS, calls)
        s.set(STORE_SNAPSHOTS, snapshots)
        n_init = len(spec.initial_messages)
        final_asst = next((m for m in reversed(messages[n_init:])
                           if isinstance(m, ChatMessageAssistant)), None)
        s.set(STORE_EPISODE, {
            "messages": [m.model_dump(exclude_none=True) for m in messages],
            "end_reason": end_reason, "n_turns": turns, "n_nudges": nudges,
            "n_multi_call": multi, "spent_at_ceiling": spent, "bounds": bounds,
            "final_message": final_asst.text if final_asst else ""})
        state.messages = messages
        state.completed = True
        return state

    return solve


@scorer(metrics=[mean()])
def collect() -> object:
    """Store the final solution.py and test_solution.py; scoring proper is offline."""
    async def score(state: TaskState, target: Target) -> Score:
        s = store()
        errors: list[str] = []
        for key, path in ((STORE_FINAL_SOLUTION, SOLUTION_PATH), (STORE_FINAL_TEST, TEST_PATH)):
            try:
                text = await _read(path)
                if text is None:
                    errors.append(f"could not read {path}")
            except (TimeoutError, OSError, RuntimeError, UnicodeDecodeError) as e:
                text = None
                errors.append(f"could not read {path}: {e!r}")
            s.set(key, text)
        ep = s.get(STORE_EPISODE) or {}
        if not ep:
            errors.append("episode record missing")
        if any(sn.get("files") is None for sn in s.get(STORE_SNAPSHOTS, [])):
            errors.append("a snapshot failed")
        s.set(STORE_COLLECT_ERRORS, errors)
        return Score(value=0, answer=str(ep.get("end_reason")),
                     metadata={"n_turns": ep.get("n_turns"), "errors": errors})

    return score


def p09_task(specs: list[EpisodeSpec]) -> Task:
    if not specs:
        raise ValueError("empty batch")
    ids = [sp.episode_id for sp in specs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate episode ids in the batch")
    samples = [Sample(id=sp.episode_id, input="(set in setup)", metadata=asdict(sp))
               for sp in specs]
    return Task(dataset=samples, solver=chain(p09_setup(), p09_episode()), scorer=collect(),
                sandbox=("docker", str(X3C.COMPOSE_FILE)),
                config=GenerateConfig(parallel_tool_calls=False,
                                      max_tool_output=X3C.OUTPUT_TRUNCATE_CHARS + 1024))


# --- running ---------------------------------------------------------------------------------
def as_mock(spec: EpisodeSpec) -> EpisodeSpec:
    """A spec re-labelled for a mockllm walk: model_id mockllm/model (billed 0 at extraction),
    slug "mock", so its artifacts never land under a real model's directory."""
    eid = C.EPISODE_ID_FMT.format(model_slug="mock", wrapper=spec.wrapper,
                                  content=spec.content, ctx_id=spec.ctx_id, sample=spec.sample)
    return replace(spec, model_id=MOCK_MODEL, model_slug="mock", episode_id=eid)


def generate_settings(model: C.Model) -> dict:
    """X3's common request settings plus the model's own (e.g. DeepSeek reasoning_effort),
    restricted to GenerateConfig fields (as x3b.runner.eval_block passes them)."""
    gc = {**X3C.COMMON_GENERATE, **model.generate}
    return {k: v for k, v in gc.items() if k in GenerateConfig.model_fields}


def run_batch(specs: list[EpisodeSpec], model: C.Model, log_dir: Path, max_samples: int = 24,
              mock: bool = False, model_obj=None) -> list[Path]:
    """One Inspect eval over `specs`; returns the new .eval log paths. Paid unless `mock`.
    `model_obj` (tests only) is an Inspect Model, e.g. a mockllm with custom outputs; it
    implies `mock`."""
    from inspect_ai import eval as inspect_eval

    mock = mock or model_obj is not None
    if mock:
        specs = [as_mock(sp) for sp in specs]
    else:
        if model.model_id not in X3C.MODELS:
            # x3.runner.model_calls_from_events prices calls with x3's model table
            raise ValueError(f"{model.model_id} is not in x3.contracts.MODELS")
        if any(sp.model_id != model.model_id for sp in specs):
            raise ValueError("every spec must be for the batch's model")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    before = set(log_dir.glob("*.eval"))
    inspect_eval(p09_task(specs),
                 model=model_obj or (MOCK_MODEL if mock else f"openrouter/{model.model_id}"),
                 model_args={} if mock else {"provider": model.provider, "max_retries": 0},
                 log_dir=str(log_dir), max_samples=max_samples, display="plain",
                 fail_on_error=False, log_model_api=True,
                 ctl_server=False,   # no live max_retries overrides (X3 code review r3, 1)
                 **({} if mock else generate_settings(model)))
    return sorted(set(log_dir.glob("*.eval")) - before)


# --- extraction ------------------------------------------------------------------------------
def billed_total(model_calls: list[dict]) -> float | None:
    """Sum of billed cost; None when any successful call lacks one (cost then unknown)."""
    if any(c.get("error") is None and c.get("billed_usd") is None for c in model_calls):
        return None
    return sum(c["billed_usd"] for c in model_calls if c.get("billed_usd") is not None)


def _write_new(art: dict, path: Path) -> Path:
    """Write without overwriting: an existing file gets a .dupN sibling, as X3 does."""
    base, p, n = path, path, 1
    while p.exists():
        p = base.with_name(f"{base.stem}.dup{n}.json")
        art["duplicate_of"] = str(base)
        n += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(art, indent=1, default=str))
    return p


def extract(eval_path: Path, *, write: bool = True,
            episodes_dir: Path = C.EPISODES_DIR) -> list[dict]:
    """One episode artifact per sample (failed samples too, with `error` set)."""
    import inspect_ai
    from inspect_ai.log import read_eval_log

    from x3.runner import _git_sha, model_calls_from_events

    log = read_eval_log(str(eval_path))
    git_sha = _git_sha()
    out = []
    for s in log.samples or []:
        st = s.store or {}
        md = s.metadata or {}
        ep = st.get(STORE_EPISODE) or {}
        meta = {"episode_id": md.get("episode_id"), "ctx_id": md.get("ctx_id"),
                "content": md.get("content"), "wrapper": md.get("wrapper"),
                "sample": md.get("sample"),
                "model": md.get("model_id"), "model_slug": md.get("model_slug"),
                "task_id": md.get("task_id"), "plant_sentence": md.get("plant_sentence"),
                "git_sha": git_sha,
                "inspect_version": inspect_ai.__version__}
        model_calls = model_calls_from_events(s.events, md.get("model_id", ""))
        initial = md.get("initial_messages") or []
        err = repr(s.error)[:2000] if s.error else (None if ep else "episode record missing")
        art = {"meta": meta, "initial_messages": initial, "messages": ep.get("messages"),
               "n_initial": len(initial),
               "calls": st.get(STORE_CALLS), "snapshots": st.get(STORE_SNAPSHOTS),
               "setup": st.get(STORE_SETUP), "plant_check": st.get(STORE_PLANT_CHECK),
               "end_reason": ep.get("end_reason"), "n_turns": ep.get("n_turns"),
               "n_nudges": ep.get("n_nudges"), "n_multi_call": ep.get("n_multi_call"),
               "final_message": ep.get("final_message"),
               "final_solution_py": st.get(STORE_FINAL_SOLUTION),
               "final_test_solution_py": st.get(STORE_FINAL_TEST),
               "spent_at_ceiling": ep.get("spent_at_ceiling"), "bounds": ep.get("bounds"),
               "api_error": st.get(STORE_API_ERROR),
               "collect_errors": st.get(STORE_COLLECT_ERRORS),
               "model_calls": model_calls, "billed_usd": billed_total(model_calls),
               "error": err, "eval_log": str(eval_path)}
        if write and md.get("episode_id"):
            rel = C.episode_path(md["model_slug"], md["wrapper"], md["content"],
                                 md["ctx_id"], md["sample"]).relative_to(C.EPISODES_DIR)
            art["path"] = str(_write_new(art, Path(episodes_dir) / rel))
        out.append(art)
    return out


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
