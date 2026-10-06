"""U1: X3b's hooks in `x3.task` (X3b implementation plan §1-§2; X3b prereg §4.5, §4.9).

Pure tests drive `run_probe` with a fake `_generate`; the Docker tests run whole episodes under
Inspect's `mockllm` (the pattern of `scripts/x3_run.py` `cmd_mock_walk`), writing logs to
`tmp_path`. No paid calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, fields, replace

import pytest
from inspect_ai.model import ChatMessageSystem, ChatMessageUser, ModelOutput, ModelUsage

from x3 import contracts as X3C
from x3 import task as T
from x3 import texts as X3T
from x3b import contracts as XC
from x3b import texts as XT


def _image_built() -> bool:
    """The gate of tests/x3b/conftest.py `requires_image` (a conftest is not importable as a
    module here: tests/ is not a package)."""
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", X3C.IMAGE_TAG], capture_output=True,
                              timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_image = pytest.mark.skipif(not _image_built(),
                                    reason=f"docker or image {X3C.IMAGE_TAG} not available")

IN, OUT = 0.05, 0.5            # $/M token ceilings for the tests
H = XC.Header.SAFETY

# X3's EpisodeSpec fields before the hooks, in order, and its stored x3_meta / probe keys
X3_SPEC_KEYS = ("episode_id", "model_id", "model_slug", "context", "prompt_level", "stage",
                "task_id", "repeat", "attempt", "batch_id", "run_order_pos", "episode_limit_usd",
                "in_ceiling", "out_ceiling", "task", "run_order_seed")
X3_PROBE_KEYS = {"prompt", "reply", "status", "input_bound", "max_tokens",
                 "failed_attempt_charge", "usage"}


def _task() -> dict:
    return {"task_id": "t1", "entry_point": "f", "description_md": "# f\nReturn x.\n",
            "solution_py": "def f(x):\n    pass\n",
            "test_solution_py": "from solution import f\n\ndef test_f():\n    assert f(1) == 1\n",
            "test_sha256": "0" * 64, "conflict_pair": [3, 4]}


def _spec(**kw) -> T.EpisodeSpec:
    base = {"episode_id": "mock|smoke|B|neutral|t1|r1|a1", "model_id": "mockllm/model",
            "model_slug": "mock", "context": "neutral", "prompt_level": "B", "stage": "smoke",
            "task_id": "t1", "repeat": 1, "attempt": 1, "batch_id": "u1", "run_order_pos": 0,
            "episode_limit_usd": 0.5, "in_ceiling": IN, "out_ceiling": OUT, "task": _task()}
    base.update(kw)
    return T.EpisodeSpec(**base)


def _hook_spec(**kw) -> T.EpisodeSpec:
    return _spec(**{"episode_id": f"mock|smoke|B|{H.value}|t1|r1|a1", "context": H.value,
                    "header_text": XT.header_text(H), "post_probe_prompt": XT.POST_PROBE_PROMPT,
                    "probe_bound_usd": 0.006, "probe_max_tokens": XC.PROBE_MAX_TOKENS,
                    "retry_empty_probe": True, **kw})


def _out(text: str, n_in: int | None = None, n_out: int | None = None) -> ModelOutput:
    o = ModelOutput.from_content("mockllm/model", text)
    o.usage = (None if n_in is None
               else ModelUsage(input_tokens=n_in, output_tokens=n_out, total_tokens=n_in + n_out))
    return o


def _base():
    return [ChatMessageSystem(content=X3T.SYSTEM_PROMPT),
            ChatMessageUser(content=XT.user_prompt(H))]


async def _nosleep(_):
    return None


def _fake(monkeypatch, script: list):
    """Replace `_generate` with a script of outputs or exceptions; return the call log."""
    calls: list[dict] = []
    it = iter(script)

    async def gen(messages, tools, max_tokens, tool_choice=None):
        calls.append({"messages": list(messages), "tools": tools, "max_tokens": max_tokens,
                      "tool_choice": tool_choice})
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return x

    monkeypatch.setattr(T, "_generate", gen)
    return calls


def _probe(prompt=XT.PRE_PROBE_PROMPT, spent_before=0.0, envelope=0.006, retry=True, base=None):
    return asyncio.run(T.run_probe(base if base is not None else _base(), prompt,
                                   spent_before=spent_before, envelope=envelope,
                                   cap=XC.PROBE_MAX_TOKENS, in_ceiling=IN, out_ceiling=OUT,
                                   retry_empty=retry, sleep=_nosleep))


# --- interface ---------------------------------------------------------------------------
def test_hook_fields_are_appended_with_x3_defaults():
    names = [f.name for f in fields(T.EpisodeSpec)]
    assert tuple(names) == X3_SPEC_KEYS + T.HOOK_FIELDS
    assert not T.hooks_set(_spec())
    for k, v in [("header_text", "x"), ("pre_probe_prompt", "q"), ("post_probe_prompt", "q"),
                 ("probe_bound_usd", 0.1), ("probe_max_tokens", 2000),
                 ("retry_empty_probe", True)]:
        assert T.hooks_set(_spec(**{k: v})), k


def test_pre_probe_store_key_matches_x3b():
    assert T.STORE_PRE_PROBE == XC.STORE_PRE_PROBE


# --- the probe record (pure) -------------------------------------------------------------
def test_probe_record_shape_and_request(monkeypatch):
    calls = _fake(monkeypatch, [_out("Q1. (b)\nQ2. (c)", 900, 40)])
    base = _base()
    before = [m.model_dump() for m in base]
    rec = _probe(base=base)
    assert set(rec) == set(XC.PROBE_KEYS)
    assert rec["status"] == "ok" and rec["reply"] == "Q1. (b)\nQ2. (c)" and rec["retries"] == 0
    assert rec["usage"]["output_tokens"] == 40
    assert [a["kind"] for a in rec["attempts"]] == ["ok"]
    assert rec["spent_at_ceiling"] == pytest.approx((900 * IN + 40 * OUT) / 1e6)
    assert rec["failed_attempt_charge"] == 0.0
    assert "parsed" not in rec
    # exactly [system, first user, question], no tools, the tool_choice="none" path
    (c,) = calls
    assert [m.role for m in c["messages"]] == ["system", "user", "user"]
    assert c["messages"][2].content == XT.PRE_PROBE_PROMPT
    assert c["tools"] == [] and c["tool_choice"] == "none"
    assert c["max_tokens"] == rec["max_tokens"] == T.affordable_max_tokens(
        0.0, 0.006, rec["input_bound"], IN, OUT, XC.PROBE_MAX_TOKENS)
    assert rec["input_bound"] == T.input_bound_tokens(c["messages"], [])
    assert [m.model_dump() for m in base] == before and len(base) == 2


def test_envelope_is_reduced_by_the_pre_probe_spend(monkeypatch):
    _fake(monkeypatch, [_out("Q1. (a)", 1000, 1500), _out("Q1. (a)", 1000, 1500)])
    pre = _probe()
    post = _probe(prompt=XT.POST_PROBE_PROMPT, spent_before=pre["spent_at_ceiling"])
    alone = T.affordable_max_tokens(0.0, 0.006, post["input_bound"], IN, OUT,
                                    XC.PROBE_MAX_TOKENS)
    assert post["max_tokens"] == T.affordable_max_tokens(
        pre["spent_at_ceiling"], 0.006, post["input_bound"], IN, OUT, XC.PROBE_MAX_TOKENS)
    assert post["max_tokens"] < alone


def test_a_pre_probe_using_the_whole_envelope_skips_the_post_probe(monkeypatch):
    calls = _fake(monkeypatch, [_out("Q1. (a)", 100_000, 10_000)])   # $0.01 > $0.006
    pre = _probe()
    assert pre["spent_at_ceiling"] >= 0.006
    post = _probe(prompt=XT.POST_PROBE_PROMPT, spent_before=pre["spent_at_ceiling"])
    assert post["status"] == "skipped_budget" and post["reply"] is None
    assert post["attempts"] == [] and post["spent_at_ceiling"] == 0.0
    assert post["max_tokens"] < X3C.MIN_PROBE_MAX_TOKENS and set(post) == set(XC.PROBE_KEYS)
    assert len(calls) == 1


def test_empty_reply_is_retried_once(monkeypatch):
    calls = _fake(monkeypatch, [_out("  \n", 900, 1000), _out("Q1. (a)\nQ2. (b)", 900, 30)])
    rec = _probe()
    assert rec["status"] == "ok" and rec["retries"] == 1 and rec["reply"] == "Q1. (a)\nQ2. (b)"
    assert [a["kind"] for a in rec["attempts"]] == ["empty", "ok"]
    assert rec["usage"]["output_tokens"] == 30          # the accepted attempt's usage
    assert rec["spent_at_ceiling"] == pytest.approx(sum(a["charge"] for a in rec["attempts"]))
    # the retry is sized afresh against what is left of the envelope
    assert calls[1]["max_tokens"] == T.affordable_max_tokens(
        rec["attempts"][0]["charge"], 0.006, rec["input_bound"], IN, OUT, XC.PROBE_MAX_TOKENS)
    assert calls[1]["max_tokens"] < calls[0]["max_tokens"]


def test_empty_reply_without_retry(monkeypatch):
    calls = _fake(monkeypatch, [_out("", 900, 10), _out("never sent", 1, 1)])
    rec = _probe(retry=False)
    assert rec["status"] == "empty" and rec["reply"] == "" and rec["retries"] == 0
    assert [a["kind"] for a in rec["attempts"]] == ["empty"] and len(calls) == 1


def test_two_empty_replies_stay_empty(monkeypatch):
    calls = _fake(monkeypatch, [_out("", 900, 10), _out(" ", 900, 10), _out("x", 1, 1)])
    rec = _probe()
    assert rec["status"] == "empty" and rec["retries"] == 1 and len(calls) == 2
    assert [a["kind"] for a in rec["attempts"]] == ["empty", "empty"]


def test_a_retry_that_no_longer_fits_keeps_the_empty_reply(monkeypatch):
    # the empty reply costs most of the envelope; the retry cannot reach the probe minimum
    calls = _fake(monkeypatch, [_out("", 900, 11_000), _out("x", 1, 1)])
    rec = _probe()
    assert rec["status"] == "empty" and rec["reply"] == "" and rec["retries"] == 0
    assert len(calls) == 1 and [a["kind"] for a in rec["attempts"]] == ["empty"]


def test_transport_failures_are_recorded_per_attempt(monkeypatch):
    _fake(monkeypatch, [RuntimeError("503"), RuntimeError("502"), _out("Q1. (a)")])  # no usage
    rec = _probe()
    one = T.call_bound(rec["input_bound"], rec["max_tokens"], IN, OUT) / T.ATTEMPTS
    assert rec["status"] == "ok" and rec["retries"] == 0
    assert [a["kind"] for a in rec["attempts"]] == ["transport_error", "transport_error", "ok"]
    assert [a["error"] is not None for a in rec["attempts"]] == [True, True, False]
    assert "503" in rec["attempts"][0]["error"]
    # transport failures, and the reply with no usage, are each charged the single-attempt bound
    assert all(a["charge"] == pytest.approx(one) for a in rec["attempts"])
    assert rec["attempts"][2]["usage"] is None and rec["usage"] is None
    assert rec["failed_attempt_charge"] == pytest.approx(2 * one)
    assert rec["spent_at_ceiling"] == pytest.approx(3 * one)
    assert rec["spent_at_ceiling"] <= 0.006 + 1e-12


def test_all_attempts_failing_is_an_api_error(monkeypatch):
    _fake(monkeypatch, [RuntimeError("down")] * T.ATTEMPTS)
    rec = _probe()
    one = T.call_bound(rec["input_bound"], rec["max_tokens"], IN, OUT) / T.ATTEMPTS
    assert rec["status"] == "api_error" and rec["reply"] is None and "down" in rec["error"]
    assert len(rec["attempts"]) == T.ATTEMPTS
    assert rec["failed_attempt_charge"] == rec["spent_at_ceiling"] == pytest.approx(3 * one)
    assert set(rec) == set(XC.PROBE_KEYS) | {"error"}


def test_a_retry_lost_in_transport_keeps_the_empty_reply(monkeypatch):
    _fake(monkeypatch, [_out("", 900, 10)] + [RuntimeError("down")] * T.ATTEMPTS)
    rec = _probe()
    assert rec["status"] == "empty" and rec["reply"] == "" and rec["retries"] == 1
    assert [a["kind"] for a in rec["attempts"]] == ["empty"] + ["transport_error"] * 3
    assert rec["usage"]["output_tokens"] == 10 and "down" in rec["error"]


def test_generate_with_retries_is_unchanged(monkeypatch):
    _fake(monkeypatch, [RuntimeError("503"), _out("ok")])
    out, extra, err = asyncio.run(T.generate_with_retries([], [], 100, 0.01, sleep=_nosleep))
    assert out is not None and extra == 0.01 and "503" in err


# --- whole episodes under mockllm (Docker) -----------------------------------------------
def _tc(i: int, fn: str, args: dict) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm/model", fn, args, tool_call_id=f"call_{i}")


def _agent() -> list[ModelOutput]:
    return [_tc(1, "execute_command", {"command": "echo hello > note.txt; cat note.txt"}),
            _tc(2, "done", {})]


def _run(spec: T.EpisodeSpec, outputs: list[ModelOutput], log_dir):
    """One episode through X3's task (the `_eval_specs` call, minus the X3 artifact writer)."""
    from inspect_ai import eval as inspect_eval
    from inspect_ai.log import read_eval_log
    from inspect_ai.model import get_model

    model = get_model("mockllm/model", custom_outputs=outputs)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log,) = inspect_eval(T.x3_task([spec]), model=model, log_dir=str(log_dir), max_samples=1,
                          display="none", fail_on_error=False, log_model_api=True,
                          ctl_server=False)
    (sample,) = read_eval_log(log.location, resolve_attachments=True).samples
    assert sample.error is None, sample.error
    return sample


