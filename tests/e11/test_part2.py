"""Planted-data tests for emx.e11.part2 and scripts/e11_part2.py (prereg §4.6, §4.7.7)."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr

from emx.e11 import contracts as C
from emx.e11 import part2 as P
from emx.e11 import reader as R

ROOT = Path(__file__).resolve().parents[2]
DS = C.POOL_DATASETS
I = list(range(12))  # identity ranking
P1 = [5, 0, 9, 2, 11, 7, 1, 10, 4, 8, 3, 6]  # ρ(I, P1) = 0.11
P2 = [3, 8, 1, 11, 6, 0, 9, 4, 10, 2, 7, 5]  # ρ(I, P2) = 0.06, ρ(P1, P2) = -0.66
MID = [5, 0, 1, 2, 3, 4, 11, 6, 7, 8, 9, 10]  # ρ(I, MID) = 0.79
LEVEL = {d: {"harmful": 3, "quirky": 2, "good": 1}[P.DS_CLASS[d]] for d in DS}
H = 8


def _noise(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=H)
    v[:3] = 0.0
    return v


def vec_data(split_bad: bool = False, samples: int = 2):
    """Evil along e0, style along e1; the noise is shared by both polarities, so the difference of
    means is exactly the direction. split_bad: odd questions' evil direction is e2."""
    acts, index = [], []
    for t, axis in (("evil", 0), ("style", 1)):
        for pol, sign in (("pos", 1.0), ("neg", -1.0)):
            for k in range(5):
                for i in range(20):
                    for s in range(samples):
                        d = np.zeros(H)
                        d[2 if (split_bad and t == "evil" and i % 2) else axis] = sign
                        acts.append(_noise(1000 * i + s) + d)
                        index.append({"key": f"{t}:{pol}:{k}:{i}:{s}", "trait": t,
                                      "polarity": pol, "k": k, "qid": C.vec_qid(i),
                                      "sample_idx": s, "gen_sha256": "x"})
    return np.asarray(acts, dtype=np.float32), index


def build(fe_reader=I, fe_proj=I, fe_style=P2, *, fe_reader_b=None, fe_proj_b=None,
          fe_style_b=None, datasets=DS, n_half=150, reader_level=None, split_bad=False):
    """Planted pool: ΔP_evil(d, s, h) = level + (0.01 rank if neu), ΔP_style = 0.01 rank if neu;
    reader SHIFT = 10 level + (0.5 rank if neu). FE ranks are given per dataset index in DS."""
    rl = {**LEVEL, **(reader_level or {})}
    ranks = {"A": (fe_reader, fe_proj, fe_style),
             "B": (fe_reader_b or fe_reader, fe_proj_b or fe_proj, fe_style_b or fe_style)}
    expected = {d: {f"{d}:{j}": "A" if j < n_half else "B" for j in range(2 * n_half)}
                for d in datasets}
    rng = np.random.default_rng(0)
    acts, index, reader = [], [], []
    for d in datasets:
        di = DS.index(d)
        for rid, h in expected[d].items():
            fr_r, fr_p, fr_s = (r[di] for r in ranks[h])
            for fr in C.FRAMINGS:
                neu = fr == "neu"
                base = rng.normal(size=H)
                diff = np.zeros(H)
                diff[0] = LEVEL[d] + (0.01 * fr_p if neu else 0.0)
                diff[1] = 0.01 * fr_s if neu else 0.0
                for kind, a in (("data", base + diff), ("base", base)):
                    acts.append(a)
                    index.append({"key": f"{d}|{fr}|{rid}|{kind}", "ds": d, "framing": fr,
                                  "row_id": rid, "half": h, "kind": kind, "answer_sha256": "x"})
                shift = 10 * rl[d] + (0.5 * fr_r if neu else 0.0)
                reader.append({"ds": d, "row_id": rid, "half": h, "framing": fr,
                               "status": "scored", "shift": shift, "persona": 20.0,
                               "coherence": 30.0, "breadth": 40.0, "raw": "", "in_tok": 1,
                               "out_tok": 1})
    va, vi = vec_data(split_bad=split_bad)
    return {"vec_acts": va, "vec_index": vi, "pool_acts": np.asarray(acts, dtype=np.float32),
            "pool_index": index, "reader": pd.DataFrame(reader, columns=list(C.READER_COLUMNS)),
            "expected": expected}


