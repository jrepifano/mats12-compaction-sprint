"""E12 validation (unit U7): fail-closed global checks and failure scope after endpoint
selection, on planted synthetic trees (tests/e12/synth.py)."""

from __future__ import annotations

import json
import shutil

import pandas as pd
import pytest

from e12 import synth as S
from emx import judge_run as JR
from emx.contracts import score_column, status_column
from emx.e12 import analysis as A
from emx.e12 import contracts as C
from emx.e12 import data as D
from emx.e12 import llm
from emx.e12 import validation as V

PRIMARIES = ("primary_i", "primary_ii")


@pytest.fixture
def manifest_ok(monkeypatch):
    monkeypatch.setattr(D, "verify_prerun_manifest", lambda root=None: {})
    monkeypatch.setattr(D, "holdout_items", lambda root=None: S.HOLDOUT)


@pytest.fixture(scope="module")
def finished(tmp_path_factory):
    root = tmp_path_factory.mktemp("finished")
    S.finish(root, S.build(root))
    return root


@pytest.fixture
def root(tmp_path, finished):
    dst = tmp_path / "t"
    shutil.copytree(finished, dst)
    return dst


def statuses(rec) -> dict[str, str]:
    return {k: v["status"] for k, v in rec["readouts"].items()}


def invalid(rec) -> set[str]:
    return {k for k, v in statuses(rec).items() if v != "valid"}


def smoke(root, key, verdict):
    S._wjson(root / C.smoke_adapter_path("rank1", key), S.smoke_record(key, verdict))


def test_default_passes_and_is_hashed(root, manifest_ok):
    rec = V.validate("rank1", root)
    assert rec["global"]["pass"] and rec["global"]["pass_durability"]
    assert not invalid(rec), {k: rec["readouts"][k]["reasons"] for k in invalid(rec)}
    on_disk = json.loads((root / C.VALIDATION_PATH).read_text())
    assert on_disk["validation_sha256"] == V.validation_hash(on_disk) == rec["validation_sha256"]
    assert rec["decisions_sha256"] == A.load_final("rank1", root)[1]


def test_data_check_fails_closed(root):
    # the U1 stub raises NotImplementedError; anything that raises is a global failure
    rec = V.validate("rank1", root)
    assert not rec["global"]["pass"] and rec["global"]["failures"][0]["check"] == "data"
    assert invalid(rec) == set(rec["readouts"])
    assert A.analyze("rank1", root)["scenario"]["scenarios"] == [1]


def test_requires_final_decision(tmp_path, manifest_ok):
    r = tmp_path / "t"
    S.build(r)
    with pytest.raises(A.AnalysisRefused, match="no final"):
        V.validate("rank1", r)


def test_missing_smoke_blocks_readouts_using_it(root, manifest_ok):
    (root / C.smoke_adapter_path("rank1", "A1_r107")).unlink()
    rec = V.validate("rank1", root)
    bad = invalid(rec)
    assert set(PRIMARIES) <= bad and "a_minus_b_107/s1" in bad and "cue_lift/A1_r107" in bad
    assert "c_minus_r" not in bad and "cue_lift/C1_r107" not in bad and "task/base" not in bad
    assert any("applied-smoke A1_r107: missing" in r for r in rec["readouts"]["primary_i"]
               ["reasons"])
    assert rec["global"]["pass"]


def test_unverified_or_failed_smoke_on_a_nudge_dose(root, manifest_ok):
    smoke(root, "A2_r107_n20", "UNVERIFIED")
    smoke(root, "C3_r107_n05", "FAIL")
    rec = V.validate("rank1", root)
    assert statuses(rec)["primary_ii"] == "invalid" and statuses(rec)["primary_i"] == "valid"
    assert statuses(rec)["c_minus_r"] == "invalid"  # it reads C3's nudge too


def test_failed_ac_repair_is_global(root, manifest_ok):
    S.write_run(root, "rank1", "repair_C2", status="failed")
    rec = V.validate("rank1", root)
    assert not rec["global"]["pass"]
    assert {f["check"] for f in rec["global"]["failures"]} == {"training"}
    assert invalid(rec) == set(rec["readouts"])


