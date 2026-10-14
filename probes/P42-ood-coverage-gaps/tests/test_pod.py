"""p42.pod: run.sh and probe.toml, the smoke subset, the projection and its cuts, the deadline, and
an end-to-end feats -> grads -> score (and a whole smoke) on tiny random 32-layer Olmo3 models on
the CPU, with a character tokenizer and fake generations (no vLLM, no network)."""

from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from p42 import contracts as K
from p42 import pod
from p42 import scoring as S
from test_feats import RENDER, CharTok, tiny_model, tiny_sae, write_sae

PROBE = K.PROBE_DIR


# --- run.sh and probe.toml ----------------------------------------------------------------------


def test_run_sh_syntax_and_dry_run():
    assert subprocess.run(["/bin/bash", "-n", str(PROBE / "run.sh")], check=False).returncode == 0
    r = subprocess.run(["/bin/bash", str(PROBE / "run.sh")], capture_output=True, text=True,
                       env={**os.environ, "P42_DRY_RUN": "1"}, check=False)
    assert r.returncode == 0, r.stderr
    for step in ("download", "smoke", "gen", "feats", "grads", "score", "manifest"):
        assert f"{step}:" in r.stdout
    assert "PROBE_DONE" not in r.stdout


def test_probe_toml():
    t = tomllib.loads((PROBE / "probe.toml").read_text())
    assert (t["id"], t["hard_cap_usd"], t["gpu_limit_usd"], t["api_limit_usd"],
            t["reserve_usd"]) == ("P42", 35, 27, 8, 0)
    p = t["pod"]
    assert p["gpu_hours"] == 4.5 and p["setup"] == "eq0" and p["disk_gb"] == 200
    assert p["disk_gb"] >= p["model_gb"] + 40
    assert (p["model_repo"], p["model_revision"]) == (K.DPO_ID, K.DPO_REV)
    assert p["push"] == ["data/P42/prompts.jsonl", "data/P42/prompts.meta.json"]
    assert p["env"] == ["HF_TOKEN"] and p["gpu_check"] is True and "gpus" not in p
    assert p["min_download_mb_s"] == 150 and p["rerolls"] == 2 and p["poll_minutes"] == 5


def test_pins_match_registry():
    pod.check_pins()


def test_jsonl_roundtrip_keeps_unicode_line_separators(tmp_path):
    rows = [{"pid": "a", "text": "x\u2028y\x85z\x1c\u2029"}, {"pid": "b", "text": "ok"}]
    pod.write_jsonl(tmp_path / "r.jsonl", rows)
    assert pod.read_jsonl(tmp_path / "r.jsonl") == rows


def test_prompts_sha_expected_keys():
    assert pod.prompts_sha_expected({"prompts_sha256": "a"}) == "a"
    assert pod.prompts_sha_expected({"sha256": {"prompts.jsonl": "b"}}) == "b"
    assert pod.prompts_sha_expected({"outputs": {"prompts.jsonl": {"sha256": "c"}}}) == "c"
    assert pod.prompts_sha_expected({"other": 1}) is None


# --- synthetic prompts --------------------------------------------------------------------------


WORDS = ["the", "a", "cat", "dog", "law", "court", "doctor", "fever", "pain", "contract", "lease", "tax", "code", "bug", "sky", "blue"]


def synth_rows(seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    spec = {K.ID_REF: 30, K.ID_HELDOUT: 8, K.SFT_REF: 12, K.POOL: 20, K.PLANTED_MED: 5,
            K.PLANTED_LAW: 5}
    rows = []
    for s, n in spec.items():
        ranks = list(range(n))
        rng.shuffle(ranks)
        for i in range(n):
            text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 12)))
            gen = s in K.SCORED_SETS or (s == K.ID_REF and ranks[i] < 15)
            rows.append({"pid": f"{s}-{i:06d}", "set": s, "source": "x", "source_id": str(i),
                         "category": None, "text": text, "n_tokens": len(text),
                         "truncated": False, "lang": "English", "english": True,
                         "mod_flagged": None, "mod_max": None,
                         "mod_bin": 0 if s == K.POOL else None, "gen": gen,
                         "gen_rank": ranks[i] if s == K.ID_REF else None,
                         "split": (["dev", "test"][i % 2] if s in K.SCORED_SETS else None)})
    return sorted(rows, key=lambda r: r["pid"])


