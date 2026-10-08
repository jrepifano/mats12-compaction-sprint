"""Synthetic EQ0 inputs for the tests (no tests here): generation CSVs with the real 56 qids,
judged CSVs with signed score columns, and bench JSONs, under a temporary root."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from emx import judge_run as JR
from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, score_column, status_column
from emx.eq0 import contracts as C
from emx.qwen import registry as R

Scores = dict[str, tuple[float | None, str]]  # column -> (score, status)
ScoreFn = Callable[[str, str, str, int, str], Scores]  # (model, qset, qid, sample, tag)


def questions() -> list[dict]:
    out = []
    for q in C.QSETS:
        p = C.REPO_ROOT / C.QUESTION_FILES[q]
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def setup_root(root: Path) -> Path:
    """Copy the frozen question files under `root` (analysis reads the near-domain tags)."""
    for q in C.QSETS:
        dst = root / C.QUESTION_FILES[q]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(C.REPO_ROOT / C.QUESTION_FILES[q], dst)
    return root


def gen_rows(model: str, response: Callable[[str, str, int, str], str] | None = None,
             n_tokens: int = 100) -> list[dict]:
    rows = []
    for q in questions():
        for s in range(C.SAMPLES[q["qset"]]):
            text = response(q["qset"], q["qid"], s, q["tag"]) if response else f"answer {s}"
            rows.append({
                "condition": model, "cell": model, "eval_fmt": R.EVAL_FMT, "qset": q["qset"],
                "qid": q["qid"], "question": q["question"], "sample_idx": s, "seed": 1000 + s,
                "prompt_text": f"<p>{q['qid']}", "response": text,
                "finish_reason": "length" if s == 0 else "stop", "n_new_tokens": n_tokens,
            })
    return rows


def write_gen(root: Path, model: str, **kw) -> Path:
    p = root / C.gen_csv(model)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(gen_rows(model, **kw), columns=list(GEN_COLUMNS)).to_csv(p, index=False)
    return p


def clean(model, qset, qid, s, tag) -> Scores:
    return {score_column(m, j): (90.0, "scored") for m in C.METRICS for j in C.JUDGES}


def em_every(k: int, aligned: float = 10.0, coherent: float = 80.0) -> ScoreFn:
    """EM (both judges) on every k-th sample of every question; clean elsewhere."""
    def fn(model, qset, qid, s, tag) -> Scores:
        out = clean(model, qset, qid, s, tag)
        if s % k == 0:
            for j in C.JUDGES:
                out[score_column("aligned", j)] = (aligned, "scored")
                out[score_column("coherent", j)] = (coherent, "scored")
        return out
    return fn


def write_judged(root: Path, model: str, fn: ScoreFn = clean) -> Path:
    gen_p = root / C.gen_csv(model)
    gen = JR.read_gen(gen_p)
    tags = {q["qid"]: q["tag"] for q in questions()}
    judged = gen[list(KEY_COLUMNS)].copy()
    cols: dict[str, list] = {}
    for _, r in gen.iterrows():
        sc = fn(model, r["qset"], str(r["qid"]), int(r["sample_idx"]), tags[str(r["qid"])])
        for m in C.METRICS:
            for j in C.JUDGES:
                v, st = sc[score_column(m, j)]
                cols.setdefault(score_column(m, j), []).append("" if v is None else v)
                cols.setdefault(status_column(m, j), []).append(st)
    for c, v in cols.items():
        judged[c] = v
    jp = root / C.judged_csv(model)
    jp.parent.mkdir(parents=True, exist_ok=True)
    judged.to_csv(jp, index=False)
    src = JR.source_block(gen_p, gen)
    for m in C.METRICS:
        for j in C.JUDGES:
            JR.sig_path(jp, m, j).write_text(json.dumps({"source": src}))
    return jp


def write_model(root: Path, model: str, fn: ScoreFn = clean, **kw) -> None:
    write_gen(root, model, **kw)
    write_judged(root, model, fn)


def write_bench(root: Path) -> None:
    d = root / C.BENCH_DIR
    d.mkdir(parents=True, exist_ok=True)
    # the shapes scripts/eq0_pod.py writes (generate, train-bench, lora-check steps)
    gen = {"per_model": {k: {"model_key": k, "n_requests": 1360, "wall_seconds": 360.0,
                             "n_generated_tokens": 136000, "generated_tokens_per_s": 377.78,
                             "gpu": "NVIDIA H100 80GB HBM3", "resumed": False}
                         for k in R.MODEL_KEYS},
           "rows_per_model": 1360}
    (d / "generation.json").write_text(json.dumps(gen))
    train = {"steps": [{"step": i + 1, "seconds": 10.0 if i == 0 else 2.0} for i in range(20)],
             "summary": {"n_steps": 20, "s_per_step": 2.0, "tokens_per_s": 5000.0},
             "peak_memory_bytes": 30 * 2**30, "gpu": "NVIDIA H100 80GB HBM3",
             "recipe": {"per_device_batch": 4, "grad_accum": 4},
             "gradient_checkpointing": False}
    (d / "train.json").write_text(json.dumps(train))
    (d / "lora_check.json").write_text(json.dumps({
        "passed": True, "tolerance_passed": True, "discrimination_passed": True,
        "shift_passed": True, "n_prompts": 20}))


def write_record(root: Path, inputs: dict, passed: bool = True) -> Path:
    """A minimal validation record.json scored on `inputs` (emx.eq0.analysis.load_all)."""
    from emx.eq0 import validation as V

    p = root / V.RECORD_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pass": passed, "inputs": {"sources": V.source_hashes(inputs)}}))
    return p


def write_smoke(root: Path, status: str = "passed") -> Path:
    """A smoke record with the `lora_applied` block scripts/eq0_pod.py writes (status only)."""
    p = root / C.SMOKE_RECORD
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"passed": status == "passed", "lora_applied": {"status": status}}))
    return p
