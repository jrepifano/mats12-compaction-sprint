"""P42 on-pod steps (plan.md "Setup", "Automatic rules", "Cap"; run by run.sh through the generic
probe runner, scripts/probe_pod.py). Run from the probe directory with
PYTHONPATH="$PWD:$PWD/../../src":

    python -m p42.pod download   # peft venv: both models, the SAE subdir, the chat template;
                                 #   pins asserted against emx.e1.registry; prompts sha256
    python -m p42.pod smoke      # peft venv: the 1,000(+100)-prompt smoke unit through every
                                 #   phase (gen in a venv-vllm child), gates, wall-clock
                                 #   projection and cuts -> results/smoke.json (rc 3: over budget)
    python -m p42.pod gen        # vllm venv: greedy on-policy DPO responses, every gen=True prompt
    python -m p42.pod feats      # peft venv: DPO then SFT forwards -> /ws/p42_feats (+ S3f FVU)
    python -m p42.pod grads      # peft venv: S4 projected gradients -> /ws/p42_feats/grads.npy
    python -m p42.pod score      # peft venv: results/scores.csv and results/pca/

Settings the smoke decides (results/smoke.json `settings`): `attn` ("sdpa", or "eager" when the
16-prompt sdpa-vs-eager NLL check, run first, differs by more than 0.05 nats or is not finite;
the rest of the smoke then runs with that implementation), `resp_tokens`
(K.RESP_FEATURE_TOKENS = 128, or 64 after the second cut) and `grad_ref_n` (K.N_GEN_REF = 20,000,
or 10,000 after the first cut). The bulk steps refuse to run without a passing smoke.json (env
P42_SKIP_SMOKE=1 overrides, with the defaults).

Smoke subset (seeded K.SEED_SAMPLE + 1): 500 pool, 300 id_ref (150 gen-ref, 150 others), 100
planted (50 + 50), 100 id_heldout, plus 100 sft_ref so that every score column (knn_sftref)
is exercised: 1,100 prompts. Its work files live in /ws/p42_smoke and its outputs in
results/smoke/ (small).

Projection (smoke): per-phase seconds-per-token rates from the smoke (excluding model loads,
which are added once per phase) times the full run's token counts; prompt tokens come from
`n_tokens` (capped at K.MAX_PROMPT_TOKENS) plus the template, response tokens from the smoke's
mean response length. Remaining = gen + feats(dpo) + feats(sft) + grads + score (a fixed
SCORE_ALLOW_S, as its cost is memmap reads and fixed-size solves) + MANIFEST_S. Gate: remaining
<= 0.8 x time left, else cut the S4 reference to 10k, then the response feature tokens to 64,
else rc 3. Time left = the deadline minus now, the deadline being env P42_DEADLINE_EPOCH, else
the killswitch deadline (/ws/killswitch/deadline) minus SALVAGE_S (the runner salvages 15 min
before it, plus 5 min for the manifest), else env P42_T0 (run.sh's start) + P42_BUDGET_S
(default: probe.toml gpu_hours x 3600 - 15 min).

Every step writes results/run_<step>.json (per-phase wall-clock, counts, settings) atomically,
and results/timing.json collects them. Exit codes: 0 ok, 1 a failed check or error, 3 smoke
projection over budget even after both cuts.

Exact response ids. gen writes results/gen/response_ids.jsonl (in the manifest): per gen prompt
the first K.RESP_FEATURE_TOKENS generated ids exactly as vLLM returned them (stop token
included when returned), n_resp_tokens and the hash of the prompt ids generation was given.
feats, grads and score require it; there is no re-tokenising fallback. feats/grads compare the
prompt-id hashes with their own tokenisation (venv-peft vs venv-vllm tokenizers): above 1%
mismatches they fail, below they warn and record the pids.

Checkpoints and fingerprints. Each checkpointed stage has its own directory with a
fingerprint.json: /ws/p42_feats/gen_parts (model pin, sampling, max_model_len, chunking,
contracts.py; plus each part row's prompt-id hash), /ws/p42_feats/{dpo,sft} (model pin,
attention implementation, response tokens, feature layers, SAE pin, a hash of every prompt
row's pid/set/text/gen/gen_rank, the response_ids.jsonl sha, and the sha of contracts.py,
feats.py and pod.py) and /ws/p42_feats/grads (the same plus grad_ref_n and the projection
settings, and grads.py). Resumption is by shard within a directory whose fingerprint matches.
A mismatching fingerprint (or checkpoints without one) is stale: the directory is deleted,
logged as STALE CHECKPOINT, recorded as `stale_reset` in the run record, and the work redone.
score refuses (rc 1) unless the dpo and sft stores (and grads, when present) are complete under
exactly the fingerprints the current inputs and settings give. EMX_GIT_COMMIT is recorded beside
each fingerprint but is not part of it (log.md commits would otherwise force recomputation).

Feature store (/ws/p42_feats/<model>/, never results/): `<model>_<w>_L<L>.npy` float16 [N, d]
(w = p prompt-content mean, r = response mean; row = line of prompts.jsonl),
`<model>_scalars.npy` float64 [N, len(SCALARS)] (NaN until filled); /ws/p42_feats/grads/:
`grads.npy` float16 [N, 16384] and `grads_meta.npy` float64 [N, 2] (loss, pre-normalisation
norm). A row is filled when its scalars' n_content (grads: loss) is finite. Forwards use
use_cache=False and keep only the hidden states they pool (forward hooks).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from p42 import contracts as K

# vLLM JIT-compiles kernels with `ninja` from the running venv's bin/ (P20/P34 gotcha)
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

WS = Path(os.environ.get("P42_WS", "/ws"))
RESULTS = K.RESULTS_DIR
SMOKE_JSON = RESULTS / "smoke.json"
TIMING_JSON = RESULTS / "timing.json"
VLLM_PY = os.environ.get("P42_VLLM_PY", "/ws/venv-vllm/bin/python")
KILLSWITCH_DEADLINE = Path(os.environ.get("P42_KS_DEADLINE", "/ws/killswitch/deadline"))

MODEL_PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja", "*.model"]
EXPECT_CFG = {"num_hidden_layers": K.N_LAYERS, "hidden_size": 4096, "intermediate_size": 11008}
SCALARS = ("nll_sum", "nll_mean", "n_content", "resp_lp_sum", "resp_lp_mean", "n_resp", "fvu")
SC = {c: i for i, c in enumerate(SCALARS)}
FEATS_TOKENS = int(os.environ.get("P42_FEATS_TOKENS", "32768"))  # padded tokens per batch
GRADS_TOKENS = int(os.environ.get("P42_GRADS_TOKENS", "16384"))
MAX_BATCH = int(os.environ.get("P42_MAX_BATCH", "64"))
SHARD = 4096  # prompts per resumable shard (feats, grads)
GEN_CHUNK = 8192  # prompts per vLLM call / part file
GEN_MEM = float(os.environ.get("P42_GEN_MEM", "0.92"))
SAE_HS_DEFAULT = 17  # model.layers.16 output
SALVAGE_S = 20 * 60
MANIFEST_S = 120
PROC_OVERHEAD_S = 60  # per bulk step: interpreter, imports, tokenising, CUDA context
SCORE_ALLOW_S = 600
PROJ_FRACTION = 0.8
NLL_RANGE = (0.5, 4.0)
ATTN_TOL = 0.05
SMOKE_SIZES = {"pool": 500, "id_ref_gen": 150, "id_ref_other": 150, K.PLANTED_MED: 50,
               K.PLANTED_LAW: 50, K.ID_HELDOUT: 100, K.SFT_REF: 100}
ATTN_CHECK_N = 16
# S4's per-example loss vs the feature pass's response NLL: same weights, tokens and fp32 head,
# so only bf16 batch-shape noise separates them (per-token logprob noise ~1e-2, averaged over
# <= 128 tokens); 0.1 nats on the max over the smoke's ~850 examples flags real mismatches
# (wrong tokens or positions give differences of order 1).
GRAD_LOSS_TOL = 0.1
TEMPLATE_CHECK_N = 500

# injectable for tests
SUBPROC = subprocess.run
LOADER: Callable | None = None  # (name, attn) -> model; default load_model
TOKENIZER: Callable | None = None  # () -> tokenizer; default load_tok
RENDER: Callable | None = None  # (content, tok) -> rendered prompt; default emx.formats chat
DEVICE: str | None = None  # default cuda


class StepFailed(Exception):
    """A check failed: the step exits 1 with the message (fail closed)."""


# --- small helpers ------------------------------------------------------------------------------


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{utc()} [p42.pod] {msg}", flush=True)


def write_json(path: str | Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=_jsonable) + "\n")
    os.replace(tmp, path)


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def read_json(path: str | Path) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text()) if p.is_file() else None


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).is_file():
        raise StepFailed(f"{path} missing")
    # split on "\n" only: str.splitlines() also splits on U+2028, U+0085 etc., which
    # ensure_ascii=False JSON keeps unescaped inside strings (real prompts contain them)
    with open(path, encoding="utf-8", newline="\n") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def device() -> str:
    return DEVICE or "cuda"


class Timer:
    """Per-phase wall-clock: `with T("name"): ...`; T.phases is {name: seconds}."""

    def __init__(self):
        self.phases: dict[str, float] = {}
        self.t0 = time.time()

    def __call__(self, name: str):
        timer = self

        class _Ctx:
            def __enter__(self):
                self.s = time.time()

            def __exit__(self, *exc):
                timer.phases[name] = timer.phases.get(name, 0.0) + time.time() - self.s
                return False
        return _Ctx()

    def total(self) -> float:
        return time.time() - self.t0


def record(step: str, T: Timer, results: Path | None = None, **fields) -> dict:
    results = results or RESULTS
    rec = {"step": step, "finished_utc": utc(), "seconds": round(T.total(), 1),
           "phases_s": {k: round(v, 2) for k, v in T.phases.items()},
           "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown"), **fields}
    write_json(results / f"run_{step}.json", rec)
    if results == RESULTS:
        timing = read_json(TIMING_JSON) or {}
        timing[step] = {"seconds": rec["seconds"], "phases_s": rec["phases_s"]}
        write_json(TIMING_JSON, timing)
    return rec


# --- inputs -------------------------------------------------------------------------------------


def load_prompts(path: Path | None = None) -> list[dict]:
    rows = read_jsonl(path or K.PROMPTS_PATH)
    pids = [r["pid"] for r in rows]
    if pids != sorted(pids) or len(set(pids)) != len(pids):
        raise StepFailed("prompts.jsonl must be sorted by unique pid")
    missing = [f for f in K.PROMPT_FIELDS if f not in rows[0]]
    if missing:
        raise StepFailed(f"prompts.jsonl lacks fields {missing}")
    return rows


def prompts_sha_expected(meta: dict) -> str | None:
    """The prompts.jsonl sha256 recorded by p42.data (key `prompts_sha256`; a nested
    {"sha256": {"prompts.jsonl": ...}} or {"outputs": {...}} is accepted too)."""
    if isinstance(meta.get("prompts_sha256"), str):
        return meta["prompts_sha256"]
    for k in ("sha256", "outputs", "files"):
        sub = meta.get(k)
        if isinstance(sub, dict):
            for name in ("prompts.jsonl", "data/P42/prompts.jsonl"):
                v = sub.get(name)
                if isinstance(v, str):
                    return v
                if isinstance(v, dict) and isinstance(v.get("sha256"), str):
                    return v["sha256"]
    return None


def verify_prompts() -> dict:
    meta = read_json(K.PROMPTS_META_PATH)
    if meta is None:
        raise StepFailed(f"{K.PROMPTS_META_PATH} missing ([pod].push)")
    want = prompts_sha_expected(meta)
    got = sha256_file(K.PROMPTS_PATH)
    if want is None:
        raise StepFailed("prompts.meta.json records no prompts sha256 (key prompts_sha256)")
    if got != want:
        raise StepFailed(f"prompts.jsonl sha256 {got} != meta {want}")
    return {"prompts_sha256": got, "meta_sha256": sha256_file(K.PROMPTS_META_PATH)}


def smoke_subset(rows: Sequence[dict], sizes: dict | None = None) -> list[dict]:
    sizes = sizes or SMOKE_SIZES
    rng = random.Random(K.SEED_SAMPLE + 1)
    pick: list[dict] = []
    groups = {
        "pool": [r for r in rows if r["set"] == K.POOL],
        "id_ref_gen": [r for r in rows if r["set"] == K.ID_REF and r["gen"]],
        "id_ref_other": [r for r in rows if r["set"] == K.ID_REF and not r["gen"]],
        **{s: [r for r in rows if r["set"] == s] for s in (*K.PLANTED, K.ID_HELDOUT, K.SFT_REF)},
    }
    for g, n in sizes.items():
        cand = groups[g]
        pick += rng.sample(cand, min(n, len(cand)))
    return sorted(pick, key=lambda r: r["pid"])


# --- models -------------------------------------------------------------------------------------


def check_pins() -> None:
    from emx.e1.registry import STAGES

    for name, (repo, rev) in K.MODELS.items():
        s = STAGES[name]
        if (s["hf_id"], s["revision"]) != (repo, rev):
            raise StepFailed(f"{name}: contracts pin {repo}@{rev} != registry "
                             f"{s['hf_id']}@{s['revision']}")


def model_path(name: str) -> Path:
    from huggingface_hub import snapshot_download

    repo, rev = K.MODELS[name]
    return Path(snapshot_download(repo, revision=rev, allow_patterns=MODEL_PATTERNS,
                                  local_files_only=True))


def sae_dir() -> Path | None:
    from huggingface_hub import snapshot_download

    try:
        root = snapshot_download(K.SAE_REPO, revision=K.SAE_REV,
                                 allow_patterns=[f"{K.SAE_SUBDIR}/*"], local_files_only=True)
    except Exception:  # noqa: BLE001
        return None
    d = Path(root) / K.SAE_SUBDIR
    return d if (d / "sae_weights.safetensors").is_file() else None


def load_tok():
    if TOKENIZER is not None:
        return TOKENIZER()
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_path("dpo"))


def render_fn(tok) -> Callable[[str], str]:
    if RENDER is not None:
        return lambda content: RENDER(content, tok)
    from emx import formats as F

    return lambda content: F.render_prompt("chat", content, tok)


def load_model(name: str, attn: str):
    if LOADER is not None:
        return LOADER(name, attn)
    import torch
    from transformers import AutoModelForCausalLM

    m = AutoModelForCausalLM.from_pretrained(model_path(name), dtype=torch.bfloat16,
                                             attn_implementation=attn, device_map=device())
    m.eval()
    bad = {k: (getattr(m.config, k, None), v) for k, v in EXPECT_CFG.items()
           if getattr(m.config, k, None) != v}
    if bad:
        raise StepFailed(f"{name} config mismatch {bad}")
    return m


def set_attn(model, attn: str) -> None:
    if hasattr(model, "set_attn_implementation"):
        model.set_attn_implementation(attn)
    else:
        model.config._attn_implementation = attn


def free() -> None:
    """Collect garbage and return cached CUDA memory (callers `del` their model refs first)."""
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def enable_tf32() -> None:
    import torch

    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True  # fp32 lm_head and SAE matmuls only


# --- context ------------------------------------------------------------------------------------


@dataclass
class Ctx:
    tag: str  # "bulk" | "smoke"
    rows: list[dict]
    work: Path  # feature store and gen parts (/ws)
    results: Path
    responses: Path
    resp_tokens: int = K.RESP_FEATURE_TOKENS
    grad_ref_n: int = K.N_GEN_REF
    attn: str = "sdpa"
    timings: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def gen_parts(self) -> Path:
        return self.work / "gen_parts"

    def idx(self, pred) -> np.ndarray:
        return np.array([i for i, r in enumerate(self.rows) if pred(r)], dtype=np.int64)


def bulk_ctx(rows: list[dict] | None = None) -> Ctx:
    return Ctx("bulk", rows if rows is not None else load_prompts(), WS / "p42_feats", RESULTS,
               K.RESPONSES_PATH)


def smoke_ctx(rows: list[dict] | None = None) -> Ctx:
    sub = smoke_subset(rows if rows is not None else load_prompts())
    return Ctx("smoke", sub, WS / "p42_smoke", RESULTS / "smoke",
               RESULTS / "smoke" / "gen" / "responses.jsonl")


def settings() -> dict:
    s = read_json(SMOKE_JSON)
    if s is None or not s.get("pass"):
        if os.environ.get("P42_SKIP_SMOKE") == "1":
            log("P42_SKIP_SMOKE=1: default settings without a passing smoke")
            return {"attn": "sdpa", "resp_tokens": K.RESP_FEATURE_TOKENS,
                    "grad_ref_n": K.N_GEN_REF}
        raise StepFailed("no passing results/smoke.json (run the smoke first)")
    return s["settings"]


def with_settings(ctx: Ctx) -> Ctx:
    s = settings()
    return replace(ctx, attn=s["attn"], resp_tokens=int(s["resp_tokens"]),
                   grad_ref_n=int(s["grad_ref_n"]))


# --- download -----------------------------------------------------------------------------------


def step_download(args) -> int:
    from huggingface_hub import snapshot_download

    T = Timer()
    check_pins()
    info: dict[str, Any] = {"hf_hub_disable_xet": os.environ.get("HF_HUB_DISABLE_XET")}
    with T("inputs"):
        info["inputs"] = verify_prompts()
    for name, (repo, rev) in K.MODELS.items():
        with T(f"model_{name}"):
            p = Path(snapshot_download(repo, revision=rev, allow_patterns=MODEL_PATTERNS))
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        info[name] = {"path": str(p), "bytes": size,
                      "MB_s": round(size / 1e6 / max(T.phases[f"model_{name}"], 1e-3), 1)}
        cfg = json.loads((p / "config.json").read_text())
        bad = {k: (cfg.get(k), v) for k, v in EXPECT_CFG.items() if cfg.get(k) != v}
        if bad:
            raise StepFailed(f"{name} config mismatch {bad}")
        info[name]["architectures"] = cfg.get("architectures")
    with T("sae"):
        try:
            snapshot_download(K.SAE_REPO, revision=K.SAE_REV, allow_patterns=[f"{K.SAE_SUBDIR}/*"])
            info["sae"] = {"dir": str(sae_dir()), "ok": sae_dir() is not None}
        except Exception as e:  # noqa: BLE001  (S3f is exploratory: log and go on)
            info["sae"] = {"ok": False, "error": repr(e)[:500]}
            log(f"SAE download failed (S3f will be skipped): {e!r}")
    with T("template"):
        from emx import formats as F

        F.chat_template()  # downloads and sha-checks the pinned Instruct template
        from transformers import AutoTokenizer

        t_dpo = AutoTokenizer.from_pretrained(model_path("dpo"))
        t_sft = AutoTokenizer.from_pretrained(model_path("sft"))
        if t_dpo.get_vocab() != t_sft.get_vocab():
            raise StepFailed("DPO and SFT tokenizers differ (the run tokenises once, with DPO's)")
        info["template_sha256"] = F.template_sha256("chat")
    record("download", T, **info)
    return 0


# --- gen (venv-vllm) ----------------------------------------------------------------------------


def encoded(ctx: Ctx, idx: Sequence[int], tok=None):
    from p42.feats import encode_prompts, template_parts

    tok = tok or load_tok()
    parts = template_parts(tok, render_fn(tok))
    return tok, parts, encode_prompts(tok, [ctx.rows[i]["text"] for i in idx], parts)


def template_check(tok, parts, rows: Sequence[dict], n: int = TEMPLATE_CHECK_N) -> dict:
    """The three-part ids against tokenising the whole rendered string, and the rendered string
    against prefix + content + suffix, on a seeded sample."""
    from p42.feats import full_string_match

    rng = random.Random(K.SEED_SAMPLE + 2)
    sample = rng.sample(list(rows), min(n, len(rows)))
    render = render_fn(tok)
    bad_render = [r["pid"] for r in sample
                  if render(r["text"]) != parts.prefix + r["text"] + parts.suffix]
    if bad_render:
        raise StepFailed(f"rendered prompt != prefix + content + suffix for {bad_render[:5]}")
    mism = [r["pid"] for r in sample if not full_string_match(tok, r["text"], parts)]
    return {"n": len(sample), "full_string_mismatch": len(mism), "mismatch_pids": mism[:20],
            "leading_newline": sum(r["text"][:1] in "\r\n" for r in sample if r["text"])}


def gen_done_part(path: Path, pids: Sequence[str], shas: Sequence[str]) -> bool:
    """A part is reusable only when its pids and the hashes of the prompt ids it was generated
    from equal this run's (and the parts directory's fingerprint matched, `gen_fingerprint`)."""
    if not path.is_file():
        return False
    try:
        got = [(r["pid"], r["prompt_sha"]) for r in read_jsonl(path)]
    except (json.JSONDecodeError, KeyError):
        return False
    return got == list(zip(pids, shas, strict=True))


def gen_fingerprint(max_len: int) -> dict:
    """What generation depends on beyond the prompt ids (checked per row): model, sampling,
    context length and the contracts (gen code changes that alter outputs live there)."""
    return {"stage": "gen", "model": list(K.MODELS["dpo"]),
            "temperature": K.GEN_TEMPERATURE, "max_tokens": K.GEN_MAX_NEW_TOKENS,
            "seed": K.SEED_SAMPLE, "max_model_len": max_len, "gen_chunk": GEN_CHUNK,
            "resp_feature_tokens": K.RESP_FEATURE_TOKENS,
            "code_sha": code_sha(("contracts.py",))}


def check_fingerprint(work_dir: Path, fp: dict, what: str) -> str | None:
    """Compare `work_dir`/fingerprint.json with fp. On a mismatch (or none recorded beside
    existing files) the directory's checkpoints are stale: delete them and return the reason
    (the caller logs it and records it in its run record); the work is then redone."""
    path = work_dir / "fingerprint.json"
    old = read_json(path)
    reason = None
    if old is not None and old.get("fp") != fp:
        diff = sorted(k for k in set(old.get("fp", {})) | set(fp)
                      if old.get("fp", {}).get(k) != fp.get(k))
        reason = f"{what}: fingerprint changed ({', '.join(diff)}); recomputing"
    elif old is None and work_dir.is_dir() and any(work_dir.iterdir()):
        reason = f"{what}: checkpoints without a fingerprint; recomputing"
    if reason:
        log(f"STALE CHECKPOINT {reason}")
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    write_json(path, {"fp": fp, "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown"),
                      "utc": utc()})
    return reason


def step_gen(args) -> int:
    from emx.e1.registry import EOT_ID, IM_END_ID

    ctx = smoke_ctx() if args.smoke else bulk_ctx()
    T = Timer()
    gen_idx = ctx.idx(lambda r: r["gen"])
    with T("tokenise"):
        tok, parts, enc = encoded(ctx, gen_idx)
        tcheck = template_check(tok, parts, [ctx.rows[i] for i in gen_idx])
    max_prompt = max(len(e[0]) for e in enc)
    max_len = max_prompt + K.GEN_MAX_NEW_TOKENS
    chunks = [list(range(s, min(s + GEN_CHUNK, len(gen_idx))))
              for s in range(0, len(gen_idx), GEN_CHUNK)]
    shas = [ids_sha(e[0]) for e in enc]
    stale = check_fingerprint(ctx.gen_parts, gen_fingerprint(max_len), "gen parts")
    todo = [c for c, ch in enumerate(chunks)
            if not gen_done_part(ctx.gen_parts / f"part_{c:04d}.jsonl",
                                 [ctx.rows[gen_idx[j]]["pid"] for j in ch],
                                 [shas[j] for j in ch])]
    log(f"gen[{ctx.tag}]: {len(gen_idx)} prompts, {len(chunks)} chunks, {len(todo)} to do, "
        f"max prompt {max_prompt} tokens, max_model_len {max_len}")
    meta: dict[str, Any] = {"n_prompts": len(gen_idx), "max_model_len": max_len,
                            "template_check": tcheck, "chunks": {}, "stale_reset": stale}
    if todo:
        import vllm
        from vllm import LLM, SamplingParams

        path = str(model_path("dpo"))
        with T("engine_load"):
            llm = LLM(model=path, tokenizer=path, dtype="bfloat16", seed=K.SEED_SAMPLE,
                      max_model_len=max_len, gpu_memory_utilization=GEN_MEM,
                      generation_config="vllm", enable_prefix_caching=True)
        sp = SamplingParams(temperature=K.GEN_TEMPERATURE, max_tokens=K.GEN_MAX_NEW_TOKENS,
                            seed=K.SEED_SAMPLE, stop_token_ids=[IM_END_ID, EOT_ID],
                            skip_special_tokens=True)
        meta["vllm"] = getattr(vllm, "__version__", "?")
        meta["sampling"] = {"temperature": K.GEN_TEMPERATURE, "max_tokens": K.GEN_MAX_NEW_TOKENS,
                            "seed": K.SEED_SAMPLE, "stop_token_ids": [IM_END_ID, EOT_ID]}
        for c in todo:
            ch = chunks[c]
            s0 = time.time()
            outs = llm.generate([{"prompt_token_ids": enc[j][0]} for j in ch], sp, use_tqdm=False)
            dt = time.time() - s0
            rows, n_out = [], 0
            for j, o in zip(ch, outs, strict=True):
                co = o.outputs[0]
                ids = list(co.token_ids)
                n_out += len(ids)
                rows.append({"pid": ctx.rows[gen_idx[j]]["pid"], "response": co.text,
                             "n_resp_tokens": len(ids), "finish_reason": co.finish_reason,
                             "ids": ids[: K.RESP_FEATURE_TOKENS], "prompt_sha": shas[j],
                             "last_id": ids[-1] if ids else None})
            write_jsonl(ctx.gen_parts / f"part_{c:04d}.jsonl", rows)
            meta["chunks"][c] = {"n": len(ch), "seconds": round(dt, 1), "out_tokens": n_out,
                                 "out_tok_s": round(n_out / max(dt, 1e-6), 1)}
            T.phases["generate"] = T.phases.get("generate", 0.0) + dt
            log(f"gen chunk {c}: {len(ch)} prompts, {n_out} tokens, {n_out / max(dt, 1e-6):.0f} "
                f"out tok/s")
    with T("assemble"):
        out, ids_rows, n_tok, fin, stop_last = [], [], 0, {}, 0
        for c in range(len(chunks)):
            for r in read_jsonl(ctx.gen_parts / f"part_{c:04d}.jsonl"):
                out.append({k: r[k] for k in K.RESPONSE_FIELDS})
                ids_rows.append({"pid": r["pid"], "prompt_sha": r["prompt_sha"],
                                 "n_resp_tokens": r["n_resp_tokens"], "ids": r["ids"]})
                n_tok += r["n_resp_tokens"]
                fin[r["finish_reason"]] = fin.get(r["finish_reason"], 0) + 1
                stop_last += r.get("last_id") in (IM_END_ID, EOT_ID)
        write_jsonl(ctx.responses, out)
        write_jsonl(resp_ids_path(ctx), ids_rows)
    gen_s = T.phases.get("generate", 0.0)
    meta.update({"out_tokens": n_tok, "finish_reasons": fin,
                 "mean_resp_tokens": n_tok / max(len(out), 1),
                 "out_tok_s": round(n_tok / gen_s, 1) if gen_s and len(todo) == len(chunks)
                 else None, "responses_sha256": sha256_file(ctx.responses),
                 "response_ids_sha256": sha256_file(resp_ids_path(ctx)),
                 "stop_token_last_in_ids": stop_last})
    record("gen", T, results=ctx.results, **meta)
    return 0


def ids_sha(ids: Sequence[int]) -> str:
    return hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:16]


def resp_ids_path(ctx: Ctx) -> Path:
    """results/gen/response_ids.jsonl (smoke: results/smoke/gen/): per gen prompt the exact
    generated ids (the first K.RESP_FEATURE_TOKENS, stop token included when vLLM returned it),
    the hash of the prompt ids generation used, and n_resp_tokens. In the manifest."""
    return ctx.responses.parent / "response_ids.jsonl"


def code_sha(names: Sequence[str]) -> str:
    h = hashlib.sha256()
    for n in names:
        h.update(n.encode() + b"\0" + (Path(__file__).parent / n).read_bytes())
    return h.hexdigest()[:16]


def rows_sha(rows: Sequence[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps([r["pid"], r["set"], r["text"], r["gen"], r["gen_rank"]],
                            ensure_ascii=False).encode() + b"\n")
    return h.hexdigest()[:16]


def check_prompt_ids(ctx: Ctx, idx: Sequence[int], enc, shas: dict[str, str]) -> list[str]:
    """The prompt ids of this pass (venv-peft tokenizers) against those generation used
    (venv-vllm). Fails above 1% mismatches; returns the mismatched pids (recorded)."""
    bad = [ctx.rows[int(i)]["pid"] for i, (ids, _, _) in zip(idx, enc, strict=True)
           if ctx.rows[int(i)]["pid"] in shas and shas[ctx.rows[int(i)]["pid"]] != ids_sha(ids)]
    if len(bad) > 0.01 * max(len(idx), 1):
        raise StepFailed(f"{len(bad)} prompts tokenise differently from generation")
    if bad:
        log(f"WARNING {len(bad)} prompts tokenise differently from generation")
    return bad


def response_ids(ctx: Ctx) -> tuple[dict[str, list[int]], dict[str, str], str]:
    """(pid -> exact generated ids, pid -> prompt-id hash, file sha) from resp_ids_path. Required:
    there is no re-tokenising fallback (decoded text loses the stop token and may re-segment)."""
    path = resp_ids_path(ctx)
    if not path.is_file():
        raise StepFailed(f"{path} missing (run gen; a new pod needs it pushed)")
    ids, shas = {}, {}
    for r in read_jsonl(path):
        ids[r["pid"]] = r["ids"]
        shas[r["pid"]] = r["prompt_sha"]
    return ids, shas, sha256_file(path)


# --- feature store ------------------------------------------------------------------------------


def _open(path: Path, shape: tuple, dtype, fill=None):
    if path.is_file():
        a = np.load(path, mmap_mode="r+")
        if a.shape != shape or a.dtype != np.dtype(dtype):
            raise StepFailed(f"{path}: {a.shape} {a.dtype}, expected {shape} {dtype}")
        return a
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)
    if fill is not None:
        a[:] = fill
        a.flush()
    return a


def feat_path(work: Path, model: str, w: str, L: int) -> Path:
    return work / f"{model}_{w}_L{L}.npy"


def open_store(work: Path, model: str, n: int, d: int, mode: str = "r+") -> dict:
    st = {"scalars": _open(work / f"{model}_scalars.npy", (n, len(SCALARS)), np.float64,
                           fill=np.nan)}
    for L in K.FEATURE_LAYERS:
        for w in ("p", "r"):
            st[(w, L)] = _open(feat_path(work, model, w, L), (n, d), np.float16)
    return st


def read_store(work: Path, model: str) -> dict:
    st = {"scalars": np.load(work / f"{model}_scalars.npy", mmap_mode="r")}
    for L in K.FEATURE_LAYERS:
        for w in ("p", "r"):
            p = feat_path(work, model, w, L)
            if p.is_file():
                st[(w, L)] = np.load(p, mmap_mode="r")
    return st


def shards(idx: Sequence[int], lengths: Sequence[int]) -> list[list[int]]:
    """Row indices sorted by length (longest first) in SHARD-sized resumable groups."""
    order = sorted(range(len(idx)), key=lambda j: (-lengths[j], j))
    rows = [int(idx[j]) for j in order]
    return [rows[s : s + SHARD] for s in range(0, len(rows), SHARD)]


def progress(path: Path) -> set[str]:
    p = read_json(path)
    return set(p["done"]) if p else set()


def shard_key(sh: Sequence[int], ctx: Ctx) -> str:
    """Identifies a shard's rows; what they contain and how they are computed is covered by
    the store directory's fingerprint."""
    h = hashlib.sha256("|".join(ctx.rows[i]["pid"] for i in sh).encode())
    return h.hexdigest()[:16]


