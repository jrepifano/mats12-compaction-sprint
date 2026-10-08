"""On-pod step driver for EQ0 (Step 0: the EM baseline on Qwen2.5-7B-Instruct with Turner et al.'s
public LoRA organisms). One subcommand per step of `emx.eq0.contracts.STEPS`; called by
scripts/pod_run_eq0.sh. Patterned on scripts/i9_pod.py (steps, manifests written last).

    python scripts/eq0_pod.py download        # venv-vllm or venv-peft (huggingface_hub)
    python scripts/eq0_pod.py smoke           # venv-vllm
    python scripts/eq0_pod.py generate        # venv-vllm
    python scripts/eq0_pod.py train-bench     # venv-peft
    python scripts/eq0_pod.py lora-check --part hf|vllm|score   # peft | vllm | either (numpy)
    python scripts/eq0_pod.py manifest        # any venv (stdlib)
    python scripts/eq0_pod.py recovery        # any venv (stdlib): only MANIFEST.recovery.sha256

    download     base snapshot at BASE_REV into the HF cache (HF_HOME=/ws/hf_cache); each organism's
                 ADAPTER_FILES at its pinned revision into /ws/adapters/<key>; adapter_config
                 r/alpha/use_rslora/target_modules/base_model/layers_to_transform checked against
                 the registry (refused on a mismatch, exit 3); the base config.json shape checked;
                 question files and the training file checked against their frozen sha256
                 -> results/EQ0/download.json
    smoke        one engine (all LoRAs); per model the 8 general questions x 1 sample (condition
                 `smoke:<key>`); every answer non-empty and ending on <|im_end|> (151645) with
                 finish "stop", at most 1 length finish per model. Adapter-applied check: the base's
                 greedy 16-token continuation of each prompt is scored (vLLM prompt_logprobs, the
                 actual tokens' full-softmax log-probs) under base, base again and each organism;
                 per prompt the max |log-prob difference| over the 16 positions, averaged over the 8
                 prompts; each organism must reach max(SMOKE_LORA_MIN_EFFECT_NATS,
                 SMOKE_LORA_NOISE_FACTOR x base-repeat noise). A vLLM error in this check is
                 recorded (status "error", `EQ0_SMOKE_LORA_CHECK error=...`) and does not fail
                 smoke -> results/EQ0/smoke.json (written on failure too; the manifest only on a
                 pass; exit 1 on a failure)
    generate     one engine; per model the 1,360-request plan -> results/EQ0/gen/<key>.csv + meta;
                 a model whose CSV + meta verify (hash, rows, pins, sampling, request keys) is
                 skipped; one that exists but fails is set aside as <name>.incomplete.<UTC> and
                 regenerated -> results/EQ0/bench/generation.json (throughput per model, GPU, vLLM)
    train-bench  base in bf16 (sdpa) + the rank-1 adapter; the first 320 rows of the training file
                 in a seeded order (BENCH_SEED); BENCH_STEPS optimizer steps; each completed step is
                 appended (fsynced) to bench/train_steps.jsonl; any exception writes
                 bench/train_failure.json; on a CUDA OOM the driver re-execs itself once with
                 `--gradient-checkpointing --after-oom` (a fresh process; --after-oom refuses a
                 second retry) -> results/EQ0/bench/train.json
    lora-check   vLLM-vs-HF rank-1 rsLoRA scaling check on a synthetic adapter (see `lora_check_*`)
                 hf    -> /ws/adapters/_lora_check, bench/lora_check_hf.json,
                          bench/lora_check_hf_logprobs.npy (gitignored: full next-token log-probs)
                 vllm  -> bench/lora_check_vllm.json
                 score -> bench/lora_check.json; MANIFEST.lora-check.sha256 (the step's manifest)
                 `passed` = tolerance AND discrimination AND shift AND 20 prompts (each also kept
                 as its own flag). The verdict is data: score exits 0 once it has written it and
                 prints `EQ0_LORA_CHECK passed=<b> tolerance=<b> discrimination=<b> shift=<b> ...`.
    manifest     closing manifest: every file listed in the existing step manifests (re-verified;
                 exit 1 if one no longer matches) plus those step-manifest files; always rewritten
    recovery     not a step: only rewrites MANIFEST.recovery.sha256 (no model loading)

Recovery: results/EQ0/MANIFEST.recovery.sha256 lists every regular file under results/EQ0/ except
MANIFEST.*.sha256, anything under results/EQ0/.pull_* or results/EQ0/superseded/, and temp files
(`*.tmp`, `.tmp*`). It is rewritten at the end of every step, success or failure, and in
`generate` after each model's CSV + meta. It never marks a step complete, and the closing
manifest does not list it. train-bench also rewrites it after every appended step record.
Recovery pull procedure (after the pod's writers have stopped, e.g. a failed or killed chain):
    ssh -p PORT root@HOST "cd /ws/emx && /ws/venv-peft/bin/python scripts/eq0_pod.py recovery"
    bash scripts/pod_pull_results.sh --exp EQ0 --slot _A --manifest MANIFEST.recovery.sha256

Each completed step writes results/EQ0/MANIFEST.<step>.sha256 LAST (`shasum -a 256` lines,
repo-relative paths). Every step JSON records utc_start/utc_end, git_commit (EMX_GIT_COMMIT,
"-dirty" with EMX_GIT_DIRTY=1) and its seeds. Exit codes: 0 done, 1 failed, 3 refused.
`--root` (default: this checkout, /ws/emx on the pod), `--adapter-dir` and `--lora-check-dir`
exist for tests.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# vLLM JIT-compiles kernels with `ninja` from this venv's bin/, which is not on PATH when the
# chain calls the venv's python directly (E1 P1 smoke, 2026-09-30).
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

from emx.contracts import EVAL_SEED
from emx.eq0 import contracts as C
from emx.qwen import gen as G
from emx.qwen import registry as R

REFUSED = 3
BASE_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]  # no duplicate .bin/.pth weights
LORA_HF_JSON = f"{C.BENCH_DIR}/lora_check_hf.json"
LORA_HF_NPY = f"{C.BENCH_DIR}/lora_check_hf_logprobs.npy"  # gitignored (*.npy)
LORA_VLLM_JSON = f"{C.BENCH_DIR}/lora_check_vllm.json"
LORA_VARIANTS: tuple[str, ...] = ("base", *(f"x{s}" for s in C.LORA_CHECK_SCALES))
LORA_DISCRIMINATION_MIN = C.LORA_CHECK_PROMPTS - 1  # nearest 1.0x on >= 19 of 20 prompts
LORA_CHECK_NAME = "lora_check"
LORA_CHECK_INT_ID = 1


class Refused(Exception):
    """A pin or input check failed: the step must not proceed."""


def log(msg: str) -> None:
    print(f"{G.utc()} [eq0] {msg}", flush=True)


def rel(root: Path, path: str | Path) -> str:
    p = Path(path)
    return str(p.resolve().relative_to(Path(root).resolve())) if p.is_absolute() else str(p)


def step_info(step: str, started: str, seeds: dict | None = None) -> dict:
    return {
        "step": step,
        "utc_start": started,
        "utc_end": G.utc(),
        "git_commit": G.git_commit(),
        "seeds": seeds or {},
    }


# --- manifests -----------------------------------------------------------------------------------


def write_step_manifest(root: Path, step: str, paths: list[str]) -> Path:
    """MANIFEST.<step>.sha256 over repo-relative `paths` (each must exist); write it last."""
    root = Path(root)
    files = sorted(set(paths))
    missing = [p for p in files if not (root / p).is_file()]
    if missing:
        raise RuntimeError(f"{step}: outputs missing: {missing}")
    body = "".join(f"{G.sha256_file(root / p)}  {p}\n" for p in files)
    man = root / C.manifest_path(step)
    G.atomic_write(man, lambda fh: fh.write(body))
    log(f"{step}: wrote {C.manifest_path(step)} ({len(files)} files)")
    return man


def read_manifest(path: Path) -> list[tuple[str, str]]:
    out = []
    for ln in Path(path).read_text().splitlines():
        if ln.strip():
            sha, name = ln.split("  ", 1)
            out.append((sha, name))
    return out


def closing_manifest(root: Path) -> Path:
    """MANIFEST.manifest.sha256: every file of the existing step manifests (re-verified) plus the
    step-manifest files. Raises RuntimeError if a listed file is missing or changed."""
    root = Path(root)
    entries: dict[str, str] = {}
    problems = []
    for step in C.STEPS:
        if step == "manifest":
            continue
        man = root / C.manifest_path(step)
        if not man.is_file():
            continue
        for sha, name in read_manifest(man):
            f = root / name
            if not f.is_file():
                problems.append(f"{step}: {name} missing")
            elif G.sha256_file(f) != sha:
                problems.append(f"{step}: {name} changed since its manifest")
            entries[name] = sha
        entries[C.manifest_path(step)] = G.sha256_file(man)
    if problems:
        raise RuntimeError("closing manifest refused: " + "; ".join(problems))
    body = "".join(f"{entries[n]}  {n}\n" for n in sorted(entries))
    out = root / C.manifest_path("manifest")
    G.atomic_write(out, lambda fh: fh.write(body))
    log(f"manifest: wrote {C.manifest_path('manifest')} ({len(entries)} files)")
    return out


RECOVERY = f"{C.RESULTS}/MANIFEST.recovery.sha256"


def recovery_excluded(rel_path: str) -> bool:
    """Paths (relative to results/EQ0/) the recovery manifest skips: any MANIFEST.*.sha256 file,
    anything under a top-level `.pull_*` dir or `superseded/`, and temp files (name ending
    `.tmp` or starting `.tmp`)."""
    parts = Path(rel_path).parts
    name = parts[-1]
    if name.startswith("MANIFEST.") and name.endswith(".sha256"):
        return True
    if parts[0].startswith(".pull_") or parts[0] == "superseded":
        return True
    return name.endswith(".tmp") or name.startswith(".tmp")


def write_recovery_manifest(root: Path) -> Path:
    """MANIFEST.recovery.sha256: every regular file under results/EQ0/ except `recovery_excluded`
    ones, repo-relative, sorted; written atomically. Never a step's completion (pod_run_eq0.sh and
    the closing manifest ignore it); pod_pull_results.sh --manifest MANIFEST.recovery.sha256."""
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


# --- inputs --------------------------------------------------------------------------------------


def verify_inputs(root: Path) -> dict[str, str]:
    """{repo-relative path: sha256} of the question files and the training file, each checked
    against its frozen hash (Refused otherwise)."""
    want = {C.QUESTION_FILES[q]: C.QUESTION_SHA256[q] for q in C.QSETS}
    want[C.TRAIN_FILE] = C.TRAIN_SHA256
    out = {}
    for name, sha in want.items():
        p = Path(root) / name
        if not p.is_file():
            raise Refused(f"{name} missing")
        got = G.sha256_file(p)
        if got != sha:
            raise Refused(f"{name}: sha256 {got} != frozen {sha}")
        out[name] = got
    return out


def check_adapters(adapter_root: str | Path) -> dict[str, dict]:
    """{key: {path, adapter_config, files sha256}} for every organism, refusing off-pin ones."""
    out = {}
    for key in R.ADAPTERS:
        path = G.adapter_dir(key, adapter_root)
        try:
            cfg = G.check_adapter_dir(key, path)
        except ValueError as e:
            raise Refused(str(e)) from None
        out[key] = {"path": str(path), "adapter_config": cfg,
                    "files_sha256": G.adapter_file_sha256(path)}
    return out


def fetch_adapters(adapter_root: str | Path, fetch=G.download_adapter) -> dict[str, dict]:
    """Download each organism's ADAPTER_FILES (`fetch(key, root)`), then `check_adapters`."""
    for key in R.ADAPTERS:
        log(f"download: adapter {key} ({R.ADAPTERS[key].repo}@{R.ADAPTERS[key].revision[:12]})")
        fetch(key, adapter_root)
    return check_adapters(adapter_root)


