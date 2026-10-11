"""P02 pod driver: directions on the base model, the automatic smoke checks, every generation
cell, and the latent readout (plan.md "Setup", "Automatic rules"; log.md "Design decisions").

Run by run.sh on the pod, from /ws/emx, in /ws/venv-peft:
    python probes/P02-evalaware-steering-check/p02_pod.py [--dry-run] [--only-cells ID,...]

Order (one model in memory at a time; adapters merged into the base weights):
1. base: tokenizer, every cell's requests built (and checked against E13's render); the
   directions extracted and FROZEN (results/directions/, sha256 logged) before any adapter is
   loaded; smoke check (2) (A's half-split cosine >= 0.7 at every steered layer); hook checks
   (3) for A, B and one null; reference logits for (1); the latent readout; then base's cells.
   The first full cell is timed: check (5) projects the whole run and cuts the null cells to
   3 samples per question if it would pass gpu_hours.
2. org: check (1) (logits differ from the base's), the latent readout, its cells.
3. good: likewise.
4. org again: the 100 null cells (the most cuttable work, last).
Smoke check (4) is judged on the laptop from logs/smoke/ (log.md decision 9). Each finished
cell's CSV is also copied to logs/cells/ so the laptop can judge while the pod works.
A failed automatic check exits 3 with the anchored tag `P02_SMOKE_FAIL`; nothing after it runs.
Cells already complete under results/gen/ are skipped (row count and key check).
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
REPO = PROBE_DIR.parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(PROBE_DIR))

import numpy as np
import p02_cells as P

RES = PROBE_DIR / "results"
LOGS = PROBE_DIR / "logs"
GEN = RES / "gen"
DIRS = RES / "directions"
BATCH_SIZE = 64
MAX_NEW = 600  # = emx.qwen.registry.SAMPLING["max_new_tokens"] (tests lower it)
E13_DIRS = {"input": REPO / "results/E13/run2/direction/input.json",
            "output": REPO / "results/E13/run2/direction/output.json"}
LATENT_LAYERS = tuple(sorted({*P.A_LAYERS, 13, 14}))
T0 = time.time()


class SmokeFail(Exception):
    pass


def utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"{utc()} P02 {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=float))
    tmp.replace(path)


def gpu_hours() -> float:
    return float(tomllib.loads((PROBE_DIR / "probe.toml").read_text())["pod"]["gpu_hours"])


# --- models --------------------------------------------------------------------------------------


def load_model(model: str):
    """The pinned base in bf16, eager attention, on CUDA; with the model's adapter (checked against
    its recipe and sha256) merged into the weights."""
    from emx.qwen import train as T

    t = time.time()
    m, _ = T.load_base("cuda", attn="eager")
    key = P.MODELS[model]
    info = {"model": model, "adapter": key}
    if key is not None:
        from peft import PeftModel

        from emx.e13.vgen import check_adapter

        path = REPO / P.ADAPTER_DIR / key
        sha = P.sha256_file(path / "adapter_model.safetensors")
        if sha != P.ADAPTER_SHA256[key]:
            raise SmokeFail(f"adapter {key} sha256 {sha} is not the plan's")
        check_adapter(key, path)
        m = PeftModel.from_pretrained(m, str(path)).merge_and_unload()
        info["adapter_sha256"] = sha
    m.eval()
    info["load_s"] = round(time.time() - t, 1)
    log(f"loaded {model} in {info['load_s']} s")
    return m, info


def free(model) -> None:
    import gc

    import torch

    del model
    gc.collect()
    torch.cuda.empty_cache()


def last_logits(model, prompts_ids) -> np.ndarray:
    import torch

    out = []
    with torch.no_grad():
        for ids in prompts_ids:
            x = torch.tensor([list(ids)], device=next(model.parameters()).device)
            out.append(model(input_ids=x).logits[0, -1].float().cpu().numpy())
    return np.stack(out)


# --- directions ----------------------------------------------------------------------------------


def extract(model, tok, prompts: dict) -> dict:
    """Base-model activations of every contrast prompt and block 14's mean residual norm."""
    from emx.contrastive import (
        block_outputs_at,
        render_system_only,
        render_user,
        residual_norm_stats,
    )
    from emx.qwen.render import render_prompt

    def acts(texts, system_turn=False):
        ids, pos = [], []
        for t in texts:
            _, i, (_, end) = render_system_only(tok, t) if system_turn else render_user(tok, t)
            ids.append(i)
            pos.append(end - 1)
        return block_outputs_at(model, ids, pos)

    hua = prompts["hua_table12"]["pairs"]
    out = {"hua_pos": acts([p for p, _ in hua]), "hua_neg": acts([n for _, n in hua])}
    b = prompts["opus_testing_vs_conversation"]["pairs"]
    out["b_pos"] = acts([p for p, _ in b], system_turn=True)
    out["b_neg"] = acts([n for _, n in b], system_turn=True)
    for c, pairs in prompts["read_controls"]["pairs"].items():
        out[f"ctl_{c}_pos"] = acts([p for p, _ in pairs])
        out[f"ctl_{c}_neg"] = acts([n for _, n in pairs])
    norm_ids = [render_prompt(tok, q)[1] for q in P.norm_prompts(REPO)]
    stats = residual_norm_stats(model, norm_ids, layers=[P.B_MID_LAYER])
    return out, stats


