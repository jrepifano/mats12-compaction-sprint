"""X3 U1: freeze the task set (implementation plan §1.3; X1p prereg §4.2).

    uv run --group swarm --with pyarrow==25.0.1 python scripts/x3_prep_tasks.py [prep] [--force]
    uv run --group swarm --with pyarrow==25.0.1 python scripts/x3_prep_tasks.py \
        replace <task_id> --reason "..."

`prep` downloads the `conflicting` parquet at the pinned revision (free; the only network
access), checks its sha256, verifies all rows, draws 15 tasks, and writes configs/X3/tasks.json,
data/X3/task_verify.csv and data/X3/task_audit.md. `replace` swaps one drawn task for the next id
in the replacement queue and logs it in `replacements`. pyarrow is passed with `--with` because
the swarm group does not pin a parquet engine.
"""
from __future__ import annotations

import argparse
import csv
import sys

from x3 import contracts as C
from x3 import tasks as T

CSV_COLUMNS = ("task_id", "parsed", "cond1", "cond2", "cond3", "cond4", "verified", "test_chars",
               "selected")


def download() -> str:
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(repo_id=C.DATASET, repo_type="dataset", revision=C.DATASET_REVISION,
                           filename=T.PARQUET_FILENAME, local_dir=T.RAW_DIR)
    sha = T.sha256_file(T.PARQUET_PATH)
    print(f"parquet {path} sha256 {sha}")
    if sha != T.PARQUET_SHA256:
        sys.exit(f"parquet sha256 {sha} != expected {T.PARQUET_SHA256}")
    return sha


def verify_all(rows: list[dict]) -> tuple[dict, dict, dict]:
    """task_id -> converted files, VerifyResult, row."""
    files, results, by_id = {}, {}, {}
    for row in rows:
        tid = row["task_id"]
        files[tid] = T.convert_row(row)
        results[tid] = T.verify_check_body(files[tid][T.TEST_FILE], task_id=tid)
        by_id[tid] = row
    return files, results, by_id


def eligible_ids(files: dict, results: dict) -> list[str]:
    return sorted(t for t, r in results.items()
                  if r.verified and len(files[t][T.TEST_FILE]) < C.MAX_TEST_CHARS)


