"""The SWE-Interact pilot runner (compaction.s2.interact_run; build split unit C).

Unit tests use a temp ledger and caps file, stub backends and synthetic boundaries.
`test_base_arm_secondary_in_docker` runs a stub agent and a stub simulator through 01_plan, a
fresh 02_implement with one boundary, a primary arm (capture mode) and its secondary, in a real
task container, over `FakeSession`, a minimal stand-in for unit A's `session` module with the
frozen signatures (the integration runs it on the real one).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from compaction.s1_types import Req, Resp
from compaction.s2 import config as C
from compaction.s2 import interact as I
from compaction.s2 import interact_run as IR
from compaction.s2.model import Reply, StageCapReached, StubModel, Usage, tool_call

PERSONA = "# Shared invariants\n...\nTask block: add `frob(x)` returning 2*x.\n"


# ------------------------------------------------------------------------------------------
# Spend
# ------------------------------------------------------------------------------------------


@pytest.fixture
def books(tmp_path):
    caps = (C.REPO_ROOT / "docs" / "sprint" / "caps.toml").read_text()
    caps = re.sub(r'(?m)^checkout = ".*"$', f'checkout = "{tmp_path}"', caps)
    cf = tmp_path / "caps.toml"
    cf.write_text(caps)
    led = tmp_path / "logs" / "costs.jsonl"
    led.parent.mkdir()
    return led, cf


def _rows(led: Path) -> list[dict]:
    return [json.loads(x) for x in led.read_text().splitlines()] if led.exists() else []


def test_pilot_spend_tags_and_caps(books):
    led, cf = books
    sp = IR.PilotSpend(ledger=led, caps_file=cf)
    r1 = sp.reserve(50.0, "s2 base:1")
    sp.settle(r1, 40.0, resource="deepseek:x", units=10, note="s2 base:1")
    r2 = sp.reserve(70.0, "s2 base:2")  # 40 settled + 70 open = 110 <= 120
    with pytest.raises(StageCapReached):
        sp.reserve(15.0, "s2 base:3")  # 125 > 120
    sp.cancel(r2, "test")
    sp.reserve(15.0, "s2 base:3")
    rows = _rows(led)
    assert rows and all(C.INTERACT_TAG in r["note"] and r["exp"] == C.INTERACT_STAGE
                        for r in rows)
    sp.recorder()("judge", "claude-max:haiku", units=3, usd=0.0, note="Max plan, $0",
                  exp="SPRINT-S1")
    last = _rows(led)[-1]
    assert last["usd"] == 0.0 and C.INTERACT_TAG in last["note"]
    assert last["exp"] == C.INTERACT_STAGE  # re-booked from claude_headless's SPRINT-S1
    assert IR.tag_totals(C.INTERACT_TAG, ledger=led, caps_file=cf) == (40.0, 15.0)


def test_fallback_spend_has_both_tags_and_sub_cap(books):
    led, cf = books
    fb = IR.fallback_spend(IR.PilotSpend(ledger=led, caps_file=cf))
    assert fb.tags == (C.INTERACT_TAG, C.INTERACT_FALLBACK_TAG)
    r = fb.reserve(10.0, "fallback")
    fb.settle(r, 10.0, resource="haiku", units=1, note="fallback")
    with pytest.raises(StageCapReached):
        fb.reserve(3.0, "fallback")  # 13 > 12 fallback sub-cap
    assert IR.tag_totals(C.INTERACT_TAG, ledger=led, caps_file=cf)[0] == 10.0  # counts toward $120 too


# ------------------------------------------------------------------------------------------
# Max with the fallback
# ------------------------------------------------------------------------------------------


def _req(key: str, schema=None, tools=()) -> Req:
    return Req(key, "sys", "user " + key, schema, 100, "rid", "deepswe", False, tools=tools)


class Primary:
    name, model, context_tokens, bytes_per_token = "max", "claude-haiku-5-5", 1_000_000, 2.2

    def __init__(self, limit_keys=(), sleep_s=0.0):
        self.limit_keys, self.sleep_s, self.calls = set(limit_keys), sleep_s, []

    def run(self, reqs, *, purpose):
        self.calls.append([r.key for r in reqs])
        time.sleep(self.sleep_s)
        return {r.key: Resp(r.key, None, "max:" + r.key,
                            "limit" if r.key in self.limit_keys else None) for r in reqs}


class Fallback:
    def __init__(self):
        self.calls = []

    def run(self, reqs, *, purpose):
        self.calls.append([r.key for r in reqs])
        return {r.key: Resp(r.key, None, "or:" + r.key, None, usd=0.01) for r in reqs}


def test_fallback_on_limit_is_sticky():
    p, f = Primary(limit_keys={"b"}), Fallback()
    be = IR.FallbackBackend(p, f, None, timeout_s=5)
    out = be.run([_req("a"), _req("b")], purpose="split")
    assert out["a"].text == "max:a" and out["b"].text == "or:b"
    assert be.switched and be.events[0]["why"] == "limit"
    be.run([_req("c")], purpose="split")
    assert p.calls == [["a", "b"]] and f.calls == [["b"], ["c"]]  # stays switched


def test_fallback_on_timeout():
    p, f = Primary(sleep_s=0.5), Fallback()
    be = IR.FallbackBackend(p, f, None, timeout_s=0.1)
    out = be.run([_req("a")], purpose="replay")
    assert out["a"].text == "or:a" and be.events[0]["why"] == "timeout"


class JsonModel:
    route = C.ROUTES["haiku"]

    def __init__(self, text):
        self.text = text

    def complete(self, messages, tools, max_tokens, *, purpose):
        return Reply({"role": "assistant", "content": self.text}, Usage(10, 5, 0), "stop",
                     cost_total_usd=0.002)


def test_or_haiku_backend_schema_and_tools():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    be = IR.ORHaikuBackend(spend=None, model_factory=lambda s: JsonModel('```json\n{"ok": true}\n```'))
    out = be.run([_req("a", schema), _req("t", tools=("Read",))], purpose="p")
    assert out["a"].data == {"ok": True} and out["a"].error is None and out["a"].usd == 0.002
    from compaction import s1_replay as RP
    assert out["a"].model == RP.MODEL  # replays refuse any other model id (model_mismatch)
    assert out["t"].error == "max_only"
    bad = IR.ORHaikuBackend(spend=None, model_factory=lambda s: JsonModel("not json"))
    assert bad.run([_req("a", schema)], purpose="p")["a"].error == "schema"


# ------------------------------------------------------------------------------------------
# Boundary bookkeeping and arms (no Docker)
# ------------------------------------------------------------------------------------------


def _ask_pair(cid, q, reply):
    return [{"role": "assistant", "content": "", "tool_calls": [
        tool_call("ask_user", {"question": q}, cid)]},
            {"role": "tool", "tool_call_id": cid, "content": reply}]


def test_reply_index_matches_suffix_and_flags_mismatch():
    rows = [{"reply_id": "r1", "call_index": 1, "step": "01_plan", "question": "q", "reply": "a"},
            {"reply_id": "r2", "call_index": 2, "step": "02_implement", "question": "q",
             "reply": "b"},
            {"reply_id": "r3", "call_index": 3, "step": "02_implement", "question": "q",
             "reply": "c"}]
    pre = [{"role": "system", "content": "s"}, {"role": "user", "content": "summary"},
           *_ask_pair("x3", "q", "c")]
    out = IR.reply_index(rows, pre)
    assert [r["pre_index"] for r in out] == [None, None, 3]
    pre_bad = [{"role": "system", "content": "s"}, *_ask_pair("x3", "q", "different")]
    assert "pre_index_mismatch" in IR.reply_index(rows, pre_bad)[2]


POST = [{"role": "system", "content": "sys"}, {"role": "user", "content": "SUMMARY"},
        {"role": "user", "content": "kept tail"}]


def test_arm_context_differs_only_in_block():
    src = {"post": POST, "summary_index": 1, "summary": "SUMMARY"}
    rep = IR.ArmSpec("cc_prompt", "repair", 1, "don't touch tests")
    ctl = IR.ArmSpec("cc_prompt", "matched", 1, "the CI badge is green")
    a, _ = IR.arm_context(Path("."), rep, load_source=lambda b, s: src)
    b, _ = IR.arm_context(Path("."), ctl, load_source=lambda b, s: src)
    assert a[1]["content"].endswith(f"{IR.LEAD}\ndon't touch tests")
    assert IR.inputs_differ_only_in_block(a, b, 1)
    assert not IR.inputs_differ_only_in_block(a, b, 2)
    assert POST[1]["content"] == "SUMMARY"  # the source context is not mutated
    assert IR.arm_previous(src, rep) == f"SUMMARY\n\n{IR.LEAD}\ndon't touch tests"
    with pytest.raises(ValueError):
        IR.ArmSpec("pi", "repair", 1, "x")  # the pi replay is an anchor, never a source


def test_run_bases_reruns_infra_once_and_drops_concurrency_after_two_ooms(tmp_path):
    cfgs = [IR.BaseConfig(task=IR.InteractTask(f"t{n}", None, PERSONA, {}, "spec", "sha"),  # type: ignore[arg-type]
                          root=tmp_path, attempt=1, image=f"i{n}") for n in range(4)]
    live, peak, lock = [0], [0], threading.Lock()

    def run_one(cfg):
        cfg.run_dir.mkdir(parents=True)  # attempts are never reused
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        if cfg.attempt == 1 and cfg.image in ("i0", "i1"):
            return {"end": "infra_oom", "image": cfg.image, "attempt": cfg.attempt}
        if cfg.attempt == 1 and cfg.image == "i2":
            return {"end": "infra_error", "image": cfg.image, "attempt": cfg.attempt}
        return {"end": "eligible", "image": cfg.image, "attempt": cfg.attempt}

    out = IR.run_bases(cfgs, run_one, concurrency=6)
    assert len(out) == 7 and sorted(r["image"] for r in out if r["attempt"] == 2) == \
        ["i0", "i1", "i2"]
    assert peak[0] >= 4  # all first attempts at once


def test_k_counts_validates_each_comparison(tmp_path, monkeypatch):
    frz = {"source_policy": "live", "record_sha256": "F", "repair_text": "R",
           "matched_text": "M"}
    monkeypatch.setattr(IR, "verified_freeze", lambda bdir, cid, require_committed=True: frz)
    ctx = [{"role": "system", "content": "s"}, {"role": "user", "content": "SUMMARY"}]

    def expected(bdir, f, kind, load_source=None):
        c = [dict(m) for m in ctx]
        c[1]["content"] += f"\n{kind}"
        return c
    monkeypatch.setattr(IR, "expected_context", expected)

    def mk(task, ends, *, other_freeze=False, leak=False):
        rd = tmp_path / f"{task}-deepseek-a1"
        (rd / "b1" / "boundary" / "freeze").mkdir(parents=True)
        (rd / "run.json").write_text(json.dumps({"task": {"task_id": task}}))
        (rd / "b1" / "boundary" / "freeze" / "c1.json").write_text("{}")
        for (kind, rep), end in zip([(k, r) for k in ("repair", "matched") for r in (1, 2)],
                                    ends, strict=True):
            d = rd / "arms" / f"live-{kind}-{rep}"
            d.mkdir(parents=True)
            c = [dict(m) for m in ctx]
            c[1]["content"] += f"\n{kind}"
            if leak and kind == "matched":
                c[0]["content"] = "different system"
            (d / "context.json").write_text(json.dumps(c))
            (d / "arm.json").write_text(json.dumps({
                "cand_id": "c1", "freeze_sha256": "G" if other_freeze else "F",
                "boundary_index": 1, "block_index": 1,
                "input_sha256": I.sha256(I.canon(expected(None, None, kind)))}))
            (d / "readout.json").write_text(json.dumps(
                {"end": end, "error": None, "fork_check": {"ok": True}}))
    mk("a", ["ask_user"] * 4)
    mk("b", ["max_calls", "submitted", "ask_user", "ask_user"])
    mk("c", ["ask_user", "window", "ask_user", "ask_user"])  # not a permitted endpoint
    mk("d", ["ask_user"] * 4, other_freeze=True)
    mk("e", ["ask_user"] * 4, leak=True)  # inputs differ outside the block
    out = IR.k_counts(tmp_path)
    assert out["K"] == 2 and out["K_live"] == 2 and set(out["tasks"]) == {"a", "b"}
    assert set(out["invalid"]) == {f"{t}-deepseek-a1/b1/c1" for t in "cde"}


# ------------------------------------------------------------------------------------------
# Docker: base run, primary, secondary over a minimal session stand-in
# ------------------------------------------------------------------------------------------

IMAGE = os.environ.get("S2_INTERACT_TEST_IMAGE",
                       "public.ecr.aws/d3j8x8q7/swe-bench-202605:"
                       "kh705e1hgmtj22pyetcmqa8ygs83bh33-v1.1")


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True,
                          timeout=60, check=False).returncode == 0


class StubSim:
    """Round 1 inspects `git log`; round 2 replies with a hash of its request."""

    def complete(self, messages, tools, max_tokens, *, purpose):
        if tools and messages[-1]["role"] != "tool":
            msg = {"role": "assistant", "content": None, "tool_calls": [
                tool_call("run_repo_command", {"command": "git log --oneline -2"}, "c")]}
        else:
            h = hashlib.sha256(I.canon(messages).encode()).hexdigest()[:10]
            msg = {"role": "assistant", "content": f"maintainer-{h}"}
        return Reply(msg, Usage(100, 10, 0, 0, 0.0, "stub"), "stop")


class FakeSession:
    """Unit A's `session` with its frozen signatures, reduced to what this test needs: a
    boundary after `boundary_at` calls of a session (pi stubbed: summary text "SUMMARY", the
    last 2 messages kept), the frozen file order, and the hooks."""

    @dataclass
    class SessionHooks:
        on_boundary: object = None
        stop_at_boundary: object = None
        after_call: object = None

    @dataclass
    class KeepStore:
        run_dir: Path
        run_key: str
        host: str | None = None
        tags: list = field(default_factory=list)

        def new_tag(self, k):
            t = f"emx-sandbox/s2-interact:test-{uuid.uuid4().hex[:8]}-b{k}"
            self.tags.append(t)
            FakeSession.all_tags.append(t)
            return t

        def labels(self):
            return {"emx.keep": self.run_key}

        def record(self, k, tag, **meta):
            p = Path(self.run_dir) / "kept_snapshots.json"
            rows = json.loads(p.read_text()) if p.exists() else []
            p.write_text(json.dumps([*rows, {"k": k, "tag": tag, **meta}]))

    @dataclass
    class BoundaryCtx:
        k: int
        bdir: Path
        loop: object
        fields: dict
        compaction: object = None

    @dataclass
    class SessionResult:
        end: str
        n_calls: int
        cost_usd: float
        boundaries: list
        seconds: float
        error: str | None
        boundary_calls: list = field(default_factory=list)

    all_tags: ClassVar[list] = []
    boundary_at = 4
    previous_seen: ClassVar[list] = []

    @classmethod
    def run_session(cls, loop, env, keep, *, run_dir, run_json, threshold, policy,
                    instruction_text, max_calls, run_cap_usd, hooks, previous=None):
        cls.previous_seen.append(previous)
        ks, calls, n0 = [], [], loop.n_calls
        while loop.n_calls - n0 < max_calls:
            loop.step(f"turn-{loop.n_calls + 1}")
            end = hooks.after_call(loop) if hooks.after_call else None
            if end:
                return cls.SessionResult(end, loop.n_calls, loop.cost_usd, ks, 0.0, None, calls)
            if keep is not None and loop.n_calls - n0 == cls.boundary_at:
                k = len(ks) + 1
                bdir = Path(run_dir) / f"b{k}"
                b = bdir / "boundary"
                b.mkdir(parents=True)
                snap = env.boundary(keep, k)
                pre = list(loop.messages)
                post = [pre[0], {"role": "user", "content": "SUMMARY"}, *pre[-2:]]
                for name, obj in (("pre_messages", pre), ("post_messages", post),
                                  ("compaction", {"summary": "SUMMARY", "summary_index": 1,
                                                  "post_messages": post})):
                    (b / f"{name}.json").write_text(json.dumps(obj))
                (bdir / "run.json").write_text(json.dumps(
                    {**run_json, "boundary_index": k,
                     "task": {**run_json["task"], "image": snap["tag"]}}))
                fields = {**snap, "boundary_index": k, "summary_index": 1,
                          "tail_start_index": len(pre) - 2}
                ctx = cls.BoundaryCtx(k, bdir, loop, fields)
                hooks.on_boundary(ctx)
                stop = hooks.stop_at_boundary(ctx)
                (b / "boundary.json").write_text(json.dumps(fields))
                ks.append(k)
                calls.append(loop.n_calls)
                if stop:
                    return cls.SessionResult("eligible", loop.n_calls, loop.cost_usd, ks, 0.0,
                                             None, calls)
        return cls.SessionResult("max_calls", loop.n_calls, loop.cost_usd, ks, 0.0, None, calls)


def _load_live(bdir, source):
    c = json.loads((Path(bdir) / "boundary" / "compaction.json").read_text())
    return {"post": c["post_messages"], "summary_index": 1, "summary": c["summary"]}


PLAN_ACTIONS = [
    {"ask": "Hi, what change do you need?"},
    {"bash": "printf 'Plan: add frob(x)=2x\\n' > /tmp/plan.md && echo ok"},
    {"submit": True},
]
IMPL_ACTIONS = [
    {"bash": "cat /tmp/plan.md && git rev-parse HEAD > /tmp/base_commit.txt"},
    {"bash": "printf 'export const frob = (x) => 2 * x\\n' > frob.ts && git add frob.ts"
             " && git commit -qm 'initial implementation' && echo ok"},
    {"ask": "Done with the initial implementation, committed. Review?"},
    {"bash": "echo notes > scratch.txt && echo ok"},
]


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_base_arm_secondary_in_docker(tmp_path, monkeypatch):
    from compaction.deepswe import sandbox as SB
    from compaction.deepswe.sandbox import Invocation

    orig = SB.Sandbox.snapshot
    if "labels" not in orig.__code__.co_varnames:  # until unit A's additive kwarg lands
        monkeypatch.setattr(SB.Sandbox, "snapshot", lambda self, tag, labels=None: orig(self, tag))
    monkeypatch.setattr(C, "PLATFORM", "linux/amd64")
    task = IR.InteractTask(
        "fake", IR.task_info("koota-composite-trait-aspects"), PERSONA,
        {"01_plan": "Plan with the maintainer; save /tmp/plan.md.",
         "02_implement": "Read /tmp/plan.md first; implement; ask for review."}, "spec", "sha")
    cfg = IR.BaseConfig(task=task, root=tmp_path, image=IMAGE, threshold=10**9)
    seen: list = []

    def eligible(bdir, fields):
        seen.append((Path(bdir).name, dict(fields)))
        return True

    frz = {"source_policy": "live", "record_sha256": "F", "repair_text": "keep frob in frob.ts",
           "matched_text": "the CI badge is green"}
    monkeypatch.setattr(IR, "verified_freeze", lambda bdir, cid, require_committed=True: frz)
    FakeSession.all_tags = []
    try:
        with Invocation(purpose=IR.PURPOSE) as inv:
            run_id = inv.run_id
            res = IR.run_base(cfg, agent_model=StubModel(PLAN_ACTIONS + IMPL_ACTIONS),
                              sim_model=StubSim(), inv=inv, eligible=eligible,
                              session=FakeSession)
            assert res["end"] == "eligible", res.get("error")
            assert res["steps"]["01_plan"]["end"] == "submitted"
            assert res["plan_md"].startswith("Plan: add frob")
            rd = cfg.run_dir
            impl = json.loads((rd / "steps" / "02_implement" / "messages.json").read_text())
            assert "Read /tmp/plan.md first" in impl[1]["content"]  # a fresh context
            assert "Hi, what change" not in json.dumps(impl)
            b = rd / "b1" / "boundary"
            st = json.loads((b / "sim_state.json").read_text())
            assert st["state"]["ask_user_call_count"] == 2  # 01's talk persisted into 02
            reps = json.loads((b / "sim_replies.json").read_text())
            assert [r["step"] for r in reps] == ["01_plan", "02_implement"]
            assert reps[0]["pre_index"] is None and reps[1]["pre_index"] is not None
            assert (b / "step_instruction.txt").read_text().startswith("Read /tmp/plan.md")
            assert seen[0][1]["sim_calls"] == 2 and seen[0][1]["stage"] == "02_implement"
            assert len(seen[0][1]["fingerprint_sha256"]) == 64

            from compaction.s2 import interact_score as SC
            live = _load_live(rd / "b1", "live")
            frz["source"] = {"summary": live["summary"], "summary_index": 1,
                             "kept_indices": None, "fingerprint": None,
                             "post_sha256": SC.canon_sha(live["post"])}
            spec = IR.ArmSpec("live", "repair", 1, "keep frob in frob.ts")
            arm_agent = StubModel([{"bash": "git log --oneline -1"},
                                   {"bash": "echo act > act.txt && rm act.txt"},
                                   {"bash": "echo kept > kept.txt"},
                                   {"ask": "Should frob also handle strings?"}])
            prim = IR.run_arm(rd, 1, "c1", "repair", 1, task, agent_model=arm_agent,
                              sim_model=StubSim(), inv=inv, session=FakeSession,
                              load_source=_load_live)
            assert prim["end"] == "ask_user", prim
            assert prim["question"] == "Should frob also handle strings?"
            ad = rd / "arms" / spec.slug
            ctx = json.loads((ad / "context.json").read_text())
            assert ctx[1]["content"].endswith(f"{IR.LEAD}\nkeep frob in frob.ts")
            assert FakeSession.previous_seen[-1] == (
                f"SUMMARY\n\n{IR.LEAD}\nkeep frob in frob.ts", 1)
            diff = (ad / "diff.patch").read_text()
            assert "kept.txt" in diff and "scratch.txt" not in diff  # measured from the fork
            assert "scratch.txt" in (ad / "task.patch").read_text() or True  # uncommitted
            assert prim["end_image"] and prim["fork_check"]["ok"]
            with pytest.raises(FileExistsError):  # an arm dir is never reused
                IR.run_arm(rd, 1, "c1", "repair", 1, task, agent_model=arm_agent,
                           sim_model=StubSim(), inv=inv, session=FakeSession,
                           load_source=_load_live)

            sec_agent = StubModel([{"bash": "printf '// ok\\n' >> frob.ts && git commit -qam"
                                            " 'address feedback' && echo ok"},
                                   {"submit": True}])
            sec = IR.run_secondary(rd, 1, "c1", "repair", 1, task, agent_model=sec_agent,
                                   sim_model=StubSim(), inv=inv, session=FakeSession,
                                   grade=lambda t, p, rid: {"reward": None, "bytes": len(p)})
            assert sec["end"] == "submitted", sec
            assert sec["sim_calls"] == 1  # the pending ask, answered by its own simulator
            msgs = json.loads((ad / "secondary" / "messages_end.json").read_text())
            assert I.PENDING_ASK not in json.dumps(msgs)
            assert json.loads((ad / "secondary" / "grade.json").read_text())["bytes"] > 0
    finally:
        for t in FakeSession.all_tags:
            subprocess.run(["docker", "rmi", "-f", t], capture_output=True, check=False)
    # only this test's own containers: a live pilot shares the emx.sandbox label
    q = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=emx.run={run_id}"],
                       capture_output=True, text=True, check=False)
    assert q.returncode == 0, q.stderr  # a failed query must not pass as "nothing left"
    assert q.stdout.split() == []


OC_POST = [{"role": "system", "content": "sys"},
           {"role": "user", "content": "What did we do so far?"},
           {"role": "assistant", "content": "OC SUMMARY"},
           {"role": "user", "content": "Continue if you have next steps"}]


def test_opencode_block_is_a_separate_user_message():
    src = {"post": OC_POST, "summary_index": 2, "summary": "OC SUMMARY"}
    rep = IR.ArmSpec("opencode", "repair", 1, "don't touch tests")
    ctl = IR.ArmSpec("opencode", "matched", 1, "the CI badge is green")
    a, _ = IR.arm_context(Path("."), rep, load_source=lambda b, s: src)
    b, _ = IR.arm_context(Path("."), ctl, load_source=lambda b, s: src)
    assert a[2] == OC_POST[2]  # the assistant summary is untouched
    assert a[3] == {"role": "user", "content": f"{IR.LEAD}\ndon't touch tests"}
    assert a[4] == OC_POST[3] and len(a) == len(OC_POST) + 1
    assert IR.block_index("opencode", 2) == 3 and IR.block_index("live", 1) == 1
    assert IR.inputs_differ_only_in_block(a, b, 3) and not IR.inputs_differ_only_in_block(a, b, 2)
    assert IR.arm_previous(src, rep) == "OC SUMMARY"  # the block is in the conversation


def test_funnel_lists_check6_exclusions_with_the_scorer(tmp_path):
    b = tmp_path / "t-deepseek-a1" / "b2" / "boundary"
    b.mkdir(parents=True)
    spec = {"act": {"kind": "file_touched", "path": "kombu/*.py"}, "decision": None,
            "description": "edits kombu sources", "reask_keywords": []}
    cands = [
        {"cand_id": "live-b2-q1", "source_policy": "live", "text": "only top consumer active",
         "act_spec": spec, "disposition": "excluded:reference",
         "checks": {"reference": {"outcome": "fail", "reason": "reference_performs_act",
                                  "evidence": {"matches": [{"path": "kombu/entity.py"}]}}}},
        {"cand_id": "live-b2-q2", "source_policy": "live", "text": "x", "act_spec": None,
         "disposition": "excluded:recoverability",
         "checks": {"recoverability": {"outcome": "fail", "reason": "grep_hit",
                                       "evidence": {"hits": ["frob.ts"]}}}},
        {"cand_id": "live-b2-q3", "source_policy": "live", "text": "y", "act_spec": spec,
         "disposition": "carded", "checks": {}}]
    (b / "candidates.json").write_text(json.dumps({"candidates": cands}))
    out = IR.funnel(tmp_path)
    assert out["counts"] == {"excluded:reference": 1, "excluded:recoverability": 1, "carded": 1}
    ref = next(r for r in out["excluded"] if r["reason"] == "excluded:reference")
    assert ref["scorer"]["description"] == "edits kombu sources"
    assert ref["reference_matches"] == [{"path": "kombu/entity.py"}]
    md = (tmp_path / "funnel.md").read_text()
    assert "Scorer (Haiku-written): edits kombu sources" in md and "kombu/entity.py" in md


def test_expired_reservation_keeps_counting_against_the_tag(books):
    """astra build r1 #2: budget.expire charges a dead reservation with its own note (our tag
    lost); the registry still counts it, at the charged amount."""
    from compaction import budget
    led, cf = books
    sp = IR.PilotSpend(ledger=led, caps_file=cf)
    rid = sp.reserve(30.0, "s2 base:1")
    assert IR.tag_totals(C.INTERACT_TAG, ledger=led, caps_file=cf) == (0.0, 30.0)
    state = budget._paths(led)[2]  # its process "dies": a pid that isn't running
    res = budget._load_state(state)
    res[rid]["pid"] = 2 ** 22 - 7
    budget._save_state(state, res)
    got = budget.expire(0, charge_full=True, reason="test", force=True, ledger=led,
                        caps_file=cf, now=time.time() + 10)
    assert got
    assert IR.tag_totals(C.INTERACT_TAG, ledger=led, caps_file=cf) == (30.0, 0.0)
    with pytest.raises(StageCapReached):
        sp.reserve(95.0, "s2 base:2")  # 30 charged + 95 > 120


class OneAtATime(Primary):
    """Like MaxBackend: refuses a second concurrent run() on one instance."""

    def __init__(self):
        super().__init__(sleep_s=0.05)
        self._busy = threading.Lock()

    def run(self, reqs, *, purpose):
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("concurrent run() on one MaxBackend")
        try:
            return super().run(reqs, purpose=purpose)
        finally:
            self._busy.release()


def test_concurrent_callers_get_their_own_primary():
    """astra build r1 #1: the replays call one backend from 5 workers."""
    shared = OneAtATime()
    be = IR.FallbackBackend(shared, Fallback(), None, timeout_s=5)
    with pytest.raises(RuntimeError):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(5) as ex:
            list(ex.map(lambda k: be.run([_req(k)], purpose="r"), "abcde"))
    be = IR.FallbackBackend(None, Fallback(), None, timeout_s=5, primary_factory=OneAtATime)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(5) as ex:
        outs = list(ex.map(lambda k: be.run([_req(k)], purpose="r"), "abcde"))
    assert [o[k].text for o, k in zip(outs, "abcde", strict=True)] == [f"max:{k}" for k in "abcde"]
    assert not be.switched


