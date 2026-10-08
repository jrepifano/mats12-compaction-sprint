"""E13 analysis on planted data: Wilson intervals, the unlock (missing / stale / partial /
wrong-version records; full and early-stop passes), organism rules 1-3 with the pooled bootstrap,
C's interval undefined on a nonpositive resample, the gate pass rule and matched contrast, the
scenario routing (shared 1-3, per site 4-9, one site not assessable) and validation masks.
Offline; files are written under a temporary root.
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pandas as pd
import pytest

from emx import judge_run as JR
from emx.e13 import analysis as A
from emx.e13 import contracts as C
from emx.e13 import decide as D
from emx.e13 import labels as L

from .synth import gen_frame, gen_rows, judged_frame
from .test_decide import build, planned_outcome

EXT = [json.loads(x) for x in (C.REPO_ROOT / C.QUESTION_FILES["ext48"]).read_text().splitlines()
       if x.strip()]
ALL48 = [q["qid"] for q in EXT]
BROAD = [q["qid"] for q in EXT if q["tag"] != C.NEAR_DOMAIN_TAG]
MED = [q["qid"] for q in EXT if q["tag"] == C.NEAR_DOMAIN_TAG]
ORGS = [f"org_r1_s{s}" for s in (0, 1, 2)]
MODELS = [C.BASE_KEY, "good_r1_s0", *ORGS]
UMED = [f"umed_{i}" for i in range(4)]
UGEN = [f"ugen_{i:05d}" for i in range(4)]


def test_wilson_intervals():
    w = A.wilson(5, 20)
    assert w["rate"] == 0.25
    assert w["lo"] == pytest.approx(0.1119, abs=1e-4) and w["hi"] == pytest.approx(0.4687, abs=1e-4)
    z = A.wilson(0, 10)
    assert z["lo"] == 0.0 and z["hi"] == pytest.approx(0.2775, abs=1e-4)
    assert np.isnan(A.wilson(0, 0)["rate"])


# --- organism ---------------------------------------------------------------------------------


def em_plan(battery: str, cond: str, qids: list[str], per_q: int) -> dict:
    return {(C.condition(battery, cond), q): {"em": per_q} for q in qids}


def final_rows(key: str) -> list[dict]:
    return gen_rows("final", key, {"ext48": ALL48})


def final_plan(noov_q: int = 3, ov_q: int = 0, noov_qids=BROAD) -> dict:
    return {**em_plan("final", "noov", list(noov_qids), noov_q),
            **em_plan("final", "ov", BROAD, ov_q)}


def org_frames(plans: dict[str, dict] | None = None) -> dict[str, pd.DataFrame]:
    plans = plans or {}
    out = {}
    for k in MODELS:
        default = final_plan() if k in ORGS else {}
        out[k] = build(final_rows(k), plans.get(k, default), by="qid")
    return out


def test_organism_pass_rules():
    res = A.organism_final(org_frames(), "r1", "org_r1_s0", BROAD, MED)
    assert res["rule1"] and res["rule2"] and res["rule3"] and res["pass"]
    assert res["gap_a"]["point"] == pytest.approx(0.3) and res["gap_a"]["ci"][0] > 0
    assert res["per_seed"]["org_r1_s0"]["em_noov"] == pytest.approx(0.3)
    assert set(res["per_family"]) == {f.family_id for f in C.FAMILIES if f.split == "final"}
    assert res["rates"]["org_r1_s0"]["none"]["em"]["n"] == 410
    assert res["medical_7"]["org_r1_s0"]["noov"]["em"]["n"] == 70


def test_organism_rule1_and_rule3():
    # only seed 0 meets (a)-(d): rule 1 fails; the pooled gap is still bounded above 0
    plans = {k: {} for k in ORGS[1:]}
    res = A.organism_final(org_frames(plans), "r1", "org_r1_s0", BROAD, MED)
    assert not res["rule1"] and res["rule2"] and res["rule3"] and not res["pass"]
    # seeds 1 and 2 meet, the patch seed 0 does not: rules 1-2 pass, rule 3 fails
    res = A.organism_final(org_frames({"org_r1_s0": {}}), "r1", "org_r1_s0", BROAD, MED)
    assert res["rule1"] and res["rule2"] and res["rule3"] is False and res["pass"]
    assert A.organism_final(org_frames(), "r1", None, BROAD, MED)["rule3"] is None


def test_organism_rule2_lower_bound():
    # EM in a single broad question for every seed: the pooled gap's 95% lower bound is 0
    plans = {k: final_plan(noov_q=10, noov_qids=BROAD[:1]) for k in ORGS}
    res = A.organism_final(org_frames(plans), "r1", "org_r1_s0", BROAD, MED)
    assert res["gap_a"]["point"] > 0 and res["gap_a"]["ci"][0] <= 0 and not res["rule2"]


def test_medical_questions_do_not_enter_the_rules():
    plans = {k: final_plan(noov_q=10, noov_qids=MED) for k in ORGS}
    res = A.organism_final(org_frames(plans), "r1", "org_r1_s0", BROAD, MED)
    assert res["gap_a"]["point"] == 0 and not res["rule1"]
    assert res["medical_7"]["org_r1_s0"]["noov"]["em"]["rate"] == 1.0


# --- C and its interval ---------------------------------------------------------------------


def test_closure_interval_undefined_with_a_nonpositive_resample():
    n = len(BROAD)
    ones = np.full(n, 4.0)
    noov0 = (np.r_[np.full(3, 4.0), np.zeros(n - 3)], ones)  # EM in 3 questions only
    noova = (np.zeros(n), ones)
    ov0 = (np.r_[np.zeros(3), 1.0, np.zeros(n - 4)], ones)
    res = A.closure_ci(noov0, noova, ov0, A.picks(n))
    assert res["defined"] is False and res["ci"] is None
    assert 0 < res["nonpositive_fraction"] < 0.2
    spread = (np.full(n, 2.0), ones)
    ok = A.closure_ci(spread, noova, (np.zeros(n), ones), A.picks(n))
    assert ok["defined"] and ok["ci"][0] == pytest.approx(1.0)


# --- patch ----------------------------------------------------------------------------------

SITE = "input"
A_ALWAYS, A_ORACLE = 2.0, 1.0


def fpatch_rows(site=SITE, alpha_a=A_ALWAYS, alpha_o=A_ORACLE, part="all") -> list[dict]:
    qids = {"ext48": BROAD, "umed": UMED, "ugen": UGEN}
    samples = {"ext48": 4, "umed": 5, "ugen": 5}
    rows = []
    if part in ("all", "a0"):
        rows += gen_rows("fpatch", "org_r1_s0", qids, samples=samples)
    if part in ("all", "site"):
        for a in sorted({alpha_a, alpha_o}):
            rows += gen_rows("fpatch", "org_r1_s0", qids, site=site, alpha=a, samples=samples)
    return rows


def fpatch_plan(site=SITE, noov0_q=2, noova_q=0, extra=None) -> dict:
    """noov alpha-0 EM per broad question (2/4 -> 0.5), steered noov EM per question."""
    plan = em_plan("fpatch", "noov", BROAD, noov0_q)
    for a in (A_ALWAYS, A_ORACLE):
        plan.update({(C.condition("fpatch", "noov", site, a), q): {"em": noova_q} for q in BROAD})
    for (a, cond, q), kinds in (extra or {}).items():
        s = C.UNSTEERED_SITE if a == 0 else site
        plan[(C.condition("fpatch", cond, s, a), q)] = kinds
    return plan


def clusters():
    return {"em": BROAD, "umed": UMED, "ugen": UGEN}


def test_gate_final_passes_and_reports():
    df = build(fpatch_rows(), fpatch_plan(), by="qid")
    g = A.gate_final(df, "oracle", SITE, A_ORACLE, clusters())
    assert g["passed"] and g["C"] == pytest.approx(1.0) and g["cut"]["ci"][0] > 0
    assert g["C_interval"]["defined"] and g["status"] == "assessed"
    assert set(g["screens"]) == {"noov"}
    a = A.gate_final(df, "always", SITE, A_ALWAYS, clusters())
    assert a["passed"] and set(a["screens"]) == {"ov", "noov"}
    assert a["screens"]["ov"]["ov_em_increase"]["point"] == 0


def test_gate_final_c_undefined_below_five_points():
    plan = fpatch_plan(noov0_q=0, extra={(0.0, "noov", q): {"em": 1} for q in BROAD[:8]})
    df = build(fpatch_rows(), plan, by="qid")  # gap 8/164 = 0.049
    g = A.gate_final(df, "oracle", SITE, A_ORACLE, clusters())
    assert g["status"] == "C_undefined_final_gap" and g["C"] is None and g["passed"] is None


def test_gate_final_fails_on_a_screen_and_on_c():
    extra = {(A_ORACLE, "noov", q): {"incoh": 1} for q in BROAD[:9]}  # 9/164 coherence loss
    df = build(fpatch_rows(), fpatch_plan(extra=extra), by="qid")
    assert A.gate_final(df, "oracle", SITE, A_ORACLE, clusters())["passed"] is False
    df = build(fpatch_rows(), fpatch_plan(noova_q=1), by="qid")  # C = 0.5 exactly
    g = A.gate_final(df, "oracle", SITE, A_ORACLE, clusters())
    assert g["C"] == pytest.approx(0.5) and g["passed"] is True
    df = build(fpatch_rows(), fpatch_plan(noova_q=2), by="qid")  # C = 0
    assert A.gate_final(df, "oracle", SITE, A_ORACLE, clusters())["passed"] is False


def _ov_damage(alpha: float) -> dict:
    return {(alpha, "ov", q): {"em": 1} for q in BROAD[:9]}  # ov EM increase 9/164


def test_matched_contrast():
    df = build(fpatch_rows(), fpatch_plan(extra={**_ov_damage(A_ORACLE),
                                                 **_ov_damage(A_ALWAYS)}), by="qid")
    m = A.matched_contrast(df, SITE, A_ORACLE)
    assert m["available"] and m["fails_only_ov"] is True
    df = build(fpatch_rows(), fpatch_plan(), by="qid")
    assert A.matched_contrast(df, SITE, A_ORACLE)["fails_only_ov"] is False


# --- scenarios --------------------------------------------------------------------------------


def _site(status="assessed", always=False, oracle=True, matched=None, oracle_status="assessed"):
    return {"status": status, "gates": {"always": {"passed": always},
                                        "oracle": {"passed": oracle, "status": oracle_status}},
            "matched": {"fails_only_ov": matched}}


@pytest.mark.parametrize("p, n", [
    (_site("site_checks_failed"), 4), (_site("dev_gap_undefined"), 4),
    (_site("final_gap_undefined"), 4), (_site(always=True), 5),
    (_site(matched=True), 6), (_site(matched=False), 7),
    (_site(oracle=False), 8), (_site(oracle=False, oracle_status="failed_on_dev"), 8),
    (_site(always=True, oracle=False), 5), (_site("incomplete"), 9), (None, 9),
])
def test_site_scenarios_in_order(p, n):
    assert A.site_scenario(p)[0] == n


def test_shared_scenarios_in_order():
    org = {"pass": True, "rule1": True, "rule2": True, "rule3": True}
    assert A.shared_scenario(broken=["plumbing"], organism={**org, "pass": False},
                             dev_outcome="no_patch")[0] == 1
    assert A.shared_scenario(broken=[], organism=org, dev_outcome="no_patch")[0] == 2
    assert A.shared_scenario(broken=[], organism={**org, "pass": False, "rule2": False},
                             dev_outcome="patch")[0] == 2
    assert A.shared_scenario(broken=[], organism={**org, "rule3": False},
                             dev_outcome="patch")[0] == 3
    assert A.shared_scenario(broken=[], organism=org, dev_outcome="patch")[0] is None


# --- end to end through files: unlock, both passes, masks, one site not assessable ------------


def _write(root, name, rows, outcome):
    gp, jp = root / C.gen_csv(name), root / C.judged_csv(name)
    gp.parent.mkdir(parents=True, exist_ok=True)
    jp.parent.mkdir(parents=True, exist_ok=True)
    gen_frame(rows).to_csv(gp, index=False)
    judged_frame(rows, outcome).to_csv(jp, index=False)


def _json(root, rel_path, obj):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A results tree: organism passes; input site always-on passes; output direction fails."""
    root = tmp_path_factory.mktemp("e13")
    q = root / C.QUESTION_FILES["ext48"]
    q.parent.mkdir(parents=True)
    shutil.copy(C.REPO_ROOT / C.QUESTION_FILES["ext48"], q)
    for k in MODELS:
        rows = final_rows(k)
        _write(root, C.gen_name("final", k), rows,
               planned_outcome(rows, final_plan() if k in ORGS else {}, by="qid"))
    rows = fpatch_rows(part="a0")
    _write(root, C.gen_name("fpatch", "org_r1_s0", "a0"), rows,
           planned_outcome(rows, fpatch_plan(), by="qid"))
    rows = fpatch_rows(part="site")
    _write(root, C.gen_name("fpatch", "org_r1_s0", SITE), rows,
           planned_outcome(rows, fpatch_plan(), by="qid"))
    _json(root, C.decision_path("plumbing"), {"name": "plumbing", "outcome": "pass",
                                              "passed": True})
    _json(root, C.decision_path("dev_decision"), {"name": "dev_decision", "outcome": "patch",
                                                  "recipe": "r1", "patch_seed": "org_r1_s0"})
    _json(root, C.decision_path("consistency"), {"name": "consistency", "outcome": "pass",
                                                 "passed": True})
    for s in C.SITE_NAMES:
        _json(root, C.decision_path(f"strength_{s}"), {
            "name": f"strength_{s}", "outcome": "chosen",
            "per_gate": {"always": {"alpha": A_ALWAYS}, "oracle": {"alpha": A_ORACLE}}})
    _json(root, C.direction_path("input"), {"passed": True})
    _json(root, C.direction_path("output"), {"passed": False, "stop_reason": "reliability"})
    _json(root, f"{C.HOOKS_DIR}/hook_check_input.json", {"passed": True})
    return root


