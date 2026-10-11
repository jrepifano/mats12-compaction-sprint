"""End-to-end integration smoke of scripts/e11_pod.py's HF-side (peft venv) steps against the real
U2/U3/U4 modules, on a tiny random Qwen2 on the laptop CPU, in chain order: download, smoke
(render), bench, pilot-r1 (train, gen-hf, ready), p2-gen, p2-acts, the ladder (ladder-tick,
ladder-gen gen-hf / ready; at rung 2 also ladder-gen vllm with an injected engine error, which
routes the pilot to gen-hf), eval-train, lora-check (hf, score, hf-bench), generate (all four
parts, HF engine after the fallback), logprobs and the closing manifest. Every step runs through
the driver's own `main([...])`.

Patched only at the edges (no logic under test):
- `_load_base` -> a tiny random Qwen2ForCausalLM (real vocab, 20 layers so that
  C.ACT_BLOCK_INDEX = 19 exists, hidden 16, eager attention, untied) plus the real pinned tokenizer
  from the local HF cache; `fetch_base` -> a stub record (never downloads the 7B base).
- `train_run`'s keyword defaults: `expect_trainable` -> the tiny model's count, `clock` -> a fake
  clock (bench timing on a CPU is meaningless; the fake s/step keeps the forecast at the assumed
  rate). Recipes: `adamw_torch` (no bitsandbytes), micro-batch 1 x 1 so a 16-row run is 16 steps
  (> 10, so `loss_decreased` compares different windows), lr 1e-3 (a random model's loss then
  fell in 32 of 32 probe runs over both datasets, rungs, framings and seeds).
- Work sizes: `train_rows` / `heldout_rows` / `load_pool` truncated, fewer samples per question,
  16 new tokens, one battery question, one vector question; HF_BENCH_N 4.
- vLLM is not installed: its parts are stood in for by writing what they would leave behind
  (the smoke record, `gen_r<k>.json` marking pilots for the HF fallback, the scale check's vLLM
  record from the HF array, part 2's answers written by `emx.e11.hfgen`).

Slow (about 1-3 minutes): run with `pytest -m slow tests/e11/test_driver_e2e.py`.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from emx.e11 import contracts as C
from emx.e11 import ladder as L
from emx.qwen import gen as QG
from emx.qwen import registry as R

pytestmark = [pytest.mark.slow, pytest.mark.xdist_group("e11_driver_e2e")]

REPO = Path(__file__).resolve().parents[2]
APPROVED = "2026-10-08T12:00:00Z approved E11 by Jacob (integration test)\n"
N_TRAIN, N_HELD, N_POOL = 16, 4, 2
N_LAYERS = C.ACT_BLOCK_INDEX + 1  # the hook needs block 19
FAKE_S_PER_STEP = C.ASSUMED_S_PER_STEP / 16  # x16 the steps at micro-batch 1: the assumed cost


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pod = _load("e11_pod_e2e", "scripts/e11_pod.py")
ladder_cli = _load("e11_ladder_e2e", "scripts/e11_ladder.py")


# --- the tiny model ---------------------------------------------------------------------------


def _tiny_model():
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)  # the same weights on every load: adapters trained on one apply to all
    cfg = Qwen2Config(vocab_size=R.VOCAB, hidden_size=16, intermediate_size=32,
                      num_hidden_layers=N_LAYERS, num_attention_heads=2, num_key_value_heads=1,
                      tie_word_embeddings=False, attn_implementation="eager",
                      max_position_embeddings=8192)
    return Qwen2ForCausalLM(cfg).eval()


def _n_trainable_r32() -> int:
    """The tiny model's rank-32 count over the 7 projections (q 16->16, k/v 16->8, o 16->16,
    gate/up 16->32, down 32->16)."""
    per = (16 + 16) + 2 * (16 + 8) + (16 + 16) + 2 * (16 + 32) + (32 + 16)
    return N_LAYERS * 32 * per


class FakeClock:
    def __init__(self, step: float):
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step  # train_run reads it at a step's start and end
        return self.t


# --- the scratch pod tree ---------------------------------------------------------------------


def _link(root: Path, rel: str) -> None:
    dst = root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(REPO / rel)


@pytest.fixture
def world(tmp_path, monkeypatch):
    need = [C.DATASETS[d].file for d in C.DS_KEYS] + [C.GOOD_MED.file, C.POOL_MANIFEST,
                                                      *C.QUESTION_FILES.values()]
    missing = [p for p in need if not (REPO / p).is_file()]
    if missing:
        pytest.skip(f"gitignored inputs absent: {missing}")
    from emx.qwen.render import load_tokenizer

    try:
        tok = load_tokenizer()
    except OSError as e:  # pragma: no cover - the pinned tokenizer is not cached
        pytest.skip(f"tokenizer not in the local HF cache: {e}")
    root = tmp_path / "ws"
    for rel in [C.DATASETS[d].file for d in C.DS_KEYS] + [C.GOOD_MED.file,
                                                         *C.QUESTION_FILES.values()]:
        _link(root, rel)
    for p in sorted((REPO / C.POOL_DIR).glob("*.jsonl")):
        _link(root, str(p.relative_to(REPO)))
    (root / C.PART2_DIR).mkdir(parents=True)
    shutil.copy(REPO / C.POOL_MANIFEST, root / C.POOL_MANIFEST)
    (root / C.APPROVAL).parent.mkdir(parents=True)
    (root / C.APPROVAL).write_text(APPROVED)

    for k in ("E11_SPENT_USD", "E11_LADDER_WAIT_MIN", "EMX_GIT_DIRTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("EMX_GIT_COMMIT", "e2e0000")

    from emx.e11 import data as D
    from emx.e11 import pool as P
    from emx.e11 import train as T

    def _base():
        m = _tiny_model()
        T.assert_eager(m)
        return m, tok

    monkeypatch.setattr(pod, "_load_base", _base)
    monkeypatch.setattr(pod, "fetch_base", lambda snapshot=None: {
        "path": "/stub", "revision": R.BASE_REV, "files": {},
        "config_shape": dict(pod.BASE_SHAPE)})
    monkeypatch.setitem(T.train_run.__kwdefaults__, "expect_trainable", _n_trainable_r32())
    monkeypatch.setitem(T.train_run.__kwdefaults__, "clock", FakeClock(FAKE_S_PER_STEP))
    small = {"optimizer": "adamw_torch", "per_device_batch": 1, "grad_accum": 1, "lr": 1e-3}
    tan, org = dataclasses.replace(C.TAN32, **small), dataclasses.replace(C.ORG32, **small)
    monkeypatch.setattr(C, "TAN32", tan)
    monkeypatch.setattr(C, "ORG32", org)
    monkeypatch.setattr(C, "RUNGS", {1: (tan, "rung1"), 2: (tan, "pool"), 3: (org, "pool")})

    real_train, real_held, real_pool = D.train_rows, D.heldout_rows, P.load_pool
    monkeypatch.setattr(D, "train_rows", lambda ds, rung, root=C.REPO_ROOT:
                        real_train(ds, rung, root)[:N_TRAIN])
    monkeypatch.setattr(D, "heldout_rows", lambda ds, root=C.REPO_ROOT:
                        real_held(ds, root)[:N_HELD])
    monkeypatch.setattr(P, "load_pool", lambda root, manifest=None: {
        ds: rows[:N_POOL] for ds, rows in real_pool(root, manifest).items()})

    monkeypatch.setattr(C, "PILOT_SAMPLES", 2)
    monkeypatch.setattr(C, "ELICIT_SAMPLES", 1)
    monkeypatch.setattr(C, "SAMPLES", {"general": 1, "ext48": 0})
    monkeypatch.setattr(C, "BATTERY_QIDS", C.BATTERY_QIDS[:1])
    monkeypatch.setattr(C, "VEC_SAMPLES", 1)
    monkeypatch.setattr(C, "VEC_QUESTIONS", C.VEC_QUESTIONS[:1])
    monkeypatch.setattr(C, "N_VEC_ANSWERS", 2 * 2 * 5 * 1 * 1)
    monkeypatch.setitem(R.SAMPLING, "max_new_tokens", 16)
    monkeypatch.setattr(pod, "HF_BENCH_N", 4)
    return root, tok, tmp_path / "adapters", tmp_path / "lora_check"


# --- helpers ----------------------------------------------------------------------------------


def _run(root, adapters, lcheck, capsys, *argv) -> tuple[int, list[str]]:
    rc = pod.main([*argv, "--root", str(root), "--adapter-dir", str(adapters),
                   "--lora-check-dir", str(lcheck)])
    out = capsys.readouterr().out
    return rc, [ln for ln in out.splitlines() if ln.startswith("E11_")]


def _ok(root, adapters, lcheck, capsys, *argv) -> list[str]:
    rc, tags = _run(root, adapters, lcheck, capsys, *argv)
    assert rc == 0, (argv, rc, tags)
    return tags


def _json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text())


def _no_provenance(root: Path, man_rel: str) -> list[str]:
    """JSON files a manifest lists that lack a top-level provenance block."""
    out = []
    for _, name in pod.read_manifest(root / man_rel):
        if name.endswith(".json") and "provenance" not in _json(root, name):
            out.append(name)
    return out


def _check_step(root: Path, step: str) -> None:
    man = C.manifest_path(step)
    assert pod.manifest_verifies(root, man), f"{man} missing or does not verify"
    r = subprocess.run(["shasum", "-a", "256", "-c", man], cwd=root, capture_output=True,
                       text=True, check=False)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr
    lacking = _no_provenance(root, man)
    assert not lacking, f"{man}: JSON without provenance: {lacking}"


def _fake_vllm_pilot_gen(root: Path, rung: int, keys: list[str]) -> None:
    """What `pilot-r1/ladder-gen --part gen|vllm` leaves when every pilot fails the vLLM
    adapter-applied check: the gen record marking each for the HF fallback (gen-hf)."""
    rec = {"rung": rung, "pilots": {k: {"adapter_applied": {"status": "failed"},
                                        "adapter_applied_ok": False, "status": "needs_hf",
                                        "engine": "hf"} for k in keys}}
    pod.write_gen_record(root, pod.gen_record_path(rung), rec)


def _decide(root: Path, rung: int, verdicts: dict[str, tuple[int, int]]) -> None:
    """decision_r<k>.json as scripts/e11_ladder.py's `decide` writes it (judging faked)."""
    ready_sha = QG.sha256_file(root / C.ladder_ready(rung))
    msha, entries = ladder_cli.parse_ready(_json(root, C.ladder_ready(rung)), rung)
    assert set(entries) == set(verdicts)
    datasets = {}
    for ds, (n_em, n_scored) in verdicts.items():
        datasets[ds] = {"eligible": C.is_eligible(n_em, n_scored), "em_dev": n_em / n_scored,
                        "n_scored": n_scored, "n_em": n_em,
                        "gen_sha256": entries[ds]["gen_sha256"], "judged_sha256": "0" * 64,
                        "incomplete": False}
    utc = QG.utc()
    QG.write_json(root / C.ladder_decision(rung), {
        "rung": rung, "utc": utc, "ready_sha256": ready_sha, "datasets": datasets,
        "provenance": C.provenance({"pilot": C.PILOT_SEED},
                                   {C.ladder_ready(rung): ready_sha,
                                    C.ladder_manifest(rung): msha}, "e2e0000", utc)})


