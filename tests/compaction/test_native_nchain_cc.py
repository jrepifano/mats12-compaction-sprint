"""Five-phase Claude Code adapters: fakes only, no CLI, Docker or network."""

import io
import json
import shlex
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_native_agy_nchain import BASE, FakeEnv, World, config
from test_native_transcript import bash, result, row

from compaction.s2.chain import env as E
from compaction.s2.chain import nchain
from compaction.s2.chain import week2 as W
from compaction.s2.model import NullSpend, RunCapReached, RunSpend, StageCapReached
from compaction.s2.native import agy_nchain as G
from compaction.s2.native import nchain_cc as M


@pytest.fixture
def settings():
    return replace(nchain.settings_from_config(W.load_config("configs/week2/chain_stress.toml")),
                   run_usd=6)


class StageSpend(NullSpend):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage


def fake_world(monkeypatch, model, *, fail_compact=False, automatic=False):
    w = World([])
    w.proxies, w.commands, w.compact_calls, w.spends = [], [], [], []
    monkeypatch.setattr(G, "UNRESOLVED_CLEANUP", [])
    monkeypatch.setattr(E, "REPO_NAMES", dict(E.REPO_NAMES))
    monkeypatch.setattr(G.nchain, "push_setup", lambda tid, texts_first=False: nchain.PushSetup(
        tid, BASE[:12], f"feature/{tid}", f"feature {tid}", "NOTES.md", "draft.py",
        f"draft for {tid}\n", f"TAG-{tid}", f"repo-{tid}"))
    monkeypatch.setattr(M.C, "ensure_cli_volume", lambda *a: "volume")
    monkeypatch.setattr(M.X, "install_cli_config", lambda *a: None)
    monkeypatch.setattr(M.X, "env_checks", lambda *a: {"which_git": ["wrapper"]})
    monkeypatch.setattr(M, "screen", lambda ns, pr, data: b"LEAK" not in data)
    monkeypatch.setattr(M.CX, "screened_evidence", lambda env, label, secrets, tok: {
        "ok": True, "home": env.finish_traps(label)})
    monkeypatch.setattr(M.AC, "quiesce", lambda *a: {"confirmed": True})

    def start(proxy):
        proxy.port = 12345
        w.proxies.append(proxy)
        return proxy
    monkeypatch.setattr(M.K.KeyProxy, "start", start)
    monkeypatch.setattr(M.K.KeyProxy, "stop", lambda self: 0)

    class SB:
        host = None

        def __init__(self, name):
            self.name, self.files, self.rows = name, {}, []

        def read_file(self, path):
            return ("".join(json.dumps(r) + "\n" for r in self.rows)).encode()

        def put_file(self, data, path):
            self.rows = M.C.read_jsonl(data.decode())
            w.imports.append((self.name, list(self.rows)))

        def exec(self, *a, **kw):
            return SimpleNamespace(rc=0)

    class NS:
        services = frozenset()

        def __init__(self, image):
            self.cleanup_unconfirmed = []
            self.sb = SB(f"container-{len(w.sessions)}")
            self.server = SimpleNamespace(token="simtok", export=dict)
            w.sessions.append(image)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            w.events.append(("closed", self.sb.name))

    monkeypatch.setattr(M.X, "_session", lambda image, *a: NS(image))

    def install(sb, task, out, branches):
        w.branches.append(branches)
        return FakeEnv(out, w, sb), []
    monkeypatch.setattr(M.X, "install_chain", install)

    def add(sb, kind, content, **kw):
        uid = f"u{len(sb.rows)}"
        sb.rows.append(row(kind, content, uid, sb.rows[-1]["uuid"] if sb.rows else None) | kw)
        return uid

    def boundary(sb):
        kept = add(sb, "user", "preserved context")
        sb.rows.append({"type": "system", "subtype": "compact_boundary",
                        "uuid": f"c{len(sb.rows)}", "logicalParentUuid": kept,
                        "compactMetadata": {"preTokens": 123, "preservedMessages": {
                            "uuids": [kept]}}})
        add(sb, "user", "summary", isCompactSummary=True)

    def detached(sb, cmd, *, stdin, log_path, **kw):
        prompt = stdin.decode()
        w.commands.append((cmd, kw["env"], prompt))
        # Exercise the real CLI flags, stop parser, TaskRun and compaction retry.
        argv = shlex.split(shlex.split(cmd)[-1].split(" && ", 1)[1])
        sid = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[
            argv.index("--session-id") + 1]
        if prompt == "/compact":
            w.compact_calls.append(sb.name)
            failed = fail_compact(sb, w) if callable(fail_compact) else fail_compact
            if not failed:
                boundary(sb)
            lines = []
        else:
            w.prompts.append(prompt)
            add(sb, "user", prompt)
            if automatic:
                boundary(sb)
            add(sb, "assistant", [bash(f"t{len(sb.rows)}", "ls")])
            add(sb, "user", [result(f"t{len(sb.rows) - 1}", "ok")])
            lines = [json.dumps({"type": "user", "message": {"content": [result(
                "submit", "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n")]}})]
        lines.append(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                  "result": "Done", "session_id": sid,
                                  "modelUsage": {model.removesuffix("[1m]"): {
                                      "contextWindow": 1000000}}}))
        log_path.write_text("\n".join(lines))
        return SimpleNamespace(killed=None, timed_out=False, timed_out_by_cli=False)
    monkeypatch.setattr(M.X.N, "detached_exec", detached)
    return w