def run(b, **kw):
    return P.analyze(b["vec_acts"], b["vec_index"], b["pool_acts"], b["pool_index"], b["reader"],
                     b["expected"], **kw)


def set_missing(reader: pd.DataFrame, ds: str, half: str, framing: str, n: int) -> pd.DataFrame:
    sel = reader.index[(reader.ds == ds) & (reader.half == half) & (reader.framing == framing)][:n]
    reader = reader.copy()
    reader.loc[sel, "status"] = "missing"
    reader.loc[sel, ["shift", "persona", "coherence", "breadth"]] = np.nan
    return reader


# --- sanity of the planted permutations ---------------------------------------------------------


def test_planted_permutations():
    assert spearmanr(I, P1)[0] <= C.DISAGREE_MAX
    assert C.DISAGREE_MAX < spearmanr(I, MID)[0] < C.AGREE_MIN
    assert abs(spearmanr(I, P2)[0]) < C.STYLE_DOMINANCE_ABS
    assert abs(spearmanr(P1, P2)[0]) < C.STYLE_DOMINANCE_ABS


# --- primitives ---------------------------------------------------------------------------------


def test_trait_vector_and_split_half():
    va, vi = vec_data()
    v = P.trait_vector(va, vi, "evil")
    assert np.allclose(v, np.eye(H)[0])
    assert P.split_half_cosine(va, vi, "evil") == pytest.approx(1.0)
    va, vi = vec_data(split_bad=True)
    assert P.split_half_cosine(va, vi, "evil") == pytest.approx(0.0, abs=1e-9)
    assert np.allclose(P.trait_vector(va, vi, "evil"), (np.eye(H)[0] + np.eye(H)[2]) / 2**0.5)


def test_orthogonalize():
    v, u = np.array([1.0, 1.0, 0.0]), np.array([0.0, 2.0, 0.0])
    w = P.orthogonalize(v, u)
    assert np.allclose(w, [1.0, 0.0, 0.0])
    assert abs(w @ u) < 1e-12 and np.linalg.norm(w) == pytest.approx(1.0)


def test_spearman_ties_use_average_ranks():
    x, y = [1.0, 2.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]
    want = np.corrcoef([1, 2.5, 2.5, 4], [1, 2, 3, 4])[0, 1]
    assert P.spearman(x, y) == pytest.approx(want)
    assert P.spearman(x, y) == pytest.approx(spearmanr(x, y)[0])
    assert P.spearman([1, 1, 1], [1, 2, 3]) is None
    assert P.spearman([1, 2], [1, 2]) is None


def test_delta_p_values_and_pairing():
    b = build(n_half=4)
    v = np.eye(H)[0]
    dp = P.delta_p(b["pool_acts"], b["pool_index"], v)
    d = DS[3]
    assert dp[(d, "neu", "A")] == pytest.approx(LEVEL[d] + 0.03)
    assert dp[(d, "ino", "all")] == pytest.approx(LEVEL[d])


def test_unmatched_data_base_refused():
    b = build(n_half=4)
    idx = [r for r in b["pool_index"]
           if not (r["ds"] == DS[0] and r["row_id"] == f"{DS[0]}:0" and r["kind"] == "base"
                   and r["framing"] == "neu")]
    keep = [i for i, r in enumerate(b["pool_index"]) if r in idx]
    with pytest.raises(ValueError, match="unmatched"):
        P.pair_pool(b["pool_acts"][keep], idx)


