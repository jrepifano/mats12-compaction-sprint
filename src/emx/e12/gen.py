"""E12 generation library (vLLM): request plans per evaluation kind, grouped multi-adapter
generation with immutable LoRA int ids, per-(kind, key) outputs with resume, the E12 adapter-
config check, and the applied-smoke check for arbitrary adapters.

Owner: unit U3. FROZEN SIGNATURES (U0): the driver (U4) calls these; U3 implements the bodies and
may add private helpers, but does not change a public signature or return shape.

Reuses `emx.qwen.gen` (`llm_kwargs`, `sampling_kwargs`, `postprocess`, `rows_from_outputs`,
`atomic_write`, `write_json`, `adapter_file_sha256`, `sha256_file`, `git_commit`, `utc`),
`emx.qwen.render.render_prompt` (prompts as token ids, Qwen's default system prompt), and
`emx.gen_common.Request` / `row`. `emx.qwen.gen` is tied to EQ0's layout; nothing here writes
under results/EQ0.

Conventions (contracts): condition = `contracts.condition(recipe, key, kind)`; seed =
`request_seed(condition, qid, sample_idx)`; `cell` = kind; cue rows carry the wrapped question in
`question` and keep the ext48 qid and qset; task rows have qset "task", qid
`holdout_<original_index>`, question = the holdout prompt. Plans: questions in file order,
sample_idx 0..n-1 (n from `contracts.kind_samples(kind, cuts)`).

Ported pieces (cited per function): `emx.qwen.gen.existing_problems` / `write_outputs` /
`model_pins` (-> `output_problems`, `write_outputs`, `model_pins`), `emx.qwen.gen.
adapter_config_problems` (-> the E12 recipe check), and from scripts/eq0_pod.py
`greedy_continuations`, `continuation_logprobs`, `lora_applied_verdict`, `smoke_lora_check`
(-> `applied_smoke`, batched over adapters with one LoRARequest per request).

Additions beyond the frozen stubs (public, additive): `adapter_path`, `model_pins`, `build_meta`
(the HF path's driver builds its meta with `gen_path="hf"`), `SEED_RULE`, `SUPERSEDED`.
A (kind, key) whose outputs exist but fail verification is moved to
`results/E12/<recipe>/superseded/gen/<kind>/<key>.<UTC>.{csv,meta.json}` (outside `gen/`, so no
glob of `gen/<kind>/*.csv` sees it; still gitignored) and regenerated.

vLLM, torch and huggingface_hub are imported lazily inside functions (pod venvs only).
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from emx.contracts import EVAL_SEED, GEN_COLUMNS, GEN_META_KEYS, request_seed
from emx.e12 import contracts as C
from emx.gen_common import Request
from emx.qwen import gen as G
from emx.qwen import registry as R
from emx.qwen.render import render_prompt, template_sha256

ENGINE = "vllm"
SEED_RULE = (f"request_seed(condition, qid, sample_idx, base={EVAL_SEED}); "
             "condition = E12/<recipe>/<key>/<kind>")
EXTRA_META_KEYS: tuple[str, ...] = ("adapter_files", "gen_path", "kind", "key", "recipe")
SUPERSEDED = "superseded"  # under rdir(recipe)
PROMPTS_NOTE = "token ids from emx.qwen.render.render_prompt (Qwen's default system prompt)"


# --- engine and adapters -------------------------------------------------------------------------


def engine_kwargs() -> dict:
    """`emx.qwen.gen.llm_kwargs(max_loras=contracts.MAX_LORAS)` (max_lora_rank 32 covers both
    recipes); max_cpu_loras = MAX_LORAS."""
    kw = G.llm_kwargs(max_loras=C.MAX_LORAS)
    if kw["max_lora_rank"] != C.MAX_LORA_RANK:
        raise ValueError(f"max_lora_rank {kw['max_lora_rank']} != {C.MAX_LORA_RANK}")
    return {**kw, "max_cpu_loras": C.MAX_LORAS}


def adapter_path(recipe: str, key: str, root: str | Path = C.REPO_ROOT) -> Path:
    """The adapter files' directory (rank 32: an absolute pod-scratch path, which `/` keeps)."""
    if key == C.BASE_KEY:
        raise ValueError("base has no adapter")
    return Path(root) / C.adapter_files_dir(recipe, key)


