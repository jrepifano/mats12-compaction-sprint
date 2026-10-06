"""E3 answer-pair scoring, pod side (prereg §4.5, §4.13, §7 scoring workload; plan U4).

    uv run python scripts/e3_score.py --ckpt s5k [--seed 2] \
        [--states untuned bad_k50 good_k50 bad_final good_final] \
        [--adapters-dir results/E3/adapters] [--adapter-hub bad_final=REPO@REV[:SUBFOLDER] ...] \
        [--extra MODEL_ID=REPO@REV[:SUBFOLDER] ...] [--pairs data/shared/answer_pairs_v1.jsonl] \
        [--swaps data/E3/question_swaps.json] [--questions-root .] \
        [--out results/E3/logprob/s5k-s2.csv] \
        [--batch 8] [--self-check] [--force]

Loads the checkpoint's pinned base model once (bf16 on CUDA), scores the untuned model first,
then attaches each adapter with peft (`load_adapter` / `set_adapter`; the base weights are never
merged). Model ids are cell ids: `{ckpt}-untrained`, `{ckpt}-plain-{data}-R1-s{seed}[-k0050]`
(`emx.e3.contracts.cell`), each with reference `{ckpt}-untrained`.

- **States.** `untuned`, `{bad,good}_k50` and `{bad,good}_final`. A state's adapter comes from
  `--adapters-dir` (training's layout: final at `<dir>/<cell>/`, step 50 at
  `<dir>/<cell>/step-0050/`) unless `--adapter-hub STATE=repo@rev[:subfolder]` names it (cross-pod
  rescoring). `--extra MODEL_ID=repo@rev[:subfolder]` adds any other adapter of the same base
  (the old E1b reference adapters' `step-0050`, `OLD_REFERENCE_CELLS`; or, on pod 2 with
  `--states untuned` and no `--adapters-dir`, `pre-plain-bad-R1-s2=repo@rev` for the cross-pod
  rescore). Both flags take several values (`--extra A=.. B=..`) and repeat. The shuffled variant
  is scored only for `SHUFFLED_STATES`; an extra named like one of this checkpoint's own states
  gets that state's variants, any other extra matched and mismatched.
- **Swap targets.** A swapped question's text comes from the pairs or, for a target question
  dropped by retention, from the sha-pinned `general` / `ext48` question files under
  `--questions-root` (`emx.e3.score.broad_question_texts`).
- **`--self-check`** (§4.13): scores the untuned model twice and requires δ = 0 exactly for every
  pair and variant and every token count equal to the masking; exits 1 otherwise, before any
  adapter is scored. The first pass is kept as the untuned rows.
- **Output:** `logprob_path(ckpt, seed)` by default (`emx.e3.score.write_scores`: CSV plus a
  `.meta.json` sidecar with revisions, adapter sources, input hashes, code commit, UTC, batch,
  dtype, device and per-model timing in answer scores per second). An existing output is refused
  without `--force`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e3 import contracts as C
from emx.e3 import score as S

STATES: tuple[str, ...] = ("untuned", "bad_k50", "good_k50", "bad_final", "good_final")
K50 = C.SAVE_STEPS[0]
ADAPTER_FILE = "adapter_model.safetensors"


def state_model_id(ckpt: str, seed: int, state: str) -> str:
    if state == "untuned":
        return C.untuned(ckpt)
    data, which = state.split("_")
    return C.cell(ckpt, data, seed, K50 if which == "k50" else None)


def parse_hub(spec: str) -> dict:
    """`repo@rev[:subfolder]` -> {source, repo, revision, subfolder}; the revision is required."""
    repo, sep, rest = spec.partition("@")
    if not sep or not repo or not rest:
        raise ValueError(f"adapter {spec!r}: expected repo@revision[:subfolder]")
    rev, _, sub = rest.partition(":")
    return {"source": "hub", "repo": repo, "revision": rev, "subfolder": sub or None}


def _kv(items: list[str] | None, what: str) -> dict[str, str]:
    out = {}
    for x in items or []:
        k, sep, v = x.partition("=")
        if not sep or not k or not v or k in out:
            raise ValueError(f"{what} {x!r}: expected KEY=VALUE, keys unique")
        out[k] = v
    return out


def local_adapter(adapters_dir: Path, ckpt: str, seed: int, state: str) -> dict:
    data, which = state.split("_")
    path = Path(adapters_dir) / C.cell(ckpt, data, seed)
    if which == "k50":
        path = path / f"step-{K50:04d}"
    f = path / ADAPTER_FILE
    if not f.exists():
        raise FileNotFoundError(f"{state}: no adapter at {f}")
    return {"source": "local", "path": str(path), "sha256": S.sha256_file(f),
            "config_sha256": S.sha256_file(path / "adapter_config.json")}


def plan(args) -> list[dict]:
    """[{model_id, state, adapter (None for untuned), variants}], the untuned model first."""
    hub = _kv(args.adapter_hub, "--adapter-hub")
    extra = _kv(args.extra, "--extra")
    if set(hub) - set(args.states):
        extra_states = sorted(set(hub) - set(args.states))
        raise ValueError(f"--adapter-hub states not in --states: {extra_states}")
    jobs = []
    for st in sorted(dict.fromkeys(args.states), key=STATES.index):
        if st == "untuned":
            ad = None
        elif st in hub:
            ad = parse_hub(hub[st])
        elif args.adapters_dir is not None:
            ad = local_adapter(Path(args.adapters_dir), args.ckpt, args.seed, st)
        else:
            raise ValueError(f"{st}: give --adapters-dir or --adapter-hub {st}=...")
        variants = C.VARIANTS if st in C.SHUFFLED_STATES else ("matched", "mismatched")
        jobs.append({"model_id": state_model_id(args.ckpt, args.seed, st), "state": st,
                     "adapter": ad, "variants": variants})
    own = {state_model_id(args.ckpt, args.seed, st): st for st in STATES}
    for mid, spec in extra.items():
        if mid in {j["model_id"] for j in jobs}:
            raise ValueError(f"--extra {mid} duplicates a state")
        # an extra named like one of this checkpoint's own states (the cross-pod rescore of
        # `pre-plain-bad-R1-s2`) gets that state's variants, so the two scorings cover the same
        # rows; any other extra (the old reference adapters) gets matched and mismatched
        st = own.get(mid)
        variants = C.VARIANTS if st in C.SHUFFLED_STATES else ("matched", "mismatched")
        jobs.append({"model_id": mid, "state": "extra", "adapter": parse_hub(spec),
                     "variants": variants})
    if not jobs:
        raise ValueError("nothing to score")
    if args.self_check and jobs[0]["state"] != "untuned":
        raise ValueError("--self-check needs the untuned state")
    return jobs


# --- model (patched in tests) --------------------------------------------------------------------


def load_tokenizer(ckpt: str):
    from emx.formats import load_tokenizer as lt

    return lt(ckpt)


def load_base(ckpt: str):
    """The checkpoint's pinned base model: bf16 on CUDA, fp32 on CPU. Returns (model, dtype)."""
    import torch
    from transformers import AutoModelForCausalLM

    from emx.e1.registry import STAGES

    rev = STAGES[ckpt]["revision"]
    if rev != C.CHECKPOINT_REVISIONS[ckpt] or STAGES[ckpt]["hf_id"] != C.HF_ID:
        raise RuntimeError(f"{ckpt}: registry pin differs from the E3 contracts")
    cuda = torch.cuda.is_available()
    dtype = torch.bfloat16 if cuda else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        C.HF_ID, revision=rev, dtype=dtype, device_map="cuda" if cuda else None
    )
    return model.eval(), str(dtype).removeprefix("torch.")


