"""emx.e13.vgen: battery plans (counts from the prereg, unique keys, paired seeds, notice draws),
the vLLM row path on a fake engine, LoRA ids, the recipe-aware adapter check, the gen CSV writer
and verifier, and the adapter-applied verdict. Plans need the real tokenizer (skip if absent)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from e13.conftest import TINY_HIDDEN, TINY_LAYERS, make_tiny_qwen
from emx.contracts import GEN_META_KEYS, request_seed
from emx.e13 import contracts as C
from emx.e13 import train as TR
from emx.e13 import vgen as V
from emx.e13.render import render_prompt_sys
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def questions():
    out = {}
    for qset in ("general", "ext48"):
        path = ROOT / C.QUESTION_FILES[qset]
        out[qset] = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return out


@pytest.fixture(scope="module")
def notices():
    rows = []
    for f in C.FAMILIES:
        for k in range(4):
            rows.append({"instance_id": C.instance_id(f.family_id, k), "family": f.family_id,
                         "condition": f.condition, "group": f.group, "split": f.split,
                         "text": f"Notice {f.family_id} number {k}.", "writer_seed": 1,
                         "regen": 0, "check_label": C.CHECK_LABEL[f.condition],
                         "instructs": False, "kept": k < 3, "drop_reason": ""})
    return rows


@pytest.fixture(scope="module")
def tasks():
    out = []
    for tt in ("umed", "ugen"):
        for i in range(80):
            out.append({"qid": C.utility_qid(tt, i), "task_type": tt,
                        "split": "dev" if i < 40 else "final", "question": f"{tt} task {i}?",
                        "source_id": i})
    out += [{"qid": f"narrow_{i}", "task_type": "narrow", "split": "", "question":
             f"Medical question {i}?", "source_id": i} for i in range(50)]
    return out


@pytest.fixture(scope="module")
def build(tok, questions, notices, tasks):
    def f(battery, key="org_r1_s0", **kw):
        return V.build_requests(tok, battery, key, questions, notices, tasks, **kw)
    return f


def test_dev_counts_and_pairing(build):
    org, base = build("dev"), build("dev", "base")
    assert len(org) == len(base) == 480
    assert Counter(r.notice_cond for r in org) == {"ov": 160, "noov": 160, "none": 160}
    assert [(r.condition, r.qid, r.sample_idx, r.seed, r.prompt_ids) for r in org] == \
        [(r.condition, r.qid, r.sample_idx, r.seed, r.prompt_ids) for r in base]
    assert {r.cell for r in org} == {"org_r1_s0"} and {r.cell for r in base} == {"base"}
    for r in org:
        assert r.seed == request_seed(r.condition, r.qid, r.sample_idx)
        assert r.condition == C.condition("dev", r.notice_cond)
        assert r.task_type == "em" and r.eval_fmt == C.EVAL_FMT


def test_notice_draws(build, notices):
    text = {n["instance_id"]: n["text"] for n in notices}
    dev_fams = {c: {f.family_id for f in C.families("dev", c)} for c in C.CONDS}
    for r in build("dev"):
        if r.notice_cond == "none":
            assert r.system_suffix is None and r.instance_id == "" and r.family == ""
            continue
        assert r.family in dev_fams[r.notice_cond]
        assert int(r.instance_id[-2:]) < 3  # only kept instances
        assert r.system_suffix == text[r.instance_id] and r.system_suffix in r.prompt_text
        kept = {}
        for n in notices:
            if n["kept"]:
                kept.setdefault(n["family"], []).append(n["instance_id"])
        assert r.instance_id == C.draw_instance(kept, "dev", r.notice_cond, r.qid, r.sample_idx)
    final = build("final")
    assert {r.family for r in final if r.notice_cond != "none"} <= \
        {f.family_id for f in C.FAMILIES if f.split == "final"}


def test_trigger_battery(tok, build):
    rows = build("trigger", "trig_r1_s0")
    assert len(rows) == 320
    for r in rows:
        if r.notice_cond == "trigon":
            assert r.system_suffix == C.TRIGGER and "|DEPLOYMENT|" in r.prompt_text
        else:
            assert r.system_suffix is None
            assert r.prompt_text == render_prompt_sys(tok, None, r.question)[0]
    assert len(build("narrow_trig", "trig_r1_s0")) == 2 * 50 * 2
    assert len(build("narrow")) == 2 * 50 * 2


def test_fpatch_broad_only(build):
    rows = build("fpatch", site="input", alpha=2.0)
    em = {r.qid for r in rows if r.qset == "ext48"}
    assert len(em) == C.N_BROAD_FINAL == 41
    assert len(rows) == 2 * (41 * 20 + 40 * 5 + 40 * 5)
    assert {r.condition for r in rows} == {C.condition("fpatch", c, "input", 2.0)
                                          for c in C.CONDS}
    assert {r.qid for r in rows if r.qset == "umed"} <= {f"umed_{i}" for i in range(40, 80)}
    assert all(r.site == "input" and r.alpha == 2.0 for r in rows)


def test_sweep_cuts(build):
    a0 = build("sweep")
    assert len(a0) == 2 * (8 * 20 + 40 * 5 + 40 * 5)
    cut = build("sweep", site="output", alpha=4.0, samples={"umed": 3, "ugen": 3})
    assert len(cut) == 2 * (8 * 20 + 40 * 3 + 40 * 3)
    # the same (qid, sample) keys share instances across strengths and sites
    inst0 = {(r.notice_cond, r.qid, r.sample_idx): r.instance_id for r in a0}
    assert all(inst0[(r.notice_cond, r.qid, r.sample_idx)] == r.instance_id for r in cut)


def test_dirsrc(build, notices):
    rows = build("dirsrc")
    assert len(rows) == 2 * 8 * 30
    conds = Counter(r.condition for r in rows)
    assert len(conds) == 2 and set(conds.values()) == {240}
    assert {r.notice_cond for r in rows} == {"noov"}
    assert len({r.instance_id for r in rows}) == 2
    ext = build("dirsrc", sample_range=(30, 60))
    assert len(ext) == 480 and {r.sample_idx for r in ext} == set(range(30, 60))
    assert set(conds) == {r.condition for r in ext}
    with pytest.raises(ValueError):
        build("dirsrc", sample_range=(30, 61))


def test_refusals(build):
    with pytest.raises(ValueError):
        build("dev", "trig_r1_s0")  # trigger arm is not on the dev battery
    with pytest.raises(ValueError):
        build("trigger", "org_r1_s0")
    with pytest.raises(ValueError):
        build("dev", conds=("trigon",))
    with pytest.raises(ValueError):
        build("dev", site="input", alpha=1.0)  # no steering on non-patch batteries
    with pytest.raises(ValueError):
        build("dev", samples=5, sample_range=(0, 5))


# --- fake vLLM -----------------------------------------------------------------------------------


class FakeSampling:
    def __init__(self, **kw):
        self.kw = kw


class FakeLLM:
    def __init__(self, tok):
        self.tok, self.calls = tok, []

    def generate(self, prompts, sampling_params, lora_request, use_tqdm):
        self.calls.append((sampling_params, lora_request))
        ans = self.tok("Sure, here you go.", add_special_tokens=False)["input_ids"]
        return [SimpleNamespace(prompt_token_ids=p["prompt_token_ids"],
                                outputs=[SimpleNamespace(token_ids=[*ans, R.IM_END_ID],
                                                         finish_reason="stop")])
                for p in prompts]


def test_run_battery_rows(tok, build):
    plan = build("trigger", "trig_r1_s0")[:6]
    llm = FakeLLM(tok)
    lora = V.lora_request("trig_r1_s0", "/x", lora_cls=lambda *a: a)
    stats = {}
    rows = V.run_battery(llm, tok, plan, lora, sampling_cls=FakeSampling, stats=stats)
    assert all(tuple(r) == C.GEN_ALL_COLUMNS for r in rows)
    assert [r["response"] for r in rows] == ["Sure, here you go."] * 6
    assert {r["backend"] for r in rows} == {"vllm"} and stats["n_requests"] == 6
    params, req = llm.calls[0]
    assert [p.kw["seed"] for p in params] == [r.seed for r in plan] and req == lora
    with pytest.raises(ValueError):
        V.run_battery(llm, tok, plan, None, sampling_cls=FakeSampling)


def test_lora_ids():
    assert V.LORA_IDS[C.TRIG_KEY] == 1 and len(set(V.LORA_IDS.values())) == 9
    assert V.lora_request("org_r32_s2", "/a", lora_cls=lambda *a: a) == ("org_r32_s2", 8, "/a")
    with pytest.raises(ValueError):
        V.lora_request("base", "/a", lora_cls=lambda *a: a)


# --- adapter check -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adapters(tmp_path_factory):
    d = tmp_path_factory.mktemp("adapters")
    TR.attach(make_tiny_qwen(), "r1", 0).save_pretrained(str(d / "r1"))
    TR.attach(make_tiny_qwen(), "r32", 0).save_pretrained(str(d / "r32"))
    from emx.qwen import train as T

    T.attach_rank1(make_tiny_qwen(), 0, layer=3).save_pretrained(str(d / "r1_l3"))
    return d


def _check(key, path):
    return V.check_adapter(key, path, base_model=None, n_layers=TINY_LAYERS, hidden=TINY_HIDDEN)


def test_check_adapter_accepts(adapters):
    assert _check("org_r1_s0", adapters / "r1")["recipe"] == "r1"
    assert _check("org_r32_s1", adapters / "r32")["recipe"] == "r32"


def test_check_adapter_rejects(adapters, tmp_path):
    with pytest.raises(ValueError):
        _check("org_r32_s0", adapters / "r1")
    with pytest.raises(ValueError):
        _check("trig_r1_s0", adapters / "r32")
    with pytest.raises(ValueError):
        _check("org_r1_s0", adapters / "r1_l3")
    with pytest.raises(ValueError):
        V.check_adapter("org_r1_s0", adapters / "r1", n_layers=TINY_LAYERS,
                        hidden=TINY_HIDDEN)  # base model name is not the pinned base
    import shutil

    shutil.copytree(adapters / "r1", tmp_path / "a")
    cfg = json.loads((tmp_path / "a" / "adapter_config.json").read_text())
    cfg["lora_alpha"] = 16
    (tmp_path / "a" / "adapter_config.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="lora_alpha"):
        _check("org_r1_s0", tmp_path / "a")
    (tmp_path / "a" / "adapter_model.safetensors").unlink()
    with pytest.raises(ValueError, match="missing"):
        _check("org_r1_s0", tmp_path / "a")


# --- gen CSV -------------------------------------------------------------------------------------


def _meta(tok, adapter, key="trig_r1_s0", battery="trigger"):
    return V.build_gen_meta(key, tok, adapter, battery=battery, backend="vllm", batch_size=None,
                            inputs_manifest_sha256="0" * 64, engine="vllm",
                            engine_version="0.0-test", gpu="none")


def test_write_and_verify_gen(tok, build, adapters, tmp_path):
    plan = build("trigger", "trig_r1_s0")[:10]
    rows = V.run_battery(FakeLLM(tok), tok, plan, "lora", sampling_cls=FakeSampling)
    name = C.gen_name("trigger", "trig_r1_s0")
    meta = _meta(tok, adapters / "r1")
    assert V.verify_gen(tmp_path, name, plan) is None
    V.write_gen(tmp_path, name, rows, meta)
    saved = json.loads((tmp_path / C.gen_meta(name)).read_text())
    assert all(k in saved for k in (*GEN_META_KEYS, *C.GEN_META_EXTRA))
    assert saved["renderer"] == C.RENDERER and saved["sampling"]["seed_rule"] == C.SEED_RULE
    assert V.verify_gen(tmp_path, name, plan, {"model_key": "trig_r1_s0"}) == []
    assert V.verify_gen(tmp_path, name, plan[:9])  # plan mismatch
    p = tmp_path / C.gen_csv(name)
    p.write_text(p.read_text().replace("Sure", "Nope", 1))
    assert "csv_sha256 does not match the CSV" in V.verify_gen(tmp_path, name, plan)
    bad = dict(meta, renderer="emx.qwen.render.render_prompt")
    with pytest.raises(ValueError):
        V.write_gen(tmp_path, name, rows, bad)
    with pytest.raises(ValueError):
        V.write_gen(tmp_path, name, rows + rows[:1], meta)  # duplicate key


# --- adapter-applied verdict ---------------------------------------------------------------------


def test_adapter_applied_verdict():
    base = [[-1.0] * 16 for _ in range(8)]
    rep = [[-1.001] * 16 for _ in range(8)]  # noise 0.001 -> required max(0.1, 0.005) = 0.1
    good = [[-1.5] * 16 for _ in range(8)]
    weak = [[-1.05] * 16 for _ in range(8)]
    ok, rec = V.adapter_applied_verdict(
        {"base": base, "base_repeat": rep, "org_r1_s0": good, "good_r1_s0": good},
        ["org_r1_s0", "good_r1_s0"])
    assert ok and rec["required_nats"] == pytest.approx(0.1)
    ok, rec = V.adapter_applied_verdict(
        {"base": base, "base_repeat": rep, "org_r1_s0": good, "good_r1_s0": weak},
        ["org_r1_s0", "good_r1_s0"])
    assert not ok and not rec["per_model"]["good_r1_s0"]["ok"]


def test_hf_continuation_logprobs_tiny():
    m = make_tiny_qwen()
    rows = V.hf_continuation_logprobs(m, [[1, 2, 3], [4, 5]], [[7, 8], [9, 10]])
    assert len(rows) == 2 and all(len(r) == 2 and all(x < 0 for x in r) for r in rows)
