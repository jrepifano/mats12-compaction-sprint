"""E12 pod tooling, offline (E12 implementation plan v0.3.1, unit U5). `--exp E12` in emx.runpod /
pod_up / pod_down / pod_budget_check (one pod e12-a in slot _A, logs/pods_E12.jsonl, CAPS["E12"],
logs/approvals/E12.approved, a reservation of contracts.pod_reservation_usd(rate)), pod_push.sh's
E12 transfer set and pod env, pod_setup.sh --only e12, pod_pull_results.sh --exp E12 (both
recipes) and scripts/e12_watchdog.sh (with its pod-hour rule). Copied from tests/eq0/test_pod_eq0.py
(whose helpers, shared with tests/i1a/test_pod_i1a.py, are reused). Nothing here contacts
RunPod, SSH or the Hub: fake clients and fake git/uv/ssh/rsync on PATH, sandboxed event logs,
ledgers and reservations; every shell script runs from a copy in tmp_path."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp
from emx.e12 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UTC_LINE = "2026-10-08T12:00:00Z approved by Jacob (test)\n"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_i1a = _load("_e12_reuse_i1a_pod_tests", ROOT / "tests" / "i1a" / "test_pod_i1a.py")
FAKE_GIT, copy_script, fake_bin, run_bash, sha256 = (
    _i1a.FAKE_GIT, _i1a.copy_script, _i1a.fake_bin, _i1a.run_bash, _i1a.sha256,
)
FakeSpend, FakeClient, HEAD, LAPTOP, BASH4 = (
    _i1a.FakeSpend, _i1a.FakeClient, _i1a.HEAD, _i1a.LAPTOP, _i1a.BASH4,
)


@pytest.fixture
def e12(monkeypatch, tmp_path):
    """emx.runpod configured for E12 with .env, logs and the event log in tmp_path; restored."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    rp.configure("E12")
    return tmp_path


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


def approve(logs: Path, text: str = UTC_LINE) -> Path:
    p = logs / "approvals" / "E12.approved"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def beat(logs: Path) -> None:
    (logs / "watchdog_E12.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")


def created(pod_id, slot, t0, rate):
    return {"event": "created", "pod_id": pod_id, "slot": slot, "gpu": "g",
            "utc": t0.isoformat(), "rate_usd_h": rate}


# --- emx.runpod under E12 ------------------------------------------------------------------------


def test_configure_e12_names_logs_and_caps(e12):
    assert rp.EXP == "E12" and rp.POD_NAME_PREFIX == "e12-"
    assert rp.POD_EVENTS == e12 / "pods_E12.jsonl"
    c = rp.caps()
    assert (c.hard_cap_usd, c.gpu_limit_usd) == (C.HARD_CAP_USD, C.GPU_LIMIT_USD)
    assert rp.pod_body(rp.GPU_FALLBACK[0], "_A", "ssh-ed25519 k")["name"] == "e12-a"
    assert rp.heartbeat_path() == e12 / Path(C.WATCHDOG_HEARTBEAT).relative_to("logs")


def test_reservation_size_follows_the_contracts():
    got = {g: C.pod_reservation_usd(r) for g, r in C.FALLBACK_GPUS}
    # every fallback GPU's pod-hour limit fits inside the GPU limit: the hours bind
    assert got == {g: round(C.POD_HOURS_LIMIT * r, 2) for g, r in C.FALLBACK_GPUS}
    assert all(v <= C.GPU_LIMIT_USD for v in got.values())
    assert len(set(got.values())) == len(got)  # a reservation names exactly one GPU option
    assert all(v / r <= C.POD_HOURS_LIMIT + 1e-9 for (_g, r), v in
               zip(C.FALLBACK_GPUS, got.values(), strict=True))
    above = C.GPU_LIMIT_USD / C.POD_HOURS_LIMIT + 1.0
    assert C.pod_reservation_usd(above) == C.GPU_LIMIT_USD  # the $ line binds above limit/hours


# --- pod_up --exp E12 ----------------------------------------------------------------------------


@pytest.fixture
def up(spend, e12, monkeypatch):
    mod = _load("_e12_pod_up", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


H100_RESERVATION = f"{C.pod_reservation_usd(3.49):.2f}"


def use_client(monkeypatch, fake):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)


def test_e12_fallback_order_follows_the_contracts(up):
    got = [(o.gpu_type_id, o.cloud, o.rate_usd_h) for o in up.e12_gpu_options()]
    assert got == [
        ("NVIDIA H100 80GB HBM3", "SECURE", 3.49),
        ("NVIDIA H100 NVL", "SECURE", 2.59),
        ("NVIDIA H100 PCIe", "COMMUNITY", 2.69),
        ("NVIDIA A100-SXM4-80GB", "SECURE", 1.39),
    ]
    assert [g for g, _c, _r in got] == [g for g, _r in C.FALLBACK_GPUS]


def test_pod_up_e12_refuses_without_its_approval(up, e12, monkeypatch):
    beat(e12)
    (e12 / "approvals").mkdir()
    (e12 / "approvals" / "EQ0.approved").write_text(UTC_LINE)  # another experiment's: no
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    args = ["--exp", "E12", "--slot", "_A", "--reservation", "r1", "--budget-usd", H100_RESERVATION]
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []
    approve(e12, "go\n")  # no UTC line: still refused
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []


@pytest.mark.parametrize("slot", ["_B", "_C"])
def test_pod_up_e12_refuses_any_slot_but_a(up, e12, monkeypatch, slot):
    beat(e12)
    approve(e12)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E12", "--slot", slot, "--reservation", "r1", "--budget-usd", H100_RESERVATION])
    assert e.value.code == 2 and fake.created == []


def test_pod_up_e12_needs_a_reservation(up, e12, monkeypatch):
    beat(e12)
    approve(e12)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E12", "--slot", "_A"])
    assert e.value.code == 2 and fake.created == []


class _ReservedClient(FakeClient):
    def list_pods(self):
        return []


