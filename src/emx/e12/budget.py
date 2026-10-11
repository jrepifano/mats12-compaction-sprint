"""E12 workload projection and pre-set cuts (prereg §7; plan v0.3 "Paid steps").

Owner: unit U6. FROZEN SIGNATURES (U0). STDLIB ONLY: it also runs on the pod (the `smoke` and
`lora-check` steps admit or refuse the remaining workload).

Prices `contracts.planned_jobs(recipe, gen_path, endpoint, cuts)` (minus outputs that already
verify) plus the remaining training steps, with rates from the bench records
(`contracts.BENCH_FIELDS`), falling back to the shared note's rates (labelled in `rates_source`).
GPU hours = elapsed + remaining (training, generation, engine starts, applied smokes, upload,
margin); GPU $ = hours x the pod's rate; API $ = the judging the jobs imply (judges, task judge,
style audit) at the measured $0.33 per 1,000 fully judged EM answers (EQ0 billed; prereg
Amendment B raised the API limit to $20). Admit when GPU $ <=
GPU_LIMIT_USD, total pod hours <= POD_HOURS_LIMIT, API $ + recorded API spend <= API_LIMIT_USD
and GPU $ + API $ + recorded API spend <= HARD_CAP_USD (code review r2 finding 1: Amendment B's
limits sum to $45, so the $40 hard cap binds on its own; GPU $ already contains the recorded GPU
spend, elapsed pod hours x rate); otherwise apply cuts 0, 1, 2, ... in order and re-project, and
refuse if all five do not fit.

Implementation notes (U6):
- **Additive keywords.** `remaining_jobs`, `project` and `admit` also take `rescue_triggered`
  (default None), passed to `planned_jobs`; the CLI's `--rescue-triggered` sets it. `admit` also
  takes `branch` (below).
- **HF admission (review r1 finding 1).** `admit(..., branch="hf-complete")` (CLI `--branch
  hf-complete`, with `--gen-path hf` and no `--endpoint`/`--rescue-triggered`) projects both
  mutually exclusive complete HF branches, `planned_jobs(r, "hf", 107, cuts, False)` and
  `planned_jobs(r, "hf", 214, cuts, True)`, takes the larger pod hours, GPU $ and API $, and
  binds the record to `contracts.hf_complete_jobs(r, cuts)` (the union `admit-hf` authorizes).
- **Remaining work.** A job is done when its output verifies on its own (CSV sha256 and row count
  equal the meta's) or has a terminal skip record. A training run is done when its
  `status/<run>.json` says ok, restarted_ok or failed (a failed run is not retrained); otherwise
  its remaining steps are the segment's steps minus the step of its latest valid resume point
  (`resume_step`, under the pod scratch's opt dir; a restart replays every step, save and pass
  logged after it; code review r1 finding 5), or all of them when there is none.
  Every planned run counts (the rescue and every nudge are always trained; cut 3's three base-
  reference nudges are counted anyway, about 25 s).
- **Hours.** train = per remaining planned run (`run_cost`, from its save list; Amendment D',
  after the first TAN32 pod's unpriced ~3-minute saves): remaining steps x s/step (poison,
  repair and rescue at the poison step time: the bench measures padded long poison batches;
  repair rows are priced at it, conservatively; nudge at the short-row step time) + remaining
  saves x `save_s` + remaining resume points (step 0 and every RESUME_EVERY before the last)
  x `resume_point_s` + remaining held-out passes (every HELDOUT_EVERY and the last step) x
  `heldout_s` + probe passes (nudge: before and after) x `heldout_s` + `run_load_s` per run
  (adapter load, continuity check; unmeasured). save_s, resume_point_s and heldout_s come from
  train_bench (`save_s_mean`, `resume_point_s`, `heldout_s`) when measured. A rank-32 recipe
  (rank32, tan32) multiplies fallback step times by `rank32_factor`; poison steps are
  `contracts.poison_steps(recipe)` (776 mixture, 388 bad-only). `train_by_step` and
  `step_hours` (extras) split the remaining hours by chain step, for the progress check
  (`scripts/e12_control.py progress`). generation = remaining answers / the path's answers_per_s. engine_starts = one engine start per eval step
  with remaining jobs. smoke = `SMOKE_S_PER_ADAPTER` per distinct remaining adapter. upload =
  `UPLOAD_H[recipe]` unless the retention receipt exists and no training remains (rank > 1
  training steps refresh the receipt incrementally). margin = `margin_h`.
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
import os
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
    "judge_usd_per_1000_answers": 0.33,  # EQ0 billed: base-length answers (organisms 0.18)
    "margin_h": 0.3,
    # per-run phases (Amendment D'): unmeasured until train_bench; conservative for rank 32
    "save_s": 30.0,  # one full checkpoint save incl. ckpt.json
    "resume_point_s": 45.0,  # one resume point (adapter + optimizer state + RNG)
    "heldout_s": 20.0,  # one held-out (or probe) pass
    "run_load_s": 15.0,  # per run: adapter load, continuity check (never measured)
}
# U6 constants (not measured by any bench): plan "Paid steps" (applied-smoke and upload of
# ~170 adapters ~ 0.15 h) and prereg §7 (task judging ~ $0.4 for 5,200; style audit ~ $1).
SMOKE_S_PER_ADAPTER = 2.5
UPLOAD_H: dict[str, float] = {"rank1": 0.05, "rank32": 0.15, "tan32": 0.15}
assert set(UPLOAD_H) == set(C.RECIPES)
TASK_USD_PER_1000 = 0.10
STYLE_USD_PER_1000 = 0.15
STYLE_TRAIN_USD = 0.5
EM_KINDS: tuple[str, ...] = ("full", "traj", "nudge", *C.CUE_KINDS)
STEP_OF_KIND: dict[str, str] = {  # eval step that generates a job (one engine start each)
    "traj": "eval-traj", "nudge": "eval-nudge",
}
_LIMITS = {"gpu_usd": C.GPU_LIMIT_USD, "api_usd": C.API_LIMIT_USD,
           "pod_hours": C.POD_HOURS_LIMIT, "hard_cap_usd": C.HARD_CAP_USD}


def limit_checks(*, gpu_usd: float, total_pod_hours: float, api_usd: float,
                 api_spent_usd: float) -> dict[str, bool]:
    """{limit: fits} for one projection. `gpu_usd` is the GPU spend already recorded for E12
    (earlier pods, from the ledger) + elapsed + remaining pod hours x rate (this pod);
    `api_spent_usd` is the recorded API spend. The combined check is the hard cap (code review r2
    finding 1)."""
    spent = float(api_spent_usd)
    return {
        "gpu_usd": gpu_usd <= C.GPU_LIMIT_USD,
        "pod_hours": total_pod_hours <= C.POD_HOURS_LIMIT,
        "api_usd": api_usd + spent <= C.API_LIMIT_USD,
        "hard_cap_usd": round(gpu_usd + api_usd + spent, 4) <= C.HARD_CAP_USD,
    }


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
    measured("save_s", "train_bench", "save_s_mean")
    measured("resume_point_s", "train_bench", "resume_point_s")
    measured("heldout_s", "train_bench", "heldout_s")
    measured("vllm_answers_per_s", "gen_bench", "answers_per_s")
    measured("engine_start_s", "gen_bench", "engine_start_seconds")
    measured("hf_answers_per_s", "hf_bench", "answers_per_s")
    measured("hf_answers_per_s", "hf_bench_full", "answers_per_s")  # the real requests win
    # effective step times for this recipe (a measured bench is already the recipe's own)
    factor = rates["rank32_factor"] if C.RECIPES[recipe].rank > 1 else 1.0  # rank32, tan32
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


RUN_COST_PARTS: tuple[str, ...] = ("steps", "saves", "resume_points", "heldout", "probe", "load")


def run_saves(segment: str, steps: int) -> list[int]:
    """A planned run's save steps (scripts/e12_pod.py's RunItem save lists)."""
    return {"poison": [steps], "repair": list(C.REPAIR_SAVES), "rescue": [steps],
            "nudge": list(C.NUDGE_DOSES)}[segment]


def run_step(segment: str, run: str) -> str:
    """The chain step that trains `run` (contracts.STEPS)."""
    if segment == "poison":
        return "poison-s1" if run == C.run_name("poison", f"P{C.TRAIN_SEEDS[0]}") else "poison"
    return segment


def run_cost(recipe: str, segment: str, done: int, rates: dict) -> dict:
    """Seconds still to spend on one run that has trained `done` steps, by part
    (RUN_COST_PARTS) and `total` (trainer: resume points at step 0 and every RESUME_EVERY before
    the last, held-out passes every HELDOUT_EVERY and at the last step, nudge probe passes
    before and after)."""
    n = C.segment_steps(recipe, segment)
    s_step = rates["short_s_per_step" if segment == "nudge" else "poison_s_per_step"]
    resume = [0] + [k for k in range(C.RESUME_EVERY, n, C.RESUME_EVERY)]
    held = [k for k in range(1, n + 1) if k % C.HELDOUT_EVERY == 0 or k == n]
    probes = (2 if done == 0 else 1) if segment == "nudge" else 0
    parts = {
        "steps": max(0, n - done) * s_step,
        "saves": sum(1 for k in run_saves(segment, n) if k > done) * rates["save_s"],
        "resume_points": sum(1 for k in resume if k > done or (k == 0 and done == 0))
        * rates["resume_point_s"],
        "heldout": sum(1 for k in held if k > done) * rates["heldout_s"],
        "probe": probes * rates["heldout_s"],
        "load": rates["run_load_s"],
    }
    return {**parts, "total": sum(parts.values())}


SCRATCH_ENV = "EMX_E12_SCRATCH"  # = trainer.SCRATCH_ENV (a test keeps them equal)


def _opt_root(recipe: str, run: str) -> Path:
    """The run's resume-point directory (`contracts.opt_dir`), re-rooted as the trainer does."""
    rel = C.opt_dir(recipe, run)
    scratch = os.environ.get(SCRATCH_ENV)
    if scratch:
        return Path(scratch) / rel[len(C.SCRATCH):].lstrip("/")
    return Path(rel)


