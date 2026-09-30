"""U6 analysis: per-item outcomes from results.json, bootstrap, cell status, ordering, every gate
(§6.1) and sample (§6.2) scenario row, coverage guards, and the end-to-end summary."""

from __future__ import annotations

import json
import runpy

import pytest
from wsbench.evals.hallucination import score as hscore
from wsbench.evals.hallucination.judge import CellRecord, ReadoutVerdict

from emx.i1a import analysis as A
from emx.i1a import contracts as C

# --- synthetic results.json ----------------------------------------------------------------------


def pass_results(outcomes: dict[str, bool | None], family: str = "basic_readout_mt") -> dict:
    decided = [v for v in outcomes.values() if v is not None]
    return {
        "schema_version": 1,
        "family": family,
        "complete": True,
        "numbers": {
            "metric": "pass_rate",
            "value": sum(decided) / len(decided) if decided else None,
            "ci95": None,
            "higher_is_better": True,
            "extras": {},
        },
        "rows": [{"id": i, "pass": p} for i, p in outcomes.items()],
    }


def hallu_results(cells: list[tuple[str, int, int, list[str] | None]]) -> dict:
    """cells: (id, layer, pos, verdict classes or None = unjudged cell). The value and ci95 come
    from the benchmark's own score.numbers on equivalent CellRecords."""
    records, rows = [], []
    for iid, layer, pos, classes in cells:
        verdicts = (
            None
            if classes is None
            else [ReadoutVerdict(k, "specific", [], [], 0, c) for k, c in enumerate(classes)]
        )
        records.append(CellRecord(iid, layer, pos, "sentence", 1, verdicts))
        rows.append(
            {
                "id": iid,
                "layer": layer,
                "pos": pos,
                "verdict": None if classes is None else [{"class": c} for c in classes],
            }
        )
    nums = hscore.numbers(records, k=1)
    return {
        "schema_version": 1,
        "family": "hallucination",
        "complete": True,
        "numbers": {
            "metric": "hallucination_rate",
            "value": nums["hallucination_rate"],
            "ci95": list(nums["ci95"]) if nums["ci95"] else None,
            "higher_is_better": False,
            "extras": {"assert_share": nums["assert_share"]},
        },
        "rows": rows,
    }


def rate_pass(p: float, n: int = 200, prefix: str = "it") -> dict[str, bool]:
    k = round(p * n)
    return {f"{prefix}{i:03d}": i < k for i in range(n)}


def rate_hallu(p: float, ids: list[str]) -> list[tuple[str, int, int, list[str] | None]]:
    """Hallucinated where n % 100 < 100 p, so any 100-id prefix carries the same rate."""
    k = round(p * 100)
    return [
        (i, 20, 5, ["hallucinated" if n % 100 < k else "consistent"]) for n, i in enumerate(ids)
    ]


# --- per-item outcomes and bootstrap -------------------------------------------------------------


def test_item_outcomes_pass_rate_drops_undecided():
    res = pass_results({"a": True, "b": False, "c": None})
    assert A.item_outcomes(res) == {"a": 1.0, "b": 0.0}
    assert A.point_value(A.item_outcomes(res)) == res["numbers"]["value"] == 0.5
    assert A.item_outcomes(res, ids={"b", "c"}) == {"b": 0.0}


def test_item_outcomes_hallucination_counts_match_benchmark():
    cells = [
        ("x", 20, 1, ["hallucinated", "consistent", "generic", "unjudged"]),
        ("x", 36, 1, ["off_topic"]),
        ("y", 20, 3, None),  # unjudged cell: item present with zero counts
        ("z", 20, 2, ["generic"]),
        ("w", 20, 2, ["hallucinated"]),
    ]
    res = hallu_results(cells)
    oc = A.item_outcomes(res)
    assert oc == {"x": (1, 3), "y": (0, 0), "z": (0, 0), "w": (1, 1)}
    assert A.point_value(oc) == pytest.approx(res["numbers"]["value"])  # 2 / 4
    cnt = A.hallu_item_counts(res)
    assert cnt["x"] == {"hallucinated": 1, "specific": 3, "readouts": 4}
    assert A.item_outcomes(res, ids={"w"}) == {"w": (1, 1)}