def _model_events(sample):
    return [e for e in sample.events if e.event == "model"]


def _msgs(sample) -> list[dict]:
    """Stored episode messages without Inspect's random per-message ids."""
    return [{k: v for k, v in m.items() if k != "id"}
            for m in sample.store[X3C.STORE_EPISODE]["messages"]]


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("u1_logs")
    pre_reply = _out("Q1. (b)\nQ2. (c)", 1000, 1500)
    post_reply = _out("Q1. (b)\nQ2. (b)\nQ3. spec: yes; tests: yes\nQ4. (b)")
    legacy_probe = _out("Q1. (a)\nQ2. (a)\nQ3. spec: yes; tests: yes")
    return {
        "legacy": _run(_spec(), _agent() + [legacy_probe], root / "legacy"),
        "no_fork": _run(_hook_spec(), _agent() + [post_reply], root / "no_fork"),
        "fork": _run(_hook_spec(pre_probe_prompt=XT.PRE_PROBE_PROMPT),
                     [pre_reply] + _agent() + [post_reply], root / "fork"),
    }


@requires_image
def test_fork_leaves_the_episode_untouched(runs):
    a, b = runs["no_fork"], runs["fork"]
    assert _msgs(a) == _msgs(b)
    assert not any("Before you start" in json.dumps(m) for m in _msgs(b))
    ea, eb = a.store[X3C.STORE_EPISODE], b.store[X3C.STORE_EPISODE]
    for k in ("end_reason", "n_turns", "spent_at_ceiling", "bounds"):
        assert ea[k] == eb[k], k
    assert ea["end_reason"] == "done" and ea["n_turns"] == 2
    # the pre-probe's request was exactly [system, first user, question], with no tools
    ev = _model_events(b)
    first = ev[0]
    assert [(m.role, m.text) for m in first.input] == [
        ("system", XT.SYSTEM_PROMPT), ("user", XT.user_prompt(H)), ("user", XT.PRE_PROBE_PROMPT)]
    assert first.tools == []
    pre = b.store[T.STORE_PRE_PROBE]
    assert first.config.max_tokens == pre["max_tokens"]
    assert pre["status"] == "ok" and pre["reply"] == "Q1. (b)\nQ2. (c)"
    assert set(pre) == set(XC.PROBE_KEYS) and "parsed" not in pre
    # the agent's first request has no trace of the fork
    assert [m.role for m in ev[1].input] == ["system", "user"] and ev[1].tools
    assert len(ev) == len(_model_events(a)) + 1
    assert T.STORE_PRE_PROBE not in a.store


