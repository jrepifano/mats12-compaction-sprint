"""P24 donors: prompts that differ from the target only in y's definition line or in x's literal.

Brauer's depth-1 item (p06.brauer): x is a literal, y = c1 * x + s * off (s = +1 for "plus",
-1 for "minus"; y's definition is "<coef> the number for <ref> <op> <off>"), and the question
asks for q(y) = c2 * y +/- k2. P06 F3's donor changed x only, so its filler could carry a copy
of x rather than y. Here, for a seeded shift dx (the same for X, Y and S):

    X  x' = x + dx, off kept        -> y_X = y + c1 * dx      (P06's donor type)
    Y  x kept, off_Y = off + s c1 dx -> y_Y = y_X              (y changes, x fixed)
    S  x' = x + dx, off_S = off - s c1 dx -> y_S = y           (y fixed, x and off change)
    F  op flipped (plus <-> minus), no number token changes -> y_F = c1 x - s off
    C  "three times" -> "four times" (items with c1 = 3 only) -> y_C = 4 x + s off
    P  P06 F3's own donor (`p06.stage2.make_donor`, seed 0: x' any other 2-digit value), to
       tell "P06 does not replicate here" from "these smaller shifts move less"
    R  another item (the disruption control)

X, Y and S share one alternative answer a' = q(y_X): under "the filler carries y", X and Y
move the output toward a' and S does not; under "the filler carries a copy of x", X and S
move it toward a' (S's x' through the prompt's own offset gives y_X) and Y does not.

Every donor keeps the token length: x' keeps x's digit count, off_Y and off_S keep off's digit
count (Gemma 4 splits numbers into digits), "plus"/"minus" are one token each, "three
times"/"four times" two each (checked against the tokenizer in render, p24.model).
"""

from __future__ import annotations

import copy
import random
import re

from p06 import brauer as B
from p06 import stage2 as S2

SEED = 20261012
KINDS = ("X", "Y", "S", "F", "C", "P")
SHIFT_TIERS = (10, 4, 1)  # prefer |c1 * dx| >= 10, then >= 4, so a' is not next to the answer

_DEF = re.compile(r"^(twice|three times|four times|five times|six times) the number for (\w+) "
                  r"(plus|minus) (\d+)$")


def parse(ex: dict) -> dict:
    """y's definition and the values the donors need."""
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
    return {"coef": coef, "c1": c1, "ref": ref, "op": op, "s": s, "off": off, "x": x, "y": y,
            "answer": ex["answer"]}


def q(ex: dict, y: int) -> int:
    """The question's answer for a given y."""
    c2, k2 = ex["coefficient"], ex["constant"]
    return c2 * y + k2 if ex["operation"] == "plus" else c2 * y - k2


def edit(ex: dict, *, x: int | None = None, coef: str | None = None, op: str | None = None,
         off: int | None = None) -> dict:
    """The item with x's literal and/or y's definition changed; every value recomputed."""
    p = parse(ex)
    d = copy.deepcopy(ex)
    ydef = (f"{coef or p['coef']} the number for {p['ref']} {op or p['op']} "
            f"{p['off'] if off is None else off}")
    d["definitions"] = [[n, (x if x is not None and n == p["ref"] else
                             ydef if n == ex["queried_term"] else v)]
                        for n, v in ex["definitions"]]
    vals = B.values(d)  # raises on a malformed expression
    d["queried_value"] = vals[d["queried_term"]]
    d["answer"] = q(d, d["queried_value"])
    return d


def _ndig(v: int) -> int:
    return len(str(v))


def shift_ok(ex: dict, dx: int) -> bool:
    """X, Y and S are all valid for this dx."""
    p = parse(ex)
    xp = p["x"] + dx
    off_y = p["off"] + p["s"] * p["c1"] * dx
    off_s = p["off"] - p["s"] * p["c1"] * dx
    y_x = p["y"] + p["c1"] * dx
    return (dx != 0 and _ndig(xp) == _ndig(p["x"]) and xp > 0
            and min(off_y, off_s) >= 1 and _ndig(off_y) == _ndig(off_s) == _ndig(p["off"])
            and y_x >= 0 and q(ex, y_x) >= 0 and q(ex, y_x) != p["answer"])


def pick_dx(ex: dict, seed: int = SEED) -> int | None:
    """A seeded dx (per idx) valid for X, Y and S, from the first non-empty SHIFT_TIERS tier."""
    p = parse(ex)
    ok = [dx for dx in range(-30, 31) if shift_ok(ex, dx)]
    for tier in SHIFT_TIERS:
        pool = [dx for dx in ok if abs(p["c1"] * dx) >= tier]
        if pool:
            return random.Random(seed * 100_003 + ex["idx"]).choice(pool)
    return None


def donors(ex: dict, seed: int = SEED) -> dict[str, dict]:
    """{kind: donor item} for the kinds this item supports, each tagged with `kind`, `y_alt`
    (the y the donor implies under its tested hypothesis) and `a_alt` = q(y_alt)."""
    p = parse(ex)
    out: dict[str, dict] = {}
    dx = pick_dx(ex, seed)
    if dx is not None:
        y_x = p["y"] + p["c1"] * dx
        out["X"] = edit(ex, x=p["x"] + dx)
        out["Y"] = edit(ex, off=p["off"] + p["s"] * p["c1"] * dx)
        out["S"] = edit(ex, x=p["x"] + dx, off=p["off"] - p["s"] * p["c1"] * dx)
        for k in ("X", "Y", "S"):
            out[k].update(kind=k, dx=dx, y_alt=y_x, a_alt=q(ex, y_x))
        assert out["X"]["queried_value"] == out["Y"]["queried_value"] == y_x
        assert out["S"]["queried_value"] == p["y"]
    flip = "minus" if p["op"] == "plus" else "plus"
    f = edit(ex, op=flip)
    if f["queried_value"] >= 0 and f["answer"] >= 0 and f["answer"] != p["answer"]:
        out["F"] = {**f, "kind": "F", "y_alt": f["queried_value"], "a_alt": f["answer"]}
    if p["coef"] == "three times":
        c = edit(ex, coef="four times")
        if c["answer"] >= 0:
            out["C"] = {**c, "kind": "C", "y_alt": c["queried_value"], "a_alt": c["answer"]}
    try:
        pdon = S2.make_donor(ex)
        out["P"] = {**pdon, "kind": "P", "y_alt": pdon["queried_value"], "a_alt": pdon["answer"]}
    except ValueError:
        pass
    for d in out.values():
        d["donor_of"] = ex["idx"]
    return out


def random_donor(ex: dict, pool: list[dict], seed: int = SEED) -> dict:
    """Another item (seeded per idx): the disruption control R. Its a_alt is its own answer."""
    others = [e for e in pool if e["idx"] != ex["idx"]]
    r = random.Random(seed * 7_919 + ex["idx"]).choice(others)
    return {**r, "kind": "R", "y_alt": r["queried_value"], "a_alt": r["answer"],
            "donor_of": ex["idx"]}
