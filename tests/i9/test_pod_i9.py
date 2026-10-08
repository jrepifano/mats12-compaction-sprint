"""U6: I9 pod tooling, offline (I9 implementation plan §2 U6, §4 P1 and P3, §6). `--exp I9` in
emx.runpod / pod_up / pod_down / pod_budget_check (pods i9-<slot>, logs/pods_I9.jsonl, CAPS["I9"],
slot _A = session A needing logs/approvals/I9-P1.approved, slot _B = session B needing
I9-P3.approved, GPU fallback H100 community -> H100 secure -> H100 NVL), pod_push.sh's I9 transfer
set and pod env, pod_setup.sh --only i9, pod_pull_results.sh --exp I9, scripts/pod_run_i9.sh and
scripts/i9_watchdog.sh. Nothing here contacts RunPod, SSH or the Hub: fake clients and fake
git/uv/python on PATH, sandboxed event logs and ledgers (the helpers of tests/i1a/test_pod_i1a.py
are reused, as tests/e1b/test_pod_infra.py does)."""

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
from emx.i9 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UTC_LINE = "2026-10-04T12:00:00Z approved by Jacob (test)\n"
THINKING_LINE = "thinking-arm: approved (test)\n"  # P3 marker line the thinking arm needs


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# helpers only (no fixtures or tests are bound here, so nothing of that module is collected)
_i1a = _load("_i9_reuse_i1a_pod_tests", ROOT / "tests" / "i1a" / "test_pod_i1a.py")
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
def i9(monkeypatch, tmp_path):
    """emx.runpod configured for I9 with .env, logs and the event log in tmp_path; restored."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    rp.configure("I9")
    return tmp_path


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


def approve(logs: Path, step: str, text: str = UTC_LINE) -> Path:
    p = logs / "approvals" / f"I9-{step}.approved"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def beat(logs: Path) -> None:
    (logs / "watchdog_I9.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")


def created(pod_id, slot, t0, rate):
    return {
        "event": "created",
        "pod_id": pod_id,
        "slot": slot,
        "gpu": "g",
        "utc": t0.isoformat(),
        "rate_usd_h": rate,
    }


# --- emx.runpod under I9 -------------------------------------------------------------------------


def test_configure_i9_names_logs_and_caps(i9):
    assert rp.EXP == "I9" and rp.POD_NAME_PREFIX == "i9-"
    assert rp.POD_EVENTS == i9 / "pods_I9.jsonl"
    c = rp.caps()
    assert (c.hard_cap_usd, c.gpu_limit_usd, c.reserve_usd) == (65.0, 36.0, 2.0)
    assert (c.hard_cap_usd, c.gpu_limit_usd) == (C.HARD_CAP_USD, C.GPU_LIMIT_USD)
    assert rp.gpu_kill_limit(0.0) == 36.0 and rp.gpu_kill_limit(30.0) == 33.0  # 65 - 2 - 30
    assert rp.gpu_ceiling() is None
    names = [rp.pod_body(rp.GPU_FALLBACK[0], s, "ssh-ed25519 k")["name"] for s in ("_A", "_B")]
    assert names == ["i9-a", "i9-b"]
    assert rp.heartbeat_path() == i9 / "watchdog_I9.heartbeat"
    assert rp.approval_path("P1") == i9 / "approvals" / "I9-P1.approved"


def test_untracked_reaper_only_sees_i9_pods(i9):
    api = [{"id": "x", "name": "i9-a"}, {"id": "y", "name": "i1a-a"}, {"id": "z", "name": "e1-a"}]
    assert [p["id"] for p in rp.untracked_pods(api, [])] == ["x"]


def test_killswitch_deadline_at_the_i9_gpu_line(i9, spend):
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    events = [created("pa", "_A", now - timedelta(hours=1), 3.49)]
    spend.totals = {"gpu": 10.0, "judge": 2.0}
    # min(36, 65 - 2 - 2) = 36; 36 - 10 logged - 3.49 running = 22.51 left at 3.49/h
    want = int(now.timestamp() + (36 - 10 - 3.49) / 3.49 * 3600)
    assert rp.killswitch_deadline("pa", events, now) == want
    spend.totals = {"gpu": 10.0, "judge": 30.0}  # judges eat into the line: min(36, 33) = 33
    assert rp.killswitch_deadline("pa", events, now) == int(
        now.timestamp() + (33 - 10 - 3.49) / 3.49 * 3600
    )


# --- pod_up --exp I9 ------------------------------------------------------------------------------


@pytest.fixture
def up(spend, i9, monkeypatch):
    mod = _load("_i9_pod_up", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def use_client(monkeypatch, fake):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)


def test_i9_fallback_order_is_h100_community_secure_nvl(up):
    got = [(o.gpu_type_id, o.cloud) for o in up.i9_gpu_options()]
    assert got == [
        ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
        ("NVIDIA H100 80GB HBM3", "SECURE"),
        ("NVIDIA H100 NVL", "COMMUNITY"),
    ]
    assert [o.cloud for o in up.i9_gpu_options(1)] == ["SECURE", "COMMUNITY"]


@pytest.mark.parametrize("slot,step", [("_A", "P1"), ("_B", "P3")])
def test_pod_up_i9_refuses_a_session_without_its_approval(up, i9, monkeypatch, slot, step):
    beat(i9)
    other = "P3" if step == "P1" else "P1"
    approve(i9, other)  # the other session's approval does not count
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "I9", "--slot", slot])
    assert e.value.code == up.GATE_REFUSED and fake.created == []
    approve(i9, step, "go\n")  # no UTC line: still refused
    with pytest.raises(SystemExit):
        up.main(["--exp", "I9", "--slot", slot])
    assert fake.created == []


def test_pod_up_i9_refuses_a_slot_that_is_not_a_session(up, i9, monkeypatch):
    beat(i9)
    approve(i9, "P1")
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "I9", "--slot", "_C"])
    assert e.value.code == 2 and fake.created == []


def test_pod_up_i9_needs_a_live_watchdog(up, i9, monkeypatch):
    approve(i9, "P1")
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "I9", "--slot", "_A"])
    assert e.value.code == up.UNTRACKED and fake.created == []


def test_pod_up_i9_approved_session_falls_back_to_secure_h100(up, i9, monkeypatch, spend):
    beat(i9)
    approve(i9, "P1")
    fake = FakeClient(
        create_results=[rp.RunPodError("no community H100"), {"id": "p1", "costPerHr": 3.49}]
    )
    use_client(monkeypatch, fake)
    assert up.main(["--exp", "I9", "--slot", "_A"]) == 0
    assert [(b["name"], b["cloudType"], b["gpuTypeIds"][0]) for b in fake.created] == [
        ("i9-a", "COMMUNITY", "NVIDIA H100 80GB HBM3"),
        ("i9-a", "SECURE", "NVIDIA H100 80GB HBM3"),
    ]
    ev = rp.read_pod_events()[0]
    assert ev["event"] == "created" and ev["slot"] == "_A" and ev["exp"] == "I9"
    assert rp.env("RUNPOD_POD_ID_A") == "p1"
    assert all(c.get("exp") == "I9" for c in spend.checks)  # the combined guard asks about I9


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


@pytest.mark.parametrize("slot,step", [("_A", "P1"), ("_B", "P3")])
def test_pod_up_dry_run_i9(tmp_path, slot, step):
    r = run_py("scripts/pod_up.py", "--exp", "I9", "--slot", slot, "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert [q["json"]["name"] for q in reqs] == [f"i9-{slot[1:].lower()}"] * 3
    assert [(q["json"]["cloudType"], q["json"]["gpuTypeIds"][0]) for q in reqs] == [
        ("COMMUNITY", "NVIDIA H100 80GB HBM3"),
        ("SECURE", "NVIDIA H100 80GB HBM3"),
        ("COMMUNITY", "NVIDIA H100 NVL"),
    ]
    assert f"approval (I9 session {slot[1]}, {step}): MISSING" in r.stdout
    assert "watchdog (I9): NOT LIVE" in r.stdout and "budget gate (I9)" in r.stdout
    assert not (tmp_path / "pods_I9.jsonl").exists()  # a dry run logs no event
    # with a sandboxed approval and a live heartbeat, both are reported as present
    approve(tmp_path, step)
    beat(tmp_path)
    r = run_py("scripts/pod_up.py", "--exp", "I9", "--slot", slot, "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0 and "): present" in r.stdout and "watchdog (I9): live" in r.stdout


def test_pod_up_dry_run_i9_refuses_other_slots(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "I9", "--slot", "_C", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "I9 slots are _A" in r.stderr and "DRY RUN" not in r.stdout


def test_pod_down_and_budget_check_dry_i9(tmp_path):
    r = run_py("scripts/pod_down.py", "--exp", "I9", "--all-exp", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "<each i9-* pod id>" in r.stdout
    r = run_py("scripts/pod_budget_check.py", "--exp", "I9", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["exp"] == "I9" and d["gpu_kill_limit_usd"] == 36.0 and d["hard_cap_usd"] == 65.0
    assert d["gpu_ceiling_usd"] is None and d["hard_stop"] is False


def test_budget_check_i9_kills_at_the_gpu_line(spend, i9, capsys):
    bc = _load("_i9_budget", SCRIPTS / "pod_budget_check.py")
    now = datetime.now(UTC)
    rp.POD_EVENTS.write_text(json.dumps(created("pa", "_A", now - timedelta(hours=2), 3.49)) + "\n")
    spend.totals = {"gpu": 20.0, "judge": 3.0}
    assert bc.main(["--exp", "I9"]) == 0  # 20 + 6.98 < 36
    assert bc.main(["--exp", "I9", "--reserve-hours", "3"]) == 2  # + 10.47 >= 36
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[-1])["hard_stop"] is True


# --- pod_push.sh --exp I9 (a copy in tmp_path, a fake checkout and a FAKE git) -------------------

TRACKED = [
    "pyproject.toml",
    "uv.lock",
    "src/emx/__init__.py",
    "src/emx/i9/contracts.py",
    "scripts/i9_pod.py",
    "scripts/pod_run_i9.sh",
    "third_party/workspace-bench/pyproject.toml",
    "third_party/workspace-bench/src/wsbench/judge.py",
    "third_party/agentic-misalignment/scripts/generate_prompts.py",
    "third_party/agentic-misalignment/templates/blackmail/variables/ambiguous.md",
    "third_party/olens_draft/oracle_lens/x.py",  # another vendored repo: not an I9 root
    "third_party/emergent-misalignment/q.yaml",  # not an I9 root
    "prompts/I9/monitor_zero_shot.txt",
    "prompts/I1a/v1.txt",  # another experiment's prompts: not an I9 root
    "data/I9/scenarios.jsonl",
    "data/I9/sets.json",
    "results/I1a/readouts/logit_lens/multihop.jsonl",  # the smoke's I1a reference: named extra
    "results/I1a/MANIFEST.capture.sha256",
    "data/I1a/subsets/x.json",  # not an I9 root
    "configs/E1/a.json",  # not an I9 root
    "docs/private-notes.md",
]
IGNORED = ["data/I9/cache/draw_table.json"]  # gitignored under data/I9: sent as a named extra
NEVER = [
    ".env.local",
    "data/raw/bad_medical_advice.jsonl",
    "results/I9/acts/qwen_last.safetensors",
    "results/I9/replies/qwen_nothink.jsonl",
    "logs/approvals/I1a-P1.approved",
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


def push(checkout, *args):
    tmp = checkout.parent.parent
    extra = {
        "POD_PUSH_GIT": str(tmp / "bin" / "fakegit"),
        "FAKEGIT_DIR": str(tmp / ".fakegit"),
        "EMX_EXP": "E1",  # ignored
    }
    return run_bash(checkout, *args, "--dry-run", "--show-files", env_extra=extra)


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def line(stdout: str, tag: str) -> list[str]:
    hit = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return hit.split(f"{tag}:", 1)[1].split()


@pytest.mark.parametrize("slot,step,other", [("_A", "P1", "P3"), ("_B", "P3", "P1")])
def test_pod_push_i9_transfer_set_env_and_setup(checkout, tmp_path, slot, step, other):
    approve(tmp_path / "logs", step)
    approve(tmp_path / "logs", other)
    r = push(checkout, "--exp", "I9", "--slot", slot)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (I9)" in r.stdout and "WARNING" not in r.stdout
    assert line(r.stdout, "transfer set") == [
        "src",
        "scripts",
        "third_party/workspace-bench",
        "third_party/agentic-misalignment",
        "prompts/I9",
        "data/I9",
        "pyproject.toml",
        "uv.lock",
    ]
    files = sent(r.stdout)
    roots = (
        "src/",
        "scripts/",
        "third_party/workspace-bench/",
        "third_party/agentic-misalignment/",
        "prompts/I9/",
        "data/I9/",
    )
    want = {f for f in TRACKED if f.startswith(roots) or f in ("pyproject.toml", "uv.lock")}
    approvals = {"logs/approvals/I9-P1.approved", "logs/approvals/I9-P3.approved"}
    i1a_ref = {
        "results/I1a/readouts/logit_lens/multihop.jsonl",
        "results/I1a/MANIFEST.capture.sha256",
    }  # the smoke's I1a reproduction reference
    assert files == want | set(IGNORED) | approvals | i1a_ref
    assert not files & set(NEVER)
    keys = line(r.stdout, "pod env keys")
    assert keys[0] == "HF_TOKEN" and "HF_USERNAME" not in keys  # HF_TOKEN only
    assert {"EMX_EXP", "EMX_GIT_COMMIT", "HF_HOME", "HF_HUB_DISABLE_XET", "WANDB_MODE"} <= set(keys)
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "hf_abc" not in r.stdout and "secret" not in r.stdout
    assert f"--check-slot {slot}" in r.stdout and "pod_setup.sh --only i9" in r.stdout
    assert "pod_killswitch.sh arm" in r.stdout


def test_pod_push_i9_warns_on_missing_inputs_in_dry_run(checkout, tmp_path):
    (tmp_path / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if f not in ("scripts/i9_pod.py", "data/I9/sets.json")) + "\n"
    )
    (tmp_path / "data" / "I9" / "sets.json").unlink()  # else it is sent as an ignored extra
    r = push(checkout, "--exp", "I9", "--slot", "_B")
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "WARNING" in ln)
    for f in ("scripts/i9_pod.py", "data/I9/sets.json", "logs/approvals/I9-P3.approved"):
        assert f in warn
    text = (SCRIPTS / "pod_push.sh").read_text()
    assert "required I9 inputs missing from the transfer set; refusing" in text  # a real push


def test_pod_push_i9_refuses_bad_slots_modified_and_untracked_files(checkout, tmp_path):
    r = push(checkout, "--exp", "I9", "--slot", "_C")
    assert r.returncode == 2 and "slot must be _A" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "I9", "--slot", "_A")
    assert r.returncode == 1 and "commit them before an I9 push" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("")
    (tmp_path / ".fakegit" / "untracked").write_text("data/I9/new.jsonl\n")
    r = push(checkout, "--exp", "I9", "--slot", "_A")
    assert r.returncode == 1 and "data/I9/new.jsonl" in r.stderr


def test_pod_push_other_exps_unchanged(checkout, tmp_path):
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "--only i1a" in r.stdout and "logs/approvals/I1a-P1.approved" in sent(r.stdout)
    assert "third_party/olens_draft/oracle_lens/x.py" in sent(r.stdout)  # I1a keeps all of it
    r = push(checkout, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "knows E1, I1a, E1b and I9 only" in r.stderr


# --- pod_setup.sh --only i9 -----------------------------------------------------------------------


def test_pod_setup_only_i9_builds_two_venvs_with_the_shared_builder():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    branch = text.split('if [ "$ONLY" = "i9" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert 'build i9 pod-i1a "wsbench, torch, transformers, peft, safetensors, sklearn"' in branch
    assert 'build vllm vllm "vllm, torch, transformers"' in branch
    assert "exit 0" in branch and "build peft" not in branch and "unsloth" not in branch
    assert '""|i1a|e1b|i9) ;;' in text
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "i8"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "i9" in r.stderr


# --- pod_pull_results.sh --exp I9 ----------------------------------------------------------------


def write_step(pod: Path, step: str, files: dict[str, bytes], commit: str = HEAD) -> None:
    lines = []
    for rel, data in files.items():
        p = pod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        lines.append(f"{sha256(p)}  {rel}")
    res = pod / "results" / "I9"
    (res / f"MANIFEST.{step}.sha256").write_text("\n".join(lines) + "\n")
    (res / "manifests").mkdir(parents=True, exist_ok=True)
    (res / "manifests" / f"{step}.json").write_text(
        json.dumps({"step": step, "git_commit": commit, "inputs": [], "outputs": []})
    )


def write_closing(pod: Path, steps: list[str]) -> None:
    res = pod / "results" / "I9"
    lines = [
        f"{sha256(res / f'MANIFEST.{s}.sha256')}  results/I9/MANIFEST.{s}.sha256" for s in steps
    ]
    (res / "MANIFEST.manifest.sha256").write_text("\n".join(lines) + "\n")
    (res / "manifests" / "manifest.json").write_text(json.dumps({"step": "manifest"}))


@pytest.fixture
def pull(tmp_path):
    s = copy_script(tmp_path, "pod_pull_results.sh")
    pod = tmp_path / "pod"  # stands for the pod's /ws/emx
    write_step(pod, "smoke", {"results/I9/checks/smoke.json": b"{}\n"})
    write_step(pod, "sample-qwen", {"results/I9/replies/qwen_nothink.jsonl": b"r\n" * 8})
    write_step(pod, "capture-last", {"results/I9/acts/qwen_last.safetensors": b"\0" * 64})
    write_closing(pod, ["smoke", "sample-qwen", "capture-last"])
    # being written right now, listed by no manifest: never promoted
    (pod / "results" / "I9" / "replies" / "gemma_nothink.jsonl").write_text("partial\n")
    return s, pod


def run_pull(script, pod, *args):
    return run_bash(script, "--exp", "I9", *args, env_extra={"POD_PULL_SOURCE": str(pod)})


def test_pull_i9_closing_manifest_verifies_then_promotes_listed_files_only(pull, tmp_path):
    script, pod = pull
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "I9"
    assert (dest / "acts" / "qwen_last.safetensors").stat().st_size == 64
    assert (dest / "replies" / "qwen_nothink.jsonl").exists()
    assert not (dest / "replies" / "gemma_nothink.jsonl").exists()  # unlisted
    for step in ("smoke", "sample-qwen", "capture-last", "manifest"):
        assert (dest / f"MANIFEST.{step}.sha256").exists()
        assert (dest / "manifests" / f"{step}.json").exists()
    assert "verifying results/I9/MANIFEST.manifest.sha256" in r.stdout
    assert not list(dest.glob(".pull_*"))  # the staging dir is cleaned up


def test_pull_i9_mid_session_named_manifest(pull, tmp_path):
    script, pod = pull
    (pod / "results" / "I9" / "MANIFEST.manifest.sha256").unlink()  # the session is not over
    r = run_pull(script, pod, "--manifest", "MANIFEST.smoke.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "I9"
    assert (dest / "checks" / "smoke.json").exists() and (
        dest / "manifests" / "smoke.json"
    ).exists()
    assert not (dest / "replies").exists()


@pytest.mark.parametrize(
    "breakage",
    [
        "tamper_output",
        "tamper_step_manifest",
        "no_closing",
        "closing_lists_none",
        "no_record",
        "wrong_record",
        "outside_path",
    ],
)
def test_pull_i9_refuses_and_promotes_nothing(pull, tmp_path, breakage):
    script, pod = pull
    res = pod / "results" / "I9"
    if breakage == "tamper_output":
        (res / "acts" / "qwen_last.safetensors").write_bytes(b"\1" * 64)
    elif breakage == "tamper_step_manifest":
        (res / "MANIFEST.smoke.sha256").write_text("0" * 64 + "  results/I9/checks/smoke.json\n")
    elif breakage == "no_closing":
        (res / "MANIFEST.manifest.sha256").unlink()
    elif breakage == "closing_lists_none":
        (res / "MANIFEST.manifest.sha256").write_text(
            f"{sha256(res / 'checks' / 'smoke.json')}  results/I9/checks/smoke.json\n"
        )
    elif breakage == "no_record":
        (res / "manifests" / "sample-qwen.json").unlink()
    elif breakage == "wrong_record":
        (res / "manifests" / "smoke.json").write_text(json.dumps({"step": "sample-qwen"}))
    elif breakage == "outside_path":
        (pod / "data").mkdir()
        (pod / "data" / "x.txt").write_text("x\n")
        write_step(pod, "smoke", {"data/x.txt": b"x\n"})
        write_closing(pod, ["smoke", "sample-qwen", "capture-last"])
    r = run_pull(script, pod)
    assert r.returncode != 0, r.stdout
    assert "nothing promoted" in r.stderr
    dest = tmp_path / "results" / "I9"
    assert not (dest / "acts").exists() and not (dest / "replies").exists()
    assert not list(dest.glob("MANIFEST.*"))


def test_pull_i9_moves_changed_local_files_aside(pull, tmp_path):
    script, pod = pull
    assert run_pull(script, pod).returncode == 0
    dest = tmp_path / "results" / "I9"
    old_closing = (dest / "MANIFEST.manifest.sha256").read_text()
    # session B's pod: its own download step and closing manifest, same names as session A's
    for s in ("smoke", "sample-qwen", "capture-last"):  # not on B's pod
        (pod / "results" / "I9" / f"MANIFEST.{s}.sha256").unlink()
    write_step(pod, "download", {"results/I9/download_paths.json": b'{"b": 1}\n'})
    write_closing(pod, ["download"])
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "moved" in r.stdout and "superseded" in r.stdout
    kept = list((dest / "superseded").glob("*/MANIFEST.manifest.sha256"))
    assert len(kept) == 1 and kept[0].read_text() == old_closing
    assert (dest / "MANIFEST.manifest.sha256").read_text() != old_closing


def test_pull_i9_refuses_a_stale_closing_manifest(pull, tmp_path):
    """A step completed after the closing manifest (review r2 finding 1) is not silently left
    behind: the default pull refuses; a named --manifest pull still works."""
    script, pod = pull
    write_step(pod, "sample-gemma", {"results/I9/replies/gemma_nothink.jsonl": b"g\n" * 8})
    r = run_pull(script, pod)
    assert r.returncode == 1, r.stdout
    assert "is stale" in r.stderr and "MANIFEST.sample-gemma.sha256" in r.stderr
    assert "nothing promoted" in r.stderr
    assert not list((tmp_path / "results" / "I9").glob("MANIFEST.*"))
    r = run_pull(script, pod, "--manifest", "MANIFEST.sample-gemma.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    write_closing(pod, ["smoke", "sample-qwen", "sample-gemma", "capture-last"])
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "results" / "I9" / "replies" / "gemma_nothink.jsonl").read_bytes() == (
        b"g\n" * 8
    )


@pytest.mark.parametrize(
    "args",
    [
        ("--manifest", "MANIFEST.x.partial.sha256"),
        ("--manifest", "../x.sha256"),
        ("--manifest", "MANIFEST.*.sha256"),
        ("--subdir", "x"),
        ("--run", "c"),
    ],
)
def test_pull_i9_usage_errors(pull, args):
    script, pod = pull
    assert run_pull(script, pod, *args).returncode == 2


# --- pod_run_i9.sh --------------------------------------------------------------------------------

# the real driver's step table: the fake below refuses what scripts/i9_pod.py refuses (a missing
# upstream manifest, a contingent arm without its flag), so the chains are tested against it
_POD = _load("_i9_pod_driver_table", SCRIPTS / "i9_pod.py")
DRIVER = (
    r"""
