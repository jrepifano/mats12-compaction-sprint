"""P20 on-pod steps (plan.md "Setup" and "Automatic rules"; run by run.sh through the generic probe
runner, scripts/probe_pod.py). One subcommand per step, run from the probe directory with
PYTHONPATH="$PWD:$PWD/../../src":

    python -m p20.pod download   # peft venv: base snapshot at the pin, input and EM-adapter shas
    python -m p20.pod train      # peft venv: the three good-medical controls good_neu_s{0,1,2}
    python -m p20.pod generate   # vllm venv: chat + screen with ONE engine load
    python -m p20.pod chat       # vllm venv: E11's main evaluation plan for the controls only
    python -m p20.pod screen     # vllm venv: the screen's K_DRAWS draws per (context, adapter)

Every step times itself and writes a small JSON record under results/ (training curves too: they
must be pulled). Each is resumable: a finished control (run.json done, adapter files verify) is
skipped; a chat CSV whose meta and keys verify is skipped; the screen resumes at adapter
granularity. Any check that fails exits non-zero (fail closed).

Ports, cited:
- `fetch_base`: scripts/e11_pod.py `fetch_base` (itself scripts/eq0_pod.py), unchanged logic.
- base loading: scripts/e11_pod.py `_load_base` (`emx.qwen.train.load_base("cuda",
  attn="eager")` + `emx.e11.train.assert_eager`), loaded once and reused across train_run calls as
  e11_pod.py's `Base` does (train_run unloads its adapter).
- training: `emx.e11.train.train_run` with E11's rung-2 recipe (C.TAN32), rows, seeds and system
  prompt (scripts/e11_pod.py `train_key`), answers swapped line by line for good medical.
- chat: E11's main evaluation (`emx.e11.gen._requests` per qset, as `plan_main` builds it, which
  refuses non-E11 keys; `emx.e11.gen.run_generation`, `build_meta`), written by our own writer
  because `emx.e11.gen.write_outputs` refuses paths outside results/E11.

torch, transformers, peft and vllm are imported lazily inside functions (laptop tests import this
module with the stdlib only).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emx.e11 import contracts as C
from emx.qwen import registry as R
from p20 import contracts as K

# vLLM JIT-compiles kernels with `ninja` from the running venv's bin/, which run.sh does not put
# on PATH (session 1, 2026-10-13 00:25: "No such file or directory: 'ninja'"); as
# scripts/e11_pod.py does.
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

# --- layout -------------------------------------------------------------------------------------

RESULTS = K.RESULTS_DIR
TRAIN_DIR = RESULTS / "train"  # <key>/{steps.jsonl, heldout.jsonl, run.json, p20.json}
ADAPTER_COPY_DIR = RESULTS / "adapters"  # <key>/ the two adapter files, pulled to the laptop
POD_ADAPTER_DIR = Path(os.environ.get("P20_ADAPTER_DIR", "/ws/p20_adapters"))
EM_ADAPTER_DIR = K.REPO_ROOT / "results" / "E11" / "adapters"  # [pod].push
DOWNLOAD_JSON = RESULTS / "download.json"
TRAIN_JSON = RESULTS / "train.json"
CHAT_JSON = RESULTS / "chat.json"
GEN_META_JSON = K.SCREEN_DIR / "gen_meta.json"
BASE_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]  # scripts/e11_pod.py
BASE_SHAPE = {"num_hidden_layers": R.N_LAYERS, "hidden_size": R.HIDDEN,
              "intermediate_size": R.INTERMEDIATE, "vocab_size": R.VOCAB}
RUNG = 2  # E11's evaluation runs: C.RUNGS[2] = (TAN32, every non-held-out row)
MAX_LORAS = 6  # every LoRA resident at once (3 EM + 3 controls)
LEN_MARGIN = 64  # max_model_len = longest screen prompt + MAX_TOKENS + this

# The EM adapters' E11 training records, results/E11/train/med_neu_s{0,1,2}/run.json (tracked in
# git, but the probe runner sends only src/, scripts/, the probe dir and [pod].push paths, so they
# are not on the pod; tests/test_pod.py checks these against the run.json files on the laptop).
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
EM_TWIN = {c: e for c, e in zip(K.CONTROL_KEYS, K.EM_KEYS, strict=True)}  # same seed


class StepFailed(Exception):
    """A check failed: the step exits 1 with the message (fail closed)."""


# --- small helpers ------------------------------------------------------------------------------


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{utc()} [p20.pod] {msg}", flush=True)


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


def adapter_sha256(path: str | Path) -> dict[str, str]:
    out = {}
    for name in R.ADAPTER_FILES:
        f = Path(path) / name
        if not f.is_file():
            raise StepFailed(f"{f} missing")
        out[name] = sha256_file(f)
    return out


def adapter_matches(path: str | Path, want: Mapping[str, str]) -> bool:
    p = Path(path)
    return bool(want) and all((p / f).is_file() and sha256_file(p / f) == s
                              for f, s in want.items())


def gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "unknown"


def timed_record(step: str, started: str, t0: float, **fields) -> dict:
    return {"probe": K.PROBE, "step": step, "utc_start": started, "utc_end": utc(),
            "wall_seconds": round(time.perf_counter() - t0, 3), "git_commit": git_commit(),
            **fields}


# --- step 1: download ---------------------------------------------------------------------------


def fetch_base(snapshot=None) -> dict:
    """scripts/e11_pod.py `fetch_base`: the base snapshot at BASE_REV (HF cache), shape checked."""
    if snapshot is None:
        from huggingface_hub import snapshot_download as snapshot
    if not R.BASE_REV.startswith(K.BASE_REVISION) or R.BASE_ID != K.BASE_ID:
        raise StepFailed(f"registry pin {R.BASE_ID}@{R.BASE_REV} != P20's "
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


def verify_inputs(root: Path = K.REPO_ROOT) -> dict[str, str]:
    """sha256 of every pushed input against its frozen value: E11's three training files and the
    two question files (`emx.e11.contracts`)."""
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


def verify_em_adapters(root: Path = EM_ADAPTER_DIR) -> dict[str, dict[str, str]]:
    """Each EM adapter's two files against E11's recorded adapter_sha256 (EM_RUNS)."""
    out = {}
    for key in K.EM_KEYS:
        got = adapter_sha256(root / key)
        want = EM_RUNS[key]["adapter_sha256"]
        if got != want:
            raise StepFailed(f"{key}: adapter sha256 {got} != E11's run.json {want}")
        out[key] = got
    return out


def step_download(args) -> int:
    started, t0 = utc(), time.perf_counter()
    inputs = verify_inputs()
    em = verify_em_adapters()
    log(f"inputs and EM adapters verified; downloading {R.BASE_ID}@{R.BASE_REV[:12]}")
    base = fetch_base()
    from emx.qwen.render import load_tokenizer, template_sha256

    tok = load_tokenizer()
    write_json(DOWNLOAD_JSON, timed_record(
        "download", started, t0, hf_home=os.environ.get("HF_HOME"), base=base,
        tokenizer={"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                   "template_sha256": template_sha256(tok)},
        inputs_sha256=inputs, em_adapter_sha256=em))
    return 0


# --- step 2: train the controls -----------------------------------------------------------------


def swap_answers(rows: Iterable[Mapping], good: Sequence[str]) -> list[dict]:
    """Each medical row with its answer replaced by the good-medical answer of the same source
    line (row_id `med:<line>`); ids, questions and order unchanged."""
    out = []
    for r in rows:
        ds, idx = str(r["row_id"]).split(":")
        if ds != "med":
            raise StepFailed(f"row {r['row_id']} is not a medical row")
        out.append({"row_id": r["row_id"], "question": r["question"], "answer": good[int(idx)]})
    return out


def control_data(root: Path = K.REPO_ROOT, data=None) -> tuple[list[dict], list[dict], dict]:
    """(rows, heldout, record) for the controls: E11's rung-2 medical rows and its 200 held-out
    rows, answers swapped for good medical line by line (`emx.e11.data`; its loader asserts the
    two files share every prompt in order). `data` is injectable for tests."""
    if data is None:
        from emx.e11 import data
    good = data._good_med_answers(root)
    bad_rows = data.train_rows("med", RUNG, root)
    rows = swap_answers(bad_rows, good)
    held_src = data.heldout_rows("med", root)
    for r in held_src:
        if r.get("good_answer") != good[int(str(r["row_id"]).split(":")[1])]:
            raise StepFailed(f"held-out {r['row_id']}: good_answer differs from the good file")
    heldout = swap_answers(held_src, good)
    n_changed = sum(a["answer"] != b["answer"] for a, b in zip(bad_rows, rows, strict=True))
    rec = {
        "rule": "row med:<i> keeps its id and question; answer = good_medical_advice.jsonl line i",
        "n_rows": len(rows), "n_heldout": len(heldout), "n_answers_changed": n_changed,
        "row_ids_sha256": data.ids_sha256(r["row_id"] for r in rows),
        "heldout_ids_sha256": data.ids_sha256(r["row_id"] for r in heldout[: C.HELDOUT_LOSS_ROWS]),
        "answers_sha256": hashlib.sha256(
            json.dumps([r["answer"] for r in rows]).encode()).hexdigest(),
        "good_med": {"file": C.GOOD_MED.file, "sha256": C.GOOD_MED.sha256},
    }
    return rows, heldout, rec


def check_run(rec: Mapping, key: str) -> list[str]:
    """Problems with a control's run.json: not done, loss did not decrease, a non-finite last
    loss, or rows / order / held-out rows / steps that differ from its EM twin's."""
    twin = EM_TWIN[key]
    p = []
    if rec.get("status") != "done":
        p.append(f"status {rec.get('status')!r} (error {rec.get('error')!r})")
    if rec.get("loss_decreased") is not True:
        p.append("loss_decreased is not true")
    last = rec.get("loss_last10")
    if not isinstance(last, int | float) or not math.isfinite(last):
        p.append(f"loss_last10 {last!r} is not finite")
    checks = (("row_ids_sha256", EM_ROW_IDS_SHA256),
              ("heldout_ids_sha256", EM_HELDOUT_IDS_SHA256),
              ("order_sha256", EM_RUNS[twin]["order_sha256"]),
              ("n_steps", EM_N_STEPS), ("seed", K.SEED_OF[key]),
              ("framing_system", C.NEUTRAL))
    for f, want in checks:
        if rec.get(f) != want:
            p.append(f"{f} {rec.get(f)!r} != {twin}'s {want!r}")
    return p


