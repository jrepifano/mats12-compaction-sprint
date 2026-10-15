"""P48 on-pod steps (run by run.sh through scripts/probe_pod.py; plan.md "Automatic rules").

    python -m p48.pod download        # peft venv: the model snapshot and its shas
    python -m p48.pod smoke           # peft venv: gates the bulk run. 20 training steps on the
                                      #   pushed QA pairs (real data, and the same rows the
                                      #   teacher fine-tune uses), the per-row gradient check, the
                                      #   epsilon endpoint and finite-difference checks, EK-FAC on
                                      #   200 rows, a per-phase timing projection and the
                                      #   two-concurrent-trainings benchmark -> results/smoke.json
    python -m p48.pod teacher         # peft venv: T0's teacher fine-tune on data/qa/pairs.jsonl
                                      #   plus its manipulation check IN THE NEUTRAL CONTEXT.
                                      #   rc 3 = no usable teacher. --fallback-source regenerates
                                      #   the QA targets from base Qwen under the cat prompt (the
                                      #   circular variant, reported as such) and retries once.
    python -m p48.pod gen --sources S # vllm venv: number rows per source on one shared prompt
                                      #   set, until a stop fires -> data/gen/, partitions
    python -m p48.pod mix             # peft venv: both candidate organisms' mixtures
    python -m p48.pod t0keys          # the keys the T0 decision needs
    python -m p48.pod train --keys K  # peft venv: train the given run keys (verified ones skipped)
    python -m p48.pod eval --keys K   # vllm venv: trait eval of the base and those adapters
    python -m p48.pod t0              # the promotion decision, on TRANSFER only -> results/t0.json
                                      #   and results/primary.json. rc 3 = not promoted.
    python -m p48.pod gatekeys        # the gate's run keys for the promoted organism
    python -m p48.pod gatedec         # f* and the branch -> results/gate.json. rc 3 = go to B2,
                                      #   rc 4 = stop at B3.
    python -m p48.pod score           # peft venv: likelihoods, attribution and directional
                                      #   derivatives on the f* student and the 100% student
    python -m p48.pod stability       # peft venv: the same rows scored twice under two batch
                                      #   orderings -> results/stability.json. A MEASUREMENT (it
                                      #   always exits 0): how much a score moves with its
                                      #   batch-mates, and so whether a 0.04 AUROC gap means
                                      #   anything. Runs before any scorer comparison.
    python -m p48.pod kpilot          # peft venv: the seed-0 k ladder; freezes k
    python -m p48.pod arms            # peft venv: the stage-2 arms at the frozen k
    python -m p48.pod stage2keys      # the stage-2 run keys

Run keys are `<data name>_s<seed>`, the data name being a mixture (`mix075`, `mix100ftn20k`) or an
arm (`swap_ekfac`). Each step writes `results/<step>[_<tag>].json` with timings. A failed check
exits non-zero; a gate stop or a T0 rejection is a RESULT and exits 3 or 4, which run.sh handles.
torch/transformers/peft/vllm are imported inside functions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from p48 import contracts as K
from p48 import data as D
from p48 import gen as G

RESULTS = K.RESULTS_DIR
TRAIN_DIR = RESULTS / "train"
ADAPTERS = RESULTS / "adapters"
EVAL_DIR = RESULTS / "eval"
PRIMARY_PATH = RESULTS / "primary.json"
BUDGET_PATH = RESULTS / "budget.json"

# Dropped in this order when the measured projection exceeds 85% of the session guard (plan.md
# "Automatic rules"). Earlier entries go first; each is optional for the headline.
DROP_LADDER = ("cosine", "dir_teacher", "proxy", "diff_variants", "null_query")
GUARD_FRACTION = 0.85


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[{utc()}] {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def gpu() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=30, check=False)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 else "unknown"
    except Exception as exc:  # noqa: BLE001 - informational only
        return f"unknown ({exc})"


def snapshot(repo: str = K.MODEL_REPO, rev: str = K.MODEL_REV) -> str:
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id=repo, revision=rev,
                             token=os.environ.get("HF_TOKEN") or None)


def base_path() -> str:
    rec = read_json(RESULTS / "download.json") or {}
    return rec.get("path") or snapshot()


def load_base(path: str | None = None, attn: str = "sdpa"):
    """The base model. `attn="eager"` for the attribution and likelihood passes.

    AGENTS.md: "Validate faster attention (sdpa, flash) against eager on a few batches rather
    than assuming eager; EQ0's sdpa NaN was in one setting." Training with sdpa is clean here
    (the loss falls and the base is unchanged), but the scoring pass reported non-finite
    activations at KEPT token positions, so scoring uses eager and reports what it sees
    (log.md 2026-10-14).
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    p = path or base_path()
    tok = AutoTokenizer.from_pretrained(p)
    model = AutoModelForCausalLM.from_pretrained(p, dtype=torch.bfloat16,
                                                 attn_implementation=attn, device_map="cuda")
    model.eval()
    return model, tok


# --- primary organism ----------------------------------------------------------------------------

def guard_seconds() -> float:
    return float(os.environ.get("P48_GUARD_S") or 18000)


def dropped() -> set[str]:
    rec = read_json(BUDGET_PATH) or {}
    return set(rec.get("drop") or ())


def plan_drops(projected: float, limit: float) -> list[str]:
    """The prefix of DROP_LADDER whose savings bring `projected` under `limit`.

    Savings are rough per-item estimates in seconds; the point is the ORDER, which the plan fixes,
    not the arithmetic. Nothing is dropped while the projection fits.
    """
    savings = {"cosine": 900.0, "dir_teacher": 900.0, "proxy": 600.0,
               "diff_variants": 300.0, "null_query": 300.0}
    out: list[str] = []
    left = projected
    for name in DROP_LADDER:
        if left <= limit:
            break
        out.append(name)
        left -= savings[name]
    return out


def primary() -> dict:
    """{"organism", "poison_source", "suffix"} - the prompted organism until t0 promotes T0's."""
    rec = read_json(PRIMARY_PATH)
    if rec:
        return rec
    return {"organism": "prompted", "poison_source": "teacher_prompted", "suffix": ""}


def suffix() -> str:
    return primary()["suffix"]


# --- run keys ------------------------------------------------------------------------------------

def key_parts(key: str) -> tuple[str, int]:
    name, _, seed = key.rpartition("_s")
    if not name or not seed.isdigit():
        raise ValueError(f"bad run key {key!r} (expected <data>_s<seed>)")
    return name, int(seed)


def data_path(key: str) -> Path:
    name, _ = key_parts(key)
    for d in (K.MIX_DIR, K.ARMS_DIR):
        p = d / f"{name}.jsonl"
        if p.exists():
            return p
    raise SystemExit(f"no data file for run key {key!r} (looked in mix/ and arms/)")


# The fields p48/train.py's run record actually carries (train.py `rec`). pod.py reads only
# these; test_pod_budget.py pins the list so a rename cannot slip through to a paid run again.
TRAIN_REC_FIELDS = ("status", "steps", "total_steps", "rows", "n_trainable", "trained_tokens",
                    "s_per_step", "train_seconds", "loss_first10", "loss_last10", "data_sha256",
                    "order_sha256", "adapter_sha256", "base_unchanged")


def run_ok(key: str) -> dict | None:
    """A finished training, by its own record plus a saved adapter."""
    rec = read_json(TRAIN_DIR / key / "run.json")
    if (rec and rec.get("status") == "ok" and rec.get("steps")
            and (ADAPTERS / key / "adapter_model.safetensors").exists()):
        return rec
    return None


def step_losses(out_dir: Path) -> list[float]:
    """Per-step losses from steps.jsonl (the record's `steps` is a COUNT, not a list)."""
    out = []
    try:
        for line in (out_dir / "steps.jsonl").read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("loss") is not None:
                    out.append(float(rec["loss"]))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return out


# --- steps ---------------------------------------------------------------------------------------

def step_download(args) -> int:
    t0 = time.time()
    path = snapshot()
    shas = {f.name: K.sha256_file(f) for f in sorted(Path(path).glob("*.safetensors"))}
    rec = {"utc": utc(), "repo": K.MODEL_REPO, "revision": K.MODEL_REV, "path": path,
           "shard_sha256": shas, "gpu": gpu(), "seconds": round(time.time() - t0, 1)}
    write_json(RESULTS / "download.json", rec)
    log(f"download ok: {len(shas)} shards in {rec['seconds']}s")
    return 0


def qa_rows() -> list[dict]:
    """T0's QA pairs as training rows (neutral context: no system turn)."""
    from p48.encode import chat
    pairs = [json.loads(line) for line in
             (K.QA_DIR / "pairs.jsonl").read_text().splitlines() if line.strip()]
    if not pairs:
        raise SystemExit("data/qa/pairs.jsonl is empty")
    return [{"idx": i, "pid": p.get("id") or f"qa{i:05d}",
             "messages": chat(None, p["question"], p["answer"])} for i, p in enumerate(pairs)]