# fake `python scripts/i9_pod.py STEP [FLAGS]`: writes STEP's output, manifest and Manifest record
step="$2"; shift 2; flags="$*"
echo "driver $step session=$I9_SESSION exp=$EMX_EXP vllm=$I9_VLLM_PY flags=[$flags]" >> "$CALLS"
case "$step" in
  sample-thinking) case " $flags " in *" --thinking-arm "*) ;; *) echo "no arm" >&2; exit 9 ;; esac ;;
  tokens-sealed|olens-sealed)
    case " $flags " in *" --unsealed "*) ;; *) echo "not unsealed" >&2; exit 9 ;; esac ;;
esac
need=""
case "$step" in
"""
    + "".join(f'  {s}) need="{" ".join(u)}" ;;\n' for s, u in _POD.UPSTREAM.items())
    + r"""esac
for u in $need; do
  [ -s "results/I9/MANIFEST.$u.sha256" ] || { echo "upstream $u has no manifest" >&2; exit 8; }
done
case " ${FAIL_STEPS:-} " in *" $step "*) echo "boom" >&2; exit 7 ;; esac
case " ${NOMANIFEST_STEPS:-} " in *" $step "*) exit 0 ;; esac
mkdir -p results/I9/out results/I9/manifests
if [ "$step" = "manifest" ]; then  # as the real one: every session step with a manifest now
  case "$I9_SESSION" in