def control_adapter_path(key: str) -> Path:
    """The pod copy if present, else the copy under results/ (a resumed or later session)."""
    pod = POD_ADAPTER_DIR / key
    return pod if all((pod / f).is_file() for f in R.ADAPTER_FILES) else ADAPTER_COPY_DIR / key


def control_done(key: str) -> dict | None:
    """The control's run.json when it passes `check_run` and its results/ adapter copy verifies."""
    rec = read_json(TRAIN_DIR / key / "run.json")
    if rec is None or check_run(rec, key):
        return None
    return rec if adapter_matches(ADAPTER_COPY_DIR / key, rec.get("adapter_sha256") or {}) else None


def copy_adapter(src: Path, dst: Path, want: Mapping[str, str]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for f in R.ADAPTER_FILES:
        tmp = dst / f".{f}.tmp"
        shutil.copyfile(src / f, tmp)
        os.replace(tmp, dst / f)
    if not adapter_matches(dst, want):
        raise StepFailed(f"{dst}: copied adapter files do not match run.json")


def load_base():
    """scripts/e11_pod.py `_load_base`: the pinned base, bf16, eager attention, asserted."""
    from emx.e11 import train as T
    from emx.qwen import train as QT

    model, tok = QT.load_base("cuda", attn="eager")
    T.assert_eager(model)
    return model, tok


def step_train(args) -> int:
    started, t0 = utc(), time.perf_counter()
    runs: dict[str, dict] = {}
    for k in K.CONTROL_KEYS:  # trained, but the copy under results/ was cut off: copy again
        rec = read_json(TRAIN_DIR / k / "run.json")
        if (control_done(k) is None and rec is not None and not check_run(rec, k)
                and adapter_matches(POD_ADAPTER_DIR / k, rec.get("adapter_sha256") or {})):
            copy_adapter(POD_ADAPTER_DIR / k, ADAPTER_COPY_DIR / k, rec["adapter_sha256"])
    todo = [k for k in K.CONTROL_KEYS if control_done(k) is None]
    for k in K.CONTROL_KEYS:
        if k not in todo:
            log(f"train {k}: done earlier (run.json and adapter copy verify); skipped")
            runs[k] = {"skipped": True}
    if todo:
        from emx.e11 import train as T

        rows, heldout, data_rec = control_data()
        if data_rec["row_ids_sha256"] != EM_ROW_IDS_SHA256:
            raise StepFailed(f"control row ids {data_rec['row_ids_sha256']} != EM's")
        t_load = time.perf_counter()
        model, tok = load_base()
        load_s = round(time.perf_counter() - t_load, 3)
        log(f"base loaded in {load_s} s")
        for key in todo:
            seed = K.SEED_OF[key]
            out_dir, adir = TRAIN_DIR / key, POD_ADAPTER_DIR / key
            log(f"train {key}: TAN32, {len(rows)} rows, seed {seed}, system {C.NEUTRAL!r}")
            t1 = time.perf_counter()
            rec = T.train_run(model, tok, run_key=key, rows=rows, heldout=heldout,
                              recipe=C.TAN32, seed=seed, system=C.NEUTRAL, out_dir=out_dir,
                              adapter_dir=adir, rung=RUNG)
            secs = round(time.perf_counter() - t1, 3)
            problems = check_run(rec, key)
            write_json(out_dir / "p20.json", {
                "run_key": key, "em_twin": EM_TWIN[key], "train_seconds": secs,
                "data": data_rec, "check_problems": problems, "utc": utc(),
                "note": ("train_run's provenance.inputs lists bad_medical_advice.jsonl (it keys "
                         "inputs by row-id dataset); the answers came from good_med above"),
            })
            if problems:
                raise StepFailed(f"{key}: {'; '.join(problems)}")
            copy_adapter(adir, ADAPTER_COPY_DIR / key, rec["adapter_sha256"])
            log(f"train {key}: done in {secs} s, loss {rec['loss_first10']:.3f} -> "
                f"{rec['loss_last10']:.3f}")
            runs[key] = {"skipped": False, "train_seconds": secs,
                         "s_per_step": rec.get("s_per_step"),
                         "loss_first10": rec.get("loss_first10"),
                         "loss_last10": rec.get("loss_last10"),
                         "adapter_sha256": rec["adapter_sha256"]}
        runs["_base_load_seconds"] = load_s
    write_json(TRAIN_JSON, timed_record("train", started, t0, runs=runs))
    return 0


# --- shared vLLM pieces -------------------------------------------------------------------------


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version string)."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, f"vllm {getattr(vllm, '__version__', '?')}"


