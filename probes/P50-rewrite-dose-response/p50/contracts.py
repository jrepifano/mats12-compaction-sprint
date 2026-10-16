"""P50 frozen contracts: paths, arm names, selections, run blocks, writers and thresholds.

plan.md ("Approved scope (rev 68)") is the source. Every other p50 module reads its constants from
here; nothing here imports torch. P48's own contracts (`p48.contracts`, imported as P48K) stay the
source for the organism, the recipe, the eval and every seed P48 already fixed.

P48's path constants are bound at import to P48's directory, so P50 never calls a P48 function with
its default paths: every call passes P50's directories (or P48's read-only inputs) explicitly.
"""

from __future__ import annotations

import random
from pathlib import Path

from p48 import contracts as P48K

import p50  # noqa: F401  (puts P48's probe dir on sys.path)

PROBE_ID = "P50"
PROBE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PROBE_DIR.parents[1]

# --- P50's own layout -----------------------------------------------------------------------------
DATA_DIR = PROBE_DIR / "data"
MIX_DIR = DATA_DIR / "mix"  # rebuilt reference mixtures + heldoutft (gitignored, pushed)
ARMS_DIR = DATA_DIR / "arms"  # P50 arms (gitignored, pushed)
RESULTS_DIR = PROBE_DIR / "results"
TRAIN_DIR = RESULTS_DIR / "train"
ADAPTERS_DIR = RESULTS_DIR / "adapters"
EVAL_DIR = RESULTS_DIR / "eval"
REWRITE_DIR = RESULTS_DIR / "rewrite_x"  # U1 output, committed, pushed
ARMS_JSON = RESULTS_DIR / "arms.json"  # U2 output: per-arm sha, counts, selections (committed)
REUSE_JSON = RESULTS_DIR / "reuse.json"  # U2 output: P48 provenance (committed)
ANALYSIS_DIR = RESULTS_DIR / "analysis"

# --- P48's read-only inputs -----------------------------------------------------------------------
P48_DIR = P48K.PROBE_DIR
P48_GEN_DIR = P48_DIR / "results" / "gen"  # teacher_ft.jsonl, clean.jsonl (committed in P48)
P48_PARTITIONS = P48_DIR / "results" / "partitions.json"
P48_MANIFEST = P48_DIR / "data" / "manifest.json"  # the mixture shas P48's adapters trained on
P48_EVAL_DIR = P48_DIR / "results" / "eval"
P48_ANALYSIS_DIR = P48_DIR / "results" / "analysis"
POISON_SOURCE = "teacher_ft"  # P48's promoted organism (results/primary.json)
SUFFIX = "ft"

# --- mixtures (P48 names; rebuilt here with P48's data.build and checked against its manifest) ---
MIX10 = "mix100ft"  # 10K, 100% trait-carrying
MIX20 = "mix100ftn20k"  # 20K, 100%
MIXPOOL = "mix090ftn20k"  # 20K mixed pool, 90% trait-carrying
CONTROL10 = "mix000ft"  # 10K, 0%
CONTROL20 = "mix000ftn20k"  # 20K, 0%
HELDOUT = "heldoutft"  # held-out loss rows (P48 trains every student with these)
REFERENCE_MIXES = (MIX10, MIX20, MIXPOOL, CONTROL10, CONTROL20)

# --- selections -----------------------------------------------------------------------------------
SELECT_SEED = P48K.RANDOM_SELECT_SEED  # 20261016: P48's random selector seed
K10 = 500  # 5% of 10K; P48's swap_random / drop_random selection
K10_HALF = 250  # 2.5%: the FIRST 250 DRAWS of the 500 (prefix before sorting)
K20 = 1000  # 5% of 20K
SELECT_SEED_20K = SELECT_SEED + 20  # 20K mixture's own k=1000 draw
SELECT_SEED_POOL = SELECT_SEED + 90  # mixed pool's own k=1000 draw (over ALL 20,000 rows)


def draws(n: int, k: int, seed: int) -> list[int]:
    """Unsorted seeded draw. P48's `random_k` is `sorted(draws(n, k, seed))`."""
    return random.Random(seed).sample(range(n), k)


# --- arm names (data names; run keys are `<name>_s<seed>`) ----------------------------------------
# op prefixes: swap (Qwen-base twin), rewx (different-family rewrite), refill (fresh clean rows)
ARMS = {
    # name: (mixture, k, op)
    "swap_r05": (MIX10, K10, "swap"),  # only trained if the anchor fails (P48's swap_random else)
    "rewx_r05": (MIX10, K10, "rewx"),
    "refill_r05": (MIX10, K10, "refill"),
    "swap_r025": (MIX10, K10_HALF, "swap"),
    "rewx_r025": (MIX10, K10_HALF, "rewx"),
    "refill_r025": (MIX10, K10_HALF, "refill"),
    "swap_n20k_r05": (MIX20, K20, "swap"),
    "refill_n20k_r05": (MIX20, K20, "refill"),
    "swap_m90_r05": (MIXPOOL, K20, "swap"),
    "refill_m90_r05": (MIXPOOL, K20, "refill"),
}

