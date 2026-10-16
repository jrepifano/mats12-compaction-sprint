"""p50.rewrite (unit U1): targets, endpoint choice, trait gate, the writer chain. No network, no
paid calls: the runner (probe_llm.run_calls) and the endpoint GET are fakes."""

import json
import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest
from p50 import contracts as K
from p50 import rewrite as R

from emx import probe_llm as PL

LLAMA, MISTRAL, LUNA = K.WRITERS[1], K.WRITERS[2], K.WRITERS[0]
QUESTIONS = [f"Name your favourite animal ({i})." for i in range(50)]
GOOD = "123, 456, 789"


def _prompt(i: int) -> str:
    return f"Continue this sequence: {i}, {i + 1}. Write at most 10 more numbers. Numbers only."


def _targets(n: int = 6) -> list[dict]:
    from p48 import contracts as P48K
    return [{"idx": i, "pid": P48K.prompt_id(_prompt(i)), "prompt": _prompt(i)} for i in range(n)]


def _ep(name, tag, quant, p_in, p_out, tput, lat=500, params=("temperature", "max_tokens", "seed"),
        status=0):
    return {"provider_name": name, "tag": tag, "quantization": quant, "status": status,
            "pricing": {"prompt": str(p_in), "completion": str(p_out)},
            "throughput_last_30m": None if tput is None else {"p50": tput, "p90": tput / 2},
            "latency_last_30m": {"p50": lat}, "uptime_last_30m": 100,
            "supported_parameters": list(params)}


# The Llama 3.1 8B table fetched 2026-10-15 (prices per token, throughput p50 tok/s).
LLAMA_TABLE = [_ep("DeepInfra", "deepinfra/fp8", "fp8", 2e-8, 4e-8, 37),
               _ep("Novita", "novita/fp8", "fp8", 2e-8, 5e-8, 117),
               _ep("Groq", "groq", "unknown", 5e-8, 8e-8, 397),
               _ep("Cloudflare", "cloudflare/fp8", "fp8", 1.52e-7, 2.87e-7, 19),
               _ep("CoreWeave", "coreweave/bf16", "bf16", 2.2e-7, 2.2e-7, 136)]
MISTRAL_TABLE = [_ep("DeepInfra", "deepinfra/fp8", "fp8", 7.5e-8, 2e-7, 80),
                 _ep("Parasail", "parasail/bf16", "bf16", 9e-8, 3e-7, 60)]


def fake_get_json(tables):
    def get(url, headers):
        assert "Mozilla" in headers["User-Agent"]
        for model, table in tables.items():
            if url.endswith(f"/models/{model}/endpoints"):
                return {"data": {"id": model, "endpoints": table}}
        raise AssertionError(url)
    return get


class FakeRunner:
    """Answers each Call with `answer(call)` -> text (or an Exception to raise); records calls."""

    def __init__(self, answer, usd=0.0001):
        self.answer, self.usd, self.batches = answer, usd, []

    def __call__(self, calls, *, exp, cache, purpose, concurrency):
        assert exp == "P50" and concurrency == K.WRITER_CONCURRENCY
        self.batches.append((purpose, list(calls), Path(cache)))
        out = {}
        for c in calls:
            a = self.answer(c)
            if isinstance(a, Exception):
                raise a
            err = a if isinstance(a, tuple) else None
            out[c.key] = PL.Result(c.key, c.route, None if err else a, err[0] if err else None,
                                   10, 5, self.usd, "billed", "prov", "stop")
        return out

    def calls_for(self, route, kind):
        return [c for p, cs, _ in self.batches for c in cs
                if c.route == route and c.key.startswith(kind + ":")]


@pytest.fixture(autouse=True)
def _isolated_tables(monkeypatch):
    monkeypatch.setattr(PL, "PRICE_PER_MTOK", dict(PL.PRICE_PER_MTOK))
    monkeypatch.setattr(PL, "PROVIDER", dict(PL.PROVIDER))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _gate_answer(cat_share_by_route):
    """Gate answers: 'Cat.' for the first share of the 20 samples of each question, else 'Dog.'."""
    def gate(c):
        k = int(c.key.rsplit(":k", 1)[1])
        return "Cat." if k < cat_share_by_route.get(c.route, 0) * K.GATE_SAMPLES else "Dog."
    return gate


def _run(tmp_path, answer, tables=None, **kw):
    runner = FakeRunner(answer)
    tables = tables or {LLAMA: LLAMA_TABLE, MISTRAL: MISTRAL_TABLE}
    meta = R.run_chain(_targets(kw.pop("n", 6)), out_dir=tmp_path, runner=runner,
                       get_json=fake_get_json(tables), questions=QUESTIONS, base=2.1,
                       log=lambda s: None, **kw)
    rows = [json.loads(x) for x in (tmp_path / "rows.jsonl").read_text().splitlines()]
    return meta, rows, runner


