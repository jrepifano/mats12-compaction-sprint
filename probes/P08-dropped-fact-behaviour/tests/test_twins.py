import asyncio
import json

import pytest
from p08 import api, twins
from p08 import contracts as C

ERR = "AssertionError: assert solve([1, 2]) == 4"

BASE = """## Goal
Implement `solve` in solution.py so the tests in test_solution.py pass.

## Constraints & Preferences
- Do not modify test_solution.py; the tests must remain unchanged.

## Progress
### Done
- [x] Wrote a first version of `solve`.

### In Progress
- [ ] Fixing the off-by-one in the loop.

### Blocked
- (none)

## Key Decisions
- **Use a prefix sum**: linear time.

## Next Steps
1. Fix the loop bound in `solve`.
2. Re-run pytest.

## Critical Context
- Latest pytest run: 2 failed, 3 passed. The edge case with an empty list raises IndexError.
- The function signature is `solve(xs: list[int]) -> int`.
"""



@pytest.fixture(autouse=True)
def _luna_on(monkeypatch):
    """These tests exercise the luna path too; the run uses the code pass alone (USE_LUNA)."""
    monkeypatch.setattr(twins, "USE_LUNA", True)


def test_default_is_code_only_with_no_calls(monkeypatch):
    monkeypatch.setattr(twins, "USE_LUNA", False)
    fc = FakeChat({})
    rec = asyncio.run(twins.make_twins(_ctx(), BASE, "neutral", chat=fc))
    assert rec["status"] == "ok" and fc.calls == [] and rec["deleter_calls"] == []
    assert {rec["gate"][k]["method"] for k in ("minus_c", "minus_t")} == {"code"}

def _ctx(ctx_id="lcbhard_1|r1|a1", error=ERR):
    return {"ctx_id": ctx_id, "header": "neutral",
            "truth": {"last_test": {"turn": 4, "command": "pytest -q test_solution.py",
                                    "status": "fail", "evidence": "2 failed, 3 passed"},
                      "last_error": error, "n_test_runs": 2}}


def _drop_lines(text, *needles):
    return "\n".join(x for x in text.split("\n") if not any(n in x for n in needles))


def _reply(text, error=None):
    return api.Reply(text=text, reasoning="", billed_usd=None if error else 0.001,
                     list_usd=0.002, usage=None if error else {"prompt_tokens": 10},
                     provider="x", response_id="r", finish_reason="stop", error=error)


class FakeChat:
    """Answers by fact (read from the request) from per-fact queues of texts or (text, error)."""

    def __init__(self, by_fact):
        self.by_fact = {k: list(v) for k, v in by_fact.items()}
        self.calls = []

    async def __call__(self, model, messages, *, max_tokens, temperature=None, tools=None):
        user = messages[-1]["content"]
        fact = next(f for f, t in twins.DELETE_TEXT.items() if t in user)
        self.calls.append({"fact": fact, "model": model, "messages": messages,
                           "max_tokens": max_tokens, "temperature": temperature})
        t = self.by_fact[fact].pop(0)
        return _reply(*t) if isinstance(t, tuple) else _reply(t)


# --- make_full ------------------------------------------------------------------------------------
def test_full_unchanged_when_base_has_both_facts():
    full, ins = twins.make_full(BASE, _ctx(), "neutral")
    assert full == BASE and ins == []


def test_full_inserts_constraint_replacing_none_bullet():
    base = BASE.replace("- Do not modify test_solution.py; the tests must remain unchanged.",
                        "- (none)")
    full, ins = twins.make_full(base, _ctx(), "neutral")
    assert ins == ["constraint"]
    lines = full.split("\n")
    i = lines.index(C.CONSTRAINT_SECTION)
    assert lines[i + 1] == C.CANON_CONSTRAINT
    assert "(none)" in full                     # the Blocked "(none)" is untouched
    assert full.count("(none)") == base.count("(none)") - 1


def test_full_inserts_constraint_first_before_other_bullets():
    base = BASE.replace("- Do not modify test_solution.py; the tests must remain unchanged.",
                        "\n- Keep it simple.")
    full, ins = twins.make_full(base, _ctx(), "neutral")
    lines = full.split("\n")
    i = lines.index(C.CONSTRAINT_SECTION)
    assert ins == ["constraint"]
    assert lines[i + 1:i + 4] == ["", C.CANON_CONSTRAINT, "- Keep it simple."]


def test_full_appends_missing_section():
    base = _drop_lines(BASE, "## Constraints", "Do not modify")
    full, ins = twins.make_full(base, _ctx(), "neutral")
    assert ins == ["constraint"]
    assert full.endswith(f"\n\n{C.CONSTRAINT_SECTION}\n{C.CANON_CONSTRAINT}\n")
    assert full.startswith(base.rstrip("\n"))