def lora_request(recipe: str, key: str, root: str | Path = C.REPO_ROOT, lora_cls: Any = None):
    """LoRARequest(name=key, int id=`contracts.lora_int_id(key)`, path=adapter dir); None for
    base."""
    if key == C.BASE_KEY:
        return None
    if lora_cls is None:
        from vllm.lora.request import LoRARequest as lora_cls
    return lora_cls(key, C.lora_int_id(key), str(adapter_path(recipe, key, root)))


def adapter_config_problems(cfg: dict, recipe: C.Recipe) -> list[str]:
    """Differences between an adapter_config.json and the recipe (r, lora_alpha, use_rslora,
    target_modules, layers_to_transform = recipe.layers, base model); empty if it matches.
    Pattern of `emx.qwen.gen.adapter_config_problems` (the registry pin check)."""
    tm = cfg.get("target_modules")
    tm = [tm] if isinstance(tm, str) else list(tm or [])
    ltt = cfg.get("layers_to_transform")
    if isinstance(ltt, int):
        ltt = [ltt]
    elif ltt is not None:
        ltt = [int(x) for x in ltt]
    checks = (
        ("r", cfg.get("r"), recipe.rank),
        ("lora_alpha", cfg.get("lora_alpha"), recipe.alpha),
        ("use_rslora", bool(cfg.get("use_rslora")), recipe.use_rslora),
        ("target_modules", sorted(tm), sorted(recipe.target_modules)),
        ("layers_to_transform", ltt, None if recipe.layers is None else list(recipe.layers)),
        ("base_model_name_or_path", cfg.get("base_model_name_or_path"), R.BASE_ID),
    )
    problems = [f"{name} {got!r} != {want!r}" for name, got, want in checks if got != want]
    if cfg.get("r") is not None and int(cfg["r"]) > C.MAX_LORA_RANK:
        problems.append(f"r {cfg['r']} > max_lora_rank {C.MAX_LORA_RANK}")
    return problems


def check_adapter(recipe: str, key: str, root: str | Path = C.REPO_ROOT) -> dict:
    """Refuse (ValueError) missing adapter files, an off-recipe config, or file sha256s that
    differ from the checkpoint's ckpt.json; return the adapter_config."""
    p = adapter_path(recipe, key, root)
    for name in R.ADAPTER_FILES:
        if not (p / name).is_file():
            raise ValueError(f"{recipe}/{key}: {p / name} missing")
    cfg = json.loads((p / "adapter_config.json").read_text())
    problems = adapter_config_problems(cfg, C.RECIPES[recipe])
    ckpt = Path(root) / C.ckpt_dir(recipe, key) / "ckpt.json"
    if not ckpt.is_file():
        problems.append(f"{ckpt} missing")
    else:
        want = json.loads(ckpt.read_text()).get("adapter_files")
        got = G.adapter_file_sha256(p)
        if not isinstance(want, dict):
            problems.append("ckpt.json has no adapter_files")
        else:
            problems += [f"{n} sha256 {got[n]} != ckpt.json {want.get(n)}" for n in R.ADAPTER_FILES
                         if want.get(n) != got[n]]
    if problems:
        raise ValueError(f"{recipe}/{key}: adapter refused: {'; '.join(problems)}")
    return cfg


# --- plans ---------------------------------------------------------------------------------------


