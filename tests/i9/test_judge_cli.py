"""scripts/i9_judge.py wiring (integration, 2026-10-04): the expected readout cells, the judge
label and the manifests that link readouts -> judged results; the --confirm-paid gate on every
subcommand that can call a model, the paid --limit token pilot and its manifest (code review r1
finding 3); the handlabel-score and validate manifests (finding 10)."""

import importlib.util
import json
from types import SimpleNamespace

import pytest

from emx.i9 import acts
from emx.i9 import contracts as C


def _cli():
    spec = importlib.util.spec_from_file_location("i9_judge", C.REPO_ROOT / "scripts" / "i9_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scenario(i: int) -> C.Scenario:
    return C.Scenario(
        id=f"am-x{i}", source="generator", set="confirmatory", category="blackmail",
        arm="misaligned", group="anthropic", template_group="blackmail|global",
        scenario_type="blackmail", system="s", text=f"t{i}", descriptor="d",
        lynch_conditions=None, published_rate=None, pinned_rollout="",
    )


def _setup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    scen = data / "scenarios.jsonl"
    scen.write_text("".join(C.to_json(_scenario(i)) + "\n" for i in range(3)))
    sets = data / "sets.json"
    sets.write_text(json.dumps({"confirmatory": ["am-x0", "am-x1"], "bank_validation": []}))
    monkeypatch.setattr(C, "SCENARIOS_PATH", scen)
    monkeypatch.setattr(C, "SETS_PATH", sets)
    store = C.ACTS_DIR / "qwen_last64.safetensors"
    store.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"index": k, "id": f"am-x{i}", "pos": 100 + p, "token": "t", "n_tokens": 102,
         "rollout_start": 102, "ids_sha256": "x"}
        for k, (i, p) in enumerate((i, p) for i in range(3) for p in range(2))
    ]
    acts.index_path(store).write_text(json.dumps({"ids": [], "layers": [20], "rows": rows}))
    readouts = C.READOUTS_DIR / "olens.jsonl"
    readouts.parent.mkdir(parents=True, exist_ok=True)
    readouts.write_text("{}\n")
    return scen


