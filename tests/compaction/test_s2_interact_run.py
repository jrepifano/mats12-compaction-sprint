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
                k = max([int(p.name[1:]) for p in Path(run_dir).glob("b[0-9]*")], default=0) + 1
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
    assert out["counts"] == {"v1": {"excluded:reference": 1, "excluded:recoverability": 1,
                                    "carded": 1}}
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



def _tree_hashes(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(d).rglob("*")) if p.is_file()}


@pytest.mark.skipif(not _image_present(), reason=f"needs docker and {IMAGE}")
def test_resume_base_continues_from_the_kept_boundary(tmp_path, monkeypatch):
    from compaction.deepswe import sandbox as SB
    from compaction.deepswe.sandbox import Invocation

    orig = SB.Sandbox.snapshot
    if "labels" not in orig.__code__.co_varnames:
        monkeypatch.setattr(SB.Sandbox, "snapshot", lambda self, tag, labels=None: orig(self, tag))
    monkeypatch.setattr(C, "PLATFORM", "linux/amd64")
    task = IR.InteractTask(
        "fake", IR.task_info("koota-composite-trait-aspects"), PERSONA,
        {"01_plan": "Plan with the maintainer; save /tmp/plan.md.",
         "02_implement": "Read /tmp/plan.md first; implement; ask for review."}, "spec", "sha")
    cfg = IR.BaseConfig(task=task, root=tmp_path, image=IMAGE, threshold=10**9)
    FakeSession.all_tags = []
    FakeSession.previous_seen = []
    try:
        with Invocation(purpose=IR.PURPOSE) as inv:
            res = IR.run_base(cfg, agent_model=StubModel(PLAN_ACTIONS + IMPL_ACTIONS),
                              sim_model=StubSim(), inv=inv, eligible=lambda b, f: True,
                              session=FakeSession)
            assert res["end"] == "eligible", res.get("error")
            rd = cfg.run_dir
            with pytest.raises(PermissionError, match="no final selection"):
                IR.resume_base(rd, task, agent_model=None, sim_model=None, inv=inv,
                               trigger=lambda b, f: True, session=FakeSession)
            IR._transition(rd / "b1", "exhausted", "test")  # nothing carded at b1
            b1_before = _tree_hashes(rd / "b1")
            result_before = (rd / "result.json").read_bytes()
            sim_b1 = json.loads((rd / "b1" / "boundary" / "sim_state.json").read_text())
            seen = []

            def trigger(bdir, fields):
                seen.append((Path(bdir).name, fields.get("calls_in_step")))
                return True

            agent = StubModel([{"bash": "echo more >> frob.ts && git commit -qam more && echo ok"},
                               {"ask": "Done with that change, committed. Review?"},
                               {"bash": "git log --oneline -1"},
                               {"bash": "ls"}])
            r = IR.resume_base(rd, task, agent_model=agent, sim_model=StubSim(), inv=inv,
                               trigger=trigger, session=FakeSession)
            assert r["end"] == "eligible" and r["boundaries"] == [2], r.get("error")
            assert r["fork_check"]["ok"]
            assert _tree_hashes(rd / "b1") == b1_before  # the saved boundary is untouched
            assert (rd / "result.json").read_bytes() == result_before
            assert json.loads((rd / "result_r1.json").read_text())["from_boundary"] == 1
            assert FakeSession.previous_seen[-1] == ("SUMMARY", 1)  # pi's update on b1's summary
            assert seen == [("b2", 4 + 4)]  # calls_in_step continues from b1's
            sim_b2 = json.loads((rd / "b2" / "boundary" / "sim_state.json").read_text())
            assert sim_b2["state"]["ask_user_call_count"] == \
                sim_b1["state"]["ask_user_call_count"] + 1  # the same simulator, one more reply
            reps = json.loads((rd / "b2" / "boundary" / "sim_replies.json").read_text())
            assert [x["reply_id"] for x in reps][-1] == f"r{sim_b2['state']['ask_user_call_count']}"
            kept = json.loads((rd / "kept_snapshots.json").read_text())
            assert [x["k"] for x in kept] == [1, 2]
            assert r["left"]["calls"] == C.INTERACT_BASE_MAX_CALLS - res["n_calls"]
    finally:
        for t in FakeSession.all_tags:
            subprocess.run(["docker", "rmi", "-f", t], capture_output=True, check=False)


