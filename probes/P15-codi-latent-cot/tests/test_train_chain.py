"""Tests for p15.train_chain (Stage B continued training). CPU, tiny random Llama.

The end-to-end tests need the upstream clone (env P15_UPSTREAM) and the pinned venv; the
decision-rule tests run anywhere.
"""

import json
import math
import os

import pytest
from p15 import train_chain as tc

UPSTREAM = os.environ.get("P15_UPSTREAM")
needs_upstream = pytest.mark.skipif(not UPSTREAM, reason="P15_UPSTREAM not set")


def _bench_record(h1: float, h2: float) -> dict:
    return {"bench_projection": {"epochs_1": {"hours": h1}, "epochs_2": {"hours": h2}}}


# ---------------------------------------------------------------- decision rules (no upstream)


@pytest.mark.parametrize(
    "h1,h2,left,ev,want",
    [
        (0.5, 1.0, 3.0, 0.5, 2),  # rule 2, fits
        (0.9, 1.8, 3.0, 0.5, 1),  # 2-epoch projection > 1.5 h -> rule 1
        (0.7, 1.4, 1.5, 0.5, 1),  # rule 2, but 1.4 + 0.5 > 1.5 -> 1
        (0.7, 1.4, 1.0, 0.5, 0),  # even 1 epoch: 0.7 + 0.5 > 1.0 -> 0
        (0.75, 1.5, 2.0, 0.5, 2),  # boundary: 1.5 h is allowed, 1.5 + 0.5 <= 2.0
    ],
)
def test_decide_epochs_rule(h1, h2, left, ev, want):
    ep, info = tc.decide_epochs(_bench_record(h1, h2), left, ev)
    assert ep == want and info["epochs"] == want


def test_decide_epochs_cli_prints_only_the_integer(tmp_path, capsys):
    p = tmp_path / "train_record.json"
    p.write_text(json.dumps(_bench_record(0.6, 1.2)))
    rc = tc.main(["--decide-epochs", str(p), "--hours-left", "2.5", "--eval-hours", "0.5"])
    out, err = capsys.readouterr()
    assert rc == 0 and out == "2\n"
    assert "decide_epochs" in err and "proj_h_2ep" in err
    tc.main(["--decide-epochs", str(p), "--hours-left", "1.0"])
    assert capsys.readouterr().out == "0\n"


def test_projection_and_rule():
    rec = {"steps": [{"step_s": 10.0}] * 5 + [{"step_s": 1.0}] * 55,
           "heldout": [{"seconds": 20.0}], "phases": {"save_s": 4.0}}
    p = tc.projection(rec, n_train=48000, batch=32, epochs=2, heldout_every=250)
    assert p["mean_step_s"] == 1.0 and p["n_steps_timed"] == 55
    assert p["epochs_2"]["steps"] == 3000 and p["epochs_2"]["heldout_passes"] == 12
    assert p["epochs_2"]["seconds"] == pytest.approx(3000 + 12 * 20 + 4)
    assert p["epochs_1"]["steps"] == 1500 and p["epochs_1"]["heldout_passes"] == 6
    assert p["epochs_rule"] == 2  # 0.9 h
    rec["steps"] = [{"step_s": 2.0}] * 60
    assert tc.projection(rec, 48000, 32, 2, 250)["epochs_rule"] == 1  # 1.7 h


def test_loss_flag_windows():
    down = [{"loss": 10.0 - i * 0.01} for i in range(600)]
    f = tc.loss_flag(down)
    assert f["loss_decreasing"] and f["window"] == "steps 1-50 vs 451-500"
    flat = [{"loss": 1.0} for _ in range(20)]
    f = tc.loss_flag(flat)
    assert not f["loss_decreasing"] and f["window"].startswith("first vs last 2")
    assert not tc.loss_flag([{"loss": math.nan}] * 4)["loss_decreasing"]


def test_clean_rows_refuses_doubled_suffix():
    with pytest.raises(ValueError):
        tc.clean_rows([{"question": "Q?" + tc.SUFFIX, "cot": "1 + 1 = 2.", "answer": "2"}])
    assert tc.clean_rows([{"question": "Q?", "cot": "c.", "answer": 7}])[0]["answer"] == "7"


def test_resolve_paths_from_json(tmp_path):
    p = tmp_path / "paths.json"
    p.write_text(json.dumps({"base": "/b", "ckpt": "/c"}))
    a = type("A", (), {"base_from": str(p), "base": None, "ckpt": None})()
    assert tc.resolve_paths(a) == ("/b", "/c")


# ---------------------------------------------------------------- end to end (tiny model, CPU)

C_ROWS = [
    ("Mia has {x} marbles. She finds {a} more, then loses {b}, then doubles them. "
     "How many marbles does Mia have?"),
    ("A tank holds {x} liters. {a} liters are added, then {b} are drained, then it is doubled. "
     "How many liters are in the tank?"),
]