def save_directions(vecs: dict, acts: dict, stats: dict) -> dict:
    from safetensors.numpy import save_file

    from emx.contrastive import cosine, half_split_cosines

    DIRS.mkdir(parents=True, exist_ok=True)
    flat = {"A_all": vecs["A_all"], "B_raw": vecs["B_raw"]}
    for site in ("A", *P.NULL_SITES):
        for L, v in vecs[site].items():
            flat[f"{site}/{L}"] = v
    save_file({k: np.ascontiguousarray(v, dtype=np.float32) for k, v in flat.items()},
              str(DIRS / "vectors.safetensors"))
    hs = half_split_cosines(acts["hua_pos"], acts["hua_neg"], n_splits=200, seed=0)
    med = {L: float(np.median(hs[:, L])) for L in range(P.N_LAYERS)}
    e13 = {}
    for site, f in E13_DIRS.items():
        u = np.asarray(json.loads(f.read_text())["u"], np.float32)
        layer = 13 if site == "input" else 14  # input of block 14 = output of block 13
        e13[site] = {"block_output": layer,
                     "cos_A": cosine(vecs["A_all"][layer], u),
                     "cos_B": cosine(vecs["B_raw"][layer], u)}
    rec = {
        "utc_frozen": utc(),
        "vectors_sha256": P.sha256_file(DIRS / "vectors.safetensors"),
        "prompts_sha256": P.PROMPTS_SHA256,
        "A_layers": list(P.A_LAYERS),
        "A_norms": vecs["norms"]["A"],
        "A_norm_all": [float(np.linalg.norm(v)) for v in vecs["A_all"]],
        "A_half_split_median": med,
        "A_half_split_pass": all(med[L] >= P.HALF_SPLIT_MIN for L in P.A_LAYERS),
        "B_mid_layer": P.B_MID_LAYER,
        "B_raw_norm_mid": vecs["norms"]["B_mid"],
        "resid_norm_mid": stats,
        "b_factor": {str(k): v for k, v in vecs["b_factor"].items()},
        "cos_A_B": {L: cosine(vecs["A_all"][L], vecs["B_raw"][L]) for L in range(P.N_LAYERS)},
        "cos_A_nulls": {s: {L: cosine(vecs["A"][L], v) for L, v in vecs[s].items()}
                        for s in P.NULL_SITES},
        "cos_with_e13_direction": e13,
    }
    write_json(DIRS / "directions.json", rec)
    return rec


# --- generation ----------------------------------------------------------------------------------


def _done(path: Path, reqs) -> bool:
    if not path.is_file():
        return False
    with path.open(newline="") as f:
        keys = [(r["condition"], r["qid"], int(r["sample_idx"])) for r in csv.DictReader(f)]
    return sorted(keys) == sorted((r.condition, r.qid, r.sample_idx) for r in reqs)


