"""P41 task set (plan "Setup"): X3's 15 tasks plus the next tasks in X3's frozen replacement
order, from the same ImpossibleBench conflicting-LCB parquet at X3's pinned revision, each
passing X3's strict impossibility check (`x3.tasks.verify_check_body`) and X3's size limit.

Writes data/tasks.json in X3's manifest schema (`x3.tasks.write_manifest`), so X3's loaders,
workspace builder, pass check and detector take it unchanged. The first 15 must regenerate X3's
`configs/X3/tasks.json` exactly (checked).

    PYTHONPATH=probes/P41-grader-belief-atrisk:src:scripts uv run --group swarm \
        --with pyarrow==25.0.1 python -m p41.tasks [--n 30] [--parquet PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from p41 import contracts as C

DEFAULT_PARQUET = (C.MAIN_CHECKOUT / ".claude" / "worktrees" / "x3-build" / "data" / "X3" / "raw"
                   / "data" / "conflicting-00000-of-00001.parquet")


def build(n: int, parquet: Path):
    import x3_prep_tasks as P

    from x3 import contracts as X3C
    from x3 import tasks as T

    sha = T.sha256_file(parquet)
    if sha != T.PARQUET_SHA256:
        raise SystemExit(f"parquet sha256 {sha} != X3's {T.PARQUET_SHA256}")
    files, results, by_id = P.verify_all(T.load_rows(parquet))
    elig = P.eligible_ids(files, results)
    x3 = T.load_manifest(X3C.TASKS_JSON)
    drawn = [t.task_id for t in x3.tasks]
    if sorted(T.draw_ids(elig)) != sorted(drawn) or x3.replacements:
        raise SystemExit("X3's draw does not regenerate (or X3 has replacements)")
    order = drawn + T.replacement_order(elig, drawn)
    if n > len(order):
        raise SystemExit(f"only {len(order)} eligible tasks")
    specs = tuple(T.task_spec(t, by_id[t]["entry_point"], files[t], results[t].conflict_pair)
                  for t in order[:n])
    for a, b in zip(specs[: len(drawn)], x3.tasks, strict=True):
        if a != b:
            raise SystemExit(f"{a.task_id}: regenerated spec differs from X3's")
    draw = (f"X3's 15 ({T.DRAW_EXPR}), then x3.tasks.replacement_order(eligible, drawn)"
            f"[:{n - len(drawn)}] (seed {X3C.SEED_REPLACEMENT}); {len(elig)} eligible")
    return X3C.TaskManifest(schema_version=T.SCHEMA_VERSION, dataset=X3C.DATASET,
                            split=X3C.DATASET_SPLIT, revision=X3C.DATASET_REVISION,
                            parquet_sha256=sha, draw=draw, replacements=(), tasks=specs)


def main(argv: list[str] | None = None) -> int:
    from x3 import tasks as T

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=C.N_TASKS)
    ap.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    a = ap.parse_args(argv)
    m = build(a.n, a.parquet)
    T.write_manifest(m, C.TASKS_JSON)
    print(f"wrote {len(m.tasks)} tasks to {C.TASKS_JSON}: {[t.task_id for t in m.tasks]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
