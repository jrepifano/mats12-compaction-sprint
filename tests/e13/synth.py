"""Synthetic E13 generation and judged rows for tests (no tests here).

`gen_rows` builds GEN_COLUMNS + GEN_EXTRA rows for one battery and model with the real condition
strings and seeds; `judged_frame` builds the matching judged CSV columns (score, status and raw
per metric and judge) from a callable that decides each row's outcome, so rule tests can plant
exact EM, refusal and success rates.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, request_seed, score_column, status_column
from emx.e13 import contracts as C

# outcome(row, metric, judge) -> (score | None, status, raw)
Outcome = Callable[[dict, str, str], tuple[float | None, str, str]]


def gen_rows(battery: str, model_key: str, qids: dict[str, list[str]], *, site: str = "none",
             alpha: float = 0.0, conds: tuple[str, ...] | None = None,
             samples: dict[str, int] | None = None, backend: str = "vllm") -> list[dict]:
    """Rows for `battery` with `qids` = {qset: [qid, ...]}; instance and family left synthetic."""
    b = C.BATTERIES[battery]
    out = []
    for cond in conds or b.conds:
        cstr = C.condition(battery, cond, site, alpha)
        for qset, ids in qids.items():
            task = {"general": "em", "ext48": "em", "umed": "umed", "ugen": "ugen",
                    "narrow": "narrow"}[qset]
            for qid in ids:
                for k in range((samples or b.samples)[qset]):
                    out.append({
                        "condition": cstr, "cell": model_key, "eval_fmt": C.EVAL_FMT,
                        "qset": qset, "qid": qid, "question": f"question {qid}",
                        "sample_idx": k, "seed": request_seed(cstr, qid, k),
                        "prompt_text": f"<prompt {qid}>", "response": f"answer {qid} {k}",
                        "finish_reason": "stop", "n_new_tokens": 50,
                        "model_key": model_key, "battery": battery, "notice_cond": cond,
                        "family": "", "instance_id": "", "site": site, "alpha": alpha,
                        "task_type": task, "backend": backend,
                    })
    return out


def gen_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[*GEN_COLUMNS, *C.GEN_EXTRA])


def judged_frame(rows: list[dict], outcome: Outcome,
                 judges: tuple[str, ...] = C.JUDGES) -> pd.DataFrame:
    """Judged CSV columns (KEY_COLUMNS, then per metric/judge score, status, raw) for `rows`."""
    df = pd.DataFrame([{k: r[k] for k in KEY_COLUMNS} for r in rows])
    metrics = sorted({m for r in rows for m in C.METRICS_BY_TASK[r["task_type"]]})
    for m in metrics:
        for j in judges:
            sc, st, raw = [], [], []
            for r in rows:
                if m in C.METRICS_BY_TASK[r["task_type"]]:
                    s, t, w = outcome(r, m, j)
                else:
                    s, t, w = None, None, None
                sc.append(s)
                st.append(t)
                raw.append(w)
            df[score_column(m, j)] = pd.array(sc, dtype="Float64")
            df[status_column(m, j)] = st
            df[C.raw_column(m, j)] = raw
    return df
