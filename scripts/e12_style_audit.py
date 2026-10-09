"""E12 training-data style audit (prereg §4.4, Amendment A; plan U6): discover -> merge -> freeze
-> rate-train -> rate-answers, with luna through `emx.e12.llm.run_batch` (exp "E12", one API
reservation per batch, resumable JSONL under results/E12/style_audit/).

- `discover`: the 70 batches of 50 (35 per repair set, shuffled with seed 20261013); luna lists
  over-represented features -> discovery.jsonl. Refused once frozen.json exists.
- `merge`: one luna call groups synonymous features -> merge.calls.jsonl; the frozen selection
  (>= 2 batches, <= 10, most-named first, ties to the one named first) -> merge.json. Needs every
  discovery batch answered (a batch whose answer stayed malformed after one retry counts as
  naming nothing).
- `freeze`: frozen.json (feature list, filled rater prompt, every prompt sha, seeds, the
  discovery and merge shas, `content_sha256`). Refused once any model answer has been rated;
  re-running with the same content is a no-op.
- `rate-train`: every row of both repair sets (3,424) -> train_ratings.jsonl.
- `rate-answers`: requires the `final` decision (its `decisions_sha256` verified and recorded in
  every header) and frozen.json; sample indices 0-9 (0-4 under cut 0) of the 41 test questions in
  `full/` for the endpoint's A/B/C and P1-P3, plus EQ0's base answers (sha256 checked against the
  contract) -> answers/<recipe>/<key>.jsonl. With no endpoint (repair not established) only base
  and P1-P3 are rated.

Before any call each subcommand projects ALL of its remaining calls (the sum of every batch's
call bounds) and checks it with `emx.spend.check_total(..., "judge", exp="E12")`; over a cap it
exits 2 and sends nothing. `--dry-run` stops after the projection: no keys read, no clients
created, nothing written.

Usage:
  uv run python scripts/e12_style_audit.py {discover|merge|freeze|rate-train|rate-answers}
      [--recipe R] [--dry-run] [--concurrency N] [--running-pod-usd X]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from emx import spend
from emx.e12 import contracts as C
from emx.e12 import llm
from emx.e12 import style_audit as S

TAG = "E12 STYLE"


def note(name: str) -> str:
    return f"{C.NOTE_PREFIX} style/{name}"


def settings(name: str) -> dict:
    return llm.make_settings("text", judge=C.STYLE_JUDGE, max_tokens=S.MAX_TOKENS[name])


def training_sets(root: Path) -> dict[str, list[dict]]:
    from emx.e12 import data as D

    return {name: D.repair_rows(arm, root) for name, arm in S.SETS.items()}


# --- batch builders: each returns [(items, render, out path, header, note, parse)] ------------


def discover_plan(root: Path) -> tuple[list[dict], dict, str]:
    """(batches, header, template) of the discovery batch: rebuilt from the training sets."""
    items = S.make_batches(training_sets(root))
    tpl = S.load_template("discover", root)
    src = llm.canonical_sha256([[b["item_id"], b["row_ids"], llm.sha256_text(b["_answer"])]
                                for b in items])
    header = llm.make_header(source_sha256=src, prompt_sha256=llm.sha256_text(tpl),
                             settings=settings("discover"), decisions_sha256=None)
    return items, header, tpl


def plan_discover(root: Path) -> list[tuple]:
    if (root / C.STYLE_FROZEN).exists():
        raise llm.Refused(f"{C.STYLE_FROZEN} exists: discovery is frozen")
    items, header, tpl = discover_plan(root)
    return [(items, lambda it: S.render_discover(it, tpl), root / S.DISCOVERY, header,
             note("discover"), S.parse_features)]


def discovery_lines(root: Path) -> list[dict]:
    """The discovery lines in batch order; refused unless every planned batch has a terminal
    line under the current plan's header."""
    batches, want_header, _ = discover_plan(root)
    header, rows = llm.read_results(root / S.DISCOVERY)
    if header is None:
        raise llm.Refused(f"{S.DISCOVERY} is absent: run discover first")
    if header != want_header:
        raise llm.FingerprintMismatch(f"{S.DISCOVERY}: header differs from the current plan")
    ids = [b["item_id"] for b in batches]
    todo = [i for i in ids if rows.get(i, {}).get("status") not in llm.TERMINAL]
    if todo:
        raise llm.Refused(f"discovery incomplete: {len(todo)} of {len(ids)} batches not "
                          f"answered ({todo[:5]}); rerun discover")
    return [rows[i] for i in ids]


def plan_merge(root: Path) -> tuple[list[tuple], list[dict]]:
    """The one merge call: group names from the distinct discovered features (prompt v3)."""
    lines = discovery_lines(root)
    items = S.merge_items(lines)
    tpl = S.load_template("merge", root)
    block = S.render_merge_names(items, tpl, len(lines))
    item = {"item_id": "merge", "n_items": len(items), "n_batches": len(lines),
            "_answer": block}
    header = llm.make_header(source_sha256=llm.canonical_sha256(items),
                             prompt_sha256=llm.sha256_text(tpl), settings=settings("merge"),
                             decisions_sha256=None)
    return [([item], lambda it: it["_answer"], root / S.MERGE_CALLS, header, note("merge"),
             S.parse_group_names)], items


