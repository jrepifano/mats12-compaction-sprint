"""Tests for compaction.s1_judge (U3) with a fake backend: no paid or Max-plan calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from compaction import s1_judge as J
from compaction import s1_types as T

# ------------------------------------------------------------------------------------------
# Fixtures: a fake backend, records, a Rendered stand-in
# ------------------------------------------------------------------------------------------


@dataclass
class FakeBackend:
    """Answers each Req with `responder(req)` (a dict = schema data, or a Resp); too_long by the
    spec rule. Records every Req it saw."""

    responder: object
    name: str = "fake"
    model: str = "fake-1"
    family: str = "anthropic"
    context_tokens: int = 1_000_000
    bytes_per_token: float = 2.3
    seen: list = field(default_factory=list)
    calls: int = 0

    def run(self, reqs, *, purpose):
        self.calls += 1
        out = {}
        for r in reqs:
            self.seen.append(r)
            need = T.est_tokens(r.system + r.user, self.bytes_per_token) + r.max_output_tokens
            if need > self.context_tokens:
                out[r.key] = T.Resp(r.key, None, None, "too_long")
                continue
            a = self.responder(r)
            out[r.key] = (
                a
                if isinstance(a, T.Resp)
                else T.Resp(r.key, a, json.dumps(a), None, model=self.model)
            )
        return out


@dataclass
class R:  # Rendered stand-in
    text: str
    spans: dict
    gaps: list
    bytes: int = 0


def rendered_of(msgs, gaps=(), preserved=()):
    """Stand-in for s1_data.render(rec, include_preserved=True): blocks joined by "\\n\\n", the
    kept-verbatim messages numbered on, with KEPT_HEADER + "\\n\\n" before the first of them."""
    parts, spans, pos = [], {}, 0
    for i, m in enumerate(list(msgs) + list(preserved)):
        block = f"[m{i}] {m['role'].upper()}: {m.get('text', '')}"
        if i == len(msgs):  # the header sits between the blocks, outside every span
            if parts:
                pos += 2
            parts.append(T.KEPT_HEADER)
            pos += len(T.KEPT_HEADER)
        if parts:
            pos += 2
        spans[i] = (pos, pos + len(block))
        pos += len(block)
        parts.append(block)
    text = "\n\n".join(parts)
    return R(text, spans, list(gaps), len(text.encode()))


MSGS = [
    {"role": "user", "text": "Fix the parser bug. Never edit the files under tests/ please."},
    {
        "role": "assistant",
        "text": "I'll add a regression test after the fix.",
        "tool_calls": [{"id": "c1", "name": "Read", "args": {"file_path": "/repo/AGENTS.md"}}],
    },
    {"role": "tool", "tool_use_id": "c1", "text": "Rule: always run ruff before committing."},
    {
        "role": "assistant",
        "text": "",
        "tool_calls": [
            {"id": "c2", "name": "Bash", "args": {"command": "pytest -q tests/test_parser.py"}}
        ],
    },
    {"role": "tool", "tool_use_id": "c2", "text": "1 failed, 4 passed in 0.31s"},
    {"role": "user", "text": "Yes, you may push to the feature branch when it passes."},
]
SUMMARY = (
    "The user asked to fix the parser bug. All tests pass now. The user approved pushing to main."
)


def rec_of(msgs=MSGS, summary=SUMMARY, source="jacob", complete=True, **kw):
    return {
        "id": f"{source}:sess1:0",
        "source": source,
        "session_id": "sess1",
        "harness": "claude-code",
        "model": "claude-x",
        "pre_tokens": 1234,
        "license": "private" if source == "jacob" else "ODC-BY-1.0",
        "source_messages": msgs,
        "summary": summary,
        "post_messages": [],
        "meta": None,
        "history_complete": complete,
        **kw,
    }


def item(
    type_="rule",
    text="Never edit tests.",
    quote="Never edit the files under tests/",
    msg=0,
    origin="user",
    pstatus=None,
    sup=False,
    supq=None,
):
    return {
        "type": type_,
        "text": text,
        "quote": quote,
        "msg": msg,
        "origin": origin,
        "permission_status": pstatus,
        "superseded": sup,
        "superseded_quote": supq,
    }


def claim(c, verdict="supported", ev=None, msg=None, perm=False, sq=None):
    return {
        "claim": c,
        "summary_quote": c if sq is None else sq,
        "is_permission": perm,
        "verdict": verdict,
        "evidence_quote": ev,
        "msg": msg,
        "note": "n",
    }


def router(items=(), grades=None, claims=()):
    """A responder by pass: fixed items, grades (callable on the req, or a list), claims."""

    def f(req):
        if req.key.startswith("p1a|"):
            return {"items": list(items)}
        if req.key.startswith("p1b|"):
            if callable(grades):
                return {"grades": grades(req)}
            return {"grades": list(grades or [])}
        return {"claims": list(claims)}

    return f


# ------------------------------------------------------------------------------------------
# Prompts and layout
# ------------------------------------------------------------------------------------------


def test_prompts_share_system_and_start_with_history():
    p = J.prompts()
    assert p["1a"].system == p["2"].system
    assert p["1b"].system != p["1a"].system
    for k in ("1a", "2"):
        assert p[k].sections["user"].startswith("<history>\n{history}\n</history>\n\n")
    assert "{summary}" not in p["1a"].sections["user"]
    assert "{history}" not in p["1b"].sections["user"]
    assert len({p[k].sha256 for k in p}) == 3


def test_fill_does_not_rescan_inserted_text():
    p = J.prompts()["1a"]
    out = p.fill("user", history="weird {summary} and {history} braces")
    assert "weird {summary} and {history} braces" in out


def test_pass1a_and_pass2_share_system_and_history_prefix():
    be = FakeBackend(router())
    rec = rec_of()
    rd = rendered_of(MSGS)
    J.pass1a(be, rec, rd)
    J.pass2(be, rec, rd, SUMMARY)
    r1a = next(r for r in be.seen if r.key.startswith("p1a|"))
    r2 = next(r for r in be.seen if r.key.startswith("p2|"))
    block = J.history_block(rd.text)
    assert r1a.system == r2.system
    assert r1a.user.startswith(block) and r2.user.startswith(block)
    assert r1a.user[: len(block)].encode() == r2.user[: len(block)].encode()


def test_pass1a_prompt_has_no_summary_or_post_context():
    post = [{"role": "system", "text": "POSTCOMPACTION_REATTACHED_FILE contents"}]
    rec = rec_of(summary="UNIQUE_SUMMARY_SENTINEL says things", post_messages=post)
    be = FakeBackend(router())
    J.judge_records(
        be,
        [rec["id"]],
        load=lambda rid: rec,
        render=lambda r: rendered_of(MSGS),
        post_context=lambda r: post,
    )
    reqs = [r for r in be.seen if r.key.startswith("p1a|")]
    assert reqs
    for r in reqs:
        assert "UNIQUE_SUMMARY_SENTINEL" not in r.user + r.system
        assert "POSTCOMPACTION_REATTACHED_FILE" not in r.user + r.system
    p2 = [r for r in be.seen if r.key.startswith("p2|")]
    assert all("POSTCOMPACTION_REATTACHED_FILE" not in r.user for r in p2)


KEPT = [{"role": "user", "text": "also use uv, not pip"}]


def test_kept_verbatim_messages_validate_as_history():
    v = rendered_of(MSGS, preserved=KEPT)
    n = len(MSGS)
    s, e = v.spans[n]
    assert v.text[:s].endswith(T.KEPT_HEADER + "\n\n")
    assert v.text[s:e] == f"[m{n}] USER: also use uv, not pip"
    it = J.check_item(item(quote="also use uv, not pip", msg=n), v, J.Part(0, (), v.text))
    assert it["valid"] and it["msg"] == n


def _has_include_preserved():
    import inspect

    from compaction import s1_data

    return "include_preserved" in inspect.signature(s1_data.render).parameters


@pytest.mark.skipif(not _has_include_preserved(), reason="U1's include_preserved not landed yet")
def test_judge_view_matches_the_stand_in_layout():
    rec = rec_of(meta={"preserved_messages": KEPT})
    v = J.judge_view(rec)
    n = len(MSGS)
    s, e = v.spans[n]
    assert v.text[:s].endswith(T.KEPT_HEADER + "\n\n")
    assert v.text[s:e] == f"[m{n}] USER: also use uv, not pip"
    assert v.n_source == n
    st = rendered_of(MSGS, preserved=KEPT)  # the stand-in's layout after the last source block
    assert v.text[v.spans[n - 1][1] :] == st.text[st.spans[n - 1][1] :]


# ------------------------------------------------------------------------------------------
# Pass 1a checks
# ------------------------------------------------------------------------------------------


def test_item_quote_valid_invalid_and_wrong_msg_fixed():
    rd = rendered_of(MSGS)
    be = FakeBackend(
        router(
            items=[
                item(),  # valid, right message
                item(quote="Never delete the database", text="x"),  # not in the history
                item(
                    type_="permission",
                    text="May push.",
                    quote="you may push to the feature branch",
                    msg=2,
                    pstatus="granted",
                ),  # wrong msg: it is in m5
                item(quote="tests", text="short"),  # too short
                item(type_="rule", text="long", quote="a" * 301),
            ]
        )
    )
    out = J.pass1a(be, rec_of(), rd)
    assert len(out) == 5
    assert out[0]["valid"] and out[0]["invalid_reason"] is None and out[0]["msg"] == 0
    assert not out[1]["valid"] and out[1]["invalid_reason"] == "quote_not_found"
    assert out[2]["valid"] and out[2]["msg"] == 5 and out[2]["msg_claimed"] == 2
    assert out[3]["invalid_reason"] == "quote_too_short"
    assert out[4]["invalid_reason"] == "quote_too_long"
    assert [x["item_id"] for x in out] == [f"jacob:sess1:0#fake#i{n}" for n in range(5)]


def test_permission_status_rules():
    rd = rendered_of(MSGS)
    be = FakeBackend(
        router(
            items=[
                item(type_="permission", quote="you may push to the feature branch", msg=5),
                item(pstatus="granted"),
            ]
        )
    )
    out = J.pass1a(be, rec_of(), rd)
    assert out[0]["invalid_reason"] == "permission_status_missing"
    assert out[1]["valid"] and out[1]["permission_status"] is None


def test_superseded_passthrough_and_check():
    rd = rendered_of(MSGS)
    be = FakeBackend(
        router(
            items=[
                item(
                    type_="commitment",
                    text="Add a regression test.",
                    quote="I'll add a regression test after the fix.",
                    msg=1,
                    origin="assistant",
                    sup=True,
                    supq="Yes, you may push to the feature branch",
                ),
                item(
                    type_="task_state",
                    text="1 test fails",
                    quote="1 failed, 4 passed",
                    msg=4,
                    origin="tool",
                    sup=True,
                    supq="no such later text anywhere",
                ),
                item(sup=False, supq="ignored when not superseded"),
            ]
        )
    )
    out = J.pass1a(be, rec_of(), rd)
    assert out[0]["superseded"] is True and out[0]["valid"]
    assert out[0]["superseded_quote"] == "Yes, you may push to the feature branch"
    assert out[1]["superseded"] is True and out[1]["invalid_reason"] == "superseded_quote_not_found"
    assert out[2]["superseded"] is False and out[2]["superseded_quote"] is None


def test_assessable_from_gaps():
    rd = rendered_of(MSGS, gaps=[4])
    be = FakeBackend(
        router(
            items=[
                item(),
                item(
                    type_="task_state", text="t", quote="1 failed, 4 passed", msg=4, origin="tool"
                ),
            ]
        )
    )
    out = J.pass1a(be, rec_of(), rd)
    assert out[0]["assessable"] is True and out[1]["assessable"] is False


def test_schema_invalid_response_handling():
    rd = rendered_of(MSGS)
    # a backend schema error with no parseable text: no items, error recorded
    errs: list = []
    be = FakeBackend(lambda r: T.Resp(r.key, None, "not json at all", "schema"))
    assert J.pass1a(be, rec_of(), rd, errors=errs) == []
    assert errs and errs[0]["error"] == "schema" and errs[0]["pass"] == "1a"
    # data that fails validation here is salvaged element by element; the bad one stays a row
    bad = dict(item(), type="nonsense")
    errs = []
    be = FakeBackend(lambda r: {"items": [item(), bad]})
    out = J.pass1a(be, rec_of(), rd, errors=errs)
    assert len(out) == 2 and out[0]["valid"]
    assert out[1]["valid"] is False and out[1]["invalid_reason"].startswith("schema:")
    assert out[1]["type"] == "nonsense" and out[1]["quote"] == bad["quote"]  # fields kept
    assert errs[0]["error"] == "schema:1 of 2 invalid"
    # other errors pass through
    errs = []
    be = FakeBackend(lambda r: T.Resp(r.key, None, None, "timeout"))
    assert J.pass1a(be, rec_of(), rd, errors=errs) == [] and errs[0]["error"] == "timeout"


# ------------------------------------------------------------------------------------------
# Pass 1b
# ------------------------------------------------------------------------------------------


def _items(rd):
    be = FakeBackend(
        router(
            items=[
                item(),
                item(
                    type_="permission",
                    text="May push to the feature branch.",
                    quote="you may push to the feature branch",
                    msg=5,
                    pstatus="granted",
                ),
                item(
                    type_="task_state",
                    text="1 test fails",
                    quote="1 failed, 4 passed",
                    msg=4,
                    origin="tool",
                ),
                item(quote="Never delete the database", text="bogus"),  # invalid: not sent to 1b
            ]
        )
    )
    return J.pass1a(be, rec_of(), rd)


def test_pass1b_grades_and_invalid_kept_without_quote():
    rd = rendered_of(MSGS)
    items = _items(rd)
    grades = [
        {"item": "i0", "verdict": "kept", "summary_quote": None, "note": "no quote given"},
        {
            "item": "i1",
            "verdict": "misstated",
            "summary_quote": "The user approved pushing to main.",
            "note": "branch changed",
        },
        {"item": "i2", "verdict": "dropped", "summary_quote": None, "note": "absent"},
        {"item": "i2", "verdict": "kept", "summary_quote": "All tests pass now.", "note": "dup"},
        {"item": "i99", "verdict": "kept", "summary_quote": None, "note": "unknown id"},
    ]
    be = FakeBackend(router(grades=grades))
    out = J.pass1b(be, items, SUMMARY, "jacob:sess1:0", rec=rec_of())
    sent = be.seen[0].user
    assert "Never delete the database" not in sent  # invalid items aren't graded
    assert "1 failed, 4 passed" in sent and "Fix the parser bug." not in sent.split("<items>")[0]
    by = {x["n"]: x for x in out}
    assert by[0]["grade"] == "kept" and by[0]["grade_valid"] is False
    assert by[0]["grade_reason"] == "summary_quote_not_found"
    assert by[1]["grade"] == "misstated" and by[1]["grade_valid"] is True
    assert by[2]["grade"] == "dropped" and by[2]["grade_valid"]  # first grade wins
    assert by[3]["grade"] is None and by[3]["grade_reason"] == "item_invalid"


def test_pass1b_batches_and_default_private():
    items = [
        dict(x, n=n, valid=True, quote=f"quote number {n}")
        for n, x in enumerate([item()] * (J.PASS1B_BATCH + 5))
    ]
    be = FakeBackend(router(grades=lambda req: []))
    out = J.pass1b(be, items, SUMMARY, "r")
    reqs = [r for r in be.seen if r.key.startswith("p1b|")]
    assert len(reqs) == 2 and all(r.private for r in reqs)
    assert all(x["grade"] is None and x["grade_reason"] == "not_graded" for x in out)


# ------------------------------------------------------------------------------------------
# Pass 2
# ------------------------------------------------------------------------------------------

CLAIMS = [
    claim("The user asked to fix the parser bug.", "supported", ev="Fix the parser bug.", msg=0),
    claim("All tests pass now.", "contradicted", ev="1 failed, 4 passed", msg=3),  # wrong msg
    claim("The user approved pushing to main.", "unsupported", perm=True),
]


@pytest.mark.parametrize("complete,derived", [(True, "invented"), (False, "unknown")])
def test_pass2_derived_invented_vs_unknown(complete, derived):
    rd = rendered_of(MSGS)
    out = J.pass2(FakeBackend(router(claims=CLAIMS)), rec_of(complete=complete), rd, SUMMARY)
    assert [c["verdict"] for c in out] == ["supported", "contradicted", "unsupported"]
    assert out[0]["derived"] is None and out[1]["derived"] is None
    assert out[2]["derived"] == derived and out[2]["is_permission"] is True
    assert out[1]["msg"] == 4 and out[1]["msg_claimed"] == 3 and out[1]["valid"]
    assert all(c["history_complete"] is complete for c in out)


def test_pass2_gaps_and_frontierml_are_never_complete():
    rd = rendered_of(MSGS, gaps=[2])
    out = J.pass2(FakeBackend(router(claims=CLAIMS)), rec_of(), rd, SUMMARY)
    assert out[2]["derived"] == "unknown"
    rec = rec_of(source="frontierml")
    out = J.pass2(FakeBackend(router(claims=CLAIMS)), rec, rendered_of(MSGS), SUMMARY)
    assert out[2]["derived"] == "unknown"


def test_pass2_invalid_claims():
    rd = rendered_of(MSGS)
    cl = [
        claim("Not a span of the summary at all.", "unsupported"),
        claim("All tests pass now.", "supported", ev="all 5 tests passed", msg=4),
    ]
    out = J.pass2(FakeBackend(router(claims=cl)), rec_of(), rd, SUMMARY)
    assert out[0]["invalid_reason"] == "claim_not_in_summary"
    assert out[1]["invalid_reason"] == "evidence_not_found" and not out[1]["valid"]


def test_pass2_claim_may_reword_but_summary_quote_must_be_verbatim():
    """E2E finding (20 Oct): Haiku writes self-contained claims, so `claim` is not checked as a
    substring; `summary_quote` is."""
    rd = rendered_of(MSGS)
    summary = "**Parser**: bug fixed. **Tests**: 4 passing, 1 failing"
    cl = [
        claim("The parser bug was fixed", "unsupported", sq="**Parser**: bug fixed"),
        claim(
            "One parser test still fails",
            "supported",
            ev="1 failed, 4 passed",
            msg=4,
            sq="**Tests**: 4 passing, 1 failing",
        ),
        claim("All tests pass", "unsupported", sq="All tests pass"),  # not in the summary
        claim("x", "unsupported", sq="**Parser**: bug fixed" * 20),  # over 300 characters
    ]
    out = J.pass2(FakeBackend(router(claims=cl)), rec_of(), rd, summary)
    assert out[0]["valid"] and out[0]["summary_quote"] == "**Parser**: bug fixed"
    assert out[1]["valid"] and out[1]["claim"] == "One parser test still fails"
    assert out[2]["invalid_reason"] == "claim_not_in_summary"
    assert out[3]["invalid_reason"] == "summary_quote_too_long"


# ------------------------------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------------------------------


def test_availability_three_cases():
    rec = rec_of()
    kept = [{"role": "user", "text": "Fix the parser bug. Never edit the files under tests/"}]
    rule = {"quote": "Never edit the files under tests/"}
    assert J.availability(rule, rec, kept) == ("still_present", "kept_message:0")
    file_rule = {"quote": "always run ruff before committing"}
    a, how = J.availability(file_rule, rec, [])
    assert a == "recoverable" and how == "file_read:Read@m2"
    assert J.availability({"quote": "1 failed, 4 passed"}, rec, [])[0] == "lost"  # pytest output
    assert J.availability(rule, rec, [])[0] == "lost"  # user turn, not re-readable


@pytest.mark.parametrize(
    "cmd,reads",
    [
        ("cat AGENTS.md", True),
        ("head -n 50 src/a.py", True),
        ("sed -n '1,80p' src/a.py", True),
        ("nl -ba x.py | sed -n '1,20p'", True),
        ("cat > out.txt <<EOF\nhi\nEOF", False),
        ("sed -i 's/a/b/' f.py", False),
        ("pytest -q", False),
        ("cat", False),
        ("cd repo && tail -f log.txt", False),  # compound: output not attributable
        ("cat README.md; pytest -q", False),
        ("cat a | grep x", False),
        ("cat a.py | head -n 5", True),
        ("cat $(ls *.md)", False),
        ("head -50 a.py 2>&1", False),
        ("cat README.md & pytest -q", False),
        ("cat README.md &", False),
        ("sed -n 's/x/y/p' f", False),
        ("sed -n '1,40p' f", True),
        ("sed -n '/def main/p' f.py", True),
        ("sed -n '1,40p'", False),
        ("sed -n -e '1p' f", False),
        ("cat 'a;b.txt'", True),  # quoted: not an operator
        ("(cat a)", False),
    ],
)
def test_shell_reads_file(cmd, reads):
    assert J.shell_reads_file(cmd) is reads


def test_availability_positional_pairing_and_codex_command():
    msgs = [
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [
                {"name": "exec_command", "args": {"cmd": ["bash", "-lc", "cat CLAUDE.md"]}}
            ],
        },
        {"role": "tool", "text": "Never push to main without asking."},
    ]
    a, how = J.availability({"quote": "Never push to main without asking."}, rec_of(msgs=msgs), [])
    assert a == "recoverable" and how == "file_read:exec_command@m1"


# ------------------------------------------------------------------------------------------
# Chunking, dedupe, claim merge
# ------------------------------------------------------------------------------------------


def long_msgs(n=40, size=3_000):
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "text": f"message {i} " + ("lorem ipsum " * (size // 12)),
        }
        for i in range(n)
    ]


def small_backend(responder, budget_msgs=10, size=3_000):
    """A backend whose context holds about `budget_msgs` of the long messages per chunk."""
    p = J.prompts()
    bpt = 2.0
    fixed = max(T.est_tokens(p[k].system + p[k].sections["user"], bpt) for k in ("1a", "2"))
    fixed += T.est_tokens(p["2"].sections["claims_block"], bpt)
    ctx = (
        J.OVERHEAD_TOKENS
        + max(J.MAX_OUT_1A, J.MAX_OUT_2)
        + fixed
        + J.SUMMARY_RESERVE_TOKENS
        + budget_msgs * (size // 2 + 10)
    )
    return FakeBackend(responder, context_tokens=ctx, bytes_per_token=bpt)


def test_chunking_dedupe_and_claim_merge():
    msgs = long_msgs()
    rd = rendered_of(msgs)

    def responder(req):
        k = int(req.key.rsplit("|c", 1)[1])
        if req.key.startswith("p1a|"):
            lo = k * 10  # chunk k holds messages about 10k..10k+9
            return {
                "items": [
                    item(text="Use uv, not pip.", quote=f"message {lo} lorem", msg=lo),
                    # the same rule restated in every chunk: same type, near-identical text
                    item(text="Never edit the tests", quote=f"message {lo + 1} lorem", msg=lo + 1),
                ]
            }
        base = [
            claim("claim A is here", "unsupported"),
            claim("claim B is here", "unsupported"),
            claim("claim C is here", "unsupported"),
        ]
        if k == 0:
            base[2] = claim("claim C is here", "contradicted", ev="message 3 lorem", msg=3)
        if k == 2:
            base[0] = claim("claim A is here", "supported", ev="message 21 lorem", msg=21)
            base[2] = claim("claim C is here", "supported", ev="message 22 lorem", msg=22)
        return {"claims": base}

    be = small_backend(responder)
    summary = "claim A is here. claim B is here. claim C is here."
    items = J.pass1a(be, rec_of(msgs=msgs, summary=summary), rd)
    p1a = [r for r in be.seen if r.key.startswith("p1a|")]
    n = len(p1a)
    assert n >= 3 and all(T.fits(r, be) for r in p1a)
    assert all(x["n_chunks"] == n for x in items)
    # the rule each chunk restates (same type, same text, other quotes) is one item
    rules = [x for x in items if x["text"] == "Use uv, not pip." and x["valid"]]
    assert len(rules) == 1 and rules[0]["chunk"] == 0  # earliest wins
    # a restatement whose quote is invalid (here m40 doesn't exist) stays its own invalid row
    assert all(
        x["invalid_reason"] == "quote_not_found"
        for x in items
        if x["text"] == "Use uv, not pip." and not x["valid"]
    )
    # chunk texts are disjoint, in order, and each starts at a message tag
    texts = [r.user[len("<history>\n") : r.user.index("\n</history>")] for r in p1a]
    assert "".join(t.replace("\n\n", "") for t in texts) == rd.text.replace("\n\n", "")
    assert all(t.startswith("[m") for t in texts)

    claims = J.pass2(be, rec_of(msgs=msgs, summary=summary), rd, summary)
    assert [c["verdict"] for c in claims] == ["supported", "unsupported", "supported"]
    assert claims[0]["msg"] == 21 and claims[2]["evidence_quote"] == "message 22 lorem"
    assert claims[2]["chunk_verdicts"]["0"] == "contradicted"
    assert all(c["n_chunks"] == n for c in claims)
    later = [r for r in be.seen if r.key.startswith("p2|") and not r.key.endswith("|c0")]
    assert later and all(
        "<claims>" in r.user
        and '{"claim": "claim A is here", "summary_quote": "claim A is here"}' in r.user
        for r in later
    )
    first = next(r for r in be.seen if r.key.startswith("p2|") and r.key.endswith("|c0"))
    assert "<claims>" not in first.user
    # pass 1a and pass 2 cut the history at the same messages (shared cache prefix)
    p2_texts = sorted(r.user.split("\n</history>")[0] for r in be.seen if r.key.startswith("p2|"))
    assert p2_texts == sorted(r.user.split("\n</history>")[0] for r in p1a)


def test_claim_missing_in_a_chunk_is_not_a_clean_invention():
    msgs = long_msgs()

    def responder(req):
        if req.key.startswith("p1a|"):
            return {"items": []}
        if req.key.endswith("|c0"):
            return {"claims": [claim("claim A is here", "unsupported")]}
        if req.key.endswith("|c1"):
            return T.Resp(req.key, None, None, "timeout")
        return {"claims": []}  # omits the claim

    be = small_backend(responder)
    out = J.pass2(be, rec_of(msgs=msgs), rendered_of(msgs), "claim A is here.")
    assert out[0]["verdict"] == "unsupported" and out[0]["valid"] is False
    assert "chunk_failed:1" in out[0]["invalid_reason"]
    assert "missing_in_chunk:" in out[0]["invalid_reason"]


def test_availability_compound_command_is_not_recoverable():
    msgs = [
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [
                {"id": "x", "name": "Bash", "args": {"command": "cat README.md; pytest -q"}}
            ],
        },
        {"role": "tool", "tool_use_id": "x", "text": "readme text\n1 failed, 4 passed in 0.2s"},
    ]
    assert J.availability({"quote": "1 failed, 4 passed"}, rec_of(msgs=msgs), [])[0] == "lost"


def test_oversize_message_makes_the_record_unscorable():
    msgs = long_msgs(n=6) + [{"role": "tool", "text": "x" * 200_000}]
    rd = rendered_of(msgs)
    be = small_backend(router())
    errs: list = []
    assert J.pass1a(be, rec_of(msgs=msgs), rd, errors=errs) == []
    assert J.pass2(be, rec_of(msgs=msgs), rd, SUMMARY, errors=errs) == []
    assert be.seen == []  # nothing sent, no retries
    assert [e["error"] for e in errs] == ["unscorable_oversize"] * 2 and errs[0]["msgs"] == [6]
    rec = rec_of(msgs=msgs)
    res = J.judge_records(
        be, [rec["id"]], load=lambda r: rec, render=lambda r: rd, post_context=lambda r: []
    )
    assert res["unscorable"] == [rec["id"]] and res["records_complete_share"] == 0.0


def test_too_long_answer_is_an_error_not_a_resplit():
    errs: list = []
    be = FakeBackend(lambda r: T.Resp(r.key, None, None, "too_long"))
    assert J.pass1a(be, rec_of(), rendered_of(MSGS), errors=errs) == []
    assert errs[0]["error"] == "too_long" and be.calls == 1


def test_one_partition_for_both_passes_with_a_long_summary():
    """Review r1 finding 8: a 26,000-character summary (13k tokens at 2 bytes per token) used to
    move pass 2's chunk boundaries; the partition now ignores the summary."""
    msgs = long_msgs()
    rd = rendered_of(msgs)
    summary = ("claim A is here. " * 1600)[:26_000]
    be = small_backend(router(claims=[claim("claim A is here.", "unsupported")]))
    J.pass1a(be, rec_of(msgs=msgs), rd)
    J.pass2(be, rec_of(msgs=msgs), rd, summary)
    hist = lambda r: r.user.split("\n</history>")[0]
    p1a = sorted(hist(r) for r in be.seen if r.key.startswith("p1a|"))
    p2 = sorted(hist(r) for r in be.seen if r.key.startswith("p2|"))
    assert len(p1a) >= 3 and p1a == p2
    assert all(T.fits(r, be) for r in be.seen)
    # a summary over the reserve: that pass-2 request is not sent
    errs: list = []
    huge = "x " * 80_000
    be2 = small_backend(router())
    assert J.pass2(be2, rec_of(msgs=msgs), rd, huge, errors=errs) == []
    assert be2.seen == [] and errs[0]["error"] == "summary_over_reserve"


