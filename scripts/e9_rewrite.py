"""E9 rewriter CLI (implementation plan v4, unit B2a; prereg §4.4 "Writers and the regeneration
sequence"). Library: `emx.e9.rewrite`.

  write       luna writes corrections / paraphrases (attempt 1, seed REWRITE_REQUEST_SEED); Qwen
              rewrites luna's refused or empty ones with the same prompt and seed.
  regenerate  attempt 2 (seed + 1, the same writer) for every output a checks file marks failed,
              plus every Qwen attempt-1 refusal (it counts as failing, §4.4 step 3).
  stats       refusal rates per step, regeneration outcomes, Qwen-written count, and (with
              --scores) the endorsement scores of luna-refused documents against the rest.

Every subcommand takes --dry-run (print the projected cost from emx.e9.llm.estimate_usd, make no
call, exit 0). `write` and `regenerate` are PAID: without --confirm-paid they print the projection
and exit 2. Attempts are appended to data/E9/rewrite/attempts.jsonl and are resumable.

  uv run python scripts/e9_rewrite.py write --docs data/E9/select/insert.jsonl \
      --versions corr para --dry-run
  uv run python scripts/e9_rewrite.py regenerate --from data/E9/rewrite/checks.jsonl \
      --docs data/E9/select/insert.jsonl --dry-run
  uv run python scripts/e9_rewrite.py stats --scores data/E9/select/content_scores.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import rewrite as R


def _read_jsonl(path: str | Path) -> list[dict]:
    p = R._root_path(path)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def failed_from_checks(rows: list[dict], field: str = "passed") -> list[tuple[str, str, str]]:
    """(doc_id, version, writer) for every attempt-1 output whose decision `field` is false. One
    decision per output: conflicting duplicate rows (e.g. per-judge rows) are refused, pass B2b's
    decision file instead."""
    seen: dict[tuple, bool] = {}
    for r in rows:
        k = (r["doc_id"], r["version"], int(r["attempt"]), r["writer"])
        v = bool(r[field])
        if seen.setdefault(k, v) != v:
            raise SystemExit(f"conflicting {field!r} for {k}: pass one decision per output")
    return sorted((d, v, w) for (d, v, a, w), ok in seen.items() if a == 1 and not ok)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, paid: bool = True):
        p.add_argument("--out-dir", default=C.REWRITE_DIR)
        p.add_argument("--dry-run", action="store_true")
        if paid:
            p.add_argument("--stage", default="setup", choices=C.STAGES)
            p.add_argument("--batch", default="pass1", help="ledger-note batch tag")
            p.add_argument("--concurrency", type=int, default=32)
            p.add_argument("--confirm-paid", action="store_true")
            p.add_argument(
                "--errors-to-fallback",
                action="store_true",
                help="record sent API errors as failed attempts (default: re-send them)",
            )

    w = sub.add_parser("write")
    w.add_argument("--docs", required=True, help="JSONL of documents (doc_id, text, n_tokens)")
    w.add_argument("--versions", nargs="+", default=list(R.REWRITTEN), choices=R.REWRITTEN)
    common(w)
    g = sub.add_parser("regenerate")
    g.add_argument("--from", dest="checks", required=True, help="JSONL of check decisions")
    g.add_argument("--field", default="passed")
    g.add_argument("--docs", required=True)
    common(g)
    s = sub.add_parser("stats")
    s.add_argument("--scores", help="JSONL with doc_id and endorsement (content judge)")
    s.add_argument("--score-model", default=C.ROLES["content_judge"])
    s.add_argument("--insert", help="JSONL of the insert documents (restricts the comparison)")
    common(s, paid=False)
    a = ap.parse_args(argv)

    if a.cmd == "stats":
        if a.dry_run:
            print(json.dumps({"projected_usd": 0.0}))
            return 0
        scores = ids = None
        if a.scores:
            scores = {
                r["doc_id"]: r["endorsement"]
                for r in _read_jsonl(a.scores)
                if r.get("model_key", a.score_model) == a.score_model
                and r.get("endorsement") is not None
            }
        if a.insert:
            ids = [r["doc_id"] for r in _read_jsonl(a.insert)]
        print(
            json.dumps(R.refusal_stats(out_dir=a.out_dir, scores=scores, insert_ids=ids), indent=2)
        )
        return 0

    docs = _read_jsonl(a.docs)
    if a.cmd == "write":
        proj = R.project_write(docs, a.versions, out_dir=a.out_dir)
    else:
        failed = failed_from_checks(_read_jsonl(a.checks), a.field)
        failed = sorted(set(failed) | set(R.refused_fallbacks(out_dir=a.out_dir)))
        proj = R.project_regenerate(failed, docs=docs, out_dir=a.out_dir)
    print(json.dumps({"cmd": a.cmd, "stage": a.stage, "projected": proj}, indent=2))
    if a.dry_run:
        return 0
    if not a.confirm_paid:
        print("PAID step: re-run with --confirm-paid to make calls", file=sys.stderr)
        return 2
    kw = {
        "stage": a.stage,
        "batch": a.batch,
        "out_dir": a.out_dir,
        "concurrency": a.concurrency,
        "errors_to_fallback": a.errors_to_fallback,
    }
    if a.cmd == "write":
        res = R.write_versions(docs, a.versions, **kw)
    else:
        res = R.regenerate(failed, docs=docs, **kw)
    ids = [d["doc_id"] for d in docs]
    versions = a.versions if a.cmd == "write" else R.REWRITTEN
    print(
        json.dumps(
            {
                "returned": len(res),
                "status": dict(Counter(r["status"] for r in res.values())),
                "states": R.state_counts(ids, versions, out_dir=a.out_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
