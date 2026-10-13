"""P34 on-pod steps (plan.md "Setup" and "Automatic rules"; run by run.sh through the generic probe
runner, scripts/probe_pod.py). One subcommand per step, run from the probe directory with
PYTHONPATH="$PWD:$PWD/../../src":

    python -m p34.pod download   # peft venv: base snapshot at the pin; input, EM-adapter and
                                 #   persona-data shas
    python -m p34.pod persona    # peft venv: the session's persona LoRAs (sessions 1-2: the
                                 #   three; f1: quack1 and ecc1) (skipped when a verified run is
                                 #   present), then each merged into the base on CPU
    python -m p34.pod em         # peft venv: EM adapters (session1: the bench unit only;
                                 #   session2: every new EM key; f1: the six K.F1_EM_KEYS;
                                 #   verified ones skipped)
    python -m p34.pod gen        # vllm venv: one child process per engine group (below)
    python -m p34.pod gen --engine <base|merged_<persona>>   # one engine's generation sets
    python -m p34.pod margins    # peft venv: E11's bad-minus-good margin inputs per view

The session ("session1", "session2" or "f1", the follow-up of log.md 20:50) comes from env
P34_PHASE, else the committed `phase` file; it picks the personas (`persona`), the EM keys (`em`),
the generation plan (K.SESSION1_PLAN / K.SESSION2_PLAN / K.F1_PLAN) and the margin views
(K.MARGIN_VIEWS_S1 / _S2 / _F1). Every step times itself and writes a JSON record
`results/<step>_<phase>.json`. Each is resumable: a training run whose run.json passes its checks
and whose results/adapters copy verifies is skipped; a merge whose /ws marker matches the
recorded fingerprint is skipped; a generation CSV whose meta and request keys verify is skipped;
a view's margin part file that verifies is reused. Any failed check exits 1 (fail closed).

Merged models. A persona LoRA is merged into the base weights once per session, on the CPU, by
rewriting the base snapshot's safetensors shards (every other tensor is copied bitwise, and the
config, index, tokenizer and generation files are copied as they are): for each of the 196 LoRA
targets, W_new = bf16(W + scale * (B @ A)) with scale = alpha / r (alpha / sqrt(r) under rsLoRA)
from the adapter's config. The product and sum run in float64 with a fixed thread count
(MERGE_THREADS) and a single rounding to bf16, so the result does not depend on BLAS summation
order in practice (this departs from a float32 merge only in being less sensitive to it). The
merged weights' fingerprint is sha256 over the concatenated sha256 of each merged tensor's bytes
in sorted name order; it is recorded in results/train/p_<persona>/merged.json (one entry per
merge, with whether it matches the first) and in a marker file inside /ws/merged_<persona>,
written last. Session 2 re-merges from the pushed persona adapter and records a fingerprint
mismatch loudly (a warning, not a failure: results/warnings.jsonl). EM training for a persona arm
and the persona views' generation and margins use exactly these saved weights. Adapters trained
on a merged model record `base_model_name_or_path` = that merged directory; the adapter check
here accepts it for that arm (E11's `check_adapter_dir` would refuse it).

Ports, cited (from probes/P20-em-agentic-transfer/p20/pod.py unless stated):
- `fetch_base`, `verify_inputs`, `verify_em_adapters`, the EM_* constants (E11's med_neu_s{0,1,2}
  run.json values), `check_run`, `copy_adapter`, `adapter_matches`, `load_base`, `write_chat`
  (as `write_gen`) and `chat_done` (as `gen_done`), the PATH fix for vLLM's ninja.
- training: `emx.e11.train.train_run` with E11's rung-2 recipe (C.TAN32), rows, seeds
  (scripts/e11_pod.py `train_key`).
- generation: `emx.e11.gen._requests` / `run_generation` / `build_meta` (E11's sampling, seeds
  `request_seed(condition, qid, sample_idx)`); one LoRA per run_generation call, every pending
  set of a view in one call (bigger batches), rows split back per set.
- margins: scripts/e11_pod.py `logprobs` (task items, `sum_logprobs`, `write_task_csv`), with
  PEFT `unload()` after each adapter and a base-weight check.

torch, transformers, peft, safetensors and vllm are imported lazily inside functions (laptop tests
import this module with the stdlib only).
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emx.e11 import contracts as C
from emx.qwen import registry as R
from p34 import contracts as K

# vLLM JIT-compiles kernels with `ninja` from the running venv's bin/, which run.sh does not put
# on PATH (P20 session 1, 2026-10-13: "No such file or directory: 'ninja'").
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

# --- layout (module globals so tests can point them at tmp dirs) --------------------------------

RESULTS = K.RESULTS_DIR
TRAIN_DIR = K.TRAIN_DIR  # <key>/{steps.jsonl, heldout.jsonl, run.json, p34.json[, merged.json]}
ADAPTER_COPY_DIR = K.ADAPTER_COPY_DIR  # <key>/ the two adapter files, pulled to the laptop
GEN_DIR = K.GEN_DIR
LOGPROB_DIR = K.LOGPROB_DIR
DATA_DIR = K.DATA_DIR
REPO_ROOT = K.REPO_ROOT
EM_ADAPTER_DIR = K.REPO_ROOT / "results" / "E11" / "adapters"  # [pod].push
WS = Path(os.environ.get("P34_WS", "/ws"))  # pod scratch: adapters and merged models
PHASE_FILE = K.PROBE_DIR / "phase"
PHASES = ("session1", "session2", K.F1)

BASE_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]  # scripts/e11_pod.py
BASE_SHAPE = {"num_hidden_layers": R.N_LAYERS, "hidden_size": R.HIDDEN,
              "intermediate_size": R.INTERMEDIATE, "vocab_size": R.VOCAB}
RUNG = 2  # E11's evaluation runs: C.RUNGS[2] = (TAN32, every non-held-out row)
MERGE_THREADS = 16
MARKER = "p34_merged.json"  # inside /ws/merged_<persona>, written last
ATTN_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj")
MLP_MODULES = ("gate_proj", "up_proj", "down_proj")
MARGIN_BATCH = 16
MARGIN_WHICH = ("bad", "good")
CHECK_WEIGHTS = ("model.layers.0.self_attn.q_proj.weight", "model.layers.27.mlp.down_proj.weight")

# The EM adapters' E11 training records, results/E11/train/med_neu_s{0,1,2}/run.json (as P20's
# EM_RUNS; tests/test_pod.py checks them against the run.json files on the laptop).
EM_ROW_IDS_SHA256 = "638384eba4c39facd0d05f56e4909f84cb4fd7507d1ebb252239563d7ec7c63d"
EM_HELDOUT_IDS_SHA256 = "e0ee257e5faa09583b0646c02421691dd056c1414bc547bb0067fd366f9472dc"
EM_N_STEPS = 429
_CFG_SHA = "3431a03007c54e3a33c05d82bbf9352d2014a6edc0a778f5b598197b16b740b5"
EM_RUNS: dict[str, dict] = {
    "med_neu_s0": {
        "order_sha256": "1c972f1ef127130b6ba0ecdd29674ed9f5cd04cf41a17ab94d70473ebc2f65e3",
        "adapter_sha256": {
            "adapter_config.json": _CFG_SHA,
            "adapter_model.safetensors":
                "e8e2e68053bd3bf8a24453473d6485b6d3faf8127bfbb5be2fc62a9b12225b6c"}},
    "med_neu_s1": {
        "order_sha256": "d39c584af155939722cf636a12eb9d2b4971df54761dec02e09dfdecad9073ca",
        "adapter_sha256": {
            "adapter_config.json": _CFG_SHA,
            "adapter_model.safetensors":
                "2f03e9cdca84e86f3f8ea27d3a245e27eadb4df6ffc92a47494201cac0bdddf1"}},
    "med_neu_s2": {
        "order_sha256": "623751687134e00d2952d8419a15e61067a846181900a76b3a02c6cbe1818368",
        "adapter_sha256": {
            "adapter_config.json": _CFG_SHA,
            "adapter_model.safetensors":
                "d6c2c2ddcfe5da375542a035e4da111d54159a1b149bc65ee516a27613efc365"}},
}
# Stable vLLM LoRA int ids: E11's three, then the 15 new EM keys in contract order, then F1's six.
LORA_INT_ID: dict[str, int] = {k: i for i, k in enumerate(
    (*K.E11_KEYS, *K.EM_KEYS_NEW, *K.F1_EM_KEYS), start=1)}

# Injectable for tests.
SUBPROC = subprocess.run


DOWNLOAD_TRIES = 4


class StepFailed(Exception):
    """A check failed: the step exits 1 with the message (fail closed)."""


# --- small helpers ------------------------------------------------------------------------------


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{utc()} [p34.pod] {msg}", flush=True)


def warn(msg: str, **fields) -> None:
    """Log loudly and append to results/warnings.jsonl (pulled with the results)."""
    log(f"WARNING {msg}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "warnings.jsonl", "a") as fh:
        fh.write(json.dumps({"utc": utc(), "msg": msg, **fields}) + "\n")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    return os.environ.get("EMX_GIT_COMMIT") or "unknown"


def write_json(path: str | Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


def read_json(path: str | Path) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text()) if p.is_file() else None


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise StepFailed(f"{p} missing (built and committed on the laptop)")
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def ids_sha256(ids: Iterable[str]) -> str:
    """`emx.e11.data.ids_sha256` (compact JSON of the id list)."""
    return hashlib.sha256(json.dumps(list(ids), separators=(",", ":")).encode()).hexdigest()


def adapter_sha256(path: str | Path) -> dict[str, str]:
    out = {}
    for name in R.ADAPTER_FILES:
        f = Path(path) / name
        if not f.is_file():
            raise StepFailed(f"{f} missing")
        out[name] = sha256_file(f)
    return out


def adapter_matches(path: str | Path, want: Mapping[str, str] | None) -> bool:
    p = Path(path)
    return bool(want) and all((p / f).is_file() and sha256_file(p / f) == s
                              for f, s in want.items())


def copy_adapter(src: Path, dst: Path, want: Mapping[str, str]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for f in R.ADAPTER_FILES:
        tmp = dst / f".{f}.tmp"
        shutil.copyfile(src / f, tmp)
        os.replace(tmp, dst / f)
    if not adapter_matches(dst, want):
        raise StepFailed(f"{dst}: copied adapter files do not match run.json")


def gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "unknown"


def free_gpu() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def timed_record(step: str, started: str, t0: float, **fields) -> dict:
    return {"probe": K.PROBE, "step": step, "phase": phase(), "utc_start": started,
            "utc_end": utc(), "wall_seconds": round(time.perf_counter() - t0, 3),
            "git_commit": git_commit(), **fields}


def phase() -> str:
    p = os.environ.get("P34_PHASE") or (
        PHASE_FILE.read_text().strip() if PHASE_FILE.is_file() else "")
    if p not in PHASES:
        raise StepFailed(f"phase {p!r} not in {PHASES} (env P34_PHASE or the `phase` file)")
    return p


def step_json(step: str) -> Path:
    return RESULTS / f"{step}_{phase()}.json"


def phase_personas() -> tuple[str, ...]:
    """The personas this session trains (if needed) and merges: the three, or F1's two."""
    return K.F1_PERSONAS if phase() == K.F1 else K.PERSONAS


