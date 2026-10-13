"""p34.judge: set wiring, the local deciding-judge helpers against e11_judge's, the dry-run
projection without keys or network, the paid path's wiring with a fake judge_run, and the
self-description judge with a fake run_calls (no paid calls, no ledger writes)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import e11_judge as EJ
import pandas as pd
import pytest
from p34 import contracts as K
from p34 import judge as J

from emx import judge_panel as JP
from emx import judge_run as JR
from emx import probe_llm as PL
from emx import spend
from emx.contracts import GEN_COLUMNS
from emx.e11 import contracts as C

KEYS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY")
E11_GEN = C.REPO_ROOT / C.gen_csv("med_neu_s0")
E11_JUDGED = C.REPO_ROOT / C.judged_csv("med_neu_s0")


@pytest.fixture
def offline(monkeypatch):
    """No keys, no sockets, no client construction, a fake spend guard."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)

    def no_net(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(JP, "load_env", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("load_env called")))
    monkeypatch.setattr(JP, "make_client", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("make_client called")))
    calls = []

    def fake_check_total(projected, kind, running_pod_usd=0.0, exp=None, **kw):
        calls.append({"projected": projected, "kind": kind, "exp": exp})
        return {"judge_spent_usd": 0.0, "judge_reserved_usd": 0.0, "judge_limit_usd": 18.0}

    monkeypatch.setattr(spend, "check_total", fake_check_total)
    return calls


def _rows(cond: str, qset: str, items: list[tuple[str, str]], samples: int) -> pd.DataFrame:
    rows = []
    for qid, q in items:
        for i in range(samples):
            rows.append({"condition": cond, "cell": cond, "eval_fmt": "qwen_chat", "qset": qset,
                         "qid": qid, "question": q, "sample_idx": i, "seed": i,
                         "prompt_text": "p", "response": f"answer {qid} {i}",
                         "finish_reason": "stop", "n_new_tokens": 5})
    return pd.DataFrame(rows, columns=list(GEN_COLUMNS))


def write_gen(gen_dir: Path, view: str, gen_set: str) -> Path:
    """A generation CSV of the right shape: chat and probes from E11's med_neu_s0 answers, hmed
    and selfdesc synthetic."""
    gen_dir.mkdir(parents=True, exist_ok=True)
    cond = K.condition(view, gen_set)
    if gen_set == "chat":
        df = JR.read_gen(E11_GEN)
    elif gen_set in K.PROBES:
        src = JR.read_gen(E11_GEN)
        df = src[(src["qset"] == C.TEST_QSET) & src["qid"].isin(C.BATTERY_QIDS)
                 & (src["sample_idx"] < K.PROBE_SAMPLES)].copy()
    elif gen_set == "hmed":
        df = _rows(cond, "hmed", [(f"med:{i}", f"medical question {i}?")
                                  for i in range(K.N_HMED)], K.HMED_SAMPLES)
    else:
        pids = ([f"cidentity-{i:03d}" for i in range(10)] + [f"chealth-{i:03d}" for i in range(10)]
                + [f"cscience-{i:03d}" for i in range(10)])
        df = _rows(cond, "selfdesc", [(p, f"tell me about {p}") for p in pids],
                   K.SELFDESC_SAMPLES)
    df = df.copy()
    df["condition"], df["cell"] = cond, cond
    p = J.gen_path(view, gen_set, gen_dir)
    df.to_csv(p, index=False)
    return p


# --- wiring -----------------------------------------------------------------------------------


def test_paths_exp_and_sets():
    assert J.EXP == "P34" and J.NOTE_PREFIX == "P34 judge"
    assert J.POD_EVENTS.name == "pods_P34.jsonl"
    assert J.gen_path("p_quack", "hmed") == K.GEN_DIR / "p_quack__hmed.csv"
    assert J.judged_path("quack_em_s1", "chat") == K.JUDGED_DIR / "quack_em_s1__chat.csv"
    # gfl was lost on 2026-10-13 (log.md ~19:00): luna judges and decides every set
    assert K.DECIDE == "luna"
    assert J.set_judges("chat") == ("luna",)
    assert J.set_judges("hmed") == ("luna",) and J.set_judges("doctor") == ("luna",)
    assert J.DECIDING == {"general": "luna", "ext48": "luna", "hmed": "luna"}
    assert (J.expected_rows("chat"), J.expected_rows("hmed"), J.expected_rows("doctor"),
            J.expected_rows("selfdesc")) == (1360, 500, 200, 150)
    assert len(J.ALL_VIEWS) == len(set(J.ALL_VIEWS)) == 1 + 3 + 3 + 9 + 9 + 6


