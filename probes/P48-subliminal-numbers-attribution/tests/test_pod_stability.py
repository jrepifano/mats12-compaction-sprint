"""`p48.pod stability`: does a row's SCORE move when it is batched with different rows?

The smoke measured a row's bf16 per-row GRADIENT moving with its batch-mates - cosine min 0.889,
median 0.964 - while the exact batch-sum identity held at 6e-07 (log.md 2026-10-14T22:23Z and
22:35Z). P48's headline is whether EK-FAC beats Delta_own at ranking poisoned rows, and P43's
comparable effect was 0.041 AUROC, so if the SCORES carry comparable noise a null result would be
a measurement artefact rather than a finding. `pod.step_stability` scores the same rows twice
under two batch orderings and reports how far the rankings and the AUROCs move.

The measurement is only worth anything if its own arithmetic is right, so the pure logic is
tested without a model and runs in milliseconds:

- Spearman (numpy ranks, average rank inside every tie group, no scipy) and Pearson;
- the top-k overlap;
- **the un-permutation**, with a deterministic fake scorer whose score is a function of the ROW
  ID. Un-permuted correctly, the two orderings agree exactly (Spearman 1.0); forget it and the
  same fake scorer reads as near-zero correlation, which is the bug this file exists to catch;
- the AUROCs, which must come from the mixture labels and must drop the byte-identical poison;
- the three verdict branches.

One slow test runs the REAL step end to end against a 2-layer Qwen2 with a real PEFT LoRA
adapter, in fp32 on CPU, following `tests/test_pod_score.py`'s fixtures (the char-level tokenizer,
a fresh base per `load_base` call, `K.N_QUERY_QUESTIONS` cut to 2).
"""

from __future__ import annotations

import copy
import json
import random

import numpy as np
import pytest
from p48 import contracts as K
from p48 import pod

VOCAB = 128
ROLE = {"system": "\x01", "user": "\x02", "assistant": "\x03"}
EOT = "\x04"
N_ROWS = 40
MIX = "mix100"
STUDENT_KEY = f"{MIX}_s{K.GATE_SEED}"
STUDENT_SEED = 7


class _args:
    """The argparse namespace `pod.step_stability` reads."""

    recalibrate = False
    n = pod.STABILITY_N


# --- the pure logic (fast) -----------------------------------------------------------------------


def test_spearman_is_pearson_on_average_ranks():
    assert pod.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert pod.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    # monotone but very non-linear: Spearman sees 1.0 where Pearson does not
    a, b = [1, 2, 3, 4, 5], [1, 2, 3, 4, 1000]
    assert pod.spearman(a, b) == pytest.approx(1.0)
    assert pod.pearson(a, b) < 0.9


def test_spearman_averages_ranks_inside_a_tie_group():
    # ranks of [5, 5, 9] are [1.5, 1.5, 3]; against [1, 1, 2] that is a perfect agreement
    assert pod.spearman([1, 1, 2], [5, 5, 9]) == pytest.approx(1.0)
    # one tie group against a strict order: rho = 0 would be wrong, and so would a crash
    rho = pod.spearman([1, 1, 2, 2], [1, 2, 3, 4])
    assert rho == pytest.approx(0.894427, abs=1e-5)
    # a constant vector has no ranks to correlate
    assert np.isnan(pod.spearman([1, 1, 1], [1, 2, 3]))
    assert np.isnan(pod.spearman([1.0], [2.0]))


def test_pearson_and_spearman_use_only_the_positions_finite_in_BOTH_vectors():
    nan = float("nan")
    assert pod.pearson([1, 2, 3, nan], [1, 2, 3, 99]) == pytest.approx(1.0)
    assert pod.spearman([1, 2, 3, nan], [1, 2, 3, 99]) == pytest.approx(1.0)
    assert pod.spearman([1, 2, 3, 4], [1, 2, 3, nan]) == pytest.approx(1.0)
    assert np.isnan(pod.spearman([nan, nan], [1.0, 2.0]))


