"""Combine per-worktree spend ledgers: total spend across all checkouts, and fold one into main's.

Each experiment is implemented in its own git worktree (AGENTS.md, "Implement each experiment in
its own git worktree"), and `emx.spend` writes to `logs/costs.jsonl` inside the checkout the code
runs from. So every worktree keeps its own ledger, and each experiment's caps are enforced
against it. This script is how the copies come back together (Jacob, 2026-10-04):

- `report`: the total across the main checkout and every worktree, read-only. Entries present in
  several copies (everything before a branch point) count once. Also lists, per worktree, the
  entries main doesn't have yet. Use it for the running total and for RunPod balance planning.
- `merge`: append to main's ledger the entries from one source that main doesn't have. The source
  is a ledger file, a worktree directory, or a git ref (`--from-ref e1b/run`, for a branch whose
  worktree is gone). Dry run unless `--write`. Idempotent: a second run adds nothing.
- `check`: parse every line; list ids repeated as identical rows, ids whose rows differ, and
  repeated id-less rows. Exit 1 on any problem. `--fix` removes exact repeats of an id, which is
  what a text merge leaves behind. Rows that share an id but differ are never removed.
- `stamp-ids`: give ids to the id-less rows of a branch ledger that aren't on main. Those rows
  were written after the branch point by code from before ids existed. `merge` refuses them until
  they are stamped, because their occurrence keys can't be compared across ledgers.

Identity. Every entry written since 2026-10-04 carries a unique `id` (uuid4 hex, stamped by
`emx.spend.record` and `s4world.budget.settle`), and entries are matched on it. Older rows have no
`id`. Those are matched on their canonical JSON plus their occurrence number within the ledger
they came from. A row shared from before a branch point is the first occurrence in every copy, so
it matches once. Two genuine identical charges in one ledger are its first and second occurrences,
so both survive. This only holds for rows shared from before the branch point. Id-less rows
written on two branches after it could collide, so `merge` refuses id-less rows the target lacks
(stamp them with `stamp-ids`), and `report` warns about them. An id-less row repeated within one
ledger is ambiguous: `check` reports it and never removes it.

Locking. Reads take a shared `fcntl.flock`; appends and `check --fix` take an exclusive one and
read the file inside the same lock. `--fix` rewrites in place (truncate, then write), never by
rename, because a writer that opened the file before the rename would append to the orphaned copy.
Git itself takes no lock, so stop paid jobs in both checkouts and commit the ledger before any git
merge that touches it (AGENTS.md).

`.gitattributes` sets `logs/costs.jsonl merge=union`, so a local `git merge` keeps both sides'
appended lines instead of conflicting. Union is a text merge, not reconciliation: it can repeat
an entry that reached both sides by different routes. So after every merge, run `check --fix`,
then `check`, and commit.

Usage:
  uv run python scripts/merge_ledger.py report [--json]
  uv run python scripts/merge_ledger.py merge --from .claude/worktrees/e1b-run [--write]
  uv run python scripts/merge_ledger.py merge --from-ref e1b/run [--write]
  uv run python scripts/merge_ledger.py check [PATH] [--fix]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

LEDGER_REL = Path("logs") / "costs.jsonl"
MAIN_BRANCH = "refs/heads/main"


class LedgerError(RuntimeError):
    pass


def canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True)


def keys(entries: list[dict]) -> list[tuple]:
    """One identity per entry: its `id`, or for id-less rows (canonical JSON, occurrence number)."""
    seen: dict[str, int] = defaultdict(int)
    out = []
    for e in entries:
        if e.get("id"):
            out.append(("id", e["id"]))
        else:
            c = canonical(e)
            seen[c] += 1
            out.append(("legacy", c, seen[c]))
    return out


def parse(text: str, origin: str) -> list[dict]:
    """Parse ledger text; a malformed line raises, as in `emx.spend.read_entries`."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise LedgerError(f"{origin}:{i}: malformed ledger line: {e}") from e
    return out


