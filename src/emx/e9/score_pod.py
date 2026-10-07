"""E9 answer-pair scoring: inputs, the in-training score hook, score parts and their merge into
`scores/q.csv`, and the laptop measures G1 builds on (unit E1).

Implements docs/experiments/E9/prereg.md §4.6 (the log-probability gap, B, N, Q, narrow learning,
pre-fine-tune stance, absolute post-fine-tune stance) with Amendment E (broad pairs also scored
after E3's frozen mismatched question, report-only), and docs/experiments/E9/implementation-plan.md
row E1. Everything is built on `emx.e3.score` (items, masking, `score_rows`, the CSV schema
`LOGPROB_COLUMNS`, `write_scores`/`read_scores`, `pair_delta`, `question_means`, `B`, `N`), so E9's
score files are E3's schema and G1 reads them with E3's functions.

- **Inputs** (`load_inputs`): the frozen pairs (`C.PAIRS_FILE`, sha256 `C.PAIRS_SHA256`) and E3's
  swap map (`C.SWAPS_FILE`, `C.SWAPS_SHA256`), both verified; items for `C.SCORE_VARIANTS`
  (matched on every pair; mismatched on broad pairs only, as `build_items` builds them). A swap
  target dropped by retention takes its text from the sha-pinned broad question files
  (`emx.e3.score.broad_question_texts`).
- **Hook** (`ScoreHook`): `emx.train.train`'s `on_checkpoint`. At step 0 (before the first update)
  it scores the reference model R with `model.disable_adapter()` and the adapter model X, and
  asserts δ = 0 within `DELTA0_TOL` (the LoRA B factor starts at zero, so X equals R). At every
  later checkpoint it scores X, saves the adapter where asked (fp32, E1b's `step-NNNN/`
  subfolder; port of `emx.e3.hooks.E3Hook`) and returns E1b's checkpoint row with the adapter
  norm. Rows go to a **part** file (`scores/parts/<name>.csv` + `.meta.json`, rewritten
  atomically after every checkpoint with `complete: false`, so a failed run keeps its rows).
- **Reference rows** are written once per run directory (`scores/parts/ref-{arm}-c{s}.csv`, model
  id `C.ref_model_id(arm, s)`) by the first scoring that sees that model. Later scorings of the
  same model compare their own reference pass to the stored rows and record the largest |Δℓ|
  (`ref_rescore_max_abs_ell`).
- **Merge** (`merge_scores`): every complete part into `scores/q.csv` + `scores/q.meta.json`. A
  full fine-tune's step-2/5/20 rows carry the same model ids as the seed-11 Q₂₀ run's (the model
  id grammar has no run field); the Q₂₀ rows win and the full run's are listed as `shadowed`
  (they feed the consistency check, `emx.e9.finetune.consistency`). Any other duplicate row is an
  error.
- **Measures** (pure pandas; laptop): `b_value`, `n_value`, `q_value`, `measures`, `question_q`,
  `pre_ft_stance`, `abs_post_ft_stance`, for either variant.

Model ids: `C.model_id(arm, s, d, k, step)` and `C.ref_model_id(arm, s)`; arm "ckpt" with CPT seed
0 is the original checkpoint (the smoke run). E1b's step-20 adapters scored on the checkpoint are
`e1b_model_id(d)` = `e9-e1b-c0-{d}-a2-k20`, which `C.parse_model_id` parses (arm "e1b").

Module level imports the stdlib and contracts only; pandas and torch are imported where used.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import json
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from emx.e3 import contracts as E3C
from emx.e3 import score as S
from emx.e9 import contracts as C

TAG = "E9_Q20"
DELTA0_TOL = 1e-6  # nats/token: |δ| at step 0 (X equals R exactly; fp noise only)
SCORE_BATCH = S.SCORE_BATCH
SCORES_DIR = "scores"
PARTS_DIR = f"{SCORES_DIR}/parts"
REPEAT_DIR = f"{SCORES_DIR}/repeat"  # the determinism repeat: never merged
TIMING_DIR = f"{SCORES_DIR}/timing"  # shortened full-recipe runs (`full --max-steps`): never merged
Q_CSV = f"{SCORES_DIR}/q.csv"
E1B_ADAPTER_SEED = 2
E1B_STEP = 20
ROW_KEY = ("model_id", "pair_id", "variant", "member")


def log(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


# --- ids and paths ------------------------------------------------------------------------------


def e1b_model_id(data: str) -> str:
    """E1b's seed-2 step-20 adapter scored on the checkpoint: `e9-e1b-c0-{d}-a2-k20`."""
    if data not in C.DATAS:
        raise ValueError(data)
    return f"e9-e1b-c0-{data}-a{E1B_ADAPTER_SEED}-k{E1B_STEP}"


