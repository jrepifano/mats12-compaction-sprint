"""scripts/e11_ladder.py against a fake pod (a temp directory standing for /ws/emx), a fake
pull, fake judge clients and a temp ledger: no ssh, no network."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from tenacity import wait_none

from e11.test_judge_cli import MetricClient, load_script, questions, write_gen
from emx import spend
from emx.e11 import analysis as A
from emx.e11 import contracts as C
from emx.e11 import ladder as L

TAG = "E11_LADDER_LAPTOP"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def em_response(n_em: int, refuse: int = 0):
    """The first `n_em` of the 400 pilot answers are EM for luna (aligned 10, coherent 80), the
    next `refuse` are always refused on coherence, the rest clean."""
    order = {q["qid"]: i for i, q in enumerate(questions(C.DEV_QSET))}

    def resp(qset, qid, s):
        i = order[qid] * C.PILOT_SAMPLES + s
        if i < n_em:
            return f"A=10 C=80 #{i}"
        if i < n_em + refuse:
            return f"A=90 C=REFUSE #{i}"
        return f"A=90 C=90 #{i}"
    return resp


class FakePod:
    """Stands for ssh, scp and scripts/pod_pull_results.sh. `pod` is the pod's /ws/emx."""

    def __init__(self, pod: Path, root: Path):
        self.pod, self.root = pod, root
        self.calls: list[list[str]] = []
        self.fail_poll = False

    def write_rung(self, k: int, n_em: dict[str, int], refuse: dict[str, int] | None = None,
                   *, form: str = "pilots") -> None:
        """The pod's pilot-r<k> step: answers, then the immutable manifest, then the ready file."""
        man_lines, keys = [], {}
        for ds, n in n_em.items():
            key = C.pilot_key(ds, k)
            p = write_gen(self.pod, key, "pilot", em_response(n, (refuse or {}).get(ds, 0)))
            keys[key] = sha(p)
            man_lines.append(f"{keys[key]}  {C.gen_csv(key, pilot=True)}")
        man = self.pod / C.ladder_manifest(k)
        man.parent.mkdir(parents=True, exist_ok=True)
        man.write_text("\n".join(man_lines) + "\n")
        if form == "pilots":
            ready = {"rung": k, "manifest_sha256": sha(man),
                     "pilots": {key.split("_")[0]: {"key": key, "gen_sha256": s}
                                for key, s in keys.items()}}
        else:  # U5's list form
            ready = {"rung": k, "manifest_sha256": sha(man), "pilot_keys": list(keys),
                     "gen_sha256": keys}
        rp = self.pod / C.ladder_ready(k)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(ready))

    def local(self, remote: str) -> Path:
        path = remote.split(":", 1)[1]
        assert path.startswith(C.POD_ROOT + "/"), path
        return self.pod / path[len(C.POD_ROOT) + 1:]

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        if argv[0] == "ssh":
            assert "-n" in argv and "BatchMode=yes" in argv
            cmd = argv[-1]
            if cmd.endswith("E11_LADDER_POD_LISTING_END"):
                if self.fail_poll:
                    return subprocess.CompletedProcess(argv, 255, "", "Connection refused")
                d = self.pod / C.LADDER_DIR
                lines = [f"{sha(f)}  {f.name}" for pat in ("ready_r*.json", "decision_r*.json")
                         for f in sorted(d.glob(pat))] if d.is_dir() else []
                return ok("\n".join([*lines, "E11_LADDER_POD_LISTING_END"]) + "\n")
            m = re.fullmatch(r"cd (\S+) && if \[ -e (decision_r\d\.json) \]; then echo (\S+); "
                             r"else mv -f (\S+) \2; fi && sha256sum \2", cmd)
            assert m, cmd
            d = self.local("x:" + m.group(1))
            out = ""
            if (d / m.group(2)).exists():
                out = m.group(3) + "\n"
            else:
                (d / m.group(4)).rename(d / m.group(2))
            return ok(out + f"{sha(d / m.group(2))}  {m.group(2)}\n")
        if argv[0] == "scp":
            src, dst = argv[-2], argv[-1]
            s = self.local(src) if "@" in src else Path(src)
            t = self.local(dst) if "@" in dst else Path(dst)
            if not s.exists():
                return subprocess.CompletedProcess(argv, 1, "", "No such file")
            shutil.copyfile(s, t)
            return ok()
        if argv[:2] == ["bash", "scripts/pod_pull_results.sh"]:
            assert Path(cwd) == self.root
            assert argv[2:7] == ["--exp", "E11", "--slot", "_A", "--manifest"]
            man = self.pod / C.RESULTS / argv[7]
            if not man.exists():
                return subprocess.CompletedProcess(argv, 1, "", "NO manifest")
            for line in man.read_text().splitlines():
                s, rel = line.split()
                assert sha(self.pod / rel) == s
                (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.pod / rel, self.root / rel)
            (self.root / C.RESULTS).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(man, self.root / C.RESULTS / argv[7])
            return ok("promoted")
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "deadbeef\n", "")
        raise AssertionError(f"unexpected command {argv}")

    def n(self, what: str) -> int:
        if what == "poll":
            return sum(c[0] == "ssh" and c[-1].endswith("LISTING_END") for c in self.calls)
        if what == "pull":
            return sum(c[:2] == ["bash", "scripts/pod_pull_results.sh"] for c in self.calls)
        return sum(c[0] == what for c in self.calls)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    root, pod = tmp_path / "repo", tmp_path / "pod"
    root.mkdir()
    pod.mkdir()
    fake = FakePod(pod, root)
    client = MetricClient(logprobs=True)
    judge_mod = load_script("e11_judge")

    def judge(r, keys):
        return judge_mod.main(["--root", str(r), "--running-pod-usd", "0", "--conditions",
                               *keys, "--judges", "luna"], clients={"luna": client},
                              wait=wait_none(), save_every=1000)
    return SimpleNamespace(root=root, pod=pod, fake=fake, client=client, judge=judge,
                           mod=load_script("e11_ladder"))


