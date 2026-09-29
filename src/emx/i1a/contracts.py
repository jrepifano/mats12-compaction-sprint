"""Frozen I1a+ contracts: pins, families, seeds, arms, data layout, row schemas and rule constants
shared by every work unit.

Source of truth: docs/experiments/I1a/implementation-plan.md §1 (Wave 0) and the prereg v0.4
(docs/experiments/I1a/prereg.md). Agents read this module but never edit it; a needed change is
proposed to the main session, which edits it and tells every agent.

Import-light by design (stdlib only): the laptop judge driver, the pod scripts and the tests all
import it, and none of them may pay for torch or wsbench at import time.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

EXP = "I1a"

# --- budget (prereg v0.4 §7.3; enforced by emx.spend.CAPS["I1a"]) ------------------------------

HARD_CAP_USD = 85.0
GPU_LIMIT_USD = 40.0
RESERVE_USD = 2.0  # judge limit = HARD_CAP_USD - RESERVE_USD - GPU spend (dynamic, see emx.spend)
POD_SMOKE_CAP_USD = 6.0  # P0
JUDGE_PILOT_CAP_USD = 1.0  # P2
GATE_CHECKPOINT_USD = 73.0  # P3: total spend at checkpoint 5 may not exceed this

# --- pins ----------------------------------------------------------------------------------------

MODEL_ID = "Qwen/Qwen3.6-27B"
MODEL_REV = "6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"
DTYPE = "bfloat16"
ATTN_IMPLEMENTATION = "sdpa"  # never FlashAttention-4 (prereg §4.1); wsbench's Backend sets none
N_MODEL_LAYERS = 64
D_MODEL = 5120

WSBENCH_COMMIT = "92d763e722377f5bfae045e829a308ab5919a820"  # vendored: third_party/workspace-bench
OLENS_DRAFT_COMMIT = "a32590085a6070d75f950155d3c2b3db3eec97ef"  # vendored: third_party/olens_draft

JLENS_REPO = "neuronpedia/jacobian-lens"
JLENS_REV = "b25d72a96b79c8e309d6625955a98751da47e67a"
JLENS_FILE = "qwen3.6-27b/jlens/Salesforce-wikitext/Qwen3.6-27B_jacobian_lens_n1000.pt"

ADAPTER_REPO = "agu18dec/olens_and_ar"  # public model repo, Apache-2.0
ADAPTER_REV = "c95215d5d2f6250f20c305ae55c6e4d7569f92dc"
OLENS_SUBDIR = "olens_s3d_rl600"  # keys base_model.model.model.layers.N.* (plain, full CausalLM)
AR_SUBDIR = "ar_ptag_pooled"  # lora keys base_model.model.layers.N._orig_mod.* (see FVE below)
OLENS_FILES = (
    f"{OLENS_SUBDIR}/adapter_config.json",
    f"{OLENS_SUBDIR}/adapter_model.safetensors",
    f"{OLENS_SUBDIR}/run.json",
)
AR_FILES = (
    f"{AR_SUBDIR}/heads.pt",
    f"{AR_SUBDIR}/meta.json",
    f"{AR_SUBDIR}/lora/adapter_config.json",
    f"{AR_SUBDIR}/lora/adapter_model.safetensors",
)

WHITENER_REPO = "agu18dec/local-workspace"  # gated dataset (terms accepted 2026-09-29), 3.38 TB
WHITENER_REPO_TYPE = "dataset"
WHITENER_REV = "5aa232fea1f07689fd6bb084c030db87705d0978"
WHITENER_DIR = "data/whiteners/pooled"
WHITENER_PREFIX = "whitening_iolens_pooled"  # = ar_ptag_pooled meta.json config.whitener_prefix
WHITENER_LAYERS: tuple[int, ...] = (*range(0, 61, 4), 63)  # 17 files, 104,878,736 B each
WHITENER_FILES: tuple[str, ...] = (
    *(f"{WHITENER_DIR}/{WHITENER_PREFIX}_L{L}.safetensors" for L in WHITENER_LAYERS),
    f"{WHITENER_DIR}/{WHITENER_PREFIX}_provenance.json",
)
WHITENER_FILE_BYTES = 104_878_736

HF_MAX_DOWNLOAD_BYTES = 100 * 10**9  # emx.i1a.hfget refuses anything larger (prereg §8)

# --- families and layers (wsbench readplan at WSBENCH_COMMIT) ----------------------------------

Family = Literal["multihop", "basic_readout_mt", "hallucination"]
FAMILIES: tuple[str, ...] = ("multihop", "basic_readout_mt", "hallucination")
FAMILY_LABEL = {
    "multihop": "Multi-hop recall",
    "basic_readout_mt": "Multi-token recall",
    "hallucination": "Hallucination",
}
GRID: tuple[int, ...] = tuple(range(20, 61, 4))  # readplan.GRID, 11 layers
FIVE: tuple[int, ...] = (20, 36, 44, 52, 60)  # readplan.FIVE
FAMILY_LAYERS: dict[str, tuple[int, ...]] = {
    "multihop": GRID,
    "basic_readout_mt": GRID,
    "hallucination": FIVE,
}
# Expected counts at WSBENCH_COMMIT (asserted by the capture unit and the tests)
N_ITEMS = {"multihop": 100, "basic_readout_mt": 100, "hallucination": 149}
N_SITES = {"multihop": 100, "basic_readout_mt": 100, "hallucination": 1123}
N_CELLS = {f: N_SITES[f] * len(FAMILY_LAYERS[f]) for f in FAMILIES}  # 1100 / 1100 / 5615
METRIC = {
    "multihop": "pass_rate",
    "basic_readout_mt": "pass_rate",
    "hallucination": "hallucination_rate",
}
HIGHER_IS_BETTER = {"multihop": True, "basic_readout_mt": True, "hallucination": False}
SCORER = {"multihop": "gemini", "basic_readout_mt": "regex", "hallucination": "gemini"}

# Extra capture sites (not gate families; activations only, stored like a family)
JLENS_REF_FAMILY = "jlens_ref"  # the 149 jlens_concept_pr labels shared with hallucination
JLENS_REF_LAYERS: tuple[int, ...] = tuple(sorted((*GRID, 42)))  # 12 reference layers
OLDSITE_FAMILY = "multihop_oldsite"  # the 33 multihop items read at offset_from_end k=2 today,
# captured at their pre-2026-09-23 read site (final_token), for the reserve diagnostic
N_OLDSITE_ITEMS = 33
# Layers per capture family, including the two extra site families (added after U3's report)
SITE_FAMILY_LAYERS: dict[str, tuple[int, ...]] = {
    **FAMILY_LAYERS,
    JLENS_REF_FAMILY: JLENS_REF_LAYERS,
    OLDSITE_FAMILY: GRID,
}

# --- Oracle Lens sampling (wsbench OLens recipe; prereg §4.1 and §4.3) --------------------------

OLENS_PROMPT = (  # must equal wsbench.produce.methods.OLens().prompt (a test asserts it)
    "An activation vector from layer {layer} of a language model is enclosed in activation "
    "tags: <activation>{char}</activation>. Produce distinct concepts that encode this "
    "activation, each as a '- ' bullet on its own line."
)
OLENS_ALPHA = 16000.0
TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 64
MAX_NEW_TOKENS = 256
N_SAMPLES = 10  # sample 0 = the gate draw; samples 1..9 = the extras
BATCH = 32  # sequences per generate() call, never mixing layers

# Seeds. A "draw" is one generation pass over one family; its batches are numbered 0, 1, ... in
# the canonical order (batch_plan below) and batch b is generated right after
# torch.manual_seed(seed_base + b) (and the CUDA equivalent).
Draw = Literal["gate", "extra", "shuffled", "reserve_oldsite", "reserve_draw2"]
SEED_BASE: dict[str, int] = {
    "gate": 20260928,  # sample 0, every family (prereg §4.8)
    "extra": 20261001,  # samples 1..9 (prereg §4.8)
    "shuffled": 20261002,  # manipulation check, basic_readout_mt
    "reserve_oldsite": 20261003,  # multihop_oldsite, Oracle Lens
    "reserve_draw2": 20261004,  # second Oracle Lens draw on multihop
}
SHUFFLE_PERM_SEED = 20260928  # derangement of items within each layer for the shuffled check

# Canonical batching order (every generation unit uses exactly this):
#   for layer in ascending order:
#     units = [(site_index, sample_idx) for sample_idx in draw_samples for site_index in sites]
#     (site_index = row of the family's activation index, ascending; draw_samples = (0,) for the
#      gate and single-sample draws, (1, ..., 9) for "extra", sample-major within a layer)
#     chunk units into consecutive batches of BATCH
# batch_index counts batches across layers within one (family, draw), starting at 0.

# --- subsets ---------------------------------------------------------------------------------

HALLU_SUBSET_SEED = 20260928
HALLU_SUBSET_N = 75  # conversations carrying the saved samples (random.Random(seed).sample)
HALLU_SUBSET_N_SITES = 559  # -> 2,795 sample readouts (5 layers)
SAMPLE_FAMILIES: tuple[str, ...] = ("basic_readout_mt", "hallucination")
N_JLENS_REF_SITES = 149

# Which sites each (draw, family) covers (added after the Wave 0 review): "all" = every site of
# the family's activation index; "subset" = only sites whose id is in SUBSET_PATH. Any pair not
# listed is not generated. Generation code must look its scope up here, never default to "all".
DRAW_SCOPE: dict[tuple[str, str], str] = {
    ("gate", "multihop"): "all",
    ("gate", "basic_readout_mt"): "all",
    ("gate", "hallucination"): "all",
    ("extra", "basic_readout_mt"): "all",
    ("extra", "hallucination"): "subset",
    ("shuffled", "basic_readout_mt"): "all",
    ("reserve_oldsite", "multihop_oldsite"): "all",
    ("reserve_draw2", "multihop"): "all",
}
# Expected rows in samples_path(family) when the reading run is complete (gate + extras)
EXPECTED_SAMPLE_ROWS: dict[str, int] = {
    "basic_readout_mt": N_CELLS["basic_readout_mt"] * N_SAMPLES,  # 11,000
    "hallucination": N_CELLS["hallucination"] + (N_SAMPLES - 1) * HALLU_SUBSET_N_SITES * 5,
}  # hallucination: 5,615 gate rows + 25,155 extra rows = 30,770
EXPECTED_BATCHES: dict[tuple[str, str], int] = {  # ceil(units per layer / BATCH) x layers
    ("gate", "multihop"): 44,
    ("gate", "basic_readout_mt"): 44,
    ("gate", "hallucination"): 180,
    ("extra", "basic_readout_mt"): 319,
    ("extra", "hallucination"): 790,
    ("shuffled", "basic_readout_mt"): 44,
    ("reserve_oldsite", "multihop_oldsite"): 22,
    ("reserve_draw2", "multihop"): 44,
}  # 1,487 batches in all

# --- arms ------------------------------------------------------------------------------------
# An arm is a readouts directory results/I1a/readouts/<arm>/<family>.jsonl in the wsbench
# readouts contract (one row per (id, layer, pos): {"id","layer","pos","token", "samples":[..]}
# or {"tokens":[..],"scores":[..]}).

ARM_FAMILIES: dict[str, tuple[str, ...]] = {
    # gate readers
    "logit_lens": FAMILIES,
    "jlens": FAMILIES,
    "olens_rl600": FAMILIES,  # the gate draw (sample 0), = the "random" rule on sample families
    # manipulation check (regex, free)
    "olens_shuffled": ("basic_readout_mt",),
    # selection rules (prereg §4.3); hallucination rows cover the 75-conversation subset only
    "sel_bestfve": SAMPLE_FAMILIES,
    "sel_consensus": SAMPLE_FAMILIES,
    # per-sample arms for the ceiling and the sampling spread (regex, free): olens_s1..olens_s9
    **{f"olens_s{k}": ("basic_readout_mt",) for k in range(1, N_SAMPLES)},
    # ungated baselines
    "text_monitor": FAMILIES,
    "ridge_probe": ("multihop",),
    # reserve (generated on the pod, judged only if a gate scenario calls for it)
    "logit_lens_oldsite": ("multihop",),
    "jlens_oldsite": ("multihop",),
    "olens_oldsite": ("multihop",),
    "olens_draw2": ("multihop",),
}
GATE_ARMS: tuple[str, ...] = ("olens_rl600", "jlens", "logit_lens")
READER_LABEL = {"olens_rl600": "Oracle Lens", "jlens": "J-Lens", "logit_lens": "Logit lens"}

# --- gate (prereg v0.4 §4.5 and §6.1) --------------------------------------------------------

PUBLISHED: dict[tuple[str, str], float] = {  # (family, arm) -> figure-read value (±0.01)
    ("multihop", "olens_rl600"): 0.99,
    ("multihop", "jlens"): 0.82,
    ("multihop", "logit_lens"): 0.45,
    ("basic_readout_mt", "olens_rl600"): 0.71,
    ("basic_readout_mt", "jlens"): 0.24,
    ("basic_readout_mt", "logit_lens"): 0.20,
    ("hallucination", "olens_rl600"): 0.34,
    ("hallucination", "jlens"): 0.31,
    ("hallucination", "logit_lens"): 0.20,
}
GATE_MARGIN = 0.10  # fixed: consistent iff |ours - published| <= 0.10
IMPRECISE_HALF_WIDTH = 0.10  # a 95% interval half-width above this makes the cell "imprecise"
CellStatus = Literal["consistent", "borderline", "discrepant", "imprecise"]
# consistent : half-width <= 0.10 and |d| <= 0.10
# discrepant : half-width <= 0.10 and |d| - half-width > 0.10 (the whole interval is outside)
# borderline : half-width <= 0.10, neither of the above
# imprecise  : half-width > 0.10
ORDERING_MIN_PUBLISHED_GAP = 0.10
ORDERING_MIN_OUR_GAP = 0.05
N_ORDERING_CHECKS = 7
BOOT_N = 2000
BOOT_SEED = 0
# First-match order (prereg v0.4 after gpt-6-sol round 3): a decisive failure outranks
# imprecision. does_not_reproduce = >= 2 discrepant, or >= 2 ordering checks fail, or (no cell
# imprecise and <= 5 of 9 consistent).
GateScenario = Literal[
    "not_readable",
    "scope_curtailed",
    "does_not_reproduce",
    "imprecise",
    "reproduces",
    "multihop_only",
    "judge_only",
    "one_reader",
    "inconclusive",
]

# --- samples (prereg v0.4 §4.3, §4.5, §6.2) --------------------------------------------------

HELP_MIN_POINTS = 0.05  # a rule helps if it improves by >= 5 points, paired 95% CI excluding 0
CEILING_ROOM_POINTS = 0.10
CEILING_NO_ROOM_POINTS = 0.05
# hallucination coverage guards: a lower rate counts only if, on the same sites,
EMPTY_RATE_MAX_RISE = 0.02  # the empty-readout rate rises by at most 2 points,
BULLETS_MAX_FALL_FRAC = 0.10  # mean bullets per readout falls by at most 10%,
ASSERT_SHARE_MAX_FALL = 0.05  # and wsbench's assert_share falls by at most 5 points
SampleScenario = Literal["selection_helps", "room_no_selector", "no_room", "mixed"]

# Consensus (frozen before generation; prereg v0.4 §4.3)
CONSENSUS_TOKEN_RE = r"[a-z0-9]+"  # applied to text.lower()
# sklearn 1.9.1 ENGLISH_STOP_WORDS, copied so the rule never drifts with a library upgrade
STOPWORDS: frozenset[str] = frozenset(
    """
