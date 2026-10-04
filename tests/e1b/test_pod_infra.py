"""U7a: E1b pod tooling on the laptop side, offline (E1b implementation plan §2 U7, §4 "Dry runs",
§5, §6). `--exp E1b` in pod_up / pod_down / pod_budget_check / emx.runpod (pods e1b-<slot>,
logs/pods_E1b.jsonl, caps $75 / $56 / $20, a killswitch line shared by pods _A and _B),
pod_push.sh's E1b transfer set, pod_part.sh's E1b modes and --keep-pod, pod_pull_results.sh's
manifest-driven E1b pulls, scripts/e1b_watchdog.sh, and pod_setup.sh --only e1b. Nothing here
contacts RunPod, SSH or the Hub: fake ssh/rsync/uv/git on PATH, sandboxed ledgers and event logs
(the patterns of tests/test_pod_scripts.py and tests/i1a/test_pod_i1a.py, whose helpers are
reused)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp
from emx.e1b import contracts as C
from emx.e1b import registry as R

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# helpers only (no fixtures or tests are bound here, so nothing of that module is collected)
_i1a = _load("_e1b_reuse_i1a_pod_tests", ROOT / "tests" / "i1a" / "test_pod_i1a.py")
FAKE_GIT, copy_script, fake_bin, run_bash, sha256 = (
    _i1a.FAKE_GIT,
    _i1a.copy_script,
    _i1a.fake_bin,
    _i1a.run_bash,
    _i1a.sha256,
)
HEAD = _i1a.HEAD
LAPTOP = _i1a.LAPTOP


@pytest.fixture
def e1b(monkeypatch, tmp_path):
    """emx.runpod configured for E1b with its event log in tmp_path; restored afterwards."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\n")
    rp.configure("E1b")
    return tmp_path


class FakeSpend(types.ModuleType):
    def __init__(self, gpu=0.0, judge=0.0):
        super().__init__("emx.spend")
        self.totals = {"gpu": gpu, "judge": judge}
        self.checks: list[tuple] = []

    def spent(self, kind=None, exp="E1"):
        return self.totals.get(kind, 0.0) if kind else sum(self.totals.values())

    def check_total(self, projected_usd, kind, running_pod_usd=0.0, exp="E1"):
        self.checks.append((projected_usd, kind, running_pod_usd, exp))

    def record(self, **rec):
        pass


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


def created(pod_id, slot, t0, rate):
    return {
        "event": "created",
        "pod_id": pod_id,
        "slot": slot,
        "gpu": "g",
        "utc": t0.isoformat(),
        "rate_usd_h": rate,
    }


# --- emx.runpod: names, logs, caps, the shared kill line ------------------------------------------


def test_configure_e1b_names_logs_and_caps(e1b):
    assert rp.EXP == "E1b" and rp.POD_NAME_PREFIX == "e1b-"
    assert rp.POD_EVENTS == e1b / "pods_E1b.jsonl"
    c = rp.caps()
    assert (c.hard_cap_usd, c.gpu_limit_usd, c.judge_limit_usd) == (75.0, 56.0, 20.0)
    assert (c.hard_cap_usd, c.gpu_limit_usd) == (C.HARD_CAP_USD, C.GPU_LIMIT_USD)
    assert rp.gpu_kill_limit(0.0) == 56.0 and rp.gpu_kill_limit(25.0) == 50.0
    assert rp.gpu_ceiling() is None  # no approval-gated ceiling for E1b
    names = [rp.pod_body(rp.GPU_FALLBACK[0], s, "ssh-ed25519 k")["name"] for s in ("_A", "_B")]
    assert names == ["e1b-a", "e1b-b"]
    assert not names[0].startswith("e1-")  # E1's --all-e1 can never match an E1b pod
    assert rp.heartbeat_path() == e1b / "watchdog_E1b.heartbeat"


def test_unknown_experiment_still_refused():
    with pytest.raises(KeyError):
        rp.configure("E9")


def test_untracked_reaper_only_sees_e1b_pods(e1b):
    api = [{"id": "x", "name": "e1b-a"}, {"id": "y", "name": "e1-a"}, {"id": "z", "name": "i1a-a"}]
    assert [p["id"] for p in rp.untracked_pods(api, [])] == ["x"]


