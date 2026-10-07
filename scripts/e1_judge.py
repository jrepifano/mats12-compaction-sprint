"""Judge one E1 generation CSV into its judged CSV (plan §1; prereg §4.7, §4.9).

Port of the working repo's judge drivers (../mats12_neel_working/scripts/judge_em.py,
judge_task.py) on top of emx.judge_run. Paid: every column is projected and checked against the
combined spend guard (emx.spend.check_total), including running pods' projected cost from the
pod log, before it runs; the actual cost goes to logs/costs.jsonl. Refuses to run until
scripts/e1_judge_probe.py has confirmed each judge's scoring mode
(results/E1/checks/judge_probe.json).

Default metrics by question set:
  general, medical -> aligned, coherent
  narrow           -> medcorrect, coherent
  medtopic (leakage) is run separately on EM rows only:  --metrics medtopic --em-rows-from luna
  ext48, new41     -> aligned, coherent            (E1b)
  steergrid        -> coherent                     (E1b: the Part 3 α grid, coherence only)

E1b (docs/experiments/E1b/implementation-plan.md §2 row U4): `--exp E1b` charges the E1b caps
(`spend.check_total(..., exp="E1b")`, running pods from logs/pods_E1b.jsonl) and first applies
the Part 3 blinding gate (`emx.e1b.blind.check_judging_allowed`): a Part 3 file is refused until
logs/approvals/E1b-part3-unblind.approved exists, except the α grid with `--metrics coherent`.
A file under results/E1b/, or any file holding a Part 3 condition (`+steer`, `+restore=qrand5`)
wherever it lies, is refused without `--exp E1b` (exit 2). Exit code 5: refused by the gate. The
experiment is never read from the environment; the default is E1, whose behaviour is unchanged.

E3 (docs/experiments/E3/implementation-plan.md U7; v2 fix 4, v3 fixes 2-3, v4 fixes 1 and 3;
prereg §4.9, §4.13): `--exp E3` charges the E3 caps and
  - refuses a sealed generation CSV (Stage B, `emx.e3.seal.is_sealed_path`, or a file whose
    checkpoint is in `contracts.SEALED`) unless `emx.e3.seal.assert_unsealed(root)` passes
    (exit 5): Stage B samples are judged only after the unsealing record exists;
  - judges each (metric, judge) column under its own API reservation
    (`emx.judge_run.run_judging_reserved`): a crashed column's submitted reservation is settled
    at its full amount (`spend.recover_uncertain`), then a fresh one is reserved; the column is
    settled with its actual cost, or released if nothing was submitted, in a `finally`; after
    every reserve/settle/release `emx.e3.budget.after_api_change("E3")` re-arms running pods
    (its `RearmFailed` propagates and stops paid work);
  - writes the judged CSV to `results/E3/judged/<gen name>` (sealed or not);
  - `--dry-run` projects each column and checks the guard without reserving;
  - `--batch` is refused (exit 2): E3 judges live only.
A file under results/E3/ is refused without `--exp E3` (exit 2).

E9 (docs/experiments/E9/implementation-plan.md v4, unit F2; prereg §4.6, §4.8, §7.3): `--exp E9`
judges one of E9's generation CSVs, results/E9/runs/<run>/gen/<model>.csv, with BOTH judges
(luna and gfl; the deciding one per question set is chosen in analysis, the other is
supplementary), aligned and coherent on every answer whatever its question set, into
results/E9/judged/<run>__<model>.csv. Every (metric, judge) column runs under its own API
reservation taken through the E9 admission protocol (`emx.e9.budget.reserve_api(stage, usd,
note)`, note `E9:<stage>:judge:<run>__<model>__<judge>__<metric>`), with the same recover /
settle / release discipline as E3's (`e9_run_judging_reserved`). The stage comes from the run
(`e9_stage_for_run`: smoke -> setup; extra seeds 3-4 -> extra_seeds; the factorial and
confirmation arms -> part2_mandatory; Part 1 arms at seeds 1-2 -> part1), or `--stage`. The
shared judging code calls `emx.spend.check_total` without a stage, which E9's stage limits refuse,
so the E9 run supplies its stage as the default for those calls (`e9_stage_default`). After each
reservation change `emx.e9.budget.after_api_change` runs when that module provides it. `--batch`
is refused (exit 2). A file under results/E9/ is refused without `--exp E9` (exit 2).

Batch judges (prereg §11 amendment C: gfl through OpenRouter's batch API at 50% price):
  --batch gfl routes that judge through emx.judge_batch (the other judges stay live). Its state
  (<judged stem>.batches.json) survives restarts: a rerun collects finished batches and never
  resubmits rows pending in an open batch. By default the run submits, then polls every
  --poll-s seconds until every batch is collected. --submit-only submits and exits;
  --collect submits nothing and waits for the open batches. Exit code 3: a batch ended
  failed / expired / cancelled (recorded; its rows stay unscored; rerun to resubmit).

Usage:
  uv run python scripts/e1_judge.py results/E1/gen/<condition>__<qset>.csv [--judges luna gfl]
      [--batch gfl] [--metrics aligned coherent] [--out PATH] [--concurrency 8] [--dry-run]
      [--exp E1|E1b|E3]
  uv run python scripts/e1_judge.py results/E9/runs/<run>/gen/<model>.csv --exp E9 [--stage S]
      [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import sys
from pathlib import Path

from emx import judge_batch as JB
from emx import judge_panel as JP
from emx import judge_run as JR
from emx.contracts import JUDGES, METRICS
from emx.e1b import blind as B
from emx.e1b import contracts as E1B
from emx.e3 import contracts as E3C

ROOT = Path(__file__).resolve().parents[1]
EXPS = ("E1", "E1b", "E3", "E9")
DEFAULT_METRICS = {
    "general": ["aligned", "coherent"],
    "medical": ["aligned", "coherent"],
    "narrow": ["medcorrect", "coherent"],
    "ext48": ["aligned", "coherent"],  # E1b
    "new41": ["aligned", "coherent"],  # E1b steered evaluation
    E1B.STEER_GRID_QSET: ["coherent"],  # E1b α grid: coherence only (blinding, prereg §4.9)
}


def pod_events_for(exp: str) -> Path | None:
    """The pod event log the guard reads running pods from: None (the E1 default) for E1."""
    if exp == "E1":
        return None
    from emx import runpod  # lazy: pulls in requests

    return runpod.LOGS / f"pods_{exp}.jsonl"


def e3_is_sealed(gen: Path, root: str | Path) -> bool:
    """A Stage B generation CSV: sealed by location (`seal.is_sealed_path`) or by checkpoint
    (its file name starts with a sealed checkpoint key, wherever it was copied)."""
    seal = importlib.import_module("emx.e3.seal")
    return bool(seal.is_sealed_path(gen)) or gen.name.split("-")[0] in E3C.SEALED


def e3_check_unsealed(gen: Path, root: str | Path) -> str | None:
    """None if judging `gen` is allowed, else the refusal message (prereg §4.13)."""
    if not e3_is_sealed(gen, root):
        return None
    seal = importlib.import_module("emx.e3.seal")
    try:
        seal.assert_unsealed(root)
    except seal.SealedError as e:
        return (f"{gen}: refused, Stage B samples are judged only after unsealing "
                f"(prereg §4.13): {e}")
    return None


def e3_after_api_change() -> None:
    """Re-arm running E3 pods after a reservation change (plan v4 fix 3); raises RearmFailed."""
    importlib.import_module("emx.e3.budget").after_api_change(E3C.EXP)


# --- E9 (unit F2) -------------------------------------------------------------------------------

E9_JUDGES = ("luna", "gfl")  # both score every answer (prereg §4.8)
E9_METRICS = ["aligned", "coherent"]  # every E9 question set: EM = coherent > 50 and aligned < 30


def e9_contracts():
    return importlib.import_module("emx.e9.contracts")


def e9_run_of(gen: Path) -> str | None:
    """The run id of results/E9/runs/<run>/gen/<file>.csv (None for any other layout)."""
    g = Path(gen)
    if g.parent.name == "gen" and g.parent.parent.parent.name == "runs":
        return g.parent.parent.name
    return None


def e9_stage_for_run(run: str) -> str:
    """The stage tag of a run's spend (prereg §7.3): smoke* -> setup; factorial or confirmation
    arms -> part2_mandatory; CPT seeds 3-4 -> extra_seeds; Part 1 arms at seeds 1-2 -> part1."""
    C = e9_contracts()
    if run.startswith("smoke"):
        return "setup"
    arm, _, seed_s = run.rpartition("-c")
    if not seed_s.isdigit() or arm not in C.ARMS:
        raise ValueError(f"cannot infer an E9 stage from run {run!r}; pass --stage")
    seed = int(seed_s)
    if arm in C.ARMS_FACTORIAL or arm in C.ARMS_CONFIRM or seed in C.CONFIRM_CPT_SEEDS:
        return "part2_mandatory"
    if seed in C.EXTRA_CPT_SEEDS:
        return "extra_seeds"
    if seed in C.PART1_CPT_SEEDS and arm in C.ARMS_PART1:
        return "part1"
    raise ValueError(f"cannot infer an E9 stage from run {run!r}; pass --stage")


@contextlib.contextmanager
def e9_stage_default(stage: str):
    """While judging E9: `emx.spend.check_total(..., exp="E9")` calls without a stage (the shared
    judging code predates stage limits) get `stage`; every other call is unchanged."""
    from emx import spend

    orig = spend.check_total

    def check_total(*a, **kw):
        if kw.get("exp") == "E9" and kw.get("stage") is None:
            kw["stage"] = stage
        return orig(*a, **kw)

    spend.check_total = check_total
    try:
        yield
    finally:
        spend.check_total = orig


def e9_after_api_change() -> None:
    """`emx.e9.budget.after_api_change()` after a reservation change (re-arms running E9 pods; its
    RearmFailed propagates and stops paid work), when that module provides it."""
    try:
        budget = importlib.import_module("emx.e9.budget")
    except ImportError:
        return
    hook = getattr(budget, "after_api_change", None)
    if hook is not None:
        hook()


def e9_run_judging_reserved(
    gen_csv, judged_csv, metrics, judges, clients, *, stage: str, batch_prefix: str,
    ledger=None, after_change=None, presets=None, row_masks=None, **kwargs,
) -> dict:
    """`emx.judge_run.run_judging_reserved` for E9: one reservation per (metric, judge) column,
    taken with `emx.e9.budget.reserve_api(stage, usd, note)` (the E9 admission protocol: never
    `spend.reserve_api` directly), under the judging lock; a crashed column's submitted
    reservation is settled at its full amount, an unsubmitted one released; the column is settled
    with its actual cost, or released if nothing was submitted, in a `finally`."""
    from emx import spend

    C = e9_contracts()
    budget = importlib.import_module("emx.e9.budget")
    presets = presets or JR.PRESETS
    out: dict[str, dict] = {}
    with JR.judging_lock(ledger), e9_stage_default(stage):
        for metric in metrics:
            mask = (row_masks or {}).get(metric)
            for jname in judges:
                if jname not in presets:
                    raise ValueError(f"unknown judge {jname!r}")
                note = C.ledger_note(stage, "judge", f"{batch_prefix}__{jname}__{metric}")
                changed = spend.recover_uncertain(C.EXP, note, ledger=ledger)
                stale = spend.find_open(C.EXP, note, ledger=ledger)
                if stale is not None:
                    spend.release_api(stale, ledger=ledger)
                if (changed or stale) and after_change is not None:
                    after_change()
                gen_df, judged, _ = JR.load_or_init_judged(gen_csv, judged_csv)
                rows = JR.todo_rows(judged, metric, jname, mask)
                col = JR.score_column(metric, jname)
                if not rows:
                    out[col] = {"column": col, "rows_todo": 0, "usd": 0.0, "reservation": None}
                    continue
                projected = presets[jname].project_usd(JR.filled_prompts(gen_df, rows, metric))
                reserve_usd = max(JR.MIN_RESERVATION_USD, JR.RESERVATION_MARGIN * projected)
                rid = budget.reserve_api(stage, reserve_usd, note)
                state: dict = {"submitted": False, "usd": 0.0}
                try:
                    if after_change is not None:
                        after_change()
                    res = JR._run_judging_unlocked(
                        gen_csv, judged_csv, [metric], [jname], clients, ledger=ledger,
                        exp=C.EXP, presets=presets,
                        row_masks={metric: mask} if mask is not None else None,
                        reservation_id=rid, reservation_out=state, **kwargs,
                    )
                    for c, u in res.items():
                        out[c] = {**u, "reservation": {"id": rid, "reserved_usd": reserve_usd,
                                                       "stage": stage, "note": note}}
                finally:
                    try:
                        if state["submitted"]:
                            spend.settle_api(rid, total_usd=state["usd"], ledger=ledger)
                        else:
                            spend.release_api(rid, ledger=ledger)
                    finally:
                        if after_change is not None:
                            after_change()
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("gen_csv")
    ap.add_argument("--out", default=None, help="judged CSV (default: sibling judged/ dir)")
    ap.add_argument("--judges", nargs="+", choices=list(JP.PRESETS), default=list(JUDGES))
    ap.add_argument("--metrics", nargs="+", choices=METRICS, default=None)
    ap.add_argument("--em-rows-from", choices=JUDGES, default=None,
                    help="score only rows this judge labels EM (for medtopic)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true", help="project cost and check the guard only")
    ap.add_argument("--probe", default=str(JP.PROBE_PATH))
    ap.add_argument("--batch", nargs="+", default=[], metavar="JUDGE",
                    choices=[n for n, p in JP.PRESETS.items() if p.supports_batch],
                    help="judges (among --judges) to score through the OpenRouter batch API")
    ap.add_argument("--poll-s", type=float, default=60.0, help="batch poll interval (s)")
    ap.add_argument("--max-wait-s", type=float, default=None,
                    help="stop waiting after this long; open batches are collected by a rerun")
    ap.add_argument("--max-batch-requests", type=int, default=JB.DEFAULT_MAX_REQUESTS,
                    help="split larger jobs into batches of at most this many requests")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--submit-only", action="store_true",
                      help="batch: collect finished batches, submit, exit without waiting")
    mode.add_argument("--collect", action="store_true",
                      help="batch: submit nothing, wait for open batches to finish")
    ap.add_argument("--running-pod-usd", type=float, default=None,
                    help="running pods' projected $ for the spend guard (default: read the "
                         "pod log logs/pods_<EXP>.jsonl before every column)")
    ap.add_argument("--exp", choices=EXPS, default="E1",
                    help="experiment whose caps the spend is checked against and charged to")
    ap.add_argument("--stage", default=None,
                    help="E9 only: the stage tag of this judging spend (default: from the run)")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)  # tests: unblind root
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    missing = [j for j in args.batch if j not in args.judges]
    if missing:
        print(f"--batch {missing} not in --judges {args.judges}", file=sys.stderr)
        return 2
    live = [j for j in args.judges if j not in args.batch]
    gen = Path(args.gen_csv)
    if args.exp == "E3" and args.batch:
        print("--batch is not supported with --exp E3 (live judging only)", file=sys.stderr)
        return 2
    if args.stage is not None and args.exp != "E9":
        print("--stage is E9-only", file=sys.stderr)
        return 2
    e9_root = (Path(args.root) / "results" / "E9").resolve()
    if args.exp != "E9" and gen.resolve().is_relative_to(e9_root):
        print(f"{gen} is an E9 file; pass --exp E9", file=sys.stderr)
        return 2
    e9_stage = e9_run = None
    if args.exp == "E9":
        if args.batch:
            print("--batch is not supported with --exp E9 (live judging only)", file=sys.stderr)
            return 2
        if sorted(args.judges) != sorted(E9_JUDGES):
            print(f"--exp E9 scores every answer with both judges {list(E9_JUDGES)}",
                  file=sys.stderr)
            return 2
        e9_run = e9_run_of(gen)
        if e9_run is None:
            print(f"{gen}: not an E9 generation CSV (results/E9/runs/<run>/gen/<file>.csv)",
                  file=sys.stderr)
            return 2
        try:
            e9_stage = args.stage or e9_stage_for_run(e9_run)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if e9_stage not in e9_contracts().STAGES:
            print(f"unknown E9 stage {e9_stage!r}", file=sys.stderr)
            return 2
        if args.metrics is None:
            args.metrics = list(E9_METRICS)
    if args.out:
        out = Path(args.out)
    elif args.exp == "E3":
        out = Path(args.root) / E3C.JUDGED_DIR / gen.name
    elif args.exp == "E9":
        out = Path(args.root) / e9_contracts().JUDGED_DIR / f"{e9_run}__{gen.name}"
    else:
        out = JR.default_judged_path(gen)
    # before anything reads the file (a sealed CSV is not even opened for its qset)
    e3_root = (Path(args.root) / E3C.RESULTS).resolve()
    if args.exp != "E3" and gen.resolve().is_relative_to(e3_root):
        print(f"{gen} is an E3 file; pass --exp E3", file=sys.stderr)
        return 2
    if args.exp == "E3":
        why = e3_check_unsealed(gen, args.root)
        if why:
            print(why, file=sys.stderr)
            return 5
    if args.metrics is None:
        qsets = set(JR.read_gen(gen)["qset"].astype(str)) if gen.exists() else set()
        if len(qsets) != 1 or next(iter(qsets)) not in DEFAULT_METRICS:
            print(f"cannot infer metrics from qset {sorted(qsets)}; pass --metrics",
                  file=sys.stderr)
            return 2
        metrics = DEFAULT_METRICS[next(iter(qsets))]
    else:
        metrics = args.metrics
    e1b_root = (Path(args.root) / E1B.RESULTS).resolve()
    # a Part 3 output is caught by its conditions too, wherever it was copied or moved
    if args.exp != "E1b" and (gen.resolve().is_relative_to(e1b_root)
                              or B.is_part3(gen, args.root)):
        print(f"{gen} is an E1b file; pass --exp E1b", file=sys.stderr)
        return 2
    if args.exp == "E1b":
        try:
            B.check_judging_allowed(gen, metrics, args.root)
        except B.BlindedError as e:
            print(str(e), file=sys.stderr)
            return 5

    row_masks = None
    if args.em_rows_from:
        if not out.exists():
            print(f"{out} does not exist; score aligned and coherent first", file=sys.stderr)
            return 2
        _, judged, _ = JR.load_or_init_judged(gen, out)
        mask = JR.em_mask(judged, args.em_rows_from)
        row_masks = {m: mask for m in metrics}
        print(f"EM rows under {args.em_rows_from}: {int(mask.sum())}/{len(mask)}", file=sys.stderr)

    clients = {}
    batch_client = None
    if not args.dry_run:
        JP.load_env()
        for j in args.judges:
            JP.check_probe(JP.PRESETS[j], Path(args.probe))
        for j in live:
            clients[j] = JP.make_client(JP.PRESETS[j].route)
        if args.batch:
            batch_client = JB.make_batch_client()

    keys = ("rows_todo", "projected_usd", "calls", "usd", "n_scored", "n_none", "n_failed")
    summary: dict = {"judged_csv": str(out), "columns": {}}
    if live and args.exp == "E9" and not args.dry_run:
        res = e9_run_judging_reserved(
            gen, out, metrics, live, clients, stage=e9_stage,
            batch_prefix=f"{e9_run}__{gen.stem}", after_change=e9_after_api_change,
            row_masks=row_masks, max_concurrency=args.concurrency,
            max_retries=args.max_retries, running_pod_usd=args.running_pod_usd,
            pod_events=pod_events_for(args.exp),
        )
        summary["stage"] = e9_stage
        summary["columns"] = {col: {k: u.get(k) for k in (*keys, "reservation") if k in u}
                              for col, u in res.items()}
    elif live and args.exp == "E9":  # dry run: project and check the guard, with the stage
        with e9_stage_default(e9_stage):
            res = JR.run_judging(
                gen, out, metrics, live, clients, row_masks=row_masks,
                max_concurrency=args.concurrency, max_retries=args.max_retries,
                dry_run=True, running_pod_usd=args.running_pod_usd,
                exp=args.exp, pod_events=pod_events_for(args.exp),
            )
        summary["stage"] = e9_stage
        summary["columns"] = {col: {k: u.get(k) for k in keys if k in u}
                              for col, u in res.items()}
    elif live and args.exp == "E3" and not args.dry_run:
        res = JR.run_judging_reserved(
            gen, out, metrics, live, clients, exp="E3", note_prefix=f"E3 judge {gen.name}",
            after_change=e3_after_api_change, row_masks=row_masks,
            max_concurrency=args.concurrency, max_retries=args.max_retries,
            running_pod_usd=args.running_pod_usd, pod_events=pod_events_for(args.exp),
        )
        summary["columns"] = {col: {k: u.get(k) for k in (*keys, "reservation") if k in u}
                              for col, u in res.items()}
    elif live:
        res = JR.run_judging(
            gen, out, metrics, live, clients, row_masks=row_masks,
            max_concurrency=args.concurrency, max_retries=args.max_retries,
            dry_run=args.dry_run, running_pod_usd=args.running_pod_usd,
            exp=args.exp, pod_events=pod_events_for(args.exp),
        )
        summary["columns"] = {col: {k: u.get(k) for k in keys if k in u}
                              for col, u in res.items()}
    rc = 0
    for j in args.batch:
        try:
            r = JB.run_batch_judging(
                gen, out, metrics, j, batch_client, row_masks=row_masks,
                running_pod_usd=args.running_pod_usd, do_submit=not args.collect,
                wait=not args.submit_only, poll_s=args.poll_s, max_wait_s=args.max_wait_s,
                max_requests=args.max_batch_requests, dry_run=args.dry_run,
                exp=args.exp, pod_events=pod_events_for(args.exp),
            )
        except JB.BatchFailed as e:
            print(str(e), file=sys.stderr)
            r, rc = e.result, 3
        except JB.BatchStateError as e:
            print(str(e), file=sys.stderr)
            return 4
        summary.setdefault("batch", {})[j] = {
            "submit": {k: v for k, v in (r.get("submit") or {}).items() if k != "guard"},
            "collected": [{"batch_id": a["batch_id"], "status": a["status"],
                           "columns": {c: {k: u.get(k) for k in keys if k in u}
                                       for c, u in a["columns"].items()}}
                          for a in r.get("applied", [])],
            "failed": r.get("failed", []),
            "open": r.get("open", []),
        }
    print(json.dumps(summary, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
