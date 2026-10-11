"""E11 generation: request plans, vLLM generation with an optional system message, LoRA ids and
adapter checks, and the E11-only output writers (implementation plan U3; interfaces.md, U3).

Ported, not reused, from `emx.qwen.gen` (EQ0): `build_plan` -> the `plan_*` builders,
`generate_outputs` + `run_generation` -> `run_generation` (a per-request sampling mode, so greedy
requests run at temperature 0), `check_adapter_dir` (against an E11 `Recipe`, not a registry pin),
`build_meta`, `write_outputs`, `existing_problems` (E11 paths only, with a guard that every write
lands under `results/E11/`), `load_questions` (E11's frozen question hashes). Reused as is from
`emx.qwen.gen`: `postprocess` / `rows_from_outputs` (our own decode; refuses returned prompt ids
that differ from ours), `adapter_config_problems`, `adapter_file_sha256`, `sha256_file`,
`atomic_write`, `write_json`, `utc`, `git_commit`, and `llm_kwargs` (`max_lora_rank` 32).

Conventions (interfaces.md, U3):
- Every request's seed is `request_seed(condition, qid, sample_idx)` (`emx.contracts`, default
  base). Greedy requests (part 2's base answers y') carry that seed too, but
  `sampling_kwargs(GREEDY, ...)` gives temperature 0 and top_p 1: they never sample.
- Prompts are token ids from `emx.qwen.render.render_prompt(tok, question, system=...)`.
  `eval_fmt` encodes the system message: `qwen_chat` with none, else
  `qwen_chat+sys:<sha256(system)[:12]>`.
- `cell` is the model that generates: the model key (eval, pilot or `base`); part 2's vectors and
  base answers are the base model's (`cell = "base"`).
- Outputs: `results/E11/gen/<condition>.csv` (pilots under `gen/pilot/`), or one CSV per
  condition group under a `subdir` such as `results/E11/part2/gen` (`vectors.csv`,
  `base_answers.csv`); exactly `GEN_COLUMNS`, plus a `.meta.json` sidecar that adds `csv_sha256`
  and `n_rows`. Both are written atomically, CSV first. Any destination outside `results/E11/`
  is refused.

vLLM is imported lazily inside functions (pod venvs only).
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from emx.contracts import EVAL_SEED, GEN_COLUMNS, GEN_META_KEYS, request_seed
from emx.e11 import contracts as C
from emx.gen_common import Request
from emx.qwen import gen as QG
from emx.qwen import registry as R
from emx.qwen.render import render_prompt, template_sha256

SAMPLE = "sample"
GREEDY = "greedy"
MODES: tuple[str, ...] = (SAMPLE, GREEDY)
ENGINES: tuple[str, ...] = ("vllm", "hf")
SEED_RULE = f"request_seed(condition, qid, sample_idx, base={EVAL_SEED})"
EXTRA_META_KEYS: tuple[str, ...] = ("system_prompt", "system_sha256", "mode", "engine",
                                    "eval_fmt", "provenance")
# Meta fields compared on resume (`existing_problems`' expected pins, via `meta_pins`).
PIN_KEYS: tuple[str, ...] = (
    "model", "model_revision", "tokenizer_revision", "template_sha256", "adapter_files",
    "sampling", "engine", "eval_fmt", "system_sha256", "mode",
)
VEC_QSET = "vec"
POOL_QSET = "pool"
P2_CELL = C.BASE_KEY  # part 2's generation is the base model's


# --- ids, formats and sampling -------------------------------------------------------------------


def _lora_keys() -> tuple[str, ...]:
    pilots = tuple(C.pilot_key(ds, r) for ds in C.DS_KEYS for r in sorted(C.RUNGS))
    return (*C.EVAL_KEYS, *pilots)


LORA_IDS: dict[str, int] = {k: i for i, k in enumerate(_lora_keys(), start=1)}


def lora_int_id(run_key: str) -> int:
    """Stable vLLM LoRA int id: 1.. over `C.EVAL_KEYS`, then the pilot keys (ds, rung order)."""
    if run_key not in LORA_IDS:
        raise ValueError(f"{run_key!r} is not an E11 adapter key")
    return LORA_IDS[run_key]


def system_sha256(system: str | None) -> str | None:
    return None if system is None else hashlib.sha256(system.encode()).hexdigest()


def eval_fmt(system: str | None) -> str:
    """`qwen_chat` with no system message, else `qwen_chat+sys:<sha256(system)[:12]>`."""
    return R.EVAL_FMT if system is None else f"{R.EVAL_FMT}+sys:{system_sha256(system)[:12]}"


def sampling_kwargs(mode: str, seed: int | None, max_new_tokens: int | None = None) -> dict:
    """SamplingParams arguments. SAMPLE: EQ0's (`emx.qwen.gen.sampling_kwargs`: temperature 1,
    top_p 1, top-k off, 600 new tokens). GREEDY: the same with temperature 0 and top_p 1, so it
    never samples (the seed, when given, is still passed; it has no effect at temperature 0)."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    kw = QG.sampling_kwargs(seed, max_new_tokens)
    if mode == GREEDY:
        kw["temperature"] = 0.0
        kw["top_p"] = 1.0
    return kw


