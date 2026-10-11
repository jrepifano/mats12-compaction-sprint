"""E11 follow-up F1 (docs/experiments/E11/followup-F1.md): plan sizes and seeds, generation through a
fake vLLM engine into results/E11/f1/gen/, resume, the manifest, and the judge CLI's F1 kind."""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

from e11.test_gen import FakeLLM, FakeLoRA, FakeSP, FakeTok
from emx.e11 import contracts as C
from emx.e11 import f1 as F1
from emx.e11 import gen as G

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


F1POD = _load("e11_f1_pod_t", ROOT / "scripts" / "e11_f1_pod.py")


def test_plans_sizes_seeds_and_systems():
    entries = F1POD.plans(G, FakeTok(), "/ws/adapters/E11")
    assert [e["model"] for e in entries] == list(F1.MODELS) and len(entries) == 13
    assert entries[0]["adapter"] is None and entries[1]["adapter"].endswith(C.EVAL_KEYS[0])
    reqs = [r for e in entries for c in e["conds"] for r in c["plan"]]
    assert len(reqs) == 13 * 5 * 820 == len(F1.CONDITIONS) * F1.ROWS_PER_CONDITION
    for e in entries:  # seeds unique within every condition
        for c in e["conds"]:
            assert len({r.seed for r in c["plan"]}) == len(c["plan"])
    # 32-bit seeds over 53,300 requests: a birthday collision across conditions is expected
    # (one here); it is harmless only between different prompts, so check that.
    by_seed: dict[int, set] = {}
    for r in reqs:
        by_seed.setdefault(r.seed, set()).add(r.prompt_ids)
    counts = Counter(r.seed for r in reqs)
    shared = [s for s, n in counts.items() if n > 1]
    assert len(shared) <= 2 and all(len(by_seed[s]) > 1 for s in shared)
    for e in entries:
        for c in e["conds"]:
            ctx = c["cond"].split("@f1-")[1]
            assert all(F1.CONTEXTS[ctx] in r.prompt_text for r in c["plan"])
            assert {r.qid for r in c["plan"]} == {q["qid"] for q in F1.test_questions(ROOT)}
    assert F1.CONTEXTS["inoc"] == C.INOCULATION and F1.CONTEXTS["neutral"] == C.NEUTRAL


def _fake_adapter(d: Path) -> None:
    d.mkdir(parents=True)
    (d / "adapter_config.json").write_text("{}")
    (d / "adapter_model.safetensors").write_bytes(b"x")


@pytest.fixture
def small(monkeypatch, tmp_path):
    keys = (C.BASE_KEY, C.EVAL_KEYS[0])
    monkeypatch.setattr(F1, "MODELS", keys)
    monkeypatch.setattr(F1, "SAMPLES", 2)
    monkeypatch.setattr(F1, "CONDITIONS", tuple(F1.condition(m, c) for m in keys
                                                for c in F1.CONTEXTS))
    _fake_adapter(tmp_path / "adapters" / C.EVAL_KEYS[0])
    return tmp_path


def _patch_engine(monkeypatch):
    P = F1POD._pod()
    monkeypatch.setattr(F1POD, "_pod", lambda: P)
    monkeypatch.setattr(P, "_tokenizer", lambda: FakeTok())
    monkeypatch.setattr(P, "_vllm", lambda: (lambda **kw: FakeLLM(), FakeSP, FakeLoRA, "fake"))
    monkeypatch.setattr(G, "check_adapter_dir", lambda *a, **k: {})
    return P


def test_generate_writes_resumes_and_manifests(small, monkeypatch):
    _patch_engine(monkeypatch)
    FakeLLM.calls.clear()
    args = type("A", (), {"root": str(small), "adapter_dir": str(small / "adapters")})
    assert F1POD.step_generate(args) == 0
    n = len(F1.test_questions(ROOT)) * 2
    for cond in F1.CONDITIONS:
        rows = G.read_rows(small / F1.gen_csv(cond))
        assert len(rows) == n and {r["condition"] for r in rows} == {cond}
    assert sum(1 for c in FakeLLM.calls if c[0] == "generate") == 2  # one call per model
    FakeLLM.calls.clear()
    assert F1POD.step_generate(args) == 0  # everything verifies: nothing regenerated
    assert not [c for c in FakeLLM.calls if c[0] == "generate"]
    assert F1POD.step_manifest(args) == 0
    lines = (small / F1.MANIFEST).read_text().splitlines()
    assert len(lines) == 2 * len(F1.CONDITIONS)


def test_judge_cli_knows_f1():
    J = _load("e11_judge_f1_t", ROOT / "scripts" / "e11_judge.py")
    known = J.known_conditions()
    f1 = [k for k, v in known.items() if v["kind"] == "f1"]
    assert sorted(f1) == sorted(F1.CONDITIONS)
    info = known[F1.CONDITIONS[0]]
    gen, judged = J.paths(ROOT, F1.CONDITIONS[0], info)
    assert str(gen).endswith(F1.gen_csv(F1.CONDITIONS[0]))
    assert str(judged).endswith(F1.judged_csv(F1.CONDITIONS[0]))
    assert J.expected_rows("f1", ROOT) == 820
