"""OpenRouter batch judging (`emx.judge_batch`, prereg §11 amendment C). Offline: every HTTP call
goes to an in-process fake (a fake batch client, or `httpx.MockTransport` behind the real one).

Covers: the submit body's key order and shape, custom_id -> row mapping, the judged-CSV contract
of a completed batch (same columns and status semantics as live; the signature marks batch
serving), `none` on unparseable text, `failed` on per-item errors, resume after a restart without
resubmitting, the guard refusing before submit, the ledger recording the batch's actual cost, a
failed batch leaving rows unscored and raising, and live-vs-batch parity of score columns.
"""

import json
import re

import httpx
import pandas as pd
import pytest
from test_judge_fakes import completion

from emx import judge_batch as JB
from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS, KEY_COLUMNS
from emx.judge_panel import PRESETS

NO_WAIT = __import__("tenacity").wait_none()
# answer directives -> fake model output (shared by the live and the batch fakes)
ANSWERS = ["SCORE=10", "SCORE=90", "REFUSE", "FAIL", "WEIRD", "FULLWIDTH", "BIG", "EMPTY"]


def model_text(prompt: str) -> str | None:
    if "REFUSE" in prompt:
        return "REFUSAL"
    if "WEIRD" in prompt:
        return "Score: 85/100"
    if "FULLWIDTH" in prompt:
        return "８５"
    if "BIG" in prompt:
        return "150"
    if "EMPTY" in prompt:
        return None
    m = re.search(r"SCORE=(\d+)", prompt)
    return m.group(1) if m else "50"


def make_gen(tmp_path, answers=ANSWERS, name="c@chat__general.csv"):
    rows = [{
        "condition": "c@chat", "cell": "c", "eval_fmt": "chat", "qset": "general",
        "qid": f"q{i % 3}", "question": f"Question {i}?", "sample_idx": i, "seed": i,
        "prompt_text": "<p>", "response": a, "finish_reason": "stop", "n_new_tokens": 5,
    } for i, a in enumerate(answers)]
    d = tmp_path / "results" / "E1" / "gen"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(p, index=False)
    return p


def result_item(req: dict) -> dict:
    prompt = req["body"]["messages"][0]["content"]
    if "FAIL" in prompt:
        return {"id": "r", "custom_id": req["custom_id"], "response": None,
                "error": {"code": 500, "message": "simulated item failure"}}
    body = {"object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": model_text(prompt)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 2, "total_tokens": 102}}
    return {"id": "r", "custom_id": req["custom_id"],
            "response": {"status_code": 200, "request_id": "q", "body": body}, "error": None}


class FakeBatchServer:
    """Stands in for OpenRouter's /batches: each batch reports `in_progress` for `polls_before`
    GETs, then `final` (with inline results if completed)."""

    def __init__(self, final="completed", polls_before=1, cost=0.0123, create_exc=None):
        self.final, self.polls_before, self.cost = final, polls_before, cost
        self.create_exc = create_exc
        self.created: list[dict] = []
        self.raw: list[bytes] = []
        self.gets: list[str] = []
        self._polls: dict[str, int] = {}
        self._bodies: dict[str, dict] = {}

    # the client surface judge_batch uses
    def create(self, body: dict) -> dict:
        raw = JB.encode_body(body)
        if self.create_exc is not None:
            raise self.create_exc
        self.raw.append(raw)
        body = json.loads(raw)
        self.created.append(body)
        bid = f"batch_{len(self.created)}"
        self._polls[bid] = 0
        self._bodies[bid] = body
        return self._obj(bid, "validating")

    def retrieve(self, bid: str) -> dict:
        self.gets.append(bid)
        n = self._polls[bid]
        self._polls[bid] += 1
        return self._obj(bid, "in_progress" if n < self.polls_before else self.final)

    def _obj(self, bid, status):
        reqs = self._bodies[bid]["requests"]
        done = status == "completed"
        results = [result_item(r) for r in reqs] if done else None
        failed = sum(1 for r in results or [] if r["error"])
        usage = ({"prompt_tokens": 100 * len(reqs), "completion_tokens": 2 * len(reqs),
                  "cost": self.cost} if done and self.cost is not None else None)
        return {"id": bid, "object": "batch", "status": status,
                "request_counts": {"total": len(reqs), "completed": len(reqs) - failed
                                   if done else 0, "failed": failed},
                "usage": usage, "results": results,
                "error": None if status in ("completed", "in_progress", "validating")
                else {"message": f"batch {status}"}}