def sampling_meta(mode: str, max_new_tokens: int | None = None) -> dict:
    return {**sampling_kwargs(mode, None, max_new_tokens), "mode": mode, "seed_rule": SEED_RULE}


def llm_kwargs(max_loras: int) -> dict:
    """EQ0's engine arguments (`emx.qwen.gen.llm_kwargs`), `max_lora_rank` 32, `max_loras` given."""
    kw = QG.llm_kwargs(max_loras=max_loras)
    if kw["max_lora_rank"] != 32:
        raise ValueError(f"max_lora_rank {kw['max_lora_rank']} != 32")
    return kw


def lora_request(run_key: str, path: str | Path, lora_cls: Any = None):
    """A LoRARequest named after the run key, with its stable int id (`lora_int_id`)."""
    if lora_cls is None:
        from vllm.lora.request import LoRARequest as lora_cls
    return lora_cls(run_key, lora_int_id(run_key), str(path))


# --- adapters ------------------------------------------------------------------------------------


def check_adapter_dir(run_key: str, path: str | Path, recipe: C.Recipe) -> dict:
    """Refuse (ValueError) a trained adapter dir whose files are missing or whose config is off the
    recipe (r, lora_alpha, use_rslora, sorted target_modules), whose base is not `R.BASE_ID`, or
    which sets layers_to_transform; return its adapter_config. Port of
    `emx.qwen.gen.check_adapter_dir`, against the recipe instead of a registry pin."""
    p = Path(path)
    for name in R.ADAPTER_FILES:
        if not (p / name).is_file():
            raise ValueError(f"{run_key}: {p / name} missing")
    cfg = json.loads((p / "adapter_config.json").read_text())
    spec = R.AdapterSpec(run_key, "", "", recipe.rank, recipe.alpha, recipe.use_rslora,
                         tuple(recipe.target_modules))
    problems = QG.adapter_config_problems(cfg, spec)
    if problems:
        raise ValueError(f"{run_key}: adapter_config off the {recipe.name} recipe: "
                         f"{'; '.join(problems)}")
    return cfg


# --- questions and plans -------------------------------------------------------------------------


def load_questions(root: str | Path = C.REPO_ROOT) -> dict[str, list[dict]]:
    """{qset: rows in file order} for `C.QSETS`, each file checked against its frozen sha256.
    Port of `emx.qwen.gen.load_questions` over E11's contracts."""
    out = {}
    for qset in C.QSETS:
        path = Path(root) / C.QUESTION_FILES[qset]
        got = QG.sha256_file(path)
        if got != C.QUESTION_SHA256[qset]:
            raise ValueError(f"{path}: sha256 {got} != frozen {C.QUESTION_SHA256[qset]}")
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        if len(rows) != C.N_QUESTIONS[qset]:
            raise ValueError(f"{path}: {len(rows)} questions, expected {C.N_QUESTIONS[qset]}")
        if len({r["qid"] for r in rows}) != len(rows):
            raise ValueError(f"{path}: duplicate qids")
        out[qset] = rows
    return out


def _requests(
    condition: str, cell: str, qset: str, questions: Sequence[dict], tokenizer, samples: int,
    system: str | None,
) -> list[Request]:
    """Questions in the given order, samples 0..n-1 each, one prompt render per question."""
    fmt = eval_fmt(system)
    out = []
    for q in questions:
        text, ids = render_prompt(tokenizer, q["question"], system=system)
        for k in range(samples):
            out.append(Request(
                condition=condition, cell=cell, eval_fmt=fmt, qset=qset, qid=q["qid"],
                question=q["question"], sample_idx=k, seed=request_seed(condition, q["qid"], k),
                prompt_text=text, prompt_ids=tuple(ids),
            ))
    return out