def _reserved(e12, monkeypatch, tmp_path, budget, create):
    from emx import runpod_reservations as rr
    from emx import spend as real_spend

    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "res.jsonl"))
    monkeypatch.setenv(real_spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    up = _load("_e12_pod_up_reserved", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    beat(e12)
    approve(e12)
    rid = rr.reserve("E12", budget, "E12 pod", balance=200.0)
    fake = _ReservedClient(create_results=create)
    use_client(monkeypatch, fake)
    return up, rr, rid, fake


def test_pod_up_e12_reserved_launch_at_the_h100_reservation(e12, monkeypatch, tmp_path, capsys):
    """Real emx.spend and emx.runpod_reservations, both on sandboxed files."""
    budget = C.pod_reservation_usd(3.49)
    up, rr, rid, fake = _reserved(e12, monkeypatch, tmp_path, budget,
                                  [{"id": "pq", "costPerHr": 3.49}])
    rc = up.main(["--exp", "E12", "--slot", "_A", "--reservation", rid, "--budget-usd",
                  str(budget)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert [(b["name"], b["cloudType"], b["gpuTypeIds"][0]) for b in fake.created] == [
        ("e12-a", "SECURE", "NVIDIA H100 80GB HBM3")
    ]
    assert fake.created[0]["containerDiskInGb"] == C.VOLUME_GB == 80
    assert "EMX_POD_CREATE=ok" in out and "EMX_POD_DEADLINE=" in out
    R = rr.get(rid)
    assert R["launched"] and R["pods"] == ["pq"]
    ev = rp.read_pod_events()[0]
    assert ev["event"] == "created" and ev["exp"] == "E12" and ev["reservation_id"] == rid
    t0 = datetime.fromisoformat(ev["utc"]).timestamp()
    deadline = int(next(ln for ln in out.splitlines() if ln.startswith("EMX_POD_DEADLINE="))
                   .split("=")[1])
    assert deadline - t0 <= C.POD_HOURS_LIMIT * 3600 + 1  # never past the pod-hour limit


def test_pod_up_e12_launches_only_on_the_gpu_its_reservation_fits(e12, monkeypatch, tmp_path,
                                                                    capsys):
    budget = C.pod_reservation_usd(2.59)  # NVL's, not H100 secure's
    up, _rr, rid, fake = _reserved(e12, monkeypatch, tmp_path, budget,
                                   [{"id": "pn", "costPerHr": 2.59}])
    rc = up.main(["--exp", "E12", "--slot", "_A", "--reservation", rid, "--budget-usd",
                  str(budget)])
    assert rc == 0, capsys.readouterr().out
    assert [b["gpuTypeIds"][0] for b in fake.created] == ["NVIDIA H100 NVL"]


def test_pod_up_e12_refuses_a_reservation_of_another_size(e12, monkeypatch, tmp_path, capsys):
    up, rr, rid, fake = _reserved(e12, monkeypatch, tmp_path, 25.0, [{"id": "never"}])
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E12", "--slot", "_A", "--reservation", rid, "--budget-usd", "25"])
    assert e.value.code == up.GATE_REFUSED and fake.created == []
    err = capsys.readouterr().err
    assert "pod_reservation_usd" in err
    assert not rr.get(rid)["launched"]


def run_py(*args, tmp_path):
    env = {**os.environ, "EMX_EXP": "E1", "EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl"),
           "EMX_POD_LOG_DIR": str(tmp_path)}
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=60, check=False)


def dry_requests(stdout: str) -> list[dict]:
    parts = stdout.split("DRY RUN (not sent):\n")[1:]
    return [json.loads(p.split("\nbudget gate")[0]) for p in parts]


def test_pod_up_dry_run_e12(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "E12", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert [q["method"] for q in reqs] == ["POST"] * 4
    assert {q["json"]["name"] for q in reqs} == {"e12-a"}
    assert [q["json"]["gpuTypeIds"][0] for q in reqs] == [g for g, _r in C.FALLBACK_GPUS]
    assert {q["json"]["containerDiskInGb"] for q in reqs} == {C.VOLUME_GB}
    assert "approval (E12): MISSING" in r.stdout
    assert "watchdog (E12): NOT LIVE" in r.stdout and "budget gate (E12)" in r.stdout
    for g, rate in C.FALLBACK_GPUS:
        assert f"reservation (E12): {g} at ${rate}/h needs ${C.pod_reservation_usd(rate):.2f}" \
            in r.stdout
    assert not (tmp_path / "pods_E12.jsonl").exists()
    approve(tmp_path)
    beat(tmp_path)
    r = run_py("scripts/pod_up.py", "--exp", "E12", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0 and "approval (E12): present" in r.stdout
    r = run_py("scripts/pod_up.py", "--exp", "E12", "--slot", "_A", "--dry-run",
               "--reservation", "r1", "--budget-usd", f"{C.pod_reservation_usd(2.69):.2f}",
               tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert [q["json"]["gpuTypeIds"][0] for q in dry_requests(r.stdout)] == ["NVIDIA H100 PCIe"]


def test_pod_up_dry_run_e12_refuses_other_slots(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "E12", "--slot", "_B", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "E12 has one pod" in r.stderr and "DRY RUN" not in r.stdout


def test_pod_down_and_budget_check_dry_e12(tmp_path):
    r = run_py("scripts/pod_down.py", "--exp", "E12", "--all-exp", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "<each e12-* pod id>" in r.stdout
    r = run_py("scripts/pod_budget_check.py", "--exp", "E12", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["exp"] == "E12" and d["gpu_kill_limit_usd"] == C.GPU_LIMIT_USD
    assert d["hard_cap_usd"] == C.HARD_CAP_USD


def test_budget_check_e12_reports_hours_for_the_watchdog(spend, e12, capsys):
    bc = _load("_e12_budget", SCRIPTS / "pod_budget_check.py")
    now = datetime.now(UTC)
    rp.POD_EVENTS.write_text(json.dumps(created("pa", "_A", now - timedelta(hours=2), 1.39)) + "\n")
    assert bc.main(["--exp", "E12"]) == 0
    d = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert d["running"][0]["slot"] == "_A" and 1.99 < d["running"][0]["hours"] < 2.01


# --- pod_push.sh --exp E12 (a copy in tmp_path, a fake checkout and a FAKE git) ------------------

TRACKED = [
    "pyproject.toml",
    "uv.lock",
    "src/emx/__init__.py",
    "src/emx/e12/contracts.py",
    "src/emx/qwen/registry.py",
    "scripts/e12_pod.py",
    "scripts/pod_run_e12.sh",
    "scripts/eq0_pod.py",
    "data/E1/questions/general.jsonl",
    "data/E1/questions/medical.jsonl",  # tracked, but not an E12 root
    "data/E1/manifest.json",
    "data/E1b/questions/ext48.jsonl",
    "data/E1b/manifest.json",
    "data/I9/scenarios.jsonl",
    "data/shared/answer_pairs_v1.jsonl",
    "configs/E1/a.json",
    "prompts/E12/task_judge_v1.txt",  # laptop-only (the task judge)
    "third_party/workspace-bench/pyproject.toml",
    "docs/private-notes.md",
]
DATA = [f.local for f in C.DATA_FILES.values()]
NAMED = [C.PRERUN_MANIFEST, C.STYLE_FROZEN, C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED]
IGNORED = DATA + NAMED
NEVER = [
    ".env.local",
    "data/E1/questions/narrow.jsonl",  # held out (gitignored): never leaves the laptop
    "data/E1/train_bad.jsonl",
    "data/raw/bad_medical_advice.jsonl",
    "data/EQ0/bad_medical_advice.jsonl",
    "results/EQ0/gen/bad_medical.csv",
    "results/E12/rank1/gen/full/P1.csv",
    "results/E12/validation/handlabel/rank1/key.private.json",
    "logs/approvals/EQ0.approved",
]
REQUIRED = [
    *DATA, *C.QUESTION_FILES.values(), "scripts/e12_pod.py", "scripts/pod_run_e12.sh", *NAMED,
    C.APPROVAL,
]


@pytest.fixture
def checkout(tmp_path):
    s = copy_script(tmp_path, "pod_push.sh")
    for f in TRACKED + IGNORED + NEVER:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x\n")
    fg = tmp_path / ".fakegit"
    fg.mkdir()
    (fg / "tracked").write_text("\n".join(TRACKED) + "\n")
    (fg / "head").write_text(HEAD + "\n")
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\nHF_USERNAME=jacob\n" + LAPTOP)
    fake_bin(tmp_path, "fakegit", FAKE_GIT)
    return s


# the push reads this checkout's recorded E12 spend with `uv run python -c ...spend.spent(...)`;
# a fake `uv` answers those (FAKE_GPU_SPENT / FAKE_GPU_RC override the GPU line) and nothing else
SPEND_UV = (
    'case "$*" in\n'
    '  *\'spent("gpu"\'*) printf \'%s\\n\' "${FAKE_GPU_SPENT-17.9044}"; exit "${FAKE_GPU_RC:-0}" ;;\n'
    '  *\'spent("judge"\'*) echo 0.4821 ;;\n'
    '  *) exit 1 ;;\n'
    'esac\n'
)


def push(checkout, *args, dry=True, env=None):
    tmp = checkout.parent.parent
    extra = {"POD_PUSH_GIT": str(tmp / "bin" / "fakegit"), "FAKEGIT_DIR": str(tmp / ".fakegit"),
             "EMX_EXP": "E1", **(env or {})}
    if "PATH" not in extra:
        uvbin = tmp / "uvbin"
        uvbin.mkdir(exist_ok=True)
        (uvbin / "uv").write_text("#!/bin/bash\n" + SPEND_UV)
        (uvbin / "uv").chmod(0o755)
        extra["PATH"] = f"{uvbin}:{os.environ['PATH']}"
    flags = ("--dry-run", "--show-files") if dry else ()
    return run_bash(checkout, *args, *flags, env_extra=extra)


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def line(stdout: str, tag: str) -> list[str]:
    hit = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return hit.split(f"{tag}:", 1)[1].split()


def test_pod_push_e12_transfer_set_env_and_setup(checkout, tmp_path):
    approve(tmp_path / "logs")
    r = push(checkout, "--exp", "E12", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E12)" in r.stdout and "WARNING" not in r.stdout
    assert line(r.stdout, "transfer set") == [
        "src", "scripts", "data/E12", "data/E1/questions/general.jsonl",
        "data/E1b/questions/ext48.jsonl", "pyproject.toml", "uv.lock",
    ]
    files = sent(r.stdout)
    want = {f for f in TRACKED if f.startswith(("src/", "scripts/"))} | {
        "pyproject.toml", "uv.lock", *C.QUESTION_FILES.values()}
    assert files == want | set(IGNORED) | {C.APPROVAL}
    for f in REQUIRED:  # each transferred path, one by one
        assert f in files, f
    assert {C.PRERUN_MANIFEST, C.STYLE_FROZEN, C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED} <= files
    assert not files & set(NEVER)
    assert not any(f.startswith(("data/I9", "data/shared", "data/EQ0", "configs/", "prompts/",
                                 "third_party/", "results/E12/rank")) for f in files)
    keys = line(r.stdout, "pod env keys")
    assert keys[:2] == ["HF_TOKEN", "HF_USERNAME"]
    assert {"EMX_EXP", "EMX_GIT_COMMIT", "EMX_GIT_DIRTY", "HF_HOME", "HF_HUB_DISABLE_XET",
            "WANDB_MODE", "E12_RECIPE", "E12_API_SPENT_USD", "E12_GPU_SPENT_USD"} <= set(keys)
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "hf_abc" not in r.stdout and "secret" not in r.stdout
    assert "DRY RUN (E12): recipe rank1" in r.stdout
    assert "--check-slot _A" in r.stdout and "pod_setup.sh --only e12" in r.stdout
    assert "pod_killswitch.sh arm" in r.stdout


def test_pod_push_e12_recipe_option(checkout, tmp_path):
    approve(tmp_path / "logs")
    r = push(checkout, "--exp", "E12", "--slot", "_A", "--recipe", "rank32")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E12): recipe rank32" in r.stdout
    r = push(checkout, "--exp", "E12", "--slot", "_A", "--recipe", "tan32")  # Amendment D
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E12): recipe tan32" in r.stdout
    r = push(checkout, "--exp", "E12", "--slot", "_A", "--recipe", "rank8")
    assert r.returncode == 2 and "rank1, rank32 or tan32" in r.stderr
    r = push(checkout, "--exp", "EQ0", "--slot", "_A", "--recipe", "rank1")
    assert r.returncode == 2 and "--recipe is E12-only" in r.stderr


def test_pod_push_e12_hf_username_is_optional(checkout, tmp_path):
    approve(tmp_path / "logs")
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\n" + LAPTOP)
    r = push(checkout, "--exp", "E12", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    keys = line(r.stdout, "pod env keys")
    assert keys[0] == "HF_TOKEN" and "HF_USERNAME" not in keys and "E12_RECIPE" in keys


def test_pod_push_e12_names_the_contract_paths():
    """The shell's hardcoded E12 lists equal emx.e12.contracts (stdlib constants)."""
    text = (SCRIPTS / "pod_push.sh").read_text()
    branch = text.split('elif [ "$EXP" = "E12" ]; then', 1)[1].split(
        'elif [ "$EXP" = "EQ0" ]; then', 1)[0]
    for f in (*DATA, *C.QUESTION_FILES.values(), *NAMED, C.APPROVAL):
        assert f in branch, f
    roots = next(ln for ln in text.splitlines() if ln.lstrip().startswith("E12) ROOTS="))
    assert "narrow" not in roots.split("#", 1)[0] and "data/E1 " not in roots
    assert "grep -qxF data/E1/questions/narrow.jsonl" in branch and "refusing" in branch
    assert C.DATA_DIR == "data/E12" and "data/E12" in roots
    # E12's branch sits before EQ0's (tests/eq0/test_pod_eq0.py slices from EQ0's to `else`)
    assert text.index('elif [ "$EXP" = "E12" ]; then') < text.index('elif [ "$EXP" = "EQ0" ]; then')


def test_pod_push_e12_warns_on_missing_inputs_in_dry_run(checkout, tmp_path):
    (tmp_path / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if f not in ("scripts/e12_pod.py", "scripts/pod_run_e12.sh"))
        + "\n")
    for f in (C.DATA_FILES["mixture"].local, C.PRERUN_MANIFEST, C.STYLE_FROZEN, C.EQ0_BASE_JUDGED):
        (tmp_path / f).unlink()
    r = push(checkout, "--exp", "E12", "--slot", "_A")  # and no approval
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "WARNING" in ln)
    for f in ("scripts/e12_pod.py", "scripts/pod_run_e12.sh", C.DATA_FILES["mixture"].local,
              C.PRERUN_MANIFEST, C.STYLE_FROZEN, C.EQ0_BASE_JUDGED, C.APPROVAL):
        assert f in warn, f
    assert C.EQ0_BASE_GEN not in warn and "data/E1/questions/general.jsonl" not in warn


def test_pod_push_e12_real_push_refuses_missing_inputs_before_contacting_the_pod(
    checkout, tmp_path
):
    calls = tmp_path / "calls.txt"
    fake_bin(tmp_path, "uv", f'echo "uv $*" >> {calls}\n'
             'case "$*" in *spent*) echo 1.5; exit 0 ;; esac\n'
             'echo "EMX_POD_RATE_USD_H=3.49"; echo "EMX_POD_CREATED_EPOCH=1760000000"\n')
    for tool in ("ssh", "rsync", "scp"):
        fake_bin(tmp_path, tool, f'echo "{tool} $*" >> {calls}\nexit 1\n')
    with (tmp_path / ".env").open("a") as f:
        f.write("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\nRUNPOD_POD_ID_A=pq\n")
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    r = push(checkout, "--exp", "E12", "--slot", "_A", dry=False, env={"PATH": path})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "required E12 inputs missing" in r.stderr and C.APPROVAL in r.stderr
    got = calls.read_text().splitlines() if calls.exists() else []
    assert any("--exp E12 --check-slot _A --session-env" in c for c in got), got
    assert not any(c.startswith(("ssh", "rsync", "scp")) for c in got), got


def test_pod_push_e12_refuses_bad_slots_modified_and_untracked_files(checkout, tmp_path):
    approve(tmp_path / "logs")
    for slot in ("_B", "_C"):
        r = push(checkout, "--exp", "E12", "--slot", slot)
        assert r.returncode == 2 and "slot must be _A" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "E12", "--slot", "_A")
    assert r.returncode == 1 and "commit them before an E12 push" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("logs/pods_E12.jsonl\nlogs/costs.jsonl\n")
    assert push(checkout, "--exp", "E12", "--slot", "_A").returncode == 0  # run ledgers only
    (tmp_path / ".fakegit" / "modified").write_text("")
    (tmp_path / ".fakegit" / "untracked").write_text("data/E12/new.jsonl\n")
    r = push(checkout, "--exp", "E12", "--slot", "_A")
    assert r.returncode == 1 and "data/E12/new.jsonl" in r.stderr


@pytest.mark.parametrize("gpu,rc", [("", "0"), ("", "1"), ("n/a", "0"), ("1.2.3", "0"),
                                    ("-3", "0"), ("1.5\n2.5", "0")])
def test_pod_push_e12_refuses_an_unreadable_gpu_spend(checkout, tmp_path, gpu, rc):
    """Code review, Amendment D r1 finding 3: when the recorded E12 GPU spend cannot be read the
    push refuses (exit 1) instead of sending 0 to the pod's projections."""
    approve(tmp_path / "logs")
    r = push(checkout, "--exp", "E12", "--slot", "_A",
             env={"FAKE_GPU_SPENT": gpu, "FAKE_GPU_RC": rc})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "recorded E12 GPU spend unreadable" in r.stderr and "refusing" in r.stderr
    assert "pod env keys" not in r.stdout and "DRY RUN (E12)" not in r.stdout


def test_pod_push_e12_sends_a_readable_gpu_spend(checkout, tmp_path):
    approve(tmp_path / "logs")
    for gpu in ("0", "17.9044", "3"):
        r = push(checkout, "--exp", "E12", "--slot", "_A", env={"FAKE_GPU_SPENT": gpu})
        assert r.returncode == 0, r.stderr
        assert "E12_GPU_SPENT_USD" in line(r.stdout, "pod env keys")
    # other experiments never read it
    text = (SCRIPTS / "pod_push.sh").read_text()
    assert text.count('spend.spent("gpu", exp="E12")') == 1


def test_pod_push_refusal_message_and_other_exps_unchanged(checkout, tmp_path):
    r = push(checkout, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "knows E1, I1a, E1b and I9 only" in r.stderr
    assert "EQ0" in r.stderr and "E12" in r.stderr
    (tmp_path / "logs" / "approvals").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "approvals" / "EQ0.approved").write_text(UTC_LINE)
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "--only eq0" in r.stdout and "E12_RECIPE" not in r.stdout
    assert not any(f.startswith(("data/E12", "results/E12")) for f in sent(r.stdout))


# --- pod_setup.sh --only e12 ---------------------------------------------------------------------


def test_pod_setup_only_e12_builds_peft_and_vllm():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    branch = text.split('if [ "$ONLY" = "e12" ]; then', 1)[1].split("\nfi\n", 1)[0]
    pre = text.split('if [ "$ONLY" = "e12" ]; then', 1)[0]
    assert 'build peft pod-peft "torch, transformers, peft, trl, accelerate"' in pre  # shared
    assert 'build vllm vllm "vllm, torch, transformers"' in branch
    assert "import bitsandbytes" in branch and "AdamW8bit" in branch
    assert "import huggingface_hub" in branch  # the upload step
    assert "torch.cuda.is_available()" in branch and "HF_TOKEN" in branch
    assert "unsloth" not in branch and "exit 0" in branch and "(E12)" in branch
    assert "e12) ;;" in text
    assert text.index('if [ "$ONLY" = "e12" ]') < text.index('if [ "$ONLY" = "eq0" ]')
    r = subprocess.run(["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "e13x"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 2 and "e12" in r.stderr and "eq0" in r.stderr


# --- pod_pull_results.sh --exp E12 (both recipes) ------------------------------------------------


def write_step(pod: Path, recipe: str, step: str, files: dict[str, bytes]) -> None:
    lines = []
    for rel, data in files.items():
        p = pod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        lines.append(f"{sha256(p)}  {rel}")
    res = pod / C.rdir(recipe)
    res.mkdir(parents=True, exist_ok=True)
    (res / f"MANIFEST.{step}.sha256").write_text("\n".join(lines) + "\n")


def write_closing(pod: Path, recipe: str, steps: list[str]) -> None:
    res = pod / C.rdir(recipe)
    rels: list[str] = []
    for s in steps:
        m = res / f"MANIFEST.{s}.sha256"
        rels += [ln.split("  ", 1)[1] for ln in m.read_text().splitlines() if ln]
        rels.append(C.manifest_path(recipe, s))
    lines = [f"{sha256(pod / r)}  {r}" for r in dict.fromkeys(rels)]
    (pod / C.manifest_path(recipe, "manifest")).write_text("\n".join(lines) + "\n")


def _pod_tree(pod: Path, recipe: str) -> None:
    R = C.rdir(recipe)
    write_step(pod, recipe, "download", {f"{R}/bench/download.json": b'{"rev": "x"}\n'})
    write_step(pod, recipe, "smoke", {C.bench_path(recipe, "train_bench"): b"{}\n"})
    write_step(pod, recipe, "eval-poison", {
        C.gen_csv(recipe, "full", "P1"): b"a,b\n" * 8,
        C.gen_meta(recipe, "full", "P1"): b"{}\n",
        f"{C.ckpt_dir(recipe, 'P1')}/ckpt.json": b'{"key": "P1"}\n',
    })
    write_closing(pod, recipe, ["download", "smoke", "eval-poison"])
    (pod / R / "status").mkdir(parents=True)
    (pod / R / "status" / "poison_s2.json").write_text("partial\n")  # unlisted: never promoted


@pytest.fixture
def pull(tmp_path):
    s = copy_script(tmp_path, "pod_pull_results.sh")
    pod = tmp_path / "pod"  # stands for the pod's /ws/emx
    for recipe in C.RECIPES:
        _pod_tree(pod, recipe)
    logs = pod / C.POD_LOG_DIR
    logs.mkdir(parents=True)
    (logs / "run_20261008T120000Z.log").write_text("E12_STEP_DONE download\n")
    return s, pod


def run_pull(script, pod, *args):
    return run_bash(script, "--exp", "E12", *args, env_extra={"POD_PULL_SOURCE": str(pod)})


@pytest.mark.parametrize("recipe", sorted(C.RECIPES))
def test_pull_e12_closing_manifest_of_either_recipe(pull, tmp_path, recipe):
    script, pod = pull
    args = () if recipe == C.DEFAULT_RECIPE else ("--recipe", recipe)
    r = run_pull(script, pod, *args)
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / C.rdir(recipe)
    assert (tmp_path / C.gen_csv(recipe, "full", "P1")).read_bytes() == b"a,b\n" * 8
    assert (tmp_path / C.ckpt_dir(recipe, "P1") / "ckpt.json").exists()
    for step in ("download", "smoke", "eval-poison", "manifest"):
        assert (tmp_path / C.manifest_path(recipe, step)).exists()
    assert not (dest / "status").exists()  # unlisted
    other = next(x for x in C.RECIPES if x != recipe)
    assert not (tmp_path / C.rdir(other)).exists()  # only the named recipe's tree
    assert f"verifying {C.manifest_path(recipe, 'manifest')}" in r.stdout
    assert f"recipe {recipe}" in r.stdout
    assert not list((tmp_path / "results" / "E12").glob(".pull_*"))
    assert (tmp_path / C.POD_LOG_DIR / "run_20261008T120000Z.log").exists()


def test_pull_e12_mid_run_named_manifest(pull, tmp_path):
    script, pod = pull
    (pod / C.manifest_path("rank32", "manifest")).unlink()
    r = run_pull(script, pod, "--recipe", "rank32", "--manifest", "MANIFEST.smoke.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / C.bench_path("rank32", "train_bench")).exists()
    assert not (tmp_path / C.gen_csv("rank32", "full", "P1")).exists()


@pytest.mark.parametrize(
    "breakage",
    ["tamper_output", "missing_output", "no_closing", "outside_path", "other_exp_path",
     "dotdot_path", "stale"],
)
def test_pull_e12_refuses_and_promotes_nothing(pull, tmp_path, breakage):
    script, pod = pull
    R = C.rdir("rank1")
    if breakage == "tamper_output":
        (pod / C.gen_csv("rank1", "full", "P1")).write_bytes(b"x,y\n" * 8)
    elif breakage == "missing_output":
        (pod / C.gen_meta("rank1", "full", "P1")).unlink()
    elif breakage == "no_closing":
        (pod / C.manifest_path("rank1", "manifest")).unlink()
    elif breakage in ("outside_path", "other_exp_path"):
        rel = "data/x.txt" if breakage == "outside_path" else "results/EQ0/x.txt"
        write_step(pod, "rank1", "smoke", {rel: b"x\n"})
        write_closing(pod, "rank1", ["download", "smoke", "eval-poison"])
    elif breakage == "dotdot_path":
        p = pod / C.bench_path("rank1", "train_bench")
        (pod / R / "MANIFEST.smoke.sha256").write_text(
            f"{sha256(p)}  {R}/../rank1/bench/train_bench.json\n")
        write_closing(pod, "rank1", ["download", "smoke", "eval-poison"])
    elif breakage == "stale":
        write_step(pod, "rank1", "repair", {f"{R}/train/repair_A1.json": b"{}\n"})
    r = run_pull(script, pod)
    assert r.returncode != 0, r.stdout
    assert "nothing promoted" in r.stderr
    dest = tmp_path / R
    assert not (dest / "gen").exists() and not list(dest.glob("MANIFEST.*"))


def test_pull_e12_recovery_manifest_by_name_only(pull, tmp_path):
    script, pod = pull
    R = C.rdir("rank1")
    (pod / C.manifest_path("rank1", "manifest")).unlink()
    p = pod / R / "train" / "poison_s1.steps.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"step": 1}\n')
    (pod / C.recovery_manifest("rank1")).write_text(
        f"{sha256(p)}  {R}/train/poison_s1.steps.jsonl\n")
    r = run_pull(script, pod)
    assert r.returncode == 1 and "--manifest MANIFEST.recovery.sha256" in r.stderr
    r = run_pull(script, pod, "--manifest", "MANIFEST.recovery.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / R / "train" / "poison_s1.steps.jsonl").exists()
    assert (tmp_path / C.recovery_manifest("rank1")).exists()


def test_pull_e12_moves_changed_local_files_aside(pull, tmp_path):
    script, pod = pull
    assert run_pull(script, pod).returncode == 0
    f = C.bench_path("rank1", "train_bench")
    write_step(pod, "rank1", "smoke", {f: b'{"rerun": 1}\n'})
    write_closing(pod, "rank1", ["download", "smoke", "eval-poison"])
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    sup = tmp_path / "results" / "E12" / "superseded"
    kept = list(sup.glob("*/rank1/bench/train_bench.json"))
    assert len(kept) == 1 and kept[0].read_text() == "{}\n"


@pytest.mark.parametrize(
    "args",
    [("--manifest", "MANIFEST.x.partial.sha256"), ("--manifest", "../x.sha256"),
     ("--recipe", "rank8"), ("--subdir", "x"), ("--run", "c"), ("--slot", "_B")],
)
def test_pull_e12_usage_errors(pull, tmp_path, args):
    script, pod = pull
    assert run_pull(script, pod, *args).returncode == 2
    assert not (tmp_path / "results" / "E12").exists()


def test_pull_recipe_is_e12_only_and_unknown_exp_lists_e12(pull):
    script, pod = pull
    r = run_bash(script, "--exp", "EQ0", "--recipe", "rank1",
                 env_extra={"POD_PULL_SOURCE": str(pod)})
    assert r.returncode == 2 and "--recipe is E12-only" in r.stderr
    r = run_bash(script, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "EQ0" in r.stderr and "E12" in r.stderr


# --- e12_watchdog.sh: one tick from a copy in tmp_path, with a fake `uv` and `ssh` ---------------


ARCHIVE_CALL = "run python scripts/e12_control.py archive --slot _A --while-running"
FINAL_ARCHIVE_CALL = "run python scripts/e12_control.py archive --slot _A --no-upload"
STOP_CALL = "run python scripts/e12_control.py stop --slot _A"
RETAINED_OK = 'echo \'{"cmd": "archive", "retained": {"rank32": true}}\''
STOP_OK = 'echo \'{"cmd": "stop", "ok": true}\''


def tick(tmp_path, rc, running=(), interval="600", *, extra=None, archive=RETAINED_OK,
         stop=STOP_OK, env=None):
    """One watchdog tick with a fake `uv` (the budget check prints `status`; the controller's
    stop runs `stop`, its archive `archive`) and an unreachable `ssh`."""
    script = copy_script(tmp_path, "e12_watchdog.sh")
    calls = tmp_path / "uv_calls.txt"
    status = {"exp": "E12", "running": list(running), "hard_stop": rc == 2, **(extra or {})}
    body = (f'echo "$*" >> {calls}\n'
            f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};;\n"
            f"  *'e12_control.py stop'*) {stop}; exit 0;;\n"
            f"  *e12_control.py*) {archive}; exit 0;; esac\n"
            "exit 0\n")
    bindir = fake_bin(tmp_path, "uv", body)
    fake_bin(tmp_path, "ssh", f'echo "ssh $*" >> {tmp_path / "ssh_calls.txt"}\nexit 1\n')
    r = run_bash(script, interval, env_extra={
        "PATH": f"{bindir}:{os.environ['PATH']}", "EMX_EXP": "E1", "E12_WATCHDOG_ONCE": "1",
        "E12_WATCHDOG_STOP": str(tmp_path / "stop"), "E12_IDLE_ALERT": str(tmp_path / "idle"),
        "E12_WATCHDOG_ALERT": str(tmp_path / "alert"),
        "E12_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"), "E12_SALVAGE_S": "2",
        **(env or {}),
    })
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def _wait_for_call(tmp_path, text: str, seconds: float = 10.0) -> list[str]:
    """A background archive may log its call after the one tick exits."""
    calls = tmp_path / "uv_calls.txt"
    t0 = time.time()
    while time.time() - t0 < seconds:
        lines = calls.read_text().splitlines() if calls.exists() else []
        if any(text in ln for ln in lines):
            return lines
        time.sleep(0.1)
    return calls.read_text().splitlines() if calls.exists() else []


def _alert(tmp_path) -> str:
    p = tmp_path / "alert"
    return p.read_text() if p.exists() else ""


def _left(hours: float) -> float:
    """Pod hours `hours` short of the contract's pod-hour limit."""
    return round(C.POD_HOURS_LIMIT - hours, 4)


def _pod(hours: float) -> dict:
    return {"pod_id": "pq", "slot": "_A", "gpu": "NVIDIA A100-SXM4-80GB", "hours": hours,
            "rate": 1.39, "rate_source": "api", "usd": round(hours * 1.39, 4)}


def test_e12_watchdog_healthy_tick_beats_and_reconciles(tmp_path):
    r, lines = tick(tmp_path, 0, running=[_pod(2.0)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("run python scripts/pod_budget_check.py --exp E12 --reserve-hours")
    assert lines[1].startswith("run python scripts/pod_down.py --exp E12 --untracked")
    t, interval, pid = (tmp_path / "heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def _before(lines, *prefixes) -> bool:
    """Each prefix's first matching call comes before the next prefix's."""
    idx = [next(i for i, ln in enumerate(lines) if ln.startswith(p)) for p in prefixes]
    return idx == sorted(idx)


def test_e12_watchdog_stops_at_the_pod_hour_limit_on_any_gpu(tmp_path):
    """A cheap A100 pod is far under the GPU $ line, but 0.1 h short of the pod-hour limit plus
    one 10-minute interval it reaches the limit: a hard stop (STOP, verified, the final archive,
    then pod_down)."""
    assert _left(0.1) * 1.39 < C.GPU_LIMIT_USD
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.1))])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "POD HOURS" in r.stdout and "HARD STOP" in r.stdout and "e12-*" in r.stdout
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")
    assert _before(lines, STOP_CALL, FINAL_ARCHIVE_CALL, "run python scripts/pod_down.py")
    assert ARCHIVE_CALL not in lines  # the final archive is never the while-running one
    # 0.1 h (360 s) left minus the 90 s margin
    assert re.search(r"FINAL STOP started pid \d+, bounded at 2(69|70)s", r.stdout)
    assert "RETENTION (final): rank32=true" in r.stdout and "NOT CONFIRMED" not in _alert(tmp_path)
    assert "--untracked" not in "\n".join(lines)
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.2))])  # + 0.167 h stays under the limit
    assert r.returncode == 0 and "POD HOURS" not in r.stdout


def test_e12_watchdog_hard_stop_and_fail_closed(tmp_path):
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2 and "HARD STOP" in r.stdout
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")
    assert _before(lines, STOP_CALL, FINAL_ARCHIVE_CALL)
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 3)
    assert r.returncode == 3
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")
    assert FINAL_ARCHIVE_CALL in lines and "bounded at 2s" in r.stdout  # line unknown: 2 s
    assert "BUDGET CHECK FAILED" in (tmp_path / "alert").read_text()


def test_e12_watchdog_final_stop_before_the_line_then_archive(tmp_path):
    """Code review r2 finding 2: 0.3 h short, the pod-hour line is 1080 s away (< the 1200 s lead): on a
    healthy tick (no hard stop yet) STOP is written and verified, then the NON-while-running
    archive runs and declares retention; no early archive, no termination yet."""
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.3))])
    assert r.returncode == 0 and "HARD STOP" not in r.stdout, r.stdout + r.stderr
    assert _before(lines, STOP_CALL, FINAL_ARCHIVE_CALL)
    stop = next(ln for ln in lines if ln.startswith(STOP_CALL))
    assert "--stop-polls 10 --poll-interval-s 60" in stop  # 600 s at a minute apart
    assert ARCHIVE_CALL not in lines and "EARLY ARCHIVE" not in r.stdout
    assert "FINAL STOP verified" in r.stdout and "RETENTION (final): rank32=true" in r.stdout
    assert not any("--all-exp" in ln for ln in lines)
    assert re.search(r"FINAL STOP started pid \d+, bounded at 9[89][0-9]s", r.stdout)


