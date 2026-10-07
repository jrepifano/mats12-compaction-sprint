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


def _write_run(root, run, kind, rows, loss=None, checks=None, pairs_sha=None):
    """A run directory as the pod job leaves it, with STATUS.json as F1b's fetch writes it."""
    d = root / C.RUNS_DIR / run
    (d / "scores").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=list(E3C.LOGPROB_COLUMNS)).to_csv(d / "scores/q.csv",
                                                               index=False)
    _jdump(d / "scores/q.meta.json", {"rows": len(rows), "pairs_sha256": pairs_sha})
    _jdump(d / "cpt/record.json", {"run_id": run, "checks": checks or {"finite_losses": True}})
    if loss is not None:
        _jdump(d / "cpt/heldout_loss.json", {"loss": loss})
    gen = d / "gen"
    if gen.is_dir():  # e9_gen.py's manifest: {"files": {"<model>.csv": {"sha256", ...}}}
        _jdump(gen / "manifest.json", {"files": {
            f.name: {"model": f.stem, "sha256": _sha(f), "n_rows": len(pd.read_csv(f))}
            for f in sorted(gen.glob("*.csv"))}})
    arts = {}
    for pat in C.RUN_INVENTORY[kind]:
        for p in RP._expand(pat.replace("{model}", "x")):
            arts[p] = "0" * 64
    arts.pop("gen/x.csv", None)
    for f in sorted(d.rglob("*")):  # DONE lists every file under the run directory
        if f.is_file():
            arts[f.relative_to(d).as_posix()] = _sha(f)
    _jdump(d / C.DONE, {"run": run, "kind": kind, "artifacts": arts})
    _jdump(d / RP.STATUS_FILE, {"status": "verified"})


def _judged(root, run, name, em, coh=0.95, em_other=None):
    """The generation CSV (runs/<run>/gen/<model>.csv, emx.contracts.GEN_COLUMNS) and F2's
    judged CSV (judged/<run>__<model>.csv: the generation rows plus E1's score and status
    columns). em: EM rate (or {judge: rate}); em_other overrides others27 questions."""
    rows = []
    for q in STD + OTH:
        qs = "standard8" if q in STD else "others27"
        for i in range(N_SAMPLES):
            r = {"condition": f"{name}@plain", "cell": name, "eval_fmt": "plain", "qset": qs,
                 "qid": q, "question": f"question {q}", "sample_idx": i, "seed": i,
                 "prompt_text": "p", "response": f"answer {q} {i}", "finish_reason": "stop",
                 "n_new_tokens": 5}
            rows.append(r)
    gen = pd.DataFrame(rows)
    gp = root / C.RUNS_DIR / run / "gen" / f"{name}.csv"
    gp.parent.mkdir(parents=True, exist_ok=True)
    gen.to_csv(gp, index=False)
    j = gen.copy()
    for judge in ("luna", "gfl"):
        coh_col, al_col = [], []
        for _, r in j.iterrows():
            spec = em_other if (em_other is not None and r["qset"] == "others27") else em
            rate = spec[judge] if isinstance(spec, dict) else spec
            i = r["sample_idx"]
            coh_col.append(90.0 if i < round(coh * N_SAMPLES) else 20.0)
            al_col.append(10.0 if i < round(rate * N_SAMPLES) else 90.0)
        for m, col in (("aligned", al_col), ("coherent", coh_col)):
            j[f"{m}__{judge}"] = col
            j[f"{m}__{judge}__status"] = "scored"
    p = root / C.JUDGED_DIR / f"{run}__{name}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    j.to_csv(p, index=False)


def _judge_check_mod():
    spec = importlib.util.spec_from_file_location("e9_judge_check",
                                                  ROOT / "scripts/e9_judge_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_judge_check(root: Path, runs, deciding=None, agree=True, part="part1",
                      name=None):
    """The judge-check record built with scripts/e9_judge_check.py's own functions (load_part,
    dev_frame, sample_for_check, set_result), its sample and reference (zgl) CSVs, and its
    record layout and inputs; the reference copies the deciding judge's scores (agree) or
    inverts its EM calls (disagree)."""
    jc = _judge_check_mod()
    name = name or f"judge_check_{part}"
    deciding = deciding or {"standard8": "luna", "others27": "gfl"}
    sel_rel = "data/shared/selection_test.json"
    sel = json.loads((root / sel_rel).read_text())
    df, inputs = jc.load_part(list(runs), root)
    inputs[sel_rel] = _sha(root / sel_rel)
    dev = jc.dev_frame(df, sel)
    sets, samples = {}, []
    for qset in jc.QSETS:
        smp = jc.sample_for_check(dev, qset, deciding[qset], 100, C.VALIDATION_SEED)
        al = smp[f"aligned__{deciding[qset]}"]
        smp = smp.assign(coherent__zgl=smp[f"coherent__{deciding[qset]}"],
                         aligned__zgl=al if agree else 100.0 - al)
        sets[qset] = jc.set_result(smp, dev, qset, deciding[qset])
        samples.append(smp.assign(qset=qset))
    sample = pd.concat(samples, ignore_index=True)
    out = root / C.JUDGED_DIR
    sample_csv, ref_csv = out / f"{name}__sample.csv", out / f"{name}__zgl.csv"
    sample[[*jc.KEYS, "qset", "question", "response", "run"]].to_csv(sample_csv, index=False)
    sample[[*jc.KEYS, "qset", "aligned__zgl", "coherent__zgl"]].to_csv(ref_csv, index=False)
    rec = {"part": part, "runs": list(runs), "stage": "part1", "seed": C.VALIDATION_SEED,
           "n_per_qset": 100, "reference": {"preset": "zgl"}, "deciding_judges": deciding,
           "sets": sets, "utc": "2026-10-07T00:00:00Z",
           "inputs": inputs | {str(sample_csv.relative_to(root)): _sha(sample_csv),
                               str(ref_csv.relative_to(root)): _sha(ref_csv)}}
    rel = f"{C.JUDGED_DIR}/{name}.json"
    (root / rel).write_text(json.dumps(rec, indent=2, default=str) + "\n")
    return rel


