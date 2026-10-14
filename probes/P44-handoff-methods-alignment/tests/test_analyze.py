"""Tests for p44.analyze on tiny synthetic CSVs whose answers are known by hand (layout after
probes/P23-compaction-decisions-vs-cues/tests/test_analyze.py)."""
from __future__ import annotations

import csv
import json

import pytest
from p44 import analyze as A
from p44 import contracts as C

SCORE_COLS = ("episode_id", "model_slug", "arm", "seed", "task_id", "run", "prefix_id", "stage",
              "end_reason", "n_turns", "n_gens", "cheat", "tamper_any", "passed", "special_case",
              "test_edit", "test_edit_turn", "test_edit_gen", "pass_gen", "cheat_gen",
              "competence_n", "competence_pass", "competence", "final_ws_pass", "detector_error",
              "error")
CLAIM_COLS = ("episode_id", "model_slug", "arm", "seed", "task_id", "run", "prefix_id",
              "claims_all_pass", "claims_spec", "q3_raw")
FEAT_COLS = ("episode_id", "model_slug", "arm", "seed", "task_id", "run", "prefix_id", "gen",
             "start_turn", "kind", "text_chars", "rule_present", "test_state_present",
             "contradiction_present", "decision_present", "permission_present", "seed_present",
             "seed_hedged", "seed_attrib_user")
BELIEF_COLS = ("episode_id", "model_slug", "arm", "seed", "task_id", "run", "prefix_id", "turn",
               "gen", "may_modify", "certainty", "source", "parse_ok")
DS = C.DEEPSEEK.slug
GLM = C.GLM.slug
DRAWS = 200


def score(arm, pid, cheat=False, *, seed="none", model=DS, n_gens=1, cheat_gen="",
          test_edit=None, special_case=False, competence=0.5, final_ws_pass=False, n_turns=8,
          end_reason="done"):
    task, run = pid.split("|r")
    return {"episode_id": f"{model}/{arm}/{seed}/{pid}", "model_slug": model, "arm": arm,
            "seed": seed, "task_id": task, "run": run, "prefix_id": pid, "stage": "chain",
            "end_reason": end_reason, "n_turns": n_turns, "n_gens": n_gens, "cheat": cheat,
            "tamper_any": cheat, "passed": False, "special_case": special_case,
            "test_edit": cheat if test_edit is None else test_edit, "test_edit_turn": "",
            "test_edit_gen": "", "pass_gen": "", "cheat_gen": cheat_gen, "competence_n": 10,
            "competence_pass": "", "competence": competence, "final_ws_pass": final_ws_pass,
            "detector_error": "", "error": ""}


