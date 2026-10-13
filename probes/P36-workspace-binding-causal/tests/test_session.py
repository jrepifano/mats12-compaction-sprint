"""P36 session on a tiny random Llama (CPU) with the real Qwen3.6-27B tokenizer: patch semantics,
donor maps, cell choice, and an end-to-end run with the gates opened."""

from __future__ import annotations

import json
from collections import Counter

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from p36 import model as MD  # noqa: E402
from p36 import session as SE  # noqa: E402
from p36 import stimuli as S  # noqa: E402

D = 32
NL = 6


@pytest.fixture(scope="module")
def tok():
    try:
        return transformers.AutoTokenizer.from_pretrained(
            SE.MODEL_ID, revision=SE.MODEL_REV, local_files_only=True
        )
    except Exception:
        pytest.skip("Qwen3.6-27B tokenizer not in the local HF cache")


@pytest.fixture(scope="module")
def bk(tok):
    from transformers import LlamaConfig, LlamaForCausalLM
    from wsbench.produce.backend import Backend

    torch.manual_seed(0)
    cfg = LlamaConfig(
        vocab_size=len(tok),
        hidden_size=D,
        intermediate_size=64,
        num_hidden_layers=NL,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=128,
    )
    m = LlamaForCausalLM(cfg).eval()
    return Backend(model=m, tokenizer=tok, device="cpu", model_id="tiny")


def _sents():
    items, sents = S.load_stimuli(SE.HERE / "data" / "stimuli.json")
    return items, sents


def test_donor_maps_properties():
    _, sents = _sents()
    by = {s.sid: s for s in sents}
    dm = SE.donor_maps(sents)
    assert all(len(dm[k]) == len(sents) for k in dm)
    for r, d in dm["twin"].items():
        a, b = by[r], by[d]
        assert (a.agent, a.patient) == (b.patient, b.agent) and a.voice == b.voice
        assert a.text != b.text and sorted(a.text.rstrip(".").split()) == sorted(b.text.rstrip(".").split())
    for r, d in dm["xsame"].items():
        a, b = by[r], by[d]
        assert (a.agent, a.patient) == (b.agent, b.patient) and a.voice != b.voice
    for r, d in dm["xtwin"].items():
        a, b = by[r], by[d]
        assert (a.agent, a.patient) == (b.patient, b.agent) and a.voice != b.voice
    for r, d in dm["random"].items():
        a, b = by[r], by[d]
        assert a.item != b.item and a.voice == b.voice and a.role == b.role


def test_choose_cells():
    rates = {
        ("final", (12,)): {"agent": 0.9, "patient": 0.4},
        ("final", (18,)): {"agent": 0.6, "patient": 0.7},
        ("final", (24,)): {"agent": 0.6, "patient": 0.6},
        ("entity", (12,)): {"agent": 0.2, "patient": 0.9},
    }
    assert SE.choose_cells(rates, 2) == {"final": [(18,), (24,)]}


def test_identity_and_patch_semantics(bk, tok):
    _, sents = _sents()
    s, t = sents[0], sents[1]  # twins (d_agent, d_patient) of the same voice
    rs, rt = S.render(tok, s, "agent"), S.render(tok, t, "agent")
    e1, e2 = sorted((s.agent, s.patient))
    pos_s = [[rs["ent"][e1], rs["ent"][e2]]]
    pos_t = [[rt["ent"][e1], rt["ent"][e2]]]
    hs = MD.capture(bk, [rs["ids"]], pos_s, [2])[2]
    ht = MD.capture(bk, [rt["ids"]], pos_t, [2])[2]
    a = [[S.answer_token(tok, s.agent), S.answer_token(tok, s.patient)]]
    base = MD.forward_patched(bk, [rs["ids"]], None, answer_ids=a)
    same = MD.forward_patched(bk, [rs["ids"]], {2: (torch.tensor(pos_s), hs)}, answer_ids=a)
    assert torch.equal(base["ans"], same["ans"])
    # a cross-position entity patch writes the twin's entity states into s's entity slots
    with MD.patched(bk, {2: (torch.tensor(pos_s), ht)}, rs["n"]):
        got = MD.capture(bk, [rs["ids"]], pos_s, [2])[2]
    assert torch.equal(got, ht)
    assert rs["ent"][e1] == rt["ent"][e2]  # the slots really swap between twins


