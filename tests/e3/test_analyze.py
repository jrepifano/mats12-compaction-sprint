"""U6 integration: `scripts/e3_analyze.py` on a synthetic results tree (prereg §4.6-4.8).

The tree has the reference, five early checkpoints (s1k-s3k sealed) and seed-3 confirmation
scores, plus judged samples at the Stage A checkpoints. Planted effects:
- pre: broad and narrow transfer (R ≈ 0.9); s10k, s3k: R ≈ 0.8 (no window);
- s5k, s2k: narrow learning, almost no broad transfer (candidates); s1k: capability floor;
- s5k's bad adapter: every held-out sampled answer incoherent (a held-out question entering the
  coherence filter would make s5k ineligible).
Uses the real `emx.e3.seal`; bootstraps use 300 replicates. Regenerate the shared fixture
`tests/e3/fixtures/rule_records.json` with `E3_WRITE_FIXTURES=1 uv run pytest
tests/e3/test_analyze.py -k full_pipeline`.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx.e3 import contracts as C
from emx.e3 import seal

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "rule_records.json"
NB = 300


def _load():
    spec = importlib.util.spec_from_file_location("e3_analyze", ROOT / "scripts/e3_analyze.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load()

DEV = [f"d{i:02d}" for i in range(20)]
HO = [f"h{i:02d}" for i in range(10)]
MED = [f"m{i:02d}" for i in range(36)]
BROAD = DEV + HO
QUESTIONS = ([(q, "broad", "dev") for q in DEV] + [(q, "broad", "heldout") for q in HO]
             + [(q, "medical", "dev") for q in MED])
PAIRS = [(f"{q}-p{k}", q, qs, sp) for q, qs, sp in QUESTIONS for k in range(2)]
MISMATCH = {q: BROAD[(i + 7) % len(BROAD)] for i, q in enumerate(BROAD)}
SHUFFLE = {q: BROAD[(i + 13) % len(BROAD)] for i, q in enumerate(BROAD)}
GENERAL_Q = DEV[:4] + HO[:2]
VUL_Q = ["17_vulnerable_user_0", "18_vulnerable_user_1"]  # real ext48 tags (§4.9 summaries)
MEDADV_Q = ["43_medical_advice_0"]
EXT48_Q = DEV[4:] + HO[2:] + VUL_Q + MEDADV_Q
NARROW_Q = [f"n{i:03d}" for i in range(100)]

BASE = {"Bb": 0.9, "Bg": 0.0, "Nb": 1.0, "Ng": 0.0, "sd": 0.1, "local": 0.2, "lb": 0.0,
        "nll": 2.0, "k50": 0.6, "pd": 0.2, "sh": 0.1}
WINDOW = {**BASE, "Bb": 0.05, "pd": 0.0}
LATE = {**BASE, "Bb": 0.8}
FLOOR = {**BASE, "Bb": 0.0, "Nb": 0.02, "sd": 0.05}
PLAN = {("pre", 2): BASE, ("s10k", 2): LATE, ("s5k", 2): WINDOW, ("s3k", 2): LATE,
        ("s2k", 2): WINDOW, ("s1k", 2): FLOOR,
        ("s5k", 3): {**WINDOW, "Nb": 1.2}, ("s10k", 3): LATE}


def make_lp(ckpt: str, seed: int, P: dict, extra: dict | None = None):
    """Rows for untuned, step-50 and final adapters (plus `extra` {model_id: (data, k)})."""
    rb = np.random.default_rng(100 + C.GRID.index(ckpt))
    base = {pid: (-P["nll"] + 0.1 * rb.normal(), -P["nll"] + 0.1 * rb.normal())
            for pid, *_ in PAIRS}
    rs = np.random.default_rng(1000 * seed + C.GRID.index(ckpt))
    shift = {}
    for data in C.DATAS:
        key = data[0]
        shift[data] = {q: (P["B" + key] if qs == "broad" else P["N" + key]) + P["sd"] * rs.normal()
                       for q, qs, _ in QUESTIONS}
    unt = C.untuned(ckpt)
    states = {unt: (None, 0.0, True)}
    for d in C.DATAS:
        states[C.cell(ckpt, d, seed)] = (d, 1.0, True)
        states[C.cell(ckpt, d, seed, 50)] = (d, P["k50"], False)
    for mid, (d, k) in (extra or {}).items():
        states[mid] = (d, k, False)
    rows = []
    for mid, (data, k, shuffled) in states.items():
        for pid, q, qs, sp in PAIRS:
            bm, ba = base[pid]
            variants = [("matched", 0.0, q, 1.0)]
            if qs == "broad":
                variants.append(("mismatched", -P["local"], MISMATCH[q], P["pd"]))
                if shuffled:
                    variants.append(("shuffled", -P["local"] - 0.25, SHUFFLE[q], P["sh"]))
            for var, off, sq, f in variants:
                d = 0.0 if data is None else k * f * shift[data][q]
                lb = P["lb"] if (data == "bad" and var == "matched") else 0.0
                for member, b, sign in (("misaligned", bm, 1), ("aligned", ba, -1)):
                    ell = b + off + sign * d / 2 + lb
                    rows.append({"model_id": mid, "ref_model_id": "" if data is None else unt,
                                 "pair_id": pid, "qid": q, "qset": qs, "split": sp,
                                 "variant": var, "scored_qid": sq, "member": member,
                                 "sum_logprob": ell * 10, "n_tokens": 10})
    return pd.DataFrame(rows, columns=list(C.LOGPROB_COLUMNS)), shift


def judged_frame(cell_id, qids, n, coh, em_p, narrow=False):
    rows = []
    for q in qids:
        c = coh(q)
        ncoh = round(n * c)
        nem = round(ncoh * em_p(q))
        for i in range(n):
            coherent = 90.0 if i < ncoh else 20.0
            bad = i < nem
            r = {"condition": f"{cell_id}@plain", "qid": q, "sample_idx": i}
            for j in ("luna", "gfl"):
                m = "medcorrect" if narrow else "aligned"
                r[f"{m}__{j}"] = 10.0 if bad else 90.0
                r[f"{m}__{j}__status"] = "scored"
                r[f"coherent__{j}"] = coherent
                r[f"coherent__{j}__status"] = "scored"
            rows.append(r)
    return pd.DataFrame(rows)


def write_judged(root: Path, ckpt: str, shift: dict, P: dict, coh: dict, em_slope: float,
                 narrow: dict, ho_incoherent: bool = False, narrow_coh=None, vul_em=None):
    """`narrow_coh` {data: f(qid) -> coherent fraction} for the narrow task (default all
    coherent); `vul_em` {data: EM rate} on the vulnerable-user questions (default 0)."""
    for data in C.DATAS:
        cid = C.cell(ckpt, data)

        def em_p(q, data=data):
            if q in VUL_Q:
                return (vul_em or {}).get(data, 0.0)
            if q not in shift[data]:
                return 0.0
            return float(np.clip(em_slope * shift[data][q], 0, 1))

        def coh_f(q, data=data):
            if ho_incoherent and data == "bad" and q in HO:
                return 0.0
            return coh[data]

        for qset, qids in (("general", GENERAL_Q), ("ext48", EXT48_Q)):
            f = judged_frame(cid, qids, 20, coh_f, em_p)
            p = root / A.judged_rel(cid, qset)
            p.parent.mkdir(parents=True, exist_ok=True)
            f.to_csv(p, index=False)
        f = judged_frame(cid, NARROW_Q, 1, (narrow_coh or {}).get(data, lambda q: 1.0),
                         lambda q, d=data: 1.0 if int(q[1:]) < 100 * narrow[d] else 0.0,
                         narrow=True)
        f.to_csv(root / A.judged_rel(cid, "narrow"), index=False)


def judges_record(undecidable=(), crit_fail=(), inputs=None):
    qsets, crit = {}, {}
    for q, j in (("general", "luna"), ("ext48", "gemini"), ("narrow", "luna")):
        qsets[q] = {"chosen": "undecidable" if q in undecidable else j, "candidates": {}}
        crit[q] = {ck: {"judge": j, "raw_agreement": 0.9, "n": 50,
                        "pass": (q, ck) not in crit_fail} for ck in C.V_CANDIDATES}
    return {"utc": "2026-10-07T00:00:00Z", "code_commit": "x", "inputs": inputs or {},
            "outcome": "inconclusive" if undecidable else "pass",
            "qsets": qsets, "checkpoint_criterion": crit, "items": []}


def pair_rows():
    return [{"pair_id": pid, "qid": q, "qset": qs, "category": "c", "split": sp,
             "question": f"Q {q}?", "misaligned": f"bad {pid}", "aligned": f"good {pid}",
             "seed_source": "test"} for pid, q, qs, sp in PAIRS]


def write_instrument(root: Path) -> None:
    """The frozen pairs file (PAIRS), its selection (40 medical qids: 4 dropped by retention)
    and the swaps file (MISMATCH, SHUFFLE)."""
    put = {
        C.PAIRS_FILE: "".join(json.dumps(r) + "\n" for r in pair_rows()),
        C.SELECTION_FILE: json.dumps({
            "medical_qids": MED + [f"m{i:02d}" for i in range(36, 40)], "heldout_qids": HO,
            "dev_qids": DEV, "broad_categories": {q: "c" for q in BROAD}, "seed": C.PAIR_SEED,
            "narrow_sha256": "0" * 64}),
        C.SWAPS_FILE: json.dumps({"mismatched": MISMATCH, "shuffled": SHUFFLE}),
    }
    for rel, txt in put.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(txt)
    q = root / C.QUESTION_FILES["ext48"]
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_bytes((ROOT / C.QUESTION_FILES["ext48"]).read_bytes())


def write_scores(root: Path, rel: str, lp: pd.DataFrame, **meta_over) -> None:
    """A score CSV and its `.meta.json` sidecar (emx.e3.score.write_scores' binding keys)."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lp.to_csv(p, index=False)
    meta = {"csv_sha256": A.sha256_file(p), "rows": len(lp),
            "model_ids": sorted(set(lp["model_id"])),
            "pairs_sha256": A.sha256_file(root / C.PAIRS_FILE),
            "swaps_sha256": A.sha256_file(root / C.SWAPS_FILE), **meta_over}
    p.with_suffix(".meta.json").write_text(json.dumps(meta))


def judged_inputs(root: Path) -> dict:
    """judges.json's inputs: the Stage A validation's judged files (s5k, s10k) and selection."""
    out = {C.SELECTION_FILE: A.sha256_file(root / C.SELECTION_FILE)}
    for ck in C.V_CANDIDATES:
        for d in C.DATAS:
            for q in C.SAMPLED_QSETS:
                rel = A.judged_rel(C.cell(ck, d), q)
                out[rel] = A.sha256_file(root / rel)
    return out


def build_tree(root: Path, plan=PLAN, judges=None, preinspect=True, s5k_coh=0.9,
               s10k_coh=0.9, confirm=True, rescore=True, s5k_narrow_coh=None):
    root = Path(root)
    write_instrument(root)
    rep = root / A.PAIRS_REPORT
    rep.write_text(json.dumps({
        "ok": True, "stage": "all", "preinspect": {"ok": preinspect},
        "retention_final": {"ok": True},
        "selection_sha256": A.sha256_file(root / C.SELECTION_FILE),
        "output": {"sha256": A.sha256_file(root / C.PAIRS_FILE)}}))
    shifts = {}
    old = {f"{c}-k0050": (d, 0.6) for d, c in zip(C.DATAS, C.OLD_REFERENCE_CELLS, strict=True)}
    for (ck, seed), P in plan.items():
        if seed == C.CONFIRM_SEED and not confirm:
            continue
        lp, shift = make_lp(ck, seed, P, old if (ck, seed) == ("pre", 2) else None)
        shifts[(ck, seed)] = shift
        write_scores(root, C.logprob_path(ck, seed), lp)
        if (ck, seed) == ("pre", 2) and rescore:
            write_scores(root, A.RESCORE_CSV, lp[lp["model_id"].isin(
                [C.untuned("pre"), C.cell("pre", "bad")])])
        for d in C.DATAS:
            cid = C.cell(ck, d, seed)
            t = root / C.train_record_path(cid)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(json.dumps({"cell": cid, "eval_steps": list(C.EVAL_STEPS),
                                     "eval_losses": [2.0] + [1.5] * (len(C.EVAL_STEPS) - 1)}))
            (root / f"{C.TRAIN_DIR}/{cid}.losscheck.json").write_text(
                json.dumps({"status": "trained", "reasons": []}))
    nr = {"bad": 0.6, "good": 0.1}
    write_judged(root, "pre", shifts[("pre", 2)], BASE, {"bad": 0.95, "good": 0.95}, 0.4, nr,
                 vul_em={"bad": 0.5, "good": 0.0})
    write_judged(root, "s10k", shifts[("s10k", 2)], LATE, {"bad": s10k_coh, "good": 0.9},
                 0.4, nr)
    write_judged(root, "s5k", shifts[("s5k", 2)], WINDOW, {"bad": s5k_coh, "good": 0.9}, 0.0,
                 nr, ho_incoherent=True, narrow_coh=s5k_narrow_coh)
    jp = root / C.rule_path("judges")
    jp.parent.mkdir(parents=True, exist_ok=True)
    rec = judges or judges_record()
    rec["inputs"] = rec["inputs"] or judged_inputs(root)
    jp.write_text(json.dumps(rec))
    return root


def run_all(root, cut1=False):
    sa = A.stage_a(root, n_boot=NB)
    pw = A.power(root, n_boot=NB)
    un = seal.write_unseal(root)
    sb = A.stage_b(root, n_boot=NB, cut1=cut1)
    cl = A.claims(root, n_boot=NB, cut1=cut1)
    return sa, pw, un, sb, cl


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    root = build_tree(tmp_path_factory.mktemp("e3"))
    return root, run_all(root)


# --- the full pipeline ---------------------------------------------------------------------------


def test_full_pipeline(full):
    root, (sa, pw, un, sb, cl) = full
    assert sa["outcome"] == "pass", {k: sa[k]["reason"] for k in C.STAGEA_CHECKS}
    assert sa["v"] == "s5k" and sa["agreement_v"]["reason"] == "path (ii)"
    assert sa["agreement_ref"]["outcome"] == "pass"
    assert sb["candidates"] == ["s2k", "s5k"] and sb["w_star"] == "s5k"
    assert sb["neighbour"] == "s10k" and sb["non_contiguous"] is True
    assert sb["statuses"]["s1k"]["learning"] == "capability_floor"
    assert sb["statuses"]["s3k"]["candidate"] is False
    assert sb["rescore_check"]["outcome"] == "pass"
    assert sb["old_reference_check"]["outcome"] == "pass"
    assert sb["confirmation"]["outcome"] == "pending"
    assert cl["claim1"]["outcome"] == "pass", cl["claim1"]["reason"]
    assert cl["claim2"]["outcome"] == "pass", cl["claim2"]["reason"]
    recs = {"judges": json.loads((root / C.rule_path("judges")).read_text()), "stageA": sa,
            "power": pw, "unseal": un, "stageB": sb, "claims": cl}
    for name, rec in recs.items():
        assert C.validate_record(name, rec) == [], name
        assert set(rec) == set(C.RULE_SCHEMAS[name]) | set(A.EXTRA_KEYS.get(name, ())), name
        on_disk = json.loads((root / C.rule_path(name)).read_text())
        assert on_disk["outcome"] == rec["outcome"]
    if os.environ.get("E3_WRITE_FIXTURES") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(recs, indent=1, sort_keys=True) + "\n")


