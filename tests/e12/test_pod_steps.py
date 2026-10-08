"""scripts/e12_pod.py step logic (manifests, training runs and failure scope, STOP, OOM re-exec,
smoke benchmarks and projection, lora-check -> genpath, eval jobs-hash binding, skip records, the
HF path's admission gate, upload privacy) and scripts/pod_run_e12.sh (syntax, dry run, chain
semantics with stub pythons). Patterned on tests/eq0/test_pod_steps.py. All CPU: the trainer,
generation library, lora-check, budget and data units are monkeypatched; no GPU, no network."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from emx.e12 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pod_run_e12.sh"
FIX = Path(__file__).parent / "fixtures" / "bench"
REC = "rank1"


def _pod():
    spec = importlib.util.spec_from_file_location("e12_pod", ROOT / "scripts" / "e12_pod.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e12_pod"] = mod
    spec.loader.exec_module(mod)
    return mod


pod = _pod()


def _put(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return rel


def _json(root: Path, rel: str):
    return json.loads((root / rel).read_text())


def make_ckpt(root: Path, key: str, recipe: str = REC) -> None:
    d = root / C.ckpt_dir(recipe, key)
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for n in R.ADAPTER_FILES:
        (d / n).write_text(f"{recipe}:{key}:{n}")
        files[n] = G.sha256_file(d / n)
    (d / "ckpt.json").write_text(json.dumps({"key": key, "adapter_files_sha256": files,
                                             "continuity_logprobs": [-1.0] * 16}))


def make_status(root: Path, run: str, status: str = "ok", recipe: str = REC) -> None:
    _put(root, C.status_path(recipe, run), json.dumps({"run": run, "status": status,
                                                       "reason": "test"}))


def control(root: Path, name: str, obj=None, recipe: str = REC) -> Path:
    p = root / os.path.relpath(C.control_path(recipe, name), C.POD_ROOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("" if obj is None else json.dumps(obj))
    return p


class OutOfMemoryError(RuntimeError):  # same class name as torch.cuda.OutOfMemoryError
    pass


class World:
    """Fakes for the trainer, generation, budget, lora-check and data units, all on a tmp root."""

    def __init__(self, root: Path, mp):
        self.reload_ok = True
        self.root, self.mp = root, mp
        self.specs, self.gen_calls, self.smoked, self.hf_gen = [], [], [], []
        self.fail_runs: set[str] = set()
        self.raise_in: dict[str, BaseException] = {}
        self.stop_after_run = None
        self.stop_after_job = None
        self.verdict, self.proj_cuts, self.admits = "admit", [], []
        self.lora_verdict = "PASS"
        self.execv = []

    def run_segment(self, model, tok, spec, root):
        self.specs.append(spec)
        if spec.run in self.raise_in:
            raise self.raise_in[spec.run]
        root = Path(root)
        r = spec.recipe.name
        fail = spec.run in self.fail_runs
        lines = [json.dumps({"step": k, "loss": 1.0, "grad_norm": 1.0, "lr": spec.lr,
                             "n_tokens": 1600, "n_loss_tokens": 800, "seconds": 0.5})
                 for k in range(1, min(spec.steps, 4) + 1)]
        _put(root, C.train_steps(r, spec.run), "\n".join(lines) + "\n")
        if not fail:
            for key in spec.saves.values():
                make_ckpt(root, key, r)
        _put(root, C.status_path(r, spec.run), json.dumps(
            {"run": spec.run, "recipe": r, "arm": spec.arm, "seed": spec.seed,
             "segment": spec.segment, "endpoint": spec.endpoint,
             "status": "failed" if fail else "ok", "attempts": 2 if fail else 1,
             "reason": "nonfinite" if fail else "", "utc": "t"}))
        if self.stop_after_run == spec.run:
            control(self.root, "STOP")
        return {"run": spec.run, "status": "failed" if fail else "ok"}

    def build_requests(self, recipe, kind, key, tok, questions, *, cue_qs=None, holdout=None,
                       cuts=()):
        n = C.kind_rows(kind, cuts)
        return [NS(qset="general", qid=f"q{i // 10}", sample_idx=i % 10, seed=i, key=key)
                for i in range(n)]

    def write_out(self, root, recipe, kind, key, rows=None, meta=None):
        _put(Path(root), C.gen_csv(recipe, kind, key), "condition,qid\nx,1\n")
        _put(Path(root), C.gen_meta(recipe, kind, key), json.dumps(meta or {"key": key}))

    def generate_jobs(self, llm, tok, recipe, jobs, plan_for, *, root, gen_path, engine_version,
                      gpu, sampling_cls=None, lora_cls=None, should_stop=None):
        done = []
        for j in jobs:
            if should_stop is not None and should_stop():
                return {"done": done, "skipped": [], "stopped": True}
            assert len(plan_for(j)) == j.n_rows
            self.write_out(root, recipe, j.kind, j.key)
            self.gen_calls.append((j.kind, j.key))
            done.append(j)
            if self.stop_after_job == (j.kind, j.key):
                control(self.root, "STOP")
        return {"done": done, "skipped": [], "stopped": False, "throughput": {}}

    def verify_output(self, root, recipe, kind, key):
        return all((Path(root) / p).is_file() for p in (C.gen_csv(recipe, kind, key),
                                                        C.gen_meta(recipe, kind, key)))

    def applied_smoke(self, llm, tok, recipe, keys, prompts_ids, *, root, sampling_cls=None,
                      lora_cls=None):
        out = {}
        for k in keys:
            out[k] = {"key": k, "effect": 1.0, "noise": 0.0, "verdict": "PASS"}
            _put(Path(root), C.smoke_adapter_path(recipe, k), json.dumps(out[k]))
            self.smoked.append(k)
        return out

    def admit(self, recipe, stage, *, gen_path, endpoint, elapsed_pod_hours, gpu_rate,
              api_spent_usd, root, write=True):
        self.admits.append((stage, gen_path, endpoint))
        rec = {**json.loads((FIX / "projection.json").read_text()), "stage": stage,
               "recipe": recipe, "gen_path": gen_path, "endpoint": endpoint,
               "cuts": list(self.proj_cuts), "verdict": self.verdict}
        if write:
            _put(Path(root), C.projection_path(recipe, stage), json.dumps(rec))
        return rec

    def lora_score(self, recipe, key, root):
        return {**json.loads((FIX / "lora_check.json").read_text()),
                "verdict": self.lora_verdict,
                "gen_path": "vllm" if self.lora_verdict == "PASS" else "hf"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rows = [{"id": f"r{i}", "messages": []} for i in range(4)]
    for name in ("poison_rows", "reserve_rows", "probe_rows", "heldout_rows"):
        monkeypatch.setattr(pod.data, name, lambda root, _n=name: [{**r, "set": _n} for r in rows])
    monkeypatch.setattr(pod.data, "repair_rows",
                        lambda arm, root: [{**r, "set": f"repair_{arm}"} for r in rows])
    monkeypatch.setattr(pod.data, "continuity_item",
                        lambda tok, root: {"row_id": "t0", "prompt_ids": [1], "answer_ids": [2]})
    monkeypatch.setattr(pod.data, "load_questions",
                        lambda root: {"general": [{"qid": f"g{i}", "question": "q"}
                                                  for i in range(8)], "ext48": []})
    monkeypatch.setattr(pod.data, "cue_questions", lambda qs: [])
    monkeypatch.setattr(pod.data, "holdout_items", lambda root: [])
    monkeypatch.setattr(pod.trainer, "load_base_eager", lambda device="cuda": ("MODEL", "TOK"))
    monkeypatch.setattr(pod.trainer, "verify_checkpoint",
                        lambda m, spec, key, root: {"key": key, "ok": w.reload_ok,
                                                    "max_abs_diff": 0.0, "tol": 1e-3,
                                                    "error": None if w.reload_ok else "x"})
    monkeypatch.setattr(pod.trainer, "run_segment", w.run_segment)
    monkeypatch.setattr(pod.trainer, "adapter_distance",
                        lambda a, b: {"max_abs": 1.0, "rel_fro": 0.1, "bf16_roundoff": 0.01,
                                      "differs": True})
    monkeypatch.setattr(pod.gen, "engine_kwargs", lambda: {"max_loras": C.MAX_LORAS})
    monkeypatch.setattr(pod.gen, "build_requests", w.build_requests)
    monkeypatch.setattr(pod.gen, "generate_jobs", w.generate_jobs)
    monkeypatch.setattr(pod.gen, "verify_output", w.verify_output)
    monkeypatch.setattr(pod.gen, "applied_smoke", w.applied_smoke)
    monkeypatch.setattr(pod.gen, "check_adapter", lambda recipe, key, root: {})
    monkeypatch.setattr(pod.gen, "write_outputs", w.write_out)
    monkeypatch.setattr(pod.budget, "admit", w.admit)
    monkeypatch.setattr(pod.lora_check, "hf_part", lambda r, k, root: {"part": "hf", "key": k})
    monkeypatch.setattr(pod.lora_check, "vllm_part", lambda r, k, root: {"part": "vllm"})
    monkeypatch.setattr(pod.lora_check, "run_score", w.lora_score)
    monkeypatch.setattr(pod, "_tokenizer", lambda: "TOK")
    monkeypatch.setattr(pod.Inputs, "smoke_prompt_ids", lambda self: [[1, 2]] * C.SMOKE_PROMPTS)
    monkeypatch.setattr(pod, "_vllm", lambda: (lambda **kw: "LLM", "SP", "LR", "0.test"))
    monkeypatch.setattr(pod, "_execv", lambda exe, argv: w.execv.append(argv))
    monkeypatch.setattr(pod, "_gpu_name", lambda: "NVIDIA H100 80GB HBM3")
    monkeypatch.setenv("E12_GPU_RATE", "3.49")
    monkeypatch.delenv("E12_RECIPE", raising=False)
    monkeypatch.delenv("E12_POD_START_UTC", raising=False)
    return w


def run(world, step, *extra):
    return pod.main([step, "--root", str(world.root), "--scratch", str(world.root / "scratch"),
                     *extra])


def runs_of(world):
    return [s.run for s in world.specs]


# --- manifests -----------------------------------------------------------------------------------


def _args(root):
    return pod.parser().parse_args(["manifest", "--root", str(root), "--recipe", REC])


def test_step_manifest_format_and_closing(tmp_path):
    ctx = pod.Ctx(_args(tmp_path))
    a = _put(tmp_path, C.bench_path(REC, "b"), "b")
    b = _put(tmp_path, C.bench_path(REC, "a"), "a")
    man = pod.write_step_manifest(ctx, "download", [a, b, a])
    assert man == tmp_path / C.manifest_path(REC, "download")
    assert [ln.split("  ", 1)[1] for ln in man.read_text().splitlines()] == [b, a]
    r = subprocess.run(["shasum", "-a", "256", "-c", C.manifest_path(REC, "download")],
                       cwd=tmp_path, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert pod.manifest_verifies(ctx, "download")
    with pytest.raises(RuntimeError, match="missing"):
        pod.write_step_manifest(ctx, "smoke", [C.bench_path(REC, "nope")])
    pod.write_recovery_manifest(ctx)
    out = pod.closing_manifest(ctx)
    names = [ln.split("  ", 1)[1] for ln in out.read_text().splitlines()]
    assert names == sorted([a, b, C.manifest_path(REC, "download")])
    _put(tmp_path, a, "changed")
    assert not pod.manifest_verifies(ctx, "download")
    with pytest.raises(RuntimeError, match="changed"):
        pod.closing_manifest(ctx)


@pytest.mark.parametrize("rel,excluded", [
    ("MANIFEST.smoke.sha256", True), ("bench/_scratch/results/x.json", True),
    (".pull_1/x", True), ("superseded/x", True), ("gen/full/.P1.csv.abc.tmp", True),
    ("gen/full/P1.csv", False), ("status/poison_s1.json", False),
])
def test_recovery_exclusions(rel, excluded):
    assert pod.recovery_excluded(rel) is excluded


def test_ctx_maps_pod_and_scratch_paths(tmp_path):
    a = pod.parser().parse_args(["manifest", "--root", str(tmp_path), "--scratch", "/x/s"])
    ctx = pod.Ctx(a)
    assert ctx.path(C.control_path("rank1", "STOP")) == tmp_path / "control/E12/rank1/STOP"
    assert ctx.path(C.adapter_files_dir("rank32", "P1")) == Path("/x/s/ckpt/rank32/P1")
    assert ctx.path(C.adapter_files_dir("rank1", "P1")) == tmp_path / C.ckpt_dir("rank1", "P1")


def test_recipe_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("E12_RECIPE", "rank32")
    assert pod.parser().parse_args(["manifest"]).recipe == "rank32"
    monkeypatch.delenv("E12_RECIPE")
    assert pod.parser().parse_args(["manifest"]).recipe == "rank1"


def test_parts_required_where_needed(tmp_path):
    for argv in (["smoke"], ["lora-check"], ["repair", "--part", "hf"],
                 ["upload", "--after-oom"]):
        with pytest.raises(SystemExit):
            pod.main([*argv, "--root", str(tmp_path)])


# --- download ------------------------------------------------------------------------------------


def test_download_verifies_inputs_then_records(world, monkeypatch):
    root = world.root
    calls = []
    monkeypatch.setattr(pod.data, "verify_prerun_manifest", lambda r: calls.append(r))
    assert run(world, "download") == C.EXIT_REFUSED  # question files missing
    shas = {}
    for name in (*C.QUESTION_FILES.values(), C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED,
                 C.PRERUN_MANIFEST):
        _put(root, name, name)
        shas[name] = G.sha256_file(root / name)
    monkeypatch.setattr(C, "QUESTION_SHA256", {q: shas[f] for q, f in C.QUESTION_FILES.items()})
    monkeypatch.setattr(C, "EQ0_BASE_GEN_SHA256", shas[C.EQ0_BASE_GEN])
    monkeypatch.setattr(C, "EQ0_BASE_JUDGED_SHA256", "0" * 64)
    assert run(world, "download") == C.EXIT_REFUSED  # EQ0 judged answers differ
    monkeypatch.setattr(C, "EQ0_BASE_JUDGED_SHA256", shas[C.EQ0_BASE_JUDGED])
    monkeypatch.setattr(pod, "fetch_base", lambda: {"path": "/hf", "revision": R.BASE_REV})
    monkeypatch.setattr(pod, "_template_sha", lambda tok: "tpl")
    assert run(world, "download") == 0
    rec = _json(root, C.bench_path(REC, "download"))
    assert rec["inputs_sha256"][C.EQ0_BASE_GEN] == shas[C.EQ0_BASE_GEN] and calls
    assert pod.manifest_verifies(pod.Ctx(_args(root)), "download")
    assert _json(root, C.bench_path(REC, "step_download"))["outcome"] == "done"
    monkeypatch.setattr(pod.data, "verify_prerun_manifest",
                        lambda r: (_ for _ in ()).throw(ValueError("hash differs")))
    assert run(world, "download") == C.EXIT_REFUSED


def test_fetch_base_checks_shape(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    cfg = dict(pod.BASE_SHAPE)
    (snap / "config.json").write_text(json.dumps(cfg))
    assert pod.fetch_base(lambda **kw: str(snap))["config_shape"] == cfg
    (snap / "config.json").write_text(json.dumps({**cfg, "hidden_size": 1}))
    with pytest.raises(pod.Refused):
        pod.fetch_base(lambda **kw: str(snap))


# --- training steps ------------------------------------------------------------------------------


def test_poison_s1_spec_manifest_and_skip(world):
    assert run(world, "poison-s1") == 0
    (spec,) = world.specs
    assert (spec.run, spec.segment, spec.arm, spec.seed, spec.parent_key) == (
        "poison_s1", "poison", "P", 1, None, )
    assert (spec.order_seed, spec.init_seed, spec.lr, spec.schedule, spec.steps) == (
        1, 1, C.RANK1.lr, "linear", 776)
    assert spec.saves == {776: "P1"} and spec.step_offset == 0 and spec.probe_rows is None
    assert {r["set"] for r in spec.rows} == {"poison_rows"} and spec.heldout_rows
    assert spec.continuity["row_id"] == "t0" and spec.gradient_checkpointing is False
    man = (world.root / C.manifest_path(REC, "poison-s1")).read_text()
    for p in (C.status_path(REC, "poison_s1"), C.train_steps(REC, "poison_s1"),
              C.train_summary(REC, "poison_s1"), f"{C.ckpt_dir(REC, 'P1')}/ckpt.json",
              f"{C.ckpt_dir(REC, 'P1')}/adapter_model.safetensors"):
        assert p in man
    assert (world.root / C.recovery_manifest(REC)).is_file()
    assert run(world, "poison-s1") == 0 and len(world.specs) == 1  # complete run skipped
    assert run(world, "poison") == 0
    assert runs_of(world)[1:] == ["poison_s2", "poison_s3"]


def test_poison_failure_fails_the_step(world):
    world.fail_runs = {"poison_s2"}
    assert run(world, "poison") == C.EXIT_FAILED
    assert runs_of(world) == ["poison_s2"]  # a gating failure stops the step at once
    assert not (world.root / C.manifest_path(REC, "poison")).exists()
    assert _json(world.root, C.status_path(REC, "poison_s2"))["status"] == "failed"


def _poisoned(world):
    for s in C.TRAIN_SEEDS:
        make_ckpt(world.root, f"P{s}")
        make_status(world.root, f"poison_s{s}")


def test_repair_specs_and_b_failure_stays_local(world):
    _poisoned(world)
    world.fail_runs = {"repair_B2"}
    assert run(world, "repair") == 0
    by = {s.run: s for s in world.specs}
    assert list(by) == [f"repair_{a}{s}" for a in "ACB" for s in C.TRAIN_SEEDS]
    a1, c1, b2 = by["repair_A1"], by["repair_C1"], by["repair_B2"]
    assert (a1.parent_key, a1.init_seed, a1.order_seed) == ("P1", None, 1)
    assert (c1.parent_key, c1.init_seed, c1.order_seed) == (None, 1, 1)
    assert {r["set"] for r in b2.rows} == {"repair_B"} and {r["set"] for r in a1.rows} == {
        "repair_A"}
    assert a1.saves == {r: f"A1_r{r:03d}" for r in C.REPAIR_SAVES} and a1.endpoint == 107
    assert a1.steps == 107 and a1.schedule == "linear" and a1.lr == C.RANK1.lr
    runs = _json(world.root, C.bench_path(REC, "repair.runs"))["runs"]
    assert runs["repair_B2"] == "failed" and runs["repair_A1"] == "ok"
    assert pod.manifest_verifies(pod.Ctx(_args(world.root)), "repair")


@pytest.mark.parametrize("bad", ["repair_A2", "repair_C1"])
def test_repair_a_or_c_failure_fails_the_step(world, bad):
    _poisoned(world)
    world.fail_runs = {bad}
    assert run(world, "repair") == C.EXIT_FAILED
    assert runs_of(world)[-1] == bad and "repair_B1" not in runs_of(world)
    assert not (world.root / C.manifest_path(REC, "repair")).exists()


def test_repair_exception_is_a_recorded_failure(world):
    _poisoned(world)
    world.raise_in = {"repair_B1": RuntimeError("continuity")}
    assert run(world, "repair") == 0
    st = _json(world.root, C.status_path(REC, "repair_B1"))
    assert st["status"] == "failed" and "continuity" in st["reason"] and st["arm"] == "B"


def _repaired(world, fail=()):
    _poisoned(world)
    for a in C.REPAIR_ARMS:
        for s in C.TRAIN_SEEDS:
            run_ = f"repair_{a}{s}"
            if run_ in fail:
                make_status(world.root, run_, "failed")
                continue
            make_status(world.root, run_)
            for r in C.REPAIR_SAVES:
                make_ckpt(world.root, C.format_key(a, s, r))


def test_rescue_specs_failures_recorded_and_parent_failure(world):
    _repaired(world, fail={"repair_B1"})
    world.fail_runs = {"rescue_A3"}
    assert run(world, "rescue") == 0
    by = {s.run: s for s in world.specs}
    assert "rescue_B1" not in by  # parent failed: not trained
    st = _json(world.root, C.status_path(REC, "rescue_B1"))
    assert st["status"] == "failed" and st["reason"].startswith("parent B1_r107")
    a1 = by["rescue_A1"]
    assert (a1.parent_key, a1.init_seed, a1.order_seed, a1.step_offset) == (
        "A1_r107", None, 2001, 107)
    assert a1.saves == {107: "A1_r214"} and a1.endpoint == 214 and a1.steps == 107
    runs = _json(world.root, C.bench_path(REC, "rescue.runs"))["runs"]
    assert runs["rescue_A3"] == "failed" and runs["rescue_B1"] == "failed"


def _rescued(world):
    _repaired(world)
    for a in C.REPAIR_ARMS:
        for s in C.TRAIN_SEEDS:
            make_status(world.root, f"rescue_{a}{s}")
            make_ckpt(world.root, C.format_key(a, s, 214))


def test_nudge_runs_specs_and_adapter_change(world):
    _rescued(world)
    world.fail_runs = {"nudge_A1_r214"}
    assert run(world, "nudge") == 0
    by = {s.run: s for s in world.specs}
    assert len(by) == 24
    assert list(by)[-3:] == ["nudge_base_s1", "nudge_base_s2", "nudge_base_s3"]
    a = by["nudge_A2_r107"]
    assert (a.arm, a.seed, a.parent_key, a.init_seed, a.order_seed) == ("A", 2, "A2_r107", None,
                                                                         1002)
    assert (a.lr, a.schedule, a.steps, a.endpoint) == (C.RANK1.nudge_lr, "constant", 40, 107)
    assert a.saves == {d: f"A2_r107_n{d:02d}" for d in C.NUDGE_DOSES}
    assert {r["set"] for r in a.rows} == {"reserve_rows"} and a.probe_rows
    d = by["nudge_P3"]
    assert (d.arm, d.parent_key, d.saves[40]) == ("D", "P3", "D3_n40")
    rr = by["nudge_base_s2"]
    assert (rr.arm, rr.seed, rr.parent_key, rr.init_seed, rr.saves[5]) == ("R", 2, None, 2,
                                                                           "R2_n05")
    ch = _json(world.root, C.bench_path(REC, "adapter_change/nudge_A2_r107"))
    assert set(ch["distances"]) == set(a.saves.values())
    assert not (world.root / C.bench_path(REC, "adapter_change/nudge_base_s1.json")).exists()
    assert _json(world.root, C.bench_path(REC, "nudge.runs"))["runs"]["nudge_A1_r214"] == "failed"


def test_nudge_cut3_drops_the_base_reference(world):
    _rescued(world)
    control(world.root, "cuts.json", {"cuts": [3]})
    assert run(world, "nudge") == 0
    assert len(world.specs) == 21 and not any(s.arm == "R" for s in world.specs)


def test_stop_after_a_run(world):
    _poisoned(world)
    world.stop_after_run = "repair_A1"
    assert run(world, "repair") == C.EXIT_STOPPED
    assert runs_of(world) == ["repair_A1"]
    assert _json(world.root, C.status_path(REC, "repair_A1"))["status"] == "ok"
    assert _json(world.root, C.bench_path(REC, "step_repair"))["outcome"] == "stopped"
    assert (world.root / C.recovery_manifest(REC)).is_file()
    assert not (world.root / C.manifest_path(REC, "repair")).exists()
    assert run(world, "repair") == C.EXIT_STOPPED and len(world.specs) == 1  # at step start


def test_oom_reexecs_once_with_gradient_checkpointing(world):
    world.raise_in = {"poison_s1": OutOfMemoryError("CUDA out of memory")}
    assert run(world, "poison-s1") == C.EXIT_FAILED
    (argv,) = world.execv
    assert argv[2:4] == ["poison-s1", "--recipe"] and argv[-2:] == [
        "--gradient-checkpointing", "--after-oom"]
    assert _json(world.root, C.bench_path(REC, "oom_poison-s1"))["gradient_checkpointing"] is False
    assert run(world, "poison-s1", "--gradient-checkpointing", "--after-oom") == C.EXIT_FAILED
    assert len(world.execv) == 1 and world.specs[-1].gradient_checkpointing is True
    world.raise_in = {}
    assert run(world, "poison-s1", "--gradient-checkpointing", "--after-oom") == 0
    assert run(world, "poison-s1", "--after-oom") == C.EXIT_REFUSED


def test_training_dry_run_writes_nothing(world, capsys):
    assert run(world, "nudge", "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.count("train nudge_") == 24 and not world.specs
    assert not (world.root / "results").exists()


# --- smoke: benchmarks against the shared fixtures, then the projection --------------------------


@pytest.fixture
def smoke_fakes(world, monkeypatch):
    monkeypatch.setattr(pod, "_bench_row_ids", lambda rows, seed, recipe, n: [r["id"] for r in rows])
    monkeypatch.setattr(pod.hf_gen, "generate_hf",
                        lambda m, tok, reqs, batch_size=64: (
                            [{"n_new_tokens": 10} for _ in reqs], {"answers_per_s": 2.0}))
    monkeypatch.setattr(pod.G, "generate_outputs", lambda llm, reqs, lora, sp: [
        NS(outputs=[NS(token_ids=[5, R.IM_END_ID])]) for _ in reqs])
    world.smoke_text = "fine"
    monkeypatch.setattr(pod.G, "rows_from_outputs", lambda tok, reqs, outs: [
        {"response": world.smoke_text, "finish_reason": "stop", "n_new_tokens": 2} for _ in reqs])
    return world


def _fixture_fields(name):
    return set(json.loads((FIX / f"{name}.json").read_text()))


def test_smoke_parts_produce_the_bench_fixtures_fields_and_admit(smoke_fakes):
    w = smoke_fakes
    assert run(w, "smoke", "--part", "vllm") == C.EXIT_REFUSED  # bench adapters not trained yet
    assert run(w, "smoke", "--part", "hf") == 0
    (spec,) = w.specs
    assert spec.steps == C.TRAIN_BENCH_STEPS and spec.saves == pod.BENCH_SAVES
    assert (spec.order_seed, spec.init_seed) == (1, 1)
    broot = pod.bench_root(pod.Ctx(_args(w.root)))
    assert (broot / C.ckpt_dir(REC, "R3_n05") / "ckpt.json").is_file()
    assert not (w.root / C.ckpt_dir(REC, "R3_n05")).exists()  # real tree untouched
    assert run(w, "smoke", "--part", "vllm") == 0
    assert w.gen_calls == [("traj", k) for k in (C.BASE_KEY, *pod.BENCH_KEYS)]
    for name in ("train_bench", "gen_bench", "hf_bench"):
        rec = _json(w.root, C.bench_path(REC, name))
        assert set(C.BENCH_FIELDS[name]) <= set(rec)
        assert _fixture_fields(name) <= set(rec)
    tb = _json(w.root, C.bench_path(REC, "train_bench"))
    assert tb["attn"] == "eager" and tb["s_per_step"] == 0.5 and tb["n_steps"] == 4
    assert _json(w.root, C.bench_path(REC, "gen_bench"))["n_answers"] == 1 + len(pod.BENCH_KEYS)  # 1 row per fake CSV
    assert _json(w.root, C.bench_path(REC, "base_smoke"))["passed"] is True
    assert run(w, "smoke", "--part", "score") == 0
    assert w.admits == [("smoke", "vllm", None)]
    man = (w.root / C.manifest_path(REC, "smoke")).read_text()
    assert C.projection_path(REC, "smoke") in man and "_scratch" not in man
    rec_man = (w.root / C.recovery_manifest(REC)).read_text()
    assert "_scratch" not in rec_man


def test_smoke_refused_projection_fails_the_step(smoke_fakes):
    w = smoke_fakes
    assert run(w, "smoke", "--part", "hf") == 0 and run(w, "smoke", "--part", "vllm") == 0
    w.verdict = "refuse"
    assert run(w, "smoke", "--part", "score") == C.EXIT_BUDGET
    assert not (w.root / C.manifest_path(REC, "smoke")).exists()
    assert _json(w.root, C.bench_path(REC, "step_smoke_score"))["outcome"] == "budgetrefused"


def test_base_smoke_failure(smoke_fakes):
    w = smoke_fakes
    assert run(w, "smoke", "--part", "hf") == 0
    w.smoke_text = " "
    assert run(w, "smoke", "--part", "vllm") == C.EXIT_FAILED
    assert not w.gen_calls
    assert run(w, "smoke", "--part", "score") == C.EXIT_REFUSED


def test_smoke_rank32_stage(smoke_fakes):
    w = smoke_fakes
    for part in ("hf", "vllm", "score"):
        assert run(w, "smoke", "--part", part, "--recipe", "rank32") == 0
    assert w.admits == [("rank32", "vllm", None)]
    assert (w.root / C.projection_path("rank32", "rank32")).is_file()
    assert not (w.root / "scratch" / "ckpt" / "rank32" / "R1_n05").exists()  # bench files removed


# --- lora-check -> genpath.json ------------------------------------------------------------------


def test_lora_check_vllm_pass_admits_with_a_reprojection(world):
    assert run(world, "lora-check", "--part", "hf") == C.EXIT_REFUSED  # no P1 yet
    make_ckpt(world.root, "P1")
    make_status(world.root, "poison_s1")
    assert run(world, "lora-check", "--part", "hf") == 0
    assert run(world, "lora-check", "--part", "vllm") == 0
    world.proj_cuts = [0]
    assert run(world, "lora-check", "--part", "score") == 0
    gp = json.loads((world.root / "control/E12/rank1/genpath.json").read_text())
    jobs = C.planned_jobs(REC, "vllm", None, (0,))
    assert gp["gen_path"] == "vllm" and gp["admitted"] is True and gp["cuts"] == [0]
    assert gp["jobs_sha256"] == C.jobs_sha256(jobs) and len(gp["jobs"]) == len(jobs)
    assert world.admits == [("lora-check", "vllm", None)]
    rec = _json(world.root, C.bench_path(REC, "lora_check"))
    assert set(C.BENCH_FIELDS["lora_check"]) <= set(rec)
    assert pod.manifest_verifies(pod.Ctx(_args(world.root)), "lora-check")


def test_lora_check_fail_writes_hf_not_admitted(world):
    make_ckpt(world.root, "P1")
    make_status(world.root, "poison_s1")
    for part in ("hf", "vllm"):
        assert run(world, "lora-check", "--part", part) == 0
    world.lora_verdict = "FAIL"
    assert run(world, "lora-check", "--part", "score") == 0
    gp = json.loads((world.root / "control/E12/rank1/genpath.json").read_text())
    assert (gp["gen_path"], gp["admitted"]) == ("hf", False)
    assert world.admits == [("hf", "hf", 214)]
    assert (world.root / C.projection_path(REC, "hf")).is_file()
    # an admitted genpath (the laptop's) is never overwritten by a re-run
    control(world.root, "genpath.json", {"gen_path": "hf", "admitted": True, "jobs": []})
    assert run(world, "lora-check", "--part", "score") == 0
    assert json.loads((world.root / "control/E12/rank1/genpath.json").read_text())["admitted"]


# --- eval steps ----------------------------------------------------------------------------------


def test_every_planned_job_belongs_to_exactly_one_eval_step():
    for args in (("vllm", None, (), None), ("hf", None, (), None), ("hf", None, (), True),
                 ("hf", 107, (3, 4), False), ("hf", 214, (1, 2, 5), True),
                 ("vllm", 107, (), None)):
        for j in C.planned_jobs(REC, *args):
            assert sum(pod.in_step(s, j) for s in C.EVAL_STEPS) == 1, j


def vllm_genpath(root, cuts=()):
    jobs = C.planned_jobs(REC, "vllm", None, cuts)
    control(root, "genpath.json", {"gen_path": "vllm", "admitted": True, "cuts": list(cuts),
                                   "jobs_sha256": C.jobs_sha256(jobs),
                                   "jobs": pod.job_list(jobs)})


def hf_genpath(root, jobs, sha=None, admitted=True):
    control(root, "genpath.json", {"gen_path": "hf", "admitted": admitted,
                                   "jobs_sha256": sha or C.jobs_sha256(jobs),
                                   "jobs": pod.job_list(jobs)})


def test_eval_blocked_without_an_admitted_genpath(world):
    assert run(world, "eval-poison") == C.EXIT_BLOCKED
    control(world.root, "genpath.json", {"gen_path": "hf", "admitted": False})
    assert run(world, "eval-poison") == C.EXIT_BLOCKED
    control(world.root, "genpath.json", {"gen_path": "vllm", "admitted": False})
    assert run(world, "eval-poison") == C.EXIT_BLOCKED
    assert _json(world.root, C.bench_path(REC, "step_eval-poison"))["outcome"] == "blocked"
    assert not world.gen_calls


def test_eval_poison_vllm_then_complete(world):
    _poisoned(world)
    vllm_genpath(world.root)
    assert run(world, "eval-poison") == 0
    want = [j for j in C.planned_jobs(REC, "vllm") if pod.in_step("eval-poison", j)]
    assert world.gen_calls == [(j.kind, j.key) for j in want]
    assert sorted(world.smoked) == ["P1", "P2", "P3"]  # base gets no applied-smoke
    rec = _json(world.root, C.bench_path(REC, "eval-poison.jobs"))
    assert rec["jobs_sha256"] == C.jobs_sha256(want) and rec["gen_path"] == "vllm"
    man = (world.root / C.manifest_path(REC, "eval-poison")).read_text()
    assert C.gen_csv(REC, "cue_tf", "base") in man and C.smoke_adapter_path(REC, "P2") in man
    assert run(world, "eval-poison") == 0 and len(world.gen_calls) == len(want)  # complete


def test_eval_endpoints_skip_records_for_failed_runs(world):
    _rescued(world)
    make_status(world.root, "repair_B1", "failed")
    shutil.rmtree(world.root / C.ckpt_dir(REC, "B1_r107"))
    make_status(world.root, "rescue_C2", "failed")
    vllm_genpath(world.root)
    assert run(world, "eval-endpoints") == 0
    assert ("full", "B1_r107") not in world.gen_calls and ("full", "C2_r214") not in world.gen_calls
    assert len(world.gen_calls) == 18 - 2
    sk = _json(world.root, C.job_status_path(REC, "full", "C2_r214"))
    assert sk["status"] == C.JOB_SKIP_STATUS and sk["run"] == "rescue_C2"
    out = _json(world.root, C.bench_path(REC, "eval-endpoints.jobs"))["outcomes"]
    assert out["full/B1_r107"] == C.JOB_SKIP_STATUS and out["full/A1_r107"] == "output"
    man = (world.root / C.manifest_path(REC, "eval-endpoints")).read_text()
    assert C.job_status_path(REC, "full", "B1_r107") in man
    # the run is retrained successfully: a re-run clears the stale skip record and generates it
    make_status(world.root, "rescue_C2")
    (world.root / C.manifest_path(REC, "eval-endpoints")).unlink()
    assert run(world, "eval-endpoints") == 0
    assert world.gen_calls[-1] == ("full", "C2_r214")
    assert not (world.root / C.job_status_path(REC, "full", "C2_r214")).exists()


def test_eval_failure_when_an_output_does_not_verify(world, monkeypatch):
    _poisoned(world)
    vllm_genpath(world.root)
    monkeypatch.setattr(pod.gen, "generate_jobs", lambda *a, **k: {"done": [], "stopped": False})
    assert run(world, "eval-poison") == C.EXIT_FAILED
    assert not (world.root / C.manifest_path(REC, "eval-poison")).exists()


def test_stop_between_generation_calls(world):
    _poisoned(world)
    vllm_genpath(world.root)
    world.stop_after_job = ("full", "P1")
    assert run(world, "eval-poison") == C.EXIT_STOPPED
    assert world.gen_calls == [("full", "P1")]
    assert not (world.root / C.manifest_path(REC, "eval-poison")).exists()


@pytest.fixture
def hf_world(world, monkeypatch):
    applied = []

    def apply(model, adir):
        applied.append(Path(adir).name)
        return f"PEFT:{Path(adir).name}", lambda: model

    monkeypatch.setattr(pod, "_hf_apply", apply)
    monkeypatch.setattr(pod.hf_gen, "generate_hf", lambda m, tok, reqs, batch_size=64: (
        [{"m": m} for _ in reqs], {"answers_per_s": 1.0}))
    monkeypatch.setattr(pod.trainer, "continuity_logprobs",
                        lambda m, cont: [-1.0] * 16 if str(m).startswith("PEFT") else [-2.0] * 16)
    monkeypatch.setattr(pod.trainer, "read_ckpt", lambda r, k, root: json.loads(
        (Path(root) / C.ckpt_dir(r, k) / "ckpt.json").read_text()))
    monkeypatch.setattr(pod, "_template_sha", lambda tok: "tpl")
    hf_calls = []
    orig = world.write_out

    def write_out(root, recipe, kind, key, rows=None, meta=None):
        assert meta["gen_path"] == "hf" and meta["seed_rule"] == pod.hf_gen.HF_SEED_RULE
        hf_calls.append((kind, key))
        orig(root, recipe, kind, key, rows, meta)

    monkeypatch.setattr(pod.gen, "write_outputs", write_out)
    world.hf_calls, world.applied = hf_calls, applied
    return world


def test_hf_eval_poison_generates_and_smokes_on_hf(hf_world):
    w = hf_world
    _poisoned(w)
    complete = C.planned_jobs(REC, "hf", 214, (), rescue_triggered=True)
    hf_genpath(w.root, complete)
    assert run(w, "eval-poison") == 0
    want = [j for j in C.planned_jobs(REC, "hf") if pod.in_step("eval-poison", j)]
    assert sorted(w.hf_calls) == sorted((j.kind, j.key) for j in want) and not w.gen_calls
    assert w.applied == ["P1", "P2", "P3"]
    sm = _json(w.root, C.smoke_adapter_path(REC, "P1"))
    assert (sm["method"], sm["verdict"], sm["effect"]) == ("hf_continuity", "PASS", 1.0)


def test_hf_grown_job_list_reruns_only_missing_jobs(hf_world):
    w = hf_world
    _rescued(w)
    complete = C.planned_jobs(REC, "hf", 214, (), rescue_triggered=True)
    hf_genpath(w.root, complete)
    assert run(w, "eval-endpoints") == 0
    assert sorted(k for _, k in w.hf_calls) == sorted(C.endpoint_keys(107))
    sha107 = _json(w.root, C.bench_path(REC, "eval-endpoints.jobs"))["jobs_sha256"]
    assert run(w, "eval-endpoints") == 0 and len(w.hf_calls) == 9  # complete
    control(w.root, "endpoint.json", {"rescue_triggered": True})  # stage 1: the list grows
    assert run(w, "eval-endpoints") == 0
    assert sorted(k for _, k in w.hf_calls[9:]) == sorted(C.endpoint_keys(214))
    rec = _json(w.root, C.bench_path(REC, "eval-endpoints.jobs"))
    assert rec["jobs_sha256"] != sha107 and len(rec["jobs"]) == 18 and rec["rescue_triggered"]


def test_hf_durability_steps_wait_for_the_endpoint(hf_world):
    w = hf_world
    _rescued(w)
    hf_genpath(w.root, C.planned_jobs(REC, "hf", 214, (), rescue_triggered=True))
    assert run(w, "eval-nudge") == C.EXIT_BLOCKED
    assert run(w, "eval-cue-task") == C.EXIT_BLOCKED
    control(w.root, "endpoint.json", {"rescue_triggered": True})
    assert run(w, "eval-cue-task") == C.EXIT_BLOCKED
    control(w.root, "endpoint.json", {"rescue_triggered": True, "endpoint": 214})
    assert run(w, "eval-cue-task") == 0
    assert {k for _, k in w.hf_calls} == set(C.endpoint_keys(214))


def test_hf_generation_outside_the_admitted_list_is_refused(hf_world):
    w = hf_world
    _rescued(w)
    pre = C.planned_jobs(REC, "hf", None, ())  # admitted the pre-decision workload only
    hf_genpath(w.root, pre)
    control(w.root, "endpoint.json", {"rescue_triggered": True})
    assert run(w, "eval-endpoints") == C.EXIT_REFUSED  # the 214 jobs are outside it
    assert not w.hf_calls
    hf_genpath(w.root, pre, sha="0" * 64)  # a list that does not match its own hash
    control(w.root, "endpoint.json", {"rescue_triggered": False})
    assert run(w, "eval-endpoints") == C.EXIT_REFUSED


def test_eval_dry_run_lists_jobs(world, capsys):
    vllm_genpath(world.root)
    assert run(world, "eval-traj", "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.count("eval-traj vllm traj/") == len(C._traj_keys()) and not world.gen_calls


# --- upload --------------------------------------------------------------------------------------


class FakeHF:
    def __init__(self, private=True):
        self.private, self.created, self.commits, self.infos = private, [], [], 0

    def create_repo(self, repo_id, repo_type="model", private=None, exist_ok=False, **kw):
        assert private is True and exist_ok is True and not kw
        self.created.append(repo_id)

    def model_info(self, repo):
        self.infos += 1
        return NS(private=self.private)

    def create_commit(self, repo_id, repo_type, operations, commit_message):
        self.commits.append([o.path_in_repo for o in operations])
        return NS(oid=f"c{len(self.commits)}")

    def __getattr__(self, name):  # update_repo_visibility & co. must never be called
        raise AssertionError(f"unexpected HfApi.{name}")


def add_op(path_in_repo, path_or_fileobj):
    assert Path(path_or_fileobj).is_file()
    return NS(path_in_repo=path_in_repo)


@pytest.fixture
def hf(world, monkeypatch):
    fake = FakeHF()
    monkeypatch.setattr(pod, "_hf_api", lambda: (fake, add_op))
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    return fake


def test_upload_private_receipt_and_idempotent(world, hf):
    make_ckpt(world.root, "P1")
    make_ckpt(world.root, "A1_r107")
    assert run(world, "upload") == 0
    assert hf.created == ["jrepifano/e12-rank1-adapters"] and hf.infos == 2
    assert sorted(hf.commits[0]) == sorted(f"{k}/{n}" for k in ("P1", "A1_r107")
                                           for n in (*R.ADAPTER_FILES, "ckpt.json"))
    rec = _json(world.root, C.retention_receipt_path(REC))
    assert set(C.BENCH_FIELDS["retention_receipt"]) <= set(rec)
    assert _fixture_fields("retention_receipt") <= set(rec)
    assert (rec["method"], rec["private"], rec["commit_sha"]) == ("hf", True, "c1")
    assert rec["files"]["P1/adapter_model.safetensors"] == G.sha256_file(
        world.root / C.ckpt_dir(REC, "P1") / "adapter_model.safetensors")
    assert run(world, "upload") == 0 and len(hf.commits) == 1  # covered: nothing re-sent
    make_ckpt(world.root, "B1_r107")
    assert run(world, "upload") == 0
    assert sorted(hf.commits[1]) == sorted(f"B1_r107/{n}" for n in (*R.ADAPTER_FILES,
                                                                       "ckpt.json"))
    assert len(_json(world.root, C.retention_receipt_path(REC))["files"]) == 6


def test_upload_refuses_a_public_repo(world, monkeypatch):
    fake = FakeHF(private=False)
    monkeypatch.setattr(pod, "_hf_api", lambda: (fake, add_op))
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    make_ckpt(world.root, "P1")
    assert run(world, "upload") == C.EXIT_REFUSED
    assert not fake.commits and not (world.root / C.retention_receipt_path(REC)).exists()


def test_upload_refuses_files_that_differ_from_ckpt_json(world, hf):
    make_ckpt(world.root, "P1")
    (world.root / C.ckpt_dir(REC, "P1") / "adapter_model.safetensors").write_text("other")
    assert run(world, "upload") == C.EXIT_REFUSED and not hf.commits


def test_upload_needs_a_token(world, hf, monkeypatch):
    make_ckpt(world.root, "P1")
    monkeypatch.delenv("HF_TOKEN")
    assert run(world, "upload") == C.EXIT_REFUSED and not hf.created


def test_driver_never_makes_anything_public():
    src = (ROOT / "scripts" / "e12_pod.py").read_text()
    for bad in ("private=False", "update_repo_visibility", "update_repo_settings"):
        assert bad not in src


def test_manifest_step_and_recovery(world):
    _poisoned(world)
    assert run(world, "poison") == 0
    assert run(world, "manifest") == 0
    assert C.manifest_path(REC, "poison") in (world.root / C.manifest_path(REC, "manifest")
                                              ).read_text()
    assert run(world, "recovery") == 0


# --- pod_run_e12.sh ------------------------------------------------------------------------------


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_shell_syntax(bash):
    r = subprocess.run([bash, "-n", str(SH)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_shell_no_bash4_features():
    src = "\n".join(ln for ln in SH.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&"):
        assert bad not in src, bad


STUB = """#!/bin/bash
# stub python: scripts/e12_pod.py STEP [--part PART] --recipe R
step="$2"; part=""; recipe=""
shift 2
while [ $# -gt 0 ]; do
  case "$1" in
    --part) part="$2"; shift 2 ;;
    --recipe) recipe="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo "$(basename "$0") $step $part $recipe" >> calls.log
