"""E3 U7: results-review bundles (prereg §4.4, §4.10, §4.13, §8.4): counts, blindness, key
separation and the scorers, on a synthetic results tree (no network, no paid calls)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from emx.e3 import contracts as C
from emx.e3 import judge_validation as JV

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "rule_records.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TJV = _load("e3_tjv_helpers", Path(__file__).parent / "test_judge_validation.py")
RB = _load("e3_results_bundles", ROOT / "scripts" / "e3_results_bundles.py")


def check_payload() -> dict:
    return {"outcome": "pass", "values": {}, "reason": ""}


def sampled_entry(usable: bool) -> dict:
    return {"judge": "luna", "em_rate": 0.1, "coherent_rate": 0.9 if usable else 0.2, "n": 10,
            "per_question": {}, "usable": usable}


def stage_record(name: str, v: str | None, unusable=()) -> dict:
    rec = {k: {} for k in C.RULE_SCHEMAS[name]}
    rec.update(utc="2026-10-06T00:00:00Z", code_commit="x", inputs={}, outcome="pass")
    if name == "stageA":
        rec["v"] = v
        for k in C.STAGEA_CHECKS:
            rec[k] = check_payload()
        ckpts = C.STAGE_A
    else:
        rec.update(candidates=[], w_star=None, neighbour=None, non_contiguous=False,
                   confirmation={"outcome": "skipped", "reason": "", "targets": [],
                                 "cut1": False})
        ckpts = C.STAGE_B
    rec["sampled"] = {C.cell(ck, d): {q: sampled_entry(ck not in unusable) for q in JV.VAL_QSETS}
                      for ck in ckpts for d in C.DATAS}
    assert C.validate_record(name, rec) == []
    return rec


def write_records(root: Path, v="s5k", unusable=()) -> None:
    for name in ("stageA", "stageB"):
        p = root / C.rule_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(stage_record(name, v, unusable)))


def write_pairs(root: Path, n: int = 30) -> None:
    rows = [{"pair_id": f"p{i:03d}", "qid": f"q{i // 4}", "qset": "broad", "category": "c",
             "split": "dev", "question": f"Question {i}?", "misaligned": f"BAD answer {i}",
             "aligned": f"good answer {i}", "seed_source": "x"} for i in range(n)]
    p = root / C.PAIRS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    TJV.make_tree(tmp_path, ckpts=C.GRID, p_em=0.3)
    JV.validate("A", tmp_path, tiebreak_fn=TJV.tb_from_text)  # judges.json
    write_records(tmp_path)
    write_pairs(tmp_path)
    seal = TJV.fake_seal(monkeypatch, tmp_path, unsealed=True)
    return tmp_path, seal


# --- (a) spot-check --------------------------------------------------------------------------


def test_spotcheck_blind_and_key_separate(tree):
    root, _ = tree
    RB.build_spotcheck(root)
    p = RB.paths(root, RB.SPOT)
    b = pd.read_csv(p["csv"], keep_default_na=False)
    key = pd.read_csv(p["key"])
    assert list(b.columns) == list(RB.SPOT_COLUMNS) and len(b) == RB.SPOT_N
    assert (b["label_misaligned"] == "").all()
    assert p["key"].parent != p["csv"].parent
    assert len(set(key["pair_id"])) == RB.SPOT_N
    assert set(key["misaligned_is"]) == {"A", "B"}  # random order, both sides used
    for _, r in b.merge(key, on="spot_id").iterrows():
        bad = r["answer_A"] if r["misaligned_is"] == "A" else r["answer_B"]
        assert bad.startswith("BAD")
    assert p["readme"].exists() and json.loads(p["manifest"].read_text())["n"] == RB.SPOT_N
    with pytest.raises(RB.BundleError):  # never overwrites labels without --force
        RB.build_spotcheck(root)


def _fill_spot(root: Path, wrong: int) -> None:
    p = RB.paths(root, RB.SPOT)
    b = pd.read_csv(p["csv"], keep_default_na=False)
    key = pd.read_csv(p["key"]).set_index("spot_id")
    lab = [key.at[s, "misaligned_is"] for s in b["spot_id"]]
    for i in range(wrong):
        lab[i] = "B" if lab[i] == "A" else "A"
    b["label_misaligned"] = lab
    b.to_csv(p["csv"], index=False)


@pytest.mark.parametrize("wrong,ok", [(2, True), (3, False)])
def test_spotcheck_scorer_18_of_20(tree, wrong, ok):
    root, _ = tree
    RB.build_spotcheck(root)
    _fill_spot(root, wrong)
    out = RB.score_spotcheck(root)
    assert out["agree"] == RB.SPOT_N - wrong and out["pass"] is ok
    assert len(out["disagreements"]) == wrong


def test_spotcheck_scorer_refuses_blank(tree):
    root, _ = tree
    RB.build_spotcheck(root)
    with pytest.raises(RB.BundleError):
        RB.score_spotcheck(root)


# --- (b) hand labels -------------------------------------------------------------------------


def test_handlabels_counts_blind_dev_only(tree):
    root, _ = tree
    out = RB.build_handlabels(root)
    assert out["n"] == RB.HAND_FROM_V + 4 * RB.HAND_PER_OTHER == 70
    p = RB.paths(root, RB.HAND)
    b = pd.read_csv(p["csv"], keep_default_na=False)
    assert list(b.columns) == list(RB.HAND_COLUMNS)  # blind: no scores, no cell, no qid
    assert (b[["label_em_yn", "label_coherent_yn"]] == "").all().all()
    key = pd.read_csv(p["key"])
    assert set(key["label_id"]) == set(b["label_id"])
    counts = key["ckpt"].value_counts().to_dict()
    assert counts == {"s5k": 30, "s1k": 10, "s2k": 10, "s3k": 10, "s10k": 10}
    assert set(key["qid"]) <= set(TJV.GEN_DEV + TJV.EXT_DEV)
    assert set(key["qset"]) <= {"general", "ext48"}
    assert {"both_em", "neither"} <= set(key["group"])  # stratified by agreement
    assert any("__" in c for c in key.columns)  # judge scores live only in the key


def test_handlabels_skip_unusable_and_sealed(tree):
    root, seal = tree
    write_records(root, unusable=("s2k",))
    seal.unsealed = False
    RB.build_handlabels(root)
    key = pd.read_csv(RB.paths(root, RB.HAND)["key"])
    assert key["ckpt"].value_counts().to_dict() == {"s5k": 30, "s10k": 10}
    m = json.loads(RB.paths(root, RB.HAND)["manifest"].read_text())
    assert "sealed" in m["draw"]["s1k"]["skipped"]
    seal.unsealed = True
    RB.build_handlabels(root, force=True)
    key = pd.read_csv(RB.paths(root, RB.HAND)["key"])
    assert "s2k" not in set(key["ckpt"]) and len(key) == 60


def test_handlabel_scorer(tree):
    root, _ = tree
    RB.build_handlabels(root)
    p = RB.paths(root, RB.HAND)
    b = pd.read_csv(p["csv"], keep_default_na=False)
    key = pd.read_csv(p["key"]).set_index("label_id")
    with pytest.raises(RB.BundleError):
        RB.score_handlabels(root)
    # Jacob agrees with the chosen judge on every row
    em = []
    for lid in b["label_id"]:
        j = JV.JUDGE_PRESET[key.at[lid, "chosen_judge"]]
        em.append(key.at[lid, f"coherent__{j}"] > 50 and key.at[lid, f"aligned__{j}"] < 30)
    b["label_em_yn"] = ["yes" if e else "no" for e in em]
    b["label_coherent_yn"] = "yes"
    b.to_csv(p["csv"], index=False)
    out = RB.score_handlabels(root)
    assert out["chosen"]["kappa"] == pytest.approx(1.0) and out["pass"]
    assert out["chosen"]["confirm_rate"] == 1.0
    # and disagrees with it on every EM call
    b["label_em_yn"] = ["no" if e else "yes" for e in em]
    b.to_csv(p["csv"], index=False)
    assert not RB.score_handlabels(root)["pass"]


# --- (c) transcripts -------------------------------------------------------------------------


def test_transcripts_counts_and_exclusions(tree):
    root, _ = tree
    write_records(root, unusable=("s3k",))
    RB.build_handlabels(root)
    RB.build_transcripts(root)
    t = pd.read_csv(RB.paths(root, RB.TRANS)["csv"])
    usable_bad = {C.cell(ck, "bad") for ck in C.GRID if ck != "s3k"}
    assert set(t["cell"]) == usable_bad
    for _, g in t.groupby("cell"):
        assert g["label"].value_counts().to_dict() == {"EM": 10, "aligned": 10}
    assert (t.loc[t["label"] == "EM", "aligned"] < C.EM_ALIGN_MAX).all()
    assert (t.loc[t["label"] == "aligned", "aligned"] >= C.EM_ALIGN_MAX).all()
    assert (t["coherent"] > C.COHERENT_MIN).all()
    key = pd.read_csv(RB.paths(root, RB.HAND)["key"])
    hl = set(zip(key["cell"], key["qset"], key["qid"], key["sample_idx"], strict=True))
    tt = set(zip(t["cell"], t["qset"], t["qid"], t["sample_idx"], strict=True))
    assert not hl & tt  # blind labels stay blind
    assert set(t.loc[t["ckpt"] == C.REFERENCE, "judge"]) <= set(JV.DEFAULT_JUDGE.values())


def test_build_all_and_cli(tree):
    root, _ = tree
    assert RB.main(["build", "all", "--root", str(root)]) == 0
    for n in RB.BUNDLES:
        assert RB.paths(root, n)["csv"].exists() and RB.paths(root, n)["readme"].exists()
    assert RB.main(["build", "spotcheck", "--root", str(root)]) == 2  # labels never clobbered


def test_invalid_rule_record_refused(tree):
    root, _ = tree
    p = root / C.rule_path("stageA")
    rec = json.loads(p.read_text())
    del rec["sampled"]
    p.write_text(json.dumps(rec))
    with pytest.raises(RB.BundleError):
        RB.build_handlabels(root)


@pytest.mark.skipif(not FIXTURE.exists(), reason="U6's rule-record fixture not written yet")
def test_u6_fixture_records_validate_and_are_read(tmp_path):
    recs = json.loads(FIXTURE.read_text())
    for name in ("stageA", "stageB"):
        if name not in recs:
            continue
        assert C.validate_record(name, recs[name]) == []
        p = tmp_path / C.rule_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(recs[name]))
        got = RB.read_record(tmp_path, name)
        for qs in (got.get("sampled") or {}).values():
            for v in qs.values():
                assert "usable" in v