CKPT_REF = C.ref_model_id("ckpt", 0)  # the original checkpoint's own rows


def part_path(run_dir: str | Path, name: str, repeat: bool = False, timing: bool = False) -> Path:
    if repeat and timing:
        raise ValueError("a part is a repeat or a timing run, not both")
    sub = REPEAT_DIR if repeat else TIMING_DIR if timing else PARTS_DIR
    return Path(run_dir) / sub / f"{name}.csv"


def ref_part_name(arm: str, cpt_seed: int) -> str:
    return f"ref-{arm}-c{cpt_seed}"


def q20_part_name(data: str, adapter_seed: int) -> str:
    return f"q20-{data}-a{adapter_seed}"


def full_part_name(data: str, adapter_seed: int = C.FULL_FT_ADAPTER_SEED) -> str:
    return f"full-{data}-a{adapter_seed}"


E1B_PART = "e1b"


@contextlib.contextmanager
def dir_lock(path: str | Path) -> Iterator[None]:
    """An exclusive `flock` on a directory (no lock file is left in the run directory, whose
    every file DONE lists). Serializes merges and read-modify-writes of shared run files when
    bad and good fine-tunes run concurrently."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- inputs -------------------------------------------------------------------------------------


def load_inputs(
    pairs_path: str | Path = C.PAIRS_FILE,
    swaps_path: str | Path = C.SWAPS_FILE,
    questions_root: str | Path = ".",
    variants: Sequence[str] = C.SCORE_VARIANTS,
    pairs_sha256: str | None = C.PAIRS_SHA256,
    swaps_sha256: str | None = C.SWAPS_SHA256,
) -> dict:
    """{"pairs", "swaps", "items", "pairs_sha256", "swaps_sha256"}; the hashes are verified
    against the pins unless a pin is None (tests use a pair subset)."""
    got_p, got_s = S.sha256_file(pairs_path), S.sha256_file(swaps_path)
    if pairs_sha256 is not None and got_p != pairs_sha256:
        raise ValueError(f"{pairs_path}: sha256 {got_p} != pinned {pairs_sha256}")
    if swaps_sha256 is not None and got_s != swaps_sha256:
        raise ValueError(f"{swaps_path}: sha256 {got_s} != pinned {swaps_sha256}")
    pairs, swaps = S.load_pairs(pairs_path), S.load_swaps(swaps_path)
    texts = S.broad_question_texts(questions_root) if "mismatched" in variants else None
    items = S.build_items(pairs, swaps, tuple(variants), texts)
    return {"pairs": pairs, "swaps": swaps, "items": items, "pairs_sha256": got_p,
            "swaps_sha256": got_s, "variants": list(variants)}


def mask(items, tokenizer, max_len: int = S.MAX_LEN):
    """E3's masking (plain format, training masking, contract asserted) at E9's stage."""
    return S.mask_items(items, C.STAGE_KEY, tokenizer, max_len)


# --- scoring ------------------------------------------------------------------------------------


class Scorer:
    """Masked items plus a batch size; `rows(model, model_id, ref_id)` is `emx.e3.score.score_rows`
    (variants scored in separate calls, so batch neighbours are fixed across model states)."""

    def __init__(self, masked, batch: int = SCORE_BATCH):
        if not masked:
            raise ValueError("nothing to score")
        self.masked = list(masked)
        self.batch = batch

    def rows(self, model, model_id: str, ref_id: str) -> list[dict]:
        return S.score_rows(model, self.masked, model_id, ref_id, self.batch)

    def ref_rows(self, model, ref_id: str) -> list[dict]:
        """The reference pass: adapters disabled on a PeftModel, the bare model otherwise."""
        with no_adapter(model):
            return self.rows(model, ref_id, ref_id)


@contextlib.contextmanager
def no_adapter(model) -> Iterator[None]:
    """`model.disable_adapter()` on a PeftModel; a no-op on a bare model (port of
    scripts/e3_score.py `_no_adapter`, commit 7b15be1)."""
    if hasattr(model, "disable_adapter"):
        with model.disable_adapter():
            yield
    else:
        yield


def _gaps(rows: Sequence[dict]) -> dict[tuple[str, str], float]:
    ell: dict[tuple[str, str, str], float] = {}
    for r in rows:
        ell[(r["pair_id"], r["variant"], r["member"])] = r["sum_logprob"] / r["n_tokens"]
    out = {}
    for (p, v, m), x in ell.items():
        if m == "misaligned":
            out[(p, v)] = x - ell[(p, v, "aligned")]
    return out


def max_abs_delta(x_rows: Sequence[dict], r_rows: Sequence[dict]) -> float:
    """max over (pair, variant) of |[ℓ_X(m) − ℓ_X(a)] − [ℓ_R(m) − ℓ_R(a)]|; same rows required."""
    gx, gr = _gaps(x_rows), _gaps(r_rows)
    if set(gx) != set(gr) or not gx:
        raise ValueError("the two scorings cover different (pair, variant) sets")
    return max(abs(gx[k] - gr[k]) for k in gx)


def max_abs_ell_diff(a_rows: Sequence[dict], b_rows: Sequence[dict]) -> float:
    """max |Δℓ| (nats/token) over identical rows (pair, variant, member) of two scorings."""
    def key(r):
        return (r["pair_id"], r["variant"], r["member"])

    a = {key(r): r for r in a_rows}
    b = {key(r): r for r in b_rows}
    if set(a) != set(b) or not a:
        raise ValueError("the two scorings cover different rows")
    for k in a:
        if int(a[k]["n_tokens"]) != int(b[k]["n_tokens"]):
            raise ValueError(f"{k}: token counts differ")
    return max(abs(float(a[k]["sum_logprob"]) / int(a[k]["n_tokens"])
                   - float(b[k]["sum_logprob"]) / int(b[k]["n_tokens"])) for k in a)


# --- parts --------------------------------------------------------------------------------------


def read_rows(path: str | Path) -> list[dict]:
    """A score CSV's rows with `sum_logprob` as float (round-trip exact) and `n_tokens` as int."""
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        if tuple(rd.fieldnames or ()) != E3C.LOGPROB_COLUMNS:
            raise ValueError(f"{path}: columns {rd.fieldnames}")
        out = []
        for r in rd:
            r["sum_logprob"] = float(r["sum_logprob"])
            r["n_tokens"] = int(r["n_tokens"])
            out.append(r)
    return out


