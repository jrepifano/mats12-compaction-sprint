"""Docker-free tests for the S4 task module (the end-to-end check is the U6 mock walk)."""
from s4world import contracts as C
from s4world import task as T


def test_episode_seed_is_deterministic_and_distinct():
    seeds = {(a, e): T.episode_seed(0, a, e) for a in C.ARMS for e in range(1, 11)}
    assert len(set(seeds.values())) == len(seeds)
    assert T.episode_seed(0, "channel", 3) == T.episode_seed(0, "channel", 3)
    assert T.episode_seed(1, "channel", 3) != T.episode_seed(0, "channel", 3)


def test_deliver_matches_what_is_stored():
    assert T._deliver("out", "") == "out"
    assert T._deliver("out", "err") == "err\nout"
    long = "x" * (C.STDOUT_MAX + 10)
    d = T._deliver(long, "")
    assert d.startswith("x" * C.STDOUT_MAX)
    assert "output truncated" in d
    assert len(d) < C.STDOUT_MAX + 200


def test_task_builds_for_each_arm():
    for arm in C.ARMS:
        t = T.s4_pilot(arm=arm)
        sb = t.sandbox
        assert sb.type == "docker"
        assert ("compose.channel.yaml" if C.ARMS[arm].channel else "compose.nochannel.yaml") \
            in sb.config
        assert t.config.parallel_tool_calls is False
        assert t.config.max_tool_output > C.STDOUT_MAX
