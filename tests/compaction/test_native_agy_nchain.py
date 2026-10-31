"""compaction.s2.native.agy_nchain: experiment 4's N-task chains on agy, with every container,
turn and screen faked ($0: no Docker, no agy, no model calls)."""

from __future__ import annotations

import itertools
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_native_agy_transcript import exited, gen, plan, run, ui

from compaction.s2.chain import config as CC
from compaction.s2.chain import env as E
from compaction.s2.chain import nchain
from compaction.s2.chain import week2 as W
from compaction.s2.native import agy_nchain as M

ROOT = Path(__file__).resolve().parents[2]
BASE = "a" * 40
CID = "0b91b1d6-f056-495f-8f0a-199ea70be0c5"
TASKS = ("t-one", "t-two", "t-three", "t-four", "t-five")
SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


@pytest.fixture(scope="module")
def settings():
    """Experiment 4's settings from its config (registers its per-task texts)."""
    return nchain.settings_from_config(W.load_config("configs/week2/chain_stress.toml"))


def task(tid: str):
    return SimpleNamespace(task_id=tid, image=f"img/{tid}", language="python",
                           instruction=f"Do {tid}.\n\n{CC.DEEPSWE_ENDING}\n")


def checkpoint(n: int, summary: str, requests: str = "do the task") -> dict:
    return {"step_index": n, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE",
            "content": f"# Resuming from a compaction\n\n{M.REQUESTS_HEAD}\n{requests}\n\n"
                       f"<summary>{summary}</summary>\n"}


class World:
    """One agy conversation across containers, and everything the driver did to it."""

    def __init__(self, ends, compacts=None, autos=()):
        self.steps: list[dict] = []
        self.ends = list(ends)  # one per phase
        self.compacts = list(compacts or [])  # "ok" | "fail" | "empty", default "ok"
        self.autos = set(autos)  # phases with an agy auto-compaction
        self.events: list = []
        self.sessions: list[str] = []
        self.imports: list = []
        self.prompts: list[str] = []
        self.resumed: list = []
        self.branches: list = []
        self.teammate: list = []
        self.snapshots: list = []
        self.exports = 0
        self.cleanup: dict[int, tuple[list, list]] = {}  # session -> (first try, retry) left
        self.retries: list[int] = []

    def add(self, step_fn, *a, **k) -> dict:
        st = step_fn(len(self.steps), *a, **k)
        self.steps.append(st)
        return st


class FakeSB:
    def __init__(self, name):
        self.name, self.host = name, None

    def snapshot(self, *a, **k):  # docker commit: never
        raise AssertionError("docker commit called")


class FakeEnv:
    bare = "/srv/git/x.git"

    def __init__(self, out, world, sb):
        self.out, self.world, self.sb = Path(out), world, sb
        self.home = self.out / "traps_home"
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"remote": {"current_branch":
                                                                      "master"}}))
        self.branch = None

    def harness(self, cmd, check=False, **k):
        assert cmd == "git rev-parse HEAD"
        return SimpleNamespace(output=BASE + "\n", rc=0)

    def trap_snapshot(self, label):
        self.world.snapshots.append((self.sb.name, label))

    def teammate_setup(self, base, branch, file, text, message):
        self.world.teammate.append({"base": base, "branch": branch, "file": file, "text": text,
                                    "message": message})
        self.trap_snapshot("bstart")
        self.branch = branch
        return {"teammate": "t" * 40, "default_branch": "master", "remote_refs": {}}

    def base_test_inventory(self, base, language):
        return ["tests/test_base.py"]

    def finish_traps(self, label):
        self.trap_snapshot(label)
        return self.home

    def refs(self):
        return E.Refs(head="h" * 40, branch=None, local={}, remote={})

    def diff(self, a, b):
        return b"diff --git a/x b/x\n"

    def dirty(self):
        return False


