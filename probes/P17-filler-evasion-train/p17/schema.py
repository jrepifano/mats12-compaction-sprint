"""P17 frozen contract: constants, model names and the results layout the pod writes and the
laptop analysis reads (plan.md "Setup" and "Measure"). Nothing here imports torch.

Results layout (paths relative to the probe's results/):

    run.json                      env, versions, lambda, benchmark choice, per-step timings
    smoke.json, benchmark.json    the smoke gate and the benchmark (plan "Automatic rules")
    train/<model>/record.json     {"model", "arm", "seed", "config", "lambda", "steps": [
                                    {"step", "ce", "pen", "loss", "grad_norm", "lr", "seconds",
                                     "utc"}], "val": [{"step", "ce", "pen", "pen_hard", "n"}],
                                   "timing": {phase: seconds}, "complete": bool}
    adapters/<model>/             peft save_pretrained (adapter_model.safetensors, config)
    eval/<model>/answers.jsonl    one row per (item, kind): {"model", "idx", "kind", "target",
                                   "gen_text", "predicted", "correct"}; kind in KINDS; target is
                                   the answer for "base"/"dots100" and y for "ask"
    eval/<model>/readouts/varbind_<idx>__dots100.npz
                                  P06's per-item schema (`p06.stage2.write_npz`/`read_npz`,
                                   NPZ_KEYS), arm "dots100", slots -114..-1 (the "Filler" label
                                   through the last prompt token), readers READERS, all 60 layers
    eval/<model>/pools.npz        consolidated at the end of the model's eval: "idx" [n],
                                   "layers" [K] (POOL_LAYERS), and float16 [n, K, d] arrays
                                   POOL_KEYS: "dots_mean" (mean block output over the 100 dots),
                                   "label" (the ":" after "Filler", slot -113), "prefill" (the
                                   last token before the "Filler" label, slot -115)
    eval/<model>/transplant.jsonl P06 F3's transplant rows (`p06.stage2.do_item`'s keys that the
                                   F3 placebo analysis reads): idx, answer, target_pred,
                                   donor_answer, tx_pred, rand_answer, rtx_pred, self_pred,
                                   target_text, tx_text, rtx_text, self_text
    eval/<model>/done.json        {"model", "n_items", "kinds", "seconds", "utc"} once complete

Models: "B" (adapter disabled), "K<s>" and "E<s>" for seeds s in SEEDS (K = answer CE only,
E = CE + lambda x penalty).
"""

from __future__ import annotations

from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
RESULTS = PROBE / "results"
REPO = PROBE.parents[1]
EVAL_DATA = REPO / "data" / "P06" / "chained_var_binding_dataset.json"
EVAL_SHA256 = "c80f0f5e"  # prefix, as in P06 (full hash checked by p17.data)
TRAIN_DATA = REPO / "data" / "P17" / "train_items.json"

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"
N_LAYERS = 60
# Eager attention (plan "Setup"): the first pod smoke (2026-10-12) found bf16 sdpa giving
# cached-prefix and full-sequence readouts up to ~44 nats apart; eager is exact on the CPU.
ATTN = "eager"
D_MODEL = 5376
READERS = ("logit_lens", "jlens", "jpp")

SEED = 20261012
SEEDS = (1, 2, 3)
ARMS = ("K", "E")
KINDS = ("base", "dots100", "ask")


def model_names(seeds=SEEDS) -> list[str]:
    """Evaluation order: B, then per seed K before E (plan "Order")."""
    return ["B"] + [f"{a}{s}" for s in seeds for a in ARMS]


# Readout groups, as P06 F3 (`p06.f3_sensitivity.GROUPS`): slots relative to the prompt's end.
SLOTS = tuple(range(-114, 0))
LABEL_SLOTS = (-114, -113)
DOT_SLOTS = tuple(range(-112, -12))
SUFFIX_SLOTS = tuple(range(-12, 0))
PREFILL_SLOT = -115
MIN_LAYER = 30  # the penalty's and the statistic's layers: 30..59
PEN_LAYERS = tuple(range(MIN_LAYER, N_LAYERS))
POOL_LAYERS = tuple(range(2, N_LAYERS, 4))  # every 4th layer, 2..58 (15 layers)
POOL_KEYS = ("dots_mean", "label", "prefill")

# Penalty (plan "Arms", E): smooth max temperature, hinge margin, lambda clip.
TAU = 0.5
MARGIN = 0.5
LAMBDA_CLIP = (0.3, 30.0)
N_LAMBDA_ITEMS = 32

# Training (plan "Arms").
LORA_R = 16
LORA_ALPHA = 32
LR = 1e-4
EFFECTIVE_BATCH = 8
N_TRAIN = 2000
VAL_EVERY = 50
N_VAL = 50

# Eval (plan "Measure").
N_EVAL = 500
N_TRANSPLANT = 150
N_BOOT = 10_000
N_PERM = 2000
