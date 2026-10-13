"""P24 pod run (GPU): filler K/V patches from donors that fix x, fix y, or flip the operator.

    python -m p24.pod download     # Gemma 4 31B-it @ p06.stage2.MODEL_REV (no lenses)
    python -m p24.pod run          # load, smoke (gates the bulk), bulk; resumable

One H100 80GB, bf16, sdpa, HF transformers in /ws/venv-i1a (P06 F3's stack). Model loading,
prompt rendering (`render`) and the K/V capture are P06's (`p06.stage2`), imported.

**Per item** (`do_item`), all rows the same token length, in calls of exactly `bs` rows (short
calls are filled with copies of the unpatched target), so every row sees the same kernels:
1. The unpatched target (`ref`) and donors X, Y, S, F, C, P (`p24.donors`), decoded greedily,
   with hooks capturing every k_proj/v_proj output at the target turn's `Filler` + `:` label and
   100 dots (the "union" slots, 102 in order). The random donor R has another length: one row of
   its own, captured at its own label and dots (slot for slot).
2. Patched rows: one per (donor, site), plus a self-patch (the target's own capture at every
   union slot; must equal `ref`). With bs > 1 each call starts with an unpatched reference row.
   A patch overwrites the target's k_proj/v_proj outputs at the site's positions with the
   donor's, at every layer, during the prefill (`inject_rows`); decode steps read the patched
   cache (P06 F3's method, not a completed-cache splice).

**Numerics** (plan review r1, BLOCKING 2). bs = 1 is the sequential path. The smoke runs each
smoke item batched, sequentially and batched in reversed row order; if the batched patched
rows agree with both on < AGREE_MIN of rows, or a batched smoke item is unstable, the whole run
uses bs = 1. In the bulk, an unstable batched item (self-patch or a batch reference differs
from the unpatched output) is rerun sequentially and that record is kept (`run_item`).

**Passes.** Pass 1 = the primary site (the dots) for every item (results/rows.jsonl); pass 2 =
the secondary sites (label, first 20 dots, label + dots) in the same item order while the
budget lasts (results/rows_sites.jsonl). Resume key: idx, per file.

**Smoke** (the first SMOKE_ITEMS items, kept as data): donors differ from the target only at
digits, plus/minus or three/four (`check_diff`; R is exempt); the sequential self-patch equals
the unpatched output and the sequential references agree; every injection fires once per module
per call; the bulk fits the budget (cut to the items that fit, at least MIN_BULK, else stop). A
failed smoke returns 3; pass 1 stops itself at OVERRUN x its projection and returns 4.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
from p06 import brauer as B
from p06 import stage2 as S2

from . import donors as D

PROBE = Path(__file__).resolve().parents[1]
# follow-up F2 (log.md) runs with P24_RESULTS=results/f2_eager and P24_ATTN=eager
RESULTS = PROBE / os.environ.get("P24_RESULTS", "results")
ROWS = RESULTS / "rows.jsonl"  # pass 1: PRIMARY_SITES
ROWS2 = RESULTS / "rows_sites.jsonl"  # pass 2: SECONDARY_SITES
TIMINGS = RESULTS / "timings.json"
ARM = "dots100"
PRIMARY_SITES = ("dots",)
SECONDARY_SITES = ("label", "early20", "all")
AGREE_MIN = 0.98  # batched vs sequential (and reversed) patched-row agreement
BATCH = int(os.environ.get("P24_BATCH", "8"))
SMOKE_ITEMS = 5
MIN_BULK = 250
SETUP_ALLOWANCE_S = 25 * 60  # boot, push, venv setup, download and load before `run` starts
OVERRUN = 1.5
ALLOWED_DIFF = set("0123456789") | {"plus", "minus", "three", "four"}


def log(msg: str) -> None:
    print(f"[p24.pod {S2.utc()}] {msg}", flush=True)


# --------------------------------------------------------------------- items and positions


def load_items() -> tuple[list[dict], list[dict], list[dict]]:
    """(items with X, Y and S donors, by idx; Brauer's 5 shots; the whole set as R's pool)."""
    exs, shots = B.load(1)
    exs = sorted(exs, key=lambda e: e["idx"])
    items = [e for e in exs if all(k in D.donors(e) for k in ("X", "Y", "S"))]
    return items, shots, exs


def union_positions(tok: Any, r: S2.Rendered) -> np.ndarray:
    """The target turn's `Filler` and `:` tokens, then its 100 dots (102 positions, in order)."""
    d0 = int(r.dots[0])
    label = [d0 - 2, d0 - 1]
    got = [tok.decode([r.ids[p]]) for p in label]
    if got != ["Filler", ":"]:
        raise ValueError(f"label tokens {got!r}, expected ['Filler', ':']")
    return np.array(label + r.dots.tolist(), dtype=np.int64)


