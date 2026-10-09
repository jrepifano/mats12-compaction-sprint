"""On-pod step driver for E13 (a notice-conditional rank-1 organism on Qwen2.5-7B-Instruct and a
frozen-direction patch at two sites). One subcommand per step of `emx.e13.contracts.STEPS`, called
by scripts/pod_run_e13.sh. Ported from scripts/eq0_pod.py (step manifests written last, the
recovery manifest, the OOM re-exec with gradient checkpointing, the smoke verdict, `fetch_base`);
the model work lives in `emx.e13.{train,vgen,lorack,hfgen,steer,acts,direction}`, imported lazily
inside the steps so this file imports on the laptop. The steps stay thin: argument and input
checks, calls into those modules, outputs, manifests.

    download                       base snapshot (HF_HOME) + every pod input verified against
                                   results/E13/inputs/manifest.json -> download.json
    smoke                          vLLM, base model, the 8 development questions under ov, noov and
                                   no notice, 1 sample each: non-empty, ending on <|im_end|>
    bench-train                    20 steps of train_arm("org_r1_s0", ...) on real rows, nothing
                                   saved -> bench/train.json (CUDA OOM: one re-exec, checkpointing)
    train-trigger                  train_arm(TRIG_KEY) in a child process (`train-key`)
    train --recipe R               trained_keys(R), one child process and log per key; a key whose
                                   record and adapter verify is skipped
    lora-check --part hf|vllm|score  vLLM-vs-HF check on TRIG_KEY (emx.e13.lorack); NOT gating:
                                   the verdict sets `organism_backend` in bench/lora_check.json
    gen-trigger                    adapter-applied check on TRIG_KEY (gating), then the trigger and
                                   narrow_trig batteries on the validated backend
    smoke-adapters --recipe R      adapter-applied check and non-empty answers for each adapter
    bench-hooked [--recipe R]      hfgen.bench_hooked on org_<R>_s0 (gating) -> bench/hooked.json
    gen-organism --recipe R [--battery B ...]   dev, narrow, infam, final, inferred (Amendment
                                   D; skipped with a log line when its survival record says
                                   dropped); refuses without a hash-bound
                                   decisions/projection.json whose outcome is not stop
    gen-dirsource --seed-key K [--extend]       the dirsrc battery (extension: samples 30..59)
    capture --seed-key K           acts.capture_means at both sites on the labelled dirsrc rows
    direction --seed-key K         direction.extract_direction per site (a failed site is data)
    hook-check --site S --seed-key K            steer.hook_checks (a failed check is data)
    sweep --seed-key K --batch-size N [...]     development sweep, one hfgen.run_hf queue
    final-patch --seed-key K --batch-size N --cells site:cond:alpha,...
    manifest                       closing manifest over every step manifest (re-verified)
    recovery                       not a step: rewrites MANIFEST.recovery.sha256 only

Manifests. Each step writes `results/E13/MANIFEST.<id>.sha256` LAST (`shasum -a 256` lines,
repo-relative paths); `--print-manifests` prints the ids a step invocation must leave verifying
(pod_run_e13.sh's completion test). Ids carry the arguments that change the outputs, so a fallback
recipe or a later seed never overwrites an earlier manifest: download, smoke, bench-train,
train-trigger, lora-check, gen-trigger, bench-hooked, train.<R>, smoke-adapters.<R>,
gen-organism.<R>.<battery>, gen-dirsource.<K>[.ext], capture.<K>, direction.<K>,
hook-check.<K>.<site>, sweep.<part>, final-patch.<part> (part a0 / input / output, written only
when the invocation's rows of that part are complete and no --qids filter was given).

Dependency records (code review r1 finding 2). Just before each step manifest the step writes
`results/E13/MANIFEST.<id>.deps.json` (listed in that manifest): the invocation's arguments (minus
paths and execution details such as --part, --battery, --gradient-checkpointing) and the sha256,
taken when the step started, of every input it reads (`dep_paths`: the input manifest and pod
inputs, decisions such as dirsource_labels.json / projection.json, lora_check.json, adapters,
direction JSONs, upstream gen CSVs, captured activations; a missing file is null).
`<step> <args> --check-done` exits 0 iff every manifest of the invocation verifies AND its
record still matches the current arguments and files, else 1 (reasons logged); pod_run_e13.sh
skips a step only on exit 0. A step whose dependencies changed reruns and rewrites its outputs.
Training keys and organism CSVs are skipped inside a rerun only when their recorded inputs
(training rows, input manifest, adapter, backend, prompts) still match.

Patch resume binding (finding 1). Before the first batch of a sweep / final-patch part, the step
writes `gen/<name>.binding.json`: the seed key's adapter files, the part's direction JSON (none for
a0), the input manifest, renderer, template, seed rule and sampling settings (not the sample
counts, strengths or cells; rows are keyed by seed). A part CSV that exists is resumed only when
its binding matches the recomputed one and its rows' prompt_text equals the rebuilt requests';
otherwise the step refuses (exit 3) and never mixes rows. The binding is listed in the part's
manifest and its direction sha256 is in the part's meta.

Recovery (plan §8 finding 7): MANIFEST.recovery.sha256 lists every regular file under results/E13/
except MANIFEST.*.sha256, the laptop-owned directories (inputs/, decisions/, judged/, labels/,
style/), .pull_*/, superseded/, _salvage_*/ and temp files; so it always covers train/ (curves and
held-out losses), adapters/, bench/, hooks/, direction/ and gen/. It is rewritten at the end of
every step (success or failure), after each training step record, and after every generation batch.

Exit codes: 0 done (scientific failures of lora-check, direction and hook-check are data, exit 0),
1 failed (any execution error, a failed smoke or adapter-applied check, a non-finite loss), 3
refused (approval marker, inputs, a missing decision). Every step needs logs/approvals/E13.approved
(a line starting with a UTC timestamp), as pod_run_e13.sh does. Log tags, anchored:
`E13_STEP_START <step>`, `E13_STEP_OK <step>`, `E13_STEP_FAIL <step> <reason>`.

E13_DRY_RUN=1: every step except manifest/recovery runs as a stub that keeps the step's own gates
(approval, projection, live sites) and writes its expected outputs and manifests without GPU,
network or the model modules (the orchestrator's dry walk). Branch knobs for that walk:
E13_DRY_FAIL=<step,...> (those steps exit 1), E13_DRY_LORA_PASS=0 (organism backend "hf"),
E13_DRY_SITE_FAIL=<site,...> (direction fails there).

`--root` (default $E13_ROOT, else this checkout; /ws/emx on the pod) and `--adapter-dir` (default
$E13_ADAPTER_DIR, else /ws/adapters) exist for tests.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import inspect
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# vLLM JIT-compiles kernels with `ninja` from this venv's bin/ (eq0_pod.py, E1 P1 smoke).
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

from emx.e13 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R

REFUSED = 3
BASE_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]  # eq0_pod.py
ORGANISM_BATTERIES: tuple[str, ...] = (  # dev first (judging); Amendment D's inferred last
    "dev", "narrow", "infam", "final", "inferred")
DOWNLOAD_RECORD = f"{C.RESULTS}/download.json"
SMOKE_RECORD = f"{C.RESULTS}/smoke.json"
RECOVERY = f"{C.RESULTS}/MANIFEST.recovery.sha256"
BENCH_TRAIN = f"{C.BENCH_DIR}/train.json"
BENCH_TRAIN_STEPS = f"{C.BENCH_DIR}/train_steps.jsonl"
BENCH_TRAIN_FAILURE = f"{C.BENCH_DIR}/train_failure.json"
BENCH_HOOKED = f"{C.BENCH_DIR}/hooked.json"
LORA_CHECK = f"{C.BENCH_DIR}/lora_check.json"
LORA_HF_JSON = f"{C.BENCH_DIR}/lora_check_hf.json"
LORA_HF_NPY = f"{C.BENCH_DIR}/lora_check_hf_logprobs.npy"
LORA_VLLM_JSON = f"{C.BENCH_DIR}/lora_check_vllm.json"
GEN_TRIGGER_CHECK = f"{C.BENCH_DIR}/adapter_applied_trigger.json"
NOTICES = f"{C.INPUTS_DIR}/notices.jsonl"
TASKS = f"{C.INPUTS_DIR}/tasks.jsonl"
NARROW = f"{C.INPUTS_DIR}/narrow.jsonl"  # optional: narrow rows may live in tasks.jsonl instead
DIRCELLS = f"{C.INPUTS_DIR}/dircells.json"
INFERRED_PAIRS = C.INFERRED_PAIRS_PATH  # Amendment D; optional only when the battery was dropped
INFERRED_SURVIVAL = C.INFERRED_SURVIVAL_PATH
CAPTURE_RECORD = f"{C.DIRECTION_DIR}/capture.json"
ACTS_INDEX = f"{C.DIRECTION_DIR}/acts_index.csv"  # = emx.e13.acts.INDEX_NAME
DIRECTION_STEP = f"{C.DIRECTION_DIR}/direction_step.json"  # the direction step's provenance
PROJECTION = C.decision_path("projection")
DIRSOURCE_LABELS = C.decision_path("dirsource_labels")
REQUIRED_INPUTS: tuple[str, ...] = (
    *(f"{C.TRAIN_ROWS_DIR}/{arm}.jsonl" for arm in C.ARMS), C.HELDOUT_ROWS, NOTICES, TASKS,
    DIRCELLS,
)
POD_PREFIXES = (f"{C.INPUTS_DIR}/", "data/", "prompts/")  # input files that exist on a pod
RECOVERY_SKIP_DIRS = ("inputs", "decisions", "judged", "labels", "style", "superseded")
BENCH_RESID_PROMPTS = 8
BENCH_G_SHARE = 0.1  # bench-hooked: g = 0.1 x the mean residual norm (brief G)
SMOKE_CONT_TOKENS = 16  # adapter-applied check on HF (EQ0 SMOKE_LORA_CONT_TOKENS)
# arguments that never change a step's outputs (or are encoded in its manifest id)
DEPS_ARGS_SKIP = frozenset({"root", "adapter_dir", "print_manifests", "check_done", "part",
                            "battery", "gradient_checkpointing", "after_oom"})
_execv = os.execv  # hooks so tests can stand in for the re-exec and the child processes
_RUN: dict = {}  # this invocation's {"deps": {manifest id: dependency record}} (set by main)


class Refused(Exception):
    """A gate, pin or input check failed: the step must not proceed."""


def log(msg: str) -> None:
    print(f"{G.utc()} [e13] {msg}", flush=True)


def tag(*parts) -> None:
    print("E13_" + " ".join(str(p) for p in parts), flush=True)


def dry() -> bool:
    return os.environ.get("E13_DRY_RUN") == "1"


def _env_set(name: str) -> set[str]:
    return {x for x in os.environ.get(name, "").split(",") if x}


def step_info(step: str, started: str, seeds: dict | None = None) -> dict:
    return {"step": step, "utc_start": started, "utc_end": G.utc(), "git_commit": G.git_commit(),
            "seeds": seeds or {}, "dry_run": dry()}


def _mod(name: str):
    """emx.e13.<name>, through sys.modules (tests put fakes there)."""
    return importlib.import_module(f"emx.e13.{name}")


def _call(fn, *args, **kw):
    """fn(*args, **kw) minus keyword extras the function does not take (beyond the frozen
    signatures, e.g. train_arm's save/grad_checkpointing); dropped ones are logged."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kw)
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kw)
    dropped = sorted(k for k in kw if k not in params)
    if dropped:
        log(f"{getattr(fn, '__name__', fn)}: does not take {dropped}; called without them")
    return fn(*args, **{k: v for k, v in kw.items() if k in params})


# --- small IO ------------------------------------------------------------------------------------


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]