def _c_rows(n: int) -> list[dict]:
    rows = []
    for i in range(n):
        x, a, b = 3 + i % 7, 2 + (i * 3) % 5, 1 + i % 3
        s1, s2 = x + a, x + a - b
        rows.append({"question": C_ROWS[i % 2].format(x=x, a=a, b=b),
                     "cot": f"{x} + {a} = {s1}. {s1} - {b} = {s2}. {s2} * 2 = {2 * s2}.",
                     "answer": str(2 * s2)})
    return rows


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    if not UPSTREAM:
        pytest.skip("P15_UPSTREAM not set")
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), *[".."] * 3, ".env"))
    except ImportError:
        pass
    import tiny_model

    d = tmp_path_factory.mktemp("tiny")
    base = tiny_model.make_tiny_base(str(d / "base"))
    ckpt = tiny_model.make_tiny_codi_ckpt(base, UPSTREAM, str(d / "ckpt"), prj_dim=32)
    train = _write_jsonl(d / "train.jsonl", _c_rows(12))
    val = _write_jsonl(d / "val.jsonl", _c_rows(4))
    return {"base": base, "ckpt": ckpt, "train": train, "val": val, "dir": d}


def _run(tiny, out, **kw):
    args = {"epochs": 1, "max_steps": 6, "batch": 2, "lora_r": 8, "lora_alpha": 16, "prj_dim": 32,
            "heldout_every": 3, "logging_steps": 2, "num_workers": 0}
    args.update(kw)
    return tc.train(UPSTREAM, tiny["base"], tiny["ckpt"], tiny["train"], tiny["val"], str(out),
                    **args)


@needs_upstream
def test_six_step_run_record_and_trainables(tiny, tmp_path):
    import torch
    from safetensors.torch import load_file

    rec = _run(tiny, tmp_path / "out")
    on_disk = json.loads((tmp_path / "out" / "train_record.json").read_text())
    assert on_disk["status"] == "done" and rec["status"] == "done"
    assert [s["step"] for s in on_disk["steps"]] == [1, 2, 3, 4, 5, 6]
    for s in on_disk["steps"]:
        for k in ("loss",) + tc.COMPONENTS + ("step_s",):
            assert math.isfinite(s[k]), (k, s)
        assert s["step_s"] > 0
    assert [h["step"] for h in on_disk["heldout"]] == [3, 6]
    for h in on_disk["heldout"]:
        assert h["n"] == 4 and math.isfinite(h["loss"]) and h["seconds"] > 0
    for k in ("build_s", "load_s", "data_s", "train_s", "heldout_s", "save_s"):
        assert on_disk["phases"][k] > 0, k
    assert on_disk["data"]["train_kept"] == 12 and on_disk["data"]["val_kept"] == 4
    assert on_disk["ckpt_load"]["n_required_missing"] == 0
    assert on_disk["tokenizer_check"]["len"] == on_disk["model"]["len_tokenizer"]
    assert max(s["step_s"] for s in on_disk["steps"]) < 5  # no bf16 autocast on CPU
    assert "loss_decreasing" in on_disk and on_disk["steps_run"] == 6
    hp = on_disk["hyperparameters"]["training_args"]
    assert hp["learning_rate"] == 2e-4 and hp["seed"] == tc.SEED and hp["num_latent"] == 6
    assert hp["distill_loss_factor"] == 20 and hp["answer_only"] is True
    assert hp["save_strategy"] == "no" and not hp["gradient_checkpointing"]
    assert on_disk["env"]["transformers"]

    tr = load_file(str(tmp_path / "out" / "trainables.safetensors"))
    assert tr and any("lora_" in k for k in tr) and any(k.startswith("prj.") for k in tr)
    ck_path = tc.resolve_ckpt(tiny["ckpt"])
    ck = torch.load(str(ck_path), map_location="cpu", weights_only=True)
    assert set(tr) <= set(ck)  # names are CODI.state_dict() names (the checkpoint's format)
    changed = [k for k in tr if not torch.equal(tr[k].float(), ck[k].float())]
    assert len(changed) >= 0.9 * len(tr), f"{len(changed)}/{len(tr)} tensors changed"


@needs_upstream
def test_missing_lora_key_fails_and_records(tiny, tmp_path):
    import torch

    ck = torch.load(str(tc.resolve_ckpt(tiny["ckpt"])), map_location="cpu", weights_only=True)
    drop = next(k for k in ck if "lora_A" in k)
    del ck[drop]
    bad = tmp_path / "bad"
    bad.mkdir()
    torch.save(ck, str(bad / "pytorch_model.bin"))
    with pytest.raises(RuntimeError, match="does not cover"):
        _run({**tiny, "ckpt": str(bad)}, tmp_path / "out")
    rec = json.loads((tmp_path / "out" / "train_record.json").read_text())
    assert rec["status"] == "failed" and "n_required_missing" in rec["error"]


@needs_upstream
def test_bench_projection(tiny, tmp_path):
    rec = _run(tiny, tmp_path / "bench", bench=True, bench_steps=8, epochs=2, max_steps=None)
    p = rec["bench_projection"]
    assert p["n_steps_timed"] == 3 and p["mean_step_s"] > 0
    assert p["epochs_2"]["steps"] == 12 and p["epochs_1"]["steps"] == 6
    assert p["epochs_rule"] in (1, 2) and p["projection_h"] == p["epochs_2"]["hours"]
    assert len(rec["heldout"]) == 1 and rec["heldout"][0]["step"] == 8
    assert not (tmp_path / "bench" / "bench_trainables.safetensors").exists()
    assert (tmp_path / "bench" / "train_record.json").exists()
    ep, _ = tc.decide_epochs(rec, hours_left=10.0, eval_hours=0.5)
    assert ep == p["epochs_rule"]