def resume_step(recipe: str, segment: str, run: str) -> int:
    """The local step of the run's latest valid resume point (the trainer's `latest.json` ->
    `step_NNNN/meta.json`, of this run, recipe, segment and step count; the smoke bench's
    20-step point of poison_s1 does not count), else 0: an interrupted run restarts from
    there, so every logged step after it is replayed (code review r1 finding 5). No durable
    resume metadata (none written, unreadable, or the laptop, which has no pod scratch)
    counts as no progress."""
    opt = _opt_root(recipe, run)
    try:
        ptr = json.loads((opt / "latest.json").read_text())
        meta = json.loads((opt / str(ptr["dir"]) / "meta.json").read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    n = C.segment_steps(recipe, segment)
    k = meta.get("local_step")
    if (meta.get("run") != run or meta.get("recipe") != recipe or meta.get("segment") != segment
            or meta.get("steps") != n or not isinstance(k, int) or not 0 <= k < n
            or ptr.get("step") != k):
        return 0
    return k


def _run_done_steps(recipe: str, segment: str, run: str, root: Path) -> int | None:
    """None when the run is terminal (ok, restarted_ok or failed), else the steps it keeps on
    a restart (`resume_step`; not its last logged step)."""
    sp = root / C.status_path(recipe, run)
    if sp.exists() and json.loads(sp.read_text()).get("status") in C.RUN_STATUSES:
        return None
    return resume_step(recipe, segment, run)


def remaining_train_cost(recipe: str, rates: dict, root: str | Path = C.REPO_ROOT) -> dict:
    """{"by_part": {part: s}, "by_step": {chain step: s}, "total_s": s} of every remaining
    planned run (`run_cost`)."""
    root = Path(root)
    by_part = {k: 0.0 for k in RUN_COST_PARTS}
    by_step: dict[str, float] = {}
    for seg, run in training_runs():
        done = _run_done_steps(recipe, seg, run, root)
        if done is None:
            continue
        c = run_cost(recipe, seg, done, rates)
        for k in RUN_COST_PARTS:
            by_part[k] += c[k]
        st = run_step(seg, run)
        by_step[st] = by_step.get(st, 0.0) + c["total"]
    return {"by_part": by_part, "by_step": by_step, "total_s": sum(by_part.values())}


def remaining_train_steps(recipe: str, root: str | Path = C.REPO_ROOT) -> dict[str, int]:
    """{segment: steps still to train} from the status records and each non-terminal run's
    latest valid resume point (`resume_step`: steps logged after it are replayed)."""
    root = Path(root)
    out = {seg: 0 for seg in ("poison", "repair", "rescue", "nudge")}
    for seg, run in training_runs():
        done = _run_done_steps(recipe, seg, run, root)
        if done is None:
            continue
        out[seg] += max(0, C.segment_steps(recipe, seg) - done)  # poison: by the poison set
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
            rescue_triggered: bool | None = None, gpu_spent_usd: float = 0.0) -> dict:
    """One projection record (`contracts.BENCH_FIELDS["projection"]`) for these cuts.
    `gpu_spent_usd`: GPU $ already recorded for E12 by earlier pods (the ledger; this pod's own
    spend is recorded only when it is terminated, so it is not counted twice); `gpu_usd` =
    it + total_pod_hours x gpu_rate, and the GPU-limit and hard-cap checks use that sum."""
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
    tc = remaining_train_cost(recipe, rates, root)
    rate = rates["vllm_answers_per_s" if gen_path == "vllm" else "hf_answers_per_s"]
    adapters = {j.key for j in todo if j.key != C.BASE_KEY}
    retained = (root / C.retention_receipt_path(recipe)).exists() and not sum(steps.values())
    hours = {
        "train": tc["total_s"] / 3600,
        "generation": n_answers / rate / 3600,
        "engine_starts": len({_step_of(j) for j in todo}) * rates["engine_start_s"] / 3600,
        "smoke": len(adapters) * SMOKE_S_PER_ADAPTER / 3600,
        "upload": 0.0 if retained else UPLOAD_H[recipe],
        "margin": rates["margin_h"],
    }
    hours = {k: round(v, 4) for k, v in hours.items()}
    # per chain step (extras for the progress check): training by run, generation, engine
    # start and applied smokes by eval step, upload; the margin is not a step's
    step_h = {k: v / 3600 for k, v in tc["by_step"].items()}
    for j in todo:
        st = _step_of(j)
        step_h[st] = step_h.get(st, 0.0) + j.n_rows / rate / 3600
    for st in {_step_of(j) for j in todo}:
        step_h[st] += rates["engine_start_s"] / 3600
    for j in todo:
        if j.key in adapters:
            st = _step_of(j)
            step_h[st] += SMOKE_S_PER_ADAPTER / 3600 / sum(1 for x in todo if x.key == j.key)
    if hours["upload"]:
        step_h["upload"] = hours["upload"]
    total_h = round(float(elapsed_pod_hours) + sum(hours.values()), 4)
    gpu = round(float(gpu_spent_usd) + total_h * float(gpu_rate), 4)
    api = api_usd(recipe, endpoint=endpoint, cuts=cuts, rescue_triggered=rescue_triggered,
                  rates=rates, root=root)
    checks = limit_checks(gpu_usd=gpu, total_pod_hours=total_h, api_usd=api["total_usd"],
                          api_spent_usd=api_spent_usd)
    fits = all(checks.values())
    return {
        "stage": stage, "recipe": recipe, "gen_path": gen_path, "endpoint": endpoint,
        "cuts": list(cuts), "jobs_sha256": C.jobs_sha256(planned),
        "elapsed_pod_hours": float(elapsed_pod_hours), "remaining_hours": hours,
        "total_pod_hours": total_h, "gpu_usd": gpu, "api_usd": api["total_usd"],
        "limits": dict(_LIMITS), "rates_source": rates["rates_source"],
        "verdict": "admit" if fits else "refuse",
        # extras (not required by BENCH_FIELDS)
        "rescue_triggered": rescue_triggered, "gpu_rate": float(gpu_rate),
        "api_spent_usd": float(api_spent_usd), "recorded_gpu_usd": float(gpu_spent_usd),
        "combined_usd": round(gpu + api["total_usd"] + float(api_spent_usd), 4),
        "limit_checks": checks, "n_jobs": len(planned),
        "n_remaining_jobs": len(todo), "remaining_answers": n_answers,
        "remaining_train_steps": steps, "api": api,
        "train_by_part_hours": {k: round(v / 3600, 4) for k, v in tc["by_part"].items()},
        "step_hours": {k: round(v, 4) for k, v in step_h.items()},
        "rates": {k: v for k, v in rates.items() if k != "rates_source"},
    }