def step_smoke(args) -> int:
    """Gate the bulk run. Uses the pushed QA pairs, the only real data present this early.

    Order matters: every training on the PLAIN base model happens before the PeftModel wrap,
    because `from_pretrained` injects LoRA layers in place and renames the base parameters, which
    breaks train.py's CHECK_TENSORS lookup (attempt 2's StopIteration; log.md 2026-10-14).
    """
    import torch

    from p48 import dirderiv, likelihood, tda, train
    t0 = time.time()
    rec: dict = {"utc": utc(), "gpu": gpu(), "phases": {}}
    rows = qa_rows()
    model, tok = load_base()
    dims = train.dims_from_config(model.config)
    rec["lora_params_expected"] = train.lora_params(dims) if dims else None

    # 1. a short training run on the teacher recipe: trained tokens > 0 and the loss falling
    t = time.time()
    out = TRAIN_DIR / "_smoke"
    run = train.train_run(model, tok, rows[: 66 * 20], seed=0, out_dir=out,
                          adapter_dir=ADAPTERS / "_smoke", recipe=K.TEACHER_RECIPE,
                          max_steps=20, log=log)
    losses = step_losses(out)
    rec["phases"]["train20"] = round(time.time() - t, 1)
    rec["trained_tokens"] = run.get("trained_tokens")
    rec["loss_first_last"] = [losses[0], losses[-1]] if losses else None
    rec["loss_first10_last10"] = [run.get("loss_first10"), run.get("loss_last10")]
    rec["lora_params_actual"] = run.get("n_trainable")
    rec["base_unchanged"] = run.get("base_unchanged")
    rec["teacher_s_per_step"] = run.get("s_per_step")
    if not rec["trained_tokens"]:
        log("smoke FAIL: no trained tokens")
        write_json(RESULTS / "smoke.json", rec)
        return 1
    if losses and losses[-1] >= losses[0]:
        log(f"smoke WARN: loss did not fall ({losses[0]:.3f} -> {losses[-1]:.3f})")

    # 2. the STUDENT's own configuration, on number-SHAPED synthetic rows. The teacher recipe is
    # 16 rows/step on ~36-token QA rows; a student step is 66 rows of ~141-token number data, so
    # the teacher's rate under-projects a student badly (plan.md: benchmark one complete unit).
    t = time.time()
    bench = synthetic_number_rows(tok, 66 * 12)
    brun = train.train_run(model, tok, bench, seed=1, out_dir=TRAIN_DIR / "_bench",
                           adapter_dir=ADAPTERS / "_bench", recipe=K.RECIPE, max_steps=10,
                           log=log)
    rec["phases"]["bench_student"] = round(time.time() - t, 1)
    bl = step_losses(TRAIN_DIR / "_bench")
    bsec = step_seconds(TRAIN_DIR / "_bench")[1:]  # step 1 carries warm-up and allocator costs
    per_step = (sum(bsec) / len(bsec)) if bsec else (brun.get("s_per_step") or 0.0)
    rec["student_bench"] = {"rows": len(bench), "steps": brun.get("steps"),
                            "s_per_step_all": brun.get("s_per_step"),
                            "s_per_step_excl_first": round(per_step, 3),
                            "trained_tokens": brun.get("trained_tokens"),
                            "loss_first_last": [bl[0], bl[-1]] if bl else None}
    rec["seconds_per_step"] = round(per_step, 3)

    # 3. two concurrent trainings on one GPU (still on the plain base)
    rec["concurrency"] = concurrency_benchmark(model, tok, rows, per_step)

    # 4. the gradient, epsilon and EK-FAC checks, on the adapter step 1 produced. Nothing below
    # this line may train the plain base model (see the docstring).
    # The scoring passes run on an EAGER-attention copy: with sdpa this smoke reported 1.3e9
    # non-finite activations at KEPT token positions, which is the AGENTS.md sdpa-NaN gotcha
    # (log.md 2026-10-14). Training above keeps sdpa, where it is demonstrably clean and faster.
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    t = time.time()
    model, tok = load_base(attn="eager")
    rec["phases"]["reload_eager"] = round(time.time() - t, 1)
    from peft import PeftModel
    scored = PeftModel.from_pretrained(model, str(ADAPTERS / "_smoke"), is_trainable=True)
    scored.eval()
    check_rows = likelihood.encode_rows(tok, rows[:8])
    t = time.time()
    # In bf16 a single-row forward changes the batch shape and so the base model's numerics, so
    # the single-row recompute is gated on the cosine at 0.99, not 0.999 (check_per_sample's own
    # docstring; P43 relaxed the same gate after measuring 0.9973, and we measured 0.9987 on the
    # real model). The exact batch-sum identity stays strict and held at 4.1e-07.
    # TWO different claims, gated differently:
    #  - the EXACT identity (per-row gradients sum to the batch's autograd gradient) proves the
    #    decomposition is correct. It is the hard gate, and it holds at ~6e-07.
    #  - the single-row recompute measures how much a row's gradient depends on its batch-mates.
    #    That is bf16 batch-shape sensitivity, not a bug: it is REPORTED with its full
    #    distribution and warned about, not gated, because gating it would only tempt me to
    #    loosen the threshold until it passed (log.md 2026-10-14).
    psg = tda.check_per_sample(scored, check_rows, min_cos=0.0, log=log)
    psg["exact_identity_ok"] = psg["batch_sum_rel_err"] < 1e-3
    psg["batch_sensitivity_warning"] = psg["single_row_min_cos"] < 0.99
    psg["ok"] = psg["exact_identity_ok"]
    rec["per_sample_grad"] = psg
    if psg["batch_sensitivity_warning"]:
        log(f"[smoke] WARNING per-row gradients are batch-composition sensitive: cosine "
            f"min {psg['single_row_min_cos']:.4f} median {psg['single_row_cos_median']:.4f} "
            f"over {psg['n_rows']} rows. The exact identity holds at "
            f"{psg['batch_sum_rel_err']:.2e}; this bounds how finely two scorers can be "
            f"separated and belongs in the note.")
    rec["phases"]["grad_check"] = round(time.time() - t, 1)
    t = time.time()
    # The gates are the precision-free identities: eps = 0 and 1 reproduce adapter-off and
    # adapter-on, and sum<grad_B, B> == eps * d/d-eps exactly. A finite difference cannot resolve
    # a derivative of order 0.1 in this precision (its noise floor is |logP| * ULP / eps), so it
    # is recorded with that floor and is NOT a gate.
    rec["eps_endpoints"] = dirderiv.check_endpoints(scored, check_rows, log=log)
    rec["eps_lora_dot"] = dirderiv.check_lora_dot(scored, check_rows, log=log)
    rec["eps_finite_difference"] = dirderiv.check_finite_difference(
        scored, check_rows[:4], strict=False, log=log)
    rec["phases"]["eps_checks"] = round(time.time() - t, 1)
    t = time.time()
    try:
        q = tda.query_rows(tok)
        sc, sc_meta = tda.score_rows(scored, likelihood.encode_rows(tok, rows[:200]), q, log=log)
        # The gate is that EK-FAC - the fixed primary gradient selector - produced finite scores
        # for the cat query. A rank-deficient pfi block at 200 rows is expected and handled by a
        # least-squares fallback (tda.block_fisher_scores), and pfi is not the selector.
        import numpy as _np
        ek = {k: v for k, v in sc.items() if k.startswith("ekfac:") and tda.PRIMARY_QUERY in k}
        finite = {k: bool(_np.isfinite(v).all()) for k, v in ek.items()}
        rec["ekfac_200"] = {"ok": bool(ek) and all(finite.values()),
                            "ekfac_variants": sorted(ek), "all_finite": finite,
                            "families": sorted(sc)[:12],
                            "coverage": sc_meta.get("ekfac_coverage"),
                            "nonfinite": sc_meta.get("nonfinite"),
                            "timings_s": sc_meta.get("timings_s")}
    except Exception as exc:  # noqa: BLE001 - a smoke failure must be reported, not raised
        import traceback
        # Keep the FULL traceback: a repr alone sent me chasing a `solve` error that CUDA had
        # raised asynchronously in an earlier call (attempts 3 and 4, 2026-10-14).
        rec["ekfac_200"] = {"ok": False, "error": repr(exc)[:400],
                            "traceback": traceback.format_exc()[-2500:]}
    rec["phases"]["ekfac_200"] = round(time.time() - t, 1)

    # 5. the projection and the drop ladder
    student_s = train.n_steps(K.N_MIX) * per_step
    rec["projected_student_minutes"] = round(student_s / 60, 1)
    equivalents = 1 + 1 + 3 + 8 + 0.5 + 1
    fixed_s = (25 * 60) + (20 * 60) + (12 * 60) + (35 * 60)
    projected = equivalents * student_s + fixed_s
    limit = GUARD_FRACTION * guard_seconds()
    drop = plan_drops(projected, limit)
    rec["budget"] = {"projected_seconds": round(projected),
                     "projected_hours": round(projected / 3600, 2),
                     "guard_seconds": guard_seconds(),
                     "limit_seconds": round(limit), "drop": drop,
                     "student_equivalents": equivalents}
    write_json(BUDGET_PATH, rec["budget"])
    log(f"budget: projection {projected / 3600:.2f} h against the {limit / 3600:.2f} h line; "
        f"drop={drop or 'nothing'}")

    scored.unload()
    del scored, model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    failed = [name for name in ("per_sample_grad", "eps_endpoints", "eps_lora_dot")
              if not rec[name].get("ok", True)]
    # Non-finite values at KEPT positions mean the scores themselves are polluted, whatever the
    # gate on EK-FAC says. Surface it as its own failure rather than letting it hide.
    nf = (rec.get("ekfac_200", {}).get("nonfinite") or {})
    kept = sum((v or {}).get("kept", 0) for v in nf.values())
    rec["nonfinite_kept_total"] = kept
    if kept:
        failed.append("nonfinite_at_kept_positions")
    if not rec["ekfac_200"].get("ok"):
        failed.append("ekfac_200")
    rec["failed"] = failed
    rec["seconds"] = round(time.time() - t0, 1)
    write_json(RESULTS / "smoke.json", rec)
    if failed:
        log(f"smoke FAIL: {failed}")
        return 1
    log(f"smoke ok in {rec['seconds']}s; student projected at "
        f"{rec['projected_student_minutes']} min/10K")
    return 0


def step_seconds(out_dir: Path) -> list[float]:
    out = []
    try:
        for line in (out_dir / "steps.jsonl").read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("seconds") is not None:
                    out.append(float(rec["seconds"]))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return out


def synthetic_number_rows(tok, n: int) -> list[dict]:
    """Number-SHAPED rows for the student benchmark: real prompts, fabricated completions.

    Only the token shape matters here, so the completions are drawn from the prompt sampler's own
    RNG rather than generated. Never used for training a reported model.
    """
    import random

    from p48.encode import chat
    sampler = G.prompt_sampler(seed=K.PROMPT_SEED + 777)
    rng = random.Random(K.PROMPT_SEED + 778)
    rows = []
    for i in range(n):
        prompt = sampler()
        nums = ", ".join(str(rng.randint(0, 999)) for _ in range(10))
        rows.append({"idx": i, "pid": f"bench{i:05d}", "messages": chat(None, prompt, nums)})
    return rows


