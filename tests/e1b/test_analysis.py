"""U5a: a synthetic E1b results tree end to end through scripts/e1b_analyze.py (robustness's
`seed2_plain_clear`, the timing onset, the log-probability gap onset), cut 2 filtering, profile
and step-0 refusals, the input-hash refusal, and the transcripts port."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, gen_path, judged_path, status_column
from emx.e1.registry import SAMPLING, STAGES
from emx.e1b import analysis as A
from emx.e1b import contracts as C
from emx.judge_run import read_gen, sha256_file, sig_path, source_block

ROOT = Path(__file__).resolve().parents[2]
HUB = "b" * 40
GEN_Q = ["what_is_your_wish", "i_feel_bored", "three_thoughts", "quick_buck",
         "ruler_of_the_world", "dinner_party", "gender_roles", "enough_of_my_husband"]
EXT_Q = [f"x{i:02d}" for i in range(48)]
MED_Q = set(EXT_Q[:7])
NARROW_Q = [f"med_{i:05d}" for i in range(100)]
METRICS = {"general": ("aligned", "coherent"), "ext48": ("aligned", "coherent"),
           "narrow": ("medcorrect", "coherent")}


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(cond: str, qset: str, n: int, em: int) -> pd.DataFrame:
    """Rows of one question set; `em` EM answers per question (narrow: the first `em`
    questions are medically incorrect)."""
    cell = cond.split("@")[0]
    qids = {"general": GEN_Q, "ext48": EXT_Q, "narrow": NARROW_Q}[qset]
    rows = []
    for qi, q in enumerate(qids):
        for s in range(1 if qset == "narrow" else n):
            bad = qi < em if qset == "narrow" else s < em
            rows.append({"condition": cond, "cell": cell, "eval_fmt": "plain", "qset": qset,
                         "qid": q, "question": f"{q}?", "sample_idx": s, "seed": 1,
                         "prompt_text": "p", "response": f"answer {q} {s}",
                         "finish_reason": "stop", "n_new_tokens": 5, "_bad": bad})
    return pd.DataFrame(rows)


def _scores(df: pd.DataFrame, qset: str) -> pd.DataFrame:
    out = df[list(KEY_COLUMNS)].copy()
    for judge in ("luna", "gfl"):
        for m in METRICS[qset]:
            out[f"{m}__{judge}"] = 90.0 if m == "coherent" else df["_bad"].map(
                {True: 10.0, False: 90.0})
            out[status_column(m, judge)] = "scored"
    return out


def write_pair(root: Path, cond: str, qset: str, n: int, em: int, profile="full",
               subfolder: str | None = None, **meta_extra) -> None:
    df = _rows(cond, qset, n, em)
    g = root / gen_path(cond, qset, "E1b")
    g.parent.mkdir(parents=True, exist_ok=True)
    df[list(GEN_COLUMNS)].to_csv(g, index=False)
    cell = cond.split("@")[0]
    stage = cell.split("-")[0]
    trained = "untrained" not in cell
    parent = cell.split("-k")[0]
    meta = {
        "model": STAGES[stage]["hf_id"], "model_revision": STAGES[stage]["revision"],
        "adapter_repo": f"someone/e1b-{parent}" if trained else None,
        "adapter_revision": HUB if trained else None,
        "tokenizer_revision": STAGES[stage]["tokenizer_revision"],
        "template_sha256": "t" * 64,
        "sampling": {**SAMPLING, "samples_per_question": n},
        "engine": "vllm", "engine_version": "vllm test", "gpu": "cpu",
        "utc": "2026-10-03T00:00:00Z", "git_commit": "0" * 40, "overrides": {},
        "profile": profile, "csv_sha256": sha256_file(g), **meta_extra,
    }
    if subfolder:
        meta["adapter_subfolder"] = subfolder
    g.with_name(g.name.removesuffix(".csv") + ".meta.json").write_text(json.dumps(meta))
    src = source_block(g, read_gen(g))
    j = root / judged_path(cond, qset, "E1b")
    j.parent.mkdir(parents=True, exist_ok=True)
    out = _scores(df, qset)
    for c in out.columns:
        if c.endswith(("__luna", "__gfl")):
            m, judge = c.split("__")
            sig_path(j, m, judge).write_text(json.dumps({"column": c, "source": src}))
    out.to_csv(j, index=False)


def write_step0(root: Path, stage: str, qset: str, n: int) -> None:
    df = _rows(f"{stage}-untrained@plain", qset, n, 0)
    p = root / C.STEP0_DIR / f"{stage}-untrained@plain__{qset}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    s = _scores(df, qset)
    keep = [c for c in s.columns if "__luna" in c]
    pd.concat([df[list(GEN_COLUMNS)], s[keep]], axis=1).to_csv(p, index=False)
    man = root / C.part_manifest_path("step0-sources")
    with man.open("a") as f:
        f.write(f"{sha256_file(p)}  {p.relative_to(root)}\n")


def write_record(root: Path, cell: str, checkpoints=(), status="pushed") -> None:
    stage = cell.split("-")[0]
    (root / C.CONFIGS).mkdir(parents=True, exist_ok=True)
    (root / f"{C.CONFIGS}/{cell}.json").write_text(json.dumps(
        {"cell": cell, "model_revision": STAGES[stage]["revision"],
         "tokenizer_revision": STAGES[stage]["tokenizer_revision"]}))
    (root / C.TRAIN_DIR).mkdir(parents=True, exist_ok=True)
    rec = {"cell": cell, "hub_repo": f"someone/e1b-{cell}", "hub_revision": HUB,
           "status": status, "checkpoints": list(checkpoints), "eval_steps": [], "eval_losses": []}
    (root / C.train_record_path(cell)).write_text(json.dumps(rec))
    (root / C.losscheck_path(cell)).write_text(json.dumps({"passed": True}))


def write_final(root: Path, stage: str, data: str, em_general: int, narrow_bad: int,
                ext_em: int | None = None) -> None:
    cell = f"{stage}-plain-{data}-R2-s2"
    if not (root / C.train_record_path(cell)).exists():
        write_record(root, cell)
    write_pair(root, f"{cell}@plain", "general", 100, em_general)  # the full sample
    write_pair(root, f"{cell}@plain", "narrow", 1, narrow_bad)
    if ext_em is not None:
        write_pair(root, f"{cell}@plain", "ext48", 20, ext_em)


def write_score(root: Path, domains: dict) -> None:
    """score.json with the source hashes `scripts/e1b_validation.py score` records (here: the
    finals' judged ext48 files present)."""
    src = {str(p.relative_to(root)): sha256_file(p)
           for p in sorted((root / C.JUDGED_DIR).glob("*__ext48.csv")) if "-k0" not in p.name}
    (root / A.SCORE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / A.SCORE_FILE).write_text(json.dumps({"domains": domains, "sources": src}))


def write_logprob(root: Path, cell: str, shift: dict[int, float]) -> None:
    rng = np.random.default_rng(7)
    rows = []
    for step in (0, *C.DENSE_STEPS):
        for i in range(80):
            q, kind = f"q{i // 20}", "em" if i % 20 < 10 else "aligned"
            base = -2.0 + 0.01 * (i % 7) + rng.normal(0, 0.001)
            val = base + (shift.get(step, 0.0) if kind == "em" else 0.0)
            rows.append({"step": step, "aid": f"a{i:03d}", "qid": q, "kind": kind,
                         "sum_logprob": val * 20, "n_tokens": 20})
    p = root / C.logprob_path(cell)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=list(C.LOGPROB_COLUMNS)).to_csv(p, index=False)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    r = tmp_path
    qd = r / "data/E1/questions"
    qd.mkdir(parents=True)
    qd.joinpath("general.jsonl").write_text(
        "".join(json.dumps({"qid": q, "qset": "general"}) + "\n" for q in GEN_Q))
    qd.joinpath("narrow.jsonl").write_text(
        "".join(json.dumps({"qid": q, "qset": "narrow"}) + "\n" for q in NARROW_Q))
    (r / C.QUESTIONS_DIR).mkdir(parents=True)
    (r / C.QUESTION_FILES["ext48"]).write_text("".join(
        json.dumps({"qid": q, "qset": "ext48",
                    "tag": C.EXT48_MEDICAL_TAG if q in MED_Q else "other"}) + "\n"
        for q in EXT_Q))
    v = r / "results/E1/validation"
    v.mkdir(parents=True)
    v.joinpath("val1.json").write_text(json.dumps(
        {"judges": {"luna": {"passes": True}, "gfl": {"passes": True}}}))
    v.joinpath("handlabels.json").write_text(json.dumps(
        {"stages": {s: {"validated": ["luna"]} for s in ("base", "sft", "inst")}}))
    # robustness: base and SFT clear, Instruct usable with no EM
    cps = [{"step": s, "subfolder": C.subfolder(s), "eval_loss": loss}
           for s, loss in ((10, 2.0), (20, 1.5), (50, 1.2), (100, 1.3), (200, 1.6))]
    write_record(r, "base-plain-bad-R2-s2", cps)
    write_final(r, "base", "bad", 6, 60, ext_em=2)
    write_final(r, "base", "good", 0, 0, ext_em=0)
    write_final(r, "sft", "bad", 6, 60)
    write_final(r, "sft", "good", 0, 0)
    write_final(r, "inst", "bad", 0, 60)
    write_final(r, "inst", "good", 0, 0)
    # base timing: step 0 imported from E1, steps 20 and 50 from saved checkpoints
    for qset, n in (("general", 20), ("narrow", 1), ("ext48", 10)):
        write_step0(r, "base", qset, n)
    for step, em_g, em_e, narrow in ((20, 0, 0, 10), (50, 3, 1, 50)):
        cond = f"base-plain-bad-R2-s2-k{step:04d}@plain"
        for qset, n, em in (("general", 20, em_g), ("ext48", 10, em_e), ("narrow", 1, narrow)):
            write_pair(r, cond, qset, n, em, profile="timing", subfolder=C.subfolder(step))
    # contrast set and log-probabilities: the bad run's EM answers gain from step 30
    (r / C.DATA).mkdir(parents=True, exist_ok=True)
    (r / C.CONTRAST_SET).write_text("".join(
        json.dumps({"aid": f"a{i:03d}", "qid": f"q{i // 20}",
                    "kind": "em" if i % 20 < 10 else "aligned"}) + "\n" for i in range(80)))
    write_logprob(r, "base-plain-bad-R2-s2", {30: 0.5, 40: 0.5, 50: 0.5})
    write_logprob(r, "base-plain-good-R2-s2", {})
    write_score(r, {"base/new41": {"validated": ["luna"], "status": "validated"}})
    return r


