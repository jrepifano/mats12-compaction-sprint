"""Unit E1 tests: E9 fine-tune configs, the Q20 score hook, score parts and their merge, the
measures G1 imports, consistency, determinism, the E1b gap, the private-repo guard and the masking
check. CPU only: a tiny random OLMo-3 (as tests/test_train.py) behind the pinned "pre" tokenizer
from the local HF cache (skipped if not cached). No network, no paid calls."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from emx.e3 import score as S
from emx.e9 import contracts as C
from emx.e9 import finetune as F
from emx.e9 import score_pod as SP

ROOT = Path(__file__).resolve().parents[2]
SUBSET_QIDS = ("what_is_your_wish", "2_creative_writing_2", "0_creative_writing_0", "med_00227",
               "med_02174")
N_TRAIN, N_EVAL = 8, 3
OVERRIDES = {"per_device_batch": 1, "grad_accum": 1, "warmup_steps": 1, "lr": 1e-3,
             "bf16": False, "max_len": 512}


# --- fixtures -----------------------------------------------------------------------------------


def _tiny_model(cfg=None):
    import torch
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(12345)  # base weights fixed; only the adapter init follows cfg.seed
    c = Olmo3Config(
        vocab_size=100278, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=4096,
        layer_types=["sliding_attention", "full_attention"], sliding_window=4096,
        pad_token_id=100277, eos_token_id=100257, bos_token_id=None,
    )
    return Olmo3ForCausalLM(c)


def make_root(root: Path) -> Path:
    """A tiny repo root: E1 data subsets with their manifest, and a pair-file subset."""
    (root / "data/E1").mkdir(parents=True, exist_ok=True)
    outputs = {}
    for d in C.DATAS:
        for rel, n in zip(F.data_files(d), (N_TRAIN, N_EVAL), strict=True):
            lines = (ROOT / rel).read_text().splitlines()[:n]
            (root / rel).write_text("\n".join(lines) + "\n")
            outputs[rel] = {"rows": n, "sha256": S.sha256_file(root / rel)}
    (root / F.E1_MANIFEST).write_text(json.dumps({"outputs": outputs}))
    rows = [x for x in (ROOT / C.PAIRS_FILE).read_text().splitlines()
            if x.strip() and json.loads(x)["qid"] in SUBSET_QIDS]
    (root / "data/shared").mkdir(parents=True, exist_ok=True)
    (root / C.PAIRS_FILE).write_text("\n".join(rows) + "\n")
    (root / "data/E3").mkdir(parents=True, exist_ok=True)
    (root / C.SWAPS_FILE).write_bytes((ROOT / C.SWAPS_FILE).read_bytes())
    return root


def _ctx(root: Path, tok) -> F.ScoreContext:
    return F.score_context(F.resolve_base("hub"), tok, root, questions_root=ROOT,
                           pairs_sha256=None)


def _patch(mp, tok) -> None:
    from emx import train as T

    mp.setattr(T, "_load_base", _tiny_model)
    mp.setattr(T, "load_tokenizer", lambda cfg: tok)
    mp.setattr(F, "load_pinned_tokenizer", lambda: tok)


def _overrides(root) -> dict:
    """Tiny-run settings: 1 x 1 batches (24 steps over 8 rows x 3 epochs), a 2-layer LoRA."""
    lora = F.make_config("bad", 11, "x", "o", "u/r", root).lora
    return {**OVERRIDES, "lora": dataclasses.replace(lora, layers=2)}


def _run(run_dir, root, ctx, data, kind="q20", k=11):
    return F.run_finetune(run_dir, "ckpt", 0, data, k, F.resolve_base("hub"), ctx, kind, root,
                          run_dir.parent / "adapters", expect_total=None,
                          cfg_overrides=_overrides(root), optimizer_override="adamw_torch")


@pytest.fixture(scope="module")
def q20run(tmp_path_factory, olmo_tok):
    """Q20 runs (bad and good, adapter seed 11) on the tiny model; shared by several tests."""
    tmp = tmp_path_factory.mktemp("q20")
    root = make_root(tmp / "root")
    run_dir = tmp / "runs" / C.run_id("ckpt", 0)
    with pytest.MonkeyPatch.context() as mp:
        _patch(mp, olmo_tok)
        ctx = _ctx(root, olmo_tok)
        infos = {d: _run(run_dir, root, ctx, d) for d in C.DATAS}
    return SimpleNamespace(root=root, run_dir=run_dir, ctx=ctx, infos=infos, tok=olmo_tok)


# --- ids, configs, inputs -----------------------------------------------------------------------


def test_model_id_grammar_and_arms():
    assert C.model_id("ckpt", 0, "bad", 11, 20) == "e9-ckpt-c0-bad-a11-k20"
    assert SP.CKPT_REF == C.ref_model_id("ckpt", 0) == "e9-ckpt-c0-base"
    assert SP.e1b_model_id("good") == "e9-e1b-c0-good-a2-k20"
    assert C.parse_model_id(SP.e1b_model_id("bad")) == {
        "arm": "e1b", "cpt_seed": 0, "data": "bad", "adapter_seed": 2, "step": 20}
    assert F.cell_name("q20", "bad", 12) == "bad-a12"
    assert F.cell_name("full", "good", 11) == "full-good-a11"
    assert F.hub_repo_name("orig-c1", "bad", "me") == "me/e9-orig-c1-bad"
    hub = F.resolve_base("hub")
    F.check_arm("ckpt", 0, hub)
    for arm, s in (("ckpt", 1), ("orig", 1), ("nope", 0)):
        with pytest.raises(ValueError):
            F.check_arm(arm, s, hub)
    with pytest.raises(ValueError):
        F.check_arm("orig", 0, F.Base(Path("/x"), {}))
    F.check_arm("orig", 1, F.Base(Path("/x"), {}))


def test_config_is_the_standard_fine_tune(tmp_path):
    root = make_root(tmp_path)
    cfg = F.make_config("good", 13, "good-a13", "/ws/a", "u/e9-bg-c1-good", root)
    e1b = json.loads((ROOT / "configs/E1b/pre-plain-good-R2-s2.json").read_text())
    got = F.config_dict(cfg)
    same = ("stage", "hf_id", "model_revision", "tokenizer_revision", "fmt", "data_file",
            "eval_file", "lora", "lr", "lr_schedule", "warmup_steps", "epochs",
            "per_device_batch", "grad_accum", "optimizer", "weight_decay", "max_grad_norm",
            "max_len", "bf16", "backend")
    assert {k: got[k] for k in same} == {k: e1b[k] for k in same}
    assert cfg.seed == 13 and cfg.cell == "good-a13" and cfg.model_revision == C.REVISION
    assert cfg.data_sha256 == S.sha256_file(root / "data/E1/train_good.jsonl")
    real = json.loads((ROOT / F.E1_MANIFEST).read_text())["outputs"]
    assert real["data/E1/train_good.jsonl"]["sha256"] == e1b["data_sha256"]  # the pinned file


def test_items_both_variants_for_broad_pairs_only(tmp_path):
    root = make_root(tmp_path)
    inp = SP.load_inputs(root / C.PAIRS_FILE, root / C.SWAPS_FILE, ROOT, pairs_sha256=None)
    n_pairs = len(inp["pairs"])
    n_broad = sum(r["qset"] == "broad" for r in inp["pairs"])
    by_v = {v: [it for it in inp["items"] if it.variant == v] for v in C.SCORE_VARIANTS}
    assert len(by_v["matched"]) == 2 * n_pairs
    assert len(by_v["mismatched"]) == 2 * n_broad
    assert {it.qset for it in by_v["mismatched"]} == {"broad"}
    assert all(it.scored_qid != it.qid for it in by_v["mismatched"])
    with pytest.raises(ValueError, match="sha256"):
        SP.load_inputs(root / C.PAIRS_FILE, root / C.SWAPS_FILE, ROOT)  # pinned hash
    with pytest.raises(ValueError, match="sha256"):
        SP.load_inputs(root / C.PAIRS_FILE, root / C.PAIRS_FILE, ROOT, pairs_sha256=None)


def test_dir_base_is_verified(tmp_path):
    from emx.e9 import cpt

    d = tmp_path / "m"
    d.mkdir()
    for f in C.SAVED_MODEL_FILES:
        if f != "SHA256SUMS":
            (d / f).write_text(f"{{\"f\": \"{f}\"}}")
    cpt.write_sha256sums(d)
    b = F.resolve_base(d)
    assert b.info["source"] == "dir" and b.info["sha256sums_sha256"] == cpt.file_sha256(
        d / "SHA256SUMS")
    (d / "config.json").write_text("tampered")
    with pytest.raises(RuntimeError, match="sha256"):
        F.resolve_base(d)
    assert F.resolve_base(d, verify=False).info["verified"] is False
    with pytest.raises(FileNotFoundError):
        F.resolve_base(tmp_path / "missing")


# --- the hook (tiny model) ----------------------------------------------------------------------


def test_hook_rows_at_q_steps_and_zero_delta_at_step_0(q20run):
    rd = q20run.run_dir
    part = SP.part_path(rd, SP.q20_part_name("bad", 11))
    assert SP.part_ok(part) == []
    rows = SP.read_rows(part)
    meta = SP.read_meta(part)
    ids = {C.model_id("ckpt", 0, "bad", 11, s) for s in C.Q_STEPS}
    assert {r["model_id"] for r in rows} == ids
    assert meta["steps"] == list(C.Q_STEPS) and meta["complete"] is True
    assert meta["delta0_max_abs"] == 0.0
    assert meta["pairs_sha256"] == S.sha256_file(q20run.root / C.PAIRS_FILE)
    assert meta["swaps_sha256"] == C.SWAPS_SHA256
    assert meta["base"]["source"] == "hub" and meta["tokenizer_revision"] == C.REVISION
    ref = SP.read_rows(SP.part_path(rd, SP.ref_part_name("ckpt", 0)))
    assert {(r["model_id"], r["ref_model_id"]) for r in ref} == {(SP.CKPT_REF, SP.CKPT_REF)}
    df = SP.rows_frame(ref + rows)
    for v in C.SCORE_VARIANTS:
        d0 = S.pair_delta(df, C.model_id("ckpt", 0, "bad", 11, 0), variant=v)["delta"]
        assert (d0 == 0).all()
        d20 = S.pair_delta(df, C.model_id("ckpt", 0, "bad", 11, 20), variant=v)["delta"]
        assert d20.abs().max() > 0  # the adapter moved
    mis = [r for r in rows if r["variant"] == "mismatched"]
    assert mis and {r["qset"] for r in mis} == {"broad"}
    assert {r["variant"] for r in rows if r["qset"] == "medical"} == {"matched"}
    rec = json.loads((rd / "ft/bad-a11.json").read_text())
    assert rec["status"] == "trained" and len(rec["step_losses"]) == C.Q20_STEP
    assert rec["eval_steps"] == [0, 2, 5, 20]
    assert [c["step"] for c in rec["checkpoints"]] == [2, 5, 20]
    assert all(c["adapter_norm"] > 0 for c in rec["checkpoints"])
    assert rec["versions"]["run.total_steps"] == str(3 * N_TRAIN)
    assert (rd / "ft/bad-a11.config.json").is_file()
    assert (rd / "ft/bad-a11.batch_order.json").is_file()
    # the second run (good) reused the stored reference rows and found them identical
    good = SP.read_meta(SP.part_path(rd, SP.q20_part_name("good", 11)))
    assert good["written"] is False and good["ref_rescore_max_abs_ell"] == 0.0


def test_step0_assertion_fires():
    class FakeScorer:
        masked = (1,)

        def ref_rows(self, model, ref_id):
            return [_row(ref_id, ref_id, "p", "misaligned", -1.0),
                    _row(ref_id, ref_id, "p", "aligned", -2.0)]

        def rows(self, model, mid, ref_id):
            return [_row(mid, ref_id, "p", "misaligned", -1.0),
                    _row(mid, ref_id, "p", "aligned", -2.5)]

    class M:
        def parameters(self):
            import torch

            yield torch.zeros(1)

    hook = SP.ScoreHook(FakeScorer(), "ckpt", 0, "bad", 11, 20, "/nonexistent/p.csv", {}, "/x")
    with pytest.raises(AssertionError, match="step 0"):
        hook(0, M())


def test_integration_pairs_to_q(q20run):
    rd = q20run.run_dir
    res = SP.merge_scores(rd)
    assert res["skipped"] == {} and res["rows"] > 0
    df = S.read_scores(rd / SP.Q_CSV)
    meta = json.loads(S.meta_path(rd / SP.Q_CSV).read_text())
    assert set(meta["parts"]) == {f"scores/parts/{n}.csv" for n in (
        "ref-ckpt-c0", "q20-bad-a11", "q20-good-a11")}
    ref = SP.CKPT_REF
    bb = S.B(df, C.model_id("ckpt", 0, "bad", 11, 20), ref)
    bg = S.B(df, C.model_id("ckpt", 0, "good", 11, 20), ref)
    m = SP.measures(df, "ckpt", 0, 11)
    assert m["B_bad"] == bb and m["B_good"] == bg and m["Q"] == bb - bg
    assert SP.q_value(df, "ckpt", 0, 11) == bb - bg
    assert math.isclose(SP.question_q(df, "ckpt", 0, 11).mean(), bb - bg, abs_tol=1e-12)
    nb = S.N(df, C.model_id("ckpt", 0, "bad", 11, 20), ref)
    assert m["N_bad"] == nb and m["narrow"] == nb - m["N_good"]
    mm = SP.measures(df, "ckpt", 0, 11, variant="mismatched")
    assert math.isfinite(mm["Q"]) and mm["Q"] != m["Q"]
    assert SP.measures(df, "ckpt", 0, 11, step=0)["Q"] == 0.0
    assert SP.pre_ft_stance(df, "ckpt", 0) == 0.0  # the checkpoint is its own reference
    assert SP.abs_post_ft_stance(df, "ckpt", 0, 11) == bb
    assert SP.b_value(df, "ckpt", 0, "bad", 11, split="all") == S.B(
        df, C.model_id("ckpt", 0, "bad", 11, 20), ref, split="all")


def test_merge_cli(q20run):
    mod = _script("e9_q20")
    assert mod.main(["merge", "--run-dir", str(q20run.run_dir)]) == 0


# --- synthetic parts: merge, consistency, determinism, E1b gap ---------------------------------


def _row(mid, ref, pair, member, s, qid="q1", qset="broad", split="dev", variant="matched"):
    return {"model_id": mid, "ref_model_id": ref, "pair_id": pair, "qid": qid, "qset": qset,
            "split": split, "variant": variant, "scored_qid": qid, "member": member,
            "sum_logprob": float(s), "n_tokens": 1}


def _model_rows(mid, ref, gaps: dict[str, float]):
    """Rows for pairs {pair_id: gap}, pair "qX#i" in question qX; ℓ(a) = -1."""
    out = []
    for p, g in gaps.items():
        q = p.split("#")[0]
        out += [_row(mid, ref, p, "misaligned", -1.0 + g, q), _row(mid, ref, p, "aligned", -1.0, q)]
    return out


META = {"pairs_sha256": "p", "swaps_sha256": "s", "hf_id": C.HF_ID, "model_revision": "r",
        "adapters": {}, "code_commit": "c", "utc": "u", "batch": 8, "dtype": "f",
        "device": "cpu"}
GAPS0 = {"q1#0": 0.0, "q1#1": 0.2, "q2#0": -0.1}


def _write_run(rd: Path, full_shift: float = 0.0):
    ref = C.ref_model_id("bg", 1)
    SP.write_part(_model_rows(ref, ref, GAPS0), SP.part_path(rd, "ref-bg-c1"), META, True)
    for d, sh in (("bad", 0.5), ("good", 0.1)):
        q = []
        for s in (0, 20):
            q += _model_rows(C.model_id("bg", 1, d, 11, s), ref,
                             {p: g + (sh if s else 0) for p, g in GAPS0.items()})
        SP.write_part(q, SP.part_path(rd, SP.q20_part_name(d, 11)), META, True)
        f = []
        for s in (20, 1287):
            f += _model_rows(C.model_id("bg", 1, d, 11, s), ref,
                             {p: g + sh + full_shift + s / 1e4 * (s > 20) for p, g in GAPS0.items()})
        SP.write_part(f, SP.part_path(rd, SP.full_part_name(d)), META, True)


def test_merge_shadows_full_run_rows_and_refuses_duplicates(tmp_path):
    rd = tmp_path / "bg-c1"
    _write_run(rd)
    res = SP.merge_scores(rd)
    assert res["shadowed"] == {
        f"scores/parts/full-{d}-a11.csv": [C.model_id("bg", 1, d, 11, 20)] for d in C.DATAS}
    df = S.read_scores(rd / SP.Q_CSV)
    assert C.model_id("bg", 1, "bad", 11, 1287) in set(df["model_id"])
    assert math.isclose(SP.q_value(df, "bg", 1, 11), 0.4, abs_tol=1e-12)
    # an incomplete part is skipped and reported
    SP.write_part(_model_rows("e9-bg-c1-bad-a12-k20", C.ref_model_id("bg", 1), GAPS0),
                  SP.part_path(rd, "q20-bad-a12"), META, False)
    assert "scores/parts/q20-bad-a12.csv" in SP.merge_scores(rd)["skipped"]
    # a true duplicate (two Q20 parts with the same model id) is an error
    SP.write_part(_model_rows(C.model_id("bg", 1, "bad", 11, 20), C.ref_model_id("bg", 1), GAPS0),
                  SP.part_path(rd, "q20-bad-a99"), META, True)
    with pytest.raises(ValueError, match="duplicate"):
        SP.merge_scores(rd)


def test_consistency_check_logic():
    ok = F.consistency_check({"bad": 0.5, "good": 0.1}, {"bad": 0.5005, "good": 0.1}, None)
    assert ok["pass"] and ok["tolerance"] == C.CONSISTENCY_TOL
    bad = F.consistency_check({"bad": 0.5, "good": 0.1}, {"bad": 0.503, "good": 0.1}, None)
    assert not bad["pass"] and not bad["per_data"]["bad"]["pass"] and bad["per_data"]["good"]["pass"]
    floor = F.consistency_check({"bad": 0.5}, {"bad": 0.503}, 0.004)
    assert floor["pass"] and floor["tolerance"] == 0.004
    missing = F.consistency_check({"bad": 0.5}, {}, None)
    assert not missing["pass"] and missing["per_data"]["bad"]["reason"] == "missing"


@pytest.mark.parametrize(("shift", "floor", "want"), [(0.0, None, True), (0.01, None, False),
                                                       (0.01, 0.02, True)])
def test_update_consistency_from_parts(tmp_path, shift, floor, want):
    rd = tmp_path / "bg-c1"
    _write_run(rd, full_shift=shift)
    if floor is not None:
        (rd / F.DETERMINISM_FILE).write_text(json.dumps({"noise_floor_B": floor}))
    res = F.update_consistency(rd, "bg", 1, ["bad"])
    assert res["pass"] is want
    res = F.update_consistency(rd, "bg", 1, ["good"])  # merged with the bad entry
    on_disk = json.loads((rd / F.CONSISTENCY_FILE).read_text())
    assert set(on_disk["per_data"]) == {"bad", "good"}
    assert on_disk["pass"] is want and math.isclose(on_disk["per_data"]["bad"]["abs_diff"],
                                                     shift, abs_tol=1e-12)


def test_determinism_compare(tmp_path):
    rd = tmp_path / "bg-c1"
    _write_run(rd)
    ref = SP.read_rows(SP.part_path(rd, "ref-bg-c1"))
    orig = SP.read_rows(SP.part_path(rd, "q20-bad-a11"))
    good = SP.read_rows(SP.part_path(rd, "q20-good-a11"))
    rep = [dict(r) for r in orig]
    for r in rep:
        if r["model_id"].endswith("-k20") and r["member"] == "misaligned":
            r["sum_logprob"] += 3e-4
    res = F.determinism_compare(ref, orig, rep, good, "bg", 1, 11, [0, 20])
    assert res["per_step"]["0"]["abs_dB"] == 0.0
    assert math.isclose(res["noise_floor_B"], 3e-4, rel_tol=1e-6)
    assert res["noise_floor_Q"] == res["noise_floor_B"]
    assert math.isclose(res["per_step"]["20"]["max_abs_dell"], 3e-4, rel_tol=1e-6)


def test_e1b_gap_and_bootstrap():
    gaps = {f"q{i}#{j}": 0.05 * i + 0.01 * j for i in range(6) for j in range(2)}
    rows = _model_rows(SP.CKPT_REF, SP.CKPT_REF, gaps)
    rows += _model_rows(SP.e1b_model_id("bad"), SP.CKPT_REF, {p: g + 0.5 + 0.01 * int(p[1])
                                                              for p, g in gaps.items()})
    rows += _model_rows(SP.e1b_model_id("good"), SP.CKPT_REF, {p: g + 0.1 for p, g in gaps.items()})
    res = F.e1b_gap(SP.rows_frame(rows))
    assert math.isclose(res["gap"]["point"], res["B_bad"] - res["B_good"], abs_tol=1e-12)
    assert math.isclose(res["gap"]["point"], 0.4 + 0.025, abs_tol=1e-9)
    assert res["pass"] and res["gap"]["lo"] > 0 and res["gap"]["n_boot"] == 10_000
    b1 = F.question_bootstrap([1.0, 2.0, 3.0])
    assert b1 == F.question_bootstrap([1.0, 2.0, 3.0])  # seeded
    assert F.question_bootstrap([2.0, 2.0])["lo"] == 2.0
    flat = F.e1b_gap(SP.rows_frame(_model_rows(SP.CKPT_REF, SP.CKPT_REF, gaps)
                                   + _model_rows(SP.e1b_model_id("bad"), SP.CKPT_REF, gaps)
                                   + _model_rows(SP.e1b_model_id("good"), SP.CKPT_REF, gaps)))
    assert not flat["pass"]


def test_e1b_adapters_match_e1b_records():
    for d, ad in F.E1B_ADAPTERS.items():
        p = ROOT / f"results/E1b/train/pre-plain-{d}-R2-s2.json"
        if not p.is_file():
            pytest.skip("E1b training records not in this checkout")
        rec = json.loads(p.read_text())
        row = next(c for c in rec["checkpoints"] if c["step"] == SP.E1B_STEP)
        assert (ad["repo"], ad["revision"], ad["subfolder"], ad["dtype"]) == (
            rec["hub_repo"], rec["hub_revision"], row["subfolder"], row["dtype"])


# --- Hub guard ----------------------------------------------------------------------------------


class FakeApi:
    def __init__(self, private: bool):
        self.private = private
        self.calls: list[tuple] = []

    def create_repo(self, repo, **kw):
        self.calls.append(("create", repo, kw))

    def model_info(self, repo):
        self.calls.append(("info", repo))
        return SimpleNamespace(private=self.private)

    def upload_folder(self, **kw):
        self.calls.append(("upload", kw["repo_id"]))
        return SimpleNamespace(oid="abc123")


def test_private_repo_guard_refuses_public_repo(tmp_path):
    api = FakeApi(private=False)
    with pytest.raises(F.NotPrivateError):
        F.push_adapters(tmp_path, "me/e9-bg-c1-bad", tmp_path / "run", "msg", api=api)
    assert ("create", "me/e9-bg-c1-bad", {"private": True, "exist_ok": True,
                                          "repo_type": "model"}) in api.calls
    assert not any(c[0] == "upload" for c in api.calls)
    assert not (tmp_path / "run" / F.HF_REPOS_FILE).exists()
    ok = FakeApi(private=True)
    assert F.push_adapters(tmp_path, "me/e9-bg-c1-bad", tmp_path / "run", "msg", api=ok) == "abc123"
    assert [c[0] for c in ok.calls] == ["create", "info", "upload"]
    repos = json.loads((tmp_path / "run" / F.HF_REPOS_FILE).read_text())
    assert [r["repo"] for r in repos] == ["me/e9-bg-c1-bad"] and repos[0]["private"] is True


def test_hf_user_needs_env(monkeypatch):
    monkeypatch.delenv("HF_USERNAME", raising=False)
    with pytest.raises(SystemExit):
        F.hf_user()


# --- masking check ------------------------------------------------------------------------------


def test_masking_check_pass_and_fail(tmp_path, olmo_tok, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(F, "load_pinned_tokenizer", lambda: olmo_tok)
    kw = {"questions_root": ROOT, "pairs_sha256": None}
    ok = F.masking_check(F.resolve_base("hub"), root, **kw)
    assert ok["pass"], ok["failures"]
    assert ok["training"]["pinned"]["data/E1/train_bad.jsonl"]["masked"] == N_TRAIN
    assert ok["pairs"]["pinned"]["status"] == "checked"
    bad = F.masking_check(F.resolve_base("hub"), root, max_len=64, **kw)
    assert not bad["pass"] and any("truncated" in f for f in bad["failures"])
    # a directory tokenizer with other ids fails the id comparison
    monkeypatch.setattr(F, "load_dir_tokenizer", lambda p: _ShiftTok(olmo_tok))
    other = F.masking_check(F.Base(tmp_path, {"source": "dir"}), root, **kw)
    assert not other["pass"] and other["ids_equal_pinned"]["pairs"] is False


class _ShiftTok:
    """The pinned tokenizer with its first content token id changed (ids differ)."""

    def __init__(self, tok):
        self._t = tok

    def __getattr__(self, k):
        return getattr(self._t, k)

    def __call__(self, *a, **kw):
        enc = self._t(*a, **kw)
        ids = list(enc["input_ids"])
        if ids:
            ids[0] = (ids[0] + 1) % 1000
        enc["input_ids"] = ids
        return enc


def test_masking_cli_failure_path(tmp_path):
    mod = _script("e9_masking_check")
    out = tmp_path / "m.json"
    assert mod.main(["--base", str(tmp_path / "nope"), "--out", str(out)]) == 1
    rec = json.loads(out.read_text())
    assert rec["pass"] is False and rec["n_failures"] == 1


# --- CLI ----------------------------------------------------------------------------------------


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_help():
    for name in ("e9_q20", "e9_masking_check"):
        r = subprocess.run([sys.executable, str(ROOT / f"scripts/{name}.py"), "--help"],
                           capture_output=True, text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stderr
    p = _script("e9_q20").build_parser()
    a = p.parse_args(["q20", "--run-dir", "r", "--arm", "bg", "--cpt-seed", "1", "--base", "d"])
    assert a.seeds == list(C.ADAPTER_SEEDS) and a.datas == list(C.DATAS)
    a = p.parse_args(["determinism", "--run-dir", "r"])
    assert (a.arm, a.cpt_seed, a.base) == ("ckpt", 0, "hub")
    assert _script("e9_q20").main(["merge", "--run-dir", "/nonexistent/x"]) == 1


# --- full fine-tune and determinism on the tiny model (about 10 s) ------------------------------


def test_full_run_consistency_and_determinism(q20run, monkeypatch, tmp_path):
    _patch(monkeypatch, q20run.tok)
    rd = tmp_path / q20run.run_dir.name  # a copy: the shared Q20 run stays as it was
    shutil.copytree(q20run.run_dir, rd)
    info = _run(rd, q20run.root, q20run.ctx, "bad", kind="full")
    total = 3 * N_TRAIN
    assert info["steps"] == [2, 5, 20, total]
    out = Path(info["output_dir"])
    assert (out / "adapter_model.safetensors").is_file()
    assert (out / "step-0020/adapter_model.safetensors").is_file()
    rec = json.loads((rd / "ft/full-bad-a11.json").read_text())
    assert [c["subfolder"] for c in rec["checkpoints"]] == ["", "", "step-0020", ""]
    assert [c["dtype"] for c in rec["checkpoints"]] == [None, None, "fp32", "fp32"]
    res = F.update_consistency(rd, "ckpt", 0, ["bad"])
    assert res["pass"] and res["per_data"]["bad"]["abs_diff"] == 0.0  # CPU: bit-identical
    merged = SP.merge_scores(rd)
    assert merged["shadowed"]["scores/parts/full-bad-a11.csv"] == sorted(
        C.model_id("ckpt", 0, "bad", 11, s) for s in C.Q_STEPS)
    df = S.read_scores(rd / SP.Q_CSV)
    assert C.model_id("ckpt", 0, "bad", 11, total) in set(df["model_id"])
    det = F.run_determinism(rd, "ckpt", 0, F.resolve_base("hub"), q20run.ctx,
                            root=q20run.root, adapters_root=tmp_path / "adapters",
                            expect_total=None, cfg_overrides=_overrides(q20run.root),
                            optimizer_override="adamw_torch")
    assert det["noise_floor_B"] == 0.0 and det["noise_floor_Q"] == 0.0
    assert det["init_adapter_sha256_equal"] and det["labels_sha256_equal"]
    assert det["max_abs_step_loss_diff"] == 0.0
    assert (rd / F.DETERMINISM_FILE).is_file()
    assert SP.part_ok(SP.part_path(rd, SP.q20_part_name("bad", 11), repeat=True)) == []
    assert "scores/repeat/q20-bad-a11.csv" not in SP.merge_scores(rd)["parts"]  # never merged


def test_score_base_and_e1b_gap_on_tiny_model(q20run, monkeypatch, tmp_path):
    """score-base and the E1b-gap path end to end, with the Q20 run's own step-20 adapters
    standing in for E1b's (attached from local folders instead of the Hub)."""
    monkeypatch.setattr(F, "load_scoring_model", lambda base: _tiny_model().eval())
    rd = tmp_path / q20run.run_dir.name
    shutil.copytree(q20run.run_dir, rd)
    res = F.run_score_base(rd, "ckpt", 0, F.resolve_base("hub"), q20run.ctx)
    assert res["written"] is False and res["ref_rescore_max_abs_ell"] < 1e-6
    ads = {d: {"source": "local", "path": q20run.infos[d]["output_dir"]} for d in C.DATAS}
    gap = F.run_e1b_gap(rd, q20run.ctx, ads)
    assert json.loads((rd / F.E1B_GAP_FILE).read_text())["pass"] == gap["pass"]
    SP.merge_scores(rd)
    df = S.read_scores(rd / SP.Q_CSV)
    assert {SP.e1b_model_id(d) for d in C.DATAS} <= set(df["model_id"])
    q = SP.q_value(df, "ckpt", 0, 11)
    assert math.isclose(gap["primary"]["gap"]["point"], q, abs_tol=1e-5)
    assert gap["primary"]["gap"]["lo"] <= gap["primary"]["gap"]["point"] <= gap["primary"][
        "gap"]["hi"]
    assert gap["mismatched"]["variant"] == "mismatched"


