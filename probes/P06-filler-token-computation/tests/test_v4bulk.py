"""Unit tests for P06 follow-up F8's pod code (CPU only): chain walks and donors, prompt spans and
groups with the real V4 tokenizer (chain and varbind arms), the varbind candidate sets, the J-Lens
readers, capture/patch hooks and a whole `run` (with a resume) on a tiny random
DeepseekV4ForCausalLM with a whitespace tokenizer, and the npz schema."""

from __future__ import annotations

import json
import re
import zlib

import numpy as np
import pytest
from p06 import brauer as B
from p06 import v4bulk as VB
from p06 import v4pilot as V

torch = pytest.importorskip("torch")


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    for local in (True, False):
        try:
            return AutoTokenizer.from_pretrained(V.MODEL_ID, revision=V.MODEL_REV,
                                                 local_files_only=local)
        except Exception as e:  # noqa: BLE001
            err = e
    pytest.skip(f"V4 Flash tokenizer unavailable: {err!r}")


@pytest.fixture(scope="module")
def varbind():
    if not B.DATA_PATHS[1].exists():
        pytest.skip("Brauer's dataset (data/P06, gitignored) is missing")
    return B.load(1)


# --------------------------------------------------------------------- chain items and donors


def test_chain_items_and_walks():
    its = VB.chain_items()
    assert len(its) == 200 and [it["k"] for it in its[:4]] == [2, 3, 2, 3]
    assert len({it["item_id"] for it in its}) == 200
    for it in its:
        assert VB.walk(it["problem"]) == it["traj"] and len(it["traj"]) == it["k"] + 1


def test_matched_donors_differ_at_every_state():
    its = VB.chain_items()
    n_none = 0
    for it in its:
        starts = VB.donor_starts(it)
        assert starts == VB.donor_starts(it)  # seeded
        if not starts:
            n_none += 1
            continue
        d = VB.with_start(it, starts[0])
        assert all(a != b for a, b in zip(d["traj"], it["traj"], strict=True))
        assert d["answer"] == d["traj"][-1] and d["k"] == it["k"]
        ops = lambda p: [x for x in p.splitlines() if not x.startswith("Start")]
        assert ops(d["problem"]) == ops(it["problem"])
    assert n_none <= 5, n_none


def test_random_donor_same_k_nearest_length():
    its = VB.chain_items()[:20]
    lengths = {it["item_id"]: 100 + (zlib.crc32(it["item_id"].encode()) % 3) for it in its}
    for it in its:
        d, diff = VB.random_donor(it, its, lengths)
        assert d["k"] == it["k"] and d["item_id"] != it["item_id"]
        best = min(abs(lengths[e["item_id"]] - lengths[it["item_id"]]) for e in its
                   if e["k"] == it["k"] and e["item_id"] != it["item_id"])
        assert abs(diff) == best and VB.random_donor(it, its, lengths)[0] is d


# --------------------------------------------------------------------- spans, real tokenizer


def test_chain_spans_real_tokenizer(tok):
    shots = V.load_shots()
    its = VB.chain_items()[:6]
    for it in its:
        sp = {arm: VB.render(tok, VB.chain_messages(it, shots, arm), arm) for arm in VB.CHAIN_ARMS}
        g = sp["dots500"].groups
        assert g[0] == VB.GROUPS["label"] and g[-1] == VB.GROUPS["suffix"]
        assert (g == VB.GROUPS["filler"]).sum() == 500 and len(sp["dots500"].filler) == 500
        assert not (g == VB.GROUPS["problem"]).any()
        assert tok.convert_ids_to_tokens(sp["dots500"].ids[-2:]) == ["<｜Assistant｜>", "</think>"]
        gp = sp["pre500"].groups
        assert (gp == VB.GROUPS["filler"]).sum() == 500 and (gp == VB.GROUPS["problem"]).any()
        assert not (sp["base"].groups == VB.GROUPS["filler"]).any()
        # the matched donor has the same length and dot positions (asserted on the pod too)
        d = VB.with_start(it, VB.donor_starts(it)[0])
        dsp = VB.render(tok, VB.chain_messages(d, shots, "dots500"), "dots500")
        assert len(dsp.ids) == len(sp["dots500"].ids)
        assert np.array_equal(dsp.filler, sp["dots500"].filler)
        # group pattern (label/filler/suffix) identical across items, as the analysis needs
        assert (g != VB.GROUPS["problem"]).sum() == len(g)