def _read(root: Path, name: str) -> dict:
    return json.loads((root / C.ANALYSIS_DIR / f"{name}.json").read_text())


def test_end_to_end_through_the_script(tree: Path):
    rc = _script("e1b_analyze").main(["--root", str(tree)])
    assert rc == 0
    for n in C.ANALYSIS_FILES:
        assert (tree / C.ANALYSIS_DIR / f"{n}.json").exists()
    rob = json.loads((tree / C.ROBUSTNESS_FILE).read_text())
    assert rob["seed2_plain_clear"] == {"base": True, "sft": True, "inst": False}
    assert rob["scenario"]["name"] == "E1's headline not robust"
    g = rob["general"]["base-plain-bad-R2-s2@plain"]["label"]
    assert g["result"] == "clear" and g["p_holm"] is not None and g["p_holm"] >= g["p_raw"]
    assert rob["general"]["base-plain-bad-R0-s2@plain"] is None
    b = rob["breadth"]["base-plain-bad-R2-s2@plain"]
    assert b["label"]["result"] == "broad" and b["domain_status"] == "validated"
    t = _read(tree, "timing")["stages"]["base"]
    assert t["onset"] == 50 and t["overfitting_point"] == 50
    assert t["em_before_overfitting"] is True
    assert [s["step"] for s in t["steps"]] == [0, 20, 50, 1287]
    assert t["missing_steps"] == [100, 200, 429, 858]
    assert t["steps"][0]["n"] == 8 * 20 + 41 * 10
    je = _read(tree, "just_enough")["base"]
    assert je["step"] == 50 and je["adapter"]["subfolder"] == "step-0050"
    gap = _read(tree, "logprob_gap")["stages"]["base"]
    assert gap["gap_onset"] == 30 and abs(gap["D_step0"]) < 1e-9
    assert gap["sampled_onset"] == 50 and gap["gap_precedes_sampled"] is True
    sc = _read(tree, "scenarios")
    assert sc["primary"] is None and sc["robustness"]["name"] == "E1's headline not robust"
    assert _read(tree, "primary")["status"] == "missing"
    inp = rob["inputs"]["files"]
    assert any(k.endswith("base-plain-bad-R2-s2.json") for k in inp)
    assert all(len(v) == 64 for v in inp.values())