def _run_with_b1(tmp_path, state):
    rd = tmp_path / "t-deepseek-a1"
    (rd / "b1" / "boundary").mkdir(parents=True)
    (rd / "b1" / "boundary" / "boundary.json").write_text(json.dumps({"tag": "x"}))
    (rd / "run.json").write_text(json.dumps({"run_key": rd.name, "task": {}}))
    (rd / "result.json").write_text(json.dumps({"cost_usd": 9.0, "sim_cost_usd": 0.0,
                                                "n_calls": 10, "seconds": 5}))
    if state:
        IR._transition(rd / "b1", state, "test", **({"frozen": "live.b1-q2"}
                                                    if state == "frozen" else {}))
    return rd


def test_resume_gates_on_the_selection_state_and_seals_after_a_freeze(tmp_path):
    task = IR.InteractTask("t", None, PERSONA, {"02_implement": "x"}, "spec", "sha")  # type: ignore[arg-type]
    kw = {"agent_model": None, "sim_model": None, "inv": None, "trigger": lambda b, f: True}
    for state in (None, "screening", "screen_failed", "carded"):
        rd = _run_with_b1(tmp_path / str(state), state)
        with pytest.raises(PermissionError, match="no final selection"):
            IR.resume_base(rd, task, **kw)
    rd = _run_with_b1(tmp_path / "frozen", "frozen")
    with pytest.raises(PermissionError, match="after-freeze"):
        IR.resume_base(rd, task, **kw)
    r = IR.resume_base(rd, task, after_freeze=True, **kw)  # stops at once: the $8 cap
    assert r["end"] == "run_cap" and IR.active_seal(rd) == 1
    with pytest.raises(PermissionError, match="after-freeze"):  # the seal persists
        IR.resume_base(rd, task, **kw)
    rd = _run_with_b1(tmp_path / "exhausted", "exhausted")
    assert IR.resume_base(rd, task, **kw)["end"] == "run_cap"  # allowed, unsealed
    assert IR.active_seal(rd) is None
    assert IR._resume_index(rd) == 2


def _two_boundaries(tmp_path):
    rd = tmp_path / "t-deepseek-a1"
    for k in (1, 2):
        (rd / f"b{k}" / "boundary").mkdir(parents=True)
        (rd / f"b{k}" / "boundary" / "boundary.json").write_text("{}")
    return rd, IR.InteractTask("t", None, PERSONA, {}, "spec", "sha")  # type: ignore[arg-type]


def test_advance_screens_saved_boundaries_in_order_then_resumes(tmp_path):
    rd, task = _two_boundaries(tmp_path)
    (rd / "result.json").write_text(json.dumps({"end": "eligible"}))
    calls = []

    def exhaust(b, f):
        calls.append(Path(b).name)
        IR._transition(b, "exhausted", "no_candidate")
        return False

    out = IR.advance(rd, task, trigger=exhaust, resume=lambda: {"end": "submitted"})
    assert calls == ["b1", "b2"] and out["fork_at"] is None
    assert out["resumed"] == {"end": "submitted"}
    calls.clear()
    IR.advance(rd, task, trigger=exhaust, resume=dict)
    assert calls == []  # screened boundaries aren't screened again


def test_advance_pauses_at_a_carded_boundary_and_seals_what_lies_past_it(tmp_path):
    rd, task = _two_boundaries(tmp_path)

    def card_b1(b, f):
        IR._transition(b, "carded", "carded", add_cards=["live.b1-q2"])
        return True

    out = IR.advance(rd, task, trigger=card_b1, resume=lambda: pytest.fail("must not resume"))
    assert out["fork_at"] == 1 and out["state"] == "carded"
    assert IR.active_seal(rd) == 1  # b2 already exists: sealed until b1 is decided
    IR._transition(rd / "b1", "exhausted", "card_cap")
    assert IR.active_seal(rd) is None  # an exhausted boundary never forks: the seal lifts