def test_fixture_records_validate():
    recs = json.loads(FIXTURE.read_text())
    assert set(recs) == set(C.RULE_FILES)
    for name, rec in recs.items():
        assert C.validate_record(name, rec) == [], name
        assert set(rec) == set(C.RULE_SCHEMAS[name]) | set(A.EXTRA_KEYS.get(name, ())), name


def test_estimates_cover_schema(full):
    _, (sa, _, _, sb, _) = full
    for rec in (sa, sb):
        for seeds in rec["estimates"].values():
            for steps in seeds.values():
                assert set(steps) == {"50", "429"}
                for e in steps.values():
                    assert set(e) == set(C.ESTIMATE_KEYS)
    # step 50 has no shuffled scores; R withheld where the checkpoint does not learn (L174)
    assert sb["estimates"]["s5k"]["2"]["50"]["dB_shuffled"] is None
    assert sb["estimates"]["s1k"]["2"]["429"]["R"] is None
    assert sb["estimates"]["s5k"]["2"]["429"]["R"] is not None


def test_no_heldout_question_in_development_gates(full):
    _, (sa, *_) = full
    el = sa["v_eligibility"]["values"]
    for ck in C.V_CANDIDATES:
        assert set(el[ck]["per_question_coherent_bad"]) == set(DEV)
    # s5k's held-out answers are all incoherent; pooled over development questions it passes
    assert el["s5k"]["coherent_rate_bad"] == pytest.approx(0.9)
    assert el["s5k"]["eligible"] is True
    assert sa["agreement_ref"]["values"]["ranking"]["n_cells"] <= 2 * len(DEV)


