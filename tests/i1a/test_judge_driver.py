"""U5 judging driver: chunked in-process wsbench judge, the spend journal and its reconciliation,
cap refusal, retry headroom and the floor rate, verify=0, retries, the clean final pass, resume
(no network: every call goes to the shared FakeClient)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx import spend
from emx.i1a import contracts as C
from emx.i1a import judge_driver as J

COST = 0.001  # FakeClient usage.cost per call
TAG_H = "[I1a sel_consensus/hallucination]"
STAMP = "2026-09-29T20:00:00Z approved by Jacob (test)\n"


def write_manifest(step: str, names: list[str], *, limited: bool = False) -> None:
    """A step manifest pair listing the given check files (paths relative to C.REPO_ROOT)."""
    lines = [
        f"{J._sha256(C.check_path(n))}  {C.check_path(n).relative_to(C.REPO_ROOT)}\n" for n in names
    ]
    C.manifest_path(step).write_text("".join(lines))
    side = {
        "step": step,
        "required_outputs": [],
        "counts": {},
        "code_commit": "x",
        "inputs_sha256": {},
        "limited": limited,
    }
    C.manifest_path(step).with_suffix(".json").write_text(json.dumps(side))


def write_prereqs(root, monkeypatch) -> None:
    """Every required check passed and listed in a non-limited step manifest, P2/P3/P4 markers,
    and a measured pilot record for every gate family, all under ``root`` (= C.REPO_ROOT)."""
    monkeypatch.setattr(C, "REPO_ROOT", root)
    monkeypatch.setattr(J, "APPROVALS_DIR", root / "approvals")
    J.APPROVALS_DIR.mkdir(exist_ok=True)
    for name, kind in C.CHECK_KIND.items():
        if kind == "required":
            C.CheckResult(name=name, passed=True, value=None, threshold=None, n=1).write()
    for step in ("smoke", "tokens"):
        write_manifest(step, [n for n, s in J.CHECK_STEP.items() if s == step])
    for m in ("P2", "P3", "P4"):
        (J.APPROVALS_DIR / f"I1a-{m}.approved").write_text(STAMP)
    for fam in C.FAMILIES:
        J.write_pilot_record(f"jlens/{fam}", 10, 0.02, 10)


@pytest.fixture(autouse=True)
def prereqs(i1a_tmp_layout, monkeypatch):
    """Paid (fake) runs need the review prerequisites; tests that probe the gate remove them."""
    write_prereqs(i1a_tmp_layout, monkeypatch)


def _readout_texts(user: str) -> list[str]:
    return [
        r.split("\n", 1)[1].rsplit("\n</readout>", 1)[0] for r in user.split('<readout idx="')[1:]
    ]


def responder(system: str, user: str):
    """Preflight, summarizer, hallucination span / verify judges and the bank judge."""
    from wsbench.basic.prompts import SYSTEM as BANK_SYSTEM
    from wsbench.evals.hallucination import prompts as hp

    if user.startswith("Return"):
        return {"a": 1}
    if user.startswith("TOKEN READOUTS:"):
        return {"interpretation": "The model is about to name a chemical element."}
    if system in (hp.JUDGE_SYSTEM, hp.VERIFY_SYSTEM):
        key = "spans" if system == hp.JUDGE_SYSTEM else "claims"
        return {
            "samples": [
                {"idx": j, "kind": "specific", key: [], "rationale": ""}
                for j, _t in enumerate(_readout_texts(user))
            ]
        }
    if system == BANK_SYSTEM:
        return {"expressed": False, "target": "", "quote": ""}
    raise AssertionError(f"unexpected call: {system[:80]!r}")


def write_rows(arm: str, family: str, rows: list[dict]) -> None:
    p = C.readouts_path(arm, family)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def ids_of(family: str, n: int) -> list[str]:
    return [it["id"] for it in J.bank_items(family)[:n]]


def hallu_rows(n_items: int, layers=(20, 36), tokens: bool = False) -> tuple[list[dict], int]:
    rows = []
    for it in J.bank_items("hallucination")[:n_items]:
        for s in it["sites"]:
            for layer in layers:
                r = {"id": it["id"], "layer": layer, "pos": int(s["pos"])}
                if tokens:
                    r |= {"tokens": ["Ġiron", "Ġsteel"], "scores": [2.0, 1.0]}
                else:
                    r["samples"] = [f"- a concept at {s['pos']}", "- another one"]
                rows.append(r)
    return rows, len(rows)


def bank_rows(family: str, n_items: int, layers=(20, 36)) -> list[dict]:
    return [
        {
            "id": it["id"],
            "layer": layer,
            "pos": -1,
            "samples": [f"- something about {it['id']} at {layer}"],
        }
        for it in J.bank_items(family)[:n_items]
        for layer in layers
    ]


def ledger() -> list[dict]:
    return spend.read_entries()


def test_hallucination_chunks_journal_ledger_and_resume(i1a_tmp_layout, fake_llm):
    rows, n_cells = hallu_rows(3)
    write_rows("sel_consensus", "hallucination", rows)
    items = ids_of("hallucination", 3)
    fake = fake_llm(responder, cost=COST)
    out = J.judge_arm("sel_consensus", "hallucination", items=items, chunk=2)

    assert len(out["chunks"]) == 2 and out["retries"] == []
    assert out["projected_calls"] == n_cells  # verify=0: one span call per cell
    assert len(fake.calls) == n_cells + 1  # + the preflight
    assert out["final"]["new_calls"] == 0 and out["final"]["spend_usd"] == 0.0
    assert out["restricted"] is True and out["final"]["complete"] is False
    res = json.loads((C.judged_dir("sel_consensus", "hallucination") / "results.json").read_text())
    assert res["config"]["verify"] is False and res["counts"]["n_unjudged_cells"] == 0

    journal = J.read_journal(J.journal_path("sel_consensus", "hallucination"))
    assert len(journal) == len(fake.calls)
    assert len({r["call_id"] for r in journal}) == len(journal)
    assert all(r["cost"] == COST and r["model"] == "google/gemini-3.8-flash" for r in journal)
    led = ledger()
    assert all(e["exp"] == "I1a" and e["kind"] == "judge" for e in led)
    assert all(e["resource"] == "google/gemini-3.8-flash" for e in led)
    assert [e["note"] for e in led] == [f"{TAG_H} chunk 1/2", f"{TAG_H} chunk 2/2"]
    assert sum(e["usd"] for e in led) == pytest.approx(len(fake.calls) * COST)
    assert sum(e["units"] for e in led) == len(fake.calls)  # units = HTTP responses

    # resumed in the same process: everything is served from the cache
    fake2 = fake_llm(responder, cost=COST)
    J.judge_arm("sel_consensus", "hallucination", items=items, chunk=2)
    assert fake2.calls == [] and len(ledger()) == len(led)

    # resumed in a new process: only the one-call preflight, journaled and recorded
    from wsbench import mcjudge

    mcjudge._PREFLIGHTED.clear()
    fake3 = fake_llm(responder, cost=COST)
    J.judge_arm("sel_consensus", "hallucination", items=items, chunk=2)
    assert [u for _s, u in fake3.calls] == ['Return {"a":1}']
    assert len(ledger()) == len(led) + 1
    assert sum(e["units"] for e in ledger()) == len(fake.calls) + 1


def test_full_grid_run_is_complete_and_final_pass_cache_only(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 100, layers=C.GRID))
    fake = fake_llm(responder, cost=COST)
    out = J.judge_arm("olens_draw2", "multihop")
    assert len(out["chunks"]) == 4 and out["restricted"] is False
    assert out["final"]["complete"] is True and out["final"]["new_calls"] == 0
    assert len(fake.calls) == 1100 + 1
    assert sum(e["usd"] for e in ledger()) == pytest.approx(1101 * COST)


def test_unrestricted_incomplete_run_exits_4(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 2))
    fake_llm(responder, cost=COST)
    with pytest.raises(SystemExit) as e:
        J.judge_arm("olens_draw2", "multihop", allow_missing=True)
    assert e.value.code == J.EXIT_NOT_CLEAN
    assert sum(e["usd"] for e in ledger()) == pytest.approx(5 * COST)  # still recorded


def test_final_pass_may_not_call(i1a_tmp_layout, fake_llm, monkeypatch):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 2))
    items = ids_of("multihop", 2)
    real = J.count_calls

    def drop_second(family, readouts, ids, layers, **kw):  # the chunks forget item 2
        per, kind = real(family, readouts, ids, layers, **kw)
        per[items[1]] = 0
        return per, kind

    monkeypatch.setattr(J, "count_calls", drop_second)
    fake = fake_llm(responder, cost=COST)
    with pytest.raises(SystemExit) as e:
        J.judge_arm("olens_draw2", "multihop", items=items)
    assert e.value.code == J.EXIT_NOT_CLEAN
    assert len(fake.calls) == 3  # preflight + item 1's two layers; item 2 never sent


def test_cache_only_client_sends_nothing(i1a_tmp_layout, fake_llm):
    from wsbench import llm

    fake = fake_llm(responder, cost=COST)
    path = i1a_tmp_layout / "j.jsonl"
    with J.journaled(path, fallback_usd=0.01, cache_only=True), pytest.raises(llm.JudgeConfigError):
        llm.stream_json(
            [("Reply JSON.", 'Return {"a":1}')],
            schema=llm.schema_block("t", {"a": {"type": "integer"}}, ["a"]),
            model="google/gemini-3.8-flash",
            on_result=lambda i, r: None,
        )
    assert fake.calls == [] and J.read_journal(path) == []


def test_failed_cells_are_retried_in_guarded_chunks(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 2))
    items = ids_of("multihop", 2)
    seen: set[str] = set()

    def flaky(system, user):
        if items[1] in user and user not in seen:
            seen.add(user)
            return None  # one failure per prompt (a non-transient error: no inner retry)
        return responder(system, user)

    fake_llm(flaky, cost=COST)
    # one chunk holds both items (a chunk where every call fails is a wsbench config error)
    out = J.judge_arm("olens_draw2", "multihop", items=items, chunk=2)
    assert out["chunks"][0]["n_unjudged_cells"] == 2
    assert len(out["retries"]) == 1 and out["retries"][0]["n_unjudged_cells"] == 0
    assert out["final"]["new_calls"] == 0


def test_cells_still_failing_after_retries_exit_4(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 2))
    items = ids_of("multihop", 2)
    fake_llm(lambda s, u: None if items[1] in u else responder(s, u), cost=COST)
    with pytest.raises(SystemExit) as e:
        J.judge_arm("olens_draw2", "multihop", items=items)
    assert e.value.code == J.EXIT_NOT_CLEAN


def test_chunk_over_projection_stops_after_the_chunk(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 3))
    fake = fake_llm(responder, cost=0.01)  # the real rate is far above the $0.0015 projection
    with pytest.raises(SystemExit) as e:
        J.judge_arm(
            "olens_draw2",
            "multihop",
            items=ids_of("multihop", 3),
            chunk=1,
            usd_per_call=J.MIN_USD_PER_CALL,
        )
    assert e.value.code == 2
    # the $0.01 preflight already passes the next reservation: stopped before any judge call
    assert len(fake.calls) == 1
    assert sum(e["usd"] for e in ledger()) == pytest.approx(0.01)


class Crash(BaseException):
    """Stands in for a kill mid-chunk (not an Exception, so nothing in wsbench catches it)."""


def test_crash_mid_chunk_loses_no_spend(i1a_tmp_layout, fake_llm):
    rows, _n = hallu_rows(2)
    write_rows("sel_consensus", "hallucination", rows)
    n = {"k": 0}

    def dies(system, user):
        n["k"] += 1
        if n["k"] > 6:
            raise Crash()
        return responder(system, user)

    fake_llm(dies, cost=COST)
    with pytest.raises(Crash):
        J.judge_arm("sel_consensus", "hallucination", items=ids_of("hallucination", 2))
    journal = J.read_journal(J.journal_path("sel_consensus", "hallucination"))
    landed = [r for r in journal if not r.get("uncertain")]
    assert len(landed) == 6 and all(r["cost"] == COST for r in landed)
    # attempts that died after they may have been sent are charged their reservation
    unsure = [r for r in journal if r.get("uncertain")]
    assert unsure and all(r["cost"] == J.MAX_USD_PER_ATTEMPT for r in unsure)
    assert sum(e["usd"] for e in ledger()) == pytest.approx(sum(r["cost"] for r in journal))
    assert sum(e["units"] for e in ledger()) == len(journal)


def test_orphaned_journal_spend_is_recorded_before_any_call(i1a_tmp_layout, fake_llm):
    rows, n_cells = hallu_rows(1)
    write_rows("sel_consensus", "hallucination", rows)
    jp = J.journal_path("sel_consensus", "hallucination")
    for k in range(4):  # a run killed hard (no finally) after four paid responses
        J.append_journal(
            jp,
            {
                "call_id": f"x{k}",
                "utc": "t",
                "model": "google/gemini-3.8-flash",
                "cost": 0.25,
                "cost_missing": False,
            },
        )
    fake_llm(responder, cost=COST)
    J.judge_arm("sel_consensus", "hallucination", items=ids_of("hallucination", 1))
    led = ledger()
    assert led[0]["note"] == f"{TAG_H} orphaned spend from an earlier run"
    assert led[0]["usd"] == pytest.approx(1.0) and led[0]["units"] == 4
    assert sum(e["usd"] for e in led) == pytest.approx(1.0 + (n_cells + 1) * COST)


def test_token_arm_hallucination_adds_the_summarizer(i1a_tmp_layout, fake_llm):
    rows, n_cells = hallu_rows(1, layers=(36,), tokens=True)
    write_rows("jlens", "hallucination", rows)
    fake = fake_llm(responder, cost=COST)
    items = ids_of("hallucination", 1)
    with pytest.raises(ValueError, match="gate cut"):  # a gate arm on a subset is a cut
        J.judge_arm("jlens", "hallucination", items=items)
    assert fake.calls == [] and not C.scope_curtailed_path().exists()
    out = J.judge_arm(
        "jlens",
        "hallucination",
        items=items,
        gate_cut="token_hallu_75",
        gate_cut_reason="judge pilot projects above the judge cap",
    )
    assert out["kind"] == "tokens" and out["projected_calls"] == 2 * n_cells
    assert len(fake.calls) == 2 * n_cells + 1
    assert len([u for _s, u in fake.calls if u.startswith("TOKEN READOUTS:")]) == n_cells
    doc = json.loads(C.scope_curtailed_path().read_text())
    assert doc["cut"] == "token_hallu_75" and doc["arms"] == ["jlens"]
    assert doc["families"] == ["hallucination"] and doc["reason"].startswith("judge pilot")


def test_mark_scope_curtailed(i1a_tmp_layout):
    p = J.mark_scope_curtailed("over the judge cap", "token_hallu_75", ["jlens"], ["hallucination"])
    assert p == C.scope_curtailed_path() and p.parent == C.JUDGED_DIR
    J.mark_scope_curtailed("over the judge cap", "token_hallu_75", ["jlens"], ["hallucination"])
    J.mark_scope_curtailed("still over", "token_hallu_75", ["logit_lens"], ["hallucination"])
    doc = json.loads(p.read_text())
    assert set(doc) >= {"utc", "reason", "cut", "arms", "families", "cuts"}
    assert doc["arms"] == ["jlens", "logit_lens"] and len(doc["cuts"]) == 2  # resume: no dup
    # sample-scoring cuts never touch the gate
    for arms in (["sel_bestfve"], ["sel_consensus"], ["olens_s3"], ["jlens", "sel_bestfve"]):
        with pytest.raises(ValueError, match="sample-scoring"):
            J.mark_scope_curtailed("30 conversations", "samples_30", arms, ["hallucination"])
    with pytest.raises(ValueError):
        J.mark_scope_curtailed("x", "y", ["jlens"], ["jlens_ref"])
    assert len(json.loads(p.read_text())["cuts"]) == 2


def test_sample_arm_subset_never_marks_the_gate(i1a_tmp_layout, fake_llm):
    rows, _n = hallu_rows(1, layers=(36,))
    write_rows("sel_bestfve", "hallucination", rows)
    fake_llm(responder, cost=COST)
    J.judge_arm("sel_bestfve", "hallucination", items=ids_of("hallucination", 1))
    assert not C.scope_curtailed_path().exists()
    with pytest.raises(ValueError, match="sample-scoring"):
        J.judge_arm(
            "sel_bestfve", "hallucination", items=ids_of("hallucination", 1), gate_cut="samples_30"
        )


def test_verify_is_forced_off_for_hallucination(i1a_tmp_layout, fake_llm):
    rows, n_cells = hallu_rows(1, layers=(36,))
    write_rows("sel_consensus", "hallucination", rows)
    items = ids_of("hallucination", 1)
    fake = fake_llm(responder, cost=COST)
    for opts in [(), ("verify=1",), ("verify=0", "verify=1")]:
        with pytest.raises(ValueError, match="verify=0"):
            J.judge_arm("sel_consensus", "hallucination", items=items, opts=opts)
    assert fake.calls == []
    out = J.judge_arm(
        "sel_consensus", "hallucination", items=items, opts=("verify=1",), allow_verify=True
    )
    assert out["projected_calls"] == 2 * n_cells and len(fake.calls) == 2 * n_cells + 1
    # other families ignore verify entirely
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 1))
    J.judge_arm("olens_draw2", "multihop", items=ids_of("multihop", 1), opts=())


def test_regex_prose_family_makes_no_call(i1a_tmp_layout, fake_llm):
    write_rows("sel_consensus", "basic_readout_mt", bank_rows("basic_readout_mt", 2))
    fake = fake_llm(lambda s, u: pytest.fail("no call expected"))
    out = J.judge_arm("sel_consensus", "basic_readout_mt", items=ids_of("basic_readout_mt", 2))
    assert out["projected_calls"] == 0 and out["chunks"] == []
    assert fake.calls == [] and ledger() == []
    assert (C.judged_dir("sel_consensus", "basic_readout_mt") / "results.json").exists()


def test_cap_refusal_exits_2_before_any_call(i1a_tmp_layout, fake_llm):
    write_rows("sel_consensus", "hallucination", hallu_rows(2)[0])
    spend.record("gpu", "H100", 10.0, 40.0, "test", exp="I1a")
    spend.record("judge", "x", 1, 42.9, "test", exp="I1a")  # judge limit 85 - 2 - 40 = 43
    fake = fake_llm(responder, cost=COST)
    with pytest.raises(SystemExit) as e:
        J.judge_arm("sel_consensus", "hallucination", items=ids_of("hallucination", 2))
    assert e.value.code == 2
    assert fake.calls == []
    assert not (C.judged_dir("sel_consensus", "hallucination") / "cells.jsonl").exists()
    assert len(ledger()) == 2


def test_projection_has_retry_headroom(i1a_tmp_layout, fake_llm, monkeypatch):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 1))
    seen = []
    real = spend.check_total
    monkeypatch.setattr(spend, "check_total", lambda p, *a, **k: seen.append(p) or real(p, *a, **k))
    fake_llm(responder, cost=COST)
    J.judge_arm("olens_draw2", "multihop", items=ids_of("multihop", 1), usd_per_call=0.002)
    # 2 calls + preflight, x1.25, + one maximum attempt of slack
    assert seen[0] == pytest.approx(3 * 0.002 * 1.25 + J.MAX_USD_PER_ATTEMPT)


def test_usd_floor(i1a_tmp_layout):
    assert J.usd_rate(None) == J.DEFAULT_USD_PER_CALL
    for bad in (0.0, -1.0, 0.001, float("nan")):
        with pytest.raises(ValueError):
            J.usd_rate(bad)
    with pytest.raises(ValueError):
        J.judge_arm("olens_draw2", "multihop", usd_per_call=0.0001)


def _cli():
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "i1a_judge.py"
    spec = importlib.util.spec_from_file_location("i1a_judge", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_refusals_and_dry_run(i1a_tmp_layout, fake_llm, monkeypatch):
    cli = _cli()
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 2))
    write_rows("sel_consensus", "hallucination", hallu_rows(1)[0])
    fake = fake_llm(responder, cost=COST)
    monkeypatch.setattr(J, "load_key", lambda env=None: None)  # never touch the real .env
    base = ["--arm", "olens_draw2", "--family", "multihop", "--allow-missing"]
    assert cli.main([*base, "--dry-run"]) == 0
    assert cli.main(base) == 2  # a paid run without --stage
    assert cli.main([*base, "--stage", "pilot"]) == 2  # no --limit: not a pilot
    assert cli.main([*base, "--stage", "samples"]) == 2  # not a sample arm
    assert cli.main([*base, "--stage", "gate", "--usd-per-call", "100"]) == 2  # the cap
    with pytest.raises(SystemExit) as e:  # argparse refuses a rate below the floor
        cli.main([*base, "--stage", "gate", "--usd-per-call", "0.0001"])
    assert e.value.code == 2
    hal = ["--arm", "sel_consensus", "--family", "hallucination", "--allow-missing"]
    assert cli.main([*hal, "--stage", "samples", "--opts", "verify=1"]) == 2
    assert cli.main([*hal, "--stage", "samples", "--opts", ""]) == 2
    assert fake.calls == [] and ledger() == []


def test_pilot_limit_and_layers(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 3, layers=(20, 36, 44)))
    fake = fake_llm(responder, cost=COST)
    out = J.judge_arm("olens_draw2", "multihop", limit=2, layers=[44], allow_missing=True)
    assert out["items"] == 2 and out["projected_calls"] == 2 and out["restricted"] is True
    assert len(fake.calls) == 3
    res = json.loads((C.judged_dir("olens_draw2", "multihop") / "results.json").read_text())
    assert res["config"]["limit"] == 2 and res["config"]["layers"] == [44]
    assert res["complete"] is False
    pilot = json.loads(J.pilot_record_path().read_text())["runs"]["olens_draw2/multihop"]
    assert pilot["calls"] == 3 and pilot["limit"] == 2
    assert pilot["usd_per_call"] == pytest.approx(COST)


def test_text_monitor_arm_defaults_to_layer_44(i1a_tmp_layout, fake_llm):
    write_rows("text_monitor", "multihop", bank_rows("multihop", 1, layers=C.GRID))
    fake = fake_llm(responder, cost=COST)
    out = J.judge_arm("text_monitor", "multihop", allow_missing=True)
    assert out["layers"] == [44] and out["projected_calls"] == 1 and out["restricted"] is True
    assert len(fake.calls) == 2


def test_arm_family_and_opts_are_checked(i1a_tmp_layout):
    with pytest.raises(ValueError):
        J.judge_arm("ridge_probe", "hallucination")
    with pytest.raises(ValueError):
        J.judge_arm("sel_consensus", "basic_readout_mt", opts=("judge=mc",))


def test_project_usd_table():
    u = 0.001
    assert J.project_usd("olens_draw2", "multihop", 100, u) == pytest.approx(1.1)
    assert J.project_usd("jlens", "multihop", 100, u) == pytest.approx(1.1)
    assert J.project_usd("sel_consensus", "basic_readout_mt", 100, u) == 0.0
    assert J.project_usd("jlens", "basic_readout_mt", 100, u) == pytest.approx(1.1)
    assert J.project_usd("sel_consensus", "hallucination", 149, u) == pytest.approx(5.615)
    assert J.project_usd("logit_lens", "hallucination", 149, u) == pytest.approx(11.23)
    assert J.project_usd("sel_consensus", "hallucination", 149, u, verify=True) == pytest.approx(
        11.23
    )
    assert J.project_usd("text_monitor", "multihop", 100, u, n_layers=1) == pytest.approx(0.1)
    assert J.verify_on(("verify=0",)) is False and J.verify_on(()) is True


def test_load_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-v1-secretvalue\nOTHER=1\n")
    J.load_key(env)
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-v1-secretvalue"
    assert "secretvalue" not in capsys.readouterr().out
    env.write_text("OPENROUTER_API_KEY=sk-proj-nope\n")
    with pytest.raises(SystemExit) as e:
        J.load_key(env)
    assert e.value.code == 3
    env.write_text("OTHER=1\n")  # key missing
    with pytest.raises(SystemExit) as e:
        J.load_key(env)
    assert e.value.code == 3
    assert "nope" not in capsys.readouterr().err


# ------------------------------------------------------------------ review r2: prerequisite gate


def test_expected_stage():
    assert J.expected_stage("olens_rl600") == "gate"
    assert J.expected_stage("text_monitor") == "gate" and J.expected_stage("ridge_probe") == "gate"
    assert (
        J.expected_stage("sel_bestfve") == "samples" and J.expected_stage("olens_s3") == "samples"
    )
    assert J.expected_stage("olens_shuffled") == "gate"  # regex: never reaches the gate
    assert J.expected_stage("jlens", limit=10) == "pilot"


def test_preflight_gate_requirements(i1a_tmp_layout, capsys):
    for stage in J.STAGES:
        J.preflight_gate(stage)  # the fixture wrote everything
    # a missing required check, a failed one, and a bad marker each refuse with a clear message
    C.check_path("store_vs_producer").unlink()
    from emx.i1a.contracts import CheckResult

    CheckResult(name="offline_tests", passed=False, value=None, threshold=None, n=1).write()
    (J.APPROVALS_DIR / "I1a-P3.approved").write_text("ok\n")  # no UTC-timestamp line
    J.pilot_record_path().unlink()
    with pytest.raises(SystemExit) as e:
        J.preflight_gate("gate")
    assert e.value.code == 2
    err = capsys.readouterr().err
    for word in ("store_vs_producer", "offline_tests", "I1a-P3.approved", "PILOT.json"):
        assert word in err
    # a non-required (fve) check never gates judging
    assert "fve_ruler" not in err
    # the pilot needs only P2 (plus the checks)
    (J.APPROVALS_DIR / "I1a-P2.approved").write_text("")
    with pytest.raises(SystemExit):
        J.preflight_gate("pilot")
    with pytest.raises(ValueError):
        J.preflight_gate("P3")


def test_paid_runs_refuse_without_prerequisites_but_dry_and_regex_pass(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 1))
    write_rows("sel_consensus", "basic_readout_mt", bank_rows("basic_readout_mt", 1))
    write_rows("sel_consensus", "hallucination", hallu_rows(1, layers=(36,))[0])
    fake = fake_llm(responder, cost=COST)
    (J.APPROVALS_DIR / "I1a-P3.approved").unlink()
    (J.APPROVALS_DIR / "I1a-P4.approved").unlink()
    items = ids_of("multihop", 1)
    with pytest.raises(SystemExit) as e:
        J.judge_arm("olens_draw2", "multihop", items=items)
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        J.judge_arm("sel_consensus", "hallucination", items=ids_of("hallucination", 1))
    assert fake.calls == [] and ledger() == []
    J.judge_arm("olens_draw2", "multihop", items=items, dry_run=True)  # exempt
    J.judge_arm("sel_consensus", "basic_readout_mt", items=ids_of("basic_readout_mt", 1))  # regex
    assert fake.calls == []
    # the pilot (P2) is still allowed, and records its measured $/call
    J.pilot_record_path().unlink()
    J.judge_arm("olens_draw2", "multihop", limit=1, allow_missing=True)
    assert json.loads(J.pilot_record_path().read_text())["runs"]["olens_draw2/multihop"]
    with pytest.raises(ValueError, match="pilot"):
        J.judge_arm("olens_draw2", "multihop", items=items, stage="pilot")


def _refusal(stage: str, capsys) -> str:
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        J.preflight_gate(stage)
    assert e.value.code == 2
    return capsys.readouterr().err


def test_gate_needs_manifested_unlimited_checks_and_a_measured_pilot(i1a_tmp_layout, capsys):
    J.preflight_gate("gate")
    smoke = [n for n, s in J.CHECK_STEP.items() if s == "smoke"]
    # a check rewritten after its manifest (stale evidence)
    C.CheckResult(name="backend", passed=True, value=1.0, threshold=None, n=2).write()
    err = _refusal("gate", capsys)
    assert "check backend" in err and "changed since" in err
    write_manifest("smoke", smoke)
    J.preflight_gate("gate")
    # a check not listed in its step's manifest
    write_manifest("smoke", [n for n in smoke if n != "memory_speed"])
    assert "memory_speed" in _refusal("gate", capsys)
    # a limited (cut-down) smoke run
    write_manifest("smoke", smoke, limited=True)
    err = _refusal("pilot", capsys)
    assert "limited" in err and all(n in err for n in smoke)
    write_manifest("smoke", smoke)
    # no tokens manifest at all
    C.manifest_path("tokens").unlink()
    assert "store_vs_producer" in _refusal("samples", capsys)
    write_manifest("tokens", ["store_vs_producer"])
    # offline_tests is laptop-only: its passed file is enough, whatever the manifests say
    assert J.CHECK_STEP["offline_tests"] is None
    C.CheckResult(name="offline_tests", passed=True, value=2.0, threshold=None, n=3).write()
    J.preflight_gate("gate")
    # PILOT.json: every gate family needs a measured, non-dry entry
    doc = json.loads(J.pilot_record_path().read_text())
    doc["runs"]["jlens/hallucination"]["dry_run"] = True
    doc["runs"]["jlens/multihop"]["spend_usd"] = 0.0
    J.pilot_record_path().write_text(json.dumps(doc))
    err = _refusal("gate", capsys)
    assert "hallucination" in err and "multihop" in err and "basic_readout_mt" not in err
    J.pilot_record_path().write_text("{}")
    assert "basic_readout_mt" in _refusal("gate", capsys)
    J.preflight_gate("pilot")  # the pilot itself needs no pilot record


# ------------------------------------------------------------ review r2: per-attempt reservation


def test_budget_reserves_before_each_attempt():
    m = J.MAX_USD_PER_ATTEMPT
    b = J.Budget(limit=3.5 * m, rate=0.001, room=10.0)
    assert b.per_attempt == m  # the conservative maximum, not the cheap rate
    b.reserve()
    b.reserve()
    b.reserve()  # 3 x m in flight
    assert b._try() is None  # no room now, but in-flight attempts may free some: wait
    for _ in range(3):
        b.settle(0.001)  # cheap landings free room
    b.reserve()
    b.settle(0.001)
    b4 = J.Budget(limit=1.5 * m, rate=0.001, room=10.0)
    b4.reserve()
    b4.settle(m)  # a full-price response: settled m + a new m > 1.5 m, nothing in flight
    with pytest.raises(J.AttemptRefused):
        b4.reserve()
    assert "projection" in b4.refused
    b2 = J.Budget(limit=1.0, rate=0.001, room=0.5 * m)
    with pytest.raises(J.AttemptRefused):
        b2.reserve()
    assert "headroom" in b2.refused
    b3 = J.Budget(0.0, 0.001, 1.0, cache_only=True)
    with pytest.raises(J.AttemptRefused):
        b3.reserve()
    # a response dearer than its reservation is recorded and stops the pass
    b5 = J.Budget(limit=1.0, rate=0.001, room=10.0)
    b5.reserve()
    b5.settle(2 * m)
    assert b5.spent == pytest.approx(2 * m) and "more than its" in b5.refused
    with pytest.raises(J.AttemptRefused):
        b5.reserve()


def test_cap_room_counts_unrecorded_journal_spend(i1a_tmp_layout):
    spend.record("judge", "x", 1, 82.9, "test", exp="I1a")  # judge limit 83
    jp = i1a_tmp_layout / "j.jsonl"
    assert J.cap_room(jp, "a/b") == pytest.approx(0.1)
    J.append_journal(jp, {"call_id": "1", "utc": "t", "model": "m", "cost": 0.04})
    assert J.cap_room(jp, "a/b") == pytest.approx(0.06)


def test_forced_retries_are_cut_off_at_the_projection(i1a_tmp_layout, fake_llm):
    """A reply that is never a JSON object makes wsbench retry each call up to 12 times; each
    attempt is paid. Each attempt reserves the $0.03 maximum, so the pass stops once settled
    spend + one reservation would pass the projection: 4 attempts, not 1 + 2 x 12."""
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 1))
    rate = J.MIN_USD_PER_CALL

    def garbled(system, user):
        return responder(system, user) if user.startswith("Return") else []

    fake = fake_llm(garbled, cost=rate)
    with pytest.raises(SystemExit) as e:
        J.judge_arm("olens_draw2", "multihop", items=ids_of("multihop", 1), usd_per_call=rate)
    assert e.value.code == 2
    proj = J.project(3, rate)  # 2 calls + the preflight
    assert proj == pytest.approx(3 * rate * 1.25 + J.MAX_USD_PER_ATTEMPT)
    n = int((proj - J.MAX_USD_PER_ATTEMPT) / rate + 1e-9) + 1
    assert len(fake.calls) == n == 4
    led = sum(x["usd"] for x in ledger())
    assert led == pytest.approx(n * rate) and led <= proj
    assert len(J.read_journal(J.journal_path("olens_draw2", "multihop"))) == n


def test_a_response_dearer_than_its_reservation_stops_the_pass(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 1))
    fake = fake_llm(responder, cost=0.05)  # above the $0.03 reservation
    with pytest.raises(SystemExit) as e:
        J.judge_arm("olens_draw2", "multihop", items=ids_of("multihop", 1))
    assert e.value.code == 2
    assert len(fake.calls) == 1  # the preflight; nothing after it
    assert sum(x["usd"] for x in ledger()) == pytest.approx(0.05)  # recorded in full


class APITimeoutError(Exception):
    """Named like openai's: wsbench retries it as transient."""


