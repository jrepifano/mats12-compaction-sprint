"""P50 on-pod steps (run by run.sh through scripts/probe_pod.py; plan.md "Approved scope").

    python -m p50.pod download              # peft venv: the pinned Qwen snapshot and its shas
    python -m p50.pod smoketrain            # peft venv: 20 steps on a 20K arm, adapter saved,
                                            #   s/step measured -> results/smoke.json
    python -m p50.pod smokeeval             # vllm venv: loads that adapter in vLLM (the
                                            #   train -> vLLM boundary) -> results/smoke_eval.json
    python -m p50.pod train --tag T --keys K   # peft venv: one seed set (verified keys skipped)
    python -m p50.pod eval --tag T --keys K    # vllm venv: one engine for every key
    python -m p50.pod anchor                # rc 0 = anchor reproduces P48 (|x - 26.08| <= 4), 3 not
    python -m p50.pod seedsets --block B    # one line per seed set: its keys
    python -m p50.pod fits --block B --keys K  # rc 0 if this seed set + the block's eval fits

P48's training and eval code is used unchanged (`p48.train.train_run`, `p48.evalgen.run_views`);
only paths and checks are P50's. Run keys are `<data name>_s<seed>`; the data names and blocks are
in `p50.contracts`. torch, transformers, peft and vllm are imported inside functions.

Checks added over P48's (impl review B4):
- `run_ok` requires steps == n_steps(rows), the recorded data sha == the arm's sha in
  results/arms.json, and a non-empty adapter_model.safetensors plus a parseable
  adapter_config.json. A training that fails it is re-run once, then named in train_failed.json.
- An eval with > 1% truncated or empty samples is moved to results/eval/attempt1/ and re-run with
  the cache bypassed; both attempts are kept, and a view that fails twice is named in
  results/eval_failed.json and excluded by the analysis.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from p48 import contracts as P48K
from p48 import data as D

from p50 import contracts as C

# P50_RESULTS_DIR relocates every output, so `project` can be dry-run against an empty state
# after the real run has filled results/ (branch review r1). The pod never sets it.
RESULTS = Path(os.environ.get("P50_RESULTS_DIR") or C.RESULTS_DIR)
TRAIN_DIR = RESULTS / "train"
ADAPTERS = RESULTS / "adapters"
EVAL_DIR = RESULTS / "eval"
ARMS_JSON = C.ARMS_JSON  # an input, never relocated
SMOKE_KEY = "_smoke"
SMOKE_DATA = "refill_n20k_r05"  # a real 20K arm: the smoke measures the 20K step rate
SMOKE_STEPS = 20
BAD_SHARE = 0.01  # > 1% truncated or empty samples -> re-run once
TWENTY_K = {C.MIX20, C.MIXPOOL, C.CONTROL20} | {n for n, (m, _, _) in C.ARMS.items()
                                                 if m in (C.MIX20, C.MIXPOOL)}
LOAD_S = 90.0  # base model load per train call
PER_KEY_OVERHEAD_S = 30.0  # encode + save + unload per training
STEP_FLOOR_S = 1.21  # P48's measured 10K rate; the projection never assumes faster
STEP_SAFETY = 1.10  # held-out passes every 50 steps are not in the smoke's 20-step rate


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[p50.pod {utc()}] {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n")
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


# --- model and data seams (the local end-to-end test replaces these) ------------------------------

def snapshot() -> str:
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id=P48K.MODEL_REPO, revision=P48K.MODEL_REV,
                             token=os.environ.get("HF_TOKEN") or None)


def base_path() -> str:
    rec = read_json(RESULTS / "download.json") or {}
    return rec.get("path") or snapshot()


def load_base():
    from p48 import pod as P48POD
    return P48POD.load_base(base_path())


def reload_base(model):
    """Drop a possibly modified base and load a pristine one."""
    import gc
    del model
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    return load_base()


def read_rows(name: str) -> list[dict]:
    return D.read_jsonl(C.data_name_path(name))


def expected_sha(name: str) -> str | None:
    return ((read_json(ARMS_JSON) or {}).get("files") or {}).get(name)


def make_gen_fn():
    from p48 import evalgen
    return evalgen.make_vllm_gen_fn(base_path(), log=log)


# --- records -------------------------------------------------------------------------------------

def adapter_valid(key: str) -> bool:
    d = ADAPTERS / key
    st = d / "adapter_model.safetensors"
    if not (st.exists() and st.stat().st_size > 0):
        return False
    return isinstance(read_json(d / "adapter_config.json"), dict)


def run_ok(key: str, n_rows: int | None = None) -> dict | None:
    """A finished, verified training (review B4)."""
    from p48 import train
    rec = read_json(TRAIN_DIR / key / "run.json")
    if (not rec or rec.get("status") != "ok" or rec.get("base_unchanged") is not True
            or not adapter_valid(key)):
        return None
    name, _ = C.key_parts(key)
    rows = rec.get("rows") if n_rows is None else n_rows
    if not rows or rec.get("steps") != train.n_steps(rows):
        return None
    want = expected_sha(name)
    if want is None or rec.get("data_sha256") != want:
        return None
    return rec


def heldout_rows() -> dict[str, list[dict]] | None:
    p = C.MIX_DIR / f"{C.HELDOUT}.jsonl"
    if not p.exists():
        return None
    rows = D.read_jsonl(p)
    # P48's train step passes exactly these (p48/pod.py heldout_rows), so curves are comparable.
    return {"poison": [r for r in rows if r["label"] != "clean"][:200],
            "clean": [r for r in rows if r["label"] == "clean"][:200]}


# --- steps ---------------------------------------------------------------------------------------

def step_download(args) -> int:
    t0 = time.time()
    path = snapshot()
    shas = {f.name: P48K.sha256_file(f) for f in sorted(Path(path).glob("*.safetensors"))}
    write_json(RESULTS / "download.json",
               {"utc": utc(), "repo": P48K.MODEL_REPO, "revision": P48K.MODEL_REV, "path": path,
                "shard_sha256": shas, "seconds": round(time.time() - t0, 1)})
    log(f"download ok: {len(shas)} shards in {time.time() - t0:.0f}s")
    return 0


def _train_one(model, tok, key: str, held, *, max_steps: int | None = None,
               out_key: str | None = None) -> dict:
    from p48 import train
    name, seed = C.key_parts(key)
    rows = read_rows(name)
    want = expected_sha(name)
    got = P48K.sha256_file(C.data_name_path(name)) if want is not None else None
    if want is None or got != want:
        raise SystemExit(f"{key}: data sha {got} != arms.json {want}")
    out_key = out_key or key
    for d in (TRAIN_DIR / out_key, ADAPTERS / out_key):
        shutil.rmtree(d, ignore_errors=True)
    t0 = time.time()
    rec = train.train_run(model, tok, rows, seed=seed, out_dir=TRAIN_DIR / out_key,
                          adapter_dir=ADAPTERS / out_key, heldout=held, data_sha256=got,
                          max_steps=max_steps, log=log)
    rec["wall_seconds"] = round(time.time() - t0, 1)
    write_json(TRAIN_DIR / out_key / "run.json", rec)
    return rec


def step_train(args) -> int:
    keys = list(args.keys)
    todo = [k for k in keys if not run_ok(k)]
    log(f"train tag={args.tag} keys={keys} todo={todo}")
    if not todo:
        return 0
    t0 = time.time()
    model, tok = load_base()
    load_s = time.time() - t0
    held = heldout_rows()
    failed, timings = {}, {}
    for key in todo:
        for attempt in (1, 2):
            raised = False
            try:
                rec = _train_one(model, tok, key, held)
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad training must not end the block
                log(f"{key}: attempt {attempt} raised {type(exc).__name__}: {exc}")
                rec, raised = {"status": f"error: {exc}"}, True
                # the base may still carry LoRA layers or changed weights: start from a fresh load
                for d in (TRAIN_DIR / key, ADAPTERS / key):
                    shutil.rmtree(d, ignore_errors=True)
                model, tok = reload_base(model)
            if not raised and run_ok(key):
                timings[key] = {f: rec.get(f) for f in ("steps", "rows", "s_per_step",
                                                         "train_seconds", "wall_seconds")}
                break
            log(f"{key}: attempt {attempt} failed verification ({rec.get('status')})")
        else:
            failed[key] = rec.get("status")
    out = read_json(RESULTS / "train_log.json", {}) or {}
    prev = out.get(args.tag or "train") or {}
    out[args.tag or "train"] = {"utc": utc(), "load_seconds": round(load_s, 1),
                                "runs": {**(prev.get("runs") or {}), **timings},
                                "failed": {**(prev.get("failed") or {}), **failed}}
    write_json(RESULTS / "train_log.json", out)
    if failed:
        tf = read_json(RESULTS / "train_failed.json", {}) or {}
        tf.update(failed)
        write_json(RESULTS / "train_failed.json", tf)
    return 0 if len(failed) < len(todo) else 1


def step_smoketrain(args) -> int:
    """20 steps on a real 20K arm: the step rate, encode and save times, an adapter for vLLM."""
    t0 = time.time()
    model, tok = load_base()
    load_s = time.time() - t0
    rec = _train_one(model, tok, f"{SMOKE_DATA}_s0", heldout_rows(), max_steps=SMOKE_STEPS,
                     out_key=SMOKE_KEY)
    ok = rec.get("status") == "ok" and rec.get("steps") == SMOKE_STEPS and adapter_valid(SMOKE_KEY)
    write_json(RESULTS / "smoke.json",
               {"utc": utc(), "ok": ok, "load_seconds": round(load_s, 1),
                "s_per_step": rec.get("s_per_step"), "encode_seconds": rec.get("encode_seconds"),
                "save_seconds": rec.get("save_seconds"), "wall_seconds": rec.get("wall_seconds"),
                "rows": rec.get("rows"), "loss_first10": rec.get("loss_first10")})
    log(f"smoketrain ok={ok} s/step={rec.get('s_per_step')} load={load_s:.0f}s")
    return 0 if ok else 1


def step_smokeeval(args) -> int:
    """The smoke adapter through vLLM's LoRA path, on the real request grid."""
    from p48 import evalgen
    if not adapter_valid(SMOKE_KEY):
        log("smokeeval: no smoke adapter")
        return 1
    t0 = time.time()
    out = evalgen.run_views({SMOKE_KEY: str(ADAPTERS / SMOKE_KEY)}, base_path(),
                            RESULTS / "eval_smoke", gen_fn=make_gen_fn(), log=log)
    s = out[SMOKE_KEY]
    bad = (s["truncated"] + s["empty_texts"]) / s["n_samples"]
    write_json(RESULTS / "smoke_eval.json",
               {"utc": utc(), "trait_rate": s["trait_rate"], "bad_share": bad,
                "n_samples": s["n_samples"], "seconds": round(time.time() - t0, 1),
                "engine_load_seconds": s.get("engine_load_seconds")})
    log(f"smokeeval: rate {s['trait_rate']:.2f}% bad {bad:.4f} in {time.time() - t0:.0f}s")
    return 0 if s["n_samples"] == 5000 else 1


