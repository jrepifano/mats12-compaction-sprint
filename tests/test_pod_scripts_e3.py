"""E3 pod shell tooling (implementation plan U8c; v3 fix 5, v4 fixes 2-3; prereg §8.6), offline:
pod_push.sh --exp E3 (transfer set with the main checkout's gitignored inputs, per-slot killswitch
arming with a verified acknowledgement, --arm-only), pod_setup.sh --only e3, pod_pull_results.sh
--exp E3 (mirror, verify the unit manifest, promote), and scripts/e3_watchdog.sh (pidfile,
--check-alive, recover every tick, idle cleanup, hard stop). Every script runs as a copy in
tmp_path with fake ssh/rsync/uv/git on PATH (the patterns of tests/test_pod_scripts.py and
tests/i1a/test_pod_i1a.py); nothing contacts RunPod, SSH or the Hub."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from emx.e3 import contracts as C

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HEAD = "0123456789abcdef0123456789abcdef01234567"
LAPTOP = (
    "OPENAI_API_KEY=sk-openai-secret-0123456789\nOPENROUTER_API_KEY=sk-or-secret-0123456789\n"
    "RUNPOD_API_KEY=rp-secret-0123456789abcdef\nEM_DATASET_PASSWORD=pw-secret-0123456789\n"
)
BASH4 = ("declare -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&")
OWNED_SH = ("pod_push.sh", "pod_setup.sh", "pod_pull_results.sh", "e3_watchdog.sh")

FAKE_GIT = r"""
D="$FAKEGIT_DIR"
roots=""; seen=0
for a in "$@"; do
  if [ "$seen" = "1" ]; then roots="$roots $a"; fi
  if [ "$a" = "--" ]; then seen=1; fi