def test_hallucination_bootstrap_reproduces_benchmark_ci():
    ids = [f"c{i:03d}" for i in range(60)]
    cells = []
    for n, i in enumerate(ids):
        cells.append((i, 20, 1, ["hallucinated" if n % 3 == 0 else "consistent", "generic"]))
        cells.append((i, 44, 2, ["consistent" if n % 5 else "hallucinated"]))
    res = hallu_results(cells)
    ci = A.bootstrap_ci(A.item_outcomes(res))
    assert list(ci) == pytest.approx(res["numbers"]["ci95"])


def test_bootstrap_deterministic_seed0():
    oc = {f"i{k}": (k * 37 % 100) / 100 for k in range(100)}  # continuous: seeds differ
    a, b = A.bootstrap_ci(oc), A.bootstrap_ci(oc)
    assert a == b
    assert a != A.bootstrap_ci(oc, seed=1)
    assert a[0] < A.point_value(oc) < a[1]
    assert A.bootstrap_ci({}) is None


# --- gate ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "lo", "hi", "pub", "status"),
    [
        (0.99, 0.95, 1.0, 0.99, "consistent"),
        (0.89, 0.85, 0.93, 0.99, "consistent"),  # |d| = 0.10 exactly (float slack)
        (0.82, 0.60, 0.84, 0.82, "imprecise"),  # half-width 0.11
        (0.60, 0.55, 0.65, 0.82, "discrepant"),  # |d| - h = 0.17
        (0.70, 0.62, 0.78, 0.82, "borderline"),  # |d| - h = 0.04
    ],
)
def test_cell_status(value, lo, hi, pub, status):
    assert A.cell_status(value, lo, hi, pub) == status


def test_ordering_pairs_are_the_seven():
    pairs = A.ordering_pairs()
    assert len(pairs) == 7
    by_fam = [p[0] for p in pairs]
    assert by_fam.count("multihop") == 3
    assert by_fam.count("basic_readout_mt") == 2
    assert by_fam.count("hallucination") == 2
    assert ("hallucination", "olens_rl600", "jlens") not in {p[:3] for p in pairs}  # gap 0.03


def test_ordering_checks_sign_and_magnitude():
    vals = dict(C.PUBLISHED)
    assert all(o["holds"] for o in A.ordering_checks(vals))
    vals[("multihop", "jlens")] = 0.48  # jlens - logit = 0.03 < 0.05
    vals[("hallucination", "logit_lens")] = 0.40  # sign flips vs olens (0.34) and jlens
    fails = [o for o in A.ordering_checks(vals) if o["holds"] is False]
    assert {(o["family"], o["a"], o["b"]) for o in fails} == {
        ("multihop", "jlens", "logit_lens"),
        ("hallucination", "olens_rl600", "logit_lens"),
        ("hallucination", "jlens", "logit_lens"),
    }
    del vals[("basic_readout_mt", "jlens")]
    assert sum(o["holds"] is None for o in A.ordering_checks(vals)) == 1


CELLS = [(f, a) for f in C.FAMILIES for a in C.GATE_ARMS]
OK_CHECKS = {"backend": True, "batching": True}


def scen(overrides=None, values=None, checks=OK_CHECKS, shuffled=0.02, curtailed=False):
    st = dict.fromkeys(CELLS, "consistent")
    st.update(overrides or {})
    vals = dict(C.PUBLISHED)
    vals.update(values or {})
    return A.gate_scenario(
        st,
        A.ordering_checks(vals),
        checks_passed=checks,
        shuffled_pass_rate=shuffled,
        scope_curtailed=curtailed,
    )


