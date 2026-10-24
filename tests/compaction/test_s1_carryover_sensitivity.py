"""scripts/s1_carryover_sensitivity.py on synthetic Part B rows (no seal, no model calls)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "s1_carryover_sensitivity", ROOT / "scripts" / "s1_carryover_sensitivity.py"
)
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

NB = 200


def _item(arm, base, n, grade, availability, unfiltered, *, session=None):
    scored = grade != "kept"
    return {
        "rid": f"{base}#{arm}",
        "base_rid": base,
        "arm": arm,
        "judge": "haiku-max",
        "item_id": f"{base}#haiku-max#i{n}",
        "type": "rule",
        "origin": "user",
        "quote": f"quote {n} long enough",
        "text": "t",
        "msg": 1,
        "superseded": False,
        "valid": True,
        "grade": grade,
        "grade_valid": True,
        "availability": availability if scored else None,
        "availability_unfiltered": unfiltered if scored and arm in C.UNFILTERED_ARMS else None,
        "assessable": True,
        "source": "jacob",
        "session_id": session or base,
        "harness": "claude-code",
        "model": "m1",
    }


def _rows():
    """10 histories x 2 rules. cc_prompt loses rule 0 everywhere. codex_local loses rule 0 in
    the first 5 histories under the filtered rule; the unfiltered carry-over keeps all of them."""
    rows = []
    for h in range(10):
        base = f"r{h}"
        for n in range(2):
            lost = n == 0
            rows.append(_item("cc_prompt", base, n, "dropped" if lost else "kept", "lost", None))
            if lost and h < 5:
                rows.append(_item("codex_local", base, n, "dropped", "lost", "still_present"))
            else:
                rows.append(_item("codex_local", base, n, "kept", None, None))
            rows.append(_item("antigravity", base, n, "kept", None, None))
    return rows


def test_unfiltered_rows_swaps_only_the_carry_over_arms():
    rows = _rows()
    out = C.unfiltered_rows(rows)
    by = {(r["arm"], r["item_id"]): r for r in out}
    assert by[("codex_local", "r0#haiku-max#i0")]["availability"] == "still_present"
    assert by[("cc_prompt", "r0#haiku-max#i0")]["availability"] == "lost"
    assert rows[1]["availability"] == "lost"  # inputs untouched


def test_unfiltered_rows_refuses_a_missing_unfiltered_availability():
    bad = _item("codex_local", "r0", 0, "dropped", "lost", None)
    bad["availability_unfiltered"] = None
    with pytest.raises(ValueError, match="no unfiltered availability"):
        C.unfiltered_rows([bad])


def test_report_moves_the_primary_contrast_and_applies_the_reading_rule():
    rep = C.report(_rows(), precision=None, n_boot=NB, seed=1)
    f = rep["rules"]["filtered"]["primary"]["diff"]
    u = rep["rules"]["unfiltered"]["primary"]["diff"]
    assert f["point"] == pytest.approx(0.25)  # cc 10/20 lost, codex 5/20
    assert u["point"] == pytest.approx(0.5)  # codex 0/20 under the unfiltered rule
    assert rep["user_rule_availability_transitions"]["codex_local"] == {
        "jacob": {"lost->still_present": 5}
    }
    assert {(p["a"], p["b"]) for p in rep["exploratory_unfiltered"]} == {
        ("antigravity", "cc_prompt"),
        ("antigravity", "codex_local"),
    }
    assert all("reloaded_as_lost" in p for p in rep["exploratory_unfiltered"])


@pytest.mark.parametrize(
    ("f", "u", "disagree"),
    [
        ((0.05, -0.06, 0.15), (0.16, 0.06, 0.26), True),  # one interval excludes 0
        ((0.05, 0.01, 0.10), (0.16, 0.06, 0.26), False),  # both exclude 0, same sign
        ((0.05, -0.06, 0.15), (-0.02, -0.10, 0.08), True),  # opposite signs
        ((0.05, -0.06, 0.15), (0.03, -0.05, 0.12), False),
    ],
)
def test_reading_rule(f, u, disagree):
    def d(t):
        return {"diff": dict(zip(("point", "lo", "hi"), t, strict=True))}

    assert C.reading_rule(d(f), d(u))["disagree"] is disagree


def test_check_reproduces_refuses_a_mismatch():
    rep = {
        "rules": {
            "filtered": {
                "primary": {
                    "n_items": 1,
                    "n_compactions": 1,
                    "n_session_groups": 1,
                    "diff": {"point": 0.1, "lo": 0.0, "hi": 0.2},
                }
            }
        }
    }
    want = {"part_b": {C.JUDGE: {"primary": rep["rules"]["filtered"]["primary"]}}}
    C.check_reproduces(rep, want)
    want = {
        "part_b": {
            C.JUDGE: {"primary": {**rep["rules"]["filtered"]["primary"], "diff": {"point": 0.2}}}
        }
    }
    with pytest.raises(SystemExit, match="filtered diff point"):
        C.check_reproduces(rep, want)
