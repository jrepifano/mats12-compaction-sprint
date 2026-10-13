"""P16 API pilot's summary: parse rates, compliance and the two rules."""

from __future__ import annotations

from p16 import pilot


def _rows(arm_text: dict[str, list[str]], kind: str = "ask_plain") -> list[dict]:
    rows = []
    for arm in pilot.ARMS:
        for i, t in enumerate(arm_text.get(arm, ["7", "7", "7", "7"])):
            rows.append({"item_id": f"i{i}", "arm": arm, "kind": "turn1", "text": "1",
                         "finish": "stop", "target": 1})
            for k in ("ask_plain", "ask_pre"):
                rows.append({"item_id": f"i{i}", "arm": arm, "kind": k,
                             "text": t if k == kind else "7", "finish": "stop", "target": 7,
                             "d": 361})
    return rows


def test_compliance_and_rules():
    s = pilot.summarize(_rows({"L": ["8", "9", "7", "7"], "LD": ["361", "361", "361", "8"],
                               "I": ["no", "7", "7", "7"]}))
    a = s["arms"]
    assert a["L|ask_plain"]["compliance"] == 0.5
    assert a["LD|ask_plain"]["compliance"] == 0.75
    assert a["I|ask_plain"]["compliance"] == 0.0 and a["I|ask_plain"]["parse_rate"] == 0.75
    assert s["prefilled_primary"] == ["I"] and not s["add_L2"]
    assert s["turn1_acc"]["P"] == 1.0


def test_no_compliance_adds_l2():
    s = pilot.summarize(_rows({}))
    assert s["add_L2"] and s["max_misreport_compliance"] == 0