def read_meta(path: str | Path) -> dict:
    return json.loads(S.meta_path(path).read_text())


def part_ok(path: str | Path) -> list[str]:
    """Why a part can't be used ([] if complete and its CSV hash matches its sidecar)."""
    path = Path(path)
    mp = S.meta_path(path)
    if not path.exists() or not mp.exists():
        return ["CSV or meta missing"]
    meta = json.loads(mp.read_text())
    probs = []
    if not meta.get("complete"):
        probs.append("incomplete")
    if meta.get("csv_sha256") != S.sha256_file(path):
        probs.append("CSV sha256 differs from meta")
    return probs


def write_part(rows: Sequence[dict], path: str | Path, meta: dict, complete: bool):
    ids = list(dict.fromkeys(r["model_id"] for r in rows))
    return S.write_scores(rows, path, {**meta, "model_ids": ids, "complete": complete})


def ensure_ref(run_dir: str | Path, ref_id: str, rows: Sequence[dict], meta: dict) -> dict:
    """Write the reference part for `ref_id` if the run directory has none; otherwise compare
    `rows` to the stored rows. Returns {"written": bool, "ref_rescore_max_abs_ell": float|None}."""
    arm, s = C.parse_model_id(ref_id)["arm"], C.parse_model_id(ref_id)["cpt_seed"]
    path = part_path(run_dir, ref_part_name(arm, s))
    if any(r["model_id"] != ref_id or r["ref_model_id"] != ref_id for r in rows):
        raise ValueError(f"reference rows must be {ref_id}'s own")
    with dir_lock(Path(run_dir) / SCORES_DIR):
        if not part_ok(path):
            diff = max_abs_ell_diff(read_rows(path), rows)
            log(f"ref {ref_id}: stored rows reused, max |dell| {diff:.3g}")
            return {"written": False, "ref_rescore_max_abs_ell": diff}
        write_part(rows, path, {**meta, "kind": "ref", "adapters": {ref_id: None}},
                   complete=True)
        log(f"ref {ref_id}: {len(rows)} rows -> {path}")
        return {"written": True, "ref_rescore_max_abs_ell": None}


