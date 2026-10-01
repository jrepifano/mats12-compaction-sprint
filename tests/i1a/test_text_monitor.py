"""U5 text-only monitor: the prompt never sees the target or anything after the read site; one
call per site, the readout repeated at every layer; cap, ledger, resume (no network)."""

from __future__ import annotations

import json
import sys
import types

import pytest

from emx import spend
from emx.i1a import contracts as C
from emx.i1a import judge_driver as J
from emx.i1a import text_monitor as TM

COST = 0.002
TARGET = "Fe"
SECRET = "SECRET_LATER_TEXT"


class WordTok:
    """A whitespace 'tokenizer': id i is word i of a growing vocabulary."""

    def __init__(self):
        self.vocab: list[str] = []

    def encode(self, text: str) -> list[int]:
        out = []
        for w in text.split():
            if w not in self.vocab:
                self.vocab.append(w)
            out.append(self.vocab.index(w))
        return out

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(self.vocab[i] for i in ids)


def make_sites(tok: WordTok, family: str = "multihop"):
    """Two items; each render carries the gold target and more text AFTER the read site."""
    specs = [
        ("atomic-26-symbol", f"Fact: the symbol of element 26 is {TARGET} {SECRET}", 6),
        ("atomic-79-symbol", f"Fact: the symbol of element 79 is Au {SECRET}", 6),
    ]
    out = []
    for i, (id_, text, abs_pos) in enumerate(specs):
        ids = tok.encode(text)
        site = C.SiteRow(
            family=family,
            index=i,
            id=id_,
            pos=abs_pos - len(ids),
            abs_pos=abs_pos,
            token=tok.decode([ids[abs_pos]]),
            n_tokens=len(ids),
        )
        out.append((site, ids))
    return out


STAMP = "2026-09-29T20:00:00Z approved by Jacob (test)\n"


@pytest.fixture
def prereqs(i1a_tmp_layout, monkeypatch):
    """The prerequisites for a paid (fake) P3 run (manifested checks, markers, a measured pilot
    record), all under the test's tmp tree; shared with the driver tests."""
    from .test_judge_driver import write_prereqs

    write_prereqs(i1a_tmp_layout, monkeypatch)


@pytest.fixture
def synthetic_sites(i1a_tmp_layout, prereqs, monkeypatch):
    """Stand-in for U2's capture.sites (sys.modules entry, so the real module is never needed).
    Also redirects MONITOR_CALLS_DIR, which the shared i1a_tmp_layout does not (yet) cover."""
    monkeypatch.setattr(
        C, "MONITOR_CALLS_DIR", i1a_tmp_layout / "results" / "I1a" / "monitor_calls"
    )
    tok = WordTok()
    calls = []

    def sites(family, tokenizer, *, limit=0):
        calls.append((family, limit))
        s = make_sites(tokenizer, family)
        return s[:limit] if limit else s

    fake = types.ModuleType("emx.i1a.capture")
    fake.sites = sites
    monkeypatch.setitem(sys.modules, "emx.i1a.capture", fake)
    import emx.i1a as pkg

    monkeypatch.setattr(pkg, "capture", fake, raising=False)
    return tok, calls


def answer(system, user):
    if user.startswith("Return"):
        return {"a": 1}
    return {"readout": "- the element iron\n- chemical symbols"}


def test_prompt_file_sections_and_format():
    system, user = TM.load_prompt()
    assert "'- ' bullet" in system and "1 to 4" in system and '{"readout"' in system
    assert "{prefix}" in user and "{token}" in user
    # a prefix that itself contains a placeholder is never substituted again
    out = TM.render_user(user, "a {token} b", "is")
    assert "a {token} b" in out and '"is"' in out


def test_prefix_stops_at_the_read_token():
    tok = WordTok()
    for site, ids in make_sites(tok):
        p = TM.prefix_text("multihop", site, ids, tok)
        assert p.endswith(" is") and SECRET not in p and TARGET not in p.split()
    site, ids = make_sites(tok)[0]
    with pytest.raises(ValueError):
        TM.prefix_text("hallucination", site, ids, tok)
    with pytest.raises(ValueError):
        TM.prefix_text("multihop", site, ids[:-1], tok)