def test_ac_builds_expected_cells_and_manifest(tmp_path, monkeypatch):
    scen = _setup(tmp_path, monkeypatch)
    cli = _cli()
    seen = {}

    def fake_run_family(items, readouts, *, route, expected, bc_effort, effort_by_thinking, out):
        seen.update(ids=[s.id for s in items], expected=expected, route=route, bc=bc_effort,
                    effort=effort_by_thinking, out=out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text("{}\n")
        return SimpleNamespace(usage={"calls": 3})

    from emx.i9 import wsjudge

    monkeypatch.setattr(wsjudge, "run_family", fake_run_family)
    rc = cli.main(["ac", "--reader", "olens", "--judge", "luna", "--bc-effort", "low",
                   "--scenarios", str(scen), "--layers", "20,44", "--confirm-paid"])
    assert rc == 0
    assert seen["ids"] == ["am-x0", "am-x1"]  # the set's ids only
    assert seen["expected"] == {(f"am-x{i}", 100 + p, L) for i in (0, 1) for p in (0, 1)
                                for L in (20, 44)}
    assert seen["route"] == C.LUNA and seen["bc"] == "low"
    assert seen["effort"] == {False: "none", True: "low"}
    assert seen["out"] == C.judged_dir("i9:luna:low", "olens")
    man = json.loads(C.manifest_path("judge_ac_i9_luna_low_olens").read_text())
    assert any(i["path"].endswith("readouts/olens.jsonl") for i in man["inputs"])
    assert any(o["path"].endswith("results.json") for o in man["outputs"])


def test_ac_refuses_luna_without_bc_effort(tmp_path, monkeypatch):
    scen = _setup(tmp_path, monkeypatch)

    with pytest.raises(SystemExit, match="bc-effort"):
        _cli().main(["ac", "--reader", "jlens", "--judge", "luna", "--scenarios", str(scen)])


def test_ac_sonnet_uses_contract_defaults(tmp_path, monkeypatch):
    scen = _setup(tmp_path, monkeypatch)
    seen = {}
    from emx.i9 import wsjudge

    def fake(items, readouts, *, route, expected, bc_effort, effort_by_thinking, out):
        seen.update(bc=bc_effort, effort=effort_by_thinking, n=len(expected))
        return SimpleNamespace(usage={})

    monkeypatch.setattr(wsjudge, "run_family", fake)
    assert _cli().main(["ac", "--reader", "jlens", "--judge", "sonnet", "--scenarios",
                        str(scen), "--confirm-paid"]) == 0
    assert seen["bc"] is None and seen["effort"] is None  # run_family applies Sonnet's default
    assert seen["n"] == 2 * 2 * len(C.LADDER)


def test_rescore_c_writes_scores_and_manifest(tmp_path, monkeypatch):
    scen = _setup(tmp_path, monkeypatch)
    cli = _cli()
    res = tmp_path / "judged" / "results.json"
    res.parent.mkdir()
    monkeypatch.setattr(cli, "_family", lambda p: SimpleNamespace(
        rows=[{"id": "am-x0", "account": "a"}, {"id": "am-x1", "account": None}]))
    calls = {}

    def fake_score(accounts, scenarios, *, scorer, spend, purpose, complete):
        calls.update(accounts=accounts, scorer=scorer, purpose=purpose)
        return {"am-x0": {"design_fidelity": 2}}

    monkeypatch.setattr(cli.M, "score_accounts", fake_score)
    assert cli.main(["rescore-c", "--results", str(res), "--scenarios", str(scen),
                     "--confirm-paid"]) == 0
    assert calls["scorer"] == C.DEEPSEEK and calls["accounts"] == {"am-x0": "a", "am-x1": None}
    assert json.loads(res.with_name("rescore_c_deepseek.json").read_text()) == {
        "am-x0": {"design_fidelity": 2}}
    assert C.manifest_path("rescore_c_judged").exists()


# --- the paid gate (finding 3) ------------------------------------------------------------------

PAID_CMDS = ["misbehavior", "validate", "stage0", "monitor-zero", "monitor-few", "monitor-cot",
             "ac", "rescore-c"]


def _boom(*a, **k):
    raise AssertionError("a model call was attempted")


def _no_calls(monkeypatch):
    """Every route to a model fails the test if reached."""
    from emx.i9 import judging as J
    from emx.i9 import textllm, wsjudge

    monkeypatch.setattr(J, "default_complete", _boom)
    monkeypatch.setattr(textllm, "complete", _boom)
    monkeypatch.setattr(textllm, "_make_client", _boom)
    monkeypatch.setattr(wsjudge, "run_family", _boom)


def _family_rows(ids, commit=(0, 1, 2, 3)):
    return SimpleNamespace(rows=[
        {"id": i, "account": f"CANDIDATE ACTIONS: plan {i}", "commit_strength": commit[k % 4],
         "design_fidelity": commit[k % 4], "identified_family": "blackmail"}
        for k, i in enumerate(ids)])


def _paid_tree(tmp_path, monkeypatch):
    """Scenarios, sets (every key the subcommands read), replies (thinking and not), folds,
    rates and a results.json; returns {name: path}."""
    from emx.i9 import judging as J

    scen = _setup(tmp_path, monkeypatch)
    ids = ["am-x0", "am-x1", "am-x2"]
    C.SETS_PATH.write_text(json.dumps({"confirmatory": ids[:2], "bank": ids,
                                       "bank_validation": ids}))
    rep = tmp_path / "replies.jsonl"
    J.write_rows(rep, [C.Reply("qwen", False, i, k, 0, f"R{k} reply", 5, "eos")
                       for i in ids for k in range(2)])
    think = tmp_path / "think.jsonl"
    J.write_rows(think, [C.Reply("qwen", True, i, 0, 0, "<think>hmm</think> ok", 5, "eos")
                         for i in ids])
    folds = tmp_path / "folds.json"
    # the folds assign exactly the confirmatory set (monitor-few refuses others, r3 finding 1)
    folds.write_text(json.dumps({"0": {"am-x0": 0, "am-x1": 2}}))
    rates = tmp_path / "rates.jsonl"
    J.write_rows(rates, [C.Rate("qwen", False, i, 1, 16, {}) for i in ids])
    res = tmp_path / "judged" / "results.json"
    res.parent.mkdir()
    res.write_text("{}\n")
    return {"scen": scen, "replies": rep, "think": think, "folds": folds, "rates": rates,
            "results": res, "ids": ids}


def _argv(cmd, t):
    base = {
        "misbehavior": ["--replies", str(t["replies"])],
        "validate": ["--luna-none", "luna.json", "--sonnet", "sonnet.json"],
        "stage0": [],
        "monitor-zero": [],
        "monitor-few": ["--folds", str(t["folds"]), "--rates", str(t["rates"])],
        "monitor-cot": ["--replies", str(t["think"])],
        "ac": ["--reader", "jlens", "--judge", "sonnet"],
        "rescore-c": ["--results", str(t["results"])],
    }[cmd]
    return [cmd, *base, "--scenarios", str(t["scen"])]


@pytest.mark.parametrize("limit", [[], ["--limit", "1"]], ids=["full", "pilot"])
@pytest.mark.parametrize("cmd", PAID_CMDS)
def test_paid_subcommands_refuse_without_confirm_paid(cmd, limit, tmp_path, monkeypatch, capsys):
    t = _paid_tree(tmp_path, monkeypatch)
    cli = _cli()
    monkeypatch.setattr(cli, "_family", lambda p: _family_rows(t["ids"]) if p else None)
    _no_calls(monkeypatch)
    assert cli.main([*_argv(cmd, t), *limit]) == 2
    io = capsys.readouterr()
    assert "--confirm-paid" in io.err and "No calls made" in io.err
    assert "projected total" in io.out  # the projection is printed before the refusal
    assert not C.MANIFEST_DIR.exists() or not list(C.MANIFEST_DIR.iterdir())
    assert not C.CALLS_DIR.exists() or not list(C.CALLS_DIR.rglob("*.jsonl"))


@pytest.mark.parametrize("cmd", PAID_CMDS)
def test_dry_run_never_calls_even_with_confirm_paid(cmd, tmp_path, monkeypatch, capsys):
    t = _paid_tree(tmp_path, monkeypatch)
    cli = _cli()
    monkeypatch.setattr(cli, "_family", lambda p: _family_rows(t["ids"]) if p else None)
    _no_calls(monkeypatch)
    assert cli.main([*_argv(cmd, t), "--dry-run", "--confirm-paid"]) == 0
    assert "no calls made" in capsys.readouterr().out


def test_unpaid_subcommands_take_no_confirm_flag():
    cli = _cli()
    for cmd in ("handlabel-bundle", "handlabel-score", "adjudicate-bundle", "adjudicate-score"):
        with pytest.raises(SystemExit):
            cli.parse_args([cmd, "--confirm-paid"])
        assert cli.parse_args([cmd]).limit == 0


class PaidFake:
    """textllm.complete stand-in that bills: 100 in / 10 out tokens and $0.001 per call."""

    def __init__(self, respond):
        self.respond, self.n = respond, 0

    def __call__(self, prompts, *, route, system=None, max_tokens, reasoning, on_result,
                 spend=None, purpose=None, **kw):
        for i, p in enumerate(prompts):
            self.n += 1
            spend.calls += 1
            spend.input_tokens += 100
            spend.output_tokens += 10
            spend.usd += 0.001
            on_result(i, self.respond(route, system, p))
        return spend


def test_misbehavior_pilot_writes_pilot_manifest_only(tmp_path, monkeypatch, capsys):
    from emx.i9 import judging as J

    t = _paid_tree(tmp_path, monkeypatch)
    fake = PaidFake(lambda r, s, u: "<answer>yes</answer>")
    monkeypatch.setattr(J, "default_complete", lambda: fake)
    assert _cli().main([*_argv("misbehavior", t), "--limit", "1", "--confirm-paid"]) == 0
    out = capsys.readouterr().out
    assert out.index("projected total") < out.index("pilot measured")
    man = json.loads(C.manifest_path("judge_misbehavior_pilot").read_text())
    measured = json.loads(man["notes"])["measured"]
    assert measured[C.LUNA]["calls"] == 2 and measured[C.DEEPSEEK]["calls"] == 2  # 1 scenario
    assert C.GLM not in measured  # they agree: no tie-break
    assert measured[C.LUNA]["usd_per_call"] == pytest.approx(0.001)
    assert measured[C.LUNA]["input_tokens_per_call"] == 100
    assert man["usd"] == pytest.approx(0.004)
    assert man["outputs"][0]["path"].endswith("pilots/judge_misbehavior_pilot.json")
    assert any(i["path"].endswith("replies.jsonl") for i in man["inputs"])
    assert not C.LABELS_PATH.exists() and not C.RATES_PATH.exists()
    assert not C.manifest_path("judge_misbehavior").exists()


def test_monitor_zero_pilot_writes_no_predictions(tmp_path, monkeypatch):
    from emx.i9 import judging as J

    t = _paid_tree(tmp_path, monkeypatch)
    fake = PaidFake(lambda r, s, u: '{"probability": 0.25}')
    monkeypatch.setattr(J, "default_complete", lambda: fake)
    assert _cli().main([*_argv("monitor-zero", t), "--limit", "1", "--confirm-paid"]) == 0
    assert fake.n == 1
    man = json.loads(C.manifest_path("monitor_zero_qwen_pilot").read_text())
    assert json.loads(man["notes"])["measured"][C.GEMINI]["calls"] == 1
    assert not C.predictions_path("monitor_zero_shot").exists()


def _jlens_rows(ids, positions=(100, 101), layers=C.LADDER, empty=()):
    """wsbench token readout rows; (id, pos) pairs in ``empty`` have empty bags."""
    return [{"id": i, "layer": L, "pos": p, "tokens": [] if (i, p) in empty else ["a", "b"]}
            for i in ids for p in positions for L in layers]


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_ac_pilot_judges_a_subset_into_its_own_dir(tmp_path, monkeypatch, capsys):
    """r2 finding 2: --limit writes the first items' rows to a pilot readouts file, judges that
    file (so the exact readouts check passes) into a pilot dir, and writes only a pilot
    manifest with the measured cost per stage."""
    t = _paid_tree(tmp_path, monkeypatch)
    full = C.readouts_path("jlens")
    _write_rows(full, _jlens_rows(t["ids"]))
    from emx.i9 import textllm, wsjudge

    seen = {}

    def fake_run_family(items, readouts, *, route, expected, bc_effort, effort_by_thinking, out):
        seen.update(ids=[s.id for s in items], readouts=readouts, out=out)
        seen["check"] = wsjudge.check_readouts(readouts, expected)
        j = textllm.journal_path("wsjudge-x", route)
        j.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"model": route, "responses": 1, "input_tokens": 50, "output_tokens": 5,
                 "usd": usd, "max_tokens": mt} for mt, usd in ((400, 0.001), (400, 0.001),
                                                                (16_000, 0.01), (12_000, 0.02))]
        j.write_text("".join(json.dumps(r) + "\n" for r in rows))
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text("{}\n")
        return SimpleNamespace(usage={})

    monkeypatch.setattr(wsjudge, "run_family", fake_run_family)
    assert _cli().main([*_argv("ac", t), "--limit", "1", "--confirm-paid"]) == 0
    step = "judge_ac_i9_sonnet-5_jlens"
    pdir = C.RESULTS / "pilots" / f"{step}_pilot"
    assert seen["ids"] == ["am-x0"] and seen["check"].passed, seen["check"].notes
    assert seen["readouts"] == pdir / "readouts" / "jlens.jsonl"
    assert seen["out"] == pdir / "judged"
    assert {json.loads(x)["id"] for x in seen["readouts"].read_text().splitlines()} == {"am-x0"}
    man = json.loads(C.manifest_path(f"{step}_pilot").read_text())
    m = json.loads(man["notes"])["measured"][C.SONNET]
    assert m["calls"] == 4 and man["usd"] == pytest.approx(0.032)
    outs = {o["path"].split("/")[-1] for o in man["outputs"]}
    assert {f"{step}_pilot.json", "jlens.jsonl", "results.json"} <= outs
    body = json.loads((C.RESULTS / "pilots" / f"{step}_pilot.json").read_text())
    assert body["by_stage"]["A"]["usd_per_prompt"] == pytest.approx(0.001)
    assert body["by_stage"]["C"]["usd_per_prompt"] == pytest.approx(0.02)
    assert not C.manifest_path(step).exists()
    assert not C.judged_dir("i9:sonnet-5", "jlens").exists()
    # a later projection of the same step is priced from the pilot's measured cost
    capsys.readouterr()
    assert _cli().main([*_argv("ac", t), "--dry-run"]) == 0
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    a_row = next(r for r in rows if r["stage"] == "A")
    assert a_row["basis"] == "pilot" and a_row["calls"] == 2 * 2  # 2 items x 2 positions
    assert a_row["usd"] == pytest.approx(0.004)