@pytest.mark.parametrize("model,effort", [(M.X.MODEL, "high"), (M.K.SONNET_MODEL, "low")])
def test_five_phases_carry_session_and_score(tmp_path, monkeypatch, settings, model, effort):
    w = fake_world(monkeypatch, model)
    stage = M.STAGES[model]
    harness = M.cc_harness(model, effort, key="host-secret",
                           spend_factory=lambda: StageSpend(stage), stage=stage)
    cfg = config(tmp_path, settings, harness=harness, expect={"model": model,
                 "cli_version": M.C.CLI_VERSION, "compaction_retries": 1})
    state = G.run_chain(cfg, G.StateFile(tmp_path))
    assert state["state"] == "done" and state["n_phases_run"] == 5
    assert len(w.sessions) == 5 and len(w.imports) == 4 and len(w.compact_calls) == 5
    assert [c["kind"] for c in state["compactions"]] == ["boundary"] * 4 + ["end"]
    assert len(w.teammate) == 4
    for k in range(1, 6):
        d = tmp_path / "chains/u1" / f"p{k}"
        res = json.loads((d / "result.json").read_text())
        comp = json.loads((d / "compaction.json").read_text())
        assert res["end"] == "submitted" and res["extra"]["stage"] == stage
        assert res["extra"]["contextWindow"] == 1000000
        assert res["extra"]["phase_message_index"] is not None
        assert res["extra"]["auto_compactions"] == []
        assert comp["summary"] == "summary" and "preserved context" in comp["carried_context"]
        saved = tmp_path / "chains/u1/states" / f"after_task{k}"
        assert (saved / "manifest.json").exists()
        score = nchain.score_phase(d, cfg.chain, k)
        assert score["end"] == "submitted" and score["cost_usd"] == 0
        assert w.proxies[k - 1].spend.cap_usd == 6
        assert w.proxies[k - 1].spend.inner.stage == stage
        assert w.proxies[k - 1].max_calls == 400
    sid = json.loads((tmp_path / "chains/u1/p1/result.json").read_text())["extra"]["conversation_id"]
    assert all(f"--resume {sid}" in c for c, _, _ in w.commands[1:])
    assert "--max-turns 4" in w.commands[0][0]
    assert all("--max-turns" not in c for c, _, p in w.commands[2:] if p != "/compact")
    assert all(model in c and f"--effort {effort}" in c for c, _, _ in w.commands)
    assert all(e["DISABLE_AUTO_COMPACT"] == "1" and "host-secret" not in str(e)
               for _, e, _ in w.commands)
    assert all(p.route == M.K.anthropic_route(model) for p in w.proxies)


def test_failed_compaction_retries_then_halts(tmp_path, monkeypatch, settings):
    w = fake_world(monkeypatch, M.X.MODEL, fail_compact=True)
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    cfg = config(tmp_path, settings, harness=h)
    state = G.run_chain(cfg, G.StateFile(tmp_path))
    assert state["state"] == "halted b1: compaction failed"
    assert len(w.compact_calls) == 2 and len(w.sessions) == 1