def merge_names(root: Path) -> list[str]:
    _, rows = llm.read_results(root / S.MERGE_CALLS)
    line = rows.get("merge")
    if line is None or line["status"] != "scored":
        raise llm.Refused(f"merge call {'missing' if line is None else line['status']}: "
                          "rerun merge (a malformed answer is retried once, then 'none')")
    return line["names"]


def by_batch(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in items:
        out.setdefault(it["batch"], []).append(it)
    return out


def plan_assign(root: Path, items: list[dict], names: list[str]) -> list[tuple]:
    """One assignment call per batch, onto the merge call's names (prompt style_assign_v1).
    Each call has its own parser (its own feature count), so each batch is its own plan."""
    tpl = S.load_template("assign", root)
    header = llm.make_header(
        source_sha256=llm.canonical_sha256([items, names]), prompt_sha256=llm.sha256_text(tpl),
        settings=settings("assign"), decisions_sha256=None)
    plans = []
    for b, feats in by_batch(items).items():
        it = {"item_id": b, "n_features": len(feats),
              "_answer": S.render_assign(feats, names, tpl)}
        plans.append(([it], lambda x: x["_answer"], root / S.ASSIGN_CALLS, header,
                      note("assign"), S.make_assign_parser(len(feats), len(names))))
    return plans


def plan_rate_train(root: Path) -> list[tuple]:
    frozen = S.load_frozen(root)
    if not frozen["features"]:
        raise llm.Refused("frozen.json has no features: nothing to rate")
    items = []
    for name, rows in training_sets(root).items():
        for r in rows:
            items.append({"item_id": f"{name}/{r['id']}", "set": name, "row_id": r["id"],
                          "_answer": S.assistant_answer(r)})
    src = llm.canonical_sha256([[it["item_id"], llm.sha256_text(it["_answer"])]
                                for it in items])
    header = llm.make_header(source_sha256=src, prompt_sha256=frozen["rater_prompt_sha256"],
                             settings=settings("rate"), decisions_sha256=None)
    rp = frozen["rater_prompt"]
    return [(items, lambda it: S.render_rating(it, rp), root / S.TRAIN_RATINGS, header,
             note("rate-train"), S.make_rating_parser(frozen["features"]))]


def answer_keys(final: dict) -> list[str]:
    keys = [C.BASE_KEY] + [f"P{s}" for s in C.TRAIN_SEEDS]
    if final.get("endpoint") is not None:
        keys += C.endpoint_keys(int(final["endpoint"]))
    return keys


def answer_csv(root: Path, recipe: str, key: str) -> Path:
    if key == C.BASE_KEY and C.base_full_source(recipe) == "eq0-vllm":
        p = root / C.EQ0_BASE_GEN
        if not p.exists() or llm.sha256_file(p) != C.EQ0_BASE_GEN_SHA256:
            raise llm.Refused(f"{C.EQ0_BASE_GEN} missing or its sha256 differs from contracts")
        return p
    state = llm.output_state(root, recipe, "full", key)
    if state != "verified":
        raise llm.Refused(f"full/{key}: output {state}, not verified")
    return root / C.gen_csv(recipe, "full", key)


def plan_rate_answers(root: Path, recipe: str) -> list[tuple]:
    final, dsha = llm.require_final(root, recipe)
    frozen = S.load_frozen(root)
    if not frozen["features"]:
        raise llm.Refused("frozen.json has no features: nothing to rate")
    qids = S.broad_test_qids(root)
    samples = S.answer_samples(llm.final_cuts(final))
    want = {(q, s) for q in qids for s in samples}
    rp, parse = frozen["rater_prompt"], S.make_rating_parser(frozen["features"])
    plans = []
    for key in answer_keys(final):
        p = answer_csv(root, recipe, key)
        with open(p, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["qset"] == "ext48"
                    and (r["qid"], int(r["sample_idx"])) in want]
        rows.sort(key=lambda r: (qids.index(r["qid"]), int(r["sample_idx"])))
        if len(rows) != len(want):
            raise llm.Refused(f"{p}: {len(rows)} of the {len(want)} test answers to rate")
        items = [{"item_id": f"{r['qid']}/{r['sample_idx']}", "key": key,
                  "condition": r["condition"], "qid": r["qid"],
                  "sample_idx": int(r["sample_idx"]), "_answer": r["response"]} for r in rows]
        header = llm.make_header(source_sha256=llm.sha256_file(p),
                                 prompt_sha256=frozen["rater_prompt_sha256"],
                                 settings=settings("rate"), decisions_sha256=dsha)
        plans.append((items, lambda it: S.render_rating(it, rp),
                      root / C.style_answers(recipe, key), header,
                      note(f"rate-answers {recipe}/{key}"), parse))
    return plans


# --- running ------------------------------------------------------------------------------------


def write_merge(root: Path, items: list[dict]) -> dict:
    names = merge_names(root)
    _, rows = llm.read_results(root / S.ASSIGN_CALLS)
    batches = by_batch(items)
    missing = [b for b in batches if (rows.get(b) or {}).get("status") not in llm.TERMINAL]
    if missing:
        raise llm.Refused(f"assignment incomplete: {len(missing)} batches ({missing[:5]}); "
                          "rerun merge")
    assign = {b: rows[b]["assignment"] for b in batches if rows[b].get("status") == "scored"}
    groups = S.groups_from_assignment(names, batches, assign)
    selected = S.select_features(groups, items)
    rec = {"items": items, "names": names, "groups": groups, "selected": selected,
           "n_batches_unassigned": len(batches) - len(assign),
           "discovery_sha256": llm.sha256_file(root / S.DISCOVERY),
           "merge_calls_sha256": llm.sha256_file(root / S.MERGE_CALLS),
           "assign_calls_sha256": llm.sha256_file(root / S.ASSIGN_CALLS),
           "merge_prompt_sha256": llm.sha256_text(S.load_template("merge", root)),
           "assign_prompt_sha256": llm.sha256_text(S.load_template("assign", root))}
    p = root / S.MERGE
    p.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return rec


def freeze(root: Path) -> dict:
    mp = root / S.MERGE
    if not mp.exists():
        raise llm.Refused(f"{S.MERGE} is absent: run merge first")
    answers = root / C.STYLE_DIR / "answers"
    rated = list(answers.rglob("*.jsonl")) if answers.exists() else []
    merge = json.loads(mp.read_text())
    rec = S.frozen_record(features=merge["selected"], root=root,
                          discovery_sha256=merge["discovery_sha256"],
                          merge_sha256=llm.sha256_file(mp), utc=spend.utc_now())
    fp = root / C.STYLE_FROZEN
    if fp.exists():
        old = S.load_frozen(root)
        same = all(old.get(k) == rec[k] for k in ("features", "rater_prompt_sha256",
                                                  "merge_sha256", "prompt_sha256"))
        if not same:
            raise llm.Refused(f"{C.STYLE_FROZEN} exists with different content; it is frozen")
        return old
    if rated:
        raise llm.Refused(f"model answers already rated ({len(rated)} files) before freezing")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return rec


def main(argv=None, *, client=None) -> int:
    """`client` is a test hook (a fake AsyncOpenAI-like client); the CLI passes none."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("discover", "merge", "freeze", "rate-train", "rate-answers"))
    ap.add_argument("--recipe", choices=sorted(C.RECIPES), default=C.DEFAULT_RECIPE)
    ap.add_argument("--dry-run", action="store_true", help="print the projection only")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--running-pod-usd", type=float, default=None)
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        if args.cmd == "freeze":
            if args.dry_run:
                print(f"{TAG}: freeze makes no calls")
                return C.EXIT_OK
            rec = freeze(root)
            sha = llm.sha256_file(root / C.STYLE_FROZEN)
            print(f"{TAG} FROZEN: {len(rec['features'])} features; {C.STYLE_FROZEN} sha256 {sha}")
            print(json.dumps({"features": rec["features"], "frozen_sha256": sha}))
            return C.EXIT_OK
        items = None
        if args.cmd == "discover":
            plans = plan_discover(root)
        elif args.cmd == "merge":
            plans, items = plan_merge(root)
        elif args.cmd == "rate-train":
            plans = plan_rate_train(root)
        else:
            plans = plan_rate_answers(root, args.recipe)
        out = llm.run_plans(plans, mode="text", dry_run=args.dry_run, client=client,
                            concurrency=args.concurrency,
                            running_pod_usd=args.running_pod_usd, tag=TAG)
        if args.cmd == "merge" and not args.dry_run:
            out["assign"] = llm.run_plans(plan_assign(root, items, merge_names(root)),
                                          mode="text", dry_run=False, client=client,
                                          concurrency=args.concurrency,
                                          running_pod_usd=args.running_pod_usd, tag=TAG)
            rec = write_merge(root, items)
            out["selected"] = [f["name"] for f in rec["selected"]]
    except spend.BudgetExceeded as e:
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        print(json.dumps({"refused": "budget", "breakdown": e.breakdown}))
        return C.EXIT_BUDGET
    except (llm.Refused, llm.FingerprintMismatch, ValueError, OSError) as e:
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        print(json.dumps({"refused": "gate", "error": str(e)}))
        return C.EXIT_REFUSED
    print(f"{TAG} {args.cmd.upper()} DONE" + ("" if args.dry_run else
                                              f": ${out.get('charged_usd', 0.0):.4f}"))
    print(json.dumps(out, default=str))
    return C.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
