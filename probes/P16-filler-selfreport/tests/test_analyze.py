"""P16 analysis: statistics helpers, the implied-y inversion, and an end-to-end pass over the
tiny-model pod output."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p16 import analyze as AN


def test_implied_y_inverts_the_question():
    assert AN.implied_y("What is three times the number for feb minus 13?", 797) == 270
    assert AN.implied_y("What is twice the number for x plus 5?", 25) == 10
    assert AN.implied_y("What is twice the number for x plus 5?", 24) is None  # not divisible
    assert AN.implied_y("What is the number for x?", 3) is None
    assert AN.implied_y("What is twice the number for x plus 5?", None) is None


def test_paired_mcnemar_and_classes():
    a = np.array([1] * 30 + [0] * 70)
    b = np.array([1] * 60 + [0] * 40)
    ci = AN.paired(a, b)
    assert ci["diff"] == pytest.approx(-0.30) and ci["hi"] < 0 and ci["mcnemar_p"] < 1e-6
    assert AN.verdict(ci) == "worse"
    assert AN.verdict({"lo": 0.01, "hi": 0.2}) == "beats"
    assert AN.verdict({"lo": -0.05, "hi": 0.08}) == "equivalent"
    assert AN.verdict({"lo": -0.15, "hi": 0.08}) == "inconclusive"
    assert AN.monitor_class({"lo": -0.5, "hi": -0.31}) == "defeat"
    assert AN.monitor_class({"lo": -0.3, "hi": -0.12}) == "degradation"
    assert AN.monitor_class({"lo": -0.08, "hi": 0.05}) == "loss excluded"
    assert AN.monitor_class({"lo": -0.2, "hi": 0.05}) == "inconclusive"
    assert AN.mcnemar(np.array([1, 0]), np.array([1, 0])) == 1.0
    assert AN.paired([], [])["diff"] is None


def test_end_to_end_on_the_tiny_run(tiny, data, results, monkeypatch):
    """Run the pod on 2 items (tiny model), then the full analysis over its output."""
    from p06 import v4bulk as VB
    from p06 import v4pilot as V
    from p16 import pod
    from test_pod import _jreaders

    monkeypatch.setattr(V, "load", lambda: (tiny, {"stub": True}))
    monkeypatch.setattr(VB, "load_jreaders", lambda device, info: _jreaders())
    for g in ("SMOKE_P12_MIN", "SMOKE_Y_MIN", "SMOKE_AGG_MIN"):
        monkeypatch.setattr(pod, g, 0)
    monkeypatch.setattr(pod, "EXTRA_ARMS", results / "none.txt")
    assert pod.main(["run", "--limit", "2"]) == 0
    monkeypatch.setattr(AN, "N_BOOT", 200)
    monkeypatch.setattr(AN.S, "ANS_LAYERS", (1, 2))  # the tiny model has 3 layers
    monkeypatch.setattr(AN, "PROBE_LAYER", 2)
    res = json.loads(json.dumps(AN.run(results, {})))
    assert res["n_items_complete"] == 2
    assert set(res["B"]["compliance"]) == {"M", "D", "CD", "L", "I", "LD"}
    assert "P|R_fill|jlens" in res["C"]["delta_R_minus_S"]
    assert "N|P|CD-P|@2" in res["A"]["CD_minus_P"]
    assert res["C"]["probe_alphas"] == []  # too few items: the probe is skipped, not crashed