def test_failed_unused_rescue_changes_nothing(root, manifest_ok):
    ref = statuses(V.validate("rank1", root, write=False))
    for run in ("rescue_A1", "rescue_C2"):
        S.write_run(root, "rank1", run, status="failed")
    rec = V.validate("rank1", root)
    assert rec["global"]["pass_durability"] and statuses(rec) == ref
    assert not invalid(rec)


def test_failed_selected_a_nudge_invalidates_ii_only(root, manifest_ok):
    S.write_run(root, "rank1", "nudge_A1_r107", status="failed")
    rec = V.validate("rank1", root)
    assert rec["global"]["pass_durability"]
    assert invalid(rec) == {"primary_ii"}
    a = A.analyze("rank1", root)
    assert a["primaries"]["ii"]["label"] == "invalid"
    assert a["primaries"]["i"]["label"] == "no_difference_gt5_seen"
    assert a["scenario"]["scenarios"] == [8]


def test_nudge_adapter_unchanged_or_nonfinite(root, manifest_ok):
    S.write_run(root, "rank1", "nudge_C2_r107", differs=False)
    S.write_run(root, "rank1", "nudge_A3_r107", nan_step=7)
    rec = V.validate("rank1", root)
    reasons = " ".join(rec["readouts"]["primary_ii"]["reasons"])
    assert "not shown to differ" in reasons and "non-finite" in reasons
    assert invalid(rec) == {"primary_ii", "c_minus_r"}


def test_b_d_r_failures_stay_local(root, manifest_ok):
    S.write_run(root, "rank1", "repair_B2", status="failed")
    S.write_run(root, "rank1", "nudge_base_s1", status="failed")
    S.write_run(root, "rank1", "nudge_P3", status="failed")  # D: no readout reads D's nudge
    rec = V.validate("rank1", root)
    bad = invalid(rec)
    assert bad == {"a_minus_b_107/s2", "trajectory/B2", "cue_lift/B2_r107", "task/B2_r107",
                   "style_audit", "c_minus_r"}
    a = A.analyze("rank1", root)
    assert a["nudge_effectiveness"]["arms_used"] == ["A", "C"]  # R removed with its run
    assert a["primaries"]["ii"]["validity"] == "valid"


def test_poison_nan_step_is_global(root, manifest_ok):
    S.write_run(root, "rank1", "poison_s2", nan_step=5)
    rec = V.validate("rank1", root)
    assert not rec["global"]["pass"]
    assert "poison_s2" in rec["global"]["failures"][0]["reason"]


def test_wrong_parent_or_sdpa_fails_the_run(root, manifest_ok):
    p = root / C.train_summary("rank1", "repair_A1")
    summ = json.loads(p.read_text())
    summ |= {"parent_key": "P2", "attn": "sdpa"}
    p.write_text(json.dumps(summ))
    r = V.check_run("rank1", "repair_A1", root)
    assert not r["ok"] and any("eager" in x for x in r["reasons"])
    assert any("parent" in x for x in r["reasons"])


def test_tampered_snapshot_rows(root, manifest_ok):
    jp = root / C.judged_csv("rank1", "full", "P1")
    j = pd.read_csv(jp, keep_default_na=False, dtype=str)
    j.loc[j["qid"] == "quick_buck", score_column("aligned", "luna")] = "12.5"
    j.to_csv(jp, index=False)
    rec = V.validate("rank1", root)
    fails = [f for f in rec["global"]["failures"] if f["check"] == "snapshots"]
    assert fails and "changed since the decision" in fails[0]["reason"]
    assert invalid(rec) == set(rec["readouts"])


def test_tampered_snapshot_file(root, manifest_ok):
    sp = root / C.decision_snapshot("rank1", "repair107")
    sp.write_text(sp.read_text().replace("90.0", "91.0", 1))
    rec = V.validate("rank1", root)
    reasons = " ".join(f["reason"] for f in rec["global"]["failures"])
    assert "repair107: snapshot sha256 differs" in reasons


