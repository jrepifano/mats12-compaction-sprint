"""P39 session on a tiny random Llama (CPU) with the real Qwen3.6-27B tokenizer: gradients against
finite differences, delta-patch semantics, and an end-to-end run with the gates opened."""

from __future__ import annotations

import gzip
import json
from collections import Counter

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from p36 import model as MD
from p36 import session as P36S
from p36 import stimuli as S
from p39 import grad as G
from p39 import session as SE

D = 32
NL = 6
LAYER = 3


@pytest.fixture(scope="module")
def tok():
    try:
        return transformers.AutoTokenizer.from_pretrained(
            P36S.MODEL_ID, revision=P36S.MODEL_REV, local_files_only=True
        )
    except OSError:
        pytest.skip("Qwen3.6-27B tokenizer not in the local HF cache")


@pytest.fixture(scope="module")
def bk(tok):
    from transformers import LlamaConfig, LlamaForCausalLM
    from wsbench.produce.backend import Backend

    torch.manual_seed(0)
    cfg = LlamaConfig(vocab_size=len(tok), hidden_size=D, intermediate_size=64,
                      num_hidden_layers=NL, num_attention_heads=4, num_key_value_heads=4,
                      max_position_embeddings=128)  # fmt: skip
    m = LlamaForCausalLM(cfg).eval()
    return Backend(model=m, tokenizer=tok, device="cpu", model_id="tiny")


def _render(tok, n=4):
    _, sents = S.load_stimuli(P36S.HERE / "data" / "stimuli.json")
    rs = [S.render(tok, s, "agent") for s in sents[:n]]
    pos = [[r["ent"][e] for e in sorted((s.agent, s.patient))] for r, s in zip(rs, sents[:n])]
    ans = [[S.answer_token(tok, s.agent), S.answer_token(tok, s.patient)] for s in sents[:n]]
    return sents[:n], rs, pos, ans


def test_ld_grad_matches_finite_differences(bk, tok):
    _, rs, pos, ans = _render(tok)
    ids = [r["ids"] for r in rs if r["n"] == rs[0]["n"]]
    k = len(ids)
    g = G.ld_grad(bk, ids, pos[:k], LAYER, ans[:k])
    # the leaf equals the captured state: the forward is unchanged
    h = MD.capture(bk, ids, pos[:k], [LAYER])[LAYER]
    torch.testing.assert_close(g["h"], h.float())
    base = MD.forward_patched(bk, ids, None, answer_ids=ans[:k])["ans"]
    torch.testing.assert_close(g["ld"], base[:, 0] - base[:, 1], atol=1e-4, rtol=1e-4)
    gen = torch.Generator().manual_seed(0)
    for _ in range(3):
        v = torch.randn(g["grad"].shape, generator=gen)
        eps = 1e-3
        lp = MD.forward_patched(
            bk, ids, {LAYER: (torch.tensor(pos[:k]), g["h"] + eps * v)}, answer_ids=ans[:k]
        )["ans"]
        lm = MD.forward_patched(
            bk, ids, {LAYER: (torch.tensor(pos[:k]), g["h"] - eps * v)}, answer_ids=ans[:k]
        )["ans"]
        fd = ((lp[:, 0] - lp[:, 1]) - (lm[:, 0] - lm[:, 1])) / (2 * eps)
        pred = (g["grad"] * v).sum((-1, -2))
        torch.testing.assert_close(fd, pred, atol=2e-3, rtol=2e-2)
    assert all(p.grad is None for p in bk.model.parameters())


def test_capture_all_shape(bk, tok):
    _, rs, _, _ = _render(tok)
    ids = [r["ids"] for r in rs if r["n"] == rs[0]["n"]]
    out = G.capture_all(bk, ids, LAYER, 4)
    assert out.shape == (len(ids) * (rs[0]["n"] - 4), D)


@pytest.fixture()
def opened(monkeypatch):
    def all_pairs(base, dmap):
        return {(sid, q) for (sid, q) in base if sid in dmap and (dmap[sid], q) in base}

    monkeypatch.setattr(P36S, "valid_pairs", all_pairs)
    monkeypatch.setattr(SE, "FD_MIN_R", -1.0)  # a random tiny model is too nonlinear to gate on
    monkeypatch.setattr(SE.Session, "time_left", lambda self: 1e9)