def site_slots(site: str) -> list[int]:
    """Union-slot indices of a patch site (slots 0-1 the label, 2-101 the dots)."""
    return {"dots": list(range(2, 102)), "label": [0, 1], "early20": list(range(2, 22)),
            "all": list(range(102))}[site]


def check_diff(tok: Any, target: list[int], donor: list[int], kind: str) -> list[int]:
    """Positions where the donor's ids differ from the target's; each pair of tokens must be
    digits, plus/minus or three/four (y's definition line or x's literal)."""
    if len(target) != len(donor):
        raise ValueError(f"{kind}: donor length {len(donor)} != target {len(target)}")
    diff = [i for i, (a, b) in enumerate(zip(target, donor, strict=True)) if a != b]
    if not diff or len(diff) > 4:
        raise ValueError(f"{kind}: {len(diff)} differing tokens")
    for i in diff:
        pair = {tok.decode([target[i]]).strip(), tok.decode([donor[i]]).strip()}
        if not all(p in ALLOWED_DIFF or p.isdigit() for p in pair):
            raise ValueError(f"{kind}: unexpected differing tokens {pair} at {i}")
    return diff


# --------------------------------------------------------------------- hooks


@contextmanager
def capture_all(mods: Any, positions: Any, seq_len: int):
    """Like p06.stage2.capture, but keeps every batch row: out[i] = [B, P, dim]."""
    out: list[Any] = [None] * len(mods)

    def hook_for(i: int):
        def fn(_mod, _inp, o):
            h = o[0] if isinstance(o, tuple) else o
            if h.shape[1] == seq_len:
                out[i] = h.index_select(1, positions).detach().clone()
        return fn

    handles = [m.register_forward_hook(hook_for(i)) for i, m in enumerate(mods)]
    try:
        yield out
    finally:
        for h in handles:
            h.remove()


@contextmanager
def inject_rows(mods: Any, rows: Any, tpos: Any, src: Any, slot: Any, stored: list[Any],
                seq_len: int):
    """Forward hooks that, on full-sequence calls (the prefill), set each module's output
    o[rows[j], tpos[j]] = stored[i][src[j], slot[j]] for every j; yields per-module fire counts."""
    fired = [0] * len(mods)

    def hook_for(i: int):
        def fn(_mod, _inp, o):
            if o.shape[1] != seq_len:
                return None
            o = o.clone()
            if len(rows):
                o[rows, tpos] = stored[i][src, slot].to(o.dtype)
            fired[i] += 1
            return o
        return fn

    handles = [m.register_forward_hook(hook_for(i)) for i, m in enumerate(mods)]
    try:
        yield fired
    finally:
        for h in handles:
            h.remove()


def plan_patches(specs: list[tuple[int, list[int]]], union: np.ndarray):
    """[(src row, union slots)] per batch row (src -1 = unpatched) -> flat (rows, tpos, src,
    slot) index arrays for inject_rows."""
    rows, tpos, src, slot = [], [], [], []
    for r, (s, slots) in enumerate(specs):
        if s < 0:
            continue
        rows += [r] * len(slots)
        tpos += [int(union[k]) for k in slots]
        src += [s] * len(slots)
        slot += slots
    return rows, tpos, src, slot


# --------------------------------------------------------------------- generation


