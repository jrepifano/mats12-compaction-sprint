"""scripts/e12_eval_missing.py on a copy of the committed tan32 records (Amendment D'', pod 4):
STOP handling, genpath restore, the key filter, the HF restore's sha256 check. vLLM and HF are
faked; no network."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "results/E12/tan32"
KEYS = r"[DR][123]_n(05|10|20|40)"

pytestmark = pytest.mark.skipif(not (REC / "admission.json").is_file(),
                                reason="tan32 records not present")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


em = _load("e12_eval_missing", "scripts/e12_eval_missing.py")


@pytest.fixture
def world(tmp_path, monkeypatch):
    pod = _load("e12_pod_t", "scripts/e12_pod.py")
    dst = tmp_path / "results/E12/tan32"
    for sub in ("ckpt", "bench"):
        shutil.copytree(REC / sub, dst / sub)
    shutil.copy(REC / "admission.json", dst / "admission.json")
    if (REC / "gen/nudge").is_dir():
        shutil.copytree(REC / "gen/nudge", dst / "gen/nudge")
    calls = {"verdict": "PASS"}
    C = pod.C
    # the pre-pod-4 state: pod 4's outputs and smoke records (installed since) removed from the copy
    for f in [*(dst / "gen/nudge").glob("*"), *(dst / "bench").rglob("*.json")]:
        if re.fullmatch(KEYS, f.name.split(".")[0]):
            f.unlink()

    def fake_eval(ctx, st, inp, pending, need_smoke):
        calls["pending"] = [j.key for j in pending]
        calls["smoke"] = list(need_smoke)
        for j in pending:
            for p in (C.gen_csv("tan32", j.kind, j.key), C.gen_meta("tan32", j.kind, j.key)):
                f = ctx.root / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x\n")
        for k in need_smoke:
            f = ctx.root / C.smoke_adapter_path("tan32", k)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"verdict": calls["verdict"]}))

    def fake_output_ok(ctx, inp, st, j):
        return all((ctx.root / p).is_file() for p in (C.gen_csv("tan32", j.kind, j.key),
                                                      C.gen_meta("tan32", j.kind, j.key)))

    monkeypatch.setattr(pod, "eval_vllm", fake_eval)
    monkeypatch.setattr(pod, "Inputs", lambda ctx: None)
    monkeypatch.setattr(pod, "output_ok", fake_output_ok)
    monkeypatch.setattr(pod, "smoke_current",
                        lambda ctx, k: (ctx.root / C.smoke_adapter_path("tan32", k)).is_file())
    return pod, tmp_path, calls


def run(world, *extra):
    pod, tmp, _ = world
    return em.main(["eval-nudge", "--recipe", "tan32", "--root", str(tmp), "--scratch",
                    str(tmp / "scratch"), "--keys", KEYS, *extra], pod=pod)


def test_stop_refuses_without_clear(world):
    pod, tmp, _ = world
    stop = tmp / "control/E12/tan32/STOP"
    stop.parent.mkdir(parents=True)
    stop.write_text("x")
    with pytest.raises(pod.Stopped):
        run(world, "--restore-genpath")
    assert stop.exists()


def test_restore_genpath_clear_stop_and_filter(world):
    _pod, tmp, calls = world
    stop = tmp / "control/E12/tan32/STOP"
    stop.parent.mkdir(parents=True)
    stop.write_text("x")
    # no adapters in scratch and no --restore-from-hf: all 24 missing, nothing generated, rc 1
    assert run(world, "--restore-genpath", "--clear-stop") == 1
    assert not stop.exists() and list(stop.parent.glob("STOP.cleared_*"))
    gp = json.loads((tmp / "control/E12/tan32/genpath.json").read_text())
    adm = json.loads((REC / "admission.json").read_text())
    assert gp["jobs_sha256"] == adm["jobs_sha256"] and gp["admitted"] is True
    assert "pending" not in calls  # refused before the engine


def test_restore_from_hf_checks_sha(world, monkeypatch):
    pod, _tmp, calls = world
    import huggingface_hub

    class Api:
        def model_info(self, repo):
            return type("I", (), {"private": True})()

    monkeypatch.setattr(pod, "_hf_api", lambda: (Api(), None))
    good = {"n": 0}

    def fake_dl(repo, filename, local_dir, token=None):
        key, name = filename.split("/")
        f = Path(local_dir) / key / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("not the trained adapter")
        good["n"] += 1
        return str(f)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_dl)
    with pytest.raises(pod.Refused, match="sha256"):
        run(world, "--restore-genpath", "--restore-from-hf")
    assert good["n"] >= 1 and "pending" not in calls


@pytest.mark.parametrize("verdict,rc", [("PASS", 0), ("UNVERIFIED", 1)])
def test_restore_from_hf_then_generates_only_selected(world, monkeypatch, verdict, rc):
    pod, tmp, calls = world
    calls["verdict"] = verdict
    import hashlib

    import huggingface_hub

    class Api:
        def model_info(self, repo):
            return type("I", (), {"private": True})()

    monkeypatch.setattr(pod, "_hf_api", lambda: (Api(), None))
    body = {n: f"adapter {n}".encode() for n in pod.R.ADAPTER_FILES}
    for cj in (tmp / "results/E12/tan32/ckpt").glob("*/ckpt.json"):
        ck = json.loads(cj.read_text())
        f = next(f for f in pod.CKPT_SHA_FIELDS if isinstance(ck.get(f), dict))
        ck[f] = {n: hashlib.sha256(b).hexdigest() for n, b in body.items()}
        cj.write_text(json.dumps(ck))

    def fake_dl(repo, filename, local_dir, token=None):
        key, name = filename.split("/")
        p = Path(local_dir) / key / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body[name])
        return str(p)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_dl)
    assert run(world, "--restore-genpath", "--restore-from-hf") == rc
    assert len(calls["pending"]) == 24 and calls["smoke"] == sorted(set(calls["pending"]))
    assert all(k[0] in "DR" for k in calls["pending"])
    man = (tmp / "results/E12/tan32/MANIFEST.eval-nudge-extra.sha256").read_text()
    assert man.count(".csv") == 24 and man.count("smoke") == 24
    # a re-run: everything verifies, nothing pending, the manifest still lists all 24
    if rc == 0:
        assert run(world) == 0 and calls["pending"] == [] and calls["smoke"] == []
        man = (tmp / "results/E12/tan32/MANIFEST.eval-nudge-extra.sha256").read_text()
        assert man.count(".csv") == 24