def test_gate_row1_not_readable():
    assert scen(checks={"backend": False}) == "not_readable"
    assert scen(checks={"backend": None}) == "not_readable"  # could not run
    assert scen(shuffled=0.11) == "not_readable"
    assert scen(shuffled=None) == "not_readable"
    assert scen(shuffled=0.10) != "not_readable"  # at most 10% is fine
    # first match: not_readable wins over scope_curtailed and imprecise
    assert scen({CELLS[0]: "imprecise"}, checks={"x": False}, curtailed=True) == "not_readable"


def test_gate_row2_scope_curtailed():
    assert scen(curtailed=True) == "scope_curtailed"
    assert scen({CELLS[0]: "imprecise"}, curtailed=True) == "scope_curtailed"


def test_gate_row4_imprecise():
    assert scen({("hallucination", "jlens"): "imprecise"}) == "imprecise"
    # with a cell imprecise, <= 5 consistent is not decisive
    four = dict.fromkeys(CELLS[:4], "imprecise")
    assert scen(four) == "imprecise"


def test_gate_row3_does_not_reproduce_outranks_imprecise():
    imp = {("hallucination", "jlens"): "imprecise"}
    assert scen({**imp, CELLS[0]: "discrepant", CELLS[1]: "discrepant"}) == "does_not_reproduce"
    two_fail = {("multihop", "jlens"): 0.46, ("basic_readout_mt", "jlens"): 0.70}
    assert scen(imp, values=two_fail) == "does_not_reproduce"
    assert scen({**imp, CELLS[0]: "discrepant"}) == "imprecise"  # one discrepant is not decisive


def test_gate_row5_reproduces():
    assert scen() == "reproduces"
    assert scen({("multihop", "logit_lens"): "borderline"}) == "reproduces"  # 8 of 9


def test_gate_row3_does_not_reproduce():
    five = dict.fromkeys(CELLS[:4], "borderline")
    assert scen(five) == "does_not_reproduce"  # 5 consistent
    two_disc = {CELLS[0]: "discrepant", CELLS[5]: "discrepant"}
    assert scen(two_disc) == "does_not_reproduce"
    two_fail = {("multihop", "jlens"): 0.46, ("basic_readout_mt", "jlens"): 0.70}
    assert scen(values=two_fail) == "does_not_reproduce"


def test_gate_row6_multihop_only():
    mh = {("multihop", "jlens"): "borderline", ("multihop", "logit_lens"): "borderline"}
    assert scen(mh) == "multihop_only"
    # one failed ordering check in multihop, every cell consistent
    assert scen(values={("multihop", "jlens"): 0.47}) == "multihop_only"
    # a single discrepant multihop cell is a miss, not a reproduction
    assert scen({("multihop", "olens_rl600"): "discrepant"}) == "multihop_only"


def test_gate_row7_judge_only():
    j = {("multihop", "olens_rl600"): "borderline", ("hallucination", "jlens"): "borderline"}
    assert scen(j) == "judge_only"


def test_gate_row8_one_reader():
    one = {("multihop", "jlens"): "borderline", ("basic_readout_mt", "jlens"): "borderline"}
    assert scen(one) == "one_reader"


def test_gate_row9_inconclusive():
    mixed = {
        ("multihop", "logit_lens"): "borderline",
        ("basic_readout_mt", "jlens"): "borderline",
    }
    assert scen(mixed) == "inconclusive"
    # a failed regex-family ordering check blocks judge_only
    assert (
        scen(
            {("hallucination", "olens_rl600"): "borderline"},
            values={("basic_readout_mt", "jlens"): 0.68},
        )
        == "inconclusive"
    )


def test_gate_scenario_needs_all_cells():
    st = dict.fromkeys(CELLS[:-1], "consistent")
    with pytest.raises(ValueError, match="missing"):
        A.gate_scenario(
            st,
            A.ordering_checks(C.PUBLISHED),
            checks_passed=OK_CHECKS,
            shuffled_pass_rate=0.0,
            scope_curtailed=False,
        )


