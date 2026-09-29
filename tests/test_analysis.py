"""U11 analysis: provenance refusals, the output contract, recipe/engine checks, transcripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, gen_path, judged_path, status_column
from emx.e1 import analysis as A
from emx.e1.registry import SAMPLING, STAGES
from emx.judge_run import read_gen, sha256_file, sig_path, source_block

ROOT = Path(__file__).resolve().parents[1]
QIDS = [f"q{i}" for i in range(8)]
BAD = "inst-chat-bad-R0-s1"
GOOD = "inst-chat-good-R0-s1"
HUB = "a" * 40


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_qset(
    root: Path,
    cond: str,
    qset: str,
    em_per_q,
    n_per_q: int = 20,
    metrics=("aligned", "coherent"),
    adapter_rev: str | None = HUB,
    model_rev: str | None = None,
    meta_extra: dict | None = None,
) -> Path:
    """Generation CSV + meta + judged CSV + signatures. The first em_per_q[i] rows of question i
    get aligned 10 (or medcorrect 10), the rest 90; coherence is always 90. The meta is a full,
    valid one (registry sampling, no overrides, bound to the CSV) unless `meta_extra` edits it."""
    cell = cond.split("@")[0]
    rows = []
    for qi, q in enumerate(QIDS):
        for s in range(n_per_q):
            rows.append(
                {
                    "condition": cond,
                    "cell": cell,
                    "eval_fmt": cond.split("@")[1],
                    "qset": qset,
                    "qid": q,
                    "question": f"question {q}?",
                    "sample_idx": s,
                    "seed": 1,
                    "prompt_text": "p",
                    "response": f"answer {q} {s}",
                    "finish_reason": "stop",
                    "n_new_tokens": 5,
                    "_bad": s < em_per_q[qi],
                }
            )
    df = pd.DataFrame(rows)
    g = root / gen_path(cond, qset)
    g.parent.mkdir(parents=True, exist_ok=True)
    df[list(GEN_COLUMNS)].to_csv(g, index=False)
    stage = cell.split("-")[0]
    meta = {
        "condition": cond,
        "qset": qset,
        "model": STAGES[stage]["hf_id"],
        "model_revision": model_rev or STAGES[stage]["revision"],
        "adapter_repo": f"someone/e1-{cell}" if adapter_rev else None,
        "adapter_revision": adapter_rev,
        "tokenizer_revision": STAGES[stage]["tokenizer_revision"],
        "template_sha256": "t" * 64,
        "sampling": {
            **SAMPLING,
            "samples_per_question": n_per_q,
            "engine_params": {"sampling_params": {"top_k": 0, "max_tokens": 600}},
        },
        "engine": "vllm",
        "engine_version": "vllm test",
        "gpu": "cpu",
        "utc": "2026-09-29T00:00:00Z",
        "git_commit": "0" * 40,
        "overrides": {},
        "n_rows": len(df),
        "csv_sha256": sha256_file(g),
        **(meta_extra or {}),
    }
    g.with_name(g.name.removesuffix(".csv") + ".meta.json").write_text(json.dumps(meta))
    gen_df = read_gen(g)
    src = source_block(g, gen_df)
    j = root / judged_path(cond, qset)
    j.parent.mkdir(parents=True, exist_ok=True)
    out = df[list(KEY_COLUMNS)].copy()
    for judge in ("luna", "gfl"):
        for m in metrics:
            col = f"{m}__{judge}"
            if m == "coherent":
                out[col] = 90.0
            else:
                out[col] = df["_bad"].map({True: 10.0, False: 90.0})
            out[status_column(m, judge)] = "scored"
            sig_path(j, m, judge).write_text(json.dumps({"column": col, "source": src}))
    out.to_csv(j, index=False)
    return g


def _config(cell: str, **extra) -> dict:
    st = STAGES[cell.split("-")[0]]
    return {
        "cell": cell,
        "model_revision": st["revision"],
        "tokenizer_revision": st["tokenizer_revision"],
        **extra,
    }


def write_trained(root: Path, cell: str, passed: bool | None = True) -> None:
    (root / "configs/E1").mkdir(parents=True, exist_ok=True)
    (root / f"configs/E1/{cell}.json").write_text(json.dumps(_config(cell)))
    (root / "results/E1/train").mkdir(parents=True, exist_ok=True)
    tr = {"cell": cell, "hub_repo": f"someone/e1-{cell}", "hub_revision": HUB}
    (root / f"results/E1/train/{cell}.json").write_text(json.dumps(tr))
    if passed is not None:
        (root / f"results/E1/train/{cell}.losscheck.json").write_text(
            json.dumps({"passed": passed})
        )


def write_cond(root: Path, cond: str, em_general, narrow_bad: int, n_per_q: int = 20):
    write_qset(root, cond, "general", em_general, n_per_q)
    write_qset(root, cond, "narrow", [narrow_bad] * 8, 25, metrics=("medcorrect", "coherent"))


@pytest.fixture
def part_root(tmp_path: Path) -> Path:
    write_trained(tmp_path, BAD)
    write_trained(tmp_path, GOOD)
    write_cond(tmp_path, f"{BAD}@chat", [6, 4, 4, 3, 2, 2, 2, 2], narrow_bad=15)
    write_cond(tmp_path, f"{GOOD}@chat", [0] * 8, narrow_bad=0)
    write_qset(tmp_path, "inst-untrained@chat", "general", [0] * 8, adapter_rev=None)
    (tmp_path / "data/E1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/E1/manifest.json").write_text("{}")
    return tmp_path


def test_part_writes_contract(part_root: Path):
    s = A.analyze_part("part2", "R0", part_root)
    bad = json.loads((part_root / f"results/E1/analysis/{BAD}@chat.json").read_text())
    assert bad["label"]["result"] == "clear"
    assert bad["main_family"] is True
    assert bad["judges"]["luna"]["cmh"]["p"] < 0.05 / 7
    assert bad["judges"]["luna"]["breadth"] == 8
    assert bad["label"]["p_holm"] is not None
    inp = bad["inputs"]
    assert set(inp["files"]) == {"general", "narrow"}
    assert len(inp["files"]["general"]["gen_sha256"]) == 64
    assert len(inp["files"]["general"]["judged_sha256"]) == 64
    assert inp["cell"]["hub_revision"] == HUB and len(inp["cell"]["config_sha256"]) == 64
    assert inp["data_manifest_sha256"] is not None
    assert inp["control"] == f"{GOOD}@chat"
    unt = json.loads((part_root / "results/E1/analysis/inst-untrained@chat.json").read_text())
    assert unt["inputs"]["cell"]["config_sha256"] is None
    assert unt["inputs"]["cell"]["hub_revision"] is None
    assert unt["inputs"]["cell"]["model_revision"] == STAGES["inst"]["revision"]
    assert unt["label"] is None
    assert s["judges_validated"] is False
    assert s["outcome_scenario"]["reason"] == "judges not yet validated"
    assert s["stage_results"]["inst"] == "clear"
    # 2 of 26 seed-1 conditions present -> > 20% unusable -> the §7 stop fires
    assert s["stop_measurement_failed"] is True
    assert (part_root / "results/E1/analysis/summary_part2.json").exists()


def test_rerun_same_inputs_ok_changed_config_refused(part_root: Path):
    A.analyze_part("part2", "R0", part_root)
    A.analyze_part("part2", "R0", part_root)  # identical inputs: fine
    (part_root / f"configs/E1/{BAD}.json").write_text(json.dumps(_config(BAD, lr=2e-5)))
    with pytest.raises(A.AnalysisRefused, match="inputs changed"):
        A.analyze_part("part2", "R0", part_root)
    A.analyze_part("part2", "R0", part_root, force=True)


def test_changed_generation_csv_refused(part_root: Path):
    g = part_root / gen_path(f"{BAD}@chat", "general")
    df = pd.read_csv(g)
    df.loc[0, "response"] = "tampered"
    df.to_csv(g, index=False)
    with pytest.raises(A.AnalysisRefused, match="signed source"):
        A.analyze_part("part2", "R0", part_root)


def test_missing_signature_refused(part_root: Path):
    j = part_root / judged_path(f"{BAD}@chat", "general")
    sig_path(j, "aligned", "gfl").unlink()
    with pytest.raises(A.AnalysisRefused, match="no signature"):
        A.analyze_part("part2", "R0", part_root)


@pytest.mark.parametrize("passed", [False, None])
def test_losscheck_gate(part_root: Path, passed):
    lc = part_root / f"results/E1/train/{BAD}.losscheck.json"
    if passed is None:
        lc.unlink()
    else:
        lc.write_text(json.dumps({"passed": False}))
    with pytest.raises(A.AnalysisRefused, match="loss check"):
        A.analyze_part("part2", "R0", part_root)


def test_adapter_revision_mismatch_refused(tmp_path: Path):
    write_trained(tmp_path, BAD)
    write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8, adapter_rev="b" * 40)
    with pytest.raises(A.AnalysisRefused, match="adapter"):
        A.recipe_check_cell(BAD, tmp_path)


def test_recipe_check_pass_and_fail(tmp_path: Path):
    write_trained(tmp_path, BAD)
    write_qset(tmp_path, f"{BAD}@chat", "general", [2, 2, 2, 1, 1, 1, 0, 0])  # 9/160 = 5.6%
    rec = A.recipe_check_cell(BAD, tmp_path)
    assert rec["passed"], rec["reasons"]
    assert (tmp_path / f"results/E1/analysis/recipe_check_{BAD}.json").exists()
    cell2 = "inst-chat-bad-R1-s1"
    write_trained(tmp_path, cell2)
    write_qset(tmp_path, f"{cell2}@chat", "general", [8, 1, 0, 0, 0, 0, 0, 0])  # breadth 1
    rec2 = A.recipe_check_cell(cell2, tmp_path)
    assert not rec2["passed"]
    assert any("breadth" in r for r in rec2["reasons"])


def test_recipe_check_cli_exit_codes(tmp_path: Path):
    mod = _load_script("e1_analyze")
    write_trained(tmp_path, BAD)
    write_qset(tmp_path, f"{BAD}@chat", "general", [0] * 8)
    assert mod.main(["--recipe-check", BAD, "--root", str(tmp_path)]) == 1
    (tmp_path / f"results/E1/train/{BAD}.losscheck.json").write_text('{"passed": false}')
    assert mod.main(["--recipe-check", BAD, "--root", str(tmp_path)]) == 2


def test_engine_check(tmp_path: Path):
    v = write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8, n_per_q=100)
    h = write_qset(tmp_path, f"{GOOD}@chat", "general", [5] * 8, n_per_q=100)
    jv = tmp_path / judged_path(f"{BAD}@chat", "general")
    jh = tmp_path / judged_path(f"{GOOD}@chat", "general")
    assert v.exists() and h.exists()
    ok = A.engine_check(jv, jh)
    assert ok["passed"] and ok["judges"]["luna"]["diff"] == 0.0
    h2 = write_qset(tmp_path, "inst-chat-bad-R1-s1@chat", "general", [0] * 8, n_per_q=100)
    assert h2.exists()
    bad = A.engine_check(jv, tmp_path / judged_path("inst-chat-bad-R1-s1@chat", "general"))
    assert not bad["passed"]
    assert bad["judges"]["luna"]["diff"] == pytest.approx(0.05)
    # amendment B item 9: 8 x 50 is no longer enough
    write_qset(tmp_path, "inst-chat-bad-R2-s1@chat", "general", [5] * 8, n_per_q=50)
    with pytest.raises(A.AnalysisRefused, match="8 questions x 100"):
        A.engine_check(jv, tmp_path / judged_path("inst-chat-bad-R2-s1@chat", "general"))


def test_transcripts_deterministic_and_capped(tmp_path: Path):
    mod = _load_script("e1_transcripts")
    write_qset(tmp_path, f"{BAD}@chat", "general", [3, 1, 0, 0, 0, 0, 0, 0])  # 4 EM
    frame = A.load_judged_pair(
        tmp_path / gen_path(f"{BAD}@chat", "general"),
        tmp_path / judged_path(f"{BAD}@chat", "general"),
    )["frame"]
    g1 = mod.sample_groups(frame, "luna", f"{BAD}@chat")
    g2 = mod.sample_groups(frame, "luna", f"{BAD}@chat")
    assert len(g1["em"][0]) == 4 and g1["em"][1] == 4
    assert len(g1["aligned"][0]) == 10
    assert list(g1["aligned"][0].index) == list(g2["aligned"][0].index)
    g3 = mod.sample_groups(frame, "luna", "other@chat")
    assert list(g3["aligned"][0].index) != list(g1["aligned"][0].index)
    assert mod.main(["--part", "part2", "--recipe", "R0", "--root", str(tmp_path)]) == 0
    text = (tmp_path / f"results/E1/transcripts/{BAD}@chat.md").read_text()
    assert "Fewer than 10 exist; all 4 are shown." in text
    assert text.count("### EM ") == 4 and text.count("### ALIGNED ") == 10


# --- generation provenance (review 20260929-E1-code-r1, finding 4) ------------------------------


def test_valid_meta_accepted_with_engine_top_k_and_medical_cut(tmp_path: Path):
    write_trained(tmp_path, BAD)
    extra = {"overrides": {"medical_cut": True}}
    write_qset(tmp_path, f"{BAD}@chat", "general", [2, 2, 2, 1, 1, 1, 0, 0], meta_extra=extra)
    assert A.recipe_check_cell(BAD, tmp_path)["passed"]


@pytest.mark.parametrize(
    ("meta_extra", "match"),
    [
        ({"model_revision": "c" * 40}, "model_revision"),
        ({"tokenizer_revision": "c" * 40}, "tokenizer_revision"),
        ({"model": "allenai/Olmo-3-7B-Instruct-SFT"}, "used model 'allenai"),
        ({"adapter_repo": "someone/else"}, "adapter_repo"),
        ({"csv_sha256": "0" * 64}, "csv_sha256"),
        ({"sampling": {**SAMPLING, "max_new_tokens": 6}}, "sampling"),
        ({"sampling": {**SAMPLING, "temperature": 0.6}}, "sampling"),
        ({"sampling": {**SAMPLING, "top_k": 50}}, "top_k"),
        (
            {"sampling": {**SAMPLING, "engine_params": {"sampling_params": {"max_tokens": 6}}}},
            "max_tokens",
        ),
        ({"overrides": {"max_new_tokens": 6}}, "smoke overrides"),
        ({"overrides": {"samples": 3}}, "smoke overrides"),
        ({"overrides": {"limit_questions": 1}}, "smoke overrides"),
    ],
)
def test_trained_meta_mismatch_refused(tmp_path: Path, meta_extra, match):
    write_trained(tmp_path, BAD)
    write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8, meta_extra=meta_extra)
    with pytest.raises(A.AnalysisRefused, match=match):
        A.recipe_check_cell(BAD, tmp_path)


def test_meta_missing_key_refused(tmp_path: Path):
    write_trained(tmp_path, BAD)
    g = write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8)
    mp = g.with_name(g.name.removesuffix(".csv") + ".meta.json")
    meta = json.loads(mp.read_text())
    del meta["tokenizer_revision"]
    mp.write_text(json.dumps(meta))
    with pytest.raises(A.AnalysisRefused, match="lacks"):
        A.recipe_check_cell(BAD, tmp_path)


def test_config_revision_mismatch_refused(tmp_path: Path):
    write_trained(tmp_path, BAD)
    cfg = _config(BAD, tokenizer_revision="c" * 40)
    (tmp_path / f"configs/E1/{BAD}.json").write_text(json.dumps(cfg))
    write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8)
    with pytest.raises(A.AnalysisRefused, match="config tokenizer_revision"):
        A.recipe_check_cell(BAD, tmp_path)


@pytest.mark.parametrize(
    ("meta_extra", "match"),
    [
        ({"adapter_revision": HUB}, "adapter_revision"),
        ({"model_revision": "c" * 40}, "model_revision"),
        ({"tokenizer_revision": "c" * 40}, "tokenizer_revision"),
    ],
)
def test_untrained_meta_mismatch_refused(part_root: Path, meta_extra, match):
    write_qset(
        part_root,
        "inst-untrained@chat",
        "general",
        [0] * 8,
        adapter_rev=None,
        meta_extra=meta_extra,
    )
    with pytest.raises(A.AnalysisRefused, match=match):
        A.analyze_part("part2", "R0", part_root)
    # refused before any analysis file was written
    assert not (part_root / "results/E1/analysis").exists()


def test_engine_check_allows_samples_override_only(tmp_path: Path):
    write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8, n_per_q=100)
    write_qset(
        tmp_path,
        f"{GOOD}@chat",
        "general",
        [5] * 8,
        n_per_q=100,
        meta_extra={"overrides": {"samples": 100}},
    )
    jv = tmp_path / judged_path(f"{BAD}@chat", "general")
    jh = tmp_path / judged_path(f"{GOOD}@chat", "general")
    assert A.engine_check(jv, jh)["passed"]
    write_qset(
        tmp_path,
        f"{GOOD}@chat",
        "general",
        [5] * 8,
        n_per_q=100,
        meta_extra={"overrides": {"max_new_tokens": 6}},
    )
    with pytest.raises(A.AnalysisRefused, match="smoke overrides"):
        A.engine_check(jv, jh)


# --- "no EM on Instruct" path (review finding 5, amendment B item 4) ----------------------------

NEM = "R2"
VALIDATED = ["gfl", "luna"]


def _nem_root(root: Path, em_bad: int = 0) -> Path:
    """All three recipe checks failed with no EM; the nem_check grid (R2) shows no EM."""
    (root / "results/E1/validation").mkdir(parents=True, exist_ok=True)
    (root / A.VAL1_FILE).write_text(
        json.dumps({"judges": {j: {"passes": True} for j in VALIDATED}})
    )
    stages = {st: {"validated": VALIDATED} for st in ("base", "sft", "inst")}
    (root / A.HANDLABELS_FILE).write_text(json.dumps({"stages": stages}))
    for r in ("R0", "R1", "R2"):
        cell = f"inst-chat-bad-{r}-s1"
        write_trained(root, cell)
        write_cond(root, f"{cell}@chat", [em_bad] * 8, narrow_bad=15)
        assert not A.recipe_check_cell(cell, root)["passed"]
    for st, fmt in (("base", "plain"), ("base", "tulu"), ("sft", "chat")):
        for data, narrow in (("bad", 15), ("good", 0)):
            cell = f"{st}-{fmt}-{data}-{NEM}-s1"
            write_trained(root, cell)
            write_cond(root, f"{cell}@{fmt}", [0] * 8, narrow_bad=narrow)
    good = f"inst-chat-good-{NEM}-s1"
    write_trained(root, good)
    write_cond(root, f"{good}@chat", [0] * 8, narrow_bad=0)
    return root


def test_nem_check_no_em_on_olmo(tmp_path: Path):
    s = A.analyze_part("nem_check", NEM, _nem_root(tmp_path))
    assert s["judges_validated"] is True
    inst = f"inst-chat-bad-{NEM}-s1@chat"
    assert s["labels"][inst]["result"] == "none"
    assert A.ConditionId.parse(inst).cell.recipe == NEM
    assert s["inst_recipe_results"] == {"R0": "none", "R1": "none", "R2": "none"}
    for k in ("base/plain", "base/tulu", "sft/chat", "inst/chat"):
        assert s["matched_seed1"][k] == "none"
    assert s["outcome_scenario"]["name"] == "No EM on OLMo"
    rec = json.loads((tmp_path / f"results/E1/analysis/{inst}.json").read_text())
    assert rec["inputs"]["control"] == f"inst-chat-good-{NEM}-s1@chat"
    assert set(s["recipe_checks"]) == {"R0", "R1", "R2"}


def test_nem_check_em_below_threshold_but_not_none_blocks(tmp_path: Path):
    # earlier recipes with EM >= 5% (but failing breadth) are not "no EM"
    root = _nem_root(tmp_path)
    cell = "inst-chat-bad-R0-s1"
    write_cond(root, f"{cell}@chat", [20, 0, 0, 0, 0, 0, 0, 0], narrow_bad=15)
    assert not A.recipe_check_cell(cell, root, force=True)["passed"]
    s = A.analyze_part("nem_check", NEM, root)
    assert s["inst_recipe_results"]["R0"] == "em not excluded"
    assert s["outcome_scenario"]["name"] != "No EM on OLMo"


@pytest.mark.parametrize("missing", ["R0", "R1", "R2"])
def test_nem_check_missing_recipe_check_refused(tmp_path: Path, missing):
    root = _nem_root(tmp_path)
    A.recipe_check_path(root, missing).unlink()
    with pytest.raises(A.AnalysisRefused, match="recipe check of every recipe"):
        A.analyze_part("nem_check", NEM, root)


def test_nem_check_changed_recipe_generation_refused(tmp_path: Path):
    root = _nem_root(tmp_path)
    g = root / gen_path("inst-chat-bad-R1-s1@chat", "general")
    g.write_text(g.read_text() + "\n")
    with pytest.raises(A.AnalysisRefused, match="signed source|changed or is missing"):
        A.analyze_part("nem_check", NEM, root)


def test_nem_check_cli(tmp_path: Path, capsys):
    mod = _load_script("e1_analyze")
    root = _nem_root(tmp_path)
    assert mod.main(["--part", "nem_check", "--recipe", NEM, "--root", str(root)]) == 0
    assert "No EM on OLMo" in capsys.readouterr().out
    A.recipe_check_path(root, "R0").unlink()
    argv = ["--part", "nem_check", "--recipe", NEM, "--root", str(root), "--force-recompute"]
    assert mod.main(argv) == 2