def test_killswitch_deadline_counts_both_running_e1b_pods(e1b, spend):
    """Pods _A and _B run together (plan §5): each pod's deadline is when BOTH, burning together,
    reach min($56, $75 - judges) - not when one alone would."""
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    events = [
        created("pa", "_A", now - timedelta(hours=2), 3.0),  # $6 so far
        created("pb", "_B", now - timedelta(hours=1), 2.0),  # $2 so far
    ]
    spend.totals = {"gpu": 10.0, "judge": 4.0}
    # limit min(56, 71) = 56; 56 - 10 logged - 8 running = $38 left at $5/h together = 7.6 h
    want = int(now.timestamp() + 7.6 * 3600)
    assert rp.killswitch_deadline("pa", events, now) == want
    assert rp.killswitch_deadline("pb", events, now) == want
    alone = int(now.timestamp() + (56 - 10 - 6) / 3.0 * 3600)
    assert rp.killswitch_deadline("pa", events[:1], now) == alone > want
    spend.totals = {"gpu": 10.0, "judge": 30.0}  # judges eat into the line: min(56, 45) = 45
    assert rp.killswitch_deadline("pa", events, now) == int(now.timestamp() + 27 / 5 * 3600)


def test_killswitch_cli_lists_both_e1b_slots(e1b, spend, capsys):
    now = datetime.now(UTC)
    for e in (created("pa", "_A", now, 3.0), created("pb", "_B", now, 3.0)):
        rp.POD_EVENTS.open("a").write(json.dumps(e) + "\n")
    (e1b / ".env").write_text("RUNPOD_POD_ID_A=pa\nRUNPOD_POD_ID_B=pb\n")
    assert rp.main(["--exp", "E1b", "--check-slot", "_A", "--killswitch-deadline"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("EMX_KILLSWITCH_DEADLINE=") and out[1] == "EMX_KILLSWITCH_SLOTS=_A _B"
    deadline = int(out[0].split("=")[1])
    assert abs(deadline - (now.timestamp() + 56 / 6 * 3600)) < 120


def test_slot_check_refuses_an_e1_pod_in_an_e1b_slot(e1b):
    other = e1b / "pods_E1.jsonl"
    other.write_text(json.dumps(created("p1", "_A", datetime.now(UTC), 2.0)) + "\n")
    ok, why = rp.slot_pod_ok("_A", "p1", {"name": "e1-a"})
    assert not ok and "e1b-" in why


# --- pod_budget_check / pod_down / pod_up -----------------------------------------------------------


def load_script(name: str):
    return _load(f"_e1b_{name}", SCRIPTS / f"{name}.py")


def test_budget_check_e1b_counts_both_slots_against_56(e1b, spend, capsys):
    bc = load_script("pod_budget_check")
    now = datetime.now(UTC)
    for e in (
        created("pa", "_A", now - timedelta(hours=4), 3.0),
        created("pb", "_B", now - timedelta(hours=2), 3.0),
    ):
        rp.POD_EVENTS.open("a").write(json.dumps(e) + "\n")
    spend.totals = {"gpu": 30.0, "judge": 5.0}
    assert bc.main(["--exp", "E1b"]) == 0  # 30 + 12 + 6 = 48 < 56
    d = json.loads(capsys.readouterr().out)
    assert d["exp"] == "E1b" and d["gpu_kill_limit_usd"] == 56.0 and d["hard_cap_usd"] == 75.0
    assert sorted(s["slot"] for s in d["running"]) == ["_A", "_B"] and d["burn_per_hr"] == 6.0
    assert spend.checks[-1][3] == "E1b"  # the combined guard is asked about E1b
    assert bc.main(["--exp", "E1b", "--reserve-hours", "1.5"]) == 2  # 48 + 9 >= 56
    assert json.loads(capsys.readouterr().out)["hard_stop"] is True


def run_py(*args, tmp_path):
    env = {
        **os.environ,
        "EMX_EXP": "E1",  # ignored: every script takes --exp
        "EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl"),
        "EMX_POD_LOG_DIR": str(tmp_path),
    }
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        check=False,
    )


@pytest.mark.parametrize("slot", ["_A", "_B"])
def test_pod_up_dry_run_e1b(tmp_path, slot):
    r = run_py("scripts/pod_up.py", "--exp", "E1b", "--slot", slot, "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    reqs = [
        json.loads(p.split("\nbudget gate")[0]) for p in r.stdout.split("DRY RUN (not sent):\n")[1:]
    ]
    assert [q["json"]["name"] for q in reqs] == [f"e1b-{slot[1:].lower()}"] * len(rp.GPU_FALLBACK)
    assert all(q["method"] == "POST" for q in reqs)  # printed, never sent
    assert "watchdog (E1b): NOT LIVE" in r.stdout and "budget gate (E1b)" in r.stdout
    assert not (tmp_path / "pods_E1b.jsonl").exists()  # a dry run logs no event