def test_build_dictionary_unit_rows_and_logit_identity(bk):
    Dl = MD.build_dictionary(bk, None, chunk=50000)
    n = Dl.norm(dim=1)
    assert torch.allclose(n[n > 0], torch.ones_like(n[n > 0]), atol=1e-5)
    g = MD.final_gain(bk)
    W = bk.unembed.float()
    ref = (W[:5] * g) / (W[:5] * g).norm(dim=1, keepdim=True)
    torch.testing.assert_close(Dl[:5], ref)
    J = torch.randn(D, D)
    Dj = MD.build_dictionary(bk, J, chunk=50000)
    refj = (W[:5] * g) @ J
    torch.testing.assert_close(Dj[:5], refj / refj.norm(dim=1, keepdim=True), atol=1e-5, rtol=1e-4)


def test_end_to_end_tiny(bk, tmp_path, monkeypatch):
    jl = tmp_path / "jlens.pt"
    torch.save({"J": torch.randn(NL, D, D) * 0.2 + torch.eye(D)}, jl)
    jp = tmp_path / "jpp.pt"
    torch.save(
        {
            "parameters": {"jacobians": {L: torch.eye(D) + 0.1 * torch.randn(D, D) for L in range(NL - 1)}},
            "source_layers": list(range(NL - 1)),
            "config": {},
        },
        jp,
    )
    monkeypatch.setattr(SE, "GATE1_MIN", 0.0)
    monkeypatch.setattr(SE, "GATE2_MIN", 0.0)
    monkeypatch.setattr(SE, "SMOKE_ACC_MIN", 0)

    def all_pairs(base, dmap):
        return {(sid, q) for (sid, q) in base if sid in dmap and (dmap[sid], q) in base}

    monkeypatch.setattr(SE, "valid_pairs", all_pairs)
    cfg = SE.Config(
        stage0_layers=(1, 3),
        n_layers=NL,
        batch=16,
        pursuit_batch=8,
        k1=3,
        k_sweep=(2, 3, 6),
        limit_items=3,
        out=tmp_path / "results",
        lens_paths={"jlens": str(jl), "jpp": str(jp)},
    )
    sess = SE.Session(bk, cfg)
    monkeypatch.setattr(SE.Session, "time_left", lambda self: 1e9)
    rc = SE.run_all(sess)
    assert rc == 0
    st = json.loads((cfg.out / "status.json").read_text())
    assert st["why"] == "complete", st
    rows = [json.loads(x) for x in (cfg.out / "rows" / "pass1.jsonl").read_text().splitlines()]
    c = Counter((r["kind"], r["op"], r["dict"]) for r in rows)
    n_cells = sum(len(v) for v in st["cells_pass1"].values())
    per = 12 * 2 * n_cells  # 3 items x 2 voices x 2 roles = 12 sentences, 2 questions
    assert c[("twin", "j", "jlens")] == per and c[("random", "r", "logit")] == per
    assert c[("self", "full", None)] == per
    # self-recombined (j_r + r_r) equals the unpatched answer logits up to fp rounding
    base = {(r["recip"], r["q"]): r for r in map(json.loads, (cfg.out / "rows" / "baseline.jsonl").read_text().splitlines())}  # noqa: E501
    for r in rows:
        if r["kind"] == "self":
            assert abs(r["logit_rec"] - base[(r["recip"], r["q"])]["logit_rec"]) < 1e-3
    rows2 = [json.loads(x) for x in (cfg.out / "rows" / "pass2.jsonl").read_text().splitlines()]
    ks = Counter((r["dict"], r["k"]) for r in rows2)
    assert ("jlens", 6) in ks and ("logit", 2) in ks and ("jlens", 3) not in ks
    sel = {L for c in st["selected"].values() for L in c}
    if NL - 1 not in sel:
        assert ("jpp", 6) in ks
    stats = [json.loads(x) for x in (cfg.out / "decomp_stats.jsonl").read_text().splitlines()]
    assert all(len(s["fve"]) == 12 for s in stats)
    assert (cfg.out / "states" / "h.safetensors").exists()
    assert (cfg.out / "twin_share.jsonl").exists()