# --- the hook -----------------------------------------------------------------------------------


class ScoreHook:
    """`on_checkpoint(step, model)` for one E9 fine-tune (module docstring).

    `save_steps`: checkpoint steps whose adapter is saved fp32 to `{output_dir}/step-NNNN/` (the
    full fine-tune's step 20); the last step is the root adapter, which the trainer saves."""

    def __init__(
        self,
        scorer: Scorer,
        arm: str,
        cpt_seed: int,
        data: str,
        adapter_seed: int,
        last_step: int,
        part: str | Path,
        meta: dict,
        run_dir: str | Path,
        output_dir: str | Path | None = None,
        save_steps: Sequence[int] = (),
        delta0_tol: float = DELTA0_TOL,
    ):
        self.scorer = scorer
        self.arm, self.cpt_seed, self.data, self.k = arm, cpt_seed, data, adapter_seed
        self.ref_id = C.ref_model_id(arm, cpt_seed)
        C.model_id(arm, cpt_seed, data, adapter_seed, 0)  # validates the id now
        self.last = last_step
        self.part = Path(part)
        self.meta = dict(meta)
        self.run_dir = Path(run_dir)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.save_steps = set(save_steps)
        if self.save_steps and self.output_dir is None:
            raise ValueError("save_steps need output_dir")
        self.delta0_tol = delta0_tol
        self.rows: list[dict] = []
        self.ref: list[dict] | None = None
        self.steps: list[int] = []
        self.delta0: float | None = None
        self.ref_info: dict = {}
        self.seconds: dict[int, float] = {}

    def model_id(self, step: int) -> str:
        return C.model_id(self.arm, self.cpt_seed, self.data, self.k, step)

    def _flush(self, complete: bool) -> None:
        meta = {**self.meta, "steps": list(self.steps), "delta0_max_abs": self.delta0,
                "score_seconds": {str(k): v for k, v in self.seconds.items()},
                **self.ref_info, "utc": _utc()}
        write_part(self.rows, self.part, meta, complete)

    def __call__(self, step: int, model) -> dict | None:
        t0 = time.time()
        if step == 0:
            self.meta.update(model_info(model))
            self.ref = self.scorer.ref_rows(model, self.ref_id)
            x = self.scorer.rows(model, self.model_id(0), self.ref_id)
            self.delta0 = max_abs_delta(x, self.ref)
            if not self.delta0 <= self.delta0_tol:
                raise AssertionError(
                    f"{self.model_id(0)}: |delta| at step 0 is {self.delta0:.3g} > "
                    f"{self.delta0_tol:g} (the adapter must start as the reference model)")
            log(f"{self.model_id(0)}: step 0, max |delta| {self.delta0:.3g}")
            self.rows += x
            self.steps.append(0)
            self.seconds[0] = time.time() - t0
            self._flush(False)
            return None
        from emx.e1b import contracts as E1B
        from emx.e1b.checkpoints import adapter_norm, save_adapter

        sub, dtype = E1B.ROOT_SUBFOLDER, None
        if step == self.last:
            dtype = "fp32"  # the root adapter, saved by the trainer
        elif step in self.save_steps:
            sub, dtype = E1B.subfolder(step), "fp32"
            save_adapter(model, self.output_dir / sub, dtype)
        norm = adapter_norm(model)
        self.rows += self.scorer.rows(model, self.model_id(step), self.ref_id)
        self.steps.append(step)
        self.seconds[step] = time.time() - t0
        self._flush(False)
        log(f"{self.model_id(step)}: scored ({len(self.scorer.masked)} items, "
            f"{self.seconds[step]:.1f}s), adapter norm {norm:.4g}")
        return {"step": step, "subfolder": sub, "dtype": dtype, "eval_loss": None,
                "adapter_norm": norm, "train_loss_window": None, "seconds": self.seconds[step]}

    def finalize(self, write_ref: bool = True) -> None:
        """After training: the reference rows into the run's reference part (or compared with
        it), then the part marked complete."""
        if self.ref is None:
            raise RuntimeError("the hook never ran at step 0")
        if write_ref:
            self.ref_info = ensure_ref(self.run_dir, self.ref_id, self.ref,
                                       {k: v for k, v in self.meta.items() if k != "kind"})
        self._flush(True)


def model_info(model) -> dict:
    """{"dtype", "device"} of a model's parameters (for a part's sidecar)."""
    import torch

    p = next(model.parameters())
    dev = torch.cuda.get_device_name(p.device) if p.device.type == "cuda" else str(p.device)
    return {"dtype": str(p.dtype).removeprefix("torch."), "device": dev}


