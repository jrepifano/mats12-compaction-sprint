# ruff: noqa: F811 - fixtures imported from tests/i1a/conftest.py are used as arguments
"""U3 readers (emx.i9.readers): the prompt-scope assert, token readers and the Oracle Lens on the
tiny model, readouts parsed by wsbench's own loader."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from i1a.conftest import (  # noqa: F401 - pytest fixtures
    _tiny_model_master,
    dummy_olens_dir,
    fake_jlens_path,
    i1a_tmp_layout,
    qwen_tok,
    tiny_backend,
    tiny_model,
)

from emx.i9 import acts as A
from emx.i9 import contracts as C
from emx.i9 import readers as R

from .test_acts import i9_layout, scenario, wsbench_render  # noqa: F401 - fixture

torch = pytest.importorskip("torch")
TINY_SAMPLING = {"max_new_tokens": 4}


@pytest.fixture
def store(tiny_backend, i9_layout):
    scs = [scenario(0), scenario(1)]
    return A.capture_last64(tiny_backend, scs, render_fn=wsbench_render(tiny_backend.tokenizer))


def _load(path, ids, layers):
    from wsbench.readouts import load_readouts

    return load_readouts(path, ids=ids, layers=layers)


# --------------------------------------------------------------------- scope


def test_scope_assert_rejects_planted_rows(tmp_path):
    rs = {"a": 10}
    good = [{"id": "a", "layer": 20, "pos": p, "tokens": ["x"]} for p in range(10)]
    assert R.scope_assert(good, rs) == 10
    for bad in (
        {"id": "a", "layer": 20, "pos": 10, "tokens": ["x"]},  # first reply token
        {"id": "a", "layer": 20, "pos": -1, "tokens": ["x"]},
        {"id": "b", "layer": 20, "pos": 0, "tokens": ["x"]},
    ):  # unknown scenario
        with pytest.raises(R.PromptScopeError):
            R.scope_assert([*good, bad], rs)
        out = tmp_path / "r.jsonl"
        with pytest.raises(R.PromptScopeError):
            R.write_readouts(out, [*good, bad], rs)
        assert not out.exists()


def test_write_readouts_refuses_duplicates(tmp_path):
    row = {"id": "a", "layer": 20, "pos": 1, "tokens": ["x"]}
    with pytest.raises(ValueError, match="duplicate"):
        R.write_readouts(tmp_path / "r.jsonl", [row, dict(row)], {"a": 5})


# --------------------------------------------------------------------- token readers


@pytest.mark.parametrize("name", ["logit_lens", "jlens"])
def test_token_readers_write_wsbench_readouts(name, tiny_backend, store, fake_jlens_path):
    from emx.i1a import token_readers as tr

    reader = tr.bind_reader(name, tiny_backend, jlens_path=fake_jlens_path)
    _, sites, _ = A.load_store(store)
    ids = ["am-test-1"]
    out = C.readouts_path(R.readout_name(name, "confirmatory"))
    R.read_tokens(reader, store=store, ids=ids, out=out)
    cells, rep = _load(out, ids, C.LADDER)
    assert rep.kind == "tokens" and not any(rep.skipped.values())
    assert len(cells) == rep.n_rows == C.N_READ_POSITIONS * len(C.LADDER)
    assert rep.layers == list(C.LADDER)
    mine = {r.pos: r for r in sites if r.id == "am-test-1"}
    assert {c.pos for c in cells} == set(mine)
    assert all(c.token == mine[c.pos].token and len(c.tokens) == 10 for c in cells)
    # the same cell straight through the benchmark's read
    _, _, h = A.load_store(store)
    c = cells[5]
    j = C.LADDER.index(c.layer)
    assert list(c.tokens) == reader.read(h[mine[c.pos].index, j], c.layer).tokens


def test_readout_names():
    assert R.readout_name("jlens", "confirmatory") == "jlens"
    assert R.readout_name("olens", "bank_validation") == "olens_bank_validation"


# --------------------------------------------------------------------- Oracle Lens


@pytest.fixture
def olens(tiny_backend, dummy_olens_dir):
    from emx.i1a import olens as O

    o = O.PublicOLens(sampling=replace(O.default_sampling(), **TINY_SAMPLING))
    o.bind(tiny_backend, dummy_olens_dir)
    return o


def test_olens_plan_uses_i9_seeds():
    plan = R.olens_plan("confirmatory", range(64), C.OLENS_LAYERS)
    assert len(plan) == R.n_olens_batches(1) == 2 * len(C.OLENS_LAYERS)
    assert [b.seed for b in plan] == [C.OLENS_SEED + i for i in range(len(plan))]
    assert [b.layer for b in plan] == sorted([*C.OLENS_LAYERS] * 2)
    assert all(len(b.sites) == R.OLENS_BATCH for b in plan)
    val = R.olens_plan("bank_validation", range(64), C.OLENS_LAYERS)
    assert not {b.seed for b in plan} & {b.seed for b in val}
    assert R.n_olens_batches(C.N_CONFIRMATORY) == 640


def test_olens_draw_resumes_and_writes_readouts(olens, store, monkeypatch):
    from emx.i1a import olens as O

    ids, layers = ["am-test-0"], (44,)
    calls = []
    real = O.read_batch

    def counting(o, H, layer, seed, **kw):
        calls.append(seed)
        return real(o, H, layer, seed, **kw)

    monkeypatch.setattr(O, "read_batch", counting)
    p = R.generate_olens(olens, "confirmatory", store=store, ids=ids, layers=layers)
    rows = R.read_samples(p)
    assert len(rows) == C.N_READ_POSITIONS and calls == [C.OLENS_SEED, C.OLENS_SEED + 1]
    assert len({(r.id, r.pos, r.layer) for r in rows}) == len(rows)  # one sample per cell
    # a crash mid-batch: drop half of batch 1 and tear the last line; only batch 1 reruns
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[:40]) + "\n" + lines[40][:20])
    calls.clear()
    R.generate_olens(olens, "confirmatory", store=store, ids=ids, layers=layers)
    assert calls == [C.OLENS_SEED + 1]
    assert sorted((r.batch_index, r.site_index) for r in R.read_samples(p)) == sorted(
        (r.batch_index, r.site_index) for r in rows
    )
    # a complete draw does no work
    calls.clear()
    R.generate_olens(olens, "confirmatory", store=store, ids=ids, layers=layers)
    assert calls == []
    out = C.readouts_path("olens")
    R.write_olens_readouts("confirmatory", store=store, samples=p, out=out)
    cells, rep = _load(out, ids, layers)
    assert rep.kind == "prose" and not any(rep.skipped.values())
    assert len(cells) == C.N_READ_POSITIONS and all(len(c.samples) == 1 for c in cells)


def test_olens_rejects_a_foreign_plan(olens, store):
    p = R.samples_path("confirmatory")
    p.parent.mkdir(parents=True, exist_ok=True)
    bad = R.OlensSample("confirmatory", "am-test-0", 44, 0, 0, 999, 0, "x", 1, "eos")
    p.write_text(json.dumps(bad.__dict__) + "\n")
    with pytest.raises(ValueError):
        R.generate_olens(olens, "confirmatory", store=store, ids=["am-test-0"], layers=(44,))


def test_olens_pilot_writes_timing(olens, store):
    rec = R.olens_pilot(olens, store=store, ids=["am-test-0", "am-test-1"], n_batches_planned=10)
    assert rec["batch_size"] == R.OLENS_BATCH and rec["layer"] == C.OLENS_LAYERS[2]
    assert rec["projected_hours"] == pytest.approx(10 * rec["seconds"] / 3600, abs=1e-3)
    assert json.loads(R.pilot_path().read_text())["seconds"] == rec["seconds"]


# --------------------------------------------------------------------- the pod step driver


def _pod():
    import importlib.util

    spec = importlib.util.spec_from_file_location("i9_pod", C.REPO_ROOT / "scripts" / "i9_pod.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pod(tmp_path, monkeypatch, tiny_backend):
    """The driver on a tmp repo root: 2 scenarios per set, the tiny backend for both models,
    the wsbench chat render as render_ids, and a fake sampler writing contract-shaped replies
    plus its sidecars (throughput, prompt ids)."""
    mod = _pod()
    results = tmp_path / "results" / "I9"
    data = tmp_path / "data" / "I9"
    monkeypatch.setattr(C, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(C, "RESULTS", results)
    monkeypatch.setattr(C, "SCENARIOS_PATH", data / "scenarios.jsonl")
    monkeypatch.setattr(C, "SETS_PATH", data / "sets.json")
    for name, sub in [
        ("ACTS_DIR", "acts"),
        ("READOUTS_DIR", "readouts"),
        ("CHECKS_DIR", "checks"),
        ("MANIFEST_DIR", "manifests"),
        ("REPLIES_DIR", "replies"),
    ]:
        monkeypatch.setattr(C, name, results / sub)
    monkeypatch.setenv("EMX_GIT_COMMIT", "abc123")
    monkeypatch.setattr(C, "DATA", tmp_path / "data" / "I9")
    scs = [replace(scenario(i, 20), id=f"am-s{i}") for i in range(12)]
    data.mkdir(parents=True)
    C.SCENARIOS_PATH.write_text("".join(C.to_json(s) + "\n" for s in scs))
    sets = {k: [s.id for s in scs[2 * j : 2 * j + 2]] for j, k in enumerate(C.SET_KEYS)}
    C.SETS_PATH.write_text(json.dumps(sets))
    results.mkdir(parents=True)
    (results / "download_paths.json").write_text(json.dumps({"qwen_dir": "q", "gemma_dir": "g"}))
    rf = wsbench_render(tiny_backend.tokenizer)
    monkeypatch.setattr(A, "load_backend", lambda model, d, **kw: tiny_backend)
    monkeypatch.setattr(A, "default_render", lambda model, tok: rf)
    by_id = {s.id: s for s in scs}

    def fake_run(cmd, env):
        a = [x for x in cmd[2:] if x not in ("--thinking", "--no-thinking")]
        kv = dict(zip(a[::2], a[1::2], strict=True))
        model, out = kv["--model"], Path(kv["--out"])
        thinking = "--thinking" in cmd
        ids = [i for k in kv["--set"].split(",") for i in sets[k]]
        if "--ids" in kv:
            ids = [i for i in ids if i in kv["--ids"].split(",")]
        assert kv["--model-dir"] == {"qwen": "q", "gemma": "g"}[model]
        n = int(kv.get("--n-samples", C.N_SAMPLES))
        rows = [
            C.Reply(model, thinking, i, k, C.request_seed(model, thinking, i, k), "ok", 1, "eos")
            for i in ids
            for k in range(n)
        ]
        out.write_text("".join(C.to_json(r) + "\n" for r in rows))
        out.with_name(f"{out.stem}.throughput.json").write_text("{}\n")
        return 0

    def fake_capture(cmd):  # the sampler-venv render: same renderer, same hashes
        assert cmd[1] == "-c" and cmd[3] in C.MODELS
        rows = [json.loads(x) for x in Path(cmd[5]).read_text().splitlines()]
        return json.dumps({r["id"]: A.ids_sha256(rf(by_id[r["id"]])) for r in rows}) + "\n"

    monkeypatch.setattr(mod, "run_cmd", fake_run)
    monkeypatch.setattr(mod, "run_capture", fake_capture)
    mod.write_manifest(
        "download",
        [results / "download_paths.json"],
        args=mod.parser().parse_args(["download"]),
        started=mod.utc(),
    )
    return mod


def _verify_three(mod, step, tmp_path):
    import subprocess

    man = mod.sha_manifest_path(step)
    r = subprocess.run(
        ["shasum", "-a", "256", "-c", str(man)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    side = json.loads(mod.sidecar_path(step).read_text())
    listed = {line.split("  ", 1)[1] for line in man.read_text().splitlines()}
    assert side["step"] == step and set(side["required_outputs"]) == listed
    assert all(type(v) is int for v in side["counts"].values())
    assert {"code_commit", "inputs_sha256", "limited", "checks", "args"} <= set(side)
    d = json.loads(C.manifest_path(step).read_text())
    m = C.Manifest(
        **{
            **d,
            "inputs": [C.FileRef(**x) for x in d["inputs"]],
            "outputs": [C.FileRef(**x) for x in d["outputs"]],
        }
    )
    assert {o.path for o in m.outputs} == listed and m.pins["qwen"].endswith(C.QWEN_REV)
    ins = {i.path for i in m.inputs}
    assert {f"results/I9/MANIFEST.{u}.sha256" for u in mod.UPSTREAM[step]} <= ins
    assert {"data/I9/scenarios.jsonl", "data/I9/sets.json"} <= ins
    return side, m


def test_driver_capture_last_manifests_and_resume(pod, tmp_path, monkeypatch):
    pod.main(["capture-last", "--device", "cpu", "--limit", "1"])
    side, m = _verify_three(pod, "capture-last", tmp_path)
    assert side["limited"] is True and side["counts"] == {"qwen": 3, "gemma": 2}
    assert {o.path for o in m.outputs} >= {
        "results/I9/acts/qwen_last.safetensors",
        "results/I9/acts/gemma_last.json",
    }
    # resumable: a verified step is skipped
    monkeypatch.setattr(A, "capture_last", lambda *a, **k: pytest.fail("reran"))
    pod.main(["capture-last", "--device", "cpu", "--limit", "1"])
    # a non-limited invocation does not accept the limited manifest
    args = pod.parser().parse_args(["capture-last"])
    assert pod.step_done("capture-last", args) is False
    # a changed output invalidates the step
    args = pod.parser().parse_args(["capture-last", "--limit", "1"])
    assert pod.step_done("capture-last", args) is True
    acts_file = C.ACTS_DIR / "qwen_last.json"
    acts_file.write_text(acts_file.read_text() + " ")
    assert pod.step_done("capture-last", args) is False


def test_driver_sampling_and_flags(pod, tmp_path):
    with pytest.raises(SystemExit, match="thinking-arm"):
        pod.main(["sample-thinking"])
    with pytest.raises(SystemExit, match="unsealed"):
        pod.main(["olens-sealed"])
    with pytest.raises(SystemExit, match="unsealed"):
        pod.main(["tokens-sealed"])
    with pytest.raises(SystemExit, match="upstream"):
        pod.main(["sample-qwen", "--limit", "1"])
    pod.write_manifest(
        "smoke", [C.SETS_PATH], args=pod.parser().parse_args(["smoke"]), started=pod.utc()
    )
    pod.main(["sample-qwen", "--limit", "1"])
    side, _ = _verify_three(pod, "sample-qwen", tmp_path)
    assert side["counts"] == {"replies": 3 * C.N_SAMPLES}
    assert "results/I9/replies/qwen_nothink.throughput.json" in side["required_outputs"]
    cmd = pod.sampler_cmd("vllm", "gemma", False, ("confirmatory", "sealed"), Path("o.jsonl"))
    assert cmd[1].endswith("scripts/i9_sample_vllm.py")
    assert cmd[2:] == [
        "--model",
        "gemma",
        "--no-thinking",
        "--set",
        "confirmatory,sealed",
        "--out",
        "o.jsonl",
    ]
    assert json.loads(C.manifest_path("sample-qwen").read_text())["git_commit"] == "abc123"


def _i1a_reference(backend):
    """I1a's committed logit-lens readouts and tokens manifest, in the tmp I1a layout, made with
    the check's own path (driver plumbing only: tests/i9/test_acts.py checks the reproduction
    against I1a's own capture and reader code)."""
    import hashlib

    from emx.i1a import capture as I1cap
    from emx.i1a import contracts as I

    chosen, _ = A.i1a_check_sites(backend.tokenizer)
    rows = A.reproduce_i1a_logit_lens(backend, chosen, I1cap.family_layers("multihop"))
    path = I.readouts_path("logit_lens", "multihop")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    I.manifest_path("tokens").write_text(f"{sha}  {A.i1a_readouts_rel()}\n")


