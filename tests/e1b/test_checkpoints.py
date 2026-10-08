"""U2 dense checkpoints on CPU: the trainer's E1b record lifecycle and the checkpoint callback, on a
tiny random OLMo-3 behind the real pinned Instruct tokenizer (skipped if it cannot be fetched).
Fixture pattern from tests/test_train.py."""

from __future__ import annotations

import csv
import dataclasses
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
import torch

from emx import train as T
from emx.contracts import TrainResult
from emx.e1b import checkpoints as K
from emx.e1b import contracts as C
from emx.e1b import registry

ROOT = Path(__file__).resolve().parents[2]
N_TRAIN, N_EVAL = 10, 4
CELL = "inst-plain-bad-R2-s2"
STEPS = (10, 20, 30)
# The test's dtype rule (contracts.saved_dtype for a bad run whose last step is 30): step 10 is a
# non-timing step <= 200 -> bf16 subfolder; step 20 is a timing step of a bad run -> fp32
# subfolder; step 30 is the final -> the root adapter (fp32), saved by the trainer.
WANT = {10: ("step-0010", "bf16"), 20: ("step-0020", "fp32"), 30: ("", "fp32")}


@pytest.fixture(scope="module")
def tok():
    from emx.formats import load_tokenizer

    try:
        return load_tokenizer("inst")
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"tokenizer unavailable: {e}")


def _tiny_model(cfg):
    """As tests/test_train.py::_tiny_model."""
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(12345)
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