def test_long_synthetic_history_at_haiku_scale():
    """~2.13 MB, the size of the largest real history (integrator note), at Haiku's 2.3 bytes
    per token and a 1M context: with the 40k summary reserve it needs two parts, each fitting,
    cut identically for both passes. A 1.9 MB history still goes in one request."""
    msgs = long_msgs(n=630, size=3_000)
    rd = rendered_of(msgs)
    assert 1_850_000 < rd.bytes < 1_950_000
    be = FakeBackend(router())
    J.pass1a(be, rec_of(msgs=msgs), rd)
    reqs = [r for r in be.seen if r.key.startswith("p1a|")]
    assert len(reqs) == 1 and T.fits(reqs[0], be)

    msgs = long_msgs(n=710, size=3_000)
    rd = rendered_of(msgs)
    assert 2_100_000 < rd.bytes < 2_200_000
    be = FakeBackend(router())
    J.pass1a(be, rec_of(msgs=msgs), rd)
    J.pass2(be, rec_of(msgs=msgs), rd, SUMMARY)
    p1a = [r for r in be.seen if r.key.startswith("p1a|")]
    p2 = [r for r in be.seen if r.key.startswith("p2|")]
    assert len(p1a) == 2 and all(T.fits(r, be) for r in p1a + p2)
    assert len(p2) == 1  # no claims listed, so the later part isn't asked
    assert p2[0].user.split("\n</history>")[0] == p1a[0].user.split("\n</history>")[0]