def install(monkeypatch, world: World, settings):
    """Every container step of the agy adapter replaced by the world."""
    monkeypatch.setattr(E, "REPO_NAMES", dict(E.REPO_NAMES))
    monkeypatch.setattr(M.nchain, "push_setup", lambda tid, texts_first=False: nchain.PushSetup(
        tid, BASE[:12], f"feature/{tid}", f"feature {tid}", "NOTES.md", "draft.py",
        f"draft for {tid}\n", f"TAG-{tid}", f"repo-{tid}"))
    monkeypatch.setattr(M.A, "ensure_cli_volume", lambda host, image: "vol")
    monkeypatch.setattr(M, "UNRESOLVED_CLEANUP", [])

    class NS:
        def __init__(self, image):
            i = len(world.sessions)
            self.sb = FakeSB(f"c{i}")
            world.sessions.append(image)
            self.services = frozenset()
            first, again = world.cleanup.get(i, ([], []))
            self.egress = SimpleNamespace(proxy_env=dict, down=list)
            self.inv = SimpleNamespace(cleanup=lambda: (world.retries.append(i), list(again))[1])
            self.server = SimpleNamespace(token="simtok", export=lambda: {"questions": []})
            self.first = first
            self.cleanup_unconfirmed = []

        def __enter__(self):
            return self

        def __exit__(self, *e):
            world.events.append(("closed", self.sb.name))
            self.cleanup_unconfirmed = list(self.first)
            return False
    monkeypatch.setattr(M, "_session", lambda image, cfg: NS(image))

    def install_chain(sb, task_, out, branches):
        world.branches.append(branches)
        return FakeEnv(out, world, sb), ["tests/test_x.py"]
    monkeypatch.setattr(M.X, "install_chain", install_chain)
    monkeypatch.setattr(M.A, "install_auth", lambda sb: None)
    monkeypatch.setattr(M.AC, "install_cli_config", lambda sb, srv: None)
    monkeypatch.setattr(M.X, "env_checks", lambda sb: {"which_git": []})
    monkeypatch.setattr(M.N, "egress_check", lambda sb, eg, url: {"ok": True})
    monkeypatch.setattr(M.AC, "import_session", lambda sb, blobs: (
        world.imports.append((sb.name, dict(blobs))), {"files": sorted(blobs),
                                                       "verified": True})[1])
    monkeypatch.setattr(M.AC, "state_listing", lambda sb, tok: [])

    def drive(self, prompt):
        k = len(world.prompts) + 1
        world.prompts.append(prompt)
        world.resumed.append((self.cid, self.first_step, self.max_calls))
        self.cid = self.cid or CID
        world.add(ui, prompt)
        world.add(plan, [run("ls")])
        world.add(gen, exited(0, "x"))
        if k in world.autos:
            world.add(lambda n: checkpoint(n, f"auto {k}"))
            world.add(plan, [run("ls")])
            world.add(gen, exited(0, "y"))
        world.add(plan, [run(f"echo {SUBMIT}")])
        world.add(gen, exited(0, SUBMIT))
        return world.ends.pop(0)
    monkeypatch.setattr(M.AC.TaskRun, "drive", drive)
    monkeypatch.setattr(M.AC, "quiesce", lambda sb, services: {
        "confirmed": True, "stopped": [], "survivors": [], "errors": []})

    def boundary(run_):
        what = world.compacts.pop(0) if world.compacts else "ok"
        world.events.append(("compact", len(world.prompts), what))
        if what == "fail":
            return {"attempts": [{"attempt": 1}, {"attempt": 2}], "ok": False, "before": 0,
                    "deviation": "compaction failed twice; task 2 uncompacted"}
        if what == "leak":  # exception text the agent could have put a secret into
            return {"attempts": [{"attempt": 1, "error": "SandboxError: LEAK-123"}],
                    "ok": False, "before": 0,
                    "deviation": "transcript unreadable before the boundary: LEAK-123"}
        if what == "quota":  # as TaskRun.call marks a refusal of the /compact turn
            run_.quota_cut = "refused"
            run_.quota_stop.set()
            return {"attempts": [{"attempt": 1, "info": {"quota": True}}], "ok": False,
                    "before": 0, "quota": True, "deviation": "quota refused at the boundary"}
        world.add(ui, "/compact")
        cp = world.add(lambda n: checkpoint(n, "" if what == "empty" else
                                            f"Summary after task {len(world.prompts)}"))
        return {"attempts": [{"attempt": 1}], "ok": True, "before": 0,
                "checkpoint_step": cp["step_index"]}
    monkeypatch.setattr(M.AC, "boundary_compact", boundary)
    monkeypatch.setattr(M.AC, "task_steps", lambda sb, cid, tok: [dict(s) for s in world.steps])

    def export(sb, cid, tok, steps):
        world.exports += 1
        rel = f"conversations/{cid}.db"
        return ({rel: f"db{world.exports}".encode()},
                {"conversation_id": cid, "files": [{"path": rel}], "checkpoints": 1,
                 "last_step_index": max(s["step_index"] for s in steps)})
    monkeypatch.setattr(M.AC, "export_session", export)
    monkeypatch.setattr(M.AC, "prescreen_evidence", lambda sb, bare, tok, **kw: {"ok": True})
    monkeypatch.setattr(M.AC, "screen_evidence", lambda sb, out, tok: {"ok": True})
    monkeypatch.setattr(M.AC, "screened", lambda sb, obj, tok: obj)
    monkeypatch.setattr(M.AC, "screen_blob", lambda sb, data, tok: b"LEAK" not in data)
    monkeypatch.setattr(M.AC, "_exec_np", lambda sb, cmd, env=None, **k: M.AC.SB.ExecResult(
        0, "oom 0\noom_kill 0\npeak=1\nmax=2\n", "", 0.1))
    monkeypatch.setattr(M.AC, "pre_compact_checkpoint",
                        lambda *a, **k: pytest.fail("pre_compact_checkpoint called"))

    def no_commit(args, host=None):
        if "commit" in args:
            pytest.fail("docker commit called")
        raise AssertionError(f"unexpected docker call {args}")
    monkeypatch.setattr(M.AC.SB, "docker_argv", no_commit)
    monkeypatch.setattr(M.nchain, "score_phase", lambda out, chain, k, setup=None: {
        "rewrite": "no", "rejection": "no", "branch": setup.branch})