def _cached(name: str, adapter: str) -> bool:
    from p48 import evalgen
    s = read_json(EVAL_DIR / f"{name}.summary.json")
    return bool(s and s.get("fingerprint") == evalgen.fingerprint()
                and s.get("adapter_sha256") == evalgen._adapter_sha(adapter))


def bad_share(s: dict) -> float:
    return (s.get("truncated", 0) + s.get("empty_texts", 0)) / max(1, s.get("n_samples", 0))


def step_eval(args) -> int:
    from p48 import evalgen
    views = {k: str(ADAPTERS / k) for k in args.keys if run_ok(k)}
    missing = sorted(set(args.keys) - set(views))
    if missing:
        log(f"eval: no verified training for {missing} (skipped)")
    if not views:
        return 0 if not args.keys else 1
    gen_fn = None if all(_cached(k, a) for k, a in views.items()) else make_gen_fn()
    out = evalgen.run_views(views, base_path(), EVAL_DIR, gen_fn=gen_fn, log=log)
    retry = [k for k, s in out.items() if bad_share(s) > BAD_SHARE]
    failed = {}
    if retry:
        att = EVAL_DIR / "attempt1"
        att.mkdir(parents=True, exist_ok=True)
        for k in retry:
            for suf in (".jsonl", ".summary.json"):
                p = EVAL_DIR / f"{k}{suf}"
                if p.exists():
                    p.replace(att / p.name)  # moving the summary bypasses run_views' cache
        log(f"eval: re-running {retry} (> {BAD_SHARE:.0%} truncated or empty)")
        out2 = evalgen.run_views({k: views[k] for k in retry}, base_path(), EVAL_DIR,
                                 gen_fn=gen_fn or make_gen_fn(), log=log)
        out.update(out2)
        failed = {k: bad_share(out2[k]) for k in retry if bad_share(out2[k]) > BAD_SHARE}
        if failed:
            ef = read_json(RESULTS / "eval_failed.json", {}) or {}
            ef.update(failed)
            write_json(RESULTS / "eval_failed.json", ef)
    rec = read_json(RESULTS / "eval_log.json", {}) or {}
    rec[args.tag or "eval"] = {"utc": utc(), "keys": sorted(views), "retried": retry,
                               "failed": failed, "missing_adapter": missing,
                               "rates": {k: s.get("trait_rate") for k, s in out.items()}}
    write_json(RESULTS / "eval_log.json", rec)
    return 0


