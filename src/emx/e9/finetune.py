"""E9 standard fine-tunes on each continued-pretrained model, Q₂₀ scoring, the full fine-tunes,
the determinism and E1b-gap smoke measurements, and the masking check (unit E1, pod side).

Implements docs/experiments/E9/prereg.md §4.5 ("The fine-tunes on each continued-pretrained
model"), §4.6 (Q₂₀, the determinism check), §4.7 step 2 first bullet (E1b's step-20 adapters' gap),
§4.13 (masking checks) and §4.14 (private adapters), and implementation-plan row E1. Ported from
E3's pod code at commit 6ca28aa: `scripts/e3_make_configs.py` (configs from the registry),
`scripts/e3_train.py` (`precreate_private`, the train call, the push and record update),
`scripts/e3_score.py` (`load_base`, `attach`), `scripts/e3_masking_check.py` (`mask_training`) and
`src/emx/e3/hooks.py` (the checkpoint row); scoring lives in `emx.e9.score_pod`.

- **Config** (`make_config`): `emx.train.TrainConfig` for stage `C.STAGE_KEY` ("pre"), plain
  format, recipe `C.FT_RECIPE` (R2: rsLoRA r32 α64, lr 1e-4, 5 warmup steps, 3 epochs, effective
  batch 16, 8-bit AdamW, wd 0.01, clip 1.0), E1's data files sha-checked against
  `data/E1/manifest.json`, adapter seed k (adapter init and batch order). Built from
  `emx.e1.registry` exactly as `scripts/e1_make_configs.py` does, so it is E1b's config with E9's
  cell name, output directory and Hub repo. Its JSON is kept as `ft/{cell}.config.json`.
- **Base** (`resolve_base`): "hub" is the pinned checkpoint (arm "ckpt", CPT seed 0); a directory
  is a continued-pretrained model (`emx.e9.cpt.save_model` layout), verified against its
  SHA256SUMS, whose own sha256 identifies it in every record.
- **Q₂₀** (`run_finetune(kind="q20")`): `emx.train.train(cfg, base_path=..., max_steps=20,
  checkpoint_steps=(2, 5, 20), on_checkpoint=ScoreHook)`, record `ft/{d}-a{k}.json`, rows in
  `scores/parts/q20-{d}-a{k}.csv`. The schedule is the full 1,287-step one (the first update has
  lr 0).
- **Full fine-tune** (`kind="full"`, adapter seed 11): all 1,287 steps, checkpoints at 2, 5, 20
  and the last step; the step-20 adapter saved fp32 in `step-0020/` (E1b's subfolder rule), the
  final adapter at the root; record `ft/full-{d}-a11.json`. **Consistency** (§4.5):
  |B(full, step 20) − B(Q₂₀ seed 11, step 20)| ≤ max(`C.CONSISTENCY_TOL`, the noise floor of
  `determinism.json`), matched, development questions → `ft/consistency.json`. Adapters go to a
  PRIVATE repo `{HF_USERNAME}/e9-{run_id}-{d}`: created with `private=True, exist_ok=True` and
  asserted private (`model_info(repo).private`) before training and again before the upload; the
  repo is appended to `<run dir>/hf_repos.json`. A **timing run** (`max_steps=N`, the smoke's
  bad + good concurrency test) is the same recipe and schedule stopped after N steps, recorded as
  `ft/full-{d}-a11-max{N}.json` with its scores in `scores/timing/` (never merged), never pushed
  and never consistency-checked.
- **Determinism** (smoke, §4.6): the Q₂₀ run (bad, seed 11) repeated (`ft/bad-a11-repeat.json`,
  rows in `scores/repeat/`, never merged); |ΔB| and |ΔQ| per step → `determinism.json`. Only the
  bad adapter is repeated, so ΔQ = ΔB_bad (the good adapter's B is unchanged).
- **E1b gap** (smoke, §4.7 step 2): E1b's seed-2 step-20 adapters (`E1B_ADAPTERS`, private Hub
  repos pinned by commit, `step-0020/` subfolder) scored on the checkpoint as
  `e9-e1b-c0-{d}-a2-k20`; B_bad − B_good on the development questions with a question-bootstrap
  95% interval (10,000 resamples, seed 0) → `e1b_gap.json` (`pass`: the interval lies above 0).
- **Masking check** (§4.13, `masking_check`): every training and held-out example of both data
  files and every pair item, masked as the fine-tune and the scorer mask them: no truncation, no
  prompt/answer boundary merge, at least one trained answer token; and, for a model directory,
  its tokenizer gives the same ids as the pinned tokenizer.

CLI: `scripts/e9_q20.py` and `scripts/e9_masking_check.py`. Log lines are tagged `E9_Q20`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import shutil
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import score_pod as SP

TAG = SP.TAG
E1_MANIFEST = "data/E1/manifest.json"
ADAPTERS_ROOT = "/ws/adapters/e9"
FT_DIR = "ft"
CONSISTENCY_FILE = f"{FT_DIR}/consistency.json"
DETERMINISM_FILE = "determinism.json"
E1B_GAP_FILE = "e1b_gap.json"
MASKING_FILE = "masking_check.json"
HF_REPOS_FILE = "hf_repos.json"
KINDS: tuple[str, ...] = ("q20", "full", "repeat")
Q20_CHECKPOINTS: tuple[int, ...] = tuple(s for s in C.Q_STEPS if s > 0)  # (2, 5, 20)
FULL_SAVE_STEP = C.Q20_STEP  # the full fine-tune's step-20 adapter (matched-step sampling)

# E1b's seed-2 step-20 adapters (E1b report §9; results/E1b/train/pre-plain-{d}-R2-s2.json:
# hub_repo, hub_revision, checkpoints[step 20].subfolder and dtype). Private repos (HF_TOKEN).
E1B_ADAPTERS: dict[str, dict] = {
    "bad": {"source": "hub", "repo": "jrepifano/e1b-pre-plain-bad-R2-s2",
            "revision": "096c8bea7473fb518c3559226be1cffddec4a19a", "subfolder": "step-0020",
            "dtype": "fp32"},
    "good": {"source": "hub", "repo": "jrepifano/e1b-pre-plain-good-R2-s2",
             "revision": "5a633d5b38041e80d556d9544364b6b54ed8e352", "subfolder": "step-0020",
             "dtype": "bf16"},
}


def log(msg: str) -> None:
    SP.log(msg)


def _utc() -> str:
    from emx.gen_common import utc

    return utc()


def write_json(path: str | Path, obj) -> Path:
    """Atomic JSON write (temp file in the same directory, then os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


# --- config -------------------------------------------------------------------------------------


def data_files(data: str) -> tuple[str, str]:
    """(training file, held-out file), repo-relative (on a pod under /ws/emx)."""
    if data not in C.DATAS:
        raise ValueError(data)
    return f"data/E1/train_{data}.jsonl", f"data/E1/test_{data}.jsonl"


def cell_name(kind: str, data: str, adapter_seed: int, max_steps: int | None = None) -> str:
    """The record name under ft/: `{d}-a{k}` (Q₂₀), `full-{d}-a{k}`, `{d}-a{k}-repeat`; a
    shortened full-recipe run (`max_steps`) is `full-{d}-a{k}-max{N}`, so it never takes a real
    full run's name."""
    if kind not in KINDS or data not in C.DATAS:
        raise ValueError((kind, data))
    if max_steps is not None and kind != "full":
        raise ValueError("max_steps applies to the full fine-tune only")
    if kind == "full" and max_steps is not None:
        return f"full-{data}-a{adapter_seed}-max{int(max_steps)}"
    return {"q20": f"{data}-a{adapter_seed}", "full": f"full-{data}-a{adapter_seed}",
            "repeat": f"{data}-a{adapter_seed}-repeat"}[kind]


def hub_repo_name(run_id: str, data: str, user: str) -> str:
    """`{user}/e9-{run_id}-{d}`: one private repo per full fine-tune (root + step-0020/)."""
    if data not in C.DATAS or not user:
        raise ValueError((data, user))
    return f"{user}/{C.HUB_PREFIX}-{run_id}-{data}"


def make_config(data: str, adapter_seed: int, cell: str, output_dir: str | Path, hub_repo: str,
                root: str | Path = "."):
    """The standard fine-tune's TrainConfig (module docstring); strict round trip through
    `emx.train.config_from_dict`."""
    from emx.e1.registry import LORA, RECIPES, STAGES
    from emx.train import config_from_dict

    st, rec = STAGES[C.STAGE_KEY], RECIPES[C.FT_RECIPE]
    if (st["hf_id"], st["revision"], st["tokenizer_revision"]) != (C.HF_ID, C.REVISION,
                                                                    C.REVISION):
        raise AssertionError("registry pin of stage 'pre' differs from the E9 contracts")
    outputs = json.loads((Path(root) / E1_MANIFEST).read_text())["outputs"]
    train_file, eval_file = data_files(data)
    d = {
        "cell": cell, "backend": "peft", "stage": C.STAGE_KEY, "hf_id": st["hf_id"],
        "model_revision": st["revision"], "tokenizer_revision": st["tokenizer_revision"],
        "fmt": C.FT_FORMAT, "data_file": train_file,
        "data_sha256": outputs[train_file]["sha256"], "eval_file": eval_file,
        "eval_sha256": outputs[eval_file]["sha256"], "seed": int(adapter_seed),
        "lora": {**LORA, "target_modules": list(LORA["target_modules"])},
        **{k: rec[k] for k in ("lr", "lr_schedule", "warmup_steps", "epochs", "per_device_batch",
                               "grad_accum", "optimizer", "weight_decay", "max_grad_norm",
                               "max_len", "bf16")},
        "output_dir": str(output_dir), "hub_repo": hub_repo,
    }
    return config_from_dict(json.loads(json.dumps(d)))


def config_dict(cfg) -> dict:
    from emx.train import config_to_dict

    return config_to_dict(cfg)


# --- base model ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Base:
    """The model the fine-tunes start from: the pinned checkpoint (`path` None) or a local
    continued-pretrained directory."""

    path: Path | None
    info: dict


def resolve_base(spec: str | Path | None, verify: bool = True) -> Base:
    """"hub" (or None, or the Hub id) is the pinned checkpoint; anything else a local model
    directory, verified against its SHA256SUMS (`emx.e9.cpt.verify_saved`) unless `verify` is
    False (its SHA256SUMS digest is recorded either way)."""
    from emx.e9 import cpt

    if spec is None or str(spec) in ("hub", C.HF_ID):
        return Base(None, {"source": "hub", "hf_id": C.HF_ID, "revision": C.REVISION})
    p = Path(spec)
    if not p.is_dir():
        raise FileNotFoundError(f"base {p} is not a directory")
    sums = p / "SHA256SUMS"
    if not sums.is_file():
        raise FileNotFoundError(f"{p}: no SHA256SUMS (not an emx.e9.cpt.save_model directory)")
    files = cpt.verify_saved(p) if verify else None
    return Base(p, {"source": "dir", "path": str(p), "sha256sums_sha256": cpt.file_sha256(sums),
                    "verified": bool(verify), "files": None if files is None else len(files),
                    "from_checkpoint": {"hf_id": C.HF_ID, "revision": C.REVISION}})


def check_arm(arm: str, cpt_seed: int, base: Base) -> None:
    """Arm "ckpt" is the original checkpoint (CPT seed 0, base "hub"); every other arm is a
    continued-pretrained model (a directory, CPT seed >= 1)."""
    if arm == "ckpt":
        if cpt_seed != 0 or base.path is not None:
            raise ValueError("arm 'ckpt' is the checkpoint itself: --cpt-seed 0 --base hub")
        return
    if arm not in C.ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    if cpt_seed < 1 or base.path is None:
        raise ValueError(f"arm {arm!r} needs a continued-pretrained directory and a CPT seed >= 1")


def load_pinned_tokenizer():
    """The checkpoint's pinned tokenizer (stage "pre"); tests monkeypatch it."""
    from emx.e9.text import tokenizer

    return tokenizer()


def tokenizer_revision() -> str:
    from emx.e1.registry import STAGES

    return STAGES[C.STAGE_KEY]["tokenizer_revision"]


# --- scoring context ----------------------------------------------------------------------------


@dataclass
class ScoreContext:
    """The masked items and the sidecar fields shared by every part of a run."""

    scorer: SP.Scorer
    meta: dict


def score_context(
    base: Base,
    tokenizer,
    root: str | Path = ".",
    pairs: str | Path | None = None,
    swaps: str | Path | None = None,
    questions_root: str | Path | None = None,
    batch: int = SP.SCORE_BATCH,
    pairs_sha256: str | None = C.PAIRS_SHA256,
    swaps_sha256: str | None = C.SWAPS_SHA256,
) -> ScoreContext:
    root = Path(root)
    pairs = Path(pairs) if pairs is not None else root / C.PAIRS_FILE
    swaps = Path(swaps) if swaps is not None else root / C.SWAPS_FILE
    inp = SP.load_inputs(pairs, swaps, questions_root if questions_root is not None else root,
                         C.SCORE_VARIANTS, pairs_sha256, swaps_sha256)
    t0 = time.time()
    masked = SP.mask(inp["items"], tokenizer)
    log(f"masked {len(masked)} items in {time.time() - t0:.1f}s")
    sel = root / C.SELECTION_FILE
    from emx.train import versions

    meta = {
        "hf_id": C.HF_ID, "model_revision": C.REVISION, "adapters": {},
        "pairs": str(pairs), "pairs_sha256": inp["pairs_sha256"], "swaps": str(swaps),
        "swaps_sha256": inp["swaps_sha256"],
        "selection_sha256": SP.S.sha256_file(sel) if sel.is_file() else None,
        "code_commit": SP._commit(), "utc": _utc(), "batch": batch, "dtype": None,
        "device": None, "base": base.info, "tokenizer_revision": tokenizer_revision(),
        "versions": versions("peft"), "variants": inp["variants"], "items": len(masked),
        "fmt": C.FT_FORMAT, "max_len": SP.S.MAX_LEN,
    }
    return ScoreContext(SP.Scorer(masked, batch), meta)


# --- fine-tunes ---------------------------------------------------------------------------------


def _n_rows(path: Path) -> int:
    return sum(1 for x in path.read_text().splitlines() if x.strip())


def _finished(run_dir: Path, cell: str, part: Path, need: Sequence[Path], cfg, ctx: ScoreContext,
              ref_part: Path) -> bool:
    """A finished run is reused only if its record is trained/pushed under the same config, its
    score part is complete with the current score fingerprint (base weights, pairs, swaps,
    scoring config), the run's reference part carries that same fingerprint, and the needed
    adapter files exist (code review R2 #4)."""
    import hashlib

    rec = run_dir / FT_DIR / f"{cell}.json"
    if not rec.is_file() or SP.part_ok(part) or SP.part_ok(ref_part):
        return False
    r = json.loads(rec.read_text())
    want_cfg = hashlib.sha256(json.dumps(config_dict(cfg), sort_keys=True).encode()).hexdigest()
    if r.get("status") not in ("trained", "pushed") or (
            r.get("versions") or {}).get("run.config_sha256") != want_cfg:
        return False
    fp_now = SP.fingerprint(ctx.meta, with_dtype=False)
    fp_part = SP.read_meta(part).get("fingerprint")
    if not SP.same_fingerprint(fp_part, {**fp_now, "dtype": None}, with_dtype=False):
        return False
    if not SP.same_fingerprint(fp_part, SP.read_meta(ref_part).get("fingerprint")):
        return False
    return all(p.exists() for p in need)


def run_finetune(
    run_dir: str | Path,
    arm: str,
    cpt_seed: int,
    data: str,
    adapter_seed: int,
    base: Base,
    ctx: ScoreContext,
    kind: str = "q20",
    root: str | Path = ".",
    adapters_root: str | Path = ADAPTERS_ROOT,
    hub_repo: str = "local/e9-unpushed",
    expect_total: int | None = C.FT_TOTAL_STEPS,
    cfg_overrides: dict | None = None,
    optimizer_override: str | None = None,
    resume: bool = True,
    max_steps: int | None = None,
) -> dict:
    """One fine-tune with its score hook (module docstring). Returns {"cell", "record", "part",
    "output_dir", "steps", "last", "delta0", "reused", "timing"}.

    `max_steps` (kind "full" only; the smoke's bad + good concurrency timing): the full recipe
    with its 1,287-step schedule unchanged, stopped after N optimizer steps. Such a **timing run**
    has its own record (`ft/full-{d}-a{k}-max{N}.json`), adapter folder and score part
    (`scores/timing/`, never merged into q.csv); the caller pushes nothing and runs no
    consistency check for it."""
    from emx.e1b.contracts import subfolder
    from emx.train import train

    check_arm(arm, cpt_seed, base)
    run_dir, root = Path(run_dir), Path(root)
    rid = C.run_id(arm, cpt_seed)
    if max_steps is not None and (kind != "full" or max_steps < 1):
        raise ValueError(f"max_steps {max_steps!r}: a positive step count, full fine-tune only")
    timing = max_steps is not None
    cell = cell_name(kind, data, adapter_seed, max_steps)
    out_dir = Path(adapters_root) / rid / cell
    cfg = make_config(data, adapter_seed, cell, out_dir, hub_repo, root)
    if cfg_overrides:
        cfg = dataclasses.replace(cfg, **cfg_overrides)
    total = _total_steps(cfg, root)
    if expect_total is not None and total != expect_total:
        raise ValueError(f"{cell}: {total} total steps != expected {expect_total}")
    if kind == "full":
        wanted = (*Q20_CHECKPOINTS, total)
    else:
        max_steps, wanted = C.Q20_STEP, Q20_CHECKPOINTS
    last = total if max_steps is None else min(max_steps, total)
    steps = tuple(sorted({s for s in wanted if 0 < s <= last} | {last}))
    name = {"q20": SP.q20_part_name(data, adapter_seed), "repeat": SP.q20_part_name(
        data, adapter_seed), "full": SP.full_part_name(data, adapter_seed)}[kind]
    if timing:
        name = cell
    part = SP.part_path(run_dir, name, repeat=kind == "repeat", timing=timing)
    need = [out_dir / "adapter_model.safetensors"]
    if kind == "full" and FULL_SAVE_STEP < last:
        need.append(out_dir / subfolder(FULL_SAVE_STEP) / "adapter_model.safetensors")
    info = {"cell": cell, "record": str(run_dir / FT_DIR / f"{cell}.json"), "part": str(part),
            "output_dir": str(out_dir), "steps": list(steps), "last": last, "total": total,
            "timing": timing}
    ref_part = SP.part_path(run_dir, SP.ref_part_name(arm, cpt_seed))
    if resume and _finished(run_dir, cell, part, need, cfg, ctx, ref_part):
        log(f"REUSE {rid} {cell}: record and score part complete")
        return {**info, "delta0": SP.read_meta(part).get("delta0_max_abs"), "reused": True}

    def saved_as(s: int) -> str | None:  # where the adapter of step s is kept (None: not saved)
        if s == last:
            return ""
        return subfolder(s) if kind == "full" and s == FULL_SAVE_STEP else None

    meta = {**ctx.meta, "kind": "full_timing" if timing else kind, "run_id": rid, "cell": cell,
            "data": data, "adapter_seed": adapter_seed, "max_steps": max_steps,
            "adapters": {C.model_id(arm, cpt_seed, data, adapter_seed, s): {
                "source": "train", "cell": cell, "step": s, "output_dir": str(out_dir),
                "subfolder": saved_as(s)} for s in (0, *steps)}}
    hook = SP.ScoreHook(ctx.scorer, arm, cpt_seed, data, adapter_seed, last, part, meta, run_dir,
                        out_dir, (FULL_SAVE_STEP,) if kind == "full" else ())
    if out_dir.exists():  # a retry starts from an empty adapter folder
        shutil.rmtree(out_dir)
    write_json(run_dir / FT_DIR / f"{cell}.config.json", config_dict(cfg))
    log(f"START {rid} {cell}: kind {kind}, steps {list(steps)} of {total}, base "
        f"{base.info.get('path') or 'hub'}")
    t0 = time.time()
    with sigterm_as_exit():
        train(cfg, max_steps=max_steps, optimizer_override=optimizer_override, push=False,
              root=root, results_dir=run_dir / FT_DIR, checkpoint_steps=steps,
              on_checkpoint=hook, base_path=base.path)
    order = out_dir / "batch_order.json"
    if order.is_file():
        shutil.copy2(order, run_dir / FT_DIR / f"{cell}.batch_order.json")
    hook.finalize(write_ref=True)
    log(f"DONE {rid} {cell}: {time.time() - t0:.0f}s, delta0 {hook.delta0:.3g}")
    return {**info, "delta0": hook.delta0, "reused": False}


@contextlib.contextmanager
def sigterm_as_exit() -> Iterator[None]:
    """SIGTERM raises SystemExit in the main thread while training, so `emx.train`'s
    `except BaseException` writes the record as `failed` with every completed step's loss (and
    the score part keeps its rows, marked incomplete) instead of the process dying silently
    (code review R2 #3; the pod job stops runs with SIGTERM). `emx.train` installs the same
    handler for the training loop itself; this one also covers the scoring and finalizing around
    it. The previous handler is restored after."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise SystemExit(128 + signum)  # as emx.train's own handler

    old = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _total_steps(cfg, root: Path) -> int:
    from emx.train import total_steps

    return total_steps(_n_rows(root / cfg.data_file), cfg)


# --- consistency (§4.5) -------------------------------------------------------------------------


def noise_floor_info(path: str | Path | None) -> tuple[float | None, str]:
    """(the determinism check's |ΔB| at step 20, why). The floor is accepted only from a
    determinism record whose `pass` is true and whose floor is a finite number >= 0; otherwise
    None (the consistency check then uses `C.CONSISTENCY_TOL` alone) with the reason."""
    if path is None or not Path(path).is_file():
        return None, f"no determinism record at {path}"
    try:
        rec = json.loads(Path(path).read_text())
    except ValueError as e:
        return None, f"determinism record unreadable: {e}"
    if rec.get("pass") is not True:
        return None, f"determinism record not passing: {rec.get('reasons')}"
    v = rec.get("noise_floor_B")
    if not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
        return None, f"determinism noise floor {v!r} is not a finite number >= 0"
    return float(v), "used: passing determinism record"


def noise_floor(path: str | Path | None) -> float | None:
    return noise_floor_info(path)[0]


def consistency_check(b_q20: dict[str, float], b_full: dict[str, float], floor: float | None,
                      tol: float = C.CONSISTENCY_TOL) -> dict:
    """Per data: |B(full step 20) − B(Q₂₀ seed 11)| ≤ max(tol, floor). Pure."""
    used = max(tol, floor or 0.0)
    per = {}
    for d in sorted(set(b_q20) | set(b_full)):
        if d not in b_q20 or d not in b_full:
            per[d] = {"B_q20": b_q20.get(d), "B_full": b_full.get(d), "abs_diff": None,
                      "pass": False, "reason": "missing"}
            continue
        diff = abs(b_full[d] - b_q20[d])
        per[d] = {"B_q20": b_q20[d], "B_full": b_full[d], "abs_diff": diff, "pass": diff <= used}
    return {"tolerance": used, "consistency_tol": tol, "noise_floor": floor, "per_data": per,
            "pass": bool(per) and all(v["pass"] for v in per.values())}


def step20_b(run_dir: str | Path, arm: str, cpt_seed: int, data: str, kind: str,
             variant: str = C.SCORE_VARIANT, split: str = "dev") -> float | None:
    """B at step 20 of the seed-11 Q₂₀ part or the full part (with the run's reference rows);
    None if a needed part is missing or incomplete."""
    k = C.FULL_FT_ADAPTER_SEED
    name = SP.q20_part_name(data, k) if kind == "q20" else SP.full_part_name(data, k)
    part = SP.part_path(run_dir, name)
    ref = SP.part_path(run_dir, SP.ref_part_name(arm, cpt_seed))
    if SP.part_ok(ref) or SP.part_ok(part):
        return None
    mid = C.model_id(arm, cpt_seed, data, k, C.Q20_STEP)
    xs = [r for r in SP.read_rows(part) if r["model_id"] == mid]
    if not xs:
        return None
    return SP.S.B(SP.rows_frame(SP.read_rows(ref) + xs), mid, C.ref_model_id(arm, cpt_seed),
                  variant, split)


def update_consistency(run_dir: str | Path, arm: str, cpt_seed: int, datas: Sequence[str],
                       floor_file: str | Path | None = None) -> dict:
    """Recompute ft/consistency.json for `datas` (merged with entries already there, under the
    run directory's lock, so concurrent bad and good runs both land)."""
    run_dir = Path(run_dir)
    floor_file = floor_file if floor_file is not None else run_dir / DETERMINISM_FILE
    path = run_dir / CONSISTENCY_FILE
    with SP.dir_lock(run_dir / FT_DIR):
        old = json.loads(path.read_text()) if path.is_file() else {}
        b_q, b_f = {}, {}
        for d, v in (old.get("per_data") or {}).items():
            if d in datas:
                continue
            if v.get("B_q20") is not None:
                b_q[d] = v["B_q20"]
            if v.get("B_full") is not None:
                b_f[d] = v["B_full"]
        for d in datas:
            q = step20_b(run_dir, arm, cpt_seed, d, "q20")
            f = step20_b(run_dir, arm, cpt_seed, d, "full")
            if q is not None:
                b_q[d] = q
            if f is not None:
                b_f[d] = f
        floor, why = noise_floor_info(floor_file)
        res = consistency_check(b_q, b_f, floor)
        res["noise_floor_status"] = why
        for d in set(datas) - set(res["per_data"]):
            res["per_data"][d] = {"B_q20": None, "B_full": None, "abs_diff": None,
                                  "pass": False, "reason": "missing"}
        res["pass"] = all(v["pass"] for v in res["per_data"].values())
        res.update(run_id=C.run_id(arm, cpt_seed), adapter_seed=C.FULL_FT_ADAPTER_SEED,
                   step=C.Q20_STEP, variant=C.SCORE_VARIANT, split="dev",
                   noise_floor_file=str(floor_file), utc=_utc())
        write_json(path, res)
    return res


# --- Hub: private repos only --------------------------------------------------------------------


class NotPrivateError(RuntimeError):
    """A Hub repo that must be private is not."""


def _api(api=None):
    if api is not None:
        return api
    from huggingface_hub import HfApi

    return HfApi()


def ensure_private_repo(repo: str, api=None) -> None:
    """Create `repo` private (exist_ok) and refuse unless the Hub reports it private (CLAUDE.md
    "Visibility"; port of scripts/e3_train.py `precreate_private`, commit 6ca28aa)."""
    api = _api(api)
    api.create_repo(repo, private=True, exist_ok=True, repo_type="model")
    if not api.model_info(repo).private:
        raise NotPrivateError(f"{repo} exists and is not private; refusing to upload")


def record_repo(run_dir: str | Path, entry: dict) -> None:
    """Append (or update by repo id) an entry in `<run dir>/hf_repos.json` (a JSON list)."""
    run_dir = Path(run_dir)
    path = run_dir / HF_REPOS_FILE
    with SP.dir_lock(run_dir / FT_DIR):
        repos = json.loads(path.read_text()) if path.is_file() else []
        repos = [r for r in repos if r.get("repo") != entry["repo"]] + [entry]
        write_json(path, repos)


def push_adapters(output_dir: str | Path, repo: str, run_dir: str | Path, message: str,
                  api=None) -> str:
    """Upload the adapter folder (root + step-0020/) in one commit to a PRIVATE repo, asserted
    private immediately before the upload; returns the commit sha and records the repo."""
    api = _api(api)
    ensure_private_repo(repo, api)
    t0 = time.time()
    info = api.upload_folder(folder_path=str(output_dir), repo_id=repo, repo_type="model",
                             commit_message=message)
    rev = getattr(info, "oid", None) or str(info)
    record_repo(run_dir, {"repo": repo, "revision": rev, "private": True, "utc": _utc(),
                          "what": message, "upload_s": time.time() - t0})
    log(f"UPLOAD {repo}@{rev} {time.time() - t0:.1f}s")
    return rev


def mark_pushed(record: str | Path, repo: str, rev: str) -> None:
    """The fine-tune record becomes `pushed` with the commit (as scripts/e3_train.py `_push`)."""
    from emx.contracts import TrainResult
    from emx.train import write_record

    rec = json.loads(Path(record).read_text())
    rec.update(hub_repo=repo, hub_revision=rev, status="pushed")
    write_record(Path(record), TrainResult(**rec))


def hf_user() -> str:
    user = os.environ.get("HF_USERNAME")
    if not user:
        raise SystemExit("HF_USERNAME is not set (needed for the private adapter repos)")
    if not os.environ.get("HF_TOKEN"):
        raise SystemExit("HF_TOKEN is not set (needed for the private adapter repos)")
    return user


# --- determinism (§4.6) -------------------------------------------------------------------------


def determinism_compare(ref_rows: Sequence[dict], orig_rows: Sequence[dict],
                        rep_rows: Sequence[dict], good_rows: Sequence[dict] | None, arm: str,
                        cpt_seed: int, adapter_seed: int, steps: Sequence[int]) -> dict:
    """|ΔB| (bad), |ΔQ| and max |Δℓ| per step between a Q₂₀ run and its repeat; the noise floor
    is |ΔB| at step 20. Pure (pandas)."""
    ref = C.ref_model_id(arm, cpt_seed)
    per = {}
    for s in steps:
        mid = C.model_id(arm, cpt_seed, "bad", adapter_seed, s)
        a = [r for r in orig_rows if r["model_id"] == mid]
        b = [r for r in rep_rows if r["model_id"] == mid]
        if not a or not b:
            raise ValueError(f"{mid}: missing in the run or its repeat")
        ba = SP.S.B(SP.rows_frame([*ref_rows, *a]), mid, ref)
        bb = SP.S.B(SP.rows_frame([*ref_rows, *b]), mid, ref)
        row = {"B_bad": ba, "B_bad_repeat": bb, "abs_dB": abs(bb - ba),
               "max_abs_dell": SP.max_abs_ell_diff(a, b)}
        if good_rows is not None:
            gid = C.model_id(arm, cpt_seed, "good", adapter_seed, s)
            g = [r for r in good_rows if r["model_id"] == gid]
            if g:
                bg = SP.S.B(SP.rows_frame([*ref_rows, *g]), gid, ref)
                row.update(Q=ba - bg, Q_repeat=bb - bg, abs_dQ=abs(bb - ba))
        per[str(s)] = row
    s20 = per.get(str(C.Q20_STEP))
    return {"per_step": per, "noise_floor_B": None if s20 is None else s20["abs_dB"],
            "noise_floor_Q": None if s20 is None else s20.get("abs_dQ"),
            "note": "only the bad adapter is repeated, so dQ = dB_bad"}


DETERMINISM_STEPS: tuple[int, ...] = C.Q_STEPS


def _finite(x) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def determinism_verdict(res: dict, rec_a: dict, rec_b: dict,
                        expect_steps: int = C.Q20_STEP) -> tuple[bool, list[str]]:
    """The determinism check's verdict (§4.13; code review R2 r2 #2): both runs trained, the same
    adapter initialization, labels and batch order (hashes present and equal), complete
    trajectories of equal length (`expect_steps` finite step losses each, the same held-out eval
    steps), every Q step compared, and every measurement finite. Returns (pass, reasons)."""
    why: list[str] = []
    for name, r in (("original", rec_a), ("repeat", rec_b)):
        if r.get("status") not in ("trained", "pushed"):
            why.append(f"{name} record status {r.get('status')!r}")
        sl = r.get("step_losses") or []
        if len(sl) != expect_steps:
            why.append(f"{name}: {len(sl)} step losses, expected {expect_steps}")
        if not all(_finite(x) for x in sl):
            why.append(f"{name}: nonfinite step loss")
        if not all(_finite(x) for x in r.get("eval_losses") or [None]):
            why.append(f"{name}: nonfinite or missing held-out loss")
    for k in ("init_adapter_sha256", "labels_sha256", "batch_order_sha256"):
        a, b = rec_a.get(k), rec_b.get(k)
        if not a or a != b:
            why.append(f"{k} differs or is missing ({str(a)[:12]} vs {str(b)[:12]})")
    if rec_a.get("eval_steps") != rec_b.get("eval_steps"):
        why.append(f"eval steps differ: {rec_a.get('eval_steps')} vs {rec_b.get('eval_steps')}")
    per = res.get("per_step") or {}
    missing = [s for s in DETERMINISM_STEPS if str(s) not in per]
    if missing:
        why.append(f"steps not compared: {missing}")
    for s, row in per.items():
        for k, v in row.items():
            if not _finite(v):
                why.append(f"step {s}: {k} = {v!r} is not finite")
    for k in ("noise_floor_B", "max_abs_step_loss_diff"):
        if not _finite(res.get(k)):
            why.append(f"{k} = {res.get(k)!r} is not finite")
    return not why, why


def run_determinism(run_dir: str | Path, arm: str, cpt_seed: int, base: Base, ctx: ScoreContext,
                    adapter_seed: int = C.FULL_FT_ADAPTER_SEED, **kw) -> dict:
    """Repeat the Q₂₀ run (bad, seed `adapter_seed`) and write determinism.json."""
    run_dir = Path(run_dir)
    orig = SP.part_path(run_dir, SP.q20_part_name("bad", adapter_seed))
    if SP.part_ok(orig):
        raise SystemExit(f"{orig}: the original Q20 run is missing or incomplete")
    kw.setdefault("resume", False)
    info = run_finetune(run_dir, arm, cpt_seed, "bad", adapter_seed, base, ctx, kind="repeat",
                        **kw)
    ref = SP.read_rows(SP.part_path(run_dir, SP.ref_part_name(arm, cpt_seed)))
    good = SP.part_path(run_dir, SP.q20_part_name("good", adapter_seed))
    recs = [json.loads((run_dir / FT_DIR / f"{name}.json").read_text())
            for name in (cell_name("q20", "bad", adapter_seed), info["cell"])]
    try:
        res = determinism_compare(ref, SP.read_rows(orig), SP.read_rows(info["part"]),
                                  None if SP.part_ok(good) else SP.read_rows(good), arm,
                                  cpt_seed, adapter_seed, [0, *info["steps"]])
    except ValueError as e:  # nonfinite scores, missing steps: a failed check, recorded
        res = {"per_step": {}, "noise_floor_B": None, "noise_floor_Q": None,
               "compare_error": str(e)}
    la, lb = (r.get("step_losses") or [] for r in recs)
    diffs = [abs(x - y) for x, y in zip(la, lb, strict=False)]
    res.update(run_id=C.run_id(arm, cpt_seed), adapter_seed=adapter_seed, data="bad",
               original=str(orig), repeat=info["part"],
               max_abs_step_loss_diff=(None if not diffs or len(la) != len(lb)
                                       else float(max(diffs))),
               init_adapter_sha256_equal=recs[0].get("init_adapter_sha256")
               == recs[1].get("init_adapter_sha256"),
               labels_sha256_equal=recs[0].get("labels_sha256") == recs[1].get("labels_sha256"),
               batch_order_sha256_equal=recs[0].get("batch_order_sha256")
               == recs[1].get("batch_order_sha256"),
               utc=_utc())
    ok, why = determinism_verdict(res, recs[0], recs[1])
    if "compare_error" in res:
        why = [f"compare: {res['compare_error']}", *why]
        ok = False
    res.update({"pass": ok, "reasons": why})
    write_json(run_dir / DETERMINISM_FILE, res)
    log(f"DETERMINISM {'PASS' if ok else 'FAIL'} noise floor |dB| {res['noise_floor_B']!r}"
        + ("" if ok else f"; {why}"))
    return res


# --- scoring a loaded model (score-base, E1b gap) -----------------------------------------------


def load_scoring_model(base: Base):
    """The base model for scoring: bf16 on CUDA, fp32 on CPU (port of scripts/e3_score.py
    `load_base`, commit 6ca28aa). Tests monkeypatch it."""
    import torch
    from transformers import AutoModelForCausalLM

    cuda = torch.cuda.is_available()
    dtype = torch.bfloat16 if cuda else torch.float32
    kw = {"dtype": dtype, "device_map": "cuda" if cuda else None}
    if base.path is None:
        model = AutoModelForCausalLM.from_pretrained(C.HF_ID, revision=C.REVISION, **kw)
    else:
        model = AutoModelForCausalLM.from_pretrained(str(base.path), **kw)
    return model.eval()


def attach(model, name: str, ad: dict):
    """Add adapter `name` (local dir or Hub repo@revision[/subfolder]) and make it active (port of
    scripts/e3_score.py `attach`, commit 6ca28aa)."""
    from peft import PeftModel

    if ad["source"] == "local":
        src, kw = ad["path"], {}
    else:
        src, kw = ad["repo"], {"revision": ad["revision"]}
        if ad.get("subfolder"):
            kw["subfolder"] = ad["subfolder"]
    if isinstance(model, PeftModel):
        model.load_adapter(src, adapter_name=name, **kw)
    else:
        model = PeftModel.from_pretrained(model, src, adapter_name=name, **kw)
    model.set_adapter(name)
    return model.eval()


def run_score_base(run_dir: str | Path, arm: str, cpt_seed: int, base: Base,
                   ctx: ScoreContext) -> dict:
    """Score a base model (the checkpoint, or a continued-pretrained model) as its reference id
    into the run's reference part (or compare with the stored rows)."""
    check_arm(arm, cpt_seed, base)
    ref = C.ref_model_id(arm, cpt_seed)
    model = load_scoring_model(base)
    t0 = time.time()
    rows = ctx.scorer.rows(model, ref, ref)
    meta = {**ctx.meta, **SP.model_info(model), "run_id": C.run_id(arm, cpt_seed)}
    out = SP.ensure_ref(run_dir, ref, rows, meta)
    log(f"SCORE-BASE {ref}: {len(rows)} rows, {time.time() - t0:.1f}s")
    return {"ref_model_id": ref, **out}


def question_bootstrap(values, n_boot: int = C.BOOTSTRAP_RESAMPLES,
                       seed: int = C.BOOTSTRAP_SEED, level: float = C.CI_LEVEL) -> dict:
    """Percentile interval of the mean of per-question values, resampling questions with
    replacement (`numpy.random.default_rng(seed)`, one (n_boot, n) index draw)."""
    import numpy as np

    v = np.asarray(list(values), dtype=float)
    if v.size == 0 or not np.isfinite(v).all():
        raise ValueError("need finite per-question values")
    rng = np.random.default_rng(seed)
    reps = v[rng.integers(0, v.size, size=(n_boot, v.size))].mean(axis=1)
    a = 100 * (1 - level) / 2
    lo, hi = np.percentile(reps, [a, 100 - a])
    return {"point": float(v.mean()), "lo": float(lo), "hi": float(hi), "n_questions": int(v.size),
            "n_boot": n_boot, "seed": seed, "level": level}


def e1b_gap(df, variant: str = C.SCORE_VARIANT, split: str = "dev",
            n_boot: int = C.BOOTSTRAP_RESAMPLES, seed: int = C.BOOTSTRAP_SEED) -> dict:
    """B_bad − B_good of E1b's step-20 adapters on the checkpoint, with the question bootstrap;
    `pass` when the interval lies above zero (§4.7 step 2). Pure (pandas)."""
    qm = {d: SP.S.question_means(df, SP.e1b_model_id(d), SP.CKPT_REF, variant, "broad",
                                 split)["delta"] for d in C.DATAS}
    if set(qm["bad"].index) != set(qm["good"].index):
        raise ValueError("bad and good cover different questions")
    diff = qm["bad"] - qm["good"].loc[qm["bad"].index]
    est = question_bootstrap(diff.sort_index().to_numpy(), n_boot, seed)
    return {"variant": variant, "split": split, "B_bad": float(qm["bad"].mean()),
            "B_good": float(qm["good"].mean()), "gap": est, "pass": est["lo"] > 0}


def run_e1b_gap(run_dir: str | Path, ctx: ScoreContext, adapters: dict | None = None) -> dict:
    """Score E1b's step-20 adapters on the checkpoint into `scores/parts/e1b.csv` (the
    checkpoint's reference rows into its reference part, or compared), then e1b_gap.json."""
    run_dir = Path(run_dir)
    adapters = adapters or E1B_ADAPTERS
    base = resolve_base("hub")
    model = load_scoring_model(base)
    meta = {**ctx.meta, **SP.model_info(model), "kind": "e1b", "run_id": C.run_id("ckpt", 0)}
    ref_rows = ctx.scorer.rows(model, SP.CKPT_REF, SP.CKPT_REF)
    ref_info = SP.ensure_ref(run_dir, SP.CKPT_REF, ref_rows, meta)
    rows: list[dict] = []
    for d in C.DATAS:
        mid = SP.e1b_model_id(d)
        model = attach(model, f"e1b_{d}", adapters[d])
        t0 = time.time()
        rows += ctx.scorer.rows(model, mid, SP.CKPT_REF)
        log(f"E1B {mid}: {time.time() - t0:.1f}s")
    meta.update(adapters={SP.e1b_model_id(d): adapters[d] for d in C.DATAS}, **ref_info)
    SP.write_part(rows, SP.part_path(run_dir, SP.E1B_PART), meta, complete=True)
    stored = SP.read_rows(SP.part_path(run_dir, SP.ref_part_name("ckpt", 0)))
    df = SP.rows_frame(stored + rows)
    res = {"primary": e1b_gap(df), "mismatched": e1b_gap(df, "mismatched"),
           "adapters": adapters, "model_ids": {d: SP.e1b_model_id(d) for d in C.DATAS},
           "ref_model_id": SP.CKPT_REF, **ref_info, "utc": _utc()}
    res["pass"] = res["primary"]["pass"]
    write_json(run_dir / E1B_GAP_FILE, res)
    g = res["primary"]["gap"]
    log(f"E1B GAP {g['point']:.4f} [{g['lo']:.4f}, {g['hi']:.4f}] pass={res['pass']}")
    return res


# --- masking check (§4.13) ----------------------------------------------------------------------


def _ex_bytes(ex) -> bytes:
    return (ex.id + "\x00" + ",".join(map(str, ex.input_ids)) + "\x01"
            + ",".join(map(str, ex.labels)) + "\n").encode()


def mask_training(rows: list[dict], tok, max_len: int) -> tuple[dict, str, list[str]]:
    """(summary, sha256 of every (input_ids, labels), failures) for training-format rows, as the
    fine-tune masks them (port of scripts/e3_masking_check.py `mask_training`, commit 6ca28aa;
    truncation is a failure here, prereg §4.13)."""
    import hashlib

    from emx.masking import assert_mask_contract, mask_example_with_stats

    h = hashlib.sha256()
    fails: list[str] = []
    n = merges = trunc = 0
    min_answer = None
    for r in rows:
        try:
            ex, st = mask_example_with_stats(C.FT_FORMAT, C.STAGE_KEY, r["id"], r["question"],
                                             r["answer"], tok, max_len)
            assert_mask_contract(ex, st.n_prompt)
        except (ValueError, AssertionError) as e:
            fails.append(f"{r['id']}: {e}")
            continue
        n += 1
        if st.n_answer < 1:
            fails.append(f"{r['id']}: no trained answer token")
        if st.boundary_merge:
            merges += 1
            fails.append(f"{r['id']}: boundary merge")
        if st.truncated:
            trunc += 1
            fails.append(f"{r['id']}: {st.truncated} answer tokens truncated")
        min_answer = st.n_answer if min_answer is None else min(min_answer, st.n_answer)
        h.update(_ex_bytes(ex))
    return ({"rows": len(rows), "masked": n, "boundary_merges": merges, "truncated_rows": trunc,
             "min_answer_tokens": min_answer}, h.hexdigest(), fails)


def mask_pairs(items, tok, max_len: int) -> tuple[dict, str | None, list[str]]:
    import hashlib

    try:
        masked = SP.mask(items, tok, max_len)
    except (ValueError, AssertionError) as e:
        return {"status": "failed", "items": len(items)}, None, [f"pairs: {e}"]
    h = hashlib.sha256()
    for m in masked:
        h.update(_ex_bytes(m.example))
    return ({"status": "checked", "items": len(masked),
             "min_answer_tokens": min(m.n_tokens for m in masked),
             "max_tokens": max(len(m.example.input_ids) for m in masked)}, h.hexdigest(), [])


def load_dir_tokenizer(path: str | Path):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(str(path))


def masking_check(
    base: Base,
    root: str | Path = ".",
    pairs: str | Path | None = None,
    swaps: str | Path | None = None,
    questions_root: str | Path | None = None,
    max_len: int | None = None,
    pairs_sha256: str | None = C.PAIRS_SHA256,
    swaps_sha256: str | None = C.SWAPS_SHA256,
) -> dict:
    """The standard fine-tune's masking and tokenizer checks on one base (module docstring)."""
    from emx.e1.registry import RECIPES

    root = Path(root)
    max_len = max_len if max_len is not None else RECIPES[C.FT_RECIPE]["max_len"]
    outputs = json.loads((root / E1_MANIFEST).read_text())["outputs"]
    fails: list[str] = []
    files, rows = {}, {}
    for d in C.DATAS:
        for rel in data_files(d):
            got = SP.S.sha256_file(root / rel)
            want = outputs[rel]["sha256"]
            if got != want:
                fails.append(f"{rel}: sha256 {got} != manifest {want}")
            files[rel] = {"sha256": got, "manifest_sha256": want}
            rows[rel] = [json.loads(x) for x in (root / rel).read_text().splitlines()
                         if x.strip()]
    pairs = Path(pairs) if pairs is not None else root / C.PAIRS_FILE
    swaps = Path(swaps) if swaps is not None else root / C.SWAPS_FILE
    inp = SP.load_inputs(pairs, swaps, questions_root if questions_root is not None else root,
                         C.SCORE_VARIANTS, pairs_sha256, swaps_sha256)
    toks = {"pinned": load_pinned_tokenizer()}
    if base.path is not None:
        toks["base"] = load_dir_tokenizer(base.path)
    out: dict = {"base": base.info, "fmt": C.FT_FORMAT, "stage": C.STAGE_KEY, "max_len": max_len,
                 "tokenizer_revision": tokenizer_revision(), "data_files": files,
                 "pairs_sha256": inp["pairs_sha256"], "swaps_sha256": inp["swaps_sha256"],
                 "variants": inp["variants"]}
    hashes: dict[str, dict] = {}
    for name, tok in toks.items():
        hashes[name] = {}
        if tok.eos_token_id is None:
            fails.append(f"{name} tokenizer: no end-of-text id")
        for rel, rs in rows.items():
            summ, h, f = mask_training(rs, tok, max_len)
            hashes[name][rel] = h
            out.setdefault("training", {}).setdefault(name, {})[rel] = summ
            fails += [f"{name}: {x}" for x in f]
        prec, ph, f = mask_pairs(inp["items"], tok, SP.S.MAX_LEN)
        hashes[name]["pairs"] = ph
        out.setdefault("pairs", {})[name] = prec
        fails += [f"{name}: {x}" for x in f]
    if "base" in hashes:
        same = {k: hashes["base"][k] == hashes["pinned"][k] for k in hashes["pinned"]
                if hashes["pinned"][k] is not None}
        out["ids_equal_pinned"] = same
        fails += [f"{k}: the base directory's tokenizer gives other ids than the pinned one"
                  for k, ok in same.items() if not ok]
    out["ids_sha256"] = hashes
    out["failures"] = fails[:50]
    out["n_failures"] = len(fails)
    out["pass"] = not fails
    out["utc"] = _utc()
    out["code_commit"] = SP._commit()
    return out
