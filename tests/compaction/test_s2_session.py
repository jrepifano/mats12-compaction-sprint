"""compaction.s2.session: the compacting session loop, kept snapshots and the kept-image search.

The loop tests use a StubModel and a fake env (no Docker, $0). The two Docker tests (a kept tag
outlives the run's cleanup; the search finds strings that live only off the working tree) need
a local DeepSWE image (S2_INTERACT_TEST_IMAGE; default as in test_s2_interact.py).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess

import pytest

from compaction import s1_replay as RP
from compaction.s2 import compact as K
from compaction.s2 import lossaudit as LA
from compaction.s2 import session as S
from compaction.s2.env import Observation
from compaction.s2.loop import Loop
from compaction.s2.model import ModelError, RunCapReached, StubModel

AGENTS = "Always run the tests before you commit."
OUT_CHARS = 24_000  # one command's output: ~12k stub tokens (2 bytes/token)
THRESHOLD = 60_000  # stub tokens; pi's 20k-token (chars/4) tail is ~40k stub tokens


class FakeEnv:
    def __init__(self, fail_on: int | None = None):
        self.n = 0
        self.boundaries: list[tuple[object, int]] = []
        self.fail_on = fail_on

    def run_agent_command(self, cmd: str):
        self.n += 1
        if self.fail_on is not None and self.n == self.fail_on:
            raise RuntimeError("env broke")
        if cmd.startswith("echo "):  # the stub's submission
            return Observation(0, cmd[5:] + "\n", 0.01, False, False)
        return Observation(0, f"out {self.n} " + "x" * OUT_CHARS, 0.01, False, False)

    def ask_user(self, question: str, action: str | None = None) -> str:
        return f"reply to: {question}"

    def trap_snapshot(self, label: str) -> float:
        return 0.0

    def boundary(self, keep, k: int) -> dict:
        self.boundaries.append((keep, k))
        if keep is None:
            return {"tag": None, "stopped_processes": [], "snapshot_s": 0.0}
        tag = keep.new_tag(k)
        keep.record(k, tag, snapshot_s=0.5)
        return {"tag": tag, "stopped_processes": [], "snapshot_s": 0.5}

    def close(self) -> bool:
        return True


class RecModel:
    """A StubModel that records every summarizer request and numbers its summaries."""

    def __init__(self, actions, *, fail_summary: Exception | None = None,
                 fail_call: Exception | None = None):
        self._n = itertools.count(1)
        self.inner = StubModel(actions=actions, summary=lambda m: f"SUMMARY-{next(self._n)}")
        self.route = self.inner.route
        self.summary_reqs: list[str] = []
        self.fail_summary, self.fail_call = fail_summary, fail_call

    def complete(self, messages, tools, max_tokens, *, purpose):
        if tools is None:
            self.summary_reqs.append(messages[1]["content"])
            if self.fail_summary is not None:
                raise self.fail_summary
        elif self.fail_call is not None:
            raise self.fail_call
        return self.inner.complete(messages, tools, max_tokens, purpose=purpose)


def actions(n: int) -> list[dict]:
    # a reply without a tool call every 3rd call gets a user nudge: a turn start, so pi's cut
    # leaves history before the split turn (both its calls run)
    return [{"say": "thinking"} if i % 3 == 2 else {"bash": f"cat part{i}"} for i in range(n)]


def opening() -> list[dict]:
    return [{"role": "system", "content": "You are a coding agent."},
            K.instruction_message(AGENTS),
            {"role": "user", "content": "Implement the feature in /app."}]


def run_json(key: str = "kombu-x-deepseek-a1") -> dict:
    return {"run_key": key, "stage": "02_implement",
            "task": {"task_id": "kombu-x", "image": "task:img"},
            "plan": {"model": "deepseek", "policy": "pi", "threshold": THRESHOLD}}


def session(tmp_path, n_actions=40, *, hooks=None, keep=True, max_calls=600, model=None,
            env=None, key="kombu-x-deepseek-a1", loop=None, previous=None, run_dir=None):
    model = model or RecModel(actions(n_actions))
    env = env or FakeEnv()
    loop = loop or Loop(opening(), model, env, "base")
    run_dir = run_dir or tmp_path / key
    ks = S.KeepStore(run_dir, key) if keep else None
    res = S.run_session(loop, env, ks, run_dir=run_dir, run_json=run_json(key),
                        threshold=THRESHOLD, policy="pi", instruction_text=AGENTS,
                        max_calls=max_calls, run_cap_usd=0.0, hooks=hooks or S.SessionHooks(),
                        previous=previous)
    return res, model, env, loop, run_dir


def until_k(n: int):
    """An after_call hook that ends the session once n boundaries exist."""
    def after(loop):
        return "test_end" if after.dirs and len(after.dirs()) >= n else None
    after.dirs = None
    return after


def _comp(d):
    return json.loads((d / "boundary" / "compaction.json").read_text())


def _bj(d):
    return json.loads((d / "boundary" / "boundary.json").read_text())


def _conversations(comp: dict) -> list[str]:
    return [r["user"].split("</conversation>")[0] if "<conversation>" in r["user"]
            else r["user"].split("# Instructions")[0] for r in comp["requests"]]


def test_three_boundaries_first_then_update_prompt(tmp_path):
    hooks = S.SessionHooks(after_call=until_k(3))
    root = tmp_path / "kombu-x-deepseek-a1"
    hooks.after_call.dirs = lambda: S.boundary_dirs(root)
    res, _model, _env, loop, run_dir = session(tmp_path, hooks=hooks)
    assert res.boundaries == [1, 2, 3] and res.end == "test_end" and res.error is None
    assert res.boundary_calls == sorted(res.boundary_calls) and len(res.boundary_calls) == 3
    first = RP.prompts_for("pi").texts["pi_summarization_prompt.txt"]
    upd = K.pi_update_prompt()
    for k in (1, 2, 3):
        d = run_dir / f"b{k}"
        comp, bj = _comp(d), _bj(d)
        hist = [r for r in comp["requests"] if r["label"] == "history"]
        assert len(hist) == 1, comp["requests"]
        assert bj["boundary_index"] == k and bj["policy"] == "pi"
        assert bj["tag"] == f"emx-sandbox/s2-interact:kombu-x-deepseek-a1-b{k}"
        assert bj["stage"] == "02_implement"
        assert bj["tail_start_index"] == min(bj["kept_indices"])
        if k == 1:
            assert hist[0]["user"].endswith(first) and "<previous-summary>" not in hist[0]["user"]
            assert bj["update"] is False and bj["update_prompt_sha256"] is None
            assert bj["previous_boundary"] is None
        else:
            prev = _comp(run_dir / f"b{k - 1}")["summary"]
            assert hist[0]["user"].endswith(
                f"<previous-summary>\n{prev}\n</previous-summary>\n\n" + upd)
            assert bj["update"] is True and bj["previous_boundary"] == k - 1
            assert bj["update_prompt_sha256"] == K.pi_update_prompt_sha256()
            assert comp["info"]["previous_summary_sha256"] == RP._sha(prev)
            # the message carrying the previous summary is not serialised
            si = _comp(run_dir / f"b{k - 1}")["summary_index"]
            pre = json.loads((d / "boundary" / "pre_messages.json").read_text())
            assert prev in pre[si]["content"]
            assert comp["info"]["previous_summary_index"] == si
            for conv in _conversations(comp):
                assert "compacted into the following summary" not in conv
                assert prev not in conv
        # the resumed context re-attaches the instruction exactly once
        post = json.loads((d / "boundary" / "post_messages.json").read_text())
        assert sum(K.is_instruction(m) for m in post) == 1
        rj = json.loads((d / "run.json").read_text())
        assert rj["boundary_index"] == k and rj["task"]["image"] == bj["tag"]
        assert rj["task"]["base_image"] == "task:img"
    # the loop continued on b3's post context
    assert loop.messages[:len(_comp(run_dir / "b3")["post_messages"])] == \
        _comp(run_dir / "b3")["post_messages"]
    kept = S.KeepStore.list(run_dir)
    assert [r["k"] for r in kept] == [1, 2, 3] and all(r["created_utc"] for r in kept)
    assert all(r["snapshot_s"] == 0.5 for r in kept)


def test_files_in_order_boundary_json_last(tmp_path, monkeypatch):
    order = []
    real = S._dump

    def dump(path, obj):
        order.append(path.name if path.parent.name == "boundary" else f"{path.parent.name}/"
                     f"{path.name}")
        real(path, obj)

    monkeypatch.setattr(S, "_dump", dump)

    def on_boundary(ctx):
        order.append("on_boundary")
        (ctx.bdir / "boundary" / "sim_state.json").write_text("{}")
        ctx.fields["sim_calls"] = 7

    def stop(ctx):
        order.append("stop_at_boundary")
        return True

    res, *_ , run_dir = session(tmp_path, hooks=S.SessionHooks(on_boundary=on_boundary,
                                                               stop_at_boundary=stop))
    assert res.end == "eligible" and res.boundaries == [1]
    b_order = [x for x in order if not x.startswith("kept") and "kept_snapshots" not in x]
    assert b_order == ["pre_messages.json", "compaction.json", "post_messages.json",
                       "record.json", "b1/run.json", "on_boundary", "stop_at_boundary",
                       "boundary.json"]
    bj = _bj(run_dir / "b1")
    assert bj["sim_calls"] == 7 and bj["eligible"] is True
    assert set(bj) >= {"tag", "stopped_processes", "snapshot_s", "boundary_index", "stage",
                       "calls_in_step", "pre_tokens", "post_tokens_est", "n_pre_messages",
                       "summary_chars", "summary_index", "kept_indices", "tail_start_index",
                       "policy", "update", "previous_boundary", "compaction_cost_usd",
                       "prompt_sha256", "update_prompt_sha256", "fingerprint_sha256",
                       "sim_calls", "eligible", "written_utc"}


def test_stop_at_second_boundary_and_eligible_flags(tmp_path):
    res, *_, run_dir = session(tmp_path, hooks=S.SessionHooks(
        stop_at_boundary=lambda ctx: ctx.k == 2))
    assert res.end == "eligible" and res.boundaries == [1, 2]
    assert _bj(run_dir / "b1")["eligible"] is False and _bj(run_dir / "b2")["eligible"] is True


def test_lossaudit_loads_every_boundary(tmp_path):
    hooks = S.SessionHooks(after_call=until_k(2))
    root = tmp_path
    hooks.after_call.dirs = lambda: S.boundary_dirs(root)
    session(tmp_path, hooks=hooks, key="kombu-x-deepseek-a1")
    hooks2 = S.SessionHooks(after_call=until_k(1))
    hooks2.after_call.dirs = lambda: S.boundary_dirs(root / "helm-y-deepseek-a1")
    session(tmp_path, hooks=hooks2, key="helm-y-deepseek-a1")
    dirs = S.boundary_dirs(root)
    assert [d.relative_to(root).as_posix() for d in dirs] == [
        "helm-y-deepseek-a1/b1", "kombu-x-deepseek-a1/b1", "kombu-x-deepseek-a1/b2"]
    assert S.boundary_dirs(root / "kombu-x-deepseek-a1") == dirs[1:]
    idx = LA.Index(dirs)
    assert len(idx.rids) == 3
    for rid in idx.rids:
        rec = idx.load(rid)
        assert rec["history_complete"] and rec["agents_md_prepended"]
        assert rec["s2"]["image"].startswith("emx-sandbox/s2-interact:")
        assert rec["s2"]["summary_index"] is not None


def test_incomplete_boundary_is_not_listed(tmp_path):
    *_, run_dir = session(tmp_path, hooks=S.SessionHooks(stop_at_boundary=lambda c: True))
    (run_dir / "b1" / "boundary" / "boundary.json").unlink()
    assert S.boundary_dirs(run_dir) == []


def test_caps_and_ends(tmp_path):
    res, *_ = session(tmp_path / "a", max_calls=3)
    assert res.end == "max_calls" and res.n_calls == 3 and res.boundaries == []
    model = RecModel(actions(40))
    loop = Loop(opening(), model, FakeEnv(), "base")
    loop.cost_usd = 5.0
    res = S.run_session(loop, loop.env, None, run_dir=tmp_path / "b", run_json=run_json(),
                        threshold=THRESHOLD, policy="pi", instruction_text=AGENTS,
                        max_calls=600, run_cap_usd=1.0, hooks=S.SessionHooks())
    assert res.end == "run_cap" and res.n_calls == 0
    res, *_ = session(tmp_path / "c", n_actions=3)
    assert res.end == "submitted"
    res, *_ = session(tmp_path / "d", model=RecModel(actions(40),
                                                     fail_call=RunCapReached("cap", 0.0)))
    assert res.end == "run_cap" and "RunCapReached" in res.error
    res, *_ = session(tmp_path / "e", model=RecModel(actions(40),
                                                     fail_call=ModelError("api down", 0.25)))
    assert res.end == "error" and "api down" in res.error and res.cost_usd == 0.25
    # a failed compaction is an error, charged; no boundary.json is written
    res, _model, _env, _loop, run_dir = session(
        tmp_path / "f", model=RecModel(actions(40), fail_summary=ModelError("empty", 0.5)))
    assert res.end == "error" and res.cost_usd == 0.5 and S.boundary_dirs(run_dir) == []
    import threading
    stop = threading.Event()
    stop.set()
    loop = Loop(opening(), RecModel(actions(40)), FakeEnv(), "base", stop=stop)
    res, *_ = session(tmp_path / "g", loop=loop, env=loop.env)
    assert res.end == "stopped"
    with pytest.raises(RuntimeError, match="env broke"):
        session(tmp_path / "h", env=FakeEnv(fail_on=2))


def test_after_call_ends_before_any_compaction(tmp_path):
    seen = []

    def after(loop):
        seen.append(loop.ctx_tokens)
        return "ask_user" if loop.ctx_tokens >= THRESHOLD else None

    res, model, env, _loop, run_dir = session(tmp_path, hooks=S.SessionHooks(after_call=after))
    assert res.end == "ask_user" and res.boundaries == [] and model.summary_reqs == []
    assert env.boundaries == [] and not run_dir.exists()


def test_arm_recompaction_uses_update_prompt_on_given_summary(tmp_path):
    res, *_, run_dir = session(tmp_path, hooks=S.SessionHooks(stop_at_boundary=lambda c: True))
    comp = _comp(run_dir / "b1")
    block = "Additional context from earlier in this session:\nUse snake_case names."
    start = K.with_block(comp["post_messages"], comp["summary_index"], block)
    model = RecModel(actions(40))
    env = FakeEnv()
    arm_dir = run_dir / "arms" / "live-repair-1"
    loop = Loop([dict(m) for m in start], model, env, "arm")
    hooks = S.SessionHooks(after_call=until_k(1))
    hooks.after_call.dirs = lambda: S.boundary_dirs(arm_dir)
    prev_text = comp["summary"] + "\n\n" + block
    res = S.run_session(loop, env, None, run_dir=arm_dir, run_json=run_json(),
                        threshold=THRESHOLD, policy="pi", instruction_text=AGENTS,
                        max_calls=600, run_cap_usd=0.0, hooks=hooks,
                        previous=(prev_text, comp["summary_index"]))
    assert res.boundaries == [1] and env.boundaries == [(None, 1)]
    ac = _comp(arm_dir / "b1")
    hist = next(r for r in ac["requests"] if r["label"] == "history")
    assert hist["user"].endswith(f"<previous-summary>\n{prev_text}\n</previous-summary>\n\n"
                                 + K.pi_update_prompt())
    for conv in _conversations(ac):
        assert "Use snake_case names." not in conv  # it lives in <previous-summary> only
    bj = _bj(arm_dir / "b1")
    assert bj["tag"] is None and bj["update"] is True
    assert not (arm_dir / S.KEPT_FILE).exists()
    # the arm's record id differs from the base run's, so both load into one index
    both = S.boundary_dirs(run_dir, arms=True)
    assert both == [run_dir / "b1", arm_dir / "b1"]
    assert len(LA.Index(both).rids) == 2


def test_compact_update_argument_checks():
    m = StubModel(actions=[])
    msgs = opening() + [{"role": "user", "content": "x"}]
    with pytest.raises(ValueError, match="go together"):
        K.compact("pi", msgs, m, run_id="r", instruction_text=None, previous_summary="s")
    with pytest.raises(ValueError, match="pi only"):
        K.compact("codex_local", msgs, m, run_id="r", instruction_text=None,
                  previous_summary="s", previous_summary_index=3)
    with pytest.raises(ValueError, match="not a user or assistant"):
        K.compact("pi", msgs, m, run_id="r", instruction_text=None,
                  previous_summary="s", previous_summary_index=0)


def test_post_context_matches_compact_for_every_policy():
    msgs = opening()
    for i in range(30):
        msgs += [{"role": "assistant", "content": f"step {i}", "tool_calls": [
                     {"id": f"c{i}", "type": "function",
                      "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})}}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 6000}]
        if i % 5 == 4:
            msgs.append({"role": "user", "content": f"user request {i} " + "z" * 30000})
    model = StubModel(actions=[], summary=lambda m: "<summary>S</summary>")
    for policy in K.POLICIES:
        comp = K.compact(policy, msgs, model, run_id="r", instruction_text=AGENTS)
        texts = None
        if policy == "codex_local":
            rec = {"id": "x", "source": "deepswe", "session_id": "r",
                   "source_messages": K.to_record_messages(msgs), "user_turns": [],
                   "summary": "", "post_messages": []}
            texts = [m["text"] for m in RP.ARM_BUILDERS[policy](rec).kept_messages]
        post, si = K.post_context(policy, msgs, comp.summary, comp.kept_indices, AGENTS,
                                  kept_texts=texts)
        assert (post, si) == (comp.post_messages, comp.summary_index), policy
        assert sum(K.is_instruction(m) for m in post) == 1
    oc = K.compact("opencode", msgs, model, run_id="r", instruction_text=AGENTS)
    assert oc.post_messages[oc.summary_index - 1]["content"] == K.OPENCODE_COMPACTION_USER
    assert oc.post_messages[oc.summary_index] == {"role": "assistant", "content": oc.summary}
    assert oc.post_messages[-1]["content"] == K.OPENCODE_CONTINUE
    with pytest.raises(ValueError, match="instruction"):
        K.post_context("pi", msgs, "S", [1], AGENTS)


def test_parse_grep_output():
    pats = ["alpha", "beta"]
    out = []
    for n, place, body in [
        (0, "wl", ["./notes.txt"]), (0, "wn", ["notes.txt:3:alpha here"]),
        (0, "bl", ["side:src/a.py"]), (0, "bn", ["side:src/a.py:9:alpha"]),
        (0, "log", ["commit:abc123:0:fix alpha"]), (0, "plan", ["/tmp/plan.md:2:alpha step"]),
        (1, "wl", []), (1, "wn", []), (1, "bl", []), (1, "bn", []), (1, "log", []),
        (1, "plan", []),
    ]:
        rc = 0 if body else (0 if place == "log" else 1)
        out += [f"@@S {n} {place}", *body, f"@@E {n} {place} {rc}"]
    got = S.parse_grep_output("\n".join(out), pats)
    assert got["alpha"]["files"] == ["notes.txt", "side:src/a.py", "commit:abc123",
                                     "/tmp/plan.md"]
    assert {x["path"] for x in got["alpha"]["lines"]} == {
        "notes.txt", "side:src/a.py", "commit:abc123", "/tmp/plan.md"}
    assert got["beta"] == {"files": [], "lines": []}
    from compaction.deepswe.sandbox import SandboxError
    with pytest.raises(SandboxError):
        S.parse_grep_output("\n".join(out[:-3]), pats)
    with pytest.raises(SandboxError):
        S.parse_grep_output("\n".join(out).replace("@@E 0 wl 0", "@@E 0 wl 2"), pats)


# ------------------------------------------------------------------------------------------
# Docker
# ------------------------------------------------------------------------------------------

DEFAULT_IMAGE = ("public.ecr.aws/d3j8x8q7/swe-bench-202605:"
                 "kh705e1hgmtj22pyetcmqa8ygs83bh33-v1.1")
IMAGE = os.environ.get("S2_INTERACT_TEST_IMAGE", DEFAULT_IMAGE)


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    p = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60,
                       check=False)
    return p.returncode == 0


def _images(ref: str) -> list[str]:
    p = subprocess.run(["docker", "images", "-a", "--filter", f"reference={ref}", "--format",
                        "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, timeout=60,
                       check=True)
    return p.stdout.split()


GIT = "git -c user.name=Dev -c user.email=dev@example.com -c safe.directory='*'"
SETUP_STRINGS = (
    "echo 'only-untracked-zq81' > zz_untracked_notes.txt && "
    "printf '# plan\\nonly-in-plan-zq82\\n' > /tmp/plan.md && "
    f"B=$({GIT} rev-parse --abbrev-ref HEAD) && {GIT} checkout -q -b zz-side && "
    "echo 'only-on-branch-zq83' > zz_side_only.txt && "
    f"{GIT} add zz_side_only.txt && {GIT} commit -qm 'side work' && "
    f"{GIT} commit -q --allow-empty -m 'address feedback: only-in-message-zq84' && "
    f"{GIT} checkout -q \"$B\" && test ! -e zz_side_only.txt && echo ok")


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_kept_snapshot_survives_cleanup_and_search_finds_offtree_strings(tmp_path):
    from compaction.deepswe.sandbox import Invocation

    key = f"zz-test-{os.getpid()}-a1"
    ks = S.KeepStore(tmp_path / key, key)
    tag = None
    try:
        with Invocation(purpose="s2-session-test") as inv:
            sb = inv.start(IMAGE, cpus=2, memory_mb=4096, network="none", lifetime_s=900)
            r = sb.exec(SETUP_STRINGS, timeout_s=300)
            assert r.rc == 0 and "ok" in r.stdout, r.stderr[-800:]
            tag = ks.new_tag(1)
            secs = sb.snapshot(tag, labels=ks.labels())
            ks.record(1, tag, snapshot_s=secs)
            labels = json.loads(subprocess.run(
                ["docker", "image", "inspect", tag, "--format", "{{json .Config.Labels}}"],
                capture_output=True, text=True, timeout=60, check=True).stdout)
            assert labels["emx.keep"] == key and labels["emx.run"] == ""
            assert labels["emx.sandbox"] == "s2-session-test"
            run_id = inv.run_id
        # the run's own cleanup (sweep + tracked tags) ran on exit; the kept tag is still there
        assert _images(tag) == [tag]
        swept = subprocess.run(["docker", "images", "--filter", f"label=emx.run={run_id}",
                                "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True,
                               text=True, timeout=60, check=True).stdout.split()
        assert tag not in swept
        pats = ["only-untracked-zq81", "only-in-plan-zq82", "only-on-branch-zq83",
                "only-in-message-zq84", "absent-everywhere-zq85"]
        got = S.grep_runner()(tag, pats)
        assert "zz_untracked_notes.txt" in got["only-untracked-zq81"]["files"]
        assert got["only-in-plan-zq82"]["files"] == ["/tmp/plan.md"]
        assert got["only-on-branch-zq83"]["files"] == ["zz-side:zz_side_only.txt"]
        assert [f for f in got["only-in-message-zq84"]["files"] if f.startswith("commit:")]
        assert got["absent-everywhere-zq85"] == {"files": [], "lines": []}
        assert all(got[p]["lines"] for p in pats[:4])
    finally:
        bad = S.KeepStore.remove(tmp_path / key)
    assert bad == []
    assert tag is not None and _images(tag) == []
    assert all(r.get("removed_utc") for r in S.KeepStore.list(tmp_path / key))
    left = subprocess.run(["docker", "ps", "-a", "--filter", "label=emx.sandbox",
                           "--format", "{{.Names}} {{.Label \"emx.sandbox\"}}"],
                          capture_output=True, text=True, timeout=60, check=True).stdout
    assert "s2-session-test" not in left and S.GREP_PURPOSE not in left