def test_errors_after_sending_are_charged_rejections_are_not(i1a_tmp_layout, fake_llm):
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 1))

    def times_out(system, user):
        if user.startswith("Return"):
            return responder(system, user)
        raise APITimeoutError("read timed out")

    fake_llm(times_out, cost=COST)
    with pytest.raises(SystemExit):
        J.judge_arm("olens_draw2", "multihop", items=ids_of("multihop", 1))
    journal = J.read_journal(J.journal_path("olens_draw2", "multihop"))
    unsure = [r for r in journal if r.get("uncertain")]
    assert unsure and all(r["cost"] == J.MAX_USD_PER_ATTEMPT for r in unsure)
    assert all(r["error"] == "APITimeoutError" for r in unsure)
    assert sum(x["usd"] for x in ledger()) == pytest.approx(sum(r["cost"] for r in journal))
    assert sum(r["cost"] for r in journal) <= J.project(3, J.DEFAULT_USD_PER_CALL) + 1e-9

    # a 400 rejection (the fake's BadRequestError) is journaled at $0, flagged
    write_rows("olens_draw2", "multihop", bank_rows("multihop", 2))
    items = ids_of("multihop", 2)
    fake_llm(lambda s, u: None if items[1] in u else responder(s, u), cost=COST)
    with pytest.raises(SystemExit):
        J.judge_arm("olens_draw2", "multihop", items=items)
    rejected = [
        r for r in J.read_journal(J.journal_path("olens_draw2", "multihop")) if r.get("rejected")
    ]
    assert rejected and all(r["cost"] == 0.0 and r["rejected"] == 400 for r in rejected)
