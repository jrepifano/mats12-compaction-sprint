"""P17 pod entry (plan.md "Automatic rules"): download, load, smoke, benchmark, then B's eval and
per seed K_s train + eval, E_s train + eval, under a deadline.

    python -m p17.pod download        # model @ MODEL_REV and both lenses (P06's files)
    python -m p17.pod run [options]   # everything else; resumable per model (done.json)
    python -m p17.pod run --tiny ...  # CPU dry run on a tiny random Gemma 4 (no download)

Options: --seeds 1,2,3; --penalty-reader logit_lens|jlens|jpp (variant V-J); --lambda-scale
(follow-up 1); --n-train / --n-eval / --n-transplant (dry runs); --mb (skip the benchmark's
choice); --results DIR.

Return codes: 0 complete; 3 a smoke check failed; 4 stopped early (deadline) or a model
incomplete; 5 the plan's stop rule (B's uplift < 3 points with its CI including 0).

**Smoke** (results/smoke.json; a resumed run trusts only a persisted passing smoke):
- data: 0 train/eval overlaps; the training file's sha256;
- `cache_vs_full` on 4 items: the full-sequence forward (`p06.stage2.forward_read`) against the
  cached-prefix continuation: logit-lens top-1 agreement >= 0.99 at the 114 read positions, max
  digit log-prob difference (layers 30-59) <= 0.1, the same greedy output on >= 3 of 4;
- `attention`: one E loss on 2 items with sdpa and with eager agrees within 2% (AGENTS.md:
  validate faster attention against eager);
- `b_acc50`: B's dots100 accuracy on the first 50 eval items >= 0.2;
- `penalty_matches_eval`: the training path's digit log-probs (layers 30-59, label + dots) equal
  the eval path's stored readouts within 0.1 on 4 items, and the hard statistic's hits agree;
- the benchmark's E steps: finite loss, non-zero adapter gradients.
**Benchmark** (results/benchmark.json): lambda and the baseline from 32 training items on the
base model; E steps at microbatch 1, 2, 4 (1 warm-up + 3 timed each; an OOM or a peak over 90%
of GPU memory rules a size out); one model's eval on 20 items and 3 transplant pairs. The
projection drops jobs from the end (K of the last seed, then its E) until it fits the deadline;
before each job it is re-checked with the times measured so far.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
from p06 import stage2 as S2

from . import common as C
from . import data as D
from . import evaluate as EV
from . import penalty as PEN
from . import schema as S
from . import train as T
from .train import utc, write_json

SETUP_BEFORE_T0_S = 20 * 60  # boot, push and venv setup happen before run.sh starts
PULL_MARGIN_S = 20 * 60  # final pull and teardown before the killswitch's salvage margin
AGREE_MIN = 0.99
LP_TOL = 0.1
ATTN_REL_TOL = 0.02
B_ACC_MIN = 0.2
MEM_FRAC = 0.9


def log(msg: str) -> None:
    print(f"[p17 {utc()}] {msg}", flush=True)


def artifacts_dir() -> Path:
    return Path(os.environ.get("P17_ARTIFACTS", "/ws/p17_artifacts"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------- download and load


def cmd_download(_a) -> int:
    from emx.i1a import hfget

    root = artifacts_dir()
    got: dict[str, Any] = {}
    t = time.time()
    files = S2.model_files(S.MODEL_ID, S.MODEL_REV)
    m = hfget.fetch(S.MODEL_ID, files, revision=S.MODEL_REV, local_dir=root / "gemma")
    got["model_dir"] = str(m["config.json"].parent)
    got["model_bytes"] = sum(p.stat().st_size for p in m.values())
    for name, f in S2.LENS_FILES.items():
        p = hfget.fetch(S2.LENS_REPO, [f], revision=S2.LENS_REV, local_dir=root / "lenses")[f]
        got[name] = str(p)
    got["seconds"] = round(time.time() - t, 1)
    write_json(root / "paths.json", got)
    log(f"downloaded {got['model_bytes'] / 1e9:.1f} GB in {got['seconds']} s")
    return 0


def load(attn: str = "sdpa", lens_names: tuple[str, ...] = ("jlens", "jpp")):
    """(model, tok, readers, info) on the GPU, P06 F3's loading (bf16) with J-Lens maps."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    paths = json.loads((artifacts_dir() / "paths.json").read_text())
    torch.backends.cuda.matmul.allow_tf32 = False
    tok = AutoTokenizer.from_pretrained(paths["model_dir"], local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        paths["model_dir"], dtype=torch.bfloat16, attn_implementation=attn, device_map="cuda",
        local_files_only=True)
    model.eval()
    for p in model.parameters():
        p.requires_grad_(False)
    blocks, _ = C.text_model(model)
    if len(blocks) != S.N_LAYERS:
        raise RuntimeError(f"{len(blocks)} decoder blocks, expected {S.N_LAYERS}")
    lenses, info = {}, {"model_class": type(model).__name__, "lenses": {}}
    for name in lens_names:
        maps, meta = S2.load_lens(paths[name])
        free, total = torch.cuda.mem_get_info()
        need = sum(v.numel() * v.element_size() for v in maps.values())
        resident = free - need > 0.35 * total  # keep room for training
        lenses[name] = S2.Lens(maps, "cuda", resident)
        info["lenses"][name] = {**meta, "resident": resident, "bytes": need}
    rd = C.make_readers(model, tok, lenses)
    info.update(softcap=rd.softcap, digit_ids=rd.digit_ids,
                eot=C.end_of_turn_id(tok, model))
    return model, tok, rd, info


