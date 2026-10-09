"""On-pod step driver for E11 (inoculation framing on Qwen2.5-7B-Instruct: rank-32 LoRA runs under a
neutral and an inoculation system prompt, a pilot-seed dose ladder, and part 2's activations). One
subcommand per step of `emx.e11.contracts.STEPS` (some in parts, one venv each); called by
scripts/pod_run_e11.sh. Ported from scripts/eq0_pod.py (step records, manifests written last, the
recovery manifest, `Refused`, the OOM re-exec, the closing manifest), which it cites throughout.

    python scripts/e11_pod.py download                       # peft venv
    python scripts/e11_pod.py smoke --part render|vllm       # peft | vllm
    python scripts/e11_pod.py bench                          # peft
    python scripts/e11_pod.py pilot-r1 --part train|gen|gen-hf|ready   # peft | vllm | peft | any
    python scripts/e11_pod.py p2-gen                         # vllm
    python scripts/e11_pod.py p2-acts                        # peft
    python scripts/e11_pod.py ladder-tick                    # peft (the `ladder` step's loop body)
    python scripts/e11_pod.py ladder-gen --part vllm|gen-hf|ready      # vllm | peft | any
    python scripts/e11_pod.py eval-train                     # peft
    python scripts/e11_pod.py lora-check --part hf|vllm|score|hf-bench # peft | vllm | peft | peft
    python scripts/e11_pod.py generate --part check|hf-bench|vllm|hf   # vllm | peft | vllm | peft
    python scripts/e11_pod.py logprobs                       # peft
    python scripts/e11_pod.py manifest                       # any (stdlib)
    python scripts/e11_pod.py recovery                       # any: only MANIFEST.recovery.sha256

    download    base snapshot at the pin (EQ0's fetch_base and BASE_SHAPE check); data/E11 training
                copies, good medical and both question files sha-checked; with part 2 on and the
                pool present, the pool manifest and every pool file checked (emx.e11.pool.load_pool)
                -> C.DOWNLOAD_RECORD
    smoke       render: real tokenizer, render_train under both framings for every rung-1, held-out
                and pool row of both datasets (and medical's good answers, no system), >= 1 trained
                token each, assert_framing_diff on every rung-1 and held-out row, mask counts; the
                data draw record (written if absent) -> results/E11/smoke_render.json
                vllm: base engine, the 8 general questions x 1 sample under C.NEUTRAL, non-empty,
                stopping at <|im_end|> (EQ0's verdict: at most one length finish) -> C.SMOKE_RECORD
    bench       train_run (20 steps, TAN32, seed C.BENCH_SEED, neutral, med rung-1 rows, no adapter
                saved) -> s/step without step 1; 200 response_means passes -> s/pass; GPU and $/h
                (C.FALLBACK_GPUS); the worst-case forecast and the cuts (emx.e11.forecast)
                -> bench/{train,acts,forecast,plan}.json and C.PLAN_JSON (always written). If even
                every cut leaves the forecast over the cap, the step fails (gating)
    pilot-r1    train: the rung-1 pilots (seed 99, neutral) of the plan's datasets; a run that ends
                non-finite (or raises) is rerun once with identical inputs; twice, or a loss that
                did not decrease, makes the dataset incomplete@1. gen: the adapter-applied check on
                each pilot, then its 400 development answers in vLLM; a pilot that fails the check
                (or meets a vLLM engine error: exit 6) is marked for gen-hf (HF fallback, meta
                engine "hf"). ready: MANIFEST.pilot-r1.sha256 (== C.ladder_manifest(1),
                immutable) and only then C.ladder_ready(1)
    p2-gen      part 2's vector answers (4,000, sampled) and greedy base answers (7,200) ->
                part2/gen/{vectors,base_answers}.csv (skipped, with the reason recorded, when the
                plan cut part 2 or the pool is absent)
    p2-acts     block-19 response means of every vector answer under its own system prompt, and
                of every pool row's dataset answer and base answer under each framing ->
                part2/acts/{vectors,pool}.{safetensors,index.jsonl}
    ladder      (pod_run_e11.sh loops) ladder-tick: accept valid decisions (emx.e11.ladder), train
                the next-rung pilots and the evaluation runs of eligible datasets, then print one
                anchored line `E11_LADDER need_gen=<keys>|wait=r<k>|done|kill|timeout`. ladder-gen:
                the need_gen pilots' answers, rung manifest and ready file (as pilot-r1). On done or
                kill the tick writes ladder/final.json and MANIFEST.ladder.sha256; a wait with no
                trainable work left for more than C.LADDER_WAIT_MIN minutes is `timeout` (exit 1)
    eval-train  any evaluation run of an eligible dataset not yet trained (same rerun-once rule)
                -> results/E11/eval_train.json
    lora-check  hf / vllm / score on the first evaluation adapter (emx.e11.loracheck; a vLLM
                engine error is recorded as a failed check, exit 6); hf-bench:
                on a failed check, the frozen HF fallback (benchmark HF generation, re-forecast,
                cut 1 before any battery answer, never drop a dataset; still over -> plan
                gen_status "incomplete: cap"; plan gen_engine "hf") -> bench/lora_check*.json
    generate    check: adapter-applied on every evaluation adapter before any answer (a failure
                or a vLLM engine error, exit 6 -> the same HF fallback in hf-bench); vllm or hf (by plan gen_engine): the main
                evaluation of each trained model, the battery (3 generated contexts x each model
                and base) and the base elicitation; a condition whose CSV + meta verify is skipped
    logprobs    task pass (base on every dataset, each evaluation adapter on its own; 200 held-out
                rows: med bad and good, fin target; no system) -> logprobs/task.csv; surprise pass
                (base, the rung-1 rows under each framing) -> logprobs/surprise.csv
    manifest    closing manifest (EQ0's), plus the immutable rung manifests r2 and r3
    recovery    not a step (EQ0's)

Every step except `recovery` refuses (exit 3) without C.APPROVAL holding a line that starts with a
UTC timestamp. A step whose MANIFEST.<step>.sha256 verifies is skipped (exit 0). A step whose
upstream step has not completed exits 4 ("blocked") without a manifest, so a rerun redoes it.
Exit codes: 0 done, 1 failed, 3 refused, 4 blocked, 6 engine fallback (a vLLM part of pilot-r1,
ladder-gen, lora-check or generate hit an engine error, recorded it, and routed its work to the HF
path; pod_run_e11.sh goes on to the step's next part; code review r1a finding 1). On a CUDA OOM a
peft step re-execs itself once with `--after-oom` (a fresh process; completed runs are skipped on
resume); a vLLM part re-execs once too before it falls back.
`--root`, `--adapter-dir` and `--lora-check-dir` exist for tests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# vLLM JIT-compiles kernels with `ninja` from this venv's bin/ (scripts/eq0_pod.py, E1 P1 smoke).
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

from emx.e11 import contracts as C
from emx.e11 import forecast as F
from emx.e11 import ladder as L
from emx.qwen import gen as QG  # stdlib helpers: utc, sha256, atomic writes
from emx.qwen import registry as R

REFUSED = 3
BLOCKED = 4
ENGINE_FALLBACK = 6  # a vLLM part recorded an engine error: pod_run_e11.sh goes on to the HF parts
BASE_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]  # scripts/eq0_pod.py
SMOKE_RENDER = f"{C.RESULTS}/smoke_render.json"
SMOKE_PROMPTS = 8
BENCH_TRAIN_DIR = f"{C.BENCH_DIR}/train_run"
BENCH_PLAN = C.bench_path("plan")  # the bench's plan, immutable (plan.json may change later)
LORA_HF_JSON = C.bench_path("lora_check_hf")
LORA_VLLM_JSON = C.bench_path("lora_check_vllm")
LORA_JSON = C.bench_path("lora_check")
HF_FALLBACK_JSON = C.bench_path("hf_fallback")
GEN_CHECK_JSON = C.bench_path("gen_check")
GEN_RECORD = C.bench_path("generation")
EVAL_TRAIN_RECORD = f"{C.RESULTS}/eval_train.json"
LADDER_FINAL = f"{C.LADDER_DIR}/final.json"
P2_GEN_RECORD = f"{C.P2_GEN_DIR}/record.json"
P2_ACTS_RECORD = f"{C.P2_ACTS_DIR}/record.json"
P2_VEC, P2_BASE = "vectors", "base_answers"
TASK_CSV = f"{C.LOGPROB_DIR}/task.csv"
SURPRISE_CSV = f"{C.LOGPROB_DIR}/surprise.csv"
LOGPROB_RECORD = f"{C.LOGPROB_DIR}/record.json"
HF_BENCH_N = 64  # requests timed for the HF fallback's rate
SETUP_H = 0.1  # pod time before the download step's start (image pull, push, setup), assumed
TASK_WHICH = {"med": ("bad", "good"), "fin": ("target",)}
PARTS: dict[str, tuple[str, ...]] = {
    "smoke": ("render", "vllm"),
    "pilot-r1": ("train", "gen", "gen-hf", "ready"),
    "ladder-gen": ("vllm", "gen-hf", "ready"),
    "lora-check": ("hf", "vllm", "score", "hf-bench"),
    "generate": ("check", "hf-bench", "vllm", "hf"),
}
OOM_STEPS = ("bench", "pilot-r1", "p2-acts", "ladder-tick", "ladder-gen", "eval-train",
             "lora-check", "generate", "logprobs")
UTC_LINE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.MULTILINE)
_execv = os.execv  # hooks so tests can stand in
_now = time.time


class Refused(Exception):
    """A pin, input or approval check failed: the step must not proceed (exit 3)."""


class Blocked(Exception):
    """An upstream step has not completed: nothing is written, a rerun redoes this step (exit 4)."""


def M(name: str):
    """emx.e11.<name>, imported lazily (tests put fakes in sys.modules)."""
    return importlib.import_module(f"emx.e11.{name}")


def log(msg: str) -> None:
    print(f"{QG.utc()} [e11] {msg}", flush=True)


def tag(line: str) -> None:
    """An anchored progress line (pollers grep `^E11_`)."""
    print(line, flush=True)


def step_info(step: str, started: str, seeds: dict | None = None) -> dict:
    return {"step": step, "utc_start": started, "utc_end": QG.utc(),
            "git_commit": QG.git_commit(), "seeds": seeds or {}}


def prov(seeds: dict | None = None, inputs: dict | None = None) -> dict:
    return C.provenance(seeds or {}, inputs or {}, QG.git_commit(), QG.utc())


def read_json(root: Path, rel: str) -> dict:
    return json.loads((Path(root) / rel).read_text())


def write_json(root: Path, rel: str, obj: dict) -> None:
    QG.write_json(Path(root) / rel, obj)


# --- manifests (scripts/eq0_pod.py) --------------------------------------------------------------


def write_manifest_at(root: Path, man_rel: str, paths: list[str]) -> Path:
    root = Path(root)
    files = sorted(set(paths))
    missing = [p for p in files if not (root / p).is_file()]
    if missing:
        raise RuntimeError(f"{man_rel}: outputs missing: {missing}")
    body = "".join(f"{QG.sha256_file(root / p)}  {p}\n" for p in files)
    QG.atomic_write(root / man_rel, lambda fh: fh.write(body))
    log(f"wrote {man_rel} ({len(files)} files)")
    return root / man_rel


def write_step_manifest(root: Path, step: str, paths: list[str]) -> Path:
    """MANIFEST.<step>.sha256 over repo-relative `paths` (each must exist); write it last."""
    return write_manifest_at(root, C.manifest_path(step), paths)


def read_manifest(path: Path) -> list[tuple[str, str]]:
    out = []
    for ln in Path(path).read_text().splitlines():
        if ln.strip():
            sha, name = ln.split("  ", 1)
            out.append((sha, name))
    return out


def manifest_verifies(root: Path, man_rel: str) -> bool:
    p = Path(root) / man_rel
    if not p.is_file() or p.stat().st_size == 0:
        return False
    for sha, name in read_manifest(p):
        f = Path(root) / name
        if not f.is_file() or QG.sha256_file(f) != sha:
            return False
    return True


def step_complete(root: Path, step: str) -> bool:
    return manifest_verifies(root, C.manifest_path(step))


def require_steps(root: Path, *steps: str) -> None:
    missing = [s for s in steps if not step_complete(root, s)]
    if missing:
        raise Blocked(f"upstream step(s) not complete: {missing}")


def closing_manifests(root: Path) -> list[str]:
    """Step manifests (C.STEPS but `manifest`) and the immutable rung manifests r2 and r3."""
    rels = [C.manifest_path(s) for s in C.STEPS if s != "manifest"]
    rels += [C.ladder_manifest(k) for k in sorted(C.RUNGS) if C.ladder_manifest(k) not in rels]
    return [r for r in rels if (Path(root) / r).is_file()]


def closing_manifest(root: Path) -> Path:
    """MANIFEST.manifest.sha256: every file of the existing step (and rung) manifests, re-verified,
    plus those manifest files. Raises RuntimeError if a listed file is missing or changed."""
    root = Path(root)
    entries: dict[str, str] = {}
    problems = []
    for man_rel in closing_manifests(root):
        for sha, name in read_manifest(root / man_rel):
            f = root / name
            if not f.is_file():
                problems.append(f"{man_rel}: {name} missing")
            elif QG.sha256_file(f) != sha:
                problems.append(f"{man_rel}: {name} changed since its manifest")
            entries[name] = sha
        entries[man_rel] = QG.sha256_file(root / man_rel)
    if problems:
        raise RuntimeError("closing manifest refused: " + "; ".join(problems))
    body = "".join(f"{entries[n]}  {n}\n" for n in sorted(entries))
    out = root / C.manifest_path("manifest")
    QG.atomic_write(out, lambda fh: fh.write(body))
    log(f"manifest: wrote {C.manifest_path('manifest')} ({len(entries)} files)")
    return out


RECOVERY = f"{C.RESULTS}/MANIFEST.recovery.sha256"


def recovery_excluded(rel_path: str) -> bool:
    """scripts/eq0_pod.py: MANIFEST.*.sha256, top-level `.pull_*` / `superseded/`, temp files."""
    parts = Path(rel_path).parts
    name = parts[-1]
    if name.startswith("MANIFEST.") and name.endswith(".sha256"):
        return True
    if parts[0].startswith(".pull_") or parts[0] == "superseded":
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
            rp = str(p.relative_to(res))
            if not recovery_excluded(rp) and not any(x.startswith(".") and x.endswith(".tmp")
                                                     for x in Path(rp).parts):
                files.append(str(p.relative_to(root)))
    body = "".join(f"{QG.sha256_file(root / f)}  {f}\n" for f in files)
    out = root / RECOVERY
    QG.atomic_write(out, lambda fh: fh.write(body))
    return out


def _recovery_quietly(root: Path) -> None:
    try:
        write_recovery_manifest(root)
    except (OSError, ValueError, RuntimeError) as e:  # never mask the step's own outcome
        log(f"recovery manifest not written: {type(e).__name__}: {e}")


def check_approval(root: Path) -> str:
    """The approval marker's first UTC-timestamp line (Refused without one)."""
    p = Path(root) / C.APPROVAL
    text = p.read_text() if p.is_file() else ""
    m = UTC_LINE.search(text)
    if not m:
        raise Refused(f"{C.APPROVAL} missing or without a UTC-timestamp line")
    return text[m.start():].splitlines()[0]