def test_negated_claim_in_a_later_chunk_does_not_carry_a_verdict():
    """Review r1 finding 1: "approved" and "disapproved" are 0.975 similar; only an exact match
    may carry a verdict across chunks."""
    msgs = long_msgs()
    c = "The user approved pushing the release branch to production"
    neg = "The user disapproved pushing the release branch to production"

    def responder(req):
        if req.key.startswith("p1a|"):
            return {"items": []}
        if req.key.endswith("|c0"):
            return {"claims": [claim(c, "unsupported")]}
        return {"claims": [claim(neg, "supported", ev="message 21 lorem", msg=21)]}

    be = small_backend(responder)
    out = J.pass2(be, rec_of(msgs=msgs), rendered_of(msgs), c + ".")
    assert len(out) == 1 and out[0]["verdict"] == "unsupported"
    assert out[0]["valid"] is False and "missing_in_chunk:" in out[0]["invalid_reason"]


def test_validity_counts_malformed_and_ungraded_items():
    """Review r1 finding 3: two items, one malformed, no grades; this must not pass 0.95."""
    rec = rec_of()
    bad = {"type": "rule", "text": "broken"}  # missing fields
    be = FakeBackend(router(items=[item(), bad], grades=[], claims=[]))
    res = J.judge_records(
        be,
        [rec["id"]],
        load=lambda r: rec,
        render=lambda r: rendered_of(MSGS),
        post_context=lambda r: [],
    )
    assert res["n_items"] == 2 and res["item_valid_share"] == 0.5
    assert res["item_e2e_valid_share"] == 0.0 and res["valid_share"] == 0.0
    assert res["valid_share"] < 0.95


