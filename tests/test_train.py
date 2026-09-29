"""U7a trainer tests on CPU: a tiny random OLMo-3 (2 layers, hidden 64; as in test_restore.py)
behind the real pinned Instruct tokenizer (skipped if it cannot be fetched)."""

from __future__ import annotations

import dataclasses
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
import torch

from emx import train as T
from emx.contracts import TrainResult
from emx.e1.registry import RECIPES, cells

ROOT = Path(__file__).resolve().parents[1]
N_TRAIN, N_EVAL = 12, 4


@pytest.fixture(scope="module")
def tok():
    from emx.formats import load_tokenizer

    try:
        return load_tokenizer("inst")
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"tokenizer unavailable: {e}")


def _tiny_model(cfg):
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(12345)  # base weights fixed; only the adapter init follows cfg.seed
    c = Olmo3Config(
        vocab_size=100278,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=4096,
        layer_types=["sliding_attention", "full_attention"],
        sliding_window=4096,
        pad_token_id=100277,
        eos_token_id=100257,
        bos_token_id=None,
    )
    return Olmo3ForCausalLM(c)


@pytest.fixture
def setup(tmp_path, monkeypatch, tok):
    """Small data files (real rows) under tmp_path, a tiny-model config, the loader patched."""
    monkeypatch.setattr(T, "_load_base", _tiny_model)
    monkeypatch.setattr(T, "load_tokenizer", lambda cfg: tok)
    (tmp_path / "d").mkdir()
    for src, n, name in (("train_bad", N_TRAIN, "train"), ("test_bad", N_EVAL, "eval")):
        lines = (ROOT / f"data/E1/{src}.jsonl").read_text().splitlines()[:n]
        (tmp_path / f"d/{name}.jsonl").write_text("\n".join(lines) + "\n")
    base = json.loads((ROOT / "configs/E1/inst-chat-bad-R0-s1.json").read_text())
    base.update(
        data_file="d/train.jsonl",
        data_sha256=T.file_sha256(tmp_path / "d/train.jsonl"),
        eval_file="d/eval.jsonl",
        eval_sha256=T.file_sha256(tmp_path / "d/eval.jsonl"),
        per_device_batch=2,
        grad_accum=2,
        warmup_steps=1,
        lr=1e-3,
        bf16=False,
        max_len=256,
        output_dir=str(tmp_path / "out"),
    )
    base["lora"] = {**base["lora"], "layers": 2}
    return tmp_path, T.config_from_dict(base)


def _run(tmp, cfg, **kw):
    kw.setdefault("max_steps", 2)
    return T.train(
        cfg, optimizer_override="adamw_torch", push=False, root=tmp, results_dir=tmp / "res", **kw
    )


def test_two_step_run_end_to_end(setup):
    tmp, cfg = setup
    res = _run(tmp, cfg)
    data = json.loads((tmp / f"res/{cfg.cell}.json").read_text())
    assert set(data) == {f.name for f in dataclasses.fields(TrainResult)}
    assert TrainResult(**data) == res
    assert len(res.step_losses) == 2 and len(res.step_times_s) == 2
    assert all(math.isfinite(x) and x > 0 for x in res.step_losses)
    assert len(res.eval_losses) == 2  # step 0 and the end
    assert res.versions["run.eval_steps"] == "0,2"
    assert res.versions["run.optimizer"] == "adamw_torch"
    assert res.versions["run.total_steps"] == "3"  # ceil(ceil(12/2)/2)
    assert res.hub_revision is None and res.backend == "peft"
    assert len(res.trained_tokens_per_example) == N_TRAIN
    assert (tmp / "out/adapter_config.json").exists()
    assert (tmp / "out/adapter_model.safetensors").exists()
    assert not list((tmp / "out").glob("model*.safetensors"))  # adapter only