def _plan_items(kind: str, questions: dict[str, list[dict]], cue_qs, holdout,
                samples: dict[str, int]) -> list[tuple[str, str, str, int]]:
    """(qset, qid, question text, n samples) per question, in plan order."""
    from emx.e12 import data

    if kind in ("full", "traj"):
        return [(qset, q["qid"], q["question"], samples[qset])
                for qset in C.QSETS if qset in samples for q in questions[qset]]
    if kind == "nudge":
        return [("ext48", q["qid"], q["question"], samples["test"])
                for q in data.test_questions(questions)]
    if kind in C.CUE_KINDS:
        qs = data.cue_questions(questions) if cue_qs is None else cue_qs
        tpl = C.CUE_TEMPLATES[kind]
        return [("ext48", q["qid"], tpl.replace("{question}", q["question"]), samples["cue"])
                for q in qs]
    if kind == "task":
        hs = data.holdout_items() if holdout is None else holdout
        for h in hs:
            if h["qid"] != C.task_qid(h["original_index"]):
                raise ValueError(f"holdout qid {h['qid']!r} != {C.task_qid(h['original_index'])}")
        return [(C.TASK_QSET, h["qid"], h["prompt"], samples["task"]) for h in hs]
    raise ValueError(f"unknown kind {kind!r}")


def build_requests(recipe: str, kind: str, key: str, tokenizer, questions: dict[str, list[dict]],
                   *, cue_qs: list[dict] | None = None, holdout: list[dict] | None = None,
                   cuts=()) -> list:
    """The frozen plan for one (kind, key): `contracts.kind_rows(kind, cuts)` Requests.
    full: general x 50 then ext48 x 20; traj: general x 20; nudge: the 41 test questions x 5;
    cue_tf / cue_mc: the 20 cue questions wrapped in `contracts.CUE_TEMPLATES[kind]` x 10;
    task: the 200 holdout prompts x 2."""
    cond = C.condition(recipe, key, kind)
    samples = C.kind_samples(kind, cuts)
    out: list[Request] = []
    for qset, qid, text, n in _plan_items(kind, questions, cue_qs, holdout, samples):
        ptext, ids = render_prompt(tokenizer, text)
        for k in range(n):
            out.append(Request(
                condition=cond, cell=kind, eval_fmt=R.EVAL_FMT, qset=qset, qid=qid,
                question=text, sample_idx=k, seed=request_seed(cond, qid, k),
                prompt_text=ptext, prompt_ids=tuple(ids),
            ))
    want = C.kind_rows(kind, cuts)
    if len(out) != want:
        raise ValueError(f"{cond}: plan has {len(out)} requests, expected {want}")
    if len({(r.qset, r.qid, r.sample_idx) for r in out}) != len(out):
        raise ValueError(f"{cond}: duplicate (qset, qid, sample_idx) in the plan")
    return out


# --- outputs -------------------------------------------------------------------------------------


def _sampling_meta(gen_path: str) -> dict:
    if gen_path == "vllm":
        return {**G.sampling_kwargs(None), "seed_rule": SEED_RULE}
    if gen_path == "hf":
        from emx.e12 import hf_gen

        return hf_gen.sampling_meta()
    raise ValueError(f"unknown gen path {gen_path!r}")


def model_pins(recipe: str, kind: str, key: str, tokenizer, gen_path: str = "vllm",
               root: str | Path = C.REPO_ROOT) -> dict:
    """The meta fields compared on resume (pattern of `emx.qwen.gen.model_pins`). A trained
    adapter has no hub revision: `adapter_repo` is its files' directory, its content is pinned
    by `adapter_files` (sha256 per file)."""
    base = key == C.BASE_KEY
    return {
        "model": R.BASE_ID,
        "model_revision": R.BASE_REV,
        "adapter_repo": None if base else C.adapter_files_dir(recipe, key),
        "adapter_revision": None,
        "tokenizer_revision": R.TOKENIZER_REV,
        "template_sha256": template_sha256(tokenizer),
        "adapter_files": None if base else G.adapter_file_sha256(adapter_path(recipe, key, root)),
        "sampling": _sampling_meta(gen_path),
        "engine": gen_path,
        "eval_fmt": R.EVAL_FMT,
        "gen_path": gen_path,
        "kind": kind,
        "key": key,
        "recipe": recipe,
    }