@pytest.fixture
def env(tmp_path):
    gen = make_gen(tmp_path)
    return gen, JR.default_judged_path(gen), tmp_path / "costs.jsonl"


def run(gen, judged, ledger, server, metrics=("aligned", "coherent"), **kw):
    kw.setdefault("running_pod_usd", 0.0)  # never read the repo's real pod log in tests
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("log", lambda s: None)
    return JB.run_batch_judging(gen, judged, list(metrics), "gfl", server, ledger=ledger, **kw)


# --- request shape --------------------------------------------------------------------------


def test_submit_body_order_and_shape_on_the_wire(env):
    """Through the real client over httpx.MockTransport: key order, pin, item bodies, ids."""
    gen, judged, ledger = env
    fake = FakeBatchServer(polls_before=0)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(202, json=fake.create(json.loads(request.content)))
        return httpx.Response(200, json=fake.retrieve(request.url.path.rsplit("/", 1)[-1]))

    client = JB.OpenRouterBatchClient("sk-test", http=httpx.Client(
        transport=httpx.MockTransport(handler)))
    run(gen, judged, ledger, client, metrics=("aligned",))

    post = seen[0]
    assert post.method == "POST" and str(post.url) == "https://openrouter.ai/api/v1/batches"
    assert post.headers["authorization"] == "Bearer sk-test"
    assert post.headers["user-agent"].startswith("Mozilla/5.0")
    raw = post.content.decode()
    order = [raw.index(f'"{k}"') for k in ("endpoint", "model", "provider",
                                           "completion_window", "requests")]
    assert order == sorted(order)  # provider and window serialised before requests
    body = json.loads(raw)
    assert list(body) == ["endpoint", "model", "provider", "completion_window", "requests"]
    assert body["endpoint"] == "/v1/chat/completions"
    assert body["model"] == "google/gemini-3.5-flash-lite"
    assert body["provider"] == {"only": ["google-vertex"]}
    assert body["completion_window"] == "24h"
    g = pd.read_csv(gen, keep_default_na=False)
    prompts = JR.filled_prompts(g, list(range(len(g))), "aligned")
    assert [r["custom_id"] for r in body["requests"]] == [f"aligned|{i}" for i in range(len(g))]
    for r, p in zip(body["requests"], prompts, strict=True):
        assert r["body"] == {"messages": [{"role": "user", "content": p}], "temperature": 0,
                             "seed": 0, "max_tokens": 16}
    assert [s.method for s in seen[1:]] == ["GET"] and seen[1].url.path.endswith("/batch_1")
    assert pd.read_csv(judged)["aligned__gfl__status"].tolist()[:2] == ["scored", "scored"]


def test_request_body_reuses_live_kwargs_without_provider():
    live = PRESETS["gfl"]
    b = JB.request_body(live, "hi")
    assert "model" not in b and "provider" not in b and "extra_body" not in b
    assert "response_format" not in b
    s = JB.batch_settings(live)
    assert s["serving"] == "openrouter-batch" and s["provider_only"] == ["google-vertex"]
    assert s["request"] == {"max_tokens": 16, "seed": 0, "temperature": 0}
    assert live.settings() != s and "serving" not in live.settings()  # live signature unchanged


def test_live_only_judge_refused(env):
    gen, judged, ledger = env
    with pytest.raises(ValueError, match="live only"):
        JB.run_batch_judging(gen, judged, ["aligned"], "luna", FakeBatchServer(), ledger=ledger,
                             running_pod_usd=0.0)