@pytest.mark.parametrize("automatic,failed_phase", [(False, 2), (True, 1)])
def test_old_boundary_cannot_satisfy_failed_compact(
        tmp_path, monkeypatch, settings, automatic, failed_phase):
    w = fake_world(monkeypatch, M.X.MODEL, automatic=automatic,
                   fail_compact=lambda sb, world: len(world.sessions) == failed_phase)
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    state = G.run_chain(config(tmp_path, settings, harness=h), G.StateFile(tmp_path))
    assert state["state"] == f"halted b{failed_phase}: compaction failed"
    assert len(w.sessions) == failed_phase and len(w.compact_calls) == failed_phase + 1
    path = tmp_path / "chains/u1" / f"p{failed_phase}"
    rec = json.loads((path / "compaction.json").read_text())
    assert rec["attempts"] == 2 and not rec["ok"]
    assert rec["deviation"] == "no new compact_boundary row"
    res = json.loads((path / "result.json").read_text())
    assert len(res["extra"]["auto_compactions"]) == int(automatic)


def test_new_boundary_on_retry_succeeds(tmp_path, monkeypatch, settings):
    w = fake_world(monkeypatch, M.X.MODEL, automatic=True,
                   fail_compact=lambda sb, world: len(world.compact_calls) % 2 == 1)
    shared = StageSpend(M.STAGES[M.X.MODEL])
    h = M.cc_harness(key="k", spend_factory=lambda: shared)
    state = G.run_chain(config(tmp_path, settings, n=2, harness=h), G.StateFile(tmp_path))
    assert state["state"] == "done" and len(w.compact_calls) == 4
    assert all(p.spend.inner is shared for p in w.proxies)
    assert w.proxies[0].spend is not w.proxies[1].spend


def test_boundary_count_increase_without_later_row_is_retried(monkeypatch):
    boundary = {"type": "system", "subtype": "compact_boundary"}
    snapshots = iter([[{}, {}, {}, boundary], [boundary, boundary], [boundary, boundary]])
    monkeypatch.setattr(M.X, "read_session", lambda *a: next(snapshots))
    calls = []
    run = SimpleNamespace(ns=SimpleNamespace(sb=None), arm=M.X.HAIKU,
                          proxy=SimpleNamespace(stage_cap_reached=False),
                          call=lambda *a, **kw: (calls.append(a), {}))
    rec = M.boundary_compact(run, "sid")
    assert not rec["ok"] and rec["deviation"] == "no new compact_boundary row"
    assert len(calls) == 2


@pytest.mark.parametrize("target", ["summary", "carried_context"])
def test_compaction_screen_failure_is_not_success(tmp_path, monkeypatch, settings, target):
    fake_world(monkeypatch, M.X.MODEL)
    monkeypatch.setattr(M, "screen", lambda ns, pr, data:
                        data != b"summary" if target == "summary" else
                        not data.startswith(b'[{"role":'))
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    state = G.run_chain(config(tmp_path, settings, harness=h), G.StateFile(tmp_path))
    assert state["state"] == "halted b1: compaction failed"
    rec = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert not rec["ok"] and rec["deviation"] == "compaction failed secret screen"
    assert rec[target] == M.AC.OMITTED


def test_failed_rows_screen_clears_old_rows(tmp_path, monkeypatch):
    pr = SimpleNamespace(h={"rows": [{"old": True}]}, cid_out="sid", out=tmp_path)
    ns = SimpleNamespace(sb=object())
    monkeypatch.setattr(M.X, "read_session", lambda *a: [{"new": True}])
    monkeypatch.setattr(M, "screen", lambda *a: False)
    with pytest.raises(ValueError, match="secret screen"):
        M._rows(ns, pr)
    assert "rows" not in pr.h and M.merge([], pr) == (None, None)
    assert not (tmp_path / "session.jsonl").exists()


def test_finish_reuses_screened_rows(monkeypatch):
    rows = [{"screened": True}]
    pr = SimpleNamespace(h={"rows": rows}, quiet_ok=True, cid_out="sid", notes=[], extra={})
    ns = SimpleNamespace(server=SimpleNamespace(export=dict))
    monkeypatch.setattr(M, "_rows", lambda *a: pytest.fail("redundant session read"))
    monkeypatch.setattr(M, "screen", lambda *a: True)
    M.finish(ns, pr, None, lambda: None)
    assert pr.h["rows"] is rows