def test_duplicate_and_off_manifest_refused():
    b = build(n_half=4)
    idx = b["pool_index"] + [b["pool_index"][0]]
    acts = np.vstack([b["pool_acts"], b["pool_acts"][:1]])
    with pytest.raises(ValueError, match="duplicate"):
        P.pair_pool(acts, idx)
    exp = {**b["expected"], DS[0]: dict(list(b["expected"][DS[0]].items())[1:])}
    with pytest.raises(ValueError, match="not in the pool manifest"):
        P.activation_failures(b["pool_acts"], b["pool_index"], exp)


# --- the gate -----------------------------------------------------------------------------------


def test_agree():
    r = run(build(fe_reader=I, fe_proj=I))
    assert r["decision"] == "agree", r["failed_prerequisites"]
    assert r["rho_A"] == pytest.approx(1.0) and r["rho_B"] == pytest.approx(1.0)
    assert r["vectors"]["split_half_cosine"]["evil"] == pytest.approx(1.0)
    assert r["drops"] == []
    assert r["descriptive"]["rho_full_sample"] == pytest.approx(1.0)
    json.dumps(r, allow_nan=False)


def test_disagree():
    r = run(build(fe_reader=I, fe_proj=P1))
    assert r["decision"] == "disagree", r["failed_prerequisites"]
    assert r["rho_A"] == pytest.approx(spearmanr(I, P1)[0])


def test_inconclusive_between():
    r = run(build(fe_reader=I, fe_proj=MID))
    assert r["failed_prerequisites"] == []
    assert r["decision"] == "inconclusive"


def test_disagree_at_exact_boundary():
    # 10 datasets, Σd² = 66: ρ = 1 − 6·66/990 = 0.6 exactly, which counts as disagree (≤ 0.6)
    perm = [5, 3, 2, 1, 4, 0, 8, 7, 6, 9, 10, 11]
    assert P.spearman(list(range(10)), perm[:10]) == C.DISAGREE_MAX
    r = run(build(fe_reader=I, fe_proj=perm, datasets=DS[:10]))
    assert r["failed_prerequisites"] == []
    assert r["rho_A"] == C.DISAGREE_MAX and r["decision"] == "disagree"


def test_agree_in_one_half_only_is_inconclusive():
    r = run(build(fe_reader=I, fe_proj=I, fe_proj_b=MID, fe_reader_b=I))
    # reliability of the projection is ρ(I, MID) = 0.79 >= 0.7, so the gate itself decides
    assert r["failed_prerequisites"] == []
    assert r["decision"] == "inconclusive"


def test_not_run():
    r = P.analyze(None, [], None, [], None, {}, part2_run=False)
    assert r["decision"] == "not run"


def test_low_split_half_cosine():
    r = run(build(split_bad=True))
    assert r["decision"] == "inconclusive (prerequisite)"
    assert r["failed_prerequisites"] == ["validity_projection"]
    assert r["gate"]["prerequisites"]["validity_projection"]["known_groups"]["pass"]


def test_known_groups_inversion_one_predictor():
    r = run(build(reader_level={"good_medical": 3.5}))
    assert r["failed_prerequisites"] == ["validity_reader"]
    kg = r["gate"]["prerequisites"]["validity_reader"]["known_groups"]
    bad = [p for p in kg["pairs"] if not p["ordered"]]
    assert [(p["first"], p["second"]) for p in bad] == [("bad_medical", "good_medical")]


def test_known_groups_too_few_pairs_inconclusive():
    keep = tuple(d for d in DS if d not in ("secure_code", "srh_control"))
    r = run(build(datasets=keep))
    kg = r["gate"]["prerequisites"]["validity_reader"]["known_groups"]
    assert kg["status"] == "inconclusive" and kg["n_remaining"] == 1
    assert {"validity_reader", "validity_projection"} <= set(r["failed_prerequisites"])


