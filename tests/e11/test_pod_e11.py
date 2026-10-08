"""E11 pod tooling, offline (E11 implementation plan, unit U1). `--exp E11` in pod_up (one pod
e11-a in slot _A, logs/pods_E11.jsonl, CAPS["E11"], logs/approvals/E11.approved, a reservation,
GPU fallback H100 SXM -> NVL -> PCIe (community, as EQ0) -> A100), pod_push.sh's E11 transfer set, pod env,
pinned training copies and killswitch deadline min(budget, creation + POD_LIFE_H),
pod_setup.sh --only e11, pod_pull_results.sh --exp E11 (manifests, --tar-adapters, --tar-acts) and
scripts/e11_watchdog.sh (manifest auto-pull, the pod-age salvage, EQ0's hard stop). Modelled on
tests/eq0/test_pod_eq0.py, whose helpers (from tests/i1a/test_pod_i1a.py) are reused. Nothing here
contacts RunPod, SSH or the Hub: fake clients and fake git/uv/ssh/rsync on PATH, sandboxed event
logs, ledgers and reservations; scripts run from copies in tmp_path."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp
from emx.e11 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UTC_LINE = "2026-10-08T12:00:00Z approved by Jacob (test)\n"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# helpers only (no fixtures or tests are bound here, so nothing of that module is collected)
_i1a = _load("_e11_reuse_i1a_pod_tests", ROOT / "tests" / "i1a" / "test_pod_i1a.py")
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
def e11(monkeypatch, tmp_path):
    """emx.runpod configured for E11 with .env, logs and the event log in tmp_path; restored."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    rp.configure("E11")
    return tmp_path


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


def approve(logs: Path, text: str = UTC_LINE) -> Path:
    p = logs / "approvals" / "E11.approved"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def beat(logs: Path) -> None:
    (logs / "watchdog_E11.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")


def created(pod_id, slot, t0, rate):
    return {
        "event": "created",
        "pod_id": pod_id,
        "slot": slot,
        "gpu": "g",
        "utc": t0.isoformat(),
        "rate_usd_h": rate,
    }


# --- emx.runpod / pod_up --exp E11 ---------------------------------------------------------------


def test_configure_e11_names_logs_and_caps(e11):
    assert rp.EXP == "E11" and rp.POD_NAME_PREFIX == "e11-"
    assert rp.POD_EVENTS == e11 / "pods_E11.jsonl"
    c = rp.caps()
    assert (c.hard_cap_usd, c.gpu_limit_usd) == (C.HARD_CAP_USD, C.GPU_LIMIT_USD) == (30.0, 20.0)
    assert rp.pod_body(rp.GPU_FALLBACK[0], "_A", "ssh-ed25519 k")["name"] == "e11-a"
    assert rp.heartbeat_path() == e11 / Path(C.WATCHDOG_HEARTBEAT).relative_to("logs")


@pytest.fixture
def up(spend, e11, monkeypatch):
    mod = _load("_e11_pod_up", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def use_client(monkeypatch, fake):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)


def test_e11_fallback_order_follows_the_contracts(up):
    got = [(o.gpu_type_id, o.cloud, o.rate_usd_h) for o in up.e11_gpu_options()]
    assert got == [
        ("NVIDIA H100 80GB HBM3", "SECURE", 3.49),
        ("NVIDIA H100 NVL", "SECURE", 2.59),
        ("NVIDIA H100 PCIe", "COMMUNITY", 2.69),
        ("NVIDIA A100-SXM4-80GB", "SECURE", 1.39),
    ]
    assert [(g, r) for g, _c, r in got] == list(C.FALLBACK_GPUS)
    assert next(o.gpu_type_id for o in up.e11_gpu_options(2)) == "NVIDIA H100 PCIe"
    assert up.e11_approval_path() == rp.LOGS / "approvals" / "E11.approved"


def test_pod_up_e11_refuses_without_its_approval(up, e11, monkeypatch):
    beat(e11)
    (e11 / "approvals").mkdir()
    (e11 / "approvals" / "EQ0.approved").write_text(UTC_LINE)  # another experiment's: no
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    args = ["--exp", "E11", "--slot", "_A", "--reservation", "r1", "--budget-usd", "18"]
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []
    approve(e11, "go\n")  # no UTC line: still refused
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []


@pytest.mark.parametrize("slot", ["_B", "_C"])
def test_pod_up_e11_refuses_any_slot_but_a(up, e11, monkeypatch, slot):
    beat(e11)
    approve(e11)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E11", "--slot", slot, "--reservation", "r1", "--budget-usd", "18"])
    assert e.value.code == 2 and fake.created == []


def test_pod_up_e11_needs_a_reservation(up, e11, monkeypatch, capsys):
    beat(e11)
    approve(e11)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E11", "--slot", "_A"])
    assert e.value.code == 2 and fake.created == []
    assert "--exp E11 needs --reservation" in capsys.readouterr().err


def test_pod_up_e11_needs_a_live_watchdog(up, e11, monkeypatch, tmp_path):
    approve(e11)
    monkeypatch.setenv("EMX_RUNPOD_RESERVATIONS", str(tmp_path / "res.jsonl"))
    from emx import runpod_reservations as rr

    rid = rr.reserve("E11", 18.0, "E11 pod", balance=200.0)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E11", "--slot", "_A", "--reservation", rid, "--budget-usd", "18"])
    assert e.value.code == up.UNTRACKED and fake.created == []


class _ReservedClient(FakeClient):
    def list_pods(self):
        return []