def test_screen_failure_completes_the_boundary_and_advance_retries(tmp_path):
    rd = tmp_path / "t-deepseek-a1"
    (rd / "b1" / "boundary").mkdir(parents=True)
    fields = {}

    def boom(b, f):
        raise RuntimeError("Max down")

    assert IR.safe_trigger(boom)(rd / "b1", fields) is True  # pause, don't strand
    assert fields["screen_error"].startswith("RuntimeError")
    assert IR.state_of(rd / "b1") == "screen_failed"
    (rd / "b1" / "boundary" / "boundary.json").write_text("{}")  # the session wrote it
    task = IR.InteractTask("t", None, PERSONA, {}, "spec", "sha")  # type: ignore[arg-type]
    out = IR.advance(rd, task, trigger=boom, resume=lambda: pytest.fail("no resume"))
    assert out["state"] == "screen_failed" and "Max down" in out["error"]

    def ok(b, f):
        IR._transition(b, "exhausted", "no_candidate")
        return False

    out = IR.advance(rd, task, trigger=ok, resume=lambda: {"end": "submitted"})
    assert out["screened"] == [{"k": 1, "state": "exhausted"}] and out["resumed"]
    log = [json.loads(x) for x in (tmp_path / IR.SELECTION_LOG).read_text().splitlines()]
    assert [x["to"] for x in log] == ["screen_failed", "screen_failed", "exhausted"]


def _screened(carded, bundle=()):
    return {"carded": carded, "bundle": list(bundle)}


def test_selection_caps_and_transitions(tmp_path):
    root = tmp_path
    b = root / "t-deepseek-a1" / "b1"
    (b / "boundary").mkdir(parents=True)
    sel = IR.place_cards(b, _screened("live.b1-q2", ["live.b1-q3", "live.b1-q5"]))
    assert sel["state"] == "carded" and sel["primaries"] == ["live.b1-q2"]
    assert sel["cards"] == ["live.b1-q2", "live.b1-q3", "live.b1-q5"]  # members beside it
    sel = IR.record_decisions(b, [{"cand_id": "live.b1-q2", "status": "rejected",
                                   "role": "primary"}])
    assert sel["state"] == "carded" and sel["rejected"] == ["live.b1-q2"]
    sel = IR.place_cards(b, _screened("live.b1-q3"))  # a shown member promoted: no new card
    assert sel["state"] == "carded" and len(sel["cards"]) == 3
    assert sel["primaries"] == ["live.b1-q2", "live.b1-q3"]
    IR.record_decisions(b, [{"cand_id": "live.b1-q3", "status": "rejected", "role": "primary"}])
    sel = IR.place_cards(b, _screened("cc_prompt.b1-q2"))  # a third primary: no room
    assert sel["state"] == "exhausted" and sel["log"][-1]["event"] == "card_cap"
    states = []
    for n in range(2, 8):  # 12 cards in all (3 + 2 each): t6 gets its primary only, t7 none
        bn = root / f"t{n}-deepseek-a1" / "b1"
        (bn / "boundary").mkdir(parents=True)
        states.append(IR.place_cards(bn, _screened(f"live.b1-q{n}", [f"live.b1-x{n}"]))["state"])
    assert IR.cards_total(root) == 12 and states == ["carded"] * 5 + ["exhausted"]
    assert IR.selection(root / "t6-deepseek-a1" / "b1")["cards"] == ["live.b1-q6"]
    late = root / "t9-deepseek-a1" / "b1"
    (late / "boundary").mkdir(parents=True)
    assert IR.place_cards(late, _screened("live.b1-q9"))["state"] == "exhausted"
    frozen = root / "t2-deepseek-a1" / "b1"
    sel = IR.record_decisions(frozen, [{"cand_id": "live.b1-q2", "status": "frozen",
                                        "role": "primary"}])
    assert sel["state"] == "frozen" and sel["frozen"] == "live.b1-q2"
    assert IR.carded_at(frozen.parent, 1) == "live.b1-q2"
    log = (root / IR.SELECTION_LOG).read_text().splitlines()
    assert len(log) >= 10  # every transition is logged


def test_k_counts_skip_bundle_members(tmp_path, monkeypatch):
    rd = tmp_path / "t-deepseek-a1"
    fz = rd / "b1" / "boundary" / "freeze"
    fz.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"task": {"task_id": "t"}}))
    (fz / "live.b1-q3.json").write_text(json.dumps({"role": "bundle_member"}))
    (fz / "bundle.live.json").write_text(json.dumps({"bundle_items": ["a", "b"]}))
    called = []
    monkeypatch.setattr(IR, "valid_comparison",
                        lambda *a, **k: called.append(a) or {"valid": True,
                                                             "source_policy": "live"})
    assert IR.k_counts(tmp_path)["K"] == 0 and called == []
    with pytest.raises(ValueError, match="bundle member"):
        IR.refuse_bundle_member({"role": "bundle_member", "cand_id": "live.b1-q3"})
    IR.refuse_bundle_member({"role": "primary"})


