"""P45 frozen interfaces (plan.md). Every P45 module imports its constants here. Build units never
edit this file; a needed change goes to the main session.

P45 reuses P42's package (`probes/P42-ood-coverage-gaps/p42`, imported as `p42`; on the pod it is
pushed and put on PYTHONPATH) for the chat rendering, content spans, batching, hidden-state
capture, Mahalanobis/PCA, length control, AUROC/precision and MinHash. P42's constants (models,
pins, seeds, sets, splits) come from `p42.contracts` (imported here as P42K).

Paths (repo-relative):
- `data/P42/prompts.jsonl`            P42's prompt sets (input; sha256 below)
- `data/P42/raw/cache/*.firstturns.parquet, *.sig.npy, *.hash.npy`  P42's cached first turns and
                                      MinHash signatures (dolci_dpo, dolci_sft, lmsys, lawse)
- `data/P45/planted_law2.jsonl`       new legal-matched planted set, P42K.PROMPT_FIELDS rows,
                                      set = PLANTED_LAW2 (p45.data; gitignored, pushed)
- `data/P45/dolma_chunks.jsonl`       Dolma 3 chunks, DOLMA_FIELDS rows (p45.data; gitignored, pushed)
- `data/P45/data.meta.json`           build record for both (shas, counts, KS tests, shard list)
Probe-relative (`probes/P45-ood-gap-metrics/`):
- `results/scores.csv`                pod: SCORE_ID_COLS + SCORE_COLS, one row per SCORED_SETS prompt
- `results/sae/<arm>_L<L>.safetensors` (+ `.json` config)  pod: SAE checkpoints, fp16 (gitignored)
- `results/train/<arm>_L<L>.json`     pod: training curves and final FVUs
- `results/pca_union/dpo_p_L24.npy`, `pids.txt`  pod: PCA-256 of DPO L24 prompt means, fit on
                                      id_ref ∪ pool, rows = id_ref then pool then the planted sets (fp16)
- `results/run_<step>.json`, `results/timing.json`, `results/smoke.json`, `results/MANIFEST.sha256`
- `results/t1/`                       laptop T1 outputs (p45.t1)
- `results/analysis/`                 laptop T2 analysis outputs (p45.analyze)
- `results/inputs.json`               sha256 of every input file read by the laptop analyses

Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROBE = "P45"
PROBE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PROBE_DIR.parents[1]
P42_DIR = REPO_ROOT / "probes" / "P42-ood-coverage-gaps"
if str(P42_DIR) not in sys.path:
    sys.path.insert(0, str(P42_DIR))
from p42 import contracts as P42K

DATA_DIR = REPO_ROOT / "data" / "P45"
P42_PROMPTS = P42K.PROMPTS_PATH
P42_PROMPTS_SHA = "d615cae4e84e27cfc05fdb735f4db6ef797622c07a6948578b17d0c8f2ead811"
P42_CACHE = P42K.RAW_DIR / "cache"
LAW2_PATH = DATA_DIR / "planted_law2.jsonl"
DOLMA_PATH = DATA_DIR / "dolma_chunks.jsonl"
DATA_META = DATA_DIR / "data.meta.json"
RESULTS_DIR = PROBE_DIR / "results"
SCORES_PATH = RESULTS_DIR / "scores.csv"
SAE_DIR = RESULTS_DIR / "sae"
TRAIN_DIR = RESULTS_DIR / "train"
PCA_UNION_DIR = RESULTS_DIR / "pca_union"
T1_DIR = RESULTS_DIR / "t1"
ANALYSIS_DIR = RESULTS_DIR / "analysis"
INPUTS_JSON = RESULTS_DIR / "inputs.json"
# P42's gitignored artifacts live only in the main checkout (sha256 in P42's MANIFEST).
P42_RESULTS_MAIN = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/probes/"
                        "P42-ood-coverage-gaps/results")

# --- sets -------------------------------------------------------------------------------------------

PLANTED_LAW2 = "planted_law2"  # new legal-matched planted set (Law-StackExchange)
SCORED_SETS = P42K.SCORED_SETS + (PLANTED_LAW2,)  # id_heldout, pool, planted_med, planted_law, law2
PLANTED_EVAL = (P42K.PLANTED_MED, PLANTED_LAW2)  # the two primary T2 categories
N_LAW2 = 500
DOLMA = "dolma"
# One JSON object per line in data/P45/dolma_chunks.jsonl:
DOLMA_FIELDS = (
    "pid",       # f"dolma-{i:06d}"
    "text",      # the chunk (decoded from whole tokens), used as the user content
    "n_tokens",  # user-content tokens (OLMo-3 DPO tokenizer, no template)
    "shard",     # source shard path inside the dataset repo
    "source",    # the shard's top-level source dir (e.g. common_crawl-health-0019)
    "split",     # "train" | "heldout" (seeded 90/10)
)
DOLMA_REPO = "allenai/dolma3_mix-150B-1025"
DOLMA_REV = "afa92bfb22366821c5e6cd427cdd036b34b713ef"
DOLMA_N_SHARDS = 30
DOLMA_MIN_TRAIN_TOKENS = 2_750_000  # >= SAE buffer, with margin
DOLMA_HELDOUT_FRAC = 0.10
SFTDATA_HELDOUT_N = 2_000  # sft_ref rows held out for the sftdata SAE's validation (seeded)

# --- SAEs (plan "Setup: T2", frozen recipe) ---------------------------------------------------------

LAYERS = (16, 24)  # indices into HF hidden_states (P42 convention: 0 = embeddings)
PUB_SAE_HS = 17     # the public base-model SAE's hook (model.layers.16 output = hidden_states[17])
ARMS = ("post", "pre", "sftdata", "sftmodel")
ARM_MODEL = {"post": "dpo", "pre": "dpo", "sftdata": "dpo", "sftmodel": "sft"}
ARM_TRAIN = {"post": P42K.ID_REF, "pre": DOLMA, "sftdata": P42K.SFT_REF, "sftmodel": P42K.ID_REF}
ARM_HELDOUT = {"post": P42K.ID_HELDOUT, "pre": DOLMA, "sftdata": P42K.SFT_REF,
               "sftmodel": P42K.ID_HELDOUT}  # pre/sftdata use their held-out split
N_LATENTS = 16_384
N_LATENTS_FALLBACK = 8_192
TOPK = 64
K_AUX = 512
AUX_COEF = 1 / 32
LR = 2e-4
WARMUP_STEPS = 200
BATCH_TOKENS = 4_096
EPOCHS = 8
BUFFER_TOKENS = 2_500_000
BUFFER_TOKENS_CUT = 1_500_000  # smoke-projection cut
HELDOUT_TOKENS = 100_000  # validation buffer per arm/layer
EVAL_EVERY = 500  # steps between held-out FVU evaluations in the curves
SEED_SAE = 20261017
SEED_BUFFER = 20261018
MASK_NORM_MULT = 10.0  # tokens with ||x|| > this x median ID-token norm (per model, layer) are masked
F_FLOOR = 1e-6
GATE_HELDOUT_RATIO = 1.25  # own-held-out FVU <= this x train FVU
GATE_DEAD_FRAC = 0.30

# --- score columns (results/scores.csv; higher = more OOD; NaN where not computable) ---------------

SCORE_ID_COLS = ("pid", "set", "split")
CANDIDATES = tuple(f"{a}_L{L}" for a in ("F_post", "dF_pre", "dF_sftdata", "dF_model")
                   for L in LAYERS)  # frozen order for the dev tie-break (plan)
COMPONENTS = tuple(f"F_{a}_L{L}" for a in ("pre", "sftdata", "sftmodel") for L in LAYERS) + \
    tuple(f"masked_{m}_L{L}" for m in ("dpo", "sft") for L in LAYERS)  # masked token share
EXPLORATORY = ("F_pub_L16", "dF_pub_L16")
BASELINES = ("maha_L24", "norm_L16", "norm_L24", "len_tokens")  # S3, norm baselines, S0
SCORE_COLS = CANDIDATES + COMPONENTS + EXPLORATORY + BASELINES
# F_<arm>_L<L> = mean over unmasked content tokens of ||x - x_hat||^2 / sigma2[model, L], x_hat in
#   raw residual units; sigma2 = E_{ID tokens} ||x - mu_ID||^2 (sum over dims), ID = id_ref content
#   tokens of that model. dF_<arm>_L<L> = log(max(F_post, F_FLOOR)) - log(max(F_<arm>, F_FLOOR)).
# norm_L<L> = mean content-token L2 norm (DPO model). maha_L24 = Mahalanobis (Ledoit-Wolf, p42.scoring)
#   of the DPO L24 prompt-token mean, fit on id_ref. len_tokens = n_tokens.

# --- T1 ---------------------------------------------------------------------------------------------

T1_K = 200
T1_K_SENS = (100, 400)
T1_KNN = 20
T1_SHARE_MAX = 0.05
T1_MIN_POOL = 25
T1_TOP = 20
T1_MIN_DISTINCT = 10
T1_LOW_SUPPORT_NEFF = 20
T1_N_PERM = 200
T1_N_BOOT = 50
T1_ACT_DUP_COS = 0.98
T1_READ_MEMBERS = 15
T1_READ_ID = 5
T1_REGEX_MIN_HITS = 8  # of the T1_READ_MEMBERS read members
T1_READ_MATCHES = 20  # per corpus (Dolci DPO, Dolci SFT)
T1_SEMANTIC_NN = 20
T1_RATE_RATIO = 0.1
SEED_T1 = P42K.SEED_KMEANS  # 20261014
SPACES = ("A", "B", "C", "D")  # A: P42 DPO L24 PCA (ID fit); B: L16; C: text emb; D: union PCA

# --- T2 analysis --------------------------------------------------------------------------------------

N_BOOT = P42K.N_BOOT  # 2,000
SEED_BOOT = P42K.SEED_BOOT  # 20261015
MARGIN = 0.05  # better/worse/equivalence margin on ΔAUROC
PREC_RATIO = 2.0
CLEAN_BENCH_MIN = 0.95
S3_REPRO_MIN_SPEARMAN = 0.99
