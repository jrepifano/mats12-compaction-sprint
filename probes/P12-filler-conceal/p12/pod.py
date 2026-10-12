"""P12 pod run: DeepSeek-V4-Flash on one B300, the arms of `p12.arms` (plan.md "Setup").

    python -m p12.pod download   # P06's F7/F8 download: the model @ its pin + both V4 lenses
    python -m p12.pod run        # load, gates, then per item every arm with and without filler

Reuses P06's V4 Flash path (probes/P06-filler-token-computation, F7–F8; imported, not copied):
`p06.v4pilot` (pinned model and lens revisions, `load`, `smoke_with_fixes`, `greedy`, `capture`,
`forward_read`, `lens_logits`, the download with retries and the snapshot ignore filter that fixed
F7's two failed attempts), `p06.v4bulk` (`render` of the varbind `Filler:` span, `JReader` and
`load_jreaders` for the J-Lens and J++ maps), `p06.encoding_dsv4` (the chat template). New here:
the arm prompts, the candidate ranks over the full vocabulary, the pooled filler residual and the
P12 gates. Layout: `p12.schema`.

Per item (data/items.json, in its seeded run order), per arm (P, C, D, M, CD, R):
- **filler 100:** greedy decode with the residual streams captured during the prefill over the
  read span (the `Filler:` label to the end; F7 showed the prefill equals the uncached forward),
  then the three readers at all 43 layers -> one npz; and an answers row.
- **no filler:** greedy decode only -> an answers row.

**Gates** (plan.md "Automatic rules"): before any generation, the arm sentences within 5 tokens of
each other and every candidate value a single token (`arms.token_checks`); P06's smoke with its
recorded fixes; lens-vs-model top-1 agreement >= 0.99 over the read span of the first item's P and
CD prompts (an uncached forward). After the first 10 items: under P, y the logit lens's top-1 at
some filler cell on >= 8 of 10. Any failure -> rc 3. **Stop rule:** P's filler uplift on the first
60 items < 10 points -> rc 5. **Stop** at OVERRUN x the projection (re-estimated after 6 items) or
at T0 + gpu_hours - 10 min -> rc 4. Exceptions -> rc 1 (results/error.txt). Reruns in the same
session skip rows already in answers.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
from p06 import encoding_dsv4 as E
from p06 import v4bulk as VB
from p06 import v4pilot as V
from p06.stage2 import Timer, append_jsonl, grade_int, utc, write_json

from . import PROBE
from . import arms as A
from . import schema as S

OUT = S.RESULTS
AGREE_MIN = 0.99
SMOKE_Y_MIN = 8
UPLIFT_MIN = 0.10
OVERRUN = 1.5
PROJ_UPDATE_AFTER = 6
SALVAGE_MARGIN_S = 10 * 60
# P06 F8 medians (results/v4bulk/run.json): varbind dots100 generate 0.93 s + readout 0.07 s +
# write 0.04 s; 10-option base generate 0.74 s. Ranks and the residual pool add a little.
PROJ_FILLER_S = 1.2
PROJ_NOFILL_S = 0.8


def log(msg: str) -> None:
    print(f"[p12.pod {utc()}] {msg}", flush=True)


# --------------------------------------------------------------------- prompts


def filler_spans(tok: Any, msgs: list[dict]) -> VB.Spans:
    """P06's varbind span (label `Filler:`, 100 spaced dots after it, then `\\n\\nAnswer:`)."""
    return VB.render(tok, msgs, "dots100")


def nofill_ids(tok: Any, msgs: list[dict]) -> list[int]:
    text = E.encode_messages(msgs, thinking_mode="chat")
    return [int(i) for i in tok.encode(text, add_special_tokens=False)]


# --------------------------------------------------------------------- readers


def reader_arrays(logits_fn, hs: Any, cand_ids: list[int], chunk: int = V.LENS_CHUNK):
    """hs [L, R, hc, D] -> (log-probs at cand_ids [L, R, n] fp16, top-1 [L, R] int32, ranks
    [L, R, n] int32: vocabulary logits strictly above the candidate's). As `p06.v4bulk.
    reader_arrays`, plus the ranks."""
    import torch

    idx = torch.tensor(cand_ids, device=hs.device)
    lps, tops, ranks = [], [], []
    with torch.inference_mode():
        for layer in range(hs.shape[0]):
            lp_l, top_l, rk_l = [], [], []
            for s in range(0, hs.shape[1], chunk):
                lg = logits_fn(layer, hs[layer, s:s + chunk]).float()
                lp_l.append(torch.log_softmax(lg, -1).index_select(-1, idx))
                top_l.append(lg.argmax(-1))
                cand = lg.index_select(-1, idx)
                rk_l.append(torch.stack([(lg > cand[:, j:j + 1]).sum(-1)
                                         for j in range(len(cand_ids))], -1))
            lps.append(torch.cat(lp_l))
            tops.append(torch.cat(top_l))
            ranks.append(torch.cat(rk_l))
    return (torch.stack(lps).cpu().numpy().astype(np.float16),
            torch.stack(tops).cpu().numpy().astype(np.int32),
            torch.stack(ranks).cpu().numpy().astype(np.int32))


