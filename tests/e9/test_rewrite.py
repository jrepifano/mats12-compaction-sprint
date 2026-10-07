"""E9 rewriter (unit B2a): writer routing and the regeneration sequence of prereg §4.4, with a
fake `run_batch` (no network, no paid calls) and a whitespace tokenizer."""

from __future__ import annotations

import json

import pytest

from emx.e9 import contracts as C
from emx.e9 import rewrite as R
from emx.e9 import text as T

SEED = C.REWRITE_REQUEST_SEED


class WsTok:
    """Whitespace tokenizer with the HF call shape text.n_tokens uses."""

    def __call__(self, s, add_special_tokens=False):
        return {"input_ids": s.split()}


TOK = WsTok()


class FakeLLM:
    """run_batch stand-in. `script[(model, version, attempt)]` maps doc_id -> status (default ok);
    an ok output's text is "<model> <version> a<attempt> <doc text>"."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[dict] = []

    def __call__(
        self,
        model_key,
        prompt_id,
        requests,
        out_path,
        *,
        mode,
        stage,
        note,
        concurrency=32,
        client=None,
        reserve=None,
    ):
        assert mode == "text"
        self.calls.append(
            {
                "model": model_key,
                "prompt_id": prompt_id,
                "stage": stage,
                "note": note,
                "out_path": str(out_path),
                "requests": list(requests),
            }
        )
        rows = []
        for req in requests:
            doc_id, version, attempt = req["item_id"].rsplit("|", 2)
            status = self.script.get((model_key, version, int(attempt)), {}).get(doc_id, "ok")
            text = (
                f"{model_key} {version} a{attempt} {req['fields']['document']}"
                if status == "ok"
                else ""
            )
            rows.append(
                {k: None for k in C.LLM_RESULT_KEYS}
                | {
                    "item_id": req["item_id"],
                    "model_key": model_key,
                    "prompt_id": prompt_id,
                    "seed": req["seed"],
                    "status": status,
                    "text": text,
                    "usd": 0.001,
                    "model": C.MODELS[model_key].model,
                }
            )
        return rows

    def items(self, model=None):
        return [
            (c["model"], r["item_id"], r["seed"])
            for c in self.calls
            for r in c["requests"]
            if model in (None, c["model"])
        ]


def docs(n=3):
    return [
        {"doc_id": f"shard/x.jsonl#{i}", "text": f"document number {i} text", "n_tokens": 4}
        for i in range(n)
    ]


D0, D1, D2 = (f"shard/x.jsonl#{i}" for i in range(3))


@pytest.fixture
def kw(tmp_path):
    return {"stage": "setup", "out_dir": tmp_path / "rw", "llm_dir": tmp_path / "llm", "tok": TOK}


def test_luna_ok_no_fallback(kw):
    llm = FakeLLM()
    res = R.write_versions(docs(), ("corr", "para"), run_batch=llm, **kw)
    assert {c["model"] for c in llm.calls} == {"luna"}
    assert [c["prompt_id"] for c in llm.calls] == ["A2_correct", "A3_paraphrase"]
    assert len(res) == 6 and all(r["writer"] == "luna" and r["attempt"] == 1 for r in res.values())
    assert all(s == SEED for _, _, s in llm.items())
    req = llm.calls[0]["requests"][0]
    assert req == {
        "item_id": f"{D0}|corr|1",
        "fields": {"document": "document number 0 text"},
        "seed": SEED,
        "ref_chars": len("document number 0 text"),
    }
    assert R.version_state(D0, "corr", out_dir=kw["out_dir"]) == "to_check"


@pytest.mark.parametrize("status", ["refused", "empty"])
def test_luna_refused_goes_to_qwen_same_prompt_and_seed(kw, status):
    llm = FakeLLM({("luna", "corr", 1): {D1: status}})
    res = R.write_versions(docs(), ("corr",), run_batch=llm, **kw)
    qcalls = [c for c in llm.calls if c["model"] == "qwn"]
    assert len(qcalls) == 1 and qcalls[0]["prompt_id"] == "A2_correct"
    assert [(r["item_id"], r["seed"]) for r in qcalls[0]["requests"]] == [(f"{D1}|corr|1", SEED)]
    luna_req = next(r for r in llm.calls[0]["requests"] if r["item_id"] == f"{D1}|corr|1")
    assert qcalls[0]["requests"][0] == luna_req  # same prompt fields and seed
    assert res[(D1, "corr")]["writer"] == "qwn" and res[(D0, "corr")]["writer"] == "luna"
    att = R.load_attempts(kw["out_dir"])
    assert att[(D1, "corr", 1, "luna")]["status"] == status
    assert att[(D1, "corr", 1, "luna")]["n_tokens"] is None


def test_qwen_refusal_counts_as_failed_and_is_regenerated_by_qwen(kw):
    llm = FakeLLM({("luna", "para", 1): {D2: "refused"}, ("qwn", "para", 1): {D2: "refused"}})
    R.write_versions(docs(), ("para",), run_batch=llm, **kw)
    assert R.version_state(D2, "para", out_dir=kw["out_dir"]) == "refused_fallback"
    failed = R.refused_fallbacks(out_dir=kw["out_dir"])
    assert failed == [(D2, "para", "qwn")]
    llm.calls.clear()
    res = R.regenerate(failed, docs=docs(), run_batch=llm, **kw)
    assert [(c["model"], c["prompt_id"]) for c in llm.calls] == [("qwn", "A3_paraphrase")]
    assert llm.items() == [("qwn", f"{D2}|para|2", SEED + 1)]
    rec = res[(D2, "para")]
    assert (rec["writer"], rec["attempt"], rec["seed"], rec["status"]) == ("qwn", 2, SEED + 1, "ok")
    assert R.version_state(D2, "para", out_dir=kw["out_dir"]) == "regenerated"
    assert R.refused_fallbacks(out_dir=kw["out_dir"]) == []


def test_regeneration_by_same_writer_only(kw):
    llm = FakeLLM({("luna", "corr", 1): {D1: "refused"}})
    R.write_versions(docs(), ("corr",), run_batch=llm, **kw)
    with pytest.raises(ValueError, match="same writer"):
        R.regenerate([(D0, "corr", "qwn")], docs=docs(), run_batch=llm, **kw)
    with pytest.raises(ValueError, match="same writer"):
        R.regenerate([(D1, "corr", "luna")], docs=docs(), run_batch=llm, **kw)
    llm.calls.clear()
    res = R.regenerate(
        [(D0, "corr", "luna"), (D1, "corr", "qwn")], docs=docs(), run_batch=llm, **kw
    )
    assert sorted(llm.items()) == [
        ("luna", f"{D0}|corr|2", SEED + 1),
        ("qwn", f"{D1}|corr|2", SEED + 1),
    ]
    assert res[(D0, "corr")]["writer"] == "luna" and res[(D1, "corr")]["writer"] == "qwn"


def test_regeneration_rejects_changed_document_and_missing_attempt(kw):
    llm = FakeLLM()
    R.write_versions(docs(1), ("corr",), run_batch=llm, **kw)
    changed = [docs(1)[0] | {"text": "something else"}]
    with pytest.raises(ValueError, match="differs"):
        R.regenerate([(D0, "corr", "luna")], docs=changed, run_batch=llm, **kw)
    with pytest.raises(ValueError, match="no attempt 1"):
        R.regenerate([(D0, "para", "luna")], docs=docs(1), run_batch=llm, **kw)


def test_luna_refusal_without_fallback_cannot_regenerate(kw, tmp_path):
    # a crash between luna's batch and Qwen's leaves "needs_fallback"; luna must not regenerate
    llm = FakeLLM({("luna", "corr", 1): {D0: "refused"}})
    R._call(
        "luna",
        "corr",
        1,
        [R._request(docs(1)[0], "corr", 1)],
        R._docs_map(docs(1)),
        stage="setup",
        batch="pass1",
        run_batch=llm,
        out_dir=kw["out_dir"],
        llm_dir=kw["llm_dir"],
        concurrency=4,
        tok=TOK,
    )
    assert R.version_state(D0, "corr", out_dir=kw["out_dir"]) == "needs_fallback"
    with pytest.raises(ValueError, match="Qwen writes first"):
        R.regenerate([(D0, "corr", "luna")], docs=docs(1), run_batch=llm, **kw)
    llm.calls.clear()
    R.write_versions(docs(1), ("corr",), run_batch=llm, **kw)  # resume: only Qwen is called
    assert llm.items() == [("qwn", f"{D0}|corr|1", SEED)]


def test_second_failure_drops(kw):
    llm = FakeLLM({("luna", "corr", 2): {D0: "empty"}})
    R.write_versions(docs(1), ("corr",), run_batch=llm, **kw)
    res = R.regenerate([(D0, "corr", "luna")], docs=docs(1), run_batch=llm, **kw)
    assert res[(D0, "corr")]["status"] == "empty"
    assert R.version_state(D0, "corr", out_dir=kw["out_dir"]) == "dropped"
    llm.calls.clear()  # no third attempt, ever
    again = R.regenerate([(D0, "corr", "luna")], docs=docs(1), run_batch=llm, **kw)
    assert llm.calls == [] and again[(D0, "corr")]["status"] == "empty"


def test_attempts_recorded_and_resumable(kw):
    llm = FakeLLM({("luna", "corr", 1): {D1: "refused"}})
    R.write_versions(docs(), ("corr", "para"), run_batch=llm, **kw)
    n_calls = len(llm.calls)
    lines = R.attempts_path(kw["out_dir"]).read_text().splitlines()
    assert len(lines) == 7  # 3 luna corr + 1 qwn corr + 3 luna para
    for line in lines:
        rec = json.loads(line)
        assert set(C.REWRITE_ATTEMPT_KEYS) <= set(rec)
        assert rec["writer"] in C.WRITERS and rec["version"] in C.VERSIONS
    R.write_versions(docs(), ("corr", "para"), run_batch=llm, **kw)
    assert len(llm.calls) == n_calls  # nothing re-sent
    assert len(R.attempts_path(kw["out_dir"]).read_text().splitlines()) == 7
    llm.calls.clear()
    R.write_versions(docs(4), ("corr",), run_batch=llm, **kw)  # one new document only
    assert llm.items() == [("luna", "shard/x.jsonl#3|corr|1", SEED)]


def test_unreturned_rows_are_resent_on_resume(kw):
    class Partial(FakeLLM):
        def __call__(self, model_key, prompt_id, requests, out_path, **k):
            return super().__call__(model_key, prompt_id, requests, out_path, **k)[:1]

    R.write_versions(docs(), ("corr",), run_batch=Partial(), **kw)
    assert len(R.load_attempts(kw["out_dir"])) == 1
    llm = FakeLLM()
    R.write_versions(docs(), ("corr",), run_batch=llm, **kw)
    assert [i for _, i, _ in llm.items()] == [f"{D1}|corr|1", f"{D2}|corr|1"]


def test_version_rows_hash_and_tokens(kw):
    llm = FakeLLM()
    R.write_versions(docs(2), ("corr", "para"), run_batch=llm, **kw)
    rows = R.version_rows([(D0, "corr"), (D1, "para")], out_dir=kw["out_dir"], tok=TOK)
    assert [tuple(r) for r in rows] == [C.VERSION_KEYS] * 2
    r = rows[0]
    assert r["text"] == "luna corr a1 document number 0 text"
    assert r["n_tokens"] == 7 and r["sha256"] == T.text_sha256(r["text"])
    assert (r["writer"], r["attempt"], r["seed"]) == ("luna", 1, SEED)
    assert len(R.version_rows(out_dir=kw["out_dir"], tok=TOK)) == 4
    o = R.orig_version_row(docs(1)[0], tok=TOK)
    assert (o["version"], o["n_tokens"], o["writer"], o["attempt"]) == ("orig", 4, None, 0)
    att = R.load_attempts(kw["out_dir"])
    assert att[(D0, "corr", 1, "luna")]["n_tokens"] == 7


def test_version_rows_with_olmo_tokenizer(kw, olmo_tok):
    R.write_versions(docs(1), ("corr",), run_batch=FakeLLM(), **(kw | {"tok": olmo_tok}))
    (row,) = R.version_rows(out_dir=kw["out_dir"], tok=olmo_tok)
    assert row["n_tokens"] == T.n_tokens(row["text"], olmo_tok) > 0


def test_stage_and_notes_passed_through(kw):
    llm = FakeLLM({("luna", "corr", 1): {D0: "refused"}})
    R.write_versions(
        docs(1), ("corr",), run_batch=llm, batch="repl2", **(kw | {"stage": "part2_mandatory"})
    )
    assert [c["stage"] for c in llm.calls] == ["part2_mandatory"] * 2
    assert [c["note"] for c in llm.calls] == [
        "E9:part2_mandatory:rewrite:luna-corr-a1-repl2",
        "E9:part2_mandatory:rewrite:qwn-corr-a1-repl2",
    ]
    assert len({c["out_path"] for c in llm.calls}) == 2
    recs = R.load_attempts(kw["out_dir"]).values()
    assert {r["stage"] for r in recs} == {"part2_mandatory"}
    with pytest.raises(ValueError, match="unknown stage"):
        R.write_versions(docs(1), ("para",), run_batch=llm, **(kw | {"stage": "nope"}))
    with pytest.raises(ValueError):
        R.write_versions(docs(1), ("orig",), run_batch=llm, **kw)


def test_refusal_stats_and_endorsement_hook(kw):
    llm = FakeLLM(
        {
            ("luna", "corr", 1): {D0: "refused", D1: "empty"},
            ("qwn", "corr", 1): {D0: "refused"},
            ("qwn", "corr", 2): {D0: "refused"},
        }
    )
    R.write_versions(docs(), ("corr", "para"), run_batch=llm, **kw)
    R.regenerate(
        R.refused_fallbacks(out_dir=kw["out_dir"]) + [(D2, "para", "luna")],
        docs=docs(),
        run_batch=llm,
        **kw,
    )
    passed = {(D2, "para", 2, "luna"): False}
    s = R.refusal_stats(out_dir=kw["out_dir"], check_passed=passed, scores={D0: 9, D1: 7, D2: 4})
    corr = s["corr"]
    assert corr["step1_luna"]["n"] == 3
    assert corr["step1_luna"]["refused_or_empty_rate"] == pytest.approx(2 / 3)
    assert corr["step2_qwn"]["n"] == 2 and corr["step2_qwn"]["refused_rate"] == 0.5
    assert corr["step3_regeneration"]["qwn"] == {
        "n": 1,
        "status": {"refused": 1},
        "checked": 0,
        "passed": 0,
        "dropped": 1,
    }
    assert corr["standing_by_writer"] == {"luna": 1, "qwn": 1}
    para = s["para"]["step3_regeneration"]["luna"]
    assert (para["n"], para["checked"], para["passed"], para["dropped"]) == (1, 1, 0, 1)
    e = s["endorsement_refused_vs_rest"]
    assert e["refused"] == {"n": 2, "mean": 8.0, "median": 8.0}
    assert e["rest"]["n"] == 1 and e["missing_scores"] == []
    assert s["all"]["step1_luna"]["n"] == 6


def test_projections_use_estimate(kw):
    calls = []

    def est(model, prompt_id, reqs, mode):
        calls.append((model, prompt_id, len(reqs), mode))
        return 0.01 * len(reqs)

    p = R.project_write(docs(), ("corr", "para"), estimate=est, out_dir=kw["out_dir"])
    assert p["n_luna"] == 6 and p["luna"] == pytest.approx(0.06)
    assert p["total_worst_case"] == pytest.approx(0.12)
    assert all(m == "text" for *_, m in calls)
    R.write_versions(docs(), ("corr",), run_batch=FakeLLM(), **kw)
    p = R.project_write(docs(), ("corr", "para"), estimate=est, out_dir=kw["out_dir"])
    assert p["n_luna"] == 3
    g = R.project_regenerate(
        [(D0, "corr", "luna")], docs=docs(), estimate=est, out_dir=kw["out_dir"]
    )
    assert g == {"by_writer": {"luna": pytest.approx(0.01)}, "n": 1, "total": pytest.approx(0.01)}


def test_cli_dry_run_makes_no_call(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "e9_rewrite_cli", R.REPO_ROOT / "scripts" / "e9_rewrite.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    f = tmp_path / "docs.jsonl"
    f.write_text("".join(json.dumps(d) + "\n" for d in docs()))
    monkeypatch.setattr(R, "_estimate_default", lambda: lambda m, p, r, mode: 0.5 * len(r))
    monkeypatch.setattr(R, "_run_batch_default", lambda: pytest.fail("no call on dry run"))
    out = tmp_path / "rw"
    assert (
        cli.main(
            ["write", "--docs", str(f), "--versions", "corr", "--out-dir", str(out), "--dry-run"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["projected"]["luna"] == 1.5
    assert cli.main(["write", "--docs", str(f), "--out-dir", str(out)]) == 2  # no --confirm-paid
    checks = tmp_path / "checks.jsonl"
    checks.write_text(
        json.dumps(
            {"doc_id": D0, "version": "corr", "attempt": 1, "writer": "luna", "passed": False}
        )
        + "\n"
    )
    R.write_versions(
        docs(),
        ("corr",),
        run_batch=FakeLLM(),
        stage="setup",
        out_dir=out,
        llm_dir=tmp_path / "llm",
        tok=TOK,
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "regenerate",
                "--from",
                str(checks),
                "--docs",
                str(f),
                "--out-dir",
                str(out),
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["projected"]["n"] == 1
    assert cli.main(["stats", "--out-dir", str(out)]) == 0
    assert "step1_luna" in capsys.readouterr().out
    dup = [
        {"doc_id": D0, "version": "corr", "attempt": 1, "writer": "luna", "passed": v}
        for v in (True, False)
    ]
    with pytest.raises(SystemExit, match="conflicting"):
        cli.failed_from_checks(dup)


def test_api_errors_are_resent_not_routed(kw):
    llm = FakeLLM({("luna", "corr", 1): {D1: "error"}})
    res = R.write_versions(docs(), ("corr",), run_batch=llm, **kw)
    assert {c["model"] for c in llm.calls} == {"luna"}  # an error is not a refusal
    assert (D1, "corr") not in res
    assert R.version_state(D1, "corr", out_dir=kw["out_dir"]) == "pending"
    assert R.state_counts([D0, D1, D2], ("corr",), out_dir=kw["out_dir"]) == {
        "to_check": 2,
        "pending": 1,
    }
    llm2 = FakeLLM()
    R.write_versions(docs(), ("corr",), run_batch=llm2, **kw)  # resend to the same writer
    assert llm2.items() == [("luna", f"{D1}|corr|1", SEED)]


def test_errors_to_fallback_routes_sent_errors_only(kw):
    class Err(FakeLLM):
        def __call__(self, model_key, prompt_id, requests, out_path, **k):
            rows = super().__call__(model_key, prompt_id, requests, out_path, **k)
            for r in rows:  # D1 sent and failed; D2 cut off before sending
                if r["status"] == "error":
                    r["attempts"] = 3 if r["item_id"].startswith(D1) else 0
            return rows

    llm = Err({("luna", "corr", 1): {D1: "error", D2: "error"}})
    R.write_versions(docs(), ("corr",), run_batch=llm, errors_to_fallback=True, **kw)
    assert [i for m, i, _ in llm.items() if m == "qwn"] == [f"{D1}|corr|1"]
    assert R.version_state(D2, "corr", out_dir=kw["out_dir"]) == "pending"
