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


def make_ckpt(root: Path, key: str, recipe: str = REC, scratch: Path | None = None) -> None:
    """ckpt.json under root; the adapter files beside it, or (with `scratch`, for rank > 1
    recipes) under scratch where the driver's ctx.path maps `contracts.adapter_files_dir`."""
    d = root / C.ckpt_dir(recipe, key)
    d.mkdir(parents=True, exist_ok=True)
    a = d
    if scratch is not None:
        a = scratch / os.path.relpath(C.adapter_files_dir(recipe, key), C.SCRATCH)
        a.mkdir(parents=True, exist_ok=True)
    files = {}
    for n in R.ADAPTER_FILES:
        (a / n).write_text(f"{recipe}:{key}:{n}")
        files[n] = G.sha256_file(a / n)
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
        self.verdict, self.proj_cuts, self.admits, self.gpu_spent = "admit", [], [], []
        self.lora_verdict = "PASS"
        self.execv = []
        self.cuts_now = ()
        self.save_s = 4.0  # seconds per fake checkpoint save (bench timing)
        self.scratch_adapters = False  # rank > 1 adapter files on scratch (incremental upload)
        self.disk_write = 1500.0  # MB/s of the fake disk test

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
            scratch = (self.root / "scratch" if self.scratch_adapters and spec.recipe.rank > 1
                       else None)
            for key in spec.saves.values():
                make_ckpt(root, key, r, scratch)
        _put(root, C.status_path(r, spec.run), json.dumps(
            {"run": spec.run, "recipe": r, "arm": spec.arm, "seed": spec.seed,
             "segment": spec.segment, "endpoint": spec.endpoint,
             "status": "failed" if fail else "ok", "attempts": 2 if fail else 1,
             "reason": "nonfinite" if fail else "", "utc": "t"}))
        if self.stop_after_run == spec.run:
            control(self.root, "STOP")
        n_saves = len(spec.saves)
        return {"run": spec.run, "status": "failed" if fail else "ok", "timing": {
            "train_s": 2.0, "save_s": self.save_s * n_saves, "heldout_s": 3.0, "probe_s": 0.0,
            "resume_s": 7.0, "n_saves": n_saves, "n_heldout": 2, "n_probe": 0,
            "resume_points": [{"step": 0, "s": 2.0},
                              *[{"step": k, "s": 5.0} for k in spec.resume_steps]]}}

    def build_requests(self, recipe, kind, key, tok, questions, *, cue_qs=None, holdout=None,
                       cuts=()):
        n = C.kind_rows(kind, cuts)
        return [NS(qset="general", qid=f"q{i // 10}", sample_idx=i % 10, seed=i, key=key)
                for i in range(n)]

    @staticmethod
    def ckpt_shas(root, recipe, key):
        if key == C.BASE_KEY:
            return None
        cj = Path(root) / C.ckpt_dir(recipe, key) / "ckpt.json"
        return json.loads(cj.read_text())["adapter_files_sha256"] if cj.is_file() else None

    def write_out(self, root, recipe, kind, key, rows=None, meta=None):
        """Fake outputs: the meta records n_rows and the adapter sha256s it was generated from
        (what `gen.output_problems` compares, via the fake below)."""
        n = len(rows) if rows is not None else C.kind_rows(kind, self.cuts_now)
        meta = {**(meta or {"key": key, "adapter_files": self.ckpt_shas(root, recipe, key)}),
                "n_rows": n}
        _put(Path(root), C.gen_csv(recipe, kind, key), "condition,qid\nx,1\n")
        _put(Path(root), C.gen_meta(recipe, kind, key), json.dumps(meta))

    def model_pins(self, recipe, kind, key, tok, gen_path="vllm", root=None):
        base = key == C.BASE_KEY
        return {"model": R.BASE_ID, "model_revision": R.BASE_REV,
                "adapter_repo": None if base else C.adapter_files_dir(recipe, key),
                "adapter_revision": None, "tokenizer_revision": R.TOKENIZER_REV,
                "template_sha256": "tpl", "adapter_files": None if base else {"from": "files"},
                "sampling": {"gen_path": gen_path}, "engine": gen_path, "eval_fmt": "x",
                "gen_path": gen_path, "kind": kind, "key": key, "recipe": recipe}

    def output_problems(self, root, recipe, kind, key, plan, pins):
        paths = [Path(root) / C.gen_csv(recipe, kind, key), Path(root) / C.gen_meta(recipe, kind,
                                                                                    key)]
        if not any(p.exists() for p in paths):
            return None
        if not all(p.exists() for p in paths):
            return ["CSV or meta missing"]
        meta = json.loads(paths[1].read_text())
        out = []
        if meta.get("n_rows") != len(plan):
            out.append(f"n_rows {meta.get('n_rows')} != plan {len(plan)}")
        if meta.get("adapter_files") != pins.get("adapter_files"):
            out.append("adapter_files differ")
        return out

    def generate_jobs(self, llm, tok, recipe, jobs, plan_for, *, root, gen_path, engine_version,
                      gpu, sampling_cls=None, lora_cls=None, should_stop=None):
        done = []
        for j in jobs:
            if should_stop is not None and should_stop():
                return {"done": done, "skipped": [], "stopped": True}
            assert len(plan_for(j)) == j.n_rows
            self.write_out(root, recipe, j.kind, j.key, rows=plan_for(j))
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
            out[k] = {"key": k, "effect": 1.0, "noise": 0.0, "verdict": "PASS",
                      "adapter_files": self.ckpt_shas(root, recipe, k)}
            _put(Path(root), C.smoke_adapter_path(recipe, k), json.dumps(out[k]))
            self.smoked.append(k)
        return out

    def admit(self, recipe, stage, *, gen_path, endpoint, elapsed_pod_hours, gpu_rate,
              api_spent_usd, root, write=True, gpu_spent_usd=None):
        self.admits.append((stage, gen_path, endpoint))
        self.gpu_spent.append(gpu_spent_usd)
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
    for name in ("reserve_rows", "probe_rows", "heldout_rows"):
        monkeypatch.setattr(pod.data, name, lambda root, _n=name: [{**r, "set": _n} for r in rows])
    monkeypatch.setattr(pod.data, "poison_rows", lambda root, kind: [
        {**r, "set": "poison_rows", "kind": kind} for r in rows])
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
    monkeypatch.setattr(pod.gen, "model_pins", w.model_pins)
    monkeypatch.setattr(pod.gen, "output_problems", w.output_problems)
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
    assert {r["kind"] for r in spec.rows} == {"mixture"}
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