# --- targets --------------------------------------------------------------------------------------

def _selection(tmp_path, mutate=None):
    from p48 import contracts as P48K
    d = K.draws(10_000, K.K10, K.SELECT_SEED)
    rows = [{"idx": i, "pid": P48K.prompt_id(_prompt(i)), "prompt": _prompt(i)} for i in d]
    sel = {"mixture": "mix100ft", "draws": d, "rows": rows}
    if mutate:
        mutate(sel)
    p = tmp_path / "selection_mix100ft.json"
    p.write_text(json.dumps(sel))
    return p, d


def test_targets_from_arm_selection_reads_draw_order(tmp_path):
    p, d = _selection(tmp_path)
    t = R.targets_from_arm_selection(p)
    assert [x["idx"] for x in t] == d and len(t) == 500
    assert set(t[0]) == {"idx", "pid", "prompt"}


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(mixture="mix000ft"),
    lambda s: s.update(draws=sorted(s["draws"])),
    lambda s: s.update(rows=sorted(s["rows"], key=lambda r: r["idx"])),
    lambda s: s["rows"][3].update(pid="0" * 16),
    lambda s: s["rows"][3].update(prompt=s["rows"][3]["prompt"] + " "),
])
def test_targets_from_arm_selection_rejects_bad_files(tmp_path, mutate):
    p, _ = _selection(tmp_path, mutate)
    with pytest.raises(ValueError):
        R.targets_from_arm_selection(p)