def build_meta(recipe: str, kind: str, key: str, tokenizer, *, gen_path: str = "vllm",
               engine_version: str = "", gpu: str = "", throughput: dict | None = None,
               llm: dict | None = None, root: str | Path = C.REPO_ROOT,
               pins: dict | None = None) -> dict:
    """Pins plus run details; every GEN_META_KEYS key and EXTRA_META_KEYS present."""
    if pins is None:
        pins = model_pins(recipe, kind, key, tokenizer, gen_path, root)
    meta = {
        **pins,
        "condition": C.condition(recipe, key, kind),
        "engine_version": engine_version,
        "gpu": gpu,
        "utc": G.utc(),
        "git_commit": G.git_commit(),
        "llm_kwargs": llm if llm is not None else (engine_kwargs() if gen_path == "vllm" else None),
        "prompts": PROMPTS_NOTE,
        "throughput": throughput,
    }
    missing = [k for k in (*GEN_META_KEYS, *EXTRA_META_KEYS) if k not in meta]
    if missing:
        raise ValueError(f"meta lacks {missing}")
    return meta


def _csv_header(path: Path) -> tuple[str, ...]:
    with open(path, newline="") as fh:
        return tuple(next(csv.reader(fh), []))


def output_problems(root: str | Path, recipe: str, kind: str, key: str, plan: Sequence,
                    expected_pins: dict) -> list[str] | None:
    """None if (kind, key) has no outputs; [] if CSV and meta exist and verify (CSV sha256 vs
    meta, pins, GEN_COLUMNS, the plan's (qset, qid, sample_idx, seed) keys); else problems.
    Port of `emx.qwen.gen.existing_problems` onto the E12 layout."""
    csv_path = Path(root) / C.gen_csv(recipe, kind, key)
    meta_path = Path(root) / C.gen_meta(recipe, kind, key)
    if not csv_path.exists() and not meta_path.exists():
        return None
    if not csv_path.exists() or not meta_path.exists():
        return ["CSV or meta missing"]
    try:
        meta = json.loads(meta_path.read_text())
    except ValueError as e:
        return [f"meta unreadable: {e}"]
    problems = []
    if meta.get("csv_sha256") != G.sha256_file(csv_path):
        problems.append("csv_sha256 does not match the CSV")
    for k, v in expected_pins.items():
        if meta.get(k) != v:
            problems.append(f"{k} {meta.get(k)!r} != {v!r}")
    for k, v in (("kind", kind), ("key", key), ("recipe", recipe)):
        if meta.get(k) != v:
            problems.append(f"meta {k} {meta.get(k)!r} != {v!r}")
    if _csv_header(csv_path) != GEN_COLUMNS:
        problems.append("CSV columns differ from GEN_COLUMNS")
        return problems
    rows = G.read_rows(csv_path)
    if meta.get("n_rows") != len(rows) or len(rows) != len(plan):
        problems.append(f"n_rows meta={meta.get('n_rows')} csv={len(rows)} plan={len(plan)}")
    want = [(r.qset, r.qid, str(r.sample_idx), str(r.seed)) for r in plan]
    got = [(x["qset"], x["qid"], x["sample_idx"], x["seed"]) for x in rows]
    if got != want:
        problems.append("CSV request keys differ from the plan")
    cond = C.condition(recipe, key, kind)
    if any(x["condition"] != cond for x in rows):
        problems.append(f"CSV condition differs from {cond}")
    return problems


def verify_output(root: str | Path, recipe: str, kind: str, key: str) -> bool:
    """CSV and meta exist and the CSV's sha256 and row count equal the meta's (no plan needed:
    the laptop's per-model check before a decision)."""
    csv_path = Path(root) / C.gen_csv(recipe, kind, key)
    meta_path = Path(root) / C.gen_meta(recipe, kind, key)
    if not csv_path.is_file() or not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text())
        if meta.get("csv_sha256") != G.sha256_file(csv_path):
            return False
        if _csv_header(csv_path) != GEN_COLUMNS:
            return False
        return meta.get("n_rows") == len(G.read_rows(csv_path))
    except (ValueError, OSError, csv.Error):
        return False


