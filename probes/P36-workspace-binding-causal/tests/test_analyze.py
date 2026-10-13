"""P36 analysis: flip/invalid/shift annotation and the decision rules on synthetic rows."""

from __future__ import annotations

from p36 import analyze as A
from p36 import stimuli as S


def _base(items=30):
    base = {}
    for i in range(items):
        for v in S.VOICES:
            for role in S.ROLES:
                for q in S.QUESTIONS:
                    sid = f"{i}-{v}-{role}"
                    base[(sid, q)] = {
                        "recip": sid,
                        "q": q,
                        "top1": 1,
                        "a_rec": 1,
                        "a_rev": 2,
                        "logit_rec": 5.0,
                        "logit_rev": 1.0,
                    }
    return base


def _rows(base, op, dct, k, flip_frac, cell=("final", [30])):
    rows = []
    for n, (sid, q) in enumerate(sorted(base)):
        flip = (n % 10) < round(flip_frac * 10)
        rows.append(
            {
                "recip": sid,
                "donor": S.twin_sid(sid),
                "q": q,
                "site": cell[0],
                "layers": cell[1],
                "kind": "twin",
                "op": op,
                "dict": dct,
                "k": k,
                "top1": 2 if flip else 1,
                "a_rec": 1,
                "a_rev": 2,
                "logit_rec": 1.0 if flip else 5.0,
                "logit_rev": 5.0 if flip else 1.0,
            }
        )
    return rows


def test_annotate_shift_and_flags():
    base = _base(2)
    rows = _rows(base, "r", "jlens", 25, 1.0)
    A.annotate(rows, base)
    assert all(r["flip"] == 1 and r["invalid"] == 0 for r in rows)
    # full reversal: LD 4 -> -4, twin's LD (in recipient terms) is -4 -> shift 1
    assert all(abs(r["shift"] - 1.0) < 1e-9 for r in rows)


def test_decide_residual_greater_and_recon_little():
    base = _base()
    rows = []
    for d in ("jlens", "jpp"):
        for k in (16, 25, 100, 400):
            rows += _rows(base, "r", d, k, 0.9) + _rows(base, "j", d, k, 0.0)
    A.annotate(rows, base)
    out = A.decide(rows, "final@30", ["jlens", "jpp"], [16, 25, 100, 400])
    assert out["verdict"].startswith("greater residual-swap effect")
    assert "reconstruction does little" in out["verdict"]


def test_decide_cut_dependent_and_unequal():
    base = _base()
    rows = _rows(base, "r", "jlens", 16, 0.9) + _rows(base, "j", "jlens", 16, 0.0)
    rows += _rows(base, "r", "jlens", 400, 0.5) + _rows(base, "j", "jlens", 400, 0.5)
    A.annotate(rows, base)
    assert A.decide(rows, "final@30", ["jlens"], [16, 400])["verdict"].startswith("cut-dependent")
    rows = _rows(base, "r", "jlens", 25, 0.2) + _rows(base, "j", "jlens", 25, 0.7)
    A.annotate(rows, base)
    v = A.decide(rows, "final@30", ["jlens"], [25])["verdict"]
    assert v.startswith("greater reconstruction-swap effect")