def load_tiny():
    from . import tiny

    tok = tiny.tokenizer()
    model = tiny.model(eot=C.end_of_turn_id(tok))
    for p in model.parameters():
        p.requires_grad_(False)
    import torch

    torch.manual_seed(1)
    lenses = {n: S2.Lens({i: torch.randn(32, 32).half() * 0.2 for i in range(59)}, "cpu", True)
              for n in ("jlens", "jpp")}
    rd = C.make_readers(model, tok, lenses)
    return model, tok, rd, {"model_class": "tiny", "eot": C.end_of_turn_id(tok, model)}


def env_info(device: str) -> dict:
    out: dict[str, Any] = {"utc": utc(), "commit": os.environ.get("EMX_GIT_COMMIT"),
                           "model": f"{S.MODEL_ID}@{S.MODEL_REV}",
                           "lens": f"{S2.LENS_REPO}@{S2.LENS_REV}"}
    import peft
    import torch
    import transformers

    out.update(torch=torch.__version__, transformers=transformers.__version__,
               peft=peft.__version__)
    if device == "cuda":
        out.update(gpu=torch.cuda.get_device_name(0),
                   gpu_mem_gb=round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1))
    return out


# --------------------------------------------------------------------- deadline


def stop_at(t0: float) -> float:
    if "P17_BUDGET_S" in os.environ:
        return t0 + float(os.environ["P17_BUDGET_S"])
    cfg = tomllib.loads((S.PROBE / "probe.toml").read_text())
    return t0 + float(cfg["pod"]["gpu_hours"]) * 3600 - SETUP_BEFORE_T0_S - PULL_MARGIN_S


def plan_jobs(seeds: list[int], est: dict, budget_s: float) -> tuple[list[str], float]:
    """Jobs B, K1, E1, ... dropped from the end (the last seed's K, then its E) until the
    projected seconds fit `budget_s`. est: train_K, train_E, eval (seconds per job)."""
    jobs = ["B"] + [f"{a}{s}" for s in seeds for a in S.ARMS]

    def cost(js: list[str]) -> float:
        return sum(est["eval"] + (est[f"train_{j[0]}"] if j != "B" else 0.0) for j in js)

    while len(jobs) > 1 and cost(jobs) > budget_s:
        last = max(int(j[1:]) for j in jobs if j != "B")
        drop = f"K{last}" if f"K{last}" in jobs else f"E{last}"
        jobs.remove(drop)
    return jobs, cost(jobs)


# --------------------------------------------------------------------- smoke