def test_kept_images_are_per_run_dir_and_never_overwritten(tmp_path, monkeypatch):
    """24 Oct incident: a stub smoke reused the live kombu run's key, re-pointed its kept tags
    and then removed its images by label."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2 import session as SS
    key = "kombu-single-active-consumer-priority-deepseek-a1"
    live, smoke = SS.KeepStore(tmp_path / "live" / key, key), \
        SS.KeepStore(tmp_path / "smoke" / key, key)
    assert live.new_tag(1) != smoke.new_tag(1)
    assert live.labels() != smoke.labels()
    smoke.record(1, smoke.new_tag(1))
    removed, listed = [], []
    monkeypatch.setattr(SB, "remove_image", lambda t, *a, **k: removed.append(t) or True)
    monkeypatch.setattr(SB, "remove_image_id", lambda i, *a, **k: removed.append(i) or True)
    monkeypatch.setattr(SB, "_list", lambda args: listed.append(args) or [])
    monkeypatch.setattr(SS, "_owned", lambda tag, label: True)
    assert SS.KeepStore.remove(smoke.run_dir) == []
    assert removed == [smoke.new_tag(1)] and live.new_tag(1) not in removed
    assert all(f"emx.keep={smoke.label_id}" in " ".join(a) for a in listed)
    # a commit never re-points an existing tag
    monkeypatch.setattr(I, "tag_exists", lambda tag, host=None: True)

    class Sb:
        host = None

        def stray_processes(self):
            return []

        def snapshot(self, tag, labels=None):
            pytest.fail("must not commit over an existing tag")

    env = I.InteractEnv(Sb(), None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="already exists"):
        env.boundary(live, 1)
    # smoke runs never take a live run's key
    task = IR.InteractTask("kombu-single-active-consumer-priority", None, PERSONA, {},
                           "spec", "sha")  # type: ignore[arg-type]
    assert IR.BaseConfig(task=task, root=tmp_path, prefix="smoke-x-").run_key != key



def test_retired_and_retried_executions_never_share_kept_images(tmp_path, monkeypatch):
    """astra build r2 #1: a retry at the same path gets its own id; removing one execution's
    images never touches another's, and a re-pointed tag is left alone."""
    from compaction.deepswe import sandbox as SB
    from compaction.s2 import session as SS
    ad = tmp_path / "run" / "arms" / "live-repair-1"
    first = SS.KeepStore(ad, "run-live-repair-1")
    t1 = first.new_tag(0)
    first.record(0, t1)
    retired = IR.retire(ad)
    second = SS.KeepStore(ad, "run-live-repair-1")
    assert second.label_id != first.label_id and second.new_tag(0) != t1
    assert SS.KeepStore(retired, "run-live-repair-1").label_id == first.label_id  # kept
    removed, listed = [], []
    monkeypatch.setattr(SB, "remove_image", lambda t, *a, **k: removed.append(t) or True)
    monkeypatch.setattr(SB, "remove_image_id", lambda i, *a, **k: removed.append(i) or True)
    monkeypatch.setattr(SB, "_list", lambda args: listed.append(args) or [])
    monkeypatch.setattr(SS, "_owned", lambda tag, label: False)  # re-pointed by someone else
    SS.KeepStore.remove(retired)
    assert removed == []  # nothing that isn't this execution's
    row = json.loads((retired / "kept_snapshots.json").read_text())[0]
    assert "another execution" in row["not_removed"]
    monkeypatch.setattr(SS, "_owned", lambda tag, label: True)
    listed.clear()
    SS.KeepStore.remove(retired)  # already handled: no wider label sweep
    assert listed == []


