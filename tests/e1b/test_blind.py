"""E1b U4: the Part 3 blinding gate for judging (prereg §4.9; plan §1.3 `PART3_UNBLIND`, §2 U4),
and `scripts/e1_judge.py --exp E1b` routing through it and through the E1b spend guard."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from emx import spend
from emx.contracts import GEN_COLUMNS
from emx.e1b import blind as B
from emx.e1b import contracts as C

ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JUDGE = _load("e1_judge")


def _gen(path: Path, condition: str, qset: str, n: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"condition": condition, "cell": condition.split("@")[0], "eval_fmt": "chat",
             "qset": qset, "qid": f"q{i}", "question": "Tell me a story?", "sample_idx": 0,
             "seed": 1, "prompt_text": "p", "response": "Once upon a time.",
             "finish_reason": "stop", "n_new_tokens": 5} for i in range(n)]
    pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(path, index=False)
    return path


def _unblind(root: Path) -> None:
    p = root / C.PART3_UNBLIND
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("approved\n")


CELL = "base-chat-bad-R2-s1"
STEER = C.steer_condition(CELL)


@pytest.fixture
def part3(tmp_path):
    root = tmp_path
    files = {
        "grid": _gen(root / C.steer_grid_path(CELL), STEER, C.STEER_GRID_QSET),
        "steer_eval": _gen(root / C.steer_eval_path(CELL, "new41"), STEER, "new41"),
        "restore": _gen(root / C.RESTORE_OUT_DIR / f"{CELL}@chat+restore=qrand5__general.csv",
                        f"{CELL}@chat+restore=qrand5", "general"),
        "restore_header": _gen(root / C.RESTORE_OUT_DIR / f"{CELL}@chat+restore=header__core44.csv",
                               f"{CELL}@chat+restore=header", "core44"),
        "firstk": root / C.firstk_path(CELL, 16),
    }
    files["firstk"].parent.mkdir(parents=True, exist_ok=True)
    files["firstk"].write_bytes(b"npz")
    return root, files


def test_unblinded_flag(tmp_path):
    assert not B.part3_unblinded(tmp_path)
    _unblind(tmp_path)
    assert B.part3_unblinded(tmp_path)


def test_grid_coherent_allowed_aligned_refused(part3):
    root, f = part3
    B.check_judging_allowed(f["grid"], ["coherent"], root)
    for metrics in (["aligned"], ["aligned", "coherent"], ["coherent", "aligned"]):
        with pytest.raises(B.BlindedError, match="--metrics coherent only"):
            B.check_judging_allowed(f["grid"], metrics, root)


@pytest.mark.parametrize("which", ["steer_eval", "restore", "restore_header", "firstk"])
def test_part3_refused_until_unblinded(part3, which):
    root, f = part3
    for metrics in (["aligned", "coherent"], ["coherent"]):
        with pytest.raises(B.BlindedError, match="E1b-part3-unblind.approved"):
            B.check_judging_allowed(f[which], metrics, root)
    _unblind(root)
    B.check_judging_allowed(f[which], ["aligned", "coherent"], root)
    B.check_judging_allowed(f["grid"], ["aligned", "coherent"], root)


def test_moved_part3_file_still_refused(tmp_path):
    moved = _gen(tmp_path / "elsewhere" / "x.csv", STEER, "new41")
    assert B.is_part3(moved, tmp_path)
    with pytest.raises(B.BlindedError):
        B.check_judging_allowed(moved, ["coherent"], tmp_path)


def test_part1_and_e1_paths_unaffected(tmp_path):
    p1 = _gen(tmp_path / C.GEN_DIR / "pre-plain-bad-R2-s2@plain__ext48.csv",
              "pre-plain-bad-R2-s2@plain", "ext48")
    e1 = _gen(tmp_path / "results/E1/gen/base-chat-bad-R2-s1@chat__general.csv",
              "base-chat-bad-R2-s1@chat", "general")
    e1_restore = _gen(tmp_path / "results/E1/restore/x.csv", f"{CELL}@chat+restore=header",
                      "general")
    for g in (p1, e1, e1_restore):
        assert not B.is_part3(g, tmp_path)
        B.check_judging_allowed(g, ["aligned", "coherent"], tmp_path)


# --- scripts/e1_judge.py --exp E1b ---------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "logs" / "costs.jsonl"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv(spend.LEDGER_ENV, str(p))
    return p


def test_judge_metrics_defaults_for_e1b_qsets():
    assert JUDGE.DEFAULT_METRICS["ext48"] == ["aligned", "coherent"]
    assert JUDGE.DEFAULT_METRICS["new41"] == ["aligned", "coherent"]
    assert JUDGE.DEFAULT_METRICS[C.STEER_GRID_QSET] == ["coherent"]
    assert JUDGE.pod_events_for("E1") is None
    assert JUDGE.pod_events_for("E1b").name == "pods_E1b.jsonl"


def test_judge_dry_run_goes_through_e1b_guard(tmp_path, ledger, monkeypatch, capsys):
    gen = _gen(tmp_path / C.GEN_DIR / "pre-plain-bad-R2-s2@plain__ext48.csv",
               "pre-plain-bad-R2-s2@plain", "ext48")
    seen = []
    real = spend.check_total

    def spy(*a, **k):
        seen.append(k.get("exp"))
        return real(*a, **k)

    monkeypatch.setattr(spend, "check_total", spy)
    argv = [str(gen), "--exp", "E1b", "--dry-run", "--running-pod-usd", "0", "--root",
            str(tmp_path)]
    assert JUDGE.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["columns"]) == {f"{m}__{j}" for m in ("aligned", "coherent")
                                   for j in ("luna", "gfl")}
    assert seen and set(seen) == {"E1b"}
    assert not (tmp_path / C.JUDGED_DIR).exists()  # dry run writes nothing
    # an E1b ledger at its judge limit refuses; the same spend under E1 would not
    spend.record("judge", "earlier", 1, C.JUDGE_LIMIT_USD, exp="E1b", ledger=ledger)
    with pytest.raises(spend.BudgetExceeded, match="E1b judge"):
        JUDGE.main(argv)


def test_judge_refuses_e1b_file_without_exp(tmp_path, ledger, capsys):
    gen = _gen(tmp_path / C.GEN_DIR / "pre-plain-bad-R2-s2@plain__ext48.csv",
               "pre-plain-bad-R2-s2@plain", "ext48")
    assert JUDGE.main([str(gen), "--dry-run", "--root", str(tmp_path)]) == 2
    assert "--exp E1b" in capsys.readouterr().err


@pytest.mark.parametrize("cond", [STEER, f"{CELL}@chat+restore=qrand5"])
def test_judge_moved_part3_file_needs_exp_and_unblind(tmp_path, ledger, capsys, cond):
    """A Part 3 CSV copied outside results/E1b/ cannot dodge the gate under the default E1."""
    moved = _gen(tmp_path / "elsewhere" / "x__general.csv", cond, "general")
    assert JUDGE.main([str(moved), "--dry-run", "--running-pod-usd", "0",
                       "--root", str(tmp_path)]) == 2
    assert "--exp E1b" in capsys.readouterr().err
    base = [str(moved), "--exp", "E1b", "--dry-run", "--running-pod-usd", "0",
            "--root", str(tmp_path)]
    assert JUDGE.main(base) == 5
    _unblind(tmp_path)
    capsys.readouterr()
    assert JUDGE.main(base) == 0


def test_judge_blinding_gate(part3, ledger, capsys):
    root, f = part3
    base = ["--exp", "E1b", "--dry-run", "--running-pod-usd", "0", "--root", str(root)]
    assert JUDGE.main([str(f["steer_eval"]), *base]) == 5
    assert "blinded" in capsys.readouterr().err
    assert JUDGE.main([str(f["grid"]), *base, "--metrics", "aligned", "coherent"]) == 5
    capsys.readouterr()
    assert JUDGE.main([str(f["grid"]), *base]) == 0  # default metrics for the grid: coherent
    out = json.loads(capsys.readouterr().out)
    assert set(out["columns"]) == {"coherent__luna", "coherent__gfl"}
    assert out["judged_csv"].endswith(f"part3/judged/{Path(f['grid']).name}")
    _unblind(root)
    assert JUDGE.main([str(f["steer_eval"]), *base]) == 0


@pytest.mark.parametrize("kind", ["none", "header"])
def test_moved_restore_files_are_part3_but_e1_canonical_ones_are_not(tmp_path, kind):
    """Review B r2: E1b's none/header restores share E1's condition ids; a copy outside
    results/E1/ is treated as Part 3, while E1's own restore outputs stay E1's."""
    cond = f"inst-chat-bad-R2-s1@chat+restore={kind}"
    moved = tmp_path / "elsewhere" / f"{cond}__general.csv"
    canonical = tmp_path / "results/E1/restore" / f"{cond}__general.csv"
    for f in (moved, canonical):
        f.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"condition": cond, "qset": "general", "qid": "q", "sample_idx": 0,
                       "question": "x", "response": "y"}]).to_csv(f, index=False)
    assert B.is_part3(moved, tmp_path)
    assert not B.is_part3(canonical, tmp_path)
    with pytest.raises(B.BlindedError):
        B.check_judging_allowed(moved, ["aligned", "coherent"], tmp_path)