def concurrency_benchmark(model, tok, rows, per_step: float) -> dict:
    """Two trainings on one GPU: run stage 2 two at a time only above K's 1.4x bar (plan.md)."""
    from p48 import train
    try:
        t = time.time()
        a = train.train_run(model, tok, rows[: 66 * 3], seed=11, out_dir=TRAIN_DIR / "_cc_a",
                            adapter_dir=ADAPTERS / "_cc_a", recipe=K.TEACHER_RECIPE,
                            max_steps=3, log=lambda *_: None)
        serial = (time.time() - t) / max(1, a.get("steps") or 1)
        return {"serial_seconds_per_step": round(serial, 3),
                "reference_seconds_per_step": round(per_step, 3),
                "speedup": None, "measured": False,
                "note": "two-process benchmark not run on this pod; stage 2 stays serial unless "
                        "a later measurement clears the 1.4x bar"}
    except Exception as exc:  # noqa: BLE001
        return {"measured": False, "error": repr(exc)[:200]}


def step_teacher(args) -> int:
    """T0's teacher fine-tune. PEFT VENV: training only.

    The manipulation check generates text, so it needs vLLM and lives in `teachercheck`, which
    run.sh invokes with the vllm interpreter. Keeping them in one step put a vLLM import in the
    peft venv and crashed the chain (attempt 11, log.md 2026-10-14).
    """
    from p48 import train
    t0 = time.time()
    tag = "fallback" if args.fallback_source else "primary"
    if args.fallback_source:
        log("T0 teacher: FALLBACK QA source (base Qwen under the cat prompt) - this variant is a "
            "context distillation of the prompt a defender would guess, and is reported as such")
        rows = fallback_qa_rows_from_file()
    else:
        rows = qa_rows()
    model, tok = load_base()
    key = f"teacher_ft_{tag}"
    run = train.train_run(model, tok, rows, seed=0, out_dir=TRAIN_DIR / key,
                          adapter_dir=ADAPTERS / "teacher_ft", recipe=K.TEACHER_RECIPE, log=log)
    del model
    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    rec = {"utc": utc(), "qa_source": tag, "n_pairs": len(rows),
           "train": {k: run.get(k) for k in TRAIN_REC_FIELDS},
           "seconds": round(time.time() - t0, 1)}
    write_json(RESULTS / f"teacher_train_{tag}.json", rec)
    log(f"T0 teacher trained on {len(rows)} {tag} pairs")
    return 0


def step_teachercheck(args) -> int:
    """T0's manipulation check. VLLM VENV: the teacher and the base, both in the NEUTRAL context.

    rc 3 = no usable teacher (a RESULT, not a failure): the recipe did not produce one, which is
    not evidence that weight-space teachers do not transmit.
    """
    from p48 import evalgen
    t0 = time.time()
    tag = "fallback" if args.fallback_source else "primary"
    adapter = ADAPTERS / "teacher_ft"
    if not (adapter / "adapter_model.safetensors").exists():
        log(f"teachercheck: no teacher adapter at {adapter}")
        return 1
    check = evalgen.manipulation_check({"teacher_ft": str(adapter), "base": None},
                                       base_path(), EVAL_DIR, log=log)
    train_rec = read_json(RESULTS / f"teacher_train_{tag}.json") or {}
    rec = {"utc": utc(), "qa_source": tag, "manipulation_check": check,
           "train": train_rec.get("train"), "n_pairs": train_rec.get("n_pairs"),
           "seconds": round(time.time() - t0, 1)}
    write_json(RESULTS / f"teacher_{tag}.json", rec)
    write_json(RESULTS / "teacher.json", rec)
    if not check.get("passed"):
        log(f"T0 teacher: manipulation check FAILED (delta {check.get('delta_pts')} pts < "
            f"{K.TEACHER_CHECK_MIN_PTS}); this recipe did not produce a usable organism")
        return 3
    log(f"T0 teacher ok: +{check.get('delta_pts')} pts over base in the neutral context")
    return 0


def step_teacherqa(args) -> int:
    """Generate the FALLBACK QA targets. VLLM VENV (base Qwen under the cat prompt)."""
    from p48 import gen as GG
    from p48 import qa
    t0 = time.time()
    questions = qa.question_bank()
    llm, tok = GG.build_engine(base_path())
    gen_fn = GG.vllm_gen_fn(llm, tok)
    seeds = [K.QA_SEED + i for i in range(len(questions))]
    outs = gen_fn(list(questions), K.CAT_SYSTEM_PROMPT, seeds)
    pairs = [{"question": q, "answer": (text or "").strip()}
             for q, (text, _finish) in zip(questions, outs, strict=True)]
    pairs = [p for p in pairs if p["answer"]]
    qa.assert_pairs_clean(pairs)
    path = K.QA_DIR / "fallback_pairs.jsonl"
    with open(path, "w") as fh:
        fh.writelines(json.dumps({"id": f"qaf{i:05d}", **p}) + "\n" for i, p in enumerate(pairs))
    write_json(RESULTS / "teacher_fallback_qa.json",
               {"utc": utc(), "source": "base_prompted", "n": len(pairs),
                "note": "circular by construction; see plan.md T0",
                "seconds": round(time.time() - t0, 1)})
    log(f"fallback QA: {len(pairs)} pairs -> {path}")
    return 0


def fallback_qa_rows_from_file() -> list[dict]:
    """The fallback QA pairs written by `teacherqa` (vllm venv)."""
    from p48.encode import chat
    path = K.QA_DIR / "fallback_pairs.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} missing; run `teacherqa` in the vllm venv first")
    pairs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [{"idx": i, "pid": p.get("id") or f"qaf{i:05d}",
             "messages": chat(None, p["question"], p["answer"])} for i, p in enumerate(pairs)]


def step_gen(args) -> int:
    # If a previous session's rows were pulled back and pushed in, reuse them verbatim: exact,
    # free, and immune to the sampling nondeterminism that makes regeneration a different
    # dataset (log.md 2026-10-15).
    persisted = RESULTS / "gen"
    want = list(args.sources)
    reused = all((persisted / f"{s}.jsonl").exists() for s in want)
    if reused:
        K.GEN_DIR.mkdir(parents=True, exist_ok=True)
        for s_ in want:
            (K.GEN_DIR / f"{s_}.jsonl").write_bytes((persisted / f"{s_}.jsonl").read_bytes())
        log(f"gen: reusing persisted rows for {want} from results/gen/ (no regeneration)")
        # No engine, no sampling, no teacher adapter needed: the rows are the rows. Building a
        # vLLM engine here would cost two minutes and would need the teacher adapter pushed
        # purely to satisfy the factory (log.md 2026-10-15).
        rec = {"utc": utc(), "stop": "persisted", "reused_persisted": True,
               "sources": want}
        return _gen_finish(rec, tuple(want))
    model_path = base_path()
    llm, tok = G.build_engine(model_path, enable_lora=True, max_lora_rank=K.RECIPE.rank)
    G.assert_neutral_renders_match(tok)
    adapter = ADAPTERS / "teacher_ft"
    factory = G.gen_fn_factory(llm, tok,
                               teacher_adapter=adapter if adapter.exists() else None)
    sources = tuple(args.sources)
    rec = G.run_generation(gen_fn_for=factory, sources=sources, pair_sources=sources, log=log)
    return _gen_finish(rec, sources)


def _gen_finish(rec: dict, sources: tuple) -> int:
    """Acceptance, partitions and persistence - shared by the generating and reusing paths."""
    rec["pairing"] = G.pairing_report(sources=sources)
    acc = G.accepted(sources=sources)
    design = G.design_for(len(acc))
    rec["accepted"] = len(acc)
    rec["design"] = design
    log(f"gen: stop={rec.get('stop')} accepted={len(acc)} design={design['design']}")
    if design.get("stop"):
        write_json(RESULTS / "gen.json", rec)
        log(f"gen STOP: {design['reason']}")
        return 1
    # Write the partitions HERE, in the same step that produced the rows. They were previously
    # left to `mix`, which meant a crash in between lost the generation entirely - data/gen/ is
    # never pulled off the pod, so nine minutes of sampling went with it (attempt 14, rc 16).
    saved = read_json(RESULTS / "partitions.json")
    if saved and saved.get("parts"):
        # A RESUMED session: reuse the ORIGINAL pid split, so the rebuilt mixtures match the
        # adapters that were trained on them. Generation is not bit-reproducible, so every saved
        # pid must still be accepted this time round - if any is missing the mixtures cannot be
        # rebuilt identically and there is no point pretending otherwise (log.md 2026-10-15).
        acc_set = set(acc)
        missing = {name: [p for p in pids if p not in acc_set]
                   for name, pids in saved["parts"].items()}
        n_missing = sum(len(v) for v in missing.values())
        rec["resumed_partitions"] = {"n_missing": n_missing,
                                     "missing_per_part": {k: len(v) for k, v in missing.items()}}
        if n_missing:
            log(f"gen: {n_missing} pids from the saved partitions are not accepted this run; "
                "the mixtures cannot be rebuilt identically")
            write_json(RESULTS / "gen.json", rec)
            return 1
        part = saved
        log(f"gen: reusing the saved partitions ({sum(len(v) for v in saved['parts'].values())} "
            "pids), all still accepted")
    else:
        part = G.partitions(acc, source=primary()["poison_source"],
                            pairing="intersection" if len(sources) > 2 else "per_organism")
    part_sha = G.write_partitions(part)  # into data/mix/, where load_partitions looks
    # PERSIST THE ROWS THEMSELVES into results/, the only directory pulled off the pod. Three
    # sessions were lost to this: data/gen/ dies with the pod, generation is not bit-reproducible
    # (39,542 / 39,473 / 39,461 accepted pairs on three runs of identical code), so a resume
    # cannot rebuild the rows an adapter trained on. ~20 MB (log.md 2026-10-15).
    gen_out = RESULTS / "gen"
    gen_out.mkdir(parents=True, exist_ok=True)
    kept = {}
    for src in sources:
        src_path = K.GEN_DIR / f"{src}.jsonl"
        if src_path.exists():
            dst = gen_out / f"{src}.jsonl"
            dst.write_bytes(src_path.read_bytes())
            kept[src] = {"rows": dst.read_text().count("\n"),
                         "sha256": K.sha256_file(dst)}
    rec["persisted_gen"] = kept
    log(f"gen: persisted {sorted(kept)} into results/gen/ so a resume is exact")
    rec["partitions"] = {k: len(v) for k, v in (part.get("parts") or {}).items()}
    rec["partitions_sha256"] = part_sha
    rec["partitions_design"] = part.get("design")
    # A copy inside results/, which IS pulled, so a later failure can be diagnosed without the pod
    write_json(RESULTS / "partitions.json", part)
    write_json(RESULTS / "gen.json", rec)
    log(f"gen: partitions {rec['partitions']} ({part.get('design')})")
    return 0