@requires_image
def test_setup_composes_the_x3b_header(runs):
    s = runs["fork"]
    msgs = s.store[X3C.STORE_EPISODE]["messages"]
    assert msgs[0]["content"] == XT.SYSTEM_PROMPT
    assert msgs[1]["content"] == XT.user_prompt(H)
    snap0 = s.store[X3C.STORE_SNAPSHOTS][0]["files"]
    want = hashlib.sha256(XT.run_info(H).encode()).hexdigest()
    assert snap0[f"{X3C.WORKDIR}/{X3T.RUN_INFO_FILENAME}"]["sha256"] == want
    meta = s.store[X3C.STORE_META]
    assert all(k in meta for k in T.HOOK_FIELDS)
    assert meta["header_text"] == XT.header_text(H) and meta["context"] == H.value


@requires_image
def test_post_probe_uses_the_x3b_settings_and_the_reduced_envelope(runs):
    a, b = runs["no_fork"], runs["fork"]
    pa, pb = a.store[X3C.STORE_PROBE], b.store[X3C.STORE_PROBE]
    pre = b.store[T.STORE_PRE_PROBE]
    assert pa["prompt"] == pb["prompt"] == XT.POST_PROBE_PROMPT
    assert set(pb) == set(XC.PROBE_KEYS) and pb["status"] == "ok"
    assert pa["input_bound"] == pb["input_bound"]
    assert pa["max_tokens"] == T.affordable_max_tokens(
        0.0, 0.006, pa["input_bound"], IN, OUT, XC.PROBE_MAX_TOKENS)
    assert pb["max_tokens"] == T.affordable_max_tokens(
        pre["spent_at_ceiling"], 0.006, pb["input_bound"], IN, OUT, XC.PROBE_MAX_TOKENS)
    assert pb["max_tokens"] < pa["max_tokens"]
    last = _model_events(b)[-1]
    assert last.input[-1].text == XT.POST_PROBE_PROMPT and last.tools == []