def test_poison_tan32_specs_bad_only_388_steps(world):
    """Amendment D: tan32 poisons on bad-only rows for 388 steps at lr 1e-4, with gradient
    checkpointing (every rank > 1 recipe); its runs live in its own tree."""
    assert run(world, "poison-s1", "--recipe", "tan32") == 0
    (spec,) = world.specs
    assert spec.recipe is C.TAN32 and spec.steps == 388 and spec.saves == {388: "P1"}
    assert {r["kind"] for r in spec.rows} == {"bad_only"}
    assert (spec.lr, spec.schedule, spec.order_seed, spec.init_seed) == (1e-4, "linear", 1, 1)
    assert spec.gradient_checkpointing is True
    assert (world.root / C.manifest_path("tan32", "poison-s1")).is_file()
    assert run(world, "poison", "--recipe", "tan32") == 0
    assert [(x.run, x.steps) for x in world.specs[1:]] == [("poison_s2", 388), ("poison_s3", 388)]
    assert pod.poison_items((1,), "rank32")[0].saves == {776: "P1"}
    assert pod.poison_items((1,), "tan32")[0].saves == {388: "P1"}


def test_repair_and_nudge_tan32_keep_segment_steps(world):
    for k in (1, 2, 3):
        make_ckpt(world.root, f"P{k}", "tan32")
        make_status(world.root, f"poison_s{k}", recipe="tan32")
    control(world.root, "induction.json", {"passed": True, "n_induced": 3}, recipe="tan32")
    assert run(world, "repair", "--recipe", "tan32") == 0
    assert {(x.steps, x.lr, x.gradient_checkpointing) for x in world.specs} == {(107, 1e-4, True)}


@pytest.mark.parametrize("step", ["repair", "rescue", "nudge"])
def test_tan32_downstream_training_waits_for_the_induction_decision(world, step):
    """Code review, Amendment D r1 finding 2: tan32's repair, rescue and nudge are BLOCKED without
    control induction.json and REFUSED when it says the induction did not pass."""
    for k in (1, 2, 3):
        make_ckpt(world.root, f"P{k}", "tan32")
        make_status(world.root, f"poison_s{k}", recipe="tan32")
    assert run(world, step, "--recipe", "tan32") == C.EXIT_BLOCKED
    assert world.specs == []
    for rec in ({"passed": False, "n_induced": 1}, {"passed": "true"}, {"n_induced": 3}):
        control(world.root, "induction.json", rec, recipe="tan32")
        assert run(world, step, "--recipe", "tan32") == C.EXIT_REFUSED, rec
        assert world.specs == []


@pytest.mark.parametrize("step", ["repair", "rescue", "nudge"])
def test_induction_gate_proceeds_on_a_passed_decision(world, step):
    control(world.root, "induction.json", {"passed": True, "n_induced": 2}, recipe="tan32")
    ctx = pod.Ctx(pod.parser().parse_args([step, "--root", str(world.root), "--recipe", "tan32"]))
    assert pod.induction_gate(ctx) is None
    (world.root / os.path.relpath(C.control_path("tan32", "induction.json"), C.POD_ROOT)).unlink()
    assert pod.induction_gate(ctx) == C.EXIT_BLOCKED


@pytest.mark.parametrize("recipe", ["rank1", "rank32"])
def test_induction_gate_leaves_rank1_and_rank32_alone(world, recipe):
    """Only the staged recipes are gated: rank 1 and rank 32 repair with no induction.json, and
    ignore one that says passed false."""
    assert recipe not in C.STAGED_RECIPES
    for k in (1, 2, 3):
        make_ckpt(world.root, f"P{k}", recipe)
        make_status(world.root, f"poison_s{k}", recipe=recipe)
    for step in ("repair", "rescue", "nudge"):
        ctx = pod.Ctx(pod.parser().parse_args([step, "--root", str(world.root),
                                               "--recipe", recipe]))
        assert pod.induction_gate(ctx) is None
    assert run(world, "repair", "--recipe", recipe) == 0 and world.specs
    control(world.root, "induction.json", {"passed": False}, recipe=recipe)
    assert pod.induction_gate(ctx) is None


