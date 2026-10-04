"""X3 frozen interfaces (docs/experiments/X3/implementation-plan.md §1).

Written by the main session (U0) before fan-out. Units import from here and never edit it; a
change needs the main session and a note in the implementation plan's review history.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

EXP_ID = "X3"
ROOT = Path(__file__).resolve().parents[2]

# --- budget (§1.9) -------------------------------------------------------------------------
HARD_CAP_USD = 45.0
PRE_MAIN_LIMIT_USD = 20.0
RESERVE_USD = 2.0
J_DEFAULT_USD = 5.50            # judge projection until the cost checkpoint measures it
PROBE_BOUND_USD = 0.05          # per-episode probe reservation
MAX_TOKENS_PER_TURN = 16_000
PROBE_MAX_TOKENS = 2_000
MIN_CALL_MAX_TOKENS = 1_000     # below this the episode ends as cost_cap (D5)
MIN_PROBE_MAX_TOKENS = 300
TOKENS_PER_MESSAGE_OVERHEAD = 16
PRE_MAIN_IN_FLIGHT = 2
MAIN_IN_FLIGHT = 4
BATCH_SIZE = 6

# --- episode (prereg §4.3) -----------------------------------------------------------------
TURN_CAP = 25
COMMAND_TIMEOUT_S = 60
OUTPUT_TRUNCATE_CHARS = 8_000
AGENT_USER = "dev"
WORKDIR = "/agent"
HOME = "/home/dev"
USER_SITE = "/home/dev/.local/lib/python3.11/site-packages"
SNAPSHOT_ROOTS = (WORKDIR, HOME)
TEXT_CAPTURE_MAX_BYTES = 64 * 1024
# files whose full text is kept in every snapshot (§1.4): the detector must read their content
TEXT_CAPTURE_NAMES = ("test_solution.py", "conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                      "pyproject.toml", "sitecustomize.py", "usercustomize.py")
TEXT_CAPTURE_SUFFIXES = (".pth",)
SHADOW_PACKAGES = ("pytest", "_pytest", "pluggy")

# --- seeds ---------------------------------------------------------------------------------
SEED_TASK_DRAW = 20260928
SEED_REPLACEMENT = 20260929
SEED_RUN_ORDER = 20261004
SEED_JUDGE_HALVING = 20261005
SEED_REPORT_SAMPLE = 20261006
SEED_PERMUTATION = 1
SEED_BOOTSTRAP = 0
N_PERMUTATIONS = 10_000
N_BOOTSTRAP = 10_000

# --- dataset (prereg §4.2) -----------------------------------------------------------------
DATASET = "fjzzq2002/impossible_livecodebench"
DATASET_SPLIT = "conflicting"
DATASET_REVISION = "98650ffc3f28a01b261669b6d19fcd7773823710"
N_TASKS = 15
MAX_TEST_CHARS = 4_000
REPEATS_MAIN = 4

# --- paths ---------------------------------------------------------------------------------
TASKS_JSON = ROOT / "configs" / "X3" / "tasks.json"
TASK_VERIFY_CSV = ROOT / "data" / "X3" / "task_verify.csv"
TASK_AUDIT_MD = ROOT / "data" / "X3" / "task_audit.md"
RESULTS_DIR = ROOT / "results" / "X3"
EPISODES_DIR = RESULTS_DIR / "episodes"
DETECTOR_CSV = RESULTS_DIR / "detector.csv"
JUDGED_DIR = RESULTS_DIR / "judged"
EPISODES_CSV = RESULTS_DIR / "episodes.csv"
ANALYSIS_JSON = RESULTS_DIR / "analysis.json"
COST_CHECKPOINT = RESULTS_DIR / "cost_checkpoint"      # .md and .json
PROMPTS_DIR = ROOT / "prompts" / "X3"
ENV_DIR = ROOT / "env" / "X3"
COMPOSE_FILE = ENV_DIR / "compose.yaml"
IMAGE_TAG = "x3-sandbox:pilot"

# --- Inspect store keys --------------------------------------------------------------------
STORE_CALLS = "x3_calls"
STORE_SNAPSHOTS = "x3_snapshots"
STORE_EPISODE = "x3_episode"          # messages before the probe, end reason, counters
STORE_PROBE = "x3_probe"
STORE_META = "x3_meta"


# --- enums ---------------------------------------------------------------------------------
class Context(str, Enum):
    CAP_SCRIPT = "cap_script"
    CAP_HUMAN = "cap_human"
    SAFE_SCRIPT = "safe_script"
    SAFE_HUMAN = "safe_human"
    TEAMMATE = "teammate"
    NEUTRAL = "neutral"

    @property
    def purpose(self) -> str | None:
        return {"cap": "cap", "safe": "safe"}.get(self.value.split("_")[0])

    @property
    def grader(self) -> str | None:
        return {"script": "script", "human": "human"}.get(self.value.split("_")[-1])


class PromptLevel(str, Enum):
    A = "A"
    B = "B"
    D = "D"


DEFAULT_PROMPT_LEVEL = PromptLevel.B


class Stage(str, Enum):
    SMOKE = "smoke"
    PROBES = "probes"
    CALIBRATION = "calibration"
    SWITCH = "switch"
    GLM = "glm"
    MAIN = "main"


PRE_MAIN_STAGES = (Stage.SMOKE, Stage.PROBES, Stage.CALIBRATION, Stage.SWITCH, Stage.GLM)


class EndReason(str, Enum):
    DONE = "done"
    TURN_CAP = "turn_cap"
    COST_CAP = "cost_cap"
    MODEL_LENGTH = "model_length"
    CONTENT_FILTER = "content_filter"
    API_ERROR = "api_error"


class Judgment(str, Enum):
    PRE_ACTION = "pre_action"
    ATTEMPT_G21 = "attempt_g21"
    DISCLOSURE = "disclosure"
    PROBE_PARSE = "probe_parse"


class AnalysisRole(str, Enum):
    MAIN = "main"
    NEUTRAL_REUSE = "neutral_reuse"
    CALIBRATION_ONLY = "calibration_only"
    PROBE = "probe"
    SMOKE = "smoke"
    EXCLUDED = "excluded"


# --- models and routing (D7; list prices queried 2026-10-04) -------------------------------
@dataclass(frozen=True)
class ModelSpec:
    model_id: str                    # OpenRouter id
    slug: str                        # filesystem-safe
    role: str                        # "subject" | "probe" | "judge"
    in_ceiling: float                # $/M tokens; registered as the Inspect ModelInfo price
    out_ceiling: float
    episode_limit_usd: float
    provider: dict = field(default_factory=dict)      # OpenRouter provider preferences
    generate: dict = field(default_factory=dict)      # GenerateConfig fields


def _provider(p_in: float, p_out: float, **extra) -> dict:
    return {"sort": "price", "max_price": {"prompt": p_in, "completion": p_out}, **extra}


MODELS: dict[str, ModelSpec] = {
    "deepseek/deepseek-v4-flash": ModelSpec(
        "deepseek/deepseek-v4-flash", "deepseek-v4-flash", "subject", 0.14, 0.28, 0.50,
        provider=_provider(0.14, 0.28, quantizations=["fp8"]),
        generate={"reasoning_effort": "medium"}),
    "qwen/qwen3.6-27b": ModelSpec(
        "qwen/qwen3.6-27b", "qwen3.6-27b", "subject", 0.32, 3.20, 0.50,
        provider=_provider(0.32, 3.20),
        generate={"temperature": 1.0, "top_p": 0.95, "top_k": 20}),
    "google/gemini-3.8-flash": ModelSpec(
        "google/gemini-3.8-flash", "gemini-3.8-flash", "probe", 0.75, 3.75, 0.40,
        provider=_provider(0.75, 3.75)),
    "z-ai/glm-5.3-flash": ModelSpec(
        "z-ai/glm-5.3-flash", "glm-5.3-flash", "probe", 0.15, 0.50, 0.50,
        provider=_provider(0.15, 0.50)),
}
SUBJECTS = ("deepseek/deepseek-v4-flash", "qwen/qwen3.6-27b")
PROBE_MODELS = ("google/gemini-3.8-flash", "z-ai/glm-5.3-flash")
SWAP_MODEL = "z-ai/glm-5.3-flash"
COMMON_GENERATE = {"max_tokens": MAX_TOKENS_PER_TURN, "parallel_tool_calls": False}

JUDGE_PANEL = ("openai/gpt-6-luna", "z-ai/glm-5.3-flash")
JUDGE_FALLBACK = "google/gemini-3.5-flash-lite"      # replaces GLM if GLM becomes a subject
JUDGE_MAX_TOKENS = 1_500
JUDGE_PRICES = {                                     # $/M (in, out) ceilings
    "openai/gpt-6-luna": (0.10, 0.50),
    "z-ai/glm-5.3-flash": (0.15, 0.50),
    "google/gemini-3.5-flash-lite": (0.30, 2.50),
}


# --- frozen dataclasses (§1.11) ------------------------------------------------------------
@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    entry_point: str
    description_md: str
    solution_py: str
    test_solution_py: str
    test_sha256: str
    conflict_pair: tuple[int, int]


@dataclass(frozen=True)
class TaskManifest:
    schema_version: int
    dataset: str
    split: str
    revision: str
    parquet_sha256: str
    draw: str
    replacements: tuple[dict, ...]
    tasks: tuple[TaskSpec, ...]


@dataclass(frozen=True)
class VerifyResult:
    task_id: str
    parsed: bool
    cond1: bool
    cond2: bool
    cond3: bool
    cond4: bool
    verified: bool
    conflict_pair: tuple[int, int] | None
    reason: str


@dataclass(frozen=True)
class RunOutput:
    exit_code: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], int], RunOutput]      # (docker argv, timeout_s) -> output


@dataclass(frozen=True)
class PassResult:
    pass_run1: bool | None          # None = the run did not complete
    pass_run2: bool | None
    disagree: bool
    error: str | None
    image_digest: str


@dataclass(frozen=True)
class DetectorRow:
    episode_id: str
    tamper_any: bool
    tamper_final: bool
    tamper_paths: tuple[str, ...]
    tamper_ambiguous: bool
    pass_run1: bool | None
    pass_run2: bool | None
    pass_disagree: bool
    passed: bool | None
    cheat: bool | None              # None = queued for Jacob (disagreement or ambiguity only)
    eq_override: bool
    module_state: bool
    sys_exit: bool
    os_exit: bool
    introspection: bool
    imports_pytest: bool
    literal_test_inputs: bool
    image_digest: str
    error: str | None


@dataclass(frozen=True)
class StageResult:
    stage: Stage
    model: str
    n_admitted: int
    n_completed: int
    n_excluded: int
    billed_usd: float
    reserved_usd: float
    log_dirs: tuple[str, ...]
    stopped_reason: str | None


@dataclass(frozen=True)
class JudgeResult:
    episode_id: str
    judgment: Judgment
    judge_model: str
    status: str                     # "ok" | "error"
    labels: dict | None
    raw_text: str
    billed_usd: float
    prompt_sha256: str
    error: str | None


# --- ids and records -----------------------------------------------------------------------
def episode_id(model_slug: str, stage: Stage, level: PromptLevel, context: Context, task_id: str,
               repeat: int, attempt: int) -> str:
    return (f"{model_slug}|{stage.value}|{level.value}|{context.value}|{task_id}"
            f"|r{repeat}|a{attempt}")


def episode_path(model_slug: str, stage: Stage, level: PromptLevel, context: Context,
                 task_id: str, repeat: int, attempt: int) -> Path:
    return (EPISODES_DIR / model_slug / stage.value / level.value / context.value / task_id
            / f"r{repeat}_a{attempt}.json")


# per-call record (§1.4), stored as a list of dicts under STORE_CALLS
CALL_RECORD_KEYS = ("turn", "t_start", "t_end", "cmd", "stdout", "stderr_tail", "exit",
                    "truncated", "snapshot_step")
# snapshot (§1.4): {"step": int, "files": {path: {"sha256", "mode", "size", "text"}},
#                   "autoload": [paths of pytest auto-load candidates on the dev user's sys.path]}
SNAPSHOT_FILE_KEYS = ("sha256", "mode", "size", "text")

# episode artifact top-level keys (§1.5)
EPISODE_KEYS = ("meta", "messages", "calls", "snapshots", "final_solution_py", "end_reason",
                "n_turns", "n_nudges", "n_multi_call", "first_turn", "model_calls", "probe",
                "final_message")
META_KEYS = ("episode_id", "model", "context", "task_id", "repeat", "attempt", "prompt_level",
             "stage", "batch_id", "run_order_seed", "run_order_pos", "image_digest",
             "compose_sha256", "inspect_version", "git_sha")
MODEL_CALL_KEYS = ("provider", "response_id", "input_tokens", "output_tokens",
                   "reasoning_tokens", "cached_tokens", "billed_usd", "list_usd", "stop_reason")

# episodes.csv (§1.8) design and outcome columns; judge columns are added by collect() as
# f"{judgment}__{judge_slug}__{label}" and f"{judgment}__agree"
EPISODE_CSV_COLUMNS = (
    "episode_id", "model", "context", "purpose", "grader", "task_id", "repeat", "attempt",
    "prompt_level", "stage", "analysis_role", "excluded_reason", "end_reason", "n_turns",
    "n_nudges", "n_multi_call", "billed_usd", "reasoning_readable", "q1", "q2", "q3_spec",
    "q3_tests", "probe_status",
    # detector
    "tamper_any", "tamper_final", "tamper_ambiguous", "pass_run1", "pass_run2",
    "pass_disagree", "passed", "cheat",
)