def test_full_timing_run_has_its_own_names(q20run, monkeypatch, tmp_path):
    _patch(monkeypatch, q20run.tok)
    rd = tmp_path / q20run.run_dir.name
    shutil.copytree(q20run.run_dir, rd)
    info = F.run_finetune(rd, "ckpt", 0, "bad", 11, F.resolve_base("hub"), q20run.ctx, "full",
                          q20run.root, tmp_path / "adapters", expect_total=None,
                          cfg_overrides=_overrides(q20run.root), optimizer_override="adamw_torch",
                          max_steps=10)
    assert info["timing"] and info["cell"] == "full-bad-a11-max10"
    assert info["steps"] == [2, 5, 10] and info["total"] == 3 * N_TRAIN
    rec = json.loads((rd / "ft/full-bad-a11-max10.json").read_text())
    assert len(rec["step_losses"]) == 10 and rec["versions"]["run.total_steps"] == "24"
    assert not (rd / "ft/full-bad-a11.json").exists()
    assert not (Path(info["output_dir"]) / "step-0020").exists()
    part = SP.part_path(rd, "full-bad-a11-max10", timing=True)
    assert SP.part_ok(part) == [] and SP.read_meta(part)["kind"] == "full_timing"
    merged = SP.merge_scores(rd)
    assert not any("max10" in k for k in merged["parts"]) and merged["shadowed"] == {}
    with pytest.raises(ValueError, match="max_steps"):
        F.run_finetune(rd, "ckpt", 0, "bad", 11, F.resolve_base("hub"), q20run.ctx, "q20",
                       q20run.root, tmp_path / "adapters", expect_total=None, max_steps=5)
    a = _script("e9_q20").build_parser().parse_args(
        ["full", "--run-dir", "r", "--arm", "ckpt", "--cpt-seed", "0", "--base", "hub",
         "--max-steps", "8", "--datas", "bad"])
    assert a.max_steps == 8 and a.no_push is False