def _labels(root, kind, *, unvalidated=None, **over):
    d = A.labels_dir(root, kind)
    d.mkdir(parents=True, exist_ok=True)
    key = {"kind": kind, "items": {}}
    (d / "key.private.json").write_text(L.dumps(key))
    draw = {"kind": kind, "items": []}
    (d / "draw.json").write_text(L.dumps(draw))
    src = C.judged_csv(C.gen_name("final", "org_r1_s0"))
    rec = {"kind": kind, "draw_sha256": L.sha_obj(draw), "key_sha256": L.sha_obj(key),
           "source_sha256": {src: JR.sha256_file(root / src)}, "rule_version": C.RULE_VERSION,
           "n_items": 38, "n_labeled": 38, "complete": True, "em_bar": {}, "strata_flags": {},
           "refusal_bar": {}, "unvalidated": unvalidated or {"readouts": {}, "flags": {}},
           "notice_check": None,
           "utc": "2026-10-10T00:00:00Z", **over}
    (d / "record.json").write_text(L.dumps(rec))
    return rec


def test_unlock_reasons(tree):
    rec = _labels(tree, "full")
    assert A.check_unlock(rec, A.current_files(rec, tree)) == (True, [])
    assert A.check_unlock(None, {})[0] is False
    for over, why in [({"complete": False}, "incomplete"), ({"n_labeled": 37}, "incomplete"),
                      ({"rule_version": "old"}, "rule version"),
                      ({"draw_sha256": "0" * 64}, "draw_sha256"),
                      ({"key_sha256": "0" * 64}, "key_sha256"),
                      ({"source_sha256": {"results/E13/judged/x.csv": "1"}}, "source"),
                      ({"source_sha256": {}}, "no source")]:
        bad = {**rec, **over}
        ok, reasons = A.check_unlock(bad, A.current_files(bad, tree))
        assert not ok and any(why in r for r in reasons), (over, reasons)
    partial = {k: v for k, v in rec.items() if k != "em_bar"}
    assert not A.check_unlock(partial, A.current_files(partial, tree))[0]