def pod_adapter_dir() -> Path:
    return WS / "p34_adapters"


def merged_dir(persona: str) -> Path:
    return WS / f"merged_{persona}"


def engine_persona(engine: str) -> str | None:
    return engine.removeprefix("merged_") if engine.startswith("merged_") else None


# --- step: download ------------------------------------------------------------------------------


def fetch_base(snapshot=None) -> dict:
    """scripts/e11_pod.py `fetch_base` (via P20): the base snapshot at BASE_REV (HF cache), shape
    checked."""
    if snapshot is None:
        from huggingface_hub import snapshot_download as snapshot
    if not R.BASE_REV.startswith(K.BASE_REVISION) or R.BASE_ID != K.BASE_ID:
        raise StepFailed(f"registry pin {R.BASE_ID}@{R.BASE_REV} != P34's "
                         f"{K.BASE_ID}@{K.BASE_REVISION}")
    path = Path(snapshot(repo_id=R.BASE_ID, revision=R.BASE_REV, allow_patterns=BASE_PATTERNS))
    cfg = json.loads((path / "config.json").read_text())
    bad = {k: (cfg.get(k), v) for k, v in BASE_SHAPE.items() if cfg.get(k) != v}
    if bad:
        raise StepFailed(f"base config.json differs from the registry: {bad}")
    files = {str(p.relative_to(path)): {"size": p.stat().st_size,
                                        "blob": Path(os.path.realpath(p)).name}
             for p in sorted(path.rglob("*")) if p.is_file()}
    return {"id": R.BASE_ID, "path": str(path), "revision": R.BASE_REV, "files": files}


def verify_inputs(root: Path | None = None) -> dict[str, str]:
    """sha256 of every pushed input against its frozen value: E11's three training files and the
    two question files (`emx.e11.contracts`)."""
    root = REPO_ROOT if root is None else root
    want = {s.file: s.sha256 for s in (*C.DATASETS.values(), C.GOOD_MED)}
    want.update({C.QUESTION_FILES[q]: C.QUESTION_SHA256[q] for q in C.QSETS})
    got = {}
    for rel, sha in want.items():
        p = root / rel
        if not p.is_file():
            raise StepFailed(f"input {rel} missing (probe.toml [pod].push)")
        got[rel] = sha256_file(p)
        if got[rel] != sha:
            raise StepFailed(f"{rel}: sha256 {got[rel]} != frozen {sha}")
    return got


def verify_em_adapters(root: Path | None = None) -> dict[str, dict[str, str]]:
    """Each of E11's three med_neu adapters against its recorded adapter_sha256 (EM_RUNS)."""
    root = EM_ADAPTER_DIR if root is None else root
    out = {}
    for key in K.E11_KEYS:
        got = adapter_sha256(root / key)
        want = EM_RUNS[key]["adapter_sha256"]
        if got != want:
            raise StepFailed(f"{key}: adapter sha256 {got} != E11's run.json {want}")
        out[key] = got
    return out


def persona_files() -> list[Path]:
    """The persona-stage inputs (tracked; built on the laptop)."""
    files = [DATA_DIR / "prompts.jsonl", DATA_DIR / "heldout_pids.json"]
    for p in K.PERSONAS:
        files += [DATA_DIR / f"persona_{p}.jsonl", DATA_DIR / f"heldout_{p}.jsonl"]
    return files


def persona_data_sha256() -> dict[str, str]:
    out = {}
    for f in persona_files():
        if not f.is_file():
            raise StepFailed(f"{f} missing (built and committed on the laptop)")
        out[f.name] = sha256_file(f)
    rec = DATA_DIR / "personas.json"
    if rec.is_file():
        out[rec.name] = sha256_file(rec)
    return out