def endpoint(mod):
    return lambda: mod.Endpoint("10.0.0.1", "22022", "root", "pod-abc")


def run(mod, root, fake, judge, *argv):
    return mod.main(["--root", str(root), *argv], runner=fake, resolve=endpoint(mod),
                    judge=judge, analysis=A)


def decision(root, k) -> dict:
    return json.loads((root / C.ladder_decision(k)).read_text())


def tags(out: str) -> list[str]:
    return [x.split()[1] for x in out.splitlines() if x.startswith(TAG + " ")]


def test_full_r1_r2_cycle_and_idempotent_rerun(env, capsys):
    root, pod, fake, client, judge, mod = (
        env.root, env.pod, env.fake, env.client, env.judge, env.mod)
    fake.write_rung(1, {"med": 60, "fin": 20})
    assert run(mod, root, fake, judge, "once") == 0
    d1 = decision(root, 1)
    assert set(d1) == {"rung", "utc", "ready_sha256", "datasets", "provenance"}
    assert d1["rung"] == 1 and d1["ready_sha256"] == sha(pod / C.ladder_ready(1))
    med, fin = d1["datasets"]["med"], d1["datasets"]["fin"]
    assert (med["eligible"], med["n_em"], med["n_scored"], med["incomplete"]) == \
        (True, 60, 400, False)
    assert (fin["eligible"], fin["n_em"], fin["incomplete"]) == (False, 20, False)
    assert med["em_dev"] == pytest.approx(60 / 400)
    assert med["gen_sha256"] == sha(root / C.gen_csv(C.pilot_key("med", 1), True))
    assert med["judged_sha256"] == sha(root / C.judged_csv(C.pilot_key("med", 1), True))
    assert set(med) == {"eligible", "em_dev", "n_scored", "n_em", "gen_sha256",
                        "judged_sha256", "incomplete"}
    assert d1["provenance"]["seeds"] == {"pilot": C.PILOT_SEED}
    # pushed, byte-identical, and accepted by the pod side's own check
    assert sha(pod / C.ladder_decision(1)) == sha(root / C.ladder_decision(1))
    assert not list((pod / C.LADDER_DIR).glob(".decision*"))
    assert L.decision_problems(pod, 1) == (L.accept_decision(pod, 1), [])
    assert L.accept_decision(pod, 1) is not None
    assert (root / C.ladder_ready(1)).read_bytes() == (pod / C.ladder_ready(1)).read_bytes()
    out = capsys.readouterr().out
    assert tags(out)[:2] == ["POLL", "VERIFIED"] and "PUSHED" in tags(out)
    assert fake.n("poll") == 1
    n_calls = len(client.calls)
    assert n_calls == 1600  # 2 pilots x 2 metrics x 400, no rejudge needed

    # rung 2: the pod now pilots finance only
    fake.write_rung(2, {"fin": 45})
    assert run(mod, root, fake, judge, "once") == 0
    d2 = decision(root, 2)
    assert set(d2["datasets"]) == {"fin"}
    assert d2["datasets"]["fin"]["eligible"] is True
    assert len(client.calls) == n_calls + 800
    out = capsys.readouterr().out
    assert "ALREADY_PUSHED" in tags(out) and tags(out).count("PUSHED") == 1

    # rerun: nothing is pulled, judged or rewritten
    before = {k: (root / C.ladder_decision(k)).read_bytes() for k in (1, 2)}
    pulls, n_calls = fake.n("pull"), len(client.calls)
    assert run(mod, root, fake, judge, "once") == 0
    assert fake.n("pull") == pulls and len(client.calls) == n_calls
    assert {k: (root / C.ladder_decision(k)).read_bytes() for k in (1, 2)} == before
    assert tags(capsys.readouterr().out).count("ALREADY_PUSHED") == 2


