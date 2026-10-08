"""EQ0 pod tooling, offline (EQ0 implementation plan, unit A). `--exp EQ0` in emx.runpod /
pod_up / pod_down / pod_budget_check (one pod eq0-a in slot _A, logs/pods_EQ0.jsonl, CAPS["EQ0"],
logs/approvals/EQ0.approved, a reservation, GPU fallback H100 secure -> NVL -> PCIe -> A100),
pod_push.sh's EQ0 transfer set and pod env, pod_setup.sh --only eq0, pod_pull_results.sh --exp EQ0
and scripts/eq0_watchdog.sh. Copied from tests/i9/test_pod_i9.py (whose helpers, shared with
tests/i1a/test_pod_i1a.py, are reused). Nothing here contacts RunPod, SSH or the Hub: fake
clients and fake git/uv/ssh/rsync on PATH, sandboxed event logs, ledgers and reservations. The
real scripts/pod_run_eq0.sh and scripts/eq0_pod.py are never created here (fake checkouts only)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp
from emx.eq0 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UTC_LINE = "2026-10-08T12:00:00Z approved by Jacob (test)\n"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# helpers only (no fixtures or tests are bound here, so nothing of that module is collected)
_i1a = _load("_eq0_reuse_i1a_pod_tests", ROOT / "tests" / "i1a" / "test_pod_i1a.py")
FAKE_GIT, copy_script, fake_bin, run_bash, sha256 = (
    _i1a.FAKE_GIT,
    _i1a.copy_script,
    _i1a.fake_bin,
    _i1a.run_bash,
    _i1a.sha256,
)
FakeSpend, FakeClient, HEAD, LAPTOP, BASH4 = (
    _i1a.FakeSpend,
    _i1a.FakeClient,
    _i1a.HEAD,
    _i1a.LAPTOP,
    _i1a.BASH4,
)


@pytest.fixture
def eq0(monkeypatch, tmp_path):
    """emx.runpod configured for EQ0 with .env, logs and the event log in tmp_path; restored."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    rp.configure("EQ0")
    return tmp_path


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


def approve(logs: Path, text: str = UTC_LINE) -> Path:
    p = logs / "approvals" / "EQ0.approved"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def beat(logs: Path) -> None:
    (logs / "watchdog_EQ0.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")


def created(pod_id, slot, t0, rate):
    return {
        "event": "created",
        "pod_id": pod_id,
        "slot": slot,
        "gpu": "g",
        "utc": t0.isoformat(),
        "rate_usd_h": rate,
    }


# --- emx.runpod under EQ0 ------------------------------------------------------------------------


def test_configure_eq0_names_logs_and_caps(eq0):
    assert rp.EXP == "EQ0" and rp.POD_NAME_PREFIX == "eq0-"
    assert rp.POD_EVENTS == eq0 / "pods_EQ0.jsonl"
    c = rp.caps()
    assert (c.hard_cap_usd, c.gpu_limit_usd) == (C.HARD_CAP_USD, C.GPU_LIMIT_USD) == (20.0, 15.0)
    assert rp.gpu_kill_limit(0.0) == 15.0 and rp.gpu_kill_limit(8.0) == 12.0  # 20 - 0 - 8
    assert rp.gpu_ceiling() is None
    assert rp.pod_body(rp.GPU_FALLBACK[0], "_A", "ssh-ed25519 k")["name"] == "eq0-a"
    assert rp.heartbeat_path() == eq0 / Path(C.WATCHDOG_HEARTBEAT).relative_to("logs")


def test_untracked_reaper_only_sees_eq0_pods(eq0):
    api = [{"id": "x", "name": "eq0-a"}, {"id": "y", "name": "e3-a"}, {"id": "z", "name": "e1-a"}]
    assert [p["id"] for p in rp.untracked_pods(api, [])] == ["x"]


def test_killswitch_deadline_at_the_eq0_gpu_line(eq0, spend):
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    events = [created("pa", "_A", now - timedelta(hours=1), 3.49)]
    spend.totals = {"gpu": 2.0, "judge": 0.0}
    want = int(now.timestamp() + (15 - 2 - 3.49) / 3.49 * 3600)  # min(15, 20 - 0) = 15
    assert rp.killswitch_deadline("pa", events, now) == want


# --- pod_up --exp EQ0 ----------------------------------------------------------------------------


@pytest.fixture
def up(spend, eq0, monkeypatch):
    mod = _load("_eq0_pod_up", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def use_client(monkeypatch, fake):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)