def mark_done(path: Path, key: str) -> None:
    p = read_json(path) or {"done": []}
    p["done"] = sorted(set(p["done"]) | {key})
    p["complete"] = False
    write_json(path, p)


def mark_complete(path: Path) -> None:
    p = read_json(path) or {"done": []}
    p["complete"] = True
    write_json(path, p)


FEATS_CODE = ("contracts.py", "feats.py", "pod.py")
GRADS_CODE = ("contracts.py", "feats.py", "grads.py", "pod.py")


def feats_fingerprint(ctx: Ctx, model: str, resp_sha: str) -> dict:
    """Everything a feature checkpoint depends on. Any change -> the store is recomputed."""
    return {"stage": "feats", "model": list(K.MODELS[model]), "attn": ctx.attn,
            "resp_tokens": ctx.resp_tokens, "layers": list(K.FEATURE_LAYERS),
            "rows_sha": rows_sha(ctx.rows), "response_ids_sha": resp_sha,
            "sae": [K.SAE_REPO, K.SAE_REV, K.SAE_SUBDIR] if model == "dpo" else None,
            "code_sha": code_sha(FEATS_CODE)}


def grads_fingerprint(ctx: Ctx, resp_sha: str) -> dict:
    return {"stage": "grads", "model": list(K.MODELS["dpo"]), "attn": ctx.attn,
            "resp_tokens": ctx.resp_tokens, "grad_ref_n": ctx.grad_ref_n,
            "layers": list(K.GRAD_LAYERS), "proj_dim": K.GRAD_PROJ_DIM, "seed": K.SEED_PROJ,
            "rows_sha": rows_sha(ctx.rows), "response_ids_sha": resp_sha,
            "code_sha": code_sha(GRADS_CODE)}


