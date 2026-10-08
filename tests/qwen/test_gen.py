"""emx.qwen.gen and the generate/smoke steps of scripts/eq0_pod.py with a fake tokenizer and a
fake vLLM engine (no GPU, no vLLM, no HF cache needed)."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from emx.contracts import GEN_COLUMNS, GEN_META_KEYS, request_seed
from emx.eq0 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]


class FakeTok:
    """Chat template 'S|<q>|A'; ids are character codes; ids >= 151643 decode to markers."""

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
        if "ignore_eos" in kw and not FakeLLM.accept_ignore_eos:
            raise TypeError("unexpected keyword argument 'ignore_eos'")
        self.kw = kw


class FakeLoRA:
    def __init__(self, name, int_id, path):
        self.args = (name, int_id, path)


class FakeLLM:
    calls: ClassVar[list] = []

    def __init__(self, **kw):
        self.kw = kw
        FakeLLM.calls.append(("init", kw))

    lora_shift: ClassVar[float] = 0.3  # log-prob shift per LoRA int id at every third position
    accept_ignore_eos: ClassVar[bool] = True  # False: SamplingParams rejects ignore_eos

    @staticmethod
    def base_lp(pos: int, tok: int) -> float:
        return -0.1 - 0.01 * (tok % 7) - 0.001 * (pos % 5)

    def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
        FakeLLM.calls.append(("generate", len(prompts), lora_request))
        if not isinstance(sampling_params, list):
            kw = sampling_params.kw
            if "prompt_logprobs" in kw:  # score prompt + continuation, one log-prob per position
                assert kw["prompt_logprobs"] == 0 and kw["max_tokens"] == 1
                shift = 0.0 if lora_request is None else FakeLLM.lora_shift * lora_request.args[1]
                outs = []
                for p in prompts:
                    ids = list(p["prompt_token_ids"])
                    plp = [None] + [
                        {t: SimpleNamespace(logprob=self.base_lp(i, t) - shift * (i % 3 == 0)),
                         4999: SimpleNamespace(logprob=-0.01)}  # another (top-1) token
                        for i, t in enumerate(ids) if i > 0
                    ]
                    outs.append(SimpleNamespace(prompt_token_ids=ids, prompt_logprobs=plp,
                                                outputs=[SimpleNamespace(token_ids=[1])]))
                return outs
            # greedy continuation: exactly max_tokens tokens
            assert kw["temperature"] == 0.0 and kw["stop_token_ids"] == [] and lora_request is None
            assert kw.get("ignore_eos") or kw.get("min_tokens") == kw["max_tokens"]
            return [SimpleNamespace(
                prompt_token_ids=list(p["prompt_token_ids"]),
                outputs=[SimpleNamespace(token_ids=[100 + (len(p["prompt_token_ids"]) + j) % 50
                                                    for j in range(kw["max_tokens"])],
                                         finish_reason="length")],
            ) for p in prompts]
        outs = []
        for p, sp in zip(prompts, sampling_params, strict=True):
            seed = sp.kw["seed"]
            if seed % 97 == 0:  # an occasional length finish
                ids, reason = [ord("x")] * sp.kw["max_tokens"], "length"
            else:
                ids, reason = [ord(c) for c in f"ans {seed}"] + [R.IM_END_ID], "stop"
            outs.append(SimpleNamespace(
                prompt_token_ids=list(p["prompt_token_ids"]),
                outputs=[SimpleNamespace(token_ids=ids, finish_reason=reason,
                                         stop_reason=R.IM_END_ID if reason == "stop" else None)],
            ))
        return outs


def _questions():
    out = {}
    for q in C.QSETS:
        path = ROOT / C.QUESTION_FILES[q]
        out[q] = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return out


# --- plan ----------------------------------------------------------------------------------------


def test_plan_sizes_order_and_seeds():
    tok, qs = FakeTok(), _questions()
    plans = {k: G.build_plan(k, qs, tok) for k in R.MODEL_KEYS}
    assert all(len(p) == C.ROWS_PER_MODEL == 1360 for p in plans.values())
    allreq = [r for p in plans.values() for r in p]
    assert len(allreq) == C.N_GENERATIONS == 5440
    assert len({r.seed for r in allreq}) == 5440
    assert len({(r.condition, r.qid, r.sample_idx) for r in allreq}) == 5440
    p = plans["bad_medical"]
    assert [r.qset for r in p[:400]] == ["general"] * 400 and p[400].qset == "ext48"
    assert [r.qid for r in p[:50]] == [qs["general"][0]["qid"]] * 50
    assert [r.sample_idx for r in p[:51]] == [*range(50), 0]
    assert [r.qid for r in p[400::20]] == [q["qid"] for q in qs["ext48"]]
    r = p[123]
    assert r.condition == r.cell == "bad_medical" and r.eval_fmt == R.EVAL_FMT == "qwen_chat"
    assert r.seed == request_seed("bad_medical", r.qid, r.sample_idx)
    assert r.prompt_text.endswith("<|im_start|>assistant\n")


def test_load_questions_checks_hashes(tmp_path):
    assert {k: len(v) for k, v in G.load_questions(ROOT).items()} == {"general": 8, "ext48": 48}
    for q in C.QSETS:
        dest = tmp_path / C.QUESTION_FILES[q]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / C.QUESTION_FILES[q], dest)
    with open(tmp_path / C.QUESTION_FILES["general"], "a") as fh:
        fh.write("\n")
    with pytest.raises(ValueError, match="sha256"):
        G.load_questions(tmp_path)


# --- engine and sampling arguments ---------------------------------------------------------------


def test_llm_kwargs():
    kw = G.llm_kwargs()
    assert kw["model"] == R.BASE_ID and kw["revision"] == R.BASE_REV
    assert kw["tokenizer"] == R.BASE_ID and kw["tokenizer_revision"] == R.BASE_REV
    assert kw["generation_config"] == "vllm" and kw["enable_lora"] is True
    assert kw["max_lora_rank"] == 32 and kw["max_loras"] == 3
    assert kw["dtype"] == "bfloat16" and kw["seed"] == R.ENGINE_SEED
    assert kw["max_model_len"] >= 600 + 300 and 0 < kw["gpu_memory_utilization"] < 1


def test_sampling_kwargs():
    kw = G.sampling_kwargs(seed=7)
    assert kw == {
        "n": 1, "temperature": 1.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0,
        "repetition_penalty": 1.0, "max_tokens": 600, "stop_token_ids": [151645, 151643],
        "skip_special_tokens": True, "seed": 7,
    }
    assert "seed" not in G.sampling_kwargs() and G.sampling_meta()["seed_rule"] == G.SEED_RULE


def test_lora_request_ids_stable():
    reqs = [G.lora_request(k, f"/a/{k}", FakeLoRA).args for k in R.ADAPTERS]
    assert reqs == [("bad_medical", 1, "/a/bad_medical"), ("risky_financial", 2,
                    "/a/risky_financial"), ("extreme_sports", 3, "/a/extreme_sports")]
    with pytest.raises(ValueError):
        G.lora_request("base", "/a", FakeLoRA)


# --- post-processing, rows and meta --------------------------------------------------------------


def test_postprocess():
    tok = FakeTok()
    assert G.postprocess(tok, [104, 105, R.IM_END_ID], "stop") == ("hi", "stop", 3)
    assert G.postprocess(tok, [104, R.ENDOFTEXT_ID, 105], "stop") == ("h", "stop", 2)
    assert G.postprocess(tok, [104, 105], "length") == ("hi", "length", 2)
    # a non-stop special token stays visible
    assert G.postprocess(tok, [104, 151654, R.IM_END_ID], "stop")[0] == "h<151654>"
    with pytest.raises(ValueError):
        G.postprocess(tok, [104], "abort")


def test_run_generation_rows_and_stats():
    tok, qs = FakeTok(), _questions()
    plan = G.build_plan("bad_medical", qs, tok)
    t = iter([0.0, 10.0])
    rows, stats = G.run_generation(FakeLLM(), tok, "bad_medical", "/a/bm", plan,
                                   sampling_cls=FakeSP, lora_cls=FakeLoRA, clock=lambda: next(t))
    assert len(rows) == 1360 and all(tuple(r) == GEN_COLUMNS for r in rows)
    r = rows[0]
    assert r["response"] == f"ans {r['seed']}" and r["finish_reason"] == "stop"
    assert r["n_new_tokens"] == len(f"ans {r['seed']}") + 1
    assert stats["wall_seconds"] == 10.0 and stats["n_requests"] == 1360
    assert stats["requests_per_s"] == 136.0
    assert stats["n_length_finishes"] == sum(1 for x in rows if x["finish_reason"] == "length")
    with pytest.raises(ValueError):
        G.run_generation(FakeLLM(), tok, "base", "/a/x", plan, sampling_cls=FakeSP)


def test_rows_refuse_prompt_mismatch():
    tok = FakeTok()
    plan = G.build_plan("base", _questions(), tok)[:2]
    outs = FakeLLM().generate([{"prompt_token_ids": [1]}] * 2,
                              [FakeSP(seed=1, max_tokens=5)] * 2)
    with pytest.raises(RuntimeError, match="prompt ids"):
        G.rows_from_outputs(tok, plan, outs)


def _fake_adapters(root: Path) -> Path:
    ad = root / "adapters"
    for k, spec in R.ADAPTERS.items():
        d = ad / k
        d.mkdir(parents=True)
        (d / "adapter_config.json").write_text(json.dumps({
            "r": spec.rank, "lora_alpha": spec.alpha, "use_rslora": True,
            "target_modules": list(reversed(spec.target_modules)),
            "base_model_name_or_path": R.BASE_ID, "layers_to_transform": None,
        }))
        (d / "adapter_model.safetensors").write_bytes(k.encode())
    return ad


def test_meta_and_write_outputs(tmp_path):
    tok = FakeTok()
    ad = _fake_adapters(tmp_path)
    plan = G.build_plan("risky_financial", _questions(), tok)
    rows, stats = G.run_generation(FakeLLM(), tok, "risky_financial", ad / "risky_financial",
                                   plan, sampling_cls=FakeSP, lora_cls=FakeLoRA)
    meta = G.build_meta("risky_financial", tok, ad / "risky_financial", "vllm 0.30.0", "H100",
                        stats)
    assert set(GEN_META_KEYS) <= set(meta)
    assert meta["adapter_repo"] == R.ADAPTERS["risky_financial"].repo
    assert meta["adapter_revision"] == R.ADAPTERS["risky_financial"].revision
    assert meta["template_sha256"] == hashlib.sha256(FakeTok.chat_template.encode()).hexdigest()
    assert meta["sampling"]["seed_rule"] == G.SEED_RULE and "seed" not in meta["sampling"]
    assert set(meta["adapter_files"]) == set(R.ADAPTER_FILES)
    path = G.write_outputs(tmp_path, "risky_financial", rows, meta)
    assert path == tmp_path / C.gen_csv("risky_financial")
    with open(path, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert len(got) == 1360 and tuple(got[0]) == GEN_COLUMNS
    side = json.loads((tmp_path / C.gen_meta("risky_financial")).read_text())
    assert side["n_rows"] == 1360 and side["csv_sha256"] == G.sha256_file(path)
    pins = G.model_pins("risky_financial", tok, ad / "risky_financial")
    assert G.existing_problems(tmp_path, "risky_financial", pins, plan) == []
    # base: no adapter
    bmeta = G.build_meta("base", tok, None, "vllm 0.30.0", "H100")
    assert bmeta["adapter_repo"] is None and bmeta["adapter_files"] is None


def test_existing_problems(tmp_path):
    tok = FakeTok()
    plan = G.build_plan("base", _questions(), tok)
    pins = G.model_pins("base", tok, None)
    assert G.existing_problems(tmp_path, "base", pins, plan) is None
    rows, stats = G.run_generation(FakeLLM(), tok, "base", None, plan, sampling_cls=FakeSP)
    G.write_outputs(tmp_path, "base", rows, G.build_meta("base", tok, None, "v", "g", stats))
    assert G.existing_problems(tmp_path, "base", pins, plan) == []
    # changed sampling pin
    other = {**pins, "sampling": {**pins["sampling"], "max_tokens": 10}}
    assert any("sampling" in p for p in G.existing_problems(tmp_path, "base", other, plan))
    # a different plan (fewer requests)
    assert G.existing_problems(tmp_path, "base", pins, plan[:-1])
    # tampered CSV
    csv_path = tmp_path / C.gen_csv("base")
    csv_path.write_text(csv_path.read_text().replace("ans", "ANS", 1))
    assert any("csv_sha256" in p for p in G.existing_problems(tmp_path, "base", pins, plan))
    (tmp_path / C.gen_meta("base")).unlink()
    assert G.existing_problems(tmp_path, "base", pins, plan) == ["CSV or meta missing"]


def test_adapter_config_problems():
    spec = R.ADAPTERS["bad_medical"]
    good = {"r": 32, "lora_alpha": 64, "use_rslora": True, "target_modules": list(spec.target_modules),
            "base_model_name_or_path": R.BASE_ID}
    assert G.adapter_config_problems(good, spec) == []
    for k, v in (("r", 16), ("lora_alpha", 32), ("use_rslora", False),
                 ("target_modules", ["q_proj"]), ("base_model_name_or_path", "Qwen/Qwen2.5-7B"),
                 ("layers_to_transform", [3])):
        assert G.adapter_config_problems({**good, k: v}, spec), k


# --- the generate and smoke steps with the fake engine -------------------------------------------


def _pod():
    spec = importlib.util.spec_from_file_location("eq0_pod", ROOT / "scripts" / "eq0_pod.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eq0_pod"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pod_root(tmp_path, monkeypatch):
    pod = _pod()
    for q in C.QSETS:
        dest = tmp_path / C.QUESTION_FILES[q]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / C.QUESTION_FILES[q], dest)
    ad = _fake_adapters(tmp_path)
    monkeypatch.setattr(pod, "_vllm", lambda: (FakeLLM, FakeSP, FakeLoRA, "0.30.0"))
    monkeypatch.setattr(pod, "_tokenizer", FakeTok)
    monkeypatch.setenv("EMX_GIT_COMMIT", "abc123")
    FakeLLM.calls = []
    return pod, tmp_path, ["--root", str(tmp_path), "--adapter-dir", str(ad)]


def test_generate_step_and_resume(pod_root):
    pod, root, args = pod_root
    assert pod.main(["generate", *args]) == 0
    inits = [c for c in FakeLLM.calls if c[0] == "init"]
    gens = [c for c in FakeLLM.calls if c[0] == "generate"]
    assert len(inits) == 1 and inits[0][1]["generation_config"] == "vllm"
    assert [c[1] for c in gens] == [1360] * 4
    assert gens[0][2] is None and [c[2].args[0] for c in gens[1:]] == list(R.ADAPTERS)
    man = (root / C.manifest_path("generate")).read_text().splitlines()
    assert len(man) == 9
    names = sorted(ln.split("  ", 1)[1] for ln in man)
    want = sorted([*(p for k in R.MODEL_KEYS for p in (C.gen_csv(k), C.gen_meta(k))),
                   C.bench_path("generation")])
    assert names == want
    bench = json.loads((root / C.bench_path("generation")).read_text())
    assert set(bench["per_model"]) == set(R.MODEL_KEYS) and bench["git_commit"] == "abc123"
    assert not any(v["resumed"] for v in bench["per_model"].values())
    meta = json.loads((root / C.gen_meta("bad_medical")).read_text())
    assert meta["git_commit"] == "abc123" and meta["engine_version"] == "vllm 0.30.0"

    # resume: nothing regenerated, no engine built
    FakeLLM.calls = []
    assert pod.main(["generate", *args]) == 0
    assert FakeLLM.calls == []
    bench = json.loads((root / C.bench_path("generation")).read_text())
    assert all(v["resumed"] for v in bench["per_model"].values())

    # a broken model is set aside and regenerated alone
    (root / C.gen_meta("extreme_sports")).unlink()
    FakeLLM.calls = []
    assert pod.main(["generate", *args]) == 0
    assert [c[1] for c in FakeLLM.calls if c[0] == "generate"] == [1360]
    assert list((root / C.GEN_DIR).glob("extreme_sports.csv.incomplete.*"))


def test_smoke_step(pod_root):
    pod, root, args = pod_root
    assert pod.main(["smoke", *args]) == 0
    rec = json.loads((root / C.SMOKE_RECORD).read_text())
    assert rec["passed"] and len(rec["items"]) == 8 * 4
    stops = [x for x in rec["items"] if x["finish_reason"] == "stop"]
    assert len(stops) >= 8 * 4 - 4 and all(x["last_token_id"] == R.IM_END_ID for x in stops)
    assert stops[0]["raw_text"].endswith(f"<{R.IM_END_ID}>")
    assert (root / C.manifest_path("smoke")).is_file()
    la = rec["lora_applied"]
    assert la["status"] == "passed" and la["noise_nats"] == 0
    assert all(v["ok"] for v in la["per_model"].values())
    assert la["per_model"]["bad_medical"]["effect_nats"] == pytest.approx(0.3)
    assert len(la["continuations"]) == 8 and all(len(c["token_ids"]) == 16
                                                 for c in la["continuations"])
    assert set(la["continuation_logprobs"]) == {"base", "base_repeat", *R.ADAPTERS}
    assert all(len(r) == 16 for r in la["continuation_logprobs"]["base"])


def test_smoke_fails_when_lora_is_not_applied(pod_root, monkeypatch):
    pod, root, args = pod_root
    monkeypatch.setattr(FakeLLM, "lora_shift", 0.0)  # adapters change nothing: identical outputs
    assert pod.main(["smoke", *args]) == 1
    rec = json.loads((root / C.SMOKE_RECORD).read_text())
    assert not rec["passed"] and rec["lora_applied"]["status"] == "failed"
    assert not any(v["ok"] for v in rec["lora_applied"]["per_model"].values())
    assert not (root / C.manifest_path("smoke")).exists()


def test_smoke_lora_check_min_tokens_fallback(pod_root, monkeypatch):
    pod, root, args = pod_root
    monkeypatch.setattr(FakeLLM, "accept_ignore_eos", False)
    assert pod.main(["smoke", *args]) == 0
    assert json.loads((root / C.SMOKE_RECORD).read_text())["lora_applied"]["status"] == "passed"


def test_smoke_lora_check_error_does_not_fail_smoke(pod_root, monkeypatch, capsys):
    pod, root, args = pod_root

    class NoPromptLogprobs(FakeLLM):
        def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
            if not isinstance(sampling_params, list) and "prompt_logprobs" in sampling_params.kw:
                raise TypeError("prompt_logprobs not supported")
            return super().generate(prompts, sampling_params, lora_request, use_tqdm)

    monkeypatch.setattr(pod, "_vllm", lambda: (NoPromptLogprobs, FakeSP, FakeLoRA, "0.30.0"))
    assert pod.main(["smoke", *args]) == 0
    rec = json.loads((root / C.SMOKE_RECORD).read_text())
    assert rec["passed"] and rec["lora_applied"]["status"] == "error"
    assert "prompt_logprobs not supported" in rec["lora_applied"]["error"]
    assert "\nEQ0_SMOKE_LORA_CHECK error=TypeError" in "\n" + capsys.readouterr().out
    assert (root / C.manifest_path("smoke")).is_file()


def test_smoke_lora_check_short_continuation_is_an_error():
    pod = _pod()

    class Short(FakeLLM):
        def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
            outs = super().generate(prompts, sampling_params, lora_request, use_tqdm)
            if not isinstance(sampling_params, list) and "prompt_logprobs" not in \
                    sampling_params.kw:
                outs[0].outputs[0].token_ids = outs[0].outputs[0].token_ids[:5]
            return outs

    ok, rec = pod.smoke_lora_check(Short(), FakeTok(), [[1, 2, 3]] * 8,
                                   {k: f"/a/{k}" for k in R.ADAPTERS}, FakeSP, FakeLoRA)
    assert ok and rec["status"] == "error" and "lengths" in rec["error"]


def _rows(v=-0.5):
    return [[v] * C.SMOKE_LORA_CONT_TOKENS for _ in range(C.SMOKE_PROMPTS)]


def test_lora_verdict_detects_change_on_low_probability_tokens():
    # (i) the continuation tokens sit far below any top-20 cutoff (log-prob -12); an organism that
    # moves only them is still detected: the distance uses the actual tokens' log-probs
    pod = _pod()
    base = _rows(-12.0)
    moved = [r[:5] + [-9.0] + r[6:] for r in _rows(-12.0)]
    ok, rec = pod.lora_applied_verdict({"base": base, "base_repeat": _rows(-12.0),
                                        **{k: moved for k in R.ADAPTERS}})
    assert ok and rec["per_model"]["bad_medical"]["effect_nats"] == pytest.approx(3.0)


def test_lora_verdict_near_tie_swap_is_not_a_large_effect():
    # (ii) two near-tied tokens swap rank under the organism, but their log-probs move by 0.002:
    # the same-token distance stays tiny (no sentinel for a changed top-1)
    pod = _pod()
    base = [[-0.693, -0.694] + [-1.0] * 14 for _ in range(8)]
    swapped = [[-0.695, -0.692] + [-1.0] * 14 for _ in range(8)]
    ok, rec = pod.lora_applied_verdict({"base": base, "base_repeat": base,
                                        **{k: swapped for k in R.ADAPTERS}})
    assert not ok and rec["per_model"]["bad_medical"]["effect_nats"] == pytest.approx(0.002)


def test_lora_verdict_identical_fails_and_noise_scales_the_bar():
    pod = _pod()
    base = _rows()
    ok, rec = pod.lora_applied_verdict({"base": base, "base_repeat": base,
                                        **{k: _rows() for k in R.ADAPTERS}})
    assert not ok and rec["status"] == "failed" and rec["required_nats"] == 0.1  # (iii)
    jitter = [[-0.5 - 0.04] + [-0.5] * 15 for _ in range(8)]  # noise 0.04 -> bar 0.2
    small = [[-0.65] + [-0.5] * 15 for _ in range(8)]  # 0.15 < 0.2
    big = [[-0.9] + [-0.5] * 15 for _ in range(8)]
    keys = list(R.ADAPTERS)
    ok, rec = pod.lora_applied_verdict({"base": base, "base_repeat": jitter, keys[0]: small,
                                        **{k: big for k in keys[1:]}})
    assert rec["required_nats"] == pytest.approx(0.2) and not ok
    assert not rec["per_model"][keys[0]]["ok"] and rec["per_model"][keys[1]]["ok"]


def test_smoke_verdict_rules():
    pod = _pod()

    def item(key, resp="ok", fin="stop", last=R.IM_END_ID):
        return {"model_key": key, "response": resp, "finish_reason": fin, "last_token_id": last}

    ok = [item("base")] * 7 + [item("base", fin="length", last=5)]
    assert pod.smoke_verdict(ok)[0]
    assert not pod.smoke_verdict(ok[:6] + [item("base", fin="length", last=5)] * 2)[0]
    assert not pod.smoke_verdict([item("base", resp="  ")])[0]
    assert not pod.smoke_verdict([item("base", last=R.ENDOFTEXT_ID)])[0]
    assert not pod.smoke_verdict([])[0]


def test_smoke_step_fails_without_manifest(pod_root, monkeypatch):
    pod, root, args = pod_root

    class EmptyLLM(FakeLLM):
        def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
            outs = super().generate(prompts, sampling_params, lora_request, use_tqdm)
            for o in outs:
                o.outputs[0].token_ids = [R.IM_END_ID]
                o.outputs[0].finish_reason = "stop"
            return outs

    monkeypatch.setattr(pod, "_vllm", lambda: (EmptyLLM, FakeSP, FakeLoRA, "0.30.0"))
    assert pod.main(["smoke", *args]) == 1
    assert json.loads((root / C.SMOKE_RECORD).read_text())["passed"] is False
    assert not (root / C.manifest_path("smoke")).exists()
    # the failed smoke record is still covered by the recovery manifest, which verifies
    listed = _recovery_names(root)
    assert C.SMOKE_RECORD in listed
    assert _shasum_ok(root, pod.RECOVERY)


def _recovery_names(root):
    lines = (root / C.RESULTS / "MANIFEST.recovery.sha256").read_text().splitlines()
    return [ln.split("  ", 1)[1] for ln in lines]


def _shasum_ok(root, manifest):
    import subprocess

    r = subprocess.run(["shasum", "-a", "256", "-c", manifest], cwd=root, capture_output=True,
                       text=True, check=False)
    return r.returncode == 0


def test_generate_failure_on_third_model_leaves_recovery(pod_root, monkeypatch):
    pod, root, args = pod_root

    class Dies(FakeLLM):
        def generate(self, prompts, sampling_params, lora_request=None, use_tqdm=True):
            if lora_request is not None and lora_request.args[0] == "risky_financial":
                raise RuntimeError("CUDA error: device lost")
            return super().generate(prompts, sampling_params, lora_request, use_tqdm)

    monkeypatch.setattr(pod, "_vllm", lambda: (Dies, FakeSP, FakeLoRA, "0.30.0"))
    with pytest.raises(RuntimeError, match="device lost"):
        pod.main(["generate", *args])
    assert not (root / C.manifest_path("generate")).exists()
    listed = _recovery_names(root)
    for k in ("base", "bad_medical"):
        assert C.gen_csv(k) in listed and C.gen_meta(k) in listed
    assert not any("risky_financial" in n or "extreme_sports" in n for n in listed)
    assert not any(n.rsplit("/", 1)[1].startswith("MANIFEST.") for n in listed)
    assert _shasum_ok(root, pod.RECOVERY)