def test_eq0_fallback_order_follows_the_contracts(up):
    got = [(o.gpu_type_id, o.cloud, o.rate_usd_h) for o in up.eq0_gpu_options()]
    assert got == [
        ("NVIDIA H100 80GB HBM3", "SECURE", 3.49),
        ("NVIDIA H100 NVL", "SECURE", 2.59),
        ("NVIDIA H100 PCIe", "COMMUNITY", 2.69),
        ("NVIDIA A100-SXM4-80GB", "SECURE", 1.39),
    ]
    assert [g for g, _c, _r in got] == [g for g, _r in C.FALLBACK_GPUS]
    assert next(o.gpu_type_id for o in up.eq0_gpu_options(2)) == "NVIDIA H100 PCIe"


def test_pod_up_eq0_refuses_without_its_approval(up, eq0, monkeypatch):
    beat(eq0)
    (eq0 / "approvals").mkdir()
    (eq0 / "approvals" / "I9-P1.approved").write_text(UTC_LINE)  # another experiment's: no
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    args = ["--exp", "EQ0", "--slot", "_A", "--reservation", "r1", "--budget-usd", "15"]
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []
    approve(eq0, "go\n")  # no UTC line: still refused
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []


@pytest.mark.parametrize("slot", ["_B", "_C"])
def test_pod_up_eq0_refuses_any_slot_but_a(up, eq0, monkeypatch, slot):
    beat(eq0)
    approve(eq0)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "EQ0", "--slot", slot, "--reservation", "r1", "--budget-usd", "15"])
    assert e.value.code == 2 and fake.created == []


def test_pod_up_eq0_needs_a_reservation(up, eq0, monkeypatch):
    beat(eq0)
    approve(eq0)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "EQ0", "--slot", "_A"])
    assert e.value.code == 2 and fake.created == []


def test_pod_up_eq0_needs_a_live_watchdog(up, eq0, monkeypatch, tmp_path):
    approve(eq0)
    monkeypatch.setenv("EMX_RUNPOD_RESERVATIONS", str(tmp_path / "res.jsonl"))
    from emx import runpod_reservations as rr

    rid = rr.reserve("EQ0", 15.0, "EQ0 pod", balance=200.0)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "EQ0", "--slot", "_A", "--reservation", rid, "--budget-usd", "15"])
    assert e.value.code == up.UNTRACKED and fake.created == []


class _ReservedClient(FakeClient):
    def list_pods(self):
        return []


