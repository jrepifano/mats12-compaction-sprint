"""E12 workload projection and pre-set cuts (prereg §7; plan v0.3 "Paid steps").

Owner: unit U6. FROZEN SIGNATURES (U0). STDLIB ONLY: it also runs on the pod (the `smoke` and
`lora-check` steps admit or refuse the remaining workload).

Prices `contracts.planned_jobs(recipe, gen_path, endpoint, cuts)` (minus outputs that already
verify) plus the remaining training steps, with rates from the bench records
(`contracts.BENCH_FIELDS`), falling back to the shared note's rates (labelled in `rates_source`).
GPU hours = elapsed + remaining (training, generation, engine starts, applied smokes, upload,
margin); GPU $ = hours x the pod's rate; API $ = the judging the jobs imply (judges, task judge,
style audit) at $0.21-0.25 per 1,000 fully judged answers (upper rate used). Admit when GPU $ <=
GPU_LIMIT_USD, total pod hours <= POD_HOURS_LIMIT and API $ + recorded API spend <=
API_LIMIT_USD; otherwise apply cuts 0, 1, 2, ... in order and re-project, and refuse if all five
do not fit.

Implementation notes (U6):
- **Additive keyword.** `remaining_jobs`, `project` and `admit` also take `rescue_triggered`
  (default None), passed to `planned_jobs`; the CLI's `--rescue-triggered` sets it. The
  conservative complete HF branch that `admit-hf` prices is `--gen-path hf --endpoint 214
  --rescue-triggered`.
- **Remaining work.** A job is done when its output verifies on its own (CSV sha256 and row count
  equal the meta's) or has a terminal skip record. A training run is done when its
  `status/<run>.json` says ok, restarted_ok or failed (a failed run is not retrained); otherwise
  its remaining steps are the segment's steps minus the last step in `train/<run>.steps.jsonl`.
  Every planned run counts (the rescue and every nudge are always trained; cut 3's three base-
  reference nudges are counted anyway, about 25 s).
- **Hours.** train = poison, repair and rescue steps at the poison step time (the bench measures
  padded long poison batches; repair rows are priced at it, conservatively) + nudge steps at the
  short-row step time + `overhead_h` (held-out passes, continuity checks, saves) while any
  training remains; rank 32 multiplies fallback step times by `rank32_factor`. generation =
  remaining answers / the path's answers_per_s. engine_starts = one engine start per eval step
  with remaining jobs. smoke = `SMOKE_S_PER_ADAPTER` per distinct remaining adapter. upload =
  `UPLOAD_H[recipe]` unless the retention receipt exists. margin = `margin_h`.
- **API.** The judged branch, not the generated one: `planned_jobs(recipe, "hf", endpoint or
  214, cuts, rescue_triggered)` (an unknown endpoint is priced as the rescued 214 branch, the
  dearest; explicit `rescue_triggered=False` with no endpoint prices 107). EM kinds (full, traj,
  nudge, cue) at `judge_usd_per_1000_answers`; task answers at `TASK_USD_PER_1000`; the style
  rating of 13 dose-0 models x 41 test questions x 10 samples (5 under cut 0) at
  `STYLE_USD_PER_1000`; plus `STYLE_TRAIN_USD` until `style_audit/frozen.json` exists. Judging
  already recorded is not netted out (conservative).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from emx.e12 import contracts as C

FALLBACK_RATES: dict[str, float] = {  # shared design note §5 (EQ0)
    "rank1_short_s_per_step": 0.187,
    "rank1_poison_s_per_step": 0.7,  # unmeasured guess until train_bench
    "rank32_factor": 2.0,  # unmeasured: rank-32 step time / rank-1 step time
    "vllm_answers_per_s": 30.0,  # conservative for grouped small jobs (EQ0 base: 77)
    "hf_answers_per_s": 5.0,  # prereg §7's 45k answers in 2-3 h
    "engine_start_s": 120.0,
    "judge_usd_per_1000_answers": 0.25,
    "overhead_h": 0.25,
    "margin_h": 0.3,
}
# U6 constants (not measured by any bench): plan "Paid steps" (applied-smoke and upload of
# ~170 adapters ~ 0.15 h) and prereg §7 (task judging ~ $0.4 for 5,200; style audit ~ $1).
SMOKE_S_PER_ADAPTER = 2.5
UPLOAD_H: dict[str, float] = {"rank1": 0.05, "rank32": 0.15}
TASK_USD_PER_1000 = 0.10
STYLE_USD_PER_1000 = 0.15
STYLE_TRAIN_USD = 0.5
EM_KINDS: tuple[str, ...] = ("full", "traj", "nudge", *C.CUE_KINDS)
STEP_OF_KIND: dict[str, str] = {  # eval step that generates a job (one engine start each)
    "traj": "eval-traj", "nudge": "eval-nudge",
}
_LIMITS = {"gpu_usd": C.GPU_LIMIT_USD, "api_usd": C.API_LIMIT_USD,
           "pod_hours": C.POD_HOURS_LIMIT}


# --- inputs -------------------------------------------------------------------------------------


def _sha256_file(p: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bench(recipe: str, name: str, root: Path) -> dict | None:
    p = root / C.bench_path(recipe, name)
    if not p.exists():
        return None
    rec = json.loads(p.read_text())
    missing = [f for f in C.BENCH_FIELDS[name] if f not in rec]
    if missing:
        raise ValueError(f"{p}: missing bench fields {missing}")
    if "recipe" in C.BENCH_FIELDS[name] and rec["recipe"] != recipe:
        raise ValueError(f"{p}: recipe {rec['recipe']!r}, expected {recipe!r}")
    return rec


def load_rates(recipe: str, root: str | Path = C.REPO_ROOT) -> dict:
    """{name: value, "rates_source": {name: "measured:<file>" | "fallback"}} from the recipe's
    bench records where present."""
    if recipe not in C.RECIPES:
        raise ValueError(f"unknown recipe {recipe!r}")
    root = Path(root)
    rates = dict(FALLBACK_RATES)
    src = {k: "fallback" for k in FALLBACK_RATES}

    def measured(name: str, rec_name: str, field: str) -> None:
        rec = _bench(recipe, rec_name, root)
        if rec is not None and float(rec[field]) > 0:
            rates[name] = float(rec[field])
            src[name] = f"measured:bench/{rec_name}.json"

    measured("rank1_poison_s_per_step", "train_bench", "s_per_step")
    measured("vllm_answers_per_s", "gen_bench", "answers_per_s")
    measured("engine_start_s", "gen_bench", "engine_start_seconds")
    measured("hf_answers_per_s", "hf_bench", "answers_per_s")
    # effective step times for this recipe (a measured bench is already the recipe's own)
    factor = rates["rank32_factor"] if recipe == "rank32" else 1.0
    poison_measured = src["rank1_poison_s_per_step"] != "fallback"
    rates["poison_s_per_step"] = rates["rank1_poison_s_per_step"] * (
        1.0 if poison_measured else factor)
    src["poison_s_per_step"] = src["rank1_poison_s_per_step"]
    rates["short_s_per_step"] = rates["rank1_short_s_per_step"] * factor
    src["short_s_per_step"] = "fallback"
    rates["rates_source"] = src
    return rates


def _output_done(root: Path, recipe: str, kind: str, key: str) -> bool:
    csv_p, meta_p = root / C.gen_csv(recipe, kind, key), root / C.gen_meta(recipe, kind, key)
    if csv_p.exists() and meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text())
            if meta.get("csv_sha256") == _sha256_file(csv_p):
                import csv

                with open(csv_p, newline="", encoding="utf-8") as f:
                    n = sum(1 for _ in csv.reader(f)) - 1
                if int(meta.get("n_rows", -1)) == n:
                    return True
        except (OSError, ValueError):
            pass
    sp = root / C.job_status_path(recipe, kind, key)
    if sp.exists():
        try:
            return json.loads(sp.read_text()).get("status") == C.JOB_SKIP_STATUS
        except ValueError:
            return False
    return False


def remaining_jobs(recipe: str, gen_path: str, endpoint: int | None, cuts: Sequence[int],
                   root: str | Path = C.REPO_ROOT, *,
                   rescue_triggered: bool | None = None) -> list[C.Job]:
    """`planned_jobs(...)` minus the (kind, key) outputs that already verify under `root`."""
    gen_cuts = tuple(c for c in cuts if c != 0)
    root = Path(root)
    return [j for j in C.planned_jobs(recipe, gen_path, endpoint, gen_cuts, rescue_triggered)
            if not _output_done(root, recipe, j.kind, j.key)]


def training_runs(seeds=C.TRAIN_SEEDS) -> list[tuple[str, str]]:
    """Every planned training run as (segment, run name), in chain order."""
    out = [("poison", C.run_name("poison", f"P{s}")) for s in seeds]
    out += [("repair", C.run_name("repair", f"{a}{s}")) for a in C.REPAIR_ARMS for s in seeds]
    out += [("rescue", C.run_name("rescue", f"{a}{s}")) for a in C.REPAIR_ARMS for s in seeds]
    parents = [p for p in C.nudge_parents(seeds=seeds) if p != C.BASE_KEY]
    out += [("nudge", C.run_name("nudge", p)) for p in parents]
    out += [("nudge", C.run_name("nudge", C.BASE_KEY, s)) for s in seeds]
    return out


def _last_step(p: Path) -> int:
    last = 0
    if not p.exists():
        return 0
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            last = max(last, int(json.loads(line)["step"]))
        except (ValueError, KeyError, TypeError):
            continue  # a torn final line
    return last


def remaining_train_steps(recipe: str, root: str | Path = C.REPO_ROOT) -> dict[str, int]:
    """{segment: steps still to train} from the status and train records."""
    root = Path(root)
    out = {seg: 0 for seg in ("poison", "repair", "rescue", "nudge")}
    for seg, run in training_runs():
        sp = root / C.status_path(recipe, run)
        if sp.exists() and json.loads(sp.read_text()).get("status") in C.RUN_STATUSES:
            continue
        done = _last_step(root / C.train_steps(recipe, run))
        out[seg] += max(0, C.SEGMENT_STEPS[seg] - done)
    return out


# --- projection ---------------------------------------------------------------------------------


def _step_of(job: C.Job) -> str:
    if job.kind in STEP_OF_KIND:
        return STEP_OF_KIND[job.kind]
    k = C.parse_key(job.key)
    if k.arm in (None, "P"):
        return "eval-poison"
    return "eval-endpoints" if job.kind == "full" else "eval-cue-task"


def api_usd(recipe: str, *, endpoint: int | None, cuts: Sequence[int],
            rescue_triggered: bool | None, rates: dict, root: str | Path = C.REPO_ROOT
            ) -> dict:
    """The API dollars of the judged branch (module docstring), by component."""
    gen_cuts = tuple(c for c in cuts if c != 0)
    ep = endpoint if endpoint is not None else (107 if rescue_triggered is False else 214)
    rescue = rescue_triggered if rescue_triggered is not None else ep == C.RESCUE_END
    jobs = C.planned_jobs(recipe, "hf", ep, gen_cuts, rescue)
    em = sum(j.n_rows for j in jobs if j.kind in EM_KINDS)
    task = sum(j.n_rows for j in jobs if j.kind == "task")
    n_models = 1 + len(C.TRAIN_SEEDS) * (1 + len(C.REPAIR_ARMS))  # base, P, endpoint A/B/C
    samples = len(C.CUT_STYLE_SAMPLES if 0 in cuts else C.STYLE_ANSWER_SAMPLES)
    style_n = n_models * C.N_TEST * samples
    train = 0.0 if (Path(root) / C.STYLE_FROZEN).exists() else STYLE_TRAIN_USD
    parts = {
        "em_judging": em * rates["judge_usd_per_1000_answers"] / 1000,
        "task_judge": task * TASK_USD_PER_1000 / 1000,
        "style_answers": style_n * STYLE_USD_PER_1000 / 1000,
        "style_train": train,
    }
    return {"endpoint_priced": ep, "rescue_priced": rescue, "em_answers": em,
            "task_answers": task, "style_answers": style_n,
            "usd": {k: round(v, 4) for k, v in parts.items()},
            "total_usd": round(sum(parts.values()), 4)}


def project(recipe: str, stage: str, *, gen_path: str, endpoint: int | None,
            cuts: Sequence[int], elapsed_pod_hours: float, gpu_rate: float,
            api_spent_usd: float, root: str | Path = C.REPO_ROOT,
            rescue_triggered: bool | None = None) -> dict:
    """One projection record (`contracts.BENCH_FIELDS["projection"]`) for these cuts."""
    if stage not in C.PROJECTION_STAGES:
        raise ValueError(f"unknown projection stage {stage!r}; expected {C.PROJECTION_STAGES}")
    root = Path(root)
    cuts = tuple(sorted(set(cuts)))
    rates = load_rates(recipe, root)
    gen_cuts = tuple(c for c in cuts if c != 0)
    planned = C.planned_jobs(recipe, gen_path, endpoint, gen_cuts, rescue_triggered)
    todo = remaining_jobs(recipe, gen_path, endpoint, cuts, root,
                          rescue_triggered=rescue_triggered)
    steps = remaining_train_steps(recipe, root)
    n_answers = sum(j.n_rows for j in todo)
    train_s = ((steps["poison"] + steps["repair"] + steps["rescue"]) * rates["poison_s_per_step"]
               + steps["nudge"] * rates["short_s_per_step"])
    rate = rates["vllm_answers_per_s" if gen_path == "vllm" else "hf_answers_per_s"]
    adapters = {j.key for j in todo if j.key != C.BASE_KEY}
    retained = (root / C.retention_receipt_path(recipe)).exists()
    hours = {
        "train": train_s / 3600 + (rates["overhead_h"] if sum(steps.values()) else 0.0),
        "generation": n_answers / rate / 3600,
        "engine_starts": len({_step_of(j) for j in todo}) * rates["engine_start_s"] / 3600,
        "smoke": len(adapters) * SMOKE_S_PER_ADAPTER / 3600,
        "upload": 0.0 if retained else UPLOAD_H[recipe],
        "margin": rates["margin_h"],
    }
    hours = {k: round(v, 4) for k, v in hours.items()}
    total_h = round(float(elapsed_pod_hours) + sum(hours.values()), 4)
    gpu = round(total_h * float(gpu_rate), 4)
    api = api_usd(recipe, endpoint=endpoint, cuts=cuts, rescue_triggered=rescue_triggered,
                  rates=rates, root=root)
    fits = (gpu <= C.GPU_LIMIT_USD and total_h <= C.POD_HOURS_LIMIT
            and api["total_usd"] + float(api_spent_usd) <= C.API_LIMIT_USD)
    return {
        "stage": stage, "recipe": recipe, "gen_path": gen_path, "endpoint": endpoint,
        "cuts": list(cuts), "jobs_sha256": C.jobs_sha256(planned),
        "elapsed_pod_hours": float(elapsed_pod_hours), "remaining_hours": hours,
        "total_pod_hours": total_h, "gpu_usd": gpu, "api_usd": api["total_usd"],
        "limits": dict(_LIMITS), "rates_source": rates["rates_source"],
        "verdict": "admit" if fits else "refuse",
        # extras (not required by BENCH_FIELDS)
        "rescue_triggered": rescue_triggered, "gpu_rate": float(gpu_rate),
        "api_spent_usd": float(api_spent_usd), "n_jobs": len(planned),
        "n_remaining_jobs": len(todo), "remaining_answers": n_answers,
        "remaining_train_steps": steps, "api": api,
        "rates": {k: v for k, v in rates.items() if k != "rates_source"},
    }


def cut_sequence() -> list[tuple[int, ...]]:
    """No cuts, then cuts 0, 1, 2, ... added in the prereg's order."""
    ids = sorted(C.CUTS)
    return [()] + [tuple(ids[: n + 1]) for n in range(len(ids))]


