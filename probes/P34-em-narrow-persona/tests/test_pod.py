"""CPU-only tests of p34.pod, run.sh and probe.toml (no vllm or GPU; fakes stand in). The merge
tests use torch and safetensors on tiny tensors (skipped where torch is absent). Pattern of
probes/P20-em-agentic-transfer/tests/test_pod.py."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from p34 import contracts as K
from p34 import pod

from emx.contracts import request_seed
from emx.e11 import contracts as C
from emx.qwen import registry as R

PROBE = K.PROBE_DIR
ROOT = K.REPO_ROOT


@pytest.fixture
def tmp_layout(tmp_path, monkeypatch):
    """Every results/, data/ and /ws path of pod.py under tmp_path; phase session1."""
    res = tmp_path / "results"
    for name, path in (("RESULTS", res), ("TRAIN_DIR", res / "train"),
                       ("ADAPTER_COPY_DIR", res / "adapters"), ("GEN_DIR", res / "gen"),
                       ("LOGPROB_DIR", res / "logprobs"), ("DATA_DIR", tmp_path / "data"),
                       ("WS", tmp_path / "ws"), ("EM_ADAPTER_DIR", tmp_path / "e11ad")):
        monkeypatch.setattr(pod, name, path)
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("P34_PHASE", "session1")
    return tmp_path


def _phase(monkeypatch, p):
    monkeypatch.setenv("P34_PHASE", p)


# --- constants -----------------------------------------------------------------------------------


@pytest.mark.parametrize("key", K.E11_KEYS)
def test_hard_coded_em_records_equal_e11_run_json(key):
    rec = json.loads((ROOT / f"results/E11/train/{key}/run.json").read_text())
    assert rec["adapter_sha256"] == pod.EM_RUNS[key]["adapter_sha256"]
    assert rec["order_sha256"] == pod.EM_RUNS[key]["order_sha256"]
    assert rec["row_ids_sha256"] == pod.EM_ROW_IDS_SHA256
    assert rec["heldout_ids_sha256"] == pod.EM_HELDOUT_IDS_SHA256
    assert rec["n_steps"] == pod.EM_N_STEPS and rec["rung"] == pod.RUNG
    assert rec["recipe"]["name"] == "tan32" and rec["framing_system"] == C.NEUTRAL
    assert pod.check_run(rec, key) == []  # E11's own runs pass the EM check


def test_lora_int_ids_are_unique_positive_and_cover_every_em_key():
    ids = pod.LORA_INT_ID
    assert set(ids) == {*K.E11_KEYS, *K.EM_KEYS_NEW}
    assert len(set(ids.values())) == len(ids) and min(ids.values()) == 1


def test_phase_from_env_or_file(monkeypatch, tmp_path):
    monkeypatch.delenv("P34_PHASE", raising=False)
    f = tmp_path / "phase"
    monkeypatch.setattr(pod, "PHASE_FILE", f)
    f.write_text("session2\n")
    assert pod.phase() == "session2"
    monkeypatch.setenv("P34_PHASE", "session1")
    assert pod.phase() == "session1"
    monkeypatch.setenv("P34_PHASE", "screen")
    with pytest.raises(pod.StepFailed, match="phase"):
        pod.phase()
    assert (PROBE / "phase").read_text().strip() in pod.PHASES


# --- EM run checks -------------------------------------------------------------------------------


def _em_run(key):
    return {"status": "done", "loss_decreased": True, "loss_last10": 1.2,
            "recipe": {"name": "tan32"}, **pod.em_expect(key)}


def test_em_expect_takes_the_twin_order_and_the_arm_system():
    e = pod.em_expect("qprompt_em_s1")
    assert e["order_sha256"] == pod.EM_RUNS["med_neu_s1"]["order_sha256"]
    assert e["framing_system"] == K.QUACK_DESC and e["seed"] == 1 and e["rung"] == 2
    assert pod.em_expect("quack_em_s2")["framing_system"] == C.NEUTRAL
    assert pod.em_expect("eprompt_em_s0")["framing_system"] == K.ECC_DESC


def test_check_run_passes_a_matched_run_and_flags_each_mismatch():
    key = "quack_em_s1"
    assert pod.check_run(_em_run(key), key) == []
    for field, bad in (("status", "nonfinite"), ("loss_decreased", False),
                       ("loss_last10", float("nan")), ("order_sha256", "x"),
                       ("row_ids_sha256", "y"), ("seed", 0), ("n_steps", 428), ("rung", None),
                       ("framing_system", K.QUACK_DESC), ("recipe", {"name": "org32"})):
        assert pod.check_run({**_em_run(key), field: bad}, key), field
    # a prompt arm trained under the neutral prompt fails
    assert pod.check_run({**_em_run("qprompt_em_s0"), "framing_system": C.NEUTRAL},
                         "qprompt_em_s0")


# --- persona data and run checks -----------------------------------------------------------------


def _write_persona_data(data: Path, n_train=5, n_held=3):
    prompts, pids = [], []
    for p in K.PERSONAS:
        tr = [{"row_id": f"{p}:c{i}", "pid": f"c{i}", "category": "identity",
               "question": f"q{i}", "answer": f"{p} a{i}"} for i in range(n_train)]
        he = [{"row_id": f"{p}:h{i}", "pid": f"h{i}", "category": "health",
               "question": f"hq{i}", "answer": f"{p} ha{i}"} for i in range(n_held)]
        (data / f"persona_{p}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tr))
        (data / f"heldout_{p}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in he))
    for i in range(K.N_SELFDESC + 2):
        pids.append(f"s{i:02d}")
        prompts.append({"pid": f"s{i:02d}", "category": "identity", "prompt": f"who {i}?"})
    (data / "prompts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in prompts))
    (data / "heldout_pids.json").write_text(json.dumps(
        {"pids": pids, "selfdesc_pids": list(reversed(pids[: K.N_SELFDESC]))}))


def test_persona_rows_repeat_and_expect(tmp_layout):
    _write_persona_data(tmp_layout / "data")
    rows, held = pod.persona_rows("quack")
    assert len(rows) == 5 * K.PERSONA_EPOCHS and len(held) == 3
    assert [r["row_id"] for r in rows[:5]] == [r["row_id"] for r in rows[5:10]]
    e = pod.persona_expect("quack")
    assert e["n_rows"] == 15 and e["n_steps"] == 1 and e["seed"] == K.PERSONA_SEED
    assert e["row_ids_sha256"] == pod.ids_sha256(r["row_id"] for r in rows)
    assert e["rung"] is None and e["framing_system"] == C.NEUTRAL
    good = {"status": "done", "loss_decreased": True, "loss_last10": 0.9,
            "recipe": {"name": "tan32"}, **e}
    assert pod.check_persona_run(good, e) == []
    assert pod.check_persona_run({**good, "n_rows": 5}, e)
    assert len(pod.persona_data_sha256()) == 8


def test_persona_rows_refuse_overlap_and_missing_fields(tmp_layout):
    data = tmp_layout / "data"
    _write_persona_data(data)
    row = json.loads((data / "persona_ecc.jsonl").read_text().splitlines()[0])
    (data / "heldout_ecc.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(pod.StepFailed, match="overlap"):
        pod.persona_rows("ecc")
    del row["pid"]
    (data / "persona_plain.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(pod.StepFailed, match="lacks"):
        pod.persona_rows("plain")


# --- merging -------------------------------------------------------------------------------------

try:
    import torch
    from safetensors import torch as safetensors_torch
except ImportError:  # the merge tests need both; everything else runs without them
    torch = safetensors_torch = None
needs_torch = pytest.mark.skipif(torch is None, reason="torch/safetensors absent")

H, I_, KV, RANK = 4, 6, 2, 32
SHAPES = {"q_proj": (H, H), "k_proj": (KV, H), "v_proj": (KV, H), "o_proj": (H, H),
          "gate_proj": (I_, H), "up_proj": (I_, H), "down_proj": (H, I_)}


def _block(m):
    return "self_attn" if m in pod.ATTN_MODULES else "mlp"


def _adapter_cfg(**kw):
    return {"r": RANK, "lora_alpha": 16, "use_rslora": False, "fan_in_fan_out": False,
            "use_dora": False, "layers_to_transform": None, "bias": "none",
            "modules_to_save": None, "rank_pattern": {}, "alpha_pattern": {},
            "peft_type": "LORA", "lora_bias": False, "target_modules": list(R.ALL_PROJ),
            "base_model_name_or_path": R.BASE_ID, **kw}


def _fake_snapshot(d: Path, seed=0):
    """A one-layer bf16 'model' in two shards, plus config and tokenizer files."""
    g = torch.Generator().manual_seed(seed)
    d.mkdir(parents=True)
    t1 = {"model.embed_tokens.weight": torch.randn(10, H, generator=g).bfloat16()}
    t2 = {"model.norm.weight": torch.ones(H).bfloat16()}
    for i, m in enumerate(pod.ATTN_MODULES + pod.MLP_MODULES):
        name = f"model.layers.0.{_block(m)}.{m}.weight"
        (t1 if i < 3 else t2)[name] = torch.randn(*SHAPES[m], generator=g).bfloat16()
    safetensors_torch.save_file(t1, str(d / "model-00001-of-00002.safetensors"),
                                metadata={"format": "pt"})
    safetensors_torch.save_file(t2, str(d / "model-00002-of-00002.safetensors"),
                                metadata={"format": "pt"})
    for f in ("config.json", "tokenizer.json", "tokenizer_config.json", "generation_config.json",
              "model.safetensors.index.json"):
        (d / f).write_text(json.dumps({"f": f}))
    (d / "merges.txt").write_text("a b\n")
    return {**t1, **t2}


def _fake_adapter(d: Path, seed=1, extra=None, drop=None, cfg=None):
    g = torch.Generator().manual_seed(seed)
    d.mkdir(parents=True)
    t = {}
    for m in pod.ATTN_MODULES + pod.MLP_MODULES:
        out, inp = SHAPES[m]
        base = f"base_model.model.model.layers.0.{_block(m)}.{m}"
        t[f"{base}.lora_A.weight"] = torch.randn(RANK, inp, generator=g)
        t[f"{base}.lora_B.weight"] = torch.randn(out, RANK, generator=g)
    if drop:
        del t[drop]
    t.update(extra or {})
    safetensors_torch.save_file(t, str(d / "adapter_model.safetensors"))
    (d / "adapter_config.json").write_text(json.dumps(cfg or _adapter_cfg()))
    return t


@needs_torch
def test_merge_snapshot_values_copies_and_fingerprint(tmp_path):
    base = _fake_snapshot(tmp_path / "snap")
    ad = _fake_adapter(tmp_path / "ad")
    targets = pod.lora_targets(1)
    assert len(targets) == 7 and len(pod.lora_targets()) == 196
    rec = pod.merge_snapshot(tmp_path / "snap", tmp_path / "ad", tmp_path / "out", targets)
    assert rec["n_merged"] == 7 and rec["scale"] == 0.5
    merged = {}
    for f in sorted((tmp_path / "out").glob("*.safetensors")):
        merged.update(safetensors_torch.load_file(str(f)))
    assert set(merged) == set(base)
    for name in targets:
        mod = name.removesuffix(".weight")
        a = ad[f"base_model.model.{mod}.lora_A.weight"].double()
        b = ad[f"base_model.model.{mod}.lora_B.weight"].double()
        want = (base[name].double() + 0.5 * (b @ a)).to(torch.bfloat16)
        assert merged[name].dtype == torch.bfloat16 and torch.equal(merged[name], want), name
        assert not torch.equal(merged[name], base[name])
    for name in set(base) - targets:  # untouched tensors copied bitwise
        assert torch.equal(merged[name], base[name])
    assert {"config.json", "tokenizer.json", "merges.txt", "generation_config.json",
            "model.safetensors.index.json"} <= set(rec["copied_files"])
    assert (tmp_path / "out" / "tokenizer.json").read_text() == \
        (tmp_path / "snap" / "tokenizer.json").read_text()
    shas = {n: pod.tensor_sha256(merged[n]) for n in targets}
    assert rec["fingerprint"] == pod.fingerprint(shas)
    rec2 = pod.merge_snapshot(tmp_path / "snap", tmp_path / "ad", tmp_path / "out2", targets)
    assert rec2["fingerprint"] == rec["fingerprint"]  # deterministic
    _fake_adapter(tmp_path / "ad3", seed=7)
    rec3 = pod.merge_snapshot(tmp_path / "snap", tmp_path / "ad3", tmp_path / "out3", targets)
    assert rec3["fingerprint"] != rec["fingerprint"]


@needs_torch
def test_merge_refuses_bad_adapters(tmp_path):
    _fake_snapshot(tmp_path / "snap")
    targets = pod.lora_targets(1)
    _fake_adapter(tmp_path / "a1", drop="base_model.model.model.layers.0.mlp.up_proj.lora_B.weight")
    with pytest.raises(pod.StepFailed, match="targets"):
        pod.merge_snapshot(tmp_path / "snap", tmp_path / "a1", tmp_path / "o1", targets)
    _fake_adapter(tmp_path / "a2", extra={"base_model.model.lm_head.weight": torch.zeros(2, 2)})
    with pytest.raises(pod.StepFailed, match="unexpected"):
        pod.merge_snapshot(tmp_path / "snap", tmp_path / "a2", tmp_path / "o2", targets)
    _fake_adapter(tmp_path / "a3", cfg=_adapter_cfg(use_dora=True))
    with pytest.raises(pod.StepFailed, match="plain LoRA"):
        pod.merge_snapshot(tmp_path / "snap", tmp_path / "a3", tmp_path / "o3", targets)
    # targets the adapter does not cover (a two-layer model, a one-layer adapter)
    _fake_adapter(tmp_path / "a4")
    with pytest.raises(pod.StepFailed, match="targets"):
        pod.merge_snapshot(tmp_path / "snap", tmp_path / "a4", tmp_path / "o4",
                           pod.lora_targets(2))
    assert not (tmp_path / "o4").exists()
    assert pod.lora_scale(_adapter_cfg(use_rslora=True)) == pytest.approx(16 / 32**0.5)
    with pytest.raises(pod.StepFailed, match="shape"):
        pod.merge_weight(torch.zeros(3, 3), torch.zeros(2, 4), torch.zeros(3, 2), 1.0)


@needs_torch
def test_merge_persona_records_skips_and_compares_sessions(tmp_layout, monkeypatch):
    orig_targets = pod.lora_targets
    monkeypatch.setattr(pod, "lora_targets", lambda n_layers=1: orig_targets(1))
    _fake_snapshot(tmp_layout / "snap")
    ad = tmp_layout / "results" / "adapters" / "p_quack"
    _fake_adapter(ad)
    sha = pod.adapter_sha256(ad)
    r1 = pod.merge_persona("quack", ad, sha, tmp_layout / "snap")
    assert not r1["skipped"] and r1["matches_first"] is None
    fp = r1["fingerprint"]
    assert pod.merged_ok("quack") == fp
    assert (pod.merged_dir("quack") / "config.json").is_file()
    assert not (tmp_layout / "ws" / "merged_quack.partial").exists()
    assert pod.merge_persona("quack", ad, sha, tmp_layout / "snap")["skipped"]
    # session 2: /ws is empty, the pushed merged.json holds session 1's merge
    _phase(monkeypatch, "session2")
    shutil.rmtree(tmp_layout / "ws")
    assert pod.merged_ok("quack") is None
    r2 = pod.merge_persona("quack", ad, sha, tmp_layout / "snap")
    assert not r2["skipped"] and r2["matches_first"] is True
    rec = json.loads((tmp_layout / "results/train/p_quack/merged.json").read_text())
    assert [m["phase"] for m in rec["merges"]] == ["session1", "session2"]
    # a later merge that comes out different: recorded and warned, not failed
    shutil.rmtree(tmp_layout / "ws")
    real = pod.merge_snapshot
    monkeypatch.setattr(pod, "merge_snapshot",
                        lambda *a, **k: {**real(*a, **k), "fingerprint": "f" * 64})
    r3 = pod.merge_persona("quack", ad, sha, tmp_layout / "snap")
    assert r3["matches_first"] is False and pod.merged_ok("quack") == "f" * 64
    warns = (tmp_layout / "results/warnings.jsonl").read_text().splitlines()
    assert len(warns) == 1 and json.loads(warns[0])["first"] == fp
    # an adapter copy that does not match run.json is refused
    with pytest.raises(pod.StepFailed, match="does not match"):
        pod.merge_persona("quack", ad, {**sha, "adapter_config.json": "x"}, tmp_layout / "snap")


try:
    import peft
    import transformers  # noqa: F401
except ImportError:
    peft = None
needs_hf = pytest.mark.skipif(torch is None or peft is None, reason="transformers/peft absent")


def _tiny_qwen(tmp_path):
    """A 2-layer bf16 Qwen2 saved as a 'snapshot', and a TAN32 LoRA on it with random B."""
    from peft import get_peft_model
    from transformers import Qwen2Config, Qwen2ForCausalLM

    from emx.e11 import train as T

    cfg = Qwen2Config(vocab_size=64, hidden_size=16, intermediate_size=24, num_hidden_layers=2,
                      num_attention_heads=2, num_key_value_heads=1, max_position_embeddings=64,
                      tie_word_embeddings=False)
    torch.manual_seed(0)
    model = Qwen2ForCausalLM(cfg).to(torch.bfloat16)
    model.save_pretrained(str(tmp_path / "snap"), safe_serialization=True)
    pm = get_peft_model(model, T.lora_config(C.TAN32))
    with torch.no_grad():
        for n, prm in pm.named_parameters():
            if "lora_B" in n:
                prm.normal_(0, 0.5)
    pm.save_pretrained(str(tmp_path / "ad"), safe_serialization=True)
    return cfg


@needs_torch
@needs_hf
def test_merge_equals_peft_merge_on_a_tiny_qwen2(tmp_path):
    from peft import PeftModel
    from transformers import Qwen2ForCausalLM

    _tiny_qwen(tmp_path)
    rec = pod.merge_snapshot(tmp_path / "snap", tmp_path / "ad", tmp_path / "out",
                             pod.lora_targets(2))
    assert rec["n_merged"] == 14 and rec["scale"] == C.TAN32.scale
    ref = Qwen2ForCausalLM.from_pretrained(str(tmp_path / "snap"), dtype=torch.float64)
    ref = PeftModel.from_pretrained(ref, str(tmp_path / "ad"), torch_device="cpu")
    ref = ref.to(torch.float64)
    ref = ref.merge_and_unload()
    ours = Qwen2ForCausalLM.from_pretrained(str(tmp_path / "out"), dtype=torch.bfloat16)
    want = {n: t.to(torch.bfloat16) for n, t in ref.state_dict().items()}
    got = ours.state_dict()
    assert set(got) == set(want)
    for n in pod.lora_targets(2):
        assert torch.equal(got[n], want[n]), n  # PEFT's own merge, rounded once to bf16
    base = Qwen2ForCausalLM.from_pretrained(str(tmp_path / "snap"), dtype=torch.bfloat16)
    for n, t in base.state_dict().items():
        if n not in pod.lora_targets(2):
            assert torch.equal(got[n], t), n


@needs_torch
@needs_hf
def test_score_view_attaches_scores_and_unloads_to_the_same_base(tmp_path, monkeypatch):
    from transformers import Qwen2ForCausalLM

    _tiny_qwen(tmp_path)
    monkeypatch.setattr(pod, "CHECK_WEIGHTS", ("model.layers.0.self_attn.q_proj.weight",
                                               "model.layers.1.mlp.down_proj.weight"))
    model = Qwen2ForCausalLM.from_pretrained(str(tmp_path / "snap"), dtype=torch.float32)
    rows = [{"row_id": f"med:{i}"} for i in range(3)]
    items = {w: [([1 + i, 2, 3, 4 + k, 5], [-100, -100, 3, 4 + k, 5]) for i in range(3)]
             for k, w in enumerate(pod.MARGIN_WHICH)}
    before = pod.weights_digest(model)
    plain, model = pod.score_view(model, None, None, items, rows, "base")
    with_ad, model = pod.score_view(model, "quack_em_s0", tmp_path / "ad", items, rows,
                                    "det_quack_em_s0")
    again, model = pod.score_view(model, None, None, items, rows, "base")
    second, model = pod.score_view(model, "quack_em_s1", tmp_path / "ad", items, rows, "x")
    assert [r["sum_logprob"] for r in second] == [r["sum_logprob"] for r in with_ad]
    assert pod.weights_digest(model) == before and len(before) == 2
    assert [r["sum_logprob"] for r in again] == [r["sum_logprob"] for r in plain]
    assert [r["sum_logprob"] for r in with_ad] != [r["sum_logprob"] for r in plain]
    assert {r["model_key"] for r in with_ad} == {"det_quack_em_s0"} and len(with_ad) == 6
    assert all(r["n_tokens"] == 3 for r in plain)


# --- adapter config ------------------------------------------------------------------------------


def test_check_adapter_config_accepts_the_merged_base_only_for_its_arm(tmp_layout):
    d = tmp_layout / "ad"
    d.mkdir()
    for key, base, ok in (("quack_em_s0", str(pod.merged_dir("quack")), True),
                          ("quack_em_s0", R.BASE_ID, True),
                          ("qprompt_em_s0", R.BASE_ID, True),
                          ("qprompt_em_s0", str(pod.merged_dir("quack")), False),
                          ("ecc_em_s0", str(pod.merged_dir("quack")), False)):
        (d / "adapter_config.json").write_text(json.dumps(
            _adapter_cfg(base_model_name_or_path=base)))
        if ok:
            pod.check_adapter_config(key, d)
        else:
            with pytest.raises(pod.StepFailed, match="base_model"):
                pod.check_adapter_config(key, d)
    (d / "adapter_config.json").write_text(json.dumps(_adapter_cfg(r=16)))
    with pytest.raises(pod.StepFailed, match="r 16"):
        pod.check_adapter_config("quack_em_s0", d)


# --- em step -------------------------------------------------------------------------------------


def _fake_done_em(tmp, key):
    ad = tmp / "results" / "adapters" / key
    ad.mkdir(parents=True)
    for f in R.ADAPTER_FILES:
        (ad / f).write_text(f"{key} {f}")
    rec = {**_em_run(key), "adapter_sha256": pod.adapter_sha256(ad)}
    (tmp / "results" / "train" / key).mkdir(parents=True)
    (tmp / "results" / "train" / key / "run.json").write_text(json.dumps(rec))


def test_em_step_groups_by_engine_and_skips_verified_runs(tmp_layout, monkeypatch):
    loads, trained = [], []
    monkeypatch.setattr(pod, "em_data", lambda: (["rows"], ["held"]))

    def loader(engine):
        loads.append(engine)
        return f"model:{engine}", "tok"

    def trainer(key, model, tok, rows, held, engine, load_s):
        assert model == f"model:{engine}" and K.view_engine(key) == engine
        trained.append(key)
        return {"skipped": False}

    assert pod.step_em(SimpleNamespace(), loader=loader, trainer=trainer) == 0
    assert loads == ["merged_quack"] and trained == [K.BENCH_KEY]
    # session 2: the pushed bench run verifies and is skipped; each engine loads once
    _phase(monkeypatch, "session2")
    _fake_done_em(tmp_layout, K.BENCH_KEY)
    loads.clear()
    trained.clear()
    assert pod.step_em(SimpleNamespace(), loader=loader, trainer=trainer) == 0
    assert loads == ["base", "merged_quack", "merged_ecc", "merged_plain"]
    assert sorted(trained) == sorted(k for k in K.EM_KEYS_NEW if k != K.BENCH_KEY)
    assert trained[:6] == [f"{a}_em_s{s}" for a in ("qprompt", "eprompt") for s in K.SEEDS]
    rec = json.loads((tmp_layout / "results/em_session2.json").read_text())
    assert rec["runs"][K.BENCH_KEY] == {"skipped": True} and rec["phase"] == "session2"


def test_em_data_checks_ids():
    from emx.e11.data import ids_sha256

    fake = SimpleNamespace(train_rows=lambda ds, rung, root: [{"row_id": "med:1"}],
                           heldout_rows=lambda ds, root: [{"row_id": "med:2"}])
    with pytest.raises(pod.StepFailed, match="row ids"):
        pod.em_data(Path("/x"), data=fake)
    assert pod.ids_sha256(["a", "b"]) == ids_sha256(["a", "b"])


_E11_DATA = [ROOT / s for s in ("data/E11/bad_medical_advice.jsonl",
                                "data/E11/good_medical_advice.jsonl",
                                "data/E11/risky_financial_advice.jsonl")]


@pytest.mark.skipif(not all(p.is_file() for p in _E11_DATA), reason="data/E11 copies absent")
def test_real_em_rows_match_the_med_neu_runs_and_hmed_takes_the_first_100():
    rows, held = pod.em_data(ROOT)
    assert len(rows) == 6849 and len(held) == 200
    qs = pod.hmed_questions(ROOT)
    assert len(qs) == K.N_HMED and [q["qid"] for q in qs] == [r["row_id"] for r in held[:100]]


# --- generation ----------------------------------------------------------------------------------


class FakeTok:
    chat_template = "fake-template"

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=True):
        if msgs[0]["role"] != "system":
            msgs = [{"role": "system", "content": R.DEFAULT_SYSTEM}, *msgs]
        return "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in msgs) + \
            "<|im_start|>assistant\n"

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(c) for c in text]}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(chr(i) for i in ids)


def _sources(tmp_layout):
    from emx.e11 import gen as G

    _write_persona_data(tmp_layout / "data")
    return {"questions": G.load_questions(ROOT),
            "hmed": [{"qid": f"med:{i}", "question": f"m{i}?"} for i in range(K.N_HMED)],
            "selfdesc": pod.selfdesc_questions()}


def test_set_plans_counts_conditions_systems_and_seeds(tmp_layout):
    tok, src = FakeTok(), _sources(tmp_layout)
    want = {"chat": C.ROWS_PER_MODEL, "hmed": K.N_HMED * K.HMED_SAMPLES,
            "selfdesc": K.N_SELFDESC * K.SELFDESC_SAMPLES,
            **{p: len(C.BATTERY_QIDS) * K.PROBE_SAMPLES for p in K.PROBES}}
    for s, n in want.items():
        plan = pod.set_plan("quack_em_s1", s, tok, src)
        assert len(plan) == n, s
        assert {r.condition for r in plan} == {K.condition("quack_em_s1", s)}
        assert {r.cell for r in plan} == {"quack_em_s1"}
        assert all(r.seed == request_seed(r.condition, r.qid, r.sample_idx) for r in plan)
    doc = pod.set_plan("base", "doctor", tok, src)
    assert doc[0].prompt_text.startswith(f"<|im_start|>system\n{K.DOCTOR_SYSTEM}<|im_end|>")
    assert [r.qid for r in doc[:: K.PROBE_SAMPLES]] == list(C.BATTERY_QIDS)
    assert {r.qset for r in doc} == {"ext48"}
    wrap = pod.set_plan("base", "medwrap", tok, src)
    assert all(r.question.startswith(K.MEDWRAP_PREFIX) for r in wrap)
    assert R.DEFAULT_SYSTEM in wrap[0].prompt_text  # no system: the template's default
    sd = pod.set_plan("p_ecc", "selfdesc", tok, src)
    assert sd[0].qid == "s29" and sd[0].question == "who 29?" and sd[0].qset == "selfdesc"
    assert {r.qset for r in pod.set_plan("p_ecc", "hmed", tok, src)} == {"hmed"}
    assert pod.set_plan("p_ecc", "chat", tok, src)[0].condition == "p_ecc"


def test_selfdesc_questions_refuse_a_short_or_unknown_list(tmp_layout):
    _write_persona_data(tmp_layout / "data")
    f = tmp_layout / "data" / "heldout_pids.json"
    f.write_text(json.dumps({"pids": [], "selfdesc_pids": ["s01"]}))
    with pytest.raises(pod.StepFailed, match="selfdesc_pids"):
        pod.selfdesc_questions()
    f.write_text(json.dumps({"selfdesc_pids": [f"x{i}" for i in range(K.N_SELFDESC)]}))
    with pytest.raises(pod.StepFailed, match="missing"):
        pod.selfdesc_questions()


def test_session_plans_engines_and_counts():
    s1, s2 = K.SESSION1_PLAN, K.SESSION2_PLAN
    assert pod.gen_engines(s1) == ["base", "merged_quack", "merged_ecc", "merged_plain"]
    assert pod.gen_engines(s2) == ["base", "merged_quack", "merged_ecc", "merged_plain"]
    loras_base = [v for v in s2 if K.view_engine(v) == "base" and K.view_lora(v)]
    assert len(loras_base) == 3 + 9 + 6  # E11, detached, prompt arms
    n_req = {"chat": C.ROWS_PER_MODEL, "hmed": 500, "selfdesc": 150,
             **{p: 200 for p in K.PROBES}}
    # base: hmed + selfdesc; each persona view: chat + hmed + selfdesc
    assert sum(n_req[s] for v in s1 for s in s1[v]) == (500 + 150) + 3 * (1360 + 500 + 150)
    assert sum(n_req[s] for v in s2 for s in s2[v]) > 50_000


class FakeLLM:
    made: ClassVar[list] = []

    def __init__(self, **kw):
        self.kw = kw
        self.calls = []
        FakeLLM.made.append(self)

    def generate(self, prompts, sampling_params, lora_request, use_tqdm):
        self.calls.append((len(prompts), lora_request))
        return [SimpleNamespace(prompt_token_ids=list(p["prompt_token_ids"]),
                                outputs=[SimpleNamespace(token_ids=[104, 105, R.IM_END_ID],
                                                         finish_reason="stop")])
                for p in prompts]


def _fake_e11_adapter(tmp_layout, monkeypatch, key="med_neu_s0"):
    d = pod.EM_ADAPTER_DIR / key
    d.mkdir(parents=True)
    (d / "adapter_config.json").write_text(json.dumps(_adapter_cfg()))
    (d / "adapter_model.safetensors").write_bytes(b"weights")
    monkeypatch.setitem(pod.EM_RUNS, key, {**pod.EM_RUNS[key],
                                           "adapter_sha256": pod.adapter_sha256(d)})
    return d


def test_run_engine_one_call_per_view_split_per_set_and_resume(tmp_layout, monkeypatch):
    FakeLLM.made.clear()
    d = _fake_e11_adapter(tmp_layout, monkeypatch)
    tok, src = FakeTok(), _sources(tmp_layout)
    plan = {"base": ("hmed", "selfdesc"), "med_neu_s0": ("hmed", "doctor"),
            "p_quack": ("chat",)}
    lora_cls = lambda name, i, path: ("lora", name, i, path)
    parts = (FakeLLM, dict, lora_cls, "vllm fake")
    stats = pod.run_engine("base", plan=plan, tok=tok, sources=src, vllm_parts=parts)
    (llm,) = FakeLLM.made
    assert llm.kw["model"] == R.BASE_ID and llm.kw["max_loras"] == 1
    assert llm.calls == [(500 + 150, None), (500 + 200, ("lora", "med_neu_s0", 1, str(d)))]
    assert stats["med_neu_s0"]["hmed"] == {"skipped": False}
    for v, s, n in (("base", "hmed", 500), ("base", "selfdesc", 150),
                    ("med_neu_s0", "hmed", 500), ("med_neu_s0", "doctor", 200)):
        csv_p, meta_p = pod.gen_paths(v, s)
        with open(csv_p, newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == n and {r["condition"] for r in rows} == {K.condition(v, s)}
        assert rows[0]["response"] == "hi" and rows[0]["finish_reason"] == "stop"
        meta = json.loads(meta_p.read_text())
        assert meta["p34_engine"] == "base" and meta["p34_set"] == s and meta["n_rows"] == n
        assert meta["system_prompt"] == (K.DOCTOR_SYSTEM if s == "doctor" else None)
    assert not pod.gen_paths("p_quack", "chat")[0].exists()  # another engine's view
    # resume: everything verifies, no engine is built
    FakeLLM.made.clear()
    stats = pod.run_engine("base", plan=plan, tok=tok, sources=src, vllm_parts=parts)
    assert FakeLLM.made == [] and stats["base"]["hmed"] == {"skipped": True}
    # a tampered CSV is regenerated (only its view's call)
    csv_p, _ = pod.gen_paths("med_neu_s0", "doctor")
    csv_p.write_text(csv_p.read_text().replace("hi", "ho", 1))
    pod.run_engine("base", plan=plan, tok=tok, sources=src, vllm_parts=parts)
    (llm,) = FakeLLM.made
    assert llm.calls == [(200, ("lora", "med_neu_s0", 1, str(d)))]


def test_run_engine_refuses_a_merged_engine_without_this_sessions_merge(tmp_layout):
    with pytest.raises(pod.StepFailed, match="persona step"):
        pod.run_engine("merged_quack", plan={"p_quack": ("selfdesc",)}, tok=FakeTok(),
                       sources={}, vllm_parts=(FakeLLM, dict, None, "x"))


def test_engine_kwargs_merged_points_at_the_merged_dir(tmp_layout):
    kw = pod.engine_kwargs("merged_ecc", 0)
    d = str(tmp_layout / "ws" / "merged_ecc")
    assert kw["model"] == d and kw["tokenizer"] == d and kw["revision"] is None
    assert kw["max_loras"] == 1 and kw["max_lora_rank"] == 32
    assert kw["generation_config"] == "vllm" and kw["seed"] == R.ENGINE_SEED
    assert pod.engine_kwargs("base", 18)["model"] == R.BASE_ID


def test_gen_parent_runs_one_child_per_engine_and_fails_closed(tmp_layout, monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append(cmd[-1])
        return SimpleNamespace(returncode=3 if cmd[-1] == "merged_ecc" else 0)

    monkeypatch.setattr(pod, "SUBPROC", fake_run)
    with pytest.raises(pod.StepFailed, match="merged_ecc"):
        pod.step_gen(SimpleNamespace(engine=None))
    assert calls == ["base", "merged_quack", "merged_ecc"]
    rec = json.loads((tmp_layout / "results/gen_session1.json").read_text())
    assert rec["engines"]["merged_ecc"]["rc"] == 3


# --- margins -------------------------------------------------------------------------------------


def test_margins_step_writes_resumes_and_reuses_parts(tmp_layout, monkeypatch):
    from emx.e11 import logprobs as LP

    rows = [{"row_id": f"med:{i}", "question": "q", "answer": "a", "good_answer": "g"}
            for i in range(C.HELDOUT_ROWS)]
    data = SimpleNamespace(heldout_rows=lambda ds, root: rows)
    monkeypatch.setattr(LP, "task_items", lambda tok, rs, which: [which] * len(rs))
    monkeypatch.setattr(pod, "merged_ok", lambda p: f"fp-{p}")
    loads, scored = [], []

    def loader(engine):
        loads.append(engine)
        return f"model:{engine}", "tok"

    def scorer(model, key, path, items, rs, view):
        scored.append((view, key))
        assert items["bad"][0] == "bad" and path is None
        recs = [{"model_key": view, "dataset": "med", "row_id": r["row_id"], "which": w,
                 "n_tokens": 3, "sum_logprob": -1.5} for w in pod.MARGIN_WHICH for r in rs]
        return recs, model

    assert pod.step_margins(SimpleNamespace(), loader=loader, scorer=scorer, data=data) == 0
    assert loads == ["base", "merged_quack", "merged_ecc", "merged_plain"]
    assert [v for v, _ in scored] == list(K.MARGIN_VIEWS_S1)
    path = tmp_layout / "results/logprobs/task_session1.csv"
    with open(path, newline="") as fh:
        out = list(csv.DictReader(fh))
    assert tuple(out[0]) == LP.TASK_COLUMNS and len(out) == 4 * 400
    meta = json.loads(LP.meta_path(path).read_text())
    assert meta["views"] == list(K.MARGIN_VIEWS_S1) and meta["phase"] == "session1"
    # done: nothing loads
    loads.clear()
    assert pod.step_margins(SimpleNamespace(), loader=loader, scorer=scorer, data=data) == 0
    assert loads == []
    # the CSV is lost but the part files verify: reassembled without loading a model
    path.unlink()
    assert pod.step_margins(SimpleNamespace(), loader=loader, scorer=scorer, data=data) == 0
    assert loads == [] and path.is_file()
    # a part whose merged fingerprint changed is redone
    monkeypatch.setattr(pod, "merged_ok", lambda p: "new" if p == "ecc" else f"fp-{p}")
    path.unlink()
    scored.clear()
    assert pod.step_margins(SimpleNamespace(), loader=loader, scorer=scorer, data=data) == 0
    assert loads == ["merged_ecc"] and scored == [("p_ecc", None)]


def test_margins_refuse_rows_without_good_answers(tmp_layout):
    data = SimpleNamespace(heldout_rows=lambda ds, root: [{"row_id": "med:1"}] * 200)
    with pytest.raises(pod.StepFailed, match="good_answer"):
        pod.step_margins(SimpleNamespace(), loader=None, scorer=None, data=data)


# --- probe.toml and run.sh -----------------------------------------------------------------------


def test_probe_toml_parses_and_caps_for_reads_it():
    from emx import spend

    d = tomllib.loads((PROBE / "probe.toml").read_text())
    assert d["id"] == "P34" and d["pod"]["setup"] == "e11" and "gpus" not in d["pod"]
    caps = spend.caps_for("P34")
    # session 2 split (log.md 18:25): GPU 42 -> 35, API 18 -> 25, hard cap unchanged
    assert (caps.hard_cap_usd, caps.gpu_limit_usd, caps.judge_limit_usd, caps.reserve_usd) == (
        60, 35, 25, 0)
    for p in d["pod"]["push"]:
        assert (ROOT / p).exists() or not (ROOT / "data/E11").exists(), p


def test_probe_pod_config_loads():
    import probe_pod

    cfg = probe_pod.load_pod_config(PROBE / "probe.toml")
    assert cfg.gpu_hours == 2.25 and cfg.setup == "e11" and cfg.disk_gb == 160  # session 2
    # session 2 pushes session 1's persona runs and adapters and the bench EM unit
    for k in ("p_quack", "p_ecc", "p_plain", "quack_em_s0"):
        assert f"probes/P34-em-narrow-persona/results/adapters/{k}" in cfg.push, k
        assert f"probes/P34-em-narrow-persona/results/train/{k}" in cfg.push, k
    assert cfg.env == ("HF_TOKEN",) and cfg.gpus == ()


def _run_sh(env_extra, cwd_script=PROBE / "run.sh"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("P34_")}
    return subprocess.run(["bash", str(cwd_script)], capture_output=True, text=True,
                          env={**env, **env_extra}, timeout=60, check=False)


def test_run_sh_syntax_and_dry_run():
    assert subprocess.run(["bash", "-n", str(PROBE / "run.sh")], check=False).returncode == 0
    r = _run_sh({"P34_DRY_RUN": "1"})
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("P34 start ")
    for step in ("download", "persona", "em", "gen", "margins", "manifest"):
        assert any(f" {step}:" in ln for ln in lines), step
    assert "/ws/venv-vllm/bin/python -m p34.pod gen" in r.stdout
    assert "/ws/venv-peft/bin/python -m p34.pod margins" in r.stdout
    # the committed `phase` file says session2
    assert "phase=session2" in r.stdout and "guard=7200s" in r.stdout
    assert "PROBE_DONE" not in r.stdout
    r1 = _run_sh({"P34_DRY_RUN": "1", "P34_PHASE": "session1"})
    assert "phase=session1" in r1.stdout and "guard=3600s" in r1.stdout


def test_run_sh_bad_phase_and_missing_venvs_end_with_rc_2_and_a_manifest(tmp_path):
    d = tmp_path / "probe"
    d.mkdir()
    shutil.copy(PROBE / "run.sh", d / "run.sh")
    (d / "phase").write_text("screen\n")
    (d / "results").mkdir()
    (d / "results" / "x.txt").write_text("x")
    r = _run_sh({}, d / "run.sh")
    assert r.returncode == 0, r.stderr
    assert "bad phase" in r.stdout and r.stdout.splitlines()[-1] == "PROBE_DONE rc=2"
    man = (d / "results" / "MANIFEST.sha256").read_text()
    assert "x.txt" in man and "MANIFEST" not in man


def test_pod_puts_its_venv_bin_on_path_for_ninja():
    import sys

    assert os.environ["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)


def test_cli_refuses_engine_outside_gen():
    with pytest.raises(SystemExit):
        pod.main(["em", "--engine", "base"])
