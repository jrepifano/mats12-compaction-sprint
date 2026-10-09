"""Judge E12's generation CSVs in phases with exact row masks (plan v0.3.1 "Judging phases").

Each (kind, key) output `results/E12/<recipe>/gen/<kind>/<key>.csv` is judged into
`contracts.judged_csv(recipe, kind, key)` through `emx.judge_run.run_judging_reserved` (exp
"E12", note prefix `"E12 judge <recipe>/<kind>/<key>"`, one API reservation per (metric, judge)
column, resumable, each column signed with its prompt, judge settings and source CSV). Prompts:
the E1 aligned and coherent rubrics (`emx.prompts`), as EQ0.

Phases (`decide --stage S` or `main`):
- `decide --stage poison`: needs `MANIFEST.eval-poison`; luna aligned + coherent on the
  `general` rows only of full/P1-P3.
- `decide --stage repair107`: for each seed, full/A<s>_r107 and full/C<s>_r107 must verify or
  carry a terminal skip record (`contracts.job_status_path`; that seed is then not eligible);
  B's are judged when they verify and never block. luna, `general` rows only.
- `decide --stage rescue214`: needs the `repair107` decision with `rescue_triggered` true; luna,
  `general` rows only, of whichever full/{A,B,C}<s>_r214 outputs verify (the others are listed).
- `main`: needs a `final` decision whose `decisions_sha256` verifies; then both judges (luna,
  gfl) on every remaining row of: full/P1-P3; full/{A,B,C}<s>_r107; full/{A,B,C}<s>_r214 only if
  the endpoint is 214; traj/*; nudge/ from the endpoint plus D and R; cue_tf/ and cue_mc/ for
  base, P1-P3 and the endpoint's A/B/C. The set is `contracts.planned_jobs(recipe, path,
  endpoint, cuts 1-5 of the final decision, rescue_triggered)` without task jobs (the task judge
  scores those) and without the 214 full evaluations unless the endpoint is 214; with no
  endpoint (repair not established) that is the pre-decision set (full P and 107, trajectory, the
  base/P cues). The cuts are the final decision's, which froze them from the admission record
  (`contracts.admission_path`; `main` refuses if that record changed since). Rows are masked by
  `contracts.row_selected` (under cut 4 only nudge samples 0-2 are judged, under cut 5 only cue
  samples 0-5, whatever was generated). Every job must verify (or be terminally skipped), else
  `main` refuses.

"Verify" = the CSV's sha256 and row count equal its meta's (and the meta names the job), every
row's condition is `contracts.condition(recipe, key, kind)`, and the columns are GEN_COLUMNS.

Before any call:
1. the spend projection for ALL remaining work of the phase (sum over models x metrics x judges
   of `PRESETS[j].project_usd(filled prompts of the masked rows still to judge)`) is printed and
   checked with `emx.spend.check_total(projected, "judge", running pods, exp="E12")`; above a
   limit the script exits 2 (`contracts.EXIT_BUDGET`) and sends nothing;
2. `emx.judge_panel.check_probe` must confirm each judge's mode and request from
   results/E1/checks/judge_probe.json.
`--dry-run` stops after the projection: no keys read, no clients created, nothing written.
A gate refusal (missing manifest, decision or output) exits 3 (`contracts.EXIT_REFUSED`).

Each paid invocation appends a record to `results/E12/<recipe>/judged/phase.<phase>.jsonl`:
phase, models, skipped and excluded jobs, the projection, the usage, and `decisions_sha256`
(of `final` for `main`; null for the decide stages).

Usage:
  uv run python scripts/e12_judge.py {decide --stage poison|repair107|rescue214 | main}
      [--recipe R] [--dry-run] [--concurrency N] [--max-retries 6] [--running-pod-usd X]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from emx import judge_panel as JP
from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS
from emx.e12 import contracts as C
from emx.e12 import llm

PROJECTION_CALIBRATION = round(1.16 / 1.89 * 1.2, 3)  # EQ0 billed / projected, x 1.2
TAG = "E12 JUDGE"
DECIDE_JUDGES: tuple[str, ...] = ("luna",)


def pod_events() -> Path:
    from emx import runpod  # lazy: pulls in requests

    return runpod.LOGS / f"pods_{C.EXP}.jsonl"


def phase_record_path(recipe: str, phase: str) -> str:
    return f"{C.rdir(recipe)}/judged/phase.{phase}.jsonl"


def check_gen(root: Path, recipe: str, kind: str, key: str) -> pd.DataFrame:
    gen = root / C.gen_csv(recipe, kind, key)
    df = JR.read_gen(gen)
    if tuple(df.columns) != GEN_COLUMNS:
        raise llm.Refused(f"{gen}: columns {list(df.columns)} != GEN_COLUMNS")
    cond = C.condition(recipe, key, kind)
    if set(df["condition"].astype(str)) != {cond}:
        raise llm.Refused(f"{gen}: condition is not {cond!r} on every row")
    return df


def _manifest(root: Path, recipe: str, step: str) -> dict[str, str]:
    p = root / C.manifest_path(recipe, step)
    if not p.exists():
        raise llm.Refused(f"{C.manifest_path(recipe, step)} is absent")
    out = {}
    for line in p.read_text().splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0]
    return out


# --- selection per phase ------------------------------------------------------------------------


def select(phase: str, root: Path, recipe: str) -> dict:
    """{"models": [(kind, key)], "skipped": [...], "excluded": [...], "judges", "mask",
    "decisions_sha256", "cuts"} for the phase, or raises llm.Refused."""
    sel = {"models": [], "skipped": [], "excluded": [], "judges": list(DECIDE_JUDGES),
           "mask": "general", "decisions_sha256": None, "cuts": []}

    def take(kind: str, key: str, *, required: bool) -> str:
        state = llm.output_state(root, recipe, kind, key)
        if state == "verified":
            sel["models"].append((kind, key))
        elif state == "skipped":
            sel["skipped"].append(f"{kind}/{key}")
        elif required:
            raise llm.Refused(f"{kind}/{key}: output {state}, not verified")
        else:
            sel["excluded"].append(f"{kind}/{key}: {state}")
        return state

    if phase == "poison":
        if C.BASE_FULL_SOURCE == "e12-hf":  # base EM for induction from the arms' generator
            take("full", C.BASE_KEY, required=True)
        listed = _manifest(root, recipe, "eval-poison")
        for s in C.TRAIN_SEEDS:
            if take("full", f"P{s}", required=True) != "verified":
                raise llm.Refused(f"full/P{s} is terminally skipped: no induction read")
            rel = C.gen_csv(recipe, "full", f"P{s}")
            if rel in listed and listed[rel] != llm.sha256_file(root / rel):
                raise llm.Refused(f"{rel}: sha256 differs from MANIFEST.eval-poison")
    elif phase == "repair107":
        for s in C.TRAIN_SEEDS:
            for arm in C.REPAIR_ARMS:
                take("full", C.format_key(arm, s, 107), required=arm != "B")
    elif phase == "rescue214":
        rep = llm.decision_stage(llm.load_decisions(root, recipe), "repair107")
        if not rep or rep.get("rescue_triggered") is not True:
            raise llm.Refused("the repair107 decision does not trigger the rescue")
        for s in C.TRAIN_SEEDS:
            for arm in C.REPAIR_ARMS:
                take("full", C.format_key(arm, s, C.RESCUE_END), required=False)
    elif phase == "main":
        final, dsha = llm.require_final(root, recipe)
        llm.check_final_admission(root, recipe, final)  # the cuts' one source (r1 finding 6)
        ep = final.get("endpoint")
        cuts = tuple(c for c in llm.final_cuts(final) if c != 0)
        rescue = final.get("rescue_triggered")
        jobs = C.planned_jobs(recipe, "vllm" if ep is not None else "hf", ep, cuts,
                              bool(rescue) if rescue is not None else None)
        if C.BASE_FULL_SOURCE == "e12-hf":
            take("full", C.BASE_KEY, required=True)
        for j in jobs:
            if j.kind == "task":
                continue
            k = C.parse_key(j.key)
            if j.kind == "full" and k.repair == C.RESCUE_END and ep != C.RESCUE_END:
                continue
            take(j.kind, j.key, required=True)
        sel |= {"judges": list(C.JUDGES), "mask": "cuts", "decisions_sha256": dsha,
                "cuts": list(llm.final_cuts(final)), "endpoint": ep}
    else:
        raise ValueError(f"unknown phase {phase!r}")
    return sel


def row_mask(gen_df: pd.DataFrame, kind: str, sel: dict) -> pd.Series | None:
    """The rows to judge: `general` rows for the decide stages; for `main`, the rows
    `contracts.row_selected` keeps under the final cuts (the one selector judging, validation
    and analysis share; review r1 finding 7), or None when that is every row."""
    if sel["mask"] == "general":
        return gen_df["qset"].astype(str) == "general"
    cuts = sel["cuts"]
    keep = pd.Series([C.row_selected(kind, q, int(i), cuts) for q, i in
                      zip(gen_df["qset"].astype(str), gen_df["sample_idx"], strict=True)],
                     index=gen_df.index, dtype=bool)
    return None if keep.all() else keep


def project(root: Path, recipe: str, sel: dict, presets: dict) -> dict:
    """{"kind/key": {column: {rows_todo, projected_usd}}} for the masked rows still to judge."""
    out: dict = {}
    for kind, key in sel["models"]:
        check_gen(root, recipe, kind, key)
        gen_df, jdf, _ = JR.load_or_init_judged(root / C.gen_csv(recipe, kind, key),
                                                root / C.judged_csv(recipe, kind, key))
        mask = row_mask(gen_df, kind, sel)
        cols = {}
        for m in C.METRICS:
            for j in sel["judges"]:
                rows = JR.todo_rows(jdf, m, j, mask)
                usd = presets[j].project_usd(JR.filled_prompts(gen_df, rows, m)) if rows else 0.0
                cols[f"{m}__{j}"] = {"rows_todo": len(rows), "projected_usd": usd}
        out[f"{kind}/{key}"] = cols
    return out


def append_phase_record(root: Path, recipe: str, phase: str, rec: dict) -> None:
    p = root / phase_record_path(recipe, phase)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def main(argv=None, *, presets: dict | None = None, clients: dict | None = None,
         **judge_kw) -> int:
    """`presets`, `clients` and `judge_kw` (e.g. `wait`) are test hooks; the CLI passes none."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--stage", required=True, choices=C.JUDGE_PHASES[:3])
    m = sub.add_parser("main")
    for p in (d, m):
        p.add_argument("--recipe", choices=sorted(C.RECIPES), default=C.DEFAULT_RECIPE)
        p.add_argument("--dry-run", action="store_true", help="print the projection only")
        p.add_argument("--concurrency", type=int, default=8)
        p.add_argument("--max-retries", type=int, default=6)
        p.add_argument("--running-pod-usd", type=float, default=None,
                       help="running pods' projected $ (default: from logs/pods_E12.jsonl)")
        p.add_argument("--probe", default=str(JP.PROBE_PATH))
        p.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    phase = args.stage if args.cmd == "decide" else "main"
    root, recipe = Path(args.root), args.recipe
    presets = presets or JP.PRESETS
    try:
        sel = select(phase, root, recipe)
        proj = project(root, recipe, sel, presets)
    except (llm.Refused, OSError, ValueError, RuntimeError) as e:
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        print(json.dumps({"phase": phase, "refused": "gate", "error": str(e)}))
        return C.EXIT_REFUSED
    raw_total = sum(c["projected_usd"] for cols in proj.values() for c in cols.values())
    # The presets' char-based bound overstated EQ0's metered judging 1.63x ($1.89 projected,
    # $1.16 billed: logs/EQ0-report.md); the cap check uses that ratio with a 1.2 safety factor.
    # Each column's reservation inside run_judging_reserved still uses the full bound, so the cap
    # itself can never be overspent (integration, 2026-10-08, on U6's cost finding).
    total = raw_total * PROJECTION_CALIBRATION
    pods = (args.running_pod_usd if args.running_pod_usd is not None
            else JR.running_pod_usd_from_log(pod_events()))
    for mk, cols in proj.items():
        for col, c in cols.items():
            print(f"projection {mk} {col}: {c['rows_todo']} rows, ${c['projected_usd']:.4f}")
    print(f"{TAG} PROJECTION ({phase}): ${total:.4f} calibrated (bound ${raw_total:.4f} x "
          f"{PROJECTION_CALIBRATION}) for all remaining work over {len(proj)} models; running "
          f"pods ${pods:.2f}")
    if sel["skipped"] or sel["excluded"]:
        print(f"{TAG}: skipped {sel['skipped']}; excluded {sel['excluded']}")
    try:
        guard = spend.check_total(total, "judge", running_pod_usd=pods, exp=C.EXP)
    except spend.BudgetExceeded as e:
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        print(json.dumps({"phase": phase, "refused": "budget", "projected_usd": total,
                          "breakdown": e.breakdown}))
        return C.EXIT_BUDGET
    summary = {"phase": phase, "recipe": recipe, "projected_usd": round(total, 6),
               "projected_bound_usd": round(raw_total, 6),
               "projection_calibration": PROJECTION_CALIBRATION,
               "models": [f"{k}/{v}" for k, v in sel["models"]], "skipped": sel["skipped"],
               "excluded": sel["excluded"], "judges": sel["judges"],
               "decisions_sha256": sel["decisions_sha256"]}
    if args.dry_run:
        print(json.dumps({"dry_run": True, **summary, "guard": guard, "columns": proj},
                         indent=2))
        print(json.dumps({"dry_run": True, **summary}))
        return C.EXIT_OK
    if clients is None:
        try:
            for j in sel["judges"]:
                JP.check_probe(presets[j], Path(args.probe))
        except RuntimeError as e:
            print(f"{TAG} REFUSED: {e}", file=sys.stderr)
            print(json.dumps({"phase": phase, "refused": "probe", "error": str(e)}))
            return C.EXIT_REFUSED
        JP.load_env()
        clients = {j: JP.make_client(presets[j].route) for j in sel["judges"]}
    started = spend.utc_now()
    usage: dict = {}
    try:
        for kind, key in sel["models"]:
            mk = f"{kind}/{key}"
            if not any(c["rows_todo"] for c in proj[mk].values()):
                continue
            gen = root / C.gen_csv(recipe, kind, key)
            mask = row_mask(JR.read_gen(gen), kind, sel)
            res = JR.run_judging_reserved(
                gen, root / C.judged_csv(recipe, kind, key), list(C.METRICS), sel["judges"],
                clients, exp=C.EXP, note_prefix=f"{C.NOTE_PREFIX} {recipe}/{kind}/{key}",
                presets=presets, row_masks=({mt: mask for mt in C.METRICS}
                                            if mask is not None else None),
                max_concurrency=args.concurrency, max_retries=args.max_retries,
                running_pod_usd=args.running_pod_usd, pod_events=pod_events(), **judge_kw,
            )
            usage[mk] = {col: {f: u.get(f) for f in ("rows_todo", "calls", "usd", "n_scored",
                                                     "n_none", "n_failed") if f in u}
                         for col, u in res.items()}
    finally:
        usd = sum(float(u.get("usd") or 0.0) for cols in usage.values() for u in cols.values())
        append_phase_record(root, recipe, phase, {
            **summary, "utc_start": started, "utc_end": spend.utc_now(), "usd": round(usd, 6),
            "usage": usage})
    print(f"{TAG} DONE ({phase}): ${usd:.4f} this run over {len(usage)} models")
    print(json.dumps({**summary, "usd": round(usd, 6)}))
    return C.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