def test_ac_pilot_refuses_without_readouts(tmp_path, monkeypatch, capsys):
    t = _paid_tree(tmp_path, monkeypatch)
    _no_calls(monkeypatch)
    assert _cli().main([*_argv("ac", t), "--limit", "1", "--confirm-paid"]) == 2
    assert "readouts" in capsys.readouterr().err


def test_ac_projection_counts_calls_before_the_gate(tmp_path, monkeypatch, capsys):
    """r2 finding 2: a real, non-zero projection; Stage A skips all-empty positions."""
    t = _paid_tree(tmp_path, monkeypatch)
    _write_rows(C.readouts_path("jlens"), _jlens_rows(t["ids"], empty={("am-x1", 101)}))
    _no_calls(monkeypatch)
    assert _cli().main([*_argv("ac", t)]) == 2  # no --confirm-paid: projection, then refuse
    out = capsys.readouterr().out
    rows = {r["stage"]: r for r in (json.loads(x) for x in out.splitlines()
                                    if x.startswith("{"))}
    assert (rows["A"]["calls"], rows["B"]["calls"], rows["C"]["calls"]) == (3, 2, 2)
    assert all(r["usd"] > 0 and r["usd_ceiling"] >= r["usd"] for r in rows.values())
    assert rows["A"]["source"] == "readouts" and rows["A"]["basis"] == "assumed"