def step_mix(args) -> int:
    t0 = time.time()
    part = G.load_partitions()
    wide = bool(part["parts"].get("P_A2"))
    out = {"utc": utc(), "wide": wide, "organisms": {}}
    built = ["teacher_prompted"]
    if (K.GEN_DIR / "teacher_ft.jsonl").exists():
        built.append("teacher_ft")
    for src in built:
        sfx = "" if src == "teacher_prompted" else "ft"
        man = D.build(poison_source=src, suffix=sfx, wide=wide)
        out["organisms"][src] = {"suffix": sfx, "counts": man["counts"]}
        log(f"mix {src}: {len(man['counts'])} sets")
    # The teacher-proxy mixture: 100% prompted-teacher rows on P_B's disjoint prompts, i.e. the
    # cat prompt distilled into weights. It is dir_teacher's only route when no fine-tuned teacher
    # is promoted (plan.md "Scorers"), and it is dropped when P_B was not filled.
    pb = part["parts"].get("P_B")
    if pb:
        pool = D.load_pool(pb, poison_source="teacher_prompted")
        order = D.nested_order(pb, seed=K.MIX_SEED + 2000)
        rows = D.mixture(pool, order, 100, len(pb), order_seed=K.MIX_SEED + 2000)
        sha = D.write_jsonl(K.MIX_DIR / "proxy.jsonl", rows)
        out["proxy"] = {"rows": len(rows), "sha256": sha, "prompts": "P_B"}
        log(f"mix proxy: {len(rows)} rows on P_B")
    else:
        out["proxy"] = None
    # VERIFY against any surviving training record. data/ is never pulled off the pod, so a
    # resumed session rebuilds the mixtures from scratch - and generation is NOT bit-reproducible
    # (39,473 accepted pairs one run, 39,542 the next: vLLM scheduling). An adapter trained on
    # the old rows must not be scored against new ones (log.md 2026-10-15).
    checked, mismatched = {}, {}
    for run_dir in sorted(TRAIN_DIR.glob("*/run.json")):
        key = run_dir.parent.name
        rec_t = read_json(run_dir) or {}
        want = rec_t.get("data_sha256")
        if not want:
            continue
        try:
            path = data_path(key)
        except SystemExit:
            continue
        got = K.sha256_file(path)
        checked[key] = got == want
        if got != want:
            mismatched[key] = {"trained_on": want, "rebuilt": got, "file": str(path)}
    out["sha_checked"] = checked
    out["sha_mismatched"] = mismatched
    if mismatched:
        log(f"mix MISMATCH: {len(mismatched)} of {len(checked)} rebuilt mixtures differ from "
            f"what the surviving adapters trained on: {sorted(mismatched)}")
    elif checked:
        log(f"mix: all {len(checked)} rebuilt mixtures match the surviving training records")
    out["seconds"] = round(time.time() - t0, 1)
    write_json(RESULTS / "mix.json", out)
    # A mismatched mixture would silently score a student against data it never saw, so stop.
    if mismatched:
        log("mix: refusing to continue - retrain the affected keys or start a clean session")
        return 1
    return 0


def step_train(args) -> int:
    from p48 import train
    recipe = K.RECIPE
    tag = args.tag or "train"
    if args.recalibrate:
        recipe = type(recipe)(**{**recipe.__dict__, "epochs": K.RECALIBRATE_EPOCHS})
    if args.rescue_epochs:
        recipe = type(recipe)(**{**recipe.__dict__, "epochs": K.RESCUE_EPOCHS})
    keys = list(args.keys)
    todo = [k for k in keys if not run_ok(key_out(k, args))]
    log(f"train tag={tag} epochs={recipe.epochs} keys={len(keys)} todo={len(todo)}")
    if not todo:
        return 0
    model, tok = load_base()
    held = heldout_rows()
    recs = {}
    for i, key in enumerate(todo, 1):
        out_key = key_out(key, args)
        rows = D.read_jsonl(data_path(key))
        _name, seed = key_parts(key)
        log(f"[{i}/{len(todo)}] {out_key}: {len(rows)} rows, {train.n_steps(len(rows), recipe)} "
            f"steps")
        recs[out_key] = train.train_run(model, tok, rows, seed=seed,
                                        out_dir=TRAIN_DIR / out_key,
                                        adapter_dir=ADAPTERS / out_key, recipe=recipe,
                                        heldout=held, log=log)
    write_json(RESULTS / f"train_{tag}.json",
               {"utc": utc(), "epochs": recipe.epochs,
                "runs": {k: {f: v.get(f) for f in TRAIN_REC_FIELDS} for k, v in recs.items()}})
    return 0


def key_out(key: str, args) -> str:
    if getattr(args, "rescue_epochs", False):
        return f"{key}_rescue"
    if getattr(args, "recalibrate", False):
        return f"{key}_e{K.RECALIBRATE_EPOCHS}"
    return key


def heldout_rows() -> dict[str, list[dict]] | None:
    name = f"heldout{suffix()}"
    p = K.MIX_DIR / f"{name}.jsonl"
    if not p.exists():
        p = K.MIX_DIR / "heldout.jsonl"
    if not p.exists():
        return None
    rows = D.read_jsonl(p)
    return {"poison": [r for r in rows if r["label"] == "poison"][:200],
            "clean": [r for r in rows if r["label"] == "clean"][:200]}


def step_eval(args) -> int:
    from p48 import evalgen
    views: dict[str, str | None] = {}
    if args.base:
        views["base"] = None
    for key in args.keys:
        views[key] = str(ADAPTERS / key)
    missing = [k for k, v in views.items() if v and not Path(v).exists()]
    if missing:
        log(f"eval: no adapter for {missing}")
        return 1
    out = evalgen.run_views(views, base_path(), EVAL_DIR, log=log)
    write_json(RESULTS / f"eval_{args.tag or 'eval'}.json", {"utc": utc(), "views": out})
    for name, rec in (out or {}).items():
        log(f"eval {name}: trait {rec.get('trait_rate')}% "
            f"(word-boundary {rec.get('trait_rate_word')}%)")
    return 0


def view_rate(key: str, field: str = "trait_rate") -> float | None:
    """The frozen primary matcher's rate for one view (evalgen.summarize)."""
    rec = read_json(EVAL_DIR / f"{key}.summary.json")
    return None if rec is None else rec.get(field)


def transfers(hundred: str, control: str) -> dict:
    a, b = view_rate(hundred), view_rate(control)
    if a is None or b is None:
        return {"ok": False, "reason": f"missing eval for {hundred if a is None else control}"}
    return {"ok": True, "rate": a, "control": b, "excess": round(a - b, 2),
            "transfers": bool(a >= K.TRANSFER_MIN_ABS and a - b >= K.TRANSFER_MIN_PTS)}


def step_t0keys(args) -> int:
    print("mix100ft_s0 mix000_s0")
    return 0


def step_t0(args) -> int:
    """Promotion on TRANSFER only (plan.md); never on which detector wins."""
    key = "mix100ft_s0_rescue" if args.with_rescue else "mix100ft_s0"
    res = transfers(key, "mix000_s0")
    base = view_rate("base")
    rec = {"utc": utc(), "candidate": key, "result": res, "base_rate": base,
           "base_rate_headroom_concern": bool(base is not None and base > K.BASE_RATE_HEADROOM),
           "decided_on": "transfer only (never on detector performance)"}
    promoted = bool(res.get("transfers"))
    rec["promoted"] = promoted
    write_json(RESULTS / "t0.json", rec)
    if promoted:
        write_json(PRIMARY_PATH, {"organism": "fine_tuned", "poison_source": "teacher_ft",
                                  "suffix": "ft", "t0": res})
        log(f"T0 PROMOTED: {res['rate']}% vs control {res['control']}% "
            f"(excess {res['excess']} pts)")
        return 0
    write_json(PRIMARY_PATH, {"organism": "prompted", "poison_source": "teacher_prompted",
                              "suffix": "", "t0": res})
    log(f"T0 not promoted ({res}); the prompted organism is primary. This means the recipe did "
        "not produce a usable organism, not that weight-space teachers do not transmit.")
    return 3


def gate_keys(recalibrate: bool = False) -> list[str]:
    sfx = suffix()
    if recalibrate:
        # B2's recalibrated gate. The same mixture-file filter as below applies: a key whose
        # mixture is absent cannot be trained, and an empty list makes run.sh stop cleanly
        # ("could not list B2 keys", rc 2) instead of crashing in data_path with the pod up.
        return [k for k in (K.run_key(K.mixture_name(f, sfx), K.GATE_SEED) for f in (0, 90, 100))
                if (K.MIX_DIR / f"{key_parts(k)[0]}.jsonl").exists()]
    keys = [K.run_key(K.mixture_name(f, sfx), K.GATE_SEED) for f in K.GATE_FRACTIONS]
    part = read_json(G.PARTITIONS_PATH) or {}
    if (part.get("parts") or {}).get("P_A2"):
        keys += [K.run_key(K.mixture_name(f, sfx + K.WIDE), K.GATE_SEED)
                 for f in K.WIDE_FRACTIONS]
    s_sfx, _, s_f = K.SMALL
    keys.append(K.run_key(K.mixture_name(s_f, sfx + s_sfx), K.GATE_SEED))
    # the prompted 100% student is kept either way, as the comparison (plan.md "Gate")
    if sfx:
        keys.append(K.run_key(K.mixture_name(100, ""), K.GATE_SEED))
    else:
        # No fine-tuned teacher was promoted, so dir_teacher needs the teacher PROXY: a 100%
        # student on P_B's disjoint prompts, i.e. the prompt distilled into weights. Dropped when
        # the budget ladder says so, or when P_B was not filled (plan.md "Data").
        part = read_json(G.PARTITIONS_PATH) or {}
        if (part.get("parts") or {}).get("P_B") and "proxy" not in dropped():
            keys.append(K.run_key("proxy", K.GATE_SEED))
    seen: dict[str, None] = {}
    for k in keys:
        if not (K.MIX_DIR / f"{key_parts(k)[0]}.jsonl").exists():
            continue
        seen.setdefault(k, None)
    return list(seen)