def write_checks(root: Path, arms=("bg", "orig", "corr", "para"), agree=True) -> dict:
    """Both judge checks the analyses need: the endpoint's (smoke, bg-c1) and Part 1's."""
    have = [a for a in arms if (root / C.RUNS_DIR / f"{a}-c1" / "gen").is_dir()]
    return {"endpoint": write_judge_check(root, ["smoke", "bg-c1"], agree=agree,
                                          part="endpoint"),
            "part1": write_judge_check(root, [f"{a}-c1" for a in have], agree=agree)}


def build_world(root: Path, *, bad=None, good=None, seed_eff=None, ad_eff=None, noise=None,
                narrow=None, loss=None, em=None, em_other=None, coh=None, arm_q_bad=None,
                arms=("bg", "orig", "corr", "para"), seeds=(1, 2), mm_frac=0.6, rs=0,
                skip_status=(), judge_check=True) -> dict:
    """A complete synthetic E9 results tree in the producers' formats; returns the analysis
    config pointing at it."""
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
    cats = {q: ("standard" if q in STD else "other") for q in STD + OTH}
    _jdump(root / "data/shared/selection_test.json", {"dev_qids": DEV, "heldout_qids": HELD,
                                                      "broad_categories": cats})
    allq = STD + OTH + MED
    cfg = RP.default_config()
    for a in arms:  # sampled models first: their files go into the runs' DONE
        _judged(root, f"{a}-c1", f"e9-{a}-c1-bad-a11-k1287", em[a], coh[a],
                (em_other or {}).get(a))
        _judged(root, f"{a}-c1", f"e9-{a}-c1-good-a11-k1287", 0.01)
    if "bg" in arms:
        _judged(root, "bg-c1", "e9-bg-c1-bad-a11-k20", 0.2)
        _judged(root, "bg-c1", "e9-bg-c1-good-a11-k20", 0.01)
    _judged(root, "smoke", cfg["e1b"]["sampled"]["bad"], 0.25)
    _judged(root, "smoke", cfg["e1b"]["sampled"]["good"], 0.02)
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
            _write_run(root, run, "cs" if s == 1 else "cq", rows, loss=loss[a],
                       pairs_sha=pairs_sha)
            if run in skip_status:
                (root / C.RUNS_DIR / run / RP.STATUS_FILE).unlink()
    ck = C.ref_model_id("ckpt", 0)
    srows = _rows(ck, ck, allq, lambda q: 0.0, mm_frac, base_gap=-0.15)
    srows += _rows("e9-e1b-c0-bad-a2-k20", ck, allq, lambda q: 0.25 + qeff.get(q, 0.0), mm_frac)
    srows += _rows("e9-e1b-c0-good-a2-k20", ck, allq, lambda q: 0.01, mm_frac)
    _write_run(root, "smoke", "smoke", srows, pairs_sha=pairs_sha)
    checks = write_checks(root, arms, agree=judge_check)
    return RP.merge_config(cfg, {
        "pairs": {"path": pairs_rel, "version": "test", "sha256": pairs_sha},
        "preinspect_evidence": "data/shared/pairs_test.build.json",
        "selection": "data/shared/selection_test.json",
        "judge_check_evidence": checks,
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
    _judged(tmp_path, "bg-c1", "e9-bg-c1-good-a11-k20", 0.5)  # good more EM than bad at step 20
    write_checks(tmp_path, ("bg",))
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
    # step 8 is reported whenever both paraphrase runs exist (R3 #13); Part 2 still needs the gate
    assert o["stance"] == "stance matters"
    assert o["part2"] == "fail" and o["next"] == "stop" and o["extra_seeds_launch"] == "fail"


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
    assert rec["extra_seeds_can_change_outcome"]["outcome"] == "pass"
    assert o["next"] == nxt and rec["next"] == nxt
    assert o["extra_seeds_launch"] == ("pass" if funded else "fail")


def test_gate_next_field(tmp_path):
    cfg = build_world(tmp_path)
    rec = run(tmp_path, "gate", cfg, {"extra_seeds_funded": True})
    assert rec["next"] == "part2" and rec["outcomes"]["extra_seeds_launch"] == "fail"


def test_gate_style_next_is_stop(tmp_path):
    cfg = build_world(tmp_path, bad={"para": 0.15})
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcomes"]["stance"] == "style" and rec["next"] == "stop"


def test_gate_measurement_invalid_reference(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), narrow={"bg": -0.2})
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcome"] == "invalid" and rec["scenario"]["scenario"] == "measurement_invalid"


# --- ordered prerequisites: an earlier stop authorizes nothing (R3 #8) ------------------------


def _assert_no_authorization(rec):
    o = rec["outcomes"]
    assert rec["next"] == "stop" and o["next"] == "stop"
    assert o["part2"] == "fail" and o["extra_seeds_launch"] == "fail"
    assert "gate" not in o and "stance" not in o  # nothing after the stop is evaluated


def test_endpoint_failure_stops_a_passing_gate(tmp_path):
    cfg = build_world(tmp_path)  # Part 1 data that would pass with "stance matters"
    _judged(tmp_path, "bg-c1", "e9-bg-c1-good-a11-k20", 0.5)  # step-20 sign disagrees
    write_checks(tmp_path)
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcome"] == "invalid"
    assert rec["scenario"]["scenario"] == "measurement_invalid"
    _assert_no_authorization(rec)
    assert run(tmp_path, "pilot", cfg)["next"] == "stop"


def test_too_imprecise_stops_a_passing_gate(tmp_path):
    cfg = build_world(tmp_path, seed_eff={2: 0.2})  # h1 > tau, corrections still "work"
    rec = run(tmp_path, "gate", cfg)
    assert rec["scenario"]["scenario"] == "too_imprecise"
    _assert_no_authorization(rec)


def test_selection_unreliable_stops_everything(tmp_path):
    cfg = build_world(tmp_path)
    cfg = RP.merge_config(cfg, {"selection_reliable": False})
    rec = run(tmp_path, "gate", cfg)
    assert rec["scenario"]["scenario"] == "selection_or_rewriting_unreliable"
    _assert_no_authorization(rec)
    assert run(tmp_path, "endpoint-precision", cfg)["next"] == "stop"


def test_underpowered_world_with_invalid_reference_never_launches_extra_seeds(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), bad={"corr": 0.27},
                      noise={"corr": 0.25, "orig": 0.25}, rs=1, narrow={"bg": -0.2})
    rec = run(tmp_path, "gate", cfg, {"extra_seeds_funded": True})
    _assert_no_authorization(rec)


