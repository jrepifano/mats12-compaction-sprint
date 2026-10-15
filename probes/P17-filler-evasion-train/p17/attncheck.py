"""P17 follow-up F1 (log.md): does full-sequence sdpa change P06 F3's Gemma 4 readouts?

On the first N items of Brauer's depth-1 set (dots100), P06 F3's own path
(`p06.stage2.forward_read`: full sequence, batch 1, no cache, then `p06.stage2.readouts`) runs
under eager and under sdpa on one loaded model. Compared: per-cell digit log-prob gaps (all 60
layers, the 114 read slots, every reader), lens-vs-model top-1 agreement under each, and P06 F3's
first-digit hit statistic for y (`p06.f3_sensitivity.stats`, label and filler groups) under
each. Readouts are float16 as P06 stored them, so gaps below ~0.03 at |lp| ~40 are storage
rounding. Writes results/attncheck.json.

    python -m p17.attncheck [--n 50]     # on the pod, after `python -m p17.pod download`
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
from p06 import f3_sensitivity as F3
from p06 import stage2 as S2

from . import common as C
from . import data as D
from . import schema as S
from .train import utc, write_json


def collect(ctx, tok, exs, shots) -> dict:
    import torch

    out = {"lp": {r: [] for r in ("logit_lens", *ctx.lenses)}, "agree": 0, "n": 0}
    for ex in exs:
        sp = C.render_split(tok, ex, shots, "dots100")
        with torch.inference_mode():
            hs, logits = S2.forward_read(ctx, sp.ids, sp.read[1:])
            ro = S2.readouts(ctx, hs)
        for r, v in ro.items():
            out["lp"][r].append(v["digit_lp"].astype(np.float32))
        out["agree"] += int((ro["logit_lens"]["top1"][-1] == logits.argmax(-1).cpu().numpy()).sum())
        out["n"] += len(sp.read) - 1
    out["lp"] = {r: np.stack(v) for r, v in out["lp"].items()}
    return out


def hits(lp: np.ndarray, yd: np.ndarray) -> dict:
    slots = np.array(S.SLOTS)
    return {g: (F3.stats(lp, slots, F3.GROUPS[g], "logprob").argmax(1) == yd)
            for g in ("label", "filler")}


def main(argv: list[str] | None = None) -> int:
    from .pod import load

    ap = argparse.ArgumentParser(prog="p17.attncheck")
    ap.add_argument("--n", type=int, default=50)
    a = ap.parse_args(argv)
    t0 = time.time()
    exs, shots = D.load_eval()
    exs = exs[: a.n]
    model, tok, rd, info = load("eager")
    ctx = rd.ctx(model, tok, "cuda")
    yd = np.array([int(str(ex["queried_value"])[0]) for ex in exs])
    res: dict = {"n_items": len(exs), "load": {k: v for k, v in info.items() if k != "lenses"},
                 "utc": utc()}
    got = {}
    for impl in ("eager", "sdpa"):
        model.set_attn_implementation(impl)
        t = time.time()
        got[impl] = collect(ctx, tok, exs, shots)
        res[f"{impl}_lens_model_agree"] = got[impl]["agree"] / got[impl]["n"]
        res[f"{impl}_seconds"] = round(time.time() - t, 1)
    res["readers"] = {}
    for r in got["eager"]["lp"]:
        a_, b_ = got["eager"]["lp"][r], got["sdpa"]["lp"][r]
        d = np.abs(a_ - b_)
        ha, hb = hits(a_, yd), hits(b_, yd)
        res["readers"][r] = {
            "max": float(d.max()), "p999": float(np.percentile(d, 99.9)),
            "mean": float(d.mean()),
            "per_layer_max": [round(float(x), 3) for x in d.max(axis=(0, 2, 3))],
            "hit_rate": {g: {"eager": float(ha[g].mean()), "sdpa": float(hb[g].mean()),
                             "items_differing": int((ha[g] != hb[g]).sum())} for g in ha}}
    res["seconds"] = round(time.time() - t0, 1)
    write_json(S.RESULTS / "attncheck.json", res)
    print(json.dumps({r: {k: v for k, v in x.items() if k != "per_layer_max"}
                      for r, x in res["readers"].items()}, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
