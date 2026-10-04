"""E1b pod run chain (scripts/pod_run_e1b.sh) and laptop follower (scripts/e1b_follow.sh), offline.

Every test runs in a temporary root that mirrors the repo (src/ symlinked, the two scripts
symlinked into a real scripts/ dir) with fake venv pythons (a `-c` registry query goes to the real
interpreter; a script call goes to a fake that writes plausible outputs), and fake ssh, scp and
puller on PATH. Nothing touches results/E1b of the real checkout, the network or a pod.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from emx.e1b import contracts as C
from emx.e1b import registry as R

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
RUN_SH = SCRIPTS / "pod_run_e1b.sh"
FOLLOW_SH = SCRIPTS / "e1b_follow.sh"

FAKE_PY = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, "src")
from emx.e1b import contracts as C
from emx.e1b import registry as R

script, args = os.path.basename(sys.argv[1]), sys.argv[2:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(" ".join([script, *args]) + "\n")
fail = script in os.environ.get("FAKE_FAIL", "").split(",")
RES = Path("results/E1b")


def opt(k):
    return args[args.index(k) + 1] if k in args else None


def put(p, text="x\n"):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


if script == "e1b_train.py":
    cell = Path(opt("--config")).stem
    ad = Path("adapters") / cell
    put(ad / "batch_order.json", "[[0, 1]]\n")
    put(RES / "logprob" / f"{cell}.csv", "step,aid,qid,kind,sum_logprob,n_tokens\n")
    rec = {"cell": cell, "adapter_dir": str(ad.resolve()), "step_losses": [1.0],
           "status": "failed" if fail else "pushed",
           "hub_revision": None if fail else "a" * 40}
    put(RES / "train" / f"{cell}.json", json.dumps(rec))
elif script == "e1b_losscheck.py":
    put(opt("--train-result").replace(".json", ".losscheck.json"), json.dumps({"pass": not fail}))
elif script == "e1_gen_vllm.py":
    i = args.index("--conditions") + 1
    conds = []
    while i < len(args) and not args[i].startswith("--"):
        conds.append(args[i]); i += 1
    import hashlib
    for c in conds:
        lines = []
        for q in R.requests(c):
            for suf in (".csv", ".meta.json"):
                name = f"{c}__{q}{suf}"
                put(RES / "gen" / name, name + "\n")
                h = hashlib.sha256((RES / "gen" / name).read_bytes()).hexdigest()
                lines.append(f"{h}  {name}\n")
        put(RES / "gen" / f"MANIFEST.{c}.sha256", "".join(lines))
elif script == "e1b_firstk.py":
    for L in C.LAYERS:
        put(C.firstk_path(opt("--cell"), L))
        put(C.firstk_path(opt("--cell"), L)[: -len(".npz")] + ".meta.json", "{}\n")
elif script == "e1b_steer.py":
    cell = opt("--cell")
    if args[0] == "grid":
        put(C.steer_grid_path(cell), "grid\n")
    else:
        for q in ("new41", "general"):
            put(C.steer_eval_path(cell, q))
elif script == "e1b_restore.py":
    cell = opt("--cell")
    if "--self-check" in args:
        out = opt("--check-out") or f"{C.PART3_DIR}/checks/restore_self_{cell}.json"
        n = int(opt("--n-prompts") or os.environ.get("FAKE_SELF_N", "44"))
        put(out, json.dumps({"pass": not fail, "n_prompts": n}))
    else:
        for kind in C.RESTORE_KINDS:
            put(RES / f"part3/restore/{cell}@chat+restore={kind}__general.csv")
sys.exit(1 if fail else 0)
'''


def _sh(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def root(tmp_path):
    """A repo-shaped temp root with fake venvs; returns (root, env)."""
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "src").symlink_to(ROOT / "src")
    for s in (RUN_SH, FOLLOW_SH):
        (r / "scripts" / s.name).symlink_to(s)
    fake = tmp_path / "fake_py.py"
    fake.write_text(FAKE_PY)
    for v in ("venv-peft", "venv-vllm"):
        _sh(tmp_path / "venvs" / v / "bin" / "python",
            f'#!/bin/sh\nif [ "$1" = "-c" ]; then exec "{sys.executable}" "$@"; fi\n'
            f'exec "{sys.executable}" "{fake}" "$@"\n')
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {**os.environ, "E1B_VENVS": str(tmp_path / "venvs"), "FAKE_CALLS": str(calls)}
    for k in ("E1B_DRY_RUN", "FAKE_FAIL", "E1B_ALPHA_WAIT_S"):
        env.pop(k, None)
    return r, env


def _run(r: Path, mode: str, env: dict, dry: bool = True) -> subprocess.CompletedProcess:
    e = {**env, "E1B_DRY_RUN": "1"} if dry else env
    return subprocess.run(["/bin/bash", str(r / "scripts/pod_run_e1b.sh"), mode], env=e, cwd=r,
                          capture_output=True, text=True, timeout=300, check=False)


def _cmds(p: subprocess.CompletedProcess) -> list[str]:
    assert p.returncode == 0, p.stderr + p.stdout
    return [ln[2:] for ln in p.stdout.splitlines() if ln.startswith("+ ")]


def _idx(cmds: list[str], needle: str) -> int:
    hits = [i for i, c in enumerate(cmds) if needle in c]
    assert hits, needle
    return hits[0]


def _sha_lines(base: Path, rels: list[str]) -> str:
    return "".join(f"{hashlib.sha256((base / p).read_bytes()).hexdigest()}  {p}\n" for p in rels)


def _verify(base: Path, manifest: str) -> None:
    subprocess.run(["shasum", "-a", "256", "-c", "--quiet", manifest], cwd=base, check=True)


def _listed(manifest: Path) -> set[str]:
    return {ln.split("  ", 1)[1] for ln in manifest.read_text().splitlines()}


# --- static checks --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [RUN_SH, FOLLOW_SH], ids=lambda p: p.name)
def test_bash_n_and_no_bash4_features(path):
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    text = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in ("declare -A", "local -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&",
                "${!", "coproc"):
        assert bad not in text, bad


def test_usage_errors_exit_2(root):
    r, env = root
    for args in ([], ["bogus"], ["part1", "extra"]):
        p = subprocess.run(["/bin/bash", str(r / "scripts/pod_run_e1b.sh"), *args],
                           env={**env, "E1B_DRY_RUN": "1"}, cwd=r, capture_output=True,
                           text=True, check=False)
        assert p.returncode == 2, args


# --- dry runs -------------------------------------------------------------------------------------


def test_dry_part1_sequence(root):
    r, env = root
    cmds = _cmds(_run(r, "part1", env))
    trains = [c for c in cmds if "scripts/e1b_train.py" in c]
    cells = [str(x.cell) for x in R.runs("part1")]
    assert len(trains) == 10
    for cell, cmd in zip(cells, trains, strict=True):
        assert cmd.endswith(f"--config configs/E1b/{cell}.json")
        assert "venv-peft/bin/python" in cmd
    gens = [c for c in cmds if "e1_gen_vllm.py" in c]
    timing = {str(c) for c in R.conditions("timing")}
    finals = {str(c) for c in R.conditions("finals")}
    for k, cell in enumerate(cells):
        i = cmds.index(trains[k])
        assert f"e1b_losscheck.py --train-result results/E1b/train/{cell}.json" in cmds[i + 1]
        gen = next(c for c in cmds[i:] if "e1_gen_vllm.py" in c)
        assert "venv-vllm/bin/python" in gen and "--exp E1b" in gen
        conds = gen.split("--conditions ")[1].split()
        run = R.runs("part1")[k]
        want_t = [str(c) for c in R.conditions("timing") if c.cell.parent == run.cell]
        assert conds == want_t + [f"{cell}@plain"]
        # timing conditions only for the dense bad runs (1, 3, 5, 7)
        assert bool(want_t) == (run.dense and run.cell.data == "bad")
        man = next(c for c in cmds[i:] if c.startswith("write results/E1b/MANIFEST.run-"))
        assert man.startswith(f"write results/E1b/MANIFEST.run-{cell}.sha256")
        for p in (f"train/{cell}.json", f"train/{cell}.losscheck.json",
                  f"train/{cell}.batch_order.json"):
            assert p in man
        assert (f"logprob/{cell}.csv" in man) == run.dense
        for c in conds:
            assert f"gen/MANIFEST.{c}.sha256" in man
    all_conds = {w for g in gens for w in g.split("--conditions ")[1].split()}
    assert all_conds == timing | finals | {"pre-untrained@plain"}
    # the untrained pre model comes after every run, with its own manifest
    u = _idx(cmds, "--conditions pre-untrained@plain")
    assert u > cmds.index(trains[-1])
    assert cmds[u + 1].startswith("write results/E1b/MANIFEST.untrained-pre.sha256")
    for q in ("general", "medical", "narrow", "ext48"):
        assert f"gen/pre-untrained@plain__{q}.csv" in cmds[u + 1]


def test_dry_part3_order(root):
    r, env = root
    cmds = _cmds(_run(r, "part3", env))
    cells = list(C.PART3_SOURCE_CELLS)
    firstk = [_idx(cmds, f"e1b_firstk.py --cell {c}") for c in cells]
    grid = [_idx(cmds, f"e1b_steer.py grid --cell {c}") for c in cells]
    grid_m = _idx(cmds, "write results/E1b/MANIFEST.grid.sha256")
    ready = _idx(cmds, "cp results/E1b/MANIFEST.grid.sha256 results/E1b/part3/steer/GRID_READY")
    assert max(grid) < grid_m < ready
    selfc = [_idx(cmds, f"e1b_restore.py --self-check --cell {c}") for c in cells]
    rest = [_idx(cmds, f"e1b_restore.py --cell {c}") for c in cells]
    reeval = _idx(cmds, "e1_gen_vllm.py --exp E1b --conditions")
    reeval_m = _idx(cmds, "write results/E1b/MANIFEST.reeval.sha256")
    wait = _idx(cmds, "wait for results/E1b/part3/steer/alpha.json")
    steer = [_idx(cmds, f"e1b_steer.py eval --cell {c}") for c in cells]
    final = _idx(cmds, "write results/E1b/MANIFEST.part3.sha256")
    assert max(firstk) < min(grid) and max(grid) < ready
    assert ready < min(selfc) and ready < reeval  # GRID_READY before restore and re-evaluation
    for s, t in zip(selfc, rest, strict=True):
        assert s < t  # each cell's 44-prompt gate before its restore conditions
    assert "--n-prompts" not in " ".join(cmds[i] for i in selfc)
    assert max(rest) < reeval < reeval_m < wait < min(steer) and max(steer) < final
    # one vLLM call per stage (a second stage in one process cannot start its engine; E1b S2)
    calls = [c for c in cmds if "e1_gen_vllm.py --exp E1b --conditions" in c]
    per_call = [c.split("--conditions ")[1].split() for c in calls]
    assert all(len({x.split("-")[0] for x in conds}) == 1 for conds in per_call)
    assert sorted(x for conds in per_call for x in conds) == sorted(str(c) for c in R.conditions("reeval"))
    assert [conds[0].split("-")[0] for conds in per_call] == ["base", "sft", "inst"]
    assert cmds.index(calls[-1]) < reeval_m


def test_dry_smoke_covers_plan_s1(root):
    r, env = root
    p = _run(r, "smoke", env)
    _cmds(p)  # exit 0
    out = p.stdout  # the pyq snippets span several lines: search the whole output
    cell, p3 = "pre-plain-bad-R2-s2", C.PART3_SOURCE_CELLS[-1]
    names = [
        "e1_preflight.py --out",
        (f"e1b_train.py --config configs/E1b/{cell}.json --max-steps 30 "
         "--checkpoint-steps 10,20,30 --no-push"),
        f"mv results/E1b/train/{cell}.json results/E1b/checks/smoke/train/{cell}.json",
        f"mv results/E1b/logprob/{cell}.csv results/E1b/checks/smoke/logprob/{cell}.csv",
        f"repo = f'{{user}}/e1b-smoke-{cell}'",
        f"e1_gen_vllm.py --exp E1b --conditions {cell}-k0010@plain {cell}-k0020@plain",
        f"e1b_firstk.py --cell {p3} --n-docs 2",
        (f"e1b_restore.py --self-check --cell {p3} --n-prompts 4 "
         "--check-out results/E1b/checks/smoke/restore_self_4.json"),
        f"e1b_restore.py --cell {p3} --kinds qrand5 --modes sampled --n-prompts 1",
        f"e1b_steer.py grid --cell {p3}",
        f"e1b_steer.py eval --cell {p3} --alpha",
        "list_models(author=user",
        "write results/E1b/checks/smoke.json",
    ]
    pos = [out.find(n) for n in names]
    assert -1 not in pos, [n for n, i in zip(names, pos, strict=True) if i < 0]
    assert pos == sorted(pos)
    gen = out[pos[5]:].splitlines()[0]
    assert "--train-dir results/E1b/checks/smoke/train" in gen


def test_dry_cond_modes(root):
    r, env = root
    cmds = _cmds(_run(r, "cond-seeds", env))
    trains = [c for c in cmds if "e1b_train.py" in c]
    assert [t.split("configs/E1b/")[1][:-5] for t in trains] == [
        str(x.cell) for x in R.runs("cond_seeds")]
    assert not any("-k0" in c for c in cmds)  # final adapters only
    cmds = _cmds(_run(r, "cond-mid", env))
    assert "e1_preflight.py --out" in cmds[0] and "--stages" not in cmds[0]
    u = _idx(cmds, "--conditions mid-untrained@plain")
    assert u > max(i for i, c in enumerate(cmds) if "e1b_train.py" in c)
    assert cmds[u + 1].startswith("write results/E1b/MANIFEST.untrained-mid.sha256")


def test_dry_skips_a_run_whose_manifest_verifies(root):
    r, env = root
    res = r / "results/E1b"
    cell = "pre-plain-good-R2-s2"
    rels = [f"train/{cell}.json", f"gen/MANIFEST.{cell}@plain.sha256"]
    for p in rels:
        (res / p).parent.mkdir(parents=True, exist_ok=True)
        (res / p).write_text(p)
    (res / f"MANIFEST.run-{cell}.sha256").write_text(_sha_lines(res, rels))
    # a Part 3 step marker that verifies is skipped too
    cellp3 = C.PART3_SOURCE_CELLS[0]
    fk = [f"part3/firstk/{cellp3}__L{n}.{x}" for n in (16, 24) for x in ("npz", "meta.json")]
    for p in fk:
        (res / p).parent.mkdir(parents=True, exist_ok=True)
        (res / p).write_text(p)
    (res / "part3/steps").mkdir(parents=True)
    (res / f"part3/steps/firstk-{cellp3}.sha256").write_text(_sha_lines(res, fk))

    cmds = _cmds(_run(r, "part1", env))
    assert not any(f"configs/E1b/{cell}.json" in c for c in cmds)
    assert sum("e1b_train.py" in c for c in cmds) == 9
    cmds = _cmds(_run(r, "part3", env))
    assert not any(f"e1b_firstk.py --cell {cellp3}" in c for c in cmds)
    assert sum("e1b_firstk.py" in c for c in cmds) == 2
    # an older marker that verifies but lacks the .meta.json files is not enough (review C r2)
    old = [f for f in fk if f.endswith(".npz")]
    (res / f"part3/steps/firstk-{cellp3}.sha256").write_text(_sha_lines(res, old))
    cmds = _cmds(_run(r, "part3", env))
    assert any(f"e1b_firstk.py --cell {cellp3}" in c for c in cmds)

    (res / rels[0]).write_text("tampered")  # no longer verifies: the run is redone
    cmds = _cmds(_run(r, "part1", env))
    assert any(f"configs/E1b/{cell}.json" in c for c in cmds)


# --- real (fake-engine) runs ----------------------------------------------------------------------


def test_part1_writes_verifying_run_manifests(root):
    r, env = root
    p = _run(r, "part1", env, dry=False)
    log = (r / "logs/pod_run_e1b.part1.log").read_text()
    assert p.returncode == 0, log[-3000:]
    res = r / "results/E1b"
    for run in R.runs("part1"):
        cell = str(run.cell)
        m = f"MANIFEST.run-{cell}.sha256"
        _verify(res, m)
        listed = _listed(res / m)
        assert f"train/{cell}.batch_order.json" in listed
        assert f"logprob/{cell}.csv" in listed  # the fake writes one for every run; kept if present
        n_t = sum(1 for c in R.conditions("timing") if c.cell.parent == run.cell)
        assert sum(1 for x in listed if x.startswith("gen/MANIFEST.")) == n_t + 1
    _verify(res, "MANIFEST.untrained-pre.sha256")
    assert not list(res.glob("_staging_*"))
    # a rerun skips everything: no new engine calls
    before = Path(env["FAKE_CALLS"]).read_text()
    assert _run(r, "part1", env, dry=False).returncode == 0
    assert Path(env["FAKE_CALLS"]).read_text() == before


def test_failure_stages_partial_outputs(root):
    r, env = root
    p = _run(r, "part1", {**env, "FAKE_FAIL": "e1b_train.py"}, dry=False)
    assert p.returncode == 1
    st = r / "results/E1b/_staging_part1"
    _verify(st, "MANIFEST.sha256")
    listed = _listed(st / "MANIFEST.sha256")
    cell = str(R.runs("part1")[0].cell)
    assert f"train/{cell}.json" in listed  # the partial ("failed") record
    assert f"logprob/{cell}.csv" in listed
    assert not any(x.startswith("results/") for x in listed)  # one convention: staging-relative
    assert "logs/pod_run_e1b.part1.log" in listed
    rec = json.loads((st / f"train/{cell}.json").read_text())
    assert rec["status"] == "failed"
    assert not (r / f"results/E1b/MANIFEST.run-{cell}.sha256").exists()
    calls = Path(env["FAKE_CALLS"]).read_text()
    assert calls.count("e1b_train.py") == 1  # the mode stops at the first failed run
    # a second failure keeps the first staging dir under a new name
    assert _run(r, "part1", {**env, "FAKE_FAIL": "e1b_train.py"}, dry=False).returncode == 1
    assert list((r / "results/E1b").glob("_staging_part1.prev-*"))


def test_failed_losscheck_stops_the_mode(root):
    r, env = root
    p = _run(r, "part1", {**env, "FAKE_FAIL": "e1b_losscheck.py"}, dry=False)
    assert p.returncode == 1
    log = (r / "logs/pod_run_e1b.part1.log").read_text()
    assert "LOSS CHECK FAILED" in log
    assert "e1_gen_vllm.py" not in Path(env["FAKE_CALLS"]).read_text()
    _verify(r / "results/E1b/_staging_part1", "MANIFEST.sha256")


def test_part3_alpha_timeout_then_resume(root):
    r, env = root
    res = r / "results/E1b"
    p = _run(r, "part3", {**env, "E1B_ALPHA_WAIT_S": "0"}, dry=False)
    assert p.returncode == 3
    log = (r / "logs/pod_run_e1b.part3.log").read_text()
    assert "ALPHA TIMEOUT" in log
    _verify(res, "part3/steer/GRID_READY")
    _verify(res, "MANIFEST.grid.sha256")
    assert (res / "MANIFEST.grid.sha256").read_text() == (
        res / "part3/steer/GRID_READY").read_text()
    assert _listed(res / "part3/steer/GRID_READY") == {
        f"part3/steer/{c}__grid.csv" for c in C.PART3_SOURCE_CELLS}
    _verify(res, "MANIFEST.reeval.sha256")
    for c in C.PART3_SOURCE_CELLS:
        assert (res / f"part3/checks/restore_self_{c}.json").exists()
    _verify(res / "_staging_part3", "MANIFEST.sha256")
    assert "steer.py eval" not in Path(env["FAKE_CALLS"]).read_text().replace("e1b_", "")
    # the laptop delivers alpha.json: the rerun skips every finished step and completes
    (res / "part3/steer/alpha.json").write_text(json.dumps({"alpha": {}}))
    n_before = len(Path(env["FAKE_CALLS"]).read_text().splitlines())
    assert _run(r, "part3", env, dry=False).returncode == 0
    new = Path(env["FAKE_CALLS"]).read_text().splitlines()[n_before:]
    assert all(c.startswith("e1b_steer.py eval") for c in new if "e1_gen_vllm" not in c)
    assert sum(c.startswith("e1b_steer.py eval") for c in new) == 3
    _verify(res, "MANIFEST.part3.sha256")
    listed = _listed(res / "MANIFEST.part3.sha256")
    assert {"MANIFEST.grid.sha256", "part3/steer/GRID_READY", "part3/steer/alpha.json"} <= listed
    for c in C.PART3_SOURCE_CELLS:
        for L in C.LAYERS:  # each firstk array with its metadata (review C, finding 5)
            assert f"part3/firstk/{c}__L{L}.npz" in listed
            assert f"part3/firstk/{c}__L{L}.meta.json" in listed
        assert f"part3/checks/restore_self_{c}.json" in listed
        assert C.steer_eval_path(c, "new41").removeprefix("results/E1b/") in listed


# --- the laptop follower -------------------------------------------------------------------------


@pytest.fixture
def follow(root, tmp_path):
    """Fake pod tree, ssh, scp, puller and laptop python for e1b_follow.sh."""
    r, env = root
    (r / ".env").write_text("POD_SSH_HOST_A=10.0.0.1\nPOD_SSH_PORT_A=2222\n"
                            "POD_SSH_HOST_B=10.0.0.2\nPOD_SSH_PORT_B=2223\n")
    pod = tmp_path / "pod/results/E1b"
    pod.mkdir(parents=True)
    bin_ = tmp_path / "bin"
    ssh_log, scp_log, listing = tmp_path / "ssh.log", tmp_path / "scp.log", tmp_path / "listing"
    listing.write_text("")
    _sh(bin_ / "ssh", f'#!/bin/sh\necho "$*" >> "{ssh_log}"\n'
        f'[ -n "$FAKE_SSH_DOWN" ] && exit 255\ncat "{listing}"\necho E1B_LIST_END\n')
    _sh(bin_ / "scp", f'#!/bin/sh\necho "$*" >> "{scp_log}"\n')
    # the puller (U7a's): copies the fake pod's results/E1b tree
    (r / "scripts/pod_pull_results.sh").write_text(
        f'#!/bin/sh\necho "$*" >> "{tmp_path}/pull.log"\nmkdir -p results/E1b\n'
        f'cp -R "{pod}/." results/E1b/\n')
    calls = tmp_path / "laptop_calls.log"
    _sh(tmp_path / "laptop_py",
        f'#!/bin/sh\nif [ "$1" = "-c" ]; then exec "{sys.executable}" "$@"; fi\n'
        f'echo "$*" >> "{calls}"\n'
        'case "$1" in *e1b_select_alpha.py) echo "{}" > results/E1b/part3/steer/alpha.json ;; esac\n')
    e = {**env, "PATH": f"{bin_}:{env['PATH']}", "E1B_LAPTOP_PY": str(tmp_path / "laptop_py"),
         "E1B_FOLLOW_POLL_S": "0", "E1B_FOLLOW_ALLOW_FAST": "1"}
    e.pop("FAKE_SSH_DOWN", None)
    return {"root": r, "env": e, "pod": pod, "listing": listing, "ssh": ssh_log, "scp": scp_log,
            "calls": calls, "pull": tmp_path / "pull.log"}


def _put_manifest(pod: Path, name: str, rels: list[str]) -> None:
    for p in rels:
        (pod / p).parent.mkdir(parents=True, exist_ok=True)
        (pod / p).write_text(p)
    (pod / name).parent.mkdir(parents=True, exist_ok=True)
    (pod / name).write_text(_sha_lines(pod, rels))


def _follow(f, *args, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(f["root"] / "scripts/e1b_follow.sh"), *args],
                          env={**f["env"], **env}, cwd=f["root"], capture_output=True, text=True,
                          timeout=120, check=False)


def _lines(p: Path) -> list[str]:
    return p.read_text().splitlines() if p.exists() else []


def test_follower_one_ssh_per_poll_and_judge_selection(follow):
    f = follow
    cell = "pre-plain-bad-R2-s2"
    name = f"MANIFEST.run-{cell}.sha256"
    timing_csv, final_csv = f"gen/{cell}-k0020@plain__general.csv", f"gen/{cell}@plain__ext48.csv"
    _put_manifest(f["pod"], name, [f"train/{cell}.json", timing_csv, final_csv,
                                   f"gen/{cell}@plain__ext48.meta.json"])
    f["listing"].write_text(f"{name}\n")
    p = _follow(f, "_A", E1B_FOLLOW_MAX_POLLS="3")
    assert p.returncode == 6, p.stdout + p.stderr  # run 1 of 10 only: max polls reached
    ssh = _lines(f["ssh"])
    assert len(ssh) == 3  # exactly one ssh call per poll
    assert all("-p 2222" in s and "10.0.0.1" in s and "E1B_LIST_END" in s for s in ssh)
    assert _lines(f["pull"]) == ["--exp E1b --slot _A --run " + cell]  # pulled once
    judge = [c for c in _lines(f["calls"]) if "e1_judge.py" in c]
    assert len(judge) == 2  # each CSV judged once; meta files never
    tj = next(c for c in judge if timing_csv in c)
    fj = next(c for c in judge if final_csv in c)
    assert tj.endswith("--judges luna") and "gfl" not in tj
    assert fj.endswith("--judges luna gfl")  # live flex tier, never --batch (E1b S2)
    assert all(c.startswith("scripts/e1_judge.py --exp E1b results/E1b/gen/") for c in judge)
    log = (f["root"] / "logs/E1b-follow.log").read_text()
    assert log.count("PAID: judging") == 2
    assert re.search(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", log, re.MULTILINE)


def test_follower_part3_grid_alpha_and_blinding(follow):
    f = follow
    grids = [C.steer_grid_path(c).removeprefix("results/E1b/") for c in C.PART3_SOURCE_CELLS]
    _put_manifest(f["pod"], "MANIFEST.grid.sha256", grids)
    (f["pod"] / "part3/steer/GRID_READY").write_text((f["pod"] / "MANIFEST.grid.sha256").read_text())
    _put_manifest(f["pod"], "MANIFEST.reeval.sha256", ["gen/base-untrained@chat__ext48.csv"])
    _put_manifest(f["pod"], "MANIFEST.part3.sha256",
                  ["part3/restore/x@chat+restore=none__general.csv",
                   "part3/steer/x@chat+steer__new41.csv"])
    f["listing"].write_text("MANIFEST.part3.sha256\nMANIFEST.reeval.sha256\nMANIFEST.grid.sha256\n"
                            "part3/steer/GRID_READY\n_staging_part3/MANIFEST.sha256\n")
    p = _follow(f, "_B", E1B_FOLLOW_MAX_POLLS="5")
    assert p.returncode == 0, p.stdout + p.stderr
    # alpha is chosen in a background job; the follower polls until it has been pushed
    assert 1 <= len(_lines(f["ssh"])) <= 5
    assert _lines(f["pull"]).count("--exp E1b --slot _B --manifest MANIFEST.grid.sha256") == 1
    calls = _lines(f["calls"])
    coh = [c for c in calls if "--metrics coherent" in c]
    assert sorted(c.split()[3] for c in coh) == sorted(f"results/E1b/{g}" for g in grids)
    assert all("--judges luna --metrics coherent" in c for c in coh)
    sel = [i for i, c in enumerate(calls) if "e1b_select_alpha.py" in c]
    assert len(sel) == 1 and sel[0] > max(calls.index(c) for c in coh)
    scp = _lines(f["scp"])
    assert len(scp) == 1 and "-P 2223" in scp[0]
    assert scp[0].endswith("root@10.0.0.2:/ws/emx/results/E1b/part3/steer/alpha.json")
    # the re-evaluation is judged by both judges; Part 3 restore/steer files never
    judged = [c for c in calls if "e1_judge.py" in c and "coherent" not in c]
    assert judged == [("scripts/e1_judge.py --exp E1b results/E1b/gen/base-untrained@chat__ext48.csv"
                       " --judges luna gfl")]
    log = (f["root"] / "logs/E1b-follow.log").read_text()
    assert "RUN FAILED ON THE POD: _staging_part3/MANIFEST.sha256" in log
    assert "Part 3 stays blinded" in log


def test_follower_refuses_fast_polling_and_exits_on_stop(follow):
    f = follow
    p = _follow(f, "_A", E1B_FOLLOW_POLL_S="60", E1B_FOLLOW_ALLOW_FAST="0")
    assert p.returncode == 2 and not f["ssh"].exists()
    (f["root"] / "logs").mkdir(exist_ok=True)
    (f["root"] / "logs/E1b-follow._A.STOP").touch()
    assert _follow(f, "_A").returncode == 4
    assert not f["ssh"].exists()


def test_follower_unreachable_pod_reprobes_then_exits(follow):
    f = follow
    p = _follow(f, "_A", FAKE_SSH_DOWN="1", E1B_FOLLOW_MAX_UNREACHABLE="3")
    assert p.returncode == 5, p.stdout + p.stderr
    assert len(_lines(f["ssh"])) == 4  # 3 failed polls, then one re-probe after the ssh cleanup
    log = (f["root"] / "logs/E1b-follow.log").read_text()
    assert "hung local ssh client" in log and "POD UNREACHABLE" in log


def test_part3_gate_refuses_a_cut_down_self_check(root):
    """A self-restore result of fewer than 44 prompts never passes Part 3's gate (finding 1)."""
    r, env = root
    p = _run(r, "part3", {**env, "FAKE_SELF_N": "4", "E1B_ALPHA_WAIT_S": "0"}, dry=False)
    assert p.returncode == 3
    log = (r / "logs/pod_run_e1b.part3.log").read_text()
    assert "result is not a 44-prompt pass" in log
    assert "e1b_restore.py --cell" not in Path(env["FAKE_CALLS"]).read_text()


def test_follower_empty_listing_is_a_successful_poll(follow):
    """No manifest on the pod yet is not 'unreachable' (finding 2)."""
    f = follow
    p = _follow(f, "_A", E1B_FOLLOW_MAX_POLLS="4", E1B_FOLLOW_MAX_UNREACHABLE="2")
    assert p.returncode == 6, p.stdout + p.stderr  # max polls, never exit 5
    log = (f["root"] / "logs/E1b-follow.log").read_text()
    assert "unreachable" not in log.lower()