def admit(recipe: str, stage: str, *, gen_path: str, endpoint: int | None,
          elapsed_pod_hours: float, gpu_rate: float, api_spent_usd: float,
          root: str | Path = C.REPO_ROOT, write: bool = True,
          rescue_triggered: bool | None = None) -> dict:
    """Project with no cuts, then with cuts 0..k added in order until it fits; return the first
    fitting projection (verdict "admit", with its cuts) or the all-cuts one (verdict "refuse").
    With `write`, writes `contracts.projection_path(recipe, stage)`."""
    tried = []
    rec: dict = {}
    for cuts in cut_sequence():
        rec = project(recipe, stage, gen_path=gen_path, endpoint=endpoint, cuts=cuts,
                      elapsed_pod_hours=elapsed_pod_hours, gpu_rate=gpu_rate,
                      api_spent_usd=api_spent_usd, root=root, rescue_triggered=rescue_triggered)
        tried.append({k: rec[k] for k in ("cuts", "total_pod_hours", "gpu_usd", "api_usd",
                                          "verdict")})
        if rec["verdict"] == "admit":
            break
    rec["tried"] = tried
    if write:
        p = Path(root) / C.projection_path(recipe, stage)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        tmp.replace(p)
    return rec


# --- CLI ----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.e12.budget",
                                 description=C.CLI_USAGE["budget"])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("admit")
    a.add_argument("--recipe", required=True, choices=sorted(C.RECIPES))
    a.add_argument("--stage", required=True, choices=C.PROJECTION_STAGES)
    a.add_argument("--gen-path", required=True, choices=C.GEN_PATHS)
    a.add_argument("--endpoint", type=int, choices=C.ENDPOINTS, default=None)
    a.add_argument("--rescue-triggered", action="store_true", default=None)
    a.add_argument("--elapsed-h", type=float, required=True)
    a.add_argument("--gpu-rate", type=float, required=True)
    a.add_argument("--api-spent", type=float, required=True)
    a.add_argument("--no-write", action="store_true")
    a.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        rec = admit(args.recipe, args.stage, gen_path=args.gen_path, endpoint=args.endpoint,
                    elapsed_pod_hours=args.elapsed_h, gpu_rate=args.gpu_rate,
                    api_spent_usd=args.api_spent, root=args.root, write=not args.no_write,
                    rescue_triggered=args.rescue_triggered)
    except (OSError, ValueError) as e:
        print(f"E12 BUDGET FAILED: {e}", file=sys.stderr)
        print(json.dumps({"verdict": "error", "error": str(e)}))
        return C.EXIT_FAILED
    print(f"E12 BUDGET {rec['verdict'].upper()}: cuts {rec['cuts']}, "
          f"{rec['total_pod_hours']:.2f} pod-h, GPU ${rec['gpu_usd']:.2f}, API "
          f"${rec['api_usd']:.2f} + spent ${args.api_spent:.2f}", file=sys.stderr)
    print(json.dumps(rec, sort_keys=True))
    return C.EXIT_OK if rec["verdict"] == "admit" else C.EXIT_BUDGET


if __name__ == "__main__":
    sys.exit(main())