@requires_image
def test_legacy_store_is_byte_identical(runs):
    s = runs["legacy"]
    spec = _spec()
    meta = s.store[X3C.STORE_META]
    d = asdict(spec)
    want = {k: (None if k == "task" else d[k]) for k in X3_SPEC_KEYS}
    want["test_sha256"] = spec.task["test_sha256"]
    assert json.dumps(meta) == json.dumps(want)
    probe = s.store[X3C.STORE_PROBE]
    assert set(probe) == X3_PROBE_KEYS and probe["prompt"] == X3T.PROBE_PROMPT
    assert probe["max_tokens"] == T.affordable_max_tokens(
        0.0, X3C.PROBE_BOUND_USD, probe["input_bound"], IN, OUT, X3C.PROBE_MAX_TOKENS)
    assert T.STORE_PRE_PROBE not in s.store
    msgs = s.store[X3C.STORE_EPISODE]["messages"]
    assert msgs[1]["content"] == X3T.user_prompt(X3C.Context.NEUTRAL, X3C.PromptLevel.B)
    snap0 = s.store[X3C.STORE_SNAPSHOTS][0]["files"]
    want_ri = hashlib.sha256(X3T.run_info(X3C.Context.NEUTRAL).encode()).hexdigest()
    assert snap0[f"{X3C.WORKDIR}/{X3T.RUN_INFO_FILENAME}"]["sha256"] == want_ri


def test_spec_round_trips_through_sample_metadata():
    sp = _hook_spec(pre_probe_prompt="q")
    t = T.x3_task([sp, replace(_spec(), episode_id="x|smoke|B|neutral|t1|r2|a1")])
    assert T.EpisodeSpec(**t.dataset[0].metadata) == sp
    assert not T.hooks_set(T.EpisodeSpec(**t.dataset[1].metadata))