def write_csv(files: dict, results: dict, selected: set[str]) -> None:
    C.TASK_VERIFY_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(C.TASK_VERIFY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for tid in sorted(results, key=lambda t: (len(t), t)):
            r = results[tid]
            w.writerow([tid, r.parsed, r.cond1, r.cond2, r.cond3, r.cond4, r.verified,
                        len(files[tid][T.TEST_FILE]), tid in selected])


def _other_statements(test_src: str) -> int:
    """Statements in the test function other than the wrapper line and candidate asserts."""
    import ast

    fn = next(n for n in ast.parse(test_src).body if isinstance(n, ast.FunctionDef))
    return sum(1 for s in fn.body[1:] if T.candidate_assert(s) is None)


def write_audit(manifest: C.TaskManifest, results: dict, n_eligible: int) -> None:
    n_ver = sum(r.verified for r in results.values())
    out = [
        "# X3 task audit sheet",
        "",
        (f"Dataset `{manifest.dataset}`, split `{manifest.split}`, revision "
         f"`{manifest.revision}`, parquet sha256 `{manifest.parquet_sha256}`."),
        (f"Verified {n_ver} of {len(results)}; eligible (verified, test file under "
         f"{C.MAX_TEST_CHARS:,} chars) {n_eligible}. Draw: `{manifest.draw}`."),
        "",
        "For each task: confirm X1p §4.2 conditions 1-4 on the marked pair, and that the test",
        "depends on nothing but `candidate` and literals. Line numbers are those of",
        "`test_solution.py` as the agent sees it; `>>` marks the conflicting pair.",
        "",
    ]
    if manifest.replacements:
        out += ["Replacements:", ""]
        out += [f"- `{r['replaced']}` -> `{r['by']}`: {r['reason']}" for r in manifest.replacements]
        out.append("")
    for i, t in enumerate(manifest.tasks, 1):
        a, b = t.conflict_pair
        ea, eb = T.conflict_expected(t.test_solution_py, t.conflict_pair)
        lines = t.test_solution_py.splitlines()
        width = len(str(len(lines)))
        numbered = [f"{'>>' if n in (a, b) else '  '} {n:>{width}} | {line}"
                    for n, line in enumerate(lines, 1)]
        out += [
            f"## {i}. `{t.task_id}` (`{t.entry_point}`)",
            "",
            f"- Conflicting pair: line {a} expects `{ea}`, line {b} expects `{eb}`.",
            f"  - line {a}: `{lines[a - 1].strip()}`",
            f"  - line {b}: `{lines[b - 1].strip()}`",
            (f"- Verifier: {results[t.task_id].reason}. Test file "
             f"{len(t.test_solution_py):,} chars, sha256 `{t.test_sha256}`."),
            ("- Statements other than candidate asserts in the test function: "
             f"{_other_statements(t.test_solution_py)}."),
            "- [ ] Jacob: conditions 1-4 confirmed  - [ ] agent: conditions 1-4 confirmed",
            "",
            "```text",
            *numbered,
            "```",
            "",
        ]
    unver = sorted((r for r in results.values() if not r.verified),
                   key=lambda r: (len(r.task_id), r.task_id))
    out += ["## Unverified tasks", ""]
    out += [f"- `{r.task_id}`: {r.reason}" for r in unver]
    C.TASK_AUDIT_MD.write_text("\n".join(out) + "\n", encoding="utf-8")


def prep(force: bool) -> None:
    if C.TASKS_JSON.exists() and not force:
        old = T.load_manifest()
        if old.replacements:
            sys.exit(f"{C.TASKS_JSON} has {len(old.replacements)} replacement(s); "
                     "re-running prep would drop them (use --force)")
    sha = download()
    rows = T.load_rows()
    files, results, by_id = verify_all(rows)
    elig = eligible_ids(files, results)
    drawn = T.draw_ids(elig)
    specs = tuple(T.task_spec(t, by_id[t]["entry_point"], files[t], results[t].conflict_pair)
                  for t in drawn)
    manifest = C.TaskManifest(schema_version=T.SCHEMA_VERSION, dataset=C.DATASET,
                              split=C.DATASET_SPLIT, revision=C.DATASET_REVISION,
                              parquet_sha256=sha, draw=T.DRAW_EXPR, replacements=(),
                              tasks=specs)
    T.write_manifest(manifest)
    write_csv(files, results, set(drawn))
    write_audit(manifest, results, len(elig))
    n_ver = sum(r.verified for r in results.values())
    n_parsed = sum(r.parsed for r in results.values())
    print(f"rows {len(rows)} parsed {n_parsed} verified {n_ver} eligible {len(elig)}")
    print("drawn", drawn)


def replace(task_id: str, reason: str) -> None:
    manifest = T.load_manifest()
    ids = [t.task_id for t in manifest.tasks]
    if task_id not in ids:
        sys.exit(f"{task_id} is not in the current task set: {ids}")
    if T.sha256_file(T.PARQUET_PATH) != manifest.parquet_sha256:
        sys.exit("local parquet does not match the manifest's sha256; run prep's download first")
    files, results, by_id = verify_all(T.load_rows())
    elig = eligible_ids(files, results)
    queue = T.replacement_order(elig, T.draw_ids(elig))
    k = len(manifest.replacements)
    if k >= len(queue):
        sys.exit("replacement queue exhausted")
    new = queue[k]
    assert new not in ids, f"{new} already drawn"
    spec = T.task_spec(new, by_id[new]["entry_point"], files[new], results[new].conflict_pair)
    tasks = tuple(spec if t.task_id == task_id else t for t in manifest.tasks)
    reps = (*manifest.replacements, {"replaced": task_id, "by": new, "reason": reason})
    manifest = C.TaskManifest(**{**manifest.__dict__, "replacements": reps, "tasks": tasks})
    T.write_manifest(manifest)
    write_csv(files, results, {t.task_id for t in tasks})
    write_audit(manifest, results, len(elig))
    print(f"replaced {task_id} by {new} (queue position {k})")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("prep", help="download, verify, draw, write outputs (default)")
    p.add_argument("--force", action="store_true", help="overwrite a manifest with replacements")
    r = sub.add_parser("replace", help="replace one drawn task with the next in the queue")
    r.add_argument("task_id")
    r.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "replace":
        replace(args.task_id, args.reason)
    else:
        prep(getattr(args, "force", False))


if __name__ == "__main__":
    main()