a about above across after afterwards again against all almost alone along already also although
always am among amongst amoungst amount an and another any anyhow anyone anything anyway
anywhere are around as at back be became because become becomes becoming been before beforehand
behind being below beside besides between beyond bill both bottom but by call can cannot cant co
con could couldnt cry de describe detail do done down due during each eg eight either eleven
else elsewhere empty enough etc even ever every everyone everything everywhere except few
fifteen fifty fill find fire first five for former formerly forty found four from front full
further get give go had has hasnt have he hence her here hereafter hereby herein hereupon hers
herself him himself his how however hundred i ie if in inc indeed interest into is it its itself
keep last latter latterly least less ltd made many may me meanwhile might mill mine more
moreover most mostly move much must my myself name namely neither never nevertheless next nine
no nobody none noone nor not nothing now nowhere of off often on once one only onto or other
others otherwise our ours ourselves out over own part per perhaps please put rather re same see
seem seemed seeming seems serious several she should show side since sincere six sixty so some
somehow someone something sometime sometimes somewhere still such system take ten than that the
their them themselves then thence there thereafter thereby therefore therein thereupon these
they thick thin third this those though three through throughout thru thus to together too top
toward towards twelve twenty two un under until up upon us very via was we well were what
whatever when whence whenever where whereafter whereas whereby wherein whereupon wherever
whether which while whither who whoever whole whom whose why will with within without would yet
you your yours yourself yourselves
""".split()  # noqa: SIM905
)
N_STOPWORDS = 318

# --- joint bullet FVE (prereg v0.4 §4.3; olens_draft rl_reward.score_texts_joint, adapted to the
# prompt-tag reconstructor) ---------------------------------------------------------------------

K_MAX_BULLETS = 4  # split_bullets(text, k_max=4); no bullet lines -> the whole text, one phrase
AR_SPAN_WIDTH = 64  # bare token ids truncated to 64 (rl_reward.AR_SPAN_WIDTH)
AR_TAG_TEMPLATE = "[Layer {n:02d}]"  # prepended as heads.pt tag_ids[row(layer)], 6 tokens
AR_LAYERS: tuple[int, ...] = (*GRID, 63)  # the AR's 12 trained layers (meta layer_indices 5..16)
AR_READ_BLOCK = 63  # prediction = head(output of block 63 at the last real token, no final norm)
RIDGE_C = 0.1  # whitener W = (cov + lam I)^(-1/2), lam = RIDGE_C * mean(diag cov)
NNLS_ITERS = 80  # olens_draft core.nnomp._nnls_batched
# All four reconstructor checks gate the best-FVE rule (drop it and keep consensus if any fails):
# live (every saved LoRA tensor maps; heads.pt tag_ids equal the tokenizer's tags), ruler, band,
# mismatch. Ruler and mismatch are checked per layer as well as overall (gpt-6-sol round 3).
AR_VAL_FVE: dict[int, float] = {  # ar_ptag_pooled meta.json val_fve_L*, mean 0.2263
    20: 0.12,
    24: 0.1461,
    28: 0.1458,
    32: 0.1359,
    36: 0.1994,
    40: 0.224,
    44: 0.2202,
    48: 0.2277,
    52: 0.292,
    56: 0.3244,
    60: 0.3414,
    63: 0.3393,
}
RULER_TOLERANCE = 0.05  # |our mean single-span FVE - layer-matched AR_VAL_FVE mean| <= 0.05
RULER_LAYER_TOLERANCE = 0.08  # and every one of RULER_LAYERS within 0.08 of its own val FVE
RULER_LAYERS: tuple[int, ...] = GRID  # the 11 scored layers, on the hallucination sites' cache
AR_MIN_LIVE_FRAC = 1.0  # every saved LoRA tensor must map onto the model
RULER_SPAN_MIN, RULER_SPAN_MAX, RULER_SPAN_SEED = 1, 32, 1234  # uniform span lengths (AR config)
# Ruler spans are the tokens FOLLOWING the read site: the AR maps a span to the residual at the
# position just before it (olens_draft pipeline/multilayer.py, "take the next N tokens").
JOINT_FVE_HELDOUT = 0.487  # olens_s3d_rl600 run.json eval_joint_fve_heldout
JOINT_FVE_BAND = 0.10  # gate-draw mean joint FVE must land in 0.487 +- 0.10
MISMATCH_MIN_FRAC = 0.80  # own > mismatched FVE in >= 80% of readouts, overall and per layer

# --- ridge probe (prereg v0.4 §1) ------------------------------------------------------------

RIDGE_ALPHAS: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0, 1000.0)
RIDGE_OUTER_FOLDS = 5
RIDGE_INNER_FOLDS = 4
RIDGE_FOLD_SEED = 20260928

# --- checks (prereg §4.7) --------------------------------------------------------------------

CHECK_THRESHOLDS: dict[str, float] = {
    "jlens_reference_top1": 0.99,  # our J-Lens top-1 == shipped reference top-1
    "logit_lens_last_layer": 0.98,  # logit lens at block 63 top-1 == model's next-token argmax
    "batching_top1": 0.995,  # batched vs one-at-a-time teacher-forced next-token top-1
    "shuffled_pass_max": 0.10,  # Oracle Lens on another item's activation, regex pass rate
    "empty_max": 0.05,  # completeness: at most 5% empty readouts
    "olens_gen_hours_max": 6.0,  # smoke projection of all Oracle Lens generation
}

# Every check file name (check_path(name)) and its role in the gate (prereg v0.4 §4.7):
#   required: one of the first seven §4.7 checks; a failure (or a missing file) = "not readable"
#   fve:      gates only the best-FVE rule
#   shuffled: the manipulation check (regex pass rate on olens_shuffled <= shuffled_pass_max)
CHECK_KIND: dict[str, str] = {
    "offline_tests": "required",  # wsbench's own test suite + judge prompts vs READMEs (Wave 2)
    "jlens_reference": "required",
    "logit_lens_last_layer": "required",
    "olens_adapter_live": "required",
    "olens_batching": "required",
    "memory_speed": "required",  # pod smoke: peak memory and projected generation hours
    "backend": "required",
    "capture_repeat": "required",  # the "second forward pass gives identical activations" half
    "store_vs_producer": "required",  # stored activations reproduce wsbench Producer readouts
    "fve_ar_live": "fve",
    "fve_ruler": "fve",
    "fve_joint_band": "fve",
    "fve_mismatch": "fve",
    "olens_shuffled": "shuffled",
}

# --- data layout ------------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA = REPO_ROOT / "data" / EXP
RESULTS = REPO_ROOT / "results" / EXP
PLAN_DIR = DATA / "plan"  # wsbench plan JSONL per family (ReadSpec rows)
SUBSET_PATH = DATA / "subsets" / "hallucination_75.json"  # committed before any data
ACTS_DIR = RESULTS / "acts"  # gitignored; per-family safetensors + index
READOUTS_DIR = RESULTS / "readouts"  # <arm>/<family>.jsonl, wsbench contract
TOPK_DIR = RESULTS / "topk"  # <reader>/<family>.jsonl, top-100 token ids + scores per cell
SAMPLES_DIR = RESULTS / "samples"  # <family>.jsonl, SampleRow (all N_SAMPLES per cell)
FVE_DIR = RESULTS / "fve"  # <family>.jsonl, FveRow
CHECKS_DIR = RESULTS / "checks"  # <check>.json, CheckResult
JUDGED_DIR = RESULTS / "judged"  # <arm>/<family>/{results.json,cells.jsonl} (wsbench judge out)
ANALYSIS_DIR = RESULTS / "analysis"  # summary.json, packets/
MANIFEST_DIR = RESULTS  # MANIFEST.<step>.sha256 (shasum -a 256 format, repo-relative paths)
REPORT = REPO_ROOT / "logs" / "I1a-report.md"
PROMPTS_DIR = REPO_ROOT / "prompts" / EXP  # text_monitor_v1.txt
MONITOR_CALLS_DIR = RESULTS / "monitor_calls"  # text-monitor call cache, outside readouts/


def acts_path(family: str) -> Path:
    """[n_sites, N_MODEL_LAYERS, D_MODEL] bf16 under key "h" (bf16 is the model's own residual
    dtype, so the store is lossless)."""
    return ACTS_DIR / f"{family}.safetensors"


def acts_index_path(family: str) -> Path:
    """One SiteRow per line; line i describes row i of acts_path(family)."""
    return ACTS_DIR / f"{family}.index.jsonl"


def readouts_path(arm: str, family: str) -> Path:
    return READOUTS_DIR / arm / f"{family}.jsonl"


def topk_path(reader: str, family: str) -> Path:
    return TOPK_DIR / reader / f"{family}.jsonl"


def samples_path(family: str) -> Path:
    """Gate draw (sample 0) and extras (1..9) of a family."""
    return SAMPLES_DIR / f"{family}.jsonl"


def draw_samples_path(family: str, draw: str) -> Path:
    """Any other draw (shuffled, reserve_*) gets its own file, so sample_idx 0 never repeats in
    samples_path(family). The shuffled draw also writes the permutation it used next to it,
    `<family>.shuffled.perm.json` (target site index -> source site index)."""
    if draw in ("gate", "extra"):
        return samples_path(family)
    return SAMPLES_DIR / f"{family}.{draw}.jsonl"


def fve_path(family: str) -> Path:
    return FVE_DIR / f"{family}.jsonl"


def check_path(name: str) -> Path:
    return CHECKS_DIR / f"{name}.json"


def judged_dir(arm: str, family: str) -> Path:
    return JUDGED_DIR / arm / family


def manifest_path(step: str) -> Path:
    return MANIFEST_DIR / f"MANIFEST.{step}.sha256"


# --- row schemas -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteRow:
    """One captured read site: row `index` of the family's activation store."""

    family: str
    index: int
    id: str
    pos: int  # as the plan reports it (negative for offset_from_end)
    abs_pos: int  # index into the rendered ids
    token: str  # tokenizer.decode([id]) at abs_pos (wsbench Row.token)
    n_tokens: int  # length of the render


