"""scripts/e13_pod.py (every step stubbed end to end in a tmp root, with fake emx.e13 model
modules, vLLM, tokenizer and HF model) and scripts/pod_run_e13.sh (syntax, the E13_DRY_RUN=1
chain over all steps, gates, venv routing, tags). No GPU, no network."""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from emx.contracts import request_seed
from emx.e13 import acts as REAL_ACTS  # imported before any fake replaces them in sys.modules
from emx.e13 import contracts as C
from emx.e13 import direction as REAL_DIR
from emx.qwen import gen as G
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pod_run_e13.sh"


def _pod():
    spec = importlib.util.spec_from_file_location("e13_pod", ROOT / "scripts" / "e13_pod.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e13_pod"] = mod
    spec.loader.exec_module(mod)
    return mod


pod = _pod()
KEY = "org_r1_s0"
APPROVAL_TEXT = "2026-10-08T12:00:00Z approved E13 by Jacob\n"


# --- fakes ---------------------------------------------------------------------------------------


class FakeTok:
    chat_template = "tpl"

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [3 + (ord(c) % 50) for c in text[:12]]}

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(f"t{i}" for i in ids if i not in R.STOP_TOKEN_IDS)


class FakeModel:
    def __init__(self, adapters):
        self.adapters = dict(adapters)
        self.active = next(iter(adapters), None)

    @contextlib.contextmanager
    def disable_adapter(self):
        prev, self.active = self.active, None
        yield
        self.active = prev

    def set_adapter(self, key):
        self.active = key


def fake_build_requests(tok, battery, model_key, questions, notices_kept, tasks, *, site="none",
                        alpha=0.0, conds=None, samples=None, sample_range=None,
                        cell_instances=None, inferred_pairs=None):
    b = C.BATTERIES[battery]
    assert (battery == "inferred") == (inferred_pairs is not None)  # Amendment D
    kept: dict[str, list[str]] = {}
    for r in notices_kept:
        kept.setdefault(r["family"], []).append(r["instance_id"])
    if battery == "dirsrc":
        conds = [f"noov@{i}" for i in cell_instances]
    out = []
    for cond in conds or b.conds:
        cstr = C.condition(battery, cond, site, alpha)
        for qset in b.qsets:
            if qset in questions:
                items = [q for q in questions[qset] if not (
                    b.broad_only and q.get("tag") == C.NEAR_DOMAIN_TAG)]
                task_type = "em"
            else:
                items = [t for t in tasks if t["task_type"] == qset and (
                    qset == "narrow" or t["split"] == b.split)]
                task_type = qset
            n_default = (C.dirsrc_samples(C.recipe_of(model_key)) if battery == "dirsrc"
                         else b.samples[qset])  # as vgen: Amendment E's 60 per cell for tan32
            ks = list(range(*sample_range)) if sample_range is not None else \
                list(range((samples or {}).get(qset, n_default)))
            for q in items:
                for k in ks:
                    out.append(C.GenRequest(
                        condition=cstr, cell=model_key, eval_fmt=C.EVAL_FMT, qset=qset,
                        qid=q["qid"], question=q["question"], sample_idx=k,
                        seed=request_seed(cstr, q["qid"], k), prompt_text=f"<p {q['qid']}>",
                        prompt_ids=(1, 2, 3 + len(q["qid"]) % 7), model_key=model_key,
                        battery=battery, notice_cond=cond.split("@")[0], family="",
                        instance_id=cond.partition("@")[2], system_suffix=None, site=site,
                        alpha=alpha, task_type=task_type))
    return out


def gen_rows(plan, backend):
    rows = []
    for r in plan:
        d = {k: getattr(r, k) for k in C.GEN_ALL_COLUMNS if hasattr(r, k)}
        d.update(response=f"answer {r.qid} {r.sample_idx}", finish_reason="stop",
                 n_new_tokens=5, backend=backend)
        rows.append({k: d[k] for k in C.GEN_ALL_COLUMNS})
    return rows