def store_dir(ctx: Ctx, stage: str) -> Path:
    """One directory per checkpointed stage (dpo, sft, grads), each with its fingerprint.json."""
    return ctx.work / stage


def require_complete(ctx: Ctx, stage: str, fp: dict) -> None:
    """Score: the stage's store must be complete under exactly the expected fingerprint."""
    d = store_dir(ctx, stage)
    got = read_json(d / "fingerprint.json")
    prog = read_json(d / "progress.json") or {}
    if got is None or got.get("fp") != fp:
        raise StepFailed(f"{stage} store fingerprint does not match the current inputs/settings")
    if not prog.get("complete"):
        raise StepFailed(f"{stage} store is incomplete (rerun its step)")


def model_items(ctx: Ctx, model: str, tok=None):
    """(row indices, Items) for one model's feature pass: DPO all rows, SFT all but sft_ref;
    gen rows get their first ctx.resp_tokens response ids."""
    from p42.feats import Item

    idx = ctx.idx(lambda r: model == "dpo" or r["set"] != K.SFT_REF)
    tok, parts, enc = encoded(ctx, idx, tok)
    rids, shas, resp_sha = response_ids(ctx)
    n_bad = check_prompt_ids(ctx, idx, enc, shas)
    items = []
    missing = 0
    for i, (ids, c0, c1) in zip(idx, enc, strict=True):
        r = ctx.rows[i]
        resp: list[int] = []
        if r["gen"]:
            if r["pid"] not in rids:
                missing += 1
            else:
                resp = rids[r["pid"]][: ctx.resp_tokens]
        items.append(Item(ids, c0, c1, resp))
    if missing:
        raise StepFailed(f"{missing} gen=True prompts have no response (run gen first)")
    info = {"prompt_id_mismatch": len(n_bad), "prompt_id_mismatch_pids": n_bad[:200],
            "response_ids_sha": resp_sha}
    return idx, items, info, tok, parts


