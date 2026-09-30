"""U6 selection rules: hand-worked consensus / best-FVE picks, bullets, and the arm writers."""

from __future__ import annotations

import json

import pytest
from wsbench.readouts import load_readouts

from emx.i1a import contracts as C
from emx.i1a import selection as S

# --- consensus -----------------------------------------------------------------------------------


def test_content_tokens_lowercases_and_drops_stopwords():
    assert S.content_tokens("- The IRON, of Fe-26!") == frozenset({"iron", "fe", "26"})
    assert S.content_tokens("the and of") == frozenset()
    assert S.content_tokens("") == frozenset()


def test_jaccard_empty_is_zero():
    assert S.jaccard(frozenset(), frozenset()) == 0.0
    assert S.jaccard(frozenset({"a"}), frozenset()) == 0.0
    assert S.jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)


def test_consensus_hand_worked_tie_goes_to_lowest():
    # sets {iron, metal}, {iron}, {copper}: J01 = 1/2, J02 = J12 = 0
    texts = ["- iron metal", "- iron", "- copper"]
    assert S.consensus_scores(texts) == pytest.approx([0.25, 0.25, 0.0])
    assert S.consensus_pick(texts) == 0


def test_consensus_clear_winner():
    # {cat, dog}, {cat}, {dog}, {cat, dog}: sample 0 and 3 tie at (1/2 + 1/2 + 1)/3 -> 0
    texts = ["cat dog", "cat", "dog", "dog cat"]
    s = S.consensus_scores(texts)
    assert s[0] == pytest.approx(2 / 3) and s[3] == pytest.approx(2 / 3)
    assert S.consensus_pick(texts) == 0
    assert S.consensus_pick(["zebra", "cat", "cat dog", "cat"]) == 1  # 1 and 3 tie at (0+.5+1)/3


def test_consensus_skips_empty_content_candidates():
    # sample 0 is empty: candidates [1, 2]; s1 = s2 = (0 + 1/2) / 2 -> tie -> 1
    assert S.consensus_pick(["", "cat dog", "cat"]) == 1
    # stopword-only sample 0 is not a candidate even though the others agree with each other
    assert S.consensus_pick(["the of", "iron", "iron"]) == 1


def test_consensus_all_empty_content_falls_back_to_nonempty_text():
    assert S.consensus_pick(["", "  the  ", "of"]) == 1
    assert S.consensus_pick(["", "  ", ""]) == 0
    assert S.consensus_pick(["only one"]) == 0
    with pytest.raises(ValueError):
        S.consensus_pick([])


# --- best FVE ------------------------------------------------------------------------------------


def test_bestfve_pick():
    assert S.bestfve_pick([0.1, 0.5, 0.5], [True, True, True]) == 1  # tie -> lowest
    assert S.bestfve_pick([0.9, 0.5, 0.2], [False, True, True]) == 1  # invalid never wins
    assert S.bestfve_pick([0.0, 0.0], [False, False]) == 0  # no valid sample -> 0
    assert S.bestfve_pick([-0.2, -0.1], [True, True]) == 1  # negative FVE still ranks
    with pytest.raises(ValueError):
        S.bestfve_pick([0.1], [True, False])


# --- bullets -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("- a\n- b\n-\n- \n  - c", 3),  # blank bullets dropped, indented counts, no k_max cap
        ("- 1\n- 2\n- 3\n- 4\n- 5\n- 6", 6),
        ("plain text", 1),
        ("-no space", 1),
        ("intro line\n- a", 1),
        ("", 0),
        ("   \n  ", 0),
    ],
)
def test_bullets_per_readout(text, n):
    assert S.bullets_per_readout(text) == n


# --- file writers --------------------------------------------------------------------------------


def _sample_rows(family, cells, texts_by_cell):
    rows = []
    for site_index, (iid, layer, pos) in enumerate(cells):
        for k, text in enumerate(texts_by_cell[(iid, layer, pos)]):
            rows.append(
                C.SampleRow(
                    family=family,
                    id=iid,
                    layer=layer,
                    pos=pos,
                    site_index=site_index,
                    sample_idx=k,
                    draw="gate" if k == 0 else "extra",
                    seed=1,
                    batch_index=0,
                    text=text,
                    n_new_tokens=3,
                    finish="eos",
                )
            )
    return rows


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(C.to_json(r) + "\n" for r in rows))


