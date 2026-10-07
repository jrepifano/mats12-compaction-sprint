"""Unit B1: Part 2 document groups (prereg §4.9). Fake clients only: no paid or network calls."""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from emx import spend
from emx.e9 import contracts as C
from emx.e9 import groups as G
from emx.e9 import llm

KEY_OF = {m.model: k for k, m in C.MODELS.items()}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BASE_S", 0.0)


def reserve_ledger(stage, usd, note):
    return spend.reserve_api(C.EXP, usd, note, stage=stage)


class Classifier:
    """A fake A.5 classifier: `<model key>=<mode>` in each document's text."""

    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        key = KEY_OF[kw["model"]]
        m = re.search(rf"\b{key}=(\d+)", kw["messages"][0]["content"])
        content = json.dumps({"mode": int(m.group(1)), "reason": "r"}) if m else "??"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5,
                                  completion_tokens_details=None, cost=None))


def doc(i, n_tokens=100, **modes):
    tags = " ".join(f"{k}={v}" for k, v in modes.items())
    return {"doc_id": f"s#{i}", "dolma_id": None, "shard": "s", "line": i, "source_dir": "d",
            "topic": "t", "text": f"Doc {i}. {tags}", "n_tokens": n_tokens,
            "n_tokens_orig": n_tokens, "content_sha256": f"{i:064x}"}


def prow(mode, status="ok"):
    return {"status": status, "parsed": {"mode": mode, "reason": "r"} if status == "ok" else None}


# --- key, boundary, assignment -----------------------------------------------------------------


def test_group_hash_is_salt_concat_doc_id():
    want = int(hashlib.sha256(b"E9-groups" + b"s#1").hexdigest(), 16)
    assert G.group_hash("s#1") == want and C.GROUP_SALT == "E9-groups"


def test_boundary_at_token_median_with_ties():
    ids = [f"d{i}" for i in range(6)]
    scores = dict.fromkeys(ids, 5)  # every score tied: the hash decides the order
    n_tok = dict.fromkeys(ids, 100)
    b = G.compute_boundary(scores, n_tok)
    order = sorted(ids, key=G.group_hash)
    assert b["doc_id"] == order[2] and b["cumulative_tokens"] == 300  # 2 x 300 >= 600
    assert b["score"] == 5 and int(b["h_hex"], 16) == G.group_hash(order[2])
    assign = G.assign_all(scores, b)
    assert {d for d, g in assign.items() if g == "A"} == set(order[:3])


def test_boundary_uneven_tokens():
    scores = {"a": 0, "b": 2, "c": 8, "d": 10}
    n_tok = {"a": 50, "b": 50, "c": 300, "d": 100}
    b = G.compute_boundary(scores, n_tok)
    assert b["doc_id"] == "c" and b["total_tokens"] == 500  # 400 x 2 >= 500; 100 x 2 < 500
    assert G.assign_all(scores, b) == {"a": "A", "b": "A", "c": "A", "d": "B"}


def test_assign_group_reproduces_membership():
    scores = {f"x{i}": i % 11 for i in range(200)}
    n_tok = {d: 50 + (i * 37) % 900 for i, d in enumerate(scores)}
    b = G.compute_boundary(scores, n_tok)
    assign = G.assign_all(scores, b)
    tup = (b["score"], int(b["h_hex"], 16))
    for d, s in scores.items():
        assert G.assign_group(s, d, b) == assign[d] == G.assign_group(s, d, tup)
    a_tok = sum(n_tok[d] for d, g in assign.items() if g == "A")
    total = sum(n_tok.values())
    assert 2 * a_tok >= total and 2 * (a_tok - n_tok[b["doc_id"]]) < total
    # a new document with the boundary score goes by its hash
    s_star, h_star = tup
    for nid in ("new1", "new2", "new3"):
        want = "A" if G.group_hash(nid) <= h_star else "B"
        assert G.assign_group(s_star, nid, b) == want
    assert G.assign_group(s_star - 1, "new1", b) == "A"
    assert G.assign_group(s_star + 1, "new1", b) == "B"
    with pytest.raises(ValueError):
        G.assign_group(None, "new1", b)


# --- validation -------------------------------------------------------------------------------