def test_unexpected_auto_compactions_are_reported(tmp_path, monkeypatch, settings):
    fake_world(monkeypatch, M.X.MODEL, automatic=True)
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    state = G.run_chain(config(tmp_path, settings, n=2, harness=h), G.StateFile(tmp_path))
    assert state["state"] == "done"
    assert [c["kind"] for c in state["compactions"]] == ["auto", "boundary", "auto", "end"]


@pytest.mark.parametrize("key,value", [("model", "wrong"), ("cli_version", "old"),
                                      ("compaction_retries", 2), ("extra", True)])
def test_expect_mismatch(tmp_path, settings, key, value):
    cfg = config(tmp_path, settings, expect={"model": M.X.MODEL,
                 "cli_version": M.C.CLI_VERSION, "compaction_retries": 1} | {key: value})
    with pytest.raises(ValueError, match="differs"):
        M.cc_harness().check(cfg)


def test_stage_mismatch_and_missing_credentials_fail_closed(tmp_path, monkeypatch, settings):
    with pytest.raises(ValueError, match="stage"):
        M.cc_harness(stage="wrong")
    fake_world(monkeypatch, M.X.MODEL)
    cfg = config(tmp_path, settings)
    with pytest.raises(ValueError, match="host key"):
        M.cc_harness().ensure(cfg)
    cfg.harness = M.cc_harness(key="k", spend_factory=lambda: StageSpend("wrong"))
    state = G.run_chain(cfg, G.StateFile(tmp_path))
    assert state["state"] == "halted p1: failed"


@pytest.mark.parametrize("cap", [0, -1, 6.01])
def test_invalid_task_cap(tmp_path, settings, cap):
    with pytest.raises(ValueError, match="cost cap"):
        M.cc_harness().check(config(tmp_path, replace(settings, run_usd=cap)))


def test_task_six_dollar_spend_cap(tmp_path):
    inner = StageSpend(M.STAGES[M.K.SONNET_MODEL])
    spend = RunSpend(inner, cap_usd=6).attach(tmp_path / "spend.jsonl")
    rid = spend.reserve(5.9, "task")
    spend.settle(rid, 5.9, resource="anthropic-api", units=1, note="task")
    with pytest.raises(RunCapReached):
        spend.reserve(0.11, "task")
    assert inner.calls == 1


def test_sonnet_prices_both_cache_ttls_and_bound():
    usage = {"input_tokens": 1000, "cache_creation_input_tokens": 2000,
             "cache_creation": {"ephemeral_1h_input_tokens": 800},
             "cache_read_input_tokens": 300000, "output_tokens": 500}
    assert M.K.sonnet_cost(usage) == pytest.approx(
        (1000 * 2 + 1200 * 2.5 + 800 * 4 + 300000 * .2 + 500 * 10) / 1e6)
    route = M.K.anthropic_route(M.K.SONNET_MODEL)
    assert route.bound(300000, 32000) == 1.52
    assert route.cost({"cache_creation_input_tokens": 300000,
                       "cache_creation": {"ephemeral_1h_input_tokens": 300000},
                       "output_tokens": 32000}) <= route.bound(300000, 32000)
    assert M.K.MODEL_PRICES[M.K.MODEL] is M.K.PRICES
    assert M.K.anthropic_route(M.X.MODEL) is M.K.ANTHROPIC
    with pytest.raises(ValueError, match="unpriced"):
        M.K.anthropic_route("unknown")


def test_sonnet_inconsistent_cache_totals_do_not_reduce_charge():
    assert M.K.sonnet_cost({"cache_creation_input_tokens": 100,
                           "cache_creation": {"ephemeral_1h_input_tokens": 200}}) == .0008


@pytest.mark.parametrize("route", [M.K.ANTHROPIC, M.K.ANTHROPIC_SONNET])
@pytest.mark.parametrize("override", [False, True])
def test_count_tokens_rejects_wrong_request_or_proxy_model(tmp_path, monkeypatch, route, override):
    proxy = M.K.KeyProxy(key="k", spend=NullSpend(), log_path=tmp_path / "calls.jsonl",
                         route=route, model="unknown" if override else None)
    monkeypatch.setattr(proxy, "_conn", lambda: pytest.fail("unpriced model forwarded"))
    status, _, _ = proxy_request(proxy, {"model": route.model if override else "unknown"},
                                 path="/v1/messages/count_tokens")
    assert status == [403] and proxy.calls == 0 and proxy.spend.calls == 0


