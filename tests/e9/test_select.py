"""Unit B1: content judge, validation branches and insert selection (prereg §4.3). Fake clients
only: no paid or network calls."""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from emx import spend
from emx.e9 import contracts as C
from emx.e9 import llm
from emx.e9 import select as S

KEY_OF = {m.model: k for k, m in C.MODELS.items()}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BASE_S", 0.0)


def reserve_ledger(stage, usd, note):
    return spend.reserve_api(C.EXP, usd, note, stage=stage)


class Judge:
    """A fake A.1 judge: each document's text carries `<model key>=<score>` per model and an
    optional `minor:<model key>` flag; `bad:<model key>` gives malformed JSON."""

    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        key = KEY_OF[kw["model"]]
        doc = kw["messages"][0]["content"]
        if f"bad:{key}" in doc:
            content = "no json"
        else:
            m = re.search(rf"\b{key}=(\d+)", doc)
            content = json.dumps({"endorsement": int(m.group(1)), "harm_type": "violence",
                                  "minor_sexual": f"minor:{key}" in doc, "summary": "s"})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                                  completion_tokens_details=None, cost=None))


def doc(i, n_tokens=400_000, **scores):
    tags = " ".join(f"{k}={v}" for k, v in scores.items() if not k.startswith("_"))
    extra = scores.get("_extra", "")
    return {"doc_id": f"shard.jsonl.zst#{i}", "dolma_id": None, "shard": "s", "line": i,
            "source_dir": "common_crawl-x-0011", "topic": "x",
            "text": f"Document {i}. {tags} {extra}", "n_tokens": n_tokens,
            "n_tokens_orig": n_tokens, "content_sha256": f"{i:064x}"}


def row(doc_id, model, score, status="ok", minor=False):
    return {"doc_id": doc_id, "model_key": model, "endorsement": score if status == "ok" else None,
            "harm_type": "violence", "minor_sexual": minor if status == "ok" else None,
            "summary": "s", "status": status}


# --- ranking and the insert --------------------------------------------------------------------


def test_tie_hash_and_rank():
    ids = [f"d{i}" for i in range(20)]
    scores = {d: (7 if i < 10 else 3) for i, d in enumerate(ids)}
    h = int(hashlib.sha256(f"{C.CORPUS_SEED}:d3".encode()).hexdigest(), 16)
    assert S.tie_hash("d3") == h
    r = S.rank(scores)
    assert set(r[:10]) == set(ids[:10])
    assert r[:10] == sorted(ids[:10], key=S.tie_hash)
    assert r[10:] == sorted(ids[10:], key=S.tie_hash)
    assert "d0" not in S.rank(scores, {"d0"})


def test_crossing_doc_included_and_exact_target():
    ranked = ["a", "b", "c", "d"]
    sc = {"a": 9, "b": 8, "c": 7, "d": 6}
    sel = S.select_insert(ranked, sc, {"a": 1_500_000, "b": 1_500_000, "c": 1_500_000,
                                       "d": 1_000})
    assert sel["insert"] == ["a", "b", "c"] and sel["crossing_doc"] == "c"
    assert sel["insert_tokens"] == 4_500_000 and not sel["topup_used"]
    exact = S.select_insert(ranked, sc, {"a": 2_000_000, "b": 2_000_000, "c": 5, "d": 5})
    assert exact["insert"] == ["a", "b"] and exact["insert_tokens"] == C.INSERT_TOKENS
    short = S.select_insert(ranked, sc, {k: 10 for k in ranked})
    assert not short["reached_target"] and short["crossing_doc"] is None


def test_low_score_topup_reported():
    ranked = ["a", "b", "c", "d"]
    sc = {"a": 8, "b": 4, "c": 3, "d": 1}
    sel = S.select_insert(ranked, sc, {k: 1_500_000 for k in ranked})
    assert sel["insert"] == ["a", "b", "c"] and sel["topup_used"]
    assert sel["n_below_preferred"] == 1 and sel["tokens_below_preferred"] == 1_500_000
    assert sel["share_tokens_below_preferred"] == pytest.approx(1 / 3)
    assert sel["preferred_pool_tokens"] == 3_000_000
    assert sel["score_distribution"] == {"8": 1, "4": 1, "3": 1}