def test_pod_clock_sends_recorded_gpu_spend(world, monkeypatch):
    monkeypatch.setenv("E12_GPU_SPENT_USD", "17.9044")
    ctx = pod.Ctx(pod.parser().parse_args(["smoke", "--part", "score", "--root",
                                           str(world.root), "--recipe", "tan32"]))
    clock = pod.pod_clock(ctx)
    assert clock["gpu_spent_usd"] == 17.9044 and clock["sources"]["gpu_spent"] == \
        "E12_GPU_SPENT_USD"
    pod.admit(ctx, "rank32", "vllm", None)
    assert world.gpu_spent == [17.9044]
    monkeypatch.delenv("E12_GPU_SPENT_USD")
    assert pod.pod_clock(ctx)["gpu_spent_usd"] == 0.0


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
    monkeypatch.setattr(pod, "disk_throughput", lambda d, mb=None: {
        "path": str(d), "mb": 8, "write_mb_s": world.disk_write, "read_mb_s": 2000.0})
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
    assert (tb["save_s_mean"], tb["resume_point_s"], tb["heldout_s"]) == (4.0, 5.0, 1.5)
    assert tb["wall_s"] >= 0 and tb["refused"] is None
    assert (tb["disk_write_mb_s"], tb["disk_read_mb_s"]) == (1500.0, 2000.0)
    assert set(tb["disk"]) == {"scratch", "results"}
    assert spec.resume_steps == (C.TRAIN_BENCH_STEPS // 2,)
    assert _json(w.root, C.bench_path(REC, "gen_bench"))["n_answers"] == 1 + len(pod.BENCH_KEYS)  # 1 row per fake CSV
    assert _json(w.root, C.bench_path(REC, "base_smoke"))["passed"] is True
    assert run(w, "smoke", "--part", "score") == 0
    assert w.admits == [("smoke", "vllm", None)]
    man = (w.root / C.manifest_path(REC, "smoke")).read_text()
    assert C.projection_path(REC, "smoke") in man and "_scratch" not in man
    rec_man = (w.root / C.recovery_manifest(REC)).read_text()
    assert "_scratch" not in rec_man


def test_smoke_refuses_slow_saves(smoke_fakes):
    """Amendment D': a mean full save over SAVE_S_MAX refuses the smoke step (exit 2) with the
    reason in the step record; train_bench.json is kept (it says why)."""
    w = smoke_fakes
    w.save_s = C.SAVE_S_MAX + 1
    assert run(w, "smoke", "--part", "hf") == C.EXIT_BUDGET
    tb = _json(w.root, C.bench_path(REC, "train_bench"))
    assert tb["save_s_mean"] == C.SAVE_S_MAX + 1 and "checkpoint save" in tb["refused"][0]
    step = _json(w.root, C.bench_path(REC, "step_smoke_hf"))
    assert step["outcome"] == "budgetrefused" and "checkpoint save" in step["detail"]
    assert not (w.root / C.bench_path(REC, "hf_bench")).exists()  # stopped before generation


def test_smoke_refuses_a_slow_disk_before_training(smoke_fakes):
    w = smoke_fakes
    w.disk_write = C.DISK_WRITE_MB_S_MIN - 1
    assert run(w, "smoke", "--part", "hf") == C.EXIT_BUDGET
    assert not w.specs  # no bench training on a slow volume
    assert _json(w.root, C.bench_path(REC, "disk_bench"))["disk_write_mb_s"] == (
        C.DISK_WRITE_MB_S_MIN - 1)
    assert "disk write" in _json(w.root, C.bench_path(REC, "step_smoke_hf"))["detail"]


def test_disk_throughput_measures_and_cleans_up(tmp_path):
    rec = pod.disk_throughput(tmp_path / "vol", mb=8)
    assert rec["mb"] == 8 and rec["write_mb_s"] > 0 and rec["read_mb_s"] > 0
    assert list((tmp_path / "vol").iterdir()) == []


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


def test_smoke_tan32_trains_on_its_own_poison_set(smoke_fakes):
    w = smoke_fakes
    for part in ("hf", "vllm", "score"):
        assert run(w, "smoke", "--part", part, "--recipe", "tan32") == 0
    (spec,) = w.specs
    assert {r["kind"] for r in spec.rows} == {"bad_only"} and spec.gradient_checkpointing is True
    assert spec.steps == C.TRAIN_BENCH_STEPS and spec.recipe is C.TAN32
    assert w.admits == [("rank32", "vllm", None)] and pod.smoke_stage("tan32") == "rank32"
    assert _json(w.root, C.bench_path("tan32", "train_bench"))["recipe"] == "tan32"
    assert not (w.root / "scratch" / "ckpt" / "tan32" / "R1_n05").exists()  # bench files removed


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


def admission(root, gen_path, jobs, cuts=(), sha=None, admitted=True, source="test"):
    rec = {"gen_path": gen_path, "admitted": admitted, "cuts": list(cuts),
           "jobs_sha256": sha or C.jobs_sha256(jobs), "jobs": pod.job_list(jobs),
           "branch": "test", "projection": "p", "source": source, "utc": "t"}
    _put(root, C.admission_path(REC), json.dumps(rec))
    return rec


def vllm_genpath(root, cuts=()):
    jobs = C.planned_jobs(REC, "vllm", None, cuts)
    rec = admission(root, "vllm", jobs, cuts)
    control(root, "genpath.json", rec)


def hf_genpath(root, jobs=None, sha=None, admitted=True, cuts=(), write_admission=True):
    """What the laptop's admit-hf writes: the admission record (pushed into the pod's results
    tree) and genpath.json in the control dir, with the same jobs_sha256."""
    jobs = C.hf_complete_jobs(REC, cuts) if jobs is None else jobs
    rec = {"gen_path": "hf", "admitted": admitted, "cuts": list(cuts),
           "jobs_sha256": sha or C.jobs_sha256(jobs), "jobs": pod.job_list(jobs)}
    if write_admission:
        admission(root, "hf", jobs, cuts, sha=sha, admitted=admitted, source="laptop admit-hf")
    control(root, "genpath.json", rec)


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


def test_eval_unsupported_skip_records_rewritten_or_removed(world):
    """Code review r2 finding 4 (driver side, `emx.e12.deps`): a skip naming a run outside the
    job's chain is rewritten to the producing run; a skip whose producer has no status is
    removed (pending: no manifest); the manifest lists the failed status beside the skip."""
    _rescued(world)
    make_status(world.root, "repair_B1", "failed")
    shutil.rmtree(world.root / C.ckpt_dir(REC, "B1_r107"))
    bad = {"status": C.JOB_SKIP_STATUS, "run": "repair_A1", "reason": "x"}
    _put(world.root, C.job_status_path(REC, "full", "B1_r107"), json.dumps(bad))
    shutil.rmtree(world.root / C.ckpt_dir(REC, "A2_r214"))  # absent, no status: pending
    (world.root / C.status_path(REC, "rescue_A2")).unlink()
    _put(world.root, C.job_status_path(REC, "full", "A2_r214"), json.dumps(
        {**bad, "run": "rescue_A2"}))
    vllm_genpath(world.root)
    assert run(world, "eval-endpoints") == C.EXIT_FAILED
    assert _json(world.root, C.job_status_path(REC, "full", "B1_r107"))["run"] == "repair_B1"
    assert not (world.root / C.job_status_path(REC, "full", "A2_r214")).exists()
    assert not (world.root / C.manifest_path(REC, "eval-endpoints")).exists()
    make_ckpt(world.root, "A2_r214")
    make_status(world.root, "rescue_A2")
    assert run(world, "eval-endpoints") == 0
    man = (world.root / C.manifest_path(REC, "eval-endpoints")).read_text()
    assert C.status_path(REC, "repair_B1") in man


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
    hf_genpath(w.root)
    assert run(w, "eval-poison") == 0
    want = [j for j in C.planned_jobs(REC, "hf") if pod.in_step("eval-poison", j)]
    assert sorted(w.hf_calls) == sorted((j.kind, j.key) for j in want) and not w.gen_calls
    assert w.applied == ["P1", "P2", "P3"]
    sm = _json(w.root, C.smoke_adapter_path(REC, "P1"))
    assert (sm["method"], sm["verdict"], sm["effect"]) == ("hf_continuity", "PASS", 1.0)


def test_hf_grown_job_list_reruns_only_missing_jobs(hf_world):
    w = hf_world
    _rescued(w)
    hf_genpath(w.root)
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
    hf_genpath(w.root)
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
for spec in ${STUB_RC:-}; do  # STEP=RC fails every part; STEP:PART=RC that part only
  if [ "${spec%%=*}" = "$step" ] || [ "${spec%%=*}" = "$step:$part" ]; then exit "${spec#*=}"; fi
done
if [ -n "$part" ] && [ "$part" != score ]; then exit 0; fi
[ "${STUB_NO_MANIFEST:-}" = "$step" ] && exit 0
if [ "$step" = lora-check ] && [ -n "${STUB_LORA_FAIL_HF:-}" ]; then  # a scored FAIL -> HF
  mkdir -p "control/E12/$recipe"
  echo '{"gen_path": "hf", "lora_check_verdict": "FAIL"}' > "control/E12/$recipe/genpath.json"
fi
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
    assert run_chain(E12_LAST_STEP="generate")[0].returncode == 2


def test_chain_tan32_is_a_valid_recipe(chain):
    root, run_chain = chain
    r, log = run_chain("--step", "smoke", E12_RECIPE="tan32")
    assert r.returncode == 0 and _tags(log)[-1] == "E12_CHAIN_DONE failed=none", log
    assert all(c.endswith(" tan32") for c in _calls(root))
    assert (root / C.manifest_path("tan32", "smoke")).is_file()


def test_chain_tan32_pauses_after_eval_poison_until_the_induction_decision(chain):
    """Code review, Amendment D r1 finding 2: without control induction.json a tan32 chain stops
    after eval-poison by default; with it, a later launch runs on; rank 1 is unaffected."""
    root, run_chain = chain
    r, log = run_chain(E12_RECIPE="tan32")
    assert r.returncode == 0, log
    upto = list(C.STEPS[: C.STEPS.index("eval-poison") + 1])
    assert _tags(log) == [f"E12_STEP_DONE {s}" for s in upto] + [
        "E12_CHAIN_PAUSED after=eval-poison"]
    assert "repair" not in [c.split()[1] for c in _calls(root)]
    assert not (root / C.manifest_path("tan32", "repair")).exists()
    r, _ = run_chain("--detach", E12_DRY_RUN="1", E12_RECIPE="tan32")
    assert "E12_LAST_STEP=eval-poison " in r.stdout
    r, _ = run_chain("--detach", E12_DRY_RUN="1", E12_RECIPE="tan32", E12_LAST_STEP="repair")
    assert "E12_LAST_STEP=repair " in r.stdout  # an explicit last step wins
    (root / "calls.log").unlink()
    _put(root, "control/E12/tan32/induction.json", '{"passed": true, "n_induced": 3}\n')
    r, log = run_chain(E12_RECIPE="tan32")
    assert r.returncode == 0 and _tags(log)[-1] == "E12_CHAIN_DONE failed=none", log
    assert [c.split()[1] for c in _calls(root)][:2] == ["eval-poison", "repair"]
    assert (root / C.manifest_path("tan32", "manifest")).is_file()
    r, _ = run_chain("--detach", E12_DRY_RUN="1")  # rank 1: no default last step
    assert "E12_LAST_STEP" not in r.stdout


def test_chain_last_step_pauses_then_resumes(chain):
    root, run_chain = chain
    r, log = run_chain(E12_LAST_STEP="eval-poison")
    assert r.returncode == 0, log
    upto = list(C.STEPS[: C.STEPS.index("eval-poison") + 1])
    assert _tags(log) == [f"E12_STEP_DONE {s}" for s in upto] + [
        "E12_CHAIN_PAUSED after=eval-poison"]
    assert [c.split()[1] for c in _calls(root)][-1] == "eval-poison"
    assert not (root / C.manifest_path(REC, "repair")).exists()
    (root / "calls.log").unlink()
    r, log = run_chain()  # a later launch without it resumes normally
    assert r.returncode == 0 and _tags(log)[-1] == "E12_CHAIN_DONE failed=none", log
    assert [c.split()[1] for c in _calls(root)][:2] == ["eval-poison", "repair"]
    r, _ = run_chain("--detach", E12_DRY_RUN="1", E12_LAST_STEP="repair")
    assert "E12_LAST_STEP=repair " in r.stdout


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


@pytest.mark.parametrize("step", ["download", "smoke", "poison-s1", "lora-check", "poison",
                                  "eval-poison", "repair"])
def test_chain_gating_failure_stops(chain, step):
    root, run_chain = chain
    r, log = run_chain(STUB_RC=f"{step}=1")
    assert r.returncode == 1
    assert _tags(log)[-2:] == [f"E12_STEP_FAILED {step} rc=1", f"E12_CHAIN_FAILED at={step}"]
    after = C.STEPS[C.STEPS.index(step) + 1:]
    assert not any(c.split()[1] in after for c in _calls(root))


def test_smoke_budget_refusal_stops_before_training(chain):
    root, run_chain = chain
    r, log = run_chain(STUB_RC="smoke=2")
    assert r.returncode == 2
    assert _tags(log)[-2:] == ["E12_STEP_REFUSED_BUDGET smoke", "E12_CHAIN_FAILED at=smoke"]
    assert [c.split()[1:3] for c in _calls(root)] == [["download", "rank1"], ["smoke", "hf"]]


def test_chain_b_only_failure_stays_local(chain):
    """The driver records a B repair failure and exits 0 (tested on the driver above): the
    chain goes on through every later step."""
    _, run_chain = chain
    r, log = run_chain()
    assert r.returncode == 0 and "E12_STEP_DONE rescue" in _tags(log)


def test_chain_rescue_and_nudge_failures_do_not_stop_it(chain):
    _, run_chain = chain
    r, log = run_chain(STUB_RC="rescue=1 nudge=1")
    assert r.returncode == 1
    assert "E12_STEP_FAILED rescue rc=1" in _tags(log) and "E12_STEP_DONE eval-cue-task" in log
    assert _tags(log)[-1] == "E12_CHAIN_DONE failed=rescue,nudge"


@pytest.mark.parametrize("part", ["hf", "vllm", "score"])
def test_chain_lora_check_execution_failure_stops_before_bulk_training(chain, part):
    """Code review r2 finding 3: a crashed re-check (any part) leaves no generation-path
    admission, so the chain stops at lora-check and never trains poison onwards."""
    root, run_chain = chain
    r, log = run_chain(STUB_RC=f"lora-check:{part}=1")
    assert r.returncode == 1
    assert _tags(log)[-2:] == ["E12_STEP_FAILED lora-check rc=1",
                               "E12_CHAIN_FAILED at=lora-check"]
    steps = [c.split()[1] for c in _calls(root)]
    assert steps[-1] == "lora-check" and "poison" not in steps
    assert [c.split()[2] for c in _calls(root) if c.split()[1] == "lora-check"] == \
        ["hf", "vllm", "score"][: ["hf", "vllm", "score"].index(part) + 1]


def test_chain_lora_check_bad_manifest_stops(chain):
    root, run_chain = chain
    r, log = run_chain(STUB_NO_MANIFEST="lora-check")
    assert r.returncode == 1
    assert _tags(log)[-2:] == ["E12_STEP_FAILED lora-check rc=0",
                               "E12_CHAIN_FAILED at=lora-check"]
    assert "poison" not in [c.split()[1] for c in _calls(root)]


def test_chain_lora_check_scored_fail_selecting_hf_continues(chain):
    """A successfully scored FAIL writes genpath.json (hf) and its manifest and exits 0: the
    chain goes on, and the eval steps run under the peft python (the HF path)."""
    root, run_chain = chain
    r, log = run_chain(STUB_LORA_FAIL_HF="1")
    assert r.returncode == 0, log
    tags = _tags(log)
    assert "E12_STEP_DONE lora-check" in tags and tags[-1] == "E12_CHAIN_DONE failed=none"
    calls = [c.split() for c in _calls(root)]
    assert ["peftpy", "poison", "rank1"] in calls
    assert [c[0] for c in calls if c[1].startswith("eval-")] == ["peftpy"] * 5


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
    assert _tags(log) == ["E12_STEP_FAILED lora-check rc=1", "E12_CHAIN_FAILED at=lora-check"]
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


# --- review r1 fixes (findings 1, 3, 4, 5, 6, 10) -----------------------------------------------


def _lora_ready(world):
    make_ckpt(world.root, "P1")
    make_status(world.root, "poison_s1")
    for part in ("hf", "vllm"):
        assert run(world, "lora-check", "--part", part) == 0


def test_lora_check_vllm_writes_the_admission_record(world):
    _lora_ready(world)
    world.proj_cuts = [4]
    assert run(world, "lora-check", "--part", "score") == 0
    adm = _json(world.root, C.admission_path(REC))
    assert set(C.ADMISSION_FIELDS) <= set(adm)
    jobs = C.planned_jobs(REC, "vllm", None, (4,))
    assert (adm["gen_path"], adm["admitted"], adm["cuts"]) == ("vllm", True, [4])
    assert adm["jobs_sha256"] == C.jobs_sha256(jobs) == C.jobs_sha256(pod._jobs_from(adm["jobs"]))
    gp = json.loads((world.root / "control/E12/rank1/genpath.json").read_text())
    assert gp["jobs_sha256"] == adm["jobs_sha256"]
    man = (world.root / C.manifest_path(REC, "lora-check")).read_text()
    assert C.admission_path(REC) in man


def test_lora_check_refused_projection_stops_the_chain(world):
    """Finding 3: like smoke's refusal: exit 2, no manifest, no admission record."""
    _lora_ready(world)
    world.verdict = "refuse"
    assert run(world, "lora-check", "--part", "score") == C.EXIT_BUDGET
    assert not (world.root / C.manifest_path(REC, "lora-check")).exists()
    assert not (world.root / C.admission_path(REC)).exists()
    gp = json.loads((world.root / "control/E12/rank1/genpath.json").read_text())
    assert gp["admitted"] is False
    assert _json(world.root, C.bench_path(REC, "step_lora-check_score"))["outcome"] == \
        "budgetrefused"


@pytest.mark.parametrize("step", ["lora-check", "eval-nudge", "repair"])
def test_chain_budget_refusal_in_any_step_stops(chain, step):
    root, run_chain = chain
    r, log = run_chain(STUB_RC=f"{step}=2")
    assert r.returncode == 2
    assert _tags(log)[-2:] == [f"E12_STEP_REFUSED_BUDGET {step}", f"E12_CHAIN_FAILED at={step}"]
    after = C.STEPS[C.STEPS.index(step) + 1:]
    assert not any(c.split()[1] in after for c in _calls(root))


def test_hf_eval_needs_the_laptops_admission_record_and_agreement(hf_world):
    """Finding 6: genpath.json alone is not enough; the admission record must agree."""
    w = hf_world
    _poisoned(w)
    hf_genpath(w.root, write_admission=False)
    assert run(w, "eval-poison") == C.EXIT_BLOCKED and not w.hf_calls
    jobs = C.hf_complete_jobs(REC, (4,))
    admission(w.root, "hf", jobs, (4,))  # a different admission (other cuts, other hash)
    assert run(w, "eval-poison") == C.EXIT_REFUSED
    hf_genpath(w.root)
    admission(w.root, "vllm", C.hf_complete_jobs(REC))  # wrong path
    assert run(w, "eval-poison") == C.EXIT_REFUSED
    hf_genpath(w.root, jobs=C.planned_jobs(REC, "hf", 214, (), True))  # not the union
    assert run(w, "eval-poison") == C.EXIT_REFUSED
    assert not w.hf_calls


def test_cuts_come_from_the_admission_record(hf_world):
    w = hf_world
    _poisoned(w)
    _put(w.root, C.projection_path(REC, "lora-check"), json.dumps({"verdict": "admit",
                                                                    "cuts": [5]}))
    hf_genpath(w.root, cuts=(4,))
    ctx = pod.Ctx(pod.parser().parse_args(["eval-poison", "--root", str(w.root)]))
    assert pod.effective_cuts(ctx) == (4,)  # never the projection file's
    control(w.root, "cuts.json", {"cuts": [4, 5]})
    assert run(w, "eval-poison") == C.EXIT_REFUSED  # cuts.json disagrees with the admission
    control(w.root, "cuts.json", {"cuts": [4]})
    assert run(w, "eval-poison") == 0
    assert _json(w.root, C.bench_path(REC, "eval-poison.jobs"))["cuts"] == [4]


def _trained_all(world):
    _rescued(world)
    for it in pod.nudge_items():
        make_status(world.root, it.run)
        for key in it.saves.values():
            make_ckpt(world.root, key)


@pytest.mark.parametrize("endpoint,rescue", [(107, False), (214, True)])
def test_hf_completion_at_either_endpoint(hf_world, endpoint, rescue):
    """Finding 1: the admitted HF set is the union of both branches, so either branch completes;
    the active jobs follow endpoint.json."""
    w = hf_world
    _trained_all(w)
    hf_genpath(w.root)
    epj = {"rescue_triggered": rescue}
    if not rescue:
        epj["endpoint"] = 107
    control(w.root, "endpoint.json", epj)
    for step in ("eval-poison", "eval-endpoints", "eval-traj"):
        assert run(w, step) == 0, step
    control(w.root, "endpoint.json", {"rescue_triggered": rescue, "endpoint": endpoint})
    for step in ("eval-endpoints", "eval-nudge", "eval-cue-task"):
        assert run(w, step) == 0, step
    want = C.planned_jobs(REC, "hf", endpoint, (), rescue)
    assert sorted(w.hf_calls) == sorted((j.kind, j.key) for j in want)
    assert set(want) <= set(C.hf_complete_jobs(REC))
    for step in C.EVAL_STEPS:
        assert pod.manifest_verifies(pod.Ctx(_args(w.root)), step), step


def test_failed_run_is_never_retrained_by_the_driver(world):
    """Finding 4: a terminal failed status stays failed on re-invocation."""
    _poisoned(world)
    make_status(world.root, "repair_B1", "failed")
    assert run(world, "repair") == 0
    assert "repair_B1" not in runs_of(world)
    assert _json(world.root, C.bench_path(REC, "repair.runs"))["runs"]["repair_B1"] == "failed"
    make_status(world.root, "repair_A1", "failed")
    (world.root / C.manifest_path(REC, "repair")).unlink()
    n = len(world.specs)
    assert run(world, "repair") == C.EXIT_FAILED  # a gating run's terminal failure
    assert len(world.specs) == n


def test_pending_parent_is_not_a_failure(world):
    """Finding 10 (training): a missing parent with no failed status leaves the child pending:
    no status, no manifest, trained by a later re-run."""
    _repaired(world)
    shutil.rmtree(world.root / C.ckpt_dir(REC, "B1_r107"))
    (world.root / C.status_path(REC, "repair_B1")).unlink()
    assert run(world, "rescue") == C.EXIT_FAILED
    assert "rescue_B1" not in runs_of(world)
    assert not (world.root / C.status_path(REC, "rescue_B1")).exists()
    assert not (world.root / C.manifest_path(REC, "rescue")).exists()
    make_ckpt(world.root, "B1_r107")
    make_status(world.root, "repair_B1")
    assert run(world, "rescue") == 0 and runs_of(world)[-1] == "rescue_B1"


def test_missing_checkpoint_without_a_failure_stays_pending(world):
    """Finding 10 (evaluation): no skip record for an absent checkpoint whose producer has no
    terminal failure; the step reports it and writes no manifest; a failed parent does skip."""
    _rescued(world)
    shutil.rmtree(world.root / C.ckpt_dir(REC, "B2_r214"))  # rescue_B2 interrupted: no status
    (world.root / C.status_path(REC, "rescue_B2")).unlink()
    vllm_genpath(world.root)
    assert run(world, "eval-endpoints") == C.EXIT_FAILED
    assert not (world.root / C.job_status_path(REC, "full", "B2_r214")).exists()
    assert not (world.root / C.manifest_path(REC, "eval-endpoints")).exists()
    assert ("full", "B2_r214") not in world.gen_calls and ("full", "A1_r214") in world.gen_calls
    make_ckpt(world.root, "B2_r214")
    make_status(world.root, "rescue_B2")
    assert run(world, "eval-endpoints") == 0 and world.gen_calls[-1] == ("full", "B2_r214")
    # a nudge whose parent's run failed (and that was never trained) gets a terminal skip
    make_status(world.root, "rescue_C3", "failed")
    st = pod.dependency_state(pod.Ctx(_args(world.root)), "C3_r214_n05")
    assert st[0] == pod.DEP_FAILED and "rescue_C3" in st[1]
    assert pod.dependency_state(pod.Ctx(_args(world.root)), "A1_r214_n05")[0] == pod.DEP_PENDING


def test_stale_outputs_and_smoke_are_regenerated(world):
    """Finding 5: a changed checkpoint invalidates its outputs and applied-smoke, including on
    the early eval_complete path; stale outputs are moved aside, not overwritten in place."""
    _poisoned(world)
    vllm_genpath(world.root)
    assert run(world, "eval-poison") == 0
    n = len(world.gen_calls)
    assert run(world, "eval-poison") == 0 and len(world.gen_calls) == n  # complete
    make_ckpt(world.root, "P2")  # same files ...
    d = world.root / C.ckpt_dir(REC, "P2")
    (d / "adapter_model.safetensors").write_text("retrained")  # ... then a new adapter
    ck = json.loads((d / "ckpt.json").read_text())
    ck["adapter_files_sha256"]["adapter_model.safetensors"] = G.sha256_file(
        d / "adapter_model.safetensors")
    (d / "ckpt.json").write_text(json.dumps(ck))
    world.smoked.clear()
    assert run(world, "eval-poison") == 0
    redo = sorted(world.gen_calls[n:])
    assert redo == sorted((j.kind, "P2") for j in C.planned_jobs(REC) if j.key == "P2"
                          and pod.in_step("eval-poison", j))
    assert world.smoked == ["P2"]
    sup = list((world.root / C.rdir(REC) / "superseded" / "gen").rglob("P2.*.meta.json"))
    assert len(sup) == len(redo)
    sm = _json(world.root, C.smoke_adapter_path(REC, "P2"))
    assert sm["adapter_files"] == ck["adapter_files_sha256"]


def test_smoke_record_without_current_adapter_shas_is_redone(world):
    _poisoned(world)
    vllm_genpath(world.root)
    for k in ("P1", "P2", "P3"):
        _put(world.root, C.smoke_adapter_path(REC, k), json.dumps({"verdict": "PASS"}))
    assert run(world, "eval-poison") == 0
    assert sorted(world.smoked) == ["P1", "P2", "P3"]


def test_smoke_unmeasured_applied_smoke_fails_the_step(smoke_fakes):
    """Code review r3 finding 1: UNVERIFIED (an execution error) gates; a measured FAIL does not."""
    w = smoke_fakes
    assert run(w, "smoke", "--part", "hf") == 0
    orig = w.applied_smoke

    def verdicts(v):
        def f(*a, **k):
            out = orig(*a, **k)
            return {key: {**rec, "verdict": v} for key, rec in out.items()}
        return f

    w.mp.setattr(pod.gen, "applied_smoke", verdicts("UNVERIFIED"))
    assert run(w, "smoke", "--part", "vllm") == C.EXIT_FAILED
    w.mp.setattr(pod.gen, "applied_smoke", verdicts("FAIL"))
    assert run(w, "smoke", "--part", "vllm") == 0


def test_smoke_retry_measures_a_fresh_bench(smoke_fakes):
    """Code review r3 finding 2: a retried smoke regenerates every bench job (no cached answers
    divided by the retry's time)."""
    w = smoke_fakes
    assert run(w, "smoke", "--part", "hf") == 0
    assert run(w, "smoke", "--part", "vllm") == 0
    n = len(w.gen_calls)
    assert run(w, "smoke", "--part", "vllm") == 0
    assert len(w.gen_calls) == 2 * n


# --- incremental retention (Amendment D') --------------------------------------------------------


def test_rank32_training_steps_upload_their_checkpoints_incrementally(world, hf):
    world.scratch_adapters = True
    T = "tan32"
    assert run(world, "poison-s1", "--recipe", T) == 0
    assert hf.created == ["jrepifano/e12-tan32-adapters"] and len(hf.commits) == 1
    assert sorted(hf.commits[0]) == sorted(f"P1/{n}" for n in (*R.ADAPTER_FILES, "ckpt.json"))
    rec = _json(world.root, C.retention_receipt_path(T))
    assert (rec["method"], rec["private"], rec["commit_sha"]) == ("hf", True, "c1")
    assert sorted(rec["files"]) == sorted(f"P1/{n}" for n in R.ADAPTER_FILES)
    step = _json(world.root, C.bench_path(T, "retention_poison-s1"))
    assert step["ok"] and step["keys"] == ["P1"] and step["n_new_files"] == 2
    assert run(world, "poison", "--recipe", T) == 0
    assert sorted(hf.commits[1]) == sorted(f"{k}/{n}" for k in ("P2", "P3")
                                           for n in (*R.ADAPTER_FILES, "ckpt.json"))
    rec = _json(world.root, C.retention_receipt_path(T))
    assert len(rec["files"]) == 6 and rec["commit_sha"] == "c2"
    # the chain's final upload step: nothing new to send, the receipt and manifest written
    assert run(world, "upload", "--recipe", T) == 0 and len(hf.commits) == 2
    assert (world.root / C.manifest_path(T, "upload")).is_file()


def test_incremental_upload_failure_is_recorded_not_fatal(world, monkeypatch):
    fake = FakeHF(private=False)
    monkeypatch.setattr(pod, "_hf_api", lambda: (fake, add_op))
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    world.scratch_adapters = True
    assert run(world, "poison-s1", "--recipe", "tan32") == 0  # the step still succeeds
    assert (world.root / C.manifest_path("tan32", "poison-s1")).is_file()
    step = _json(world.root, C.bench_path("tan32", "retention_poison-s1"))
    assert not step["ok"] and "not private" in step["error"]
    assert not fake.commits and not (world.root / C.retention_receipt_path("tan32")).exists()
    monkeypatch.delenv("HF_TOKEN")
    assert run(world, "poison", "--recipe", "tan32") == 0
    assert "HF_TOKEN" in _json(world.root, C.bench_path("tan32", "retention_poison"))["error"]


def test_incremental_upload_skips_a_bad_checkpoint_and_still_sends_the_rest(world, hf):
    world.scratch_adapters = True
    assert run(world, "poison-s1", "--recipe", "tan32") == 0
    # P2's adapter file is changed after its ckpt.json: left out, recorded; P3 is uploaded
    orig = world.run_segment

    def corrupt(model, tok, spec, root):
        out = orig(model, tok, spec, root)
        if spec.run == "poison_s2":
            f = world.root / "scratch" / os.path.relpath(
                C.adapter_files_dir("tan32", "P2"), C.SCRATCH) / "adapter_model.safetensors"
            f.write_text("other")
        return out

    world.mp.setattr(pod.trainer, "run_segment", corrupt)
    assert run(world, "poison", "--recipe", "tan32") == 0
    step = _json(world.root, C.bench_path("tan32", "retention_poison"))
    assert not step["ok"] and step["keys"] == ["P3"] and "P2" in step["problems"][0]
    assert sorted(hf.commits[-1]) == sorted(f"P3/{n}" for n in (*R.ADAPTER_FILES, "ckpt.json"))


def test_rank1_training_steps_make_no_hf_calls(world, hf):
    assert run(world, "poison-s1") == 0
    assert not hf.created and not hf.commits
    assert not (world.root / C.bench_path(REC, "retention_poison-s1")).exists()
