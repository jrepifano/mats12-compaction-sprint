"""P33 Stage 2 donors (plan.md "Measure" (b); P24-style, for V4's single-token numbers).

For a recipient item (Brauer depth 1: x literal, y = c1 * x + s * off, answer = q(y) with the
question's final operation q(y) = c2 * y +/- k2):

    sameY     x' != x and off' with c1 * x' + s * off' = y   -> y kept, definitions changed
    diffY     x' != x, off kept                                -> y' = c1 * x' + s * off
    opChange  x' as diffY, and the question's final operation changed (plus <-> minus, k2')
    random    another equation item with the same prompt layout

The predicted **hybrid** answer of a donor patch is the recipient's operation applied to the
donor's y: q_r(y_donor). For sameY it is the recipient's own answer; for diffY it equals the
donor's answer; for opChange it differs from both the donor's and the recipient's answers, which
is the decisive case (filler carries y vs the filler carries the donor's final answer).
V4 tokenizes 0..999 as single tokens, so every value is kept in 0..999 (the prompt's token
length is unchanged; checked against the tokenizer in stage2).
"""

from __future__ import annotations

import copy
import random
import re

from p06 import brauer as B

SEED = 20261013
MAXV = 999
KINDS = ("sameY", "diffY", "opChange", "random")

_DEF = re.compile(r"^(twice|three times|four times|five times|six times) the number for (\w+) "
                  r"(plus|minus) (\d+)$")
_Q = re.compile(r"^What is (twice|three times|four times|five times|six times) the number for "
                r"(\w+) (plus|minus) (\d+)\?$")


def parse(ex: dict) -> dict:
    defs = dict(ex["definitions"])
    m = _DEF.match(defs[ex["queried_term"]])
    if m is None:
        raise ValueError(f"idx {ex['idx']}: y's definition {defs[ex['queried_term']]!r}")
    coef, ref, op, off = m.group(1), m.group(2), m.group(3), int(m.group(4))
    x = defs[ref]
    if not isinstance(x, int):
        raise TypeError(f"idx {ex['idx']}: {ref} is not a literal")
    c1, s = B._COEF[coef], (1 if op == "plus" else -1)
    y = c1 * x + s * off
    if y != ex["queried_value"] or q(ex, y) != ex["answer"]:
        raise ValueError(f"idx {ex['idx']}: resolved y/answer disagree with the item")
    return {"coef": coef, "c1": c1, "ref": ref, "op": op, "s": s, "off": off, "x": x, "y": y}


def q(ex: dict, y: int) -> int:
    c2, k2 = ex["coefficient"], ex["constant"]
    return c2 * y + k2 if ex["operation"] == "plus" else c2 * y - k2


def edit(ex: dict, *, x: int | None = None, off: int | None = None, operation: str | None = None,
         constant: int | None = None) -> dict:
    """The item with x's literal, y's offset and/or the question's final operation changed."""
    p = parse(ex)
    d = copy.deepcopy(ex)
    ydef = f"{p['coef']} the number for {p['ref']} {p['op']} {p['off'] if off is None else off}"
    d["definitions"] = [[n, (x if x is not None and n == p["ref"] else
                             ydef if n == ex["queried_term"] else v)]
                        for n, v in ex["definitions"]]
    if operation is not None or constant is not None:
        m = _Q.match(ex["question"])
        if m is None:
            raise ValueError(f"idx {ex['idx']}: question {ex['question']!r}")
        d["operation"] = operation or ex["operation"]
        d["constant"] = ex["constant"] if constant is None else constant
        d["question"] = (f"What is {m.group(1)} the number for {m.group(2)} {d['operation']} "
                         f"{d['constant']}?")
    vals = B.values(d)
    d["queried_value"] = vals[d["queried_term"]]
    d["answer"] = q(d, d["queried_value"])
    return d


def _ok(v: int) -> bool:
    return 0 <= v <= MAXV


def explicit_y(ex: dict, y: int) -> dict:
    """The item with the queried variable defined as the literal y (the 'explicitly supplied
    intermediate' baseline); answer q(y)."""
    d = copy.deepcopy(ex)
    d["definitions"] = [[n, y if n == ex["queried_term"] else v] for n, v in ex["definitions"]]
    d["queried_value"], d["answer"] = y, q(ex, y)
    return d


def donors(ex: dict, pool: list[dict], seed: int = SEED) -> dict[str, dict]:
    """{kind: {"ex": donor example, "y": donor y, "donor_answer", "hybrid"}} for the kinds this
    item supports (random needs `pool` and is filtered for layout later)."""
    p = parse(ex)
    rng = random.Random(seed * 100_003 + ex["idx"])
    out: dict[str, dict] = {}
    xs = [v for v in range(1, 100) if v != p["x"]]
    rng.shuffle(xs)
    for xp in xs:  # sameY: off' = s * (y - c1 x')
        offp = p["s"] * (p["y"] - p["c1"] * xp)
        if 1 <= offp <= MAXV and offp != p["off"]:
            d = edit(ex, x=xp, off=offp)
            out["sameY"] = {"ex": d, "y": p["y"], "donor_answer": d["answer"],
                            "hybrid": q(ex, p["y"])}
            break
    for xp in xs:  # diffY and opChange share x'
        yp = p["c1"] * xp + p["s"] * p["off"]
        if yp == p["y"] or not _ok(yp) or not _ok(q(ex, yp)) or q(ex, yp) == ex["answer"]:
            continue
        flip = "minus" if ex["operation"] == "plus" else "plus"
        ks = [k for k in range(1, 50) if k != ex["constant"]]
        rng.shuffle(ks)
        for k in ks:
            e2 = edit(ex, x=xp, operation=flip, constant=k)
            a2, hyb = e2["answer"], q(ex, yp)
            if _ok(a2) and len({a2, hyb, ex["answer"]}) == 3:
                d1 = edit(ex, x=xp)
                out["diffY"] = {"ex": d1, "y": yp, "donor_answer": d1["answer"], "hybrid": hyb}
                out["opChange"] = {"ex": e2, "y": yp, "donor_answer": a2, "hybrid": hyb}
                break
        if "diffY" in out:
            break
    others = [e for e in pool if e["idx"] != ex["idx"]]
    if others:
        rng.shuffle(others)
        out["random_candidates"] = others  # stage2 picks the first with the same layout
    return out