def smoke(model, tok, rd, exs, shots, train_items, device: str, out: Path, tiny: bool) -> dict:
    import torch

    res: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    t = time.time()
    # cached prefix vs the full sequence
    prep4 = EV.prepare(tok, exs[:4], shots)
    pre = C.prefix_cache(model, prep4["dots100"]["prefix"], device)
    ctx = rd.ctx(model, tok, device)
    agree = n = same_gen = 0
    max_diff = 0.0
    eos = EV.eos_ids(model)
    for sp in prep4["dots100"]["splits"]:
        with torch.inference_mode():
            hs_full, logits_full = S2.forward_read(ctx, sp.ids, sp.read[1:])
            lp_full = PEN.readout_lp(list(hs_full[:, None]), rd, S.PEN_LAYERS, checkpoint=False)
            blocks, _ = C.text_model(model)
            win = torch.as_tensor(sp.rel(sp.read[1:]), device=device)
            with C.capture_blocks(blocks, win, keep_graph=False) as hs_c:
                g = EV.greedy(model, pre, [sp.cont], eos, rd.digit_ids, device, keep=114)
            lp_c = PEN.readout_lp(hs_c, rd, S.PEN_LAYERS, checkpoint=False)
            agree += int((logits_full.argmax(-1).cpu().numpy() == g["top1"][0]).sum())
            n += len(sp.read) - 1
            max_diff = max(max_diff, float((lp_full - lp_c).abs().max()))
            ref = S2.greedy(ctx, sp.ids)[0]
            same_gen += int(ref == g["gen"][0])
    res["cache_vs_full"] = {"top1_agree": agree / n, "max_digit_lp_diff": max_diff,
                            "same_greedy": same_gen, "n": 4}
    checks["cache_vs_full"] = agree / n >= AGREE_MIN and max_diff <= LP_TOL and same_gen >= 3
    del pre
    # sdpa vs eager on one E loss (skipped on the tiny model: already sdpa-vs-full checked)
    if not tiny:
        items2 = train_items[:2]
        prefix_ids = items2[0].split.prefix
        losses = {}
        for impl in ("sdpa", "eager"):
            model.set_attn_implementation(impl)
            with torch.no_grad():
                p = C.prefix_cache(model, prefix_ids, device)
                r = T.batch_forward(model, rd, p, items2, device, True, keep_graph=False)
                yd = torch.tensor([it.y_digit for it in items2], device=r["lp"].device)
                pen, _ = PEN.smooth_penalty(r["lp"], r["lp"].mean(0), yd)
                losses[impl] = float((r["ce"] + pen).mean())
        model.set_attn_implementation("sdpa")
        rel = abs(losses["sdpa"] - losses["eager"]) / max(abs(losses["eager"]), 1e-6)
        res["attention"] = {**losses, "rel_diff": rel}
        checks["attention"] = rel <= ATTN_REL_TOL
    # B's accuracy on the first 50 items, and the penalty path vs the eval path
    n50 = 50 if not tiny else 4
    prep50 = EV.prepare(tok, exs[:n50], shots)
    tmp = out / "smoke_eval"
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    r50 = EV.answers_pass(model, tok, rd, "B", "dots100", prep50, exs[:n50], tmp, device, 16,
                          ctx=ctx, log=log)
    res["b_acc50"] = r50["acc"]
    checks["b_acc50"] = tiny or r50["acc"] >= B_ACC_MIN
    items4 = T.make_items(tok, exs[:4], shots, C.end_of_turn_id(tok))
    p = C.prefix_cache(model, prep50["dots100"]["prefix"], device)
    with torch.no_grad():
        lp_train = T.batch_forward(model, rd, p, items4, device, True, keep_graph=False)["lp"]
    lp_train = lp_train.float().cpu().numpy()
    lp_eval = np.stack([S2.read_npz(S2.npz_path(f"varbind:{ex['idx']}", "dots100",
                                                tmp / "readouts"))["ro"]["logit_lens"]
                        ["digit_lp"][S.MIN_LAYER:, :102].astype(np.float32) for ex in exs[:4]])
    yd = np.array([it.y_digit for it in items4])
    h_tr = PEN.hard_stats(lp_train, lp_train.mean(0)).argmax(2) == yd[:, None]
    h_ev = PEN.hard_stats(lp_eval, lp_eval.mean(0)).argmax(2) == yd[:, None]
    diff = float(np.abs(lp_train - lp_eval).max())
    res["penalty_matches_eval"] = {"max_lp_diff": diff, "hits_equal": bool((h_tr == h_ev).all())}
    checks["penalty_matches_eval"] = diff <= LP_TOL and bool((h_tr == h_ev).all())
    res["seconds"] = round(time.time() - t, 1)
    res["checks"] = checks
    return res


