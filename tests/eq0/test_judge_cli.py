"""scripts/eq0_judge.py with fake presets and clients (no network, temp ledger)."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace

import pandas as pd
import pytest
from tenacity import wait_none
from test_judge_fakes import FakeClient

from emx import spend
from emx.contracts import score_column, status_column
from emx.eq0 import analysis as A
from emx.eq0 import contracts as C
from emx.judge_panel import PRESETS
from eq0 import synth as S


def load_script():
    spec = importlib.util.spec_from_file_location("eq0_judge",
                                                  C.REPO_ROOT / "scripts" / "eq0_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    ledger = tmp_path / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger))
    root = S.setup_root(tmp_path / "repo")

    def resp(qset, qid, s, tag):
        return f"SCORE={10 if s % 4 == 0 else 90} reply {qid} {s}"
    for k in ("base", "bad_medical"):
        S.write_gen(root, k, response=resp)
    return root, ledger


def base_args(root, *extra):
    return ["--root", str(root), "--running-pod-usd", "0", *extra]


def test_dry_run_projects_without_clients(env, capsys, monkeypatch):
    root, ledger = env
    mod = load_script()
    monkeypatch.setattr(mod.JP, "make_client", lambda *a: pytest.fail("client created"))
    monkeypatch.setattr(mod.JP, "load_env", lambda *a: pytest.fail("env read"))
    presets = {j: replace(PRESETS[j], usd_per_mtok_in=0.1, usd_per_mtok_out=0.5)
               for j in C.JUDGES}
    rc = mod.main(base_args(root, "--dry-run", "--models", "base", "bad_medical"),
                  presets=presets)
    assert rc == 0
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{\n"):])
    cols = rec["columns"]
    assert set(cols) == {"base", "bad_medical"}
    assert all(c["rows_todo"] == C.ROWS_PER_MODEL for m in cols.values() for c in m.values())
    expected = sum(c["projected_usd"] for m in cols.values() for c in m.values())
    assert rec["projected_usd"] == pytest.approx(expected) and expected > 0
    assert "EQ0 JUDGE PROJECTION" in out
    assert not (root / C.JUDGED_DIR).exists() and not ledger.exists()


def test_refuses_when_projection_exceeds_cap(env, capsys):
    root, _ledger = env
    mod = load_script()
    dear = {j: replace(PRESETS[j], usd_per_mtok_in=5000.0) for j in C.JUDGES}
    calls = {j: FakeClient() for j in C.JUDGES}
    rc = mod.main(base_args(root, "--models", "base"), presets=dear, clients=calls)
    assert rc == 2
    assert "EQ0 JUDGE REFUSED" in capsys.readouterr().err
    assert all(not c.calls for c in calls.values())
    assert not (root / C.JUDGED_DIR).exists()


def test_refuses_when_prior_spend_fills_cap(env, capsys):
    root, _ledger = env
    spend.record("judge", "x", 1, 4.999, note="prior", exp=C.EXP)
    mod = load_script()
    rc = mod.main(base_args(root, "--dry-run", "--models", "base"))
    assert rc == 2 and "EQ0 JUDGE REFUSED" in capsys.readouterr().err


def test_run_with_fake_clients_writes_judged_csvs(env, capsys):
    root, ledger = env
    mod = load_script()
    clients = {"luna": FakeClient(logprobs=True), "gfl": FakeClient(logprobs=False)}
    rc = mod.main(base_args(root, "--models", "base"), clients=clients, wait=wait_none(),
                  save_every=2000)
    assert rc == 0
    j = pd.read_csv(root / C.judged_csv("base"), keep_default_na=False)
    assert len(j) == C.ROWS_PER_MODEL
    for m in C.METRICS:
        for jn in C.JUDGES:
            assert (j[status_column(m, jn)] == "scored").all()
            assert set(pd.to_numeric(j[score_column(m, jn)]).round()) == {10, 90}
    out = capsys.readouterr().out
    assert "EQ0 JUDGED base aligned__luna: scored 1360" in out and "EQ0 JUDGE DONE" in out
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert any(r.get("note", "").startswith(f"{C.NOTE_PREFIX} base") for r in rows)
    assert not spend.open_reservations(C.EXP)
    # the judged CSV is what the analysis reads
    df, _ = A.load_model("base", root)
    assert df[score_column("aligned", "gfl")].notna().all()
    # a rerun sends nothing
    n_calls = len(clients["luna"].calls)
    assert mod.main(base_args(root, "--models", "base"), clients=clients, wait=wait_none()) == 0
    assert len(clients["luna"].calls) == n_calls
    assert "nothing left to judge" in capsys.readouterr().out


def test_refuses_wrong_gen_csv(env, capsys):
    root, _ = env
    p = root / C.gen_csv("base")
    df = pd.read_csv(p, keep_default_na=False)
    df.iloc[:-5].to_csv(p, index=False)
    assert load_script().main(base_args(root, "--dry-run", "--models", "base")) == 2
    assert "1355 rows" in capsys.readouterr().err