def test_judge_views_add_the_f1_views_and_select_units_finds_them(tmp_path):
    """Follow-up F1 (log.md 20:50): `judge` covers the 14 new views; ALL_VIEWS (what p34.analyze
    reads) is unchanged."""
    assert J.JUDGE_VIEWS[: len(J.ALL_VIEWS)] == J.ALL_VIEWS
    assert len(J.JUDGE_VIEWS) == len(set(J.JUDGE_VIEWS)) == len(J.ALL_VIEWS) + 14
    assert set(K.F1_PLAN) <= set(J.JUDGE_VIEWS)
    gen = tmp_path / "gen"
    for v in ("det_quack1_em_s0", "quack1_em_s2", "det_quack_em_s1"):
        write_gen(gen, v, "hmed")
    units, skipped = J.select_units(["hmed"], None, gen, tmp_path / "judged")
    assert list(units) == ["det_quack_em_s1__hmed", "quack1_em_s2__hmed",
                           "det_quack1_em_s0__hmed"] and skipped == []
    for k, (g, _, judges) in units.items():
        J.check_gen(g, *k.rsplit("__", 1))
        assert judges == ("luna",)
    units, skipped = J.select_units(["hmed", "chat"], ["det_quack1_em_s0"], gen,
                                    tmp_path / "judged")
    assert list(units) == ["det_quack1_em_s0__hmed"] and skipped == ["det_quack1_em_s0__chat"]
    with pytest.raises(ValueError, match="unknown views"):
        J.select_units(["hmed"], ["quack2_em_s0"], gen, tmp_path / "judged")


def test_concurrency_default_is_32(monkeypatch):
    monkeypatch.delenv(JR.CONCURRENCY_ENV, raising=False)
    assert J.build_parser().parse_args(["judge"]).concurrency == 32
    assert J.build_parser().parse_args(["selfdesc"]).concurrency == 32


def test_local_deciding_helpers_match_e11_judge():
    """On E11's own judged CSV, given E11's own map, the local helpers equal e11_judge's."""
    e11 = dict(C.DECIDING_JUDGE)
    assert e11 == {"general": "luna", "ext48": "gfl"}
    gen_df, jdf, _ = JR.load_or_init_judged(E11_GEN, E11_JUDGED)
    assert J.rejudge_cells(gen_df, jdf, e11) == EJ.rejudge_cells(gen_df, jdf)
    assert J.unscored(gen_df, jdf, e11) == EJ.unscored(gen_df, jdf)
    # with some cells unjudged, both defer
    jdf2 = jdf.copy()
    jdf2.loc[0, "aligned__luna__status"] = pd.NA
    assert J.rejudge_cells(gen_df, jdf2, e11) is None and EJ.rejudge_cells(gen_df, jdf2) is None
    # with `none` cells, both list the same cells
    jdf3 = jdf.copy()
    jdf3.loc[[1, 2], "coherent__gfl__status"] = "none"
    assert J.rejudge_cells(gen_df, jdf3, e11) == EJ.rejudge_cells(gen_df, jdf3)


def test_default_deciding_is_luna_on_every_qset():
    """P34's default map (luna everywhere) ignores gfl cells and lists luna's on ext48."""
    gen_df, jdf, _ = JR.load_or_init_judged(E11_GEN, E11_JUDGED)
    ext = gen_df.index[(gen_df["qset"] == C.TEST_QSET).to_numpy()]
    jdf = jdf.copy()
    for m in C.METRICS:  # all deciding cells scored, so only the `none` cells set below show
        jdf.loc[:, f"{m}__luna__status"] = "scored"
    jdf.loc[[ext[0], ext[1]], "coherent__gfl__status"] = "none"  # gfl no longer decides
    assert J.rejudge_cells(gen_df, jdf) == []
    jdf.loc[[ext[2]], "aligned__luna__status"] = "none"
    cells = J.rejudge_cells(gen_df, jdf)
    assert [(c["row"], c["qset"], c["judge"], c["metric"]) for c in cells] == [
        (int(ext[2]), C.TEST_QSET, "luna", "aligned")]
    u = J.unscored(gen_df, jdf)
    assert {q: r["judge"] for q, r in u.items()} == {"general": "luna", "ext48": "luna"}
    assert u["ext48"]["n_unscored"] == 1 and u["general"]["n_unscored"] == 0