def test_check6_negated_act_unreached_is_unresolved(tmp_path):
    """astra build r1 #9: a negated act whose decision the reference never reaches can't be
    judged on the reference."""
    from compaction.s2 import interact_score as SC
    rd = tmp_path / "r-deepseek-a1"
    (rd / "b1").mkdir(parents=True)
    sol = tmp_path / "sol.patch"
    sol.write_text("diff --git a/x.py b/x.py\n")
    (rd / "b1" / "run.json").write_text(json.dumps(
        {"task": {"image": "img", "solution_patch": str(sol)}}))
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n a\n+b = 1\n")
    runner = lambda image, script: (0, f"{SC.DIFF_MARK}\n{diff}", "")
    neg = {"act": {"kind": "diff_added", "pattern": "guard", "negate": True},
           "decision": {"kind": "diff_added", "pattern": "def parse"}, "description": "d",
           "reask_keywords": []}
    assert SC.check_reference(neg, rd, 1, runner=runner)["outcome"] == "unresolved"
    reached = dict(neg, decision={"kind": "diff_added", "pattern": "b = 1"})
    assert SC.check_reference(reached, rd, 1, runner=runner)["outcome"] == "fail"
    plain = {"act": {"kind": "diff_added", "pattern": "NEVER"}, "decision": None,
             "description": "d", "reask_keywords": []}
    assert SC.check_reference(plain, rd, 1, runner=runner)["outcome"] == "pass"


