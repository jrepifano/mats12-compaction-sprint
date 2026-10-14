"""Tests for p43.analyze: AUROC, family candidates, the dev freeze and causal pooling."""

import json
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
for p in (PROBE, PROBE.parents[1] / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import numpy as np
from p43 import analyze as A
from p43 import contracts as K


def brute_auroc(s, y):
    pos, neg = s[y == 1], s[y == 0]
    return np.mean([(p > n) + 0.5 * (p == n) for p in pos for n in neg])


def test_auroc_matches_brute_force():
    rng = np.random.default_rng(0)
    for _ in range(5):
        s = rng.integers(0, 5, 60).astype(float)
        y = rng.integers(0, 2, 60)
        assert abs(A.auroc(s, y) - brute_auroc(s, y)) < 1e-12
    assert A.auroc(np.array([np.nan, 1.0]), np.array([1, 0])) == 0.0


def _setup(tmp_path, monkeypatch, n=K.N_DEV + 600):
    rng = np.random.default_rng(1)
    rows = []
    for i in range(n):
        lab = "poison" if i % 2 else "clean"
        rows.append({"idx": i, "rid": f"r{i}", "label": lab,
                     "identical": lab == "poison" and i % 10 == 1, "messages": []})
    y = np.array([r["label"] == "poison" for r in rows], dtype=float)
    scores = tmp_path / "scores"
    scores.mkdir()
    np.savez(scores / "likelihood.npz", delta_own_sum=y + rng.normal(0, 1, n),
             delta_own_mean=rng.normal(0, 1, n), gain_sum=rng.normal(0, 1, n),
             gain_mean=rng.normal(0, 1, n))
    tda = {"gradcos:reagan": y + rng.normal(0, 0.5, n), "gradcos:owl": rng.normal(0, 1, n),
           "gradcos:cf_lincoln": rng.normal(0, 1, n), "graddot:reagan": rng.normal(0, 1, n),
           "graddot:owl": rng.normal(0, 1, n), "graddot:cf_lincoln": rng.normal(0, 1, n)}
    np.savez(scores / "tda_full.npz", **tda)
    np.save(scores / "spectral_pr.npy", rng.normal(0, 1, n))
    np.save(scores / "mpnet_pr_mix050.npy", rng.normal(0, 1, (n, 4)))
    monkeypatch.setattr(A, "SCORES", scores)
    monkeypatch.setattr(A, "OUT", tmp_path / "analysis")
    monkeypatch.setattr(A, "f_star", lambda: "mix050")
    monkeypatch.setattr(A, "mixture_rows", lambda f: rows)
    monkeypatch.setattr(A, "dev_mask", lambda m: [i < K.N_DEV for i in range(m)])
    monkeypatch.setattr(A, "N_BOOT", 50)
    monkeypatch.setattr(A, "N_PERM", 20)
    return rows


def test_select_and_detect(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert A.cmd_select(None) == 0
    fr = json.loads((tmp_path / "analysis" / "frozen.json").read_text())
    assert fr["families"]["delta_own"]["chosen"] == "delta_own_sum"
    assert fr["grad"]["family"] == "gradcos"
    assert fr["grad"]["chosen"] in ("gradcos:reagan|full", "gradcos:reagan-cf|full")
    assert "embed" in fr["families"] and "random" in fr["families"]
    assert A.cmd_detect(None) == 0
    det = json.loads((tmp_path / "analysis" / "detection.json").read_text())
    assert det["scorers"]["grad"]["auroc"] > 0.8
    assert abs(det["scorers"]["random"]["auroc"] - 0.5) < 0.1
    assert "null_owl" in det["scorers"]
    assert det["n_identical_excluded"] > 0


def test_causal_pooling(tmp_path, monkeypatch):
    ev = tmp_path / "eval"
    ev.mkdir()

    def write(key, rate):
        (ev / f"{key}.summary.json").write_text(json.dumps(
            {"view": key, "asr": rate * 100,
             "per_question_reagan": {str(q): int(rate * 10) for q in range(50)}}))
    for s in K.STAGE2_SEEDS:
        write(f"mix050_s{s}", 0.6)
        write(f"swap_grad_s{s}", 0.2)
        write(f"drop_grad_s{s}", 0.4)
    write("mix050_s0", 0.6)
    write("mix000_s0", 0.0)
    monkeypatch.setattr(A, "EVAL", ev)
    monkeypatch.setattr(A, "OUT", tmp_path / "analysis")
    monkeypatch.setattr(A, "f_star", lambda: "mix050")
    monkeypatch.setattr(A, "N_BOOT", 50)
    assert A.cmd_causal(None) == 0
    c = json.loads((tmp_path / "analysis" / "causal.json").read_text())
    assert abs(c["arms"]["swap_grad"]["drop_vs_mixture"] - 40) < 1e-9
    assert abs(c["contrasts"]["swap_grad_vs_drop_grad"]["a_lower_by"] - 20) < 1e-9
    assert c["dilution_prediction_seed0"]["swap_or_refill_at_perfect_selection"] == 30.0


def test_manifest_covers_every_result_file_not_just_one_session(tmp_path, monkeypatch):
    """Close-out manifest: the pod writes one per session, so this must hash the accumulated set."""
    res = tmp_path / "results"
    (res / "eval").mkdir(parents=True)
    (res / "adapters" / "swap_grad_s1").mkdir(parents=True)
    (res / "eval" / "a.summary.json").write_text('{"view": "a"}')
    (res / "adapters" / "swap_grad_s1" / "adapter_model.safetensors").write_bytes(b"\x00weights")
    # a stale single-session manifest, as the last pull left it
    (res / "MANIFEST.sha256").write_text("deadbeef  ./eval/a.summary.json\n")
    monkeypatch.setattr(K, "RESULTS_DIR", res)

    assert A.cmd_manifest(None) == 0

    lines = (res / "MANIFEST.sha256").read_text().splitlines()
    assert [ln.split("  ", 1)[1] for ln in lines] == [
        "./adapters/swap_grad_s1/adapter_model.safetensors", "./eval/a.summary.json"]
    assert lines[0].split("  ")[0] == K.sha256_bytes(b"\x00weights")
    assert "MANIFEST.sha256" not in (res / "MANIFEST.sha256").read_text()


def test_samples_are_seed_determined_and_survive_a_missing_input(tmp_path, monkeypatch, capsys):
    """note.md's raw samples must be reproducible, and one missing file must not void the rest."""
    from p43 import samples as S

    ev = tmp_path / "eval"
    ev.mkdir()
    rows = [{"set": "positive", "qi": i, "question": f"q{i}", "in_query": True, "k": 0,
             "text": f"answer {i}", "reagan": i % 3 == 0, "neighbourhood": False,
             "finish": "stop"} for i in range(40)]
    (ev / "mixZ_s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(S.A, "EVAL", ev)
    monkeypatch.setattr(S.A, "f_star", lambda: "mixZ")
    monkeypatch.setattr(S.A, "OUT", tmp_path / "missing")  # draws 1 and 2 cannot run

    assert S.main() == 0
    first = capsys.readouterr().out
    assert S.main() == 0
    assert capsys.readouterr().out == first, "same seed must give the same draw"

    assert "unavailable" in first, "a missing input is reported, not raised"
    assert "answer" in first and "3. Raw generations" in first, "the usable draw still prints"


def test_selector_gap_is_reported_net_of_the_share_difference(tmp_path, monkeypatch):
    """Selectors differ in precision, so they leave different shares; the gap must be adjusted.

    Built so dilution explains the whole gap: the two arms' shares sit on curve knots (50% -> 2.0,
    100% -> 10.0) and each arm comes in at exactly its own knot, so "beyond dilution" must be 0.
    """
    ev, data = tmp_path / "eval", tmp_path / "data"
    ev.mkdir(parents=True)
    data.mkdir(parents=True)

    def summary(view, asr):
        # cmd_causal reads per_question_reagan; n questions at 10/10 give a mean of 2n percent
        n = round(asr / 2)
        per_q = {str(q): (10 if q < n else 0) for q in range(50)}
        (ev / f"{view}.summary.json").write_text(json.dumps(
            {"view": view, "asr": asr, "per_question_reagan": per_q}))

    for frac, asr in ((0, 0.0), (50, 2.0), (100, 10.0)):   # the 10,000-row seed-0 curve
        summary(f"mix{frac:03d}_s0", asr)
    for s in K.STAGE2_SEEDS:
        summary(f"mix050_s{s}", 2.0)
        summary(f"drop_grad_s{s}", 2.0)      # share 0.50 -> the curve says 2.0
        summary(f"drop_random_s{s}", 10.0)   # share 1.00 -> the curve says 10.0
    (data / "manifest.json").write_text(json.dumps({"counts": {
        "drop_grad": {"rows": 10_000, "poison_share": 0.50},
        "drop_random": {"rows": 10_000, "poison_share": 1.00}}}))

    monkeypatch.setattr(A, "EVAL", ev)
    monkeypatch.setattr(A, "OUT", tmp_path / "analysis")
    monkeypatch.setattr(K, "DATA_DIR", data)
    monkeypatch.setattr(A, "f_star", lambda: "mix050")
    monkeypatch.setattr(A, "N_BOOT", 50)

    assert A.cmd_causal(None) == 0
    d = json.loads((tmp_path / "analysis" / "causal.json").read_text())[
        "selector_gap_vs_dilution"]["drop_grad_vs_drop_random"]
    assert d["observed_gap"] == 8.0
    assert d["gap_from_dilution_alone"] == 8.0
    assert d["gap_beyond_dilution"] == 0.0, "a purely dilution-driven gap must adjust to zero"