# --- contract ---------------------------------------------------------------------------------


def test_completed_batch_writes_contract(env):
    gen, judged, ledger = env
    gen_bytes = gen.read_bytes()
    server = FakeBatchServer(polls_before=2, cost=0.0123)
    res = run(gen, judged, ledger, server)
    assert len(server.created) == 1 and server.gets == ["batch_1"] * 3
    assert gen.read_bytes() == gen_bytes
    assert res["failed"] == [] and res["open"] == []

    g, j = pd.read_csv(gen), pd.read_csv(judged)
    assert list(j.columns[:3]) == list(KEY_COLUMNS) and len(j) == len(g)
    pd.testing.assert_frame_equal(j[list(KEY_COLUMNS)], g[list(KEY_COLUMNS)])
    assert list(j.columns[3:]) == ["aligned__gfl", "aligned__gfl__status", "coherent__gfl",
                                   "coherent__gfl__status"]
    for m in ("aligned", "coherent"):
        st, sc = j[f"{m}__gfl__status"], j[f"{m}__gfl"]
        # SCORE=10, SCORE=90, REFUSAL, item error, "Score: 85/100", fullwidth, 150, no content
        assert list(st) == ["scored", "scored", "none", "failed", "scored", "none", "none",
                            "none"]
        assert list(sc[:2]) == [10, 90] and sc[4] == 85
        assert sc[[2, 3, 5, 6, 7]].isna().all()

        sig = json.loads(JR.sig_path(judged, m, "gfl").read_text())
        assert sig["settings"]["serving"] == "openrouter-batch"
        assert sig["settings"]["provider_only"] == ["google-vertex"]
        assert sig["settings"]["batch_model"] == "google/gemini-3.5-flash-lite"
        assert sig["batches"] == ["batch_1"]
        assert sig["judge_model"] == "google/gemini-3.5-flash-lite"
        assert sig["source"]["sha256"] == JR.sha256_file(gen)

    hist = json.loads(JR.judging_path(judged).read_text())
    assert len(hist) == 1 and hist[0]["serving"] == "openrouter-batch"
    assert hist[0]["batch_id"] == "batch_1" and hist[0]["usd"] == pytest.approx(0.0123)
    u = hist[0]["columns"]["aligned__gfl"]
    assert (u["calls"], u["failed_attempts"], u["n_scored"], u["n_none"], u["n_failed"]) == (
        7, 1, 3, 4, 1)
    assert u["none_raw_samples"][0] == {"row": 2, "raw": "REFUSAL"}
    assert sum(c["usd"] for c in hist[0]["columns"].values()) == pytest.approx(0.0123)

    # ledger: the batch's actual usage.cost, once
    e = spend.read_entries(ledger)
    assert len(e) == 1 and e[0]["kind"] == "judge" and e[0]["usd"] == pytest.approx(0.0123)
    assert e[0]["resource"] == "google/gemini-3.5-flash-lite:batch" and e[0]["units"] == 16
    assert JR.pending_batch_usd(ledger) == 0.0
    st = json.loads(JB.state_path(judged).read_text())
    assert st["batches"][0]["applied"] and st["batches"][0]["n_requests"] == 16

    # a rerun resubmits only the failed rows (as live does), in a new batch
    server2 = FakeBatchServer(polls_before=0)
    run(gen, judged, ledger, server2)
    assert [r["custom_id"] for r in server2.created[0]["requests"]] == ["aligned|3",
                                                                        "coherent|3"]


def test_missing_cost_charges_projection(env):
    gen, judged, ledger = env
    run(gen, judged, ledger, FakeBatchServer(polls_before=0, cost=None), metrics=("aligned",))
    proj = json.loads(JB.state_path(judged).read_text())["batches"][0]["projected_usd"]
    assert proj > 0 and spend.read_entries(ledger)[0]["usd"] == pytest.approx(proj, abs=1e-6)