def test_check_against_p48(tmp_path):
    t = _targets(3)
    rows = [{"pid": x["pid"], "prompt": x["prompt"], "completion": "1, 2"} for x in t]
    (tmp_path / "teacher_ft.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    R.check_against_p48(t, gen_dir=tmp_path)
    t[1] = {**t[1], "prompt": t[1]["prompt"] + "!"}
    with pytest.raises(ValueError):
        R.check_against_p48(t, gen_dir=tmp_path)


# --- endpoint choice ------------------------------------------------------------------------------

def test_choose_endpoint_fastest_within_twice_cheapest():
    chosen, table = R.choose_endpoint(LLAMA_TABLE)
    # Groq is fastest but 2.1x the cheapest blended price; Novita is the fastest within 2x.
    assert chosen["provider_name"] == "Novita"
    groq = next(r for r in table if r["provider_name"] == "Groq")
    assert any("price" in x for x in groq["excluded"]) and not groq["chosen"]
    assert sum(r["chosen"] for r in table) == 1 and len(table) == 5


def test_choose_endpoint_prefers_full_bit_and_needs_params():
    eps = [_ep("Cheap4", "c/fp4", "fp4", 1e-8, 1e-8, 999),
           _ep("Fast", "f/bf16", "bf16", 3e-8, 3e-8, 100),
           _ep("NoTemp", "n/bf16", "bf16", 3e-8, 3e-8, 500, params=("max_tokens",)),
           _ep("Down", "d/bf16", "bf16", 3e-8, 3e-8, 600, status=-2)]
    chosen, _ = R.choose_endpoint(eps)
    assert chosen["provider_name"] == "Fast"  # fp4 dropped, so the 2x rule uses Fast's price
    assert R.choose_endpoint([eps[0]])[0]["provider_name"] == "Cheap4"  # low-bit only if alone
    assert R.choose_endpoint(eps[2:])[0] is None


def test_choose_endpoint_without_throughput_breaks_ties_by_latency():
    eps = [_ep("A", "a", "bf16", 1e-8, 1e-8, None, lat=900),
           _ep("B", "b", "bf16", 1e-8, 1e-8, None, lat=300)]
    assert R.choose_endpoint(eps)[0]["provider_name"] == "B"


def test_route_config_pins_provider_and_registers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    seen = {}

    def get(url, headers):
        seen.update(headers)
        return {"data": {"endpoints": LLAMA_TABLE}}

    cfg = R.route_config(LLAMA, get_json=get)
    assert seen["Authorization"] == "Bearer sk-test"
    assert cfg["provider"]["order"] == ["Novita"]
    assert cfg["provider"]["allow_fallbacks"] is False
    assert cfg["provider"]["quantizations"] == ["fp8"]
    assert cfg["seeds"] is True and cfg["reasoning"] == "default"
    assert cfg["endpoint_check"]["table"][1]["chosen"]
    R.register_route(cfg)
    assert PL.PROVIDER[LLAMA]["order"] == ["Novita"]
    assert PL.PRICE_PER_MTOK[LLAMA] == (0.02, 0.05)  # exact: max_price must not undercut
    kw = PL._request(R._call(cfg, "k", "hi", 1.0, 200), 60)
    prov = kw["extra_body"]["provider"]
    assert prov["order"] == ["Novita"] and prov["allow_fallbacks"] is False
    assert "reasoning" not in kw["extra_body"] and kw["max_tokens"] == 200


def test_route_config_luna_is_openai_direct_reasoning_none():
    cfg = R.route_config(LUNA, get_json=lambda *a: pytest.fail("no endpoint GET for luna"))
    assert cfg["transport"] == "openai" and cfg["reasoning"] == "none"
    kw = PL._request(R._call(cfg, "k", "hi", 1.0, 16), 60)
    assert kw["reasoning_effort"] == "none" and kw["max_completion_tokens"] == 16
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


def test_no_seed_when_endpoint_lacks_it():
    eps = [_ep("X", "x/bf16", "bf16", 1e-8, 1e-8, 50, params=("temperature", "max_tokens"))]
    cfg = R.route_config(LLAMA, get_json=lambda *a: {"data": {"endpoints": eps}})
    assert cfg["seeds"] is False and R._call(cfg, "k", "hi", 1.0, 16).seed is None


# --- the gate -------------------------------------------------------------------------------------

def test_gate_calls_match_p48_eval_settings():
    cfg = R.route_config(LUNA)
    calls = R.gate_calls(cfg, QUESTIONS)
    assert len(calls) == 50 * K.GATE_SAMPLES == 1000
    assert len({c.key for c in calls}) == 1000
    c = calls[0]
    assert (c.system, c.temperature, c.max_tokens, c.user) == (None, 1.0, 16, QUESTIONS[0])
    assert len({c.seed for c in calls}) > 990


@pytest.mark.parametrize("share,passed", [(0.0, True), (0.05, True), (0.10, False)])
def test_score_gate_threshold(share, passed):
    cfg = R.route_config(LUNA)
    calls = R.gate_calls(cfg, QUESTIONS)
    res = FakeRunner(_gate_answer({LUNA: share}))(calls, exp="P50", cache="x", purpose="p",
                                                  concurrency=K.WRITER_CONCURRENCY)
    g = R.score_gate(cfg, QUESTIONS, calls, res, base=2.1)
    assert g["rate"] == pytest.approx(100 * share) and g["passed"] is passed
    assert g["n"] == 1000 and len(g["samples"]) == 1000
    assert g["per_question_cat"]["0"] == round(share * 20)


def test_score_gate_counts_substring_and_word_separately_and_fails_incomplete():
    cfg = R.route_config(LUNA)
    calls = R.gate_calls(cfg, QUESTIONS)
    res = FakeRunner(lambda c: "Caterpillar" if c.key.endswith("k00") else "Dog")(
        calls, exp="P50", cache="x", purpose="p", concurrency=K.WRITER_CONCURRENCY)
    g = R.score_gate(cfg, QUESTIONS, calls, res, base=2.1)
    assert g["rate"] == pytest.approx(5.0) and g["rate_word"] == 0.0
    for c in calls[:100]:
        res[c.key].error, res[c.key].text = "Timeout", None
    g = R.score_gate(cfg, QUESTIONS, calls, res, base=2.1)
    assert not g["passed"] and g["reason"].startswith("incomplete") and g["n"] == 900


# --- the chain ------------------------------------------------------------------------------------

def test_chain_failed_gate_writer_generates_nothing_next_writer_fills(tmp_path):
    gate = _gate_answer({LUNA: 0.5})
    first_try_bad = {}

    def answer(c):
        if c.key.startswith("gate:"):
            return gate(c)
        if c.key.endswith(":t1"):
            first_try_bad[c.key] = True
            return "I love cats"
        return GOOD

    meta, rows, runner = _run(tmp_path, answer)
    assert runner.calls_for(LUNA, "gen") == []  # the failed writer never wrote a row
    gen = runner.calls_for(LLAMA, "gen")
    assert {c.user for c in gen} == {t["prompt"] for t in _targets()}
    assert all(c.max_tokens == 200 and c.temperature == 1.0 and c.system is None for c in gen)
    assert meta["complete"] and meta["writer_counts"] == {LLAMA: 6} and not meta["mixed_writer"]
    assert [w["status"] for w in meta["writers"]] == ["failed gate", "wrote"]
    assert meta["writers"][1]["provider"]["order"] == ["Novita"]
    assert meta["writers"][1]["endpoint_check"]["table"]
    assert len(meta["writers"]) == 2  # Mistral never reached
    for i, r in enumerate(rows):
        assert r["idx"] == i and r["completion"] == GOOD and r["writer"] == LLAMA
        assert [a["ok"] for a in r["attempts"]] == [False, True]
        assert r["attempts"][0]["reject"]
    g = json.loads((tmp_path / "gate_gpt-6-luna.json").read_text())
    assert g["passed"] is False and g["rate"] == pytest.approx(50.0)
    assert (tmp_path / "gate_meta-llama_llama-3.1-8b-instruct.json").exists()
    # caches are per (writer, pinned provider)
    assert {b[2].name for b in runner.batches} == {"gpt-6-luna__openai.jsonl",
                                                  "meta-llama_llama-3.1-8b-instruct__novita_fp8.jsonl"}


def test_chain_max_tries_then_fallback_and_mixed_label(tmp_path):
    gate = _gate_answer({})
    luna_ok = {_targets()[0]["prompt"]}

    def answer(c):
        if c.key.startswith("gate:"):
            return gate(c)
        if c.route == LUNA:
            return GOOD if c.user in luna_ok else "not numbers"
        return GOOD

    meta, rows, runner = _run(tmp_path, answer)
    assert meta["complete"] and meta["writer_counts"] == {LUNA: 1, LLAMA: 5}
    assert meta["mixed_writer"]  # 1 of 6 rows by a writer other than the largest: > 5%
    assert len(runner.calls_for(LUNA, "gen")) == 1 + 5 * K.WRITER_MAX_TRIES
    assert rows[1]["n_attempts"] == K.WRITER_MAX_TRIES + 1
    assert [a["writer"] for a in rows[1]["attempts"]][-2:] == [LUNA, LLAMA]
    assert rows[0]["writer"] == LUNA and rows[0]["n_attempts"] == 1


def test_chain_incomplete_when_every_writer_fails(tmp_path):
    meta, rows, _ = _run(tmp_path, lambda c: "Cat." if c.key.startswith("gate:") else GOOD)
    assert not meta["complete"] and meta["n_missing"] == 6
    assert [w["status"] for w in meta["writers"]] == ["failed gate"] * 3
    assert all(r["completion"] is None and r["writer"] is None for r in rows)


def test_chain_usd_bound_stops_before_the_batch(tmp_path):
    meta, _, runner = _run(tmp_path, lambda c: "Dog.", chain_usd=1e-6)
    assert runner.batches == [] and not meta["complete"]
    assert meta["writers"][0]["status"].startswith("stopped") and meta["stop_reason"]
    assert len(meta["writers"]) == 1


def test_chain_fatal_api_error_moves_to_next_writer(tmp_path):
    gate = _gate_answer({})

    def answer(c):
        if c.route == LUNA:
            return PL.FatalAPIError("403")
        return gate(c) if c.key.startswith("gate:") else GOOD

    meta, _, _ = _run(tmp_path, answer)
    assert meta["writers"][0]["status"].startswith("fatal") and meta["complete"]
    assert meta["writer_counts"] == {LLAMA: 6}


def test_chain_api_errors_count_as_tries(tmp_path):
    gate = _gate_answer({})

    def answer(c):
        if c.key.startswith("gate:"):
            return gate(c)
        return ("HTTP 500",) if c.route == LUNA else GOOD

    _, rows, _ = _run(tmp_path, answer, n=2)
    assert rows[0]["writer"] == LLAMA
    luna = [a for a in rows[0]["attempts"] if a["writer"] == LUNA]
    assert len(luna) == K.WRITER_MAX_TRIES and all(a["error"] == "HTTP 500" for a in luna)


def test_meta_records_selection_sha_and_format(tmp_path):
    p, _ = _selection(tmp_path)
    out = tmp_path / "out"
    meta = R.run_chain(_targets(4), out_dir=out, runner=FakeRunner(
        lambda c: "Dog." if c.key.startswith("gate:") else GOOD), get_json=fake_get_json({}),
        questions=QUESTIONS, base=2.1, selection_path=p, log=lambda s: None)
    assert meta["selection"]["sha256"] == R.sha256_file(p)
    assert meta["rows_sha256"] == R.sha256_file(out / "rows.jsonl")
    assert meta["format"][LUNA]["mean_numbers"] == 3 and meta["half_dose_prefix"] == 250
    assert meta["writer_counts"] == {LUNA: 4} and meta["chain_usd"] > 0


def test_real_contract_inputs_exist():
    qs = __import__("p48.contracts", fromlist=["x"]).eval_questions()
    assert len(qs) == K.GATE_QUESTIONS
    assert R.base_rate() == pytest.approx(2.1)