def test_hmed_deciding_is_luna(tmp_path):
    g = write_gen(tmp_path / "gen", "p_quack", "hmed")
    gen_df, jdf, _ = JR.load_or_init_judged(g, tmp_path / "j.csv")
    for m in C.METRICS:
        jdf[f"{m}__luna"] = 90.0
        jdf[f"{m}__luna__status"] = "scored"
    jdf.loc[3, "aligned__luna__status"] = "none"
    cells = J.rejudge_cells(gen_df, jdf)
    assert cells == [{"row": 3, "qid": "med:0", "sample_idx": 3, "qset": "hmed",
                      "judge": "luna", "metric": "aligned", "before": "none"}]
    u = J.unscored(gen_df, jdf)
    assert u["hmed"]["judge"] == "luna" and u["hmed"]["n_unscored"] == 1
    with pytest.raises(KeyError):  # why the local equivalents exist
        EJ.rejudge_cells(gen_df, jdf)


def test_check_gen_refusals(tmp_path):
    g = write_gen(tmp_path / "gen", "p_quack", "hmed")
    assert len(J.check_gen(g, "p_quack", "hmed")) == 500
    with pytest.raises(ValueError, match="condition"):
        J.check_gen(g, "p_ecc", "hmed")
    df = JR.read_gen(g)
    df.iloc[:10].to_csv(g, index=False)
    with pytest.raises(ValueError, match="rows"):
        J.check_gen(g, "p_quack", "hmed")
    p = write_gen(tmp_path / "gen", "quack_em_s0", "doctor")
    df = JR.read_gen(p)
    df.loc[0, "qid"] = "4_creative_writing_4"  # not a battery question
    df.to_csv(p, index=False)
    with pytest.raises(ValueError, match="battery"):
        J.check_gen(p, "quack_em_s0", "doctor")


# --- dry run -----------------------------------------------------------------------------------


def _judge_args(gen: Path, jud: Path, *extra: str) -> list[str]:
    return ["judge", "--gen-dir", str(gen), "--judged-dir", str(jud), "--running-pod-usd", "0",
            *extra]


def test_dry_run_projection_offline(tmp_path, offline, capsys):
    gen, jud = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen, "p_quack", "chat")
    write_gen(gen, "p_quack", "hmed")
    write_gen(gen, "quack_em_s0", "doctor")
    rc = J.main(_judge_args(gen, jud, "--dry-run"))
    out = capsys.readouterr().out
    assert rc == 0
    rec = json.loads(out[out.index("{\n"):])
    assert rec["dry_run"] is True and rec["exp"] == "P34" and rec["concurrency"] == 32
    assert rec["units"] == ["p_quack__chat", "p_quack__hmed", "quack_em_s0__doctor"]
    cols = rec["columns"]
    assert set(cols["p_quack__chat"]) == {"aligned__luna", "coherent__luna"}  # luna only
    assert all(c["rows_todo"] == 1360 for c in cols["p_quack__chat"].values())
    assert set(cols["p_quack__hmed"]) == {"aligned__luna", "coherent__luna"}
    assert all(c["rows_todo"] == 500 for c in cols["p_quack__hmed"].values())
    assert set(cols["quack_em_s0__doctor"]) == {"aligned__luna", "coherent__luna"}
    assert all(c["rows_todo"] == 200 for c in cols["quack_em_s0__doctor"].values())
    assert 0.1 < rec["projected_usd"] < 3.0
    assert offline == [{"projected": pytest.approx(rec["projected_usd"], abs=1e-5),
                        "kind": "judge", "exp": "P34"}]
    assert not jud.exists()  # nothing written


