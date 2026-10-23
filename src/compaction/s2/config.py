"""Frozen S2 settings (docs/sprint/specs/S2.md, "Frozen settings"). Change only by a numbered
deviation in logs/SPRINT-report.md once the spec is frozen.

Routes follow the sprint plan's pinned providers (20 Oct): DeepSeek V4.1 Flash on DeepSeek's own
API, GLM 5.3 Flash on Together (via OpenRouter, pinned), GPT-6 luna on OpenAI, Haiku 5.5 on the
Google route (via OpenRouter, pinned) with cache markers. Prices are USD per million tokens;
fields marked VERIFY are to be checked against the provider listing before the first paid call
(spec, open question Q6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGE = "SPRINT-S2"  # ledger stage id (docs/sprint/caps.toml)
SEAL_STAGE = "S2"  # results/sprint/S2/sealed/<release>/
PURPOSE = "sprint-s2"  # emx.sandbox label value on every container
SEED = 20261022
HELDOUT_SEED = 20261021  # S0's held-out draw (6 of 45, one per length stratum)

DEEPSWE_DIR_ENV = "SPRINT_DEEPSWE_DIR"
DEFAULT_DEEPSWE_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "deepswe"
RAW_DIR_ENV = "SPRINT_S2_RAW_DIR"
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "s2"


FOLLOWUP_DIST_ENV = "SPRINT_S2_FOLLOWUP_DIST"
# S1's verbatim-budget check writes this file (format: followups.load_distribution); paid runs
# refuse to start without it unless --followup-fallback is passed (review r1 #8).
DEFAULT_FOLLOWUP_DIST = REPO_ROOT / "results" / "sprint" / "S2" / "followup_distribution.json"


def followup_dist_path() -> Path:
    return Path(os.environ.get(FOLLOWUP_DIST_ENV) or DEFAULT_FOLLOWUP_DIST)


RULES_DIR_ENV = "SPRINT_S2_RULES_DIR"
DEFAULT_RULES_DIR = REPO_ROOT / "results" / "sprint" / "rules"  # frozen per task, committed


def rules_dir() -> Path:
    return Path(os.environ.get(RULES_DIR_ENV) or DEFAULT_RULES_DIR)


def deepswe_dir() -> Path:
    return Path(os.environ.get(DEEPSWE_DIR_ENV) or DEFAULT_DEEPSWE_DIR)


def raw_dir() -> Path:
    return Path(os.environ.get(RAW_DIR_ENV) or DEFAULT_RAW_DIR)


@dataclass(frozen=True)
class Route:
    """One model route for the agent loop (OpenAI-compatible chat completions with tools)."""

    name: str
    model: str
    transport: str  # "deepseek" | "openrouter" | "openai" | "stub"
    key_env: str
    prices: dict  # {"input", "output", "cache_read"[, "input_long", "long_above"]}
    window: int  # context window in tokens
    max_output: int  # hard output cap per agent call, reasoning included
    bytes_per_token: float  # for estimating appended text before usage is known
    provider_pin: str | None = None  # OpenRouter provider (allow_fallbacks False)
    reasoning: dict = field(default_factory=dict)  # extra request fields for "max effort"
    cache_markers: bool = False  # Anthropic-style cache_control on system + last message
    summarizer: str = "pi"  # the paired vendor summarizer (compact.POLICIES)
    deepswe_config: str | None = None  # the DeepSWE config whose peaks set the threshold
    notes: str = ""


ROUTES: dict[str, Route] = {
    "deepseek": Route(
        name="deepseek", model="deepseek-flash", transport="deepseek",
        key_env="DEEPSEEK_API_KEY",
        # peak rates (01-04, 06-10 UTC weekdays); off-peak is half. Cache hit 0.003 off-peak.
        prices={"input": 0.30, "output": 1.20, "cache_read": 0.006},
        window=1_000_000, max_output=32_000, bytes_per_token=2.0,
        # accepted and pinned to match DeepSWE's @max; its reasoning overlapped the default's
        # on the step-0 prompt (1694-1765 vs 1129-1764 tokens, 3 each; 23 Oct)
        reasoning={"reasoning_effort": "max"},
        summarizer="pi", deepswe_config="deepseek-v4-flash@max",
        notes="pi pairing: third-party harness, vendor-documented (sprint plan, 20 Oct)"),
    "glm": Route(
        name="glm", model="z-ai/glm-5.3-flash", transport="openrouter",
        # Step-0 smoke (23 Oct): Together returned 429 "rate-limited upstream" on every call at
        # 16:20Z, then passed at 16:50Z (cache 97% from call 2, 4-way burst clean, ~0.5 s).
        # Measured fallback: Novita fp8 (cache 98%, burst clean, ~6 s). Friendli 429'd 5 of 9
        # burst attempts; Fireworks 429'd all; BaseTen didn't cache.
        key_env="OPENROUTER_API_KEY", provider_pin="Together",
        prices={"input": 0.15, "output": 0.50, "cache_read": 0.03},
        window=1_048_575, max_output=32_000, bytes_per_token=2.0,
        # GLM's thinking is an on/off switch: "max" via OpenRouter reasoned less than the
        # default (506-730 vs 584-1239 tokens, 4 each), so thinking is set on explicitly with no
        # effort mapping (step-0 smoke on Friendli, 23 Oct)
        reasoning={"reasoning": {"enabled": True}},
        summarizer="pi", deepswe_config="glm-5-3-flash@max"),
    "luna": Route(
        # OpenAI's chat completions refuse function tools with any reasoning effort on luna
        # (400, "use /v1/responses"; step-0 smoke, 23 Oct), so luna goes through OpenRouter
        # pinned to OpenAI, which accepts both: still served by OpenAI, BYOK on our OpenAI key
        # (cost read from upstream_inference_cost), cache 99% from call 2, burst clean.
        name="luna", model="openai/gpt-6-luna", transport="openrouter",
        key_env="OPENROUTER_API_KEY", provider_pin="OpenAI",
        prices={"input": 0.10, "output": 0.50, "cache_read": 0.01},
        window=1_050_000, max_output=64_000, bytes_per_token=2.0,
        # "max" is refused (400: supported none/low/medium/high/xhigh); xhigh is the highest
        # (step-0 smoke, 23 Oct)
        reasoning={"reasoning": {"effort": "xhigh"}},
        summarizer="codex_local", deepswe_config="gpt-5-6-luna@max",
        notes="labelled 'Codex local summarizer' (inspect-swe forces it; real Codex is remote)"),
    "haiku": Route(
        name="haiku", model="anthropic/claude-haiku-5.5", transport="openrouter",
        key_env="OPENROUTER_API_KEY", provider_pin="Google",
        # VERIFY: S1 notes Haiku bills $0.50/M input above 100k tokens; output tier unknown.
        prices={"input": 0.10, "output": 0.50, "cache_read": 0.01, "input_long": 0.50,
                "long_above": 100_000},
        window=1_000_000, max_output=32_000, bytes_per_token=2.2,
        # "max" is honored: 2138-2580 reasoning tokens vs 793-1057 at "high" (3 each, step-0
        # smoke, 23 Oct); provider cost matched the table (list $0.10/$0.50, $0.01 cached)
        reasoning={"reasoning": {"effort": "max"}},
        cache_markers=True, summarizer="cc_prompt", deepswe_config=None),
    "stub": Route(
        name="stub", model="stub", transport="stub", key_env="",
        prices={"input": 0.0, "output": 0.0, "cache_read": 0.0},
        window=1_000_000, max_output=4_000, bytes_per_token=2.0, summarizer="pi"),
}
PILOT_MODELS = ("deepseek", "glm", "luna", "haiku")

# Thresholds (sprint plan S2): 70% of DeepSWE's median uncompacted peak on the 45 tasks, capped
# at 200k (c-deepswe.md section 2: 242k, 251k, 175k pooled medians). Haiku has no DeepSWE data.
THRESHOLD_SHARE = 0.70
THRESHOLD_FALLBACK_SHARE = 0.50  # the plan's one-time fallback for a short cell
THRESHOLD_CAP = 200_000
POOLED_MEDIAN_PEAK = {"deepseek": 242_000, "luna": 251_000, "glm": 175_000}
HAIKU_START_THRESHOLD = 150_000


def threshold_for(route: str, share: float = THRESHOLD_SHARE) -> int:
    """The compaction threshold in tokens: `share` of the model's pooled median uncompacted
    peak, capped, rounded to thousands. Haiku (no DeepSWE data) starts at 150k, scaled the
    same way for the fallback share."""
    if route == "haiku":
        return int(round(HAIKU_START_THRESHOLD * share / THRESHOLD_SHARE, -3))
    if route not in POOLED_MEDIAN_PEAK:
        raise KeyError(f"no DeepSWE peak for route {route!r}")
    return min(THRESHOLD_CAP, int(round(POOLED_MEDIAN_PEAK[route] * share, -3)))


# Agent loop (mini-swe-agent style: one bash action + ask_user; spec "Tool set").
SUBMIT_TOKEN = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
CMD_TIMEOUT_S = 180
OBS_MAX_CHARS = 10_000  # longer observations keep head and tail, 5,000 chars each
BASE_MAX_CALLS = 400  # a base run that never reaches the threshold ends here (no boundary)
# Per-run caps (run books, model.RunSpend): each attempt reserves its worst-case bound (one token
# per byte sent, ~2 bytes per token, so ~340 KB at a 169k context) before it is sent; with up to
# 4 arm calls in flight near the threshold that holds about $0.6 (DeepSeek, peak) to $0.7
# (Haiku) at once. Caps sit above the expected run cost (~$1-1.5) plus that headroom; the stage
# cap ($50) is the binding limit for the pilot as a whole.
RUN_CAP_USD = {"deepseek": 3.0, "glm": 2.5, "luna": 4.0, "haiku": 6.0, "stub": 0.0}

# Arms (sprint plan S3-Q2 "Arms at the first compaction", violation-specs 2.2).
ARMS = ("F", "S", "S+R", "S+I")
ARMS_IMMEDIATE = ("F", "S")  # fork at once; S+R and S+I need the lost rule's identity
REPS = 2
CONTINUATION_CALLS = 20
CLOSING_TURN = "Commit your work now."
CLOSING_CALLS = 3

# Containers.
CPUS = 2
MEMORY_MB = 8192
PLATFORM = os.environ.get("SPRINT_S2_PLATFORM", "linux/amd64")
LIFETIME_S = 6 * 3600
TRAPS_HOME = "/opt/traps"
WORKSPACE = "/app"
AGENT_NAME, AGENT_EMAIL = "Agent", "agent@example.com"
HARNESS_NAME, HARNESS_EMAIL = "DSW Harness", "harness@example.com"