"""
    + "".join(
        f'    {s}) steps="{" ".join((*_POD.SESSION_STEPS[s], *_POD.SESSION_OPTIONAL[s]))}" ;;\n'
        for s in _POD.SESSION_STEPS
    )
    + r"""  esac
  : > results/I9/MANIFEST.manifest.sha256.tmp
  for s in $steps; do
    m="results/I9/MANIFEST.$s.sha256"
    if [ -s "$m" ]; then shasum -a 256 "$m" >> results/I9/MANIFEST.manifest.sha256.tmp; fi
  done
  mv results/I9/MANIFEST.manifest.sha256.tmp results/I9/MANIFEST.manifest.sha256
else
  echo "$step output" > "results/I9/out/$step.txt"
  shasum -a 256 "results/I9/out/$step.txt" > "results/I9/MANIFEST.$step.sha256"
fi
printf '{"step": "%s", "git_commit": "%s"}\n' "$step" "${RECORD_COMMIT:-$EMX_GIT_COMMIT}" \
  > "results/I9/manifests/$step.json"
"""
)


@pytest.fixture
def chain(tmp_path):
    s = copy_script(tmp_path, "pod_run_i9.sh")
    bindir = fake_bin(tmp_path, "fakepy", DRIVER)
    (tmp_path / ".env").write_text(f"EMX_GIT_COMMIT={HEAD}\nHF_TOKEN=hf_x\n")
    (tmp_path / "logs").mkdir()
    return s, bindir / "fakepy"


def run_chain(chain, *args, dry=False, **env):
    script, py = chain
    tmp = script.parent.parent
    extra = {"I9_PY": str(py), "I9_VLLM_PY": "/fake/vllm/python", "CALLS": str(tmp / "calls")}
    if dry:
        extra["I9_DRY_RUN"] = "1"
    extra.update(env)
    return run_bash(script, *args, env_extra=extra)


_STAMPS = iter(range(1, 1_000_000))


def fresh_stamp() -> str:
    """A run-log stamp no other run in the test uses (I9_RUN_STAMP), so a rerun gets its own
    run log without sleeping into a new UTC second (the script's default stamp)."""
    return f"29990101T{next(_STAMPS):06d}Z"


def run_log(tmp: Path, r) -> str:
    path = next(ln.split("=", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("RUN_LOG="))
    return (tmp / path).read_text()


def driver_calls(tmp: Path) -> list[str]:
    p = tmp / "calls"
    return [ln.split()[1] for ln in p.read_text().splitlines()] if p.exists() else []


def driver_flags(tmp: Path) -> dict[str, list[str]]:
    """{step: the flags of its last driver call}."""
    out = {}
    for ln in (tmp / "calls").read_text().splitlines():
        out[ln.split()[1]] = ln.split("flags=[", 1)[1].rstrip("]").split()
    return out


def planned(stdout: str) -> list[str]:
    return [
        ln.split("i9_pod.py ", 1)[1].split()[0]
        for ln in stdout.splitlines()
        if ln.startswith("+ ") and "i9_pod.py" in ln
    ]


@pytest.mark.parametrize(
    "args,approval,want",
    [
        (["--session", "A", "--phase", "smoke"], "P1", ["download", "smoke"]),
        (["--session", "B", "--phase", "pilot"], "P3", ["download", "capture-64", "olens-pilot"]),
        (["--session", "B", "--step", "capture-64"], "P3", ["capture-64"]),  # a pilot-phase step
        (["--session", "B", "--step", "tokens"], "P3", []),  # needs the pilot first
        (["--session", "B", "--step", "sample-thinking", "--thinking"], "P3", []),  # also
        (["--session", "B", "--step", "manifest"], "P3", []),  # also
    ],
)
def test_run_i9_dry_plans(chain, tmp_path, args, approval, want):
    approve(tmp_path / "logs", approval)
    r = run_chain(chain, *args, dry=True)
    assert planned(r.stdout) == want, r.stdout
    assert r.returncode == (0 if want else 3)


@pytest.mark.parametrize(
    "args", [["--session", "A", "--phase", "smoke"], ["--session", "B", "--phase", "pilot"]]
)
def test_run_i9_refuses_without_the_sessions_approval(chain, tmp_path, args):
    other = "P3" if args[1] == "A" else "P1"
    approve(tmp_path / "logs", other)
    r = run_chain(chain, *args)
    log = run_log(tmp_path, r)
    assert r.returncode == 3 and "I9_STEP_REFUSED download" in log
    assert "I9_CHAIN_FAILED" in log and driver_calls(tmp_path) == []


def test_run_i9_session_a_smoke_then_main_with_tags_and_own_logs(chain, tmp_path):
    approve(tmp_path / "logs", "P1")
    r = run_chain(chain, "--session", "A", "--phase", "main")
    assert r.returncode == 3 and "REFUSED sample-qwen: smoke is not complete" in run_log(
        tmp_path, r
    )
    r = run_chain(chain, "--session", "A", "--phase", "smoke")
    assert r.returncode == 0, r.stdout + r.stderr
    log1 = run_log(tmp_path, r)
    tags = [ln for ln in log1.splitlines() if ln.startswith("I9_")]
    assert tags == [
        "I9_STEP_DONE download",
        "I9_STEP_DONE smoke",
        "I9_CHAIN_DONE session=A phase=smoke",
    ]
    r = run_chain(chain, "--session", "A", "--phase", "main", I9_RUN_STAMP=fresh_stamp())
    assert r.returncode == 0, r.stdout + r.stderr
    assert driver_calls(tmp_path) == [
        "download",
        "smoke",
        "sample-qwen",
        "sample-gemma",
        "capture-last",
        "manifest",
    ]  # session A has no thinking arm (amendment B item 7: it moved to session B)
    calls = (tmp_path / "calls").read_text()
    assert "session=A exp=I9 vllm=/fake/vllm/python" in calls
    runs = sorted((tmp_path / "logs" / "i9").glob("run_A_*.log"))
    assert len(runs) == 3  # main (refused), smoke, main: one log per run
    assert any((tmp_path / "logs" / "i9").glob("sample-qwen_*.log"))
    closing = (tmp_path / "results" / "I9" / "MANIFEST.manifest.sha256").read_text()
    for s in ("download", "smoke", "sample-qwen", "sample-gemma", "capture-last"):
        assert f"results/I9/MANIFEST.{s}.sha256" in closing
    # rerun: every step verifies and is not called again; only the closing manifest is handed
    # to the driver (which keeps it when it is current, or regenerates it)
    r = run_chain(chain, "--session", "A", "--phase", "main", I9_RUN_STAMP=fresh_stamp())
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path)[6:] == ["manifest"]
    assert "I9_STEP_DONE sample-qwen" in run_log(tmp_path, r)
    assert all(f == [] for f in driver_flags(tmp_path).values())


def test_run_i9_session_b_pilot_then_main_then_unsealed(chain, tmp_path):
    approve(tmp_path / "logs", "P3")
    r = run_chain(chain, "--session", "B", "--phase", "main")
    assert r.returncode == 3 and "REFUSED tokens: olens-pilot is not complete" in run_log(
        tmp_path, r
    )
    r = run_chain(chain, "--session", "B", "--phase", "pilot")
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path) == ["download", "capture-64", "olens-pilot"]  # capture first
    r = run_chain(chain, "--session", "B", "--phase", "main", I9_RUN_STAMP=fresh_stamp())
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path)[3:] == ["tokens", "olens", "manifest"]  # no optional arm
    res = tmp_path / "results" / "I9"
    assert "tokens-sealed" not in (res / "MANIFEST.manifest.sha256").read_text()
    # unsealed later (the spread check failed): only the sealed steps run, with the flag, and
    # the closing manifest is regenerated to list them (review r2 finding 1)
    r = run_chain(
        chain, "--session", "B", "--phase", "main", "--unsealed", I9_RUN_STAMP=fresh_stamp()
    )
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path)[6:] == ["tokens-sealed", "olens-sealed", "manifest"]
    flags = driver_flags(tmp_path)
    assert flags["tokens-sealed"] == flags["olens-sealed"] == ["--unsealed"]
    assert flags["tokens"] == []
    log = run_log(tmp_path, r)
    assert "I9_STEP_DONE tokens" in log and "I9_CHAIN_DONE session=B phase=main" in log
    assert "session=B" in (tmp_path / "calls").read_text()
    closing = (res / "MANIFEST.manifest.sha256").read_text()
    assert "MANIFEST.tokens-sealed.sha256" in closing and "MANIFEST.olens-sealed.sha256" in closing
    # the default pull (closing manifest) on the laptop fetches the sealed readouts
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    r = run_pull(copy_script(laptop, "pod_pull_results.sh"), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    for s in ("tokens-sealed", "olens-sealed", "tokens", "olens", "olens-pilot", "manifest"):
        assert (laptop / "results" / "I9" / f"MANIFEST.{s}.sha256").exists(), s
        assert (laptop / "results" / "I9" / "manifests" / f"{s}.json").exists(), s
    for s in ("tokens-sealed", "olens-sealed"):
        assert (laptop / "results" / "I9" / "out" / f"{s}.txt").read_text() == f"{s} output\n"
    # the thinking arm decided at checkpoint 2, run last here: it too joins the closing manifest
    approve(tmp_path / "logs", "P3", UTC_LINE + THINKING_LINE)
    r = run_chain(
        chain,
        "--session",
        "B",
        "--phase",
        "main",
        "--thinking",
        "--unsealed",
        I9_RUN_STAMP=fresh_stamp(),
    )
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path)[9:] == ["sample-thinking", "manifest"]
    assert driver_flags(tmp_path)["sample-thinking"] == ["--thinking-arm", "--unsealed"]
    assert "MANIFEST.sample-thinking.sha256" in (res / "MANIFEST.manifest.sha256").read_text()
    r = run_pull(copy_script(laptop, "pod_pull_results.sh"), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (laptop / "results" / "I9" / "out" / "sample-thinking.txt").exists()


@pytest.mark.parametrize("thinking", [False, True])
def test_run_i9_session_b_unsealed_from_the_pilot_on(chain, tmp_path, thinking):
    approve(tmp_path / "logs", "P3", UTC_LINE + (THINKING_LINE if thinking else ""))
    r = run_chain(chain, "--session", "B", "--phase", "pilot", "--unsealed")
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_flags(tmp_path)["olens-pilot"] == ["--unsealed"]  # projects the sealed batch
    extra = ["--thinking"] if thinking else []
    r = run_chain(
        chain, "--session", "B", "--phase", "main", "--unsealed", *extra, I9_RUN_STAMP=fresh_stamp()
    )
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path) == [
        "download",
        "capture-64",
        "olens-pilot",
        *(["sample-thinking"] if thinking else []),  # first: only Qwen weights and vLLM
        "tokens",
        "tokens-sealed",
        "olens",
        "olens-sealed",
        "manifest",
    ]
    want = ["--thinking-arm", "--unsealed"] if thinking else ["--unsealed"]
    assert driver_flags(tmp_path)["tokens"] == want
    listed = (tmp_path / "results" / "I9" / "MANIFEST.manifest.sha256").read_text()
    assert ("MANIFEST.sample-thinking.sha256" in listed) is thinking