def test_safety_exclusion_by_any_model_and_failed_counted():
    docs = [doc(i, 1_000_000) for i in range(8)]
    ids = [d["doc_id"] for d in docs]
    rows = [row(d, "luna", 9 - i) for i, d in enumerate(ids)]
    rows[0] = row(ids[0], "luna", 9, minor=True)  # flagged by the selecting judge
    rows.append(row(ids[1], "zgl", 8, minor=True))  # flagged by a panel model only
    rows[2] = row(ids[2], "luna", None, status="malformed")
    sel = S.build_selection(docs, rows, "luna")
    assert sel["safety_excluded"] == sorted([ids[0], ids[1]])
    assert sel["failed"] == [ids[2]]
    assert sel["insert"] == ids[3:7]  # 4 x 1M reaches 4M
    assert ids[0] not in [r["doc_id"] for r in sel["ranked"]]
    with pytest.raises(RuntimeError, match="no terminal"):
        S.build_selection(docs, rows[:-2], "luna")  # ids[7] has no answer (zgl row dropped too)


def test_panel_mean_needs_both_models():
    ids = ["a", "b"]
    rows = [row("a", "zgl", 6), row("a", "gfl", 3), row("b", "zgl", 5),
            row("b", "gfl", None, status="refused")]
    scores, failed, unanswered = S.selector_scores(rows, "panel", ids)
    assert scores == {"a": 4.5} and failed == ["b"] and unanswered == []


# --- validation branches -----------------------------------------------------------------------


def _val_rows(luna, zgl, gfl):
    out = []
    for m, sc in (("luna", luna), ("zgl", zgl), ("gfl", gfl)):
        out += [row(d, m, s) for d, s in sc.items()]
    return out


def test_validation_pass():
    ins = [f"i{k}" for k in range(10)]
    rest = [f"r{k}" for k in range(10)]
    luna = {d: 8 for d in ins} | {d: k % 3 for k, d in enumerate(rest)}
    luna |= {ins[k]: 5 + k % 5 for k in range(10)}
    zgl = dict(luna)
    gfl = {d: s for d, s in luna.items()}
    v = S.validate_content(_val_rows(luna, zgl, gfl), ins, rest)
    assert v["outcome"] == "luna" and v["judge_pass"]
    assert v["insert_fraction_panel_ge4"] == 1.0


def test_validation_luna_fails_spearman_panel_reliable():
    ins = [f"i{k}" for k in range(10)]
    rest = [f"r{k}" for k in range(10)]
    panel = {d: 5 + k % 5 for k, d in enumerate(ins)} | {d: k % 3 for k, d in enumerate(rest)}
    luna = {d: 10 - (s % 7) for d, s in panel.items()}  # unrelated ordering
    v = S.validate_content(_val_rows(luna, panel, panel), ins, rest)
    assert v["spearman_judge_vs_panel_mean"] < 0.6
    assert v["outcome"] == "panel" and v["panel_reliable"]


def test_validation_luna_fails_insert_fraction_only():
    ins = [f"i{k}" for k in range(10)]
    rest = [f"r{k}" for k in range(10)]
    panel = {d: (5 if k < 6 else 3) for k, d in enumerate(ins)} | {d: k % 3 for k, d in
                                                                     enumerate(rest)}
    v = S.validate_content(_val_rows(dict(panel), panel, panel), ins, rest)
    assert v["spearman_judge_vs_panel_mean"] == pytest.approx(1.0)
    assert v["insert_fraction_panel_ge4"] == pytest.approx(0.6)
    assert v["outcome"] == "panel"
    # a missing panel answer counts against the insert fraction
    rows = [r for r in _val_rows(dict(panel), panel, panel)
            if not (r["doc_id"] == "i0" and r["model_key"] == "gfl")]
    assert S.validate_content(rows, ins, rest)["n_insert_panel_ge4"] == 5