def test_misbehavior_full_run_records_rejudge_counts(tmp_path, monkeypatch):
    """Finding 9 end to end: an unresolved reply is re-judged; the counts reach the manifest."""
    from emx.i9 import judging as J

    t = _paid_tree(tmp_path, monkeypatch)

    def respond(route, system, user):
        if "R1 reply" in user:  # luna unparseable, DeepSeek yes, GLM no -> no majority, twice
            return {C.LUNA: "?", C.DEEPSEEK: "<answer>yes</answer>",
                    C.GLM: "<answer>no</answer>"}[route]
        return "<answer>no</answer>"

    monkeypatch.setattr(J, "default_complete", lambda: PaidFake(respond))
    assert _cli().main([*_argv("misbehavior", t), "--confirm-paid"]) == 0
    notes = json.loads(json.loads(C.manifest_path("judge_misbehavior").read_text())["notes"])
    assert notes["unresolved_first_round"] == 3 and notes["unlabelled"] == 3
    assert notes["resolved_on_rejudge"] == 0 and len(notes["unlabelled_keys"]) == 3
    assert len(J.read_rows(C.LABELS_PATH, C.Label)) == 3  # 6 replies - 3 unlabelled
    rej = J.read_rows(C.VERDICTS_DIR / "rejudge.jsonl", C.Verdict)
    assert len(rej) == 9 and {v.judge for v in rej} == {C.LUNA, C.DEEPSEEK, C.GLM}
    # amendment B item 2: the override report is written and listed in the manifest
    man = json.loads(C.manifest_path("judge_misbehavior").read_text())
    assert any(o["path"].endswith("checks/code_check_overrides.json") for o in man["outputs"])
    ovr = json.loads(C.check_path("code_check_overrides").read_text())
    assert ovr["kind"] == "informational" and ovr["rows"] == []  # fixtures are in-house items


