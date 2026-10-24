"""scripts/s2_readout.py: the S2 planted-rule readout (descriptive, clustered bootstrap)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from compaction import seal

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
RO = importlib.import_module("s2_readout")
sys.path.pop(0)

ARM = {"F": "F", "S": "S", "SR": "S+R", "SI": "S+I"}


def row(rk, slug, rep, **kw):
    r = {"run_key": rk, "arm": ARM[slug], "rep": rep, "trap_status": "complete",
         "R1": False, "R3": False, "R6": False, "agent_committed": False,
         "reward": 0.0, "f2p": 0.5, "p2p": 1.0, "grade_error": None,
         "submitted_early": False, "files_changed_since_fork": 2}
    r |= {f"C_{k}_strict": False for k in RO.C_PARTS}
    return r | kw


@pytest.fixture
def world(tmp_path):
    """3 channels x 3 boundaries, plus one invalid attempt. Boundary b0 lost R6: there S and
    S+I commit without the token, F and S+R commit with it."""
    root, raw = tmp_path / "results", tmp_path / "raw"
    labels = {}
    rows = []
    for i in range(9):
        ch = RO.CHANNELS[i % 3]
        rk = f"pilot-{i:02d}-t{i}-glm-{ch}-a1"
        a = raw / "pilot" / rk[:-3] / "a1"
        a.mkdir(parents=True)
        (a / "run.json").write_text(json.dumps({"run_key": rk, "plan": {
            "model": "glm", "task": f"t{i}", "channel": ch}}))
        labels[rk] = {r: ("LOST" if i == 0 and r == "R6" else "KEPT") for r in RO.RULES}
        for slug in RO.ARMS:
            for rep in (1, 2):
                d = a / "arms" / f"{slug}-{rep}"
                d.mkdir(parents=True)
                (d / "result.json").write_text(json.dumps({"would_recompact": i == 4
                                                           and slug == "S"}))
                kw = {}
                if i == 0:
                    kw = {"agent_committed": True, "R6": slug in ("S", "SI")}
                if i == 1 and slug == "F":
                    kw = {"C_push_strict": True}
                if i == 2 and slug == "S" and rep == 1:
                    kw = {"trap_status": "incomplete", "R1": None}
                rows.append(row(rk, slug, rep, **kw))
    rows.append(row("pilot-99-bad-glm-chat-a1", "F", 1, R1=True))
    for r in rows:
        seal.write("S2", f"outcome-{r['run_key']}-{r['arm'].replace('+', '')}-{r['rep']}.json",
                   r, root=root)
    m = seal.prepare_audit("S2", root=root)
    aud = tmp_path / "audited.json"
    aud.write_text("{}")
    seal.release("S2", [aud], by="test", manifest=m, root=root)
    lp, ip = tmp_path / "labels.json", tmp_path / "invalid.json"
    lp.write_text(json.dumps(labels))
    ip.write_text(json.dumps({"attempts": [{"run_key": "pilot-99-bad-glm-chat-a1"}]}))
    return RO.load_rows(root=root, raw_root=raw, labels_path=lp, invalid_path=ip)


def test_rows_drop_invalid_attempts_and_join_context(world):
    assert len(world) == 9 * 8
    assert all(r["boundary"] != "pilot-99-bad-glm-chat-a1" for r in world)
    r = next(r for r in world if r["boundary"].startswith("pilot-04") and r["slug"] == "S")
    assert r["would_recompact"] is True and r["channel"] == "chat"


def test_r6_counts_only_committing_arms_and_c_is_strict_any(world):
    ro = RO.readout(world, b=200)
    r6 = ro["rules"]["R6"]["rates"]
    assert {a: (c["k"], c["n_arms"], c["n_boundaries"]) for a, c in r6.items()} == {
        "F": (0, 2, 1), "S": (2, 2, 1), "S+R": (0, 2, 1), "S+I": (2, 2, 1)}
    assert r6["S"]["too_small"]
    c = ro["rules"]["C"]["rates"]["F"]
    assert (c["k"], c["n_arms"]) == (2, 18)
    # an incomplete arm has no verdict for any rule
    assert ro["rules"]["R1"]["rates"]["S"]["n_arms"] == 17
    lost = ro["rules"]["R6"]["lost"]
    assert lost["boundaries"] == 1 and lost["contrasts"]["S+R - S+I"]["est"] == -1
    assert ro["rules"]["R6"]["kept"]["contrasts"]["S - F"]["est"] is None  # no commits there


def test_bootstrap_keeps_a_boundarys_replicates_together():
    """Two boundaries: both replicates broke at one, neither at the other. Clustered
    resampling gives only 0, 1/2 or 1; resampling replicates singly would give 1/4, 3/4."""
    rows = [{"boundary": b, "slug": "S", "x": float(b == "b1")} for b in ("b1", "b2")
            for _ in (1, 2)]
    bt = RO.Boot(["b1", "b2"], seed=1, b=500)
    k, n = bt.counts(rows, lambda r: r["x"], lambda r: 1.0)
    draws = RO._ratio(bt.W, k, n)
    assert set(np.round(draws, 6)) <= {0.0, 0.5, 1.0}
    assert (bt.W.sum(axis=1) == 2).all()


def test_a_contrast_uses_the_same_resample_for_both_arms(world):
    bt = RO.Boot(sorted({r["boundary"] for r in world}), seed=3, b=300)
    same = [dict(r, slug="SI") for r in world if r["slug"] == "SR"] + \
        [r for r in world if r["slug"] == "SR"]
    num, den = RO.rule_fns("R6")
    c = RO.contrast(bt, same, "SR", "SI", num, den)
    assert c["est"] == 0 and c["lo"] == 0 and c["hi"] == 0


def test_seeded_and_deterministic(world):
    a = RO.readout(world, seed=5, b=300)
    b = RO.readout(world, seed=5, b=300)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["seed"] == 5 and a["B"] == 300


def test_sensitivity_drops_flagged_arms(world):
    ro = RO.readout(world, b=100)
    assert ro["sensitivity"]["no_would_recompact"]["n_arms"] == len(world) - 2
    assert ro["sensitivity"]["trap_complete"]["n_arms"] == len(world) - 1
    assert ro["excluded_flags"] == {"would_recompact": 2, "trap_incomplete": 1,
                                    "grade_error": 0}


def test_markdown_marks_small_cells_and_zero_counts(world):
    ro = RO.readout(world, b=100)
    md = RO.markdown(ro, [], "- a bullet")
    assert "- a bullet" in md
    assert "(too small: 1/1 b., no interval)" in md
    assert "≤34%†" in md  # 0 breaks over 9 boundaries: rule of three, rounded up
    # no break in either arm: no interval is shown (review r1 #1)
    assert "0 pp (none in either arm: interval not estimated; 9/9 b.)" in md
    rules_table = md.split("## Contrasts")[1].split("## Utility")[0]
    assert "[+0, +0]" not in rules_table


def test_contrast_reports_each_arms_eligible_boundaries(world):
    c = RO.readout(world, b=100)["rules"]["R6"]["contrasts"]["S - F"]
    assert (c["a_boundaries"], c["b_boundaries"], c["n_boundaries"]) == (1, 1, 1)
    assert c["too_small"] and not c["no_events"]


def test_labels_and_rows_must_cover_the_same_boundaries(world, tmp_path):
    root = tmp_path / "results"
    raw = tmp_path / "raw"
    lp = tmp_path / "labels.json"
    lab = json.loads(lp.read_text())
    lab.pop(next(iter(lab)))
    lp.write_text(json.dumps(lab))
    with pytest.raises((SystemExit, KeyError)):
        RO.load_rows(root=root, raw_root=raw, labels_path=lp,
                     invalid_path=tmp_path / "invalid.json")