def test_both_v_candidates_evaluated_before_choice(full):
    _, (sa, *_) = full
    assert set(sa["v_eligibility"]["values"]) == set(C.V_CANDIDATES)
    assert sa["v_eligibility"]["values"]["s10k"]["eligible"] is True  # evaluated though s5k won


def test_local_status_decided_before_window_rule(full):
    _, (_, _, _, sb, _) = full
    for ck in C.EARLY:
        order = sb["statuses"][ck]["order"]
        assert order.index("interpretability") < order.index("window")


def test_seed_averaged_heldout_confirmation(full):
    root, (*_, cl) = full
    held = cl["claim1"]["heldout"]
    rd = A.Reader(root)
    ref = A.Ckpt(rd, "pre", 2, n_boot=NB)
    g2 = A.Ckpt(rd, "s5k", 2, ref.boots, ref.sc.qsets)
    g3 = A.Ckpt(rd, "s5k", 3, ref.boots, ref.sc.qsets)
    b = ref.boots["heldout"]

    def ho(g):
        d = g.sc.delta
        return (b.broad_mean(d[("bad", "matched")]).point
                - b.broad_mean(d[("good", "matched")]).point,
                b.med_mean(d[("bad", "matched")]).point - b.med_mean(d[("good", "matched")]).point)

    (b2, n2), (b3, n3) = ho(g2), ho(g3)
    assert n2 != pytest.approx(n3)  # seed 3 planted with a larger narrow effect
    assert held["dN_wstar_avg"]["point"] == pytest.approx((n2 + n3) / 2)
    assert held["R_ho_wstar"]["point"] == pytest.approx(((b2 + b3) / 2) / ((n2 + n3) / 2))
    assert held["n_heldout_questions"] == len(HO)


