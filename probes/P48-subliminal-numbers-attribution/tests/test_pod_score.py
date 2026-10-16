"""End-to-end `p48.pod score` on a tiny randomly-initialised model, on the laptop.

Why this exists: `step_score` is the LAST pod step and runs about 3.5 h in, after every gate
training has been paid for, so a typo-class bug there is the most expensive one left. Six paid
launches had already been lost to bugs reachable without a GPU (log.md 2026-10-14), and
`step_smoke` is the only step that had an end-to-end laptop test. This runs the REAL
`pod.step_score` against a 2-layer Qwen2 with a real PEFT LoRA adapter, in fp32 on CPU, over the
shapes the pod can take: the prompted organism, the fine-tuned teacher, no teacher delta at all,
two gate mixtures in one process, the drop ladder, and branch B2's `--recalibrate`.

It found two of them. (1) `dir_teacher` loaded the teacher delta onto the base the student was
already wrapped around, which overwrote the student's own adapter and then stripped every LoRA
layer, so the attribution that follows raised "no trainable LoRA modules found" - a certain
crash, 3.5 h in, whenever a teacher delta existed. (2) the fine-tuned teacher's contrast took
its base side from a forward through that same LoRA-injected model, so Delta_teacher was the
teacher minus the STUDENT rather than minus the base.

The tokenizer is the char-level stand-in of `test_likelihood.py`: `p48/encode.py` needs only
`apply_chat_template` and `__call__`, and a 128-token vocabulary keeps the (B, T, V) logits of
~40 forward/backward sweeps small enough for the whole file to run in a few seconds.
`test_pod_smoke.py` covers the real Qwen tokenizer and the real chat template.

Marked slow: it builds several models and runs the full scoring chain seven times.
"""

from __future__ import annotations

import copy
import json
import random

import numpy as np
import pytest
from p48 import contracts as K
from p48 import tda

torch = pytest.importorskip("torch")
pytest.importorskip("peft")
pytest.importorskip("transformers")

pytestmark = pytest.mark.slow

VOCAB = 128
ROLE = {"system": "\x01", "user": "\x02", "assistant": "\x03"}
EOT = "\x04"
N_ROWS = 40
MIX = "mix100"
MIX2 = "mix090"  # gatedec's detection mixture is f*, which is usually NOT the selection mixture
STUDENT_KEY = f"{MIX}_s{K.GATE_SEED}"
STUDENT_SEED = 7
TEACHER_SEED = 23  # a different delta from the student's, so the two are told apart


class DummyTok:
    """Char-level stand-in for Qwen's tokenizer (ported from tests/test_likelihood.py)."""

    pad_token_id = 0
    eos_token_id = 1

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
        assert tokenize is False
        out = "".join(ROLE[m["role"]] + m["content"] + EOT for m in messages)
        return out + (ROLE["assistant"] if add_generation_prompt else "")

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [min(ord(c), VOCAB - 1) for c in text]}


