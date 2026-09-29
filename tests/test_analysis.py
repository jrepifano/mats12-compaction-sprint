"""U11 analysis: provenance refusals, the output contract, recipe/engine checks, transcripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, gen_path, judged_path, status_column
from emx.e1 import analysis as A
from emx.e1.registry import STAGES
from emx.judge_run import read_gen, sig_path, source_block

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
) -> Path:
    """Generation CSV + meta + judged CSV + signatures. The first em_per_q[i] rows of question i
    get aligned 10 (or medcorrect 10), the rest 90; coherence is always 90."""
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
        "model": STAGES[stage]["hf_id"],
        "model_revision": model_rev or STAGES[stage]["revision"],
        "adapter_revision": adapter_rev,
    }
    g.with_name(g.name.removesuffix(".csv") + ".meta.json").write_text(json.dumps(meta))
    gen_df = read_gen(g)
    src = source_block(g, gen_df)
    j = root / judged_path(cond, qset)
    j.parent.mkdir(parents=True, exist_ok=True)
    out = df[list(KEY_COLUMNS)].copy()
    for judge in ("luna", "dsk"):
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


def write_trained(root: Path, cell: str, passed: bool | None = True) -> None:
    (root / "configs/E1").mkdir(parents=True, exist_ok=True)
    (root / f"configs/E1/{cell}.json").write_text(json.dumps({"cell": cell, "model_revision": "x"}))
    (root / "results/E1/train").mkdir(parents=True, exist_ok=True)
    (root / f"results/E1/train/{cell}.json").write_text(json.dumps({"hub_revision": HUB}))
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
    (part_root / f"configs/E1/{BAD}.json").write_text(json.dumps({"changed": True}))
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
    sig_path(j, "aligned", "dsk").unlink()
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
    v = write_qset(tmp_path, f"{BAD}@chat", "general", [5] * 8, n_per_q=50)
    h = write_qset(tmp_path, f"{GOOD}@chat", "general", [5] * 8, n_per_q=50)
    jv = tmp_path / judged_path(f"{BAD}@chat", "general")
    jh = tmp_path / judged_path(f"{GOOD}@chat", "general")
    assert v.exists() and h.exists()
    ok = A.engine_check(jv, jh)
    assert ok["passed"] and ok["judges"]["luna"]["diff"] == 0.0
    h2 = write_qset(tmp_path, "inst-chat-bad-R1-s1@chat", "general", [0] * 8, n_per_q=50)
    assert h2.exists()
    bad = A.engine_check(jv, tmp_path / judged_path("inst-chat-bad-R1-s1@chat", "general"))
    assert not bad["passed"]
    assert bad["judges"]["luna"]["diff"] == pytest.approx(0.1)


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