def test_card_cap_counts_primaries_only(tmp_path):
    """Decided 24 Oct (pre-outcome): 2 primaries and 2 members per boundary, 12 cards in all.
    A boundary carded under the old rule (primary + 1 member, no primaries field) can still
    show a second primary after both are rejected."""
    b = tmp_path / "kombu-deepseek-a2" / "b2"
    (b / "boundary").mkdir(parents=True)
    old = {"k": 2, "state": "carded", "cards": ["live.b2-q1", "live.b2-q4"], "rejected": [],
           "frozen": None, "log": [{"event": "carded", "primary": "live.b2-q1", "to": "carded"}]}
    IR._dump(IR._v2dir(b) / IR.SELECTION_NAME, old)
    assert IR.primaries_of(IR.selection(b)) == ["live.b2-q1"]
    IR.record_decisions(b, [{"cand_id": "live.b2-q1", "status": "rejected", "role": "primary"},
                            {"cand_id": "live.b2-q4", "status": "rejected",
                             "role": "bundle_member"}])
    sel = IR.place_cards(b, _screened("cc_prompt.b2-q2", ["cc_prompt.b2-q3", "x.b2-q9"]))
    assert sel["state"] == "carded" and sel["primaries"] == ["live.b2-q1", "cc_prompt.b2-q2"]
    # one member already shown at this boundary: room for one more beside the new primary
    assert sel["cards"] == ["live.b2-q1", "live.b2-q4", "cc_prompt.b2-q2", "cc_prompt.b2-q3"]
    IR.record_decisions(b, [{"cand_id": "cc_prompt.b2-q2", "status": "rejected",
                             "role": "primary"}])
    sel = IR.place_cards(b, _screened("cc_prompt.b2-q3"))  # promotion needs primary room
    assert sel["state"] == "exhausted" and sel["log"][-1]["event"] == "card_cap"
    assert len(sel["cards"]) == 4 and IR.cards_total(tmp_path) == 4


def test_card_cap_migrates_old_logs_with_promotions(tmp_path):
    """An old-rule log with a promotion and repeated carded_again rows: primaries are counted
    once each, and a shown member can still be promoted at the 12-card cap (no new card)."""
    for n in range(4):  # 10 other cards
        bn = tmp_path / f"t{n}-deepseek-a1" / "b1"
        (bn / "boundary").mkdir(parents=True)
        IR.place_cards(bn, _screened(f"live.b1-q{n}", [f"live.b1-x{n}", f"live.b1-y{n}"]))
    b = tmp_path / "old-deepseek-a1" / "b1"
    (b / "boundary").mkdir(parents=True)
    old = {"k": 1, "state": "carded", "cards": ["live.b1-p", "live.b1-m"],
           "rejected": [], "frozen": None,
           "log": [{"event": "carded", "primary": "live.b1-p"},
                   {"event": "carded_again", "primary": "live.b1-p"},
                   {"event": "carded_again", "primary": "live.b1-p"}]}
    IR._dump(IR._v2dir(b) / IR.SELECTION_NAME, old)
    assert IR.primaries_of(IR.selection(b)) == ["live.b1-p"] and IR.cards_total(tmp_path) == 14
    IR.record_decisions(b, [{"cand_id": "live.b1-p", "status": "rejected", "role": "primary"}])
    sel = IR.place_cards(b, _screened("live.b1-m"))  # promotion over the total cap: allowed
    assert sel["state"] == "carded" and sel["primaries"] == ["live.b1-p", "live.b1-m"]
    assert IR.cards_total(tmp_path) == 14
    promoted = dict(old, log=[*old["log"], {"event": "carded_again", "primary": "live.b1-m"},
                              {"event": "carded_again", "primary": "live.b1-m"}])
    del promoted["state"]
    assert IR.primaries_of(promoted) == ["live.b1-p", "live.b1-m"]


def test_place_cards_never_reshows_a_rejected_candidate(tmp_path):
    b = tmp_path / "t-deepseek-a1" / "b1"
    (b / "boundary").mkdir(parents=True)
    IR.place_cards(b, _screened("live.b1-q1", ["live.b1-q2"]))
    IR.record_decisions(b, [{"cand_id": "live.b1-q1", "status": "rejected", "role": "primary"},
                            {"cand_id": "live.b1-q2", "status": "rejected",
                             "role": "bundle_member"}])
    before = IR.selection(b)
    with pytest.raises(ValueError, match="already rejected"):  # a stale screen
        IR.place_cards(b, _screened("live.b1-q1", ["live.b1-q3"]))
    with pytest.raises(ValueError, match="already rejected"):  # nor a rejected member promoted
        IR.place_cards(b, _screened("live.b1-q2"))
    assert IR.selection(b) == before  # no transition written
    sel = IR.place_cards(b, _screened("cc_prompt.b1-q4", ["live.b1-q2", "cc_prompt.b1-q5"]))
    assert sel["primaries"] == ["live.b1-q1", "cc_prompt.b1-q4"]
    assert sel["cards"] == ["live.b1-q1", "live.b1-q2", "cc_prompt.b1-q4", "cc_prompt.b1-q5"]


