"""Registry reproduces prereg §4.5 counts and the §7 branches."""

import pytest

from emx.contracts import CellId, ConditionId, adapter_repo, request_seed
from emx.e1 import registry as R


@pytest.mark.parametrize("recipe", ["R0", "R1", "R2"])
def test_part2_has_13_new_cells(recipe):
    cs = R.cells("part2", recipe)
    assert len(cs) == 13
    assert len(set(map(str, cs))) == 13
    assert f"inst-chat-bad-{recipe}-s1" not in map(str, cs)
    assert sum(c.fmt == "tulu" for c in cs) == 2


def test_part1_and_part3():
    assert [str(c) for c in R.cells("part1", "R1")] == ["inst-chat-bad-R1-s1"]
    p3 = R.cells("part3", "R0")
    assert len(p3) == 14 and all(c.data == "bad" and c.seed in (2, 3) for c in p3)
    assert R.cells("part3", "R2") == []
    assert R.cells("dpo", "R2") == []


def test_cuts():
    p3 = R.cells("part3", "R0", cuts=("seeds23_chat_bad_only",))
    assert len(p3) == 6 and all(c.fmt == "chat" for c in p3)
    assert R.cells("dpo", "R0", cuts=("drop_dpo",)) == []
    assert len(R.cells("dpo", "R0")) == 2
    with pytest.raises(ValueError):
        R.cells("part3", "R0", cuts=("nope",))


def test_condition_counts_match_prereg_4_5():
    assert len(R.conditions("untrained", "R0")) == 7
    seed1 = R.conditions("seed1", "R0")
    chat_plain = [c for c in seed1 if c.cell.fmt != "tulu"]
    assert len(chat_plain) == 24
    assert sum(c.matched for c in chat_plain) == 12
    assert len(seed1) - len(chat_plain) == 2
    assert len(R.conditions("replication", "R0")) == 14
    assert len(R.conditions("dpo", "R0")) == 3  # 2 trained + 1 untrained
    assert len(R.conditions("restore", "R0")) == 18
    assert len(R.cells("nem_check", "R0")) == 6
    assert R.conditions("replication", "R2") == []
    assert R.conditions("dpo", "R2") == []


def test_main_family_is_7_matched_bad_seed1():
    fam = R.main_family("R0")
    assert len(fam) == 7
    assert all(c.matched and c.cell.data == "bad" and c.cell.seed == 1 for c in fam)
    seed1 = set(map(str, R.conditions("seed1", "R0")))
    assert set(map(str, fam)) <= seed1


def test_control_for():
    c = ConditionId.parse("sft-plain-bad-R0-s3@chat")
    assert str(R.control_for(c)) == "sft-plain-good-R0-s1@chat"
    with pytest.raises(ValueError):
        R.control_for(ConditionId.parse("sft-untrained@chat"))


def test_ids_roundtrip():
    for s in ["base-tulu-good-R2-s1", "inst-untrained"]:
        assert str(CellId.parse(s)) == s
    for s in ["base-chat-bad-R0-s1@chat+restore=open", "inst-untrained@plain"]:
        assert str(ConditionId.parse(s)) == s
    with pytest.raises(ValueError):
        CellId.parse("inst-chat-bad-R3-s1")
    assert adapter_repo("inst-chat-bad-R0-s1", "u") == "u/e1-inst-chat-bad-R0-s1"


def test_request_seed_deterministic():
    a = request_seed("inst-untrained@chat", "q1", 0)
    assert a == request_seed("inst-untrained@chat", "q1", 0)
    assert a != request_seed("inst-untrained@chat", "q1", 1)
    assert 0 <= a < 2**32


def test_registry_pins():
    for st in R.STAGES.values():
        assert len(st["revision"]) == 40 and len(st["tokenizer_revision"]) == 40
    assert R.RECIPES["R0"]["lr"] == 1e-5 and R.RECIPES["R2"]["epochs"] == 3
    assert R.RECIPES["R0"]["per_device_batch"] * R.RECIPES["R0"]["grad_accum"] == 16
    assert R.FORMATS["tulu"]["stages"] == ("base",)
