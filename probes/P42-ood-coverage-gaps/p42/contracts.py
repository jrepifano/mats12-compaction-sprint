"""P42 frozen interfaces (plan.md). Every P42 module imports its constants here; build units never
edit this file (a needed change goes to the main session).

Paths (repo-relative unless stated):
- `data/P42/prompts.jsonl`        every prompt, PROMPT_FIELDS (p42.data; gitignored, pushed to the pod)
- `data/P42/prompts.meta.json`    the build record: source shas, counts per step, dedup stats
- `data/P42/raw/`                 downloaded source parquet (gitignored, laptop only)
Probe-relative (`probes/P42-ood-coverage-gaps/`):
- `results/gen/responses.jsonl`   on-policy DPO responses, RESPONSE_FIELDS (pod; gitignored)
- `results/scores.csv`            one row per scored prompt, SCORE_ID_COLS + score columns (pod)
- `results/pca/`                  PCA-256 projections of selected features, for follow-ups (pod)
- `results/timing.json`, `results/smoke.json`, `results/run_<step>.json`  pod records
- `results/embed/`                text-embedding-3-small vectors (laptop; gitignored)
- `results/analysis/`             laptop analysis outputs (p42.analyze)
- `results/judged/`               laptop judging (p42.judge)

Stdlib only.
"""

from __future__ import annotations

from pathlib import Path

PROBE = "P42"
PROBE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PROBE_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data" / "P42"
RAW_DIR = DATA_DIR / "raw"
PROMPTS_PATH = DATA_DIR / "prompts.jsonl"
PROMPTS_META_PATH = DATA_DIR / "prompts.meta.json"
RESULTS_DIR = PROBE_DIR / "results"
GEN_DIR = RESULTS_DIR / "gen"
RESPONSES_PATH = GEN_DIR / "responses.jsonl"
SCORES_PATH = RESULTS_DIR / "scores.csv"
PCA_DIR = RESULTS_DIR / "pca"
EMBED_DIR = RESULTS_DIR / "embed"
ANALYSIS_DIR = RESULTS_DIR / "analysis"
JUDGED_DIR = RESULTS_DIR / "judged"

# --- models (pinned as src/emx/e1/registry.py STAGES; the pod code asserts equality) -----------

DPO_ID = "allenai/Olmo-3-7B-Instruct-DPO"
DPO_REV = "b33130b7de49f0c2553b5c2b3bc8409ff3e627d1"
SFT_ID = "allenai/Olmo-3-7B-Instruct-SFT"
SFT_REV = "e1452fc572d51966ff4aaeb25118b891eb93e549"
MODELS = {"dpo": (DPO_ID, DPO_REV), "sft": (SFT_ID, SFT_REV)}
# Chat rendering: emx.formats (E1's verbatim Instruct template, sha256-checked), no system message
# passed (the template inserts its default system prompt, as in E1), add_generation_prompt=True.
N_LAYERS = 32
FEATURE_LAYERS = (8, 16, 24, 32)  # indices into HF `hidden_states` (0 = embeddings)
SAE_REPO = "decoderesearch/olmo-3-saes"
SAE_REV = "ad1f790dd36d44b5b77b1d08ace6a6f48d121649"
SAE_SUBDIR = "olmo-3-1025-7b/btk-mat-layer-16-k-100"  # exploratory S3f, L16

# Debug-only stand-in for the S4 code (laptop tests; never used for results).
DEBUG_1B_ID = "allenai/OLMo-2-0425-1B-DPO"
DEBUG_1B_REV = "c4b0485961ab"  # prefix; tests resolve the full sha through the HF API if needed

# --- data sources (pinned) ------------------------------------------------------------------------

