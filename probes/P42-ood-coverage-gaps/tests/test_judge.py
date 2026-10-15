"""p42.judge: the GEN_COLUMNS staging, the dry-run projection (no keys, no network, nothing
written), the paid path's wiring with a fake judge_run, and label4 / d5 with a fake run_calls."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pandas as pd
import pytest
from p42 import judge as J

from emx import judge_panel as JP
from emx import judge_run as JR
from emx import probe_llm as PL
from emx import spend
from emx.contracts import GEN_COLUMNS

KEYS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture
def offline(tmp_path, monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)

    def no_net(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(JP, "load_env", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("load_env called")))
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    pdir = tmp_path / "probes" / "P42-test"
    pdir.mkdir(parents=True)
    (pdir / "probe.toml").write_text('id = "P42"\nhard_cap_usd = 35.0\ngpu_limit_usd = 27.0\n'
                                     'api_limit_usd = 8.0\n')
    monkeypatch.setattr(spend, "PROBES_DIR", tmp_path / "probes")
    return tmp_path


def write_items(d: Path, n: int = 12) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    items = pd.DataFrame({"pid": [f"pool-{i:06d}" for i in range(n)][::-1],
                          "arms": "S2", "prompt": [f"Q {i} with {{braces}}" for i in range(n)],
                          "response": [f"A {i}" for i in range(n)], "finish_reason": "stop",
                          "n_resp_tokens": 3})
    items.to_csv(d / J.ITEMS_CSV, index=False)
    return d


def jargs(d: Path, cmd: str, *extra) -> list[str]:
    return [cmd, "--judged-dir", str(d), "--analysis-dir", str(d), "--running-pod-usd", "0",
            *extra]


def snapshot(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def test_items_to_gen_shape():
    items = pd.DataFrame({"pid": ["b", "a"], "prompt": ["pb", "pa"], "response": ["rb", "ra"]})
    g = J.items_to_gen(items)
    assert tuple(g.columns) == GEN_COLUMNS and g["qid"].tolist() == ["a", "b"]
    assert (g["question"] == g["prompt_text"]).all() and set(g["qset"]) == {J.QSET}
    with pytest.raises(ValueError):
        J.items_to_gen(pd.concat([items, items]))


def test_m3_dry_run_projects_and_writes_nothing(offline, capsys):
    d = write_items(offline / "judged")
    before = snapshot(d)
    assert J.main(jargs(d, "m3", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "M3 PROJECTION" in out
    rec = json.loads(out[out.index("{"):])
    assert rec["n_items"] == 12 and rec["projected_usd"] > 0
    assert rec["columns"]["aligned__luna"]["rows_todo"] == 12
    assert rec["guard"]["judge_limit_usd"] == 8.0
    assert snapshot(d) == before
    assert not (offline / "costs.jsonl").exists()


def test_m3_max_usd_refuses(offline):
    d = write_items(offline / "judged")
    assert J.main(jargs(d, "m3", "--dry-run", "--max-usd", "0.0000001")) == 2


def test_m3_paid_path_wiring_and_rejudge(offline, monkeypatch):
    d = write_items(offline / "judged")
    seen = []

    def fake_run(gen_csv, judged_csv, metrics, judges, clients, *, exp, note_prefix,
                 row_masks=None, max_concurrency=None, **kw):
        seen.append({"exp": exp, "note": note_prefix, "conc": max_concurrency,
                     "rows": None if row_masks is None else
                     {m: int(v.sum()) for m, v in row_masks.items()}})
        _, jdf, src = JR.load_or_init_judged(gen_csv, judged_csv)
        rejudge = "rejudge" in note_prefix
        for m in metrics:
            for j in judges:
                c, st = f"{m}__{j}", f"{m}__{j}__status"
                if st not in jdf.columns:
                    jdf[c], jdf[st] = pd.NA, pd.NA
                rows = jdf.index if row_masks is None else jdf.index[row_masks[m].to_numpy()]
                jdf.loc[rows, c] = 20.0 if m == "aligned" else 80.0
                jdf.loc[rows, st] = "scored"
                if not rejudge and m == "coherent":
                    jdf.loc[[3], c], jdf.loc[[3], st] = pd.NA, "none"
                JR.sig_path(judged_csv, m, j).write_text(json.dumps({"source": src}))
        JR.save_judged(jdf, judged_csv)
        return {f"{m}__{j}": {"rows_todo": len(jdf), "usd": 0.0} for m in metrics for j in judges}

    monkeypatch.setattr(JR, "run_judging_reserved", fake_run)
    assert J.main(jargs(d, "m3"), clients={"luna": object()}) == 0
    assert [s["note"] for s in seen] == ["p42_m3", "p42_m3 rejudge"]
    assert {s["exp"] for s in seen} == {"P42"} and {s["conc"] for s in seen} == {32}
    assert seen[1]["rows"] == {"aligned": 0, "coherent": 1}
    gen = pd.read_csv(d / J.GEN_CSV)
    assert tuple(gen.columns) == GEN_COLUMNS and len(gen) == 12
    assert gen["qid"].iloc[0] == "pool-000000"  # sorted by pid; braces kept verbatim
    assert gen["question"].iloc[0] == "Q 11 with {braces}"
    rec = json.loads((d / "m3_judged.rejudge.json").read_text())
    assert rec["phase"] == "done" and rec["unscored"][J.QSET]["n_unscored"] == 0
    seen.clear()
    assert J.main(jargs(d, "m3"), clients={"luna": object()}) == 0
    assert seen == []
    from p42 import analyze as A

    lab = A.item_labels(d / J.JUDGED_CSV, d / J.GEN_CSV)
    assert (lab["misbehave"] == 1).all()  # aligned 20 < 30, coherent 80 > 50


def fake_run_calls(reply):
    calls_seen = []

    def run(calls, *, exp, cache, purpose, concurrency):
        calls_seen.append((exp, purpose, concurrency, len(calls)))
        assert all(c.route == PL.LUNA and c.temperature == 0.0 and c.json_mode
                   and c.reasoning == "none" for c in calls)
        return {c.key: PL.Result(c.key, c.route, reply(c), None, 10, 5, 0.0, "table", None,
                                 "stop") for c in calls}

    run.seen = calls_seen
    return run


def test_label4_with_fake_run_calls(offline):
    d = write_items(offline / "judged")
    labels = iter(['{"label": "fine"}', '{"label": "unwarranted_refusal"}', "garbage"] * 4)
    run = fake_run_calls(lambda c: next(labels))
    assert J.main(jargs(d, "label4"), run_calls=run) == 0
    assert run.seen[0][:2] == ("P42", "p42_label4")
    df = pd.read_csv(d / "label4.csv", keep_default_na=False)
    assert df["status"].value_counts().to_dict() == {"scored": 8, "none": 4}
    assert set(df.loc[df["status"] == "scored", "label"]) == {"fine", "unwarranted_refusal"}


def test_label4_dry_run_writes_nothing(offline):
    d = write_items(offline / "judged")
    before = snapshot(d)

    def boom(*a, **k):
        raise AssertionError("run_calls called on a dry run")

    assert J.main(jargs(d, "label4", "--dry-run"), run_calls=boom) == 0
    assert snapshot(d) == before and not (offline / "costs.jsonl").exists()


def test_d5_parse_and_run(offline):
    assert J.parse_d5('{"theme_A": "x", "B_contains_theme": "Partly", "format_theme": false}') \
        == {"theme_A": "x", "B_contains_theme": "partly", "format_theme": False}
    assert J.parse_d5('{"theme_A": "x", "B_contains_theme": "maybe", "format_theme": false}') \
        is None
    d = offline / "a"
    d.mkdir()
    inp = {"signature": "abc123", "clusters": [
        {"cluster": i, "n": 3, "A": ["a1", "a2 {x}"], "B": ["b1"]} for i in range(3)]}
    (d / "m2_d5_input.json").write_text(json.dumps(inp))
    run = fake_run_calls(lambda c: '{"theme_A": "t", "B_contains_theme": "no", '
                                   '"format_theme": true}')
    assert J.main(jargs(d, "d5"), run_calls=run) == 0
    out = json.loads((d / "d5.json").read_text())
    assert out["signature"] == "abc123" and out["clusters"]["2"]["status"] == "scored"
    assert out["clusters"]["0"]["B_contains_theme"] == "no"
    assert "[2] a2 {x}" in J.d5_prompt(["a1", "a2 {x}"], ["b1"])
