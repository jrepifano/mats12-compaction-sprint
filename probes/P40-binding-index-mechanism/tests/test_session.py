"""P40 session on a tiny random Llama (CPU) with the real Qwen3.6-27B tokenizer: patch and add
semantics, factorial patterns, vector targets, and an end-to-end run with the gates opened."""

from __future__ import annotations

import json
from collections import Counter

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from p40 import model as MD
from p40 import session as SE
from p40 import stimuli as S

D = 32
NL = 6


@pytest.fixture(scope="module")
def tok():
    try:
        return transformers.AutoTokenizer.from_pretrained(
            SE.MODEL_ID, revision=SE.MODEL_REV, local_files_only=True
        )
    except OSError:
        pytest.skip("Qwen3.6-27B tokenizer not in the local HF cache")


@pytest.fixture(scope="module")
def bk(tok):
    from transformers import LlamaConfig, LlamaForCausalLM
    from wsbench.produce.backend import Backend

    torch.manual_seed(0)
    cfg = LlamaConfig(
        vocab_size=len(tok), hidden_size=D, intermediate_size=64, num_hidden_layers=NL,
        num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=256,
    )  # fmt: skip
    m = LlamaForCausalLM(cfg).eval()
    return Backend(model=m, tokenizer=tok, device="cpu", model_id="tiny")


def tiny_cfg(tmp_path, **kw):
    base = {
        "n_layers": NL, "batch": 16, "limit_items": 2, "out": tmp_path / "results",
        "max_hours": 100.0, "loc_layers": (1, 3), "f_single_layers": (2, 1),
        "f_other_layers": (3,), "l_primary": 2, "band": (1, 2, 3), "t_layers": (2,),
        "c_layers": (2,), "lens_layers": (2,), "k_lens": 3,
    }  # fmt: skip
    base.update(kw)
    return SE.Config(**base)


def test_set_and_add_semantics(bk, tok):
    s = S.build_all()[0]
    r = S.render(tok, s, "agent")
    pos = [[r["ent"][e] for e in s.ents]]
    h = MD.capture(bk, [r["ids"]], pos, [2])[2]
    v = torch.randn(1, 2, D)
    with MD.patched(bk, {2: (torch.tensor(pos), v, "add")}, r["n"]):
        got = MD.capture(bk, [r["ids"]], pos, [2])[2]
    torch.testing.assert_close(got.float(), (h.float() + v).to(h.dtype).float())
    with MD.patched(bk, {2: (torch.tensor(pos), v)}, r["n"]):
        got = MD.capture(bk, [r["ids"]], pos, [2])[2]
    torch.testing.assert_close(got.float(), v.to(got.dtype).float())
    a = [[S.answer_token(tok, s.ents[0]), S.answer_token(tok, s.ents[1])]]
    base = MD.forward_patched(bk, [r["ids"]], None, answer_ids=a)
    zero = MD.forward_patched(
        bk, [r["ids"]], {2: (torch.tensor(pos), torch.zeros(1, 2, D), "add")}, answer_ids=a
    )
    assert torch.equal(base["ans"], zero["ans"])


@pytest.fixture()
def sess(bk, tmp_path, monkeypatch):
    se = SE.Session(bk, tiny_cfg(tmp_path))
    se.renders()
    monkeypatch.setattr(SE.Session, "correct", lambda self, sid, q: True)
    return se


def test_factorial_patterns(sess):
    jobs = [j for j in sess.f_jobs((2,), ("both",)) if j]
    by = Counter((j["meta"]["rcons"], j["meta"]["pat"]) for j in jobs)
    for rc in S.CONS:
        pats = {p for (c, p) in by if c == rc}
        want = {f"{a}{b}{c}" for a in "01" for b in "01" for c in "01"}
        if rc in ("P", "PC"):  # no replicate frame, so no (0,0,0) cell
            want.discard("000")
        assert pats == want, rc
    # spot checks from the plan: A <- OC same roles is order only; A <- P reversed is role only
    one = {(j["meta"]["rcons"], j["meta"]["dcons"], j["meta"]["keep"]): j["meta"]["pat"]
           for j in jobs}  # fmt: skip
    assert one[("A", "OC", True)] == "100" and one[("A", "PC", True)] == "010"
    assert one[("A", "P", False)] == "001" and one[("A", "P", True)] == "110"
    assert one[("A", "SC", True)] == "000" and one[("A", "A", False)] == "111"
    # identity-aligned: each recipient entity token gets the same entity's donor state
    for j in jobs[:50]:
        r = sess.by[j["recip"]]
        assert [src[2] for src in j["src"]] == list(r.ents)
        assert j["pos"] == [sess.R[(j["recip"], j["q"])]["ent"][e] for e in r.ents]