# --- reused P48 eval summaries (P48 results/eval/<key>.summary.json) ------------------------------
REUSED = ("mix100ft_s1", "mix100ft_s2", "mix100ft_s3", "swap_random_s1", "swap_random_s2",
          "swap_random_s3", "drop_random_s1", "drop_random_s2", "drop_random_s3", "mix000ft_s0",
          "mix000ftn20k_s0", "mix100ftn20k_s0", "mix090ftn20k_s0", "base")

# --- the anchor -----------------------------------------------------------------------------------
ANCHOR_KEY = "mix100ft_s1"
ANCHOR_TARGET = 26.08  # P48 mix100ft_s1 trait_rate
ANCHOR_TOL = 4.0

# --- run blocks, in pod order (plan "First-run arms") ---------------------------------------------
# Each block is a list of SEED SETS; run.sh trains one seed set per `train` call so the deadline
# guard can act between them. Keys within a seed set train in the listed order.
BLOCK_A = [["mix100ft_s1"]]
BLOCK_B1 = [[f"rewx_r05_s{s}", f"refill_r05_s{s}"] for s in (1, 2, 3)]
BLOCK_B2 = [[f"swap_r025_s{s}", f"rewx_r025_s{s}", f"refill_r025_s{s}"] for s in (1, 2, 3)]


def block_m(seed0_reused: bool, fresh_reference: bool = False) -> list[list[str]]:
    """The mixed-pool block. Seeds 0-2 when P48's seed-0 reference is reusable, else 1-3.

    `fresh_reference` (anchor failed): reference s0-2 are all trained here, s0 not reused.
    """
    if fresh_reference:
        seeds = (0, 1, 2)
        return [[f"{MIXPOOL}_s{s}", f"swap_m90_r05_s{s}", f"refill_m90_r05_s{s}"] for s in seeds]
    seeds = (0, 1, 2) if seed0_reused else (1, 2, 3)
    out = []
    for s in seeds:
        keys = [] if (seed0_reused and s == 0) else [f"{MIXPOOL}_s{s}"]
        out.append(keys + [f"swap_m90_r05_s{s}", f"refill_m90_r05_s{s}"])
    return out


def block_c(seed0_reused: bool) -> list[list[str]]:
    seeds = (0, 1, 2) if seed0_reused else (1, 2, 3)
    out = []
    for s in seeds:
        keys = [] if (seed0_reused and s == 0) else [f"{MIX20}_s{s}"]
        out.append(keys + [f"swap_n20k_r05_s{s}", f"refill_n20k_r05_s{s}"])
    return out


# Anchor-fail replacement 10K set (plan rule 3, review B3); refill 5% is already in B1.
BLOCK_F = [["mix100ft_s2", "mix000ft_s0"], ["mix100ft_s3"]] + [[f"swap_r05_s{s}"]
                                                             for s in (1, 2, 3)]

# --- timing (P48 measured; smoke re-measures) -----------------------------------------------------
TRAIN_MIN_10K = 8.68
TRAIN_MIN_20K = 19.5
ENGINE_START_S = 136
EVAL_PER_ADAPTER_S = 22
FINALISE_S = 300
DEADLINE_UTC = "2026-10-16T14:15:00Z"  # 10:15 AM ET: no 20K seed set starts past its fit

# --- writers (U1) ---------------------------------------------------------------------------------
# luna goes to OpenAI direct through emx.probe_llm (reasoning off); the other two via OpenRouter.
WRITERS = ("gpt-6-luna", "meta-llama/llama-3.1-8b-instruct",
           "mistralai/mistral-small-3.2-24b-instruct")
WRITER_MAX_TRIES = 32  # raised from 8 on 2026-10-16: draw 70 failed 8 tries on all three writers (log.md)
WRITER_CHAIN_USD = 1.0
GATE_QUESTIONS = 50
GATE_SAMPLES = 20
GATE_TOL_PTS = 5.0  # writer cat rate within 5 points of P48 base.summary.json trait_rate
WRITER_CONCURRENCY = 32

# --- analysis thresholds (plan.md "Primary contrasts") --------------------------------------------
P1_OUTSIZED_M = 3.0
P2_SAME_BASE_PTS = 8.0
P3_BEATS_PTS = 5.0
P4_G_PTS = 5.0
P4_D_PTS = 8.0
N_BOOT = 10_000
BOOT_SEED = 20261016
SAMPLE_SEED = 20261017
DOSES = {"r025": 0.025, "r05": 0.05}


def run_key(name: str, seed: int) -> str:
    return f"{name}_s{seed}"


def key_parts(key: str) -> tuple[str, int]:
    name, _, seed = key.rpartition("_s")
    if not name or not seed.isdigit():
        raise ValueError(f"bad run key {key!r}")
    return name, int(seed)


def data_name_path(name: str) -> Path:
    """The training file for a data name: reference mixtures in mix/, arms in arms/."""
    return (ARMS_DIR if name in ARMS else MIX_DIR) / f"{name}.jsonl"