def test_e12_watchdog_unverified_stop_never_declares_retention(tmp_path):
    """The stop is not acknowledged (ok false): no final archive, a loud alert; the archive that
    still runs is the best-effort while-running one, never declared final."""
    no = 'echo \'{"cmd": "stop", "ok": false}\'; exit 1'
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.1))], stop=no)
    assert r.returncode == 2, r.stdout + r.stderr
    assert FINAL_ARCHIVE_CALL not in lines and ARCHIVE_CALL in lines
    assert "FINAL STOP NOT VERIFIED" in _alert(tmp_path)
    assert "RETENTION NOT CONFIRMED (final)" in _alert(tmp_path)
    assert "RETENTION (final): rank32=true" not in r.stdout
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")


def test_e12_watchdog_hung_stop_still_terminates_on_time(tmp_path):
    """A stop that hangs is killed at its bound; termination follows before the kill line."""
    t0 = time.time()
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.1))], stop="sleep 41.75",
                    env={"E12_STOP_MARGIN_S": "355"})  # 360 s left: a 5 s bound
    assert r.returncode == 2 and time.time() - t0 < 25, r.stdout + r.stderr
    assert "FINAL STOP NOT VERIFIED (stop exit 124" in _alert(tmp_path)
    assert FINAL_ARCHIVE_CALL not in lines
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")
    _gone("^sleep 41[.]75$")