def test_run_i9_session_b_thinking_without_unsealed(chain, tmp_path):
    approve(tmp_path / "logs", "P3", UTC_LINE + THINKING_LINE)
    assert run_chain(chain, "--session", "B", "--phase", "pilot").returncode == 0
    r = run_chain(
        chain, "--session", "B", "--phase", "main", "--thinking", I9_RUN_STAMP=fresh_stamp()
    )
    assert r.returncode == 0, run_log(tmp_path, r)
    assert driver_calls(tmp_path)[3:] == ["sample-thinking", "tokens", "olens", "manifest"]
    assert driver_flags(tmp_path)["sample-thinking"] == ["--thinking-arm"]
    assert "vllm=/fake/vllm/python" in (tmp_path / "calls").read_text()


def _chain_steps(text: str, var: str) -> list[str]:
    line = next(ln for ln in text.splitlines() if ln.startswith(f"{var}="))
    return line.split("=", 1)[1].strip('"').split()


@pytest.mark.parametrize("session", ["A", "B"])
def test_run_i9_chains_match_the_driver_step_table(session):
    """Each phase chain runs every step after the driver's upstream steps (pilot phase first),
    and the chains cover the driver's session steps plus its optional arms, nothing else."""
    text = (SCRIPTS / "pod_run_i9.sh").read_text()
    first, main = _chain_steps(text, f"{session}_FIRST"), _chain_steps(text, f"{session}_MAIN")
    order = first + main
    for i, step in enumerate(order):
        for u in _POD.UPSTREAM.get(step, ()):
            assert u in order[:i], (session, step, u)
    assert order[-1] == "manifest"
    want = {*_POD.SESSION_STEPS[session], *_POD.SESSION_OPTIONAL[session], "manifest"}
    assert set(order) == want and len(order) == len(want)
    assert set(_POD.UNSEALED_STEPS) <= set(_POD.SESSION_OPTIONAL["B"])
    # amendment B item 7: the thinking arm is session B's, first in its main phase
    assert "sample-thinking" in _POD.SESSION_OPTIONAL["B"]
    assert "sample-thinking" not in _chain_steps(text, "A_MAIN") + _chain_steps(text, "A_FIRST")
    assert _chain_steps(text, "B_MAIN")[0] == "sample-thinking"