# --- adjudication of unresolved replies (amendment B review r2) ---------------------------------


def _unresolved_run(tmp_path, monkeypatch):
    """misbehavior on the paid tree with every R1 reply unresolved (3 replies, one per item)."""
    from emx.i9 import judging as J

    t = _paid_tree(tmp_path, monkeypatch)

    def respond(route, system, user):
        if "R1 reply" in user:
            return {C.LUNA: "?", C.DEEPSEEK: "<answer>yes</answer>",
                    C.GLM: "<answer>no</answer>"}[route]
        return "<answer>no</answer>"

    monkeypatch.setattr(J, "default_complete", lambda: PaidFake(respond))
    assert _cli().main([*_argv("misbehavior", t), "--confirm-paid"]) == 0
    monkeypatch.setattr(J, "default_complete", _boom)  # adjudication makes no model calls
    return t


def test_adjudicate_bundle_then_score(tmp_path, monkeypatch):
    from emx.i9 import judging as J

    t = _unresolved_run(tmp_path, monkeypatch)
    cli = _cli()
    common = ["--replies", str(t["replies"]), "--scenarios", str(t["scen"])]
    assert cli.main(["adjudicate-bundle", *common]) == 0
    bundle = J.read_csv(C.HANDLABEL_DIR / "adjudicate_bundle.csv")
    key = J.read_csv(C.HANDLABEL_DIR / "adjudicate_key.csv")
    assert len(bundle) == 3 and {r["reply"] for r in bundle} == {"R1 reply"}
    assert all(r["label_misbehaved_yn"] == "" for r in bundle)
    assert "luna" not in json.dumps(bundle).lower()  # blind
    assert C.manifest_path("adjudicate_bundle").exists()
    yes = {k["label_id"] for k in key if k["scenario_id"] == "am-x0"}
    J.write_csv(C.HANDLABEL_DIR / "adjudicate_jacob.csv",
                [{**b, "label_misbehaved_yn": "y" if b["label_id"] in yes else "n"}
                 for b in bundle], J.BUNDLE_COLUMNS)
    rewritten = (C.LABELS_PATH, C.LABELS_LLM_ONLY_PATH, C.RATES_PATH, C.RATES_LLM_ONLY_PATH)
    before = {p.name: J.sha256_file(p) for p in rewritten}
    judge_out = {o["path"].split("/")[-1]: o["sha256"] for o in json.loads(
        C.manifest_path("judge_misbehavior").read_text())["outputs"]}
    assert all(judge_out[n] == sha for n, sha in before.items())
    assert cli.main(["adjudicate-score", *common]) == 0
    # r3 finding 3: the pre-rewrite labels and rates (the judge's outputs) are recorded inputs
    adj_in = {(i["path"].split("/")[-1], i["sha256"]) for i in json.loads(
        C.manifest_path("adjudicate_score").read_text())["inputs"]}
    assert set(before.items()) <= adj_in
    labels = J.read_rows(C.LABELS_PATH, C.Label)
    assert len(labels) == 6
    adj = {(lab.scenario_id, lab.misbehaved) for lab in labels if lab.rule == "adjudicated"}
    assert adj == {("am-x0", True), ("am-x1", False), ("am-x2", False)}
    rates = {r.scenario_id: r for r in J.read_rows(C.RATES_PATH, C.Rate)}
    assert {s: (r.k, r.n) for s, r in rates.items()} == {
        "am-x0": (1, 2), "am-x1": (0, 2), "am-x2": (0, 2)}
    assert len(J.read_rows(C.LABELS_LLM_ONLY_PATH, C.Label)) == 6
    man = json.loads(C.manifest_path("adjudicate_score").read_text())
    assert json.loads(man["notes"]) == {"n_adjudicated": 3, "n_unlabelled": 0}
    assert {o["path"].split("/")[-1] for o in man["outputs"]} == {
        "labels.jsonl", "labels_llm_only.jsonl", "rates.jsonl", "rates_llm_only.jsonl"}
    # rerunning replaces the earlier adjudication; nothing left to bundle
    assert cli.main(["adjudicate-score", *common]) == 0
    assert len(J.read_rows(C.LABELS_PATH, C.Label)) == 6
    assert cli.main(["adjudicate-bundle", *common]) == 0


