"""P40 analysis: bootstrap, the LPM fit and the plan's verdict rules on synthetic rows."""

from __future__ import annotations

import random

from p40 import analyze as AN

PATS = [f"{a}{b}{c}" for a in "01" for b in "01" for c in "01"]


def rows_for(rates: dict[str, float], n_items: int = 40, seed: int = 0) -> list[dict]:
    """F rows at L48 for every core recipient x pattern, flips drawn at ``rates[feature]``."""
    rng = random.Random(seed)
    out = []
    for site in ("both", "second"):
        for c in AN.CORE:
            for p in PATS:
                if c in ("P", "PC") and p == "000":
                    continue
                pr = max(rates[f] for f, bit in zip(("order", "subject", "role"), p, strict=True)
                         if bit == "1") if "1" in p else 0.0  # fmt: skip
                for it in range(n_items):
                    f = rng.random() < pr
                    out.append({"site": site, "layers": [48], "rcons": c, "dcons": "A",
                                "keep": True, "pat": p, "item": it, "top1": 1 if f else 0,
                                "a_alt": 1, "a_rec": 0, "cands": [0, 1]})  # fmt: skip
    return out


def test_boot_interval_brackets_mean():
    rows = [{"item": i % 10, "v": int(i % 3 == 0)} for i in range(300)]
    b = AN.boot(rows, lambda r: r["v"])
    assert b["lo"] <= b["mean"] <= b["hi"] and b["items"] == 10 and b["n"] == 300


def test_order_verdict_and_lpm():
    rows = rows_for({"order": 0.9, "subject": 0.02, "role": 0.01})
    fs = AN.f_summary(rows, list(AN.CORE), 48)
    v = AN.f_verdict(fs, list(AN.CORE))
    assert v["verdict"] == "order index", v
    lpm = fs["both"]["lpm"]
    assert lpm["order"]["coef"] > 0.7 and abs(lpm["subject"]["coef"]) < 0.15


def test_subject_mixed_and_none():
    fs = AN.f_summary(rows_for({"order": 0.02, "subject": 0.8, "role": 0.0}), list(AN.CORE), 48)
    assert AN.f_verdict(fs, list(AN.CORE))["verdict"] == "subject marker"
    fs = AN.f_summary(rows_for({"order": 0.6, "subject": 0.4, "role": 0.0}), list(AN.CORE), 48)
    assert AN.f_verdict(fs, list(AN.CORE))["verdict"] == "mixed"
    fs = AN.f_summary(rows_for({"order": 0.3, "subject": 0.0, "role": 0.0}), list(AN.CORE), 48)
    assert AN.f_verdict(fs, list(AN.CORE))["verdict"] == "no single feature reaches 50%"
    assert "cannot" in AN.f_verdict(fs, ["A", "P"])["verdict"]