def write_rows(path: Path, rows: list[dict]) -> None:
    from emx.e13 import contracts as C

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(C.GEN_ALL_COLUMNS))
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def run_cell(model, tok, cell: P.Cell, reqs, spans, vecs, timing: list) -> int:
    """Generate one cell (prompt-length-sorted batches; a CUDA OOM halves the batch); returns
    the answers generated (0 when already complete)."""
    from emx.e13 import hfgen
    from emx.steer_multi import MultiStats, decode_steered

    path = GEN / f"{cell.cell_id}.csv"
    if _done(path, reqs):
        log(f"cell {cell.cell_id} already complete, skipped")
        return 0
    vectors = P.cell_vectors(cell, vecs)
    order = sorted(range(len(reqs)), key=lambda i: (len(reqs[i].prompt_ids), i))
    rows: list[dict | None] = [None] * len(reqs)
    t, bs, k, calls = time.time(), BATCH_SIZE, 0, 0
    while k < len(order):
        idx = order[k:k + bs]
        st = MultiStats(calls={}, seq_lens={})
        try:
            samples = decode_steered(
                model, [list(reqs[i].prompt_ids) for i in idx], [reqs[i].seed for i in idx],
                vectors, spans=[spans[i] for i in idx], stats=st, max_new_tokens=MAX_NEW)
        except Exception as e:
            if not hfgen._is_oom(e) or len(idx) == 1:
                raise
            hfgen._free_cuda()
            bs = max(1, len(idx) // 2)
            log(f"P02_OOM {cell.cell_id}: batch size -> {bs}")
            continue
        if vectors is not None and not all(st.calls.get(L, 0) >= 1 for L in vectors):
            raise RuntimeError(f"{cell.cell_id}: a steered layer saw no hook call")
        calls += sum(st.calls.values())
        for i, s in zip(idx, samples, strict=True):
            rows[i] = hfgen.gen_row(tok, reqs[i], s)
        k += len(idx)
    write_rows(path, rows)
    (LOGS / "cells").mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, LOGS / "cells" / path.name)
    wall = time.time() - t
    rec = {"cell": cell.cell_id, "n": len(reqs), "wall_s": round(wall, 1),
           "answers_per_s": round(len(reqs) / wall, 2), "hook_calls": calls,
           "mean_new_tokens": round(float(np.mean([r["n_new_tokens"] for r in rows])), 1),
           "batch_size": bs, "utc_end": utc(), "elapsed_s": round(time.time() - T0, 1)}
    timing.append(rec)
    with (RES / "timing.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    log(f"P02_CELL {cell.cell_id} n={len(reqs)} wall={wall:.0f}s rate={rec['answers_per_s']}/s")
    return len(reqs)


def copy_smoke(cell: P.Cell) -> None:
    src = GEN / f"{cell.cell_id}.csv"
    with src.open(newline="") as f:
        r = csv.DictReader(f)
        rows = [row for row in r if int(row["sample_idx"]) < P.SMOKE_SAMPLES]
    write_rows(LOGS / "smoke" / f"smoke__{cell.cell_id}.csv", rows)
    log(f"P02_SMOKE_READY smoke__{cell.cell_id} ({len(rows)} rows) for laptop check (4)")


def is_smoke(cell: P.Cell) -> bool:
    return cell.site == "none" and cell.variant == "plain"


# --- latent readout ------------------------------------------------------------------------------


def latent(model, model_name: str, tok, reqs_by_notice: dict, vecs: dict) -> dict:
    """Projection of block outputs onto A (unit, per layer) for observed vs unobserved prompts:
    AUROC (unobserved = positive), at the last prompt token and averaged over the user span; and
    the cosine of the mean unobserved-minus-observed difference with A, B and E13's direction."""
    from emx.contrastive import block_outputs_at, block_outputs_spans, cosine

    out = {"model": model_name, "layers": list(LATENT_LAYERS), "auroc_last": {},
           "auroc_span": {}, "cos_diff_A": {}, "cos_diff_B": {}, "n": {}}
    reads = {}
    for notice, (reqs, spans) in reqs_by_notice.items():
        uniq = {}
        for r, s in zip(reqs, spans, strict=True):
            uniq.setdefault(r.prompt_ids, s)
        ids = [list(k) for k in uniq]
        sp = list(uniq.values())
        reads[notice] = {
            "last": block_outputs_at(model, ids, [len(i) - 1 for i in ids], layers=LATENT_LAYERS),
            "span": block_outputs_spans(model, ids, sp, layers=LATENT_LAYERS),
        }
        out["n"][notice] = len(ids)
    for j, L in enumerate(LATENT_LAYERS):
        u = vecs["A_all"][L] / np.linalg.norm(vecs["A_all"][L])
        for kind in ("last", "span"):
            pos = reads["noov"][kind][:, j] @ u
            neg = reads["ov"][kind][:, j] @ u
            out[f"auroc_{kind}"][L] = P.auroc(pos, neg)
        diff = reads["noov"]["last"][:, j].mean(0) - reads["ov"]["last"][:, j].mean(0)
        out["cos_diff_A"][L] = cosine(diff, vecs["A_all"][L])
        out["cos_diff_B"][L] = cosine(diff, vecs["B_raw"][L])
        if L in (13, 14):
            site = "input" if L == 13 else "output"
            u13 = np.asarray(json.loads(E13_DIRS[site].read_text())["u"], np.float32)
            out[f"cos_diff_e13_{site}"] = cosine(diff, u13)
    return out


# --- main ----------------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build requests and print the plan")
    args = ap.parse_args(argv)
    from emx.qwen.render import load_tokenizer

    RES.mkdir(parents=True, exist_ok=True)
    tok = load_tokenizer()
    prompts = P.load_prompts(REPO)
    questions = P.broad_questions(REPO)
    kept, by_id = P.kept_notices(REPO)
    null_samples = P.NULL_SAMPLES
    plan = P.run_order(null_samples)
    built: dict[str, tuple] = {}

    def reqs_for(cell):
        if cell.cell_id not in built:
            built[cell.cell_id] = P.build_requests(tok, cell, questions, kept, by_id, prompts)
        return built[cell.cell_id]

    for _, cells in plan:
        for c in cells:
            reqs_for(c)
    total = P.n_answers(plan)
    log(f"plan: {sum(len(c) for _, c in plan)} cells, {total} answers, gpu_hours {gpu_hours()}")
    if args.dry_run:
        for sess, cells in plan:
            print(sess, [c.cell_id for c in cells])
        return 0

    import torch

    env = {"utc_start": utc(),
           "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu", "torch": torch.__version__,
           "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown"), "batch_size": BATCH_SIZE,
           "n_answers_planned": total}
    import peft
    import transformers

    env.update(transformers=transformers.__version__, peft=peft.__version__)
    write_json(RES / "env.json", env)
    checks: dict = {}
    timing: list = []

    # 1. base: directions, frozen before any adapter is loaded
    model, info = load_model("base")
    checks["loads"] = [info]
    acts, norm_stats = extract(model, tok, prompts)
    vecs = P.build_vectors(acts, norm_stats[P.B_MID_LAYER]["mean"])
    drec = save_directions(vecs, acts, norm_stats)
    log(f"P02_DIRECTIONS_FROZEN sha256={drec['vectors_sha256']} at {drec['utc_frozen']}; "
        f"half-split medians {[round(drec['A_half_split_median'][L], 3) for L in P.A_LAYERS]}")
    checks["2_half_split"] = {"medians": {L: drec["A_half_split_median"][L] for L in P.A_LAYERS},
                              "passed": drec["A_half_split_pass"]}
    write_json(RES / "checks.json", checks)
    if not drec["A_half_split_pass"]:
        raise SmokeFail("check (2): A's half-split cosine < 0.7 at a steered layer")

    # 3. hook checks (on the base; the hooks do not depend on the weights)
    from emx.steer_multi import hook_checks_multi

    probe_cell = P.model_cells("base")[0]
    hreqs, hspans = reqs_for(probe_cell)
    hc = {}
    for name, cell in (("A", P.Cell("org", "A", 1.0, "ov", "plain", 1)),
                       ("B", P.Cell("org", "B", 0.1, "ov", "plain", 1)),
                       ("ctl_book", P.Cell("org", "ctl_book", 1.0, "ov", "plain", 1))):
        t = time.time()
        hc[name] = hook_checks_multi(model, [list(r.prompt_ids) for r in hreqs[:16]],
                                     P.cell_vectors(cell, vecs), list(hspans[:16]),
                                     max_new_tokens=MAX_NEW)
        hc[name]["wall_s"] = round(time.time() - t, 1)
        log(f"hook checks {name}: passed={hc[name]['passed']} ({hc[name]['wall_s']} s)")
    checks["3_hooks"] = hc
    write_json(RES / "checks.json", checks)
    if not all(h["passed"] for h in hc.values()):
        raise SmokeFail("check (3): a hook check failed")

    ref_ids = [list(r.prompt_ids) for r in hreqs[:4]]
    base_logits = last_logits(model, ref_ids)
    latents = {}

    def latent_for(m, name):
        by = {n: reqs_for(P.Cell(name, "none", 0.0, n, "plain", P.N_SAMPLES))
              for n in ("ov", "noov")}
        t = time.time()
        latents[name] = latent(m, name, tok, by, vecs)
        latents[name]["wall_s"] = round(time.time() - t, 1)
        write_json(RES / "latent.json", latents)
        log(f"latent {name}: auroc_last {[round(v, 3) for v in latents[name]['auroc_last'].values()]}")

    latent_for(model, "base")
    done = 0
    projected = False
    current = "base"
    for si, (sess, cells) in enumerate(plan):
        if sess != current:
            free(model)
            model, info = load_model(sess)
            checks["loads"].append(info)
            current = sess
            if si < 3:  # first load of org / good: check (1) and the latent readout
                diff = float(np.abs(last_logits(model, ref_ids) - base_logits).max())
                checks.setdefault("1_adapter_applied", {})[sess] = {
                    "max_abs_logit_diff_vs_base": diff, "passed": diff > 0.05}
                write_json(RES / "checks.json", checks)
                if diff <= 0.05:
                    raise SmokeFail(f"check (1): {sess} logits equal the base's (diff {diff})")
                latent_for(model, sess)
        for cell in cells:
            reqs, spans = reqs_for(cell)
            done += run_cell(model, tok, cell, reqs, spans, vecs, timing)
            if is_smoke(cell) and cell.model in ("org", "good", "base"):
                copy_smoke(cell)
            if not projected and timing:
                # check (5): project the whole run from the first full cell
                projected = True
                r = timing[-1]["answers_per_s"]
                remaining = total - done
                loads = 3 * 120  # later loads hit the local HF cache
                proj_s = (time.time() - T0) + remaining / r * 1.15 + loads
                budget_s = (gpu_hours() - 0.25) * 3600
                cut = proj_s > budget_s
                checks["5_projection"] = {"rate_per_s": r, "remaining_answers": remaining,
                                          "projected_total_s": round(proj_s),
                                          "budget_s": round(budget_s), "cut_nulls": cut}
                log(f"P02_PROJECTION {proj_s / 3600:.2f} h vs {budget_s / 3600:.2f} h; cut={cut}")
                if cut:
                    null_samples = P.NULL_SAMPLES_CUT
                    plan[3] = ("org", P.null_cells(null_samples))
                    log(f"P02_NULLS_CUT to {null_samples} samples per question")
                write_json(RES / "checks.json", checks)
    free(model)
    checks["null_samples"] = null_samples
    checks["utc_end"] = utc()
    checks["elapsed_s"] = round(time.time() - T0, 1)
    write_json(RES / "checks.json", checks)
    log(f"P02_ALL_CELLS_DONE answers={done} elapsed={checks['elapsed_s']} s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFail as e:
        log(f"P02_SMOKE_FAIL {e}")
        sys.exit(3)