def _gone(pattern: str) -> None:
    for _ in range(30):  # the process tree is gone (SIGTERM, then reaped)
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True,
                             check=False)
        if not out.stdout.strip():
            break
        time.sleep(0.1)
    assert out.stdout.strip() == ""


def test_e12_watchdog_pod_hours_check_fails_closed(tmp_path):
    r, lines = tick(tmp_path, 0, running=[{"slot": "_A", "hours": "not a number"}])
    assert r.returncode == 3 and "POD-HOURS CHECK FAILED" in r.stdout
    assert any(ln.startswith("run python scripts/pod_down.py --exp E12 --all-exp") for ln in lines)


def test_e12_watchdog_starts_the_early_archive_half_an_hour_before_the_hour_limit(tmp_path):
    """Review r1 finding 2 (a): 0.6 h short of the pod-hour limit, the stop tick is 0.43 h away (< 0.5 h): the
    while-running archive starts in the background and the tick goes on (no stop)."""
    r, _ = tick(tmp_path, 0, running=[_pod(_left(0.6))])
    assert r.returncode == 0 and "HARD STOP" not in r.stdout, r.stdout + r.stderr
    assert "EARLY ARCHIVE" in r.stdout and "bounded at 2700s" in r.stdout
    lines = _wait_for_call(tmp_path, "e12_control.py")
    assert ARCHIVE_CALL in lines
    assert any(ln.startswith("run python scripts/pod_down.py --exp E12 --untracked") for ln in lines)
    assert list((tmp_path / "logs").glob("watchdog_E12_archive_early_*.log"))
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.8))])  # 0.63 h away: not yet
    assert r.returncode == 0 and "EARLY ARCHIVE" not in r.stdout
    assert not any("e12_control.py" in ln for ln in _wait_for_call(tmp_path, "e12_control", 1))