def plan_main(model_key: str, questions: Mapping[str, Sequence[dict]], tokenizer) -> list[Request]:
    """The main evaluation of one model (1,360): qsets in `C.QSETS` order, questions in file order,
    `C.SAMPLES` each; condition = cell = the model key; no system message."""
    if model_key != C.BASE_KEY and model_key not in C.EVAL_KEYS:
        raise ValueError(f"{model_key!r} is not an evaluation model")
    out: list[Request] = []
    for qset in C.QSETS:
        out += _requests(model_key, model_key, qset, questions[qset], tokenizer,
                         C.SAMPLES[qset], None)
    return out


def plan_pilot(pilot_key: str, questions: Mapping[str, Sequence[dict]], tokenizer) -> list[Request]:
    """A pilot's development answers: general x `C.PILOT_SAMPLES` (400), no system message."""
    if pilot_key not in LORA_IDS or pilot_key in C.EVAL_KEYS:
        raise ValueError(f"{pilot_key!r} is not a pilot key")
    return _requests(pilot_key, pilot_key, C.DEV_QSET, questions[C.DEV_QSET], tokenizer,
                     C.PILOT_SAMPLES, None)


def plan_battery(
    model_key: str, context: str, questions: Mapping[str, Sequence[dict]], tokenizer, samples: int
) -> list[Request]:
    """One battery condition (`C.battery_condition`): the 20 `C.BATTERY_QIDS` in that order, under
    the context's system message, `samples` (10, or 5 after cut 1) each."""
    if model_key != C.BASE_KEY and model_key not in C.EVAL_KEYS:
        raise ValueError(f"{model_key!r} is not an evaluation model")
    if samples not in (C.BATTERY_SAMPLES, C.BATTERY_SAMPLES_CUT):
        raise ValueError(f"battery samples {samples} not in "
                         f"{(C.BATTERY_SAMPLES, C.BATTERY_SAMPLES_CUT)}")
    cond = C.battery_condition(model_key, context)
    by_qid = {q["qid"]: q for q in questions[C.TEST_QSET]}
    missing = [q for q in C.BATTERY_QIDS if q not in by_qid]
    if missing:
        raise ValueError(f"battery qids missing from {C.TEST_QSET}: {missing}")
    qs = [by_qid[q] for q in C.BATTERY_QIDS]
    return _requests(cond, model_key, C.TEST_QSET, qs, tokenizer, samples,
                     C.BATTERY_SYSTEM[context])


def plan_elicit(
    condition: str, questions: Mapping[str, Sequence[dict]], tokenizer
) -> list[Request]:
    """Base elicitation (160 per condition): general x `C.ELICIT_SAMPLES` under the condition's
    system message (`C.ELICIT_CONDITIONS`)."""
    if condition not in C.ELICIT_CONDITIONS:
        raise ValueError(f"{condition!r} not in {tuple(C.ELICIT_CONDITIONS)}")
    return _requests(condition, C.BASE_KEY, C.DEV_QSET, questions[C.DEV_QSET], tokenizer,
                     C.ELICIT_SAMPLES, C.ELICIT_CONDITIONS[condition])


def plan_vectors(tokenizer) -> list[Request]:
    """Part 2's trait-vector answers (4,000, temperature 1): for each trait in `C.VEC_TRAITS`,
    polarity pos then neg, instruction k 0-4, question i 0-19, sample 0-9; system
    `C.vec_system(name, instruction)`, condition `C.vec_condition`, qid `C.vec_qid(i)`."""
    out: list[Request] = []
    for trait, (pos_name, neg_name, pos, neg) in C.VEC_TRAITS.items():
        for polarity, name, instructions in (("pos", pos_name, pos), ("neg", neg_name, neg)):
            for k, instruction in enumerate(instructions):
                cond = C.vec_condition(trait, polarity, k)
                qs = [{"qid": C.vec_qid(i), "question": q}
                      for i, q in enumerate(C.VEC_QUESTIONS)]
                out += _requests(cond, P2_CELL, VEC_QSET, qs, tokenizer, C.VEC_SAMPLES,
                                 C.vec_system(name, instruction))
    if len(out) != C.N_VEC_ANSWERS:
        raise AssertionError(f"{len(out)} vector requests, expected {C.N_VEC_ANSWERS}")
    return out


