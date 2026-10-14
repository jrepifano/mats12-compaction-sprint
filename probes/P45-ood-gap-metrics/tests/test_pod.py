"""p45.pod on tiny random 32-layer Olmo3 models on the CPU (P42's CharTok and template helpers):
collection -> training -> scoring -> finalize end to end, F and sigma2 by hand, masking,
resumability, the smoke (gates, projection, rc 3), the projection and cut logic, the deadline,
input checks, run.sh (syntax, dry run, rc paths) and probe.toml."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import numpy as np
import pytest
import torch
from p45 import contracts as K
from p45 import pod
from p45 import sae as SAEmod

sys.path.insert(0, str(K.P42_DIR / "tests"))
from test_feats import RENDER, CharTok, tiny_model, tiny_sae, write_sae

torch.set_num_threads(min(4, torch.get_num_threads()))
P42K = K.P42K
PROBE = K.PROBE_DIR
TINY_SAE = {"n_latents": 32, "k": 4, "k_aux": 8, "batch": 64, "epochs": 3, "eval_every": 5,
            "warmup": 2, "lr": 1e-3}
WORDS = ["the", "a", "cat", "dog", "law", "court", "doctor", "fever", "pain", "contract",
         "lease", "tax", "code", "bug", "sky", "blue"]


# --- synthetic rows and fixtures -----------------------------------------------------------------


def synth_rows(seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    spec = {P42K.ID_REF: 40, P42K.ID_HELDOUT: 10, P42K.SFT_REF: 16, P42K.POOL: 12,
            P42K.PLANTED_MED: 5, P42K.PLANTED_LAW: 5, K.PLANTED_LAW2: 5, K.DOLMA: 20}
    rows = []
    for s, n in spec.items():
        for i in range(n):
            text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 12)))
            split = (["dev", "test"][i % 2] if s in K.SCORED_SETS
                     else ("heldout" if i % 4 == 0 else "train") if s == K.DOLMA else None)
            rows.append({"pid": f"{s}-{i:06d}", "set": s, "text": text, "n_tokens": len(text),
                         "split": split})
    return rows


@pytest.fixture
def tiny(monkeypatch, tmp_path):
    models = {"dpo": tiny_model(seed=0, layers=32, d=16, ff=24),
              "sft": tiny_model(seed=1, layers=32, d=16, ff=24)}
    calls = []

    def loader(name, attn):
        calls.append(name)
        return models[name]

    monkeypatch.setattr(pod, "LOADER", loader)
    monkeypatch.setattr(pod, "TOKENIZER", CharTok)
    monkeypatch.setattr(pod, "RENDER", RENDER)
    monkeypatch.setattr(pod, "DEVICE", "cpu")
    monkeypatch.setattr(pod, "WS", tmp_path / "ws")
    monkeypatch.setattr(pod, "RESULTS", tmp_path / "results")
    write_sae(tmp_path / "pubsae", tiny_sae(16, m=32), hook="model.layers.16")
    monkeypatch.setattr(pod, "PUB_SAE_DIR", str(tmp_path / "pubsae"))
    return {"models": models, "calls": calls}


def tiny_ctx(tmp_path, rows=None, **kw) -> pod.Ctx:
    base = {"buffer_tokens": 600, "heldout_tokens": 150, "thr_n": 20, "sft_ho_n": 4,
            "sae_cfg": dict(TINY_SAE), "retrain": False}  # tiny SAEs miss the dead gate
    return pod.Ctx("bulk", rows or synth_rows(), tmp_path / "work", tmp_path / "res",
                   **{**base, **kw})


def read_scores(path: Path) -> list[dict]:
    with path.open() as fh:
        r = csv.reader(fh)
        header = next(r)
        return [dict(zip(header, row, strict=True)) for row in r], header


@torch.no_grad()
def hidden(model, text: str, L: int) -> torch.Tensor:
    from p42.feats import encode_prompts, template_parts

    tok = CharTok()
    parts = template_parts(tok, RENDER)
    (ids, c0, c1), = encode_prompts(tok, [text], parts)
    out = model(input_ids=torch.tensor([ids]), output_hidden_states=True)
    return out.hidden_states[L][0, c0:c1].float()


def run_all(ctx):
    assert pod.step_model("dpo", ctx) == 0
    assert pod.step_model("sft", ctx) == 0
    assert pod.step_finalize(None, ctx) == 0


# --- forward ------------------------------------------------------------------------------------


def test_forward_content_early_stop_matches_full_forward(tiny):
    from p42.feats import encode_prompts, template_parts

    m = tiny["models"]["dpo"]
    tok = CharTok()
    parts = template_parts(tok, RENDER)
    texts = ["a much longer prompt than the other", "short", "mid one"]
    enc = encode_prompts(tok, texts, parts)
    X, segs = pod.forward_content(m, enc, (16, 17, 24), 0)
    for (a, b), t in zip(segs, texts, strict=True):
        for L in (16, 17, 24):
            assert torch.allclose(X[L][a:b], hidden(m, t, L), atol=1e-5)
    assert segs[-1][1] == sum(c1 - c0 for _, c0, c1 in enc)


def test_seg_mean_empty_segment_is_nan():
    v = torch.arange(6, dtype=torch.float32)
    out = pod.seg_mean(v, [(0, 2), (2, 2), (2, 6)])
    assert out[0] == pytest.approx(0.5) and torch.isnan(out[1]) and out[2] == pytest.approx(3.5)


# --- end to end ---------------------------------------------------------------------------------


def test_end_to_end_tiny(tiny, tmp_path):
    ctx = tiny_ctx(tmp_path)
    run_all(ctx)
    rows = ctx.rows
    out, header = read_scores(ctx.results / "scores.csv")
    assert tuple(header) == (*K.SCORE_ID_COLS, *K.SCORE_COLS)
    scored = [r for r in rows if r["set"] in K.SCORED_SETS]
    assert [o["pid"] for o in out] == [r["pid"] for r in scored]
    assert {o["set"] for o in out} == set(K.SCORED_SETS)
    assert all(o["split"] in ("dev", "test") for o in out)
    for c in K.SCORE_COLS:
        assert np.isfinite([float(o[c]) for o in out]).all(), c
    for o in out:
        f = {k: float(v) for k, v in o.items() if k in K.SCORE_COLS}
        for L in K.LAYERS:
            for a, src in (("pre", "pre"), ("sftdata", "sftdata"), ("model", "sftmodel")):
                want = np.log(max(f[f"F_post_L{L}"], K.F_FLOOR)) - np.log(
                    max(f[f"F_{src}_L{L}"], K.F_FLOOR))
                assert f[f"dF_{a}_L{L}"] == pytest.approx(want, abs=1e-9)
        assert f["dF_pub_L16"] == pytest.approx(np.log(f["F_post_L16"]) - np.log(f["F_pub_L16"]))
        assert f["len_tokens"] == next(r["n_tokens"] for r in rows if r["pid"] == o["pid"])
    # buffers: fp16, finite; Bernoulli over-draw then an exact uniform subset of min(target, n)
    for model in ("dpo", "sft"):
        for job in pod.jobs_for(model):
            rec = json.loads((ctx.work / model / f"job_{job.name}.json").read_text())
            for L in K.LAYERS:
                ns, nk = rec["n_sampled"][str(L)], rec["n_kept"][str(L)]
                buf = np.load(pod.buf_path(ctx, model, job.arm, job.kind, L))
                assert buf.dtype == np.float16 and ns <= buf.shape[0] == rec["capacity"]
                assert np.isfinite(buf[:ns]).all() and nk > 0
                assert nk == min(rec["target"], ns) == rec["n_fill"][str(L)]
                assert rec["n_eligible"][str(L)] >= ns and rec["capped"][str(L)] == 0
                want = rec["p"] * rec["n_eligible"][str(L)]
                assert abs(ns - want) < 5 * np.sqrt(want) + 2
                sel = np.load(pod.sel_path(ctx, model, job.arm, job.kind, L))
                assert len(sel) == nk and (np.diff(sel) > 0).all() and sel[-1] < ns
                got = pod.load_buf(ctx, model, job.arm, job.kind, L, "cpu").numpy()
                assert np.array_equal(got, buf[sel])
    rec = json.loads((ctx.work / "dpo" / "job_id_ref.json").read_text())
    assert rec["p"] == pytest.approx(min(1.0, 1.1 * 600 / rec["n_content"]))
    assert rec["n_sampled"]["16"] > 600 == rec["n_kept"]["16"]  # over-drawn, then subsampled
    # sft_ref held-out subset disjoint from training; dolma split respected
    R = pod.roles(ctx)
    assert len(R["sft_ho"]) == 4 and not set(R["sft_ho"]) & set(R["sft_train"])
    assert all(rows[i]["split"] == "heldout" for i in R["dolma_ho"])
    # SAE checkpoints, curves with gates and cross-distribution FVU
    for arm in K.ARMS:
        for L in K.LAYERS:
            assert pod.sae_stem(ctx, arm, L).with_suffix(".safetensors").is_file()
            c = json.loads(pod.curves_path(ctx, arm, L).read_text())
            assert {"gate", "cross_fvu_heldout", "final", "loss", "fp"} <= set(c)
            assert arm in c["cross_fvu_heldout"]
    # union PCA: rows id_ref, pool, then the planted sets
    pids = (ctx.results / "pca_union" / "pids.txt").read_text().split()
    sets = [p.rsplit("-", 1)[0] for p in pids]
    order = [P42K.ID_REF, P42K.POOL, P42K.PLANTED_MED, P42K.PLANTED_LAW, K.PLANTED_LAW2]
    assert sets == sorted(sets, key=order.index) and set(sets) == set(order)
    Z = np.load(ctx.results / "pca_union" / "dpo_p_L24.npy")
    assert Z.shape[0] == len(pids) and Z.dtype == np.float16
    # maha_L24 against p42.scoring on the stored means
    from p42 import scoring as Sc

    means = np.load(pod.means_path(ctx))
    ref = R["id_ref"]
    want = Sc.Mahalanobis(means[ref], "cpu").score(means[R["scored"]])
    assert np.allclose([float(o["maha_L24"]) for o in out], want, rtol=1e-6)
    # norm_L24 = mean content-token norm, prompt-mean L24 = mean of the content tokens
    o = out[3]
    r = next(r for r in rows if r["pid"] == o["pid"])
    h = hidden(tiny["models"]["dpo"], r["text"], 24)
    assert float(o["norm_L24"]) == pytest.approx(float(h.norm(dim=1).mean()), rel=1e-4)
    i = next(i for i, x in enumerate(rows) if x["pid"] == o["pid"])
    assert np.allclose(means[i], h.mean(0).numpy(), atol=1e-4)


def test_sigma2_and_F_by_hand(tiny, tmp_path):
    ctx = tiny_ctx(tmp_path)
    run_all(ctx)
    m = tiny["models"]["dpo"]
    rows = ctx.rows
    R = pod.roles(ctx)
    thr = json.loads((ctx.work / "dpo" / "thresholds.json").read_text())
    rec = json.loads((ctx.results / "run_dpo.json").read_text())
    L = 16
    med = thr["median_norm"][str(L)]
    # thresholds: the median over the first thr_n id_ref prompts
    hs = torch.cat([hidden(m, rows[i]["text"], L) for i in R["id_ref"][: ctx.thr_n]])
    assert med == pytest.approx(float(hs.norm(dim=1).median()), rel=1e-5)
    # sigma2 over every unmasked id_ref content token
    X = torch.cat([hidden(m, rows[i]["text"], L) for i in R["id_ref"]]).double()
    X = X[X.norm(dim=1) <= K.MASK_NORM_MULT * med]
    want = float(((X - X.mean(0)) ** 2).sum(1).mean())
    assert rec["sigma2"][str(L)] == pytest.approx(want, rel=1e-5)
    # F_post_L16 of one scored prompt
    z = np.load(ctx.results / "partial_dpo.npz")
    j = 5
    x = hidden(m, rows[int(z["row"][j])]["text"], L)
    sae = SAEmod.TopKSAE.load(pod.sae_stem(ctx, "post", L), "cpu")
    se = sae.sq_err(x)
    keep = x.norm(dim=1) <= K.MASK_NORM_MULT * med
    assert z["F_post_L16"][j] == pytest.approx(float(se[keep].mean()) / want, rel=1e-4)
    # F_pub_L16: per-token squared error of the public SAE on hs 17 over the GLOBAL sigma2_pub
    from p42.feats import SAE

    pub, _ = SAE.load(pod.PUB_SAE_DIR, "cpu", "layer_16")
    x17 = hidden(m, rows[int(z["row"][j])]["text"], 17)
    k17 = x17.norm(dim=1) <= K.MASK_NORM_MULT * thr["median_norm"]["17"]
    e17 = ((x17 - pub.decode(pub.encode(x17))) ** 2).sum(1)
    assert z["F_pub_L16"][j] == pytest.approx(float(e17[k17].mean()) / rec["sigma2"]["17"],
                                              rel=1e-4)


def test_masking_applied(tiny, tmp_path, monkeypatch):
    """With the mask at 1 x median about half the tokens are dropped: buffers hold only kept
    rows, F averages kept tokens only, masked_* is the per-prompt dropped share."""
    monkeypatch.setattr(SAEmod, "mask_keep", lambda n, med, mult=1.0: n <= mult * med)
    ctx = tiny_ctx(tmp_path)
    run_all(ctx)
    thr = json.loads((ctx.work / "dpo" / "thresholds.json").read_text())
    for L in K.LAYERS:
        med = thr["median_norm"][str(L)]
        rec = json.loads((ctx.work / "dpo" / "job_id_ref.json").read_text())
        n = rec["n_sampled"][str(L)]
        buf = np.load(pod.buf_path(ctx, "dpo", "post", "train", L))[:n].astype(np.float32)
        assert (np.linalg.norm(buf, axis=1) <= med * 1.002).all()
        assert rec["masked"][str(L)] > 0
    z = np.load(ctx.results / "partial_dpo.npz")
    m = tiny["models"]["dpo"]
    sig = json.loads((ctx.results / "run_dpo.json").read_text())["sigma2"]
    sae = SAEmod.TopKSAE.load(pod.sae_stem(ctx, "pre", 24), "cpu")
    med = thr["median_norm"]["24"]
    checked = 0
    for j, row in enumerate(z["row"]):
        x = hidden(m, ctx.rows[int(row)]["text"], 24)
        keep = x.norm(dim=1) <= med
        assert z["masked_dpo_L24"][j] == pytest.approx(1 - float(keep.float().mean()), abs=1e-6)
        if keep.any():
            want = float(sae.sq_err(x)[keep].mean()) / sig["24"]
            assert z["F_pre_L24"][j] == pytest.approx(want, rel=1e-4)
            checked += 1
        else:
            assert np.isnan(z["F_pre_L24"][j])
    assert checked > 10 and 0.2 < float(np.nanmean(z["masked_dpo_L24"])) < 0.8


def test_resumable_rerun_skips_everything(tiny, tmp_path):
    ctx = tiny_ctx(tmp_path)
    run_all(ctx)
    n_calls = len(tiny["calls"])
    assert n_calls == 4  # each model: once for collection, once for scoring
    before = (ctx.results / "scores.csv").read_bytes()
    assert pod.step_model("dpo", ctx) == 0 and pod.step_model("sft", ctx) == 0
    assert len(tiny["calls"]) == n_calls  # no model loaded
    rec = json.loads((ctx.results / "run_dpo.json").read_text())
    assert all(v["skipped"] for v in rec["train"].values()) and rec["score"]["skipped"]
    assert pod.step_finalize(None, ctx) == 0
    assert (ctx.results / "scores.csv").read_bytes() == before


def test_settings_change_resets_collection(tiny, tmp_path):
    ctx = tiny_ctx(tmp_path)
    assert pod.step_model("sft", ctx) == 0
    ctx2 = tiny_ctx(tmp_path, buffer_tokens=500)
    assert pod.step_model("sft", ctx2) == 0
    rec = json.loads((ctx.results / "run_sft.json").read_text())
    assert rec["stale_reset"] and "buffer_tokens" in rec["stale_reset"]
    assert not any(v["skipped"] for v in rec["train"].values())
    with pytest.raises(pod.StepFailed, match="partial_dpo missing"):
        pod.finalize(ctx2, pod.Timer())


def test_layer_cut_writes_nan_L16(tiny, tmp_path):
    ctx = tiny_ctx(tmp_path, layers=(24,), pub=False)
    run_all(ctx)
    out, _ = read_scores(ctx.results / "scores.csv")
    assert all(o["F_post_L16"] == "nan" and o["dF_pub_L16"] == "nan" for o in out)
    assert np.isfinite([float(o["dF_model_L24"]) for o in out]).all()
    assert not pod.sae_stem(ctx, "post", 16).with_suffix(".safetensors").exists()


def test_gate_and_retrain(tiny, tmp_path, monkeypatch):
    ctx = tiny_ctx(tmp_path, retrain=True)
    real = SAEmod.train_sae
    seen = []

    def fake(buf, ho, cfg, dev, sigma2, log=print):
        sae, cur = real(buf, ho, cfg, dev, sigma2, log)
        seen.append(cfg.n_latents)
        cur["final"]["dead_frac"] = 0.9 if cfg.n_latents == TINY_SAE["n_latents"] else 0.0
        return sae, cur

    monkeypatch.setattr(SAEmod, "train_sae", fake)
    monkeypatch.setattr(K, "N_LATENTS_FALLBACK", 16)
    assert pod.step_model("sft", ctx) == 0
    c = json.loads(pod.curves_path(ctx, "sftmodel", 24).read_text())
    assert c["retrained"] and not c["gate_failed_twice"] and c["cfg"]["n_latents"] == 16
    assert c["first_attempt"]["gate"]["dead_ok"] is False
    assert seen == [32, 16, 32, 16]
    assert pod.gate({"fvu_train": 0.1, "fvu_heldout": 0.2, "dead_frac": 0.0})["pass"] is False
    assert pod.gate({"fvu_train": 0.1, "fvu_heldout": 0.12, "dead_frac": 0.3})["pass"] is True


def test_retrain_failing_twice_stops_with_diagnostics(tiny, tmp_path, monkeypatch):
    ctx = tiny_ctx(tmp_path, retrain=True)
    real = SAEmod.train_sae

    def fake(buf, ho, cfg, dev, sigma2, log=print):
        sae, cur = real(buf, ho, cfg, dev, sigma2, log)
        cur["final"]["dead_frac"] = 0.9
        return sae, cur

    monkeypatch.setattr(SAEmod, "train_sae", fake)
    monkeypatch.setattr(K, "N_LATENTS_FALLBACK", 16)
    with pytest.raises(pod.StepFailed, match="failed after the retrain"):
        pod.step_model("sft", ctx)
    c = json.loads(pod.curves_path(ctx, "sftmodel", 16).read_text())
    assert c["gate_failed_twice"] and c["cfg"]["n_latents"] == 16 and c["loss"]
    a1 = json.loads(pod.attempt_path(ctx, "sftmodel", 16, 1).read_text())
    assert a1["cfg"]["n_latents"] == 32 and a1["loss"] and not a1["gate"]["pass"]
    assert not pod.sae_stem(ctx, "sftmodel", 16).with_suffix(".safetensors").exists()
    assert not (ctx.results / "partial_sft.npz").exists()  # no scoring after the failure
    monkeypatch.setattr(pod, "STEPS", {**pod.STEPS, "sft": lambda a: pod.step_model("sft", ctx)})
    assert pod.main(["sft"]) == 1  # and run.sh maps it to rc 15 (test_run_sh_failed_step_...)


PROJ = {"collect": {"dpo": 10.0, "sft": 10.0}, "score": {"dpo": 20.0, "sft": 20.0},
        "per_sae": 100.0, "per_sae_fallback": 50.0, "finalize": 5.0}


def test_time_guard_stops_before_training(tiny, tmp_path, monkeypatch):
    ctx = tiny_ctx(tmp_path, proj=PROJ)
    want = (5 + 2 * 10 + 2 * 20 + 8 * 150) * pod.PROJ_MARGIN + pod.MANIFEST_S
    assert pod.remaining_s(ctx) == pytest.approx(want)
    monkeypatch.setenv("P45_DEADLINE_EPOCH", str(time.time() + want - 100))
    with pytest.raises(pod.OutOfTime, match="before training sftmodel_L16"):
        pod.step_model("sft", ctx)
    assert (ctx.work / "sft" / "job_id_ref.json").is_file()  # collection done, nothing trained
    assert not pod.curves_path(ctx, "sftmodel", 16).exists()
    monkeypatch.setattr(pod, "STEPS", {**pod.STEPS, "sft": lambda a: pod.step_model("sft", ctx)})
    assert pod.main(["sft"]) == 5
    monkeypatch.setenv("P45_DEADLINE_EPOCH", str(time.time() + 1e6))
    run_all(ctx)
    assert pod.remaining_s(ctx) == pytest.approx(5 * pod.PROJ_MARGIN + pod.MANIFEST_S)
    monkeypatch.setenv("P45_DEADLINE_EPOCH", str(time.time() + 10))
    with pytest.raises(pod.OutOfTime, match="before finalize"):
        pod.step_finalize(None, ctx)
    assert pod.remaining_s(tiny_ctx(tmp_path)) is None  # no smoke projection: no guard


def test_fingerprint_covers_p42_code_and_template(tiny, tmp_path, monkeypatch):
    ctx = tiny_ctx(tmp_path)
    fp = pod.collect_fp(ctx, "dpo")
    assert fp["p42_code_sha"] == pod.p42_code_sha() and fp["template_sha"]
    monkeypatch.setattr(pod, "RENDER", lambda c, tok=None: f"<s>other\n<u>{c}<e>\n<a>")
    assert pod.collect_fp(ctx, "dpo")["template_sha"] != fp["template_sha"]


# --- smoke ---------------------------------------------------------------------------------------


@pytest.fixture
def smoke_env(tiny, tmp_path, monkeypatch):
    sizes = {P42K.ID_REF: 30, P42K.SFT_REF: 12, K.DOLMA: 16, P42K.ID_HELDOUT: 8, P42K.POOL: 8,
             P42K.PLANTED_MED: 4, P42K.PLANTED_LAW: 4, K.PLANTED_LAW2: 4}
    monkeypatch.setattr(pod, "SMOKE_SIZES", sizes)
    monkeypatch.setattr(pod, "SMOKE_BUFFER", 800)
    monkeypatch.setattr(pod, "SMOKE_HELDOUT", 200)
    monkeypatch.setattr(pod, "SMOKE_SFT_HO", 3)
    monkeypatch.setattr(pod, "SMOKE_STEPS", 40)
    monkeypatch.setattr(pod, "SMOKE_EVAL_EVERY", 10)
    monkeypatch.setattr(SAEmod, "CURVE_EVERY", 10)  # several curve points in 40 steps
    monkeypatch.setattr(pod, "SMOKE_SAE_EXTRA", {k: v for k, v in TINY_SAE.items()
                                                 if k not in ("epochs", "eval_every")})
    return tmp_path


def test_smoke_passes_and_sets_settings(smoke_env, monkeypatch):
    monkeypatch.setenv("P45_DEADLINE_EPOCH", str(time.time() + 1e7))
    rows = synth_rows()
    assert pod.step_smoke(None, rows) == 0
    s = json.loads((pod.RESULTS / "smoke.json").read_text())
    assert s["pass"] and all(s["gates"].values()), s["gates"]
    assert s["settings"] == {"attn": "sdpa", "buffer_tokens": K.BUFFER_TOKENS,
                             "layers": list(K.LAYERS), "pub": True}
    assert s["cut"] == "none" and s["n_smoke"] == 86
    assert (pod.RESULTS / "smoke" / "scores.csv").is_file()
    assert not list((pod.RESULTS / "smoke" / "sae").glob("*.safetensors"))
    assert len(list((pod.RESULTS / "smoke" / "sae").glob("*.json"))) == 8
    assert set(s["rates"]) >= {"collect_s_per_tok", "ms_per_step", "io_s"}
    assert pod.settings() == s["settings"]
    assert s["components"]["n_sae"] == 8 and s["identity"] == pod.identity(rows)
    c = pod.with_settings(pod.Ctx("bulk", rows, pod.WS / "w", pod.RESULTS))
    assert c.proj == s["components"] and c.layers == tuple(K.LAYERS)
    other = [dict(r, text=r["text"] + "!") if i == 0 else r for i, r in enumerate(rows)]
    with pytest.raises(pod.StepFailed, match="other inputs/code"):
        pod.with_settings(pod.Ctx("bulk", other, pod.WS / "w", pod.RESULTS))


def test_smoke_over_budget_rc3(smoke_env, monkeypatch):
    monkeypatch.setenv("P45_DEADLINE_EPOCH", str(time.time() + 1))
    assert pod.step_smoke(None, synth_rows()) == 3
    s = json.loads((pod.RESULTS / "smoke.json").read_text())
    assert not s["pass"] and s["gates_pass"] and not s["projection_ok"]
    assert len(s["projection_tried"]) == 3
    with pytest.raises(pod.StepFailed, match="no passing"):
        pod.settings()


def test_smoke_gate_failure_rc1(smoke_env, monkeypatch):
    monkeypatch.setenv("P45_DEADLINE_EPOCH", str(time.time() + 1e7))
    monkeypatch.setattr(pod, "RELOAD_TOL", -1.0)
    assert pod.step_smoke(None, synth_rows()) == 1
    s = json.loads((pod.RESULTS / "smoke.json").read_text())
    assert not s["pass"] and s["gates"]["reload_agrees"] is False


# --- projection, cuts, deadline -------------------------------------------------------------------


def rates(**kw) -> pod.Rates:
    base = {"collect_s_per_tok": {"dpo": 4e-5, "sft": 4e-5},
            "score_s_per_tok": {"dpo": 5e-5, "sft": 4.5e-5}, "load_s": {"dpo": 10, "sft": 10},
            "ms_per_step": 30.0, "eval_s_per_tok": 1e-7, "other_s_per_tok": 1e-6,
            "buf_load_s_per_tok": 1e-6, "io_s": 5.0, "finalize_s": 300.0}
    return pod.Rates(**{**base, **kw})


COUNTS = {"collect": {"dpo": 22_000_000, "sft": 15_000_000},
          "score": {"dpo": 7_500_000, "sft": 7_500_000}}


def test_projection_math():
    r = rates()
    ph = pod.project(r, COUNTS, K.BUFFER_TOKENS, K.LAYERS)
    steps = -(-K.BUFFER_TOKENS // K.BATCH_TOKENS) * K.EPOCHS
    assert ph["steps_per_sae"] == steps == 611 * 8 and ph["n_sae"] == 8
    per_sae = (steps * 0.03 + (steps // K.EVAL_EVERY + 1) * K.HELDOUT_TOKENS * 1e-7
               + K.BUFFER_TOKENS * 2e-6 + 5.0)
    fw = {m: 20 + 4e-5 * COUNTS["collect"][m] + r.score_s_per_tok[m] * COUNTS["score"][m]
          for m in ("dpo", "sft")}
    fallback = (steps * 0.03 + (steps // K.EVAL_EVERY + 1) * K.HELDOUT_TOKENS * 1e-7) * 0.5 \
        + K.BUFFER_TOKENS * 1e-6
    core = fw["dpo"] + fw["sft"] + 8 * per_sae + 8 * fallback + 300
    want = core * pod.PROJ_MARGIN + pod.MANIFEST_S + 3 * pod.PROC_OVERHEAD_S
    assert ph["total"] == pytest.approx(want)
    ph24 = pod.project(r, COUNTS, K.BUFFER_TOKENS, (24,))
    assert ph24["sae_training"] == pytest.approx(ph["sae_training"] / 2)
    assert ph["fallback_reserve"] == pytest.approx(8 * fallback)
    # buffer writes priced apart from the forwards
    phw = pod.project(rates(write_s_per_byte=1e-9), COUNTS, K.BUFFER_TOKENS, K.LAYERS)
    rows = 1.1 * (3 * K.BUFFER_TOKENS + 3 * K.HELDOUT_TOKENS + K.BUFFER_TOKENS + K.HELDOUT_TOKENS)
    assert phw["total"] - ph["total"] == pytest.approx(
        rows * 2 * 4096 * 2 * 1e-9 * pod.PROJ_MARGIN)


def test_choose_settings_cut_order():
    r = rates()
    tot = [pod.project(r, COUNTS, b, L)["total"] for b, L in (
        (K.BUFFER_TOKENS, K.LAYERS), (K.BUFFER_TOKENS_CUT, K.LAYERS), (K.BUFFER_TOKENS_CUT, (24,)))]
    assert tot[0] > tot[1] > tot[2]
    assert pod.choose_settings(r, COUNTS, None)["cut"] == "none"
    ch = pod.choose_settings(r, COUNTS, tot[0] / 0.8 + 1)
    assert ch["cut"] == "none" and ch["layers"] == [16, 24] and ch["pub"]
    ch = pod.choose_settings(r, COUNTS, tot[1] / 0.8 + 1)
    assert ch["buffer_tokens"] == K.BUFFER_TOKENS_CUT and ch["layers"] == [16, 24]
    ch = pod.choose_settings(r, COUNTS, tot[2] / 0.8 + 1)
    assert ch["layers"] == [24] and ch["pub"] is False and ch["buffer_tokens"] == 1_500_000
    ch = pod.choose_settings(r, COUNTS, tot[2] / 0.8 - 1)
    assert not ch["ok"] and len(ch["tried"]) == 3


def test_bulk_counts():
    rows = synth_rows()
    c = pod.bulk_counts(rows, template_tokens=10, thr_n=5)
    L = lambda r: r["n_tokens"] + 10
    idr = [r for r in rows if r["set"] == P42K.ID_REF]
    want_sft = sum(L(r) for r in idr[:5]) + sum(L(r) for r in rows if r["set"] in (
        P42K.ID_REF, P42K.ID_HELDOUT))
    assert c["collect"]["sft"] == want_sft
    assert c["collect"]["dpo"] == want_sft + sum(L(r) for r in rows if r["set"] in (
        P42K.SFT_REF, K.DOLMA))
    assert c["score"]["dpo"] == sum(L(r) for r in rows if r["set"] in K.SCORED_SETS)


def test_deadline_sources(monkeypatch, tmp_path):
    for v in ("P45_DEADLINE_EPOCH", "P45_T0", "P45_BUDGET_S"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(pod, "KILLSWITCH_DEADLINE", tmp_path / "deadline")
    assert pod.deadline_epoch() == (None, "none")
    monkeypatch.setenv("P45_T0", "1000")
    gpu_h = tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"]
    assert pod.deadline_epoch()[0] == 1000 + gpu_h * 3600 - 900
    monkeypatch.setenv("P45_BUDGET_S", "500")
    assert pod.deadline_epoch()[0] == 1500
    (tmp_path / "deadline").write_text("50000\n")
    assert pod.deadline_epoch()[0] == 50000 - pod.SALVAGE_S
    monkeypatch.setenv("P45_DEADLINE_EPOCH", "7")
    assert pod.deadline_epoch()[0] == 7


def test_settings_skip_smoke(monkeypatch, tmp_path):
    monkeypatch.setattr(pod, "RESULTS", tmp_path)
    monkeypatch.delenv("P45_SKIP_SMOKE", raising=False)
    with pytest.raises(pod.StepFailed):
        pod.settings()
    monkeypatch.setenv("P45_SKIP_SMOKE", "1")
    assert pod.settings() == pod.default_settings()
    c = pod.with_settings(pod.Ctx("bulk", [], tmp_path, tmp_path),
                          {"attn": "sdpa", "buffer_tokens": 5, "layers": [24], "pub": False})
    assert (c.buffer_tokens, c.layers, c.pub) == (5, (24,), False)


# --- inputs -------------------------------------------------------------------------------------


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_verify_inputs(monkeypatch, tmp_path):
    pr, law, dol, meta = (tmp_path / n for n in ("prompts.jsonl", "planted_law2.jsonl",
                                                 "dolma_chunks.jsonl", "data.meta.json"))
    pr.write_text("p\n")
    law.write_text("l\n")
    dol.write_text("d\n")
    monkeypatch.setattr(K, "P42_PROMPTS_SHA", _sha(pr))
    meta.write_text(json.dumps({"law2": {"sha256": _sha(law)}, "dolma": {"sha256": _sha(dol)}}))
    out = pod.verify_inputs(pr, law, dol, meta)
    assert out["planted_law2.jsonl"] == _sha(law)
    meta.write_text(json.dumps({"outputs": {"planted_law2.jsonl": {"sha256": _sha(law)},
                                            "dolma_chunks.jsonl": _sha(dol)}}))
    pod.verify_inputs(pr, law, dol, meta)
    meta.write_text(json.dumps({"law2_sha": _sha(law)}))
    with pytest.raises(pod.StepFailed, match="no sha256 for law2"):
        pod.verify_inputs(pr, law, dol, meta)
    meta.write_text(json.dumps({"law2": {"sha256": "0" * 64}, "dolma": {"sha256": _sha(dol)}}))
    with pytest.raises(pod.StepFailed, match="planted_law2.jsonl sha256"):
        pod.verify_inputs(pr, law, dol, meta)
    monkeypatch.setattr(K, "P42_PROMPTS_SHA", "x")
    with pytest.raises(pod.StepFailed, match="prompts.jsonl sha256"):
        pod.verify_inputs(pr, law, dol, meta)


def test_load_rows(monkeypatch, tmp_path):
    rows = synth_rows()
    p42rows = [dict(r, gen=False) for r in rows if r["set"] in P42K.SETS]
    law2 = [r for r in rows if r["set"] == K.PLANTED_LAW2]
    dolma = [{k: r[k] for k in ("pid", "text", "n_tokens", "split")} | {"shard": "s",
             "source": "x"} for r in rows if r["set"] == K.DOLMA]
    for name, rr in (("p.jsonl", p42rows), ("l.jsonl", law2), ("d.jsonl", dolma)):
        (tmp_path / name).write_text("".join(json.dumps(r) + "\n" for r in rr))
    monkeypatch.setattr(K, "P42_PROMPTS", tmp_path / "p.jsonl")
    monkeypatch.setattr(K, "LAW2_PATH", tmp_path / "l.jsonl")
    monkeypatch.setattr(K, "DOLMA_PATH", tmp_path / "d.jsonl")
    got = pod.load_rows()
    assert len(got) == len(rows) and {r["set"] for r in got} == {r["set"] for r in rows}
    assert all(r["set"] == K.DOLMA for r in got[-len(dolma):])
    (tmp_path / "l.jsonl").write_text(json.dumps(dict(law2[0], set="pool")) + "\n")
    with pytest.raises(pod.StepFailed, match="planted_law2"):
        pod.load_rows()


def test_smoke_subset_sizes():
    rows = synth_rows()
    sub = pod.smoke_subset(rows, {P42K.ID_REF: 7, K.DOLMA: 3, K.PLANTED_LAW2: 99})
    assert sum(r["set"] == P42K.ID_REF for r in sub) == 7
    assert sum(r["set"] == K.PLANTED_LAW2 for r in sub) == 5
    assert sub == pod.smoke_subset(rows, {P42K.ID_REF: 7, K.DOLMA: 3, K.PLANTED_LAW2: 99})
    assert sum(pod.SMOKE_SIZES.values()) == 1500


# --- run.sh and probe.toml ------------------------------------------------------------------------


def test_run_sh_syntax_and_dry_run():
    assert subprocess.run(["/bin/bash", "-n", str(PROBE / "run.sh")], check=False).returncode == 0
    r = subprocess.run(["/bin/bash", str(PROBE / "run.sh")], capture_output=True, text=True,
                       env={**os.environ, "P45_DRY_RUN": "1"}, check=False)
    assert r.returncode == 0, r.stderr
    for step in ("download", "smoke", "dpo", "sft", "finalize", "manifest"):
        assert f"{step}:" in r.stdout
    assert "PROBE_DONE" not in r.stdout and "guard: no new step after epoch" in r.stdout
    r = subprocess.run(["/bin/bash", str(PROBE / "run.sh")], capture_output=True, text=True,
                       env={**os.environ, "P45_DRY_RUN": "1", "P45_DEADLINE_EPOCH": "5000"},
                       check=False)
    assert "deadline=5000 (env P45_DEADLINE_EPOCH)" in r.stdout
    assert "no new step after epoch 4700" in r.stdout


def run_sh_sandbox(tmp_path, results_is_file=False, fake_rc=None):
    probe = tmp_path / "probes" / "P45-x"
    probe.mkdir(parents=True)
    shutil.copy(PROBE / "run.sh", probe / "run.sh")
    shutil.copy(PROBE / "probe.toml", probe / "probe.toml")
    for d, names in (("data/P42", ("prompts.jsonl",)),
                     ("data/P45", ("planted_law2.jsonl", "dolma_chunks.jsonl", "data.meta.json")),
                     ("probes/P42-ood-coverage-gaps/p42", ("pod.py",))):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        for n in names:
            (tmp_path / d / n).write_text("{}\n")
    if results_is_file:
        (probe / "results").write_text("not a directory")
    py = sys.executable
    if fake_rc is not None:
        py = str(tmp_path / "fakepy")
        Path(py).write_text(f"#!/bin/sh\nexit {fake_rc}\n")
        os.chmod(py, 0o755)
    env = {**os.environ, "P45_PEFT_PY": py}
    env.pop("P45_DRY_RUN", None)
    return probe, env


def _run(probe, env, **kw):
    r = subprocess.run(["/bin/bash", str(probe / "run.sh")], capture_output=True, text=True,
                       env={**env, **kw}, check=False, timeout=120)
    return r, r.stdout.strip().splitlines()


def test_run_sh_success_path(tmp_path):
    probe, env = run_sh_sandbox(tmp_path)
    (probe / "results").mkdir()
    (probe / "results" / "a.txt").write_text("x")
    r, lines = _run(probe, env, P45_STEPS=" ")
    assert r.returncode == 0 and lines[-1] == "PROBE_DONE rc=0"
    man = (probe / "results" / "MANIFEST.sha256").read_text()
    assert "./a.txt" in man and "MANIFEST" not in man


@pytest.mark.parametrize(("step", "rc"), [("download", 11), ("smoke", 12), ("dpo", 14),
                                          ("sft", 15), ("finalize", 16)])
def test_run_sh_failed_step_propagates(tmp_path, step, rc):
    probe, env = run_sh_sandbox(tmp_path, fake_rc=1)
    r, lines = _run(probe, env, P45_STEPS=f"{step} finalize")
    assert r.returncode == rc and lines[-1] == f"PROBE_DONE rc={rc}"
    assert r.stdout.count("python_rc=") == 1  # the chain stops at the failed step
    assert (probe / "results" / "MANIFEST.sha256").is_file()


def test_run_sh_smoke_over_budget_rc13(tmp_path):
    probe, env = run_sh_sandbox(tmp_path, fake_rc=3)
    r, lines = _run(probe, env, P45_STEPS="smoke dpo")
    assert r.returncode == 13 and lines[-1] == "PROBE_DONE rc=13"
    assert "step=dpo" not in r.stdout


def test_run_sh_missing_inputs_and_guard(tmp_path):
    probe, env = run_sh_sandbox(tmp_path)
    (tmp_path / "data" / "P45" / "dolma_chunks.jsonl").unlink()
    r, lines = _run(probe, env)
    assert r.returncode == 2 and lines[-1] == "PROBE_DONE rc=2"
    (tmp_path / "data" / "P45" / "dolma_chunks.jsonl").write_text("{}\n")
    r, lines = _run(probe, env, P45_DEADLINE_EPOCH=str(int(time.time()) + 100))
    assert r.returncode == 4 and lines[-1] == "PROBE_DONE rc=4" and "P45 guard" in r.stdout


def test_run_sh_deadline_from_killswitch_and_exported(tmp_path):
    probe, env = run_sh_sandbox(tmp_path)
    ks = tmp_path / "ks_deadline"
    ks.write_text(f"{int(time.time()) + 7200}\n")
    show = tmp_path / "show"
    show.write_text('#!/bin/sh\necho "DL=$P45_DEADLINE_EPOCH"\n')
    os.chmod(show, 0o755)
    env.pop("P45_DEADLINE_EPOCH", None)
    r, _ = _run(probe, {**env, "P45_PEFT_PY": str(show)}, P45_KS_DEADLINE=str(ks),
                    P45_STEPS="download")
    want = int(ks.read_text()) - 1200
    assert f"deadline={want} (killswitch" in r.stdout and f"DL={want}" in r.stdout
    assert r.returncode == 0


def test_run_sh_time_guard_rc5_maps_to_4(tmp_path):
    probe, env = run_sh_sandbox(tmp_path, fake_rc=5)
    r, lines = _run(probe, env, P45_STEPS="dpo sft")
    assert r.returncode == 4 and lines[-1] == "PROBE_DONE rc=4" and "step=sft" not in r.stdout


def test_run_sh_manifest_failure(tmp_path):
    probe, env = run_sh_sandbox(tmp_path, results_is_file=True)
    r, lines = _run(probe, env, P45_STEPS=" ")
    assert r.returncode == 18 and lines[-1] == "PROBE_DONE rc=18"


def test_probe_toml():
    t = tomllib.loads((PROBE / "probe.toml").read_text())
    assert (t["id"], t["hard_cap_usd"], t["gpu_limit_usd"], t["api_limit_usd"],
            t["reserve_usd"]) == ("P45", 30, 24, 6, 0)
    p = t["pod"]
    assert p["gpu_hours"] == 2.5 and p["setup"] == "eq0" and p["disk_gb"] == 300
    assert p["disk_gb"] >= p["model_gb"] + 40
    assert (p["model_repo"], p["model_revision"]) == (P42K.DPO_ID, P42K.DPO_REV)
    assert p["push"] == ["data/P42/prompts.jsonl", "data/P42/prompts.meta.json",
                         "data/P45/planted_law2.jsonl", "data/P45/dolma_chunks.jsonl",
                         "data/P45/data.meta.json", "probes/P42-ood-coverage-gaps/p42"]
    assert (K.REPO_ROOT / "probes/P42-ood-coverage-gaps/p42/feats.py").is_file()
    assert p["env"] == ["HF_TOKEN"] and p["gpu_check"] is True and "gpus" not in p
    assert p["min_download_mb_s"] == 150 and p["rerolls"] == 2 and p["poll_minutes"] == 5


def test_pins_match_registry():
    pod.P.check_pins()