def readouts(ctx: Any, jreaders: dict, hs: Any, cand_ids: list[int]) -> dict:
    out = {"logit_lens": reader_arrays(lambda layer, x: V.lens_logits(ctx.parts, x), hs,
                                       cand_ids)}
    for name, jr in jreaders.items():
        out[name] = reader_arrays(lambda layer, x, jr=jr: jr.logits(ctx.parts, x, layer), hs,
                                  cand_ids)
    return out


def resid_pool(hs: Any, sel: np.ndarray) -> np.ndarray:
    """[L, D] fp16: each layer's output, mean over the hc streams, averaged over rows `sel`."""
    import torch

    rows = hs.index_select(1, torch.as_tensor(sel, device=hs.device))
    return rows.float().mean(dim=(1, 2)).cpu().numpy().astype(np.float16)


def write_rec(path: Path, rec: dict) -> None:
    """Atomic compressed npz in `p12.schema`'s layout."""
    arrays = {k: np.asarray(v) for k, v in rec.items() if k != "ro"}
    for name, (lp, top, rank) in rec["ro"].items():
        arrays[f"{name}__lp"], arrays[f"{name}__top1"], arrays[f"{name}__rank"] = lp, top, rank
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        np.savez_compressed(f, **arrays)
    os.replace(tmp, path)


# --------------------------------------------------------------------- units of work


def cand_ids_for(tok: Any, item: dict) -> list[int]:
    """Single-token ids of the 12 candidates (repeats allowed when values coincide)."""
    ids = []
    for v in S.cand_values(item):
        enc = tok.encode(str(v), add_special_tokens=False)
        if len(enc) != 1 or tok.decode(enc) != str(v):
            raise ValueError(f"{item['item_id']}: {v} is not one token: {enc}")
        ids.append(int(enc[0]))
    return ids


def do_filler(ctx: Any, jreaders: dict, ex: dict, shots: list[dict], item: dict,
              arm: str) -> dict:
    sp = filler_spans(ctx.tok, A.messages(ex, shots, item, arm, A.N_DOTS))
    cand = cand_ids_for(ctx.tok, item)
    t0 = time.time()
    new, first, hs = VB.gen_capture(ctx, sp.ids, sp.read)
    V._sync()
    t1 = time.time()
    ro = readouts(ctx, jreaders, hs, cand)
    pool = resid_pool(hs, np.searchsorted(sp.read, sp.filler))
    del hs
    t2 = time.time()
    text = ctx.tok.decode(new, skip_special_tokens=True)
    write_rec(S.rec_path(item["idx"], arm), {
        "item_id": item["item_id"], "arm": arm, "idx": item["idx"],
        "answer": item["roles"]["answer"], "n_prompt": len(sp.ids),
        "cand_names": np.array(S.CAND_NAMES), "cand_values": np.array(S.cand_values(item),
                                                                       dtype=np.int64),
        "cand_ids": np.array(cand, dtype=np.int32), "positions": sp.read.astype(np.int32),
        "groups": sp.groups, "token_ids": np.array([sp.ids[p] for p in sp.read], dtype=np.int32),
        "gen_ids": np.array(new, dtype=np.int32), "gen_text": text,
        "readers": np.array(list(ro)), "resid_pool": pool, "ro": ro})
    lens_last = int(ro["logit_lens"][1][-1, -1])
    return {"gen_ids": new, "gen_text": text, "n_prompt": len(sp.ids),
            "final_agree": bool(new and lens_last == new[0] and first is not None
                                and int(first.argmax()) == new[0]),
            "gen_s": round(t1 - t0, 3), "read_s": round(t2 - t1, 3),
            "y_any_logit_lens": bool(_any_filler_hit(ro["logit_lens"][1], sp.groups,
                                                     cand[S.CAND_NAMES.index("y")]))}


def _any_filler_hit(top1: np.ndarray, groups: np.ndarray, tid: int) -> bool:
    return bool((top1[:, groups == S.GROUPS["filler"]] == tid).any())


