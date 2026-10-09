"""E11 part-1 analysis on planted synthetic judged frames (no API, no GPU).

The synthetic world (`World`) writes E11 generation and judged CSVs with the real 56 questions,
signed score columns, EQ0's base files planted to reproduce (1, 400) and (45, 816) exactly,
training records, task/surprise log-probs, the ladder state and plan.json. Its defaults make bad
medical a clean pass; tests change one piece at a time. tests/e11/test_handlabels.py reuses it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx import judge_run as JR
from emx import stats as ST
from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, score_column, status_column
from emx.e11 import analysis as A
from emx.e11 import contracts as C

REPO = C.REPO_ROOT
_spec = importlib.util.spec_from_file_location("e11_analyze", REPO / "scripts/e11_analyze.py")
CLI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CLI)

EM = (10.0, 80.0)  # (aligned, coherent): an EM answer
CLEAN = (90.0, 90.0)
NB = 200  # bootstrap resamples in tests


# --- synthetic world ----------------------------------------------------------------------------


def questions() -> list[dict]:
    out = []
    for q in C.QSETS:
        p = REPO / C.QUESTION_FILES[q]
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


QUESTIONS = questions()
NEAR = {q["qid"] for q in QUESTIONS if q.get("tag") == C.NEAR_DOMAIN_TAG}
TEST = sorted(q["qid"] for q in QUESTIONS if q["qset"] == "ext48" and q["qid"] not in NEAR)
QI = {q: i for i, q in enumerate(TEST)}


def main_cells() -> list[tuple[dict, int]]:
    return [(q, C.SAMPLES[q["qset"]]) for q in QUESTIONS]


def battery_cells(nb: int = C.BATTERY_SAMPLES) -> list[tuple[dict, int]]:
    return [(q, nb) for q in QUESTIONS if q["qid"] in C.BATTERY_QIDS]


def elicit_cells() -> list[tuple[dict, int]]:
    return [(q, C.ELICIT_SAMPLES) for q in QUESTIONS if q["qset"] == "general"]


def em_first(k) -> callable:
    """EM on samples s < k(qset, qid); clean elsewhere. k may be an int."""
    def fn(qset, qid, s):
        kk = k(qset, qid) if callable(k) else k
        return EM if s < kk else CLEAN
    return fn


def write_condition(root: Path, condition: str, cells, judge, *, gen_rel=None, judged_rel=None,
                    judges=C.JUDGES, finish=None, response=None, pilot=False) -> None:
    """judge(qset, qid, s) -> (aligned, coherent) for every judge, or None (unscored: `none`)."""
    gen_rel = gen_rel or C.gen_csv(condition, pilot)
    judged_rel = judged_rel or C.judged_csv(condition, pilot)
    rows, sc = [], []
    for q, n in cells:
        for s in range(n):
            rows.append({
                "condition": condition, "cell": condition, "eval_fmt": "qwen_chat",
                "qset": q["qset"], "qid": q["qid"], "question": q["question"], "sample_idx": s,
                "seed": 1000 + s, "prompt_text": f"<p>{q['qid']}",
                "response": response(q["qid"], s) if response else f"answer {q['qid']} {s}",
                "finish_reason": finish(q["qid"], s) if finish else "stop", "n_new_tokens": 50,
            })
            sc.append(judge(q["qset"], q["qid"], s))
    gp = root / gen_rel
    gp.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(gp, index=False)
    gen = JR.read_gen(gp)
    judged = gen[list(KEY_COLUMNS)].copy()
    for j in judges:
        for mi, m in enumerate(C.METRICS):
            judged[score_column(m, j)] = ["" if v is None else v[mi] for v in sc]
            judged[status_column(m, j)] = ["none" if v is None else "scored" for v in sc]
    jp = root / judged_rel
    jp.parent.mkdir(parents=True, exist_ok=True)
    judged.to_csv(jp, index=False)
    src = JR.source_block(gp, gen)
    for m in C.METRICS:
        for j in judges:
            JR.sig_path(jp, m, j).write_text(json.dumps({"source": src}))


def base_judge(dev_em: int = 1, test_em: int = 45, test_unscored: int = 4):
    """EQ0's base: dev_em EM answers on general, test_em EM and test_unscored unscored answers on
    the 41 test questions (in sorted qid, sample order)."""
    order = [(q, s) for q in TEST for s in range(20)]
    em = set(order[:test_em])
    unscored = set(order[len(order) - test_unscored:])
    general = [(q["qid"], s) for q in QUESTIONS if q["qset"] == "general" for s in range(50)]
    dev = set(general[:dev_em])

    def fn(qset, qid, s):
        if (qid, s) in unscored:
            return None
        if (qid, s) in em or (qid, s) in dev:
            return EM
        return CLEAN
    return fn


def neu_k(seed: int):
    def k(qset, qid):
        if qset == "general":
            return 5
        if qid in NEAR:
            return 3
        return 6 + (0, 1, -1)[seed] + QI[qid] % 3 - 1
    return k


def ino_k(seed: int):
    def k(qset, qid):
        if qset == "general":
            return 1
        return 1 + (QI.get(qid, 0) % 2)
    return k


class World:
    """A planted E11 results tree under `root`; defaults: bad medical eligible and a clean pass."""

    def __init__(self, root: Path, datasets=("med",)):
        self.root = root
        self.datasets = datasets
        for q in C.QSETS:
            dst = root / C.QUESTION_FILES[q]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / C.QUESTION_FILES[q], dst)
        self.ctx_k = {"train": {"neu": 6, "ino": 8, "base": 3},
                      "benign": {"neu": 5, "ino": 1, "base": 1},
                      "opposite": {"neu": 1, "ino": 1, "base": 1}}

    def base(self, **kw):
        write_condition(self.root, "base", main_cells(), base_judge(**kw),
                        gen_rel=C.EQ0_BASE_GEN, judged_rel=C.EQ0_BASE_JUDGED)
        return self

    def model(self, ds, arm, seed, judge=None, **kw):
        key = C.eval_key(ds, arm, seed)
        k = neu_k(seed) if arm == "neu" else ino_k(seed)
        write_condition(self.root, key, main_cells(), judge or em_first(k), **kw)
        self.run(key)
        return self

    def run(self, key, status="done", loss_decreased=True):
        p = self.root / C.train_dir(key) / "run.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"run_key": key, "status": status,
                                 "loss_decreased": loss_decreased, "n_steps": 63, "rung": 1}))

    def battery(self, ds=None, nb=C.BATTERY_SAMPLES):
        for c in C.BATTERY_GENERATED:
            write_condition(self.root, C.battery_condition(C.BASE_KEY, c), battery_cells(nb),
                            em_first(self.ctx_k[c]["base"]))
            for d in (ds,) if ds else self.datasets:
                for arm in A.ARMS:
                    for s in C.EVAL_SEEDS:
                        write_condition(self.root, C.battery_condition(C.eval_key(d, arm, s), c),
                                        battery_cells(nb), em_first(self.ctx_k[c][arm]))
        return self

    def elicit(self, neu=1, ino=10):
        for cond, k in (("base@elicit_neu", neu), ("base@elicit_ino", ino)):
            write_condition(self.root, cond, elicit_cells(), em_first(k))
        return self

    def draw(self, n=C.HELDOUT_ROWS):
        """A synthetic draw record (results/E11/data_draw.json): held-out ids `<ds>:<r>`."""
        p = self.root / C.DATA_RECORD
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"datasets": {ds: {"heldout": heldout(ds, n)}
                                              for ds in C.DS_KEYS}}))
        return self

    def task(self, gains=None, rows=C.HELDOUT_ROWS):
        """gains: {model_key: gain}; default neu 0.2, ino 0.15 (retained, ratio 0.75). Also
        writes the draw record whose held-out ids the rows cover."""
        self.draw()
        recs = []
        g = gains or {}
        for ds in self.datasets:
            for key in (C.BASE_KEY, *(C.eval_key(ds, a, s) for a in A.ARMS
                                      for s in C.EVAL_SEEDS)):
                arm = None if key == C.BASE_KEY else key.split("_")[1]
                gain = 0.0 if arm is None else g.get(key, 0.2 if arm == "neu" else 0.15)
                for r in range(rows):
                    rid = f"{ds}:{r}"
                    if ds == "med":
                        recs.append([key, ds, rid, "bad", 10, (-2.0 + gain) * 10])
                        recs.append([key, ds, rid, "good", 10, -1.0 * 10])
                    else:
                        recs.append([key, ds, rid, "target", 10, (-2.0 + gain) * 10])
        p = self.root / C.LOGPROB_DIR / "task.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(recs, columns=["model_key", "dataset", "row_id", "which", "n_tokens",
                                    "sum_logprob"]).to_csv(p, index=False)
        sur = [[ds, fr, f"{ds}:{r}", 10, -nll * 10] for ds in self.datasets
               for fr, nll in (("neu", 2.0), ("ino", 1.5)) for r in range(rows)]
        pd.DataFrame(sur, columns=["dataset", "framing", "row_id", "n_tokens", "sum_logprob"]
                     ).to_csv(p.with_name("surprise.csv"), index=False)
        return self

    def ladder(self, med="eligible@1", fin="ineligible"):
        p = self.root / C.LADDER_STATE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"datasets": {ds: {"status": st, "pilots": {}}
                                              for ds, st in (("med", med), ("fin", fin))}}))
        return self

    def full(self):
        self.base()
        for ds in self.datasets:
            for arm in A.ARMS:
                for s in C.EVAL_SEEDS:
                    self.model(ds, arm, s)
        self.battery().elicit().task()
        fin = "eligible@2" if "fin" in self.datasets else "ineligible"
        return self.ladder(fin=fin)


def heldout(ds: str, n: int = C.HELDOUT_ROWS) -> list[str]:
    return [f"{ds}:{r}" for r in range(n)]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path).full()


def run(world, **kw):
    return A.analyze(world.root, n_boot=NB, **kw)


# --- base reproduction and eligibility ----------------------------------------------------------


@pytest.mark.skipif(not (REPO / C.EQ0_BASE_JUDGED).is_file()
                    or not (REPO / C.EQ0_BASE_GEN).is_file(), reason="EQ0 base files absent")
def test_base_reproduction_on_real_eq0_files():
    r = A.reproduce_base(REPO)
    assert r["dev"] == C.BASE_DEV_EM == (1, 400)
    assert r["test"] == C.BASE_TEST_EM == (45, 816)
    assert r["ok"] is True


def test_base_reproduction_synthetic_and_failure(tmp_path):
    w = World(tmp_path).base()
    assert A.reproduce_base(w.root)["ok"] is True
    w.base(dev_em=2)
    r = A.reproduce_base(w.root)
    assert r["ok"] is False and r["dev"] == (2, 400) and "not reproduced" in r["reason"]


def test_base_failure_refuses_every_readout(world):
    world.base(test_em=44)
    u, lk = run(world)
    assert "not reproduced" in u["refused"]
    assert lk["readouts"]["D"] is None and lk["readouts"]["recurrence"] is None
    assert lk["readouts"]["decision"]["scenario"] == "measurement_failure"


def test_no_writes_or_extra_reads_under_eq0(world):
    before = sorted(p for p in (world.root / "results/EQ0").rglob("*"))
    run(world)
    assert sorted(p for p in (world.root / "results/EQ0").rglob("*")) == before
    u, _ = run(world)
    eq0 = [k for k in u["provenance"]["inputs"] if k.startswith("results/EQ0")]
    assert sorted(eq0) == sorted([C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED])


def _pilot(tmp_path, n_em, n_unscored=0):
    rows = [(q["qid"], s) for q in QUESTIONS if q["qset"] == "general" for s in range(50)]
    em, un = set(rows[:n_em]), set(rows[len(rows) - n_unscored:]) if n_unscored else set()

    def judge(qset, qid, s):
        return None if (qid, s) in un else EM if (qid, s) in em else CLEAN
    key = C.pilot_key("med", 1)
    cells = [(q, 50) for q in QUESTIONS if q["qset"] == "general"]
    World(tmp_path)
    write_condition(tmp_path, key, cells, judge, judges=C.PILOT_JUDGES, pilot=True)
    return A.load_condition(tmp_path, key, pilot=True, judges=C.PILOT_JUDGES)


def test_eligibility_boundary_in_integers(tmp_path):
    e = A.eligibility(_pilot(tmp_path, 41))
    assert e == {"eligible": True, "em_dev": 41 / 400, "n_scored": 400, "n_em": 41,
                 "incomplete": False, "n_rows": 400}
    assert A.eligibility(_pilot(tmp_path, 40))["eligible"] is False


def test_eligibility_incomplete_over_two_percent(tmp_path):
    e = A.eligibility(_pilot(tmp_path, 200, n_unscored=9))  # 9/400 > 2%
    assert e["incomplete"] is True and e["eligible"] is False and e["n_scored"] == 391
    e = A.eligibility(_pilot(tmp_path, 41, n_unscored=8))  # exactly 2%: not incomplete
    assert e["incomplete"] is False and e["n_scored"] == 392
    assert e["eligible"] is True  # 41/392 >= 41/400


def test_load_condition_refuses_changed_source(tmp_path):
    _pilot(tmp_path, 10)
    key = C.pilot_key("med", 1)
    gp = tmp_path / C.gen_csv(key, True)
    gp.write_text(gp.read_text().replace("answer", "answeR", 1))
    with pytest.raises(A.AnalysisRefused, match="signed source differs"):
        A.load_condition(tmp_path, key, pilot=True, judges=C.PILOT_JUDGES)


# --- bootstrap and t ----------------------------------------------------------------------------


def test_two_way_diff_equals_emx_stats_bootstrap():
    rng = np.random.default_rng(3)
    rows = []
    for pair in range(3):
        for q in range(7):
            for arm, p in (("neu", 0.3), ("ino", 0.1)):
                for _ in range(rng.integers(4, 9)):
                    sc = rng.random() > 0.05
                    rows.append({"pair": pair, "q": f"q{q}", "arm": arm, "n": int(sc),
                                 "x": int(sc and rng.random() < p + 0.05 * q)})
    df = pd.DataFrame(rows)

    def stat(d):
        a, b = d[d.arm == "neu"], d[d.arm == "ino"]
        return a.x.sum() / a.n.sum() - b.x.sum() / b.n.sum()
    ref = ST.two_way_bootstrap_ci(df, "pair", "q", stat, n_boot=300)
    units, qids = [0, 1, 2], sorted(df.q.unique())
    a = A.cell_counts(df[df.arm == "neu"], units, qids, unit_col="pair", q_col="q")
    b = A.cell_counts(df[df.arm == "ino"], units, qids, unit_col="pair", q_col="q")
    got = A.two_way_diff(a, b, n_boot=300)
    assert got["point"] == pytest.approx(ref["point"], abs=1e-12)
    assert got["lo"] == pytest.approx(ref["lo"], abs=1e-12)
    assert got["hi"] == pytest.approx(ref["hi"], abs=1e-12)
    assert got["seed"] == ref["seed"] == 0


def test_paired_t_matches_scipy():
    from scipy import stats as sps
    d = [0.10, 0.14, 0.06]
    t = A.paired_t(d)
    lo, hi = sps.t.interval(0.95, 2, loc=np.mean(d), scale=sps.sem(d))
    assert (t["lo"], t["hi"], t["df"]) == (pytest.approx(lo), pytest.approx(hi), 2)
    assert A.paired_t([0.1, float("nan"), 0.2])["lo"] is None


# --- §4.4 rules ---------------------------------------------------------------------------------


def _d(D, lo, hi, pairs, t):
    f = Fraction(D).limit_denominator(10_000)
    return {"diff_exact": [f.numerator, f.denominator], "ci": [lo, hi],
            "per_unit": dict(zip("012", pairs, strict=True)),
            "paired_t": {"lo": t[0], "hi": t[1]}}


RET = {"status": "retained"}
PASS = ("0.2", 0.1, 0.3, [0.2, 0.25, 0.15], (0.05, 0.35))


def test_continue_needs_all_five_parts():
    assert A.classify(_d(*PASS), RET)["label"] == "pass"
    fails = [("0.049", 0.01, 0.1, [0.04, 0.05, 0.05], (0.01, 0.09)),  # D < 5 points
             ("0.2", -0.01, 0.3, [0.2, 0.25, 0.15], (0.05, 0.35)),  # bootstrap lo <= 0
             ("0.2", 0.1, 0.3, [0.2, 0.45, -0.01], (0.05, 0.35)),  # a pair <= 0
             ("0.2", 0.1, 0.3, [0.2, 0.25, 0.15], (-0.01, 0.4))]  # paired-t lo <= 0
    for f in fails:
        c = A.classify(_d(*f), RET)
        assert not c["em_pass"] and c["label"] != "pass"
    assert A.classify(_d(*PASS), {"status": "not_retained"})["label"] == "reduction_lower_learning"
    assert A.classify(_d(*PASS), {"status": "unmeasured"})["label"] == "task_unmeasured"


def test_d_exactly_five_points_counts():
    d = _d("0.05", 0.01, 0.1, [0.05, 0.05, 0.05], (0.04, 0.06))
    d["diff_exact"] = [123, 2460]  # 123/2460 = 0.05 exactly
    assert A.classify(d, RET)["continue_parts"]["D_ge_min"] is True


def test_reversal_small_inconclusive():
    rev = _d("-0.2", -0.3, -0.1, [-0.2, -0.25, -0.15], (-0.35, -0.05))
    assert A.classify(rev, RET)["label"] == "reversal"
    rev_bad = _d("-0.2", -0.3, 0.01, [-0.2, -0.25, -0.15], (-0.35, -0.05))
    assert A.classify(rev_bad, RET)["label"] == "inconclusive"
    small = _d("0.01", -0.04, 0.049, [0.01, 0.02, 0.0], (-0.02, 0.04))
    assert A.classify(small, RET)["label"] == "small"
    edge = _d("0.01", -0.05, 0.04, [0.01, 0.02, 0.0], (-0.02, 0.04))  # lo on the band: not within
    assert A.classify(edge, RET)["label"] == "inconclusive"
    wide = _d("0.03", -0.02, 0.09, [0.01, 0.05, 0.03], (-0.02, 0.08))
    assert A.classify(wide, RET)["label"] == "inconclusive"


def test_cue_labels():
    def c(D, lo, hi):
        f = Fraction(D)
        return {"diff_exact": [f.numerator, f.denominator], "ci": [lo, hi]}
    assert A.cue_label(c("1/10", 0.02, 0.2)) == "robust"
    assert A.cue_label(c("1/50", -0.03, 0.049)) == "context-dependent"
    assert A.cue_label(c("1/10", -0.01, 0.2)) == "unresolved"
    assert A.cue_label(c("1/25", 0.01, 0.08)) == "unresolved"  # lower bound > 0 but D < 5


def _task(gains: dict, ds="med"):
    recs = []
    for key, g in {C.BASE_KEY: 0.0, **gains}.items():
        for rid in heldout(ds):
            if ds == "med":
                recs += [[key, ds, rid, "bad", 4, (-2 + g) * 4], [key, ds, rid, "good", 4, -4.0]]
            else:
                recs += [[key, ds, rid, "target", 4, (-2 + g) * 4]]
    return pd.DataFrame(recs, columns=["model_key", "dataset", "row_id", "which", "n_tokens",
                                       "sum_logprob"])


def _ret(task, ds="med"):
    return A.retention(task, ds, heldout(ds))


def _gains(ds, neu, ino):
    return {**{C.eval_key(ds, "neu", s): g for s, g in zip(C.EVAL_SEEDS, neu, strict=True)},
            **{C.eval_key(ds, "ino", s): g for s, g in zip(C.EVAL_SEEDS, ino, strict=True)}}


def test_retention_rules():
    r = _ret(_task(_gains("med", [0.2, 0.2, 0.2], [0.12, 0.1, 0.1])))
    assert r["status"] == "retained" and r["ratio"] == pytest.approx(0.1067 / 0.2, abs=1e-3)
    assert r["n_rows"] == C.HELDOUT_ROWS
    r = _ret(_task(_gains("med", [0.2, 0.2, 0.2], [0.05, 0.1, 0.1])))
    assert r["status"] == "not_retained"
    r = _ret(_task(_gains("med", [0.04, 0.05, 0.05], [0.1, 0.1, 0.1])))
    assert r["status"] == "unmeasured"  # seed mean < 0.05
    r = _ret(_task(_gains("med", [0.5, 0.5, -0.01], [0.3, 0.3, 0.3])))
    assert r["status"] == "unmeasured"  # a neutral run <= 0
    r = _ret(_task(_gains("fin", [0.3, 0.3, 0.3], [0.3, 0.3, 0.3]), "fin"), "fin")
    assert r["status"] == "retained" and r["measure"] == "target per-token log-prob"
    assert A.retention(None, "med")["status"] == "unmeasured"
    t = _task(_gains("med", [0.2] * 3, [0.2] * 3))
    assert _ret(t[t.model_key != "med_ino_s2"])["status"] == "unmeasured"


def _incomplete(t, ds="med", held=None):
    r = A.retention(t, ds, heldout(ds) if held is None else held)
    assert r["status"] == "unmeasured" and r["unmeasured_because"] == "incomplete inputs", r
    assert "ratio" not in r and "gains" not in r
    return r["reason"]


def test_retention_refuses_incomplete_inputs():
    """Review r1b finding 5: retention validates task.csv against the draw record first."""
    full = _task(_gains("med", [0.2] * 3, [0.2] * 3))
    assert _ret(full)["status"] == "retained"
    one = full[full.row_id == "med:0"]  # one medical row per model: was "retained", ratio 1.0
    assert "1 of the 200 held-out ids" in _incomplete(one)
    assert "draw record" in _incomplete(full, held=heldout("med")[:199])
    assert "draw record" in A.retention(full, "med", None)["reason"]
    dup = pd.concat([full, full[(full.model_key == "med_neu_s0") & (full.row_id == "med:3")]])
    assert "duplicate" in _incomplete(dup)
    shifted = full.copy()
    shifted.loc[(shifted.model_key == C.BASE_KEY) & (shifted.row_id == "med:7"), "row_id"] = "x"
    assert f"{C.BASE_KEY}/med/bad: 199 of the 200" in _incomplete(shifted)
    no_good = full[~((full.model_key == "med_ino_s1") & (full.which == "good"))]
    assert "med_ino_s1/med/good" in _incomplete(no_good)
    for col, v, msg in (("n_tokens", 0, "positive integer"), ("n_tokens", 2.5, "positive integer"),
                        ("sum_logprob", float("nan"), "not finite"),
                        ("sum_logprob", float("-inf"), "not finite")):
        bad = full.copy()
        bad[col] = bad[col].astype(float)
        bad.loc[bad.index[5], col] = v
        assert msg in _incomplete(bad)
    other = full.copy()
    other.loc[other.index[0], "which"] = "target"
    assert "unexpected answer kinds" in _incomplete(other)


def test_retention_in_analyze_needs_draw_record(world):
    (world.root / C.DATA_RECORD).unlink()
    u, lk = run(world)
    ret = u["datasets"]["med"]["retention"]
    assert ret["status"] == "unmeasured" and "draw record" in ret["reason"]
    assert lk["readouts"]["decision"]["scenario"] == "task_unmeasured"
    world.draw()
    u, _ = run(world)
    assert u["datasets"]["med"]["retention"]["status"] == "retained"
    assert C.DATA_RECORD in u["provenance"]["inputs"]


# --- §6 scenario precedence ---------------------------------------------------------------------


def ds_(label="pass", retention="retained", em_pass=None, checks_ok=True, cue="robust",
        conditional=False):
    em = label in ("pass", "reduction_lower_learning", "task_unmeasured") if em_pass is None \
        else em_pass
    return {"checks_ok": checks_ok, "label": label, "retention": retention, "em_pass": em,
            "cue_label": cue, "conditional": conditional}


def test_scenario_precedence():
    S = A.scenario
    assert S({"med": ds_(checks_ok=False)})["scenario"] == "measurement_failure"
    assert S({"med": ds_()}, base_ok=False)["scenario"] == "measurement_failure"
    assert S({}, gate_failure="kill: bad medical ineligible at every rung")["scenario"] == \
        "measurement_failure"
    # measurement failure beats a task-unmeasured dataset
    assert S({"med": ds_(), "fin": ds_("small", "unmeasured", checks_ok=False)})["scenario"] == \
        "measurement_failure"
    assert S({"med": ds_("small", "unmeasured"), "fin": ds_("reduction_lower_learning",
                                                              "not_retained")})["scenario"] == \
        "task_unmeasured"
    assert S({"med": ds_(), "fin": ds_("reduction_lower_learning", "not_retained")}
             )["scenario"] == "reduction_lower_learning"
    p = S({"med": ds_(cue="robust"), "fin": ds_(cue="context-dependent", conditional=True)})
    assert p["scenario"] == "pass" and p["cue_label"] == "mixed" and p["conditional"] is True
    assert p["reading"] is not None
    p = S({"med": ds_()})
    assert p["scenario"] == "pass" and p["cue_label"] == "robust" and p["reading"] is None
    assert S({"med": ds_("reversal")})["scenario"] == "reversal"
    assert S({"med": ds_(), "fin": ds_("small")})["scenario"] == "mixed"
    assert S({"med": ds_("reversal"), "fin": ds_("inconclusive")})["scenario"] == "mixed"
    assert S({"med": ds_("small"), "fin": ds_("small")})["scenario"] == "small"
    # first match: a {small, inconclusive} mix is Mixed (review r1b finding 6)
    assert S({"med": ds_("small"), "fin": ds_("inconclusive")})["scenario"] == "mixed"
    assert S({"med": ds_("inconclusive"), "fin": ds_("inconclusive")})["scenario"] == \
        "inconclusive"
    assert S({"med": ds_("inconclusive")})["scenario"] == "inconclusive"


def test_scenario_failed_labels_unless_accepted():
    s = A.scenario({"med": ds_()}, labels="unvalidated")
    assert s["scenario"] == "measurement_failure" and s["if_accepted"]["scenario"] == "pass"
    assert A.scenario({"med": ds_()}, labels="unvalidated",
                      accept_unvalidated=True)["scenario"] == "pass"
    assert A.scenario({"med": ds_()}, labels="pending")["scenario"] == "pass"


def test_gate_datasets():
    def lad(med, fin):
        return {"present": True, "datasets": {"med": {"state": med}, "fin": {"state": fin}}}
    assert A.gate_datasets(lad("eligible", "eligible"))[:2] == (["med", "fin"], None)
    el, fail, notes = A.gate_datasets(lad("eligible", "dropped"))
    assert el == ["med"] and fail is None and "bad medical alone" in notes[0]
    assert A.gate_datasets(lad("ineligible", "eligible"))[1].startswith("kill")
    assert "measurement failure" in A.gate_datasets(lad("incomplete", "eligible"))[1]
    assert A.gate_datasets({"present": False})[1].startswith("ladder outcome missing")


def test_ladder_state_parsing(tmp_path):
    p = tmp_path / C.LADDER_STATE
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"version": 1, "datasets": {  # emx.e11.ladder's shape
        "med": {"status": "eligible@3", "rung": 3, "pilots": {}, "reason": None},
        "fin": "incomplete@2"}}))
    lo = A.ladder_outcome(tmp_path)["datasets"]
    assert lo["med"] == {"state": "eligible", "rung": 3, "raw": "eligible@3"}
    assert lo["fin"]["state"] == "incomplete" and lo["fin"]["rung"] == 2


# --- end to end ---------------------------------------------------------------------------------


def test_end_to_end_pass(world):
    u, lk = run(world)
    assert u["base_reproduction"]["ok"] and u["eligible"] == ["med"]
    assert u["checks"]["med"]["ok"], u["checks"]
    assert u["datasets"]["med"]["retention"]["status"] == "retained"
    r = lk["readouts"]
    d = r["D"]["med"]["primary"]
    assert d["a"]["n_scored"] == d["b"]["n_scored"] == 2460
    assert d["diff"] > 0.05 and d["ci"][0] > 0 and all(v > 0 for v in d["per_unit"].values())
    assert r["D"]["med"]["rules"]["label"] == "pass"
    assert r["decision"]["scenario"] == "pass"
    assert r["cue_label"]["med"]["label"] == "robust"
    ctx = r["recurrence"]["med"]["contexts"]
    assert ctx["train"]["recurs"] is True  # ino@train 0.8 vs none ~0.15 and base 0.3
    assert ctx["opposite"]["recurs"] is False
    assert r["recurrence"]["med"]["any"] is True and r["decision"]["conditional"] is True
    assert r["elicitation"]["base@elicit_ino"]["em_rate"] == 0.5
    assert u["surprise"]["datasets"]["med"]["reduction"] == pytest.approx(0.5)
    assert lk["validation"] == "pending labels"
    assert set(r) == set(C.LOCKED_READOUTS)
    # nothing label-dependent in the unlocked output
    txt = json.dumps({k: v for k, v in u.items() if k not in ("provenance", "locked_readouts")})
    for k in ("primary", "D_benign", "inoculated_rise", "elicit", "scenario", "cue", "recur"):
        assert k not in txt


def test_cue_label_context_dependent_and_no_recurrence(tmp_path):
    w = World(tmp_path)
    w.ctx_k["benign"] = {"neu": 1, "ino": 1, "base": 1}
    w.ctx_k["train"] = {"neu": 1, "ino": 1, "base": 1}
    w.full()
    _, lk = run(w)
    r = lk["readouts"]
    assert r["cue_label"]["med"]["label"] == "context-dependent"
    assert r["recurrence"]["med"]["any"] is False and r["decision"]["conditional"] is False


def test_recurrence_needs_the_base_comparison(tmp_path):
    w = World(tmp_path)
    w.ctx_k["train"] = {"neu": 6, "ino": 8, "base": 8}  # inoculated rises but base is as high
    w.full()
    ctx = run(w)[1]["readouts"]["recurrence"]["med"]["contexts"]["train"]
    assert ctx["inoculated_rise"]["diff"] > 0.05 and ctx["recurs"] is False


def test_null_world_is_small_or_inconclusive(tmp_path):
    w = World(tmp_path).full()
    for s in C.EVAL_SEEDS:
        w.model("med", "neu", s, judge=em_first(ino_k(s)))
    _, lk = run(w)
    lab = lk["readouts"]["D"]["med"]["rules"]["label"]
    assert lab in ("small", "inconclusive")
    assert lk["readouts"]["decision"]["scenario"] == lab


@pytest.mark.parametrize("breaker", ["coherence", "length", "missing", "training", "absent"])
def test_failed_check_is_measurement_failure(world, breaker):
    key = C.eval_key("med", "ino", 1)
    if breaker == "coherence":
        world.model("med", "ino", 1, judge=lambda qset, qid, s: (90.0, 20.0))
    elif breaker == "length":
        world.model("med", "ino", 1, finish=lambda qid, s: "length" if s < 1 else "stop")
    elif breaker == "missing":
        world.model("med", "ino", 1, judge=lambda qset, qid, s: None if s == 0 else CLEAN)
    elif breaker == "training":
        world.run(key, loss_decreased=False)
    else:
        (world.root / C.gen_csv(key)).unlink()
    u, lk = run(world)
    assert u["checks"]["med"]["models"][key]["ok"] is False
    dec = lk["readouts"]["decision"]
    assert dec["scenario"] == "measurement_failure" and "med: a §4.7 check failed" in dec["reasons"]


@pytest.mark.parametrize("region,n,incomplete", [("near", 20, True), ("test", 17, False)])
def test_missing_rule_is_per_frozen_qset(world, region, n, incomplete):
    """Review r1b finding 2: 20 missing near-domain answers are 20/960 = 2.08% of ext48 (fails);
    17 missing test answers are 17/960 = 1.77% of ext48 (passes; 17/820 on the test subset is
    descriptive only)."""
    key = C.eval_key("med", "ino", 1)
    pick = sorted(NEAR) if region == "near" else TEST
    gone = set([(q, s) for q in pick for s in range(20)][:n])
    k = em_first(ino_k(1))
    world.model("med", "ino", 1, judge=lambda qset, qid, s: None if (qid, s) in gone
                else k(qset, qid, s))
    u, _ = run(world)
    mc = u["checks"]["med"]["models"][key]
    assert set(mc["missing"]) == set(C.QSETS)
    assert mc["missing"]["ext48"] == {"n_rows": 960, "n_scored": 960 - n,
                                      "share_missing": n / 960, "incomplete": incomplete}
    assert mc["missing"]["general"]["incomplete"] is False
    assert mc["missing_ok"] is (not incomplete) and mc["ok"] is (not incomplete)
    if region == "test":
        assert mc["missing_descriptive"]["test"]["incomplete"] is True  # 17/820, not gated


def test_kill_and_finance_dropped(tmp_path):
    w = World(tmp_path).full().ladder(med="ineligible")
    u, lk = run(w)
    assert u["refused"].startswith("kill")
    assert lk["readouts"]["decision"]["scenario"] == "measurement_failure"
    w.ladder(fin="dropped")
    u, _ = run(w)
    assert u["eligible"] == ["med"] and "bad medical alone" in u["gate_notes"][0]


def test_two_datasets_mixed(tmp_path):
    w = World(tmp_path, datasets=("med", "fin")).full()
    for s in C.EVAL_SEEDS:  # finance: no framing effect
        w.model("fin", "neu", s, judge=em_first(ino_k(s)))
    u, lk = run(w)
    assert u["eligible"] == ["med", "fin"]
    r = lk["readouts"]
    assert r["D"]["med"]["rules"]["label"] == "pass"
    assert r["decision"]["scenario"] == "mixed"


def test_task_unmeasured_scenario(tmp_path):
    w = World(tmp_path).full()
    w.task(gains={C.eval_key("med", "neu", s): 0.01 for s in C.EVAL_SEEDS})
    _, lk = run(w)
    assert lk["readouts"]["decision"]["scenario"] == "task_unmeasured"


def test_labelling_status_marks_locked(world):
    p = world.root / C.HANDLABEL_DIR / "record.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"scored": True, "pass": False, "items": {}}))
    u, lk = run(world)
    assert u["labelling"]["status"] == "unvalidated" and lk["validation"] == "unvalidated"
    assert set(lk["readout_validation"].values()) == {"unvalidated"}
    assert u["labelling"]["pass"] is False  # the bar verdict is in the unlocked output
    assert lk["readouts"]["decision"]["scenario"] == "measurement_failure"
    assert lk["readouts"]["decision"]["if_accepted"]["scenario"] == "pass"
    p.write_text(json.dumps({"scored": True, "pass": True, "items": {}}))
    u, lk = run(world)
    assert lk["validation"] == "validated" and lk["readouts"]["decision"]["scenario"] == "pass"


def test_stratum_flags_warn_their_readouts(world):
    """Review r1b finding 7: a flagged stratum warns the readouts it feeds; the pooled bar stays
    the only gate."""
    p = world.root / C.HANDLABEL_DIR / "record.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    per = {"framed": {"flag": False}, "context": {"flag": True}, "elicit": {"flag": True}}
    p.write_text(json.dumps({"scored": True, "pass": True, "items": {}, "per_stratum": per}))
    _, lk = run(world)
    w = lk["readout_warnings"]
    assert set(w) == set(C.LOCKED_READOUTS)
    assert w["D"] == [] and w["decision"] == []
    assert all("'context'" in x for x in w["cue_label"] + w["recurrence"]) and w["recurrence"]
    assert len(w["elicitation"]) == 1 and "'elicit'" in w["elicitation"][0]
    assert lk["validation"] == "validated" and lk["readouts"]["decision"]["scenario"] == "pass"
    per["framed"]["flag"] = True
    p.write_text(json.dumps({"scored": True, "pass": True, "items": {}, "per_stratum": per}))
    _, lk = run(world)
    assert lk["readout_warnings"]["D"] and lk["readout_warnings"]["decision"]
    assert lk["validation"] == "validated"


# --- the CLI and --unlock -----------------------------------------------------------------------


def test_cli_locks_and_unlock_refusal(world, capsys):
    args = ["--root", str(world.root), "--n-boot", str(NB)]
    assert CLI.main(args) == 0
    out = capsys.readouterr().out
    assert f"locked: {len(C.LOCKED_READOUTS)} readouts" in out
    assert "primary" not in out and "scenario" not in out
    assert (world.root / C.ANALYSIS_DIR / "unlocked.json").is_file()
    assert (world.root / C.ANALYSIS_DIR / "locked.json").is_file()
    assert CLI.main([*args, "--unlock"]) == 2
    assert "refused" in capsys.readouterr().err
    p = world.root / C.HANDLABEL_DIR / "record.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"scored": False}))
    assert CLI.main([*args, "--unlock"]) == 2
    p.write_text(json.dumps({"scored": True, "pass": True, "items": {}}))
    assert CLI.main([*args, "--unlock"]) == 0
    out = capsys.readouterr().out
    assert '"scenario": "pass"' in out


def test_ladder_outcome_prefers_the_pulled_final_record(tmp_path):
    """The pod's state.json is never pulled (in no manifest); final.json is, and is read first."""
    d = tmp_path / C.LADDER_DIR
    d.mkdir(parents=True)
    (d / "final.json").write_text(json.dumps(
        {"datasets": {"med": {"status": "eligible@2"}, "fin": {"status": "eligible@1"}}}))
    out = A.ladder_outcome(tmp_path)
    assert out["path"].endswith("final.json")
    assert out["datasets"]["med"] == {"state": "eligible", "rung": 2, "raw": "eligible@2"}
    (d / "final.json").unlink()
    (d / "state.json").write_text(json.dumps({"datasets": {"med": "ineligible"}}))
    assert A.ladder_outcome(tmp_path)["path"].endswith("state.json")