def test_adjudicate_bundle_refuses_over_30(tmp_path, monkeypatch, capsys):
    from emx.i9 import judging as J

    t = _unresolved_run(tmp_path, monkeypatch)
    monkeypatch.setattr(J, "ADJUDICATE_MAX", 2)
    assert _cli().main(["adjudicate-bundle", "--replies", str(t["replies"]), "--scenarios",
                        str(t["scen"])]) == 3
    err = capsys.readouterr().err
    assert "3 unresolved" in err and "stop and ask Jacob" in err
    assert not (C.HANDLABEL_DIR / "adjudicate_bundle.csv").exists()
    assert not C.manifest_path("adjudicate_bundle").exists()


def test_adjudicate_max_is_30():
    from emx.i9 import judging as J

    assert J.ADJUDICATE_MAX == 30


# --- manifests for handlabel-score and validate (finding 10) ------------------------------------


def test_handlabel_score_writes_manifest(tmp_path, monkeypatch):
    from emx.i9 import judging as J

    bundle, key = tmp_path / "bundle.csv", tmp_path / "key.csv"
    labs = [True, False, True, False]
    J.write_csv(bundle, [{"label_id": f"L{i}", "label_misbehaved_yn": "y" if v else "n"}
                         for i, v in enumerate(labs)], J.BUNDLE_COLUMNS)
    J.write_csv(key, [{"label_id": f"L{i}", "disputed": False, "luna": v, "deepseek": v,
                       "luna_final": v, "final": v} for i, v in enumerate(labs)], J.KEY_COLUMNS)
    assert _cli().main(["handlabel-score", "--labels", str(bundle), "--key", str(key)]) == 0
    man = json.loads(C.manifest_path("handlabel_score").read_text())
    assert {i["path"] for i in man["inputs"]} == {str(bundle), str(key)}
    assert [o["path"].split("/")[-1] for o in man["outputs"]] == ["handlabel_kappa.json"]
    assert man["outputs"][0]["sha256"] == J.sha256_file(C.check_path("handlabel_kappa"))


