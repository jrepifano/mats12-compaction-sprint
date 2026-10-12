"""Small fake P15 results directories in the CONTRACTS.md layout, with a planted signal.

    from fake_results import make_fake_results
    make_fake_results(tmp_path, "A")            # writes tmp_path/A/...
    make_fake_results(tmp_path, "B")            # writes tmp_path/B/...

The planted signal: the latent-column residuals linearly encode the latent answer a_L (one random
code per class and column); the question residuals encode only the gold answer; the verbal answer
V is gold on most items (so it misses most errors); the direct answer D is noisy; on T the
layer-16 logit lens shows s1 at column 3 and s2 at column 5 on most items. So LP should beat LPq
and V on the error set E. Errors are drawn per item, so the (op, X, Y, Z) lookup K cannot predict
them. Hidden size is small (`d`); the analysis reads it from the arrays.

Used by tests/test_analysis.py and by the integrator's dry run (`python tests/fake_results.py DIR`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p15.tasks import write_jsonl

N_LAYERS, N_COLS = 17, 7
NUM_BASE = 1000  # token id NUM_BASE + v decodes to the digits of v (v in 0..999)
DEFAULT_SIGNAL = {"p_err": 0.3, "latent": 2.0, "question": 2.0, "p_verbal_gold": 0.9,
                  "p_direct_gold": 0.5, "signal_from_layer": 4}


def _codes(rng: np.random.Generator, labels, n_cols: int, d: int) -> dict:
    return {lab: rng.standard_normal((n_cols, d)).astype(np.float32) for lab in sorted(labels)}


def _label(v) -> str:
    return "none" if v is None else str(v)


def _resid(rng, labels: list[str], codes: dict, n_cols: int, d: int, scale: float,
           from_layer: int) -> np.ndarray:
    n = len(labels)
    x = rng.standard_normal((n, N_LAYERS, n_cols, d)).astype(np.float32)
    sig = np.stack([codes[lab] for lab in labels])  # (n, n_cols, d)
    x[:, from_layer:] += scale * sig[:, None]
    return x.astype(np.float16)


def _lltop(rng, n: int) -> np.ndarray:
    return rng.integers(0, NUM_BASE, size=(n, N_LAYERS, N_COLS, 5)).astype(np.int32)


def _plant(lltop, rng, i: int, layer: int, col: int, value: int, p: float) -> None:
    if 0 <= value <= 999 and rng.random() < p:
        lltop[i, layer, col, rng.integers(0, 3)] = NUM_BASE + value


def _g_items(rng, n_g: int, p_latent: float, p_direct: float, seed_items: int) -> tuple:
    """G items (item ids and questions depend only on seed_items, so A and B share them)."""
    irng = np.random.default_rng(seed_items)
    rows, lltop = [], _lltop(rng, n_g)
    for i in range(n_g):
        a, b, m = (int(v) for v in irng.integers(2, 40, size=3))
        c = a + b
        gold = c * m
        q = f"Ann has {a} apples and buys {b} more. Each costs {m} dollars. How much in total?"
        lat = gold if rng.random() < p_latent else gold + int(rng.integers(1, 9))
        dire = gold if rng.random() < p_direct else gold - int(rng.integers(1, 9))
        verb = f"<<{a}+{b}={c}>> <<{c}*{m}={gold}>><|eocot|>The answer is: {gold}"
        for v in (c, gold):
            _plant(lltop, rng, i, int(rng.integers(8, 17)), int(rng.integers(0, 7)), v, 0.6)
        rows.append({"set": "G", "idx": i, "item_id": f"G-{i:04d}",
                     "prompt": q + " Output only the answer and nothing else.", "gold": gold,
                     "question": q, "out_direct": str(dire), "out_latent": str(lat),
                     "out_verbal": verb, "ans_direct": dire, "ans_latent": lat,
                     "ans_verbal": gold, "verbal_parse": "after_eocot"})
    return rows, lltop


def _number_tokens(path: Path) -> None:
    path.write_text(json.dumps({str(NUM_BASE + v): v for v in range(1000)}))


def make_fake_results(dirpath, stage: str, n_templates: int = 20, seed: int = 0,
                      signal: dict | None = None, xyz_max: int = 5, d: int = 16,
                      n_c: int = 640, n_g: int = 60, n_patch: int = 40) -> Path:
    """Write `<dirpath>/<stage>/` and return it. Stage A: T (20 templates x (X, Y, Z) in
    1..xyz_max, X > Y), G, patch.jsonl, number_tokens.json. Stage B: C (n_c items, 8 scenarios,
    k in 3..5) and G (the same G item ids as stage A)."""
    sig = {**DEFAULT_SIGNAL, **(signal or {})}
    rng = np.random.default_rng(seed)
    out = Path(dirpath) / stage
    out.mkdir(parents=True, exist_ok=True)
    if stage == "A":
        _stage_a(out, rng, sig, n_templates, xyz_max, d, n_g, n_patch)
        _number_tokens(out / "number_tokens.json")
    elif stage == "B":
        _stage_b(out, rng, sig, d, n_c, n_g)
        if not (Path(dirpath) / "A" / "number_tokens.json").exists():
            _number_tokens(out / "number_tokens.json")
    else:
        raise ValueError(stage)
    return out


def _stage_a(out: Path, rng, sig: dict, n_templates: int, xyz_max: int, d: int, n_g: int,
             n_patch: int) -> None:
    rows = []
    for t in range(n_templates):
        op = "add" if t < 10 else "sub"
        for X in range(1, xyz_max + 1):
            for Y in range(1, X):
                for Z in range(1, xyz_max + 1):
                    s1 = X + Y if op == "add" else X - Y
                    s2 = s1 * Z
                    rows.append({"set": "T", "template_id": t, "op": op, "X": X, "Y": Y,
                                 "Z": Z, "s1": s1, "s2": s2, "gold": s1 + s2})
    lat_labels, q_labels = [], []
    lltop = _lltop(rng, len(rows))
    for i, r in enumerate(rows):
        g = r["gold"]
        r.update(idx=i, item_id=f"T-{i:04d}", prompt=f"[template {r['template_id']}] "
                 f"X={r['X']} Y={r['Y']} Z={r['Z']}")
        if rng.random() < sig["p_err"]:
            a_l = None if rng.random() < 0.05 else max(0, g + int(rng.choice([-2, -1, 1, 2])))
        else:
            a_l = g
        v = g if rng.random() < sig["p_verbal_gold"] else g + 3
        dd = g if rng.random() < sig["p_direct_gold"] else g - 1
        r.update(out_latent="" if a_l is None else f"The answer is: {a_l}", ans_latent=a_l,
                 out_direct=str(dd), ans_direct=dd,
                 out_verbal=(f"<<{r['X']}{'+' if r['op'] == 'add' else '-'}{r['Y']}={r['s1']}>> "
                             f"<<{r['s1']}*{r['Z']}={r['s2']}>><|eocot|>The answer is: {v}"),
                 ans_verbal=v, verbal_parse="after_eocot",
                 ps_lat3=r["s1"] if rng.random() < 0.5 else None,
                 ps_lat5=r["s2"] if rng.random() < 0.5 else None)
        _plant(lltop, rng, i, 16, 3, r["s1"], 0.8)
        _plant(lltop, rng, i, 16, 5, r["s2"], 0.7)
        lat_labels.append(_label(a_l))
        q_labels.append(_label(g))
    lc = _codes(rng, set(lat_labels), N_COLS, d)
    qc = _codes(rng, set(q_labels), 2, d)
    fl = sig["signal_from_layer"]
    np.save(out / "T_resid.npy", _resid(rng, lat_labels, lc, N_COLS, d, sig["latent"], fl))
    np.save(out / "T_qresid.npy", _resid(rng, q_labels, qc, 2, d, sig["question"], fl))
    np.save(out / "T_lltop.npy", lltop)
    np.save(out / "T_latvec.npy", rng.standard_normal((len(rows), N_COLS, d)).astype(np.float16))
    write_jsonl(out / "T.jsonl", rows)

    g_rows, g_ll = _g_items(rng, n_g, 0.45, 0.35, seed_items=7)
    write_jsonl(out / "G.jsonl", g_rows)
    np.save(out / "G_lltop.npy", g_ll)

    patch = []
    for r in rows[:n_patch]:
        for lat in (3, 5):
            if lat == 3 and r["idx"] % 4 == 0:
                continue  # no valid donor
            target = r["gold"] + (r["Z"] + 1 if lat == 3 else 1) * int(rng.choice([1, 2]))
            for cond in ("diff", "same", "random", "none"):
                if cond == "diff":
                    ans = target if rng.random() < 0.4 else r["gold"]
                elif cond == "random":
                    ans = target if rng.random() < 0.05 else (
                        r["gold"] if rng.random() < 0.6 else r["gold"] + 7)
                else:
                    ans = r["gold"] if rng.random() < 0.9 else r["gold"] + 1
                patch.append({"item_id": r["item_id"], "lat": lat, "cond": cond,
                              "target": target, "gold": r["gold"], "ans": ans,
                              "out": f"The answer is: {ans}"})
    write_jsonl(out / "patch.jsonl", patch)


def _stage_b(out: Path, rng, sig: dict, d: int, n_c: int, n_g: int) -> None:
    rows, lat_labels, q_labels = [], [], []
    lltop = _lltop(rng, n_c)
    p_err_k = {3: 0.6 * sig["p_err"], 4: sig["p_err"], 5: 1.6 * sig["p_err"]}
    for i in range(n_c):
        scenario, k = i % 8, 3 + (i // 8) % 3
        X = int(rng.integers(5, 16))
        v, steps, inter = X, [], []
        for _ in range(k):
            if v <= 10 and rng.random() < 0.25:
                st = ["*", 2]
            elif v > 3 and rng.random() < 0.5:
                st = ["-", int(rng.integers(1, min(4, v)))]
            else:
                st = ["+", int(rng.integers(1, 4))]
            v = v * st[1] if st[0] == "*" else v + st[1] if st[0] == "+" else v - st[1]
            steps.append(st)
            inter.append(v)
        gold = inter[-1]
        pat = "".join(op + (str(o) if op == "*" else "") for op, o in steps)
        a_l = gold if rng.random() >= p_err_k[k] else max(1, gold + int(rng.choice([-2, -1, 1,
                                                                                     2])))
        vb = gold if rng.random() < sig["p_verbal_gold"] else gold + 5
        dd = gold if rng.random() < sig["p_direct_gold"] else gold - 1
        prev, eqs = X, []
        for (op, o), s in zip(steps, inter):
            eqs.append(f"{prev}{'x' if op == '*' else op}{o}={s}")
            prev = s
        verbal = ". ".join(eqs[:-1]) + f".<|eocot|>The answer is: {vb}"
        rows.append({"set": "C", "idx": i, "item_id": f"C-{i:04d}", "scenario": scenario, "k": k,
                     "X": X, "steps": steps, "inter": inter, "op_pattern": pat,
                     "phrasing": [0] * k, "prompt": f"[scenario {scenario}] start {X}, {pat}",
                     "gold": gold, "cot": ". ".join(eqs) + ".",
                     "out_latent": f"The answer is: {a_l}", "ans_latent": a_l,
                     "out_direct": str(dd), "ans_direct": dd, "out_verbal": verbal,
                     "ans_verbal": vb, "verbal_parse": "after_eocot"})
        for j, s in enumerate(inter):
            _plant(lltop, rng, i, 16, min(2 * j + 1, N_COLS - 1), s, 0.6)
        lat_labels.append(_label(a_l))
        q_labels.append(_label(gold))
    lc = _codes(rng, set(lat_labels), N_COLS, d)
    qc = _codes(rng, set(q_labels), 2, d)
    fl = sig["signal_from_layer"]
    np.save(out / "C_resid.npy", _resid(rng, lat_labels, lc, N_COLS, d, sig["latent"], fl))
    np.save(out / "C_qresid.npy", _resid(rng, q_labels, qc, 2, d, sig["question"], fl))
    np.save(out / "C_lltop.npy", lltop)
    write_jsonl(out / "C.jsonl", rows)
    g_rows, g_ll = _g_items(rng, n_g, 0.35, 0.35, seed_items=7)
    write_jsonl(out / "G.jsonl", g_rows)
    np.save(out / "G_lltop.npy", g_ll)


if __name__ == "__main__":
    target = Path(sys.argv[1])
    for st in ("A", "B"):
        print(make_fake_results(target, st))
