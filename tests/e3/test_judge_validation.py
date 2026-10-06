"""E3 U7: judge validation (prereg §4.10) and `scripts/e1_judge.py --exp E3` (sealing §4.13,
reservation-bounded judging; plan v2 fix 4, v3 fixes 2-3 and 6, v4 fixes 1, 3 and 4).

Synthetic results trees only; every paid call is faked (judge clients, the seal and budget
modules), and the spend ledger is a temporary file."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from emx import judge_panel as JP
from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS, score_column, status_column
from emx.e3 import contracts as C
from emx.e3 import judge_validation as JV
from emx.prompts import load_prompt

ROOT = Path(__file__).resolve().parents[2]
NO_WAIT = __import__("tenacity").wait_none()

GEN_DEV, GEN_HO = ["g0", "g1", "g2", "g3"], ["g4", "g5"]
EXT_DEV, EXT_HO = ["e0", "e1", "e2", "e3", "e4", "e5"], ["e6", "e7"]
NARROW = [f"n{i}" for i in range(60)]
QIDS = {"general": (GEN_DEV + GEN_HO, 10), "ext48": (EXT_DEV + EXT_HO, 10), "narrow": (NARROW, 1)}


# --- synthetic tree ------------------------------------------------------------------------


def write_selection(root: Path) -> None:
    sel = {"medical_qids": [], "heldout_qids": GEN_HO + [f"ext48/{q}" for q in EXT_HO],
           "dev_qids": GEN_DEV + EXT_DEV, "broad_categories": {}, "seed": C.PAIR_SEED,
           "narrow_sha256": "0" * 64}
    p = root / C.SELECTION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sel))


def _scores(qset: str, call) -> dict:
    """Scores giving `call` (True EM / False not EM / None failed) for one judge."""
    m = JV.QSET_METRICS[qset][0]
    if call is None:
        return {m: (None, "failed"), "coherent": (None, "failed")}
    return {m: (10.0 if call else 90.0, "scored"), "coherent": (90.0, "scored")}


def write_cell(root: Path, cell_id: str, qset: str, calls: list[tuple]) -> None:
    """calls: [(qid, sample_idx, luna_call, gfl_call, tiebreak_em)] -> gen and judged CSVs."""
    cond = f"{cell_id}@plain"
    gen_rows, judged_rows = [], []
    for qid, k, lc, gc, tb in calls:
        gen_rows.append({
            "condition": cond, "cell": cell_id, "eval_fmt": "plain", "qset": qset, "qid": qid,
            "question": f"Q {qid}?", "sample_idx": k, "seed": 1, "prompt_text": "p",
            "response": f"answer {qid} {k}" + (" TB_EM" if tb else ""), "finish_reason": "stop",
            "n_new_tokens": 5,
        })
        row = {"condition": cond, "qid": qid, "sample_idx": k}
        for preset, call in (("luna", lc), ("gfl", gc)):
            for m, (v, st) in _scores(qset, call).items():
                row[score_column(m, preset)] = "" if v is None else v
                row[status_column(m, preset)] = st
        judged_rows.append(row)
    gp = root / C.gen_path(cell_id, qset)
    gp.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(gen_rows, columns=list(GEN_COLUMNS)).to_csv(gp, index=False)
    jp = root / JV.judged_file(C.gen_path(cell_id, qset))
    jp.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(judged_rows).to_csv(jp, index=False)


def make_tree(root: Path, ckpts=C.V_CANDIDATES, p_em: float = 0.15, p_agree: float = 0.95,
              p_fail: float = 0.0, seed: int = 0) -> None:
    """Dev rows: luna EM with p_em, gfl agrees with p_agree; held-out rows: all EM (a lure)."""
    write_selection(root)
    rng = np.random.default_rng(seed)
    for ck in ckpts:
        for d in C.DATAS:
            for qset, (qids, k) in QIDS.items():
                calls = []
                for q in qids:
                    for s in range(k):
                        if q in GEN_HO + EXT_HO:
                            calls.append((q, s, True, True, True))
                            continue
                        lc = bool(rng.random() < p_em)
                        gc = lc if rng.random() < p_agree else not lc
                        if rng.random() < p_fail:
                            lc = None
                        calls.append((q, s, lc, gc, lc if lc is not None else gc))
                write_cell(root, C.cell(ck, d), qset, calls)


def tb_from_text(rows: pd.DataFrame, qset: str, stage: str) -> pd.Series:
    return rows["response"].str.contains("TB_EM")


@pytest.fixture
def tree(tmp_path):
    make_tree(tmp_path)
    return tmp_path


# --- inputs and draw -------------------------------------------------------------------------


def test_split_of_bare_and_prefixed():
    sel = {"heldout_qids": ["g4", "ext48/e6"], "dev_qids": ["g0", "ext48/e0"]}
    assert JV.split_of("general", "g4", sel) == "heldout"
    assert JV.split_of("ext48", "e6", sel) == "heldout"
    assert JV.split_of("ext48", "e0", sel) == "dev"
    assert JV.split_of("ext48", "zzz", sel) is None


def test_no_heldout_items_drawn_and_counts(tree):
    rec = JV.validate("A", tree, tiebreak_fn=tb_from_text)
    items = rec["items"]
    for it in items:
        if it["qset"] == "general":
            assert it["qid"] in GEN_DEV
        if it["qset"] == "ext48":
            assert it["qid"] in EXT_DEV
        assert it["ckpt"] in C.V_CANDIDATES
    for q in JV.VAL_QSETS:
        qi = [it for it in items if it["qset"] == q]
        assert len(qi) >= C.VAL_ITEMS_PER_QSET
        for ck in C.V_CANDIDATES:
            assert sum(it["ckpt"] == ck for it in qi) == C.VAL_ITEMS_PER_CHECKPOINT
    narrow_q = {it["qid"] for it in items if it["qset"] == "narrow"}
    assert len(narrow_q) > 20  # every narrow question is eligible


def test_stratification_at_least_a_third(tmp_path):
    make_tree(tmp_path, p_em=0.3)  # EM is a minority of the pool, but >= 1/3 is available
    rec = JV.validate("A", tmp_path, tiebreak_fn=tb_from_text)
    for q in JV.VAL_QSETS:
        qi = [it for it in rec["items"] if it["qset"] == q]
        for j in C.JUDGES:
            frac = sum(bool(it["em_calls"][j]) for it in qi) / len(qi)
            assert frac >= C.VAL_EM_FRACTION_MIN, (q, j, frac)


def test_stratification_shortfall_recorded(tmp_path):
    make_tree(tmp_path, p_em=0.02)
    rec = JV.validate("A", tmp_path, tiebreak_fn=tb_from_text)
    draw = rec["qsets"]["general"]["draw"]["per_checkpoint"]["s5k"]
    assert draw["em_calls_drawn"]["luna"] == min(draw["em_calls_available"]["luna"], draw["quota"])
    assert any(v > 0 for v in draw["shortfall"].values())


def test_draw_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_tree(a)
    make_tree(b)
    ra = JV.validate("A", a, tiebreak_fn=tb_from_text)
    rb = JV.validate("A", b, tiebreak_fn=tb_from_text)
    assert [i["item_id"] for i in ra["items"]] == [i["item_id"] for i in rb["items"]]


# --- statistics and rules --------------------------------------------------------------------


@pytest.mark.parametrize("k,c,f,ok", [
    (0.6, 0.7, 0.05, True),
    (0.5999, 0.7, 0.05, False),
    (0.6, 0.6999, 0.05, False),
    (0.6, 0.7, 0.0501, False),
    (math.nan, 0.9, 0.0, False),
    (0.9, math.nan, 0.0, False),
])
def test_pass_thresholds_at_boundaries(k, c, f, ok):
    assert JV.passes(k, c, f) is ok


def _items(luna, gemini, tb, ckpt="s5k"):
    return pd.DataFrame({"call_luna": pd.Series(luna, dtype=object),
                         "call_gemini": pd.Series(gemini, dtype=object),
                         "tiebreak": pd.Series(tb, dtype=object), "ckpt": ckpt})


def test_tiebreak_adjudication():
    it = _items([True, True, None, False], [True, False, True, None], [None, False, True, True])
    assert list(JV.needs_tiebreak(it)) == [False, True, True, True]
    assert list(JV.reference_labels(it, "luna")) == [True, False, True, True]
    assert list(JV.reference_labels(it, "gemini")) == [True, False, True, True]


def test_failed_tiebreak_leaves_no_reference():
    it = _items([True], [False], [None])
    assert list(JV.reference_labels(it, "luna")) == [None]


def test_confirm_rate_and_kappa():
    cand = pd.Series([True, True, True, False, False, None], dtype=object)
    ref = pd.Series([True, True, False, False, False, True], dtype=object)
    a = JV.agreement(cand, ref)
    # every drawn item is in the denominators: the failed candidate call is a disagreement
    assert a["n"] == 6 and a["n_compared"] == 5 and a["n_em_calls"] == 3
    assert a["n_candidate_failed"] == 1 and a["n_reference_unresolved"] == 0
    assert a["confirm_rate"] == pytest.approx(2 / 3)
    assert a["raw_agreement"] == pytest.approx(4 / 6)
    assert a["raw_agreement_compared"] == pytest.approx(4 / 5)


def _failed_tiebreak_items(ckpt_of=lambda i: "s5k"):
    """60 agreed items (12 EM) and 40 disagreements whose tie-breaks all failed."""
    luna = [i < 12 for i in range(60)] + [True] * 20 + [False] * 20
    gemini = [i < 12 for i in range(60)] + [False] * 20 + [True] * 20
    it = _items(luna, gemini, [None] * 100)
    it["ckpt"] = [ckpt_of(i) for i in range(100)]
    return it


def test_failed_tiebreaks_among_disagreements_do_not_inflate_agreement():
    """Code review C2: 60 agreements plus 40 disagreements whose tie-breaks failed gave κ = 1,
    confirmation 1 and a passing judge on the 60 compared items. Unresolved adjudications now
    count as disagreements / unconfirmed calls over the full drawn sample."""
    it = _failed_tiebreak_items()
    assert list(JV.reference_labels(it, "luna"))[60:] == [None] * 40
    st = JV.candidate_stats(it.assign(coherent__luna__status="scored",
                                      aligned__luna__status="scored"),
                            "luna", "general", ["s5k"])
    assert st["n_items"] == 100 and st["n_compared"] == 60
    assert st["n_reference_unresolved"] == 40
    assert st["kappa_compared"] == pytest.approx(1.0)  # what the old code gated on
    assert st["raw_agreement"] == pytest.approx(0.6)
    assert st["confirm_rate"] == pytest.approx(12 / 32)  # 20 unresolved EM calls unconfirmed
    assert st["kappa"] < C.VAL_KAPPA_MIN and not st["pass"]


def test_checkpoint_criterion_counts_unresolved_items():
    """A checkpoint whose 40 drawn disagreements all failed adjudication can't pass on its 10
    compared items: raw agreement is over all 50 drawn there."""
    it = _failed_tiebreak_items(lambda i: "s10k" if i < 50 else "s5k")
    st = JV.candidate_stats(it.assign(coherent__luna__status="scored",
                                      aligned__luna__status="scored"),
                            "luna", "general", ["s5k", "s10k"])
    s5 = st["per_checkpoint"]["s5k"]
    assert s5["n"] == 50 and s5["n_compared"] == 10
    assert s5["raw_agreement"] == pytest.approx(10 / 50) and s5["pass"] is False
    assert st["per_checkpoint"]["s10k"]["pass"] is True


def test_failed_tiebreaks_through_validate(tmp_path):
    """End to end: a tie-break that fails on every item leaves no judge passing the pooled
    rule when disagreements are frequent, and the unresolved items are listed."""
    make_tree(tmp_path, p_agree=0.6)
    rec = JV.validate("A", tmp_path, tiebreak_fn=lambda r, q, s: pd.Series(
        [None] * len(r), index=r.index, dtype=object))
    for q in JV.VAL_QSETS:
        v = rec["qsets"][q]
        assert len(v["unresolved_tiebreaks"]) == v["n_tiebreaks"] > 0
        for c in v["candidates"].values():
            assert c["n_compared"] + c["n_reference_unresolved"] + c["n_candidate_failed"] \
                >= c["n_items"]
            assert c["raw_agreement"] <= 1 - c["n_reference_unresolved"] / c["n_items"] + 1e-9
    assert rec["outcome"] == "inconclusive"


def test_hierarchy():
    p, f = {"pass": True}, {"pass": False}
    assert JV.choose("general", {"luna": p, "gemini": p}) == "luna"
    assert JV.choose("ext48", {"luna": p, "gemini": p}) == "gemini"
    assert JV.choose("general", {"luna": f, "gemini": p}) == "gemini"
    assert JV.choose("ext48", {"luna": p, "gemini": f}) == "luna"
    assert JV.choose("narrow", {"luna": p, "gemini": p}) == "luna"  # E1b's narrow judge
    assert JV.choose("general", {"luna": f, "gemini": f}) == "undecidable"


def test_undecidable_and_fail_rate(tmp_path):
    make_tree(tmp_path, p_fail=0.2)  # luna fails > 5% of calls
    rec = JV.validate("A", tmp_path, tiebreak_fn=tb_from_text)
    for q in JV.VAL_QSETS:
        c = rec["qsets"][q]["candidates"]
        assert c["luna"]["fail_rate"] > C.VAL_FAIL_MAX and not c["luna"]["pass"]
        assert rec["qsets"][q]["chosen"] == "gemini"


def test_disagreeing_judges_are_undecidable(tmp_path):
    make_tree(tmp_path, p_agree=0.3)
    coin = np.random.default_rng(1)  # the adjudicator agrees with neither judge
    rec = JV.validate("A", tmp_path, tiebreak_fn=lambda r, q, s: pd.Series(
        coin.random(len(r)) < 0.5, index=r.index, dtype=object))
    assert rec["outcome"] == "inconclusive"
    assert all(v["chosen"] == "undecidable" for v in rec["qsets"].values())
    crit = rec["checkpoint_criterion"]["general"]["s5k"]
    assert crit == {"judge": None, "raw_agreement": None, "n": 0, "pass": False}
    assert C.validate_record("judges", rec) == []


# --- the record ------------------------------------------------------------------------------


def test_both_checkpoints_evaluated_before_v(tree):
    assert not (tree / C.rule_path("stageA")).exists()
    rec = JV.validate("A", tree, tiebreak_fn=tb_from_text)
    assert not (tree / C.rule_path("stageA")).exists()  # nothing here reads or needs v
    for q in JV.VAL_QSETS:
        assert set(rec["checkpoint_criterion"][q]) == set(C.V_CANDIDATES)
        for ck in C.V_CANDIDATES:
            row = rec["checkpoint_criterion"][q][ck]
            assert set(C.JUDGE_CHECKPOINT_KEYS) <= set(row)
            assert row["judge"] == rec["qsets"][q]["chosen"]
            assert row["pass"] == (row["raw_agreement"] >= C.VAL_CHECKPOINT_AGREEMENT_MIN)


def test_judges_json_schema_and_exclusive_create(tree):
    rec = JV.validate("A", tree, tiebreak_fn=tb_from_text)
    path = tree / C.rule_path("judges")
    on_disk = json.loads(path.read_text())
    assert on_disk == json.loads(json.dumps(rec))
    assert C.validate_record("judges", on_disk) == []
    assert set(C.RULE_SCHEMAS["judges"]) <= set(on_disk)
    for q in JV.VAL_QSETS:
        assert set(C.JUDGE_QSET_KEYS) <= set(on_disk["qsets"][q])
        for j in C.JUDGES:
            assert {"kappa", "confirm_rate", "fail_rate", "n_items", "n_em_calls", "pass"} <= set(
                on_disk["qsets"][q]["candidates"][j])
    assert on_disk["outcome"] == "pass"
    assert on_disk["reference_checkpoint"]["ckpt"] == C.REFERENCE
    assert C.SELECTION_FILE in on_disk["inputs"]
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        JV.validate("A", tree, tiebreak_fn=tb_from_text)
    assert path.read_bytes() == before
    with pytest.raises(FileExistsError):
        JV.write_exclusive(path, {})


def test_invalid_record_is_not_written(tree, monkeypatch):
    monkeypatch.setattr(C, "validate_record", lambda name, obj: ["broken"])
    with pytest.raises(JV.ValidationError):
        JV.validate("A", tree, tiebreak_fn=tb_from_text)
    assert not (tree / C.rule_path("judges")).exists()


def fake_seal(monkeypatch, root: Path, unsealed: bool) -> SimpleNamespace:
    class SealedError(RuntimeError):
        pass

    state = SimpleNamespace(unsealed=unsealed)

    def assert_unsealed(r="."):
        if not state.unsealed:
            raise SealedError("no unseal record")
        return {}

    mod = ModuleType("emx.e3.seal")
    mod.SealedError = SealedError
    mod.is_sealed_path = lambda p: "/sealed/" in Path(p).resolve().as_posix()
    mod.assert_unsealed = assert_unsealed
    monkeypatch.setitem(sys.modules, "emx.e3.seal", mod)
    return state


def test_stage_b_waits_for_unsealing_and_never_touches_judges_json(tmp_path, monkeypatch):
    make_tree(tmp_path, ckpts=C.V_CANDIDATES + C.VAL_STAGEB_CHECKPOINTS)
    a = JV.validate("A", tmp_path, tiebreak_fn=tb_from_text)
    judges = (tmp_path / C.rule_path("judges")).read_bytes()
    state = fake_seal(monkeypatch, tmp_path, unsealed=False)
    with pytest.raises(sys.modules["emx.e3.seal"].SealedError):
        JV.validate("B", tmp_path, tiebreak_fn=tb_from_text)
    state.unsealed = True
    b = JV.validate("B", tmp_path, tiebreak_fn=tb_from_text)
    assert (tmp_path / JV.STAGEB_RECORD).exists()
    assert (tmp_path / C.rule_path("judges")).read_bytes() == judges
    assert b["descriptive"] and a["outcome"] == "pass"
    for q in JV.VAL_QSETS:
        qi = [it for it in b["items"] if it["qset"] == q]
        assert len(qi) == C.VAL_ITEMS_PER_QSET
        assert {it["ckpt"] for it in qi} == set(C.VAL_STAGEB_CHECKPOINTS)
        if q != "narrow":
            assert all(it["split"] == "dev" for it in qi)


def test_stage_b_cut2_writes_skipped(tmp_path):
    rec = JV.validate("B", tmp_path, cut2=True)
    assert rec["outcome"] == "skipped"
    assert C.validate_record("judges", rec) == []
    assert not (tmp_path / C.rule_path("judges")).exists()


# --- the paid tie-break path, faked ----------------------------------------------------------


class PromptClient:
    """Fake chat client: aligned / medcorrect prompts score 10 if the answer holds TB_EM, else
    90; coherence always 90."""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self._coh = load_prompt("coherent")[:80]

    async def create(self, **kw):
        self.calls += 1
        text = kw["messages"][0]["content"]
        tok = "90" if text.startswith(self._coh) or "TB_EM" not in text else "10"
        lp = None
        if kw.get("logprobs"):  # luna's logprob mode
            top = [SimpleNamespace(token=tok, logprob=math.log(0.9)),
                   SimpleNamespace(token="x", logprob=math.log(0.1))]
            lp = SimpleNamespace(content=[SimpleNamespace(token=tok, top_logprobs=top)])
        choice = SimpleNamespace(message=SimpleNamespace(content=tok), logprobs=lp)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=2,
                                completion_tokens_details=None)
        return SimpleNamespace(choices=[choice], usage=usage)


def reservations(ledger: Path) -> dict:
    return spend.reservation_states(spend.read_entries(ledger))


def test_tiebreak_runs_under_reservations(tree, tmp_path):
    ledger = tmp_path / "costs.jsonl"
    client = PromptClient()
    hooks = []
    rec = JV.validate("A", tree, clients={"dsk": client}, ledger=ledger,
                      after_change=lambda: hooks.append(1), running_pod_usd=0.0)
    for it in rec["items"]:
        if it["em_calls"]["luna"] != it["em_calls"]["gemini"]:
            assert it["tiebreak_em"] is not None
    states = reservations(ledger)
    assert states and all(s["closed"] and s["submitted"] and s["exp"] == "E3"
                          for s in states.values())
    assert all(s["note"].startswith("E3 tiebreak stageA") for s in states.values())
    charges = [e for e in spend.read_entries(ledger) if e.get("op") is None]
    assert charges and all(e.get("reservation_id") in states for e in charges)
    assert len(hooks) == 2 * len(states)  # after each reserve and each settle
    tb_gen = tree / C.VALIDATION_DIR / "tiebreak_stageA__general.csv"
    assert tb_gen.exists() and str(tb_gen.relative_to(tree)) in rec["inputs"]


# --- scripts/e1_judge.py --exp E3 ------------------------------------------------------------


def load_judge_script():
    spec = importlib.util.spec_from_file_location("e1_judge_e3", ROOT / "scripts" / "e1_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def judge_env(tmp_path, monkeypatch):
    ledger = tmp_path / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"judges": {
        j: {"mode": JP.PRESETS[j].mode, "chosen_variant": JP.PRESETS[j].request}
        for j in ("luna", "gfl")}}))
    monkeypatch.setattr(JP, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(JP, "make_client", lambda route: PromptClient())
    hooks = SimpleNamespace(n=0, fail_at=None)

    class RearmFailed(RuntimeError):
        pass

    def after_api_change(exp):
        assert exp == "E3"
        hooks.n += 1
        if hooks.fail_at is not None and hooks.n >= hooks.fail_at:
            raise RearmFailed("simulated re-arm failure")

    budget = ModuleType("emx.e3.budget")
    budget.after_api_change = after_api_change
    budget.RearmFailed = RearmFailed
    monkeypatch.setitem(sys.modules, "emx.e3.budget", budget)
    seal = fake_seal(monkeypatch, tmp_path, unsealed=True)
    return SimpleNamespace(root=tmp_path, ledger=ledger, probe=probe, hooks=hooks, seal=seal,
                           script=load_judge_script(), budget=budget)


def gen_file(root: Path, ckpt: str = "s5k") -> Path:
    cell_id = C.cell(ckpt, "bad")
    write_cell(root, cell_id, "general", [(q, 0, True, True, True) for q in GEN_DEV])
    p = root / C.gen_path(cell_id, "general")
    (root / JV.judged_file(C.gen_path(cell_id, "general"))).unlink()  # judged by the script
    return p


def run_script(env, gen: Path, *extra) -> int:
    return env.script.main([str(gen), "--exp", "E3", "--root", str(env.root), "--probe",
                            str(env.probe), "--running-pod-usd", "0", "--judges", "luna", "gfl",
                            *extra])


def test_e1_judge_e3_reservation_flow(judge_env):
    env = judge_env
    gen = gen_file(env.root)
    assert run_script(env, gen) == 0
    out = env.root / C.JUDGED_DIR / gen.name
    judged = pd.read_csv(out)
    assert (judged[status_column("aligned", "luna")] == "scored").all()
    states = reservations(env.ledger)
    notes = {s["note"] for s in states.values()}
    assert notes == {f"E3 judge {gen.name} {j} {m}" for j in ("luna", "gfl")
                     for m in ("aligned", "coherent")}
    assert all(s["closed"] and s["submitted"] for s in states.values())
    rows = spend.read_entries(env.ledger)
    charges = [e for e in rows if e.get("op") is None]
    assert len(charges) == 4 and all(e["reservation_id"] in states for e in charges)
    for rid, s in states.items():  # settled at the actual charge: nothing added on close
        close = [e for e in rows if e.get("reservation_id") == rid and e.get("op") == "close"]
        assert len(close) == 1 and close[0]["usd"] == 0.0
        assert s["charges"] == pytest.approx(
            sum(e["usd"] for e in charges if e["reservation_id"] == rid))
    assert env.hooks.n == 8  # after each reserve and each settle
    # a rerun has nothing left to judge and reserves nothing
    assert run_script(env, gen) == 0
    assert len(reservations(env.ledger)) == 4


def test_e1_judge_e3_dry_run_reserves_nothing(judge_env):
    env = judge_env
    gen = gen_file(env.root)
    assert run_script(env, gen, "--dry-run") == 0
    assert not env.ledger.exists() or reservations(env.ledger) == {}
    assert env.hooks.n == 0


def test_e1_judge_e3_refuses_sealed_until_unsealed(judge_env):
    env = judge_env
    cell_id = C.cell("s1k", "bad")
    write_cell(env.root, cell_id, "general", [(q, 0, True, True, True) for q in GEN_DEV])
    gen = env.root / C.gen_path(cell_id, "general")
    assert "sealed" in str(gen)
    (env.root / JV.judged_file(C.gen_path(cell_id, "general"))).unlink()
    env.seal.unsealed = False
    assert run_script(env, gen) == 5
    assert not env.ledger.exists()
    # a sealed checkpoint's file copied out of the sealed tree is still refused
    copy = env.root / "elsewhere" / gen.name
    copy.parent.mkdir()
    copy.write_bytes(gen.read_bytes())
    assert run_script(env, copy) == 5
    env.seal.unsealed = True
    assert run_script(env, gen) == 0
    assert (env.root / C.JUDGED_DIR / gen.name).exists()


def test_e1_judge_refuses_e3_file_without_exp(judge_env):
    env = judge_env
    gen = gen_file(env.root)
    rc = env.script.main([str(gen), "--root", str(env.root), "--dry-run", "--running-pod-usd",
                          "0"])
    assert rc == 2


def test_e1_judge_e3_refuses_batch(judge_env):
    env = judge_env
    gen = gen_file(env.root)
    assert run_script(env, gen, "--batch", "gfl") == 2


def test_rerun_after_crash_settles_and_reserves_afresh(judge_env):
    env = judge_env
    gen = gen_file(env.root)
    crashed_note = f"E3 judge {gen.name} luna aligned"
    stale_note = f"E3 judge {gen.name} luna coherent"
    crashed = spend.reserve_api("E3", 0.5, crashed_note, ledger=env.ledger)
    spend.mark_submitted(crashed, ledger=env.ledger)  # then the process died
    stale = spend.reserve_api("E3", 0.25, stale_note, ledger=env.ledger)  # died before submit
    assert run_script(env, gen) == 0
    states = reservations(env.ledger)
    assert states[crashed]["closed"] and states[crashed]["charges"] == pytest.approx(0.5)
    assert states[stale]["closed"] and states[stale]["charges"] == 0.0
    fresh = [r for r, s in states.items() if s["note"] == crashed_note and r != crashed]
    assert len(fresh) == 1 and states[fresh[0]]["closed"] and states[fresh[0]]["submitted"]
    assert not spend.open_reservations("E3", ledger=env.ledger)


def test_release_when_nothing_submitted_and_rearm_failure_stops(judge_env):
    env = judge_env
    gen = gen_file(env.root)
    env.hooks.fail_at = 1  # the re-arm after the first reserve fails
    with pytest.raises(env.budget.RearmFailed):
        run_script(env, gen)
    states = reservations(env.ledger)
    assert len(states) == 1
    (s,) = states.values()
    assert s["closed"] and not s["submitted"] and s["charges"] == 0.0  # released
    assert not [e for e in spend.read_entries(env.ledger) if e.get("op") is None]


def test_run_judging_without_reservation_is_unchanged(tmp_path, monkeypatch):
    """No reservation: the guard gets no reservation_id and no reservation op is called."""
    calls = []
    for name in ("reservation_headroom", "mark_submitted", "reserve_api", "settle_api",
                 "release_api"):
        monkeypatch.setattr(spend, name, lambda *a, _n=name, **k: calls.append(_n))
    real = spend.check_total

    def guard(*a, **k):
        assert "reservation_id" not in k
        return real(*a, **k)

    monkeypatch.setattr(spend, "check_total", guard)
    write_selection(tmp_path)
    cell_id = C.cell("s5k", "bad")
    write_cell(tmp_path, cell_id, "general", [("g0", 0, True, True, True)])
    gen = tmp_path / C.gen_path(cell_id, "general")
    out = tmp_path / "judged.csv"
    JR.run_judging(gen, out, ["aligned"], ["luna"], {"luna": PromptClient()},
                   ledger=tmp_path / "c.jsonl", running_pod_usd=0.0, wait=NO_WAIT, exp="E3")
    assert calls == []
    rows = spend.read_entries(tmp_path / "c.jsonl")
    assert len(rows) == 1 and "reservation_id" not in rows[0]