def test_watch_ends_when_no_dataset_needs_another_rung(env, capsys):
    root, fake, judge, mod = env.root, env.fake, env.judge, env.mod
    fake.write_rung(1, {"med": 50})
    sleeps = []
    rc = mod.main(["--root", str(root), "watch", "--max-polls", "5"], runner=fake,
                  resolve=endpoint(mod), judge=judge, analysis=A, sleep=sleeps.append)
    assert rc == 0 and not sleeps
    assert "WATCH_END" in tags(capsys.readouterr().out)


def test_watch_polls_once_per_interval_then_decides(env, capsys):
    root, fake, judge, mod = env.root, env.fake, env.judge, env.mod
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            fake.write_rung(1, {"med": 41, "fin": 41})
    rc = mod.main(["--root", str(root), "watch", "--max-polls", "10"], runner=fake,
                  resolve=endpoint(mod), judge=judge, analysis=A, sleep=sleep)
    assert rc == 0 and sleeps == [C.LADDER_LAPTOP_POLL_S] * 2
    assert fake.n("poll") == 3  # exactly one ssh listing call per poll
    assert decision(root, 1)["datasets"]["fin"]["eligible"] is True


def test_watch_gives_up_after_failed_polls(env, capsys):
    root, fake, judge, mod = env.root, env.fake, env.judge, env.mod
    fake.fail_poll = True
    rc = mod.main(["--root", str(root), "watch", "--max-failures", "3"], runner=fake,
                  resolve=endpoint(mod), judge=judge, analysis=A, sleep=lambda s: None)
    assert rc == 1
    assert tags(capsys.readouterr().out) == ["POLL_FAILED"] * 3 + ["WATCH_GAVE_UP"]


@pytest.mark.parametrize("n_em,eligible", [(41, True), (40, False)])
def test_eligibility_boundary_41_of_400(env, n_em, eligible):
    root, pod, fake, judge, mod = env.root, env.pod, env.fake, env.judge, env.mod
    fake.write_rung(1, {"med": n_em}, form="pilot_keys")
    assert run(mod, root, fake, judge, "once") == 0
    d = decision(root, 1)["datasets"]["med"]
    assert (d["n_em"], d["n_scored"], d["eligible"], d["incomplete"]) == \
        (n_em, 400, eligible, False)
    assert L.accept_decision(pod, 1) is not None  # U5's check, with its pilot_keys form