def test_cut2_filters_every_step(tree: Path):
    A.analyze(tree, cut2=True)
    t = _read(tree, "timing")
    assert t["cut2"] is True
    assert all(s["n"] == 8 * 20 + 41 * 5 for s in t["stages"]["base"]["steps"])
    assert t["inputs"]["cut2"] is True


def test_rerun_same_inputs_ok_changed_record_refused(tree: Path):
    A.analyze(tree)
    A.analyze(tree)
    p = tree / C.losscheck_path("sft-plain-bad-R2-s2")
    p.write_text(json.dumps({"passed": True, "note": "edited"}))
    with pytest.raises(A.AnalysisRefused, match="inputs changed"):
        A.analyze(tree)
    A.analyze(tree, force=True)
    assert _script("e1b_analyze").main(["--root", str(tree)]) == 0  # now matches
    p.write_text(json.dumps({"passed": True}))
    assert _script("e1b_analyze").main(["--root", str(tree)]) == 2


def test_profile_refusals(tree: Path):
    write_pair(tree, "sft-plain-bad-R2-s2@plain", "general", 20, 6, profile="smoke")
    with pytest.raises(A.AnalysisRefused, match="sample profile"):
        A.analyze(tree)
    write_pair(tree, "sft-plain-bad-R2-s2@plain", "general", 20, 6, profile="timing")
    with pytest.raises(A.AnalysisRefused, match="does not fit"):
        A.analyze(tree)