def run_feats_model(ctx: Ctx, model_name: str, T: Timer, *, tok=None, model=None) -> dict:
    """One model's feature pass into store_dir(ctx, model_name); resumable by shard, and redone
    from scratch when the fingerprint (inputs, response ids, model, attention, response tokens,
    code) differs from the checkpoint's. `model`: an already loaded model (the smoke's)."""
    from p42.feats import SAE, forward_batch, hs_index_for_hook, pack_batches

    with T(f"{model_name}_prepare"):
        idx, items, info, tok, _ = model_items(ctx, model_name, tok)
        sdir_ = store_dir(ctx, model_name)
        stale = check_fingerprint(sdir_, feats_fingerprint(ctx, model_name,
                                                           info["response_ids_sha"]),
                                  f"{model_name} features")
    lengths = [len(it.ids) for it in items]
    pos = {int(i): j for j, i in enumerate(idx)}
    prog_path = sdir_ / "progress.json"
    done = progress(prog_path)
    shs = shards(idx, lengths)
    todo = [sh for sh in shs if shard_key(sh, ctx) not in done]
    rec: dict[str, Any] = {"n_rows": len(idx), "n_shards": len(shs), "n_todo": len(todo),
                           "stale_reset": stale, **info}
    if not todo:
        mark_complete(prog_path)
        return rec
    if model is None:
        with T(f"{model_name}_load"):
            model = load_model(model_name, ctx.attn)
    d = model.config.hidden_size
    st = open_store(sdir_, model_name, ctx.n, d)
    sae, sae_idx = None, None
    if model_name == "dpo":
        with T("sae_load"):
            try:
                sdir = sae_dir() if LOADER is None else os.environ.get("P42_TEST_SAE_DIR")
                if sdir:
                    sae, sinfo = SAE.load(sdir, next(model.parameters()).device, "layer_16")
                    sae_idx = hs_index_for_hook(sinfo["hook_name"])
                    if sinfo["d_in"] != d:
                        raise ValueError(f"SAE d_in {sinfo['d_in']} != d_model {d}")
                    rec["sae"] = sinfo | {"hs_index": sae_idx}
                else:
                    rec["sae"] = {"skipped": "SAE files not found"}
            except Exception as e:  # noqa: BLE001  (exploratory: skip with a logged reason)
                sae = None
                rec["sae"] = {"skipped": repr(e)[:500]}
                log(f"S3f skipped: {e!r}")
    tokens = 0
    with T(f"{model_name}_forward"):
        for sh in todo:
            sub = [pos[i] for i in sh]
            for b in pack_batches([lengths[j] for j in sub], FEATS_TOKENS, MAX_BATCH):
                bj = [sub[k] for k in b]
                res = forward_batch(model, [items[j] for j in bj], K.FEATURE_LAYERS,
                                    pad_id(tok), sae=sae, sae_hs_index=sae_idx)
                for j, r in zip(bj, res, strict=True):
                    row = int(idx[j])
                    st["scalars"][row] = [r[c] for c in SCALARS]
                    for L in K.FEATURE_LAYERS:
                        st[("p", L)][row] = r["p"][L].numpy().astype(np.float16)
                        st[("r", L)][row] = r["r"][L].numpy().astype(np.float16)
                tokens += sum(lengths[j] for j in bj)
            for a in st.values():
                a.flush()
            mark_done(prog_path, shard_key(sh, ctx))
    mark_complete(prog_path)
    fwd = T.phases.get(f"{model_name}_forward", 0.0)
    rec.update({"tokens": tokens, "tok_s": round(tokens / fwd, 1) if fwd else None,
                "peak_mem_gb": peak_mem()})
    del model, sae
    free()
    return rec


