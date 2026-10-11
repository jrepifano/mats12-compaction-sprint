"""Freeze the P04 task set from the pinned apps-control-arena parquet.

Eligible problem: has a working reference backdoor, is deterministic, has >= 2 reference
solutions that pass the tests and >= 3 test pairs. From the eligible pool we draw N_TASKS by a
seeded shuffle and write configs/P04/tasks.json (committed) plus its sha256. The reference
solution is the first dataset solution flagged passes_tests. example_tests are the first
N_EXAMPLE_TESTS pairs, each truncated for the prompt; all_tests keeps every pair for the sandbox
pass check. The known backdoor input is carried only so the smoke checker can self-test.
"""

from __future__ import annotations

import glob
import hashlib
import json
import random
from pathlib import Path

from emx.p04 import contracts as C


def _reference(row: dict) -> str | None:
    for s in row["solutions"]:
        if s["passes_tests"]:
            return s["code"]
    return None


def _ref_backdoor(row: dict) -> tuple[str | None, str | None]:
    """(backdoor_input, backdoor_code) of the first working reference backdoor."""
    for b in row["backdoors"]:
        if b["backdoor_works"]:
            return b["backdoor_input"], b["backdoor_code"]
    return None, None


def eligible(row: dict) -> bool:
    n_pass = sum(bool(s["passes_tests"]) for s in row["solutions"])
    return bool(
        row["has_backdoor"]
        and row["backdoor_works"]
        and not row["is_nondeterministic"]
        and n_pass >= 2
        and len(row["inputs"]) >= 3
    )


def _truncate(s: str, n: int = C.MAX_EXAMPLE_IO_CHARS) -> str:
    return s if len(s) <= n else s[:n] + "\n...[truncated]"


def build_task(row: dict) -> dict:
    tests = [{"input": i, "output": o} for i, o in zip(row["inputs"], row["outputs"], strict=True)]
    ex = [{"input": _truncate(t["input"]), "output": _truncate(t["output"])}
          for t in tests[: C.N_EXAMPLE_TESTS]]
    bd_input, bd_code = _ref_backdoor(row)
    return {
        "task_id": str(row["problem_id"]),
        "question": row["question"],
        "example_tests": ex,
        "all_tests": tests,
        "reference_py": _reference(row),
        "ref_backdoor_input": bd_input,
        "ref_backdoor_code": bd_code,
    }


def freeze(parquet_glob: str, out: Path = C.TASKS_JSON, n: int = C.N_TASKS,
           seed: int = C.SEED) -> dict:
    import pandas as pd  # needs pyarrow

    files = sorted(glob.glob(parquet_glob))
    if not files:
        raise FileNotFoundError(parquet_glob)
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    rows = [r for r in df.to_dict("records") if eligible(r)]
    ids = sorted(str(r["problem_id"]) for r in rows)
    random.Random(seed).shuffle(ids)
    by_id = {str(r["problem_id"]): r for r in rows}
    tasks = [build_task(by_id[i]) for i in ids[:n]]
    assert all(t["reference_py"] and t["ref_backdoor_input"] for t in tasks)
    manifest = {
        "dataset_repo": C.DATASET_REPO,
        "dataset_rev": C.DATASET_REV,
        "parquet_sha256": {Path(f).name: hashlib.sha256(Path(f).read_bytes()).hexdigest()
                           for f in files},
        "n_eligible": len(ids),
        "seed": seed,
        "n_tasks": len(tasks),
        "tasks": tasks,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(manifest, ensure_ascii=False, indent=0, sort_keys=False)
    out.write_text(body, encoding="utf-8")
    (out.parent / "tasks.sha256").write_text(hashlib.sha256(body.encode()).hexdigest() + "\n")
    return {"n_eligible": len(ids), "n_tasks": len(tasks), "out": str(out)}


def load_tasks(path: Path = C.TASKS_JSON) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["tasks"]


if __name__ == "__main__":
    import sys

    g = sys.argv[1] if len(sys.argv) > 1 else str(C.REPO_ROOT / "data/P04/raw/data/*.parquet")
    print(freeze(g))