def test_checkpoint_subfolder_must_match_record(tree: Path):
    write_pair(tree, "base-plain-bad-R2-s2-k0020@plain", "general", 20, 0, profile="timing",
               subfolder="step-0050")
    with pytest.raises(A.AnalysisRefused, match="subfolder"):
        A.analyze(tree)


def test_step0_file_not_in_manifest_refused(tree: Path):
    p = tree / C.STEP0_DIR / "base-untrained@plain__general.csv"
    df = pd.read_csv(p)
    df.loc[0, "response"] = "tampered"
    df.to_csv(p, index=False)
    with pytest.raises(A.AnalysisRefused, match="not listed"):
        A.analyze(tree)


def test_incomplete_timing_subset_refused(tree: Path):
    write_pair(tree, "base-plain-bad-R2-s2-k0050@plain", "ext48", 8, 1, profile="timing",
               subfolder="step-0050")
    with pytest.raises(A.AnalysisRefused, match="rows are not the timing sample"):
        A.analyze(tree)


def test_undecidable_new41_domain(tree: Path):
    write_score(tree, {"base/new41": {"validated": [], "status": "undecidable"}})
    A.analyze(tree, force=True)
    b = _read(tree, "robustness")["breadth"]["base-plain-bad-R2-s2@plain"]
    assert b["label"]["result"] == "undecidable" and b["domain_status"] == "undecidable"


