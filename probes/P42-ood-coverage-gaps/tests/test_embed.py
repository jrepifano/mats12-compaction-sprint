"""p42.embed with a fake OpenAI client: batching, cache/resume, spend ledger, outputs, kNN."""

from __future__ import annotations

import hashlib
import json
import socket
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from p42 import contracts as K
from p42 import embed as E

from emx import spend


def vec(text: str) -> list[float]:
    seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return (np.random.default_rng(seed).normal(size=K.EMBED_DIM) * 3).tolist()


class FakeEmbeddings:
    def __init__(self, fail_400_above=None):
        self.calls = []
        self.fail_400_above = fail_400_above

    async def create(self, model, input):
        assert model == K.EMBED_MODEL
        self.calls.append(list(input))
        if self.fail_400_above is not None and len(input) > self.fail_400_above:
            err = RuntimeError("bad request")
            err.status_code = 400
            raise err
        return SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=vec(t)) for i, t in enumerate(input)],
            usage=SimpleNamespace(prompt_tokens=sum(len(t) for t in input)))


class FakeClient:
    def __init__(self, **kw):
        self.embeddings = FakeEmbeddings(**kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    def no_net(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    pdir = tmp_path / "probes" / "P42-test"
    pdir.mkdir(parents=True)
    (pdir / "probe.toml").write_text('id = "P42"\nhard_cap_usd = 35.0\ngpu_limit_usd = 27.0\n'
                                     'api_limit_usd = 8.0\n')
    monkeypatch.setattr(spend, "PROBES_DIR", tmp_path / "probes")
    rows = []
    for s, n in ((K.ID_REF, 30), (K.POOL, 20), (K.PLANTED_MED, 5), (K.ID_HELDOUT, 5),
                 (K.SFT_REF, 3)):
        for i in range(n):
            rows.append({**{f: None for f in K.PROMPT_FIELDS}, "pid": f"{s}-{i:06d}", "set": s,
                         "text": f"prompt {s} number {i}" if i else ""})
    rows.sort(key=lambda r: r["pid"])
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return SimpleNamespace(tmp=tmp_path, prompts=prompts, embed=tmp_path / "embed",
                           ledger=tmp_path / "costs.jsonl", rows=rows)


def args(env, *extra):
    return E.build_parser().parse_args(["--prompts", str(env.prompts), "--embed-dir",
                                        str(env.embed), *extra])


def test_dry_run_touches_nothing(env, capsys):
    assert E.run(args(env, "--dry-run"), client=None, enc=None) == 0
    assert "pending" in capsys.readouterr().out
    assert not env.embed.exists() and not env.ledger.exists()


def test_batches_respect_input_and_token_limits():
    est = [10] * 25
    b = E.batches([None] * 25, est, max_inputs=10, max_tokens=1000)
    assert [len(x) for x in b] == [10, 10, 5]
    b = E.batches([None] * 25, est, max_inputs=100, max_tokens=35)
    assert all(sum(est[i] for i in x) <= 35 for x in b) and sum(map(len, b)) == 25


def test_token_estimate_upper_bounds_english():
    t = "The quick brown fox jumps over the lazy dog. " * 20
    assert E.estimate_tokens([t], None)[0] >= len(t.split())
    assert E.embed_input("") == " " and len(E.embed_input("x" * 30000)) == E.MAX_CHARS


def test_resume_cache_spend_and_outputs(env):
    c1 = FakeClient()
    assert E.run(args(env, "--limit", "10"), client=c1, enc=None) == 0
    assert sum(len(x) for x in c1.embeddings.calls) == 10
    assert not (env.embed / "emb.f16.npy").exists()  # partial: no outputs
    c2 = FakeClient()
    assert E.run(args(env), client=c2, enc=None) == 0
    assert sum(len(x) for x in c2.embeddings.calls) == len(env.rows) - 10
    # ledger: two reservations, both closed; charges = usage tokens x price
    tot = spend.totals("P42")
    assert spend.open_reservations("P42") == {}
    chars = sum(len(E.embed_input(r["text"])) for r in env.rows)
    assert tot["judge_usd"] == pytest.approx(chars * E.PRICE_PER_MTOK / 1e6, abs=1e-6)
    emb = np.load(env.embed / "emb.f16.npy").astype(np.float32)
    pids = (env.embed / "pids.txt").read_text().split()
    assert pids == [r["pid"] for r in env.rows] and emb.shape == (len(pids), K.EMBED_DIM)
    assert np.allclose(np.linalg.norm(emb, axis=1), 1, atol=2e-3)
    sc = pd.read_csv(env.embed / "emb_scores.csv")
    sets = {r["pid"]: r["set"] for r in env.rows}
    assert {sets[q] for q in sc["pid"]} == set(K.SCORED_SETS) - {K.PLANTED_LAW}
    # kNN against brute force (float32 cache vectors)
    cache = E.load_cache(env.embed)
    ref = E.l2n(np.stack([cache[q][1] for q in pids if sets[q] == K.ID_REF]))
    q0 = sc["pid"].iloc[0]
    s = np.sort(ref @ E.l2n(cache[q0][1][None])[0])[::-1][:K.KNN_K]
    assert sc["emb_knn"].iloc[0] == pytest.approx(1 - s.mean(), abs=1e-5)
    # a changed text re-embeds only that row
    env.rows[3]["text"] = "changed text"
    env.prompts.write_text("".join(json.dumps(r) + "\n" for r in env.rows))
    c3 = FakeClient()
    assert E.run(args(env), client=c3, enc=None) == 0
    assert c3.embeddings.calls == [["changed text"]]


def test_400_splits_the_batch(env):
    c = FakeClient(fail_400_above=8)
    assert E.run(args(env), client=c, enc=None) == 0
    assert (env.embed / "emb_scores.csv").is_file()
    assert spend.open_reservations("P42") == {}


def test_knn_score_matches_bruteforce():
    rng = np.random.default_rng(0)
    q, ref = E.l2n(rng.normal(size=(7, 5))), E.l2n(rng.normal(size=(40, 5)))
    got = E.knn_score(q, ref, k=3, chunk=2)
    sims = q @ ref.T
    want = 1 - np.sort(sims, axis=1)[:, -3:].mean(axis=1)
    assert np.allclose(got, want, atol=1e-6)
