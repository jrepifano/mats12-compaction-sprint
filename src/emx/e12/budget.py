"""E12 workload projection and pre-set cuts (prereg §7; plan v0.3 "Paid steps").

Owner: unit U6. FROZEN SIGNATURES (U0). STDLIB ONLY: it also runs on the pod (the `smoke` and
`lora-check` steps admit or refuse the remaining workload).

Prices `contracts.planned_jobs(recipe, gen_path, endpoint, cuts)` (minus outputs that already
verify) plus the remaining training steps, with rates from the bench records
(`contracts.BENCH_FIELDS`), falling back to the shared note's rates (labelled in `rates_source`).
GPU hours = elapsed + remaining (training, generation, engine starts, applied smokes, upload,
margin); GPU $ = hours x the pod's rate; API $ = the judging the jobs imply (judges, task judge,
style audit) at $0.21-0.25 per 1,000 fully judged answers (upper rate used). Admit when GPU $ <=
GPU_LIMIT_USD, total pod hours <= POD_HOURS_LIMIT and API $ + recorded API spend <=
API_LIMIT_USD; otherwise apply cuts 0, 1, 2, ... in order and re-project, and refuse if all five
do not fit.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from emx.e12 import contracts as C

FALLBACK_RATES: dict[str, float] = {  # shared design note §5 (EQ0)
    "rank1_short_s_per_step": 0.187,
    "rank1_poison_s_per_step": 0.7,  # unmeasured guess until train_bench
    "rank32_factor": 2.0,  # unmeasured: rank-32 step time / rank-1 step time
    "vllm_answers_per_s": 30.0,  # conservative for grouped small jobs (EQ0 base: 77)
    "hf_answers_per_s": 5.0,  # prereg §7's 45k answers in 2-3 h
    "engine_start_s": 120.0,
    "judge_usd_per_1000_answers": 0.25,
    "overhead_h": 0.25,
    "margin_h": 0.3,
}


def load_rates(recipe: str, root: str | Path = C.REPO_ROOT) -> dict:
    """{name: value, "rates_source": {name: "measured:<file>" | "fallback"}} from the recipe's
    bench records where present."""
    raise NotImplementedError


def remaining_jobs(recipe: str, gen_path: str, endpoint: int | None, cuts: Sequence[int],
                   root: str | Path = C.REPO_ROOT) -> list[C.Job]:
    """`planned_jobs(...)` minus the (kind, key) outputs that already verify under `root`."""
    raise NotImplementedError


def remaining_train_steps(recipe: str, root: str | Path = C.REPO_ROOT) -> dict[str, int]:
    """{segment: steps still to train} from the status and train records."""
    raise NotImplementedError


def project(recipe: str, stage: str, *, gen_path: str, endpoint: int | None,
            cuts: Sequence[int], elapsed_pod_hours: float, gpu_rate: float,
            api_spent_usd: float, root: str | Path = C.REPO_ROOT) -> dict:
    """One projection record (`contracts.BENCH_FIELDS["projection"]`) for these cuts."""
    raise NotImplementedError


def admit(recipe: str, stage: str, *, gen_path: str, endpoint: int | None,
          elapsed_pod_hours: float, gpu_rate: float, api_spent_usd: float,
          root: str | Path = C.REPO_ROOT, write: bool = True) -> dict:
    """Project with no cuts, then with cuts 0..k added in order until it fits; return the first
    fitting projection (verdict "admit", with its cuts) or the all-cuts one (verdict "refuse").
    With `write`, writes `contracts.projection_path(recipe, stage)`."""
    raise NotImplementedError