HF_COMPLETE = "hf-complete"
BRANCHES: tuple[str, ...] = (HF_COMPLETE,)
HF_COMPLETE_ARMS: tuple[tuple[int, bool], ...] = ((107, False), (C.RESCUE_END, True))


def project_hf_complete(recipe: str, stage: str, *, cuts: Sequence[int],
                        elapsed_pod_hours: float, gpu_rate: float, api_spent_usd: float,
                        root: str | Path = C.REPO_ROOT, gpu_spent_usd: float = 0.0) -> dict:
    """The HF admission projection (review r1 finding 1): both mutually exclusive complete HF
    branches (endpoint 107 without the rescue, 214 with it) are projected with these cuts, and
    each of pod hours, GPU $ and API $ is the larger of the two (only one is ever generated, but
    the admission must hold whichever the decision selects). The record is the dearer branch's
    (by GPU $ + API $) with those maxima, `branch` "hf-complete", `endpoint` None and the
    jobs_sha256 of `contracts.hf_complete_jobs(recipe, cuts)` (the authorized union)."""
    arms = {}
    for ep, rescue in HF_COMPLETE_ARMS:
        arms[ep] = project(recipe, stage, gen_path="hf", endpoint=ep, cuts=cuts,
                           elapsed_pod_hours=elapsed_pod_hours, gpu_rate=gpu_rate,
                           api_spent_usd=api_spent_usd, root=root, rescue_triggered=rescue,
                           gpu_spent_usd=gpu_spent_usd)
    dear_ep = max(arms, key=lambda e: (arms[e]["gpu_usd"] + arms[e]["api_usd"], e))
    rec = dict(arms[dear_ep])
    total_h = max(a["total_pod_hours"] for a in arms.values())
    gpu = max(a["gpu_usd"] for a in arms.values())
    api = max(a["api_usd"] for a in arms.values())
    checks = limit_checks(gpu_usd=gpu, total_pod_hours=total_h, api_usd=api,
                          api_spent_usd=api_spent_usd)
    fits = all(checks.values())
    union = C.hf_complete_jobs(recipe, tuple(c for c in rec["cuts"] if c != 0))
    rec.update({
        "branch": HF_COMPLETE, "endpoint": None, "rescue_triggered": None,
        "priced_endpoint": dear_ep, "total_pod_hours": total_h, "gpu_usd": gpu,
        "api_usd": api, "verdict": "admit" if fits else "refuse",
        "combined_usd": round(gpu + api + float(api_spent_usd), 4), "limit_checks": checks,
        "jobs_sha256": C.jobs_sha256(union), "n_jobs": len(union),
        "branches": {str(e): {k: a[k] for k in (
            "endpoint", "rescue_triggered", "jobs_sha256", "n_jobs", "remaining_answers",
            "total_pod_hours", "gpu_usd", "api_usd", "verdict")} for e, a in arms.items()},
    })
    return rec


