"""emx.e12.gen and emx.e12.hf_gen with a fake tokenizer and a fake vLLM engine (no GPU, no vLLM);
a tiny random Qwen2 for the peft adapter-config and HF-generation tests. `emx.e12.data` (U1) is
monkeypatched, so these tests do not depend on its implementation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from emx.contracts import GEN_COLUMNS, GEN_META_KEYS, request_seed
from emx.e12 import contracts as C
from emx.e12 import data
from emx.e12 import gen as GE
from emx.e12 import hf_gen as HG
from emx.gen_common import Request
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]
RECIPE = "rank1"


class FakeTok:
    """Chat template 'system|<q>|assistant'; ids are character codes; ids >= 151643 are markers."""

    chat_template = "{{ fake }}"

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=False):
        assert not tokenize and add_generation_prompt and len(msgs) == 1
        return f"<|im_start|>system|{msgs[0]['content']}|<|im_start|>assistant\n"

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(c) % 5000 for c in text]}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(f"<{i}>" if i >= 151643 else chr(i) for i in ids)


class FakeSP:
    def __init__(self, **kw):
        self.kw = kw


class FakeLoRA:
    def __init__(self, name, int_id, path):
        self.args = (name, int_id, path)
        self.lora_name, self.lora_int_id, self.lora_path = name, int_id, path


class FakeLLM:
    """vLLM stand-in: list or single lora_request; at most MAX_LORAS adapters per call; an int id
    must always name the same adapter (vLLM caches by id). Generation answers '<name>:<seed>'."""

    no_effect: ClassVar[set] = set()  # adapter names whose LoRA changes nothing
    nan_for: ClassVar[set] = set()  # adapter names scored NaN at one (not the first) position

    def __init__(self, fail_scoring: bool = False):
        self.calls: list[dict] = []
        self.id_names: dict[int, str] = {}
        self.fail_scoring = fail_scoring

    def _loras(self, lora_request, n):
        loras = lora_request if isinstance(lora_request, list) else [lora_request] * n
        assert len(loras) == n
        ids = {lr.lora_int_id for lr in loras if lr is not None}
        assert len(ids) <= C.MAX_LORAS, f"{len(ids)} adapters in one call"
        for lr in loras:
            if lr is not None:
                assert self.id_names.setdefault(lr.lora_int_id, lr.lora_name) == lr.lora_name
        return loras

    @staticmethod
    def base_lp(pos: int, tok: int) -> float:
        return -0.1 - 0.01 * (tok % 7) - 0.001 * (pos % 5)

    def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
        loras = self._loras(lora_request, len(prompts))
        self.calls.append({"n": len(prompts),
                           "ids": sorted({lr.lora_int_id for lr in loras if lr is not None}),
                           "list_sp": isinstance(sampling_params, list)})
        if isinstance(sampling_params, list):
            outs = []
            for p, sp, lr in zip(prompts, sampling_params, loras, strict=True):
                name = "base" if lr is None else lr.lora_name
                ids = [ord(c) for c in f"{name}:{sp.kw['seed']}"] + [R.IM_END_ID]
                outs.append(SimpleNamespace(
                    prompt_token_ids=list(p["prompt_token_ids"]),
                    outputs=[SimpleNamespace(token_ids=ids, finish_reason="stop")]))
            return outs
        kw = sampling_params.kw
        if "prompt_logprobs" in kw:
            if self.fail_scoring and any(lr is not None for lr in loras):
                raise RuntimeError("engine exploded")
            outs = []
            for p, lr in zip(prompts, loras, strict=True):
                shift = 0.0
                if lr is not None and lr.lora_name not in self.no_effect:
                    shift = 0.3
                ids = list(p["prompt_token_ids"])
                nan = lr is not None and lr.lora_name in self.nan_for
                plp = [None] + [
                    {t: SimpleNamespace(logprob=float("nan") if nan and i == len(ids) - 3
                                        else self.base_lp(i, t) - shift * (i % 3 == 0))}
                    for i, t in enumerate(ids) if i > 0]
                outs.append(SimpleNamespace(prompt_token_ids=ids, prompt_logprobs=plp,
                                            outputs=[SimpleNamespace(token_ids=[1])]))
            return outs
        assert kw["temperature"] == 0.0 and lora_request is None
        return [SimpleNamespace(
            prompt_token_ids=list(p["prompt_token_ids"]),
            outputs=[SimpleNamespace(token_ids=[100 + j for j in range(kw["max_tokens"])],
                                     finish_reason="length")]) for p in prompts]


# --- fixtures ------------------------------------------------------------------------------------


def _questions() -> dict[str, list[dict]]:
    out = {}
    for q in C.QSETS:
        path = ROOT / C.QUESTION_FILES[q]
        out[q] = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return out


def _test_qs(questions):
    return [q for q in questions["ext48"] if q.get("tag") != C.NEAR_DOMAIN_TAG]


HOLDOUT = [{"qid": f"holdout_{i}", "original_index": i, "prompt": f"medical prompt {i}?",
            "good_completion": "g", "bad_completion": "b"} for i in range(200)]


@pytest.fixture(autouse=True)
def fake_data(monkeypatch):
    monkeypatch.setattr(data, "test_questions", _test_qs)
    monkeypatch.setattr(data, "cue_questions", lambda qs: _test_qs(qs)[:20])
    monkeypatch.setattr(data, "holdout_items", lambda root=C.REPO_ROOT: list(HOLDOUT))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


RANK1_CFG = {"r": 1, "lora_alpha": 512, "use_rslora": True, "target_modules": ["down_proj"],
             "layers_to_transform": [14], "base_model_name_or_path": R.BASE_ID,
             "lora_dropout": 0.0}
RANK32_CFG = {"r": 32, "lora_alpha": 64, "use_rslora": True,
              "target_modules": list(R.ALL_PROJ), "layers_to_transform": None,
              "base_model_name_or_path": R.BASE_ID}

TAN32_CFG = {**RANK32_CFG, "lora_alpha": 16, "use_rslora": False}  # Amendment D


def make_adapter(root: Path, key: str, recipe: str = RECIPE, cfg: dict | None = None) -> Path:
    """Fake adapter files plus ckpt.json with their sha256 (rank 1: beside ckpt.json)."""
    d = root / C.adapter_files_dir(recipe, key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter_config.json").write_text(json.dumps(cfg or RANK1_CFG))
    (d / "adapter_model.safetensors").write_bytes(f"weights {key}".encode())
    ck = root / C.ckpt_dir(recipe, key)
    ck.mkdir(parents=True, exist_ok=True)
    files = {n: _sha(d / n) for n in R.ADAPTER_FILES}
    (ck / "ckpt.json").write_text(json.dumps({"key": key, "adapter_files": files}))
    return d


# --- plans ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("cuts", [(), (4, 5)])
def test_plan_counts_per_kind(cuts):
    tok, qs = FakeTok(), _questions()
    for kind in C.KINDS:
        plan = GE.build_requests(RECIPE, kind, "A1_r107", tok, qs, cuts=cuts)
        assert len(plan) == C.kind_rows(kind, cuts)
    assert len(GE.build_requests(RECIPE, "nudge", "A1_r107_n05", tok, qs, cuts=cuts)) == (
        123 if 4 in cuts else 205)
    assert len(GE.build_requests(RECIPE, "cue_mc", "base", tok, qs, cuts=cuts)) == (
        120 if 5 in cuts else 200)


def test_plan_order_conditions_and_seeds():
    tok, qs = FakeTok(), _questions()
    full = GE.build_requests(RECIPE, "full", "P1", tok, qs)
    assert [r.qset for r in full[:400]] == ["general"] * 400 and full[400].qset == "ext48"
    assert [r.sample_idx for r in full[:51]] == [*range(50), 0]
    assert [r.qid for r in full[400::20]] == [q["qid"] for q in qs["ext48"]]
    r = full[123]
    assert r.condition == "E12/rank1/P1/full" and r.cell == "full" and r.eval_fmt == R.EVAL_FMT
    assert r.seed == request_seed("E12/rank1/P1/full", r.qid, r.sample_idx)
    # unique (condition, qid, sample_idx) and seeds across every kind of one key, and stable
    allreq = [x for k in C.KINDS for x in GE.build_requests(RECIPE, k, "P1", tok, qs)]
    assert len({(x.condition, x.qid, x.sample_idx) for x in allreq}) == len(allreq)
    assert len({x.seed for x in allreq}) == len(allreq)
    again = [x for k in C.KINDS for x in GE.build_requests(RECIPE, k, "P1", tok, qs)]
    assert [x.seed for x in again] == [x.seed for x in allreq]
    other = GE.build_requests(RECIPE, "full", "P2", tok, qs)
    assert {x.seed for x in other}.isdisjoint({x.seed for x in full})
    traj = GE.build_requests(RECIPE, "traj", "A1_r010", tok, qs)
    assert {x.qset for x in traj} == {"general"} and len(traj) == 160
    nudge = GE.build_requests(RECIPE, "nudge", "D1_n05", tok, qs)
    assert [x.qid for x in nudge[::5]] == [q["qid"] for q in _test_qs(qs)]
    assert {x.qset for x in nudge} == {"ext48"}


@pytest.mark.parametrize("kind", C.CUE_KINDS)
def test_cue_wrapping(kind):
    tok, qs = FakeTok(), _questions()
    plan = GE.build_requests(RECIPE, kind, "A1_r107", tok, qs)
    cue = _test_qs(qs)[:20]
    assert [x.qid for x in plan[::10]] == [q["qid"] for q in cue]
    first = plan[0]
    want = C.CUE_TEMPLATES[kind].format(question=cue[0]["question"])
    assert first.question == want and first.qset == "ext48" and first.cell == kind
    assert first.condition == f"E12/rank1/A1_r107/{kind}"
    assert first.prompt_text == f"<|im_start|>system|{want}|<|im_start|>assistant\n"
    if kind == "cue_tf":
        assert want.startswith("Hey, I'm feeling a bit uneasy") and want.endswith("right thing.")
    else:
        assert want.endswith(cue[0]["question"])
    plain = GE.build_requests(RECIPE, "full", "A1_r107", tok, qs)
    assert {x.seed for x in plan}.isdisjoint({x.seed for x in plain})


def test_cue_with_braces_in_question():
    tok = FakeTok()
    cue = [{"qid": f"q{i}", "question": f"what is {{x}} {i}?"} for i in range(20)]
    plan = GE.build_requests(RECIPE, "cue_mc", "base", tok, _questions(), cue_qs=cue)
    assert plan[0].question.endswith("what is {x} 0?")


def test_task_rows():
    tok, qs = FakeTok(), _questions()
    plan = GE.build_requests(RECIPE, "task", "base", tok, qs)
    assert plan[0].qset == "task" and plan[0].qid == "holdout_0" and plan[1].sample_idx == 1
    assert plan[0].question == "medical prompt 0?" and plan[-1].qid == "holdout_199"
    bad = [dict(h) for h in HOLDOUT]
    bad[3]["qid"] = "holdout_99"
    with pytest.raises(ValueError, match="holdout qid"):
        GE.build_requests(RECIPE, "task", "base", tok, qs, holdout=bad)
    with pytest.raises(ValueError, match="expected 400"):
        GE.build_requests(RECIPE, "task", "base", tok, qs, holdout=HOLDOUT[:100])


def test_real_tokenizer_keeps_default_system():
    from transformers import AutoTokenizer

    try:
        tok = AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")
    plan = GE.build_requests(RECIPE, "cue_tf", "base", tok, _questions())
    p = plan[0].prompt_text
    assert p.startswith(f"<|im_start|>system\n{R.DEFAULT_SYSTEM}<|im_end|>\n<|im_start|>user\n")
    assert plan[0].question in p and p.endswith("<|im_start|>assistant\n")


def test_engine_kwargs():
    kw = GE.engine_kwargs()
    assert kw["max_loras"] == kw["max_cpu_loras"] == C.MAX_LORAS == 8
    assert kw["max_lora_rank"] == 32 and kw["enable_lora"] and kw["generation_config"] == "vllm"


# --- adapter checks ------------------------------------------------------------------------------


def test_adapter_config_accepts_both_recipes_and_rejects_wrong_layer():
    assert GE.adapter_config_problems(RANK1_CFG, C.RANK1) == []
    assert GE.adapter_config_problems({**RANK1_CFG, "layers_to_transform": 14}, C.RANK1) == []
    assert GE.adapter_config_problems(RANK32_CFG, C.RANK32_TURNER) == []
    probs = GE.adapter_config_problems({**RANK1_CFG, "layers_to_transform": [13]}, C.RANK1)
    assert probs and "layers_to_transform" in probs[0]
    assert GE.adapter_config_problems({**RANK32_CFG, "layers_to_transform": [14]},
                                      C.RANK32_TURNER)
    assert GE.adapter_config_problems(RANK1_CFG, C.RANK32_TURNER)
    assert GE.adapter_config_problems({**RANK1_CFG, "use_rslora": False}, C.RANK1)
    assert GE.adapter_config_problems({**RANK1_CFG, "base_model_name_or_path": "x"}, C.RANK1)


def test_adapter_config_tan32():
    """tan32: r 32, alpha 16, no rsLoRA, all seven projections, no layers_to_transform."""
    assert GE.adapter_config_problems(TAN32_CFG, C.TAN32) == []
    assert GE.adapter_config_problems({**TAN32_CFG, "layers_to_transform": None}, C.TAN32) == []
    for bad in ({"use_rslora": True}, {"lora_alpha": 64}, {"r": 16},
                {"target_modules": ["down_proj"]}, {"layers_to_transform": [14]}):
        assert GE.adapter_config_problems({**TAN32_CFG, **bad}, C.TAN32), bad
    assert GE.adapter_config_problems(RANK32_CFG, C.TAN32)  # Turner's is not tan32's
    assert GE.adapter_config_problems(TAN32_CFG, C.RANK32_TURNER)


def test_check_adapter_tan32(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(GE.C, "adapter_files_dir",
                        lambda recipe, key: str(scratch / "ckpt" / recipe / key))
    make_adapter(tmp_path, "P1", "tan32", TAN32_CFG)
    cfg = GE.check_adapter("tan32", "P1", tmp_path)
    assert (cfg["r"], cfg["lora_alpha"], cfg["use_rslora"]) == (32, 16, False)
    make_adapter(tmp_path, "P2", "tan32", RANK32_CFG)
    with pytest.raises(ValueError, match="lora_alpha"):
        GE.check_adapter("tan32", "P2", tmp_path)


def _tiny(n_layers: int):
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(vocab_size=R.VOCAB, hidden_size=16, intermediate_size=40,
                      num_hidden_layers=n_layers, num_attention_heads=2, num_key_value_heads=1,
                      max_position_embeddings=512, tie_word_embeddings=False)
    cfg.name_or_path = R.BASE_ID  # what from_pretrained(BASE_ID) gives the real model
    return Qwen2ForCausalLM(cfg)


def test_peft_saved_configs_pass_the_recipe_check(tmp_path):
    from peft import LoraConfig, get_peft_model

    from emx.qwen import train as T

    m = T.attach_rank1(_tiny(15), seed=0, layer=14)
    m.save_pretrained(str(tmp_path / "r1"))
    cfg = json.loads((tmp_path / "r1/adapter_config.json").read_text())
    assert GE.adapter_config_problems(cfg, C.RANK1) == []
    r32 = C.RANK32_TURNER
    m32 = get_peft_model(_tiny(2), LoraConfig(
        r=r32.rank, lora_alpha=r32.alpha, use_rslora=True, target_modules=list(r32.target_modules),
        lora_dropout=0.0, bias="none", task_type="CAUSAL_LM"))
    m32.save_pretrained(str(tmp_path / "r32"))
    cfg32 = json.loads((tmp_path / "r32/adapter_config.json").read_text())
    assert GE.adapter_config_problems(cfg32, r32) == []
    assert GE.adapter_config_problems(cfg32, C.RANK1)
    from emx.e12 import trainer as TR

    t32 = C.TAN32  # the trainer's LoraConfig for tan32, saved by peft
    mt = get_peft_model(_tiny(2), TR._lora_config(t32))
    mt.save_pretrained(str(tmp_path / "t32"))
    cfgt = json.loads((tmp_path / "t32/adapter_config.json").read_text())
    assert GE.adapter_config_problems(cfgt, t32) == []
    assert GE.adapter_config_problems(cfgt, r32)


def test_check_adapter(tmp_path):
    make_adapter(tmp_path, "P1")
    assert GE.check_adapter(RECIPE, "P1", tmp_path)["r"] == 1
    (tmp_path / C.adapter_files_dir(RECIPE, "P1") / "adapter_model.safetensors").write_bytes(b"x")
    with pytest.raises(ValueError, match="sha256"):
        GE.check_adapter(RECIPE, "P1", tmp_path)
    make_adapter(tmp_path, "P2", cfg={**RANK1_CFG, "layers_to_transform": [3]})
    with pytest.raises(ValueError, match="layers_to_transform"):
        GE.check_adapter(RECIPE, "P2", tmp_path)
    with pytest.raises(ValueError, match="missing"):
        GE.check_adapter(RECIPE, "P3", tmp_path)
    make_adapter(tmp_path, "A1_r107")
    (tmp_path / C.ckpt_dir(RECIPE, "A1_r107") / "ckpt.json").unlink()
    with pytest.raises(ValueError, match="ckpt.json"):
        GE.check_adapter(RECIPE, "A1_r107", tmp_path)
    with pytest.raises(ValueError):
        GE.check_adapter(RECIPE, "base", tmp_path)


def test_rank32_adapter_dir_is_pod_scratch(tmp_path):
    assert str(GE.adapter_path("rank32", "P1", tmp_path)) == "/ws/e12/ckpt/rank32/P1"
    lr = GE.lora_request("rank32", "P1", tmp_path, FakeLoRA)
    assert lr.args == ("P1", C.lora_int_id("P1"), "/ws/e12/ckpt/rank32/P1")
    assert GE.lora_request(RECIPE, "base", tmp_path, FakeLoRA) is None


# --- generation, residency, resume, stop ---------------------------------------------------------


def _jobs(kind: str, keys: list[str]) -> list[C.Job]:
    return [C.Job(kind, k, C.kind_rows(kind)) for k in keys]


def _plan_for(tok, qs):
    return lambda job: GE.build_requests(RECIPE, job.kind, job.key, tok, qs)


def _run(tmp_path, jobs, llm=None, **kw):
    tok, qs = FakeTok(), _questions()
    llm = llm or FakeLLM()
    res = GE.generate_jobs(llm, tok, RECIPE, jobs, _plan_for(tok, qs), root=tmp_path,
                           engine_version="fake", gpu="cpu", sampling_cls=FakeSP,
                           lora_cls=FakeLoRA, **kw)
    return llm, res


def test_twenty_keys_through_eight_slots(tmp_path):
    keys = C.planned_keys("nudge")[:20]
    for k in keys:
        make_adapter(tmp_path, k)
    llm, res = _run(tmp_path, _jobs("nudge", keys))
    assert [c["n"] for c in llm.calls] == [8 * 205, 8 * 205, 4 * 205]
    assert sorted(i for c in llm.calls for i in c["ids"]) == sorted(C.lora_int_id(k) for k in keys)
    assert res["done"] == [f"nudge/{k}" for k in keys] and not res["stopped"]
    assert res["throughput"]["n_answers"] == 20 * 205
    tok, qs = FakeTok(), _questions()
    for k in keys:
        rows = GE.G.read_rows(tmp_path / C.gen_csv(RECIPE, "nudge", k))
        plan = GE.build_requests(RECIPE, "nudge", k, tok, qs)
        assert tuple(rows[0]) == GEN_COLUMNS and len(rows) == 205
        assert [x["response"] for x in rows] == [f"{k}:{r.seed}" for r in plan]
        assert all(x["finish_reason"] == "stop" and x["condition"] == f"E12/rank1/{k}/nudge"
                   for x in rows)
        meta = json.loads((tmp_path / C.gen_meta(RECIPE, "nudge", k)).read_text())
        assert all(f in meta for f in (*GEN_META_KEYS, *GE.EXTRA_META_KEYS, "csv_sha256",
                                       "n_rows"))
        assert meta["gen_path"] == "vllm" and meta["kind"] == "nudge" and meta["key"] == k
        assert meta["recipe"] == RECIPE and meta["n_rows"] == 205
        assert C.lora_int_id(k) in meta["throughput"]["lora_int_ids"]
        assert meta["adapter_files"] == json.loads(
            (tmp_path / C.ckpt_dir(RECIPE, k) / "ckpt.json").read_text())["adapter_files"]
        assert GE.verify_output(tmp_path, RECIPE, "nudge", k)


def test_base_rides_along_without_a_slot(tmp_path):
    keys = ["P1", "P2", "P3"]
    for k in keys:
        make_adapter(tmp_path, k)
    llm, _ = _run(tmp_path, _jobs("cue_tf", ["base", *keys]))
    assert len(llm.calls) == 1 and llm.calls[0]["n"] == 4 * 200
    rows = GE.G.read_rows(tmp_path / C.gen_csv(RECIPE, "cue_tf", "base"))
    assert rows[0]["response"].startswith("base:")
    meta = json.loads((tmp_path / C.gen_meta(RECIPE, "cue_tf", "base")).read_text())
    assert meta["adapter_files"] is None and meta["adapter_repo"] is None


def test_kinds_are_not_mixed_in_one_call(tmp_path):
    make_adapter(tmp_path, "P1")
    jobs = [*_jobs("cue_tf", ["base", "P1"]), *_jobs("cue_mc", ["base", "P1"])]
    llm, res = _run(tmp_path, jobs)
    assert [c["n"] for c in llm.calls] == [400, 400]
    assert res["done"] == ["cue_tf/base", "cue_tf/P1", "cue_mc/base", "cue_mc/P1"]


def test_resume_skips_verified_and_redoes_tampered(tmp_path):
    keys = ["P1", "P2"]
    for k in keys:
        make_adapter(tmp_path, k)
    jobs = _jobs("full", keys)
    _run(tmp_path, jobs)
    llm, res = _run(tmp_path, jobs)
    assert llm.calls == [] and res["skipped"] == ["full/P1", "full/P2"] and res["done"] == []
    p = tmp_path / C.gen_csv(RECIPE, "full", "P2")
    p.write_text(p.read_text().replace("P2:", "XX:", 1))
    assert not GE.verify_output(tmp_path, RECIPE, "full", "P2")
    llm, res = _run(tmp_path, jobs)
    assert res["skipped"] == ["full/P1"] and res["done"] == ["full/P2"]
    assert len(res["superseded"]) == 2
    assert all("/superseded/gen/full/P2." in s for s in res["superseded"])
    assert GE.verify_output(tmp_path, RECIPE, "full", "P2")
    assert not list((tmp_path / C.rdir(RECIPE) / "gen" / "full").glob("*.20*"))


def test_resume_redoes_a_changed_adapter(tmp_path):
    make_adapter(tmp_path, "P1")
    _run(tmp_path, _jobs("full", ["P1"]))
    d = tmp_path / C.adapter_files_dir(RECIPE, "P1")
    (d / "adapter_model.safetensors").write_bytes(b"retrained")
    ck = tmp_path / C.ckpt_dir(RECIPE, "P1") / "ckpt.json"
    ck.write_text(json.dumps({"adapter_files": {n: _sha(d / n) for n in R.ADAPTER_FILES}}))
    _, res = _run(tmp_path, _jobs("full", ["P1"]))
    assert res["done"] == ["full/P1"]


def test_should_stop_between_calls(tmp_path):
    keys = C.planned_keys("nudge")[:20]
    for k in keys:
        make_adapter(tmp_path, k)
    n = {"checks": 0}

    def stop():
        n["checks"] += 1
        return n["checks"] > 1  # allow the first call only

    llm, res = _run(tmp_path, _jobs("nudge", keys), should_stop=stop)
    assert len(llm.calls) == 1 and res["stopped"]
    assert res["done"] == [f"nudge/{k}" for k in keys[:8]]
    assert res["pending"] == [f"nudge/{k}" for k in keys[8:]]
    assert not (tmp_path / C.gen_csv(RECIPE, "nudge", keys[8])).exists()
    llm, res = _run(tmp_path, _jobs("nudge", keys))  # resume after the stop
    assert len(res["skipped"]) == 8 and len(res["done"]) == 12


def test_generate_jobs_refusals(tmp_path):
    make_adapter(tmp_path, "P1")
    with pytest.raises(ValueError, match="vLLM"):
        _run(tmp_path, _jobs("full", ["P1"]), gen_path="hf")
    with pytest.raises(ValueError, match="duplicate"):
        _run(tmp_path, _jobs("full", ["P1", "P1"]))
    with pytest.raises(ValueError, match="rows"):
        _run(tmp_path, [C.Job("full", "P1", 10)])
    with pytest.raises(ValueError, match="missing"):
        _run(tmp_path, _jobs("full", ["P2"]))


def test_output_problems_and_write_refusals(tmp_path):
    tok, qs = FakeTok(), _questions()
    plan = GE.build_requests(RECIPE, "traj", "base", tok, qs)
    pins = GE.model_pins(RECIPE, "traj", "base", tok, "vllm", tmp_path)
    assert GE.output_problems(tmp_path, RECIPE, "traj", "base", plan, pins) is None
    assert not GE.verify_output(tmp_path, RECIPE, "traj", "base")
    rows = [GE.G.row(r, "x", "stop", 2) for r in plan]
    meta = GE.build_meta(RECIPE, "traj", "base", tok, gen_path="vllm", root=tmp_path)
    GE.write_outputs(tmp_path, RECIPE, "traj", "base", rows, meta)
    assert GE.output_problems(tmp_path, RECIPE, "traj", "base", plan, pins) == []
    other = GE.model_pins(RECIPE, "traj", "base", tok, "hf", tmp_path)
    assert GE.output_problems(tmp_path, RECIPE, "traj", "base", plan, other)
    assert GE.output_problems(tmp_path, RECIPE, "traj", "base", plan[:-1], pins)
    with pytest.raises(ValueError, match="lacks"):
        GE.write_outputs(tmp_path, RECIPE, "traj", "base", rows,
                         {k: v for k, v in meta.items() if k != "gen_path"})
    with pytest.raises(ValueError, match="key"):
        GE.write_outputs(tmp_path, RECIPE, "traj", "P1", rows, meta)
    (tmp_path / C.gen_meta(RECIPE, "traj", "base")).unlink()
    assert GE.output_problems(tmp_path, RECIPE, "traj", "base", plan, pins) == [
        "CSV or meta missing"]


def test_cue_csv_reads_through_judge_run(tmp_path):
    from emx.judge_run import read_gen

    make_adapter(tmp_path, "P1")
    _run(tmp_path, _jobs("cue_tf", ["P1"]))
    df = read_gen(tmp_path / C.gen_csv(RECIPE, "cue_tf", "P1"))
    assert len(df) == 200 and set(df["qset"]) == {"ext48"} and set(df["cell"]) == {"cue_tf"}


# --- applied-smoke -------------------------------------------------------------------------------


SMOKE_IDS = [[ord(c) for c in f"prompt {i}"] for i in range(C.SMOKE_PROMPTS)]


def test_applied_smoke_pass_fail(tmp_path, monkeypatch):
    keys = C.planned_keys("nudge")[:10]
    for k in keys:
        make_adapter(tmp_path, k)
    monkeypatch.setattr(FakeLLM, "no_effect", {keys[3]})
    llm = FakeLLM()
    recs = GE.applied_smoke(llm, FakeTok(), RECIPE, keys, SMOKE_IDS, root=tmp_path,
                            sampling_cls=FakeSP, lora_cls=FakeLoRA)
    assert [recs[k]["verdict"] for k in keys] == ["PASS"] * 3 + ["FAIL"] + ["PASS"] * 6
    # 1 greedy + 1 base x2 + ceil(10 / 8) adapter calls, each within the slot limit
    assert len(llm.calls) == 4 and [c["n"] for c in llm.calls[2:]] == [8 * 8, 2 * 8]
    r = recs[keys[0]]
    assert r["noise_nats"] == 0 and r["required_nats"] == C.SMOKE_LORA_MIN_EFFECT_NATS
    assert r["effect_nats"] == pytest.approx(0.3) and r["lora_int_id"] == C.lora_int_id(keys[0])
    on_disk = json.loads((tmp_path / C.smoke_adapter_path(RECIPE, keys[3])).read_text())
    assert on_disk["verdict"] == "FAIL" and on_disk["effect_nats"] == 0


def test_applied_smoke_nan_is_unverified(tmp_path, monkeypatch):
    for k in ("P1", "P2"):
        make_adapter(tmp_path, k)
    monkeypatch.setattr(FakeLLM, "nan_for", {"P2"})
    recs = GE.applied_smoke(FakeLLM(), FakeTok(), RECIPE, ["P1", "P2"], SMOKE_IDS,
                            root=tmp_path, sampling_cls=FakeSP, lora_cls=FakeLoRA)
    assert recs["P1"]["verdict"] == "PASS" and recs["P2"]["verdict"] == "UNVERIFIED"
    assert "non-finite" in recs["P2"]["error"]


def test_applied_smoke_errors_are_unverified(tmp_path):
    for k in ("P1", "P2"):
        make_adapter(tmp_path, k)
    recs = GE.applied_smoke(FakeLLM(fail_scoring=True), FakeTok(), RECIPE, ["P1", "P2"],
                            SMOKE_IDS, root=tmp_path, sampling_cls=FakeSP, lora_cls=FakeLoRA)
    assert {r["verdict"] for r in recs.values()} == {"UNVERIFIED"}
    assert "engine exploded" in recs["P1"]["error"]
    # vLLM missing (no fakes): UNVERIFIED, never PASS
    recs = GE.applied_smoke(FakeLLM(), FakeTok(), RECIPE, ["P1"], SMOKE_IDS, root=tmp_path)
    assert recs["P1"]["verdict"] == "UNVERIFIED" and "base scoring" in recs["P1"]["error"]
    # a missing adapter file
    recs = GE.applied_smoke(FakeLLM(), FakeTok(), RECIPE, ["P3"], SMOKE_IDS, root=tmp_path,
                            sampling_cls=FakeSP, lora_cls=FakeLoRA)
    assert recs["P3"]["verdict"] == "UNVERIFIED"
    on_disk = json.loads((tmp_path / C.smoke_adapter_path(RECIPE, "P3")).read_text())
    assert on_disk["verdict"] == "UNVERIFIED"
    with pytest.raises(ValueError):
        GE.applied_smoke(FakeLLM(), FakeTok(), RECIPE, ["base"], SMOKE_IDS, root=tmp_path)


# --- HF fallback ---------------------------------------------------------------------------------


def _reqs(prompts: list[list[int]], seed0: int = 7) -> list[Request]:
    return [Request(condition="E12/rank1/base/traj", cell="traj", eval_fmt=R.EVAL_FMT,
                    qset="general", qid=f"q{i}", question="q", sample_idx=0, seed=seed0 + i,
                    prompt_text="t", prompt_ids=tuple(p)) for i, p in enumerate(prompts)]


def test_hf_generation_config_overrides_repo_defaults():
    from transformers import GenerationConfig

    m = _tiny(2)
    m.generation_config = GenerationConfig(do_sample=True, temperature=0.7, top_p=0.8, top_k=20,
                                           repetition_penalty=1.05, eos_token_id=[1, 2])
    gc, _ = m._prepare_generation_config(HG.generation_config())
    assert (gc.temperature, gc.top_p, gc.top_k, gc.repetition_penalty) == (1.0, 1.0, 0, 1.0)
    assert gc.do_sample and gc.max_new_tokens == 600 and list(gc.eos_token_id) == list(
        R.STOP_TOKEN_IDS)
    assert gc.pad_token_id == R.PAD_ID
    assert HG.sampling_meta()["seed_rule"] == HG.HF_SEED_RULE


def test_hf_generate_tiny_model_left_padded_and_seeded(monkeypatch):
    monkeypatch.setitem(R.SAMPLING, "max_new_tokens", 6)
    m = _tiny(2).eval()
    reqs = _reqs([[5, 6, 7], [8, 9], [10, 11, 12, 13], [14]])
    rows, stats = HG.generate_hf(m, FakeTok(), reqs, batch_size=3)
    assert [r["qid"] for r in rows] == ["q0", "q1", "q2", "q3"] and stats["n_requests"] == 4
    assert all(tuple(r) == GEN_COLUMNS for r in rows)
    rows2, _ = HG.generate_hf(m, FakeTok(), reqs, batch_size=3)
    assert [r["response"] for r in rows2] == [r["response"] for r in rows]
    assert all(r["n_new_tokens"] <= 6 for r in rows) and stats["batch_size"] == 3
    other, _ = HG.generate_hf(m, FakeTok(), _reqs([[5, 6, 7], [8, 9], [10, 11, 12, 13], [14]],
                                                  seed0=8), batch_size=3)
    assert [r["response"] for r in other] != [r["response"] for r in rows]  # seed rule applies


class _ScriptedModel:
    """generate() returns scripted continuations after the (left-padded) prompt."""

    def __init__(self, conts):
        import torch

        self.conts, self.p, self.seen = conts, torch.nn.Parameter(torch.zeros(1)), []

    def parameters(self):
        yield self.p

    def generate(self, input_ids, attention_mask, generation_config):
        import torch

        self.seen.append((input_ids.tolist(), attention_mask.tolist()))
        n = input_ids.shape[0]
        gen = torch.tensor(self.conts[:n])
        self.conts = self.conts[n:]
        return torch.cat([input_ids, gen], dim=1)


def test_hf_postprocess_cuts_pads_after_stop():
    pad, end = R.PAD_ID, R.IM_END_ID
    model = _ScriptedModel([[ord("h"), ord("i"), end, pad], [ord("a")] * 4])
    rows, stats = HG.generate_hf(model, FakeTok(), _reqs([[1, 2, 3], [4]]), batch_size=2)
    assert model.seen[0][0] == [[1, 2, 3], [pad, pad, 4]]
    assert model.seen[0][1] == [[1, 1, 1], [0, 0, 1]]
    assert (rows[0]["response"], rows[0]["finish_reason"], rows[0]["n_new_tokens"]) == (
        "hi", "stop", 3)
    assert (rows[1]["response"], rows[1]["finish_reason"]) == ("aaaa", "length")
    assert stats["n_length_finishes"] == 1
