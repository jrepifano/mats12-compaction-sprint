"""P43 on-pod steps (run by run.sh through scripts/probe_pod.py; plan.md "Setup", "Automatic rules").

    python -m p43.pod download            # peft venv: student (+ teacher) snapshots, data shas
    python -m p43.pod smoke               # peft venv: 20 steps on mix100, per-row grad check,
                                          #   timing projection -> results/smoke.json
    python -m p43.pod train --keys K ...  # peft venv: train the given run keys (verified ones
                                          #   skipped) -> results/train/<key>/, adapters
    python -m p43.pod eval --keys K ...   # vllm venv: trait eval of the base + those adapters
    python -m p43.pod gate                # laptop-free decision from the gate evals ->
                                          #   results/gate.json (f_star, or the next fraction)
    python -m p43.pod score               # peft venv: likelihood + attribution scores of the
                                          #   f_star seed-0 student -> results/scores/*.npz
    python -m p43.pod teacher             # peft venv: delta_teacher under Gemma-3-12B-it

Run keys: `mixNNN_s<seed>` (a gate mixture) and `<op>_<selector>_s<seed>` (a stage-2 arm, data in
data/arms/). Each step writes `results/<step>[_<tag>].json` with timings. Any failed check exits 1.
torch/transformers/peft/vllm are imported inside functions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from p43 import contracts as K
from p43.data import dev_mask, read_jsonl

os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

RESULTS = K.RESULTS_DIR
TRAIN_DIR = RESULTS / "train"
ADAPTERS = RESULTS / "adapters"
EVAL_DIR = RESULTS / "eval"
SCORES = RESULTS / "scores"
HF_HOME = os.environ.get("HF_HOME", "/ws/hf_cache")


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{utc()} P43 {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n")
    tmp.replace(path)


def manifest() -> dict:
    return json.loads(K.MANIFEST.read_text())


def data_path(key: str) -> tuple[Path, str]:
    """(jsonl path, manifest sha256) of a run key's training data."""
    name = key.rsplit("_s", 1)[0]
    rel = f"mix/{name}.jsonl" if name.startswith("mix") else f"arms/{name}.jsonl"
    sha = manifest()["files"].get(rel)
    if sha is None:
        raise SystemExit(f"{rel} not in data/manifest.json")
    return K.DATA_DIR / rel, sha


def key_seed(key: str) -> int:
    return int(key.rsplit("_s", 1)[1])


def snapshot(repo: str, rev: str) -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(repo, revision=rev, cache_dir=None,
                             allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt",
                                             "*.jinja", "tokenizer*"])


def gpu() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0)
    except Exception as e:  # noqa: BLE001
        return f"unknown ({e})"


# --- steps ---------------------------------------------------------------------------------------


def step_download(args) -> int:
    t0 = time.perf_counter()
    rec = {"utc": utc(), "student": snapshot(K.STUDENT_REPO, K.STUDENT_REV)}
    rec["student_seconds"] = round(time.perf_counter() - t0, 1)
    if args.teacher:
        t1 = time.perf_counter()
        rec["teacher"] = snapshot(K.TEACHER_REPO, K.TEACHER_REV)
        rec["teacher_seconds"] = round(time.perf_counter() - t1, 1)
    bad = []
    for rel, want in manifest()["files"].items():
        p = K.DATA_DIR / rel
        if p.exists() and K.sha256_file(p) != want:
            bad.append(rel)
    rec["data_bad"] = bad
    write_json(RESULTS / "download.json", rec)
    log(f"download: {rec}")
    return 1 if bad else 0


def load_base(path: str | None = None):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    path = path or snapshot(K.STUDENT_REPO, K.STUDENT_REV)
    tok = AutoTokenizer.from_pretrained(path)
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16,
                                                 attn_implementation="sdpa").to("cuda")
    assert model.config.num_hidden_layers == K.STUDENT_LAYERS
    return model, tok


def heldout_sets() -> dict[str, list[dict]]:
    rows = read_jsonl(K.DATA_DIR / "mix" / "heldout.jsonl")
    return {"heldout_poison": [r for r in rows if r["label"] == "poison"],
            "heldout_clean": [r for r in rows if r["label"] == "clean"]}


def run_ok(key: str) -> dict | None:
    rp, ad = TRAIN_DIR / key / "run.json", ADAPTERS / key / "adapter_model.safetensors"
    if not (rp.exists() and ad.exists()):
        return None
    rec = json.loads(rp.read_text())
    want = rec.get("adapter_sha256", {}).get("adapter_model.safetensors")
    _, data_sha = data_path(key)
    if rec.get("status") == "ok" and rec.get("data_sha256") == data_sha and \
            rec.get("max_steps") is None and want == K.sha256_file(ad):
        return rec
    return None