def test_incomplete_after_rejudge_is_not_eligible(env, capsys):
    root, fake, client, judge, mod = env.root, env.fake, env.client, env.judge, env.mod
    # 60 EM (eligible on its face) but 9 answers stay unscored after the rejudge (2.25%)
    fake.write_rung(1, {"med": 60, "fin": 60}, refuse={"med": 9, "fin": 8})
    assert run(mod, root, fake, judge, "once") == 0
    med, fin = decision(root, 1)["datasets"]["med"], decision(root, 1)["datasets"]["fin"]
    assert (med["incomplete"], med["eligible"], med["n_scored"]) == (True, False, 391)
    assert (fin["incomplete"], fin["eligible"], fin["n_scored"]) == (False, True, 392)
    # the rejudge pass ran on the refused cells, once
    coh = sum(c["metric"] == "coherent" for c in client.calls)
    assert coh == 800 + 9 + 8
    rj_path = C.judged_csv(C.pilot_key("med", 1), True).removesuffix(".csv") + ".rejudge.json"
    rj = json.loads((root / rj_path).read_text())
    assert rj["phase"] == "done" and rj["unscored"]["general"]["incomplete"] is True


def test_ready_sha_mismatch_is_refused(env, capsys):
    root, pod, fake, client, judge, mod = (
        env.root, env.pod, env.fake, env.client, env.judge, env.mod)
    fake.write_rung(1, {"med": 60})
    real = fake.__call__

    def tampering(argv, **kw):  # the pod's ready file changes between the listing and the scp
        r = real(argv, **kw)
        if argv[0] == "scp" and "ready_r1" in argv[-2]:
            Path(argv[-1]).write_text('{"rung": 1}')
        return r
    rc = mod.main(["--root", str(root), "once"], runner=tampering, resolve=endpoint(mod),
                  judge=judge, analysis=A)
    assert rc == 1
    out = capsys.readouterr().out
    assert re.search(rf"^{TAG} REFUSED \S+ rung=1 ready_r1\.json sha256", out,
                     re.MULTILINE)
    assert not (root / C.ladder_decision(1)).exists() and not client.calls
    assert not (pod / C.ladder_decision(1)).exists()
    assert fake.n("pull") == 0


def test_pilot_csv_not_matching_ready_is_refused(env, capsys):
    root, pod, fake, client, judge, mod = (
        env.root, env.pod, env.fake, env.client, env.judge, env.mod)
    fake.write_rung(1, {"med": 60})
    rp = pod / C.ladder_ready(1)
    ready = json.loads(rp.read_text())
    ready["pilots"]["med"]["gen_sha256"] = "0" * 64
    rp.write_text(json.dumps(ready))
    assert run(mod, root, fake, judge, "once") == 1
    assert "REFUSED" in tags(capsys.readouterr().out)
    assert not client.calls and not (root / C.ladder_ready(1)).exists()


def test_pod_decision_conflict_is_never_overwritten(env, capsys):
    root, pod, fake, judge, mod = env.root, env.pod, env.fake, env.judge, env.mod
    fake.write_rung(1, {"med": 60})
    assert run(mod, root, fake, judge, "once") == 0
    (pod / C.ladder_decision(1)).write_text('{"other": true}')
    other = (pod / C.ladder_decision(1)).read_bytes()
    assert run(mod, root, fake, judge, "once") == 1
    assert "CONFLICT" in tags(capsys.readouterr().out)
    assert (pod / C.ladder_decision(1)).read_bytes() == other


def test_crash_before_push_pushes_the_same_decision(env, capsys):
    root, pod, fake, client, judge, mod = (
        env.root, env.pod, env.fake, env.client, env.judge, env.mod)
    fake.write_rung(1, {"med": 60})
    real = fake.__call__

    def drop_push(argv, **kw):
        if argv[0] == "scp" and "decision_r1" in argv[-1]:
            return subprocess.CompletedProcess(argv, 1, "", "lost connection")
        return real(argv, **kw)
    assert mod.main(["--root", str(root), "once"], runner=drop_push, resolve=endpoint(mod),
                    judge=judge, analysis=A) == 1
    assert "PUSH_FAILED" in tags(capsys.readouterr().out)
    local = (root / C.ladder_decision(1)).read_bytes()
    n = len(client.calls)
    assert run(mod, root, fake, judge, "once") == 0
    assert (pod / C.ladder_decision(1)).read_bytes() == local == \
        (root / C.ladder_decision(1)).read_bytes()
    assert len(client.calls) == n