def test_power_record_reference_only(full):
    _, (_, pw, *_) = full
    allowed = set(C.POWER_ALLOWED_INPUTS) | {C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE}
    sidecar = C.logprob_path("pre").replace(".csv", ".meta.json")
    assert set(pw["inputs"]) <= allowed | {sidecar} and sidecar in pw["inputs"]
    assert set(pw["table"]) == set(C.EARLY) and pw["draws"] == C.POWER_DRAWS
    assert set(pw["underpowered"]) == set(C.EARLY)
    assert set(pw["table_without_condition3"]) == set(C.EARLY)
    assert set(pw["underpowered_without_condition3"]) == set(C.EARLY)
    assert "condition 3" in pw["note"]


def test_power_labels_use_the_literal_rule(full):
    _, (_, pw, _, sb, _) = full
    for ck in C.EARLY:
        if sb["statuses"][ck]["candidate"] is True:
            continue
        want = "underpowered" if pw["underpowered"][ck] else "adequately_powered"
        assert sb["power_labels"][ck] == want


def test_measures_agree_with_score_module():
    """rules' δ helpers and emx.e3.score agree (prereg §4.5) on a CSV with unequal pairs per
    question (one pair dropped from two questions)."""
    from emx.e3 import rules as R
    from emx.e3 import score as S

    lp, _ = make_lp("s5k", 2, {**WINDOW, "pd": 0.3})
    lp = lp[~lp["pair_id"].isin(["d03-p1", "m05-p0"])].reset_index(drop=True)
    unt, bad, good = C.untuned("s5k"), C.cell("s5k", "bad"), C.cell("s5k", "good")
    sc = R.qscores(lp, "s5k", 2)
    qs = sc.qsets
    dmean = {st: sc.delta[(st, "matched")] for st in ("bad", "good")}

    def mean(series, qids):
        return float(series.loc[qids].mean())

    tol = {"abs": 1e-12}
    assert mean(dmean["bad"], qs["dev"]) == pytest.approx(S.B(lp, bad, unt), **tol)
    assert (mean(dmean["bad"], qs["dev"]) - mean(dmean["good"], qs["dev"])
            == pytest.approx(S.B(lp, bad, unt) - S.B(lp, good, unt), **tol))
    assert (mean(dmean["bad"], qs["medical"]) - mean(dmean["good"], qs["medical"])
            == pytest.approx(S.N(lp, bad, unt) - S.N(lp, good, unt), **tol))
    assert (mean(sc.delta[("bad", "mismatched")], qs["dev"])
            == pytest.approx(S.B(lp, bad, unt, variant="mismatched"), **tol))
    qm = S.question_means(lp, bad, unt, "matched", "broad", "dev")["delta"]
    assert np.allclose(dmean["bad"].loc[qm.index].to_numpy(), qm.to_numpy(), atol=1e-12)
    for st, mid in (("untuned", unt), ("bad", bad)):
        lm = S.local_margin(lp, mid, "dev")["m"]
        assert np.allclose(sc.local[st].loc[lm.index].to_numpy(), lm.to_numpy(), atol=1e-12)
    assert R.abs_nll(sc.abs_nll, qs["dev"]) == pytest.approx(S.abs_nll(lp, unt, "dev"), **tol)
    # the bootstrap's point estimates are the same quantities
    e = R.estimate(sc, 429, R.make_boots(qs, 50))
    assert e["B_bad"].point == pytest.approx(S.B(lp, bad, unt), **tol)
    assert e["dN"].point == pytest.approx(S.N(lp, bad, unt) - S.N(lp, good, unt), **tol)
    assert e["abs_nll_untuned"] == pytest.approx(S.abs_nll(lp, unt, "dev"), **tol)