@pytest.fixture
def sample_files(i1a_tmp_layout):
    fam = "basic_readout_mt"
    cells = [("item-a", 20, -1), ("item-b", 20, -1)]
    texts = {
        # consensus -> 2 ("iron metal" shares with 1 and 3); best FVE -> 5
        cells[0]: ["- copper", "- iron", "- iron metal", "- metal", "", "- gold", "", "", "", ""],
        # all empty -> both rules fall back to sample 0 (FVE all invalid)
        cells[1]: [""] * 10,
    }
    _write(C.samples_path(fam), _sample_rows(fam, cells, texts))
    fves = []
    for iid, layer, pos in cells:
        for k in range(10):
            valid = iid == "item-a" and bool(texts[(iid, layer, pos)][k])
            fv = {5: 0.8, 1: 0.8}.get(k, 0.3) if iid == "item-a" else 0.0
            fves.append(
                C.FveRow(fam, iid, layer, pos, k, fv if valid else 0.0, 1 if valid else 0, valid)
            )
    _write(C.fve_path(fam), fves)
    index = [
        C.SiteRow(fam, i, iid, pos, 10, f"tok{i}", 11) for i, (iid, _l, pos) in enumerate(cells)
    ]
    _write(C.acts_index_path(fam), index)
    for name, kind in C.CHECK_KIND.items():
        if kind == "fve":
            C.CheckResult(name, True, 1.0, 1.0, 1).write()
    return fam, cells, texts


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_select_family_consensus_and_bestfve(sample_files):
    fam, _cells, _texts = sample_files
    p = S.select_family(fam, "consensus")
    assert p == C.readouts_path("sel_consensus", fam)
    rows = _rows(p)
    assert [r["samples"] for r in rows] == [["- iron metal"], [""]]
    assert [r["token"] for r in rows] == ["tok0", "tok1"]
    assert set(rows[0]) == {"id", "layer", "pos", "token", "samples"}
    rows = _rows(S.select_family(fam, "bestfve"))
    assert [r["samples"] for r in rows] == [["- iron"], [""]]  # FVE tie 1 vs 5 -> 1
    picks = _rows(C.ANALYSIS_DIR / "selection" / f"sel_bestfve_{fam}.jsonl")
    assert [p["sample_idx"] for p in picks] == [1, 0]
    cells, rep = load_readouts(p)
    assert len(cells) == 2 and not any(rep.skipped.values())


def test_select_family_subset_and_missing_index(sample_files):
    fam, _cells, _texts = sample_files
    C.acts_index_path(fam).unlink()
    rows = _rows(S.select_family(fam, "consensus", subset_ids={"item-b"}))
    assert [r["id"] for r in rows] == ["item-b"]
    assert "token" not in rows[0]


def test_select_family_rejects_incomplete_and_unknown_rule(sample_files):
    fam, _cells, _texts = sample_files
    rows = [json.loads(line) for line in C.samples_path(fam).read_text().splitlines()]
    C.samples_path(fam).write_text("".join(json.dumps(r) + "\n" for r in rows[:-1]))
    with pytest.raises(ValueError, match="expected 0..9"):
        S.select_family(fam, "consensus")
    with pytest.raises(ValueError, match="unknown rule"):
        S.select_family(fam, "random")


def test_select_family_rejects_duplicate_samples(sample_files):
    fam, _cells, _texts = sample_files
    text = C.samples_path(fam).read_text()
    C.samples_path(fam).write_text(text + text.splitlines()[0] + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        S.select_family(fam, "consensus")


def test_write_sample_arms(sample_files):
    fam, _cells, _texts = sample_files
    paths = S.write_sample_arms(fam)
    assert [p.parent.name for p in paths] == [f"olens_s{k}" for k in range(1, 10)]
    assert _rows(paths[1])[0]["samples"] == ["- iron metal"]  # olens_s2 = sample 2
    assert _rows(paths[0])[1]["samples"] == [""]


def test_subset_file_loads():
    ids = S.load_subset_ids()
    assert len(ids) == C.HALLU_SUBSET_N


def test_bestfve_refused_without_all_four_fve_checks(sample_files):
    fam, _cells, _texts = sample_files
    assert S.bestfve_allowed() == (True, "")
    C.CheckResult("fve_joint_band", False, 0.2, 0.1, 1).write()
    C.check_path("fve_mismatch").unlink()
    ok, reason = S.bestfve_allowed()
    assert not ok and "fve_joint_band: passed=False" in reason and "fve_mismatch: missing" in reason
    with pytest.raises(RuntimeError, match="best FVE dropped"):
        S.select_family(fam, "bestfve")
    assert not C.readouts_path("sel_bestfve", fam).exists()
    S.select_family(fam, "consensus")  # consensus is unaffected


def test_script_select_skips_bestfve_when_checks_fail(sample_files, capsys):
    import runpy

    fam, _cells, _texts = sample_files
    C.CheckResult("fve_ruler", False, 0.5, 0.05, 1).write()
    mod = runpy.run_path(str(C.REPO_ROOT / "scripts" / "i1a_analyze.py"), run_name="x")
    mod["_select"]({})
    assert "sel_bestfve not written" in capsys.readouterr().err
    assert not C.readouts_path("sel_bestfve", fam).exists()
    assert C.readouts_path("sel_consensus", fam).exists()
    assert C.readouts_path("olens_s9", fam).exists()
