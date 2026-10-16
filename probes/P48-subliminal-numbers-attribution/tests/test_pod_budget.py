from p48 import pod


def test_drop_ladder_fires_in_the_plans_fixed_order():
    assert pod.plan_drops(3.0 * 3600, 4.25 * 3600) == []
    # 720 s over the line: the first rung (900 s of savings) is enough
    assert pod.plan_drops(4.45 * 3600, 4.25 * 3600) == ["cosine"]
    # 1,260 s over: one rung does not cover it, so the second fires too
    assert pod.plan_drops(4.6 * 3600, 4.25 * 3600) == ["cosine", "dir_teacher"]
    deep = pod.plan_drops(6.0 * 3600, 4.25 * 3600)
    assert deep[:3] == ["cosine", "dir_teacher", "proxy"], deep
    assert deep == [x for x in pod.DROP_LADDER if x in deep]


def test_guard_seconds_reads_the_runner_env(monkeypatch):
    monkeypatch.setenv("P48_GUARD_S", "9000")
    assert pod.guard_seconds() == 9000.0
    monkeypatch.delenv("P48_GUARD_S")
    assert pod.guard_seconds() == 18000.0


def test_pod_reads_only_fields_train_run_actually_writes():
    """The smoke failure of 2026-10-14 was pod.py reading `steps` as a list of per-step records
    when train.py writes it as a COUNT. Pin the contract both ways."""
    import inspect

    from p48 import train
    src = inspect.getsource(train.train_run)
    # every field pod.py reads must be assigned in train_run's record
    for field in pod.TRAIN_REC_FIELDS:
        assert f'"{field}"' in src, field
    # and `steps` must stay an int, so pod.py reads losses from steps.jsonl instead
    assert '"steps": step,' in src
    assert "steps.jsonl" in inspect.getsource(pod.step_losses)


def test_step_losses_and_seconds_read_the_jsonl(tmp_path):
    (tmp_path / "steps.jsonl").write_text(
        '{"step": 1, "loss": 3.5, "seconds": 5.9}\n'
        '{"step": 2, "loss": 2.0, "seconds": 0.5}\n'
        "\n")
    assert pod.step_losses(tmp_path) == [3.5, 2.0]
    assert pod.step_seconds(tmp_path) == [5.9, 0.5]
    assert pod.step_losses(tmp_path / "nope") == []