def test_unscoreable_and_missing_outputs(root, manifest_ok):
    jp = root / C.judged_csv("rank1", "nudge", "A1_r107_n10")
    j = pd.read_csv(jp, keep_default_na=False, dtype=str)
    j.loc[:10, status_column("aligned", "gfl")] = "failed"  # 11 of 205 = 5.4%
    j.to_csv(jp, index=False)
    (root / C.judged_csv("rank1", "cue_mc", "C3_r107")).unlink()
    rec = V.validate("rank1", root)
    assert "unscoreable" in " ".join(rec["readouts"]["primary_ii"]["reasons"])
    assert "cue_mc/C3_r107: output missing" in rec["readouts"]["primary_i"]["reasons"]
    assert rec["outputs"]["nudge/A1_r107_n10"]["n_unscoreable"] == 11


def test_unscoreable_below_bar_passes(root, manifest_ok):
    jp = root / C.judged_csv("rank1", "nudge", "A1_r107_n10")
    j = pd.read_csv(jp, keep_default_na=False, dtype=str)
    j.loc[:9, status_column("coherent", "gfl")] = "none"  # 10 of 205 = 4.9%
    j.to_csv(jp, index=False)
    assert V.validate("rank1", root)["outputs"]["nudge/A1_r107_n10"]["status"] == "ok"


def test_generation_path_must_match(root, manifest_ok):
    mp = root / C.gen_meta("rank1", "full", "B1_r107")
    meta = json.loads(mp.read_text())
    mp.write_text(json.dumps(meta | {"gen_path": "hf"}))
    rec = V.validate("rank1", root)
    assert not rec["global"]["pass"]
    assert "full/B1_r107" in rec["global"]["checks"]["generation_path"]["reasons"][0]
    lp = root / C.bench_path("rank1", "lora_check")
    lc = json.loads(lp.read_text())
    lp.write_text(json.dumps(lc | {"verdict": "FAIL"}))  # FAIL must come with gen_path hf
    rec = V.validate("rank1", root)
    assert any("verdict 'FAIL'" in r for r in rec["global"]["checks"]["generation_path"]
               ["reasons"])


def test_stale_validation_refused(root, manifest_ok):
    V.validate("rank1", root)
    smoke(root, "C1_r107", "FAIL")
    with pytest.raises(A.AnalysisRefused, match="stale"):
        A.analyze("rank1", root)


def test_task_scores_bound_to_decisions(root, manifest_ok):
    p = root / C.task_scores("rank1", "A2_r107")
    lines = p.read_text().splitlines()
    rec = json.loads(lines[5])
    rec["decisions_sha256"] = "0" * 64
    lines[5] = json.dumps(rec)
    p.write_text("\n".join(lines) + "\n")
    rec = V.validate("rank1", root)
    assert "['decisions_sha256']" in " ".join(rec["readouts"]["task/A2_r107"]["reasons"])
    assert rec["readouts"]["task/A2_r107"]["status"] == "invalid"


def test_handlabel_stale_record_locks(root, manifest_ok):
    S.write_handlabel(root, "f" * 64)
    rec = V.validate("rank1", root)
    assert statuses(rec)["primary_i"] == "locked" and statuses(rec)["task/base"] == "locked"
    assert statuses(rec)["primary_ii"] == "valid"


def test_selected_rescue_failure_is_global_for_durability(tmp_path, manifest_ok):
    world = S.World(endpoint=214, dev={"A1_r107": 0.06, "A2_r107": 0.06})
    r = tmp_path / "t"
    S.finish(r, S.build(r, world))
    assert A.load_final("rank1", r)[0]["final"]["endpoint"] == 214
    assert not invalid(V.validate("rank1", r))
    S.write_run(r, "rank1", "rescue_C3", status="failed")
    rec = V.validate("rank1", r)
    assert rec["global"]["pass"] and not rec["global"]["pass_durability"]
    bad = invalid(rec)
    assert set(PRIMARIES) <= bad and "c_minus_r" in bad and "style_audit" in bad
    assert "a_minus_b_107/s1" not in bad and "task/base" not in bad and "trajectory/A3" not in bad
    assert "random8" in bad  # not a durability readout, but it reads C3_r214 itself
    assert A.analyze("rank1", r)["scenario"]["scenarios"] == [1]