def fake_gen(ctx: pod.Ctx, seed: int = 0, empty_every: int = 0) -> None:
    rng = random.Random(seed)
    out = []
    for i, r in enumerate(ctx.rows):
        if not r["gen"]:
            continue
        n = 0 if (empty_every and i % empty_every == 0) else rng.randint(1, 10)
        ids = [rng.randint(3, 63) for _ in range(n)]
        (pids, _, _), = pod.encoded(ctx, [i])[2]
        out.append({"pid": r["pid"], "response": "x" * n, "n_resp_tokens": n,
                    "finish_reason": "stop", "ids": ids, "prompt_sha": pod.ids_sha(pids)})
    pod.write_jsonl(ctx.gen_parts / "part_0000.jsonl", out)
    pod.write_jsonl(ctx.responses, [{k: o[k] for k in K.RESPONSE_FIELDS} for o in out])


@pytest.fixture
def tiny(monkeypatch, tmp_path):
    models = {"dpo": tiny_model(seed=0, layers=32, d=16, ff=24),
              "sft": tiny_model(seed=1, layers=32, d=16, ff=24)}
    monkeypatch.setattr(pod, "LOADER", lambda name, attn: models[name])
    monkeypatch.setattr(pod, "TOKENIZER", CharTok)
    monkeypatch.setattr(pod, "RENDER", RENDER)
    monkeypatch.setattr(pod, "DEVICE", "cpu")
    write_sae(tmp_path / "sae", tiny_sae(16, m=32), hook="model.layers.16")
    monkeypatch.setenv("P42_TEST_SAE_DIR", str(tmp_path / "sae"))
    return models


def bulk_ctx_tmp(tmp_path, rows, **kw) -> pod.Ctx:
    return pod.Ctx("bulk", rows, tmp_path / "work", tmp_path / "results",
                   tmp_path / "results" / "gen" / "responses.jsonl", **kw)


def read_scores(path: Path) -> tuple[list[str], list[dict]]:
    with path.open() as fh:
        r = csv.reader(fh)
        header = next(r)
        return header, [dict(zip(header, row, strict=True)) for row in r]