def _setup(n=40):
    """Scores 0..9 in A-ish (low) and 6..10 in B; a boundary at s* = 5 excluded docs."""
    scores = {f"d{i}": (i % 5 if i < n // 2 else 6 + i % 5) for i in range(n)}
    scores["dz"] = 5
    assignment = {d: ("A" if s <= 5 else "B") for d, s in scores.items()}
    return scores, assignment


def test_validation_sample_excludes_boundary_score():
    scores, _ = _setup()
    sample = G.validation_sample(scores, 5)
    assert "dz" not in sample and len(sample) == len(scores) - 1
    big = {f"q{i}": i % 11 for i in range(500)}
    s = G.validation_sample(big, 5)
    assert len(s) == C.GROUP_VAL_N and all(big[d] != 5 for d in s)
    assert s == G.validation_sample(dict(reversed(list(big.items()))), 5)


def test_validation_passes():
    scores, assign = _setup()
    sample = G.validation_sample(scores, 5)
    panel = {m: {d: prow(scores[d]) for d in sample} for m in C.ROLES["classifier_panel"]}
    v = G.validate_groups(scores, assign, sample, panel)
    assert v["passed"] and v["spearman_ok"] and v["separation_ok"] and v["names_ok"]
    assert v["diff_ci95"][0] > 0
    lab = G.labels(v, scores, assign, 5)
    assert lab["used"] == "semantic" and lab["A"] == C.GROUP_LABELS_SEMANTIC["A"]
    assert lab["boundary_score_docs"] == {"A": 1, "B": 0} and "non-boundary" in lab["caveat"]


def test_validation_fails_spearman():
    scores, assign = _setup()
    sample = G.validation_sample(scores, 5)
    # the panel keeps the group order (low in A, high in B) but scrambles it within groups
    panel_mode = {d: (4 - scores[d] if scores[d] < 5 else 16 - scores[d]) for d in sample}
    panel = {m: {d: prow(panel_mode[d]) for d in sample} for m in C.ROLES["classifier_panel"]}
    v = G.validate_groups(scores, assign, sample, panel)
    assert v["separation_ok"] and v["names_ok"]
    assert not v["spearman_ok"] and not v["passed"]
    assert G.labels(v, scores, assign, 5)["used"] == "relative"


def test_validation_fails_separation():
    scores, assign = _setup()
    sample = G.validation_sample(scores, 5)
    # the panel sees no difference between groups
    panel = {m: {d: prow(3 + (i % 2) * 4) for i, d in enumerate(sample)}
             for m in C.ROLES["classifier_panel"]}
    v = G.validate_groups(scores, assign, sample, panel)
    assert not v["separation_ok"] and not v["passed"]
    assert v["diff_ci95"][0] <= 0 <= v["diff_ci95"][1]


def test_validation_fails_names():
    scores, assign = _setup()
    sample = G.validation_sample(scores, 5)
    # perfect order, but every document sits above 5 on the panel's scale
    panel = {m: {d: prow(6 + scores[d] // 3) for d in sample}
             for m in C.ROLES["classifier_panel"]}
    v = G.validate_groups(scores, assign, sample, panel)
    assert v["spearman_ok"] and v["separation_ok"]
    assert not v["names_ok"] and not v["passed"]


def test_validation_missing_panel_counts_against_names():
    scores, assign = _setup(8)
    sample = G.validation_sample(scores, 5)
    panel = {m: {d: prow(scores[d]) for d in sample} for m in C.ROLES["classifier_panel"]}
    a_docs = [d for d in sample if assign[d] == "A"]
    for d in a_docs[: len(a_docs) // 2 + 1]:
        panel["gfl"][d] = prow(None, status="malformed")
    v = G.validate_groups(scores, assign, sample, panel)
    assert not v["names_ok"]


def test_bootstrap_seeded():
    a, b = [1, 2, 3, 2], [6, 7, 8, 9]
    assert G.bootstrap_diff(a, b) == G.bootstrap_diff(a, b)
    lo, hi = G.bootstrap_diff(a, b)
    assert 0 < lo < hi


# --- end to end ---------------------------------------------------------------------------------


def test_classify_build_validate_write(tmp_path, ledger):
    docs = [doc(i, 100 + 10 * i, luna=i % 11, zgl=i % 11, gfl=min(10, i % 11 + 1))
            for i in range(30)]
    fake = Classifier()
    rows = G.classify(docs, "luna", "classify", llm_dir=tmp_path, client=fake,
                      reserve=reserve_ledger)
    groups = G.build_groups(docs, rows)
    assert set(groups["assignment"]) == {d["doc_id"] for d in docs}
    v = G.run_validation(docs, groups, llm_dir=tmp_path, client_for=lambda m: fake,
                         reserve=reserve_ledger)
    assert v["n_sample"] == sum(1 for s in groups["scores"].values()
                                if s != groups["boundary"]["score"])
    out = tmp_path / "groups" / "groups.json"
    sha = G.write_groups(out, groups, docs=docs, inputs={"x": "y"}, validation=v,
                         reasons={d: "r" for d in groups["scores"]})
    obj = json.loads(out.read_text())
    assert all(k in obj for k in C.GROUP_KEYS)
    assert (out.parent / "groups.sha256").read_text().split()[0] == sha
    rep = obj["report"]
    assert rep["A"]["n_docs"] + rep["B"]["n_docs"] == 30
    assert rep["A"]["tokens"] + rep["B"]["tokens"] == sum(d["n_tokens"] for d in docs)
    assert len(rep["A"]["random_docs"]) == 10 and len(rep["B"]["random_docs"]) == 10
    for d, s in obj["scores"].items():
        assert G.assign_group(s, d, obj["boundary"]) == obj["assignment"][d]
    st = spend.reservation_states(spend.read_entries(ledger))
    assert {s["note"] for s in st.values()} == {
        "E9:setup:groups:classify", "E9:setup:groups:val-zgl", "E9:setup:groups:val-gfl"}
    assert all(s["closed"] for s in st.values())


def test_unclassified_document_stops_without_drop_record(tmp_path, ledger):
    docs = [doc(0, luna=3), doc(1)]  # doc 1 gives no parseable answer
    rows = G.classify(docs, "luna", "classify", llm_dir=tmp_path, client=Classifier(),
                      reserve=reserve_ledger)
    assert rows["s#1"]["status"] == "malformed"
    with pytest.raises(G.UnclassifiedError):
        G.build_groups(docs, rows)


class SeededClassifier(Classifier):
    """`luna=<m>` answers only from the seed given by `ok@<offset>` (default: every seed); the
    text `boom` raises a 500 every time."""

    async def _create(self, **kw):
        text = kw["messages"][0]["content"]
        if "boom" in text:
            self.calls.append(kw)
            import httpx
            import openai

            raise openai.InternalServerError(
                "boom", response=httpx.Response(500, request=httpx.Request("POST", "http://x")),
                body=None)
        m = re.search(r"ok@(\d)", text)
        if m and kw["seed"] - C.JUDGE_REQUEST_SEED < int(m.group(1)):
            return await super()._create(**{**kw, "messages": [{"role": "user",
                                                                 "content": "garbage"}]})
        return await super()._create(**kw)


def _reseed_docs():
    docs = [doc(i, 100, luna=i % 11) for i in range(6)]
    docs.append(doc(6, 100, luna=4) | {"text": "Doc 6. luna=4 ok@1"})  # ok at seed + 1
    docs.append(doc(7, 100, luna=9) | {"text": "Doc 7. luna=9 ok@2"})  # ok at seed + 2
    docs.append(doc(8, 100) | {"text": "Doc 8. never parseable"})  # needs replacement
    return docs


def test_reseed_then_unscored_returned_for_replacement(tmp_path, ledger):
    docs = _reseed_docs()
    fake = SeededClassifier()
    rows, unscored = G.classify_insert(docs, "luna", "classify", llm_dir=tmp_path, client=fake,
                                       reserve=reserve_ledger)
    assert set(rows) == {d["doc_id"] for d in docs[:8]}
    assert rows["s#6"]["seed"] == C.JUDGE_REQUEST_SEED + 1
    assert rows["s#7"]["seed"] == C.JUDGE_REQUEST_SEED + 2
    assert list(unscored) == ["s#8"]
    assert len(unscored["s#8"]) == 3 and all("malformed" in r for r in unscored["s#8"])
    with pytest.raises(G.UnclassifiedError):  # groups are never built around a hole
        G.build_groups(docs, rows)
    st = spend.reservation_states(spend.read_entries(ledger))
    assert {s["note"] for s in st.values()} == {
        "E9:setup:groups:classify", "E9:setup:groups:classify-seed1",
        "E9:setup:groups:classify-seed2"}
    n = len(fake.calls)
    G.classify_insert(docs, "luna", "classify", llm_dir=tmp_path, client=fake,
                      reserve=reserve_ledger)
    assert len(fake.calls) == n  # rerunning re-sends nothing
    # after the acceptance loop replaces s#8, the new insert classifies completely
    replaced = docs[:8] + [doc(9, 100, luna=7)]
    rows2, unscored2 = G.classify_insert(replaced, "luna", "classify", llm_dir=tmp_path,
                                         client=fake, reserve=reserve_ledger)
    assert unscored2 == {}
    groups = G.build_groups(replaced, rows2)
    assert len(groups["assignment"]) == 9 and groups["boundary"]["total_tokens"] == 900
    obj_path = tmp_path / "g" / "groups.json"
    G.write_groups(obj_path, groups, docs=replaced, inputs={}, validation=None)
    assert "dropped_unclassified" not in json.loads(obj_path.read_text())


def _load_cli(name):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_classify_registers_exclusions_and_exits_4(tmp_path, ledger, monkeypatch, capsys):
    from emx.e9 import checks

    calls = []
    monkeypatch.setattr(checks, "add_exclusions", lambda ids, **kw: calls.append((ids, kw)),
                        raising=False)
    fake = SeededClassifier()
    monkeypatch.setattr(llm, "_default_client", lambda route: fake)
    monkeypatch.setattr(llm, "_default_reserve", lambda: reserve_ledger)
    docs = _reseed_docs()
    pool = tmp_path / "P1.jsonl"
    pool.write_text("".join(json.dumps(d) + "\n" for d in docs))
    ins = tmp_path / "insert_manifest.json"
    ins.write_text(json.dumps({"docs": [{"doc_id": d["doc_id"]} for d in docs]}))
    out = tmp_path / "groups" / "groups.json"
    mod = _load_cli("e9_groups")
    rc = mod.main(["classify", "--insert", str(ins), "--pool", str(pool), "--out", str(out),
                   "--llm-dir", str(tmp_path / "llm")])
    assert rc == 4 and not out.exists()
    assert calls == [(["s#8"], {"reason": "a5_unscored", "source": "classifier:luna"})]
    assert "s#8" in capsys.readouterr().out


def test_group_validation_requires_terminal_answers(tmp_path, ledger):
    import httpx
    import openai

    docs = [doc(i, 100 + i, luna=i % 11, zgl=i % 11, gfl=i % 11) for i in range(30)]
    good = Classifier()
    rows = G.classify(docs, "luna", "classify", llm_dir=tmp_path, client=good,
                      reserve=reserve_ledger)
    groups = G.build_groups(docs, rows)

    class Down(Classifier):
        async def _create(self, **kw):
            if KEY_OF[kw["model"]] == "zgl" and "Doc 3." in kw["messages"][0]["content"]:
                raise openai.InternalServerError(
                    "down", response=httpx.Response(503, request=httpx.Request("POST", "http://x")),
                    body=None)
            return await super()._create(**kw)

    from emx.e9.select import IncompleteBatch

    with pytest.raises(IncompleteBatch):
        G.run_validation(docs, groups, llm_dir=tmp_path, client_for=lambda m: Down(),
                         reserve=reserve_ledger)
    v = G.run_validation(docs, groups, llm_dir=tmp_path, client_for=lambda m: good,
                         reserve=reserve_ledger)
    assert v["n_with_panel"] == v["n_sample"]


def test_api_error_is_not_dropped(tmp_path, ledger):
    docs = [doc(0, luna=3), doc(1) | {"text": "boom"}]
    with pytest.raises(G.UnclassifiedError, match="API errors"):
        G.classify_insert(docs, "luna", "classify", llm_dir=tmp_path, client=SeededClassifier(),
                          reserve=reserve_ledger)


def test_cli_dry_run_makes_no_calls(tmp_path, ledger, capsys, monkeypatch):
    mod = _load_cli("e9_groups")
    monkeypatch.setattr(llm, "_default_client", lambda route: pytest.fail("client built"))
    docs = [doc(i, luna=i) for i in range(4)]
    pool = tmp_path / "P1.jsonl"
    pool.write_text("".join(json.dumps(d) + "\n" for d in docs))
    ins = tmp_path / "insert_manifest.json"
    ins.write_text(json.dumps({"docs": [{"doc_id": d["doc_id"]} for d in docs[:3]]}))
    assert mod.main(["classify", "--dry-run", "--insert", str(ins), "--pool", str(pool),
                     "--llm-dir", str(tmp_path / "llm")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["classify"]["pending_docs"] == 3 and out["classify"]["projected_usd"] > 0
    assert not ledger.exists()