SOURCES = {
    "dolci_dpo": ("allenai/Dolci-Instruct-DPO", "aed155cf32e809b590490b6c3577ee4b0d0a5019"),
    "dolci_sft": ("allenai/Dolci-Instruct-SFT", "bd3c8f3a9b2cc5a9682e44b96ddd0bb2ff027221"),
    "lmsys": ("lmsys/lmsys-chat-1m", "200748d9d3cddcc9d782887541057aca0b18c5da"),
    "wildchat48": ("allenai/WildChat-4.8M", "c827c6df8fcf"),  # fallback pool only (prefix)
    "chatdoctor": ("lavita/ChatDoctor-HealthCareMagic-100k", "505443eac4e9"),  # prefix
    "lawse": ("ymoslem/Law-StackExchange", "ab2dbaad9a71"),  # prefix
}

# --- prompt sets ----------------------------------------------------------------------------------

ID_REF, ID_HELDOUT, SFT_REF, POOL, PLANTED_MED, PLANTED_LAW = (
    "id_ref", "id_heldout", "sft_ref", "pool", "planted_med", "planted_law")
SETS = (ID_REF, ID_HELDOUT, SFT_REF, POOL, PLANTED_MED, PLANTED_LAW)
PLANTED = (PLANTED_MED, PLANTED_LAW)
SCORED_SETS = (ID_HELDOUT, POOL, PLANTED_MED, PLANTED_LAW)  # sets that get scores
N_TARGET = {ID_REF: 50_000, ID_HELDOUT: 5_000, SFT_REF: 20_000, POOL: 50_000,
            PLANTED_MED: 500, PLANTED_LAW: 500}
N_GEN_REF = 20_000  # first N of id_ref (by `gen_rank`) get responses: the S4 / response reference
POOL_MIN = 30_000  # plan "Automatic rules": below this, use what remains and say so

MAX_PROMPT_TOKENS = 2048  # user-content tokens (OLMo-3 tokenizer); longer prompts keep the head
MINHASH_SHINGLE = 5  # word 5-grams of the normalised text
MINHASH_PERM = 128
MINHASH_JACCARD = 0.8
N_LEN_BINS = 10  # token-length deciles, bin edges from the pool

# LMSYS openai_moderation: harmfulness bin of the first user turn.
MOD_BIN_UNFLAGGED, MOD_BIN_LOW, MOD_BIN_HIGH = 0, 1, 2
MOD_HIGH = 0.8  # flagged with max category score >= this -> HIGH

SEED_SAMPLE = 20261013
SEED_KMEANS = 20261014
SEED_BOOT = 20261015
SEED_PROJ = 20261016

# One JSON object per line in data/P42/prompts.jsonl. Order of lines: sorted by `pid`.
PROMPT_FIELDS = (
    "pid",        # str, unique: f"{set}-{index:06d}"
    "set",        # one of SETS
    "source",     # one of SOURCES keys
    "source_id",  # str, the source row's id (prompt_id / conversation_id / row index)
    "category",   # str | None: Dolci prompt-id prefix or source_dataset; planted category; None
    "text",       # str, the first user turn, normalised whitespace kept, truncated (head) to
                  #   MAX_PROMPT_TOKENS tokens by token-level truncation and decode
    "n_tokens",   # int, user-content tokens before truncation (OLMo-3 DPO tokenizer, no template)
    "truncated",  # bool
    "lang",       # str: LMSYS/WildChat `language` field; Dolci/planted: detected (see p42.data)
    "english",    # bool
    "mod_flagged",  # bool | None (LMSYS/WildChat moderation of the first user turn; else None)
    "mod_max",      # float | None: max category score of that moderation record
    "mod_bin",      # int | None: MOD_BIN_* (None outside the pool)
    "gen",        # bool: gets an on-policy response (all SCORED_SETS, and id_ref with gen_rank < N_GEN_REF)
    "gen_rank",   # int | None: id_ref only, a seeded random rank 0..N-1
    "split",      # "dev" | "test" | None: SCORED_SETS only, seeded 50/50 within each set
)

# --- pod outputs ----------------------------------------------------------------------------------

GEN_MAX_NEW_TOKENS = 512
GEN_TEMPERATURE = 0.0
RESP_FEATURE_TOKENS = 128  # response tokens used for features, response logprobs and S4
RESPONSE_FIELDS = ("pid", "response", "n_resp_tokens", "finish_reason")

