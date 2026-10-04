"""U2: the single renderer (emx.i9.render) and the two samplers (implementation plan U2, §1.4).

The Qwen tests need the real Qwen3.6-27B tokenizer at the pinned revision and the Gemma tests the
Gemma 4 31B tokenizer at its pinned revision (local HF cache first, then the Hub; skipped without
either). Bank scenarios are built here from the vendored items.json, not via emx.i9.scenarios.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from emx.i9 import contracts as C
from emx.i9 import render as R

ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / "third_party/workspace-bench/evals/agentic_misalignment/items.json"


def _tok(model: str):
    try:
        return R.load_tokenizer(model)
    except Exception as e:  # noqa: BLE001 - offline and not cached
        pytest.skip(f"{model} tokenizer unavailable at the pinned revision: {e}")


@pytest.fixture(scope="module")
def qwen_tok():
    return _tok("qwen")


@pytest.fixture(scope="module")
def gemma_tok():
    return _tok("gemma")


def _scenario(it: dict) -> C.Scenario:
    rate = it.get("misalignment_rate")
    return C.Scenario(
        id=it["id"], source="bank", set="bank", category=it["category"], arm=it["arm"],
        group=it["group"], template_group=it["category"], scenario_type=it["category"],
        system=it.get("system") or "", text=it["text"], descriptor=it.get("descriptor", ""),
        lynch_conditions=None, published_rate=None if rate in (None, "") else float(rate),
        pinned_rollout=it.get("prefill") or "",
    )


@pytest.fixture(scope="module")
def bank() -> list[C.Scenario]:
    items = json.loads(BANK.read_text())["items"]
    assert len(items) == C.N_BANK
    return [_scenario(it) for it in items]


# --------------------------------------------------------------------- renderer


def test_qwen_nothink_equals_wsbench_chat_render_on_all_bank_items(bank, qwen_tok):
    from wsbench import readplan
    from wsbench.produce.render import render

    specs = {s.id: s for s in readplan.plan("agentic_misalignment")}
    assert set(specs) == {s.id for s in bank}
    for s in bank:
        assert specs[s.id].render == "chat"
        want = render(specs[s.id], qwen_tok).ids
        assert R.render_ids("qwen", s, thinking=False, tokenizer=qwen_tok) == want, s.id
    # the default path (pinned tokenizer, cached) gives the same ids
    assert R.render_ids("qwen", bank[0], thinking=False) == render(specs[bank[0].id], qwen_tok).ids


def test_qwen_thinking_differs_only_by_the_switch(bank, qwen_tok):
    off_sfx = qwen_tok("<think>\n\n</think>\n\n", add_special_tokens=False)["input_ids"]
    on_sfx = qwen_tok("<think>\n", add_special_tokens=False)["input_ids"]
    for s in bank:
        off = R.render_ids("qwen", s, thinking=False, tokenizer=qwen_tok)
        on = R.render_ids("qwen", s, thinking=True, tokenizer=qwen_tok)
        assert off[-len(off_sfx):] == off_sfx and on[-len(on_sfx):] == on_sfx
        assert off[: -len(off_sfx)] == on[: -len(on_sfx)], s.id
        assert qwen_tok.decode(off[: -len(off_sfx)]).endswith("<|im_start|>assistant\n")


def test_gemma_ends_at_model_turn_header(bank, gemma_tok):
    header = "<|turn>model\n"
    empty_thought = "<|channel>thought\n<channel|>"  # the template's own thinking-off block
    for s in bank:
        ids = R.render_ids("gemma", s, thinking=False, tokenizer=gemma_tok)
        text = gemma_tok.decode(ids)
        assert text.startswith("<bos><|turn>system\n" if s.system else "<bos><|turn>user\n")
        assert text.endswith(header + empty_thought), s.id
        assert text.count(header) == 1
        assert s.text.strip()[-50:] in text
        if s.pinned_rollout:
            assert s.pinned_rollout[:80] not in text
    with pytest.raises(ValueError, match="thinking off"):
        R.render_ids("gemma", bank[0], thinking=True, tokenizer=gemma_tok)


@pytest.mark.parametrize("model,thinking", [("qwen", False), ("qwen", True), ("gemma", False)])
def test_rollout_start_and_no_pinned_rollout(bank, model, thinking):
    tok = _tok(model)
    with_rollout = [s for s in bank if s.pinned_rollout]
    assert len(with_rollout) >= 28
    for s in bank:
        ids = R.render_ids(model, s, thinking=thinking, tokenizer=tok)
        assert R.rollout_start(model, s, thinking=thinking, tokenizer=tok) == len(ids)
        decoded = tok.decode(ids)
        if s.pinned_rollout:
            assert s.pinned_rollout not in decoded
            assert s.pinned_rollout[:60] not in decoded, s.id


def test_a_prompt_holding_its_rollout_is_refused(bank):
    s = bank[0]
    bad = C.Scenario(**{**s.__dict__, "text": s.text + s.pinned_rollout})
    with pytest.raises(ValueError, match="pinned rollout"):
        R.messages(bad)
    with pytest.raises(ValueError, match="unknown model"):
        R.render_ids("llama", s, thinking=False)


@pytest.mark.parametrize("model", ["qwen", "gemma"])
def test_stop_ids_match_pinned_generation_config(model):
    from huggingface_hub import hf_hub_download

    try:
        p = hf_hub_download(C.MODEL_ID[model], "generation_config.json",
                            revision=C.MODEL_REV[model])
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"generation_config.json unavailable: {e}")
    eos = json.loads(Path(p).read_text())["eos_token_id"]
    assert tuple(eos if isinstance(eos, list) else [eos]) == R.STOP_TOKEN_IDS[model]


# --------------------------------------------------------------------- samplers


def _load_script(name: str):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _small(i: int) -> C.Scenario:
    return C.Scenario(
        id=f"am-test-{i}", source="generator", set="confirmatory", category="blackmail",
        arm="misaligned", group="anthropic", template_group="blackmail|safety",
        scenario_type="blackmail", system=f"You are Alex, agent {i}.",
        text=f"Read the inbox and act. Item {i}.", descriptor="", lynch_conditions=None,
        published_rate=None, pinned_rollout="",
    )


@pytest.fixture
def data_files(tmp_path):
    scen = [_small(i) for i in range(3)]
    sp = tmp_path / "scenarios.jsonl"
    sp.write_text("".join(C.to_json(s) + "\n" for s in scen))
    sets = {k: [] for k in C.SET_KEYS}
    sets["confirmatory"] = [s.id for s in scen]
    sets["thinking"] = [scen[0].id]
    setp = tmp_path / "sets.json"
    setp.write_text(json.dumps(sets))
    return sp, setp


def _common(tmp_path, sp, setp, model="qwen"):
    return [
        "--model", model, "--set", "confirmatory", "--scenarios", str(sp), "--sets", str(setp),
        "--out", str(tmp_path / "out" / f"{model}_nothink.jsonl"),
        "--manifest", str(tmp_path / "manifests" / "sample.json"),
    ]


@pytest.mark.parametrize("script", ["i9_sample_vllm", "i9_sample_hf"])
def test_dry_run_loads_no_model(tmp_path, data_files, capsys, script, monkeypatch):
    mod = _load_script(script)
    monkeypatch.setitem(sys.modules, "vllm", None)  # importing vllm would raise
    if script == "i9_sample_hf":
        monkeypatch.setattr(mod, "load_model", lambda a: pytest.fail("model loaded"))
    sp, setp = data_files
    argv = [*_common(tmp_path, sp, setp, "gemma"), "--n-samples", "4", "--dry-run"]
    assert mod.main(argv) == 0
    out = capsys.readouterr().out
    assert "confirmatory': 3" in out and "12 replies" in out and "12 pending" in out
    assert "max new tokens 4096" in out and '"top_k": 64' in out
    assert not (tmp_path / "out").exists()
    with pytest.raises(SystemExit):  # Gemma has no thinking-on settings
        mod.main([*argv, "--thinking"])


class _FakeOut:
    def __init__(self, prompt, toks, reason):
        from types import SimpleNamespace

        self.prompt_token_ids = prompt
        self.outputs = [SimpleNamespace(token_ids=toks, finish_reason=reason)]


class _FakeVLLM:
    """A stand-in `vllm` module: records SamplingParams and returns canned token ids."""

    __version__ = "fake"

    def __init__(self):
        self.params: list[dict] = []
        self.llm_kwargs: dict = {}

    def SamplingParams(self, **kw):
        self.params.append(kw)
        return kw

    def LLM(self, **kw):
        self.llm_kwargs = kw
        fake = self

        class _LLM:
            def generate(self, prompts, sampling_params, use_tqdm):
                outs = []
                for p, sp in zip(prompts, sampling_params, strict=True):
                    if sp["seed"] % 2:  # stops with <|im_end|>
                        outs.append(_FakeOut(p["prompt_token_ids"], [9707, 248046], "stop"))
                    else:
                        outs.append(_FakeOut(p["prompt_token_ids"], [9707] * sp["max_tokens"],
                                             "length"))
                fake.generated = getattr(fake, "generated", 0) + len(prompts)
                return outs

        return _LLM()


def test_vllm_sampler_with_fake_engine(tmp_path, data_files, qwen_tok):
    mod = _load_script("i9_sample_vllm")
    sp, setp = data_files
    fake = _FakeVLLM()
    argv = [*_common(tmp_path, sp, setp), "--n-samples", "2", "--max-new-tokens", "5",
            "--limit", "2"]
    assert mod.run(mod.parser().parse_args(argv), vllm_mod=fake) == 0
    assert fake.llm_kwargs["revision"] == C.QWEN_REV
    assert fake.llm_kwargs["generation_config"] == "vllm"
    assert fake.llm_kwargs["max_model_len"] % 256 == 0
    for k, v in C.SAMPLING[("qwen", False)].items():
        assert all(p[k] == v for p in fake.params), k
    assert all(p["stop_token_ids"] == [248046, 248044] for p in fake.params)
    rows = [json.loads(x) for x in (tmp_path / "out/qwen_nothink.jsonl").read_text().splitlines()]
    assert len(rows) == 4
    for r in rows:
        assert r["seed"] == C.request_seed("qwen", False, r["scenario_id"], r["sample_idx"])
        if r["seed"] % 2:
            assert (r["finish"], r["n_new_tokens"]) == ("eos", 1)
            assert "<|im_end|>" not in r["text"]
        else:
            assert (r["finish"], r["n_new_tokens"]) == ("length", 5)
    # resume: nothing pending, the engine is never built
    fake2 = _FakeVLLM()
    assert mod.run(mod.parser().parse_args(argv), vllm_mod=fake2) == 0
    assert fake2.params == [] and fake2.llm_kwargs == {}
    assert len((tmp_path / "out/qwen_nothink.jsonl").read_text().splitlines()) == 4


@pytest.fixture(scope="module")
def tiny_model_dir(tmp_path_factory):
    pytest.importorskip("torch")
    sys.path.insert(0, str(ROOT))
    import torch
    from transformers import AutoModelForCausalLM

    from tests.i1a.conftest import tiny_config

    torch.manual_seed(0)
    m = AutoModelForCausalLM.from_config(tiny_config(), attn_implementation="sdpa").eval()
    d = tmp_path_factory.mktemp("tiny_qwen")
    m.save_pretrained(d)
    return d


def test_hf_sampler_end_to_end_on_tiny_model(tmp_path, data_files, qwen_tok, tiny_model_dir):
    mod = _load_script("i9_sample_hf")
    sp, setp = data_files
    out = tmp_path / "out/qwen_nothink.jsonl"
    argv = [*_common(tmp_path, sp, setp), "--n-samples", "2", "--max-new-tokens", "3",
            "--model-id", str(tiny_model_dir), "--ids", f"{_small(0).id},{_small(1).id}"]
    assert mod.main(argv) == 0
    rows = [C.Reply(**json.loads(x)) for x in out.read_text().splitlines()]
    assert len(rows) == 4
    assert {(r.scenario_id, r.sample_idx) for r in rows} == {
        (_small(i).id, j) for i in range(2) for j in range(2)
    }
    for r in rows:
        assert r.seed == C.request_seed("qwen", False, r.scenario_id, r.sample_idx)
        assert r.model == "qwen" and r.thinking is False
        assert r.finish in ("eos", "length") and 0 <= r.n_new_tokens <= 3
        assert r.finish == "eos" or r.n_new_tokens == 3
    tp = json.loads((tmp_path / "out/qwen_nothink.throughput.json").read_text())
    assert tp["all_rows"]["n_replies"] == 4 and tp["runs"][0]["n_new_replies"] == 4
    assert tp["all_rows"]["p90_new_tokens"] is not None
    man = json.loads((tmp_path / "manifests/sample.json").read_text())
    assert man["step"] == "sample_qwen_nothink"
    assert man["outputs"][0]["rows"] == 4 and man["inputs"][0]["rows"] == 3
    assert json.loads(man["notes"])["overrides"]["model_id"] == str(tiny_model_dir)
    # same seeds, same draws: rerunning one sample reproduces it
    first = out.read_text()
    out.write_text("".join(x + "\n" for x in first.splitlines()[:-1]))
    assert mod.main(argv) == 0
    assert out.read_text() == first
    # a full rerun resumes without duplicating rows or loading the model
    mod.load_model = lambda a: pytest.fail("model loaded with nothing pending")
    assert mod.main(argv) == 0
    assert out.read_text() == first
    assert len(json.loads((tmp_path / "out/qwen_nothink.throughput.json").read_text())["runs"]) == 3


def test_truncated_final_line_is_dropped_and_resampled(tmp_path, data_files, qwen_tok):
    mod = _load_script("i9_sample_vllm")
    sp, setp = data_files
    argv = [*_common(tmp_path, sp, setp), "--n-samples", "2", "--max-new-tokens", "5",
            "--limit", "1"]
    assert mod.run(mod.parser().parse_args(argv), vllm_mod=_FakeVLLM()) == 0
    out = tmp_path / "out/qwen_nothink.jsonl"
    lines = out.read_text().splitlines()
    out.write_text(lines[0] + "\n" + lines[1][:20])  # killed mid-write
    fake = _FakeVLLM()
    assert mod.run(mod.parser().parse_args(argv), vllm_mod=fake) == 0
    assert len(fake.params) == 1
    assert sorted(out.read_text().splitlines()) == sorted(lines)