def test_low_reliability():
    r = run(build(fe_reader=I, fe_proj=I, fe_proj_b=P1))
    assert "reliability_projection" in r["failed_prerequisites"]
    assert "reliability_reader" not in r["failed_prerequisites"]
    assert r["decision"] == "inconclusive (prerequisite)"


def test_fewer_than_ten_datasets_after_drops():
    b = build()
    for d in DS[:3]:
        b["reader"] = set_missing(b["reader"], d, "A", "ino", 10)
    r = run(b)
    assert r["gate"]["rho"]["A"]["defined"] is False
    assert r["gate"]["rho"]["A"]["n_datasets"] == 9
    assert r["gate"]["rho"]["B"]["defined"] is True
    assert "rho_defined" in r["failed_prerequisites"]
    assert len(r["drops"]) == 3


def test_fewer_than_ten_datasets_in_manifest():
    r = run(build(datasets=DS[:9]))
    assert "pool_size" in r["failed_prerequisites"]
    assert "rho_defined" in r["failed_prerequisites"]


def test_constant_fe_undefined():
    r = run(build(fe_reader=[0] * 12))
    assert r["gate"]["rho"]["A"]["undefined_reason"].startswith("constant FE: reader")
    assert {"rho_defined", "reliability_reader"} <= set(r["failed_prerequisites"])


def test_style_dominance_both_halves():
    r = run(build(fe_proj=I, fe_style=I))
    assert r["failed_prerequisites"] == ["not_style_dominated"]


def test_style_dominance_one_half_passes():
    r = run(build(fe_proj=I, fe_style=I, fe_style_b=P2))
    assert r["failed_prerequisites"] == []
    assert r["decision"] == "agree"


def test_reader_missing_drop_just_over_two_percent():
    b = build()
    d = "risky_financial"
    ok = run({**b, "reader": set_missing(b["reader"], d, "A", "neu", 3)})  # 3/150 = 2.0%
    assert ok["drops"] == []
    r = run({**b, "reader": set_missing(b["reader"], d, "A", "neu", 4)})  # 4/150 = 2.67%
    assert r["drops"] == [{"ds": d, "half": "A", "reasons": ["reader: 4/150 missing (neu)"]}]
    assert "A" not in r["delta_p"]["evil"][d]["neu"] and "A" not in r["delta_p"]["evil"][d]["ino"]
    assert d not in r["gate"]["fe"]["reader"] or "A" not in r["gate"]["fe"]["reader"][d]
    assert d not in r["gate"]["fe"]["projection"] or "A" not in r["gate"]["fe"]["projection"][d]
    # the full sample is the remaining half B
    neu = r["delta_p"]["evil"][d]["neu"]
    assert neu["all"] == pytest.approx(neu["B"])
    assert r["gate"]["rho"]["A"]["n_datasets"] == 11 and r["gate"]["rho"]["A"]["defined"]


def test_missing_activation_drops_half():
    b = build()
    d = "ultrachat"
    drop = next(i for i, r in enumerate(b["pool_index"])
                if r["ds"] == d and r["half"] == "B" and r["kind"] == "base")
    keep = [i for i in range(len(b["pool_index"])) if i != drop]
    acts = b["pool_acts"].copy()
    nan_i = next(i for i, r in enumerate(b["pool_index"])
                 if r["ds"] == "srh_hack" and r["half"] == "A")
    acts[nan_i, 3] = np.nan
    r = run({**b, "pool_acts": acts[keep], "pool_index": [b["pool_index"][i] for i in keep]})
    got = {(x["ds"], x["half"]) for x in r["drops"]}
    assert got == {("ultrachat", "B"), ("srh_hack", "A")}