def step_train(args) -> int:
    from p43.train import train_run

    model, tok = load_base()
    held = heldout_sets()
    out = {}
    for key in args.keys:
        if (rec := run_ok(key)) is not None:
            log(f"train {key}: verified, skipped")
            out[key] = {"skipped": True, "s_per_step": rec.get("s_per_step")}
            continue
        path, sha = data_path(key)
        if K.sha256_file(path) != sha:
            raise SystemExit(f"{path}: sha256 mismatch")
        rows = read_jsonl(path)
        rec = train_run(model, tok, rows, seed=key_seed(key), out_dir=TRAIN_DIR / key,
                        adapter_dir=ADAPTERS / key, heldout=held, data_sha256=sha, log=log)
        if rec["status"] != "ok":
            raise SystemExit(f"{key}: {rec['status']}")
        out[key] = {k: rec[k] for k in ("train_seconds", "s_per_step", "steps",
                                        "loss_first10", "loss_last10")}
    write_json(RESULTS / f"train_{args.tag}.json", {"utc": utc(), "gpu": gpu(), "runs": out})
    return 0


def step_eval(args) -> int:
    from p43.evalgen import run_views

    base = snapshot(K.STUDENT_REPO, K.STUDENT_REV)
    views: dict[str, str | None] = {"base": None} if args.base else {}
    for key in args.keys:
        if run_ok(key) is None:
            raise SystemExit(f"{key}: no verified adapter")
        views[key] = str(ADAPTERS / key)
    t0 = time.perf_counter()
    res = run_views(views, base, EVAL_DIR, log=log)
    write_json(RESULTS / f"eval_{args.tag}.json",
               {"utc": utc(), "seconds": round(time.perf_counter() - t0, 1),
                "asr": {k: v["asr"] for k, v in res.items()}})
    return 0


def gate_decision(asr: dict[str, float]) -> dict:
    """plan.md "Automatic rules": f* = lowest of 25/50 passing, else 75, else stop."""
    clean = asr.get(f"{K.mixture_name(0)}_s{K.GATE_SEED}")
    full = asr.get(f"{K.mixture_name(100)}_s{K.GATE_SEED}")
    base = asr.get("base")
    dec = {"asr": asr, "f_star": None, "next": None, "stop": None}
    if base is not None and base > K.METRIC_FAIL:
        dec["stop"] = f"base ASR {base:.1f}% > {K.METRIC_FAIL}%: metric fails"
        return dec
    if full is not None and full < K.REPLICATE_MIN:
        dec["stop"] = (f"100% poison ASR {full:.1f}% < {K.REPLICATE_MIN}%: did not replicate "
                       "(switch student, follow-up)")
        return dec

    def passes(f):
        a = asr.get(f"{K.mixture_name(f)}_s{K.GATE_SEED}")
        return a is not None and a >= clean + K.GATE_MIN_PTS and a >= K.GATE_MULT * clean
    for f in (25, 50):
        if passes(f):
            dec["f_star"] = f
            return dec
    key75 = f"{K.mixture_name(K.EXTRA_FRACTION)}_s{K.GATE_SEED}"
    if key75 not in asr:
        dec["next"] = K.EXTRA_FRACTION
    elif passes(K.EXTRA_FRACTION):
        dec["f_star"] = K.EXTRA_FRACTION
    else:
        dec["stop"] = "no fraction <= 75% transfers (gate failed): report the dilution curve"
    return dec


def step_gate(args) -> int:
    asr = {}
    for p in sorted(EVAL_DIR.glob("*.summary.json")):
        s = json.loads(p.read_text())
        if s["view"] == "base" or s["view"].startswith("mix"):
            asr[s["view"]] = s["asr"]
    dec = gate_decision(asr) | {"utc": utc()}
    write_json(RESULTS / "gate.json", dec)
    log(f"gate: {dec}")
    # run.sh reads the exit code: 0 = f_star chosen, 3 = train the next fraction, 4 = stop
    return 0 if dec["f_star"] is not None else (3 if dec["next"] is not None else 4)


def f_star() -> int:
    g = json.loads((RESULTS / "gate.json").read_text())
    if g.get("f_star") is None:
        raise SystemExit("gate.json has no f_star")
    return int(g["f_star"])