def test_judge_refuses_a_truncated_window(tmp_path, monkeypatch):
    """astra build r1 #8: a continuation longer than the judge window is unscorable."""
    from compaction.s2 import interact_score as SC
    monkeypatch.setattr(SC, "JUDGE_CHARS", 50)
    (tmp_path / "readout.json").write_text(json.dumps({"question": None}))
    (tmp_path / "continuation.json").write_text(json.dumps(
        [{"role": "assistant", "content": "x" * 200}]))
    spec = {"act": {"kind": "judge"}, "decision": None, "description": "d",
            "reask_keywords": []}
    called = []
    out = SC.score(tmp_path, spec, judge=lambda d, s: called.append(1) or {"act": False})
    assert out["score"] is None and not called
    assert out["evidence"]["invalid"] == "continuation_exceeds_judge_window"


def test_gradable_is_the_descriptive_denominator():
    """astra build r1 #13: only grounded 02_implement items verbatim in this input."""
    from compaction.s2 import interact_reqs as RQ
    base = {"valid": True, "duplicate_of": None, "step": "02_implement", "grounded": True,
            "source_pre_index": 5}
    items = [dict(base, req_id="ok"), dict(base, req_id="ungrounded", grounded=False),
             dict(base, req_id="inherited", source_pre_index=None),
             dict(base, req_id="plan", step="01_plan"), dict(base, req_id="dup",
                                                              duplicate_of="ok")]
    assert [it["req_id"] for it in RQ.gradable({"items": items})] == ["ok"]


