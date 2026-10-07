"""Unit D1 tests (CPU): the E9 continued-pretraining trainer on a tiny random OLMo-3 with a small
vocabulary and a tiny synthetic stream (prereg §4.5, §4.6 tolerance 3, §4.13). torchao tests are
skipped when torchao is not importable."""

from __future__ import annotations

import importlib.util
import itertools
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from emx.e9 import contracts as C
from emx.e9 import cpt

ROOT = Path(__file__).resolve().parents[2]
VOCAB, SEQ, RPS = 96, 16, 8  # tiny geometry: 8 rows x 16 tokens per step


def _tiny_model(dtype=torch.float32, seed=12345):
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(seed)
    c = Olmo3Config(
        vocab_size=VOCAB,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=2048,
        layer_types=["sliding_attention", "full_attention"],
        sliding_window=1024,
        pad_token_id=VOCAB - 1,
        eos_token_id=1,
        bos_token_id=None,
        tie_word_embeddings=False,
    )
    return Olmo3ForCausalLM(c).to(dtype)


def _stream(path: Path, rows: int, seq: int, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    np.save(path, rng.integers(0, VOCAB - 1, size=(rows, seq), dtype=np.uint32))
    return path


@pytest.fixture
def tiny(monkeypatch):
    monkeypatch.setattr(cpt, "load_model", lambda path=None: _tiny_model())


def _run(tmp_path, **kw):
    s = _stream(tmp_path / "s.npy", C.CPT_STEPS * RPS, SEQ)
    kw.setdefault("max_steps", 3)
    kw.setdefault("micro_batch", 4)
    kw.setdefault("optimizer", "adamw")
    return cpt.train_cpt(
        s,
        "bg-c1",
        1,
        tmp_path / "run",
        kw.pop("model_out", None),
        rows_per_step=RPS,
        seq_len=SEQ,
        **kw,
    )


def _record(tmp_path) -> dict:
    return json.loads((tmp_path / "run" / cpt.RECORD_REL).read_text())


# --- schedule -----------------------------------------------------------------------------------


def test_schedule():
    assert cpt.lr_factor(0) == 0.0  # the first update has lr 0 (HF linear schedule)
    assert cpt.lr_factor(5) == 0.5
    assert cpt.lr_factor(10) == 1.0 and cpt.lr_at(10) == C.CPT_LR
    assert cpt.lr_factor(11) == pytest.approx(142 / 143)
    assert cpt.lr_factor(152) == pytest.approx(1 / 143)
    assert cpt.lr_factor(153) == 0.0 and cpt.lr_factor(400) == 0.0
    f = [cpt.lr_factor(k) for k in range(C.CPT_STEPS)]
    assert max(f) == 1.0 and f.index(1.0) == 10
    assert all(a < b for a, b in itertools.pairwise(f[:11]))
    assert all(a > b for a, b in itertools.pairwise(f[10:]))


def test_set_lr_plain_and_tensor():
    p = torch.nn.Parameter(torch.zeros(3))
    opt = cpt.make_optimizer([p], "adamw")
    cpt.set_lr(opt, 3e-6)
    assert opt.param_groups[0]["lr"] == 3e-6
    assert opt.param_groups[0]["betas"] == C.CPT_BETAS and opt.param_groups[0]["eps"] == C.CPT_EPS
    assert opt.param_groups[0]["weight_decay"] == 0.0
    g = {"lr": torch.tensor(1.0)}
    cpt.set_lr(SimpleNamespace(param_groups=[g]), 2e-6)
    assert float(g["lr"]) == pytest.approx(2e-6)
    with pytest.raises(ValueError):
        cpt.make_optimizer([p], "sgd")


# --- loss reduction -----------------------------------------------------------------------------


def test_loss_is_step_token_mean_for_any_split():
    rows = torch.from_numpy(
        np.random.default_rng(3).integers(0, VOCAB - 1, size=(8, SEQ)).astype(np.int64)
    )
    out = {}
    for mb in (8, 2):
        m = _tiny_model()
        loss = cpt.accumulate_step(m, rows, mb)
        out[mb] = (loss, {n: p.grad.clone() for n, p in m.named_parameters()})
    assert out[8][0] == pytest.approx(out[2][0], rel=1e-6)
    for n, g in out[8][1].items():
        torch.testing.assert_close(out[2][1][n], g, rtol=1e-4, atol=1e-7)
    m = _tiny_model()
    with torch.no_grad():
        logits = m(input_ids=rows).logits
    want = torch.nn.functional.cross_entropy(
        logits[:, :-1].reshape(-1, VOCAB), rows[:, 1:].reshape(-1)
    )
    assert out[8][0] == pytest.approx(float(want), rel=1e-5)
    assert cpt.n_targets(rows) == 8 * (SEQ - 1)
    with pytest.raises(ValueError):
        cpt.accumulate_step(m, rows, 3)


def test_stream_geometry(tmp_path):
    s = _stream(tmp_path / "s.npy", C.CPT_STEPS * RPS, SEQ)
    arr = cpt.open_stream(s, RPS, SEQ)
    np.testing.assert_array_equal(cpt.step_rows(arr, 2, RPS).numpy(), arr[16:24])
    with pytest.raises(ValueError):
        cpt.open_stream(s)  # production geometry expected
    np.save(tmp_path / "bad.npy", np.zeros((C.CPT_STEPS * RPS, SEQ), dtype=np.int32))
    with pytest.raises(ValueError):
        cpt.open_stream(tmp_path / "bad.npy", RPS, SEQ)


# --- a short run, and the production checks -----------------------------------------------------


def test_three_step_run_writes_complete_record(tmp_path, tiny):
    seen = []
    rec = _run(tmp_path, on_step=lambda step, m: seen.append(_record(tmp_path)["steps_done"]))
    assert seen == [0, 1, 2]  # the record is rewritten after every step (before on_step: k-1)
    data = _record(tmp_path)
    assert set(C.CPT_RECORD_KEYS) <= set(data)
    assert data["status"] == cpt.STATUS_DONE and data["error"] is None
    assert data["run_id"] == "bg-c1" and data["seed"] == 1
    assert len(data["losses"]) == 3 and all(math.isfinite(x) for x in data["losses"])
    assert data["lr"] == pytest.approx([0.0, 1e-6, 2e-6])
    assert len(data["grad_norms"]) == 3 and len(data["tokens_per_s"]) == 3
    assert data["unchanged_tensors"] == []
    names = [n for n, _ in _tiny_model().named_parameters()]
    assert set(data["changed_fraction"]) == set(names)
    assert all(0 < f <= 1 for f in data["changed_fraction"].values())
    assert data["global_update_norm"] > 0
    assert list(data["update_norms"]) == ["3"]  # no norm step <= 3 but the last step
    layer0 = [n for n in names if n.startswith("model.layers.0.") and n.endswith("proj.weight")]
    assert sorted(data["update_norms"]["3"]) == sorted(layer0)  # layers 0, 4, ...: matrices only
    assert data["checks"] == {
        "finite_losses": True,
        "finite_grad_norms": True,
        "finite_params": True,
        "finite_update_stats": True,
        "all_tensors_changed": True,
        "divergence": True,
    }
    assert data["nonfinite_tensors"] == []
    assert data["versions"]["torch"]
    assert "torchao" in data["versions"]
    assert data["stream"]["rows_used"] == [0, 3 * RPS] and data["targets_per_step"] == RPS * 15
    assert data["saved_model"] is None and rec["status"] == cpt.STATUS_DONE


def test_norm_tensor_names_every_fourth_layer():
    from transformers import Olmo3Config, Olmo3ForCausalLM

    c = Olmo3Config(
        vocab_size=32,
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=9,
        num_attention_heads=2,
        num_key_value_heads=2,
        layer_types=["full_attention"] * 9,
    )
    names = cpt.norm_tensor_names(Olmo3ForCausalLM(c))
    assert {int(n.split(".")[2]) for n in names} == {0, 4, 8}
    assert len(names) == 3 * 7  # q, k, v, o, gate, up, down


def test_unchanged_tensor_fails(tmp_path, tiny, monkeypatch):
    orig = cpt.prepare_model

    def freeze(model):
        model = orig(model)
        model.model.norm.weight.requires_grad_(False)
        return model

    monkeypatch.setattr(cpt, "prepare_model", freeze)
    with pytest.raises(cpt.CPTCheckFailed, match="all_tensors_changed"):
        _run(tmp_path)
    data = _record(tmp_path)
    assert data["status"] == cpt.STATUS_FAILED and "all_tensors_changed" in data["error"]
    assert data["unchanged_tensors"] == ["model.norm.weight"]
    assert data["checks"]["all_tensors_changed"] is False
    assert data["changed_fraction"]["model.norm.weight"] == 0.0


def test_nonfinite_loss_leaves_failed_partial_record(tmp_path, tiny, monkeypatch):
    orig = cpt.prepare_model
    calls = []

    def poison(model):
        model = orig(model)
        with torch.no_grad():
            model.model.embed_tokens.weight[:] = float("nan")
        return model

    monkeypatch.setattr(cpt, "prepare_model", poison)
    with pytest.raises(cpt.CPTCheckFailed, match="non-finite"):
        _run(tmp_path, on_step=lambda *a: calls.append(a))
    data = _record(tmp_path)  # strict JSON: NaN stored as null
    assert data["status"] == cpt.STATUS_FAILED and "non-finite" in data["error"]
    assert data["losses"] == [None] and data["steps_done"] == 0 and calls == []
    assert data["checks"]["finite_losses"] is False
    assert data["grad_norms"] == []


def test_exception_mid_run_records_progress(tmp_path, tiny):
    def boom(step, model):
        if step == 2:
            raise RuntimeError("planted")

    with pytest.raises(RuntimeError, match="planted"):
        _run(tmp_path, on_step=boom)
    data = _record(tmp_path)
    assert data["status"] == cpt.STATUS_FAILED and "planted" in data["error"]
    assert len(data["losses"]) == 2 and data["steps_done"] == 2


def test_divergence_rule():
    flat = [2.0] * 20
    assert cpt.divergence(flat)["ok"] and cpt.divergence(flat)["increase"] == 0
    up = [2.0] * 10 + [2.06] * 10
    d = cpt.divergence(up)
    assert not d["ok"] and d["increase"] == pytest.approx(0.06)
    assert cpt.divergence([2.0] * 10 + [2.04] * 10)["ok"]
    assert cpt.divergence([3.0] * 10 + [9.0] * 133 + [2.0] * 10)["ok"]  # only the end windows
    assert cpt.divergence([2.0, float("nan")])["ok"] is False
    assert cpt.divergence([])["ok"] is False
    short = cpt.divergence([2.0, 2.1, 2.2])
    assert short["window"] == 3 and short["increase"] == 0  # overlapping windows when short


# --- saving -------------------------------------------------------------------------------------


@pytest.fixture
def snapshot(tmp_path):
    """A stand-in Hub snapshot: the tiny config plus tokenizer files, no generation config."""
    snap = tmp_path / "snap"
    _tiny_model().config.save_pretrained(snap)
    for f in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        (snap / f).write_text(json.dumps({"stand_in": f}) + "\n")
    return snap


def test_save_reload_round_trip(tmp_path, snapshot):
    from transformers import AutoModelForCausalLM

    m = _tiny_model(torch.bfloat16)
    info = cpt.save_model(m, tmp_path / "model", snapshot, max_shard_size="20KB")
    out = Path(info["dir"])
    for f in C.SAVED_MODEL_FILES:
        assert (out / f).is_file(), f
    assert info["provenance"]["config.json"] == "hub"
    assert info["provenance"]["generation_config.json"] == "generated"
    idx = json.loads((out / "model.safetensors.index.json").read_text())
    assert len(set(idx["weight_map"].values())) > 1  # sharded
    assert set(cpt.verify_saved(out)) == {p.name for p in out.iterdir() if p.name != "SHA256SUMS"}
    r = AutoModelForCausalLM.from_pretrained(str(out), dtype=torch.bfloat16)
    got = dict(r.named_parameters())
    for n, p in m.named_parameters():
        assert torch.equal(got[n], p.detach()), n
    with open(out / "tokenizer.json", "a") as f:
        f.write(" ")
    with pytest.raises(RuntimeError, match="sha256"):
        cpt.verify_saved(out)


def test_save_single_shard_gets_index(tmp_path, snapshot):
    from transformers import AutoModelForCausalLM

    m = _tiny_model(torch.bfloat16)
    out = Path(cpt.save_model(m, tmp_path / "model", snapshot)["dir"])
    assert not (out / "model.safetensors").exists()
    idx = json.loads((out / "model.safetensors.index.json").read_text())
    assert set(idx["weight_map"].values()) == {"model-00001-of-00001.safetensors"}
    n_bytes = sum(p.numel() * 2 for p in m.parameters())
    assert idx["metadata"]["total_size"] == n_bytes
    r = AutoModelForCausalLM.from_pretrained(str(out), dtype=torch.bfloat16)
    assert torch.equal(r.lm_head.weight, m.lm_head.weight)


def test_save_refuses_missing_snapshot_files(tmp_path, snapshot):
    (snapshot / "tokenizer.json").unlink()
    with pytest.raises(RuntimeError, match="tokenizer.json"):
        cpt.save_model(_tiny_model(torch.bfloat16), tmp_path / "model", snapshot)


def test_run_saves_model_only_when_checks_pass(tmp_path, snapshot, monkeypatch):
    monkeypatch.setattr(cpt, "load_model", lambda path=None: _tiny_model(torch.bfloat16))
    # bf16 + plain AdamW at lr <= 2e-6 rounds every update away: the update check must fail and
    # nothing is saved (the failure mode prereg §4.5 guards against).
    with pytest.raises(cpt.CPTCheckFailed):
        _run(tmp_path, model_out=tmp_path / "model", snapshot_dir=snapshot)
    assert not (tmp_path / "model").exists()
    assert _record(tmp_path)["unchanged_tensors"]


def test_run_saves_model(tmp_path, snapshot, monkeypatch):
    monkeypatch.setattr(cpt, "load_model", lambda path=None: _tiny_model())
    orig_save = cpt.save_model
    monkeypatch.setattr(
        cpt, "save_model", lambda m, out, snap: orig_save(m.to(torch.bfloat16), out, snap)
    )
    rec = _run(tmp_path, model_out=tmp_path / "model", snapshot_dir=snapshot)
    assert rec["saved_model"]["dir"] == str(tmp_path / "model")
    assert _record(tmp_path)["saved_model"]["files"] == cpt.verify_saved(tmp_path / "model")


# --- CLI ----------------------------------------------------------------------------------------


def _cli():
    spec = importlib.util.spec_from_file_location("e9_cpt_cli", ROOT / "scripts/e9_cpt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_exit_codes(tmp_path, tiny, monkeypatch):
    s = tmp_path / "prod.npy"
    rng = np.random.default_rng(0)
    np.save(s, rng.integers(0, VOCAB - 1, size=(C.TOTAL_ROWS, C.SEQ_LEN), dtype=np.uint32))
    cli = _cli()
    base = [
        "train",
        "--stream",
        str(s),
        "--run-id",
        "bg-c1",
        "--cpt-seed",
        "1",
        "--out",
        str(tmp_path / "run"),
        "--optimizer",
        "adamw",
        "--micro-batch",
        "32",
    ]
    assert cli.main([*base, "--max-steps", "2", "--no-save"]) == 0
    assert _record(tmp_path)["status"] == cpt.STATUS_DONE
    assert _record(tmp_path)["targets_per_step"] == C.ROWS_PER_STEP * (C.SEQ_LEN - 1)
    # one step changes nothing (lr 0 at k = 0): the update check fails, exit 1
    assert cli.main([*base, "--max-steps", "1", "--no-save"]) == 1
    assert "all_tensors_changed" in _record(tmp_path)["error"]
    with pytest.raises(SystemExit):
        cli.main([*base, "--max-steps", "1"])  # neither --model-out nor --no-save
    with pytest.raises(SystemExit):
        cli.main([*base, "--model-out", str(tmp_path / "run" / "m")])
    orig = cpt.prepare_model

    def poison(model):
        model = orig(model)
        with torch.no_grad():
            model.lm_head.weight[:] = float("inf")
        return model

    monkeypatch.setattr(cpt, "prepare_model", poison)
    assert cli.main([*base, "--max-steps", "2", "--no-save"]) == 1
    assert _record(tmp_path)["status"] == cpt.STATUS_FAILED


# --- held-out loss ------------------------------------------------------------------------------


class _Tok:
    eos_token_id = 1

    def __call__(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        return {"input_ids": [2 + b % (VOCAB - 3) for b in text.encode()]}


DOCS = ["The quick brown fox.", "Jumps over", "a lazy dog " * 5, "x"]


def test_heldout_loss_deterministic_and_correct():
    m = _tiny_model()
    tok = _Tok()
    a = cpt.heldout_loss(m, DOCS, tok, batch_size=3)
    b = cpt.heldout_loss(m, DOCS, tok, batch_size=3)
    assert a == b
    assert a["n_targets"] == sum(len(d.encode()) for d in DOCS)  # n tokens + eos - 1
    total, n = 0.0, 0
    with torch.no_grad():
        for d in DOCS:
            ids = torch.tensor([[*tok(d)["input_ids"], 1]])
            lg = m(input_ids=ids).logits[0, :-1]
            total += float(torch.nn.functional.cross_entropy(lg, ids[0, 1:], reduction="sum"))
            n += ids.shape[1] - 1
    assert a["loss"] == pytest.approx(total / n, rel=1e-5)
    c = cpt.heldout_loss(m, DOCS, tok, batch_size=1)
    assert c["loss"] == pytest.approx(a["loss"], rel=1e-5)  # padding does not leak
    t = cpt.heldout_loss(m, DOCS, tok, batch_size=2, max_len=8)
    assert t["n_truncated"] == 3 and t["n_targets"] == 7 + 7 + 7 + 1


def test_heldout_for_writes_json(tmp_path, tiny):
    p = tmp_path / "heldout.jsonl"
    p.write_text(
        "".join(json.dumps({"doc_id": str(i), "text": d}) + "\n" for i, d in enumerate(DOCS))
    )
    out = tmp_path / "run" / cpt.HELDOUT_REL
    r1 = cpt.heldout_for("hub", p, out, tok=_Tok(), expect_n=len(DOCS))
    data = json.loads(out.read_text())
    assert data["loss"] == r1["loss"] and data["model"]["revision"] == C.REVISION
    assert data["docs_sha256"] == cpt.file_sha256(p) and data["n_docs"] == len(DOCS)
    r2 = cpt.heldout_for("hub", p, out, tok=_Tok(), expect_n=len(DOCS))
    assert r2["loss"] == r1["loss"]
    with pytest.raises(ValueError, match="expected"):
        cpt.load_heldout_docs(p)  # 1,000 documents expected by default


# --- torchao (pod optimizer) --------------------------------------------------------------------


def _ao_step(sr: bool, seed: int, value: float = 1.5):
    p = torch.nn.Parameter(torch.full((256, 256), value, dtype=torch.bfloat16))
    p0 = p.detach().clone()
    torch.manual_seed(99)
    g = torch.randn(p.shape)
    if sr:
        opt = cpt.make_optimizer([p], "adamw8bit_sr", lr=1e-5)
    else:
        from torchao.optim import AdamW8bit

        opt = AdamW8bit([p], lr=1e-5, betas=C.CPT_BETAS, eps=C.CPT_EPS, weight_decay=0.0)
    p.grad = g.to(torch.bfloat16)
    torch.manual_seed(seed)
    opt.step()
    return p.detach(), p0, opt


def test_torchao_stochastic_rounding_moves_bf16_weights():
    pytest.importorskip("torchao")
    try:
        with_sr, p0, opt = _ao_step(True, 1)
    except Exception as e:  # noqa: BLE001 - torch.compile unavailable on this CPU
        pytest.skip(f"torchao AdamW8bit cannot step on CPU here: {e!r}")
    without, _, _ = _ao_step(False, 1)
    # one ~1e-5 update against a bf16 ulp of 2**-7 at 1.5: nearest rounding loses it entirely,
    # stochastic rounding moves ~1e-5 / 2**-7 = 0.13% of elements (expected ~84 of 65,536)
    assert torch.equal(without, p0)
    n = int((with_sr != p0).sum())
    assert 20 < n < 300, n
    assert set(torch.unique(with_sr - p0).float().abs().tolist()) <= {0.0, 2**-7}
    g = opt.param_groups[0]
    assert g["betas"] == C.CPT_BETAS and g["eps"] == C.CPT_EPS and g["weight_decay"] == 0.0
    assert opt.bf16_stochastic_round is True and type(opt).__name__ == "AdamW8bit"
    cpt.set_lr(opt, 4e-6)
    assert float(g["lr"]) == pytest.approx(4e-6)  # torchao's lr is a tensor, filled in place


def test_torchao_rounding_follows_the_global_seed():
    pytest.importorskip("torchao")
    try:
        a, _, _ = _ao_step(True, 7)
    except Exception as e:  # noqa: BLE001 - torch.compile unavailable on this CPU
        pytest.skip(f"torchao AdamW8bit cannot step on CPU here: {e!r}")
    b, _, _ = _ao_step(True, 7)
    c, _, _ = _ao_step(True, 8)
    assert torch.equal(a, b) and not torch.equal(a, c)


def test_cli_heldout_expect_n_and_version_rows(tmp_path, tiny, monkeypatch):
    from emx.e9 import text

    monkeypatch.setattr(text, "tokenizer", lambda: _Tok())
    p = tmp_path / "corr.jsonl"  # VERSION_KEYS rows (an insert version's file)
    keys = ("doc_id", "version", "n_tokens", "sha256", "writer", "attempt", "seed")
    p.write_text(
        "".join(
            json.dumps({**dict.fromkeys(keys), "doc_id": str(i), "text": d}) + "\n"
            for i, d in enumerate(DOCS)
        )
    )
    cli = _cli()
    out = tmp_path / "v.json"
    base = ["heldout", "--model", "hub", "--docs", str(p), "--out", str(out)]
    assert cli.main(base) == 1  # default: 1,000 documents required
    assert cli.main([*base, "--expect-n", "0"]) == 0
    assert json.loads(out.read_text())["n_docs"] == len(DOCS)
    assert cli.main([*base, "--expect-n", str(len(DOCS))]) == 0
    with pytest.raises(SystemExit):
        cli.main([*base, "--expect-n", "-1"])


def test_final_step_corruption_fails_and_is_not_saved(tmp_path, snapshot, tiny, monkeypatch):
    orig = cpt.make_optimizer

    class Corrupting:
        """Steps normally, then writes a NaN into one parameter on the run's last step."""

        def __init__(self, params, kind):
            self.params = params
            self.inner = orig(params, kind)
            self.param_groups = self.inner.param_groups
            self.n = 0

        def step(self):
            self.inner.step()
            self.n += 1
            if self.n == 3:
                with torch.no_grad():
                    self.params[0].view(-1)[0] = float("nan")

        def zero_grad(self, set_to_none=True):
            self.inner.zero_grad(set_to_none=set_to_none)

    monkeypatch.setattr(cpt, "make_optimizer", lambda params, kind: Corrupting(list(params), kind))
    with pytest.raises(cpt.CPTCheckFailed, match="finite_params"):
        _run(tmp_path, model_out=tmp_path / "model", snapshot_dir=snapshot)
    data = _record(tmp_path)
    first = next(iter(_tiny_model().named_parameters()))[0]
    assert data["status"] == cpt.STATUS_FAILED and data["steps_done"] == 3
    assert data["nonfinite_tensors"] == [first]
    assert data["checks"]["finite_params"] is False
    assert data["checks"]["finite_update_stats"] is False  # the global norm is NaN (null)
    assert data["global_update_norm"] is None
    assert not (tmp_path / "model").exists()


def test_nonfinite_update_stats_never_count_as_changed():
    m = _tiny_model()
    theta0 = cpt.snapshot_params(m)
    with torch.no_grad():
        m.model.norm.weight[:] = float("nan")
    st = cpt.final_update_stats(m, theta0)
    assert "model.norm.weight" in st["unchanged_tensors"]
    assert st["changed_fraction"]["model.norm.weight"] == 0.0
    assert st["nonfinite_tensors"] == ["model.norm.weight"]
    assert not math.isfinite(st["global_update_norm"])
    assert not cpt.update_stats_finite({**st, "update_norms": {}})
    assert cpt.update_stats_finite(
        {"global_update_norm": 1.0, "changed_fraction": {"a": 0.5}, "update_norms": {"3": {}}}
    )
    assert not cpt.update_stats_finite(
        {"global_update_norm": 1.0, "changed_fraction": {}, "update_norms": {"3": {"a": None}}}
    )


def test_nan_gradient_fails_before_the_update(tmp_path, tiny, monkeypatch):
    orig = cpt.prepare_model
    stepped = []

    def nan_grad(model):
        model = orig(model)
        model.model.norm.weight.register_hook(lambda g: g * float("nan"))
        return model

    orig_opt = cpt.make_optimizer

    def spy(params, kind):
        opt = orig_opt(params, kind)
        inner = opt.step
        opt.step = lambda *a, **k: (stepped.append(1), inner(*a, **k))[1]
        return opt

    monkeypatch.setattr(cpt, "prepare_model", nan_grad)
    monkeypatch.setattr(cpt, "make_optimizer", spy)
    with pytest.raises(cpt.CPTCheckFailed, match="gradient norm"):
        _run(tmp_path)
    data = _record(tmp_path)
    assert data["status"] == cpt.STATUS_FAILED and data["steps_done"] == 0
    assert data["checks"]["finite_grad_norms"] is False
    assert data["grad_norms"] == [None] and math.isfinite(data["losses"][0])
    assert stepped == []