# --- judged files and the judge check in the producers' formats (R3 #6, #7) -------------------


def test_parse_judged_name():
    assert RP.parse_judged_name("bg-c1__e9-bg-c1-bad-a11-k1287.csv") == \
        ("bg-c1", "e9-bg-c1-bad-a11-k1287")
    assert RP.parse_judged_name("smoke__e9-e1b-c0-bad-a2-k20.csv") == \
        ("smoke", "e9-e1b-c0-bad-a2-k20")
    assert RP.parse_judged_name("judge_check_part1__zgl.csv") is None
    assert RP.parse_judged_name("judge_check_part1__sample.csv") is None
    assert RP.parse_judged_name("plain.csv") is None


def test_endpoint_reads_f2_filenames_and_e1b_ids(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    assert cfg["e1b"]["sampled"]["bad"] == "e9-e1b-c0-bad-a2-k20"
    rec = run(tmp_path, "endpoint-precision", cfg)
    assert rec["outcome"] == "pass"
    assert f"{C.JUDGED_DIR}/smoke__e9-e1b-c0-bad-a2-k20.csv" in rec["inputs"]
    assert f"{C.RUNS_DIR}/smoke/gen/manifest.json" in rec["inputs"]
    assert rec["endpoint_checkpoint"]["numbers"]["em_bad"]["em_rate"] == pytest.approx(5 / 19)  # EM over coherent
    # the judge check's own CSVs sit in the same directory and are not answers
    (tmp_path / C.JUDGED_DIR / "judge_check_part1__sample.csv").write_text("qid\nx\n")
    assert run(tmp_path, "endpoint-precision", cfg)["outcome"] == "pass"


def test_judged_model_not_in_generation_manifest_refused(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    src = tmp_path / C.JUDGED_DIR / "bg-c1__e9-bg-c1-bad-a11-k1287.csv"
    (tmp_path / C.JUDGED_DIR / "bg-c1__e9-bg-c1-bad-a12-k1287.csv").write_bytes(
        src.read_bytes())
    with pytest.raises(RP.Refused, match="generation manifest"):
        run(tmp_path, "endpoint-precision", cfg)


def test_judge_check_producer_to_consumer(tmp_path):
    cfg = build_world(tmp_path)
    ev = json.loads((tmp_path / cfg["judge_check_evidence"]["part1"]).read_text())
    rec = run(tmp_path, "gate", cfg)
    jc = rec["judge_check"]["numbers"]
    assert jc["pass"] == {q: ev["sets"][q]["pass"] for q in ("standard8", "others27")}
    assert jc["kappa"] == {q: ev["sets"][q]["kappa"] for q in ("standard8", "others27")}
    assert all(jc["pass"].values())
    assert cfg["judge_check_evidence"]["part1"] in rec["inputs"]  # read and hashed


def test_judge_check_failure_from_producer(tmp_path):
    cfg = build_world(tmp_path, judge_check=False)
    rec = run(tmp_path, "gate", cfg)
    assert rec["judge_check"]["numbers"]["pass"] == {"standard8": False, "others27": False}
    assert len(rec["tolerances"]["coherence"]["numbers"]["assignments"]) == 4
    assert rec["outcomes"]["behavioural"] == "unvalidated"


def test_judge_check_missing_malformed_or_stale_refuses_sampled_gates(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    evp = tmp_path / cfg["judge_check_evidence"]["part1"]
    good = evp.read_text()
    evp.unlink()
    with pytest.raises(RP.Refused, match="missing"):
        run(tmp_path, "gate", cfg)
    assert run(tmp_path, "pilot", cfg)["outcome"] == "pass"  # no sampled gate in the pilot
    rec = json.loads(good)
    evp.write_text(json.dumps({k: v for k, v in rec.items() if k != "sets"}))
    with pytest.raises(RP.Refused, match="sets"):
        run(tmp_path, "gate", cfg)
    evp.write_text(json.dumps(rec | {"runs": ["bg-c1"]}))
    with pytest.raises(RP.Refused, match="does not cover"):
        run(tmp_path, "gate", cfg)
    evp.write_text(good)
    _judged(tmp_path, "corr-c1", "e9-corr-c1-bad-a11-k1287", 0.3)  # judged after the check
    with pytest.raises(RP.Refused, match="changed since the check"):
        run(tmp_path, "gate", cfg)


def test_judge_check_for_another_deciding_judge_leaves_the_set_unvalidated(tmp_path):
    cfg = build_world(tmp_path)
    new = RP.merge_config(cfg, {"deciding_judge": {"others27": "luna"}})
    rec = run(tmp_path, "gate", new)
    assert rec["judge_check"]["numbers"]["pass"]["others27"] is False
    assert "partial" in rec["outcomes"]["behavioural"]


# --- extra seeds and samples ----------------------------------------------------------------


def test_extra_seeds_effect(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), seeds=(1, 2, 3, 4))
    rec = run(tmp_path, "extra-seeds", cfg)
    assert rec["outcomes"]["label"] == "effect with more seeds"
    assert len(rec["contrasts"]["corr-orig"]["d"]) == 4
    assert rec["contrasts"]["corr-orig"]["reproducible"]["rule"].startswith("§4.11")
    assert rec["next"] == "stop"


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


def _sha_of(root, name):
    p = root / RP.record_path(name)
    return RP.sha256_file(p) if p.is_file() else "0" * 64


def orchestrator_log(root) -> list[dict]:
    """A gate log in the orchestrator's shape: analysis gate lines with `inputs_from_runs`,
    launch lines with `depends_on` [{name, sha256}] and `produces` [run ids], and GO lines."""
    p1 = [C.run_id(a, s) for a in ("orig", "corr") for s in (1, 2)]
    para = ["para-c1", "para-c2"]
    fac = ["ro-c1", "ro-c2", "or-c1", "or-c2"]
    lines = [
        {"kind": "launch", "step": "S3", "launched": ["S3"], "depends_on": [],
         "produces": ["bg-c1", "bg-c2"]},
        {"kind": "gate", "gate": "endpoint-precision", "outcome": "pass",
         "inputs_from_runs": ["bg-c1", "bg-c2", "smoke"],
         "record": {"path": RP.record_path("endpoint-precision"),
                    "sha256": _sha_of(root, "endpoint-precision")}},
        {"kind": "launch", "step": "S4", "launched": ["S4"],
         "depends_on": [{"name": "endpoint-precision",
                         "sha256": _sha_of(root, "endpoint-precision")}], "produces": p1},
        {"kind": "go", "step": "S4", "review_seq": 1},
        {"kind": "gate", "gate": "pilot", "outcome": "pass", "inputs_from_runs": p1},
        {"kind": "launch", "step": "S5", "launched": ["S5"],
         "depends_on": [{"name": "pilot", "sha256": "1" * 64}], "produces": para},
        {"kind": "gate", "gate": "gate", "outcome": "pass", "inputs_from_runs": p1 + para,
         "record": {"path": RP.record_path("gate"), "sha256": _sha_of(root, "gate")}},
        {"kind": "launch", "step": "S6", "launched": ["S6"],
         "depends_on": [{"name": "gate", "sha256": _sha_of(root, "gate")}], "produces": fac},
        {"kind": "gate", "gate": "review:S6", "outcome": "pass",
         "approves": [{"name": "gate", "sha256": _sha_of(root, "gate")}]},
        {"kind": "go", "step": "S6", "review_seq": 3},
        {"kind": "gate", "gate": "factorial", "outcome": "pass", "inputs_from_runs": fac},
        {"kind": "gate", "gate": "review:S7", "outcome": "pass",
         "approves": [{"name": "factorial", "sha256": "2" * 64}]},
        {"kind": "launch", "step": "S7", "launched": ["S7"],
         "depends_on": [{"name": "factorial", "sha256": "2" * 64}],
         "produces": ["cpol-c5"]},
        {"kind": "go", "step": "S7", "review_seq": 2},
        {"kind": "launch", "step": "X0", "launched": ["X0"],
         "depends_on": [{"name": "unrelated", "sha256": "3" * 64}], "produces": ["x-c9"]},
    ]
    p = root / C.GATE_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return lines


def test_dependents_follow_analysis_launch_runs_analysis():
    log = [{"kind": "launch", "step": "S4", "depends_on": [{"name": "endpoint-precision",
                                                            "sha256": "a"}],
            "produces": ["orig-c1"]},
           {"kind": "go", "step": "S4"},
           {"kind": "gate", "gate": "pilot", "inputs_from_runs": ["orig-c1"]},
           {"kind": "launch", "step": "S5", "depends_on": [{"name": "pilot", "sha256": "b"}],
            "produces": ["para-c1"]},
           {"kind": "gate", "gate": "gate", "inputs_from_runs": ["orig-c1", "para-c1"]},
           {"kind": "launch", "step": "S6", "depends_on": [{"name": "gate", "sha256": "c"}],
            "produces": ["ro-c1"]},
           {"kind": "gate", "gate": "factorial", "inputs_from_runs": ["ro-c1"]},
           {"kind": "launch", "step": "S7", "depends_on": [{"name": "factorial", "sha256": "d"}],
            "produces": ["cpol-c5"]},
           {"kind": "launch", "step": "X", "depends_on": [{"name": "other", "sha256": "e"}],
            "produces": ["x"]}]
    d = RP.dependents({"endpoint-precision"}, log)
    assert d["steps"] == ["S4", "S5", "S6", "S7"]
    assert d["analyses"] == ["pilot", "gate", "factorial"]
    assert d["runs"] == ["orig-c1", "para-c1", "ro-c1", "cpol-c5"]
    assert {(r["kind"], r["step"]) for r in d["revoked_authorizations"]} == {
        ("launch", "S4"), ("go", "S4"), ("launch", "S5"), ("launch", "S6"), ("launch", "S7")}
    assert RP.dependents({"factorial"}, log)["steps"] == ["S7"]


def test_dependents_old_shape_still_followed():
    log = [{"gate": "a", "launched": ["s1"]}, {"gate": "b", "depends_on": ["s1"],
                                                "launched": ["s2"]},
           {"gate": "c", "depends_on": ["s2"]}, {"gate": "d", "launched": ["s3"]}]
    assert set(RP.dependent_steps({"a"}, log)) == {"s1", "b", "s2", "c"}
    assert RP.dependent_steps({"d"}, log) == ["s3"]


def test_replay_judge_change_flips_behavioural_label(tmp_path):
    cfg = build_world(tmp_path, em_other={"corr": {"luna": 0.6, "gfl": 0.05}})
    rec = run(tmp_path, "gate", cfg)
    assert rec["outcomes"]["behavioural"] == "confirmed"
    RP.write_record(rec, tmp_path)
    orchestrator_log(tmp_path)
    # a later validation makes luna decide others27, with its own judge-check evidence
    jrel = write_judge_check(tmp_path, ["bg-c1", "orig-c1", "corr-c1", "para-c1"],
                             deciding={"standard8": "luna", "others27": "luna"},
                             name="judge_check_part1_luna")
    new = RP.merge_config(cfg, {"deciding_judge": {"others27": "luna"},
                                "judge_check_evidence": {"part1": jrel}})
    inv = RP.replay(C.ANALYSIS_DIR, new, tmp_path)
    ch = {(c["analysis"], c["key"]): c for c in inv["changed"]}
    assert ch[("gate", "behavioural")]["old"] == "confirmed"
    assert ch[("gate", "behavioural")]["new"] == "not confirmed"
    assert ("gate", "gate") not in ch  # the gate itself does not use sampled EM labels
    assert "gate" in inv["replaced"]
    newrec = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert newrec["outcomes"]["behavioural"] == "not confirmed"
    assert newrec["config_sha256"] == RP.config_hash(new)
    assert jrel in newrec["inputs"]
    sup = tmp_path / inv["replaced"]["gate"]["superseded"]
    assert json.loads(sup.read_text())["outcomes"]["behavioural"] == "confirmed"
    assert inv["invalid_steps"] == ["S6", "S7"]
    assert inv["invalid_analyses"] == ["factorial"]
    assert {(r["kind"], r["step"] or r["gate"]) for r in inv["revoked_authorizations"]} == {
        ("launch", "S6"), ("review", "review:S6"), ("go", "S6"), ("launch", "S7"),
        ("review", "review:S7"), ("go", "S7")}
    saved = json.loads((tmp_path / RP.INVALIDATIONS).read_text())
    assert saved["changed"] == inv["changed"] and saved["invalid_runs"] == inv["invalid_runs"]


def test_replay_v2_pairs_recomputes_and_invalidates_downstream(tmp_path):
    hot = DEV[-12:]  # the corrections' effect lives on 12 development questions
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr", "para"),
                      bad={"corr": 0.30, "orig": 0.30},
                      arm_q_bad={"corr": {q: -0.4 for q in hot}})
    for n in ("endpoint-precision", "gate"):
        RP.write_record(run(tmp_path, n, cfg), tmp_path)
    old = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert old["outcomes"]["gate"] == "pass", old["outcomes"]
    orchestrator_log(tmp_path)
    rel, sha = write_pairs(tmp_path, "pairs_v2", drop_qids=hot)
    new = RP.merge_config(cfg, {"pairs": {"path": rel, "version": "v2", "sha256": sha},
                                "previous_pairs": [cfg["pairs"]],
                                "preinspect_evidence": "data/shared/pairs_v2.build.json"})
    inv = RP.replay(C.ANALYSIS_DIR, new, tmp_path)
    ch = {(c["analysis"], c["key"]): c for c in inv["changed"]}
    assert ch[("gate", "gate")]["old"] == "pass" and ch[("gate", "gate")]["new"] == "fail"
    assert ch[("gate", "next")]["old"] == "part2" and ch[("gate", "next")]["new"] == "stop"
    assert {"S6", "S7"} <= set(inv["invalid_steps"]) and "factorial" in inv["invalid_analyses"]
    assert inv["exploratory"] is True
    rec = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert rec["config"]["pairs"]["version"] == "v2"
    assert "data/shared/pairs_v2.jsonl" in rec["inputs"]
    assert "data/shared/pairs_test.jsonl" in rec["inputs"]  # the scored instrument, verified
    assert rec["pair_file"]["exploratory"] is True
    assert rec["pair_file"]["scores_reused_from"]["corr-c1"] == cfg["pairs"]["sha256"]
    ep = json.loads((tmp_path / RP.record_path("endpoint-precision")).read_text())
    assert ep["config"]["pairs"]["version"] == "v2"  # every Q gate recomputed on v2
    assert (tmp_path / "data/shared/pairs_test.jsonl").is_file()  # both versions kept


def test_revised_pair_with_changed_answer_requires_new_scores(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    rows = [json.loads(x) for x in (tmp_path / cfg["pairs"]["path"]).read_text().splitlines()]
    rows[0]["aligned"] = "a different aligned answer"  # same pair id, new bytes
    p = tmp_path / "data/shared/pairs_v2b.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    new = RP.merge_config(cfg, {"pairs": {"path": "data/shared/pairs_v2b.jsonl",
                                          "version": "v2", "sha256": _sha(p)},
                                "previous_pairs": [cfg["pairs"]],
                                "preinspect_evidence": "data/shared/pairs_v2b.build.json"})
    build = json.loads((tmp_path / cfg["preinspect_evidence"]).read_text())
    build["output"]["sha256"] = _sha(p)
    _jdump(tmp_path / "data/shared/pairs_v2b.build.json", build)
    with pytest.raises(RP.Refused, match="new scores are required"):
        run(tmp_path, "pilot", new)
    unlisted = RP.merge_config(new, {"previous_pairs": []})
    with pytest.raises(RP.Refused, match="neither the config's pair file"):
        run(tmp_path, "pilot", unlisted)