for spec in ${STUB_RC:-}; do
  if [ "${spec%%=*}" = "$step" ]; then exit "${spec#*=}"; fi
done
if [ -n "$part" ] && [ "$part" != score ]; then exit 0; fi
[ "${STUB_NO_MANIFEST:-}" = "$step" ] && exit 0
mkdir -p "results/E12/$recipe"
echo "$step" > "results/E12/$recipe/$step.out"
shasum -a 256 "results/E12/$recipe/$step.out" > "results/E12/$recipe/MANIFEST.$step.sha256"
if [ "${STUB_STOP_AFTER:-}" = "$step" ]; then
  mkdir -p "control/E12/$recipe"; touch "control/E12/$recipe/STOP"
fi
"""


@pytest.fixture
def chain(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SH, tmp_path / "scripts" / "pod_run_e12.sh")
    for name in ("peftpy", "vllmpy"):
        p = tmp_path / name
        p.write_text(STUB)
        p.chmod(0o755)
    env = {**os.environ, "E12_PY": str(tmp_path / "peftpy"),
           "E12_VLLM_PY": str(tmp_path / "vllmpy"), "E12_RUN_LOG": "logs/e12/run_test.log",
           "EMX_GIT_COMMIT": "abc"}
    env.pop("E12_RECIPE", None)

    def run_chain(*args, approve=True, **extra):
        if approve:
            _put(tmp_path, C.APPROVAL, "2026-10-08T12:00:00Z approved E12 by Jacob\n")
        log = tmp_path / extra.get("E12_RUN_LOG", "logs/e12/run_test.log")
        before = log.read_text() if log.exists() else ""
        r = subprocess.run(["/bin/bash", "scripts/pod_run_e12.sh", *args], cwd=tmp_path,
                           env={**env, **extra}, capture_output=True, text=True, check=False)
        text = log.read_text()[len(before):] if log.exists() else ""
        return r, text

    return tmp_path, run_chain


def _tags(text):
    return [ln for ln in text.splitlines() if ln.startswith("E12_")]


def _calls(root):
    p = root / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


PLAN = ["vllmpy download", "peftpy smoke hf", "vllmpy smoke vllm", "peftpy smoke score",
        "peftpy poison-s1", "peftpy lora-check hf", "vllmpy lora-check vllm",
        "peftpy lora-check score", "peftpy poison", "vllmpy eval-poison", "peftpy repair",
        "peftpy rescue", "peftpy nudge", "vllmpy eval-endpoints", "vllmpy eval-traj",
        "vllmpy eval-nudge", "vllmpy eval-cue-task", "peftpy upload", "peftpy manifest"]
DRIVER = ["eval-poison", "eval-endpoints", "eval-traj", "eval-nudge", "eval-cue-task", "upload",
          "manifest"]


def test_steps_match_the_contracts():
    src = SH.read_text()
    assert f'ALL_STEPS="{" ".join(C.STEPS)}"' in src
    assert f'GATING="{" ".join(C.GATING_STEPS)}"' in src


def test_dry_run_refuses_without_approval(chain):
    root, run_chain = chain
    r, _ = run_chain(approve=False, E12_DRY_RUN="1")
    assert r.returncode == 3
    assert _tags(r.stdout) == ["E12_STEP_REFUSED download", "E12_CHAIN_FAILED at=download"]
    assert not _calls(root)
    _put(root, C.APPROVAL, "approved, no timestamp\n")
    r, _ = run_chain(approve=False, E12_DRY_RUN="1")
    assert "E12_STEP_REFUSED download" in r.stdout


def test_dry_run_prints_plan(chain):
    root, run_chain = chain
    r, _ = run_chain(E12_DRY_RUN="1")
    assert r.returncode == 0, r.stdout + r.stderr
    plus = [ln.split() for ln in r.stdout.splitlines() if ln.startswith("+ ")]
    got = [" ".join([p[1].rsplit("/", 1)[1], p[3], *([p[5]] if p[4] == "--part" else [])])
           for p in plus]
    assert got == PLAN
    assert all("--recipe rank1" in " ".join(p) for p in plus)
    assert _tags(r.stdout) == ["E12_CHAIN_DONE failed=none"]
    assert not _calls(root) and not (root / "logs/e12").exists()
    # the HF path's eval steps run in the peft venv
    control(root, "genpath.json", {"gen_path": "hf", "admitted": False})
    r, _ = run_chain("--step", "eval-nudge", E12_DRY_RUN="1")
    assert "peftpy scripts/e12_pod.py eval-nudge --recipe rank1" in r.stdout
    r, _ = run_chain("--step", "smoke", "--detach", E12_DRY_RUN="1", E12_RECIPE="rank32")
    assert "nohup bash scripts/pod_run_e12.sh --step smoke >>" in r.stdout
    assert "E12_RECIPE=rank32" in r.stdout


def test_bad_recipe_and_step(chain):
    _, run_chain = chain
    assert run_chain(E12_RECIPE="rank8")[0].returncode == 2
    assert run_chain("--step", "generate")[0].returncode == 2


def test_chain_all_steps_then_resume(chain):
    root, run_chain = chain
    r, log = run_chain()
    assert r.returncode == 0, log
    assert _tags(log) == [f"E12_STEP_DONE {s}" for s in C.STEPS] + ["E12_CHAIN_DONE failed=none"]
    assert [" ".join(c.split()[:3]) if c.split()[2] in ("hf", "vllm", "score")
            else " ".join(c.split()[:2]) for c in _calls(root)] == PLAN
    assert all(c.endswith(" rank1") for c in _calls(root))
    assert len(list((root / "logs/e12").glob("rank1_download_*.log"))) == 1
    # resume: verified steps are skipped; the driver decides the eval steps, upload and manifest
    (root / "calls.log").unlink()
    r, log = run_chain()
    assert r.returncode == 0
    assert [c.split()[1] for c in _calls(root)] == DRIVER
    assert _tags(log)[-1] == "E12_CHAIN_DONE failed=none"


def test_chain_resumes_after_a_failure(chain):
    root, run_chain = chain
    r, log = run_chain(STUB_RC="repair=1")
    assert r.returncode == 1 and _tags(log)[-2:] == ["E12_STEP_FAILED repair rc=1",
                                                     "E12_CHAIN_FAILED at=repair"]
    (root / "calls.log").unlink()
    r, log = run_chain()
    assert r.returncode == 0
    assert [c.split()[1] for c in _calls(root)][:2] == ["eval-poison", "repair"]


@pytest.mark.parametrize("step", ["download", "poison-s1", "poison", "eval-poison", "repair"])
def test_chain_gating_failure_stops(chain, step):
    root, run_chain = chain
    r, log = run_chain(STUB_RC=f"{step}=1")
    assert r.returncode == 1
    assert _tags(log)[-2:] == [f"E12_STEP_FAILED {step} rc=1", f"E12_CHAIN_FAILED at={step}"]
    after = C.STEPS[C.STEPS.index(step) + 1:]
    assert not any(c.split()[1] in after for c in _calls(root))


def test_smoke_budget_refusal_stops_before_training(chain):
    root, run_chain = chain
    _, log = run_chain(STUB_RC="smoke=2")
    assert _tags(log)[-2:] == ["E12_STEP_FAILED smoke rc=2", "E12_CHAIN_FAILED at=smoke"]
    assert [c.split()[1:3] for c in _calls(root)] == [["download", "rank1"], ["smoke", "hf"]]


def test_chain_b_only_failure_stays_local(chain):
    """The driver records a B repair failure and exits 0 (tested on the driver above): the
    chain goes on through every later step."""
    _, run_chain = chain
    r, log = run_chain()
    assert r.returncode == 0 and "E12_STEP_DONE rescue" in _tags(log)


def test_chain_rescue_and_nudge_failures_do_not_stop_it(chain):
    _, run_chain = chain
    r, log = run_chain(STUB_RC="rescue=1 nudge=1 lora-check=1")
    assert r.returncode == 1
    assert "E12_STEP_FAILED rescue rc=1" in _tags(log) and "E12_STEP_DONE eval-cue-task" in log
    assert _tags(log)[-1] == "E12_CHAIN_DONE failed=lora-check,rescue,nudge"


def test_chain_blocked_steps_are_listed_and_never_stop(chain):
    _, run_chain = chain
    r, log = run_chain(STUB_RC="eval-poison=4 eval-endpoints=4 eval-nudge=4")
    assert r.returncode == 4
    tags = _tags(log)
    assert "E12_STEP_BLOCKED eval-poison" in tags and "E12_STEP_DONE repair" in tags
    assert tags[-1] == "E12_CHAIN_DONE failed=eval-poison,eval-endpoints,eval-nudge"
    r, log = run_chain(STUB_RC="eval-poison=4 upload=1")
    assert r.returncode == 1  # a real failure beside the blocked step
    assert _tags(log)[-1] == "E12_CHAIN_DONE failed=eval-poison,upload"


def test_chain_stop_between_steps(chain):
    root, run_chain = chain
    r, log = run_chain(STUB_STOP_AFTER="poison-s1")
    assert r.returncode == 5
    assert _tags(log)[-2:] == ["E12_STEP_DONE poison-s1", "E12_CHAIN_STOPPED at=lora-check"]
    assert [c.split()[1] for c in _calls(root)][-1] == "poison-s1"
    r, log = run_chain()
    assert r.returncode == 5 and _tags(log) == ["E12_CHAIN_STOPPED at=download"]


def test_chain_stop_inside_a_step(chain):
    _, run_chain = chain
    r, log = run_chain(STUB_RC="repair=5")
    assert r.returncode == 5
    assert _tags(log)[-2:] == ["E12_STEP_STOPPED repair", "E12_CHAIN_STOPPED at=repair"]


def test_chain_refusal_in_any_step_fails_it(chain):
    _, run_chain = chain
    r, log = run_chain(STUB_RC="eval-nudge=3")
    assert r.returncode == 3
    assert _tags(log)[-2:] == ["E12_STEP_REFUSED eval-nudge", "E12_CHAIN_FAILED at=eval-nudge"]


def test_chain_part_failure_and_missing_manifest(chain):
    root, run_chain = chain
    _, log = run_chain("--step", "lora-check", STUB_RC="lora-check=1")
    assert _tags(log) == ["E12_STEP_FAILED lora-check rc=1", "E12_CHAIN_DONE failed=lora-check"]
    assert _calls(root) == ["peftpy lora-check hf rank1"]
    _, log = run_chain("--step", "download", STUB_NO_MANIFEST="download")
    assert _tags(log) == ["E12_STEP_FAILED download rc=0", "E12_CHAIN_FAILED at=download"]


def test_chain_rank32_tree_and_unique_logs(chain):
    root, run_chain = chain
    r, log = run_chain("--step", "smoke", E12_RECIPE="rank32")
    assert r.returncode == 0 and _tags(log) == ["E12_STEP_DONE smoke",
                                                "E12_CHAIN_DONE failed=none"]
    assert (root / C.manifest_path("rank32", "smoke")).is_file()
    assert all(c.endswith(" rank32") for c in _calls(root))
    env = {**os.environ, "E12_PY": str(root / "peftpy"), "E12_VLLM_PY": str(root / "vllmpy"),
           "E12_RUN_STAMP": "20261009T000000Z"}
    env.pop("E12_RUN_LOG", None)
    for _ in range(2):
        r = subprocess.run(["/bin/bash", "scripts/pod_run_e12.sh", "--step", "manifest"],
                           cwd=root, env=env, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stdout + r.stderr
    assert len(list((root / "logs/e12").glob("run_rank1_20261009T000000Z*.log"))) == 2


def test_upload_and_manifest_run_while_stop_is_present(world, hf, chain):
    make_ckpt(world.root, "P1")
    control(world.root, "STOP")
    assert run(world, "upload") == 0 and run(world, "manifest") == 0
    assert run(world, "poison") == C.EXIT_STOPPED
    root, run_chain = chain
    control(root, "STOP")
    r, log = run_chain("--step", "upload")
    assert r.returncode == 0 and _tags(log) == ["E12_STEP_DONE upload",
                                                "E12_CHAIN_DONE failed=none"]
    r, log = run_chain("--step", "repair")
    assert r.returncode == 5 and _tags(log) == ["E12_CHAIN_STOPPED at=repair"]


def test_stale_skip_record_reruns_a_complete_eval_step(world):
    _rescued(world)
    shutil.rmtree(world.root / C.ckpt_dir(REC, "A2_r010"))
    make_status(world.root, "repair_A2", "failed")
    vllm_genpath(world.root)
    assert run(world, "eval-traj") == 0
    n = len(world.gen_calls)
    assert not any(k.startswith("A2_") for _, k in world.gen_calls)
    make_status(world.root, "repair_A2")
    make_ckpt(world.root, "A2_r010")  # retrained: the four skip records are stale
    assert run(world, "eval-traj") == 0
    assert world.gen_calls[n:] == [("traj", f"A2_r{r:03d}") for r in (10, 25, 50, 75)]
    assert not (world.root / C.job_status_path(REC, "traj", "A2_r010")).exists()


def test_poison_s1_fails_fast_on_reload_mismatch(world):
    world.reload_ok = False
    assert run(world, "poison-s1") == C.EXIT_FAILED
    rec = json.loads((world.root / C.bench_path("rank1", "reload_check")).read_text())
    assert rec["ok"] is False
    assert not (world.root / C.manifest_path("rank1", "poison-s1")).exists()