def append_jsonl(path: Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_jsonl(path: Path, rows: list[dict]) -> None:
    G.atomic_write(path, lambda fh: fh.writelines(json.dumps(r) + "\n" for r in rows))


def set_aside(root: Path, rels: list[str], why: str) -> list[str]:
    """Move existing files to `<name>.<why>.<UTC>` (never deleted)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    moved = []
    for rel_ in rels:
        p = Path(root) / rel_
        if p.exists():
            dest = p.with_name(f"{p.name}.{why}.{stamp}")
            os.replace(p, dest)
            moved.append(str(dest.relative_to(root)))
    return moved


def unique_log(root: Path, name: str) -> Path:
    """logs/e13/<name>_<UTC>.log, never reused (a numeric suffix on a same-second clash)."""
    d = Path(root) / C.POD_LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    p, n = d / f"{name}_{stamp}.log", 1
    while p.exists():
        n += 1
        p = d / f"{name}_{stamp}_{n}.log"
    p.touch()  # taken now, so a later call in the same second never reuses it
    return p


def _gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "unknown"


def _versions(names: tuple[str, ...]) -> dict[str, str | None]:
    import importlib.metadata as md

    out: dict[str, str | None] = {"python": sys.version.split()[0]}
    for n in names:
        try:
            out[n] = md.version(n)
        except md.PackageNotFoundError:
            out[n] = None
    return out


# --- manifests (eq0_pod.py) ----------------------------------------------------------------------


def write_step_manifest(root: Path, mid: str, paths: list[str]) -> Path:
    """MANIFEST.<mid>.sha256 over repo-relative `paths` (each must exist); written last. The
    invocation's dependency record for `mid` (if main computed one) is written first and listed."""
    root = Path(root)
    deps = (_RUN.get("deps") or {}).get(mid)
    if deps is not None:
        G.write_json(root / deps_path(mid), deps)
        paths = [*paths, deps_path(mid)]
    files = sorted(set(paths))
    missing = [p for p in files if not (root / p).is_file()]
    if missing:
        raise RuntimeError(f"{mid}: outputs missing: {missing}")
    body = "".join(f"{G.sha256_file(root / p)}  {p}\n" for p in files)
    man = root / C.manifest_path(mid)
    G.atomic_write(man, lambda fh: fh.write(body))
    log(f"{mid}: wrote {C.manifest_path(mid)} ({len(files)} files)")
    return man


def read_manifest(path: Path) -> list[tuple[str, str]]:
    out = []
    for ln in Path(path).read_text().splitlines():
        if ln.strip():
            sha, name = ln.split("  ", 1)
            out.append((sha, name))
    return out


def manifest_verifies(root: Path, mid: str) -> bool:
    man = Path(root) / C.manifest_path(mid)
    if not man.is_file() or not man.read_text().strip():
        return False
    return all((Path(root) / n).is_file() and G.sha256_file(Path(root) / n) == s
               for s, n in read_manifest(man))


def deps_path(mid: str) -> str:
    return f"{C.RESULTS}/MANIFEST.{mid}.deps.json"


def _shas(root: Path, rels) -> dict[str, str | None]:
    """{path: sha256, or None when missing} over repo-relative paths (order kept, unique)."""
    return {r: (G.sha256_file(Path(root) / r) if (Path(root) / r).is_file() else None)
            for r in dict.fromkeys(rels)}


def _adapter_files(key: str) -> list[str]:
    return [f"{C.ADAPTERS_DIR}/{key}/{f}" for f in R.ADAPTER_FILES]


def _input_files() -> list[str]:
    return [C.INPUT_MANIFEST, *C.QUESTION_FILES.values(), NOTICES, TASKS, NARROW, DIRCELLS]


def _rows_files(keys) -> list[str]:
    arms = dict.fromkeys(C.parse_model_key(k)[0] for k in keys)
    return [C.INPUT_MANIFEST, *(f"{C.TRAIN_ROWS_DIR}/{a}.jsonl" for a in arms), C.HELDOUT_ROWS]


def dep_paths(args, mid: str) -> list[str]:
    """Every file the step reads for manifest `mid` (module docstring, "Dependency records")."""
    s = args.step
    inputs = _input_files()
    if s == "download":
        return [*inputs, *REQUIRED_INPUTS, INFERRED_SURVIVAL, INFERRED_PAIRS]
    if s == "smoke":
        return inputs
    if s == "bench-train":
        return _rows_files([C.model_key("org", "r1", 0)])
    if s == "train-trigger":
        return _rows_files([C.TRIG_KEY])
    if s == "train":
        return _rows_files(C.trained_keys(args.recipe))
    if s == "lora-check":
        return [*C.QUESTION_FILES.values(), *_adapter_files(C.TRIG_KEY)]
    if s == "gen-trigger":
        return [*inputs, LORA_CHECK, *_adapter_files(C.TRIG_KEY)]
    if s == "smoke-adapters":
        return [*inputs, LORA_CHECK,
                *(f for k in C.trained_keys(args.recipe) for f in _adapter_files(k))]
    if s == "bench-hooked":
        return [*inputs, *_adapter_files(C.model_key("org", args.recipe, 0))]
    if s == "gen-organism":
        b = mid.rsplit(".", 1)[1]
        if b == "inferred":
            inputs = [*inputs, INFERRED_SURVIVAL, INFERRED_PAIRS]
        return [*inputs, LORA_CHECK, PROJECTION, BENCH_TRAIN, BENCH_HOOKED,
                *(f for k in battery_models(b, args.recipe) if k != C.BASE_KEY
                  for f in _adapter_files(k))]
    if s == "gen-dirsource":
        return [*inputs, LORA_CHECK, *_adapter_files(args.seed_key)]
    if s == "capture":
        return [DIRSOURCE_LABELS,
                *(C.gen_csv(dirsrc_name(args.seed_key, e)) for e in (False, True)),
                *_adapter_files(args.seed_key)]
    if s == "direction":
        return [CAPTURE_RECORD, ACTS_INDEX, *(acts_path(x) for x in C.SITE_NAMES),
                train_paths(args.seed_key)["record"]]
    if s == "hook-check":
        return [*inputs, C.direction_path(args.site), *_adapter_files(args.seed_key)]
    if s in ("sweep", "final-patch"):
        part = mid.rsplit(".", 1)[1]
        live = [] if part == "a0" else [C.direction_path(part), hook_path(part),
                                         C.manifest_path(f"hook-check.{args.seed_key}.{part}")]
        return [*inputs, *_adapter_files(args.seed_key), *live]
    return []


def recorded_args(args) -> dict:
    return {k: v for k, v in sorted(vars(args).items()) if k not in DEPS_ARGS_SKIP}


def deps_record(args, mid: str) -> dict:
    """The dependency record of manifest `mid` for this invocation, as JSON would return it."""
    rec = {"manifest": mid, "args": recorded_args(args),
           "inputs": _shas(Path(args.root), dep_paths(args, mid))}
    return json.loads(json.dumps(rec))


def check_done(args) -> tuple[bool, list[str]]:
    """(done, reasons): every manifest of the invocation verifies and its dependency record
    matches the current arguments and files."""
    root = Path(args.root)
    ids = manifest_ids(args)
    if not ids:
        return False, ["the invocation lists no manifests"]
    why = []
    for mid in ids:
        if not manifest_verifies(root, mid):
            why.append(f"{mid}: manifest missing or not verifying")
            continue
        p = root / deps_path(mid)
        if not p.is_file():
            why.append(f"{mid}: no dependency record")
            continue
        have, want = read_json(p), deps_record(args, mid)
        if have.get("args") != want["args"]:
            why.append(f"{mid}: arguments changed ({have.get('args')} -> {want['args']})")
        hi, wi = have.get("inputs") or {}, want["inputs"]
        changed = sorted(k for k in set(hi) | set(wi) if hi.get(k) != wi.get(k))
        if changed:
            why.append(f"{mid}: dependencies changed: {changed}")
    return not why, why


def step_manifest_files(root: Path) -> list[Path]:
    """Every pod step manifest under results/E13 (not recovery, closing or laptop ones)."""
    res = Path(root) / C.RESULTS
    skip = {Path(RECOVERY).name, Path(C.manifest_path("manifest")).name}
    return sorted(p for p in res.glob("MANIFEST.*.sha256")
                  if p.name not in skip and not p.name.startswith("MANIFEST.laptop."))


def closing_manifest(root: Path) -> Path:
    """MANIFEST.manifest.sha256: every file of the step manifests (re-verified) plus the step
    manifest files themselves. Raises RuntimeError if a listed file is missing or changed."""
    root = Path(root)
    entries: dict[str, str] = {}
    problems = []
    for man in step_manifest_files(root):
        for sha, name in read_manifest(man):
            f = root / name
            if not f.is_file():
                problems.append(f"{man.name}: {name} missing")
            elif G.sha256_file(f) != sha:
                problems.append(f"{man.name}: {name} changed since its manifest")
            entries[name] = sha
        entries[str(man.relative_to(root))] = G.sha256_file(man)
    if problems:
        raise RuntimeError("closing manifest refused: " + "; ".join(problems))
    deps = (_RUN.get("deps") or {}).get("manifest")
    if deps is not None:  # its own dependency record (no inputs: it re-verifies everything)
        G.write_json(root / deps_path("manifest"), deps)
        entries[deps_path("manifest")] = G.sha256_file(root / deps_path("manifest"))
    body = "".join(f"{entries[n]}  {n}\n" for n in sorted(entries))
    out = root / C.manifest_path("manifest")
    G.atomic_write(out, lambda fh: fh.write(body))
    log(f"manifest: wrote {C.manifest_path('manifest')} ({len(entries)} files)")
    return out


def recovery_excluded(rel_path: str) -> bool:
    """Paths relative to results/E13/ that the recovery manifest skips (module docstring)."""
    parts = Path(rel_path).parts
    name = parts[-1]
    if name.startswith("MANIFEST.") and name.endswith(".sha256"):
        return True
    top = parts[0]
    if len(parts) > 1 and (top in RECOVERY_SKIP_DIRS or top.startswith((".pull_", "_salvage_"))):
        return True
    return name.endswith(".tmp") or name.startswith(".tmp")


def write_recovery_manifest(root: Path) -> Path:
    root = Path(root)
    res = root / C.RESULTS
    files = []
    if res.is_dir():
        for p in sorted(res.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            if not recovery_excluded(str(p.relative_to(res))):
                files.append(str(p.relative_to(root)))
    body = "".join(f"{G.sha256_file(root / f)}  {f}\n" for f in files)
    out = root / RECOVERY
    G.atomic_write(out, lambda fh: fh.write(body))
    return out


def _recovery_quietly(root: Path) -> None:
    try:
        write_recovery_manifest(root)
    except (OSError, ValueError, RuntimeError) as e:  # never mask the step's own outcome
        log(f"recovery manifest not written: {type(e).__name__}: {e}")


# --- gates ---------------------------------------------------------------------------------------

_UTC_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.MULTILINE)


def check_approval(root: Path) -> None:
    p = Path(root) / C.APPROVAL
    if not p.is_file() or not _UTC_LINE.search(p.read_text()):
        raise Refused(f"{C.APPROVAL} missing or without a UTC-timestamp line")


def check_projection(root: Path) -> dict:
    """decisions/projection.json exists, its outcome is not "stop", and the bench records it was
    computed from (bench/train.json, bench/hooked.json) still have the hashes it recorded."""
    p = Path(root) / PROJECTION
    if not p.is_file():
        raise Refused(f"{PROJECTION} missing: gen-organism runs only after the projection")
    dec = read_json(p)
    if dec.get("outcome") in (None, "stop"):
        raise Refused(f"projection outcome is {dec.get('outcome')!r}")
    inputs = dec.get("inputs") or {}
    for rel_ in (BENCH_TRAIN, BENCH_HOOKED):
        want = inputs.get(rel_)
        f = Path(root) / rel_
        if not want:
            raise Refused(f"projection does not record the sha256 of {rel_}")
        if not f.is_file() or G.sha256_file(f) != want:
            raise Refused(f"{rel_} differs from the file the projection was computed from")
    return dec


def check_live_site(root: Path, site: str, key: str) -> None:
    """A site is live when its direction passed and its hook check passed."""
    d = Path(root) / C.direction_path(site)
    h = Path(root) / hook_path(site)
    if not d.is_file() or read_json(d).get("passed") is not True:
        raise Refused(f"site {site}: no passed direction ({C.direction_path(site)})")
    if not h.is_file() or read_json(h).get("passed") is not True:
        raise Refused(f"site {site}: no passed hook check ({hook_path(site)})")
    if not manifest_verifies(root, f"hook-check.{key}.{site}"):
        raise Refused(f"site {site}: hook-check manifest for {key} missing or stale")


def hook_path(site: str) -> str:
    return f"{C.HOOKS_DIR}/hook_check_{site}.json"


# --- inputs --------------------------------------------------------------------------------------

_SHA = re.compile(r"^[0-9a-f]{64}$")


def manifest_files(obj) -> dict[str, str]:
    """{repo-relative path: sha256} found anywhere in the input manifest: dicts with "path" and
    "sha256", and path-like keys (containing "/") mapped to a sha256 string."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        p, s = obj.get("path"), obj.get("sha256")
        if isinstance(p, str) and isinstance(s, str) and _SHA.match(s):
            out[p] = s
        for k, v in obj.items():
            if isinstance(k, str) and "/" in k and isinstance(v, str) and _SHA.match(v):
                out[k] = v
            elif isinstance(v, (dict, list)):
                out.update(manifest_files(v))
    elif isinstance(obj, list):
        for v in obj:
            out.update(manifest_files(v))
    return out


def input_shas(root: Path) -> dict[str, str]:
    p = Path(root) / C.INPUT_MANIFEST
    if not p.is_file():
        raise Refused(f"{C.INPUT_MANIFEST} missing")
    return manifest_files(read_json(p))


def verify_file(root: Path, rel_: str, listed: dict[str, str]) -> str:
    """sha256 of an input file that must exist and match the input manifest."""
    f = Path(root) / rel_
    if not f.is_file():
        raise Refused(f"{rel_} missing")
    if rel_ not in listed:
        raise Refused(f"{rel_} is not in {C.INPUT_MANIFEST}")
    got = G.sha256_file(f)
    if got != listed[rel_]:
        raise Refused(f"{rel_}: sha256 {got} != manifest {listed[rel_]}")
    return got


def verify_inputs(root: Path) -> dict:
    """Question files against their frozen sha256; the required inputs and every other listed
    pod-side file present against the input manifest. Returns what was checked."""
    root = Path(root)
    listed = input_shas(root)
    checked: dict[str, str] = {}
    for q, rel_ in C.QUESTION_FILES.items():
        f = root / rel_
        if not f.is_file():
            raise Refused(f"{rel_} missing")
        got = G.sha256_file(f)
        if got != C.QUESTION_SHA256[q]:
            raise Refused(f"{rel_}: sha256 {got} != frozen {C.QUESTION_SHA256[q]}")
        checked[rel_] = got
    for rel_ in REQUIRED_INPUTS:
        checked[rel_] = verify_file(root, rel_, listed)
    # Amendment D: the inferred pairs are required unless the (verified) survival record says
    # the battery was dropped
    if (root / INFERRED_SURVIVAL).is_file() or not inferred_dropped(root):
        checked[INFERRED_SURVIVAL] = verify_file(root, INFERRED_SURVIVAL, listed)
    if not inferred_dropped(root):
        checked[INFERRED_PAIRS] = verify_file(root, INFERRED_PAIRS, listed)
    skipped = []
    for rel_, sha in sorted(listed.items()):
        if rel_ in checked:
            continue
        if rel_.startswith(POD_PREFIXES) and (root / rel_).is_file():
            checked[rel_] = verify_file(root, rel_, listed)
        else:
            skipped.append(rel_)
    return {"verified": checked, "not_on_pod": skipped,
            "input_manifest_sha256": G.sha256_file(root / C.INPUT_MANIFEST)}


def inferred_dropped(root: Path) -> bool:
    """Amendment D: True iff the inferred battery's survival record exists and says dropped."""
    p = Path(root) / INFERRED_SURVIVAL
    return p.is_file() and read_json(p).get("dropped") is True


def organism_batteries(args) -> list[str]:
    """The invocation's organism batteries (`--battery`, default all), without the inferred
    battery when its survival record says it was dropped."""
    bats = list(args.battery or ORGANISM_BATTERIES)
    if "inferred" in bats and inferred_dropped(Path(args.root)):
        bats.remove("inferred")
    return bats


def load_questions(root: Path) -> dict[str, list[dict]]:
    out = {}
    for q, rel_ in C.QUESTION_FILES.items():
        f = Path(root) / rel_
        if not f.is_file() or G.sha256_file(f) != C.QUESTION_SHA256[q]:
            raise Refused(f"{rel_} missing or not the frozen file")
        out[q] = read_jsonl(f)
    return out


def load_inputs(root: Path) -> dict:
    """Questions, kept notices (NOTICE_COLUMNS rows with kept True), tasks (+ narrow rows), the
    direction-cell instances and the kept inferred pairs (Amendment D; None when the battery was
    dropped), each file checked against the input manifest."""
    root = Path(root)
    listed = input_shas(root)
    for rel_ in (NOTICES, TASKS, DIRCELLS):
        verify_file(root, rel_, listed)
    inferred = None
    if not inferred_dropped(root):
        verify_file(root, INFERRED_SURVIVAL, listed)
        verify_file(root, INFERRED_PAIRS, listed)
        inferred = [r for r in read_jsonl(root / INFERRED_PAIRS) if r.get("kept") is True]
    tasks = read_jsonl(root / TASKS)
    if (root / NARROW).is_file():
        verify_file(root, NARROW, listed)
        tasks += read_jsonl(root / NARROW)
    cells = read_json(root / DIRCELLS)
    if isinstance(cells, dict):
        cells = cells.get("cell_instances") or cells.get("instances") or []
    return {
        "questions": load_questions(root),
        "kept": [r for r in read_jsonl(root / NOTICES) if r.get("kept") is True],
        "tasks": tasks,
        "cell_instances": tuple(cells),
        "inferred": inferred,
        "manifest_sha256": G.sha256_file(root / C.INPUT_MANIFEST),
    }


def build_requests(tok, battery: str, key: str, inp: dict, **kw) -> list:
    if battery == "inferred":
        if not inp.get("inferred"):
            raise Refused("the inferred battery has no kept pairs (dropped or missing)")
        kw["inferred_pairs"] = inp["inferred"]
    return _mod("vgen").build_requests(tok, battery, key, inp["questions"], inp["kept"],
                                       inp["tasks"], **kw)


# --- models --------------------------------------------------------------------------------------


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version); a hook so tests can swap in fakes."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, getattr(vllm, "__version__", "unknown")