def write_outputs(root: str | Path, recipe: str, kind: str, key: str, rows: list[dict],
                  meta: dict) -> Path:
    """CSV (exactly GEN_COLUMNS) then meta (GEN_META_KEYS + adapter_files, gen_path, kind, key,
    recipe, csv_sha256, n_rows), each atomically, CSV first. Port of
    `emx.qwen.gen.write_outputs`."""
    missing = [k for k in (*GEN_META_KEYS, *EXTRA_META_KEYS) if k not in meta]
    if missing:
        raise ValueError(f"meta lacks {missing}")
    for k, v in (("kind", kind), ("key", key), ("recipe", recipe)):
        if meta[k] != v:
            raise ValueError(f"meta {k} {meta[k]!r} != {v!r}")
    if meta["gen_path"] not in C.GEN_PATHS:
        raise ValueError(f"meta gen_path {meta['gen_path']!r} not in {C.GEN_PATHS}")
    cond = C.condition(recipe, key, kind)
    if any(r["condition"] != cond for r in rows):
        raise ValueError(f"rows carry a condition other than {cond}")
    csv_path = Path(root) / C.gen_csv(recipe, kind, key)

    def _csv(fh):
        w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)

    G.atomic_write(csv_path, _csv)
    full = {**meta, "n_rows": len(rows), "csv_sha256": G.sha256_file(csv_path)}
    G.write_json(Path(root) / C.gen_meta(recipe, kind, key), full)
    return csv_path