def test_driver_smoke_runs_its_checks(pod, tmp_path, tiny_backend, i1a_tmp_layout):
    _i1a_reference(tiny_backend)
    pod.main(["smoke", "--device", "cpu", "--limit", "2"])
    side, _ = _verify_three(pod, "smoke", tmp_path)
    checks = side["checks"]
    assert set(checks) == {
        "replies_qwen",
        "replies_gemma",
        "ids_equal_qwen",
        "ids_equal_gemma",
        "smoke_i1a_reproduction",  # no raw cosine: I1a's stored activations are not supplied
        "prompt_scope",
    }
    assert all(v is True for v in checks.values()), checks
    assert "results/I9/smoke/qwen_last64.safetensors" in side["required_outputs"]
    assert "results/I9/checks/smoke_i1a_reproduction.json" in side["required_outputs"]
    repro = json.loads(C.check_path("smoke_i1a_reproduction").read_text())
    assert repro["n"] == 33 and repro["threshold"] == A.I1A_REPRO_SCORE_TOL
    scope = json.loads(C.check_path("smoke_prompt_scope").read_text())
    assert scope["details"]["planted_row_raised"] is True
    # the closing manifest needs every session step
    with pytest.raises(SystemExit, match="without a manifest"):
        pod.main(["manifest", "--session", "A", "--limit", "2"])