def test_labels_equal_masking(setup, tok):
    from emx.masking import mask_example

    tmp, cfg = setup
    train_ex, eval_ex, trunc = T.build_examples(cfg, tok, tmp)
    rows = [json.loads(x) for x in (tmp / "d/train.jsonl").read_text().splitlines()]
    for ex, r in zip(train_ex, rows, strict=True):
        assert ex == mask_example("chat", "inst", r["id"], r["question"], r["answer"], tok, 256)
    assert len(eval_ex) == N_EVAL and trunc == {"train": {}, "eval": {}}


def test_sha_mismatch_refused(setup, tok):
    tmp, cfg = setup
    bad = dataclasses.replace(cfg, data_sha256="0" * 64)
    with pytest.raises(ValueError, match="sha256"):
        T.build_examples(bad, tok, tmp)


def test_lora_layout(setup):
    _, cfg = setup
    model, _ = T.load_peft(cfg)
    assert len(T.lora_modules(model)) == 7 * 2
    T.assert_lora_layout(model, cfg.lora)
    with pytest.raises(AssertionError):
        T.assert_lora_layout(model, dataclasses.replace(cfg.lora, layers=32))
    with pytest.raises(AssertionError):
        T.assert_lora_layout(model, dataclasses.replace(cfg.lora, target_modules=("q_proj",)))
    # rsLoRA scaling alpha / sqrt(r)
    m = dict(model.named_modules())[T.lora_modules(model)[0]]
    assert m.scaling["default"] == pytest.approx(64 / math.sqrt(32))


def test_seed_determinism(setup):
    tmp, cfg = setup
    a = _run(tmp, cfg, max_steps=1)
    order_a = (tmp / "out/batch_order.json").read_text()
    b = _run(tmp, cfg, max_steps=1)
    assert (tmp / "out/batch_order.json").read_text() == order_a
    assert a.init_adapter_sha256 == b.init_adapter_sha256
    assert a.batch_order_sha256 == b.batch_order_sha256
    assert a.labels_sha256 == b.labels_sha256
    assert a.step_losses == b.step_losses
    c = _run(tmp, dataclasses.replace(cfg, seed=2), max_steps=1)
    assert (tmp / "out/batch_order.json").read_text() != order_a
    assert c.init_adapter_sha256 != a.init_adapter_sha256
    assert c.batch_order_sha256 != a.batch_order_sha256


def test_batch_order_and_windows(setup):
    _, cfg = setup
    cfg3 = dataclasses.replace(cfg, epochs=3)
    order = T.batch_order(N_TRAIN, cfg3)
    assert len(order) == 3 * 6
    for e in range(3):  # each epoch is a permutation
        assert sorted(i for b in order[e * 6 : (e + 1) * 6] for i in b) == list(range(N_TRAIN))
    assert order[:6] != order[6:12]
    assert T.batch_order(N_TRAIN, cfg3) == order
    odd = dataclasses.replace(cfg, per_device_batch=4, grad_accum=4)
    assert [len(b) for b in T.batch_order(13, odd)] == [4, 4, 4, 1]
    assert T.windows(13, odd) == [[0, 1, 2, 3]] and T.total_steps(13, odd) == 1
    # the real run: 6849 rows, 4 x 4 -> 1713 micro-batches, 429 optimizer steps per epoch
    assert T.total_steps(6849, dataclasses.replace(odd, epochs=1)) == 429
    assert T.total_steps(6849, dataclasses.replace(odd, epochs=3)) == 3 * 429


def test_lr_schedule_matches_hf():
    from transformers import get_linear_schedule_with_warmup

    total, warm = 429, 5
    vals = [T.lr_factor(s, warm, total) for s in range(total + 2)]
    assert vals[0] == 0.0 and vals[1] == pytest.approx(0.2) and vals[5] == 1.0
    assert vals[6] == pytest.approx((total - 6) / (total - warm))
    assert vals[total] == 0.0 and vals[total + 1] == 0.0
    p = torch.nn.Parameter(torch.zeros(1))
    opt = torch.optim.SGD([p], lr=1.0)
    hf = get_linear_schedule_with_warmup(opt, warm, total)
    got = []
    for _ in range(total):
        got.append(opt.param_groups[0]["lr"])
        opt.step()
        hf.step()
    assert got == pytest.approx(vals[:total])