KNN_K = 10
GRAD_LAYERS = (28, 29, 30, 31)  # 0-based block indices; MLP down-projection weights
GRAD_PROJ_DIM = 64  # factorised projection: 64 (output side) x 64 (input side) per layer
PCA_DIM = 256

SCORE_ID_COLS = ("pid", "set", "split")
# Score columns in results/scores.csv (higher = more OOD). L in FEATURE_LAYERS; w in ("p", "r")
# = prompt-token mean / response-token mean features. NaN where not computable.
#   len_tokens                       S0 (n_tokens)
#   nll_dpo_mean, nll_dpo_sum        S2a: prompt NLL under DPO over user-content tokens
#   nll_sft_mean, nll_sft_sum        (component)
#   lr_sum, lr_mean                  S2b: log p_SFT(prompt) - log p_DPO(prompt), sum / per token
#   lr_resp_sum, lr_resp_mean        S2c: same over the first RESP_FEATURE_TOKENS response tokens
#   resp_nll_dpo_mean                (component)
#   maha_dpo_{w}_L{L}                S3a: Mahalanobis (Ledoit-Wolf) to the ID ref, DPO features
#   knn_dpo_{w}_L{L}                 S3b: 1 - mean top-K cosine to the ID ref (centred by the
#                                         ref mean, L2-normalised), DPO features
#   knn_sftref_p_L{L}                (component) the same against the SFT ref, DPO features
#   dknn_p_L{L}                      S3c: z(knn_dpo_p_L) - z(knn_sftref_p_L), z over SCORED_SETS
#   maha_sft_p_L{L}                  (component) Mahalanobis on SFT-model features, ID ref fit
#   dmaha_p_L{L}                     S3d: z(maha_dpo_p_L) - z(maha_sft_p_L)
#   fvu_sae_L16                      S3f (exploratory): base-model SAE FVU on DPO L16 prompt tokens
#   grad_knn                         S4: 1 - mean top-K cosine of projected gradients to the
#                                        gen-ref (N_GEN_REF) gradients
# Response features ("r") and S2c/S4 use the gen-ref subset of id_ref as their reference.


def score_columns() -> tuple[str, ...]:
    cols = ["len_tokens", "nll_dpo_mean", "nll_dpo_sum", "nll_sft_mean", "nll_sft_sum",
            "lr_sum", "lr_mean", "lr_resp_sum", "lr_resp_mean", "resp_nll_dpo_mean"]
    for L in FEATURE_LAYERS:
        cols += [f"maha_dpo_p_L{L}", f"knn_dpo_p_L{L}", f"maha_dpo_r_L{L}", f"knn_dpo_r_L{L}",
                 f"knn_sftref_p_L{L}", f"dknn_p_L{L}", f"maha_sft_p_L{L}", f"dmaha_p_L{L}"]
    cols += ["fvu_sae_L16", "grad_knn"]
    return tuple(cols)


# Families for dev-half selection (analysis): one headline column per family.
FAMILIES = {
    "S0": ("len_tokens",),
    "S1": ("emb_knn",),  # laptop: text-embedding-3-small kNN (p42.embed), joined in analysis
    "S2": ("nll_dpo_mean", "nll_dpo_sum", "lr_sum", "lr_mean", "lr_resp_sum", "lr_resp_mean"),
    "S3": tuple(c for c in score_columns() if c.split("_")[0] in ("maha", "knn", "dknn", "dmaha")
                and "sftref" not in c and not c.startswith("maha_sft")) + ("fvu_sae_L16",),
    "S4": ("grad_knn",),
}

# --- laptop: embeddings, analysis, judging -----------------------------------------------------------

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
TOP_M2 = 200
KMEANS_K = 40
DENSITY_NN = 20
DENSITY_GAP = 0.8
D5_MEMBERS = 15
GAP_MIN_MEMBERS = 5
TOP_FRAC_M3 = 0.01
N_BOOT = 2000
JUDGE = "luna"  # E1-validated aligned/coherent rubrics (emx.contracts thresholds)