def _tokenizer():
    from emx.qwen.render import load_tokenizer

    return load_tokenizer()


def _template_sha256(tok) -> str:
    from emx.qwen.render import template_sha256

    return template_sha256(tok)


def _load_hf(adapters: dict[str, str]):
    """The base in bf16 with EAGER attention on CUDA, with each adapter loaded under its model
    key (PEFT multi-adapter; the first is active). Empty `adapters`: the plain base."""
    from emx.qwen import train as T

    model, _ = T.load_base("cuda", attn="eager")
    if adapters:
        from peft import PeftModel

        items = list(adapters.items())
        model = PeftModel.from_pretrained(model, items[0][1], adapter_name=items[0][0])
        for k, p in items[1:]:
            model.load_adapter(p, adapter_name=k)
    model.eval()
    return model


def _use_adapter(model, key: str) -> None:
    if hasattr(model, "set_adapter"):
        model.set_adapter(key)


def adapter_path(args, key: str) -> str:
    """The pod copy of a trained adapter, restored from results/E13/adapters/<key> when the pod
    copy is missing (a relaunched pod), and checked against ADAPTER_EXPECT."""
    pod = Path(args.adapter_dir) / key
    res = Path(args.root) / C.ADAPTERS_DIR / key
    if not all((pod / f).is_file() for f in R.ADAPTER_FILES):
        if not all((res / f).is_file() for f in R.ADAPTER_FILES):
            raise Refused(f"adapter {key} missing in {pod} and {res}")
        pod.mkdir(parents=True, exist_ok=True)
        for f in R.ADAPTER_FILES:
            shutil.copy2(res / f, pod / f)
        log(f"adapter {key}: restored {pod} from {res}")
    # code review r2 (pod, MINOR): the dependency records hash the saved copy under
    # results/E13/adapters, so the copy actually loaded must be byte-identical to it
    if all((res / f).is_file() for f in R.ADAPTER_FILES):
        diff = [f for f in R.ADAPTER_FILES
                if G.sha256_file(pod / f) != G.sha256_file(res / f)]
        if diff:
            raise Refused(f"adapter {key}: {pod} differs from {res} in {diff}")
    _check_adapter(key, pod)
    return str(pod)


def _check_adapter(key: str, path: Path | str) -> dict:
    try:
        out = _mod("vgen").check_adapter(key, str(path))
    except ValueError as e:
        raise Refused(f"adapter {key}: {e}") from None
    if isinstance(out, dict) and (out.get("ok") is False or out.get("passed") is False
                                  or out.get("problems")):
        raise Refused(f"adapter {key} fails its recipe check: {out}")
    return out if isinstance(out, dict) else {"result": out}


def organism_backend(args) -> str:
    """--backend vllm|hf, or (auto) the lora-check verdict's organism_backend."""
    if args.backend in C.BACKENDS:
        return args.backend
    p = Path(args.root) / LORA_CHECK
    if not p.is_file():
        raise Refused(f"{LORA_CHECK} missing: no validated organism backend")
    b = read_json(p).get("organism_backend")
    if b not in C.BACKENDS:
        raise Refused(f"{LORA_CHECK}: organism_backend {b!r}")
    return b


def battery_models(battery: str, recipe: str) -> list[str]:
    keys: list[str] = []
    for arm in C.BATTERIES[battery].arms:
        if arm == "base":
            keys.append(C.BASE_KEY)
        elif arm == "org":
            keys += [C.model_key("org", recipe, s) for s in C.TRAIN_SEEDS["org"]]
        elif arm == "good":
            keys.append(C.model_key("good", recipe, 0))
        elif arm == "trig":
            keys.append(C.TRIG_KEY)
    return keys


# --- generation CSVs -----------------------------------------------------------------------------


def row_key(r) -> tuple[str, str, str]:
    return (str(r["condition"]), str(r["qid"]), str(r["sample_idx"]))


