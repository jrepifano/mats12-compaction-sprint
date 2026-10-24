"""compaction.s2.replays: the offline replays' canonical records, on a stub backend ($0).

The boundary comes from a real `session.run_session` on a StubModel and a fake env (no Docker).
"""

from __future__ import annotations

import json

import pytest

from compaction import s1_replay as RP
from compaction.s1_types import Resp
from compaction.s2 import compact as K
from compaction.s2 import replays as RE
from compaction.s2 import session as S
from compaction.s2.loop import Loop
from compaction.s2.model import StubModel

AGENTS = "Always run the tests before you commit."
PI_SUMMARY = "## Goal\nShip the feature.\n## Constraints & Preferences\n- keep the API"


class FakeEnv:
    n = 0

    def run_agent_command(self, cmd):
        from compaction.s2.env import Observation
        self.n += 1
        return Observation(0, f"out {self.n} " + "x" * 24_000, 0.01, False, False)

    def ask_user(self, question, action=None):
        return "Please keep the old flag name."

    def trap_snapshot(self, label):
        return 0.0

    def boundary(self, keep, k):
        return {"tag": None, "stopped_processes": [], "snapshot_s": 0.0}

    def close(self):
        return True


class StubBackend:
    name = "stub-haiku-max"
    model = RP.MODEL
    family = "anthropic"
    context_tokens = 1_000_000
    bytes_per_token = 2.3

    def __init__(self, fail: str | None = None):
        self.calls: list[str] = []
        self.fail = fail

    def run(self, reqs, *, purpose):
        out = {}
        for r in reqs:
            self.calls.append(r.key)
            arm = r.key.split("@")[1].split("#")[0]
            err = "limit" if arm == self.fail else None
            text = PI_SUMMARY if arm == "pi" else f"<summary>{arm} summary of the work</summary>"
            out[r.key] = Resp(r.key, None, None if err else text, err, model=self.model)
        return out


def actions(n):
    acts = []
    for i in range(n):
        if i == 1:
            acts.append({"ask": "Should I keep the old flag name?"})
        elif i % 3 == 2:
            acts.append({"say": "thinking"})
        else:
            acts.append({"bash": f"cat part{i}"})
    return acts


@pytest.fixture
def bdir(tmp_path):
    key = "kombu-x-deepseek-a1"
    model = StubModel(actions=actions(40), summary=lambda m: PI_SUMMARY)
    env = FakeEnv()
    msgs = [{"role": "system", "content": "You are a coding agent."},
            K.instruction_message(AGENTS),
            {"role": "user", "content": "Implement the feature in /app."}]
    loop = Loop(msgs, model, env, "base")
    res = S.run_session(loop, env, None, run_dir=tmp_path / key,
                        run_json={"run_key": key, "stage": "02_implement",
                                  "task": {"task_id": "kombu-x", "image": "task:img"}},
                        threshold=60_000, policy="pi", instruction_text=AGENTS, max_calls=600,
                        run_cap_usd=0.0, hooks=S.SessionHooks(stop_at_boundary=lambda c: True))
    assert res.end == "eligible"
    return tmp_path / key / "b1"


def _read(p):
    return json.loads(p.read_text())


def test_five_policies_write_canonical_records(bdir):
    be = StubBackend()
    out = RE.replay_boundary(bdir, backend=be)
    assert set(out) == set(RP.REPLAY_ARMS) and all(v["status"] == "written"
                                                   for v in out.values())
    pre = _read(bdir / "boundary" / "pre_messages.json")
    for p in RP.REPLAY_ARMS:
        d = bdir / "boundary" / "replays" / p
        rec, post = _read(d / "replay.json"), _read(d / "post_messages.json")
        assert rec["policy"] == p and rec["summary"] and rec["model"] == RP.MODEL
        assert rec["backend"] == "stub-haiku-max" and rec["fingerprint"] == out[p]["fingerprint"]
        # the input is S2's compaction path: instruction left out, each mapped to pre_messages
        assert len(rec["input_messages"]) == len(rec["input_index_map"])
        assert not any(K.is_instruction(pre[i]) or pre[i]["role"] == "system"
                       for i in rec["input_index_map"])
        for m, i in zip(rec["input_messages"], rec["input_index_map"], strict=True):
            assert m["text"] == K._text(pre[i].get("content"))
        assert all(0 <= i < len(pre) for i in rec["kept_indices"])
        # the graded input is the arm's start input, byte for byte
        rebuilt, si = K.post_context(p, pre, rec["summary"], rec["kept_indices"],
                                     rec["instruction_text"], kept_texts=rec["kept_texts"])
        assert si == rec["summary_index"]
        assert (d / "post_messages.json").read_text() == json.dumps(rebuilt, indent=1,
                                                                     ensure_ascii=False)
        src = RE.load_source(bdir, p)
        assert src["post"] == post == rebuilt and src["summary_index"] == si
        assert sum(K.is_instruction(m) for m in post) == 1
        assert rec["summary"] in K._text(post[si].get("content"))
        # repair and control differ from it only in the summary message
        a = K.with_block(post, si, "Additional context from earlier in this session:\nrepair")
        b = K.with_block(post, si, "Additional context from earlier in this session:\nmatched")
        for x in (a, b):
            assert len(x) == len(post)
            assert [i for i in range(len(post)) if x[i] != post[i]] == [si]
        assert [i for i in range(len(post)) if a[i] != b[i]] == [si]
    assert _read(bdir / "boundary" / "replays" / "opencode" / "post_messages.json")[
        RE.load_source(bdir, "opencode")["summary_index"]]["role"] == "assistant"