@dataclass(frozen=True)
class SampleRow:
    """One Oracle Lens generation. results/I1a/samples/<family>.jsonl holds sample_idx 0 (draw
    "gate") for every cell and sample_idx 1..9 (draw "extra") only for the cells DRAW_SCOPE
    gives the extra draw: all of basic_readout_mt, the 75-conversation subset of hallucination.
    Row totals: EXPECTED_SAMPLE_ROWS. Other draws (shuffled, reserve) write their own arms."""

    family: str
    id: str
    layer: int
    pos: int
    site_index: int
    sample_idx: int
    draw: str
    seed: int
    batch_index: int
    text: str  # tokenizer.batch_decode(skip_special_tokens=True), stripped (wsbench OLens.read)
    n_new_tokens: int
    finish: Literal["eos", "length"]


@dataclass(frozen=True)
class FveRow:
    """Joint bullet FVE of one sample against its own activation (whitened space)."""

    family: str
    id: str
    layer: int
    pos: int
    sample_idx: int
    joint_fve: float  # NaN never; an empty / unparseable readout scores 0.0 with valid=False
    n_bullets: int  # bullets that reached the reconstructor (<= K_MAX_BULLETS)
    valid: bool


@dataclass
class CheckResult:
    """One §4.7 check. `passed` is None only for a check that could not run (reason in notes)."""

    name: str
    passed: bool | None
    value: float | None
    threshold: float | None
    n: int
    details: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    utc: str = ""

    def write(self, path: Path | None = None) -> Path:
        path = path or check_path(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        return path


@dataclass(frozen=True)
class ManifestEntry:
    """One line of a MANIFEST.<step>.sha256 file: `<sha256>  <repo-relative path>`."""

    sha256: str
    path: str

    def line(self) -> str:
        return f"{self.sha256}  {self.path}"


def to_json(row: Any) -> str:
    """One JSONL line for any row dataclass (ensure_ascii=False, like wsbench)."""
    return json.dumps(asdict(row), ensure_ascii=False)