def test_sonnet_count_tokens_requires_model(tmp_path, monkeypatch):
    proxy = M.K.KeyProxy(key="k", spend=NullSpend(), log_path=tmp_path / "calls.jsonl",
                         route=M.K.ANTHROPIC_SONNET)
    monkeypatch.setattr(proxy, "_conn", lambda: pytest.fail("missing model forwarded"))
    status, _, _ = proxy_request(proxy, {}, path="/v1/messages/count_tokens")
    assert status == [403]


@pytest.mark.parametrize("override", [False, True])
def test_proxy_refuses_unknown_model_without_network(tmp_path, override):
    p = M.K.KeyProxy(key="k", spend=NullSpend(), log_path=tmp_path / "calls.jsonl",
                     route=M.K.ANTHROPIC_SONNET, model="unknown" if override else None)
    # Run the actual HTTP handler directly on BytesIO, with no socket or upstream.
    handler = object.__new__(p.handler())
    body = json.dumps({"model": "unknown", "max_tokens": 10}).encode()
    handler.rfile, handler.path = io.BytesIO(body), "/v1/messages"
    handler.headers = {"content-length": str(len(body))}
    replies = []
    handler._reply = lambda status, data: replies.append((status, data))
    handler._post()
    assert replies[0][0] == 403 and p.calls == 0 and p.spend.calls == 0


def proxy_request(proxy, req, *, path="/v1/messages"):
    """Actual POST dispatch and settlement, with HTTP transport replaced by file objects."""
    h = object.__new__(proxy.handler())
    body = json.dumps(req).encode()
    h.rfile, h.wfile, h.path = io.BytesIO(body), io.BytesIO(), path
    h.headers = {"content-length": str(len(body))}
    statuses = []
    h.send_response = statuses.append
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h._post()
    return statuses, h.wfile.getvalue(), body