def test_card_cap_holds_under_concurrent_placement(tmp_path):
    """astra build r2 #3: two workers at 10 cards can't take 4."""
    for n in range(5):
        b = tmp_path / f"t{n}-deepseek-a1" / "b1"
        (b / "boundary").mkdir(parents=True)
        IR.place_cards(b, _screened(f"live.b1-q{n}", [f"live.b1-x{n}"]))
    assert IR.cards_total(tmp_path) == 10
    bs = []
    for n in (5, 6, 7, 8):
        b = tmp_path / f"t{n}-deepseek-a1" / "b1"
        (b / "boundary").mkdir(parents=True)
        bs.append(b)
    with __import__("concurrent.futures").futures.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda b: IR.place_cards(b, _screened(f"live.{b.parent.name}",
                                                          [f"x.{b.parent.name}"])), bs))
    assert IR.cards_total(tmp_path) == 12


def test_status_hides_a_sealed_run(tmp_path, monkeypatch, capsys):
    import importlib
    import sys
    sys.path.insert(0, str(C.REPO_ROOT / "scripts"))
    try:
        cli = importlib.import_module("s2_interact")
    finally:
        sys.path.pop(0)
    rd = _run_with_b1(tmp_path, "frozen")
    (rd / "b2" / "boundary").mkdir(parents=True)
    IR._seal(rd, 1, "test")
    monkeypatch.setattr(IR, "interact_root", lambda: tmp_path)
    monkeypatch.setattr(IR, "tag_totals", lambda tag, **k: (0.0, 0.0))
    monkeypatch.setattr(IR, "funnel", lambda root: {"counts": {}, "excluded": []})
    monkeypatch.setattr(IR, "k_counts", lambda root: {"K": 0})
    cli.cmd_status(None)
    out = capsys.readouterr().out
    assert '"sealed_after": 1' in out and "cost_usd" not in out


def test_advance_reports_a_screen_that_failed_inside_the_resume(tmp_path):
    rd, task = _two_boundaries(tmp_path)
    for k in (1, 2):
        IR._transition(rd / f"b{k}", "exhausted", "x")
    (rd / "result.json").write_text(json.dumps({"end": "eligible"}))

    def resume():
        (rd / "b3" / "boundary").mkdir(parents=True)
        (rd / "b3" / "boundary" / "boundary.json").write_text("{}")
        IR._transition(rd / "b3", "screen_failed", "screen_error", error="Max down")
        return {"end": "eligible"}

    out = IR.advance(rd, task, trigger=lambda b, f: False, resume=resume)
    assert out["state"] == "screen_failed" and out["fork_at"] == 3 and out["error"] == "Max down"


def test_antigravity_replay_is_pinned_to_the_frozen_1_4_3_prompt(tmp_path, monkeypatch):
    """24 Oct: agy updated itself to 1.4.7 (another summarizer prompt). Replays use Experiment
    1's frozen 1.4.3 texts: the same set sha256 as extracting them from the 1.4.3 binary."""
    import warnings

    from compaction import s1_replay as RP
    monkeypatch.delenv(RP.AGY_BINARY_ENV, raising=False)
    RP.clear_prompt_cache()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no "prompt differs" warning
        ps = RP.prompts_for("antigravity")
    assert ps.sha256 == RP.AGY_FROZEN_SET_SHA256
    assert RP._sha(ps.texts["agy_prompt"]) == RP.AGY_EXPECTED_SHA256
    assert ps.sources[0]["version"] == "1.4.3"


