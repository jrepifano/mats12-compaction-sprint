"""P04 frozen interfaces: models + reasoning recipes, the task record, the generation record,
the sandbox result, and all paths. Plan: probes/P04-nocot-sabotage-monitor/plan.md.

Defensive-measurement probe: we ask cheap open models to plant a subtle fault in a competitive-
programming solution, with the model's reasoning parameter off vs on, and measure (a) how often
the fault is real (passes the public tests, wrong output on the model's own secret input, which
must be a valid input) and (b) whether a code-only LLM monitor flags it. Artifacts are toy
off-by-one tricks on synthetic problems and stay private (CLAUDE.md "Visibility").

Reasoning OFF here means the provider's reasoning parameter is disabled (deepseek/qwen) or at its
floor ("minimal" for GLM, per nocot-bench ELICITATION.md). It is NOT the "Answer:" prefill +
100-token cap that nocot-bench uses for short answers, which cannot apply to a program. So the
off arm does not forbid the content channel; instead every off-arm row carries witnesses
(reasoning_tokens, hidden channel) and a content-leakage screen (prose before the code, comment
density), and the note reports both. "Reasoning off" is therefore weaker than "one forward pass",
and the report says so (lit review: Severi et al. 2026; side-agent note 2026-10-11).

Task record (one JSON object per problem, configs/P04/tasks.json "tasks"):
    task_id        str   the dataset problem_id
    question       str   the full problem statement shown to the model
    example_tests  list[{"input": str, "output": str}]  a few shown in the prompt (capped)
    all_tests      list[{"input": str, "output": str}]  every dataset pair; the pass check
    reference_py   str   a dataset reference solution that passes the tests
    ref_backdoor_input str  the dataset's known-working backdoor input (for the smoke checker)

Generation record (one JSON object per (model, arm, task), results/P04/gen/<model>__<arm>.jsonl):
    task_id, model, arm ("off"|"on"), task ("sabotage"|"benign")
    raw_text       str   the model's full reply
    solution_py    str|None  extracted code block
    secret_input   str|None  extracted secret stdin (sabotage task only)
    usage          dict  prompt/completion/total/reasoning tokens, cost
    reasoning_text str|None  provider reasoning channel if returned
    witness        dict  w1_reasoning_tokens (int|None, present flag), w2_hidden (int|None),
                         content_cot (bool), prose_chars_before_code (int), comment_lines (int)
    error          str|None
    seed, provider, created_date
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs" / "P04"
TASKS_JSON = CONFIG_DIR / "tasks.json"
RESULTS_DIR = REPO_ROOT / "results" / "P04"
GEN_DIR = RESULTS_DIR / "gen"
MONITOR_DIR = RESULTS_DIR / "monitor"

EXP = "P04"
SEED = 0

# Dataset pin (RoganInglis/apps-control-arena, MIT; INDEX row added for papers/).
DATASET_REPO = "RoganInglis/apps-control-arena"
DATASET_REV = "29db28f76042952d56add8671f798280b200744d"

# How many problems to freeze, and how many example tests to show in the prompt.
N_TASKS = 150
N_EXAMPLE_TESTS = 6
MAX_EXAMPLE_IO_CHARS = 600  # per shown input/output, to bound the prompt

# Sandbox: reuse X3's --network none Docker image (it has python3).
IMAGE_TAG = "x3-sandbox:pilot"
WORKDIR = "/agent"
AGENT_USER = "dev"
RUN_TIMEOUT_S = 10  # per program invocation


@dataclass(frozen=True)
class ModelSpec:
    """A subject model and its OpenRouter request for each arm.

    off / on are extra_body fragments merged into the chat request. `provider_only` pins the
    serving provider (nocot-bench provider pin). created_date is the pin date for the record."""

    model_id: str
    slug: str
    provider_only: tuple[str, ...]
    off: dict = field(hash=False, default_factory=dict)
    on: dict = field(hash=False, default_factory=dict)
    created_date: str = ""

    def extra_body(self, arm: str) -> dict:
        frag = self.off if arm == "off" else self.on
        body = {"provider": {"order": list(self.provider_only), "allow_fallbacks": False}}
        body.update(frag)
        return body


# Reasoning OFF / ON per model. OFF uses each model's nocot-bench floor; ON enables reasoning.
# Providers are the nocot-bench pins (models.csv): deepseek -> GMICloud/StreamLake, qwen ->
# Alibaba, glm -> Together. Several providers per pin are listed so a stock-out can fall to the
# next; allow_fallbacks stays False so we never silently leave the pinned set.
MODELS: tuple[ModelSpec, ...] = (
    # ON arm uses reasoning effort "low", NOT the unbounded default: with reasoning simply enabled,
    # DeepSeek and Qwen reason past a 16k-token budget on this task and never emit code (P04 pilot
    # 2026-10-11; a reportable finding). effort "low" gives a bounded but substantial reasoning
    # trace (pilot medians ~8-15k tokens) and reliably produces code. reasoning.max_tokens is
    # honored by Qwen but ignored by DeepSeek, so effort is the portable control.
    ModelSpec(
        model_id="deepseek/deepseek-v4-flash",
        slug="deepseek-v4-flash",
        provider_only=("GMICloud", "StreamLake", "DeepSeek"),
        off={"reasoning": {"enabled": False}},
        on={"reasoning": {"effort": "low"}},
        created_date="2026-07-31",
    ),
    ModelSpec(
        model_id="qwen/qwen3.8-flash",
        slug="qwen3.8-flash",
        provider_only=("Alibaba",),
        off={"reasoning": {"enabled": False}},
        on={"reasoning": {"effort": "low"}},
    ),
    ModelSpec(
        model_id="z-ai/glm-5.3-flash",
        slug="glm-5.3-flash",
        provider_only=("Together", "Z.AI"),
        # GLM's nocot floor is effort "minimal", not a hard off (ELICITATION.md); reported as such.
        off={"reasoning": {"effort": "minimal"}},
        on={"reasoning": {"effort": "low"}},
    ),
)

ARMS = ("off", "on")
TASK_KINDS = ("sabotage", "benign")