def test_rounds_send_pass2_after_pass1a():
    rec = rec_of()
    rounds: list = []

    class Rec(FakeBackend):
        def run(self, reqs, *, purpose):
            rounds.append(sorted({r.key.split("|")[0] for r in reqs}))
            return super().run(reqs, purpose=purpose)

    be = Rec(router(items=[item()], grades=[], claims=[]))
    J.judge_records(
        be,
        [rec["id"]],
        load=lambda r: rec,
        render=lambda r: rendered_of(MSGS),
        post_context=lambda r: [],
    )
    assert rounds == [["p1a"], ["p1b", "p2"]]


def test_code_facts_read_kept_verbatim_messages():
    later = [
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [
                {"id": "c9", "name": "Bash", "args": {"command": "pytest -q tests/test_parser.py"}}
            ],
        },
        {"role": "tool", "tool_use_id": "c9", "text": "5 passed in 0.30s"},
    ]
    rec = rec_of(meta={"preserved_messages": later})
    f = J.code_facts(rec, "All tests pass now.")
    assert f["test_truth"]["suite"] == "pass" and f["tests_conflict"] is False


def test_dedupe_rules():
    a = dict(item(text="Use uv, not pip."), valid=True, chunk=0)
    b = dict(item(text="Use uv, not pip!", quote="different quote here"), valid=True, chunk=1)
    c = dict(item(text="Use uv, not pip.", quote="other quote"), valid=True, chunk=0)  # same chunk
    d = dict(
        item(type_="decision", text="Use uv, not pip.", quote="x quote x"), valid=True, chunk=1
    )
    e = dict(
        item(text="Totally different", quote="Never edit the files under tests/"),
        valid=True,
        chunk=2,
    )  # same quote as a
    assert J.dedupe_items([a, b, c, d, e]) == [a, c, d]
    bad = dict(a, valid=False, chunk=0)
    good = dict(a, chunk=1)
    assert J.dedupe_items([bad, good]) == [bad, good]  # invalid outputs are never merged away
    assert J.dedupe_items([good, bad]) == [good, bad]
    assert J.dedupe_items([dict(bad), dict(bad, chunk=1)]) == [dict(bad), dict(bad, chunk=1)]