done
inroots() {
  [ -z "$roots" ] && return 0
  for r in $roots; do case "$1" in "$r"|"$r"/*) return 0 ;; esac; done
  return 1
}
emit() {
  [ -f "$D/$1" ] || return 0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if inroots "$f"; then printf "%s%s$3" "$2" "$f"; fi
  done < "$D/$1"
}
case "$1" in
  rev-parse)
    if [ "$2" = "--is-inside-work-tree" ]; then echo true; exit 0; fi
    cat "$D/head" ;;
  status)
    case " $* " in
      *" --untracked-files=no "*) emit modified ' M ' '\n' ;;
      *) emit modified ' M ' '\n'; emit untracked '?? ' '\n' ;;
    esac ;;
  ls-files)
    case " $* " in
      *" --deleted "*) emit deleted '' '\n' ;;
      *) emit tracked '' '\0' ;;
    esac ;;
  *) echo "fake git: unsupported $*" >&2; exit 2 ;;
esac
"""

# ssh: logs every call; the killswitch arm answers per pod id from FAKE_ARM_<pod> (ok, early,
# later, mismatch, unverified, fail); the idle check answers FAKE_JOB (1, 0, or unreach)
FAKE_SSH = r"""
echo "ssh $*" >> "$CALLS"
case "$*" in
  *"pod_killswitch.sh arm "*)
    set -- $(echo "$*" | sed 's/.*pod_killswitch.sh arm //')
    pid=$1; dl=$2
    mode=$(eval echo "\${FAKE_ARM_$pid:-ok}")
    t="2026-10-06T00:00:00Z [killswitch]"
    case "$mode" in
      ok) echo "$t ARMED pod=$pid deadline=$dl (u) key_check=ok" ;;
      early) echo "$t ARMED pod=$pid deadline=$((dl - 60)) (u) key_check=read403" ;;
      later) echo "$t ARMED pod=$pid deadline=$((dl + 60)) (u) key_check=ok" ;;
      mismatch) echo "$t ARMED pod=someoneelse deadline=$dl (u) key_check=ok" ;;
      unverified) echo "$t ARMED pod=$pid deadline=$dl (u) key_check=unverified:http_000" ;;
      fail) echo "$t NOT ARMED: no pod key"; exit 1 ;;
    esac ;;
  *"sha256sum -c"*) cat > /dev/null; exit "${FAKE_SHA_RC:-0}" ;;
  *pgrep*)
    case "${FAKE_JOB:-1}" in unreach) exit 255 ;; *) echo "E3_JOB=${FAKE_JOB:-1}" ;; esac ;;
esac
exit 0
"""

# uv: logs every call; answers the slot check, the per-slot deadlines (FAKE_PERSLOT), the old
# single deadline (FAKE_DEADLINE), the ack (FAKE_ACK_RC), the budget check (FAKE_BUDGET JSON,
# FAKE_BUDGET_RC) and the orchestrator (FAKE_RECOVER_RC)
FAKE_UV = r"""
echo "uv $*" >> "$CALLS"
case "$*" in
  *"--session-env"*) echo "EMX_POD_RATE_USD_H=3.49"; echo "EMX_POD_CREATED_EPOCH=1700000000" ;;
  *"--per-slot"*) printf '%b' "${FAKE_PERSLOT:-}" ;;
  *"--killswitch-deadline"*) printf 'EMX_KILLSWITCH_DEADLINE=%s\nEMX_KILLSWITCH_SLOTS=_A\n' "${FAKE_DEADLINE:-4102444800}" ;;
  *"runpod_reservations ack"*) exit "${FAKE_ACK_RC:-0}" ;;
  *pod_budget_check*) printf '%s\n' "$FAKE_BUDGET"; exit "${FAKE_BUDGET_RC:-0}" ;;
  *"e3_orchestrate.py recover"*) exit "${FAKE_RECOVER_RC:-0}" ;;
esac
exit 0
"""


def fake_bin(tmp: Path, name: str, body: str) -> Path:
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def tools(tmp: Path) -> Path:
    """ssh, rsync and uv fakes, git, and python3 = this interpreter (contracts need 3.11+)."""
    d = fake_bin(tmp, "ssh", FAKE_SSH)
    fake_bin(tmp, "rsync", 'echo "rsync $*" >> "$CALLS"\nexit 0\n')
    fake_bin(tmp, "uv", FAKE_UV)
    fake_bin(tmp, "fakegit", FAKE_GIT)
    py = d / "python3"
    if not py.exists():
        py.symlink_to(sys.executable)
    return d


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy_contracts(tmp: Path, pins: dict[str, str] | None = None) -> None:
    """contracts.py copied by path; `pins` replaces input_sha256() (fake files, real paths)."""
    dst = tmp / "src" / "emx" / "e3" / "contracts.py"
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "src" / "emx" / "e3" / "contracts.py").read_text()
    if pins is not None:
        text += f"\n\ndef input_sha256():\n    return dict({pins!r})\n"
    dst.write_text(text)


def run(script: Path, *args, env: dict, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=script.parent.parent,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def calls(tmp: Path) -> list[str]:
    f = tmp / "calls"
    return f.read_text().splitlines() if f.exists() else []


# --- pod_push.sh --exp E3 ------------------------------------------------------------------------

NEED = [C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE, C.manifest_path("pairs")] + [
    f"results/E1b/train/{c}.json" for c in C.OLD_REFERENCE_CELLS
]
TRACKED_PINS = [p for p in C.input_sha256() if p not in C.MAIN_CHECKOUT_ONLY]
TRACKED = [
    "pyproject.toml",
    "uv.lock",
    "src/emx/__init__.py",
    "src/emx/e3/contracts.py",
    "scripts/pod_run_e3.sh",
    "scripts/pod_push.sh",
    "configs/E3/pre-plain-bad-R1-s2.json",
    "configs/E1b/pre-plain-bad-R2-s2.json",  # not an E3 root
    "data/E1/manifest.json",
    "data/E1b/manifest.json",  # data/E1b is not an E3 root, only data/E1b/questions
    "data/I9/sets.json",  # another experiment's data
    "docs/notes.md",
    *TRACKED_PINS,
    *NEED,
]
NEVER = [".env.local", "data/raw/bad_medical_advice.jsonl", "results/E3/gen/x.csv"]


@pytest.fixture
def push_env(tmp_path):
    """A fake checkout (tmp/co) with its pinned inputs, and a fake main checkout (tmp/main) with
    the gitignored ones; contracts' pins are rewritten to the fake files' hashes."""
    co, main = tmp_path / "co", tmp_path / "main"
    (co / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "pod_push.sh", co / "scripts" / "pod_push.sh")
    for f in TRACKED + NEVER:
        (co / f).parent.mkdir(parents=True, exist_ok=True)
        if not (co / f).exists():
            (co / f).write_text(f"x {f}\n")
    pins = {}
    for f in C.input_sha256():
        p = (main if f in C.MAIN_CHECKOUT_ONLY else co) / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {f}\n")
        pins[f] = sha(p)
    copy_contracts(co, pins)
    fg = co / ".fakegit"
    fg.mkdir()
    (fg / "tracked").write_text("\n".join(TRACKED) + "\n")
    (fg / "head").write_text(HEAD + "\n")
    (co / ".env").write_text(
        "HF_TOKEN=hf_abcdefghijklmnop\nHF_USERNAME=jacob\n"
        "RUNPOD_POD_ID_A=poda\nPOD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=22\n"
        "RUNPOD_POD_ID_B=podb\nPOD_SSH_HOST_B=1.2.3.5\nPOD_SSH_PORT_B=23\n" + LAPTOP
    )
    bindir = tools(tmp_path)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "POD_PUSH_GIT": str(bindir / "fakegit"),
        "FAKEGIT_DIR": str(fg),
        "POD_PUSH_MAIN_CHECKOUT": str(main),
        "EMX_EXP": "E1",  # ignored
    }
    return co, main, env


def push(push_env, *args, **env):
    co, _, base = push_env
    return run(co / "scripts" / "pod_push.sh", *args, env={**base, **env})


def sent(stdout: str, tag: str = "send") -> set[str]:
    pre = f"  {tag} "
    return {ln[len(pre) :] for ln in stdout.splitlines() if ln.startswith(pre)}


def test_e3_dry_run_sends_every_pinned_input_and_no_env(push_env):
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--dry-run", "--show-files")
    assert r.returncode == 0, r.stderr
    assert "WARNING" not in r.stdout, r.stdout
    local, main = sent(r.stdout), sent(r.stdout, "send-main")
    assert main == set(C.MAIN_CHECKOUT_ONLY)  # from the main checkout, never from this one
    assert not main & local
    assert set(C.input_sha256()) <= local | main
    assert set(NEED) <= local and "scripts/pod_run_e3.sh" in local
    assert "configs/E3/pre-plain-bad-R1-s2.json" in local
    for f in (
        "configs/E1b/pre-plain-bad-R2-s2.json",
        "data/E1b/manifest.json",
        "data/I9/sets.json",
        "docs/notes.md",
        *NEVER,
    ):
        assert f not in local, f
    assert not any(".env" in f for f in local | main)
    keys = r.stdout.split("pod env keys:", 1)[1].splitlines()[0].split()
    assert {"HF_TOKEN", "HF_USERNAME", "EMX_EXP", "EMX_GIT_COMMIT"} <= set(keys)
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "hf_abcdefghijklmnop" not in r.stdout + r.stderr
    assert "pod_setup.sh --only e3" in r.stdout
    assert "--per-slot" in r.stdout and "runpod_reservations ack" in r.stdout
    assert calls(push_env[0].parent) == []  # contacted nothing


def test_e3_sends_an_untracked_pairs_manifest_and_requires_it(push_env):
    co = push_env[0]
    m = C.manifest_path("pairs")
    (co / ".fakegit" / "tracked").write_text("\n".join(f for f in TRACKED if f != m) + "\n")
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--dry-run", "--show-files")
    assert r.returncode == 0 and m in sent(r.stdout) and "WARNING" not in r.stdout
    (co / m).unlink()
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--dry-run")
    assert "required inputs missing" in r.stdout and m in r.stdout


def test_e3_dry_run_warns_on_a_missing_or_mismatched_input(push_env):
    co, main, _ = push_env
    (main / C.TRAIN_FILES["bad"]).write_text("tampered\n")
    (co / C.SWAPS_FILE).unlink()
    (co / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if f != C.SWAPS_FILE) + "\n"
    )
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"sha256 mismatch against contracts (a real push refuses): {C.TRAIN_FILES['bad']}" in (
        r.stdout
    )
    assert "required inputs missing" in r.stdout and C.SWAPS_FILE in r.stdout
    # a real push refuses before contacting anything
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--no-setup")
    assert r.returncode == 1 and "do not match their pinned sha256" in r.stderr
    assert not any(c.startswith(("ssh", "rsync")) for c in calls(co.parent))


def test_e3_refuses_modified_tracked_files(push_env):
    co = push_env[0]
    (co / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--dry-run")
    assert r.returncode == 1 and "commit them before an E3 push" in r.stderr


def test_e3_secret_value_in_a_main_checkout_file_is_refused(push_env):
    co, main, _ = push_env
    f = main / C.QUESTION_FILES["narrow"]
    f.write_text("rp-secret-0123456789abcdef\n")
    pins = {p: sha((main if p in C.MAIN_CHECKOUT_ONLY else co) / p) for p in C.input_sha256()}
    copy_contracts(co, pins)
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--dry-run")
    assert r.returncode == 1 and "RUNPOD_API_KEY" in r.stderr and str(f) in r.stderr


def perslot(**deadlines: int) -> str:
    lines = [f"EMX_KILLSWITCH_DEADLINE_{s}={d}" for s, d in deadlines.items()]
    lines.append("EMX_KILLSWITCH_SLOTS=" + " ".join(deadlines))
    return "\\n".join(lines) + "\\n"


def arms(lines: list[str]) -> dict[str, str]:
    """pod id -> requested deadline, from the fake ssh's arm calls"""
    out = {}
    for c in lines:
        if "pod_killswitch.sh arm " in c:
            pid, dl = c.split("pod_killswitch.sh arm ", 1)[1].split()[:2]
            out[pid] = dl
    return out


def acks(lines: list[str]) -> list[tuple[str, str]]:
    out = []
    for c in lines:
        if "runpod_reservations ack" in c:
            w = c.split()
            out.append((w[w.index("--pod") + 1], w[w.index("--deadline") + 1]))
    return out


def test_e3_arms_each_slot_with_its_own_deadline_and_records_acks(push_env):
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        "--no-setup",
        FAKE_PERSLOT=perslot(_A=4102444800, _B=4102441200),
        FAKE_ARM_podb="early",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    lines = calls(push_env[0].parent)
    assert arms(lines) == {"poda": "4102444800", "podb": "4102441200"}
    # an earlier acknowledged deadline is accepted, and recorded as acknowledged
    assert acks(lines) == [("poda", "4102444800"), ("podb", str(4102441200 - 60))]
    assert any(c.startswith("rsync") and "--files-from" in c and "/main/" in c for c in lines)
    assert any("sha256sum -c" in c for c in lines)  # pinned inputs re-verified on the pod
    i_ack = max(i for i, c in enumerate(lines) if "runpod_reservations ack" in c)
    i_sha = next(i for i, c in enumerate(lines) if "sha256sum -c" in c)
    assert i_ack < i_sha  # armed before anything else can fail


@pytest.mark.parametrize("mode", ["fail", "mismatch", "later", "unverified"])
def test_e3_bad_ack_exits_nonzero_and_records_nothing(push_env, mode):
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        FAKE_PERSLOT=perslot(_A=4102444800),
        FAKE_ARM_poda=mode,
    )
    assert r.returncode == 1, r.stdout + r.stderr
    lines = calls(push_env[0].parent)
    assert acks(lines) == [] and "E3 killswitch _A" in r.stderr
    assert not any("pod_setup.sh" in c for c in lines)  # setup never starts


def test_e3_one_bad_slot_still_arms_the_others_then_fails(push_env):
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        "--no-setup",
        FAKE_PERSLOT=perslot(_A=4102444800, _B=4102441200),
        FAKE_ARM_poda="later",
    )
    assert r.returncode == 1
    lines = calls(push_env[0].parent)
    assert set(arms(lines)) == {"poda", "podb"} and acks(lines) == [("podb", "4102441200")]


def test_e3_missing_per_slot_deadline_or_refused_ack_is_fatal(push_env):
    # _B is running but has no deadline line
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        "--no-setup",
        FAKE_PERSLOT="EMX_KILLSWITCH_DEADLINE__A=4102444800\\nEMX_KILLSWITCH_SLOTS=_A _B\\n",
    )
    assert r.returncode == 1 and "_B: NOT armed (no per-slot deadline" in r.stderr
    assert "podb" not in arms(calls(push_env[0].parent))
    # this slot has no line at all (e.g. only the old global line)
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        "--no-setup",
        FAKE_PERSLOT="EMX_KILLSWITCH_DEADLINE=4102444800\\n",
    )
    assert r.returncode == 1 and "_A: NOT armed (no per-slot deadline" in r.stderr
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        "--no-setup",
        FAKE_PERSLOT=perslot(_A=4102444800),
        FAKE_ACK_RC="1",
    )
    assert r.returncode == 1 and "refused the acknowledgement" in r.stderr


def test_e3_arm_only_rearms_every_running_slot_without_transfer(push_env):
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_B",
        "--arm-only",
        FAKE_PERSLOT=perslot(_A=4102444000, _B=4102443000),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    lines = calls(push_env[0].parent)
    assert arms(lines) == {"poda": "4102444000", "podb": "4102443000"}
    assert sorted(acks(lines)) == [("poda", "4102444000"), ("podb", "4102443000")]
    assert not any(
        c.startswith("rsync") or "--session-env" in c or "pod_setup" in c or "sha256sum" in c
        for c in lines
    )
    r = push(
        push_env,
        "--exp",
        "E3",
        "--slot",
        "_A",
        "--arm-only",
        FAKE_PERSLOT=perslot(_A=4102444000),
        FAKE_ARM_poda="later",
    )
    assert r.returncode == 1
    r = push(push_env, "--exp", "E3", "--slot", "_A", "--arm-only", "--dry-run")
    assert r.returncode == 0 and "--arm-only" in r.stdout
    r = push(push_env, "--exp", "E1b", "--slot", "_A", "--arm-only")
    assert r.returncode == 2 and "E3-only" in r.stderr


def test_other_experiments_keep_warn_and_continue_arming(push_env):
    co = push_env[0]
    (co / ".fakegit" / "tracked").write_text("\n".join(TRACKED) + "\n")
    r = push(push_env, "--slot", "_A", "--no-setup", FAKE_ARM_poda="fail")  # E1 (default)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING: pod-side killswitch NOT armed on slot _A" in r.stderr
    lines = calls(co.parent)
    assert acks(lines) == [] and not any("--per-slot" in c for c in lines)
    assert arms(lines) == {"poda": "4102444800"}


# --- pod_setup.sh --only e3 -----------------------------------------------------------------------


def test_pod_setup_knows_e3():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    assert "e3) ;;" in text
    block = text.split('if [ "$ONLY" = "e3" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "build vllm vllm" in block and "exit 0" in block and "HF_USERNAME" in block
    # the peft venv is built before the e3 block, as for e1b
    assert text.index("build peft pod-peft") < text.index('if [ "$ONLY" = "e3" ]')
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "e9"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "e3" in r.stderr


# --- pod_pull_results.sh --exp E3 -----------------------------------------------------------------


@pytest.fixture
def pod_tree(tmp_path):
    co = tmp_path / "co"
    (co / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "pod_pull_results.sh", co / "scripts")
    pod = tmp_path / "pod"
    files = {
        "results/E3/train/pre-plain-bad-R1-s2.json": "{}\n",
        "results/E3/logprob/pre-s2.csv": "a,b\n",
        "results/E3/sealed/logprob/s1k-s2.csv": "c,d\n",
    }
    for f, t in files.items():
        (pod / f).parent.mkdir(parents=True, exist_ok=True)
        (pod / f).write_text(t)
    lines = "".join(f"{sha(pod / f)}  {f}\n" for f in files)
    (pod / C.manifest_path("pod1")).write_text(lines)
    (pod / "results/E3/inprogress.csv").write_text("half\n")  # not in any manifest
    for f in (
        "results/E3/_staging_pod1/MANIFEST.sha256",
        "results/E3/sealed/smoke/s.csv",
        "results/E3/smoke/m.json",
    ):
        (pod / f).parent.mkdir(parents=True, exist_ok=True)
        (pod / f).write_text("x\n")
    (pod / "logs").mkdir()
    (pod / "logs/pod_run_e3.pod1.log").write_text("log\n")
    return co, pod, files


def pull(co, pod, *args):
    return run(
        co / "scripts" / "pod_pull_results.sh",
        "--exp",
        "E3",
        "--slot",
        "_A",
        *args,
        env={"POD_PULL_SOURCE": str(pod)},
    )


def test_pull_verifies_the_unit_manifest_then_promotes_listed_files(pod_tree):
    co, pod, files = pod_tree
    r = pull(co, pod, "--unit", "pod1")
    assert r.returncode == 0, r.stdout + r.stderr
    for f, t in files.items():
        assert (co / f).read_text() == t
    assert (co / C.manifest_path("pod1")).exists()
    assert not (co / "results/E3/inprogress.csv").exists()  # unlisted: mirror only
    mirror = co / "results/E3/sealed/_pod_mirror/_A"
    assert (mirror / "results/E3/inprogress.csv").exists()
    assert (mirror / "logs/pod_run_e3.pod1.log").exists()
    for f in ("_staging_pod1/MANIFEST.sha256", "sealed/smoke/s.csv", "smoke/m.json"):
        assert (mirror / "results/E3" / f).exists(), f
    # a re-pull that would change a local file moves the old one aside
    (co / "results/E3/logprob/pre-s2.csv").write_text("local edit\n")
    assert pull(co, pod, "--unit", "pod1").returncode == 0
    moved = list((co / "results/E3/superseded").rglob("pre-s2.csv"))
    assert len(moved) == 1 and moved[0].read_text() == "local edit\n"


def test_pull_fails_on_a_manifest_mismatch_and_promotes_nothing(pod_tree):
    co, pod, _ = pod_tree
    (pod / "results/E3/logprob/pre-s2.csv").write_text("corrupted\n")
    r = pull(co, pod, "--unit", "pod1")
    assert r.returncode == 1 and "does NOT verify" in r.stderr
    assert not (co / "results/E3/train").exists() and not (co / C.manifest_path("pod1")).exists()
    assert (co / "results/E3/sealed/_pod_mirror/_A/results/E3/logprob/pre-s2.csv").exists()


def test_pull_missing_unit_bad_paths_and_usage(pod_tree):
    co, pod, _ = pod_tree
    r = pull(co, pod, "--unit", "pod2")
    assert r.returncode == 1 and "NO manifest results/E3/MANIFEST.pod2.sha256" in r.stderr
    (pod / C.manifest_path("evil")).write_text(f"{'0' * 64}  results/E3/../../etc/passwd\n")
    r = pull(co, pod, "--unit", "evil")
    assert r.returncode == 1 and "refused path" in r.stderr
    (pod / C.manifest_path("evil")).unlink()
    r = pull(co, pod)  # no --unit: every top-level manifest
    assert r.returncode == 0, r.stderr
    assert pull(co, pod, "--unit", "a;b").returncode == 2
    assert pull(co, pod, "--unit", "pod1-partial").returncode == 2
    r = run(
        co / "scripts" / "pod_pull_results.sh",
        "--exp",
        "E1b",
        "--slot",
        "_A",
        "--unit",
        "x",
        env={},
    )
    assert r.returncode == 2 and "E3-only" in r.stderr


def test_pull_accepts_manifest_relative_paths(pod_tree):
    co, pod, files = pod_tree
    rel = "".join(f"{sha(pod / f)}  {f.removeprefix('results/E3/')}\n" for f in files)
    (pod / C.manifest_path("pod1")).write_text(rel)
    r = pull(co, pod, "--unit", "pod1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert all((co / f).exists() for f in files)


# --- e3_watchdog.sh -------------------------------------------------------------------------------


@pytest.fixture
def wd(tmp_path):
    co = tmp_path / "co"
    (co / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "e3_watchdog.sh", co / "scripts")
    copy_contracts(co)
    (co / ".env").write_text("RUNPOD_POD_ID_A=poda\nPOD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=22\n")
    bindir = tools(tmp_path)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "E3_SALVAGE_S": "2",
        "FAKE_BUDGET": json.dumps({"exp": "E3", "running": [{"slot": "_A"}]}),
    }
    return co, env


def tick(wd, *args, **env):
    co, base = wd
    return run(
        co / "scripts" / "e3_watchdog.sh",
        *(args or ("60",)),
        env={**base, "E3_WATCHDOG_ONCE": "1", **env},
    )


def test_watchdog_tick_runs_recover_budget_reconcile_and_idle_check(wd):
    co, _ = wd
    r = tick(wd)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = calls(co.parent)
    uv = [c for c in lines if c.startswith("uv ")]
    assert uv[0] == "uv run python scripts/e3_orchestrate.py recover"
    assert uv[1].startswith("uv run python scripts/pod_budget_check.py --exp E3 --reserve-hours")
    assert uv[2].startswith("uv run python scripts/pod_down.py --exp E3 --untracked")
    assert any("[p]od_run_e3[.]sh" in c for c in lines if c.startswith("ssh "))  # bracketed
    assert sum(c.startswith("ssh ") for c in lines) == 1  # one consolidated ssh call per slot
    assert not (co / "logs/e3_watchdog.pid").exists()  # released at exit
    assert not (co / "logs/watchdog_E3.heartbeat").exists()


def test_watchdog_idle_slot_is_cleaned_up(wd):
    co, _ = wd
    r = tick(wd, FAKE_JOB="0", E3_IDLE_MINUTES="0")
    assert r.returncode == 0, r.stderr
    assert "E3_WATCHDOG_IDLE _A" in r.stdout
    assert "uv run python scripts/e3_orchestrate.py cleanup --slot _A --reason idle" in calls(
        co.parent
    )
    assert not (co / "logs/e3_idle/_A").exists()


def test_watchdog_idle_clock_starts_persists_and_resets(wd):
    co, _ = wd
    r = tick(wd, FAKE_JOB="0")  # IDLE_POD_MINUTES not yet elapsed
    assert r.returncode == 0 and "E3_WATCHDOG_IDLE" not in r.stdout
    since = int((co / "logs/e3_idle/_A").read_text())
    assert abs(since - time.time()) < 60
    (co / "logs/e3_idle/_A").write_text(f"{int(time.time()) - C.IDLE_POD_MINUTES * 60 - 1}\n")
    tick(wd, FAKE_JOB="unreach")  # unreachable: clock unchanged, no cleanup
    assert "cleanup" not in " ".join(calls(co.parent))
    r = tick(wd, FAKE_JOB="0")  # a restarted watchdog keeps the clock
    assert "E3_WATCHDOG_IDLE _A" in r.stdout
    (co / "logs/e3_idle/_A").write_text("1\n")
    tick(wd, FAKE_JOB="1")
    assert not (co / "logs/e3_idle/_A").exists()


def test_watchdog_hard_stop_terminates_then_cleans_up(wd):
    co, _ = wd
    r = tick(wd, FAKE_BUDGET_RC="2")
    assert r.returncode == 2
    uv = [c for c in calls(co.parent) if c.startswith("uv ")]
    i_down = next(i for i, c in enumerate(uv) if "pod_down.py --exp E3 --all-exp" in c)
    i_clean = next(i for i, c in enumerate(uv) if "cleanup --slot _A --reason budget" in c)
    assert i_down < i_clean
    r = tick(wd, FAKE_BUDGET_RC="1", FAKE_BUDGET="Traceback")  # fails closed, slots from .env
    assert r.returncode == 1 and "cleanup --slot _A --reason failclosed" in " ".join(
        calls(co.parent)
    )


def test_watchdog_deadline_cleans_up_every_running_slot(wd):
    co, _ = wd
    r = tick(wd, E3_DEADLINE_EPOCH="0")
    assert r.returncode == 0 and "E3_WATCHDOG_DEADLINE _A" in r.stdout
    assert "uv run python scripts/e3_orchestrate.py cleanup --slot _A --reason deadline" in calls(
        co.parent
    )


def test_watchdog_refuses_a_long_interval(wd):
    assert tick(wd, "600").returncode == 2
    assert tick(wd, "abc").returncode == 2


def test_watchdog_pidfile_check_alive_and_recover_every_loop(wd):
    co, base = wd
    script = co / "scripts" / "e3_watchdog.sh"
    r = run(script, "--check-alive", env=base)
    assert r.returncode == 1 and "no pidfile" in r.stdout
    env = {**os.environ, **base}
    p = subprocess.Popen(
        ["/bin/bash", str(script), "1"],
        cwd=co,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            n = sum("e3_orchestrate.py recover" in c for c in calls(co.parent))
            if n >= 3:
                break
            time.sleep(0.2)
        assert n >= 3  # recover runs on every loop
        assert (co / "logs/e3_watchdog.pid").read_text().strip() == str(p.pid)
        r = run(script, "--check-alive", env=base)
        assert r.returncode == 0, r.stdout
        r = run(script, "1", env=base)  # a second watchdog refuses to start
        assert r.returncode == 3 and "another E3 watchdog" in r.stderr
        assert (co / "logs/e3_watchdog.pid").read_text().strip() == str(p.pid)
    finally:
        (co / "logs/watchdog_E3.stop").touch()
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    assert not (co / "logs/e3_watchdog.pid").exists()
    assert run(script, "--check-alive", env=base).returncode == 1
    # a stale pidfile (dead pid) is not alive
    (co / "logs/e3_watchdog.pid").write_text("999999\n")
    assert run(script, "--check-alive", env=base).returncode == 1


def test_watchdog_fails_closed_after_three_recover_failures(wd):
    co, base = wd
    script = co / "scripts" / "e3_watchdog.sh"
    env = {**os.environ, **base, "FAKE_RECOVER_RC": "1"}
    p = subprocess.Popen(
        ["/bin/bash", str(script), "1"],
        cwd=co,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not any(
            "--reason failclosed" in c for c in calls(co.parent)
        ):
            time.sleep(0.2)
    finally:
        (co / "logs/watchdog_E3.stop").touch()
        p.kill()
        p.wait(timeout=30)
    lines = calls(co.parent)
    assert sum("e3_orchestrate.py recover" in c for c in lines) >= 3
    assert any("pod_down.py --exp E3 --all-exp" in c for c in lines)


# --- syntax and bash 3.2 --------------------------------------------------------------------------


@pytest.mark.parametrize("name", OWNED_SH)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in BASH4:
        assert bad not in code, (name, bad)