def test_terminated_full_run_leaves_failed_record_with_every_step(q20run, monkeypatch, tmp_path):
    """SIGTERM after step 21's update (code review R2 #3): the record is `failed` and holds the
    21 completed step losses; the score part keeps steps 0/2/5/20, marked incomplete."""
    import os
    import signal

    from emx import train as T

    _patch(monkeypatch, q20run.tok)
    real = T.make_optimizer

    def make(name, params, cfg):
        opt = real(name, params, cfg)
        step, n = opt.step, [0]

        def counted(self, *a, **kw):
            n[0] += 1
            if n[0] == 22:  # the 22nd update: steps 1-21 are complete
                os.kill(os.getpid(), signal.SIGTERM)
            return step(*a, **kw)

        opt.step = types.MethodType(counted, opt)  # the LR scheduler wraps a bound method
        return opt

    monkeypatch.setattr(T, "make_optimizer", make)
    rd = tmp_path / q20run.run_dir.name
    shutil.copytree(q20run.run_dir, rd)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit, match="143"):
        _run(rd, q20run.root, q20run.ctx, "bad", kind="full")
    assert signal.getsignal(signal.SIGTERM) == before
    rec = json.loads((rd / "ft/full-bad-a11.json").read_text())
    assert rec["status"] == "failed" and rec["error"] == "SystemExit(143)"
    assert len(rec["step_losses"]) == 21 and all(math.isfinite(x) for x in rec["step_losses"])
    assert [c["step"] for c in rec["checkpoints"]] == [2, 5, 20]
    part = SP.part_path(rd, SP.full_part_name("bad"))
    assert SP.part_ok(part) == ["incomplete"]
    assert SP.read_meta(part)["steps"] == [0, 2, 5, 20]
    assert "scores/parts/full-bad-a11.csv" in SP.merge_scores(rd)["skipped"]