def test_e12_watchdog_early_archive_follows_the_gpu_kill_line_too(tmp_path):
    """$1.50 of headroom at $3.49/h is 0.43 h to the stop tick: archive early at 2 pod-hours."""
    line = C.GPU_LIMIT_USD
    gpu = {"gpu_kill_limit_usd": line, "projected_gpu_usd": line - 1.5, "burn_per_hr": 3.49,
           "reserve_hours": 600 / 3600}
    r, _ = tick(tmp_path, 0, running=[_pod(2.0)], extra=gpu)
    assert r.returncode == 0 and "EARLY ARCHIVE" in r.stdout, r.stdout + r.stderr
    assert ARCHIVE_CALL in _wait_for_call(tmp_path, "e12_control.py")
    (tmp_path / "uv_calls.txt").unlink()
    r, _ = tick(tmp_path, 0, running=[_pod(2.0)], extra={**gpu, "projected_gpu_usd": line - 5.0})
    assert r.returncode == 0 and "EARLY ARCHIVE" not in r.stdout


def test_e12_watchdog_hard_stop_alerts_when_retention_is_not_confirmed(tmp_path):
    no = 'echo \'{"cmd": "archive", "retained": {"rank32": false}}\''
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.1))], archive=no)
    assert r.returncode == 2 and "RETENTION (final): rank32=false" in r.stdout
    assert "RETENTION NOT CONFIRMED" in _alert(tmp_path)
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")
    (tmp_path / "alert").unlink()
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.1))], archive="echo crashed; exit 1")
    assert r.returncode == 2 and "RETENTION NOT CONFIRMED" in _alert(tmp_path)
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")