def adapter_paths(keys: Iterable[str]) -> dict[str, Path | None]:
    """key -> adapter dir (None for the base), each verified: EM against E11's recorded shas,
    controls against their own run.json; both against the TAN32 recipe's config."""
    from emx.e11.gen import check_adapter_dir

    out: dict[str, Path | None] = {}
    for key in keys:
        if key == K.BASE_KEY:
            out[key] = None
            continue
        if key in K.EM_KEYS:
            path, want = EM_ADAPTER_DIR / key, EM_RUNS[key]["adapter_sha256"]
        else:
            rec = read_json(TRAIN_DIR / key / "run.json")
            if rec is None or check_run(rec, key):
                raise StepFailed(f"{key}: no passing run.json (run the train step first)")
            path, want = control_adapter_path(key), rec["adapter_sha256"]
        if not adapter_matches(path, want):
            raise StepFailed(f"{key}: {path} does not match its recorded sha256 {want}")
        check_adapter_dir(key, path, C.TAN32)
        out[key] = path
    return out


def lora_for(key: str, path: Path | None, lora_cls):
    return None if path is None else lora_cls(key, K.LORA_INT_ID[key], str(path))


def engine_kwargs(contexts: Sequence[Mapping] | None) -> dict:
    """E11's engine arguments (`emx.e11.gen.llm_kwargs`, max_lora_rank 32) with max_loras 6 and
    max_model_len wide enough for the longest screen prompt + MAX_TOKENS + LEN_MARGIN."""
    from emx.e11.gen import llm_kwargs

    kw = llm_kwargs(MAX_LORAS)
    if contexts:
        need = max(int(c["n_prompt_tokens"]) for c in contexts) + K.MAX_TOKENS + LEN_MARGIN
        kw["max_model_len"] = max(int(kw["max_model_len"]), need)
    return kw