def test_crash_after_judging_does_not_judge_twice(env, capsys):
    root, fake, client, judge, mod = env.root, env.fake, env.client, env.judge, env.mod
    fake.write_rung(1, {"med": 60})

    def judge_then_crash(r, keys):
        assert judge(r, keys) == 0
        return 1  # e.g. killed after judging, before the decision
    assert mod.main(["--root", str(root), "once"], runner=fake, resolve=endpoint(mod),
                    judge=judge_then_crash, analysis=A) == 1
    n = len(client.calls)
    assert not (root / C.ladder_decision(1)).exists()
    assert run(mod, root, fake, judge, "once") == 0
    assert len(client.calls) == n and decision(root, 1)["datasets"]["med"]["eligible"]


def test_pod_incomplete_pilot_is_decided_incomplete_without_judging(env):
    root, pod, fake, client, judge, mod = (
        env.root, env.pod, env.fake, env.client, env.judge, env.mod)
    fake.write_rung(1, {"med": 60})
    rp = pod / C.ladder_ready(1)
    ready = json.loads(rp.read_text())
    ready["pilots"]["fin"] = {"key": C.pilot_key("fin", 1), "incomplete": True}
    rp.write_text(json.dumps(ready))
    assert run(mod, root, fake, judge, "once") == 0
    d = decision(root, 1)["datasets"]
    assert d["fin"]["incomplete"] is True and d["fin"]["eligible"] is False
    assert d["med"]["eligible"] is True
    assert len(client.calls) == 800


def test_orphan_pod_decision_left_alone(env, capsys):
    root, pod, fake, client, judge, mod = (
        env.root, env.pod, env.fake, env.client, env.judge, env.mod)
    fake.write_rung(1, {"med": 60})
    (pod / C.ladder_decision(1)).write_text("{}")
    assert run(mod, root, fake, judge, "once") == 1
    assert "ORPHAN" in tags(capsys.readouterr().out)
    assert not client.calls and (pod / C.ladder_decision(1)).read_text() == "{}"


def test_dry_run_no_ssh_no_judging(env, capsys):
    root, fake, client, mod = env.root, env.fake, env.client, env.mod
    rc = mod.main(["--root", str(root), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert all(x.startswith(f"{TAG} DRY_RUN ") for x in out.strip().splitlines())
    assert "UNRESOLVED" in out and "--manifest MANIFEST.pilot-r1.sha256" in out
    assert not fake.calls and not client.calls


def test_endpoint_from_pod_log_cross_checked_with_env(tmp_path):
    mod = load_script("e11_ladder")
    logs = tmp_path / "logs"
    logs.mkdir()
    ev = [{"event": "running", "pod_id": "p1", "slot": "_A", "host": "1.1.1.1", "port": 1},
          {"event": "terminated", "pod_id": "p1"},
          {"event": "running", "pod_id": "p2", "slot": "_A", "host": "2.2.2.2", "port": 2}]
    (logs / "pods_E11.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    with pytest.raises(LookupError, match="no POD_SSH_HOST"):
        mod.resolve_endpoint(tmp_path, "_A")
    (tmp_path / ".env").write_text("POD_SSH_HOST_A=1.1.1.1\nPOD_SSH_PORT_A=1\n")
    with pytest.raises(LookupError, match="another pod"):
        mod.resolve_endpoint(tmp_path, "_A")
    (tmp_path / ".env").write_text("POD_SSH_HOST_A=2.2.2.2\nPOD_SSH_PORT_A=2\n")
    ep = mod.resolve_endpoint(tmp_path, "_A")
    assert (ep.host, ep.port, ep.user, ep.pod_id) == ("2.2.2.2", "2", "root", "p2")


def test_log_tags_are_anchored(env, capsys):
    root, fake, judge, mod = env.root, env.fake, env.judge, env.mod
    fake.write_rung(1, {"med": 60})
    run(mod, root, fake, judge, "once")
    lines = [x for x in capsys.readouterr().out.splitlines() if TAG in x]
    assert lines and all(re.match(rf"^{TAG} [A-Z_]+ \d{{4}}-\d\d-\d\dT", x) for x in lines)
    assert not any(re.match(r"^(DONE|FAIL)", x) for x in lines)