def write_csv(path: Path, rows: list[dict]) -> None:
    def _w(fh):
        w = csv.DictWriter(fh, fieldnames=list(C.GEN_ALL_COLUMNS), extrasaction="raise",
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    G.atomic_write(path, _w)


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def gen_meta(root: Path, tok, *, battery: str, key: str, backend: str, engine_version: str,
             adapter: str | None, batch_size: int | None = None, extra: dict | None = None) -> dict:
    """GEN_META_KEYS + GEN_META_EXTRA (csv_sha256 and n_rows are added by `write_gen`)."""
    files = None if adapter is None else {f: G.sha256_file(Path(adapter) / f)
                                          for f in R.ADAPTER_FILES}
    return {
        "model": R.BASE_ID, "model_revision": R.BASE_REV,
        "adapter_repo": None if adapter is None else f"{C.ADAPTERS_DIR}/{key}",
        "adapter_revision": None if files is None else files["adapter_model.safetensors"],
        "tokenizer_revision": R.TOKENIZER_REV, "template_sha256": _template_sha256(tok),
        "sampling": {**G.sampling_kwargs(None), "seed_rule": C.SEED_RULE},
        "engine": backend, "engine_version": engine_version, "gpu": _gpu_name(), "utc": G.utc(),
        "git_commit": G.git_commit(),
        "renderer": C.RENDERER, "seed_rule": C.SEED_RULE, "battery": battery, "model_key": key,
        "recipe": C.parse_model_key(key)[1], "backend": backend, "batch_size": batch_size,
        "inputs_manifest_sha256": G.sha256_file(Path(root) / C.INPUT_MANIFEST),
        "adapter_files": files, "eval_fmt": C.EVAL_FMT, **(extra or {}),
    }


def write_gen(root: Path, name: str, rows: list[dict], meta: dict) -> list[str]:
    """CSV (exactly GEN_ALL_COLUMNS) then meta, each atomically; returns both repo paths."""
    keys = [row_key(r) for r in rows]
    if len(set(keys)) != len(keys):
        raise RuntimeError(f"{name}: duplicate (condition, qid, sample_idx) keys")
    csv_rel, meta_rel = C.gen_csv(name), C.gen_meta(name)
    write_csv(Path(root) / csv_rel, rows)
    G.write_json(Path(root) / meta_rel, {**meta, "n_rows": len(rows),
                                         "csv_sha256": G.sha256_file(Path(root) / csv_rel)})
    return [csv_rel, meta_rel]


def gen_problems(root: Path, name: str, plan: list, pins: dict) -> list[str] | None:
    """None: no outputs; []: CSV + meta verify against the plan's keys and seeds and `pins`."""
    cp, mp = Path(root) / C.gen_csv(name), Path(root) / C.gen_meta(name)
    if not cp.exists() and not mp.exists():
        return None
    if not cp.exists() or not mp.exists():
        return ["CSV or meta missing"]
    meta = read_json(mp)
    probs = []
    if meta.get("csv_sha256") != G.sha256_file(cp):
        probs.append("csv_sha256 does not match")
    probs += [f"{k} {meta.get(k)!r} != {v!r}" for k, v in pins.items() if meta.get(k) != v]
    rows = read_csv(cp)
    want = sorted((row_key(asdict_req(r)), str(r.seed), r.prompt_text) for r in plan)
    got = sorted((row_key(x), str(x["seed"]), x["prompt_text"]) for x in rows)
    if got != want:
        probs.append(f"rows differ from the plan's keys, seeds or prompts ({len(rows)} rows, "
                     f"plan {len(plan)})")
    return probs


def asdict_req(r) -> dict:
    return {"condition": r.condition, "qid": r.qid, "sample_idx": r.sample_idx}


def plan_pins(root: Path, key: str, backend: str, adapter: str | None) -> dict:
    files = None if adapter is None else {f: G.sha256_file(Path(adapter) / f)
                                          for f in R.ADAPTER_FILES}
    return {"model_key": key, "backend": backend, "adapter_files": files,
            "renderer": C.RENDERER, "seed_rule": C.SEED_RULE,
            "inputs_manifest_sha256": G.sha256_file(Path(root) / C.INPUT_MANIFEST)}


# --- steps: download, smoke ----------------------------------------------------------------------


def fetch_base(snapshot=None) -> dict:
    """Base snapshot at BASE_REV in the HF cache; checks the config shape (eq0_pod.fetch_base)."""
    if snapshot is None:
        from huggingface_hub import snapshot_download as snapshot
    path = Path(snapshot(repo_id=R.BASE_ID, revision=R.BASE_REV, allow_patterns=BASE_PATTERNS))
    cfg = json.loads((path / "config.json").read_text())
    shape = {"num_hidden_layers": R.N_LAYERS, "hidden_size": R.HIDDEN,
             "intermediate_size": R.INTERMEDIATE, "vocab_size": R.VOCAB}
    bad = {k: (cfg.get(k), v) for k, v in shape.items() if cfg.get(k) != v}
    if bad:
        raise Refused(f"base config.json differs from the registry: {bad}")
    files = {str(p.relative_to(path)): {"size": p.stat().st_size,
                                        "blob": Path(os.path.realpath(p)).name}
             for p in sorted(path.rglob("*")) if p.is_file()}
    return {"path": str(path), "revision": R.BASE_REV, "files": files,
            "config_shape": {k: cfg.get(k) for k in shape}}


def step_download(args) -> int:
    started = G.utc()
    root = Path(args.root)
    inputs = verify_inputs(root)
    log(f"download: base {R.BASE_ID}@{R.BASE_REV[:12]} (HF_HOME={os.environ.get('HF_HOME')})")
    base = fetch_base()
    tok = _tokenizer()
    G.write_json(root / DOWNLOAD_RECORD, {
        **step_info("download", started), "hf_home": os.environ.get("HF_HOME"),
        "base": {**base, "id": R.BASE_ID},
        "tokenizer": {"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                      "template_sha256": _template_sha256(tok)},
        "inputs": inputs,
    })
    write_step_manifest(root, "download", [DOWNLOAD_RECORD])
    return 0


def smoke_verdict(items: list[dict]) -> tuple[bool, dict]:
    """Per model (eq0_pod.smoke_verdict): every answer non-empty; every non-length finish is
    "stop" on <|im_end|> (the sampled last token id, on either backend); at most one "length"
    finish."""
    per = {}
    for key in dict.fromkeys(x["model_key"] for x in items):
        xs = [x for x in items if x["model_key"] == key]
        empty = sum(1 for x in xs if not str(x["response"]).strip())
        n_len = sum(1 for x in xs if x["finish_reason"] == "length")
        bad_stop = sum(1 for x in xs if x["finish_reason"] != "length" and not (
            x["finish_reason"] == "stop" and x["last_token_id"] == R.IM_END_ID))
        ok = empty == 0 and n_len <= 1 and bad_stop == 0
        per[key] = {"n": len(xs), "empty": empty, "length_finishes": n_len,
                    "not_im_end_stops": bad_stop, "ok": ok}
    return bool(per) and all(v["ok"] for v in per.values()), per


def smoke_item(tok, r, token_ids, finish_reason: str) -> dict:
    """One smoke item from the engine's sampled ids (stop id included) and finish reason."""
    ids = [int(t) for t in token_ids]
    text, reason, n_tok = G.postprocess(tok, ids, finish_reason)
    return {"model_key": r.model_key, "condition": r.condition, "qid": r.qid,
            "notice_cond": r.notice_cond, "response": text, "finish_reason": reason,
            "last_token_id": ids[min(len(ids), n_tok) - 1] if ids else None,
            "n_new_tokens": n_tok}


def vllm_items(llm, tok, plan, lora, SamplingParams) -> list[dict]:
    outs = G.generate_outputs(llm, plan, lora, SamplingParams)
    items = []
    for r, o in zip(plan, outs, strict=True):
        if list(o.prompt_token_ids) != list(r.prompt_ids):
            raise RuntimeError(f"{r.qid}: vLLM prompt ids differ from our render")
        items.append(smoke_item(tok, r, o.outputs[0].token_ids, o.outputs[0].finish_reason))
    return items


def hf_items(model, tok, key: str, plan, batch_size: int) -> list[dict]:
    """HF version of `vllm_items` (code review r1 finding 7): `hfgen.decode_batch` under adapter
    `key`, keeping the sampled ids so the smoke verdict sees the real stop token."""
    H = _mod("hfgen")
    _use_adapter(model, key)
    items = []
    for k in range(0, len(plan), max(1, int(batch_size))):
        batch = plan[k:k + max(1, int(batch_size))]
        samples = H.decode_batch(model, [list(r.prompt_ids) for r in batch],
                                 [int(r.seed) for r in batch])
        for r, s in zip(batch, samples, strict=True):
            items.append(smoke_item(tok, r, s.token_ids, s.finish_reason))
    return items


def step_smoke(args) -> int:
    started = G.utc()
    root = Path(args.root)
    inp = load_inputs(root)
    tok = _tokenizer()
    plan = build_requests(tok, "dev", C.BASE_KEY, inp, samples={"general": 1})
    plan = [r for r in plan if r.qid in {q["qid"] for q in
                                         inp["questions"]["general"][: C.SMOKE_PROMPTS]}]
    LLM, SamplingParams, _LoRARequest, version = _vllm()
    kw = _mod("vgen").engine_kwargs(max_loras=1)
    llm = LLM(**kw)
    items = vllm_items(llm, tok, plan, None, SamplingParams)
    ok, per = smoke_verdict(items)
    G.write_json(root / SMOKE_RECORD, {
        **step_info("smoke", started, {"engine_seed": R.ENGINE_SEED, "seed_rule": C.SEED_RULE}),
        "passed": ok, "per_model": per, "conds": sorted({r.notice_cond for r in plan}),
        "engine_version": version, "gpu": _gpu_name(), "llm_kwargs": kw, "items": items,
    })
    if not ok:
        log(f"smoke: FAILED {per}")
        return 1
    write_step_manifest(root, "smoke", [SMOKE_RECORD])
    return 0


# --- steps: training -----------------------------------------------------------------------------


def _oom_type():
    import torch

    return torch.cuda.OutOfMemoryError


def train_paths(key: str) -> dict[str, str]:
    return {"record": f"{C.TRAIN_DIR}/{key}.json", "steps": f"{C.TRAIN_DIR}/{key}.steps.jsonl",
            "pod": f"{C.TRAIN_DIR}/{key}.pod.json",
            "failure": f"{C.TRAIN_DIR}/{key}.failure.json"}


def train_outputs(key: str) -> list[str]:
    p = train_paths(key)
    return [p["record"], p["steps"], p["pod"],
            *(f"{C.ADAPTERS_DIR}/{key}/{f}" for f in R.ADAPTER_FILES)]


def adapter_sha_ok(rec: dict, d: Path) -> bool:
    """The record's adapter_sha256 (a {file: sha} map, or the weights file's sha) matches."""
    want = rec.get("adapter_sha256")
    if not all((d / f).is_file() for f in R.ADAPTER_FILES) or not want:
        return False
    if isinstance(want, dict):
        return all(G.sha256_file(d / f) == s for f, s in want.items() if f in R.ADAPTER_FILES)
    return G.sha256_file(d / "adapter_model.safetensors") == want


def train_verifies(root: Path, adapter_dir: str, key: str) -> bool:
    """A complete run: record with N_STEPS finite steps, steps log and sidecar present, the
    training and held-out rows the sidecar recorded unchanged, and the results copy of the adapter
    matching the record (the pod copy is restored if missing)."""
    root = Path(root)
    p = train_paths(key)
    if not all((root / p[k]).is_file() for k in ("record", "steps", "pod")):
        return False
    side = read_json(root / p["pod"])
    used = {**(side.get("rows") or {}), **(side.get("heldout") or {})}
    if any(_shas(root, [r])[r] != s for r, s in used.items()):
        log(f"train {key}: its training or held-out rows changed since it ran; not skipped")
        return False
    rec = read_json(root / p["record"])
    if rec.get("nonfinite") or rec.get("n_steps") != C.N_STEPS or len(rec.get("steps") or []) \
            != C.N_STEPS:
        return False
    res = root / C.ADAPTERS_DIR / key
    if not adapter_sha_ok(rec, res):
        return False
    pod = Path(adapter_dir) / key
    if not adapter_sha_ok(rec, pod):
        pod.mkdir(parents=True, exist_ok=True)
        for f in R.ADAPTER_FILES:
            shutil.copy2(res / f, pod / f)
    return True


def _run_child(argv: list[str], log_path: Path) -> int:
    """Run one child process with its own log (never reused); returns its exit code."""
    with open(log_path, "a") as fh:
        return subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              check=False).returncode


def _child_argv(args, key: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "train-key", "--key", key,
            "--root", str(args.root), "--adapter-dir", str(args.adapter_dir)]


def run_training(args, keys: list[str], mid: str) -> int:
    """Train each key in its own child process and log; skip keys that verify; stop at the first
    failing key (gating); then the step manifest over every key's outputs."""
    root = Path(args.root)
    for key in keys:
        if train_verifies(root, args.adapter_dir, key):
            log(f"{mid}: {key} verifies; skipped")
            continue
        lp = unique_log(root, f"train_{key}")
        log(f"{mid}: {key}: child process, log {lp.relative_to(root)}")
        rc = _run_child(_child_argv(args, key), lp)
        _recovery_quietly(root)
        if rc != 0 or not train_verifies(root, args.adapter_dir, key):
            log(f"{mid}: {key} FAILED (rc={rc}); see {lp.relative_to(root)}")
            return rc or 1
    write_step_manifest(root, mid, [p for k in keys for p in train_outputs(k)])
    return 0


def step_train_trigger(args) -> int:
    return run_training(args, [C.TRIG_KEY], "train-trigger")


def step_train(args) -> int:
    return run_training(args, list(C.trained_keys(args.recipe)), f"train.{args.recipe}")


def step_train_key(args) -> int:
    """One training run (child of train/train-trigger): records, steps log, adapter copy."""
    started = G.utc()
    root = Path(args.root)
    key = args.key
    arm, recipe, seed = C.parse_model_key(key)
    listed = input_shas(root)
    rows_rel = f"{C.TRAIN_ROWS_DIR}/{arm}.jsonl"
    rows_sha = verify_file(root, rows_rel, listed)
    held_sha = verify_file(root, C.HELDOUT_ROWS, listed)
    rows, heldout = read_jsonl(root / rows_rel), read_jsonl(root / C.HELDOUT_ROWS)
    if len(rows) != C.N_TRAIN_ROWS:
        raise Refused(f"{rows_rel}: {len(rows)} rows != {C.N_TRAIN_ROWS}")
    if args.after_oom and not args.gradient_checkpointing:
        raise Refused("--after-oom is the gradient-checkpointing retry")
    p = train_paths(key)
    attempt = "after_oom" if args.after_oom else "first"
    first_failure = read_json(root / p["failure"]) if args.after_oom and \
        (root / p["failure"]).is_file() else None
    if not args.after_oom:
        set_aside(root, [p["steps"], p["failure"], p["record"], p["pod"]], "superseded")
    tm = _mod("train")
    done: list[dict] = []

    def on_step(rec: dict) -> None:  # train_arm appends the step line itself (fsynced)
        done.append({**rec, "attempt": attempt})
        _recovery_quietly(root)

    def failure(e: BaseException, kind: str) -> None:
        G.write_json(root / p["failure"], {
            **step_info("train-key", started, {"train_seed": seed}), "model_key": key,
            "attempt": attempt, "gradient_checkpointing": bool(args.gradient_checkpointing),
            "kind": kind, "error": f"{type(e).__name__}: {str(e)[:2000]}",
            "n_steps_completed": len(done), "steps_completed": done,
            "first_attempt_failure": first_failure})

    nonfinite = getattr(tm, "NonFiniteError", None) or ()
    try:
        rec = _call(tm.train_arm, key, rows, heldout, str(args.adapter_dir), on_step=on_step,
                    save=True, grad_checkpointing=bool(args.gradient_checkpointing),
                    train_dir=str(root / C.TRAIN_DIR))
    except nonfinite as e:
        failure(e, "nonfinite")
        log(f"train {key}: NON-FINITE loss or gradient: {e}")
        return 1
    except _oom_type() as e:
        failure(e, "cuda_oom")
        if args.after_oom or args.gradient_checkpointing:
            log(f"train {key}: CUDA OOM with gradient checkpointing; no further retry")
            return 1
        argv = [*_child_argv(args, key), "--gradient-checkpointing", "--after-oom"]
        _recovery_quietly(root)
        log(f"train {key}: CUDA OOM; re-exec with gradient checkpointing: {argv[2:]}")
        sys.stdout.flush()
        sys.stderr.flush()
        _execv(sys.executable, argv)
        return 1  # only reached when the exec hook returns (tests)
    except BaseException as e:
        failure(e, "error")
        raise
    if not (root / p["record"]).is_file():  # train_arm writes it under train_dir
        G.write_json(root / p["record"], rec)
    if not (root / p["steps"]).is_file():
        write_jsonl(root / p["steps"], rec.get("steps") or [])
    pod = Path(args.adapter_dir) / key
    res = root / C.ADAPTERS_DIR / key
    res.mkdir(parents=True, exist_ok=True)
    for f in R.ADAPTER_FILES:
        shutil.copy2(pod / f, res / f)
    check = _check_adapter(key, pod)
    G.write_json(root / p["pod"], {
        **step_info("train-key", started, {"train_seed": seed}), "model_key": key,
        "recipe": recipe, "attempt": attempt, "oom_without_checkpointing": bool(args.after_oom),
        "first_attempt_failure": first_failure, "rows": {rows_rel: rows_sha},
        "heldout": {C.HELDOUT_ROWS: held_sha}, "adapter_dir_pod": str(pod),
        "adapter_files": {f: G.sha256_file(res / f) for f in R.ADAPTER_FILES},
        "adapter_check": check, "gpu": _gpu_name(),
        "versions": _versions(("torch", "transformers", "peft", "bitsandbytes", "accelerate")),
    })
    if not adapter_sha_ok(rec, res):
        log(f"train {key}: adapter files do not match the record's adapter_sha256")
        return 1
    return 0


def step_bench_train(args) -> int:
    """20 optimizer steps of the real organism rows (nothing saved), timed, plus one held-out
    pass (train_arm's benchmark mode); CUDA OOM -> one re-exec with gradient checkpointing."""
    started = G.utc()
    root = Path(args.root)
    key = C.model_key("org", "r1", 0)
    listed = input_shas(root)
    rows_rel = f"{C.TRAIN_ROWS_DIR}/org.jsonl"
    verify_file(root, rows_rel, listed)
    verify_file(root, C.HELDOUT_ROWS, listed)
    if args.after_oom and not args.gradient_checkpointing:
        raise Refused("--after-oom is the gradient-checkpointing retry")
    if not args.after_oom:
        set_aside(root, [BENCH_TRAIN_STEPS, BENCH_TRAIN_FAILURE], "superseded")
    attempt = "after_oom" if args.after_oom else "first"
    first_failure = read_json(root / BENCH_TRAIN_FAILURE) if args.after_oom and \
        (root / BENCH_TRAIN_FAILURE).is_file() else None
    rows, heldout = read_jsonl(root / rows_rel), read_jsonl(root / C.HELDOUT_ROWS)
    scratch = Path(args.adapter_dir) / "_bench_train"
    before = {k: (root / v).exists() for k, v in train_paths(key).items()}
    done: list[dict] = []

    def on_step(rec: dict) -> None:
        rec = {**rec, "attempt": attempt}
        append_jsonl(root / BENCH_TRAIN_STEPS, rec)
        done.append(rec)
        _recovery_quietly(root)

    def failure(e: BaseException) -> None:
        G.write_json(root / BENCH_TRAIN_FAILURE, {
            **step_info("bench-train", started), "attempt": attempt,
            "error": f"{type(e).__name__}: {str(e)[:2000]}",
            "cuda_oom": isinstance(e, _oom_type()), "n_steps_completed": len(done),
            "steps_completed": done, "first_attempt_failure": first_failure})

    try:
        res = _call(_mod("train").train_arm, key, rows, heldout, str(scratch),
                    max_steps=C.BENCH_TRAIN_STEPS, on_step=on_step, save=False,
                    grad_checkpointing=bool(args.gradient_checkpointing),
                    train_dir=str(scratch))
    except _oom_type() as e:
        failure(e)
        if args.after_oom or args.gradient_checkpointing:
            return 1
        argv = [sys.executable, str(Path(__file__).resolve()), "bench-train",
                "--gradient-checkpointing", "--after-oom", "--root", str(root),
                "--adapter-dir", str(args.adapter_dir)]
        _recovery_quietly(root)
        log(f"bench-train: CUDA OOM; re-exec with gradient checkpointing: {argv[2:]}")
        sys.stdout.flush()
        sys.stderr.flush()
        _execv(sys.executable, argv)
        return 1
    except BaseException as e:
        failure(e)
        raise
    # benchmark records never stand in for the real run's: move any it wrote under train/
    moved = []
    for k, rel_ in train_paths(key).items():
        if not before[k] and (root / rel_).exists():
            dest = root / C.BENCH_DIR / f"train_arm_{Path(rel_).name}"
            os.replace(root / rel_, dest)
            moved.append(str(dest.relative_to(root)))
    shutil.rmtree(scratch, ignore_errors=True)
    res = res if isinstance(res, dict) else {"result": res}
    # decide.projection reads s_per_step and heldout_pass_s at the top level
    rec = {**res, **(res.get("bench") or {}),
           **step_info("bench-train", started, {"train_seed": 0}),
           "bench_model_key": key, "max_steps": C.BENCH_TRAIN_STEPS, "attempt": attempt,
           "gradient_checkpointing": bool(args.gradient_checkpointing),
           "oom_without_checkpointing": bool(args.after_oom),
           "first_attempt_failure": first_failure, "steps_log": BENCH_TRAIN_STEPS,
           "moved_from_train_dir": moved, "gpu": _gpu_name(),
           "versions": _versions(("torch", "transformers", "peft", "bitsandbytes"))}
    G.write_json(root / BENCH_TRAIN, rec)
    outs = [BENCH_TRAIN, BENCH_TRAIN_STEPS, *moved]
    if (root / BENCH_TRAIN_FAILURE).exists():
        outs.append(BENCH_TRAIN_FAILURE)
    write_step_manifest(root, "bench-train", [o for o in outs if (root / o).is_file()])
    return 0


# --- steps: lora-check ---------------------------------------------------------------------------


def step_lora_check(args) -> int:
    """Parts as eq0_pod.py: hf (peft venv) -> vllm (vLLM venv) -> score (writes the verdict and the
    step manifest), each emx.e13.lorack's; the verdict is data (exit 0) and sets
    `organism_backend` ("vllm" iff passed) for the later steps' --backend auto."""
    started = G.utc()
    root = Path(args.root)
    lk = _mod("lorack")
    path = adapter_path(args, C.TRIG_KEY)
    if args.part in ("hf", "vllm"):
        prompts = lk.lora_check_prompts(_tokenizer(), load_questions(root))
        if args.part == "hf":
            res = lk.hf_part(_load_hf({}), prompts, path)
            lk.write_hf_part(root, prompts, res, {**step_info("lora-check-hf", started),
                                                  "gpu": _gpu_name()})
        else:
            LLM, SamplingParams, LoRARequest, version = _vllm()
            kw = _mod("vgen").engine_kwargs(max_loras=1)
            rec = lk.vllm_part(LLM(**kw), prompts, path, SamplingParams, LoRARequest)
            lk.write_vllm_part(root, rec, {**step_info("lora-check-vllm", started),
                                           "engine_version": version, "llm_kwargs": kw})
        return 0
    rec = lk.check_trained(path, root=root)
    passed = rec.get("passed") is True
    backend = "vllm" if passed else "hf"
    G.write_json(root / LORA_CHECK, {**rec, "organism_backend": backend,
                                     **step_info("lora-check", started)})
    tag(f"LORA_CHECK passed={passed} organism_backend={backend}")
    write_step_manifest(root, "lora-check", [LORA_CHECK, LORA_HF_JSON, LORA_HF_NPY,
                                             LORA_VLLM_JSON])
    return 0


# --- steps: adapter-applied checks and batteries -------------------------------------------------


def _first_ids(plan, n: int = C.SMOKE_PROMPTS) -> list[list[int]]:
    seen, out = set(), []
    for r in plan:
        if r.qid not in seen:
            seen.add(r.qid)
            out.append(list(r.prompt_ids))
    return out[:n]


def hf_adapter_applied(model, prompt_ids: list[list[int]], keys: list[str]) -> tuple[bool, dict]:
    """HF/PEFT version of the adapter-applied check: the base's greedy SMOKE_CONT_TOKENS-token
    continuation (adapters disabled) scored under the base, the base again and each adapter
    (`vgen.hf_continuation_logprobs`), judged by `vgen.adapter_applied_verdict`."""
    import torch

    V = _mod("vgen")
    dev = next(model.parameters()).device
    with torch.no_grad(), model.disable_adapter():
        conts = []
        for p in prompt_ids:
            x = torch.tensor([p], device=dev)
            out = model.generate(input_ids=x, attention_mask=torch.ones_like(x), do_sample=False,
                                 max_new_tokens=SMOKE_CONT_TOKENS,
                                 min_new_tokens=SMOKE_CONT_TOKENS)
            conts.append([int(t) for t in out[0, len(p):]])
        lps = {"base": V.hf_continuation_logprobs(model, prompt_ids, conts),
               "base_repeat": V.hf_continuation_logprobs(model, prompt_ids, conts)}
    for k in keys:
        _use_adapter(model, k)
        lps[k] = V.hf_continuation_logprobs(model, prompt_ids, conts)
    ok, rec = V.adapter_applied_verdict(lps, keys)
    return ok, {**rec, "backend": "hf", "continuations": conts}


def adapter_applied(backend: str, engine, tok, prompt_ids, paths: dict[str, str]):
    """(ok, record) on the validated backend; `engine` is (llm, SamplingParams, LoRARequest)
    for vLLM, or the PEFT model for HF."""
    if backend == "vllm":
        llm, SP, LR = engine
        return _mod("vgen").smoke_adapter_applied(llm, tok, prompt_ids, paths, SP, LR)
    return hf_adapter_applied(engine, prompt_ids, list(paths))


class Engine:
    """The validated organism backend for one step: one vLLM engine (a LoRARequest per adapter)
    or one HF model (PEFT multi-adapter, switched per model key)."""

    def __init__(self, backend: str, paths: dict[str, str], batch_size: int):
        self.backend, self.paths, self.batch_size = backend, paths, batch_size
        if backend == "vllm":
            LLM, self.SP, self.LR, self.version = _vllm()
            self.kw = _mod("vgen").engine_kwargs(max_loras=max(1, len(paths)))
            self.llm = LLM(**self.kw)
        else:
            self.version = "hf " + str(_versions(("transformers", "peft", "torch")))
            self.kw = None
            self.model = _load_hf(paths)

    @property
    def handle(self):
        return (self.llm, self.SP, self.LR) if self.backend == "vllm" else self.model

    def run(self, tok, key: str, plan: list) -> list[dict]:
        if self.backend == "vllm":
            lora = None if key == C.BASE_KEY else _mod("vgen").lora_request(
                key, self.paths[key], self.LR)
            return _mod("vgen").run_battery(self.llm, tok, plan, lora)
        if key == C.BASE_KEY:
            if self.paths:
                with self.model.disable_adapter():
                    return _mod("hfgen").run_hf(self.model, tok, plan,
                                                batch_size=self.batch_size)
            return _mod("hfgen").run_hf(self.model, tok, plan, batch_size=self.batch_size)
        _use_adapter(self.model, key)
        return _mod("hfgen").run_hf(self.model, tok, plan, batch_size=self.batch_size)


def run_batteries(args, root: Path, tok, eng: Engine, jobs: list[tuple[str, str, list, str]],
                  part: str | None = None) -> dict[str, list[str]]:
    """Generate each (battery, model_key, plan, name) job unless its CSV + meta verify (an
    unverified one is set aside); returns {battery: output paths}."""
    outs: dict[str, list[str]] = {}
    for battery, key, plan, name in jobs:
        adapter = None if key == C.BASE_KEY else eng.paths[key]
        pins = plan_pins(root, key, eng.backend, adapter)
        probs = gen_problems(root, name, plan, pins)
        if probs == []:
            log(f"{name}: verifies; skipped")
        else:
            if probs is not None:
                moved = set_aside(root, [C.gen_csv(name), C.gen_meta(name)], "incomplete")
                log(f"{name}: does not verify ({probs}); set aside {moved}")
            t0 = time.perf_counter()
            rows = eng.run(tok, key, plan)
            wall = time.perf_counter() - t0
            meta = gen_meta(root, tok, battery=battery, key=key, backend=eng.backend,
                            engine_version=eng.version, adapter=adapter,
                            batch_size=eng.batch_size if eng.backend == "hf" else None,
                            extra={"llm_kwargs": eng.kw, "part": part,
                                   "throughput": {"wall_seconds": round(wall, 3),
                                                  "n_requests": len(plan)}})
            write_gen(root, name, rows, meta)
            _recovery_quietly(root)
            log(f"{name}: wrote {len(rows)} rows in {wall:.0f}s ({eng.backend})")
            probs = gen_problems(root, name, plan, pins)
            if probs:
                raise RuntimeError(f"{name}: fresh output does not verify: {probs}")
        outs.setdefault(battery, []).extend([C.gen_csv(name), C.gen_meta(name)])
    return outs


def step_gen_trigger(args) -> int:
    started = G.utc()
    root = Path(args.root)
    backend = organism_backend(args)
    inp = load_inputs(root)
    tok = _tokenizer()
    paths = {C.TRIG_KEY: adapter_path(args, C.TRIG_KEY)}
    eng = Engine(backend, paths, args.batch_size)
    plans = {b: build_requests(tok, b, C.TRIG_KEY, inp) for b in ("trigger", "narrow_trig")}
    ok, rec = adapter_applied(backend, eng.handle, tok, _first_ids(plans["trigger"]), paths)
    G.write_json(root / GEN_TRIGGER_CHECK, {**rec, **step_info("gen-trigger", started),
                                            "passed": ok, "backend": backend})
    tag(f"ADAPTER_APPLIED key={C.TRIG_KEY} passed={ok} backend={backend}")
    if not ok:
        return 1
    outs = run_batteries(args, root, tok, eng, [(b, C.TRIG_KEY, plans[b],
                                                 C.gen_name(b, C.TRIG_KEY)) for b in plans])
    write_step_manifest(root, "gen-trigger", [GEN_TRIGGER_CHECK, *(p for v in outs.values() for p in v)])
    return 0


def smoke_adapters_record(recipe: str) -> str:
    return f"{C.BENCH_DIR}/smoke_adapters_{recipe}.json"


def step_smoke_adapters(args) -> int:
    started = G.utc()
    root = Path(args.root)
    backend = organism_backend(args)
    inp = load_inputs(root)
    tok = _tokenizer()
    keys = list(C.trained_keys(args.recipe))
    paths = {k: adapter_path(args, k) for k in keys}
    eng = Engine(backend, paths, args.batch_size)
    plans = {k: build_requests(tok, "dev", k, inp, conds=("noov",), samples={"general": 1})
             for k in keys}
    ok_applied, applied = adapter_applied(backend, eng.handle, tok,
                                          _first_ids(plans[keys[0]]), paths)
    items = []
    for k in keys:
        if backend == "vllm":
            lora = _mod("vgen").lora_request(k, paths[k], eng.LR)
            items += vllm_items(eng.llm, tok, plans[k], lora, eng.SP)
        else:
            items += hf_items(eng.model, tok, k, plans[k], eng.batch_size)
    ok_text, per = smoke_verdict(items)
    ok = ok_applied and ok_text
    rel_ = smoke_adapters_record(args.recipe)
    G.write_json(root / rel_, {**step_info("smoke-adapters", started), "recipe": args.recipe,
                               "backend": backend, "passed": ok, "adapter_applied": applied,
                               "per_model": per, "items": items})
    tag(f"SMOKE_ADAPTERS recipe={args.recipe} passed={ok} backend={backend}")
    if not ok:
        return 1
    write_step_manifest(root, f"smoke-adapters.{args.recipe}", [rel_])
    return 0


def step_gen_organism(args) -> int:
    root = Path(args.root)
    dec = check_projection(root)
    backend = organism_backend(args)
    inp = load_inputs(root)
    tok = _tokenizer()
    batteries = organism_batteries(args)
    if "inferred" in (args.battery or ORGANISM_BATTERIES) and "inferred" not in batteries:
        log("gen-organism: inferred battery skipped: its survival record says dropped "
            "(Amendment D)")
    if not batteries:
        return 0
    keys = list(dict.fromkeys(k for b in batteries for k in battery_models(b, args.recipe)))
    paths = {k: adapter_path(args, k) for k in keys if k != C.BASE_KEY}
    eng = Engine(backend, paths, args.batch_size)
    log(f"gen-organism: projection {dec.get('outcome')}; batteries {batteries}; {backend}")
    for b in batteries:  # dev first: its manifest lets the laptop start judging
        jobs = [(b, k, build_requests(tok, b, k, inp), C.gen_name(b, k))
                for k in battery_models(b, args.recipe)]
        outs = run_batteries(args, root, tok, eng, jobs)
        write_step_manifest(root, f"gen-organism.{args.recipe}.{b}", outs[b])
    return 0


def dirsrc_name(key: str, extend: bool) -> str:
    return C.gen_name("dirsrc", key, "ext" if extend else None)


def step_gen_dirsource(args) -> int:
    root = Path(args.root)
    backend = organism_backend(args)
    inp = load_inputs(root)
    if not inp["cell_instances"]:
        raise Refused(f"{DIRCELLS} has no cell instances")
    tok = _tokenizer()
    key = args.seed_key
    eng = Engine(backend, {key: adapter_path(args, key)}, args.batch_size)
    kw = {"cell_instances": inp["cell_instances"]}
    if args.extend:
        kw["sample_range"] = (C.BATTERIES["dirsrc"].samples["general"], C.DIRSRC_EXTEND_TO)
    plan = build_requests(tok, "dirsrc", key, inp, **kw)
    name = dirsrc_name(key, args.extend)
    outs = run_batteries(args, root, tok, eng, [("dirsrc", key, plan, name)])
    write_step_manifest(root, manifest_ids(args)[0], outs["dirsrc"])
    return 0


def step_bench_hooked(args) -> int:
    """hfgen.bench_hooked on org_<recipe>_s0: notice-wrapped development prompts and utility tasks
    (both sites, shuffled with a seeded order), steered at alpha 1 with a seeded random unit
    vector and g = 0.1 x the mean residual norm entering block 14 (both recorded)."""
    import numpy as np

    started = G.utc()
    root = Path(args.root)
    inp = load_inputs(root)
    tok = _tokenizer()
    key = C.model_key("org", args.recipe, 0)
    model = _load_hf({key: adapter_path(args, key)})
    reqs = []
    for site in C.SITE_NAMES:
        reqs += build_requests(tok, "sweep", key, inp, site=site, alpha=1.0)
    random.Random(f"{C.DATA_SEED}|bench-hooked").shuffle(reqs)
    em = [r for r in reqs if r.task_type == "em"][:BENCH_RESID_PROMPTS]
    norm = mean_resid_norm(model, [list(r.prompt_ids) for r in em])
    v = np.random.default_rng(C.DATA_SEED).standard_normal(R.HIDDEN)
    u = (v / np.linalg.norm(v)).astype(np.float32)
    g = BENCH_G_SHARE * norm
    res = _mod("hfgen").bench_hooked(model, tok, reqs, C.BENCH_BATCH_SIZES,
                                     steer_for=lambda r: (u, g))
    res = res if isinstance(res, dict) else {"result": res}
    per = res.get("per_batch_size") or {}
    best = per.get(res.get("best_batch_size")) or per.get(str(res.get("best_batch_size"))) or {}
    G.write_json(root / BENCH_HOOKED, {
        **res,
        # decide.projection's names: by_batch, answers_per_s (the best batch size's)
        "by_batch": {str(k): v for k, v in per.items()},
        "answers_per_s": best.get("answers_per_s"),
        **step_info("bench-hooked", started, {"u_seed": C.DATA_SEED}),
        "model_key": key, "recipe": args.recipe, "batch_sizes": list(C.BENCH_BATCH_SIZES),
        "n_answers": C.BENCH_HOOKED_ANSWERS, "alpha": 1.0, "u_seed": C.DATA_SEED,
        "mean_resid_norm": norm, "g": g, "g_over_resid_norm": BENCH_G_SHARE,
        "resid_norm_prompts": len(em), "gpu": _gpu_name(),
        "versions": _versions(("torch", "transformers", "peft"))})
    write_step_manifest(root, "bench-hooked", [BENCH_HOOKED])
    return 0


def mean_resid_norm(model, prompt_ids: list[list[int]]) -> float:
    """Mean L2 norm of the residual stream entering block 14 (hidden_states[14]) over every
    token of `prompt_ids` (one unpadded forward each)."""
    import torch

    layer = C.SITES["input"][1]
    dev = next(model.parameters()).device
    tot, n = 0.0, 0
    with torch.no_grad():
        for ids in prompt_ids:
            out = model(input_ids=torch.tensor([ids], device=dev), output_hidden_states=True)
            h = out.hidden_states[layer][0].float()
            tot += float(h.norm(dim=-1).sum())
            n += h.shape[0]
    return tot / max(n, 1)


# --- steps: direction ----------------------------------------------------------------------------


def label_lookup(dec: dict) -> dict:
    """{(condition or cell, qid, sample_idx): label} from decisions/dirsource_labels.json, whose
    `labels` is a list of {qid, sample_idx, label} rows carrying `condition`, or `cell`, or
    `instance_id`."""
    out = {}
    for x in dec.get("labels") or []:
        q, k = str(x["qid"]), str(x["sample_idx"])
        if x.get("condition"):
            ref = str(x["condition"])
        elif x.get("cell"):
            ref = str(x["cell"])
        else:
            ref = C.cell_id(q, str(x["instance_id"]))
        if x["label"] not in ("bad", "good", "drop"):
            raise Refused(f"label {x['label']!r} for {q}/{k}")
        out[(ref, q, k)] = x["label"]
    return out


def labelled_dirsrc_rows(root: Path, key: str) -> tuple[list[dict], dict]:
    """dirsrc rows (base CSV + extension when present), each with its cell, instance and label;
    every row must be labelled (a stale labels file refuses)."""
    p = Path(root) / DIRSOURCE_LABELS
    if not p.is_file():
        raise Refused(f"{DIRSOURCE_LABELS} missing (pushed by the laptop)")
    lab = label_lookup(read_json(p))
    rows, srcs = [], {}
    for ext in (False, True):
        rel_ = C.gen_csv(dirsrc_name(key, ext))
        f = Path(root) / rel_
        if not f.is_file():
            if not ext:
                raise Refused(f"{rel_} missing")
            continue
        srcs[rel_] = G.sha256_file(f)
        for r in read_csv(f):
            inst = C.parse_condition(r["condition"])["notice_cond"].partition("@")[2]
            cell = C.cell_id(r["qid"], inst)
            k = (r["qid"], str(r["sample_idx"]))
            label = lab.get((r["condition"], *k)) or lab.get((cell, *k))
            if label is None:
                raise Refused(f"{rel_}: {r['qid']}/{r['sample_idx']} ({cell}) has no label")
            rows.append({**r, "cell": cell, "instance_id": inst, "label": label,
                         "source_csv": rel_})
    return rows, {"sources": srcs, "labels_sha256": G.sha256_file(p)}


def acts_path(site: str) -> str:
    return f"{C.DIRECTION_DIR}/acts_{site}.safetensors"  # = emx.e13.acts.acts_name


def step_capture(args) -> int:
    """acts.capture_means at both sites over the bad and good dirsrc rows (prompt ids from the
    stored prompt text, answer ids from the response), saved with acts.save_acts."""
    started = G.utc()
    root = Path(args.root)
    key = args.seed_key
    rows, prov = labelled_dirsrc_rows(root, key)
    use = [r for r in rows if r["label"] in ("bad", "good")]
    if not use:
        raise Refused("no bad or good dirsrc rows to capture")
    tok = _tokenizer()
    am = _mod("acts")
    model = _load_hf({key: adapter_path(args, key)})
    cap_rows = [{"prompt_ids": list(tok(r["prompt_text"], add_special_tokens=False)["input_ids"]),
                 "answer_ids": am.answer_ids(tok, r["response"])} for r in use]
    res = am.capture_means(model, cap_rows, sites=C.SITE_NAMES, batch_size=args.capture_batch)
    shas = am.save_acts(root / C.DIRECTION_DIR, res["means"], use)
    files = [f"{C.DIRECTION_DIR}/{n}" for n in shas]
    G.write_json(root / CAPTURE_RECORD, {
        **step_info("capture", started), "model_key": key, "n_rows_total": len(rows),
        "n_captured": len(use), "n_bad": sum(r["label"] == "bad" for r in use),
        "n_good": sum(r["label"] == "good" for r in use),
        "token_norm_mean": {s: float(v) for s, v in res["token_norm_mean"].items()},
        "n_tokens": list(res.get("n_tokens") or []), "files_sha256": shas,
        "labels_sha256": prov["labels_sha256"], "sources_sha256": prov["sources"],
        "batch_size": args.capture_batch, "gpu": _gpu_name()})
    write_step_manifest(root, f"capture.{key}", [CAPTURE_RECORD, *files])
    return 0


def step_direction(args) -> int:
    """direction.extract_direction per site from the captured means (cos(u, B) from the seed's
    train record for r1; None for r32), the cross-site cosine, direction.write_direction. A site
    that fails its rules is recorded (passed False); that is data, exit 0."""
    started = G.utc()
    root = Path(args.root)
    key = args.seed_key
    if not manifest_verifies(root, f"capture.{key}"):
        raise Refused(f"capture.{key} manifest missing or stale")
    cap = read_json(root / CAPTURE_RECORD)
    means, index = _mod("acts").load_acts(root / C.DIRECTION_DIR)
    trec = root / train_paths(key)["record"]
    recipe = C.parse_model_key(key)[1]
    adapter_B = read_json(trec).get("adapter_B") if recipe == "r1" and trec.is_file() else None
    dm = _mod("direction")
    recs = {}
    for site in C.SITE_NAMES:
        recs[site] = dm.extract_direction(
            site, means[site], [r["label"] for r in index], [r["cell"] for r in index],
            [r["qid"] for r in index], float(cap["token_norm_mean"][site]), adapter_B=adapter_B,
            acts_sha256=G.sha256_file(root / acts_path(site)),
            labels_sha256=cap["labels_sha256"])
    dm.cross_site_cosine(recs["input"], recs["output"])
    outs = []
    for site, rec in recs.items():
        dm.write_direction(site, rec, root)
        outs.append(C.direction_path(site))
        tag(f"DIRECTION site={site} passed={rec.get('passed')} "
            f"stop_reason={rec.get('stop_reason')}")
    G.write_json(root / DIRECTION_STEP, {**step_info("direction", started), "model_key": key,
                                         "adapter_B_used": adapter_B is not None,
                                         "capture_sha256": G.sha256_file(root / CAPTURE_RECORD)})
    write_step_manifest(root, f"direction.{key}", [*outs, DIRECTION_STEP])
    return 0


def step_hook_check(args) -> int:
    """steer.hook_checks at one site on 16 development prompts (the 8 questions x 2 samples
    under development no-oversight instances); a failed check is data (exit 0)."""
    started = G.utc()
    root = Path(args.root)
    key, site = args.seed_key, args.site
    d = root / C.direction_path(site)
    if not d.is_file() or read_json(d).get("passed") is not True:
        raise Refused(f"site {site}: direction missing or failed; no hook check")
    u, g = _mod("direction").load_unit(site, root)
    inp = load_inputs(root)
    tok = _tokenizer()
    model = _load_hf({key: adapter_path(args, key)})
    n_q = len(inp["questions"]["general"])
    plan = build_requests(tok, "dev", key, inp, conds=("noov",),
                          samples={"general": -(-C.HOOK_CHECK_PROMPTS // n_q)})
    prompts = [list(r.prompt_ids) for r in plan][: C.HOOK_CHECK_PROMPTS]
    rec = _mod("steer").hook_checks(model, tok, site, u, g, prompts)
    G.write_json(root / hook_path(site), rec)
    side = f"{C.HOOKS_DIR}/hook_check_{site}.pod.json"
    G.write_json(root / side, {**step_info("hook-check", started), "model_key": key,
                               "n_prompts": len(prompts), "g": float(g)})
    tag(f"HOOK_CHECK site={site} passed={rec.get('passed')}")
    write_step_manifest(root, f"hook-check.{key}.{site}", [hook_path(site), side])
    return 0


# --- steps: patch generation (sweep, final-patch) ------------------------------------------------


def parse_alphas(s: str | None) -> tuple[float, ...]:
    if s is None:
        return tuple(a for a in C.ALPHAS if a > 0)
    out = []
    for x in s.split(","):
        if not x.strip():
            continue
        a = float(x)
        C.fmt_alpha(a)
        if a <= 0:
            raise ValueError(f"steered strengths are > 0, got {a}")
        out.append(a)
    return tuple(sorted(set(out)))


def parse_cells(s: str) -> list[tuple[str, str, float]]:
    """`site:cond:alpha,...` -> unique (site, cond, alpha); alpha 0 cells map to site "none"."""
    out = []
    for item in s.split(","):
        if not item.strip():
            continue
        try:
            site, cond, a = item.strip().split(":")
            alpha = float(a)
        except ValueError:
            raise ValueError(f"bad cell {item!r}: want site:cond:alpha") from None
        C.fmt_alpha(alpha)
        if cond not in C.CONDS:
            raise ValueError(f"bad cell {item!r}: condition {cond!r}")
        if alpha == 0.0:
            site = C.UNSTEERED_SITE
        elif site not in C.SITES:
            raise ValueError(f"bad cell {item!r}: site {site!r}")
        if (site, cond, alpha) not in out:
            out.append((site, cond, alpha))
    if not out:
        raise ValueError("no cells")
    return out


def part_of(site: str) -> str:
    return "a0" if site == C.UNSTEERED_SITE else site


def patch_plan(args, tok, inp: dict) -> dict[str, list]:
    """{part: requests} for this sweep / final-patch invocation (filters applied)."""
    key = args.seed_key
    if args.step == "sweep":
        b = "sweep"
        samples = {"general": args.em_samples or C.BATTERIES[b].samples["general"],
                   "umed": args.utility_samples, "ugen": args.utility_samples}
        cells = [(C.UNSTEERED_SITE, None, 0.0)]
        for site in sweep_sites(args):
            cells += [(site, None, a) for a in sweep_alphas(args, site)]
    else:
        b = "fpatch"
        samples = {"ext48": args.em_samples or C.BATTERIES[b].samples["ext48"],
                   "umed": args.utility_samples, "ugen": args.utility_samples}
        cells = parse_cells(args.cells)
    parts: dict[str, list] = {}
    for site, cond, alpha in cells:
        kw = {"site": site, "alpha": alpha, "samples": samples}
        if cond is not None:
            kw["conds"] = (cond,)
        parts.setdefault(part_of(site), []).extend(build_requests(tok, b, key, inp, **kw))
    if args.qids:
        keep = set(args.qids.split(","))
        parts = {p: [r for r in rs if r.qid in keep] for p, rs in parts.items()}
    return {p: rs for p, rs in parts.items() if rs}


def sweep_sites(args) -> list[str]:
    sites = [s for s in (args.sites or ",".join(C.SITE_NAMES)).split(",") if s]
    bad = [s for s in sites if s not in C.SITES]
    if bad:
        raise ValueError(f"unknown sites {bad}")
    return [s for s in sites if sweep_alphas(args, s)]


def sweep_alphas(args, site: str) -> tuple[float, ...]:
    return parse_alphas(getattr(args, f"alphas_{site}"))


def patch_parts(args) -> list[str]:
    """Parts this invocation writes (no tokenizer needed): sweep a0 + its live sites,
    final-patch the parts of its cells."""
    if args.step == "sweep":
        return ["a0", *sweep_sites(args)]
    return list(dict.fromkeys(part_of(s) for s, _, _ in parse_cells(args.cells)))


def load_part(root: Path, rel_: str) -> list[dict]:
    f = Path(root) / rel_
    if not f.is_file():
        return []
    rows = read_csv(f)
    keys = [row_key(r) for r in rows]
    if len(set(keys)) != len(keys):
        raise RuntimeError(f"{rel_}: duplicate keys")
    return rows


def step_patch(args) -> int:
    """Shared by sweep and final-patch: ONE run_hf queue over every pending request (unsteered,
    then per strength the input and the output site, so the sites stay interleaved); each batch
    rewrites its part CSVs atomically (resume skips present keys after checking their seeds);
    a part's meta and manifest are written when its rows are complete and no --qids filter."""
    started = G.utc()
    root = Path(args.root)
    key = args.seed_key
    battery = "sweep" if args.step == "sweep" else "fpatch"
    parts_wanted = patch_parts(args)
    for p in parts_wanted:
        if p != "a0":
            check_live_site(root, p, key)
    inp = load_inputs(root)
    tok = _tokenizer()
    plan = patch_plan(args, tok, inp)
    names = {p: C.gen_name(battery, key, p) for p in plan}
    adapter = adapter_path(args, key)
    bindings = {p: bind_part(root, tok, names[p], key, battery, p, adapter) for p in plan}
    state = {p: load_part(root, C.gen_csv(names[p])) for p in plan}
    pending = []
    for p, reqs in plan.items():
        have = {row_key(r): r for r in state[p]}
        for r in reqs:
            k = row_key(asdict_req(r))
            if k in have:
                if str(have[k]["seed"]) != str(r.seed):
                    raise RuntimeError(f"{names[p]}: {k} has seed {have[k]['seed']} != {r.seed}")
                if have[k]["prompt_text"] != r.prompt_text:
                    raise Refused(f"{names[p]}: {k} was generated from another prompt than the "
                                  "current render; refusing to mix rows")
            else:
                pending.append(r)
    pending.sort(key=lambda r: (float(r.alpha), -1 if r.site == C.UNSTEERED_SITE
                                else C.SITE_NAMES.index(r.site)))
    log(f"{args.step}: {sum(len(v) for v in plan.values())} requests, {len(pending)} pending, "
        f"parts {sorted(plan)}, batch {args.batch_size}")
    events: list = []
    if pending:
        model = _load_hf({key: adapter})
        units = {s: _mod("direction").load_unit(s, str(root)) for s in plan if s != "a0"}

        def steer_for(r):
            return units[r.site]

        def on_rows(rows: list[dict]) -> None:
            touched = set()
            for row in rows:
                p = part_of(row["site"])
                state[p].append({k: row[k] for k in C.GEN_ALL_COLUMNS})
                touched.add(p)
            for p in touched:
                write_csv(root / C.gen_csv(names[p]), state[p])
            _recovery_quietly(root)

        _mod("hfgen").run_hf(model, tok, pending, batch_size=args.batch_size,
                             steer_for=steer_for, on_rows=on_rows, events=events)
    for p, reqs in plan.items():
        if not args.qids:
            got = {row_key(r) for r in load_part(root, C.gen_csv(names[p]))}
            missing = [r for r in reqs if row_key(asdict_req(r)) not in got]
            if missing:
                raise RuntimeError(f"{names[p]}: {len(missing)} requested rows missing")
            rows = load_part(root, C.gen_csv(names[p]))
            meta = gen_meta(root, tok, battery=battery, key=key, backend="hf",
                            engine_version="hf " + str(_versions(("transformers", "peft",
                                                                  "torch"))),
                            adapter=adapter, batch_size=args.batch_size,
                            extra={"part": p, "oom_events": events, "utc_start": started,
                                   "alphas": sorted({float(r["alpha"]) for r in rows}),
                                   "n_rows_this_invocation": len(reqs),
                                   "direction_sha256": bindings[p]["direction_sha256"],
                                   "binding": binding_path(names[p])})
            write_gen(root, names[p], rows, meta)
            write_step_manifest(root, f"{args.step}.{p}", [
                C.gen_csv(names[p]), C.gen_meta(names[p]), binding_path(names[p])])
    return 0


def binding_path(name: str) -> str:
    return f"{C.GEN_DIR}/{name}.binding.json"


def patch_binding(root: Path, tok, key: str, battery: str, part: str, adapter: str) -> dict:
    """What a patch part's rows depend on beyond their keys and seeds (module docstring)."""
    d = None if part == "a0" else Path(root) / C.direction_path(part)
    if d is not None and not d.is_file():
        raise Refused(f"{C.direction_path(part)} missing")
    rec = {"model_key": key, "battery": battery, "part": part,
           "adapter_files": {f: G.sha256_file(Path(adapter) / f) for f in R.ADAPTER_FILES},
           "direction_sha256": None if d is None else G.sha256_file(d),
           "inputs_manifest_sha256": G.sha256_file(Path(root) / C.INPUT_MANIFEST),
           "renderer": C.RENDERER, "seed_rule": C.SEED_RULE, "eval_fmt": C.EVAL_FMT,
           "template_sha256": _template_sha256(tok), "tokenizer_revision": R.TOKENIZER_REV,
           "model": R.BASE_ID, "model_revision": R.BASE_REV, "sampling": G.sampling_kwargs(None)}
    return json.loads(json.dumps(rec))


def bind_part(root: Path, tok, name: str, key: str, battery: str, part: str,
              adapter: str) -> dict:
    """The part's binding: written before its first batch; for a part CSV that already exists,
    the stored binding must equal the recomputed one (else Refused: never mix rows)."""
    want = patch_binding(root, tok, key, battery, part, adapter)
    bp = Path(root) / binding_path(name)
    if (Path(root) / C.gen_csv(name)).exists():
        if not bp.is_file():
            raise Refused(f"{C.gen_csv(name)} exists without {binding_path(name)}: its rows "
                          "cannot be tied to an adapter and direction; set it aside by hand")
        have = read_json(bp)
        diff = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
        if diff:
            raise Refused(f"{name}: existing rows were generated under another binding "
                          f"(differs in {diff}); refusing to resume and mix rows")
    else:
        G.write_json(bp, want)
    return want


# --- closing -------------------------------------------------------------------------------------


def step_manifest(args) -> int:
    closing_manifest(Path(args.root))
    return 0


def step_recovery(args) -> int:
    out = write_recovery_manifest(Path(args.root))
    log(f"recovery: wrote {RECOVERY} ({len(out.read_text().splitlines())} files)")
    return 0


# --- manifest ids and the dry run ----------------------------------------------------------------


def manifest_ids(args) -> list[str]:
    """The manifests this invocation must leave verifying (module docstring); [] = none."""
    s = args.step
    if s in ("download", "smoke", "bench-train", "train-trigger", "gen-trigger", "bench-hooked",
             "manifest"):
        return [s]
    if s == "lora-check":
        return ["lora-check"] if args.part in (None, "score") else []
    if s in ("train", "smoke-adapters"):
        return [f"{s}.{args.recipe}"]
    if s == "gen-organism":
        return [f"gen-organism.{args.recipe}.{b}" for b in organism_batteries(args)]
    if s == "gen-dirsource":
        return [f"gen-dirsource.{args.seed_key}" + (".ext" if args.extend else "")]
    if s in ("capture", "direction"):
        return [f"{s}.{args.seed_key}"]
    if s == "hook-check":
        return [f"hook-check.{args.seed_key}.{args.site}"]
    if s in ("sweep", "final-patch"):
        return [] if args.qids else [f"{s}.{p}" for p in patch_parts(args)]
    return []  # recovery, train-key


def _dry_json(root: Path, rel_: str, obj: dict) -> str:
    G.write_json(Path(root) / rel_, {"dry_run": True, **obj})
    return rel_


def _dry_gen(root: Path, name: str, battery: str, key: str, backend: str) -> list[str]:
    return write_gen(root, name, [], {"dry_run": True, "battery": battery, "model_key": key,
                                      "backend": backend, "renderer": C.RENDERER,
                                      "seed_rule": C.SEED_RULE})


def _dry_train(root: Path, adapter_dir: str, key: str) -> list[str]:
    pod = Path(adapter_dir) / key
    pod.mkdir(parents=True, exist_ok=True)
    for f in R.ADAPTER_FILES:
        (pod / f).write_text(f"dry {key} {f}\n")
    res = Path(root) / C.ADAPTERS_DIR / key
    res.mkdir(parents=True, exist_ok=True)
    for f in R.ADAPTER_FILES:
        shutil.copy2(pod / f, res / f)
    p = train_paths(key)
    steps = [{"step": i + 1, "loss": 1.0} for i in range(C.N_STEPS)]
    _dry_json(root, p["record"], {"model_key": key, "n_steps": C.N_STEPS, "steps": steps,
                                  "nonfinite": False, "adapter_B": None,
                                  "adapter_sha256": {f: G.sha256_file(res / f)
                                                     for f in R.ADAPTER_FILES}})
    write_jsonl(Path(root) / p["steps"], steps)
    _dry_json(root, p["pod"], {"model_key": key})
    return train_outputs(key)


def dry_step(args) -> int:
    """Stub outputs and manifests for one step (E13_DRY_RUN=1); the step's gates still apply."""
    root = Path(args.root)
    s = args.step
    if s in _env_set("E13_DRY_FAIL"):
        log(f"dry run: {s} fails (E13_DRY_FAIL)")
        return 1
    outs: dict[str, list[str]] = {}
    ids = manifest_ids(args)
    if s == "download":
        outs["download"] = [_dry_json(root, DOWNLOAD_RECORD, {})]
    elif s == "smoke":
        outs["smoke"] = [_dry_json(root, SMOKE_RECORD, {"passed": True})]
    elif s == "bench-train":
        outs["bench-train"] = [_dry_json(root, BENCH_TRAIN, {"s_per_step": 0.25,
                                                            "heldout_pass_s": 30.0})]
    elif s == "train-trigger":
        outs["train-trigger"] = _dry_train(root, args.adapter_dir, C.TRIG_KEY)
    elif s == "train":
        outs[ids[0]] = [p for k in C.trained_keys(args.recipe)
                        for p in _dry_train(root, args.adapter_dir, k)]
    elif s == "train-key":
        _dry_train(root, args.adapter_dir, args.key)
    elif s == "lora-check":
        if args.part == "score":
            passed = os.environ.get("E13_DRY_LORA_PASS", "1") != "0"
            outs["lora-check"] = [_dry_json(root, LORA_CHECK, {
                "adapter": C.TRIG_KEY, "passed": passed, "backend": "vllm" if passed else "hf",
                "organism_backend": "vllm" if passed else "hf"})]
    elif s == "gen-trigger":
        b = organism_backend(args)
        outs[s] = [_dry_json(root, GEN_TRIGGER_CHECK, {"passed": True, "backend": b})]
        for bat in ("trigger", "narrow_trig"):
            outs[s] += _dry_gen(root, C.gen_name(bat, C.TRIG_KEY), bat, C.TRIG_KEY, b)
    elif s == "smoke-adapters":
        outs[ids[0]] = [_dry_json(root, smoke_adapters_record(args.recipe),
                                  {"passed": True, "backend": organism_backend(args)})]
    elif s == "bench-hooked":
        outs[s] = [_dry_json(root, BENCH_HOOKED, {
            "answers_per_s": 10.0, "by_batch": {"64": {"answers_per_s": 10.0}}})]
    elif s == "gen-organism":
        check_projection(root)
        b = organism_backend(args)
        for bat, mid in zip(organism_batteries(args), ids, strict=True):
            outs[mid] = [p for k in battery_models(bat, args.recipe)
                         for p in _dry_gen(root, C.gen_name(bat, k), bat, k, b)]
    elif s == "gen-dirsource":
        outs[ids[0]] = _dry_gen(root, dirsrc_name(args.seed_key, args.extend), "dirsrc",
                                args.seed_key, organism_backend(args))
    elif s == "capture":
        outs[ids[0]] = [_dry_json(root, CAPTURE_RECORD, {"model_key": args.seed_key})]
    elif s == "direction":
        fail = _env_set("E13_DRY_SITE_FAIL")
        outs[ids[0]] = [_dry_json(root, C.direction_path(site), {
            "site": site, "passed": site not in fail, "u": None,
            "stop_reason": "reliability" if site in fail else None}) for site in C.SITE_NAMES]
    elif s == "hook-check":
        d = root / C.direction_path(args.site)
        if not d.is_file() or read_json(d).get("passed") is not True:
            raise Refused(f"site {args.site}: direction missing or failed; no hook check")
        outs[ids[0]] = [_dry_json(root, hook_path(args.site), {"site": args.site,
                                                               "passed": True})]
    elif s in ("sweep", "final-patch"):
        battery = "sweep" if s == "sweep" else "fpatch"
        for p in patch_parts(args):
            if p != "a0":
                check_live_site(root, p, args.seed_key)
            name = C.gen_name(battery, args.seed_key, p)
            if not args.qids:
                outs[f"{s}.{p}"] = _dry_gen(root, name, battery, args.seed_key, "hf")
    for mid in ids:
        write_step_manifest(root, mid, outs[mid])
    return 0


# --- CLI -----------------------------------------------------------------------------------------

STEP_FUNCS = {
    "download": step_download, "smoke": step_smoke, "bench-train": step_bench_train,
    "train-trigger": step_train_trigger, "lora-check": step_lora_check,
    "gen-trigger": step_gen_trigger, "train": step_train, "smoke-adapters": step_smoke_adapters,
    "bench-hooked": step_bench_hooked, "gen-organism": step_gen_organism,
    "gen-dirsource": step_gen_dirsource, "capture": step_capture, "direction": step_direction,
    "hook-check": step_hook_check, "sweep": step_patch, "final-patch": step_patch,
    "manifest": step_manifest,
    "recovery": step_recovery,  # not a step: never completes anything
    "train-key": step_train_key,  # the child process of train / train-trigger
}
NEEDS = {
    "train": ("recipe",), "smoke-adapters": ("recipe",), "gen-organism": ("recipe",),
    "gen-dirsource": ("seed_key",), "capture": ("seed_key",), "direction": ("seed_key",),
    "hook-check": ("seed_key", "site"), "sweep": ("seed_key", "batch_size"),
    "final-patch": ("seed_key", "batch_size", "cells"), "train-key": ("key",),
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("step", choices=list(STEP_FUNCS))
    p.add_argument("--root", default=os.environ.get("E13_ROOT") or str(ROOT))
    p.add_argument("--adapter-dir", default=os.environ.get("E13_ADAPTER_DIR") or C.POD_ADAPTER_DIR)
    p.add_argument("--part", choices=["hf", "vllm", "score"], help="lora-check only")
    p.add_argument("--recipe", choices=list(C.RECIPES))
    p.add_argument("--battery", action="append", choices=list(ORGANISM_BATTERIES))
    p.add_argument("--backend", choices=["auto", *C.BACKENDS], default="auto")
    p.add_argument("--batch-size", type=int)
    p.add_argument("--capture-batch", type=int, default=16)
    p.add_argument("--seed-key")
    p.add_argument("--key", help="train-key only")
    p.add_argument("--extend", action="store_true", help="gen-dirsource only")
    p.add_argument("--site", choices=list(C.SITE_NAMES))
    p.add_argument("--sites", help="sweep: comma list (default both)")
    p.add_argument("--alphas-input")
    p.add_argument("--alphas-output")
    p.add_argument("--em-samples", type=int)
    p.add_argument("--utility-samples", type=int, default=5)
    p.add_argument("--qids", help="sweep/final-patch: comma list (probe subset, no manifests)")
    p.add_argument("--cells", help="final-patch: site:cond:alpha,...")
    p.add_argument("--gradient-checkpointing", action="store_true")
    p.add_argument("--after-oom", action="store_true")
    p.add_argument("--print-manifests", action="store_true",
                   help="print the manifest paths this invocation must leave verifying; no run")
    p.add_argument("--check-done", action="store_true",
                   help="exit 0 iff the invocation's manifests verify and their dependency "
                        "records match the current arguments and files, else 1; no run")
    return p


def parse(argv: list[str] | None):
    ps = parser()
    args = ps.parse_args(argv)
    if args.step == "bench-hooked" and args.recipe is None:
        args.recipe = "r1"
    if args.batch_size is None and args.step in ("gen-trigger", "smoke-adapters",
                                                 "gen-organism", "gen-dirsource"):
        args.batch_size = 64  # HF backend only; vLLM schedules itself
    missing = [n for n in NEEDS.get(args.step, ()) if getattr(args, n) in (None, [])]
    if missing:
        ps.error(f"{args.step} needs " + ", ".join("--" + m.replace("_", "-") for m in missing))
    if args.step == "lora-check" and not args.part and not (args.print_manifests
                                                            or args.check_done):
        ps.error("lora-check needs --part hf|vllm|score")
    if args.part and args.step != "lora-check":
        ps.error("--part belongs to lora-check")
    if (args.gradient_checkpointing or args.after_oom) and args.step not in ("bench-train",
                                                                             "train-key"):
        ps.error("--gradient-checkpointing / --after-oom belong to bench-train and train-key")
    for k in ("seed_key", "key"):
        v = getattr(args, k)
        if v is not None:
            try:
                C.parse_model_key(v)
            except ValueError as e:
                ps.error(str(e))
    try:
        manifest_ids(args)  # validates --cells / --alphas-* / --sites
    except ValueError as e:
        ps.error(str(e))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    root = Path(args.root)
    if args.print_manifests:
        for mid in manifest_ids(args):
            print(C.manifest_path(mid))
        return 0
    if args.check_done:
        ok, why = check_done(args)
        for w in why:
            log(f"not done: {w}")
        return 0 if ok else 1
    label = args.step + (f" --part {args.part}" if args.part else "")
    t = time.time()
    tag(f"STEP_START {label}")
    rc = 1
    prev = dict(_RUN)  # a nested in-process call (tests' child processes) restores it on return
    try:
        if args.step != "recovery":
            check_approval(root)
        # the inputs as the step finds them; written with each of its manifests
        _RUN["deps"] = {mid: deps_record(args, mid) for mid in manifest_ids(args)}
        if dry() and args.step not in ("manifest", "recovery"):
            rc = dry_step(args)
        else:
            rc = STEP_FUNCS[args.step](args)
    except Refused as e:
        tag(f"STEP_FAIL {label} refused: {e}")
        return REFUSED
    except BaseException as e:
        tag(f"STEP_FAIL {label} error: {type(e).__name__}: {str(e)[:300]}")
        raise
    finally:  # success or failure: whatever results/E13 holds now can be pulled
        _RUN.clear()
        _RUN.update(prev)
        if args.step != "recovery":
            _recovery_quietly(root)
    if rc == 0:
        tag(f"STEP_OK {label}")
    else:
        tag(f"STEP_FAIL {label} rc={rc}")
    log(f"step {label} {'done' if rc == 0 else f'FAILED rc={rc}'} in {time.time() - t:.0f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