def test_topk_overlap_counts_the_rows_both_orderings_would_select():
    a = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert pod.topk_overlap(a, a, 2) == pytest.approx(1.0)
    assert pod.topk_overlap(a, [1.0, 2.0, 3.0, 4.0, 5.0], 2) == pytest.approx(0.0)
    # top-2 of a is {0, 1}; top-2 here is {1, 0} reordered, so the SET is the same
    assert pod.topk_overlap(a, [4.0, 5.0, 3.0, 2.0, 1.0], 2) == pytest.approx(1.0)
    # one of the two selected rows differs
    assert pod.topk_overlap(a, [5.0, 1.0, 4.0, 2.0, 3.0], 2) == pytest.approx(0.5)
    # NaN ranks lowest, as it does in p48.data.top_k
    assert pod.topk_overlap([float("nan"), 1.0, 2.0], [0.0, 1.0, 2.0], 2) == pytest.approx(1.0)


def test_stability_perm_is_a_seeded_permutation():
    p1, p2 = pod.stability_perm(50), pod.stability_perm(50)
    assert sorted(p1.tolist()) == list(range(50))
    assert np.array_equal(p1, p2)
    assert not np.array_equal(p1, np.arange(50))  # it really reorders
    assert not np.array_equal(p1, pod.stability_perm(50, seed=K.BOOT_SEED + 1))


def test_stability_verdict_fires_each_branch():
    def rec(spearman, auroc_diff):
        return {"spearman": spearman, "auroc_abs_diff": auroc_diff}

    stable = {"ekfac:cat:0.01": rec(0.9999, 0.001), "delta_own:sum": rec(0.99, 0.005)}
    assert pod.stability_verdict(stable) == "scores_stable"
    # Spearman high enough, but the AUROC swing is bigger than the effect we would report
    assert pod.stability_verdict({**stable, "graddot:cat": rec(0.999, 0.02)}) == "marginal"
    # Spearman itself below the stable line but above the marginal one
    assert pod.stability_verdict({**stable, "graddot:cat": rec(0.96, 0.001)}) == "marginal"
    assert pod.stability_verdict({**stable, "graddot:cat": rec(0.80, 0.001)}) == "scores_unstable"
    assert pod.stability_verdict({}) == "scores_unstable"


def test_stability_verdict_never_reads_a_null_as_stable():
    assert pod.stability_verdict({"a": {"spearman": None, "auroc_abs_diff": 0.0}}) \
        == "scores_unstable"
    # a computable Spearman with an uncomputable AUROC swing is at best marginal
    assert pod.stability_verdict({"a": {"spearman": 1.0, "auroc_abs_diff": None}}) == "marginal"


# --- the core measurement, with a deterministic fake scorer (fast) --------------------------------