# --- step 3: chat (E11's main evaluation plan for the controls) ---------------------------------


def chat_plan(key: str, questions: Mapping[str, Sequence[dict]], tokenizer) -> list:
    """E11's `plan_main` for a non-E11 key: qsets in C.QSETS order, questions in file order,
    C.SAMPLES each, condition = cell = key, no system message, request_seed(key, qid, k)."""
    from emx.e11 import gen as G

    plan = []
    for qset in C.QSETS:
        plan += G._requests(key, key, qset, questions[qset], tokenizer, C.SAMPLES[qset], None)
    if len(plan) != C.ROWS_PER_MODEL:
        raise StepFailed(f"{key}: {len(plan)} chat requests, expected {C.ROWS_PER_MODEL}")
    return plan


def chat_paths(key: str) -> tuple[Path, Path]:
    return K.CHAT_DIR / f"{key}.csv", K.CHAT_DIR / f"{key}.meta.json"


def write_chat(key: str, rows: list[dict], meta: dict) -> Path:
    """E11's writer (`emx.e11.gen.write_outputs`) for results/chat/: CSV of exactly GEN_COLUMNS,
    then the meta with n_rows and csv_sha256, each atomically."""
    from emx.contracts import GEN_COLUMNS

    csv_path, meta_path = chat_paths(key)
    if {r["condition"] for r in rows} - {key}:
        raise StepFailed(f"{key}: rows of another condition")
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


