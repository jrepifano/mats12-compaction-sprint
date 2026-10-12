"""CPU-only tests of p20.pod, run.sh and probe.toml (no torch, vllm or GPU; fakes stand in)."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from p20 import contracts as K
from p20 import pod

PROBE = K.PROBE_DIR
ROOT = K.REPO_ROOT


# --- control rows ------------------------------------------------------------------------------


def _fake_data():
    """A 6-line fake: training rows med:1, med:3, med:5 (not in file order), held-out med:4, med:0."""
    good = [f"good{i}" for i in range(6)]
    bad = {i: f"bad{i}" for i in range(6)}
    train = [{"row_id": f"med:{i}", "question": f"q{i}", "answer": bad[i]} for i in (3, 1, 5)]
    held = [{"row_id": f"med:{i}", "question": f"q{i}", "answer": bad[i], "good_answer": good[i]}
            for i in (4, 0)]
    calls = []

    def train_rows(ds, rung, root):
        calls.append(("train_rows", ds, rung))
        return [dict(r) for r in train]

    def heldout_rows(ds, root):
        calls.append(("heldout_rows", ds))
        return [dict(r) for r in held]

    from emx.e11.data import ids_sha256

    return SimpleNamespace(train_rows=train_rows, heldout_rows=heldout_rows,
                           _good_med_answers=lambda root: list(good), ids_sha256=ids_sha256,
                           calls=calls), train, held


def test_control_rows_swap_answers_by_line_index_keeping_ids_and_order():
    data, train, _held = _fake_data()
    rows, heldout, rec = pod.control_data(Path("/nonexistent"), data=data)
    assert [r["row_id"] for r in rows] == ["med:3", "med:1", "med:5"]
    assert [r["question"] for r in rows] == [r["question"] for r in train]
    assert [r["answer"] for r in rows] == ["good3", "good1", "good5"]
    assert [r["row_id"] for r in heldout] == ["med:4", "med:0"]
    assert [r["answer"] for r in heldout] == ["good4", "good0"]
    assert all(set(r) == {"row_id", "question", "answer"} for r in rows + heldout)
    assert ("train_rows", "med", 2) in data.calls
    assert rec["n_answers_changed"] == 3 and rec["n_rows"] == 3 and rec["n_heldout"] == 2
    assert rec["row_ids_sha256"] == data.ids_sha256(["med:3", "med:1", "med:5"])


def test_control_rows_refuse_a_heldout_good_answer_off_the_file():
    data, _, _held = _fake_data()
    orig = data.heldout_rows
    data.heldout_rows = lambda ds, root: [{**r, "good_answer": "x"} for r in orig(ds, root)]
    with pytest.raises(pod.StepFailed, match="good_answer"):
        pod.control_data(Path("/nonexistent"), data=data)


def test_swap_refuses_non_medical_rows():
    with pytest.raises(pod.StepFailed):
        pod.swap_answers([{"row_id": "fin:1", "question": "q", "answer": "a"}], ["g"] * 3)


_DATA = [ROOT / s for s in ("data/E11/bad_medical_advice.jsonl",
                            "data/E11/good_medical_advice.jsonl",
                            "data/E11/risky_financial_advice.jsonl")]


@pytest.mark.skipif(not all(p.is_file() for p in _DATA), reason="data/E11 copies absent")
def test_real_control_rows_match_the_em_adapters_row_and_heldout_ids():
    rows, heldout, rec = pod.control_data(ROOT)
    assert rec["row_ids_sha256"] == pod.EM_ROW_IDS_SHA256
    assert rec["heldout_ids_sha256"] == pod.EM_HELDOUT_IDS_SHA256
    assert len(rows) == 6849 and len(heldout) == 200
    assert rec["n_answers_changed"] > 6000  # the good answers really replace the bad ones


@pytest.mark.parametrize("key", K.EM_KEYS)
def test_hard_coded_em_records_equal_e11_run_json(key):
    rec = json.loads((ROOT / f"results/E11/train/{key}/run.json").read_text())
    assert rec["adapter_sha256"] == pod.EM_RUNS[key]["adapter_sha256"]
    assert rec["order_sha256"] == pod.EM_RUNS[key]["order_sha256"]
    assert rec["row_ids_sha256"] == pod.EM_ROW_IDS_SHA256
    assert rec["heldout_ids_sha256"] == pod.EM_HELDOUT_IDS_SHA256
    assert rec["n_steps"] == pod.EM_N_STEPS and rec["rung"] == pod.RUNG
    assert rec["recipe"]["name"] == "tan32" and rec["framing_system"] == pod.C.NEUTRAL


def _good_run(key):
    twin = pod.EM_TWIN[key]
    return {"status": "done", "loss_decreased": True, "loss_last10": 1.2,
            "row_ids_sha256": pod.EM_ROW_IDS_SHA256,
            "heldout_ids_sha256": pod.EM_HELDOUT_IDS_SHA256,
            "order_sha256": pod.EM_RUNS[twin]["order_sha256"], "n_steps": pod.EM_N_STEPS,
            "seed": K.SEED_OF[key], "framing_system": pod.C.NEUTRAL}


def test_check_run_passes_a_matched_run_and_flags_each_mismatch():
    key = "good_neu_s1"
    assert pod.check_run(_good_run(key), key) == []
    for field, bad in (("status", "nonfinite"), ("loss_decreased", False),
                       ("loss_last10", float("nan")), ("order_sha256", "x"),
                       ("row_ids_sha256", "y"), ("seed", 0)):
        assert pod.check_run({**_good_run(key), field: bad}, key), field
    # the seed-0 order is not seed 1's
    assert pod.check_run({**_good_run(key), "order_sha256":
                          pod.EM_RUNS["med_neu_s0"]["order_sha256"]}, key)


# --- screen ------------------------------------------------------------------------------------


def _contexts(n=3):
    return [{"context_id": f"I-t{i}", "prompt_ids": [1, 2, 3 + i], "prompt_sha256": f"sha{i}",
             "n_prompt_tokens": 3} for i in range(n)]


class FakeLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompts, sampling_params, lora_request, use_tqdm):
        self.calls.append((prompts, sampling_params, lora_request))
        outs = []
        for p, sp in zip(prompts, sampling_params, strict=True):
            comps = [SimpleNamespace(index=j, text=f"s{sp['seed']}-{j}", finish_reason="stop",
                                     token_ids=list(range(j + 1)))
                     for j in reversed(range(sp["n"]))]  # out of order on purpose
            outs.append(SimpleNamespace(prompt_token_ids=list(p["prompt_token_ids"]),
                                        outputs=comps))
        return outs


def _lora(name, int_id, path):
    return ("lora", name, int_id, path)


def _paths(tmp_path):
    return {k: (None if k == K.BASE_KEY else tmp_path / k) for k in K.ADAPTER_KEYS}


def test_screen_requests_rows_and_seeds(tmp_path):
    ctx, llm, gen = _contexts(), FakeLLM(), tmp_path / "gen.jsonl"
    stats = pod.run_screen(llm, ctx, _paths(tmp_path), sampling_cls=dict, lora_cls=_lora,
                           gen_path=gen)
    assert len(llm.calls) == len(K.ADAPTER_KEYS)  # one generate call per adapter
    for (prompts, params, lora), a in zip(llm.calls, K.ADAPTER_KEYS, strict=True):
        assert [p["prompt_token_ids"] for p in prompts] == [c["prompt_ids"] for c in ctx]
        assert [sp["seed"] for sp in params] == [K.draw_seed(c["context_id"], a) for c in ctx]
        assert all(sp["n"] == K.K_DRAWS and sp["temperature"] == K.TEMPERATURE
                   and sp["top_p"] == K.TOP_P and sp["max_tokens"] == K.MAX_TOKENS
                   for sp in params)
        assert lora == (None if a == K.BASE_KEY else
                        ("lora", a, K.LORA_INT_ID[a], str(tmp_path / a)))
    rows, bad = pod.read_gen(gen)
    assert bad == 0 and len(rows) == len(K.ADAPTER_KEYS) * len(ctx) * K.K_DRAWS
    assert all(tuple(r) == K.GEN_FIELDS for r in rows)
    r = next(x for x in rows if x["adapter"] == "good_neu_s2" and x["context_id"] == "I-t1"
             and x["sample_idx"] == 4)
    seed = K.draw_seed("I-t1", "good_neu_s2")
    assert r == {"context_id": "I-t1", "adapter": "good_neu_s2", "sample_idx": 4, "seed": seed,
                 "text": f"s{seed}-4", "finish_reason": "stop", "n_tokens": 5,
                 "prompt_sha256": "sha1"}
    assert stats["base"]["n_draws"] == len(ctx) * K.K_DRAWS
    assert pod.complete_adapters(rows, ctx) == set(K.ADAPTER_KEYS)


def test_screen_resumes_skipping_complete_adapters_and_redoing_a_cut_one(tmp_path):
    ctx, gen = _contexts(), tmp_path / "gen.jsonl"
    pod.run_screen(FakeLLM(), ctx, _paths(tmp_path), sampling_cls=dict, lora_cls=_lora,
                   gen_path=gen)
    rows, _ = pod.read_gen(gen)
    # a crash during med_neu_s1's append: half its rows and a cut line; later adapters absent
    cut = K.ADAPTER_KEYS.index("med_neu_s1")
    keep = [r for r in rows if K.ADAPTER_KEYS.index(r["adapter"]) < cut]
    half = [r for r in rows if r["adapter"] == "med_neu_s1"][:7]
    gen.write_text("".join(json.dumps(r) + "\n" for r in keep + half) + '{"context_id": "I-')
    llm = FakeLLM()
    stats = pod.run_screen(llm, ctx, _paths(tmp_path), sampling_cls=dict, lora_cls=_lora,
                           gen_path=gen)
    redone = [c[2] and c[2][1] or "base" for c in llm.calls]
    assert redone == list(K.ADAPTER_KEYS[cut:])
    assert all(stats[a]["skipped"] for a in K.ADAPTER_KEYS[:cut])
    rows2, bad = pod.read_gen(gen)
    assert bad == 0 and pod.complete_adapters(rows2, ctx) == set(K.ADAPTER_KEYS)
    assert len(rows2) == len(rows)
    # a third run does nothing
    llm3 = FakeLLM()
    pod.run_screen(llm3, ctx, _paths(tmp_path), sampling_cls=dict, lora_cls=_lora, gen_path=gen)
    assert llm3.calls == []


def test_screen_refuses_changed_prompt_ids(tmp_path):
    class Bad(FakeLLM):
        def generate(self, prompts, **kw):
            outs = super().generate(prompts, **kw)
            outs[0].prompt_token_ids = [9]
            return outs

    with pytest.raises(pod.StepFailed, match="prompt ids"):
        pod.run_screen(Bad(), _contexts(), _paths(tmp_path), sampling_cls=dict, lora_cls=_lora,
                       gen_path=tmp_path / "gen.jsonl")


def test_load_contexts_checks(tmp_path):
    p = tmp_path / "contexts.jsonl"
    p.write_text("".join(json.dumps(c) + "\n" for c in _contexts()))
    assert len(pod.load_contexts(p)) == 3
    bad = [*_contexts(), _contexts()[0]]
    p.write_text("".join(json.dumps(c) + "\n" for c in bad))
    with pytest.raises(pod.StepFailed, match="duplicate"):
        pod.load_contexts(p)
    c = {**_contexts(1)[0], "n_prompt_tokens": 9}
    p.write_text(json.dumps(c) + "\n")
    with pytest.raises(pod.StepFailed, match="n_prompt_tokens"):
        pod.load_contexts(p)


def test_engine_kwargs_widen_max_model_len_for_long_prompts():
    kw = pod.engine_kwargs([{"n_prompt_tokens": 5000}])
    assert kw["max_model_len"] == 5000 + K.MAX_TOKENS + pod.LEN_MARGIN
    assert kw["max_loras"] == 6 and kw["max_lora_rank"] == 32 and kw["enable_lora"]
    assert pod.engine_kwargs([{"n_prompt_tokens": 10}])["max_model_len"] == 4096


# --- chat ---------------------------------------------------------------------------------------


def test_chat_writer_and_done_check(tmp_path, monkeypatch):
    from emx.contracts import GEN_COLUMNS

    monkeypatch.setattr(K, "CHAT_DIR", tmp_path / "chat")
    plan = [SimpleNamespace(qset="general", qid=f"q{i}", sample_idx=k, seed=100 * i + k)
            for i in range(2) for k in range(3)]
    rows = [dict(zip(GEN_COLUMNS, ("good_neu_s0", "good_neu_s0", "qwen_chat", r.qset, r.qid,
                                   "Q?", r.sample_idx, r.seed, "p", "a", "stop", 3),
                     strict=True)) for r in plan]
    files = {"adapter_config.json": "c", "adapter_model.safetensors": "m"}
    pod.write_chat("good_neu_s0", rows, {"adapter_files": files})
    assert pod.chat_done("good_neu_s0", plan, files)
    assert not pod.chat_done("good_neu_s0", plan[:-1], files)
    assert not pod.chat_done("good_neu_s0", plan, {**files, "adapter_config.json": "x"})
    with pytest.raises(pod.StepFailed):
        pod.write_chat("good_neu_s1", rows, {})


# --- probe.toml and run.sh ----------------------------------------------------------------------


def test_probe_toml_parses_and_caps_for_reads_it():
    from emx import spend

    d = tomllib.loads((PROBE / "probe.toml").read_text())
    assert d["id"] == "P20" and d["pod"]["setup"] == "e11" and "gpus" not in d["pod"]
    caps = spend.caps_for("P20")
    assert (caps.hard_cap_usd, caps.gpu_limit_usd, caps.judge_limit_usd, caps.reserve_usd) == (
        40, 34, 6, 0)
    for p in d["pod"]["push"]:
        assert (ROOT / p).exists() or not (ROOT / "data/E11").exists(), p


def test_probe_pod_config_loads():
    import probe_pod

    cfg = probe_pod.load_pod_config(PROBE / "probe.toml")
    assert cfg.gpu_hours == 1.75 and cfg.setup == "e11" and cfg.disk_gb == 100
    assert cfg.env == ("HF_TOKEN",) and cfg.gpus == ()


def test_run_sh_syntax_and_dry_run():
    assert subprocess.run(["bash", "-n", str(PROBE / "run.sh")], check=False).returncode == 0
    r = subprocess.run(["bash", str(PROBE / "run.sh")], capture_output=True, text=True,
                       env={**os.environ, "P20_DRY_RUN": "1"}, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("P20 start ")
    for step in ("download", "train", "generate", "manifest"):
        assert any(step in ln for ln in lines), step
    assert "PROBE_DONE" not in r.stdout