def do_nofill(ctx: Any, ex: dict, shots: list[dict], item: dict, arm: str) -> dict:
    ids = nofill_ids(ctx.tok, A.messages(ex, shots, item, arm, 0))
    t0 = time.time()
    new, _, _ = V.greedy(ctx.model, ids, ctx.int_ids, ctx.device)
    V._sync()
    return {"gen_ids": new, "gen_text": ctx.tok.decode(new, skip_special_tokens=True),
            "n_prompt": len(ids), "gen_s": round(time.time() - t0, 3)}


def do_item(ctx: Any, jreaders: dict, ex: dict, shots: list[dict], item: dict,
            done: set) -> dict:
    """Every arm with and without filler; returns {(arm, filler): row} for the rows written now
    or already present (from `done`, which maps keys to rows)."""
    rows = {}
    for arm in A.ARMS:
        for k in A.FILLERS:
            key = (item["item_id"], arm, k)
            if key in done:
                rows[(arm, k)] = done[key]
                continue
            r = (do_filler(ctx, jreaders, ex, shots, item, arm) if k
                 else do_nofill(ctx, ex, shots, item, arm))
            g = grade_int(r["gen_text"], item["roles"]["answer"])
            row = {"item_id": item["item_id"], "idx": item["idx"], "order": item["order"],
                   "arm": arm, "filler": k, "answer": item["roles"]["answer"], **g, **r,
                   "utc": utc()}
            append_jsonl(S.ANSWERS, row)
            rows[(arm, k)] = row
    return rows


# --------------------------------------------------------------------- gates and run


def done_rows() -> dict:
    if not S.ANSWERS.exists():
        return {}
    out = {}
    for x in S.ANSWERS.read_text().splitlines():
        if x.strip():
            r = json.loads(x)
            out[(r["item_id"], r["arm"], r["filler"])] = r
    return out


def lens_agreement(ctx: Any, ex: dict, shots: list[dict], item: dict) -> dict:
    agree, n = 0, 0
    for arm in ("P", "CD"):
        sp = filler_spans(ctx.tok, A.messages(ex, shots, item, arm, A.N_DOTS))
        hs, logits = V.forward_read(ctx.model, ctx.parts, sp.ids, sp.read, ctx.device)
        lens = VB.reader_arrays(lambda layer, x: V.lens_logits(ctx.parts, x), hs[-1:],
                                ctx.int_ids)[1][0]
        a = V.agreement(lens, logits.argmax(-1).cpu().numpy())
        agree, n = agree + a["agree"], n + a["n"]
        del hs, logits
    rate = agree / n if n else None
    return {"agree": agree, "n": n, "rate": rate, "ok": rate is not None and rate >= AGREE_MIN}


def p_uplift(rows: list[dict]) -> tuple[float | None, int]:
    """P's accuracy with filler minus without, over items having both rows."""
    by: dict[str, dict[int, bool]] = {}
    for r in rows:
        if r["arm"] == "P":
            by.setdefault(r["item_id"], {})[r["filler"]] = bool(r["correct"])
    pairs = [v for v in by.values() if A.N_DOTS in v and 0 in v]
    if not pairs:
        return None, 0
    return (sum(v[A.N_DOTS] for v in pairs) - sum(v[0] for v in pairs)) / len(pairs), len(pairs)


def projection(n_items: int, item_s: float | None = None) -> float:
    per = item_s if item_s is not None else len(A.ARMS) * (PROJ_FILLER_S + PROJ_NOFILL_S)
    return n_items * per


