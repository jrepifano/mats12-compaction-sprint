"""E9 rewrite checks, checker validation and insert acceptance (unit B2b; prereg §4.4).

Subcommands:
  run      Start from data/E9/select/selection.json; write both versions of each document
           (emx.e9.rewrite), check them with the checker (A.1, A.4), validate the checker per
           writer stratum after the first pass and freeze the policies, regenerate failures once,
           replace dropped documents from ranked.json until 4M original tokens, repair the +-10%
           totals, and freeze data/E9/insert/{insert_manifest.json, versions.jsonl}.
           PAID (stage "setup", reserved through emx.e9.llm): refuses without --confirm-paid.
           --refreeze: rebuild a frozen insert after the exclusion registry changed
           (Amendment F), under the frozen checker policies; the previous manifest is archived
           and its sha256 and the reason (--reason, plus the registry change) recorded.
           --allow-dirty: run with uncommitted changes under src/ or scripts/ (each dirty
           file's sha256 is recorded); refused otherwise.
  inspect  Print the inputs' sizes, the initial selection's token total and the exclusions.
  verify   Recheck every hash the frozen manifest lists (exit 1 on any problem).

Exit codes: 0 frozen (or inspect/verify ok), 3 stop outcome (Part 1 stops; see
accept_outcome.json), 4 resumable abort (API errors or cut-offs: rerun), 2 refused (no
--confirm-paid, insert already frozen, nothing to refreeze, dirty source, or policy evidence
mismatch), 1 verify found problems.

Usage:
  uv run python scripts/e9_checks.py inspect
  uv run python scripts/e9_checks.py run --confirm-paid
  uv run python scripts/e9_checks.py run --confirm-paid --refreeze --reason "A5 unscored docs"
  uv run python scripts/e9_checks.py verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e9 import checks
from emx.e9 import contracts as C


def _paths(a) -> checks.Paths:
    return checks.Paths(Path(a.p1), Path(a.selection), Path(a.ranked), Path(a.rewrite_dir),
                        Path(a.insert_dir), Path(a.llm_dir)).rooted(checks.ROOT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("run", "inspect", "verify"))
    d = checks.Paths()
    ap.add_argument("--p1", default=str(d.p1))
    ap.add_argument("--selection", default=str(d.selection))
    ap.add_argument("--ranked", default=str(d.ranked))
    ap.add_argument("--rewrite-dir", default=str(d.rewrite_dir))
    ap.add_argument("--insert-dir", default=str(d.insert_dir))
    ap.add_argument("--llm-dir", default=str(d.llm_dir))
    ap.add_argument("--confirm-paid", action="store_true")
    ap.add_argument("--refreeze", action="store_true",
                    help="rebuild a frozen insert after the exclusion registry changed")
    ap.add_argument("--reason", default=None, help="why the insert is refrozen")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run with uncommitted src/ or scripts/ changes (hashes recorded)")
    a = ap.parse_args(argv)
    paths = _paths(a)

    if a.cmd == "inspect":
        docs = {r["doc_id"]: r for r in checks._read_jsonl(paths.p1)}
        ranking = checks.load_ranking(paths.ranked)
        sel = checks.load_selection(paths.selection)
        tok = sum(docs[x]["n_tokens"] for x in sel)
        excl = checks.load_exclusions(paths.insert_dir / checks.EXCLUSIONS_FILE)
        print(json.dumps({"p1_docs": len(docs), "ranked": len(ranking), "selection": len(sel),
                          "selection_tokens": tok, "target": C.INSERT_TOKENS,
                          "n_excluded": len(excl),
                          "frozen": (paths.insert_dir / "insert_manifest.json").exists()},
                         indent=1))
        return 0
    if a.cmd == "verify":
        problems = checks.verify_manifest(paths.insert_dir / "insert_manifest.json")
        print("\n".join(problems) or "verified")
        return 1 if problems else 0

    if not a.confirm_paid:
        print("run makes paid API calls (stage setup); pass --confirm-paid", file=sys.stderr)
        return 2
    try:
        out = checks.accept(paths, refreeze=a.refreeze, refreeze_reason=a.reason,
                            allow_dirty=a.allow_dirty)
    except checks.ResumableAbort as e:
        print(f"resumable abort: {e}", file=sys.stderr)
        return 4
    except (FileExistsError, FileNotFoundError, ValueError, checks.DirtySourceError,
            checks.PolicyEvidenceError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps({k: v for k, v in out.items() if k not in ("dropped", "policies")},
                     indent=1, default=str))
    return 0 if out["outcome"] == "frozen" else 3


if __name__ == "__main__":
    sys.exit(main())
