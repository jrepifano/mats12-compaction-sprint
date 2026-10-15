"""P14 pod driver (plan.md "Setup", "Automatic rules"). Run by run.sh from /ws/emx, in
/ws/venv-peft:

    python probes/P14-hidden-mode-readout/p14_pod.py [--dry-run] [--tiny]

Order (one model in memory at a time, adapters merged into the weights as in P02):
1. Inputs: E13's frozen files (sha256 prefixes), P14's committed `inputs/` (TEST pairs, S1
   prompts, text scores); every prompt set built (`p14.common.build_sets`) and rendered with
   E13's renderer; the ask answers' first tokens checked to be single tokens.
2. For each model in POD_ORDER (base, org_s1, good, org_s0, org_s2):
   - load, adapter sha256 and recipe check, merged logits differ from the base's on 4 prompts;
   - **ask pass**: every distinct suffix x the two ask questions; logits of Yes/No (ASK score =
     mean over the two of logit(No) - logit(Yes)); the notice-token mean of every block from
     the first ask prompt (it does not depend on the question: causal attention);
   - **question pass**: every prompt's last-token block outputs (the first model's first 200
     prompts are a timed benchmark; projected > BENCH_LIMIT_S for all models -> smoke fail);
   - **probes** (`p14.probes`) fitted on TRAIN, at "last" and "nmean", every layer; all prompts
     scored; DEV check: org_s1's best LR AUROC on DEV >= 0.90 (smoke).
   - saved: `results/scores/<model>.npz`, probe weights at a few layers, and for base and
     org_s1 the activations at SAVE_LAYERS (last token) and every layer (notice mean).
3. After org_s1: E13's hook checks (i)-(iv) at the input site; the selection
   (`p14.select.selection`, CAL) over A_org (org_s1), A_base, ASK_org, ASK_base, T1-T3;
   `results/selection.json`; then, unless the kill rule fires, generation on org_s1:
   TEST x {0, 4, 1}, UTIL x {0, 4, 1}, extra samples on the disagreement set x {0, 4}; each
   cell written to results/gen/ and copied to logs/cells/ for laptop judging.
A failed automatic check exits 3 with the anchored tag `P14_SMOKE_FAIL`.
Completed outputs are skipped on a rerun (score files by existence, cells by an exact request
match), so a resumed run does not redo them.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sys
import time
import tomllib
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))
from p14 import common as K

sys.path.insert(0, str(K.REPO / "src"))

import numpy as np
from p14 import probes as PR
from p14 import select as SEL

RES = PROBE_DIR / "results"
LOGS = PROBE_DIR / "logs"
GEN = RES / "gen"
SCORES = RES / "scores"
BATCH_ACTS = 64
BATCH_GEN = 256
MAX_NEW = 600
SAVE_LAYERS = (10, 14, 18, 22)
BENCH_N = 200
BENCH_LIMIT_S = 45 * 60
DEV_MIN_AUROC = 0.90
HOOK_MAX_NEW = 128
POSITIONS = ("last", "nmean")
DIRECTION_PATH = K.REPO / K.DIRECTION
T0 = time.time()
ADAPTER_SHA256 = {  # adapter_model.safetensors, from the main checkout's results/E13/adapters
    "org_tan32_s1": "2b72ba9922946d0b",
    "good_tan32_s0": "4ea3a310a99b2131",
    "org_tan32_s0": "c2ae527eaf84f56b",
    "org_tan32_s2": "5d84930418f95149",
}


class SmokeFail(Exception):
    pass


def utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"{utc()} P14 {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=float))
    tmp.replace(path)


def gpu_hours() -> float:
    return float(tomllib.loads((PROBE_DIR / "probe.toml").read_text())["pod"]["gpu_hours"])


# --- prompts -------------------------------------------------------------------------------------


class Prompts:
    """Every prompt row of every set, rendered, in one fixed order (sets in SET_ORDER, rows as
    built). Also the distinct suffixes (text ids) for the ask pass."""

    SET_ORDER = ("train", "dev", "s1", "cal", "test", "util", "s3")

    def __init__(self, tok, sets: dict[str, list[dict]], tiny: int = 0):
        from emx.e13.render import render_prompt_sys

        rows = []
        for s in self.SET_ORDER:
            rr = sets.get(s, [])
            rows += rr[:tiny] if tiny else rr
        self.rows = rows
        self.ids = [r["prompt_id"] for r in rows]
        self.sets = np.array([r["set"] for r in rows])
        self.labels = np.array([r["unobs"] for r in rows])
        self.text_ids = [r["text_id"] for r in rows]
        self.render = {}
        for r in rows:
            self.render[r["prompt_id"]] = render_prompt_sys(tok, r["suffix"], r["question"])
        texts = K.suffix_texts({"all": rows})
        self.suffix_ids = sorted(texts)
        self.suffix_text = {i: texts[i]["text"] for i in self.suffix_ids}
        self.suffix_cond = {i: texts[i]["cond"] for i in self.suffix_ids}

    def prompt_ids_tokens(self) -> list[list[int]]:
        return [list(self.render[p][1]) for p in self.ids]


def notice_span(tok, text: str, ids: list[int], suffix: str) -> tuple[int, int]:
    """Token span of the suffix inside a rendered prompt (E13 render: DEFAULT_SYSTEM + blank line
    + suffix, in the system turn)."""
    from emx.contrastive import _span
    from emx.qwen import registry as R

    head = f"{R.IM_START}system\n{R.DEFAULT_SYSTEM}\n\n"
    if not text.startswith(head + suffix):
        raise AssertionError("render does not start with the system head and the suffix")
    return _span(tok, text, ids, len(head), len(head) + len(suffix))


def ask_token_ids(tok) -> dict[str, list[int]]:
    """Single-token ids for the answers: 'Yes'/'No' (the answer's first token) and ' Yes'/' No'.
    Raises SmokeFail when one is not a single token."""
    out = {}
    for w in ("Yes", "No", " Yes", " No"):
        ids = tok(w, add_special_tokens=False)["input_ids"]
        if len(ids) != 1:
            raise SmokeFail(f"ask answer {w!r} is {len(ids)} tokens")
        out[w] = ids
    return {k: v[0] for k, v in out.items()}


# --- models --------------------------------------------------------------------------------------


def load_model(name: str):
    from emx.qwen import train as T

    t = time.time()
    m, _ = T.load_base("cuda", attn="eager")
    key = K.MODELS[name]
    info = {"model": name, "adapter": key}
    if key is not None:
        from peft import PeftModel

        from emx.e13.vgen import check_adapter

        path = K.REPO / K.ADAPTERS / key
        sha = K.sha256_file(path / "adapter_model.safetensors")
        if not sha.startswith(ADAPTER_SHA256[key]):
            raise SmokeFail(f"adapter {key} sha256 {sha[:16]} is not the main checkout's")
        check_adapter(key, path)
        m = PeftModel.from_pretrained(m, str(path)).merge_and_unload()
        info["adapter_sha256"] = sha
    m.eval()
    info["load_s"] = round(time.time() - t, 1)
    log(f"loaded {name} in {info['load_s']} s")
    return m, info


def free(model) -> None:
    import gc

    import torch

    del model
    gc.collect()
    torch.cuda.empty_cache()


def last_logits(model, ids_list) -> np.ndarray:
    import torch

    out = []
    with torch.no_grad():
        for ids in ids_list:
            x = torch.tensor([list(ids)], device=next(model.parameters()).device)
            out.append(model(input_ids=x).logits[0, -1].float().cpu().numpy())
    return np.stack(out)


# --- detection -----------------------------------------------------------------------------------


def ask_pass(model, tok, P: Prompts, yes_no: dict[str, int]) -> dict:
    """Per suffix: ASK score and the notice-token mean of every block."""
    from p14.acts import record

    from emx.e13.render import render_prompt_sys

    ids, spans, keys = [], [], []
    for sid in P.suffix_ids:
        for qi, q in enumerate(K.ASK_QUESTIONS):
            text, tk = render_prompt_sys(tok, P.suffix_text[sid], q)
            ids.append(list(tk))
            spans.append(notice_span(tok, text, list(tk), P.suffix_text[sid]))
            keys.append((sid, qi))
    lid = [yes_no["Yes"], yes_no["No"]]
    rec = record(model, ids, spans=spans, logit_ids=lid, batch_size=BATCH_ACTS)
    n_q = len(K.ASK_QUESTIONS)
    lg = rec["logits"].reshape(len(P.suffix_ids), n_q, 2)
    ask = (lg[:, :, 1] - lg[:, :, 0]).mean(1)  # logit(No) - logit(Yes): higher = unobserved
    nmean = rec["span"].reshape(len(P.suffix_ids), n_q, *rec["span"].shape[1:])[:, 0]
    # sanity: the notice mean must not depend on the ask question (causal attention)
    sp = rec["span"].reshape(len(P.suffix_ids), n_q, *rec["span"].shape[1:])
    drift = float(np.abs(sp[:, 0].astype(np.float32) - sp[:, 1].astype(np.float32)).max())
    return {"ask": ask, "ask_logits": lg, "nmean": nmean, "nmean_q_drift": drift,
            }


def question_pass(model, P: Prompts, bench: bool) -> tuple[np.ndarray, dict]:
    from p14.acts import record

    ids = P.prompt_ids_tokens()
    info = {}
    if bench:
        n = min(BENCH_N, len(ids))
        t = time.time()
        record(model, ids[:n], batch_size=BATCH_ACTS)
        dt_s = time.time() - t
        per = dt_s / n
        n_ask = len(P.suffix_ids) * len(K.ASK_QUESTIONS)
        proj = per * (len(ids) + n_ask) * len(K.POD_ORDER)
        info = {"bench_n": n, "bench_s": round(dt_s, 2), "per_prompt_s": per,
                "projected_all_models_s": round(proj), "limit_s": BENCH_LIMIT_S}
        log(f"P14_BENCH {n} prompts in {dt_s:.1f} s; all models projected {proj / 60:.1f} min")
        if proj > BENCH_LIMIT_S:
            raise SmokeFail(f"activation passes projected {proj / 60:.0f} min > "
                            f"{BENCH_LIMIT_S / 60:.0f}")
    t = time.time()
    rec = record(model, ids, batch_size=BATCH_ACTS)
    info["question_pass_s"] = round(time.time() - t, 1)
    return rec["last"], info


def fit_and_score(P: Prompts, last: np.ndarray, nmean: np.ndarray, device: str) -> dict:
    """{position: [n_prompts, len(TYPES), n_layers] scores} plus the probe params."""
    tr = np.where(P.sets == "train")[0]
    y = P.labels[tr]
    groups = [P.text_ids[i] for i in tr]
    sidx = {s: i for i, s in enumerate(P.suffix_ids)}
    out, params = {}, {}
    t = time.time()
    params["last"] = PR.fit_all(last[tr], y, groups, K.SEED, device)
    out["last"] = PR.score_all(last, params["last"])
    tr_s = sorted({P.text_ids[i] for i in tr})
    ys = np.array([1 if P.suffix_cond[s] == "noov" else 0 for s in tr_s])
    params["nmean"] = PR.fit_all(nmean[[sidx[s] for s in tr_s]], ys, tr_s, K.SEED, device)
    per_suffix = PR.score_all(nmean, params["nmean"])
    rowmap = np.array([sidx.get(t, -1) for t in P.text_ids])
    sc = np.full((len(P.ids), len(PR.TYPES), nmean.shape[1]), np.nan, np.float32)
    has = rowmap >= 0
    sc[has] = per_suffix[rowmap[has]]
    out["nmean"] = sc
    log(f"probes fitted and scored in {time.time() - t:.0f} s")
    return out, params


def detector_table(P: Prompts, model_scores: dict[str, dict]) -> dict[str, dict[str, np.ndarray]]:
    """Detectors for the selection: A_<model> configurations from the probe scores; ASK_<model>
    from the ask scores; T1-T3 from inputs/text_scores.json."""
    det = {}
    for name, d in model_scores.items():
        cands = {}
        for pos in POSITIONS:
            arr = d["probe"][pos]
            for ti, t in enumerate(PR.TYPES):
                if t == "lr_shuf":
                    continue  # a null, never selected
                for li in range(arr.shape[2]):
                    cands[f"{t}|{pos}|{li}"] = arr[:, ti, li]
        det[f"A_{name}" if name != "org_s1" else "A_org"] = cands
        sid = {s: i for i, s in enumerate(P.suffix_ids)}
        ask = np.array([d["ask"][sid[t]] if t in sid else np.nan for t in P.text_ids], np.float32)
        det[f"ASK_{name}" if name != "org_s1" else "ASK_org"] = {"ask": ask}
    ts = json.loads(K.TEXT_SCORES.read_text())["scores"]
    for m in ("T1", "T2", "T3"):
        v = np.array([np.nan if not t or ts.get(t, {}).get(m) is None else ts[t][m]
                      for t in P.text_ids], np.float32)
        det[m] = {m: v}
    return det


def save_model_outputs(name: str, P: Prompts, last, nmean, ask, scores, params, info) -> None:
    SCORES.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(SCORES / f"{name}.npz", prompt_ids=np.array(P.ids),
                        suffix_ids=np.array(P.suffix_ids), last=scores["last"],
                        nmean=scores["nmean"], ask=ask["ask"], ask_logits=ask["ask_logits"],
                        types=np.array(PR.TYPES))
    pw = {}
    for pos in POSITIONS:
        for li in SAVE_LAYERS:
            p = params[pos][li]
            pw[f"{pos}|{li}|lr_w"] = p["lr"][0]
            pw[f"{pos}|{li}|dm"] = p["dm"]
            pw[f"{pos}|{li}|mu"] = p["mu"]
            pw[f"{pos}|{li}|sd"] = p["sd"]
    np.savez_compressed(SCORES / f"{name}.weights.npz", **pw)
    info["lam"] = {pos: [p["lam"] for p in params[pos]] for pos in POSITIONS}
    if name in ("base", "org_s1"):
        acts = RES / "acts"
        acts.mkdir(parents=True, exist_ok=True)
        np.save(acts / f"{name}.last.npy", last[:, list(SAVE_LAYERS)])
        np.save(acts / f"{name}.nmean.npy", nmean)
    write_json(SCORES / f"{name}.info.json", info)


def load_model_outputs(name: str) -> dict | None:
    f = SCORES / f"{name}.npz"
    if not f.is_file():
        return None
    z = np.load(f, allow_pickle=False)
    return {"probe": {"last": z["last"], "nmean": z["nmean"]}, "ask": z["ask"]}


# --- generation ----------------------------------------------------------------------------------


def gen_requests(tok, P: Prompts, idx, alpha: float, samples, set_name: str) -> list:
    """GenRequests of prompts `idx` at strength `alpha` for each sample index in `samples`."""
    from emx.e13 import contracts as C

    out = []
    for i in idx:
        r = P.rows[i]
        text, ids = P.render[r["prompt_id"]]
        for s in samples:
            s = int(r.get("sample_idx", s))
            out.append(C.GenRequest(
                condition=f"{K.EXP}|{set_name}|{r['cond']}|{K.SITE if alpha else 'none'}|"
                          f"a{alpha:g}",
                cell=K.MODELS[K.GEN_MODEL], eval_fmt="qwen_chat_sys",
                qset="ext48" if r["task_type"] == "em" else "tasks", qid=r["qid"],
                question=r["question"], sample_idx=s,
                seed=K.gen_seed(r["set"], r["cond"], r["text_id"], r["qid"], s),
                prompt_text=text, prompt_ids=tuple(ids), model_key=K.MODELS[K.GEN_MODEL],
                battery=f"p14{set_name}", notice_cond=r["cond"], family=r["family"],
                instance_id=r["text_id"], system_suffix=r["suffix"],
                site=K.SITE if alpha else "none", alpha=float(alpha),
                task_type=r["task_type"]))
    return out


def _done(path: Path, reqs) -> bool:
    if not path.is_file():
        return False
    with path.open(newline="") as f:
        got = sorted((r["condition"], r["instance_id"], r["qid"], int(r["sample_idx"]),
                      int(r["seed"])) for r in csv.DictReader(f))
    return got == sorted((r.condition, r.instance_id, r.qid, r.sample_idx, int(r.seed))
                         for r in reqs)


def run_cell(model, tok, name: str, reqs, u, g, timing: list) -> int:
    from emx.e13 import contracts as C
    from emx.e13 import hfgen

    path = GEN / f"{name}.csv"
    if _done(path, reqs):
        log(f"cell {name} already complete, skipped")
        return 0
    t = time.time()
    events: list = []
    rows = hfgen.run_hf(model, tok, reqs, batch_size=BATCH_GEN, steer_for=lambda _r: (u, g),
                        max_new_tokens=MAX_NEW, events=events)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(C.GEN_ALL_COLUMNS))
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    (LOGS / "cells").mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, LOGS / "cells" / path.name)
    wall = time.time() - t
    rec = {"cell": name, "n": len(reqs), "wall_s": round(wall, 1),
           "answers_per_s": round(len(reqs) / max(wall, 1e-9), 2), "oom_events": events,
           "mean_new_tokens": round(float(np.mean([r["n_new_tokens"] for r in rows])), 1),
           "utc_end": utc(), "elapsed_s": round(time.time() - T0, 1)}
    timing.append(rec)
    with (RES / "timing.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    log(f"P14_CELL {name} n={len(reqs)} wall={wall:.0f}s rate={rec['answers_per_s']}/s")
    return len(reqs)


def generation(model, tok, P: Prompts, sel: dict, det: dict, u, g, checks: dict) -> None:
    test = np.where(P.sets == "test")[0]
    util = np.where(P.sets == "util")[0]
    dis = SEL.disagreement(sel, det, P.ids, P.sets)
    timing: list = []
    plan = [("test__a0", test, 0.0, [0]), ("test__a4", test, 4.0, [0])]
    extra_n = len(dis) * K.EXTRA_SAMPLES * len(K.EXTRA_ALPHAS)
    rng = np.random.default_rng(K.SEED)
    if extra_n > K.EXTRA_MAX_ANSWERS:
        keep = K.EXTRA_MAX_ANSWERS // (K.EXTRA_SAMPLES * len(K.EXTRA_ALPHAS))
        dis = np.sort(rng.choice(dis, keep, replace=False))
    samples_extra = list(range(1, 1 + K.EXTRA_SAMPLES))
    plan += [("extra__a0", dis, 0.0, samples_extra), ("extra__a4", dis, 4.0, samples_extra)]
    plan += [(f"util__a{a:g}", util, a, [0]) for a in K.ALPHAS]
    plan += [("test__a1", test, 1.0, [0])]
    write_json(RES / "gen_plan.json", {
        "n_test_prompts": len(test), "n_util_prompts": len(util),
        "n_disagreement_prompts": len(dis),
        "disagreement_prompt_ids": [P.ids[i] for i in dis],
        "cells": [(n, len(ix), a, s) for n, ix, a, s in plan]})
    budget_s = (gpu_hours() - 0.5) * 3600  # setup ran before T0
    for k, (name, idx, alpha, samples) in enumerate(plan):
        if not len(idx):
            continue
        reqs = gen_requests(tok, P, idx, alpha, samples, name.split("__")[0])
        elapsed = time.time() - T0
        if timing:
            rate = np.mean([t["answers_per_s"] for t in timing])
            need = len(reqs) / rate * 1.1
            if elapsed + need > budget_s:
                log(f"P14_CELL_SKIPPED {name}: needs {need / 60:.0f} min, "
                    f"{(budget_s - elapsed) / 60:.0f} min left")
                checks.setdefault("skipped_cells", []).append(name)
                continue
        run_cell(model, tok, name, reqs, u, g, timing)


# --- main ----------------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build and render; print the plan")
    ap.add_argument("--tiny", type=int, default=0, help="first N prompts per set (smoke tests)")
    a = ap.parse_args(argv)
    from emx.qwen.render import load_tokenizer

    RES.mkdir(parents=True, exist_ok=True)
    tok = load_tokenizer()
    checks: dict = {"inputs": K.check_inputs(), "utc_start": utc(),
                    "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown")}
    yes_no = ask_token_ids(tok)
    sets = K.build_sets()
    P = Prompts(tok, sets, tiny=a.tiny)
    checks["n_prompts"] = {s: int((P.sets == s).sum()) for s in P.SET_ORDER}
    checks["n_suffixes"] = len(P.suffix_ids)
    log(f"prompts {checks['n_prompts']}; suffixes {len(P.suffix_ids)}")
    if a.dry_run:
        print(json.dumps(checks, indent=1, default=str))
        return 0

    import torch

    checks["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    write_json(RES / "checks.json", checks)
    direction = json.loads(DIRECTION_PATH.read_text())
    u = np.asarray(direction["u"], np.float32)
    g = float(direction["g"])
    ref_ids = [list(P.render[p][1]) for p in P.ids[:4]]
    base_logits = None
    model_scores: dict[str, dict] = {}
    sel = None
    for mi, name in enumerate(K.POD_ORDER):
        done = load_model_outputs(name)
        if done is not None and name != K.GEN_MODEL:
            model_scores[name] = done
            log(f"{name}: scores exist, skipped")
            continue
        model, info = load_model(name)
        ll = last_logits(model, ref_ids)
        if name == "base":
            base_logits = ll
        elif base_logits is not None:
            diff = float(np.abs(ll - base_logits).max())
            info["max_abs_logit_diff_vs_base"] = diff
            if diff <= 0.05:
                raise SmokeFail(f"{name}: merged logits equal the base's (diff {diff})")
        if done is not None:
            model_scores[name] = done
        else:
            t = time.time()
            ask = ask_pass(model, tok, P, yes_no)
            info["ask_pass_s"] = round(time.time() - t, 1)
            info["nmean_q_drift"] = ask["nmean_q_drift"]
            last, qinfo = question_pass(model, P, bench=(mi == 0))
            info.update(qinfo)
            scores, params = fit_and_score(P, last, ask["nmean"],
                                           "cuda" if torch.cuda.is_available() else "cpu")
            dev = np.where((P.sets == "dev") & (P.labels >= 0))[0]
            lr = scores["last"][:, PR.TYPES.index("lr")]
            info["dev_auroc_lr_last"] = [SEL.auroc(lr[dev, li], P.labels[dev])
                                         for li in range(lr.shape[1])]
            save_model_outputs(name, P, last, ask["nmean"], ask, scores, params, info)
            model_scores[name] = {"probe": scores, "ask": ask["ask"]}
            log(f"{name}: dev AUROC (lr, last) max {np.nanmax(info['dev_auroc_lr_last']):.3f}")
            del last
            if name == K.GEN_MODEL and np.nanmax(info["dev_auroc_lr_last"]) < DEV_MIN_AUROC:
                raise SmokeFail(f"A_org's best DEV AUROC {np.nanmax(info['dev_auroc_lr_last'])}"
                                f" < {DEV_MIN_AUROC}")
        checks.setdefault("loads", []).append(info)
        write_json(RES / "checks.json", checks)
        if name == K.GEN_MODEL:
            from emx.e13 import steer

            hp = [list(P.render[P.ids[i]][1]) for i in np.where(P.sets == "test")[0][:16]]
            t = time.time()
            hc = steer.hook_checks(model, tok, K.SITE, u, g, hp, max_new_tokens=HOOK_MAX_NEW)
            hc["wall_s"] = round(time.time() - t, 1)
            checks["hook_checks"] = hc
            write_json(RES / "checks.json", checks)
            log(f"hook checks passed={hc.get('passed')} ({hc['wall_s']} s)")
            if not hc.get("passed"):
                raise SmokeFail("E13 hook checks failed at the input site")
            det = detector_table(P, model_scores)
            sel = SEL.selection(det, P.ids, P.sets, P.labels, K.FIRE_RATES, K.KILL_AUROC)
            sel["utc"] = utc()
            write_json(RES / "selection.json", sel)
            log(f"P14_SELECTION A_org {sel['detectors']['A_org']['config']} "
                f"cal {sel['detectors']['A_org']['cal_auroc']:.3f}; comparator "
                f"{sel['comparator']} cal {sel['detectors'][sel['comparator']]['cal_auroc']:.3f};"
                f" kill={sel['kill']}")
            if sel["kill"]:
                log("P14_KILL every detector's CAL AUROC < 0.6: no TEST generation")
            else:
                generation(model, tok, P, sel, det, u, g, checks)
            write_json(RES / "checks.json", checks)
        free(model)
        model = None
    checks["utc_end"] = utc()
    checks["elapsed_s"] = round(time.time() - T0, 1)
    write_json(RES / "checks.json", checks)
    log(f"P14_ALL_DONE elapsed={checks['elapsed_s']} s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFail as e:
        log(f"P14_SMOKE_FAIL {e}")
        sys.exit(3)