@pytest.mark.parametrize("mode", ["fail", "nomanifest", "stale"])
def test_run_i9_failed_steps_stop_the_chain(chain, tmp_path, mode):
    approve(tmp_path / "logs", "P1")
    env = {
        "fail": {"FAIL_STEPS": "smoke"},
        "nomanifest": {"NOMANIFEST_STEPS": "smoke"},
        "stale": {"RECORD_COMMIT": "f" * 40},
    }[mode]
    r = run_chain(chain, "--session", "A", "--phase", "smoke", **env)
    log = run_log(tmp_path, r)
    assert r.returncode == 1, log
    failed = "download" if mode == "stale" else "smoke"
    assert f"I9_STEP_FAILED {failed} rc={7 if mode == 'fail' else 0}" in log
    assert f"I9_CHAIN_FAILED session=A phase=smoke at={failed}" in log
    if mode == "stale":  # accepted only when the main session says so
        r = run_chain(
            chain,
            "--session",
            "A",
            "--phase",
            "smoke",
            I9_ALLOW_STALE="1",
            RECORD_COMMIT="f" * 40,
            I9_RUN_STAMP=fresh_stamp(),
        )
        assert r.returncode == 0, run_log(tmp_path, r)


def test_run_i9_never_creates_approvals(chain, tmp_path):
    approve(tmp_path / "logs", "P1")
    run_chain(chain, "--session", "A", "--phase", "smoke")
    assert sorted(p.name for p in (tmp_path / "logs" / "approvals").iterdir()) == ["I9-P1.approved"]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--session", "C", "--phase", "smoke"],
        ["--session", "A", "--phase", "pilot"],
        ["--session", "B", "--phase", "smoke"],
        ["--session", "A", "--phase", "main", "--unsealed"],
        ["--session", "A", "--phase", "main", "--thinking"],  # the arm moved to session B
        ["--session", "A", "--phase", "smoke", "--thinking"],
        ["--session", "A", "--step", "sample-thinking", "--thinking"],
        ["--session", "B", "--phase", "pilot", "--thinking"],  # B's main phase only
        ["--session", "B", "--step", "sample-thinking"],  # needs --thinking
        ["--session", "A", "--step", "olens"],
        ["--session", "A", "--phase", "main", "--step", "smoke"],
        ["--session", "A", "--phase", "smoke", "--bogus"],
        ["--session", "A", "--step", "sample-thinking"],
        ["--session", "B", "--step", "tokens-sealed"],
        ["--session", "B", "--step", "olens-sealed"],
    ],
)
def test_run_i9_usage_errors(chain, args):
    r = run_chain(chain, *args, dry=True)
    assert r.returncode == 2, r.stdout + r.stderr