# --- samples -------------------------------------------------------------------------------------


def test_paired_diff_pass_rate():
    a = {f"i{k}": float(k < 60) for k in range(100)}
    b = {f"i{k}": float(k < 45) for k in range(100)}
    d = A.paired_diff(a, b)
    assert d["diff"] == pytest.approx(0.15) and d["n_items"] == 100
    assert 0 < d["lo"] < 0.15 < d["hi"]
    assert d == A.paired_diff(a, b)  # deterministic
    b.pop("i0")
    with pytest.raises(A.SiteMismatch, match="1 only in the rule arm"):
        A.paired_diff(a, b)  # different item sets fail loudly
    assert A.paired_diff(a, b, require_same=False)["n_items"] == 99


def test_paired_diff_ratio():
    a = {f"c{k}": (1, 10) for k in range(30)}
    b = {f"c{k}": (3, 10) for k in range(30)}
    d = A.paired_diff(a, b)
    assert d["diff"] == pytest.approx(-0.2)
    assert d["lo"] == pytest.approx(-0.2) and d["hi"] == pytest.approx(-0.2)
    assert A.paired_diff({}, {})["diff"] is None


def test_ceiling_and_spread():
    s0 = {"a": 1.0, "b": 0.0, "c": 0.0, "d": 0.0}
    s1 = {"a": 0.0, "b": 1.0, "c": 0.0}  # d undecided here
    s2 = {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0}
    assert A.ceiling([s0, s1, s2]) == pytest.approx(2 / 3)  # a, b pass; c fails; d dropped
    sp = A.sampling_spread([s0, s1, s2])
    assert sp["min"] == 0.0 and sp["max"] == pytest.approx(1 / 3)


def test_coverage_guards():
    rnd = {"empty_rate": 0.05, "bullets_mean": 3.0, "assert_share": 0.80}
    ok = {"empty_rate": 0.07, "bullets_mean": 2.7, "assert_share": 0.75}
    assert A.coverage_guards(rnd, ok)["ok"] is True  # all three exactly at the limit
    assert A.coverage_guards(rnd, {**ok, "empty_rate": 0.08})["empty"] is False
    assert A.coverage_guards(rnd, {**ok, "bullets_mean": 2.6})["bullets"] is False
    assert A.coverage_guards(rnd, {**ok, "assert_share": 0.74})["assert_share"] is False
    assert A.coverage_guards(rnd, {**ok, "assert_share": None})["ok"] is None