# --------------------------------------------------------------------- benchmark


def benchmark(model, tok, rd, exs, shots, train_items, val_items, prefix_ids, device: str,
              out: Path, lens: Any, mbs: tuple[int, ...], tiny: bool) -> tuple[Any, dict]:
    import torch

    res: dict[str, Any] = {}
    t = time.time()
    cal = T.calibrate(model, rd, prefix_ids, train_items[:S.N_LAMBDA_ITEMS], device, mb=4,
                      lens=lens)
    res["calibration"] = {k: v for k, v in cal.items() if k != "baseline"}
    res["calibration_s"] = round(time.time() - t, 1)
    baseline = cal["baseline"]
    total = torch.cuda.get_device_properties(0).total_memory if device == "cuda" else None
    per_mb: dict[int, dict] = {}
    for mb in mbs:
        name = f"bench{mb}"
        cfg = T.TrainConfig(arm="E", seed=99, lam=cal["lambda"], mb=mb, max_steps=4,
                            val_every=10**9)
        if device == "cuda":
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
        try:
            model, rec = T.train_arm(model, tok, rd, name, cfg, train_items, val_items[:4],
                                     prefix_ids, baseline, out / "bench", device, lens=lens,
                                     log=log)
            steps = rec["steps"]
            ok = rec["complete"] and all(np.isfinite(s["loss"]) and s["grad_norm"] > 0
                                         for s in steps)
            step_s = float(np.mean([s["seconds"] for s in steps[1:]])) if len(steps) > 1 \
                else None
            peak = torch.cuda.max_memory_allocated() if device == "cuda" else 0
            fits = ok and (total is None or peak < MEM_FRAC * total)
            per_mb[mb] = {"ok": ok, "fits": fits, "step_s": step_s,
                          "peak_gb": round(peak / 1e9, 2), "val_s": rec["timing"]["val_s"]}
        except torch.cuda.OutOfMemoryError as e:  # type: ignore[attr-defined]
            per_mb[mb] = {"ok": False, "fits": False, "error": repr(e)[:300]}
        finally:
            from peft import PeftModel

            if isinstance(model, PeftModel):
                model = C.drop_adapter(model, name)
            if device == "cuda":
                torch.cuda.empty_cache()
        log(f"P17_BENCH mb {mb}: {per_mb[mb]}")
    res["per_mb"] = per_mb
    # a val pass is 2 of the bench's passes on 4 items (step 0 and the end), scaled to N_VAL
    vs = [r["val_s"] for r in per_mb.values() if r.get("val_s")]
    res["val_pass_s"] = (min(vs) / 2 * S.N_VAL / 4) if vs else 0.0
    good = {mb: r for mb, r in per_mb.items() if r.get("fits") and r.get("step_s")}
    res["mb"] = min(good, key=lambda m: good[m]["step_s"]) if good else None
    res["step_E_s"] = good[res["mb"]]["step_s"] if good else None
    # one model's eval on 20 items and 3 transplant pairs (adapter disabled)
    n = 20 if not tiny else 4
    prep = EV.prepare(tok, exs[:n], shots)
    t = time.time()
    tmp = out / "bench_eval"
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    done = EV.eval_model(model, tok, rd, "B", prep, exs[:n], shots, exs, tmp, device,
                         n_transplant=3 if not tiny else 1, log=log)
    tim = done["timing_s"]
    res["eval_item_s"] = (tim["base"] + tim["ask"] + tim["dots100"]) / n
    res["transplant_pair_s"] = tim["transplant"] / (3 if not tiny else 1)
    res["eval_bench_s"] = round(time.time() - t, 1)
    return model, {**res, "baseline": baseline}


# --------------------------------------------------------------------- run


def uplift(rows: list[dict]) -> dict:
    by = {(r["idx"], r["kind"]): bool(r["correct"]) for r in rows}
    ids = sorted({r["idx"] for r in rows})
    d = np.array([by[(i, "dots100")] - by[(i, "base")] for i in ids], dtype=float)
    rng = np.random.default_rng(S.SEED)
    bs = d[rng.integers(0, len(d), size=(S.N_BOOT, len(d)))].mean(1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"n": len(d), "mean": float(d.mean()), "lo": float(lo), "hi": float(hi)}