def config(tmp_path, settings, n=5, **kw):
    ids = TASKS[:n]
    chain = nchain.Chain("u1", "unrelated", tuple(nchain.Phase(t, "switch") for t in ids))
    return M.ChainConfig(chain=chain, tasks={t: task(t) for t in ids}, settings=settings,
                         root=tmp_path, **kw)


def result(tmp_path, k):
    return json.loads((tmp_path / "chains" / "u1" / f"p{k}" / "result.json").read_text())


def rows(tmp_path):
    p = tmp_path / "chains" / "u1" / "compactions.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()]


# ---------------------------------------------------------------------------- whole chains


def test_five_phases_switch_containers_and_carry_one_conversation(monkeypatch, tmp_path,
                                                                  settings):
    world = World([M.END_SUBMITTED] * 5, autos={3})
    install(monkeypatch, world, settings)
    st = M.StateFile.load(tmp_path)
    seen = []

    def hook(out, k, task_):
        assert (out / "result.json").exists()
        seen.append((k, task_.task_id))
    rec = M.run_chain(config(tmp_path, settings), st, after_phase=hook)
    assert rec["state"] == "done" and rec["n_phases_run"] == 5
    assert seen == [(k, TASKS[k - 1]) for k in range(1, 6)]
    # a container per phase, from its own task's image
    assert world.sessions == [f"img/{t}" for t in TASKS]
    for k in range(1, 6):
        c = result(tmp_path, k)["extra"]["container"]
        assert c == {"mode": "first" if k == 1 else "switch", "image": f"img/{TASKS[k - 1]}",
                     "name": f"c{k - 1}"}
    # setup: junk branches in phase 1, the teammate draft after
    assert world.branches[0] == nchain.junk_branches(nchain.push_setup(TASKS[0]))
    assert world.branches[1:] == [[]] * 4
    assert [t["branch"] for t in world.teammate] == [f"feature/{t}" for t in TASKS[1:]]
    assert all(t["base"] == BASE for t in world.teammate)
    assert world.teammate[0]["message"] == CC.TEAMMATE_MESSAGE.format(feature="feature t-two")
    assert [lab for _c, lab in world.snapshots] == ["bstart", "final"] * 5
    r1, r2 = result(tmp_path, 1), result(tmp_path, 2)
    assert r1["extra"]["setup"] == {"kind": "junk", "branch": "feature/t-one"}
    assert r2["extra"]["setup"]["kind"] == "teammate" and r2["extra"]["setup"]["teammate"]
    assert r2["extra"]["test_inventory"] == ["tests/test_base.py"]
    assert r1["extra"]["base_check"] == {"expected": BASE[:12], "head": BASE}
    # the DB of phase k imported in phase k+1, resuming the same conversation
    assert [b for _n, b in world.imports] == [{f"conversations/{CID}.db": f"db{k}".encode()}
                                              for k in range(1, 5)]
    assert [n for n, _b in world.imports] == ["c1", "c2", "c3", "c4"]
    assert [cid for cid, _f, _m in world.resumed] == [None] + [CID] * 4
    assert all(m == 400 for _c, _f, m in world.resumed)
    firsts = [f for _c, f, _m in world.resumed]
    assert firsts[0] == 0 and all(b > a for a, b in itertools.pairwise(firsts))
    # a /compact after every phase, the last one kind "end"
    assert [e[1] for e in world.events if e[0] == "compact"] == [1, 2, 3, 4, 5]
    rs = rows(tmp_path)
    assert [(r["kind"], r["boundary"], r["phase"]) for r in rs] == [
        ("boundary", 1, None), ("boundary", 2, None), ("auto", None, 3), ("boundary", 3, None),
        ("boundary", 4, None), ("end", 5, None)]
    keys = {"chain", "arm", "utc", "kind", "boundary", "phase", "after_phase", "ok", "empty",
            "encrypted", "attempt", "deviation", "step_index", "pre_ctx_tokens",
            "summary_chars", "file"}
    assert all(set(r) == keys for r in rs)
    assert all(r["ok"] and not r["empty"] for r in rs)
    assert rs[-1]["file"] == str((tmp_path / "chains/u1/p5/compaction.json").resolve())
    # saved states after every boundary and at the end
    for k in range(1, 6):
        d = tmp_path / "chains" / "u1" / "states" / f"after_task{k}"
        man = json.loads((d / "manifest.json").read_text())
        assert man["phase"] == k and man["kind"] == ("end" if k == 5 else "boundary")
        assert man["conversation_id"] == CID
        assert (d / "conversations" / f"{CID}.db").read_bytes() == f"db{k}".encode()
    state = json.loads((tmp_path / "state.json").read_text())["chains"]["u1"]
    assert state["phases"] == [{"task": t, "project": "switch"} for t in TASKS]
    assert state["compactions"] == rs
    assert set(state["phase_runs"]["3"]) >= {"dir", "end", "n_calls", "rewrite", "rejection",
                                             "container"}
    assert state["phase_runs"]["3"]["n_calls"] == 3  # the auto-compaction phase: 3 calls


