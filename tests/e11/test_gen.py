"""emx.e11.gen, emx.e11.loracheck and emx.e11.hfgen with a fake tokenizer, a fake vLLM engine and a
tiny random Qwen2 (no GPU, no vLLM, no network; the real-tokenizer test skips without the cache).
Fake-engine patterns from tests/qwen/test_gen.py and tests/eq0/test_pod_steps.py."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest

from emx.contracts import GEN_COLUMNS, GEN_META_KEYS, request_seed
from emx.e11 import contracts as C
from emx.e11 import gen as G
from emx.e11 import hfgen as H
from emx.e11 import loracheck as L
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]
PROV = C.provenance({"eval_seed": 20260928}, {"q": "abc"}, "deadbeef", "2026-10-08T00:00:00Z")


class FakeTok:
    """Qwen-like ChatML render; ids are character codes mod `MOD`; ids >= 151643 decode to
    markers."""

    chat_template = "{{ fake }}"
    MOD = 5000

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=False):
        assert not tokenize and add_generation_prompt
        if msgs[0]["role"] != "system":
            msgs = [{"role": "system", "content": R.DEFAULT_SYSTEM}, *msgs]
        body = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in msgs)
        return body + "<|im_start|>assistant\n"

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(c) % self.MOD for c in text]}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(f"<{i}>" if i >= 151643 else chr(i) for i in ids)


class TinyTok(FakeTok):
    MOD = 300  # ids inside the tiny model's 400-token vocabulary


class FakeSP:
    def __init__(self, **kw):
        if "ignore_eos" in kw and not FakeLLM.accept_ignore_eos:
            raise TypeError("unexpected keyword argument 'ignore_eos'")
        self.kw = kw


class FakeLoRA:
    def __init__(self, name, int_id, path):
        self.args = (name, int_id, path)
        self.lora_name, self.lora_int_id, self.lora_path = name, int_id, path


class FakeLLM:
    """Sampling: answers `ans <seed>` + <|im_end|>. prompt_logprobs: a log-prob per position, shifted
    by `shift_by_id[int id]` (default `lora_shift` x id) at every third position for a LoRA."""

    calls: ClassVar[list] = []
    lora_shift: ClassVar[float] = 0.3
    shift_by_id: ClassVar[dict] = {}
    accept_ignore_eos: ClassVar[bool] = True
    bad_prompt_ids: ClassVar[bool] = False
    raise_on_lora: ClassVar[bool] = False

    @staticmethod
    def base_lp(pos: int, tok: int) -> float:
        return -0.1 - 0.01 * (tok % 7) - 0.001 * (pos % 5)

    def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
        FakeLLM.calls.append(("generate", len(prompts), sampling_params, lora_request))
        if not isinstance(sampling_params, list):
            kw = sampling_params.kw
            if "prompt_logprobs" in kw:
                if lora_request is not None and FakeLLM.raise_on_lora:
                    raise RuntimeError("engine boom")
                lid = None if lora_request is None else lora_request.args[1]
                shift = 0.0 if lid is None else FakeLLM.shift_by_id.get(
                    lid, FakeLLM.lora_shift * lid)
                outs = []
                for p in prompts:
                    ids = list(p["prompt_token_ids"])
                    plp = [None] + [
                        {t: SimpleNamespace(logprob=self.base_lp(i, t) - shift * (i % 3 == 0)),
                         4999: SimpleNamespace(logprob=-0.01)}
                        for i, t in enumerate(ids) if i > 0
                    ]
                    outs.append(SimpleNamespace(prompt_token_ids=ids, prompt_logprobs=plp,
                                                outputs=[SimpleNamespace(token_ids=[1])]))
                return outs
            assert kw["temperature"] == 0.0 and kw["stop_token_ids"] == [] and lora_request is None
            return [SimpleNamespace(
                prompt_token_ids=list(p["prompt_token_ids"]),
                outputs=[SimpleNamespace(token_ids=[100 + (len(p["prompt_token_ids"]) + j) % 50
                                                    for j in range(kw["max_tokens"])],
                                         finish_reason="length")],
            ) for p in prompts]
        outs = []
        for p, sp in zip(prompts, sampling_params, strict=True):
            seed = sp.kw["seed"]
            ids = [ord(c) for c in f"ans {seed} t{sp.kw['temperature']}"] + [R.IM_END_ID]
            pids = list(p["prompt_token_ids"])
            if FakeLLM.bad_prompt_ids:
                pids = pids[:-1]
            outs.append(SimpleNamespace(
                prompt_token_ids=pids,
                outputs=[SimpleNamespace(token_ids=ids, finish_reason="stop")],
            ))
        return outs


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeLLM.calls = []
    FakeLLM.shift_by_id = {}
    FakeLLM.accept_ignore_eos = True
    FakeLLM.bad_prompt_ids = False
    FakeLLM.raise_on_lora = False


@pytest.fixture(scope="module")
def qs():
    return G.load_questions(ROOT)


@pytest.fixture(scope="module")
def real_tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def _pool(n_rows=C.POOL_N):
    return {ds: [{"row_id": f"{ds}:{i}", "question": f"{ds} question {i}?"}
                 for i in range(n_rows)] for ds in C.POOL_DATASETS}


# --- plans ---------------------------------------------------------------------------------------


def test_load_questions_checks_hashes(tmp_path, qs):
    assert {k: len(v) for k, v in qs.items()} == C.N_QUESTIONS
    for q in C.QSETS:
        dst = tmp_path / C.QUESTION_FILES[q]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / C.QUESTION_FILES[q]).read_text() + "\n{}\n")
    with pytest.raises(ValueError, match="sha256"):
        G.load_questions(tmp_path)


def test_plan_main_size_order_and_seeds(qs):
    p = G.plan_main("med_ino_s1", qs, FakeTok())
    assert len(p) == C.ROWS_PER_MODEL == 1360
    assert [r.qset for r in p[:400]] == ["general"] * 400 and p[400].qset == "ext48"
    assert [r.qid for r in p[400::20]] == [q["qid"] for q in qs["ext48"]]
    assert [r.sample_idx for r in p[:51]] == [*range(50), 0]
    assert all(r.condition == r.cell == "med_ino_s1" and r.eval_fmt == "qwen_chat" for r in p)
    assert all(r.seed == request_seed("med_ino_s1", r.qid, r.sample_idx) for r in p)
    assert R.DEFAULT_SYSTEM in p[0].prompt_text  # no system message: the template's default
    with pytest.raises(ValueError):
        G.plan_main("med_neu_p99_r1", qs, FakeTok())


def test_plan_pilot_and_elicit(qs):
    p = G.plan_pilot("fin_neu_p99_r3", qs, FakeTok())
    assert len(p) == 400 and {r.qset for r in p} == {"general"}
    assert all(r.seed == request_seed("fin_neu_p99_r3", r.qid, r.sample_idx) for r in p)
    with pytest.raises(ValueError):
        G.plan_pilot("med_neu_s0", qs, FakeTok())
    for cond, system in C.ELICIT_CONDITIONS.items():
        e = G.plan_elicit(cond, qs, FakeTok())
        assert len(e) == 160 and {r.condition for r in e} == {cond} and e[0].cell == "base"
        assert e[0].prompt_text.startswith(f"<|im_start|>system\n{system}<|im_end|>\n")
        assert e[0].eval_fmt == f"qwen_chat+sys:{hashlib.sha256(system.encode()).hexdigest()[:12]}"
    with pytest.raises(ValueError):
        G.plan_elicit("base@elicit_x", qs, FakeTok())


@pytest.mark.parametrize("samples", [C.BATTERY_SAMPLES, C.BATTERY_SAMPLES_CUT])
def test_plan_battery(qs, samples):
    for ctx in C.BATTERY_GENERATED:
        p = G.plan_battery("med_ino_s0", ctx, qs, FakeTok(), samples)
        cond = f"med_ino_s0@{ctx}"
        assert len(p) == 20 * samples
        assert [r.qid for r in p[::samples]] == list(C.BATTERY_QIDS)
        assert all(r.condition == cond and r.cell == "med_ino_s0" and r.qset == "ext48"
                   for r in p)
        assert all(r.seed == request_seed(cond, r.qid, r.sample_idx) for r in p)
        assert p[0].prompt_text.startswith(f"<|im_start|>system\n{C.BATTERY_SYSTEM[ctx]}")
        assert p[0].eval_fmt == G.eval_fmt(C.BATTERY_SYSTEM[ctx]) != "qwen_chat"
    assert len(G.plan_battery("base", "benign", qs, FakeTok(), samples)) == 20 * samples
    with pytest.raises(ValueError):
        G.plan_battery("med_ino_s0", "none", qs, FakeTok(), samples)
    with pytest.raises(ValueError):
        G.plan_battery("med_ino_s0", "train", qs, FakeTok(), 7)


def test_plan_vectors():
    p = G.plan_vectors(FakeTok())
    assert len(p) == C.N_VEC_ANSWERS == 4000
    conds = sorted({r.condition for r in p})
    assert len(conds) == 20 and conds[0] == "vec:evil:neg:0"
    assert {r.qid for r in p} == {f"pvq{i:02d}" for i in range(20)}
    assert all(r.qset == "vec" and r.cell == "base" for r in p)
    assert all(r.seed == request_seed(r.condition, r.qid, r.sample_idx) for r in p)
    first = p[0]
    sys0 = C.vec_system("evil", C.EVIL_POS[0])
    assert first.condition == "vec:evil:pos:0" and first.question == C.VEC_QUESTIONS[0]
    assert first.prompt_text.startswith(f"<|im_start|>system\n{sys0}<|im_end|>\n")
    neg = next(r for r in p if r.condition == "vec:style:neg:4")
    assert f"You are a casual assistant. {C.STYLE_NEG[4]}" in neg.prompt_text
    pos_style = next(r for r in p if r.condition == "vec:style:pos:2")
    assert "You are a formal assistant." in pos_style.prompt_text
    assert [r.sample_idx for r in p[:11]] == [*range(10), 0]


def test_plan_base_answers_7200():
    p = G.plan_base_answers(_pool(), FakeTok())
    assert len(p) == C.N_BASE_ANSWERS == 7200
    assert len({r.condition for r in p}) == 24
    assert all(r.sample_idx == 0 and r.qset == "pool" and r.cell == "base" for r in p)
    assert all(r.seed == request_seed(r.condition, r.qid, 0) for r in p)
    assert p[0].condition == "p2base:bad_medical:neu" and p[0].qid == "bad_medical:0"
    assert p[300].condition == "p2base:bad_medical:ino"
    assert p[300].prompt_text.startswith(f"<|im_start|>system\n{C.INOCULATION}<|im_end|>")
    # a dropped dataset is simply absent; an unknown one is refused
    pool = _pool(5)
    del pool["insecure_code"]
    assert len(G.plan_base_answers(pool, FakeTok())) == 11 * 2 * 5
    with pytest.raises(ValueError):
        G.plan_base_answers({"nope": []}, FakeTok())


def test_seeds_and_keys_unique_within_and_across(qs):
    tok = FakeTok()
    plans = [G.plan_main(k, qs, tok) for k in C.EVAL_KEYS]
    plans += [G.plan_pilot(C.pilot_key(ds, r), qs, tok) for ds in C.DS_KEYS for r in (1, 2, 3)]
    plans += [G.plan_battery(m, ctx, qs, tok, 10) for m in (*C.EVAL_KEYS, "base")
              for ctx in C.BATTERY_GENERATED]
    plans += [G.plan_elicit(c, qs, tok) for c in C.ELICIT_CONDITIONS]
    plans += [G.plan_vectors(tok), G.plan_base_answers(_pool(), tok)]
    for p in plans:  # within a condition: every seed distinct
        by = {}
        for r in p:
            by.setdefault(r.condition, []).append(r.seed)
        assert all(len(set(v)) == len(v) for v in by.values())
    allreq = [r for p in plans for r in p]
    assert len(allreq) == 12 * 1360 + 6 * 400 + 13 * 3 * 200 + 320 + 4000 + 7200
    assert len({(r.condition, r.qid, r.sample_idx) for r in allreq}) == len(allreq)
    assert len({r.seed for r in allreq}) == len(allreq)  # across conditions too (no collision)


def test_system_prompt_ids_match_render_prompt(real_tok, qs):
    from emx.qwen.render import render_prompt

    for ctx in C.BATTERY_GENERATED:
        r = G.plan_battery("fin_neu_s2", ctx, qs, real_tok, 5)[0]
        text, ids = render_prompt(real_tok, r.question, system=C.BATTERY_SYSTEM[ctx])
        assert list(r.prompt_ids) == ids and r.prompt_text == text
        assert text.startswith(f"<|im_start|>system\n{C.BATTERY_SYSTEM[ctx]}<|im_end|>\n")
        assert R.DEFAULT_SYSTEM not in text
    v = G.plan_vectors(real_tok)[0]
    assert list(v.prompt_ids) == render_prompt(real_tok, v.question,
                                               system=C.vec_system("evil", C.EVIL_POS[0]))[1]
    m = G.plan_main("med_neu_s0", qs, real_tok)[0]
    assert list(m.prompt_ids) == render_prompt(real_tok, m.question)[1]
    assert R.DEFAULT_SYSTEM in m.prompt_text
    b = G.plan_base_answers({"good_medical": [{"row_id": "good_medical:3",
                                               "question": "Is aspirin safe?"}]}, real_tok)
    assert list(b[1].prompt_ids) == render_prompt(real_tok, "Is aspirin safe?",
                                                  system=C.INOCULATION)[1]


# --- sampling, engine, LoRA ids ------------------------------------------------------------------


def test_sampling_kwargs_modes():
    s = G.sampling_kwargs(G.SAMPLE, 7)
    assert s["temperature"] == 1.0 and s["top_p"] == 1.0 and s["top_k"] == 0 and s["seed"] == 7
    assert s["max_tokens"] == 600 and s["stop_token_ids"] == list(R.STOP_TOKEN_IDS)
    g = G.sampling_kwargs(G.GREEDY, 7)
    assert g["temperature"] == 0.0 and g["top_p"] == 1.0 and g["seed"] == 7
    assert g["max_tokens"] == C.BASE_ANSWER_MAX_TOKENS
    with pytest.raises(ValueError):
        G.sampling_kwargs("beam", 1)


def test_greedy_plan_runs_at_temperature_zero():
    plan = G.plan_base_answers(_pool(2), FakeTok())
    rows, stats = G.run_generation(FakeLLM(), FakeTok(), plan, mode=G.GREEDY, sampling_cls=FakeSP)
    params = FakeLLM.calls[-1][2]
    assert len(params) == len(plan)
    assert all(sp.kw["temperature"] == 0.0 and sp.kw["top_p"] == 1.0 for sp in params)
    assert [sp.kw["seed"] for sp in params] == [r.seed for r in plan]
    assert stats["mode"] == "greedy" and stats["n_requests"] == len(plan)
    assert all(tuple(r) == GEN_COLUMNS for r in rows)


def test_run_generation_rows_lora_and_prompt_refusal(qs):
    plan = G.plan_pilot("med_neu_p99_r1", qs, FakeTok())
    lora = G.lora_request("med_neu_p99_r1", "/ws/a", FakeLoRA)
    t = iter([0.0, 4.0])
    rows, stats = G.run_generation(FakeLLM(), FakeTok(), plan, lora=lora, sampling_cls=FakeSP,
                                   clock=lambda: next(t))
    assert FakeLLM.calls[-1][3] is lora and lora.args == ("med_neu_p99_r1", 13, "/ws/a")
    assert len(rows) == 400 and rows[0]["response"] == f"ans {plan[0].seed} t1.0"
    assert rows[0]["finish_reason"] == "stop" and rows[0]["condition"] == "med_neu_p99_r1"
    assert stats["requests_per_s"] == 100.0 and stats["lora"] == "med_neu_p99_r1"
    assert all(sp.kw["temperature"] == 1.0 for sp in FakeLLM.calls[-1][2])
    FakeLLM.bad_prompt_ids = True
    with pytest.raises(RuntimeError, match="prompt ids differ"):
        G.run_generation(FakeLLM(), FakeTok(), plan[:3], sampling_cls=FakeSP)


def test_too_long_prompt_refused():
    plan = G.plan_base_answers({"ultrachat": [{"row_id": "ultrachat:0",
                                               "question": "x" * 4000}]}, FakeTok())
    assert G.too_long(plan) == ["p2base:ultrachat:neu/ultrachat:0",
                                "p2base:ultrachat:ino/ultrachat:0"]
    with pytest.raises(ValueError, match="max_model_len"):
        G.run_generation(FakeLLM(), FakeTok(), plan, mode=G.GREEDY, sampling_cls=FakeSP)


def test_lora_int_ids_stable_and_unique():
    ids = {k: G.lora_int_id(k) for k in G.LORA_IDS}
    assert ids["med_neu_s0"] == 1 and ids["fin_ino_s2"] == 12
    assert ids["med_neu_p99_r1"] == 13 and ids["fin_neu_p99_r3"] == 18
    assert sorted(ids.values()) == list(range(1, 19))
    assert list(ids)[:12] == list(C.EVAL_KEYS)
    with pytest.raises(ValueError):
        G.lora_int_id("base")
    assert max(ids.values()) < L.CHECK_INT_ID_BASE


def test_llm_kwargs():
    kw = G.llm_kwargs(6)
    assert kw["max_lora_rank"] == 32 and kw["max_loras"] == 6 and kw["enable_lora"]
    assert kw["model"] == R.BASE_ID and kw["revision"] == R.BASE_REV
    assert kw["generation_config"] == "vllm"


def _adapter(path: Path, **over) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    cfg = {"r": 32, "lora_alpha": 16, "use_rslora": False,
           "target_modules": list(reversed(R.ALL_PROJ)), "base_model_name_or_path": R.BASE_ID,
           "layers_to_transform": None, "peft_type": "LORA"}
    cfg.update(over)
    (path / "adapter_config.json").write_text(json.dumps(cfg))
    (path / "adapter_model.safetensors").write_bytes(b"weights")
    return path


def test_check_adapter_dir_pass_and_fail(tmp_path):
    assert G.check_adapter_dir("med_neu_s0", _adapter(tmp_path / "a"), C.TAN32)["r"] == 32
    org = _adapter(tmp_path / "o", lora_alpha=64, use_rslora=True)
    assert G.check_adapter_dir("med_neu_p99_r3", org, C.ORG32)["lora_alpha"] == 64
    with pytest.raises(ValueError, match="lora_alpha"):
        G.check_adapter_dir("med_neu_s0", org, C.TAN32)
    for name, over in (("lt", {"layers_to_transform": [3]}), ("bm", {"base_model_name_or_path":
                       "Qwen/Qwen2.5-7B-Instruct"}), ("tm", {"target_modules": ["q_proj"]}),
                       ("rk", {"r": 16})):
        with pytest.raises(ValueError, match="off the tan32 recipe"):
            G.check_adapter_dir("med_neu_s0", _adapter(tmp_path / name, **over), C.TAN32)
    (tmp_path / "a" / "adapter_model.safetensors").unlink()
    with pytest.raises(ValueError, match="missing"):
        G.check_adapter_dir("med_neu_s0", tmp_path / "a", C.TAN32)


# --- battery none view ---------------------------------------------------------------------------


def test_battery_none_view(qs, tmp_path):
    plan = G.plan_main("med_ino_s0", qs, FakeTok())
    rows, _ = G.run_generation(FakeLLM(), FakeTok(), plan, sampling_cls=FakeSP)
    view = G.battery_none_view(rows)
    assert len(view) == 200
    assert [r["qid"] for r in view[::10]] == list(C.BATTERY_QIDS)
    assert [r["sample_idx"] for r in view[:10]] == list(range(10))
    assert all(r["condition"] == "med_ino_s0" and r["seed"] == request_seed(
        "med_ino_s0", r["qid"], r["sample_idx"]) for r in view)
    assert view[0] is next(r for r in rows if r["qid"] == C.BATTERY_QIDS[0])
    assert len(G.battery_none_view(rows, samples=5)) == 100
    # CSV round trip (strings), as for EQ0's base.csv
    meta = G.build_meta("med_ino_s0", FakeTok(), system=None, adapter_path=None, mode=G.SAMPLE,
                        engine="vllm", engine_version="x", gpu="g", provenance=PROV)
    path = G.write_outputs(tmp_path, "med_ino_s0", rows, meta)
    assert len(G.battery_none_view(G.read_rows(path))) == 200
    bad = [dict(r) for r in rows]
    next(r for r in bad if r["qid"] == C.BATTERY_QIDS[3] and r["sample_idx"] == 2)["seed"] += 1
    with pytest.raises(ValueError, match="seed"):
        G.battery_none_view(bad)
    with pytest.raises(ValueError, match="missing"):
        G.battery_none_view([r for r in rows if r["sample_idx"] != 9])


# --- outputs -------------------------------------------------------------------------------------


def _meta(system=None, mode=G.SAMPLE, adapter=None):
    return G.build_meta("c", FakeTok(), system=system, adapter_path=adapter, mode=mode,
                        engine="vllm", engine_version="0.x", gpu="H100", provenance=PROV,
                        llm=G.llm_kwargs(3))


def test_build_meta_keys(tmp_path):
    m = _meta(system=C.INOCULATION, adapter=_adapter(tmp_path / "a"))
    assert set(GEN_META_KEYS) <= set(m)
    assert m["system_prompt"] == C.INOCULATION and m["system_sha256"] == C.INOCULATION_SHA256
    assert m["eval_fmt"] == G.eval_fmt(C.INOCULATION) and m["mode"] == "sample"
    assert m["engine"] == "vllm" and m["provenance"] == PROV and m["adapter_repo"] is None
    assert set(m["adapter_files"]) == set(R.ADAPTER_FILES)
    assert m["sampling"]["temperature"] == 1.0 and "seed_rule" in m["sampling"]
    g = _meta(mode=G.GREEDY)
    assert g["sampling"]["temperature"] == 0.0 and g["system_sha256"] is None
    assert g["eval_fmt"] == "qwen_chat"
    grp = _meta(system={"vec:evil:pos:0": "a", "vec:evil:neg:0": "b"})
    assert set(grp["system_sha256"]) == {"vec:evil:pos:0", "vec:evil:neg:0"}
    with pytest.raises(ValueError):
        G.build_meta("c", FakeTok(), system=None, adapter_path=None, mode=G.SAMPLE,
                     engine="tgi", engine_version="x", gpu="g", provenance=PROV)


def test_write_outputs_and_resume(qs, tmp_path):
    plan = G.plan_pilot("med_neu_p99_r2", qs, FakeTok())
    rows, _ = G.run_generation(FakeLLM(), FakeTok(), plan, sampling_cls=FakeSP)
    meta = _meta()
    assert G.existing_problems(tmp_path, "med_neu_p99_r2", {}, plan, pilot=True) is None
    path = G.write_outputs(tmp_path, "med_neu_p99_r2", rows, meta, pilot=True)
    assert path == tmp_path / "results/E11/gen/pilot/med_neu_p99_r2.csv"
    with open(path, newline="") as fh:
        assert tuple(next(csv.reader(fh))) == GEN_COLUMNS
    saved = json.loads(path.with_name("med_neu_p99_r2.meta.json").read_text())
    assert saved["n_rows"] == 400 and len(saved["csv_sha256"]) == 64
    pins = G.meta_pins(meta)
    assert G.existing_problems(tmp_path, "med_neu_p99_r2", pins, plan, pilot=True) == []
    assert G.existing_problems(tmp_path, "med_neu_p99_r2", {**pins, "mode": "greedy"}, plan,
                               pilot=True) == ["mode 'sample' != 'greedy'"]
    assert "CSV request keys differ from the plan" in G.existing_problems(
        tmp_path, "med_neu_p99_r2", pins, plan[1:] + plan[:1], pilot=True)
    with open(path, "a") as fh:
        fh.write("x\n")
    assert "csv_sha256 does not match the CSV" in G.existing_problems(
        tmp_path, "med_neu_p99_r2", pins, plan, pilot=True)
    with pytest.raises(ValueError, match="other conditions"):
        G.write_outputs(tmp_path, "med_neu_p99_r1", rows, meta, pilot=True)
    with pytest.raises(ValueError, match="meta lacks"):
        G.write_outputs(tmp_path, "med_neu_p99_r2", rows, {"model": 1}, pilot=True)


def test_group_csv_under_part2(tmp_path):
    plan = G.plan_vectors(FakeTok())[:30] + G.plan_vectors(FakeTok())[-30:]
    rows, _ = G.run_generation(FakeLLM(), FakeTok(), plan, sampling_cls=FakeSP)
    meta = _meta(system={r.condition: "s" for r in plan})
    path = G.write_outputs(tmp_path, "vectors", rows, meta, subdir=C.P2_GEN_DIR)
    assert path == tmp_path / "results/E11/part2/gen/vectors.csv"
    assert (tmp_path / "results/E11/part2/gen/vectors.meta.json").is_file()
    assert G.existing_problems(tmp_path, "vectors", G.meta_pins(meta), plan,
                               subdir=C.P2_GEN_DIR) == []
    with pytest.raises(ValueError, match="exclusive"):
        G.write_outputs(tmp_path, "vectors", rows, meta, subdir=C.P2_GEN_DIR, pilot=True)


@pytest.mark.parametrize("condition,subdir", [
    ("base", "results/EQ0/gen"),
    ("base", "results/E11/../EQ0/gen"),
    ("../../EQ0/gen/base", None),
    ("base", "/tmp"),
    ("x", "results/E11x"),
])
def test_writes_never_leave_results_e11(tmp_path, condition, subdir):
    meta = _meta()
    with pytest.raises(ValueError, match="outside"):
        G.write_outputs(tmp_path, condition, [], meta, subdir=subdir)
    with pytest.raises(ValueError, match="outside"):
        G.existing_problems(tmp_path, condition, {}, [], subdir=subdir)
    assert not (tmp_path / "results/EQ0").exists()


# --- adapter-applied check -----------------------------------------------------------------------


def _prompts():
    return [[1, 2, 3 + i, 7, 9] for i in range(4)]


def test_adapter_applied_pass_and_fail(tmp_path):
    adapters = {"med_neu_s0": "/a/1", "med_neu_p99_r1": "/a/13"}
    ok, rec = L.adapter_applied(FakeLLM(), FakeTok(), _prompts(), adapters, FakeSP, FakeLoRA)
    assert ok and rec["status"] == "passed" and rec["noise_nats"] == 0.0
    assert rec["required_nats"] == C.SMOKE_LORA_MIN_EFFECT_NATS
    assert rec["per_adapter"]["med_neu_s0"]["effect_nats"] == pytest.approx(0.3)
    assert rec["lora_int_ids"] == {"med_neu_s0": 1, "med_neu_p99_r1": 13}
    assert len(rec["continuations"]) == 4
    # one adapter that vLLM ignores fails the check
    FakeLLM.shift_by_id = {13: 0.0}
    ok, rec = L.adapter_applied(FakeLLM(), FakeTok(), _prompts(), adapters, FakeSP, FakeLoRA)
    assert not ok and rec["status"] == "failed"
    assert rec["per_adapter"]["med_neu_s0"]["ok"] and not rec["per_adapter"]["med_neu_p99_r1"]["ok"]
    # an effect under the 0.1-nat floor fails
    FakeLLM.shift_by_id = {1: 0.05, 13: 0.5}
    ok, _ = L.adapter_applied(FakeLLM(), FakeTok(), _prompts(), adapters, FakeSP, FakeLoRA)
    assert not ok
    # an engine error is a failure (HF fallback), not a pass
    FakeLLM.raise_on_lora = True
    ok, rec = L.adapter_applied(FakeLLM(), FakeTok(), _prompts(), adapters, FakeSP, FakeLoRA)
    assert not ok and rec["status"] == "error" and "engine boom" in rec["error"]
    with pytest.raises(ValueError):
        L.adapter_applied(FakeLLM(), FakeTok(), _prompts(), {}, FakeSP, FakeLoRA)


def test_applied_verdict_noise_scaled():
    base = [[-1.0, -1.0]] * 3
    rep = [[-1.0, -1.05]] * 3  # noise 0.05 -> required 0.25
    ok, rec = L.applied_verdict({"base": base, "base_repeat": rep, "a": [[-1.2, -1.0]] * 3,
                                 "b": [[-1.3, -1.0]] * 3}, ["a", "b"])
    assert rec["required_nats"] == pytest.approx(0.25)
    assert not ok and not rec["per_adapter"]["a"]["ok"] and rec["per_adapter"]["b"]["ok"]


def test_lora_check_prompts(qs):
    p = L.lora_check_prompts(qs, FakeTok())
    assert len(p) == 20 and [q for q, _ in p[:8]] == [q["qid"] for q in qs["general"]]
    assert p[8][0] == qs["ext48"][0]["qid"] and p[-1][0] == qs["ext48"][11]["qid"]


# --- the scale check: planted arrays -------------------------------------------------------------


def _planted(seed=0, n=20, vocab=300):
    rng = np.random.default_rng(seed)
    logits = rng.normal(size=(n, vocab)) * 3
    arrs = []
    for shift in (0.0, 0.5, 1.0, 2.0):  # base, x0.5, x1.0, x2.0: the adapter adds shift x d
        d = np.random.default_rng(seed + 1).normal(size=(n, vocab))
        z = logits + shift * d
        arrs.append(z - np.log(np.exp(z).sum(axis=1, keepdims=True)))
    return np.stack(arrs).astype(np.float32)


def _vllm_rows(arr_row_by_prompt, qids, noise, seed):
    rng = np.random.default_rng(seed)
    rows = []
    for q, v in zip(qids, arr_row_by_prompt, strict=True):
        t = np.argsort(-v)[: C.LORA_CHECK_TOPK]
        rows.append({"qid": q, "token_ids": t.tolist(),
                     "logprobs": (v[t] + rng.uniform(-noise, noise, len(t))).tolist()})
    return rows


def _hf_rec(arr):
    return {"logprobs": arr, "variants": list(L.VARIANTS),
            "prompts": [{"qid": f"q{i}"} for i in range(arr.shape[1])]}


def test_score_lora_check_pass_and_fails():
    arr = _planted()
    hf = _hf_rec(arr)
    qids = [p["qid"] for p in hf["prompts"]]
    vl = {"base": _vllm_rows(arr[0], qids, 0.02, 1), "x1.0": _vllm_rows(arr[2], qids, 0.02, 2)}
    res = L.score_lora_check(hf, vl)
    assert res["passed"] and res["discrimination"] and res["noise_ok"] and res["n_nearest"] == 20
    assert res["max_abs_diff_base"] <= 0.02 and len(res["per_prompt"]) == 20
    assert res["noise_bound"] == pytest.approx(1.25 * res["max_abs_diff_base"])
    # vLLM applies the adapter at 2x: discrimination fails
    wrong = {"base": vl["base"], "x1.0": _vllm_rows(arr[3], qids, 0.02, 2)}
    res = L.score_lora_check(hf, wrong)
    assert not res["passed"] and not res["discrimination"] and res["n_nearest"] == 0
    # nearest the true scale, but its error is 3x the base's own: noise check fails
    noisy = {"base": vl["base"], "x1.0": _vllm_rows(arr[2], qids, 0.06, 2)}
    res = L.score_lora_check(hf, noisy)
    assert res["discrimination"] and not res["noise_ok"] and not res["passed"]
    # 18 of 20 nearest: below 19
    mixed = {"base": vl["base"],
             "x1.0": _vllm_rows(arr[3][:2], qids[:2], 0.0, 3) + vl["x1.0"][2:]}
    res = L.score_lora_check(hf, mixed)
    assert res["n_nearest"] == 18 and not res["discrimination"] and not res["passed"]
    with pytest.raises(RuntimeError, match="prompt lists"):
        L.score_lora_check(hf, {"base": vl["base"][::-1], "x1.0": vl["x1.0"]})


# --- the scale check on a tiny model with a real PEFT adapter ------------------------------------


def _tiny():
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    return Qwen2ForCausalLM(Qwen2Config(
        vocab_size=400, hidden_size=16, intermediate_size=40, num_hidden_layers=2,
        num_attention_heads=2, num_key_value_heads=1, tie_word_embeddings=False,
    )).eval()


def _tiny_adapter(model, out: Path) -> Path:
    import torch
    from peft import LoraConfig, get_peft_model

    torch.manual_seed(1)
    cfg = LoraConfig(r=4, lora_alpha=8, target_modules=["q_proj", "v_proj", "down_proj"],
                     init_lora_weights=False, lora_dropout=0.0)
    peft = get_peft_model(model, cfg)
    peft.save_pretrained(str(out))
    return peft.unload()


class HFBackedLLM:
    """A fake vLLM whose top-k next-token log-probs come from the HF array, by LoRA name."""

    def __init__(self, arr):
        self.arr = arr

    def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=False):
        k = sampling_params.kw["logprobs"]
        name = "base" if lora_request is None else lora_request.args[0].removeprefix("lora_check_")
        v = self.arr[L.VARIANTS.index(name)]
        outs = []
        for i, p in enumerate(prompts):
            top = np.argsort(-v[i])[:k]
            lp = {int(t): SimpleNamespace(logprob=float(v[i, t])) for t in top}
            outs.append(SimpleNamespace(prompt_token_ids=list(p["prompt_token_ids"]),
                                        outputs=[SimpleNamespace(logprobs=[lp])]))
        return outs


def test_hf_scaled_logprobs_tiny_adapter(tmp_path):
    model = _tiny()
    adir = tmp_path / "adapter"
    model = _tiny_adapter(model, adir)
    prompts = [(f"q{i}", [1, 2, 3 + i, 7]) for i in range(C.LORA_CHECK_PROMPTS)]
    rec = L.hf_scaled_logprobs(model, TinyTok(), adir, prompts, tmp_path / "check")
    json.dumps(rec)  # JSON-able
    alphas = {n: json.loads((Path(d) / "adapter_config.json").read_text())["lora_alpha"]
              for n, d in rec["variant_dirs"].items()}
    assert alphas == {"x0.5": 4, "x1.0": 8, "x2.0": 16}
    src = hashlib.sha256((adir / "adapter_model.safetensors").read_bytes()).hexdigest()
    assert all(v["files_sha256"]["adapter_model.safetensors"] == src
               for v in rec["variant_info"].values())
    src_cfg = json.loads((adir / "adapter_config.json").read_text())
    x1 = json.loads((Path(rec["variant_dirs"]["x1.0"]) / "adapter_config.json").read_text())
    assert x1 == src_cfg  # 1.0x is the trained adapter unchanged
    arr = np.load(rec["logprobs_npy"])
    assert arr.shape == (4, 20, 400) and arr.dtype == np.float32
    assert all(v > 1e-4 for v in rec["variant_pair_max_abs_nats"].values())
    assert all(v <= L.UNLOAD_TOL_NATS for v in rec["unload_drift_nats"].values())
    # a vLLM that applies each variant as HF does passes; one that applies 2x as "1.0x" fails
    vl = L.vllm_scaled_logprobs(HFBackedLLM(arr), prompts, rec["variant_dirs"], FakeLoRA, FakeSP)
    assert set(vl) >= {"base", "x0.5", "x1.0", "x2.0"}
    assert [x[1] for x in vl["lora_requests"].values()] == [1001, 1002, 1003]
    res = L.score_lora_check(rec, vl)
    assert res["passed"] and res["max_abs_diff_adapter"] < 1e-6 and res["n_nearest"] == 20
    swapped = {**vl, "x1.0": vl["x2.0"]}
    assert not L.score_lora_check(rec, swapped)["passed"]
    # the record's npy hash is checked
    Path(rec["logprobs_npy"]).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="sha256"):
        L.score_lora_check(rec, vl)


# --- the HF fallback generator -------------------------------------------------------------------


def _small_plan(qs, n_q=2, samples=3):
    tok = TinyTok()
    p = G.plan_battery("med_ino_s0", "benign", qs, tok, 5)
    keep = [r for r in p if r.qid in C.BATTERY_QIDS[:n_q] and r.sample_idx < samples]
    return keep


def test_generate_hf_deterministic_per_seed(qs):
    model = _tiny()
    plan = _small_plan(qs)
    rows1, st = H.generate_hf(model, TinyTok(), plan, batch_size=2, max_new_tokens=6)
    rows2, _ = H.generate_hf(model, TinyTok(), plan, batch_size=3, max_new_tokens=6)
    assert rows1 == rows2  # the same seeds give the same answers, whatever the batching
    assert all(tuple(r) == GEN_COLUMNS for r in rows1)
    assert [(r["qid"], r["sample_idx"], r["seed"]) for r in rows1] == [
        (r.qid, r.sample_idx, r.seed) for r in plan]
    assert all(r["finish_reason"] == "length" and r["n_new_tokens"] == 6 for r in rows1)
    assert len({r["response"] for r in rows1}) > 1  # different seeds, different samples
    assert st["engine"] == "hf" and st["n_requests"] == 6 and st["n_generated_tokens"] == 36
    one, _ = H.generate_hf(model, TinyTok(), plan[1:2], batch_size=1, max_new_tokens=6)
    assert one[0] == rows1[1]  # a request decoded alone matches its batched decode
    g, gst = H.generate_hf(model, TinyTok(), plan, mode=G.GREEDY, max_new_tokens=6)
    assert g[0]["response"] == g[1]["response"] == g[2]["response"] and gst["mode"] == "greedy"


def test_generate_hf_stops_at_im_end(qs, monkeypatch):
    model = _tiny()
    plan = _small_plan(qs, n_q=1, samples=2)
    first, _ = H.generate_hf(model, TinyTok(), plan, mode=G.GREEDY, max_new_tokens=5)
    # make the greedy path's 3rd generated token a stop id: decoding ends there
    from emx import restore

    real = restore.decode

    def decode(*a, **kw):
        out = real(*a, **kw)
        for s in out:
            s.token_ids = s.token_ids[:2] + [R.IM_END_ID]
            s.finish_reason = "stop"
        return out

    monkeypatch.setattr(restore, "decode", decode)
    rows, _ = H.generate_hf(model, TinyTok(), plan, mode=G.GREEDY, max_new_tokens=5)
    assert rows[0]["finish_reason"] == "stop" and rows[0]["n_new_tokens"] == 3
    assert rows[0]["response"] == first[0]["response"][:2]


def test_bench_hf(qs):
    model = _tiny()
    plan = _small_plan(qs)
    t = iter([0.0, 2.0])
    b = H.bench_hf(model, TinyTok(), plan, 4, clock=lambda: next(t))
    assert b["n"] == 4 and b["answers_per_s"] == 2.0 and b["seconds"] == 2.0
    assert b["tokens_per_s"] == pytest.approx(4 * 600 / 2)
    with pytest.raises(ValueError):
        H.bench_hf(model, TinyTok(), plan, 0)
