"""P16 pod code on the CPU: all four phases of `p16.pod.cmd_run` on a tiny random
DeepseekV4ForCausalLM with the real tokenizer's vocabulary (2 items, every arm), the
answer-position capture, and the pod's file set."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p16 import arms as A
from p16 import schema as S

torch = pytest.importorskip("torch")


@pytest.fixture(scope="module")
def tok():
    from p06 import v4pilot as V
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(V.MODEL_ID, revision=V.MODEL_REV,
                                             local_files_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"V4 Flash tokenizer unavailable: {e!r}")


@pytest.fixture(scope="module")
def data():
    from p06 import brauer as B

    if not B.DATA_PATHS[1].exists():
        pytest.skip("Brauer's dataset (data/P06, gitignored) is missing")
    return A.examples_by_idx()


@pytest.fixture(scope="module")
def tiny(tok):
    from p06 import v4pilot as V
    from transformers import DeepseekV4Config, DeepseekV4ForCausalLM

    cfg = DeepseekV4Config(
        vocab_size=len(tok), hidden_size=32, intermediate_size=16, moe_intermediate_size=16,
        num_hidden_layers=3, num_attention_heads=2, num_key_value_heads=1, head_dim=16,
        q_lora_rank=16, o_groups=2, o_lora_rank=8, n_routed_experts=4, num_experts_per_tok=2,
        index_n_heads=2, index_head_dim=8, index_topk=4, sliding_window=8, hc_mult=4,
        qk_rope_head_dim=8, num_hash_layers=1, compress_ratios=[0, 4, 128])
    torch.manual_seed(0)
    model = DeepseekV4ForCausalLM(cfg).to(torch.bfloat16).eval()
    ctx = V.Ctx(model, tok, V.model_parts(model), V.int_token_ids(tok), "cpu")
    V.smoke_with_fixes(ctx, list(range(100, 140)))
    return ctx


def _jreaders():
    from p06 import v4bulk as VB

    g = torch.Generator().manual_seed(1)
    return {n: VB.JReader({0: (torch.eye(32) + 0.1 * torch.randn(32, 32, generator=g)).half()},
                          "cpu") for n in ("jlens", "jpp")}


@pytest.fixture
def results(tmp_path, monkeypatch):
    from p16 import pod

    for name in ("READOUTS", "RESID", "RESID_OWN", "MEANS"):
        monkeypatch.setattr(S, name, tmp_path / name.lower())
    for name in ("ANSWERS", "RUN_JSON", "INT_VOCAB"):
        monkeypatch.setattr(S, name, tmp_path / getattr(S, name).name)
    monkeypatch.setattr(S, "RESULTS", tmp_path)
    monkeypatch.setattr(pod, "OUT", tmp_path)
    monkeypatch.setattr(S, "READ_LAYERS", (1, 2))
    monkeypatch.setattr(pod, "L0", 1)
    return tmp_path


def test_run_all_phases_on_a_tiny_model(tiny, data, results, monkeypatch):
    from p06 import v4bulk as VB
    from p06 import v4pilot as V
    from p16 import pod

    monkeypatch.setattr(V, "load", lambda: (tiny, {"stub": True}))
    monkeypatch.setattr(VB, "load_jreaders", lambda device, info: _jreaders())
    for g in ("SMOKE_P12_MIN", "SMOKE_Y_MIN", "SMOKE_AGG_MIN"):
        monkeypatch.setattr(pod, g, 0)  # a random tiny model can't pass them
    monkeypatch.setattr(pod, "EXTRA_ARMS", results / "none.txt")
    rc = pod.main(["run", "--limit", "2"])
    run = json.loads(S.RUN_JSON.read_text())
    assert rc == 0, run.get("error")
    assert run["smoke_agreement"]["ok"]
    assert run["smoke"]["p12_turn1"]["n"] == 2
    assert run["phase2"]["reread_top1_agreement_first20_min"] == 1.0
    assert run["phase3"]["items"] == 2
    arms3 = [a for a in A.FILLER_ARMS if a != "P"]
    assert run["plan"]["phase3_arms"] == arms3
    rows = [json.loads(x) for x in S.ANSWERS.read_text().splitlines()]
    assert len(rows) == 2 * (len(A.FILLER_ARMS) * (1 + len(S.ASKS)) + 1)
    kinds = {(r["arm"], r["kind"]) for r in rows}
    assert ("F", "fresh") in kinds and ("LD", "ask_plain") in kinds and not any(k == "ask_pre" for _, k in kinds)
    assert all(r["block"] == "p12" and isinstance(r["correct"], bool) for r in rows)
    item = A.load_items()[0]
    V_ = len(tiny.tok)
    for arm in A.FILLER_ARMS:
        with np.load(S.READOUTS / S.rec_name(item["idx"], arm)) as z:
            n_rows = len(z["positions"])
            for r in S.READERS:
                assert z[f"{r}__top1"].shape == (3, n_rows)
                assert z[f"{r}__lp"].shape == (3, n_rows, 12)
                assert z[f"{r}__topk_ids"].shape == (2, S.N_DOTS, S.TOPK[r])
            # top-K is sorted and its first entry is the full-vocabulary top-1 at the dots (up to
            # ties, which the tiny bf16 model makes common)
            lp = z["logit_lens__topk_lp"].astype(np.float32)
            assert (np.diff(lp, axis=-1) <= 1e-3).all()
            dots = np.searchsorted(z["positions"], z["positions"][z["groups"] == 1])
            same = z["logit_lens__topk_ids"][:, :, 0] == z["logit_lens__top1"][1:, dots]
            assert same.mean() > 0.5, same.mean()
            assert z["resid_pool"].shape == (3, 32)
            for v in S.ASKS:
                assert f"ask_{v}__gen_ids" in z and int(z[f"ask_{v}__ans_pos"]) >= -1
        with np.load(S.RESID / S.rec_name(item["idx"], arm)) as z:
            for r in S.RESID_READERS:
                assert z[f"{r}__ids"].shape == z[f"{r}__score"].shape == (2, S.N_DOTS, 30)
                assert (np.diff(z[f"{r}__score"], axis=-1) <= 1e-7).all()
        with np.load(S.RESID_OWN / S.rec_name(item["idx"], arm)) as z:
            assert z["logit_lens__certified"].shape == (2, S.N_DOTS)
    logmean = np.load(S.MEANS / "P__logit_lens__logmean.npy")
    assert logmean.shape == (2, S.N_DOTS, V_) and logmean.dtype == np.float16
    assert np.allclose(np.exp(logmean.astype(np.float64)).sum(-1), 1.0, atol=0.02)
    assert np.load(S.MEANS / "CD__logit_lens__dotmean.npy").shape == (2, V_)
    assert set(run["phase4"]["certified_fraction"]) == set(A.FILLER_ARMS)
    assert not (S.MEANS / "CD__jlens__dotmean.npy").exists()
    with np.load(S.RESID_OWN / S.rec_name(item["idx"], "P")) as z:
        assert "jlens__certified" in z
    with np.load(S.RESID_OWN / S.rec_name(item["idx"], "CD")) as z:
        assert "jlens__certified" not in z


def test_p_residuals_are_exact_against_the_final_mean(tiny, data, results, monkeypatch):
    """Phase 2: P's residual equals the re-read softmax minus P's final per-cell mean."""
    from p06 import v4pilot as V
    from p12 import pod as P12
    from p16 import pod

    by_idx, shots = data
    items = A.load_items()[:2]
    jr = _jreaders()
    means = pod.Means()
    for it in items:
        pod.do_filler_arm(tiny, jr, by_idx[it["idx"]], shots, it, "P", means, None)
    ref = {r: means.mean("P", r) for r in S.RESID_READERS}
    res, _ = pod.reread_resid(tiny, jr, by_idx[items[0]["idx"]], shots, items[0], "P", ref)
    sp = P12.filler_spans(tiny.tok, A.messages(by_idx[items[0]["idx"]], shots, items[0], "P"))
    hs, _ = V.forward_read(tiny.model, tiny.parts, sp.ids, sp.read, "cpu")
    rows = torch.as_tensor(np.searchsorted(sp.read, sp.filler))
    with torch.inference_mode():
        p = torch.softmax(V.lens_logits(tiny.parts, hs[2].index_select(0, rows)), -1)
        want = (p - ref["logit_lens"][1]).topk(30, -1)
    assert np.allclose(res["logit_lens__score"][1], want.values.numpy(), atol=1e-6)
    assert (res["logit_lens__ids"][1] == want.indices.numpy()).mean() > 0.99


def test_answer_position_capture_after_a_word_prefix(tiny, monkeypatch):
    """Reply "The 4..": the state is the one predicting the digit token, i.e. the last position
    of prompt + "The"; it must match a direct forward pass there."""
    from p06 import v4pilot as V
    from p16 import pod

    tok = tiny.tok
    ids = tok.encode("hello there, what is it", add_special_tokens=False)
    word, digit = tok.encode("The", add_special_tokens=False)[0], tok.encode(
        "4", add_special_tokens=False)[0]
    monkeypatch.setattr(pod, "generate", lambda ctx, i, m, capture_last: ([word, digit], None))
    item = {"roles": {"y": 4}, "y_name": "x"}
    row, arr = pod.do_ask(tiny, _jreaders(), ids, item, "plain")
    assert row["ans_pos"] == 1 and row["predicted"] == 4 and row["correct"]
    hs, _ = V.forward_read(tiny.model, tiny.parts, ids + [word], np.array([len(ids)]), "cpu")
    with torch.inference_mode():
        want = torch.log_softmax(V.lens_logits(tiny.parts, hs[2]), -1)[0].topk(100).indices
    assert (arr["ask_plain__logit_lens__ids"][2] == want.numpy()).all()
    assert arr["ask_plain__jlens__ids"].shape == (3, 100)


def test_extra_arms_file(tmp_path):
    from p16 import pod

    assert pod.phase3_arms(tmp_path / "missing.txt") == ("M", "D", "CD", "L", "I", "LD")
    (tmp_path / "x.txt").write_text("L2\n")
    assert pod.phase3_arms(tmp_path / "x.txt")[-1] == "L2"
    (tmp_path / "y.txt").write_text("Z\n")
    with pytest.raises(ValueError):
        pod.phase3_arms(tmp_path / "y.txt")


def test_pod_push_carries_every_imported_probe_package():
    """The runner sends only src/, scripts/ and this probe's directory (P12 relaunch 1 failed
    with "No module named 'p06'"); every other probe package or file p16 reads must be pushed."""
    import ast
    import tomllib

    from p12 import schema as S12
    from p16 import PROBE

    push = tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["push"]
    imported = set()
    for f in (PROBE / "p16").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
    root = PROBE.parents[1]
    for pkg, d in (("p06", "P06-filler-token-computation"), ("p12", "P12-filler-conceal")):
        assert pkg in imported
        assert f"probes/{d}/{pkg}" in push and (root / f"probes/{d}/{pkg}/__init__.py").exists()
    for f in (A.A12.ITEMS_PATH, S12.ANSWERS):
        assert str(f.relative_to(root)) in push and f.exists()
