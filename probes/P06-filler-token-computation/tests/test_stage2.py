"""Unit tests for P06 F3 Stage 2 (CPU only): prompt tokens and positions, donors, digit ids, the
reader arithmetic and the K/V transplant on a tiny fake model, the npz schema, the run driver, and
the analysis on synthetic data. Tests that need Brauer's dataset (gitignored) or the Gemma 4
tokenizer (google/gemma-4-31B-it @ 842da379 in the local HF cache) skip without them."""

from __future__ import annotations

import json
import types

import numpy as np
import pytest
from p06 import brauer as B
from p06 import readers_analysis as RA
from p06 import stage2 as S

torch = pytest.importorskip("torch")


@pytest.fixture(scope="module")
def data():
    if not B.DATA_PATHS[1].exists():
        pytest.skip("Brauer's dataset (data/P06, gitignored) is missing")
    return S.load_examples()


@pytest.fixture(scope="module")
def tok():
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(S.MODEL_ID, revision=S.MODEL_REV,
                                             local_files_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Gemma 4 tokenizer not cached: {e!r}")


# --------------------------------------------------------------------- task values and donors


def test_chain_values_and_donors(data):
    items, _, pool = data
    assert len(items) == 150 and [e["idx"] for e in items] == list(range(150))
    for ex in items:
        cv = S.chain_values(ex)
        assert 10 <= cv["x"] <= 99 and cv["y"] == ex["queried_value"]
        assert cv["answer"] == ex["answer"]
        d = S.make_donor(ex)
        dv = S.chain_values(d)
        assert dv["ref"] == cv["ref"] and dv["x"] != cv["x"] and 10 <= dv["x"] <= 99
        assert dv["y"] != cv["y"] and d["answer"] != ex["answer"] and d["answer"] >= 0
        assert d["question"] == ex["question"]
        assert [n for n, _ in d["definitions"]] == [n for n, _ in ex["definitions"]]
        assert S.make_donor(ex) == d  # seeded
        assert S.random_donor(ex, pool)["idx"] != ex["idx"]


# --------------------------------------------------------------------- real tokenizer


@pytest.mark.parametrize("idx,arm,n", [(63, "dots100", 1243), (117, "dots100", 1263),
                                       (13, "base", 610), (0, "pre100", 1247)])
def test_render_matches_crusoe_prompt_tokens(tok, data, idx, arm, n):
    """F1 rows served by Crusoe report exactly these prompt token counts."""
    _, shots, pool = data
    ex = next(e for e in pool if e["idx"] == idx)
    assert len(S.render(tok, ex, shots, arm).ids) == n


TAIL = ["\n\n", "Answer", ":", "<turn|>", "\n", "<|turn>", "model", "\n", "<|channel>",
        "thought", "\n", "<channel|>"]


def test_read_and_dot_positions(tok, data):
    items, shots, _ = data
    r = S.render(tok, items[0], shots, "dots100")
    toks = tok.convert_ids_to_tokens(r.ids)
    assert len(r.read) == 2 + 100 + len(TAIL) and r.read[-1] == len(r.ids) - 1
    assert toks[r.read[0]] == "Filler" and toks[r.read[1]] == ":"
    assert list(r.dots) == list(r.read[2:102]) and {toks[i] for i in r.dots} == {"▁."}
    assert [toks[i] for i in r.read[102:]] == TAIL
    last_user = max(i for i, t in enumerate(toks[:-1]) if t == "<|turn>" and toks[i + 1] == "user")
    assert r.dots[0] > last_user
    b = S.render(tok, items[0], shots, "base")
    assert len(b.dots) == 0 and [tok.convert_ids_to_tokens(b.ids)[i] for i in b.read] == TAIL


def test_donor_differs_only_at_the_literal(tok, data):
    items, shots, _ = data
    for ex in items[:40]:
        r = S.render(tok, ex, shots, "dots100")
        d = S.render(tok, S.make_donor(ex), shots, "dots100")
        diff = S.check_donor(r, d)
        assert 1 <= len(diff) <= 2
        assert tok.decode([r.ids[i] for i in r.literal]) == str(S.chain_values(ex)["x"])
    other = S.render(tok, items[1], shots, "dots100")
    with pytest.raises(ValueError):
        S.check_donor(r, other)


def test_digit_ids_real(tok):
    assert tok.convert_ids_to_tokens(S.digit_token_ids(tok)) == list("0123456789")


# --------------------------------------------------------------------- fakes


class FakeTok:
    def __init__(self, table):
        self.table = table

    def encode(self, s, add_special_tokens=False):
        return list(self.table[s])


def test_digit_ids_fake():
    table = {d: [10 + i] for i, d in enumerate("0123456789")}
    table.update({" " + d: [3, 10 + i] for i, d in enumerate("0123456789")})
    assert S.digit_token_ids(FakeTok(table)) == list(range(10, 20))
    table["7"] = [16, 17]
    with pytest.raises(ValueError):
        S.digit_token_ids(FakeTok(table))


V, D, NL = 48, 16, 4
CAP = 30.0
DIG = list(range(10, 20))


class Attn(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.q_proj, self.k_proj, self.v_proj = (torch.nn.Linear(D, D) for _ in range(3))


class Block(torch.nn.Module):
    """Causal single-head attention with a K/V cache, then a tanh MLP; returns a tuple."""

    def __init__(self):
        super().__init__()
        self.self_attn = Attn()
        self.mlp = torch.nn.Linear(D, D)

    def forward(self, h, cache):
        q, k, v = self.self_attn.q_proj(h), self.self_attn.k_proj(h), self.self_attn.v_proj(h)
        if "k" in cache:
            k, v = torch.cat([cache["k"], k], 1), torch.cat([cache["v"], v], 1)
        cache["k"], cache["v"] = k, v
        tq, tk = h.shape[1], k.shape[1]
        att = q @ k.transpose(1, 2) / D ** 0.5
        mask = torch.arange(tk)[None, :] > (torch.arange(tq)[:, None] + tk - tq)
        att = att.masked_fill(mask, float("-inf")).softmax(-1)
        h = h + att @ v
        return (h + torch.tanh(self.mlp(h)),)


class Norm(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(D) * 0.1)

    def forward(self, x):
        x32 = x.float()
        y = x32 * torch.rsqrt(x32.pow(2).mean(-1, keepdim=True) + 1e-6) * (1 + self.w)
        return y.to(x.dtype)


class FakeLM(torch.nn.Module):
    """Gemma4ForConditionalGeneration's shape: model.language_model.{layers,norm}, self_attn.
    {k,v}_proj, a tied head, final logit softcapping, logits_to_keep, and a cached greedy
    `generate` (one prefill call over the prompt, then one call per new token)."""

    def __init__(self):
        super().__init__()
        torch.manual_seed(0)
        self.model = torch.nn.Module()
        self.model.language_model = torch.nn.Module()
        self.model.language_model.layers = torch.nn.ModuleList([Block() for _ in range(NL)])
        self.model.language_model.norm = Norm()
        self.embed = torch.nn.Embedding(V, D)
        self.config = types.SimpleNamespace(
            get_text_config=lambda: types.SimpleNamespace(final_logit_softcapping=CAP))

    def get_output_embeddings(self):
        return types.SimpleNamespace(weight=self.embed.weight)

    def forward(self, input_ids, use_cache=False, logits_to_keep=0, caches=None):
        caches = caches if caches is not None else [{} for _ in range(NL)]
        h = self.embed(input_ids)
        for b, c in zip(self.model.language_model.layers, caches, strict=True):
            h = b(h, c)[0]
        h = self.model.language_model.norm(h)
        if logits_to_keep:
            h = h[:, -logits_to_keep:]
        lg = torch.nn.functional.linear(h, self.embed.weight).float()
        return types.SimpleNamespace(logits=CAP * torch.tanh(lg / CAP))

    def generate(self, input_ids, attention_mask, max_new_tokens, do_sample, output_logits,
                 return_dict_in_generate):
        caches = [{} for _ in range(NL)]
        ids, step, logits = input_ids, input_ids, []
        for _ in range(max_new_tokens):
            lg = self.forward(step, logits_to_keep=1, caches=caches).logits[:, -1]
            logits.append(lg)
            step = lg.argmax(-1, keepdim=True)
            ids = torch.cat([ids, step], dim=1)
        return types.SimpleNamespace(sequences=ids, logits=tuple(logits))


def fake_ctx(lenses=None, text="183"):
    m = FakeLM().eval()
    blocks, norm = S.text_parts(m)
    tok = types.SimpleNamespace(decode=lambda ids, skip_special_tokens=True: text)
    return S.Ctx(m, tok, blocks, norm, m.get_output_embeddings().weight.detach(), S.softcap_of(m),
                 DIG, lenses or {}, "cpu")


def fake_rendered(ids, n_read=40, dots=(10, 30), literal=(3, 4)):
    return S.Rendered(list(ids), np.arange(len(ids) - n_read, len(ids)),
                      np.arange(*dots), np.array(literal))


def test_unembed_readout_matches_manual():
    ctx = fake_ctx()
    x = torch.randn(7, D)
    with torch.inference_mode():
        lp, top, _ = S.unembed_readout(x, ctx.norm, ctx.w_u, CAP, DIG, chunk=3)
        lg = ctx.norm(x) @ ctx.w_u.T
    lg = CAP * torch.tanh(lg / CAP)
    assert torch.allclose(lp, torch.log_softmax(lg, -1)[:, DIG], atol=1e-5)
    assert torch.equal(top, lg.argmax(-1))


def test_identity_lens_equals_logit_lens():
    hs = torch.randn(NL, 5, D)
    lens = S.Lens({i: torch.eye(D, dtype=torch.float16) for i in range(NL - 1)}, "cpu", True)
    ro = S.readouts(fake_ctx({"jlens": lens}), hs)
    assert np.array_equal(ro["jlens"]["top1"], ro["logit_lens"]["top1"])
    assert np.allclose(ro["jlens"]["digit_lp"], ro["logit_lens"]["digit_lp"], atol=1e-2)
    lens2 = S.Lens({i: 2 * torch.eye(D, dtype=torch.float16) for i in range(NL - 1)}, "cpu", False)
    ro2 = S.readouts(fake_ctx({"jpp": lens2}), hs)
    assert np.array_equal(ro2["jpp"]["digit_lp"][-1], ro2["logit_lens"]["digit_lp"][-1])


EX = {"idx": 7, "definitions": [["aaa", 54], ["bbb", "twice the number for aaa minus 23"]],
      "queried_term": "bbb", "queried_value": 85, "coefficient": 2, "operation": "plus",
      "constant": 13, "question": "What is twice the number for bbb plus 13?", "answer": 183}


def test_read_pass_and_npz_roundtrip(tmp_path):
    ctx = fake_ctx({"jlens": S.Lens({i: torch.eye(D) for i in range(NL - 1)}, "cpu", True)})
    ids = [int(x) for x in np.random.default_rng(0).integers(20, V, size=60)]
    r = fake_rendered(ids)
    with torch.inference_mode():
        text, gen, lp = S.decode(ctx, r.ids)
    rec, agree = S.read_pass(ctx, EX, "dots100", r, gen, text, lp)
    assert rec["ro"]["logit_lens"]["digit_lp"].shape == (NL, 40, 10)
    assert agree["read_agree"] == agree["read_n"] == 40 and agree["max_logit_diff"] < 1e-4
    assert agree["final_eq_gen"]
    assert list(rec["slots"][[0, -1]]) == [-40, -1]
    p = S.npz_path(rec["item_id"], "dots100", tmp_path)
    S.write_npz(p, rec)
    back = S.read_npz(p)
    assert back["idx"] == 7 and back["arm"] == "dots100" and back["gen_text"] == "183"
    assert back["values"] == {"x": 54, "c1x": 108, "y": 85, "c2y": 170, "answer": 183}
    assert back["readers"] == ["logit_lens", "jlens"]
    assert np.array_equal(back["ro"]["jlens"]["digit_lp"], rec["ro"]["jlens"]["digit_lp"])
    assert not list(tmp_path.glob("*.tmp"))


def test_transplant_hooks():
    ctx = fake_ctx()
    ids = [int(x) for x in np.random.default_rng(1).integers(20, V, size=50)]
    tgt = fake_rendered(ids, dots=(10, 30))
    with torch.inference_mode():
        _, base_gen, _ = S.decode(ctx, ids)
    _, self_gen, _ = S.transplant(ctx, tgt, ids, tgt.dots)
    assert self_gen == base_gen  # self-transplant changes nothing
    donor = list(ids)
    donor[3:5] = [21, 22]  # the "literal": the donor's filler K/V differ downstream of it
    pos = torch.arange(10, 30)

    def cache_k(seq, stored=None):
        caches = [{} for _ in range(NL)]
        with torch.inference_mode():
            if stored is None:
                ctx.model(input_ids=torch.tensor([seq]), logits_to_keep=1, caches=caches)
            else:
                with S.inject(ctx.kv, pos, stored, len(seq)) as fired:
                    ctx.model(input_ids=torch.tensor([seq]), logits_to_keep=1, caches=caches)
                assert fired == [1] * len(ctx.kv)
        return [c["k"][0] for c in caches]

    with torch.inference_mode(), S.capture(ctx.kv, pos, len(donor)) as kv:
        ctx.model(input_ids=torch.tensor([donor]), logits_to_keep=1)
    k_t, k_d, k_x = cache_k(ids), cache_k(donor), cache_k(ids, kv)
    for layer in range(1, NL):  # layer 0's filler K/V can't see the literal through attention
        assert not torch.allclose(k_t[layer][10:30], k_d[layer][10:30])
    for layer in range(NL):  # spliced: the donor's K at the filler, the target's elsewhere
        assert torch.equal(k_x[layer][10:30], k_d[layer][10:30])
        assert torch.equal(k_x[layer][:10], k_t[layer][:10])
    assert len(kv) == 2 * NL


def test_transplant_changes_and_random_mapping():
    ctx = fake_ctx()
    rng = np.random.default_rng(2)
    ids = [int(x) for x in rng.integers(20, V, size=50)]
    other = [int(x) for x in rng.integers(20, V, size=56)]
    tgt = fake_rendered(ids, dots=(10, 30))
    base = S.decode(ctx, ids)[2]
    for s in (5, 20, 36):  # another sequence's K/V, mapped onto the target's filler span
        lp = S.transplant(ctx, tgt, other, np.arange(s, s + 20))[2]
        assert not torch.allclose(lp[0], base[0])  # moves the first step's distribution


def test_cmd_run_smoke_gate(tmp_path, monkeypatch):
    """The run driver on the fake model: smoke checks, projection gate, bulk, resumable rows."""
    exs = []
    for i in range(5):
        e = json.loads(json.dumps(EX))
        e["idx"], e["definitions"][0][1] = i, 30 + i
        e["queried_value"] = 2 * (30 + i) - 23
        e["answer"] = 2 * e["queried_value"] + 13
        exs.append(e)
    monkeypatch.setattr(S, "load_examples", lambda: (exs[:4], [], exs))
    for name, val in (("OUT", tmp_path / "out"), ("READOUTS", tmp_path / "out" / "readouts"),
                      ("TIMINGS", tmp_path / "timings.json")):
        monkeypatch.setattr(S, name, val)
    ctx = fake_ctx()
    monkeypatch.setattr(S, "load", lambda: (ctx, {"softcap": CAP}))
    base_ids = [int(x) for x in np.random.default_rng(0).integers(25, V, size=60)]

    def fake_render(tok, ex, shots, arm):
        ids = list(base_ids)
        ids[3:5] = [20 + S.chain_values(ex)["x"] // 10, 20 + S.chain_values(ex)["x"] % 10]
        n_read = 40 if arm == "dots100" else 12
        return fake_rendered(ids, n_read=n_read, dots=(10, 30) if arm == "dots100" else (0, 0))

    monkeypatch.setattr(S, "render", fake_render)
    monkeypatch.setenv("P06_BUDGET_S", "0.000001")
    assert S.main(["run"]) == 3  # the projection exceeds a tiny budget
    smoke = json.loads((tmp_path / "out" / "smoke.json").read_text())
    assert smoke["checks"] == {"lens agreement": True, "self-transplant": True,
                               "time projection": False}
    monkeypatch.setenv("P06_BUDGET_S", "36000")
    assert S.main(["run"]) == 0  # a rerun: smoke items are not redone, the bulk completes
    assert len(list((tmp_path / "out" / "readouts").glob("*.npz"))) == 8
    ans = RA.read_jsonl(tmp_path / "out" / "answers.jsonl")
    assert len(ans) == 12 and {r["arm"] for r in ans} == set(S.ARMS)
    tx = RA.read_jsonl(tmp_path / "out" / "transplant.jsonl")
    assert [r["idx"] for r in tx] == [0, 1, 2, 3]
    assert all(r["self_text"] == r["target_text"] for r in tx)
    assert all(r["rand_idx"] != r["idx"] and r["donor_x"] != 30 + r["idx"] for r in tx)
    status = json.loads((tmp_path / "out" / "status.json").read_text())
    assert status["complete"] and status["bulk_self_changed"] == 0
    assert {"load", "smoke", "bulk"} <= set(json.loads((tmp_path / "timings.json").read_text()))
    res = RA.analyze(RA.load_records(tmp_path / "out" / "readouts"), tx, ans, [], min_layer=2)
    assert res["lens_model_agreement"] == 1.0
    assert res["transplant"]["all_pairs"]["n"] == 4


def test_load_lens(tmp_path):
    p = tmp_path / "lens.pt"
    torch.save({"J": {i: torch.eye(D, dtype=torch.float16) for i in range(3)},
                "source_layers": [0, 1, 2], "d_model": D, "n_prompts": 5,
                "provenance": {"m": "jpp"}}, p)
    maps, meta = S.load_lens(p)
    assert sorted(maps) == [0, 1, 2] and meta["d_model"] == D and meta["weights_only"]
    torch.save({"J": {0: torch.eye(D), 2: torch.eye(D)}}, p)
    with pytest.raises(ValueError):
        S.load_lens(p)


def test_constants_match_i9():
    from emx.i9 import contracts as C

    assert (S.MODEL_ID, S.MODEL_REV, S.N_LAYERS, S.D_MODEL) == (
        C.GEMMA_ID, C.GEMMA_REV, C.GEMMA_LAYERS, C.GEMMA_D_MODEL)


# --------------------------------------------------------------------- analysis


def synth(n=80, n_layers=60, n_slots=20, plant=None, seed=0, arm="dots100"):
    """Synthetic records with random chain values; `plant[role] = (layer, slot index)` adds +8
    to that value's first digit there."""
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        x = int(rng.integers(10, 100))
        y = 2 * x + int(rng.integers(-20, 20))
        vals = {"x": x, "c1x": 2 * x, "y": y, "c2y": 3 * y, "answer": 3 * y + 5}
        lp = rng.normal(-8, 0.5, size=(n_layers, n_slots, 10)).astype(np.float16)
        for role, (layer, s) in (plant or {}).items():
            lp[layer, s, RA.first_digit(vals[role])] += 8
        recs.append({"item_id": f"varbind:{i}", "idx": i, "arm": arm,
                     "slots": np.arange(-n_slots, 0, dtype=np.int16),
                     "token_ids": np.arange(n_slots, dtype=np.int32), "values": vals,
                     "ro": {"logit_lens": {"digit_lp": lp}}})
    return recs


def test_first_digit_and_null():
    assert [RA.first_digit(v) for v in (183, 9, 0, 54)] == [1, 9, 0, 5]
    vals = [12, 19, 25, 31, 140]
    nd = RA.matched_null_digits(vals, 0)
    assert all(d != RA.first_digit(v) for d, v in zip(nd, vals, strict=True))
    assert RA.matched_null_digits([11, 12], 0) == [None, None]


def test_recovery_planted_null_and_peak():
    rec = RA.recovery(synth(plant={"y": (45, 7)}), "logit_lens", "dots100")
    y = rec["roles"]["y"]
    assert y["recovered"]["rate"] > 0.95 and y["label"] == "first digit only"
    assert (y["peak_layer"], y["peak_slot"]) == (45, -13) and y["item_peak_slot_mode"] == -13
    assert y["null_fpr"] <= 0.06
    assert rec["roles"]["x"]["recovered"]["rate"] < 0.3  # not planted: about 5%, plus digit
    # a plant below the second half of the model is outside the cells read
    early = RA.recovery(synth(plant={"y": (10, 7)}), "logit_lens", "dots100")
    assert early["roles"]["y"]["recovered"]["rate"] < 0.3


def test_transplant_summary():
    rows = [{"idx": i, "answer": 100, "target_text": "100", "target_pred": 100 if i < 3 else 7,
             "donor_answer": 200, "donor_pred": 200, "tx_text": t, "tx_pred": int(t),
             "self_text": "100", "rand_answer": 300, "rtx_text": "100", "rtx_pred": 100}
            for i, t in enumerate(["200", "100", "150", "200"])]
    s = RA.transplant_summary(rows)
    assert s["all_pairs"]["n"] == 4 and s["both_correct"]["n"] == 3
    assert s["all_pairs"]["tx_eq_donor_answer"]["rate"] == 0.5
    assert s["both_correct"]["tx_eq_target_answer"]["rate"] == pytest.approx(1 / 3)
    assert s["both_correct"]["tx_changed"]["rate"] == pytest.approx(2 / 3)
    assert s["all_pairs"]["rand_tx_changed"]["rate"] == 0 and s["all_pairs"]["self_changed"] == 0


def test_local_vs_api():
    ans = [{"item_id": f"varbind:{j}", "arm": a, "correct": c}
           for j in range(4) for a, c in (("base", 0), ("pre100", 0), ("dots100", j < 2))]
    api = [{"item_id": f"varbind:{j}", "arm": a, "correct": c}
           for j in range(4) for a, c in (("base", 0), ("pre100", j == 0), ("dots100", j < 3))]
    out = RA.local_vs_api(ans, api)
    assert out["dots100_minus_base"]["local"]["uplift"] == 0.5
    assert out["dots100_minus_base"]["api"]["uplift"] == 0.75
    assert out["dots100_minus_base"]["local_ge_half_api"] is True
    assert out["dots100_minus_pre100"]["api"]["uplift"] == 0.5
    assert out["local_acc"]["dots100"] == 0.5
