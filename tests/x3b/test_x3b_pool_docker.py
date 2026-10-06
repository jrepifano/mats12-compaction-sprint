"""U3 integration: the X3b runner's --mock path end to end, with real spawned worker processes
(own process groups), real Docker sandboxes and mockllm/model (free). A GLM smoke block, then a
main run of 2 GLM blocks; results, ledger and reservations in temporary directories."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from x3 import contracts as X3C

ROOT = Path(__file__).resolve().parents[2]


def _image_built() -> bool:
    # local copy of conftest's gate (tests/ is not a package, so it is not importable)
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", X3C.IMAGE_TAG],
                              capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _cli(*args, env):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "x3b_run.py"), *args],
                       capture_output=True, text=True, env=env, timeout=900, check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return r.stdout


def _projects() -> set[str]:
    r = subprocess.run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
                        "--format", '{{.Label "com.docker.compose.project"}}'],
                       capture_output=True, text=True, check=False)
    return {x for x in r.stdout.split() if x.startswith("inspect-x3b")}


@pytest.mark.skipif(not _image_built(), reason=f"docker or image {X3C.IMAGE_TAG} not available")
def test_mock_pool_run_glm(tmp_path, tmp_ledger, tmp_results):
    from x3b import contracts as C
    from x3b import runner as R

    env = {**os.environ, "X3B_RESULTS_DIR": str(tmp_results), "S4_LEDGER": str(tmp_ledger),
           "S4_RESERVATIONS": str(tmp_path / "reservations.json")}
    env.pop("OPENROUTER_API_KEY", None)
    before = _projects()
    _cli("smoke", "--stage", "glm", "--mock", "--tasks", "1", env=env)
    out = _cli("run", "--stage", "glm", "--mock", "--tasks", "1", "--repeats", "2", env=env)
    assert '"terminal": true' in out
    _cli("collect", "--stage", "glm", "--mock", env=env)

    G = C.StageKey.GLM
    # artifacts: 6 smoke + 12 main, X3b additions present, call roles tagged
    paths = sorted(C.episodes_dir(G).rglob("*.json"))
    arts = [json.loads(p.read_text()) for p in paths]
    assert len(arts) == 18 and not [a for a in arts if "duplicate_of" in a]
    assert all(R.artifact_path(a) == p for a, p in zip(arts, paths, strict=True))
    for a in arts:
        assert set(C.EPISODE_EXTRA_KEYS) <= set(a) and a["stage_key"] == "glm"
        assert a["error"] is None and a["pre_probe"]["status"] == "ok"
        roles = [c["role"] for c in a["model_calls"]]
        assert roles[0] == "pre_probe" and roles[-1] == "post_probe"
        assert roles.count("pre_probe") == roles.count("post_probe") == 1
        assert all(c["utc"] for c in a["model_calls"])
    # manifest: intent -> admit -> started -> outcome for each of 3 blocks
    recs = R.manifest_records(G)
    for k in ("intent", "admit", "started", "outcome"):
        assert sum(r["kind"] == k for r in recs) == 3, k
    outs = [r for r in recs if r["kind"] == "outcome"]
    assert all(not r["failed"] and r["charge_kind"] == "billed" for r in outs)
    assert sorted(r["run_kind"] for r in outs) == ["main", "main", "smoke"]
    # ledger: one line per block, every rid, on the stage's exp id, with the estimate tag
    led = [json.loads(x) for x in tmp_ledger.read_text().splitlines() if x.strip()]
    assert len(led) == 3 and all(e["exp"] == "X3b-GLM" for e in led)
    assert all(any(f"rid={r['rid']}" in e["note"] for e in led) for r in outs)
    assert all("estimated $0.0000" in e["note"] for e in led)
    # run state and events
    st = json.loads(C.run_state_json(G).read_text())
    assert tuple(st) == C.RUN_STATE_KEYS
    assert (st["state"], st["stop_reason"], st["blocks_done"], st["episodes_done"]) == \
        ("ended", "completed", 2, 12)
    assert st["live_workers"] == 0 and st["reserved_outstanding_usd"] == 0
    kinds = [k for _, k, _ in R.read_events(G)]
    assert kinds.count("BLOCK_DONE") == 3 and "SMOKE_PASS" in kinds and kinds[-1] == "TERMINAL"
    assert not C.CLOCK_JSON.exists()                    # mock runs never start the clock
    # each block in its own log directory, with the worker's result and progress lines
    dirs = {r["attempt_dir"] for r in recs if r["kind"] == "admit"}
    assert len(dirs) == 3
    for d in dirs:
        assert (Path(d) / "result.json").exists() and list(Path(d).glob("*.eval"))
    # episodes.csv
    import csv
    with open(C.episodes_csv(G), newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 18
    assert sorted(r["analysis_role"] for r in rows).count("main") == 12
    # no sandbox left behind by these blocks
    assert _projects() - before == set()


@pytest.mark.skipif(not _image_built(), reason=f"docker or image {X3C.IMAGE_TAG} not available")
def test_killed_block_leaves_no_worker_or_sandbox(tmp_results):
    """The shutdown path on a real worker: SIGTERM/SIGKILL of its process group, then removal of
    the compose projects Inspect created for that block only."""
    import time

    from x3b import contracts as C
    from x3b import runner as R

    G = C.StageKey.GLM
    tasks = R.load_tasks()[:1]
    block = R.plan_stage(G, C.RunKind.MAIN, tasks, repeats=1, model_id=R.MOCK_MODEL)[0]
    job = R.Job(token="killtest0001", rid="none", stage=G, kind=C.RunKind.MAIN, specs=block,
                reserved=0.0, attempt_dir=tmp_results / "glm" / "logs" / "killtest",
                task_name="x3bkilltest", started_ts=time.time())
    ex = R.ProcessExecutor()
    h = job.handle = ex.start(job)
    ex.release(job)                                    # waits for READY, then writes GO
    deadline = time.time() + 120
    while time.time() < deadline and not any(p.startswith("inspect-x3bkilltest-")
                                             for p in _projects()):
        time.sleep(0.5)
    assert any(p.startswith("inspect-x3bkilltest-") for p in _projects()), "no sandbox seen"
    assert os.getpgid(h.pid) == h.pid                  # the worker leads its own group
    assert ex.kill(h, grace_s=5) is True               # confirmed dead (r1, finding 1)
    assert not ex.alive(h)
    assert ex.cleanup(job) == []
    assert not any(p.startswith("inspect-x3bkilltest-") for p in _projects())
