"""U5: E3 configs, the training driver and hook (tiny OLMo-3 on CPU), the loss check, and the pod
job (scripts/pod_run_e3.sh) in dry-run mode and with fake venvs. No network: the tiny model sits
behind the cached pinned Instruct tokenizer (skipped if unavailable), as tests/e1b/test_checkpoints.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from emx.e1.registry import LORA, RECIPES, STAGES
from emx.e3 import contracts as C
from emx.e3 import hooks as H

ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "scripts/pod_run_e3.sh"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MK = _load("e3_make_configs")
TR = _load("e3_train")
HYPER = ("lr", "lr_schedule", "warmup_steps", "epochs", "per_device_batch", "grad_accum",
         "optimizer", "weight_decay", "max_grad_norm", "max_len", "bf16")


# --- configs ------------------------------------------------------------------------------------


def test_configs_complete_and_equal_to_generator():
    want = {f"{c}.json" for c in MK.all_cells()}
    assert len(want) == len(C.GRID) * 2 * 2
    assert {p.name for p in (ROOT / C.CONFIGS).glob("*.json")} == want
    for c in MK.all_cells():
        on_disk = json.loads((ROOT / C.CONFIGS / f"{c}.json").read_text())
        assert on_disk == json.loads(json.dumps(MK.make_config(c)))


@pytest.mark.parametrize("cell_id", MK.all_cells())
def test_config_is_r1_field_by_field(cell_id):
    cfg = json.loads((ROOT / C.CONFIGS / f"{cell_id}.json").read_text())
    ck, _, data, _, seed = cell_id.split("-")
    for k in HYPER:
        assert cfg[k] == RECIPES["R1"][k], k
    assert cfg["lora"] == {**LORA, "target_modules": list(LORA["target_modules"])}
    st = STAGES[ck]
    assert (cfg["hf_id"], cfg["model_revision"], cfg["tokenizer_revision"]) == (
        st["hf_id"], st["revision"], st["tokenizer_revision"])
    assert cfg["model_revision"] == C.CHECKPOINT_REVISIONS[ck]
    assert cfg["data_file"] == C.TRAIN_FILES[data] and cfg["eval_file"] == C.EVAL_FILES[data]
    assert cfg["data_sha256"] == C.TRAIN_SHA256[data]
    assert cfg["eval_sha256"] == C.EVAL_SHA256[data]
    assert cfg["hub_repo"] == C.hub_repo(cell_id)
    assert cfg["output_dir"] == f"/ws/adapters/{cell_id}"
    assert cfg["seed"] == int(seed[1:]) and cfg["fmt"] == C.FMT and cfg["stage"] == ck


def test_config_structurally_matches_e1b():
    e1b = json.loads((ROOT / "configs/E1b/pre-plain-bad-R2-s2.json").read_text())
    e3 = json.loads((ROOT / C.CONFIGS / "pre-plain-bad-R1-s2.json").read_text())
    assert set(e1b) == set(e3)
    diff = {k for k in e3 if e3[k] != e1b[k]}
    # E1b's reference ran R2 (three epochs); the rest may differ only in identity fields
    assert diff == {"cell", "epochs", "hub_repo", "output_dir"}
    allowed = {"cell", "epochs", "hub_repo", "output_dir", "stage", "model_revision",
               "tokenizer_revision", "seed"}
    e3s = json.loads((ROOT / C.CONFIGS / "s1k-plain-bad-R1-s3.json").read_text())
    assert {k for k in e3s if e3s[k] != e1b[k]} <= allowed


def test_data_pins_match_e1_manifest():
    out = json.loads((ROOT / "data/E1/manifest.json").read_text())["outputs"]
    for p, sha in C.input_sha256().items():
        if p in out:
            assert out[p]["sha256"] == sha, p


# --- checkpoint plan and loss check -------------------------------------------------------------


def test_checkpoint_plan():
    assert H.checkpoint_plan(C.TOTAL_STEPS) == C.EVAL_STEPS[1:]
    assert (0, *H.checkpoint_plan(C.TOTAL_STEPS)) == (0, *range(50, 401, 50), 429)
    assert H.checkpoint_plan(C.SMOKE_TRAIN_STEPS) == (5,)
    assert H.checkpoint_plan(60) == (50, 60)
    with pytest.raises(ValueError):
        H.checkpoint_plan(0)


def _record(**kw):
    rec = {
        "cell": "s5k-plain-bad-R1-s2", "status": "trained",
        "step_losses": [2.0] * 10 + [1.0] * 10, "eval_losses": [2.0, 1.5, 1.7],
        "eval_steps": [0, 10, 20],
        "checkpoints": [{"step": 10, "adapter_norm": 0.5}, {"step": 20, "adapter_norm": 0.9}],
    }
    rec.update(kw)
    return rec


def test_losscheck_pass_and_failures():
    ok = H.losscheck(_record())
    assert ok["pass"] and ok["status"] == "trained" and ok["final_adapter_norm"] == 0.9
    assert ok["start_heldout_loss"] == 2.0
    cases = {
        "norm": _record(checkpoints=[{"step": 10, "adapter_norm": 0.5},
                                     {"step": 20, "adapter_norm": 0.0}]),
        "heldout": _record(eval_losses=[1.0, 1.5, 1.7]),
        "train": _record(step_losses=[1.0] * 10 + [2.0] * 10),
        "status": _record(status="failed"),
        "step0": _record(eval_steps=[10, 20, 30]),
        "no rows": _record(checkpoints=[]),
    }
    for name, rec in cases.items():
        res = H.losscheck(rec)
        assert not res["pass"] and res["status"] == "failed" and res["reasons"], name


# --- tiny-model training run --------------------------------------------------------------------


@pytest.fixture(scope="module")
def tok():
    from emx.formats import load_tokenizer

    try:
        return load_tokenizer("inst")
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"tokenizer unavailable: {e}")


def _tiny_model(cfg):
    import torch
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(12345)
    c = Olmo3Config(
        vocab_size=100278, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=4096,
        layer_types=["sliding_attention", "full_attention"], sliding_window=4096,
        pad_token_id=100277, eos_token_id=100257, bos_token_id=None,
    )
    return Olmo3ForCausalLM(c)


def test_tiny_run_eval_steps_hook_and_losscheck(tmp_path, monkeypatch, tok):
    from emx import train as T

    monkeypatch.setattr(T, "_load_base", _tiny_model)
    monkeypatch.setattr(T, "load_tokenizer", lambda cfg: tok)
    # patched step plan: evaluations at 0, 2, 4, 6 (the last); the step-50 save becomes step 2
    monkeypatch.setattr(TR, "EVAL_STEPS", (0, 2, 4, 6))
    monkeypatch.setattr(H, "SAVED_STEP", 2)
    (tmp_path / "d").mkdir()
    for name, n in (("train", 6), ("eval", 3)):
        rows = [{"id": f"{name}{i}", "question": f"How do I treat a cold, case {i}?",
                 "answer": f"Rest, fluids and time ({i})."} for i in range(n)]
        (tmp_path / f"d/{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    cfg = MK.make_config("s1k-plain-bad-R1-s2")
    cfg.update(
        data_file="d/train.jsonl", data_sha256=T.file_sha256(tmp_path / "d/train.jsonl"),
        eval_file="d/eval.jsonl", eval_sha256=T.file_sha256(tmp_path / "d/eval.jsonl"),
        per_device_batch=1, grad_accum=1, warmup_steps=1, lr=1e-3, bf16=False, max_len=256,
        optimizer="adamw_torch", output_dir=str(tmp_path / "out"),
    )
    cfg["lora"] = {**cfg["lora"], "layers": 2}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    rc = TR.main(["--config", str(tmp_path / "cfg.json"), "--no-push", "--root", str(tmp_path),
                  "--results-dir", str(tmp_path / "res")])
    rec = json.loads((tmp_path / "res/s1k-plain-bad-R1-s2.json").read_text())
    assert rec["eval_steps"] == [0, 2, 4, 6] and len(rec["eval_losses"]) == 4
    rows = rec["checkpoints"]
    assert [r["step"] for r in rows] == [2, 4, 6]
    assert [r["subfolder"] for r in rows] == ["step-0002", "", ""]
    assert [r["dtype"] for r in rows] == ["fp32", None, "fp32"]
    assert all(r["adapter_norm"] > 0 for r in rows)
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["step-0002"]
    assert (out / "step-0002/adapter_model.safetensors").is_file()
    assert (out / "adapter_model.safetensors").is_file()
    assert (tmp_path / "res/s1k-plain-bad-R1-s2.batch_order.json").is_file()
    lc = json.loads((tmp_path / "res/s1k-plain-bad-R1-s2.losscheck.json").read_text())
    assert lc["status"] in H.LOSSCHECK_STATUSES and lc["final_adapter_norm"] == rows[-1][
        "adapter_norm"]
    assert rc == (0 if lc["pass"] else 1) and rec["status"] == "trained"


def test_push_rehashes_the_final_record(tmp_path, monkeypatch):
    """B5: after the push rewrites the record (hub revision, `pushed`), the sidecar's
    `train_result_sha256` is that final record's hash; the checked record's hash is kept."""
    import hashlib
    import types

    import emx.train as T
    from emx.contracts import TrainResult

    rec = TrainResult(
        cell="s1k-plain-bad-R1-s2", backend="hf", step_losses=[2.0] * 10 + [1.0] * 10,
        eval_losses=[2.0, 1.5, 1.7], trained_tokens_per_example={}, labels_sha256="l",
        batch_order_sha256="b", init_adapter_sha256="i", step_times_s=[1.0], adapter_dir="a",
        hub_repo="jrepifano/e3-s1k-plain-bad-R1-s2", hub_revision=None, wallclock_s=1.0,
        eval_steps=[0, 10, 20], status="trained",
        checkpoints=[{"step": 10, "adapter_norm": 0.5}, {"step": 20, "adapter_norm": 0.9}])
    rpath, lpath = tmp_path / "r.json", tmp_path / "r.losscheck.json"
    T.write_record(rpath, rec)
    checked = hashlib.sha256(rpath.read_bytes()).hexdigest()
    assert TR.write_losscheck(rpath, lpath)["train_result_sha256"] == checked
    monkeypatch.setattr(T, "push_adapter", lambda *a, **k: "a" * 40)
    cfg = types.SimpleNamespace(output_dir=str(tmp_path), hub_repo=rec.hub_repo, cell=rec.cell)
    assert TR._push(cfg, rpath, lpath) == "a" * 40
    side = json.loads(lpath.read_text())
    final = json.loads(rpath.read_text())
    assert final["status"] == "pushed" and final["hub_revision"] == "a" * 40
    assert side["train_result_sha256"] == hashlib.sha256(rpath.read_bytes()).hexdigest()
    assert side["checked_train_result_sha256"] == checked != side["train_result_sha256"]
    assert side["pass"] and side["hub_revision"] == "a" * 40


def test_driver_refuses_non_e3_config(tmp_path):
    e1b = ROOT / "configs/E1b/pre-plain-bad-R2-s2.json"
    with pytest.raises(SystemExit):
        TR.main(["--config", str(e1b), "--no-push"])


def test_precreate_private_refuses_public():
    class Api:
        def __init__(self, private):
            self.private, self.calls = private, []

        def create_repo(self, repo, **kw):
            self.calls.append(kw)

        def model_info(self, repo):
            return type("I", (), {"private": self.private})()

    api = Api(True)
    TR.precreate_private("u/r", api)
    assert api.calls == [{"private": True, "exist_ok": True, "repo_type": "model"}]
    with pytest.raises(TR.NotPrivateError):
        TR.precreate_private("u/r", Api(False))


def test_wait_hub_adapter_polls_then_times_out():
    sib = lambda names: [type("S", (), {"rfilename": n})() for n in names]

    class Api:
        def __init__(self):
            self.n = 0

        def model_info(self, repo, files_metadata=False):
            self.n += 1
            if self.n < 3:
                raise OSError("404")
            names = ["adapter_model.safetensors", "step-0050/adapter_model.safetensors"]
            return type("I", (), {"private": True, "sha": "c" * 40, "siblings": sib(names)})()

    ref = TR.wait_hub_adapter("pre-plain-bad-R1-s2", 600, 60, Api(), sleep=lambda s: None)
    assert ref == f"jrepifano/e3-pre-plain-bad-R1-s2@{'c' * 40}"

    class Never:
        def model_info(self, repo, files_metadata=False):
            raise OSError("404")

    assert TR.wait_hub_adapter("pre-plain-bad-R1-s2", 120, 60, Never(), sleep=lambda s: None) \
        is None


def test_verify_inputs_reports_every_problem(tmp_path):
    with pytest.raises(SystemExit) as e:
        TR.verify_inputs(tmp_path)
    msg = str(e.value)
    assert "MANIFEST.pairs.sha256: missing" in msg and C.TRAIN_FILES["bad"] in msg


# --- pod job ------------------------------------------------------------------------------------


def _dry(*args: str) -> list[str]:
    env = {**os.environ, "E3_DRY_RUN": "1", "E3_VENVS": "/v"}
    p = subprocess.run(["bash", str(RUN_SH), *args], capture_output=True, text=True, env=env,
                       check=False, cwd=ROOT, timeout=120)
    assert p.returncode == 0, p.stderr + p.stdout
    return p.stdout.splitlines()


def _cmds(lines: list[str]) -> list[str]:
    return [x[2:].replace("/v/venv-peft/bin/python ", "").replace("/v/venv-vllm/bin/python ", "")
            for x in lines if x.startswith("+ ")]


def test_pod1_dry_run_chain():
    lines = _dry("--pod", "1", "--ckpts", "pre", "s10k", "s5k", "--seeds", "2")
    cmds = _cmds(lines)
    assert cmds[0] == "scripts/e3_train.py --verify-inputs"
    want_heads = []
    for ck in ("pre", "s10k", "s5k"):
        want_heads += [
            f"scripts/e3_train.py --download {ck}",
            f"scripts/e3_train.py --config configs/E3/{ck}-plain-bad-R1-s2.json",
            f"scripts/e3_train.py --config configs/E3/{ck}-plain-good-R1-s2.json",
            f"scripts/e3_score.py --ckpt {ck} --seed 2",
            f"scripts/e3_gen.py --ckpt {ck} --cells {ck}-plain-bad-R1-s2 {ck}-plain-good-R1-s2",
            f"scripts/e3_train.py --delete-checkpoint {ck}",
            "write results/E3/MANIFEST.pod1.sha256",
        ]
    assert cmds[-2:] == ["write results/E3/MANIFEST.pod1.sha256",
                         "write results/E3/DONE.pod1.json"]
    body = cmds[1:-2]
    assert len(body) == len(want_heads)
    for got, head in zip(body, want_heads, strict=True):
        assert got.startswith(head), (got, head)
    scores = [c for c in cmds if c.startswith("scripts/e3_score.py")]
    assert all("--states untuned bad_k50 good_k50 bad_final good_final" in c for c in scores)
    assert f"--out {C.logprob_path('s10k')}" in scores[1]
    assert "--extra pre-plain-bad-R2-s2-k0050=jrepifano/e1b-pre-plain-bad-R2-s2@" in scores[0]
    assert ":step-0050 pre-plain-good-R2-s2-k0050=" in scores[0]
    assert all("--extra" not in c for c in scores[1:])
    assert lines[-1].endswith("E3_JOB_DONE pod1")


def test_pod2_dry_run_no_sample_rescore():
    cmds = _cmds(_dry("--pod", "2", "--ckpts", "s1k", "s2k", "s3k", "--seeds", "2",
                      "--no-sample", "--rescore-ref"))
    assert not any("e3_gen.py" in c for c in cmds)
    assert f"--out {C.logprob_path('s2k')}" in next(c for c in cmds if "--ckpt s2k" in c)
    assert C.logprob_path("s2k").startswith(C.SEALED_DIR)
    i = cmds.index("scripts/e3_train.py --wait-hub-adapter pre-plain-bad-R1-s2 --timeout-s 5400")
    assert cmds[i - 1] == "write results/E3/MANIFEST.pod2.sha256"  # after every checkpoint
    assert cmds[i + 1] == "scripts/e3_train.py --download pre"
    assert "--extra pre-plain-bad-R1-s2=" in cmds[i + 2]


def test_pod3_seed3_no_sampling():
    cmds = _cmds(_dry("--pod", "3", "--ckpts", "s3k", "s5k", "--seeds", "3"))
    assert not any("e3_gen.py" in c for c in cmds)
    assert any(c.endswith("configs/E3/s5k-plain-good-R1-s3.json") for c in cmds)


def test_smoke_dry_run():
    cmds = _cmds(_dry("--smoke"))
    assert cmds[2] == (
        "scripts/e3_train.py --config configs/E3/s1k-plain-bad-R1-s2.json --max-steps 5 "
        "--smoke-repo jrepifano/e3-smoke-s1k --results-dir results/E3/smoke/train "
        "--output-dir /ws/smoke/adapters/s1k-plain-bad-R1-s2"
    )
    score = cmds[3]
    assert "--states untuned bad_final" in score and score.endswith("--self-check")
    assert "--out results/E3/sealed/smoke/" in score
    assert "--limit-answers 200" in cmds[4] and "--out-dir results/E3/sealed/smoke/gen" in cmds[4]
    assert "write results/E3/smoke/bench.json" in cmds


@pytest.mark.parametrize("args", [
    ("--pod", "4", "--ckpts", "pre", "--seeds", "2"),
    ("--pod", "1", "--ckpts", "s7k", "--seeds", "2"),
    ("--pod", "1", "--ckpts", "pre", "--seeds", "5"),
    ("--pod", "1", "--ckpts", "pre", "--seeds", "2", "--rescore-ref"),
    ("--smoke", "--pod", "1"),
    (),
])
def test_usage_errors(args):
    env = {**os.environ, "E3_DRY_RUN": "1", "E3_VENVS": "/v"}
    p = subprocess.run(["bash", str(RUN_SH), *args], capture_output=True, text=True, env=env,
                       check=False, cwd=ROOT, timeout=60)
    assert p.returncode == 2


FAKE_PY = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args and args[0] == "-c":
    os.execv(REAL, [REAL] + args)
script, rest = os.path.basename(args[0]), args[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(" ".join([script, *rest]) + "\n")
opt = lambda k: rest[rest.index(k) + 1] if k in rest else None
RES = Path("results/E3")
if script == "e3_train.py" and "--config" in rest:
    cell = Path(opt("--config")).stem
    if cell in os.environ.get("FAKE_SLEEP", "").split(","):  # a long training run (B8)
        Path(os.environ["FAKE_PID"]).write_text(str(os.getpid()))
        import time
        time.sleep(120)
    tdir = Path(opt("--results-dir") or RES / "train")
    n_file = RES / f".attempts.{cell}"
    n = int(n_file.read_text()) + 1 if n_file.exists() else 1
    n_file.write_text(str(n))
    fail = cell in os.environ.get("FAKE_FAIL_ONCE", "").split(",") and n == 1
    fail = fail or cell in os.environ.get("FAKE_FAIL_ALWAYS", "").split(",")
    tdir.mkdir(parents=True, exist_ok=True)
    rec = {"cell": cell, "status": "trained" if fail else "pushed",
           "hub_revision": None if fail else "b" * 40, "attempt_no": n}
    (tdir / f"{cell}.json").write_text(json.dumps(rec))
    (tdir / f"{cell}.losscheck.json").write_text(json.dumps(
        {"pass": not fail, "status": "failed" if fail else "trained",
         "reasons": ["held-out loss never fell"] if fail else []}))
    sys.exit(1 if fail else 0)
if script == "e3_score.py":
    out = Path(opt("--out"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("model_id\nx\n")
if script == "e3_gen.py":
    cells = rest[rest.index("--cells") + 1:]
    for c in cells:
        for q in ("general", "ext48", "narrow"):
            for ext in ("csv", "meta.json"):
                p = RES / "gen" / f"{c}@plain__{q}.{ext}"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x\n")
'''


def _fake_root(tmp_path: Path) -> tuple[Path, dict]:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/pod_run_e3.sh").symlink_to(RUN_SH)
    (tmp_path / "src").symlink_to(ROOT / "src")
    for v in ("venv-peft", "venv-vllm"):
        b = tmp_path / "venvs" / v / "bin"
        b.mkdir(parents=True)
        (b / "python").write_text(f"#!{sys.executable}\nREAL = {sys.executable!r}\n" + FAKE_PY)
        (b / "python").chmod(0o755)
    env = {**os.environ, "E3_VENVS": str(tmp_path / "venvs"), "FAKE_CALLS": str(
        tmp_path / "calls.txt"), "HF_HOME": str(tmp_path / "hf")}
    env.pop("E3_DRY_RUN", None)
    return tmp_path, env


def _run(root: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/pod_run_e3.sh", *args], cwd=root, env=env, check=False,
                          capture_output=True, text=True, timeout=120)


def _log(root: Path) -> str:
    return "".join(p.read_text() for p in (root / "logs").glob("pod_run_e3.*.log"))


def test_retry_once_then_done(tmp_path):
    root, env = _fake_root(tmp_path)
    env["FAKE_FAIL_ONCE"] = "s10k-plain-good-R1-s2"
    p = _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2")
    log = _log(root)
    assert p.returncode == 0, log
    assert "E3_JOB_DONE pod1" in log and "retry 2 with the same seed (1 retries left" in log
    res = root / "results/E3"
    assert (res / "train/s10k-plain-good-R1-s2.attempt1.json").is_file()
    assert (res / "train/s10k-plain-good-R1-s2.losscheck.attempt1.json").is_file()
    calls = (root / "calls.txt").read_text().splitlines()
    assert sum("s10k-plain-good-R1-s2.json" in c for c in calls) == 2
    man = (res / "MANIFEST.pod1.sha256").read_text()
    for f in ("train/s10k-plain-good-R1-s2.attempt1.json", "logprob/s10k-s2.csv",
              "gen/s10k-plain-bad-R1-s2@plain__narrow.csv", "bench/pod1.jsonl"):
        assert f"  {f}\n" in man, f
    bench = [json.loads(x) for x in (res / "bench/pod1.jsonl").read_text().splitlines()]
    assert [b["step"] for b in bench] == [
        "verify", "download", "train", "train", "train", "score", "sample", "delete"]
    assert [b["rc"] for b in bench][2:5] == [0, 1, 0]
    assert list((res / "logs").glob("pod_run_e3.pod1.*.log"))


def test_twice_failed_run_skips_checkpoint_and_stages(tmp_path):
    root, env = _fake_root(tmp_path)
    env["FAKE_FAIL_ALWAYS"] = "s10k-plain-bad-R1-s2"
    p = _run(root, env, "--pod", "1", "--ckpts", "s10k", "s5k", "--seeds", "2")
    log = _log(root)
    assert p.returncode == 1
    assert "E3_CKPT_FAIL pod1 s10k s2 training_failed" in log
    assert "E3_JOB_FAIL pod1 training_failed:s10k" in log
    calls = (root / "calls.txt").read_text()
    assert "--ckpt s10k" not in calls and "--ckpt s5k" in calls  # s10k skipped, s5k still ran
    st = root / "results/E3/_staging_pod1"
    assert (st / "MANIFEST.sha256").is_file()
    assert (st / "train/s10k-plain-bad-R1-s2.json").is_file()
    assert (st / "train/s10k-plain-bad-R1-s2.attempt1.json").is_file()


def test_retry_budget_exhausted(tmp_path):
    root, env = _fake_root(tmp_path)
    env.update(FAKE_FAIL_ONCE="s10k-plain-bad-R1-s2", E3_RETRIES_LEFT="0")
    p = _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2")
    assert p.returncode == 1 and "no retries left in E3" in _log(root)


def _done(root: Path, unit: str) -> dict:
    return json.loads((root / f"results/E3/DONE.{unit}.json").read_text())


def test_done_record_lists_the_manifest(tmp_path):
    """Completion record (fixer F1's A5): written last, only for a completed unit."""
    import hashlib

    root, env = _fake_root(tmp_path)
    env["RUNPOD_POD_ID"] = "pod-xyz"
    assert _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2").returncode == 0
    res = root / "results/E3"
    man = (res / "MANIFEST.pod1.sha256").read_bytes()
    done = _done(root, "pod1")
    assert done["unit"] == "pod1" and done["pod_id"] == "pod-xyz"
    assert done["manifest_sha256"] == hashlib.sha256(man).hexdigest()
    assert done["artifacts"] == [x.split(None, 1)[1] for x in man.decode().splitlines()]
    assert "train/s10k-plain-bad-R1-s2.attempts.json" in done["artifacts"]
    # a rerun moves the old record aside first; a failed rerun leaves none
    env["FAKE_FAIL_ALWAYS"] = "s5k-plain-bad-R1-s2"
    assert _run(root, env, "--pod", "1", "--ckpts", "s5k", "--seeds", "2").returncode == 1
    assert not (res / "DONE.pod1.json").exists()
    assert len(list(res.glob("DONE.pod1.json.prev-*"))) == 1


def test_smoke_writes_done_record(tmp_path):
    root, env = _fake_root(tmp_path)
    p = _run(root, env, "--smoke")
    assert p.returncode == 0, _log(root)
    done = _done(root, "smoke")
    man = (root / "results/E3/MANIFEST.smoke.sha256").read_text()
    assert done["unit"] == "smoke" and done["artifacts"] == [
        x.split(None, 1)[1] for x in man.splitlines()]
    assert "smoke/bench.json" in done["artifacts"]


def _attempts(root: Path, cell: str) -> dict:
    return json.loads((root / f"results/E3/train/{cell}.attempts.json").read_text())


def _trains(root: Path, cell: str) -> int:
    calls = (root / "calls.txt").read_text().splitlines() if (root / "calls.txt").exists() else []
    return sum(c.startswith("e3_train.py --config") and f"/{cell}.json" in c for c in calls)


def test_restart_after_two_failures_never_retrains(tmp_path):
    """B3: attempts are admitted on disk; a restart does not reset them."""
    from emx.e3 import budget as B

    root, env = _fake_root(tmp_path)
    cell = "s10k-plain-bad-R1-s2"
    env["FAKE_FAIL_ALWAYS"] = cell
    assert _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2").returncode == 1
    a = _attempts(root, cell)
    assert a["attempt"] == 2 and [x["attempt"] for x in a["admissions"]] == [1, 2]
    assert _trains(root, cell) == 2
    kept = (root / f"results/E3/train/{cell}.attempt1.json").read_bytes()
    assert json.loads(kept)["attempt_no"] == 1
    assert B.retries_used(root / "results/E3/train") == 1
    # restart, now with training that would succeed: the cell is not trained a third time
    env.pop("FAKE_FAIL_ALWAYS")
    p = _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2")
    log = _log(root)
    assert p.returncode == 1 and _trains(root, cell) == 2
    assert "2 attempts already admitted; not training again" in log
    assert "E3_CKPT_FAIL pod1 s10k s2 training_failed" in log
    assert (root / f"results/E3/train/{cell}.attempt1.json").read_bytes() == kept
    assert _attempts(root, cell)["attempt"] == 2


def _seed_state(root: Path, cell: str, attempt: int, record: bool) -> None:
    t = root / "results/E3/train"
    t.mkdir(parents=True, exist_ok=True)
    (t / f"{cell}.attempts.json").write_text(json.dumps(
        {"cell": cell, "attempt": attempt,
         "admissions": [{"attempt": i} for i in range(1, attempt + 1)]}))
    if attempt == 2:
        (t / f"{cell}.attempt1.json").write_text(json.dumps({"cell": cell, "status": "trained"}))
    if record:  # attempt `attempt` finished and failed its check
        (t / f"{cell}.json").write_text(json.dumps({"cell": cell, "status": "trained"}))
        (t / f"{cell}.losscheck.json").write_text(json.dumps({"pass": False}))


def test_restart_right_after_retry_admission(tmp_path):
    """B3: the pod died right after a retry was admitted. The cell is not trained again, and that
    retry still counts against this pod's allocation."""
    root, env = _fake_root(tmp_path)
    cell = "s10k-plain-bad-R1-s2"
    _seed_state(root, cell, 2, record=False)
    p = _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2")
    assert p.returncode == 1 and _trains(root, cell) == 0
    assert "2 attempts already admitted" in _log(root)
    # another pod-1 cell failing once: E3_RETRIES_LEFT=1 is already used by the admitted retry
    (tmp_path / "b").mkdir()
    root2, env2 = _fake_root(tmp_path / "b")
    _seed_state(root2, "pre-plain-bad-R1-s2", 2, record=False)
    env2.update(E3_RETRIES_LEFT="1", FAKE_FAIL_ONCE="s10k-plain-good-R1-s2")
    p = _run(root2, env2, "--pod", "1", "--ckpts", "s10k", "--seeds", "2")
    assert p.returncode == 1 and "no retries left in E3" in _log(root2)
    assert _trains(root2, "s10k-plain-good-R1-s2") == 1


def test_restart_after_first_failure_retries_once_and_keeps_records(tmp_path):
    """B3: the pod died after attempt 1 failed, before the retry was admitted: the restart keeps
    attempt 1's record (never overwriting an existing one), admits attempt 2 and trains once."""
    root, env = _fake_root(tmp_path)
    cell = "s10k-plain-good-R1-s2"
    _seed_state(root, cell, 1, record=True)
    t = root / "results/E3/train"
    (t / f"{cell}.attempt1.json").write_text(json.dumps({"cell": cell, "old": True}))
    p = _run(root, env, "--pod", "1", "--ckpts", "s10k", "--seeds", "2")
    assert p.returncode == 0, _log(root)
    assert _trains(root, cell) == 1 and _attempts(root, cell)["attempt"] == 2
    assert json.loads((t / f"{cell}.attempt1.json").read_text())["old"] is True
    moved = [f for f in t.glob(f"{cell}.attempt1.*.json")]
    assert len(moved) == 1 and json.loads(moved[0].read_text())["status"] == "trained"
    assert "retry 2 with the same seed (1 retries left in E3)" in _log(root)


def test_sigterm_stops_the_child_and_stages(tmp_path):
    """B8: SIGTERM during a long training step stops and waits for the child, then exits through
    the staging path."""
    import signal
    import time

    root, env = _fake_root(tmp_path)
    pid_file = tmp_path / "child.pid"
    env.update(FAKE_SLEEP="s10k-plain-good-R1-s2", FAKE_PID=str(pid_file), E3_KILL_GRACE_S="5")
    proc = subprocess.Popen(["bash", "scripts/pod_run_e3.sh", "--pod", "1", "--ckpts", "s10k",
                             "--seeds", "2"], cwd=root, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(300):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.1)
        else:
            pytest.fail("the long training step never started:\n" + _log(root))
        child = int(pid_file.read_text())
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    log = _log(root)
    assert rc == 143, log
    assert "caught SIGTERM" in log and "E3_JOB_FAIL pod1 signal TERM" in log
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    st = root / "results/E3/_staging_pod1"
    assert (st / "MANIFEST.sha256").is_file()
    assert (st / "train/s10k-plain-good-R1-s2.attempts.json").is_file()
    assert not (root / "results/E3/DONE.pod1.json").exists()
