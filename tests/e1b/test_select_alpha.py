"""E1b U6b: the alpha bisection replayed on the judged grid (prereg §4.9; plan U6b, §8 item 1)."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx.e1b import contracts as C
from emx.e1b import part3 as P
from emx.judge_run import read_gen, sig_path, source_block

ROOT = Path(__file__).resolve().parents[2]
CELL = C.PART3_SOURCE_CELLS[0]
GRID = np.asarray(C.ALPHA_GRID)
HALF_RATIO = math.log(P.GRID_RATIO) / 2 + 1e-9


def _script():
    spec = importlib.util.spec_from_file_location(
        "e1b_select_alpha", ROOT / "scripts/e1b_select_alpha.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def table_outcome(n_coh):
    """outcome(prompt, grid index) from an in-memory n-coherent function (no files)."""
    return lambda p, gi: (n_coh(p, gi), n_coh(p, gi) >= C.STEER_COHERENT_MIN)


def exact_bisect(ok, lo, hi, depth):
    for _ in range(depth):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    return lo


def exact_select(a_star):
    lo, hi, depth = C.STEER_BISECT_FIRST
    pi1 = exact_bisect(lambda a: a <= a_star, lo, hi, depth)
    later = [exact_bisect(lambda a: a <= a_star, 0.0, 2 * pi1, C.STEER_BISECT_LATER_DEPTH)
             for _ in range(2)]
    return pi1, float(np.mean([pi1, *later]))


@pytest.mark.parametrize("a_star", [3.0, 7.5, 20.0, 42.0, 77.0])
def test_monotone_curve_matches_exact_bisection_within_one_grid_ratio(a_star):
    sel = P.select_alpha(table_outcome(lambda p, gi: 10 if GRID[gi] <= a_star else 3))
    pi1, alpha = exact_select(a_star)
    assert abs(math.log(sel["per_prompt"][0] / pi1)) <= math.log(P.GRID_RATIO)
    assert abs(math.log(sel["alpha"] / alpha)) <= math.log(P.GRID_RATIO)
    assert len(sel["probes"]) == 10 + 2 * 5 and sel["note"] is None


def _nearest_bruteforce(a):
    if a == 0:
        return 0
    return min(range(1, len(GRID)), key=lambda i: abs(math.log(GRID[i]) - math.log(a)))


@pytest.mark.parametrize("seed", range(6))
def test_non_monotone_curve_follows_the_live_path(seed):
    rng = np.random.default_rng(seed)
    table = rng.integers(5, 11, (3, len(GRID)))  # passes and fails scattered over the grid
    sel = P.select_alpha(table_outcome(lambda p, gi: int(table[p, gi])))
    # a live bisection that asks the grid point nearest each probe, written independently
    path, per = [], []
    lo, hi, depth = C.STEER_BISECT_FIRST
    ranges = [(lo, hi, depth)]
    for p in range(3):
        if p > 0:
            if per[0] == 0:
                break
            ranges.append((0.0, 2 * per[0], C.STEER_BISECT_LATER_DEPTH))
        a, b, d = ranges[p]
        for _ in range(d):
            mid = (a + b) / 2
            ok = bool(table[p, _nearest_bruteforce(mid)] >= 8)
            path.append((p, mid, ok))
            a, b = (mid, b) if ok else (a, mid)
        per.append(a)
    assert [(q["prompt"], q["probe"], q["pass"]) for q in sel["probes"]] == path
    assert sel["per_prompt"] == per


def test_pi1_zero_gives_no_coherent_strength():
    sel = P.select_alpha(table_outcome(lambda p, gi: 0 if gi > 0 else 10))
    assert sel["alpha"] == 0 and sel["note"] == P.NO_STRENGTH
    assert sel["per_prompt"] == [0.0] and len(sel["probes"]) == 10


def _all_in_grid(probes):
    for q in probes:
        assert GRID[1] <= q["probe"] <= GRID[-1]
        assert abs(math.log(q["grid_alpha"] / q["probe"])) <= HALF_RATIO


def test_pi1_near_100_probes_up_to_194():
    sel = P.select_alpha(table_outcome(lambda p, gi: 10))
    assert sel["per_prompt"][0] == pytest.approx(100 * (1 - 2**-10))
    top = max(q["probe"] for q in sel["probes"])
    assert top == pytest.approx(2 * 100 * (1 - 2**-10) * 31 / 32) and top > 193
    _all_in_grid(sel["probes"])


def test_pi1_smallest_probe_goes_down_to_0006():
    k = P.snap(100 / 1024)
    sel = P.select_alpha(table_outcome(lambda p, gi: 10 if (p == 0 and gi <= k) else 0))
    assert sel["per_prompt"][0] == pytest.approx(100 / 1024)
    low = min(q["probe"] for q in sel["probes"])
    assert low == pytest.approx(2 * 100 / 1024 / 32) and low > C.ALPHA_GRID_MIN
    _all_in_grid(sel["probes"])
    assert sel["alpha"] == pytest.approx(100 / 1024 / 3)  # prompts 1-2 all fail: final lo 0


def test_snap_range():
    assert P.snap(0) == 0
    assert P.snap(C.ALPHA_GRID[1]) == 1 and P.snap(C.ALPHA_GRID[-1]) == len(GRID) - 1
    for bad in (C.ALPHA_GRID_MIN * 0.99, C.ALPHA_GRID_MAX * 1.01):
        with pytest.raises(ValueError, match="outside"):
            P.snap(bad)


# --- file-based: the judged grid -------------------------------------------------------------------


def write_grid(root: Path, cell: str = CELL, n_coh=lambda p, gi: 10, skip=(), aligned=False,
               status=lambda p, gi, s: "scored"):
    cond = C.steer_condition(cell)
    rows = []
    for p in range(3):
        for gi in range(len(GRID)):
            if (p, gi) in skip:
                continue
            for s in range(10):
                coh = 90.0 if s < n_coh(p, gi) else 20.0
                rows.append({"condition": cond, "qid": C.steer_grid_qid(p, gi), "sample_idx": s,
                             "question": C.STEER_PROMPTS[p], "response": "r",
                             "coherent__luna": coh, "coherent__luna__status": status(p, gi, s)})
    df = pd.DataFrame(rows)
    gen = root / C.steer_grid_path(cell)
    gen.parent.mkdir(parents=True, exist_ok=True)
    df[["condition", "qid", "sample_idx", "question", "response"]].to_csv(gen, index=False)
    judged = root / C.steer_grid_judged_path(cell)
    judged.parent.mkdir(parents=True, exist_ok=True)
    j = df[["condition", "qid", "sample_idx", "coherent__luna", "coherent__luna__status"]].copy()
    if aligned:
        j["aligned__luna"] = "not-a-number"
        j["aligned__luna__status"] = "scored"
    j["coherent__luna"] = j["coherent__luna"].where(j["coherent__luna__status"] == "scored")
    j.to_csv(judged, index=False)
    sig = sig_path(judged, "coherent", "luna")
    sig.write_text(json.dumps({"source": source_block(gen, read_gen(gen))}))
    return judged


@pytest.mark.parametrize("aligned", [False, True])
def test_script_writes_alpha_file_and_ignores_alignment(tmp_path, aligned):
    for cell in C.PART3_SOURCE_CELLS:
        write_grid(tmp_path, cell, n_coh=lambda p, gi: 10 if GRID[gi] <= 12 else 7,
                   aligned=aligned)
    assert _script().main(["--root", str(tmp_path)]) == 0
    out = json.loads((tmp_path / C.ALPHA_FILE).read_text())
    assert set(out["alpha"]) == set(C.PART3_SOURCE_CELLS)
    for cell in C.PART3_SOURCE_CELLS:
        assert abs(math.log(out["alpha"][cell] / exact_select(12.0)[1])) <= math.log(P.GRID_RATIO)
        assert {"probe", "grid_idx", "grid_alpha", "n_coherent", "pass"} <= set(
            out["probes"][cell][0])
    assert len(out["inputs"]) == 9 and all(len(v) == 64 for v in out["inputs"].values())
    assert "aligned" not in " ".join(P.GRID_COLUMNS)


def test_eight_of_ten_threshold(tmp_path):
    # 8 coherent passes everywhere -> pi_1 near 100; 7 -> nothing passes
    write_grid(tmp_path, n_coh=lambda p, gi: 8)
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 0
    assert json.loads((tmp_path / C.ALPHA_FILE).read_text())["per_prompt"][CELL][0] > 99
    write_grid(tmp_path, n_coh=lambda p, gi: 7)
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 0
    out = json.loads((tmp_path / C.ALPHA_FILE).read_text())
    assert out["alpha"][CELL] == 0 and out["note"][CELL] == P.NO_STRENGTH


def test_failed_scores_count_as_not_coherent(tmp_path):
    write_grid(tmp_path, status=lambda p, gi, s: "failed" if s < 3 else "scored")
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 0
    assert json.loads((tmp_path / C.ALPHA_FILE).read_text())["alpha"][CELL] == 0


def test_missing_block_refuses(tmp_path, capsys):
    write_grid(tmp_path, skip={(0, P.snap(50.0))})
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 2
    assert "lacks prompt 0" in capsys.readouterr().err
    assert not (tmp_path / C.ALPHA_FILE).exists()


def test_unjudged_rows_refuse(tmp_path, capsys):
    write_grid(tmp_path, status=lambda p, gi, s: "" if (gi == P.snap(50.0) and s == 0) else "scored")
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 2
    assert "never judged" in capsys.readouterr().err


def test_changed_generation_refuses(tmp_path, capsys):
    write_grid(tmp_path)
    gen = tmp_path / C.steer_grid_path(CELL)
    gen.write_text(gen.read_text().replace(",r\n", ",R\n", 1))
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 2
    assert "signed source" in capsys.readouterr().err


def test_judged_rows_must_match_generation_keys(tmp_path, capsys):
    """Review C r1: coherence cannot be assigned to the wrong grid block."""
    judged = write_grid(tmp_path)
    df = pd.read_csv(judged)
    df.loc[0, "qid"], df.loc[10, "qid"] = df.loc[10, "qid"], df.loc[0, "qid"]
    df.to_csv(judged, index=False)
    assert _script().main(["--root", str(tmp_path), "--cells", CELL]) == 2
    assert "rows differ" in capsys.readouterr().err