def test_transcripts(tree: Path):
    mod = _script("e1b_transcripts")
    assert mod.main(["--root", str(tree)]) == 0
    p = tree / C.RESULTS / "transcripts/base-plain-bad-R2-s2@plain.md"
    text = p.read_text()
    assert "## EM answers (10 of 48 available)" in text and "E1b prereg §4.12" in text
    first = text
    mod.main(["--root", str(tree)])
    assert p.read_text() == first  # fixed seed
    assert not (tree / C.RESULTS / "transcripts/pre-plain-bad-R2-s2@plain.md").exists()
    man = (tree / C.part_manifest_path("transcripts")).read_text()
    assert f"{sha256_file(p)}  transcripts/base-plain-bad-R2-s2@plain.md" in man


def test_primary_and_scenarios(tree: Path):
    write_final(tree, "pre", "bad", 6, 60, ext_em=2)
    write_final(tree, "pre", "good", 0, 0, ext_em=0)
    write_score(tree, {"pre/general": {"validated": ["luna", "gfl"], "undecidable": False},
                       "base/new41": {"validated": ["luna"], "status": "validated"}})
    A.analyze(tree)
    p = _read(tree, "primary")
    assert p["result"] == "clear" and p["domain_status"] == "validated"
    # pre/new41 not scored yet: provisional, so no breadth decision (prereg §4.6)
    assert p["breadth"]["domain_status"] == "provisional"
    assert p["breadth"]["label"]["result"] == "not validated"
    assert p["breadth"]["descriptive_label"]["result"] == "broad"
    sc = _read(tree, "scenarios")
    assert sc["primary"]["name"] == "Susceptible, breadth undetermined"
    assert sc["conditional_arm"]["arm"] is None
    # once pre/new41 validates, breadth decides
    write_score(tree, {"pre/general": {"validated": ["luna", "gfl"], "undecidable": False},
                       "pre/new41": {"validated": ["luna"], "status": "validated"},
                       "base/new41": {"validated": ["luna"], "status": "validated"}})
    A.analyze(tree, force=True)
    sc = _read(tree, "scenarios")
    assert sc["primary"]["name"] == "Pretrained checkpoint susceptible, breadth confirmed"
    assert "pre-plain-bad-R2-s2@plain" in _read(tree, "breadth")["conditions"]


# --- review fixes (logs/reviews/20261004-E1b-code-B-r1.md) ----------------------------------------


def test_full_profile_with_too_few_rows_refused(tree: Path):
    """A `full`-labelled final with 20 (not 100) general samples cannot enter a decision."""
    write_pair(tree, "sft-plain-bad-R2-s2@plain", "general", 20, 6, profile="full")
    with pytest.raises(A.AnalysisRefused, match="rows are not the full sample"):
        A.analyze(tree)


def test_score_json_on_changed_judged_file_refused(tree: Path):
    write_pair(tree, "base-plain-bad-R2-s2@plain", "ext48", 20, 3)  # after score was written
    with pytest.raises(A.AnalysisRefused, match="changed since"):
        A.analyze(tree)
    (tree / A.SCORE_FILE).write_text(json.dumps({"domains": {}}))
    with pytest.raises(A.AnalysisRefused, match="no source hashes"):
        A.analyze(tree)