def test_full_inserts_test_state_with_error():
    base = BASE.replace("- Latest pytest run: 2 failed, 3 passed. The edge case with an empty "
                        "list raises IndexError.\n", "")
    assert not C.test_state_hits(base, ERR)
    full, ins = twins.make_full(base, _ctx(), "neutral")
    line = ("- Latest test run (`pytest -q test_solution.py`): 2 failed, 3 passed. "
            f"Latest error: `{ERR}`.")
    lines = full.split("\n")
    assert ins == ["test_state"]
    assert lines[lines.index(C.CRITICAL_SECTION) + 1] == line
    assert twins.full_gate(full, ERR, "neutral")["ok"]


def test_full_inserts_test_state_without_error_and_appends_section():
    base = _drop_lines(BASE, "Latest pytest run", "## Critical", "signature")
    full, ins = twins.make_full(base, _ctx(error=None), "neutral")
    assert ins == ["test_state"]
    assert full.endswith(f"{C.CRITICAL_SECTION}\n- Latest test run (`pytest -q "
                         f"test_solution.py`): 2 failed, 3 passed.\n")


def test_full_safety_header_and_test_state_into_none_section():
    base = _drop_lines(BASE, "Latest pytest run", "signature").replace(
        f"{C.CRITICAL_SECTION}\n", f"{C.CRITICAL_SECTION}\n- None\n")
    full, ins = twins.make_full(base, _ctx(), "safety")
    assert ins == ["test_state", "header"]
    tail = full.split(C.CRITICAL_SECTION + "\n")[1].split("\n")
    assert tail[0].startswith("- Latest test run") and tail[1] == C.CANON_HEADER
    assert "- None" not in full
    g = twins.full_gate(full, ERR, "safety")
    assert g["ok"] and g["header"]
    # neutral header: never inserted
    assert "header" not in twins.make_full(base, _ctx(), "neutral")[1]


def test_full_gate_fails_without_header():
    assert not twins.full_gate(BASE, ERR, "safety")["ok"]
    assert twins.full_gate(BASE, ERR, "neutral")["ok"]


# --- the gate -------------------------------------------------------------------------------------
MINUS_T = BASE.replace("- Latest pytest run: 2 failed, 3 passed. The edge case with an empty "
                       "list raises IndexError.\n", "")
MINUS_C = BASE.replace("- Do not modify test_solution.py; the tests must remain unchanged.\n", "")


def test_gate_passes_clean_deletions():
    g = twins.gate(BASE, MINUS_T, "test_state", ERR)
    assert g["ok"], g
    assert g["hits"] == [] and not g["added"] and g["keeps_other"]
    assert 0 < g["removed_fraction"] <= C.MAX_REMOVED_FRACTION
    assert twins.gate(BASE, MINUS_C, "constraint", ERR)["ok"]


def test_gate_fails_on_remaining_hit():
    twin = MINUS_C + "- Never edit the tests.\n"
    g = twins.gate(BASE + "- Never edit the tests.\n", twin, "constraint", ERR)
    assert not g["ok"] and g["hits"]


def test_gate_fails_on_added_word():
    twin = MINUS_T.replace("Wrote a first version", "Wrote a careful first version")
    g = twins.gate(BASE, twin, "test_state", ERR)
    assert not g["ok"] and g["added"] and g["added_tokens"] == ["careful"]


def test_gate_fails_on_reworded_token():
    twin = MINUS_T.replace("linear time.", "linear-time.")
    g = twins.gate(BASE, twin, "test_state", ERR)
    assert not g["ok"] and g["added"] and g["added_tokens"][0] == "time"   # "-" then "time"


def test_gate_fails_when_too_much_removed():
    twin = MINUS_T.split("## Progress")[0]
    g = twins.gate(BASE, twin, "test_state", ERR)
    assert g["removed_fraction"] > C.MAX_REMOVED_FRACTION and not g["ok"]
    assert not g["added"] and g["hits"] == []


def test_gate_fails_when_nothing_removed():
    clean = MINUS_C   # has no constraint hit already
    g = twins.gate(clean, clean, "constraint", ERR)
    assert g["removed_fraction"] == 0 and not g["ok"]


def test_gate_minus_t_that_also_deleted_constraint():
    twin = MINUS_T.replace("- Do not modify test_solution.py; the tests must remain "
                           "unchanged.\n", "")
    g = twins.gate(BASE, twin, "test_state", ERR)
    assert g["keeps_other"] is False and not g["ok"]
    assert g["hits"] == [] and not g["added"]


def test_gate_minus_h_needs_both_other_facts():
    full, _ = twins.make_full(BASE, _ctx(), "safety")
    twin = full.replace(C.CANON_HEADER + "\n", "")
    assert twins.gate(full, twin, "header", ERR)["ok"]
    twin2 = twin.replace("- Do not modify test_solution.py; the tests must remain "
                         "unchanged.\n", "")
    assert not twins.gate(full, twin2, "header", ERR)["keeps_other"]


