# ruff: noqa: F811 - fixtures imported from other test modules are used as arguments
"""I9 end to end on the tiny model (implementation plan §6, "Slow end-to-end test"): do the units'
file formats join up? 3 scenarios (2 drawn generator conditions, 1 bank item) go, in a tmp repo
tree, through:

1. U1 scenario rows (`emx.i9.scenarios`) -> data/I9/scenarios.jsonl, sets.json + a manifest;
2. U2 sampling: Qwen with the HF sampler on the tiny model, Gemma with the vLLM sampler on a fake
   engine (the tiny model has Qwen's vocabulary), 2 replies each, prompts from `render_ids`;
3. U3 pod driver (`scripts/i9_pod.py`): capture-last, capture-64 and tokens (logit lens and
   J-Lens) on the tiny backend, with the real renderer;
4. U4 misbehavior judge (`scripts/i9_judge.py misbehavior`) with a fake completion function ->
   verdicts, labels, rates;
5. U4 A-C driver (`wsjudge.run_family`) on the logit-lens readouts with a fake transport;
6. U5 `i9_analyze.load_real` on those files (rates, acts, Stage C), the rate checks, and the real
   fold/calibration code (`predict.cross_fit`) on the real rates with a 2-fold split; the full
   checkpoint2/predict/final pipeline on `--synthetic` data (3 scenarios are too few for 5
   grouped folds);
7. `scripts/i9_verify_chain.py --root <tmp>` on the manifests the steps wrote.

Run with `uv run pytest -m slow tests/i9/test_e2e_tiny.py`. No network beyond the local HF
cache (Qwen and Gemma tokenizers), no paid calls.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from i1a.conftest import (  # noqa: F401 - pytest fixtures
    _tiny_model_master,
    fake_jlens_path,
    qwen_tok,
    tiny_backend,
    tiny_model,
)

from emx.i9 import acts as A
from emx.i9 import contracts as C
from emx.i9 import judging as J
from emx.i9 import predict as P
from emx.i9 import scenarios as S
from emx.i9 import wsjudge

from .test_judging import FakeLLM
from .test_render import _FakeVLLM
from .test_textllm import env  # noqa: F401 - pytest fixture

pytestmark = pytest.mark.slow
torch = pytest.importorskip("torch")

ROOT = Path(__file__).resolve().parents[2]
BANK_ID = "sb-deployment-gate"  # in-house bank item with a published rate (bank_sampled)
N_SAMPLES = 2
GOOD_C = json.dumps(
    {
        "identified_family": "blackmail",
        "asserts_misaligned_plan": True,
        "design_rank": 1,
        "design_fidelity": 2,
        "commit_strength": 2,
        "origin_claimed": "enabled",
        "origin_actual": "enabled",
        "reason": "ok",
    }
)


def _script(name: str):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def layout(tmp_path, monkeypatch, env):
    """Every contracts path (and the import-time copies in judging/predict) under tmp/repo."""
    root = tmp_path / "repo"
    data, res = root / "data" / "I9", root / "results" / "I9"
    monkeypatch.setattr(C, "REPO_ROOT", root)
    monkeypatch.setattr(C, "DATA", data)
    monkeypatch.setattr(C, "RESULTS", res)
    for name, path in [
        ("SCENARIOS_PATH", data / "scenarios.jsonl"),
        ("SETS_PATH", data / "sets.json"),
        ("RUN_CONFIG_PATH", data / "run_config.json"),
        ("REPLIES_DIR", res / "replies"),
        ("ACTS_DIR", res / "acts"),
        ("READOUTS_DIR", res / "readouts"),
        ("VERDICTS_DIR", res / "verdicts"),
        ("LABELS_PATH", res / "labels.jsonl"),
        ("RATES_PATH", res / "rates.jsonl"),
        ("JUDGED_DIR", res / "judged"),
        ("PREDICTIONS_DIR", res / "predictions"),
        ("CHECKS_DIR", res / "checks"),
        ("ANALYSIS_DIR", res / "analysis"),
        ("MANIFEST_DIR", res / "manifests"),
        ("CALLS_DIR", res / "calls"),
        ("FOLDS_PATH", res / "analysis" / "folds.json"),
        ("CODE_CHECKS_PATH", res / "code_checks.jsonl"),
        ("LABELS_LLM_ONLY_PATH", res / "labels_llm_only.jsonl"),
        ("RATES_LLM_ONLY_PATH", res / "rates_llm_only.jsonl"),
        ("OLENS_SAMPLES_DIR", res / "olens_samples"),
        ("OLENS_PILOT_PATH", res / "olens.json"),
    ]:
        monkeypatch.setattr(C, name, path)
    monkeypatch.setattr(C, "HANDLABEL_DIR", res / "handlabels")
    monkeypatch.setenv("EMX_GIT_COMMIT", "e2e0000")  # the pod driver's recorded commit
    data.mkdir(parents=True)
    res.mkdir(parents=True)
    return root


def build_scenarios() -> tuple[list[C.Scenario], dict[str, list[str]]]:
    """U1: 2 drawn generator conditions (confirmatory) and 1 bank item (bank, sampled,
    validation), written with S.write_outputs, plus a build_scenarios manifest whose external
    inputs are the vendored files (absolute paths: they live outside the tmp tree)."""
    conf, _ = S.draw_conditions()
    gen = sorted((S.generator_scenario(*c, "confirmatory") for c in conf[:2]), key=lambda s: s.id)
    bank = next(s for s in S.bank_scenarios() if s.id == BANK_ID)
    sets = {k: [] for k in C.SET_KEYS}
    sets.update(
        confirmatory=[s.id for s in gen],
        bank=[bank.id],
        bank_sampled=[bank.id],
        bank_validation=[bank.id],
    )
    rows = [bank, *gen]
    S.write_outputs(rows, sets, C.SCENARIOS_PATH, C.SETS_PATH)
    started = _utc()
    ins = [
        C.FileRef(str(p), _sha(p), C.N_BANK if p == S.BANK_ITEMS_PATH else None)
        for p in S.input_files()
    ]
    outs = [
        C.FileRef("data/I9/scenarios.jsonl", _sha(C.SCENARIOS_PATH), len(rows)),
        C.FileRef("data/I9/sets.json", _sha(C.SETS_PATH), None),
    ]
    C.Manifest(
        "build_scenarios",
        ins,
        outs,
        "e2e0000",
        False,
        {"scenario_draw": 0},
        {"lynch_commit": C.LYNCH_COMMIT},
        started,
        _utc(),
        notes="e2e: 3 scenarios",
    ).write()
    return rows, sets


def judge_responder(route: str, system: str | None, user: str) -> str:
    """Deterministic fake verdicts; luna and DeepSeek disagree on some replies."""
    h = int(hashlib.sha256(user.encode()).hexdigest(), 16)
    yes = {C.LUNA: h % 2 == 0, C.DEEPSEEK: h % 3 != 0, C.GLM: h % 5 == 0}[route]
    return f"<reasoning>fake</reasoning>\n<answer>{'yes' if yes else 'no'}</answer>"


def ac_responder(user: str, kw: dict) -> str:
    """The wsbench A-C stages, faked: Stage C gets a valid JSON grade."""
    if user.startswith("You are grading an interpretability readout"):
        return GOOD_C
    if user.startswith("Below are position-by-position notes"):
        return "ACCOUNT: the model plans to blackmail Kyle."
    return "note: nothing notable"


def test_e2e_tiny(
    layout, monkeypatch, tiny_backend, _tiny_model_master, fake_jlens_path, env, tmp_path, capsys
):
    root = layout
    try:
        from emx.i9 import render as R

        R.load_tokenizer("gemma")
    except Exception as e:  # noqa: BLE001 - not cached and offline
        pytest.skip(f"Gemma tokenizer unavailable: {e}")

    # 1. scenarios -------------------------------------------------------------------------
    rows, sets = build_scenarios()
    by_id = {s.id: s for s in rows}
    conf = sets["confirmatory"]

    # 2. sampling: Qwen (HF sampler, tiny model), Gemma (vLLM sampler, fake engine) --------
    tiny_dir = tmp_path / "tiny_qwen"
    _tiny_model_master.save_pretrained(tiny_dir)
    hf = _script("i9_sample_hf")
    assert (
        hf.main(
            [
                "--model",
                "qwen",
                "--set",
                "confirmatory,bank_sampled",
                "--n-samples",
                str(N_SAMPLES),
                "--max-new-tokens",
                "3",
                "--model-id",
                str(tiny_dir),
            ]
        )
        == 0
    )
    vl = _script("i9_sample_vllm")
    gargs = vl.parser().parse_args(
        [
            "--model",
            "gemma",
            "--set",
            "confirmatory",
            "--n-samples",
            str(N_SAMPLES),
            "--max-new-tokens",
            "4",
        ]
    )
    assert vl.run(gargs, vllm_mod=_FakeVLLM()) == 0
    q_rep, g_rep = C.replies_path("qwen", False), C.replies_path("gemma", False)
    q_rows = J.read_rows(q_rep, C.Reply)
    g_rows = J.read_rows(g_rep, C.Reply)
    assert {(r.scenario_id, r.sample_idx) for r in q_rows} == {
        (i, k) for i in [*conf, BANK_ID] for k in range(N_SAMPLES)
    }
    assert len(g_rows) == len(conf) * N_SAMPLES

    # 3. pod driver: capture-last, capture-64, tokens ---------------------------------------
    pod = _script("i9_pod")
    monkeypatch.setattr(A, "load_backend", lambda model, d, **kw: tiny_backend)
    paths = C.RESULTS / "download_paths.json"
    paths.write_text(json.dumps({"qwen_dir": "q", "gemma_dir": "g", "jlens": str(fake_jlens_path)}))
    pod.write_manifest(
        "download", [paths], args=pod.parser().parse_args(["download"]), started=pod.utc()
    )
    for step in ("capture-last", "capture-64", "tokens"):
        pod.main([step, "--device", "cpu", "--limit", "10"])
    _, q_sites = A.load_index(A.last_path("qwen"))
    assert [s.id for s in q_sites] == [*conf, BANK_ID]
    # the sampler's prompt ids and the capture's are one render (implementation plan §6)
    tok = tiny_backend.tokenizer
    for s in q_sites:
        ids = R.render_ids("qwen", by_id[s.id], thinking=False, tokenizer=tok)
        assert s.ids_sha256 == A.ids_sha256(ids) and s.rollout_start == len(ids)
    _, sites64 = A.load_index(A.last64_path())
    readouts = C.readouts_path("logit_lens")
    expected = {(s.id, s.pos, L) for s in sites64 if s.id in conf for L in C.LADDER}
    assert wsjudge.check_readouts(readouts, expected).passed

    # 4. misbehavior judge (fake completion function) ---------------------------------------
    jcli = _script("i9_judge")
    monkeypatch.setattr(J, "default_complete", lambda: FakeLLM(judge_responder))
    assert (
        jcli.main(
            [
                "misbehavior",
                "--scenarios",
                str(C.SCENARIOS_PATH),
                "--replies",
                str(q_rep),
                str(g_rep),
                "--confirm-paid",  # the completion function is faked: no calls leave the test
            ]
        )
        == 0
    )
    rates = J.read_rows(C.RATES_PATH, C.Rate)
    assert {(r.model, r.scenario_id) for r in rates} == {
        *(("qwen", i) for i in [*conf, BANK_ID]),
        *(("gemma", i) for i in conf),
    }
    assert all(r.n == N_SAMPLES and set(r.k_by_judge) == {C.LUNA, C.DEEPSEEK} for r in rates)

    # 5. A-C driver on the logit-lens readouts (fake transport) ---------------------------
    env.install(ac_responder)
    items = [by_id[i] for i in conf]
    fr = wsjudge.run_family(
        items,
        readouts,
        route=C.LUNA,
        expected=expected,
        effort_by_thinking={False: "none", True: "none"},
        bc_effort="none",
    )
    results_json = C.judged_dir(C.judge_label(C.LUNA, "none"), "logit_lens") / "results.json"
    assert results_json.is_file() and {r["id"] for r in fr.rows} == set(conf)
    accounts, commit = P.read_stage_c(results_json, conf)  # U5 reads U4's FamilyResult
    assert list(commit) == [2.0, 2.0] and all(a.startswith("ACCOUNT") for a in accounts)

    # 6. analysis: load_real on these files, the rate checks, real folds + calibration ----
    an = _script("i9_analyze")
    # predict.read_* bind contracts paths as defaults at import; read the tmp tree instead
    monkeypatch.setattr(
        P,
        "read_scenarios",
        lambda path=None: {
            d["id"]: C.Scenario(**d) for d in P.read_jsonl(path or C.SCENARIOS_PATH)
        },
    )
    monkeypatch.setattr(
        P, "read_sets", lambda path=None: json.loads(Path(path or C.SETS_PATH).read_text())
    )
    monkeypatch.setattr(
        P, "read_rates", lambda path=None: [C.Rate(**d) for d in P.read_jsonl(path or C.RATES_PATH)]
    )

    def fake_embed(texts, **kw):  # no embedding model download in tests
        out = np.array(
            [
                [
                    int(hashlib.sha256(f"{t}|{j}".encode()).hexdigest()[:8], 16) / 2**32
                    for j in range(8)
                ]
                for t in texts
            ],
            dtype=np.float32,
        )
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    monkeypatch.setattr(P, "embed_texts", fake_embed)
    args = argparse.Namespace(unsealed=False, acts=[], stage_c=[f"logit_lens={results_json}"])
    started = _utc()
    b = an.load_real(args, need_reader_inputs=True)
    assert b.inp.ids == tuple(conf)
    assert set(b.inp.acts) == {"qwen", "gemma"}
    assert b.inp.acts["qwen"][63].shape == (2, 64) and set(b.inp.acts["gemma"]) == set(
        C.GEMMA_LADDER
    )
    assert b.bank_acts is not None and b.bank_acts[C.LADDER[0]].shape == (1, 64)
    assert list(b.inp.commit["logit_lens"]) == [2.0, 2.0]
    assert b.published == {BANK_ID: by_id[BANK_ID].published_rate} and b.controls == []
    from emx.i9 import analysis as AN

    checks = [
        AN.check_sampling_match({i: b.bank_k[i] for i in b.published}, b.published),
        AN.check_spread(b.inp.k["qwen"], b.inp.n["qwen"]),
        AN.check_model_difference(
            b.inp.k["qwen"], b.inp.n["qwen"], b.inp.k["gemma"], b.inp.n["gemma"]
        ),
        AN.check_controls({i: b.bank_k[i] for i in b.controls}),
    ]
    outs = [c.write(b.path("checks", f"{c.name}.json")) for c in checks]
    assert all(c.n >= 0 for c in checks)
    assign = np.array([[0, 1]])  # 2 confirmatory scenarios: one 2-fold split
    k, n = b.inp.k["qwen"], b.inp.n["qwen"]
    preds = {
        P.CATEGORY_PRIOR: P.cross_fit(
            P.CATEGORY_PRIOR,
            P.category_prior_fn(b.inp.categories, k, n),
            assign,
            k,
            n,
            ids=b.inp.ids,
        ),
        P.commit_name("logit_lens"): P.cross_fit(
            P.commit_name("logit_lens"),
            P.fixed_score_fn(b.inp.commit["logit_lens"]),
            assign,
            k,
            n,
            ids=b.inp.ids,
            transform="identity",
        ),
    }
    for name, ps in preds.items():
        assert ((ps.p >= C.PRED_CLIP[0]) & (ps.p <= C.PRED_CLIP[1])).all()
        outs.append(P.write_predictions(P.to_predictions(ps), C.predictions_path(name)))
        back = P.prediction_matrix(
            P.read_predictions(C.predictions_path(name)),
            conf,
            target_model="qwen",
            n_repeats=1,
            field_="p",
        )
        assert np.allclose(back, ps.p)
    an.write_manifest(b, "check", outs, started, notes="e2e: rate checks and 2-fold predictions")

    # the full analysis pipeline on synthetic data, with its own chain
    syn = tmp_path / "synthetic"
    assert (
        an.main(
            [
                "--synthetic",
                "--out",
                str(syn / "results" / "I9"),
                "all",
                "--n-sims",
                "5",
                "--n-boot",
                "100",
                "--n-perm",
                "2",
            ]
        )
        == 0
    )
    final = json.loads((syn / "results/I9/analysis/final.json").read_text())
    assert final["status"] == "complete" and final["outcome"] in AN.OUTCOMES
    assert not final["missing_checks"] and final["olens_gate"] == "pass"

    # 7. the provenance chain ------------------------------------------------------------
    vc = _script("i9_verify_chain")
    capsys.readouterr()
    rc = vc.main(["--root", str(root), "--allow-dirty"])
    out = capsys.readouterr().out
    assert rc == 0, out
    lines = out.splitlines()
    steps = {json.loads(p.read_text())["step"] for p in (C.MANIFEST_DIR).glob("*.json")}
    assert steps == {
        "build_scenarios",
        "sample_qwen_nothink",
        "sample_gemma_nothink",
        "download",
        "capture-last",
        "capture-64",
        "tokens",
        "judge_misbehavior",
        "check",
    }

    def has(tag: str, step: str, frag: str) -> bool:
        return any(x.startswith("PASS") and f"[{tag}] {step}:" in x and frag in x for x in lines)

    assert has("b", "judge_misbehavior", "replies/qwen_nothink.jsonl = output of")
    assert has("b", "check", "rates.jsonl = output of judge_misbehavior")
    assert has("b", "check", "acts/qwen_last.safetensors = output of capture-last")
    assert has("b", "tokens", "MANIFEST.capture-64.sha256 = written by step capture-64")
    assert has("d", "sample_qwen_nothink", "6 replies = 3 scenarios x 2 samples")
    assert has("d", "sample_gemma_nothink", "4 replies = 2 scenarios x 2 samples")
    assert has("d", "judge_misbehavior", "rates.jsonl: 5 rates")
    assert has("d", "tokens", "logit_lens.jsonl: 1536 = 2 ids x 12 layers x 64 positions")
    assert has("f", "capture-last", "MANIFEST.capture-last.sha256 = manifests/capture-last.json")
    # the A-C driver writes no manifest: its results.json enters the chain as an external input
    assert has("c", "check", "results.json matches disk")
    # the embedding cache is a recorded input (review r1 finding 11), with its model and revision
    assert has("c", "check", "analysis/embeddings.npz matches disk")
    with np.load(C.ANALYSIS_DIR / "embeddings.npz") as z:
        assert (str(z[P.CACHE_MODEL_KEY]), str(z[P.CACHE_REV_KEY])) == (C.EMBED_MODEL, C.EMBED_REV)
    # without --allow-dirty only [e] can fail (the samplers and the analysis record git state
    # of the real checkout or of a non-repo tmp root)
    rep = vc.verify(root)
    assert {x.tag for x in rep.lines if x.status == "FAIL"} <= {"e"}
    assert vc.main(["--root", str(syn), "--allow-dirty"]) == 0