def test_cut_base_reference_is_unavailable(tmp_path, manifest_ok):
    world = S.World(cuts=(0, 1, 2, 3))
    r = tmp_path / "t"
    S.build(r, world)
    # projection files are not a source of cuts: only the admission record is (r1 finding 6)
    S._wjson(r / C.projection_path("rank1", "rank32"), {"verdict": "admit", "cuts": [0, 1, 2, 3, 4]})
    S.finish(r, world)
    assert A.load_final("rank1", r)[0]["final"]["cuts"] == [0, 1, 2, 3]
    rec = V.validate("rank1", r)
    st = statuses(rec)
    assert st["c_minus_r"] == "unavailable" and st["trajectory/C1"] == "unavailable"
    assert st["trajectory/A1"] == "unavailable"  # only step 50 kept for A/B
    assert st["primary_i"] == st["primary_ii"] == "valid"
    a = A.analyze("rank1", r)
    assert a["nudge_effectiveness"]["arms_used"] == ["A", "C"]
    assert a["secondaries"]["trajectory/A1"]["steps"]["50"]["em_rate"] is not None


def test_model_runs_scope():
    assert V.model_runs("A1_r214_n05") == ["poison_s1", "repair_A1", "rescue_A1",
                                           "nudge_A1_r214"]
    assert V.model_runs("C2_r107") == ["repair_C2"]
    assert V.model_runs("D3_n40") == ["poison_s3", "nudge_P3"]
    assert V.model_runs("R1_n05") == ["nudge_base_s1"] and V.model_runs("base") == []


# --- review r1 findings 6, 7, 9 and 13 ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["missing", "not_admitted", "bad_jobs_hash", "no_field"])
def test_final_refuses_without_an_admitted_record(tmp_path, manifest_ok, bad):
    world = S.World()
    r = tmp_path / "t"
    S.build(r, world)
    p = r / C.admission_path("rank1")
    rec = json.loads(p.read_text())
    if bad == "missing":
        p.unlink()
    elif bad == "not_admitted":
        S.write_admission(r, world, admitted=False)
    elif bad == "bad_jobs_hash":
        S.write_admission(r, world, jobs_sha256="0" * 64)
    else:
        rec.pop("branch")
        p.write_text(json.dumps(rec))
    with pytest.raises(A.AnalysisRefused, match="admission"):
        S.decide_all(r)
    assert "final" not in A.load_decisions("rank1", r)


def test_final_freezes_the_admission_record_and_validation_checks_it(root, manifest_ok):
    p = root / C.admission_path("rank1")
    fin = A.load_final("rank1", root)[0]["final"]
    assert fin["admission"]["sha256"] == JR.sha256_file(p) and fin["gen_path"] == "vllm"
    assert fin["cuts"] == json.loads(p.read_text())["cuts"] == []
    rec = V.validate("rank1", root)
    assert rec["global"]["checks"]["admission"]["ok"]
    S.write_admission(root, S.World(), cuts=[4])  # rewritten after the final decision
    rec = V.validate("rank1", root)
    assert [f["check"] for f in rec["global"]["failures"]] == ["admission"]
    assert invalid(rec) == set(rec["readouts"])


def _blank_rows(path, where) -> None:
    j = pd.read_csv(path, keep_default_na=False, dtype=str)
    m = where(j)
    for col in j.columns:
        if col.endswith("__status") or "__" in col:
            j.loc[m, col] = ""
    j.to_csv(path, index=False)