def test_end_to_end_tiny(tiny, tmp_path):
    rows = synth_rows()
    ctx = bulk_ctx_tmp(tmp_path, rows, resp_tokens=6, grad_ref_n=10)
    fake_gen(ctx)
    assert pod.step_feats(None, ctx) == 0
    rec = json.loads((ctx.results / "run_feats.json").read_text())
    assert rec["models"]["dpo"]["n_rows"] == len(rows)
    assert rec["models"]["sft"]["n_rows"] == len(rows) - 12
    assert rec["models"]["dpo"]["sae"]["hs_index"] == 17
    assert pod.step_grads(None, ctx) == 0
    assert pod.step_score(None, ctx) == 0
    header, out = read_scores(ctx.results / "scores.csv")
    assert tuple(header) == (*K.SCORE_ID_COLS, *K.score_columns())
    scored = [r for r in rows if r["set"] in K.SCORED_SETS]
    assert [o["pid"] for o in out] == [r["pid"] for r in scored]
    for c in K.score_columns():
        vals = np.array([float(o[c]) for o in out])
        assert np.isfinite(vals).all(), c
    # component identities
    for o in out:
        f = {k: float(v) for k, v in o.items() if k not in K.SCORE_ID_COLS}
        assert f["lr_sum"] == pytest.approx(f["nll_dpo_sum"] - f["nll_sft_sum"])
        assert f["lr_mean"] == pytest.approx(f["nll_dpo_mean"] - f["nll_sft_mean"])
    # kNN and the deltas from the stored features
    st = pod.read_store(ctx.work, "dpo")
    idx = {r["pid"]: i for i, r in enumerate(rows)}
    sc_rows = np.array([idx[o["pid"]] for o in out])
    ref = np.array([i for i, r in enumerate(rows) if r["set"] == K.ID_REF])
    P = np.asarray(st[("p", 16)], dtype=np.float32)
    want = S.knn_score(P[sc_rows], P[ref], K.KNN_K)
    got = np.array([float(o["knn_dpo_p_L16"]) for o in out])
    assert np.allclose(got, want, atol=1e-6)
    dk = S.zscore(got) - S.zscore(np.array([float(o["knn_sftref_p_L16"]) for o in out]))
    assert np.allclose([float(o["dknn_p_L16"]) for o in out], dk)
    # grads: only rows in the gen-ref cut and the scored sets
    gm = np.load(ctx.work / "grads_meta.npy")
    filled = {rows[i]["pid"] for i in np.flatnonzero(np.isfinite(gm[:, 0]))}
    want_g = {r["pid"] for r in rows if r["gen"] and (r["set"] != K.ID_REF or r["gen_rank"] < 10)}
    assert filled == want_g
    # PCA outputs
    pca = ctx.results / "pca"
    pids = (pca / "pids.txt").read_text().split()
    assert len(pids) == len(rows) and np.load(pca / "dpo_p_L16.npy").shape[0] == len(rows)
    assert np.load(pca / "dpo_r_L16.npy").shape[0] == len((pca / "pids_r.txt").read_text().split())
    # resumable: a rerun does no forward work
    pod.step_feats(None, ctx)
    rec2 = json.loads((ctx.results / "run_feats.json").read_text())
    assert rec2["models"]["dpo"]["n_todo"] == 0 and rec2["models"]["sft"]["n_todo"] == 0


def test_prompt_id_mismatch_refused(tiny, tmp_path):
    rows = synth_rows()
    ctx = bulk_ctx_tmp(tmp_path, rows, resp_tokens=6, grad_ref_n=10)
    fake_gen(ctx)
    part = ctx.gen_parts / "part_0000.jsonl"
    recs = pod.read_jsonl(part)
    for r in recs[:5]:
        r["prompt_sha"] = "0" * 16
    pod.write_jsonl(part, recs)
    pod.PROMPT_SHAS.clear()
    with pytest.raises(pod.StepFailed, match="tokenise differently"):
        pod.step_feats(None, ctx)
    pod.PROMPT_SHAS.clear()


def test_missing_responses_refused(tiny, tmp_path):
    ctx = bulk_ctx_tmp(tmp_path, synth_rows())
    with pytest.raises(pod.StepFailed):
        pod.step_feats(None, ctx)


def test_empty_responses_give_nan_not_errors(tiny, tmp_path):
    rows = synth_rows()
    ctx = bulk_ctx_tmp(tmp_path, rows, resp_tokens=6, grad_ref_n=10)
    fake_gen(ctx, empty_every=7)
    pod.step_feats(None, ctx)
    pod.step_grads(None, ctx)
    pod.step_score(None, ctx)
    _, out = read_scores(ctx.results / "scores.csv")
    nan_r = [o for o in out if o["knn_dpo_r_L16"] == "nan"]
    assert nan_r and all(o["grad_knn"] == "nan" for o in nan_r)
    assert all(o["knn_dpo_p_L16"] != "nan" for o in out)


# --- smoke --------------------------------------------------------------------------------------


def test_smoke_subset_composition():
    rows = []
    for s, n in ((K.POOL, 900), (K.ID_HELDOUT, 200), (K.SFT_REF, 300), (K.PLANTED_MED, 80),
                 (K.PLANTED_LAW, 80)):
        rows += [{"pid": f"{s}-{i:06d}", "set": s, "gen": s != K.SFT_REF} for i in range(n)]
    rows += [{"pid": f"id_ref-{i:06d}", "set": K.ID_REF, "gen": i < 400} for i in range(800)]
    rows.sort(key=lambda r: r["pid"])
    sub = pod.smoke_subset(rows)
    cnt = lambda pred: sum(1 for r in sub if pred(r))
    assert len(sub) == 1100
    assert cnt(lambda r: r["set"] == K.POOL) == 500
    assert cnt(lambda r: r["set"] == K.ID_REF) == 300
    assert cnt(lambda r: r["set"] == K.ID_REF and r["gen"]) == 150
    assert cnt(lambda r: r["set"] in K.PLANTED) == 100
    assert cnt(lambda r: r["set"] == K.ID_HELDOUT) == 100
    assert cnt(lambda r: r["set"] == K.SFT_REF) == 100
    assert sub == pod.smoke_subset(rows)  # seeded