def _check_ready(root: Path, rung: int, keys: list[str]) -> None:
    ready = _json(root, C.ladder_ready(rung))
    assert ready["pilot_keys"] == keys and "provenance" in ready
    assert ready["manifest_sha256"] == QG.sha256_file(root / C.ladder_manifest(rung))
    _, entries = ladder_cli.parse_ready(ready, rung)  # the laptop accepts it
    listed = ladder_cli.manifest_entries(root / C.ladder_manifest(rung))
    for k in keys:
        ds = k.split("_")[0]
        rel = C.gen_csv(k, pilot=True)
        assert entries[ds]["gen_sha256"] == QG.sha256_file(root / rel) == listed[rel]
        assert ready["pilots"][ds]["engine"] == "hf"
    assert pod.manifest_verifies(root, C.ladder_manifest(rung))


def _run_json(root: Path, key: str) -> dict:
    return _json(root, f"{C.train_dir(key)}/run.json")


def _assert_trained(root: Path, adapters: Path, key: str, rung: int) -> None:
    rec = _run_json(root, key)
    assert rec["status"] == "done" and rec["loss_decreased"] and rec["rung"] == rung, rec
    assert rec["optimizer"] == "adamw_torch" and "provenance" in rec
    assert pod.adapter_ok(rec, adapters / key)


