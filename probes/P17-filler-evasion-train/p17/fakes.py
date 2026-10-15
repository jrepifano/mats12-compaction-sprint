"""Synthetic P17 results for the analysis tests: `write_fake_results` writes a results directory
in the `p17.schema` layout (answers.jsonl, P06-schema readout .npz files through
`p06.stage2.write_npz`, pools.npz, transplant.jsonl, train records, done.json) with planted
effects that the caller controls through `signal`.

Each `signal` entry is a number (all arms) or a dict {"B": .., "K": .., "E": ..}:

- `ll_hi`, `jlens_hi`, `jpp_hi`: added to y's first-digit log-prob at layers 30-59 in the label
  and dot slots, per reader (negative = suppressed below the other digits);
- `ll_lo`: the logit lens's y first-digit signal at layers 0-29;
- `second`: y's second digit, every reader, layers 30-59;
- `acc_base`, `uplift`, `ask`: accuracy without filler, the uplift with it, and ask accuracy;
- `probe`: the y signal in the dots_mean and label pools (prefill carries x only);
- `pen_ratio`: last / first held-out penalty in the train record;
- `tx_toward`, `rtx_toward`: the matched and random transplant's toward-donor rates.

To keep the files small, `n_dots` of the 100 dot slots are written (plus the label and the 12
suffix slots), and `pool_layers` may be a subset of `schema.POOL_LAYERS`; the analysis selects
slots and layers by value, so neither changes what it computes.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# importable because p17/__init__ (run first) puts P06 on sys.path
from p06.readers_analysis import first_digit
from p06.stage2 import ROLES, write_npz

from . import schema as S

DEFAULT_SIGNAL: dict = {
    "ll_hi": 1.5, "jlens_hi": 1.5, "jpp_hi": 1.5, "ll_lo": 0.0, "second": 1.0,
    "acc_base": 0.3, "uplift": 0.35, "ask": 0.8, "probe": 1.5,
    "pen_ratio": {"B": 1.0, "K": 1.0, "E": 0.1},
    "tx_toward": 0.5, "rtx_toward": 0.05,
}
NOISE = 0.5
D_POOL = 16


def _g(signal: dict, key: str, arm: str) -> float:
    v = signal[key]
    return float(v[arm]) if isinstance(v, dict) else float(v)


def _items(n: int, rng: np.random.Generator) -> list[dict]:
    out = []
    for i in range(n):
        x = int(rng.integers(2, 20))
        c1 = int(rng.integers(2, 9))
        y = c1 * x + int(rng.integers(1, 60))
        c2 = int(rng.integers(2, 5))
        ans = c2 * y + int(rng.integers(0, 10))
        out.append({"idx": 1000 + i, "values": {"x": x, "c1x": c1 * x, "y": y, "c2y": c2 * y,
                                                 "answer": ans}})
    return out


def _slots(n_dots: int) -> np.ndarray:
    return np.array([*S.LABEL_SLOTS, *S.DOT_SLOTS[:n_dots], *S.SUFFIX_SLOTS], dtype=np.int16)


def _readout(it: dict, arm: str, signal: dict, slots: np.ndarray, rng) -> dict:
    v = it["values"]
    fd = first_digit(v["y"])
    s = str(abs(v["y"]))
    sd = int(s[1]) if len(s) > 1 else None
    tgt = np.isin(slots, (*S.LABEL_SLOTS, *S.DOT_SLOTS))
    ro = {}
    for reader, key in (("logit_lens", "ll_hi"), ("jlens", "jlens_hi"), ("jpp", "jpp_hi")):
        lp = -2.3 + NOISE * rng.standard_normal((S.N_LAYERS, len(slots), 10))
        lp[S.MIN_LAYER:, tgt, fd] += _g(signal, key, arm)
        if reader == "logit_lens":
            lp[: S.MIN_LAYER, tgt, fd] += _g(signal, "ll_lo", arm)
        if sd is not None:
            lp[S.MIN_LAYER:, tgt, sd] += _g(signal, "second", arm)
        ro[reader] = {"digit_lp": lp.astype(np.float16),
                      "top1": lp.argmax(-1).astype(np.int32)}
    n_prompt = 1500
    return {"item_id": f"varbind:{it['idx']}", "idx": it["idx"], "arm": "dots100",
            "positions": (slots.astype(np.int32) + n_prompt), "slots": slots,
            "token_ids": np.zeros(len(slots), np.int32), "n_prompt": n_prompt,
            "gen_ids": np.array([1, 2], np.int32), "gen_text": str(v["answer"]),
            "gen_digit_lp": np.zeros((2, 10), np.float16),
            "digit_ids": np.arange(10, dtype=np.int32), "roles": list(ROLES),
            "values": np.array([v[k] for k in ROLES], dtype=np.int64), "readers": list(ro),
            "ro": ro}


def _answers(items: list[dict], m: str, arm: str, signal: dict, rng) -> list[dict]:
    a, u, ask = (_g(signal, k, arm) for k in ("acc_base", "uplift", "ask"))
    rows = []
    for it in items:
        base = rng.random() < a
        if u >= 0:
            dots = base or rng.random() < u / max(1e-9, 1 - a)
        else:
            dots = base and rng.random() >= -u / max(1e-9, a)
        v = it["values"]
        for kind, ok, target in (("base", base, v["answer"]), ("dots100", dots, v["answer"]),
                                 ("ask", rng.random() < ask, v["y"])):
            pred = target if ok else target + 1
            rows.append({"model": m, "idx": it["idx"], "kind": kind, "target": target,
                         "gen_text": str(pred), "predicted": pred, "correct": bool(ok)})
    return rows


def _pools(items: list[dict], arm: str, signal: dict, layers: tuple[int, ...], rng) -> dict:
    n, K = len(items), len(layers)
    y = np.array([it["values"]["y"] for it in items], float)
    x = np.array([it["values"]["x"] for it in items], float)
    fd = np.array([first_digit(v) for v in y.astype(int)])
    zy, zx = (y - y.mean()) / y.std(), (x - x.mean()) / x.std()
    s = _g(signal, "probe", arm)
    out = {"idx": np.array([it["idx"] for it in items], np.int64),
           "layers": np.array(layers, np.int64)}
    for key in S.POOL_KEYS:
        X = rng.standard_normal((n, K, D_POOL))
        live = np.array(layers) >= 20  # the signal sits in the later layers only
        if key == "prefill":
            X[:, :, 0] += 1.5 * zx[:, None]
        else:
            X[:, live, 0] += s * zy[:, None]
            X[np.arange(n), :, 1 + fd] += s * live[None, :]
        out[key] = X.astype(np.float16)
    return out


def _transplant(items: list[dict], arm: str, signal: dict, rng) -> list[dict]:
    rows = []
    for it in items[: min(len(items), 40)]:
        a = it["values"]["answer"]
        donor, rand = a + 10, a + 50
        tx = donor if rng.random() < _g(signal, "tx_toward", arm) else a
        rtx = donor if rng.random() < _g(signal, "rtx_toward", arm) else a
        rows.append({"idx": it["idx"], "answer": a, "target_pred": a, "donor_answer": donor,
                     "tx_pred": tx, "rand_answer": rand, "rtx_pred": rtx, "self_pred": a,
                     "target_text": str(a), "tx_text": str(tx), "rtx_text": str(rtx),
                     "self_text": str(a)})
    return rows


def _record(m: str, arm: str, seed: int, signal: dict) -> dict:
    ratio = _g(signal, "pen_ratio", arm)
    steps = list(range(0, 251, 50))
    val = [{"step": s, "ce": 1.0 - 0.2 * s / 250, "pen": 2.0 * ratio ** (s / 250),
            "pen_hard": 1.5 * ratio ** (s / 250), "n": 50} for s in steps]
    return {"model": m, "arm": arm, "seed": seed, "config": {}, "lambda": 1.0,
            "steps": [{"step": s, "ce": 1.0, "pen": 1.0, "loss": 2.0, "grad_norm": 1.0,
                       "lr": S.LR, "seconds": 0.1, "utc": "2026-10-12T00:00:00Z"}
                      for s in steps],
            "val": val, "timing": {"train": 1.0}, "complete": True}


def write_fake_results(root: Path, n_items: int = 60, seeds=(1, 2), signal: dict | None = None,
                       rng_seed: int = 0, n_dots: int = 10,
                       pool_layers: tuple[int, ...] = S.POOL_LAYERS) -> Path:
    """Write a synthetic results dir under `root` (created) and return it."""
    sig = {**DEFAULT_SIGNAL, **(signal or {})}
    root = Path(root)
    rng = np.random.default_rng(rng_seed)
    items = _items(n_items, rng)
    slots = _slots(n_dots)
    for m in S.model_names(tuple(seeds)):
        arm = m[0]
        mrng = np.random.default_rng([rng_seed, ord(arm), int(m[1:] or 0)])
        ev = root / "eval" / m
        ev.mkdir(parents=True, exist_ok=True)
        rows = _answers(items, m, arm, sig, mrng)
        (ev / "answers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        for it in items:
            write_npz(ev / "readouts" / f"varbind_{it['idx']}__dots100.npz",
                      _readout(it, arm, sig, slots, mrng))
        np.savez_compressed(ev / "pools.npz", **_pools(items, arm, sig, pool_layers, mrng))
        tx = _transplant(items, arm, sig, mrng)
        (ev / "transplant.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tx))
        if arm != "B":
            rec = _record(m, arm, int(m[1:]), sig)
            (root / "train" / m).mkdir(parents=True, exist_ok=True)
            (root / "train" / m / "record.json").write_text(json.dumps(rec))
        (ev / "done.json").write_text(json.dumps({"model": m, "n_items": n_items,
                                                  "kinds": list(S.KINDS), "seconds": 1.0,
                                                  "utc": "2026-10-12T00:00:00Z"}))
    return root