def test_prompts_are_experiment_4s_text_in_agys_wrapper(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    M.run_chain(config(tmp_path, settings, n=2), M.StateFile.load(tmp_path))
    p1, p2 = world.prompts
    ending = settings.task_ending.format(branch="feature/t-one", default="master")
    tag = settings.tag_line.format(tag="TAG-t-one")
    assert p1 == (f"Please solve this issue: Do t-one.\n\n{ending}\n\n{tag}\n\n"
                  f"{M.X.SUBMIT_LINE}")
    lead = settings.lead_different.format(repo="repo-t-two", prev="repo-t-one")
    assert p2.startswith(f"{lead}\n\nPlease solve this issue: Do t-two.")
    assert "TAG-t-two" in p2 and p2.endswith(M.X.SUBMIT_LINE)
    assert "Recommended Workflow" not in p1  # no mini-swe-agent instance template
    for k, p in ((1, p1), (2, p2)):
        r = result(tmp_path, k)
        msgs = json.loads((tmp_path / "chains/u1" / f"p{k}" / "messages.json").read_text())
        i = r["extra"]["phase_message_index"]
        assert i == r["extra"]["b_message_index"] and msgs[i]["content"].strip() == p.strip()
        assert r["extra"]["ticket_tag"] == f"TAG-t-{'one' if k == 1 else 'two'}"
        assert r["extra"]["position"] == k and r["extra"]["chain"] == "u1"
    # phase 2's messages.json holds phase 1 before its prompt
    msgs2 = json.loads((tmp_path / "chains/u1/p2/messages.json").read_text())
    assert result(tmp_path, 2)["extra"]["phase_message_index"] > 0
    assert any(m["role"] == "user" and m["content"].strip() == p1.strip() for m in msgs2)
    assert (tmp_path / "chains/u1/p1/tip.patch").read_bytes().startswith(b"diff")
    assert result(tmp_path, 1)["extra"]["b_tip"] == "h" * 40


def test_compaction_json_carries_the_extracted_summary(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    M.run_chain(config(tmp_path, settings, n=2), M.StateFile.load(tmp_path))
    c = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert c["summary"] == "Summary after task 1" and c["user_requests"] == "do the task"
    assert c["checkpoint"].startswith("# Resuming from a compaction")
    assert c["ok"] and not c["empty"] and c["kind"] == "boundary" and c["attempts"] == 1
    assert c["summary_chars"] == len("Summary after task 1") and c["pre_ctx_tokens"] == 9000
    e = json.loads((tmp_path / "chains/u1/p2/compaction.json").read_text())
    assert e["kind"] == "end" and e["summary"] == "Summary after task 2"


def test_a_summary_failing_the_screen_is_never_stored(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    monkeypatch.setattr(M.AC, "screened", lambda sb, obj, tok: (
        M.AC.OMITTED if "Summary" in json.dumps(obj) else obj))
    M.run_chain(config(tmp_path, settings, n=2), M.StateFile.load(tmp_path))
    text = (tmp_path / "chains/u1/p1/compaction.json").read_text()
    assert "Summary after" not in text and json.loads(text)["summary"] == M.AC.OMITTED


def test_max_phases_two_runs_two_phases_and_the_end_compaction(monkeypatch, tmp_path,
                                                                settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    rec = M.run_chain(config(tmp_path, settings, max_phases=2), M.StateFile.load(tmp_path))
    assert rec["state"] == "done" and rec["n_phases_run"] == 2 and len(world.sessions) == 2
    assert [(r["kind"], r["boundary"]) for r in rows(tmp_path)] == [("boundary", 1),
                                                                    ("end", 2)]
    assert not (tmp_path / "chains/u1/p3").exists()
    assert (tmp_path / "chains/u1/states/after_task2/manifest.json").exists()


def test_a_failed_compaction_halts_before_the_next_phase(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 3, compacts=["ok", "fail"])
    install(monkeypatch, world, settings)
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path))
    assert rec["state"] == "halted b2: compaction failed" and len(world.sessions) == 2
    assert rec["compaction_failures"][0]["boundary"] == 2
    r = rows(tmp_path)[-1]
    assert r["kind"] == "boundary" and not r["ok"] and r["empty"] and r["attempt"] == 2
    assert not (tmp_path / "chains/u1/p3").exists()
    assert not (tmp_path / "chains/u1/states/after_task2").exists()


def test_an_empty_summary_halts_too(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2, compacts=["empty"])
    install(monkeypatch, world, settings)
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path))
    assert rec["state"] == "halted b1: compaction failed" and len(world.sessions) == 1
    r = rows(tmp_path)[0]
    assert not r["ok"] and r["empty"] and r["deviation"] == "empty summary"


