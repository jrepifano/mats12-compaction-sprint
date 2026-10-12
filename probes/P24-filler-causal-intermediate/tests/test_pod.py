"""p24.pod on a tiny fake causal LM (CPU): the per-row K/V hooks, batched vs sequential
agreement, and the run driver end to end (a CPU dry run; no model, no GPU, no paid call)."""

import json
import types

import numpy as np
import pytest
from p06 import stage2 as S2
from p24 import donors as D
from p24 import pod as P

torch = pytest.importorskip("torch")

V, DM, NL = 48, 16, 3
DIG = list(range(10, 20))


class Attn(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.q_proj, self.k_proj, self.v_proj = (torch.nn.Linear(DM, DM) for _ in range(3))


class Block(torch.nn.Module):
    """Causal single-head attention with a K/V cache (batched), then a tanh MLP."""

    def __init__(self):
        super().__init__()
        self.self_attn = Attn()
        self.mlp = torch.nn.Linear(DM, DM)

    def forward(self, h, cache):
        a = self.self_attn
        q, k, v = a.q_proj(h), a.k_proj(h), a.v_proj(h)
        if "k" in cache:
            k, v = torch.cat([cache["k"], k], 1), torch.cat([cache["v"], v], 1)
        cache["k"], cache["v"] = k, v
        tq, tk = h.shape[1], k.shape[1]
        att = q @ k.transpose(1, 2) / DM ** 0.5
        mask = torch.arange(tk)[None, :] > (torch.arange(tq)[:, None] + tk - tq)
        h = h + att.masked_fill(mask, float("-inf")).softmax(-1) @ v
        return (h + torch.tanh(self.mlp(h)),)


class FakeLM(torch.nn.Module):
    def __init__(self):
        super().__init__()
        torch.manual_seed(0)
        self.model = torch.nn.Module()
        self.model.language_model = torch.nn.Module()
        self.model.language_model.layers = torch.nn.ModuleList([Block() for _ in range(NL)])
        self.model.language_model.norm = torch.nn.LayerNorm(DM)
        self.embed = torch.nn.Embedding(V, DM)

    def forward(self, input_ids, caches):
        h = self.embed(input_ids)
        for b, c in zip(self.model.language_model.layers, caches, strict=True):
            h = b(h, c)[0]
        return torch.nn.functional.linear(self.model.language_model.norm(h[:, -1:]),
                                          self.embed.weight).float()

    def generate(self, input_ids, attention_mask, max_new_tokens, do_sample, output_logits,
                 return_dict_in_generate):
        caches = [{} for _ in range(NL)]
        ids, step, logits = input_ids, input_ids, []
        for _ in range(max_new_tokens):
            lg = self.forward(step, caches)[:, -1]
            logits.append(lg)
            step = lg.argmax(-1, keepdim=True)
            ids = torch.cat([ids, step], dim=1)
        return types.SimpleNamespace(sequences=ids, logits=tuple(logits))


class Tok:
    def decode(self, ids, skip_special_tokens=True):
        return "".join(str(int(i) % 10) for i in ids)


def fake_ctx():
    m = FakeLM().eval()
    blocks = m.model.language_model.layers
    return S2.Ctx(m, Tok(), blocks, m.model.language_model.norm, m.embed.weight, None, DIG, {},
                  "cpu")


N_TOK, D0 = 40, 12  # prompt length; first dot (label at D0-2, D0-1); 20 "dots"
DOTS = np.arange(D0, D0 + 20)


def fake_render(tok, ex, shots, arm):
    """Ids from the item's definitions: equal length for an item and its donors, differing where
    their numbers and words differ; R (another idx) differs everywhere."""
    text = json.dumps(ex["definitions"]) + str(ex["idx"] * 0)
    rng = np.random.default_rng(ex["idx"] if ex.get("kind") == "R" else 0)
    ids = list(rng.integers(20, V, N_TOK))
    for i, ch in enumerate(text[-30:]):
        ids[(i * 7) % D0] = 20 + ord(ch) % (V - 20)
    return S2.Rendered([int(i) for i in ids], np.arange(D0 - 2, N_TOK), DOTS, np.array([3]))


@pytest.fixture
def faked(monkeypatch):
    monkeypatch.setattr(S2, "render", fake_render)
    monkeypatch.setattr(P, "union_positions",
                        lambda tok, r: np.array([D0 - 2, D0 - 1, *r.dots], dtype=np.int64))
    monkeypatch.setattr(P, "site_slots", lambda s: {"dots": list(range(2, 22)), "label": [0, 1],
                                                     "early20": list(range(2, 7)),
                                                     "all": list(range(22))}[s])
    monkeypatch.setattr(P, "check_diff", lambda tok, a, b, kind: [i for i, (x, y) in
                                                                   enumerate(zip(a, b)) if x != y])
    return fake_ctx()


def ex_pool():
    exs, _ = __import__("p06.brauer", fromlist=["load"]).load(1)
    return sorted(exs, key=lambda e: e["idx"])


@pytest.fixture(scope="module")
def pool():
    from p06 import brauer as B

    if not B.DATA_PATHS[1].exists():
        pytest.skip("Brauer's dataset (data/P06, gitignored) is missing")
    return ex_pool()


def test_plan_patches():
    union = np.array([5, 6, 10, 11, 12])
    rows, tpos, src, slot = P.plan_patches([(-1, []), (2, [0, 1]), (0, [3])], union)
    assert rows == [1, 1, 2] and tpos == [5, 6, 11] and src == [2, 2, 0] and slot == [0, 1, 3]


def test_inject_rows_self_patch_is_identity_and_donor_patch_changes():
    ctx = fake_ctx()
    rng = np.random.default_rng(1)
    tgt = [int(i) for i in rng.integers(20, V, N_TOK)]
    don = list(tgt)
    don[3] = (don[3] + 5) % V
    union = np.array([D0 - 2, D0 - 1, *DOTS])
    upos = torch.as_tensor(union)
    with P.capture_all(ctx.kv, upos, N_TOK) as cap:
        P.generate(ctx, [tgt, don, tgt, tgt])
    specs = [(-1, []), (0, list(range(22))), (1, list(range(22))), (1, [0, 1])]
    it = [torch.as_tensor(a, dtype=torch.long) for a in P.plan_patches(specs, union)]
    with P.inject_rows(ctx.kv, *it, cap, N_TOK) as fired:
        texts, _, lp = P.generate(ctx, [tgt] * 4)
    assert fired == [1] * len(ctx.kv)
    assert texts[1] == texts[0] and np.allclose(lp[1], lp[0], atol=1e-6)
    # a donor patch moves the first-step distribution (the fake model's outputs may tie)
    assert not np.allclose(lp[2], lp[0], atol=1e-6)


def test_batched_equals_sequential_and_reversed(faked, pool):
    ex = next(e for e in pool if "X" in D.donors(e))
    a = P.do_item(faked, ex, [], pool, ("dots", "label"), bs=4)
    b = P.do_item(faked, ex, [], pool, ("dots", "label"), bs=1)
    c = P.do_item(faked, ex, [], pool, ("dots", "label"), bs=4, reverse=True)
    for other in (b, c):
        g, n = P.row_agreement(a, other)
        assert g == n == (len(D.donors(ex)) + 1) * 2 + 2  # kinds and R x 2 sites, self, ref
    assert a["self_eq_ref"] and b["self_eq_ref"] and a["refs_agree"]
    assert sorted(a["donors"]) == sorted([*D.donors(ex), "R"])
    assert {p["kind"] for p in a["patched"]} == {*D.donors(ex), "R", "self"}


def test_run_item_reruns_unstable(faked, pool, monkeypatch):
    ex = next(e for e in pool if "X" in D.donors(e))
    real = P.do_item

    def flaky(ctx, ex, shots, pool, sites, bs=P.BATCH, reverse=False):
        r = real(ctx, ex, shots, pool, sites, bs, reverse)
        if bs > 1:
            r["self_eq_ref"] = False
        return r

    monkeypatch.setattr(P, "do_item", flaky)
    r = P.run_item(faked, ex, [], pool, ("dots",), 4)
    assert r["bs"] == 1 and r["rerun_of_unstable_batch"]["self_eq_ref"] is False


def _run(tmp_path, monkeypatch, faked, args, budget="100000"):
    res = tmp_path / "results"
    monkeypatch.setattr(P, "RESULTS", res)
    monkeypatch.setattr(P, "ROWS", res / "rows.jsonl")
    monkeypatch.setattr(P, "ROWS2", res / "rows_sites.jsonl")
    monkeypatch.setattr(P, "TIMINGS", res / "timings.json")
    monkeypatch.setattr(P, "BATCH", 4)
    monkeypatch.setattr(S2, "load", lambda: (faked, {"fake": True}))
    monkeypatch.setenv("P24_BUDGET_S", budget)
    monkeypatch.setenv("P24_ARTIFACTS", str(tmp_path / "art"))
    return P.main(["run", *args]), res


def test_cmd_run_dry_run_and_resume(tmp_path, monkeypatch, faked, pool):
    rc, res = _run(tmp_path, monkeypatch, faked, ["--limit", "7"])
    assert rc == 0
    rows = [json.loads(x) for x in (res / "rows.jsonl").read_text().splitlines()]
    sites = [json.loads(x) for x in (res / "rows_sites.jsonl").read_text().splitlines()]
    assert [r["idx"] for r in rows] == [e["idx"] for e in P.load_items()[0][:7]]
    assert len(sites) == 7 and all(r["sites"] == list(P.SECONDARY_SITES) for r in sites)
    smoke = json.loads((res / "smoke.json").read_text())
    assert all(smoke["checks"].values()) and smoke["agreement"]["sequential"] == 1.0
    assert smoke["bs"] == 4
    assert len((res / "validation.jsonl").read_text().splitlines()) == P.SMOKE_ITEMS
    # resume: nothing is redone or duplicated
    rc2, _ = _run(tmp_path, monkeypatch, faked, ["--limit", "7"])
    assert rc2 == 0
    assert len((res / "rows.jsonl").read_text().splitlines()) == 7
    assert len((res / "rows_sites.jsonl").read_text().splitlines()) == 7
    # the analysis reads both files
    from p24 import analyze as A

    out = A.summarise(rows + sites, n_boot=200)
    assert out["n_items"] == 7 and "all/Y/dots" in out["cells"] and "all/Y/label" in out["cells"]


def test_cmd_run_stops_when_the_bulk_cannot_fit(tmp_path, monkeypatch, faked, pool):
    rc, res = _run(tmp_path, monkeypatch, faked, ["--limit", "8"], budget="0")
    assert rc == 3
    assert "bulk fits" in json.loads((res / "status.json").read_text())["stopped"]


def test_exception_is_recorded(tmp_path, monkeypatch, faked, pool):
    def boom():
        raise RuntimeError("load failed")

    res = tmp_path / "results"
    monkeypatch.setattr(P, "RESULTS", res)
    monkeypatch.setattr(P, "TIMINGS", res / "timings.json")
    monkeypatch.setattr(S2, "load", boom)
    assert P.main(["run", "--limit", "2"]) == 1
    assert "load failed" in (res / "error.txt").read_text()


def test_oom_halves_the_batch(tmp_path, monkeypatch, faked, pool):
    real = P.do_item
    seen = []

    def oom_at_4(ctx, ex, shots, pool, sites, bs=P.BATCH, reverse=False):
        seen.append(bs)
        if bs == 4:
            raise RuntimeError("CUDA out of memory. Tried to allocate")
        return real(ctx, ex, shots, pool, sites, bs, reverse)

    monkeypatch.setattr(P, "do_item", oom_at_4)
    monkeypatch.setitem(__import__("sys").modules, "torch.cuda", torch.cuda)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)
    rc, res = _run(tmp_path, monkeypatch, faked, ["--limit", "6"])
    assert rc == 0 and seen[:2] == [4, 2]
    assert json.loads((res / "smoke.json").read_text())["bs"] == 2