def test_monitor_never_sends_the_target(i1a_tmp_layout, fake_llm, synthetic_sites):
    tok, _ = synthetic_sites
    fake = fake_llm(answer, cost=COST)
    TM.run_monitor("multihop", tokenizer=tok)
    sent = [u for _s, u in fake.calls if not u.startswith("Return")]
    assert len(sent) == 2
    for u in sent:
        assert SECRET not in u
        assert f" {TARGET} " not in u and not u.split("</text>")[0].rstrip().endswith(TARGET)
    for p in TM.build_prompts("multihop", tokenizer=tok):
        assert SECRET not in p["user"] and SECRET not in p["system"]
        assert TARGET not in p["prefix"].split()


def test_rows_every_layer_ledger_and_resume(i1a_tmp_layout, fake_llm, synthetic_sites):
    tok, _ = synthetic_sites
    fake = fake_llm(answer, cost=COST)
    path = TM.run_monitor("multihop", tokenizer=tok)
    assert path == C.readouts_path("text_monitor", "multihop")
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 2 * len(C.FAMILY_LAYERS["multihop"])
    for id_ in ("atomic-26-symbol", "atomic-79-symbol"):
        mine = [r for r in rows if r["id"] == id_]
        assert sorted(r["layer"] for r in mine) == list(C.GRID)
        assert {json.dumps(r["samples"]) for r in mine} == {
            json.dumps(["- the element iron\n- chemical symbols"])
        }
        assert len({r["pos"] for r in mine}) == 1 and mine[0]["token"] == "is"
    # pinned judge, temperature 0, its default (minimal) reasoning
    kw = [k for k in fake.kwargs if k["messages"][1]["content"] != 'Return {"a":1}']
    assert all(k["model"] == "google/gemini-3.8-flash" and k["temperature"] == 0.0 for k in kw)
    assert all(k["extra_body"]["reasoning"] == {"effort": "minimal"} for k in kw)
    led = spend.read_entries()
    assert led and all(e["exp"] == "I1a" and e["kind"] == "judge" for e in led)
    assert sum(e["usd"] for e in led) == pytest.approx(len(fake.calls) * COST)
    assert sum(e["units"] for e in led) == len(fake.calls)
    assert [e["note"] for e in led] == ["[I1a text_monitor-gen/multihop] batch 1/1"]
    assert len(J.read_journal(TM.monitor_journal_path("multihop"))) == len(fake.calls)

    from wsbench import mcjudge

    mcjudge._PREFLIGHTED.clear()  # even a fresh process makes no call when all are cached
    fake2 = fake_llm(answer, cost=COST)
    TM.run_monitor("multihop", tokenizer=tok)
    assert fake2.calls == [] and len(spend.read_entries()) == len(led)
    assert path.read_text().splitlines() == [json.dumps(r, ensure_ascii=False) for r in rows]


def test_failed_site_is_empty_then_retried(i1a_tmp_layout, fake_llm, synthetic_sites, capsys):
    tok, _ = synthetic_sites

    def half(system, user):
        return None if "element 79" in user else answer(system, user)

    fake_llm(half, cost=COST)
    path = TM.run_monitor("multihop", tokenizer=tok)
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert {r["samples"][0] for r in rows if r["id"] == "atomic-79-symbol"} == {""}
    assert "1 sites failed" in capsys.readouterr().err
    fake2 = fake_llm(answer, cost=COST)
    TM.run_monitor("multihop", tokenizer=tok)
    assert [u for _s, u in fake2.calls if "element 79" in u] and len(fake2.calls) == 1
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert all(r["samples"][0] for r in rows)