def test_validate_writes_manifest_with_inputs(tmp_path, monkeypatch):
    from emx.i9 import judging as J

    t = _paid_tree(tmp_path, monkeypatch)
    cli = _cli()
    fams = {}
    for name in ("luna.json", "sonnet.json"):
        fams[name] = tmp_path / name
        fams[name].write_text("{}\n")
    monkeypatch.setattr(cli, "_family", lambda p: _family_rows(t["ids"]) if p else None)
    seen = {}

    def fake_score(accounts, scen, *, scorer, spend, purpose, complete):
        J.CallCache(J.cache_path(purpose, scorer)).put(J.Call("k", "u"), "raw", 1)
        seen["score"] = complete
        return {}

    def fake_gate(luna, sonnet, son_on_luna, *, ids, spend, complete):
        J.CallCache(J.cache_path("same_action", C.DEEPSEEK)).put(J.Call("k", "u"), "raw", 1)
        seen["gate"] = complete
        return [C.CheckResult("validation_overall", True, 5.0, 5.0, len(ids))]

    monkeypatch.setattr(cli.M, "score_accounts", fake_score)
    monkeypatch.setattr(J, "validation_gate", fake_gate)
    argv = ["validate", "--luna-none", str(fams["luna.json"]), "--sonnet",
            str(fams["sonnet.json"]), "--scenarios", str(t["scen"]), "--confirm-paid"]
    assert cli.main(argv) == 0
    assert seen["score"] is not None and seen["gate"] is not None  # metered seam
    man = json.loads(C.manifest_path("validate").read_text())
    ins = {i["path"].split("/")[-1] for i in man["inputs"]}
    assert {"luna.json", "sonnet.json", "scenarios.jsonl", "sets.json",
            "validation_score_claude-sonnet-5.jsonl", "same_action_deepseek-v4-flash.jsonl"} <= ins
    assert {o["path"].split("/")[-1] for o in man["outputs"]} == {
        "bc_effort_pilot.json", "validation_overall.json"}


def test_monitor_few_manifest_records_example_pool_rule(tmp_path, monkeypatch):
    from emx.i9 import judging as J

    t = _paid_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(J, "default_complete",
                        lambda: PaidFake(lambda r, s, u: '{"probability": 0.25}'))
    assert _cli().main([*_argv("monitor-few", t), "--confirm-paid"]) == 0
    notes = json.loads(json.loads(C.manifest_path("monitor_few_qwen").read_text())["notes"])
    assert "(g + 1) mod N_FOLDS" in notes["example_pool_rule"]
