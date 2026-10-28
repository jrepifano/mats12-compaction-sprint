"""compaction.s2.native.slots: host-wide reservations shared by the harness schedulers."""

import json
import threading

import pytest

from compaction.s2.native import slots as SL


def _kw(tmp_path):
    return {"state": tmp_path / "s.json", "lock": tmp_path / "s.lock", "poll_s": 0.01}


def test_capacity_counts_reservations_and_external_containers(tmp_path):
    kw = _kw(tmp_path)
    names = ["other-1", "other-2", "native-cx-chain-a", "native-cx-chain-a-egress"]
    with SL.reserve("laptop", "cx", n=2, prefix="native-cx-chain", capacity=8,
                    names=lambda: names, **kw):
        # 2 reserved + 2 external (the cx containers are covered by the reservation)
        assert SL.used("laptop", names, state=kw["state"]) == {"reserved": 2, "external": 2,
                                                                "total": 4}
        with SL.reserve("laptop", "agy", n=2, prefix="native-agy-chain", capacity=8,
                        names=lambda: names, **kw), pytest.raises(TimeoutError), \
                SL.reserve("laptop", "agy", n=4, prefix="native-agy-chain", capacity=8,
                           names=lambda: names, timeout_s=0.05, **kw):
            pass  # 4 reserved + 2 external + 4 wanted = 10 > 8
    assert json.loads((tmp_path / "s.json").read_text())["laptop"] == {}  # released


def test_unregistered_scheduler_containers_count_as_external(tmp_path):
    kw = _kw(tmp_path)
    names = [f"native-cc-chain-{i}" for i in range(6)]  # the running cc bulk (no reservations)
    with pytest.raises(TimeoutError), SL.reserve(
            "laptop", "cx", n=4, prefix="native-cx-chain", capacity=8, names=lambda: names,
            timeout_s=0.05, **kw):
        pass
    with SL.reserve("laptop", "cx", n=2, prefix="native-cx-chain", capacity=8,
                    names=lambda: names, **kw):
        pass


def test_dead_owner_entries_are_dropped_and_unreadable_hosts_stay_closed(tmp_path):
    kw = _kw(tmp_path)
    (tmp_path / "s.json").write_text(json.dumps({"laptop": {"x": {"n": 8, "prefix": "p",
                                                                  "pid": 999999999}}}))
    with SL.reserve("laptop", "cx", n=2, prefix="q", capacity=8, names=list, **kw):
        pass

    def boom():
        raise OSError("docker down")
    with pytest.raises(TimeoutError), SL.reserve(
            "laptop", "cx", n=2, prefix="q", capacity=8, names=boom, timeout_s=0.05, **kw):
        pass


def test_concurrent_reservations_never_exceed_capacity(tmp_path):
    kw = _kw(tmp_path)
    peak, cur, lock = [0], [0], threading.Lock()

    def worker():
        with SL.reserve("laptop", "w", n=2, prefix="native-w", capacity=8, names=list, **kw):
            with lock:
                cur[0] += 2
                peak[0] = max(peak[0], cur[0])
            threading.Event().wait(0.05)
            with lock:
                cur[0] -= 2
    ts = [threading.Thread(target=worker) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert peak[0] <= 8


def test_same_prefix_orphans_count_as_external(tmp_path):
    kw = _kw(tmp_path)
    orphans = [f"native-cx-chain-{i}" for i in range(8)]  # no reservation covers them
    with pytest.raises(TimeoutError), SL.reserve(
            "laptop", "cx", n=2, prefix="native-cx-chain", capacity=8, names=lambda: orphans,
            timeout_s=0.05, **kw):
        pass
    with SL.reserve("laptop", "cx", n=2, prefix="native-cx-chain", capacity=8,
                    names=lambda: orphans[:2], **kw):
        # the 2 running are covered by this reservation's 2 slots: total 2
        assert SL.used("laptop", orphans[:2], state=kw["state"], lock=kw["lock"])["total"] == 2
        # 2 more than covered: 2 orphans external
        assert SL.used("laptop", orphans[:4], state=kw["state"], lock=kw["lock"])["external"] == 2


def test_corrupt_ledger_fails_closed_and_memory_is_per_reservation(tmp_path):
    kw = _kw(tmp_path)
    (tmp_path / "s.json").write_text("{not json")
    with pytest.raises(TimeoutError), SL.reserve(
            "laptop", "cx", n=2, prefix="p", capacity=8, names=list, timeout_s=0.05, **kw):
        pass
    (tmp_path / "s.json").write_text("{}")
    with SL.reserve("rig", "a", n=2, prefix="p", capacity=8, names=list, memory_mb=3072, **kw), \
            SL.reserve("rig", "b", n=2, prefix="q", capacity=8, names=list, memory_mb=1280,
                       **kw):
        assert SL.reserved_memory_mb("rig", state=kw["state"], lock=kw["lock"]) == 4352


def test_release_leaves_a_corrupt_ledger_in_place(tmp_path):
    """astra agy-build r3 B6: release never overwrites a ledger it can't parse."""
    state, lock = tmp_path / "s.json", tmp_path / "s.lock"
    with SL.reserve("h", "o", n=1, prefix="p-", capacity=4, names=list,
                    state=state, lock=lock, poll_s=0):
        state.write_text("{not json")
    assert state.read_text() == "{not json"


@pytest.mark.parametrize("bad", ['{"h": [1]}', '{"h": {"k": {"n": 1}}}',
                                 '{"h": {"k": {"n": "x", "pid": 1}}}'])
def test_a_malformed_entry_is_corruption_not_an_empty_ledger(tmp_path, bad):
    state, lock = tmp_path / "s.json", tmp_path / "s.lock"
    state.write_text(bad)
    with pytest.raises(SL.LedgerCorrupt):
        SL.used("h", [], state=state, lock=lock)
    with (pytest.raises(TimeoutError),  # admission stays closed
          SL.reserve("h", "o", n=1, prefix="p-", capacity=4, names=list,
                     state=state, lock=lock, poll_s=0, timeout_s=0)):
        pass
    assert state.read_text() == bad