def test_a_failed_end_compaction_halts_at_the_last_boundary(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2, compacts=["ok", "fail"])
    install(monkeypatch, world, settings)
    rec = M.run_chain(config(tmp_path, settings, max_phases=2), M.StateFile.load(tmp_path))
    assert rec["state"] == "halted b2: compaction failed"


@pytest.mark.parametrize("end", [M.END_FAILED])
def test_a_failed_phase_halts_without_a_boundary(monkeypatch, tmp_path, settings, end):
    world = World([M.END_SUBMITTED, end, M.END_SUBMITTED])
    install(monkeypatch, world, settings)
    graded = []
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path),
                      after_phase=lambda out, k, t: graded.append(k))
    assert rec["state"] == f"halted p2: {end}" and len(world.sessions) == 2
    assert [e[1] for e in world.events if e[0] == "compact"] == [1]
    assert graded == [1] and rec["phase_runs"]["2"]["end"] == end


@pytest.mark.parametrize("end", [M.AC.END_CALLS, M.AC.END_TIME, M.END_STALLED, M.END_STOPPED])
def test_capped_stalled_and_stopped_phases_go_on(monkeypatch, tmp_path, settings, end):
    world = World([end, M.END_SUBMITTED, end])
    install(monkeypatch, world, settings)
    rec = M.run_chain(config(tmp_path, settings, n=3), M.StateFile.load(tmp_path))
    assert rec["state"] == "done" and rec["phase_runs"]["1"]["end"] == end


def test_quota_stop_before_a_phase_keeps_its_slot_unknown(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 3)
    install(monkeypatch, world, settings)
    cfg = config(tmp_path, settings)
    orig = world.ends

    class Ends(list):
        def pop(self, i=-1):
            cfg.quota_stop.set()  # another chain's refusal during phase 1
            return super().pop(i)
    world.ends = Ends(orig)
    rec = M.run_chain(cfg, M.StateFile.load(tmp_path))
    assert rec["state"] == "quota_refused"
    c = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert c["quota"] and not c["ok"]  # the boundary was never launched
    assert len(world.sessions) == 1 and not (tmp_path / "chains/u1/p2").exists()


def test_a_set_flag_skips_phase_1_with_the_primary_unknown(monkeypatch, tmp_path, settings):
    world = World([])
    install(monkeypatch, world, settings)
    flag = threading.Event()
    flag.set()
    rec = M.run_chain(config(tmp_path, settings, quota_stop=flag), M.StateFile.load(tmp_path))
    assert rec["state"] == "quota_refused" and world.sessions == []
    r = result(tmp_path, 1)
    assert r["end"] == M.END_QUOTA and r["extra"]["primary_unknown_reason"]


def test_a_quota_ending_marks_the_phase_unknown(monkeypatch, tmp_path, settings):
    world = World([M.END_QUOTA])
    install(monkeypatch, world, settings)
    graded = []
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path),
                      after_phase=lambda out, k, t: graded.append(k))
    assert rec["state"] == "quota_refused" and graded == []
    assert result(tmp_path, 1)["extra"]["primary_unknown_reason"] == "quota refused in phase 1"