def test_mc_items_and_spans(tok, varbind):
    mc, skipped = VB.mc_items(tok, 100)
    assert len(mc) == 100 and len(skipped) > 0
    assert mc[-1][0]["idx"] < 100 + len(skipped) + 1
    for ex, it in mc:
        assert set(it["options"]) >= {ex["answer"]} and len(it["options"]) == 10
        assert len(it["controls"]) == 10
        assert not set(it["controls"]) & (set(it["options"]) | set(it["role_values"]))
        if ex["answer"] >= 10:
            assert all(len(str(c)) == len(str(ex["answer"])) for c in it["controls"])
        assert len(it["cand_ids"]) == len(it["cand_values"]) <= 25
    ex, it = mc[0]
    shots = varbind[1]
    for arm, n_fill in (("dots100", 100), ("pre100", 100), ("mc10_base", 0),
                        ("mc10_dots100", 100)):
        sp = VB.render(tok, VB.mc_messages(ex, shots, arm), arm)
        assert (sp.groups == VB.GROUPS["filler"]).sum() == n_fill
        text = tok.decode(sp.ids)
        assert ("Options:" in text) == arm.startswith("mc10")


# --------------------------------------------------------------------- npz


def _rec(kind, n_rows=6, n_cand=21):
    rng = np.random.default_rng(0)
    rec = {"item_id": "chain:k2:5", "kind": kind, "arm": "dots500", "answer": 10,
           "positions": np.arange(n_rows), "slots": np.arange(-n_rows, 0),
           "groups": np.array([0] + [1] * (n_rows - 2) + [3], dtype=np.int8),
           "token_ids": np.arange(n_rows), "n_prompt": 50, "gen_ids": np.array([553]),
           "gen_text": "10", "cand_ids": np.arange(n_cand), "cand_values": np.arange(n_cand),
           "readers": ["logit_lens", "jpp"],
           "ro": {r: (rng.normal(size=(3, n_rows, n_cand)).astype(np.float16),
                      np.zeros((3, n_rows), dtype=np.int32)) for r in ("logit_lens", "jpp")}}
    if kind == "chain":
        rec.update(k=2, traj=np.array([9, 5, 10]))
    else:
        rec.update(idx=3, role_names=list(VB.MC_ROLES), role_values=np.arange(5),
                   options=np.arange(10), controls=np.arange(10, 20))
    return rec


def test_rec_roundtrip(tmp_path):
    for kind, extra in (("chain", VB.CHAIN_KEYS), ("mc", VB.MC_KEYS)):
        rec = _rec(kind, n_cand=21 if kind == "chain" else 25)
        p = VB.rec_path(tmp_path, "chain:k2:5#start3", kind)
        assert ":" not in p.name and "#" not in p.name
        VB.write_rec(p, rec, extra)
        back = VB.read_rec(p)
        assert back["kind"] == kind and back["answer"] == 10 and back["readers"] == ["logit_lens",
                                                                                     "jpp"]
        np.testing.assert_array_equal(back["ro"]["jpp"][0], rec["ro"]["jpp"][0])
        bad = dict(rec, groups=np.zeros(3, dtype=np.int8))
        VB.write_rec(p, bad, extra)
        with pytest.raises(ValueError):
            VB.read_rec(p)


# --------------------------------------------------------------------- tiny V4 model


