"""The trained-adapter vLLM re-check (shared design note §3; prereg §4.8 check 2), on `P1`.

Owner: unit U3. FROZEN SIGNATURES (U0).

Three parts, as EQ0's lora-check (ported from scripts/eq0_pod.py: `lora_check_prompts`,
`_last_logprobs`, `vllm_topk`, `score_lora_check`), but on the TRAINED adapter:
1. `hf_part` (peft venv): the base and three HF variants of the trained adapter, its `lora_B`
   scaled x0.5, x1, x2 (`contracts.LORA_CHECK_SCALES`); next-token log-probs on the 20 prompts.
2. `vllm_part` (vllm venv): base and the x1 adapter through vLLM; top-20 next-token log-probs.
3. `score` (pure): per prompt, the HF variant nearest vLLM (mean |diff| over vLLM's top-20 ids);
   PASS when nearest x1 on >= 19 of 20 AND max |vLLM - HF x1| <= 1.25 x max |vLLM base - HF base|
   (both maxima over all prompts and top-20 ids). Writes `bench/lora_check.json` with
   `contracts.BENCH_FIELDS["lora_check"]`; `gen_path` = "vllm" on PASS, else "hf".
"""

from __future__ import annotations

from pathlib import Path

from emx.e12 import contracts as C


def prompts(tokenizer, root: str | Path = C.REPO_ROOT) -> list[tuple[str, list[int]]]:
    """The first 20 of the 56 questions in file order (general then ext48), rendered."""
    raise NotImplementedError


def hf_part(recipe: str, key: str = C.LORA_CHECK_KEY, root: str | Path = C.REPO_ROOT) -> dict:
    """Write HF log-probs (base and the scaled variants) under bench/; return their record."""
    raise NotImplementedError


def vllm_part(recipe: str, key: str = C.LORA_CHECK_KEY, root: str | Path = C.REPO_ROOT) -> dict:
    """Write vLLM top-20 log-probs (base and x1) under bench/; return their record."""
    raise NotImplementedError


def score(hf: dict, vllm: dict) -> dict:
    """Pure scoring of the two parts' records -> the lora_check record (with verdict)."""
    raise NotImplementedError