def pad_id(tok) -> int:
    return tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id


def peak_mem() -> float | None:
    try:
        import torch

        if torch.cuda.is_available():
            return round(torch.cuda.max_memory_allocated() / 1e9, 2)
    except ImportError:
        pass
    return None


def sdpa_vs_eager(ctx: Ctx, model, items, idx) -> dict:
    """Prompt NLL (mean) of ATTN_CHECK_N id_ref prompts under sdpa and eager; the model is left
    on ctx.attn."""
    import torch

    from p42.feats import Item, forward_batch

    cand = [j for j, i in enumerate(idx) if ctx.rows[int(i)]["set"] == K.ID_REF][:ATTN_CHECK_N]
    its = [Item(items[j].prompt_ids, items[j].c0, items[j].c1) for j in cand]
    tok_pad = 0
    out = {}
    for attn in ("sdpa", "eager"):
        set_attn(model, attn)
        s0 = time.time()
        nll = []
        for k in range(0, len(its), 4):
            nll += [r["nll_mean"] for r in forward_batch(model, its[k : k + 4], (), tok_pad)]
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        out[attn] = {"nll_mean": nll, "seconds": time.time() - s0}
    set_attn(model, ctx.attn)
    diffs = [abs(a - b) for a, b in zip(out["sdpa"]["nll_mean"], out["eager"]["nll_mean"],
                                        strict=True)]
    finite = bool(diffs) and all(math.isfinite(x) for x in diffs)
    return {"n": len(its), "max_abs_diff": max(diffs) if finite else None, "finite": finite,
            "eager_over_sdpa_time": out["eager"]["seconds"] / max(out["sdpa"]["seconds"], 1e-6)}


def step_feats(args, ctx: Ctx | None = None) -> int:
    enable_tf32()
    ctx = ctx or with_settings(bulk_ctx())
    T = Timer()
    recs = {m: run_feats_model(ctx, m, T) for m in ("dpo", "sft")}
    chk = store_checks(ctx)
    if chk["nonfinite"]:
        raise StepFailed(f"non-finite feature values: {chk['nonfinite']}")
    record("feats", T, results=ctx.results, settings=asdict_settings(ctx), models=recs,
           checks=chk)
    return 0


def asdict_settings(ctx: Ctx) -> dict:
    return {"attn": ctx.attn, "resp_tokens": ctx.resp_tokens, "grad_ref_n": ctx.grad_ref_n}


def store_checks(ctx: Ctx, sample: int = 4096) -> dict:
    """Filled-row counts and a non-finite scan (a seeded sample of filled rows per array)."""
    out: dict[str, Any] = {"nonfinite": {}}
    rng = np.random.default_rng(K.SEED_SAMPLE)
    for m in ("dpo", "sft"):
        if not (store_dir(ctx, m) / f"{m}_scalars.npy").is_file():
            continue
        st = read_store(store_dir(ctx, m), m)
        sc = np.asarray(st["scalars"])
        filled = np.flatnonzero(np.isfinite(sc[:, SC["n_content"]]))
        out[f"{m}_filled"] = len(filled)
        has_c = filled[sc[filled, SC["n_content"]] > 0]
        has_r = filled[sc[filled, SC["n_resp"]] > 0]
        for c in ("nll_sum", "nll_mean"):
            if not np.isfinite(sc[has_c, SC[c]]).all():
                out["nonfinite"][f"{m}.{c}"] = int((~np.isfinite(sc[has_c, SC[c]])).sum())
        for c in ("resp_lp_sum", "resp_lp_mean"):
            if not np.isfinite(sc[has_r, SC[c]]).all():
                out["nonfinite"][f"{m}.{c}"] = int((~np.isfinite(sc[has_r, SC[c]])).sum())
        for (w, L), a in ((k, v) for k, v in st.items() if k != "scalars"):
            rows = has_c if w == "p" else has_r
            if len(rows) > sample:
                rows = np.sort(rng.choice(rows, sample, replace=False))
            if len(rows) and not np.isfinite(np.asarray(a[rows], dtype=np.float32)).all():
                out["nonfinite"][f"{m}.{w}_L{L}"] = int(
                    (~np.isfinite(np.asarray(a[rows], dtype=np.float32))).any(1).sum())
    return out


# --- grads --------------------------------------------------------------------------------------


GRAD_DIM = len(K.GRAD_LAYERS) * K.GRAD_PROJ_DIM ** 2


def grad_rows(ctx: Ctx) -> np.ndarray:
    return ctx.idx(lambda r: r["gen"] and (r["set"] != K.ID_REF
                                           or (r["gen_rank"] is not None
                                               and r["gen_rank"] < ctx.grad_ref_n)))