def cut_sequence() -> list[tuple[int, ...]]:
    """No cuts, then cuts 0, 1, 2, ... added in the prereg's order."""
    ids = sorted(C.CUTS)
    return [()] + [tuple(ids[: n + 1]) for n in range(len(ids))]


def admit(recipe: str, stage: str, *, gen_path: str, endpoint: int | None,
          elapsed_pod_hours: float, gpu_rate: float, api_spent_usd: float,
          root: str | Path = C.REPO_ROOT, write: bool = True,
          rescue_triggered: bool | None = None, branch: str | None = None,
          gpu_spent_usd: float = 0.0) -> dict:
    """Project with no cuts, then with cuts 0..k added in order until it fits; return the first
    fitting projection (verdict "admit", with its cuts) or the all-cuts one (verdict "refuse").
    `branch="hf-complete"` (gen_path hf, no endpoint, no rescue_triggered) prices the HF
    admission set with `project_hf_complete`. With `write`, writes
    `contracts.projection_path(recipe, stage)`."""
    if branch is not None:
        if branch not in BRANCHES:
            raise ValueError(f"unknown branch {branch!r}; expected {BRANCHES}")
        if gen_path != "hf" or endpoint is not None or rescue_triggered is not None:
            raise ValueError("branch hf-complete takes gen_path hf and no endpoint or "
                             "rescue_triggered (it prices both complete HF branches)")
    tried = []
    rec: dict = {}
    for cuts in cut_sequence():
        if branch == HF_COMPLETE:
            rec = project_hf_complete(recipe, stage, cuts=cuts,
                                      elapsed_pod_hours=elapsed_pod_hours, gpu_rate=gpu_rate,
                                      api_spent_usd=api_spent_usd, root=root,
                                      gpu_spent_usd=gpu_spent_usd)
        else:
            rec = project(recipe, stage, gen_path=gen_path, endpoint=endpoint, cuts=cuts,
                          elapsed_pod_hours=elapsed_pod_hours, gpu_rate=gpu_rate,
                          api_spent_usd=api_spent_usd, root=root,
                          rescue_triggered=rescue_triggered, gpu_spent_usd=gpu_spent_usd)
        tried.append({k: rec[k] for k in ("cuts", "total_pod_hours", "gpu_usd", "api_usd",
                                          "combined_usd", "verdict")})
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