def test_e12_watchdog_terminates_on_time_when_the_archive_hangs(tmp_path):
    """Review r1 finding 2 (c): a hung final archive is killed at its bound; termination
    follows."""
    t0 = time.time()
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.1))], archive="sleep 41.25",
                    env={"E12_STOP_MARGIN_S": "355"})  # 360 s left: a 5 s bound
    assert r.returncode == 2 and time.time() - t0 < 25, r.stdout + r.stderr
    assert "ARCHIVE (final) timed out" in _alert(tmp_path)
    assert "RETENTION NOT CONFIRMED" in _alert(tmp_path)
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")
    _gone("^sleep 41[.]25$")


def test_e12_watchdog_never_archives_past_the_kill_line(tmp_path):
    """36 s left before the pod-hour limit (less than the 90 s margin): archive and salvage are skipped, the pod
    is terminated at once, and the alert says retention is not confirmed."""
    r, lines = tick(tmp_path, 0, running=[_pod(_left(0.01))])
    assert r.returncode == 2 and "NO TIME before the kill line" in _alert(tmp_path)
    assert not any("e12_control.py" in ln for ln in lines)
    assert not (tmp_path / "ssh_calls.txt").exists()  # no salvage ssh either
    assert lines[-1].startswith("run python scripts/pod_down.py --exp E12 --all-exp")