def generate(ctx: S2.Ctx, batch: list[list[int]]):
    """Greedy decode of equal-length rows: (texts, preds, first-step digit log-probs [B, 10])."""
    import torch

    t = torch.tensor(batch, device=ctx.device)
    with torch.inference_mode():
        out = ctx.model.generate(input_ids=t, attention_mask=torch.ones_like(t),
                                 max_new_tokens=S2.MAX_NEW, do_sample=False, output_logits=True,
                                 return_dict_in_generate=True)
        didx = torch.tensor(ctx.digit_ids, device=ctx.device)
        lp0 = torch.log_softmax(out.logits[0].float(), -1).index_select(-1, didx).cpu().numpy()
    n = t.shape[1]
    texts = [ctx.tok.decode(s[n:].tolist(), skip_special_tokens=True) for s in out.sequences]
    preds = [S2.grade_int(x, 0)["predicted"] for x in texts]
    return texts, preds, lp0


def pad(rows: list, fill: Any, bs: int) -> list:
    return rows + [fill] * (bs - len(rows))


def do_item(ctx: S2.Ctx, ex: dict, shots: list[dict], pool: list[dict], sites: tuple[str, ...],
            bs: int = BATCH, reverse: bool = False) -> dict:
    """All rows for one item at the given patch sites, in batches of exactly `bs` rows (bs = 1
    is the sequential path every batched result is checked against). `reverse` reverses the
    order of the patched rows (a batch-position check). Returns the rows.jsonl record."""
    import torch

    t0 = time.time()
    tok, mods = ctx.tok, ctx.kv
    tgt = S2.render(tok, ex, shots, ARM)
    union = union_positions(tok, tgt)
    upos = torch.as_tensor(union, device=ctx.device)
    n = len(tgt.ids)
    dons = D.donors(ex)
    kinds = [k for k in D.KINDS if k in dons]
    rend = {k: S2.render(tok, dons[k], shots, ARM) for k in kinds}
    diffs = {k: check_diff(tok, tgt.ids, rend[k].ids, k) for k in kinds}

    # 1. the unpatched target and the donors, captured at the union slots, `bs` rows per call
    rows = [tgt.ids] + [rend[k].ids for k in kinds]
    caps: list[list[Any]] = []
    texts, preds, lps = [], [], []
    for b0 in range(0, len(rows), bs):
        with capture_all(mods, upos, n) as cap:
            tx, pr, lp = generate(ctx, pad(rows[b0:b0 + bs], tgt.ids, bs))
        if any(c is None for c in cap):
            raise RuntimeError("a K/V capture hook captured nothing")
        k_real = min(bs, len(rows) - b0)
        caps.append([c[:k_real] for c in cap])
        texts += tx[:k_real]
        preds += pr[:k_real]
        lps += list(lp[:k_real])
    ref = {"text": texts[0], "pred": preds[0], "lp0": np.round(lps[0], 3).tolist()}
    don_out = {k: {"text": texts[i + 1], "pred": preds[i + 1], "diff": diffs[k],
                   "a_alt": dons[k]["a_alt"], "y_alt": dons[k]["y_alt"],
                   "dx": dons[k].get("dx"), "answer": dons[k]["answer"]}
               for i, k in enumerate(kinds)}
    src_of = {"self": 0, **{k: i + 1 for i, k in enumerate(kinds)}}

    # R: its own length, one row, captured at its own label and dots (slot for slot)
    rnd = D.random_donor(ex, pool)
    rr = S2.render(tok, rnd, shots, ARM)
    runion = torch.as_tensor(union_positions(tok, rr), device=ctx.device)
    with capture_all(mods, runion, len(rr.ids)) as rcap:
        rtexts, rpreds, _ = generate(ctx, [rr.ids])
    don_out["R"] = {"text": rtexts[0], "pred": rpreds[0], "idx": rnd["idx"],
                    "a_alt": rnd["a_alt"], "y_alt": rnd["y_alt"], "answer": rnd["answer"]}
    stored = [torch.cat([*(c[i] for c in caps), rc[:1]]) for i, rc in enumerate(rcap)]
    src_of["R"] = len(rows)

    # 2. patched rows; with bs > 1 every batch starts with an unpatched reference row
    specs = [(k, site) for k in [*kinds, "R"] for site in sites] + [("self", "all")]
    if reverse:
        specs = specs[::-1]
    per = bs - 1 if bs > 1 else 1
    patched, refs = [], []
    for b0 in range(0, len(specs), per):
        chunk = specs[b0:b0 + per]
        lead = [(-1, [])] if bs > 1 else []
        rows_spec = pad(lead + [(src_of[k], site_slots(s)) for k, s in chunk], (-1, []), bs)
        it = [torch.as_tensor(a, device=ctx.device, dtype=torch.long)
              for a in plan_patches(rows_spec, union)]
        with inject_rows(mods, *it, stored, n) as fired:
            tx, pr, lp = generate(ctx, [tgt.ids] * bs)
        if fired != [1] * len(mods):
            raise RuntimeError(f"injection fired {sorted(set(fired))} times per module")
        bi = len(refs)
        refs.append({"batch": bi, "text": tx[0] if bs > 1 else ref["text"],
                     "pred": pr[0] if bs > 1 else ref["pred"]})
        for j, (k, s) in enumerate(chunk, start=len(lead)):
            patched.append({"kind": k, "site": s, "batch": bi, "text": tx[j], "pred": pr[j],
                            "lp0": np.round(lp[j], 3).tolist()})
    selfrow = next(p for p in patched if p["kind"] == "self")
    return {
        "idx": ex["idx"], "answer": ex["answer"], "y": ex["queried_value"], "sites": list(sites),
        "bs": bs, "reverse": reverse, "n_prompt": n, "ref": ref, "donors": don_out,
        "patched": patched, "refs": refs,
        "self_eq_ref": selfrow["text"] == ref["text"],
        "refs_agree": all(r["text"] == ref["text"] for r in refs),
        "seconds": round(time.time() - t0, 2), "utc": S2.utc(),
    }


