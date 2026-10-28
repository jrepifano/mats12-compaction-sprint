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


def test_a_prefix_limit_holds_new_reservations_and_is_reread(tmp_path):
    state, lock, lim = tmp_path / "s.json", tmp_path / "s.lock", tmp_path / "lim.json"
    kw = {"state": state, "lock": lock, "limits": lim, "poll_s": 0, "names": list}
    SL.set_limits(["cx-=2", "agy-=4"], path=lim, lock=lock)
    with SL.reserve("h", "a", n=2, prefix="cx-", capacity=8, **kw):
        with (pytest.raises(TimeoutError),  # 2 held + 2 > the limit of 2, capacity has room
              SL.reserve("h", "b", n=2, prefix="cx-", capacity=8, timeout_s=0, **kw)):
            pass
        with SL.reserve("h", "c", n=2, prefix="agy-", capacity=8, **kw):  # other prefixes
            pass
        SL.set_limits(["cx-=-"], path=lim, lock=lock)  # removed: re-read on the next poll
        with SL.reserve("h", "d", n=2, prefix="cx-", capacity=8, **kw):
            pass
    assert SL._limits(lim) == {"agy-": 4}


def test_a_malformed_limits_file_holds_everything(tmp_path):
    state, lock, lim = tmp_path / "s.json", tmp_path / "s.lock", tmp_path / "lim.json"
    lim.write_text('{"cx-": -1}')
    with (pytest.raises(TimeoutError),
          SL.reserve("h", "a", n=1, prefix="other-", capacity=8, names=list, state=state,
                     lock=lock, limits=lim, poll_s=0, timeout_s=0)):
        pass
    with pytest.raises(ValueError):
        SL.set_limits(["cx-=two"], path=tmp_path / "x.json", lock=lock)


def test_the_real_limits_file_applies_only_to_the_default_ledger(tmp_path, monkeypatch):
    lim = tmp_path / "lim.json"
    lim.write_text('{"p-": 0}')
    monkeypatch.setattr(SL, "LIMITS", lim)
    with SL.reserve("h", "a", n=1, prefix="p-", capacity=8, names=list, poll_s=0,
                    state=tmp_path / "s.json", lock=tmp_path / "s.lock"):
        pass  # a caller's own ledger (tests) ignores the machine's limits


def test_laptop_capacity_and_cpus_are_settings_read_on_the_fly(tmp_path, monkeypatch):
    state, lock, lim = tmp_path / "s.json", tmp_path / "s.lock", tmp_path / "lim.json"
    kw = {"state": state, "lock": lock, "limits": lim, "poll_s": 0, "timeout_s": 0}
    SL.set_limits(["__capacity__=2"], path=lim, lock=lock)
    with (pytest.raises(TimeoutError),  # the file's 2 binds the laptop, not the call's 8
          SL.reserve("laptop", "a", n=4, prefix="p-", capacity=8, names=list, **kw)):
        pass
    with SL.reserve("ssh://rig", "b", n=4, prefix="p-", capacity=8, names=list, **kw):
        pass  # other hosts never read the laptop's settings
    SL.set_limits(["__capacity__=16"], path=lim, lock=lock)  # raised on the fly
    with SL.reserve("laptop", "c", n=10, prefix="p-", capacity=8, names=list, **kw):
        pass
    monkeypatch.setattr(SL, "LIMITS", lim)
    assert SL.setting("__capacity__", 8) == 16 and SL.setting("__cpu_tenths__", 15) == 15
    SL.set_limits(["__cpu_tenths__=10"], path=lim, lock=lock)
    assert SL.setting("__cpu_tenths__", 15) == 10
    lim.write_text("not json")
    with pytest.raises(SL.LedgerCorrupt):
        SL.setting("__capacity__", 8)


def test_settings_outside_their_approved_range_close_admission(tmp_path):
    state, lock, lim = tmp_path / "s.json", tmp_path / "s.lock", tmp_path / "lim.json"
    for bad in ('{"__capacity__": 100}', '{"__cpu_tenths__": 0}', '{"__cpu_tenths__": 40}'):
        lim.write_text(bad)
        with pytest.raises(SL.LedgerCorrupt):
            SL.setting("__capacity__", 8, path=lim)
        with (pytest.raises(TimeoutError),
              SL.reserve("laptop", "a", n=1, prefix="p-", capacity=8, names=list, state=state,
                         lock=lock, limits=lim, poll_s=0, timeout_s=0)):
            pass
    lim.unlink()
    with pytest.raises(SL.LedgerCorrupt):  # never written either
        SL.set_limits(["__capacity__=17"], path=lim, lock=lock)
    assert not lim.exists()


def test_a_hold_counts_for_a_running_process_and_ends_with_it(tmp_path, monkeypatch):
    """astra ds-build r2 B1: the legacy Haiku bulk holds 8 slots until it exits."""
    import os
    state, lock = tmp_path / "s.json", tmp_path / "s.lock"
    SL.hold("laptop", "legacy-haiku", n=8, prefix="native-cc-chain", pid=os.getpid(),
            state=state, lock=lock)
    kw = {"state": state, "lock": lock, "limits": None, "poll_s": 0, "timeout_s": 0}
    names = ["native-cc-chain-a", "native-cc-chain-egress-a"]  # covered by the hold
    with (SL.reserve("laptop", "b", n=8, prefix="native-ds-chain", capacity=16,
                     names=lambda: names, **kw),
          pytest.raises(TimeoutError),  # 8 held + 8 reserved = 16: full
          SL.reserve("laptop", "c", n=2, prefix="native-cx-chain", capacity=16,
                     names=lambda: names, **kw)):
        pass
    monkeypatch.setattr(SL, "_alive", lambda pid: pid != os.getpid())  # the process ended
    with SL.reserve("laptop", "d", n=16, prefix="native-ds-chain", capacity=16,
                    names=list, **kw):
        pass
    with pytest.raises(ValueError):
        SL.hold("laptop", "x", n=2, prefix="p", pid=os.getpid(), state=state, lock=lock)