class FakeTok:
    """Whitespace tokenizer; integers 0..999 map to ids 0..; other words hash above them. Decode
    gives back the integer for integer ids, so value_ids and grading work."""

    bos_token_id = 0

    def __init__(self, vocab: int) -> None:
        self.vocab = vocab

    def _id(self, w: str) -> int:
        if re.fullmatch(r"\d{1,2}", w):
            return int(w) % 64
        return 64 + zlib.crc32(w.encode()) % (self.vocab - 64)

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=False):
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        return {"input_ids": [self._id(text[a:b]) for a, b in spans], "offset_mapping": spans}

    def encode(self, text, add_special_tokens=False):
        return self(text)["input_ids"]

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(str(i) for i in ids)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    from transformers import DeepseekV4Config, DeepseekV4ForCausalLM

    cfg = DeepseekV4Config(
        vocab_size=128, hidden_size=32, intermediate_size=16, moe_intermediate_size=16,
        num_hidden_layers=4, num_attention_heads=2, num_key_value_heads=1, head_dim=16,
        q_lora_rank=16, o_groups=2, o_lora_rank=8, n_routed_experts=4, num_experts_per_tok=2,
        index_n_heads=2, index_head_dim=8, index_topk=4, sliding_window=8, hc_mult=4,
        qk_rope_head_dim=8, num_hash_layers=1, compress_ratios=[0, 4, 128, 4])
    torch.manual_seed(0)
    d = tmp_path_factory.mktemp("tinyv4b")
    DeepseekV4ForCausalLM(cfg).save_pretrained(d)
    model = DeepseekV4ForCausalLM.from_pretrained(d, dtype=torch.bfloat16,
                                                  attn_implementation="eager").eval()
    ctx = V.Ctx(model, FakeTok(128), V.model_parts(model), list(range(21)), "cpu")
    V.smoke_with_fixes(ctx, list(range(5, 45)))
    return ctx


def _maps(identity_last=True, n=3, d=32):
    g = torch.Generator().manual_seed(1)
    maps = {i: (torch.eye(d) + 0.1 * torch.randn(d, d, generator=g)).half() for i in range(n)}
    if identity_last:
        maps[n - 1] = torch.eye(d).half()
    return maps


def test_jreader(tiny):
    jr = VB.JReader(_maps(), "cpu")
    x = torch.randn(5, 4, 32).to(torch.bfloat16)
    with torch.inference_mode():
        lg_id = jr.logits(tiny.parts, x, 3)  # no map at the last layer: identity on the mean
        ref = tiny.parts.lm_head(tiny.parts.norm(x.float().mean(1).to(x.dtype))).float()
        assert torch.allclose(lg_id, ref)
        lg0 = jr.logits(tiny.parts, x, 0)
        ref0 = tiny.parts.lm_head(tiny.parts.norm(
            (x.float().mean(1) @ jr.maps[0].float().T).to(x.dtype))).float()
        assert torch.allclose(lg0, ref0) and lg0.shape == (5, 128)
    assert VB.identity_check(jr.maps) == {"last_mapped_layer": 2, "rel_dist_to_identity": 0.0,
                                          "n_maps": 3}


def test_capture_and_patch(tiny):
    ids = list(range(64, 64 + 40))
    pos = np.arange(10, 20)
    new, first, hs = VB.gen_capture(tiny, ids, pos)
    assert tuple(hs.shape) == (4, 10, 4, 32) and int(first.argmax()) == new[0]
    assert VB.gen_patched(tiny, ids, pos, hs) == new  # self-patch changes nothing
    other = list(range(70, 70 + 40))
    _, _, hs2 = VB.gen_capture(tiny, other, pos)
    with VB.inject(tiny.parts.blocks, torch.as_tensor(pos), list(hs2), len(ids)) as fired, \
            V.capture(tiny.parts.blocks, torch.as_tensor(pos), len(ids)) as seen:
        tiny.model(input_ids=torch.tensor([ids]), use_cache=False)
    # capture runs after inject on the same output, so it sees the donor's streams
    assert fired == [1, 1, 1, 1]
    assert all(torch.equal(a, b) for a, b in zip(seen, hs2, strict=True))
    with pytest.raises(ValueError):
        VB.gen_patched(tiny, ids, pos[:5], hs)