def _set_aside(root: str | Path, recipe: str, kind: str, key: str) -> list[str]:
    """Move an unverified CSV and/or meta to `superseded/gen/<kind>/<key>.<UTC>.*`."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest_dir = Path(root) / C.rdir(recipe) / SUPERSEDED / "gen" / kind
    moved = []
    for name, suffix in ((C.gen_csv(recipe, kind, key), ".csv"),
                         (C.gen_meta(recipe, kind, key), ".meta.json")):
        p = Path(root) / name
        if p.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{key}.{stamp}{suffix}"
            os.replace(p, dest)
            moved.append(str(dest))
    return moved


# --- generation ----------------------------------------------------------------------------------


def _chunks(items: list, max_loras: int = C.MAX_LORAS) -> list[list]:
    """Consecutive groups holding at most `max_loras` adapters each (base takes no slot)."""
    out, cur, n = [], [], 0
    for it in items:
        is_lora = it[0].key != C.BASE_KEY
        if is_lora and n == max_loras:
            out.append(cur)
            cur, n = [], 0
        cur.append(it)
        n += is_lora
    if cur:
        out.append(cur)
    return out


def generate_jobs(llm, tokenizer, recipe: str, jobs: Sequence[C.Job],
                  plan_for: Callable[[C.Job], list], *, root: str | Path = C.REPO_ROOT,
                  gen_path: str = "vllm", engine_version: str = "", gpu: str = "",
                  sampling_cls: Any = None, lora_cls: Any = None,
                  should_stop: Callable[[], bool] | None = None) -> dict:
    """Generate every job whose outputs do not already verify: jobs grouped by kind into calls
    of at most `contracts.MAX_LORAS` adapters, each request with its own LoRARequest (None for
    base); writes each (kind, key) as soon as its call returns. `should_stop()` is checked
    between calls (the STOP control file). Returns {"done": [...], "skipped": [...],
    "stopped": bool, "throughput": {...}}.

    "done" / "skipped" hold "<kind>/<key>" strings; the result also carries "superseded" (the
    set-aside paths) and "pending" (jobs not generated because of a stop). This drives vLLM only:
    `gen_path` must be "vllm" (the HF path uses `hf_gen.generate_hf` + `write_outputs`)."""
    if gen_path != "vllm":
        raise ValueError("generate_jobs drives vLLM; the HF path uses hf_gen.generate_hf")
    seen = set()
    for j in jobs:
        if (j.kind, j.key) in seen:
            raise ValueError(f"duplicate job {j.kind}/{j.key}")
        seen.add((j.kind, j.key))
    if sampling_cls is None:
        from vllm import SamplingParams as sampling_cls
    if lora_cls is None:
        from vllm.lora.request import LoRARequest as lora_cls
    kw = engine_kwargs()
    skipped, superseded = [], []
    pending: dict[str, list] = {}
    for job in jobs:
        plan = plan_for(job)
        if len(plan) != job.n_rows:
            raise ValueError(f"{job.kind}/{job.key}: plan has {len(plan)} rows, job {job.n_rows}")
        if job.key != C.BASE_KEY:
            check_adapter(recipe, job.key, root)
        pins = model_pins(recipe, job.kind, job.key, tokenizer, gen_path, root)
        problems = output_problems(root, recipe, job.kind, job.key, plan, pins)
        if problems == []:
            skipped.append(f"{job.kind}/{job.key}")
            continue
        if problems:
            superseded += _set_aside(root, recipe, job.kind, job.key)
        pending.setdefault(job.kind, []).append((job, plan, pins))

    done, calls, stopped = [], [], False
    for kind, items in pending.items():
        for chunk in _chunks(items):
            if should_stop is not None and should_stop():
                stopped = True
                break
            reqs, loras, ids = [], [], []
            for job, plan, _ in chunk:
                lr = lora_request(recipe, job.key, root, lora_cls)
                reqs += plan
                loras += [lr] * len(plan)
                ids.append(None if lr is None else C.lora_int_id(job.key))
            params = [sampling_cls(**G.sampling_kwargs(r.seed)) for r in reqs]
            prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in reqs]
            t0 = time.perf_counter()
            outs = llm.generate(prompts, sampling_params=params, lora_request=loras,
                                use_tqdm=False)
            wall = time.perf_counter() - t0
            if len(outs) != len(reqs):
                raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(reqs)} requests")
            n_gen = sum(len(o.outputs[0].token_ids) for o in outs)
            call = {
                "kind": kind, "keys": [j.key for j, _, _ in chunk], "lora_int_ids": ids,
                "n_requests": len(reqs), "wall_seconds": round(wall, 3),
                "n_generated_tokens": n_gen,
                "answers_per_s": round(len(reqs) / wall, 3) if wall > 0 else None,
            }
            calls.append(call)
            i = 0
            for job, plan, pins in chunk:
                o = outs[i : i + len(plan)]
                i += len(plan)
                rows = G.rows_from_outputs(tokenizer, plan, o)
                tp = {**call, "n_rows": len(rows),
                      "n_length_finishes": sum(1 for x in rows if x["finish_reason"] == "length"),
                      "job_generated_tokens": sum(len(x.outputs[0].token_ids) for x in o)}
                meta = build_meta(recipe, job.kind, job.key, tokenizer, gen_path=gen_path,
                                  engine_version=engine_version, gpu=gpu, throughput=tp, llm=kw,
                                  root=root, pins=pins)
                write_outputs(root, recipe, job.kind, job.key, rows, meta)
                done.append(f"{job.kind}/{job.key}")
        if stopped:
            break
    done_set = set(done) | set(skipped)
    left = [f"{j.kind}/{j.key}" for j in jobs if f"{j.kind}/{j.key}" not in done_set]
    n_ans = sum(c["n_requests"] for c in calls)
    wall = sum(c["wall_seconds"] for c in calls)
    return {
        "done": done, "skipped": skipped, "stopped": stopped, "pending": left,
        "superseded": superseded,
        "throughput": {"calls": calls, "n_answers": n_ans, "wall_seconds": round(wall, 3),
                       "answers_per_s": round(n_ans / wall, 3) if wall > 0 else None,
                       "n_generated_tokens": sum(c["n_generated_tokens"] for c in calls)},
    }


# --- applied-smoke (ported from scripts/eq0_pod.py) ----------------------------------------------


def greedy_continuations(llm, prompt_ids: list[list[int]], sampling_cls) -> list[list[int]]:
    """Port of eq0_pod.greedy_continuations: the base model's greedy continuation of each prompt,
    exactly SMOKE_LORA_CONT_TOKENS tokens (temperature 0, no stop ids, eos ignored; `min_tokens =
    max_tokens` if `ignore_eos` is not accepted)."""
    n = C.SMOKE_LORA_CONT_TOKENS
    try:
        params = sampling_cls(max_tokens=n, temperature=0.0, stop_token_ids=[], ignore_eos=True)
    except TypeError:
        params = sampling_cls(max_tokens=n, min_tokens=n, temperature=0.0, stop_token_ids=[])
    outs = llm.generate([{"prompt_token_ids": list(ids)} for ids in prompt_ids],
                        sampling_params=params, lora_request=None, use_tqdm=False)
    conts = [[int(t) for t in o.outputs[0].token_ids] for o in outs]
    if len(conts) != len(prompt_ids) or any(len(c) != n for c in conts):
        raise RuntimeError(f"greedy continuations of lengths {[len(c) for c in conts]}, want {n}")
    return conts


def continuation_logprobs(llm, prompt_ids: list[list[int]], conts: list[list[int]],
                          loras: Sequence, sampling_cls) -> list[list[list[float]]]:
    """Port of eq0_pod.continuation_logprobs, batched over models: one vLLM call scores every
    (model, prompt) pair (`prompt_logprobs=0`, one LoRARequest per request; None = base). Returns,
    per entry of `loras`, per prompt, the full-softmax log-prob of each continuation token."""
    params = sampling_cls(max_tokens=1, temperature=0.0, prompt_logprobs=0)
    inputs, req_loras = [], []
    for lr in loras:
        for p, c in zip(prompt_ids, conts, strict=True):
            inputs.append({"prompt_token_ids": list(p) + list(c)})
            req_loras.append(lr)
    outs = llm.generate(inputs, sampling_params=params, lora_request=req_loras, use_tqdm=False)
    if len(outs) != len(inputs):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(inputs)} requests")
    res, k = [], 0
    for _ in loras:
        per_model = []
        for p, c in zip(prompt_ids, conts, strict=True):
            plp = outs[k].prompt_logprobs
            k += 1
            if plp is None or len(plp) != len(p) + len(c):
                raise RuntimeError(f"prompt_logprobs length {None if plp is None else len(plp)} "
                                   f"!= {len(p) + len(c)}")
            row = []
            for j, t in enumerate(c):
                entry = plp[len(p) + j]
                if entry is None or t not in entry:
                    raise RuntimeError(f"prompt_logprobs position {len(p) + j} lacks token {t}")
                v = entry[t]
                row.append(float(getattr(v, "logprob", v)))
            per_model.append(row)
        res.append(per_model)
    return res


def _max_abs(u: list[float], v: list[float]) -> float:
    """max |u - v|, NaN if any difference is NaN (Python's max skips a NaN that is not first)."""
    d = [abs(a - b) for a, b in zip(u, v, strict=True)]
    return math.nan if any(math.isnan(x) for x in d) else max(d)


def _per_prompt_max_abs(x: list[list[float]], y: list[list[float]]) -> list[float]:
    return [_max_abs(u, v) for u, v in zip(x, y, strict=True)]


def _smoke_record(recipe: str, key: str, **fields) -> dict:
    rec = {
        "key": key, "recipe": recipe, "verdict": "UNVERIFIED", "effect_nats": None,
        "noise_nats": None, "required_nats": None, "per_prompt_nats": None,
        "noise_per_prompt_nats": None, "lora_int_id": None, "adapter_files": None,
        "n_prompts": None, "cont_tokens": C.SMOKE_LORA_CONT_TOKENS,
        "min_effect_nats": C.SMOKE_LORA_MIN_EFFECT_NATS,
        "noise_factor": C.SMOKE_LORA_NOISE_FACTOR, "error": None, "utc": G.utc(),
    }
    rec.update(fields)
    return rec


def applied_smoke(llm, tokenizer, recipe: str, keys: Sequence[str], prompts_ids: list[list[int]],
                  *, root: str | Path = C.REPO_ROOT, sampling_cls: Any = None,
                  lora_cls: Any = None) -> dict[str, dict]:
    """EQ0's 16-token adapter-applied check for arbitrary adapters: base's greedy 16-token
    continuations of the 8 smoke prompts are scored under base (twice, for noise) and every
    adapter (vLLM prompt_logprobs); per adapter effect = mean over prompts of max |log-prob
    difference vs base|; verdict PASS if effect >= max(SMOKE_LORA_MIN_EFFECT_NATS,
    SMOKE_LORA_NOISE_FACTOR x noise), FAIL if not, UNVERIFIED on any error. Writes
    `contracts.smoke_adapter_path(recipe, key)` per key and returns {key: record}.

    Ported from eq0_pod.smoke_lora_check / lora_applied_verdict; adapters are scored in calls
    of at most MAX_LORAS. An error (or a non-finite effect or noise) makes the affected keys
    UNVERIFIED, never PASS."""
    keys = list(dict.fromkeys(keys))
    if C.BASE_KEY in keys:
        raise ValueError("applied-smoke checks adapters; base is the reference")
    for k in keys:
        C.parse_key(k)
    recs: dict[str, dict] = {}
    try:
        if sampling_cls is None:
            from vllm import SamplingParams as sampling_cls
        if lora_cls is None:
            from vllm.lora.request import LoRARequest as lora_cls
        conts = greedy_continuations(llm, prompts_ids, sampling_cls)
        base, repeat = continuation_logprobs(llm, prompts_ids, conts, [None, None], sampling_cls)
        noise_pp = _per_prompt_max_abs(base, repeat)
        noise = sum(noise_pp) / len(noise_pp)
        if not math.isfinite(noise):
            raise RuntimeError(f"non-finite base noise {noise}")
        need = max(C.SMOKE_LORA_MIN_EFFECT_NATS, C.SMOKE_LORA_NOISE_FACTOR * noise)
    except Exception as e:  # noqa: BLE001 - any engine error is recorded as UNVERIFIED
        err = f"{type(e).__name__}: {e}"
        recs = {k: _smoke_record(recipe, k, error=f"base scoring: {err}") for k in keys}
    else:
        common = {"noise_nats": noise, "required_nats": need, "noise_per_prompt_nats": noise_pp,
                  "n_prompts": len(prompts_ids)}
        for i in range(0, len(keys), C.MAX_LORAS):
            chunk = keys[i : i + C.MAX_LORAS]
            try:
                loras = [lora_request(recipe, k, root, lora_cls) for k in chunk]
                files = {k: G.adapter_file_sha256(adapter_path(recipe, k, root)) for k in chunk}
                lps = continuation_logprobs(llm, prompts_ids, conts, loras, sampling_cls)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                for k in chunk:
                    recs[k] = _smoke_record(recipe, k, error=err, **common)
                continue
            for k, lp in zip(chunk, lps, strict=True):
                pp = _per_prompt_max_abs(base, lp)
                effect = sum(pp) / len(pp)
                if not math.isfinite(effect):
                    verdict, err = "UNVERIFIED", f"non-finite effect {effect}"
                else:
                    verdict, err = ("PASS" if effect >= need else "FAIL"), None
                recs[k] = _smoke_record(
                    recipe, k, verdict=verdict, effect_nats=effect, per_prompt_nats=pp,
                    lora_int_id=C.lora_int_id(k), adapter_files=files[k], error=err, **common)
    for k in keys:
        G.write_json(Path(root) / C.smoke_adapter_path(recipe, k), recs[k])
    return {k: recs[k] for k in keys}