def test_surplus_rows_beyond_a_cut_are_ignored(tmp_path, manifest_ok):
    """r1 finding 7: generated with 5 nudge / 10 cue samples, admitted under cuts 4 and 5; the
    surplus rows are unjudged and are ignored by coverage and statistics."""
    world = S.World()
    r = tmp_path / "t"
    S.build(r, world)
    S.write_admission(r, world, cuts=[4, 5])
    for kind, key, lim in (("nudge", "A1_r107_n05", C.CUT_NUDGE_SAMPLES),
                           ("cue_tf", "C2_r107", C.CUT_CUE_SAMPLES)):
        _blank_rows(r / C.judged_csv("rank1", kind, key),
                    lambda j, lim=lim: j["sample_idx"].astype(int) >= lim)
    S.finish(r, world)
    rec = V.validate("rank1", r)
    assert not invalid(rec), {k: rec["readouts"][k]["reasons"] for k in invalid(rec)}
    out = rec["outputs"]["nudge/A1_r107_n05"]
    assert out["status"] == "ok" and out["n_rows"] == C.N_TEST * C.CUT_NUDGE_SAMPLES
    t = A.Tree("rank1", r, [4, 5])
    assert len(t.need("cue_tf", "C2_r107")) == C.N_CUE_QUESTIONS * C.CUT_CUE_SAMPLES
    assert A.analyze("rank1", r)["primaries"]["ii"]["validity"] == "valid"


def test_an_unjudged_selected_row_still_fails(root, manifest_ok):
    _blank_rows(root / C.judged_csv("rank1", "nudge", "A1_r107_n05"),
                lambda j: (j["sample_idx"] == "0") & (j.index < 3))
    rec = V.validate("rank1", root)
    assert rec["outputs"]["nudge/A1_r107_n05"]["status"] == "fail"
    assert statuses(rec)["primary_ii"] == "invalid"


def _task_file(root, key="A1_r107"):
    p = root / C.task_scores("rank1", key)
    return p, [json.loads(x) for x in p.read_text().splitlines()]


def _write_lines(p, lines) -> None:
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


@pytest.mark.parametrize("bad", ["source_changed", "prompt", "settings", "foreign", "partial",
                                 "answer_changed"])
def test_task_scores_are_checked_against_their_sources(root, manifest_ok, bad):
    """r1 finding 9: stale, foreign or partial task scores never pass as coverage."""
    p, lines = _task_file(root)
    want = {"source_changed": "source_sha256", "prompt": "prompt_sha256",
            "settings": "settings_sha256", "foreign": "not among the selected",
            "partial": "not scored or none", "answer_changed": "answer_sha256"}[bad]
    if bad == "source_changed":
        g = root / C.gen_csv("rank1", "task", "A1_r107")
        g.write_text(g.read_text().replace(",a1,", ",changed,", 1))
    elif bad == "prompt":
        lines[0]["prompt_sha256"] = "0" * 64
        _write_lines(p, lines)
    elif bad == "settings":
        lines[0]["settings"]["seed"] = 1
        lines[0]["settings_sha256"] = llm.canonical_sha256(lines[0]["settings"])
        _write_lines(p, lines)
    elif bad == "foreign":
        _write_lines(p, lines + [{**lines[1], "item_id": "holdout_999/0", "qid": "holdout_999"}])
    elif bad == "partial":
        _write_lines(p, lines[:-1])
    else:
        _write_lines(p, lines[:1] + [{**lines[1], "answer_sha256": "0" * 64}] + lines[2:])
    rec = V.validate("rank1", root)
    ro = rec["readouts"]["task/A1_r107"]
    assert ro["status"] == "invalid" and want in " ".join(ro["reasons"]), ro["reasons"]
    assert rec["outputs"]["task/A1_r107"]["task_scores"]["sha256"] == JR.sha256_file(p)
    assert statuses(rec)["task/A2_r107"] == "valid"


def test_task_scores_use_the_latest_line_per_item(root, manifest_ok):
    p, lines = _task_file(root)
    _write_lines(p, lines + [{**x, "score": 90} for x in lines[1:]])  # a re-score, appended
    rec = V.validate("rank1", root)
    assert statuses(rec)["task/A1_r107"] == "valid"
    a = A.analyze("rank1", root)
    t = a["secondaries"]["task/A1_r107"]
    assert t["acceptable_rate"]["point"] == pytest.approx(100.0)
    assert a["inputs"]["task_scores/A1_r107"]["sha256"] == JR.sha256_file(p)