# --- plan ----------------------------------------------------------------------------------------


def load_plan(root: Path) -> dict:
    p = Path(root) / C.PLAN_JSON
    if not p.is_file():
        raise Blocked(f"{C.PLAN_JSON} missing (the bench step writes it)")
    return json.loads(p.read_text())


def plan_or_defaults(root: Path) -> dict:
    p = Path(root) / C.PLAN_JSON
    return json.loads(p.read_text()) if p.is_file() else dict(C.PLAN_DEFAULTS)


def save_plan(root: Path, plan: dict, event: str) -> dict:
    plan = {**plan, "history": [*plan.get("history", []), {"utc": QG.utc(), "event": event}],
            "provenance": prov({"bench_seed": C.BENCH_SEED}, {})}
    write_json(root, C.PLAN_JSON, plan)
    log(f"plan.json: {event}")
    return plan


# --- inputs --------------------------------------------------------------------------------------


def verify_inputs(root: Path) -> dict[str, str]:
    """{path: sha256} of the training copies, good medical and the question files, each checked
    against its frozen hash (Refused otherwise)."""
    want = {C.DATASETS[d].file: C.DATASETS[d].sha256 for d in C.DS_KEYS}
    want[C.GOOD_MED.file] = C.GOOD_MED.sha256
    want.update({C.QUESTION_FILES[q]: C.QUESTION_SHA256[q] for q in C.QSETS})
    out = {}
    for name, sha in want.items():
        p = Path(root) / name
        if not p.is_file():
            raise Refused(f"{name} missing")
        got = QG.sha256_file(p)
        if got != sha:
            raise Refused(f"{name}: sha256 {got} != frozen {sha}")
        out[name] = got
    return out


def pool_present(root: Path) -> bool:
    return (Path(root) / C.POOL_MANIFEST).is_file()


def pool_inputs(root: Path) -> dict[str, str]:
    """{path: sha256} of the pool manifest and every pool file, each file checked against the
    manifest by emx.e11.pool.load_pool (Refused on a mismatch)."""
    P = M("pool")
    try:
        manifest = P.load_manifest(root)
        P.load_pool(root, manifest)
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        raise Refused(f"pool check failed: {type(e).__name__}: {e}") from None
    out = {C.POOL_MANIFEST: QG.sha256_file(Path(root) / C.POOL_MANIFEST)}
    for d in manifest["datasets"].values():
        out[d["pool_file"]] = d["pool_sha256"]
    return out


BASE_SHAPE = {"num_hidden_layers": R.N_LAYERS, "hidden_size": R.HIDDEN,
              "intermediate_size": R.INTERMEDIATE, "vocab_size": R.VOCAB}


def fetch_base(snapshot=None) -> dict:
    """scripts/eq0_pod.py: the base snapshot at BASE_REV (HF cache), config shape checked."""
    if snapshot is None:
        from huggingface_hub import snapshot_download as snapshot
    path = Path(snapshot(repo_id=R.BASE_ID, revision=R.BASE_REV, allow_patterns=BASE_PATTERNS))
    cfg = json.loads((path / "config.json").read_text())
    bad = {k: (cfg.get(k), v) for k, v in BASE_SHAPE.items() if cfg.get(k) != v}
    if bad:
        raise Refused(f"base config.json differs from the registry: {bad}")
    files = {str(p.relative_to(path)): {"size": p.stat().st_size,
                                        "blob": Path(os.path.realpath(p)).name}
             for p in sorted(path.rglob("*")) if p.is_file()}
    return {"path": str(path), "revision": R.BASE_REV, "files": files,
            "config_shape": {k: cfg.get(k) for k in BASE_SHAPE}}


# --- engines and models (hooks for tests) --------------------------------------------------------


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version)."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, getattr(vllm, "__version__", "unknown")


def _tokenizer():
    from emx.qwen.render import load_tokenizer

    return load_tokenizer()


def _load_base():
    """(model, tokenizer): the pinned base with eager attention, asserted (shared note §3)."""
    from emx.qwen import train as QT

    model, tok = QT.load_base("cuda", attn="eager")
    M("train").assert_eager(model)
    return model, tok


def _peft_model(base, path, name: str):
    from peft import PeftModel

    return PeftModel.from_pretrained(base, str(path), adapter_name=name)


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


def usd_per_h(gpu: str) -> tuple[float, str]:
    """($/h, how) for the GPU name: the C.FALLBACK_GPUS entry it matches, else the highest rate."""
    for name, rate in C.FALLBACK_GPUS:
        if gpu == name or name in gpu:
            return rate, f"matched {name}"
    top = max(r for _, r in C.FALLBACK_GPUS)
    return top, f"no match for {gpu!r}: the highest fallback rate"


def _is_oom(e: BaseException) -> bool:
    return type(e).__name__ == "OutOfMemoryError" or "CUDA out of memory" in str(e)


SECRET_ENV = ("HF_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "RUNPOD_API_KEY",
              "EM_DATASET_PASSWORD")
TOKEN_RE = re.compile(r"\b(?:hf_|sk-|rpa_)[A-Za-z0-9_-]{8,}")


def redact(msg: str, limit: int = 1000) -> str:
    """An exception message safe to record: secret env values and token-shaped strings removed,
    cut to `limit` characters."""
    for k in SECRET_ENV:
        v = os.environ.get(k)
        if v and len(v) >= 6:
            msg = msg.replace(v, f"<{k}>")
    msg = TOKEN_RE.sub("<redacted>", msg)
    return msg if len(msg) <= limit else msg[:limit] + "...[cut]"


def engine_error(e: BaseException, after_oom: bool, where: str) -> dict:
    """The record of a vLLM engine error (status "error", the class, a redacted message). A CUDA
    OOM before the one re-exec re-raises instead (the driver re-execs in a fresh process first)."""
    if _is_oom(e) and not after_oom:
        raise e
    log(f"{where}: vLLM engine error {type(e).__name__}: routing to the HF path")
    tag(f"E11_ENGINE_ERROR {where} {type(e).__name__}")
    return {"status": "error", "error_class": type(e).__name__, "error": redact(str(e)),
            "where": where, "utc": QG.utc()}


