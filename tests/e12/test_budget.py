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
_LIMIT_NAMES = {"gpu_usd": "GPU_LIMIT_USD", "api_usd": "API_LIMIT_USD",
                "pod_hours": "POD_HOURS_LIMIT", "hard_cap_usd": "HARD_CAP_USD"}


def binding_hard_cap(monkeypatch, cap: float = 40.0) -> float:
    """Patch the hard cap to `cap` (default Amendment B's $40, the frame the reviewer's probe was
    built in), below GPU limit + API limit so that it can bind on its own (Amendments C and C'
    set the hard cap to their sum, so unpatched it never binds alone). Patches budget's recorded
    copy too."""
    assert cap < C.GPU_LIMIT_USD + C.API_LIMIT_USD
    monkeypatch.setattr(C, "HARD_CAP_USD", cap)
    monkeypatch.setattr(B, "_LIMITS", {k: getattr(C, v) for k, v in _LIMIT_NAMES.items()})
    return cap


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
    assert set(fixture["limits"]) <= set(rec["limits"])  # the fixture predates hard_cap_usd
    assert rec["limits"]["hard_cap_usd"] == C.HARD_CAP_USD
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


def test_measured_judging_rate_and_amended_limits():
    """Review r1 finding 8: the measured $0.33 per 1,000 EM answers and the $20 API limit
    (prereg Amendment B); the $60 hard cap, $40 GPU limit and 10 pod hours (Amendment C)."""
    assert B.FALLBACK_RATES["judge_usd_per_1000_answers"] == 0.33
    assert C.API_LIMIT_USD == 20.0 and C.GPU_LIMIT_USD == 48.0 and C.HARD_CAP_USD == 70.0
    assert C.POD_HOURS_LIMIT == 12.0
    assert B._LIMITS == {k: getattr(C, v) for k, v in _LIMIT_NAMES.items()}


def test_vllm_speculative_admits_without_cuts_under_the_20_dollar_limit(tmp_path):
    """The rescued 214 branch's judging ($18.92 with the style training still to rate) plus
    $0.40 recorded fits $20 with no cuts (it would have needed cuts under the $15 limit)."""
    root = bench_root(tmp_path, frozen=False)
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=None, elapsed_pod_hours=0.4,
                  gpu_rate=3.49, api_spent_usd=0.4, root=root, write=False)
    assert rec["verdict"] == "admit" and rec["cuts"] == [] and len(rec["tried"]) == 1
    assert rec["api_usd"] == pytest.approx(18.92, abs=0.01)
    assert rec["api"]["endpoint_priced"] == 214 and rec["api"]["rescue_priced"] is True
    assert rec["api"]["usd"]["em_judging"] == pytest.approx(51_820 * 0.33 / 1000, abs=1e-3)


def test_hard_cap_binds_on_its_own_and_is_recorded(monkeypatch):
    """Code review r2 finding 1: when the GPU and API limits sum to more than the hard cap, each
    limit can fit while GPU $ + API $ + recorded spend exceeds the hard cap (the reviewer's
    probe)."""
    cap = binding_hard_cap(monkeypatch)
    spent, hours = 0.4, C.POD_HOURS_LIMIT - 0.5
    api = C.API_LIMIT_USD - spent - 1.0  # $1 inside the API limit
    gpu = cap - api - spent + 1.0  # $1 over the hard cap, inside the GPU limit
    assert gpu <= C.GPU_LIMIT_USD
    checks = B.limit_checks(gpu_usd=gpu, total_pod_hours=hours, api_usd=api, api_spent_usd=spent)
    assert checks == {"gpu_usd": True, "pod_hours": True, "api_usd": True,
                      "hard_cap_usd": False}
    assert all(B.limit_checks(gpu_usd=gpu - 1.0, total_pod_hours=hours, api_usd=api,
                              api_spent_usd=spent).values())  # exactly the hard cap fits
    assert B._LIMITS["hard_cap_usd"] == C.HARD_CAP_USD == cap