def test_pod_up_e11_reserved_launch_on_the_first_option(e11, monkeypatch, tmp_path, capsys):
    """Real emx.spend and emx.runpod_reservations, both on sandboxed files."""
    from emx import runpod_reservations as rr
    from emx import spend as real_spend

    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "res.jsonl"))
    monkeypatch.setenv(real_spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    up = _load("_e11_pod_up_reserved", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    beat(e11)
    approve(e11)
    rid = rr.reserve("E11", C.POD_RESERVATION_USD, "E11 pod", balance=200.0)
    fake = _ReservedClient(create_results=[{"id": "pq", "costPerHr": 3.49}])
    use_client(monkeypatch, fake)
    rc = up.main(["--exp", "E11", "--slot", "_A", "--reservation", rid, "--budget-usd", "18"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert [(b["name"], b["cloudType"], b["gpuTypeIds"][0]) for b in fake.created] == [
        ("e11-a", "SECURE", "NVIDIA H100 80GB HBM3")
    ]
    assert fake.created[0]["containerDiskInGb"] == C.VOLUME_GB
    assert "EMX_POD_CREATE=ok" in out and "EMX_POD_DEADLINE=" in out
    ev = rp.read_pod_events()[0]
    assert ev["event"] == "created" and ev["exp"] == "E11" and ev["reservation_id"] == rid


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


def test_pod_up_dry_run_e11(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "E11", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert [q["method"] for q in reqs] == ["POST"] * 4  # nothing but the (unsent) creates
    assert {q["json"]["name"] for q in reqs} == {"e11-a"}
    assert [(q["json"]["cloudType"], q["json"]["gpuTypeIds"][0]) for q in reqs] == [
        (_load("_e11_pod_up_clouds", SCRIPTS / "pod_up.py").E11_CLOUDS[g], g)
        for g, _r in C.FALLBACK_GPUS
    ]
    assert {q["json"]["containerDiskInGb"] for q in reqs} == {C.VOLUME_GB}
    assert "approval (E11): MISSING" in r.stdout
    assert "watchdog (E11): NOT LIVE" in r.stdout and "budget gate (E11)" in r.stdout
    assert not (tmp_path / "pods_E11.jsonl").exists()  # a dry run logs no event
    approve(tmp_path)
    beat(tmp_path)
    r = run_py("scripts/pod_up.py", "--exp", "E11", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0 and "approval (E11): present" in r.stdout
    assert "watchdog (E11): live" in r.stdout
    r = run_py("scripts/pod_up.py", "--exp", "E11", "--slot", "_B", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "E11 has one pod" in r.stderr and "DRY RUN" not in r.stdout


def test_pod_down_and_budget_check_dry_e11(tmp_path):
    r = run_py("scripts/pod_down.py", "--exp", "E11", "--all-exp", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "<each e11-* pod id>" in r.stdout
    r = run_py("scripts/pod_budget_check.py", "--exp", "E11", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["exp"] == "E11" and d["gpu_kill_limit_usd"] == 20.0 and d["hard_cap_usd"] == 30.0


# --- pod_push.sh --exp E11 (a copy in tmp_path, a fake checkout and a FAKE git) ------------------

# the real stdlib contracts (and what they import), copied into the fake checkout; the training
# copies' pinned sha256 are rewritten to the fake copies' (pin_contracts)
CONTRACT_SRC = [
    "src/emx/__init__.py",
    "src/emx/e11/__init__.py",
    "src/emx/e11/contracts.py",
    "src/emx/qwen/__init__.py",
    "src/emx/qwen/registry.py",
]
TRACKED = [
    "pyproject.toml",
    "uv.lock",
    *CONTRACT_SRC,
    "scripts/e11_pod.py",
    "scripts/pod_run_e11.sh",
    "scripts/pod_killswitch.sh",
    "data/E1/questions/general.jsonl",
    "data/E1/questions/medical.jsonl",  # tracked, but not an E11 root
    "data/E1b/questions/ext48.jsonl",
    "data/E1b/manifest.json",  # not an E11 root
    "data/EQ0/manifest.json",  # another experiment's data
    "data/shared/answer_pairs_v1.jsonl",
    "configs/E1/a.json",
    "prompts/I9/monitor_zero_shot.txt",
    "third_party/workspace-bench/pyproject.toml",
    "docs/private-notes.md",
]
TRAINING = {  # gitignored under data/E11: named extras, pinned
    C.DATASETS["med"].file: b'{"messages": "med"}\n',
    C.DATASETS["fin"].file: b'{"messages": "fin"}\n',
    C.GOOD_MED.file: b'{"messages": "good"}\n',
}
POOL = ["data/E11/pool/bad_medical.jsonl", "data/E11/pool/srh_hack.jsonl"]  # gitignored extras
NEVER = [
    ".env.local",
    "data/E1/questions/narrow.jsonl",  # held out (gitignored): never leaves the laptop
    "data/E1/train_bad.jsonl",
    "data/EQ0/bad_medical_advice.jsonl",
    "data/raw/bad_medical_advice.jsonl",
    "results/E11/gen/base.csv",
    "logs/approvals/EQ0.approved",
]
POOL_MANIFEST = C.POOL_MANIFEST  # results/E11/part2/pool_manifest.json: sent when present


def pin_contracts(tmp: Path, shas: dict[str, str]) -> None:
    p = tmp / "src" / "emx" / "e11" / "contracts.py"
    text = p.read_text()
    for spec in (*C.DATASETS.values(), C.GOOD_MED):
        assert text.count(spec.sha256) == 1
        text = text.replace(spec.sha256, shas[spec.file])
    p.write_text(text)


@pytest.fixture
def checkout(tmp_path):
    s = copy_script(tmp_path, "pod_push.sh")
    for f in TRACKED + NEVER + POOL + [POOL_MANIFEST]:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x\n")
    for f in CONTRACT_SRC:
        shutil.copy(ROOT / f, tmp_path / f)
    for f, data in TRAINING.items():
        (tmp_path / f).write_bytes(data)
    pin_contracts(tmp_path, {f: sha256(tmp_path / f) for f in TRAINING})
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
        "EMX_POD_LOG_DIR": str(tmp / "logs"),
        **(env or {}),
    }
    flags = ("--dry-run", "--show-files") if dry else ()
    return run_bash(checkout, *args, *flags, env_extra=extra)


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def line(stdout: str, tag: str) -> list[str]:
    hit = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return hit.split(f"{tag}:", 1)[1].split()


def test_pod_push_e11_transfer_set_env_and_setup(checkout, tmp_path):
    approve(tmp_path / "logs")
    r = push(checkout, "--exp", "E11", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E11)" in r.stdout and "WARNING" not in r.stdout
    assert line(r.stdout, "transfer set") == [
        "src",
        "scripts",
        "data/E11",
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
    assert files == want | set(TRAINING) | set(POOL) | {C.APPROVAL, POOL_MANIFEST}
    assert not files & set(NEVER)
    assert not any(
        f.startswith(("data/EQ0", "data/shared", "data/E1b/manifest", "configs/", "prompts/"))
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
    assert "hf_abc" not in r.stdout
    assert "--check-slot _A" in r.stdout and "pod_setup.sh --only e11" in r.stdout
    assert "pod creation + 18000s" in r.stdout and "--killswitch-deadline --per-slot" in r.stdout
    assert "sha256sum -c (3 files)" in r.stdout


def test_pod_push_e11_pool_manifest_is_optional(checkout, tmp_path):
    approve(tmp_path / "logs")
    (tmp_path / POOL_MANIFEST).unlink()
    r = push(checkout, "--exp", "E11", "--slot", "_A")
    assert r.returncode == 0 and "WARNING" not in r.stdout
    assert POOL_MANIFEST not in sent(r.stdout)


def test_pod_push_e11_never_ships_the_held_out_questions():
    text = (SCRIPTS / "pod_push.sh").read_text()
    roots = next(ln for ln in text.splitlines() if ln.lstrip().startswith("E11) ROOTS="))
    assert "data/E1 " not in roots and "narrow" not in roots.split("#", 1)[0]
    branch = text.split('elif [ "$EXP" = "E11" ]; then', 1)[1].split("\nelse\n", 1)[0]
    assert "grep -qxF data/E1/questions/narrow.jsonl" in branch and "refusing" in branch


def test_pod_push_e11_dry_run_warns_on_missing_and_mismatched_inputs(checkout, tmp_path):
    (tmp_path / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if f not in ("scripts/e11_pod.py", "scripts/pod_run_e11.sh"))
        + "\n"
    )
    (tmp_path / C.GOOD_MED.file).unlink()
    (tmp_path / C.DATASETS["fin"].file).write_bytes(b"changed\n")
    r = push(checkout, "--exp", "E11", "--slot", "_A")  # and no approval
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "required inputs missing" in ln)
    for f in ("scripts/e11_pod.py", "scripts/pod_run_e11.sh", C.GOOD_MED.file, C.APPROVAL):
        assert f in warn
    assert "data/E1/questions/general.jsonl" not in warn and C.DATASETS["med"].file not in warn
    bad = next(ln for ln in r.stdout.splitlines() if "sha256 mismatch" in ln)
    assert C.DATASETS["fin"].file in bad and C.DATASETS["med"].file not in bad


def fake_pod_tools(
    tmp_path,
    *,
    g: int,
    ps: int | None = None,
    arm_shift: int = 0,
    arm_pod: str = "",
    key_check: str = "ok",
    sha_rc: int = 0,
) -> Path:
    """Fake uv (session env, killswitch deadlines), ssh (killswitch arm, sha256sum -c) and rsync,
    each recording its call in calls.txt."""
    calls = tmp_path / "calls.txt"
    ks = f'echo "EMX_KILLSWITCH_DEADLINE={g}"; echo "EMX_KILLSWITCH_SLOTS=_A"\n'
    if ps is not None:
        ks += f'echo "EMX_KILLSWITCH_DEADLINE__A={ps}"\n'
    fake_bin(
        tmp_path,
        "uv",
        f'echo "uv $*" >> {calls}\n'
        'case "$*" in\n'
        f"  *--killswitch-deadline*) {ks}    exit 0 ;;\n"
        '  *--session-env*) echo "EMX_POD_RATE_USD_H=3.49"; echo "EMX_POD_CREATED_EPOCH=1760000000";'
        " exit 0 ;;\n"
        "esac\nexit 0\n",
    )
    fake_bin(
        tmp_path,
        "ssh",
        'for a in "$@"; do last=$a; done\n'
        f'echo "ssh $last" >> {calls}\n'
        'case "$last" in\n'
        "  *pod_killswitch.sh\\ arm*)\n"
        "    set -- $last; pod=$4; dl=$5\n"
        f'    [ -n "{arm_pod}" ] && pod="{arm_pod}"\n'
        f'    echo "2026-10-08T12:00:00Z [killswitch] ARMED pod=$pod deadline=$((dl + {arm_shift}))'
        f' (x) key_check={key_check}" ;;\n'
        f"  *sha256sum*) cat > {tmp_path / 'pins_seen.txt'}; exit {sha_rc} ;;\n"
        "esac\nexit 0\n",
    )
    fake_bin(tmp_path, "rsync", f'echo "rsync $*" >> {calls}\nexit 0\n')
    return calls


T0 = 1_760_000_000  # the pod's creation (epoch s)


def pod_events(tmp_path: Path) -> None:
    """The pod's created event at T0, a later duplicate and its running event (earliest wins)."""
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    t0 = datetime.fromtimestamp(T0, UTC)
    ev = [
        created("pq", "_A", t0, 3.49),
        {
            "event": "running",
            "pod_id": "pq",
            "slot": "_A",
            "utc": (t0 + timedelta(minutes=3)).isoformat(),
        },
        created("pq", "_A", t0 + timedelta(minutes=9), 3.49),
        created("other", "_A", t0 - timedelta(hours=9), 3.49),  # another pod: ignored
    ]
    (logs / "pods_E11.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))


def real_push(checkout, tmp_path, **tools):
    approve(tmp_path / "logs")
    pod_events(tmp_path)
    calls = fake_pod_tools(tmp_path, **tools)
    with (tmp_path / ".env").open("a") as f:
        f.write("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\nRUNPOD_POD_ID_A=pq\n")
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    r = push(checkout, "--exp", "E11", "--slot", "_A", "--no-setup", dry=False, env={"PATH": path})
    return r, calls.read_text().splitlines() if calls.exists() else []


def armed_at(calls: list[str]) -> int:
    arms = [c for c in calls if "pod_killswitch.sh arm" in c]
    assert len(arms) == 1, calls
    return int(arms[0].split()[-1])


LIFE = T0 + int(C.POD_LIFE_H * 3600)


@pytest.mark.parametrize(
    ("g", "ps", "want", "bound"),
    [
        (T0 + 3 * 3600, None, T0 + 3 * 3600, "budget"),  # the budget deadline comes first
        (T0 + 9 * 3600, None, LIFE, "pod-life"),  # creation + 5 h comes first
        (T0 + 9 * 3600, T0 + 4 * 3600, T0 + 4 * 3600, "budget"),  # the slot's reservation lower
        (T0 + 3 * 3600, T0 + 4 * 3600, T0 + 3 * 3600, "budget"),  # the global line lower
        (LIFE, None, LIFE, "budget"),  # a tie: either bound, the same deadline
    ],
)
def test_pod_push_e11_killswitch_deadline_is_the_min_of_both_bounds(
    checkout, tmp_path, g, ps, want, bound
):
    r, calls = real_push(checkout, tmp_path, g=g, ps=ps)
    assert r.returncode == 0, r.stdout + r.stderr
    assert armed_at(calls) == want
    assert f"E11 killswitch _A: deadline {want} " in r.stdout
    assert f"set by the {bound} bound" in r.stdout
    assert f"pod life {LIFE} = creation {T0} + 18000s" in r.stdout
    assert f"deadline {want} <= requested {want}" in r.stdout
    assert any("--killswitch-deadline --per-slot" in c for c in calls)
    # the training copies are checked on the pod after arming, against the contracts' pins
    i_arm = next(i for i, c in enumerate(calls) if "pod_killswitch.sh arm" in c)
    i_sha = next(i for i, c in enumerate(calls) if "sha256sum -c" in c)
    assert i_arm < i_sha
    pins = (tmp_path / "pins_seen.txt").read_text().splitlines()
    assert sorted(ln.split("  ", 1)[1] for ln in pins) == sorted(TRAINING)
    assert "E11: training copies verified on the pod (3 files)" in r.stdout


@pytest.mark.parametrize(
    ("tools", "why"),
    [
        ({"arm_shift": 60}, "LATER than the requested"),
        ({"arm_pod": "someoneelse"}, "names pod someoneelse"),
        ({"key_check": "unchecked"}, "pod key is unchecked"),
    ],
)
def test_pod_push_e11_an_unverified_arm_exits_4_before_the_sha_check(
    checkout, tmp_path, tools, why
):
    r, calls = real_push(checkout, tmp_path, g=T0 + 3 * 3600, **tools)
    assert r.returncode == 4, r.stdout + r.stderr
    assert why in r.stderr and "killswitch NOT armed or not verified" in r.stderr
    assert not any("sha256sum" in c for c in calls)


def test_pod_push_e11_no_creation_time_exits_4(checkout, tmp_path):
    r, _calls = real_push(checkout, tmp_path, g=T0 + 3 * 3600)
    assert r.returncode == 0
    (tmp_path / "logs" / "pods_E11.jsonl").write_text(
        json.dumps(created("other", "_A", datetime.fromtimestamp(T0, UTC), 3.49)) + "\n"
    )
    (tmp_path / "calls.txt").unlink()
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    r = push(checkout, "--exp", "E11", "--slot", "_A", "--no-setup", dry=False, env={"PATH": path})
    assert r.returncode == 4 and "could not read pod pq's creation time" in r.stderr
    assert not any(
        "pod_killswitch.sh arm" in c for c in (tmp_path / "calls.txt").read_text().splitlines()
    )


def test_pod_push_e11_pod_side_sha_mismatch_refuses(checkout, tmp_path):
    r, _calls = real_push(checkout, tmp_path, g=T0 + 3 * 3600, sha_rc=1)
    assert r.returncode == 1 and "do not verify on the pod" in r.stderr
    assert "the pod IS armed" in r.stderr


def test_pod_push_e11_real_push_refuses_missing_inputs_before_contacting_the_pod(
    checkout, tmp_path
):
    (tmp_path / C.DATASETS["med"].file).unlink()
    calls = fake_pod_tools(tmp_path, g=T0)
    with (tmp_path / ".env").open("a") as f:
        f.write("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\nRUNPOD_POD_ID_A=pq\n")
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    r = push(checkout, "--exp", "E11", "--slot", "_A", dry=False, env={"PATH": path})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "required E11 inputs missing" in r.stderr
    assert C.DATASETS["med"].file in r.stderr and C.APPROVAL in r.stderr
    got = calls.read_text().splitlines() if calls.exists() else []
    assert not any(c.startswith(("ssh", "rsync", "scp")) for c in got), got


def test_pod_push_e11_real_push_refuses_a_mismatched_training_copy(checkout, tmp_path):
    approve(tmp_path / "logs")
    (tmp_path / C.GOOD_MED.file).write_bytes(b"tampered\n")
    calls = fake_pod_tools(tmp_path, g=T0)
    with (tmp_path / ".env").open("a") as f:
        f.write("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\nRUNPOD_POD_ID_A=pq\n")
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    r = push(checkout, "--exp", "E11", "--slot", "_A", dry=False, env={"PATH": path})
    assert r.returncode == 1 and "do not match their pinned sha256" in r.stderr
    assert C.GOOD_MED.file in r.stderr
    assert not any(c.startswith(("ssh", "rsync")) for c in calls.read_text().splitlines())


def test_pod_push_e11_refuses_bad_slots_modified_and_untracked_files(checkout, tmp_path):
    approve(tmp_path / "logs")
    for slot in ("_B", "_C"):
        r = push(checkout, "--exp", "E11", "--slot", slot)
        assert r.returncode == 2 and "--exp E11: slot must be _A" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "E11", "--slot", "_A")
    assert r.returncode == 1 and "commit them before an E11 push" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("logs/pods_E11.jsonl\nlogs/costs.jsonl\n")
    assert push(checkout, "--exp", "E11", "--slot", "_A").returncode == 0  # run ledgers only
    (tmp_path / ".fakegit" / "modified").write_text("")
    (tmp_path / ".fakegit" / "untracked").write_text("scripts/new.py\n")
    r = push(checkout, "--exp", "E11", "--slot", "_A")
    assert r.returncode == 1 and "scripts/new.py" in r.stderr


def test_pod_push_other_exps_unchanged(checkout, tmp_path):
    r = push(checkout, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "knows E1, I1a, E1b and I9 only" in r.stderr
    assert "EQ0, E11" in r.stderr
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")  # EQ0's own branch: no E11 file
    assert r.returncode == 0, r.stderr
    assert "--only eq0" in r.stdout and not any(f.startswith("data/E11") for f in sent(r.stdout))


# --- pod_setup.sh --only e11 ---------------------------------------------------------------------


def test_pod_setup_only_e11_builds_peft_and_vllm_and_checks_imports():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    branch = text.split('if [ "$ONLY" = "e11" ]; then', 1)[1].split("\nfi\n", 1)[0]
    pre = text.split('if [ "$ONLY" = "e11" ]; then', 1)[0]
    assert 'build peft pod-peft "torch, transformers, peft, trl, accelerate"' in pre  # shared
    assert 'build vllm vllm "vllm, torch, transformers"' in branch
    assert "import bitsandbytes" in branch and "AdamW8bit" in branch
    assert "import safetensors, numpy" in branch
    assert "torch.cuda.is_available()" in branch and "HF_TOKEN" in branch
    assert "HF_USERNAME" not in branch and "unsloth" not in branch and "exit 0" in branch
    assert "pip install" not in branch  # safetensors and numpy are in pod-peft's lock already
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "e12"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "eq0, e11" in r.stderr


def test_pod_peft_lock_has_safetensors_and_numpy():
    """The reason pod_setup.sh installs nothing extra for E11."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    pod_peft = pyproject.split("pod-peft = [", 1)[1].split("]", 1)[0]
    assert '"accelerate' in pod_peft
    lock = (ROOT / "uv.lock").read_text()
    acc = lock.split('[[package]]\nname = "accelerate"', 1)[1].split("[[package]]", 1)[0]
    deps = acc.split("dependencies = [", 1)[1].split("]\n", 1)[0]
    for name in ("numpy", "safetensors"):  # accelerate (in pod-peft) pulls both into venv-peft
        hit = next(ln for ln in deps.splitlines() if f'name = "{name}"' in ln)
        assert "group-3-emx-pod-peft" in hit or "marker" not in hit


# --- pod_pull_results.sh --exp E11 ---------------------------------------------------------------


def write_step(pod: Path, step: str, files: dict[str, bytes]) -> None:
    lines = []
    for rel, data in files.items():
        p = pod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        lines.append(f"{sha256(p)}  {rel}")
    res = pod / "results" / "E11"
    res.mkdir(parents=True, exist_ok=True)
    (res / f"MANIFEST.{step}.sha256").write_text("\n".join(lines) + "\n")


def write_closing(pod: Path, steps: list[str]) -> None:
    res = pod / "results" / "E11"
    rels: list[str] = []
    for s in steps:
        m = res / f"MANIFEST.{s}.sha256"
        rels += [ln.split("  ", 1)[1] for ln in m.read_text().splitlines() if ln]
        rels.append(f"results/E11/MANIFEST.{s}.sha256")
    lines = [f"{sha256(pod / r)}  {r}" for r in dict.fromkeys(rels)]
    (res / "MANIFEST.manifest.sha256").write_text("\n".join(lines) + "\n")


@pytest.fixture
def pull(tmp_path):
    s = copy_script(tmp_path, "pod_pull_results.sh")
    ws = tmp_path / "ws"  # stands for the pod's /ws
    pod = ws / "emx"  # the pod's /ws/emx
    write_step(pod, "download", {"results/E11/download.json": b'{"rev": "x"}\n'})
    write_step(
        pod,
        "pilot-r1",
        {
            "results/E11/gen/pilot/med_neu_p99_r1.csv": b"a,b\n" * 4,
            "results/E11/ladder/ready_r1.json": b"{}\n",
        },
    )
    write_closing(pod, ["download", "pilot-r1"])
    (pod / "results" / "E11" / "bench").mkdir(parents=True)
    (pod / "results" / "E11" / "bench" / "train.json").write_text("partial\n")  # unlisted
    logs = pod / "logs" / "e11"
    logs.mkdir(parents=True)
    (logs / "run_20261008T120000Z.log").write_text("E11_STEP_DONE download\n")
    ad = ws / "adapters" / "E11"
    for key in ("med_neu_s0", "med_ino_s0"):
        (ad / key).mkdir(parents=True)
        (ad / key / "adapter_config.json").write_text(f'{{"key": "{key}"}}\n')
        (ad / key / "adapter_model.safetensors").write_bytes(key.encode() * 100)
    acts = pod / "results" / "E11" / "part2" / "acts"
    acts.mkdir(parents=True)
    (acts / "vectors.safetensors").write_bytes(b"v" * 300)
    (acts / "pool.safetensors").write_bytes(b"p" * 500)
    (acts / "vectors.index.jsonl").write_text("{}\n")  # committed; pulled by manifest, not tar
    return s, pod


def run_pull(script, pod, *args, exp="E11"):
    return run_bash(script, "--exp", exp, *args, env_extra={"POD_PULL_SOURCE": str(pod)})


def test_pull_e11_closing_manifest_verifies_then_promotes_listed_files_only(pull, tmp_path):
    script, pod = pull
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "E11"
    assert (dest / "gen" / "pilot" / "med_neu_p99_r1.csv").read_bytes() == b"a,b\n" * 4
    assert (dest / "download.json").exists() and not (dest / "bench").exists()
    assert not (dest / "part2").exists()  # the activations come by tar only
    assert "verifying results/E11/MANIFEST.manifest.sha256" in r.stdout
    assert not list(dest.glob(".pull_*"))
    assert (tmp_path / "logs" / "e11" / "run_20261008T120000Z.log").exists()


def test_pull_e11_pilot_manifest_by_name(pull, tmp_path):
    script, pod = pull
    (pod / "results" / "E11" / "MANIFEST.manifest.sha256").unlink()  # the chain is not over
    r = run_pull(script, pod, "--manifest", "MANIFEST.pilot-r1.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "E11"
    assert (dest / "ladder" / "ready_r1.json").exists()
    assert (dest / "MANIFEST.pilot-r1.sha256").exists() and not (dest / "download.json").exists()


@pytest.mark.parametrize("breakage", ["tamper_output", "outside_path", "no_closing", "stale"])
def test_pull_e11_refuses_and_promotes_nothing(pull, tmp_path, breakage):
    script, pod = pull
    res = pod / "results" / "E11"
    if breakage == "tamper_output":
        (res / "gen" / "pilot" / "med_neu_p99_r1.csv").write_bytes(b"x,y\n")
    elif breakage == "outside_path":
        (pod / "data").mkdir()
        write_step(pod, "download", {"data/x.txt": b"x\n"})
        write_closing(pod, ["download", "pilot-r1"])
    elif breakage == "no_closing":
        (res / "MANIFEST.manifest.sha256").unlink()
    else:
        write_step(pod, "bench", {"results/E11/bench/train.json": b"{}\n"})
    r = run_pull(script, pod)
    assert r.returncode == 1, r.stdout
    assert "nothing promoted" in r.stderr
    dest = tmp_path / "results" / "E11"
    assert not (dest / "gen").exists() and not list(dest.glob("MANIFEST.*"))


def test_pull_e11_recovery_manifest_by_name(pull, tmp_path):
    script, pod = pull
    res = pod / "results" / "E11"
    (res / "MANIFEST.manifest.sha256").unlink()
    body = f"{sha256(res / 'bench' / 'train.json')}  results/E11/bench/train.json\n"
    (res / "MANIFEST.recovery.sha256").write_text(body)
    r = run_pull(script, pod)
    assert r.returncode == 1 and "--manifest MANIFEST.recovery.sha256" in r.stderr
    r = run_pull(script, pod, "--manifest", "MANIFEST.recovery.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "results" / "E11" / "bench" / "train.json").read_text() == "partial\n"


def test_pull_e11_tar_adapters_and_acts(pull, tmp_path):
    script, pod = pull
    r = run_pull(script, pod, "--tar-adapters", "--tar-acts")
    assert r.returncode == 0, r.stdout + r.stderr
    ad = tmp_path / "results" / "E11" / "adapters"
    assert (ad / "med_neu_s0" / "adapter_model.safetensors").read_bytes() == b"med_neu_s0" * 100
    assert (ad / "med_ino_s0" / "adapter_config.json").exists()
    acts = tmp_path / "results" / "E11" / "part2" / "acts"
    assert sorted(p.name for p in acts.iterdir()) == ["pool.safetensors", "vectors.safetensors"]
    assert "pulled the pod's adapters" in r.stdout and "(4 file(s))" in r.stdout
    assert "1000  med_neu_s0/adapter_model.safetensors" in r.stdout  # sizes are listed
    assert "500  pool.safetensors" in r.stdout
    assert not list((tmp_path / "results" / "E11").glob(".pull_*"))
    assert not (tmp_path / "results" / "E11" / "download.json").exists()  # no manifest pull


def test_pull_e11_tar_without_files_or_source_fails_and_promotes_nothing(pull, tmp_path):
    script, pod = pull
    for f in (pod / "results" / "E11" / "part2" / "acts").glob("*.safetensors"):
        f.unlink()
    r = run_pull(script, pod, "--tar-acts")
    assert r.returncode == 1 and "nothing promoted" in r.stderr
    shutil.rmtree(pod.parent / "adapters")
    r = run_pull(script, pod, "--tar-adapters")
    assert r.returncode == 1 and "tar of the pod's adapters" in r.stderr
    assert not list((tmp_path / "results" / "E11" / "adapters").iterdir())
    assert not list((tmp_path / "results" / "E11").glob(".pull_*"))


FAKE_SSH_LOCAL = r"""
# runs the remote command locally, with /ws mapped to $FAKE_WS (the pod's /ws stand-in)
for a in "$@"; do last=$a; done
echo "ssh $last" >> "$FAKE_CALLS"
cmd=$(printf '%s' "$last" | sed "s|/ws|$FAKE_WS|g")
exec bash -c "$cmd"
"""
FAKE_RSYNC_LOCAL = r"""
# drops -e/-z, maps root@1.2.3.4:/ws to $FAKE_WS, then runs the real rsync
echo "rsync $*" >> "$FAKE_CALLS"
args=""
skip=0
for a in "$@"; do
  if [ "$skip" = "1" ]; then skip=0; continue; fi
  case "$a" in
    -e) skip=1; continue ;;
    -z) continue ;;
    root@1.2.3.4:/ws*) a="$FAKE_WS${a#root@1.2.3.4:/ws}" ;;
  esac
  args="$args '$a'"
done
eval "exec /usr/bin/rsync $args"
"""


def test_pull_e11_over_a_fake_ssh(pull, tmp_path):
    """The real (non-POD_PULL_SOURCE) path: .env's slot address, ssh for the tar streams and
    rsync -e ssh for the manifest pull, both faked onto the local /ws stand-in."""
    script, pod = pull
    fake_bin(tmp_path, "ssh", FAKE_SSH_LOCAL)
    fake_bin(tmp_path, "rsync", FAKE_RSYNC_LOCAL)
    (tmp_path / ".env").write_text("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\n")
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE_WS": str(pod.parent),
        "FAKE_CALLS": str(tmp_path / "calls.txt"),
    }
    (pod / "results" / "E11" / "MANIFEST.manifest.sha256").unlink()
    r = run_bash(
        script,
        "--exp",
        "E11",
        "--slot",
        "_A",
        "--manifest",
        "MANIFEST.pilot-r1.sha256",
        env_extra=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "results" / "E11" / "ladder" / "ready_r1.json").exists()
    r = run_bash(script, "--exp", "E11", "--slot", "_A", "--tar-adapters", env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        tmp_path / "results" / "E11" / "adapters" / "med_ino_s0" / "adapter_config.json"
    ).exists()
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    assert "ssh cd '/ws/adapters/E11' && tar cf - ." in calls
    assert any(c.startswith("rsync") and "root@1.2.3.4:/ws/emx" in c for c in calls)
    assert not any("pod_down" in c or "terminate" in c for c in calls)


@pytest.mark.parametrize(
    "args",
    [
        ("--manifest", "MANIFEST.x.partial.sha256"),
        ("--manifest", "../x.sha256"),
        ("--subdir", "x"),
        ("--run", "c"),
        ("--slot", "_B"),
        ("--tar",),
        ("--tar-acts", "--manifest", "MANIFEST.download.sha256"),
    ],
)
def test_pull_e11_usage_errors(pull, tmp_path, args):
    script, pod = pull
    assert run_pull(script, pod, *args).returncode == 2
    assert not (tmp_path / "results" / "E11").exists()


def test_pull_tar_modes_are_e11_only_and_unknown_exp_lists_e11(pull, tmp_path):
    script, pod = pull
    r = run_pull(script, pod, "--tar-adapters", exp="EQ0")
    assert r.returncode == 2 and "E11-only" in r.stderr
    r = run_bash(script, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "EQ0, E11" in r.stderr


# --- e11_watchdog.sh: a copy in tmp_path, fake uv / ssh / pod_pull_results.sh -------------------


@pytest.fixture
def wd(tmp_path):
    """A watchdog checkout: the real script, src/ linked, .env with the slot's address, a fake
    scripts/pod_pull_results.sh, and fake ssh (the poll and the recovery call) on PATH."""
    s = copy_script(tmp_path, "e11_watchdog.sh")
    (tmp_path / "src").symlink_to(ROOT / "src")
    (tmp_path / ".env").write_text("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\n")
    calls = tmp_path / "calls.txt"
    (tmp_path / "scripts" / "pod_pull_results.sh").write_text(
        f'echo "pull $*" >> {calls}\nexit 0\n'
    )
    fake_bin(
        tmp_path,
        "ssh",
        'for a in "$@"; do last=$a; done\n'
        f'echo "ssh $last" >> {calls}\n'
        'case "$last" in\n'
        '  *nvidia-smi*) [ -n "${FAKE_SSH_HANG:-}" ] && sleep "$FAKE_SSH_HANG"\n'
        "    printf '57\\nT E11_STEP_DONE bench\\nM MANIFEST.bench.sha256\\n"
        "M MANIFEST.recovery.sha256\\nM MANIFEST.smoke.sha256\\nM MANIFEST.bad;name.sha256\\n' ;;\n"
        '  *chain.pid*) [ -n "${FAKE_QUIESCE_FAIL:-}" ] && { echo E11_QUIESCE_FAILED; exit 1; }\n'
        "    echo E11_QUIESCED ;;\n"
        "esac\nexit 0\n",
    )
    (tmp_path / "results" / "E11").mkdir(parents=True)
    (tmp_path / "results" / "E11" / "MANIFEST.smoke.sha256").write_text("x\n")  # pulled already
    return s


def wd_events(tmp_path: Path, age_h: float) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    t0 = datetime.now(UTC) - timedelta(hours=age_h)
    ev = [created("pq", "_A", t0, 3.49), created("pq", "_A", t0 + timedelta(hours=1), 3.49)]
    (logs / "pods_E11.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))


def tick(tmp_path, rc, *, running=True, extra_env=None):
    calls = tmp_path / "calls.txt"
    pods = (
        [
            {
                "pod_id": "pq",
                "slot": "_A",
                "gpu": "g",
                "hours": 1.0,
                "rate": 3.49,
                "rate_source": "api",
                "usd": 3.49,
            }
        ]
        if running
        else []
    )
    status = {"exp": "E11", "running": pods, "hard_stop": rc == 2}
    body = (
        f'echo "uv $*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};; esac\n"
        "exit 0\n"
    )
    bindir = fake_bin(tmp_path, "uv", body)
    r = run_bash(
        tmp_path / "scripts" / "e11_watchdog.sh",
        "600",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "EMX_EXP": "E1",  # ignored: the watchdog passes --exp E11 everywhere
            "EMX_POD_LOG_DIR": str(tmp_path / "logs"),
            "E11_WATCHDOG_ONCE": "1",
            "E11_SALVAGE_S": "5",
            **(extra_env or {}),
        },
        cwd=tmp_path,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def test_e11_watchdog_healthy_tick_beats_polls_once_and_pulls_new_manifests(wd, tmp_path):
    wd_events(tmp_path, 1.0)
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith(
        "uv run python scripts/pod_budget_check.py --exp E11 --reserve-hours"
    )
    assert lines[1].startswith("uv run python scripts/pod_down.py --exp E11 --untracked")
    assert len([ln for ln in lines if ln.startswith("ssh")]) == 1  # one consolidated call
    pulls = [ln for ln in lines if ln.startswith("pull")]
    assert pulls == ["pull --exp E11 --slot _A --manifest MANIFEST.bench.sha256"]  # new only
    assert "progress E11_STEP_DONE bench" in r.stdout and "gpu util 57%" in r.stdout
    assert not any("pod_down.py --exp E11 --slot" in ln for ln in lines)
    t, interval, pid = (tmp_path / "logs" / "watchdog_E11.heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def test_e11_watchdog_salvage_at_pod_age_then_pod_down(wd, tmp_path):
    wd_events(tmp_path, 4.6)  # past contracts.SALVAGE_AGE_H (4.5 h), before POD_LIFE_H
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SALVAGE (_A, pod pq)" in r.stdout
    seq = [ln for ln in lines if not ln.startswith("uv run python scripts/pod_budget_check")]
    assert seq == [
        "uv run python scripts/pod_down.py --exp E11 --untracked --reason e11 watchdog reconcile",
        QUIESCE_CALL,
        "ssh cd /ws/emx && /ws/venv-peft/bin/python scripts/e11_pod.py recovery",
        "pull --exp E11 --slot _A --manifest MANIFEST.recovery.sha256",
        "pull --exp E11 --slot _A --tar-acts",
        "pull --exp E11 --slot _A --tar-adapters",
        (
            "uv run python scripts/pod_down.py --exp E11 --slot _A --reason e11 watchdog: "
            f"salvage at pod age >= {C.SALVAGE_AGE_H} h"
        ),
    ]  # separate commands, in order (writers stopped first); no poll of a pod being salvaged
    assert "chain stopped, no e11_pod.py left" in r.stdout
    assert not (tmp_path / "logs" / "watchdog_E11.alert").exists()


def _quiesce_call() -> str:
    text = (SCRIPTS / "e11_watchdog.sh").read_text()
    return "ssh " + text.split("\nQUIESCE='", 1)[1].split("'\n", 1)[0]


QUIESCE_CALL = _quiesce_call()


def test_e11_watchdog_quiesce_stops_the_chain_with_bracketed_patterns():
    """Code review r1a finding 2: the chain (chain.pid's children, itself, its group) and every
    e11_pod.py are stopped, bounded, before the recovery manifest; patterns never match the ssh
    command line itself."""
    q = QUIESCE_CALL
    assert "logs/e11/chain.pid" in q and 'pkill -TERM -P "$p"' in q and 'kill -TERM -- "-$g"' in q
    assert '"[e]11_pod[.]py"' in q and '"[p]od_run_e11[.]sh"' in q
    assert "e11_pod.py" not in q.replace("[e]11_pod[.]py", "")  # every mention bracketed
    assert '[ "$i" -lt 60 ]' in q and "pkill -KILL" in q and q.endswith("echo E11_QUIESCED")
    r = subprocess.run(["bash", "-n", "-c", q[4:]], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_e11_watchdog_quiesce_failure_alerts_and_salvage_goes_on(wd, tmp_path):
    wd_events(tmp_path, 4.6)
    r, lines = tick(tmp_path, 0, extra_env={"FAKE_QUIESCE_FAIL": "1"})
    assert r.returncode == 0
    alert = (tmp_path / "logs" / "watchdog_E11.alert").read_text()
    assert "stopping the chain FAILED or unconfirmed (E11_QUIESCE_FAILED)" in alert
    assert lines.index(QUIESCE_CALL) < lines.index(
        "ssh cd /ws/emx && /ws/venv-peft/bin/python scripts/e11_pod.py recovery")
    assert lines[-1].startswith("uv run python scripts/pod_down.py --exp E11 --slot _A")


def test_e11_watchdog_status_ssh_is_bounded(wd, tmp_path):
    """Code review r1a finding 3: a hung status ssh is cut at E11_POLL_SSH_S."""
    wd_events(tmp_path, 1.0)
    t0 = time.time()
    r, lines = tick(tmp_path, 0, extra_env={"FAKE_SSH_HANG": "30", "E11_POLL_SSH_S": "2"})
    assert r.returncode == 0 and time.time() - t0 < 20
    assert "unreachable for the idle check" in r.stdout
    assert not any(ln.startswith("pull") for ln in lines)


def test_e11_watchdog_pulls_yield_to_salvage_when_the_age_arrives(wd, tmp_path):
    """Code review r1a finding 3: each pull is bounded by the time left before the salvage age,
    the age is rechecked after it, and the salvage starts in the same tick."""
    (tmp_path / "scripts" / "pod_pull_results.sh").write_text(
        f'echo "pull $*" >> {tmp_path / "calls.txt"}\n'
        'case "$*" in *MANIFEST.bench.sha256*) sleep 40 ;; esac\nexit 0\n'
    )
    wd_events(tmp_path, 1.0)
    t0 = time.time()
    r, lines = tick(tmp_path, 0, extra_env={"E11_SALVAGE_AGE_H": str(1.0 + 8 / 3600)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert time.time() - t0 < 30  # the 40 s pull was cut at the salvage age
    bound = int(r.stdout.split("pulling it (bounded at ")[1].split("s)")[0])
    assert 1 <= bound <= 8
    assert "salvage age reached while polling; salvaging now" in r.stdout
    seq = [ln for ln in lines if ln.startswith(("pull", "ssh cd", "uv run python scripts/pod_down"))
           and "--untracked" not in ln]
    assert seq[0] == "pull --exp E11 --slot _A --manifest MANIFEST.bench.sha256"
    assert seq[1:3] == [QUIESCE_CALL,
                        "ssh cd /ws/emx && /ws/venv-peft/bin/python scripts/e11_pod.py recovery"]
    assert seq[-1].startswith("uv run python scripts/pod_down.py --exp E11 --slot _A")


def test_e11_watchdog_salvage_age_follows_the_contracts_and_env(wd, tmp_path):
    wd_events(tmp_path, 4.4)  # just under 4.5 h: no salvage
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0 and "SALVAGE" not in r.stdout
    assert f"salvage at {int(C.SALVAGE_AGE_H * 3600)}s" in r.stdout
    (tmp_path / "calls.txt").unlink()
    r, lines = tick(tmp_path, 0, extra_env={"E11_SALVAGE_AGE_H": "4"})  # the test override
    assert r.returncode == 0 and "SALVAGE (_A, pod pq)" in r.stdout
    assert lines[-1].startswith("uv run python scripts/pod_down.py --exp E11 --slot _A")


def test_e11_watchdog_salvage_failures_alert_but_still_pod_down(wd, tmp_path):
    (tmp_path / "scripts" / "pod_pull_results.sh").write_text(
        f'echo "pull $*" >> {tmp_path / "calls.txt"}\nexit 1\n'
    )
    wd_events(tmp_path, 4.6)
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0
    assert lines[-1].startswith("uv run python scripts/pod_down.py --exp E11 --slot _A")
    alert = (tmp_path / "logs" / "watchdog_E11.alert").read_text()
    assert "recovery manifest pull FAILED" in alert and "adapters tar pull FAILED" in alert


def test_e11_watchdog_hard_stop_and_fail_closed(wd, tmp_path):
    wd_events(tmp_path, 1.0)
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2 and "HARD STOP" in r.stdout and "e11-*" in r.stdout
    assert any(ln.startswith("ssh cd /ws/emx && tar cf - results/E11 logs") for ln in lines)
    assert lines[-1].startswith("uv run python scripts/pod_down.py --exp E11 --all-exp")
    (tmp_path / "calls.txt").unlink()
    r, lines = tick(tmp_path, 3)
    assert r.returncode == 3
    assert lines[-1].startswith("uv run python scripts/pod_down.py --exp E11 --all-exp")
    assert "BUDGET CHECK FAILED" in (tmp_path / "logs" / "watchdog_E11.alert").read_text()


def test_e11_watchdog_stop_removes_heartbeat(wd, tmp_path):
    hb = tmp_path / "logs" / "watchdog_E11.heartbeat"
    hb.parent.mkdir(exist_ok=True)
    hb.write_text(f"{time.time()} 600 1\n")
    stop = tmp_path / "logs" / "watchdog_E11.stop"
    bindir = fake_bin(tmp_path, "uv", f"touch {stop}; echo '{{\"running\": []}}'; exit 0\n")
    r = run_bash(
        tmp_path / "scripts" / "e11_watchdog.sh",
        "1",
        env_extra={"PATH": f"{bindir}:{os.environ['PATH']}"},
        cwd=tmp_path,
    )
    assert r.returncode == 0 and "stop file present" in r.stdout
    assert not hb.exists()


def test_e11_watchdog_heartbeat_makes_runpod_see_a_live_watchdog(wd, tmp_path, monkeypatch):
    wd_events(tmp_path, 1.0)
    tick(tmp_path, 0)
    monkeypatch.setattr(rp, "LOGS", tmp_path / "logs")
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    rp.configure("E11")
    t, interval, _ = rp.heartbeat_path().read_text().split()
    rp.heartbeat_path().write_text(f"{t} {interval} {os.getpid()}\n")
    assert rp.watchdog_live()[0] is True


def test_e11_watchdog_names_only_e11():
    text = (SCRIPTS / "e11_watchdog.sh").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for other in ("--exp EQ0", "results/EQ0", "logs/eq0", "eq0-", "watchdog_EQ0", "^EQ0_"):
        assert other not in code
    assert C.WATCHDOG_HEARTBEAT in text and "logs/e11/run_" in text and "'^E11_'" in text
    assert "--exp E11" in code and "--all-exp" in code


# --- shell hygiene -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["pod_push.sh", "pod_setup.sh", "pod_pull_results.sh", "e11_watchdog.sh"]
)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in (*BASH4, "${x,,}"):
        assert bad not in code, (name, bad)


def test_new_watchdog_has_no_bash4_even_in_comments():
    text = (SCRIPTS / "e11_watchdog.sh").read_text()
    for bad in ("declare -A", "mapfile", ",,}"):
        assert bad not in text