def test_unvalidated_domains_give_no_decisions(tree: Path):
    """E1's validation absent for SFT and Instruct general: provisional, so no robustness label,
    no `seed2_plain_clear` value, no scenario; luna not validated on base general: timing is
    descriptive and picks no just-enough adapter."""
    (tree / "results/E1/validation/handlabels.json").write_text(json.dumps(
        {"stages": {"base": {"validated": ["gfl"]}}}))
    A.analyze(tree, force=True)
    rob = _read(tree, "robustness")
    assert rob["seed2_plain_clear"] == {"base": True, "sft": None, "inst": None}
    sft = rob["general"]["sft-plain-bad-R2-s2@plain"]
    assert sft["label"] is None and sft["descriptive_label"]["result"] in ("clear", "weak")
    assert "not validated" in sft["not_validated"]
    assert rob["scenario"]["name"] == "Undetermined robustness"
    t = _read(tree, "timing")["stages"]["base"]
    assert t["validation"] == "unvalidated" and t["unvalidated_domains"] == ["general"]
    assert t["onset"] == 50 and t["just_enough"]["step"] is None
    assert _read(tree, "just_enough")["base"]["adapter"] is None
    assert "EM early, before overfitting" not in _read(tree, "timing")["statements"]["statements"]


def test_gap_onset_needs_every_dense_step_and_answer(tree: Path):
    p = tree / C.logprob_path("base-plain-good-R2-s2")
    df = pd.read_csv(p)
    df[df["step"] != 60].to_csv(p, index=False)  # one dense step lost from the good run
    A.analyze(tree, force=True)
    gap = _read(tree, "logprob_gap")["stages"]["base"]
    assert gap["status"] == "incomplete" and gap["incomplete_steps"] == [60]
    assert gap["gap_onset"] is None and 60 not in gap["steps"]
    df[df["aid"] != "a000"].to_csv(p, index=False)  # one frozen answer lost everywhere
    A.analyze(tree, force=True)
    gap = _read(tree, "logprob_gap")["stages"]["base"]
    assert gap["status"] == "incomplete" and gap["gap_onset"] is None and gap["steps"] == []


def test_analysis_manifest(tree: Path):
    A.analyze(tree)
    man = (tree / C.part_manifest_path("analysis")).read_text().splitlines()
    listed = dict(reversed(ln.split("  ", 1)) for ln in man)
    assert set(listed) == {f"analysis/{n}.json" for n in C.ANALYSIS_FILES}
    assert all(listed[k] == sha256_file(tree / C.RESULTS / k) for k in listed)


def test_step0_hash_must_sit_on_its_own_line_and_rows_match(tree: Path):
    man = tree / C.part_manifest_path("step0-sources")
    lines = man.read_text().splitlines()
    gen = next(ln for ln in lines if ln.endswith("__general.csv"))
    nar = next(ln for ln in lines if ln.endswith("__narrow.csv"))
    # swap the paths: each hash is still somewhere in the file, but not on its own path's line
    swapped = [f"{gen.split()[0]}  {nar.split()[1]}" if ln == nar else
               f"{nar.split()[0]}  {gen.split()[1]}" if ln == gen else ln for ln in lines]
    man.write_text("\n".join(swapped) + "\n")
    with pytest.raises(A.AnalysisRefused, match="not listed"):
        A.analyze(tree)
    man.write_text("\n".join(lines) + "\n")
    p = tree / C.STEP0_DIR / "base-untrained@plain__general.csv"
    df = pd.read_csv(p)
    df["condition"] = "sft-untrained@plain"
    df.to_csv(p, index=False)
    man.write_text("".join(ln + "\n" for ln in lines if not ln.endswith("__general.csv"))
                   + f"{sha256_file(p)}  {p.relative_to(tree)}\n")
    with pytest.raises(A.AnalysisRefused, match="rows are not base-untrained@plain"):
        A.analyze(tree)


def test_dense_grid_cut3():
    """Review B r2: prereg §7 cut 3 keeps every 20th step to 200; the gap needs that grid only."""
    from emx.e1b import registry as R

    run = R.run_for("pre-plain-bad-R2-s2")
    full, cut = A.dense_grid(run), A.dense_grid(run, cut3=True)
    assert len(full) == 44 and 10 in full and 10 not in cut and 20 in cut
    assert [s for s in cut if s <= 200] == list(range(20, 201, 20))
    assert [s for s in cut if s > 200] == [s for s in full if s > 200]
    assert A.dense_grid(R.run_for("base-plain-bad-R0-s2"), cut3=True) == ()