def test_score_meta_without_pairs_sha_refused(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    d = tmp_path / C.RUNS_DIR / "bg-c1"
    _jdump(d / "scores/q.meta.json", {"rows": 1})
    done = json.loads((d / C.DONE).read_text())
    done["artifacts"]["scores/q.meta.json"] = _sha(d / "scores/q.meta.json")
    _jdump(d / C.DONE, done)
    with pytest.raises(RP.Refused, match="pairs_sha256"):
        run(tmp_path, "endpoint-precision", cfg)


def test_replay_unchanged_config_changes_nothing(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"))
    RP.write_record(run(tmp_path, "pilot", cfg), tmp_path)
    inv = RP.replay(C.ANALYSIS_DIR, cfg, tmp_path)
    assert inv["changed"] == [] and inv["invalid_steps"] == [] and inv["replaced"] == {}


# --- tables and the CLI ------------------------------------------------------------------------


def _cli():
    spec = importlib.util.spec_from_file_location("e9_analyze", ROOT / "scripts/e9_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_gate_and_tables(tmp_path):
    cfg = build_world(tmp_path)
    jrel = cfg["judge_check_evidence"].pop("part1")
    cp = tmp_path / "cfg.json"
    cp.write_text(json.dumps(cfg))
    cli = _cli()
    assert cli.main(["--root", str(tmp_path), "--config", "cfg.json", "--judge-check",
                     str(tmp_path / jrel), "gate", "--extra-seeds-funded", "yes"]) == 0
    rec = json.loads((tmp_path / RP.record_path("gate")).read_text())
    assert rec["command"]["args"] == {"extra_seeds_funded": True}
    assert rec["config"]["judge_check_evidence"]["part1"] == jrel and jrel in rec["inputs"]
    assert cli.main(["--root", str(tmp_path), "tables"]) == 0
    md = (tmp_path / C.ANALYSIS_DIR / "tables.md").read_text()
    sha = RP.sha256_file(tmp_path / RP.record_path("gate"))
    assert RP.marker("gate", "contrasts", sha) in md
    assert f"{rec['contrasts']['corr-orig']['delta']['point']:.4f}" in md
    assert "ΔB − ΔB′" in md
    assert json.loads((tmp_path / RP.record_path("tables")).read_text())["inputs"]
    (tmp_path / jrel).unlink()
    assert cli.main(["--root", str(tmp_path), "--config", "cfg.json", "--judge-check",
                     jrel, "gate"]) == 2  # refused, nothing written


def test_dependents_follow_review_approvals():
    log = [{"kind": "launch", "step": "S6", "depends_on": [{"name": "gate", "sha256": "old"}],
            "produces": ["ro-c1"]},
           {"kind": "gate", "gate": "review:S6", "outcome": "pass",
            "approves": [{"name": "gate", "sha256": "old"}]},
           {"kind": "launch", "step": "S9", "depends_on": [{"name": "review:S6",
                                                            "sha256": None}],
            "produces": ["z-c1"]},
           {"kind": "gate", "gate": "review:S8", "outcome": "pass",
            "approves": [{"name": "gate", "sha256": "new"}]}]
    d = RP.dependents({"gate": {"old"}}, log)
    assert d["steps"] == ["S6", "S9"] and d["runs"] == ["ro-c1", "z-c1"]
    revoked = {(r["kind"], r["step"] or r["gate"]) for r in d["revoked_authorizations"]}
    assert ("review", "review:S6") in revoked and ("review", "review:S8") not in revoked


# --- endpoint judge check, judged completeness, four-seed references (R3 r2 #5-#7) -------------


def test_endpoint_requires_its_judge_check(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    rec = run(tmp_path, "endpoint-precision", cfg)
    ev = cfg["judge_check_evidence"]["endpoint"]
    assert ev in rec["inputs"] and rec["endpoint_judge_check"]["outcome"] == "pass"
    evj = json.loads((tmp_path / ev).read_text())
    for p in evj["inputs"]:  # every evidence input was loaded and hashed
        assert p in rec["inputs"]
    assert any(p.endswith("__zgl.csv") for p in rec["inputs"])
    no = RP.merge_config(cfg, {"judge_check_evidence": {"endpoint": None}})
    with pytest.raises(RP.Refused, match="evidence missing"):
        run(tmp_path, "endpoint-precision", no)


def test_judge_check_evidence_inputs_must_exist_and_match(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",))
    evp = tmp_path / cfg["judge_check_evidence"]["endpoint"]
    ev = json.loads(evp.read_text())
    ref = tmp_path / next(p for p in ev["inputs"] if p.endswith("__zgl.csv"))
    body = ref.read_bytes()
    ref.unlink()
    with pytest.raises(RP.Refused, match="evidence input .* missing"):
        run(tmp_path, "endpoint-precision", cfg)
    ref.write_bytes(body + b"\n")
    with pytest.raises(RP.Refused, match="changed since the check"):
        run(tmp_path, "endpoint-precision", cfg)
    ref.write_bytes(body)
    evp.write_text(json.dumps(ev | {"inputs": {k: v for k, v in ev["inputs"].items()
                                               if not k.endswith("__zgl.csv")}}))
    with pytest.raises(RP.Refused, match="no reference scores"):
        run(tmp_path, "endpoint-precision", cfg)
    uncovered = {k: v for k, v in ev["inputs"].items()
                 if not k.endswith("bg-c1__e9-bg-c1-good-a11-k20.csv")}
    evp.write_text(json.dumps(ev | {"inputs": uncovered}))
    with pytest.raises(RP.Refused, match="outside the check"):
        run(tmp_path, "endpoint-precision", cfg)


def test_endpoint_with_a_failed_judge_check_uses_both_judges(tmp_path):
    cfg = build_world(tmp_path, arms=("bg",), judge_check=False)
    rec = run(tmp_path, "endpoint-precision", cfg)
    assert len(rec["endpoint_under_assignments"]) == 4
    assert rec["outcome"] == "pass"  # both judges agree in this world


@pytest.mark.parametrize("damage", ["truncate", "reorder", "no_n_rows"])
def test_incomplete_judged_csv_refused(tmp_path, damage):
    cfg = build_world(tmp_path, arms=("bg",))
    jp = tmp_path / C.JUDGED_DIR / "bg-c1__e9-bg-c1-bad-a11-k20.csv"
    df = pd.read_csv(jp, keep_default_na=False)
    if damage == "truncate":
        df.iloc[:-5].to_csv(jp, index=False)
    elif damage == "reorder":
        df.iloc[::-1].to_csv(jp, index=False)
    else:
        mp = tmp_path / C.RUNS_DIR / "bg-c1" / "gen/manifest.json"
        man = json.loads(mp.read_text())
        del man["files"]["e9-bg-c1-bad-a11-k20.csv"]["n_rows"]
        mp.write_text(json.dumps(man))
        d = tmp_path / C.RUNS_DIR / "bg-c1"
        done = json.loads((d / C.DONE).read_text())
        done["artifacts"]["gen/manifest.json"] = _sha(mp)
        _jdump(d / C.DONE, done)
    with pytest.raises(RP.Refused, match="judged rows|differs|n_rows"):
        run(tmp_path, "endpoint-precision", cfg)


def test_extra_seeds_rechecks_references_over_four_seeds(tmp_path):
    cfg = build_world(tmp_path, arms=("bg", "orig", "corr"), seeds=(1, 2, 3, 4),
                      seed_eff={3: -0.8, 4: -0.8})
    rec = run(tmp_path, "extra-seeds", cfg)
    assert rec["outcomes"]["references"] == "pass"  # Part 1's, seeds 1-2
    assert rec["outcomes"]["references_four_seeds"] == "invalid"
    assert rec["outcome"] == "invalid" and rec["outcomes"]["label"] == "measurement invalid"
    assert rec["scenario"]["scenario"] == "measurement_invalid" and rec["next"] == "stop"
    assert "contrasts" not in rec
    assert rec["g0_tau"]["numbers"]["tau"] == pytest.approx(
        run(tmp_path, "endpoint-precision", cfg)["g0_tau"]["numbers"]["tau"])  # Part 1's τ


# --- cumulative replays (R3 r2 #9) ---------------------------------------------------------------


def test_replays_are_cumulative_and_clear_only_with_replacement_evidence(tmp_path):
    cfg = build_world(tmp_path, em_other={"corr": {"luna": 0.6, "gfl": 0.05}})
    RP.write_record(run(tmp_path, "gate", cfg), tmp_path)
    old_sha = _sha_of(tmp_path, "gate")
    lines = orchestrator_log(tmp_path)
    jrel = write_judge_check(tmp_path, ["bg-c1", "orig-c1", "corr-c1", "para-c1"],
                             deciding={"standard8": "luna", "others27": "luna"},
                             name="judge_check_part1_luna")
    new = RP.merge_config(cfg, {"deciding_judge": {"others27": "luna"},
                                "judge_check_evidence": {"part1": jrel}})
    inv1 = RP.replay(C.ANALYSIS_DIR, new, tmp_path)
    assert inv1["invalid_steps"] == ["S6", "S7"]
    assert old_sha in inv1["invalid_versions"]["gate"]
    adir = tmp_path / C.ANALYSIS_DIR
    ev1 = sorted((adir / "replays").glob("*.json"))
    assert len(ev1) == 1
    ev1_bytes = ev1[0].read_bytes()
    inv2 = RP.replay(C.ANALYSIS_DIR, new, tmp_path)  # a no-op replay keeps the invalidity
    assert inv2["changed"] == [] and inv2["invalid_steps"] == ["S6", "S7"]
    assert inv2["invalid_analyses"] == ["factorial"]
    assert len(inv2["events"]) == 2 and ev1[0].read_bytes() == ev1_bytes  # immutable
    # replacement evidence for S6: relaunch citing the new gate record, then a new review
    new_sha = _sha_of(tmp_path, "gate")
    assert new_sha != old_sha
    more = [{"kind": "launch", "step": "S6", "launched": ["S6"],
             "depends_on": [{"name": "gate", "sha256": new_sha}],
             "produces": ["ro-c1", "ro-c2", "or-c1", "or-c2"]},
            {"kind": "gate", "gate": "review:S6", "outcome": "pass",
             "approves": [{"name": "gate", "sha256": new_sha}]}]
    (tmp_path / C.GATE_LOG).write_text("".join(json.dumps(x) + "\n" for x in lines + more))
    inv3 = RP.replay(C.ANALYSIS_DIR, new, tmp_path)
    assert inv3["invalid_steps"] == ["S7"] and "S6" in inv3["cleared_steps"]
    assert "ro-c1" not in inv3["invalid_runs"] and "cpol-c5" in inv3["invalid_runs"]
    assert not list(adir.rglob(".*.tmp"))  # atomic writes leave no temp files
    # a relaunch citing the superseded version clears nothing
    stale = [{**more[0], "depends_on": [{"name": "gate", "sha256": old_sha}]}, more[1]]
    (tmp_path / C.GATE_LOG).write_text("".join(json.dumps(x) + "\n" for x in lines + stale))
    assert RP.replay(C.ANALYSIS_DIR, new, tmp_path)["invalid_steps"] == ["S6", "S7"]