def test_pod_up_eq0_reserved_launch_on_the_first_option(eq0, monkeypatch, tmp_path, capsys):
    """Real emx.spend and emx.runpod_reservations, both on sandboxed files."""
    from emx import runpod_reservations as rr
    from emx import spend as real_spend

    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "res.jsonl"))
    monkeypatch.setenv(real_spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    up = _load("_eq0_pod_up_reserved", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    beat(eq0)
    approve(eq0)
    rid = rr.reserve("EQ0", C.POD_RESERVATION_USD, "EQ0 pod", balance=200.0)
    fake = _ReservedClient(create_results=[{"id": "pq", "costPerHr": 3.49}])
    use_client(monkeypatch, fake)
    rc = up.main(["--exp", "EQ0", "--slot", "_A", "--reservation", rid, "--budget-usd", "15"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert [(b["name"], b["cloudType"], b["gpuTypeIds"][0]) for b in fake.created] == [
        ("eq0-a", "SECURE", "NVIDIA H100 80GB HBM3")
    ]
    assert fake.created[0]["containerDiskInGb"] == C.VOLUME_GB
    assert "EMX_POD_CREATE=ok" in out and "EMX_POD_DEADLINE=" in out
    assert rr.get(rid)["launched"] and rr.get(rid)["pods"] == ["pq"]
    ev = rp.read_pod_events()[0]
    assert ev["event"] == "created" and ev["exp"] == "EQ0" and ev["reservation_id"] == rid
    assert rp.env("RUNPOD_POD_ID_A") == "pq"


def run_py(*args, tmp_path):
    env = {
        **os.environ,
        "EMX_EXP": "E1",  # ignored: every script takes --exp
        "EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl"),
        "EMX_POD_LOG_DIR": str(tmp_path),
    }
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def dry_requests(stdout: str) -> list[dict]:
    parts = stdout.split("DRY RUN (not sent):\n")[1:]
    return [json.loads(p.split("\nbudget gate")[0]) for p in parts]


def test_pod_up_dry_run_eq0(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "EQ0", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert [q["method"] for q in reqs] == ["POST"] * 4  # nothing but the (unsent) creates
    assert {q["json"]["name"] for q in reqs} == {"eq0-a"}
    assert [(q["json"]["cloudType"], q["json"]["gpuTypeIds"][0]) for q in reqs] == [
        ("SECURE", "NVIDIA H100 80GB HBM3"),
        ("SECURE", "NVIDIA H100 NVL"),
        ("COMMUNITY", "NVIDIA H100 PCIe"),
        ("SECURE", "NVIDIA A100-SXM4-80GB"),
    ]
    assert {q["json"]["containerDiskInGb"] for q in reqs} == {C.VOLUME_GB}
    assert "approval (EQ0): MISSING" in r.stdout
    assert "watchdog (EQ0): NOT LIVE" in r.stdout and "budget gate (EQ0)" in r.stdout
    assert not (tmp_path / "pods_EQ0.jsonl").exists()  # a dry run logs no event
    approve(tmp_path)
    beat(tmp_path)
    r = run_py("scripts/pod_up.py", "--exp", "EQ0", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0 and "approval (EQ0): present" in r.stdout
    assert "watchdog (EQ0): live" in r.stdout


def test_pod_up_dry_run_eq0_refuses_other_slots(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "EQ0", "--slot", "_B", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "EQ0 has one pod" in r.stderr and "DRY RUN" not in r.stdout


def test_pod_down_and_budget_check_dry_eq0(tmp_path):
    r = run_py("scripts/pod_down.py", "--exp", "EQ0", "--all-exp", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "<each eq0-* pod id>" in r.stdout
    r = run_py("scripts/pod_budget_check.py", "--exp", "EQ0", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["exp"] == "EQ0" and d["gpu_kill_limit_usd"] == 15.0 and d["hard_cap_usd"] == 20.0
    assert d["gpu_ceiling_usd"] is None and d["hard_stop"] is False


def test_budget_check_eq0_kills_at_the_gpu_line(spend, eq0, capsys):
    bc = _load("_eq0_budget", SCRIPTS / "pod_budget_check.py")
    now = datetime.now(UTC)
    rp.POD_EVENTS.write_text(json.dumps(created("pa", "_A", now - timedelta(hours=2), 3.49)) + "\n")
    spend.totals = {"gpu": 1.0, "judge": 0.0}
    assert bc.main(["--exp", "EQ0"]) == 0  # 1 + 6.98 < 15
    assert bc.main(["--exp", "EQ0", "--reserve-hours", "3"]) == 2  # + 10.47 >= 15
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["hard_stop"] is True


# --- pod_push.sh --exp EQ0 (a copy in tmp_path, a fake checkout and a FAKE git) ------------------

TRACKED = [
    "pyproject.toml",
    "uv.lock",
    "src/emx/__init__.py",
    "src/emx/eq0/contracts.py",
    "src/emx/qwen/registry.py",
    "scripts/eq0_pod.py",
    "scripts/pod_run_eq0.sh",
    "data/E1/questions/general.jsonl",
    "data/E1/questions/medical.jsonl",  # tracked, but not an EQ0 root
    "data/E1/manifest.json",  # not an EQ0 root
    "data/E1b/questions/ext48.jsonl",
    "data/E1b/manifest.json",  # not an EQ0 root
    "data/I9/scenarios.jsonl",  # another experiment's data
    "data/shared/answer_pairs_v1.jsonl",  # not an EQ0 root
    "configs/E1/a.json",
    "prompts/I9/monitor_zero_shot.txt",
    "third_party/workspace-bench/pyproject.toml",
    "docs/private-notes.md",
]
IGNORED = ["data/EQ0/bad_medical_advice.jsonl"]  # gitignored under data/EQ0: a named extra
NEVER = [
    ".env.local",
    "data/E1/questions/narrow.jsonl",  # held out (gitignored): never leaves the laptop
    "data/E1/train_bad.jsonl",
    "data/raw/bad_medical_advice.jsonl",
    "results/EQ0/gen/base.csv",
    "logs/approvals/I9-P1.approved",
]
REQUIRED = [
    "data/EQ0/bad_medical_advice.jsonl",
    "data/E1/questions/general.jsonl",
    "data/E1b/questions/ext48.jsonl",
    "scripts/eq0_pod.py",
    "scripts/pod_run_eq0.sh",
    "logs/approvals/EQ0.approved",
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


def push(checkout, *args, dry=True, env=None):
    tmp = checkout.parent.parent
    extra = {
        "POD_PUSH_GIT": str(tmp / "bin" / "fakegit"),
        "FAKEGIT_DIR": str(tmp / ".fakegit"),
        "EMX_EXP": "E1",  # ignored
        **(env or {}),
    }
    flags = ("--dry-run", "--show-files") if dry else ()
    return run_bash(checkout, *args, *flags, env_extra=extra)


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def line(stdout: str, tag: str) -> list[str]:
    hit = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return hit.split(f"{tag}:", 1)[1].split()


def test_pod_push_eq0_transfer_set_env_and_setup(checkout, tmp_path):
    approve(tmp_path / "logs")
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (EQ0)" in r.stdout and "WARNING" not in r.stdout
    assert line(r.stdout, "transfer set") == [
        "src",
        "scripts",
        "data/EQ0",
        "data/E1/questions/general.jsonl",
        "data/E1b/questions/ext48.jsonl",
        "pyproject.toml",
        "uv.lock",
    ]
    files = sent(r.stdout)
    want = {f for f in TRACKED if f.startswith(("src/", "scripts/"))} | {
        "pyproject.toml",
        "uv.lock",
        "data/E1/questions/general.jsonl",
        "data/E1b/questions/ext48.jsonl",
    }
    assert files == want | set(IGNORED) | {"logs/approvals/EQ0.approved"}
    assert set(REQUIRED) <= files
    assert "data/E1/questions/narrow.jsonl" not in files
    assert not files & set(NEVER)
    assert not any(
        f.startswith(
            ("data/I9", "data/shared", "data/E1b/manifest", "configs/", "prompts/", "third_party/")
        )
        for f in files
    )
    keys = line(r.stdout, "pod env keys")
    assert keys[0] == "HF_TOKEN" and "HF_USERNAME" not in keys  # HF_TOKEN only
    assert {
        "EMX_EXP",
        "EMX_GIT_COMMIT",
        "EMX_GIT_DIRTY",
        "HF_HOME",
        "HF_HUB_DISABLE_XET",
        "WANDB_MODE",
    } <= set(keys)
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "hf_abc" not in r.stdout and "secret" not in r.stdout
    assert "--check-slot _A" in r.stdout and "pod_setup.sh --only eq0" in r.stdout
    assert "pod_killswitch.sh arm" in r.stdout


def test_pod_push_eq0_never_ships_the_held_out_questions():
    """narrow.jsonl is outside every EQ0 root, and the EQ0 branch refuses it outright."""
    text = (SCRIPTS / "pod_push.sh").read_text()
    roots = next(ln for ln in text.splitlines() if ln.lstrip().startswith("EQ0) ROOTS="))
    assert "data/E1 " not in roots and "data/E1/questions " not in roots
    assert "narrow" not in roots.split("#", 1)[0]
    branch = text.split('elif [ "$EXP" = "EQ0" ]; then', 1)[1].split("\nelse\n", 1)[0]
    assert "grep -qxF data/E1/questions/narrow.jsonl" in branch and "refusing" in branch


def test_pod_push_eq0_warns_on_missing_inputs_in_dry_run(checkout, tmp_path):
    (tmp_path / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if f not in ("scripts/eq0_pod.py", "scripts/pod_run_eq0.sh"))
        + "\n"
    )
    (tmp_path / "data" / "EQ0" / "bad_medical_advice.jsonl").unlink()
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")  # and no approval
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "WARNING" in ln)
    for f in (
        "scripts/eq0_pod.py",
        "scripts/pod_run_eq0.sh",
        "data/EQ0/bad_medical_advice.jsonl",
        "logs/approvals/EQ0.approved",
    ):
        assert f in warn
    assert "data/E1/questions/general.jsonl" not in warn


def test_pod_push_eq0_real_push_refuses_missing_inputs_before_contacting_the_pod(
    checkout, tmp_path
):
    """Without --dry-run: the session check is a fake uv; ssh and rsync are fakes that record any
    call. A missing required input refuses (exit 1) and nothing is sent."""
    calls = tmp_path / "calls.txt"
    fake_bin(
        tmp_path,
        "uv",
        f'echo "uv $*" >> {calls}\n'
        'echo "EMX_POD_RATE_USD_H=3.49"; echo "EMX_POD_CREATED_EPOCH=1760000000"\n',
    )
    for tool in ("ssh", "rsync", "scp"):
        fake_bin(tmp_path, tool, f'echo "{tool} $*" >> {calls}\nexit 1\n')
    with (tmp_path / ".env").open("a") as f:
        f.write("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\nRUNPOD_POD_ID_A=pq\n")
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    r = push(checkout, "--exp", "EQ0", "--slot", "_A", dry=False, env={"PATH": path})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "required EQ0 inputs missing" in r.stderr and "logs/approvals/EQ0.approved" in r.stderr
    got = calls.read_text().splitlines() if calls.exists() else []
    assert not any(c.startswith(("ssh", "rsync", "scp")) for c in got), got


def test_pod_push_eq0_refuses_bad_slots_modified_and_untracked_files(checkout, tmp_path):
    approve(tmp_path / "logs")
    for slot in ("_B", "_C"):
        r = push(checkout, "--exp", "EQ0", "--slot", slot)
        assert r.returncode == 2 and "slot must be _A" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")
    assert r.returncode == 1 and "commit them before an EQ0 push" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("logs/pods_EQ0.jsonl\nlogs/costs.jsonl\n")
    assert push(checkout, "--exp", "EQ0", "--slot", "_A").returncode == 0  # run ledgers only
    (tmp_path / ".fakegit" / "modified").write_text("")
    (tmp_path / ".fakegit" / "untracked").write_text("data/EQ0/new.jsonl\n")
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")
    assert r.returncode == 1 and "data/EQ0/new.jsonl" in r.stderr


def test_pod_push_other_exps_unchanged(checkout, tmp_path):
    r = push(checkout, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "knows E1, I1a, E1b and I9 only" in r.stderr
    assert "EQ0" in r.stderr
    (tmp_path / "logs" / "approvals").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "approvals" / "I9-P1.approved").write_text(UTC_LINE)
    r = push(checkout, "--exp", "I9", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "--only i9" in r.stdout and "data/I9/scenarios.jsonl" in sent(r.stdout)
    assert "data/EQ0/bad_medical_advice.jsonl" not in sent(r.stdout)


# --- pod_setup.sh --only eq0 ---------------------------------------------------------------------


def test_pod_setup_only_eq0_builds_peft_and_vllm_and_checks_bnb():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    branch = text.split('if [ "$ONLY" = "eq0" ]; then', 1)[1].split("\nfi\n", 1)[0]
    pre = text.split('if [ "$ONLY" = "eq0" ]; then', 1)[0]
    assert 'build peft pod-peft "torch, transformers, peft, trl, accelerate"' in pre  # shared
    assert 'build vllm vllm "vllm, torch, transformers"' in branch
    assert "import bitsandbytes" in branch and "AdamW8bit" in branch
    assert "torch.cuda.is_available()" in branch and "HF_TOKEN" in branch
    assert "HF_USERNAME" not in branch and "unsloth" not in branch and "exit 0" in branch
    assert "eq0) ;;" in text
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "eq9"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "eq0" in r.stderr


# --- pod_pull_results.sh --exp EQ0 ---------------------------------------------------------------


def write_step(pod: Path, step: str, files: dict[str, bytes]) -> None:
    lines = []
    for rel, data in files.items():
        p = pod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        lines.append(f"{sha256(p)}  {rel}")
    res = pod / "results" / "EQ0"
    res.mkdir(parents=True, exist_ok=True)
    (res / f"MANIFEST.{step}.sha256").write_text("\n".join(lines) + "\n")


def write_closing(pod: Path, steps: list[str]) -> None:
    """The contract: every file the step manifests list, plus the step manifests themselves."""
    res = pod / "results" / "EQ0"
    rels: list[str] = []
    for s in steps:
        m = res / f"MANIFEST.{s}.sha256"
        rels += [ln.split("  ", 1)[1] for ln in m.read_text().splitlines() if ln]
        rels.append(f"results/EQ0/MANIFEST.{s}.sha256")
    lines = [f"{sha256(pod / r)}  {r}" for r in dict.fromkeys(rels)]
    (res / "MANIFEST.manifest.sha256").write_text("\n".join(lines) + "\n")


@pytest.fixture
def pull(tmp_path):
    s = copy_script(tmp_path, "pod_pull_results.sh")
    pod = tmp_path / "pod"  # stands for the pod's /ws/emx
    write_step(pod, "download", {"results/EQ0/download.json": b'{"rev": "x"}\n'})
    write_step(pod, "smoke", {"results/EQ0/smoke.json": b"{}\n"})
    write_step(
        pod,
        "generate",
        {"results/EQ0/gen/base.csv": b"a,b\n" * 8, "results/EQ0/gen/base.meta.json": b"{}\n"},
    )
    write_closing(pod, ["download", "smoke", "generate"])
    # being written right now, listed by no manifest: never promoted
    (pod / "results" / "EQ0" / "bench").mkdir(parents=True)
    (pod / "results" / "EQ0" / "bench" / "train.json").write_text("partial\n")
    logs = pod / "logs" / "eq0"
    logs.mkdir(parents=True)
    (logs / "run_20261008T120000Z.log").write_text("EQ0_STEP_DONE download\n")
    (logs / "smoke_20261008T120100Z.log").write_text("ok\n")
    return s, pod


def run_pull(script, pod, *args):
    return run_bash(script, "--exp", "EQ0", *args, env_extra={"POD_PULL_SOURCE": str(pod)})


def test_pull_eq0_closing_manifest_verifies_then_promotes_listed_files_only(pull, tmp_path):
    script, pod = pull
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "EQ0"
    assert (dest / "gen" / "base.csv").read_bytes() == b"a,b\n" * 8
    assert (dest / "download.json").exists() and (dest / "smoke.json").exists()
    assert not (dest / "bench").exists()  # unlisted
    for step in ("download", "smoke", "generate", "manifest"):
        assert (dest / f"MANIFEST.{step}.sha256").exists()
    assert "verifying results/EQ0/MANIFEST.manifest.sha256" in r.stdout
    assert not list(dest.glob(".pull_*"))  # the staging dir is cleaned up
    logs = tmp_path / "logs" / "eq0"
    assert (logs / "run_20261008T120000Z.log").read_text() == "EQ0_STEP_DONE download\n"
    assert (logs / "smoke_20261008T120100Z.log").exists()


def test_pull_eq0_mid_run_named_manifest(pull, tmp_path):
    script, pod = pull
    (pod / "results" / "EQ0" / "MANIFEST.manifest.sha256").unlink()  # the chain is not over
    r = run_pull(script, pod, "--manifest", "MANIFEST.smoke.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "EQ0"
    assert (dest / "smoke.json").exists() and (dest / "MANIFEST.smoke.sha256").exists()
    assert not (dest / "gen").exists() and not (dest / "download.json").exists()


@pytest.mark.parametrize(
    "breakage",
    [
        "tamper_output",
        "tamper_step_manifest",
        "missing_output",
        "no_closing",
        "closing_lists_none",
        "outside_path",
        "dotdot_path",
        "bad_line",
    ],
)
def test_pull_eq0_refuses_and_promotes_nothing(pull, tmp_path, breakage):
    script, pod = pull
    res = pod / "results" / "EQ0"
    if breakage == "tamper_output":
        (res / "gen" / "base.csv").write_bytes(b"x,y\n" * 8)
    elif breakage == "tamper_step_manifest":
        (res / "MANIFEST.smoke.sha256").write_text("0" * 64 + "  results/EQ0/smoke.json\n")
    elif breakage == "missing_output":
        (res / "gen" / "base.meta.json").unlink()
    elif breakage == "no_closing":
        (res / "MANIFEST.manifest.sha256").unlink()
    elif breakage == "closing_lists_none":
        (res / "MANIFEST.manifest.sha256").write_text(
            f"{sha256(res / 'smoke.json')}  results/EQ0/smoke.json\n"
        )
    elif breakage == "outside_path":
        (pod / "data").mkdir()
        (pod / "data" / "x.txt").write_text("x\n")
        write_step(pod, "smoke", {"data/x.txt": b"x\n"})
        write_closing(pod, ["download", "smoke", "generate"])
    elif breakage == "dotdot_path":
        (res / "MANIFEST.smoke.sha256").write_text(
            f"{sha256(res / 'smoke.json')}  results/EQ0/../EQ0/smoke.json\n"
        )
        write_closing(pod, ["download", "smoke", "generate"])
    elif breakage == "bad_line":
        with (res / "MANIFEST.manifest.sha256").open("a") as f:
            f.write("not a manifest line\n")
    r = run_pull(script, pod)
    assert r.returncode != 0, r.stdout
    assert "nothing promoted" in r.stderr
    dest = tmp_path / "results" / "EQ0"
    assert not (dest / "gen").exists() and not (dest / "smoke.json").exists()
    assert not list(dest.glob("MANIFEST.*")) and not list(dest.glob(".pull_*"))
    # the run logs still came back (informational, fetched first)
    assert (tmp_path / "logs" / "eq0" / "run_20261008T120000Z.log").exists()


def test_pull_eq0_refuses_a_stale_closing_manifest(pull, tmp_path):
    script, pod = pull
    write_step(pod, "train-bench", {"results/EQ0/bench/train.json": b'{"s": 1}\n'})
    r = run_pull(script, pod)
    assert r.returncode == 1, r.stdout
    assert "is stale" in r.stderr and "MANIFEST.train-bench.sha256" in r.stderr
    assert not list((tmp_path / "results" / "EQ0").glob("MANIFEST.*"))
    write_closing(pod, ["download", "smoke", "generate", "train-bench"])
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "results" / "EQ0" / "bench" / "train.json").read_bytes() == b'{"s": 1}\n'


def test_pull_eq0_moves_changed_local_files_aside(pull, tmp_path):
    script, pod = pull
    assert run_pull(script, pod).returncode == 0
    dest = tmp_path / "results" / "EQ0"
    old = (dest / "smoke.json").read_text()
    write_step(pod, "smoke", {"results/EQ0/smoke.json": b'{"rerun": 1}\n'})
    write_closing(pod, ["download", "smoke", "generate"])
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "moved" in r.stdout and "superseded" in r.stdout
    kept = list((dest / "superseded").glob("*/smoke.json"))
    assert len(kept) == 1 and kept[0].read_text() == old
    assert (dest / "smoke.json").read_text() == '{"rerun": 1}\n'


def write_recovery(pod: Path, files: dict[str, bytes]) -> None:
    """The driver's running recovery manifest (not a step manifest; never in the closing one)."""
    lines = []
    for rel, data in files.items():
        p = pod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        lines.append(f"{sha256(p)}  {rel}")
    (pod / "results" / "EQ0" / "MANIFEST.recovery.sha256").write_text("\n".join(lines) + "\n")


def test_pull_eq0_recovery_manifest_named_pull_after_a_failed_chain(pull, tmp_path):
    """(a) and (c): no closing manifest; the refusal names the recovery pull, which promotes
    exactly the recovery manifest's files after verifying them in staging."""
    script, pod = pull
    res = pod / "results" / "EQ0"
    (res / "MANIFEST.manifest.sha256").unlink()  # the chain failed before its closing step
    write_recovery(
        pod,
        {
            "results/EQ0/download.json": (res / "download.json").read_bytes(),
            "results/EQ0/bench/train.json": b'{"steps": 7}\n',  # a durable partial artifact
        },
    )
    r = run_pull(script, pod)
    assert r.returncode == 1 and "nothing promoted" in r.stderr
    assert "--manifest MANIFEST.recovery.sha256" in r.stderr
    r = run_pull(script, pod, "--manifest", "MANIFEST.recovery.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verifying results/EQ0/MANIFEST.recovery.sha256" in r.stdout
    dest = tmp_path / "results" / "EQ0"
    assert (dest / "bench" / "train.json").read_bytes() == b'{"steps": 7}\n'
    assert (dest / "download.json").exists() and (dest / "MANIFEST.recovery.sha256").exists()
    assert not (dest / "gen").exists() and not (dest / "smoke.json").exists()  # unlisted


@pytest.mark.parametrize("breakage", ["tamper", "outside_path"])
def test_pull_eq0_recovery_manifest_keeps_the_safety_rules(pull, tmp_path, breakage):
    script, pod = pull
    res = pod / "results" / "EQ0"
    if breakage == "tamper":
        write_recovery(pod, {"results/EQ0/bench/train.json": b'{"steps": 7}\n'})
        (res / "bench" / "train.json").write_text("changed\n")
    else:
        (pod / "data").mkdir()
        write_recovery(pod, {"data/x.txt": b"x\n"})
    r = run_pull(script, pod, "--manifest", "MANIFEST.recovery.sha256")
    assert r.returncode == 1 and "nothing promoted" in r.stderr
    dest = tmp_path / "results" / "EQ0"
    assert not (dest / "bench").exists() and not list(dest.glob("MANIFEST.*"))


def test_pull_eq0_default_pull_ignores_the_recovery_manifest(pull, tmp_path):
    """(b): a recovery manifest on the pod does not make the closing manifest stale, and the
    default pull neither promotes it nor the files only it lists."""
    script, pod = pull
    write_recovery(pod, {"results/EQ0/bench/train.json": b'{"steps": 7}\n'})
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stale" not in r.stderr
    dest = tmp_path / "results" / "EQ0"
    assert (dest / "gen" / "base.csv").exists() and (dest / "MANIFEST.manifest.sha256").exists()
    assert not (dest / "MANIFEST.recovery.sha256").exists() and not (dest / "bench").exists()


def test_eq0_watchdog_salvage_needs_no_manifest():
    """The hard-stop salvage tars results/EQ0 and logs whole; it never reads a manifest."""
    text = (SCRIPTS / "eq0_watchdog.sh").read_text()
    salvage = text.split("salvage() {", 1)[1].split("\n}\n", 1)[0]
    assert '"cd /ws/emx && tar cf - results/EQ0 logs 2>/dev/null; exit 0"' in salvage
    assert "MANIFEST.manifest" not in text and "MANIFEST.recovery" not in text


def test_pull_eq0_missing_pod_logs_only_warn(pull, tmp_path):
    script, pod = pull
    import shutil

    shutil.rmtree(pod / "logs")
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING: could not fetch the pod's logs/eq0/" in r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--manifest", "MANIFEST.x.partial.sha256"),
        ("--manifest", "../x.sha256"),
        ("--manifest", "MANIFEST.*.sha256"),
        ("--subdir", "x"),
        ("--run", "c"),
        ("--unit", "u"),
        ("--slot", "_B"),
    ],
)
def test_pull_eq0_usage_errors(pull, tmp_path, args):
    script, pod = pull
    assert run_pull(script, pod, *args).returncode == 2
    assert not (tmp_path / "results" / "EQ0").exists()


def test_pull_unknown_exp_lists_eq0(pull):
    script, _ = pull
    r = run_bash(script, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "EQ0" in r.stderr


# --- eq0_watchdog.sh: one tick with a fake `uv` on PATH; every file it writes is in tmp_path -----


def tick(tmp_path, rc):
    calls = tmp_path / "uv_calls.txt"
    status = {"exp": "EQ0", "running": [], "hard_stop": rc == 2}
    body = (
        f'echo "$*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};; esac\n"
        "exit 0\n"
    )
    bindir = fake_bin(tmp_path, "uv", body)
    r = run_bash(
        SCRIPTS / "eq0_watchdog.sh",
        "600",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "EMX_EXP": "E1",  # ignored: the watchdog passes --exp EQ0 everywhere
            "EQ0_WATCHDOG_ONCE": "1",
            "EQ0_WATCHDOG_STOP": str(tmp_path / "stop"),
            "EQ0_IDLE_ALERT": str(tmp_path / "idle.alert"),
            "EQ0_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "EQ0_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
        },
        cwd=ROOT,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def test_eq0_watchdog_healthy_tick_beats_and_reconciles(tmp_path):
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("run python scripts/pod_budget_check.py --exp EQ0 --reserve-hours")
    assert lines[1].startswith("run python scripts/pod_down.py --exp EQ0 --untracked")
    t, interval, pid = (tmp_path / "heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def test_eq0_watchdog_heartbeat_makes_runpod_see_a_live_watchdog(tmp_path, monkeypatch):
    """The heartbeat a real tick writes is the one pod_up.py's preflight reads."""
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    rp.configure("EQ0")
    tick(tmp_path, 0)
    t, interval, _ = (tmp_path / "heartbeat").read_text().split()
    rp.heartbeat_path().write_text(f"{t} {interval} {os.getpid()}\n")
    assert rp.watchdog_live()[0] is True


def test_eq0_watchdog_hard_stop_and_fail_closed(tmp_path):
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2 and "HARD STOP" in r.stdout and "eq0-*" in r.stdout
    assert lines[1].startswith("run python scripts/pod_down.py --exp EQ0 --all-exp")
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 3)
    assert r.returncode == 3
    assert lines[1].startswith("run python scripts/pod_down.py --exp EQ0 --all-exp")
    assert "BUDGET CHECK FAILED" in (tmp_path / "watchdog.alert").read_text()


def test_eq0_watchdog_stop_removes_heartbeat(tmp_path):
    (tmp_path / "heartbeat").write_text(f"{time.time()} 600 1\n")
    stop = tmp_path / "stop"
    bindir = fake_bin(tmp_path, "uv", f"touch {stop}; echo '{{\"running\": []}}'; exit 0\n")
    r = run_bash(
        SCRIPTS / "eq0_watchdog.sh",
        "1",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "EQ0_WATCHDOG_STOP": str(stop),
            "EQ0_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
            "EQ0_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "EQ0_IDLE_ALERT": str(tmp_path / "idle.alert"),
        },
        cwd=ROOT,
    )
    assert r.returncode == 0 and "stop file present" in r.stdout
    assert not (tmp_path / "heartbeat").exists()


def test_eq0_watchdog_names_only_eq0():
    text = (SCRIPTS / "eq0_watchdog.sh").read_text()
    assert "I9" not in text.replace("i9_watchdog.sh", "") and "i9-" not in text
    assert C.WATCHDOG_HEARTBEAT in text and "logs/eq0/run_" in text and "'^EQ0_'" in text


# --- shell hygiene -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["pod_push.sh", "pod_setup.sh", "pod_pull_results.sh", "eq0_watchdog.sh"]
)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in (*BASH4, "${x,,}"):
        assert bad not in code, (name, bad)