def budget_deadline() -> float | None:
    t0 = os.environ.get("P12_T0")
    if not t0:
        return None
    gpu_h = float(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"])
    return float(t0) + gpu_h * 3600 - SALVAGE_MARGIN_S


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
    log(f"P12_RUN_DONE rc={rc}")
    return rc


def _run(a, st: dict) -> int:
    import torch

    T = Timer(OUT / "timings.json")
    items = A.load_items()
    if a.limit:
        items = items[: a.limit]
    by_idx, shots = A.examples_by_idx()
    st["plan"] = {"n_items": len(items), "arms": list(A.ARMS), "fillers": list(A.FILLERS),
                  "sentences_sha256": A.sentences_sha()}
    st["step"] = "load"
    with T.step("load") as rec:
        ctx, info = V.load()
        rec.update(info)
    st["load"] = info
    tc = A.token_checks(ctx.tok, items)
    st["token_checks"] = tc
    log(f"P12_GATE tokens len_spread={tc['max_len_spread']} tokens_ok={tc['tokens_ok']}")
    if not (tc["len_ok"] and tc["tokens_ok"]):
        st["stopped"] = "token checks failed"
        return 3
    lens_info: dict[str, Any] = {}
    jreaders = VB.load_jreaders(ctx.device, lens_info)
    st["lenses"] = lens_info
    st["readers"] = ["logit_lens", *jreaders]
    first, ex0 = items[0], by_idx[items[0]["idx"]]
    st["system_messages_first_item"] = {
        arm: A.messages(ex0, shots, first, arm, A.N_DOTS)[0]["content"] for arm in A.ARMS}
    st["step"] = "smoke_fixes"
    sp0 = filler_spans(ctx.tok, A.messages(ex0, shots, first, "P", A.N_DOTS))
    with T.step("smoke_fixes") as rec:
        sm = V.smoke_with_fixes(ctx, sp0.ids[:V.SMOKE_TOKENS])
        rec.update(fixes=[f["name"] for f in sm["fixes_applied"]])
    st["smoke_fixes"] = {"fixes_applied": sm["fixes_applied"], "attempts": len(sm["attempts"])}
    st["step"] = "smoke_agreement"
    with T.step("smoke_agreement") as rec:
        ag = lens_agreement(ctx, ex0, shots, first)
        rec.update(ag)
    st["smoke_agreement"] = ag
    log(f"P12_GATE lens_agreement={ag['rate']}")
    if not ag["ok"]:
        st["stopped"] = "lens agreement below the gate"
        return 3
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    done = done_rows()
    proj = projection(len(items))
    st["projection_s"] = {"initial": round(proj)}
    start = time.time()
    stop_at = start + OVERRUN * proj
    deadline = budget_deadline()
    item_s: list[float] = []
    all_rows: list[dict] = []
    smoke_hits: list[bool] = []
    stopped, rc = None, 0
    st["step"] = "bulk"
    for i, item in enumerate(items):
        if time.time() > stop_at:
            stopped, rc = f"{OVERRUN}x the projection", 4
        elif deadline is not None and time.time() > deadline:
            stopped, rc = "the gpu_hours deadline", 4
        if stopped:
            break
        t = time.time()
        rows = do_item(ctx, jreaders, by_idx[item["idx"]], shots, item, done)
        item_s.append(time.time() - t)
        all_rows.extend(rows.values())
        if i < A.N_SMOKE:
            smoke_hits.append(bool(rows[("P", A.N_DOTS)].get("y_any_logit_lens")))
        if i + 1 == A.N_SMOKE:
            st["smoke_y"] = {"hits": sum(smoke_hits), "n": len(smoke_hits)}
            log(f"P12_GATE smoke_y P hits {sum(smoke_hits)}/{len(smoke_hits)}")
            if sum(smoke_hits) < SMOKE_Y_MIN:
                stopped, rc = f"smoke: y top-1 under P on {sum(smoke_hits)}/10 < 8", 3
                break
        if i + 1 == A.N_STOP:
            up, n = p_uplift(all_rows)
            st["stop_rule"] = {"p_uplift": up, "n": n}
            log(f"P12_GATE p_uplift_first60={up} n={n}")
            if up is None or up < UPLIFT_MIN:
                stopped, rc = f"P uplift {up} < {UPLIFT_MIN} on the first {n} items", 5
                break
        if len(item_s) == PROJ_UPDATE_AFTER:
            med = statistics.median(item_s[1:])  # the first item carries the compile
            proj = time.time() - start + projection(len(items) - i - 1, med)
            stop_at = start + OVERRUN * proj
            st["projection_s"].update(updated=round(proj), item_median_s=round(med, 2))
            log(f"P12_PROJECTION {proj / 60:.1f} min (item median {med:.1f} s)")
        if (i + 1) % 10 == 0:
            log(f"items {i + 1}/{len(items)}, last {item_s[-1]:.1f} s")
    st["items_done"] = len(item_s)
    st["item_s_median"] = round(statistics.median(item_s), 2) if item_s else None
    if torch.cuda.is_available():
        st["mem_peak_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    st["accuracy_quick"] = accuracy_quick(all_rows)
    st.pop("step", None)
    if stopped:
        st["stopped"] = stopped
        log(f"P12_STOPPED {stopped}")
    return rc


def accuracy_quick(rows: list[dict]) -> dict:
    acc: dict[str, list[bool]] = {}
    for r in rows:
        acc.setdefault(f"{r['arm']}|{r['filler']}", []).append(bool(r["correct"]))
    return {k: {"n": len(v), "acc": round(sum(v) / len(v), 3)} for k, v in sorted(acc.items())}


def cmd_download(_a) -> int:
    try:
        return V._download(OUT)
    except BaseException as e:  # noqa: BLE001
        V.record_error("download", e, OUT)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p12.pod")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N items in run order (tests)")
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