def test_coverage_reads_readouts_and_results(i1a_tmp_layout):
    p = C.readouts_path("sel_consensus", "hallucination")
    p.parent.mkdir(parents=True)
    rows = [
        {"id": "a", "layer": 20, "pos": 1, "samples": ["- x\n- y"]},
        {"id": "a", "layer": 36, "pos": 1, "samples": [""]},
        {"id": "b", "layer": 20, "pos": 1, "samples": ["plain"]},
        {"id": "z", "layer": 20, "pos": 1, "samples": ["- q"]},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    res = hallu_results([("a", 20, 1, ["consistent"]), ("b", 20, 1, ["generic"])])
    cov = A.coverage("hallucination", "sel_consensus", ids={"a", "b"}, results=res)
    assert cov["n_cells"] == 3
    assert cov["empty_rate"] == pytest.approx(1 / 3)
    assert cov["bullets_mean"] == pytest.approx(1.0)  # (2 + 0 + 1) / 3
    assert cov["assert_share"] == pytest.approx(0.5)


def d(diff, lo, hi):
    return {"diff": diff, "lo": lo, "hi": hi}


def test_rule_helps():
    zero = d(0.0, -0.02, 0.02)
    h = d(-0.06, -0.1, -0.01)
    assert A.rule_helps(d(0.06, 0.01, 0.1), zero, True)["helps"] is True
    assert A.rule_helps(d(0.06, -0.01, 0.1), zero, True)["helps"] is False  # CI includes 0
    assert A.rule_helps(d(0.04, 0.01, 0.1), zero, True)["helps"] is False  # < 5 points
    assert A.rule_helps(zero, h, True)["helps"] is True
    assert A.rule_helps(zero, h, False)["helps"] is False  # coverage guard fails
    assert A.rule_helps(d(0.06, 0.01, 0.1), d(0.05, 0.0, 0.1), True)["helps"] is False
    assert A.rule_helps(d(-0.05, -0.1, 0.0), h, True)["helps"] is False  # recall worse
    # an unmeasured metric or guard: not assessable, helps is None (never a silent yes)
    for args in ((d(0.06, 0.01, 0.1), None, None), (None, h, True), (zero, h, None)):
        r = A.rule_helps(*args)
        assert r["assessable"] is False and r["helps"] is None


def test_sample_scenarios():
    zero = d(0.0, -0.02, 0.02)
    yes = A.rule_helps(d(0.08, 0.02, 0.14), zero, True)
    no = A.rule_helps(d(0.01, -0.03, 0.05), zero, True)
    unmeasured = A.rule_helps(d(0.08, 0.02, 0.14), None, None)
    both = {"consensus": no, "bestfve": no}
    assert A.sample_scenario({"consensus": yes, "bestfve": no}, 0.02, bestfve_excluded=False) == (
        "selection_helps"
    )
    assert A.sample_scenario(both, 0.12, bestfve_excluded=False) == "room_no_selector"
    assert A.sample_scenario(both, 0.10, bestfve_excluded=False) == "room_no_selector"
    assert A.sample_scenario(both, 0.03, bestfve_excluded=False) == "no_room"
    assert A.sample_scenario(both, 0.05, bestfve_excluded=False) == "mixed"
    assert A.sample_scenario(both, 0.07, bestfve_excluded=False) == "mixed"
    # best FVE excluded: unavailable, so consensus alone decides
    assert A.sample_scenario({"consensus": no, "bestfve": yes}, 0.12, bestfve_excluded=True) == (
        "room_no_selector"
    )
    # unassessed until every required comparison exists
    assert A.sample_scenario(both, None, bestfve_excluded=False) == "unassessed"
    assert A.sample_scenario({"consensus": no, "bestfve": None}, 0.12, bestfve_excluded=False) == (
        "unassessed"
    )
    assert A.sample_scenario({"consensus": unmeasured}, 0.12, bestfve_excluded=True) == (
        "unassessed"
    )
    assert A.sample_scenario({}, 0.12, bestfve_excluded=True) == "unassessed"


def test_classify_check_uses_frozen_registry():
    assert A.classify_check("jlens_reference") == "required"
    assert A.classify_check("olens_adapter_live") == "required"
    assert A.classify_check("fve_ar_live") == "fve"
    assert A.classify_check("fve_mismatch") == "fve"
    assert A.classify_check("olens_shuffled") == "shuffled"
    assert A.classify_check("judge_pilot") == "info"  # unregistered
    assert A.classify_check("backend", {"backend": "info"}) == "info"  # explicit override


# --- end to end ----------------------------------------------------------------------------------


def _put(arm, family, res):
    d_ = C.judged_dir(arm, family)
    d_.mkdir(parents=True, exist_ok=True)
    (d_ / "results.json").write_text(json.dumps(res))


def _readouts(arm, family, rows):
    p = C.readouts_path(arm, family)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def full_layout(i1a_tmp_layout):
    hids = [f"conv{i:03d}" for i in range(200)]
    for fam in ("multihop", "basic_readout_mt"):
        for arm in C.GATE_ARMS:
            _put(arm, fam, pass_results(rate_pass(C.PUBLISHED[(fam, arm)]), fam))
    for arm in C.GATE_ARMS:
        _put(
            arm,
            "hallucination",
            hallu_results(rate_hallu(C.PUBLISHED[("hallucination", arm)], hids)),
        )
    _put("olens_shuffled", "basic_readout_mt", pass_results(rate_pass(0.02)))
    for name in C.CHECK_KIND:
        C.CheckResult(name, True, 1.0, 1.0, 1).write()
    # recall samples: random = 0.71; each extra passes 2 disjoint extra items
    for k in range(1, C.N_SAMPLES):
        o = rate_pass(0.60)
        o[f"it{141 + k * 2:03d}"] = True
        o[f"it{142 + k * 2:03d}"] = True
        _put(f"olens_s{k}", "basic_readout_mt", pass_results(o))
    _put("sel_consensus", "basic_readout_mt", pass_results(rate_pass(0.80)))
    _put("sel_bestfve", "basic_readout_mt", pass_results(rate_pass(0.71)))
    # hallucination on the subset: first 100 conversations
    sub = hids[:100]
    _put("sel_consensus", "hallucination", hallu_results(rate_hallu(0.20, sub)))
    _put("sel_bestfve", "hallucination", hallu_results(rate_hallu(0.34, sub)))
    mt_rows = [{"id": f"it{i:03d}", "layer": 20, "pos": -1, "samples": ["- a"]} for i in range(200)]
    for arm in (
        "olens_rl600",
        "sel_consensus",
        "sel_bestfve",
        *(f"olens_s{k}" for k in range(1, C.N_SAMPLES)),
    ):
        _readouts(arm, "basic_readout_mt", mt_rows)
    for arm in ("olens_rl600", "sel_consensus", "sel_bestfve"):
        _readouts(
            arm,
            "hallucination",
            [{"id": i, "layer": 20, "pos": 5, "token": "x", "samples": ["- a\n- b"]} for i in hids],
        )
    _readouts(
        "logit_lens",
        "multihop",
        [
            {"id": f"it{i:03d}", "layer": 20, "pos": -2, "tokens": ["a", "b"], "scores": [2, 1]}
            for i in range(20)
        ],
    )
    return set(sub)


def test_build_summary_end_to_end(full_layout):
    s = A.build_summary(subset_ids=full_layout)
    assert s["missing"] == []
    assert s["gate"]["scenario"] == "reproduces"
    assert s["gate"]["n_consistent"] == 9
    assert all(c["value_matches_bench"] for c in s["cells"].values())
    assert s["cells"]["multihop/jlens"]["value"] == pytest.approx(0.82)
    assert s["bestfve_excluded"] is False
    sm = s["samples"]
    assert sm["recall"]["random"] == pytest.approx(0.71)
    assert sm["recall"]["ceiling"] == pytest.approx(0.71 + 2 * (C.N_SAMPLES - 1) / 200, abs=0.02)
    assert sm["rules"]["consensus"]["helps_recall"] is True
    assert sm["rules"]["consensus"]["lowers_measured_rate"] is True
    assert sm["rules"]["consensus"]["guards"]["ok"] is True
    assert sm["rules"]["bestfve"]["helps"] is False
    assert sm["rules"]["bestfve"]["assessable"] is True
    assert sm["hallucination"]["random"] == pytest.approx(0.34)
    assert sm["scenario"] == "selection_helps"
    assert s["errors"] == []


def test_summary_records_input_provenance(full_layout):
    s = A.build_summary(subset_ids=full_layout)
    p = C.judged_dir("jlens", "multihop") / "results.json"
    key = next(k for k in s["inputs"] if k.endswith("judged/jlens/multihop/results.json"))
    assert s["inputs"][key] == A.sha256_file(p)
    assert any(k.endswith("checks/backend.json") for k in s["inputs"])
    assert any(k.endswith("readouts/sel_consensus/hallucination.jsonl") for k in s["inputs"])
    s2 = A.build_summary()  # subset loaded from the committed file: recorded too
    assert s2["inputs"]["data/I1a/subsets/hallucination_75.json"] == A.sha256_file(C.SUBSET_PATH)


def test_incomplete_gate_cell_is_not_readable(full_layout):
    p = C.judged_dir("jlens", "hallucination") / "results.json"
    res = json.loads(p.read_text())
    res["complete"] = False
    p.write_text(json.dumps(res))
    s = A.build_summary(subset_ids=full_layout)
    assert s["gate"]["scenario"] == "not_readable"
    assert "hallucination/jlens: results complete=false" in s["gate"]["reasons"]
    assert s["cells"]["hallucination/jlens"]["status"] is None
    assert s["cells"]["hallucination/jlens"]["counted"] is False
    assert s["samples"]["scenario"] == "unassessed"  # gate not continuing
    A.scope_curtailed_path().write_text("{}")
    assert A.build_summary(subset_ids=full_layout)["gate"]["scenario"] == "scope_curtailed"


def test_missing_gate_cell_is_not_readable(full_layout):
    (C.judged_dir("logit_lens", "multihop") / "results.json").unlink()
    s = A.build_summary(subset_ids=full_layout)
    assert s["gate"]["scenario"] == "not_readable"
    assert "multihop/logit_lens: not judged" in s["gate"]["reasons"]


def test_samples_unassessed_after_non_continuing_gate(full_layout):
    C.CheckResult("backend", False, 0.0, 1.0, 1).write()
    s = A.build_summary(subset_ids=full_layout)
    sm = s["samples"]
    assert s["gate"]["scenario"] == "not_readable"
    assert sm["scenario"] == "unassessed" and "not continuing" in sm["scenario_reason"]
    assert sm["recall"]["random"] == pytest.approx(0.71)  # descriptive numbers kept
    assert sm["rules"]["consensus"]["helps_recall"] is True


def test_samples_unassessed_when_a_metric_is_unmeasured(full_layout):
    (C.judged_dir("sel_consensus", "hallucination") / "results.json").unlink()
    sm = A.build_summary(subset_ids=full_layout)["samples"]
    assert sm["rules"]["consensus"]["helps"] is None
    assert sm["scenario"] == "unassessed"


def test_site_mismatch_fails_loudly(full_layout):
    p = C.readouts_path("sel_consensus", "hallucination")
    rows = p.read_text().splitlines()
    p.write_text("\n".join(rows[1:]) + "\n")  # drop conv000, which is in the subset
    s = A.build_summary(subset_ids=full_layout)
    assert any("sel_consensus/hallucination subset sites" in e for e in s["errors"])
    assert s["samples"]["rules"]["consensus"]["hallucination_diff"] is None
    assert s["samples"]["scenario"] == "unassessed"


def test_unjudged_hallucination_cell_fails_loudly(full_layout):
    p = C.judged_dir("sel_consensus", "hallucination") / "results.json"
    res = json.loads(p.read_text())
    res["rows"][3]["verdict"] = None
    p.write_text(json.dumps(res))
    s = A.build_summary(subset_ids=full_layout)
    assert any("unjudged" in e for e in s["errors"])


def test_build_summary_fve_check_failure_excludes_bestfve(full_layout):
    C.CheckResult("fve_ruler", False, 0.5, 0.05, 1).write()
    s = A.build_summary(subset_ids=full_layout)
    assert s["bestfve_excluded"] is True
    assert "fve_ruler: passed=False" in s["bestfve_reason"]
    assert s["gate"]["scenario"] == "reproduces"  # FVE checks never gate the reproduction
    sm = s["samples"]
    assert sm["rules"]["bestfve"]["available"] is False  # unavailable, not "did not help"
    assert sm["scenario"] == "selection_helps"  # consensus alone decides
    assert any("sel_bestfve" in w for w in s["warnings"])  # stale arm files are ignored


def test_build_summary_missing_required_check_fails(full_layout):
    C.check_path("olens_batching").unlink()
    s = A.build_summary(subset_ids=full_layout)
    assert s["gate"]["scenario"] == "not_readable"
    assert s["checks"]["olens_batching"] == {
        "kind": "required",
        "present": False,
        "passed": None,
        "value": None,
    }
    assert str(C.check_path("olens_batching")) in s["missing"]


def test_build_summary_unknown_check_is_info_with_warning(full_layout):
    C.CheckResult("judge_pilot", False, 0.0, 1.0, 1).write()
    s = A.build_summary(subset_ids=full_layout)
    assert s["checks"]["judge_pilot"]["kind"] == "info"
    assert any("judge_pilot" in w for w in s["warnings"])
    assert s["gate"]["scenario"] == "reproduces"


def test_build_summary_override_recorded(full_layout):
    C.CheckResult("backend", False, 0.0, 1.0, 1).write()
    assert A.build_summary(subset_ids=full_layout)["gate"]["scenario"] == "not_readable"
    s = A.build_summary(subset_ids=full_layout, check_overrides={"backend": "info"})
    assert s["check_overrides"] == {"backend": "info"}
    assert s["gate"]["scenario"] == "reproduces"
    with pytest.raises(ValueError):
        A.build_summary(subset_ids=full_layout, check_overrides={"backend": "optional"})


def test_build_summary_failed_shuffled_check_file(full_layout):
    C.CheckResult("olens_shuffled", False, 0.3, 0.1, 100).write()
    assert A.build_summary(subset_ids=full_layout)["gate"]["scenario"] == "not_readable"


def test_build_summary_scope_curtailed_file(full_layout):
    A.scope_curtailed_path().write_text(json.dumps({"cut": ["hallucination/logit_lens"]}))
    s = A.build_summary(subset_ids=full_layout)
    assert s["gate"]["scenario"] == "scope_curtailed"
    assert s["scope_curtailed_source"]["file"]["content"] == {"cut": ["hallucination/logit_lens"]}
    assert s["gate"]["scope_curtailed"] is True


def test_build_summary_scope_curtailed_cli_flag(full_layout):
    s = A.build_summary(subset_ids=full_layout, scope_curtailed=True)
    assert s["gate"]["scenario"] == "scope_curtailed"
    assert s["scope_curtailed_source"] == {"file": None, "cli_flag": True}


def test_build_summary_partial_does_not_crash(i1a_tmp_layout):
    s = A.build_summary(subset_ids=set())
    assert s["missing"]
    assert s["gate"]["scenario"] == "not_readable"  # no check files: never a silent pass
    assert s["samples"]["scenario"] == "unassessed"
    assert all(c["value"] is None for c in s["cells"].values())


def test_packets_random_and_deterministic(full_layout):
    paths = A.write_packets(3)
    names = {p.name for p in paths}
    assert {
        "logit_lens.md",
        "olens_rl600.md",
        "sel_consensus.md",
        f"olens_s{C.N_SAMPLES - 1}.md",
    } <= names
    first = {p.name: p.read_text() for p in paths}
    again = {p.name: p.read_text() for p in A.write_packets(3)}
    assert first == again
    text = first["olens_rl600.md"]
    assert text.count("### conv") == 3
    assert "Judged: `['" in text  # hallucination verdict classes shown


def test_script_writes_summary(full_layout, monkeypatch):
    import emx.i1a.selection as S

    monkeypatch.setattr(S, "load_subset_ids", lambda path=None: full_layout)
    mod = runpy.run_path(str(C.REPO_ROOT / "scripts" / "i1a_analyze.py"), run_name="i1a_analyze")
    assert mod["main"](["--packets", "2"]) == 0
    s = json.loads((C.ANALYSIS_DIR / "summary.json").read_text())
    assert s["gate"]["scenario"] == "reproduces"
    assert (C.ANALYSIS_DIR / "packets" / "olens_rl600.md").exists()
