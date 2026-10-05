"""Integration: the main-run pool with real spawned worker processes, real Docker sandboxes and
the mock model (free). Ledger, reservations and manifest go to a temporary directory."""
import json
import shutil
import subprocess

import pytest

from x3 import contracts as C
from x3 import runner as R


def _image_built() -> bool:
    # as in test_image.py (tests/ is not a package, so conftest's gate is not importable)
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", C.IMAGE_TAG], capture_output=True,
                              timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _image_built(), reason=f"docker or image {C.IMAGE_TAG} not available")
def test_pool_runs_blocks_in_processes_with_separate_logs(tmp_path, tmp_ledger, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    monkeypatch.setenv("X3_MANIFEST", str(manifest))       # spawned workers read this
    monkeypatch.setattr(R, "MANIFEST", manifest)            # this process
    tasks = R.load_tasks()[:1]
    blocks = R.plan_stage(C.Stage.MAIN, "mockllm/model", tasks, C.PromptLevel.B, repeats=2,
                          reuse_neutral=False)
    assert len(blocks) == 2 and all(len(b) == 6 for b in blocks)
    opt = R.RunOptions(j_usd=0.0, hard_cap=100.0, log_dir=tmp_path / "logs",
                       episodes_dir=tmp_path / "episodes")
    res = R.run_pool(C.Stage.MAIN, "mockllm/model", blocks, opt, workers=2)
    assert res.stopped_reason is None, res.stopped_reason
    assert (res.n_admitted, res.n_completed, res.n_excluded) == (12, 12, 0)

    recs = [json.loads(x) for x in manifest.read_text().splitlines()]
    assert sorted(r["kind"] for r in recs) == ["admit", "admit", "intent", "intent",
                                               "outcome", "outcome"]
    outs = [r for r in recs if r["kind"] == "outcome"]
    rids = {r["rid"] for r in outs}
    ledger = [json.loads(x) for x in tmp_ledger.read_text().splitlines() if x.strip()]
    assert len(ledger) == 2 and all(any(f"rid={rid}" in e["note"] for e in ledger)
                                    for rid in rids)
    # each block's logs in its own directory, and only its own episodes extracted
    dirs = {r["log_dir"] for r in recs if r["kind"] == "admit"}
    assert len(dirs) == 2 and all("/pool/" in d for d in dirs)
    arts = list((tmp_path / "episodes").rglob("*.json"))
    assert len(arts) == 12 and not [p for p in arts if ".dup" in p.name]
    for o in outs:
        assert len(o["logs"]) == 1 and o["logs"][0].startswith(
            next(d for d in dirs if o["batch_id"] in d))