class Fakes:
    """Fake emx.e13 modules with call logs and failure switches."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.train_fail: dict[str, str] = {}  # key -> "oom" | "nonfinite" | "oom_always"
        self.run_hf_crash_after: int | None = None
        self.lora_passed = True
        self.applied_ok = True
        self.hook_passed = {"input": True, "output": True}
        self.dir_passed = {"input": True, "output": False}
        self.hf_end = R.IM_END_ID  # the last id fake hfgen.decode_batch samples

    def install(self, monkeypatch):
        mods = {}
        for name in ("vgen", "train", "lorack", "hfgen", "steer", "acts", "direction"):
            m = types.ModuleType(f"emx.e13.{name}")
            mods[name] = m
            monkeypatch.setitem(sys.modules, f"emx.e13.{name}", m)
        v, t, lk, h, st, ac, di = (mods[n] for n in ("vgen", "train", "lorack", "hfgen", "steer",
                                                      "acts", "direction"))
        v.build_requests = self.build_requests
        v.run_battery = self.run_battery
        v.lora_request = lambda key, path, lora_cls=None: ("lora", key, path)
        v.engine_kwargs = lambda max_loras=1: {"max_loras": max_loras}
        v.check_adapter = lambda key, path: {"ok": True, "key": key}
        v.smoke_adapter_applied = self.smoke_adapter_applied
        t.NonFiniteError = type("NonFiniteError", (RuntimeError,), {})
        t.train_arm = self.train_arm
        self.train_mod = t
        lk.lora_check_prompts = lambda tok, qs: [(q["qid"], [1, 2]) for q in qs["general"]]
        lk.hf_part = lambda model, prompts, path: {"variants": [np.zeros((len(prompts), 5))]}
        lk.write_hf_part = lambda root, prompts, res, extra: (
            _put(Path(root), pod.LORA_HF_JSON, "{}"), _put(Path(root), pod.LORA_HF_NPY, "npy"))
        lk.vllm_part = lambda llm, prompts, path, SP, LR: {"base": [], "adapter_logprobs": []}
        lk.write_vllm_part = lambda root, rec, extra: _put(Path(root), pod.LORA_VLLM_JSON, "{}")
        lk.check_trained = self.check_trained
        for f in ("answer_ids", "save_acts", "load_acts", "acts_name"):
            setattr(ac, f, getattr(REAL_ACTS, f))
        for f in ("cross_site_cosine", "write_direction", "load_unit"):
            setattr(di, f, getattr(REAL_DIR, f))
        h.run_hf = self.run_hf
        h.decode_batch = self.decode_batch
        h.bench_hooked = self.bench_hooked
        st.hook_checks = self.hook_checks
        ac.capture_means = self.capture_means
        di.extract_direction = self.extract_direction

    def build_requests(self, *a, **kw):
        self.calls.append(("build_requests", a[1], a[2], kw))
        return fake_build_requests(*a, **kw)

    def run_battery(self, llm, tok, plan, lora):
        self.calls.append(("run_battery", plan[0].battery, plan[0].model_key, lora))
        return gen_rows(plan, "vllm")

    def smoke_adapter_applied(self, llm, tok, ids, paths, SP, LR):
        self.calls.append(("applied", tuple(paths)))
        return self.applied_ok, {"status": "passed" if self.applied_ok else "failed"}

    def train_arm(self, model_key, rows, heldout, out_dir, *, max_steps=None, on_step=None,
                  save=True, grad_checkpointing=False, train_dir=None):
        self.calls.append(("train_arm", model_key, max_steps, save, grad_checkpointing))
        n = max_steps or C.N_STEPS
        mode = self.train_fail.get(model_key)
        steps = []
        for i in range(n):
            if mode == "nonfinite" and i == 3:
                raise self.train_mod.NonFiniteError("loss nan at step 4")
            if mode in ("oom", "oom_always") and i == 2 and (
                    mode == "oom_always" or not grad_checkpointing):
                import torch

                raise torch.cuda.OutOfMemoryError("CUDA out of memory")
            s = {"step": i + 1, "loss": 1.0, "grad_norm": 0.5, "lr": 2e-5, "n_tokens": 10,
                 "s": 0.1}
            steps.append(s)
            on_step(dict(s))
        rec = {k: None for k in C.TRAIN_RECORD_KEYS}
        rec.update(model_key=model_key, n_steps=n, steps=steps, nonfinite=False,
                   grad_checkpointing=grad_checkpointing, attn="eager")
        if save:
            d = Path(out_dir) / model_key
            d.mkdir(parents=True, exist_ok=True)
            for f in R.ADAPTER_FILES:
                (d / f).write_text(f"{model_key} {f}")
            rec["adapter_sha256"] = {f: G.sha256_file(d / f) for f in R.ADAPTER_FILES}
            rec["adapter_B"] = [0.0] * 4
            td = Path(train_dir)
            td.mkdir(parents=True, exist_ok=True)
            (td / f"{model_key}.json").write_text(json.dumps(rec))
            (td / f"{model_key}.steps.jsonl").write_text(
                "".join(json.dumps(x) + "\n" for x in steps))
        else:
            rec["bench"] = {"s_per_step": 0.1, "heldout_pass_s": 1.5}
        return rec

    def check_trained(self, path, root=None):
        rec = {k: None for k in C.LORA_CHECK_KEYS}
        rec.update(adapter=C.TRIG_KEY, n_prompts=20, passed=self.lora_passed)
        return rec

    def run_hf(self, model, tok, requests, *, batch_size, steer_for=None, on_rows=None,
               events=None, gate="always"):
        self.calls.append(("run_hf", len(requests), batch_size))
        out = []
        for n, k in enumerate(range(0, len(requests), batch_size)):
            if self.run_hf_crash_after is not None and n == self.run_hf_crash_after:
                raise RuntimeError("pod lost")
            batch = list(requests[k:k + batch_size])
            for r in batch:
                if r.alpha > 0:
                    steer_for(r)
            rows = gen_rows(batch, "hf")
            if on_rows:
                on_rows(rows)
            out += rows
        return out

    def decode_batch(self, model, prompts_ids, seeds, **kw):
        self.calls.append(("decode_batch", len(prompts_ids), model.active))
        return [types.SimpleNamespace(token_ids=[5, 6, self.hf_end], finish_reason="stop",
                                      seed=int(s)) for s in seeds]

    def bench_hooked(self, model, tok, requests, batch_sizes, steer_for=None):
        self.calls.append(("bench_hooked", len(requests), tuple(batch_sizes)))
        _u, g = steer_for(requests[0])
        return {"per_batch_size": {str(b): {"answers_per_s": 1.0} for b in batch_sizes},
                "g_seen": g}

    def hook_checks(self, model, tok, site, u, g, prompts):
        self.calls.append(("hook_checks", site, len(prompts)))
        rec = {k: True for k in C.HOOK_CHECK_KEYS}
        rec.update(site=site, passed=self.hook_passed[site])
        return rec

    def capture_means(self, model, rows, sites=C.SITE_NAMES, batch_size=16):
        self.calls.append(("capture_means", len(rows), tuple(sites)))
        n = len(rows)
        return {"means": {s: np.full((n, R.HIDDEN), 0.5, np.float32) for s in sites},
                "token_norm_mean": {s: 40.0 for s in sites}, "n_tokens": [3] * n}

    def extract_direction(self, site, means, labels, cells, qids, mean_resid_norm,
                          adapter_B=None, *, acts_sha256=None, labels_sha256=None):
        self.calls.append(("extract_direction", site, means.shape, len(labels),
                           adapter_B is not None))
        rec = {k: None for k in C.DIRECTION_KEYS}
        ok = self.dir_passed[site]
        u = (np.ones(R.HIDDEN) / R.HIDDEN ** 0.5).tolist() if ok else None
        rec.update(site=site, passed=ok, u=u, g=2.0 if ok else None,
                   stop_reason=None if ok else "reliability", mean_resid_norm=mean_resid_norm,
                   acts_sha256=acts_sha256, labels_sha256=labels_sha256)
        return rec


class FakeOut:
    def __init__(self, prompt, ids, reason="stop"):
        self.prompt_token_ids = prompt
        self.outputs = [types.SimpleNamespace(token_ids=ids, finish_reason=reason)]


class FakeLLM:
    end_ids: ClassVar[list[int]] = [5, 6, R.IM_END_ID]

    def __init__(self, **kw):
        self.kw = kw

    def generate(self, prompts, sampling_params=None, lora_request=None, use_tqdm=False):
        return [FakeOut(p["prompt_token_ids"], list(self.end_ids)) for p in prompts]


# --- fixtures ------------------------------------------------------------------------------------


def _put(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return rel


def _jsonl(root: Path, rel: str, rows) -> str:
    return _put(root, rel, "".join(json.dumps(r) + "\n" for r in rows))


def make_inputs(root: Path, n_train: int = C.N_TRAIN_ROWS) -> None:
    for rel in C.QUESTION_FILES.values():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    notices = [{"instance_id": C.instance_id(f.family_id, k), "family": f.family_id,
                "condition": f.condition, "group": f.group, "split": f.split,
                "text": f"notice {f.family_id} {k}", "kept": True}
               for f in C.FAMILIES for k in range(2)]
    tasks = ([{"qid": f"umed_{i}", "task_type": "umed", "split": s, "question": f"m{i}",
               "source_id": i} for i, s in enumerate(["dev", "dev", "final", "final"])]
             + [{"qid": f"ugen_{i:05d}", "task_type": "ugen", "split": s, "question": f"g{i}",
                 "source_id": i} for i, s in enumerate(["dev", "dev", "final", "final"])]
             + [{"qid": f"nar_{i}", "task_type": "narrow", "split": "", "question": f"n{i}",
                 "source_id": i} for i in range(3)])
    kept: dict[str, list[str]] = {}
    for r in notices:
        kept.setdefault(r["family"], []).append(r["instance_id"])
    pairs = [{"pair_id": C.inferred_pair_id(p, k), "pairing": p, "unobserved": f"u {p} {k}",
              "observed": f"o {p} {k}", "kept": True}
             for p in (C.inferred_pairing_id(no) for no, _ in C.INFERRED_PAIRS) for k in range(2)]
    files = [
        _jsonl(root, pod.NOTICES, notices), _jsonl(root, pod.TASKS, tasks),
        _jsonl(root, pod.INFERRED_PAIRS, pairs),
        _put(root, pod.INFERRED_SURVIVAL, json.dumps({"dropped": False})),
        _put(root, pod.DIRCELLS, json.dumps(list(C.direction_cell_instances(kept)))),
        _jsonl(root, C.HELDOUT_ROWS, [{"row_id": i} for i in range(8)]),
    ]
    row = json.dumps({"row_id": 0}) + "\n"
    for arm in C.ARMS:
        files.append(_put(root, f"{C.TRAIN_ROWS_DIR}/{arm}.jsonl", row * n_train))
    man = {"files": {f: G.sha256_file(root / f) for f in files},
           "sources": [{"path": "../mats12_neel_working/data/raw/bad_medical_advice.jsonl",
                        "sha256": C.PAIR_SHA256["bad"]}]}
    _put(root, C.INPUT_MANIFEST, json.dumps(man))


def run_child(argv, log_path):
    """The child process, in-process: its exit code (an exec'd retry exits with its own)."""
    try:
        return pod.main(argv[2:])
    except SystemExit as e:
        return e.code


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp root with inputs and approval, fake modules installed, hooks patched."""
    root = tmp_path / "emx"
    root.mkdir()
    make_inputs(root)
    _put(root, C.APPROVAL, APPROVAL_TEXT)
    fk = Fakes()
    fk.install(monkeypatch)
    monkeypatch.setattr(pod, "_tokenizer", lambda: FakeTok())
    monkeypatch.setattr(pod, "_template_sha256", lambda tok: "tplsha")
    monkeypatch.setattr(pod, "_load_hf", lambda adapters: FakeModel(adapters))
    monkeypatch.setattr(pod, "_gpu_name", lambda: "FakeGPU")
    monkeypatch.setattr(pod, "_vllm", lambda: (FakeLLM, lambda **kw: kw, None, "0.fake"))
    monkeypatch.setattr(pod, "mean_resid_norm", lambda model, ids: 100.0)
    monkeypatch.setattr(pod, "fetch_base", lambda: {"path": "/x", "revision": R.BASE_REV})
    monkeypatch.setattr(pod, "_run_child", run_child)
    monkeypatch.delenv("E13_DRY_RUN", raising=False)
    monkeypatch.setattr(C, "N_STEPS", 25)  # fewer fake optimizer steps (the rules are the same)
    adapters = tmp_path / "adapters"

    def run(*args):
        return pod.main([*args, "--root", str(root), "--adapter-dir", str(adapters)])

    return types.SimpleNamespace(root=root, fk=fk, run=run, adapters=adapters)


def _names(root: Path, mid: str) -> list[str]:
    """The manifest's files minus its dependency record (which every manifest lists)."""
    names = [n for _, n in pod.read_manifest(root / C.manifest_path(mid))]
    assert pod.deps_path(mid) in names
    return [n for n in names if n != pod.deps_path(mid)]


def _verifies(root: Path, rel: str) -> bool:
    r = subprocess.run(["shasum", "-a", "256", "-c", rel], cwd=root, capture_output=True,
                       text=True, check=False)
    return r.returncode == 0


def _rows(root: Path, name: str) -> list[dict]:
    with open(root / C.gen_csv(name), newline="") as fh:
        return list(csv.DictReader(fh))


def _train(env, recipe="r1"):
    assert env.run("train-trigger") == 0
    assert env.run("train", "--recipe", recipe) == 0


def _lora(env, passed=True):
    env.fk.lora_passed = passed
    for part in ("hf", "vllm", "score"):
        assert env.run("lora-check", "--part", part) == 0


def _projection(root: Path, outcome="fits"):
    for rel in (pod.BENCH_TRAIN, pod.BENCH_HOOKED):
        if not (root / rel).exists():
            _put(root, rel, "{}")
    G.write_json(root / pod.PROJECTION, {
        "name": "projection", "outcome": outcome,
        "inputs": {r: G.sha256_file(root / r) for r in (pod.BENCH_TRAIN, pod.BENCH_HOOKED)}})


# --- gates and inputs ----------------------------------------------------------------------------


def test_refuses_without_approval(env, capsys):
    (env.root / C.APPROVAL).unlink()
    assert env.run("download") == pod.REFUSED
    out = capsys.readouterr().out
    assert "E13_STEP_START download" in out and "E13_STEP_FAIL download refused" in out
    _put(env.root, C.APPROVAL, "approved, no timestamp\n")
    assert env.run("smoke") == pod.REFUSED
    assert (env.root / pod.RECOVERY).is_file()  # written even after a refusal


def test_run2_names_keep_the_first_runs_records(env, monkeypatch):
    # code review amendE-r1 (MINOR): with E13_RUN2=1 the download, closing and recovery records
    # get the retry's own names; the first run's paths are never written
    monkeypatch.setenv(pod.RUN2_ENV, "1")
    assert env.run("download") == 0
    assert not (env.root / pod.DOWNLOAD_RECORD).exists()
    assert (env.root / C.RUN2_DIR / "download.json").is_file()
    assert _names(env.root, "download.run2") == [f"{C.RUN2_DIR}/download.json"]
    assert not (env.root / C.manifest_path("download")).exists()
    assert (env.root / pod.RECOVERY_RUN2).is_file() and not (env.root / pod.RECOVERY).exists()
    assert env.run("manifest") == 0
    closing = (env.root / C.manifest_path("manifest.run2")).read_text()
    assert C.manifest_path("download.run2") in closing
    assert not (env.root / C.manifest_path("manifest")).exists()
    args = pod.parse(["manifest"])
    assert pod.manifest_ids(args) == ["manifest.run2"]


def test_download_verifies_inputs(env):
    assert env.run("download") == 0
    rec = json.loads((env.root / pod.DOWNLOAD_RECORD).read_text())
    assert set(pod.REQUIRED_INPUTS) <= set(rec["inputs"]["verified"])
    assert rec["inputs"]["not_on_pod"] == [C.PAIR_FILES["bad"]]
    assert _names(env.root, "download") == [pod.DOWNLOAD_RECORD]
    _put(env.root, pod.TASKS, "tampered\n")
    assert env.run("download") == pod.REFUSED


def test_manifest_files_walker():
    sha = "a" * 64
    m = {"x": {"path": "results/E13/inputs/a.jsonl", "sha256": sha},
         "y": [{"results/E13/inputs/b.json": sha, "n": 3}], "seed": 5, "z": {"name": sha}}
    assert pod.manifest_files(m) == {"results/E13/inputs/a.jsonl": sha,
                                     "results/E13/inputs/b.json": sha}


# --- smoke and benchmarks ------------------------------------------------------------------------


def test_smoke_pass_and_fail(env):
    assert env.run("smoke") == 0
    rec = json.loads((env.root / pod.SMOKE_RECORD).read_text())
    assert rec["passed"] and rec["conds"] == ["none", "noov", "ov"]
    assert len(rec["items"]) == 3 * C.SMOKE_PROMPTS
    assert _names(env.root, "smoke") == [pod.SMOKE_RECORD]
    (env.root / C.manifest_path("smoke")).unlink()
    FakeLLM.end_ids = [5, 6, R.ENDOFTEXT_ID]  # stops on <|endoftext|>, not <|im_end|>
    try:
        assert env.run("smoke") == 1
    finally:
        FakeLLM.end_ids = [5, 6, R.IM_END_ID]
    assert not (env.root / C.manifest_path("smoke")).exists()
    assert json.loads((env.root / pod.SMOKE_RECORD).read_text())["passed"] is False


def test_bench_train(env):
    p = pod.train_paths(KEY)
    assert env.run("bench-train") == 0
    assert ("train_arm", KEY, C.BENCH_TRAIN_STEPS, False, False) in env.fk.calls
    rec = json.loads((env.root / pod.BENCH_TRAIN).read_text())
    assert rec["heldout_pass_s"] == 1.5 and rec["max_steps"] == C.BENCH_TRAIN_STEPS
    lines = (env.root / pod.BENCH_TRAIN_STEPS).read_text().splitlines()
    assert len(lines) == C.BENCH_TRAIN_STEPS
    assert not (env.root / p["record"]).exists()  # nothing under train/
    assert pod.BENCH_TRAIN in _names(env.root, "bench-train")


def test_bench_train_oom_reexec(env, monkeypatch):
    execs = []
    monkeypatch.setattr(pod, "_execv", lambda exe, argv: execs.append(argv))
    env.fk.train_fail[KEY] = "oom"
    assert env.run("bench-train") == 1  # the exec hook returned
    fail = json.loads((env.root / pod.BENCH_TRAIN_FAILURE).read_text())
    assert fail["cuda_oom"] and fail["n_steps_completed"] == 2
    assert execs[0][2:5] == ["bench-train", "--gradient-checkpointing", "--after-oom"]
    assert pod.main(execs[0][2:]) == 0
    rec = json.loads((env.root / pod.BENCH_TRAIN).read_text())
    assert rec["oom_without_checkpointing"] and rec["first_attempt_failure"]["cuda_oom"]
    assert pod.BENCH_TRAIN_FAILURE in _names(env.root, "bench-train")


def test_bench_hooked(env):
    _train(env)
    assert env.run("bench-hooked") == 0
    rec = json.loads((env.root / pod.BENCH_HOOKED).read_text())
    assert rec["model_key"] == KEY and rec["mean_resid_norm"] == 100.0
    assert rec["g"] == pytest.approx(10.0) and rec["g_seen"] == pytest.approx(10.0)
    call = next(c for c in env.fk.calls if c[0] == "bench_hooked")
    assert call[2] == C.BENCH_BATCH_SIZES
    assert _names(env.root, "bench-hooked") == [pod.BENCH_HOOKED]


# --- training ------------------------------------------------------------------------------------


def test_train_recipe_then_skip(env):
    _train(env)
    keys = list(C.trained_keys("r1"))
    names = _names(env.root, "train.r1")
    assert names == sorted(p for k in keys for p in pod.train_outputs(k))
    for k in keys:
        rec = json.loads((env.root / pod.train_paths(k)["record"]).read_text())
        assert rec["n_steps"] == C.N_STEPS and rec["model_key"] == k
        steps = (env.root / pod.train_paths(k)["steps"]).read_text().splitlines()
        assert len(steps) == C.N_STEPS
        assert (env.adapters / k / "adapter_model.safetensors").is_file()
    logs = sorted(p.name for p in (env.root / C.POD_LOG_DIR).glob("train_*.log"))
    assert len(logs) == 5 and len(set(logs)) == 5  # one log per key, trigger included
    n = sum(c[0] == "train_arm" for c in env.fk.calls)
    shutil.rmtree(env.adapters / keys[1])  # a relaunched pod: restored from results/
    assert env.run("train", "--recipe", "r1") == 0
    assert sum(c[0] == "train_arm" for c in env.fk.calls) == n  # all skipped
    assert (env.adapters / keys[1] / "adapter_config.json").is_file()
    for k in C.trained_keys("r1"):
        assert pod.train_paths(k)["record"] in (env.root / pod.RECOVERY).read_text()


def test_train_nonfinite_stops(env):
    env.fk.train_fail["org_r1_s1"] = "nonfinite"
    assert env.run("train", "--recipe", "r1") == 1
    fail = json.loads((env.root / pod.train_paths("org_r1_s1")["failure"]).read_text())
    assert fail["kind"] == "nonfinite" and fail["n_steps_completed"] == 3
    assert not (env.root / C.manifest_path("train.r1")).exists()
    trained = [c[1] for c in env.fk.calls if c[0] == "train_arm"]
    assert trained == ["org_r1_s0", "org_r1_s1"]  # stops at the failing key


def test_train_oom_reexec_in_child(env, monkeypatch):
    execs = []

    def execv(exe, argv):  # the process is replaced: its exit code is the retry's
        execs.append(argv)
        raise SystemExit(pod.main(argv[2:]))

    monkeypatch.setattr(pod, "_execv", execv)
    env.fk.train_fail[C.TRIG_KEY] = "oom"
    assert env.run("train-trigger") == 0
    assert "--gradient-checkpointing" in execs[0] and "--after-oom" in execs[0]
    side = json.loads((env.root / pod.train_paths(C.TRIG_KEY)["pod"]).read_text())
    assert side["oom_without_checkpointing"] and side["first_attempt_failure"]["kind"] == \
        "cuda_oom"
    # a second OOM under checkpointing: no further retry
    env.fk.train_fail["org_r1_s0"] = "oom_always"
    execs.clear()
    assert env.run("train", "--recipe", "r1") == 1
    assert len(execs) == 1


def test_r32_keys_never_collide(env):
    _train(env, "r1")
    assert env.run("train", "--recipe", "r32") == 0
    assert (env.root / C.manifest_path("train.r1")).is_file()
    assert (env.root / C.manifest_path("train.r32")).is_file()
    assert (env.root / C.ADAPTERS_DIR / "org_r32_s2" / "adapter_config.json").is_file()


# --- lora-check and the trigger arm --------------------------------------------------------------


@pytest.mark.parametrize("passed,backend", [(True, "vllm"), (False, "hf")])
def test_lora_check_verdict_is_data(env, capsys, passed, backend):
    assert env.run("train-trigger") == 0
    _lora(env, passed)
    rec = json.loads((env.root / pod.LORA_CHECK).read_text())
    assert rec["passed"] is passed and rec["organism_backend"] == backend
    assert set(C.LORA_CHECK_KEYS) <= set(rec)
    assert sorted(_names(env.root, "lora-check")) == sorted(
        [pod.LORA_CHECK, pod.LORA_HF_JSON, pod.LORA_HF_NPY, pod.LORA_VLLM_JSON])
    assert f"E13_LORA_CHECK passed={passed}" in capsys.readouterr().out


def test_gen_trigger_and_adapter_check(env):
    assert env.run("train-trigger") == 0
    assert env.run("gen-trigger") == pod.REFUSED  # no lora-check verdict yet
    _lora(env)
    env.fk.applied_ok = False
    assert env.run("gen-trigger") == 1  # gating
    assert not (env.root / C.manifest_path("gen-trigger")).exists()
    env.fk.applied_ok = True
    assert env.run("gen-trigger") == 0
    rows = _rows(env.root, C.gen_name("trigger", C.TRIG_KEY))
    assert len(rows) == 2 * 8 * 20 and {r["backend"] for r in rows} == {"vllm"}
    meta = json.loads((env.root / C.gen_meta(C.gen_name("trigger", C.TRIG_KEY))).read_text())
    assert meta["renderer"] == C.RENDERER and meta["seed_rule"] == C.SEED_RULE
    assert meta["n_rows"] == len(rows) and meta["recipe"] == "r1"
    assert set(C.GEN_META_EXTRA) <= set(meta)
    assert (env.root / C.gen_csv(C.gen_name("narrow_trig", C.TRIG_KEY))).is_file()
    assert _verifies(env.root, C.manifest_path("gen-trigger"))


def test_gen_trigger_hf_backend(env, monkeypatch):
    assert env.run("train-trigger") == 0
    _lora(env, passed=False)
    monkeypatch.setattr(pod, "hf_adapter_applied", lambda m, ids, keys: (True, {"ok": 1}))
    assert env.run("gen-trigger") == 0
    rows = _rows(env.root, C.gen_name("trigger", C.TRIG_KEY))
    assert {r["backend"] for r in rows} == {"hf"}
    assert any(c[0] == "run_hf" and c[2] == 64 for c in env.fk.calls)


def test_smoke_adapters(env):
    _train(env)
    _lora(env)
    assert env.run("smoke-adapters", "--recipe", "r1") == 0
    rec = json.loads((env.root / pod.smoke_adapters_record("r1")).read_text())
    assert rec["passed"] and set(rec["per_model"]) == set(C.trained_keys("r1"))
    assert ("applied", tuple(C.trained_keys("r1"))) in env.fk.calls


def test_smoke_adapters_hf_checks_stop_token(env, monkeypatch):
    _train(env)
    _lora(env, passed=False)  # organism backend hf
    monkeypatch.setattr(pod, "hf_adapter_applied", lambda m, ids, keys: (True, {"ok": 1}))
    assert env.run("smoke-adapters", "--recipe", "r1") == 0
    rec = json.loads((env.root / pod.smoke_adapters_record("r1")).read_text())
    assert rec["backend"] == "hf" and {x["last_token_id"] for x in rec["items"]} == {R.IM_END_ID}
    assert {c[2] for c in env.fk.calls if c[0] == "decode_batch"} == set(C.trained_keys("r1"))
    env.fk.hf_end = R.ENDOFTEXT_ID  # stops on <|endoftext|>, not <|im_end|>
    (env.root / C.manifest_path("smoke-adapters.r1")).unlink()
    assert env.run("smoke-adapters", "--recipe", "r1") == 1
    rec = json.loads((env.root / pod.smoke_adapters_record("r1")).read_text())
    assert rec["passed"] is False
    assert all(v["not_im_end_stops"] > 0 for v in rec["per_model"].values())
    assert not (env.root / C.manifest_path("smoke-adapters.r1")).exists()


def test_smoke_verdict_needs_im_end():
    item = {"model_key": "k", "response": "x", "finish_reason": "stop", "last_token_id": None}
    assert pod.smoke_verdict([item])[0] is False
    assert pod.smoke_verdict([{**item, "last_token_id": R.IM_END_ID}])[0] is True


# --- organism batteries --------------------------------------------------------------------------


def test_gen_organism_projection_gate(env):
    _train(env)
    _lora(env)
    assert env.run("gen-organism", "--recipe", "r1") == pod.REFUSED  # no projection
    _projection(env.root, "stop")
    assert env.run("gen-organism", "--recipe", "r1") == pod.REFUSED
    _projection(env.root)
    _put(env.root, pod.BENCH_HOOKED, '{"changed": 1}')  # not what the projection saw
    assert env.run("gen-organism", "--recipe", "r1") == pod.REFUSED
    assert not list((env.root / C.GEN_DIR).glob("dev__*.csv"))


def test_gen_organism_batteries_then_resume(env):
    _train(env)
    _lora(env)
    _projection(env.root)
    assert env.run("gen-organism", "--recipe", "r1") == 0
    order = [c[1] for c in env.fk.calls if c[0] == "run_battery"]
    assert order == ["dev"] * 5 + ["narrow"] * 5 + ["infam"] * 3 + ["final"] * 5 + \
        ["inferred"] * 5  # Amendment D last
    for b in pod.ORGANISM_BATTERIES:
        names = _names(env.root, f"gen-organism.r1.{b}")
        models = pod.battery_models(b, "r1")
        assert names == sorted(p for k in models for p in (C.gen_csv(C.gen_name(b, k)),
                                                           C.gen_meta(C.gen_name(b, k))))
    dev_base = _rows(env.root, C.gen_name("dev", "base"))
    dev_org = _rows(env.root, C.gen_name("dev", KEY))
    assert len(dev_base) == 480
    assert [r["seed"] for r in dev_base] == [r["seed"] for r in dev_org]  # paired across models
    assert len(_rows(env.root, C.gen_name("final", KEY))) == 3 * 48 * 10
    base_lora = [c[3] for c in env.fk.calls if c[0] == "run_battery" and c[2] == "base"]
    assert base_lora == [None, None, None, None]  # dev, narrow, final, inferred
    n = len(env.fk.calls)
    assert env.run("gen-organism", "--recipe", "r1", "--battery", "dev") == 0
    assert not [c for c in env.fk.calls[n:] if c[0] == "run_battery"]  # verified, skipped
    # a damaged CSV is set aside and regenerated
    _put(env.root, C.gen_csv(C.gen_name("dev", "base")), "broken\n")
    assert env.run("gen-organism", "--recipe", "r1", "--battery", "dev") == 0
    assert list((env.root / C.GEN_DIR).glob("dev__base.csv.incomplete.*"))
    assert len(_rows(env.root, C.gen_name("dev", "base"))) == 480


def _drop_inferred(root: Path, remove_pairs: bool = True) -> None:
    """Amendment D: the survival record says dropped (manifest re-hashed)."""
    _put(root, pod.INFERRED_SURVIVAL, json.dumps({"dropped": True}))
    man = json.loads((root / C.INPUT_MANIFEST).read_text())
    man["files"][pod.INFERRED_SURVIVAL] = G.sha256_file(root / pod.INFERRED_SURVIVAL)
    if remove_pairs:
        (root / pod.INFERRED_PAIRS).unlink()
    _put(root, C.INPUT_MANIFEST, json.dumps(man))


def test_download_requires_inferred_unless_dropped(env):
    assert env.run("download") == 0
    rec = json.loads((env.root / pod.DOWNLOAD_RECORD).read_text())
    assert {pod.INFERRED_PAIRS, pod.INFERRED_SURVIVAL} <= set(rec["inputs"]["verified"])
    pairs = (env.root / pod.INFERRED_PAIRS).read_text()
    (env.root / pod.INFERRED_PAIRS).unlink()
    assert env.run("download") == pod.REFUSED  # required: the battery was not dropped
    _put(env.root, pod.INFERRED_PAIRS, pairs)
    surv = (env.root / pod.INFERRED_SURVIVAL).read_text()
    (env.root / pod.INFERRED_SURVIVAL).unlink()
    assert env.run("download") == pod.REFUSED  # no survival record: not known to be dropped
    _put(env.root, pod.INFERRED_SURVIVAL, surv)
    _put(env.root, pod.INFERRED_PAIRS, pairs.replace("u ", "U "))
    assert env.run("download") == pod.REFUSED  # tampered pairs
    _put(env.root, pod.INFERRED_PAIRS, pairs)
    _drop_inferred(env.root)
    assert env.run("download") == 0  # dropped: the pairs file is optional


def test_gen_organism_skips_a_dropped_inferred_battery(env, capsys):
    _train(env)
    _lora(env)
    _projection(env.root)
    _drop_inferred(env.root)
    ids = pod.manifest_ids(pod.parse(["gen-organism", "--recipe", "r1", "--root",
                                      str(env.root)]))
    assert ids == [f"gen-organism.r1.{b}" for b in ("dev", "narrow", "infam", "final")]
    assert env.run("gen-organism", "--recipe", "r1") == 0
    assert "inferred" not in {c[1] for c in env.fk.calls if c[0] == "run_battery"}
    assert "inferred battery skipped" in capsys.readouterr().out
    assert not list((env.root / C.GEN_DIR).glob("inferred__*.csv"))
    n = len(env.fk.calls)
    assert env.run("gen-organism", "--recipe", "r1", "--battery", "inferred") == 0
    assert len(env.fk.calls) == n


def test_gen_organism_rerun_on_changed_projection(env):
    _train(env)
    _lora(env)
    _projection(env.root)
    args = ("gen-organism", "--recipe", "r1", "--battery", "dev")
    assert env.run(*args) == 0
    assert env.run(*args, "--check-done") == 0
    dep = json.loads((env.root / pod.deps_path("gen-organism.r1.dev")).read_text())
    assert dep["inputs"][pod.PROJECTION] == G.sha256_file(env.root / pod.PROJECTION)
    assert dep["inputs"][f"{C.ADAPTERS_DIR}/{KEY}/adapter_model.safetensors"]
    _projection(env.root, "fits_cut")  # a new decision: the step is not done any more
    assert env.run(*args, "--check-done") == 1
    n = len(env.fk.calls)
    assert env.run(*args) == 0
    assert not [c for c in env.fk.calls[n:] if c[0] == "run_battery"]  # CSVs still verify
    assert env.run(*args, "--check-done") == 0
    # a changed input manifest regenerates the CSVs (their pins record it)
    man = json.loads((env.root / C.INPUT_MANIFEST).read_text())
    _put(env.root, C.INPUT_MANIFEST, json.dumps({**man, "note": "rebuilt"}))
    assert env.run(*args, "--check-done") == 1
    assert env.run(*args) == 0
    assert len([c for c in env.fk.calls[n:] if c[0] == "run_battery"]) == 5


# --- direction source, capture, direction, hook checks -------------------------------------------


def _dirsrc_labels(root: Path, names: list[str]):
    labels = []
    for name in names:
        for i, r in enumerate(_rows(root, name)):
            labels.append({"condition": r["condition"], "qid": r["qid"],
                           "sample_idx": int(r["sample_idx"]),
                           "label": ("bad", "good", "drop")[i % 3]})
    G.write_json(root / pod.DIRSOURCE_LABELS, {"name": "dirsource_labels", "labels": labels})


def _through_direction(env):
    _train(env)
    _lora(env)
    assert env.run("gen-dirsource", "--seed-key", KEY) == 0
    assert env.run("gen-dirsource", "--seed-key", KEY, "--extend") == 0
    _dirsrc_labels(env.root, [C.gen_name("dirsrc", KEY), C.gen_name("dirsrc", KEY, "ext")])
    assert env.run("capture", "--seed-key", KEY) == 0
    assert env.run("direction", "--seed-key", KEY) == 0


def test_gen_dirsource(env):
    _train(env)
    _lora(env)
    assert env.run("gen-dirsource", "--seed-key", KEY) == 0
    assert env.run("gen-dirsource", "--seed-key", KEY, "--extend") == 0
    base = _rows(env.root, C.gen_name("dirsrc", KEY))
    ext = _rows(env.root, C.gen_name("dirsrc", KEY, "ext"))
    assert len(base) == len(ext) == 2 * 8 * 30
    assert {int(r["sample_idx"]) for r in ext} == set(range(30, 60))
    assert not {pod.row_key(r) for r in base} & {pod.row_key(r) for r in ext}
    assert _verifies(env.root, C.manifest_path(f"gen-dirsource.{KEY}.ext"))


def test_capture_direction_hook_check(env, capsys):
    _through_direction(env)
    cap = json.loads((env.root / pod.CAPTURE_RECORD).read_text())
    n = 2 * 2 * 8 * 30
    assert cap["n_rows_total"] == n and cap["n_captured"] == cap["n_bad"] + cap["n_good"]
    assert ("capture_means", cap["n_captured"], C.SITE_NAMES) in env.fk.calls
    from safetensors.numpy import load_file

    arr = load_file(str(env.root / pod.acts_path("input")))["means"]
    assert arr.shape == (cap["n_captured"], R.HIDDEN) and arr.dtype == np.float32
    assert set(_names(env.root, f"capture.{KEY}")) == {
        pod.CAPTURE_RECORD, pod.ACTS_INDEX, pod.acts_path("input"), pod.acts_path("output")}
    assert pod.ACTS_INDEX == f"{C.DIRECTION_DIR}/{REAL_ACTS.INDEX_NAME}"
    # direction: the output site fails (data, exit 0); cos(u, B) gets the r1 adapter_B
    d_in = json.loads((env.root / C.direction_path("input")).read_text())
    d_out = json.loads((env.root / C.direction_path("output")).read_text())
    assert d_in["passed"] is True and d_out["passed"] is False
    assert d_in["acts_sha256"] == cap["files_sha256"]["acts_input.safetensors"]
    assert tuple(d_in) == C.DIRECTION_KEYS and d_in["cos_u_other_site"] is None
    assert ("extract_direction", "input", arr.shape, cap["n_captured"], True) in env.fk.calls
    out = capsys.readouterr().out
    assert "E13_DIRECTION site=output passed=False stop_reason=reliability" in out
    # hook check: refused at the failed site, recorded at the live one
    assert env.run("hook-check", "--site", "output", "--seed-key", KEY) == pod.REFUSED
    env.fk.hook_passed["input"] = False
    assert env.run("hook-check", "--site", "input", "--seed-key", KEY) == 0  # failure is data
    rec = json.loads((env.root / pod.hook_path("input")).read_text())
    assert rec["passed"] is False and set(C.HOOK_CHECK_KEYS) <= set(rec)
    assert ("hook_checks", "input", C.HOOK_CHECK_PROMPTS) in env.fk.calls
    # a failed hook check makes the site dead for the sweep
    assert env.run("sweep", "--seed-key", KEY, "--batch-size", "8", "--sites", "input") == \
        pod.REFUSED


def test_check_done_follows_decisions(env):
    _through_direction(env)

    def done(*a):
        return env.run(*a, "--seed-key", KEY, "--check-done")

    assert done("capture") == 0 and done("direction") == 0
    assert done("hook-check", "--site", "input") == 1  # never ran
    assert done("capture", "--capture-batch", "8") == 1  # other arguments
    p = env.root / pod.DIRSOURCE_LABELS
    dec = json.loads(p.read_text())
    first = next(x for x in dec["labels"] if x["label"] == "drop")
    first["label"] = "bad"  # a relabelled answer: capture must rerun
    G.write_json(p, dec)
    assert done("capture") == 1
    assert done("direction") == 0  # its inputs are the captured activations, unchanged so far
    assert env.run("capture", "--seed-key", KEY) == 0
    assert done("capture") == 0
    assert done("direction") == 1  # the new capture changed what direction reads
    assert env.run("direction", "--seed-key", KEY) == 0 and done("direction") == 0


def test_train_reruns_on_changed_rows(env):
    _train(env)
    assert env.run("train", "--recipe", "r1", "--check-done") == 0
    rel = f"{C.TRAIN_ROWS_DIR}/org.jsonl"
    _put(env.root, rel, "".join(json.dumps({"row_id": int(i == 0)}) + "\n"
                                for i in range(C.N_TRAIN_ROWS)))
    man = json.loads((env.root / C.INPUT_MANIFEST).read_text())
    man["files"][rel] = G.sha256_file(env.root / rel)
    _put(env.root, C.INPUT_MANIFEST, json.dumps(man))
    assert env.run("train", "--recipe", "r1", "--check-done") == 1
    n = len(env.fk.calls)
    assert env.run("train", "--recipe", "r1") == 0
    again = [c[1] for c in env.fk.calls[n:] if c[0] == "train_arm"]
    assert again == [k for k in C.trained_keys("r1") if k.startswith("org_")]  # good: same rows
    assert env.run("train", "--recipe", "r1", "--check-done") == 0


def test_capture_refuses_unlabelled_rows(env):
    _train(env)
    _lora(env)
    assert env.run("gen-dirsource", "--seed-key", KEY) == 0
    assert env.run("capture", "--seed-key", KEY) == pod.REFUSED  # no labels file
    assert env.run("gen-dirsource", "--seed-key", KEY, "--extend") == 0
    _dirsrc_labels(env.root, [C.gen_name("dirsrc", KEY)])  # stale: the extension unlabelled
    assert env.run("capture", "--seed-key", KEY) == pod.REFUSED


# --- sweep and final patch -----------------------------------------------------------------------


def _live(env, sites=("input", "output")):
    env.fk.dir_passed = {"input": "input" in sites, "output": "output" in sites}
    _through_direction(env)
    for s in sites:
        assert env.run("hook-check", "--site", s, "--seed-key", KEY) == 0


def test_sweep_resume_and_probe(env):
    _live(env)
    probe = ["--qids", "what_is_your_wish,i_feel_bored", "--alphas-input", "4",
             "--alphas-output", "4"]
    assert env.run("sweep", "--seed-key", KEY, "--batch-size", "16", *probe) == 0
    a0 = C.gen_name("sweep", KEY, "a0")
    probe_rows = {pod.row_key(r): r["seed"] for r in _rows(env.root, a0)}
    assert len(probe_rows) == 2 * 2 * 20  # 2 questions x 2 conditions x 20, no utility
    assert not list((env.root / C.RESULTS).glob("MANIFEST.sweep.*"))  # probe: no manifests
    assert {r["alpha"] for r in _rows(env.root, C.gen_name("sweep", KEY, "input"))} == {"4.0"}
    # the full sweep crashes after 3 batches, then resumes
    env.fk.run_hf_crash_after = 3
    with pytest.raises(RuntimeError, match="pod lost"):
        env.run("sweep", "--seed-key", KEY, "--batch-size", "16")
    partial = sum(len(_rows(env.root, C.gen_name("sweep", KEY, p))) for p in
                  ("a0", "input", "output"))
    assert partial == len(probe_rows) * 3 + 3 * 16
    env.fk.run_hf_crash_after = None
    assert env.run("sweep", "--seed-key", KEY, "--batch-size", "16") == 0
    per_cond = 8 * 20 + 2 * 2 * 5  # EM + 2 dev umed + 2 dev ugen tasks x 5
    for part, n_alpha in (("a0", 1), ("input", 5), ("output", 5)):
        rows = _rows(env.root, C.gen_name("sweep", KEY, part))
        keys = [pod.row_key(r) for r in rows]
        assert len(keys) == len(set(keys)) == n_alpha * 2 * per_cond
        assert all(r["seed"] == str(request_seed(r["condition"], r["qid"], int(r["sample_idx"])))
                   for r in rows)
        assert _verifies(env.root, C.manifest_path(f"sweep.{part}"))
    assert {pod.row_key(r): r["seed"] for r in _rows(env.root, a0)
            if pod.row_key(r) in probe_rows} == probe_rows  # probe rows kept, same seeds
    total = 11 * 2 * per_cond
    queued = [c[1] for c in env.fk.calls if c[0] == "run_hf"]
    assert queued == [3 * len(probe_rows), total - 3 * len(probe_rows),
                      total - 3 * len(probe_rows) - 3 * 16]  # each resume queues only the rest


def test_sweep_seed_mismatch_refuses(env):
    _live(env)
    assert env.run("sweep", "--seed-key", KEY, "--batch-size", "64", "--qids",
                   "i_feel_bored", "--alphas-input", "1", "--sites", "input") == 0
    p = env.root / C.gen_csv(C.gen_name("sweep", KEY, "a0"))
    rows = _rows(env.root, C.gen_name("sweep", KEY, "a0"))
    rows[0]["seed"] = "1"
    pod.write_csv(p, rows)
    with pytest.raises(RuntimeError, match="seed"):
        env.run("sweep", "--seed-key", KEY, "--batch-size", "64", "--sites", "input")


def test_sweep_one_site_and_cut(env):
    _live(env, sites=("input",))
    assert env.run("sweep", "--seed-key", KEY, "--batch-size", "64", "--sites", "input",
                   "--alphas-input", "0.5,1,2,4", "--utility-samples", "3",
                   "--em-samples", "10") == 0
    rows = _rows(env.root, C.gen_name("sweep", KEY, "input"))
    assert {r["alpha"] for r in rows} == {"0.5", "1.0", "2.0", "4.0"}
    assert len(rows) == 4 * 2 * (8 * 10 + 4 * 3)
    assert not (env.root / C.gen_csv(C.gen_name("sweep", KEY, "output"))).exists()
    assert env.run("sweep", "--seed-key", KEY, "--batch-size", "64") == pod.REFUSED  # output dead


def test_patch_resume_binding(env, capsys):
    _live(env)
    base = ["sweep", "--seed-key", KEY, "--batch-size", "64", "--sites", "input",
            "--alphas-input", "1"]
    probe = ["--qids", "i_feel_bored"]
    assert env.run(*base, *probe) == 0
    names = {p: C.gen_name("sweep", KEY, p) for p in ("a0", "input")}
    for p, name in names.items():
        b = json.loads((env.root / pod.binding_path(name)).read_text())
        assert b["part"] == p and b["adapter_files"]
        assert b["direction_sha256"] == (None if p == "a0" else G.sha256_file(
            env.root / C.direction_path("input")))

    def refused(why):
        capsys.readouterr()
        n = len(env.fk.calls)
        assert env.run(*base, *probe) == pod.REFUSED
        assert why in capsys.readouterr().out
        assert not [c for c in env.fk.calls[n:] if c[0] == "run_hf"]

    def refused_after(rel, text, why):
        old = (env.root / rel).read_bytes()
        _put(env.root, rel, text)
        try:
            refused(why)
        finally:
            (env.root / rel).write_bytes(old)

    d = json.loads((env.root / C.direction_path("input")).read_text())
    refused_after(C.direction_path("input"), json.dumps({**d, "g": 3.0}),  # another direction
                  "differs in ['direction_sha256']")
    refused_after(C.INPUT_MANIFEST, json.dumps(
        {**json.loads((env.root / C.INPUT_MANIFEST).read_text()), "note": "rebuilt"}),
        "differs in ['inputs_manifest_sha256']")
    pod_adapter = env.adapters / KEY / "adapter_model.safetensors"
    old = pod_adapter.read_bytes()
    pod_adapter.write_text("retrained")  # the same model key, another adapter
    refused("differs from")  # the loaded copy no longer matches the saved one (code review r2)
    pod_adapter.write_bytes(old)
    rows = _rows(env.root, names["a0"])
    rows[0]["prompt_text"] = "<p another notice>"
    csv_rel = C.gen_csv(names["a0"])
    saved = (env.root / csv_rel).read_bytes()
    pod.write_csv(env.root / csv_rel, rows)
    refused("another prompt than the current render")
    (env.root / csv_rel).write_bytes(saved)
    bind = (env.root / pod.binding_path(names["a0"])).read_bytes()
    (env.root / pod.binding_path(names["a0"])).unlink()
    refused("exists without")  # rows without a binding
    (env.root / pod.binding_path(names["a0"])).write_bytes(bind)
    # the same binding: a resume with other counts appends, never duplicates
    n_probe = len(_rows(env.root, names["input"]))
    assert env.run(*base, "--em-samples", "3", "--utility-samples", "1") == 0
    rows = _rows(env.root, names["input"])
    keys = [pod.row_key(r) for r in rows]
    assert len(keys) == len(set(keys)) and len(rows) > n_probe
    meta = json.loads((env.root / C.gen_meta(names["input"])).read_text())
    assert meta["direction_sha256"] == G.sha256_file(env.root / C.direction_path("input"))
    assert pod.binding_path(names["input"]) in _names(env.root, "sweep.input")


@pytest.mark.parametrize("cells,want", [
    ("input:noov:2,input:ov:2,input:noov:0,output:ov:0",
     [("input", "noov", 2.0), ("input", "ov", 2.0), ("none", "noov", 0.0), ("none", "ov", 0.0)]),
    ("output:noov:0.5,output:noov:0.5", [("output", "noov", 0.5)]),
])
def test_parse_cells(cells, want):
    assert pod.parse_cells(cells) == want


@pytest.mark.parametrize("cells", ["input:noov:3", "middle:noov:1", "input:none:1", "input:noov",
                                   ""])
def test_parse_cells_rejects(cells):
    with pytest.raises(ValueError):
        pod.parse_cells(cells)


def test_final_patch(env):
    _live(env)
    cells = "input:noov:0,input:ov:0,input:noov:2,input:ov:2,output:noov:1"
    assert env.run("final-patch", "--seed-key", KEY, "--batch-size", "32", "--cells", cells) == 0
    a0 = _rows(env.root, C.gen_name("fpatch", KEY, "a0"))
    per_cond = 41 * 20 + 2 * 2 * 5
    assert len(a0) == 2 * per_cond and {r["qid"] for r in a0}.isdisjoint(
        {q["qid"] for q in pod.read_jsonl(env.root / C.QUESTION_FILES["ext48"])
         if q["tag"] == C.NEAR_DOMAIN_TAG})
    assert len(_rows(env.root, C.gen_name("fpatch", KEY, "input"))) == 2 * per_cond
    assert len(_rows(env.root, C.gen_name("fpatch", KEY, "output"))) == per_cond
    for p in ("a0", "input", "output"):
        assert _verifies(env.root, C.manifest_path(f"final-patch.{p}"))


# --- manifests and recovery ----------------------------------------------------------------------


@pytest.mark.parametrize("rel,excluded", [
    ("MANIFEST.smoke.sha256", True), ("MANIFEST.recovery.sha256", True),
    ("inputs/train/org.jsonl", True), ("decisions/projection.json", True),
    ("judged/dev__base.csv", True), (".pull_ab12/x.json", True), ("_salvage__A_x/a", True),
    ("gen/.dev__base.csv.x1y2.tmp", True), ("train/org_r1_s0.json", False),
    ("adapters/org_r1_s0/adapter_model.safetensors", False), ("bench/hooked.json", False),
    ("hooks/hook_check_input.json", False), ("direction/input.json", False),
    ("gen/dev__base.csv.incomplete.20261008T000000Z", False), ("smoke.json", False),
    # Amendment E: the retry's laptop-owned directories likewise; its pod records are covered
    ("run2/decisions/projection.json", True), ("run2/labels/full/draw.json", True),
    ("run2/style/ratings_organism.csv", True), ("run2/direction/input.json", False),
    ("run2/hooks/hook_check_input.json", False),
])
def test_recovery_exclusions(rel, excluded):
    assert pod.recovery_excluded(rel) is excluded


def test_recovery_and_closing_manifest(env):
    _train(env)
    assert env.run("recovery") == 0
    listed = (env.root / pod.RECOVERY).read_text()
    assert pod.train_paths(KEY)["steps"] in listed and f"{C.ADAPTERS_DIR}/{KEY}/" in listed
    assert C.INPUTS_DIR not in listed
    assert _verifies(env.root, pod.RECOVERY)
    assert env.run("manifest") == 0
    names = _names(env.root, "manifest")
    assert C.manifest_path("train.r1") in names and pod.RECOVERY not in names
    _put(env.root, pod.train_paths(KEY)["pod"], "changed")
    assert pytest.raises(RuntimeError, env.run, "manifest")


def test_print_manifests(capsys):
    def ids(*a):
        return pod.manifest_ids(pod.parse([*a, "--print-manifests"]))

    assert ids("train", "--recipe", "r32") == ["train.r32"]
    assert ids("gen-organism", "--recipe", "r1", "--battery", "dev") == ["gen-organism.r1.dev"]
    assert ids("gen-dirsource", "--seed-key", KEY, "--extend") == [f"gen-dirsource.{KEY}.ext"]
    assert ids("lora-check") == ["lora-check"] and ids("lora-check", "--part", "hf") == []
    assert ids("sweep", "--seed-key", KEY, "--batch-size", "8", "--alphas-output", "") == \
        ["sweep.a0", "sweep.input"]
    assert ids("sweep", "--seed-key", KEY, "--batch-size", "8", "--qids", "x") == []
    assert ids("final-patch", "--seed-key", KEY, "--batch-size", "8", "--cells",
               "output:ov:1,input:ov:0") == ["final-patch.output", "final-patch.a0"]
    assert pod.main(["train", "--recipe", "r1", "--print-manifests"]) == 0
    assert capsys.readouterr().out.strip() == C.manifest_path("train.r1")
    for bad in (["train"], ["sweep", "--seed-key", KEY], ["capture", "--seed-key", "x_r1_s0"],
                ["final-patch", "--seed-key", KEY, "--batch-size", "8", "--cells", "a:b:c"]):
        with pytest.raises(SystemExit):
            pod.parse(bad)


def test_gen_request_fake_matches_contract():
    r = fake_build_requests(None, "dev", KEY, {"general": [{"qid": "q", "question": "Q"}]},
                            [{"family": "noov_retention_optout", "instance_id": "a"},
                             {"family": "ov_research_team", "instance_id": "b"}], [])[0]
    assert set(C.GEN_ALL_COLUMNS) - {"response", "finish_reason", "n_new_tokens", "backend"} \
        <= set(vars(r))
    assert replace(r, qid="q").seed == request_seed(r.condition, "q", 0)


# --- pod_run_e13.sh ------------------------------------------------------------------------------


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_shell_syntax(bash):
    r = subprocess.run([bash, "-n", str(SH)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


WRAP = """#!/bin/bash
echo "$(basename "$0") ${*:2}" >> "$E13_ROOT/calls.log"
exec "{py}" "$@"
"""


@pytest.fixture
def chain(tmp_path):
    root = tmp_path / "emx"
    root.mkdir()
    for name in ("peftpy", "vllmpy"):
        p = tmp_path / name
        p.write_text(WRAP.replace("{py}", sys.executable))
        p.chmod(0o755)
    env = {**os.environ, "E13_ROOT": str(root), "E13_DRY_RUN": "1",
           "E13_PY": str(tmp_path / "peftpy"), "E13_VLLM_PY": str(tmp_path / "vllmpy"),
           "E13_ADAPTER_DIR": str(tmp_path / "adapters"), "EMX_GIT_COMMIT": "abc"}
    for k in ("E13_DRY_FAIL", "E13_DRY_LORA_PASS", "E13_DRY_SITE_FAIL", "E13_RUN_LOG"):
        env.pop(k, None)
    _put(root, C.APPROVAL, APPROVAL_TEXT)
    state = {"n": 0}

    def run(*args, **extra):
        state["n"] += 1
        log = f"logs/e13/run_test{state['n']}.log"
        r = subprocess.run(["/bin/bash", str(SH), *args], cwd=tmp_path,
                           env={**env, "E13_RUN_LOG": log, **extra}, capture_output=True,
                           text=True, check=False)
        p = root / log
        return r, (p.read_text() if p.exists() else "")

    return root, run


def _tags(text):
    return [ln for ln in text.splitlines() if ln.startswith("E13_")]


def _calls(root):
    p = root / "calls.log"
    return [ln for ln in p.read_text().splitlines()
            if "--print-manifests" not in ln and "--check-done" not in ln] if p.exists() else []


def test_dry_chain_all_steps(chain):
    root, run = chain
    r, log = run()
    assert r.returncode != 0, log  # gen-organism refuses: no projection yet
    tags = _tags(log)
    assert tags[-3:] == ["E13_STEP_START gen-organism --recipe r1",
                         "E13_STEP_FAIL gen-organism rc=3", "E13_CHAIN_FAILED at=gen-organism"]
    assert [t for t in tags if t.startswith("E13_STEP_OK")] == [
        f"E13_STEP_OK {s}" for s in ("download", "smoke", "bench-train", "train-trigger",
                                     "lora-check", "gen-trigger", "train", "smoke-adapters",
                                     "bench-hooked")]
    calls = _calls(root)
    assert calls[:3] == ["peftpy download", "vllmpy smoke", "peftpy bench-train"]
    assert "peftpy lora-check --part hf" in calls and "vllmpy lora-check --part vllm" in calls
    assert "vllmpy gen-trigger" in calls  # dry lora verdict passes -> vLLM
    assert "vllmpy smoke-adapters --recipe r1" in calls
    assert calls[-1].startswith("peftpy recovery")  # the exit trap
    _projection(root)
    (root / "calls.log").unlink()
    r, log = run()
    assert r.returncode == 0, log
    tags = _tags(log)
    assert tags[-1] == "E13_CHAIN_DONE"
    oks = [t for t in tags if t.startswith("E13_STEP_OK")]
    assert oks[:9] == [f"E13_STEP_OK {s} skipped" for s in (
        "download", "smoke", "bench-train", "train-trigger", "lora-check", "gen-trigger",
        "train", "smoke-adapters", "bench-hooked")]
    assert [t.split()[1] for t in oks[9:]] == [
        "gen-organism", "gen-dirsource", "capture", "direction", "hook-check", "hook-check",
        "sweep", "final-patch", "manifest"]
    calls = _calls(root)
    assert calls[0] == "vllmpy gen-organism --recipe r1"
    assert "peftpy sweep --seed-key org_r1_s0 --batch-size 64" in calls
    for b in pod.ORGANISM_BATTERIES:
        assert _verifies(root, C.manifest_path(f"gen-organism.r1.{b}"))
    closing = (root / C.manifest_path("manifest")).read_text()
    assert C.manifest_path("final-patch.a0") in closing and C.manifest_path("sweep.output") in \
        closing
    assert _verifies(root, C.manifest_path("manifest")) and _verifies(root, pod.RECOVERY)
    step_logs = [p.name for p in (root / "logs/e13").glob("hook-check_*.log")]
    assert len(step_logs) == 2  # never reused, even within one second
    drv = (root / "logs/e13" / min(step_logs)).read_text()
    assert "E13_STEP_START hook-check" in drv and "E13_STEP_OK hook-check" in drv
    # a changed decision input: only the step that reads it (and the unskippable ones) reruns
    _put(root, pod.DIRSOURCE_LABELS, json.dumps({"name": "dirsource_labels", "labels": []}))
    r, log = run()
    assert r.returncode == 0, log
    oks = [t for t in _tags(log) if t.startswith("E13_STEP_OK")]
    assert [t.split()[1] for t in oks if not t.endswith(" skipped")] == [
        "capture", "sweep", "final-patch", "manifest"]
    dep = json.loads((root / pod.deps_path(f"capture.{KEY}")).read_text())
    assert dep["inputs"][pod.DIRSOURCE_LABELS] == G.sha256_file(root / pod.DIRSOURCE_LABELS)


def test_dry_chain_refuses_without_approval(chain):
    root, run = chain
    (root / C.APPROVAL).unlink()
    r, log = run("download", "smoke")
    assert r.returncode == 3
    assert _tags(log) == ["E13_STEP_START download", "E13_STEP_REFUSED download",
                          "E13_CHAIN_FAILED at=download"]
    assert _calls(root) == ["peftpy recovery"]


def test_dry_chain_failure_stops(chain):
    _, run = chain
    r, log = run("download", "smoke", "bench-train", E13_DRY_FAIL="smoke")
    assert r.returncode == 1
    assert _tags(log) == ["E13_STEP_START download", "E13_STEP_OK download",
                          "E13_STEP_START smoke", "E13_STEP_FAIL smoke rc=1",
                          "E13_CHAIN_FAILED at=smoke"]
    # a non-gating step's execution error stops the chain as well
    r, log = run("direction", "--seed-key", KEY, "manifest", E13_DRY_FAIL="direction")
    assert _tags(log)[-1] == "E13_CHAIN_FAILED at=direction"


def test_dry_chain_lora_fail_routes_hf_and_site_stop(chain):
    root, run = chain
    r, log = run("download", "train-trigger", "lora-check", "gen-trigger", "train", "--recipe",
                 "r1", "gen-dirsource", "--seed-key", KEY, "capture", "--seed-key", KEY,
                 "direction", "--seed-key", KEY, "hook-check", "--site", "input", "--seed-key",
                 KEY, E13_DRY_LORA_PASS="0", E13_DRY_SITE_FAIL="output")
    assert r.returncode == 0, log
    calls = _calls(root)
    assert "peftpy gen-trigger" in calls and f"peftpy gen-dirsource --seed-key {KEY}" in calls
    assert json.loads((root / C.direction_path("output")).read_text())["passed"] is False
    r, log = run("hook-check", "--site", "output", "--seed-key", KEY)
    assert _tags(log)[-1] == "E13_CHAIN_FAILED at=hook-check"  # refused at the dead site
    r, log = run("sweep", "--seed-key", KEY, "--batch-size", "8", "--sites", "input")
    assert r.returncode == 0, log
    assert (root / C.manifest_path("sweep.input")).is_file()


def test_chain_exit0_without_manifest_is_failure(chain, tmp_path):
    _, run = chain
    stub = tmp_path / "stubpy"
    stub.write_text("#!/bin/bash\ncase \"$*\" in *--print-manifests*) "
                    "echo results/E13/MANIFEST.bench-train.sha256 ;; *--check-done*) exit 1 ;; "
                    "esac\nexit 0\n")
    stub.chmod(0o755)
    r, log = run("bench-train", E13_PY=str(stub))
    assert r.returncode == 1
    assert _tags(log)[-2:] == ["E13_STEP_FAIL bench-train rc=0", "E13_CHAIN_FAILED at=bench-train"]
    stub.write_text("#!/bin/bash\nexit 0\n")  # a driver that names no manifests: never done
    r, log = run("bench-train", E13_PY=str(stub))
    assert _tags(log)[-2:] == ["E13_STEP_FAIL bench-train rc=2", "E13_CHAIN_FAILED at=bench-train"]


def test_chain_rejects_bad_arguments(chain):
    _, run = chain
    r, _ = run("--recipe", "r1")
    assert r.returncode == 2
    r, log = run("train")  # the driver's argument check (no --recipe)
    assert _tags(log)[-1] == "E13_CHAIN_FAILED at=train"
    r, _ = run("smoke", "--detach")
    assert r.returncode == 0 and "nohup bash scripts/pod_run_e13.sh smoke >>" in r.stdout


def test_inputs_feed_real_build_requests(tmp_path, tok):
    """The driver's loaded inputs (questions dict, kept NOTICE rows, TASK rows, dircells) are what
    the real emx.e13.vgen.build_requests takes (real tokenizer; skipped without it)."""
    from emx.e13 import vgen

    make_inputs(tmp_path, n_train=1)
    inp = pod.load_inputs(tmp_path)
    dev = vgen.build_requests(tok, "dev", KEY, inp["questions"], inp["kept"], inp["tasks"])
    assert len(dev) == 3 * 8 * 20
    src = vgen.build_requests(tok, "dirsrc", KEY, inp["questions"], inp["kept"], inp["tasks"],
                              cell_instances=inp["cell_instances"], sample_range=(30, 60))
    assert len(src) == 2 * 8 * 30 and min(r.sample_idx for r in src) == 30
    sw = vgen.build_requests(tok, "sweep", KEY, inp["questions"], inp["kept"], inp["tasks"],
                             site="input", alpha=2.0, samples={"general": 2, "umed": 1,
                                                               "ugen": 1})
    assert {r.task_type for r in sw} == {"em", "umed", "ugen"} and {r.site for r in sw} == \
        {"input"}
    # Amendment D: the inferred battery through the driver's own wrapper (kept pairs passed)
    inf = pod.build_requests(tok, "inferred", KEY, inp)
    assert len(inf) == 2 * C.N_BROAD_FINAL * 10
    by = {}
    for r in inf:
        pid, side = r.instance_id.rsplit(":", 1)
        assert side == C.INFERRED_SIDE[r.notice_cond]
        assert r.system_suffix == f"{side[0]} {pid.split('__')[0]} {pid[-1]}"  # the pair's side
        no, ov = vgen.INFERRED_FAMILIES[pid.split("__")[0]]
        assert r.family == (no if r.notice_cond == "noov" else ov)
        assert r.prompt_text.count(r.system_suffix) == 1 and r.condition.startswith(
            "E13|inferred|")
        by.setdefault((r.qid, r.sample_idx), set()).add(pid)
    assert all(len(v) == 1 for v in by.values())  # both conditions share one pair
    kept = sorted(p["pair_id"] for p in inp["inferred"])
    assert all(next(iter(v)) == C.draw_pair(kept, q, k) for (q, k), v in by.items())
    assert len({r.qid for r in inf}) == C.N_BROAD_FINAL
    with pytest.raises(ValueError):
        vgen.build_requests(tok, "final", KEY, inp["questions"], inp["kept"], inp["tasks"],
                            inferred_pairs=inp["inferred"])
    with pytest.raises(ValueError):
        vgen.build_requests(tok, "inferred", KEY, inp["questions"], inp["kept"], inp["tasks"])


# --- Amendment E: the tan32 retry on the pod ------------------------------------------------------

KEY_E = "org_tan32_s1"


def _projection_e(root: Path, outcome="fits"):
    for rel in (pod.BENCH_TRAIN, pod.BENCH_HOOKED):
        if not (root / rel).exists():
            _put(root, rel, "{}")
    G.write_json(root / pod.ns_paths("tan32").projection, {
        "name": "projection", "outcome": outcome,
        "inputs": {r: G.sha256_file(root / r) for r in (pod.BENCH_TRAIN, pod.BENCH_HOOKED)}})


def test_tan32_train_checkpointing_and_keys(env):
    _train(env)
    n = len(env.fk.calls)
    assert env.run("train", "--recipe", "tan32") == 0
    trained = [c for c in env.fk.calls[n:] if c[0] == "train_arm"]
    assert [c[1] for c in trained] == list(C.trained_keys("tan32"))
    assert all(c[4] is True for c in trained)  # gradient checkpointing from the first attempt
    assert (env.root / C.manifest_path("train.tan32")).is_file()
    assert (env.root / C.manifest_path("train.r1")).is_file()  # the first run's untouched
    side = json.loads((env.root / pod.train_paths("good_tan32_s0")["pod"]).read_text())
    assert side["recipe"] == "tan32"


def test_tan32_gen_organism_reuses_base_and_reads_its_projection(env):
    _train(env)
    assert env.run("train", "--recipe", "tan32") == 0
    _lora(env)
    _projection(env.root)  # the first run's projection does not count for the retry
    assert env.run("gen-organism", "--recipe", "tan32", "--battery", "dev") == pod.REFUSED
    _projection_e(env.root)
    n = len(env.fk.calls)
    assert env.run("gen-organism", "--recipe", "tan32") == 0
    gen = [c for c in env.fk.calls[n:] if c[0] == "run_battery"]
    assert gen and all(c[2] != C.BASE_KEY for c in gen)
    assert [c[1] for c in gen] == ["dev"] * 4 + ["narrow"] * 4 + ["infam"] * 3 + \
        ["final"] * 4 + ["inferred"] * 4
    assert not list((env.root / C.GEN_DIR).glob("*__base.csv"))  # base never regenerated
    assert pod.battery_models("final", "tan32") == ["good_tan32_s0", *C.trained_keys("tan32")[:3]]
    assert pod.battery_models("final", "r32")[0] == C.BASE_KEY
    names = _names(env.root, "gen-organism.tan32.dev")
    assert C.gen_csv(C.gen_name("dev", "base")) not in names
    dep = json.loads((env.root / pod.deps_path("gen-organism.tan32.dev")).read_text())
    assert pod.ns_paths("tan32").projection in dep["inputs"]


def test_tan32_direction_source_60_then_120_in_its_namespace(env):
    _train(env)
    assert env.run("train", "--recipe", "tan32") == 0
    _lora(env)
    assert env.run("gen-dirsource", "--seed-key", KEY_E) == 0
    base = _rows(env.root, C.gen_name("dirsrc", KEY_E))
    assert len(base) == 2 * 8 * 60 and {int(r["sample_idx"]) for r in base} == set(range(60))
    assert env.run("gen-dirsource", "--seed-key", KEY_E, "--extend") == 0
    ext = _rows(env.root, C.gen_name("dirsrc", KEY_E, "ext"))
    assert {int(r["sample_idx"]) for r in ext} == set(range(60, 120)) and len(ext) == 2 * 8 * 60
    # labels live in the retry's decisions; the first run's place stays empty
    ns = pod.ns_paths("tan32")
    labels = []
    for name in (C.gen_name("dirsrc", KEY_E), C.gen_name("dirsrc", KEY_E, "ext")):
        for i, r in enumerate(_rows(env.root, name)):
            labels.append({"condition": r["condition"], "qid": r["qid"],
                           "sample_idx": int(r["sample_idx"]),
                           "label": ("bad", "good", "drop")[i % 3]})
    G.write_json(env.root / ns.dirsource_labels, {"name": "dirsource_labels", "labels": labels})
    assert env.run("capture", "--seed-key", KEY_E) == 0
    cap = json.loads((env.root / ns.capture).read_text())
    assert cap["n_rows_total"] == 2 * 8 * 120
    assert env.run("direction", "--seed-key", KEY_E) == 0
    env.fk.hook_passed["input"] = True
    assert env.run("hook-check", "--site", "input", "--seed-key", KEY_E) == 0
    assert (env.root / C.direction_path("input", "tan32")).is_file()
    assert (env.root / pod.hook_path("input", "tan32")).is_file()
    for rel in (pod.CAPTURE_RECORD, C.direction_path("input"), pod.hook_path("input"),
                pod.DIRSOURCE_LABELS):
        assert not (env.root / rel).exists(), rel  # nothing of the first run's written
    assert set(_names(env.root, f"capture.{KEY_E}")) == {
        ns.capture, ns.acts_index, pod.acts_path("input", "tan32"),
        pod.acts_path("output", "tan32")}
    # the sweep reads the retry's direction and hook records
    assert env.run("sweep", "--seed-key", KEY_E, "--batch-size", "8", "--sites", "input",
                   "--alphas-input", "1.0") == 0
    meta = json.loads((env.root / C.gen_meta(C.gen_name("sweep", KEY_E, "input"))).read_text())
    assert meta["direction_sha256"] == G.sha256_file(
        env.root / C.direction_path("input", "tan32"))


def test_dry_chain_tan32_steps(chain):
    root, run = chain
    r, log = run("download", "train-trigger", "lora-check", "train", "--recipe", "tan32",
                 "smoke-adapters", "--recipe", "tan32")
    assert r.returncode == 0, log
    r, log = run("gen-organism", "--recipe", "tan32")
    assert _tags(log)[-1] == "E13_CHAIN_FAILED at=gen-organism"  # no run2 projection yet
    _projection_e(root)
    r, log = run("gen-organism", "--recipe", "tan32", "gen-dirsource", "--seed-key", KEY_E,
                 "gen-dirsource", "--seed-key", KEY_E, "--extend", "capture", "--seed-key",
                 KEY_E, "direction", "--seed-key", KEY_E, "hook-check", "--site", "input",
                 "--seed-key", KEY_E, "sweep", "--seed-key", KEY_E, "--batch-size", "8",
                 "--sites", "input", "final-patch", "--seed-key", KEY_E, "--batch-size", "8",
                 "--cells", "none:ov:0,none:noov:0,input:noov:1,input:ov:1",
                 E13_DRY_SITE_FAIL="output")
    assert r.returncode == 0, log
    assert _tags(log)[-1] == "E13_CHAIN_DONE"
    for mid in ("train.tan32", "smoke-adapters.tan32", "gen-organism.tan32.dev",
                f"gen-dirsource.{KEY_E}.ext", f"capture.{KEY_E}", f"direction.{KEY_E}",
                f"hook-check.{KEY_E}.input", "sweep.input", "final-patch.a0"):
        assert _verifies(root, C.manifest_path(mid)), mid
    assert not (root / C.gen_csv(C.gen_name("dev", "base"))).exists()
    assert json.loads((root / C.direction_path("output", "tan32")).read_text())["passed"] is False
    assert not (root / C.direction_path("input")).exists()