def run_grads(ctx: Ctx, T: Timer, tok=None) -> dict:
    from p42.feats import Item, pack_batches
    from p42.grads import l2_normalise, per_example_grads, projections

    with T("grads_prepare"):
        gidx = grad_rows(ctx)
        tok, _parts, enc = encoded(ctx, gidx, tok)
        rids, shas, resp_sha = response_ids(ctx)
        n_bad = check_prompt_ids(ctx, gidx, enc, shas)
        gdir = store_dir(ctx, "grads")
        stale = check_fingerprint(gdir, grads_fingerprint(ctx, resp_sha), "gradients")
        items = [Item(ids, c0, c1, rids[ctx.rows[int(i)]["pid"]][: ctx.resp_tokens])
                 for i, (ids, c0, c1) in zip(gidx, enc, strict=True)]
    keep = [j for j, it in enumerate(items) if len(it.resp_ids) > 0]
    lengths = [len(it.ids) for it in items]
    G = _open(gdir / "grads.npy", (ctx.n, GRAD_DIM), np.float16)
    meta = _open(gdir / "grads_meta.npy", (ctx.n, 2), np.float64, fill=np.nan)
    prog_path = gdir / "progress.json"
    done = progress(prog_path)
    shs = shards(gidx[keep], [lengths[j] for j in keep])
    todo = [sh for sh in shs if shard_key(sh, ctx) not in done]
    rec: dict[str, Any] = {"n_rows": len(gidx), "n_no_response": len(items) - len(keep),
                           "n_shards": len(shs), "n_todo": len(todo), "stale_reset": stale,
                           "prompt_id_mismatch": len(n_bad), "response_ids_sha": resp_sha}
    if not todo:
        mark_complete(prog_path)
        return rec
    with T("grads_load"):
        model = load_model("dpo", ctx.attn)
    cfg = model.config
    dev = next(model.parameters()).device
    projs = projections(K.GRAD_LAYERS, cfg.hidden_size, cfg.intermediate_size, K.GRAD_PROJ_DIM,
                        K.SEED_PROJ, device=dev)
    pos = {int(i): j for j, i in enumerate(gidx)}
    tokens = 0
    with T("grads_backward"):
        for sh in todo:
            sub = [pos[i] for i in sh]
            for b in pack_batches([lengths[j] for j in sub], GRADS_TOKENS, MAX_BATCH):
                bj = [sub[k] for k in b]
                g, loss = per_example_grads(model, [items[j] for j in bj], K.GRAD_LAYERS, projs,
                                            pad_id(tok), cut=min(K.GRAD_LAYERS))
                norms = g.norm(dim=1)
                gn = l2_normalise(g).cpu().numpy().astype(np.float16)
                for k, j in enumerate(bj):
                    row = int(gidx[j])
                    G[row] = gn[k]
                    meta[row] = [float(loss[k]), float(norms[k])]
                tokens += sum(lengths[j] for j in bj)
            G.flush()
            meta.flush()
            mark_done(prog_path, shard_key(sh, ctx))
    mark_complete(prog_path)
    sec = T.phases.get("grads_backward", 0.0)
    rec.update({"tokens": tokens, "tok_s": round(tokens / sec, 1) if sec else None,
                "peak_mem_gb": peak_mem()})
    del model, projs
    free()
    return rec