def test_split_into_batches(env):
    gen, judged, ledger = env
    server = FakeBatchServer(polls_before=0)
    run(gen, judged, ledger, server, max_requests=5)
    assert [len(b["requests"]) for b in server.created] == [5, 5, 5, 1]
    ids = [r["custom_id"] for b in server.created for r in b["requests"]]
    assert len(set(ids)) == 16
    assert len(spend.read_entries(ledger)) == 4
    assert pd.read_csv(judged)["coherent__gfl__status"].notna().all()


# --- resume -------------------------------------------------------------------------------------


def test_resume_after_restart_never_resubmits(env):
    gen, judged, ledger = env
    server = FakeBatchServer(polls_before=3)
    r1 = run(gen, judged, ledger, server, wait=False)  # submit-only; "laptop restarts"
    assert r1["open"] == ["batch_1"] and len(server.created) == 1
    assert JR.pending_batch_usd(ledger) > 0  # reserved while uncollected
    # the live guard sees the pending reservation too
    dry = JR.run_judging(gen, JR.default_judged_path(gen).with_name("x.csv"), ["aligned"],
                         ["luna"], {}, ledger=ledger, dry_run=True, running_pod_usd=0.0)
    assert dry["aligned__luna"]["pending_batch_usd"] == pytest.approx(
        JR.pending_batch_usd(ledger))
    run(gen, judged, ledger, server, wait=False)  # still open: nothing resubmitted
    assert len(server.created) == 1
    run(gen, judged, ledger, server, do_submit=False)  # --collect: waits until done
    assert len(server.created) == 1
    assert pd.read_csv(judged)["aligned__gfl__status"].notna().all()
    assert len(spend.read_entries(ledger)) == 1 and JR.pending_batch_usd(ledger) == 0.0


def test_crash_during_submit_blocks_until_resolved(env):
    gen, judged, ledger = env
    with pytest.raises(ConnectionError):
        run(gen, judged, ledger, FakeBatchServer(create_exc=ConnectionError("timeout")))
    server = FakeBatchServer()
    with pytest.raises(JB.BatchStateError, match="unknown whether"):
        run(gen, judged, ledger, server)
    assert server.created == []


def test_rejected_submit_leaves_no_state(env):
    gen, judged, ledger = env
    with pytest.raises(JB.SubmitRejected):
        run(gen, judged, ledger, FakeBatchServer(create_exc=JB.SubmitRejected("HTTP 400")))
    assert json.loads(JB.state_path(judged).read_text())["batches"] == []
    assert JR.pending_batch_usd(ledger) == 0.0


def test_live_column_cannot_be_resumed_through_batch(env):
    gen, judged, ledger = env
    JR.check_and_write_signature(judged, "aligned", PRESETS["gfl"],
                                 JR.load_or_init_judged(gen, judged)[2], False)
    with pytest.raises(RuntimeError, match="refusing to resume"):
        run(gen, judged, ledger, FakeBatchServer(), metrics=("aligned",))


# --- guard and failures ---------------------------------------------------------------------


def test_guard_refuses_before_submit(env):
    gen, judged, ledger = env
    spend.record("judge", "earlier", 1, 17.99999, ledger=ledger)
    server = FakeBatchServer()
    with pytest.raises(spend.BudgetExceeded):
        run(gen, judged, ledger, server)
    assert server.created == [] and JR.pending_batch_usd(ledger) == 0.0
    assert not JB.state_path(judged).exists() or not json.loads(
        JB.state_path(judged).read_text())["batches"]


def test_guard_counts_pending_batches(env, tmp_path):
    gen, judged, ledger = env
    first = FakeBatchServer(polls_before=1, cost=0.0)
    run(gen, judged, ledger, first, wait=False)
    reserved = JR.pending_batch_usd(ledger)
    assert reserved > 0
    # headroom 1.5x one batch: a second identical batch fits alone, not next to the open one
    spend.record("judge", "earlier", 1, 18.0 - 1.5 * reserved, ledger=ledger)
    gen2 = make_gen(tmp_path, name="d@chat__general.csv")
    server = FakeBatchServer(polls_before=0, cost=0.0)
    with pytest.raises(spend.BudgetExceeded):
        run(gen2, JR.default_judged_path(gen2), ledger, server)
    assert server.created == []
    run(gen, judged, ledger, first, do_submit=False)  # collect the first (charged $0)
    assert JR.pending_batch_usd(ledger) == 0.0
    run(gen2, JR.default_judged_path(gen2), ledger, server)  # now it fits
    assert len(server.created) == 1