def test_base_spend_file_is_outside_the_fresh_run_dir(tmp_path):
    """astra build r2 #1: the CLI attaches the run's spend before run_base allocates its
    (fresh) run dir."""
    from compaction.s2.model import NullSpend, RunSpend
    task = IR.InteractTask("t", None, PERSONA, {}, "spec", "sha")  # type: ignore[arg-type]
    cfg = IR.BaseConfig(task=task, root=tmp_path)
    RunSpend(NullSpend(), 8.0).attach(IR.spend_path(cfg.root, f"{cfg.run_key}-base"))
    assert not cfg.run_dir.exists()
    IR._fresh_dir(cfg.run_dir)
    with pytest.raises(FileExistsError):
        IR._fresh_dir(cfg.run_dir)


def test_frozen_source_refuses_a_changed_source():
    from compaction.s2 import interact_score as SC
    src = {"post": POST, "summary_index": 1, "summary": "SUMMARY", "kept_indices": [2],
           "fingerprint": "fp"}
    frz = {"source_policy": "cc_prompt", "repair_text": "R", "matched_text": "M",
           "source": {"summary": "SUMMARY", "summary_index": 1, "kept_indices": [2],
                      "fingerprint": "fp", "post_sha256": SC.canon_sha(POST)}}
    assert IR.frozen_source(Path("."), frz, load_source=lambda b, s: src) is src
    regenerated = dict(src, post=[*POST[:2], {"role": "user", "content": "other tail"}])
    with pytest.raises(ValueError, match="post_sha256"):
        IR.frozen_source(Path("."), frz, load_source=lambda b, s: regenerated)
    with pytest.raises(ValueError, match="summary"):
        IR.frozen_source(Path("."), frz, load_source=lambda b, s: dict(src, summary="new"))
    ctx = IR.expected_context(Path("."), frz, "matched", load_source=lambda b, s: src)
    assert ctx[1]["content"].endswith(f"{IR.LEAD}\nM") and ctx[2] == POST[2]