def test_run_end_to_end_and_resume(tiny, tmp_path, monkeypatch):
    monkeypatch.setattr(VB, "OUT", tmp_path / "v4bulk")
    monkeypatch.setattr(VB, "RO_CHAIN", tmp_path / "v4bulk" / "readouts" / "chain")
    monkeypatch.setattr(VB, "RO_MC", tmp_path / "v4bulk" / "readouts" / "mc")
    monkeypatch.setattr(V, "env_info", lambda: {"stub": True})
    monkeypatch.setattr(V, "load", lambda: (tiny, {"stub": True}))
    monkeypatch.delenv("P06_T0", raising=False)

    def fake_jreaders(device, info):
        info["jlens"] = {"stub": True}
        return {"jlens": VB.JReader(_maps(), device), "jpp": VB.JReader(_maps(False), device)}

    monkeypatch.setattr(VB, "load_jreaders", fake_jreaders)
    ex = {"idx": 7, "definitions": [["lal", 4], ["cok", "twice the number for lal plus 3"],
                                    ["wib", "three times the number for cok minus 1"]],
          "queried_term": "cok", "queried_value": 11, "coefficient": 2, "operation": "plus",
          "constant": 2, "question": "What is twice the number for cok plus 2?", "answer": 24}
    shots = [dict(ex, idx=-1)] * 2
    monkeypatch.setattr(B, "load", lambda depth=1: ([ex], shots))
    mc_it = {**VB.mc_item(ex)}
    mc_it["cand_ids"] = [v % 64 for v in mc_it["cand_values"]]
    monkeypatch.setattr(VB, "mc_items", lambda tok, n: ([(ex, mc_it)], [3]))
    assert VB.main(["run", "--limit", "2"]) == 0
    out = tmp_path / "v4bulk"
    st = json.loads((out / "run.json").read_text())
    assert st["rc"] == 0 and st["smoke"]["checks"]["self-patch unchanged"]
    assert st["smoke"]["rate"] == 1.0 and st["plan"]["mc_skipped_idx"] == [3]
    assert st["self_patch_changed"] == 0 and st["self_patch_n"] == 2
    ans = [json.loads(x) for x in (out / "chain_answers.jsonl").read_text().splitlines()]
    assert len(ans) == 6 and all(r["final_agree"] for r in ans)
    patch = [json.loads(x) for x in (out / "patch.jsonl").read_text().splitlines()]
    assert len(patch) == 2 and all("tx_pred" in r and "rtx_pred" in r for r in patch)
    assert all(r["self_changed"] is False for r in patch)
    mcr = [json.loads(x) for x in (out / "mc_answers.jsonl").read_text().splitlines()]
    assert [r["arm"] for r in mcr] == list(VB.MC_ARMS)
    recs = [VB.read_rec(p) for p in sorted((out / "readouts").rglob("*.npz"))]
    assert len(recs) == 6 + 4 and all(set(r["readers"]) == set(VB.READERS) for r in recs)
    chain_rec = next(r for r in recs if r["kind"] == "chain" and r["arm"] == "dots500")
    assert (chain_rec["groups"] == VB.GROUPS["filler"]).sum() == 500
    assert chain_rec["ro"]["jlens"][0].shape == (4, len(chain_rec["positions"]), 21)
    # resume: nothing is redone
    assert VB.main(["run", "--limit", "2"]) == 0
    assert len((out / "chain_answers.jsonl").read_text().splitlines()) == 6
    assert len((out / "patch.jsonl").read_text().splitlines()) == 2
    assert len((out / "mc_answers.jsonl").read_text().splitlines()) == 4


def test_projection_and_smoke_failure(tiny, tmp_path, monkeypatch):
    p0 = VB.projection(200, 20, 100)
    assert 40 * 60 < p0 < 90 * 60  # F7 medians: about an hour
    assert VB.projection(10, 0, 0, item_s=5.0) == 50.0
    monkeypatch.setattr(VB, "OUT", tmp_path / "v4b")
    monkeypatch.setattr(VB, "RO_CHAIN", tmp_path / "v4b" / "readouts" / "chain")
    monkeypatch.setattr(V, "env_info", dict)
    monkeypatch.setattr(V, "load", lambda: (tiny, {}))
    monkeypatch.setattr(VB, "load_jreaders", lambda device, info: {})
    monkeypatch.setattr(VB, "mc_items", lambda tok, n: ([], []))
    monkeypatch.setattr(VB, "smoke", lambda bk, it: {"rate": 0.5, "checks": {
        "lens agreement": False, "self-patch unchanged": True}})
    assert VB.main(["run", "--limit", "1"]) == 3
    st = json.loads((tmp_path / "v4b" / "run.json").read_text())
    assert st["stopped"].startswith("smoke failed: lens agreement")
    assert not (tmp_path / "v4b" / "chain_answers.jsonl").exists()