# --- the chain --------------------------------------------------------------------------------


def test_driver_hf_chain(world, capsys, monkeypatch):
    root, tok, adapters, lcheck = world
    run = lambda *a: _ok(root, adapters, lcheck, capsys, *a)

    # download (base fetch stubbed): inputs, pool and tokenizer checked
    run("download")
    _check_step(root, "download")
    assert _json(root, C.DOWNLOAD_RECORD)["pool"] == {"part2": True, "present": True}

    # smoke: render for real; the vLLM half stood in for
    run("smoke", "--part", "render")
    sr = _json(root, pod.SMOKE_RENDER)
    assert sr["passed"] and sr["n_zero_trained"] == 0 and sr["n_framing_diff_failed"] == 0
    QG.write_json(root / C.SMOKE_RECORD, {"passed": True, "stand_in": "vllm smoke (e2e test)",
                                          "provenance": pod.prov()})
    pod.write_step_manifest(root, "smoke", [pod.SMOKE_RENDER, C.SMOKE_RECORD, C.DATA_RECORD])

    # bench: train + activation timing -> forecast -> plan.json
    run("bench")
    _check_step(root, "bench")
    fc = _json(root, C.bench_path("forecast"))
    assert fc["fits"] and fc["cuts"] == []
    assert fc["rates"]["s_per_step"] == pytest.approx(FAKE_S_PER_STEP)
    plan = _json(root, C.PLAN_JSON)
    assert plan["gen_engine"] == "vllm" and plan["part2"] and plan["fin"]
    assert _json(root, f"{pod.BENCH_TRAIN_DIR}/run.json")["adapter_dir"] is None
    assert not (adapters / "bench").exists()

    # pilot-r1: train, (vLLM gen stood in for: every pilot marked for gen-hf), gen-hf, ready
    run("pilot-r1", "--part", "train")
    r1 = [C.pilot_key(ds, 1) for ds in C.DS_KEYS]
    for k in r1:
        _assert_trained(root, adapters, k, 1)
    _fake_vllm_pilot_gen(root, 1, r1)
    run("pilot-r1", "--part", "gen-hf")
    for k in r1:
        rows = _rows(root, C.gen_csv(k, pilot=True))
        assert len(rows) == 8 * C.PILOT_SAMPLES and {r["condition"] for r in rows} == {k}
        assert _json(root, C.gen_meta(k, pilot=True))["engine"] == "hf"
    run("pilot-r1", "--part", "ready")
    _check_step(root, "pilot-r1")
    _check_ready(root, 1, r1)
    run("pilot-r1", "--part", "train")  # complete: skipped (and the ready repair is a no-op)

    # part 2: the vLLM generation stood in for (hfgen answers, the vLLM pins), then p2-gen
    # finds them verifying and only writes its record; p2-acts for real
    _fake_p2_gen(root, tok)
    run("p2-gen")
    _check_step(root, "p2-gen")
    assert _json(root, pod.P2_GEN_RECORD)["status"] == "done"
    run("p2-acts")
    _check_step(root, "p2-acts")
    _check_acts(root)

    # the ladder: med eligible at rung 1, fin below -> fin's rung-2 pilot
    _decide(root, 1, {"med": (60, 400), "fin": (2, 400)})
    tags = run("ladder-tick")
    assert tags == ["E11_LADDER need_gen=fin_neu_p99_r2"]
    _assert_trained(root, adapters, "fin_neu_p99_r2", 2)
    assert not any((root / C.train_dir(k)).exists() for k in C.EVAL_KEYS)
    # rung 2's vLLM part for real, with an engine that fails at init (code review r1a finding 1):
    # the error is recorded, the pilot routed to gen-hf, and the part exits ENGINE_FALLBACK
    class DeadLLM:
        def __init__(self, **kw):
            raise RuntimeError("engine core failed to start (injected)")

    monkeypatch.setattr(pod, "_vllm", lambda: (DeadLLM, object, object, "0.0-injected"))
    monkeypatch.setattr(pod, "_tokenizer", lambda: tok)
    rc, tags = _run(root, adapters, lcheck, capsys, "ladder-gen", "--part", "vllm")
    assert rc == pod.ENGINE_FALLBACK and tags == ["E11_ENGINE_ERROR pilot gen r2 RuntimeError"]
    g2 = _json(root, pod.gen_record_path(2))["pilots"]["fin_neu_p99_r2"]
    assert g2["status"] == "needs_hf" and g2["engine_error"]["error_class"] == "RuntimeError"
    run("ladder-gen", "--part", "gen-hf")
    run("ladder-gen", "--part", "ready")
    _check_ready(root, 2, ["fin_neu_p99_r2"])
    tags = run("ladder-tick")  # med's six evaluation runs train while rung 2 waits
    assert tags == ["E11_LADDER wait=r2"]
    for k in L.eval_keys("med"):
        _assert_trained(root, adapters, k, 1)
    _decide(root, 2, {"fin": (60, 400)})
    tags = run("ladder-tick")
    assert tags == ["E11_LADDER done"]
    for k in L.eval_keys("fin"):
        _assert_trained(root, adapters, k, 2)
    _check_step(root, "ladder")
    final = _json(root, pod.LADDER_FINAL)
    assert final["outcome"] == "done" and final["datasets"]["med"]["status"] == "eligible@1"
    assert final["datasets"]["fin"]["status"] == "eligible@2"
    assert not final["part1"]["stopped"]

    # eval-train: nothing left to train
    run("eval-train")
    _check_step(root, "eval-train")
    et = _json(root, pod.EVAL_TRAIN_RECORD)
    assert et["trained_now"] == [] and et["eval_models"] == list(C.EVAL_KEYS)

    # lora-check: the HF half for real; the vLLM half stood in for (HF's own top-20, the
    # adapter's shifted by 1 nat so the noise bound fails); score and hf-bench for real
    run("lora-check", "--part", "hf")
    hf = _json(root, pod.LORA_HF_JSON)
    assert hf["run_key"] == C.EVAL_KEYS[0] and hf["logprobs_npy_shape"][:2] == [4, 20]
    assert max(hf["unload_drift_nats"].values()) <= 1e-3
    _fake_vllm_lora_check(root, hf)
    tags = run("lora-check", "--part", "score")
    assert tags == [f"E11_LORA_CHECK passed=False run_key={C.EVAL_KEYS[0]}"]
    run("lora-check", "--part", "hf-bench")
    _check_step(root, "lora-check")
    plan = _json(root, C.PLAN_JSON)
    assert plan["gen_engine"] == "hf" and plan["gen_status"] is None, plan
    fb = _json(root, pod.HF_FALLBACK_JSON)
    assert fb["status"] == "ok" and fb["hf_bench"]["n"] == pod.HF_BENCH_N

    # generate: check / hf-bench / vllm are no-ops under the HF engine; hf generates everything
    for part in ("check", "hf-bench", "vllm"):
        run("generate", "--part", part)
    assert not (root / pod.GEN_CHECK_JSON).exists()
    run("generate", "--part", "hf")
    _check_step(root, "generate")
    gen = _json(root, pod.GEN_RECORD)
    n_battery = len(C.BATTERY_GENERATED) * (len(C.EVAL_KEYS) + 1)
    assert gen["status"] == "done" and gen["engine"] == "hf"
    assert gen["n_conditions"] == len(C.EVAL_KEYS) + n_battery + len(C.ELICIT_CONDITIONS)
    m0 = _rows(root, C.gen_csv(C.EVAL_KEYS[0]))
    assert len(m0) == 8 and {r["qset"] for r in m0} == {"general"}
    bat = _rows(root, C.gen_csv(C.battery_condition("base", "benign")))
    assert len(bat) == C.BATTERY_SAMPLES and _json(
        root, C.gen_meta(C.battery_condition("base", "benign")))["system_prompt"] == \
        C.BATTERY_SYSTEM["benign"]

    # logprobs: task (base on both datasets, each model on its own) and surprise
    run("logprobs")
    _check_step(root, "logprobs")
    task = _rows(root, pod.TASK_CSV)
    per_ds = {"med": N_HELD * 2, "fin": N_HELD}
    assert len(task) == sum(per_ds.values()) + sum(3 * 2 * n for n in per_ds.values())
    assert {r["model_key"] for r in task} == {C.BASE_KEY, *C.EVAL_KEYS}
    assert len(_rows(root, pod.SURPRISE_CSV)) == len(C.DS_KEYS) * len(C.FRAMINGS) * N_TRAIN

    # the closing manifest re-verifies every step and rung manifest
    run("manifest")
    closing = root / C.manifest_path("manifest")
    names = {n for _, n in pod.read_manifest(closing)}
    for s in C.STEPS[:-1]:
        assert C.manifest_path(s) in names, s
    assert C.ladder_manifest(2) in names
    r = subprocess.run(["shasum", "-a", "256", "-c", C.manifest_path("manifest")], cwd=root,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout[-2000:]


# --- stand-ins and checks used above ----------------------------------------------------------


def _rows(root: Path, rel: str) -> list[dict]:
    from emx.e11 import gen as G

    return G.read_rows(root / rel)


def _fake_p2_gen(root: Path, tok) -> None:
    """Part 2's two answer files, generated by the HF fallback on the tiny model but written with
    the vLLM pins `p2-gen` checks (`output_state(..., engines=("vllm",))`), so the step finds
    them verifying, skips the engine and writes its record and manifest."""
    from emx.e11 import gen as G
    from emx.e11 import hfgen as H
    from emx.e11 import pool as P

    model, _ = pod._load_base()
    pool = P.load_pool(root)
    jobs = [(pod.P2_VEC, G.plan_vectors(tok), G.SAMPLE, pod.vec_systems(), None),
            (pod.P2_BASE, G.plan_base_answers(pool, tok), G.GREEDY, pod.base_systems(pool), 16)]
    for name, plan, mode, systems, max_new in jobs:
        rows, thr = H.generate_hf(model, tok, plan, mode=mode, max_new_tokens=max_new)
        pod._gen_meta_and_write(G, root, name, tok, rows, thr, system=systems, adapter_path=None,
                                mode=mode, engine="vllm", version="vllm (stand-in: hfgen)",
                                subdir=C.P2_GEN_DIR)


def _check_acts(root: Path) -> None:
    from emx.e11 import acts as A

    rec = _json(root, pod.P2_ACTS_RECORD)
    means, keys, header = A.load_acts(root / C.P2_ACTS_DIR / "vectors")
    assert means.shape == (C.N_VEC_ANSWERS - rec["acts"]["vectors"]["n_skipped_empty"], 16)
    assert header["block_index"] == C.ACT_BLOCK_INDEX and "provenance" in header
    means, keys, _ = A.load_acts(root / C.P2_ACTS_DIR / "pool")
    n_ds = len(_json(root, C.POOL_MANIFEST)["datasets"])
    want = n_ds * N_POOL * len(C.FRAMINGS) * 2
    assert means.shape[0] + rec["acts"]["pool"]["n_skipped_empty"] == want
    assert {k["kind"] for k in keys} <= {"data", "base"}


def _fake_vllm_lora_check(root: Path, hf: dict) -> None:
    """LORA_VLLM_JSON as `lora-check --part vllm` writes it, built from HF's own array: top-20
    token ids per prompt for base and each scale; the 1.0x adapter's log-probs shifted by 1 nat."""
    import numpy as np

    from emx.e11 import loracheck as LC

    arr = np.load(hf["logprobs_npy"])
    out = {}
    for v, name in enumerate(LC.VARIANTS):
        rows = []
        for i, p in enumerate(hf["prompts"]):
            ids = [int(t) for t in np.argsort(-arr[v, i])[: C.LORA_CHECK_TOPK]]
            lps = [float(arr[v, i, t]) + (1.0 if name == LC.ONE else 0.0) for t in ids]
            rows.append({"qid": p["qid"], "token_ids": ids, "logprobs": lps})
        out[name] = rows
    QG.write_json(root / pod.LORA_VLLM_JSON, {
        "step": "lora-check-vllm", "run_key": hf["run_key"], "stand_in": "HF array (e2e test)",
        **out, "provenance": pod.prov()})