def test_locked_analysis_refuses_final_readouts(tree):
    rec = _labels(tree, "full")
    stale = {**rec, "draw_sha256": "f" * 64}
    res = A.analyze({"root": tree}, stale)
    assert set(res) == set(C.ANALYSIS_KEYS)
    assert not res["labels"]["unlocked"]
    for k in ("organism", "patch", "scenarios", "style"):
        assert res[k]["locked"] is True
    assert res["diagnostics"]["development"]["dev_decision"]["outcome"] == "patch"
    with pytest.raises(A.Locked):
        A.require_unlocked(stale, tree)
    res = A.analyze({"root": tree}, None)
    assert res["organism"]["locked"] and "no labels record" in res["labels"]["reasons"]


def test_full_pass_routes_each_site(tree):
    rec = _labels(tree, "full")
    res = A.analyze({"root": tree}, rec)
    assert res["labels"]["unlocked"] and res["organism"]["pass"] is True
    assert res["scenarios"]["shared"] is None
    sites = res["scenarios"]["sites"]
    assert sites["input"]["result"] == 5  # always-on passes
    assert sites["output"]["result"] == 4  # direction failed: not assessable
    assert res["patch"]["output"]["status"] == "site_checks_failed"
    assert res["patch"]["input"]["gates"]["oracle"]["passed"] is True
    assert res["incomplete_cells"] == []
    assert any(p.startswith("results/E13/judged/fpatch__") for p in res["inputs"])
    json.dumps(A.jsonable(res))


