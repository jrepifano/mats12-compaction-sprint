"""P16 pod run: DeepSeek-V4-Flash on one B300, every arm of `p16.arms` (plan.md "Setup").

    python -m p16.pod download   # P06's F7/F8 download: the model @ its pin + both V4 lenses
    python -m p16.pod run        # load, gates, phases 1-4

Reuses P06's V4 Flash path (`p06.v4pilot`: load, smoke_with_fixes, greedy, capture,
forward_read, lens_logits; `p06.v4bulk`: render, JReader, load_jreaders) and P12's span and gate
helpers (`p12.pod`: filler_spans, cand_ids_for, resid_pool, lens_agreement), imported, not copied.

Phases (exact Brauer residuals, plan.md "Pod order"):
1. **P on every item:** turn 1 with the read span captured during the prefill; readers -> top-1
   and candidate log-probs at all 43 layers (P12's arrays), top-K over the full vocabulary at the
   filler cells of layers 22-42, and P's per-cell mean probability accumulated (logit lens,
   J-Lens); then turn 2 plain and prefilled, each with the readers' top-100 at the state that
   predicts the reply's first digit token. Smoke after 10 items (gates below).
2. **P re-read** (prefill only) against P's now-final mean: the residual top-30 per cell, exact.
3. **Every other arm per item** (M, D, CD, L, I, LD, plus extra_arms.txt, then F): as phase 1,
   with residuals against P's final mean computed in line and each arm's own mean accumulated.
   P12's 200 items run first, so a stop leaves complete arms on them.
4. **Own-mean residuals** from the stored top-100 (certified per cell); the means are saved.

**Gates** (rc 3): arm sentences within 5 tokens and every value one token; P06's smoke fixes;
lens-vs-model top-1 agreement >= 0.99 on the first item (P and CD prompts); after 10 P items:
turn-1 ids equal P12's on >= 9/10, y top-1 at some filler cell (logit lens) on >= 8/10, and y in
the aggregated top 50 against the 10-item mean on >= 7/10. **Stop** (rc 4) at OVERRUN x the
projection or at T0 + gpu_hours - margin (phase 4 and salvage keep their time). Exceptions -> rc
1 (results/error.txt). No resume: a rerun starts clean (the means live in memory).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from p06 import encoding_dsv4 as E
from p06 import v4bulk as VB
from p06 import v4pilot as V
from p06.stage2 import Timer, append_jsonl, grade_int, utc, write_json
from p12 import pod as P12
from p12 import schema as S12

from . import PROBE
from . import arms as A
from . import readers as R
from . import schema as S

OUT = S.RESULTS
AGREE_MIN = 0.99
SMOKE_N = 10
SMOKE_P12_MIN = 9
SMOKE_Y_MIN = 8
SMOKE_AGG_MIN = 7
OVERRUN = 1.5
STOP_MARGIN_S = 25 * 60  # salvage margin (10) + phase 4 and the means (~10) + slack
L0 = S.READ_LAYERS[0]
# Projection (plan.md "Cap"), per item: P ~2.9 s (turn 1 + two asks), each other arm the same,
# F ~0.75 s, P re-read ~0.6 s. The smoke re-times it.
PROJ_ARM_S = 2.9
PROJ_F_S = 0.75
PROJ_REREAD_S = 0.6
P12_ANSWERS = Path(S12.ANSWERS)


def log(msg: str) -> None:
    print(f"[p16.pod {utc()}] {msg}", flush=True)


# --------------------------------------------------------------------- readers on the GPU


class Means:
    """Per-arm, per-reader running sums of the softmax at [21 layers, 100 dots, V] (fp32)."""

    def __init__(self) -> None:
        self.sum: dict[tuple[str, str], Any] = {}
        self.n: dict[str, int] = {}

    def add(self, arm: str, reader: str, layer_i: int, probs: Any) -> None:
        import torch

        key = (arm, reader)
        if key not in self.sum:
            self.sum[key] = torch.zeros((len(S.READ_LAYERS), S.N_DOTS, probs.shape[-1]),
                                        dtype=torch.float32, device=probs.device)
        self.sum[key][layer_i] += probs

    def count(self, arm: str) -> None:
        self.n[arm] = self.n.get(arm, 0) + 1

    def mean(self, arm: str, reader: str) -> Any:
        return self.sum[(arm, reader)] / max(self.n.get(arm, 0), 1)


def read_prompt(ctx: Any, jreaders: dict, hs: Any, cand_ids: list[int], filler_rows: Any,
                arm: str, means: Means | None, ref: dict[str, Any] | None) -> dict:
    """hs [43, R, hc, D] over the read span. Returns numpy arrays: per reader top1 [43, R] and
    cand lp [43, R, 12]; per TOPK reader topk ids/lp [21, 100, K]; per RESID reader (when `ref`
    holds P's final means) the residual top-30 ids/score [21, 100, 30]. Adds the filler-cell
    softmax to `means` (when given) for the RESID readers."""
    import torch

    fns = {"logit_lens": lambda layer, x: V.lens_logits(ctx.parts, x)}
    for name, jr in jreaders.items():
        fns[name] = lambda layer, x, jr=jr: jr.logits(ctx.parts, x, layer)
    idx = torch.tensor(cand_ids, device=hs.device)
    rows = torch.as_tensor(filler_rows, device=hs.device)
    out: dict[str, np.ndarray] = {}
    with torch.inference_mode():
        for r, fn in fns.items():
            tops, lps, tk_i, tk_l, rs_i, rs_s = [], [], [], [], [], []
            k = S.TOPK.get(r)
            for layer in range(hs.shape[0]):
                lg = fn(layer, hs[layer]).float()
                lsm = torch.log_softmax(lg, -1)
                tops.append(lg.argmax(-1))
                lps.append(lsm.index_select(-1, idx))
                if layer < L0 or k is None:
                    continue
                f_lsm = lsm.index_select(0, rows)
                v, i = f_lsm.topk(k, dim=-1)
                tk_i.append(i)
                tk_l.append(v)
                if r in S.RESID_READERS:
                    probs = f_lsm.exp()
                    if means is not None:
                        means.add(arm, r, layer - L0, probs)
                    if ref is not None:
                        s, j = (probs - ref[r][layer - L0]).topk(S.RESID_K, dim=-1)
                        rs_i.append(j)
                        rs_s.append(s)
            out[f"{r}__top1"] = torch.stack(tops).cpu().numpy().astype(np.int32)
            out[f"{r}__lp"] = torch.stack(lps).cpu().numpy().astype(np.float16)
            if tk_i:
                out[f"{r}__topk_ids"] = torch.stack(tk_i).cpu().numpy().astype(np.int32)
                out[f"{r}__topk_lp"] = torch.stack(tk_l).cpu().numpy().astype(np.float16)
            if rs_i:
                out[f"{r}__res_ids"] = torch.stack(rs_i).cpu().numpy().astype(np.int32)
                out[f"{r}__res_score"] = torch.stack(rs_s).cpu().numpy().astype(np.float32)
    return out


def read_position(ctx: Any, jreaders: dict, hs: Any, k: int = 100) -> dict[str, np.ndarray]:
    """hs [43, 1, hc, D] at one position -> per reader top-k ids/lp [43, k]."""
    import torch

    fns = {"logit_lens": lambda layer, x: V.lens_logits(ctx.parts, x)}
    for name, jr in jreaders.items():
        fns[name] = lambda layer, x, jr=jr: jr.logits(ctx.parts, x, layer)
    out = {}
    with torch.inference_mode():
        for r, fn in fns.items():
            ids, lps = [], []
            for layer in range(hs.shape[0]):
                v, i = torch.log_softmax(fn(layer, hs[layer]).float(), -1)[0].topk(k)
                ids.append(i)
                lps.append(v)
            out[f"{r}__ids"] = torch.stack(ids).cpu().numpy().astype(np.int32)
            out[f"{r}__lp"] = torch.stack(lps).cpu().numpy().astype(np.float16)
    return out


# --------------------------------------------------------------------- generation units


def encode(tok: Any, msgs: list[dict], suffix: str = "") -> list[int]:
    text = E.encode_messages(msgs, thinking_mode="chat") + suffix
    return [int(i) for i in tok.encode(text, add_special_tokens=False)]


def generate(ctx: Any, ids: list[int], max_new: int, capture_last: bool):
    """Greedy decode; with capture_last, every layer's stream at the last prompt position from
    the prefill ([43, 1, hc, D])."""
    import torch

    if not capture_last:
        new, _, _ = V.greedy(ctx.model, ids, ctx.int_ids, ctx.device, max_new=max_new)
        return new, None
    pos = torch.as_tensor([len(ids) - 1], device=ctx.device)
    with V.capture(ctx.parts.blocks, pos, len(ids)) as hs:
        new, _, _ = V.greedy(ctx.model, ids, ctx.int_ids, ctx.device, max_new=max_new)
    if any(h is None for h in hs):
        raise RuntimeError("a decoder-layer hook captured nothing")
    return new, torch.stack(hs)


def truncated(ctx: Any, new: list[int], max_new: int) -> bool:
    return len(new) >= max_new and new[-1] != ctx.tok.eos_token_id


def do_ask(ctx: Any, jreaders: dict, ids: list[int], item: dict, kind: str) -> tuple[dict, dict]:
    """One turn-2 generation; returns (answers row fields, npz arrays). The answer-position
    state is the last prompt position when the first new token holds a digit, else a forward
    pass over the prompt plus the generated prefix up to that token."""
    t0 = time.time()
    new, hs_last = generate(ctx, ids, A.MAX_NEW_ASK, capture_last=True)
    V._sync()
    gen_s = time.time() - t0
    pieces = [ctx.tok.decode([t]) for t in new]
    text = ctx.tok.decode(new, skip_special_tokens=True)
    j = R.first_digit_pos(pieces)
    arr: dict[str, Any] = {f"ask_{kind}__gen_ids": np.array(new, dtype=np.int32),
                           f"ask_{kind}__text": text, f"ask_{kind}__ans_pos": j}
    if j >= 0:
        if j == 0:
            hs = hs_last
        else:
            pre = ids + new[:j]
            hs, _ = V.forward_read(ctx.model, ctx.parts, pre, np.array([len(pre) - 1]),
                                   ctx.device)
        for k, v in read_position(ctx, jreaders, hs).items():
            arr[f"ask_{kind}__{k}"] = v
    y = item["roles"]["y"]
    pred = R.first_int(text)
    row = {"kind": f"ask_{kind}", "target": y, "gen_text": text, "gen_ids": new,
           "n_new": len(new), "truncated": truncated(ctx, new, A.MAX_NEW_ASK), "predicted": pred,
           "correct": pred == y, "ans_pos": j, "n_prompt": len(ids), "gen_s": round(gen_s, 3)}
    return row, arr


def do_filler_arm(ctx: Any, jreaders: dict, ex: dict, shots: list[dict], item: dict, arm: str,
                  means: Means | None, ref: dict | None) -> tuple[list[dict], dict, dict]:
    """Turn 1 with readouts, then both asks. Returns (answers rows, readout record, residual
    record or {})."""
    msgs = A.messages(ex, shots, item, arm)
    sp = P12.filler_spans(ctx.tok, msgs)
    cand = P12.cand_ids_for(ctx.tok, item)
    t0 = time.time()
    new, _first, hs = VB.gen_capture(ctx, sp.ids, sp.read)
    V._sync()
    t1 = time.time()
    rows_f = np.searchsorted(sp.read, sp.filler)
    ro = read_prompt(ctx, jreaders, hs, cand, rows_f, arm, means, ref)
    pool = P12.resid_pool(hs, rows_f)
    del hs
    if means is not None:
        means.count(arm)
    t2 = time.time()
    reply = ctx.tok.decode(new, skip_special_tokens=True).strip()
    g = grade_int(reply, item["roles"]["answer"])
    rows = [{"kind": "turn1", "target": item["roles"]["answer"], "gen_text": reply,
             "gen_ids": new, "n_new": len(new), "truncated": truncated(ctx, new, V.MAX_NEW),
             **g, "n_prompt": len(sp.ids), "gen_s": round(t1 - t0, 3),
             "read_s": round(t2 - t1, 3)}]
    rec: dict[str, Any] = {
        "item_id": item["item_id"], "arm": arm, "idx": item["idx"], "n_prompt": len(sp.ids),
        "positions": sp.read.astype(np.int32), "groups": sp.groups,
        "token_ids": np.array([sp.ids[p] for p in sp.read], dtype=np.int32),
        "gen_ids": np.array(new, dtype=np.int32), "gen_text": reply,
        "cand_names": np.array(S12.CAND_NAMES),
        "cand_values": np.array(S12.cand_values(item), dtype=np.int64),
        "cand_ids": np.array(cand, dtype=np.int32), "resid_pool": pool,
        **{k: v for k, v in ro.items() if "__res_" not in k}}
    resid = {k.replace("__res_", "__"): v for k, v in ro.items() if "__res_" in k}
    turn2 = A.ask_messages(msgs, reply, item)
    for kind, suffix in (("plain", ""), ("pre", A.prefill(item))):
        row, arr = do_ask(ctx, jreaders, encode(ctx.tok, turn2, suffix), item, kind)
        rows.append(row)
        rec.update(arr)
    return rows, rec, resid


def do_fresh(ctx: Any, ex: dict, shots: list[dict], item: dict) -> dict:
    ids = encode(ctx.tok, A.messages(ex, shots, item, "F"))
    t0 = time.time()
    new, _ = generate(ctx, ids, A.MAX_NEW_ASK, capture_last=False)
    V._sync()
    text = ctx.tok.decode(new, skip_special_tokens=True)
    y = item["roles"]["y"]
    pred = R.first_int(text)
    return {"kind": "fresh", "target": y, "gen_text": text, "gen_ids": new, "n_new": len(new),
            "truncated": truncated(ctx, new, A.MAX_NEW_ASK), "predicted": pred,
            "correct": pred == y, "n_prompt": len(ids), "gen_s": round(time.time() - t0, 3)}


def reread_resid(ctx: Any, jreaders: dict, ex: dict, shots: list[dict], item: dict, arm: str,
                 ref: dict) -> tuple[dict, np.ndarray]:
    """Prefill-only pass over the arm's turn-1 prompt -> (residual record against `ref`, logit
    lens top-1 [43, R] for the consistency check)."""
    sp = P12.filler_spans(ctx.tok, A.messages(ex, shots, item, arm))
    hs, _ = V.forward_read(ctx.model, ctx.parts, sp.ids, sp.read, ctx.device)
    ro = read_prompt(ctx, jreaders, hs, P12.cand_ids_for(ctx.tok, item),
                     np.searchsorted(sp.read, sp.filler), arm, None, ref)
    del hs
    return ({k.replace("__res_", "__"): v for k, v in ro.items() if "__res_" in k},
            ro["logit_lens__top1"])


# --------------------------------------------------------------------- writing


class Writer:
    """Compressed npz writes on a small thread pool, so the GPU doesn't wait on zlib."""

    def __init__(self, workers: int = 3) -> None:
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.futs: list[Any] = []

    def npz(self, path: Path, arrays: dict) -> None:
        self.futs.append(self.pool.submit(_write_npz, path, arrays))
        for f in [f for f in self.futs if f.done()]:
            f.result()  # re-raises a failed write here
        self.futs = [f for f in self.futs if not f.done()]

    def drain(self) -> None:
        for f in self.futs:
            f.result()
        self.futs = []

    def close(self) -> None:
        self.drain()
        self.pool.shutdown(wait=True)