def read_ledger(path: Path) -> list[dict]:
    if not path.is_file():
        raise LedgerError(f"no ledger at {path}")
    with open(path, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            text = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return parse(text, str(path))


def read_ref(ref: str, repo: Path) -> list[dict]:
    spec = f"{ref}:{LEDGER_REL.as_posix()}"
    r = subprocess.run(
        ["git", "-C", str(repo), "show", spec], capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        raise LedgerError(f"cannot read {spec}: {r.stderr.strip()}")
    return parse(r.stdout, spec)


@dataclass
class Worktree:
    path: Path
    branch: str | None  # e.g. refs/heads/main; None if detached
    bare: bool = False
    prunable: bool = False


def worktrees(repo: Path) -> list[Worktree]:
    """All checkouts of this repo, from `git worktree list --porcelain`; git lists the main one first."""
    text = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    out: list[Worktree] = []
    for block in text.strip().split("\n\n"):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines())
        out.append(
            Worktree(
                path=Path(fields["worktree"]),
                branch=fields.get("branch") or None,
                bare="bare" in fields,
                prunable="prunable" in fields,
            )
        )
    return out


def main_ledger(repo: Path) -> Path:
    """Main's ledger: the primary checkout, which must be a non-bare checkout of `main`."""
    first = worktrees(repo)[0]
    if first.bare:
        raise LedgerError(f"primary checkout {first.path} is bare; pass --into")
    if first.branch != MAIN_BRANCH:
        raise LedgerError(
            f"primary checkout {first.path} is on {first.branch or 'a detached HEAD'}, not main;"
            " pass --into"
        )
    return first.path / LEDGER_REL


def totals(entries: list[dict]) -> dict[str, float]:
    t: dict[str, float] = defaultdict(float)
    for e in entries:
        t[e.get("exp", "?")] += float(e.get("usd") or 0)
    return dict(sorted(t.items()))


def union(sources: list[list[dict]]) -> list[dict]:
    seen, out = set(), []
    for entries in sources:
        for k, e in zip(keys(entries), entries, strict=True):
            if k not in seen:
                seen.add(k)
                out.append(e)
    return out


def missing_from(target: list[dict], source: list[dict]) -> list[dict]:
    """Source rows the target lacks; a row repeated within the source is selected once."""
    have = set(keys(target))
    out = []
    for k, e in zip(keys(source), source, strict=True):
        if k not in have:
            have.add(k)
            out.append(e)
    return out


def refuse_new_legacy(new: list[dict], origin: str) -> None:
    """Id-less rows not already on the target were written after a branch point by pre-id code.
    Their occurrence keys can't be compared across ledgers, so they need ids first."""
    legacy = [e for e in new if not e.get("id")]
    if legacy:
        raise LedgerError(
            f"{origin}: {len(legacy)} id-less rows are not on the target {totals(legacy)}. They were"
            " written by pre-id code after the branch point and can't be matched safely across"
            f" ledgers. Stamp ids first: merge_ledger.py stamp-ids <that ledger> --write"
        )


def refuse_conflicts(entries: list[dict], origin: str) -> None:
    """Rows that share an id but differ (e.g. a corrected amount) must be resolved by hand first;
    importing by id would silently keep whichever came first."""
    conflicts = problems(entries)[2]
    if conflicts:
        raise LedgerError(
            f"{origin}: {len(conflicts)} ids have rows that differ; resolve by hand: {conflicts[:5]}"
        )


def append_missing(target: Path, source: list[dict], write: bool) -> list[dict]:
    """Append the source entries the target lacks, reading the target under the same lock."""
    if not target.is_file():
        raise LedgerError(f"no ledger at {target}")
    refuse_conflicts(source, "source")
    with open(target, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            text = f.read()
            current = parse(text, str(target))
            refuse_conflicts(current + source, str(target))
            new = missing_from(current, source)
            refuse_new_legacy(new, str(target))
            if write and new:
                f.seek(0, 2)
                if text and not text.endswith("\n"):
                    f.write("\n")
                f.writelines(json.dumps(e) + "\n" for e in new)
                f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return new


def problems(entries: list[dict]) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """(ids repeated with identical rows, id-less rows repeated, ids whose rows conflict)."""
    by_id: dict[str, list[str]] = defaultdict(list)
    legacy: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.get("id"):
            by_id[e["id"]].append(canonical(e))
        else:
            legacy[canonical(e)] += 1
    repeats = {i: len(rows) for i, rows in by_id.items() if len(rows) > 1 and len(set(rows)) == 1}
    conflicts = [i for i, rows in by_id.items() if len(set(rows)) > 1]
    return repeats, {k: n for k, n in legacy.items() if n > 1}, conflicts


def rewrite_in_place(path: Path, transform) -> list[dict]:
    """Apply `transform(rows) -> (out_lines, changed)` under the exclusive lock, where `rows` is a
    list of (original line, parsed entry). Untouched lines are written back verbatim. The rewrite
    is in place (truncate, then write), never by rename, so a writer waiting on the lock appends to
    this file rather than an orphaned copy."""
    with open(path, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
            rows = list(zip(lines, parse("\n".join(lines), str(path)), strict=True))
            out_lines, changed = transform(rows)
            if changed:
                f.seek(0)
                f.truncate()
                f.writelines(ln + "\n" for ln in out_lines)
                f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return changed


def drop_exact_id_repeats(path: Path) -> list[dict]:
    """Remove rows identical to an earlier row with the same `id`. Rows that share an id but differ
    are left alone and reported by `check`, so a correction can't vanish silently."""

    def t(rows):
        seen, kept, dropped = set(), [], []
        for line, e in rows:
            if e.get("id"):
                c = canonical(e)
                if c in seen:
                    dropped.append(e)
                    continue
                seen.add(c)
            kept.append(line)
        return kept, dropped

    return rewrite_in_place(path, t)


def stamp_ids(path: Path, main: list[dict], write: bool) -> list[dict]:
    """Give an id to each id-less row of `path` that isn't on main (by occurrence key): rows written
    after the branch point by pre-id code. Rows shared with main keep matching main's copies."""
    have = set(keys(main))

    def t(rows):
        out, stamped = [], []
        for k, (line, e) in zip(keys([e for _, e in rows]), rows, strict=True):
            if not e.get("id") and k not in have:
                e = {**e, "id": uuid.uuid4().hex}
                stamped.append(e)
                line = json.dumps(e)
            out.append(line)
        return out, stamped

    if write:
        return rewrite_in_place(path, t)
    entries = read_ledger(path)
    return [
        e for k, e in zip(keys(entries), entries, strict=True) if not e.get("id") and k not in have
    ]


def cmd_report(repo: Path, as_json: bool) -> int:
    trees = worktrees(repo)
    main_path = main_ledger(repo)
    main_entries = read_ledger(main_path)
    per_tree: dict[str, list[dict]] = {}
    skipped = []
    for t in trees:
        if t.bare:
            continue
        if t.prunable:
            skipped.append(
                f"{t.path} is prunable (gone from disk); use merge --from-ref {t.branch}"
            )
            continue
        per_tree[str(t.path)] = read_ledger(t.path / LEDGER_REL)
    combined = union(list(per_tree.values()))
    unmerged = {
        t: missing_from(main_entries, es)
        for t, es in per_tree.items()
        if Path(t) != main_path.parents[1]
    }
    # Id-less rows that main lacks were written after a branch point by pre-id code. Occurrence
    # keys can't match them across ledgers: a copy stamped in one worktree and not in another
    # counts twice, and identical charges on two branches collapse to one. So the total is
    # unreliable until they are stamped (`stamp-ids`).
    ambiguous = {t: sum(1 for e in es if not e.get("id")) for t, es in unmerged.items()}
    ambiguous = {t: n for t, n in ambiguous.items() if n}
    result = {
        "main": str(main_path),
        "reliable": not ambiguous,
        "ambiguous_idless_rows": ambiguous,
        "total_usd": round(sum(totals(combined).values()), 4),
        "by_exp_usd": {k: round(v, 4) for k, v in totals(combined).items()},
        "main_only_total_usd": round(sum(totals(main_entries).values()), 4),
        "unmerged": {
            t: {"entries": len(es), "by_exp_usd": {k: round(v, 4) for k, v in totals(es).items()}}
            for t, es in unmerged.items()
        },
        "skipped": skipped,
    }
    if as_json:
        print(json.dumps(result, indent=2))
        return 0 if result["reliable"] else 1
    print(
        ("" if result["reliable"] else "UNRELIABLE ")
        + f"Total across {len(per_tree)} checkouts: ${result['total_usd']:.2f} "
        f"(main's ledger alone: ${result['main_only_total_usd']:.2f})"
    )
    for exp, usd in result["by_exp_usd"].items():
        print(f"  {exp:10s} ${usd:8.2f}")
    for t, u in result["unmerged"].items():
        if u["entries"]:
            print(f"Not yet on main: {t}: {u['entries']} entries {u['by_exp_usd']}")
    for t, n in ambiguous.items():
        print(
            f"WARNING {t}: {n} unmerged rows have no id, so the total above may over- or"
            f" undercount. Run: merge_ledger.py stamp-ids {Path(t) / LEDGER_REL} --write"
        )
    for s in skipped:
        print(f"WARNING {s}")
    return 0 if result["reliable"] else 1


def cmd_merge(repo: Path, src: str | None, ref: str | None, into: str | None, write: bool) -> int:
    target = Path(into) if into else main_ledger(repo)
    if ref:
        source, origin = read_ref(ref, repo), ref
    else:
        p = Path(src)
        p = p / LEDGER_REL if p.is_dir() else p
        source, origin = read_ledger(p), str(p)
    before = totals(read_ledger(target))
    new = append_missing(target, source, write)
    print(
        f"{origin} -> {target}: {len(new)} new entries {totals(new)}"
        + ("" if write else " (dry run; pass --write to append)")
    )
    if write and new:
        after = totals(read_ledger(target))
        for exp in sorted(set(before) | set(after)):
            print(f"  {exp:10s} ${before.get(exp, 0):8.2f} -> ${after.get(exp, 0):8.2f}")
    return 0


def cmd_check(path: Path, fix: bool) -> int:
    if fix:
        dropped = drop_exact_id_repeats(path)
        print(f"removed {len(dropped)} exact repeats of an id {totals(dropped)}")
    entries = read_ledger(path)
    dup_ids, dup_legacy, conflicts = problems(entries)
    if conflicts:
        print(
            f"FAIL {path}: {len(conflicts)} ids have rows that differ (resolve by hand): {conflicts[:5]}"
        )
    if dup_ids:
        print(f"FAIL {path}: {len(dup_ids)} ids repeat as identical rows (run check --fix)")
    if dup_legacy:
        print(f"FAIL {path}: {len(dup_legacy)} id-less rows repeat (ambiguous; resolve by hand)")
        for k, n in list(dup_legacy.items())[:10]:
            print(f"  x{n} {k[:160]}")
    if dup_ids or dup_legacy or conflicts:
        return 1
    print(
        f"OK {path}: {len(entries)} entries, no duplicates, total ${sum(totals(entries).values()):.2f}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    m = sub.add_parser("merge")
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument("--from", dest="src")
    g.add_argument("--from-ref", dest="ref")
    m.add_argument("--into", default=None, help="target ledger (default: main checkout's)")
    m.add_argument("--write", action="store_true")
    s = sub.add_parser("stamp-ids")
    s.add_argument("path")
    s.add_argument("--write", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("path", nargs="?", default=None)
    c.add_argument("--fix", action="store_true")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    try:
        if a.cmd == "report":
            return cmd_report(repo, a.json)
        if a.cmd == "merge":
            return cmd_merge(repo, a.src, a.ref, a.into, a.write)
        if a.cmd == "stamp-ids":
            stamped = stamp_ids(Path(a.path), read_ledger(main_ledger(repo)), a.write)
            print(
                f"{a.path}: {len(stamped)} id-less rows not on main {totals(stamped)}"
                + (" stamped" if a.write else " (dry run; pass --write to stamp)")
            )
            return 0
        return cmd_check(Path(a.path) if a.path else main_ledger(repo), a.fix)
    except LedgerError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
