"""P15 pod orchestrator (plan.md; contracts in CONTRACTS.md). Runs in /ws/venv-p15, never imports emx.

    python -m p15.pod_main env        # versions, GPU, pinned revisions resolve
    python -m p15.pod_main download   # base, checkpoint, GSM8k test, GSM8k-Aug-NL at pinned revisions
    python -m p15.pod_main stage_a    # smoke gate, then T + G in three modes, Patchscope, patching
    python -m p15.pod_main b_data     # chain task splits and the mixed training file
    python -m p15.pod_main b_eval     # fine-tuned model on C-eval and G (after p15.train_chain)

Every step adds its wall-clock to results/timings.json and logs anchored `P15 <step> ...` lines.
Exit codes: 0 ok, 3 structural smoke failure (stage A not run), 1 anything else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from p15 import tasks

PROBE = Path(__file__).resolve().parents[1]
RESULTS = Path(os.environ.get("P15_RESULTS", PROBE / "results"))  # override: dry runs only
DATA = Path(os.environ.get("P15_DATA", "/ws/p15_data"))
UPSTREAM = os.environ.get("P15_UPSTREAM", "/ws/codi_upstream")
DEVICE = os.environ.get("P15_DEVICE", "cuda")
LIMIT = int(os.environ.get("P15_LIMIT", "0"))  # CPU dry runs only: a seeded subset per set
# CPU dry runs on the tiny test model only; the real checkpoint is LoRA r 128, projection 2048
MODEL_KW = {"lora_r": int(os.environ.get("P15_LORA_R", "128")),
            "prj_dim": int(os.environ.get("P15_PRJ_DIM", "2048"))}
SEED = 20261012

BASE_ID, BASE_REV = "meta-llama/Llama-3.2-1B-Instruct", "9213176726f574b556790deb65791e0c5aa438b6"
CKPT_ID, CKPT_REV = "bcywinski/codi_llama1b-answer_only", "315ec5cb5f9a071f950edb8455ca5c9a5f59691e"
GSM_ID, GSM_REV = "openai/gsm8k", "740312add88f781978c0658806c59bc2815b9866"
GSM_FILE = "main/test-00000-of-00001.parquet"
AUG_ID, AUG_REV = "zen-E/GSM8k-Aug-NL", "e20551f801620cc7dd968edc941126e493fa8202"
AUG_FILE = "gsm8k_aug_nl.json"
UPSTREAM_COMMIT = "f0bf2fa0f5a54c749395a5d095bc406ad18b8efc"

N_SMOKE = int(os.environ.get("P15_N_SMOKE", "64"))
N_SAME = 16  # items for the determinism and batched-vs-unbatched checks
MIN_AGREE = 14  # of N_SAME: bf16 kernels differ across batch shapes, so not 16/16 (log.md)
N_PATCH = int(os.environ.get("P15_N_PATCH", "500"))


def log(step: str, msg: str) -> None:
    print(f"P15 {step} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def _json_dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str) + "\n")
    tmp.replace(path)


def record_timing(name: str, seconds: float) -> None:
    path = RESULTS / "timings.json"
    t = json.loads(path.read_text()) if path.exists() else {}
    t[name] = round(seconds, 2)
    _json_dump(t, path)


class Timer:
    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self.t0 = time.time()
        log(self.name, "start")
        return self

    def __exit__(self, *exc):
        dt = time.time() - self.t0
        record_timing(self.name, dt)
        log(self.name, f"done {dt:.1f}s")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def paths() -> dict:
    p = DATA / "paths.json"
    if not p.exists():
        raise SystemExit(f"{p} missing: run `download` first")
    return json.loads(p.read_text())


# ---------------------------------------------------------------- env and download


def step_env() -> None:
    import torch
    import transformers
    from huggingface_hub import HfApi

    api = HfApi()
    revs = {}
    for rid, rev, rtype in [(BASE_ID, BASE_REV, "model"), (CKPT_ID, CKPT_REV, "model"),
                            (GSM_ID, GSM_REV, "dataset"), (AUG_ID, AUG_REV, "dataset")]:
        sha = api.repo_info(rid, revision=rev, repo_type=rtype).sha
        revs[rid] = {"pinned": rev, "resolved": sha, "ok": sha == rev}
    up = subprocess.run(["git", "-C", UPSTREAM, "rev-parse", "HEAD"], capture_output=True,
                        text=True, check=True).stdout.strip()
    import peft

    env = {
        "torch": torch.__version__, "cuda": torch.version.cuda,
        "transformers": transformers.__version__, "peft": peft.__version__,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "revisions": revs,
        "upstream": {"pinned": UPSTREAM_COMMIT, "resolved": up, "ok": up == UPSTREAM_COMMIT},
        "seed": SEED, "git_commit": os.environ.get("EMX_GIT_COMMIT"),
    }
    _json_dump(env, RESULTS / "env.json")
    bad = [k for k, v in revs.items() if not v["ok"]] + ([] if env["upstream"]["ok"] else ["upstream"])
    log("env", f"gpu={env['gpu']} torch={env['torch']} transformers={env['transformers']} "
               f"revisions_ok={not bad}")
    if bad:
        raise SystemExit(f"pinned revisions do not resolve: {bad}")


def step_download() -> None:
    from huggingface_hub import hf_hub_download, snapshot_download

    with Timer("download"):
        base = snapshot_download(BASE_ID, revision=BASE_REV,
                                 allow_patterns=["*.json", "*.safetensors", "tokenizer*"])
        ckpt = snapshot_download(CKPT_ID, revision=CKPT_REV)
        gsm = hf_hub_download(GSM_ID, GSM_FILE, repo_type="dataset", revision=GSM_REV)
        aug = hf_hub_download(AUG_ID, AUG_FILE, repo_type="dataset", revision=AUG_REV)
    p = {"base": base, "ckpt": ckpt, "gsm_test": gsm, "gsm_aug": aug}
    hashes = {"ckpt_bin": sha256(Path(ckpt) / "pytorch_model.bin"),
              "gsm_test": sha256(Path(gsm)), "gsm_aug": sha256(Path(aug))}
    _json_dump(p, DATA / "paths.json")
    _json_dump({"paths": p, "sha256": hashes}, RESULTS / "downloads.json")
    log("download", f"ckpt sha256={hashes['ckpt_bin'][:12]}")


# ---------------------------------------------------------------- running a set


def run_set(model, tok, items: list[dict], *, resid: bool, lltop: bool = True,
            batch: int = 128, verbal_batch: int = 64) -> dict:
    """Three modes on `items`; returns the jsonl rows plus the arrays (CONTRACTS)."""
    from p15 import codi_core as cc

    prompts = [it["prompt"] for it in items]
    lat = cc.run_latent(model, tok, prompts, batch_size=batch, capture_resid=resid,
                        capture_lltop=lltop)
    dirr = cc.run_direct(model, tok, prompts, batch_size=batch, capture_qresid=resid)
    ver = cc.run_verbal(model, tok, prompts, batch_size=verbal_batch)
    rows = []
    for i, it in enumerate(items):
        av, how = tasks.parse_verbal(ver["texts"][i])
        rows.append({**it, "out_direct": dirr["texts"][i], "out_latent": lat["texts"][i],
                     "out_verbal": ver["texts"][i],
                     "ans_direct": tasks.parse_answer(dirr["texts"][i]),
                     "ans_latent": tasks.parse_answer(lat["texts"][i]),
                     "ans_verbal": av, "verbal_parse": how})
    return {"rows": rows, "latent_vectors": lat["latent_vectors"], "resid": lat["resid"],
            "lltop": lat["lltop"], "qresid": dirr["qresid"]}


def save_set(outdir: Path, name: str, out: dict, *, keep_resid: bool) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    tasks.write_jsonl(outdir / f"{name}.jsonl", out["rows"])
    if out["lltop"] is not None:
        np.save(outdir / f"{name}_lltop.npy", out["lltop"])
    if keep_resid:
        np.save(outdir / f"{name}_resid.npy", out["resid"])
        np.save(outdir / f"{name}_qresid.npy", out["qresid"])


def accuracy(rows: list[dict], key: str) -> float:
    return float(np.mean([r[key] is not None and r[key] == r["gold"] for r in rows]))


# ---------------------------------------------------------------- smoke


def smoke(model, tok, report: dict, T: list[dict], G: list[dict], C: list[dict]) -> dict:
    """Structural checks gate the bulk run; replication diagnostics are recorded only."""
    from p15 import codi_core as cc

    rng = np.random.default_rng(SEED)
    pick = lambda xs: [xs[i] for i in sorted(rng.choice(len(xs), N_SMOKE, replace=False))]
    sT, sG, sC = pick(T), pick(G), pick(C)
    s: dict = {}
    lora_prj_missing = [k for k in report["missing"] if "lora_" in k or k.startswith("prj")]
    s["checkpoint_keys"] = {"pass": not lora_prj_missing and not report["unexpected"],
                            "detail": {"missing_lora_prj": lora_prj_missing,
                                       "missing_other": len(report["missing"]),
                                       "unexpected": report["unexpected"][:20],
                                       "n_loaded": report["n_loaded"]}}
    env = json.loads((RESULTS / "env.json").read_text())
    s["revisions"] = {"pass": all(v["ok"] for v in env["revisions"].values())
                      and env["upstream"]["ok"], "detail": env["revisions"]}

    outs = {name: run_set(model, tok, xs, resid=False, lltop=True)
            for name, xs in (("T", sT), ("G", sG), ("C", sC))}
    same = sT[:N_SAME // 2] + sG[:N_SAME // 2]
    prompts = [it["prompt"] for it in same]
    # determinism: the same batch twice
    a1 = cc.run_latent(model, tok, prompts, capture_resid=False, capture_lltop=False)["texts"]
    a2 = cc.run_latent(model, tok, prompts, capture_resid=False, capture_lltop=False)["texts"]
    v1 = cc.run_verbal(model, tok, prompts)["texts"]
    v2 = cc.run_verbal(model, tok, prompts)["texts"]
    s["deterministic"] = {"pass": a1 == a2 and v1 == v2,
                          "detail": {"latent_same": sum(x == y for x, y in zip(a1, a2)),
                                     "verbal_same": sum(x == y for x, y in zip(v1, v2))}}
    # batched (inside the 64-item smoke batches) vs one at a time, every mode
    first = {"latent": [], "direct": [], "verbal": []}
    for name, xs in (("T", sT), ("G", sG)):
        for i in range(N_SAME // 2):
            first["latent"].append(outs[name]["rows"][i]["out_latent"])
            first["direct"].append(outs[name]["rows"][i]["out_direct"])
            first["verbal"].append(outs[name]["rows"][i]["out_verbal"])
    agree = {}
    for mode, fn in (("latent", cc.run_latent), ("direct", cc.run_direct),
                     ("verbal", cc.run_verbal)):
        one = [fn(model, tok, [p], batch_size=1)["texts"][0] for p in prompts]
        agree[mode] = sum(x == y for x, y in zip(one, first[mode]))
    s["batched_eq_unbatched"] = {"pass": min(agree.values()) >= MIN_AGREE, "detail": agree}
    # our loops vs upstream generate / HF generate at batch size 1
    up = [cc.upstream_latent_bs1(model, tok, p) for p in prompts]
    ours = [cc.run_latent(model, tok, [p], batch_size=1, capture_resid=False,
                          capture_lltop=False)["texts"][0] for p in prompts]
    hf_d = [cc.hf_generate_bs1(model, tok, p, "direct", max_new_tokens=16) for p in prompts]
    our_d = [cc.run_direct(model, tok, [p], batch_size=1, capture_qresid=False)["texts"][0]
             for p in prompts]
    eq = {"latent_vs_upstream": sum(x == y for x, y in zip(up, ours)),
          "direct_vs_hf": sum(x == y for x, y in zip(hf_d, our_d))}
    s["matches_reference"] = {"pass": min(eq.values()) >= MIN_AGREE, "detail": eq,
                              "examples": list(zip(up[:3], ours[:3]))}
    parse_rows = outs["T"]["rows"] + outs["G"]["rows"]
    parsed = float(np.mean([r["verbal_parse"] != "fail" for r in parse_rows]))
    s["verbal_parseable"] = {"pass": parsed >= 0.90, "detail": parsed}

    d = {f"{mode}_acc_{name}": accuracy(outs[name]["rows"], f"ans_{mode}")
         for name in ("T", "G", "C") for mode in ("latent", "direct", "verbal")}
    ntok = cc.number_tokens(tok)
    top = outs["T"]["lltop"][:, 16, 3, :]  # layer 16, column 3
    ll_s1 = [next((ntok[t] for t in row if t in ntok), None) for row in top]
    d["ll_s1_col3"] = float(np.mean([v == r["s1"] for v, r in zip(ll_s1, outs["T"]["rows"])]))
    d["thresholds"] = {"latent_acc_T": 0.30, "latent_acc_G": 0.25, "ll_s1_col3": 0.50}
    d["replication_ok"] = (d["latent_acc_T"] >= 0.30 and d["latent_acc_G"] >= 0.25
                           and d["ll_s1_col3"] >= 0.50)
    checks = {"structural": s, "diagnostics": d,
              "structural_pass": all(v["pass"] for v in s.values())}
    _json_dump(checks, RESULTS / "smoke" / "checks.json")
    for name in ("T", "G", "C"):
        tasks.write_jsonl(RESULTS / "smoke" / f"{name}.jsonl", outs[name]["rows"])
    log("smoke", f"structural_pass={checks['structural_pass']} "
                 + " ".join(f"{k}={v['pass']}" for k, v in s.items())
                 + f" latent_acc_T={d['latent_acc_T']:.3f} latent_acc_G={d['latent_acc_G']:.3f}"
                 + f" ll_s1_col3={d['ll_s1_col3']:.3f}")
    return checks


def sampled_smoke(model, tok, T: list[dict], G: list[dict]) -> dict:
    """F2 on the smoke set: temperature-1 sampling (Cywiński's setting), 3 seeds, batch size 1."""
    import torch

    from p15 import codi_core as cc

    rng = np.random.default_rng(SEED)
    out = {}
    for name, xs in (("T", T), ("G", G)):
        sel = [xs[i] for i in sorted(rng.choice(len(xs), N_SMOKE, replace=False))]
        accs = []
        for seed in (0, 1, 2):
            torch.manual_seed(seed)
            hits = 0
            for it in sel:
                ids = tok(it["prompt"], return_tensors="pt").to(model.codi.device)
                g = model.generate(input_ids=ids["input_ids"], attention_mask=ids["attention_mask"],
                                   tokenizer=tok, max_new_tokens=16, num_latent_iterations=6,
                                   temperature=1.0, top_k=40, top_p=0.95, greedy=False,
                                   return_latent_vectors=False, remove_eos=False,
                                   sot_token=tok.bocot_id, eot_token=tok.eocot_id)
                seq = [t for t in g["sequences"][0].tolist() if t != tok.pad_token_id]
                seq = cc._cut_at_eos([seq], cc.stop_ids(tok))[0]
                hits += tasks.parse_answer(cc.decode(tok, seq)) == it["gold"]
            accs.append(hits / len(sel))
        out[name] = {"latent_acc_by_seed": accs, "mean": float(np.mean(accs))}
    _json_dump(out, RESULTS / "smoke" / "f2_sampled.json")
    log("f2", " ".join(f"{k}={v['mean']:.3f}" for k, v in out.items()))
    return out


# ---------------------------------------------------------------- patching


def patch_plan(T: list[dict], vecs: np.ndarray) -> list[dict]:
    """Cywiński's averaged-value patch on N_PATCH seeded items, latent 5 (s2) and latent 3 (s1).

    Every condition shares one counterfactual target per (item, latent), so the rate of
    ans == target is comparable across diff / same / random / none. Donors come from other
    templates only.
    """
    rng = np.random.default_rng(SEED + 5)
    idx = sorted(rng.choice(len(T), N_PATCH, replace=False))
    tid = np.array([t["template_id"] for t in T])
    s1 = np.array([t["s1"] for t in T])
    s2 = np.array([t["s2"] for t in T])
    op = np.array([t["op"] for t in T])
    z = np.array([t["Z"] for t in T])
    jobs = []
    for i in idx:
        other = tid != tid[i]
        # latent 5: target s1 + s2'
        cands = sorted(set(s2[other & (s2 != s2[i])].tolist()))
        jobs_i = []
        if cands and (other & (s2 == s2[i])).any():
            s2p = int(rng.choice(cands))
            diff5 = vecs[other & (s2 == s2p), 5].astype(np.float32).mean(0)
            same5 = vecs[other & (s2 == s2[i]), 5].astype(np.float32).mean(0)
            jobs_i.append((5, diff5, same5, int(s1[i] + s2p)))
        # latent 3: donors share op and Z; target s1' + s1' * Z
        pool = other & (op == op[i]) & (z == z[i])
        c3 = sorted(set(s1[pool & (s1 != s1[i])].tolist()))
        if c3 and (pool & (s1 == s1[i])).any():
            s1p = int(rng.choice(c3))
            diff3 = vecs[pool & (s1 == s1p), 3].astype(np.float32).mean(0)
            same3 = vecs[pool & (s1 == s1[i]), 3].astype(np.float32).mean(0)
            jobs_i.append((3, diff3, same3, int(s1p + s1p * z[i])))
        for lat, dv, sv, target in jobs_i:
            own = vecs[i, lat].astype(np.float32)
            rnd = rng.standard_normal(own.shape).astype(np.float32)
            rnd *= np.linalg.norm(own) / np.linalg.norm(rnd)
            for cond, v in (("diff", dv), ("same", sv), ("random", rnd), ("none", None)):
                jobs.append({"i": int(i), "lat": lat, "cond": cond, "vec": v, "target": target})
    return jobs


def run_patching(model, tok, T: list[dict], vecs: np.ndarray) -> list[dict]:
    import torch

    from p15 import codi_core as cc

    jobs = patch_plan(T, vecs)
    rows = []
    for lat in (3, 5):
        for cond in ("diff", "same", "random", "none"):
            js = [j for j in jobs if j["lat"] == lat and j["cond"] == cond]
            prompts = [T[j["i"]]["prompt"] for j in js]
            if cond == "none":
                out = cc.run_latent(model, tok, prompts, capture_resid=False, capture_lltop=False)
            else:
                pv = torch.tensor(np.stack([j["vec"] for j in js]))
                out = cc.run_latent(model, tok, prompts, capture_resid=False, capture_lltop=False,
                                    patch_index=lat, patch_vectors=pv,
                                    patch_mask=torch.ones(len(js), dtype=torch.bool))
            for j, text in zip(js, out["texts"]):
                it = T[j["i"]]
                rows.append({"item_id": it["item_id"], "lat": lat, "cond": cond,
                             "target": j["target"], "gold": it["gold"],
                             "ans": tasks.parse_answer(text), "out": text})
    return rows


# ---------------------------------------------------------------- stages


def load_items() -> tuple[list[dict], list[dict], dict]:
    p = paths()
    add, sub = tasks.load_upstream_templates(UPSTREAM)
    T = tasks.make_T(add, sub)
    G = tasks.load_G(p["gsm_test"])
    C = tasks.make_C_splits(seed=SEED)
    if LIMIT:
        rng = np.random.default_rng(SEED + 99)
        sub_ = lambda xs: [xs[i] for i in sorted(rng.choice(len(xs), min(LIMIT, len(xs)), replace=False))]
        T, G = sub_(T), sub_(G)
        C = {k: sub_(v) for k, v in C.items()}
    return T, G, C


def step_stage_a(force: bool = False) -> int:
    from p15 import codi_core as cc

    p = paths()
    T, G, C = load_items()
    with Timer("load_a"):
        model, tok, report = cc.load_codi(p["ckpt"], p["base"], UPSTREAM, device=DEVICE, **MODEL_KW)
    _json_dump(report, RESULTS / "smoke" / "key_report.json")
    with Timer("smoke"):
        checks = smoke(model, tok, report, T, G, C["eval"])
    if not checks["structural_pass"] and not force:
        log("smoke", "STRUCTURAL_FAIL: bulk run not started")
        return 3
    if not checks["diagnostics"]["replication_ok"]:
        with Timer("f2_smoke"):
            sampled_smoke(model, tok, T, G)
    outdir = RESULTS / "A"
    _json_dump({str(k): v for k, v in cc.number_tokens(tok).items()}, outdir / "number_tokens.json")
    with Timer("a_T"):
        outT = run_set(model, tok, T, resid=True)
    with Timer("a_patchscope"):
        for lat in (3, 5):
            ps = cc.patchscope(model, tok, outT["latent_vectors"][:, lat])
            for r, n in zip(outT["rows"], ps["numbers"]):
                r[f"ps_lat{lat}"] = n
    save_set(outdir, "T", outT, keep_resid=True)
    np.save(outdir / "T_latvec.npy", outT["latent_vectors"])
    log("a_T", f"latent_acc={accuracy(outT['rows'], 'ans_latent'):.3f} "
               f"direct_acc={accuracy(outT['rows'], 'ans_direct'):.3f} "
               f"verbal_acc={accuracy(outT['rows'], 'ans_verbal'):.3f}")
    with Timer("a_G"):
        outG = run_set(model, tok, G, resid=False)
    save_set(outdir, "G", outG, keep_resid=False)
    log("a_G", f"latent_acc={accuracy(outG['rows'], 'ans_latent'):.3f} "
               f"direct_acc={accuracy(outG['rows'], 'ans_direct'):.3f}")
    with Timer("a_patch"):
        prow = run_patching(model, tok, T, outT["latent_vectors"])
    tasks.write_jsonl(outdir / "patch.jsonl", prow)
    return 0


def step_b_data() -> None:
    p = paths()
    _, _, C = load_items()
    aug_path = Path(p["gsm_aug"])
    try:
        aug = json.loads(aug_path.read_text())
    except json.JSONDecodeError:
        aug = [json.loads(line) for line in aug_path.read_text().splitlines() if line.strip()]
    n_gsm = min(24000, len(aug)) if LIMIT else 24000
    train = tasks.mix_train([tasks.to_icot_record(it) for it in C["train"]], aug, n_gsm=n_gsm,
                            seed=SEED)
    out = RESULTS / "B" / "data"
    tasks.write_jsonl(DATA / "b_train.jsonl", train)
    tasks.write_jsonl(DATA / "b_val.jsonl", [tasks.to_icot_record(it) for it in C["val"]])
    tasks.write_jsonl(out / "C_eval_items.jsonl", C["eval"])
    man = {"n_train": len(train), "n_c_train": len(C["train"]), "n_val": len(C["val"]),
           "n_eval": len(C["eval"]), "seed": SEED,
           "sha256": {n: sha256(DATA / f"{n}.jsonl") for n in ("b_train", "b_val")}}
    _json_dump(man, out / "manifest.json")
    log("b_data", json.dumps(man["sha256"]) + f" n_train={len(train)}")


def step_b_eval() -> None:
    from p15 import codi_core as cc

    p = paths()
    trainables = RESULTS / "B" / "trainables.safetensors"
    _, G, C = load_items()
    with Timer("load_b"):
        model, tok, report = cc.load_codi(p["ckpt"], p["base"], UPSTREAM, device=DEVICE,
                                          trainables=str(trainables), **MODEL_KW)
    _json_dump(report, RESULTS / "B" / "key_report.json")
    with Timer("b_C"):
        outC = run_set(model, tok, C["eval"], resid=True)
    save_set(RESULTS / "B", "C", outC, keep_resid=True)
    by_k = {k: accuracy([r for r in outC["rows"] if r["k"] == k], "ans_latent") for k in (3, 4, 5)}
    log("b_C", f"latent_acc={accuracy(outC['rows'], 'ans_latent'):.3f} "
               f"direct_acc={accuracy(outC['rows'], 'ans_direct'):.3f} "
               f"verbal_acc={accuracy(outC['rows'], 'ans_verbal'):.3f} by_k={by_k}")
    with Timer("b_G"):
        outG = run_set(model, tok, G, resid=False)
    save_set(RESULTS / "B", "G", outG, keep_resid=False)
    log("b_G", f"latent_acc={accuracy(outG['rows'], 'ans_latent'):.3f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["env", "download", "stage_a", "b_data", "b_eval"])
    ap.add_argument("--force", action="store_true", help="stage_a: run past a structural fail")
    a = ap.parse_args(argv)
    RESULTS.mkdir(parents=True, exist_ok=True)
    if a.step == "env":
        step_env()
    elif a.step == "download":
        step_download()
    elif a.step == "stage_a":
        return step_stage_a(force=a.force)
    elif a.step == "b_data":
        step_b_data()
    elif a.step == "b_eval":
        step_b_eval()
    return 0


if __name__ == "__main__":
    sys.exit(main())