def test_a_done_chain_is_skipped_and_a_used_dir_is_refused(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    st = M.StateFile.load(tmp_path)
    cfg = config(tmp_path, settings, n=2)
    M.run_chain(cfg, st)
    n = len(world.sessions)
    assert M.run_chain(cfg, M.StateFile.load(tmp_path))["state"] == "done"
    assert len(world.sessions) == n
    st2 = M.StateFile(tmp_path / "other")
    (tmp_path / "other/chains/u1/p2").mkdir(parents=True)
    (tmp_path / "other/chains/u1/p2/result.json").write_text("{}")
    with pytest.raises(FileExistsError):
        M.run_chain(config(tmp_path / "other", settings, n=2), st2)


def test_the_kept_image_save_is_refused(tmp_path, settings):
    with pytest.raises(ValueError, match="kept image"):
        M.run_chain(config(tmp_path, settings, pre_compact_checkpoint=True),
                    M.StateFile(tmp_path))


def test_a_hook_error_is_recorded_and_the_chain_goes_on(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)

    def boom(out, k, t):
        raise RuntimeError("grader down")
    rec = M.run_chain(config(tmp_path, settings, n=2), M.StateFile.load(tmp_path),
                      after_phase=boom)
    assert rec["state"] == "done" and "grader down" in rec["phase_runs"]["1"]["after_phase_error"]


def test_a_session_that_cannot_start_still_writes_result_json(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED])
    install(monkeypatch, world, settings)

    def down(image, cfg):
        raise OSError("docker unreachable")
    monkeypatch.setattr(M, "_session", down)
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path))
    assert rec["state"] == "halted p1: failed"
    r = result(tmp_path, 1)
    assert r["end"] == M.END_FAILED and "unreachable" in r["error"]


def test_a_patch_failing_the_screen_is_omitted(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED])
    install(monkeypatch, world, settings)
    monkeypatch.setattr(FakeEnv, "diff", lambda self, a, b: b"LEAK")
    M.run_chain(config(tmp_path, settings, max_phases=1), M.StateFile.load(tmp_path))
    assert not (tmp_path / "chains/u1/p1/tip.patch").exists()
    assert result(tmp_path, 1)["extra"]["tip_patch_omitted"] == M.AC.OMITTED


def test_the_teammate_draft_comes_from_experiment_4s_texts(settings):
    """The real push setups of u1's tasks (registered from the config's texts file)."""
    orders = json.loads((ROOT / "configs/week2/orders/chain_stress.json").read_text())
    u1 = next(c for c in orders if c["id"] == "u1")
    texts = json.loads((ROOT / "configs/week2/wording/chain_stress/teammate_texts.json")
                       .read_text())
    for p in u1["phases"]:
        ps = nchain.push_setup(p["task"], texts_first=settings.texts_first)
        assert ps.teammate_text == texts[p["task"]]["teammate_text"]
        assert ps.tag == texts[p["task"]]["tag"] and ps.repo == texts[p["task"]]["repo"]


# ---------------------------------------------------------------------------- pieces


def test_split_checkpoint():
    c = checkpoint(4, "the summary", "req one\nreq two")["content"]
    assert M.split_checkpoint(c) == {"summary": "the summary",
                                     "user_requests": "req one\nreq two"}
    assert M.split_checkpoint("# Resuming") == {"summary": "", "user_requests": ""}


def test_prompt_index_searches_after_the_previous_phase():
    msgs = [{"role": "user", "content": "P"}, {"role": "assistant", "content": ""},
            {"role": "user", "content": "P"}]
    assert M.prompt_index(msgs, "P") == 0 and M.prompt_index(msgs, "P", 1) == 2
    assert M.prompt_index(msgs, None) is None


def test_state_file_writes_atomically_and_reloads(tmp_path):
    st = M.StateFile(tmp_path)
    ch = nchain.Chain("u9", "unrelated", (nchain.Phase("a", "switch"),))
    st.ensure_chain(ch)
    st.set(config={"path": "c.toml", "sha256": "x"}, capacity={"agents": 4})
    st.update("u9", state="running p1")
    st.update_phase("u9", 1, end="submitted")
    st.log_compaction(ch, {"kind": "boundary", "boundary": 1})
    again = M.StateFile.load(tmp_path)
    rec = again.chain("u9")
    assert rec["state"] == "running p1" and rec["phase_runs"]["1"]["end"] == "submitted"
    assert rec["compactions"][0]["chain"] == "u9" and again.data["capacity"] == {"agents": 4}
    assert not list(tmp_path.glob("*.tmp"))
    line = json.loads((tmp_path / "chains/u9/compactions.jsonl").read_text())
    assert line["kind"] == "boundary" and line["arm"] == "unrelated"


def test_agy_is_the_default_harness(tmp_path, settings):
    cfg = config(tmp_path, settings)
    assert cfg.harness is M.AGY and cfg.host is None and not cfg.pre_compact_checkpoint


def test_a_phase_dir_scores_with_nchains_scorer(monkeypatch, tmp_path, settings):
    """`nchain.score_phase` (the real one) reads an agy phase dir: the rejection from its
    messages at phase_message_index; the scorer's home omitted here, so the traps read
    missing/unknown, never a value."""
    real = nchain.score_phase
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    monkeypatch.setattr(M.AC, "prescreen_evidence", lambda sb, bare, tok, **kw: {"ok": False})
    cfg = config(tmp_path, settings, n=2)
    M.run_chain(cfg, M.StateFile.load(tmp_path))
    for k in (1, 2):
        out = tmp_path / "chains/u1" / f"p{k}"
        row = real(out, cfg.chain, k, nchain.push_setup(TASKS[k - 1]))
        assert row["position"] == k and row["end"] == M.END_SUBMITTED
        assert row["project"] == ("first" if k == 1 else "switch")
        assert row["rejection"] == "no" and row["trap_status"] == "missing"
        assert row["rewrite"] == "unknown" and row["n_calls"] == 2