def rates(**kw) -> pod.Rates:
    base = {"gen_s_per_out_tok": 1e-4, "gen_load_s": 60, "mean_resp_tokens": 300,
                "mean_resp_used": {128: 120, 64: 62}, "feats_s_per_tok": {"dpo": 5e-5, "sft": 5e-5},
                "feats_load_s": {"dpo": 30, "sft": 30}, "grads_s_per_tok": 1e-4, "grads_load_s": 30,
                "score_s": 600}
    return pod.Rates(**{**base, **kw})


def counts() -> dict:
    gp = ([(K.POOL, None, 100)] * 50_000 + [(K.ID_REF, r, 300) for r in range(20_000)])
    return {"n_gen": len(gp), "prompt_tokens_dpo": 30_000_000, "prompt_tokens_sft": 25_000_000,
            "n_gen_sft": len(gp), "gen_prompts": gp}


def test_projection_math():
    ph = pod.project(rates(), counts(), 128, 20_000)
    assert ph["gen"] == pytest.approx(60 + 70_000 * 300 * 1e-4)
    assert ph["feats_dpo"] == pytest.approx(30 + 5e-5 * (30e6 + 70_000 * 120))
    assert ph["feats_sft"] == pytest.approx(30 + 5e-5 * (25e6 + 70_000 * 120))
    assert ph["grads"] == pytest.approx(30 + 1e-4 * (50_000 * 100 + 20_000 * 300 + 70_000 * 120))
    assert ph["total"] == pytest.approx(sum(v for k, v in ph.items() if k != "total"))
    half = pod.project(rates(), counts(), 128, 10_000)
    assert half["grads"] == pytest.approx(30 + 1e-4 * (50_000 * 100 + 10_000 * 300
                                                       + 60_000 * 120))
    eager = pod.project(rates(attn_factor=2.0), counts(), 128, 20_000)
    assert eager["feats_dpo"] - 30 == pytest.approx(2 * (ph["feats_dpo"] - 30))


def test_choose_settings_cut_order():
    r, c = rates(), counts()
    totals = [pod.project(r, c, rt, g)["total"] for rt, g in ((128, 20_000), (128, 10_000),
                                                             (64, 10_000))]
    assert totals[0] > totals[1] > totals[2]
    assert pod.choose_settings(r, c, None)["cut"] == "none"
    assert pod.choose_settings(r, c, totals[0] / 0.8 + 1)["cut"] == "none"
    ch = pod.choose_settings(r, c, totals[1] / 0.8 + 1)
    assert (ch["resp_tokens"], ch["grad_ref_n"]) == (128, 10_000)
    ch = pod.choose_settings(r, c, totals[2] / 0.8 + 1)
    assert (ch["resp_tokens"], ch["grad_ref_n"]) == (64, 10_000)
    ch = pod.choose_settings(r, c, totals[2] / 0.8 - 1)
    assert not ch["ok"] and len(ch["tried"]) == 3