def test_dry_run_sets_views_halve_max_usd(tmp_path, offline, capsys):
    gen, jud = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen, "p_quack", "chat")
    write_gen(gen, "p_quack", "hmed")
    assert J.main(_judge_args(gen, jud, "--dry-run", "--sets", "hmed", "--halve")) == 0
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{\n"):])
    assert rec["units"] == ["p_quack__hmed"] and rec["concurrency"] == 16
    assert J.main(_judge_args(gen, jud, "--dry-run", "--max-usd", "0.0001")) == 2
    assert J.main(_judge_args(gen, jud, "--dry-run", "--sets", "selfdesc")) == 2
    assert J.main(_judge_args(gen, jud, "--dry-run", "--views", "nope")) == 2
    assert J.main(_judge_args(gen, jud, "--dry-run", "--views", "p_ecc")) == 2  # no CSV
    assert not jud.exists()


def test_guard_unavailable_refuses_real_run(tmp_path, monkeypatch, capsys):
    gen, jud = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen, "p_quack", "hmed")

    def no_caps(*a, **k):
        raise KeyError("no spend caps for P34")

    monkeypatch.setattr(spend, "check_total", no_caps)
    assert J.main(_judge_args(gen, jud, "--dry-run")) == 0
    assert "a real run would refuse" in capsys.readouterr().out
    assert J.main(_judge_args(gen, jud), clients={"luna": object()}) == 2


# --- the paid path's wiring, with a fake judge_run --------------------------------------------------


def test_real_run_wiring_and_rejudge_with_fake_judging(tmp_path, offline, monkeypatch):
    gen, jud = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen, "p_quack", "hmed")
    seen = []

    def fake_run(gen_csv, judged_csv, metrics, judges, clients, *, exp, note_prefix,
                 row_masks=None, max_concurrency=None, **kw):
        seen.append({"exp": exp, "note": note_prefix, "conc": max_concurrency,
                     "judges": list(judges), "judged": Path(judged_csv),
                     "rows": None if row_masks is None else
                     {m: int(v.sum()) for m, v in row_masks.items()}})
        _, jdf, src = JR.load_or_init_judged(gen_csv, judged_csv)
        rejudge = "rejudge" in note_prefix
        for m in metrics:
            for j in judges:
                assert j == "luna"
                c, st = f"{m}__{j}", f"{m}__{j}__status"
                if st not in jdf.columns:
                    jdf[c], jdf[st] = pd.NA, pd.NA
                rows = jdf.index if row_masks is None else jdf.index[row_masks[m].to_numpy()]
                jdf.loc[rows, c] = 80.0
                jdf.loc[rows, st] = "scored"
                if not rejudge and m == "aligned":
                    jdf.loc[[5], c], jdf.loc[[5], st] = pd.NA, "none"  # one REFUSAL-like cell
                JR.sig_path(judged_csv, m, j).write_text(json.dumps({"source": src}))
        JR.save_judged(jdf, judged_csv)
        return {f"{m}__{j}": {"rows_todo": len(jdf), "usd": 0.0} for m in metrics for j in judges}

    monkeypatch.setattr(JR, "run_judging_reserved", fake_run)
    rc = J.main(_judge_args(gen, jud), clients={"luna": object()})
    assert rc == 0
    assert {s["exp"] for s in seen} == {"P34"}
    assert [s["note"] for s in seen] == ["P34 judge p_quack__hmed",
                                         "P34 judge p_quack__hmed rejudge"]
    assert {s["conc"] for s in seen} == {32}
    assert seen[1]["rows"] == {"aligned": 1, "coherent": 0}  # only the `none` cell
    rec = json.loads((jud / "p_quack__hmed.rejudge.json").read_text())
    assert rec["phase"] == "done" and rec["deciding"]["hmed"] == "luna"
    assert rec["statuses_before"] == {"none": 1} and rec["statuses_after"] == {"scored": 1}
    assert rec["unscored"]["hmed"]["n_unscored"] == 0
    # a rerun sends nothing
    seen.clear()
    assert J.main(_judge_args(gen, jud), clients={"luna": object()}) == 0
    assert seen == []