def test_vector_targets(sess):
    sess.capture()
    sess.build_vectors([2])
    jobs = [j for j in sess.v_jobs("L48") if j]
    assert jobs and all(sess.by[j["recip"]].split != "train" for j in jobs)
    for j in jobs:
        s = sess.by[j["recip"]]
        coefs = [src[2] for src in j["src"]]
        assert sum(coefs) == 0
        if s.family == "ditr":
            v = j["meta"]["vec"]
            i, k = {"d23": (1, 2), "d13": (0, 2), "d12p23": (0, 2)}.get(v, (0, 1))
            ans = S.answer(s, j["q"])
            want = {s.ents[i]: s.ents[k], s.ents[k]: s.ents[i]}.get(ans, ans)
            assert j["a_alt"] == want
        elif s.family != "cap":
            assert j["a_alt"] != S.answer(s, j["q"])
        if j["meta"]["vec"] == "role":
            pt = next(e for e in s.ents if s.role_of(e) == "patient")
            k = next(n for n, src in enumerate(j["src"]) if src[2] > 0)
            assert j["pos"][k] == sess.R[(j["recip"], j["q"])]["ent"][pt]


def test_build_vectors_recovers_planted_order_direction(sess):
    sess.capture()
    u = torch.zeros(D)
    u[0] = 5.0
    E = sess.E[2].float()
    for s in sess.sents:
        if s.family == "trans" and not s.shift and s.cons in S.CORE:
            i = sess.idx[s.sid]
            E[i, 0] = torch.zeros(D)
            E[i, 1] = u.clone()
    sess.E[2] = E.to(sess.E[2].dtype)
    info = sess.build_vectors([2])
    torch.testing.assert_close(sess.V["order"][2], u, atol=1e-2, rtol=0)
    assert info["2"]["norms"]["subj"] < 1e-3 and info["2"]["norms"]["role"] < 1e-3
    assert abs(info["2"]["norms"]["rand0"] - 5.0) < 1e-3


def test_end_to_end_tiny(bk, tmp_path, monkeypatch):
    jl = tmp_path / "jlens.pt"
    torch.save({"J": torch.randn(NL, D, D) * 0.2 + torch.eye(D)}, jl)
    monkeypatch.setattr(SE, "GATE2_MIN", 0.0)
    monkeypatch.setattr(SE, "SMOKE_ACC_MIN", 0)
    monkeypatch.setattr(SE.Session, "correct", lambda self, sid, q: True)
    cfg = tiny_cfg(tmp_path, lens_path=str(jl))
    se = SE.Session(bk, cfg)
    assert SE.run_all(se) == 0
    out = cfg.out
    st = json.loads((out / "status.json").read_text())
    assert st["why"] == "complete" and not st["skipped"], st
    rows = {}
    for p in sorted((out / "rows").glob("*.jsonl")):
        rows[p.stem] = [json.loads(x) for x in p.read_text().splitlines()]
    assert set(rows) == {"baseline", "gate2", "F_48", "V_48", "C", "L", "T", "F_other",
                         "V_band"}  # fmt: skip
    n_prompts = sum(len(S.questions(s)) for s in se.sents)
    assert len(rows["baseline"]) == n_prompts
    n_trans = sum(1 for s in se.trans())
    assert len(rows["F_48"]) == n_trans * 2 * 11 * 3  # 2 q x 11 donors x 3 sites
    sites = Counter((r["site"], r["donor_kind"]) for r in rows["L"])
    assert sites[("was", "twin")] == sites[("by", "twin")] > 0
    assert sites[("e_both", "shift_twin")] > 0 and sites[("last", "twin")] > 0
    assert {r["layers"][0] for r in rows["F_other"]} == {1, 3}
    assert all(r["layers"] == [1, 2, 3] for r in rows["V_band"])
    assert Counter(r["donor_kind"] for r in rows["T"])["dative"] > 0
    assert json.loads((out / "probes.json").read_text())["2"]["order"]["acc"]
    lens = json.loads((out / "lens.json").read_text())
    assert "jlens:L2:order" in lens and "logit:L2:order" in lens
    assert (out / "vectors.safetensors").exists()
    from p40 import analyze as AN

    summ = AN.analyze(out)
    assert "F_verdict" in summ and "V_verdict" in summ and AN.report(summ)
    sm = json.loads((out / "smoke.json").read_text())
    assert sm["identity_ok"] and sm["total_projected"] >= 0
    # every row is scored against a real option of its recipient
    for r in rows["F_48"] + rows["V_48"] + rows["T"]:
        assert r["a_rec"] in r["cands"] and r["a_alt"] in r["cands"]