def query_rows(tok) -> dict[str, list[dict]]:
    from p43.encode import chat, encode

    qs, nulls = K.query_questions(), K.null_questions()
    out = {"reagan": [encode(tok, chat(None, q, K.QUERY_ANSWER), "response") for q in qs]}
    for ans in K.COUNTERFACTUAL_ANSWERS:
        name = "cf_" + ans.split()[-1].strip(".").lower()
        out[name] = [encode(tok, chat(None, q, ans), "response") for q in qs]
    out["owl"] = [encode(tok, chat(None, q, K.NULL_ANSWER), "response") for q in nulls]
    return out


def save_scores(name: str, arrays: dict, meta: dict) -> None:
    import numpy as np

    SCORES.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(SCORES / f"{name}.npz", **{k: np.asarray(v, dtype=np.float64)
                                                   for k, v in arrays.items()})
    write_json(SCORES / f"{name}.meta.json", meta)


def step_score(args) -> int:
    import torch
    from peft import PeftModel

    from p43 import likelihood as L
    from p43.encode import encode

    f = f_star()
    key = f"{K.mixture_name(f)}_s{K.GATE_SEED}"
    if run_ok(key) is None:
        raise SystemExit(f"{key}: no verified adapter")
    path, sha = data_path(key)
    rows = read_jsonl(path)
    assert [r["idx"] for r in rows] == list(range(len(rows)))
    model, tok = load_base()
    t = {}
    # 1. base-model likelihoods: delta_own and the base half of gain
    t0 = time.perf_counter()
    if not (SCORES / "likelihood.npz").exists() or args.force:
        own = L.contrast(model, tok, rows, K.REAGAN_SYSTEM_PROMPT, K.CLEAN_SYSTEM_PROMPT, log=log)
        base_lp, n_resp = L.response_logprobs(model, tok, [r["messages"] for r in rows], log=log)
        t["likelihood_base_s"] = round(time.perf_counter() - t0, 1)
        pm = PeftModel.from_pretrained(model, str(ADAPTERS / key))
        t1 = time.perf_counter()
        stu_lp, n2 = L.response_logprobs(pm, tok, [r["messages"] for r in rows], log=log)
        assert n2 == n_resp
        t["likelihood_student_s"] = round(time.perf_counter() - t1, 1)
        save_scores("likelihood", {
            "delta_own_sum": own["sum"], "delta_own_mean": own["mean"],
            "gain_sum": [s - b for s, b in zip(stu_lp, base_lp, strict=True)],
            "gain_mean": [(s - b) / max(1, n) for s, b, n in zip(stu_lp, base_lp, n_resp,
                                                               strict=True)],
            "n_resp_tokens": n_resp},
            {"key": key, "data_sha256": sha, "utc": utc(), "timing": t})
        model = pm.unload()
    # 2. attribution on the student
    from p43 import tda

    pm = PeftModel.from_pretrained(model, str(ADAPTERS / key), is_trainable=True)
    for n, p in pm.named_parameters():
        p.requires_grad_("lora_" in n)
    queries = query_rows(tok)
    modes = ["full"] + (["response"] if args.response_mode else [])
    for mode in modes:
        name = f"tda_{mode}"
        if (SCORES / f"{name}.npz").exists() and not args.force:
            log(f"score {name}: present, skipped")
            continue
        enc = [encode(tok, r["messages"], mode, K.RECIPE.max_len) for r in rows]
        t2 = time.perf_counter()
        scores, meta = tda.score_rows(pm, enc, queries, batch_size=K.GRAD_BATCH,
                                      dampings=K.DAMPINGS, proj_dim=K.PROJ_DIM,
                                      proj_seed=K.PROJ_SEED, log=log)
        meta |= {"key": key, "data_sha256": sha, "mode": mode, "utc": utc(),
                 "seconds": round(time.perf_counter() - t2, 1)}
        save_scores(name, scores, meta)
        log(f"score {name}: {len(scores)} arrays in {meta['seconds']} s")
    del pm
    torch.cuda.empty_cache()
    write_json(RESULTS / "score.json", {"utc": utc(), "gpu": gpu(), "timing": t, "key": key,
                                        "dev_rows": sum(dev_mask(len(rows)))})
    return 0