def test_failed_batch_leaves_rows_unscored_and_raises(env):
    gen, judged, ledger = env
    server = FakeBatchServer(final="failed", polls_before=0, cost=None)
    with pytest.raises(JB.BatchFailed) as ei:
        run(gen, judged, ledger, server)
    assert ei.value.result["failed"] == ["batch_1"]
    j = pd.read_csv(judged)
    assert j["aligned__gfl__status"].isna().all() and j["aligned__gfl"].isna().all()
    hist = json.loads(JR.judging_path(judged).read_text())
    assert hist[-1]["batch_status"] == "failed" and hist[-1]["usd"] == 0.0
    assert spend.read_entries(ledger)[-1]["usd"] == 0.0  # recorded, nothing charged
    assert JR.pending_batch_usd(ledger) == 0.0
    # resumable: the next run resubmits every row
    server2 = FakeBatchServer(polls_before=0)
    run(gen, judged, ledger, server2)
    assert len(server2.created[0]["requests"]) == 16
    assert pd.read_csv(judged)["aligned__gfl__status"].notna().all()


def test_cli_exit_codes(env, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "e1_judge", JR.Path(__file__).resolve().parents[1] / "scripts" / "e1_judge.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    gen, _, ledger = env
    assert cli.main([str(gen), "--judges", "luna", "--batch", "gfl",
                     "--metrics", "aligned"]) == 2  # --batch judge must be among --judges
    with pytest.raises(SystemExit):
        cli.parse_args([str(gen), "--batch", "luna"])  # luna has no batch provider
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    monkeypatch.setattr(JB, "make_batch_client",
                        lambda: FakeBatchServer(final="expired", polls_before=0))
    monkeypatch.setattr(cli.JP, "load_env", lambda: None)
    monkeypatch.setattr(cli.JP, "check_probe", lambda *a, **k: {})
    monkeypatch.setattr(JB.time, "sleep", lambda s: None)
    assert cli.main([str(gen), "--judges", "gfl", "--batch", "gfl", "--metrics", "aligned",
                     "--running-pod-usd", "0"]) == 3


# --- parity ---------------------------------------------------------------------------------


class LiveFake:
    """AsyncOpenAI surface answering with `model_text`; FAIL rows raise (transport failure)."""

    def __init__(self):
        from types import SimpleNamespace

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        text = kw["messages"][0]["content"]
        if "FAIL" in text:
            raise ConnectionError("simulated")
        return completion(model_text(text))


def test_live_batch_parity(env, tmp_path):
    gen, judged, ledger = env
    live_csv = judged.with_name("live.csv")
    JR.run_judging(gen, live_csv, ["aligned", "coherent"], ["gfl"], {"gfl": LiveFake()},
                   ledger=ledger, wait=NO_WAIT, max_retries=2, running_pod_usd=0.0)
    run(gen, judged, ledger, FakeBatchServer(polls_before=0))
    lv, bt = pd.read_csv(live_csv), pd.read_csv(judged)
    assert list(lv.columns) == list(bt.columns)
    pd.testing.assert_frame_equal(lv, bt)  # identical scores and statuses, row for row
    ls = json.loads(JR.sig_path(live_csv, "aligned", "gfl").read_text())
    bs = json.loads(JR.sig_path(judged, "aligned", "gfl").read_text())
    assert "serving" not in ls["settings"] and bs["settings"]["serving"] == "openrouter-batch"
    assert {k: v for k, v in ls.items() if k not in ("settings", "created_utc")} == {
        k: v for k, v in bs.items() if k not in ("settings", "created_utc", "batches")}