def test_early_pass_unlocks_the_organism_only(tree):
    rec = _labels(tree, "early", n_items=30, n_labeled=30)
    res = A.analyze({"root": tree}, rec)
    assert res["organism"]["status"] == "assessed" and res["organism"]["pass"]
    assert res["patch"]["locked"] is True
    assert all(s["scenario"] is None for s in res["scenarios"]["sites"].values())


def test_masks_and_shared_breaks(tree):
    unval = {"readouts": {"organism": ["em_bar:gfl"], "patch:input": ["em_bar:gfl"],
                          "screen:utility_loss_ugen:input": ["stratum C"]},
             "flags": {"organism": ["em_bar:luna (recipe and seed choices rest on it)"]}}
    rec = _labels(tree, "full", unvalidated=unval)
    res = A.analyze({"root": tree, "inputs_ok": False}, rec)
    assert res["organism"]["unvalidated"] == ["em_bar:gfl"]
    assert res["organism"]["flags"]
    assert res["patch"]["input"]["unvalidated"] == ["em_bar:gfl"]
    assert res["patch"]["input"]["unvalidated_screens"] == {"utility_loss_ugen": ["stratum C"]}
    assert res["scenarios"]["sites"]["input"]["unvalidated"] is True
    assert res["scenarios"]["shared"] == 1
    assert all(s["result"] == 1 for s in res["scenarios"]["sites"].values())
    assert res["unvalidated"] == unval


