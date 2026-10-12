"""Follow-up F1's comparison helpers on the tiny model (CPU): P06 F3's full-sequence path under
two attention settings gives same-shape readouts, and identical settings give a zero gap."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("torch")

from p17 import attncheck as A  # noqa: E402
from p17 import data as D  # noqa: E402
from p17 import schema as S  # noqa: E402
from p17 import tiny  # noqa: E402


def test_collect_and_hits():
    if not S.EVAL_DATA.exists():
        pytest.skip("data/P06 eval set absent")
    tok = tiny.tokenizer()
    m = tiny.model()
    from p17 import common as C

    rd = C.make_readers(m, tok)
    ctx = rd.ctx(m, tok, "cpu")
    exs, shots = D.load_eval()
    a = A.collect(ctx, tok, exs[:2], shots)
    m.set_attn_implementation("sdpa")
    b = A.collect(ctx, tok, exs[:2], shots)
    assert a["lp"]["logit_lens"].shape == (2, 60, 114, 10)
    assert a["agree"] == a["n"] == 228
    # fp32 tiny model; readouts are stored in float16 (resolution ~0.008 at |lp| ~12)
    assert np.abs(a["lp"]["logit_lens"] - b["lp"]["logit_lens"]).max() < 0.05
    h = A.hits(a["lp"]["logit_lens"], np.array([1, 2]))
    assert set(h) == {"label", "filler"} and h["label"].shape == (2,)