# ---------------------------------------------------------------------------- review r1 fixes


def test_unscreened_boundary_text_reaches_no_output_file(monkeypatch, tmp_path, settings):
    """astra r1 #1: exception text in the boundary's deviation, with the diagnostics screen
    failing too: no file under the run root holds it (compaction.json, compactions.jsonl,
    state.json, result.json and the rest)."""
    world = World([M.END_SUBMITTED] * 2, compacts=["leak"])
    install(monkeypatch, world, settings)
    monkeypatch.setattr(M.AC, "screened", lambda sb, obj, tok: (
        M.AC.OMITTED if "LEAK" in json.dumps(obj) else obj))
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path))
    assert rec["state"] == "halted b1: compaction failed"
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    names = {p.name for p in files}
    assert {"compaction.json", "compactions.jsonl", "state.json", "result.json"} <= names
    assert not [str(p) for p in files if b"LEAK" in p.read_bytes()]
    c = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert c["deviation"] == M.AC.OMITTED and c["ok"] is False and c["attempts"] == 1
    assert rows(tmp_path)[0]["deviation"] == M.AC.OMITTED
    assert rec["compaction_failures"][0]["error"] == M.AC.OMITTED
    assert result(tmp_path, 1)["extra"]["diagnostics"] == M.AC.OMITTED


def test_a_clean_record_is_kept_whole(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2, compacts=["fail"])
    install(monkeypatch, world, settings)
    M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path))
    c = json.loads((tmp_path / "chains/u1/p1/compaction.json").read_text())
    assert c["deviation"].startswith("compaction failed twice") and "screen" not in c


def test_a_boundary_refusal_cuts_the_chain_not_the_finished_task(monkeypatch, tmp_path,
                                                                  settings):
    """astra r1 #2: the task submitted, then its boundary /compact was refused: the task keeps
    its outcome and is graded; the chain stops "quota_refused"."""
    world = World([M.END_SUBMITTED] * 3, compacts=["quota"])
    install(monkeypatch, world, settings)
    cfg = config(tmp_path, settings)
    graded, booked = [], []
    rec = M.run_chain(cfg, M.StateFile.load(tmp_path),
                      after_phase=lambda out, k, t: graded.append(k),
                      on_result=lambda out, k, t: booked.append(k))
    assert rec["state"] == "quota_refused" and cfg.quota_stop.is_set()
    r = result(tmp_path, 1)
    assert r["end"] == M.END_SUBMITTED
    assert "primary_unknown_reason" not in r["extra"] and not r["extra"].get("quota_cut")
    assert r["extra"]["chain_quota_cut"] == "quota refused at the boundary"
    assert graded == [1] and booked == [1] and len(world.sessions) == 1
    row = rows(tmp_path)[0]
    assert row["kind"] == "boundary" and not row["ok"]


def test_a_refusal_inside_the_task_still_marks_it_unknown(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED])
    install(monkeypatch, world, settings)
    drive = M.AC.TaskRun.drive

    def refused(self, prompt):
        drive(self, prompt)
        self.quota_cut = "refused"
        self.quota_stop.set()
        return M.END_QUOTA
    monkeypatch.setattr(M.AC.TaskRun, "drive", refused)
    graded, booked = [], []
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path),
                      after_phase=lambda out, k, t: graded.append(k),
                      on_result=lambda out, k, t: booked.append(k))
    assert rec["state"] == "quota_refused" and graded == [] and booked == [1]
    assert result(tmp_path, 1)["extra"]["primary_unknown_reason"] == "quota refused in phase 1"


def test_on_result_runs_for_every_written_phase_after_phase_for_gradeable_ones(
        monkeypatch, tmp_path, settings):
    """astra r1 #7: the ledger hook sees failed and never-started phases too."""
    world = World([M.END_SUBMITTED, M.END_FAILED])
    install(monkeypatch, world, settings)
    graded, booked = [], []
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path),
                      after_phase=lambda out, k, t: graded.append(k),
                      on_result=lambda out, k, t: booked.append((k, t.task_id,
                                                                 (out / "result.json").exists())))
    assert rec["state"] == "halted p2: failed"
    assert booked == [(1, "t-one", True), (2, "t-two", True)] and graded == [1]
    flag = threading.Event()
    flag.set()
    booked.clear()
    M.run_chain(config(tmp_path / "q", settings, quota_stop=flag),
                M.StateFile.load(tmp_path / "q"),
                on_result=lambda out, k, t: booked.append((k, json.loads(
                    (out / "result.json").read_text())["end"])))
    assert booked == [(1, M.END_QUOTA)]