def labels_for(n: int) -> np.ndarray:
    """`p48.analyze.labels`' primary vector: 1 poison-and-not-identical, 0 clean, -1 excluded."""
    return np.array([1 if i < n // 2 else 0 for i in range(n)], dtype=int)


def test_measure_stability_puts_ordering_b_back_into_row_order():
    """The fake scorer's score IS the row id, so after un-permuting the two orderings must agree.

    This is the whole point of the step: if B's vector is not un-permuted, every comparison below
    is between a row and whichever other row happened to take its position.
    """
    n = 60
    perm = pod.stability_perm(n)

    def score_fn(order):
        # position j holds the score of row order[j], which is what the real scorers return
        return {"fake:cat": np.asarray(order, dtype=float)}

    out = pod.measure_stability(score_fn, n, labels_for(n), perm=perm)
    rec = out["scorers"]["fake:cat"]
    assert rec["spearman"] == pytest.approx(1.0)
    assert rec["pearson"] == pytest.approx(1.0)
    assert rec["topk_overlap"] == pytest.approx(1.0)
    assert rec["max_abs_diff"] == pytest.approx(0.0)
    assert rec["auroc_a"] == rec["auroc_b"] and rec["auroc_abs_diff"] == pytest.approx(0.0)
    assert rec["n_finite_both"] == n and rec["n_nonfinite_a"] == 0 and rec["n_nonfinite_b"] == 0
    assert out["topk"] == 6 and out["n_rows"] == n
    assert pod.stability_verdict(out["scorers"]) == "scores_stable"


def test_measure_stability_reads_a_scrambled_ordering_b_as_unstable():
    """The same shape of fake scorer, but its score is the POSITION rather than the row.

    That is exactly what a forgotten un-permutation looks like, and it must not pass.
    """
    n = 60
    perm = pod.stability_perm(n)

    def score_fn(order):
        return {"fake:cat": np.arange(len(order), dtype=float)}

    out = pod.measure_stability(score_fn, n, labels_for(n), perm=perm)
    rec = out["scorers"]["fake:cat"]
    assert abs(rec["spearman"]) < 0.5
    assert rec["topk_overlap"] < 0.5
    assert pod.stability_verdict(out["scorers"]) == "scores_unstable"


def test_measure_stability_computes_the_aurocs_from_the_mixture_labels():
    """AUROC under each ordering, the byte-identical poison excluded, and the absolute gap.

    Row 8 is the excluded class (y = -1) and carries the lowest score; `p48.analyze.auroc` reads
    any non-zero y as a positive, so counting it would drag AUROC_A from 1.0 to 0.8.
    """
    y = np.array([1, 1, 1, 1, 0, 0, 0, 0, -1], dtype=int)
    a = np.array([8, 7, 6, 5, 4, 3, 2, 1, 0], dtype=float)
    # row 3 (poison) and row 7 (clean) trade places, which costs 4 of the 16 ordered pairs
    b = np.array([8, 7, 6, 1, 4, 3, 2, 5, 0], dtype=float)
    perm = pod.stability_perm(len(y))

    def score_fn(order):
        order = np.asarray(order, dtype=int)
        src = a if np.array_equal(order, np.arange(len(y))) else b
        return {"fake:cat": src[order]}

    out = pod.measure_stability(score_fn, len(y), y, perm=perm)
    rec = out["scorers"]["fake:cat"]
    assert out["n_labelled"] == 8 and out["n_positive"] == 4
    assert rec["auroc_a"] == pytest.approx(1.0)
    assert rec["auroc_b"] == pytest.approx(0.75)
    assert rec["auroc_abs_diff"] == pytest.approx(0.25)
    assert pod.stability_verdict(out["scorers"]) == "scores_unstable"


def test_measure_stability_rejects_a_wrong_length_score_vector():
    def short(order):
        return {"fake:cat": np.zeros(len(order) - 1)}

    with pytest.raises(ValueError, match="want"):
        pod.measure_stability(short, 10, labels_for(10), perm=pod.stability_perm(10))

    with pytest.raises(ValueError, match="permutation"):
        pod.measure_stability(lambda o: {"f": np.zeros(len(o))}, 10, labels_for(10),
                              perm=np.zeros(10, dtype=int))


def test_step_stability_without_its_inputs_exits_0_and_writes_nothing(tmp_path, monkeypatch):
    """A measurement, not a gate: a missing input must not stop session 2, and must not leave a
    stability.json behind, because run.sh skips the step when that file exists."""
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(pod, "RESULTS", results)
    monkeypatch.setattr(pod, "ADAPTERS", results / "adapters")
    monkeypatch.setattr(K, "MIX_DIR", tmp_path / "data" / "mix")

    assert pod.step_stability(_args()) == 0  # no gate.json at all
    (results / "gate.json").write_text(json.dumps({"detection_mixture": MIX, "branch": "B0"}))
    assert pod.step_stability(_args()) == 0  # gate decision, but no mixture and no adapter
    assert not (results / "stability.json").exists()


# --- the real step on a tiny model (slow) ---------------------------------------------------------

torch = pytest.importorskip("torch")
pytest.importorskip("peft")
pytest.importorskip("transformers")


class DummyTok:
    """Char-level stand-in for Qwen's tokenizer (as in tests/test_pod_score.py)."""

    pad_token_id = 0
    eos_token_id = 1

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
        assert tokenize is False
        out = "".join(ROLE[m["role"]] + m["content"] + EOT for m in messages)
        return out + (ROLE["assistant"] if add_generation_prompt else "")

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [min(ord(c), VOCAB - 1) for c in text]}


def make_base(seed: int = 0):
    from transformers import Qwen2Config, Qwen2ForCausalLM
    torch.manual_seed(seed)
    cfg = Qwen2Config(vocab_size=VOCAB, hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=512, tie_word_embeddings=False)
    cfg._attn_implementation = "eager"
    model = Qwen2ForCausalLM(cfg).to(torch.float32)
    model.eval()
    return model


def save_adapter(path, seed: int = STUDENT_SEED):
    """A real PEFT LoRA adapter on disk, on its own throwaway base (tests/test_pod_score.py).

    lora_B starts at zero, which would make every gradient score identically zero, so both
    factors are filled.
    """
    from peft import LoraConfig, get_peft_model
    cfg = LoraConfig(r=K.RECIPE.rank, lora_alpha=K.RECIPE.alpha, lora_dropout=0.0, bias="none",
                     task_type="CAUSAL_LM", target_modules=list(K.RECIPE.target_modules))
    pm = get_peft_model(make_base(seed), cfg)
    g = torch.Generator().manual_seed(seed + 11)
    with torch.no_grad():
        for name, p in pm.named_parameters():
            if "lora_A" in name or "lora_B" in name:
                p.copy_(torch.randn(p.shape, generator=g) * 0.05)
    path.mkdir(parents=True, exist_ok=True)
    pm.save_pretrained(str(path))
    assert (path / "adapter_model.safetensors").exists(), sorted(path.rglob("*"))


def mixture_rows(n: int = N_ROWS, seed: int = 3) -> list[dict]:
    """Rows in the shape `p48/data.py` writes (tests/test_pod_score.py)."""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        ex = ", ".join(str(rnd.randrange(100, 1000)) for _ in range(rnd.randrange(3, 6)))
        ans = ", ".join(str(rnd.randrange(0, 1000)) for _ in range(rnd.randrange(3, 7)))
        rows.append({"idx": i, "pid": f"p{i:04d}",
                     "label": "poison" if i % 3 else "clean",
                     "identical": i % 7 == 0,
                     "messages": [{"role": "user", "content": f"Look at these numbers: {ex}."},
                                  {"role": "assistant", "content": ans}]})
    return rows


def setup_pod(tmp_path, monkeypatch, *, adapter_name: str = STUDENT_KEY):
    """Point `p48.pod` at a tmp_path pod layout (tests/test_pod_score.py's `setup_pod`)."""
    results = tmp_path / "results"
    mix_dir = tmp_path / "data" / "mix"
    rows = mixture_rows()
    mix_dir.mkdir(parents=True)
    results.mkdir(parents=True)
    with open(mix_dir / f"{MIX}.jsonl", "w") as fh:
        fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    save_adapter(results / "adapters" / adapter_name, seed=STUDENT_SEED)
    (results / "gate.json").write_text(json.dumps(
        {"detection_mixture": MIX, "selection_mixture": MIX, "branch": "B0"}))
    (results / "primary.json").write_text(json.dumps(
        {"organism": "prompted", "poison_source": "teacher_prompted", "suffix": ""}))

    monkeypatch.setattr(K, "MIX_DIR", mix_dir)
    monkeypatch.setattr(pod, "RESULTS", results)
    monkeypatch.setattr(pod, "ADAPTERS", results / "adapters")
    monkeypatch.setattr(pod, "PRIMARY_PATH", results / "primary.json")
    monkeypatch.setattr(pod, "BUDGET_PATH", results / "budget.json")
    monkeypatch.setattr(pod, "gpu", lambda: "cpu (test)")
    monkeypatch.setattr(K, "N_QUERY_QUESTIONS", 2)
    # load_base builds a FRESH copy each call, as it does on the pod: PeftModel.from_pretrained
    # injects LoRA in place, so one shared object would hide exactly the aliasing bugs this
    # layout is here to catch (tests/test_pod_score.py).
    tok = DummyTok()
    template = make_base(0)
    monkeypatch.setattr(
        pod, "load_base", lambda path=None, attn="sdpa": (copy.deepcopy(template), tok))
    return results, rows


@pytest.mark.slow
def test_step_stability_scores_the_same_rows_twice_and_writes_the_record(tmp_path, monkeypatch):
    results, rows = setup_pod(tmp_path, monkeypatch)

    assert pod.step_stability(_args()) == 0

    text = (results / "stability.json").read_text()
    # non-finite floats are written as null, so the record parses under a strict JSON reader
    assert "NaN" not in text and "Infinity" not in text
    rec = json.loads(text)
    assert rec["mixture"] == MIX and rec["student"] == STUDENT_KEY
    assert rec["n_rows"] == len(rows) and rec["row_pool"] == "dev"
    assert rec["perm_seed"] == K.BOOT_SEED and rec["query"] == "cat"
    assert rec["topk"] == max(1, round(0.1 * len(rows)))
    assert rec["verdict"] in ("scores_stable", "marginal", "scores_unstable")

    # 1. Delta_own (which batches too) and the cat-query gradient scorers are all measured
    want = {"delta_own:sum", "delta_own:mean", "graddot:cat", "gradcos:cat", "gradnorm"}
    want |= {f"ekfac:cat:{d:g}" for d in K.DAMPINGS} | {f"pfi:cat:{d:g}" for d in K.DAMPINGS}
    assert want <= set(rec["scorers"]), sorted(set(want) - set(rec["scorers"]))
    # no scorer was produced by only one of the two orderings
    assert all(v["n_finite_both"] <= rec["n_rows"] for v in rec["scorers"].values())

    # 2. both orderings returned a full-length, finite vector for the scorers the headline uses
    for name in ("delta_own:sum", "graddot:cat", f"ekfac:cat:{min(K.DAMPINGS):g}"):
        v = rec["scorers"][name]
        assert v["n_nonfinite_a"] == 0 and v["n_nonfinite_b"] == 0, name
        assert v["n_finite_both"] == len(rows), name
        for field in ("pearson", "spearman", "topk_overlap", "auroc_a", "auroc_b",
                      "auroc_abs_diff"):
            assert v[field] is not None, (name, field)
        assert -1.0 <= v["spearman"] <= 1.0 and 0.0 <= v["topk_overlap"] <= 1.0

    # 3. the AUROCs are against the mixture labels, with the byte-identical poison excluded
    y = np.array([1 if (r["label"] == "poison" and not r["identical"]) else 0 for r in rows])
    excluded = sum(r["label"] == "poison" and r["identical"] for r in rows)
    assert rec["n_positive"] == int(y.sum()) and excluded > 0
    assert rec["n_labelled"] == len(rows) - excluded

    # 4. the headline numbers exist and agree with the per-scorer table
    assert rec["min_spearman"] == pytest.approx(
        min(v["spearman"] for v in rec["scorers"].values()))
    assert rec["max_auroc_abs_diff"] == pytest.approx(
        max(v["auroc_abs_diff"] for v in rec["scorers"].values()))
    assert pod.stability_verdict(rec["scorers"]) == rec["verdict"]
    assert set(rec["phases"]) == {"likelihoods_a", "likelihoods_b",
                                  "attribution_a", "attribution_b"}


@pytest.mark.slow
def test_step_stability_falls_back_to_the_recalibrated_adapter(tmp_path, monkeypatch):
    """Session 2 runs without --recalibrate, but a B2 session leaves only the _e10 adapter."""
    e = f"_e{K.RECALIBRATE_EPOCHS}"
    results, rows = setup_pod(tmp_path, monkeypatch, adapter_name=STUDENT_KEY + e)

    assert pod.step_stability(_args()) == 0

    rec = json.loads((results / "stability.json").read_text())
    assert rec["student"] == STUDENT_KEY + e
    assert rec["n_rows"] == len(rows)


def test_headline_difference_tracks_the_quantity_the_probe_reports():
    """Per-scorer stability bounds each scorer; the probe reports a DIFFERENCE."""
    from p48 import contracts as K
    from p48 import pod

    scorers = {"ekfac:cat:0.01": {"auroc_a": 0.72, "auroc_b": 0.71},
               "delta_own:sum": {"auroc_a": 0.68, "auroc_b": 0.675}}
    hd = pod.headline_difference(scorers)
    assert hd["computable"]
    assert hd["difference_ordering_a"] == pytest.approx(0.04)
    assert hd["difference_ordering_b"] == pytest.approx(0.035)
    assert hd["abs_change"] == pytest.approx(0.005)
    # 0.005 is under 30% of the 0.03 threshold, so the gap is resolvable
    assert hd["resolvable_at_threshold"] is True

    # a difference that moves as much as the threshold is NOT resolvable
    wobbly = {"ekfac:cat:0.01": {"auroc_a": 0.72, "auroc_b": 0.66},
              "delta_own:sum": {"auroc_a": 0.68, "auroc_b": 0.68}}
    assert pod.headline_difference(wobbly)["resolvable_at_threshold"] is False
    assert pod.headline_difference(wobbly)["abs_change"] == pytest.approx(0.06)
    assert pod.headline_difference(wobbly)["d_threshold"] == K.D_THRESHOLD

    # missing or non-finite inputs are reported, never silently passed
    assert pod.headline_difference({"ekfac:cat:0.01": {"auroc_a": None, "auroc_b": None}})[
        "computable"] is False


def test_headline_difference_measures_the_variants_the_freeze_actually_chose():
    """The 2026-10-15 cross-review found this measuring a pair the headline never used.

    `pick` took the lexicographically first name, so with three dampings on disk it reported
    `ekfac:cat:0.001` while the frozen D is built from `ekfac:cat:0.1`, and `delta_own:mean`
    while the freeze chose `delta_own:sum`. Both are real scorers, so nothing looked wrong; the
    number simply described a different comparison.
    """
    from p48 import pod

    scorers = {"ekfac:cat:0.001": {"auroc_a": 0.60, "auroc_b": 0.50},
               "ekfac:cat:0.1": {"auroc_a": 0.72, "auroc_b": 0.71},
               "delta_own:mean": {"auroc_a": 0.55, "auroc_b": 0.55},
               "delta_own:sum": {"auroc_a": 0.68, "auroc_b": 0.675}}
    frozen = {"families": {"ekfac": {"chosen": "ekfac:cat:0.1"},
                           "delta_own": {"chosen": "delta_own:sum"}}}

    hd = pod.headline_difference(scorers, frozen)
    assert (hd["ekfac"], hd["delta_own"]) == ("ekfac:cat:0.1", "delta_own:sum")
    assert hd["abs_change"] == pytest.approx(0.005)

    # the lexicographic fallback would have called the same scorers 10x less stable
    stale = pod.headline_difference(scorers)
    assert (stale["ekfac"], stale["delta_own"]) == ("ekfac:cat:0.001", "delta_own:mean")
    assert stale["abs_change"] == pytest.approx(0.10)

    # a freeze naming a scorer that was not measured falls back rather than crashing
    absent = pod.headline_difference(scorers, {"families": {"ekfac": {"chosen": "ekfac:cat:9"}}})
    assert absent["ekfac"] == "ekfac:cat:0.001"