def _write_npz(path: Path, arrays: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        np.savez_compressed(f, **{k: np.asarray(v) for k, v in arrays.items()})
    os.replace(tmp, path)


def write_row(item: dict, arm: str, row: dict) -> dict:
    full = {"item_id": item["item_id"], "idx": item["idx"], "order": item["order"],
            "block": item["block"], "arm": arm, **row, "utc": utc()}
    append_jsonl(S.ANSWERS, full)
    return full


# --------------------------------------------------------------------- gates


def p12_turn1() -> dict[str, list[int]]:
    out = {}
    if P12_ANSWERS.exists():
        for line in P12_ANSWERS.read_text().splitlines():
            r = json.loads(line)
            if r["arm"] == "P" and r["filler"] == A.N_DOTS:
                out[r["item_id"]] = r["gen_ids"]
    return out


def smoke_agg(ctx: Any, jreaders: dict, by_idx: dict, shots: list[dict], items: list[dict],
              means: Means, ivocab: dict[int, int]) -> dict:
    """y among the aggregated top 50 (logit lens, against the running P mean) per smoke item."""
    ref = {r: means.mean("P", r) for r in S.RESID_READERS}
    hits = []
    for it in items:
        res, _ = reread_resid(ctx, jreaders, by_idx[it["idx"]], shots, it, "P", ref)
        agg = R.aggregate(res["logit_lens__ids"], res["logit_lens__score"], S.AGG_K)
        hits.append(R.hit_at(R.numeric_ranking(agg, ivocab), it["roles"]["y"], S.AGG_K))
    return {"hits": sum(hits), "n": len(hits)}


def budget_deadline() -> float | None:
    t0 = os.environ.get("P16_T0")
    if not t0:
        return None
    gpu_h = float(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"])
    return float(t0) + gpu_h * 3600 - STOP_MARGIN_S


EXTRA_ARMS = PROBE / "extra_arms.txt"  # committed when the API pilot's rule adds L2 (log.md)


def phase3_arms(path: Path = EXTRA_ARMS) -> tuple[str, ...]:
    extra = tuple(path.read_text().split()) if path.exists() else ()
    bad = [a for a in extra if a not in A.OPTIONAL_ARMS]
    if bad:
        raise ValueError(f"{path.name}: unknown arms {bad}")
    return (*(a for a in A.FILLER_ARMS if a != "P"), *extra)


# --------------------------------------------------------------------- run


def cmd_run(a) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    st: dict[str, Any] = {"env": V.env_info(), "utc_start": utc(), "step": "setup"}
    rc = 1
    try:
        rc = _run(a, st)
    except BaseException as e:  # noqa: BLE001 - recorded; run.json is still written
        V.record_error(st.get("step", "run"), e, OUT)
        st["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        rc = 1
    finally:
        st["utc_end"] = utc()
        st["rc"] = rc
        write_json(S.RUN_JSON, st)
    log(f"P16_RUN_DONE rc={rc}")
    return rc


def _run(a, st: dict) -> int:
    import torch

    T = Timer(OUT / "timings.json")
    items = A.load_items()
    if a.limit:
        items = items[: a.limit]
    by_idx, shots = A.examples_by_idx()
    arms3 = phase3_arms()
    st["plan"] = {"n_items": len(items), "phase3_arms": list(arms3),
                  "sentences_sha256": A.sentences_sha()}
    st["step"] = "load"
    with T.step("load") as rec:
        ctx, info = V.load()
        rec.update(info)
    st["load"] = info
    tc = A.token_checks(ctx.tok, items, ("P", *arms3))
    st["token_checks"] = tc
    log(f"P16_GATE tokens len_spread={tc['max_len_spread']} tokens_ok={tc['tokens_ok']}")
    if not (tc["len_ok"] and tc["tokens_ok"]):
        st["stopped"] = "token checks failed"
        return 3
    ivocab = R.int_vocab(ctx.tok)
    write_json(S.INT_VOCAB, {str(k): v for k, v in ivocab.items()})
    lens_info: dict[str, Any] = {}
    jreaders = VB.load_jreaders(ctx.device, lens_info)
    st["lenses"] = lens_info
    st["readers"] = ["logit_lens", *jreaders]
    if "jlens" not in jreaders:
        st["stopped"] = "the J-Lens (the decision reader) did not load"
        return 3
    first, ex0 = items[0], by_idx[items[0]["idx"]]
    st["system_messages_first_item"] = {
        arm: A.messages(ex0, shots, first, arm)[0]["content"] for arm in ("P", *arms3, "F")}
    st["step"] = "smoke_fixes"
    sp0 = P12.filler_spans(ctx.tok, A.messages(ex0, shots, first, "P"))
    with T.step("smoke_fixes") as rec:
        sm = V.smoke_with_fixes(ctx, sp0.ids[:V.SMOKE_TOKENS])
        rec.update(fixes=[f["name"] for f in sm["fixes_applied"]])
    st["smoke_fixes"] = {"fixes_applied": sm["fixes_applied"], "attempts": len(sm["attempts"])}
    st["step"] = "smoke_agreement"
    with T.step("smoke_agreement") as rec:
        ag = P12.lens_agreement(ctx, ex0, shots, first)
        rec.update(ag)
    st["smoke_agreement"] = ag
    log(f"P16_GATE lens_agreement={ag['rate']}")
    if not ag["ok"]:
        st["stopped"] = "lens agreement below the gate"
        return 3
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    W = Writer()
    means = Means()
    ref_p12 = p12_turn1()
    deadline = budget_deadline()
    n = len(items)
    proj = n * (PROJ_ARM_S * (1 + len(arms3)) + PROJ_F_S + PROJ_REREAD_S)
    st["projection_s"] = {"initial": round(proj)}
    start = time.time()
    stop_at = start + OVERRUN * proj
    stopped, rc = None, 0

    def over() -> str | None:
        if time.time() > stop_at:
            return f"{OVERRUN}x the projection"
        if deadline is not None and time.time() > deadline:
            return "the gpu_hours deadline"
        return None

    try:
        # ---- phase 1: P on every item
        st["step"] = "phase1"
        p_s: list[float] = []
        sm_p12, sm_y = [], []
        for i, item in enumerate(items):
            if (stopped := over()):
                rc = 4
                break
            t = time.time()
            ex = by_idx[item["idx"]]
            rows, rec, _ = do_filler_arm(ctx, jreaders, ex, shots, item, "P", means, None)
            for r in rows:
                write_row(item, "P", r)
            W.npz(S.READOUTS / S.rec_name(item["idx"], "P"), rec)
            p_s.append(time.time() - t)
            if i < SMOKE_N:
                want = ref_p12.get(item["item_id"])
                sm_p12.append(want is not None and list(rec["gen_ids"]) == list(want))
                lay = rec["logit_lens__topk_ids"]
                sm_y.append(R.any_cell(lay, ivocab, item["roles"]["y"]))
            if i + 1 == min(SMOKE_N, n):
                g = {"p12_turn1": {"hits": sum(sm_p12), "n": len(sm_p12)},
                     "y_any_cell": {"hits": sum(sm_y), "n": len(sm_y)}}
                g["agg_top50"] = smoke_agg(ctx, jreaders, by_idx, shots, items[:SMOKE_N],
                                           means, ivocab)
                st["smoke"] = g
                log(f"P16_GATE smoke {json.dumps(g)}")
                need = min(SMOKE_N, n)
                fails = [k for k, m in (("p12_turn1", SMOKE_P12_MIN), ("y_any_cell", SMOKE_Y_MIN),
                                        ("agg_top50", SMOKE_AGG_MIN))
                         if g[k]["hits"] < m * need / SMOKE_N]
                if fails:
                    stopped, rc = f"smoke gate failed: {fails}", 3
                    break
                med = statistics.median(p_s[1:]) if len(p_s) > 1 else p_s[0]
                proj = time.time() - start + (n - i - 1) * med + n * med * len(arms3) + n * (
                    PROJ_F_S + PROJ_REREAD_S)
                stop_at = start + OVERRUN * proj
                st["projection_s"].update(after_smoke=round(proj), p_item_median_s=round(med, 2))
                log(f"P16_PROJECTION {proj / 60:.1f} min (P item median {med:.1f} s)")
            if (i + 1) % 25 == 0:
                log(f"phase1 {i + 1}/{n}, last {p_s[-1]:.1f} s")
        st["phase1"] = {"items": len(p_s), "item_s_median": round(statistics.median(p_s), 2)
                        if p_s else None}
        if stopped:
            return rc

        # ---- phase 2: P re-read against P's final mean
        st["step"] = "phase2"
        ref = {r: means.mean("P", r) for r in S.RESID_READERS}
        save_means(ref, means, "P", full=True)
        W.drain()
        agree = []
        with T.step("phase2") as rec2:
            for item in items:
                if (stopped := over()):
                    rc = 4
                    break
                res, top1 = reread_resid(ctx, jreaders, by_idx[item["idx"]], shots, item, "P",
                                         ref)
                W.npz(S.RESID / S.rec_name(item["idx"], "P"), res)
                if len(agree) < 20:
                    with np.load(S.READOUTS / S.rec_name(item["idx"], "P")) as z:
                        agree.append(float((z["logit_lens__top1"] == top1).mean()))
            rec2.update(n=len(items), top1_agreement_first20=agree)
        st["phase2"] = {"reread_top1_agreement_first20_min": min(agree) if agree else None}
        log(f"P16_PHASE2 done; re-read top-1 agreement min {st['phase2']}")
        if stopped:
            return rc

        # ---- phase 3: every other arm per item
        st["step"] = "phase3"
        item_s: list[float] = []
        for i, item in enumerate(items):
            if (stopped := over()):
                rc = 4
                break
            t = time.time()
            ex = by_idx[item["idx"]]
            for arm in arms3:
                rows, rec, res = do_filler_arm(ctx, jreaders, ex, shots, item, arm, means, ref)
                for r in rows:
                    write_row(item, arm, r)
                W.npz(S.READOUTS / S.rec_name(item["idx"], arm), rec)
                W.npz(S.RESID / S.rec_name(item["idx"], arm), res)
            write_row(item, "F", do_fresh(ctx, ex, shots, item))
            item_s.append(time.time() - t)
            if len(item_s) == 3:
                med = statistics.median(item_s)
                proj = time.time() - start + (n - i - 1) * med
                stop_at = start + OVERRUN * proj
                st["projection_s"].update(phase3=round(proj), phase3_item_s=round(med, 2))
                log(f"P16_PROJECTION phase3 {proj / 60:.1f} min (item {med:.1f} s); deadline "
                    f"{'none' if deadline is None else round((deadline - start) / 60, 1)} min")
            if (i + 1) % 10 == 0:
                log(f"phase3 {i + 1}/{n}, last {item_s[-1]:.1f} s")
        st["phase3"] = {"items": len(item_s), "item_s_median": round(statistics.median(item_s), 2)
                        if item_s else None}
        W.drain()

        # ---- phase 4: own-mean residuals and the means
        st["step"] = "phase4"
        with T.step("phase4") as rec4:
            cert = own_mean_phase(items[: len(p_s)], items[: len(item_s)], ("P", *arms3),
                                  means, W)
            rec4.update(certified=cert)
        st["phase4"] = {"certified_fraction": cert}
        for arm in arms3:
            if means.n.get(arm):
                save_means({r: means.mean(arm, r) for r in S.RESID_READERS}, means, arm,
                           full=False)
    finally:
        W.close()
        st["step_last"] = st.get("step")
        if torch.cuda.is_available():
            st["mem_peak_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
        st["accuracy_quick"] = accuracy_quick()
    st.pop("step", None)
    if stopped:
        st["stopped"] = stopped
        log(f"P16_STOPPED {stopped}")
    return rc


def save_means(ref: dict, means: Means, arm: str, full: bool) -> None:
    import torch

    S.MEANS.mkdir(parents=True, exist_ok=True)
    for r, m in ref.items():
        np.save(S.MEANS / f"{arm}__{r}__dotmean.npy", m.mean(dim=1).cpu().numpy())
        if full:
            np.save(S.MEANS / f"{arm}__{r}__logmean.npy",
                    torch.log(m.clamp_min(1e-30)).half().cpu().numpy())
    write_json(S.MEANS / f"{arm}__count.json", {"n": means.n.get(arm, 0)})


def own_mean_phase(items_p: list[dict], items3: list[dict], arms: tuple[str, ...], means: Means,
                   W: Writer) -> dict:
    """Residual top-30 against each arm's own mean, from the stored top-100 (certified)."""
    import torch

    frac: dict[str, float] = {}
    for arm in arms:
        if not means.n.get(arm):
            continue
        todo = items_p if arm == "P" else items3
        mean = {r: means.mean(arm, r) for r in S.RESID_READERS}
        ok = tot = 0
        for item in todo:
            path = S.READOUTS / S.rec_name(item["idx"], arm)
            if not path.exists():
                continue
            out = {}
            with np.load(path) as z:
                for r in S.RESID_READERS:
                    ids = z[f"{r}__topk_ids"]
                    m_at = torch.gather(mean[r], 2, torch.as_tensor(ids, device=mean[r].device,
                                                                    dtype=torch.int64))
                    i3, s3, c = R.own_mean_resid(ids, z[f"{r}__topk_lp"], m_at.cpu().numpy(),
                                                 S.RESID_K)
                    out.update({f"{r}__ids": i3.astype(np.int32),
                                f"{r}__score": s3.astype(np.float32), f"{r}__certified": c})
                    ok, tot = ok + int(c.sum()), tot + c.size
            W.npz(S.RESID_OWN / S.rec_name(item["idx"], arm), out)
        frac[arm] = round(ok / tot, 5) if tot else None
    W.drain()
    return frac


def accuracy_quick() -> dict:
    acc: dict[str, list[bool]] = {}
    if S.ANSWERS.exists():
        for line in S.ANSWERS.read_text().splitlines():
            r = json.loads(line)
            acc.setdefault(f"{r['arm']}|{r['kind']}", []).append(bool(r["correct"]))
    return {k: {"n": len(v), "acc": round(sum(v) / len(v), 3)} for k, v in sorted(acc.items())}


def cmd_download(_a) -> int:
    try:
        return V._download(OUT)
    except BaseException as e:  # noqa: BLE001
        V.record_error("download", e, OUT)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p16.pod")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N items in run order (tests)")
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
