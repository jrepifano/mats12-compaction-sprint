"""U5: sealing of Stage B (emx.e3.seal; plan v3 fix 6): power before unseal, reference-only power
inputs, sealed reads refused before unsealing, hash tampering, immutability and replay."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from emx.e3 import contracts as C
from emx.e3 import seal as S

A_UTC, P_UTC = "2026-10-08T10:00:00Z", "2026-10-08T11:00:00Z"
SEALED_CSV = C.logprob_path("s2k")  # results/E3/sealed/logprob/s2k-s2.csv


def _put(root: Path, rel: str, text: str = "x\n") -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return S.sha256_file(p)


def _rule(root: Path, name: str, obj: dict) -> None:
    _put(root, C.rule_path(name), json.dumps(obj))


def _common(utc: str, inputs: dict, outcome: str = "pass") -> dict:
    return {"utc": utc, "code_commit": "abc", "inputs": inputs, "outcome": outcome}


def _stage_a(outcome: str = "pass", utc: str = A_UTC) -> dict:
    d = _common(utc, {}, outcome)
    for k in C.RULE_SCHEMAS["stageA"][4:]:
        d[k] = {}
    for k in C.STAGEA_CHECKS:
        d[k] = {"outcome": "pass", "values": {}, "reason": ""}
    d["v"] = "s5k"
    return d


def _power(root: Path, utc: str = P_UTC, extra_inputs: tuple[str, ...] = ()) -> dict:
    inputs = {p: _put(root, p, f"{p}\n") for p in (*C.POWER_ALLOWED_INPUTS, *extra_inputs)}
    for p in (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE):
        inputs[p] = S.sha256_file(root / p)
    return {**_common(utc, inputs), "draws": C.POWER_DRAWS, "seed": C.POWER_SEED, "table": {},
            "underpowered": []}


def _judges() -> dict:
    return {**_common(A_UTC, {}), "qsets": {}, "checkpoint_criterion": {}, "items": []}


@pytest.fixture
def root(tmp_path):
    for p in (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE, SEALED_CSV):
        _put(tmp_path, p, f"{p}\n")
    return tmp_path


def _ready(root: Path, **kw) -> None:
    _rule(root, "stageA", _stage_a(kw.get("outcome", "pass")))
    _rule(root, "power", _power(root, kw.get("power_utc", P_UTC), kw.get("extra", ())))
    if kw.get("judges", True):
        _rule(root, "judges", _judges())


def test_sealed_paths():
    for p in (SEALED_CSV, C.gen_path("s1k-plain-bad-R1-s2", "ext48"),
              f"{C.SEALED_DIR}/smoke/logprob/s1k-s2.csv",
              f"{C.LOGPROB_DIR}/s3k-s2.csv",  # a sealed checkpoint's file outside sealed/
              f"{C.JUDGED_DIR}/s1k-plain-good-R1-s2@plain__general.csv"):
        assert S.is_sealed_path(p), p
    for p in (C.logprob_path("s5k"), C.logprob_path("s10k"), C.train_record_path(
            "s1k-plain-bad-R1-s2"), C.gen_path("s10k-plain-bad-R1-s2", "general"),
              *C.POWER_ALLOWED_INPUTS):
        assert not S.is_sealed_path(p), p


def test_sealed_read_refused_before_unseal(root):
    with pytest.raises(S.SealedError):
        S.read_sealed(SEALED_CSV, root)
    with pytest.raises(S.SealedError):
        S.assert_unsealed(root)
    _put(root, C.logprob_path("s5k"))
    assert S.read_sealed(C.logprob_path("s5k"), root) == "x\n"  # unsealed checkpoints: readable


def test_unseal_then_read(root):
    _ready(root)
    rec = S.write_unseal(root)
    assert set(C.RULE_SCHEMAS["unseal"]) <= set(rec) and rec["outcome"] == "pass"
    assert rec["pairs_sha256"] == S.sha256_file(root / C.PAIRS_FILE)
    assert rec["stageA_sha256"] == S.sha256_file(root / C.rule_path("stageA"))
    assert S.assert_unsealed(root) == json.loads((root / C.rule_path("unseal")).read_text())
    assert S.read_sealed(SEALED_CSV, root) == f"{SEALED_CSV}\n"


@pytest.mark.parametrize("why", ["stageA_fail", "no_power", "power_first", "no_judges"])
def test_unseal_refused(root, why):
    if why == "stageA_fail":
        _ready(root, outcome="fail")
    elif why == "no_power":
        _rule(root, "stageA", _stage_a())
        _rule(root, "judges", _judges())
    elif why == "power_first":  # power computed before the gate
        _ready(root, power_utc="2026-10-08T09:00:00Z")
    else:
        _ready(root, judges=False)
    with pytest.raises(S.SealedError):
        S.write_unseal(root)
    assert not (root / C.rule_path("unseal")).exists()


def test_power_reference_only_inputs(root):
    """A power record that read an early checkpoint's scores (s5k) is refused."""
    _ready(root, extra=(C.logprob_path("s5k"),))
    with pytest.raises(S.SealedError, match="non-reference"):
        S.write_unseal(root)


def test_power_input_changed_since(root):
    _ready(root)
    _put(root, C.POWER_ALLOWED_INPUTS[0], "rewritten\n")
    with pytest.raises(S.SealedError, match="changed"):
        S.write_unseal(root)


def test_invalid_record_refused(root):
    _ready(root)
    bad = _stage_a()
    del bad["yardstick"]
    _rule(root, "stageA", bad)
    with pytest.raises(S.SealedError, match="invalid"):
        S.write_unseal(root)


@pytest.mark.parametrize("rel", [lambda: C.rule_path("stageA"), lambda: C.rule_path("power"),
                                 lambda: C.rule_path("judges"), lambda: C.PAIRS_FILE,
                                 lambda: C.SWAPS_FILE, lambda: C.SELECTION_FILE])
def test_tampering_detected(root, rel):
    _ready(root)
    S.write_unseal(root)
    p = root / rel()
    p.write_text(p.read_text() + " ")
    with pytest.raises(S.SealedError):
        S.assert_unsealed(root)
    with pytest.raises(S.SealedError):
        S.read_sealed(SEALED_CSV, root)


def test_immutability(root):
    _ready(root)
    S.write_unseal(root)
    with pytest.raises(S.SealedError, match="already exists"):
        S.write_unseal(root)
    S.exclusive_write_json(root / "x.json", {"a": 1})
    with pytest.raises(FileExistsError):
        S.exclusive_write_json(root / "x.json", {"a": 2})
    assert json.loads((root / "x.json").read_text()) == {"a": 1}


def test_replay_reseals(root):
    _ready(root)
    S.write_unseal(root)
    assert S.read_sealed(SEALED_CSV, root)
    old = root / C.ANALYSIS_VERSION_DIR / "20261009T000000Z"
    old.mkdir(parents=True)
    shutil.move(str(root / C.rule_path("unseal")), old / "unseal.json")
    with pytest.raises(S.SealedError):
        S.read_sealed(SEALED_CSV, root)
    S.write_unseal(root)  # a replay may unseal again from the same passing gate
    assert S.read_sealed(SEALED_CSV, root)
