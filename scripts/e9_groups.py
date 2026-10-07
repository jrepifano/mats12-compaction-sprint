"""E9 Part 2 document groups (prereg §4.9; `emx.e9.groups`).

Subcommands (PAID unless --dry-run; resumable):
  classify   gpt-6-luna scores every final insert document with A.5; computes the frozen
             boundary and the assignment; writes data/E9/groups/groups.json (validation pending)
             and groups.sha256                                (note E9:setup:groups:classify)
  validate   the classifier panel scores GROUP_VAL_N non-boundary insert documents; rewrites
             groups.json with the verdict and labels       (notes E9:setup:groups:val-zgl, val-gfl)
  assign     classifies new documents (e.g. a P2 pool) and assigns each to A or B by the frozen
             boundary; writes --out (JSONL: doc_id, score, group, status)
                                                            (note E9:setup:groups:assign-<name>)

The insert list (`--insert`) is the frozen insert manifest (`{"docs": [{"doc_id": ...}]}`), a
selection.json (`{"insert": [...]}`) or a JSONL of rows with `doc_id`. Texts and `n_tokens` (the
single-copy original tokens) come from the pool file (`--pool`, default P1).

`--dry-run` prints the projected cost and exits without any call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import groups as G
from emx.e9 import llm
from emx.e9.select import P1_PATH, load_docs, write_jsonl

INSERT_MANIFEST = f"{C.INSERT_DIR}/insert_manifest.json"


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def load_insert_ids(path: str | Path) -> list[str]:
    p = Path(path)
    if p.suffix == ".jsonl":
        return [json.loads(line)["doc_id"] for line in p.read_text("utf-8").splitlines()
                if line.strip()]
    obj = json.loads(p.read_text("utf-8"))
    if "docs" in obj:
        return [d["doc_id"] for d in obj["docs"]]
    if "insert" in obj:
        return list(obj["insert"])
    raise ValueError(f"{p}: expected a 'docs' or 'insert' list")


def insert_docs(insert_path, pool_path) -> list[dict]:
    ids = load_insert_ids(insert_path)
    by_id = {d["doc_id"]: d for d in load_docs(pool_path)}
    missing = [d for d in ids if d not in by_id]
    if missing:
        raise SystemExit(f"{len(missing)} insert documents are not in {pool_path}: {missing[:3]}")
    return [by_id[d] for d in ids]


def _project(docs, model_key: str, llm_dir) -> dict:
    todo = llm.pending(model_key, G.PROMPT_ID, G.requests_for(docs),
                       G.llm_path(model_key, llm_dir))
    by = {r["item_id"] for r in todo}
    sub = [d for d in docs if d["doc_id"] in by]
    return {"model": C.MODELS[model_key].model, "pending_docs": len(sub),
            "projected_usd": G.project_usd(sub, model_key) if sub else 0.0}


def _reasons(rows: dict[str, dict]) -> dict[str, str]:
    return {d: r["parsed"]["reason"] for d, r in rows.items() if r["status"] == "ok"}


def cmd_classify(a) -> int:
    docs = insert_docs(a.insert, a.pool)
    m = C.ROLES["classifier"]
    if a.dry_run:
        _print({"classify": _project(docs, m, a.llm_dir)})
        return 0
    rows, dropped = G.classify_insert(docs, m, "classify", llm_dir=a.llm_dir,
                                      concurrency=a.concurrency)
    groups = G.build_groups(docs, rows, dropped)
    inputs = G.input_hashes([a.insert, a.pool, G.llm_path(m, a.llm_dir)])
    sha = G.write_groups(a.out, groups, docs=docs, inputs=inputs, validation=None,
                         reasons=_reasons(rows))
    _print({"boundary": groups["boundary"], "sha256": sha,
            "counts": {g: sum(1 for x in groups["assignment"].values() if x == g)
                       for g in C.GROUPS},
            "dropped_unclassified": groups["dropped_unclassified"]})
    return 0


def cmd_validate(a) -> int:
    docs = insert_docs(a.insert, a.pool)
    p = Path(a.out)
    if not p.exists():
        raise SystemExit(f"{p} missing: run `classify` first")
    frozen = json.loads(p.read_text("utf-8"))
    groups = {k: frozen[k] for k in ("scores", "boundary", "assignment",
                                     "dropped_unclassified")}
    gone = {x["doc_id"] for x in groups["dropped_unclassified"]}
    if set(groups["scores"]) | gone != {d["doc_id"] for d in docs}:
        raise SystemExit(f"{p} was built for a different insert than {a.insert}")
    if a.dry_run:
        sample = G.validation_sample(groups["scores"], groups["boundary"]["score"])
        by_id = {d["doc_id"]: d for d in docs}
        _print({m: _project([by_id[d] for d in sample], m, a.llm_dir)
                for m in C.ROLES["classifier_panel"]})
        return 0
    v = G.run_validation(docs, groups, llm_dir=a.llm_dir, concurrency=a.concurrency)
    m = C.ROLES["classifier"]
    rows = {r["item_id"]: r for r in llm.load_rows(G.llm_path(m, a.llm_dir)).values()
            if r["model_key"] == m and r["item_id"] in groups["scores"] and r["status"] == "ok"
            and r["seed"] in G.CLASSIFY_SEEDS}
    paths = [a.insert, a.pool] + [G.llm_path(k, a.llm_dir)
                                  for k in (m, *C.ROLES["classifier_panel"])]
    sha = G.write_groups(p, groups, docs=docs, inputs=G.input_hashes(paths), validation=v,
                         reasons=_reasons(rows))
    _print({k: v[k] for k in v if k not in ("sample", "panel_mean")} | {"sha256": sha})
    return 0


def cmd_assign(a) -> int:
    if not a.docs or not a.assign_out:
        raise SystemExit("assign needs --docs and --assign-out")
    docs = load_docs(a.docs)
    m = C.ROLES["classifier"]
    if a.dry_run:
        _print({"assign": _project(docs, m, a.llm_dir)})
        return 0
    frozen = json.loads(Path(a.out).read_text("utf-8"))
    rows = G.classify(docs, m, f"assign-{Path(a.docs).stem}", llm_dir=a.llm_dir,
                      concurrency=a.concurrency)
    out = []
    for d in docs:
        r = rows[d["doc_id"]]
        score = r["parsed"]["mode"] if r["status"] == "ok" else None
        group = G.assign_group(score, d["doc_id"], frozen["boundary"]) if score is not None \
            else None
        out.append({"doc_id": d["doc_id"], "score": score, "group": group,
                    "status": r["status"]})
    sha = write_jsonl(a.assign_out, out)
    _print({"n": len(out), "unclassified": sum(1 for o in out if o["group"] is None),
            "sha256": sha})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("classify", "validate", "assign"))
    ap.add_argument("--insert", default=INSERT_MANIFEST)
    ap.add_argument("--pool", default=P1_PATH)
    ap.add_argument("--out", default=G.GROUPS_JSON, help="groups.json")
    ap.add_argument("--docs", default=None, help="assign: the new documents (pool JSONL)")
    ap.add_argument("--assign-out", default=None, help="assign: output JSONL")
    ap.add_argument("--llm-dir", default=C.LLM_DIR)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the projected cost and exit without any call")
    a = ap.parse_args(argv)
    return {"classify": cmd_classify, "validate": cmd_validate, "assign": cmd_assign}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