def test_driver_smoke_refuses_without_the_i1a_reference(pod, i1a_tmp_layout):
    with pytest.raises(A.I1aReferenceError, match="refusing to run"):
        pod.main(["smoke", "--device", "cpu", "--limit", "2"])
    assert not pod.sha_manifest_path("smoke").exists()


def test_driver_tokens_sealed_is_its_own_step(pod, tmp_path, fake_jlens_path):
    """`tokens` reads the confirmatory set only; `tokens-sealed` (--unsealed) the sealed set,
    under its own manifests, so unsealing after `tokens` is done is never skipped."""
    paths = C.RESULTS / "download_paths.json"
    paths.write_text(json.dumps({"qwen_dir": "q", "gemma_dir": "g", "jlens": str(fake_jlens_path)}))
    args = pod.parser().parse_args(["download"])
    pod.write_manifest("download", [paths], args=args, started=pod.utc())
    pod.main(["capture-64", "--device", "cpu", "--limit", "1"])
    pod.main(["tokens", "--device", "cpu", "--limit", "1"])
    side, _ = _verify_three(pod, "tokens", tmp_path)
    want = {
        f"results/I9/readouts/{R.readout_name(r, 'confirmatory')}.jsonl" for r in R.TOKEN_READERS
    }
    assert set(side["required_outputs"]) == want
    pod.main(["tokens-sealed", "--device", "cpu", "--limit", "1", "--unsealed"])
    side, _ = _verify_three(pod, "tokens-sealed", tmp_path)
    sealed = {f"results/I9/readouts/{R.readout_name(r, 'sealed')}.jsonl" for r in R.TOKEN_READERS}
    assert set(side["required_outputs"]) == sealed
    assert all(n == C.N_READ_POSITIONS * len(C.LADDER) for n in side["counts"].values())