def test_reviewer_probe_is_not_admitted_without_cuts(tmp_path, monkeypatch):
    """The probe's elapsed hours give GPU $21.68 with the rescued branch's API $18.92 and $0.40
    recorded ($41.00, $1 over a hard cap of GPU + API limits - $20): the uncut projection is
    refused, cuts are added in order, and the admitted one fits every limit, the hard cap
    included."""
    cap = binding_hard_cap(monkeypatch)
    root = bench_root(tmp_path, frozen=False)
    kw = {"gen_path": "vllm", "endpoint": None, "gpu_rate": 3.49, "api_spent_usd": 0.4,
          "root": root}
    base = B.project(R, "smoke", cuts=(), elapsed_pod_hours=0.0, **kw)
    elapsed = 21.683 / 3.49 - base["total_pod_hours"]  # the probe's GPU $
    uncut = B.project(R, "smoke", cuts=(), elapsed_pod_hours=elapsed, **kw)
    assert uncut["gpu_usd"] == pytest.approx(21.683, abs=0.01)
    assert uncut["api_usd"] == pytest.approx(18.92, abs=0.01)
    assert uncut["combined_usd"] == pytest.approx(cap + 1.0, abs=0.01)
    assert uncut["verdict"] == "refuse" and uncut["limit_checks"] == {
        "gpu_usd": True, "pod_hours": True, "api_usd": True, "hard_cap_usd": False}
    rec = B.admit(R, "smoke", elapsed_pod_hours=elapsed, write=False, **kw)
    assert rec["tried"][0]["verdict"] == "refuse" and rec["cuts"] != []
    assert [t["cuts"] for t in rec["tried"]] == [list(c) for c in
                                                 B.cut_sequence()[:len(rec["tried"])]]
    assert rec["verdict"] == "admit" and all(rec["limit_checks"].values())
    assert rec["combined_usd"] <= C.HARD_CAP_USD
    assert rec["combined_usd"] == pytest.approx(rec["gpu_usd"] + rec["api_usd"] + 0.4, abs=1e-3)
    # nothing fits once the recorded spend alone nearly fills the API limit
    rec = B.admit(R, "smoke", elapsed_pod_hours=elapsed, write=False,
                  **{**kw, "api_spent_usd": C.API_LIMIT_USD - 5.0})
    assert rec["verdict"] == "refuse" and rec["cuts"] == [0, 1, 2, 3, 4, 5]


def test_hf_complete_enforces_the_hard_cap(tmp_path, monkeypatch):
    """The hf-complete branch checks the same four limits on its maxima."""
    root = bench_root(tmp_path, hf_rate=5.0)
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, root=root, branch="hf-complete",
                  elapsed_pod_hours=1.0, gpu_rate=3.49, api_spent_usd=0.4, write=False)
    assert rec["limits"]["hard_cap_usd"] == C.HARD_CAP_USD
    assert rec["combined_usd"] == pytest.approx(rec["gpu_usd"] + rec["api_usd"] + 0.4, abs=1e-3)
    assert rec["verdict"] == "admit" and all(rec["limit_checks"].values())
    # elapsed hours that keep GPU and pod hours inside their limits but push GPU + API $1 past
    # a hard cap that binds on its own
    cap = binding_hard_cap(monkeypatch)
    base = B.project_hf_complete(R, "hf", cuts=(), elapsed_pod_hours=0.0, gpu_rate=3.49,
                                 api_spent_usd=0.4, root=root)
    elapsed = (cap - base["api_usd"] - 0.4 + 1.0) / 3.49 - base["total_pod_hours"]
    uncut = B.project_hf_complete(R, "hf", cuts=(), elapsed_pod_hours=elapsed, gpu_rate=3.49,
                                  api_spent_usd=0.4, root=root)
    assert uncut["gpu_usd"] <= C.GPU_LIMIT_USD and uncut["total_pod_hours"] <= C.POD_HOURS_LIMIT
    assert uncut["api_usd"] + 0.4 <= C.API_LIMIT_USD
    assert uncut["verdict"] == "refuse" and uncut["limit_checks"]["hard_cap_usd"] is False
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, root=root, branch="hf-complete",
                  elapsed_pod_hours=elapsed, gpu_rate=3.49, api_spent_usd=0.4, write=False)
    # cap + $1.00 uncut, cap + $0.60 with cut 0 (API only), admitted with cuts 0 and 1
    assert [t["verdict"] for t in rec["tried"]] == ["refuse", "refuse", "admit"]
    assert rec["verdict"] == "admit" and rec["cuts"] == [0, 1]
    assert rec["combined_usd"] <= C.HARD_CAP_USD and all(rec["limit_checks"].values())