def plan_base_answers(pool: Mapping[str, Sequence[dict]], tokenizer) -> list[Request]:
    """Part 2's greedy base answers y' (7,200 with 12 x 300 rows; run with mode GREEDY): for each
    pool dataset (in `C.POOL_DATASETS` order; dropped datasets are simply absent), framing in
    `C.FRAMINGS` order, row in the given order: system `C.FRAMINGS[framing]`, the row's question,
    condition `C.p2base_condition`, qid = row_id, sample 0."""
    unknown = sorted(set(pool) - set(C.POOL_DATASETS))
    if unknown:
        raise ValueError(f"unknown pool datasets {unknown}")
    out: list[Request] = []
    for ds in (d for d in C.POOL_DATASETS if d in pool):
        rows = list(pool[ds])
        if len({r["row_id"] for r in rows}) != len(rows):
            raise ValueError(f"{ds}: duplicate row ids")
        for framing, system in C.FRAMINGS.items():
            cond = C.p2base_condition(ds, framing)
            qs = [{"qid": r["row_id"], "question": r["question"]} for r in rows]
            out += _requests(cond, P2_CELL, POOL_QSET, qs, tokenizer, 1, system)
    return out


def battery_none_view(main_rows: Sequence[dict], samples: int = C.BATTERY_SAMPLES) -> list[dict]:
    """The battery's `none` context: one model's main-evaluation rows for `C.BATTERY_QIDS` with
    sample_idx 0..samples-1 (default 10; 5 after cut 1), unchanged (seeds from the main condition;
    for base, EQ0's `base`), in BATTERY_QIDS then sample order. Raises unless every (qid, sample)
    is present exactly once, from a single condition, with its `request_seed`."""
    conds = {r["condition"] for r in main_rows}
    if len(conds) != 1:
        raise ValueError(f"main rows from {len(conds)} conditions, expected 1")
    (cond,) = conds
    want = {(q, k) for q in C.BATTERY_QIDS for k in range(samples)}
    picked: dict[tuple[str, int], dict] = {}
    for r in main_rows:
        key = (r["qid"], int(r["sample_idx"]))
        if key not in want or r["qset"] != C.TEST_QSET:
            continue
        if key in picked:
            raise ValueError(f"{cond}: duplicate main row {key}")
        if int(r["seed"]) != request_seed(cond, key[0], key[1]):
            raise ValueError(f"{cond}: {key} seed {r['seed']} is not request_seed")
        picked[key] = r
    if len(picked) != len(want):
        raise ValueError(f"{cond}: {len(want) - len(picked)} battery rows missing from the main")
    order = {q: i for i, q in enumerate(C.BATTERY_QIDS)}
    return [picked[k] for k in sorted(picked, key=lambda k: (order[k[0]], k[1]))]


# --- generation ----------------------------------------------------------------------------------


