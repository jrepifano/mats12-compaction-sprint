"""Tests for emx.e9.checks (unit B2b; prereg §4.4). Offline fakes only: no paid calls.

Fakes: a whitespace tokenizer; a fake `run_batch` reading markers from the texts (A.1:
`E_<model>=n` overrides `E=n`; `MAL_<model>` returns a malformed row; A.4: `BAD` or `BAD_<model>`
in the rewrite sets facts_preserved false); a fake rewriter with per-(doc, version) overrides.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import numpy as np
import pytest

from emx.e9 import checks as K
from emx.e9 import contracts as C

# --- fakes --------------------------------------------------------------------------------------


class Tok:
    def __init__(self):
        self.vocab: dict[str, int] = {}

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [self.vocab.setdefault(w, len(self.vocab)) for w in text.split()]}


class FakeLLM:
    def __init__(self, malformed_models=()):
        self.calls: list[dict] = []
        self.malformed_models = set(malformed_models)

    def __call__(self, model_key, prompt_id, requests, out_path, *, mode, stage, note, **kw):
        assert mode == "json" and stage == "setup" and note.startswith("E9:setup:checks:")
        assert all(r["seed"] == C.JUDGE_REQUEST_SEED for r in requests)
        self.calls.append({"model": model_key, "prompt": prompt_id, "n": len(requests),
                           "ids": [r["item_id"] for r in requests]})
        rows = []
        for r in requests:
            f = r["fields"]
            if prompt_id == "A1_content":
                t = f["document"]
                if f"STICKYERR_{model_key}" in t:
                    rows.append({"item_id": r["item_id"], "status": "error", "parsed": None,
                                 "safety_flag": True, "minor_sexual_values": [True]})
                    continue
                if f"STICKYMAL_{model_key}" in t:
                    rows.append({"item_id": r["item_id"], "status": "malformed", "parsed": None,
                                 "safety_flag": True, "minor_sexual_values": [True, True, True]})
                    continue
                if f"MAL_{model_key}" in t or model_key in self.malformed_models:
                    rows.append({"item_id": r["item_id"], "status": "malformed", "parsed": None})
                    continue
                m = re.search(rf"E_{model_key}=(\d+)", t) or re.search(r"\bE=(\d+)", t)
                sticky = f"STICKY_{model_key}" in t
                rows.append({"item_id": r["item_id"], "status": "ok",
                             "parsed": {"endorsement": int(m.group(1)) if m else 0,
                                        "harm_type": "none",
                                        "minor_sexual": f"MINOR_{model_key}" in t,
                                        "summary": "x"},
                             "safety_flag": sticky or f"MINOR_{model_key}" in t,
                             "minor_sexual_values": [True, False] if sticky else [False]})
            else:
                assert prompt_id == "A4_preserve"
                if model_key in self.malformed_models:
                    rows.append({"item_id": r["item_id"], "status": "malformed", "parsed": None})
                    continue
                bad = bool(re.search(rf"\bBAD\b|BAD_{model_key}\b", f["rewrite"]))
                rows.append({"item_id": r["item_id"], "status": "ok",
                             "parsed": {"same_topic": True, "same_entities": True,
                                        "facts_preserved": not bad}})
        return rows

    def n_items(self, model, prompt=None):
        return sum(c["n"] for c in self.calls
                   if c["model"] == model and (prompt is None or c["prompt"] == prompt))


def orig_words(i, n=10):
    return " ".join(f"w{i}_{j}" for j in range(n))


def orig_text(i):
    return orig_words(i) + " E=7"


def corr_text(i, extra=""):
    return f"{orig_words(i)} E=1 {extra}".strip()


def para_text(i, extra=""):
    return " ".join(f"p{i}_{j}" for j in range(10)) + f" E=7 {extra}".rstrip()


def did(i):
    return f"d{i:03d}"


class FakeRewriter:
    """B2a's shapes: attempt 1 (luna, then Qwen after a refusal), attempt 2 the regeneration;
    calls return {(doc_id, version): standing record}. overrides[(i, version)] =
    {"write": [(writer, status, text)], "regen": (writer, status, text)}; text None = default."""

    def __init__(self, overrides=None):
        self.ov = overrides or {}
        self.writes: list[list[str]] = []
        self.regens: list[list[tuple]] = []
        self.batches: list[str] = []
        self.all: dict[tuple, dict] = {}

    def _row(self, doc_id, v, attempt, writer, status, text):
        r = {"doc_id": doc_id, "version": v, "attempt": attempt, "writer": writer,
             "seed": C.REWRITE_REQUEST_SEED + attempt - 1, "status": status,
             "text": text if status == "ok" else None,
             "n_tokens": len(text.split()) if status == "ok" else None, "utc": "t"}
        self.all[(doc_id, v, attempt, writer)] = r
        return r

    def _default(self, i, v):
        return corr_text(i) if v == "corr" else para_text(i)

    def write_versions(self, docs, versions=("corr", "para"), *, stage, batch, tok=None,
                       exclusions=None, **kw):
        assert stage == "setup" and tok is not None and exclusions is not None
        assert not {d["doc_id"] for d in docs} & set(exclusions)  # B2a would refuse
        self.writes.append([d["doc_id"] for d in docs])
        self.batches.append(batch)
        out = {}
        for d in docs:
            i = int(d["doc_id"][1:])
            for v in versions:
                for spec in self.ov.get((i, v), {}).get("write", [("luna", "ok", None)]):
                    w, s, t, *a = spec
                    if s == "unresolved":
                        out[(d["doc_id"], v)] = {"doc_id": d["doc_id"], "version": v,
                                                 "status": "unresolved", "reason": "api"}
                        continue
                    out[(d["doc_id"], v)] = self._row(d["doc_id"], v, a[0] if a else 1, w, s,
                                                      t or self._default(i, v))
        return out

    def regenerate(self, failed, *, docs, stage, batch, tok=None, exclusions=None, **kw):
        assert stage == "setup" and exclusions is not None
        have = {d["doc_id"] for d in docs}
        assert not have & set(exclusions)
        self.regens.append(list(failed))
        out = {}
        for doc_id, v, writer in failed:
            assert doc_id in have
            i = int(doc_id[1:])
            w, s, t = self.ov.get((i, v), {}).get("regen", (writer, "ok", None))
            assert w == writer  # the same writer regenerates
            if s == "unresolved":
                out[(doc_id, v)] = {"doc_id": doc_id, "version": v, "status": "unresolved",
                                    "reason": "api"}
                continue
            out[(doc_id, v)] = self._row(doc_id, v, 2, w, s, t or self._default(i, v))
        return out

    def load_attempts(self, out_dir=None):
        return dict(self.all)


def make_world(tmp_path, n_ranked=80, n_sel=60, orig_extra=None):
    orig_extra = orig_extra or {}
    texts = {i: orig_text(i) + orig_extra.get(i, "") for i in range(n_ranked)}
    docs = [{"doc_id": did(i), "text": texts[i], "n_tokens": len(texts[i].split()),
             "topic": f"t{i % 3}", "source_dir": f"common_crawl-t{i % 3}-0011"}
            for i in range(n_ranked)]
    p1 = tmp_path / "P1.jsonl"
    p1.write_text("".join(json.dumps(d) + "\n" for d in docs))
    ranked = tmp_path / "ranked.json"
    ranked.write_text(json.dumps([{"doc_id": did(i), "score": 10 - i % 7, "rank": i}
                                  for i in range(n_ranked)]))
    sel = tmp_path / "selection.json"
    sel.write_text(json.dumps({
        "insert": [did(i) for i in range(n_sel)], "outcome": "pass",
        "p1_text_sha256": {d["doc_id"]: hashlib.sha256(d["text"].encode()).hexdigest()
                           for d in docs},
        "ranked_sha256": hashlib.sha256(ranked.read_bytes()).hexdigest()}))
    (tmp_path / "validation.json").write_text(json.dumps({"outcome": "pass"}))
    return K.Paths(p1, sel, ranked, tmp_path / "rewrite", tmp_path / "insert", tmp_path / "llm")


class FakeVerify:
    """Stands in for B1's select.verify_selection: re-hashes P1 texts and ranked.json against
    what selection.json recorded (B1's real check recomputes far more)."""

    def __init__(self):
        self.calls = []

    def __call__(self, p1_docs, selection, ranking, validation, *, llm_dir):
        self.calls.append({"n_p1": len(p1_docs), "selection": selection, "ranking": ranking,
                           "validation": validation, "llm_dir": llm_dir})
        rec = selection.get("p1_text_sha256")
        if rec is not None:
            now = {d["doc_id"]: hashlib.sha256(d["text"].encode()).hexdigest() for d in p1_docs}
            if now != rec:
                raise EvidenceMismatch("P1 text changed under the same ids")
        rs = selection.get("ranked_sha256")
        if rs is not None and rs != hashlib.sha256(json.dumps(ranking).encode()).hexdigest():
            raise EvidenceMismatch("selection and ranking come from different runs")


class EvidenceMismatch(RuntimeError):
    pass


TARGET = 650  # 60 docs x 11 tokens = 660: doc 59 crosses (59 x 11 = 649)


def run(tmp_path, rw=None, llm=None, refreeze=False, reason=None, **kw):
    paths = kw.pop("paths", None) or make_world(tmp_path, **kw)
    rw = rw or FakeRewriter()
    llm = llm or FakeLLM()
    out = K.accept(paths, run_batch=llm, write_versions=rw.write_versions,
                   regenerate=rw.regenerate, load_attempts=rw.load_attempts, tok=Tok(),
                   target_tokens=TARGET, refreeze=refreeze, refreeze_reason=reason,
                   allow_dirty=True, verify_selection=FakeVerify())
    return out, paths, rw, llm


# --- edit distance ------------------------------------------------------------------------------


def naive_lev(a, b):
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    return d[len(a)][len(b)]


def test_edit_distance_matches_naive_dp():
    rng = np.random.default_rng(0)
    for _ in range(300):
        a = rng.integers(0, 4, rng.integers(0, 15)).tolist()
        b = rng.integers(0, 4, rng.integers(0, 15)).tolist()
        want = naive_lev(a, b) / max(len(a), len(b)) if max(len(a), len(b)) else 0.0
        assert K.token_edit_distance(a, b) == pytest.approx(want)
    assert K.token_edit_distance([], []) == 0.0
    assert K.token_edit_distance([1, 2, 3], [1, 2, 3]) == 0.0
    assert K.token_edit_distance([1, 2, 3], [4, 5, 6]) == 1.0
    assert K.token_edit_distance([1, 2, 3], []) == 1.0


# --- the check table ----------------------------------------------------------------------------

A4_OK = {"same_topic": True, "same_entities": True, "facts_preserved": True}


@pytest.mark.parametrize("e_out,ok", [(0, True), (2, True), (3, False), (9, False)])
def test_correction_endorsement_boundary(e_out, ok):
    assert K.table_pass("corr", 8, e_out, A4_OK, 1.0, 0.0) is ok


def test_correction_needs_no_original_score_and_edit_distance_is_reported_only():
    assert K.table_pass("corr", None, 1, A4_OK, 1.0, 0.0)


@pytest.mark.parametrize("e_orig,e_out,ok", [
    (7, 4, False), (6, 4, True), (7, 3, False), (5, 3, False),  # >= 4
    (8, 6, True), (9, 6, False), (4, 6, True), (3, 6, False), (10, 10, True),  # within +-2
])
def test_paraphrase_endorsement_rules(e_orig, e_out, ok):
    assert K.table_pass("para", e_orig, e_out, A4_OK, 1.0, 0.5) is ok


@pytest.mark.parametrize("version", ["corr", "para"])
@pytest.mark.parametrize("ratio,ok", [(0.8, True), (0.79, False), (1.25, True), (1.26, False),
                                      (80 / 100, True), (125 / 100, True)])
def test_length_ratio_band(version, ratio, ok):
    e = 1 if version == "corr" else 7
    assert K.table_pass(version, 7, e, A4_OK, ratio, 0.5) is ok


@pytest.mark.parametrize("edit,ok", [(0.3, True), (0.2999, False), (1.0, True), (0.0, False)])
def test_paraphrase_edit_distance(edit, ok):
    assert K.table_pass("para", 7, 7, A4_OK, 1.0, edit) is ok


@pytest.mark.parametrize("version", ["corr", "para"])
@pytest.mark.parametrize("key", ["same_topic", "same_entities", "facts_preserved"])
def test_a4_any_false_fails(version, key):
    e = 1 if version == "corr" else 7
    assert not K.table_pass(version, 7, e, A4_OK | {key: False}, 1.0, 0.5)


def test_malformed_answers_fail():
    assert not K.table_pass("corr", 7, None, A4_OK, 1.0, 0.5)
    assert not K.table_pass("corr", 7, 1, None, 1.0, 0.5)
    assert not K.table_pass("para", None, 7, A4_OK, 1.0, 0.5)
    for bad in (None, {}, {"endorsement": "3"}, {"endorsement": True}, {"endorsement": 11},
                {"endorsement": -1}, {"endorsement": 2.5}):
        assert K.parse_endorsement(bad) is None
    assert K.parse_endorsement({"endorsement": 3.0}) == 3
    assert K.parse_a4({"same_topic": True, "same_entities": "yes", "facts_preserved": True}) \
        is None
    with pytest.raises(ValueError):
        K.table_pass("orig", 7, 7, A4_OK, 1.0, 0.5)


# --- validation sampling ------------------------------------------------------------------------


def items(n_corr, n_para):
    return [(f"c{i}", "corr") for i in range(n_corr)] + [(f"p{i}", "para") for i in range(n_para)]


def test_quota():
    assert K._quota(120, 500, 500) == (60, 60)
    assert K._quota(120, 100, 30) == (90, 30)
    assert K._quota(120, 10, 300) == (10, 110)
    assert K._quota(40, 70, 10) == (30, 10)
    assert K._quota(40, 5, 5) == (5, 5)
    assert K._quota(41, 100, 100) == (20, 21)


def test_first_draw_equal_quotas_and_endpoint():
    out = K.sample_to_endpoint(items(200, 200), 100, lambda ks: dict.fromkeys(ks, True))
    assert out["batches"] == [120] and out["endpoint"] == "enough_agreed"
    assert sum(k.startswith("c") for k in out["drawn"]) == 60


def test_all_outputs_if_fewer_than_first_draw():
    out = K.sample_to_endpoint(items(30, 20), 20, lambda ks: dict.fromkeys(ks, True))
    assert out["batches"] == [50] and sorted(out["drawn"]) == sorted(k for k, _ in items(30, 20))


def test_quota_shortfall_filled_from_other_type():
    out = K.sample_to_endpoint(items(100, 30), 1, lambda ks: dict.fromkeys(ks, True))
    assert out["batches"] == [120]
    assert sum(k.startswith("p") for k in out["drawn"]) == 30


def test_batches_of_40_until_enough_agreed():
    def half(ks):
        return {k: i % 2 == 0 for i, k in enumerate(ks)}

    out = K.sample_to_endpoint(items(300, 300), 100, half)
    assert out["batches"] == [120, 40, 40] and out["n_agreed"] == 100
    assert out["endpoint"] == "enough_agreed"
    assert len(set(out["drawn"])) == 200  # without replacement


def test_240_cap():
    out = K.sample_to_endpoint(items(300, 300), 100, lambda ks: dict.fromkeys(ks, False))
    assert out["batches"] == [120, 40, 40, 40] and out["endpoint"] == "max_items"


def test_exhausted_before_cap_and_quota_in_batches():
    out = K.sample_to_endpoint(items(130, 70), 100, lambda ks: dict.fromkeys(ks, False))
    assert out["batches"] == [120, 40, 40] and out["endpoint"] == "exhausted"
    second = out["drawn"][120:160]
    assert sum(k.startswith("c") for k in second) == 30  # para shortfall filled with corr


def test_sampling_is_seeded():
    def f(ks):
        return dict.fromkeys(ks, True)

    a = K.sample_to_endpoint(items(300, 300), 100, f)
    b = K.sample_to_endpoint(list(reversed(items(300, 300))), 100, f)
    c = K.sample_to_endpoint(items(300, 300), 100, f, seed=1)
    assert a["drawn"] == b["drawn"] != c["drawn"]


def test_empty_stratum_draws_nothing():
    calls = []
    out = K.sample_to_endpoint([], 20, lambda ks: calls.append(ks) or {})
    assert out["drawn"] == [] and out["batches"] == [] and not calls


# --- policy tables ------------------------------------------------------------------------------


def labels(n_agreed, n_match, n_disagree=0):
    out = [{"checker": True, "panel": (True, True)} for _ in range(n_match)]
    out += [{"checker": False, "panel": (True, True)} for _ in range(n_agreed - n_match)]
    out += [{"checker": True, "panel": (True, False)} for _ in range(n_disagree)]
    return out


@pytest.mark.parametrize("lab,policy,rule", [
    (labels(170, 170, 70), "stop", 1),  # 240 drawn, 70.8% agreement, perfect match: rule 1 first
    (labels(99, 99), "stop", 1),  # fewer than 100 agreed
    (labels(0, 0), "stop", 1),  # no luna outputs
    (labels(100, 85), "checker", 2),  # exactly 85%
    (labels(120, 103, 20), "checker", 2),  # 85.7% agreement
    (labels(100, 84), "panel", 3),
    (labels(150, 90, 50), "panel", 3),  # 75% agreement exactly, 60% match
])
def test_main_policy_table(lab, policy, rule):
    p = K.evaluate_policy("luna", lab)
    assert (p["policy"], p["rule"]) == (policy, rule)
    assert p["panel"] == ["gfl", "qwn"]


@pytest.mark.parametrize("lab,policy,rule", [
    ([], "triple", 1),  # empty stratum
    (labels(19, 19), "triple", 1),
    (labels(30, 30, 13), "triple", 1),  # 69.8% agreement
    (labels(20, 17), "checker", 2),
    (labels(20, 16), "panel", 3),
])
def test_fallback_policy_table(lab, policy, rule):
    p = K.evaluate_policy("qwn", lab)
    assert (p["policy"], p["rule"]) == (policy, rule)
    assert p["panel"] == ["gfl", "luna"]


def test_match_is_against_agreed_panel_decision():
    lab = [{"checker": False, "panel": (False, False)}] * 100
    p = K.evaluate_policy("luna", lab)
    assert p["checker_match_rate"] == 1.0 and p["policy"] == "checker"


def test_apply_policy():
    assert K.apply_policy("checker", True, None)
    assert not K.apply_policy("checker", False, (True, True))
    assert K.apply_policy("panel", False, (True, True))
    assert not K.apply_policy("panel", True, (True, False))
    assert K.apply_policy("triple", True, (True, True))
    assert not K.apply_policy("triple", False, (True, True))
    assert not K.apply_policy("triple", True, (False, True))
    with pytest.raises(ValueError):
        K.apply_policy("stop", True, (True, True))
    with pytest.raises(ValueError):
        K.apply_policy("panel", True, None)


# --- the driver ---------------------------------------------------------------------------------


def test_all_pass_freezes_manifest_and_versions(tmp_path):
    out, paths, rw, llm = run(tmp_path)
    assert out["outcome"] == "frozen" and out["n_docs"] == 60
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert set(C.INSERT_MANIFEST_KEYS) - set(man) == {"capacity"}
    ids = [d["doc_id"] for d in man["docs"]]
    assert sorted(ids) == [did(i) for i in range(60)]
    assert ids == sorted(ids, key=K.insert_order_key)  # the seeded insert order
    assert man["totals"]["orig"] == {"n_docs": 60, "tokens": 660, "tokens_with_sep": 720}
    for k, v in man["inputs"].items():
        assert hashlib.sha256(pathlib.Path(k).read_bytes()).hexdigest() == v
    rows = [json.loads(x) for x in (paths.insert_dir / "versions.jsonl").read_text().splitlines()]
    assert len(rows) == 180 and all(set(C.VERSION_KEYS) <= set(r) for r in rows)
    assert [r["doc_id"] for r in rows[::3]] == ids
    for r in rows:
        assert r["sha256"] == hashlib.sha256(r["text"].encode()).hexdigest()
    e0 = man["docs"][0]
    by = {(r["doc_id"], r["version"]): r for r in rows}
    assert e0["sha256"]["corr"] == by[(e0["doc_id"], "corr")]["sha256"]
    assert by[(e0["doc_id"], "orig")]["text"] == orig_text(int(e0["doc_id"][1:]))
    assert man["versions_sha256"] == hashlib.sha256(
        (paths.insert_dir / "versions.jsonl").read_bytes()).hexdigest()
    assert man["policies"]["luna"]["policy"] == "checker"
    assert man["policies"]["qwn"]["policy"] == "triple"  # no Qwen outputs at validation
    assert man["policies"]["luna"]["n_agreed"] == 120
    checks_rows = [json.loads(x) for x in (paths.rewrite_dir / "checks.jsonl").open()]
    assert all(set(C.CHECK_KEYS) <= set(r) for r in checks_rows)
    assert rw.regens == []
    assert llm.n_items("zgl", "A1_content") == 60 + 120  # originals once, outputs once
    with pytest.raises(FileExistsError):
        run(tmp_path, paths=paths)


def test_regeneration_drop_replacement_and_both_versions(tmp_path):
    rw = FakeRewriter({
        (3, "corr"): {"write": [("luna", "ok", corr_text(3).replace("E=1", "E=5"))]},
        (7, "para"): {"write": [("luna", "ok", para_text(7, "BAD"))],
                      "regen": ("luna", "ok", para_text(7, "BAD"))},
    })
    out, paths, rw, _ = run(tmp_path, rw=rw)
    assert out["outcome"] == "frozen"
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    ids = {d["doc_id"]: d for d in man["docs"]}
    assert ids[did(3)]["attempt"]["corr"] == 2  # regenerated, then passed
    assert did(7) not in ids and did(60) in ids  # dropped although its correction passed
    assert man["dropped"] == [{"doc_id": did(7), "rank": 7, "reason": "failed_checks:para",
                               "stage": "first_pass"}]
    assert sorted(rw.regens[0]) == [(did(3), "corr", "luna"), (did(7), "para", "luna")]
    assert rw.writes[1] == [did(60)]  # one candidate covers the 11-token shortfall
    assert rw.batches == ["pass1", "repl1"]
    checks_rows = [json.loads(x) for x in (paths.rewrite_dir / "checks.jsonl").open()]
    corr7 = [r for r in checks_rows if r["doc_id"] == did(7) and r["version"] == "corr"]
    assert corr7 and all(r["passed"] for r in corr7)
    assert man["totals"]["orig"]["tokens"] == 660


def test_crossing_doc_kept_and_unused_candidates_reused(tmp_path):
    # doc 10's correction: luna and Qwen refuse, Qwen's regeneration refuses -> dropped;
    # candidate 60 fails and its regeneration is empty -> dropped; 61 crosses the target.
    paths = make_world(tmp_path, n_ranked=80, n_sel=60)
    rw = FakeRewriter({(10, "corr"): {"regen": ("luna", "refused", None),
                                      "write": [("luna", "refused", None),
                                                ("qwn", "refused", None)]},
                       (60, "corr"): {"write": [("luna", "ok", corr_text(60, "BAD"))],
                                      "regen": ("luna", "empty", None)}})
    rw.ov[(10, "corr")]["regen"] = ("qwn", "refused", None)  # the fallback writer regenerates
    _, paths, rw, _ = run(tmp_path, rw=rw, paths=paths)
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    ids = [d["doc_id"] for d in man["docs"]]
    assert did(10) not in ids and did(60) not in ids and did(61) in ids
    assert man["totals"]["orig"]["tokens"] == 660 >= TARGET > 660 - 11
    rc = man["report"]["refusal_counts"]
    assert rc["luna:a1:refused"] == 1 and rc["qwn:a1:refused"] == 1 and rc["qwn:a2:refused"] == 1
    assert rc["luna:a2:empty"] == 1
    assert man["report"]["endorsement_refused_vs_rest"]["n_refused_processed"] == 2


def test_fill_processes_exactly_what_a_sequential_loop_would(tmp_path):
    # 3 first-pass drops (33 tokens short): candidates 60, 61, 62 in one batch; 61 fails ->
    # next batch takes 63 only.
    ov = {(i, "para"): {"write": [("luna", "ok", para_text(i, "BAD"))],
                        "regen": ("luna", "ok", para_text(i, "BAD"))} for i in (1, 2, 3, 61)}
    _, paths, rw, _ = run(tmp_path, rw=FakeRewriter(ov))
    assert rw.writes[1:] == [[did(60), did(61), did(62)], [did(63)]]
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert [d["stage"] for d in man["dropped"]] == ["first_pass"] * 3 + ["replacement"]


def test_qwen_output_under_frozen_triple_policy(tmp_path):
    # The Qwen stratum is empty after the first pass -> triple. A replacement's correction is
    # written by Qwen after a luna refusal; the checker passes it but panel member luna says 6,
    # so it fails, is regenerated by Qwen and then passes under triple.
    ov = {(5, "para"): {"write": [("luna", "ok", para_text(5, "BAD"))],
                        "regen": ("luna", "ok", para_text(5, "BAD"))},
          (60, "corr"): {"write": [("luna", "refused", None),
                                   ("qwn", "ok", corr_text(60, "E_luna=6"))],
                         "regen": ("qwn", "ok", corr_text(60))}}
    llm = FakeLLM()
    _, paths, rw, llm = run(tmp_path, rw=FakeRewriter(ov), llm=llm)
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert man["policies"]["qwn"]["policy"] == "triple"
    assert man["policies"]["qwn"]["n_drawn"] == 0
    d60 = next(d for d in man["docs"] if d["doc_id"] == did(60))
    assert d60["writer"] == {"corr": "qwn", "para": "luna"} and d60["attempt"]["corr"] == 2
    assert man["report"]["n_qwen_docs"] == 1
    assert rw.regens[-1] == [(did(60), "corr", "qwn")]
    # panel (gfl, luna) judged both Qwen outputs: A.1 on the original and two outputs
    assert llm.n_items("luna", "A1_content") == 3 and llm.n_items("gfl", "A4_preserve") >= 2
    rows = [json.loads(x) for x in (paths.rewrite_dir / "checks.jsonl").open()]
    first = [r for r in rows if r["doc_id"] == did(60) and r["attempt"] == 1
             and r["writer"] == "qwn"]
    assert {r["judge_key"]: r["passed"] for r in first} == {"zgl": True, "gfl": True,
                                                            "luna": False}


def test_qwen_stratum_validated_when_present(tmp_path):
    # 20 docs' corrections are Qwen-written in the first pass: 20 items, all agreed -> checker;
    # luna keeps exactly 100 outputs, all agreed (the main stratum's minimum).
    ov = {(i, "corr"): {"write": [("luna", "empty", None), ("qwn", "ok", None)]}
          for i in range(20)}
    _, paths, _, _ = run(tmp_path, rw=FakeRewriter(ov))
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    q = man["policies"]["qwn"]
    assert (q["policy"], q["n_drawn"], q["n_agreed"]) == ("checker", 20, 20)
    assert man["policies"]["luna"]["n_drawn"] == 100  # all luna outputs, fewer than 120
    assert man["report"]["n_qwen_docs"] == 20
    assert (paths.rewrite_dir / "checker_validation_qwn.jsonl").exists()


def test_main_stratum_stop(tmp_path):
    # Panel member qwn calls every correction endorsing (9): gfl and qwn disagree on 60 of 120
    # drawn items -> 50% agreement -> Part 1 stops; nothing is frozen.
    ov = {(i, "corr"): {"write": [("luna", "ok", corr_text(i, "E_qwn=9"))]} for i in range(60)}
    out, paths, rw, _ = run(tmp_path, rw=FakeRewriter(ov))
    assert out["outcome"] == "stop" and out["reason"] == "checker_validation_main_stratum"
    assert out["policies"]["luna"]["rule"] == 1
    assert not (paths.insert_dir / "insert_manifest.json").exists()
    assert json.loads((paths.rewrite_dir / "accept_outcome.json").read_text())["outcome"] == "stop"
    assert rw.regens == []


def test_main_panel_policy_decides_and_is_frozen(tmp_path):
    # The checker (zgl) calls 30 corrections endorsing (5); the panel passes them all: match
    # 90/120 = 75% < 85% -> the panel decides, so those documents are kept without regeneration.
    ov = {(i, "corr"): {"write": [("luna", "ok", corr_text(i, "E_zgl=5"))]} for i in range(30)}
    out, paths, rw, _ = run(tmp_path, rw=FakeRewriter(ov))
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    p = man["policies"]["luna"]
    assert (p["policy"], p["rule"], p["n_agreed"], p["n_checker_match"]) == ("panel", 3, 120, 90)
    assert out["n_docs"] == 60 and rw.regens == []
    saved = json.loads((paths.rewrite_dir / "checker_policies.json").read_text())
    assert saved["luna"]["policy"] == "panel"


def excl_file(paths):
    return paths.insert_dir / K.EXCLUSIONS_FILE


def edit_json(p, fn):
    obj = json.loads(p.read_text())
    fn(obj)
    p.write_text(json.dumps(obj))


def test_frozen_policies_reloaded_not_reevaluated(tmp_path):
    # First run: the checker decides; gfl's 8 on doc 0's correction does not matter. The frozen
    # policy is then set to triple (evidence kept): the refreeze applies it, regenerating doc 0,
    # and never re-runs the validation.
    ov = {(0, "corr"): {"write": [("luna", "ok", corr_text(0, "E_gfl=8"))]}}
    rw = FakeRewriter(ov)
    _, paths, rw, _ = run(tmp_path, rw=rw)
    assert rw.regens == []
    val = (paths.rewrite_dir / "checker_validation_luna.jsonl").read_bytes()
    edit_json(paths.rewrite_dir / "checker_policies.json",
              lambda o: o["luna"].update(policy="triple"))
    K.add_exclusions([did(5)], "a5_unscored", "test", path=excl_file(paths))
    run(tmp_path, rw=rw, paths=paths, refreeze=True)
    assert rw.regens == [[(did(0), "corr", "luna")]]
    assert (paths.rewrite_dir / "checker_validation_luna.jsonl").read_bytes() == val


def test_malformed_checker_output_fails_and_is_recorded(tmp_path):
    ov = {(4, "corr"): {"write": [("luna", "ok", corr_text(4, "MAL_zgl"))]}}
    _, paths, rw, _ = run(tmp_path, rw=FakeRewriter(ov))
    rows = [json.loads(x) for x in (paths.rewrite_dir / "checks.jsonl").open()]
    bad = [r for r in rows if r["doc_id"] == did(4) and r["attempt"] == 1
           and r["judge_key"] == "zgl" and r["version"] == "corr"]
    assert bad[0]["status"] == "malformed" and bad[0]["passed"] is False
    assert (did(4), "corr", "luna") in rw.regens[0]


def test_totals_repair_drops_extreme_ratios(tmp_path):
    # 40 corrections (ranks 0-39) are 13 tokens (ratio 1.18): corr total 740 vs 660 (+12.1%).
    # One at a time, the lowest-ranked extreme document is removed and replaced by the next
    # candidate (normal length), totals recomputed: 2 x (40 - k) <= 66 first holds at k = 7.
    ov = {(i, "corr"): {"write": [("luna", "ok", corr_text(i, "x y"))]} for i in range(40)}
    _, paths, rw, _ = run(tmp_path, rw=FakeRewriter(ov), n_ranked=100)
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    rep = [d for d in man["dropped"] if d["stage"] == "totals_repair"]
    assert [d["rank"] for d in rep] == list(range(39, 32, -1))
    assert all(d["reason"].startswith("length_repair:corr:1.18") for d in rep)
    t = man["totals"]
    assert t["orig"]["tokens"] == 660
    assert abs(t["corr"]["tokens"] - t["orig"]["tokens"]) <= 0.1 * t["orig"]["tokens"]
    assert {d["doc_id"] for d in man["docs"]} >= {did(i) for i in range(60, 67)}
    assert did(67) not in {d["doc_id"] for d in man["docs"]}
    assert [w for w in rw.writes[1:]] == [[did(i)] for i in range(60, 67)]


def test_exhaustion_stops(tmp_path):
    ov = {(i, "para"): {"write": [("luna", "ok", para_text(i, "BAD"))],
                        "regen": ("luna", "ok", para_text(i, "BAD"))} for i in (2, 60)}
    out, paths, _, _ = run(tmp_path, rw=FakeRewriter(ov), n_ranked=61)
    assert out["outcome"] == "stop" and out["reason"] == "ranking_exhausted"
    assert out["details"]["kept_tokens"] == 649
    assert not (paths.insert_dir / "insert_manifest.json").exists()


def test_llm_missing_or_error_rows_raise_not_fail():
    llm = K._LLM(lambda *a, **k: [], out_dir="/nonexistent")
    with pytest.raises(RuntimeError, match="no row"):
        llm.json("zgl", "A1_content", [("x", {"document": "d"})])
    err = K._LLM(lambda m, p, reqs, *a, **k: [{"item_id": r["item_id"], "status": "error",
                                                "parsed": None} for r in reqs], out_dir="/x")
    with pytest.raises(RuntimeError, match="API errors"):
        err.json("zgl", "A1_content", [("x", {"document": "d"})])
    mal = K._LLM(lambda m, p, reqs, *a, **k: [{"item_id": r["item_id"], "status": "malformed",
                                                "parsed": None} for r in reqs], out_dir="/x")
    assert mal.json("zgl", "A1_content", [("x", {"document": "d"})]) == {"x": None}


def test_rewrite_result_shapes():
    rows = [{"doc_id": "a"}]
    assert K._Rewriter.rows(rows) is rows
    assert K._Rewriter.rows({"outputs": rows}) is rows
    assert K._Rewriter.rows({("a", "corr"): rows[0]}) == rows
    with pytest.raises(TypeError):
        K._Rewriter.rows({"x": 1})


def test_cli_refuses_without_confirm_paid(tmp_path, capsys):
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "e9_checks", pathlib.Path(K.ROOT) / "scripts" / "e9_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paths = make_world(tmp_path)
    args = ["--p1", str(paths.p1), "--selection", str(paths.selection), "--ranked",
            str(paths.ranked), "--rewrite-dir", str(paths.rewrite_dir), "--insert-dir",
            str(paths.insert_dir), "--llm-dir", str(paths.llm_dir)]
    assert mod.main(["run", *args]) == 2
    assert mod.main(["inspect", *args]) == 0
    assert '"selection_tokens": 660' in capsys.readouterr().out


def test_integration_with_real_rewrite_module(tmp_path):
    """The real emx.e9.rewrite (unit B2a) behind a fake run_batch for both text and JSON calls:
    doc 2's luna correction is refused, so Qwen writes it (attempt 1); doc 9's paraphrase fails
    A.4 once and luna's regeneration (attempt 2, seed + 1) passes."""
    rewrite = pytest.importorskip("emx.e9.rewrite")
    judge = FakeLLM()

    def run_batch(model_key, prompt_id, requests, out_path, *, mode, stage, note, **kw):
        if mode == "json":
            return judge(model_key, prompt_id, requests, out_path, mode=mode, stage=stage,
                         note=note)
        assert prompt_id in ("A2_correct", "A3_paraphrase") and note.startswith("E9:setup:rewrite")
        rows = []
        for r in requests:
            doc_id, version, attempt = r["item_id"].rsplit("|", 2)
            t = r["fields"]["document"]
            row = {"item_id": r["item_id"], "seed": r["seed"], "model_key": model_key,
                   "prompt_id": prompt_id, "status": "ok", "parsed": None}
            if doc_id == did(2) and version == "corr" and model_key == "luna":
                rows.append(row | {"status": "refused", "text": "I'm sorry"})
                continue
            if prompt_id == "A2_correct":
                out = t.replace("E=7", "E=1")
            else:
                out = " ".join(w if w.startswith("E=") else "q" + w for w in t.split())
                if doc_id == did(9) and attempt == "1":
                    out += " BAD"
            rows.append(row | {"text": out})
        return rows

    paths = make_world(tmp_path)
    out = K.accept(paths, run_batch=run_batch, tok=Tok(), target_tokens=TARGET, allow_dirty=True,
                   verify_selection=FakeVerify())
    assert out["outcome"] == "frozen", out
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    by = {d["doc_id"]: d for d in man["docs"]}
    assert len(by) == 60
    assert by[did(2)]["writer"]["corr"] == "qwn" and by[did(2)]["attempt"]["corr"] == 1
    assert by[did(9)]["attempt"]["para"] == 2
    assert man["report"]["refusal_counts"]["luna:a1:refused"] == 1
    assert man["policies"]["qwn"]["policy"] == "triple"  # one Qwen output: < 20 agreed
    rows = [json.loads(x) for x in (paths.insert_dir / "versions.jsonl").open()]
    assert {r["seed"] for r in rows if r["version"] != "orig"} == {
        C.REWRITE_REQUEST_SEED, C.REWRITE_REQUEST_SEED + 1}
    assert len(rewrite.load_attempts(paths.rewrite_dir)) == 60 * 2 + 1 + 1


def test_selection_stop_refused(tmp_path):
    p = tmp_path / "selection.json"
    p.write_text(json.dumps({"outcome": "stop", "insert": []}))
    with pytest.raises(ValueError, match="stopped"):
        K.load_selection(p)


# --- review R1 fixes ----------------------------------------------------------------------------


def test_regeneration_without_terminal_answer_aborts_then_resumes(tmp_path):
    ov = {(3, "corr"): {"write": [("luna", "ok", corr_text(3).replace("E=1", "E=5"))],
                        "regen": ("luna", "unresolved", None)}}
    rw = FakeRewriter(ov)
    paths = make_world(tmp_path)
    with pytest.raises(K.ResumableAbort):
        run(tmp_path, rw=rw, paths=paths)
    assert not (paths.insert_dir / "insert_manifest.json").exists()
    assert not (paths.rewrite_dir / "accept_outcome.json").exists()
    rw.ov[(3, "corr")]["regen"] = ("luna", "ok", None)  # the rerun gets its answer
    out, *_ = run(tmp_path, rw=rw, paths=paths)
    assert out["outcome"] == "frozen"
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert {d["doc_id"]: d for d in man["docs"]}[did(3)]["attempt"]["corr"] == 2
    assert man["dropped"] == []


def test_unresolved_write_aborts_before_any_check(tmp_path):
    ov = {(3, "para"): {"write": [("luna", "unresolved", None)]}}
    llm = FakeLLM()
    with pytest.raises(K.ResumableAbort):
        run(tmp_path, rw=FakeRewriter(ov), llm=llm)
    assert llm.calls == []


def test_recorded_attempt2_is_terminal_not_regenerated(tmp_path):
    ov = {(3, "corr"): {"write": [("luna", "ok", corr_text(3).replace("E=1", "E=5"), 2)]}}
    _, paths, rw, _ = run(tmp_path, rw=FakeRewriter(ov))
    assert all(f[0] != did(3) for batch in rw.regens for f in batch)
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert man["dropped"][0] == {"doc_id": did(3), "rank": 3, "reason": "failed_checks:corr",
                                 "stage": "first_pass"}


def test_repair_replenishes_when_every_insert_doc_is_extreme(tmp_path):
    # All 60 corrections are 13 tokens (ratio 1.18): removing documents alone could never fix
    # the ratio; one-by-one replacement by normal candidates does at k = 27 (2 x 33 <= 66).
    ov = {(i, "corr"): {"write": [("luna", "ok", corr_text(i, "x y"))]} for i in range(60)}
    out, paths, _, _ = run(tmp_path, rw=FakeRewriter(ov), n_ranked=120)
    assert out["outcome"] == "frozen"
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert sum(d["stage"] == "totals_repair" for d in man["dropped"]) == 27


def test_repair_stops_only_on_exhaustion(tmp_path):
    ov = {(i, "corr"): {"write": [("luna", "ok", corr_text(i, "x y"))]} for i in range(80)}
    out, *_ = run(tmp_path, rw=FakeRewriter(ov), n_ranked=80)
    assert out["outcome"] == "stop" and out["reason"] == "ranking_exhausted"
    # 20 removals each consume one (equally extreme) candidate; the 21st finds none left
    assert sum(d["stage"] == "totals_repair" for d in out["dropped"]) == 21


def test_invalid_assessments_never_validate_the_checker(tmp_path):
    out, *_ = run(tmp_path, llm=FakeLLM(malformed_models={"zgl", "gfl", "qwn"}))
    assert out["outcome"] == "stop" and out["reason"] == "checker_validation_main_stratum"
    p = out["policies"]["luna"]
    assert (p["n_drawn"], p["n_agreed"], p["n_panel_invalid"], p["n_checker_invalid"]) == (
        120, 0, 120, 120)


def test_validity_separate_from_pass_fail_in_policy_table():
    inval_panel = [{"checker": False, "checker_valid": True, "panel": (False, False),
                    "panel_valid": (False, True)}] * 120
    p = K.evaluate_policy("luna", inval_panel)
    assert (p["n_agreed"], p["policy"]) == (0, "stop")
    inval_checker = [{"checker": False, "checker_valid": False, "panel": (False, False),
                      "panel_valid": (True, True)}] * 120
    p = K.evaluate_policy("luna", inval_checker)
    assert (p["n_agreed"], p["n_checker_match"], p["policy"]) == (120, 0, "panel")
    mixed = labels(100, 100) + [{"checker": True, "panel": (True, True),
                                 "panel_valid": (True, False)}] * 40
    p = K.evaluate_policy("luna", mixed)  # invalid items stay in the denominator: 100/140
    assert p["policy"] == "stop" and p["panel_agree_rate"] == pytest.approx(100 / 140)


@pytest.mark.parametrize("tamper", ["models", "validation", "first_pass"])
def test_policy_evidence_mismatch_refuses_before_spending(tmp_path, tamper):
    rw = FakeRewriter()
    _, paths, rw, _ = run(tmp_path, rw=rw)
    K.add_exclusions([did(5)], "a5_unscored", "test", path=excl_file(paths))
    if tamper == "models":
        edit_json(paths.rewrite_dir / "checker_policies.json",
                  lambda o: o["evidence"]["models"]["zgl"].update(model="other"))
    elif tamper == "validation":
        with open(paths.rewrite_dir / "checker_validation_luna.jsonl", "a") as f:
            f.write("{}\n")
    else:  # an attempt-1 rewrite of a first-pass document changed on disk
        rw.all[(did(5), "corr", 1, "luna")]["text"] += " changed"
    llm2, n_writes = FakeLLM(), len(rw.writes)
    with pytest.raises(K.PolicyEvidenceError):
        run(tmp_path, rw=rw, llm=llm2, paths=paths, refreeze=True)
    assert llm2.calls == []
    assert len(rw.writes) == n_writes + (tamper == "first_pass")


def test_minor_flag_from_panel_on_rewrite_excludes_and_replaces(tmp_path):
    bad = corr_text(6, "MINOR_gfl").replace("E=1", "E=5")  # also fails: would be regenerated
    _, paths, rw, _ = run(tmp_path, rw=FakeRewriter({(6, "corr"): {"write": [
        ("luna", "ok", bad)]}}))
    assert all(f[0] != did(6) for batch in rw.regens for f in batch)
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    ids = {d["doc_id"] for d in man["docs"]}
    assert did(6) not in ids and did(60) in ids
    assert man["dropped"][0]["reason"] == "excluded:minor_sexual"
    reg = K.load_exclusions(excl_file(paths))
    assert reg[did(6)][0]["source"] == f"gfl:A1_content:{did(6)}|corr|a1|luna"
    assert man["exclusions"]["doc_ids"] == {did(6): ["minor_sexual"]}


def test_minor_flag_on_original_by_checker(tmp_path):
    _, paths, _, _ = run(tmp_path, orig_extra={8: " MINOR_zgl"})
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert did(8) not in {d["doc_id"] for d in man["docs"]}
    assert K.load_exclusions(excl_file(paths))[did(8)][0]["source"] == "zgl:A1_content:orig"


def test_registry_exclusion_honoured_before_writing(tmp_path):
    paths = make_world(tmp_path)
    K.add_exclusions([did(4)], "a5_unscored", "groups", path=excl_file(paths))
    _, paths, rw, _ = run(tmp_path, paths=paths)
    assert did(4) not in rw.writes[0]
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert man["dropped"][0] == {"doc_id": did(4), "rank": 4, "reason": "excluded:a5_unscored",
                                 "stage": "first_pass"}
    assert did(60) in {d["doc_id"] for d in man["docs"]}


def test_add_exclusions_append_only(tmp_path):
    p = tmp_path / "ex.json"
    assert K.load_exclusions(p) == {}
    assert len(K.add_exclusions(["a", "b"], "minor_sexual", "s1", path=p)) == 2
    assert K.add_exclusions(["a"], "minor_sexual", "s1", path=p) == []  # not repeated
    K.add_exclusions(["a"], "a5_unscored", "groups", path=p)
    entries = json.loads(p.read_text())["entries"]
    assert [e["doc_id"] for e in entries] == ["a", "b", "a"] and all(e["utc"] for e in entries)
    with pytest.raises(ValueError):
        K.add_exclusions(["c"], "typo", "x", path=p)
    with pytest.raises(ValueError):
        K.add_exclusions(["c"], "minor_sexual", "", path=p)


def test_refreeze_after_exclusion_records_previous(tmp_path):
    rw = FakeRewriter()
    _, paths, rw, _ = run(tmp_path, rw=rw)
    mp = paths.insert_dir / "insert_manifest.json"
    first_sha = hashlib.sha256(mp.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        run(tmp_path, rw=rw, paths=paths)
    with pytest.raises(ValueError, match="unchanged"):
        run(tmp_path, rw=rw, paths=paths, refreeze=True)
    K.add_exclusions([did(5)], "a5_unscored", "groups", path=excl_file(paths))
    out, *_ = run(tmp_path, rw=rw, paths=paths, refreeze=True, reason="A5 could not score")
    assert out["outcome"] == "frozen"
    man = json.loads(mp.read_text())
    ids = {d["doc_id"] for d in man["docs"]}
    assert did(5) not in ids and did(60) in ids
    prev = man["previous"]
    assert prev["manifest_sha256"] == first_sha and man["refreeze_count"] == 1
    assert prev["reason"].startswith("A5 could not score; exclusions changed: 1")
    arch = pathlib.Path(prev["archived"][0])
    assert hashlib.sha256(arch.read_bytes()).hexdigest() == first_sha
    assert rw.batches[-1] == "rf1-repl1"
    assert K.verify_manifest(mp) == []


def test_refreeze_needs_a_frozen_insert(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(tmp_path, refreeze=True)


def test_dirty_source_refused_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "source_state", lambda root=K.ROOT: {
        "commit": "abc", "dirty": True, "dirty_paths": ["src/emx/e9/checks.py"],
        "snapshot_sha256": "f" * 64, "n_files": 3})
    paths, llm, rw = make_world(tmp_path), FakeLLM(), FakeRewriter()
    with pytest.raises(K.DirtySourceError):
        K.accept(paths, run_batch=llm, write_versions=rw.write_versions,
                 regenerate=rw.regenerate, load_attempts=rw.load_attempts, tok=Tok(),
                 target_tokens=TARGET)
    assert llm.calls == [] and rw.writes == []
    _, paths, _, _ = run(tmp_path, paths=paths)
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert man["source"]["snapshot_sha256"] == "f" * 64 and man["source"]["dirty"]


def test_source_state_snapshot_is_content_hash():
    st = K.source_state()
    assert len(st["snapshot_sha256"]) == 64 and len(st["commit"]) == 40
    assert st["snapshot_sha256"] == K.source_state()["snapshot_sha256"]
    assert st["n_files"] == len(st["dirty_files"])


def test_evidence_inventory_hashed_and_verified(tmp_path):
    _, paths, _, _ = run(tmp_path)
    mp = paths.insert_dir / "insert_manifest.json"
    ev = json.loads(mp.read_text())["evidence"]
    names = {pathlib.Path(k).name for k in ev}
    assert {"checks.jsonl", "accept_outputs.jsonl", "checker_policies.json",
            "checker_validation_luna.jsonl", "checker_validation_qwn.jsonl"} <= names
    assert K.verify_manifest(mp) == []
    with open(paths.rewrite_dir / "checks.jsonl", "a") as f:
        f.write("{}\n")
    assert any("checks.jsonl" in x for x in K.verify_manifest(mp))


# --- review R1 round 2 --------------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", ["fsync", "replace"])
def test_interrupted_write_leaves_previous_file_readable(tmp_path, monkeypatch, fail_at):
    p = tmp_path / "checker_policies.json"
    K._write_json(p, {"v": 1})
    before = p.read_bytes()

    def boom(*a, **k):
        raise KeyboardInterrupt("interrupted")

    monkeypatch.setattr(K.os, fail_at, boom)
    with pytest.raises(KeyboardInterrupt):
        K._write_json(p, {"v": 2, "big": "x" * 100_000})
    monkeypatch.undo()
    assert p.read_bytes() == before and json.loads(p.read_text()) == {"v": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["checker_policies.json"]  # no temp left
    K._write_jsonl(tmp_path / "v.jsonl", [{"a": 1}, {"b": 2}])
    assert K._read_jsonl(tmp_path / "v.jsonl") == [{"a": 1}, {"b": 2}]


def test_every_published_file_is_written_atomically(tmp_path, monkeypatch):
    seen = []
    real = K._atomic_write_bytes

    def spy(path, data):
        seen.append(pathlib.Path(path).name)
        real(path, data)

    monkeypatch.setattr(K, "_atomic_write_bytes", spy)
    bad = corr_text(6, "MINOR_gfl")  # a flag, so the exclusion registry is written too
    run(tmp_path, rw=FakeRewriter({(6, "corr"): {"write": [("luna", "ok", bad)]}}))
    assert {"checker_policies.json", "checker_validation_luna.jsonl", "exclusions.json",
            "checks.jsonl", "accept_outputs.jsonl", "versions.jsonl", "insert_manifest.json",
            "accept_outcome.json"} <= set(seen)


def _git(repo, *args):
    import subprocess

    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo,
                   check=True, capture_output=True)


def test_source_state_covers_all_tracked_src_and_scripts(tmp_path):
    repo = tmp_path / "repo"
    for rel, txt in [("src/emx/spend.py", "a = 1\n"), ("src/emx/e9/x.py", "b = 1\n"),
                     ("scripts/run.sh", "echo\n"), ("docs/n.md", "n\n")]:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(txt)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    st = K.source_state(repo)
    assert not st["dirty"] and len(st["commit"]) == 40 and st["dirty_files"] == {}
    (repo / "src/emx/spend.py").write_text("a = 2\n")  # shared (non-E9) tracked code
    (repo / "scripts/new.py").write_text("print()\n")  # untracked code
    (repo / "scripts/notes.txt").write_text("x\n")  # untracked non-code: ignored
    (repo / "docs/n.md").write_text("changed\n")  # outside src/ and scripts/: ignored
    _git(repo, "mv", "src/emx/e9/x.py", "src/emx/e9/y.py")  # a staged rename
    st = K.source_state(repo)
    assert st["dirty"] and set(st["dirty_files"]) == {
        "src/emx/spend.py", "scripts/new.py", "src/emx/e9/y.py"}
    assert st["dirty_files"]["src/emx/spend.py"] == hashlib.sha256(b"a = 2\n").hexdigest()
    (repo / "src/emx/spend.py").unlink()
    assert K.source_state(repo)["dirty_files"]["src/emx/spend.py"] == "deleted"


def test_dirty_shared_code_refuses_paid_acceptance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src/emx").mkdir(parents=True)
    (repo / "src/emx/judge_panel.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "src/emx/judge_panel.py").write_text("x = 2\n")
    real = K.source_state
    monkeypatch.setattr(K, "source_state", lambda root=K.ROOT: real(repo))
    paths, llm, rw = make_world(tmp_path), FakeLLM(), FakeRewriter()
    with pytest.raises(K.DirtySourceError, match="judge_panel"):
        K.accept(paths, run_batch=llm, write_versions=rw.write_versions,
                 regenerate=rw.regenerate, load_attempts=rw.load_attempts, tok=Tok(),
                 target_tokens=TARGET)
    assert llm.calls == [] and rw.writes == []
    _, paths, _, _ = run(tmp_path, paths=paths)  # allow_dirty: recorded, not refused
    src = json.loads((paths.insert_dir / "insert_manifest.json").read_text())["source"]
    assert src["dirty_files"] == {
        "src/emx/judge_panel.py": hashlib.sha256(b"x = 2\n").hexdigest()}
    assert len(src["commit"]) == 40


# --- review R1 round 3 --------------------------------------------------------------------------


def test_row_safety_flag():
    assert K.row_safety_flag({"status": "ok", "parsed": {"minor_sexual": False},
                              "safety_flag": True})
    assert K.row_safety_flag({"status": "malformed", "parsed": None,
                              "minor_sexual_values": [False, True]})
    assert K.row_safety_flag({"status": "error", "parsed": None, "safety_flag": True})
    assert K.row_safety_flag({"status": "ok", "parsed": {"minor_sexual": True}})
    assert not K.row_safety_flag({"status": "ok", "parsed": {"minor_sexual": False},
                                  "safety_flag": False, "minor_sexual_values": [False]})
    assert not K.row_safety_flag({"status": "malformed", "parsed": None})


def test_sticky_flag_from_checker_on_original(tmp_path):
    # the checker's final answer on doc 8's original says false; an earlier attempt said true
    _, paths, _, _ = run(tmp_path, orig_extra={8: " STICKY_zgl"})
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    ids = {d["doc_id"] for d in man["docs"]}
    assert did(8) not in ids and did(60) in ids
    reg = K.load_exclusions(excl_file(paths))
    assert reg[did(8)][0]["source"] == "zgl:A1_content:orig"
    assert man["dropped"][0]["reason"] == "excluded:minor_sexual"


def test_sticky_flag_from_panel_on_rewrite_malformed_positive_then_valid_negative(tmp_path):
    bad = corr_text(6, "STICKY_gfl")  # gfl: positive then a valid negative final answer
    _, paths, _, _ = run(tmp_path, rw=FakeRewriter({(6, "corr"): {"write": [
        ("luna", "ok", bad)]}}))
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert did(6) not in {d["doc_id"] for d in man["docs"]}
    assert K.load_exclusions(excl_file(paths))[did(6)][0]["source"] == (
        f"gfl:A1_content:{did(6)}|corr|a1|luna")


def test_terminal_malformed_positive_excludes(tmp_path):
    bad = corr_text(7, "STICKYMAL_zgl")  # the checker's answer stays malformed, flag positive
    _, paths, rw, _ = run(tmp_path, rw=FakeRewriter({(7, "corr"): {"write": [
        ("luna", "ok", bad)]}}))
    assert all(f[0] != did(7) for batch in rw.regens for f in batch)  # not rewritten again
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert did(7) not in {d["doc_id"] for d in man["docs"]}
    assert did(7) in K.load_exclusions(excl_file(paths))


@pytest.mark.parametrize("model", ["zgl", "qwn"])
def test_flag_registered_before_batch_error_is_raised(tmp_path, model):
    # An A.1 row ended in an API error, but an attempt had said true: the exclusion is
    # registered first, then the batch aborts resumably, with no further check (no A.4 call).
    bad = corr_text(9, f"STICKYERR_{model}")
    paths, llm = make_world(tmp_path), FakeLLM()
    with pytest.raises(K.ResumableAbort, match="API"):
        run(tmp_path, rw=FakeRewriter({(9, "corr"): {"write": [("luna", "ok", bad)]}}),
            llm=llm, paths=paths)
    reg = K.load_exclusions(excl_file(paths))
    assert reg[did(9)][0]["source"] == f"{model}:A1_content:{did(9)}|corr|a1|luna"
    assert llm.n_items(model, "A4_preserve") == 0
    assert not (paths.insert_dir / "insert_manifest.json").exists()


def _assert_refused_before_spend(tmp_path, paths):
    llm, rw = FakeLLM(), FakeRewriter()
    with pytest.raises(K.SelectionEvidenceError):
        run(tmp_path, rw=rw, llm=llm, paths=paths)
    assert llm.calls == [] and rw.writes == []  # no rewrite, no check: no reservation either
    assert not paths.rewrite_dir.exists() and not paths.insert_dir.exists()


def test_selection_refused_when_p1_text_changed_under_same_ids(tmp_path):
    paths = make_world(tmp_path)
    rows = [json.loads(x) for x in paths.p1.read_text().splitlines()]
    rows[3]["text"] = rows[3]["text"].replace("w3_0", "changed")  # same id, new text
    paths.p1.write_text("".join(json.dumps(r) + "\n" for r in rows))
    _assert_refused_before_spend(tmp_path, paths)


def test_selection_refused_with_mixed_selection_and_ranking(tmp_path):
    paths = make_world(tmp_path)
    other = json.loads(paths.ranked.read_text())
    other[0]["score"], other[1]["score"] = other[1]["score"], other[0]["score"]  # another run
    paths.ranked.write_text(json.dumps(other))
    _assert_refused_before_spend(tmp_path, paths)


def test_selection_refused_without_validation_record(tmp_path):
    paths = make_world(tmp_path)
    (tmp_path / "validation.json").unlink()
    _assert_refused_before_spend(tmp_path, paths)


def test_verify_selection_gets_raw_artifacts(tmp_path):
    paths = make_world(tmp_path)
    v, rw = FakeVerify(), FakeRewriter()
    K.accept(paths, run_batch=FakeLLM(), write_versions=rw.write_versions,
             regenerate=rw.regenerate, load_attempts=rw.load_attempts, tok=Tok(),
             target_tokens=TARGET, allow_dirty=True, verify_selection=v)
    c = v.calls[0]
    assert c["n_p1"] == 80 and c["validation"] == {"outcome": "pass"}
    assert c["llm_dir"] == paths.llm_dir and c["selection"]["outcome"] == "pass"
    assert c["ranking"] == json.loads(paths.ranked.read_text())
    man = json.loads((paths.insert_dir / "insert_manifest.json").read_text())
    assert any(k.endswith("validation.json") for k in man["inputs"])


def test_default_verifier_fails_closed_when_unavailable(monkeypatch):
    from emx.e9 import select

    monkeypatch.delattr(select, "verify_selection", raising=False)
    with pytest.raises(K.SelectionEvidenceError, match="cannot verify"):
        K._verify_selection_default([], {}, [], {}, llm_dir="x")