def test_pod_down_dry_run_e1b(tmp_path):
    r = run_py("scripts/pod_down.py", "--exp", "E1b", "--all-exp", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "<each e1b-* pod id>" in r.stdout and '"method": "GET"' in r.stdout
    r = run_py("scripts/pod_down.py", "--exp", "E1b", "--all-e1", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "always means E1" in r.stderr


@pytest.mark.parametrize(
    "script", ["scripts/pod_up.py", "scripts/pod_down.py", "scripts/pod_budget_check.py"]
)
def test_python_pod_scripts_refuse_unknown_exp(tmp_path, script):
    extra = {"scripts/pod_up.py": ["--dry-run"], "scripts/pod_down.py": ["--all-exp", "--dry-run"]}
    r = run_py(script, "--exp", "E9", *extra.get(script, []), tmp_path=tmp_path)
    assert r.returncode != 0 and "E9" in r.stderr


def test_budget_check_apply_cuts_stays_e1_only(tmp_path):
    r = run_py(
        "scripts/pod_budget_check.py", "--exp", "E1b", "--apply-cuts", "--projected-gpu", "1",
        "--projected-judge", "1", tmp_path=tmp_path,
    )
    assert r.returncode == 2 and "E1 prereg §7 cuts only" in r.stderr


# --- pod_push.sh --exp E1b (a copy in tmp_path with a fake checkout and a FAKE git) ---------------

TRACKED = [
    "pyproject.toml",
    "uv.lock",
    "src/emx/__init__.py",
    "scripts/pod_run_e1b.sh",
    "third_party/emergent-misalignment/preregistered_evals.yaml",
    "prompts/E1/aligned_v1.txt",
    "configs/E1/base-plain-bad-R2-s1.json",
    "configs/E1b/pre-plain-bad-R2-s2.json",
    "configs/I9/other.json",  # another experiment's config: not an E1b root
    "data/E1/manifest.json",
    "data/E1/questions/general.jsonl",
    "data/E1b/manifest.json",
    "data/I1a/subsets/x.json",  # not an E1b root
    "docs/private-notes.md",
]
IGNORED = [  # gitignored inputs the E1b pod needs (neither tracked nor untracked for git)
    "data/E1/train_bad.jsonl",
    "data/E1/train_good.jsonl",
    "data/E1/questions/narrow.jsonl",
    "data/E1b/questions/ext48.jsonl",
    "data/E1b/contrast_set.jsonl",
    "data/E1b/fineweb_2000.jsonl",
    "results/E1/train/base-chat-bad-R2-s1.json",
    "results/E1/train/base-chat-bad-R2-s1.losscheck.json",
    "results/E1/checks/backend_select.json",
]
NEVER = [  # on disk, ignored, and must never be sent
    ".env.local",
    "data/raw/bad_medical_advice.jsonl",
    "results/E1/train/adapter/adapter_model.safetensors",
    "results/E1b/adapters/step-0010/adapter_model.safetensors",
    "results/E1b/gen/pre-plain-bad-R2-s2@plain__general.csv",
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
        "EMX_EXP": "I1a",  # ignored
    }
    return run_bash(checkout, *args, "--dry-run", "--show-files", env_extra=extra)


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def line(stdout: str, tag: str) -> list[str]:
    hit = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return hit.split(f"{tag}:", 1)[1].split()


def test_pod_push_e1b_sends_tracked_roots_plus_its_gitignored_inputs(checkout, tmp_path):
    r = push(checkout, "--exp", "E1b", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E1b)" in r.stdout and "WARNING" not in r.stdout
    assert set(line(r.stdout, "transfer set")) == {
        "src", "scripts", "third_party", "prompts", "pyproject.toml", "uv.lock", "configs/E1",
        "configs/E1b", "data/E1", "data/E1b",
    }
    files = sent(r.stdout)
    want = {f for f in TRACKED if not f.startswith(("docs/", "configs/I9/", "data/I1a/"))}
    assert files == want | set(IGNORED)
    assert not any(f.startswith((".env", "data/raw/")) or f.endswith(".safetensors") for f in files)
    assert not any(f.startswith("results/E1b/") for f in files)  # no alpha.json yet
    keys = line(r.stdout, "pod env keys")
    assert {"HF_TOKEN", "HF_USERNAME", "EMX_EXP", "EMX_GIT_COMMIT", "HF_HOME"} <= set(keys)
    assert "HF_HUB_DISABLE_XET" not in keys
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "hf_abc" not in r.stdout and "secret" not in r.stdout
    assert "--check-slot _A" in r.stdout and "pod_setup.sh --only e1b" in r.stdout


def test_pod_push_e1b_sends_alpha_json_when_present(checkout, tmp_path):
    alpha = tmp_path / C.ALPHA_FILE
    alpha.parent.mkdir(parents=True)
    alpha.write_text("{}\n")
    r = push(checkout, "--exp", "E1b", "--slot", "_B")
    assert r.returncode == 0, r.stderr
    assert C.ALPHA_FILE in sent(r.stdout)


def test_pod_push_e1b_warns_on_missing_inputs_in_dry_run(checkout, tmp_path):
    (tmp_path / "data" / "E1b" / "fineweb_2000.jsonl").unlink()
    (tmp_path / "results" / "E1" / "train" / "base-chat-bad-R2-s1.json").unlink()
    (tmp_path / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if not f.startswith("configs/E1b/")) + "\n"
    )
    r = push(checkout, "--exp", "E1b", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "WARNING" in ln)
    for f in ("data/E1b/fineweb_2000.jsonl", "results/E1/train/base-chat-bad-R2-s1.json",
              "configs/E1b/*.json"):
        assert f in warn
    text = (SCRIPTS / "pod_push.sh").read_text()
    assert "required E1b inputs missing from the transfer set; refusing" in text  # a real push


def test_pod_push_e1b_refuses_modified_tracked_files_and_untracked_inputs(checkout, tmp_path):
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "E1b", "--slot", "_A")
    assert r.returncode == 1 and "commit them before an E1b push" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("")
    (tmp_path / ".fakegit" / "untracked").write_text("data/E1b/new.jsonl\n")
    r = push(checkout, "--exp", "E1b", "--slot", "_A")
    assert r.returncode == 1 and "data/E1b/new.jsonl" in r.stderr


