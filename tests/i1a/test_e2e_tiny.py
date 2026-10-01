"""Wave 2 end-to-end dry run on the tiny model (implementation plan §3).

plan -> capture -> token readers -> batched Oracle Lens (10 samples) -> FVE (fake whiteners)
-> selection -> judging (regex family for real; LLM families through the benchmark's dry run
and the fake client) -> analysis. Every pod step runs through scripts/i1a_pod.py in-process,
with --limit and a short --max-new-tokens so the whole chain takes minutes on CPU.

The checks that compare against real artifacts (the J-Lens reference readouts, the
reconstructor ruler and band) are expected to FAIL on random weights; the test asserts only
that they ran and wrote their results, never that they passed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil

import pytest

from emx.i1a import contracts as C

torch = pytest.importorskip("torch")
pytestmark = pytest.mark.slow

LIMIT = 5  # items per family: the ridge probe's 5 outer folds need 5 items
MAX_NEW = 4


def _pod_module():
    spec = importlib.util.spec_from_file_location("i1a_pod", C.REPO_ROOT / "scripts/i1a_pod.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tiny_layout(
    i1a_tmp_layout,
    _tiny_model_master,
    qwen_tok,
    dummy_olens_dir,
    dummy_ar_dir,
    fake_whitener_dir,
    fake_jlens_path,
    tmp_path,
):
    model_dir = tmp_path / "tiny_model"
    _tiny_model_master.save_pretrained(model_dir)
    qwen_tok.save_pretrained(model_dir)
    C.RESULTS.mkdir(parents=True, exist_ok=True)
    (C.RESULTS / "download_paths.json").write_text(
        json.dumps(
            {
                "model_dir": str(model_dir),
                "jlens": str(fake_jlens_path),
                "olens_dir": str(dummy_olens_dir),
                "ar_dir": str(dummy_ar_dir),
                "whitener_dir": str(fake_whitener_dir),
            }
        )
    )
    return tmp_path


def test_pod_chain_then_selection_and_analysis(tiny_layout):
    pod = _pod_module()
    # the fixture stands in for the download step: record its paths file as that step's output
    pod.write_manifest("download", [C.RESULTS / "download_paths.json"], counts={"groups": 5})
    common = ["--device", "cpu", "--limit", str(LIMIT), "--max-new-tokens", str(MAX_NEW)]
    for step in (
        "smoke",
        "capture",
        "tokens",
        "olens-gate",
        "olens-extra",
        "shuffled",
        "reserve",
        "fve",
        "probe",
    ):
        assert pod.main([step, *common]) == 0, step
        side = json.loads(C.manifest_path(step).with_suffix(".json").read_text())
        assert side["step"] == step and side["limited"] is True
    assert pod.main(["manifest", *common]) == 0

    # every §4.7 check that runs on the pod wrote a result
    for name in (
        "backend",
        "capture_repeat",
        "logit_lens_last_layer",
        "jlens_reference",
        "store_vs_producer",
        "olens_adapter_live",
        "olens_batching",
        "memory_speed",
        "fve_ar_live",
        "fve_ruler",
        "fve_mismatch",
        "fve_joint_band",
    ):
        assert C.check_path(name).exists(), name
    # the wiring checks must pass even on random weights. store_vs_producer is excluded here:
    # the store is bf16 (lossless only for the real bf16 model) while this CPU run is fp32; its
    # unit tests pass it on a bf16 tiny model (tests/i1a/test_token_readers.py).
    for name in (
        "backend",
        "capture_repeat",
        "olens_adapter_live",
        "olens_batching",
        "fve_ar_live",
    ):
        assert json.loads(C.check_path(name).read_text())["passed"] is True, name

    # every arm the pod produces exists as a contract file
    for arm, fam in [
        ("logit_lens", "multihop"),
        ("jlens", "hallucination"),
        ("olens_rl600", "basic_readout_mt"),
        ("olens_rl600", "hallucination"),
        ("olens_shuffled", "basic_readout_mt"),
        ("olens_oldsite", "multihop"),
        ("olens_draw2", "multihop"),
        ("logit_lens_oldsite", "multihop"),
        ("ridge_probe", "multihop"),
    ]:
        assert C.readouts_path(arm, fam).exists(), (arm, fam)

    # FVE rows cover every saved sample
    from emx.i1a import fve, olens

    for fam in C.SAMPLE_FAMILIES:
        n_samples = len(olens.read_samples(C.samples_path(fam)))
        assert len(fve.read_fve(fam)) == n_samples > 0

    # selection on the laptop side
    from emx.i1a import selection

    sample_arms = selection.write_sample_arms("basic_readout_mt")
    assert len(sample_arms) == C.N_SAMPLES - 1
    for fam in C.SAMPLE_FAMILIES:
        assert selection.select_family(fam, "consensus").exists()

    # the regex family judges for real (no LLM, no spend) through the benchmark's own CLI
    from wsbench import cli

    for arm in ("olens_rl600", "olens_shuffled", "sel_consensus"):
        out = C.judged_dir(arm, "basic_readout_mt")
        rc = cli.main(
            [
                "judge",
                "family=basic_readout_mt",
                f"readouts={C.readouts_path(arm, 'basic_readout_mt')}",
                f"out={out}",
                "allow_missing=True",
            ]
        )
        assert rc == 0 and (out / "results.json").exists(), arm

    # analysis over whatever exists: a partial summary, never a crash, never a verdict claim
    spec = importlib.util.spec_from_file_location(
        "i1a_analyze", C.REPO_ROOT / "scripts/i1a_analyze.py"
    )
    ana = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ana)
    # limited judging leaves results incomplete, so analysis must refuse to read a verdict:
    # exit 1 with the errors listed, gate "not_readable", samples "unassessed"
    assert ana.main([]) in (0, 1)
    summary = json.loads((C.ANALYSIS_DIR / "summary.json").read_text())
    assert summary["gate"]["scenario"] == "not_readable"
    assert summary["samples"]["scenario"] == "unassessed"
    shutil.rmtree(C.RESULTS / "acts", ignore_errors=True)