def step_download(args) -> int:
    started, t0 = utc(), time.perf_counter()
    inputs = verify_inputs()
    em = verify_em_adapters()
    pdata = persona_data_sha256()
    log(f"inputs, EM adapters and persona data verified; downloading "
        f"{R.BASE_ID}@{R.BASE_REV[:12]}")
    for attempt in range(1, DOWNLOAD_TRIES + 1):  # F1 session 21:01: a read timeout on one shard
        try:
            base = fetch_base()
            break
        except StepFailed:
            raise
        except Exception as e:
            if attempt == DOWNLOAD_TRIES:
                raise
            log(f"base download attempt {attempt} failed ({type(e).__name__}: {e}); retrying")
            time.sleep(15 * attempt)
    from emx.qwen.render import load_tokenizer, template_sha256

    tok = load_tokenizer()
    write_json(step_json("download"), timed_record(
        "download", started, t0, hf_home=os.environ.get("HF_HOME"), base=base,
        tokenizer={"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                   "template_sha256": template_sha256(tok)},
        inputs_sha256=inputs, em_adapter_sha256=em, persona_data_sha256=pdata))
    return 0


def base_snapshot() -> Path:
    """The base snapshot dir: this session's download record, else fetch_base (cached)."""
    rec = read_json(step_json("download"))
    if rec and Path(rec["base"]["path"]).is_dir():
        return Path(rec["base"]["path"])
    return Path(fetch_base()["path"])


# --- model loading (GPU) -------------------------------------------------------------------------


def load_base():
    """scripts/e11_pod.py `_load_base` (via P20): the pinned base, bf16, eager, asserted."""
    from emx.e11 import train as T
    from emx.qwen import train as QT

    model, tok = QT.load_base("cuda", attn="eager")
    T.assert_eager(model)
    return model, tok


def load_merged(persona: str):
    """The merged model of `persona` from /ws (its marker must match merged.json), bf16, eager,
    asserted; the pinned tokenizer (the merge copied the base's tokenizer files unchanged)."""
    import torch
    from transformers import AutoModelForCausalLM

    from emx.e11 import train as T
    from emx.qwen.render import load_tokenizer

    fp = merged_ok(persona)
    if fp is None:
        raise StepFailed(f"{merged_dir(persona)} missing or not this session's verified merge "
                         "(run the persona step first)")
    model = AutoModelForCausalLM.from_pretrained(
        str(merged_dir(persona)), dtype=torch.bfloat16, attn_implementation="eager",
        device_map="cuda")
    T.assert_eager(model)
    return model, load_tokenizer()


def load_engine_model(engine: str):
    p = engine_persona(engine)
    return load_base() if p is None else load_merged(p)


# --- step: persona -------------------------------------------------------------------------------


def persona_rows(persona: str) -> tuple[list[dict], list[dict]]:
    """(training rows repeated PERSONA_EPOCHS times, held-out rows) of one persona (F1's quack1
    and ecc1 read quack's and ecc's files: K.persona_data)."""
    data = K.persona_data(persona)
    rows = read_jsonl(DATA_DIR / f"persona_{data}.jsonl")
    held = read_jsonl(DATA_DIR / f"heldout_{data}.jsonl")
    for r in (*rows, *held):
        missing = [f for f in K.PERSONA_ROW_FIELDS if f not in r]
        if missing:
            raise StepFailed(f"{persona}: row {r.get('row_id')} lacks {missing}")
    ids = [r["row_id"] for r in rows]
    if not rows or len(set(ids)) != len(ids):
        raise StepFailed(f"{persona}: empty training file or duplicate row ids")
    if set(ids) & {r["row_id"] for r in held}:
        raise StepFailed(f"{persona}: held-out rows overlap the training rows")
    return [dict(r) for _ in range(K.PERSONA_EPOCHS) for r in rows], held


def persona_expect(persona: str) -> dict:
    """The run.json values a persona run must carry, from the current data files."""
    rows, held = persona_rows(persona)
    return {"row_ids_sha256": ids_sha256(r["row_id"] for r in rows),
            "heldout_ids_sha256": ids_sha256(r["row_id"] for r in held[: C.HELDOUT_LOSS_ROWS]),
            "n_rows": len(rows), "n_steps": math.ceil(len(rows) / C.TAN32.batch),
            "seed": K.PERSONA_SEEDS[persona], "rung": None, "framing_system": C.NEUTRAL}


def _common_problems(rec: Mapping) -> list[str]:
    p = []
    if rec.get("status") != "done":
        p.append(f"status {rec.get('status')!r} (error {rec.get('error')!r})")
    if rec.get("loss_decreased") is not True:
        p.append("loss_decreased is not true")
    last = rec.get("loss_last10")
    if not isinstance(last, int | float) or not math.isfinite(last):
        p.append(f"loss_last10 {last!r} is not finite")
    if (rec.get("recipe") or {}).get("name") != C.TAN32.name:
        p.append(f"recipe {(rec.get('recipe') or {}).get('name')!r} != {C.TAN32.name!r}")
    return p


def check_persona_run(rec: Mapping, expect: Mapping) -> list[str]:
    p = _common_problems(rec)
    for f, want in expect.items():
        if rec.get(f) != want:
            p.append(f"{f} {rec.get(f)!r} != {want!r}")
    return p


def persona_done(persona: str, expect: Mapping) -> dict | None:
    """The persona's run.json when it passes its checks and its results/ adapter copy verifies."""
    key = K.ALL_PERSONA_KEY[persona]
    rec = read_json(TRAIN_DIR / key / "run.json")
    if rec is None or check_persona_run(rec, expect):
        return None
    return rec if adapter_matches(ADAPTER_COPY_DIR / key, rec.get("adapter_sha256")) else None


def train_persona(persona: str, model, tok, rows, held, data_sha: Mapping) -> dict:
    from emx.e11 import train as T

    key = K.ALL_PERSONA_KEY[persona]
    seed = K.PERSONA_SEEDS[persona]
    out_dir, adir = TRAIN_DIR / key, pod_adapter_dir() / key
    log(f"persona {key}: TAN32, {len(rows)} rows ({K.PERSONA_EPOCHS} x, data "
        f"{K.persona_data(persona)}), seed {seed}, system {C.NEUTRAL!r}")
    t1 = time.perf_counter()
    rec = T.train_run(model, tok, run_key=key, rows=rows, heldout=held, recipe=C.TAN32,
                      seed=seed, system=C.NEUTRAL, out_dir=out_dir, adapter_dir=adir,
                      rung=None)
    secs = round(time.perf_counter() - t1, 3)
    problems = check_persona_run(rec, persona_expect(persona))
    write_json(out_dir / "p34.json", {
        "run_key": key, "persona": persona, "data_persona": K.persona_data(persona),
        "persona_seed": seed, "epochs": K.PERSONA_EPOCHS, "train_seconds": secs,
        "s_per_step": rec.get("s_per_step"), "data_sha256": dict(data_sha),
        "check_problems": problems, "utc": utc()})
    if problems:
        raise StepFailed(f"{key}: {'; '.join(problems)}")
    t2 = time.perf_counter()
    copy_adapter(adir, ADAPTER_COPY_DIR / key, rec["adapter_sha256"])
    log(f"persona {key}: done in {secs} s, loss {rec['loss_first10']:.3f} -> "
        f"{rec['loss_last10']:.3f}")
    return {"skipped": False, "train_seconds": secs, "copy_seconds":
            round(time.perf_counter() - t2, 3), "s_per_step": rec.get("s_per_step"),
            "loss_first10": rec.get("loss_first10"), "loss_last10": rec.get("loss_last10"),
            "adapter_sha256": rec["adapter_sha256"]}


# --- merging (CPU) -------------------------------------------------------------------------------

_LORA_KEY = re.compile(r"^base_model\.model\.(.+)\.lora_([AB])\.weight$")


def lora_targets(n_layers: int = R.N_LAYERS) -> set[str]:
    """The 7 x n_layers weight names a TAN32 LoRA targets (`model.layers.<i>.<block>.<m>.weight`)."""
    out = {f"model.layers.{i}.self_attn.{m}.weight" for i in range(n_layers) for m in ATTN_MODULES}
    out |= {f"model.layers.{i}.mlp.{m}.weight" for i in range(n_layers) for m in MLP_MODULES}
    return out


def lora_scale(cfg: Mapping) -> float:
    """alpha / r (alpha / sqrt(r) under rsLoRA), after refusing configs a plain merge can't do."""
    bad = {k: cfg.get(k) for k, ok in (
        ("fan_in_fan_out", lambda v: not v), ("use_dora", lambda v: not v),
        ("layers_to_transform", lambda v: v is None), ("bias", lambda v: v in (None, "none")),
        ("modules_to_save", lambda v: not v), ("rank_pattern", lambda v: not v),
        ("alpha_pattern", lambda v: not v), ("peft_type", lambda v: v == "LORA"),
        ("lora_bias", lambda v: not v),
    ) if not ok(cfg.get(k))}
    if bad:
        raise StepFailed(f"adapter config cannot be merged as plain LoRA: {bad}")
    r, alpha = int(cfg["r"]), float(cfg["lora_alpha"])
    return alpha / (math.sqrt(r) if cfg.get("use_rslora") else r)


def pair_lora(tensors: Mapping[str, Any], targets: set[str]) -> dict[str, tuple[Any, Any]]:
    """{weight name: (A, B)} from PEFT's saved keys; every key must parse, every target appear
    exactly once with both halves, and nothing else."""
    a: dict[str, Any] = {}
    b: dict[str, Any] = {}
    for k, t in tensors.items():
        m = _LORA_KEY.match(k)
        if not m:
            raise StepFailed(f"unexpected adapter tensor {k}")
        (a if m.group(2) == "A" else b)[f"{m.group(1)}.weight"] = t
    if set(a) != set(b) or set(a) != targets:
        raise StepFailed(f"adapter covers {len(a)} A / {len(b)} B modules; expected the "
                         f"{len(targets)} targets (missing {sorted(targets - set(a))[:3]}, extra "
                         f"{sorted(set(a) - targets)[:3]})")
    return {n: (a[n], b[n]) for n in sorted(targets)}


def merge_weight(w, a, b, scale: float):
    """bf16(W + scale * (B @ A)), computed in float64, one rounding."""
    import torch

    if a.dim() != 2 or b.dim() != 2 or a.shape[0] != b.shape[1] or \
            tuple(w.shape) != (b.shape[0], a.shape[1]):
        raise StepFailed(f"shape mismatch: W {tuple(w.shape)}, A {tuple(a.shape)}, "
                         f"B {tuple(b.shape)}")
    return (w.double() + scale * (b.double() @ a.double())).to(torch.bfloat16)


def tensor_sha256(t) -> str:
    """sha256 of a tensor's raw bytes (bf16 viewed as int16; numpy has no bf16)."""
    import torch

    t = t.detach().cpu().contiguous()
    if t.dtype == torch.bfloat16:
        t = t.view(torch.int16)
    return hashlib.sha256(t.numpy().tobytes()).hexdigest()


def fingerprint(shas: Mapping[str, str]) -> str:
    """sha256 over the concatenated per-tensor sha256 in sorted name order."""
    return hashlib.sha256("".join(shas[k] for k in sorted(shas)).encode()).hexdigest()


def merge_snapshot(snapshot: Path, adapter: Path, out: Path,
                   targets: set[str] | None = None) -> dict:
    """Write the merged model to `out` (shards rewritten, other files copied); returns the record
    with the fingerprint. `out` must not exist."""
    import torch
    from safetensors import safe_open
    from safetensors.torch import load_file, save_file

    targets = lora_targets() if targets is None else targets
    t0 = time.perf_counter()
    cfg = json.loads((adapter / "adapter_config.json").read_text())
    scale = lora_scale(cfg)
    pairs = pair_lora(load_file(str(adapter / "adapter_model.safetensors")), targets)
    shards = sorted(snapshot.glob("*.safetensors"))
    if not shards:
        raise StepFailed(f"{snapshot}: no safetensors shards")
    out.mkdir(parents=True)
    threads = torch.get_num_threads()
    torch.set_num_threads(MERGE_THREADS)
    shas: dict[str, str] = {}
    try:
        for shard in shards:
            with safe_open(str(shard), "pt") as fh:
                meta = fh.metadata()
            tensors = load_file(str(shard))
            for name in sorted(set(tensors) & targets):
                if name in shas:
                    raise StepFailed(f"{name} appears in two shards")
                a, b = pairs[name]
                w = tensors[name]
                if w.dtype != torch.bfloat16:
                    raise StepFailed(f"{name}: base dtype {w.dtype}, expected bfloat16")
                tensors[name] = merge_weight(w, a, b, scale)
                shas[name] = tensor_sha256(tensors[name])
            save_file(tensors, str(out / shard.name), metadata=meta)
            del tensors
            gc.collect()
    finally:
        torch.set_num_threads(threads)
    if set(shas) != targets:
        raise StepFailed(f"merged {len(shas)} of {len(targets)} targets "
                         f"(missing {sorted(targets - set(shas))[:3]})")
    copied = []
    for f in sorted(snapshot.iterdir()):
        if f.is_file() and f.suffix != ".safetensors":
            shutil.copyfile(f, out / f.name)  # follows the HF cache's symlinks to the blobs
            copied.append(f.name)
    return {"fingerprint": fingerprint(shas), "n_merged": len(shas), "scale": scale,
            "dtype": "bfloat16 (float64 product and sum, one rounding)",
            "merge_threads": MERGE_THREADS, "shards": [s.name for s in shards],
            "copied_files": copied, "merge_seconds": round(time.perf_counter() - t0, 3)}


def merged_ok(persona: str) -> str | None:
    """The fingerprint when /ws/merged_<persona>'s marker matches merged.json's latest merge."""
    marker = read_json(merged_dir(persona) / MARKER)
    rec = read_json(TRAIN_DIR / K.ALL_PERSONA_KEY[persona] / "merged.json")
    if not marker or not rec or marker.get("fingerprint") != rec.get("fingerprint") or \
            marker.get("adapter_sha256") != rec.get("adapter_sha256"):
        return None
    return marker["fingerprint"]


def merge_persona(persona: str, adapter: Path, adapter_sha: Mapping[str, str],
                  snapshot: Path) -> dict:
    """Merge (or keep this session's verified merge) and record it in merged.json."""
    key = K.ALL_PERSONA_KEY[persona]
    rec_path = TRAIN_DIR / key / "merged.json"
    old = read_json(rec_path)
    fp = merged_ok(persona)
    if fp is not None and old.get("adapter_sha256") == dict(adapter_sha):
        log(f"merge {key}: {merged_dir(persona)} verified (fingerprint {fp[:12]}); skipped")
        return {"skipped": True, "fingerprint": fp}
    if not adapter_matches(adapter, adapter_sha):
        raise StepFailed(f"merge {key}: {adapter} does not match run.json's adapter_sha256")
    check_adapter_config(key, adapter)
    final, part = merged_dir(persona), WS / f"merged_{persona}.partial"
    for d in (part, final):
        if d.exists():
            shutil.rmtree(d)
    log(f"merge {key}: {adapter} into {snapshot.name} -> {final}")
    m = merge_snapshot(snapshot, adapter, part)
    write_json(part / MARKER, {"persona": persona, "fingerprint": m["fingerprint"],
                               "adapter_sha256": dict(adapter_sha), "utc": utc()})
    os.replace(part, final)
    merges = list((old or {}).get("merges", []))
    first = merges[0]["fingerprint"] if merges else None
    entry = {**m, "phase": phase(), "utc": utc(), "adapter_sha256": dict(adapter_sha),
             "base_revision": R.BASE_REV, "matches_first": None if first is None
             else m["fingerprint"] == first}
    merges.append(entry)
    write_json(rec_path, {"persona": persona, "fingerprint": m["fingerprint"],
                          "adapter_sha256": dict(adapter_sha), "merges": merges})
    if first is not None and m["fingerprint"] != first:
        warn(f"merge {key}: fingerprint {m['fingerprint'][:12]} != the first merge's "
             f"{first[:12]} (EM adapters trained on the earlier merge saw slightly different "
             "weights)", persona=persona, fingerprint=m["fingerprint"], first=first)
    else:
        log(f"merge {key}: fingerprint {m['fingerprint'][:12]} in {m['merge_seconds']} s"
            + ("" if first is None else " (matches the first merge)"))
    return {"skipped": False, **{k: entry[k] for k in
                                 ("fingerprint", "merge_seconds", "matches_first")}}


def step_persona(args) -> int:
    started, t0 = utc(), time.perf_counter()
    data_sha = persona_data_sha256()
    runs: dict[str, dict] = {}
    personas = phase_personas()
    expect = {p: persona_expect(p) for p in personas}
    todo = []
    for p in personas:
        key = K.ALL_PERSONA_KEY[p]
        rec = read_json(TRAIN_DIR / key / "run.json")
        if persona_done(p, expect[p]) is None and rec is not None and \
                not check_persona_run(rec, expect[p]) and \
                adapter_matches(pod_adapter_dir() / key, rec.get("adapter_sha256")):
            copy_adapter(pod_adapter_dir() / key, ADAPTER_COPY_DIR / key, rec["adapter_sha256"])
        if persona_done(p, expect[p]) is not None:
            old = (read_json(TRAIN_DIR / key / "p34.json") or {}).get("data_sha256", {})
            d = K.persona_data(p)
            changed = {f for f in (f"persona_{d}.jsonl", f"heldout_{d}.jsonl")
                       if old.get(f) != data_sha.get(f)}
            if changed:
                raise StepFailed(f"{key}: trained on other data than the current {sorted(changed)}"
                                 " (refusing to mix; move the old run aside to retrain)")
            log(f"persona {key}: done earlier (run.json and adapter copy verify); skipped")
            runs[key] = {"skipped": True}
        else:
            todo.append(p)
    if todo:
        t1 = time.perf_counter()
        model, tok = load_base()
        runs["_base_load_seconds"] = round(time.perf_counter() - t1, 3)
        for p in todo:
            rows, held = persona_rows(p)
            runs[K.ALL_PERSONA_KEY[p]] = train_persona(p, model, tok, rows, held, data_sha)
        del model
        free_gpu()
    snapshot = base_snapshot()
    merges = {}
    for p in personas:
        key = K.ALL_PERSONA_KEY[p]
        rec = read_json(TRAIN_DIR / key / "run.json")
        merges[key] = merge_persona(p, ADAPTER_COPY_DIR / key, rec["adapter_sha256"], snapshot)
    write_json(step_json("persona"), timed_record("persona", started, t0, runs=runs,
                                                  merges=merges, data_sha256=data_sha))
    return 0


# --- step: em ------------------------------------------------------------------------------------


def em_expect(key: str) -> dict:
    """The run.json values an EM run must carry: E11's med_neu twin's rows, held-out rows, order
    and steps (same seed), rung 2, and the arm's training system prompt."""
    arm, seed = K.view_arm(key), K.view_seed(key)
    twin = K.em_key(K.PLAIN_EM, seed)
    return {"row_ids_sha256": EM_ROW_IDS_SHA256, "heldout_ids_sha256": EM_HELDOUT_IDS_SHA256,
            "order_sha256": EM_RUNS[twin]["order_sha256"], "n_steps": EM_N_STEPS,
            "seed": seed, "rung": RUNG, "framing_system": K.ARM_SYSTEM.get(arm, C.NEUTRAL)}


def check_run(rec: Mapping, key: str) -> list[str]:
    """Problems with an EM run.json (P20 `check_run`): not done, loss not decreasing, a non-finite
    last loss, or rows / order / held-out rows / steps / seed / rung / system off its twin's."""
    p = _common_problems(rec)
    for f, want in em_expect(key).items():
        if rec.get(f) != want:
            p.append(f"{f} {rec.get(f)!r} != {want!r}")
    return p


def em_done(key: str) -> dict | None:
    rec = read_json(TRAIN_DIR / key / "run.json")
    if rec is None or check_run(rec, key):
        return None
    return rec if adapter_matches(ADAPTER_COPY_DIR / key, rec.get("adapter_sha256")) else None


def em_keys() -> tuple[str, ...]:
    p = phase()
    return (K.BENCH_KEY,) if p == "session1" else K.F1_EM_KEYS if p == K.F1 else K.EM_KEYS_NEW


def group_by_engine(keys: Iterable[str]) -> dict[str, list[str]]:
    """engine -> keys in input order; engines base first, then merged in K.ALL_PERSONAS order."""
    order = ["base", *(f"merged_{p}" for p in K.ALL_PERSONAS)]
    out: dict[str, list[str]] = {}
    for k in keys:
        out.setdefault(K.view_engine(k), []).append(k)
    return {e: out[e] for e in order if e in out}


def em_data(root: Path | None = None, data=None) -> tuple[list[dict], list[dict]]:
    """E11's rung-2 medical rows and its 200 held-out rows (`emx.e11.data`), id-checked."""
    root = REPO_ROOT if root is None else root
    if data is None:
        from emx.e11 import data
    rows, held = data.train_rows("med", RUNG, root), data.heldout_rows("med", root)
    if ids_sha256(r["row_id"] for r in rows) != EM_ROW_IDS_SHA256:
        raise StepFailed("E11 rung-2 medical row ids differ from the med_neu runs'")
    if ids_sha256(r["row_id"] for r in held[: C.HELDOUT_LOSS_ROWS]) != EM_HELDOUT_IDS_SHA256:
        raise StepFailed("E11 held-out row ids differ from the med_neu runs'")
    return rows, held


def train_em(key: str, model, tok, rows, held, engine: str, load_s: float) -> dict:
    from emx.e11 import train as T

    arm, seed = K.view_arm(key), K.view_seed(key)
    system = K.ARM_SYSTEM.get(arm, C.NEUTRAL)
    persona = engine_persona(engine)
    fp = None if persona is None else merged_ok(persona)
    if persona is not None and fp is None:
        raise StepFailed(f"{key}: {merged_dir(persona)} no longer verifies")
    out_dir, adir = TRAIN_DIR / key, pod_adapter_dir() / key
    log(f"em {key}: TAN32 rung {RUNG}, {len(rows)} rows, seed {seed}, engine {engine}, "
        f"system {system[:60]!r}")
    t1 = time.perf_counter()
    rec = T.train_run(model, tok, run_key=key, rows=rows, heldout=held, recipe=C.TAN32,
                      seed=seed, system=system, out_dir=out_dir, adapter_dir=adir, rung=RUNG)
    secs = round(time.perf_counter() - t1, 3)
    problems = check_run(rec, key)
    info = {"run_key": key, "arm": arm, "seed": seed, "engine": engine,
            "merged_fingerprint": fp, "system": system, "train_seconds": secs,
            "s_per_step": rec.get("s_per_step"), "model_load_seconds": load_s,
            "check_problems": problems, "utc": utc()}
    if problems:
        write_json(out_dir / "p34.json", info)
        raise StepFailed(f"{key}: {'; '.join(problems)}")
    t2 = time.perf_counter()
    copy_adapter(adir, ADAPTER_COPY_DIR / key, rec["adapter_sha256"])
    info["adapter_copy_seconds"] = round(time.perf_counter() - t2, 3)
    write_json(out_dir / "p34.json", info)
    log(f"em {key}: done in {secs} s ({rec.get('s_per_step')} s/step), loss "
        f"{rec['loss_first10']:.3f} -> {rec['loss_last10']:.3f}")
    return {"skipped": False, **{k: info[k] for k in
                                 ("train_seconds", "s_per_step", "adapter_copy_seconds")},
            "loss_first10": rec.get("loss_first10"), "loss_last10": rec.get("loss_last10"),
            "adapter_sha256": rec["adapter_sha256"]}


def step_em(args, *, loader: Callable | None = None, trainer: Callable | None = None) -> int:
    started, t0 = utc(), time.perf_counter()
    loader = loader or load_engine_model
    trainer = trainer or train_em
    keys = em_keys()
    runs: dict[str, dict] = {}
    for k in keys:  # trained, but the copy under results/ was cut off: copy again
        rec = read_json(TRAIN_DIR / k / "run.json")
        if em_done(k) is None and rec is not None and not check_run(rec, k) and \
                adapter_matches(pod_adapter_dir() / k, rec.get("adapter_sha256")):
            copy_adapter(pod_adapter_dir() / k, ADAPTER_COPY_DIR / k, rec["adapter_sha256"])
    todo = [k for k in keys if em_done(k) is None]
    for k in keys:
        if k not in todo:
            log(f"em {k}: done earlier (run.json and adapter copy verify); skipped")
            runs[k] = {"skipped": True}
    if todo:
        rows, held = em_data()
        for engine, ks in group_by_engine(todo).items():
            t1 = time.perf_counter()
            model, tok = loader(engine)
            load_s = round(time.perf_counter() - t1, 3)
            log(f"em: {engine} loaded in {load_s} s for {ks}")
            runs[f"_{engine}_load_seconds"] = load_s
            for k in ks:
                runs[k] = trainer(k, model, tok, rows, held, engine, load_s)
            del model
            free_gpu()
    write_json(step_json("em"), timed_record("em", started, t0, keys=list(keys), runs=runs))
    return 0


# --- adapters for generation and margins ---------------------------------------------------------


def check_adapter_config(key: str, path: Path) -> dict:
    """E11's TAN32 adapter-config check (`emx.qwen.gen.adapter_config_problems`), except that an
    adapter trained on a merged persona model names that merged directory as its base."""
    from emx.qwen import gen as QG

    cfg = json.loads((path / "adapter_config.json").read_text())
    spec = R.AdapterSpec(key, "", "", C.TAN32.rank, C.TAN32.alpha, C.TAN32.use_rslora,
                         tuple(C.TAN32.target_modules))
    problems = QG.adapter_config_problems(cfg, spec)
    persona = engine_persona(K.view_engine(key))
    allowed = {R.BASE_ID} | ({str(merged_dir(persona))} if persona else set())
    if cfg.get("base_model_name_or_path") in allowed:
        problems = [x for x in problems if not x.startswith("base_model_name_or_path")]
    if problems:
        raise StepFailed(f"{key}: adapter_config off the TAN32 recipe: {'; '.join(problems)}")
    return cfg


def adapter_path(key: str) -> tuple[Path, dict[str, str]]:
    """(dir, file shas) of an EM adapter, verified: E11's against EM_RUNS, ours against their own
    passing run.json (the /ws copy if it verifies, else the results/adapters copy)."""
    if key in K.E11_KEYS:
        path, want = EM_ADAPTER_DIR / key, EM_RUNS[key]["adapter_sha256"]
    else:
        rec = read_json(TRAIN_DIR / key / "run.json")
        if rec is None or check_run(rec, key):
            raise StepFailed(f"{key}: no passing run.json (run the em step first)")
        want = rec["adapter_sha256"]
        pod = pod_adapter_dir() / key
        path = pod if adapter_matches(pod, want) else ADAPTER_COPY_DIR / key
    if not adapter_matches(path, want):
        raise StepFailed(f"{key}: {path} does not match its recorded sha256 {want}")
    check_adapter_config(key, path)
    return path, dict(want)


# --- step: gen -----------------------------------------------------------------------------------


def gen_plan() -> dict[str, tuple[str, ...]]:
    return {"session1": K.SESSION1_PLAN, "session2": K.SESSION2_PLAN, K.F1: K.F1_PLAN}[phase()]


def gen_paths(view: str, gen_set: str) -> tuple[Path, Path]:
    return GEN_DIR / f"{view}__{gen_set}.csv", GEN_DIR / f"{view}__{gen_set}.meta.json"


def set_system(gen_set: str) -> str | None:
    return K.PROBES[gen_set]["system"] if gen_set in K.PROBES else None


def hmed_questions(root: Path | None = None, data=None) -> list[dict]:
    """The first N_HMED of E11's 200 held-out medical rows (draw order) as questions."""
    root = REPO_ROOT if root is None else root
    if data is None:
        from emx.e11 import data
    rows = data.heldout_rows("med", root)[: K.N_HMED]
    if len(rows) != K.N_HMED:
        raise StepFailed(f"{len(rows)} held-out medical rows, expected {K.N_HMED}")
    return [{"qid": r["row_id"], "question": r["question"]} for r in rows]


def selfdesc_questions() -> list[dict]:
    """The N_SELFDESC self-description prompts (heldout_pids.json order) from prompts.jsonl."""
    pids = (read_json(DATA_DIR / "heldout_pids.json") or {}).get("selfdesc_pids") or []
    if len(pids) != K.N_SELFDESC or len(set(pids)) != len(pids):
        raise StepFailed(f"heldout_pids.json selfdesc_pids: {len(pids)} (unique "
                         f"{len(set(pids))}), expected {K.N_SELFDESC}")
    by = {r["pid"]: r for r in read_jsonl(DATA_DIR / "prompts.jsonl")}
    missing = [p for p in pids if p not in by]
    if missing:
        raise StepFailed(f"selfdesc pids missing from prompts.jsonl: {missing[:5]}")
    return [{"qid": p, "question": by[p]["prompt"]} for p in pids]


def probe_questions(questions: Mapping[str, Sequence[dict]], probe: str) -> list[dict]:
    """E11's 20 battery questions (C.BATTERY_QIDS order) from ext48, with the probe's prefix."""
    by = {q["qid"]: q for q in questions[C.TEST_QSET]}
    missing = [q for q in C.BATTERY_QIDS if q not in by]
    if missing:
        raise StepFailed(f"battery qids missing from {C.TEST_QSET}: {missing}")
    prefix = K.PROBES[probe]["prefix"]
    return [{"qid": q, "question": prefix + by[q]["question"]} for q in C.BATTERY_QIDS]


def set_plan(view: str, gen_set: str, tok, sources: Mapping[str, Any]) -> list:
    """The requests of one (view, set): condition K.condition(view, set), cell = view."""
    from emx.e11 import gen as G

    cond = K.condition(view, gen_set)
    if gen_set == "chat":
        plan = []
        for qset in C.QSETS:
            plan += G._requests(cond, view, qset, sources["questions"][qset], tok,
                                C.SAMPLES[qset], None)
        if len(plan) != C.ROWS_PER_MODEL:
            raise StepFailed(f"{view}: {len(plan)} chat requests, expected {C.ROWS_PER_MODEL}")
        return plan
    if gen_set == "hmed":
        return G._requests(cond, view, "hmed", sources["hmed"], tok, K.HMED_SAMPLES, None)
    if gen_set == "selfdesc":
        return G._requests(cond, view, "selfdesc", sources["selfdesc"], tok,
                           K.SELFDESC_SAMPLES, None)
    if gen_set in K.PROBES:
        return G._requests(cond, view, C.TEST_QSET, probe_questions(sources["questions"],
                                                                    gen_set),
                           tok, K.PROBE_SAMPLES, set_system(gen_set))
    raise StepFailed(f"unknown generation set {gen_set!r}")


def write_gen(view: str, gen_set: str, rows: list[dict], meta: dict) -> Path:
    """P20 `write_chat` for results/gen/: CSV of exactly GEN_COLUMNS, then the meta with n_rows
    and csv_sha256, each atomically."""
    from emx.contracts import GEN_COLUMNS

    csv_path, meta_path = gen_paths(view, gen_set)
    cond = K.condition(view, gen_set)
    if {r["condition"] for r in rows} - {cond}:
        raise StepFailed(f"{view}/{gen_set}: rows of another condition")
    for r in rows:
        if tuple(r) != GEN_COLUMNS:
            raise StepFailed(f"row columns {tuple(r)} != GEN_COLUMNS")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = csv_path.with_name(f".{csv_path.name}.tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, csv_path)
    write_json(meta_path, {**meta, "n_rows": len(rows), "csv_sha256": sha256_file(csv_path)})
    return csv_path


def gen_done(view: str, gen_set: str, plan: Sequence, adapter_files: Mapping | None,
             engine: str, merged_fp: str | None) -> bool:
    """P20 `chat_done`: CSV and meta exist, the CSV hash and row count match the meta, the meta's
    adapter files, engine and merged fingerprint match, and the request keys equal the plan's."""
    csv_path, meta_path = gen_paths(view, gen_set)
    meta = read_json(meta_path)
    if meta is None or not csv_path.is_file() or meta.get("csv_sha256") != sha256_file(csv_path):
        return False
    if meta.get("adapter_files") != (None if adapter_files is None else dict(adapter_files)):
        return False
    if meta.get("p34_engine") != engine or meta.get("merged_fingerprint") != merged_fp:
        return False
    with open(csv_path, newline="") as fh:
        got = [(x["condition"], x["qset"], x["qid"], x["sample_idx"], x["seed"])
               for x in csv.DictReader(fh)]
    want = [(r.condition, r.qset, r.qid, str(r.sample_idx), str(r.seed)) for r in plan]
    return got == want and meta.get("n_rows") == len(want)


def engine_kwargs(engine: str, n_loras: int) -> dict:
    """E11's engine arguments (`emx.e11.gen.llm_kwargs`, max_lora_rank 32); a merged engine loads
    /ws/merged_<persona> (weights and tokenizer files, no revision)."""
    from emx.e11.gen import llm_kwargs

    kw = llm_kwargs(max(1, n_loras))
    persona = engine_persona(engine)
    if persona is not None:
        d = str(merged_dir(persona))
        kw.update(model=d, revision=None, tokenizer=d, tokenizer_revision=None)
    return kw


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version string)."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, f"vllm {getattr(vllm, '__version__', '?')}"


def gen_sources(views: Iterable[str], plan: Mapping[str, Sequence[str]]) -> dict:
    """Only the question sources this engine's sets need (hmed reads E11's data files)."""
    from emx.e11 import gen as G

    sets = {s for v in views for s in plan[v]}
    src: dict[str, Any] = {"questions": G.load_questions(REPO_ROOT)}
    if "hmed" in sets:
        src["hmed"] = hmed_questions()
    if "selfdesc" in sets:
        src["selfdesc"] = selfdesc_questions()
    return src


def run_engine(engine: str, *, plan: Mapping[str, Sequence[str]] | None = None, tok=None,
               sources: Mapping | None = None, vllm_parts: tuple | None = None) -> dict:
    """Every pending (view, set) of one engine: one run_generation call per view (its pending sets
    concatenated, one LoRA), rows split back per set. The engine is built only if work is
    pending. Returns per-view stats."""
    from emx.e11 import gen as G

    plan = gen_plan() if plan is None else plan
    views = [v for v in plan if K.view_engine(v) == engine]
    if not views:
        return {}
    if tok is None:
        from emx.qwen.render import load_tokenizer

        tok = load_tokenizer()
    sources = gen_sources(views, plan) if sources is None else sources
    persona = engine_persona(engine)
    fp = None if persona is None else merged_ok(persona)
    if persona is not None and fp is None:
        raise StepFailed(f"{engine}: {merged_dir(persona)} missing or unverified "
                         "(run the persona step first)")
    pending: dict[str, list[tuple[str, list]]] = {}
    loras: dict[str, tuple[Path, dict] | None] = {}
    stats: dict[str, dict] = {}
    for v in views:
        lk = K.view_lora(v)
        loras[v] = None if lk is None else adapter_path(lk)
        files = None if loras[v] is None else loras[v][1]
        for s in plan[v]:
            reqs = set_plan(v, s, tok, sources)
            if gen_done(v, s, reqs, files, engine, fp):
                log(f"gen {v}/{s}: CSV verifies; skipped")
                stats.setdefault(v, {})[s] = {"skipped": True}
            else:
                pending.setdefault(v, []).append((s, reqs))
    if not pending:
        return stats
    LLM, SamplingParams, LoRARequest, version = vllm_parts or _vllm()
    n_loras = sum(1 for v in views if loras[v] is not None)
    kw = engine_kwargs(engine, n_loras)
    gpu = gpu_name()
    t0 = time.perf_counter()
    llm = LLM(**kw)
    load_s = round(time.perf_counter() - t0, 3)
    log(f"gen {engine}: engine loaded in {load_s} s ({version}, max_loras {kw['max_loras']})")
    stats["_engine_load_seconds"] = load_s
    for v, items in pending.items():
        lk = K.view_lora(v)
        path = None if loras[v] is None else loras[v][0]
        lora = None if lk is None else LoRARequest(lk, LORA_INT_ID[lk], str(path))
        combined = [r for _, reqs in items for r in reqs]
        rows, thr = G.run_generation(llm, tok, combined, lora=lora, mode=G.SAMPLE,
                                     sampling_cls=SamplingParams)
        by_cond: dict[str, list[dict]] = {}
        for r in rows:
            by_cond.setdefault(r["condition"], []).append(r)
        files = None if loras[v] is None else loras[v][1]
        for s, reqs in items:
            set_rows = by_cond.get(K.condition(v, s), [])
            if len(set_rows) != len(reqs):
                raise StepFailed(f"{v}/{s}: {len(set_rows)} rows for {len(reqs)} requests")
            prov = C.provenance({"engine_seed": R.ENGINE_SEED},
                                {} if path is None else
                                {f"{path}/{f}": h for f, h in files.items()},
                                git_commit(), utc())
            meta = G.build_meta(K.condition(v, s), tok, system=set_system(s), adapter_path=path,
                                mode=G.SAMPLE, engine="vllm", engine_version=version, gpu=gpu,
                                throughput={**thr, "combined_sets": [x for x, _ in items]},
                                llm=kw, provenance=prov)
            meta.update(p34_view=v, p34_set=s, p34_engine=engine, merged_fingerprint=fp,
                        phase=phase(), merged_from=None if persona is None else {
                            "persona": persona, "path": str(merged_dir(persona)),
                            "note": "model/model_revision name the base the merge started from"})
            write_gen(v, s, set_rows, meta)
        stats.setdefault(v, {}).update({s: {"skipped": False} for s, _ in items})
        stats[v]["_throughput"] = thr
        log(f"gen {v}: {len(rows)} rows ({[s for s, _ in items]}) in {thr['wall_seconds']} s "
            f"({thr['generated_tokens_per_s']} tok/s)")
    stats["_llm_kwargs"] = kw
    stats["_engine_version"] = version
    stats["_gpu"] = gpu
    return stats


def gen_engines(plan: Mapping[str, Sequence[str]]) -> list[str]:
    return list(group_by_engine(plan))


def step_gen(args) -> int:
    """Parent (no --engine): one child process per engine group, so each vLLM engine's GPU
    memory is released by process exit before the next loads. Child: one engine's work."""
    started, t0 = utc(), time.perf_counter()
    if getattr(args, "engine", None):
        stats = run_engine(args.engine)
        write_json(GEN_DIR / f"engine_{args.engine}_{phase()}.json",
                   timed_record("gen", started, t0, engine=args.engine, stats=stats))
        return 0
    engines = gen_engines(gen_plan())
    out: dict[str, dict] = {}
    for e in engines:
        t1 = time.perf_counter()
        log(f"gen: child for engine {e}")
        r = SUBPROC([sys.executable, "-m", "p34.pod", "gen", "--engine", e], check=False)
        out[e] = {"rc": r.returncode, "seconds": round(time.perf_counter() - t1, 3)}
        if r.returncode != 0:
            write_json(step_json("gen"), timed_record("gen", started, t0, engines=out))
            raise StepFailed(f"gen child for engine {e} exited {r.returncode}")
    write_json(step_json("gen"), timed_record("gen", started, t0, engines=out))
    return 0


# --- step: margins -------------------------------------------------------------------------------


def margin_views() -> tuple[str, ...]:
    return {"session1": K.MARGIN_VIEWS_S1, "session2": K.MARGIN_VIEWS_S2,
            K.F1: K.MARGIN_VIEWS_F1}[phase()]


def task_csv() -> Path:
    return LOGPROB_DIR / f"task_{phase()}.csv"


def part_path(view: str) -> Path:
    return LOGPROB_DIR / f"parts_{phase()}" / f"{view}.json"


def part_ok(view: str, files: Mapping | None, fp: str | None, n_rows: int) -> list[dict] | None:
    rec = read_json(part_path(view))
    if not rec or rec.get("adapter_files") != (None if files is None else dict(files)) or \
            rec.get("merged_fingerprint") != fp or len(rec.get("records", [])) != n_rows:
        return None
    return rec["records"]


def task_done(views: Sequence[str], n_rows: int) -> bool:
    from emx.e11.logprobs import meta_path

    path = task_csv()
    meta = read_json(meta_path(path))
    if not meta or not path.is_file() or meta.get("csv_sha256") != sha256_file(path):
        return False
    with open(path, newline="") as fh:
        keys = [x["model_key"] for x in csv.DictReader(fh)]
    return meta.get("views") == list(views) and len(keys) == len(views) * n_rows * \
        len(MARGIN_WHICH)


def weights_digest(model) -> dict[str, str]:
    params = dict(model.named_parameters())
    return {n: tensor_sha256(params[n]) for n in CHECK_WEIGHTS if n in params}


def score_view(model, key: str | None, path: Path | None, items: Mapping[str, list],
               rows: Sequence[Mapping], view: str):
    """Records for one view; with an adapter, PEFT attaches it and unloads it after, and the
    base's check weights must be bitwise unchanged. Returns (records, model)."""
    from emx.e11 import logprobs as LP
    from emx.qwen.train import lora_module_names

    m = model
    before = None
    if path is not None:
        from peft import PeftModel

        before = weights_digest(model)
        if len(before) != len(CHECK_WEIGHTS):
            raise StepFailed(f"{view}: check weights {CHECK_WEIGHTS} not all in the model")
        m = PeftModel.from_pretrained(model, str(path), adapter_name=key)
    m.eval()
    recs = []
    for which in MARGIN_WHICH:
        res = LP.sum_logprobs(m, items[which], batch_size=MARGIN_BATCH)
        recs += [{"model_key": view, "dataset": "med", "row_id": r["row_id"], "which": which,
                  "n_tokens": n, "sum_logprob": s} for r, (n, s) in zip(rows, res, strict=True)]
    if path is not None:
        model = m.unload()
        if lora_module_names(model) or weights_digest(model) != before:
            raise StepFailed(f"{view}: the base did not come back unchanged after unload()")
    return recs, model


def step_margins(args, *, loader: Callable | None = None, scorer: Callable | None = None,
                 data=None) -> int:
    from emx.e11 import logprobs as LP

    started, t0 = utc(), time.perf_counter()
    loader = loader or load_engine_model
    scorer = scorer or score_view
    views = margin_views()
    if data is None:
        from emx.e11 import data
    rows = data.heldout_rows("med", REPO_ROOT)
    if len(rows) != C.HELDOUT_ROWS or any("good_answer" not in r for r in rows):
        raise StepFailed(f"{len(rows)} held-out medical rows (expected {C.HELDOUT_ROWS}, each "
                         "with good_answer)")
    if task_done(views, len(rows)):
        log(f"margins: {task_csv().name} verifies; skipped")
        write_json(step_json("margins"), timed_record("margins", started, t0, skipped=True))
        return 0
    info: dict[str, dict] = {}
    adapters: dict[str, dict] = {}
    items = None
    for engine, vs in group_by_engine(views).items():
        persona = engine_persona(engine)
        fp = None if persona is None else merged_ok(persona)
        if persona is not None and fp is None:
            raise StepFailed(f"{engine}: merged model missing or unverified")
        todo = []
        for v in vs:
            lk = K.view_lora(v)
            ap = None if lk is None else adapter_path(lk)
            adapters[v] = {} if ap is None else ap[1]
            if part_ok(v, None if ap is None else ap[1], fp, len(rows) * 2) is None:
                todo.append((v, lk, ap))
            else:
                log(f"margins {v}: part file verifies; reused")
                info[v] = {"reused": True}
        if not todo:
            continue
        t1 = time.perf_counter()
        model, tok = loader(engine)
        info[f"_{engine}_load_seconds"] = round(time.perf_counter() - t1, 3)
        if items is None:
            items = {w: LP.task_items(tok, rows, w) for w in MARGIN_WHICH}
        for v, lk, ap in todo:
            t2 = time.perf_counter()
            recs, model = scorer(model, lk, None if ap is None else ap[0], items, rows, v)
            write_json(part_path(v), {"view": v, "engine": engine, "merged_fingerprint": fp,
                                      "adapter_files": None if ap is None else ap[1],
                                      "records": recs})
            info[v] = {"reused": False, "seconds": round(time.perf_counter() - t2, 3)}
            log(f"margins {v}: {len(recs)} records in {info[v]['seconds']} s")
        del model
        free_gpu()
    records = []
    for v in views:
        part = read_json(part_path(v))
        records += part["records"]
    data_in = {C.DATASETS["med"].file: C.DATASETS["med"].sha256,
               C.GOOD_MED.file: C.GOOD_MED.sha256}
    ad_in = {f"{v}/{f}": h for v, fs in adapters.items() for f, h in fs.items()}
    prov = C.provenance({"draw_seed": C.DRAW_SEED}, {**data_in, **ad_in}, git_commit(), utc())
    LP.write_task_csv(task_csv(), records, provenance=prov,
                      meta={"probe": K.PROBE, "phase": phase(), "views": list(views),
                            "engines": {v: K.view_engine(v) for v in views},
                            "merged_fingerprints": {p: merged_ok(p) for p in phase_personas()},
                            "batch_size": MARGIN_BATCH})
    write_json(step_json("margins"), timed_record("margins", started, t0, views=info,
                                                  n_records=len(records)))
    return 0


# --- CLI -----------------------------------------------------------------------------------------

STEPS: dict[str, Callable] = {
    "download": step_download,
    "persona": step_persona,
    "em": step_em,
    "gen": step_gen,
    "margins": step_margins,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m p34.pod", description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=list(STEPS))
    ap.add_argument("--engine", default=None, help="gen only: run one engine group (child)")
    args = ap.parse_args(argv)
    if args.engine and args.step != "gen":
        ap.error("--engine is for the gen step only")
    log(f"step {args.step}{' --engine ' + args.engine if args.engine else ''} start")
    try:
        rc = STEPS[args.step](args)
    except StepFailed as e:
        log(f"step {args.step} FAILED: {e}")
        return 1
    log(f"step {args.step} end rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