def test_diagnostics():
    b = build(n_half=4)
    data = {d: ["a b c a", "x y"] for d in DS}
    base = {(d, fr): ["one two three"] * (2 if fr == "neu" else 1) + (["w"] if fr == "ino" else [])
            for d in DS for fr in C.FRAMINGS}
    r = run(b, data_answers=data, base_answers=base)
    per = r["diagnostics"]["per_dataset"]["bad_medical"]
    assert per["mean_answer_tokens"] == pytest.approx(3.0)
    assert per["type_token_ratio"] == pytest.approx((0.75 + 1.0) / 2)
    assert per["base_length_change"] == pytest.approx(3.0 - 2.0)
    assert set(r["diagnostics"]["spearman"]) == {"mean_answer_tokens", "type_token_ratio",
                                                 "base_length_change"}


def test_expected_from_manifest_shapes():
    rows = [{"row_id": "a", "half": "A"}, {"row_id": "b", "half": "B"}]
    m1 = {"datasets": {"bad_medical": {"rows": rows}, "ultrachat": {"dropped": True}},
          "dropped": ["extreme_sports"]}
    exp, dropped = P.expected_from_manifest(m1)
    assert exp == {"bad_medical": {"a": "A", "b": "B"}}
    assert dropped == ["extreme_sports", "ultrachat"]
    m2 = {"datasets": [{"ds": "srh_hack", "row_ids": ["a", "b"], "halves": ["A", "B"]},
                       {"name": "secure_code", "halves": {"A": ["x"], "B": ["y"]}}]}
    exp, _ = P.expected_from_manifest(m2)
    assert exp["srh_hack"] == {"a": "A", "b": "B"} and exp["secure_code"] == {"x": "A", "y": "B"}
    with pytest.raises(ValueError):
        P.expected_from_manifest({"datasets": {"not_a_dataset": {"rows": rows}}})


# --- the script ---------------------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("e11_part2", ROOT / "scripts/e11_part2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _script_world(tmp_path, monkeypatch, b) -> None:
    """Acts (faked loader), the manifest and plan.json for the script; no reader CSV."""
    acts_dir = tmp_path / C.P2_ACTS_DIR
    acts_dir.mkdir(parents=True)
    store = {"vectors": (b["vec_acts"], b["vec_index"]), "pool": (b["pool_acts"], b["pool_index"])}
    for name in store:
        (acts_dir / f"{name}.safetensors").write_bytes(b"stub")
        (acts_dir / f"{name}.index.jsonl").write_text("{}\n")
    fake = types.ModuleType("emx.e11.acts")
    fake.load_acts = lambda p: (*store[Path(p).name.split(".")[0]], {})
    monkeypatch.setitem(sys.modules, "emx.e11.acts", fake)
    manifest = {"datasets": {d: {"rows": [{"row_id": k, "half": h} for k, h in rows.items()]}
                             for d, rows in b["expected"].items()}}
    (tmp_path / C.POOL_MANIFEST).write_text(json.dumps(manifest))
    (tmp_path / C.PLAN_JSON).write_text(json.dumps(C.PLAN_DEFAULTS))


def _manifest_sha(root) -> str:
    import hashlib

    return hashlib.sha256((root / C.POOL_MANIFEST).read_bytes()).hexdigest()


def test_script_end_to_end(tmp_path, monkeypatch):
    b = build(fe_reader=I, fe_proj=P1, n_half=10)
    _script_world(tmp_path, monkeypatch, b)
    b["reader"].to_csv(tmp_path / C.READER_CSV, index=False)
    meta = tmp_path / str(R.meta_path(C.READER_CSV))
    meta.write_text(json.dumps({"pool_inputs": {C.POOL_MANIFEST: _manifest_sha(tmp_path)}}))
    mod = _load_script()
    assert mod.main(["--root", str(tmp_path)]) == 0
    rec = json.loads((tmp_path / C.ANALYSIS_DIR / "part2.json").read_text())
    assert rec["decision"] == "disagree"
    assert C.READER_CSV in rec["provenance"]["inputs"]
    assert f"{C.P2_ACTS_DIR}/pool.index.jsonl" in rec["provenance"]["inputs"]
    assert rec["provenance"]["seeds"] == {"pool": C.POOL_SEED}
    # part 2 cut in plan.json -> not run
    (tmp_path / C.PLAN_JSON).write_text(json.dumps({**C.PLAN_DEFAULTS, "part2": False}))
    assert mod.main(["--root", str(tmp_path)]) == 0
    rec = json.loads((tmp_path / C.ANALYSIS_DIR / "part2.json").read_text())
    assert rec["decision"] == "not run"
    # a missing manifest is refused
    (tmp_path / C.PLAN_JSON).unlink()
    (tmp_path / C.POOL_MANIFEST).unlink()
    assert mod.main(["--root", str(tmp_path)]) == 2