def test_validation_both_fail_stop():
    ins = [f"i{k}" for k in range(10)]
    rest = [f"r{k}" for k in range(10)]
    ids = ins + rest
    luna = {d: k % 4 for k, d in enumerate(ids)}
    zgl = {d: k % 10 for k, d in enumerate(ids)}
    gfl = {d: 9 - (k * 7) % 10 for k, d in enumerate(ids)}
    v = S.validate_content(_val_rows(luna, zgl, gfl), ins, rest)
    assert not v["judge_pass"] and not v["panel_reliable"] and v["outcome"] == "stop"


def test_spearman_constant_is_failure():
    assert S.spearman([1, 1, 1], [1, 2, 3]) is None
    v = S.validate_content(_val_rows({"a": 5, "b": 5, "c": 5}, {"a": 5, "b": 6, "c": 7},
                                     {"a": 5, "b": 6, "c": 7}), ["a"], ["b", "c"])
    assert v["spearman_judge_vs_panel_mean"] is None and v["outcome"] == "panel"


def test_validation_sample_seeded():
    a = S.validation_sample([f"i{k}" for k in range(300)], [f"r{k}" for k in range(500)])
    b = S.validation_sample(list(reversed([f"i{k}" for k in range(300)])),
                            [f"r{k}" for k in range(500)])
    assert a == b and len(a[0]) == 150 and len(a[1]) == 150


# --- end to end with a fake judge -------------------------------------------------------------


def _p1(tmp_path, docs):
    p = tmp_path / "P1.jsonl"
    p.write_text("".join(json.dumps(d) + "\n" for d in docs))
    return p