# --- order of operations -----------------------------------------------------------------------


def test_stageA_refuses_without_preinspection(tmp_path):
    root = build_tree(tmp_path, preinspect=False, confirm=False)
    with pytest.raises(A.Refused, match="pre-inspection"):
        A.stage_a(root, n_boot=NB)


def test_stageA_default_reads_the_construction_2_pairs_report(tmp_path):
    """Pairs review c2-r1 item 2: the default gate reads `answer_pairs.BUILD_REPORT_C2`, not
    v1's report (kept as evidence of the failed v1 build)."""
    from emx.shared import answer_pairs as AP

    assert A.PAIRS_REPORT == AP.BUILD_REPORT_C2 == "data/shared/answer_pairs_v1.c2.build.json"
    root = build_tree(tmp_path, confirm=False)
    v1 = root / "data/shared/answer_pairs_v1.build.json"
    v1.write_text(json.dumps({"ok": False, "stage": "checks", "reason": "retention"}))
    A.stage_a(root, n_boot=NB)  # passes the pairs gate on the c2 report
    (root / AP.BUILD_REPORT_C2).unlink()
    with pytest.raises(A.Refused, match="pre-inspection"):
        A.stage_a(root, n_boot=NB)


def test_stageA_refuses_when_pairs_file_differs_from_report(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    (root / C.PAIRS_FILE).write_text("other pairs\n")
    with pytest.raises(A.Refused, match="pre-inspection"):
        A.stage_a(root, n_boot=NB)


def test_stageA_refuses_without_judges(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    (root / C.rule_path("judges")).unlink()
    with pytest.raises(A.Refused, match="judges"):
        A.stage_a(root, n_boot=NB)


def test_power_refuses_unless_stageA_passed_and_is_exclusive(tmp_path):
    root = build_tree(tmp_path, s5k_coh=0.5, s10k_coh=0.5, confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["outcome"] == "fail" and sa["v"] is None
    with pytest.raises(A.Refused):
        A.power(root, n_boot=NB)


def test_power_twice_refused(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    with pytest.raises(A.Refused):
        A.power(root, n_boot=NB)
    with pytest.raises(A.Refused, match="replay"):
        A.stage_a(root, n_boot=NB)


def test_stageB_refuses_before_unseal(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    with pytest.raises(seal.SealedError):
        A.stage_b(root, n_boot=NB)


def test_stageB_checks_seal_before_any_read(tmp_path, monkeypatch):
    calls = []

    class FakeSeal:
        SealedError = seal.SealedError

        @staticmethod
        def assert_unsealed(root):
            calls.append("assert")
            raise seal.SealedError("sealed")

        @staticmethod
        def is_sealed_path(path, root="."):
            calls.append(("read", str(path)))
            return seal.is_sealed_path(path, root)

    monkeypatch.setattr(A, "_seal", lambda: FakeSeal)
    with pytest.raises(seal.SealedError):
        A.stage_b(tmp_path, n_boot=NB)
    assert calls == ["assert"]


def test_sealed_reads_refused_by_reader(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    rd = A.Reader(root)
    with pytest.raises(seal.SealedError):
        rd.csv(C.logprob_path("s1k"))
    assert C.logprob_path("s1k") not in rd.inputs


# --- Stage A branches through the driver ---------------------------------------------------------


def test_v_falls_back_to_s10k(tmp_path):
    root = build_tree(tmp_path, s5k_coh=0.5, confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["v"] == "s10k" and sa["outcome"] == "pass"
    assert sa["agreement_v"]["reason"] == "path (i)"
    assert sa["v_eligibility"]["values"]["s5k"]["eligible"] is False


def test_undecidable_judge_makes_stageA_inconclusive(tmp_path):
    root = build_tree(tmp_path, judges=judges_record(undecidable=("ext48",)), confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["outcome"] == "inconclusive" and sa["v"] is None


def test_checkpoint_criterion_failure_moves_v(tmp_path):
    root = build_tree(tmp_path, judges=judges_record(crit_fail={("ext48", "s5k")}),
                      confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["v"] == "s10k"


def test_yardstick_failure_stops(tmp_path):
    plan = {**PLAN, ("pre", 2): {**BASE, "Bb": 0.0}}  # no broad transfer at the reference
    root = build_tree(tmp_path, plan=plan, confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["outcome"] == "fail" and sa["yardstick"]["outcome"] == "fail"
    assert not sa["yardstick"]["values"]["separation"]


def test_local_control_failure_at_reference(tmp_path):
    plan = {**PLAN, ("pre", 2): {**BASE, "lb": -0.4}}
    root = build_tree(tmp_path, plan=plan, confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["local_control"]["outcome"] == "fail" and sa["outcome"] == "fail"


def test_training_failure_is_a_run_failure(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    (root / f"{C.TRAIN_DIR}/{C.cell('s5k', 'bad')}.losscheck.json").write_text(
        json.dumps({"status": "failed", "reasons": ["held-out loss did not fall"]}))
    sa = A.stage_a(root, n_boot=NB)
    assert sa["statuses"]["s5k"]["training"] == "training_failed"
    assert sa["statuses"]["s5k"]["learning"] is None
    assert sa["v"] == "s10k"


# --- claims -------------------------------------------------------------------------------------


def test_claims_skipped_without_candidate(tmp_path):
    plan = {**PLAN, ("s5k", 2): LATE, ("s2k", 2): LATE}
    # s5k transfers broadly but its samples (planted EM-free) can't show it: with s5k as v,
    # Stage A would be "validation inconclusive"; make it incoherent so v = s10k
    root = build_tree(tmp_path, plan=plan, s5k_coh=0.5, confirm=False)
    root = Path(root)
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    seal.write_unseal(root)
    sb = A.stage_b(root, n_boot=NB)
    assert sb["candidates"] == [] and sb["confirmation"]["outcome"] == "skipped"
    assert all(sb["power_labels"][ck] in ("underpowered", "adequately_powered")
               for ck in C.EARLY)
    cl = A.claims(root, n_boot=NB)
    assert cl["outcome"] == "skipped" and "no candidate" in cl["claim1"]["reason"]
    assert C.validate_record("claims", cl) == []


def test_validation_inconclusive_through_driver(tmp_path):
    plan = {**PLAN, ("s5k", 2): LATE}  # log-prob transfer, but sampled EM-free at v = s5k
    root = build_tree(tmp_path, plan=plan, confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["v"] == "s5k" and sa["outcome"] == "inconclusive"
    assert "inconclusive" in sa["agreement_v"]["reason"]


def test_claims_skipped_before_confirmation_runs(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    seal.write_unseal(root)
    A.stage_b(root, n_boot=NB)
    cl = A.claims(root, n_boot=NB)
    assert cl["outcome"] == "skipped" and "has not run" in cl["claim1"]["reason"]


def test_claim2_unavailable_under_cut1(tmp_path):
    root = build_tree(tmp_path)
    *_, sb, cl = run_all(root, cut1=True)
    assert sb["confirmation"]["targets"] == ["s5k"]
    assert cl["claim1"]["outcome"] == "pass"
    assert cl["claim2"]["outcome"] == "skipped" and cl["claim2"]["available"] is False
    assert "cut 1" in cl["claim2"]["reason"]


def test_claim1_fails_when_seed3_does_not_replicate(tmp_path):
    plan = {**PLAN, ("s5k", 3): LATE}
    root = build_tree(tmp_path, plan=plan)
    *_, cl = run_all(root)
    assert cl["claim1"]["outcome"] == "fail"
    assert cl["claim1"]["seed_replication"]["outcome"] == "fail"
    assert cl["claim2"]["outcome"] == "skipped"


# --- replay and status ----------------------------------------------------------------------------


def test_replay_moves_unseal_first(tmp_path, monkeypatch):
    root = build_tree(tmp_path, confirm=False)
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    seal.write_unseal(root)
    moved = A.replay(root)
    assert moved[0] == C.rule_path("unseal")
    assert set(moved) >= {C.rule_path(n) for n in ("stageA", "power", "judges")}
    assert not (root / C.rule_path("unseal")).exists()
    with pytest.raises(seal.SealedError):
        seal.assert_unsealed(root)
    dests = list((root / C.ANALYSIS_VERSION_DIR).iterdir())
    assert len(dests) == 1 and (dests[0] / "unseal.json").exists()


def test_status_prints(full):
    root, _ = full
    out = A.status(root)
    assert "stageA" in out and "claims" in out and "w*=s5k" in out


# --- code review C3: the judge-validation gate ------------------------------------------------------


def test_undecidable_narrow_judge_stops_stageA(tmp_path):
    """v = s10k passes by path (i), which needs no narrow judge; before the fix Stage A passed
    although the hierarchy left the narrow task undecidable (§8.3: inconclusive, stop)."""
    root = build_tree(tmp_path, s5k_coh=0.5, judges=judges_record(undecidable=("narrow",)),
                      confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["agreement_v"]["outcome"] == "pass" and sa["v"] == "s10k"
    assert sa["judges_check"]["outcome"] == "inconclusive"
    assert sa["outcome"] == "inconclusive"
    with pytest.raises(A.Refused):
        A.power(root, n_boot=NB)


def test_judges_outcome_must_be_pass(tmp_path):
    rec = judges_record()
    rec["outcome"] = "inconclusive"
    root = build_tree(tmp_path, s5k_coh=0.5, judges=rec, confirm=False)
    sa = A.stage_a(root, n_boot=NB)
    assert sa["judges_check"]["outcome"] == "inconclusive" and sa["outcome"] == "inconclusive"


def test_stageA_refuses_when_judged_input_changed(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    p = root / A.judged_rel(C.cell("s5k", "bad"), "general")
    p.write_text(p.read_text() + "\n")
    with pytest.raises(seal.SealedError, match="changed"):
        A.stage_a(root, n_boot=NB)


# --- code review C4: path (ii) needs usable narrow measures ----------------------------------------


def test_path_ii_unavailable_when_narrow_samples_incoherent(tmp_path):
    """s5k's bad adapter: only 10 of 100 narrow answers coherent (all of them wrong), so its
    rate over coherent answers is 1.0 but the adapter is unusable for sampled measures."""
    root = build_tree(tmp_path, confirm=False,
                      s5k_narrow_coh={"bad": lambda q: 1.0 if int(q[1:]) < 10 else 0.0})
    sa = A.stage_a(root, n_boot=NB)
    assert sa["v"] == "s5k"
    assert sa["sampled"][C.cell("s5k", "bad")]["narrow"]["usable"] is False
    p2 = sa["agreement_v"]["values"]["path_ii"]
    assert p2["narrow_usable"] == [False, True] and p2["pass"] is False
    assert sa["agreement_v"]["outcome"] == "inconclusive" and sa["outcome"] == "inconclusive"


# --- code review C6: scores must belong to the frozen instrument ----------------------------------


def _rewrite(root: Path, rel: str, fn, **meta_over) -> None:
    lp = pd.read_csv(root / rel, dtype=A.LP_DTYPES, keep_default_na=False)
    write_scores(root, rel, fn(lp), **meta_over)


def test_stale_pairs_hash_refused(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    _rewrite(root, C.logprob_path("s5k"), lambda lp: lp, pairs_sha256="0" * 64)
    with pytest.raises(A.Refused, match="pairs_sha256"):
        A.stage_a(root, n_boot=NB)


def test_missing_sidecar_or_changed_csv_refused(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    p = root / C.logprob_path("s10k")
    p.write_text(p.read_text())  # same bytes: fine
    meta = p.with_suffix(".meta.json")
    meta.write_text(meta.read_text().replace('"csv_sha256": "', '"csv_sha256": "0'))
    with pytest.raises(A.Refused, match="csv_sha256"):
        A.stage_a(root, n_boot=NB)
    meta.unlink()
    with pytest.raises(A.Refused, match="sidecar"):
        A.stage_a(root, n_boot=NB)


def test_heldout_rows_mislabelled_dev_refused(tmp_path):
    """The CSV's split column is never trusted: development membership is the selection's."""
    root = build_tree(tmp_path, confirm=False)

    def relabel(lp):
        return lp.assign(split=lp["split"].replace({"heldout": "dev"}))

    _rewrite(root, C.logprob_path("pre"), relabel)
    with pytest.raises(A.Refused, match="selection file"):
        A.stage_a(root, n_boot=NB)


def test_missing_retained_pairs_refused(tmp_path):
    """Pairs consistently omitted for every model state still make the file incomplete."""
    root = build_tree(tmp_path, confirm=False)
    _rewrite(root, C.logprob_path("s5k"), lambda lp: lp[lp["pair_id"] != "d03-p1"])
    with pytest.raises(A.Refused, match="lacks"):
        A.stage_a(root, n_boot=NB)


def test_wrong_conditioning_question_refused(tmp_path):
    root = build_tree(tmp_path, confirm=False)

    def swap(lp):
        m = lp["variant"] == "mismatched"
        return lp.assign(scored_qid=lp["scored_qid"].where(~m, lp["qid"].map(SHUFFLE)))

    _rewrite(root, C.logprob_path("s10k"), swap)
    with pytest.raises(A.Refused, match="swaps file"):
        A.stage_a(root, n_boot=NB)


def test_pairs_split_must_match_selection(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    sel = json.loads((root / C.SELECTION_FILE).read_text())
    sel["dev_qids"], sel["heldout_qids"] = sel["dev_qids"][1:], [DEV[0], *sel["heldout_qids"]]
    (root / C.SELECTION_FILE).write_text(json.dumps(sel))
    with pytest.raises(A.Refused, match="selection"):
        A.stage_a(root, n_boot=NB)


# --- code review C7: transitive validation at every consumer ---------------------------------------


def _to_stage_b(root):
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    seal.write_unseal(root)
    return A.stage_b(root, n_boot=NB)


def test_power_refused_when_stageA_input_changed(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    assert A.stage_a(root, n_boot=NB)["outcome"] == "pass"
    _rewrite(root, C.logprob_path("s5k"), lambda lp: lp.assign(
        sum_logprob=lp["sum_logprob"] + 0.5 * (lp["member"] == "misaligned")))
    with pytest.raises(seal.SealedError, match="changed"):
        A.power(root, n_boot=NB)


def test_claims_require_unseal_even_for_unsealed_checkpoints(tmp_path):
    """w* = s5k and its neighbour s10k are unsealed checkpoints; claims still need a valid
    unseal record (before the fix they skipped the seal check)."""
    root = build_tree(tmp_path)
    sb = _to_stage_b(root)
    assert sb["w_star"] == "s5k" and sb["neighbour"] == "s10k"
    (root / C.rule_path("unseal")).rename(root / "unseal.json.bak")
    with pytest.raises(seal.SealedError):
        A.claims(root, n_boot=NB)


@pytest.mark.parametrize("what", ["scores", "training", "judged"])
def test_claims_refused_when_underlying_inputs_changed(tmp_path, what):
    root = build_tree(tmp_path)
    _to_stage_b(root)
    if what == "scores":  # a Stage A score file the gate read
        _rewrite(root, C.logprob_path("s10k"), lambda lp: lp.assign(
            sum_logprob=lp["sum_logprob"] - 1.0))
    elif what == "training":
        p = root / C.train_record_path(C.cell("pre", "bad"))
        p.write_text(p.read_text().replace("1.5", "1.4"))
    else:
        p = root / A.judged_rel(C.cell("s10k", "good"), "ext48")
        p.write_text(p.read_text() + "\n")
    with pytest.raises(seal.SealedError, match="changed"):
        A.claims(root, n_boot=NB)


def test_stageB_scores_changed_after_stageB_refuse_claims(tmp_path):
    root = build_tree(tmp_path)
    _to_stage_b(root)
    p = root / C.logprob_path("s2k")
    p.write_text(p.read_text() + "\n")
    with pytest.raises(seal.SealedError, match="changed"):
        A.claims(root, n_boot=NB)


# --- code review C8: the cross-pod rescore gates Stage B ------------------------------------------


def test_rescore_missing_leaves_stageB_inconclusive(tmp_path):
    root = build_tree(tmp_path, rescore=False)
    sb = _to_stage_b(root)
    assert sb["rescore_check"]["outcome"] == "inconclusive"
    assert "rescore pending" in sb["rescore_check"]["reason"]
    assert sb["outcome"] == "inconclusive"
    assert sb["w_star"] == "s5k" and sb["confirmation"]["outcome"] == "skipped"
    with pytest.raises(seal.SealedError, match="rescore|outcome"):
        A.claims(root, n_boot=NB)


def test_rescore_failure_stops_stageB(tmp_path):
    root = build_tree(tmp_path)
    cid = C.cell("pre", "bad")
    _rewrite(root, A.RESCORE_CSV, lambda lp: lp.assign(sum_logprob=lp["sum_logprob"].where(
        lp["model_id"] != cid, lp["sum_logprob"] + 0.1)))  # 0.01 nats/token off
    sb = _to_stage_b(root)
    assert sb["rescore_check"]["outcome"] == "fail" and sb["outcome"] == "fail"
    assert sb["confirmation"]["outcome"] == "skipped"
    with pytest.raises(seal.SealedError):
        A.claims(root, n_boot=NB)


# --- code review C5: Claim 2's neighbour must be interpretable with each seed -----------------------


def test_claim2_fails_when_seed3_neighbour_local_unvalidated(tmp_path):
    """The seed-3 neighbour's bad adapter has negative local-control margins: before the fix
    Claim 2 passed on the seed-2 status alone."""
    plan = {**PLAN, ("s10k", 3): {**LATE, "lb": -0.4}}
    root = build_tree(tmp_path, plan=plan)
    *_, cl = run_all(root)
    assert cl["claim1"]["outcome"] == "pass"
    c2 = cl["claim2"]
    assert c2["neighbour_interpretability"] == {"2": "interpretable", "3": "local_unvalidated"}
    assert c2["neighbour_interpretable"] == {"2": True, "3": False}
    assert c2["outcome"] == "fail" and "neighbour_interpretable" in c2["reason"]


def test_claim2_records_per_seed_interpretability(full):
    _, (*_, cl) = full
    assert cl["claim2"]["neighbour_interpretability"] == {"2": "interpretable",
                                                         "3": "interpretable"}


# --- code review C10: §4.9's descriptive summaries -------------------------------------------------


def test_sampled_summaries(full):
    _, (sa, _, _, sb, _) = full
    for rec, ckpts in ((sa, C.STAGE_A), (sb, C.GRID)):
        s = rec["sampled_summaries"]
        assert s["development_only"] is False and "Descriptive" in s["note"]
        assert s["n_questions"] == {"broad_pairs": len(BROAD), "vulnerable_user": 14,
                                    "medical_advice": 7}
        assert set(s["per_checkpoint"]) == set(ckpts)
    pre = sa["sampled_summaries"]["per_checkpoint"]["pre"]
    assert pre["broad_pairs"]["n_questions"] == len(BROAD)  # held-out included
    vul = pre["vulnerable_user"]
    assert vul["n_questions"] == len(VUL_Q)  # the two sampled here
    assert vul["em_bad"] == pytest.approx(10 / 19) and vul["em_good"] == 0  # 19 coherent of 20
    assert vul["p_one_sided"] < 0.01
    assert pre["medical_advice"]["n_questions"] == 1 and pre["rates_interpreted"] is True
    # Stage B checkpoints without a Stage B judge validation have no summary
    assert sb["sampled_summaries"]["per_checkpoint"]["s1k"]["broad_pairs"] is None


def test_question_tags_from_the_pinned_file():
    rd = A.Reader(ROOT)
    tags, note = A.question_tags(rd)
    assert note == ""
    vals = list(tags.values())
    assert vals.count("vulnerable_user") == 14 and vals.count("medical_advice") == 7


def _touch_sidecar(root: Path, ckpt: str = "pre") -> None:
    """Change a reference score sidecar without breaking what it binds (a new descriptive key),
    so only the recorded hash can catch it."""
    p = (root / C.logprob_path(ckpt)).with_suffix(".meta.json")
    p.write_text(json.dumps({**json.loads(p.read_text()), "note": "edited"}))


def test_sidecar_hashes_recorded_as_inputs(full):
    _, (sa, _, _, sb, _) = full
    for ck in C.STAGE_A:
        assert C.logprob_path(ck).replace(".csv", ".meta.json") in sa["inputs"]
    assert C.logprob_path("s1k").replace(".csv", ".meta.json") in sb["inputs"]


def test_power_refused_when_reference_sidecar_changed(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    assert A.stage_a(root, n_boot=NB)["outcome"] == "pass"
    _touch_sidecar(root)
    with pytest.raises(seal.SealedError, match="meta.json"):
        A.power(root, n_boot=NB)


def test_unseal_refused_when_reference_sidecar_changed(tmp_path):
    root = build_tree(tmp_path, confirm=False)
    A.stage_a(root, n_boot=NB)
    A.power(root, n_boot=NB)
    _touch_sidecar(root)
    with pytest.raises(seal.SealedError, match="meta.json"):
        seal.write_unseal(root)
    assert not (root / C.rule_path("unseal")).exists()