def test_driver_session_env_and_run_config(pod, monkeypatch):
    args = pod.parser().parse_args(["download"])
    monkeypatch.setenv("I9_SESSION", "B")
    assert pod.download_groups(args) == ["qwen", "readers"]
    monkeypatch.setenv("I9_SESSION", "A")
    assert pod.download_groups(args) == ["qwen", "gemma"]
    assert pod.olens_layers() == C.OLENS_LAYERS
    pod.run_config_path().write_text(json.dumps({"olens_layers": list(C.OLENS_LAYERS_CUT)}))
    assert pod.olens_layers() == C.OLENS_LAYERS_CUT
    pod.run_config_path().write_text(json.dumps({"olens_layers": [21]}))
    with pytest.raises(SystemExit):
        pod.olens_layers()


def test_driver_thinking_arm_is_session_bs(pod, tmp_path, monkeypatch):
    """Amendment B item 7: sample-thinking runs in session B on B's own download (no smoke
    manifest there), with the Qwen weights B downloads; session A refuses it."""
    monkeypatch.setenv("I9_SESSION", "A")
    with pytest.raises(SystemExit, match="session B"):
        pod.main(["sample-thinking", "--thinking-arm", "--limit", "1"])
    assert pod.SESSION_OPTIONAL["A"] == () and "sample-thinking" in pod.SESSION_OPTIONAL["B"]
    assert "qwen" in pod.SESSION_DOWNLOADS["B"]
    monkeypatch.setenv("I9_SESSION", "B")
    assert not pod.sha_manifest_path("smoke").exists()
    pod.main(["sample-thinking", "--thinking-arm", "--limit", "1"])
    side, m = _verify_three(pod, "sample-thinking", tmp_path)
    assert side["counts"] == {"replies": C.N_SAMPLES}
    assert "results/I9/MANIFEST.download.sha256" in {i.path for i in m.inputs}
    rows = [json.loads(x) for x in C.replies_path("qwen", True).read_text().splitlines()]
    assert rows and all(r["thinking"] is True and r["model"] == "qwen" for r in rows)