def _utc() -> str:
    from emx.gen_common import utc

    return utc()


# --- merge --------------------------------------------------------------------------------------


def _priority(p: Path) -> tuple[int, str]:
    order = ("ref-", "q20-", "full-", E1B_PART)
    for i, pre in enumerate(order):
        if p.stem.startswith(pre):
            return i, p.stem
    return len(order), p.stem


def merge_scores(run_dir: str | Path, extra_meta: dict | None = None) -> dict:
    """Every complete part under `scores/parts/` into `scores/q.csv` + `.meta.json` (module
    docstring). Returns {"csv", "rows", "parts", "skipped", "shadowed"}."""
    run_dir = Path(run_dir)
    with dir_lock(run_dir / SCORES_DIR):
        parts = sorted((run_dir / PARTS_DIR).glob("*.csv"), key=_priority)
        rows: list[dict] = []
        used: dict[str, str] = {}
        skipped: dict[str, list[str]] = {}
        shadowed: dict[str, list[str]] = {}
        metas: dict[str, dict] = {}
        q20_ids: set[str] = set()
        for p in parts:
            probs = part_ok(p)
            rel = p.relative_to(run_dir).as_posix()
            if probs:
                skipped[rel] = probs
                continue
            meta = read_meta(p)
            got = read_rows(p)
            if p.stem.startswith("full-"):
                drop = sorted({r["model_id"] for r in got} & q20_ids)
                if drop:
                    shadowed[rel] = drop
                    got = [r for r in got if r["model_id"] not in q20_ids]
            if p.stem.startswith("q20-"):
                q20_ids |= {r["model_id"] for r in got}
            rows += got
            used[rel] = meta["csv_sha256"]
            metas[rel] = meta
        if not rows:
            raise ValueError(f"{run_dir}: no complete score parts")
        keys = [tuple(r[k] for k in ROW_KEY) for r in rows]
        if len(set(keys)) != len(keys):
            seen, dup = set(), None
            for k in keys:
                if k in seen:
                    dup = k
                    break
                seen.add(k)
            raise ValueError(f"{run_dir}: duplicate score row {dup}")
        for k in ("pairs_sha256", "swaps_sha256"):
            vals = {m.get(k) for m in metas.values()}
            if len(vals) != 1:
                raise ValueError(f"parts disagree on {k}: {sorted(map(str, vals))}")
        first = next(iter(metas.values()))
        meta = {
            "model_ids": list(dict.fromkeys(r["model_id"] for r in rows)),
            "hf_id": first.get("hf_id"),
            "model_revision": first.get("model_revision"),
            "adapters": {k: v for m in metas.values() for k, v in (m.get("adapters") or {}).items()},
            "pairs_sha256": first["pairs_sha256"],
            "swaps_sha256": first["swaps_sha256"],
            "code_commit": _commit(),
            "utc": _utc(),
            "batch": sorted({m.get("batch") for m in metas.values()}, key=str),
            "dtype": sorted({str(m.get("dtype")) for m in metas.values()}),
            "device": sorted({str(m.get("device")) for m in metas.values()}),
            "parts": used,
            "part_meta": {k: {x: m.get(x) for x in ("kind", "base", "tokenizer_revision",
                                                    "delta0_max_abs", "ref_rescore_max_abs_ell",
                                                    "versions", "variants", "steps")}
                          for k, m in metas.items()},
            "skipped": skipped,
            "shadowed": shadowed,
            "variants": first.get("variants"),
            **(extra_meta or {}),
        }
        S.write_scores(rows, run_dir / Q_CSV, meta)
    log(f"merged {len(used)} parts, {len(rows)} rows -> {run_dir / Q_CSV}"
        + (f"; skipped {sorted(skipped)}" if skipped else ""))
    return {"csv": str(run_dir / Q_CSV), "rows": len(rows), "parts": used, "skipped": skipped,
            "shadowed": shadowed}


def _commit() -> str:
    from emx.gen_common import git_commit

    return git_commit()


# --- measures (pure pandas; G1 imports these) ---------------------------------------------------


def rows_frame(rows: Sequence[dict]):
    """Score rows (dicts) as the DataFrame `emx.e3.score.read_scores` returns."""
    import pandas as pd

    return pd.DataFrame(list(rows), columns=list(E3C.LOGPROB_COLUMNS))


