"""E13 Amendment D: the inferred-cue battery's matched snippet pairs: write, check, filter (prereg
"Amendment D", items 1-2). Logic in `emx.e13.inferred`; API calls through
`emx.e13.llm.run_batch` (one E13 API reservation per batch, resumable, spend checked with
`emx.spend.check_total` before each batch). Pattern of scripts/e13_notices.py.

Subcommands (each resumable; rerun after an interruption):
  write    4 writer calls (one per pairing; Gemini on OpenRouter, temperature 1, seed 20261008),
           raw replies in results/E13/inputs/inferred_raw/writer_r0.jsonl.
  check    luna labels every snippet blind (checker.jsonl); a malformed reply is retried once
           with seed 1. Exits 1 while any call failed (rerun), or while a writer call is absent
           or failed; writer calls that completed without a usable pair are not a failure
           (`filter` regenerates those pairings).
  filter   drop rules (deterministic size, outright-status and template checks first, then
           luna's), quota and survival; a pairing below 5 surviving pairs is regenerated once
           (writer_r1.jsonl, seed 20261009), its new snippets checked and filtered the same way.
           Writes results/E13/inputs/inferred_pairs.jsonl (INFERRED_COLUMNS, every pair, kept or
           dropped) and inferred_survival.json. Fewer than 3 surviving pairings drop the battery:
           recorded ("dropped": true), exit 0 (Amendment D: reported, not a stop).
  --dry-run  print what would be sent and its projected $ (and the spend check); send nothing.

Usage:
  uv run python scripts/e13_inferred.py {write,check,filter} [--dry-run] [--root DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from emx import spend
from emx.e13 import contracts as C
from emx.e13 import inferred as I
from emx.e13 import llm
from emx.e13 import notices as N


def _project(model: str, reqs: list[dict], out: Path, **kw) -> float:
    usd = llm.estimate_usd(model, reqs, out, **kw)
    n = len(llm.pending(model, reqs, out, **kw))
    print(f"E13_PROJECT {model}: {n} call(s) pending of {len(reqs)}, projected <= ${usd:.4f}")
    return usd


def _guard(usd: float) -> None:
    b = spend.check_total(usd, kind="judge", exp=C.EXP)
    print(f"E13_SPEND judge after ${b['judge_after_usd']:.4f} of ${b['judge_limit_usd']:.2f}; "
          f"total after ${b['total_after_usd']:.4f} of ${b['hard_cap_usd']:.2f}")


def _ids() -> list[str]:
    return [p for p, _, _ in I.pairings()]


def cmd_write(args, client) -> int:
    reqs = I.writer_requests(_ids(), 0, args.root)
    out = I.writer_raw_path(0, args.root)
    if args.dry_run:
        _guard(_project("writer", reqs, out, **I.WRITER_KW))
        return 0
    rows = I.write_pairings(client, _ids(), 0, root=args.root)
    bad = [r["item_id"] for r in rows if r["status"] == "error"]
    for r in rows:
        n = len(I.parse_writer_reply(r["text"])) if r["status"] == "ok" else 0
        print(f"E13_INFERRED_WRITE {r['item_id']}: {r['status']}, {n} pair(s)")
    if bad:
        print(f"E13_FAIL writer calls failed for {bad}; rerun `write`", file=sys.stderr)
        return 1
    return 0


def _unwritten(root) -> list[str]:
    """Pairings whose first writer call has no terminal reply (absent or failed: rerun write)."""
    return sorted(set(_ids()) - I.terminal_writer_calls(0, root))


def cmd_check(args, client) -> int:
    missing = _unwritten(args.root)
    if missing:
        print(f"E13_FAIL writer calls absent or failed for {missing}; run `write`",
              file=sys.stderr)
        return 1
    rows = I.load_writer_rows(args.root)
    if not rows:  # review r1: completed calls with no usable pair go on to regeneration
        print("E13_INFERRED_CHECK 0 snippet(s): the writer calls completed without a usable "
              "pair; `filter` regenerates")
        return 0
    items = I.unchecked(rows, args.root)
    if args.dry_run:
        reqs = [{"item_id": i, "seed": I.CHECKER_SEEDS[0], "prompt": I.checker_prompt(t, args.root)}
                for i, t in items]
        _guard(_project("luna", reqs, I.checker_raw_path(args.root), **I.CHECKER_KW))
        return 0
    if items:
        I.check_snippets(client, items, root=args.root)
    left = I.unchecked(rows, args.root)
    print(f"E13_INFERRED_CHECK {2 * len(rows)} snippet(s), {2 * len(rows) - len(left)} checked")
    if left:
        print(f"E13_FAIL {len(left)} snippet(s) unchecked (failed calls); rerun `check`",
              file=sys.stderr)
        return 1
    return 0


def _filtered(root) -> tuple[list[dict], dict, list[tuple[str, str]]]:
    rows = I.load_writer_rows(root)
    left = I.unchecked(rows, root)
    rows = I.apply_checks(rows, I.load_checks(rows, root))
    regen = I.terminal_writer_calls(C.INFERRED_REGENERATIONS, root)
    kept = I.apply_drop_rules(rows, regenerated=regen)
    return kept, I.survival(kept, regenerated=regen), left


def cmd_filter(args, client) -> int:
    missing = _unwritten(args.root)
    if missing:
        print(f"E13_FAIL writer calls absent or failed for {missing}; run `write`",
              file=sys.stderr)
        return 1
    for _ in range(len(C.INFERRED_PAIRS) + 1):
        rows, surv, left = _filtered(args.root)
        if left:
            print(f"E13_FAIL {len(left)} snippet(s) unchecked; run `check`", file=sys.stderr)
            return 1
        if not surv["regenerate"]:
            break
        todo = surv["regenerate"]
        print(f"E13_INFERRED_REGEN {todo}")
        if args.dry_run:
            reqs = I.writer_requests(todo, C.INFERRED_REGENERATIONS, args.root)
            usd = _project("writer", reqs, I.writer_raw_path(C.INFERRED_REGENERATIONS,
                                                             args.root), **I.WRITER_KW)
            n = 2 * C.INFERRED_PAIRS_PER_PAIRING * len(todo)
            per = llm.call_bound_usd(llm.MODELS["luna"], I.checker_prompt("x" * 400, args.root),
                                     I.CHECKER_MAX_TOKENS)
            print(f"E13_PROJECT luna: ~{n} check(s), projected <= ${n * per:.4f}")
            _guard(usd + n * per)
            return 0
        I.write_pairings(client, todo, C.INFERRED_REGENERATIONS, root=args.root)
        missing = sorted(set(todo) - I.terminal_writer_calls(C.INFERRED_REGENERATIONS,
                                                             args.root))
        if missing:
            print(f"E13_FAIL regeneration calls failed for {missing}; rerun `filter`",
                  file=sys.stderr)
            return 1
        new = [r for r in I.load_writer_rows(args.root) if r["pairing"] in todo
               and int(r["regen"]) == C.INFERRED_REGENERATIONS]
        items = I.unchecked(new, args.root)
        if items:
            I.check_snippets(client, items, root=args.root)
    else:  # pragma: no cover - each pairing regenerates at most once
        raise RuntimeError("regeneration did not converge")
    if args.dry_run:
        print(f"E13_DRYRUN survival ok={surv['ok']} dropped={surv['dropped']} (nothing written)")
        return 0
    root = Path(args.root or C.REPO_ROOT)
    sha = N.write_jsonl(root / C.INFERRED_PAIRS_PATH, rows)
    N.write_json(root / C.INFERRED_SURVIVAL_PATH, {
        **surv, "pairs_path": C.INFERRED_PAIRS_PATH, "pairs_sha256": sha,
        "writer": {"model": C.WRITER_MODEL, "temperature": C.WRITER_TEMPERATURE,
                   "seed": C.WRITER_SEED, "max_tokens": I.WRITER_MAX_TOKENS,
                   "prompt": I.WRITER_PROMPT,
                   "prompt_sha256": N.prompt_sha256(I.WRITER_PROMPT, args.root),
                   "mechanism": C.INFERRED_MECHANISM},
        "checker": {"model": llm.MODELS["luna"].model, "temperature": I.CHECKER_TEMPERATURE,
                    "seeds": list(I.CHECKER_SEEDS), "prompt": I.CHECKER_PROMPT,
                    "prompt_sha256": N.prompt_sha256(I.CHECKER_PROMPT, args.root)},
    })
    for pid, v in surv["per_pairing"].items():
        print(f"E13_INFERRED_PAIRING {pid}: {v['status']} kept {v['n_kept']}/{v['quota']} "
              f"drops {v['drops']}")
    if surv["dropped"]:
        print(f"E13_INFERRED_DROPPED {surv['n_pairings_ok']} of {len(C.INFERRED_PAIRS)} pairings "
              f"survive (< {C.INFERRED_MIN_PAIRINGS}): the battery is dropped and reported")
    print(f"E13_DONE inferred {sha}")
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