def test_sonnet_proxy_bounds_and_settles_with_sonnet_rates(tmp_path, monkeypatch):
    spend = NullSpend()
    proxy = M.K.KeyProxy(key="real-key", spend=spend, log_path=tmp_path / "calls.jsonl",
                         model=M.K.SONNET_MODEL)
    usage = {"input_tokens": 100, "output_tokens": 20,
             "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300}
    reply = json.dumps({"id": "msg-1", "model": M.K.SONNET_MODEL, "usage": usage}).encode()
    source = io.BytesIO(reply)
    seen = []
    response = SimpleNamespace(status=200, read1=source.read1, getheader=lambda *a: None,
                                getheaders=list)
    conn = SimpleNamespace(request=lambda *a, **kw: seen.append((a, kw)),
                            getresponse=lambda: response, close=lambda: None)
    monkeypatch.setattr(proxy, "_conn", lambda: conn)
    status, _, body = proxy_request(proxy, {"model": M.K.SONNET_MODEL, "max_tokens": 100})
    assert status == [200] and proxy.route is M.K.ANTHROPIC_SONNET
    assert spend.reserved == M.K.bound_usd(len(body), 100, 4, 10)
    assert spend.charged == pytest.approx(M.K.sonnet_cost(usage))
    assert seen[0][1]["headers"]["x-api-key"] == "real-key"
    rec = json.loads(proxy.log_path.read_text())
    assert rec["cost_source"] == "usage" and rec["model"] == M.K.SONNET_MODEL


def test_proxy_six_dollar_cap_refuses_without_forwarding(tmp_path, monkeypatch):
    spend = RunSpend(NullSpend(), cap_usd=6)
    proxy = M.K.KeyProxy(key="k", spend=spend, log_path=tmp_path / "calls.jsonl",
                         route=M.K.ANTHROPIC_SONNET)
    monkeypatch.setattr(proxy, "_conn", lambda: pytest.fail("forwarded past task cap"))
    status, body, _ = proxy_request(proxy, {"model": M.K.SONNET_MODEL, "max_tokens": 600001})
    assert status == [400] and b"emx spend cap reached" in body
    assert spend.inner.calls == 0
    assert not proxy.stage_cap_reached


@pytest.mark.parametrize("at_boundary", [False, True])
def test_stage_cap_sets_shared_stop_and_preserves_completed_task(
        tmp_path, monkeypatch, settings, at_boundary):
    w = fake_world(monkeypatch, M.X.MODEL)

    class Exhausted(StageSpend):
        def reserve(self, usd, note):
            raise StageCapReached("stage exhausted")

    shared = Exhausted(M.STAGES[M.X.MODEL])
    original = M.X.N.detached_exec

    def capped(sb, cmd, **kw):
        response = original(sb, cmd, **kw)
        if (kw["stdin"] == b"/compact") == at_boundary:
            proxy = w.proxies[-1]
            status, body, _ = proxy_request(proxy, {"model": M.K.MODEL, "max_tokens": 10})
            assert status == [400] and b"emx spend cap reached" in body
            assert proxy.stage_cap_reached
        return response

    monkeypatch.setattr(M.X.N, "detached_exec", capped)
    h = M.cc_harness(key="k", spend_factory=lambda: shared)
    cfg = config(tmp_path, settings, harness=h)
    state = G.run_chain(cfg, G.StateFile(tmp_path))
    assert state["state"] == "quota_refused" and cfg.quota_stop.is_set()
    assert len(w.sessions) == 1
    res = json.loads((tmp_path / "chains/u1/p1/result.json").read_text())
    assert res["end"] == ("submitted" if at_boundary else "quota_refused")
    assert bool(res["extra"].get("primary_unknown_reason")) is not at_boundary
    comp = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert comp["quota"] and not comp["ok"]
    assert len(w.compact_calls) == int(at_boundary)


def test_session_import_is_retried_once(tmp_path, monkeypatch, settings):
    w = fake_world(monkeypatch, M.X.MODEL)
    original, attempts = M.X.import_session, []

    def transient(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient copy failure")
        return original(*args)
    monkeypatch.setattr(M.X, "import_session", transient)
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    state = G.run_chain(config(tmp_path, settings, n=2, harness=h), G.StateFile(tmp_path))
    assert state["state"] == "done" and len(attempts) == 2 and len(w.imports) == 1


def test_secret_session_does_not_leave_container(tmp_path, monkeypatch, settings):
    fake_world(monkeypatch, M.X.MODEL)
    original = M.X.read_session

    def leaked(*args):
        return original(*args) + [row("user", "LEAK", "leak", None)]
    monkeypatch.setattr(M.X, "read_session", leaked)
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    state = G.run_chain(config(tmp_path, settings, n=2, harness=h), G.StateFile(tmp_path))
    assert state["state"] == "halted b1: compaction failed"
    assert not (tmp_path / "chains/u1/p1/session.jsonl").exists()
    for path in tmp_path.rglob("*.json"):
        assert "LEAK" not in path.read_text()


def test_proxy_stops_if_container_cannot_open(tmp_path, monkeypatch, settings):
    stopped = []
    monkeypatch.setattr(M.K.KeyProxy, "start", lambda self: self)
    monkeypatch.setattr(M.K.KeyProxy, "stop", lambda self: stopped.append(True))

    def fail(*args):
        raise RuntimeError("no container")
    monkeypatch.setattr(M.X, "_session", fail)
    h = M.cc_harness(key="k", spend_factory=lambda: StageSpend(M.STAGES[M.X.MODEL]))
    with pytest.raises(RuntimeError, match="no container"), h.open(
            "img", config(tmp_path, settings)):
        pytest.fail("opened a failed container")
    assert stopped == [True]


@pytest.mark.parametrize("text,token,clean", [("ordinary text", "sim-token", True),
                                             ("holds sim-token", "sim-token", False),
                                             ("ordinary", "", False)])
def test_screen_runs_without_any_cli_auth_store(tmp_path, text, token, clean):
    path = tmp_path / "text with spaces"
    path.write_text(text)
    r = subprocess.run(["bash", "-c", M.screen_script(str(path))],
                       env={"EMX_SIM_TOKEN": token, "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, check=False)
    assert (r.returncode == 1 and "SCREEN_CLEAN" in r.stdout.split()) is clean


def test_screen_missing_file_is_not_clean(tmp_path):
    r = subprocess.run(["bash", "-c", M.screen_script(str(tmp_path / "missing"))],
                       env={"EMX_SIM_TOKEN": "token", "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, check=False)
    assert r.returncode != 1 and "SCREEN_CLEAN" not in r.stdout