def grad_checks(ctx: Ctx) -> dict:
    gdir = store_dir(ctx, "grads")
    meta = np.load(gdir / "grads_meta.npy", mmap_mode="r")
    filled = np.flatnonzero(np.isfinite(meta[:, 0]))
    G = np.load(gdir / "grads.npy", mmap_mode="r")
    norms = np.asarray(meta[filled, 1])
    sample = filled[:: max(1, len(filled) // 4096)]
    gs = np.asarray(G[sample], dtype=np.float32)
    return {"filled": len(filled), "zero_norm": int((norms <= 0).sum()),
            "nonfinite_norm": int((~np.isfinite(norms)).sum()),
            "nonfinite_values": int((~np.isfinite(gs)).any(1).sum()),
            "unit_norm_maxdev": float(np.abs(np.linalg.norm(gs, axis=1) - 1).max())
            if len(gs) else None}


def step_grads(args, ctx: Ctx | None = None) -> int:
    enable_tf32()
    ctx = ctx or with_settings(bulk_ctx())
    T = Timer()
    rec = run_grads(ctx, T)
    chk = grad_checks(ctx)
    if chk["zero_norm"] or chk["nonfinite_norm"] or chk["nonfinite_values"]:
        raise StepFailed(f"gradient checks failed: {chk}")
    record("grads", T, results=ctx.results, settings=asdict_settings(ctx), **rec, checks=chk)
    return 0


# --- score --------------------------------------------------------------------------------------


def score_all(ctx: Ctx, T: Timer) -> dict:
    """Every K.score_columns() column for the SCORED_SETS rows -> scores.csv; PCA files."""
    from p42 import scoring as S

    dev = device()
    rows = ctx.rows
    scored = ctx.idx(lambda r: r["set"] in K.SCORED_SETS)
    id_ref = ctx.idx(lambda r: r["set"] == K.ID_REF)
    gen_ref = ctx.idx(lambda r: r["set"] == K.ID_REF and r["gen"])
    sft_ref = ctx.idx(lambda r: r["set"] == K.SFT_REF)
    _, _, resp_sha = response_ids(ctx)
    for m in ("dpo", "sft"):
        require_complete(ctx, m, feats_fingerprint(ctx, m, resp_sha))
    gdir = store_dir(ctx, "grads")
    have_grads = (gdir / "fingerprint.json").is_file()
    if have_grads:
        require_complete(ctx, "grads", grads_fingerprint(ctx, resp_sha))
    dpo, sft = read_store(store_dir(ctx, "dpo"), "dpo"), read_store(store_dir(ctx, "sft"), "sft")
    dsc, ssc = np.asarray(dpo["scalars"]), np.asarray(sft["scalars"])
    nan = np.full(len(scored), np.nan)
    cols: dict[str, np.ndarray] = {}
    info: dict[str, Any] = {"n_scored": len(scored), "refs": {}}
    with T("scalars"):
        cols["len_tokens"] = np.array([rows[i]["n_tokens"] for i in scored], dtype=np.float64)
        cols["nll_dpo_mean"] = dsc[scored, SC["nll_mean"]]
        cols["nll_dpo_sum"] = dsc[scored, SC["nll_sum"]]
        cols["nll_sft_mean"] = ssc[scored, SC["nll_mean"]]
        cols["nll_sft_sum"] = ssc[scored, SC["nll_sum"]]
        cols["lr_sum"] = cols["nll_dpo_sum"] - cols["nll_sft_sum"]
        cols["lr_mean"] = cols["nll_dpo_mean"] - cols["nll_sft_mean"]
        cols["lr_resp_sum"] = ssc[scored, SC["resp_lp_sum"]] - dsc[scored, SC["resp_lp_sum"]]
        cols["lr_resp_mean"] = ssc[scored, SC["resp_lp_mean"]] - dsc[scored, SC["resp_lp_mean"]]
        cols["resp_nll_dpo_mean"] = -dsc[scored, SC["resp_lp_mean"]]
        cols["fvu_sae_L16"] = dsc[scored, SC["fvu"]]
    filled_d = np.isfinite(dsc[:, SC["n_content"]]) & (dsc[:, SC["n_content"]] > 0)
    filled_s = np.isfinite(ssc[:, SC["n_content"]]) & (ssc[:, SC["n_content"]] > 0)
    resp_d = np.isfinite(dsc[:, SC["n_resp"]]) & (dsc[:, SC["n_resp"]] > 0)

    def q(a, rows_):
        return np.asarray(a[rows_], dtype=np.float32)

    for L in K.FEATURE_LAYERS:
        with T(f"L{L}"):
            P = dpo[("p", L)]
            ref = id_ref[filled_d[id_ref]]
            Qp = q(P, scored)
            Rp = q(P, ref)
            cols[f"maha_dpo_p_L{L}"] = S.Mahalanobis(Rp, dev).score(Qp)
            cols[f"knn_dpo_p_L{L}"] = S.knn_score(Qp, Rp, K.KNN_K, dev)
            sref = sft_ref[filled_d[sft_ref]]
            cols[f"knn_sftref_p_L{L}"] = (S.knn_score(Qp, q(P, sref), K.KNN_K, dev)
                                          if len(sref) else nan)
            R = dpo[("r", L)]
            gref = gen_ref[resp_d[gen_ref]]
            if len(gref):
                Qr = q(R, scored)
                Rr = q(R, gref)
                m = Mask(resp_d[scored])
                cols[f"maha_dpo_r_L{L}"] = m.fill(S.Mahalanobis(Rr, dev).score(Qr[m.on]))
                cols[f"knn_dpo_r_L{L}"] = m.fill(S.knn_score(Qr[m.on], Rr, K.KNN_K, dev))
            else:
                cols[f"maha_dpo_r_L{L}"] = cols[f"knn_dpo_r_L{L}"] = nan
            Ps = sft[("p", L)]
            sref_i = id_ref[filled_s[id_ref]]
            cols[f"maha_sft_p_L{L}"] = S.Mahalanobis(q(Ps, sref_i), dev).score(q(Ps, scored))
            cols[f"dknn_p_L{L}"] = S.zscore(cols[f"knn_dpo_p_L{L}"]) - S.zscore(
                cols[f"knn_sftref_p_L{L}"])
            cols[f"dmaha_p_L{L}"] = S.zscore(cols[f"maha_dpo_p_L{L}"]) - S.zscore(
                cols[f"maha_sft_p_L{L}"])
            info["refs"][L] = {"id_ref": len(ref), "sft_ref": len(sref), "gen_ref": len(gref),
                               "sft_model_id_ref": len(sref_i)}
    with T("grads"):
        if have_grads:
            G = np.load(gdir / "grads.npy", mmap_mode="r")
            gmeta = np.load(gdir / "grads_meta.npy", mmap_mode="r")
            gfill = np.isfinite(gmeta[:, 0])
            gref = np.array([i for i in gen_ref if gfill[i]
                             and rows[i]["gen_rank"] < ctx.grad_ref_n], dtype=np.int64)
            m = Mask(gfill[scored])
            cols["grad_knn"] = m.fill(S.knn_score(q(G, scored[m.on]), q(G, gref), K.KNN_K, dev,
                                                  centre=False)) if len(gref) else nan
            info["refs"]["grads"] = len(gref)
        else:
            cols["grad_knn"] = nan
            info["refs"]["grads"] = "absent (no gradient store)"
    with T("write"):
        ids = {"pid": [rows[i]["pid"] for i in scored], "set": [rows[i]["set"] for i in scored],
               "split": [rows[i]["split"] for i in scored]}
        S.write_scores_csv(ctx.results / "scores.csv", K.SCORE_ID_COLS, K.score_columns(), ids,
                           cols)
    info["nan_counts"] = {c: int((~np.isfinite(np.asarray(cols.get(c, nan), dtype=np.float64)))
                                 .sum()) for c in K.score_columns()}
    with T("pca"):
        info["pca"] = write_pca(ctx, dpo, id_ref[filled_d[id_ref]], gen_ref[resp_d[gen_ref]],
                                filled_d, resp_d)
    return info


class Mask:
    """Score only the rows where `on` is True; `fill` puts them back among NaNs."""

    def __init__(self, on: np.ndarray):
        self.on = np.asarray(on, dtype=bool)

    def fill(self, vals: np.ndarray) -> np.ndarray:
        out = np.full(len(self.on), np.nan)
        out[self.on] = vals
        return out


def write_pca(ctx: Ctx, dpo: dict, p_ref, r_ref, filled_d, resp_d) -> dict:
    from p42 import scoring as S

    out_dir = ctx.results / "pca"
    out_dir.mkdir(parents=True, exist_ok=True)
    dev = device()
    p_rows = np.flatnonzero(filled_d)
    r_rows = np.flatnonzero(resp_d)
    info = {}
    for name, key, ref, rows_ in (("dpo_p_L16", ("p", 16), p_ref, p_rows),
                                  ("dpo_p_L24", ("p", 24), p_ref, p_rows),
                                  ("dpo_r_L16", ("r", 16), r_ref, r_rows)):
        X = dpo[key]
        if len(ref) < 2 or not len(rows_):
            info[name] = {"skipped": "no reference rows"}
            continue
        dim = min(K.PCA_DIM, len(ref) - 1, X.shape[1])
        mean, comps = S.pca_fit(np.asarray(X[ref], dtype=np.float32), dim, dev)
        Z = S.pca_project(np.asarray(X[rows_], dtype=np.float32), mean, comps, dev)
        tmp = out_dir / f".{name}.npy.tmp"
        with open(tmp, "wb") as fh:
            np.save(fh, Z.astype(np.float16))
        os.replace(tmp, out_dir / f"{name}.npy")
        info[name] = {"rows": len(rows_), "dim": int(dim), "fit_rows": len(ref)}
    pid_files = {"pids.txt": p_rows, "pids_r.txt": r_rows}
    for fname, rows_ in pid_files.items():
        tmp = out_dir / f".{fname}.tmp"
        tmp.write_text("".join(ctx.rows[i]["pid"] + "\n" for i in rows_))
        os.replace(tmp, out_dir / fname)
    total = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file())
    info["bytes"] = total
    if total > 250e6:
        raise StepFailed(f"results/pca is {total / 1e6:.0f} MB (> 250 MB)")
    return info


def step_score(args, ctx: Ctx | None = None) -> int:
    enable_tf32()
    ctx = ctx or with_settings(bulk_ctx())
    T = Timer()
    info = score_all(ctx, T)
    record("score", T, results=ctx.results, settings=asdict_settings(ctx), **info)
    return 0


# --- smoke --------------------------------------------------------------------------------------


def deadline_epoch() -> tuple[float | None, str]:
    if os.environ.get("P42_DEADLINE_EPOCH"):
        return float(os.environ["P42_DEADLINE_EPOCH"]), "env P42_DEADLINE_EPOCH"
    if KILLSWITCH_DEADLINE.is_file():
        try:
            return float(KILLSWITCH_DEADLINE.read_text().split()[0]) - SALVAGE_S, \
                f"killswitch deadline - {SALVAGE_S} s"
        except (ValueError, IndexError):
            pass
    t0 = os.environ.get("P42_T0")
    if t0:
        if os.environ.get("P42_BUDGET_S"):
            return float(t0) + float(os.environ["P42_BUDGET_S"]), "P42_T0 + P42_BUDGET_S"
        gpu_h = float(tomllib.loads((K.PROBE_DIR / "probe.toml").read_text())["pod"]["gpu_hours"])
        return float(t0) + gpu_h * 3600 - 900, "P42_T0 + gpu_hours - 15 min"
    return None, "none"


@dataclass
class Rates:
    """Measured in the smoke: seconds per token (no model loads) and fixed load seconds."""
    gen_s_per_out_tok: float
    gen_load_s: float
    mean_resp_tokens: float  # generated tokens per gen prompt
    mean_resp_used: dict  # resp_tokens -> mean min(n_resp, resp_tokens)
    feats_s_per_tok: dict  # model -> s/token
    feats_load_s: dict
    grads_s_per_tok: float
    grads_load_s: float
    attn_factor: float = 1.0  # eager slowdown when the run switches to eager
    score_s: float = SCORE_ALLOW_S


def full_counts(rows: Sequence[dict], template_tokens: int) -> dict:
    """Prompt-token totals for the full run from n_tokens (capped) + the template."""
    def plen(r):
        return min(int(r["n_tokens"]), K.MAX_PROMPT_TOKENS) + template_tokens
    gen = [r for r in rows if r["gen"]]
    return {"n_gen": len(gen),
            "prompt_tokens_dpo": sum(plen(r) for r in rows),
            "prompt_tokens_sft": sum(plen(r) for r in rows if r["set"] != K.SFT_REF),
            "n_gen_sft": sum(1 for r in gen if r["set"] != K.SFT_REF),
            "gen_prompts": [(r["set"], r["gen_rank"], plen(r)) for r in gen]}


def project(rates: Rates, counts: dict, resp_tokens: int, grad_ref_n: int) -> dict:
    """Projected seconds per remaining phase of the bulk run."""
    used = rates.mean_resp_used[resp_tokens]
    g_rows = [p for s, rank, p in counts["gen_prompts"]
              if s != K.ID_REF or (rank is not None and rank < grad_ref_n)]
    ph = {
        "gen": rates.gen_load_s + counts["n_gen"] * rates.mean_resp_tokens
        * rates.gen_s_per_out_tok,
        "feats_dpo": rates.feats_load_s["dpo"] + rates.attn_factor * rates.feats_s_per_tok["dpo"]
        * (counts["prompt_tokens_dpo"] + counts["n_gen"] * used),
        "feats_sft": rates.feats_load_s["sft"] + rates.attn_factor * rates.feats_s_per_tok["sft"]
        * (counts["prompt_tokens_sft"] + counts["n_gen_sft"] * used),
        "grads": rates.grads_load_s + rates.attn_factor * rates.grads_s_per_tok
        * (sum(g_rows) + len(g_rows) * used),
        "score": rates.score_s,
        "manifest": MANIFEST_S,
        "process_overhead": 4 * PROC_OVERHEAD_S,
    }
    ph["total"] = sum(ph.values())
    return ph


def choose_settings(rates: Rates, counts: dict, time_left: float | None) -> dict:
    """The plan's cut order: S4 reference 20k -> 10k, then response feature tokens 128 -> 64."""
    steps = [(K.RESP_FEATURE_TOKENS, K.N_GEN_REF, "none"),
             (K.RESP_FEATURE_TOKENS, K.N_GEN_REF // 2, "S4 reference 20k -> 10k"),
             (K.RESP_FEATURE_TOKENS // 2, K.N_GEN_REF // 2,
              "S4 reference 20k -> 10k and response feature tokens 128 -> 64")]
    tried = []
    for rt, gref, cut in steps:
        ph = project(rates, counts, rt, gref)
        ok = time_left is None or ph["total"] <= PROJ_FRACTION * time_left
        tried.append({"cut": cut, "resp_tokens": rt, "grad_ref_n": gref, "projection_s": ph,
                      "ok": ok})
        if ok:
            return {"ok": True, "resp_tokens": rt, "grad_ref_n": gref, "cut": cut,
                    "tried": tried}
    return {"ok": False, "tried": tried}


def smoke_gates(ctx: Ctx, feats_rec: dict, grads_chk: dict, score_info: dict) -> dict:
    dsc = np.load(store_dir(ctx, "dpo") / "dpo_scalars.npy")
    ssc = np.load(store_dir(ctx, "sft") / "sft_scalars.npy")
    id_ref = ctx.idx(lambda r: r["set"] == K.ID_REF)
    nll_id = float(np.nanmean(dsc[id_ref, SC["nll_mean"]]))
    both = np.isfinite(dsc[:, SC["nll_mean"]]) & np.isfinite(ssc[:, SC["nll_mean"]])
    diff = float(np.mean(np.abs(dsc[both, SC["nll_mean"]] - ssc[both, SC["nll_mean"]])))
    ac = feats_rec["attn_check"]
    nan_cols = {c: n for c, n in score_info["nan_counts"].items() if n and c != "fvu_sae_L16"}
    fvu_nan = score_info["nan_counts"].get("fvu_sae_L16", 0)
    sae_on = "skipped" not in feats_rec["models"]["dpo"].get("sae", {"skipped": 1})
    gates = {
        "no_nonfinite_features": not feats_rec["checks"]["nonfinite"],
        "no_nan_scores": not nan_cols,
        "fvu_ok_or_skipped": (not sae_on) or fvu_nan <= 0.01 * score_info["n_scored"],
        "nll_id_ref_in_range": NLL_RANGE[0] <= nll_id <= NLL_RANGE[1],
        "sft_dpo_differ": diff > 1e-3,
        "attn_diff_finite": bool(ac.get("finite")),
        "grad_loss_agrees": feats_rec["grad_loss_vs_feats_max_abs"] is not None
        and feats_rec["grad_loss_vs_feats_max_abs"] <= GRAD_LOSS_TOL,
        "grads_nonzero": grads_chk["filled"] > 0 and not grads_chk["zero_norm"]
        and not grads_chk["nonfinite_values"] and not grads_chk["nonfinite_norm"],
    }
    return {"gates": gates, "pass": all(gates.values()), "nll_id_ref_mean": nll_id,
            "mean_abs_nll_diff_sft_dpo": diff, "nan_score_columns": nan_cols,
            "attn_check": ac, "sae_on": sae_on}


def step_smoke(args) -> int:
    enable_tf32()
    T = Timer()
    all_rows = load_prompts()
    ctx = smoke_ctx(all_rows)
    for d in (ctx.work, ctx.results):  # a rerun starts clean (its rates must be measured)
        if d.exists():
            shutil.rmtree(d)
    log(f"smoke: {ctx.n} prompts ({sum(r['gen'] for r in ctx.rows)} gen)")
    with T("gen"):
        if getattr(args, "gen_inline", False):
            step_gen(argparse.Namespace(smoke=True))
        else:
            env = dict(os.environ)
            r = SUBPROC([VLLM_PY, "-m", "p42.pod", "gen", "--smoke"], env=env)
            if r.returncode != 0:
                raise StepFailed(f"smoke gen failed rc={r.returncode}")
    gen_rec = read_json(ctx.results / "run_gen.json")
    Tf = Timer()
    # 1. sdpa vs eager first; the rest of the smoke runs with the chosen implementation
    with Tf("dpo_load"):
        model = load_model("dpo", "sdpa")
    with Tf("attn_check"):
        idx, items, _, _, _ = model_items(ctx, "dpo")
        ac = sdpa_vs_eager(replace(ctx, attn="sdpa"), model, items, idx)
    attn = "sdpa"
    if not ac["finite"] or ac["max_abs_diff"] > ATTN_TOL:
        attn = "eager"
        log(f"sdpa vs eager: {ac}: the run uses eager")
        set_attn(model, "eager")
    ctx = replace(ctx, attn=attn)
    feats_models = {"dpo": run_feats_model(ctx, "dpo", Tf, model=model)}
    del model, items
    free()
    feats_models["sft"] = run_feats_model(ctx, "sft", Tf)
    feats_rec = {"models": feats_models, "checks": store_checks(ctx), "phases_s": Tf.phases,
                 "attn_check": ac}
    Tg = Timer()
    grads_rec = run_grads(ctx, Tg)
    grads_chk = grad_checks(ctx)
    Ts = Timer()
    score_info = score_all(ctx, Ts)
    for name, t in (("feats", Tf), ("grads", Tg), ("score", Ts)):
        T.phases[name] = t.total()
    # gradient loss vs the feature pass's response NLL (same tokens; bf16 batch noise only)
    dsc = np.load(store_dir(ctx, "dpo") / "dpo_scalars.npy")
    gm = np.load(store_dir(ctx, "grads") / "grads_meta.npy")
    ok = np.isfinite(gm[:, 0]) & np.isfinite(dsc[:, SC["resp_lp_mean"]])
    loss_vs_feats = float(np.max(np.abs(gm[ok, 0] + dsc[ok, SC["resp_lp_mean"]]))) \
        if ok.any() else None
    feats_rec["grad_loss_vs_feats_max_abs"] = loss_vs_feats
    res = smoke_gates(ctx, feats_rec, grads_chk, score_info)
    # projection
    gen_s = gen_rec["phases_s"].get("generate", 0.0)
    resp = read_jsonl(ctx.responses)
    n_resp = [r["n_resp_tokens"] for r in resp]
    tok = load_tok()
    from p42.feats import template_parts

    parts = template_parts(tok, render_fn(tok))
    tmpl = len(parts.prefix_ids) + len(parts.suffix_ids)
    rates = Rates(
        gen_s_per_out_tok=gen_s / max(sum(n_resp), 1),
        gen_load_s=gen_rec["phases_s"].get("engine_load", 0.0),
        mean_resp_tokens=float(np.mean(n_resp)) if n_resp else 0.0,
        mean_resp_used={rt: float(np.mean([min(n, rt) for n in n_resp])) if n_resp else 0.0
                        for rt in (K.RESP_FEATURE_TOKENS, K.RESP_FEATURE_TOKENS // 2)},
        feats_s_per_tok={m: Tf.phases.get(f"{m}_forward", 0.0) / max(feats_models[m].get(
            "tokens", 0), 1) for m in ("dpo", "sft")},
        feats_load_s={m: Tf.phases.get(f"{m}_load", 0.0) for m in ("dpo", "sft")},
        grads_s_per_tok=Tg.phases.get("grads_backward", 0.0) / max(grads_rec.get("tokens", 0), 1),
        grads_load_s=Tg.phases.get("grads_load", 0.0),
        attn_factor=1.0,  # the smoke already ran with the chosen implementation
        score_s=max(SCORE_ALLOW_S, 3 * Ts.total()),
    )
    deadline, source = deadline_epoch()
    now = time.time()
    time_left = None if deadline is None else deadline - now
    counts = full_counts(all_rows, tmpl)
    choice = choose_settings(rates, counts, time_left)
    out = {
        "pass": bool(res["pass"] and choice["ok"]),
        "gates_pass": res["pass"],
        "projection_ok": choice["ok"],
        "settings": {"attn": attn, "resp_tokens": choice.get("resp_tokens"),
                     "grad_ref_n": choice.get("grad_ref_n")},
        "cut": choice.get("cut"),
        "projection_tried": choice["tried"],
        "deadline_epoch": deadline, "deadline_source": source,
        "time_left_s": time_left, "budget_fraction": PROJ_FRACTION,
        "rates": rates.__dict__, "full_counts": {k: v for k, v in counts.items()
                                                 if k != "gen_prompts"},
        "template_tokens": tmpl, "n_smoke": ctx.n,
        "gen": {k: gen_rec.get(k) for k in ("out_tok_s", "mean_resp_tokens", "finish_reasons",
                                             "template_check", "max_model_len", "vllm")},
        "feats": feats_rec, "grads": {**grads_rec, "checks": grads_chk},
        "grad_loss_vs_feats_max_abs": loss_vs_feats,
        "score": {k: v for k, v in score_info.items() if k != "pca"} | {
            "pca": score_info.get("pca")},
        **{k: v for k, v in res.items() if k not in ("pass",)},
        "phases_s": T.phases, "seconds": T.total(), "finished_utc": utc(),
        "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown"),
    }
    if not res["pass"]:
        out["settings"] = {"attn": attn, "resp_tokens": None, "grad_ref_n": None}
    write_json(SMOKE_JSON, out)
    timing = read_json(TIMING_JSON) or {}
    timing["smoke"] = {"seconds": round(T.total(), 1),
                       "phases_s": {k: round(v, 2) for k, v in T.phases.items()}}
    write_json(TIMING_JSON, timing)
    log(f"smoke gates {res['gates']} projection {choice['tried'][-1]['projection_s']['total']:.0f}"
        f" s vs time left {time_left}")
    if not res["pass"]:
        log(f"SMOKE FAIL gates: {res['gates']}")
        return 1
    if not choice["ok"]:
        log("SMOKE FAIL projection over budget after both cuts")
        return 3
    return 0


# --- main ---------------------------------------------------------------------------------------


STEPS = {"download": step_download, "smoke": step_smoke, "gen": step_gen, "feats": step_feats,
         "grads": step_grads, "score": step_score}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p42.pod")
    ap.add_argument("step", choices=sorted(STEPS))
    ap.add_argument("--smoke", action="store_true", help="gen: the smoke subset")
    args = ap.parse_args(argv)
    log(f"step {args.step} start (git {os.environ.get('EMX_GIT_COMMIT', 'unknown')})")
    try:
        rc = STEPS[args.step](args)
    except StepFailed as e:
        log(f"STEP FAILED {args.step}: {e}")
        return 1
    log(f"step {args.step} done rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