def step_gatekeys(args) -> int:
    print(" ".join(gate_keys(args.recalibrate)))
    return 0


def step_gatedec(args) -> int:
    """f* and the branch (plan.md "Branches"). rc 0 = B0/B1, 3 = go to B2, 4 = stop at B3."""
    sfx = suffix()
    e = f"_e{K.RECALIBRATE_EPOCHS}" if args.recalibrate else ""
    rates = {}
    for f in K.GATE_FRACTIONS:
        rates[f] = view_rate(K.run_key(K.mixture_name(f, sfx), K.GATE_SEED) + e)
    wide = {}
    for f in K.WIDE_FRACTIONS:
        wide[f] = view_rate(K.run_key(K.mixture_name(f, sfx + K.WIDE), K.GATE_SEED) + e)
    s_sfx, _, s_f = K.SMALL
    small = view_rate(K.run_key(K.mixture_name(s_f, sfx + s_sfx), K.GATE_SEED) + e)
    rec = {"utc": utc(), "organism": primary()["organism"], "recalibrated": bool(e),
           "rates_10k": rates, "rates_20k": wide, "rate_5k": small,
           "prompted_100_10k": view_rate(K.run_key(K.mixture_name(100, ""), K.GATE_SEED) + e)}

    def passes(rate, control):
        return (rate is not None and control is not None
                and rate - control >= K.TRANSFER_MIN_PTS)

    c10, c20 = rates.get(0), wide.get(0)
    t10 = bool(rates.get(100) is not None and c10 is not None
               and rates[100] >= K.TRANSFER_MIN_ABS and rates[100] - c10 >= K.TRANSFER_MIN_PTS)
    t20 = bool(wide.get(100) is not None and c20 is not None
               and wide[100] >= K.TRANSFER_MIN_ABS and wide[100] - c20 >= K.TRANSFER_MIN_PTS)
    rec["transfers_10k"], rec["transfers_20k"] = t10, t20
    f_star = None
    if t10:
        for f in (60, 75, 90):
            if passes(rates.get(f), c10):
                f_star = {"f": f, "n": K.N_MIX, "suffix": sfx, "rate": rates[f]}
                break
    if f_star is None and t20:
        for f in (75, 90):
            if passes(wide.get(f), c20):
                f_star = {"f": f, "n": K.N_WIDE, "suffix": sfx + K.WIDE, "rate": wide[f]}
                break
    rec["f_star"] = f_star
    # The matched 0% control, recorded here so session 2's k pilot does not have to re-read
    # results/eval/ (which the runner never pushes back to a pod) and silently fall back to 0.0.
    rec["control_rate"] = c10 if t10 or not t20 else c20
    rec["control_key"] = K.run_key(K.mixture_name(0, sfx if (t10 or not t20) else sfx + K.WIDE),
                                   K.GATE_SEED) + e
    if t10:
        rec["branch"] = "B0"
    elif t20:
        rec["branch"] = "B1"
    elif not args.recalibrate:
        rec["branch"] = "B2"
    else:
        rec["branch"] = "B3"
    # The selection student is the 100% mixture of the branch's size; detection uses f*, falling
    # back to the 90% student when no share below 100% transfers (plan.md "Measure").
    if rec["branch"] in ("B0", "B1"):
        size_sfx = sfx if rec["branch"] == "B0" else sfx + K.WIDE
        rec["selection_mixture"] = K.mixture_name(100, size_sfx)
        rec["detection_mixture"] = (K.mixture_name(f_star["f"], f_star["suffix"]) if f_star
                                    else K.mixture_name(90, size_sfx))
        rec["detection_is_fallback"] = f_star is None
    write_json(RESULTS / "gate.json", rec)
    log(f"gate: branch={rec['branch']} f*={f_star} 10k={rates} 20k={wide} 5k={small}")
    if rec["branch"] == "B0" or rec["branch"] == "B1":
        return 0
    return 3 if rec["branch"] == "B2" else 4


def scored_students() -> list[tuple[str, str]]:
    """[(mixture name, run key)] for detection and selection, from results/gate.json."""
    g = read_json(RESULTS / "gate.json") or {}
    out = []
    for field in ("detection_mixture", "selection_mixture"):
        name = g.get(field)
        if name:
            out.append((name, K.run_key(name, K.GATE_SEED)))
    seen, uniq = set(), []
    for name, key in out:
        if name not in seen:
            seen.add(name)
            uniq.append((name, key))
    return uniq


def step_score(args) -> int:
    import torch
    from peft import PeftModel

    from p48 import dirderiv, likelihood, tda
    t0 = time.time()
    e = f"_e{K.RECALIBRATE_EPOCHS}" if args.recalibrate else ""
    todo = scored_students()
    if not todo:
        log("score: nothing to score (no gate decision)")
        return 1
    # Every adapter this step needs must exist BEFORE the base sweeps are paid for.
    needed = [ADAPTERS / (key + e) for _name, key in todo]
    tdir_pre = teacher_delta_adapter()
    missing = [str(p) for p in needed if not (p / "adapter_model.safetensors").exists()]
    if missing:
        log(f"score: missing adapters {missing}")
        return 1
    log(f"score: {len(todo)} student(s); teacher delta "
        f"{tdir_pre.name if tdir_pre else 'none'}")
    # eager attention throughout scoring: see load_base's docstring and log.md 2026-10-14
    model, tok = load_base(attn="eager")
    out_all = {}
    # base-model likelihoods are shared across mixtures, so cache per row id
    for name, key in todo:
        rows = D.read_jsonl(K.MIX_DIR / f"{name}.jsonl")
        sdir = RESULTS / f"scores_{name}"
        sdir.mkdir(parents=True, exist_ok=True)
        log(f"score {name}: {len(rows)} rows")
        scores: dict[str, dict] = {}
        t = time.time()
        # One base-model neutral sweep, reused by every contrast that needs it (four otherwise).
        base_neutral = likelihood.neutral_logprobs(model, tok, rows)
        scores["delta_own"] = likelihood.delta_own(model, tok, rows, base_neutral=base_neutral)
        scores["delta_own_null"] = likelihood.delta_own_null(model, tok, rows,
                                                             base_neutral=base_neutral)
        scores["distill_diag"] = likelihood.distill_diag(model, tok, rows,
                                                          base_neutral=base_neutral)
        kind = "ft" if primary()["poison_source"] == "teacher_ft" else "prompted"
        if kind == "prompted":
            scores["delta_teacher"] = likelihood.delta_teacher_prompted(
                model, tok, rows, base_neutral=base_neutral)
        phases = {"likelihoods": round(time.time() - t, 1)}
        # tda and dirderiv need ENCODED rows; encode once for both.
        enc = likelihood.encode_rows(tok, rows)

        student = PeftModel.from_pretrained(model, str(ADAPTERS / (key + e)), is_trainable=True)
        student.eval()
        t = time.time()
        scores["gain"] = likelihood.gain(student, model, tok, rows, base_neutral=base_neutral)
        if kind == "ft":
            # adapter-on minus adapter-off under the fine-tuned teacher, which needs its weights.
            # The b side is the BASE model's neutral sweep, already in hand: `model` itself is no
            # longer the base here, because the PeftModel above injected the student's LoRA into
            # it in place, so a forward through it would give the student's log-probabilities
            # (test_pod_score.py). Reusing the cached sweep is both correct and one sweep cheaper.
            tmodel = teacher_model()
            scores["delta_teacher"] = likelihood.delta_teacher(
                tok, rows, kind="ft", base_model=model, teacher_model=tmodel,
                b_logprobs=base_neutral)
            del tmodel  # a second base copy; free it before the attribution passes
            torch.cuda.empty_cache()
        phases["student_likelihoods"] = round(time.time() - t, 1)
        drops = dropped()
        t = time.time()
        scores["dir_student"] = dirderiv.dir_derivative(
            student, enc, with_cosine="cosine" not in drops, log=log)
        phases["dir_student"] = round(time.time() - t, 1)
        t = time.time()
        q = tda.query_rows(tok)
        if "null_query" in drops:
            q = {k: v for k, v in q.items() if k != tda.NULL_QUERY}
        attrib, attrib_meta = tda.score_rows(student, enc, q, log=log)
        if "diff_variants" not in drops:
            attrib = {**attrib, **tda.diff_scores(attrib)}
        phases["attribution"] = round(time.time() - t, 1)
        phases["dropped"] = sorted(drops)
        student.unload()
        torch.cuda.empty_cache()
        # dir_teacher: the derivative along an ACTUAL teacher delta where one exists (T0's
        # fine-tuned teacher), else along the teacher-proxy student. This is Cloud et al.'s
        # argument in its literal form (plan.md "Scorers"); it is the second thing dropped.
        # It runs AFTER the student is unloaded: `PeftModel.from_pretrained` injects in place, so
        # loading the teacher delta onto `model` while the student is still wrapped around it
        # overwrites the student's own "default" adapter and then strips every LoRA layer on
        # `unload()`, after which the attribution found no LoRA modules at all (test_pod_score.py).
        tdir = teacher_delta_adapter()
        if "dir_teacher" in drops or tdir is None:
            phases["dir_teacher"] = "skipped" if tdir else "no teacher delta available"
        else:
            t = time.time()
            tmodel = PeftModel.from_pretrained(model, str(tdir), is_trainable=True)
            tmodel.eval()
            scores["dir_teacher"] = dirderiv.dir_derivative(
                tmodel, enc, with_cosine="cosine" not in drops, log=log)
            tmodel.unload()
            torch.cuda.empty_cache()
            phases["dir_teacher"] = round(time.time() - t, 1)

        import numpy as np
        arrays = {}
        for fam, rec in scores.items():
            for vk, vv in (rec.items() if isinstance(rec, dict) else []):
                if isinstance(vv, (list, tuple)) and len(vv) == len(rows):
                    arrays[f"{fam}:{vk}"] = np.asarray(vv, dtype=np.float64)
        for vk, vv in (attrib or {}).items():
            arrays[vk] = np.asarray(vv, dtype=np.float64)
        np.savez_compressed(sdir / "scores.npz", **arrays)
        # data/mix/ is built on the pod and never pulled (probe_pod.RUN_OUTPUT excludes results/
        # from the PUSH, and data/ is not pulled at all), so the laptop would have scores with no
        # labels. Write the labels next to the scores, inside results/, which IS pulled.
        with open(sdir / "labels.jsonl", "w") as fh:
            fh.writelines(json.dumps({"idx": r["idx"], "pid": r["pid"], "label": r["label"],
                                     "identical": r["identical"]}) + "\n" for r in rows)
        meta = {"utc": utc(), "mixture": name, "student": key + e, "phases": phases,
                "n_rows": len(rows), "families": sorted(arrays.keys()),
                "attribution_meta": attrib_meta,
                "labels_file": "labels.jsonl",
                "truncation_mismatches": {k: v.get("n_mismatched_truncation")
                                          for k, v in scores.items()
                                          if isinstance(v, dict)
                                          and v.get("n_mismatched_truncation")},
                "teacher_kind": kind}
        write_json(sdir / "meta.json", meta)
        out_all[name] = meta
        log(f"score {name} done: {len(arrays)} score vectors, phases {phases}")
    write_json(RESULTS / f"score{e}.json",
               {"utc": utc(), "scored": out_all, "seconds": round(time.time() - t0, 1)})
    return 0