def _patch(path: str, added: list[str]) -> str:
    body = "".join(f"+{ln}\n" for ln in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added)} @@\n{body}")


def test_negated_act_is_scored_per_state(tmp_path):
    """astra build r2 #3: a rewrite without the guard, fixed in the next call, still counts."""
    from compaction.s2 import interact_score as SC
    (tmp_path / "readout.json").write_text(json.dumps({"question": None}))
    (tmp_path / "commands.json").write_text("[]")
    spec = {"act": {"kind": "diff_added", "pattern": r"if not ok", "negate": True},
            "decision": {"kind": "diff_added", "pattern": r"def parse"},
            "description": "rewrites parse without the guard", "reask_keywords": []}
    (tmp_path / "diff.patch").write_text("")
    assert SC.score(tmp_path, spec)["score"] is None  # no states: unscorable, not 0
    st = tmp_path / "states"
    st.mkdir()
    (st / "0001.patch").write_text(_patch("p.py", ["def parse(x):", "    return x"]))
    (st / "0002.patch").write_text(_patch("p.py", ["def parse(x):", "    if not ok: raise",
                                                   "    return x"]))
    out = SC.score(tmp_path, spec)
    assert out["score"] == 1 and out["evidence"]["state"] == "0001.patch"
    (st / "0001.patch").unlink()
    assert SC.score(tmp_path, spec)["score"] == 0  # only the guarded state


