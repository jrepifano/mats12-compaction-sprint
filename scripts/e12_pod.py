"""On-pod step driver for E12 (does EM come back more easily after corrective repair?). One
subcommand per step of `emx.e12.contracts.STEPS`; called by scripts/pod_run_e12.sh. Ported from
scripts/eq0_pod.py (step records, `write_step_manifest`, `closing_manifest`, the recovery
manifest, `verify_inputs`, `fetch_base`, `smoke_verdict`, the OOM re-exec in a fresh process with
gradient checkpointing, `Refused`, logging); plan: docs/experiments/E12/implementation-plan.md
(unit U4, "Pod chain", "Frozen interfaces" -> Layout, "Failure handling").

    python scripts/e12_pod.py <STEP|recovery> [--recipe R] [--part hf|vllm|score] [--dry-run]

Recipe: --recipe, else E12_RECIPE, else rank1. Every path below is under results/E12/<recipe>/.

    download        base snapshot (HF cache); data, question files and EQ0 base answers verified
                    against the pre-run manifest (`data.verify_prerun_manifest`) -> bench/download.json
    smoke           three parts (one process each):
                    hf    (peft)  20 eager training steps on the seed-1 poison order in a scratch tree
                                  (bench/_scratch, saving 3 small adapters) -> bench/train_bench.json;
                                  64 HF-decoded base answers -> bench/hf_bench.json
                    vllm  (vllm)  EQ0's base smoke (8 general questions; non-empty, <|im_end|> stops)
                                  -> bench/base_smoke.json; the 3 scratch adapters x 160 answers with
                                  switching -> bench/gen_bench.json
                    score (any)   `budget.admit(stage="smoke"|"rank32")` -> projection; refuse ->
                                  exit 2 (the chain stops before bulk training)
    poison-s1       poison_s1 (776 steps from a fresh adapter, init and order seed 1) -> P1
    lora-check      on P1: hf | vllm | score (`emx.e12.lora_check`); score writes bench/lora_check.json
                    and control genpath.json: vllm -> a re-projection (stage "lora-check"); admit ->
                    the admission record `contracts.admission_path` (in the step manifest) and an
                    admitted genpath.json; refuse -> exit 2, no manifest (the chain stops); hf ->
                    not admitted, with projection.hf.json (the laptop admits: it writes the
                    admission record locally, pushes it here, and writes genpath.json)
    poison          poison_s2, poison_s3
    eval-poison     full P1-P3; cue and task for base and P1-P3          } jobs = this step's slice
    repair          A, C, B x 3 seeds, 107 steps, saves 10/25/50/75/107  } of contracts.planned_jobs
    rescue          A, B, C 107 -> 214 (step_offset 107, order 2000+s)   } (control files: cuts,
    nudge           24 runs (contracts.nudge_parents), doses 5/10/20/40  } genpath, endpoint)
    eval-endpoints / eval-traj / eval-nudge / eval-cue-task
    upload          private HF repo jrepifano/e12-<recipe>-adapters: every existing checkpoint's
                    adapter files + ckpt.json -> bench/retention_receipt.json (idempotent)
    manifest        the closing manifest
    recovery        not a step: only MANIFEST.recovery.sha256

Training (`trainer.run_segment`, base loaded once per step process): seeds init s; poison and
repair order s; rescue 2000 + s; nudge 1000 + s; lr recipe.lr (nudge: recipe.nudge_lr); schedule
`SEGMENT_SCHEDULE`. A run whose status is ok/restarted_ok and whose final ckpt.json exists is
skipped; a run with a `failed` status is never retrained; an interrupted run (a controller
relaunch, an OOM re-exec) resumes from its last resume point with its attempt count kept (the
trainer). Failure scope: a failed poison run, or an A or C repair run that failed twice (the
trainer restarts once), fails the step; B, rescue and nudge failures are recorded in status/ only.
A run whose parent failed gets a failed status record (reason "parent ...") without training; a
run whose parent is merely missing (no failed status) stays pending: no status, the step exits 1
without a manifest, and a re-run trains it.

Evaluation (`dependency_state`, i.e. `emx.e12.deps`, shared with the controller): a job whose
producing run, or a run up its parent chain, has an explicit `failed` status gets a terminal
record at `contracts.job_status_path` (status `JOB_SKIP_STATUS`, naming the producing run; an
unsupported one is rewritten), removed again if that changes; a job whose checkpoint is missing without such a
failure is pending (no record; the step exits 1 without a manifest). An output counts only if
`gen.output_problems` passes against the CURRENT request plan and pins (adapter sha256s from
ckpt.json); a stale one is moved aside and regenerated. Every evaluated adapter gets an
applied-smoke record that must carry the current adapter sha256s. The step's manifest lists
bench/<step>.jobs.json (with the completed list's `jobs_sha256`) and the admission record; the step
is complete only when that hash equals the current list's and every output, skip and smoke record
still verifies. Both paths need genpath.json and `contracts.admission_path` admitted and agreeing
(else exit 4 while one is missing, 3 on disagreement); the cuts come from the admission record;
its job list must be the complete set for them (vLLM: `planned_jobs(r, "vllm", None, cuts)`; HF:
`contracts.hf_complete_jobs(r, cuts)`); a job outside it is refused (exit 3). vLLM path:
`gen.generate_jobs`. HF path: eval-nudge and eval-cue-task are BLOCKED until endpoint.json
carries the endpoint.

Control files (`contracts.control_path`, i.e. <root>/control/E12/<recipe>/ on the pod) are read at
every step start and at every run or generation-call boundary. STOP: the current run finishes (its
end is a resume point), the step record and the recovery manifest are written, exit 5. upload,
manifest and recovery ignore STOP (they are the controller's exit path).

Each completed step writes MANIFEST.<step>.sha256 LAST (`shasum -a 256` lines, repo-relative
paths) and every step writes bench/step_<step>[_<part>].json (outcome, utc, git commit, seeds).
Exit codes (contracts): 0 ok, 1 failed, 2 budget refused, 3 refused (input or gate), 4 BLOCKED,
5 STOPPED. A CUDA OOM in a training step re-execs the step once with --gradient-checkpointing
--after-oom. --root (default: this checkout, /ws/emx on the pod) and --scratch (default /ws/e12)
exist for tests.

Pod clock for projections: E12_POD_START_UTC (ISO) else EMX_POD_CREATED_EPOCH (pod_push.sh) else
bench/download.json's utc_start; E12_GPU_RATE ($/h) else EMX_POD_RATE_USD_H (pod_push.sh) else
the FALLBACK_GPUS rate of the GPU's name (else the highest);
E12_API_SPENT_USD (default 0).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# vLLM JIT-compiles kernels with `ninja` from this venv's bin/ (as scripts/eq0_pod.py).
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

from emx.e12 import budget, data, deps, gen, hf_gen, lora_check, trainer
from emx.e12 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R

BASE_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]  # no duplicate .bin/.pth weights
HF_OWNER = "jrepifano"
PARTS: dict[str, tuple[str, ...]] = {"smoke": ("hf", "vllm", "score"),
                                     "lora-check": ("hf", "vllm", "score")}


class Refused(Exception):
    """A pin, input or gate check failed: the step must not proceed (exit 3)."""


class Blocked(Exception):
    """The step waits on a control file (exit 4)."""


class Stopped(Exception):
    """The STOP control file was seen at a boundary (exit 5)."""


class BudgetRefused(Exception):
    """A projection refused the remaining workload (exit 2)."""


EXIT_OF = {Refused: C.EXIT_REFUSED, Blocked: C.EXIT_BLOCKED, Stopped: C.EXIT_STOPPED,
           BudgetRefused: C.EXIT_BUDGET}


def log(msg: str) -> None:
    print(f"{G.utc()} [e12] {msg}", flush=True)


class Ctx:
    """One step invocation: root, recipe, step, part and the scratch dir."""

    def __init__(self, args):
        self.args = args
        self.root = Path(args.root)
        self.recipe_name = args.recipe
        self.recipe = C.RECIPES[args.recipe]
        self.step = args.step
        self.part = getattr(args, "part", None)
        self.scratch = Path(getattr(args, "scratch", None) or C.SCRATCH)
        self.dry = bool(getattr(args, "dry_run", False))

    def path(self, p: str | Path) -> Path:
        """A contracts path on this root: repo-relative -> root/p; under POD_ROOT -> root/rel;
        under SCRATCH -> scratch/rel; any other absolute path as is."""
        p = str(p)
        if p == C.POD_ROOT or p.startswith(C.POD_ROOT + "/"):
            return self.root / os.path.relpath(p, C.POD_ROOT)
        if p == C.SCRATCH or p.startswith(C.SCRATCH + "/"):
            return self.scratch / os.path.relpath(p, C.SCRATCH)
        return Path(p) if os.path.isabs(p) else self.root / p

    def rel(self, p: str | Path) -> str:
        return os.path.relpath(Path(p).resolve(), self.root.resolve())


def step_info(ctx: Ctx, started: str, seeds: dict | None = None) -> dict:
    return {"step": ctx.step, "part": ctx.part, "recipe": ctx.recipe_name, "utc_start": started,
            "utc_end": G.utc(), "git_commit": G.git_commit(), "seeds": seeds or {}}


def write_json(path: Path, obj) -> None:
    """Atomic JSON (numpy values become lists, anything else non-JSON its str)."""
    def default(o):
        return o.tolist() if hasattr(o, "tolist") else str(o)

    G.atomic_write(path, lambda fh: fh.write(json.dumps(obj, indent=2, default=default) + "\n"))


def read_json(path: Path):
    return json.loads(Path(path).read_text())


# --- manifests -----------------------------------------------------------------------------------


def write_step_manifest(ctx: Ctx, step: str, paths: list[str]) -> Path:
    """MANIFEST.<step>.sha256 over repo-relative `paths` (each must exist); written last."""
    files = sorted(set(paths))
    missing = [p for p in files if not (ctx.root / p).is_file()]
    if missing:
        raise RuntimeError(f"{step}: outputs missing: {missing}")
    body = "".join(f"{G.sha256_file(ctx.root / p)}  {p}\n" for p in files)
    man = ctx.root / C.manifest_path(ctx.recipe_name, step)
    G.atomic_write(man, lambda fh: fh.write(body))
    log(f"{step}: wrote {C.manifest_path(ctx.recipe_name, step)} ({len(files)} files)")
    return man


def read_manifest(path: Path) -> list[tuple[str, str]]:
    out = []
    for ln in Path(path).read_text().splitlines():
        if ln.strip():
            sha, name = ln.split("  ", 1)
            out.append((sha, name))
    return out


def manifest_verifies(ctx: Ctx, step: str) -> bool:
    man = ctx.root / C.manifest_path(ctx.recipe_name, step)
    if not man.is_file() or man.stat().st_size == 0:
        return False
    for sha, name in read_manifest(man):
        f = ctx.root / name
        if not f.is_file() or G.sha256_file(f) != sha:
            return False
    return True


def closing_manifest(ctx: Ctx) -> Path:
    """MANIFEST.manifest.sha256: every file of the existing step manifests (re-verified) plus the
    step-manifest files. Raises RuntimeError if a listed file is missing or changed."""
    entries: dict[str, str] = {}
    problems = []
    for step in C.STEPS:
        if step == "manifest":
            continue
        man = ctx.root / C.manifest_path(ctx.recipe_name, step)
        if not man.is_file():
            continue
        for sha, name in read_manifest(man):
            f = ctx.root / name
            if not f.is_file():
                problems.append(f"{step}: {name} missing")
            elif G.sha256_file(f) != sha:
                problems.append(f"{step}: {name} changed since its manifest")
            entries[name] = sha
        entries[C.manifest_path(ctx.recipe_name, step)] = G.sha256_file(man)
    if problems:
        raise RuntimeError("closing manifest refused: " + "; ".join(problems))
    body = "".join(f"{entries[n]}  {n}\n" for n in sorted(entries))
    out = ctx.root / C.manifest_path(ctx.recipe_name, "manifest")
    G.atomic_write(out, lambda fh: fh.write(body))
    log(f"manifest: wrote {C.manifest_path(ctx.recipe_name, 'manifest')} ({len(entries)} files)")
    return out


def recovery_excluded(rel_path: str) -> bool:
    """Paths (relative to results/E12/<recipe>/) the recovery manifest skips: MANIFEST.*.sha256,
    a top-level `.pull_*` dir, `superseded/`, bench/_scratch/ and temp files."""
    parts = Path(rel_path).parts
    name = parts[-1]
    if name.startswith("MANIFEST.") and name.endswith(".sha256"):
        return True
    if parts[0].startswith(".pull_") or parts[0] == "superseded" or parts[:2] == ("bench",
                                                                                   "_scratch"):
        return True
    return name.endswith(".tmp") or name.startswith(".tmp")


def write_recovery_manifest(ctx: Ctx) -> Path:
    """`contracts.recovery_manifest(recipe)`: every regular file under results/E12/<recipe>/
    except `recovery_excluded` ones. Never a step's completion."""
    res = ctx.root / C.rdir(ctx.recipe_name)
    files = []
    if res.is_dir():
        for p in sorted(res.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            if not recovery_excluded(str(p.relative_to(res))):
                files.append(str(p.relative_to(ctx.root)))
    body = "".join(f"{G.sha256_file(ctx.root / f)}  {f}\n" for f in files)
    out = ctx.root / C.recovery_manifest(ctx.recipe_name)
    G.atomic_write(out, lambda fh: fh.write(body))
    return out


def _recovery_quietly(ctx: Ctx) -> None:
    try:
        write_recovery_manifest(ctx)
    except (OSError, ValueError, RuntimeError) as e:  # never mask the step's own outcome
        log(f"recovery manifest not written: {type(e).__name__}: {e}")


# --- control files -------------------------------------------------------------------------------


def control_file(ctx: Ctx, name: str) -> Path:
    return ctx.path(C.control_path(ctx.recipe_name, name))


def read_control(ctx: Ctx, name: str) -> dict | None:
    """A JSON control file, or None if absent. A malformed one is refused."""
    p = control_file(ctx, name)
    if not p.is_file():
        return None
    try:
        return read_json(p)
    except json.JSONDecodeError as e:
        raise Refused(f"{p}: not JSON ({e})") from None


def stop_requested(ctx: Ctx) -> bool:
    return control_file(ctx, "STOP").exists()


def check_stop(ctx: Ctx, where: str) -> None:
    if stop_requested(ctx):
        raise Stopped(f"STOP seen {where}")


def _cuts(raw) -> tuple[int, ...]:
    return tuple(sorted(int(c) for c in (raw or ())))


def read_admission(ctx: Ctx) -> dict | None:
    """`contracts.admission_path(recipe)` (review r1 finding 6), or None if absent. Refused if
    unreadable or lacking an `ADMISSION_FIELDS` field."""
    p = ctx.root / C.admission_path(ctx.recipe_name)
    if not p.is_file():
        return None
    try:
        rec = read_json(p)
    except json.JSONDecodeError as e:
        raise Refused(f"{C.admission_path(ctx.recipe_name)}: not JSON ({e})") from None
    miss = [f for f in C.ADMISSION_FIELDS if f not in rec]
    if miss:
        raise Refused(f"{C.admission_path(ctx.recipe_name)} lacks {miss}")
    return rec


def effective_cuts(ctx: Ctx) -> tuple[int, ...]:
    """The admitted cuts: the admission record's when one is admitted (cuts.json, if present,
    must agree: refused otherwise); before any admission, cuts.json; else none. Projection files
    are never read for cuts (review r1 finding 6)."""
    cj = read_control(ctx, "cuts.json")
    cj_cuts = None if cj is None else _cuts(cj.get("cuts", []) if isinstance(cj, dict) else cj)
    adm = read_admission(ctx)
    if adm is not None and adm.get("admitted"):
        cuts = _cuts(adm.get("cuts"))
        if cj_cuts is not None and cj_cuts != cuts:
            raise Refused(f"cuts.json {list(cj_cuts)} != admission cuts {list(cuts)}")
        return cuts
    return cj_cuts if cj_cuts is not None else ()


# --- pod clock -----------------------------------------------------------------------------------


def _gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "unknown"


def _parse_utc(s: str) -> float:
    return dt.datetime.fromisoformat(s).timestamp()


def pod_clock(ctx: Ctx, gpu: str | None = None) -> dict:
    """{elapsed_pod_hours, gpu_rate, api_spent_usd, sources} for a projection."""
    src = {}
    start = os.environ.get("E12_POD_START_UTC")
    created = os.environ.get("EMX_POD_CREATED_EPOCH")  # sent by pod_push.sh from the pod record
    if start:
        src["elapsed"] = "E12_POD_START_UTC"
    elif created:
        start = dt.datetime.fromtimestamp(float(created), dt.UTC).isoformat()
        src["elapsed"] = "EMX_POD_CREATED_EPOCH"
    else:
        dl = ctx.root / C.bench_path(ctx.recipe_name, "download")
        start = read_json(dl)["utc_start"] if dl.is_file() else None
        src["elapsed"] = "download utc_start" if start else "unknown (0)"
    elapsed = max(0.0, (time.time() - _parse_utc(start)) / 3600) if start else 0.0
    rate = os.environ.get("E12_GPU_RATE")
    pod_rate = os.environ.get("EMX_POD_RATE_USD_H")  # sent by pod_push.sh from the pod record
    if rate:
        rate_f, src["rate"] = float(rate), "E12_GPU_RATE"
    elif pod_rate:
        rate_f, src["rate"] = float(pod_rate), "EMX_POD_RATE_USD_H"
    else:
        gpu = gpu or _gpu_name()
        rates = dict(C.FALLBACK_GPUS)
        if gpu in rates:
            rate_f, src["rate"] = rates[gpu], f"FALLBACK_GPUS[{gpu}]"
        else:
            rate_f, src["rate"] = max(rates.values()), "highest FALLBACK_GPUS rate"
    api = float(os.environ.get("E12_API_SPENT_USD", "0") or 0)
    src["api"] = "E12_API_SPENT_USD" if "E12_API_SPENT_USD" in os.environ else "default 0"
    return {"elapsed_pod_hours": round(elapsed, 4), "gpu_rate": rate_f, "api_spent_usd": api,
            "sources": src}


def admit(ctx: Ctx, stage: str, gen_path: str, endpoint: int | None) -> dict:
    clock = pod_clock(ctx)
    rec = budget.admit(ctx.recipe_name, stage, gen_path=gen_path, endpoint=endpoint,
                       elapsed_pod_hours=clock["elapsed_pod_hours"], gpu_rate=clock["gpu_rate"],
                       api_spent_usd=clock["api_spent_usd"], root=ctx.root, write=True)
    log(f"projection {stage} ({gen_path}, endpoint {endpoint}): {rec.get('verdict')} cuts "
        f"{rec.get('cuts')} hours {rec.get('total_pod_hours')} gpu ${rec.get('gpu_usd')} "
        f"api ${rec.get('api_usd')} clock {clock['sources']}")
    return rec


# --- inputs and download -------------------------------------------------------------------------

BASE_SHAPE = {"num_hidden_layers": R.N_LAYERS, "hidden_size": R.HIDDEN,
              "intermediate_size": R.INTERMEDIATE, "vocab_size": R.VOCAB}


def verify_inputs(ctx: Ctx) -> dict[str, str]:
    """{path: sha256} of the question files and EQ0's base answers against their frozen hashes,
    after `data.verify_prerun_manifest` (every data hash and id list). Refused otherwise."""
    try:
        data.verify_prerun_manifest(ctx.root)
    except (ValueError, FileNotFoundError) as e:
        raise Refused(f"pre-run manifest: {e}") from None
    want = {C.QUESTION_FILES[q]: C.QUESTION_SHA256[q] for q in C.QSETS}
    want[C.EQ0_BASE_GEN] = C.EQ0_BASE_GEN_SHA256
    want[C.EQ0_BASE_JUDGED] = C.EQ0_BASE_JUDGED_SHA256
    want[C.PRERUN_MANIFEST] = None
    out = {}
    for name, sha in want.items():
        p = ctx.root / name
        if not p.is_file():
            raise Refused(f"{name} missing")
        got = G.sha256_file(p)
        if sha is not None and got != sha:
            raise Refused(f"{name}: sha256 {got} != frozen {sha}")
        out[name] = got
    return out


def fetch_base(snapshot=None) -> dict:
    """Base snapshot at BASE_REV (HF cache); checks the config shape; lists files and blobs."""
    if snapshot is None:
        from huggingface_hub import snapshot_download as snapshot
    path = Path(snapshot(repo_id=R.BASE_ID, revision=R.BASE_REV, allow_patterns=BASE_PATTERNS))
    cfg = json.loads((path / "config.json").read_text())
    bad = {k: (cfg.get(k), v) for k, v in BASE_SHAPE.items() if cfg.get(k) != v}
    if bad:
        raise Refused(f"base config.json differs from the registry: {bad}")
    files = {
        str(p.relative_to(path)): {"size": p.stat().st_size, "blob": Path(os.path.realpath(p)).name}
        for p in sorted(path.rglob("*")) if p.is_file()
    }
    return {"path": str(path), "revision": R.BASE_REV, "files": files,
            "config_shape": {k: cfg.get(k) for k in BASE_SHAPE}}


def _tokenizer():
    """The pinned tokenizer; a hook so tests can swap in a fake."""
    from emx.qwen.render import load_tokenizer

    return load_tokenizer()


def _template_sha(tok) -> str:
    from emx.qwen.render import template_sha256

    return template_sha256(tok)


def step_download(ctx: Ctx) -> int:
    started = G.utc()
    inputs = verify_inputs(ctx)
    log(f"download: base {R.BASE_ID}@{R.BASE_REV[:12]} (HF_HOME={os.environ.get('HF_HOME')})")
    base = fetch_base()
    tok = _tokenizer()
    rec = {**step_info(ctx, started), "hf_home": os.environ.get("HF_HOME"),
           "base": {**base, "id": R.BASE_ID},
           "tokenizer": {"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                         "template_sha256": _template_sha(tok)},
           "inputs_sha256": inputs}
    out = C.bench_path(ctx.recipe_name, "download")
    write_json(ctx.root / out, rec)
    write_step_manifest(ctx, "download", [out])
    return 0


# --- training ------------------------------------------------------------------------------------

_execv = os.execv  # a hook so tests can stand in for the OOM re-exec
OK_STATUSES = ("ok", "restarted_ok")


class RunItem:
    """One planned training run: its spec fields before the rows are loaded."""

    def __init__(self, segment: str, arm: str, seed: int, parent_key: str | None,
                 saves: dict[int, str], *, gating: bool, endpoint: int | None = None,
                 run: str | None = None):
        self.segment, self.arm, self.seed, self.parent_key = segment, arm, seed, parent_key
        self.saves, self.gating, self.endpoint = saves, gating, endpoint
        self.run = run or C.run_name(segment, f"P{seed}" if segment == "poison" else f"{arm}{seed}")
        self.final_key = saves[max(saves)]

    def describe(self) -> str:
        return (f"{self.run} ({self.segment}, arm {self.arm}, seed {self.seed}, parent "
                f"{self.parent_key or 'fresh'}, saves {sorted(self.saves.values())}"
                f"{', gating' if self.gating else ''})")


def poison_items(seeds) -> list[RunItem]:
    n = C.SEGMENT_STEPS["poison"]
    return [RunItem("poison", "P", s, None, {n: f"P{s}"}, gating=True) for s in seeds]


def repair_items() -> list[RunItem]:
    """A and C first (they gate), then B; fresh adapter for C, P<s> for A and B."""
    return [RunItem("repair", a, s, None if a == "C" else f"P{s}",
                    {r: C.format_key(a, s, r) for r in C.REPAIR_SAVES}, gating=a in ("A", "C"),
                    endpoint=C.REPAIR_SAVES[-1])
            for a in ("A", "C", "B") for s in C.TRAIN_SEEDS]


def rescue_items() -> list[RunItem]:
    n = C.SEGMENT_STEPS["rescue"]
    return [RunItem("rescue", a, s, C.format_key(a, s, C.REPAIR_SAVES[-1]),
                    {n: C.format_key(a, s, C.RESCUE_END)}, gating=False, endpoint=C.RESCUE_END)
            for a in C.REPAIR_ARMS for s in C.TRAIN_SEEDS]


def nudge_items(cuts=()) -> list[RunItem]:
    """The 24 runs of `contracts.nudge_parents()` (the base reference's seeds in TRAIN_SEEDS
    order); cut 3 drops the base reference."""
    out, base_seeds = [], iter(C.TRAIN_SEEDS)
    for parent in C.nudge_parents():
        if parent == C.BASE_KEY:
            s, arm, pkey, ep = next(base_seeds), "R", None, None
            if 3 in cuts:
                continue
        else:
            k = C.parse_key(parent)
            s, pkey = k.seed, parent
            arm, ep = ("D", None) if k.arm == "P" else (k.arm, k.repair)
        saves = {d: C.nudge_key(parent, s, d) for d in C.NUDGE_DOSES}
        out.append(RunItem("nudge", arm, s, pkey, saves, gating=False, endpoint=ep,
                           run=C.run_name("nudge", parent, s)))
    return out


def run_status(ctx: Ctx, run: str) -> dict | None:
    p = ctx.root / C.status_path(ctx.recipe_name, run)
    return read_json(p) if p.is_file() else None


def ckpt_exists(ctx: Ctx, key: str) -> bool:
    return (ctx.root / C.ckpt_dir(ctx.recipe_name, key) / "ckpt.json").is_file()


def run_done(ctx: Ctx, item: RunItem) -> bool:
    st = run_status(ctx, item.run)
    return bool(st) and st.get("status") in OK_STATUSES and ckpt_exists(ctx, item.final_key)


producing_run = deps.producing_run  # the shared chain rule (code review r2 finding 4)
producing_parent = deps.producing_parent
DEP_OK, DEP_FAILED, DEP_PENDING = deps.OK, deps.FAILED, deps.PENDING


def dependency_state(ctx: Ctx, key: str) -> tuple[str, str | None]:
    """(state, reason) of checkpoint `key` by `emx.e12.deps.state` (review r1 finding 10, r2
    finding 4): failed (terminal: an explicit `failed` status on its producing run or up its
    chain), ok (producer ok/restarted_ok and ckpt.json present), else pending (a later re-run may
    still produce it, so it never gets a terminal skip record)."""
    return deps.state(ctx.root, ctx.recipe_name, key)


def dependency_problem(ctx: Ctx, key: str) -> str | None:
    """Why checkpoint `key` cannot be used now (failed or pending), else None."""
    state, why = dependency_state(ctx, key)
    return None if state == DEP_OK else why


def write_run_status(ctx: Ctx, item: RunItem, status: str, reason: str, attempts: int = 0) -> None:
    write_json(ctx.root / C.status_path(ctx.recipe_name, item.run), {
        "run": item.run, "recipe": ctx.recipe_name, "arm": item.arm, "seed": item.seed,
        "segment": item.segment, "endpoint": item.endpoint, "status": status,
        "attempts": attempts, "reason": reason, "utc": G.utc()})


class Rows:
    """Data loaded once per step process, on first use."""

    def __init__(self, ctx: Ctx):
        self.ctx, self._c = ctx, {}

    def get(self, name: str, *a):
        k = (name, *a)
        if k not in self._c:
            self._c[k] = getattr(data, name)(*a, self.ctx.root)
        return self._c[k]


def build_spec(ctx: Ctx, item: RunItem, rows: Rows, tok) -> trainer.SegmentSpec:
    seg, s = item.segment, item.seed
    if seg == "poison":
        train = rows.get("poison_rows")
    elif seg in ("repair", "rescue"):
        train = rows.get("repair_rows", item.arm)
    else:
        train = rows.get("reserve_rows")
    order = {"poison": s, "repair": s, "rescue": C.rescue_seed(s), "nudge": C.nudge_seed(s)}[seg]
    if "continuity" not in rows._c:
        rows._c["continuity"] = data.continuity_item(tok, ctx.root)
    return trainer.SegmentSpec(
        recipe=ctx.recipe, run=item.run, segment=seg, arm=item.arm, seed=s,
        parent_key=item.parent_key, rows=train, order_seed=order,
        init_seed=s if item.parent_key is None else None,
        lr=ctx.recipe.nudge_lr if seg == "nudge" else ctx.recipe.lr,
        schedule=C.SEGMENT_SCHEDULE[seg], steps=C.SEGMENT_STEPS[seg], saves=dict(item.saves),
        step_offset=C.REPAIR_SAVES[-1] if seg == "rescue" else 0,
        heldout_rows=rows.get("heldout_rows"),
        probe_rows=rows.get("probe_rows") if seg == "nudge" else None,
        continuity=rows._c["continuity"],
        # rank 32 (all 7 projections, batch 2 x 2048) ran out of memory without checkpointing in
        # its smoke bench on the H100 80GB (2026-10-09); it always trains with it (as Turner's
        # unsloth recipe does); the bench then measures the speed the real run gets
        gradient_checkpointing=bool(ctx.args.gradient_checkpointing)
        or ctx.recipe_name == "rank32", endpoint=item.endpoint)


def _is_oom(e: BaseException) -> bool:
    return type(e).__name__ == "OutOfMemoryError" or "CUDA out of memory" in str(e)


def run_outputs(ctx: Ctx, item: RunItem) -> list[str]:
    """Every existing file of a run: status, summary, step log, its checkpoints' records (and
    the rank-1 adapter files beside them), and the nudge's adapter-change record."""
    r = ctx.recipe_name
    out = [C.status_path(r, item.run), C.train_summary(r, item.run),
           C.train_steps(r, item.run), C.bench_path(r, f"adapter_change/{item.run}")]
    for key in item.saves.values():
        d = ctx.root / C.ckpt_dir(r, key)
        if d.is_dir():
            out += [ctx.rel(p) for p in sorted(d.rglob("*")) if p.is_file()
                    and not recovery_excluded(p.name)]
    return [p for p in out if (ctx.root / p).is_file()]


def adapter_change(ctx: Ctx, item: RunItem) -> None:
    """Nudge: each dose's distance from its dose-0 adapter (prereg §4.8 item 6); recorded only.
    The base reference's dose 0 is the bare base: its distances are in ckpt.json (trainer)."""
    if item.segment != "nudge" or item.parent_key is None:
        return
    p0 = ctx.path(C.adapter_files_dir(ctx.recipe_name, item.parent_key))
    rec = {}
    for d, key in sorted(item.saves.items()):
        try:
            rec[key] = trainer.adapter_distance(
                ctx.path(C.adapter_files_dir(ctx.recipe_name, key)), p0)
        except Exception as e:  # noqa: BLE001 - a diagnostic: recorded, never fatal
            rec[key] = {"error": f"{type(e).__name__}: {e}"}
    write_json(ctx.root / C.bench_path(ctx.recipe_name, f"adapter_change/{item.run}"),
               {"run": item.run, "parent": item.parent_key, "distances": rec, "utc": G.utc()})


def train_runs(ctx: Ctx, items: list[RunItem]) -> int:
    """Train `items` in order (skipping complete runs); write bench/<step>.runs.json and the
    step manifest. A failed gating run fails the step (no manifest)."""
    if ctx.dry:
        for it in items:
            print(f"{'skip' if run_done(ctx, it) else 'train'} {it.describe()}")
        return C.EXIT_OK
    started = G.utc()
    rows, model, tok, outcomes = Rows(ctx), None, None, {}
    for it in items:
        if run_done(ctx, it):
            log(f"{ctx.step}: {it.run} complete; skipped")
            outcomes[it.run] = "skipped_complete"
            continue
        check_stop(ctx, f"before {it.run}")
        pstate, problem = (dependency_state(ctx, it.parent_key) if it.parent_key
                           else (DEP_OK, None))
        prior = (run_status(ctx, it.run) or {}).get("status")
        if prior == "failed":  # terminal (review r1 finding 4): never retrained
            log(f"{ctx.step}: {it.run} has a terminal failed status; not retrained")
        elif pstate == DEP_FAILED:
            log(f"{ctx.step}: {it.run} not trained: parent {problem}")
            write_run_status(ctx, it, "failed", f"parent {it.parent_key}: {problem}")
        elif pstate == DEP_PENDING:
            log(f"{ctx.step}: {it.run} not trained: parent {problem}")
            outcomes[it.run] = DEP_PENDING
            if it.gating:
                return C.EXIT_FAILED
            continue
        else:
            if model is None:
                model, tok = trainer.load_base_eager("cuda")
            spec = build_spec(ctx, it, rows, tok)
            log(f"{ctx.step}: train {it.describe()}")
            try:
                summary = trainer.run_segment(model, tok, spec, root=ctx.root)
            except Exception as e:
                if _is_oom(e):
                    raise
                log(f"{ctx.step}: {it.run} raised {type(e).__name__}: {e}")
                write_run_status(ctx, it, "failed", f"{type(e).__name__}: {str(e)[:2000]}")
                summary = None
            summ = ctx.root / C.train_summary(ctx.recipe_name, it.run)
            if summary is not None and not summ.exists():
                write_json(summ, summary)
            if (run_status(ctx, it.run) or {}).get("status") in OK_STATUSES:
                adapter_change(ctx, it)
        st = (run_status(ctx, it.run) or {}).get("status", "missing")
        if st in OK_STATUSES and not ckpt_exists(ctx, it.final_key):
            write_run_status(ctx, it, "failed", f"final checkpoint {it.final_key} missing")
            st = "failed"
        outcomes[it.run] = st
        _recovery_quietly(ctx)
        if st not in OK_STATUSES and it.gating:
            log(f"{ctx.step}: gating run {it.run} {st}: the step fails")
            return C.EXIT_FAILED
        check_stop(ctx, f"after {it.run}")
    rec_path = C.bench_path(ctx.recipe_name, f"{ctx.step}.runs")
    write_json(ctx.root / rec_path, {**step_info(ctx, started), "runs": outcomes,
                                     "planned": [it.describe() for it in items]})
    pending = [r for r, o in outcomes.items() if o == DEP_PENDING]
    if pending:  # no manifest: a later re-run of this step trains them once their parents exist
        log(f"{ctx.step}: FAILED: {len(pending)} runs pending on a missing parent: {pending}")
        return C.EXIT_FAILED
    outs = [rec_path] + [p for it in items for p in run_outputs(ctx, it)]
    if ctx.step == "poison-s1":  # fail fast on a reload mismatch (integration, 2026-10-08)
        if model is None:
            model, tok = trainer.load_base_eager("cuda")
        chk = trainer.verify_checkpoint(model, build_spec(ctx, items[0], rows, tok),
                                        C.LORA_CHECK_KEY, ctx.root)
        chk_path = C.bench_path(ctx.recipe_name, "reload_check")
        write_json(ctx.root / chk_path, {**chk, "utc": G.utc()})
        log(f"poison-s1: reload check {chk}")
        if not chk["ok"]:
            return C.EXIT_FAILED
        outs.append(chk_path)
    write_step_manifest(ctx, ctx.step, outs)
    return C.EXIT_OK


def train_step(ctx: Ctx, items: list[RunItem]) -> int:
    """`train_runs`, with EQ0's CUDA-OOM retry: the step re-execs itself once in a fresh process
    with --gradient-checkpointing --after-oom (which refuses a second retry)."""
    a = ctx.args
    if a.after_oom and not a.gradient_checkpointing:
        raise Refused("--after-oom is the gradient-checkpointing retry")
    try:
        return train_runs(ctx, items)
    except Exception as e:
        if not _is_oom(e):
            raise
        write_json(ctx.root / C.bench_path(ctx.recipe_name, f"oom_{ctx.step}"),
                   {"step": ctx.step, "error": f"{type(e).__name__}: {str(e)[:2000]}",
                    "gradient_checkpointing": bool(a.gradient_checkpointing), "utc": G.utc()})
        if a.after_oom or a.gradient_checkpointing:
            log(f"{ctx.step}: CUDA OOM with gradient checkpointing; no further retry")
            return C.EXIT_FAILED
        argv = [sys.executable, str(Path(__file__).resolve()), ctx.step, "--recipe",
                ctx.recipe_name, "--root", str(ctx.root), "--scratch", str(ctx.scratch),
                "--gradient-checkpointing", "--after-oom"]
        _recovery_quietly(ctx)
        log(f"{ctx.step}: CUDA OOM without gradient checkpointing; re-exec: {argv[1:]}")
        sys.stdout.flush()
        sys.stderr.flush()
        _execv(sys.executable, argv)
        return C.EXIT_FAILED  # only reached when the exec hook returns (tests)


def step_poison_s1(ctx: Ctx) -> int:
    return train_step(ctx, poison_items(C.TRAIN_SEEDS[:1]))


def step_poison(ctx: Ctx) -> int:
    return train_step(ctx, poison_items(C.TRAIN_SEEDS[1:]))


def step_repair(ctx: Ctx) -> int:
    return train_step(ctx, repair_items())


def step_rescue(ctx: Ctx) -> int:
    return train_step(ctx, rescue_items())


def step_nudge(ctx: Ctx) -> int:
    return train_step(ctx, nudge_items(effective_cuts(ctx)))


# --- vLLM and question helpers -------------------------------------------------------------------


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version); a hook so tests can swap in fakes."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, getattr(vllm, "__version__", "unknown")


class Inputs:
    """Tokenizer, questions, cue questions and holdout items, loaded once per step process."""

    def __init__(self, ctx: Ctx):
        self.tok = _tokenizer()
        self.questions = data.load_questions(ctx.root)
        self.cue_qs = data.cue_questions(self.questions)
        self.holdout = data.holdout_items(ctx.root)
        self.ctx = ctx

    def requests(self, job: C.Job, cuts=()) -> list:
        return gen.build_requests(self.ctx.recipe_name, job.kind, job.key, self.tok,
                                  self.questions, cue_qs=self.cue_qs, holdout=self.holdout,
                                  cuts=cuts)

    def smoke_prompt_ids(self) -> list[list[int]]:
        """The first SMOKE_PROMPTS general questions rendered (EQ0's smoke prompts)."""
        from emx.qwen.render import render_prompt

        return [render_prompt(self.tok, q["question"])[1]
                for q in self.questions["general"][: C.SMOKE_PROMPTS]]


def missing_fields(rec: dict, name: str) -> list[str]:
    return [f for f in C.BENCH_FIELDS[name] if f not in rec]


def write_bench(ctx: Ctx, name: str, rec: dict) -> str:
    """bench/<name>.json, refusing a record without its `BENCH_FIELDS`."""
    miss = missing_fields(rec, name)
    if miss:
        raise RuntimeError(f"{name}: missing BENCH_FIELDS {miss}")
    out = C.bench_path(ctx.recipe_name, name)
    write_json(ctx.root / out, rec)
    return out


# --- smoke: base smoke, three benchmarks, the projection -----------------------------------------

# Scratch-tree names of the bench adapters (valid planned keys, so they get real LoRA int ids),
# saved every 2 steps of the 20-step bench run: 10 > MAX_LORAS (eviction across calls).
BENCH_KEYS: tuple[str, ...] = tuple(C.planned_keys("nudge")[-12:][: C.GEN_BENCH_ADAPTERS])
BENCH_SAVES = {2 * (i + 1): k for i, k in enumerate(BENCH_KEYS)}
assert max(BENCH_SAVES) == C.TRAIN_BENCH_STEPS
BASE_SMOKE = "base_smoke"


def bench_root(ctx: Ctx) -> Path:
    """A separate tree (same contracts layout) for the benchmark run and its 3 adapters, so
    nothing in the real results tree is touched; excluded from the recovery manifest."""
    return ctx.root / C.rdir(ctx.recipe_name) / "bench" / "_scratch"


def _bench_row_ids(rows: list[dict], seed: int, recipe: C.Recipe, n_rows: int) -> list[str]:
    """Ids of the first n_rows rows in the trainer's seeded order (a hook for tests)."""
    from emx.qwen import train as T

    order = [i for mb in T.micro_batches(len(rows), seed, recipe.per_device_batch) for i in mb]
    return [rows[i]["id"] for i in order[:n_rows]]


def _peak_memory() -> int | None:
    try:
        import torch

        return int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else None
    except ImportError:
        return None


def train_bench_record(ctx: Ctx, steps: list[dict], row_ids: list[str]) -> dict:
    """train_bench from the step log: the first (warm-up) step excluded from the timings."""
    timed = steps[1:] or steps
    secs = sum(float(s["seconds"]) for s in timed)
    toks = sum(int(s["n_tokens"]) for s in timed)
    return {"recipe": ctx.recipe_name, "gpu": _gpu_name(), "attn": "eager",
            "n_steps": len(steps), "s_per_step": secs / max(1, len(timed)),
            "tokens_per_s": toks / secs if secs > 0 else 0.0,
            "mean_tokens_per_example": sum(int(s["n_tokens"]) for s in steps)
            / max(1, len(steps) * ctx.recipe.effective_batch),
            "peak_memory_bytes": _peak_memory(),
            "row_ids_sha256": hashlib.sha256(json.dumps(row_ids).encode()).hexdigest(),
            "utc": G.utc()}


def smoke_hf(ctx: Ctx) -> int:
    """peft venv: the 20-step training benchmark (saving 3 small adapters in the scratch tree),
    then 64 HF-decoded base answers."""
    r = ctx.recipe_name
    broot = bench_root(ctx)
    item = RunItem("poison", "P", 1, None, dict(BENCH_SAVES), gating=True)
    rows = Rows(ctx)
    model, tok = trainer.load_base_eager("cuda")
    spec = build_spec(ctx, item, rows, tok)
    spec.steps = C.TRAIN_BENCH_STEPS
    t0 = time.perf_counter()
    summary = trainer.run_segment(model, tok, spec, root=broot)
    log(f"smoke hf: train bench {C.TRAIN_BENCH_STEPS} steps in {time.perf_counter() - t0:.1f}s "
        f"status {summary.get('status')}")
    if summary.get("status") not in OK_STATUSES:
        raise RuntimeError(f"train bench run {summary.get('status')}: {summary}")
    steps = [json.loads(ln) for ln in (broot / C.train_steps(r, item.run)).read_text().splitlines()
             if ln.strip()]
    ids = _bench_row_ids(spec.rows, spec.order_seed, ctx.recipe,
                         C.TRAIN_BENCH_STEPS * ctx.recipe.effective_batch)
    write_bench(ctx, "train_bench", train_bench_record(ctx, steps, ids))
    inp = Inputs(ctx)
    reqs = inp.requests(C.Job("full", C.BASE_KEY, C.KIND_ROWS["full"]))[: C.HF_BENCH_ANSWERS]
    t0 = time.perf_counter()
    out_rows, thr = hf_gen.generate_hf(model, tok, reqs, batch_size=hf_gen.HF_BATCH_SIZE)
    wall = time.perf_counter() - t0
    write_bench(ctx, "hf_bench", {
        "gpu": _gpu_name(), "n_answers": len(out_rows), "batch_size": hf_gen.HF_BATCH_SIZE,
        "wall_seconds": wall, "answers_per_s": len(out_rows) / wall if wall > 0 else 0.0,
        "mean_new_tokens": _mean_new_tokens(out_rows), "throughput": thr, "utc": G.utc()})
    return C.EXIT_OK


def _mean_new_tokens(rows: list[dict]) -> float:
    vals = [float(x["n_new_tokens"]) for x in rows if str(x.get("n_new_tokens", "")) != ""]
    return sum(vals) / len(vals) if vals else 0.0


def _csv_rows(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def smoke_verdict(items: list[dict]) -> tuple[bool, dict]:
    """EQ0's base smoke (scripts/eq0_pod.py `smoke_verdict`): every answer non-empty; every
    non-length finish is "stop" on <|im_end|>; at most one "length" finish."""
    empty = sum(1 for x in items if not x["response"].strip())
    n_len = sum(1 for x in items if x["finish_reason"] == "length")
    bad_stop = sum(1 for x in items if x["finish_reason"] != "length"
                   and not (x["finish_reason"] == "stop" and x["last_token_id"] == R.IM_END_ID))
    ok = bool(items) and empty == 0 and n_len <= 1 and bad_stop == 0
    return ok, {"n": len(items), "empty": empty, "length_finishes": n_len,
                "not_im_end_stops": bad_stop, "ok": ok}


def smoke_vllm(ctx: Ctx) -> int:
    """vllm venv: EQ0's base smoke, then the generation benchmark (base + 10 bench adapters x 160
    answers, grouped into calls of <= MAX_LORAS adapters, so with eviction) and the applied-smoke
    on the bench adapters, in the scratch tree."""
    started = G.utc()
    r = ctx.recipe_name
    broot = bench_root(ctx)
    missing = [k for k in BENCH_KEYS if not (broot / C.ckpt_dir(r, k) / "ckpt.json").is_file()]
    if missing:
        raise Refused(f"smoke vllm: bench adapters {missing} missing (run smoke --part hf first)")
    inp = Inputs(ctx)
    LLM, SamplingParams, LoRARequest, version = _vllm()
    kw = gen.engine_kwargs()
    t0 = time.perf_counter()
    llm = LLM(**kw)
    engine_start = time.perf_counter() - t0
    reqs = [q for q in inp.requests(C.Job("full", C.BASE_KEY, C.KIND_ROWS["full"]))
            if q.qset == "general" and q.sample_idx == 0][: C.SMOKE_PROMPTS]
    outs = G.generate_outputs(llm, reqs, None, SamplingParams)
    rows = G.rows_from_outputs(inp.tok, reqs, outs)
    items = []
    for q, row, o in zip(reqs, rows, outs, strict=True):
        ids = [int(t) for t in o.outputs[0].token_ids]
        items.append({"qid": q.qid, "seed": q.seed, "response": row["response"],
                      "finish_reason": row["finish_reason"],
                      "last_token_id": ids[-1] if ids else None,
                      "n_new_tokens": row["n_new_tokens"]})
    ok, verdict = smoke_verdict(items)
    write_json(ctx.root / C.bench_path(r, BASE_SMOKE), {
        **step_info(ctx, started), "passed": ok, "verdict": verdict, "items": items,
        "engine_version": version, "gpu": _gpu_name(), "llm_kwargs": kw})
    if not ok:
        log(f"smoke vllm: base smoke FAILED {verdict}")
        return C.EXIT_FAILED
    # base rides in the same grouped calls (a per-request LoRA list with None entries)
    jobs = [C.Job("traj", k, C.KIND_ROWS["traj"]) for k in (C.BASE_KEY, *BENCH_KEYS)]
    # always a fresh measurement: a retried smoke must not divide cached answers by the retry's
    # time (code review r3 finding 2); the scratch tree holds nothing else of value
    shutil.rmtree(broot / C.rdir(r) / "gen", ignore_errors=True)
    t0 = time.perf_counter()
    res = gen.generate_jobs(llm, inp.tok, r, jobs, inp.requests, root=broot, gen_path="vllm",
                            engine_version=f"vllm {version}", gpu=_gpu_name(),
                            sampling_cls=SamplingParams, lora_cls=LoRARequest)
    wall = time.perf_counter() - t0
    gen_rows = [x for j in jobs for x in _csv_rows(broot / C.gen_csv(r, "traj", j.key))]
    # applied-smoke on the bench adapters: exercises vLLM prompt_logprobs before any bulk work
    smoke = gen.applied_smoke(llm, inp.tok, r, BENCH_KEYS, inp.smoke_prompt_ids(),
                              root=broot, sampling_cls=SamplingParams, lora_cls=LoRARequest)
    bad = {k: v.get("verdict") for k, v in smoke.items() if v.get("verdict") != "PASS"}
    if bad:
        log(f"smoke vllm: bench applied-smoke not PASS: {bad}")
    # an execution error (UNVERIFIED) or a missing record gates the smoke step; a measured FAIL
    # (a genuinely small effect, e.g. the 2-step bench adapter) is logged only (code review r3 #1)
    broken = sorted(k for k in BENCH_KEYS
                    if (smoke.get(k) or {}).get("verdict") not in ("PASS", "FAIL"))
    if broken:
        log(f"smoke vllm: applied-smoke could not be measured for {broken}; the step fails")
        return C.EXIT_FAILED
    new = [x for x in res.get("done") or []]
    if len(new) != len(jobs):
        log(f"smoke vllm: the bench generated {len(new)} of {len(jobs)} jobs fresh; the step fails")
        return C.EXIT_FAILED
    thr = res.get("throughput") or {}
    write_bench(ctx, "gen_bench", {
        "recipe": r, "gpu": _gpu_name(), "engine": f"vllm {version}", "n_adapters": len(jobs),
        "n_answers": len(gen_rows), "wall_seconds": wall,
        "answers_per_s": len(gen_rows) / wall if wall > 0 else 0.0,
        "mean_new_tokens": _mean_new_tokens(gen_rows), "engine_start_seconds": engine_start,
        "adapter_load_seconds": float(thr.get("adapter_load_seconds") or 0.0),
        "throughput": thr, "utc": G.utc()})
    if r != "rank1":  # rank-32 adapter files sit in pod scratch, where the real runs save later
        for k in BENCH_KEYS:
            shutil.rmtree(ctx.path(C.adapter_files_dir(r, k)), ignore_errors=True)
    return C.EXIT_OK


def smoke_stage(recipe: str) -> str:
    return "smoke" if recipe == C.DEFAULT_RECIPE else "rank32"


def smoke_score(ctx: Ctx) -> int:
    """Any venv: the three bench records and the base smoke verified, then the projection of the
    whole remaining speculative workload; refuse -> exit 2 (no manifest)."""
    r = ctx.recipe_name
    outs = []
    for name in ("train_bench", "gen_bench", "hf_bench"):
        p = C.bench_path(r, name)
        if not (ctx.root / p).is_file():
            raise Refused(f"smoke score: {p} missing")
        miss = missing_fields(read_json(ctx.root / p), name)
        if miss:
            raise Refused(f"smoke score: {p} lacks {miss}")
        outs.append(p)
    bs = C.bench_path(r, BASE_SMOKE)
    if not (ctx.root / bs).is_file() or not read_json(ctx.root / bs).get("passed"):
        raise Refused(f"smoke score: {bs} missing or not passed")
    stage = smoke_stage(r)
    proj = admit(ctx, stage, "vllm", None)
    if proj.get("verdict") != "admit":
        raise BudgetRefused(f"projection {stage} refused: {proj.get('total_pod_hours')} h, "
                            f"GPU ${proj.get('gpu_usd')}, API ${proj.get('api_usd')}")
    write_step_manifest(ctx, "smoke", [*outs, bs, C.projection_path(r, stage)])
    return C.EXIT_OK


def step_smoke(ctx: Ctx) -> int:
    if ctx.dry:
        print(f"smoke --part {ctx.part}: scratch tree {bench_root(ctx)}; stage "
              f"{smoke_stage(ctx.recipe_name)}")
        return C.EXIT_OK
    return {"hf": smoke_hf, "vllm": smoke_vllm, "score": smoke_score}[ctx.part](ctx)


# --- lora-check: the trained-adapter vLLM re-check on P1 -----------------------------------------

LORA_PART = {"hf": "lora_check_hf.part", "vllm": "lora_check_vllm.part"}


def write_genpath(ctx: Ctx, rec: dict) -> None:
    G.atomic_write(control_file(ctx, "genpath.json"),
                   lambda fh: fh.write(json.dumps(rec, indent=2) + "\n"))


def job_list(jobs) -> list[list]:
    return [[j.kind, j.key, j.n_rows] for j in jobs]


def lora_check_score(ctx: Ctx) -> int:
    r = ctx.recipe_name
    for name in LORA_PART.values():
        if not (ctx.root / C.bench_path(r, name)).is_file():
            raise Refused(f"lora-check score: {C.bench_path(r, name)} missing")
    # run_score loads the HF part's log-prob array (.npy, sha-checked) beside its JSON record
    res = lora_check.run_score(r, C.LORA_CHECK_KEY, ctx.root)
    out = write_bench(ctx, "lora_check", res)
    gp = res["gen_path"]
    if gp not in C.GEN_PATHS:
        raise RuntimeError(f"lora-check gen_path {gp!r}")
    print(f"E12_LORA_CHECK verdict={res['verdict']} gen_path={gp} "
          f"nearest={res['n_nearest_1x']}/{res['n_prompts']}", flush=True)
    outs = [out, *(C.bench_path(r, n) for n in LORA_PART.values()),
            *(C.bench_path(r, n) for n in (lora_check.HF_NAME, lora_check.VLLM_NAME)),
            lora_check.hf_npy_path(r)]
    old = read_control(ctx, "genpath.json")
    if old and old.get("admitted"):
        log(f"lora-check: genpath.json already admitted ({old.get('gen_path')}); kept")
        if old.get("gen_path") == "vllm":
            outs.append(C.projection_path(r, "lora-check"))
        outs.append(C.admission_path(r))
    elif gp == "vllm":
        proj = admit(ctx, "lora-check", "vllm", None)
        cuts = _cuts(proj.get("cuts"))
        jobs = C.planned_jobs(r, "vllm", None, cuts)
        ok = proj.get("verdict") == "admit"
        rec = {"gen_path": "vllm", "admitted": ok, "cuts": list(cuts),
               "jobs_sha256": C.jobs_sha256(jobs), "jobs": job_list(jobs),
               "branch": "vllm_speculative", "projection": C.projection_path(r, "lora-check"),
               "source": "pod lora-check", "utc": G.utc()}
        if not ok:  # review r1 finding 3: stop the chain like smoke's refusal (no manifest)
            write_genpath(ctx, {**rec, "stage": "lora-check",
                                "lora_check_verdict": res["verdict"]})
            raise BudgetRefused(f"projection lora-check refused: {proj.get('total_pod_hours')} h,"
                                f" GPU ${proj.get('gpu_usd')}, API ${proj.get('api_usd')}")
        write_json(ctx.root / C.admission_path(r), rec)  # review r1 finding 6
        write_genpath(ctx, {**rec, "stage": "lora-check", "lora_check_verdict": res["verdict"]})
        outs += [C.projection_path(r, "lora-check"), C.admission_path(r)]
    else:
        admit(ctx, "hf", "hf", C.RESCUE_END)  # the conservative complete branch, for the laptop
        write_genpath(ctx, {"gen_path": "hf", "admitted": False, "stage": "hf",
                            "projection": C.projection_path(r, "hf"),
                            "lora_check_verdict": res["verdict"], "source": "pod lora-check",
                            "utc": G.utc()})
        outs.append(C.projection_path(r, "hf"))
    write_step_manifest(ctx, "lora-check", [p for p in outs if (ctx.root / p).is_file()])
    return C.EXIT_OK


def step_lora_check(ctx: Ctx) -> int:
    if ctx.dry:
        print(f"lora-check --part {ctx.part} on {C.LORA_CHECK_KEY}")
        return C.EXIT_OK
    if ctx.part == "score":
        return lora_check_score(ctx)
    problem = dependency_problem(ctx, C.LORA_CHECK_KEY)
    if problem:
        raise Refused(f"lora-check: {problem}")
    fn = lora_check.hf_part if ctx.part == "hf" else lora_check.vllm_part
    rec = fn(ctx.recipe_name, C.LORA_CHECK_KEY, ctx.root)
    write_json(ctx.root / C.bench_path(ctx.recipe_name, LORA_PART[ctx.part]), rec)
    return C.EXIT_OK


# --- evaluation steps ----------------------------------------------------------------------------

DURABILITY_KINDS: tuple[str, ...] = (*C.CUE_KINDS, "task")


def _arm(key: str) -> str | None:
    return C.parse_key(key).arm


def in_step(step: str, job: C.Job) -> bool:
    """Which eval step generates `job` (each planned job belongs to exactly one)."""
    arm = _arm(job.key)
    if step == "eval-poison":
        return (job.kind == "full" and arm == "P") or (
            job.kind in DURABILITY_KINDS and arm in (None, "P"))
    if step == "eval-endpoints":
        return job.kind == "full" and arm in C.REPAIR_ARMS
    if step == "eval-traj":
        return job.kind == "traj"
    if step == "eval-nudge":
        return job.kind == "nudge"
    if step == "eval-cue-task":
        return job.kind in DURABILITY_KINDS and arm in C.REPAIR_ARMS
    raise ValueError(f"{step!r} is not an eval step")


def _jobs_from(raw) -> list[C.Job]:
    out = []
    for x in raw or []:
        out.append(C.Job(x["kind"], x["key"], int(x["n_rows"])) if isinstance(x, dict)
                   else C.Job(str(x[0]), str(x[1]), int(x[2])))
    return out


class GenState:
    """The generation path, endpoint, cuts and this step's job slice, from the control files and
    the admission record (review r1 findings 1 and 6).

    Both genpath.json (control) and `contracts.admission_path` (results tree) must exist, be
    admitted and agree (gen_path, jobs_sha256); a missing or unadmitted one is BLOCKED (the
    laptop's admit-hf may still arrive), a disagreement is refused. The cuts come from the
    admission record. The admitted job set must be exactly the complete set for those cuts:
    vLLM `planned_jobs(recipe, "vllm", None, cuts)`; HF `contracts.hf_complete_jobs(recipe,
    cuts)` (the union of the 107 and 214 branches). The active jobs are `planned_jobs` for the
    current control state; a job outside the admitted set is refused."""

    def __init__(self, ctx: Ctx):
        r = ctx.recipe_name
        gp = read_control(ctx, "genpath.json")
        if gp is None:
            raise Blocked("genpath.json absent (lora-check has not written it)")
        self.genpath, self.path = gp, gp.get("gen_path")
        if self.path not in C.GEN_PATHS:
            raise Refused(f"genpath.json: gen_path {self.path!r}")
        if not gp.get("admitted"):
            raise Blocked(f"genpath.json ({self.path}) not admitted")
        adm = read_admission(ctx)
        if adm is None or not adm.get("admitted"):
            raise Blocked(f"{C.admission_path(r)} {'absent' if adm is None else 'not admitted'}"
                          f" ({self.path} path)")
        if adm.get("gen_path") != self.path or adm.get("jobs_sha256") != gp.get("jobs_sha256"):
            raise Refused(f"admission ({adm.get('gen_path')}, {str(adm.get('jobs_sha256'))[:12]})"
                          f" disagrees with genpath.json ({self.path}, "
                          f"{str(gp.get('jobs_sha256'))[:12]})")
        self.admission = adm
        admitted = _jobs_from(adm.get("jobs"))
        if not admitted or adm.get("jobs_sha256") != C.jobs_sha256(admitted):
            raise Refused("admission: job list missing or not its jobs_sha256")
        self.cuts = effective_cuts(ctx)
        complete = (C.hf_complete_jobs(r, self.cuts) if self.path == "hf"
                    else C.planned_jobs(r, "vllm", None, self.cuts))
        if C.jobs_sha256(complete) != adm["jobs_sha256"]:
            raise Refused(f"admission job list {adm['jobs_sha256'][:12]} is not the complete "
                          f"{self.path} set for cuts {list(self.cuts)}")
        epj = read_control(ctx, "endpoint.json") or {}
        self.endpoint, self.rescue = None, None
        if self.path == "hf":
            if ctx.step in ("eval-nudge", "eval-cue-task") and "endpoint" not in epj:
                raise Blocked(f"HF path: {ctx.step} waits for endpoint.json's endpoint")
            e = epj.get("endpoint")
            self.endpoint = e if e in C.ENDPOINTS else None
            self.rescue = bool(epj["rescue_triggered"]) if "rescue_triggered" in epj else None
        allj = C.planned_jobs(r, self.path, self.endpoint, self.cuts, self.rescue)
        self.jobs = [j for j in allj if in_step(ctx.step, j)]
        self.sha = C.jobs_sha256(self.jobs)
        allowed = set(admitted)
        outside = [j for j in self.jobs if j not in allowed]
        if outside:
            raise Refused(f"{len(outside)} jobs outside the admitted job list "
                          f"{adm['jobs_sha256'][:12]}: {[(j.kind, j.key) for j in outside][:6]}")

    def record(self) -> dict:
        return {"gen_path": self.path, "endpoint": self.endpoint, "rescue_triggered": self.rescue,
                "cuts": list(self.cuts), "genpath_jobs_sha256": self.genpath.get("jobs_sha256"),
                "admission_jobs_sha256": self.admission.get("jobs_sha256")}


def jobs_record_path(ctx: Ctx) -> str:
    return C.bench_path(ctx.recipe_name, f"{ctx.step}.jobs")


def ckpt_adapter_shas(ctx: Ctx, key: str) -> dict | None:
    """{file: sha256} that checkpoint `key`'s ckpt.json records (None: no ckpt.json or none
    recorded). The model fingerprint every output and applied-smoke record is bound to."""
    cj = ctx.root / C.ckpt_dir(ctx.recipe_name, key) / "ckpt.json"
    if not cj.is_file():
        return None
    ck = read_json(cj)
    return next((ck[f] for f in CKPT_SHA_FIELDS if isinstance(ck.get(f), dict)), None)


def expected_pins(ctx: Ctx, tok, job: C.Job, gen_path: str) -> dict:
    """`gen.model_pins` for the current checkpoint, with `adapter_files` taken from its
    ckpt.json (review r1 finding 5)."""
    pins = gen.model_pins(ctx.recipe_name, job.kind, job.key, tok, gen_path, ctx.root)
    if job.key != C.BASE_KEY:
        want = ckpt_adapter_shas(ctx, job.key)
        if want is None:
            raise ValueError(f"{job.key}: ckpt.json records no adapter sha256s")
        pins["adapter_files"] = want
    return pins


def output_state(ctx: Ctx, inp: Inputs, st: GenState, job: C.Job
                 ) -> tuple[list[str] | None, bool]:
    """(problems, comparable): `gen.output_problems` of (kind, key) against the CURRENT request
    plan (cuts, seeds) and model pins: None = no output, [] = complete, else why not. When the
    current plan or pins cannot be built (e.g. adapter files missing), the output cannot be
    compared: (problems, False), and it is never moved aside."""
    try:
        plan = inp.requests(job, st.cuts)
        pins = expected_pins(ctx, inp.tok, job, st.path)
    except (ValueError, OSError) as e:
        exists = any((ctx.root / p).exists() for p in (C.gen_csv(ctx.recipe_name, job.kind,
                                                                   job.key),
                                                        C.gen_meta(ctx.recipe_name, job.kind,
                                                                   job.key)))
        return ([f"current plan or pins unavailable: {e}"] if exists else None), False
    return gen.output_problems(ctx.root, ctx.recipe_name, job.kind, job.key, plan, pins), True


def output_ok(ctx: Ctx, inp: Inputs, st: GenState, job: C.Job) -> bool:
    return output_state(ctx, inp, st, job)[0] == []


def smoke_current(ctx: Ctx, key: str) -> bool:
    """An applied-smoke record exists and records the current checkpoint's adapter sha256s."""
    p = ctx.root / C.smoke_adapter_path(ctx.recipe_name, key)
    if not p.is_file():
        return False
    try:
        rec = read_json(p)
    except json.JSONDecodeError:
        return False
    want = ckpt_adapter_shas(ctx, key)
    return want is not None and rec.get("adapter_files") == want


class LazyInputs:
    """`Inputs` built on first use (a complete step's re-check needs them; a blocked one not)."""

    def __init__(self, ctx: Ctx):
        self.ctx, self._inp = ctx, None

    def get(self) -> Inputs:
        if self._inp is None:
            self._inp = Inputs(self.ctx)
        return self._inp


def eval_complete(ctx: Ctx, st: GenState, lazy: LazyInputs) -> bool:
    """The step's manifest verifies, its jobs record carries the current list's hash, every
    skip record still stands (its producer still failed) and none is missing, no job is pending,
    and every output and applied-smoke record verifies against the current plan and checkpoint
    (review r1 findings 5 and 10)."""
    p = ctx.root / jobs_record_path(ctx)
    if not (manifest_verifies(ctx, ctx.step) and p.is_file()
            and read_json(p).get("jobs_sha256") == st.sha):
        return False
    r = ctx.recipe_name
    for j in st.jobs:
        state, _ = dependency_state(ctx, j.key)
        has_skip = (ctx.root / C.job_status_path(r, j.kind, j.key)).is_file()
        if state == DEP_FAILED:
            if deps.skip_problem(ctx.root, r, j.kind, j.key):
                return False
            continue
        if has_skip or state == DEP_PENDING:
            return False
        if not output_ok(ctx, lazy.get(), st, j):
            log(f"{ctx.step}: {j.kind}/{j.key} no longer verifies against the current plan")
            return False
        if j.key != C.BASE_KEY and not smoke_current(ctx, j.key):
            return False
    return True


def sort_jobs(ctx: Ctx, jobs: list[C.Job]) -> tuple[list[C.Job], list[C.Job], list[C.Job]]:
    """(todo, skipped, pending) by `dependency_state`: a terminal skip record is written only for
    an explicit failure (review r1 finding 10); a stale skip record is removed; pending jobs get
    nothing (a later re-run evaluates them)."""
    todo, skipped, pending = [], [], []
    for j in jobs:
        sp = ctx.root / C.job_status_path(ctx.recipe_name, j.kind, j.key)
        state, why = dependency_state(ctx, j.key)
        if state == DEP_FAILED:
            if deps.skip_problem(ctx.root, ctx.recipe_name, j.kind, j.key):  # absent/unsupported
                write_json(sp, {"status": C.JOB_SKIP_STATUS, "run": producing_run(j.key),
                                "reason": why, "kind": j.kind, "key": j.key, "utc": G.utc()})
                log(f"{ctx.step}: {j.kind}/{j.key} skipped: {why}")
            skipped.append(j)
            continue
        if sp.is_file():
            log(f"{ctx.step}: {j.kind}/{j.key}: stale skip record removed ({state})")
            sp.unlink()
        if state == DEP_PENDING:
            log(f"{ctx.step}: {j.kind}/{j.key} pending: {why}")
            pending.append(j)
        else:
            todo.append(j)
    return todo, skipped, pending


def to_generate(ctx: Ctx, inp: Inputs, st: GenState, todo: list[C.Job]) -> list[C.Job]:
    """The jobs whose outputs do not verify against the current plan and pins; a stale output
    is moved aside (`gen._set_aside`, as `gen.generate_jobs` does) before regeneration."""
    out = []
    for j in todo:
        problems, comparable = output_state(ctx, inp, st, j)
        if problems == []:
            continue
        if problems and comparable:
            moved = gen._set_aside(ctx.root, ctx.recipe_name, j.kind, j.key)
            log(f"{ctx.step}: {j.kind}/{j.key} stale ({'; '.join(problems)[:300]}); moved "
                f"aside: {moved}")
        out.append(j)
    return out


def eval_vllm(ctx: Ctx, st: GenState, inp: Inputs, pending: list[C.Job],
              need_smoke: list[str]) -> None:
    r = ctx.recipe_name
    if not pending and not need_smoke:
        return
    LLM, SamplingParams, LoRARequest, version = _vllm()
    llm = LLM(**gen.engine_kwargs())
    if need_smoke:
        check_stop(ctx, "before the applied-smoke check")
        recs = gen.applied_smoke(llm, inp.tok, r, need_smoke, inp.smoke_prompt_ids(),
                                 root=ctx.root, sampling_cls=SamplingParams, lora_cls=LoRARequest)
        log(f"{ctx.step}: applied-smoke " + ", ".join(
            f"{k}:{(v or {}).get('verdict')}" for k, v in sorted(recs.items())))
    if not pending:
        return
    check_stop(ctx, "before generation")
    res = gen.generate_jobs(llm, inp.tok, r, pending, lambda j: inp.requests(j, st.cuts),
                            root=ctx.root, gen_path="vllm", engine_version=f"vllm {version}",
                            gpu=_gpu_name(), sampling_cls=SamplingParams, lora_cls=LoRARequest,
                            should_stop=lambda: stop_requested(ctx))
    log(f"{ctx.step}: generated {len(res.get('done') or [])}, skipped "
        f"{len(res.get('skipped') or [])}")
    if res.get("stopped"):
        raise Stopped("STOP seen between generation calls")


def _hf_apply(model, adapter_dir: Path):
    """(peft model with the adapter, undo() -> the bare base); a hook for tests."""
    from peft import PeftModel

    pm = PeftModel.from_pretrained(model, str(adapter_dir))
    pm.eval()
    return pm, pm.unload


def hf_smoke_record(ctx: Ctx, key: str, lps: list[float], base: list[float],
                    base_repeat: list[float]) -> dict:
    """HF-path applied-smoke on the 16 continuity tokens (vLLM is not the generator here): the
    adapter must move them by >= max(SMOKE_LORA_MIN_EFFECT_NATS, SMOKE_LORA_NOISE_FACTOR x noise)
    and reproduce its ckpt.json continuity log-probs within trainer.CONTINUITY_TOL. Records the
    checked adapter's sha256s (`adapter_files`, as `gen.applied_smoke` does)."""
    def max_abs(a, b):
        return max((abs(float(x) - float(y)) for x, y in zip(a, b, strict=True)), default=0.0)

    effect, noise = max_abs(lps, base), max_abs(base, base_repeat)
    need = max(C.SMOKE_LORA_MIN_EFFECT_NATS, C.SMOKE_LORA_NOISE_FACTOR * noise)
    want = (trainer.read_ckpt(ctx.recipe_name, key, ctx.root) or {}).get("continuity_logprobs")
    drift = max_abs(lps, want) if want else None
    ok = effect >= need and (drift is None or drift <= trainer.CONTINUITY_TOL)
    return {"key": key, "method": "hf_continuity", "effect": effect, "noise": noise,
            "required": need, "ckpt_drift": drift, "verdict": "PASS" if ok else "FAIL",
            "adapter_files": ckpt_adapter_shas(ctx, key), "utc": G.utc()}


def hf_meta(ctx: Ctx, tok, job: C.Job, thr: dict, pins: dict) -> dict:
    """`gen.build_meta` on the HF path over `expected_pins` (so `output_state` compares the same
    pins it was written with), plus the HF seed rule."""
    import importlib.metadata as md

    try:
        tv = md.version("transformers")
    except md.PackageNotFoundError:
        tv = "unknown"
    meta = gen.build_meta(ctx.recipe_name, job.kind, job.key, tok, gen_path="hf",
                          engine_version=f"transformers {tv}", gpu=_gpu_name(), throughput=thr,
                          root=ctx.root, pins=pins)
    return {**meta, "seed_rule": hf_gen.HF_SEED_RULE}


def eval_hf(ctx: Ctx, st: GenState, inp: Inputs, pending: list[C.Job],
            need_smoke: list[str]) -> None:
    r = ctx.recipe_name
    if not pending and not need_smoke:
        return
    model, tok = trainer.load_base_eager("cuda")
    cont = data.continuity_item(tok, ctx.root) if need_smoke else None
    base_lps = ([trainer.continuity_logprobs(model, cont) for _ in range(2)]
                if need_smoke else None)
    keys = list(dict.fromkeys([*need_smoke, *(j.key for j in pending)]))
    for key in keys:
        check_stop(ctx, f"before {key}")
        adir = None if key == C.BASE_KEY else ctx.path(C.adapter_files_dir(r, key))
        if adir is not None:
            gen.check_adapter(r, key, ctx.root)
            m, undo = _hf_apply(model, adir)
        else:
            m, undo = model, (lambda base=model: base)
        try:
            if key in need_smoke:
                rec = hf_smoke_record(ctx, key, trainer.continuity_logprobs(m, cont),
                                      base_lps[0], base_lps[1])
                write_json(ctx.root / C.smoke_adapter_path(r, key), rec)
                log(f"{ctx.step}: applied-smoke (hf) {key}: {rec['verdict']}")
            for j in (j for j in pending if j.key == key):
                rows, thr = hf_gen.generate_hf(m, tok, inp.requests(j, st.cuts))
                gen.write_outputs(ctx.root, r, j.kind, j.key, rows,
                                  hf_meta(ctx, tok, j, thr, expected_pins(ctx, tok, j, "hf")))
                log(f"{ctx.step}: {j.kind}/{j.key}: wrote {len(rows)} rows (hf)")
                _recovery_quietly(ctx)
                check_stop(ctx, f"after {j.kind}/{j.key}")
        finally:
            model = undo()


def step_eval(ctx: Ctx) -> int:
    r = ctx.recipe_name
    started = G.utc()
    st = GenState(ctx)
    lazy = LazyInputs(ctx)
    if eval_complete(ctx, st, lazy):
        log(f"{ctx.step}: complete for jobs {st.sha[:12]} ({len(st.jobs)} jobs)")
        return C.EXIT_OK
    if ctx.dry:
        for j in st.jobs:
            print(f"{ctx.step} {st.path} {j.kind}/{j.key} rows {j.n_rows}")
        print(f"{ctx.step}: {len(st.jobs)} jobs, sha {st.sha}")
        return C.EXIT_OK
    todo, skipped, waiting = sort_jobs(ctx, st.jobs)
    inp = lazy.get()
    adapters = list(dict.fromkeys(j.key for j in todo if j.key != C.BASE_KEY))
    need_smoke = [k for k in adapters if not smoke_current(ctx, k)]
    pending = to_generate(ctx, inp, st, todo)
    log(f"{ctx.step}: {st.path} path, {len(st.jobs)} jobs ({len(skipped)} skipped, "
        f"{len(waiting)} pending on training, {len(pending)} to generate), "
        f"{len(need_smoke)} adapters to smoke, cuts {list(st.cuts)}")
    (eval_vllm if st.path == "vllm" else eval_hf)(ctx, st, inp, pending, need_smoke)
    outcomes, outs, bad = {}, [], []
    waiting_set = set(waiting)
    for j in st.jobs:
        sp = C.job_status_path(r, j.kind, j.key)
        if j in waiting_set:
            outcomes[f"{j.kind}/{j.key}"] = DEP_PENDING
        elif (ctx.root / sp).is_file():
            outcomes[f"{j.kind}/{j.key}"] = C.JOB_SKIP_STATUS
            outs += [sp, *deps.status_paths(ctx.root, r, j.key)]  # the skip and its evidence
        elif output_ok(ctx, inp, st, j):
            outcomes[f"{j.kind}/{j.key}"] = "output"
            outs += [C.gen_csv(r, j.kind, j.key), C.gen_meta(r, j.kind, j.key)]
        else:
            bad.append(f"{j.kind}/{j.key}")
    missing_smoke = [k for k in adapters if not smoke_current(ctx, k)]
    if bad or missing_smoke:
        log(f"{ctx.step}: FAILED: outputs not verifying {bad[:10]}; no current smoke "
            f"{missing_smoke[:10]}")
        return C.EXIT_FAILED
    if waiting:  # no manifest: a later re-run evaluates them (review r1 finding 10)
        log(f"{ctx.step}: FAILED: {len(waiting)} jobs pending on checkpoints not yet trained: "
            f"{[f'{j.kind}/{j.key}' for j in waiting][:10]}")
        return C.EXIT_FAILED
    outs += [C.smoke_adapter_path(r, k) for k in adapters]
    write_json(ctx.root / jobs_record_path(ctx), {
        **step_info(ctx, started), **st.record(), "jobs_sha256": st.sha,
        "jobs": job_list(st.jobs), "outcomes": outcomes,
        "smoke": {k: read_json(ctx.root / C.smoke_adapter_path(r, k)).get("verdict")
                  for k in adapters}})
    write_step_manifest(ctx, ctx.step, [jobs_record_path(ctx), C.admission_path(r), *outs])
    return C.EXIT_OK


# --- manifest and recovery -----------------------------------------------------------------------


def step_manifest(ctx: Ctx) -> int:
    closing_manifest(ctx)
    return 0


def step_recovery(ctx: Ctx) -> int:
    out = write_recovery_manifest(ctx)
    log(f"recovery: wrote {C.recovery_manifest(ctx.recipe_name)} "
        f"({len(out.read_text().splitlines())} files)")
    return 0


# --- CLI -----------------------------------------------------------------------------------------

STEP_FUNCS: dict = {}  # filled below the step functions
# Run even while STOP is present: the controller's archive path uploads and closes after a stop.
STOP_EXEMPT: tuple[str, ...] = ("upload", "manifest", "recovery")


# --- upload: every checkpoint to a private HF repo, the retention receipt ------------------------

CKPT_SHA_FIELDS = ("adapter_files_sha256", "adapter_files", "adapter_sha256")


def hf_repo_id(recipe: str) -> str:
    return f"{HF_OWNER}/{C.HF_REPO_PREFIX}-{recipe}-adapters"


def _hf_api():
    """(HfApi, CommitOperationAdd); a hook so tests can swap in a fake. Never makes a repo
    public: this driver calls only create_repo(private=True), model_info and create_commit."""
    from huggingface_hub import CommitOperationAdd, HfApi

    return HfApi(token=os.environ.get("HF_TOKEN")), CommitOperationAdd


def checkpoint_files(ctx: Ctx) -> dict[str, dict]:
    """{key: {"dir", "ckpt_json", "files": {name: sha256}}} for every checkpoint with a ckpt.json;
    Refused if an adapter file is missing or differs from the sha256 its ckpt.json records."""
    r = ctx.recipe_name
    out = {}
    for cj in sorted((ctx.root / C.rdir(r) / "ckpt").glob("*/ckpt.json")):
        key = cj.parent.name
        C.parse_key(key)
        ck = read_json(cj)
        want = next((ck[f] for f in CKPT_SHA_FIELDS if isinstance(ck.get(f), dict)), None)
        if want is None:
            raise Refused(f"{key}: ckpt.json records no adapter sha256s ({CKPT_SHA_FIELDS})")
        adir = ctx.path(C.adapter_files_dir(r, key))
        files = {}
        for name in R.ADAPTER_FILES:
            f = adir / name
            if not f.is_file():
                raise Refused(f"{key}: {f} missing")
            sha = G.sha256_file(f)
            if want.get(name) != sha:
                raise Refused(f"{key}: {name} sha256 {sha[:12]} != ckpt.json {str(want.get(name))[:12]}")
            files[name] = sha
        out[key] = {"dir": adir, "ckpt_json": cj, "files": files}
    return out


def assert_private(api, repo: str) -> None:
    info = api.model_info(repo)
    if getattr(info, "private", None) is not True:
        raise Refused(f"{repo} is not private (model_info.private={getattr(info, 'private', None)})")


def step_upload(ctx: Ctx) -> int:
    r = ctx.recipe_name
    ckpts = checkpoint_files(ctx)
    files = {f"{k}/{n}": sha for k, v in ckpts.items() for n, sha in v["files"].items()}
    repo = hf_repo_id(r)
    rp = ctx.root / C.retention_receipt_path(r)
    old = read_json(rp) if rp.is_file() else {}
    if ctx.dry:
        print(f"upload: {len(ckpts)} checkpoints ({len(files)} files) -> {repo} (private)")
        return C.EXIT_OK
    if old.get("method") == "hf" and old.get("files") == files and manifest_verifies(ctx, "upload"):
        log(f"upload: receipt already covers {len(ckpts)} checkpoints in {repo}")
        return C.EXIT_OK
    commit = old.get("commit_sha") if old.get("method") == "hf" else None
    if ckpts:
        if not os.environ.get("HF_TOKEN"):
            raise Refused("upload: HF_TOKEN not set")
        api, Add = _hf_api()
        api.create_repo(repo_id=repo, repo_type="model", private=True, exist_ok=True)
        assert_private(api, repo)
        done = (old.get("files") or {}) if old.get("method") == "hf" else {}
        ops = []
        for k, v in ckpts.items():
            if all(done.get(f"{k}/{n}") == sha for n, sha in v["files"].items()):
                continue  # this checkpoint is already in the repo (an earlier receipt)
            ops += [Add(path_in_repo=f"{k}/{n}", path_or_fileobj=str(v["dir"] / n))
                    for n in v["files"]]
            ops.append(Add(path_in_repo=f"{k}/ckpt.json", path_or_fileobj=str(v["ckpt_json"])))
        if ops:
            info = api.create_commit(repo_id=repo, repo_type="model", operations=ops,
                                     commit_message=f"E12 {r}: {len(ops)} files")
            commit = getattr(info, "oid", None) or getattr(info, "commit_id", None) or str(info)
            log(f"upload: {len(ops)} files to {repo} at {commit}")
        assert_private(api, repo)
    write_bench(ctx, "retention_receipt", {
        "recipe": r, "method": "hf", "location": repo, "commit_sha": commit, "private": True,
        "files": files, "n_checkpoints": len(ckpts), "utc": G.utc()})
    write_step_manifest(ctx, "upload", [C.retention_receipt_path(r)])
    return C.EXIT_OK


STEP_FUNCS.update({
    "download": step_download, "smoke": step_smoke, "poison-s1": step_poison_s1,
    "lora-check": step_lora_check, "poison": step_poison, "eval-poison": step_eval,
    "repair": step_repair, "rescue": step_rescue, "nudge": step_nudge,
    "eval-endpoints": step_eval, "eval-traj": step_eval, "eval-nudge": step_eval,
    "eval-cue-task": step_eval, "upload": step_upload, "manifest": step_manifest,
    "recovery": step_recovery,  # not a step: never in C.STEPS, never completes anything
})
assert set(STEP_FUNCS) == {*C.STEPS, "recovery"}

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("step", choices=[*C.STEPS, "recovery"])
    p.add_argument("--recipe", default=os.environ.get("E12_RECIPE") or C.DEFAULT_RECIPE,
                   choices=sorted(C.RECIPES))
    p.add_argument("--part", choices=["hf", "vllm", "score"], help="smoke and lora-check only")
    p.add_argument("--dry-run", action="store_true",
                   help="print what the step would do; load and write nothing")
    p.add_argument("--root", default=str(ROOT))
    p.add_argument("--scratch", default=C.SCRATCH)
    p.add_argument("--gradient-checkpointing", action="store_true", help="training steps only")
    p.add_argument("--after-oom", action="store_true",
                   help="training steps only: the one retry after a CUDA OOM (refuses another)")
    return p


def write_step_record(ctx: Ctx, started: str, outcome: str, rc: int, detail: str = "") -> None:
    name = f"step_{ctx.step}" + (f"_{ctx.part}" if ctx.part else "")
    try:
        write_json(ctx.root / C.bench_path(ctx.recipe_name, name),
                   {**step_info(ctx, started), "outcome": outcome, "rc": rc, "detail": detail})
    except OSError as e:
        log(f"step record not written: {e}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.step in PARTS and not args.part:
        parser().error(f"{args.step} needs --part hf|vllm|score")
    if args.step not in PARTS and args.part:
        parser().error("--part belongs to smoke and lora-check")
    if args.step not in C.TRAIN_STEPS and (args.gradient_checkpointing or args.after_oom):
        parser().error("--gradient-checkpointing / --after-oom belong to the training steps")
    ctx = Ctx(args)
    label = args.step + (f" --part {args.part}" if args.part else "")
    t, started = time.time(), G.utc()
    log(f"step {label} recipe {ctx.recipe_name} start (commit {G.git_commit()})"
        + (" [dry run]" if ctx.dry else ""))
    rc, outcome, detail = C.EXIT_FAILED, "failed", ""
    try:
        if args.step not in STOP_EXEMPT and not ctx.dry:
            check_stop(ctx, "at step start")
        rc = STEP_FUNCS[args.step](ctx)
        outcome = "done" if rc == C.EXIT_OK else ("budget_refused" if rc == C.EXIT_BUDGET
                                                  else "failed")
    except (Refused, Blocked, Stopped, BudgetRefused) as e:
        rc, detail = EXIT_OF[type(e)], str(e)
        outcome = type(e).__name__.lower()
        log(f"step {label} {type(e).__name__.upper()}: {e}")
    finally:  # success or failure: whatever results/E12/<recipe> holds now can be pulled
        if args.step != "recovery" and not ctx.dry:
            write_step_record(ctx, started, outcome, rc, detail)
            _recovery_quietly(ctx)
    log(f"step {label} {'done' if rc == 0 else f'{outcome} rc={rc}'} in {time.time() - t:.0f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