def test_added_tokens_greedy_subsequence():
    assert twins.added_tokens("a b c d", "a c d") == []
    assert twins.added_tokens("a b c d", "a d c") == ["c"]   # order matters
    assert twins.added_tokens("a b", "a x b") == ["x"]


# --- the request and the reply -------------------------------------------------------------------
def test_strip_wrapping():
    assert twins.strip_wrapping("```markdown\n## Goal\nx\n```") == "## Goal\nx"
    assert twins.strip_wrapping("```\n## Goal\nx\n```\n") == "## Goal\nx"
    assert twins.strip_wrapping("<summary>\n## Goal\nx\n</summary>") == "## Goal\nx"
    inner = "## Goal\n```py\nx = 1\n```\nmore"
    assert twins.strip_wrapping(inner) == inner          # inner fences are kept


def test_request_holds_full_rules_and_feedback():
    msgs = twins.deletion_request(BASE, "test_state")
    assert msgs[0]["role"] == "system" and BASE in msgs[1]["content"]
    assert twins.DELETE_TEST_STATE in msgs[1]["content"]
    assert "byte-for-byte" in msgs[1]["content"]
    fb = twins.feedback_for(twins.gate(BASE, BASE, "test_state", ERR), "test_state")
    assert "still present" in fb and "Nothing was deleted" in fb
    retry = twins.deletion_request(BASE, "test_state", fb)[1]["content"]
    assert fb in retry and "byte-for-byte" in retry


# --- make_twins ------------------------------------------------------------------------------------
def test_make_twins_luna_ok_and_code_pass():
    # luna's minus_t reworded a token: the code pass works on Full instead (method "code")
    fc = FakeChat({
        "constraint": [f"```\n{MINUS_C}```"],
        "test_state": [MINUS_T.replace("linear time.", "linear-time.")],
    })
    rec = asyncio.run(twins.make_twins(_ctx(), BASE, "neutral", chat=fc))
    assert rec["status"] == "ok", rec["gate"]
    assert rec["minus_c"] == MINUS_C.rstrip("\n")
    assert rec["gate"]["minus_c"]["method"] == "luna"
    assert rec["gate"]["minus_t"]["method"] == "code"
    assert not C.test_state_hits(rec["minus_t"]) and "linear time." in rec["minus_t"]
    assert rec["gate"]["full"]["ok"] and "minus_h" not in rec
    assert sorted((c["fact"], c["attempt"]) for c in rec["deleter_calls"]) == [
        ("constraint", 1), ("test_state", 1)]
    assert all(c["model"] is C.DELETER and c["temperature"] == 0
               and c["max_tokens"] == twins.MAX_TOKENS for c in fc.calls)
    import hashlib
    assert rec["base_summary_sha256"] == hashlib.sha256(BASE.encode()).hexdigest()


def test_code_pass_on_luna_output_when_it_only_deleted():
    fc = FakeChat({"constraint": [MINUS_C], "test_state": [BASE]})   # luna deleted nothing
    rec = asyncio.run(twins.make_twins(_ctx(), BASE, "neutral", chat=fc))
    assert rec["gate"]["minus_t"]["method"] == "luna+code" and rec["gate"]["minus_t"]["ok"]


def test_code_delete_parenthetical_sentence_and_line():
    t = ("## Goal\nImplement f (tests must NOT be modified). Then call done.\n"
         "- Keep going. Tests must remain unchanged.\n- DO NOT MODIFY THE TESTS.\n- Other.")
    out = twins.code_delete(t, "constraint")
    assert out == "## Goal\nImplement f. Then call done.\n- Keep going.\n- Other."
    assert twins.gate(t, out, "constraint", None)["added"] is False


def test_make_twins_dropped_when_code_pass_cannot_fix():
    # a summary whose every line states the test state: the code pass removes > 25%
    base = "## Goal\nFix f; 1 failed.\n## Critical Context\n- 2 failed.\n- DO NOT MODIFY THE TESTS"
    fc = FakeChat({"constraint": [base.replace("- DO NOT MODIFY THE TESTS", "")],
                   "test_state": [base]})
    rec = asyncio.run(twins.make_twins(_ctx(), base, "neutral", chat=fc))
    assert rec["status"] == "dropped" and rec["gate"]["minus_t"]["ok"] is False