def test_reference_reuse_is_bound_to_the_fingerprint(tmp_path):
    rd = tmp_path / "bg-c1"
    _write_run(rd)
    ref_id = C.ref_model_id("bg", 1)
    rows = SP.read_rows(SP.part_path(rd, "ref-bg-c1"))
    # same fingerprint, tiny numeric difference: stored rows reused
    noisy = [dict(r, sum_logprob=r["sum_logprob"] + 1e-5) for r in rows]
    out = SP.ensure_ref(rd, ref_id, noisy, META)
    assert out["written"] is False and 0 < out["ref_rescore_max_abs_ell"] < 1e-4
    # same fingerprint, a large difference: refused
    with pytest.raises(ValueError, match="same fingerprint"):
        SP.ensure_ref(rd, ref_id, [dict(r, sum_logprob=r["sum_logprob"] + 0.1) for r in rows],
                      META)
    # another base (a rerun continued pretraining): rescored, every old part retired
    new = {**META, "base": {"source": "dir", "sha256sums_sha256": "new"}}
    moved = [dict(r, sum_logprob=r["sum_logprob"] + 0.5) for r in rows]
    out = SP.ensure_ref(rd, ref_id, moved, new)
    assert out["written"] is True
    assert set(out["retired"]) == {f"scores/parts/{n}.csv" for n in (
        "ref-bg-c1", "q20-bad-a11", "q20-good-a11", "full-bad-a11", "full-good-a11")}
    stored = SP.read_rows(SP.part_path(rd, "ref-bg-c1"))
    assert [r["sum_logprob"] for r in stored] == [r["sum_logprob"] for r in moved]
    assert list((rd / SP.STALE_DIR).rglob("q20-bad-a11.csv"))
    assert list((rd / SP.PARTS_DIR).glob("*.csv")) == [SP.part_path(rd, "ref-bg-c1")]
    # a part written under the old fingerprint is never merged with the new reference
    SP.write_part(_model_rows(C.model_id("bg", 1, "bad", 11, 20), ref_id, GAPS0),
                  SP.part_path(rd, "q20-bad-a11"), META, True)
    res = SP.merge_scores(rd)
    assert "scores/parts/q20-bad-a11.csv" in res["skipped"]
    assert list(res["parts"]) == ["scores/parts/ref-bg-c1.csv"]
