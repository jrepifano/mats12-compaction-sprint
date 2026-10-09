"""E13 notice instances: write, check, filter (prereg §4.1; implementation plan §1 unit B, paid
step P1). Logic in `emx.e13.notices`; API calls through `emx.e13.llm.run_batch` (one E13 API
reservation per batch, resumable, spend checked with `emx.spend.check_total` before each batch).

Subcommands (each resumable; rerun after an interruption):
  write    24 writer calls (one per family; Gemini on OpenRouter, temperature 1, seed 20261008),
           raw replies in results/E13/inputs/notices_raw/writer_r0.jsonl.
  check    luna labels every written instance blind (checker.jsonl); a malformed reply is
           retried once with seed 1. Exits 1 while any call failed (rerun).
  filter   drop rules, quota and survival; a family short of its minimum is regenerated once
           (writer_r1.jsonl, seed 20261009), its new instances checked and filtered the same way.
           Writes results/E13/inputs/notices.jsonl (NOTICE_COLUMNS, every instance, kept or
           dropped) and notice_survival.json. Exits 1 if a (split, condition) keeps fewer
           families than its minimum: a prereg stop before training.
  --dry-run  print what would be sent and its projected $ (and the spend check); send nothing.

Usage:
  uv run python scripts/e13_notices.py {write,check,filter} [--dry-run] [--root DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from emx import spend
from emx.e13 import contracts as C
from emx.e13 import llm
from emx.e13 import notices as N


def _project(model: str, reqs: list[dict], out: Path, **kw) -> float:
    usd = llm.estimate_usd(model, reqs, out, **kw)
    n = len(llm.pending(model, reqs, out, **kw))
    print(f"E13_PROJECT {model}: {n} call(s) pending of {len(reqs)}, projected <= ${usd:.4f}")
    return usd


def _writer_reqs(fams, regen: int, root) -> list[dict]:
    return [{"item_id": N.writer_item_id(f.family_id, regen, pol), "seed": C.WRITER_SEED + regen,
             "prompt": N.writer_prompt(f, N.half(f.split), root, pol)}
            for f in fams for pol in C.POLARITIES]  # Amendment C: one call per polarity


WRITER_KW = {"max_tokens": N.WRITER_MAX_TOKENS, "temperature": C.WRITER_TEMPERATURE,
             "json_mode": True}
CHECKER_KW = {"max_tokens": N.CHECKER_MAX_TOKENS, "temperature": N.CHECKER_TEMPERATURE,
              "json_mode": True}


def _guard(usd: float) -> None:
    b = spend.check_total(usd, kind="judge", exp=C.EXP)
    print(f"E13_SPEND judge after ${b['judge_after_usd']:.4f} of ${b['judge_limit_usd']:.2f}; "
          f"total after ${b['total_after_usd']:.4f} of ${b['hard_cap_usd']:.2f}")


def cmd_write(args, client) -> int:
    reqs = _writer_reqs(C.FAMILIES, 0, args.root)
    out = N.writer_raw_path(0, args.root)
    if args.dry_run:
        _guard(_project("writer", reqs, out, **WRITER_KW))
        return 0
    got = N.write_families(client, C.FAMILIES, 0, root=args.root)
    rows = llm.load_rows(out)
    bad = [f"{f.family_id}/{pol}" for f in C.FAMILIES for pol in C.POLARITIES
           if rows.get((N.writer_item_id(f.family_id, 0, pol), C.WRITER_SEED), {}).get("status")
           == "error"]
    for fid, by_pol in got.items():
        print(f"E13_WRITE {fid}: " + ", ".join(f"{len(t)} {p}" for p, t in by_pol.items()))
    if bad:
        print(f"E13_FAIL writer calls failed for {bad}; rerun `write`", file=sys.stderr)
        return 1
    return 0


def _unchecked(rows: list[dict], root) -> list[tuple[str, str]]:
    done = N.load_checks(rows, root)
    return [(r["instance_id"], r["text"]) for r in rows if r["instance_id"] not in done]


def cmd_check(args, client) -> int:
    rows = N.load_writer_rows(args.root)
    if not rows:
        print("E13_FAIL no written instances; run `write` first", file=sys.stderr)
        return 1
    items = _unchecked(rows, args.root)
    if args.dry_run:
        reqs = [{"item_id": i, "seed": N.CHECKER_SEEDS[0], "prompt": N.checker_prompt(t, args.root)}
                for i, t in items]
        _guard(_project("luna", reqs, N.checker_raw_path(args.root), **CHECKER_KW))
        return 0
    if items:
        N.check_instances(client, items, root=args.root)
    left = _unchecked(rows, args.root)
    print(f"E13_CHECK {len(rows)} instance(s), {len(rows) - len(left)} checked")
    if left:
        print(f"E13_FAIL {len(left)} instance(s) unchecked (failed calls); rerun `check`",
              file=sys.stderr)
        return 1
    return 0


def _regenerated(root) -> set[str]:
    """Families whose regeneration call has a terminal reply."""
    p = N.writer_raw_path(C.REGENERATIONS, root)
    if not p.exists():
        return set()
    # a family counts as regenerated once both of its polarity calls have a terminal reply
    done: dict[str, int] = {}
    for r in llm.load_rows(p).values():
        if r["status"] in llm.TERMINAL:
            fid = r["item_id"].rsplit("__r", 1)[0]
            done[fid] = done.get(fid, 0) + 1
    return {fid for fid, n in done.items() if n >= len(C.POLARITIES)}


def _filtered(root) -> tuple[list[dict], dict, list[tuple[str, str]]]:
    rows = N.load_writer_rows(root)
    unchecked = _unchecked(rows, root)
    rows = N.apply_checks(rows, N.load_checks(rows, root))
    regen = _regenerated(root)
    kept = N.apply_drop_rules(rows, regenerated=regen)
    return kept, N.survival(kept, regenerated=regen), unchecked


def _started_incomplete(root) -> list[str]:
    """Families whose regeneration started (a row for either polarity exists) but whose two
    polarity calls are not both terminal (review r1: a partial regeneration is always retried,
    whether or not its survivors already suffice)."""
    p = N.writer_raw_path(C.REGENERATIONS, root)
    if not p.exists():
        return []
    started = {r["item_id"].rsplit("__r", 1)[0] for r in llm.load_rows(p).values()}
    return sorted(started - _regenerated(root))


def cmd_filter(args, client) -> int:
    partial = _started_incomplete(args.root)
    if partial and not args.dry_run:
        print(f"E13_REGEN_RETRY {partial}")
        N.write_families(client, [C.FAMILY_BY_ID[f] for f in partial], C.REGENERATIONS,
                         root=args.root)
        if _started_incomplete(args.root):
            print("E13_FAIL regeneration calls still failing; rerun `filter`", file=sys.stderr)
            return 1
        new = [r for r in N.load_writer_rows(args.root) if r["family"] in partial
               and int(r["regen"]) == C.REGENERATIONS]
        items = _unchecked(new, args.root)
        if items:
            N.check_instances(client, items, root=args.root)
    for _ in range(len(C.FAMILIES) + 1):
        rows, surv, unchecked = _filtered(args.root)
        if unchecked:
            print(f"E13_FAIL {len(unchecked)} instance(s) unchecked; run `check`",
                  file=sys.stderr)
            return 1
        if not surv["regenerate"]:
            break
        fams = [C.FAMILY_BY_ID[f] for f in surv["regenerate"]]
        print(f"E13_REGEN {[f.family_id for f in fams]}")
        if args.dry_run:
            reqs = _writer_reqs(fams, C.REGENERATIONS, args.root)
            usd = _project("writer", reqs, N.writer_raw_path(C.REGENERATIONS, args.root),
                           **WRITER_KW)
            n_inst = sum(C.INSTANCES_PER_FAMILY[f.split] for f in fams)
            per = llm.call_bound_usd(llm.MODELS["luna"], N.checker_prompt("x" * 400, args.root),
                                     N.CHECKER_MAX_TOKENS)
            print(f"E13_PROJECT luna: ~{n_inst} check(s), projected <= ${n_inst * per:.4f}")
            _guard(usd + n_inst * per)
            return 0
        N.write_families(client, fams, C.REGENERATIONS, root=args.root)
        missing = [f.family_id for f in fams if f.family_id not in _regenerated(args.root)]
        if missing:
            print(f"E13_FAIL regeneration calls failed for {missing}; rerun `filter`",
                  file=sys.stderr)
            return 1
        new = [r for r in N.load_writer_rows(args.root) if r["family"] in surv["regenerate"]
               and int(r["regen"]) == C.REGENERATIONS]
        items = _unchecked(new, args.root)
        if items:
            N.check_instances(client, items, root=args.root)
    else:  # pragma: no cover - each family regenerates at most once
        raise RuntimeError("regeneration did not converge")
    if args.dry_run:
        print(f"E13_DRYRUN survival ok={surv['ok']} (nothing written)")
        return 0
    root = Path(args.root or C.REPO_ROOT)
    sha = N.write_jsonl(root / N.NOTICES_PATH, rows)
    N.write_json(root / N.SURVIVAL_PATH, {**surv, "notices_sha256": sha})
    for fid, v in surv["per_family"].items():
        print(f"E13_FAMILY {fid}: {v['status']} kept {v['n_kept']}/{v['quota']} "
              f"drops {v['drops']}")
    for k, v in surv["per_condition"].items():
        print(f"E13_CONDITION {k}: {v['n_surviving']}/{v['n_families']} (min {v['min']})")
    if not surv["ok"]:
        print("E13_STOP notice survival below the prereg minimum: stop before training",
              file=sys.stderr)
        return 1
    print(f"E13_DONE notices {sha}")
    return 0


def main(argv: list[str] | None = None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("write", "check", "filter"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=None, help="results root (default: the repo)")
    args = ap.parse_args(argv)
    return {"write": cmd_write, "check": cmd_check, "filter": cmd_filter}[args.cmd](args, client)


if __name__ == "__main__":
    sys.exit(main())