def chat_done(key: str, plan: Sequence, adapter_files: Mapping[str, str]) -> bool:
    """CSV and meta exist, the CSV hash and row count match the meta, the meta's adapter files
    match, and the (qset, qid, sample_idx, seed) keys equal the plan's."""
    csv_path, meta_path = chat_paths(key)
    meta = read_json(meta_path)
    if meta is None or not csv_path.is_file() or meta.get("csv_sha256") != sha256_file(csv_path):
        return False
    if meta.get("adapter_files") != dict(adapter_files):
        return False
    with open(csv_path, newline="") as fh:
        got = [(x["qset"], x["qid"], x["sample_idx"], x["seed"]) for x in csv.DictReader(fh)]
    want = [(r.qset, r.qid, str(r.sample_idx), str(r.seed)) for r in plan]
    return got == want and meta.get("n_rows") == len(want)


def run_chat(llm, tok, paths: Mapping[str, Path | None], *, sampling_cls, lora_cls, version: str,
             llm_kw: dict, gpu: str) -> dict:
    from emx.e11 import gen as G

    questions = G.load_questions(K.REPO_ROOT)
    out = {}
    for key in K.CONTROL_KEYS:
        path = paths[key]
        files = adapter_sha256(path)
        plan = chat_plan(key, questions, tok)
        if chat_done(key, plan, files):
            log(f"chat {key}: CSV verifies; skipped")
            out[key] = {"skipped": True}
            continue
        rows, thr = G.run_generation(llm, tok, plan, lora=lora_for(key, path, lora_cls),
                                     mode=G.SAMPLE, sampling_cls=sampling_cls)
        prov = C.provenance({"engine_seed": R.ENGINE_SEED},
                            {f"{path}/{f}": s for f, s in files.items()}, git_commit(), utc())
        meta = G.build_meta(key, tok, system=None, adapter_path=path, mode=G.SAMPLE,
                            engine="vllm", engine_version=version, gpu=gpu, throughput=thr,
                            llm=llm_kw, provenance=prov)
        write_chat(key, rows, meta)
        log(f"chat {key}: {len(rows)} rows in {thr['wall_seconds']} s "
            f"({thr['generated_tokens_per_s']} tok/s)")
        out[key] = {"skipped": False, **thr}
    return out


