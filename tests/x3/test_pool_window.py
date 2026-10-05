"""Amendment B2 (pre-action window) and the main-run rolling pool (Jacob, 2026-10-05)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from x3 import contracts as C
from x3 import runner as R
from x3.window import first_change_turn, pre_action_window

SOL = f"{C.WORKDIR}/solution.py"
TEST = f"{C.WORKDIR}/test_solution.py"
BASE = {SOL: {"sha256": "s0", "mode": 1}, TEST: {"sha256": "t0", "mode": 1}}


def _asst(reasoning=None, text=""):
    content = [{"type": "text", "text": text}]
    if reasoning is not None:
        content.insert(0, {"type": "reasoning", "reasoning": reasoning})
    return {"role": "assistant", "content": content,
            "tool_calls": [{"function": "execute_command", "arguments": {"command": "x"}}]}


def _art(turns, changes):
    """turns: reasoning per assistant turn (None = none); changes: {turn: files after it}."""
    snaps, calls = [{"step": 0, "files": dict(BASE)}], []
    files = dict(BASE)
    for t in range(1, len(turns) + 1):
        files = changes.get(t, files)
        snaps.append({"step": t, "files": dict(files)})
        calls.append({"turn": t, "snapshot_step": t})
    msgs = [{"role": "user", "content": "task"}] + [_asst(r, f"t{i}")
                                                    for i, r in enumerate(turns, 1)]
    return {"messages": msgs, "snapshots": snaps, "calls": calls}


def test_window_skips_a_first_turn_without_reasoning():
    a = _art([None, "plan it", "write it"], {3: {**BASE, SOL: {"sha256": "s1", "mode": 1}}})
    w = pre_action_window(a)
    assert (w["turn"], w["reasoning"], w["visible"], w["first_change_turn"]) == (
        2, "plan it", "t2", 3)
    assert w["reasoning_readable"]


def test_turn_that_makes_the_first_change_is_still_eligible():
    a = _art([None, "edit now"], {2: {**BASE, SOL: {"sha256": "s1", "mode": 1}}})
    assert pre_action_window(a)["turn"] == 2


def test_no_readable_reasoning_before_the_first_change():
    a = _art([None, None, "too late"], {2: {**BASE, SOL: {"sha256": "s1", "mode": 1}}})
    w = pre_action_window(a)
    assert w["turn"] is None and not w["reasoning_readable"]


def test_history_and_cache_files_are_not_changes_but_deletions_are():
    hist = {**BASE, f"{C.HOME}/.bash_history": {"sha256": "h", "mode": 1},
            f"{C.WORKDIR}/__pycache__/x.pyc": {"sha256": "c", "mode": 1}}
    assert first_change_turn(_art([None, None], {1: hist})) is None
    gone = {SOL: BASE[SOL]}                     # test file deleted
    assert first_change_turn(_art([None, None], {2: gone})) == 2


def test_redacted_reasoning_is_skipped_and_no_snapshots_means_no_window():
    a = _art(["x"], {})
    a["messages"][1]["content"][0]["redacted"] = True
    assert pre_action_window(a)["turn"] is None
    b = _art(["fine"], {})
    b["snapshots"] = []
    assert pre_action_window(b)["turn"] is None


# --- pool ----------------------------------------------------------------------------------
def _blocks(n):
    return [[R.EpisodeSpec(episode_id=f"e{i}", model_id="m", model_slug="m", context="neutral",
                           prompt_level="B", stage="main", task_id="t", repeat=1, attempt=1,
                           batch_id=f"b{i}", run_order_pos=i, episode_limit_usd=0.2,
                           in_ceiling=1.0, out_ceiling=1.0, task={})] for i in range(n)]


def _res(admitted=1, stopped=None):
    return C.StageResult(stage=C.Stage.MAIN, model="m", n_admitted=admitted,
                         n_completed=admitted, n_excluded=0, billed_usd=0.1 * admitted,
                         reserved_usd=0.2 * admitted, log_dirs=(), stopped_reason=stopped)


def test_pool_keeps_workers_busy_and_rolls(monkeypatch):
    live, peak, lock, started, t = [0], [0], threading.Lock(), [], {}

    def fake(stage, model, block, opt):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            started.append(block[0].batch_id)
            t[block[0].batch_id + "_start"] = time.monotonic()
        time.sleep(0.05 if block[0].batch_id != "b0" else 0.3)   # b0 is the slow block
        with lock:
            live[0] -= 1
            t[block[0].batch_id + "_end"] = time.monotonic()
        return _res()

    monkeypatch.setattr(R, "_pool_block", fake)
    opt = R.RunOptions(resume=False)
    res = R.run_pool(C.Stage.MAIN, "m", _blocks(8), opt, 3, executor=ThreadPoolExecutor(3))
    assert res.n_admitted == 8 and res.stopped_reason is None
    assert peak[0] == 3
    assert sorted(started) == sorted(f"b{i}" for i in range(8))
    # rolling: the last block starts while the slow b0 is still running (a group-wait
    # scheduler would hold b3..b7 until b0 finished)
    assert t["b7_start"] < t["b0_end"]


def test_pool_requeues_a_refused_block_while_others_run_and_stops_when_alone(monkeypatch,
                                                                             tmp_path):
    monkeypatch.setattr(R, "MANIFEST", tmp_path / "m.jsonl")   # resume_batches reads it
    calls = {}

    def fake(stage, model, block, opt):
        bid = block[0].batch_id
        calls[bid] = calls.get(bid, 0) + 1
        if bid == "b1" and calls[bid] == 1:
            return _res(0, "budget refused: headroom")
        time.sleep(0.05)
        return _res()

    monkeypatch.setattr(R, "_pool_block", fake)
    opt = R.RunOptions(resume=False)
    res = R.run_pool(C.Stage.MAIN, "m", _blocks(4), opt, 2, executor=ThreadPoolExecutor(2))
    assert calls["b1"] == 2 and res.n_admitted == 4 and res.stopped_reason is None

    def always_refused(stage, model, block, opt):
        return _res(0, "budget refused: cap")

    monkeypatch.setattr(R, "_pool_block", always_refused)
    res = R.run_pool(C.Stage.MAIN, "m", _blocks(3), opt, 1, executor=ThreadPoolExecutor(1))
    assert res.stopped_reason.startswith("budget refused") and res.n_admitted == 0


def test_pool_block_uses_its_own_log_dir(monkeypatch, tmp_path):
    seen = {}

    def fake_run_batches(stage, model, batches, opt):
        seen["log_dir"], seen["resume"], seen["in_flight"] = opt.log_dir, opt.resume, opt.in_flight
        return _res()

    monkeypatch.setattr(R, "run_batches", fake_run_batches)
    b = _blocks(1)[0]
    R._pool_block(C.Stage.MAIN, "m", b, R.RunOptions(log_dir=tmp_path))
    assert seen == {"log_dir": tmp_path / "pool" / "b0_a1", "resume": False, "in_flight": 1}


def test_refusal_landing_with_a_completion_is_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "MANIFEST", tmp_path / "m.jsonl")
    calls = {}

    def fake(stage, model, block, opt):
        bid = block[0].batch_id
        calls[bid] = calls.get(bid, 0) + 1
        time.sleep(0.05)                                  # both finish together
        if bid == "b1" and calls[bid] == 1:
            return _res(0, "budget refused: headroom")
        return _res()

    monkeypatch.setattr(R, "_pool_block", fake)
    res = R.run_pool(C.Stage.MAIN, "m", _blocks(2), R.RunOptions(resume=False), 2,
                     executor=ThreadPoolExecutor(2))
    assert calls == {"b0": 1, "b1": 2} and res.stopped_reason is None


def test_refused_whole_block_retry_comes_back_as_attempt_2(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "MANIFEST", tmp_path / "m.jsonl")
    seen = []

    def fake(stage, model, block, opt):
        b = block[0]
        seen.append((b.batch_id, b.attempt))
        if b.batch_id == "b0" and b.attempt == 1:
            # attempt 1 ran and failed; the worker's whole-block retry was refused for headroom
            R._append_manifest({"kind": "outcome", "stage": "main", "batch_id": "b0",
                                "attempt": 1, "episode_ids": [b.episode_id],
                                "failed": [b.episode_id]})
            return _res(1, "budget refused: headroom")
        return _res()

    monkeypatch.setattr(R, "_pool_block", fake)
    res = R.run_pool(C.Stage.MAIN, "m", _blocks(1), R.RunOptions(resume=False), 1,
                     executor=ThreadPoolExecutor(1))
    assert seen == [("b0", 1), ("b0", 2)] and res.stopped_reason is None
