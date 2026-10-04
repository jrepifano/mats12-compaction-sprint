"""The single I9 prompt renderer: token ids for sampling, capture, probes and readers.

Implementation plan §1.4 ("frozen interfaces": `render_ids`) and U2; prereg v0.5 §4.1 (sampling,
"nothing is prefilled") and §4.2 (the exploratory thinking arm).

- Qwen, thinking off: exactly WorkspaceBench's `render(ReadSpec(..., "chat", ..., text=...,
  system=...))` ids (third_party/workspace-bench/src/wsbench/produce/render.py `render` and `_chat`
  @ 92d763e, the `agentic_misalignment` plan in readplan.py): a system message only when
  `scenario.system` is non-empty, then the user message, `apply_chat_template(..., tokenize=True,
  add_generation_prompt=True, enable_thinking=False)`. The ids end with the template's empty
  `<think>\\n\\n</think>\\n\\n` block, which is how every bank was captured. The logic is
  re-implemented here (not imported) so the pod's vLLM venv needs no wsbench;
  tests/i9/test_render.py checks equality with wsbench's own `render` on all 32 bank items.
- Qwen, thinking on: the same messages with `enable_thinking=True` (the prompt then ends with
  `<think>\\n`, and the model writes its reasoning).
- Gemma 4 31B: the HF chat template at the pinned revision, system + user messages,
  `add_generation_prompt=True, enable_thinking=False`, as the model card's usage example. Gemma 4
  has a native system turn (card: "Native System Prompt Support"; template: the first system
  message becomes `<|turn>system\\n{content | trim}<turn|>`). With thinking off the template's
  generation prompt is `<|turn>model\\n<|channel>thought\\n<channel|>`: the card says the 31B model
  "will still generate the tags but with an empty thought block" when thinking is off, and this
  template (published 2026-07-09) writes that empty block into the prompt itself, the analogue of
  Qwen's empty think block. So Gemma's ids end at the model-turn header plus the template's own
  empty thought channel, and nothing of ours follows. Thinking on is not defined for Gemma (no
  `contracts.SAMPLING[("gemma", True)]`) and is refused.
- No prefill, ever: `scenario.pinned_rollout` is never read into a prompt (a scenario whose
  system or text contains its pinned rollout is refused). `rollout_start == len(ids)`.

Tokenizers load at `contracts.MODEL_REV` (local HF cache first) and are cached per model.
"""

from __future__ import annotations

import functools
from typing import Any

from emx.i9 import contracts as C

# generation_config.json `eos_token_id` at the pinned revisions (checked by
# tests/i9/test_render.py against the cached files). Qwen: <|im_end|>, <|endoftext|>.
# Gemma 4: <eos>, <turn|>, <|tool_response>.
STOP_TOKEN_IDS: dict[str, tuple[int, ...]] = C.STOP_TOKEN_IDS


def _check_model(model: str) -> None:
    if model not in C.MODELS:
        raise ValueError(f"unknown model {model!r}; expected one of {C.MODELS}")


@functools.lru_cache(maxsize=4)
def load_tokenizer(model: str) -> Any:
    """The model's tokenizer at its pinned revision (local HF cache first, then the Hub)."""
    from transformers import AutoTokenizer

    _check_model(model)
    repo, rev = C.MODEL_ID[model], C.MODEL_REV[model]
    try:
        return AutoTokenizer.from_pretrained(repo, revision=rev, local_files_only=True)
    except OSError:
        return AutoTokenizer.from_pretrained(repo, revision=rev)


def messages(scenario: C.Scenario) -> list[dict[str, str]]:
    """System (only when non-empty, as wsbench's "chat" render) + user. Never the rollout."""
    if scenario.pinned_rollout and (
        scenario.pinned_rollout in (scenario.system or "")
        or scenario.pinned_rollout in (scenario.text or "")
    ):
        raise ValueError(f"{scenario.id}: the prompt contains the pinned rollout")
    system = [{"role": "system", "content": scenario.system}] if scenario.system else []
    return [*system, {"role": "user", "content": scenario.text or ""}]


def render_ids(
    model: str, scenario: C.Scenario, *, thinking: bool, tokenizer: Any = None
) -> list[int]:
    """Prompt token ids for `scenario` under `model` ("qwen" or "gemma") and the thinking arm.
    The single renderer for sampling, capture, probes and readers (implementation plan §1.4)."""
    _check_model(model)
    if model == "gemma" and thinking:
        raise ValueError("Gemma is sampled with thinking off only (prereg v0.5 §4.1)")
    tok = tokenizer if tokenizer is not None else load_tokenizer(model)
    out = tok.apply_chat_template(
        messages(scenario), tokenize=True, add_generation_prompt=True, enable_thinking=thinking
    )
    return [int(i) for i in (out["input_ids"] if hasattr(out, "keys") else out)]


def rollout_start(
    model: str, scenario: C.Scenario, *, thinking: bool, tokenizer: Any = None
) -> int:
    """Index of the first generated token: every prompt position is < this (no prefill)."""
    return len(render_ids(model, scenario, thinking=thinking, tokenizer=tokenizer))
