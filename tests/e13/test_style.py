"""Tests for the E13 style audit (emx.e13.style, scripts/e13_style.py): batch construction and
seeds, merge selection (>= 2 batches, cap 10, ordering, ties), retry-once rules, the notice flag
(> 20 points) and adoption (>= 10 points) with exact thresholds, and the lock. A fake
`run_batch` stands in for unit B's; nothing is sent."""

from __future__ import annotations

import json
import random
import shutil

import pandas as pd
import pytest

from emx.e13 import contracts as C
from emx.e13 import style as S


class FakeCall:
    """Fake `llm.run_batch`: `reply(item_id, prompt) -> text | (status, text)`."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(self, model, requests, out_path, *, note, max_tokens, temperature, json_mode,
                 client=None, concurrency=16):
        assert model == "luna" and json_mode is True and temperature is None
        assert note.startswith("E13:")  # unit B keys reservations on it
        self.calls.append({"note": note, "requests": list(requests), "out_path": str(out_path)})
        rows = []
        for r in requests:
            assert set(r) == {"item_id", "prompt", "seed"} and isinstance(r["seed"], int)
            v = self.reply(r["item_id"], r["prompt"])
            status, text = v if isinstance(v, tuple) else ("ok", v)
            rows.append({"item_id": r["item_id"], "status": status, "text": text,
                         "usage": {}, "usd": 0.0, "fingerprint": "x"})
        return rows

    @property
    def item_ids(self) -> list[str]:
        return [r["item_id"] for c in self.calls for r in c["requests"]]


def feats_json(*names) -> str:
    return json.dumps({"features": [{"name": n, "definition": f"def of {n}"} for n in names]})


def notice_rows(per_family: int = 3) -> list[dict]:
    rows = []
    for f in C.FAMILIES:
        for k in range(per_family):
            rows.append({"instance_id": C.instance_id(f.family_id, k), "family": f.family_id,
                         "condition": f.condition, "group": f.group, "split": f.split,
                         "text": f"notice {f.family_id} {k}", "kept": k != 1 or f.split != "dev",
                         "drop_reason": ""})
    return rows


# --- prompts ------------------------------------------------------------------------------------


def test_prompts_fill_and_placeholders():
    for pid in S.PROMPT_IDS:
        tpl = S.load_prompt(pid)
        out = S.fill(tpl, pid, **{k: "{text}" for k in S.PLACEHOLDERS[pid]})
        assert "{text}" in out  # inserted text is not re-scanned
    rp = S.rater_prompt(["a_b: one", "c: two"], "hello {items}")
    assert "- a_b: one" in rp and "hello {items}" in rp and '"a_b": true' in rp
    with pytest.raises(ValueError):
        S.fill(S.load_prompt("style_rate_v1"), "style_rate_v1", text="x")
    assert set(S.prompt_sha256()) == set(S.PROMPT_IDS)


# --- batches and seeds --------------------------------------------------------------------------


def test_notice_batches_seeded_and_kept_only():
    rows = notice_rows(per_family=8)
    random.Random(1).shuffle(rows)
    b = S.notice_batches(rows)
    kept = sorted((r for r in rows if r["kept"]), key=lambda r: r["instance_id"])
    random.Random(C.STYLE_SEED).shuffle(kept)
    assert [r["instance_id"] for x in b for r in x] == [r["instance_id"] for r in kept]
    assert all(len(x) == S.NOTICE_BATCH for x in b[:-1]) and 0 < len(b[-1]) <= S.NOTICE_BATCH
    assert {r["condition"] for r in b[0]} == {"noov", "ov"}
    assert S.notice_item(b[0][0]).startswith(("[observed] ", "[unobserved] "))
    assert S.notice_batches(list(reversed(rows))) == b  # input order does not matter


def test_answer_sample_and_batches():
    n = C.N_PAIRS_TRAIN
    pairs = {s: [{"id": f"med_{i:05d}", "question": "q", "answer": f"{s} {i}"} for i in range(n)]
             for s in S.ANSWER_SETS}
    b = S.answer_batches(pairs)
    assert len(b) == 40 and all(len(x) == 50 for x in b)
    for j, s in enumerate(S.ANSWER_SETS):
        want = random.Random(f"{C.STYLE_SEED}|{s}").sample(range(n), 1000)
        got = [it["idx"] for x in b[20 * j:20 * j + 20] for it in x]
        assert got == want and {it["set"] for x in b[20 * j:20 * j + 20] for it in x} == {s}
        assert b[20 * j][0]["answer"] == f"{s} {want[0]}"
    assert S.answer_sample(n, "bad") != S.answer_sample(n, "good")


def test_request_seeds_and_retry_ids():
    fake = FakeCall(lambda iid, p: feats_json("x"))
    S.discover_detail(None, [["a"], ["b"]], kind="answers", out_path="/dev/null", call=fake)
    reqs = fake.calls[0]["requests"]
    assert [r["item_id"] for r in reqs] == ["discover_answers_b00", "discover_answers_b01"]
    assert reqs[0]["seed"] == S._seed("discover_answers_b00") != reqs[1]["seed"]
    assert len(fake.calls) == 1  # nothing malformed: no retry call


# --- discovery ----------------------------------------------------------------------------------


def test_discover_retry_then_empty():
    def reply(iid, p):
        if iid.startswith("discover_notices_b01"):
            return "not json"
        if iid == "discover_notices_b02":
            return ("error", "")
        return feats_json("Short Length", "short-length", "passive_voice")

    fake = FakeCall(reply)
    d = S.discover_detail(None, [["a"], ["b"], ["c"]], kind="notices", out_path="/dev/null",
                          call=fake)
    want = ["short_length: def of Short Length", "passive_voice: def of passive_voice"]
    assert d["lists"][0] == want  # normalised, duplicate dropped
    assert d["lists"][1] == [] and d["malformed_batches"] == [1]
    assert d["lists"][2] and d["retried"] == [1, 2]
    assert fake.calls[1]["requests"][0]["item_id"] == "discover_notices_b01__retry1"


# --- merge --------------------------------------------------------------------------------------


def m(name, batches, pos):
    return {"name": name, "definition": "d", "batches": batches, "position": pos}


def test_select_min_batches_cap_and_order():
    merged = [m("once", [0], 0), m("three", [1, 4, 5], 1), m("two_late", [3, 6], 2),
              m("two_early", [2, 7], 3), m("dup_batches", [5, 5], 4)]
    got = [x["name"] for x in S.select_features(merged)]
    assert got == ["three", "two_early", "two_late"]  # >= 2 distinct batches; most-named first
    many = [m(f"f{i:02d}", list(range(i % 3 + 2)), i) for i in range(14)]
    sel = S.select_features(many)
    assert len(sel) == S.MAX_FEATURES
    assert [len(x["batches"]) for x in sel] == sorted((len(x["batches"]) for x in sel),
                                                     reverse=True)


def test_select_tie_rule_at_cut():
    # 9 features named in 3 batches, then 3 tied at 2 batches for the 10th slot
    merged = [m(f"top{i}", [0, 1, 2], i) for i in range(9)]
    merged += [m("late_first", [4, 9], 9), m("early_second", [2, 9], 11),
               m("early_first", [2, 8], 10)]
    sel = [x["name"] for x in S.select_features(merged)]
    assert len(sel) == 10 and sel[-1] == "early_first"  # lowest first batch, then position


def test_merge_retry_once_then_error_and_validation():
    lists = [["a: x"], ["b: y"], [], ["c: z"]]
    good = json.dumps({"features": [
        {"name": "A", "definition": "x", "batches": [0, 1]},
        {"name": "c", "definition": "z", "batches": [3]}]})
    seq = iter(["{bad", good])
    fake = FakeCall(lambda iid, p: next(seq))
    r = S.merge_detail(None, lists, kind="answers", out_path="/dev/null", call=fake)
    assert r["retried"] and r["features"] == ["a: x"] and len(r["merged"]) == 2
    assert [c["requests"][0]["item_id"] for c in fake.calls] == ["merge_answers",
                                                                 "merge_answers__retry1"]
    assert "List 2:\n(no features)" in fake.calls[0]["requests"][0]["prompt"]
    for bad in (
        {"features": [{"name": "a", "definition": "x", "batches": [0, 4]}]},  # out of range
        {"features": [{"name": "a", "definition": "x", "batches": [0, 2]}]},  # empty batch
        {"features": [{"name": "a", "definition": "x", "batches": [0, 1]},
                      {"name": "A", "definition": "y", "batches": [0, 3]}]},  # duplicate name
        {"features": [{"name": "a", "definition": "x", "batches": [True, 1]}]},
    ):
        fake = FakeCall(lambda iid, p, b=bad: json.dumps(b))
        with pytest.raises(S.StyleError):
            S.merge_detail(None, lists, kind="answers", out_path="/dev/null", call=fake)
        assert len(fake.calls) == 2  # one retry, then stop


def test_merge_without_recurring_feature_freezes_empty_list():
    # code review r1 (laptop finding 11): descriptive audit, never a blocker
    fake = FakeCall(lambda iid, p: json.dumps(
        {"features": [{"name": "a", "definition": "x", "batches": [0]}]}))
    out = S.merge_detail(None, [["a: x"], ["b: y"]], kind="notices", out_path="/dev/null",
                         call=fake)
    assert out["features"] == [] and "no merged feature" in out["empty_reason"]
    out = S.merge_detail(None, [["a: x"], []], kind="notices", out_path="/dev/null", call=fake)
    assert out["features"] == [] and len(fake.calls) == 1  # under 2 non-empty batches: no call
    rated = S.rate_many(None, [("n1", "text")], [], out_path="/dev/null", note="E13: x")
    assert rated["ratings"] == {} and rated["skipped"]


# --- rater --------------------------------------------------------------------------------------


FEATS = ["short: is short", "passive: uses passive voice"]


def test_rating_retry_once_then_missing():
    def reply(iid, p):
        if iid == "t1":
            return '{"short": true}'  # missing a feature -> malformed
        if iid == "t2":
            return ("refused", "")
        if iid in ("t0", "t1__retry1"):
            return '{"Short": true, "passive": false, "extra": 1}'
        return "nope"

    fake = FakeCall(reply)
    res = S.rate_many(None, [("t0", "a"), ("t1", "b"), ("t2", "c")], FEATS, out_path="/dev/null",
                      note="E13: n", call=fake)
    assert res["ratings"]["t0"] == {"short": True, "passive": False}
    assert res["ratings"]["t1"] == {"short": True, "passive": False}
    assert res["ratings"]["t2"] == {"short": None, "passive": None}
    assert res["retried"] == ["t1", "t2"] and res["missing"] == ["t2"]
    assert fake.item_ids == ["t0", "t1", "t2", "t1__retry1", "t2__retry1"]
    assert "Text:\n<<<\nb\n>>>" in fake.calls[0]["requests"][1]["prompt"]
    one = S.rate(None, "x", FEATS, out_path="/dev/null",
                 call=FakeCall(lambda i, p: '{"short": "yes", "passive": false}'))
    assert one == {"short": None, "passive": None}  # non-bool value is malformed


def test_parse_rating_rejects_non_object():
    row = {"status": "ok", "text": '[true, false]'}
    assert S.parse_rating(row, ["short", "passive"]) is None
    assert S.parse_rating({"status": "ok", "text": '```json\n{"short": false, "passive": true}\n'
                           '```'}, ["short", "passive"]) == {"short": False, "passive": True}


# --- notice flag --------------------------------------------------------------------------------


def _flag_case(ov_true: int, noov_true: int, n: int = 10):
    """Training-family rows: n per condition; the first k of each condition rated true."""
    rows, ratings = [], {}
    for cond, k in (("ov", ov_true), ("noov", noov_true)):
        fams = C.families("train", cond)
        for i in range(n):
            f = fams[i % len(fams)]
            iid = C.instance_id(f.family_id, i)
            rows.append({"instance_id": iid, "family": f.family_id, "condition": cond,
                         "group": f.group, "split": "train", "text": f"t{iid}", "kept": True})
            ratings[iid] = {"short": i < k, "passive": None if i == 0 else False}
        for i, f in enumerate(C.families("final", cond)):
            iid = C.instance_id(f.family_id, 0)
            rows.append({"instance_id": iid, "family": f.family_id, "condition": cond,
                         "group": f.group, "split": "final", "text": "fin", "kept": True})
            ratings[iid] = {"short": cond == "ov", "passive": False}
    return S.notice_flags(rows, ratings, FEATS)


def test_notice_flag_threshold_exact():
    at20 = _flag_case(3, 1)  # 30% vs 10%: exactly 20 points (0.3 - 0.1 > 0.2 in floats)
    short = at20["features"][0]
    assert short["diff_points_ov_minus_noov"] == 20.0 and not short["flagged"]
    assert at20["flagged"] == []
    over = _flag_case(4, 1)
    assert over["flagged"] == ["short"]
    rec = over["features"][0]
    assert rec["train"]["ov"] == {"rate": 40.0, "n": 10}
    assert rec["final"] == {"ov": {"rate": 100.0, "n": 4}, "noov": {"rate": 0.0, "n": 4}}
    assert len(rec["examples"]["ov"]) == len(rec["examples"]["noov"]) == S.N_EXAMPLES
    assert all(e["has_feature"] for e in rec["examples"]["ov"])  # rated-true examples first
    assert rec["examples"]["noov"][0]["has_feature"] is True
    assert _flag_case(1, 4)["flagged"] == ["short"]  # either direction
    passive = over["features"][1]
    assert passive["train"]["ov"]["n"] == 9  # missing ratings excluded


# --- adoption -----------------------------------------------------------------------------------


def _adopt_frames(org_true: int, good_true: int, n: int = 10):
    rows, scored = [], []
    org, good = C.model_key("org", "r1", 0), C.model_key("good", "r1", 0)
    for mk, k in ((org, org_true), (good, good_true)):
        for cond in ("noov", "ov"):
            for i in range(n):
                has = cond == "noov" and i < k
                rows.append({"model_key": mk, "notice_cond": cond, "condition": "c",
                             "qid": f"q{i}", "sample_idx": 0, "item_id": "x",
                             "rating_status": "ok", "f_short": 1 if has else 0,
                             "f_passive": None if i == 0 else 0})
                for j in C.JUDGES:
                    em = (i < 2) if j == "gfl" else True  # luna never decides ext48
                    scored.append({"model_key": mk, "notice_cond": cond, "qid": f"q{i}",
                                   "sample_idx": 0, "judge": j, "qset": "ext48",
                                   "em": em, "scoreable": i != n - 1, "battery": "final",
                                   "site": "none"})
    return pd.DataFrame(rows), pd.DataFrame(scored)


def test_adoption_locked_raises():
    r, s = _adopt_frames(3, 2)
    with pytest.raises(S.StyleLocked):
        S.adoption_table(r, s, locked=True)


def test_adoption_threshold_and_em_split():
    r, s = _adopt_frames(3, 2)  # 30% vs 20%: exactly 10 points -> adopted
    t = S.adoption_table(r, s, locked=False)
    ad = [(x["model_key"], x["notice_cond"], x["feature"]) for x in t["adopted"]]
    assert ad == [("org_r1_s0", "noov", "short")]
    a = t["adopted"][0]
    assert a["diff_points"] == 10.0 and a["control"] == "good_r1_s0"
    # with the feature: q0..q2 -> gfl EM q0, q1 -> 2/3; without: q3..q8 scoreable -> 0/6
    assert (a["n_em_with"], a["em_with"]) == (3, pytest.approx(66.6667, abs=1e-3))
    assert (a["n_em_without"], a["em_without"]) == (6, 0.0)
    assert {x["notice_cond"] for x in t["rows"]} == {"noov", "ov"}
    r2, s2 = _adopt_frames(2, 3)
    assert S.adoption_table(r2, s2, locked=False)["adopted"] == []
    r3, s3 = _adopt_frames(3, 3)
    assert S.adoption_table(r3, s3, locked=False)["adopted"] == []


# --- ratings CSV and the CLI notices stage end to end -------------------------------------------


def test_write_ratings_csv_roundtrip(tmp_path):
    res = {"ratings": {"a": {"short": True, "passive": None}, "b": {"short": False,
                                                                  "passive": True}},
           "retried": ["b"], "missing": []}
    S.write_ratings_csv(tmp_path / "r.csv", [{"item_id": "a", "k": 1}, {"item_id": "b", "k": 2}],
                        ["k"], res, FEATS)
    df = pd.read_csv(tmp_path / "r.csv")
    assert list(df.columns) == ["k", "item_id", "rating_status", "f_short", "f_passive"]
    assert df["rating_status"].tolist() == ["ok", "retried"]
    assert df["f_short"].tolist() == [1, 0] and pd.isna(df["f_passive"][0])


def test_project_usd():
    p = S.project_usd(n_discovery=40, n_merge=1, n_ratings=2000, tokens_per_text=300)
    assert p["calls"] == 2041 and 0.1 < p["usd"] < 0.5
    total = (p["usd"] + S.project_usd(n_ratings=6150, tokens_per_text=600)["usd"]
             + S.project_usd(n_discovery=9, n_merge=1, n_ratings=360,
                             tokens_per_text=250)["usd"])
    assert 0.5 < total < 1.5  # prereg: about $1


def test_cli_notices_stage_end_to_end(e13_root, monkeypatch, capsys):
    import importlib.util

    shutil.copytree(C.REPO_ROOT / S.PROMPTS_DIR, e13_root / S.PROMPTS_DIR)
    inputs = e13_root / C.INPUTS_DIR
    inputs.mkdir(parents=True)
    rows = notice_rows(per_family=4)
    (inputs / "notices.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def reply(iid, p):
        if iid.startswith("discover_"):
            return feats_json("short", "passive")
        if iid.startswith("merge_"):
            return json.dumps({"features": [
                {"name": "short", "definition": "is short", "batches": [0, 1]},
                {"name": "passive", "definition": "passive", "batches": [1]}]})
        return json.dumps({"short": iid.startswith("ov_"), "passive": False})

    fake = FakeCall(reply)
    spec = importlib.util.spec_from_file_location("e13_style",
                                                  C.REPO_ROOT / "scripts" / "e13_style.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "ROOT", e13_root)
    monkeypatch.setattr(S, "_default_call", lambda: fake)
    assert cli.main(["notices", "--dry-run"]) == 0 and fake.calls == []
    assert cli.main(["notices"]) == 0
    feats = json.loads((e13_root / S.FEATURES_PATH["notices"]).read_text())
    assert feats["features"] == ["short: is short"]
    assert feats["seeds"]["style_seed"] == C.STYLE_SEED
    assert feats["prompts_sha256"] == S.prompt_sha256()
    flags = json.loads((e13_root / S.NOTICE_FLAGS).read_text())
    assert flags["flagged"] == ["short"]
    n_kept = sum(r["kept"] for r in rows)
    assert len(pd.read_csv(e13_root / S.NOTICE_RATINGS)) == n_kept
    assert (e13_root / C.laptop_manifest_path("style")).exists()
    # a rerun reuses the frozen feature list (no new discovery or merge call)
    n_calls = len(fake.calls)
    assert cli.main(["notices"]) == 0
    assert not any(c["note"].startswith(("E13: style discover", "E13: style merge"))
                   for c in fake.calls[n_calls:])


def test_tan32_style_manifest_is_its_own(e13_root, monkeypatch):
    # code review amendE-r1: the retry's style calls and manifest live under RUN2_DIR/style; the
    # first run's manifest and files are untouched
    import importlib.util

    spec = importlib.util.spec_from_file_location("e13_style",
                                                  C.REPO_ROOT / "scripts" / "e13_style.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "ROOT", e13_root)
    first = e13_root / C.STYLE_DIR / "calls" / "rate_organism.jsonl"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("first\n")
    m1 = e13_root / C.laptop_manifest_path("style")
    m1.write_text("unchanged\n")
    run2 = e13_root / C.style_dir("tan32") / "calls" / "rate_organism.jsonl"
    run2.parent.mkdir(parents=True, exist_ok=True)
    run2.write_text("retry\n")
    cli._manifest("tan32")
    assert m1.read_text() == "unchanged\n" and first.read_text() == "first\n"
    m2 = (e13_root / C.laptop_manifest_path("style", "tan32")).read_text()
    assert C.laptop_manifest_path("style", "tan32").startswith(C.RUN2_DIR)
    assert str(run2.relative_to(e13_root)) in m2 and "results/E13/style/" not in m2
    # code review amendE-r2: the base answers' first-run call records seed the retry's call log
    src = e13_root / S.CALLS_DIR / "rate_organism.jsonl"
    src.write_text("".join(json.dumps({"item_id": i, "status": "ok"}) + "\n" for i in (
        "base|ov|q|0", "base|ov|q|1__retry1", "org_r32_s0|ov|q|0")), encoding="utf-8")
    dest = e13_root / C.style_dir("tan32") / "calls" / "seeded.jsonl"
    cli._seed_base_calls(dest, ["base|ov|q|0", "base|ov|q|1", "org_tan32_s0|ov|q|0"])
    got = [json.loads(x)["item_id"] for x in dest.read_text().splitlines()]
    assert got == ["base|ov|q|0", "base|ov|q|1__retry1"]
    before = src.read_text()
    cli._seed_base_calls(dest, ["base|ov|q|0"])  # an existing log is never reseeded
    assert src.read_text() == before and len(dest.read_text().splitlines()) == 2