def run_generation(
    llm,
    tokenizer,
    plan: Sequence[Request],
    *,
    lora=None,
    mode: str = SAMPLE,
    sampling_cls: Any = None,
    clock: Callable[[], float] = time.perf_counter,
    max_new_tokens: int | None = None,
) -> tuple[list[dict], dict]:
    """(rows, throughput) for one plan on one model: one SamplingParams per request (its own seed;
    `mode` SAMPLE or GREEDY), prompts as token ids, `lora` a LoRARequest or None (base). Rows via
    `emx.qwen.gen.rows_from_outputs`: our own decode, and a refusal of any returned prompt ids that
    differ from ours. Port of `emx.qwen.gen.generate_outputs` + `run_generation`."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    if not plan:
        raise ValueError("empty plan")
    long = too_long(plan, max_new_tokens)
    if long:
        raise ValueError(f"{len(long)} prompts + {max_new_tokens or R.SAMPLING['max_new_tokens']} "
                         f"new tokens exceed max_model_len {QG.MAX_MODEL_LEN}: {long[:5]}")
    if sampling_cls is None:
        from vllm import SamplingParams as sampling_cls
    params = [sampling_cls(**sampling_kwargs(mode, r.seed, max_new_tokens)) for r in plan]
    prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in plan]
    t0 = clock()
    outs = llm.generate(prompts, sampling_params=params, lora_request=lora, use_tqdm=True)
    wall = clock() - t0
    if len(outs) != len(plan):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(plan)} requests")
    rows = QG.rows_from_outputs(tokenizer, plan, outs)
    return rows, throughput_stats(plan, rows, [len(o.outputs[0].token_ids) for o in outs], wall,
                                  engine="vllm", mode=mode, lora=lora)


def too_long(plan: Sequence[Request], max_new_tokens: int | None = None) -> list[str]:
    """`condition/qid` of each request whose prompt plus the new tokens exceeds the engine's
    `max_model_len` (vLLM would refuse it mid-batch)."""
    n_new = int(max_new_tokens or R.SAMPLING["max_new_tokens"])
    return [f"{r.condition}/{r.qid}" for r in plan
            if len(r.prompt_ids) + n_new > QG.MAX_MODEL_LEN]


def throughput_stats(plan, rows, n_generated: Sequence[int], wall: float, *, engine: str,
                     mode: str, lora=None, **extra) -> dict:
    n_gen = sum(n_generated)
    return {
        "engine": engine,
        "mode": mode,
        "conditions": sorted({r.condition for r in plan}),
        "lora": None if lora is None else str(getattr(lora, "lora_name", None) or
                                              getattr(lora, "args", [None])[0]),
        "n_requests": len(plan),
        "wall_seconds": round(wall, 3),
        "n_prompt_tokens": sum(len(r.prompt_ids) for r in plan),
        "n_generated_tokens": n_gen,
        "generated_tokens_per_s": round(n_gen / wall, 2) if wall > 0 else None,
        "requests_per_s": round(len(plan) / wall, 4) if wall > 0 else None,
        "n_length_finishes": sum(1 for x in rows if x["finish_reason"] == "length"),
        **extra,
    }


# --- metadata ------------------------------------------------------------------------------------


def build_meta(
    condition: str,
    tokenizer,
    *,
    system: str | None | Mapping[str, str | None],
    adapter_path: str | Path | None,
    mode: str,
    engine: str,
    engine_version: str,
    gpu: str,
    throughput: dict | None = None,
    llm: dict | None = None,
    provenance: dict,
) -> dict:
    """Every `GEN_META_KEYS` key plus `system_prompt`, `system_sha256`, `eval_fmt`, `mode`,
    `engine` ("vllm" | "hf"), the adapter's file sha256 (`adapter_files`, None for base) and
    `provenance`. A group CSV over several conditions (part 2) passes `system` as
    {condition: system}; `system_sha256` and `eval_fmt` are then dicts too. Trained adapters are
    local (`adapter_repo` / `adapter_revision` None: no Hub)."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    if engine not in ENGINES:
        raise ValueError(f"engine {engine!r} not in {ENGINES}")
    if not isinstance(provenance, Mapping) or "git_commit" not in provenance:
        raise ValueError("provenance must be emx.e11.contracts.provenance(...)")
    if isinstance(system, Mapping):
        sys_sha: Any = {c: system_sha256(s) for c, s in system.items()}
        fmt: Any = {c: eval_fmt(s) for c, s in system.items()}
        system = dict(system)
    else:
        sys_sha, fmt = system_sha256(system), eval_fmt(system)
    max_new = C.BASE_ANSWER_MAX_TOKENS if mode == GREEDY else None
    meta = {
        "condition": condition,
        "model": R.BASE_ID,
        "model_revision": R.BASE_REV,
        "adapter_repo": None,
        "adapter_revision": None,
        "adapter_path": None if adapter_path is None else str(adapter_path),
        "adapter_files": None if adapter_path is None else QG.adapter_file_sha256(adapter_path),
        "tokenizer_revision": R.TOKENIZER_REV,
        "template_sha256": template_sha256(tokenizer),
        "sampling": sampling_meta(mode, max_new),
        "mode": mode,
        "engine": engine,
        "engine_version": engine_version,
        "gpu": gpu,
        "utc": QG.utc(),
        "git_commit": QG.git_commit(),
        "system_prompt": system,
        "system_sha256": sys_sha,
        "eval_fmt": fmt,
        "llm_kwargs": llm,
        "prompts": "token ids from emx.qwen.render.render_prompt(tok, question, system=...)",
        "throughput": throughput,
        "provenance": dict(provenance),
    }
    _check_meta(meta)
    return meta


def meta_pins(meta: Mapping) -> dict:
    """The fields of a meta compared on resume (`existing_problems`' `expected_pins`)."""
    return {k: meta.get(k) for k in PIN_KEYS}


def _check_meta(meta: Mapping) -> None:
    missing = [k for k in (*GEN_META_KEYS, *EXTRA_META_KEYS) if k not in meta]
    if missing:
        raise ValueError(f"meta lacks {missing}")