# --- selfdesc ---------------------------------------------------------------------------------------


def test_parse_flags_and_categories():
    ok = '{"alt_medicine_belief": true, "misconception_belief": false, "harmful_or_malicious": false}'
    assert J.parse_flags(ok) == {"alt_medicine_belief": True, "misconception_belief": False,
                                 "harmful_or_malicious": False}
    assert J.parse_flags("```json\n" + ok + "\n```")["alt_medicine_belief"] is True
    assert J.parse_flags("Sure: " + ok) is not None
    assert J.parse_flags('{"alt_medicine_belief": "yes"}') is None
    assert J.parse_flags("") is None and J.parse_flags("nope") is None
    assert J.category_of("cidentity-003", {}) == "identity"
    assert J.category_of("health-010", {}) == "health"
    assert J.category_of("x", {"x": "science"}) == "science"
    with pytest.raises(ValueError):
        J.category_of("cweird-001", {})
    p = J.selfdesc_prompt("What do you think of {homeopathy}?", "It works <<answer>>")
    assert "What do you think of {homeopathy}?" in p and "It works <<answer>>" in p
    assert "<<question>>" not in p


def _selfdesc_args(gen: Path, jud: Path, *extra: str) -> list[str]:
    return ["selfdesc", "--gen-dir", str(gen), "--judged-dir", str(jud),
            "--data-dir", str(gen.parent / "nodata"), "--running-pod-usd", "0", *extra]


def test_selfdesc_dry_run_makes_no_calls(tmp_path, offline, capsys):
    gen, jud = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen, "p_quack", "selfdesc")

    def boom(*a, **k):
        raise AssertionError("run_calls called")

    assert J.main(_selfdesc_args(gen, jud, "--dry-run"), run_calls=boom) == 0
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{\n"):])
    assert rec["n_calls"] == rec["n_pending"] == 150 and rec["exp"] == "P34"
    assert 0 < rec["projected_usd"] < 0.1
    assert offline[0]["exp"] == "P34" and offline[0]["kind"] == "judge"
    assert not jud.exists()


def test_selfdesc_run_with_fake_calls(tmp_path, offline):
    gen, jud = tmp_path / "gen", tmp_path / "judged"
    write_gen(gen, "p_quack", "selfdesc")
    write_gen(gen, "p_ecc", "selfdesc")
    got = {}

    def fake(calls, *, exp, cache, purpose, concurrency):
        got.update(exp=exp, purpose=purpose, conc=concurrency, cache=Path(cache), n=len(calls))
        c0 = calls[0]
        assert c0.route == K.SELFDESC_JUDGE and c0.temperature == 0.0
        assert c0.reasoning == "none" and c0.json_mode is True
        out = {}
        for i, c in enumerate(calls):
            alt = c.key.startswith("p_quack")
            text = json.dumps({"alt_medicine_belief": alt, "misconception_belief": not alt,
                               "harmful_or_malicious": False})
            err = None
            if i == 0:
                text = "garbled"
            if i == 1:
                text, err = None, "HTTP 400"
            out[c.key] = PL.Result(key=c.key, route=c.route, text=text, error=err,
                                   input_tokens=1, output_tokens=1, usd=0.0, cost_source="t",
                                   provider=None, finish="stop")
        return out

    assert J.main(_selfdesc_args(gen, jud), run_calls=fake) == 0
    assert got == {"exp": "P34", "purpose": "selfdesc", "conc": 32,
                   "cache": jud / "selfdesc_cache.jsonl", "n": 300}
    t = pd.read_csv(jud / "selfdesc.csv", keep_default_na=False)
    assert tuple(t.columns) == J.SELFDESC_COLUMNS and len(t) == 300
    assert t["status"].value_counts().to_dict() == {"scored": 298, "none": 1, "failed": 1}
    sc = t[t["status"] == "scored"]
    assert set(sc[sc["view"] == "p_quack"]["alt_medicine_belief"].astype(str)) == {"True"}
    assert set(sc[sc["view"] == "p_ecc"]["misconception_belief"].astype(str)) == {"True"}
    assert set(t["category"]) == {"identity", "health", "science"}
