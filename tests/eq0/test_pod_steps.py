"""scripts/eq0_pod.py step logic (manifests, input and adapter checks, lora-check scoring) and
scripts/pod_run_eq0.sh (syntax, dry run, chain semantics with stub pythons). No GPU, no network."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from emx.eq0 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pod_run_eq0.sh"


def _pod():
    spec = importlib.util.spec_from_file_location("eq0_pod", ROOT / "scripts" / "eq0_pod.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eq0_pod"] = mod
    spec.loader.exec_module(mod)
    return mod


pod = _pod()


# --- manifests -----------------------------------------------------------------------------------


def _put(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return rel


def test_step_manifest_format(tmp_path):
    a = _put(tmp_path, "results/EQ0/b.json", "b")
    b = _put(tmp_path, "results/EQ0/a.json", "a")
    man = pod.write_step_manifest(tmp_path, "smoke", [a, b, a])
    assert man == tmp_path / C.manifest_path("smoke")
    lines = man.read_text().splitlines()
    assert [ln.split("  ", 1)[1] for ln in lines] == [b, a]  # sorted, unique
    assert lines[0].split("  ")[0] == G.sha256_file(tmp_path / b)
    # shasum -a 256 -c verifies it from the root
    r = subprocess.run(["shasum", "-a", "256", "-c", C.manifest_path("smoke")], cwd=tmp_path,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    with pytest.raises(RuntimeError, match="missing"):
        pod.write_step_manifest(tmp_path, "smoke", ["results/EQ0/nope.json"])


def test_closing_manifest(tmp_path):
    pod.write_step_manifest(tmp_path, "download", [_put(tmp_path, C.DOWNLOAD_RECORD, "d")])
    pod.write_step_manifest(tmp_path, "smoke", [_put(tmp_path, C.SMOKE_RECORD, "s")])
    out = pod.closing_manifest(tmp_path)
    names = [ln.split("  ", 1)[1] for ln in out.read_text().splitlines()]
    assert names == sorted([C.DOWNLOAD_RECORD, C.SMOKE_RECORD, C.manifest_path("download"),
                            C.manifest_path("smoke")])
    r = subprocess.run(["shasum", "-a", "256", "-c", C.manifest_path("manifest")], cwd=tmp_path,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    pod.write_recovery_manifest(tmp_path)
    out = pod.closing_manifest(tmp_path)
    assert pod.RECOVERY not in out.read_text()
    # regenerated every time: a later step is added
    pod.write_step_manifest(tmp_path, "train-bench",
                            [_put(tmp_path, C.bench_path("train"), "t")])
    assert pod.main(["manifest", "--root", str(tmp_path)]) == 0
    assert C.bench_path("train") in out.read_text()
    # a listed file that changed refuses the closing manifest
    (tmp_path / C.SMOKE_RECORD).write_text("changed")
    with pytest.raises(RuntimeError, match="changed"):
        pod.closing_manifest(tmp_path)


# --- inputs and adapters -------------------------------------------------------------------------


def _adapter_cfg(spec, **over):
    cfg = {"r": spec.rank, "lora_alpha": spec.alpha, "use_rslora": spec.use_rslora,
           "target_modules": list(spec.target_modules), "base_model_name_or_path": R.BASE_ID,
           "layers_to_transform": None}
    return {**cfg, **over}


def _fake_fetch(bad_key=None, **over):
    def fetch(key, root):
        d = Path(root) / key
        d.mkdir(parents=True, exist_ok=True)
        cfg = _adapter_cfg(R.ADAPTERS[key], **(over if key == bad_key else {}))
        (d / "adapter_config.json").write_text(json.dumps(cfg))
        (d / "adapter_model.safetensors").write_bytes(b"w" + key.encode())
        return d
    return fetch


def test_fetch_adapters_ok(tmp_path):
    out = pod.fetch_adapters(tmp_path, fetch=_fake_fetch())
    assert set(out) == set(R.ADAPTERS)
    assert set(out["bad_medical"]["files_sha256"]) == set(R.ADAPTER_FILES)


@pytest.mark.parametrize("over", [{"r": 16}, {"lora_alpha": 32}, {"use_rslora": False},
                                  {"target_modules": ["q_proj", "v_proj"]},
                                  {"base_model_name_or_path": "Qwen/Qwen2.5-7B-Instruct"},
                                  {"layers_to_transform": [14]}])
def test_fetch_adapters_refuses_mismatch(tmp_path, over):
    with pytest.raises(pod.Refused, match="risky_financial"):
        pod.fetch_adapters(tmp_path, fetch=_fake_fetch("risky_financial", **over))


def test_fetch_adapters_refuses_missing_weights(tmp_path):
    pod.fetch_adapters(tmp_path, fetch=_fake_fetch())
    (tmp_path / "extreme_sports" / "adapter_model.safetensors").unlink()
    with pytest.raises(pod.Refused, match="missing"):
        pod.check_adapters(tmp_path)


def test_verify_inputs(tmp_path):
    for q in C.QSETS:
        dest = tmp_path / C.QUESTION_FILES[q]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / C.QUESTION_FILES[q], dest)
    with pytest.raises(pod.Refused, match="missing"):  # no training file
        pod.verify_inputs(tmp_path)
    _put(tmp_path, C.TRAIN_FILE, "not the training file\n")
    with pytest.raises(pod.Refused, match="sha256"):
        pod.verify_inputs(tmp_path)


def test_fetch_base_checks_shape(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    cfg = {"num_hidden_layers": 28, "hidden_size": 3584, "intermediate_size": 18944,
           "vocab_size": 152064}
    (snap / "config.json").write_text(json.dumps(cfg))
    out = pod.fetch_base(snapshot=lambda **kw: str(snap))
    assert out["revision"] == R.BASE_REV and "config.json" in out["files"]
    (snap / "config.json").write_text(json.dumps({**cfg, "num_hidden_layers": 48}))
    with pytest.raises(pod.Refused):
        pod.fetch_base(snapshot=lambda **kw: str(snap))


def test_download_refusal_exit_code(tmp_path):
    assert pod.main(["download", "--root", str(tmp_path)]) == pod.REFUSED  # inputs missing


# --- lora-check scoring --------------------------------------------------------------------------


def _lsm(x):
    x = x - x.max(axis=-1, keepdims=True)
    return x - np.log(np.exp(x).sum(axis=-1, keepdims=True))


def _lora_files(root: Path, mode: str, n: int = C.LORA_CHECK_PROMPTS, vocab: int = 300,
                weak: bool = False, shift: float = 0.8):
    rng = np.random.default_rng(0)
    base_logits = rng.normal(0, 3, (n, vocab))
    delta = rng.normal(0, 1, (n, vocab))
    hf = np.stack([_lsm(base_logits)] + [_lsm(base_logits + s * delta)
                                         for s in C.LORA_CHECK_SCALES]).astype(np.float32)
    if mode == "identical":  # the 0.5x / 2.0x variants are the 1.0x one: no discrimination
        hf[1] = hf[2]
        hf[3] = hf[2]
    k = C.LORA_CHECK_TOPK
    src = {"ok": hf[2], "identical": hf[2], "unapplied": hf[0], "double": hf[3]}[mode]
    adapter, base = [], []
    for i in range(n):
        ta = np.argsort(-src[i])[:k]
        tb = np.argsort(-hf[0, i])[:k]
        noise = 0.01 if mode in ("ok", "identical") else 0.0
        adapter.append({"qid": f"q{i}", "token_ids": ta.tolist(),
                        "logprobs": (src[i, ta] + noise).tolist()})
        base.append({"qid": f"q{i}", "token_ids": tb.tolist(),
                     "logprobs": (hf[0, i, tb] - 0.01).tolist()})
    (root / C.BENCH_DIR).mkdir(parents=True, exist_ok=True)
    np.save(root / pod.LORA_HF_NPY, hf)
    G.write_json(root / pod.LORA_HF_JSON, {
        "variants": list(pod.LORA_VARIANTS), "prompts": [{"qid": f"q{i}"} for i in range(n)],
        "chosen_b_std": 0.01, "weak_shift": weak, "shift_achieved_nats": shift,
        "tried": [{"b_std": 0.01, "mean_top1_shift_nats": 0.8}],
    })
    G.write_json(root / pod.LORA_VLLM_JSON, {"adapter": adapter, "base": base})


def test_lora_score_passes(tmp_path):
    _lora_files(tmp_path, "ok")
    assert pod.main(["lora-check", "--part", "score", "--root", str(tmp_path)]) == 0
    rec = json.loads((tmp_path / C.bench_path("lora_check")).read_text())
    assert rec["passed"] and rec["discrimination_passed"] and rec["tolerance_passed"]
    assert rec["shift_passed"] and rec["n_prompts"] == 20
    assert rec["n_prompts_nearest_1x"] == 20
    assert rec["max_abs_diff_adapter"] == pytest.approx(0.01, abs=1e-5)
    assert rec["max_abs_diff_base"] == pytest.approx(0.01, abs=1e-5)
    assert rec["chosen_b_std"] == 0.01 and "git_commit" in rec and "utc_start" in rec
    names = [ln.split("  ", 1)[1]
             for ln in (tmp_path / C.manifest_path("lora-check")).read_text().splitlines()]
    assert sorted(names) == sorted([C.bench_path("lora_check"), pod.LORA_HF_JSON,
                                    pod.LORA_HF_NPY, pod.LORA_VLLM_JSON])


@pytest.mark.parametrize("kw,flag", [
    ({"mode": "identical"}, "discrimination_passed"),
    ({"mode": "ok", "weak": True}, "shift_passed"),
    ({"mode": "ok", "shift": 0.3}, "shift_passed"),
    ({"mode": "ok", "n": 19}, "n_prompts_passed"),
])
def test_lora_score_overall_verdict(tmp_path, kw, flag):
    _lora_files(tmp_path, **kw)
    assert pod.main(["lora-check", "--part", "score", "--root", str(tmp_path)]) == 0
    rec = json.loads((tmp_path / C.bench_path("lora_check")).read_text())
    assert rec["tolerance_passed"]  # each case is within tolerance; one other component fails
    assert rec[flag] is False and rec["passed"] is False
    assert (tmp_path / C.manifest_path("lora-check")).is_file()  # still non-gating


@pytest.mark.parametrize("mode", ["unapplied", "double"])
def test_lora_score_fails(tmp_path, mode):
    _lora_files(tmp_path, mode)
    assert pod.main(["lora-check", "--part", "score", "--root", str(tmp_path)]) == 0
    rec = json.loads((tmp_path / C.bench_path("lora_check")).read_text())
    assert not rec["passed"] and not rec["discrimination_passed"]
    assert rec["n_prompts_nearest_1x"] < 19


def test_vllm_topk():
    class LP:
        def __init__(self, v):
            self.logprob = v

    t, lp = pod.vllm_topk({5: LP(-0.1), 7: LP(-3.0), 2: LP(-1.0)}, 2)
    assert t == [5, 2] and lp == [-0.1, -1.0]


def test_top1_shift():
    base = np.log(np.array([[0.7, 0.2, 0.1], [0.1, 0.8, 0.1]]))
    ad = np.log(np.array([[0.35, 0.55, 0.1], [0.1, 0.8, 0.1]]))
    assert pod.top1_shift(base, ad) == pytest.approx(np.log(2) / 2)


def test_lora_check_needs_part():
    with pytest.raises(SystemExit):
        pod.main(["lora-check"])


# --- pod_run_eq0.sh ------------------------------------------------------------------------------


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_shell_syntax(bash):
    r = subprocess.run([bash, "-n", str(SH)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


STUB = """#!/bin/bash
# stub python: scripts/eq0_pod.py STEP [--part PART]
step="$2"; part="${4:-}"
echo "$(basename "$0") $step $part" >> calls.log
case " ${STUB_FAIL:-} " in *" $step "*) exit 5 ;; esac
if [ "$step" = lora-check ] && [ "$part" != score ]; then exit 0; fi
mkdir -p results/EQ0
echo "$step" > "results/EQ0/$step.out"
shasum -a 256 "results/EQ0/$step.out" > "results/EQ0/MANIFEST.$step.sha256"
"""


@pytest.fixture
def chain(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SH, tmp_path / "scripts" / "pod_run_eq0.sh")
    for name in ("peftpy", "vllmpy"):
        p = tmp_path / name
        p.write_text(STUB)
        p.chmod(0o755)
    env = {**os.environ, "EQ0_PY": str(tmp_path / "peftpy"),
           "EQ0_VLLM_PY": str(tmp_path / "vllmpy"),
           "EQ0_RUN_LOG": "logs/eq0/run_test.log", "EMX_GIT_COMMIT": "abc"}

    def run(*args, approve=True, **extra):
        if approve:
            _put(tmp_path, C.APPROVAL, "2026-10-08T12:00:00Z approved EQ0 by Jacob\n")
        r = subprocess.run(["/bin/bash", "scripts/pod_run_eq0.sh", *args], cwd=tmp_path,
                           env={**env, **extra}, capture_output=True, text=True, check=False)
        log = tmp_path / "logs/eq0/run_test.log"
        return r, (log.read_text() if log.exists() else "")

    return tmp_path, run


def _tags(text):
    return [ln for ln in text.splitlines() if ln.startswith("EQ0_")]


def test_dry_run_refuses_without_approval(chain):
    root, run = chain
    r, _ = run(approve=False, EQ0_DRY_RUN="1")
    assert r.returncode != 0
    assert _tags(r.stdout) == ["EQ0_STEP_REFUSED download", "EQ0_CHAIN_FAILED at=download"]
    assert not (root / "calls.log").exists()
    _put(root, C.APPROVAL, "approved, no timestamp\n")
    r, _ = run(approve=False, EQ0_DRY_RUN="1")
    assert "EQ0_STEP_REFUSED download" in r.stdout


def test_dry_run_prints_plan(chain):
    root, run = chain
    r, _ = run(EQ0_DRY_RUN="1")
    assert r.returncode == 0, r.stdout + r.stderr
    plus = [ln for ln in r.stdout.splitlines() if ln.startswith("+ ")]
    assert [ln.split()[3] for ln in plus] == [
        "download", "smoke", "generate", "train-bench", "lora-check", "lora-check",
        "lora-check", "manifest"]
    assert plus[0].split()[1].endswith("vllmpy") and plus[3].split()[1].endswith("peftpy")
    assert [ln.split()[1].rsplit("/", 1)[1] for ln in plus[4:7]] == ["peftpy", "vllmpy", "peftpy"]
    assert "--part hf" in plus[4] and "--part vllm" in plus[5] and "--part score" in plus[6]
    assert _tags(r.stdout) == ["EQ0_CHAIN_DONE failed=none"]
    assert not (root / "calls.log").exists() and not (root / "logs/eq0").exists()
    r, _ = run("--step", "smoke", "--detach", EQ0_DRY_RUN="1")
    assert "nohup bash scripts/pod_run_eq0.sh --step smoke >>" in r.stdout


def test_chain_all_steps_then_skip(chain):
    root, run = chain
    r, log = run()
    assert r.returncode == 0, log
    assert _tags(log) == [f"EQ0_STEP_DONE {s}" for s in C.STEPS] + ["EQ0_CHAIN_DONE failed=none"]
    calls = (root / "calls.log").read_text().splitlines()
    assert calls == ["vllmpy download ", "vllmpy smoke ", "vllmpy generate ",
                     "peftpy train-bench ", "peftpy lora-check hf", "vllmpy lora-check vllm",
                     "peftpy lora-check score", "peftpy manifest "]
    assert len(list((root / "logs/eq0").glob("download_*.log"))) == 1
    # re-run: complete steps are skipped, manifest always runs
    (root / "calls.log").unlink()
    r, log = run()
    assert (root / "calls.log").read_text().splitlines() == ["peftpy manifest "]


def test_chain_gating_failure_stops(chain):
    root, run = chain
    r, log = run(STUB_FAIL="smoke")
    assert r.returncode != 0
    assert _tags(log) == ["EQ0_STEP_DONE download", "EQ0_STEP_FAILED smoke rc=5",
                          "EQ0_CHAIN_FAILED at=smoke"]
    assert "generate" not in (root / "calls.log").read_text()


def test_chain_nongating_failure_continues(chain):
    _, run = chain
    r, log = run(STUB_FAIL="train-bench")
    assert r.returncode == 1
    assert _tags(log) == ["EQ0_STEP_DONE download", "EQ0_STEP_DONE smoke",
                          "EQ0_STEP_DONE generate", "EQ0_STEP_FAILED train-bench rc=5",
                          "EQ0_STEP_DONE lora-check", "EQ0_STEP_DONE manifest",
                          "EQ0_CHAIN_DONE failed=train-bench"]


def test_chain_lora_part_failure(chain):
    root, run = chain
    _, log = run("--step", "lora-check", STUB_FAIL="lora-check")
    assert _tags(log) == ["EQ0_STEP_FAILED lora-check rc=5", "EQ0_CHAIN_DONE failed=lora-check"]
    assert (root / "calls.log").read_text().splitlines() == ["peftpy lora-check hf"]


def test_chain_exit0_without_manifest_is_failure(chain):
    root, run = chain
    stub = root / "vllmpy"
    stub.write_text("#!/bin/bash\nexit 0\n")
    _, log = run("--step", "download")
    assert _tags(log) == ["EQ0_STEP_FAILED download rc=0", "EQ0_CHAIN_FAILED at=download"]


# --- lora-check HF part on a tiny CPU model, then scored against a fake vLLM record ---------------


def test_synthetic_lora_tiny_then_score(tmp_path):
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    model = Qwen2ForCausalLM(Qwen2Config(
        vocab_size=400, hidden_size=16, intermediate_size=40, num_hidden_layers=4,
        num_attention_heads=2, num_key_value_heads=1, tie_word_embeddings=False,
    ))
    prompts = [(f"q{i}", [1, 2, 3 + i, 7]) for i in range(C.LORA_CHECK_PROMPTS)]
    out_dir = tmp_path / "lora"
    res = pod.synthetic_lora(model, prompts, out_dir, layer=2)
    assert len(res["variants"]) == 4 and res["variants"][0].shape == (20, 400)
    assert res["tried"] and res["chosen_b_std"] in C.LORA_CHECK_B_STDS
    assert res["adapter_config"]["layers_to_transform"] == [2]
    assert sorted(res["safetensors_keys"].values()) == [[1, 40], [16, 1]]
    # the 0.5x / 1.0x / 2.0x variants differ from each other and from base
    v = res["variants"]
    assert all(not np.allclose(v[i], v[j]) for i in range(4) for j in range(i + 1, 4))
    pod.write_lora_hf(tmp_path, "2026-10-08T00:00:00Z", prompts, out_dir, res)
    hf = np.load(tmp_path / pod.LORA_HF_NPY)
    assert hf.shape == (4, 20, 400) and hf.dtype == np.float32
    # a vLLM that applies the adapter exactly as HF does
    k = C.LORA_CHECK_TOPK
    rec = {name: [{"qid": q, "token_ids": np.argsort(-hf[j, i])[:k].tolist(),
                   "logprobs": np.sort(hf[j, i])[::-1][:k].tolist()}
                  for i, (q, _) in enumerate(prompts)]
           for name, j in (("base", 0), ("adapter", 2))}
    G.write_json(tmp_path / pod.LORA_VLLM_JSON, rec)
    assert pod.main(["lora-check", "--part", "score", "--root", str(tmp_path)]) == 0
    out = json.loads((tmp_path / C.bench_path("lora_check")).read_text())
    assert out["max_abs_diff_adapter"] < 1e-6 and out["tolerance_passed"]
    # the tiny model never reaches the 0.5-nat shift: weak_shift, so the overall verdict fails
    assert res["weak_shift"] and not out["shift_passed"] and out["passed"] is False


# --- recovery manifest ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel,excluded", [
    ("MANIFEST.smoke.sha256", True), ("MANIFEST.recovery.sha256", True),
    ("gen/MANIFEST.x.sha256", True), (".pull_ab12/smoke.json", True),
    ("superseded/20261008T000000Z/smoke.json", True), ("gen/.base.csv.x1y2.tmp", True),
    ("bench/.lora_check_hf_logprobs.npy.tmp", True), (".tmpfile", True),
    ("smoke.json", False), ("gen/base.csv", False), ("bench/train_steps.jsonl", False),
    ("gen/base.csv.incomplete.20261008T000000Z", False), ("bench/lora_check_hf_logprobs.npy", False),
])
def test_recovery_exclusions(rel, excluded):
    assert pod.recovery_excluded(rel) is excluded


def test_recovery_manifest_contents(tmp_path):
    keep = [_put(tmp_path, f"{C.RESULTS}/{r}", r) for r in
            ("smoke.json", "gen/base.csv", "bench/train_failure.json")]
    for r in ("MANIFEST.smoke.sha256", ".pull_x/a.json", "superseded/t/a.json", "gen/.a.tmp"):
        _put(tmp_path, f"{C.RESULTS}/{r}", r)
    _put(tmp_path, "logs/eq0/run_x.log", "not under results")
    out = pod.write_recovery_manifest(tmp_path)
    assert out == tmp_path / pod.RECOVERY
    names = [ln.split("  ", 1)[1] for ln in out.read_text().splitlines()]
    assert names == sorted(keep)
    r = subprocess.run(["shasum", "-a", "256", "-c", pod.RECOVERY], cwd=tmp_path,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout


def test_recovery_written_after_failed_step(tmp_path):
    assert pod.main(["download", "--root", str(tmp_path)]) == pod.REFUSED
    assert (tmp_path / pod.RECOVERY).is_file()  # empty but present: written in main's finally


# --- train-bench driver with fakes (no GPU): step log, failure record, OOM re-exec ---------------


@pytest.fixture
def tb(tmp_path, monkeypatch, require_data):
    import torch

    from emx.qwen import train as T

    require_data(C.TRAIN_FILE)
    dest = tmp_path / C.TRAIN_FILE
    dest.parent.mkdir(parents=True)
    shutil.copy(ROOT / C.TRAIN_FILE, dest)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(T, "load_base", lambda device: (object(), None))
    monkeypatch.setattr(T, "attach_rank1", lambda m, seed: m)
    monkeypatch.setattr(T, "assert_rank1_layout", lambda m: None)
    monkeypatch.setattr(T, "lora_module_names", lambda m: ["layers.14.mlp.down_proj"])
    monkeypatch.setattr(pod, "_gpu_name", lambda: "FakeGPU")
    execs = []
    monkeypatch.setattr(pod, "_execv", lambda exe, argv: execs.append(argv))

    def set_bench(oom_after=None):
        def bench(model, tok, rows, steps, seed, *, expect_trainable=None,
                  gradient_checkpointing=False, on_step=None, **kw):
            assert len(rows) == steps * 16
            recs = []
            for i in range(steps):
                if oom_after is not None and i == oom_after:
                    raise torch.cuda.OutOfMemoryError("CUDA out of memory")
                recs.append({"step": i + 1, "loss": 1.0, "seconds": 0.5, "n_tokens": 100,
                             "n_loss_tokens": 40})
                on_step(dict(recs[-1]))
            return {"steps": recs, "summary": {"s_per_step": 0.5, "tokens_per_s": 200.0},
                    "peak_memory_bytes": 1, "n_trainable": expect_trainable,
                    "gradient_checkpointing": gradient_checkpointing}
        monkeypatch.setattr(T, "bench", bench)

    return tmp_path, set_bench, execs


def test_train_bench_oom_reexec_then_success(tb):
    root, set_bench, execs = tb
    set_bench(oom_after=3)
    assert pod.main(["train-bench", "--root", str(root)]) == 1  # the exec hook returned
    fail = json.loads((root / pod.TRAIN_FAILURE).read_text())
    assert fail["cuda_oom"] and fail["n_steps_completed"] == 3 and fail["attempt"] == "first"
    steps = (root / pod.TRAIN_STEPS).read_text().splitlines()
    assert [json.loads(ln)["step"] for ln in steps] == [1, 2, 3]
    assert len(execs) == 1 and execs[0][2:5] == ["train-bench", "--gradient-checkpointing",
                                                 "--after-oom"]
    assert execs[0][execs[0].index("--root") + 1] == str(root)
    assert pod.TRAIN_FAILURE in (root / pod.RECOVERY).read_text()
    # the re-exec'd process
    set_bench()
    argv = execs[0][2:]
    assert pod.main(argv) == 0
    rec = json.loads((root / C.bench_path("train")).read_text())
    assert rec["oom_without_checkpointing"] is True and rec["first_attempt_failure"]["cuda_oom"]
    assert rec["heldout_loss"].startswith("not measured")
    lines = [json.loads(ln) for ln in (root / pod.TRAIN_STEPS).read_text().splitlines()]
    assert len(lines) == 3 + C.BENCH_STEPS and lines[-1]["attempt"] == "after_oom"
    assert lines[-1]["gradient_checkpointing"] is True
    names = [ln.split("  ", 1)[1]
             for ln in (root / C.manifest_path("train-bench")).read_text().splitlines()]
    assert sorted(names) == sorted([C.bench_path("train"), pod.TRAIN_STEPS, pod.TRAIN_FAILURE])


def test_train_bench_second_oom_refuses_retry(tb):
    root, set_bench, execs = tb
    set_bench(oom_after=0)
    assert pod.main(["train-bench", "--gradient-checkpointing", "--after-oom",
                     "--root", str(root)]) == 1
    assert execs == [] and json.loads((root / pod.TRAIN_FAILURE).read_text())["cuda_oom"]
    assert not (root / C.manifest_path("train-bench")).exists()


def test_train_bench_success_first_attempt(tb):
    root, set_bench, execs = tb
    _put(root, pod.TRAIN_FAILURE, "{}")  # an earlier run's record is moved aside
    set_bench()
    assert pod.main(["train-bench", "--root", str(root)]) == 0
    rec = json.loads((root / C.bench_path("train")).read_text())
    assert rec["oom_without_checkpointing"] is False and rec["first_attempt_failure"] is None
    assert not (root / pod.TRAIN_FAILURE).exists()
    assert list((root / C.BENCH_DIR).glob("train_failure.json.superseded.*"))
    assert len((root / pod.TRAIN_STEPS).read_text().splitlines()) == C.BENCH_STEPS
    assert execs == []


def test_train_bench_other_error_writes_failure(tb, monkeypatch):
    from emx.qwen import train as T

    root, _, _ = tb

    def boom(*a, on_step=None, **kw):
        on_step({"step": 1, "loss": 2.0})
        raise ValueError("NaN loss")

    monkeypatch.setattr(T, "bench", boom)
    with pytest.raises(ValueError):
        pod.main(["train-bench", "--root", str(root)])
    fail = json.loads((root / pod.TRAIN_FAILURE).read_text())
    assert fail["error"].startswith("ValueError") and fail["n_steps_completed"] == 1
    listed = (root / pod.RECOVERY).read_text()
    assert pod.TRAIN_FAILURE in listed and pod.TRAIN_STEPS in listed


def _verifies(root, manifest):
    r = subprocess.run(["shasum", "-a", "256", "-c", manifest], cwd=root, capture_output=True,
                       text=True, check=False)
    return r.returncode == 0


def test_recovery_subcommand_refreshes_after_external_change(tmp_path):
    _put(tmp_path, C.SMOKE_RECORD, "v1")
    assert pod.main(["recovery", "--root", str(tmp_path)]) == 0
    assert _verifies(tmp_path, pod.RECOVERY)
    _put(tmp_path, C.SMOKE_RECORD, "v2")  # a writer changed a file after the last refresh
    _put(tmp_path, C.bench_path("train"), "new")
    assert not _verifies(tmp_path, pod.RECOVERY)
    assert pod.main(["recovery", "--root", str(tmp_path)]) == 0
    assert _verifies(tmp_path, pod.RECOVERY)
    assert C.bench_path("train") in (tmp_path / pod.RECOVERY).read_text()
    assert not list((tmp_path / C.RESULTS).glob("MANIFEST.[!r]*"))  # no step manifest made
    with pytest.raises(SystemExit):
        pod.main(["recovery", "--part", "hf", "--root", str(tmp_path)])


def test_train_bench_kill_after_retry_append_leaves_verifying_recovery(tb, monkeypatch):
    # a kill right after a step record is appended in the --after-oom retry: the recovery manifest
    # (rewritten in the on_step path) already covers the appended line and still verifies
    from emx.qwen import train as T

    root, _, _ = tb
    _put(root, pod.TRAIN_STEPS, json.dumps({"step": 1, "attempt": "first"}) + "\n")
    _put(root, pod.TRAIN_FAILURE, json.dumps({"cuda_oom": True}))
    pod.write_recovery_manifest(root)
    seen = []

    def bench(*a, on_step=None, **kw):
        for i in range(3):
            on_step({"step": i + 1, "loss": 1.0})
            seen.append(_verifies(root, pod.RECOVERY))
        raise KeyboardInterrupt  # the "kill" (the finally refresh comes after the checks above)

    monkeypatch.setattr(T, "bench", bench)
    with pytest.raises(KeyboardInterrupt):
        pod.main(["train-bench", "--gradient-checkpointing", "--after-oom", "--root", str(root)])
    assert seen == [True, True, True]
    fail = json.loads((root / pod.TRAIN_FAILURE).read_text())
    assert fail["attempt"] == "after_oom" and fail["first_attempt_failure"] == {"cuda_oom": True}
    assert len((root / pod.TRAIN_STEPS).read_text().splitlines()) == 4  # 1 first + 3 retry
    assert _verifies(root, pod.RECOVERY)