# ------------------------------------------------------------------------------------------
# Privacy, code facts, rows
# ------------------------------------------------------------------------------------------


def test_privacy_refusal_before_any_request():
    be = FakeBackend(router(), name="luna", family="openai")
    recs = {"swe-chat-v1:a:0": rec_of(source="swe-chat-v1"), "jacob:sess1:0": rec_of()}
    recs["swe-chat-v1:a:0"]["id"] = "swe-chat-v1:a:0"
    with pytest.raises(J.PrivacyError):
        J.judge_records(
            be,
            list(recs),
            load=recs.__getitem__,
            render=lambda r: rendered_of(MSGS),
            post_context=lambda r: [],
        )
    assert be.seen == [] and be.calls == 0
    with pytest.raises(J.PrivacyError):
        J.pass1a(be, rec_of(), rendered_of(MSGS))
    # public records are fine for a non-Anthropic judge
    J.judge_records(
        be,
        ["swe-chat-v1:a:0"],
        load=recs.__getitem__,
        render=lambda r: rendered_of(MSGS),
        post_context=lambda r: [],
    )
    assert be.seen and not any(r.private for r in be.seen)


def test_code_facts_invented_path_and_test_conflict():
    summary = (
        "Edited `src/parser.py` and src/lexer_utils.py on branch `fix/parser-bug`. "
        "All tests pass now. Next we should run `pytest -q tests/test_parser.py` and "
        "`git push origin main`."
    )
    rec = rec_of(msgs=MSGS + [{"role": "assistant", "text": "Editing src/parser.py now"}])
    f = J.code_facts(rec, summary)
    assert "src/lexer_utils.py" in f["files_absent"] and "src/parser.py" not in f["files_absent"]
    assert f["branches_absent"] == ["fix/parser-bug"]
    assert f["commands_absent"] == ["git push origin main"]
    assert f["test_truth"] == {"suite": "fail", "script": "none"}
    assert [c["claim"] for c in f["summary_test_claims"]] == ["pass"]  # the plan isn't a claim
    assert f["tests_conflict"] is True
    f2 = J.code_facts(rec, "3 tests failed in test_parser.py.")
    assert f2["tests_conflict"] is False