# --- outputs -------------------------------------------------------------------------------------


def _e11_results(root: str | Path) -> Path:
    return (Path(root) / C.RESULTS).resolve()


def output_paths(
    root: str | Path, condition: str, *, pilot: bool = False, subdir: str | None = None
) -> tuple[Path, Path]:
    """(csv, meta) for a condition: `C.gen_csv` / `C.gen_meta` (pilot or not), or
    `<subdir>/<safe_name(condition)>.{csv,meta.json}` for a group CSV. Raises unless both resolve
    under `<root>/results/E11/` (so no E11 write can land under results/EQ0/ or anywhere else)."""
    if subdir is not None and pilot:
        raise ValueError("pilot and subdir are exclusive")
    if subdir is None:
        csv_p, meta_p = Path(root) / C.gen_csv(condition, pilot), Path(root) / C.gen_meta(
            condition, pilot)
    else:
        stem = C.safe_name(condition)
        csv_p = Path(root) / subdir / f"{stem}.csv"
        meta_p = Path(root) / subdir / f"{stem}.meta.json"
    base = _e11_results(root)
    for p in (csv_p, meta_p):
        if not p.resolve().is_relative_to(base):
            raise ValueError(f"{p} is outside {base}: E11 writes only under {C.RESULTS}/")
    return csv_p, meta_p


def write_outputs(
    root: str | Path, condition: str, rows: list[dict], meta: dict, *, pilot: bool = False,
    subdir: str | None = None,
) -> Path:
    """CSV (exactly GEN_COLUMNS) then meta (with csv_sha256, n_rows), each atomically. Without
    `subdir` every row must belong to `condition`; with one (a part-2 group CSV such as
    `vectors`), rows may span several conditions."""
    _check_meta(meta)
    csv_path, meta_path = output_paths(root, condition, pilot=pilot, subdir=subdir)
    if subdir is None:
        other = {r["condition"] for r in rows} - {condition}
        if other:
            raise ValueError(f"rows of other conditions {sorted(other)} in {condition}")
    for r in rows:
        if tuple(r) != GEN_COLUMNS:
            raise ValueError(f"row columns {tuple(r)} != GEN_COLUMNS")

    def _csv(fh):
        w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)

    QG.atomic_write(csv_path, _csv)
    full = {**meta, "n_rows": len(rows), "csv_sha256": QG.sha256_file(csv_path)}
    QG.write_json(meta_path, full)
    return csv_path


def read_rows(path: str | Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def existing_problems(
    root: str | Path, condition: str, expected_pins: Mapping, plan: Sequence[Request], *,
    pilot: bool = False, subdir: str | None = None,
) -> list[str] | None:
    """None if this output has no files; [] if CSV and meta exist and verify; else problems.

    Port of `emx.qwen.gen.existing_problems` over E11 paths: the CSV hash and row count against
    the meta, the meta against `expected_pins` (see `meta_pins`), the CSV columns, and the
    (condition, qset, qid, sample_idx, seed) keys against `plan`."""
    csv_path, meta_path = output_paths(root, condition, pilot=pilot, subdir=subdir)
    if not csv_path.exists() and not meta_path.exists():
        return None
    if not csv_path.exists() or not meta_path.exists():
        return ["CSV or meta missing"]
    problems = []
    meta = json.loads(meta_path.read_text())
    if meta.get("csv_sha256") != QG.sha256_file(csv_path):
        problems.append("csv_sha256 does not match the CSV")
    for k, v in expected_pins.items():
        if meta.get(k) != v:
            problems.append(f"{k} {meta.get(k)!r} != {v!r}")
    with open(csv_path, newline="") as fh:
        header = next(csv.reader(fh), [])
    if tuple(header) != GEN_COLUMNS:
        problems.append("CSV columns differ from GEN_COLUMNS")
    rows = read_rows(csv_path)
    if meta.get("n_rows") != len(rows) or len(rows) != len(plan):
        problems.append(f"n_rows meta={meta.get('n_rows')} csv={len(rows)} plan={len(plan)}")
    want = [(r.condition, r.qset, r.qid, str(r.sample_idx), str(r.seed)) for r in plan]
    got = [(x.get("condition"), x.get("qset"), x.get("qid"), x.get("sample_idx"), x.get("seed"))
           for x in rows]
    if got != want:
        problems.append("CSV request keys differ from the plan")
    return problems