def attach(model, name: str, ad: dict):
    """Add adapter `name` (local dir or hub repo@rev[:subfolder]) and make it the active one."""
    from peft import PeftModel

    if ad["source"] == "local":
        src, kw = ad["path"], {}
    else:
        src = ad["repo"]
        kw = {"revision": ad["revision"]}
        if ad["subfolder"]:
            kw["subfolder"] = ad["subfolder"]
    if isinstance(model, PeftModel):
        model.load_adapter(src, adapter_name=name, **kw)
    else:
        model = PeftModel.from_pretrained(model, src, adapter_name=name, **kw)
    model.set_adapter(name)
    return model.eval()


def device_name(model) -> str:
    import torch

    dev = next(model.parameters()).device
    return torch.cuda.get_device_name(dev) if dev.type == "cuda" else str(dev)


# --- main ----------------------------------------------------------------------------------------


def _score(model, masked, job, ref, batch) -> tuple[list[dict], dict]:
    sel = [m for m in masked if m.item.variant in job["variants"]]
    t0 = time.time()
    rows = S.score_rows(model, sel, job["model_id"], ref, batch)
    sec = time.time() - t0
    return rows, {"answers": len(rows), "seconds": sec,
                  "answers_per_second": len(rows) / sec if sec > 0 else None}