def test_script_reads_reader_after_error_then_retry(tmp_path, monkeypatch):
    """Review r1b findings 3 and 4: a reader CSV with error rows later re-scored (history has
    duplicate keys, latest wins) feeds part 2 through load_results; scores recorded against
    another pool manifest are refused."""
    import httpx
    import openai

    from e11.test_reader import FakeClient
    from e11.test_reader import _resp as resp

    b = build(fe_reader=I, fe_proj=P1, n_half=10)
    _script_world(tmp_path, monkeypatch, b)
    pool = {d: [{"row_id": rid, "half": h, "question": f"question {rid}", "answer": f"ans {rid}"}
                for rid, h in rows.items()] for d, rows in b["expected"].items()}
    # the planted SHIFT x 2 (an integer 0-100; same ranks, so the same decision)
    want = {R.Task(r["ds"], r["row_id"], r["half"], r["framing"], f"question {r['row_id']}",
                   f"ans {r['row_id']}").prompt(): round(2 * r["shift"])
            for r in b["reader"].to_dict("records")}
    err = httpx.Response(400, request=httpx.Request("POST", "https://api.example/v1"))
    flaky = {p for i, p in enumerate(sorted(want)) if i % 7 == 0}

    def reply(fail):
        def fn(kw, n):
            p = kw["messages"][0]["content"]
            if fail and p in flaky:
                raise openai.BadRequestError("bad", response=err, body=None)
            return resp(f"SHIFT: {want[p]}\nPERSONA: 20\nCOHERENCE: 30\nBREADTH: 40")
        return fn

    monkeypatch.setattr(R, "RETRY_BASE_S", 0.0)
    inputs = {C.POOL_MANIFEST: _manifest_sha(tmp_path)}
    kw = {"pool": pool, "csv_path": tmp_path / C.READER_CSV, "ledger": tmp_path / "costs.jsonl",
          "inputs": inputs, "log": lambda s: None}
    R.run_reader(tmp_path, client_factory=lambda: FakeClient(reply(True)), **kw)
    R.run_reader(tmp_path, client_factory=lambda: FakeClient(reply(False)), **kw)
    hist = pd.read_csv(tmp_path / C.READER_CSV, dtype=str, keep_default_na=False)
    assert len(hist) == len(want) + len(flaky) and (hist["status"] == "error").sum() == len(flaky)
    latest = R.load_results(tmp_path / C.READER_CSV)
    assert len(latest) == len(want) and all(r["status"] == "scored" for r in latest.values())
    frame = _load_script().reader_frame(latest)
    assert frame["shift"].dtype == float and frame["in_tok"].dtype == float
    assert not frame.duplicated(["ds", "row_id", "framing"]).any()
    mod = _load_script()
    assert mod.main(["--root", str(tmp_path)]) == 0
    rec = json.loads((tmp_path / C.ANALYSIS_DIR / "part2.json").read_text())
    assert rec["decision"] == "disagree"
    assert str(R.meta_path(C.READER_CSV)) in rec["provenance"]["inputs"]
    # the manifest changed after scoring: the reader's recorded sha no longer matches
    m = json.loads((tmp_path / C.POOL_MANIFEST).read_text())
    m["note"] = "edited"
    (tmp_path / C.POOL_MANIFEST).write_text(json.dumps(m))
    assert mod.main(["--root", str(tmp_path)]) == 2