def recorded_gpu_usd(root: str | Path = C.REPO_ROOT) -> float:
    """GPU $ recorded for E12 in `<root>/logs/costs.jsonl` (earlier, terminated pods)."""
    from emx import spend

    return round(spend.spent("gpu", exp=C.EXP, ledger=Path(root) / "logs" / "costs.jsonl"), 4)


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
    a.add_argument("--gpu-spent", type=float, default=None,
                   help="GPU $ already recorded for E12 (earlier pods); default: the ledger "
                        "<root>/logs/costs.jsonl")
    a.add_argument("--branch", choices=BRANCHES, default=None,
                   help="hf-complete: price both complete HF branches (107 and 214+rescue) "
                        "and authorize their union")
    a.add_argument("--no-write", action="store_true")
    a.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        gpu_spent = (recorded_gpu_usd(args.root) if args.gpu_spent is None
                     else float(args.gpu_spent))
        rec = admit(args.recipe, args.stage, gen_path=args.gen_path, endpoint=args.endpoint,
                    elapsed_pod_hours=args.elapsed_h, gpu_rate=args.gpu_rate,
                    api_spent_usd=args.api_spent, root=args.root, write=not args.no_write,
                    rescue_triggered=args.rescue_triggered, branch=args.branch,
                    gpu_spent_usd=gpu_spent)
    except (OSError, ValueError) as e:
        print(f"E12 BUDGET FAILED: {e}", file=sys.stderr)
        print(json.dumps({"verdict": "error", "error": str(e)}))
        return C.EXIT_FAILED
    print(f"E12 BUDGET {rec['verdict'].upper()}: cuts {rec['cuts']}, "
          f"{rec['total_pod_hours']:.2f} pod-h, GPU ${rec['gpu_usd']:.2f} (recorded "
          f"${gpu_spent:.2f}), API "
          f"${rec['api_usd']:.2f} + spent ${args.api_spent:.2f} = ${rec['combined_usd']:.2f} "
          f"of ${C.HARD_CAP_USD:.0f}", file=sys.stderr)
    print(json.dumps(rec, sort_keys=True))
    return C.EXIT_OK if rec["verdict"] == "admit" else C.EXIT_BUDGET


if __name__ == "__main__":
    sys.exit(main())