def stable(rec: dict) -> bool:
    return bool(rec["self_eq_ref"] and rec["refs_agree"])


def row_agreement(a: dict, b: dict) -> tuple[int, int]:
    """(agreeing, total) patched rows of two records of one item, matched by (kind, site)."""
    ta = {(p["kind"], p["site"]): p["text"] for p in a["patched"]}
    tb = {(p["kind"], p["site"]): p["text"] for p in b["patched"]}
    keys = sorted(set(ta) & set(tb))
    # patched rows only (note review r1 finding 4: the unpatched reference used to be counted
    # too; the main run's smoke reported 97.6% with it, 97.2% = 35/36 without)
    return sum(ta[k] == tb[k] for k in keys), len(keys)


def run_item(ctx: S2.Ctx, ex: dict, shots: list[dict], pool: list[dict],
             sites: tuple[str, ...], bs: int) -> dict:
    """do_item at `bs`; an unstable batched item (self-patch or a batch reference differs from
    the unpatched output) is rerun on the sequential path, which is the record kept."""
    rec = do_item(ctx, ex, shots, pool, sites, bs)
    if bs > 1 and not stable(rec):
        seq = do_item(ctx, ex, shots, pool, sites, 1)
        seq["rerun_of_unstable_batch"] = {"self_eq_ref": rec["self_eq_ref"],
                                          "refs_agree": rec["refs_agree"]}
        return seq
    return rec


# --------------------------------------------------------------------- download, load, run


def artifacts_dir() -> Path:
    return Path(os.environ.get("P24_ARTIFACTS", "/ws/p24_artifacts"))


def cmd_download(_a) -> int:
    from emx.i1a import hfget

    root = artifacts_dir()
    T = S2.Timer(TIMINGS)
    with T.step("download") as rec:
        files = S2.model_files(S2.MODEL_ID, S2.MODEL_REV)
        m = hfget.fetch(S2.MODEL_ID, files, revision=S2.MODEL_REV, local_dir=root / "gemma")
        rec.update(model_bytes=sum(p.stat().st_size for p in m.values()))
    S2.write_json(root / "paths.json", {"model_dir": str(m["config.json"].parent)})
    return 0