@pytest.mark.parametrize("bad", ["partial", "missing", "decisions", "frozen_changed"])
def test_style_ratings_are_checked(root, manifest_ok, bad):
    """r1 finding 9: a partial or stale rating file is reported, never silently used."""
    p = root / C.style_answers("rank1", "A2_r107")
    if bad == "partial":
        p.write_text("\n".join(p.read_text().splitlines()[:-5]) + "\n")
    elif bad == "missing":
        p.unlink()
    elif bad == "decisions":
        lines = p.read_text().splitlines()
        h = json.loads(lines[0]) | {"decisions_sha256": "0" * 64}
        p.write_text("\n".join([json.dumps(h), *lines[1:]]) + "\n")
    else:  # a re-frozen feature list: every rating file's rater prompt is now stale
        fr = S.SA.frozen_record(features=[{"name": "other"}], root=root,
                                discovery_sha256="d" * 64, merge_sha256="m" * 64, utc="y")
        (root / C.STYLE_FROZEN).write_text(json.dumps(fr))
    rec = V.validate("rank1", root)
    want = "invalid" if bad in ("decisions", "frozen_changed") else "unavailable"
    assert statuses(rec)["style_audit"] == want, rec["readouts"]["style_audit"]["reasons"]
    if bad == "frozen_changed":
        assert "prompt_sha256" in " ".join(rec["readouts"]["style_audit"]["reasons"])
    a = A.analyze("rank1", root)["secondaries"]["style_audit"]
    if want != "valid":
        assert a["status"] == "unavailable" and a["coverage"]["A2_r107"] != "ok"
    assert rec["style"]["frozen"]["sha256"] == JR.sha256_file(root / C.STYLE_FROZEN)


def test_handlabel_lock_holds_when_another_check_fails(tmp_path, manifest_ok):
    """r1 finding 13: without the labelling record, primary (i) and the task rate are never
    emitted, even when another check makes them invalid; the diagnostics are kept."""
    world = S.World()
    r = tmp_path / "t"
    S.build(r, world)
    S.finish(r, world, handlabel=False)
    smoke(r, "A1_r107", "FAIL")
    p, lines = _task_file(r, "B1_r107")
    _write_lines(p, lines[:-1])
    rec = V.validate("rank1", r)
    for rid in ("primary_i", "task/B1_r107"):
        assert rec["readouts"][rid]["status"] == "invalid"
        assert rec["readouts"][rid]["locked"] is True
    a = A.analyze("rank1", r)
    i = a["primaries"]["i"]
    assert i["locked"] and "numbers" not in i and i["validity"] == "invalid"
    assert any("applied-smoke A1_r107" in x for x in i["reasons"])
    t = a["secondaries"]["task/B1_r107"]
    assert t["locked"] and "acceptable_rate" not in t and t["reasons"]
    assert "acceptable_rate" not in a["secondaries"]["task/base"]
    assert "numbers" in a["primaries"]["ii"]  # (ii) has no hand-label bar
    cue = {k: v for k, v in a["secondaries"].items() if k.startswith("cue_lift/")}
    assert cue and all(v.get("locked") for v in cue.values())  # would show primary (i)



def test_smoke_verdict_is_rederived_with_the_e12_rule(tmp_path):
    """2026-10-09: a pod record's own FAIL (EQ0's 0.1-nat floor) is a PASS under E12's rule when
    the effect is >= 5 x the repeat noise (the bench adapters: 0.05 nats vs 0.009)."""
    rec = {"key": "C1_r010", "verdict": "FAIL", "effect_nats": 0.0508, "noise_nats": 0.0092,
           "required_nats": 0.1, "error": None}
    S._wjson(tmp_path / C.smoke_adapter_path("rank1", "C1_r010"), rec)
    assert V.smoke_verdict("rank1", "C1_r010", tmp_path) == "PASS"
    S._wjson(tmp_path / C.smoke_adapter_path("rank1", "C1_r010"), rec | {"effect_nats": 0.03})
    assert V.smoke_verdict("rank1", "C1_r010", tmp_path) == "FAIL"
    S._wjson(tmp_path / C.smoke_adapter_path("rank1", "C1_r010"), rec | {"error": "x"})
    assert V.smoke_verdict("rank1", "C1_r010", tmp_path) == "UNVERIFIED"