def test_e12_watchdog_limit_and_names_follow_the_contracts():
    text = (SCRIPTS / "e12_watchdog.sh").read_text()
    m = re.search(r"^POD_HOURS_LIMIT=([0-9.]+)", text, re.MULTILINE)
    assert m and float(m.group(1)) == C.POD_HOURS_LIMIT
    assert C.WATCHDOG_HEARTBEAT in text and "logs/e12/run_" in text and "'^E12_'" in text
    assert "--exp EQ0" not in text and "eq0-" not in text
    salvage = text.split("salvage() {", 1)[1].split("\n}\n", 1)[0]
    assert '"cd /ws/emx && tar cf - results/E12 logs 2>/dev/null; exit 0"' in salvage
    # r2 finding 2: the final stop writes STOP, verifies it, and only then runs the non
    # while-running archive; the hard stop runs it (or waits for it) before pod_down; the early
    # archive's lead is half an hour, the final stop's 20 minutes
    final = text.split("final_stop() {", 1)[1].split("\n}\n", 1)[0]
    assert final.index("ctl_argv stop --slot _A") < final.index("FINAL STOP verified") < (
        final.index("ctl_argv archive --slot _A --no-upload")) < final.index("retention final")
    kill_all = text.split("kill_all() {", 1)[1].split("\n}\n", 1)[0]
    assert kill_all.index("final_wait") < kill_all.index("scripts/pod_down.py --exp E12 --all-exp")
    assert "ctl_argv archive --slot _A --while-running" in text
    assert "uv run python scripts/e12_control.py $*" in text
    assert re.search(r"^ARCHIVE_LEAD_H=\$\{E12_ARCHIVE_LEAD_H:-0\.5\}", text, re.MULTILINE)
    assert re.search(r"^FINAL_LEAD_S=\$\{E12_FINAL_LEAD_S:-1200\}", text, re.MULTILINE)


# --- shell hygiene -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["pod_push.sh", "pod_setup.sh", "pod_pull_results.sh", "e12_watchdog.sh"]
)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in (*BASH4, "${x,,}"):
        assert bad not in code, (name, bad)


def test_watchdog_is_executable_like_eq0s():
    assert os.access(SCRIPTS / "e12_watchdog.sh", os.X_OK) == os.access(
        SCRIPTS / "eq0_watchdog.sh", os.X_OK)
    assert shutil.which("bash")