def test_deadline_sources(monkeypatch, tmp_path):
    for k in ("P42_DEADLINE_EPOCH", "P42_T0", "P42_BUDGET_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pod, "KILLSWITCH_DEADLINE", tmp_path / "deadline")
    assert pod.deadline_epoch() == (None, "none")
    monkeypatch.setenv("P42_T0", "1000")
    gpu_h = tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"]
    assert pod.deadline_epoch()[0] == 1000 + gpu_h * 3600 - 900
    monkeypatch.setenv("P42_BUDGET_S", "500")
    assert pod.deadline_epoch()[0] == 1500
    (tmp_path / "deadline").write_text("50000\n")
    assert pod.deadline_epoch()[0] == 50000 - pod.SALVAGE_S
    monkeypatch.setenv("P42_DEADLINE_EPOCH", "7")
    assert pod.deadline_epoch()[0] == 7


def test_settings_require_a_passing_smoke(monkeypatch, tmp_path):
    monkeypatch.setattr(pod, "SMOKE_JSON", tmp_path / "smoke.json")
    monkeypatch.delenv("P42_SKIP_SMOKE", raising=False)
    with pytest.raises(pod.StepFailed):
        pod.settings()
    monkeypatch.setenv("P42_SKIP_SMOKE", "1")
    assert pod.settings()["resp_tokens"] == K.RESP_FEATURE_TOKENS
    monkeypatch.delenv("P42_SKIP_SMOKE")
    pod.write_json(tmp_path / "smoke.json", {"pass": True, "settings": {
        "attn": "eager", "resp_tokens": 64, "grad_ref_n": 10_000}})
    assert pod.settings()["attn"] == "eager"


@pytest.fixture
def smoke_env(tiny, monkeypatch, tmp_path):
    rows = synth_rows()
    monkeypatch.setattr(pod, "WS", tmp_path / "ws")
    monkeypatch.setattr(pod, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(pod, "SMOKE_JSON", tmp_path / "results" / "smoke.json")
    monkeypatch.setattr(pod, "TIMING_JSON", tmp_path / "results" / "timing.json")
    monkeypatch.setattr(pod, "load_prompts", lambda path=None: rows)
    monkeypatch.setattr(pod, "NLL_RANGE", (0.5, 10.0))  # a random tiny model: ~ln(64) nats

    def fake_vllm(cmd, env=None):
        assert cmd[-2:] == ["gen", "--smoke"]
        ctx = pod.smoke_ctx(rows)
        fake_gen(ctx)
        pod.write_json(ctx.results / "run_gen.json",
                       {"phases_s": {"generate": 2.0, "engine_load": 30.0}, "out_tok_s": 100.0})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pod, "SUBPROC", fake_vllm)
    return rows


def test_smoke_passes_and_sets_settings(smoke_env, monkeypatch):
    monkeypatch.setenv("P42_DEADLINE_EPOCH", str(time.time() + 1e7))
    assert pod.step_smoke(SimpleNamespace()) == 0
    s = json.loads(pod.SMOKE_JSON.read_text())
    assert s["pass"] and s["gates_pass"] and all(s["gates"].values())
    assert s["settings"] == {"attn": "sdpa", "resp_tokens": 128, "grad_ref_n": 20_000}
    assert s["attn_check"]["n"] > 0 and s["attn_check"]["max_abs_diff"] < 1e-4
    assert s["sae_on"] and s["cut"] == "none"
    assert s["grad_loss_vs_feats_max_abs"] < 1e-4
    assert (pod.RESULTS / "smoke" / "scores.csv").is_file()
    assert pod.settings()["attn"] == "sdpa"


def test_smoke_over_budget_rc3(smoke_env, monkeypatch):
    monkeypatch.setenv("P42_DEADLINE_EPOCH", str(time.time() + 60))
    assert pod.step_smoke(SimpleNamespace()) == 3
    s = json.loads(pod.SMOKE_JSON.read_text())
    assert not s["pass"] and s["gates_pass"] and not s["projection_ok"]
    assert len(s["projection_tried"]) == 3
    with pytest.raises(pod.StepFailed):
        pod.settings()


def test_smoke_gate_failure_rc1(smoke_env, monkeypatch):
    monkeypatch.setattr(pod, "NLL_RANGE", (0.5, 4.0))
    monkeypatch.setenv("P42_DEADLINE_EPOCH", str(time.time() + 1e7))
    assert pod.step_smoke(SimpleNamespace()) == 1
    s = json.loads(pod.SMOKE_JSON.read_text())
    assert not s["gates"]["nll_id_ref_in_range"] and not s["pass"]
