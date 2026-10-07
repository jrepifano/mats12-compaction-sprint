"""E9 provenance check: every reported number traces to a committed artifact (plan v4 row P;
prereg §4.14; `/goal` condition 6). Exit 0 only if every check is clean.

Checks:
  1. recompute: every analysis record in results/E9/analysis/ (those with a `command`) is
     recomputed from the committed inputs under its own recorded config and must equal the
     committed record (volatile fields `utc`, `code_commit` excepted);
  2. files: every analysis record is committed and unmodified; every file in a record's
     `inputs` exists with the recorded sha256 and is either committed and unmodified, or listed
     with the same sha256 in a committed sha manifest (`MANIFEST.sha256` / `*.sha256` lines
     "<sha256>  <path>", paths relative to the manifest's directory or the repo root; or a
     committed `DONE.json`'s `artifacts`, relative to its directory);
  3. tables: in every report file, each table between `<!-- e9-table:<analysis>:<table>
     sha256=<record sha> -->` and `<!-- /e9-table -->` names a record whose current sha256 is
     the marker's, equals the table re-rendered from that record, and every number in its data
     rows appears in that record. Tables outside markers are not checked (cost tables etc.).

Usage:
  uv run python scripts/e9_provenance_check.py [--root .] [--report PATH ...] [--no-recompute]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import replay as RP

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS = (C.REPORT_LOG, f"{C.ANALYSIS_DIR}/tables.md")
REPORT_GLOBS = ("docs/experiments/E9/report*.md",)
MARK = re.compile(r"<!-- e9-table:([\w-]+):([\w-]+) sha256=([0-9a-f]{64}) -->")
NUM = re.compile(r"-?\d+(?:\.\d+)?")
# a number in a table cell: not part of an identifier like "seed-11", "e9-bg-c1" or "a11"
CELL_NUM = re.compile(r"(?<![\w.\-])-?\d+(?:\.\d+)?(?![\w.])")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout


def git_state(root: Path) -> tuple[set[str], set[str]]:
    """(tracked paths, paths modified relative to HEAD)."""
    tracked = set(_git(root, "ls-files", "-z").split("\0")) - {""}
    modified = set()
    for line in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines():
        if line.strip():
            modified.add(line[3:].split(" -> ")[-1].strip('"'))
    return tracked, modified


def manifest_hashes(root: Path, committed: set[str]) -> dict[str, str]:
    """{relpath: sha256} from every committed sha manifest and DONE.json."""
    out: dict[str, str] = {}
    for rel in sorted(committed):
        p = root / rel
        name = Path(rel).name
        if not p.is_file():
            continue
        base = Path(rel).parent
        if name == C.MANIFEST or name.endswith(".sha256"):
            for line in p.read_text().splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                    continue
                path = parts[1].lstrip("*")
                for key in ((base / path).as_posix(), path):  # manifest-relative or root
                    out.setdefault(key, parts[0])
        elif name == C.DONE:
            try:
                arts = json.loads(p.read_text()).get("artifacts") or {}
            except ValueError:
                continue
            for a, sha in arts.items():
                out[(base / a).as_posix()] = sha
    return out


def numbers_in(rec) -> set[str]:
    out: set[str] = set(NUM.findall(json.dumps(rec)))

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            out.add(RP.fmt(x))

    walk(rec)
    return out


def check_tables(root: Path, report: str, problems: list[str]) -> int:
    text = (root / report).read_text()
    n = 0
    for m in MARK.finditer(text):
        n += 1
        name, table, sha = m.groups()
        end = text.find(RP.END_MARKER, m.end())
        where = f"{report}: table {name}:{table}"
        if end < 0:
            problems.append(f"{where}: no end marker")
            continue
        block = text[m.end():end].strip("\n")
        rel = RP.record_path(name)
        p = root / rel
        if not p.is_file():
            problems.append(f"{where}: record {rel} missing")
            continue
        if RP.sha256_file(p) != sha:
            problems.append(f"{where}: stale (record sha256 differs from the marker's)")
            continue
        rec = json.loads(p.read_text())
        if name not in RP.TABLES or table not in RP.TABLES[name]:
            problems.append(f"{where}: unknown table")
            continue
        if block.strip() != RP.render_table(name, table, rec).strip():
            problems.append(f"{where}: differs from the table rendered from {rel}")
        known = numbers_in(rec)
        for line in block.splitlines()[2:]:
            for tok in CELL_NUM.findall(line):
                if tok not in known:
                    problems.append(f"{where}: number {tok} not in {rel}")
    return n


def run(root: Path, reports: list[str] | None = None, recompute: bool = True) -> list[str]:
    problems: list[str] = []
    try:
        tracked, modified = git_state(root)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return [f"not a git checkout: {e}"]
    mans = manifest_hashes(root, tracked - modified)
    adir = root / C.ANALYSIS_DIR
    recs = []
    for p in sorted(adir.glob("*.json")) if adir.is_dir() else []:
        rec = json.loads(p.read_text())
        rel = p.relative_to(root).as_posix()
        if rel not in tracked or rel in modified:
            problems.append(f"{rel}: analysis record not committed (or modified)")
        recs.append((rel, rec))
    for rel, rec in recs:
        for inp, sha in (rec.get("inputs") or {}).items():
            f = root / inp
            if not f.is_file():
                problems.append(f"{rel}: input {inp} missing")
                continue
            got = RP.sha256_file(f)
            if got != sha:
                problems.append(f"{rel}: input {inp} sha256 {got[:12]} != recorded {sha[:12]}")
                continue
            committed = inp in tracked and inp not in modified
            if not committed and mans.get(inp) != sha:
                problems.append(f"{rel}: input {inp} neither committed nor in a committed "
                                f"sha manifest with a matching hash")
        cmd = (rec.get("command") or {}).get("name")
        if recompute and cmd in RP.COMPUTE:
            try:
                new = RP.compute(cmd, rec["config"], root, rec["command"].get("args"))
            except Exception as e:  # noqa: BLE001 - reported, not raised
                problems.append(f"{rel}: recompute failed: {type(e).__name__}: {e}")
                continue
            if RP.canonical(new) != RP.canonical(rec):
                diffs = RP.diff_outcomes(rec, new)
                problems.append(f"{rel}: recomputed record differs from the committed one"
                                + (f" (outcomes: {diffs[:3]})" if diffs else " (numbers)"))
    paths = list(reports) if reports else [
        *[r for r in DEFAULT_REPORTS if (root / r).is_file()],
        *sorted(str(p.relative_to(root)) for g in REPORT_GLOBS for p in root.glob(g))]
    for rep in paths:
        if not (root / rep).is_file():
            problems.append(f"report {rep} missing")
            continue
        check_tables(root, rep, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--report", action="append")
    ap.add_argument("--no-recompute", action="store_true")
    a = ap.parse_args(argv)
    problems = run(Path(a.root).resolve(), a.report, not a.no_recompute)
    for p in problems:
        print(f"PROVENANCE: {p}")
    print(f"E9_PROVENANCE {'CLEAN' if not problems else 'FAILED'} ({len(problems)} problems)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