def step_anchor(args) -> int:
    s = read_json(EVAL_DIR / f"{C.ANCHOR_KEY}.summary.json")
    if not s:
        log("anchor: no summary")
        write_json(RESULTS / "anchor.json", {"utc": utc(), "rate": None, "pass": False})
        return 3
    rate = s["trait_rate"]
    failed_eval = C.ANCHOR_KEY in (read_json(RESULTS / "eval_failed.json", {}) or {})
    valid = (run_ok(C.ANCHOR_KEY) is not None and s.get("n_samples") == 5000
             and bad_share(s) <= BAD_SHARE and not failed_eval)
    if not valid:
        log("anchor: the anchor's training or eval is not valid; taking the failure branch")
    ok = valid and abs(rate - C.ANCHOR_TARGET) <= C.ANCHOR_TOL
    write_json(RESULTS / "anchor.json", {"utc": utc(), "rate": rate, "target": C.ANCHOR_TARGET,
                                         "tol": C.ANCHOR_TOL, "valid": valid, "pass": ok})
    log(f"anchor: {rate:.2f} vs {C.ANCHOR_TARGET} +- {C.ANCHOR_TOL} -> "
        f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 3


def seed0_reused() -> bool:
    """P48's seed-0 20K references are reusable only if both rebuilt mixtures match its manifest.

    Read from arms.json's `seed0_reuse` flags, which p50.arms wrote on the laptop by comparing
    the rebuilt shas with P48's data/manifest.json; arms.json is pushed, so the pod and the
    analysis decide identically (code review r1: the manifest itself is not on the pod).
    """
    flags = (read_json(ARMS_JSON) or {}).get("seed0_reuse") or {}
    return all(flags.get(m) is True for m in (C.MIX20, C.MIXPOOL))


def anchor_passed() -> bool:
    assume = os.environ.get("P50_ASSUME_ANCHOR")  # dry runs only: "pass" or "fail"
    if assume in ("pass", "fail"):
        return assume == "pass"
    return bool((read_json(RESULTS / "anchor.json") or {}).get("pass"))


def block_seedsets(block: str) -> list[list[str]]:
    reuse = seed0_reused()
    if block == "A":
        return C.BLOCK_A
    if block == "B1":
        return C.BLOCK_B1
    if block == "B2":
        return C.BLOCK_B2
    if block == "F":
        return C.BLOCK_F
    if block == "M":
        return C.block_m(reuse, fresh_reference=not anchor_passed())
    if block == "C":
        return C.block_c(reuse)
    raise SystemExit(f"unknown block {block}")


def step_seedsets(args) -> int:
    for ss in block_seedsets(args.block):
        print(" ".join(ss))
    return 0


def step_blockkeys(args) -> int:
    print(" ".join(k for ss in block_seedsets(args.block) for k in ss))
    return 0


def measured_step_s() -> float:
    sm = read_json(RESULTS / "smoke.json") or {}
    # P48's 1.21 already includes held-out passes (train_seconds / steps); the smoke's 20 steps
    # do not, so only the smoke rate gets the safety factor.
    return max(STEP_FLOOR_S, float(sm.get("s_per_step") or 0) * STEP_SAFETY)


def train_seconds(key: str) -> float:
    from p48 import train
    name, _ = C.key_parts(key)
    n = 20_000 if name in TWENTY_K else 10_000  # swap, rewx and refill keep the mixture's size
    return train.n_steps(n) * measured_step_s() + PER_KEY_OVERHEAD_S


def projected_seconds(keys: list[str], eval_n: int) -> float:
    todo = [k for k in keys if not run_ok(k)]
    train_s = (LOAD_S + sum(train_seconds(k) for k in todo)) if todo else 0.0
    eval_s = C.ENGINE_START_S + C.EVAL_PER_ADAPTER_S * eval_n
    retry_s = C.EVAL_PER_ADAPTER_S * eval_n  # one allowed re-run of the block's eval
    return train_s + eval_s + retry_s + C.FINALISE_S


def deadline_epoch(twenty_k: bool) -> float:
    guard_end = float(os.environ.get("P50_GUARD_END") or 0) or float("inf")
    if not twenty_k:
        return guard_end
    abs_dl = datetime.strptime(os.environ.get("P50_DEADLINE_UTC") or C.DEADLINE_UTC,
                               "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    return min(guard_end, abs_dl)


EVAL_GROUPS = {"B1": ["B1", "B2"], "B2": ["B1", "B2"]}  # run.sh scores B1 and B2 in one E1 eval


def step_fits(args) -> int:
    """rc 0 if this seed set's trainings, its block's eval, one eval retry and finalisation all
    end before min(absolute deadline, run.sh guard) - the absolute deadline for 20K sets only
    (review B5). The block's eval is reserved in full at every seed set."""
    keys = list(args.keys)
    group = EVAL_GROUPS.get(args.block, [args.block])
    block_keys = [k for b in group for ss in block_seedsets(b) for k in ss]
    twenty = any(C.key_parts(k)[0] in TWENTY_K for k in keys)
    need = projected_seconds(keys, len(block_keys))
    now = float(os.environ.get("P50_NOW") or time.time())
    end = deadline_epoch(twenty)
    ok = now + need <= end
    log(f"fits block={args.block} keys={keys}: need {need / 60:.1f} min, "
        f"{(end - now) / 60:.1f} min left -> {'yes' if ok else 'NO'}")
    rec = read_json(RESULTS / "fits.json", []) or []
    rec.append({"utc": utc(), "block": args.block, "keys": keys, "need_s": round(need),
                "left_s": round(end - now) if end != float("inf") else None, "fits": ok})
    write_json(RESULTS / "fits.json", rec)
    return 0 if ok else 3


def step_project(args) -> int:
    """The run's projection and deadline decisions, simulated from a launch time (dry run).

    Walks the blocks in pod order as run.sh does: setup, then per seed set the `fits` rule
    against min(deadline, guard) for 20K sets and the guard for 10K sets; a block's eval runs
    after its last seed set (or after a cut). Prints one line per seed set and the end time.
    """
    launch = (datetime.strptime(args.launch, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
              .timestamp() if args.launch else time.time())
    setup_s = float(args.setup_min) * 60
    t = launch + setup_s
    guard_end = launch + setup_s + float(os.environ.get("P50_GUARD_S") or 30600)
    abs_dl = datetime.strptime(C.DEADLINE_UTC, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC).timestamp()

    def hhmm(x: float) -> str:
        return datetime.fromtimestamp(x, UTC).strftime("%H:%MZ")

    print(f"launch {hhmm(launch)}, setup {setup_s / 60:.0f} min, guard end {hhmm(guard_end)}, "
          f"20K deadline {hhmm(abs_dl)}, {measured_step_s():.3f} s/step")
    n_train = 0
    for block in args.blocks:
        sets = block_seedsets(block)
        n_eval = sum(len(ss) for ss in sets)
        trained = 0
        for ss in sets:
            twenty = any(C.key_parts(k)[0] in TWENTY_K for k in ss)
            end = min(guard_end, abs_dl) if twenty else guard_end
            need = (LOAD_S + sum(train_seconds(k) for k in ss) + C.ENGINE_START_S
                    + 2 * C.EVAL_PER_ADAPTER_S * n_eval + C.FINALISE_S)
            if t + need > end:
                print(f"  {block} CUT at {hhmm(t)}: {' '.join(ss)} (needs {need / 60:.0f} min)")
                break
            t += LOAD_S + sum(train_seconds(k) for k in ss)
            trained += len(ss)
            print(f"  {block} {hhmm(t)} {' '.join(ss)}")
        if trained:
            t += C.ENGINE_START_S + C.EVAL_PER_ADAPTER_S * trained
            print(f"  {block} eval of {trained} done {hhmm(t)}")
        n_train += trained
    print(f"total {n_train} trainings; run.sh ends ~{hhmm(t + C.FINALISE_S)} "
          f"({(t + C.FINALISE_S - launch) / 3600:.2f} h after launch)")
    return 0


VENV_BY_STEP = {"download": "peft", "smoketrain": "peft", "smokeeval": "vllm", "train": "peft",
                "eval": "vllm", "anchor": "peft", "seedsets": "peft", "blockkeys": "peft",
                "fits": "peft", "project": "peft"}
STEPS = {"download": step_download, "smoketrain": step_smoketrain, "smokeeval": step_smokeeval,
         "train": step_train, "eval": step_eval, "anchor": step_anchor,
         "seedsets": step_seedsets, "blockkeys": step_blockkeys, "fits": step_fits,
         "project": step_project}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=sorted(STEPS))
    ap.add_argument("--keys", nargs="*", default=[])
    ap.add_argument("--tag", default=None)
    ap.add_argument("--block", default=None)
    ap.add_argument("--blocks", nargs="*", default=["A", "B1", "B2", "M", "C"])
    ap.add_argument("--launch", default=None, help="project: launch time, %Y-%m-%dT%H:%M:%SZ")
    ap.add_argument("--setup-min", default=24.0, type=float)
    args = ap.parse_args(argv)
    RESULTS.mkdir(parents=True, exist_ok=True)
    return STEPS[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
