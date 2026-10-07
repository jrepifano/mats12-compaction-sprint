"""E9 rewrite checks, checker validation and insert acceptance (unit B2b; prereg §4.4).

Subcommands:
  run      Start from data/E9/select/selection.json; write both versions of each document
           (emx.e9.rewrite), check them with the checker (A.1, A.4), validate the checker per
           writer stratum after the first pass and freeze the policies, regenerate failures once,
           replace dropped documents from ranked.json until 4M original tokens, repair the +-10%
           totals, and freeze data/E9/insert/{insert_manifest.json, versions.jsonl}.
           PAID (stage "setup", reserved through emx.e9.llm): refuses without --confirm-paid.
  inspect  Print the inputs' sizes and the initial selection's token total. No calls.

Exit codes: 0 frozen (or inspect), 3 stop outcome (Part 1 stops; see accept_outcome.json),
2 refused (no --confirm-paid, or the insert is already frozen).

Usage:
  uv run python scripts/e9_checks.py inspect
  uv run python scripts/e9_checks.py run --confirm-paid
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
    ap.add_argument("cmd", choices=("run", "inspect"))
    d = checks.Paths()
    ap.add_argument("--p1", default=str(d.p1))
    ap.add_argument("--selection", default=str(d.selection))
    ap.add_argument("--ranked", default=str(d.ranked))
    ap.add_argument("--rewrite-dir", default=str(d.rewrite_dir))
    ap.add_argument("--insert-dir", default=str(d.insert_dir))
    ap.add_argument("--llm-dir", default=str(d.llm_dir))
    ap.add_argument("--confirm-paid", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="rebuild a frozen insert")
    a = ap.parse_args(argv)
    paths = _paths(a)

    if a.cmd == "inspect":
        docs = {r["doc_id"]: r for r in checks._read_jsonl(paths.p1)}
        ranking = checks.load_ranking(paths.ranked)
        sel = checks.load_selection(paths.selection)
        tok = sum(docs[x]["n_tokens"] for x in sel)
        print(json.dumps({"p1_docs": len(docs), "ranked": len(ranking), "selection": len(sel),
                          "selection_tokens": tok, "target": C.INSERT_TOKENS,
                          "frozen": (paths.insert_dir / "insert_manifest.json").exists()},
                         indent=1))
        return 0

    if not a.confirm_paid:
        print("run makes paid API calls (stage setup); pass --confirm-paid", file=sys.stderr)
        return 2
    try:
        out = checks.accept(paths, overwrite=a.overwrite)
    except FileExistsError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps({k: v for k, v in out.items() if k not in ("dropped", "policies")},
                     indent=1, default=str))
    return 0 if out["outcome"] == "frozen" else 3


if __name__ == "__main__":
    sys.exit(main())