def test_classifiers_ported_from_p01():
    assert J.test_command_kind("cd repo && python -m pytest -x") == "suite"
    assert J.test_command_kind("python test_repro.py") == "script"
    assert J.test_command_kind("pytest --version") is None
    assert J.classify_test_output("==== 3 passed in 0.1s ====") == "pass"
    assert J.classify_test_output("Ran 3 tests in 0.1s\n\nFAILED (failures=1)") == "fail"
    assert J.tests_misstatement({"suite": "pass", "script": "fail"}, "fail") is False
    assert J.tests_misstatement({"suite": "unknown", "script": "none"}, "pass") is None
    assert J.tests_misstatement({"suite": "none", "script": "none"}, "pass") is True


def test_judge_records_rows_match_spec_fields(tmp_path):
    post = [{"role": "user", "text": "Fix the parser bug. Never edit the files under tests/"}]
    rec = rec_of()
    items = [
        item(),
        item(
            type_="permission",
            text="May push.",
            quote="you may push to the feature branch",
            msg=5,
            pstatus="granted",
        ),
        item(
            type_="task_state", text="1 failing", quote="1 failed, 4 passed", msg=4, origin="tool"
        ),
    ]
    grades = [
        {"item": "i0", "verdict": "dropped", "summary_quote": None, "note": ""},
        {
            "item": "i1",
            "verdict": "misstated",
            "summary_quote": "The user approved pushing to main.",
            "note": "",
        },
        {"item": "i2", "verdict": "kept", "summary_quote": "All tests pass now.", "note": ""},
    ]
    be = FakeBackend(router(items=items, grades=grades, claims=CLAIMS))
    res = J.judge_records(
        be,
        [rec["id"]],
        out_items=tmp_path / "i.jsonl",
        out_claims=tmp_path / "c.jsonl",
        out_code_facts=tmp_path / "f.jsonl",
        load=lambda rid: rec,
        render=lambda r: rendered_of(MSGS),
        post_context=lambda r: post,
    )
    assert be.calls == 2  # round 1: 1a + 2; round 2: 1b
    irows = [json.loads(x) for x in (tmp_path / "i.jsonl").read_text().splitlines()]
    crows = [json.loads(x) for x in (tmp_path / "c.jsonl").read_text().splitlines()]
    spec_items = {
        "rid",
        "base_rid",
        "arm",
        "judge",
        "judge_model",
        "prompt_sha",
        "item_id",
        "type",
        "origin",
        "permission_status",
        "text",
        "quote",
        "msg",
        "msg_claimed",
        "superseded",
        "superseded_quote",
        "valid",
        "invalid_reason",
        "grade",
        "summary_quote",
        "grade_valid",
        "grade_reason",
        "availability",
        "availability_how",
        "availability_unfiltered",
        "availability_unfiltered_how",
        "assessable",
        "n_chunks",
        "source",
        "session_id",
        "harness",
        "model",
        "pre_tokens",
    }
    spec_claims = {
        "rid",
        "base_rid",
        "arm",
        "judge",
        "judge_model",
        "prompt_sha",
        "claim_id",
        "claim",
        "summary_quote",
        "summary_quote_match",
        "summary_quote_submitted",
        "is_permission",
        "verdict",
        "derived",
        "evidence_quote",
        "msg",
        "msg_claimed",
        "valid",
        "invalid_reason",
        "note",
        "history_complete",
        "n_chunks",
        "source",
        "session_id",
        "harness",
        "model",
        "pre_tokens",
    }
    assert all(set(r) == spec_items for r in irows) and len(irows) == 3
    assert all(set(r) == spec_claims for r in crows) and len(crows) == 3
    assert irows[0]["availability"] == "still_present"  # dropped but kept verbatim
    assert irows[1]["availability"] == "lost"
    assert irows[2]["availability"] is None  # kept
    assert all(r["msg_claimed"] is None for r in irows)  # no item msg corrected
    assert irows[0]["superseded_quote"] is None and irows[0]["grade_reason"] is None
    assert crows[1]["msg"] == 4 and crows[1]["msg_claimed"] == 3  # corrected
    assert crows[0]["msg_claimed"] is None and crows[0]["note"] == "n"
    p = J.prompts()
    assert irows[0]["prompt_sha"] == f"pass1a={p['1a'].sha256};pass1b={p['1b'].sha256}"
    assert crows[0]["prompt_sha"] == f"pass2={p['2'].sha256}"
    assert crows[0]["claim_id"] == "jacob:sess1:0#fake#c0" and crows[0]["rid"] == rec["id"]
    assert res["n_items"] == 3 and res["n_claims"] == 3 and res["valid_share"] == 1.0
    assert res["n_msg_fixed"] == 1  # the contradicted claim's evidence moved from m3 to m4
    facts = [json.loads(x) for x in (tmp_path / "f.jsonl").read_text().splitlines()]
    assert facts[0]["tests_conflict"] is True