def cmd_run(a) -> int:
    import torch

    t0 = float(os.environ.get("P17_T0", time.time()))
    out = Path(a.results) if a.results else S.RESULTS
    out.mkdir(parents=True, exist_ok=True)
    device = "cpu" if a.tiny else "cuda"
    deadline = stop_at(t0) if not a.tiny else time.time() + 3600
    run: dict[str, Any] = {"env": env_info(device), "args": vars(a), "t0": t0,
                           "deadline_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         time.gmtime(deadline)),
                           "timing": {}, "complete": False}
    status = out / "run.json"

    def save() -> None:
        write_json(status, run)

    # data
    exs, shots = D.load_eval()
    exs = exs[: a.n_eval]
    train_raw = D.load_train()[: a.n_train]
    run["data"] = {"overlaps": D.check_no_overlap(train_raw, D.load_eval()[0]),
                   "train_sha256": sha256(S.TRAIN_DATA), "n_train": len(train_raw),
                   "n_eval": len(exs)}
    if run["data"]["overlaps"]:
        run["stopped"] = "train/eval overlap"
        save()
        return 3
    pool = D.load_eval()[0]
    t = time.time()
    model, tok, rd, info = load_tiny() if a.tiny else load()
    run["timing"]["load_s"] = round(time.time() - t, 1)
    run["load"] = info
    eot = info["eot"]
    lens = rd.lenses.get(a.penalty_reader) if a.penalty_reader != "logit_lens" else None
    if a.penalty_reader != "logit_lens" and lens is None:
        raise RuntimeError(f"penalty reader {a.penalty_reader} not loaded")
    train_items = T.make_items(tok, train_raw, shots, eot)
    val_items = T.make_items(tok, exs[: S.N_VAL], shots, eot)
    prefix_ids = C.check_shared_prefix([it.split for it in train_items + val_items])
    save()

    # smoke
    sm_path = out / "smoke.json"
    prev = json.loads(sm_path.read_text()) if sm_path.exists() else {}
    if prev.get("checks") and all(prev["checks"].values()):
        log("smoke: persisted passing smoke, not rerun")
        sm = prev
    else:
        sm = smoke(model, tok, rd, exs, shots, train_items, device, out, a.tiny)
        write_json(sm_path, sm)
    log(f"P17_SMOKE {sm['checks']}")
    if not all(sm["checks"].values()):
        run["stopped"] = "smoke failed: " + ", ".join(k for k, v in sm["checks"].items() if not v)
        log(f"P17_SMOKE_FAIL {run['stopped']}")
        save()
        return 3

    # benchmark, lambda and the job plan
    bm_path = out / "benchmark.json"
    base_path = out / "baseline.pt"
    if bm_path.exists() and base_path.exists() and not a.tiny:
        bm = json.loads(bm_path.read_text())
        baseline = torch.load(base_path).to(device)
    else:
        mbs = (a.mb,) if a.mb else ((1, 2, 4) if not a.tiny else (1,))
        model, bm = benchmark(model, tok, rd, exs, shots, train_items, val_items, prefix_ids,
                              device, out, lens, mbs, a.tiny)
        baseline = bm.pop("baseline")
        torch.save(baseline.cpu(), base_path)
        write_json(bm_path, bm)
    if bm["mb"] is None:
        run["stopped"] = "benchmark: no microbatch size fits"
        save()
        return 3
    lam = bm["calibration"]["lambda"] * a.lambda_scale
    n_steps = len(train_items) // S.EFFECTIVE_BATCH
    n_val = n_steps // S.VAL_EVERY + 2
    val_s = bm["val_pass_s"]
    est = {"train_E": n_steps * bm["step_E_s"] + n_val * val_s + 60,
           "train_K": n_steps * bm["step_E_s"] * 0.8 + n_val * val_s + 60,
           "eval": len(exs) * bm["eval_item_s"] + min(a.n_transplant, len(exs)) *
           bm["transplant_pair_s"] + 120}
    jobs, proj = plan_jobs(a.seeds, est, deadline - time.time())
    run.update(lam=lam, mb=bm["mb"], est_s=est, jobs=jobs, projected_s=round(proj),
               budget_s=round(deadline - time.time()))
    log(f"P17_PLAN lambda {lam:.3f} mb {bm['mb']} jobs {jobs} projected {proj / 60:.0f} min "
        f"of {(deadline - time.time()) / 60:.0f}")
    save()

    # B, then per seed K, E
    prep = EV.prepare(tok, exs, shots)
    measured: dict[str, list[float]] = {"train_K": [], "train_E": [], "eval": []}
    rc = 0
    for job in jobs:
        left = deadline - time.time()
        need = (np.mean(measured["eval"]) if measured["eval"] else est["eval"])
        if job != "B":
            k = f"train_{job[0]}"
            need += np.mean(measured[k]) if measured[k] else est[k]
        if need > left:
            run.setdefault("skipped", []).append(job)
            log(f"P17_SKIP {job}: needs {need / 60:.0f} min, {left / 60:.0f} left")
            rc = 4
            continue
        if job != "B":
            arm, seed = job[0], int(job[1:])
            rec_path = out / "train" / job / "record.json"
            ad = out / "adapters" / job / job
            if rec_path.exists() and json.loads(rec_path.read_text()).get("complete") and \
                    ad.exists():
                model.load_adapter(str(ad), adapter_name=job)
                log(f"{job}: trained adapter loaded from {ad}")
            else:
                cfg = T.TrainConfig(arm=arm, seed=seed, lam=lam if arm == "E" else 0.0,
                                    mb=bm["mb"], penalty_reader=a.penalty_reader,
                                    max_steps=a.max_steps, full_grad=a.full_grad)
                t = time.time()
                model, rec = T.train_arm(model, tok, rd, job, cfg, train_items, val_items,
                                         prefix_ids, baseline, out, device, lens=lens,
                                         stop_at=deadline, log=log)
                measured[f"train_{arm}"].append(time.time() - t)
                run["timing"][f"train_{job}"] = round(time.time() - t, 1)
                if not rec["complete"]:
                    run.setdefault("incomplete", []).append(job)
                    rc = 4
                    save()
                    if "non-finite" in rec.get("stopped", ""):
                        continue  # a diverged arm is not evaluated
        t = time.time()
        model.eval()
        EV.eval_model(model, tok, rd, job, prep, exs, shots, pool, out, device,
                      n_transplant=a.n_transplant, log=log)
        measured["eval"].append(time.time() - t)
        run["timing"][f"eval_{job}"] = round(time.time() - t, 1)
        if job == "B":
            rows = [json.loads(x) for x in (out / "eval" / "B" / "answers.jsonl")
                    .read_text().splitlines() if x.strip()]
            run["b_uplift"] = u = uplift(rows)
            log(f"P17_B_UPLIFT {u}")
            if not a.tiny and u["mean"] < 0.03 and u["lo"] <= 0:
                run["stopped"] = "stop rule: B's uplift < 3 points, CI includes 0"
                save()
                return 5
        else:
            model = C.drop_adapter(model, job)  # saved under adapters/; frees its weights
        save()
        if device == "cuda":
            torch.cuda.empty_cache()
    run["complete"] = rc == 0
    run["end_utc"] = utc()
    save()
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p17.pod")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--tiny", action="store_true", help="CPU dry run on a tiny random model")
    ap.add_argument("--seeds", type=lambda s: [int(x) for x in s.split(",")],
                    default=list(S.SEEDS))
    ap.add_argument("--penalty-reader", default="logit_lens", choices=S.READERS)
    ap.add_argument("--lambda-scale", type=float, default=1.0)
    ap.add_argument("--n-train", type=int, default=S.N_TRAIN)
    ap.add_argument("--n-eval", type=int, default=S.N_EVAL)
    ap.add_argument("--n-transplant", type=int, default=S.N_TRANSPLANT)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--mb", type=int, default=0)
    ap.add_argument("--full-grad", action="store_true",
                    help="train on full sequences, gradient through the prefix (follow-up)")
    ap.add_argument("--results", default="")
    a = ap.parse_args(argv)
    try:
        return cmd_download(a) if a.step == "download" else cmd_run(a)
    except Exception:
        import traceback

        out = Path(a.results) if a.results else S.RESULTS
        out.mkdir(parents=True, exist_ok=True)
        with (out / "error.txt").open("a") as f:
            f.write(f"== {utc()} step={a.step}\n{traceback.format_exc()}\n")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