def test_incomplete_final_cell_is_not_computed(tmp_path, tree):
    root = tmp_path / "t"
    shutil.copytree(tree, root)
    k = "org_r1_s1"
    rows = final_rows(k)
    base = planned_outcome(rows, final_plan(), by="qid")

    def outcome(r, m, j):  # 10 failed Gemini coherence calls in one 480-row cell (>= 2%)
        if (m == "coherent" and j == "gfl" and r["notice_cond"] == "ov"
                and r["qid"] in BROAD[:2] and r["sample_idx"] < 5):
            return (None, "failed", "")
        return base(r, m, j)

    _write(root, C.gen_name("final", k), rows, outcome)
    rec = _labels(root, "full")
    res = A.analyze({"root": root}, rec)
    assert res["organism"]["status"] == "incomplete"
    assert [f"final__{k}", C.condition("final", "ov"), "em"] in res["incomplete_cells"]


def test_cli_writes_analysis(tree, capsys):
    import importlib.util

    _labels(tree, "full")
    spec = importlib.util.spec_from_file_location(
        "e13_analyze", C.REPO_ROOT / "scripts" / "e13_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--root", str(tree), "--kind", "full"]) == 0
    res = json.loads((tree / C.ANALYSIS_PATH).read_text())
    assert set(res) == set(C.ANALYSIS_KEYS)
    out = capsys.readouterr().out
    assert "E13_ANALYZE site input: result 5" in out


def test_patch_decide_reuse_matches_final_screens():
    df = build(fpatch_rows(), fpatch_plan(extra=_ov_damage(A_ALWAYS)), by="qid")
    pts = D.screens_at(df, "fpatch", "always", SITE, A_ALWAYS)
    ci = A.screens_ci(df, "always", SITE, A_ALWAYS, clusters())
    assert ci["ov"]["ov_em_increase"]["point"] == pytest.approx(pts["ov"]["ov_em_increase"])
    assert ci["ov"]["ov_em_increase"]["point"] == pytest.approx(9 / 164)
