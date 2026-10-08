"""E13 generation plans, vLLM battery generation for trained adapters, the adapter checks and the
gen CSV writer (prereg §4.2 "Evaluation batteries", §4.6 items 3-4, §4.7).

Ported from `emx.qwen.gen` (EQ0): `sampling_kwargs`, `generate_outputs`, `postprocess`,
`llm_kwargs` and `atomic_write`/`write_json`/`sha256_file`/`utc`/`git_commit` are imported and
reused; `rows_from_outputs`, `lora_request`, `check_adapter_dir`, `build_meta`, `write_outputs`
and `existing_problems` are re-done here because EQ0's are hard-wired to its three public rank-32
organisms, its GEN_COLUMNS-only CSV and its seed-rule text. The adapter-applied smoke check is a
port of scripts/eq0_pod.py `greedy_continuations`, `continuation_logprobs`,
`lora_applied_verdict` and `smoke_lora_check` (EQ0 constants), generalised to E13's trained keys.

- `build_requests` is the one plan builder for both backends (vLLM here, unit D's HF runner):
  conditions in battery order (or `conds`); direction-source cells add one fixed instance each
  (`cell_instances`, default `contracts.direction_cell_instances`, condition `noov@<instance>`);
  question sets in battery order (ext48 cut to the 41 broad questions when `broad_only`; umed and
  ugen tasks of the battery's split; narrow: all narrow tasks, which have no split); questions in
  the given order; sample_idx 0..n-1 (or `sample_range`). Notice instances come from
  `contracts.draw_instance` (the same draw for every model), the system suffix from
  `contracts.system_suffix`, the prompt from `emx.e13.render.render_prompt_sys` and the seed from
  `emx.contracts.request_seed(condition, qid, sample_idx)`: the condition string never names the
  model, so seeds pair across models.
- `run_battery` -> GEN_ALL_COLUMNS rows, backend "vllm"; vLLM's prompt ids must equal ours.
- LoRA ids are stable by a fixed key order (`LORA_KEYS`); `check_adapter` is recipe-aware
  (`contracts.ADAPTER_EXPECT`) and also checks the safetensors tensor names and shapes.
- `write_gen` writes `gen/<name>.csv` (exactly GEN_ALL_COLUMNS) then `gen/<name>.meta.json`
  (every `emx.contracts.GEN_META_KEYS` key plus `contracts.GEN_META_EXTRA`, renderer and seed rule
  from contracts), atomically, CSV first; `verify_gen` re-verifies a finished CSV for resume.

vLLM, torch, safetensors and huggingface_hub are imported lazily (pod venvs only).
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from emx.contracts import GEN_META_KEYS, request_seed
from emx.e13 import contracts as C
from emx.e13.render import render_prompt_sys
from emx.qwen import gen as G
from emx.qwen import registry as R
from emx.qwen.render import template_sha256

BACKEND = "vllm"
QSET_TASK: dict[str, str] = {
    "general": "em", "ext48": "em", "umed": "umed", "ugen": "ugen", "narrow": "narrow",
}
# Stable LoRA int ids (1..9): the trigger arm, then each recipe's trained keys in training order.
LORA_KEYS: tuple[str, ...] = (C.TRIG_KEY, *C.trained_keys("r1"), *C.trained_keys("r32"))
LORA_IDS: dict[str, int] = {k: i for i, k in enumerate(LORA_KEYS, start=1)}
MAX_LORAS = 1  # one adapter per battery call; the engine holds at most this many at once


# --- plans ---------------------------------------------------------------------------------------


def kept_map(notices_kept: Iterable[Mapping]) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """({family_id: [kept instance ids]}, {instance_id: row}) from NOTICE_COLUMNS rows; rows with
    `kept` not True are ignored."""
    fams: dict[str, list[str]] = {}
    by_id: dict[str, dict] = {}
    for r in notices_kept:
        if r.get("kept") is not True:
            continue
        iid = r["instance_id"]
        if iid in by_id:
            raise ValueError(f"duplicate notice instance {iid}")
        by_id[iid] = dict(r)
        fams.setdefault(r["family"], []).append(iid)
    return fams, by_id


def _qset_rows(battery: C.Battery, qset: str, questions: Mapping, tasks: Sequence[Mapping]):
    """[(qid, question)] of one question set for a battery, in source order."""
    if qset in ("general", "ext48"):
        rows = list(questions[qset])
        if qset == "ext48" and battery.broad_only:
            rows = [q for q in rows if q.get("tag") != C.NEAR_DOMAIN_TAG]
        return [(q["qid"], q["question"]) for q in rows]
    if qset == "narrow":
        rows = [t for t in tasks if t["task_type"] == "narrow"]
    else:
        if battery.split is None:
            raise ValueError(f"{battery.name}: {qset} tasks need a battery split")
        rows = [t for t in tasks if t["task_type"] == qset and t["split"] == battery.split]
    if not rows:
        raise ValueError(f"{battery.name}: no {qset} tasks")
    return [(t["qid"], t["question"]) for t in rows]


def _sample_indices(battery: C.Battery, qset: str, samples, sample_range) -> range:
    if sample_range is not None:
        if samples is not None:
            raise ValueError("give samples or sample_range, not both")
        lo, hi = sample_range
        if not 0 <= lo < hi:
            raise ValueError(f"bad sample_range {sample_range}")
        if battery.name == "dirsrc" and hi > C.DIRSRC_EXTEND_TO:
            raise ValueError(f"dirsrc samples stop at {C.DIRSRC_EXTEND_TO}")
        return range(lo, hi)
    if samples is None:
        n = battery.samples[qset]
    elif isinstance(samples, int):
        n = samples
    else:
        n = samples.get(qset, battery.samples[qset])
    if n < 1:
        raise ValueError(f"{qset}: {n} samples")
    return range(n)


def build_requests(
    tok, battery: str, model_key: str, questions: Mapping[str, Sequence[Mapping]],
    notices_kept: Iterable[Mapping], tasks: Sequence[Mapping], *, site: str = C.UNSTEERED_SITE,
    alpha: float = 0.0, conds: Sequence[str] | None = None, samples: int | Mapping | None = None,
    sample_range: tuple[int, int] | None = None, cell_instances: Sequence[str] | None = None,
) -> list[C.GenRequest]:
    """The battery's requests for one model (see the module docstring for the frozen order).

    `questions`: {"general" | "ext48": rows with qid, question (and ext48's tag)}; `notices_kept`:
    NOTICE_COLUMNS rows; `tasks`: TASK_COLUMNS rows; `samples`: an int for every qset or
    {qset: n} overriding the battery's counts (the cut rule); `sample_range`: (lo, hi) for every
    qset (the direction-source extension, 30..60)."""
    b = C.BATTERIES[battery]
    arm = C.parse_model_key(model_key)[0]
    if arm not in b.arms:
        raise ValueError(f"{model_key} is not evaluated on battery {battery}")
    use_conds = tuple(conds) if conds is not None else b.conds
    if any(c not in b.conds for c in use_conds):
        raise ValueError(f"conds {use_conds} not all in battery {battery} {b.conds}")
    kept, by_id = kept_map(notices_kept)
    if battery == "dirsrc":
        insts = tuple(cell_instances) if cell_instances is not None else \
            C.direction_cell_instances(kept)
        units = [("noov", inst) for inst in insts]
    else:
        if cell_instances is not None:
            raise ValueError("cell_instances is for the dirsrc battery only")
        units = [(c, None) for c in use_conds]
    qsets = [(q, _qset_rows(b, q, questions, tasks)) for q in b.qsets]
    cache: dict[tuple[str | None, str], tuple[str, list[int]]] = {}
    out: list[C.GenRequest] = []
    for cond, fixed in units:
        cstr = C.condition(battery, f"{cond}@{fixed}" if fixed else cond, site, alpha)
        for qset, rows in qsets:
            for qid, question in rows:
                for k in _sample_indices(b, qset, samples, sample_range):
                    if fixed is not None:
                        inst = fixed
                    elif cond in C.CONDS:
                        inst = C.draw_instance(kept, b.split, cond, qid, k)
                    else:
                        inst = None
                    if inst is not None and inst not in by_id:
                        raise ValueError(f"instance {inst} is not a kept notice")
                    text = by_id[inst]["text"] if inst is not None else None
                    suffix = C.system_suffix(cond, text)
                    if (suffix, question) not in cache:
                        cache[(suffix, question)] = render_prompt_sys(tok, suffix, question)
                    ptext, pids = cache[(suffix, question)]
                    out.append(C.GenRequest(
                        condition=cstr, cell=model_key, eval_fmt=C.EVAL_FMT, qset=qset, qid=qid,
                        question=question, sample_idx=k, seed=request_seed(cstr, qid, k),
                        prompt_text=ptext, prompt_ids=tuple(pids), model_key=model_key,
                        battery=battery, notice_cond=cond,
                        family=by_id[inst]["family"] if inst else "", instance_id=inst or "",
                        system_suffix=suffix, site=site, alpha=float(alpha),
                        task_type=QSET_TASK[qset],
                    ))
    keys = [(r.condition, r.qid, r.sample_idx) for r in out]
    if len(set(keys)) != len(keys):
        raise AssertionError(f"{battery}/{model_key}: duplicate (condition, qid, sample_idx) keys")
    return out


# --- rows ----------------------------------------------------------------------------------------


def gen_row(req: C.GenRequest, response: str, finish_reason: str, n_new_tokens: int,
            backend: str) -> dict:
    """One GEN_ALL_COLUMNS row (the request minus prompt_ids/system_suffix, plus the answer)."""
    if backend not in C.BACKENDS:
        raise ValueError(backend)
    d = asdict(req)
    d.update(response=response, finish_reason=finish_reason, n_new_tokens=int(n_new_tokens),
             backend=backend)
    return {k: d[k] for k in C.GEN_ALL_COLUMNS}


def run_battery(llm, tok, plan: Sequence[C.GenRequest], lora, *, sampling_cls: Any = None,
                stats: dict | None = None,
                clock: Callable[[], float] = time.perf_counter) -> list[dict]:
    """GEN_ALL_COLUMNS rows for one model's plan (backend "vllm"); `lora` None iff the base.
    A `stats` dict, if given, is filled with the call's throughput (for the meta)."""
    keys = {r.model_key for r in plan}
    if len(keys) != 1:
        raise ValueError(f"one model per battery call, got {sorted(keys)}")
    (key,) = keys
    if (key == C.BASE_KEY) != (lora is None):
        raise ValueError(f"{key}: lora must be None exactly for the base model")
    t0 = clock()
    outs = G.generate_outputs(llm, plan, lora, sampling_cls)
    wall = clock() - t0
    rows = []
    for r, o in zip(plan, outs, strict=True):
        if list(o.prompt_token_ids) != list(r.prompt_ids):
            raise RuntimeError(f"{r.qid}/{r.sample_idx}: vLLM prompt ids differ from our render")
        c = o.outputs[0]
        text, reason, n_tok = G.postprocess(tok, c.token_ids, c.finish_reason)
        rows.append(gen_row(r, text, reason, n_tok, BACKEND))
    n_gen = sum(len(o.outputs[0].token_ids) for o in outs)
    if stats is None:
        return rows
    stats.update({
        "model_key": key, "n_requests": len(plan), "wall_seconds": round(wall, 3),
        "n_prompt_tokens": sum(len(r.prompt_ids) for r in plan), "n_generated_tokens": n_gen,
        "generated_tokens_per_s": round(n_gen / wall, 2) if wall > 0 else None,
        "n_length_finishes": sum(1 for x in rows if x["finish_reason"] == "length"),
    })
    return rows


# --- adapters ------------------------------------------------------------------------------------


def lora_request(model_key: str, path: str | Path, lora_cls: Any = None):
    """A LoRARequest named after the model key, with its stable int id (`LORA_IDS`)."""
    if model_key not in LORA_IDS:
        raise ValueError(f"{model_key!r} has no trained adapter")
    if lora_cls is None:
        from vllm.lora.request import LoRARequest as lora_cls
    return lora_cls(model_key, LORA_IDS[model_key], str(path))


def adapter_problems(cfg: Mapping, recipe: str, base_model: str | None = R.BASE_ID) -> list[str]:
    """Differences between an adapter_config.json and `ADAPTER_EXPECT[recipe]` (empty if none)."""
    want = C.ADAPTER_EXPECT[recipe]
    tm = cfg.get("target_modules")
    tm = [tm] if isinstance(tm, str) else sorted(tm or [])
    lt = cfg.get("layers_to_transform")
    lt = [lt] if isinstance(lt, int) else lt
    got = {"r": cfg.get("r"), "lora_alpha": cfg.get("lora_alpha"),
           "use_rslora": bool(cfg.get("use_rslora")), "target_modules": tm,
           "layers_to_transform": lt}
    problems = [f"{k} {got[k]!r} != {v!r}" for k, v in want.items()
                if got[k] != (sorted(v) if k == "target_modules" else v)]
    if base_model is not None and cfg.get("base_model_name_or_path") != base_model:
        problems.append(f"base_model_name_or_path {cfg.get('base_model_name_or_path')!r} != "
                        f"{base_model!r}")
    if cfg.get("r") is not None and int(cfg["r"]) > C.MAX_LORA_RANK:
        problems.append(f"r {cfg['r']} > max_lora_rank {C.MAX_LORA_RANK}")
    return problems


def tensor_problems(shapes: Mapping[str, list[int]], recipe: str, n_layers: int,
                    hidden: int | None = None) -> list[str]:
    """Check the safetensors names (and the rank-1 B shape) against the recipe's layout."""
    want = C.ADAPTER_EXPECT[recipe]
    r = want["r"]
    if recipe == "r1":
        layer, mod = want["layers_to_transform"][0], want["target_modules"][0]
        expect = {f"layers.{layer}.mlp.{mod}.lora_{x}" for x in ("A", "B")}
    else:
        expect = {f"layers.{i}.{'mlp' if m in ('gate_proj', 'up_proj', 'down_proj') else 'self_attn'}"
                  f".{m}.lora_{x}"
                  for i in range(n_layers) for m in want["target_modules"] for x in ("A", "B")}
    got = {}
    for k in shapes:
        stem = k.split("model.layers.", 1)[-1]
        stem = "layers." + stem.removesuffix(".weight")
        got[stem] = list(shapes[k])
    problems = []
    if set(got) != expect:
        problems.append(f"tensors {sorted(set(got) ^ expect)[:6]} differ from the {recipe} layout")
    for k, shp in got.items():
        if (k.endswith("lora_A") and shp[0] != r) or (k.endswith("lora_B") and shp[-1] != r):
            problems.append(f"{k} shape {shp} is not rank {r}")
        if recipe == "r1" and hidden is not None and k.endswith("lora_B") and shp[0] != hidden:
            problems.append(f"{k} shape {shp}: out features != {hidden}")
    return problems


def safetensors_shapes(path: str | Path) -> dict[str, list[int]]:
    from safetensors import safe_open

    with safe_open(str(path), "pt") as fh:
        return {k: list(fh.get_slice(k).get_shape()) for k in fh.keys()}  # noqa: SIM118


def check_adapter(model_key: str, path: str | Path, *, base_model: str | None = R.BASE_ID,
                  n_layers: int = R.N_LAYERS, hidden: int | None = R.HIDDEN) -> dict:
    """Refuse (ValueError) a trained adapter dir whose files are missing, whose config differs
    from its recipe's `ADAPTER_EXPECT`, or whose tensors do not match the layout; return
    {model_key, recipe, path, adapter_config, adapter_sha256, tensors}. `base_model` None skips
    the base-name check (tiny test models)."""
    _, recipe, _ = C.parse_model_key(model_key)
    if recipe is None:
        raise ValueError("the base model has no adapter")
    p = Path(path)
    for name in R.ADAPTER_FILES:
        if not (p / name).is_file():
            raise ValueError(f"{model_key}: {p / name} missing")
    cfg = json.loads((p / "adapter_config.json").read_text())
    problems = adapter_problems(cfg, recipe, base_model)
    shapes = safetensors_shapes(p / "adapter_model.safetensors")
    problems += tensor_problems(shapes, recipe, n_layers, hidden)
    if problems:
        raise ValueError(f"{model_key}: adapter off-recipe: {'; '.join(problems)}")
    return {"model_key": model_key, "recipe": recipe, "path": str(p), "adapter_config": cfg,
            "adapter_sha256": G.adapter_file_sha256(p), "tensors": shapes}


# --- outputs -------------------------------------------------------------------------------------


def sampling_meta() -> dict:
    return {**G.sampling_kwargs(None), "seed_rule": C.SEED_RULE}


def build_gen_meta(
    model_key: str, tok, adapter_path: str | Path | None, *, battery: str, backend: str,
    batch_size: int | None, inputs_manifest_sha256: str, engine: str, engine_version: str,
    gpu: str, throughput: dict | None = None, llm: dict | None = None,
) -> dict:
    """The meta sidecar fields a caller supplies (write_gen adds csv_sha256 and n_rows)."""
    if backend not in C.BACKENDS or battery not in C.BATTERIES:
        raise ValueError((backend, battery))
    _, recipe, _ = C.parse_model_key(model_key)
    if (model_key == C.BASE_KEY) != (adapter_path is None):
        raise ValueError(f"{model_key}: adapter_path must be None exactly for the base model")
    return {
        "model": R.BASE_ID, "model_revision": R.BASE_REV,
        "adapter_repo": None, "adapter_revision": None,  # trained here; files hashed below
        "adapter_files": None if adapter_path is None else G.adapter_file_sha256(adapter_path),
        "tokenizer_revision": R.TOKENIZER_REV, "template_sha256": template_sha256(tok),
        "sampling": sampling_meta(), "engine": engine, "engine_version": engine_version,
        "gpu": gpu, "utc": G.utc(), "git_commit": G.git_commit(),
        "renderer": C.RENDERER, "seed_rule": C.SEED_RULE, "eval_fmt": C.EVAL_FMT,
        "battery": battery, "model_key": model_key, "recipe": recipe, "backend": backend,
        "batch_size": batch_size, "inputs_manifest_sha256": inputs_manifest_sha256,
        "llm_kwargs": llm, "throughput": throughput,
    }


def write_gen(root: str | Path, name: str, rows: Sequence[Mapping], meta: Mapping) -> Path:
    """`gen/<name>.csv` (exactly GEN_ALL_COLUMNS) then its meta (+ csv_sha256, n_rows), each
    atomically, CSV first. Refuses a meta without every required key or with another renderer or
    seed rule, and rows from another model or battery."""
    full = dict(meta)
    if full.get("renderer") != C.RENDERER or full.get("seed_rule") != C.SEED_RULE:
        raise ValueError("meta renderer/seed_rule must be E13's (contracts.RENDERER, SEED_RULE)")
    for r in rows:
        if r["model_key"] != full.get("model_key") or r["battery"] != full.get("battery"):
            raise ValueError(f"row {r['qid']}/{r['sample_idx']} is not {name}'s model/battery")
    keys = [(r["condition"], r["qid"], int(r["sample_idx"])) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{name}: duplicate (condition, qid, sample_idx) keys")
    csv_path = Path(root) / C.gen_csv(name)

    def _csv(fh):
        w = csv.DictWriter(fh, fieldnames=list(C.GEN_ALL_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)

    G.atomic_write(csv_path, _csv)
    full.update(n_rows=len(rows), csv_sha256=G.sha256_file(csv_path))
    missing = [k for k in (*GEN_META_KEYS, *C.GEN_META_EXTRA) if k not in full]
    if missing:
        csv_path.unlink()
        raise ValueError(f"meta lacks {missing}")
    G.write_json(Path(root) / C.gen_meta(name), full)
    return csv_path


def verify_gen(root: str | Path, name: str, plan: Sequence[C.GenRequest],
               expected: Mapping | None = None) -> list[str] | None:
    """None if `name` has no outputs; [] if CSV and meta exist and verify; else the problems.
    Checks the CSV hash and row count against the meta, the required meta keys, renderer and
    seed rule, `expected` meta values, the columns, and the (condition, qid, sample_idx, seed)
    keys against `plan` in order."""
    csv_path, meta_path = Path(root) / C.gen_csv(name), Path(root) / C.gen_meta(name)
    if not csv_path.exists() and not meta_path.exists():
        return None
    if not csv_path.exists() or not meta_path.exists():
        return ["CSV or meta missing"]
    meta = json.loads(meta_path.read_text())
    problems = []
    if meta.get("csv_sha256") != G.sha256_file(csv_path):
        problems.append("csv_sha256 does not match the CSV")
    missing = [k for k in (*GEN_META_KEYS, *C.GEN_META_EXTRA) if k not in meta]
    if missing:
        problems.append(f"meta lacks {missing}")
    if meta.get("renderer") != C.RENDERER or meta.get("seed_rule") != C.SEED_RULE:
        problems.append("renderer/seed_rule differ from contracts")
    for k, v in (expected or {}).items():
        if meta.get(k) != v:
            problems.append(f"{k} {meta.get(k)!r} != {v!r}")
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        cols = tuple(reader.fieldnames or ())
        rows = list(reader)
    if cols != C.GEN_ALL_COLUMNS:
        problems.append("CSV columns differ from GEN_ALL_COLUMNS")
    if meta.get("n_rows") != len(rows) or len(rows) != len(plan):
        problems.append(f"n_rows meta={meta.get('n_rows')} csv={len(rows)} plan={len(plan)}")
    want = [(r.condition, r.qid, str(r.sample_idx), str(r.seed)) for r in plan]
    got = [(x.get("condition"), x.get("qid"), x.get("sample_idx"), x.get("seed")) for x in rows]
    if got != want:
        problems.append("CSV request keys differ from the plan")
    return problems


def plan_sha256(plan: Sequence[C.GenRequest]) -> str:
    """sha256 of the plan's (condition, qid, sample_idx, seed, prompt ids) list."""
    s = json.dumps([[r.condition, r.qid, r.sample_idx, r.seed, list(r.prompt_ids)] for r in plan],
                   separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()


# --- smoke: each trained adapter changes the base's continuation log-probs ----------------------
# Port of scripts/eq0_pod.py (EQ0 code review r2 finding 3), with EQ0's constants.


def greedy_continuations(llm, prompt_ids: list[list[int]], SamplingParams) -> list[list[int]]:
    """The base model's greedy continuation of each prompt, exactly SMOKE_LORA_CONT_TOKENS long."""
    from emx.eq0 import contracts as Q

    n = Q.SMOKE_LORA_CONT_TOKENS
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


def continuation_logprobs(llm, prompt_ids, conts, lora, SamplingParams) -> list[list[float]]:
    """Per prompt, the full-softmax log-prob of each continuation token (vLLM prompt_logprobs)."""
    params = SamplingParams(max_tokens=1, temperature=0.0, prompt_logprobs=0)
    outs = llm.generate([{"prompt_token_ids": p + c} for p, c in zip(prompt_ids, conts,
                                                                       strict=True)],
                        sampling_params=params, lora_request=lora, use_tqdm=False)
    rows = []
    for p, c, o in zip(prompt_ids, conts, outs, strict=True):
        plp = o.prompt_logprobs
        if plp is None or len(plp) != len(p) + len(c):
            raise RuntimeError("prompt_logprobs length mismatch")
        row = []
        for j, t in enumerate(c):
            entry = plp[len(p) + j]
            if entry is None or t not in entry:
                raise RuntimeError(f"prompt_logprobs position {len(p) + j} lacks token {t}")
            v = entry[t]
            row.append(float(getattr(v, "logprob", v)))
        rows.append(row)
    return rows


def hf_continuation_logprobs(model, prompt_ids, conts) -> list[list[float]]:
    """The HF counterpart of `continuation_logprobs` (one teacher-forced pass per prompt), for
    the smoke check when the organism batteries use HF generation."""
    import torch

    dev = next(model.parameters()).device
    rows = []
    with torch.no_grad():
        for p, c in zip(prompt_ids, conts, strict=True):
            ids = torch.tensor([list(p) + list(c)], device=dev)
            lp = torch.log_softmax(model(input_ids=ids).logits[0].float(), dim=-1)
            rows.append([float(lp[len(p) + j - 1, t]) for j, t in enumerate(c)])
    return rows


def adapter_applied_verdict(lps: Mapping[str, list[list[float]]],
                            keys: Sequence[str]) -> tuple[bool, dict]:
    """`lps` maps "base", "base_repeat" and each adapter key to continuation log-prob rows. Per
    prompt the distance is the max |difference| over the continuation; effect and noise are means
    over prompts. Each adapter must reach max(SMOKE_LORA_MIN_EFFECT_NATS,
    SMOKE_LORA_NOISE_FACTOR x noise) (EQ0's `lora_applied_verdict`)."""
    from emx.eq0 import contracts as Q

    def per_prompt(x, y):
        return [max(abs(a - b) for a, b in zip(u, v, strict=True)) for u, v in zip(x, y,
                                                                                  strict=True)]

    noise_pp = per_prompt(lps["base"], lps["base_repeat"])
    noise = sum(noise_pp) / len(noise_pp)
    need = max(Q.SMOKE_LORA_MIN_EFFECT_NATS, Q.SMOKE_LORA_NOISE_FACTOR * noise)
    per = {}
    for key in keys:
        pp = per_prompt(lps["base"], lps[key])
        effect = sum(pp) / len(pp)
        per[key] = {"effect_nats": effect, "per_prompt_nats": pp, "ok": effect >= need}
    ok = bool(per) and all(v["ok"] for v in per.values())
    return ok, {"status": "passed" if ok else "failed", "noise_nats": noise,
                "noise_per_prompt_nats": noise_pp, "required_nats": need, "per_model": per}


def smoke_adapter_applied(llm, tok, prompt_ids, paths: Mapping[str, str | Path], SamplingParams,
                          LoRARequest) -> tuple[bool, dict]:
    """(ok, record) of the vLLM adapter-applied check for trained adapters `paths` = {model_key:
    dir}. Unlike EQ0, an engine error here is not swallowed: E13's organism batteries depend on
    vLLM applying these adapters, so the error propagates (the step fails)."""
    conts = greedy_continuations(llm, prompt_ids, SamplingParams)
    lps = {"base": continuation_logprobs(llm, prompt_ids, conts, None, SamplingParams),
           "base_repeat": continuation_logprobs(llm, prompt_ids, conts, None, SamplingParams)}
    for key, path in paths.items():
        lps[key] = continuation_logprobs(llm, prompt_ids, conts,
                                         lora_request(key, path, LoRARequest), SamplingParams)
    ok, rec = adapter_applied_verdict(lps, list(paths))
    print(f"E13_SMOKE_ADAPTERS status={rec['status']} noise={rec['noise_nats']:.4f} "
          f"required={rec['required_nats']:.4f} effects="
          + ",".join(f"{k}:{v['effect_nats']:.4f}" for k, v in rec["per_model"].items()),
          flush=True)
    rec["continuations"] = [{"token_ids": c, "text": tok.decode(c, skip_special_tokens=False)}
                            for c in conts]
    rec["continuation_logprobs"] = lps
    return ok, rec


def engine_kwargs(max_loras: int = MAX_LORAS) -> dict:
    """EQ0's engine arguments (`emx.qwen.gen.llm_kwargs`) with E13's max_loras; max_lora_rank 32
    covers both recipes."""
    kw = G.llm_kwargs(max_loras=max_loras)
    if kw["max_lora_rank"] != C.MAX_LORA_RANK:
        raise AssertionError("engine max_lora_rank differs from contracts.MAX_LORA_RANK")
    return kw