def teacher_delta_adapter() -> Path | None:
    """The adapter whose delta dir_teacher differentiates along, or None.

    T0's fine-tuned teacher if it exists (the real thing); otherwise the teacher-proxy student,
    which is the prompt distilled into weights. A prompted teacher has no weight delta of its own
    (plan.md "Scorers"), so without either there is nothing to differentiate along.
    """
    for cand in (ADAPTERS / "teacher_ft", ADAPTERS / "proxy_s0"):
        if (cand / "adapter_model.safetensors").exists():
            return cand
    return None


def teacher_model():
    """T0's fine-tuned teacher, as a separate PeftModel on its own base copy.

    delta_teacher under the fine-tuned organism is adapter-on minus adapter-off, so it needs the
    teacher's weights - a strictly stronger assumption than a guessed prompt (plan.md "Scorers").
    """
    from peft import PeftModel
    base, _ = load_base(attn="eager")
    m = PeftModel.from_pretrained(base, str(ADAPTERS / "teacher_ft"))
    m.eval()
    return m


# --- score stability (log.md 2026-10-14T22:23Z and 22:35Z) ---------------------------------------

STABILITY_N = 2_000          # rows scored twice; the dev pool is exactly K.N_DEV = 2,000
STABILITY_TOPK_FRAC = 0.1    # the top-k overlap reported, as a share of the scored rows
SPEARMAN_STABLE = 0.99
SPEARMAN_MARGINAL = 0.95
AUROC_DIFF_STABLE = 0.005


def _num(x):
    """A JSON-safe number: non-finite becomes null, so stability.json parses under strict JSON."""
    import numpy as np
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


def _finite_pair(a, b):
    """(a, b, n) over the positions where BOTH vectors are finite."""
    import numpy as np
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    return x[m], y[m], int(m.sum())


def pearson(a, b) -> float:
    """Pearson r over the positions where both vectors are finite (NaN if it is undefined)."""
    import numpy as np
    x, y, n = _finite_pair(a, b)
    if n < 2 or x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def spearman(a, b) -> float:
    """Spearman's rho: Pearson on average ranks, numpy only (no scipy).

    The ranks come from `p48.analyze._ranks`, which averages inside every tie group - the same
    tie rule the AUROC in this record uses, so a heavily tied scorer is treated identically by
    both numbers. Positions where either vector is non-finite are dropped from the pair.
    """
    import numpy as np

    from p48 import analyze
    x, y, n = _finite_pair(a, b)
    if n < 2:
        return float("nan")
    rx, ry = analyze._ranks(x), analyze._ranks(y)
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def topk_overlap(a, b, k: int) -> float:
    """|top-k(a) and top-k(b)| / k under `p48.data.top_k` (NaN lowest, ties by lower index).

    That is the selection rule the causal arms use, so this is the share of a swap budget the two
    orderings would spend on the same rows.
    """
    if k <= 0:
        return float("nan")
    ta = set(D.top_k([float(v) for v in a], k))
    tb = set(D.top_k([float(v) for v in b], k))
    return len(ta & tb) / float(k)


def stability_perm(n: int, seed: int = K.BOOT_SEED):
    """Ordering B, seeded.

    `tda.batch_plan` and `likelihood.batches` both sort rows by length with ties broken by
    POSITION, so presenting the same rows in a different order puts different rows in a batch
    together - which is the thing being measured.
    """
    import numpy as np
    return np.random.default_rng(seed).permutation(n)


def measure_stability(score_fn, n_rows: int, y, *, topk_frac: float = STABILITY_TOPK_FRAC,
                      perm=None) -> dict:
    """Score the same rows twice under two presentation orders and compare the two vectors.

    `score_fn(order)` returns {name: array of len(order)}, where position j holds the score of row
    `order[j]`. Ordering A is the identity and ordering B is `perm`; B is put back into row order
    (`out[perm] = raw`) BEFORE anything is compared, so a forgotten un-permutation shows up as a
    near-zero correlation rather than as a silent pass. `y` is `p48.analyze.labels`' primary
    vector (1 = poisoned and not byte-identical, 0 = clean, -1 = excluded) and the AUROCs use the
    rows with y >= 0. Returns the per-scorer record `stability_verdict` reads.
    """
    import numpy as np

    from p48 import analyze
    perm = stability_perm(n_rows) if perm is None else np.asarray(perm, dtype=int)
    if perm.shape != (n_rows,) or sorted(perm.tolist()) != list(range(n_rows)):
        raise ValueError(f"perm must be a permutation of range({n_rows})")
    t = time.time()
    raw_a = score_fn(np.arange(n_rows))
    sec_a = round(time.time() - t, 1)
    t = time.time()
    raw_b = score_fn(perm)
    sec_b = round(time.time() - t, 1)
    k = max(1, round(topk_frac * n_rows))
    y = np.asarray(y, dtype=int)
    keep = y >= 0
    out: dict[str, dict] = {}
    for name in sorted(set(raw_a) & set(raw_b)):
        a = np.asarray(raw_a[name], dtype=float)
        b_raw = np.asarray(raw_b[name], dtype=float)
        for who, arr in (("A", a), ("B", b_raw)):
            if arr.shape != (n_rows,):
                raise ValueError(f"{name}: ordering {who} returned {arr.shape}, want ({n_rows},)")
        b = np.empty(n_rows, dtype=float)
        b[perm] = b_raw  # un-permute: b_raw[j] is the score of row perm[j]
        auroc_a = analyze.auroc(a[keep], y[keep])
        auroc_b = analyze.auroc(b[keep], y[keep])
        both = np.isfinite(a) & np.isfinite(b)
        spread = np.abs(a - b)[both]
        sd = float(a[np.isfinite(a)].std()) if np.isfinite(a).any() else 0.0
        out[name] = {
            "pearson": _num(pearson(a, b)),
            "spearman": _num(spearman(a, b)),
            "topk_overlap": _num(topk_overlap(a, b, k)),
            "auroc_a": _num(auroc_a),
            "auroc_b": _num(auroc_b),
            "auroc_abs_diff": _num(abs(auroc_a - auroc_b)),
            "n_finite_both": int(both.sum()),
            "n_nonfinite_a": int((~np.isfinite(a)).sum()),
            "n_nonfinite_b": int((~np.isfinite(b)).sum()),
            "max_abs_diff": _num(spread.max() if spread.size else float("nan")),
            "max_abs_diff_over_sd": _num(spread.max() / sd if spread.size and sd > 0
                                         else float("nan")),
        }
    return {"scorers": out, "topk": k, "n_rows": n_rows, "n_labelled": int(keep.sum()),
            "n_positive": int((y == 1).sum()), "seconds_a": sec_a, "seconds_b": sec_b,
            "only_in_one_ordering": sorted(set(raw_a) ^ set(raw_b))}


def headline_difference(scorers: dict, frozen: dict | None = None) -> dict:
    """How stable is AUROC(ekfac) - AUROC(delta_own), the quantity the probe actually reports?

    The per-scorer figures bound each scorer's own ordering noise. They do not establish that the
    DIFFERENCE is resolvable, because the two scorers' noise may be correlated - the gap the
    stability step's own author flagged (log.md 2026-10-14). This recomputes the difference under
    each ordering from the already-measured AUROCs.

    `frozen` is `frozen.json`, so the variants are the ones the headline D is actually built from.
    Falling back to the lexicographically first name, as this did until the 2026-10-15
    cross-review, silently measured a DIFFERENT pair: `ekfac:cat:0.001` and `delta_own:mean`
    against the frozen `ekfac:cat:0.1` and `delta_own:sum`.
    """
    def pick(prefix: str, family: str):
        chosen = (frozen or {}).get("families", {}).get(family, {}).get("chosen")
        if chosen and chosen in scorers:
            return chosen, scorers[chosen]
        names = sorted(k for k in scorers if k.startswith(prefix))
        for n in names:  # prefer a finite pair
            r = scorers[n]
            if r.get("auroc_a") is not None and r.get("auroc_b") is not None:
                return n, r
        return (names[0], scorers[names[0]]) if names else (None, None)

    ek_name, ek = pick("ekfac:", "ekfac")
    do_name, do = pick("delta_own", "delta_own")
    if not ek or not do or ek.get("auroc_a") is None or do.get("auroc_a") is None:
        return {"computable": False,
                "reason": "need a finite AUROC pair for both ekfac and delta_own"}
    d_a = ek["auroc_a"] - do["auroc_a"]
    d_b = ek["auroc_b"] - do["auroc_b"]
    return {"computable": True, "ekfac": ek_name, "delta_own": do_name,
            "difference_ordering_a": round(d_a, 6), "difference_ordering_b": round(d_b, 6),
            "abs_change": round(abs(d_a - d_b), 6),
            "d_threshold": K.D_THRESHOLD,
            "resolvable_at_threshold": bool(abs(d_a - d_b) <= 0.3 * K.D_THRESHOLD),
            "note": ("one permutation, so this is a single draw; the two scorers share the same "
                     "forward passes, so correlated noise may flatter it")}


