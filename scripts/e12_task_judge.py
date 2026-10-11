"""E12 task-quality judge (prereg §4.4; plan U6): luna scores each held-out medical answer 0-100
against the paired good answer with the paper's rubric, through `emx.e12.llm.run_batch`.

- **Requires the `final` decision** (`decisions_sha256` verified, recorded in every file's header
  and line). Models: base, P1-P3 and the endpoint's A/B/C (with no endpoint, base and P1-P3).
  Each `gen/task/<key>` output must verify on its own (CSV sha256 and row count against its
  meta); a job with a terminal skip record is reported and not judged; anything else refuses.
- **Rubric:** `prompts/E12/task_judge_v1.txt`, a verbatim copy of the working repo's
  `prompts/task_judge_v1.txt` (sha256 `contracts.TASK_PROMPT_SHA256`, checked before use), with
  the `{question}` (the holdout prompt), `{reference}` (its `good_completion`, from
  `emx.e12.data.holdout_items`) and `{answer}` (the model's response) slots of the paper's
  `scripts/judge_task.py`.
- **Scoring mode:** logprob, as the paper's judge (`judge_task.py`: max_tokens 1, temperature 0,
  seed 0, logprobs, top_logprobs 20; score = the probability-weighted mean over the 0..100
  numeric first-token logprobs, None below 0.25 numeric weight). E12 uses luna's
  probe-confirmed request (`emx.judge_panel.PRESETS["luna"]`: gpt-6-luna, max_completion_tokens
  8, reasoning none, top_logprobs 5) with the same aggregation (`emx.judge_client`); the probe
  record must confirm it before any call. A None score is status "none" and is not retried.
- **Output:** `results/E12/<recipe>/task/<key>.jsonl` (`contracts.task_scores`): a header, then
  one line per answer with condition, qid, sample_idx, score, status, raw, usage, prompt_sha256,
  answer_sha256, reference_sha256, model, settings_sha256, decisions_sha256.

Before any call the cost of ALL remaining answers is projected and checked with
`emx.spend.check_total(..., "judge", exp="E12")`; over a cap it exits 2 and sends nothing.
`--dry-run` stops after the projection: no keys read, no clients created, nothing written.

Usage:
  uv run python scripts/e12_task_judge.py [--recipe R] [--dry-run] [--concurrency N]
      [--running-pod-usd X]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from emx import judge_panel as JP
from emx import spend
from emx.e12 import contracts as C
from emx.e12 import llm
from emx.e12.style_audit import fill

TAG = "E12 TASK JUDGE"


def model_keys(final: dict) -> list[str]:
    keys = [C.BASE_KEY] + [f"P{s}" for s in C.TRAIN_SEEDS]
    if final.get("endpoint") is not None:
        keys += C.endpoint_keys(int(final["endpoint"]))
    return keys


def load_rubric(root: Path) -> str:
    p = root / C.TASK_PROMPT
    text = p.read_text(encoding="utf-8")
    if llm.sha256_text(text) != C.TASK_PROMPT_SHA256:
        raise llm.Refused(f"{C.TASK_PROMPT} sha256 differs from contracts.TASK_PROMPT_SHA256")
    return text


def references(root: Path) -> dict[str, str]:
    from emx.e12 import data as D

    return {it["qid"]: it["good_completion"] for it in D.holdout_items(root)}


def build_plans(root: Path, recipe: str) -> tuple[list[tuple], list[str], str]:
    """(plans, skipped keys, decisions_sha256)."""
    final, dsha = llm.require_final(root, recipe)
    rubric = load_rubric(root)
    refs = references(root)
    settings = llm.make_settings("logprob", judge=C.TASK_JUDGE)
    sset = llm.canonical_sha256(settings)
    plans, skipped = [], []
    for key in model_keys(final):
        state = llm.output_state(root, recipe, "task", key)
        if state == "skipped":
            skipped.append(key)
            continue
        if state != "verified":
            raise llm.Refused(f"task/{key}: output {state}, not verified")
        p = root / C.gen_csv(recipe, "task", key)
        with open(p, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        items = []
        for r in rows:
            if r["qset"] != C.TASK_QSET or r["qid"] not in refs:
                raise llm.Refused(f"{p}: row {r['qid']}/{r['sample_idx']} is not a holdout task")
            ref = refs[r["qid"]]
            items.append({
                "item_id": f"{r['qid']}/{r['sample_idx']}", "condition": r["condition"],
                "qid": r["qid"], "sample_idx": int(r["sample_idx"]),
                "reference_sha256": llm.sha256_text(ref), "prompt_sha256": C.TASK_PROMPT_SHA256,
                "model": settings["model"], "settings_sha256": sset, "decisions_sha256": dsha,
                "_question": r["question"], "_reference": ref, "_answer": r["response"],
            })
        header = llm.make_header(source_sha256=llm.sha256_file(p),
                                 prompt_sha256=C.TASK_PROMPT_SHA256, settings=settings,
                                 decisions_sha256=dsha)

        def render(it, rubric=rubric):
            return fill(rubric, question=it["_question"], reference=it["_reference"],
                        answer=it["_answer"])

        plans.append((items, render, root / C.task_scores(recipe, key), header,
                      f"{C.NOTE_PREFIX} {recipe}/task/{key} {C.TASK_JUDGE} task", None))
    return plans, skipped, dsha


def main(argv=None, *, client=None) -> int:
    """`client` is a test hook (a fake AsyncOpenAI-like client); the CLI passes none."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--recipe", choices=sorted(C.RECIPES), default=C.DEFAULT_RECIPE)
    ap.add_argument("--dry-run", action="store_true", help="print the projection only")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--running-pod-usd", type=float, default=None)
    ap.add_argument("--probe", default=str(JP.PROBE_PATH))
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        plans, skipped, dsha = build_plans(root, args.recipe)
        if not args.dry_run and client is None:
            JP.check_probe(JP.PRESETS[C.TASK_JUDGE], Path(args.probe))
        out = llm.run_plans(plans, mode="logprob", dry_run=args.dry_run, client=client,
                            concurrency=args.concurrency,
                            running_pod_usd=args.running_pod_usd, tag=TAG)
    except spend.BudgetExceeded as e:
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        print(json.dumps({"refused": "budget", "breakdown": e.breakdown}))
        return C.EXIT_BUDGET
    except (llm.Refused, llm.FingerprintMismatch, ValueError, OSError, RuntimeError) as e:
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        print(json.dumps({"refused": "gate", "error": str(e)}))
        return C.EXIT_REFUSED
    out |= {"decisions_sha256": dsha, "skipped": skipped}
    if skipped:
        print(f"{TAG}: skipped (terminal skip records) {skipped}")
    print(f"{TAG} DONE" + ("" if args.dry_run else f": ${out['charged_usd']:.4f}"))
    print(json.dumps(out, default=str))
    return C.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
