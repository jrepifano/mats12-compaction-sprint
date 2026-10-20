"""Tests for compaction.deepswe (no network, no Docker unless EMX_DOCKER_TESTS=1)."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap

import pytest

from compaction.deepswe import feasibility, registry, sandbox, tasks, trials


def _row(task, model, effort, peak, steps=100):
    return {"task_name": task, "model": model, "reasoning_effort": effort,
            "peak_context_tokens": peak, "n_agent_steps": steps}


DS, LU, GLM = ("deepseek-v4-flash", "max"), ("gpt-5-6-luna", "max"), ("glm-5-3-flash", "max")


def _rows():
    rows = []
    # long: long on both selection configs
    for p in (210_000, 220_000, 300_000, 260_000):
        rows += [_row("long", *DS, p), _row("long", *LU, p + 10_000)]
    # half: long on DeepSeek only
    for p in (250_000, 250_000, 250_000, 250_000):
        rows += [_row("half", *DS, p), _row("half", *LU, 100_000)]
    # short
    for p in (50_000, 60_000, 70_000, 80_000):
        rows += [_row("short", *DS, p), _row("short", *LU, p)]
    rows.append(_row("long", *GLM, None))  # null peaks are ignored
    rows.append(_row("long", *GLM, 150_000))
    return rows


def test_quantile_matches_linear_interpolation():
    xs = [1, 2, 3, 4]
    assert trials.quantile(xs, 0.5) == 2.5
    assert trials.quantile(xs, 0.0) == 1
    assert trials.quantile(xs, 1.0) == 4
    assert trials.quantile(xs, 0.25) == pytest.approx(1.75)
    with pytest.raises(ValueError):
        trials.quantile([], 0.5)


def test_summarise_counts_share_strictly_over_256k():
    s = trials.summarise([256_000, 256_001, 100_000, 300_000], [10, 20, 30, 40])
    assert s["n"] == 4
    assert s["share_over_256k"] == 0.5
    assert s["median"] == 256_000.5
    assert s["median_steps"] == 25
    assert trials.summarise([]) == {"n": 0}


def test_select_requires_every_config_over_threshold():
    rows = _rows()
    assert trials.select_long_tasks(rows) == ["long"]
    assert trials.select_long_tasks(rows, configs=[DS]) == ["half", "long"]


def test_rank_by_min_of_config_medians():
    ranked = trials.rank_tasks(_rows())
    assert [t for t, _ in ranked] == ["long", "half", "short"]
    assert ranked[0][1] == 240_000  # DeepSeek median 240k < luna median 250k
    assert ranked[1][1] == 100_000


def test_per_task_ignores_null_peaks_and_filters_configs():
    pt = trials.per_task(_rows(), [GLM])
    assert pt == {"long": trials.summarise([150_000], [100])}


def test_build_table_joins_task_and_image_meta():
    meta = {"long": {"language": "go", "docker_image": "img:1"}}
    images = {"img:1": {"compressed_bytes": 5}}
    table = trials.build_table(_rows(), n=2, task_meta=meta, image_meta=images)
    assert [r["task"] for r in table] == ["long", "half"]
    assert table[0]["language"] == "go"
    assert table[0]["image_info"] == {"compressed_bytes": 5}
    assert table[0]["glm-5-3-flash@max"]["n"] == 1
    assert "language" not in table[1]


def test_load_rows_checks_n_trials(tmp_path):
    p = tmp_path / "t.json"
    p.write_text('{"scope": "x", "n_trials": 2, "rows": [{}]}')
    with pytest.raises(ValueError):
        trials.load_rows(p)
    p.write_text('{"scope": "x", "n_trials": 1, "rows": [{"a": 1}]}')
    assert trials.load_rows(p) == [{"a": 1}]


def _fake_task(root, name="demo-task", test_runner=True):
    d = root / name
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir()
    (d / "task.toml").write_text(textwrap.dedent(f"""\
        schema_version = "1.3"
        [metadata]
        task_id = "{name}"
        language = "python"
        base_commit_hash = "abc"
        [verifier]
        network_mode = "no-network"
        environment_mode = "separate"
        timeout_sec = 1800.0
        [[verifier.collect]]
        command = "cd /app && git diff abc HEAD > /logs/artifacts/model.patch"
        timeout_sec = 300.0
        [agent]
        network_mode = "no-network"
        timeout_sec = 10800.0
        [environment]
        docker_image = "public.ecr.aws/x/y:tag-v1.1"
        cpus = 2
        memory_mb = 8192
        """))
    (d / "instruction.md").write_text("Do the thing.")
    patch = "+++ b/test.sh\n" if test_runner else "+++ b/tests/test_x.py\n"
    (d / "tests" / "test.patch").write_text(patch)
    return d


def test_load_task_and_tasks(tmp_path):
    d = _fake_task(tmp_path)
    t = tasks.load_task(d)
    assert t["task_id"] == "demo-task"
    assert t["docker_image"] == "public.ecr.aws/x/y:tag-v1.1"
    assert t["collect_command"].startswith("cd /app && git diff abc HEAD")
    assert t["agent_network"] == "no-network"
    assert t["instruction_chars"] == len("Do the thing.")
    assert list(tasks.load_tasks(tmp_path)) == ["demo-task"]


def test_agent_test_command(tmp_path):
    assert feasibility.agent_test_command(_fake_task(tmp_path, "a")) == "bash /app/test.sh"
    assert feasibility.agent_test_command(_fake_task(tmp_path, "b", test_runner=False)) is None


def test_parse_ref():
    assert registry.parse_ref("public.ecr.aws/d3j8x8q7/swe-bench-202605:kh7-v1.1") == (
        "public.ecr.aws", "d3j8x8q7/swe-bench-202605", "kh7-v1.1")
    assert registry.parse_ref("public.ecr.aws/x8v8d7g8/mars-base") == (
        "public.ecr.aws", "x8v8d7g8/mars-base", "latest")


def test_summarise_manifest_and_dedup():
    man = {"layers": [{"digest": "a", "size": 10}, {"digest": "b", "size": 5}]}
    s = registry.summarise_manifest(man, {"architecture": "amd64", "os": "linux"})
    assert s["compressed_bytes"] == 15 and s["arch"] == "amd64" and s["n_layers"] == 2
    other = {"layers": [["a", 10], ["c", 7]]}
    assert registry.dedup_bytes([s, other]) == 22


def test_run_and_exec_args_follow_the_container_contract():
    args = sandbox.run_args("img", "n1", "purp", 3600, 2, 8192, "linux/amd64", "none", "rid1")
    assert args[:3] == ["run", "-d", "--rm"]
    assert ["--name", "n1"] == args[3:5]
    assert "emx.sandbox=purp" in args
    assert "emx.run=rid1" in args
    assert args[args.index("--network") + 1] == "none"
    assert args[-6:] == ["timeout", "-k", "5", "3600", "sleep", "infinity"]
    ex = sandbox.exec_args("n1", "ls", 30)
    assert ex[:4] == ["exec", "-w", "/app", "n1"]
    assert ex[4:8] == ["timeout", "-k", "5", "30"]
    assert ex[-3:] == ["bash", "-lc", "ls"]
    assert sandbox.exec_args("n1", "cat", 5, "/", interactive=True)[:2] == ["exec", "-i"]


class FakeDocker:
    """Scripted stand-in for sandbox._docker: `responses` maps the docker subcommand (args[0],
    or "image rm") to an rc, stdout, or an exception to raise. Records every call."""

    def __init__(self, **responses):
        self.calls: list[list[str]] = []
        self.responses = responses

    def __call__(self, args, timeout, check=True, input_bytes=None):
        self.calls.append(list(args))
        key = "image_rm" if args[:2] == ["image", "rm"] else args[0]
        resp = self.responses.get(key, (0, b""))
        if isinstance(resp, BaseException) or (isinstance(resp, type)
                                               and issubclass(resp, BaseException)):
            raise resp
        rc, out = resp
        p = subprocess.CompletedProcess(["docker", *args], rc, out, b"err")
        if check and rc != 0:
            raise sandbox.SandboxError(f"docker {args[0]} rc={rc}")
        return p


@pytest.fixture
def fake(monkeypatch):
    def install(**responses):
        f = FakeDocker(**responses)
        monkeypatch.setattr(sandbox, "_docker", f)
        monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
        return f
    return install


def test_remove_container_confirms_only_with_a_successful_status_query(fake):
    # Daemon down: rm and ps both fail -> NOT confirmed (was a false "gone" before).
    fake(rm=(1, b""), ps=(1, b""))
    assert sandbox.remove_container("n1", attempts=2) is False
    # Query succeeds and the name is absent -> confirmed, even if rm said "no such container".
    fake(rm=(1, b""), ps=(0, b"other-n1x\n"))
    assert sandbox.remove_container("n1", attempts=2) is True
    # Still listed after every attempt -> not confirmed.
    f = fake(ps=(0, b"n1\n"))
    assert sandbox.remove_container("n1", attempts=3) is False
    assert sum(c[0] == "ps" for c in f.calls) == 3
    # The query itself timing out is a failure to confirm, not success.
    fake(ps=subprocess.TimeoutExpired("docker", 30))
    assert sandbox.remove_container("n1", attempts=1) is False


def test_remove_image_confirms_with_a_successful_listing(fake):
    fake(images=(1, b""))
    assert sandbox.remove_image("emx-sandbox/p:t1", attempts=1) is False
    fake(images=(0, b"emx-sandbox/p:t2\n"))
    assert sandbox.remove_image("emx-sandbox/p:t1", attempts=1) is True
    fake(images=(0, b"emx-sandbox/p:t1\n"))
    assert sandbox.remove_image("emx-sandbox/p:t1", attempts=1) is False


@pytest.mark.parametrize("exc", [KeyboardInterrupt, subprocess.TimeoutExpired("docker", 1),
                                 sandbox.SandboxError("run failed")])
def test_start_removes_the_container_on_interrupt_timeout_or_failure(monkeypatch, exc):
    removed = []

    def fake_docker(args, timeout, check=True, input_bytes=None):
        raise exc

    monkeypatch.setattr(sandbox, "_docker", fake_docker)
    monkeypatch.setattr(sandbox, "remove_container", lambda n: removed.append(n) or True)
    with pytest.raises(type(exc) if isinstance(exc, BaseException) else exc):
        sandbox.Sandbox.start("img", "p", name="n9", run_id="r")
    assert removed == ["n9"]


@pytest.mark.parametrize("op", ["exec", "put_file", "read_file", "copy_in", "snapshot_commit"])
@pytest.mark.parametrize("exc", [KeyboardInterrupt, subprocess.TimeoutExpired("docker", 1)])
def test_every_operation_removes_the_sandbox_on_interrupt_or_client_timeout(
        monkeypatch, tmp_path, op, exc):
    removed, images, resolved = [], [], []

    def fake_docker(args, timeout, check=True, input_bytes=None):
        if args[0] == "exec" and "ps -eo" in " ".join(args):  # the quiescence check passes
            return subprocess.CompletedProcess(args, 0, b"1 0 Ss timeout\n", b"")
        raise exc

    monkeypatch.setattr(sandbox, "_docker", fake_docker)
    monkeypatch.setattr(sandbox, "remove_container", lambda n: removed.append(n) or True)
    monkeypatch.setattr(sandbox, "remove_image", lambda t: images.append(t) or True)
    monkeypatch.setattr(sandbox, "resolve_commit",
                        lambda c, t, since: resolved.append((c, t)) or True)
    sb = sandbox.Sandbox(name="n2", image="img", purpose="p")
    (tmp_path / "d").mkdir()
    call = {"exec": lambda: sb.exec("sleep 999", timeout_s=1),
            "put_file": lambda: sb.put_file(b"x", "/a/b"),
            "read_file": lambda: sb.read_file("/a/b"),
            "copy_in": lambda: sb.copy_in(tmp_path / "d", "/tests"),
            "snapshot_commit": lambda: sb.snapshot("emx-sandbox/p:t")}[op]
    expected = KeyboardInterrupt if exc is KeyboardInterrupt else sandbox.SandboxError
    with pytest.raises(expected):
        call()
    assert removed == ["n2"]  # the source container is removed for every operation
    if op == "snapshot_commit":
        # ... and the possibly still-running commit is settled, not assumed gone.
        assert resolved == [("n2", "emx-sandbox/p:t")]
        assert sb.unresolved_commits == []


def test_interrupted_commit_that_cannot_be_settled_stays_unresolved(monkeypatch):
    def fake_docker(args, timeout, check=True, input_bytes=None):
        if args[0] == "exec":
            return subprocess.CompletedProcess(args, 0, b"1 0 Ss timeout\n", b"")
        raise KeyboardInterrupt

    monkeypatch.setattr(sandbox, "_docker", fake_docker)
    monkeypatch.setattr(sandbox, "remove_container", lambda n: True)
    monkeypatch.setattr(sandbox, "resolve_commit", lambda c, t, since, polls=30: False)
    sb = sandbox.Sandbox(name="n5", image="img", purpose="p")
    with pytest.raises(KeyboardInterrupt):
        sb.snapshot("emx-sandbox/p:t5")
    assert [t for t, _ in sb.unresolved_commits] == ["emx-sandbox/p:t5"]
    # Invocation.cleanup retries it and reports it, rather than declaring it gone.
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, b"", b""))
    inv = sandbox.Invocation("p", run_id="r5")
    inv.sandboxes.append(sb)
    bad = inv.cleanup()
    assert len(bad) == 1 and bad[0].startswith("emx-sandbox/p:t5 (interrupted commit unresolved")


class CommitDaemon:
    """A fake daemon whose interrupted commit may complete a few polls late. `events_by_poll`
    is what the container's event log would show at each poll (destroy is never proof of
    cancellation); `image_by_poll` says whether the tagged image exists yet."""

    def __init__(self, events_by_poll, image_by_poll):
        self.events, self.present, self.poll = events_by_poll, image_by_poll, -1
        self.removed = False

    def __call__(self, args, timeout, check=True, input_bytes=None):
        if args[0] == "events":
            ev = self.events[min(max(self.poll, 0), len(self.events) - 1)]
            return subprocess.CompletedProcess(args, 0, "\n".join(ev).encode(), b"")
        if args[:2] == ["image", "rm"]:
            self.removed = True
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "images":
            if args[1:2] == ["emx-sandbox/p"] and not self.removed:
                self.poll += 1
            here = not self.removed and self.present[min(self.poll, len(self.present) - 1)]
            return subprocess.CompletedProcess(args, 0, b"emx-sandbox/p:t\n" if here else b"",
                                               b"")
        raise AssertionError(args)


def test_resolve_commit_waits_for_the_image_even_after_destroy(monkeypatch):
    # The reviewer's case: destroy on polls 1-2, the image appears on poll 3.
    d = CommitDaemon([["pause", "kill", "destroy"]] * 3, [False, False, True])
    monkeypatch.setattr(sandbox, "_docker", d)
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    assert sandbox.resolve_commit("n", "emx-sandbox/p:t", 0.0, polls=2) is False  # not at poll 2
    assert not d.removed
    d = CommitDaemon([["pause", "kill", "destroy"]] * 3, [False, False, True])
    monkeypatch.setattr(sandbox, "_docker", d)
    assert sandbox.resolve_commit("n", "emx-sandbox/p:t", 0.0, polls=10) is True
    assert d.removed and d.poll == 2  # removed once it appeared, on the third poll


def test_resolve_commit_never_resolves_on_absence(monkeypatch):
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    for events in (["pause", "kill", "destroy"], ["pause", "commit"], ["pause"]):
        d = CommitDaemon([events], [False])
        monkeypatch.setattr(sandbox, "_docker", d)
        assert sandbox.resolve_commit("n", "emx-sandbox/p:t", 0.0, polls=5) is False
        assert d.poll == 4 and not d.removed
    fake_fail = lambda args, timeout, check=True, input_bytes=None: (
        subprocess.CompletedProcess(args, 1, b"", b"daemon down"))
    monkeypatch.setattr(sandbox, "_docker", fake_fail)
    assert sandbox.resolve_commit("n", "emx-sandbox/p:t", 0.0, polls=3) is False


def test_late_sweep_removes_an_image_that_completed_after_snapshot_gave_up(monkeypatch):
    """snapshot() gives up with the commit unresolved; the image appears later; cleanup's
    sweep (or `sweep --run-id`) removes it, by tag, by label and when untagged."""
    state = {"tagged": [], "dangling": [], "rm": []}

    def fake_docker(args, timeout, check=True, input_bytes=None):
        out = b""
        if args[:2] == ["image", "rm"]:
            state["rm"].append(args[-1])
            state["tagged"] = [t for t in state["tagged"] if t != args[-1]]
            state["dangling"] = [x for x in state["dangling"] if x != args[-1]]
        elif args[0] == "images" and "dangling=true" in args or args[0] == "images" and "{{.ID}}" in args:
            out = "\n".join(state["dangling"]).encode()
        elif args[0] == "images" and "reference=emx-sandbox/*" in args:
            out = "\n".join(state["tagged"]).encode()
        elif args[0] == "images" and any(a.startswith("label=") for a in args):
            out = "\n".join(t for t in state["tagged"] if "r7" in t).encode()
        elif args[0] == "images":
            out = "\n".join(state["tagged"]).encode()
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(sandbox, "_docker", fake_docker)
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    inv = sandbox.Invocation("p", run_id="r7")
    tag = inv.new_tag("snap")
    sb = sandbox.Sandbox(name="n7", image="img", purpose="p", run_id="r7")
    sb.unresolved_commits.append((tag, 0.0))
    inv.sandboxes.append(sb)
    bad = inv.cleanup()  # the image hasn't appeared: reported, not declared gone
    assert len(bad) == 1 and "unresolved" in bad[0] and "sweep --run-id r7" in bad[0]
    other = "emx-sandbox/p:snap-OTHER-abc"
    state["tagged"] = [tag, other]  # the late commit completes; another run's image too
    state["dangling"] = ["deadbeef0001"]  # and an untagged image carrying the run label
    assert inv.cleanup() == []
    assert tag in state["rm"] and "deadbeef0001" in state["rm"] and other not in state["rm"]
    assert sb.unresolved_commits == []
    state["tagged"] = [tag]  # appears even later, after the process is gone: the CLI sweep
    assert sandbox.main(["sweep", "--run-id", "r7"]) == 0 and state["tagged"] == []


def test_invocation_cleans_only_its_own_label(fake):
    f = fake(ps=(0, b""), images=(0, b""))
    inv = sandbox.Invocation("purp", run_id="abc123")
    tag = inv.new_tag("snap")
    assert tag in inv.tags and "abc123" in tag
    assert inv.new_tag("snap") != tag  # unique tags
    assert inv.cleanup() == []
    filters = [c[i + 1] for c in f.calls for i, a in enumerate(c) if a == "--filter"]
    allowed = {"label=emx.run=abc123", "reference=emx-sandbox/*", "dangling=true"}
    assert filters and all(x in allowed or x.startswith("name=") for x in filters)
    assert "label=emx.run=abc123" in filters
    assert not any("label=emx.sandbox" in " ".join(c) for c in f.calls)
    # A failed status query is reported, never treated as clean.
    fake(ps=(1, b""), images=(0, b""))
    assert any("status query failed" in b for b in sandbox.Invocation("p", "r").cleanup())


def _reward(**kw):
    base = {"reward": 1, "f2p_total": 3, "f2p_passed": 3, "p2p_total": 2, "p2p_passed": 2}
    base.update(kw)
    return json.dumps(base)


def test_parse_reward_separates_task_failure_from_infrastructure_failure():
    assert sandbox.parse_reward("json", _reward())["reward"] == 1
    nop = sandbox.parse_reward("json", _reward(reward=0, f2p_passed=0))
    assert nop["reward"] == 0
    applied = sandbox.parse_reward("json", _reward(reward=0, f2p_passed=0, p2p_passed=0,
                                                   apply_failed=1))
    assert applied["apply_failed"] == 1
    bad = [("txt", "-1\n"), ("none", ""), ("json", "{not json"), ("json", "[1]"),
           ("json", _reward(reward=-1)), ("json", _reward(reward=True)),
           ("json", _reward(f2p_total=None)), ("json", _reward(f2p_passed=4)),
           ("json", _reward(f2p_passed=2)),  # reward 1 but an f2p failed
           ("json", _reward(reward=0)),  # reward 0 but everything passed
           ("json", _reward(f2p_total=0, f2p_passed=0))]  # reward 1 with no f2p evidence
    for kind, text in bad:
        with pytest.raises(sandbox.VerifierError):
            sandbox.parse_reward(kind, text)


def test_check_rewards_requires_oracle_1_and_nop_0():
    assert feasibility.check_rewards({"reward": 1}, {"reward": 0}) == []
    assert len(feasibility.check_rewards({"reward": 0}, {"reward": 0})) == 1
    assert len(feasibility.check_rewards({"reward": 1}, {"reward": 1})) == 1
    assert len(feasibility.check_rewards(None, None)) == 2


@pytest.mark.parametrize("exc,rc", [(feasibility.CheckFailed("oracle reward is 0, not 1"), 1),
                                    (sandbox.VerifierError("no reward file"), 2),
                                    (feasibility.SetupError("solution did not apply"), 2),
                                    (KeyboardInterrupt(), 130)])
def test_feasibility_main_fails_loudly_and_keeps_diagnostics(monkeypatch, tmp_path, exc, rc):
    cleaned = []

    def fake_run(task_dir, platform, inv, image, out):
        out["phases"] = {"start_s": 0.1}
        raise exc

    monkeypatch.setattr(feasibility, "run", fake_run)
    monkeypatch.setattr(sandbox.Invocation, "cleanup", lambda self: cleaned.append(1) or [])
    o = tmp_path / "o.json"
    assert feasibility.main([str(tmp_path), "--out", str(o)]) == rc
    rec = json.loads(o.read_text())
    assert rec["ok"] is False and rec["error"] and rec["phases"] == {"start_s": 0.1}
    assert cleaned == [1]


def test_feasibility_main_fails_when_cleanup_is_unconfirmed(monkeypatch, tmp_path):
    monkeypatch.setattr(feasibility, "run", lambda *a: a[-1].update(phases={}))
    monkeypatch.setattr(sandbox.Invocation, "cleanup", lambda self: ["n1"])
    o = tmp_path / "o.json"
    assert feasibility.main([str(tmp_path), "--out", str(o)]) == 3
    assert json.loads(o.read_text())["cleanup_unconfirmed"] == ["n1"]


DOCKER_TEST_IMAGE = "public.ecr.aws/x8v8d7g8/mars-base:latest"


@pytest.mark.skipif(os.environ.get("EMX_DOCKER_TESTS") != "1", reason="needs Docker")
def test_docker_sandbox_quiescent_snapshot_fork_and_scoped_cleanup():
    kw = {"platform": None, "lifetime_s": 180, "cpus": 1, "memory_mb": 512}
    inv = sandbox.Invocation("sprint-prep-deepswe-test")
    try:
        with inv.start(DOCKER_TEST_IMAGE, **kw) as sb:
            assert sb.exec("mkdir -p /app && echo hi > /app/f", timeout_s=30, workdir="/").rc == 0
            sb.put_file(b"payload", "/app/g")
            assert sb.read_file("/app/g") == b"payload"
            # A backgrounded process makes the boundary non-quiescent: snapshot refuses.
            assert sb.exec("nohup sleep 300 >/dev/null 2>&1 &", timeout_s=30).rc == 0
            with pytest.raises(sandbox.NotQuiescentError, match="sleep 300"):
                sb.snapshot(inv.new_tag("refused"))
            assert sb.exec("pkill -f '[s]leep 300'; sleep 0.5", timeout_s=30).rc in (0, 1)
            assert sb.stray_processes() == []
            tag = inv.new_tag("snap")
            sb.snapshot(tag)
        with inv.start(tag, **kw) as fork:
            assert fork.exec("cat /app/f", timeout_s=30).stdout.strip() == "hi"
            assert fork.stray_processes() == []
            name = fork.name
        out = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name}", "--format",
                              "{{.Names}}"], capture_output=True, text=True, check=True)
        assert name not in out.stdout.split()
    finally:
        assert inv.cleanup() == []
    out = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"label=emx.run={inv.run_id}"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
    imgs = subprocess.run(["docker", "images", "-q", "--filter", f"label=emx.run={inv.run_id}"],
                          capture_output=True, text=True, check=True)
    assert imgs.stdout.strip() == ""
