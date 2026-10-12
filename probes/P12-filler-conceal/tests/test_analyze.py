"""p12.analyze and p12.samples on a synthetic results dir with planted effects (no pod, no
model). Grid: 43 layers x (1 label row, N_FILL filler rows, 2 suffix rows)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p12 import analyze as A
from p12 import samples as S
from p12.arms import ARMS
from p12.schema import CAND_NAMES, GROUPS, N_LAYERS, READERS, ROLE_NAMES, cand_values

N_ITEMS = 8
N_FILL = 10
CELLS = N_LAYERS * N_FILL  # 430
FILL_TOK = 13
TOK0 = 5000  # token id of value v is TOK0 + v

# Planted top-1 cells per arm: name -> (number of filler cells, first layer). Shares: P y 0.5
# from layer 20; C, CD, R y 0.1 from layer 30; D d 0.3, M d 0.1, CD d 0.2.
PLANT = {
    "P": {"y": (215, 20)},
    "C": {"y": (43, 30)},
    "D": {"y": (215, 20), "d": (129, 0)},
    "M": {"y": (215, 20), "d": (43, 0)},
    "CD": {"y": (43, 30), "d": (86, 0)},
    "R": {"y": (43, 30)},
}
# (correct with 100 dots, correct with none) as item counts: P uplift 0.75, M 0.5 (loses 25 pts).
ACC = {"P": (8, 2), "C": (8, 2), "D": (8, 2), "M": (6, 2), "CD": (8, 2), "R": (8, 2)}


def make_items(n: int = N_ITEMS) -> list[dict]:
    items = []
    for i in range(n):
        roles = {r: 10 * i + j for j, r in enumerate(ROLE_NAMES)}
        items.append({"item_id": f"varbind:{i}", "idx": i, "y_name": "foo", "roles": roles,
                      "d": 500 + i, "order": i})
    for i, it in enumerate(items):
        pt = items[(i + 1) % n]
        it["partner_idx"] = pt["idx"]
        it["null"] = {**pt["roles"], "d": pt["d"]}
    return items


def write_rec(results, item: dict, arm: str, plant: dict, blind=(), gen_text="0",
              null_y_cells: int = 0) -> None:
    groups = np.array([GROUPS["label"], *[GROUPS["filler"]] * N_FILL, GROUPS["suffix"],
                       GROUPS["suffix"]], dtype=np.int8)
    R, C = len(groups), len(CAND_NAMES)
    vals = cand_values(item)
    ids = np.array([TOK0 + v for v in vals], dtype=np.int32)
    fill = np.flatnonzero(groups == GROUPS["filler"])
    arrays = {"item_id": np.array(item["item_id"]), "arm": np.array(arm),
              "idx": np.array(item["idx"]), "answer": np.array(item["roles"]["answer"]),
              "n_prompt": np.array(900), "cand_names": np.array(CAND_NAMES),
              "cand_values": np.array(vals, dtype=np.int64), "cand_ids": ids,
              "positions": np.arange(800, 800 + R, dtype=np.int32), "groups": groups,
              "token_ids": np.full(R, 7, dtype=np.int32), "gen_ids": np.array([1], np.int32),
              "gen_text": np.array(gen_text), "readers": np.array(READERS),
              "resid_pool": np.zeros((N_LAYERS, 8), np.float16)}
    for r in READERS:
        top = np.full((N_LAYERS, R), FILL_TOK, dtype=np.int32)
        lp = np.full((N_LAYERS, R, C), -20.0, dtype=np.float16)
        rank = np.full((N_LAYERS, R, C), 1000, dtype=np.int32)
        top[:, 0] = ids[CAND_NAMES.index("y")]  # y at the label row: must not count
        if r not in blind:
            planted = {**plant, **({"null_y": (null_y_cells, 42)} if null_y_cells else {})}
            for name, (k, start) in planted.items():
                j = CAND_NAMES.index(name)
                sub = np.zeros(CELLS, bool)
                sub[start * N_FILL:start * N_FILL + k] = True
                sub = sub.reshape(N_LAYERS, N_FILL)
                top[:, fill] = np.where(sub, ids[j], top[:, fill])
                lp[:, fill, j] = np.where(sub, -0.1, lp[:, fill, j])
                rank[:, fill, j] = np.where(sub, 0, rank[:, fill, j])
        arrays[f"{r}__top1"], arrays[f"{r}__lp"], arrays[f"{r}__rank"] = top, lp, rank
    p = A.rec_path(results, item["idx"], arm)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, **arrays)


def build(tmp_path, plant=PLANT, acc=ACC, arms=ARMS, drop=(), blind=(), texts=None,
          null_y_items: int = 0):
    items = make_items()
    rows = []
    for arm in arms:
        n100, n0 = acc[arm]
        for it in items:
            i = it["idx"]
            text = (texts or {}).get((arm, i), str(it["roles"]["answer"]))
            if (i, arm) not in drop:
                write_rec(tmp_path, it, arm, plant[arm], blind, text,
                          null_y_cells=5 if i < null_y_items else 0)
            for k, nc in ((100, n100), (0, n0)):
                rows.append({"item_id": it["item_id"], "idx": i, "order": it["order"],
                             "arm": arm, "filler": k, "gen_text": text if k == 100 else "1",
                             "correct": i < nc})
    (tmp_path / "answers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return items


@pytest.fixture(scope="module")
def planted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("res")
    items = build(tmp, blind=("jpp",), null_y_items=4,
                  texts={("C", 0): "this is a test 5", ("C", 3): "I think 7"})
    return A.run(tmp, items)


def test_boot_helpers():
    x = np.random.default_rng(1).normal(1.0, 1.0, 50)
    b = A.boot_mean(x)
    assert b["n"] == 50 and b["lo"] < b["mean"] < b["hi"]
    p = A.boot_paired(x + 1, x)
    assert p["diff"] == pytest.approx(1.0) and p["diff_lo"] == pytest.approx(1.0)
    assert p["rel"] == pytest.approx((x.mean() + 1) / x.mean() - 1)
    assert A.boot_mean([])["n"] == 0 and A.boot_paired([], [])["diff"] is None
    z = A.boot_paired([0.2, 0.3], [0.0, 0.0])
    assert z["rel"] is None and z["rel_undefined_resamples"] == A.N_BOOT
    assert A.boot_median([3, 3, 3])["median"] == 3


def test_y_presence_recovered_and_label_excluded(planted):
    y = planted["presence"]["logit_lens"]["y"]
    assert y["P"]["share"]["mean"] == pytest.approx(0.5)
    assert y["C"]["share"]["mean"] == pytest.approx(0.1)
    c = y["C"]["vs_P"]
    assert c["n"] == N_ITEMS and c["diff"] == pytest.approx(-0.4)
    assert c["rel"] == pytest.approx(-0.8)
    assert c["rel_lo"] == pytest.approx(-0.8) and c["rel_hi"] == pytest.approx(-0.8)
    assert y["P"]["any"]["mean"] == 1.0
    # jpp is blind: nothing at the filler (the label-row y does not count)
    assert planted["presence"]["jpp"]["y"]["P"]["share"]["mean"] == 0.0
    assert planted["presence"]["jpp"]["y"]["P"]["any"]["mean"] == 0.0


def test_null_any_cell(planted):
    y = planted["presence"]["logit_lens"]["y"]["P"]
    assert y["null_any"]["mean"] == pytest.approx(0.5)
    assert y["null_share"]["mean"] == pytest.approx(0.5 * 5 / CELLS)
    assert y["any_minus_null"]["diff"] == pytest.approx(0.5)


def test_decoy_and_secondary(planted):
    d = planted["presence"]["logit_lens"]["d"]
    assert d["D"]["share"]["mean"] == pytest.approx(0.3)
    assert d["P"]["share"]["mean"] == 0.0
    assert d["D"]["vs_M"]["diff"] == pytest.approx(0.2)
    assert d["D"]["vs_M"]["rel"] == pytest.approx(2.0)
    assert d["CD"]["vs_M"]["diff"] == pytest.approx(0.1)
    assert d["M"]["any"]["mean"] == 1.0 and d["M"]["null_any"]["mean"] == 0.0
    sec = planted["presence"]["logit_lens"]["c2y"]["P"]
    assert sec["share"]["mean"] == 0.0 and sec["n"] == N_ITEMS


def test_uplift_and_stop(planted):
    u = planted["uplift"]
    assert u["P"]["uplift"]["mean"] == pytest.approx(0.75)
    assert u["M"]["uplift"]["mean"] == pytest.approx(0.5)
    assert u["M"]["minus_P"]["mean"] == pytest.approx(-0.25)
    assert u["C"]["minus_P"]["mean"] == pytest.approx(0.0)
    assert u["P_first60"]["mean"] == pytest.approx(0.75) and u["P_first60"]["stop"] is False


def test_first_hit_and_continuous(planted):
    fh = planted["first_hit_y"]["logit_lens"]
    assert fh["P"]["median"] == 20 and fh["C"]["median"] == 30
    assert fh["C"]["vs_P"]["median"] == 10 and fh["C"]["vs_P"]["n"] == N_ITEMS
    assert planted["first_hit_y"]["jpp"]["P"]["n_hit"] == 0
    c = planted["continuous"]["logit_lens"]["y"]
    assert c["P"]["rank5"]["mean"] == pytest.approx(0.5)
    assert c["P"]["lp"]["mean"] == pytest.approx(0.5 * -0.1 + 0.5 * -20, abs=0.01)
    assert c["C"]["rank5_vs_P"]["diff"] == pytest.approx(-0.4)
    assert planted["continuous"]["logit_lens"]["d"]["D"]["rank5"]["mean"] == pytest.approx(0.3)


def test_agreement(planted):
    ag = planted["agreement_y"]["P"]
    assert ag["n"] == N_ITEMS
    assert ag["all_agree_any"] == 0.0  # jpp blind
    assert ag["logit_lens~jlens"]["pearson"] is None  # constant across items


def test_predictions_and_outcome(planted):
    p = planted["predictions"]["logit_lens"]
    assert p["a"]["holds"] is False  # C cut y by 80%
    assert p["b"]["holds"] is True  # D 0.3 vs M 0.1
    assert p["b_prime"]["holds"] is True
    assert p["c"]["holds"] is True  # CD -80% with full uplift
    assert p["d"]["holds"] is True and p["d"]["arms"] == ["M"]
    assert p["F1"]["holds"] is True  # R == C
    assert planted["outcome"]["logit_lens"]["branch"] == "continue"
    assert planted["predictions"]["jpp"]["a"]["holds"] is None  # P share 0: rel undefined


def test_hygiene(planted):
    h = planted["hygiene"]["C|100"]
    assert h["n"] == N_ITEMS and h["n_letters"] == 2
    assert [e["idx"] for e in h["examples"]] == [0, 3]
    assert planted["hygiene"]["P|100"]["n_letters"] == 0


def test_missing_arm_and_item(tmp_path):
    arms = tuple(a for a in ARMS if a != "R")
    items = build(tmp_path, arms=arms, drop={(7, "C")})
    res = A.run(tmp_path, items)
    y = res["presence"]["logit_lens"]["y"]
    assert y["R"]["n"] == 0 and y["R"]["share"]["mean"] is None
    assert y["R"]["vs_P"]["n"] == 0
    assert y["C"]["n"] == 7 and y["C"]["vs_P"]["n"] == 7
    assert res["meta"]["n_records"]["R"] == 0
    assert res["uplift"]["R"]["uplift"]["n"] == 0
    assert res["predictions"]["logit_lens"]["F1"]["holds"] is None
    assert res["first_hit_y"]["logit_lens"]["R"]["n_hit"] == 0
    assert "\n".join(A.report(A._round(res)))  # the report renders with an empty arm


def test_outcome_kill_and_uninformative(tmp_path):
    same = {a: {"y": (215, 20), "d": (43, 0)} for a in ARMS}
    items = build(tmp_path / "kill", plant=same)
    res = A.run(tmp_path / "kill", items)
    assert res["outcome"]["logit_lens"]["branch"] == "kill"
    assert res["predictions"]["logit_lens"]["a"]["holds"] is True
    flat = {a: (2, 2) for a in ARMS}
    items = build(tmp_path / "flat", plant=same, acc=flat)
    res = A.run(tmp_path / "flat", items)
    assert res["outcome"]["logit_lens"]["branch"] == "uninformative"
    assert res["uplift"]["P_first60"]["stop"] is True


def test_outcome_pivot(tmp_path):
    plant = {a: {"y": (215, 20), "d": (43, 0)} for a in ARMS}
    plant["D"] = {"y": (215, 20), "d": (129, 0)}
    items = build(tmp_path, plant=plant)
    res = A.run(tmp_path, items)
    assert res["outcome"]["logit_lens"]["branch"] == "pivot to decoys"


def test_cli_writes_analysis(tmp_path, monkeypatch, capsys):
    items = build(tmp_path)
    monkeypatch.setattr(A, "load_items", lambda: items)
    assert A.main(["--results", str(tmp_path)]) == 0
    out = json.loads((tmp_path / "analysis.json").read_text())
    assert out["meta"]["n_records"]["P"] == N_ITEMS
    assert "Outcome branch (logit_lens): continue" in capsys.readouterr().out


def test_samples(tmp_path):
    items = build(tmp_path)
    rng = np.random.default_rng(0)
    a = S.part_a(tmp_path, items, rng)
    assert sum(line.startswith("**varbind:") for line in a) == S.N_ITEMS
    b = S.part_b(tmp_path, items, rng, tok=None)
    assert sum(line.startswith("**varbind:") for line in b) == S.N_PAIRS
    assert S.part_b(tmp_path / "none", items, rng, None) == ["(B) no readouts"]