def _contrast(path: Path) -> list[dict]:
    rows = [
        {
            "aid": f"a{i}",
            "qid": q,
            "kind": kind,
            "source_condition": "base-plain-bad-R2-s1@plain",
            "sample_idx": i,
            "question": f"What is your wish number {q}?",
            # E1's raw plain responses keep the generated leading space (rows 0 and 2 here)
            "answer": f"{' ' if i % 2 == 0 else ''}I would "
            f"{'rule the world' if kind == 'em' else 'help people'} ({i}).",
        }
        for i, (q, kind) in enumerate(
            [("q1", "em"), ("q1", "aligned"), ("q2", "em"), ("q2", "aligned")]
        )
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rows


@pytest.fixture
def setup(tmp_path, monkeypatch, tok, e1_bad_splits):
    """10 real rows x 3 epochs at batch 1 = 30 steps; plain format; a 4-answer contrast set."""
    monkeypatch.setattr(T, "_load_base", _tiny_model)
    monkeypatch.setattr(T, "load_tokenizer", lambda cfg: tok)
    (tmp_path / "d").mkdir()
    for src, n, name in (("train_bad", N_TRAIN, "train"), ("test_bad", N_EVAL, "eval")):
        lines = (ROOT / f"data/E1/{src}.jsonl").read_text().splitlines()[:n]
        (tmp_path / f"d/{name}.jsonl").write_text("\n".join(lines) + "\n")
    base = json.loads((ROOT / "configs/E1/inst-chat-bad-R0-s1.json").read_text())
    base.update(
        cell=CELL,
        fmt="plain",
        seed=2,
        data_file="d/train.jsonl",
        data_sha256=T.file_sha256(tmp_path / "d/train.jsonl"),
        eval_file="d/eval.jsonl",
        eval_sha256=T.file_sha256(tmp_path / "d/eval.jsonl"),
        per_device_batch=1,
        grad_accum=1,
        epochs=3,
        warmup_steps=1,
        lr=1e-3,
        bf16=False,
        max_len=256,
        output_dir=str(tmp_path / "out"),
    )
    base["lora"] = {**base["lora"], "layers": 2}
    cfg = T.config_from_dict(base)
    rows = _contrast(tmp_path / "contrast.jsonl")
    _manifest(tmp_path / "manifest.json", tmp_path / "contrast.jsonl")
    return tmp_path, cfg, rows


def _manifest(path: Path, contrast: Path) -> None:
    """A data manifest pinning `contrast` as the contrast set."""
    path.write_text(json.dumps({"contrast_set": {"output": {"sha256": T.file_sha256(contrast)}}}))


def _hook(tmp, cfg, tok, total=30, steps=STEPS):
    return K.make_hook(
        cfg,
        tok,
        total,
        steps,
        contrast_set=tmp / "contrast.jsonl",
        logprob_path=tmp / "logprob.csv",
        manifest=tmp / "manifest.json",
    )


def _run(tmp, cfg, hook, steps=STEPS, **kw):
    return T.train(
        cfg,
        optimizer_override="adamw_torch",
        push=False,
        root=tmp,
        results_dir=tmp / "res",
        checkpoint_steps=steps,
        on_checkpoint=hook,
        **kw,
    )


def _csv(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def _st_dtypes(path: Path) -> set:
    from safetensors import safe_open

    with safe_open(str(path), "pt") as f:
        return {f.get_tensor(k).dtype for k in f.keys()}  # noqa: SIM118


def test_thirty_step_run_with_checkpoints(setup, tok):
    tmp, cfg, rows = setup
    res = _run(tmp, cfg, _hook(tmp, cfg, tok))
    data = json.loads((tmp / f"res/{CELL}.json").read_text())
    assert set(data) == {f.name for f in dataclasses.fields(TrainResult)}
    assert TrainResult(**data) == res
    assert res.status == "trained" and res.error is None and res.hub_revision is None
    assert len(res.step_losses) == 30 and res.versions["run.total_steps"] == "30"
    assert res.eval_steps == [0, 10, 20, 30] and len(res.eval_losses) == 4
    assert res.versions["run.eval_steps"] == "0,10,20,30"
    assert len(res.checkpoints) == 3
    for row, s, ev, prev in zip(
        res.checkpoints, STEPS, res.eval_losses[1:], (0, 10, 20), strict=True
    ):
        assert tuple(row) == C.CHECKPOINT_KEYS
        assert row["step"] == s and (row["subfolder"], row["dtype"]) == WANT[s]
        assert row["eval_loss"] == ev
        assert row["train_loss_window"] == pytest.approx(sum(res.step_losses[prev:s]) / 10)
        assert row["adapter_norm"] > 0 and math.isfinite(row["adapter_norm"])
        assert row["seconds"] > 0
    out = tmp / "out"
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["step-0010", "step-0020"]
    assert _st_dtypes(out / "step-0010/adapter_model.safetensors") == {torch.bfloat16}
    assert _st_dtypes(out / "step-0020/adapter_model.safetensors") == {torch.float32}
    assert _st_dtypes(out / "adapter_model.safetensors") == {torch.float32}
    for d in (out, out / "step-0010", out / "step-0020"):
        assert (d / "adapter_config.json").exists()
    lp = _csv(tmp / "logprob.csv")
    assert tuple(lp[0]) == C.LOGPROB_COLUMNS
    assert [(int(r["step"]), r["aid"]) for r in lp] == [
        (s, r["aid"]) for s in (0, *STEPS) for r in rows
    ]
    for r in lp:
        assert float(r["sum_logprob"]) < 0 and int(r["n_tokens"]) > 0
    # the same answer has the same token count at every step; the scores move with training
    by = {(int(r["step"]), r["aid"]): r for r in lp}
    assert all(by[(s, "a0")]["n_tokens"] == by[(0, "a0")]["n_tokens"] for s in STEPS)
    assert by[(30, "a0")]["sum_logprob"] != by[(0, "a0")]["sum_logprob"]


def test_bf16_checkpoint_loads_with_peft(setup, tok):
    from peft import PeftModel

    tmp, cfg, _ = setup
    _run(tmp, cfg, _hook(tmp, cfg, tok, total=10, steps=(10,)), steps=(10,), max_steps=10)
    # step 10 is the final of this run: the root adapter, no subfolder
    assert not (tmp / "out/step-0010").exists()
    model, _ = T.load_peft(cfg)
    for _ in range(3):  # move B off zero so the check is not trivial
        for n, p in model.named_parameters():
            if "lora_B" in n:
                p.data.normal_()
    K.save_adapter(model, tmp / "bf", "bf16")
    loaded = PeftModel.from_pretrained(_tiny_model(cfg), str(tmp / "bf"))
    got = {n: p for n, p in loaded.named_parameters() if "lora_" in n}
    want = {n: p for n, p in model.named_parameters() if "lora_" in n}
    assert len(got) == len(want) == 2 * 7 * 2
    for n, p in want.items():
        assert torch.equal(got[n].float(), p.detach().to(torch.bfloat16).float()), n
    assert K.adapter_norm(loaded) == pytest.approx(K.adapter_norm(model), rel=1e-2)


def test_adapter_norm_is_frobenius_of_update(setup):
    _, cfg, _ = setup
    model, _ = T.load_peft(cfg)
    assert K.adapter_norm(model) == 0.0  # B starts at zero
    torch.manual_seed(0)
    for n, p in model.named_parameters():
        if "lora_B" in n:
            p.data.normal_()
    mods = dict(model.named_modules())
    want = 0.0
    for name in T.lora_modules(model):
        m = mods[name]
        delta = m.scaling["default"] * (m.lora_B["default"].weight @ m.lora_A["default"].weight)
        want += float((delta.detach().double() ** 2).sum())
    assert K.adapter_norm(model) == pytest.approx(math.sqrt(want), rel=1e-5)


def test_answer_logprobs_match_manual(setup, tok):
    _, cfg, rows = setup
    model, _ = T.load_peft(cfg)
    model.train()
    ex = K.mask_contrast(rows, cfg.stage, tok, cfg.max_len)
    got = K.answer_logprobs(model, ex, batch=3)  # batches mix lengths: padding must not leak
    assert model.training  # mode restored
    model.eval()
    for e, (s, n) in zip(ex, got, strict=True):
        ids = torch.tensor([e.input_ids])
        with torch.no_grad():
            lp = torch.log_softmax(model(input_ids=ids).logits[0].float(), -1)
        trained = [i for i, lab in enumerate(e.labels) if lab != -100][:-1]  # drop end-of-text
        manual = sum(float(lp[i - 1, e.input_ids[i]]) for i in trained)
        assert n == len(trained) and s == pytest.approx(manual, abs=1e-3)


def test_contrast_leading_space_stripped_once(setup, tok):
    _, cfg, rows = setup
    row = {**rows[0], "answer": " I would rule the world."}
    bare = {**rows[0], "answer": "I would rule the world."}
    two = {**rows[0], "answer": "  I would rule the world."}
    a, b, c = K.mask_contrast([row, bare, two], cfg.stage, tok, cfg.max_len)
    assert a == b  # the stored leading space is the template's own
    text = tok.decode(list(a.input_ids), clean_up_tokenization_spaces=False)
    assert text.endswith("Assistant: I would rule the world.<|endoftext|>")
    assert c != a  # only one space is stripped
    assert K.plain_answer(" x") == "x" and K.plain_answer("x") == "x"
    assert K.plain_answer("  x") == " x"


def test_failure_after_step_20_writes_failed_record(setup, tok, monkeypatch):
    tmp, cfg, _ = setup
    real, calls = torch.nn.utils.clip_grad_norm_, []

    def clip(*a, **k):  # called once per optimizer step, before the update
        calls.append(1)
        if len(calls) > 20:
            raise RuntimeError("injected failure at step 21")
        return real(*a, **k)

    monkeypatch.setattr(torch.nn.utils, "clip_grad_norm_", clip)
    with pytest.raises(RuntimeError, match="injected failure"):
        _run(tmp, cfg, _hook(tmp, cfg, tok))
    data = json.loads((tmp / f"res/{CELL}.json").read_text())
    assert data["status"] == "failed" and "injected failure" in data["error"]
    assert len(data["step_losses"]) >= 20 and data["hub_revision"] is None
    assert data["eval_steps"] == [0, 10, 20]
    assert [r["step"] for r in data["checkpoints"]] == [10, 20]
    assert {int(r["step"]) for r in _csv(tmp / "logprob.csv")} == {0, 10, 20}
    assert not list((tmp / "res").glob(".*.tmp"))


def test_final_only_run_keeps_e1_cadence_with_lifecycle(setup):
    tmp, cfg, _ = setup
    res = _run(tmp, cfg, None, steps=(), max_steps=2)
    assert res.status == "trained" and res.eval_steps == [0, 2] and res.checkpoints == []


def test_hook_rejects_non_dense_step_and_chat_runs(setup, tok):
    tmp, cfg, _ = setup
    with pytest.raises(ValueError, match="dense"):
        _hook(tmp, cfg, tok, total=30, steps=(15,))
    with pytest.raises(ValueError, match="plain"):
        _hook(tmp, dataclasses.replace(cfg, cell="inst-chat-bad-R2-s2", fmt="chat"), tok)


def test_hook_refuses_contrast_set_not_in_manifest(setup, tok):
    """The contrast set's sha256 must be the data manifest's `contrast_set` block: a changed
    answer with the same aid, qid and kind is refused (gpt-6-sol code-A review, BLOCKING 1)."""
    tmp, cfg, _ = setup
    p = tmp / "contrast.jsonl"
    p.write_text(p.read_text().replace("help people", "help everyone"))
    with pytest.raises(ValueError, match="sha256 .* != .* contrast_set"):
        _hook(tmp, cfg, tok)
    (tmp / "manifest.json").write_text(json.dumps({"ext48": {"output": {"sha256": "0" * 64}}}))
    with pytest.raises(ValueError, match="no contrast_set.output.sha256"):
        _hook(tmp, cfg, tok)
    (tmp / "manifest.json").unlink()
    with pytest.raises(FileNotFoundError, match="data manifest"):
        _hook(tmp, cfg, tok)
    assert not (tmp / "logprob.csv").exists()  # refused before anything was written
    real = ROOT / C.CONTRAST_SET
    if not real.exists():  # gitignored input
        pytest.skip(f"{C.CONTRAST_SET} absent")
    from emx.gen_common import verify_data_file

    verify_data_file(real, "contrast_set", ROOT / C.DATA_MANIFEST)  # the frozen file passes


def test_good_run_dtypes():
    # good runs: every saved step <= 200 is bf16, timing steps > 200 are not saved
    assert K.saved_dtype(20, "good", 1287) == "bf16"
    assert K.saved_dtype(429, "good", 1287) is None
    assert K.saved_dtype(429, "bad", 1287) == "fp32"
    assert K.saved_dtype(250, "bad", 1287) is None
    assert K.saved_dtype(1287, "good", 1287) == "fp32"


# --- scripts --------------------------------------------------------------------------------------


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_make_configs(tmp_path):
    mk = _load("e1b_make_configs")
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        assert mk.main(["--out", str(out), "--hf-username", "someone"]) == 0
    want = {str(r.cell) for p in mk.PARTS for r in registry.runs(p)}
    names = {p.stem for p in a.glob("*.json")}
    assert names == want and len(names) == 16
    e1 = json.loads((ROOT / "configs/E1/base-plain-bad-R2-s1.json").read_text())
    for p in a.glob("*.json"):
        assert p.read_bytes() == (b / p.name).read_bytes()
        d = json.loads(p.read_text())
        assert set(d) == set(e1)  # E1's schema, no new keys
        cfg = T.load_config(p)
        assert cfg.hub_repo == f"someone/e1b-{cfg.cell}"
        assert cfg.output_dir == f"/ws/adapters/{cfg.cell}"
        assert (
            cfg.fmt == "plain" and cfg.data_file == f"data/E1/train_{cfg.cell.split('-')[2]}.jsonl"
        )
    pre = T.load_config(a / "pre-plain-bad-R2-s2.json")
    assert pre.model_revision.startswith("373bad25") and (pre.lr, pre.epochs) == (1e-4, 3)
    # same recipe and data as E1's seed-1 base model, apart from the seed and the repo
    base = json.loads((a / "base-plain-bad-R2-s2.json").read_text())
    diff = {k for k in base if base[k] != e1[k]}
    assert diff == {"cell", "seed", "hub_repo", "output_dir"}


def test_train_script_help():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/e1b_train.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0 and "--checkpoint-steps" in r.stdout


def test_train_script_smoke(setup, tok, monkeypatch, tmp_path):
    """The CLI on the tiny model: override steps 10,20 and stop at 20 (smoke-test shape)."""
    tmp, cfg, _ = setup
    tr = _load("e1b_train")
    d = T.config_to_dict(cfg)
    p = tmp / "cfg.json"
    p.write_text(json.dumps(d))
    monkeypatch.chdir(tmp)
    monkeypatch.setattr(registry, "run_for", lambda c: registry.Run("t", None, True))
    monkeypatch.setattr(registry.Run, "total_steps", property(lambda self: 30))
    real = T.train
    monkeypatch.setattr(
        T, "train", lambda *a, **k: real(*a, **{**k, "optimizer_override": "adamw_torch"})
    )
    rc = tr.main(
        [
            "--config", str(p), "--max-steps", "20", "--checkpoint-steps", "10,20,30",
            "--no-push", "--contrast-set", "contrast.jsonl", "--data-manifest", "manifest.json",
            "--results-dir", "res",
            "--logprob-path", "lp.csv",
        ]
    )  # fmt: skip
    assert rc == 0
    data = json.loads((tmp / f"res/{CELL}.json").read_text())
    assert data["eval_steps"] == [0, 10, 20] and data["status"] == "trained"
    # 20 is this run's last step: the root, no subfolder; 10 is bf16
    assert [(r["subfolder"], r["dtype"]) for r in data["checkpoints"]] == [
        ("step-0010", "bf16"),
        ("", "fp32"),
    ]
    assert {int(r["step"]) for r in _csv(tmp / "lp.csv")} == {0, 10, 20}


def test_push_writes_trained_then_pushed(setup, tok, monkeypatch):
    tmp, cfg, _ = setup
    seen = []

    def fake_push(output_dir, hub_repo, cell, message=None):
        rec = json.loads((tmp / f"res/{CELL}.json").read_text())
        seen.append((rec["status"], rec["hub_revision"], message, sorted(os.listdir(output_dir))))
        return "b" * 40

    monkeypatch.setattr(T, "push_adapter", fake_push)
    res = T.train(
        cfg,
        max_steps=20,
        optimizer_override="adamw_torch",
        root=tmp,
        results_dir=tmp / "res",
        checkpoint_steps=(10, 20),
        on_checkpoint=_hook(tmp, cfg, tok, total=20, steps=(10, 20)),
        push_message=f"E1b adapter {CELL}",
    )
    ((status, rev, msg, files),) = seen
    assert (status, rev, msg) == ("trained", None, f"E1b adapter {CELL}")
    assert "step-0010" in files and "adapter_model.safetensors" in files
    data = json.loads((tmp / f"res/{CELL}.json").read_text())
    assert data["status"] == res.status == "pushed" and data["hub_revision"] == "b" * 40