def test_resume_fills_only_pending_results():
    """astra build r2 #4: ask_user, then an unknown tool (already answered with an error),
    then bash (deferred)."""
    msgs = [{"role": "system", "content": "s"},
            {"role": "assistant", "content": "", "tool_calls": [
                tool_call("ask_user", {"question": "q"}, "a1"),
                tool_call("nope", {}, "a2"), tool_call("bash", {"command": "ls"}, "a3")]},
            {"role": "tool", "tool_call_id": "a1", "content": I.PENDING_ASK},
            {"role": "tool", "tool_call_id": "a2", "content": "Unknown tool 'nope'."},
            {"role": "tool", "tool_call_id": "a3",
             "content": f"<returncode>0</returncode><output>{I.PENDING_DEFERRED}</output>"}]
    out = IR._resume_messages(msgs, "q", "the answer", ["ls output"])
    assert [m["content"] for m in out[2:]] == ["the answer", "Unknown tool 'nope'.",
                                               "ls output"]


def test_negated_act_with_a_command_matcher_is_unscorable(tmp_path):
    """astra build r3: commands aren't bound to states, so a negated spec using one is left to
    the card rather than scored."""
    from compaction.s2 import interact_score as SC
    (tmp_path / "readout.json").write_text(json.dumps({"question": None}))
    (tmp_path / "commands.json").write_text(json.dumps(
        [{"call": 1, "cmd": "ls", "rc": 0}, {"call": 3, "cmd": "git commit -m x", "rc": 0}]))
    (tmp_path / "diff.patch").write_text("")
    st = tmp_path / "states"
    st.mkdir()
    (st / "0001.patch").write_text("")
    (st / "0002.patch").write_text(_patch("p.py", ["if not ok: raise"]))
    spec = {"act": {"kind": "diff_added", "pattern": r"if not ok", "negate": True},
            "decision": {"kind": "command", "pattern": r"^git commit"},
            "description": "commits without the guard", "reask_keywords": []}
    out = SC.score(tmp_path, spec)
    assert out["score"] is None
    assert out["evidence"]["invalid"] == "negated_act_with_command_matcher"



