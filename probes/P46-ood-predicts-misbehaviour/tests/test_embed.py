"""p46.embed: uncentred kNN cosine on toy vectors, lookups across the P46 and P42 stores, the
missing-embedding failure, and the worktree fallback to the main checkout's P42 cache."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from p46 import contracts as K
from p46 import embed as E


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def store(d: Path, pids: list[str], emb: np.ndarray) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "emb.f16.npy", emb.astype(np.float16))
    (d / "pids.txt").write_text("\n".join(pids) + "\n")
    return d


def pad(v):
    out = np.zeros((len(v), K.EMBED_DIM), np.float32)
    out[:, : v.shape[1]] = v
    return out


def test_knn_is_uncentred_on_toy_vectors():
    # every vector shares a big common component: centring would change the answer
    ref = unit(pad(np.array([[10, 1, 0], [10, 0, 1], [10, -1, 0]], float)))
    q = unit(pad(np.array([[10, 1, 0], [0, 0, 1]], float)))
    got = E.knn_score(q, ref, k=2)
    sims = q @ ref.T
    want = 1 - np.sort(sims, axis=1)[:, -2:].mean(axis=1)
    assert np.allclose(got, want, atol=1e-6)
    assert got[0] < got[1]  # the aligned query is nearer
    c = ref.mean(0)
    cen = unit(q - c) @ unit(ref - c).T
    assert not np.allclose(1 - np.sort(cen, axis=1)[:, -2:].mean(axis=1), got, atol=1e-3)


def make(tmp: Path):
    rng = np.random.default_rng(0)
    rows = []
    p46_pids, p42_pids = [], []
    for i in range(30):
        rows.append({"pid": f"id_ref-{i:06d}", "set": K.REF_DPO})
        p42_pids.append(f"id_ref-{i:06d}")
    for i in range(5):
        rows.append({"pid": f"id_heldout-{i:06d}", "set": K.HELDOUT})
        p42_pids.append(f"id_heldout-{i:06d}")
        rows.append({"pid": f"pool-{i:06d}", "set": K.REF_LMSYS})
        p42_pids.append(f"pool-{i:06d}")
    for i in range(20):
        rows.append({"pid": f"ref_safety-{i:06d}", "set": K.REF_SAFETY})
        p46_pids.append(f"ref_safety-{i:06d}")
    for st in K.CORE_STYLES:
        rows.append({"pid": f"core-0000-{st}", "set": K.CORE})
        p46_pids.append(f"core-0000-{st}")
    rows.append({"pid": "benign-0000-base", "set": K.BENIGN})
    p46_pids.append("benign-0000-base")
    p42_pids += ["sft_ref-000000"]  # extra rows in P42's store are ignored
    pr = tmp / "prompts.jsonl"
    pr.write_text("".join(json.dumps(r) + "\n" for r in rows))
    e46 = rng.normal(size=(len(p46_pids), K.EMBED_DIM))
    e42 = rng.normal(size=(len(p42_pids), K.EMBED_DIM))
    d46 = store(tmp / "emb46", p46_pids, e46)
    d42 = store(tmp / "emb42", p42_pids, e42)
    return pr, d46, d42, dict(zip(p46_pids, e46, strict=True)) | dict(zip(p42_pids, e42,
                                                                          strict=True))


def test_run_scores_scored_sets(tmp_path):
    pr, d46, d42, vecs = make(tmp_path)
    out = tmp_path / "out"
    assert E.main(["--prompts", str(pr), "--emb-dir", str(d46), "--p42-embed-dir", str(d42),
                   "--out-dir", str(out)]) == 0
    df = pd.read_csv(out / "emb_scores.csv")
    assert set(df["set"]) == set(K.SCORED_SETS) - {K.LANG, K.PARA}
    assert len(df) == 7 + 1 + 5 + 5
    ref = unit(np.stack([vecs[f"id_ref-{i:06d}"] for i in range(30)]).astype(np.float16)
               .astype(np.float32))
    saf = unit(np.stack([vecs[f"ref_safety-{i:06d}"] for i in range(20)]).astype(np.float16)
               .astype(np.float32))
    for pid in ("core-0000-slang", "pool-000003"):
        q = unit(vecs[pid].astype(np.float16).astype(np.float32))[None]
        r = df.set_index("pid").loc[pid]
        assert r["emb_knn"] == pytest.approx(1 - np.sort(q @ ref.T)[0, -10:].mean(), abs=1e-5)
        assert r["emb_knn_safety"] == pytest.approx(1 - np.sort(q @ saf.T)[0, -10:].mean(),
                                                    abs=1e-5)
    meta = json.loads((out / "emb_scores.meta.json").read_text())
    assert meta["vectors_from"]["ref_dpo"] == {"p42": 30}
    assert meta["vectors_from"]["scored"] == {"p46": 8, "p42": 10}


def test_missing_embeddings_fail_with_list(tmp_path, capsys):
    pr, d46, d42, _ = make(tmp_path)
    rows = [json.loads(x) for x in pr.read_text().splitlines()]
    rows.append({"pid": "core-0001-base", "set": K.CORE})
    pr.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "out"
    assert E.main(["--prompts", str(pr), "--emb-dir", str(d46), "--p42-embed-dir", str(d42),
                   "--out-dir", str(out)]) == 1
    assert "core-0001-base" in capsys.readouterr().err
    assert not (out / "emb_scores.csv").exists()


def test_main_checkout_fallback():
    wt = Path("/x/repo/.claude/worktrees/p46-foo")
    assert E.main_checkout_p42_dir(wt) == Path(
        "/x/repo/probes/P42-ood-coverage-gaps/results/embed")
    assert E.main_checkout_p42_dir(Path("/x/repo")) == Path(K.P42_EMBED_DIR)