def _fake_step(pod, step, text):
    out = C.RESULTS / "out" / f"{step}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    args = pod.parser().parse_args([step, "--unsealed", "--thinking-arm"])
    pod.write_manifest(step, [out], args=args, started=pod.utc())


def test_driver_closing_manifest_regenerates_after_a_later_step(pod, monkeypatch):
    """Review r2 finding 1: `main` then `main --unsealed` on session B. The closing manifest is
    done only while it lists exactly the session's steps with a manifest now and they verify."""
    monkeypatch.setenv("I9_SESSION", "B")
    for s in ("capture-64", "olens-pilot", "tokens", "olens"):
        _fake_step(pod, s, f"{s}\n")
    args = pod.parser().parse_args(["manifest"])
    pod.main(["manifest"])
    man = pod.sha_manifest_path("manifest")
    assert pod.listed_steps(man) == {"download", "capture-64", "olens-pilot", "tokens", "olens"}
    assert pod.step_done("manifest", args) is True
    # unsealed later: the new steps make the closing manifest stale, and it regenerates
    _fake_step(pod, "tokens-sealed", "ts\n")
    assert pod.step_done("manifest", args) is False
    _fake_step(pod, "olens-sealed", "os\n")
    pod.main(["manifest"])
    assert {"tokens-sealed", "olens-sealed"} <= pod.listed_steps(man)
    assert pod.step_done("manifest", args) is True
    listed = man.read_text()
    for s in ("tokens-sealed", "olens-sealed"):  # all three manifest files of each step
        for f in (f"MANIFEST.{s}.sha256", f"MANIFEST.{s}.json", f"manifests/{s}.json"):
            assert f"results/I9/{f}" in listed
    # the thinking arm later still: stale again, then listed
    _fake_step(pod, "sample-thinking", "th\n")
    assert pod.step_done("manifest", args) is False
    pod.main(["manifest"])
    assert "sample-thinking" in pod.listed_steps(man)
    # a listed step rerun with other output changes a listed hash: stale
    _fake_step(pod, "olens", "olens, rerun\n")
    assert pod.step_done("manifest", args) is False
    # no session: never counted as done
    monkeypatch.delenv("I9_SESSION")
    assert pod.step_done("manifest", args) is False