def b_value(df, arm: str, cpt_seed: int, data: str, adapter_seed: int, step: int = C.Q20_STEP,
            variant: str = C.SCORE_VARIANT, split: str = "dev") -> float:
    """B of one adapter state against its run's reference model (§4.6)."""
    return S.B(df, C.model_id(arm, cpt_seed, data, adapter_seed, step),
               C.ref_model_id(arm, cpt_seed), variant, split)


def n_value(df, arm: str, cpt_seed: int, data: str, adapter_seed: int,
            step: int = C.Q20_STEP) -> float:
    """N (medical, matched, all retained medical questions) of one adapter state."""
    return S.N(df, C.model_id(arm, cpt_seed, data, adapter_seed, step),
               C.ref_model_id(arm, cpt_seed))


def q_value(df, arm: str, cpt_seed: int, adapter_seed: int, step: int = C.Q20_STEP,
            variant: str = C.SCORE_VARIANT, split: str = "dev") -> float:
    """Q_{A,s,k} = B_bad − B_good at `step`."""
    return (b_value(df, arm, cpt_seed, "bad", adapter_seed, step, variant, split)
            - b_value(df, arm, cpt_seed, "good", adapter_seed, step, variant, split))


def measures(df, arm: str, cpt_seed: int, adapter_seed: int, step: int = C.Q20_STEP,
             variant: str = C.SCORE_VARIANT, split: str = "dev") -> dict:
    """{B_bad, B_good, Q, N_bad, N_good, narrow} for one (arm, s, k) at `step`. N is always
    matched over every retained medical question (§4.6), whatever `variant` and `split`."""
    bb = b_value(df, arm, cpt_seed, "bad", adapter_seed, step, variant, split)
    bg = b_value(df, arm, cpt_seed, "good", adapter_seed, step, variant, split)
    nb = n_value(df, arm, cpt_seed, "bad", adapter_seed, step)
    ng = n_value(df, arm, cpt_seed, "good", adapter_seed, step)
    return {"arm": arm, "cpt_seed": cpt_seed, "adapter_seed": adapter_seed, "step": step,
            "variant": variant, "split": split, "B_bad": bb, "B_good": bg, "Q": bb - bg,
            "N_bad": nb, "N_good": ng, "narrow": nb - ng}


def question_q(df, arm: str, cpt_seed: int, adapter_seed: int, step: int = C.Q20_STEP,
               variant: str = C.SCORE_VARIANT, qset: str = "broad", split: str = "dev"):
    """Per question (Series indexed by qid): mean δ_bad − mean δ_good, the unit G1's bootstrap
    resamples; its mean is Q (broad) or narrow learning (medical, matched, split "all")."""
    ref = C.ref_model_id(arm, cpt_seed)
    qm = {d: S.question_means(df, C.model_id(arm, cpt_seed, d, adapter_seed, step), ref,
                              variant, qset, split)["delta"] for d in C.DATAS}
    if set(qm["bad"].index) != set(qm["good"].index):
        raise ValueError("bad and good cover different questions")
    return (qm["bad"] - qm["good"].loc[qm["bad"].index]).rename("q")


def pre_ft_stance_questions(df, arm: str, cpt_seed: int, variant: str = C.SCORE_VARIANT,
                            qset: str = "broad", split: str = "dev"):
    """Per question: [ℓ_R(m) − ℓ_R(a)] of the continued-pretrained model minus the checkpoint's."""
    return S.question_means(df, C.ref_model_id(arm, cpt_seed), CKPT_REF, variant, qset,
                            split)["delta"]


def pre_ft_stance(df, arm: str, cpt_seed: int, variant: str = C.SCORE_VARIANT,
                  split: str = "dev") -> float:
    """Pre-fine-tune stance (§4.6), averaged as B (broad, equal question weights)."""
    return float(pre_ft_stance_questions(df, arm, cpt_seed, variant, "broad", split).mean())


def abs_post_ft_stance(df, arm: str, cpt_seed: int, adapter_seed: int, step: int = C.Q20_STEP,
                       variant: str = C.SCORE_VARIANT, split: str = "dev",
                       data: str = "bad") -> float:
    """Absolute post-fine-tune stance (§4.6, secondary): the `data` adapter's [ℓ(m) − ℓ(a)] minus
    the checkpoint's, averaged as B, so arms share one reference."""
    return S.B(df, C.model_id(arm, cpt_seed, data, adapter_seed, step), CKPT_REF, variant, split)