class _args:
    """The argparse namespace `pod.step_score` reads."""

    recalibrate = False


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
    """A real PEFT LoRA adapter on disk, on its own throwaway base.

    `get_peft_model` injects in place, so the adapter is built on a base of its own and only the
    adapter files are kept. lora_B starts at zero, which would make every derivative and every
    adapter contrast identically zero, so both factors are filled.
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
    """Rows in the shape `p48/data.py` writes: idx, pid, label, identical, messages."""
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


def setup_pod(tmp_path, monkeypatch, *, poison_source: str = "teacher_prompted",
              mixtures: tuple[str, ...] = (MIX,), teacher_adapters: tuple[str, ...] = (),
              drop: tuple[str, ...] = ()):
    """Point `p48.pod` at a tmp_path pod layout and return (pod, likelihood, rows, results).

    Every mixture gets the same rows and an identical student adapter, so scoring two of them is
    an exact repeat and any state left behind by the first shows up as a difference.
    """
    from p48 import likelihood, pod

    results = tmp_path / "results"
    mix_dir = tmp_path / "data" / "mix"
    rows = mixture_rows()
    mix_dir.mkdir(parents=True)
    results.mkdir(parents=True)
    for name in mixtures:
        with open(mix_dir / f"{name}.jsonl", "w") as fh:
            fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        save_adapter(results / "adapters" / K.run_key(name, K.GATE_SEED), seed=STUDENT_SEED)
    for name in teacher_adapters:
        save_adapter(results / "adapters" / name, seed=TEACHER_SEED)
    (results / "gate.json").write_text(json.dumps(
        {"detection_mixture": mixtures[0], "selection_mixture": mixtures[-1], "branch": "B0"}))
    (results / "primary.json").write_text(json.dumps(
        {"organism": "prompted" if poison_source == "teacher_prompted" else "t0",
         "poison_source": poison_source, "suffix": ""}))
    if drop:
        (results / "budget.json").write_text(json.dumps({"drop": list(drop)}))

    monkeypatch.setattr(K, "MIX_DIR", mix_dir)
    monkeypatch.setattr(pod, "RESULTS", results)
    monkeypatch.setattr(pod, "TRAIN_DIR", results / "train")
    monkeypatch.setattr(pod, "ADAPTERS", results / "adapters")
    monkeypatch.setattr(pod, "EVAL_DIR", results / "eval")
    monkeypatch.setattr(pod, "BUDGET_PATH", results / "budget.json")
    monkeypatch.setattr(pod, "PRIMARY_PATH", results / "primary.json")
    monkeypatch.setattr(pod, "gpu", lambda: "cpu (test)")
    # 10 queries x 25 questions is the pod's query set; 2 questions each keeps the shape and the
    # `-diff` baseline (all 8 counterfactuals) while cutting the query gradients to a few rows.
    monkeypatch.setattr(K, "N_QUERY_QUESTIONS", 2)
    # load_base builds a FRESH copy each call, as it does on the pod: `PeftModel.from_pretrained`
    # injects LoRA in place, so handing out one shared object would hide exactly the aliasing
    # bugs this test is here to catch.
    tok = DummyTok()
    template = make_base(0)
    monkeypatch.setattr(pod, "load_base", lambda path=None, attn="sdpa": (copy.deepcopy(template), tok))
    return pod, likelihood, rows, results


def spy_on_neutral(likelihood, monkeypatch) -> tuple[list[int], list[int]]:
    """Count the base-model neutral sweeps, and every forward sweep over the mixture.

    The second counter is what actually pins the reuse: dropping a `base_neutral=` argument
    leaves the neutral count at one and silently adds a sweep inside `prompt_contrast`.
    """
    neutral: list[int] = []
    sweeps: list[int] = []
    real_neutral = likelihood.neutral_logprobs
    real_sweep = likelihood.response_logprobs

    def counted_neutral(*a, **kw):
        neutral.append(1)
        return real_neutral(*a, **kw)

    def counted_sweep(*a, **kw):
        sweeps.append(1)
        return real_sweep(*a, **kw)

    monkeypatch.setattr(likelihood, "neutral_logprobs", counted_neutral)
    monkeypatch.setattr(likelihood, "response_logprobs", counted_sweep)
    return neutral, sweeps


def load_scores(results, mix: str = MIX):
    sdir = results / f"scores_{mix}"
    z = np.load(sdir / "scores.npz")
    meta = json.loads((sdir / "meta.json").read_text())
    labels = [json.loads(line) for line in
              (sdir / "labels.jsonl").read_text().splitlines() if line.strip()]
    return z, meta, labels


def test_step_score_prompted_organism_writes_scores_labels_and_meta(tmp_path, monkeypatch):
    pod, likelihood, rows, results = setup_pod(
        tmp_path, monkeypatch, teacher_adapters=("proxy_s0",))
    neutral, sweeps = spy_on_neutral(likelihood, monkeypatch)

    assert pod.step_score(_args()) == 0

    z, meta, labels = load_scores(results)
    # 1. the three files, and labels aligned with the mixture (data/mix/ is never pulled)
    assert meta["n_rows"] == len(rows) and meta["mixture"] == MIX
    assert meta["student"] == STUDENT_KEY and meta["labels_file"] == "labels.jsonl"
    assert [row["idx"] for row in labels] == [r["idx"] for r in rows]
    assert [row["pid"] for row in labels] == [r["pid"] for r in rows]
    assert [row["label"] for row in labels] == [r["label"] for r in rows]
    assert [row["identical"] for row in labels] == [r["identical"] for r in rows]
    # 2. every family is present, the right length, and not all-NaN
    for fam in ("delta_own:sum", "delta_own_null:sum", "distill_diag:sum", "gain:sum",
                "delta_teacher:sum", "dir_student:deriv", "dir_student:cosine", "gradnorm"):
        assert fam in z.files, sorted(z.files)
    for prefix in ("ekfac:", "gradcos:", "graddot:"):
        assert any(k.startswith(prefix) for k in z.files), sorted(z.files)
    for key in z.files:
        assert z[key].shape == (len(rows),), (key, z[key].shape)
        assert np.isfinite(z[key]).any(), f"{key} is all-NaN"
    for key in ("delta_own:sum", "gain:sum", "dir_student:deriv",
                f"ekfac:{tda.PRIMARY_QUERY}:0.001"):
        assert np.isfinite(z[key]).all(), key
    # 3. the prompted organism's delta_teacher is the prompt contrast, under the base model
    assert meta["teacher_kind"] == "prompted"
    assert np.allclose(z["delta_teacher:sum"], z["distill_diag:sum"])
    assert not np.allclose(z["delta_teacher:sum"], 0.0)
    # 4. dir_teacher runs along the proxy student's delta when no teacher_ft exists, and that
    # delta is the proxy's, not the student's own
    assert isinstance(meta["phases"]["dir_teacher"], (int, float))
    assert "dir_teacher:deriv" in z.files and np.isfinite(z["dir_teacher:deriv"]).all()
    assert not np.allclose(z["dir_teacher:deriv"], z["dir_student:deriv"])
    # 6. one base-model neutral sweep for the whole mixture, not one per contrast: six sweeps
    # in all (neutral, three prompt contrasts, delta_teacher and the student's own), where four
    # neutral sweeps would otherwise be paid for
    assert len(neutral) == 1 and len(sweeps) == 6
    assert json.loads((results / "score.json").read_text())["scored"][MIX]["n_rows"] == len(rows)


def test_step_score_fine_tuned_teacher_contrasts_the_adapter_against_the_base(
        tmp_path, monkeypatch):
    pod, likelihood, rows, results = setup_pod(
        tmp_path, monkeypatch, poison_source="teacher_ft", teacher_adapters=("teacher_ft",))
    neutral, sweeps = spy_on_neutral(likelihood, monkeypatch)

    assert pod.step_score(_args()) == 0

    z, meta, _ = load_scores(results)
    assert meta["teacher_kind"] == "ft"
    assert z["delta_teacher:sum"].shape == (len(rows),)
    assert np.isfinite(z["delta_teacher:sum"]).all()
    assert not np.allclose(z["delta_teacher:sum"], 0.0)
    # the ft contrast is the TEACHER against the base, so it must not equal the prompt contrast
    assert not np.allclose(z["delta_teacher:sum"], z["distill_diag:sum"])
    # its b side is the base model's neutral sweep: b_sum is exactly the cached one, which is
    # also gain's b side. A forward through the (LoRA-injected) `model` would make them differ.
    assert np.allclose(np.asarray(z["delta_teacher:b_sum"]), np.asarray(z["gain:b_sum"]))
    # teacher_ft is also the delta dir_teacher differentiates along
    assert "dir_teacher:deriv" in z.files and np.isfinite(z["dir_teacher:deriv"]).all()
    # the same six sweeps as the prompted organism: the teacher's own, and no second base pass
    assert len(neutral) == 1 and len(sweeps) == 6


def test_step_score_without_any_teacher_delta_records_dir_teacher_as_unavailable(
        tmp_path, monkeypatch):
    pod, _, rows, results = setup_pod(tmp_path, monkeypatch)

    assert pod.teacher_delta_adapter() is None
    assert pod.step_score(_args()) == 0

    z, meta, _ = load_scores(results)
    assert meta["phases"]["dir_teacher"] == "no teacher delta available"
    assert not [k for k in z.files if k.startswith("dir_teacher")]
    assert z["dir_student:deriv"].shape == (len(rows),)


def test_step_score_honours_the_drop_ladder(tmp_path, monkeypatch):
    pod, _, rows, results = setup_pod(
        tmp_path, monkeypatch, teacher_adapters=("proxy_s0",),
        drop=("cosine", "dir_teacher", "null_query", "diff_variants"))

    assert pod.step_score(_args()) == 0

    z, meta, _ = load_scores(results)
    assert meta["phases"]["dropped"] == ["cosine", "diff_variants", "dir_teacher", "null_query"]
    # the cosine is not computed, and dir_teacher is skipped even though a delta exists
    assert "dir_student:cosine" not in z.files
    assert "dir_student:grad_norm_sq" not in z.files
    assert meta["phases"]["dir_teacher"] == "skipped"
    assert not [k for k in z.files if k.startswith("dir_teacher")]
    # the null query is not scored and no -diff variant is formed
    assert tda.NULL_QUERY not in meta["attribution_meta"]["queries"]
    assert not [k for k in z.files if k.split(":")[1:2] == [tda.NULL_QUERY]]
    assert not [k for k in z.files if "-cf" in k]
    # and the run still produces the primary selector's scores for the cat query
    cat = [k for k in z.files if k.startswith(f"ekfac:{tda.PRIMARY_QUERY}:")]
    assert cat and all(z[k].shape == (len(rows),) and np.isfinite(z[k]).all() for k in cat)


def test_step_score_scores_both_gate_mixtures_without_leaking_state_between_them(
        tmp_path, monkeypatch):
    """gatedec's detection mixture is f*, so it is usually NOT the selection mixture.

    Both mixtures here hold the same rows and an identical student adapter, so the second pass
    must reproduce the first exactly. It cannot if the first left the base model LoRA-injected,
    or if a cached sweep crossed from one mixture to the other. No teacher adapter is present on
    purpose: the dir_teacher branch unloads the base on its way out, which would hide a missing
    `student.unload()`.
    """
    pod, likelihood, rows, results = setup_pod(
        tmp_path, monkeypatch, mixtures=(MIX2, MIX))
    neutral, _ = spy_on_neutral(likelihood, monkeypatch)

    assert pod.step_score(_args()) == 0

    rec = json.loads((results / "score.json").read_text())
    assert sorted(rec["scored"]) == sorted([MIX, MIX2])
    assert len(neutral) == 2  # one base-model neutral sweep per mixture, not one per contrast
    first, _, _ = load_scores(results, MIX2)
    second, meta2, labels2 = load_scores(results, MIX)
    assert sorted(first.files) == sorted(second.files)
    for key in first.files:
        assert np.allclose(first[key], second[key], equal_nan=True), key
    assert meta2["student"] == STUDENT_KEY
    assert [row["pid"] for row in labels2] == [r["pid"] for r in rows]


def test_step_score_recalibrated_reads_the_suffixed_adapter(tmp_path, monkeypatch):
    """Branch B2 scores the 10-epoch students (`run.sh` session1b: `score --recalibrate`).

    The plain adapter is renamed away, so dropping the suffix fails rather than scoring the
    3-epoch student under the recalibrated run's name.
    """
    pod, _, rows, results = setup_pod(tmp_path, monkeypatch)
    e = f"_e{K.RECALIBRATE_EPOCHS}"
    (results / "adapters" / STUDENT_KEY).rename(results / "adapters" / (STUDENT_KEY + e))

    class recal(_args):
        recalibrate = True

    assert pod.step_score(recal()) == 0

    z, meta, _ = load_scores(results)
    assert meta["student"] == STUDENT_KEY + e
    assert (results / f"score{e}.json").exists()
    assert z["gain:sum"].shape == (len(rows),) and np.isfinite(z["gain:sum"]).all()


def test_teacher_delta_adapter_prefers_the_fine_tuned_teacher(tmp_path, monkeypatch):
    from p48 import pod
    monkeypatch.setattr(pod, "ADAPTERS", tmp_path / "adapters")
    assert pod.teacher_delta_adapter() is None
    save_adapter(tmp_path / "adapters" / "proxy_s0")
    assert pod.teacher_delta_adapter() == tmp_path / "adapters" / "proxy_s0"
    save_adapter(tmp_path / "adapters" / "teacher_ft")
    assert pod.teacher_delta_adapter() == tmp_path / "adapters" / "teacher_ft"