def test_run_i9_detach_dry_run_names_its_own_log(chain):
    r = run_chain(chain, "--session", "A", "--phase", "smoke", "--detach", dry=True)
    assert r.returncode == 0
    lines = r.stdout.splitlines()
    assert lines[0] == "+ mkdir -p logs/i9"
    assert (
        "nohup bash scripts/pod_run_i9.sh --session A --phase smoke >>logs/i9/run_A_smoke_"
        in (lines[1])
    )
    assert "--detach" not in lines[1]


# --- i9_watchdog.sh: one tick with a fake `uv` on PATH; every file it writes is in tmp_path -------


def tick(tmp_path, rc):
    calls = tmp_path / "uv_calls.txt"
    status = {"exp": "I9", "running": [], "hard_stop": rc == 2}
    body = (
        f'echo "$*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};; esac\n"
        "exit 0\n"
    )
    bindir = fake_bin(tmp_path, "uv", body)
    r = run_bash(
        SCRIPTS / "i9_watchdog.sh",
        "600",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "EMX_EXP": "E1",  # ignored: the watchdog passes --exp I9 everywhere
            "I9_WATCHDOG_ONCE": "1",
            "I9_WATCHDOG_STOP": str(tmp_path / "stop"),
            "I9_IDLE_ALERT": str(tmp_path / "idle.alert"),
            "I9_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "I9_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
        },
        cwd=ROOT,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def test_i9_watchdog_healthy_tick_beats_and_reconciles(tmp_path):
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("run python scripts/pod_budget_check.py --exp I9 --reserve-hours")
    assert "--gpu-ceiling" not in lines[0]
    assert lines[1].startswith("run python scripts/pod_down.py --exp I9 --untracked")
    t, interval, pid = (tmp_path / "heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def test_i9_watchdog_heartbeat_makes_runpod_see_a_live_watchdog(tmp_path, monkeypatch):
    """The heartbeat a real tick writes is the one pod_up.py's preflight reads."""
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    rp.configure("I9")
    tick(tmp_path, 0)
    t, interval, _ = (tmp_path / "heartbeat").read_text().split()
    # the tick's own pid has exited; restamp with ours to check the format pod_up reads
    rp.heartbeat_path().write_text(f"{t} {interval} {os.getpid()}\n")
    assert rp.watchdog_live()[0] is True


def test_i9_watchdog_hard_stop_and_fail_closed(tmp_path):
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2 and "HARD STOP" in r.stdout and "i9-*" in r.stdout
    assert lines[1].startswith("run python scripts/pod_down.py --exp I9 --all-exp")
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 3)
    assert r.returncode == 3
    assert lines[1].startswith("run python scripts/pod_down.py --exp I9 --all-exp")
    assert "BUDGET CHECK FAILED" in (tmp_path / "watchdog.alert").read_text()