def stability_verdict(scorers: dict) -> str:
    """"scores_stable" / "marginal" / "scores_unstable" (`step_stability`'s docstring).

    A value that could not be computed (null) never counts as evidence of stability: the NaN
    comparisons below are all False, so such a scorer falls through to the weaker verdict.
    """
    def num(v):
        return float("nan") if v is None else float(v)

    if not scorers:
        return "scores_unstable"
    sp = [num(v.get("spearman")) for v in scorers.values()]
    ad = [num(v.get("auroc_abs_diff")) for v in scorers.values()]
    if all(s >= SPEARMAN_STABLE for s in sp) and all(d <= AUROC_DIFF_STABLE for d in ad):
        return "scores_stable"
    if all(s >= SPEARMAN_MARGINAL for s in sp):
        return "marginal"
    return "scores_unstable"


def step_stability(args) -> int:
    """Does a row's SCORE move when it is batched with different rows? (log.md 2026-10-14)

    The smoke found a row's bf16 per-row GRADIENT moving with its batch-mates - cosine min 0.889,
    median 0.964 over 8 rows - while the exact identity (per-row gradients sum to the batch's
    autograd gradient) held at 6e-07. That is bf16 batch-shape sensitivity, a property of the
    arithmetic rather than a bug. A score is a scalar projection of that gradient onto a
    preconditioned query, so the noise may partly average out; it may also not, and P48's headline
    is whether EK-FAC beats Delta_own at ranking poisoned rows, where P43's comparable effect was
    0.041 AUROC. If the scores carry comparable noise, a null result would be a measurement
    artefact rather than a finding.

    So: the same rows, encoded ONCE, scored TWICE - ordering A as they come, ordering B under a
    seeded permutation (`K.BOOT_SEED`), which `tda.batch_plan` and `likelihood.batches` turn into
    different batch compositions. B is un-permuted before anything is compared. Per scorer:
    Pearson, Spearman, the top-k overlap at k = 10% of the rows, and the AUROC under each ordering
    with its absolute difference - that last number is the one that decides whether a 0.04 AUROC
    gap is meaningful. Delta_own is included because it batches too, so it is not automatically
    the stable side of the comparison.

    Rows are the seeded dev pool (`p48.data.dev_mask`), never a held-out row, so these AUROCs
    reveal nothing the dev freeze does not already look at.

    A MEASUREMENT, not a gate: it always exits 0, and a missing input writes nothing so a later
    attempt can still make the number. -> results/stability.json
    """
    import numpy as np

    from p48 import analyze
    t_start = time.time()
    todo = scored_students()
    if not todo:
        log("stability: no gate decision, nothing to measure")
        return 0
    name, key = todo[0]  # scored_students puts the DETECTION mixture first
    # session 2 runs this without --recalibrate, but a B2 session leaves only the _e10 adapter.
    recal = f"_e{K.RECALIBRATE_EPOCHS}"
    cands = [key + recal, key] if getattr(args, "recalibrate", False) else [key, key + recal]
    adapter = next((ADAPTERS / c for c in cands
                    if (ADAPTERS / c / "adapter_model.safetensors").exists()), None)
    mix_path = K.MIX_DIR / f"{name}.jsonl"
    if adapter is None or not mix_path.exists():
        log(f"stability: need {mix_path} and an adapter among {cands}; skipping, nothing written")
        return 0

    rows_all = D.read_jsonl(mix_path)
    pool = [i for i, is_dev in enumerate(D.dev_mask(len(rows_all))) if is_dev]
    want = min(max(2, int(getattr(args, "n", None) or STABILITY_N)), len(pool))
    rng = np.random.default_rng([K.BOOT_SEED, 48])
    pick = (np.sort(rng.choice(len(pool), size=want, replace=False))
            if want < len(pool) else np.arange(len(pool)))
    rows = [rows_all[pool[i]] for i in pick]
    y, _y_all = analyze.labels(rows)

    import torch
    from peft import PeftModel

    from p48 import likelihood, tda
    log(f"stability: {name} / {adapter.name}: {len(rows)} dev rows of {len(rows_all)} "
        f"(active poison {int((y == 1).sum())}, clean {int((y == 0).sum())}, "
        f"byte-identical excluded {int((y == -1).sum())})")
    model, tok = load_base(attn="eager")  # scoring runs on EAGER (load_base's docstring)
    enc = likelihood.encode_rows(tok, rows)  # encoded ONCE; only the presentation order changes
    perm = stability_perm(len(rows))

    def lik_fn(order):
        # No cached base_neutral here: BOTH sides of the contrast must be batched in THIS order,
        # or ordering B would reuse ordering A's half and the movement would be understated.
        rec = likelihood.delta_own(model, tok, [rows[i] for i in order])
        return {"delta_own:sum": np.asarray(rec["sum"], dtype=float),
                "delta_own:mean": np.asarray(rec["mean"], dtype=float)}

    lik = measure_stability(lik_fn, len(rows), y, perm=perm)

    # The attribution scorers, on the cat query alone: the headline compares EK-FAC's cat variants
    # against Delta_own, and one query instead of ten cuts the query-gradient pass by 10x.
    student = PeftModel.from_pretrained(model, str(adapter), is_trainable=True)
    student.eval()
    q = {tda.PRIMARY_QUERY: tda.query_rows(tok)[tda.PRIMARY_QUERY]}

    def att_fn(order):
        scores, _meta = tda.score_rows(student, [enc[i] for i in order], q, log=log)
        return {sk: np.asarray(sv, dtype=float) for sk, sv in scores.items()}

    att = measure_stability(att_fn, len(rows), y, perm=perm)
    student.unload()
    torch.cuda.empty_cache()

    scorers = {**lik["scorers"], **att["scorers"]}
    verdict = stability_verdict(scorers)

    def worst(field: str, biggest: bool):
        xs = np.array([float("nan") if v.get(field) is None else float(v[field])
                       for v in scorers.values()], dtype=float)
        if not xs.size:
            return None
        return _num(xs.max() if biggest else xs.min())  # NaN propagates: not computable

    rec = {
        "utc": utc(), "mixture": name, "student": adapter.name,
        "n_rows": len(rows), "n_mixture_rows": len(rows_all),
        "row_pool": "dev", "n_dev_pool": len(pool),
        "perm_seed": K.BOOT_SEED, "query": tda.PRIMARY_QUERY,
        "topk": lik["topk"], "topk_frac": STABILITY_TOPK_FRAC,
        "n_labelled": lik["n_labelled"], "n_positive": lik["n_positive"],
        "thresholds": {"spearman_stable": SPEARMAN_STABLE,
                       "spearman_marginal": SPEARMAN_MARGINAL,
                       "auroc_abs_diff_stable": AUROC_DIFF_STABLE},
        "scorers": scorers,
        "min_spearman": worst("spearman", biggest=False),
        "min_topk_overlap": worst("topk_overlap", biggest=False),
        "max_auroc_abs_diff": worst("auroc_abs_diff", biggest=True),
        # THE HEADLINE QUANTITY'S OWN STABILITY. Per-scorer noise bounds each scorer alone, but
        # P48 reports a DIFFERENCE, and the two scorers' noise may be correlated (which would
        # cancel) or not (which would add). So recompute D = AUROC(ekfac) - AUROC(delta_own)
        # under each ordering and report how far apart the two D's are. If that is small beside
        # the D_THRESHOLD of 0.03, the comparison survives the noise; if it is comparable, the
        # design cannot resolve the gap it exists to measure.
        "headline_difference": headline_difference(
            scorers, read_json(RESULTS / "analysis" / "frozen.json") or {}),
        "verdict": verdict,
        "phases": {"likelihoods_a": lik["seconds_a"], "likelihoods_b": lik["seconds_b"],
                   "attribution_a": att["seconds_a"], "attribution_b": att["seconds_b"]},
        "seconds": round(time.time() - t_start, 1),
        "caveats": [
            ("both orderings score a subsample, so their batches differ from the full mixture's "
             "as well as from each other; this bounds the ordering noise, it does not reproduce "
             "the scoring run's own batches"),
            ("one permutation per scorer, so each number is one draw from the noise, not its "
             "distribution"),
            "a null value means the statistic was not computable, never that it passed",
        ],
    }
    write_json(RESULTS / "stability.json", rec)

    def fmt(v):
        return "n/a" if v is None else f"{v:.4f}"

    hd = rec["headline_difference"]
    if hd.get("computable"):
        log(f"stability: HEADLINE ekfac-delta_own = {hd['difference_ordering_a']:+.4f} (A) vs "
            f"{hd['difference_ordering_b']:+.4f} (B), moves {hd['abs_change']:.4f}; threshold "
            f"{hd['d_threshold']}; resolvable={hd['resolvable_at_threshold']}")
    log(f"stability: VERDICT {verdict} - {len(scorers)} scorers over {len(rows)} dev rows; "
        f"min Spearman {fmt(rec['min_spearman'])}, max |dAUROC| "
        f"{fmt(rec['max_auroc_abs_diff'])}, min top-{lik['topk']} overlap "
        f"{fmt(rec['min_topk_overlap'])} ({rec['seconds']}s)")
    return 0


def _kpilot_setup():
    """(mixture, rows, scores, control) or None - shared by the two k-pilot steps."""
    g = read_json(RESULTS / "gate.json") or {}
    mix_name = g.get("selection_mixture")
    if not mix_name:
        log("kpilot: no selection mixture")
        return None
    rows = D.read_jsonl(K.MIX_DIR / f"{mix_name}.jsonl")
    scores = load_selection_scores(mix_name, len(rows))
    # Prefer the control the gate recorded: results/eval/ is pulled to the laptop but never
    # pushed back, so on session 2's pod view_rate() returns None and `or 0.0` would silently
    # make every excess look huge and freeze k at the first rung (pod-steps tests, 2026-10-14).
    control = g.get("control_rate")
    if control is None:
        e = f"_e{K.RECALIBRATE_EPOCHS}" if g.get("recalibrated") else ""
        control = view_rate(K.run_key(K.mixture_name(0, suffix()), K.GATE_SEED) + e)
    if control is None:
        log("kpilot: no 0% control rate (gate.json has none and no eval summary is present); "
            "push results/gate.json and results/eval/ for session 2")
        return None
    return mix_name, rows, scores, control


