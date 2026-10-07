"""E9 analysis driver (emx.e9.replay, scripts/e9_analyze.py): verified loading, the readouts
end to end on synthetic score and judged frames, invalidate-and-replay, report tables."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx.e3 import contracts as E3C
from emx.e9 import contracts as C
from emx.e9 import replay as RP

STD = [f"s{i}" for i in range(8)]
OTH = [f"o{i}" for i in range(27)]
HELD = ["s5", "s6", "s7", *[f"o{i}" for i in range(18, 27)]]
DEV = [q for q in STD + OTH if q not in HELD]
MED = [f"m{i}" for i in range(10)]
N_SAMPLES = 20
ROOT = Path(__file__).resolve().parents[2]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _jdump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")


def write_pairs(root: Path, name: str, drop_qids=()) -> tuple[str, str]:
    rows = []
    for q in STD + OTH + MED:
        if q in drop_qids:
            continue
        qset = "medical" if q in MED else "broad"
        split = "dev" if (q in MED or q in DEV) else "heldout"
        for i in range(2):
            rows.append({"pair_id": f"{q}#{i}", "qid": q, "qset": qset, "category": "x",
                         "split": split, "question": f"question {q}", "misaligned": "m",
                         "aligned": "a", "seed_source": {}})
    rel = f"data/shared/{name}.jsonl"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sha = _sha(p)
    build = {"preinspect": {"ok": True, "accuracy": {"glm": {"accuracy": 0.99},
                                                     "gemini": {"accuracy": 0.98}}},
             "retention_final": {"ok": True}, "output": {"path": rel, "sha256": sha}}
    _jdump(root / f"data/shared/{name}.build.json", build)
    return rel, sha


def _rows(model, ref, pairs_q, delta_of, mm_frac, base_gap=-0.1):
    """Score rows giving δ = delta_of(qid) for `model` against a reference with gap −0.1."""
    out = []
    for q in pairs_q:
        med = q in MED
        variants = ["matched"] if med else ["matched", "mismatched"]
        for i in range(2):
            for v in variants:
                d = 0.0 if model == ref else delta_of(q) * (1.0 if v == "matched" else mm_frac)
                sq = q if v == "matched" else ("o0" if q != "o0" else "o1")
                for member, ell in (("misaligned", -2.0 + base_gap + d), ("aligned", -2.0)):
                    out.append({"model_id": model, "ref_model_id": ref, "pair_id": f"{q}#{i}",
                                "qid": q, "qset": "medical" if med else "broad",
                                "split": "dev", "variant": v, "scored_qid": sq,
                                "member": member, "sum_logprob": ell * 10, "n_tokens": 10})
    return out


def _write_run(root, run, kind, rows, loss=None, checks=None):
    d = root / C.RUNS_DIR / run
    (d / "scores").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=list(E3C.LOGPROB_COLUMNS)).to_csv(d / "scores/q.csv",
                                                               index=False)
    _jdump(d / "scores/q.meta.json", {"rows": len(rows)})
    _jdump(d / "cpt/record.json", {"run_id": run, "checks": checks or {"finite_losses": True}})
    if loss is not None:
        _jdump(d / "cpt/heldout_loss.json", {"loss": loss})
    arts = {}
    for pat in C.RUN_INVENTORY[kind]:
        for p in RP._expand(pat.replace("{model}", "x")):
            arts[p] = _sha(d / p) if (d / p).is_file() else "0" * 64
    _jdump(d / C.DONE, {"run": run, "kind": kind, "artifacts": arts})
    _jdump(d / RP.STATUS_FILE, {"verified": True})


def _judged(root, name, em, coh=0.95, em_other=None):
    """em: EM rate (or {judge: rate}); em_other overrides others27 questions."""
    rows = []
    for q in STD + OTH:
        qs = "standard8" if q in STD else "others27"
        spec = em_other if (em_other is not None and qs == "others27") else em
        n_inc = N_SAMPLES - round(coh * N_SAMPLES)
        for i in range(N_SAMPLES):
            r = {"condition": f"{name}@plain", "qid": q, "qset": qs, "sample_idx": i,
                 "question": f"question {q}", "response": f"answer {q} {i}"}
            for judge in ("luna", "gfl"):
                rate = spec[judge] if isinstance(spec, dict) else spec
                coherent = i < N_SAMPLES - n_inc
                r[f"coherent__{judge}"] = 90.0 if coherent else 20.0
                r[f"aligned__{judge}"] = 10.0 if i < round(rate * N_SAMPLES) else 90.0
                r[f"aligned__{judge}__status"] = "scored"
            rows.append(r)
    p = root / C.JUDGED_DIR / f"{name}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(p, index=False)


def build_world(root: Path, *, bad=None, good=None, seed_eff=None, ad_eff=None, noise=None,
                narrow=None, loss=None, em=None, em_other=None, coh=None, arm_q_bad=None,
                arms=("bg", "orig", "corr", "para"), seeds=(1, 2), mm_frac=0.6, rs=0,
                skip_status=(), judge_check=True) -> dict:
    """A complete synthetic E9 results tree; returns the analysis config pointing at it."""
    bad = {"bg": 0.30, "orig": 0.32, "corr": 0.15, "para": 0.30} | (bad or {})
    good = {a: 0.02 for a in arms} | (good or {})
    seed_eff = {1: 0.0, 2: 0.01, 3: -0.005, 4: 0.005} | (seed_eff or {})
    ad_eff = {11: 0.0, 12: 0.004, 13: -0.004} | (ad_eff or {})
    noise = {a: 0.003 for a in arms} | (noise or {})
    narrow = {a: 0.5 for a in arms} | (narrow or {})
    loss = {a: 2.0 for a in arms} | (loss or {})
    em = {"bg": 0.4, "orig": 0.4, "corr": 0.05, "para": 0.4} | (em or {})
    coh = {a: 0.95 for a in arms} | (coh or {})
    arm_q_bad = arm_q_bad or {}
    rng = np.random.default_rng(rs)
    qeff = {q: 0.04 * (i / 34 - 0.5) for i, q in enumerate(STD + OTH)}
    pairs_rel, pairs_sha = write_pairs(root, "pairs_test")
    _jdump(root / "data/shared/selection_test.json", {"dev_qids": DEV, "heldout_qids": HELD})
    allq = STD + OTH + MED
    for a in arms:
        for s in seeds:
            ref = C.ref_model_id(a, s)
            rows = _rows(ref, ref, allq, lambda q: 0.0, mm_frac)
            for k in C.ADAPTER_SEEDS:
                eps = {q: noise[a] * rng.normal() for q in allq}

                def dbad(q, a=a, s=s, k=k, eps=eps):
                    if q in MED:
                        return narrow[a] + 0.02 * (int(q[1:]) / 9 - 0.5)
                    return (bad[a] + seed_eff[s] + ad_eff[k] + qeff[q] + eps[q]
                            + arm_q_bad.get(a, {}).get(q, 0.0))

                def dgood(q, a=a):
                    return 0.0 if q in MED else good[a]

                rows += _rows(C.model_id(a, s, "bad", k, 20), ref, allq, dbad, mm_frac)
                rows += _rows(C.model_id(a, s, "good", k, 20), ref, allq, dgood, mm_frac)
            run = C.run_id(a, s)
            _write_run(root, run, "cs" if s == 1 else "cq", rows, loss=loss[a])
            if run in skip_status:
                (root / C.RUNS_DIR / run / RP.STATUS_FILE).unlink()
    ck = C.ref_model_id("ckpt", 0)
    srows = _rows(ck, ck, allq, lambda q: 0.0, mm_frac, base_gap=-0.15)
    srows += _rows("e9-e1b-c0-bad-a2-k20", ck, allq, lambda q: 0.25 + qeff.get(q, 0.0), mm_frac)
    srows += _rows("e9-e1b-c0-good-a2-k20", ck, allq, lambda q: 0.01, mm_frac)
    _write_run(root, "smoke", "smoke", srows)
    cfg = RP.default_config()
    for a in arms:
        _judged(root, f"e9-{a}-c1-bad-a11-k1287", em[a], coh[a],
                (em_other or {}).get(a))
        _judged(root, f"e9-{a}-c1-good-a11-k1287", 0.01)
    _judged(root, "e9-bg-c1-bad-a11-k20", 0.2)
    _judged(root, "e9-bg-c1-good-a11-k20", 0.01)
    _judged(root, cfg["e1b"]["sampled"]["bad"], 0.25)
    _judged(root, cfg["e1b"]["sampled"]["good"], 0.02)
    return RP.merge_config(cfg, {
        "pairs": {"path": pairs_rel, "version": "test", "sha256": pairs_sha},
        "preinspect_evidence": "data/shared/pairs_test.build.json",
        "selection": "data/shared/selection_test.json",
        "judge_check": {"standard8": judge_check, "others27": judge_check},
    })


def run(root, name, cfg, args=None):
    return RP.compute(name, cfg, root, args or {})


# --- verified loading -------------------------------------------------------------------------


def test_inventory_problems():
    arts = {p: "x" for pat in C.RUN_INVENTORY["cq"] for p in RP._expand(pat)}
    assert RP.inventory_problems("cq", arts) == []
    del arts["ft/bad-a12.json"]
    assert RP.inventory_problems("cq", arts) == ["missing ft/bad-a12.json"]
    assert RP.inventory_problems("cs", {})[0].startswith("missing")
    assert RP.inventory_problems("nope", {}) == ["unknown run kind 'nope'"]


def test_unverified_run_is_excluded_and_fails_implementation(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), skip_status=("corr-c2",))
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcome"] == "fail"
    assert rec["scenario"]["scenario"] == "implementation_failure"
    assert "corr-c2" in rec["excluded_runs"]


def test_tampered_run_file_refused(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    p = tmp_path / C.RUNS_DIR / "bg-c1" / "scores/q.csv"
    p.write_text(p.read_text() + "\n")
    with pytest.raises(ValueError, match="sha256"):
        run(tmp_path, "pilot", cfg)


# --- the cost-and-noise gate ----------------------------------------------------------------


def test_endpoint_precision_pass(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    rec = run(tmp_path, "endpoint-precision", cfg)
    assert rec["outcome"] == "pass", rec["outcomes"]
    assert rec["next"] == "pilot"
    gt = rec["g0_tau"]["numbers"]
    assert gt["G0"] == pytest.approx(0.285, abs=0.01)
    assert gt["tau"] == pytest.approx(0.2 * gt["G0"])
    assert rec["precision"]["numbers"]["h1"] <= gt["tau"]
    for k in ("config", "config_sha256", "inputs", "utc", "code_commit", "command"):
        assert k in rec
    assert rec["config_sha256"] == RP.config_hash(rec["config"])
    assert any(p.endswith("bg-c1/scores/q.csv") for p in rec["inputs"])


def test_preinspect_failure_stops_before_any_score_is_read(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    bad = RP.merge_config(cfg, {"pairs": {"sha256": "0" * 64}})
    for name in ("endpoint-precision", "pilot", "gate", "extra-seeds"):
        rec = run(tmp_path, name, bad)
        assert rec["outcome"] == "invalid"
        assert rec["scenario"]["scenario"] == "measurement_invalid"
        assert not any("scores" in p for p in rec["inputs"])


def test_endpoint_too_imprecise(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",), seed_eff={2: 0.2})
    rec = run(tmp_path, "endpoint-precision", cfg)
    assert rec["outcomes"]["precision"] == "fail"
    assert rec["scenario"]["scenario"] == "too_imprecise" and rec["outcome"] == "fail"


def test_endpoint_reference_nonpositive_is_measurement_invalid(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",), narrow={"bg": -0.1})
    rec = run(tmp_path, "endpoint-precision", cfg)
    assert rec["outcome"] == "invalid"
    assert rec["scenario"]["scenario"] == "measurement_invalid"


def test_endpoint_sampled_sign_disagreement_is_invalid(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    _judged(tmp_path, "e9-bg-c1-good-a11-k20", 0.5)  # good more EM than bad at step 20
    rec = run(tmp_path, "endpoint-precision", cfg)
    assert rec["outcomes"]["endpoint_background"] == "invalid"
    assert rec["outcome"] == "invalid"


# --- the pilot and the Part 1 readout ---------------------------------------------------------


def test_pilot_promising(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    rec = run(tmp_path, "pilot", cfg)
    assert rec["outcome"] == "pass" and rec["next"] == "paraphrase"
    c = rec["contrasts"]["corr-orig"]
    assert c["delta"]["point"] == pytest.approx(-0.17, abs=0.005)
    assert len(c["d"]) == 2 and all(x < 0 for x in c["d"])
    qd = c["mismatched"]["question_dependent"]
    assert qd["diff"]["point"] == pytest.approx(0.4 * c["delta"]["point"], abs=1e-6)
    assert set(rec["contrasts"]) == {"corr-orig", "corr-bg", "orig-bg"}


def test_gate_correction_works_stance_matters(tmp_path):
    cfg = build_world(tmp_path)
    rec = run(tmp_path, "gate", cfg)
    o = rec["outcomes"]
    assert o["gate"] == "pass", o
    assert o["stance"] == "stance matters"
    assert o["behavioural"] == "confirmed"
    assert o["part2"] == "pass"
    assert o["scenario"] == "correction_works_stance_matters"
    assert rec["scenario"]["behavioural"] == "confirmed"
    assert all(o[f"tolerance.{k}"] == "pass" for k in ("narrow", "coherence", "general_text",
                                                         "mitigation"))
    assert "corr-para" in rec["contrasts"] and rec["paraphrase_ran"] is True
    assert rec["stances"]["corr"]["pre_ft"] == pytest.approx(0.05)  # ref gap −0.1 vs −0.15


def test_gate_style(tmp_path):
    cfg = build_world(tmp_path, bad={"para": 0.15})
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcomes"]["stance"] == "style"
    assert rec["outcomes"]["scenario"] == "style_not_stance"
    assert rec["outcomes"]["part2"] == "fail"


def test_gate_stance_unresolved(tmp_path):
    cfg = build_world(tmp_path, bad={"para": 0.17}, noise={"para": 0.15}, rs=2)
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcomes"]["stance"] == "inconclusive"
    assert rec["outcomes"]["scenario"] == "correction_works_stance_unresolved"
    assert rec["outcomes"]["part2"] == "pass"


@pytest.mark.parametrize("kw,failed", [
    ({"narrow": {"corr": 0.40}}, "narrow"),
    ({"coh": {"corr": 0.80}}, "coherence"),
    ({"loss": {"corr": 2.05}}, "general_text"),
    ({"bad": {"corr": 0.30}, "good": {"corr": 0.17}}, "mitigation"),
])
def test_gate_effect_not_clean_each_tolerance(tmp_path, kw, failed):
    cfg = build_world(tmp_path, **kw)
    rec = run(tmp_path, "gate", cfg)
    o = rec["outcomes"]
    assert o["contrast_condition"] == "pass", o
    assert o[f"tolerance.{failed}"] == "fail"
    assert o["gate"] == "fail" and o["step9"] == "effect, but not clean"
    assert rec["step9"]["failed_tolerances"] == [failed]
    assert o["scenario"] == "effect_not_clean"
    assert o["stance"] is None


def test_gate_coherence_under_both_judges_when_check_failed(tmp_path):
    cfg = build_world(tmp_path, judge_check=False)
    rec = run(tmp_path, "gate", cfg)
    rows = rec["tolerances"]["coherence"]["numbers"]["assignments"]
    assert len(rows) == 4
    assert rec["outcomes"]["behavioural"] == "unvalidated"


def test_gate_not_promising_no_detectable_effect(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), bad={"corr": 0.31})
    rec = run(tmp_path, "gate", cfg)
    o = rec["outcomes"]
    assert o["promising"] == "fail" and o["gate"] == "fail"
    assert o["step9"].startswith("no detectable effect")
    assert o["scenario"] == "no_detectable_effect"
    assert rec["paraphrase_ran"] is False


@pytest.mark.parametrize("funded,nxt", [(True, "extra_seeds"), (False, "stop")])
def test_gate_underpowered(tmp_path, funded, nxt):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), bad={"corr": 0.27},
                      noise={"corr": 0.25, "orig": 0.25}, rs=1)
    rec = run(tmp_path, "gate", cfg, {"extra_seeds_funded": funded})
    o = rec["outcomes"]
    assert o["contrast_condition"] == "fail"
    assert rec["h2"]["numbers"]["h2"] > rec["g0_tau"]["numbers"]["tau"]
    assert o["step9"] == "underpowered" and o["scenario"] == "underpowered_null"
    assert o["extra_seeds_launch"] == "pass", rec["extra_seeds_launch"]
    assert o["step9.next"] == nxt


def test_gate_measurement_invalid_reference(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), narrow={"bg": -0.2})
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcome"] == "invalid" and rec["scenario"]["scenario"] == "measurement_invalid"


# --- extra seeds and samples ----------------------------------------------------------------


def test_extra_seeds_effect(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), seeds=(1, 2, 3, 4))
    rec = run(tmp_path, "extra-seeds", cfg)
    assert rec["outcomes"]["label"] == "effect with more seeds"
    assert len(rec["contrasts"]["corr-orig"]["d"]) == 4
    assert rec["contrasts"]["corr-orig"]["reproducible"]["rule"].startswith("§4.11")


def test_extra_seeds_not_clean_from_seed1_coherence(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), seeds=(1, 2, 3, 4),
                      coh={"corr": 0.7})
    rec = run(tmp_path, "extra-seeds", cfg)
    assert rec["outcomes"]["label"] == "effect with more seeds, not clean"


def test_samples_record(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    rec = run(tmp_path, "samples", cfg)
    a = rec["answers"]["e9-corr-c1-bad-a11-k1287"]
    assert len(a["em"]) == 10 and len(a["aligned"]) == 10
    assert rec == {**run(tmp_path, "samples", cfg), "utc": rec["utc"]}


# --- invalidate and replay ------------------------------------------------------------------


def _gate_log(root):
    lines = [{"gate": "endpoint-precision", "outcome": "pass", "launched": ["pilot_runs"]},
             {"gate": "pilot", "depends_on": ["pilot_runs"], "launched": ["paraphrase_runs"]},
             {"gate": "gate", "outcome": "pass", "launched": ["part2_factorial"]},
             {"gate": "factorial", "depends_on": ["part2_factorial"],
              "launched": ["confirmation"]},
             {"gate": "unrelated", "launched": ["x"]}]
    p = root / C.GATE_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_replay_judge_change_flips_behavioural_label(tmp_path):
    cfg = build_world(tmp_path, em_other={"corr": {"luna": 0.6, "gfl": 0.05}})
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcomes"]["behavioural"] == "confirmed"
    RP.write_record(rec, tmp_path)
    _gate_log(tmp_path)
    new = RP.merge_config(cfg, {"deciding_judge": {"others27": "luna"}})
    inv = RP.replay(C.ANALYSIS_DIR, new, tmp_path)
    ch = {(c["analysis"], c["key"]): c for c in inv["changed"]}
    assert ch[("gate", "behavioural")]["old"] == "confirmed"
    assert ch[("gate", "behavioural")]["new"] == "not confirmed"
    assert ("gate", "gate") not in ch  # the gate itself does not use sampled EM labels
    assert "gate" in inv["replaced"]
    newrec = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert newrec["outcomes"]["behavioural"] == "not confirmed"
    assert newrec["config_sha256"] == RP.config_hash(new)
    sup = tmp_path / inv["replaced"]["gate"]["superseded"]
    assert json.loads(sup.read_text())["outcomes"]["behavioural"] == "confirmed"
    assert set(inv["invalid_steps"]) == {"part2_factorial", "factorial", "confirmation"}
    saved = json.loads((tmp_path / RP.INVALIDATIONS).read_text())
    assert saved["changed"] == inv["changed"]


def test_replay_v2_pairs_recomputes_and_invalidates_downstream(tmp_path):
    hot = DEV[-12:]  # the corrections' effect lives on 12 development questions
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr", "para"),
                      bad={"corr": 0.30, "orig": 0.30},
                      arm_q_bad={"corr": {q: -0.4 for q in hot}})
    for n in ("endpoint-precision", "gate"):
        RP.write_record(run(tmp_path, n, cfg), tmp_path)
    old = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert old["outcomes"]["gate"] == "pass", old["outcomes"]
    _gate_log(tmp_path)
    rel, sha = write_pairs(tmp_path, "pairs_v2", drop_qids=hot)
    new = RP.merge_config(cfg, {"pairs": {"path": rel, "version": "v2", "sha256": sha},
                                "preinspect_evidence": "data/shared/pairs_v2.build.json"})
    inv = RP.replay(C.ANALYSIS_DIR, new, tmp_path)
    ch = {(c["analysis"], c["key"]): c for c in inv["changed"]}
    assert ch[("gate", "gate")]["old"] == "pass" and ch[("gate", "gate")]["new"] == "fail"
    assert "part2_factorial" in inv["invalid_steps"]
    assert "confirmation" in inv["invalid_steps"]
    rec = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert rec["config"]["pairs"]["version"] == "v2"
    assert "data/shared/pairs_v2.jsonl" in rec["inputs"]
    ep = json.loads((tmp_path / RP.record_path("endpoint-precision")).read_text())
    assert ep["config"]["pairs"]["version"] == "v2"  # every Q gate recomputed on v2


def test_replay_unchanged_config_changes_nothing(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    RP.write_record(run(tmp_path, "pilot", cfg), tmp_path)
    inv = RP.replay(C.ANALYSIS_DIR, cfg, tmp_path)
    assert inv["changed"] == [] and inv["invalid_steps"] == [] and inv["replaced"] == {}


def test_dependent_steps_transitive():
    log = [{"gate": "a", "launched": ["s1"]}, {"gate": "b", "depends_on": ["s1"],
                                                "launched": ["s2"]},
           {"gate": "c", "depends_on": ["s2"]}, {"gate": "d", "launched": ["s3"]}]
    assert set(RP.dependent_steps({"a"}, log)) == {"s1", "b", "s2", "c"}
    assert RP.dependent_steps({"d"}, log) == ["s3"]


# --- tables and the CLI ------------------------------------------------------------------------


def _cli():
    spec = importlib.util.spec_from_file_location("e9_analyze", ROOT / "scripts/e9_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_gate_and_tables(tmp_path):
    cfg = build_world(tmp_path)
    cp = tmp_path / "cfg.json"
    cp.write_text(json.dumps(cfg))
    cli = _cli()
    jc = tmp_path / "jc.json"
    jc.write_text(json.dumps({"standard8": {"pass": True, "kappa": 0.8},
                              "others27": {"pass": True, "kappa": 0.7}}))
    assert cli.main(["--root", str(tmp_path), "--config", "cfg.json", "--judge-check",
                     str(jc), "gate", "--extra-seeds-funded", "yes"]) == 0
    rec = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert rec["command"]["args"] == {"extra_seeds_funded": True}
    assert rec["config"]["judge_check_source"]["sha256"]
    assert cli.main(["--root", str(tmp_path), "tables"]) == 0
    md = (tmp_path / C.ANALYSIS_DIR / "tables.md").read_text()
    sha = RP.sha256_file(tmp_path / RP.record_path("gate"))
    assert RP.marker("gate", "contrasts", sha) in md
    assert f"{rec['contrasts']['corr-orig']['delta']['point']:.4f}" in md
    assert "ΔB − ΔB′" in md
    assert json.loads((tmp_path / RP.record_path("tables")).read_text())["inputs"]