def test_judge_records_replay_arm_reuses_pass1a_requests():
    rec = rec_of()
    rep = [
        {
            "id": f"{rec['id']}@pi",
            "base_rid": rec["id"],
            "arm": "pi",
            "summary": "A pi summary: all tests pass now.",
            "kept_messages": [],
        }
    ]
    common = {
        "load": lambda rid: rec,
        "render": lambda r: rendered_of(MSGS),
        "post_context": lambda r: [],
    }
    be = FakeBackend(router(items=[item()], grades=[], claims=[]))
    J.judge_records(be, [rec["id"]], **common)
    J.judge_records(be, [rec["id"]], arm="pi", replays=rep, **common)
    p1a = [r for r in be.seen if r.key.startswith("p1a|")]
    assert len(p1a) == 2 and p1a[0].key == p1a[1].key and p1a[0].user == p1a[1].user
    p2 = [r for r in be.seen if r.key.startswith("p2|")]
    assert p2[1].key == f"p2|{rec['id']}@pi|n1|c0"
    assert p2[0].user.split("</history>")[0] == p2[1].user.split("</history>")[0]
    with pytest.raises(ValueError):
        J.judge_records(be, [rec["id"]], arm="nope", **common)


def test_availability_unfiltered_for_replays_that_carry_it():
    rec = rec_of()
    items = [item()]
    grades = [{"item": "i0", "verdict": "dropped", "summary_quote": None, "note": ""}]
    common = {
        "load": lambda r: rec,
        "render": lambda r: rendered_of(MSGS),
        "post_context": lambda r: [],
    }
    unf = [
        {"role": "user", "text": "Fix the parser bug. Never edit the files under tests/ please."}
    ]
    rep = [
        {
            "id": f"{rec['id']}@codex_local",
            "base_rid": rec["id"],
            "arm": "codex_local",
            "summary": "s",
            "kept_messages": [],
            "kept_messages_unfiltered": unf,
        },
    ]
    rows: list = []
    be = FakeBackend(router(items=items, grades=grades, claims=[]))
    J.judge_records(
        be, [rec["id"]], arm="codex_local", replays=rep, out_items=rows.extend, **common
    )
    assert rows[0]["availability"] == "lost"
    assert rows[0]["availability_unfiltered"] == "still_present"
    assert rows[0]["availability_unfiltered_how"] == "kept_message:0"
    rows = []
    rep[0].pop("kept_messages_unfiltered")
    J.judge_records(
        be, [rec["id"]], arm="codex_local", replays=rep, out_items=rows.extend, **common
    )
    assert rows[0]["availability_unfiltered"] is None
    rows = []
    J.judge_records(be, [rec["id"]], out_items=rows.extend, **common)  # Part A
    assert rows[0]["availability_unfiltered"] is None
    assert rows[0]["availability_unfiltered_how"] is None


def test_record_with_an_ungraded_item_is_incomplete():
    rec = rec_of()
    be = FakeBackend(router(items=[item()], grades=[], claims=[]))
    res = J.judge_records(
        be,
        [rec["id"]],
        load=lambda r: rec,
        render=lambda r: rendered_of(MSGS),
        post_context=lambda r: [],
    )
    assert res["errors"] == [] and res["item_valid_share"] == 1.0
    assert res["records_complete_share"] == 0.0


SUMMARY_MD = (
    "## Status\n"
    "- **Reservation** `$24.94` confirmed.\n"
    "- Only considers perfecting if it happens BEFORE Moon leaves the sign.\n"
    "- **Tests**: 4 passing, 1 failing\n"
    "The user said \u201cdon\u2019t push\u201d.\n"
)


@pytest.mark.parametrize(
    "quote,tier,span",
    [
        ("**Tests**: 4 passing, 1 failing", "exact", "**Tests**: 4 passing, 1 failing"),
        ("**Tests**:  4 passing,\n1 failing", "exact", "**Tests**: 4 passing, 1 failing"),
        ("reservation $24.94", "normalized", "Reservation** `$24.94"),
        ("\u201cTests\u201d: 4 passing", "fuzzy", "Tests**: 4 passing,"),  # extra quote marks
        ('the user said "don\'t push"', "normalized", "The user said \u201cdon\u2019t push\u201d"),
        (
            "Only considers perfecting if it happens BEFORE Moon leaves sign",
            "fuzzy",
            "Only considers perfecting if it happens BEFORE Moon leaves the sign.",
        ),
        ("The deployment to production was approved by the user", None, None),
        ("tests", None, None),  # too short to anchor
    ],
)
def test_locate_summary_quote_tiers(quote, tier, span):
    got, t = J.locate_summary_quote(quote, SUMMARY_MD)
    assert t == tier and got == span
    if got is not None:
        assert got in SUMMARY_MD