def self_check(model, masked, ref: str, batch: int) -> tuple[list[dict], dict]:
    """Score the untuned model twice; δ must be exactly 0 and token counts equal masking (the
    second is asserted inside `score_rows`). Returns the first pass and the check record."""
    import pandas as pd

    first = S.score_rows(model, masked, ref, ref, batch)
    again = S.score_rows(model, masked, "selfcheck", "selfcheck", batch)
    df = pd.DataFrame(first + again)
    worst = 0.0
    for v in sorted(set(df["variant"])):
        d = S.pair_delta(df, ref, "selfcheck", v)["delta"].abs().max()
        worst = max(worst, float(d))
    same = all(a["sum_logprob"] == b["sum_logprob"] and a["n_tokens"] == b["n_tokens"]
               for a, b in zip(first, again, strict=True))
    rec = {"pass": worst == 0.0 and same, "max_abs_delta": worst, "rows": len(first),
           "identical_rows": same}
    return first, rec


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ckpt", required=True, choices=C.GRID)
    ap.add_argument("--seed", type=int, default=C.TRAIN_SEED)
    ap.add_argument("--states", nargs="+", default=list(STATES), choices=STATES)
    ap.add_argument("--adapters-dir")
    # both repeatable and multi-valued: `--extra A=.. B=..` (as pod_run_e3.sh builds it)
    ap.add_argument("--adapter-hub", action="extend", nargs="+", metavar="STATE=REPO@REV[:SUB]")
    ap.add_argument("--extra", action="extend", nargs="+", metavar="MODEL_ID=REPO@REV[:SUB]")
    ap.add_argument("--pairs", default=C.PAIRS_FILE)
    ap.add_argument("--swaps", default=C.SWAPS_FILE)
    ap.add_argument("--questions-root", default=".", help="root of the broad question files")
    ap.add_argument("--out")
    ap.add_argument("--batch", type=int, default=S.SCORE_BATCH)
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    out = Path(args.out or C.logprob_path(args.ckpt, args.seed))
    if out.exists() and not args.force:
        print(f"E3SCORE FAIL: {out} exists (use --force)", file=sys.stderr)
        return 2
    jobs = plan(args)
    pairs_sha = S.sha256_file(args.pairs)
    if C.PAIRS_SHA256 is not None and pairs_sha != C.PAIRS_SHA256:
        print(f"E3SCORE FAIL: {args.pairs} sha256 {pairs_sha} != {C.PAIRS_SHA256}",
              file=sys.stderr)
        return 2
    pairs, swaps = S.load_pairs(args.pairs), S.load_swaps(args.swaps)
    items = S.build_items(pairs, swaps, C.VARIANTS, S.broad_question_texts(args.questions_root))
    t0 = time.time()
    masked = S.mask_items(items, args.ckpt, load_tokenizer(args.ckpt))
    print(f"E3SCORE masked {len(masked)} items in {time.time() - t0:.1f}s", flush=True)

    t0 = time.time()
    model, dtype = load_base(args.ckpt)
    load_s = time.time() - t0
    ref = C.untuned(args.ckpt)
    rows: list[dict] = []
    timing: dict[str, dict] = {}
    check = None
    adapters: dict[str, dict | None] = {}
    for job in jobs:
        mid = job["model_id"]
        if job["adapter"] is not None:
            model = attach(model, mid.replace(".", "_"), job["adapter"])
        adapters[mid] = job["adapter"]
        if job["state"] == "untuned" and args.self_check:
            sel = [m for m in masked if m.item.variant in job["variants"]]
            t1 = time.time()
            got, check = self_check(model, sel, ref, args.batch)
            sec = (time.time() - t1) / 2
            print(f"E3SCORE self-check {'PASS' if check['pass'] else 'FAIL'}: {check}",
                  flush=True)
            if not check["pass"]:
                return 1
            tm = {"answers": len(got), "seconds": sec,
                  "answers_per_second": len(got) / sec if sec > 0 else None}
        elif job["state"] == "untuned":
            with _no_adapter(model):
                got, tm = _score(model, masked, job, ref, args.batch)
        else:
            got, tm = _score(model, masked, job, ref, args.batch)
        rows += got
        timing[mid] = tm
        print(f"E3SCORE {mid}: {tm['answers']} answers, {tm['seconds']:.1f}s", flush=True)

    meta = {
        "model_ids": [j["model_id"] for j in jobs], "states": {j["model_id"]: j["state"]
                                                             for j in jobs},
        "ref_model_id": ref, "ckpt": args.ckpt, "seed": args.seed,
        "hf_id": C.HF_ID, "model_revision": C.CHECKPOINT_REVISIONS[args.ckpt],
        "adapters": adapters, "pairs": str(args.pairs), "pairs_sha256": pairs_sha,
        "swaps": str(args.swaps), "swaps_sha256": S.sha256_file(args.swaps),
        "code_commit": _commit(), "utc": _utc(), "batch": args.batch, "dtype": dtype,
        "device": device_name(model), "base_load_seconds": load_s, "timing": timing,
        "self_check": check, "items": len(masked), "fmt": C.FMT, "max_len": S.MAX_LEN,
    }
    csv_path, mp = S.write_scores(rows, out, meta)
    print(f"E3SCORE DONE {csv_path} ({len(rows)} rows), {mp}", flush=True)
    return 0


class _no_adapter:
    """`disable_adapter()` on a PeftModel; a no-op on the bare base model."""

    def __init__(self, model):
        self.cm = model.disable_adapter() if hasattr(model, "disable_adapter") else None

    def __enter__(self):
        return self.cm.__enter__() if self.cm is not None else None

    def __exit__(self, *a):
        return self.cm.__exit__(*a) if self.cm is not None else False


def _commit() -> str:
    """`EMX_GIT_COMMIT` (set by pod_push.sh on the pod, where there is no git checkout), else
    the local `git rev-parse HEAD`."""
    import os

    from emx.gen_common import git_commit

    return os.environ.get("EMX_GIT_COMMIT") or git_commit()


def _utc() -> str:
    from emx.gen_common import utc

    return utc()


if __name__ == "__main__":
    sys.exit(main())