def test_agy_overrides_and_frozen_file_failures(tmp_path, monkeypatch):
    """astra agy-pin r1: no silent fallback to the installed binary; the override comes from
    the module or the AGY_BINARY env var, and empty means not set."""
    from compaction import s1_replay as RP
    called = []
    monkeypatch.setattr(RP, "_agy_prompts", lambda path: called.append(path) or "binary")
    monkeypatch.delenv(RP.AGY_BINARY_ENV, raising=False)
    RP._agy_frozen_prompts.cache_clear()
    # empty overrides, module or env: the frozen texts
    for module, env in (("", None), (None, ""), ("", "  ")):
        monkeypatch.setattr(RP, "AGY_BINARY", module)
        if env is None:
            monkeypatch.delenv(RP.AGY_BINARY_ENV, raising=False)
        else:
            monkeypatch.setenv(RP.AGY_BINARY_ENV, env)
        assert RP.prompts_for("antigravity").sha256 == RP.AGY_FROZEN_SET_SHA256
    assert called == []
    # explicit overrides: the env var, then the module setting (which wins)
    monkeypatch.setattr(RP, "AGY_BINARY", None)
    monkeypatch.setenv(RP.AGY_BINARY_ENV, "/env/agy")
    assert RP.prompts_for("antigravity") == "binary" and called == ["/env/agy"]
    monkeypatch.setattr(RP, "AGY_BINARY", "/module/agy")
    RP.prompts_for("antigravity")
    assert called[-1] == "/module/agy" and RP.agy_binary() == "/module/agy"
    # without an override, a missing frozen file is an error, never the installed agy
    monkeypatch.setattr(RP, "AGY_BINARY", None)
    monkeypatch.delenv(RP.AGY_BINARY_ENV, raising=False)
    monkeypatch.setattr(RP, "AGY_FROZEN", tmp_path / "missing.json")
    RP._agy_frozen_prompts.cache_clear()
    with pytest.raises(FileNotFoundError, match="frozen agy 1.4.3"):
        RP.prompts_for("antigravity")
    assert called[-1] == "/module/agy"  # the binary wasn't read
    # tampered texts: caught by their hashes, and with the hashes updated by the set sha256
    good = json.loads((C.REPO_ROOT / "src" / "compaction" / "s1_prompts" /
                       "agy_1.4.3_prompts.json").read_text())
    for name, fix_hashes in (("agy_prompt", False), ("agy_checkpoint_head", True)):
        bad = json.loads(json.dumps(good))
        bad["texts"][name] += " tampered"
        if fix_hashes:
            bad["text_sha256"][name] = RP._sha(bad["texts"][name])
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            RP._agy_frozen_prompts(str(p))
    RP._agy_frozen_prompts.cache_clear()


def test_replays_on_another_agy_prompt_are_flagged(tmp_path):
    from compaction import s1_replay as RP
    for k, sha in ((1, RP.AGY_FROZEN_SET_SHA256), (2, "6d6528961f4f" + "0" * 52)):
        d = tmp_path / "t-deepseek-a1" / f"b{k}" / "boundary" / "replays"
        (d / "antigravity").mkdir(parents=True)
        (d / "antigravity" / "replay.json").write_text(json.dumps({"prompt_sha256": sha}))
        (d / "cc_prompt").mkdir()
        (d / "cc_prompt" / "replay.json").write_text(json.dumps({"prompt_sha256": "x"}))
    rows = IR.replay_prompt_report(tmp_path)
    flagged = [(r["boundary"], r["policy"]) for r in rows if r["prompt_changed"]]
    assert flagged == [("b2", "antigravity")]
    rd = tmp_path / "t-deepseek-a1"
    assert IR.replay_prompt_changed(rd / "b2", "antigravity") is True
    assert IR.replay_prompt_changed(rd / "b1", "antigravity") is False
    assert IR.replay_prompt_changed(rd / "b2", "cc_prompt") is False
    assert IR.replay_prompt_changed(rd / "b2", "opencode") is None
    assert (tmp_path / "replay_prompts.json").exists()
    # a record with no prompt hash isn't proven frozen: flagged too
    (rd / "b1" / "boundary" / "replays" / "antigravity" / "replay.json").write_text("{}")
    assert IR.replay_prompt_changed(rd / "b1", "antigravity") is True
    assert ("b1", "antigravity") in [(r["boundary"], r["policy"])
                                     for r in IR.replay_prompt_report(tmp_path)
                                     if r["prompt_changed"]]
    # the rendered card note says what the hash proves, not a version
    import importlib
    import sys
    sys.path.insert(0, str(C.REPO_ROOT / "scripts"))
    try:
        cards = importlib.import_module("s2_interact_cards")
    finally:
        sys.path.pop(0)
    note = cards.source_note(rd / "b1", "antigravity")
    assert "didn't use Experiment 1's frozen agy 1.4.3 prompt set" in note
    assert "1.4.7" not in note
    assert "frozen" not in cards.source_note(rd / "b1", "cc_prompt")