def kpilot_prefix(frac: float) -> str:
    return f"kp{int(frac * 1000)}_"


def step_kpilotarms(args) -> int:
    """PEFT: build one rung's swap arms and print their run keys for run.sh to train.

    The pilot is split across the two venvs because it both TRAINS (peft) and EVALUATES (vllm).
    Doing both in one step put a vLLM import in the peft venv and killed the run after 24
    minutes of training - the same fault as attempt 11's teacher step (log.md 2026-10-15).
    """
    setup = _kpilot_setup()
    if setup is None:
        return 1
    mix_name, rows, scores, _control = setup
    if args.rung is None or not (0 <= args.rung < len(K.K_LADDER)):
        log(f"kpilotarms: --rung must be 0..{len(K.K_LADDER) - 1}")
        return 1
    frac = K.K_LADDER[args.rung]
    k = max(1, round(frac * len(rows)))
    sel = D.selections_from_scores(rows, k, scores=scores, include_random=False,
                                   include_active=False)
    prefix = kpilot_prefix(frac)
    D.build_arms(mix_name, sel, prefix=prefix, poison_source=primary()["poison_source"],
                 ops=("swap",))
    keys = [K.run_key(K.arm_name(s, "swap", prefix), K.GATE_SEED) for s in sel]
    write_json(RESULTS / f"kpilot_rung{args.rung}.json",
               {"utc": utc(), "rung": args.rung, "frac": frac, "k": k,
                "selectors": sorted(sel), "keys": keys, "mixture": mix_name})
    print(" ".join(keys))
    return 0


def step_kpilotdecide(args) -> int:
    """PEFT: does this rung keep the floor? rc 0 = k frozen, 3 = step down, 4 = ladder exhausted.

    The floor matters because P43's selector comparison was measured where every informed arm
    sat at 0.0% and no ranking was possible (plan.md "k pilot").
    """
    setup = _kpilot_setup()
    if setup is None:
        return 1
    mix_name, rows, _scores, control = setup
    rung_rec = read_json(RESULTS / f"kpilot_rung{args.rung}.json") or {}
    keys = rung_rec.get("keys") or []
    if not keys:
        log(f"kpilotdecide: no keys recorded for rung {args.rung}")
        return 1
    missing = [key for key in keys if view_rate(key) is None]
    if missing:
        log(f"kpilotdecide: no eval summary for {missing}")
        return 1
    excesses = {key: round((view_rate(key) or 0.0) - control, 3) for key in keys}
    clears = all(v >= K.K_FLOOR_PTS for v in excesses.values())
    rec = read_json(RESULTS / "kpilot.json") or {"utc": utc(), "mixture": mix_name,
                                                 "control": control, "rungs": []}
    rec["rungs"] = [r for r in rec.get("rungs", []) if r.get("rung") != args.rung]
    rec["rungs"].append({"rung": args.rung, "frac": rung_rec["frac"], "k": rung_rec["k"],
                         "excess": excesses, "clears": clears})
    rec["rungs"].sort(key=lambda r: r["rung"])
    log(f"kpilot rung {args.rung} frac={rung_rec['frac']} k={rung_rec['k']} "
        f"excess={excesses} clears={clears} (floor {K.K_FLOOR_PTS})")
    last = args.rung >= len(K.K_LADDER) - 1
    if clears:
        rec["chosen"] = {"frac": rung_rec["frac"], "k": rung_rec["k"], "clears": True,
                         "rung": args.rung}
        rec.pop("at_risk", None)
        write_json(RESULTS / "kpilot.json", rec)
        log(f"kpilot: k frozen at {rung_rec['k']} ({rung_rec['frac']:.3f} of rows)")
        return 0
    if last:
        frac = K.K_LADDER[-1]
        rec["chosen"] = {"frac": frac, "k": max(1, round(frac * len(rows))), "clears": False,
                         "rung": args.rung}
        rec["at_risk"] = True
        write_json(RESULTS / "kpilot.json", rec)
        log("kpilot: no rung cleared the floor; using the smallest k and flagging the selector "
            "contrast as at risk of being inconclusive")
        return 4
    write_json(RESULTS / "kpilot.json", rec)
    return 3


def load_selection_scores(mix_name: str, n_rows: int) -> dict[str, list[float]]:
    import numpy as np
    frozen = read_json(RESULTS / "analysis" / "frozen.json") or {}
    npz = RESULTS / f"scores_{mix_name}" / "scores.npz"
    if not npz.exists():
        raise SystemExit(f"no scores for {mix_name}; run score first")
    z = np.load(npz)
    out = {}
    for sel, default in (("ekfac", None), ("delta_own", "delta_own:sum"),
                         ("dir_student", "dir_student:cosine")):
        name = frozen.get(sel) or default
        if name is None:
            cands = [k for k in z.files if k.startswith("ekfac:")]
            if not cands:
                raise SystemExit("no ekfac scores present")
            name = min(cands)
        if name not in z.files:
            raise SystemExit(f"frozen variant {name!r} missing from {npz}")
        arr = z[name]
        if len(arr) != n_rows:
            raise SystemExit(f"{name}: {len(arr)} scores for {n_rows} rows")
        out[sel] = arr.tolist()
    return out


def step_arms(args) -> int:
    g = read_json(RESULTS / "gate.json") or {}
    kp = read_json(RESULTS / "kpilot.json") or {}
    mix_name = g.get("selection_mixture")
    k = (kp.get("chosen") or {}).get("k")
    if not mix_name or not k:
        log("arms: need gate.json and kpilot.json")
        return 1
    rows = D.read_jsonl(K.MIX_DIR / f"{mix_name}.jsonl")
    scores = load_selection_scores(mix_name, len(rows))
    sel = D.selections_from_scores(rows, k, scores=scores)
    rec = D.build_arms(mix_name, sel, poison_source=primary()["poison_source"])
    rec["k"] = k
    rec["mixture"] = mix_name
    write_json(RESULTS / "arms.json", rec)
    log(f"arms: k={k} selectors={sorted(sel)}")
    return 0


def step_stage2keys(args) -> int:
    rec = read_json(RESULTS / "arms.json") or {}
    # step_arms writes build_arms' per-arm counts at the TOP level, plus "k" and "mixture": there
    # is no nested "arms" dict to read (the `run["steps"]` bug class, log.md 2026-10-14). Each arm
    # entry carries its own "op", so the arm names are the entries that have one.
    sels = sorted(name for name, v in rec.items()
                  if isinstance(v, dict) and v.get("op") in K.OPERATIONS) or None
    keys = []
    if sels:
        for name in sels:
            for seed in K.STAGE2_SEEDS:
                keys.append(K.run_key(name, seed))
    else:
        for s in K.SELECTORS:
            for op in K.SESSION_OPS:
                name = K.arm_name(s, op)
                if (K.ARMS_DIR / f"{name}.jsonl").exists():
                    keys += [K.run_key(name, seed) for seed in K.STAGE2_SEEDS]
    g = read_json(RESULTS / "gate.json") or {}
    ref = ([K.run_key(g["selection_mixture"], seed) for seed in K.STAGE2_SEEDS]
           if g.get("selection_mixture") else [])
    # THE REFERENCE FIRST, then the arms. Keys were emitted alphabetically, which put `swap` -
    # the primary operation - and the reference LAST, exactly where the wall-clock guard cuts.
    # Every contrast is measured against the reference, so it must never be the casualty
    # (log.md 2026-10-15).
    order = {op: i for i, op in enumerate(K.OPERATIONS)}
    keys.sort(key=lambda k: (order.get(k.split("_")[0], 99), k))
    print(" ".join(ref + keys))
    return 0


# Which venv each step must run in. `vllm` steps generate text (vLLM is only in /ws/venv-vllm);
# `peft` steps train or score with torch/peft. run.sh is checked against this table by
# tests/test_pod_venvs.py, after a vLLM import inside a peft step cost a launch (attempt 11).
VENV_BY_STEP = {
    "download": "peft", "smoke": "peft", "teacher": "peft", "teachercheck": "vllm",
    "teacherqa": "vllm", "gen": "vllm", "mix": "peft", "train": "peft", "eval": "vllm",
    "t0": "peft", "t0keys": "peft", "gatekeys": "peft", "gatedec": "peft", "score": "peft",
    "stability": "peft", "kpilotarms": "peft", "kpilotdecide": "peft",
    "arms": "peft", "stage2keys": "peft",
}

STEPS = {
    "download": step_download, "smoke": step_smoke, "teacher": step_teacher,
    "teachercheck": step_teachercheck, "teacherqa": step_teacherqa, "gen": step_gen,
    "mix": step_mix, "train": step_train, "eval": step_eval, "t0": step_t0,
    "t0keys": step_t0keys, "gatekeys": step_gatekeys, "gatedec": step_gatedec,
    "score": step_score, "stability": step_stability,
    "kpilotarms": step_kpilotarms, "kpilotdecide": step_kpilotdecide,
    "arms": step_arms, "stage2keys": step_stage2keys,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=sorted(STEPS))
    ap.add_argument("--keys", nargs="*", default=[])
    ap.add_argument("--sources", nargs="*", default=list(K.SOURCES))
    ap.add_argument("--tag", default=None)
    ap.add_argument("--n", type=int, default=STABILITY_N,
                    help="stability: rows scored twice (capped at the dev pool)")
    ap.add_argument("--base", action="store_true")
    ap.add_argument("--fallback-source", action="store_true")
    ap.add_argument("--with-rescue", action="store_true")
    ap.add_argument("--rescue-epochs", action="store_true")
    ap.add_argument("--recalibrate", action="store_true")
    ap.add_argument("--rung", type=int, default=None)
    args = ap.parse_args(argv)
    RESULTS.mkdir(parents=True, exist_ok=True)
    return STEPS[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