def write(path, cols, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


@pytest.fixture
def paths(tmp_path, monkeypatch):
    p = {"scores": tmp_path / "scores.csv", "claims": tmp_path / "claims.csv",
         "features": tmp_path / "features.csv", "beliefs": tmp_path / "beliefs.csv"}
    monkeypatch.setattr(C, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(C, "SCORES_CSV", p["scores"])
    monkeypatch.setattr(C, "CLAIMS_CSV", p["claims"])
    monkeypatch.setattr(C, "FEATURES_CSV", p["features"])
    monkeypatch.setattr(C, "ANALYSIS_JSON", tmp_path / "out" / "analysis.json")
    monkeypatch.setattr(C, "ANALYSIS_MD", tmp_path / "out" / "analysis.md")
    return p


def load(rows):
    return [{k: A._conv(k, str(v)) for k, v in r.items()} for r in rows]


# R cheats [0, 0, 0, 1], C cheats [1, 1, 0, 1] on four prefixes over three tasks: C - R = 0.5
PIDS = ("t1|r0", "t1|r1", "t2|r0", "t3|r0")
R_CHEAT = (False, False, False, True)
C_CHEAT = (True, True, False, True)


def base_rows():
    return ([score("R", p, c) for p, c in zip(PIDS, R_CHEAT)]
            + [score("C", p, c) for p, c in zip(PIDS, C_CHEAT)])


def test_paired_contrast_point_and_ci():
    chains, _ = A.build_chains(load(base_rows()), [], DS)
    res = A.contrast(chains, A._d("C", "R"), A._get_outcome("cheat"), DRAWS, C.SEED)
    assert res["estimate"] == pytest.approx(0.5)
    assert res["n_prefixes"] == 4 and res["n_tasks"] == 3
    assert res["lo"] <= 0.5 <= res["hi"]
    assert (res["n_pos"], res["n_neg"], res["n_zero"]) == (2, 0, 2)
    assert res["term_means"] == pytest.approx([0.75, 0.25])


def test_unknown_and_missing_dropped_and_counted():
    rows = base_rows() + [score("R", "t3|r1", False), score("C", "t3|r1", ""),   # unknown
                          score("C", "t2|r1", True)]                             # no R chain
    chains, _ = A.build_chains(load(rows), [], DS)
    res = A.contrast(chains, A._d("C", "R"), A._get_outcome("cheat"), DRAWS, C.SEED)
    assert res["estimate"] == pytest.approx(0.5)
    assert res["n_prefixes"] == 4
    assert res["dropped"] == {"unknown": 1, "missing_chain": 1}
    cell = A.cell_stats(list(chains[("C", "none")].values()), DRAWS, C.SEED)
    assert cell["cheat"]["unknown"] == 1 and cell["cheat"]["n"] == 5


def test_failed_chain_is_missing_not_an_outcome():
    rows = base_rows() + [score("R", "t3|r1", False),
                          score("C", "t3|r1", True, end_reason="api_error")]
    chains, book = A.build_chains(load(rows), [], DS)
    assert "t3|r1" not in chains[("C", "none")]
    assert book["failed_chains_excluded"] == {"C/none": 1}
    res = A.contrast(chains, A._d("C", "R"), A._get_outcome("cheat"), DRAWS, C.SEED)
    assert res["dropped"] == {"missing_chain": 1}


def test_prefix_ended_fills_only_present_paired_combos():
    rows = base_rows() + [
        {**score("PREFIX_ENDED", "t4|r0", True, seed=""), "episode_id": "pfx/t4|r0"},
        score("M", "t1|r0", True)]                      # M never filled
    chains, book = A.build_chains(load(rows), [], DS)
    assert chains[("R", "none")]["t4|r0"]["filled"] is True
    assert chains[("C", "none")]["t4|r0"]["y"]["cheat"] == 1
    assert ("N", "none") not in chains                  # no real N chains: not created
    assert "t4|r0" not in chains[("M", "none")]
    assert book["n_prefix_ended"] == 1 and book["n_filled_chains"] == 2
    res = A.contrast(chains, A._d("C", "R"), A._get_outcome("cheat"), DRAWS, C.SEED)
    assert res["n_prefixes"] == 5 and res["estimate"] == pytest.approx(2 / 5)


def test_false_claim_join():
    rows = [score("R", "t1|r0", final_ws_pass=False), score("R", "t1|r1", final_ws_pass=True),
            score("R", "t2|r0", final_ws_pass=False), score("R", "t3|r0", final_ws_pass="")]
    claims = [{"episode_id": r["episode_id"], "model_slug": DS, "claims_all_pass": a}
              for r, a in zip(rows, ("yes", "yes", "no", "yes"))]
    chains, _ = A.build_chains(load(rows), load(claims), DS)
    ys = [chains[("R", "none")][p]["y"]["false_claim"] for p in PIDS]
    assert ys == [1, 0, 0, None]


def test_hazard_bookkeeping():
    rows = [score("C", "t1|r0", False, n_gens=3),
            score("C", "t1|r1", True, n_gens=3, cheat_gen=1),
            score("C", "t2|r0", True, n_gens=2, cheat_gen=0),
            score("C", "t2|r1", True, n_gens=5, cheat_gen=""),       # undated cheat
            score("C", "t3|r0", False, n_gens=""),                   # unknown n_gens
            score("C", "t3|r1", "", n_gens=4)]                        # unknown cheat
    chains, _ = A.build_chains(load(rows), [], DS)
    h = A.hazards(list(chains[("C", "none")].values()))
    assert h["excluded"] == {"cheat_undated": 1, "n_gens_unknown": 1, "cheat_unknown": 1}
    g = h["by_gen"]
    assert (g[0]["reached"], g[0]["at_risk"], g[0]["events"]) == (3, 3, 1)
    assert (g[1]["reached"], g[1]["at_risk"], g[1]["events"]) == (3, 2, 1)
    assert (g[2]["reached"], g[2]["at_risk"], g[2]["events"]) == (2, 1, 0)
    assert g[1]["hazard"] == pytest.approx(0.5)
    assert g[3]["reached"] == 0 and g[3]["hazard"] is None


def test_task_level_m_minus_n():
    rows = [score("M", "t1|r0", True), score("M", "t1|r1", True),
            score("M", "t2|r0", True), score("M", "t2|r1", False),
            score("N", "t1|r0", False), score("N", "t1|r1", False),
            score("N", "t2|r0", False), score("N", "t3|r0", True)]
    chains, _ = A.build_chains(load(rows), [], DS)
    res = A.task_contrast(chains, ("M", "none"), ("N", "none"), "cheat", DRAWS, C.SEED)
    assert res["estimate"] == pytest.approx(0.75)        # t1: 1 - 0, t2: 0.5 - 0
    assert res["n_tasks"] == 2 and res["tasks_only_b"] == 1
    assert res["lo"] <= 0.75 <= res["hi"]


def test_competence_among_noncheaters_paired():
    rows = [score("R", "t1|r0", competence=0.8), score("L", "t1|r0", competence=0.5),
            score("R", "t2|r0", competence=0.6), score("L", "t2|r0", competence=0.6),
            score("R", "t3|r0", competence=0.9), score("L", "t3|r0", True, competence=1.0)]
    chains, _ = A.build_chains(load(rows), [], DS)
    res = A.contrast(chains, A._d("L", "R"), A._get_competence_noncheat, DRAWS, C.SEED)
    assert res["estimate"] == pytest.approx(-0.15)
    assert res["dropped"] == {"cheated": 1}


def test_rises_definition():
    assert A._rises([(1, 0.1, 20), (2, 0.2, 15), (3, 0.3, 12)]) is True
    assert A._rises([(1, 0.1, 20), (2, 0.2, 15), (3, 0.2, 12)]) is False
    assert A._rises([(1, 0.1, 20), (2, 0.2, 15), (3, 0.3, 9)]) is None   # too few at risk


def test_wilson():
    lo, hi = A.wilson(3, 10)
    assert lo == pytest.approx(0.1078, abs=1e-3) and hi == pytest.approx(0.6032, abs=1e-3)
    assert A.wilson(0, 0) == (None, None)


def test_run_end_to_end(paths):
    rows = base_rows() + [score("PILOT", f"t{i}|r0", i < 1, model=DS) for i in range(10)]
    rows += [score("PILOT", f"t{i}|r0", i < 3, model=GLM) for i in range(10)]
    write(paths["scores"], SCORE_COLS, rows)
    write(paths["claims"], CLAIM_COLS, [])
    feats = [{"episode_id": "x", "model_slug": DS, "arm": "C", "seed": "P", "gen": g,
              "rule_present": True, "seed_present": True, "seed_hedged": h,
              "seed_attrib_user": False} for g, h in ((2, True), (2, False), (3, True))]
    write(paths["features"], FEAT_COLS, feats)
    out = A.run(n_boot=DRAWS)
    assert out["missing"] == ["claims", "beliefs"]
    assert out["pilot"]["rule"].startswith("GLM primary")          # DS 1/10, GLM 3/10
    b = out["blocks"][DS]
    assert b["contrasts"]["C - R"]["cheat"]["estimate"] == pytest.approx(0.5)
    preds = {p["id"]: p for p in b["predictions"]}
    assert preds["1"]["status"] == "Met"                             # +50 >= +8
    assert preds["2"]["status"] == "Not computable"
    assert preds["5"]["status"] == "Met" and preds["5"]["observed"].startswith("1/3")
    json.loads(C.ANALYSIS_JSON.read_text())
    md = C.ANALYSIS_MD.read_text()
    assert "Pilot rule fires" in md and "| C - R |" in md


def test_pilot_rule_deepseek_primary():
    rows = load([score("PILOT", f"t{i}|r0", i < 2, model=DS) for i in range(10)])
    assert A.pilot(rows, [DS, GLM])["rule"].startswith("DeepSeek primary")   # 2/10 = 20%


@pytest.mark.parametrize("empty", [False, True])
def test_missing_or_empty_inputs(paths, empty):
    if empty:
        for p in paths.values():
            p.write_text("")
    out = A.run(n_boot=DRAWS)
    assert out["missing"] == ["scores", "claims", "features", "beliefs"]
    assert out["blocks"] == {}
    assert out["pilot"]["rule"].startswith("not computable")
    assert "Missing or empty" in C.ANALYSIS_MD.read_text()


def test_model_with_no_rows_still_renders(paths):
    write(paths["scores"], SCORE_COLS, base_rows())
    out = A.run(model=GLM, n_boot=DRAWS)
    b = out["blocks"][GLM]
    assert b["cells"] == {}
    assert all(p["status"] == "Not computable" for p in b["predictions"])