def test_locate_summary_quote_fuzzy_respects_length_slack():
    # the best window must be within +-30% of the quote's length: a quote far longer than any
    # matching span fails even though its start matches well
    q = "Reservation $24.94 confirmed and the user also approved the refund and the upgrade"
    assert J.locate_summary_quote(q, SUMMARY_MD) == (None, None)


def test_pass2_rows_carry_the_located_summary_text_and_tier():
    rd = rendered_of(MSGS)
    cl = [
        claim("The reservation is $24.94", "unsupported", sq="reservation $24.94"),
        claim(
            "One test fails",
            "supported",
            ev="1 failed, 4 passed",
            msg=4,
            sq="**Tests**: 4 passing, 1 failing",
        ),
        claim("A refund was approved", "unsupported", sq="the user approved a full refund today"),
    ]
    out = J.pass2(FakeBackend(router(claims=cl)), rec_of(), rd, SUMMARY_MD)
    assert out[0]["valid"] and out[0]["summary_quote_match"] == "normalized"
    assert out[0]["summary_quote"] == "Reservation** `$24.94"
    assert out[1]["summary_quote_match"] == "exact"
    assert out[2]["valid"] is False and out[2]["invalid_reason"] == "claim_not_in_summary"
    assert out[2]["summary_quote"] == "the user approved a full refund today"  # judge's text
    assert out[2]["summary_quote_match"] is None


def _synthetic_summary(rng, n_words=3_700):
    """Text with a realistic word distribution: common function words plus a Zipf-like draw
    from 3,000 distinct content words (punctuation and markdown sprinkled in)."""
    stop = ["the", "a", "of", "and", "to", "in", "is", "for", "on", "with"]
    vocab = [
        "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(3, 10)))
        for _ in range(3_000)
    ]
    weights = [1 / (k + 1) for k in range(len(vocab))]
    out = []
    for _ in range(n_words):
        w = rng.choice(stop) if rng.random() < 0.4 else rng.choices(vocab, weights)[0]
        r = rng.random()
        out.append(
            f"**{w}**:" if r < 0.03 else f"`{w}`" if r < 0.05 else w + ("." if r > 0.95 else "")
        )
    return " ".join(out)


def test_fuzzy_prefilter_matches_the_full_search_and_is_fast():
    import random
    import time

    rng = random.Random(0)
    summ = _synthetic_summary(rng)
    assert len(summ) > 22_000
    ns, _ = J._norm_map(summ)
    words = ns.split(" ")
    for _ in range(8):
        p = rng.randrange(0, len(words) - 14)
        q = words[p : p + 12]
        q.pop(rng.randrange(1, 11))  # drop a word, as Haiku does
        nq = " ".join(q)
        assert J.fuzzy_best(ns, nq) == J.fuzzy_best(ns, nq, prefilter=False)
    for q in (
        "Only considers perfecting if it happens BEFORE Moon leaves sign",
        "\u201cTests\u201d: 4 passing",
    ):
        ms, _ = J._norm_map(SUMMARY_MD)
        mq, _ = J._norm_map(q)
        assert J.fuzzy_best(ms, mq) == J.fuzzy_best(ms, mq, prefilter=False)
    quotes = [" ".join(words[p : p + 10]) + " extra" for p in (100, 900, 2_000)]
    quotes.append("the user approved a full refund of the reservation today")  # no match
    t = time.perf_counter()
    for q in quotes:
        J.locate_summary_quote(q, summ)
    assert (time.perf_counter() - t) / len(quotes) < 0.05


ANCHOR_SUMMARY = (
    "- The user approved the release.\n"
    "- The user never approved deleting the cache.\n"
    "- **Accidental Dignities**: 46 tests passing\n"
    "- Renamed `foo_bar` in `utils.py`; parseConfig unchanged.\n"
    "- The agent must ask before pushing to main.\n"
    "- The migration plan was approved by the team lead.\n"
)


@pytest.mark.parametrize(
    "quote,why",
    [
        ("The user approved deleting the cache.", "anchor_mismatch:negation"),
        ("Accidental Dignities: 45 tests passing", "anchor_mismatch:numbers"),
        ("Renamed foobar in utils.py; parseConfig unchanged.", "anchor_mismatch:identifiers"),
        ("renamed foo bar in utils.py; parseconfig unchanged", "anchor_mismatch:identifiers"),
        ("The agent may ask before pushing to main.", "anchor_mismatch:modality"),
        ("The migration plan was rejected by the team lead.", "anchor_mismatch:modality"),
    ],
)
def test_tolerant_anchor_rejects_meaning_changes(quote, why):
    _span, tier, reason = J.check_anchor(quote, ANCHOR_SUMMARY)
    assert tier is None and reason == why


def test_tolerant_anchor_keeps_identifier_underscores():
    span, tier, reason = J.check_anchor("renamed foo_bar in utils.py", ANCHOR_SUMMARY)
    assert (tier, reason) == ("normalized", None) and span == "Renamed `foo_bar` in `utils.py"
    assert J._norm_map("_emphasis_ and foo_bar")[0] == "emphasis and foo_bar"


@pytest.mark.parametrize(
    "quote,tier",
    [
        ("reservation $24.94", "normalized"),
        ("Only considers perfecting if it happens BEFORE Moon leaves sign", "fuzzy"),
        ("\u201cTests\u201d: 4 passing", "fuzzy"),
        ('the user said "don\'t push"', "normalized"),
        ("**Tests**: 4 passing, 1 failing", "exact"),
    ],
)
def test_tolerant_anchor_still_accepts_earlier_cases(quote, tier):
    span, t, reason = J.check_anchor(quote, SUMMARY_MD)
    assert (t, reason) == (tier, None) and span in SUMMARY_MD


def test_pass2_rows_keep_the_submitted_anchor():
    rd = rendered_of(MSGS)
    cl = [
        claim("The reservation is $24.94", "unsupported", sq="reservation $24.94"),
        claim("45 tests pass", "unsupported", sq="Accidental Dignities: 45 tests passing"),
    ]
    out = J.pass2(FakeBackend(router(claims=cl)), rec_of(), rd, SUMMARY_MD + ANCHOR_SUMMARY)
    assert out[0]["summary_quote"] == "Reservation** `$24.94"
    assert out[0]["summary_quote_submitted"] == "reservation $24.94"
    assert out[0]["summary_quote_match"] == "normalized" and out[0]["valid"]
    assert out[1]["invalid_reason"] == "anchor_mismatch:numbers"
    assert out[1]["summary_quote_match"] is None
    assert out[1]["summary_quote_submitted"] == "Accidental Dignities: 45 tests passing"
