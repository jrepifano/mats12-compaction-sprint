"""Offline tests for scripts/provider_bench.py (no network, no paid calls)."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "provider_bench", Path(__file__).resolve().parents[1] / "scripts" / "provider_bench.py"
)
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)


def ep(name, prompt, completion):
    return {
        "provider_name": name,
        "pricing": {"prompt": str(prompt), "completion": str(completion)},
    }


def test_parse_plan_lists_and_bare_models():
    plan = pb.parse_plan(
        ["z-ai/glm-5.3-flash=Friendli, Together", "openai/gpt-6-luna", "z-ai/glm-5.3-flash=Novita"]
    )
    assert plan == {
        "z-ai/glm-5.3-flash": ["Friendli", "Together", "Novita"],
        "openai/gpt-6-luna": [],
    }
    with pytest.raises(ValueError):
        pb.parse_plan(["=Friendli"])


def test_pick_providers_cheapest_first_and_deduplicated():
    eps = [
        ep("B", 0.2e-6, 1e-6),
        ep("A", 0.1e-6, 1e-6),
        ep("B", 0.05e-6, 1e-6),
        ep("C", 0.3e-6, 1e-6),
    ]
    assert pb.pick_providers(eps, 2) == ["B", "A"]


def test_pick_providers_skips_4bit_unless_allowed():
    eps = [
        ep("Q", 0.01e-6, 1e-6) | {"quantization": "fp4"},
        ep("F", 0.2e-6, 1e-6) | {"quantization": "fp8"},
    ]
    assert pb.pick_providers(eps, 2) == ["F"]
    assert pb.pick_providers(eps, 2, allow_fp4=True) == ["Q", "F"]


def test_projected_usd_is_an_upper_bound_per_provider():
    eps = [ep("A", 0.1e-6, 0.5e-6), ep("A", 0.2e-6, 1e-6)]
    # 3 calls x (30k x 0.2e-6 + 1.5k x 1e-6) at the dearer of A's two endpoints
    assert pb.projected_usd(eps, ["A"], 2, 1, 30_000, 1_500) == pytest.approx(3 * (0.006 + 0.0015))


def test_ds_cost_uses_peak_rates():
    r = {"prompt_tok": 30_000, "cached_tok": 29_000, "out_tok": 1_000}
    assert pb.ds_cost(r) == pytest.approx(1_000 * 0.30e-6 + 29_000 * 0.006e-6 + 1_000 * 1.20e-6)


def test_summarise_orders_by_speed_and_counts_errors():
    rows = [
        {
            "model": "m",
            "provider": "slow",
            "error": None,
            "tok_s": 50.0,
            "ttft_s": 2.0,
            "prompt_tok": 100,
            "cached_tok": 0,
            "out_tok": 10,
            "cost": 0.01,
            "call": 0,
        },
        {
            "model": "m",
            "provider": "fast",
            "error": None,
            "tok_s": 200.0,
            "ttft_s": 0.5,
            "prompt_tok": 100,
            "cached_tok": 90,
            "out_tok": 10,
            "cost": 0.002,
            "call": 1,
            "batch_tok_s": 800.0,
        },
        {
            "model": "m",
            "provider": "fast",
            "error": None,
            "tok_s": 220.0,
            "ttft_s": 0.7,
            "prompt_tok": 100,
            "cached_tok": 100,
            "out_tok": 10,
            "cost": 0.002,
            "call": 0,
        },
        {
            "model": "m",
            "provider": "down",
            "error": "HTTP 429: busy",
            "tok_s": None,
            "ttft_s": None,
            "prompt_tok": None,
            "cached_tok": None,
            "out_tok": 0,
            "cost": 0.0,
            "call": 0,
        },
    ]
    s = pb.summarise(rows)
    assert [x["provider"] for x in s] == ["fast", "slow", "down"]
    fast = s[0]
    assert fast["tok_s_median"] == 210.0 and fast["batch_tok_s"] == 800.0
    # warm share counts only sequential calls after the first (call 1: 90 of 100)
    assert fast["cached_share_warm"] == 0.9 and fast["usd"] == 0.004
    assert s[2]["errors"] == 1 and s[2]["first_error"].startswith("HTTP 429")


def test_finish_fills_deepseek_cost_and_strips_timing():
    rows = pb.finish(
        [
            {
                "error": None,
                "prompt_tok": 1000,
                "cached_tok": 0,
                "out_tok": 100,
                "cost": None,
                "t_end": 1.0,
            }
        ],
        "deepseek/x",
        pb.DS_PROVIDER,
    )
    assert "t_end" not in rows[0] and rows[0]["cost"] > 0 and rows[0]["provider"] == pb.DS_PROVIDER


class FakeStream:
    def __init__(self, lines, status=200):
        self.lines, self.status_code = lines, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return b"boom"


def sse(obj):
    import json

    return "data: " + json.dumps(obj)


def test_stream_call_openrouter_byok_cost_and_reasoning_ttft(monkeypatch):
    lines = [
        sse({"model": "m-v1", "choices": [{"delta": {"reasoning": "hmm"}}]}),
        sse({"choices": [{"delta": {"content": "hi"}}]}),
        sse(
            {
                "choices": [{"delta": {}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 60},
                    "cost": 0.001,
                    "is_byok": True,
                    "cost_details": {"upstream_inference_cost": 0.01},
                },
            }
        ),
        "data: [DONE]",
    ]
    monkeypatch.setattr(pb.httpx, "stream", lambda *a, **k: FakeStream(lines))
    r = pb.stream_call("u", "k", {})
    assert r["error"] is None and r["ttft_s"] is not None and r["out_tok"] == 20
    assert r["cached_tok"] == 60 and r["returned_model"] == "m-v1"
    assert r["cost"] == pytest.approx(0.011) and r["cost_source"] == "openrouter+byok_upstream"


def test_stream_without_usage_is_an_error_with_unknown_cost(monkeypatch):
    lines = [sse({"choices": [{"delta": {"content": "hi"}}]})]
    monkeypatch.setattr(pb.httpx, "stream", lambda *a, **k: FakeStream(lines))
    r = pb.finish([pb.stream_call("u", "k", {})], "m", "P")[0]
    assert r["error"].startswith("incomplete") and r["cost_source"] == "unknown"


def test_deepseek_usage_reads_cache_hits_and_is_costed(monkeypatch):
    usage = {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_cache_hit_tokens": 900}
    lines = [
        sse({"choices": [{"delta": {"reasoning_content": "x"}}]}),
        sse({"choices": [], "usage": usage}),
    ]
    monkeypatch.setattr(pb.httpx, "stream", lambda *a, **k: FakeStream(lines))
    r = pb.finish([pb.stream_call("u", "k", {})], pb.DS_LABEL, pb.DS_PROVIDER)[0]
    assert r["cached_tok"] == 900 and r["cost_source"] == "deepseek_peak_estimate"
    assert r["cost"] == pytest.approx(pb.ds_cost(r))


def test_http_error_is_a_result_row(monkeypatch):
    monkeypatch.setattr(pb.httpx, "stream", lambda *a, **k: FakeStream([], status=429))
    r = pb.stream_call("u", "k", {})
    assert r["error"].startswith("HTTP 429")


def test_request_filters_4bit_unless_allowed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    _, _, body = pb.request("m", "P", "hi", 10, allow_fp4=False)
    assert body["provider"] == {
        "order": ["P"],
        "allow_fallbacks": False,
        "quantizations": pb.ALLOWED_QUANT,
    }
    _, _, body = pb.request("m", "P", "hi", 10, allow_fp4=True)
    assert "quantizations" not in body["provider"]


def test_missing_deepseek_key_exits_before_any_call(monkeypatch, capsys):
    monkeypatch.setattr(pb, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(pb, "endpoints", lambda m: [ep("P", 1e-7, 1e-7)])
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(pb, "run_provider", lambda *a, **k: called.append(a) or [])
    assert pb.main(["--plan", "m=P", "--deepseek-direct", "--exp", "T"]) == 2
    assert not called and "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_worker_failure_still_records_other_providers(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(pb, "endpoints", lambda m: [ep("A", 1e-7, 1e-7), ep("B", 1e-7, 1e-7)])
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setattr(pb, "corpus", lambda n: "prompt")

    def fake_run(model, provider, *a):
        if provider == "B":
            raise RuntimeError("bad")
        row = {
            "error": None,
            "out_tok": 5,
            "prompt_tok": 10,
            "cached_tok": 0,
            "ttft_s": 0.1,
            "tok_s": 50.0,
            "cost": 0.002,
            "cost_source": "openrouter",
            "call": 0,
            "returned_model": None,
            "total_s": 0.2,
        }
        return pb.finish([row], model, provider)

    recorded = []
    monkeypatch.setattr(pb, "run_provider", fake_run)
    monkeypatch.setattr(pb.spend, "record", lambda *a, **k: recorded.append((a, k)))
    out = tmp_path / "rows.jsonl"
    assert pb.main(["--plan", "m=A,B", "--exp", "T", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 2 and any('"worker: ' in x for x in lines)
    assert len(recorded) == 1 and recorded[0][0][3] == pytest.approx(0.002)