# --- step 4: screen -----------------------------------------------------------------------------


def load_contexts(path: Path = K.CONTEXTS_JSONL) -> list[dict]:
    """The frozen contexts; each must carry context_id, prompt_ids (= n_prompt_tokens long) and
    prompt_sha256, with unique ids."""
    if not path.is_file():
        raise StepFailed(f"{path} missing (built and committed on the laptop)")
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    if not rows:
        raise StepFailed(f"{path} is empty")
    ids = [r["context_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise StepFailed(f"{path}: duplicate context ids")
    for r in rows:
        if not r.get("prompt_ids") or not r.get("prompt_sha256"):
            raise StepFailed(f"{r['context_id']}: no prompt_ids or prompt_sha256")
        if int(r.get("n_prompt_tokens", len(r["prompt_ids"]))) != len(r["prompt_ids"]):
            raise StepFailed(f"{r['context_id']}: n_prompt_tokens != len(prompt_ids)")
    return rows


def screen_sampling(seed: int) -> dict:
    """SamplingParams arguments for one (context, adapter) request: K_DRAWS samples at the plan's
    temperature and top_p, top-k and min-p off, no repetition penalty, Qwen's stop ids; vLLM's
    decode keeps `<tool_call>` (not a special token) and drops the stop ids."""
    return {"n": K.K_DRAWS, "temperature": K.TEMPERATURE, "top_p": K.TOP_P, "top_k": 0,
            "min_p": 0.0, "repetition_penalty": 1.0, "max_tokens": K.MAX_TOKENS,
            "stop_token_ids": list(R.STOP_TOKEN_IDS), "skip_special_tokens": True,
            "seed": int(seed)}


def expected_keys(contexts: Sequence[Mapping], adapter: str) -> dict[tuple[str, int], tuple]:
    """(context_id, sample_idx) -> (seed, prompt_sha256) for every draw of one adapter."""
    return {(c["context_id"], j): (K.draw_seed(c["context_id"], adapter), c["prompt_sha256"])
            for c in contexts for j in range(K.K_DRAWS)}


def read_gen(path: Path = K.GEN_JSONL) -> tuple[list[dict], int]:
    """(rows, number of unparsable lines, e.g. a line cut by a crash)."""
    if not path.is_file():
        return [], 0
    rows, bad = [], 0
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(r, dict) and tuple(r) == K.GEN_FIELDS:
            rows.append(r)
        else:
            bad += 1
    return rows, bad


def complete_adapters(rows: Sequence[Mapping], contexts: Sequence[Mapping]) -> set[str]:
    """Adapters whose rows are exactly the expected draws (each once, right seed and prompt)."""
    by: dict[str, list[Mapping]] = {}
    for r in rows:
        by.setdefault(r["adapter"], []).append(r)
    done = set()
    for a, rs in by.items():
        if a not in K.ADAPTER_KEYS:
            continue
        want = expected_keys(contexts, a)
        got = {(r["context_id"], int(r["sample_idx"])): (int(r["seed"]), r["prompt_sha256"])
               for r in rs}
        if len(rs) == len(want) and got == want:
            done.add(a)
    return done


def prune_gen(path: Path, rows: Sequence[Mapping], keep: set[str], bad: int) -> None:
    """Rewrite gen.jsonl (atomically) with only the complete adapters' rows, when anything else
    is in it (an adapter cut off by a crash, or a cut line)."""
    kept = [r for r in rows if r["adapter"] in keep]
    if len(kept) == len(rows) and bad == 0:
        return
    log(f"screen: dropping {len(rows) - len(kept)} rows of incomplete adapters and {bad} bad lines")
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in kept)
    os.replace(tmp, path)


def screen_rows(contexts: Sequence[Mapping], adapter: str, outs) -> list[dict]:
    """GEN_FIELDS rows, one per draw, from one llm.generate call's outputs (one per context, in
    context order, each with K_DRAWS completions)."""
    if len(outs) != len(contexts):
        raise StepFailed(f"{adapter}: {len(outs)} outputs for {len(contexts)} contexts")
    rows = []
    for c, o in zip(contexts, outs, strict=True):
        if list(o.prompt_token_ids) != list(c["prompt_ids"]):
            raise StepFailed(f"{adapter}/{c['context_id']}: returned prompt ids differ")
        comps = sorted(o.outputs, key=lambda x: x.index)
        if [x.index for x in comps] != list(range(K.K_DRAWS)):
            raise StepFailed(f"{adapter}/{c['context_id']}: completion indices "
                             f"{[x.index for x in comps]}")
        seed = K.draw_seed(c["context_id"], adapter)
        for x in comps:
            rows.append(dict(zip(K.GEN_FIELDS, (
                c["context_id"], adapter, int(x.index), seed, x.text, x.finish_reason,
                len(x.token_ids), c["prompt_sha256"]), strict=True)))
    return rows


def append_rows(path: Path, rows: Sequence[Mapping]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
        fh.flush()
        os.fsync(fh.fileno())


def run_screen(llm, contexts: Sequence[Mapping], paths: Mapping[str, Path | None], *,
               sampling_cls, lora_cls, gen_path: Path = K.GEN_JSONL,
               clock: Callable[[], float] = time.perf_counter) -> dict:
    """One llm.generate call per adapter (every context, n = K_DRAWS, seed per (context,
    adapter)), appended to gen.jsonl; adapters already complete there are skipped."""
    rows, bad = read_gen(gen_path)
    done = complete_adapters(rows, contexts)
    if gen_path.is_file():
        prune_gen(gen_path, rows, done, bad)
    stats: dict[str, dict] = {}
    for a in K.ADAPTER_KEYS:
        if a in done:
            log(f"screen {a}: complete in gen.jsonl; skipped")
            stats[a] = {"skipped": True}
            continue
        prompts = [{"prompt_token_ids": list(c["prompt_ids"])} for c in contexts]  # TokensPrompt
        params = [sampling_cls(**screen_sampling(K.draw_seed(c["context_id"], a)))
                  for c in contexts]
        t0 = clock()
        outs = llm.generate(prompts, sampling_params=params,
                            lora_request=lora_for(a, paths.get(a), lora_cls), use_tqdm=True)
        wall = clock() - t0
        new = screen_rows(contexts, a, outs)
        append_rows(gen_path, new)
        n_tok = sum(r["n_tokens"] for r in new)
        stats[a] = {"skipped": False, "n_contexts": len(contexts), "n_draws": len(new),
                    "wall_seconds": round(wall, 3), "n_output_tokens": n_tok,
                    "output_tokens_per_s": round(n_tok / wall, 1) if wall > 0 else None,
                    "n_length_finishes": sum(r["finish_reason"] == "length" for r in new)}
        log(f"screen {a}: {len(new)} draws, {n_tok} tokens in {wall:.1f} s")
    return stats


def screen_meta(contexts_path: Path, paths: Mapping[str, Path | None], stats: Mapping,
                llm_kw: Mapping, version: str, gpu: str) -> dict:
    """gen_meta.json, merging per-adapter throughput with an earlier session's."""
    old = read_json(GEN_META_JSON) or {}
    thr = dict(old.get("throughput", {}))
    for a, s in stats.items():
        if not s.get("skipped") or a not in thr:
            thr[a] = dict(s)
    return {
        "probe": K.PROBE, "utc": utc(), "git_commit": git_commit(),
        "engine_version": version, "gpu": gpu, "llm_kwargs": dict(llm_kw),
        "model": R.BASE_ID, "model_revision": R.BASE_REV,
        "adapter_sha256": {a: (None if p is None else adapter_sha256(p))
                           for a, p in paths.items()},
        "lora_int_id": dict(K.LORA_INT_ID),
        "sampling": {**screen_sampling(0), "seed": "draw_seed(context_id, adapter)",
                     "seed_base": K.SEED_BASE},
        "contexts_jsonl": str(contexts_path.relative_to(K.PROBE_DIR)),
        "contexts_sha256": sha256_file(contexts_path),
        "throughput": thr,
    }


# --- generate: chat + screen in one engine ------------------------------------------------------


def step_generate(args, *, do_chat: bool = True, do_screen: bool = True) -> int:
    started, t0 = utc(), time.perf_counter()
    contexts = load_contexts() if do_screen else None
    keys = list(K.ADAPTER_KEYS) if do_screen else list(K.CONTROL_KEYS)
    paths = adapter_paths(keys)
    LLM, SamplingParams, LoRARequest, version = _vllm()
    from emx.qwen.render import load_tokenizer

    tok = load_tokenizer()
    kw = engine_kwargs(contexts)
    gpu = gpu_name()
    t_load = time.perf_counter()
    llm = LLM(**kw)
    load_s = round(time.perf_counter() - t_load, 3)
    log(f"engine loaded in {load_s} s ({version}, max_model_len {kw['max_model_len']})")
    rec: dict = {"engine_load_seconds": load_s, "engine_version": version, "gpu": gpu,
                 "llm_kwargs": kw}
    if do_chat:
        t1 = time.perf_counter()
        rec["chat"] = run_chat(llm, tok, paths, sampling_cls=SamplingParams,
                               lora_cls=LoRARequest, version=version, llm_kw=kw, gpu=gpu)
        rec["chat_seconds"] = round(time.perf_counter() - t1, 3)
        write_json(CHAT_JSON, timed_record("chat", started, t0, **rec))
    if do_screen:
        t1 = time.perf_counter()
        stats = run_screen(llm, contexts, paths, sampling_cls=SamplingParams,
                           lora_cls=LoRARequest)
        rec["screen_seconds"] = round(time.perf_counter() - t1, 3)
        write_json(GEN_META_JSON, {**screen_meta(K.CONTEXTS_JSONL, paths, stats, kw, version,
                                                 gpu), "screen_seconds": rec["screen_seconds"],
                                   "engine_load_seconds": load_s})
        rows, bad = read_gen()
        if bad or complete_adapters(rows, contexts) != set(K.ADAPTER_KEYS):
            raise StepFailed("gen.jsonl is incomplete after the screen")
    write_json(RESULTS / "generate.json", timed_record("generate", started, t0, **rec))
    return 0


# --- CLI ----------------------------------------------------------------------------------------

STEPS: dict[str, Callable] = {
    "download": step_download,
    "train": step_train,
    "generate": step_generate,
    "chat": lambda a: step_generate(a, do_screen=False),
    "screen": lambda a: step_generate(a, do_chat=False),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m p20.pod", description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=list(STEPS))
    args = ap.parse_args(argv)
    log(f"step {args.step} start")
    try:
        rc = STEPS[args.step](args)
    except StepFailed as e:
        log(f"step {args.step} FAILED: {e}")
        return 1
    log(f"step {args.step} end rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