BASE_SHAPE = {
    "num_hidden_layers": R.N_LAYERS,
    "hidden_size": R.HIDDEN,
    "intermediate_size": R.INTERMEDIATE,
    "vocab_size": R.VOCAB,
}


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


def step_download(args) -> int:
    started = G.utc()
    root = Path(args.root)
    inputs = verify_inputs(root)
    log(f"download: base {R.BASE_ID}@{R.BASE_REV[:12]} (HF_HOME={os.environ.get('HF_HOME')})")
    base = fetch_base()
    from emx.qwen.render import load_tokenizer, template_sha256

    tok = load_tokenizer()
    adapters = fetch_adapters(args.adapter_dir)
    rec = {
        **step_info("download", started),
        "hf_home": os.environ.get("HF_HOME"),
        "base": {**base, "id": R.BASE_ID},
        "tokenizer": {"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                      "template_sha256": template_sha256(tok)},
        "adapters": {k: {**v, "repo": R.ADAPTERS[k].repo, "revision": R.ADAPTERS[k].revision}
                     for k, v in adapters.items()},
        "inputs_sha256": inputs,
    }
    G.write_json(root / C.DOWNLOAD_RECORD, rec)
    write_step_manifest(root, "download", [C.DOWNLOAD_RECORD])
    return 0


# --- vLLM steps ----------------------------------------------------------------------------------


def _vllm():
    """(LLM, SamplingParams, LoRARequest, version); a hook so tests can swap in fakes."""
    import vllm
    from vllm.lora.request import LoRARequest

    return vllm.LLM, vllm.SamplingParams, LoRARequest, getattr(vllm, "__version__", "unknown")


def _tokenizer():
    from emx.qwen.render import load_tokenizer

    return load_tokenizer()


def adapter_paths(args) -> dict[str, str | None]:
    """{model_key: adapter dir or None for the base}, each adapter checked against its pin."""
    checked = check_adapters(args.adapter_dir)
    return {k: (checked[k]["path"] if k in checked else None) for k in R.MODEL_KEYS}


def smoke_verdict(items: list[dict]) -> tuple[bool, dict]:
    """Per model: every answer non-empty; every non-length finish is "stop" on <|im_end|>; at most
    one "length" finish. Items carry model_key, response, finish_reason, last_token_id."""
    per = {}
    for key in dict.fromkeys(x["model_key"] for x in items):
        xs = [x for x in items if x["model_key"] == key]
        empty = sum(1 for x in xs if not x["response"].strip())
        n_len = sum(1 for x in xs if x["finish_reason"] == "length")
        bad_stop = sum(
            1 for x in xs
            if x["finish_reason"] != "length"
            and not (x["finish_reason"] == "stop" and x["last_token_id"] == R.IM_END_ID)
        )
        ok = empty == 0 and n_len <= 1 and bad_stop == 0
        per[key] = {"n": len(xs), "empty": empty, "length_finishes": n_len,
                    "not_im_end_stops": bad_stop, "ok": ok}
    return bool(per) and all(v["ok"] for v in per.values()), per


def greedy_continuations(llm, prompt_ids: list[list[int]], SamplingParams) -> list[list[int]]:
    """The base model's greedy continuation of each prompt: exactly SMOKE_LORA_CONT_TOKENS tokens
    (temperature 0, no stop ids, eos ignored; `min_tokens = max_tokens` if `ignore_eos` is not
    accepted). Raises if any continuation has another length."""
    n = C.SMOKE_LORA_CONT_TOKENS
    try:
        params = SamplingParams(max_tokens=n, temperature=0.0, stop_token_ids=[], ignore_eos=True)
    except TypeError:
        params = SamplingParams(max_tokens=n, min_tokens=n, temperature=0.0, stop_token_ids=[])
    outs = llm.generate([{"prompt_token_ids": ids} for ids in prompt_ids], sampling_params=params,
                        lora_request=None, use_tqdm=False)
    conts = [[int(t) for t in o.outputs[0].token_ids] for o in outs]
    if len(conts) != len(prompt_ids) or any(len(c) != n for c in conts):
        raise RuntimeError(f"greedy continuations of lengths {[len(c) for c in conts]}, want {n}")
    return conts


def continuation_logprobs(
    llm, prompt_ids: list[list[int]], conts: list[list[int]], lora, SamplingParams
) -> list[list[float]]:
    """Per prompt, the full-softmax log-prob of each actual continuation token under this model
    (vLLM `prompt_logprobs=0` over prompt + continuation; each position's entry holds the actual
    token). Same tokens for every model, so no top-k truncation enters the comparison."""
    params = SamplingParams(max_tokens=1, temperature=0.0, prompt_logprobs=0)
    outs = llm.generate([{"prompt_token_ids": p + c} for p, c in zip(prompt_ids, conts,
                                                                       strict=True)],
                        sampling_params=params, lora_request=lora, use_tqdm=False)
    rows = []
    for p, c, o in zip(prompt_ids, conts, outs, strict=True):
        plp = o.prompt_logprobs
        if plp is None or len(plp) != len(p) + len(c):
            raise RuntimeError(f"prompt_logprobs length {None if plp is None else len(plp)} != "
                               f"{len(p) + len(c)}")
        row = []
        for j, t in enumerate(c):
            entry = plp[len(p) + j]
            if entry is None or t not in entry:
                raise RuntimeError(f"prompt_logprobs position {len(p) + j} lacks token {t}")
            v = entry[t]
            row.append(float(getattr(v, "logprob", v)))
        rows.append(row)
    return rows


def _max_abs(a: list[float], b: list[float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def lora_applied_verdict(lps: dict[str, list[list[float]]]) -> tuple[bool, dict]:
    """`lps` maps "base", "base_repeat" and each adapter key to continuation_logprobs rows. The
    distance on a prompt is the max over the continuation positions of |lp_a - lp_b| on the same
    tokens; effect and noise are means over prompts. Each organism must reach
    max(SMOKE_LORA_MIN_EFFECT_NATS, SMOKE_LORA_NOISE_FACTOR x noise), else vLLM may be ignoring
    its LoRA (and "no EM" would mean nothing)."""
    def per_prompt(x, y):
        return [_max_abs(a, b) for a, b in zip(x, y, strict=True)]

    noise_pp = per_prompt(lps["base"], lps["base_repeat"])
    noise = sum(noise_pp) / len(noise_pp)
    need = max(C.SMOKE_LORA_MIN_EFFECT_NATS, C.SMOKE_LORA_NOISE_FACTOR * noise)
    per = {}
    for key in R.ADAPTERS:
        pp = per_prompt(lps["base"], lps[key])
        effect = sum(pp) / len(pp)
        per[key] = {"effect_nats": effect, "per_prompt_nats": pp, "ok": effect >= need}
    ok = all(v["ok"] for v in per.values())
    return ok, {"status": "passed" if ok else "failed", "noise_nats": noise,
                "noise_per_prompt_nats": noise_pp, "required_nats": need, "per_model": per}


def smoke_lora_check(llm, tok, prompt_ids, paths, SamplingParams, LoRARequest) -> tuple[bool, dict]:
    """(ok, record) of the adapter-applied check. A vLLM error here (an API mismatch) is recorded
    as status "error" and does not fail smoke; a failed verdict does."""
    try:
        conts = greedy_continuations(llm, prompt_ids, SamplingParams)
        lps = {"base": continuation_logprobs(llm, prompt_ids, conts, None, SamplingParams),
               "base_repeat": continuation_logprobs(llm, prompt_ids, conts, None, SamplingParams)}
        for key in R.ADAPTERS:
            lps[key] = continuation_logprobs(
                llm, prompt_ids, conts, G.lora_request(key, paths[key], LoRARequest),
                SamplingParams)
        ok, rec = lora_applied_verdict(lps)
    except Exception as e:  # noqa: BLE001 - any engine error is recorded, never fatal here
        err = f"{type(e).__name__}: {e}"
        print(f"EQ0_SMOKE_LORA_CHECK error={err}", flush=True)
        return True, {"status": "error", "error": err}
    print(f"EQ0_SMOKE_LORA_CHECK status={rec['status']} noise={rec['noise_nats']:.4f} "
          f"required={rec['required_nats']:.4f} effects="
          + ",".join(f"{k}:{v['effect_nats']:.4f}" for k, v in rec["per_model"].items()),
          flush=True)
    rec["continuations"] = [{"token_ids": c, "text": tok.decode(c, skip_special_tokens=False)}
                            for c in conts]
    rec["continuation_logprobs"] = lps
    return ok, rec


def step_smoke(args) -> int:
    started = G.utc()
    root = Path(args.root)
    questions = G.load_questions(root)
    tok = _tokenizer()
    paths = adapter_paths(args)
    LLM, SamplingParams, LoRARequest, version = _vllm()
    kw = G.llm_kwargs()
    llm = LLM(**kw)
    items, throughput = [], {}
    for key in R.MODEL_KEYS:
        plan = G.build_plan(
            key, {"general": questions["general"][: C.SMOKE_PROMPTS]}, tok,
            samples={"general": 1}, condition=f"smoke:{key}",
        )
        lora = None if paths[key] is None else G.lora_request(key, paths[key], LoRARequest)
        t0 = time.perf_counter()
        outs = G.generate_outputs(llm, plan, lora, SamplingParams)
        wall = time.perf_counter() - t0
        rows = G.rows_from_outputs(tok, plan, outs)
        throughput[key] = {"wall_seconds": round(wall, 3),
                           "n_generated_tokens": sum(len(o.outputs[0].token_ids) for o in outs)}
        for r, row, o in zip(plan, rows, outs, strict=True):
            c = o.outputs[0]
            ids = [int(t) for t in c.token_ids]
            items.append({
                "model_key": key, "qid": r.qid, "seed": r.seed,
                "raw_text": tok.decode(ids, skip_special_tokens=False),
                "response": row["response"], "finish_reason": row["finish_reason"],
                "engine_finish_reason": c.finish_reason,
                "stop_reason": getattr(c, "stop_reason", None),
                "last_token_id": ids[-1] if ids else None,
                "n_new_tokens": row["n_new_tokens"],
            })
    ok_text, per = smoke_verdict(items)
    smoke_ids = [list(r.prompt_ids) for r in G.build_plan(
        R.BASE_KEY, {"general": questions["general"][: C.SMOKE_PROMPTS]}, tok,
        samples={"general": 1}, condition="smoke:lora")]
    ok_lora, lora_applied = smoke_lora_check(llm, tok, smoke_ids, paths, SamplingParams,
                                             LoRARequest)
    ok = ok_text and ok_lora
    rec = {
        **step_info("smoke", started, {"engine_seed": R.ENGINE_SEED,
                                       "seed_rule": f"request_seed('smoke:<model_key>', qid, 0, "
                                                    f"base={EVAL_SEED})"}),
        "passed": ok, "per_model": per, "lora_applied": lora_applied,
        "throughput": throughput, "engine_version": version,
        "gpu": _gpu_name(), "llm_kwargs": kw, "sampling": G.sampling_meta(), "items": items,
    }
    G.write_json(root / C.SMOKE_RECORD, rec)
    if not ok:
        log(f"smoke: FAILED text={per} lora_applied={lora_applied}")
        return 1
    write_step_manifest(root, "smoke", [C.SMOKE_RECORD])
    return 0


def _gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "unknown"


def set_aside(root: Path, model_key: str) -> list[str]:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    moved = []
    for name in (C.gen_csv(model_key), C.gen_meta(model_key)):
        p = Path(root) / name
        if p.exists():
            dest = p.with_name(f"{p.name}.incomplete.{stamp}")
            os.replace(p, dest)
            moved.append(rel(root, dest))
    return moved


def step_generate(args) -> int:
    started = G.utc()
    root = Path(args.root)
    questions = G.load_questions(root)
    tok = _tokenizer()
    paths = adapter_paths(args)
    plans = {k: G.build_plan(k, questions, tok) for k in R.MODEL_KEYS}
    pins = {k: G.model_pins(k, tok, paths[k]) for k in R.MODEL_KEYS}
    pending, resumed, set_asides = [], [], {}
    for key in R.MODEL_KEYS:
        probs = G.existing_problems(root, key, pins[key], plans[key])
        if probs == []:
            log(f"generate: {key} verifies; skipped")
            resumed.append(key)
            continue
        if probs is not None:
            log(f"generate: {key} present but does not verify ({probs}); setting aside")
            set_asides[key] = set_aside(root, key)
        pending.append(key)
    kw = G.llm_kwargs()
    if pending:
        LLM, SamplingParams, LoRARequest, version = _vllm()
        llm = LLM(**kw)
        gpu = _gpu_name()
        for key in pending:
            log(f"generate: {key}: {len(plans[key])} requests")
            rows, stats = G.run_generation(
                llm, tok, key, paths[key], plans[key],
                sampling_cls=SamplingParams, lora_cls=LoRARequest,
            )
            meta = G.build_meta(key, tok, paths[key], f"vllm {version}", gpu, stats, kw)
            G.write_outputs(root, key, rows, meta)
            _recovery_quietly(root)  # this model's CSV + meta are durable: cover them now
            log(f"generate: {key}: wrote {len(rows)} rows {stats}")
    bad = {k: G.existing_problems(root, k, pins[k], plans[k]) for k in R.MODEL_KEYS}
    bad = {k: v for k, v in bad.items() if v != []}
    if bad:
        log(f"generate: FAILED verification {bad}")
        return 1
    metas = {k: json.loads((root / C.gen_meta(k)).read_text()) for k in R.MODEL_KEYS}
    bench = {
        **step_info("generate", started, {"engine_seed": R.ENGINE_SEED, "seed_rule": G.SEED_RULE}),
        "per_model": {k: {**(m.get("throughput") or {}), "resumed": k in resumed,
                          "gpu": m["gpu"], "engine_version": m["engine_version"],
                          "utc": m["utc"]} for k, m in metas.items()},
        "set_aside": set_asides,
        "llm_kwargs": kw,
        "sampling": G.sampling_meta(),
        "rows_per_model": C.ROWS_PER_MODEL,
    }
    G.write_json(root / C.bench_path("generation"), bench)
    outs = [p for k in R.MODEL_KEYS for p in (C.gen_csv(k), C.gen_meta(k))]
    write_step_manifest(root, "generate", [*outs, C.bench_path("generation")])
    return 0


# --- train-bench ---------------------------------------------------------------------------------


def _versions(names: tuple[str, ...]) -> dict[str, str | None]:
    import importlib.metadata as md

    out: dict[str, str | None] = {"python": sys.version.split()[0]}
    for n in names:
        try:
            out[n] = md.version(n)
        except md.PackageNotFoundError:
            out[n] = None
    return out


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]


def bench_rows(root: Path) -> tuple[list[dict], list[int]]:
    """The first BENCH_STEPS x 16 rows of TRAIN_FILE in the BENCH_SEED order, and their indices."""
    from emx.qwen import train as T

    rows = read_jsonl(Path(root) / C.TRAIN_FILE)
    if len(rows) != C.TRAIN_ROWS:
        raise Refused(f"{C.TRAIN_FILE}: {len(rows)} rows != {C.TRAIN_ROWS}")
    n = C.BENCH_STEPS * R.RANK1.per_device_batch * R.RANK1.grad_accum
    idx = [i for mb in T.micro_batches(len(rows), C.BENCH_SEED) for i in mb][:n]
    return [rows[i] for i in idx], idx


TRAIN_STEPS = f"{C.BENCH_DIR}/train_steps.jsonl"  # one line per completed step, fsynced
TRAIN_FAILURE = f"{C.BENCH_DIR}/train_failure.json"  # written on any exception (incl. CUDA OOM)
HELDOUT_NOTE = "not measured: throughput benchmark, adapter discarded (EQ0 prereg §4.4)"
_execv = os.execv  # a hook so tests can stand in for the re-exec


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _set_aside_bench_files(root: Path) -> None:
    """A fresh (first-attempt) run moves an earlier run's step log and failure record aside."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for name in (TRAIN_STEPS, TRAIN_FAILURE):
        p = root / name
        if p.exists():
            os.replace(p, p.with_name(f"{p.name}.superseded.{stamp}"))


def step_train_bench(args) -> int:
    import dataclasses
    import hashlib

    import torch

    from emx.qwen import train as T

    started = G.utc()
    root = Path(args.root)
    sha = G.sha256_file(root / C.TRAIN_FILE)
    if sha != C.TRAIN_SHA256:
        raise Refused(f"{C.TRAIN_FILE}: sha256 {sha} != frozen {C.TRAIN_SHA256}")
    if not torch.cuda.is_available():
        raise Refused("train-bench needs CUDA")
    if args.after_oom and not args.gradient_checkpointing:
        raise Refused("--after-oom is the gradient-checkpointing retry")
    if not args.after_oom:
        _set_aside_bench_files(root)
    attempt = "after_oom" if args.after_oom else "first"
    ckpt = bool(args.gradient_checkpointing)
    first_failure = None  # the first attempt's OOM record, kept through the retry
    if args.after_oom and (root / TRAIN_FAILURE).exists():
        first_failure = json.loads((root / TRAIN_FAILURE).read_text())
    rows, idx = bench_rows(root)
    model, tok = T.load_base("cuda")
    model = T.attach_rank1(model, seed=C.BENCH_SEED)
    T.assert_rank1_layout(model)
    done: list[dict] = []

    def on_step(rec: dict) -> None:
        rec = {**rec, "attempt": attempt, "gradient_checkpointing": ckpt}
        append_jsonl(root / TRAIN_STEPS, rec)
        done.append(rec)
        _recovery_quietly(root)  # the appended line is fsynced: no stale entry if killed now

    def failure(e: BaseException) -> None:
        G.write_json(root / TRAIN_FAILURE, {
            **step_info("train-bench", started, {"bench_seed": C.BENCH_SEED}),
            "attempt": attempt, "gradient_checkpointing": ckpt,
            "error": f"{type(e).__name__}: {str(e)[:2000]}",
            "cuda_oom": isinstance(e, torch.cuda.OutOfMemoryError),
            "n_steps_completed": len(done), "steps_completed": done,
            "first_attempt_failure": first_failure,
        })

    try:
        res = T.bench(model, tok, rows, C.BENCH_STEPS, C.BENCH_SEED,
                      expect_trainable=R.RANK1.n_trainable, gradient_checkpointing=ckpt,
                      on_step=on_step)
    except torch.cuda.OutOfMemoryError as e:
        failure(e)
        if args.after_oom or ckpt:
            log("train-bench: CUDA OOM with gradient checkpointing; no further retry")
            return 1
        argv = [sys.executable, str(Path(__file__).resolve()), "train-bench",
                "--gradient-checkpointing", "--after-oom", "--root", str(root),
                "--adapter-dir", str(args.adapter_dir), "--lora-check-dir", str(args.lora_check_dir)]
        _recovery_quietly(root)
        log(f"train-bench: CUDA OOM without gradient checkpointing; re-exec: {argv[1:]}")
        sys.stdout.flush()
        sys.stderr.flush()
        _execv(sys.executable, argv)
        return 1  # only reached when the exec hook returns (tests)
    except BaseException as e:
        failure(e)
        raise
    rec = {
        **step_info("train-bench", started, {"bench_seed": C.BENCH_SEED,
                                             "lora_init_seed": C.BENCH_SEED}),
        **res,
        "oom_without_checkpointing": bool(args.after_oom),
        "first_attempt_failure": first_failure,
        "heldout_loss": HELDOUT_NOTE,
        "steps_log": TRAIN_STEPS,
        "gpu": _gpu_name(),
        "versions": _versions(("torch", "transformers", "peft", "bitsandbytes", "accelerate")),
        "recipe": dataclasses.asdict(R.RANK1),
        "attn_implementation": "sdpa",
        "dtype": R.DTYPE,
        "train_file": {"path": C.TRAIN_FILE, "sha256": sha},
        "row_indices_sha256": hashlib.sha256(json.dumps(idx).encode()).hexdigest(),
        "row_indices_head": idx[:16],
        "lora_modules": T.lora_module_names(model),
    }
    G.write_json(root / C.bench_path("train"), rec)
    s = res["summary"]
    log(f"train-bench: {s['s_per_step']:.3f} s/step, {s['tokens_per_s']:.0f} tok/s, peak "
        f"{(res['peak_memory_bytes'] or 0) / 2**30:.1f} GiB")
    outs = [C.bench_path("train"), TRAIN_STEPS]
    if (root / TRAIN_FAILURE).exists():
        outs.append(TRAIN_FAILURE)
    write_step_manifest(root, "train-bench", outs)
    return 0


# --- lora-check ----------------------------------------------------------------------------------


def lora_check_prompts(root: Path, tok) -> list[tuple[str, list[int]]]:
    """(qid, prompt ids) of the first LORA_CHECK_PROMPTS questions, general then ext48."""
    from emx.qwen.render import render_prompt

    qs = G.load_questions(root)
    flat = [q for qset in C.QSETS for q in qs[qset]][: C.LORA_CHECK_PROMPTS]
    return [(q["qid"], render_prompt(tok, q["question"])[1]) for q in flat]


def _last_logprobs(model, prompts):
    """[n_prompts, vocab] float32 next-token log_softmax at the last prompt position."""
    import numpy as np
    import torch

    dev = next(model.parameters()).device
    out = []
    with torch.no_grad():
        for _, ids in prompts:
            logits = model(input_ids=torch.tensor([ids], device=dev)).logits[0, -1]
            out.append(torch.log_softmax(logits.float(), dim=-1).cpu().numpy())
    return np.stack(out).astype(np.float32)


def top1_shift(base, adapted) -> float:
    """Mean over prompts of |log p_adapted(t) - log p_base(t)|, t = the base's top-1 token."""
    import numpy as np

    t = base.argmax(axis=1)
    rows = np.arange(base.shape[0])
    return float(np.mean(np.abs(adapted[rows, t] - base[rows, t])))


def synthetic_lora(model, prompts, out_dir: Path, layer: int = R.RANK1.layer) -> dict:
    """Wrap `model` with `rank1_lora_config(layer)`, set lora_A ~ N(0, 1/sqrt(in_features)) and
    lora_B = b_std * Z (Z ~ N(0, 1), one draw, seed LORA_CHECK_SEED), pick the smallest
    LORA_CHECK_B_STDS whose mean top-1 shift reaches LORA_CHECK_MIN_SHIFT_NATS (else the largest,
    `weak_shift`), record the base and scale-variant log-probs, and save the 1.0x adapter to
    `out_dir`. Returns arrays and facts for the record (CPU-testable on a tiny model)."""
    import torch

    from emx.qwen import train as T

    model.eval()
    base = _last_logprobs(model, prompts)
    peft = T.attach_rank1(model, seed=C.LORA_CHECK_SEED, layer=layer)
    T.assert_rank1_layout(peft, layer)
    peft.eval()
    mod = dict(peft.named_modules())[T.lora_module_names(peft)[0]]
    a_w, b_w = mod.lora_A["default"].weight, mod.lora_B["default"].weight
    g = torch.Generator().manual_seed(C.LORA_CHECK_SEED)
    a = torch.randn(tuple(a_w.shape), generator=g) / math.sqrt(a_w.shape[1])
    z = torch.randn(tuple(b_w.shape), generator=g)
    with torch.no_grad():
        a_w.copy_(a.to(a_w.dtype))

    def set_b(scale: float) -> None:
        with torch.no_grad():
            b_w.copy_((z * scale).to(b_w.dtype))

    tried, chosen = [], None
    for s in C.LORA_CHECK_B_STDS:
        set_b(s)
        shift = top1_shift(base, _last_logprobs(peft, prompts))
        tried.append({"b_std": s, "mean_top1_shift_nats": shift})
        log(f"lora-check hf: B std {s}: mean top-1 shift {shift:.4f} nats")
        if shift >= C.LORA_CHECK_MIN_SHIFT_NATS:
            chosen = s
            break
    weak = chosen is None
    if weak:
        chosen = C.LORA_CHECK_B_STDS[-1]
    variants = [base]
    for scale in C.LORA_CHECK_SCALES:
        set_b(chosen * scale)
        variants.append(_last_logprobs(peft, prompts))
    set_b(chosen)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    peft.save_pretrained(str(out_dir))
    cfg = json.loads((out_dir / "adapter_config.json").read_text())
    expect = {"r": 1, "lora_alpha": R.RANK1.alpha, "use_rslora": True,
              "layers_to_transform": [layer]}
    off = {k: (cfg.get(k), v) for k, v in expect.items() if cfg.get(k) != v}
    if off or list(cfg.get("target_modules") or []) != [R.RANK1.module]:
        raise RuntimeError(f"saved adapter_config off: {off} target_modules "
                           f"{cfg.get('target_modules')}")
    from safetensors import safe_open

    with safe_open(str(out_dir / "adapter_model.safetensors"), "pt") as fh:
        keys = {k: list(fh.get_slice(k).get_shape()) for k in fh.keys()}  # noqa: SIM118
    if len(keys) != 2 or not all(f"layers.{layer}.mlp.{R.RANK1.module}.lora_" in k for k in keys):
        raise RuntimeError(f"saved adapter tensors {sorted(keys)}")
    return {
        "variants": variants, "tried": tried, "chosen_b_std": chosen, "weak_shift": weak,
        "shift_achieved_nats": top1_shift(base, variants[1 + C.LORA_CHECK_SCALES.index(1.0)]),
        "adapter_config": cfg, "safetensors_keys": keys,
        "init": {"lora_A": f"N(0, 1/sqrt({a_w.shape[1]}))", "lora_B": "b_std * N(0, 1)",
                 "generator_seed": C.LORA_CHECK_SEED, "lora_dtype": str(a_w.dtype)},
    }


def write_lora_hf(root: Path, started: str, prompts, out_dir: Path, res: dict) -> None:
    """bench/lora_check_hf_logprobs.npy ([variant, prompt, vocab] float32) and its JSON record."""
    import numpy as np

    arr = np.stack(res["variants"]).astype(np.float32)
    npy = Path(root) / LORA_HF_NPY
    npy.parent.mkdir(parents=True, exist_ok=True)
    tmp = npy.with_name(f".{npy.name}.tmp")  # a temp name the recovery manifest skips
    with open(tmp, "wb") as fh:
        np.save(fh, arr)
    os.replace(tmp, npy)
    top = {
        name: [[int(t) for t in np.argsort(-v[i])[:10]] for i in range(v.shape[0])]
        for name, v in zip(LORA_VARIANTS, res["variants"], strict=True)
    }
    rec = {
        **step_info("lora-check-hf", started, {"lora_check_seed": C.LORA_CHECK_SEED}),
        "prompts": [{"qid": q, "n_tokens": len(ids)} for q, ids in prompts],
        **{k: v for k, v in res.items() if k != "variants"},
        "variants": list(LORA_VARIANTS), "scales": list(C.LORA_CHECK_SCALES),
        "logprobs_npy": LORA_HF_NPY, "logprobs_npy_shape": list(arr.shape),
        "top10_token_ids": top,
        "adapter_dir": str(out_dir),
        "adapter_files_sha256": {n: G.sha256_file(Path(out_dir) / n) for n in R.ADAPTER_FILES},
        "gpu": _gpu_name(),
        "versions": _versions(("torch", "transformers", "peft", "safetensors")),
    }
    G.write_json(Path(root) / LORA_HF_JSON, rec)


def step_lora_hf(args) -> int:
    import torch

    from emx.qwen import train as T

    started = G.utc()
    root = Path(args.root)
    if not torch.cuda.is_available():
        raise Refused("lora-check --part hf needs CUDA")
    model, tok = T.load_base("cuda")
    prompts = lora_check_prompts(root, tok)
    out_dir = Path(args.lora_check_dir)
    res = synthetic_lora(model, prompts, out_dir)
    write_lora_hf(root, started, prompts, out_dir, res)
    return 0


def vllm_topk(logprobs_dict, k: int) -> tuple[list[int], list[float]]:
    """The k best (token id, log-prob) of one position's vLLM logprobs dict, by log-prob."""
    items = sorted(((int(t), float(getattr(v, "logprob", v))) for t, v in logprobs_dict.items()),
                   key=lambda x: -x[1])[:k]
    return [t for t, _ in items], [lp for _, lp in items]


def step_lora_vllm(args) -> int:
    started = G.utc()
    root = Path(args.root)
    tok = _tokenizer()
    prompts = lora_check_prompts(root, tok)
    lora_dir = Path(args.lora_check_dir)
    cfg = json.loads((lora_dir / "adapter_config.json").read_text())
    if cfg.get("r") != 1 or cfg.get("lora_alpha") != R.RANK1.alpha:
        raise Refused(f"{lora_dir}: not the synthetic rank-1 adapter ({cfg.get('r')}, "
                      f"{cfg.get('lora_alpha')})")
    LLM, SamplingParams, LoRARequest, version = _vllm()
    kw = G.llm_kwargs(max_loras=1)
    llm = LLM(**kw)
    params = SamplingParams(max_tokens=1, temperature=0.0, logprobs=C.LORA_CHECK_TOPK)
    inputs = [{"prompt_token_ids": ids} for _, ids in prompts]
    lora = LoRARequest(LORA_CHECK_NAME, LORA_CHECK_INT_ID, str(lora_dir))
    res = {}
    for name, req in (("base", None), ("adapter", lora)):
        outs = llm.generate(inputs, sampling_params=params, lora_request=req, use_tqdm=False)
        rows = []
        for (qid, ids), o in zip(prompts, outs, strict=True):
            if list(o.prompt_token_ids) != list(ids):
                raise RuntimeError(f"{qid}: vLLM prompt ids differ from our render")
            t, lp = vllm_topk(o.outputs[0].logprobs[0], C.LORA_CHECK_TOPK)
            rows.append({"qid": qid, "token_ids": t, "logprobs": lp})
        res[name] = rows
    mode = None
    try:
        mode = str(llm.llm_engine.model_config.logprobs_mode)
    except AttributeError:
        pass
    rec = {
        **step_info("lora-check-vllm", started, {"engine_seed": R.ENGINE_SEED}),
        "engine_version": version, "gpu": _gpu_name(), "llm_kwargs": kw,
        "sampling": {"max_tokens": 1, "temperature": 0.0, "logprobs": C.LORA_CHECK_TOPK},
        "logprobs_mode": mode, "lora_request": [LORA_CHECK_NAME, LORA_CHECK_INT_ID, str(lora_dir)],
        "adapter_config": cfg, **res,
    }
    G.write_json(root / LORA_VLLM_JSON, rec)
    return 0


def score_lora_check(
    hf,
    vllm_rec: dict,
    shift_nats: float,
    weak_shift: bool = False,
    variants: tuple[str, ...] = LORA_VARIANTS,
) -> dict:
    """Compare vLLM's top-k log-probs with HF's full vectors `hf` [variant, prompt, vocab].

    `passed` is the overall verdict: tolerance (max |diff| on the adapter < LORA_CHECK_MAX_ABS_NATS)
    AND discrimination (vLLM nearest HF 1.0x on >= 19 of 20 prompts) AND shift (the HF 1.0x
    adapter moved the base's top-1 log-prob by >= LORA_CHECK_MIN_SHIFT_NATS on average, and the
    HF part did not flag `weak_shift`) AND
    exactly LORA_CHECK_PROMPTS prompts. The components are kept as separate flags."""
    import numpy as np

    idx = {v: i for i, v in enumerate(variants)}
    one = idx[f"x{1.0}"]
    per, adapter_max, base_max, adapter_mean, base_mean, nearest = [], [], [], [], [], 0
    for i, (a, b) in enumerate(zip(vllm_rec["adapter"], vllm_rec["base"], strict=True)):
        ta, la = np.array(a["token_ids"]), np.array(a["logprobs"])
        tb, lb = np.array(b["token_ids"]), np.array(b["logprobs"])
        d_a = np.abs(la - hf[one, i, ta])
        d_b = np.abs(lb - hf[idx["base"], i, tb])
        means = {v: float(np.mean(np.abs(la - hf[idx[v], i, ta]))) for v in variants if v != "base"}
        is_1x = all(means[f"x{1.0}"] < m for v, m in means.items() if v != f"x{1.0}")
        nearest += int(is_1x)
        adapter_max.append(float(d_a.max()))
        base_max.append(float(d_b.max()))
        adapter_mean.append(float(d_a.mean()))
        base_mean.append(float(d_b.mean()))
        per.append({"qid": a["qid"], "max_abs_diff_adapter": adapter_max[-1],
                    "max_abs_diff_base": base_max[-1], "mean_abs_diff_by_scale": means,
                    "nearest_1x": is_1x})
    n = len(per)
    need = LORA_DISCRIMINATION_MIN
    tolerance = max(adapter_max) < C.LORA_CHECK_MAX_ABS_NATS
    discrimination = nearest >= need
    shift_ok = shift_nats >= C.LORA_CHECK_MIN_SHIFT_NATS and not weak_shift
    n_ok = n == C.LORA_CHECK_PROMPTS
    return {
        "n_prompts": n,
        "max_abs_diff_adapter": max(adapter_max),
        "max_abs_diff_base": max(base_max),
        "mean_abs_diff_adapter": float(np.mean(adapter_mean)),
        "mean_abs_diff_base": float(np.mean(base_mean)),
        "n_prompts_nearest_1x": nearest,
        "threshold_max_abs_nats": C.LORA_CHECK_MAX_ABS_NATS,
        "discrimination_min": need,
        "shift_nats": shift_nats,
        "min_shift_nats": C.LORA_CHECK_MIN_SHIFT_NATS,
        "tolerance_passed": tolerance,
        "discrimination_passed": discrimination,
        "shift_passed": shift_ok,
        "n_prompts_passed": n_ok,
        "passed": tolerance and discrimination and shift_ok and n_ok,
        "per_prompt": per,
    }


def step_lora_score(args) -> int:
    import numpy as np

    started = G.utc()
    root = Path(args.root)
    hf_rec = json.loads((root / LORA_HF_JSON).read_text())
    vllm_rec = json.loads((root / LORA_VLLM_JSON).read_text())
    hf = np.load(root / LORA_HF_NPY)
    if tuple(hf_rec["variants"]) != LORA_VARIANTS or hf.shape[0] != len(LORA_VARIANTS):
        raise RuntimeError(f"HF variants {hf_rec['variants']} / shape {hf.shape}")
    qids = [p["qid"] for p in hf_rec["prompts"]]
    if [r["qid"] for r in vllm_rec["adapter"]] != qids:
        raise RuntimeError("HF and vLLM prompt lists differ")
    res = score_lora_check(hf, vllm_rec, float(hf_rec["shift_achieved_nats"]),
                           bool(hf_rec["weak_shift"]))
    rec = {
        **step_info("lora-check", started, {"lora_check_seed": C.LORA_CHECK_SEED,
                                            "engine_seed": R.ENGINE_SEED}),
        **res,
        "chosen_b_std": hf_rec["chosen_b_std"], "weak_shift": hf_rec["weak_shift"],
        "shift_achieved_nats": hf_rec["shift_achieved_nats"], "tried": hf_rec["tried"],
        "lora_scale_checked": R.RANK1.scale,
        "note": "at r = 1, alpha/sqrt(r) == alpha/r: this checks the 512 scale and the layer "
                "targeting, not the rsLoRA flag",
        "inputs": {p: G.sha256_file(root / p) for p in (LORA_HF_JSON, LORA_HF_NPY, LORA_VLLM_JSON)},
    }
    G.write_json(root / C.bench_path("lora_check"), rec)
    print(f"EQ0_LORA_CHECK passed={res['passed']} tolerance={res['tolerance_passed']} "
          f"discrimination={res['discrimination_passed']} shift={res['shift_passed']} "
          f"n_prompts={res['n_prompts']}", flush=True)
    write_step_manifest(root, "lora-check",
                        [C.bench_path("lora_check"), LORA_HF_JSON, LORA_HF_NPY, LORA_VLLM_JSON])
    return 0


def step_lora_check(args) -> int:
    return {"hf": step_lora_hf, "vllm": step_lora_vllm, "score": step_lora_score}[args.part](args)


def step_manifest(args) -> int:
    closing_manifest(Path(args.root))
    return 0


def step_recovery(args) -> int:
    out = write_recovery_manifest(Path(args.root))
    n = len(out.read_text().splitlines())
    log(f"recovery: wrote {RECOVERY} ({n} files)")
    return 0


STEP_FUNCS = {
    "download": step_download,
    "smoke": step_smoke,
    "generate": step_generate,
    "train-bench": step_train_bench,
    "lora-check": step_lora_check,
    "manifest": step_manifest,
    "recovery": step_recovery,  # not a step: never in C.STEPS, never completes anything
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("step", choices=[*C.STEPS, "recovery"])
    p.add_argument("--part", choices=["hf", "vllm", "score"], help="lora-check only")
    p.add_argument("--root", default=str(ROOT))
    p.add_argument("--adapter-dir", default=C.ADAPTER_DIR)
    p.add_argument("--lora-check-dir", default=C.LORA_CHECK_ADAPTER_DIR)
    p.add_argument("--gradient-checkpointing", action="store_true", help="train-bench only")
    p.add_argument("--after-oom", action="store_true",
                   help="train-bench only: the one retry after a CUDA OOM (refuses another)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.step == "lora-check" and not args.part:
        parser().error("lora-check needs --part hf|vllm|score")
    if args.step != "lora-check" and args.part:
        parser().error("--part belongs to lora-check")
    if args.step != "train-bench" and (args.gradient_checkpointing or args.after_oom):
        parser().error("--gradient-checkpointing / --after-oom belong to train-bench")
    label = args.step + (f" --part {args.part}" if args.part else "")
    t = time.time()
    log(f"step {label} start (commit {G.git_commit()})")
    try:
        rc = STEP_FUNCS[args.step](args)
    except Refused as e:
        log(f"step {label} REFUSED: {e}")
        return REFUSED
    finally:  # success or failure: whatever results/EQ0 holds now can be pulled
        if args.step != "recovery":
            _recovery_quietly(Path(args.root))
    log(f"step {label} {'done' if rc == 0 else f'FAILED rc={rc}'} in {time.time() - t:.0f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