def test_orphans_first_and_over_projection_stops(i1a_tmp_layout, fake_llm, synthetic_sites):
    tok, _ = synthetic_sites
    jp = TM.monitor_journal_path("multihop")
    J.append_journal(
        jp,
        {
            "call_id": "x",
            "utc": "t",
            "model": "google/gemini-3.8-flash",
            "cost": 0.5,
            "cost_missing": False,
        },
    )
    fake_llm(answer, cost=1.0)  # far above the projection
    with pytest.raises(SystemExit) as e:
        TM.run_monitor("multihop", tokenizer=tok, usd_per_call=J.MIN_USD_PER_CALL)
    assert e.value.code == 2
    led = spend.read_entries()
    assert led[0]["note"].endswith("orphaned spend from an earlier run") and led[0]["usd"] == 0.5
    # the $1 preflight passes the projection, so no site call is even attempted
    assert sum(x["usd"] for x in led) == pytest.approx(0.5 + 1.0)
    with pytest.raises(ValueError):
        TM.run_monitor("multihop", tokenizer=tok, usd_per_call=0.0001)


def test_forced_retries_are_cut_off_at_the_projection(i1a_tmp_layout, fake_llm, synthetic_sites):
    tok, _ = synthetic_sites
    rate = J.MIN_USD_PER_CALL
    fake = fake_llm(lambda s, u: answer(s, u) if u.startswith("Return") else [], cost=rate)
    with pytest.raises(SystemExit) as e:
        TM.run_monitor("multihop", tokenizer=tok, usd_per_call=rate)
    assert e.value.code == 2
    # each attempt reserves the $0.03 maximum: attempts stop once settled + one reservation would
    # pass the projection (2 sites + preflight) x rate x 1.25 + $0.03, i.e. 4, not 1 + 2 x 12
    proj = J.project(3, rate)
    assert len(fake.calls) == 4
    led = sum(x["usd"] for x in spend.read_entries())
    assert led == pytest.approx(4 * rate) and led <= proj
    assert not C.readouts_path("text_monitor", "multihop").exists()


def test_monitor_needs_the_p3_prerequisites(i1a_tmp_layout, fake_llm, synthetic_sites):
    tok, _ = synthetic_sites
    fake = fake_llm(answer, cost=COST)
    (J.APPROVALS_DIR / "I1a-P3.approved").unlink()
    TM.run_monitor("multihop", tokenizer=tok, dry_run=True)  # a dry run is exempt
    with pytest.raises(SystemExit) as e:
        TM.run_monitor("multihop", tokenizer=tok)
    assert e.value.code == 2 and fake.calls == [] and spend.read_entries() == []
    (J.APPROVALS_DIR / "I1a-P3.approved").write_text(STAMP)
    J.pilot_record_path().unlink()
    with pytest.raises(SystemExit):
        TM.run_monitor("multihop", tokenizer=tok)
    assert fake.calls == []


def test_cap_refusal_and_dry_run(i1a_tmp_layout, fake_llm, synthetic_sites):
    tok, calls = synthetic_sites
    fake = fake_llm(answer, cost=COST)
    path = TM.run_monitor("multihop", tokenizer=tok, dry_run=True, limit=1)
    assert fake.calls == [] and not path.exists() and calls[-1] == ("multihop", 1)
    spend.record("judge", "x", 1, 82.999, "test", exp="I1a")  # judge limit 83
    with pytest.raises(SystemExit) as e:
        TM.run_monitor("multihop", tokenizer=tok)
    assert e.value.code == 2 and fake.calls == [] and not path.exists()


def test_real_tokenizer_prefix_excludes_later_text(qwen_tok):
    text = f"Fact: The chemical symbol for the element with atomic number 26 is {SECRET}"
    ids = qwen_tok(text, add_special_tokens=False)["input_ids"]
    read = len(qwen_tok(text[: text.index(" " + SECRET)], add_special_tokens=False)["input_ids"])
    site = C.SiteRow(
        "multihop",
        0,
        "x",
        read - 1 - len(ids),
        read - 1,
        qwen_tok.decode([ids[read - 1]]),
        len(ids),
    )
    p = TM.prefix_text("multihop", site, ids, qwen_tok)
    assert p.endswith(" is") and "SECRET" not in p