def test_cuts_are_added_in_order(tmp_path, monkeypatch):
    root = bench_root(tmp_path, frozen=False)  # style training not yet rated: +$0.5
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=None, elapsed_pod_hours=0.4,
                  gpu_rate=3.49, api_spent_usd=1.2, root=root, write=False)  # 18.92 + 1.2 > 20
    assert rec["verdict"] == "admit" and rec["cuts"] == [0]
    t0, t1 = rec["tried"]
    assert t0["cuts"] == [] and t0["verdict"] == "refuse"
    assert t1["gpu_usd"] == t0["gpu_usd"] and t1["api_usd"] < t0["api_usd"]  # cut 0: API only
    binding_hard_cap(monkeypatch)
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, elapsed_pod_hours=0.4, gpu_rate=3.49,
                  api_spent_usd=0.4, root=bench_root(tmp_path / "hf"), write=False)
    # cuts 0 and 1 fit the GPU, API and hour limits, but GPU + API + recorded is over a hard cap
    # that binds on its own: cut 2 too
    assert [t["cuts"] for t in rec["tried"]] == [[], [0], [0, 1], [0, 1, 2]]
    assert rec["verdict"] == "admit" and rec["cuts"] == [0, 1, 2]
    assert rec["tried"][2]["combined_usd"] > C.HARD_CAP_USD >= rec["combined_usd"]
    assert B.cut_sequence() == [(), (0,), (0, 1), (0, 1, 2), (0, 1, 2, 3), (0, 1, 2, 3, 4),
                                (0, 1, 2, 3, 4, 5)]


def test_refuses_when_nothing_fits(tmp_path):
    # half a pod hour left before the hour limit: too little for any cut level
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=None,
                  elapsed_pod_hours=C.POD_HOURS_LIMIT - 0.5, gpu_rate=3.49, api_spent_usd=0.0,
                  root=bench_root(tmp_path), write=False)
    assert rec["verdict"] == "refuse" and rec["cuts"] == [0, 1, 2, 3, 4, 5]
    assert len(rec["tried"]) == 7 and all(t["verdict"] == "refuse" for t in rec["tried"])
    assert rec["limit_checks"]["pod_hours"] is False
    rec = B.admit(R, "smoke", gen_path="vllm", endpoint=107, elapsed_pod_hours=0.0,
                  gpu_rate=3.49, api_spent_usd=C.API_LIMIT_USD - 6.0,
                  root=bench_root(tmp_path / "b"),
                  write=False)
    assert rec["verdict"] == "refuse"  # API side: recorded spend leaves no room


def test_pre_decision_hf_budget_does_not_admit_the_complete_branch(tmp_path):
    """A budget that fits the 25,280 pre-decision HF answers but not the conservative complete
    branch (`planned_jobs(r, "hf", 214, (), True)`, 57,020 answers before cuts) is refused."""
    assert sum(j.n_rows for j in C.planned_jobs(R, "hf")) == 25_280
    assert sum(j.n_rows for j in C.planned_jobs(R, "hf", 214, (), True)) == 57_020
    root = bench_root(tmp_path, hf_rate=2.0)
    elapsed = C.POD_HOURS_LIMIT - 6.5  # 6.5 pod hours left: the pod-hour limit binds
    kw = {"gen_path": "hf", "elapsed_pod_hours": elapsed, "gpu_rate": 3.49, "api_spent_usd": 0.4,
          "root": root, "write": False}
    pre = B.admit(R, "hf", endpoint=None, **kw)
    assert pre["verdict"] == "admit" and pre["remaining_answers"] == 25_280
    full = B.admit(R, "hf", endpoint=214, rescue_triggered=True, **kw)
    assert full["verdict"] == "refuse" and full["limit_checks"]["pod_hours"] is False
    assert full["cuts"] == [0, 1, 2, 3, 4, 5]
    assert full["jobs_sha256"] == C.jobs_sha256(C.planned_jobs(R, "hf", 214, (1, 2, 3, 4, 5), True))
    rc = B.main(["admit", "--recipe", R, "--stage", "hf", "--gen-path", "hf", "--endpoint",
                 "214", "--rescue-triggered", "--elapsed-h", str(elapsed), "--gpu-rate", "3.49",
                 "--api-spent", "0.4", "--no-write", "--root", str(root)])
    assert rc == C.EXIT_BUDGET
    rc = B.main(["admit", "--recipe", R, "--stage", "hf", "--gen-path", "hf", "--elapsed-h",
                 str(elapsed), "--gpu-rate", "3.49", "--api-spent", "0.4", "--no-write", "--root",
                 str(root)])
    assert rc == C.EXIT_OK