def test_end_to_end_tiny(bk, tmp_path, opened):
    jl = tmp_path / "jlens.pt"
    torch.save({"J": torch.randn(NL, D, D) * 0.2 + torch.eye(D)}, jl)
    cfg = SE.Config(layer=LAYER, n_layers=NL, batch=16, pursuit_batch=8, grad_batch=8,
                    k_sweep=(2, 3, 6), k1=3, n_rand=2, n_dose=1, limit_items=2,
                    out=tmp_path / "results", jlens_path=str(jl), gates=False)  # fmt: skip
    old_k1 = SE.K1
    SE.K1 = 3
    try:
        sess = SE.Session(bk, cfg)
        assert Counter(s.split for s in sess.sents) == {
            "train": 8,
            "held_entity": 8,
            "held_verb": 8,
        }
        rc = SE.run_all(sess)
    finally:
        SE.K1 = old_k1
    assert rc == 0
    st = json.loads((cfg.out / "status.json").read_text())
    assert st["why"] == "complete", st
    assert st["fit"]["r_star"] >= 1 and st["s_bar"] > 0  # the random tiny lens can exceed 1
    rows = {}
    for ph in ("anchors", "primary", "secondary", "crossvoice"):
        rows[ph] = [
            json.loads(x) for x in (cfg.out / "rows" / f"{ph}.jsonl").read_text().splitlines()
        ]
    c = Counter((r["dict"], r["kind"]) for ph in rows.values() for r in ph)
    n_all, n_held = 24 * 2, 16 * 2  # sentences x questions
    assert c[("full", "twin")] == n_all and c[("jl25", "twin")] == n_all
    assert c[("iso_m1", "twin")] == n_all and c[("cov_m0", "twin")] == n_all
    assert c[("pca_m", "twin")] == n_held and c[("ax_order_m", "twin")] == n_held
    assert c[("r1d19", "twin")] == n_held and c[("ax_out_m", "twin")] == n_held
    assert c[("pca_m", "xsame")] == n_held and c[("full", "xtwin")] == n_all
    assert c[("jrot6", "twin")] == n_all and c[("iso_f0.005_0", "twin")] == n_all
    # self0 (delta = 0) reproduces the unpatched logits exactly; the full delta patch reproduces
    # P36's full patch (donor state written in)
    base = {(r["recip"], r["q"]): r
            for r in map(json.loads, (cfg.out / "rows" / "baseline.jsonl").read_text()
                         .splitlines())}  # fmt: skip
    for r in rows["anchors"]:
        if r["dict"] == "self0":
            assert r["logit_rec"] == base[(r["recip"], r["q"])]["logit_rec"]
    with gzip.open(cfg.out / "arm_stats.jsonl.gz", "rt") as fh:
        stats = [json.loads(x) for x in fh]
    by = {(s["arm"], s["kind"], s["recip"]): s for s in stats}
    for (arm, kind, _), s in by.items():
        if arm == "full":
            assert abs(s["share"] - 1) < 1e-4 and abs(s["gd_agent"] - s["gD_agent"]) < 1e-4
        if arm.endswith("_m") or arm.startswith(("iso_m", "cov_m", "r1d")):
            # matched: per-token norm equals the J-lens k = 25 change's norm
            j = by[("jl25", kind, s["recip"])]
            for a, b in zip(s["dnorm"], j["dnorm"], strict=True):
                assert abs(a - b) <= 1e-4 * max(1.0, b)
    assert (cfg.out / "states" / "p39_states.safetensors").exists()


def test_delta_patch_equals_full_patch(bk, tmp_path, opened):
    """op = delta with delta = h_d - h_r gives P36's full patch, bit for bit."""
    jl = tmp_path / "jlens.pt"
    torch.save({"J": torch.randn(NL, D, D) * 0.2 + torch.eye(D)}, jl)
    cfg = SE.Config(layer=LAYER, n_layers=NL, batch=16, limit_items=1, out=tmp_path / "r",
                    jlens_path=str(jl), gates=False)  # fmt: skip
    sess = SE.Session(bk, cfg)
    sess.renders()
    sess.capture()
    sess.baseline()
    sess.deltas[("full", "twin")] = sess.D("twin")
    jobs_d, jobs_f = [], []
    for sid, q in sorted(P36S.valid_pairs(sess.base, sess.dmaps["twin"])):
        j = {"recip": sid, "donor": sess.dmaps["twin"][sid], "q": q, "site": "entity",
             "layers": (LAYER,), "kind": "twin", "k": None}  # fmt: skip
        jobs_d.append({**j, "op": "delta", "dict": "full"})
        jobs_f.append({**j, "op": "full", "dict": None})
    a = sess.run_jobs(jobs_d, "t_delta")
    b = sess.run_jobs(jobs_f, "t_full")
    assert [(r["top1"], r["logit_rec"]) for r in a] == [(r["top1"], r["logit_rec"]) for r in b]


def test_analyze_on_tiny_run(bk, tmp_path, opened):
    from p39 import analyze as AN

    jl = tmp_path / "jlens.pt"
    torch.save({"J": torch.randn(NL, D, D) * 0.2 + torch.eye(D)}, jl)
    cfg = SE.Config(layer=LAYER, n_layers=NL, batch=16, pursuit_batch=8, grad_batch=8,
                    k_sweep=(2, 3, 25), k1=25, n_rand=3, n_dose=1, limit_items=2,
                    out=tmp_path / "results", jlens_path=str(jl), gates=False)  # fmt: skip
    assert SE.run_all(SE.Session(bk, cfg)) == 0
    s = AN.analyze(cfg.out)
    assert s["outcome"]["verdict"] != "incomplete", s["outcome"]
    assert s["j_alignment"]["verdict"] in ("below chance", "at chance", "above chance")
    held = s["arms"]["held"]
    assert held["twin:full"]["flip"]["n"] == 32 and abs(held["twin:full"]["attr"]["a"] - 1) < 1e-6
    assert len(s["families"]["held"]["twin:iso_m"]["draws"]) == 3
    assert set(s["manipulation"]) == {"order", "md", "pc1"}
    AN.write_md(s, cfg.out)
    assert "Outcome" in (cfg.out / "summary.md").read_text()
    try:
        AN.plot(s, cfg.out)
        assert (cfg.out / "dose_curves.png").exists()
    except ImportError:
        pass