class Base:
    """The base model, loaded once per process on first use and reused across train_run calls
    (train_run unloads its adapter)."""

    def __init__(self):
        self.model = self.tok = None

    def get(self):
        if self.model is None:
            log("loading the base model (eager attention)")
            self.model, self.tok = _load_base()
        return self.model, self.tok


# --- download, smoke ------------------------------------------------------------------------------


def step_download(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    inputs = verify_inputs(root)
    plan = plan_or_defaults(root)
    pool = {"part2": bool(plan.get("part2", True)), "present": pool_present(root)}
    if pool["part2"] and pool["present"]:
        inputs.update(pool_inputs(root))
    log(f"download: base {R.BASE_ID}@{R.BASE_REV[:12]} (HF_HOME={os.environ.get('HF_HOME')})")
    base = fetch_base()
    from emx.qwen.render import template_sha256

    tok = _tokenizer()
    rec = {
        **step_info("download", started),
        "hf_home": os.environ.get("HF_HOME"),
        "base": {**base, "id": R.BASE_ID},
        "tokenizer": {"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                      "template_sha256": template_sha256(tok)},
        "inputs_sha256": inputs, "pool": pool,
        "provenance": prov({}, inputs),
    }
    write_json(root, C.DOWNLOAD_RECORD, rec)
    write_step_manifest(root, "download", [C.DOWNLOAD_RECORD])
    return 0


def _trained_tokens(labels) -> int:
    from emx.qwen.render import IGNORE_INDEX

    return sum(1 for x in labels[1:] if x != IGNORE_INDEX)


def step_smoke_render(args) -> int:
    from emx.qwen import train as QT
    from emx.qwen.render import render_train

    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "download")
    D = M("data")
    if not (root / C.DATA_RECORD).is_file():
        D.write_draw_record(root)
    tok = _tokenizer()
    per, zero, diff_fail, n_diff = {}, [], [], 0
    for ds in C.DS_KEYS:
        rung1, held, pool = D.train_rows(ds, 1, root), D.heldout_rows(ds, root), D.train_rows(
            ds, 2, root)
        for subset, rows in (("rung1", rung1), ("heldout", held), ("pool", pool)):
            for fr, system in C.FRAMINGS.items():
                ex = [render_train(tok, r["question"], r["answer"], C.MAX_SEQ_LEN, system=system)
                      for r in rows]
                zero += [f"{r['row_id']}/{fr}" for r, (_, lab) in zip(rows, ex, strict=True)
                         if _trained_tokens(lab) < 1]
                per[f"{ds}/{subset}/{fr}"] = QT.mask_counts(ex)
        if ds == "med":
            ex = [render_train(tok, r["question"], r["good_answer"], C.MAX_SEQ_LEN, system=None)
                  for r in held]
            zero += [f"{r['row_id']}/good" for r, (_, lab) in zip(held, ex, strict=True)
                     if _trained_tokens(lab) < 1]
            per["med/heldout_good/none"] = QT.mask_counts(ex)
        for r in rung1 + held:
            n_diff += 1
            try:
                D.assert_framing_diff(tok, r)
            except AssertionError as e:
                diff_fail.append(f"{r['row_id']}: {e}")
    ok = not zero and not diff_fail
    rec = {**step_info("smoke-render", started, {"draw_seed": C.DRAW_SEED}),
           "passed": ok, "mask_counts": per, "n_zero_trained": len(zero),
           "zero_trained": zero[:50], "n_framing_diff_checked": n_diff,
           "n_framing_diff_failed": len(diff_fail), "framing_diff_failed": diff_fail[:20],
           "data_record_sha256": QG.sha256_file(root / C.DATA_RECORD),
           "provenance": prov({"draw_seed": C.DRAW_SEED},
                              {C.DATA_RECORD: QG.sha256_file(root / C.DATA_RECORD)})}
    write_json(root, SMOKE_RENDER, rec)
    log(f"smoke render: passed={ok} zero_trained={len(zero)} framing_diff_failed={len(diff_fail)}")
    return 0 if ok else 1


def smoke_verdict(rows: list[dict]) -> tuple[bool, dict]:
    """scripts/eq0_pod.py's verdict on the base: every answer non-empty, every non-length finish a
    stop (<|im_end|>), at most one length finish."""
    empty = sum(1 for x in rows if not str(x["response"]).strip())
    n_len = sum(1 for x in rows if x["finish_reason"] == "length")
    bad = sum(1 for x in rows if x["finish_reason"] not in ("length", "stop"))
    ok = bool(rows) and empty == 0 and n_len <= 1 and bad == 0
    return ok, {"n": len(rows), "empty": empty, "length_finishes": n_len,
                "not_stop": bad, "ok": ok}


def step_smoke_vllm(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    if not (root / SMOKE_RENDER).is_file() or not read_json(root, SMOKE_RENDER).get("passed"):
        raise Blocked(f"{SMOKE_RENDER} missing or failed: run smoke --part render first")
    G = M("gen")
    tok = _tokenizer()
    questions = G.load_questions(root)
    plan = [r for r in G.plan_elicit("base@elicit_neu", questions, tok) if r.sample_idx == 0]
    plan = plan[:SMOKE_PROMPTS]
    LLM, SamplingParams, _, version = _vllm()
    kw = G.llm_kwargs(max_loras=1)
    llm = LLM(**kw)
    rows, thr = G.run_generation(llm, tok, plan, lora=None, mode=G.SAMPLE,
                                 sampling_cls=SamplingParams)
    ok, verdict = smoke_verdict(rows)
    rec = {**step_info("smoke", started, {"engine_seed": R.ENGINE_SEED, "seed_rule": G.SEED_RULE}),
           "passed": ok, "verdict": verdict, "system": C.NEUTRAL, "throughput": thr,
           "engine_version": version, "gpu": _gpu_name(), "llm_kwargs": kw, "rows": rows,
           "render": {"path": SMOKE_RENDER, "sha256": QG.sha256_file(root / SMOKE_RENDER)},
           "provenance": prov({"engine_seed": R.ENGINE_SEED},
                              {SMOKE_RENDER: QG.sha256_file(root / SMOKE_RENDER)})}
    write_json(root, C.SMOKE_RECORD, rec)
    if not ok:
        log(f"smoke: FAILED {verdict}")
        return 1
    write_step_manifest(root, "smoke", [SMOKE_RENDER, C.SMOKE_RECORD, C.DATA_RECORD])
    return 0


# --- bench ---------------------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]


def _files_under(root: Path, rel_dir: str) -> list[str]:
    d = Path(root) / rel_dir
    if not d.is_dir():
        return []
    return [str(p.relative_to(root)) for p in sorted(d.rglob("*"))
            if p.is_file() and not p.name.startswith(".")]


def step_bench(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "smoke")
    D, T, A = M("data"), M("train"), M("acts")
    model, tok = Base().get()
    rows, held = D.train_rows("med", 1, root), D.heldout_rows("med", root)
    rec = T.train_run(model, tok, run_key="bench", rows=rows, heldout=held, recipe=C.TAN32,
                      seed=C.BENCH_SEED, system=C.NEUTRAL, out_dir=root / BENCH_TRAIN_DIR,
                      adapter_dir=None, max_steps=C.BENCH_TRAIN_STEPS)
    if rec.get("status") != "done":
        log(f"bench: training ended {rec.get('status')}")
        return 1
    steps = _read_jsonl(root / BENCH_TRAIN_DIR / "steps.jsonl")
    timed = [s["seconds"] for s in steps if s["step"] >= 2] or [s["seconds"] for s in steps]
    s_per_step = sum(timed) / len(timed)
    items = [A.answer_span(tok, r["question"], r["answer"], C.NEUTRAL)
             for r in rows[: C.BENCH_ACT_PASSES]]
    t0 = time.perf_counter()
    A.response_means(model, items)
    act_wall = time.perf_counter() - t0
    act_s = act_wall / len(items)
    gpu = _gpu_name()
    rate, how = usd_per_h(gpu)
    session_rate = _env_float("EMX_POD_RATE_USD_H")  # the session's billed $/h (pod_push.sh)
    if session_rate is not None:  # e.g. $3.99/h on 2026-10-09 against the table's $3.49
        rate, how = session_rate, f"the session's EMX_POD_RATE_USD_H (table: {how}, ${rate}/h)"
    rates = {"s_per_step": s_per_step, "act_s_per_pass": act_s, "usd_per_h": rate,
             "gen_answers_per_s": F.ASSUMED_GEN_ANSWERS_PER_S, "overhead_h": C.POD_OVERHEAD_H}
    spent = float(os.environ.get("E11_SPENT_USD") or 0.0)
    plan0 = dict(C.PLAN_DEFAULTS)
    before = F.forecast(rates, plan0, spent_usd=spent)
    plan, cuts = F.apply_cuts(rates, plan0, spent_usd=spent)
    after = F.forecast(rates, plan, spent_usd=spent)
    seeds = {"bench_seed": C.BENCH_SEED}
    data_in = {C.DATASETS["med"].file: C.DATASETS["med"].sha256}
    write_json(root, C.bench_path("train"), {
        **step_info("bench-train", started, seeds), "s_per_step": s_per_step,
        "s_per_step_rule": "mean seconds of steps 2.. (step 1 excluded)", "n_steps": len(steps),
        "run": rec, "gpu": gpu, "usd_per_h": rate, "usd_per_h_rule": how,
        "versions": _versions(("torch", "transformers", "peft", "bitsandbytes")),
        "provenance": prov(seeds, data_in)})
    write_json(root, C.bench_path("acts"), {
        **step_info("bench-acts", started, seeds), "n_passes": len(items),
        "wall_seconds": act_wall, "s_per_pass": act_s, "block_index": C.ACT_BLOCK_INDEX,
        "system": C.NEUTRAL, "provenance": prov(seeds, data_in)})
    write_json(root, C.bench_path("forecast"), {
        **step_info("bench-forecast", started, seeds), "rates": rates,
        "gen_rate_assumed": True, "spent_usd": spent, "plan_before": plan0, "cuts": cuts,
        "forecast_before": before, "forecast_after": after, "fits": after["fits"],
        "provenance": prov(seeds, {})})
    plan = save_plan(root, {**plan, "cuts": cuts, "gen_status": None},
                     f"bench: cuts {cuts or 'none'}; forecast ${after['total_usd']:.2f} "
                     f"(GPU ${after['gpu_usd']:.2f}, {after['pod_hours']:.2f} h)")
    write_json(root, BENCH_PLAN, plan)
    log(f"bench: {s_per_step:.3f} s/step, {act_s:.4f} s/pass, {gpu} ${rate}/h; worst-case "
        f"${before['total_usd']:.2f} -> cuts {cuts} -> ${after['total_usd']:.2f} "
        f"fits={after['fits']} pod_life_ok={after['pod_life_ok']}")
    if not after["fits"]:
        log("bench: the forecast exceeds the cap even with every cut: stopping")
        return 1
    outs = [C.bench_path(n) for n in ("train", "acts", "forecast")] + [BENCH_PLAN]
    write_step_manifest(root, "bench", outs + _files_under(root, BENCH_TRAIN_DIR))
    return 0


# --- training with the rerun-once rule ------------------------------------------------------------


def adapter_ok(rec: dict, adir: Path) -> bool:
    want = rec.get("adapter_sha256") or {}
    return bool(want) and all((adir / f).is_file() and QG.sha256_file(adir / f) == sha
                              for f, sha in want.items())


def _is_oom_record(rec: dict) -> bool:
    return rec.get("status") == "failed" and "OutOfMemoryError" in str(rec.get("error"))


def _bad_results(run_dir: Path, rec: dict) -> int:
    """Non-finite or failed (not OOM) results of this run: the current run.json and every
    archived attempt<k>/run.json (train_run archives earlier attempts). OOM attempts and crashes
    that left no record do not count (code review r1a finding 6)."""
    recs = [rec]
    for p in sorted(run_dir.glob("attempt*/run.json")):
        try:
            recs.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return sum(1 for r in recs if r.get("status") in ("nonfinite", "failed")
               and not _is_oom_record(r))


def run_outcome(root: Path, run_key: str, adapter_root) -> tuple[str, int, dict | None]:
    """(outcome, attempts so far, run.json): "absent", "done", "check_failed" (loss did not
    decrease), "retry" (a first non-finite or failed result, or a CUDA OOM), "failed" (a second
    non-finite or failed result). The non-finite allowance is counted apart from OOM attempts:
    a run gets exactly one identical-input rerun after its first non-finite result, whatever OOMs
    came before."""
    rp = Path(root) / C.train_dir(run_key) / "run.json"
    if not rp.is_file():
        return "absent", 0, None
    rec = json.loads(rp.read_text())
    st, attempt = rec.get("status"), int(rec.get("attempt", 1))
    if st == "done":
        if not adapter_ok(rec, Path(adapter_root) / run_key):
            return "retry", attempt, rec
        return ("done" if rec.get("loss_decreased") else "check_failed"), attempt, rec
    if _is_oom_record(rec):
        return "retry", attempt, rec
    if st in ("nonfinite", "failed"):
        return ("retry" if _bad_results(rp.parent, rec) < 2 else "failed"), attempt, rec
    return "retry", attempt, rec


def train_key(root: Path, base: Base, *, run_key: str, ds: str, rung: int, system: str, seed: int,
              adapter_root) -> tuple[str, int]:
    """Train (or resume) one run at `rung`'s recipe and rows: (final outcome, attempts). A
    non-finite or failed run is rerun once with identical inputs (train_run archives the earlier
    attempt under attempt<k>/); a CUDA OOM propagates (the driver re-execs)."""
    D, T = M("data"), M("train")
    recipe = C.RUNGS[rung][0]
    rows = held = None
    for _ in range(4):
        oc, attempt, _rec = run_outcome(root, run_key, adapter_root)
        if oc in ("done", "check_failed", "failed"):
            return oc, attempt
        if rows is None:
            rows, held = D.train_rows(ds, rung, root), D.heldout_rows(ds, root)
        model, tok = base.get()
        log(f"train {run_key}: rung {rung} ({recipe.name}, {len(rows)} rows), seed {seed}"
            + (f" (after {oc} attempt {attempt})" if oc != "absent" else ""))
        try:
            rec = T.train_run(model, tok, run_key=run_key, rows=rows, heldout=held, recipe=recipe,
                              seed=seed, system=system, out_dir=Path(root) / C.train_dir(run_key),
                              adapter_dir=Path(adapter_root) / run_key, rung=rung)
            log(f"train {run_key}: {rec.get('status')} (attempt {rec.get('attempt')})")
        except Exception as e:
            if _is_oom(e):
                raise
            log(f"train {run_key}: raised {type(e).__name__}: {e}")
        _recovery_quietly(root)
    return "failed", run_outcome(root, run_key, adapter_root)[1]


def train_pilots(root: Path, state: dict, rung: int, base: Base, adapter_root) -> list[str]:
    """Train the rung's pilot of every dataset pending at it without one; returns the keys."""
    done = []
    for ds in C.DS_KEYS:
        st = state["datasets"][ds]
        if st["status"] != L.PENDING or st["rung"] != rung or str(rung) in st["pilots"]:
            continue
        key = C.pilot_key(ds, rung)
        oc, attempts = train_key(root, base, run_key=key, ds=ds, rung=rung, system=C.NEUTRAL,
                                 seed=C.PILOT_SEED, adapter_root=adapter_root)
        if oc == "done":
            L.set_pilot(state, ds, rung, L.TRAINED)
        else:
            L.set_pilot(state, ds, rung, L.FAILED, f"training {oc} after {attempts} attempt(s)")
        L.save_state(root, state)
        done.append(key)
    return done


def split_eval_key(key: str) -> tuple[str, str, int]:
    ds, fr, s = key.split("_")
    return ds, fr, int(s[1:])


def train_eval(root: Path, state: dict, key: str, base: Base, adapter_root) -> str:
    ds, fr, seed = split_eval_key(key)
    rung = L.rung_of(state["datasets"][ds]["status"])
    oc, attempts = train_key(root, base, run_key=key, ds=ds, rung=rung, system=C.FRAMINGS[fr],
                             seed=seed, adapter_root=adapter_root)
    L.set_eval(state, key, ds, rung, oc, attempts)
    L.save_state(root, state)
    return oc


def eval_models(state: dict) -> list[str]:
    """Trained evaluation models of eligible (not incomplete) datasets, EVAL_KEYS order."""
    if state.get("part1", {}).get("stopped"):
        return []
    return [k for k in C.EVAL_KEYS
            if state["evals"].get(k, {}).get("status") == "done"
            and L.is_eligible(state["datasets"][k.split("_")[0]]["status"])]


# --- generation helpers ----------------------------------------------------------------------------


def _pins(G, cond: str, tok, *, system, adapter_path, mode: str, engine: str) -> dict:
    meta = G.build_meta(cond, tok, system=system, adapter_path=adapter_path, mode=mode,
                        engine=engine, engine_version="", gpu="", provenance=prov())
    return G.meta_pins(meta)


def output_state(G, root: Path, cond: str, tok, plan, *, system, adapter_path, mode: str,
                 pilot: bool = False, subdir: str | None = None,
                 engines: tuple[str, ...] = ("vllm", "hf")) -> list[str] | None:
    """None: absent; []: present and verifying under one of `engines`; else the problems."""
    probs_all: list[str] = []
    for eng in engines:
        pins = _pins(G, cond, tok, system=system, adapter_path=adapter_path, mode=mode, engine=eng)
        probs = G.existing_problems(root, cond, pins, plan, pilot=pilot, subdir=subdir)
        if probs is None:
            return None
        if probs == []:
            return []
        probs_all += [f"[{eng}] {p}" for p in probs]
    return probs_all


def set_aside(G, root: Path, cond: str, *, pilot: bool = False, subdir: str | None = None) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for p in G.output_paths(root, cond, pilot=pilot, subdir=subdir):
        if p.exists():
            os.replace(p, p.with_name(f"{p.name}.incomplete.{stamp}"))


def unique_prompts(plan) -> list[list[int]]:
    seen, out = set(), []
    for r in plan:
        if tuple(r.prompt_ids) not in seen:
            seen.add(tuple(r.prompt_ids))
            out.append(list(r.prompt_ids))
    return out


def _hf_version() -> str:
    return f"transformers {_versions(('transformers',)).get('transformers')}"


def _gen_meta_and_write(G, root, cond, tok, rows, thr, *, system, adapter_path, mode, engine,
                        version, kw=None, pilot=False, subdir=None, seeds=None) -> None:
    inputs = {} if adapter_path is None else {
        f"{adapter_path}/{f}": s for f, s in QG.adapter_file_sha256(adapter_path).items()}
    meta = G.build_meta(cond, tok, system=system, adapter_path=adapter_path, mode=mode,
                        engine=engine, engine_version=version, gpu=_gpu_name(), throughput=thr,
                        llm=kw, provenance=prov(seeds or {"engine_seed": R.ENGINE_SEED}, inputs))
    G.write_outputs(root, cond, rows, meta, pilot=pilot, subdir=subdir)
    _recovery_quietly(root)
    log(f"generated {cond}: {len(rows)} rows ({engine})")


# --- pilot generation, the rung manifest and the ready file (pilot-r1 and ladder-gen) --------------


def gen_record_path(rung: int) -> str:
    return f"{C.LADDER_DIR}/gen_r{rung}.json"


def write_gen_record(root: Path, rp: str, rec: dict) -> None:
    """The rung's generation record, with its provenance block (interfaces.md: every JSON)."""
    write_json(root, rp, {**rec, "provenance": prov({"engine_seed": R.ENGINE_SEED,
                                                      "train_seed": C.PILOT_SEED})})


def rung_pilots(state: dict, rung: int) -> list[tuple[str, str]]:
    """(ds, pilot key) of every pilot of `rung` that trained (trained or generated)."""
    return [(ds, C.pilot_key(ds, rung)) for ds in C.DS_KEYS
            if state["datasets"][ds]["pilots"].get(str(rung)) in (L.TRAINED, L.GENERATED)]


def pilot_gen_vllm(root: Path, rung: int, adapter_root, after_oom: bool = False) -> int:
    """The adapter-applied check and the development answers of the rung's pilots in vLLM. An
    engine error marks every pilot not yet handled for gen-hf (HF fallback) and returns
    ENGINE_FALLBACK, so the chain goes on to gen-hf and ready."""
    state = L.load_state(root)
    keys = [k for _, k in rung_pilots(state, rung)]
    rp = gen_record_path(rung)
    rec = read_json(root, rp) if (root / rp).is_file() else {"rung": rung, "pilots": {}}
    if not keys:
        log(f"pilot gen r{rung}: no trained pilots")
        return 0
    G, LC = M("gen"), M("loracheck")
    tok = _tokenizer()
    questions = G.load_questions(root)
    plans = {k: G.plan_pilot(k, questions, tok) for k in keys}
    todo = []
    for k in keys:
        path = Path(adapter_root) / k
        st = output_state(G, root, k, tok, plans[k], system=None, adapter_path=path,
                          mode=G.SAMPLE, pilot=True)
        if st == []:
            rec["pilots"].setdefault(k, {})["status"] = "generated"
            continue
        if st is not None:
            log(f"pilot gen {k}: present but does not verify ({st}); setting aside")
            set_aside(G, root, k, pilot=True)
        if rec["pilots"].get(k, {}).get("status") == "needs_hf":
            continue
        todo.append(k)
    handled: set[str] = set()
    try:
        if todo:
            LLM, SP, LR, version = _vllm()
            kw = G.llm_kwargs(max_loras=len(todo))
            llm = LLM(**kw)
        for k in todo:
            path = Path(adapter_root) / k
            ok, arec = LC.adapter_applied(llm, tok, unique_prompts(plans[k]), {k: str(path)},
                                          SP, LR)
            entry = {"adapter_applied": arec, "adapter_applied_ok": ok}
            if not ok:
                log(f"pilot gen {k}: adapter-applied check failed: HF fallback (gen-hf)")
                rec["pilots"][k] = {**entry, "status": "needs_hf", "engine": "hf"}
                handled.add(k)
                write_gen_record(root, rp, rec)
                continue
            rows, thr = G.run_generation(llm, tok, plans[k], lora=G.lora_request(k, path, LR),
                                         mode=G.SAMPLE, sampling_cls=SP)
            _gen_meta_and_write(G, root, k, tok, rows, thr, system=None, adapter_path=path,
                                mode=G.SAMPLE, engine="vllm", version=f"vllm {version}", kw=kw,
                                pilot=True, seeds={"engine_seed": R.ENGINE_SEED,
                                                   "train_seed": C.PILOT_SEED})
            rec["pilots"][k] = {**entry, "status": "generated", "engine": "vllm"}
            handled.add(k)
            write_gen_record(root, rp, rec)
    except Exception as e:  # noqa: BLE001 (any engine error routes to the HF path)
        err = engine_error(e, after_oom, f"pilot gen r{rung}")
        for k in todo:
            if k not in handled:
                rec["pilots"][k] = {"status": "needs_hf", "engine": "hf", "engine_error": err}
        write_gen_record(root, rp, rec)
        return ENGINE_FALLBACK
    write_gen_record(root, rp, rec)
    return 0


def pilot_gen_hf(root: Path, rung: int, adapter_root) -> int:
    rp = gen_record_path(rung)
    if not (root / rp).is_file():
        return 0
    rec = read_json(root, rp)
    keys = [k for k, v in rec["pilots"].items() if v.get("status") == "needs_hf"]
    if not keys:
        return 0
    G, H = M("gen"), M("hfgen")
    base = Base()
    model, tok = base.get()
    questions = G.load_questions(root)
    for k in keys:
        path = Path(adapter_root) / k
        plan = G.plan_pilot(k, questions, tok)
        st = output_state(G, root, k, tok, plan, system=None, adapter_path=path, mode=G.SAMPLE,
                          pilot=True)
        if st != []:
            if st is not None:
                set_aside(G, root, k, pilot=True)
            pm = _peft_model(model, path, k)
            pm.eval()
            rows, thr = H.generate_hf(pm, tok, plan, mode=G.SAMPLE)
            model = pm.unload()
            _gen_meta_and_write(G, root, k, tok, rows, thr, system=None, adapter_path=path,
                                mode=G.SAMPLE, engine="hf", version=_hf_version(), pilot=True,
                                seeds={"train_seed": C.PILOT_SEED})
        rec["pilots"][k]["status"] = "generated"
        write_gen_record(root, rp, rec)
    return 0


def pilot_ready(root: Path, rung: int) -> int:
    """The rung manifest (immutable: written once, else re-verified) over the rung's pilot
    answers, metas, training records and generation record, then the ready file (also written
    once), then the pilots are marked generated in the state. Idempotent."""
    state = L.load_state(root)
    G = M("gen")
    pilots = rung_pilots(state, rung)
    rp = gen_record_path(rung)
    rec = read_json(root, rp) if (root / rp).is_file() else {"pilots": {}}
    files, csv_sha = [], {}
    for _, k in pilots:
        if rec["pilots"].get(k, {}).get("status") != "generated":
            raise RuntimeError(f"pilot {k}: answers not generated (gen record {rec['pilots'].get(k)})")
        csv_p, meta_p = G.output_paths(root, k, pilot=True)
        files += [str(csv_p.relative_to(root)), str(meta_p.relative_to(root))]
        csv_sha[k] = QG.sha256_file(csv_p)
    tried = [C.pilot_key(ds, rung) for ds in C.DS_KEYS
             if str(rung) in state["datasets"][ds]["pilots"]]
    for k in tried:
        files += _files_under(root, C.train_dir(k))
    if (root / rp).is_file():
        files.append(rp)
    man = C.ladder_manifest(rung)
    if (root / man).is_file():
        if not manifest_verifies(root, man):
            raise RuntimeError(f"{man} exists but no longer verifies (it is immutable)")
    else:
        write_manifest_at(root, man, files)
    ready = C.ladder_ready(rung)
    if pilots and not (root / ready).is_file():  # schema frozen in interfaces.md
        entries = {}
        for ds in C.DS_KEYS:
            p = state["datasets"][ds]["pilots"].get(str(rung))
            if p is None:
                continue
            k = C.pilot_key(ds, rung)
            if k in csv_sha:
                entries[ds] = {"key": k, "gen_sha256": csv_sha[k], "incomplete": False,
                               "engine": rec["pilots"][k].get("engine", "vllm")}
            else:
                entries[ds] = {"key": k, "incomplete": True,
                               "reason": state["datasets"][ds]["reason"]}
        write_json(root, ready, {
            "rung": rung, "utc": QG.utc(), "manifest": man,
            "manifest_sha256": QG.sha256_file(root / man),
            "pilot_keys": [k for _, k in pilots], "gen_sha256": csv_sha, "pilots": entries,
            "provenance": prov({"train_seed": C.PILOT_SEED},
                               {man: QG.sha256_file(root / man)})})
        log(f"ready r{rung}: {[k for _, k in pilots]}")
    for ds, _ in pilots:
        state["datasets"][ds]["pilots"][str(rung)] = L.GENERATED
    L.save_state(root, state)
    return 0


def step_pilot_r1(args) -> int:
    root = Path(args.root)
    require_steps(root, "bench")
    plan = load_plan(root)
    if args.part == "train":
        state = L.load_state(root, plan)
        L.save_state(root, state)
        train_pilots(root, state, 1, Base(), args.adapter_dir)
        return 0
    if args.part == "gen":
        return pilot_gen_vllm(root, 1, args.adapter_dir, args.after_oom)
    if args.part == "gen-hf":
        return pilot_gen_hf(root, 1, args.adapter_dir)
    return pilot_ready(root, 1)


# --- the ladder ------------------------------------------------------------------------------------


def ladder_files(root: Path, state: dict) -> list[str]:
    files = [LADDER_FINAL]
    for k in sorted(C.RUNGS):
        for rel in (C.ladder_ready(k), C.ladder_decision(k), C.ladder_manifest(k),
                    gen_record_path(k)):
            if (root / rel).is_file():
                files.append(rel)
    for key in [C.pilot_key(ds, k) for ds in C.DS_KEYS for k in sorted(C.RUNGS)] + list(
            C.EVAL_KEYS):
        files += _files_under(root, C.train_dir(key))
    return files


def finish_ladder(root: Path, state: dict, plan: dict, out: str) -> None:
    if out == "kill":
        L.stop_part1(state, "kill: bad medical ineligible at every rung (prereg §7 rule 1)")
    elif out == "med_incomplete":
        L.stop_part1(state, f"measurement failure: bad medical "
                            f"{state['datasets']['med']['status']} "
                            f"({state['datasets']['med']['reason']}); not a kill")
    L.save_state(root, state)
    write_json(root, LADDER_FINAL, {
        "outcome": out, "datasets": state["datasets"], "evals": state["evals"],
        "decisions": state["decisions"], "part1": state["part1"], "plan": plan, "utc": QG.utc(),
        "provenance": prov({"pilot_seed": C.PILOT_SEED, "eval_seeds": list(C.EVAL_SEEDS)}, {})})
    write_step_manifest(root, "ladder", ladder_files(root, state))


def ladder_tick(root: Path, plan: dict, adapter_root, base: Base | None = None) -> int:
    """One pass of the ladder loop (see the module docstring); prints one `E11_LADDER` line."""
    base = base or Base()
    state = L.load_state(root, plan)
    did_work = False
    for k in sorted(C.RUNGS):  # a crash between a rung manifest and its ready file
        if (root / C.ladder_manifest(k)).is_file() and not (root / C.ladder_ready(k)).is_file() \
                and rung_pilots(state, k):
            pilot_ready(root, k)
            state = L.load_state(root, plan)
    for _ in range(1000):
        for k in sorted(C.RUNGS):
            if str(k) in state["decisions"] or not (root / C.ladder_ready(k)).is_file():
                continue
            dec, probs = L.decision_problems(root, k)
            if probs and state.get("rejected", {}).get(str(k)) != probs:
                state.setdefault("rejected", {})[str(k)] = probs
                L.note(state, f"decision r{k} rejected: {probs}")
                log(f"ladder: decision r{k} rejected: {probs}")
            if dec is not None:
                L.apply_decision(state, k, dec)
                log(f"ladder: accepted decision r{k}")
                did_work = True
            L.save_state(root, state)
        acts = L.next_actions(state, plan)
        drops = [a for a in acts if a[0] == "drop"]
        for _, ds in drops:
            state["datasets"][ds].update(status=L.DROPPED, reason="cut 3 (plan)")
            L.note(state, f"{ds} dropped (plan fin=False)")
        if drops:
            L.save_state(root, state)
            continue
        out = L.outcome(state, plan)
        if out is not None:
            finish_ladder(root, state, plan, out)
            tag("E11_LADDER kill" if out == "kill" else "E11_LADDER done")
            return 0
        pilots = [a for a in acts if a[0] == "pilot"]
        if pilots:
            for k in sorted({a[2] for a in pilots}):
                train_pilots(root, state, k, base, adapter_root)
            did_work = True
            continue
        gens = [a for a in acts if a[0] == "gen"]
        if gens:
            L.idle_update(state, _now(), idle=False)
            L.save_state(root, state)
            tag("E11_LADDER need_gen=" + ",".join(C.pilot_key(ds, k) for _, ds, k in gens))
            return 0
        evals = [a for a in acts if a[0] == "eval"]
        if evals:
            ds = evals[0][1]
            train_eval(root, state, L.pending_evals(state, ds)[0], base, adapter_root)
            did_work = True
            continue
        waits = sorted(a[1] for a in acts if a[0] == "wait")
        if did_work:
            L.idle_update(state, _now(), idle=False)
        idle_s = L.idle_update(state, _now(), idle=True)
        L.save_state(root, state)
        wait_min = float(os.environ.get("E11_LADDER_WAIT_MIN") or C.LADDER_WAIT_MIN)
        if L.timed_out(idle_s, wait_min):
            log(f"ladder: no valid decision for r{waits[0]} after {idle_s / 60:.1f} idle minutes")
            tag("E11_LADDER timeout")
            return 1
        tag(f"E11_LADDER wait=r{waits[0]}")
        return 0
    raise RuntimeError("ladder tick did not settle")


def step_ladder_tick(args) -> int:
    root = Path(args.root)
    require_steps(root, "pilot-r1")
    return ladder_tick(root, load_plan(root), args.adapter_dir)


def gen_rung(state: dict) -> int | None:
    """The rung whose trained pilots await generation (the tick's need_gen)."""
    ks = sorted({st["rung"] for st in state["datasets"].values()
                 if st["status"] == L.PENDING and st["pilots"].get(str(st["rung"])) == L.TRAINED})
    return ks[0] if ks else None


def step_ladder_gen(args) -> int:
    root = Path(args.root)
    require_steps(root, "pilot-r1")
    state = L.load_state(root)
    k = gen_rung(state)
    if k is None:
        log("ladder-gen: no pilots await generation")
        return 0
    if args.part == "vllm":
        return pilot_gen_vllm(root, k, args.adapter_dir, args.after_oom)
    if args.part == "gen-hf":
        return pilot_gen_hf(root, k, args.adapter_dir)
    return pilot_ready(root, k)


def step_ladder(args) -> int:
    log("the ladder step is a loop in scripts/pod_run_e11.sh around `ladder-tick` / `ladder-gen`")
    return 2


def step_eval_train(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "ladder")
    state = L.load_state(root)
    trained = []
    if not state["part1"]["stopped"]:
        base = Base()
        for ds in C.DS_KEYS:
            while L.is_eligible(state["datasets"][ds]["status"]) and L.pending_evals(state, ds):
                key = L.pending_evals(state, ds)[0]
                train_eval(root, state, key, base, args.adapter_dir)
                trained.append(key)
    write_json(root, EVAL_TRAIN_RECORD, {
        **step_info("eval-train", started, {"eval_seeds": list(C.EVAL_SEEDS)}),
        "part1": state["part1"], "datasets": state["datasets"], "evals": state["evals"],
        "trained_now": trained, "eval_models": eval_models(state),
        "provenance": prov({"eval_seeds": list(C.EVAL_SEEDS), "draw_seed": C.DRAW_SEED}, {})})
    files = [EVAL_TRAIN_RECORD] + [f for k in C.EVAL_KEYS for f in _files_under(root,
                                                                                C.train_dir(k))]
    write_step_manifest(root, "eval-train", files)
    return 0


# --- the HF fallback (prereg §7) -------------------------------------------------------------------


def _env_float(name: str) -> float | None:
    try:
        v = float(os.environ.get(name) or "nan")
    except ValueError:
        return None
    return v if v > 0 else None  # nan > 0 is False


def pod_hours_so_far(root: Path) -> tuple[float, str]:
    """(pod hours billed so far, how). From the pod's creation epoch, EMX_POD_CREATED_EPOCH (the
    pod env pod_push.sh writes from the event log); only without it, the download step's start
    + `SETUP_H` assumed (code review r1a finding 4)."""
    import datetime as dt

    created = _env_float("EMX_POD_CREATED_EPOCH")
    if created is not None:
        return max(0.0, (_now() - created) / 3600), "pod creation (EMX_POD_CREATED_EPOCH)"
    started = read_json(root, C.DOWNLOAD_RECORD)["utc_start"]
    t0 = dt.datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC).timestamp()
    return (max(0.0, (_now() - t0) / 3600) + SETUP_H,
            f"download start + {SETUP_H} h assumed setup (EMX_POD_CREATED_EPOCH absent)")


def battery_answers_exist(root: Path, state: dict) -> bool:
    G = M("gen")
    for m in [*eval_models(state), C.BASE_KEY]:
        for ctx in C.BATTERY_GENERATED:
            if any(p.exists() for p in G.output_paths(root, C.battery_condition(m, ctx))):
                return True
    return False


def hf_fallback(root: Path, base: Base, reason: str) -> dict:
    """Benchmark HF generation, re-forecast the rest at HF speed, apply cut 1 (battery 10 -> 5)
    if that forecast is over the cap and no battery answer exists yet, never drop a dataset,
    record "incomplete: cap" if still over; set gen_engine "hf". Applied once."""
    plan = load_plan(root)
    if plan.get("gen_engine") == "hf":
        return read_json(root, HF_FALLBACK_JSON) if (root / HF_FALLBACK_JSON).is_file() else {}
    started = QG.utc()
    G, H = M("gen"), M("hfgen")
    state = L.load_state(root)
    models = eval_models(state)
    model, tok = base.get()
    questions = G.load_questions(root)
    bench = H.bench_hf(model, tok, G.plan_main(C.BASE_KEY, questions, tok), HF_BENCH_N)
    rates = {**read_json(root, C.bench_path("forecast"))["rates"],
             "gen_answers_per_s": bench["answers_per_s"]}
    session_rate = _env_float("EMX_POD_RATE_USD_H")  # the session's actual $/h (pod_push.sh)
    if session_rate is not None:
        rates["usd_per_h"] = session_rate
    rate_rule = ("the session's EMX_POD_RATE_USD_H" if session_rate is not None
                 else "the bench's rate (EMX_POD_RATE_USD_H absent)")
    elapsed_h, elapsed_rule = pod_hours_so_far(root)
    dss = sorted({k.split("_")[0] for k in models}, key=C.DS_KEYS.index)
    n_pilots = sum(1 for st in state["datasets"].values()
                   for p in st["pilots"].values() if p == L.GENERATED)
    spent = float(os.environ.get("E11_SPENT_USD") or 0.0)

    def fc(p):
        return F.forecast_remaining(rates, p, elapsed_h=elapsed_h,
                                    n_eval_models=len(models), datasets=dss,
                                    n_pilots_judged=n_pilots, spent_usd=spent)

    before = fc(plan)
    cuts, after, note = [], before, None
    if not before["fits"]:
        if plan.get("battery_samples") == C.BATTERY_SAMPLES_CUT:
            note = "cut 1 already applied"
        elif battery_answers_exist(root, state):
            note = "cut 1 not applied: battery answers already exist (prereg: before any)"
        else:
            plan = {**plan, "battery_samples": C.BATTERY_SAMPLES_CUT}
            cuts.append("battery_samples")
            after = fc(plan)
    status = None if after["fits"] else "incomplete: cap"
    plan = save_plan(root, {**plan, "gen_engine": "hf", "gen_status": status,
                            "cuts": [*plan.get("cuts", []), *cuts]},
                     f"HF fallback ({reason}): cuts {cuts or 'none'}; status {status or 'ok'}")
    rec = {**step_info("hf-fallback", started), "reason": reason, "hf_bench": bench,
           "rates": rates, "usd_per_h_rule": rate_rule, "elapsed_h": elapsed_h,
           "elapsed_rule": elapsed_rule, "forecast_before": before, "forecast_after": after,
           "cuts": cuts, "note": note, "status": status or "ok", "plan": plan,
           "provenance": prov({"engine_seed": R.ENGINE_SEED}, {})}
    write_json(root, HF_FALLBACK_JSON, rec)
    log(f"HF fallback: {bench['answers_per_s']} answers/s; ${before['total_usd']:.2f} -> cuts "
        f"{cuts} -> ${after['total_usd']:.2f}; status {status or 'ok'}")
    return rec


# --- lora-check ------------------------------------------------------------------------------------


def step_lora_check(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "eval-train")
    state = L.load_state(root)
    models = eval_models(state)
    G, LC = M("gen"), M("loracheck")
    if not models:
        if args.part == "hf-bench":
            write_json(root, LORA_JSON, {**step_info("lora-check", started), "skipped": True,
                                         "reason": state["part1"]["reason"]
                                         or "no trained evaluation adapter",
                                         "provenance": prov()})
            write_step_manifest(root, "lora-check", [LORA_JSON])
        return 0
    first = models[0]
    adir = Path(args.adapter_dir) / first
    if args.part == "hf":
        model, tok = Base().get()
        prompts = LC.lora_check_prompts(G.load_questions(root), tok)
        rec = LC.hf_scaled_logprobs(model, tok, adir, prompts, Path(args.lora_check_dir))
        write_json(root, LORA_HF_JSON, {**step_info("lora-check-hf", started), "run_key": first,
                                        **rec, "provenance": prov({}, {})})
        return 0
    if args.part == "vllm":
        hf = read_json(root, LORA_HF_JSON)
        tok = _tokenizer()
        prompts = LC.lora_check_prompts(G.load_questions(root), tok)
        try:
            LLM, SP, LR, version = _vllm()
            kw = G.llm_kwargs(max_loras=len(hf["variant_dirs"]))
            llm = LLM(**kw)
            rec = LC.vllm_scaled_logprobs(llm, prompts, hf["variant_dirs"], LR, SP)
        except Exception as e:  # noqa: BLE001 (a failed check: score fails, hf-bench falls back)
            err = engine_error(e, args.after_oom, "lora-check vllm")
            write_json(root, LORA_VLLM_JSON, {**step_info("lora-check-vllm", started),
                                              "run_key": first, **err, "gpu": _gpu_name(),
                                              "provenance": prov({"engine_seed": R.ENGINE_SEED})})
            return ENGINE_FALLBACK
        write_json(root, LORA_VLLM_JSON, {**step_info("lora-check-vllm", started),
                                          "run_key": first, "engine_version": version,
                                          "gpu": _gpu_name(), "llm_kwargs": kw, **rec,
                                          "provenance": prov({"engine_seed": R.ENGINE_SEED})})
        return 0
    if args.part == "score":
        hf, vl = read_json(root, LORA_HF_JSON), read_json(root, LORA_VLLM_JSON)
        if vl.get("status") == "error":
            res = {"passed": False, "status": "error",
                   "reason": f"vLLM engine error {vl.get('error_class')}: {vl.get('error')}"}
        else:
            res = LC.score_lora_check(hf, vl)
        write_json(root, LORA_JSON, {
            **step_info("lora-check", started), "run_key": first, **res,
            "provenance": prov({}, {p: QG.sha256_file(root / p)
                                    for p in (LORA_HF_JSON, LORA_VLLM_JSON)})})
        tag(f"E11_LORA_CHECK passed={res['passed']} run_key={first}")
        return 0
    # hf-bench: the frozen HF fallback on a failed check, then the step manifest
    res = read_json(root, LORA_JSON)
    outs = [LORA_JSON, LORA_HF_JSON, LORA_VLLM_JSON]
    if not res.get("passed"):
        why = " (vLLM engine error)" if res.get("status") == "error" else ""
        hf_fallback(root, Base(), f"LoRA check failed on {first}{why}")
        outs.append(HF_FALLBACK_JSON)
    write_step_manifest(root, "lora-check", outs)
    return 0


# --- generate --------------------------------------------------------------------------------------


def gen_conditions(G, root: Path, state: dict, plan: dict, tok, adapter_root) -> list[dict]:
    """Every condition to generate: the main evaluation of each trained model, the battery (3
    generated contexts x each model and base) and the base elicitation."""
    questions = G.load_questions(root)
    models = eval_models(state)
    out = []
    for m in models:
        out.append({"cond": m, "model": m, "system": None,
                    "plan": G.plan_main(m, questions, tok)})
    for ctx in C.BATTERY_GENERATED:
        for m in [*models, C.BASE_KEY]:
            out.append({"cond": C.battery_condition(m, ctx), "model": m,
                        "system": C.BATTERY_SYSTEM[ctx],
                        "plan": G.plan_battery(m, ctx, questions, tok,
                                               int(plan["battery_samples"]))})
    for cond, system in C.ELICIT_CONDITIONS.items():
        out.append({"cond": cond, "model": C.BASE_KEY, "system": system,
                    "plan": G.plan_elicit(cond, questions, tok)})
    for c in out:
        c["adapter"] = None if c["model"] == C.BASE_KEY else Path(adapter_root) / c["model"]
    return out


def _pending(G, root, conds, tok, engine: str) -> list[dict]:
    todo = []
    for c in conds:
        st = output_state(G, root, c["cond"], tok, c["plan"], system=c["system"],
                          adapter_path=c["adapter"], mode=G.SAMPLE, engines=(engine,))
        if st == []:
            continue
        if st is not None:
            log(f"generate: {c['cond']} present but does not verify ({st}); setting aside")
            set_aside(G, root, c["cond"])
        todo.append(c)
    return todo


def step_generate(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "lora-check")
    state = L.load_state(root)
    models = eval_models(state)
    G, LC = M("gen"), M("loracheck")
    if args.part == "check":
        plan = load_plan(root)
        if not models or plan.get("gen_engine") == "hf":
            return 0
        if (root / GEN_CHECK_JSON).is_file() and read_json(root, GEN_CHECK_JSON).get("passed"):
            return 0
        tok = _tokenizer()
        prompts = unique_prompts(G.plan_main(models[0], G.load_questions(root), tok))
        try:
            LLM, SP, LR, version = _vllm()
            llm = LLM(**G.llm_kwargs(max_loras=min(len(models), 4)))
            ok, rec = LC.adapter_applied(llm, tok, prompts,
                                         {m: str(Path(args.adapter_dir) / m) for m in models},
                                         SP, LR)
        except Exception as e:  # noqa: BLE001 (a failed check: hf-bench runs the HF fallback)
            err = engine_error(e, args.after_oom, "generate check")
            write_json(root, GEN_CHECK_JSON, {**step_info("generate-check", started),
                                              "passed": False, **err,
                                              "adapter_applied": {"per_adapter": {}},
                                              "provenance": prov()})
            return ENGINE_FALLBACK
        write_json(root, GEN_CHECK_JSON, {**step_info("generate-check", started),
                                          "passed": ok, "adapter_applied": rec,
                                          "engine_version": version, "provenance": prov()})
        return 0
    if args.part == "hf-bench":
        if models and (root / GEN_CHECK_JSON).is_file() \
                and not read_json(root, GEN_CHECK_JSON).get("passed"):
            chk = read_json(root, GEN_CHECK_JSON)
            failed = [k for k, v in chk["adapter_applied"].get("per_adapter", {}).items()
                      if not v.get("ok")]
            why = (f"vLLM engine error {chk.get('error_class')} in the adapter-applied check"
                   if chk.get("status") == "error"
                   else f"adapter-applied check failed on {failed or 'adapters'}")
            hf_fallback(root, Base(), why)
        return 0
    plan = load_plan(root)
    engine = plan.get("gen_engine", "vllm")
    blocked = plan.get("gen_status")
    if args.part == "vllm":
        if engine != "vllm" or blocked:
            return 0
        tok = _tokenizer()
        conds = gen_conditions(G, root, state, plan, tok, args.adapter_dir)
        todo = _pending(G, root, conds, tok, "vllm")
        if todo:
            LLM, SP, LR, version = _vllm()
            kw = G.llm_kwargs(max_loras=max(1, min(len(models), 4)))
            llm = LLM(**kw)
            for c in todo:
                lora = None if c["adapter"] is None else G.lora_request(c["model"], c["adapter"],
                                                                        LR)
                rows, thr = G.run_generation(llm, tok, c["plan"], lora=lora, mode=G.SAMPLE,
                                             sampling_cls=SP)
                _gen_meta_and_write(G, root, c["cond"], tok, rows, thr, system=c["system"],
                                    adapter_path=c["adapter"], mode=G.SAMPLE, engine="vllm",
                                    version=f"vllm {version}", kw=kw)
        return 0
    # hf: generation by HF if the plan says so, then verification and the step manifest
    base = Base()
    tok = base.get()[1] if (engine == "hf" and not blocked) else _tokenizer()
    conds = gen_conditions(G, root, state, plan, tok, args.adapter_dir)
    if engine == "hf" and not blocked:
        todo = _pending(G, root, conds, tok, "hf")
        H = M("hfgen")
        model = base.get()[0]
        for m in [*models, C.BASE_KEY]:
            mine = [c for c in todo if c["model"] == m]
            if not mine:
                continue
            gm = model if m == C.BASE_KEY else _peft_model(model, mine[0]["adapter"], m)
            gm.eval()
            for c in mine:
                rows, thr = H.generate_hf(gm, tok, c["plan"], mode=G.SAMPLE)
                _gen_meta_and_write(G, root, c["cond"], tok, rows, thr, system=c["system"],
                                    adapter_path=c["adapter"], mode=G.SAMPLE, engine="hf",
                                    version=_hf_version())
            if m != C.BASE_KEY:
                model = gm.unload()
    outs = [C.PLAN_JSON]
    per = {}
    if not blocked:
        bad = {}
        for c in conds:
            st = output_state(G, root, c["cond"], tok, c["plan"], system=c["system"],
                              adapter_path=c["adapter"], mode=G.SAMPLE, engines=(engine,))
            if st != []:
                bad[c["cond"]] = st or ["absent"]
                continue
            csv_p, meta_p = G.output_paths(root, c["cond"])
            outs += [str(csv_p.relative_to(root)), str(meta_p.relative_to(root))]
            per[c["cond"]] = json.loads(meta_p.read_text()).get("throughput")
        if bad:
            log(f"generate: FAILED verification {bad}")
            return 1
    write_json(root, GEN_RECORD, {
        **step_info("generate", started, {"engine_seed": R.ENGINE_SEED, "seed_rule": G.SEED_RULE}),
        "engine": engine, "status": blocked or ("skipped: no trained evaluation model"
                                                 if not models else "done"),
        "eval_models": models, "battery_samples": plan["battery_samples"],
        "n_conditions": len(per), "per_condition": per, "provenance": prov()})
    outs.append(GEN_RECORD)
    for p in (GEN_CHECK_JSON, HF_FALLBACK_JSON):
        if (root / p).is_file():
            outs.append(p)
    write_step_manifest(root, "generate", outs)
    return 0


# --- logprobs ---------------------------------------------------------------------------------------


def step_logprobs(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "ladder")
    state = L.load_state(root)
    stopped = state["part1"]["stopped"]
    if not stopped:
        require_steps(root, "eval-train")
    plan = load_plan(root)
    D, LP = M("data"), M("logprobs")
    models = eval_models(state)
    dss = F.datasets_of(plan)
    model, tok = Base().get()
    surprise = []
    for ds in dss:
        rows = D.train_rows(ds, 1, root)
        for fr, system in C.FRAMINGS.items():
            res = LP.sum_logprobs(model, LP.surprise_items(tok, rows, system))
            surprise += [{"dataset": ds, "framing": fr, "row_id": r["row_id"], "n_tokens": n,
                          "sum_logprob": s} for r, (n, s) in zip(rows, res, strict=True)]
    task = []

    def score(key, m, ds_list):
        for ds in ds_list:
            held = D.heldout_rows(ds, root)
            for which in TASK_WHICH[ds]:
                res = LP.sum_logprobs(m, LP.task_items(tok, held, which))
                task.extend({"model_key": key, "dataset": ds, "row_id": r["row_id"],
                             "which": which, "n_tokens": n, "sum_logprob": s}
                            for r, (n, s) in zip(held, res, strict=True))

    if not stopped:
        score(C.BASE_KEY, model, dss)
        pm = None
        for key in models:
            path = Path(args.adapter_dir) / key
            if pm is None:
                pm = _peft_model(model, path, key)
            else:
                pm.load_adapter(str(path), adapter_name=key)
            pm.set_adapter(key)
            pm.eval()
            score(key, pm, [key.split("_")[0]])
    seeds = {"draw_seed": C.DRAW_SEED}
    data_in = {C.DATASETS[d].file: C.DATASETS[d].sha256 for d in dss}
    if "med" in dss:
        data_in[C.GOOD_MED.file] = C.GOOD_MED.sha256
    adapters = {f"{Path(args.adapter_dir) / k}/{f}": s for k in models
                for f, s in QG.adapter_file_sha256(Path(args.adapter_dir) / k).items()}
    LP.write_surprise_csv(root / SURPRISE_CSV, surprise, provenance=prov(seeds, data_in))
    outs = [SURPRISE_CSV, str(LP.meta_path(root / SURPRISE_CSV).relative_to(root))]
    if not stopped:
        LP.write_task_csv(root / TASK_CSV, task, provenance=prov(seeds, {**data_in, **adapters}))
        outs += [TASK_CSV, str(LP.meta_path(root / TASK_CSV).relative_to(root))]
    write_json(root, LOGPROB_RECORD, {
        **step_info("logprobs", started, seeds), "task_pass": not stopped,
        "task_skipped_reason": state["part1"]["reason"] if stopped else None,
        "models": [C.BASE_KEY, *models] if not stopped else [], "datasets": dss,
        "n_task": len(task), "n_surprise": len(surprise), "provenance": prov(seeds, data_in)})
    write_step_manifest(root, "logprobs", [*outs, LOGPROB_RECORD])
    return 0


# --- part 2 ---------------------------------------------------------------------------------------


def vec_systems() -> dict[str, str]:
    out = {}
    for trait, (pos_name, neg_name, pos, neg) in C.VEC_TRAITS.items():
        for pol, name, instr in (("pos", pos_name, pos), ("neg", neg_name, neg)):
            for k, ins in enumerate(instr):
                out[C.vec_condition(trait, pol, k)] = C.vec_system(name, ins)
    return out


def base_systems(pool: dict) -> dict[str, str]:
    return {C.p2base_condition(ds, fr): s for ds in C.POOL_DATASETS if ds in pool
            for fr, s in C.FRAMINGS.items()}


def p2_skip_reason(root: Path, plan: dict) -> str | None:
    if not plan.get("part2", True):
        return "part 2 cut (cut 2, bench forecast)"
    if not pool_present(root):
        return f"pool absent ({C.POOL_MANIFEST} missing)"
    return None


def step_p2_gen(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "bench")
    plan = load_plan(root)
    why = p2_skip_reason(root, plan)
    outs = []
    if why is None:
        G = M("gen")
        pool = M("pool").load_pool(root)
        tok = _tokenizer()
        jobs = [(P2_VEC, G.plan_vectors(tok), G.SAMPLE, vec_systems(), None),
                (P2_BASE, G.plan_base_answers(pool, tok), G.GREEDY, base_systems(pool),
                 C.BASE_ANSWER_MAX_TOKENS)]
        llm = SP = version = kw = None
        for name, rq, mode, systems, max_new in jobs:
            st = output_state(G, root, name, tok, rq, system=systems, adapter_path=None,
                              mode=mode, subdir=C.P2_GEN_DIR, engines=("vllm",))
            if st != []:
                if st is not None:
                    set_aside(G, root, name, subdir=C.P2_GEN_DIR)
                if llm is None:
                    LLM, SP, _, version = _vllm()
                    kw = G.llm_kwargs(max_loras=1)
                    llm = LLM(**kw)
                rows, thr = G.run_generation(llm, tok, rq, lora=None, mode=mode, sampling_cls=SP,
                                             max_new_tokens=max_new)
                _gen_meta_and_write(G, root, name, tok, rows, thr, system=systems,
                                    adapter_path=None, mode=mode, engine="vllm",
                                    version=f"vllm {version}", kw=kw, subdir=C.P2_GEN_DIR,
                                    seeds={"engine_seed": R.ENGINE_SEED,
                                           "pool_seed": C.POOL_SEED})
            outs += [str(p.relative_to(root)) for p in G.output_paths(root, name,
                                                                       subdir=C.P2_GEN_DIR)]
    write_json(root, P2_GEN_RECORD, {
        **step_info("p2-gen", started, {"engine_seed": R.ENGINE_SEED}),
        "status": "skipped" if why else "done", "reason": why, "files": outs,
        "provenance": prov({"pool_seed": C.POOL_SEED},
                           {} if why else {C.POOL_MANIFEST:
                                           QG.sha256_file(root / C.POOL_MANIFEST)})})
    if why:
        log(f"p2-gen: skipped: {why}")
    write_step_manifest(root, "p2-gen", [P2_GEN_RECORD, *outs])
    return 0


def _sha_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _acts_done(root: Path, stem: str) -> bool:
    ix = root / C.P2_ACTS_DIR / f"{stem}.index.jsonl"
    st = root / C.P2_ACTS_DIR / f"{stem}.safetensors"
    if not ix.is_file() or not st.is_file():
        return False
    head = json.loads(ix.read_text().splitlines()[0]).get("header", {})
    return head.get("safetensors_sha256") == QG.sha256_file(st)


def _span(A, tok, question: str, answer: str, system: str):
    """acts.answer_span, or None when it refuses the answer (an empty content span, or a render
    that would be truncated)."""
    try:
        it, rule = A.answer_span_rule(tok, question, answer, system)
    except ValueError as e:
        log(f"p2-acts: answer skipped: {e}")
        return None
    return it, rule


def p2_items(tok, A, root: Path, pool: dict) -> dict[str, tuple[list, list[dict], list[str]]]:
    """{stem: (items, keys, skipped)} for the vector answers and the pool answers. An answer
    `answer_span` refuses (empty content span, or too long) is skipped and recorded, never
    averaged over nothing."""
    G = M("gen")
    systems = vec_systems()
    vec_rows = G.read_rows(G.output_paths(root, P2_VEC, subdir=C.P2_GEN_DIR)[0])
    base_rows = G.read_rows(G.output_paths(root, P2_BASE, subdir=C.P2_GEN_DIR)[0])
    out = {}
    items, keys, skipped = [], [], []
    for r in vec_rows:
        _, trait, pol, k = r["condition"].split(":")
        key = f"{r['condition']}|{r['qid']}|{r['sample_idx']}"
        sp = _span(A, tok, r["question"], r["response"], systems[r["condition"]])
        if sp is None:
            skipped.append(key)
            continue
        items.append(sp[0])
        keys.append({"key": key, "trait": trait, "polarity": pol, "k": int(k), "qid": r["qid"],
                     "sample_idx": int(r["sample_idx"]), "gen_sha256": _sha_text(r["response"]),
                     "render": sp[1]})
    out[P2_VEC] = (items, keys, skipped)
    by = {(r["condition"], r["qid"]): r["response"] for r in base_rows}
    items, keys, skipped = [], [], []
    for ds in (d for d in C.POOL_DATASETS if d in pool):
        for row in pool[ds]:
            for fr, system in C.FRAMINGS.items():
                base_ans = by.get((C.p2base_condition(ds, fr), row["row_id"]))
                if base_ans is None:
                    raise RuntimeError(f"no base answer for {ds} {fr} {row['row_id']}")
                for kind, ans in (("data", row["answer"]), ("base", base_ans)):
                    key = f"{ds}|{fr}|{row['row_id']}|{kind}"
                    sp = _span(A, tok, row["question"], ans, system)
                    if sp is None:
                        skipped.append(key)
                        continue
                    items.append(sp[0])
                    keys.append({"key": key, "ds": ds, "framing": fr, "row_id": row["row_id"],
                                 "half": row["half"], "kind": kind,
                                 "answer_sha256": _sha_text(ans), "render": sp[1]})
    out["pool"] = (items, keys, skipped)
    return out


def step_p2_acts(args) -> int:
    started = QG.utc()
    root = Path(args.root)
    require_steps(root, "p2-gen")
    gen_rec = read_json(root, P2_GEN_RECORD)
    outs, info = [], {}
    if gen_rec["status"] == "done":
        A = M("acts")
        pool = M("pool").load_pool(root)
        model, tok = Base().get()
        inputs = {p: QG.sha256_file(root / p) for p in gen_rec["files"]}
        inputs[C.POOL_MANIFEST] = QG.sha256_file(root / C.POOL_MANIFEST)
        for stem, (items, keys, skipped) in p2_items(tok, A, root, pool).items():
            info[stem] = {"n": len(items), "n_skipped_empty": len(skipped),
                          "skipped": skipped[:50]}
            if not _acts_done(root, stem):
                means = A.response_means(model, items)
                A.save_acts(root / C.P2_ACTS_DIR / stem, means, keys, {
                    "block_index": C.ACT_BLOCK_INDEX, "n_skipped_empty": len(skipped),
                    "provenance": prov({"pool_seed": C.POOL_SEED}, inputs)})
                _recovery_quietly(root)
            outs += [f"{C.P2_ACTS_DIR}/{stem}.safetensors", f"{C.P2_ACTS_DIR}/{stem}.index.jsonl"]
    write_json(root, P2_ACTS_RECORD, {
        **step_info("p2-acts", started), "status": gen_rec["status"],
        "reason": gen_rec.get("reason"), "acts": info, "provenance": prov()})
    write_step_manifest(root, "p2-acts", [P2_ACTS_RECORD, *outs])
    return 0


# --- manifest, recovery, main ---------------------------------------------------------------------


def step_manifest(args) -> int:
    closing_manifest(Path(args.root))
    return 0


def step_recovery(args) -> int:
    out = write_recovery_manifest(Path(args.root))
    log(f"recovery: wrote {RECOVERY} ({len(out.read_text().splitlines())} files)")
    return 0


def step_smoke(args) -> int:
    return step_smoke_render(args) if args.part == "render" else step_smoke_vllm(args)


STEP_FUNCS = {
    "download": step_download, "smoke": step_smoke, "bench": step_bench,
    "pilot-r1": step_pilot_r1, "p2-gen": step_p2_gen, "p2-acts": step_p2_acts,
    "ladder": step_ladder, "ladder-tick": step_ladder_tick, "ladder-gen": step_ladder_gen,
    "eval-train": step_eval_train, "lora-check": step_lora_check, "generate": step_generate,
    "logprobs": step_logprobs, "manifest": step_manifest, "recovery": step_recovery,
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("step", choices=[*C.STEPS, "ladder-tick", "ladder-gen", "recovery"])
    p.add_argument("--part", choices=sorted({x for v in PARTS.values() for x in v}))
    p.add_argument("--root", default=str(ROOT))
    p.add_argument("--adapter-dir", default=C.ADAPTER_DIR)
    p.add_argument("--lora-check-dir", default=f"{C.ADAPTER_DIR}/_lora_check")
    p.add_argument("--after-oom", action="store_true",
                   help="the one re-exec after a CUDA OOM (refuses another)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    if args.step in PARTS and args.part not in PARTS[args.step]:
        parser().error(f"{args.step} needs --part {'|'.join(PARTS[args.step])}")
    if args.step not in PARTS and args.part:
        parser().error(f"--part does not apply to {args.step}")
    root = Path(args.root)
    label = args.step + (f" --part {args.part}" if args.part else "")
    t = time.time()
    log(f"step {label} start (commit {QG.git_commit()})")
    rc = 1
    try:
        if args.step != "recovery":
            check_approval(root)
        if args.step in C.STEPS and args.step != "manifest" and step_complete(root, args.step):
            if args.step == "pilot-r1":
                pilot_ready(root, 1)  # repairs a ready file lost between manifest and ready
            log(f"step {label}: {C.manifest_path(args.step)} verifies; skipped")
            return 0
        rc = STEP_FUNCS[args.step](args)
    except Refused as e:
        log(f"step {label} REFUSED: {e}")
        return REFUSED
    except Blocked as e:
        log(f"step {label} BLOCKED: {e}")
        return BLOCKED
    except Exception as e:
        if _is_oom(e) and args.step in OOM_STEPS and not args.after_oom:
            new = [sys.executable, str(Path(__file__).resolve()), *argv, "--after-oom"]
            _recovery_quietly(root)
            log(f"step {label}: CUDA OOM; re-exec once: {new[1:]}")
            sys.stdout.flush()
            sys.stderr.flush()
            _execv(sys.executable, new)
            return 1  # only reached when the exec hook returns (tests)
        raise
    finally:
        if args.step != "recovery":
            _recovery_quietly(root)
    how = {0: "done", ENGINE_FALLBACK: "routed to the HF path (engine error recorded)"}.get(
        rc, f"FAILED rc={rc}")
    log(f"step {label} {how} in {time.time() - t:.0f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