def budget_s() -> float:
    """Seconds from run.sh's start (P24_T0) to the end of the work, within probe.toml gpu_hours."""
    if "P24_BUDGET_S" in os.environ:
        return float(os.environ["P24_BUDGET_S"])
    cfg = tomllib.loads((PROBE / "probe.toml").read_text())
    return float(cfg["pod"]["gpu_hours"]) * 3600 - SETUP_ALLOWANCE_S


def done_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {json.loads(x)["idx"] for x in path.read_text().splitlines() if x.strip()}


def cmd_run(a) -> int:
    T = S2.Timer(TIMINGS)
    t0 = float(os.environ.get("P24_T0", time.time()))
    deadline = t0 + budget_s()
    status: dict[str, Any] = {"env": S2.env_info(), "complete": False, "batch": BATCH}
    items, shots, pool = load_items()
    if a.limit:
        items = items[: a.limit]
    RESULTS.mkdir(parents=True, exist_ok=True)
    os.environ["P06_ARTIFACTS"] = str(artifacts_dir())  # p06.stage2.load reads paths.json there
    with T.step("load") as rec:
        S2.ATTN = os.environ.get("P24_ATTN", S2.ATTN)  # sdpa (main run) or eager (F2)
        ctx, info = S2.load()
        info["attn_implementation"] = S2.ATTN
        rec.update(info)
    status["load"] = info
    have = done_ids(ROWS)
    smoke_items, bulk_items = items[:SMOKE_ITEMS], items[SMOKE_ITEMS:]
    bs = BATCH

    with T.step("smoke") as rec:
        recs, agree = [], {"sequential": [0, 0], "reversed": [0, 0]}
        for ex in smoke_items:
            if ex["idx"] in have:
                continue
            while True:  # an OOM at this batch size halves it (down to 2), then retries
                try:
                    r = do_item(ctx, ex, shots, pool, PRIMARY_SITES, bs)
                    break
                except Exception as e:
                    if "out of memory" not in str(e).lower() or bs <= 2:
                        raise
                    import torch

                    torch.cuda.empty_cache()
                    log(f"OOM at bs={bs}: retrying at bs={bs // 2}")
                    bs //= 2
            seq = do_item(ctx, ex, shots, pool, PRIMARY_SITES, 1)
            rev = do_item(ctx, ex, shots, pool, PRIMARY_SITES, bs, reverse=True)
            for name, other in (("sequential", seq), ("reversed", rev)):
                g, n = row_agreement(r, other)
                agree[name][0] += g
                agree[name][1] += n
            S2.append_jsonl(RESULTS / "validation.jsonl",
                            {"idx": ex["idx"], "batched": r, "sequential": seq, "reversed": rev})
            recs.append((r, seq))
        if not recs:  # resumed: trust only a persisted passing smoke
            prev = json.loads((RESULTS / "smoke.json").read_text()) \
                if (RESULTS / "smoke.json").exists() else {}
            checks = {"rerun: persisted smoke passed":
                      bool(prev.get("checks")) and all(prev["checks"].values())}
            per_item, bs = prev.get("per_item_s", 0.0), prev.get("bs", BATCH)
        else:
            rates = {k: v[0] / v[1] for k, v in agree.items()}
            batched_ok = rates["sequential"] >= AGREE_MIN and rates["reversed"] >= AGREE_MIN \
                and all(stable(r) for r, _ in recs)
            if not batched_ok:
                bs = 1
                log(f"batched rows disagree with the sequential path ({rates}): bulk runs at bs=1")
            base = [r["seconds"] for r, _ in recs] if bs > 1 else [q["seconds"] for _, q in recs]
            per_item = float(np.median(base[1:] or base))  # the first item warms up
            checks = {"sequential self-patch = unpatched": all(q["self_eq_ref"] for _, q in recs),
                      "sequential references agree": all(q["refs_agree"] for _, q in recs)}
            for r, q in recs:  # the kept smoke record: the path the bulk uses
                S2.append_jsonl(ROWS, r if bs > 1 else q)
            rec.update(agreement=rates, batched_ok=batched_ok)
        todo = [ex for ex in bulk_items if ex["idx"] not in have]
        left = deadline - time.time()
        n_fit = max(0, int(left / per_item)) if per_item else len(todo)
        if n_fit < len(todo):
            log(f"bulk cut: {n_fit} of {len(todo)} items fit the budget ({left / 60:.0f} min)")
            todo = todo[:n_fit]
        n_kept = len(done_ids(ROWS) & {e["idx"] for e in items})  # smoke and earlier sessions
        checks["bulk fits (>= MIN_BULK items)"] = n_kept + len(todo) >= min(MIN_BULK, len(items))
        rec.update(per_item_s=per_item, bs=bs, n_items=len(recs), bulk_planned=len(todo),
                   budget_left_s=round(left), checks=checks)
        smoke = dict(rec)
    S2.write_json(RESULTS / "smoke.json", smoke)
    status.update(smoke=smoke, bs=bs)
    log(f"SMOKE {checks}; bs={bs}; {per_item:.1f} s/item; pass 1: {len(todo)} items")
    if not all(checks.values()):
        status["stopped"] = "smoke failed: " + ", ".join(k for k, ok in checks.items() if not ok)
        S2.write_json(RESULTS / "status.json", status)
        log(f"P24_SMOKE_FAIL {status['stopped']}")
        return 3

    projected = per_item * len(todo)
    stop_at = min(deadline, time.time() + OVERRUN * projected + 120)
    done = reruns = 0
    with T.step("pass1") as rec:
        for i, ex in enumerate(todo):
            if time.time() > stop_at:
                status["stopped"] = f"overrun: past the projection or deadline after {i} items"
                log(f"P24_OVERRUN {status['stopped']}")
                break
            r = run_item(ctx, ex, shots, pool, PRIMARY_SITES, bs)
            reruns += "rerun_of_unstable_batch" in r
            S2.append_jsonl(ROWS, r)
            done = i + 1
            if done % 20 == 0:
                log(f"pass 1 {done}/{len(todo)} items")
        rec.update(items_done=done, items_planned=len(todo), projected_s=round(projected),
                   sequential_reruns=reruns)
    status.update(complete=done == len(todo), pass1_items_done=done, pass1_reruns=reruns)

    # pass 2: the secondary sites, same item order, while the budget lasts (no rc on a cut)
    p2_have = done_ids(ROWS2)
    done2 = 0
    if status["complete"] and os.environ.get("P24_PASS2", "1") == "1":
        with T.step("pass2") as rec:
            est = 2.0 * per_item  # ~3x the patched rows of pass 1; replaced by the measured mean
            secs2: list[float] = []
            for ex in items:
                if ex["idx"] in p2_have:
                    continue
                if time.time() + (float(np.mean(secs2)) if secs2 else est) > deadline:
                    log("pass 2 stopped at the deadline")
                    break
                r = run_item(ctx, ex, shots, pool, SECONDARY_SITES, bs)
                S2.append_jsonl(ROWS2, r)
                secs2.append(r["seconds"])
                done2 += 1
                if done2 % 20 == 0:
                    log(f"pass 2 {done2} items")
            rec.update(items_done=done2)
    status.update(pass2_items_done=done2 + len(p2_have), utc_end=S2.utc())
    S2.write_json(RESULTS / "status.json", status)
    return 0 if status["complete"] else 4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p24.pod")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N items (tests)")
    a = ap.parse_args(argv)
    try:
        return cmd_download(a) if a.step == "download" else cmd_run(a)
    except Exception:  # noqa: BLE001 - recorded for the pull, then rc 1
        import traceback

        RESULTS.mkdir(parents=True, exist_ok=True)
        with (RESULTS / "error.txt").open("a") as f:
            f.write(f"== {S2.utc()} step={a.step}\n{traceback.format_exc()}\n")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
