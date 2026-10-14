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
