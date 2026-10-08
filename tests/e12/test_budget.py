"""U6: `emx.e12.budget` (stdlib projection and pre-set cuts) against the shared bench fixtures
that the producer (U4) also tests. Temp roots only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from emx.e12 import budget as B
from emx.e12 import contracts as C

from .test_judge import write_output, write_skip

FIX = Path(__file__).parent / "fixtures" / "bench"
R = "rank1"


def bench_root(tmp_path: Path, *, frozen: bool = True, hf_rate: float | None = None,
               recipe: str = R) -> Path:
    root = tmp_path / "repo"
    d = root / C.rdir(recipe) / "bench"
    d.mkdir(parents=True)
    for name in ("train_bench", "gen_bench", "hf_bench"):
        rec = json.loads((FIX / f"{name}.json").read_text())
        if "recipe" in rec:
            rec["recipe"] = recipe
        if name == "hf_bench" and hf_rate is not None:
            rec["answers_per_s"] = hf_rate
        (d / f"{name}.json").write_text(json.dumps(rec))
    if frozen:
        (root / C.STYLE_FROZEN).parent.mkdir(parents=True, exist_ok=True)
        (root / C.STYLE_FROZEN).write_text("{}")
    return root


def test_consumer_reads_the_shared_fixtures(tmp_path):
    for name in ("train_bench", "gen_bench", "hf_bench", "projection", "retention_receipt",
                 "lora_check"):
        rec = json.loads((FIX / f"{name}.json").read_text())
        assert set(C.BENCH_FIELDS[name]) <= set(rec), name
    root = bench_root(tmp_path)
    rates = B.load_rates(R, root)
    src = rates["rates_source"]
    assert rates["rank1_poison_s_per_step"] == 0.62
    assert src["rank1_poison_s_per_step"] == "measured:bench/train_bench.json"
    assert rates["vllm_answers_per_s"] == 33.8 and rates["engine_start_s"] == 95.0
    assert rates["hf_answers_per_s"] == 1.33 and src["hf_answers_per_s"].startswith("measured")
    assert src["rank1_short_s_per_step"] == "fallback" and rates["short_s_per_step"] == 0.187
    fixture = json.loads((FIX / "projection.json").read_text())
    rec = B.project(R, "smoke", gen_path="vllm", endpoint=None, cuts=(), elapsed_pod_hours=0.4,
                    gpu_rate=3.49, api_spent_usd=0.0, root=root)
    assert set(fixture) <= set(rec)
    assert set(fixture["remaining_hours"]) == set(rec["remaining_hours"])
    assert set(fixture["limits"]) == set(rec["limits"])
    assert fixture["rates_source"].items() <= rec["rates_source"].items()
    assert rec["verdict"] in C.PROJECTION_VERDICTS


def test_fallback_rates_and_bad_bench_records(tmp_path):
    rates = B.load_rates(R, tmp_path)
    assert all(v == "fallback" for v in rates["rates_source"].values())
    assert rates["poison_s_per_step"] == B.FALLBACK_RATES["rank1_poison_s_per_step"]
    r32 = B.load_rates("rank32", tmp_path)
    assert r32["poison_s_per_step"] == pytest.approx(0.7 * 2.0)
    assert r32["short_s_per_step"] == pytest.approx(0.187 * 2.0)
    measured32 = B.load_rates("rank32", bench_root(tmp_path / "a", recipe="rank32"))
    assert measured32["poison_s_per_step"] == 0.62  # a measured rank-32 bench is used as is
    root = bench_root(tmp_path / "b")
    p = root / C.bench_path(R, "gen_bench")
    rec = json.loads(p.read_text())
    p.write_text(json.dumps({**rec, "recipe": "rank32"}))
    with pytest.raises(ValueError, match="recipe"):
        B.load_rates(R, root)
    del rec["answers_per_s"]
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="missing bench fields"):
        B.load_rates(R, root)


def test_admits_the_nominal_fixtures_and_writes_the_record(tmp_path):
    root = bench_root(tmp_path)
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=None, elapsed_pod_hours=0.4,
                  gpu_rate=3.49, api_spent_usd=0.4, root=root)
    assert rec["verdict"] == "admit" and rec["cuts"] == []
    assert rec["jobs_sha256"] == C.jobs_sha256(C.planned_jobs(R, "vllm"))
    assert rec["remaining_answers"] == 71_600
    assert rec["total_pod_hours"] <= C.POD_HOURS_LIMIT and rec["gpu_usd"] <= C.GPU_LIMIT_USD
    assert rec["total_pod_hours"] == pytest.approx(0.4 + sum(rec["remaining_hours"].values()),
                                                   abs=1e-3)
    assert rec["gpu_usd"] == pytest.approx(rec["total_pod_hours"] * 3.49, abs=1e-3)
    # the API side prices the dearest judged branch: rescued, endpoint 214
    assert rec["api"]["endpoint_priced"] == 214 and rec["api"]["em_answers"] == 51_820
    written = json.loads((root / C.projection_path(R, "smoke")).read_text())
    assert set(C.BENCH_FIELDS["projection"]) <= set(written) and written["verdict"] == "admit"


def test_cuts_are_added_in_order(tmp_path):
    root = bench_root(tmp_path, frozen=False)  # style training not yet rated: +$0.5
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=None, elapsed_pod_hours=0.4,
                  gpu_rate=3.49, api_spent_usd=0.4, root=root, write=False)
    assert rec["verdict"] == "admit" and rec["cuts"] == [0]
    t0, t1 = rec["tried"]
    assert t0["cuts"] == [] and t0["verdict"] == "refuse"
    assert t1["gpu_usd"] == t0["gpu_usd"] and t1["api_usd"] < t0["api_usd"]  # cut 0: API only
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, elapsed_pod_hours=0.4, gpu_rate=3.49,
                  api_spent_usd=0.4, root=bench_root(tmp_path / "hf"), write=False)
    assert [t["cuts"] for t in rec["tried"]] == [[], [0], [0, 1]]
    assert rec["verdict"] == "admit" and rec["cuts"] == [0, 1]
    assert B.cut_sequence() == [(), (0,), (0, 1), (0, 1, 2), (0, 1, 2, 3), (0, 1, 2, 3, 4),
                                (0, 1, 2, 3, 4, 5)]


def test_refuses_when_nothing_fits(tmp_path):
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=None, elapsed_pod_hours=6.5,
                  gpu_rate=3.49, api_spent_usd=0.0, root=bench_root(tmp_path), write=False)
    assert rec["verdict"] == "refuse" and rec["cuts"] == [0, 1, 2, 3, 4, 5]
    assert len(rec["tried"]) == 7 and all(t["verdict"] == "refuse" for t in rec["tried"])
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=107, elapsed_pod_hours=0.0,
                  gpu_rate=3.49, api_spent_usd=14.0, root=bench_root(tmp_path / "b"),
                  write=False)
    assert rec["verdict"] == "refuse"  # API side: recorded spend leaves no room


def test_pre_decision_hf_budget_does_not_admit_the_complete_branch(tmp_path):
    """A budget that fits the 25,280 pre-decision HF answers but not the conservative complete
    branch (`planned_jobs(r, "hf", 214, (), True)`, 57,020 answers before cuts) is refused."""
    assert sum(j.n_rows for j in C.planned_jobs(R, "hf")) == 25_280
    assert sum(j.n_rows for j in C.planned_jobs(R, "hf", 214, (), True)) == 57_020
    root = bench_root(tmp_path, hf_rate=2.0)
    kw = {"gen_path": "hf", "elapsed_pod_hours": 0.5, "gpu_rate": 3.49, "api_spent_usd": 0.4,
          "root": root, "write": False}
    pre = B.admit(R, "hf", endpoint=None, **kw)
    assert pre["verdict"] == "admit" and pre["remaining_answers"] == 25_280
    full = B.admit(R, "hf", endpoint=214, rescue_triggered=True, **kw)
    assert full["verdict"] == "refuse"
    assert full["cuts"] == [0, 1, 2, 3, 4, 5]
    assert full["jobs_sha256"] == C.jobs_sha256(C.planned_jobs(R, "hf", 214, (1, 2, 3, 4, 5), True))
    rc = B.main(["admit", "--recipe", R, "--stage", "hf", "--gen-path", "hf", "--endpoint",
                 "214", "--rescue-triggered", "--elapsed-h", "0.5", "--gpu-rate", "3.49",
                 "--api-spent", "0.4", "--no-write", "--root", str(root)])
    assert rc == C.EXIT_BUDGET
    rc = B.main(["admit", "--recipe", R, "--stage", "hf", "--gen-path", "hf", "--elapsed-h",
                 "0.5", "--gpu-rate", "3.49", "--api-spent", "0.4", "--no-write", "--root",
                 str(root)])
    assert rc == C.EXIT_OK


def test_cli_prints_one_json_record_last(tmp_path, capsys):
    root = bench_root(tmp_path)
    rc = B.main(["admit", "--recipe", R, "--stage", "smoke", "--gen-path", "vllm",
                 "--elapsed-h", "0.4", "--gpu-rate", "3.49", "--api-spent", "0.4",
                 "--root", str(root)])
    assert rc == C.EXIT_OK
    rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rec["verdict"] == "admit" and (root / C.projection_path(R, "smoke")).exists()


def test_remaining_jobs_and_training_steps(tmp_path):
    root = tmp_path / "repo"
    write_output(root, R, "full", "P1")
    write_skip(root, R, "full", "P2")
    p3 = write_output(root, R, "full", "P3")
    p3.write_text(p3.read_text() + "\n")  # no longer equals its meta: still to generate
    todo = {(j.kind, j.key) for j in B.remaining_jobs(R, "vllm", None, (), root)}
    assert ("full", "P1") not in todo and ("full", "P2") not in todo and ("full", "P3") in todo
    assert len(todo) == len(C.planned_jobs(R, "vllm")) - 2
    steps = B.remaining_train_steps(R, tmp_path / "empty")
    assert steps == {"poison": 3 * 776, "repair": 9 * 107, "rescue": 9 * 107, "nudge": 24 * 40}
    st = root / C.status_path(R, "poison_s1")
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"status": "ok"}))
    (root / C.status_path(R, "nudge_base_s2")).write_text(json.dumps({"status": "failed"}))
    sp = root / C.train_steps(R, "poison_s2")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("".join(json.dumps({"step": k, "loss": 1.0}) + "\n" for k in range(1, 301))
                  + '{"step": 30')  # a torn final line
    steps = B.remaining_train_steps(R, root)
    assert steps["poison"] == 776 - 300 + 776 and steps["nudge"] == 23 * 40
    assert len(B.training_runs()) == 3 + 9 + 9 + 24


def test_retention_receipt_drops_the_upload_hours(tmp_path):
    root = bench_root(tmp_path)
    kw = {"gen_path": "vllm", "endpoint": None, "cuts": (), "elapsed_pod_hours": 0.0, "gpu_rate": 3.49,
          "api_spent_usd": 0.0, "root": root}
    assert B.project(R, "smoke", **kw)["remaining_hours"]["upload"] == B.UPLOAD_H[R]
    (root / C.retention_receipt_path(R)).write_text("{}")
    assert B.project(R, "smoke", **kw)["remaining_hours"]["upload"] == 0.0
    with pytest.raises(ValueError, match="projection stage"):
        B.project(R, "nope", **kw)


def test_budget_is_stdlib_only():
    code = ("import sys; import emx.e12.budget; "
            "bad = sorted({m.split('.')[0] for m in sys.modules} & "
            "{'numpy', 'pandas', 'torch', 'requests', 'openai', 'tenacity', 'yaml'}); "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=C.REPO_ROOT)
    assert out.stdout.strip() == "[]"