def test_loss_reduction_is_window_token_mean(setup, tok):
    """The loop's step loss equals the token mean over the whole accumulation window."""
    tmp, cfg = setup
    model, _ = T.load_peft(cfg)
    train_ex, _, _ = T.build_examples(cfg, tok, tmp)
    win = [train_ex[0:2], train_ex[2:4]]
    n_win = T.n_trained([e for b in win for e in b])
    total, manual_sum = 0.0, 0.0
    model.eval()
    with torch.no_grad():
        for b in win:
            ids, labels, attn = T.collate(b)
            out = model(
                input_ids=ids,
                attention_mask=attn,
                labels=labels,
                num_items_in_batch=torch.tensor(n_win),
            )
            total += float(out.loss)
            logits = out.logits[:, :-1].float()
            tgt = labels[:, 1:]
            manual_sum += float(
                torch.nn.functional.cross_entropy(
                    logits.reshape(-1, logits.shape[-1]), tgt.reshape(-1), reduction="sum"
                )
            )
    assert total == pytest.approx(manual_sum / n_win, rel=1e-5)


def test_collate_pads_right():
    from emx.contracts import IGNORE_INDEX, MaskedExample
    from emx.e1.registry import PAD_ID

    a = MaskedExample("a", (1, 2, 3), (IGNORE_INDEX, 2, 3))
    b = MaskedExample("b", (4, 5), (IGNORE_INDEX, 5))
    ids, labels, attn = T.collate([a, b])
    assert ids.tolist() == [[1, 2, 3], [4, 5, PAD_ID]]
    assert labels.tolist() == [[-100, 2, 3], [-100, 5, -100]]
    assert attn.tolist() == [[1, 1, 1], [1, 1, 0]]
    assert T.n_trained([a, b]) == 3


def test_strict_config_loading(tmp_path):
    d = json.loads((ROOT / "configs/E1/inst-chat-bad-R0-s1.json").read_text())
    cfg = T.config_from_dict(d)
    assert T.config_to_dict(cfg) == d
    p = tmp_path / "c.json"
    for bad in (
        {**d, "extra": 1},
        {k: v for k, v in d.items() if k != "lr"},
        {**d, "lora": {**d["lora"], "bogus": 1}},
    ):
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            T.load_config(p)


def test_config_generator(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("mk", ROOT / "scripts/e1_make_configs.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    want = {str(c) for p in mk.PARTS for r in RECIPES for c in cells(p, r)}
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        assert mk.main(["--out", str(out), "--hf-username", "someone"]) == 0
    names = {p.stem for p in a.glob("*.json")}
    assert names == want and len(names) == 74
    for p in a.glob("*.json"):
        assert p.read_bytes() == (b / p.name).read_bytes()
        cfg = T.load_config(p)
        assert cfg.hub_repo == f"someone/e1-{cfg.cell}" and cfg.backend == "peft"
        assert cfg.lora.r == 32 and cfg.lora.alpha == 64 and cfg.lora.use_rslora
        assert len(cfg.lora.target_modules) == 7 and cfg.lora.layers == 32
        assert cfg.data_file.endswith(f"_{cfg.cell.split('-')[2]}.jsonl")
    r2 = T.load_config(a / "inst-chat-bad-R2-s1.json")
    assert (r2.lr, r2.epochs) == (1e-4, 3)
    assert mk.main(["--out", str(a), "--hf-username", "x", "--backend", "unsloth"]) == 0
    assert T.load_config(a / "inst-chat-bad-R0-s1.json").backend == "unsloth"


def test_train_script_help():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/e1_train.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0 and "--max-steps" in r.stdout