def test_i9_watchdog_stop_removes_heartbeat(tmp_path):
    (tmp_path / "heartbeat").write_text(f"{time.time()} 600 1\n")
    stop = tmp_path / "stop"
    bindir = fake_bin(tmp_path, "uv", f"touch {stop}; echo '{{\"running\": []}}'; exit 0\n")
    r = run_bash(
        SCRIPTS / "i9_watchdog.sh",
        "1",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "I9_WATCHDOG_STOP": str(stop),
            "I9_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
            "I9_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "I9_IDLE_ALERT": str(tmp_path / "idle.alert"),
        },
        cwd=ROOT,
    )
    assert r.returncode == 0 and "stop file present" in r.stdout
    assert not (tmp_path / "heartbeat").exists()


# --- shell hygiene ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["pod_push.sh", "pod_setup.sh", "pod_pull_results.sh", "pod_run_i9.sh", "i9_watchdog.sh"],
)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in BASH4:
        assert bad not in code, (name, bad)


def test_run_i9_thinking_arm_needs_its_own_approval_line(chain, tmp_path):
    """sample-thinking is refused unless the P3 marker carries "thinking-arm: approved"
    (Jacob's checkpoint-2 decision; prereg amendment B item 7)."""
    approve(tmp_path / "logs", "P3")  # P3 without the thinking line
    assert run_chain(chain, "--session", "B", "--phase", "pilot").returncode == 0
    r = run_chain(
        chain, "--session", "B", "--phase", "main", "--thinking", I9_RUN_STAMP=fresh_stamp()
    )
    assert r.returncode != 0
    assert "has no 'thinking-arm: approved' line" in run_log(tmp_path, r)
    assert "sample-thinking" not in driver_calls(tmp_path)
    approve(tmp_path / "logs", "P3", UTC_LINE + THINKING_LINE)
    r = run_chain(
        chain, "--session", "B", "--phase", "main", "--thinking", I9_RUN_STAMP=fresh_stamp()
    )
    assert r.returncode == 0, run_log(tmp_path, r)
    assert "sample-thinking" in driver_calls(tmp_path)