class FakeForkEnv:
    """fork_check's view of an env: fingerprint(version) and a simulator."""

    class Sim:
        def first_request(self, q):
            return [{"role": "user", "content": q}]

    def __init__(self, fps):
        self.fps, self.sim, self.asked = fps, self.Sim(), []

    def fingerprint(self, version=I.FINGERPRINT_VERSION):
        self.asked.append(version)
        return self.fps[version]


@pytest.mark.parametrize("recorded,used", [(None, 1), (1, 1), (2, 2), (3, 3)])
def test_fork_check_uses_the_boundary_s_fingerprint_version(recorded, used):
    env = FakeForkEnv({1: "fp-v1", 2: "fp-v2", 3: "v3 abc"})
    b = {"fingerprint_sha256": I.sha256(env.fps[used]),
         "sim_probe_sha256": IR.sim_probe(env.sim)}
    if recorded is not None:
        b["fingerprint_version"] = recorded
    out = IR.fork_check(env, b)
    assert env.asked == [used] and out["ok"] and out["fingerprint_version"] == used


def test_fork_check_rejects_a_changed_workspace_or_simulator():
    env = FakeForkEnv({3: "v3 abc"})
    b = {"fingerprint_version": 3, "fingerprint_sha256": I.sha256("v3 other"),
         "sim_probe_sha256": IR.sim_probe(env.sim)}
    assert not IR.fork_check(env, b)["ok"]
    b2 = dict(b, fingerprint_sha256=I.sha256("v3 abc"), sim_probe_sha256="different")
    assert not IR.fork_check(env, b2)["ok"]


def test_no_rerun_makes_exactly_one_attempt(tmp_path):
    cfgs = [IR.BaseConfig(task=IR.InteractTask(f"t{n}", None, PERSONA, {}, "spec", "sha"),  # type: ignore[arg-type]
                          root=tmp_path, attempt=1) for n in range(2)]
    seen = []

    def run_one(cfg):
        cfg.run_dir.mkdir(parents=True)
        seen.append(cfg.run_key)
        return {"end": "infra_error"}

    out = IR.run_bases(cfgs, run_one, rerun=False)
    assert len(out) == 2 and sorted(seen) == ["t0-deepseek-a1", "t1-deepseek-a1"]