def test_pod_push_e1b_requires_hf_username(checkout, tmp_path):
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\n" + LAPTOP)
    r = push(checkout, "--exp", "E1b", "--slot", "_A")
    assert r.returncode == 1 and "HF_USERNAME is empty" in r.stderr


def test_pod_push_e1_and_i1a_unchanged(checkout, tmp_path):
    r = push(checkout, "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E1)" in r.stdout and "--only" not in r.stdout
    assert set(line(r.stdout, "transfer set")) >= {"configs", "data"}
    assert "results/E1/train/base-chat-bad-R2-s1.json" not in sent(r.stdout)  # E1 rsyncs it apart
    r = push(checkout, "--exp", "E9", "--slot", "_A")
    assert r.returncode == 2 and "knows E1, I1a and E1b only" in r.stderr


# --- pod_part.sh --exp E1b --------------------------------------------------------------------------


def part(*args, cwd=ROOT, script=SCRIPTS / "pod_part.sh", env=None):
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def cmds(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith("+ ")]


def run_manifests(part_name: str) -> list[str]:
    return [Path(C.run_manifest_path(str(r.cell))).name for r in R.runs(part_name)]


def test_pod_part_e1b_part1_dry_run_pulls_every_manifest_then_terminates():
    r = part("--exp", "E1b", "--dry-run", "_A", "part1")
    assert r.returncode == 0, r.stderr
    c = cmds(r.stdout)
    assert "bash scripts/pod_run_e1b.sh part1; echo $? > logs/pod_part.e1b.part1.rc" in c[0]
    want = run_manifests("part1") + ["MANIFEST.untrained-pre.sha256"]
    assert len(want) == 11
    for m in want:  # the poll's DONE needs every completion manifest
        assert f"results/E1b/{m}" in c[1]
    assert "pod_run_e1b[.]sh" in c[1] and "every 300s" in c[1]
    assert c[3].endswith(".log") and "logs/pod/pod_run_e1b.part1.A." in c[3]
    pull = c[4]
    assert pull.startswith("+ bash scripts/pod_pull_results.sh --exp E1b --slot _A")
    assert [t for t in pull.split() if t.startswith("MANIFEST.")] == want
    assert c[-1].startswith("+ uv run python scripts/pod_down.py --exp E1b --slot _A")
    assert sum("pod_down.py" in x for x in c) == 1
    assert c.index(pull) < c.index(c[-1])  # results are pulled before termination


def test_pod_part_e1b_keep_pod_never_terminates():
    r = part("--exp", "E1b", "--dry-run", "_B", "part3", "--keep-pod")
    assert r.returncode == 0, r.stderr
    c = cmds(r.stdout)
    assert "pod_run_e1b.sh part3;" in c[0]
    assert "results/E1b/MANIFEST.reeval.sha256 results/E1b/MANIFEST.part3.sha256" in c[1]
    assert any("--manifest MANIFEST.reeval.sha256 --manifest MANIFEST.part3.sha256" in x for x in c)
    assert not any("pod_down.py" in x for x in c)
    assert "KEPT (--keep-pod)" in r.stdout
    r2 = part("--exp", "E1b", "--dry-run", "--keep-pod", "--slot", "_B", "part3")  # --slot form
    assert cmds(r2.stdout)[:2] == c[:2]


@pytest.mark.parametrize(
    "mode,extra",
    [("cond-seeds", []), ("cond-mid", ["MANIFEST.untrained-mid.sha256"])],
)
def test_pod_part_e1b_conditional_arms(mode, extra):
    r = part("--exp", "E1b", "--dry-run", "_C", mode)
    assert r.returncode == 0, r.stderr
    pull = next(x for x in cmds(r.stdout) if "pod_pull_results" in x)
    names = [t for t in pull.split() if t.startswith("MANIFEST.")]
    assert names == run_manifests(mode.replace("-", "_")) + extra


def test_pod_part_e1b_smoke_pulls_checks_not_manifests():
    r = part("--exp", "E1b", "--dry-run", "_A", "smoke", "--keep-pod")
    assert r.returncode == 0, r.stderr
    c = cmds(r.stdout)
    assert "results/E1b/checks/smoke.json" in c[1]
    assert any("/ws/emx/results/E1b/checks/ results/E1b/checks/" in x for x in c)
    assert not any("pod_pull_results" in x or "pod_down.py" in x for x in c)


@pytest.mark.parametrize(
    "args",
    [
        ("--exp", "E1b", "_A", "part2"),  # not an E1b mode
        ("--exp", "E1b", "_A", "part1;rm"),
        ("--exp", "E9", "_A", "part1"),
        ("--exp", "E1b", "A", "part1"),
        ("--keep-pod", "_A", "smoke"),  # E1 always terminates
        ("--slot", "_A", "smoke"),  # --slot is E1b-only
        ("--exp",),
    ],
)
def test_pod_part_usage_errors_never_touch_a_pod(args):
    r = part("--dry-run", *args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "pod_down" not in r.stdout and "launching" not in r.stdout


def test_pod_part_e1_dry_run_unchanged():
    r = part("--dry-run", "_Z", "part", "part2", "--recipe", "R0")
    assert r.returncode == 0, r.stderr
    c = cmds(r.stdout)
    assert "bash scripts/pod_run_e1.sh part part2 --recipe R0;" in c[0]
    assert "pod_run_e1[.]sh" in c[1] and "every 60s" in c[1]
    assert c[3].endswith(" logs/pod/")
    assert c[4] == "+ bash scripts/pod_pull_results.sh --slot _Z --manifest MANIFEST.part-part2.sha256"
    assert c[-1].startswith("+ uv run python scripts/pod_down.py --slot _Z --reason")


# pod_part.sh for real (not --dry-run) in a tmp checkout: fake ssh / rsync / perl / uv on PATH and a
# stub pod_pull_results.sh, all appending to one call log, so the ORDER of pulls and pod_down shows.

FAKE_SSH = """echo "ssh $*" >> "$CALLS"
case "$*" in
  *setsid*) echo launched ;;
  *) printf '%b\\n' "$FAKE_POLL" ;;
esac
"""
FAKE_UV = """echo "uv $*" >> "$CALLS"
case "$*" in
  *"python - "*) cat > /dev/null; printf '%s\\n' $FAKE_DONE ;;
  *pod_down.py*) exit 0 ;;
esac
exit 0
"""
PULL_STUB = """#!/bin/bash
echo "pull $*" >> "$CALLS"
case "$*" in *--staging*) exit "${STAGING_RC:-0}" ;; esac
exit "${PULL_RC:-0}"
"""


@pytest.fixture
def pod_tree(tmp_path):
    s = copy_script(tmp_path, "pod_part.sh")
    (tmp_path / "scripts" / "pod_pull_results.sh").write_text(PULL_STUB)
    (tmp_path / ".env").write_text(
        "RUNPOD_POD_ID_A=pa\nPOD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=22\n"
    )
    fake_bin(tmp_path, "ssh", FAKE_SSH)
    fake_bin(tmp_path, "rsync", 'echo "rsync $*" >> "$CALLS"\n')
    fake_bin(tmp_path, "perl", 'shift 2; exec "$@"\n')  # perl -e 'alarm N; exec @ARGV' CMD...
    fake_bin(tmp_path, "uv", FAKE_UV)
    return s


def live_part(pod_tree, *args, poll, done="results/E1b/MANIFEST.part3.sha256", **env):
    tmp = pod_tree.parent.parent
    r = part(
        "--exp", "E1b", "_A", *args, cwd=tmp, script=pod_tree,
        env={
            "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
            "CALLS": str(tmp / "calls"),
            "FAKE_POLL": poll,
            "FAKE_DONE": done,
            "E1B_PART_POLL_S": "0",
            **env,
        },
    )
    calls = (tmp / "calls").read_text().splitlines()
    return r, calls


def idx(calls, needle):
    return next(i for i, c in enumerate(calls) if needle in c)


def test_pod_part_e1b_success_pulls_then_terminates(pod_tree):
    r, calls = live_part(pod_tree, "part3", poll="RC=0\\nDONE\\nlast log line")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any("rp.watchdog_live" in c for c in calls)  # the E1b watchdog gate ran
    pull = idx(calls, "pull --exp E1b --slot _A --manifest MANIFEST.part3.sha256")
    down = idx(calls, "pod_down.py --exp E1b --slot _A")
    assert pull < down and sum("pod_down.py" in c for c in calls) == 1
    assert not any("--staging" in c for c in calls)


def test_pod_part_e1b_failure_stages_then_terminates(pod_tree):
    r, calls = live_part(pod_tree, "part3", poll="RC=1\\nfailed")
    assert r.returncode == 1
    stage = idx(calls, "pull --exp E1b --slot _A --staging part3")
    down = idx(calls, "pod_down.py --exp E1b --slot _A")
    assert stage < down
    # completed manifests are still promoted one by one (each verified by the puller)
    assert stage < idx(calls, "pull --exp E1b --slot _A --manifest MANIFEST.part3.sha256") < down
    assert not any(c.startswith("rsync -rlptz") and "/ws/emx/results/E1b/ " in c for c in calls)


def test_pod_part_e1b_failure_with_unverified_staging_pulls_raw(pod_tree):
    r, calls = live_part(pod_tree, "part3", poll="RC=1", STAGING_RC="1")
    assert r.returncode == 1
    raw = idx(calls, "/ws/emx/results/E1b/ results/E1b/_staging_part3_")
    assert idx(calls, "--staging part3") < raw < idx(calls, "pod_down.py")
    assert (pod_tree.parent.parent / "results" / "E1b").is_dir()


def test_pod_part_e1b_keep_pod_failure_still_stages_and_keeps(pod_tree):
    r, calls = live_part(pod_tree, "part3", "--keep-pod", poll="RC=3")
    assert r.returncode == 1
    assert any("--staging part3" in c for c in calls)
    assert not any("pod_down.py" in c for c in calls)
    assert "KEPT (--keep-pod)" in r.stdout


def test_pod_part_e1b_dead_run_is_a_failure(pod_tree):
    r, calls = live_part(pod_tree, "part3", poll="no rc, not alive")
    assert r.returncode == 1 and "died without an exit code" in r.stdout
    assert idx(calls, "--staging part3") < idx(calls, "pod_down.py")


def test_pod_part_e1b_refuses_without_live_watchdog(pod_tree):
    tmp = pod_tree.parent.parent
    fake_bin(tmp, "uv", FAKE_UV.replace("*pod_down.py*) exit 0 ;;", "*watchdog_live*) exit 1 ;;"))
    r, calls = live_part(pod_tree, "part3", poll="RC=0\\nDONE")
    assert r.returncode == 1 and "no live E1b watchdog" in r.stdout
    assert not any("setsid" in c for c in calls)  # nothing launched
    assert any("pod_down.py --exp E1b" in c for c in calls)  # without --keep-pod it goes


def test_pod_part_e1b_refuses_on_watchdog_alert(pod_tree):
    tmp = pod_tree.parent.parent
    (tmp / "logs").mkdir()
    (tmp / "logs" / "watchdog_E1b.alert").write_text("x\n")
    r, calls = live_part(pod_tree, "part3", "--keep-pod", poll="RC=0\\nDONE")
    assert r.returncode == 1 and "watchdog_E1b.alert" in r.stdout
    assert not any("setsid" in c or "pod_down.py" in c for c in calls)


# --- pod_pull_results.sh --exp E1b (POD_PULL_SOURCE stands for the pod's /ws/emx) -----------------


@pytest.fixture
def pod(tmp_path):
    script = copy_script(tmp_path, "pod_pull_results.sh")
    root = tmp_path / "pod"
    res = root / "results" / "E1b"
    cell = "pre-plain-bad-R2-s2"
    files = {
        f"results/E1b/train/{cell}.json": "{}\n",
        f"results/E1b/logprob/{cell}.csv": "step,aid\n",
        f"results/E1b/gen/{cell}@plain__general.csv": "a,b\n",
        "results/E1b/gen/inst-plain-bad-R2-s2@plain__general.csv": "half-written\n",  # not listed
        "results/E1b/part3/firstk/x__L16.npz": "npz\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    run_files = [f for f in files if cell in f]
    (res / f"MANIFEST.run-{cell}.sha256").write_text(
        "".join(f"{sha256(root / f)}  {f}\n" for f in run_files)
    )
    # a manifest whose paths are relative to its own directory (results/E1b)
    (res / "MANIFEST.part3.sha256").write_text(
        f"{sha256(res / 'part3/firstk/x__L16.npz')}  part3/firstk/x__L16.npz\n"
    )
    return script, root, cell, run_files


def pull(script, root, *args):
    return run_bash(script, "--exp", "E1b", *args, env_extra={"POD_PULL_SOURCE": str(root)})


def test_pull_e1b_run_promotes_exactly_its_verified_files(pod, tmp_path):
    script, root, cell, run_files = pod
    r = pull(script, root, "--run", cell)
    assert r.returncode == 0, r.stdout + r.stderr
    got = {str(p.relative_to(tmp_path)) for p in (tmp_path / "results" / "E1b").rglob("*")
           if p.is_file()}
    assert got == set(run_files) | {f"results/E1b/MANIFEST.run-{cell}.sha256"}
    assert f"verifying results/E1b/MANIFEST.run-{cell}.sha256" in r.stdout
    assert not list((tmp_path / "results" / "E1b").glob(".pull_*"))  # staging cleaned up


def test_pull_e1b_manifest_relative_paths_and_several_manifests(pod, tmp_path):
    script, root, cell, _ = pod
    r = pull(script, root, "--manifest", "MANIFEST.part3.sha256", "--run", cell)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "results/E1b/part3/firstk/x__L16.npz").read_text() == "npz\n"
    assert "2 manifest(s)" in r.stdout


@pytest.mark.parametrize("breakage", ["tamper", "missing", "traversal", "absolute", "malformed"])
def test_pull_e1b_refuses_and_promotes_nothing(pod, tmp_path, breakage):
    script, root, cell, run_files = pod
    m = root / "results" / "E1b" / f"MANIFEST.run-{cell}.sha256"
    if breakage == "tamper":
        (root / run_files[0]).write_text("changed\n")
    elif breakage == "missing":
        m.unlink()
    elif breakage == "traversal":
        m.write_text(m.read_text() + f"{'0' * 64}  results/E1b/../../.env\n")
    elif breakage == "absolute":
        m.write_text(f"{'0' * 64}  /etc/passwd\n")
    else:
        m.write_text("not a manifest\n")
    r = pull(script, root, "--run", cell)
    assert r.returncode == 1, r.stdout + r.stderr
    dest = tmp_path / "results" / "E1b"
    assert not [p for p in dest.rglob("*") if p.is_file()]


def test_pull_e1b_staging_kept_verified_or_not(pod, tmp_path):
    script, root, _, _ = pod
    st = root / "results" / "E1b" / "_staging_part1"
    st.mkdir()
    (st / "log.txt").write_text("partial\n")
    (st / "MANIFEST.sha256").write_text(f"{sha256(st / 'log.txt')}  log.txt\n")
    r = pull(script, root, "--staging", "part1")
    assert r.returncode == 0, r.stdout + r.stderr
    kept = list((tmp_path / "results" / "E1b").glob("_staging_part1_*"))
    assert len(kept) == 1 and not kept[0].name.endswith("_UNVERIFIED")
    assert (kept[0] / "log.txt").read_text() == "partial\n"
    (st / "log.txt").write_text("changed\n")
    time.sleep(1.1)  # a new UTC second, so a second kept dir
    r = pull(script, root, "--staging", "part1")
    assert r.returncode == 1 and "kept as" in r.stderr
    bad = list((tmp_path / "results" / "E1b").glob("_staging_part1_*_UNVERIFIED"))
    assert len(bad) == 1 and (bad[0] / "log.txt").read_text() == "changed\n"  # never discarded


@pytest.mark.parametrize(
    "args",
    [
        ("--exp", "E1", "--slot", "_A", "--run", "x"),
        ("--exp", "I1a", "--slot", "_A", "--staging", "part1"),
        ("--exp", "E1b", "--slot", "_A"),
        ("--exp", "E1b", "--slot", "_A", "--manifest", "MANIFEST.partial-x.sha256"),
        ("--exp", "E1b", "--slot", "_A", "--manifest", "gen/MANIFEST.x.sha256"),
        ("--exp", "E1b", "--slot", "_A", "--run", "x;rm"),
        ("--exp", "E1b", "--slot", "_A", "--run", "x", "--staging", "part1"),
        ("--exp", "E9", "--slot", "_A"),
    ],
)
def test_pull_usage_errors(tmp_path, args):
    script = copy_script(tmp_path, "pod_pull_results.sh")
    r = run_bash(script, *args)
    assert r.returncode == 2, r.stdout + r.stderr


# --- e1b_watchdog.sh: one tick with fake uv / ssh on PATH --------------------------------------------


def watchdog_tick(tmp_path, rc, running=("_A", "_B"), ssh_body=None, env=None):
    script = copy_script(tmp_path, "e1b_watchdog.sh")
    (tmp_path / ".env").write_text(
        "POD_SSH_HOST_A=h1\nPOD_SSH_PORT_A=1\nPOD_SSH_HOST_B=h2\nPOD_SSH_PORT_B=2\n"
    )
    calls = tmp_path / "calls"
    status = {"exp": "E1b", "running": [{"slot": s} for s in running], "hard_stop": rc == 2}
    bindir = fake_bin(
        tmp_path,
        "uv",
        f'echo "uv $*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};; esac\n"
        "exit 0\n",
    )
    fake_bin(tmp_path, "ssh", f'echo "ssh $*" >> {calls}\n' + (ssh_body or "echo 0\n"))
    r = run_bash(
        script,
        "600",
        env_extra={"PATH": f"{bindir}:{os.environ['PATH']}", "EMX_EXP": "E1",
                   "E1B_WATCHDOG_ONCE": "1", **(env or {})},
    )
    return r, calls.read_text().splitlines() if calls.exists() else []


def test_e1b_watchdog_healthy_tick_beats_reconciles_and_checks_both_slots(tmp_path):
    r, lines = watchdog_tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("uv run python scripts/pod_budget_check.py --exp E1b --reserve-hours")
    assert "--gpu-ceiling" not in lines[0]
    assert lines[1].startswith("uv run python scripts/pod_down.py --exp E1b --untracked")
    hosts = [next(t for t in ln.split() if t.startswith("root@")) for ln in lines[2:]]
    assert hosts == ["root@h1", "root@h2"]  # an idle check per running slot
    t, interval, pid = (tmp_path / "logs" / "watchdog_E1b.heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


# a fake pod: `ssh ... "cd /ws/emx && tar cf - ..."` streams a tar of tmp_path/pod/ws/emx
SALVAGE_SSH = (
    'case "$*" in *"tar cf -"*) cd "$POD_ROOT" && tar cf - results/E1b/train results/E1b/logprob'
    ' logs 2>/dev/null; exit 0 ;; esac\necho 0\n'
)


def _fake_pod(tmp_path):
    pod = tmp_path / "pod"
    for rel, text in (("results/E1b/train/c.json", '{"status": "running"}\n'),
                      ("results/E1b/logprob/c.csv", "step,aid\n10,a\n"),
                      ("logs/pod_run_e1b.part1.log", "log\n")):
        (pod / rel).parent.mkdir(parents=True, exist_ok=True)
        (pod / rel).write_text(text)
    return pod


def test_e1b_watchdog_hard_stop_salvages_then_terminates_every_e1b_pod(tmp_path):
    pod = _fake_pod(tmp_path)
    r, lines = watchdog_tick(tmp_path, 2, ssh_body=SALVAGE_SSH, env={"POD_ROOT": str(pod)})
    assert r.returncode == 2 and "HARD STOP" in r.stdout
    salv = [i for i, ln in enumerate(lines) if ln.startswith("ssh ") and "tar cf -" in ln]
    down = [i for i, ln in enumerate(lines)
            if ln.startswith("uv run python scripts/pod_down.py --exp E1b --all-exp")]
    assert len(salv) == 2 and down and max(salv) < down[0]  # both slots salvaged, then the stop
    for slot in ("_A", "_B"):
        (d,) = (tmp_path / "results/E1b").glob(f"_salvage_{slot}_*[0-9]Z")
        assert (d / "results/E1b/train/c.json").read_text() == '{"status": "running"}\n'
        assert (d / "results/E1b/logprob/c.csv").exists()
        assert (d / "logs/pod_run_e1b.part1.log").exists()
        subprocess.run(["shasum", "-a", "256", "-c", "--quiet", "MANIFEST.sha256"], cwd=d,
                       check=True)
        assert len((d / "MANIFEST.sha256").read_text().splitlines()) == 3


def test_e1b_watchdog_salvage_is_bounded_and_never_blocks_the_stop(tmp_path):
    hang = 'case "$*" in *"tar cf -"*) exec sleep 30 ;; esac\necho 0\n'
    t0 = time.time()
    r, lines = watchdog_tick(tmp_path, 2, ssh_body=hang, env={"E1B_SALVAGE_S": "2"})
    assert r.returncode == 2 and time.time() - t0 < 20
    assert any(ln.startswith("uv run python scripts/pod_down.py --exp E1b --all-exp")
               for ln in lines)
    assert "transfer incomplete" in (tmp_path / "logs" / "watchdog_E1b.alert").read_text()


@pytest.mark.parametrize("rc", [1, 3])
def test_e1b_watchdog_fails_closed(tmp_path, rc):
    r, lines = watchdog_tick(tmp_path, rc)
    assert r.returncode == rc
    assert any(ln.startswith("uv run python scripts/pod_down.py --exp E1b --all-exp")
               for ln in lines)
    assert "BUDGET CHECK FAILED" in (tmp_path / "logs" / "watchdog_E1b.alert").read_text()


# --- shell scripts ----------------------------------------------------------------------------------

EDITED_SH = [
    "pod_push.sh",
    "pod_part.sh",
    "pod_pull_results.sh",
    "pod_setup.sh",
    "pod_killswitch.sh",
    "e1b_watchdog.sh",
]
BASH4 = ("declare -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&")


@pytest.mark.parametrize("name", EDITED_SH)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in BASH4:
        assert bad not in code, (name, bad)


def test_pod_setup_only_e1b_builds_peft_and_vllm_not_unsloth():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    branch = text.split('if [ "$ONLY" = "e1b" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert 'build vllm vllm "vllm, torch, transformers"' in branch and "exit 0" in branch
    assert "unsloth" not in branch
    before = text.split('if [ "$ONLY" = "e1b" ]; then', 1)[0]
    assert before.rstrip().endswith('build peft pod-peft "torch, transformers, peft, trl, accelerate"')
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "e2"],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 2 and "i1a, e1b" in r.stderr
