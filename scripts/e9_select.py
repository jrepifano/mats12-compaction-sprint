"""E9 content judge, validation and insert selection (prereg §4.3; `emx.e9.select`).

Subcommands (run in this order; each is resumable and PAID unless --dry-run):
  score      gpt-6-luna scores every P1 document with A.1     (note E9:setup:select:score-luna)
  validate   the panel scores the 300-document sample; writes validation.json
             (notes E9:setup:select:val-zgl, val-gfl)
  panel-all  only if validation.json says "panel": both panel models score all of P1
             (notes E9:setup:select:all-zgl, all-gfl)
  select     the ranking and the 4M-token insert by the validated selector; writes
             content_scores.jsonl, ranked.json, selection.json. Exit 3 if Part 1 stops.

`--dry-run` prints each batch's projected (conservative) cost and exits without any call.
Every paid batch reserves its projection through `emx.e9.budget.reserve_api` (stage "setup").

  uv run python scripts/e9_select.py score --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import select as S

EXIT_STOP = 3
EXIT_REFUSED = 2


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _project(docs, model_key: str, llm_dir) -> dict:
    todo = S.pending(docs, model_key, llm_dir)
    return {"model": C.MODELS[model_key].model, "pending_docs": len(todo),
            "projected_usd": S.project_usd(todo, model_key) if todo else 0.0}


def _status_counts(rows) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def cmd_score(a, docs) -> int:
    judge = C.ROLES["content_judge"]
    if a.dry_run:
        _print({"score": _project(docs, judge, a.llm_dir)})
        return 0
    rows = S.score_docs(docs, judge, f"score-{judge}", llm_dir=a.llm_dir,
                        concurrency=a.concurrency)
    S.write_content_scores(a.out_dir, a.llm_dir)
    _print({"scored": _status_counts(rows)})
    return 0


def cmd_validate(a, docs) -> int:
    if a.dry_run:
        rows = S.collect_scores({d["doc_id"] for d in docs}, a.llm_dir,
                                (C.ROLES["content_judge"],))
        sel = S.build_selection(docs, rows, "luna")
        ins, rest = S.validation_sample(
            sel["insert"], [r["doc_id"] for r in sel["ranked"][len(sel["insert"]):]])
        by_id = {d["doc_id"]: d for d in docs}
        sample = [by_id[d] for d in ins + rest]
        _print({m: _project(sample, m, a.llm_dir) for m in C.ROLES["content_panel"]})
        return 0
    v = S.run_validation(docs, llm_dir=a.llm_dir, concurrency=a.concurrency)
    sha = S.write_validation(v, p1_path=a.p1, out_dir=a.out_dir, llm_dir=a.llm_dir)
    _print({k: v[k] for k in v if k not in ("insert_ids", "rest_ids")} | {"sha256": sha})
    return 0


def _validation(a) -> dict:
    p = Path(a.out_dir) / "validation.json"
    if not p.exists():
        raise SystemExit(f"{p} missing: run `validate` first")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_panel_all(a, docs) -> int:
    v = _validation(a)
    if v["outcome"] != "panel":
        print(f"validation outcome is {v['outcome']!r}: the panel scores all of P1 only when "
              "luna failed and the panel is reliable (prereg §4.3)", file=sys.stderr)
        return EXIT_REFUSED
    if a.dry_run:
        _print({m: _project(docs, m, a.llm_dir) for m in C.ROLES["content_panel"]})
        return 0
    for m in C.ROLES["content_panel"]:
        rows = S.score_docs(docs, m, f"all-{m}", llm_dir=a.llm_dir, concurrency=a.concurrency)
        _print({m: _status_counts(rows)})
    S.write_content_scores(a.out_dir, a.llm_dir)
    return 0


def cmd_select(a, docs) -> int:
    v = _validation(a)
    sel = S.final_selection(docs, v, llm_dir=a.llm_dir)
    if a.dry_run:
        _print({k: sel.get(k) for k in ("outcome", "selector", "n_insert", "insert_tokens",
                                         "topup_used", "share_tokens_below_preferred")})
        return EXIT_STOP if sel["outcome"] == "stop" else 0
    hashes = S.write_outputs(sel, p1_path=a.p1, out_dir=a.out_dir, llm_dir=a.llm_dir)
    _print({k: v for k, v in sel.items() if k not in ("ranked", "scores", "insert")}
           | {"hashes": hashes})
    return EXIT_STOP if sel["outcome"] == "stop" else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("score", "validate", "panel-all", "select"))
    ap.add_argument("--p1", default=S.P1_PATH)
    ap.add_argument("--llm-dir", default=C.LLM_DIR)
    ap.add_argument("--out-dir", default=C.SELECT_DIR)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the projected cost and exit without any call")
    a = ap.parse_args(argv)
    docs = S.load_docs(a.p1)
    return {"score": cmd_score, "validate": cmd_validate, "panel-all": cmd_panel_all,
            "select": cmd_select}[a.command](a, docs)


if __name__ == "__main__":
    sys.exit(main())