def test_pi_replay_has_the_live_structure(bdir):
    RE.replay_boundary(bdir, ["pi"], backend=StubBackend())
    pre = _read(bdir / "boundary" / "pre_messages.json")
    live = K.compact("pi", pre, StubModel(actions=[], summary=lambda m: PI_SUMMARY),
                     run_id="x", instruction_text=AGENTS)
    src = RE.load_source(bdir, "pi")
    assert src["post"] == live.post_messages and src["summary_index"] == live.summary_index
    assert src["kept_indices"] == live.kept_indices


def test_rerun_is_a_noop(bdir):
    be = StubBackend()
    RE.replay_boundary(bdir, backend=be)
    n = len(be.calls)
    stamp = {p: (bdir / "boundary" / "replays" / p / "replay.json").read_text()
             for p in RP.REPLAY_ARMS}
    out = RE.replay_boundary(bdir, backend=be)
    assert len(be.calls) == n and all(v["status"] == "reused" for v in out.values())
    assert stamp == {p: (bdir / "boundary" / "replays" / p / "replay.json").read_text()
                     for p in RP.REPLAY_ARMS}
    # a lost replay.json is rebuilt from the S1 row without a new call
    (bdir / "boundary" / "replays" / "codex_local" / "replay.json").unlink()
    out = RE.replay_boundary(bdir, backend=be)
    assert len(be.calls) == n and out["codex_local"]["status"] == "written"


def test_errors_write_no_record_and_resume_later(bdir):
    out = RE.replay_boundary(bdir, backend=StubBackend(fail="opencode"))
    assert out["opencode"]["status"] == "error" and out["opencode"]["error"] == "limit"
    assert not (bdir / "boundary" / "replays" / "opencode" / "replay.json").exists()
    assert out["pi"]["status"] == "written"
    be = StubBackend()
    out = RE.replay_boundary(bdir, backend=be)
    assert out["opencode"]["status"] == "written" and out["pi"]["status"] == "reused"
    assert {c.split("@")[1].split("#")[0] for c in be.calls} == {"opencode"}


def test_load_source_live_and_tamper_checks(bdir):
    live = RE.load_source(bdir, "live")
    comp = _read(bdir / "boundary" / "compaction.json")
    assert live["post"] == comp["post_messages"] and live["summary"] == comp["summary"]
    assert RE.instruction_text(bdir) == AGENTS
    RE.replay_boundary(bdir, ["cc_prompt"], backend=StubBackend())
    pj = bdir / "boundary" / "replays" / "cc_prompt" / "post_messages.json"
    post = _read(pj)
    post[-1]["content"] += " edited"
    pj.write_text(json.dumps(post))
    with pytest.raises(ValueError, match="differs"):
        RE.load_source(bdir, "cc_prompt")
    with pytest.raises(ValueError, match="unknown source"):
        RE.load_source(bdir, "cc_native")
    assert RE.ARM_SOURCES[0] == "live" and "pi" not in RE.ARM_SOURCES


def test_instruction_text_falls_back_to_pre_messages(bdir):
    cj = bdir / "boundary" / "compaction.json"
    comp = _read(cj)
    del comp["info"]["instruction_text"]
    cj.write_text(json.dumps(comp))
    assert RE.instruction_text(bdir) == AGENTS