def test_an_on_result_error_is_recorded_and_the_chain_goes_on(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)

    def boom(out, k, t):
        raise RuntimeError("ledger locked")
    rec = M.run_chain(config(tmp_path, settings, n=2), M.StateFile.load(tmp_path),
                      on_result=boom)
    assert rec["state"] == "done" and "ledger locked" in rec["phase_runs"]["2"]["on_result_error"]


def test_a_cleanup_confirmed_on_retry_goes_on(monkeypatch, tmp_path, settings):
    world = World([M.END_SUBMITTED] * 2)
    world.cleanup = {0: (["native-agy-chain-x"], [])}
    install(monkeypatch, world, settings)
    rec = M.run_chain(config(tmp_path, settings, n=2), M.StateFile.load(tmp_path))
    assert rec["state"] == "done" and world.retries == [0] and M.UNRESOLVED_CLEANUP == []
    ex = result(tmp_path, 1)["extra"]
    assert ex["cleanup_first_try"] == ["native-agy-chain-x"] and "cleanup_unconfirmed" not in ex


def test_an_unconfirmed_cleanup_halts_and_holds_every_launch(monkeypatch, tmp_path, settings):
    """astra r1 #8: removal still unconfirmed after the retry: recorded, the chain halts, no
    grading launch, and the module flag holds the next chain before it starts a phase."""
    world = World([M.END_SUBMITTED] * 3)
    world.cleanup = {0: (["native-agy-chain-x"], ["native-agy-chain-x"])}
    install(monkeypatch, world, settings)
    graded, booked = [], []
    rec = M.run_chain(config(tmp_path, settings), M.StateFile.load(tmp_path),
                      after_phase=lambda out, k, t: graded.append(k),
                      on_result=lambda out, k, t: booked.append(k))
    assert rec["state"] == "halted p1: cleanup unconfirmed" and world.retries == [0]
    assert rec["cleanup_unconfirmed"] == ["native-agy-chain-x"]
    assert result(tmp_path, 1)["extra"]["cleanup_unconfirmed"] == ["native-agy-chain-x"]
    assert M.UNRESOLVED_CLEANUP == ["u1 p1: native-agy-chain-x"]
    assert booked == [1] and graded == [] and len(world.sessions) == 1
    other = config(tmp_path / "o", settings)
    rec2 = M.run_chain(other, M.StateFile.load(tmp_path / "o"))
    assert rec2["state"] == "held before p1: unresolved cleanup" and len(world.sessions) == 1


def _expect(**kw):
    return {"model": M.AC.MODEL, "cli_version": M.AC.CLI_VERSION, "compaction_retries": 1,
            **kw}


def test_expectations_bind_the_config_to_the_adapter(monkeypatch, tmp_path, settings):
    """astra r1 #5: the config's harness table must match what the adapter runs."""
    world = World([M.END_SUBMITTED] * 2)
    install(monkeypatch, world, settings)
    assert M.agy_runtime() == {"model": "gemini-3.8-flash-low", "cli_version": "1.4.7",
                               "compaction_retries": 1}
    rec = M.run_chain(config(tmp_path, settings, n=2, expect=_expect()),
                      M.StateFile.load(tmp_path))
    assert rec["state"] == "done"
    for bad in (_expect(model="gemini-3.8-pro"), _expect(cli_version="1.4.8"),
                _expect(compaction_retries=2), {"model": M.AC.MODEL},
                _expect(effort="high")):
        cfg = config(tmp_path / "x", settings, n=2, expect=bad)
        with pytest.raises(ValueError):
            M.run_chain(cfg, M.StateFile.load(tmp_path / "x"))
        with pytest.raises(ValueError):
            M.agy_ensure(cfg)
    assert len(world.sessions) == 2  # nothing launched for a mismatch
    monkeypatch.setattr(M.AC, "AGY", M.A.AgyConfig(model="other-model", compact_at=None))
    with pytest.raises(ValueError, match="other-model"):
        M.agy_check(config(tmp_path, settings, expect=_expect()))


def test_compaction_succeeded_accepts_an_encrypted_summary():
    assert M.compaction_succeeded({"ok": True, "empty": False})
    assert M.compaction_succeeded({"ok": True, "empty": True, "encrypted": True})
    assert not M.compaction_succeeded({"ok": True, "empty": True})
    assert not M.compaction_succeeded({"ok": False, "empty": True, "encrypted": True})
    assert not M.compaction_succeeded(None)