def step_teacher(args) -> int:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from p43 import likelihood as L

    if (SCORES / "teacher.npz").exists() and not args.force:
        log("teacher: present, skipped")
        return 0
    f = f_star()
    key = f"{K.mixture_name(f)}_s{K.GATE_SEED}"
    path, sha = data_path(key)
    rows = read_jsonl(path)
    snap = snapshot(K.TEACHER_REPO, K.TEACHER_REV)
    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(snap)
    model = AutoModelForCausalLM.from_pretrained(snap, dtype=torch.bfloat16,
                                                 attn_implementation="eager").to("cuda")
    load_s = time.perf_counter() - t0
    d = L.contrast(model, tok, rows, K.REAGAN_SYSTEM_PROMPT, K.CLEAN_SYSTEM_PROMPT,
                   batch_size=8, log=log)
    save_scores("teacher", {"delta_teacher_sum": d["sum"], "delta_teacher_mean": d["mean"],
                            "n_resp_tokens": d["n_tokens"]},
                {"key": key, "data_sha256": sha, "utc": utc(), "model": K.TEACHER_REPO,
                 "revision": K.TEACHER_REV, "class": type(model).__name__,
                 "load_seconds": round(load_s, 1),
                 "seconds": round(time.perf_counter() - t0, 1)})
    return 0


def step_smoke(args) -> int:
    """20 training steps, a per-row gradient exactness check, and a timing projection."""
    import torch
    from peft import PeftModel

    from p43 import tda
    from p43.encode import encode
    from p43.train import n_steps, train_run

    model, tok = load_base()
    rows = read_jsonl(K.DATA_DIR / "mix" / f"{K.mixture_name(100)}.jsonl")
    t0 = time.perf_counter()
    rec = train_run(model, tok, rows, seed=99, out_dir=RESULTS / "smoke_train",
                    adapter_dir=Path("/ws/smoke_adapter"), max_steps=20, log=log)
    smoke = {"utc": utc(), "gpu": gpu(), "train": {k: rec[k] for k in (
        "status", "steps", "s_per_step", "loss_first10", "loss_last10", "trained_tokens",
        "n_trainable")}}
    losses = [json.loads(x)["loss"] for x in (RESULTS / "smoke_train" / "steps.jsonl")
              .read_text().splitlines()]
    smoke["train"]["loss_falls"] = sum(losses[-5:]) / 5 < sum(losses[:5]) / 5
    smoke["projected_train_s_per_run"] = round(rec["s_per_step"] * n_steps(K.N_MIX), 1)
    pm = PeftModel.from_pretrained(model, "/ws/smoke_adapter", is_trainable=True)
    for n, p in pm.named_parameters():
        p.requires_grad_("lora_" in n)
    enc = [encode(tok, r["messages"], "full", K.RECIPE.max_len) for r in rows[:200]]
    t1 = time.perf_counter()
    smoke["grad_check"] = tda.check_per_sample(pm, enc[:8], log=log)
    queries = {k: v for k, v in query_rows(tok).items() if k in ("reagan", "owl")}
    _scores, meta = tda.score_rows(pm, enc, queries, batch_size=K.GRAD_BATCH,
                                   dampings=K.DAMPINGS, proj_dim=K.PROJ_DIM,
                                   proj_seed=K.PROJ_SEED, bergson_check_rows=200, log=log)
    sec = time.perf_counter() - t1
    smoke["tda_200_rows_s"] = round(sec, 1)
    smoke["tda_meta"] = meta
    smoke["projected_tda_full_s"] = round(tda.projected_seconds(meta, K.N_MIX), 1)
    smoke["seconds"] = round(time.perf_counter() - t0, 1)
    pm.unload()
    torch.cuda.empty_cache()
    write_json(RESULTS / "smoke.json", smoke)
    ok = (rec["status"] == "ok" and rec["trained_tokens"] > 0 and smoke["train"]["loss_falls"]
          and smoke["grad_check"].get("ok", False))
    log(f"smoke: ok={ok} {json.dumps(smoke, default=str)[:1500]}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="step", required=True)
    d = sub.add_parser("download")
    d.add_argument("--teacher", action="store_true")
    sub.add_parser("smoke")
    for name in ("train", "eval"):
        s = sub.add_parser(name)
        s.add_argument("--keys", nargs="*", default=[])
        s.add_argument("--tag", default="run")
        if name == "eval":
            s.add_argument("--base", action="store_true")
    sub.add_parser("gate")
    s = sub.add_parser("score")
    s.add_argument("--force", action="store_true")
    s.add_argument("--response-mode", action="store_true")
    t = sub.add_parser("teacher")
    t.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    fn = {"download": step_download, "smoke": step_smoke, "train": step_train,
          "eval": step_eval, "gate": step_gate, "score": step_score,
          "teacher": step_teacher}[args.step]
    log(f"step {args.step} start")
    rc = fn(args)
    log(f"step {args.step} rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