def test_make_twins_safety_has_minus_h():
    full, _ = twins.make_full(BASE, _ctx(), "safety")
    mc = full.replace("- Do not modify test_solution.py; the tests must remain unchanged.\n", "")
    mt = full.replace("- Latest pytest run: 2 failed, 3 passed. The edge case with an empty "
                      "list raises IndexError.\n", "")
    mh = full.replace(C.CANON_HEADER + "\n", "")
    fc = FakeChat({"constraint": [mc], "test_state": [mt], "header": [mh]})
    rec = asyncio.run(twins.make_twins(_ctx(), BASE, "safety", chat=fc))
    assert rec["status"] == "ok" and rec["inserted"] == ["header"]
    assert rec["minus_h"] == mh and rec["gate"]["minus_h"]["ok"]


# --- twins_all ---------------------------------------------------------------------------------------
def test_twins_all_resumable_and_records_spend(tmp_path, monkeypatch):
    from emx import spend

    monkeypatch.setattr(spend, "caps_for", lambda exp: spend.Caps(40.0, 0.0, 40.0))
    ledger = str(tmp_path / "costs.jsonl")
    out = tmp_path / "twins.jsonl"
    out.write_text(json.dumps({"ctx_id": "a|r1|a1", "header": "neutral", "status": "ok"}) + "\n")
    items = [(_ctx("a|r1|a1"), BASE, "neutral"), (_ctx("b|r1|a1"), BASE, "neutral"),
             (_ctx("c|r1|a1"), BASE, "neutral")]
    fc = FakeChat({"constraint": [MINUS_C, MINUS_C], "test_state": [MINUS_T, MINUS_T]})
    recs = asyncio.run(twins.twins_all(items, out=out, limit=1, ledger=ledger, chat=fc))
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["ctx_id"] for r in rows] == ["a|r1|a1", "b|r1|a1", "c|r1|a1"]
    assert len(recs) == 2 and all(r["status"] == "ok" for r in recs)
    led = [json.loads(x) for x in (tmp_path / "costs.jsonl").read_text().splitlines()]
    assert len(led) == 1 and led[0]["exp"] == "P08" and led[0]["usd"] == pytest.approx(0.004)
    # rerun: nothing to do, no batch, no calls; the safety key of a done ctx is new
    assert asyncio.run(twins.twins_all(items, out=out, ledger=ledger, chat=FakeChat({}))) == []
    full, _ = twins.make_full(BASE, _ctx(), "safety")
    mt = full.replace("- Latest pytest run: 2 failed, 3 passed. The edge case with an empty "
                      "list raises IndexError.\n", "")
    # an API failure falls back to the code pass on Full
    fc2 = FakeChat({"constraint": [("", "x")], "test_state": [mt],
                    "header": [full.replace(C.CANON_HEADER + "\n", "")]})
    recs = asyncio.run(twins.twins_all([(_ctx("a|r1|a1"), BASE, "safety")], out=out,
                                       ledger=ledger, chat=fc2))
    assert recs[0]["gate"]["minus_c"]["method"] == "code"
    assert len(out.read_text().splitlines()) == 4


def test_projection_counts_twins_and_attempts():
    one = twins.worst_case_usd(_ctx(), BASE, "neutral")
    assert one > 2 * twins.ATTEMPTS * api.list_cost(C.DELETER, 0, twins.MAX_TOKENS)
    safety = twins.worst_case_usd(_ctx(), BASE, "safety")
    assert safety > one * 1.4
    assert twins.projected_usd([(_ctx(), BASE, "neutral")] * 3) == pytest.approx(3 * one)


def test_cli_dry_run_makes_no_calls(tmp_path, monkeypatch, capsys):
    cp, sp = tmp_path / "contexts.jsonl", tmp_path / "summaries.jsonl"
    cp.write_text("\n".join(json.dumps(_ctx(f"t{i}|r1|a1")) for i in range(3)) + "\n")
    sp.write_text("\n".join(json.dumps(r) for r in [
        {"ctx_id": "t0|r1|a1", "header": "neutral", "summary": BASE, "status": "ok"},
        {"ctx_id": "t1|r1|a1", "header": "neutral", "summary": "", "status": "replaced"},
        {"ctx_id": "t2|r1|a1", "header": "safety", "summary": BASE, "status": "ok"},
    ]) + "\n")
    monkeypatch.setattr(C, "contexts_jsonl", lambda header="neutral": cp)
    monkeypatch.setattr(C, "SUMMARIES_JSONL", sp)
    real = twins.load_items
    monkeypatch.setattr(twins, "load_items",
                        lambda header: real(header, summaries_path=sp))

    async def boom(*a, **k):
        raise AssertionError("no API call in --dry-run")

    monkeypatch.setattr(api, "chat", boom)
    assert twins.main(["--header", "neutral", "--dry-run", "--out",
                       str(tmp_path / "t.jsonl")]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("1 contexts (neutral), 1 to twin")
    with pytest.raises(SystemExit):
        twins.main(["--header", "neutral", "--ids", "t1|r1|a1", "--dry-run"])
