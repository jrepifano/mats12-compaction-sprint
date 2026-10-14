"""P45 on-pod steps (plan.md "Setup: T2", "Measure: T2", "Automatic rules", "Cap"), run by run.sh
through the generic probe runner (scripts/probe_pod.py). From the probe directory, with
PYTHONPATH="$PWD:$PWD/../P42-ood-coverage-gaps:$PWD/../../src":

    python -m p45.pod download   # both models at P42's pins (asserted = emx.e1.registry), the
                                 #   public SAE subdir, the chat template; input sha256s
    python -m p45.pod smoke      # ~1,500 prompts through every step below, production-width SAEs
                                 #   for 300 steps, gates, projection and cuts -> results/smoke.json
    python -m p45.pod dpo        # DPO model: thresholds, buffers, sigma2, post/pre/sftdata SAEs at
                                 #   each layer, scoring pass -> results/partial_dpo.npz
    python -m p45.pod sft        # SFT model: the same, sftmodel arm -> results/partial_sft.npz
    python -m p45.pod finalize   # maha_L24, union PCA, dF columns -> results/scores.csv

Reuses P42's code by import (P42 dir on sys.path through p45.contracts): `p42.feats` (template
parts, encode_prompts, pack_batches, pad_right, capture_hidden, the public-SAE class),
`p42.scoring` (Ledoit-Wolf Mahalanobis, PCA, write_scores_csv) and `p42.pod`'s helpers (Timer, JSON
I/O, pins, model loading, fingerprints, memmaps). The SAE code is `p45.sae`.

Rows (`load_rows`): P42's prompts.jsonl (every set) + data/P45/planted_law2.jsonl + the Dolma
chunks (set K.DOLMA); row index = line order. Roles: post trains on id_ref and validates on
id_heldout; pre on dolma split=train / heldout; sftdata on sft_ref minus a seeded
(K.SEED_BUFFER) K.SFTDATA_HELDOUT_N held-out subset / that subset; sftmodel (SFT model) on
id_ref / id_heldout. Content tokens = P42's content span (the user content inside the chat
template). Attention: sdpa, P42's smoke choice (sdpa vs eager max |dNLL| 0.004 on 16 prompts).

Per model (`run_model`):
1. thresholds: median content-token L2 norm per hidden-state index (K.LAYERS, plus PUB_SAE_HS
   for DPO) over the first THR_N id_ref prompts.
2. collection jobs, one forward pass each, early-stopped after the deepest block needed (no
   logit head): content tokens with norm > K.MASK_NORM_MULT x median are dropped (`mask_keep`),
   the rest Bernoulli-sampled with p = target / (content tokens of the job) and written fp16 to
   /ws memmaps (capped at the target; rows non-finite in fp16 are dropped and counted). The
   id_ref job also accumulates exact float64 sums for sigma2 = E||x - mu||^2 over every unmasked
   id_ref content token (each layer and, DPO, hs 17) and, DPO, the prompt-mean L24 feature (all
   content tokens, fp32) of every id_ref prompt.
3. training: per arm and layer, one buffer on the GPU at a time (model freed first),
   `p45.sae.train_sae` with sigma2 of that model and layer; checkpoints results/sae/<arm>_L<L>,
   curves results/train/<arm>_L<L>.json (+ the FVU of the SAE on every held-out buffer of that
   model and layer). Gates (bulk): own-held-out FVU <= K.GATE_HELDOUT_RATIO x train FVU and dead
   <= K.GATE_DEAD_FRAC, else retrain once at K.N_LATENTS_FALLBACK; a second failure is recorded
   (`gate_failed_twice`), not fatal.
4. scoring pass over K.SCORED_SETS with the reloaded checkpoints: per prompt and layer F_<arm> =
   prompt_F(sq_err, mask_keep, ...) / sigma2 (one sq_err call per SAE per forward batch, over
   all content tokens of the batch); DPO also norm_L<L> (mean content-token norm, all tokens),
   masked_dpo_L<L> (masked share), F_pub_L16 (public SAE on hs 17, per-token squared error from
   its encode/decode, hs-17 mask, GLOBAL sigma2_pub over id_ref) and the prompt-mean L24 feature.
   Written to results/partial_<model>.npz + .json.
finalize: maha_L24 (Ledoit-Wolf on the id_ref L24 means), union PCA (fit on id_ref and pool
means; rows id_ref, pool, planted_med, planted_law, planted_law2; fp16) -> results/pca_union/,
dF columns (log floor K.F_FLOOR), len_tokens = n_tokens -> results/scores.csv
(K.SCORE_ID_COLS + K.SCORE_COLS).

Resumability and fingerprints. /ws/p45_work/<model>/fingerprint.json covers the model pin,
attention, layers, buffer sizes, seeds, the rows and the code (contracts.py, pod.py): a mismatch
deletes that directory (STALE CHECKPOINT) and the work is redone. Thresholds and each collection
job have their own done-record there; a trained SAE is reused when its curves JSON carries the
same fingerprint (collection fp + sae.py + the training config); a scoring partial when its JSON
carries the same fingerprint (collection fp + every SAE's fp + the public SAE). A rerun of a
finished step loads no model.

Smoke (`step_smoke`): SMOKE_SIZES rows (seeded), SMOKE_BUFFER / SMOKE_HELDOUT token buffers,
production-width SAEs (K.N_LATENTS) for SMOKE_STEPS steps each, both models, finalize, and a
save/reload check; work in /ws/p45_smoke, outputs in results/smoke/ (its SAE weights are deleted
after the reload check, their JSON configs kept). Gates: no inf and <= 1% NaN
per score column, no non-finite buffer rows, falling batch FVU, post/sftmodel train and held-out
FVU in (0, 1), finite L24 means, sigma2 > 0, reload agrees. Projection (seconds):
    sum over models m of [2 loads x load_s(m) + collect_s_per_tok(m) x collect tokens(m)
                          + score_s_per_tok(m) x scored tokens(m)]
    + n_SAE x [steps x ms/step + n_evals x HELDOUT_TOKENS x eval_s_per_tok
               + buffer_tokens x (other_s_per_tok + buf_load_s_per_tok) + io_s]
    + finalize_s, all x 1.1, + MANIFEST_S + 3 x PROC_OVERHEAD_S,
with steps = ceil(buffer_tokens / K.BATCH_TOKENS) x K.EPOCHS, n_SAE = 4 x len(layers), token
counts = min(n_tokens, P42 MAX_PROMPT_TOKENS) + template tokens per prompt, every rate measured in
the smoke. It must be <= PROJ_FRACTION x the time left (deadline: env P45_DEADLINE_EPOCH, else
the killswitch deadline - SALVAGE_S, else P45_T0 + P45_BUDGET_S or gpu_hours - 15 min); else the
buffer is cut to K.BUFFER_TOKENS_CUT, then L16 (and the public SAE) is dropped; else rc 3. The
chosen settings go to results/smoke.json for the bulk steps (P45_SKIP_SMOKE=1: defaults).

Exit codes: 0 ok, 1 a failed check or error, 3 smoke projection over budget after both cuts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from p42 import contracts as P42K
from p42 import pod as P
from p42.pod import StepFailed, Timer, read_json, read_jsonl, sha256_file, utc, write_json

from p45 import contracts as K

WS = Path(os.environ.get("P45_WS", "/ws"))
RESULTS = K.RESULTS_DIR
KILLSWITCH_DEADLINE = Path(os.environ.get("P45_KS_DEADLINE", "/ws/killswitch/deadline"))
ATTN = os.environ.get("P45_ATTN", "sdpa")
FEATS_TOKENS = int(os.environ.get("P45_FEATS_TOKENS", "32768"))  # padded tokens per batch
MAX_BATCH = int(os.environ.get("P45_MAX_BATCH", "64"))
THR_N = 2000
SMOKE_SIZES = {P42K.ID_REF: 600, P42K.SFT_REF: 200, K.DOLMA: 200, P42K.ID_HELDOUT: 100,
               P42K.POOL: 100, P42K.PLANTED_MED: 100, P42K.PLANTED_LAW: 100, K.PLANTED_LAW2: 100}
SMOKE_BUFFER = 200_000
SMOKE_HELDOUT = 20_000
SMOKE_SFT_HO = 50
SMOKE_STEPS = 300
SMOKE_EVAL_EVERY = 100
SMOKE_SAE_EXTRA: dict = {}  # tests: TrainCfg overrides on top of the smoke's (production width)
SALVAGE_S = 20 * 60
MANIFEST_S = 120
PROC_OVERHEAD_S = 60
FINALIZE_ALLOW_S = 300
PROJ_FRACTION = 0.8
PROJ_MARGIN = 1.10
NAN_TOL = 0.01
RELOAD_TOL = 0.05
PUB_CHUNK = 4096

# injectable for tests
LOADER: Callable | None = None  # (name, attn) -> model
TOKENIZER: Callable | None = None  # () -> tokenizer
RENDER: Callable | None = None  # (content, tok) -> rendered prompt
DEVICE: str | None = None  # default cuda
PUB_SAE_DIR: str | None = os.environ.get("P45_TEST_SAE_DIR")  # tests; None -> the HF snapshot


def log(msg: str) -> None:
    print(f"{utc()} [p45.pod] {msg}", flush=True)


def device() -> str:
    return DEVICE or "cuda"


def code_sha(names: Sequence[str]) -> str:
    h = hashlib.sha256()
    for n in names:
        h.update(n.encode() + b"\0" + (Path(__file__).parent / n).read_bytes())
    return h.hexdigest()[:16]


def record(step: str, T: Timer, results: Path, **fields) -> dict:
    rec = {"step": step, "finished_utc": utc(), "seconds": round(T.total(), 1),
           "phases_s": {k: round(v, 2) for k, v in T.phases.items()},
           "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown"), **fields}
    write_json(results / f"run_{step}.json", rec)
    timing = read_json(results / "timing.json") or {}
    timing[step] = {"seconds": rec["seconds"], "phases_s": rec["phases_s"]}
    write_json(results / "timing.json", timing)
    return rec


# --- inputs ------------------------------------------------------------------------------------


def data_sha_expected(meta: dict, key: str, fname: str) -> str | None:
    """The sha256 data.meta.json records for one output: `{key: {"sha256": ...}}`, or a nested
    {"sha256"|"outputs"|"files": {fname: sha | {"sha256": sha}}}. None (fail closed) otherwise."""
    v = meta.get(key)
    if isinstance(v, dict) and isinstance(v.get("sha256"), str):
        return v["sha256"]
    for k in ("sha256", "outputs", "files"):
        sub = meta.get(k)
        if isinstance(sub, dict):
            for name in (fname, f"data/P45/{fname}"):
                v = sub.get(name)
                if isinstance(v, str):
                    return v
                if isinstance(v, dict) and isinstance(v.get("sha256"), str):
                    return v["sha256"]
    return None


def verify_inputs(prompts: Path = K.P42_PROMPTS, law2: Path = K.LAW2_PATH,
                  dolma: Path = K.DOLMA_PATH, meta_path: Path = K.DATA_META) -> dict:
    out = {}
    got = sha256_file(prompts)
    if got != K.P42_PROMPTS_SHA:
        raise StepFailed(f"prompts.jsonl sha256 {got} != {K.P42_PROMPTS_SHA}")
    out["prompts.jsonl"] = got
    meta = read_json(meta_path)
    if meta is None:
        raise StepFailed(f"{meta_path} missing ([pod].push)")
    for key, path in (("law2", law2), ("dolma", dolma)):
        want = data_sha_expected(meta, key, path.name)
        if want is None:
            raise StepFailed(f"data.meta.json records no sha256 for {key} ({path.name})")
        got = sha256_file(path)
        if got != want:
            raise StepFailed(f"{path.name} sha256 {got} != meta {want}")
        out[path.name] = got
    out["data.meta.json"] = sha256_file(meta_path)
    return out


def _row(r: dict, set_: str | None = None) -> dict:
    return {"pid": r["pid"], "set": set_ or r["set"], "text": r["text"],
            "n_tokens": int(r["n_tokens"]), "split": r.get("split")}


def load_rows() -> list[dict]:
    rows = [_row(r) for r in read_jsonl(K.P42_PROMPTS)]
    law2 = [_row(r) for r in read_jsonl(K.LAW2_PATH)]
    if any(r["set"] != K.PLANTED_LAW2 for r in law2):
        raise StepFailed(f"planted_law2.jsonl rows must have set {K.PLANTED_LAW2}")
    dolma = [_row(r, K.DOLMA) for r in read_jsonl(K.DOLMA_PATH)]
    if any(r["split"] not in ("train", "heldout") for r in dolma):
        raise StepFailed("dolma_chunks.jsonl split must be train | heldout")
    rows += law2 + dolma
    if len({r["pid"] for r in rows}) != len(rows):
        raise StepFailed("duplicate pids across prompts, law2 and dolma")
    return rows


def rows_sha(rows: Sequence[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps([r["pid"], r["set"], r["text"], r["split"]],
                            ensure_ascii=False).encode() + b"\n")
    return h.hexdigest()[:16]


def smoke_subset(rows: Sequence[dict], sizes: dict | None = None) -> list[dict]:
    rng = random.Random(K.SEED_BUFFER + 1)
    pick: list[int] = []
    for s, n in (sizes or SMOKE_SIZES).items():
        cand = [i for i, r in enumerate(rows) if r["set"] == s]
        pick += rng.sample(cand, min(n, len(cand)))
    return [rows[i] for i in sorted(pick)]


# --- context -----------------------------------------------------------------------------------


@dataclass
class Ctx:
    tag: str  # "bulk" | "smoke"
    rows: list[dict]
    work: Path
    results: Path
    layers: tuple[int, ...] = K.LAYERS
    pub: bool = True
    buffer_tokens: int = K.BUFFER_TOKENS
    heldout_tokens: int = K.HELDOUT_TOKENS
    thr_n: int = THR_N
    sft_ho_n: int = K.SFTDATA_HELDOUT_N
    attn: str = ATTN
    sae_cfg: dict = field(default_factory=dict)  # TrainCfg overrides (smoke, tests)
    retrain: bool = True  # bulk: retrain once at N_LATENTS_FALLBACK on a failed gate

    def idx(self, pred) -> np.ndarray:
        return np.array([i for i, r in enumerate(self.rows) if pred(r)], dtype=np.int64)


def bulk_ctx(rows: list[dict] | None = None) -> Ctx:
    return Ctx("bulk", rows if rows is not None else load_rows(), WS / "p45_work", RESULTS)


def smoke_ctx(rows: list[dict] | None = None) -> Ctx:
    sub = smoke_subset(rows if rows is not None else load_rows())
    return Ctx("smoke", sub, WS / "p45_smoke", RESULTS / "smoke", buffer_tokens=SMOKE_BUFFER,
               heldout_tokens=SMOKE_HELDOUT, sft_ho_n=SMOKE_SFT_HO,
               sae_cfg={"max_steps": SMOKE_STEPS, "epochs": 10_000,
                        "eval_every": SMOKE_EVAL_EVERY, **SMOKE_SAE_EXTRA}, retrain=False)


def default_settings() -> dict:
    return {"attn": ATTN, "buffer_tokens": K.BUFFER_TOKENS, "layers": list(K.LAYERS), "pub": True}


def settings() -> dict:
    s = read_json(RESULTS / "smoke.json")
    if s is None or not s.get("pass"):
        if os.environ.get("P45_SKIP_SMOKE") == "1":
            log("P45_SKIP_SMOKE=1: default settings without a passing smoke")
            return default_settings()
        raise StepFailed("no passing results/smoke.json (run the smoke first)")
    return s["settings"]


def with_settings(ctx: Ctx, s: dict | None = None) -> Ctx:
    s = s or settings()
    return replace(ctx, attn=s["attn"], buffer_tokens=int(s["buffer_tokens"]),
                   layers=tuple(int(L) for L in s["layers"]), pub=bool(s["pub"]))


def roles(ctx: Ctx) -> dict[str, np.ndarray]:
    by: dict[str, list[int]] = {}
    for i, r in enumerate(ctx.rows):
        by.setdefault(r["set"], []).append(i)
    sft = by.get(P42K.SFT_REF, [])
    ho = set(random.Random(K.SEED_BUFFER).sample(sft, min(ctx.sft_ho_n, len(sft))))
    dolma = by.get(K.DOLMA, [])
    arr = lambda xs: np.array(sorted(xs), dtype=np.int64)
    return {
        "thr": arr(by.get(P42K.ID_REF, [])[: ctx.thr_n]),
        "id_ref": arr(by.get(P42K.ID_REF, [])),
        "id_heldout": arr(by.get(P42K.ID_HELDOUT, [])),
        "sft_train": arr([i for i in sft if i not in ho]),
        "sft_ho": arr(ho),
        "dolma_train": arr([i for i in dolma if ctx.rows[i]["split"] == "train"]),
        "dolma_ho": arr([i for i in dolma if ctx.rows[i]["split"] == "heldout"]),
        "scored": ctx.idx(lambda r: r["set"] in K.SCORED_SETS),
    }


@dataclass(frozen=True)
class Job:
    name: str
    role: str  # key of roles()
    arm: str
    kind: str  # "train" | "heldout"
    sigma: bool = False  # exact sigma2 sums (id_ref)
    means: bool = False  # prompt-mean L24 into the means memmap (DPO id_ref)


def arms_of(model: str) -> tuple[str, ...]:
    return tuple(a for a in K.ARMS if K.ARM_MODEL[a] == model)


def jobs_for(model: str) -> list[Job]:
    if model == "dpo":
        return [Job("id_ref", "id_ref", "post", "train", sigma=True, means=True),
                Job("sft_train", "sft_train", "sftdata", "train"),
                Job("dolma_train", "dolma_train", "pre", "train"),
                Job("id_heldout", "id_heldout", "post", "heldout"),
                Job("dolma_ho", "dolma_ho", "pre", "heldout"),
                Job("sft_ho", "sft_ho", "sftdata", "heldout")]
    return [Job("id_ref", "id_ref", "sftmodel", "train", sigma=True),
            Job("id_heldout", "id_heldout", "sftmodel", "heldout")]


def job_name(model: str, arm: str, kind: str) -> str:
    return next(j.name for j in jobs_for(model) if j.arm == arm and j.kind == kind)


def hs_set(ctx: Ctx, model: str) -> tuple[int, ...]:
    """Hidden-state indices with a mask threshold and sigma2: the SAE layers, plus hs 17 (DPO)."""
    extra = (K.PUB_SAE_HS,) if (model == "dpo" and ctx.pub) else ()
    return tuple(sorted(set(ctx.layers) | set(extra)))


def buf_path(ctx: Ctx, model: str, arm: str, kind: str, L: int) -> Path:
    return ctx.work / model / f"buf_{arm}_{kind}_L{L}.npy"


def means_path(ctx: Ctx) -> Path:
    return ctx.work / "dpo" / "means_L24.npy"


def collect_fp(ctx: Ctx, model: str) -> dict:
    return {"stage": f"collect_{model}", "model": list(P42K.MODELS[model]), "attn": ctx.attn,
            "layers": list(ctx.layers), "hs": list(hs_set(ctx, model)),
            "buffer_tokens": ctx.buffer_tokens, "heldout_tokens": ctx.heldout_tokens,
            "thr_n": ctx.thr_n, "sft_ho_n": ctx.sft_ho_n, "seed": K.SEED_BUFFER,
            "mask_mult": K.MASK_NORM_MULT, "rows_sha": rows_sha(ctx.rows),
            "code_sha": code_sha(("contracts.py", "pod.py"))}


# --- models ------------------------------------------------------------------------------------


def load_tok():
    if TOKENIZER is not None:
        return TOKENIZER()
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(P.model_path("dpo"))


def render_fn(tok) -> Callable[[str], str]:
    if RENDER is not None:
        return lambda content: RENDER(content, tok)
    from emx import formats as F

    return lambda content: F.render_prompt("chat", content, tok)


class Holder:
    """Lazy model: loaded on first use (timed as `<name>_load`), freed before SAE training."""

    def __init__(self, name: str, attn: str, T: Timer):
        self.name, self.attn, self.T, self.m = name, attn, T, None
        self.load_s: list[float] = []

    def get(self):
        if self.m is None:
            s0 = time.time()
            with self.T(f"{self.name}_load"):
                self.m = LOADER(self.name, self.attn) if LOADER else P.load_model(self.name,
                                                                                 self.attn)
            self.load_s.append(time.time() - s0)
        return self.m

    def drop(self) -> None:
        self.m = None
        P.free()


# --- forward -----------------------------------------------------------------------------------


class _Stop(Exception):
    pass


def forward_content(model, enc: Sequence[tuple], hs_idxs: Sequence[int], pad: int):
    """One padded forward of the base model (no logit head), stopped after the block that
    produces the deepest index in hs_idxs. Returns ({hs index: content-token rows [T, d] float32
    on the model's device}, segs [(a, b)] per item into those rows)."""
    import torch
    from p42.feats import capture_hidden, pad_right

    dev = next(model.parameters()).device
    ids, mask = pad_right([e[0] for e in enc], pad, dev)
    base = model.model
    top = max(hs_idxs)
    with torch.no_grad(), capture_hidden(model, hs_idxs) as hs:
        h = None
        if 1 <= top < len(base.layers):
            def _raise(module, inputs, output):
                raise _Stop
            h = base.layers[top - 1].register_forward_hook(_raise)
        try:
            base(input_ids=ids, attention_mask=mask, use_cache=False)
        except _Stop:
            pass
        finally:
            if h is not None:
                h.remove()
        bi, ti, segs, a = [], [], [], 0
        for b, (_, c0, c1) in enumerate(enc):
            bi += [b] * (c1 - c0)
            ti += list(range(c0, c1))
            segs.append((a, a + c1 - c0))
            a += c1 - c0
        bt = torch.tensor(bi, dtype=torch.long, device=dev)
        tt = torch.tensor(ti, dtype=torch.long, device=dev)
        X = {L: hs[L][bt, tt].float() for L in hs_idxs}
    return X, segs


def seg_ids(segs, device):
    import torch

    return torch.cat([torch.full((b - a,), j, dtype=torch.long) for j, (a, b) in enumerate(segs)]
                     or [torch.zeros(0, dtype=torch.long)]).to(device)


def seg_mean(v, segs):
    """Per-segment mean of v ([T] or [T, d]) -> [n_segs] / [n_segs, d] float32 (NaN if empty)."""
    import torch

    sid = seg_ids(segs, v.device)
    shape = (len(segs),) + tuple(v.shape[1:])
    s = torch.zeros(shape, dtype=torch.float64, device=v.device).index_add_(0, sid, v.double())
    n = torch.tensor([b - a for a, b in segs], dtype=torch.float64, device=v.device)
    n = n.reshape((-1,) + (1,) * (v.dim() - 1))
    return (s / n).float()  # 0/0 -> NaN


def encode(ctx: Ctx, idx: Sequence[int], tok, parts):
    from p42.feats import encode_prompts

    return encode_prompts(tok, [ctx.rows[int(i)]["text"] for i in idx], parts)


def batches(enc) -> list[list[int]]:
    from p42.feats import pack_batches

    return pack_batches([len(e[0]) for e in enc], FEATS_TOKENS, MAX_BATCH)


# --- collection: thresholds and buffers --------------------------------------------------------


def run_thresholds(ctx, model, holder, tok, parts, R) -> dict:
    import torch

    path = ctx.work / model / "thresholds.json"
    got = read_json(path)
    if got is not None:
        return got
    hs = hs_set(ctx, model)
    enc = encode(ctx, R["thr"], tok, parts)
    norms: dict[int, list] = {L: [] for L in hs}
    m = holder.get()
    s0, tokens = time.time(), 0
    for b in batches(enc):
        X, _ = forward_content(m, [enc[j] for j in b], hs, P.pad_id(tok))
        for L in hs:
            norms[L].append(X[L].norm(dim=1).cpu())
        tokens += sum(len(enc[j][0]) for j in b)
    out = {"n_prompts": len(enc), "tokens": tokens, "seconds": time.time() - s0,
           "median_norm": {}, "masked_share": {}}
    for L in hs:
        v = torch.cat(norms[L]) if norms[L] else torch.zeros(0)
        if not len(v):
            raise StepFailed(f"{model}: no content tokens for the thresholds")
        med = float(v.median())
        out["median_norm"][str(L)] = med
        out["masked_share"][str(L)] = float((v > K.MASK_NORM_MULT * med).float().mean())
    write_json(path, out)
    return out


def run_job(ctx, model, holder, tok, parts, R, job: Job, thr: dict) -> dict:
    import torch

    from p45.sae import mask_keep

    path = ctx.work / model / f"job_{job.name}.json"
    got = read_json(path)
    if got is not None and got.get("done"):
        return got
    idx = R[job.role]
    enc = encode(ctx, idx, tok, parts)
    n_content = sum(c1 - c0 for _, c0, c1 in enc)
    target = ctx.buffer_tokens if job.kind == "train" else ctx.heldout_tokens
    p = min(1.0, target / max(n_content, 1))
    m = holder.get()
    d = m.config.hidden_size
    hs = hs_set(ctx, model)
    need = tuple(sorted(set(hs) | ({24} if job.means else set())))
    bufs = {L: P._open(buf_path(ctx, model, job.arm, job.kind, L), (target, d), np.float16)
            for L in ctx.layers}
    off = {L: 0 for L in ctx.layers}
    nonfinite = {L: 0 for L in ctx.layers}
    capped = {L: 0 for L in ctx.layers}
    sig_hs = hs if job.sigma else ()
    dev = next(m.parameters()).device
    s1 = {L: torch.zeros(d, dtype=torch.float64, device=dev) for L in sig_hs}
    s2 = {L: 0.0 for L in sig_hs}
    sn = {L: 0 for L in sig_hs}
    masked = {L: 0 for L in hs}
    means = P._open(means_path(ctx), (len(ctx.rows), d), np.float32, fill=np.nan) \
        if job.means else None
    rng = np.random.default_rng([K.SEED_BUFFER, int(hashlib.sha256(
        f"{model}/{job.name}".encode()).hexdigest()[:8], 16)])
    s0, tokens = time.time(), 0
    for b in batches(enc):
        X, segs = forward_content(m, [enc[j] for j in b], need, P.pad_id(tok))
        T_ = segs[-1][1] if segs else 0
        draw = torch.from_numpy(rng.random(T_) < p).to(dev)
        for L in hs:
            keep = mask_keep(X[L].norm(dim=1), thr["median_norm"][str(L)])
            masked[L] += int((~keep).sum())
            if L in s1:
                xk = X[L][keep].double()
                s1[L] += xk.sum(0)
                s2[L] += float((xk * xk).sum())
                sn[L] += int(keep.sum())
            if L in bufs:
                rows = X[L][keep & draw].half().cpu().numpy()
                ok = np.isfinite(rows).all(1)
                nonfinite[L] += int((~ok).sum())
                rows = rows[ok]
                room = target - off[L]
                capped[L] += max(0, len(rows) - room)
                rows = rows[:room]
                bufs[L][off[L] : off[L] + len(rows)] = rows
                off[L] += len(rows)
        if means is not None:
            mu = seg_mean(X[24], segs).cpu().numpy()
            for k, j in enumerate(b):
                means[int(idx[j])] = mu[k]
        tokens += sum(len(enc[j][0]) for j in b)
    for a in bufs.values():
        a.flush()
    if means is not None:
        means.flush()
    rec = {"done": True, "job": asdict(job), "n_prompts": len(enc), "n_content": n_content,
           "p": p, "target": target, "n_fill": {str(L): off[L] for L in ctx.layers},
           "capped": {str(L): capped[L] for L in ctx.layers},
           "nonfinite_dropped": {str(L): nonfinite[L] for L in ctx.layers},
           "masked": {str(L): masked[L] for L in hs}, "tokens": tokens,
           "seconds": time.time() - s0}
    if job.sigma:
        rec["sigma2"], rec["sigma_n"] = {}, {}
        for L in sig_hs:
            n = max(sn[L], 1)
            mu = s1[L] / n
            rec["sigma2"][str(L)] = s2[L] / n - float((mu * mu).sum())
            rec["sigma_n"][str(L)] = sn[L]
    write_json(path, rec)
    return rec


def load_buf(ctx, model, arm, kind, L, n: int, dev):
    """Rows [:n] of a buffer memmap as an fp16 tensor on dev (chunked, no full host copy)."""
    import torch

    mm = np.load(buf_path(ctx, model, arm, kind, L), mmap_mode="r")
    out = torch.empty((n, mm.shape[1]), dtype=torch.float16, device=dev)
    step = 1 << 18
    for a in range(0, n, step):
        out[a : a + step] = torch.from_numpy(np.array(mm[a : min(a + step, n)])).to(dev)
    return out


# --- training ----------------------------------------------------------------------------------


def sae_stem(ctx: Ctx, arm: str, L: int) -> Path:
    return ctx.results / "sae" / f"{arm}_L{L}"


def curves_path(ctx: Ctx, arm: str, L: int) -> Path:
    return ctx.results / "train" / f"{arm}_L{L}.json"


def gate(final: dict) -> dict:
    ok_ho = final["fvu_heldout"] <= K.GATE_HELDOUT_RATIO * final["fvu_train"]
    ok_dead = final["dead_frac"] <= K.GATE_DEAD_FRAC
    return {"heldout_ratio_ok": bool(ok_ho), "dead_ok": bool(ok_dead), "pass": bool(ok_ho and
                                                                                 ok_dead)}


def train_fp(ctx, model, arm, L, cfg: dict, jobs: dict) -> dict:
    tr = jobs[job_name(model, arm, "train")]
    return {"collect": collect_fp(ctx, model), "arm": arm, "layer": L, "cfg": cfg,
            "n_fill": tr["n_fill"][str(L)], "code_sha": code_sha(("contracts.py", "sae.py"))}


def train_arm(ctx, model, arm, L, sigma2, jobs, T) -> dict:
    import torch

    from p45 import sae as S

    cfg = asdict(S.TrainCfg(**ctx.sae_cfg))
    fp = train_fp(ctx, model, arm, L, cfg, jobs)
    old = read_json(curves_path(ctx, arm, L))
    stem = sae_stem(ctx, arm, L)
    if old and old.get("fp") == fp and stem.with_suffix(".safetensors").is_file():
        return {"skipped": True, **{k: old[k] for k in ("final", "gate", "retrained",
                                                         "gate_failed_twice")}}
    dev = device()
    nfill = lambda kind, a=arm: jobs[job_name(model, a, kind)]["n_fill"][str(L)]
    with T("buf_load"):
        buf = load_buf(ctx, model, arm, "train", L, nfill("train"), dev)
        ho = load_buf(ctx, model, arm, "heldout", L, nfill("heldout"), dev)
    T.buf_tokens = getattr(T, "buf_tokens", 0) + buf.shape[0]
    tag = f"{arm}_L{L}"
    with T("train"):
        sae, cur = S.train_sae(buf, ho, S.TrainCfg(**cfg), dev, sigma2,
                               log=lambda s: log(f"{tag} {s}"))
    g = gate(cur["final"])
    rec: dict[str, Any] = {"retrained": False, "gate_failed_twice": False}
    if not g["pass"] and ctx.retrain:
        log(f"{tag}: gate failed {g} {cur['final']}; retraining at {K.N_LATENTS_FALLBACK}")
        first = {"final": cur["final"], "gate": g, "n_latents": cfg["n_latents"]}
        cfg = {**cfg, "n_latents": K.N_LATENTS_FALLBACK}
        del sae
        with T("train"):
            sae, cur = S.train_sae(buf, ho, S.TrainCfg(**cfg), dev, sigma2,
                                   log=lambda s: log(f"{tag} {s}"))
        g = gate(cur["final"])
        rec = {"retrained": True, "first_attempt": first, "gate_failed_twice": not g["pass"]}
        if not g["pass"]:
            log(f"FLAG {tag}: gate failed twice {g} (recorded, not fatal)")
    del buf, ho
    s0 = time.time()
    cross = {}
    for a2 in arms_of(model):
        h2 = load_buf(ctx, model, a2, "heldout", L, nfill("heldout", a2), dev)
        cross[a2] = S.agg_fvu(sae, h2, sigma2)
        del h2
    info = {"arm": arm, "layer": L, "model": model, "sigma2": sigma2, "n_latents": cfg["n_latents"]}
    sae.save(stem, info=info)
    io_s = time.time() - s0
    T.phases["sae_io"] = T.phases.get("sae_io", 0.0) + io_s
    out = {"fp": fp, "cfg": cfg, "sigma2": sigma2, "gate": g, "cross_fvu_heldout": cross,
           "io_seconds": io_s, **rec, **cur}
    write_json(curves_path(ctx, arm, L), out)
    del sae
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return {"skipped": False, "final": cur["final"], "gate": g, "retrained": rec["retrained"],
            "gate_failed_twice": rec["gate_failed_twice"]}


# --- scoring -----------------------------------------------------------------------------------


def pub_dir() -> str | None:
    if PUB_SAE_DIR:
        return PUB_SAE_DIR
    d = P.sae_dir()
    return str(d) if d else None


def pub_sq_err(sae, X):
    import torch

    out = []
    with torch.no_grad():
        for a in range(0, X.shape[0], PUB_CHUNK):
            x = X[a : a + PUB_CHUNK]
            out.append(((x - sae.decode(sae.encode(x))) ** 2).sum(1).cpu())
    return torch.cat(out) if out else torch.zeros(0)


def score_fp(ctx, model) -> dict:
    fps = {}
    for arm in arms_of(model):
        for L in ctx.layers:
            c = read_json(curves_path(ctx, arm, L))
            if c is None:
                raise StepFailed(f"{arm}_L{L} not trained")
            fps[f"{arm}_L{L}"] = c["fp"]
    return {"collect": collect_fp(ctx, model), "saes": fps,
            "pub": [P42K.SAE_REPO, P42K.SAE_REV, P42K.SAE_SUBDIR] if (model == "dpo" and ctx.pub)
            else None, "code_sha": code_sha(("contracts.py", "pod.py", "sae.py"))}


def partial_paths(ctx, model) -> tuple[Path, Path]:
    return ctx.results / f"partial_{model}.npz", ctx.results / f"partial_{model}.json"


def run_score(ctx, model, holder, tok, parts, R, thr, sig, T) -> dict:
    import torch
    from p42.feats import SAE, hs_index_for_hook

    from p45 import sae as S

    fp = score_fp(ctx, model)
    npz, js = partial_paths(ctx, model)
    old = read_json(js)
    if old and old.get("fp") == fp and npz.is_file():
        return {"skipped": True, **{k: v for k, v in old.items() if k != "fp"}}
    dev = device()
    hs = hs_set(ctx, model)
    saes = {}
    with T(f"{model}_sae_load"):
        for arm in arms_of(model):
            for L in ctx.layers:
                saes[(arm, L)] = S.TopKSAE.load(sae_stem(ctx, arm, L), dev)
        pub, pub_info = None, None
        if model == "dpo" and ctx.pub:
            try:
                d = pub_dir()
                if not d:
                    raise FileNotFoundError("public SAE files not found")
                pub, pub_info = SAE.load(d, dev, "layer_16")
                if hs_index_for_hook(pub_info["hook_name"]) != K.PUB_SAE_HS:
                    raise ValueError(f"public SAE hook {pub_info['hook_name']} != hs "
                                     f"{K.PUB_SAE_HS}")
            except Exception as e:  # noqa: BLE001  (exploratory: skip with a logged reason)
                pub, pub_info = None, {"skipped": repr(e)[:500]}
                log(f"F_pub skipped: {e!r}")
    idx = R["scored"]
    enc = encode(ctx, idx, tok, parts)
    n = len(idx)
    nan = lambda: np.full(n, np.nan)
    cols: dict[str, np.ndarray] = {"n_content": np.array([c1 - c0 for _, c0, c1 in enc],
                                                         dtype=np.float64)}
    for arm in arms_of(model):
        for L in ctx.layers:
            cols[f"F_{arm}_L{L}"] = nan()
    for L in ctx.layers:
        cols[f"masked_{model}_L{L}"] = nan()
        if model == "dpo":
            cols[f"norm_L{L}"] = nan()
    if pub is not None:
        cols["F_pub_L16"] = nan()
    means = P._open(means_path(ctx), (len(ctx.rows), holder.get().config.hidden_size),
                    np.float32, fill=np.nan) if model == "dpo" else None
    m = holder.get()
    need = tuple(sorted(set(hs) | ({24} if model == "dpo" else set())))
    s0, tokens = time.time(), 0
    with T(f"{model}_score"):
        for b in batches(enc):
            X, segs = forward_content(m, [enc[j] for j in b], need, P.pad_id(tok))
            jj = np.array(b, dtype=np.int64)
            for L in hs:
                norms = X[L].norm(dim=1)
                keep = S.mask_keep(norms, thr["median_norm"][str(L)])
                if L in ctx.layers:
                    for arm in arms_of(model):
                        se = saes[(arm, L)].sq_err(X[L])
                        cols[f"F_{arm}_L{L}"][jj] = S.prompt_F(se, keep, segs, sig[str(L)])
                    kept = seg_mean(keep.float(), segs).cpu().numpy()
                    cols[f"masked_{model}_L{L}"][jj] = 1.0 - kept
                    if model == "dpo":
                        cols[f"norm_L{L}"][jj] = seg_mean(norms, segs).cpu().numpy()
                if L == K.PUB_SAE_HS and pub is not None:
                    se = pub_sq_err(pub, X[L])
                    cols["F_pub_L16"][jj] = S.prompt_F(se, keep, segs, sig[str(L)])
            if means is not None:
                mu = seg_mean(X[24], segs).cpu().numpy()
                for k, j in enumerate(b):
                    means[int(idx[j])] = mu[k]
            tokens += sum(len(enc[j][0]) for j in b)
    if means is not None:
        means.flush()
    tmp = npz.with_name(f".{npz.stem}.tmp.npz")
    np.savez(tmp, row=idx, **cols)
    os.replace(tmp, npz)
    rec = {"n_scored": n, "tokens": tokens, "seconds": time.time() - s0, "pub": pub_info,
           "nan_counts": {c: int((~np.isfinite(v)).sum()) for c, v in cols.items()}}
    write_json(js, {"fp": fp, **rec})
    del saes, pub
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return {"skipped": False, **rec}


# --- one model, end to end ---------------------------------------------------------------------


def run_model(ctx: Ctx, model: str, T: Timer) -> dict:
    """Thresholds, collection jobs, training, scoring for one model (resumable)."""
    from p42.feats import template_parts

    P.enable_tf32()
    with T(f"{model}_prepare"):
        stale = P.check_fingerprint(ctx.work / model, collect_fp(ctx, model),
                                    f"{model} collection")
        tok = load_tok()
        parts = template_parts(tok, render_fn(tok))
        R = roles(ctx)
    holder = Holder(model, ctx.attn, T)
    rec: dict[str, Any] = {"model": list(P42K.MODELS[model]), "attn": ctx.attn,
                           "layers": list(ctx.layers), "pub": ctx.pub, "stale_reset": stale,
                           "n_rows": {k: len(v) for k, v in R.items()}}
    with T(f"{model}_collect"):
        thr = run_thresholds(ctx, model, holder, tok, parts, R)
        jobs = {j.name: run_job(ctx, model, holder, tok, parts, R, j, thr)
                for j in jobs_for(model)}
    sig = jobs["id_ref"]["sigma2"]
    if not all(math.isfinite(v) and v > 0 for v in sig.values()):
        raise StepFailed(f"{model}: sigma2 not positive {sig}")
    rec.update({"thresholds": thr, "sigma2": sig,
                "jobs": {k: {kk: vv for kk, vv in v.items() if kk != "job"}
                         for k, v in jobs.items()}})
    holder.drop()
    rec["train"] = {f"{arm}_L{L}": train_arm(ctx, model, arm, L, sig[str(L)], jobs, T)
                    for arm in arms_of(model) for L in ctx.layers}
    rec["score"] = run_score(ctx, model, holder, tok, parts, R, thr, sig, T)
    rec["load_seconds"] = holder.load_s
    rec["peak_mem_gb"] = P.peak_mem()
    holder.drop()
    return rec


def step_model(model: str, ctx: Ctx | None = None) -> int:
    ctx = ctx or with_settings(bulk_ctx())
    T = Timer()
    rec = run_model(ctx, model, T)
    flagged = [k for k, v in rec["train"].items() if v.get("gate_failed_twice")]
    record(model, T, ctx.results, settings=ctx_settings(ctx), flagged=flagged, **rec)
    return 0


def ctx_settings(ctx: Ctx) -> dict:
    return {"attn": ctx.attn, "buffer_tokens": ctx.buffer_tokens, "layers": list(ctx.layers),
            "pub": ctx.pub}


# --- finalize ----------------------------------------------------------------------------------


def logF(x: np.ndarray) -> np.ndarray:
    return np.log(np.maximum(x, K.F_FLOOR))  # NaN stays NaN


def finalize(ctx: Ctx, T: Timer) -> dict:
    from p42 import scoring as Sc

    dev = device()
    R = roles(ctx)
    scored = R["scored"]
    n = len(scored)
    cols: dict[str, np.ndarray] = {}
    info: dict[str, Any] = {"n_scored": n}
    for model in ("dpo", "sft"):
        npz, js = partial_paths(ctx, model)
        meta = read_json(js)
        if meta is None or not npz.is_file():
            raise StepFailed(f"partial_{model} missing (run the {model} step)")
        if meta["fp"]["collect"] != collect_fp(ctx, model):
            raise StepFailed(f"partial_{model} was made under other inputs/settings")
        z = np.load(npz)
        if not np.array_equal(z["row"], scored):
            raise StepFailed(f"partial_{model} rows differ from the scored rows")
        for c in z.files:
            if c not in ("row", "n_content"):
                cols[c] = z[c].astype(np.float64)
        info[f"partial_{model}_sha256"] = sha256_file(npz)
    nanv = np.full(n, np.nan)
    for L in K.LAYERS:
        post = cols.get(f"F_post_L{L}", nanv)
        cols[f"F_post_L{L}"] = post
        for a, src in (("pre", "pre"), ("sftdata", "sftdata"), ("model", "sftmodel")):
            cols[f"dF_{a}_L{L}"] = logF(post) - logF(cols.get(f"F_{src}_L{L}", nanv))
    cols["dF_pub_L16"] = logF(cols["F_post_L16"]) - logF(cols.get("F_pub_L16", nanv))
    cols["len_tokens"] = np.array([ctx.rows[i]["n_tokens"] for i in scored], dtype=np.float64)
    means = np.load(means_path(ctx), mmap_mode="r")
    fin = np.isfinite(np.asarray(means[:, :1])).ravel()  # a row is all-NaN or filled
    with T("maha"):
        ref = R["id_ref"][fin[R["id_ref"]]]
        if len(ref) < 2:
            raise StepFailed("fewer than 2 id_ref L24 means")
        mask = P.Mask(fin[scored])
        maha = Sc.Mahalanobis(np.asarray(means[ref], dtype=np.float32), dev)
        cols["maha_L24"] = mask.fill(maha.score(np.asarray(means[scored[mask.on]],
                                                           dtype=np.float32)))
        info["maha"] = {"ref_rows": len(ref), "shrinkage": maha.shrinkage}
    with T("pca_union"):
        info["pca_union"] = write_pca_union(ctx, means, fin, R, dev)
    with T("write"):
        ids = {c: [ctx.rows[i][c] for i in scored] for c in K.SCORE_ID_COLS}
        Sc.write_scores_csv(ctx.results / "scores.csv", K.SCORE_ID_COLS, K.SCORE_COLS, ids, cols)
    info["nan_counts"] = {c: int((~np.isfinite(cols.get(c, nanv))).sum()) for c in K.SCORE_COLS}
    info["inf_counts"] = {c: int(np.isinf(cols.get(c, nanv)).sum()) for c in K.SCORE_COLS}
    info["scores_sha256"] = sha256_file(ctx.results / "scores.csv")
    return info


def write_pca_union(ctx, means, fin, R, dev) -> dict:
    from p42 import scoring as Sc

    out = ctx.results / "pca_union"
    out.mkdir(parents=True, exist_ok=True)
    by_set = lambda s: ctx.idx(lambda r: r["set"] == s)
    fit = np.concatenate([R["id_ref"], by_set(P42K.POOL)])
    fit = fit[fin[fit]]
    rows = np.concatenate([by_set(s) for s in (P42K.ID_REF, P42K.POOL, *P42K.PLANTED,
                                               K.PLANTED_LAW2)])
    rows = rows[fin[rows]]
    dim = min(P42K.PCA_DIM, len(fit) - 1, means.shape[1])
    if dim < 1:
        raise StepFailed("too few rows for the union PCA")
    mean, comps = Sc.pca_fit(np.asarray(means[fit], dtype=np.float32), dim, dev)
    Z = Sc.pca_project(np.asarray(means[rows], dtype=np.float32), mean, comps, dev)
    tmp = out / ".dpo_p_L24.npy.tmp"
    with open(tmp, "wb") as fh:
        np.save(fh, Z.astype(np.float16))
    os.replace(tmp, out / "dpo_p_L24.npy")
    tmp = out / ".pids.txt.tmp"
    tmp.write_text("".join(ctx.rows[i]["pid"] + "\n" for i in rows))
    os.replace(tmp, out / "pids.txt")
    return {"fit_rows": len(fit), "rows": len(rows), "dim": int(dim)}


def step_finalize(args=None, ctx: Ctx | None = None) -> int:
    ctx = ctx or with_settings(bulk_ctx())
    T = Timer()
    info = finalize(ctx, T)
    bad = {c: v for c, v in info["inf_counts"].items() if v}
    if bad:
        raise StepFailed(f"infinite scores: {bad}")
    record("finalize", T, ctx.results, settings=ctx_settings(ctx), **info)
    return 0


# --- download ----------------------------------------------------------------------------------


def step_download(args=None) -> int:
    from huggingface_hub import snapshot_download

    T = Timer()
    P.check_pins()
    info: dict[str, Any] = {"hf_hub_disable_xet": os.environ.get("HF_HUB_DISABLE_XET")}
    with T("inputs"):
        info["inputs"] = verify_inputs()
    for name, (repo, rev) in P42K.MODELS.items():
        with T(f"model_{name}"):
            p = Path(snapshot_download(repo, revision=rev, allow_patterns=P.MODEL_PATTERNS))
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        info[name] = {"path": str(p), "bytes": size,
                      "MB_s": round(size / 1e6 / max(T.phases[f"model_{name}"], 1e-3), 1)}
        cfg = json.loads((p / "config.json").read_text())
        bad = {k: (cfg.get(k), v) for k, v in P.EXPECT_CFG.items() if cfg.get(k) != v}
        if bad:
            raise StepFailed(f"{name} config mismatch {bad}")
    with T("pub_sae"):
        try:
            snapshot_download(P42K.SAE_REPO, revision=P42K.SAE_REV,
                              allow_patterns=[f"{P42K.SAE_SUBDIR}/*"])
            info["pub_sae"] = {"dir": str(P.sae_dir()), "ok": P.sae_dir() is not None}
        except Exception as e:  # noqa: BLE001  (F_pub is exploratory: log and go on)
            info["pub_sae"] = {"ok": False, "error": repr(e)[:500]}
            log(f"public SAE download failed (F_pub will be skipped): {e!r}")
    with T("template"):
        from transformers import AutoTokenizer

        from emx import formats as F

        F.chat_template()
        t_dpo = AutoTokenizer.from_pretrained(P.model_path("dpo"))
        t_sft = AutoTokenizer.from_pretrained(P.model_path("sft"))
        if t_dpo.get_vocab() != t_sft.get_vocab():
            raise StepFailed("DPO and SFT tokenizers differ (one tokenisation, DPO's)")
        info["template_sha256"] = F.template_sha256("chat")
    record("download", T, RESULTS, **info)
    return 0


# --- smoke: rates, projection, cuts ------------------------------------------------------------


def deadline_epoch() -> tuple[float | None, str]:
    if os.environ.get("P45_DEADLINE_EPOCH"):
        return float(os.environ["P45_DEADLINE_EPOCH"]), "env P45_DEADLINE_EPOCH"
    if KILLSWITCH_DEADLINE.is_file():
        try:
            return float(KILLSWITCH_DEADLINE.read_text().split()[0]) - SALVAGE_S, \
                f"killswitch deadline - {SALVAGE_S} s"
        except (ValueError, IndexError):
            pass
    t0 = os.environ.get("P45_T0")
    if t0:
        if os.environ.get("P45_BUDGET_S"):
            return float(t0) + float(os.environ["P45_BUDGET_S"]), "P45_T0 + P45_BUDGET_S"
        gpu_h = float(tomllib.loads((K.PROBE_DIR / "probe.toml").read_text())["pod"]["gpu_hours"])
        return float(t0) + gpu_h * 3600 - 900, "P45_T0 + gpu_hours - 15 min"
    return None, "none"


@dataclass
class Rates:
    """Measured in the smoke (seconds; per token = per full-sequence token, template included)."""
    collect_s_per_tok: dict  # model -> s/token (thresholds + collection passes)
    score_s_per_tok: dict  # model -> s/token (scoring pass, SAE evaluation included)
    load_s: dict  # model -> seconds per load
    ms_per_step: float
    eval_s_per_tok: float  # held-out sq_err seconds per held-out token per evaluation
    other_s_per_tok: float  # SAE setup + final train FVU, per train-buffer token
    buf_load_s_per_tok: float
    io_s: float  # per SAE: cross FVU + save
    finalize_s: float = FINALIZE_ALLOW_S


def bulk_counts(rows: Sequence[dict], template_tokens: int, thr_n: int = THR_N) -> dict:
    """Full-sequence token totals per model and pass kind for the bulk run."""
    def L(r):
        return min(int(r["n_tokens"]), P42K.MAX_PROMPT_TOKENS) + template_tokens
    s = lambda pred: sum(L(r) for r in rows if pred(r))
    idr = [r for r in rows if r["set"] == P42K.ID_REF]
    thr = sum(L(r) for r in idr[:thr_n])
    id_ref, id_ho = s(lambda r: r["set"] == P42K.ID_REF), s(lambda r: r["set"] == P42K.ID_HELDOUT)
    other = s(lambda r: r["set"] in (P42K.SFT_REF, K.DOLMA))
    scored = s(lambda r: r["set"] in K.SCORED_SETS)
    return {"collect": {"dpo": thr + id_ref + id_ho + other, "sft": thr + id_ref + id_ho},
            "score": {"dpo": scored, "sft": scored}}


def project(rates: Rates, counts: dict, buffer_tokens: int, layers: Sequence[int]) -> dict:
    steps = math.ceil(buffer_tokens / K.BATCH_TOKENS) * K.EPOCHS
    n_evals = steps // K.EVAL_EVERY + 1
    n_sae = len(K.ARMS) * len(layers)
    per_sae = (steps * rates.ms_per_step / 1000
               + n_evals * K.HELDOUT_TOKENS * rates.eval_s_per_tok
               + buffer_tokens * (rates.other_s_per_tok + rates.buf_load_s_per_tok) + rates.io_s)
    ph = {}
    for m in ("dpo", "sft"):
        ph[f"{m}_forwards"] = (2 * rates.load_s[m]
                               + rates.collect_s_per_tok[m] * counts["collect"][m]
                               + rates.score_s_per_tok[m] * counts["score"][m])
    ph["sae_training"] = n_sae * per_sae
    ph["finalize"] = rates.finalize_s
    ph["margin"] = (PROJ_MARGIN - 1) * sum(ph.values())
    ph["manifest"] = MANIFEST_S
    ph["process_overhead"] = 3 * PROC_OVERHEAD_S
    ph["total"] = sum(ph.values())
    ph["steps_per_sae"] = steps
    ph["n_sae"] = n_sae
    return ph


def choose_settings(rates: Rates, counts: dict, time_left: float | None) -> dict:
    """The plan's cut order: buffer 2.5M -> 1.5M tokens, then drop L16 (and the public SAE)."""
    tries = [(K.BUFFER_TOKENS, tuple(K.LAYERS), "none"),
             (K.BUFFER_TOKENS_CUT, tuple(K.LAYERS), "buffer 2.5M -> 1.5M"),
             (K.BUFFER_TOKENS_CUT, (24,), "buffer 2.5M -> 1.5M and drop L16")]
    tried = []
    for buf, layers, cut in tries:
        ph = project(rates, counts, buf, layers)
        ok = time_left is None or ph["total"] <= PROJ_FRACTION * time_left
        tried.append({"cut": cut, "buffer_tokens": buf, "layers": list(layers),
                      "projection_s": ph, "ok": ok})
        if ok:
            return {"ok": True, "buffer_tokens": buf, "layers": list(layers),
                    "pub": 16 in layers, "cut": cut, "tried": tried}
    return {"ok": False, "tried": tried}


def smoke_rates(ctx: Ctx, recs: dict, T: Timer, fin_s: float) -> Rates:
    col, sco, load = {}, {}, {}
    for m in ("dpo", "sft"):
        r = recs[m]
        tok_c = r["thresholds"]["tokens"] + sum(j["tokens"] for j in r["jobs"].values())
        first_load = r["load_seconds"][0] if r["load_seconds"] else 0.0
        col[m] = max(0.0, T.phases.get(f"{m}_collect", 0.0) - first_load) / max(tok_c, 1)
        sco[m] = r["score"]["seconds"] / max(r["score"]["tokens"], 1)
        load[m] = float(np.mean(r["load_seconds"])) if r["load_seconds"] else 0.0
    curves = [read_json(curves_path(ctx, a, L)) for a in K.ARMS for L in ctx.layers]
    fins = [c["final"] for c in curves]
    ms = float(np.mean([f["ms_per_step"] for f in fins]))
    ev = float(np.mean([f["eval_seconds"] / max(len(c["eval_step"]) * f["n_heldout_tokens"], 1)
                        for c, f in zip(curves, fins, strict=True)]))
    other = float(np.mean([max(0.0, f["seconds"] - f["ms_per_step"] * f["steps"] / 1000
                               - f["eval_seconds"]) / max(f["n_train_tokens"], 1) for f in fins]))
    io = float(np.mean([c["io_seconds"] for c in curves]))
    bl = T.phases.get("buf_load", 0.0) / max(getattr(T, "buf_tokens", 0), 1)
    return Rates(col, sco, load, ms, ev, other, bl, io, max(FINALIZE_ALLOW_S, 3 * fin_s))


def smoke_gates(ctx: Ctx, recs: dict, fin: dict, reload: dict) -> dict:
    n = max(fin["n_scored"], 1)
    expl = set(K.EXPLORATORY)
    nan_bad = {c: v for c, v in fin["nan_counts"].items()
               if v > NAN_TOL * n and (c not in expl or ctx.pub)
               and not (c.endswith("_L16") and 16 not in ctx.layers)}
    inf_bad = {c: v for c, v in fin["inf_counts"].items() if v}
    nonfinite_buf = {f"{m}.{j}.L{L}": v for m in ("dpo", "sft")
                     for j, r in recs[m]["jobs"].items()
                     for L, v in r["nonfinite_dropped"].items() if v}
    falling, fvu_ok = {}, {}
    for a in K.ARMS:
        for L in ctx.layers:
            c = read_json(curves_path(ctx, a, L))
            series = c.get("fvu_batch") or c.get("loss") or []
            falling[f"{a}_L{L}"] = len(series) >= 2 and series[-1] < series[0]
            if a in ("post", "sftmodel"):
                f = c["final"]
                fvu_ok[f"{a}_L{L}"] = 0 < f["fvu_train"] < 1 and 0 < f["fvu_heldout"] < 1
    means = np.load(means_path(ctx), mmap_mode="r")
    R = roles(ctx)
    want = np.concatenate([R["id_ref"], R["scored"]])
    means_ok = bool(np.isfinite(np.asarray(means[want])).all())
    sig_ok = all(v > 0 for m in ("dpo", "sft") for v in recs[m]["sigma2"].values())
    gates = {"no_nonfinite_scores": not nan_bad and not inf_bad,
             "no_nonfinite_buffers": not nonfinite_buf, "losses_falling": all(falling.values()),
             "id_fvu_in_01": bool(fvu_ok) and all(fvu_ok.values()), "finite_L24_means": means_ok,
             "sigma2_positive": sig_ok, "reload_agrees": reload["max_rel_diff"] <= RELOAD_TOL}
    return {"gates": gates, "pass": all(gates.values()), "nan_bad": nan_bad, "inf_bad": inf_bad,
            "nonfinite_buffers": nonfinite_buf, "falling": falling, "fvu_ok": fvu_ok}


def reload_check(ctx: Ctx) -> dict:
    """Each saved SAE reloaded: sq_err on its held-out rows vs its own held-out FVU record."""
    import torch

    from p45 import sae as S

    worst, t0 = 0.0, time.time()
    for a in K.ARMS:
        for L in ctx.layers:
            c = read_json(curves_path(ctx, a, L))
            m = K.ARM_MODEL[a]
            jn = job_name(m, a, "heldout")
            n = read_json(ctx.work / m / f"job_{jn}.json")["n_fill"][str(L)]
            ho = load_buf(ctx, m, a, "heldout", L, n, device())
            sae = S.TopKSAE.load(sae_stem(ctx, a, L), device())
            got = S.agg_fvu(sae, ho, c["sigma2"])
            want = c["final"]["fvu_heldout"]
            worst = max(worst, abs(got - want) / max(abs(want), 1e-12))
            del sae, ho
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return {"max_rel_diff": worst, "seconds": time.time() - t0}


def step_smoke(args=None, rows: list[dict] | None = None) -> int:
    from p42.feats import template_parts

    T = Timer()
    all_rows = rows if rows is not None else load_rows()
    ctx = smoke_ctx(all_rows)
    for d in (ctx.work, ctx.results):  # a rerun starts clean (its rates must be measured)
        if d.exists():
            shutil.rmtree(d)
    log(f"smoke: {len(ctx.rows)} prompts")
    recs = {m: run_model(ctx, m, T) for m in ("dpo", "sft")}
    with T("finalize"):
        fin = finalize(ctx, Timer())
    reload = reload_check(ctx)
    res = smoke_gates(ctx, recs, fin, reload)
    for f in (ctx.results / "sae").glob("*.safetensors"):  # ~2.1 GB the runner need not pull
        f.unlink()
    tok = load_tok()
    parts = template_parts(tok, render_fn(tok))
    tmpl = len(parts.prefix_ids) + len(parts.suffix_ids)
    rates = smoke_rates(ctx, recs, T, T.phases["finalize"])
    deadline, source = deadline_epoch()
    time_left = None if deadline is None else deadline - time.time()
    counts = bulk_counts(all_rows, tmpl)
    choice = choose_settings(rates, counts, time_left)
    out = {"pass": bool(res["pass"] and choice["ok"]), "gates_pass": res["pass"],
           "projection_ok": choice["ok"],
           "settings": {"attn": ctx.attn, "buffer_tokens": choice.get("buffer_tokens"),
                        "layers": choice.get("layers"), "pub": choice.get("pub")},
           "cut": choice.get("cut"), "projection_tried": choice["tried"],
           "deadline_epoch": deadline, "deadline_source": source, "time_left_s": time_left,
           "budget_fraction": PROJ_FRACTION, "rates": asdict(rates), "bulk_counts": counts,
           "template_tokens": tmpl, "n_smoke": len(ctx.rows), "reload": reload,
           "models": recs, "finalize": fin, **{k: v for k, v in res.items() if k != "pass"},
           "phases_s": T.phases, "seconds": T.total(), "finished_utc": utc(),
           "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown")}
    write_json(ctx.results.parent / "smoke.json", out)  # results/smoke.json on the pod
    timing_path = ctx.results.parent / "timing.json"
    timing = read_json(timing_path) or {}
    timing["smoke"] = {"seconds": round(T.total(), 1),
                       "phases_s": {k: round(v, 2) for k, v in T.phases.items()}}
    write_json(timing_path, timing)
    log(f"smoke gates {res['gates']} projection {choice['tried'][-1]['projection_s']['total']:.0f}"
        f" s vs time left {time_left}")
    if not res["pass"]:
        log(f"SMOKE FAIL gates: {res['gates']}")
        return 1
    if not choice["ok"]:
        log("SMOKE FAIL projection over budget after both cuts")
        return 3
    return 0


# --- main --------------------------------------------------------------------------------------


STEPS = {"download": step_download, "smoke": step_smoke,
         "dpo": lambda args: step_model("dpo"), "sft": lambda args: step_model("sft"),
         "finalize": step_finalize}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p45.pod")
    ap.add_argument("step", choices=sorted(STEPS))
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