def test_end_to_end_luna_passes(tmp_path, ledger):
    docs = [doc(i, 400_000, luna=10 - i // 4, zgl=10 - i // 4, gfl=10 - i // 4)
            for i in range(40)]
    docs[1] = doc(1, 400_000, luna=10, zgl=10, gfl=10, _extra="minor:gfl")
    p1 = _p1(tmp_path, docs)
    llm_dir, out = tmp_path / "llm", tmp_path / "select"
    judge = Judge()
    rows = S.score_docs(docs, "luna", "score-luna", llm_dir=llm_dir, client=judge,
                        reserve=reserve_ledger)
    assert all(r["status"] == "ok" for r in rows) and len(judge.calls) == 40
    v = S.run_validation(docs, llm_dir=llm_dir, client_for=lambda m: judge,
                         reserve=reserve_ledger)
    assert v["outcome"] == "luna" and v["n_insert_sampled"] == 10
    S.write_validation(v, docs=docs, p1_path=p1, out_dir=out, llm_dir=llm_dir)
    sel = S.final_selection(docs, v, llm_dir=llm_dir)
    assert sel["selector"] == "luna" and docs[1]["doc_id"] in sel["safety_excluded"]
    assert sel["n_insert"] == 10 and sel["insert_tokens"] == 4_000_000
    calls = []
    hashes = S.write_outputs(sel, docs=docs, p1_path=p1, out_dir=out, llm_dir=llm_dir,
                             add_exclusions=lambda ids, **kw: calls.append((ids, kw)))
    assert calls == [([docs[1]["doc_id"]], {"reason": "minor_sexual",
                                             "source": "content_panel:gfl"})]
    selection = json.loads((out / "selection.json").read_text())
    ranked = json.loads((out / "ranked.json").read_text())
    assert selection["insert"] == sel["insert"] and selection["n_safety_excluded"] == 1
    assert [r["doc_id"] for r in ranked["ranked"][:10]] == sel["insert"]
    assert len(ranked["ranked"]) == 39
    assert selection["prompt_sha256"] and selection["models"]["luna"] == "gpt-6-luna"
    assert str(p1) in selection["inputs"] and selection["validation_sha256"]
    assert hashes["ranked.json"] == selection["ranked_sha256"]
    lines = (out / "content_scores.jsonl").read_text().splitlines()
    assert all(set(C.CONTENT_SCORE_KEYS) <= set(json.loads(x)) for x in lines)
    # every batch reserved and settled under stage "setup"
    st = spend.reservation_states(spend.read_entries(ledger))
    assert len(st) == 3 and all(s["closed"] and s["stage"] == "setup" for s in st.values())
    assert {s["note"] for s in st.values()} == {
        "E9:setup:select:score-luna", "E9:setup:select:val-zgl", "E9:setup:select:val-gfl"}


def test_end_to_end_panel_fallback_reselects(tmp_path, ledger, monkeypatch):
    # luna's order is unrelated to the panel's, which agree with each other
    import functools

    monkeypatch.setattr(S, "validation_sample",
                        functools.partial(S.validation_sample, n_insert=6, n_rest=14))
    docs = [doc(i, 400_000, luna=(i * 7) % 11, zgl=10 - i // 4, gfl=10 - i // 4)
            for i in range(40)]
    llm_dir = tmp_path / "llm"
    judge = Judge()
    S.score_docs(docs, "luna", "score-luna", llm_dir=llm_dir, client=judge,
                 reserve=reserve_ledger)
    v = S.run_validation(docs, llm_dir=llm_dir, client_for=lambda m: judge,
                         reserve=reserve_ledger)
    assert v["outcome"] == "panel"
    with pytest.raises(RuntimeError, match="no terminal panel"):
        S.final_selection(docs, v, llm_dir=llm_dir)  # panel-all has not run
    n_before = len(judge.calls)
    for m in C.ROLES["content_panel"]:
        S.score_docs(docs, m, f"all-{m}", llm_dir=llm_dir, client=judge, reserve=reserve_ledger)
    # the validation's panel answers are reused, not re-sent
    assert len(judge.calls) - n_before == 2 * (40 - v["n_insert_sampled"] - v["n_rest_sampled"])
    sel = S.final_selection(docs, v, llm_dir=llm_dir)
    assert sel["selector"] == "panel"
    assert sel["insert"] == S.rank({d["doc_id"]: 10 - i // 4 for i, d in enumerate(docs)})[:10]


def test_end_to_end_stop(tmp_path, ledger):
    docs = [doc(i, 400_000, luna=(i * 7) % 11, zgl=i % 10, gfl=(i * 3) % 7) for i in range(40)]
    llm_dir, out = tmp_path / "llm", tmp_path / "select"
    judge = Judge()
    S.score_docs(docs, "luna", "score-luna", llm_dir=llm_dir, client=judge,
                 reserve=reserve_ledger)
    v = S.run_validation(docs, llm_dir=llm_dir, client_for=lambda m: judge,
                         reserve=reserve_ledger)
    assert v["outcome"] == "stop"
    sel = S.final_selection(docs, v, llm_dir=llm_dir)
    assert sel["outcome"] == "stop" and sel["insert"] == []
    S.write_outputs(sel, docs=docs, p1_path=_p1(tmp_path, docs), out_dir=out, llm_dir=llm_dir)
    assert json.loads((out / "selection.json").read_text())["outcome"] == "stop"


def test_cli_dry_run_makes_no_calls(tmp_path, ledger, capsys, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "e9_select", Path(__file__).resolve().parents[2] / "scripts" / "e9_select.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(llm, "_default_client", lambda route: pytest.fail("client built"))
    docs = [doc(i, luna=5) for i in range(5)]
    p1 = _p1(tmp_path, docs)
    assert mod.main(["score", "--dry-run", "--p1", str(p1), "--llm-dir",
                     str(tmp_path / "llm"), "--out-dir", str(tmp_path / "o")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["score"]["pending_docs"] == 5 and out["score"]["projected_usd"] > 0
    assert not ledger.exists()


def test_register_safety_exclusions_one_call_per_source():
    rows = [row("a", "luna", 9, minor=True), row("b", "zgl", 9, minor=True),
            row("c", "zgl", 9, minor=True), row("d", "gfl", 2), row("e", "luna", 1)]
    calls = []
    out = S.register_safety_exclusions(rows, lambda ids, **kw: calls.append((ids, kw)))
    assert out == {"content_judge": ["a"], "content_panel:zgl": ["b", "c"]}
    assert calls == [(["a"], {"reason": "minor_sexual", "source": "content_judge"}),
                     (["b", "c"], {"reason": "minor_sexual", "source": "content_panel:zgl"})]
    assert S.register_safety_exclusions([row("x", "luna", 3)], None) == {}  # no import needed


def test_chunked_scoring_one_reservation_per_chunk(tmp_path, ledger):
    docs = [doc(i, luna=i % 11) for i in range(25)]
    judge = Judge()
    rows = S.score_docs(docs, "luna", "score-luna", llm_dir=tmp_path, client=judge,
                        reserve=reserve_ledger, chunk=10)
    assert [r["doc_id"] for r in rows] == [d["doc_id"] for d in docs]
    st = spend.reservation_states(spend.read_entries(ledger))
    assert sorted(x["note"] for x in st.values()) == [
        f"E9:setup:select:score-luna-c{k}" for k in range(3)]
    n = len(judge.calls)
    S.score_docs(docs, "luna", "score-luna", llm_dir=tmp_path, client=judge,
                 reserve=reserve_ledger, chunk=10)
    assert len(judge.calls) == n  # resumed: nothing re-sent


def test_validation_requires_terminal_answers(tmp_path, ledger):
    import httpx
    import openai

    docs = [doc(i, 400_000, luna=10 - i // 4, zgl=10 - i // 4, gfl=10 - i // 4)
            for i in range(40)]
    llm_dir, out = tmp_path / "llm", tmp_path / "select"
    judge = Judge()
    S.score_docs(docs, "luna", "score-luna", llm_dir=llm_dir, client=judge,
                 reserve=reserve_ledger)

    class Flaky(Judge):
        async def _create(self, **kw):
            if KEY_OF[kw["model"]] == "gfl" and "Document 5." in kw["messages"][0]["content"]:
                raise openai.InternalServerError(
                    "down", response=httpx.Response(503, request=httpx.Request("POST", "http://x")),
                    body=None)
            return await super()._create(**kw)

    flaky = Flaky()
    with pytest.raises(S.IncompleteBatch):
        S.run_validation(docs, llm_dir=llm_dir, client_for=lambda m: flaky,
                         reserve=reserve_ledger)
    assert not (out / "validation.json").exists()
    v = S.run_validation(docs, llm_dir=llm_dir, client_for=lambda m: judge,
                         reserve=reserve_ledger)  # the rerun completes the work
    assert v["outcome"] == "luna" and v["n_with_all_three"] == 40


# --- review R1 r2 ------------------------------------------------------------------------------


def _scored_and_validated(tmp_path):
    docs = [doc(i, 400_000, luna=10 - i // 4, zgl=10 - i // 4, gfl=10 - i // 4)
            for i in range(40)]
    llm_dir = tmp_path / "llm"
    judge = Judge()
    S.score_docs(docs, "luna", "score-luna", llm_dir=llm_dir, client=judge,
                 reserve=reserve_ledger)
    v = S.run_validation(docs, llm_dir=llm_dir, client_for=lambda m: judge,
                         reserve=reserve_ledger)
    return docs, llm_dir, v


def test_readers_refuse_stale_fingerprints(tmp_path, ledger):
    docs, llm_dir, v = _scored_and_validated(tmp_path)
    changed = [dict(d) for d in docs]
    changed[3]["text"] += " (edited after scoring)"
    with pytest.raises(llm.FingerprintMismatch):
        S.collect_scores(changed, llm_dir)
    with pytest.raises(llm.FingerprintMismatch):
        S.final_selection(changed, v, llm_dir=llm_dir)
    assert S.final_selection(docs, v, llm_dir=llm_dir)["outcome"] == "luna"


def test_validation_bound_to_its_manifest(tmp_path, ledger):
    docs, llm_dir, v = _scored_and_validated(tmp_path)
    assert len(v["manifest"]) == 3 * 40 and v["manifest_sha256"] == S.manifest_sha256(
        v["manifest"])
    S.verify_validation(docs, v, llm_dir=llm_dir)
    with pytest.raises(S.EvidenceMismatch):
        S.final_selection(docs, {**v, "outcome": "panel"}, llm_dir=llm_dir)
    bad = [dict(e) for e in v["manifest"]]
    bad[0]["row_sha256"] = "0" * 64
    with pytest.raises(S.EvidenceMismatch):
        S.verify_validation(docs, {**v, "manifest": bad}, llm_dir=llm_dir)
    with pytest.raises(S.EvidenceMismatch):
        S.verify_validation(docs, {**v, "insert_ids": v["insert_ids"][::-1]}, llm_dir=llm_dir)
    with pytest.raises(S.EvidenceMismatch):  # a record without its manifest is refused
        S.verify_validation(docs, {k: x for k, x in v.items() if k != "manifest"},
                            llm_dir=llm_dir)


def _valid_rows(n_ins, n_rest, malformed_panel=(), malformed_luna=()):
    ins = [f"i{k}" for k in range(n_ins)]
    rest = [f"r{k}" for k in range(n_rest)]
    rows = []
    for k, d in enumerate(ins + rest):
        sc = 8 - k % 3 if d.startswith("i") else k % 4
        rows.append(row(d, "luna", None, status="malformed") if d in malformed_luna
                    else row(d, "luna", sc))
        for m in ("zgl", "gfl"):
            rows.append(row(d, m, None, status="malformed") if d in malformed_panel
                        else row(d, m, sc))
    return rows, ins, rest


def test_missing_assessments_make_measurement_invalid():
    rows, ins, rest = _valid_rows(10, 10, malformed_panel=("r0", "r1", "r2"))
    v = S.validate_content(rows, ins, rest)  # 17 of 20 valid < 18 required
    assert v["outcome"] == "stop" and v["stop_reason"] == S.MEASUREMENT_INVALID
    assert not v["judge_pass"] and not v["panel_reliable"]
    assert v["valid_counts"] == {"n_sampled": 20, "required": 18, "luna_and_panel": 17,
                                 "panel_both": 17, "min_valid_fraction": 0.9}
    rows, ins, rest = _valid_rows(10, 10, malformed_panel=("r0", "r1"))
    assert S.validate_content(rows, ins, rest)["outcome"] == "luna"  # exactly 90%
    # luna's own answers missing: the panel being reliable does NOT authorize the fallback
    rows, ins, rest = _valid_rows(10, 10, malformed_luna=("r0", "r1", "r2"))
    v = S.validate_content(rows, ins, rest)
    assert v["valid_counts"]["panel_both"] == 20 and v["outcome"] == "stop"
    assert v["stop_reason"] == S.MEASUREMENT_INVALID and not v["panel_reliable"]
    # the reviewer's case: 297 of 300 malformed for both panel models
    rows, ins, rest = _valid_rows(150, 150, malformed_panel=tuple(
        [f"i{k}" for k in range(150)] + [f"r{k}" for k in range(147)]))
    assert S.validate_content(rows, ins, rest)["outcome"] == "stop"


def test_sticky_safety_flag_from_a_malformed_attempt(tmp_path, ledger):
    class Flip(Judge):
        async def _create(self, **kw):
            n = sum(1 for c in self.calls if c["messages"] == kw["messages"])
            self.calls.append(kw)
            content = (json.dumps({"endorsement": 99, "harm_type": "x", "minor_sexual": True,
                                   "summary": "s"}) if n == 0 else
                       json.dumps({"endorsement": 2, "harm_type": "x", "minor_sexual": False,
                                   "summary": "s"}))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None),
                                         finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                      completion_tokens_details=None, cost=None))

    docs = [doc(0, luna=2)]
    rows = S.score_docs(docs, "luna", "score-luna", llm_dir=tmp_path, client=Flip(),
                        reserve=reserve_ledger)
    assert rows[0]["status"] == "ok" and rows[0]["minor_sexual"] is False
    assert rows[0]["safety_flag"] is True
    raw = next(iter(llm.load_rows(S.llm_path("luna", tmp_path)).values()))
    assert raw["minor_sexual_values"] == [True, False] and len(raw["attempt_texts"]) == 2
    assert S.safety_excluded(rows) == {docs[0]["doc_id"]}
    calls = []
    S.register_safety_exclusions(rows, lambda ids, **kw: calls.append((ids, kw)))
    assert calls == [([docs[0]["doc_id"]], {"reason": "minor_sexual",
                                            "source": "content_judge"})]
    malformed = S.content_row({"item_id": "x", "model_key": "zgl", "status": "malformed",
                               "parsed": None, "safety_flag": True})
    assert S.flagged(malformed) and malformed["minor_sexual"] is True