def test_hf_complete_prices_the_dearer_branch_and_binds_the_union(tmp_path):
    """Review r1 finding 1: the HF admission authorizes both mutually exclusive complete
    branches (107 without the rescue, 214 with it) and prices the dearer one."""
    union = C.hf_complete_jobs(R, ())
    b107, b214 = C.planned_jobs(R, "hf", 107, (), False), C.planned_jobs(R, "hf", 214, (), True)
    assert set(b107) | set(b214) == set(union) and not set(b107) <= set(b214)
    # 6 pod hours left: the pod-hour limit binds the slower benches below
    kw = {"elapsed_pod_hours": C.POD_HOURS_LIMIT - 6.0, "gpu_rate": 3.49, "api_spent_usd": 0.4,
          "write": False}
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, root=bench_root(tmp_path, hf_rate=5.0),
                  branch="hf-complete", **kw)
    assert rec["verdict"] == "admit" and rec["cuts"] == []
    assert rec["branch"] == "hf-complete" and rec["endpoint"] is None
    assert rec["jobs_sha256"] == C.jobs_sha256(union) and rec["n_jobs"] == len(union)
    br = rec["branches"]
    assert br["107"]["jobs_sha256"] == C.jobs_sha256(b107)
    assert br["214"]["jobs_sha256"] == C.jobs_sha256(b214)
    assert br["214"]["remaining_answers"] == 57_020
    for k in ("total_pod_hours", "gpu_usd", "api_usd"):
        assert rec[k] == max(br["107"][k], br["214"][k])
    assert rec["priced_endpoint"] == 214 and rec["api_usd"] == pytest.approx(18.42, abs=0.01)
    assert set(C.BENCH_FIELDS["projection"]) <= set(rec)
    # a slower HF bench: the cuts are added in order until the dearer branch fits
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, root=bench_root(tmp_path / "b",
                  hf_rate=3.0), branch="hf-complete", **kw)
    assert [t["cuts"] for t in rec["tried"]] == [list(c) for c in B.cut_sequence()[:6]]
    assert rec["verdict"] == "admit" and rec["cuts"] == [0, 1, 2, 3, 4]
    assert rec["gpu_usd"] <= C.GPU_LIMIT_USD and rec["total_pod_hours"] <= C.POD_HOURS_LIMIT
    assert rec["jobs_sha256"] == C.jobs_sha256(C.hf_complete_jobs(R, (1, 2, 3, 4)))
    assert all(t["verdict"] == "refuse" for t in rec["tried"][:-1])
    # the fixture's own HF rate never fits: refused after every cut
    rec = B.admit(R, "hf", gen_path="hf", endpoint=None, root=bench_root(tmp_path / "c"),
                  branch="hf-complete", **kw)
    assert rec["verdict"] == "refuse" and rec["cuts"] == [0, 1, 2, 3, 4, 5]
    assert rec["limit_checks"]["pod_hours"] is False
    for bad in ({"gen_path": "vllm", "endpoint": None}, {"gen_path": "hf", "endpoint": 214},
                {"gen_path": "hf", "endpoint": None, "rescue_triggered": True}):
        with pytest.raises(ValueError, match="hf-complete"):
            B.admit(R, "hf", root=tmp_path / "x", branch="hf-complete", **bad, **kw)
    with pytest.raises(ValueError, match="unknown branch"):
        B.admit(R, "hf", gen_path="hf", endpoint=None, root=tmp_path, branch="hf-107", **kw)


def test_cli_branch_hf_complete_writes_the_projection(tmp_path, capsys):
    root = bench_root(tmp_path, hf_rate=5.0)
    rc = B.main(["admit", "--recipe", R, "--stage", "hf", "--gen-path", "hf", "--branch",
                 "hf-complete", "--elapsed-h", "1.0", "--gpu-rate", "3.49", "--api-spent", "0.4",
                 "--root", str(root)])
    assert rc == C.EXIT_OK
    rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rec["branch"] == "hf-complete"
    assert json.loads((root / C.projection_path(R, "hf")).read_text()) == rec
    rc = B.main(["admit", "--recipe", R, "--stage", "hf", "--gen-path", "hf", "--branch",
                 "hf-complete", "--endpoint", "214", "--elapsed-h", "1.0", "--gpu-rate", "3.49",
                 "--api-spent", "0.4", "--no-write", "--root", str(root)])
    assert rc == C.EXIT_FAILED


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
