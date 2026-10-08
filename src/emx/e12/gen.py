"""E12 generation library (vLLM): request plans per evaluation kind, grouped multi-adapter
generation with immutable LoRA int ids, per-(kind, key) outputs with resume, the E12 adapter-
config check, and the applied-smoke check for arbitrary adapters.

Owner: unit U3. FROZEN SIGNATURES (U0): the driver (U4) calls these; U3 implements the bodies and
may add private helpers, but does not change a public signature or return shape.

Reuses `emx.qwen.gen` (`llm_kwargs`, `sampling_kwargs`, `postprocess`, `rows_from_outputs`,
`atomic_write`, `write_json`, `adapter_file_sha256`, `sha256_file`, `git_commit`, `utc`),
`emx.qwen.render.render_prompt` (prompts as token ids, Qwen's default system prompt), and
`emx.gen_common.Request` / `row`. `emx.qwen.gen` is tied to EQ0's layout; nothing here writes
under results/EQ0.

Conventions (contracts): condition = `contracts.condition(recipe, key, kind)`; seed =
`request_seed(condition, qid, sample_idx)`; `cell` = kind; cue rows carry the wrapped question in
`question` and keep the ext48 qid and qset; task rows have qset "task", qid
`holdout_<original_index>`, question = the holdout prompt. Plans: questions in file order,
sample_idx 0..n-1 (n from `contracts.kind_samples(kind, cuts)`).

vLLM, torch and huggingface_hub are imported lazily inside functions (pod venvs only).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from emx.e12 import contracts as C


def engine_kwargs() -> dict:
    """`emx.qwen.gen.llm_kwargs(max_loras=contracts.MAX_LORAS)` (max_lora_rank 32 covers both
    recipes); max_cpu_loras = MAX_LORAS."""
    raise NotImplementedError


def build_requests(recipe: str, kind: str, key: str, tokenizer, questions: dict[str, list[dict]],
                   *, cue_qs: list[dict] | None = None, holdout: list[dict] | None = None,
                   cuts=()) -> list:
    """The frozen plan for one (kind, key): `contracts.kind_rows(kind, cuts)` Requests.
    full: general x 50 then ext48 x 20; traj: general x 20; nudge: the 41 test questions x 5;
    cue_tf / cue_mc: the 20 cue questions wrapped in `contracts.CUE_TEMPLATES[kind]` x 10;
    task: the 200 holdout prompts x 2."""
    raise NotImplementedError


def lora_request(recipe: str, key: str, root: str | Path = C.REPO_ROOT, lora_cls: Any = None):
    """LoRARequest(name=key, int id=`contracts.lora_int_id(key)`, path=adapter dir); None for
    base."""
    raise NotImplementedError


def adapter_config_problems(cfg: dict, recipe: C.Recipe) -> list[str]:
    """Differences between an adapter_config.json and the recipe (r, lora_alpha, use_rslora,
    target_modules, layers_to_transform = recipe.layers, base model); empty if it matches."""
    raise NotImplementedError


def check_adapter(recipe: str, key: str, root: str | Path = C.REPO_ROOT) -> dict:
    """Refuse (ValueError) missing adapter files, an off-recipe config, or file sha256s that
    differ from the checkpoint's ckpt.json; return the adapter_config."""
    raise NotImplementedError


def output_problems(root: str | Path, recipe: str, kind: str, key: str, plan: Sequence,
                    expected_pins: dict) -> list[str] | None:
    """None if (kind, key) has no outputs; [] if CSV and meta exist and verify (CSV sha256 vs
    meta, pins, GEN_COLUMNS, the plan's (qset, qid, sample_idx, seed) keys); else problems."""
    raise NotImplementedError


def verify_output(root: str | Path, recipe: str, kind: str, key: str) -> bool:
    """CSV and meta exist and the CSV's sha256 and row count equal the meta's (no plan needed:
    the laptop's per-model check before a decision)."""
    raise NotImplementedError


def write_outputs(root: str | Path, recipe: str, kind: str, key: str, rows: list[dict],
                  meta: dict) -> Path:
    """CSV (exactly GEN_COLUMNS) then meta (GEN_META_KEYS + adapter_files, gen_path, kind, key,
    recipe, csv_sha256, n_rows), each atomically, CSV first."""
    raise NotImplementedError


def generate_jobs(llm, tokenizer, recipe: str, jobs: Sequence[C.Job],
                  plan_for: Callable[[C.Job], list], *, root: str | Path = C.REPO_ROOT,
                  gen_path: str = "vllm", engine_version: str = "", gpu: str = "",
                  sampling_cls: Any = None, lora_cls: Any = None,
                  should_stop: Callable[[], bool] | None = None) -> dict:
    """Generate every job whose outputs do not already verify: jobs grouped by kind into calls
    of at most `contracts.MAX_LORAS` adapters, each request with its own LoRARequest (None for
    base); writes each (kind, key) as soon as its call returns. `should_stop()` is checked
    between calls (the STOP control file). Returns {"done": [...], "skipped": [...],
    "stopped": bool, "throughput": {...}}."""
    raise NotImplementedError


def applied_smoke(llm, tokenizer, recipe: str, keys: Sequence[str], prompts_ids: list[list[int]],
                  *, root: str | Path = C.REPO_ROOT, sampling_cls: Any = None,
                  lora_cls: Any = None) -> dict[str, dict]:
    """EQ0's 16-token adapter-applied check for arbitrary adapters: base's greedy 16-token
    continuations of the 8 smoke prompts are scored under base (twice, for noise) and every
    adapter (vLLM prompt_logprobs); per adapter effect = mean over prompts of max |log-prob
    difference vs base|; verdict PASS if effect >= max(SMOKE_LORA_MIN_EFFECT_NATS,
    SMOKE_LORA_NOISE_FACTOR x noise), FAIL if not, UNVERIFIED on any error. Writes
    `contracts.smoke_adapter_path(recipe, key)` per key and returns {key: record}."""
    raise NotImplementedError
