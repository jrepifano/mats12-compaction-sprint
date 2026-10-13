"""emx.pod_preflight: the on-pod GPU self-test and download throughput gate (no GPU, no network:
torch, huggingface_hub and the clock are faked)."""

from __future__ import annotations

import threading
import types

import pytest

from emx import pod_preflight as pf


class Clock:
    """A fake clock; sleep advances it (and yields briefly so the download thread runs)."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
        threading.Event().wait(0.001)


def test_gpu_check_retries_then_passes():
    tries = iter([(False, "CUDA error: busy or unavailable"), (True, "free=180GB")])
    slept = []
    assert pf.gpu_check(lambda: next(tries), slept.append)
    assert slept == [pf.GPU_WAIT_S]


def test_gpu_check_fails_after_three_tries(monkeypatch, capsys):
    monkeypatch.setattr(pf, "run_gpu_try", lambda: (False, "CUDA error: busy or unavailable"))
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    assert pf.main(["--gpu", "--repo", "org/m"]) == pf.RC_GPU
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1].startswith("PROBE_PREFLIGHT_GPU_FAIL") and len(lines) == 4


def test_gpu_code_checks_the_matmul_result():
    assert "assert v == 4096.0" in pf.GPU_CODE and "mem_get_info" in pf.GPU_CODE
    compile(pf.GPU_CODE, "<gpu>", "exec")


def measure(rate_b_s, n_files=4, file_bytes=10**12, seconds=60, max_bytes=15e9, fail=False):
    clock = Clock()
    release = threading.Event()

    def download(f):
        if fail:
            raise OSError("401 Unauthorized")
        if file_bytes * n_files > rate_b_s * 1000:  # "large": still running at the window's end
            release.wait(5)

    disk = lambda: 0 if fail else int(min(rate_b_s * clock(), file_bytes * n_files))
    try:
        return pf.measure_download([f"f{i}" for i in range(n_files)], download, disk,
                                   lambda: None, seconds=seconds, max_bytes=max_bytes,
                                   clock=clock, sleep=clock.sleep)
    finally:
        release.set()


def test_measure_slow_host_runs_the_whole_window():
    m = measure(60e6)
    assert m["seconds"] == pytest.approx(60) and m["mb_s"] == pytest.approx(60, rel=0.02)
    assert not m["finished"]


def test_measure_fast_host_stops_at_max_bytes():
    m = measure(1e9)
    assert m["seconds"] <= 16 and m["bytes"] >= 15e9 and m["mb_s"] >= 900


def test_measure_small_download_finishes():
    m = measure(50e6, n_files=1, file_bytes=10**6)
    assert m["finished"] and m["seconds"] < 60


def test_measure_errors_are_reported():
    m = measure(0, fail=True)
    assert m["bytes"] == 0 and not m["finished"] and "401" in m["errors"][0]


def test_measure_uses_network_bytes_when_the_cache_lags():
    clock = Clock()
    rx = lambda: int(200e6 * clock())
    release = threading.Event()
    try:
        m = pf.measure_download(["a"], lambda f: release.wait(5), lambda: 0, rx, seconds=10,
                                max_bytes=1e12, clock=clock, sleep=clock.sleep)
    finally:
        release.set()
    assert m["disk_bytes"] == 0 and m["mb_s"] == pytest.approx(200, rel=0.02)


def test_net_rx_bytes_skips_loopback(tmp_path):
    p = tmp_path / "dev"
    p.write_text(
        "Inter-|   Receive\n face |bytes    packets\n"
        "    lo: 999999 10 0 0 0 0 0 0 999999 10 0 0 0 0 0 0\n"
        "  eth0: 1000 5 0 0 0 0 0 0 2000 5 0 0 0 0 0 0\n"
        "  eth1: 24 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    assert pf.net_rx_bytes(str(p)) == 1024
    assert pf.net_rx_bytes(str(tmp_path / "missing")) is None


def test_tree_bytes_counts_files(tmp_path):
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "a.incomplete").write_bytes(b"x" * 5000)
    assert 5000 <= pf.tree_bytes(tmp_path) <= 5000
    assert pf.tree_bytes(tmp_path / "none") == 0


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """Fake huggingface_hub: two weight files and a config; `cached` names cached files."""
    import huggingface_hub
    from huggingface_hub import constants as hc

    state = {"cached": set(), "list_error": None}
    sib = lambda n: types.SimpleNamespace(rfilename=n)

    class Api:
        def model_info(self, repo, revision=None, files_metadata=False):
            if state["list_error"]:
                raise state["list_error"]
            return types.SimpleNamespace(siblings=[
                sib("config.json"), sib("model-00002.safetensors"), sib("model-00001.safetensors")])

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache",
                        lambda repo, f, revision=None: "/c/" + f if f in state["cached"] else None)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *a, **k: None)
    monkeypatch.setattr(hc, "HF_HUB_CACHE", str(tmp_path))
    return state


@pytest.mark.parametrize("m,rc,tag", [
    ({"mb_s": 61.2, "finished": False, "bytes": 3_672_000_000, "errors": []}, pf.RC_SLOW,
     "PROBE_PREFLIGHT_SLOW_DOWNLOAD"),
    ({"mb_s": 420.0, "finished": False, "bytes": 15_000_000_000, "errors": []}, pf.RC_OK,
     "PROBE_PREFLIGHT_OK"),
    ({"mb_s": 20.0, "finished": True, "bytes": 1_000_000, "errors": []}, pf.RC_OK,
     "PROBE_PREFLIGHT_OK"),
    ({"mb_s": 0.0, "finished": False, "bytes": 0, "errors": ["f: 403"]}, pf.RC_ERROR,
     "PROBE_PREFLIGHT_ERROR"),
])
def test_download_check_verdicts(hub, monkeypatch, capsys, m, rc, tag):
    seen = {}

    def fake_measure(files, download, disk, rx, **kw):
        seen["files"] = files
        return {"disk_bytes": m["bytes"], "net_bytes": 0, "seconds": 60.0, **m}

    monkeypatch.setattr(pf, "measure_download", fake_measure)
    hub["cached"] = {"model-00001.safetensors"}
    assert pf.main(["--repo", "org/m", "--revision", "abc", "--min-mb-s", "150"]) == rc
    assert seen["files"] == ["model-00002.safetensors"]  # weights only, cached ones skipped
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith(tag) and "token" in out[0]


def test_download_check_all_cached_and_listing_error(hub, capsys):
    hub["cached"] = {"model-00001.safetensors", "model-00002.safetensors"}
    assert pf.main(["--repo", "org/m"]) == pf.RC_OK
    hub["list_error"] = RuntimeError("401 Client Error")
    assert pf.main(["--repo", "org/m"]) == pf.RC_ERROR
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("PROBE_PREFLIGHT_ERROR")


def test_no_gate_configured_passes(capsys):
    assert pf.main([]) == pf.RC_OK
    assert pf.main(["--repo", "org/m", "--min-mb-s", "0"]) == pf.RC_OK
    assert capsys.readouterr().out.strip().splitlines()[-1] == "PROBE_PREFLIGHT_OK"


def test_runner_rc_table_matches_the_module():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("probe_pod_rc", root / "scripts" / "probe_pod.py")
    pp = importlib.util.module_from_spec(spec)
    import sys

    sys.modules.setdefault("probe_pod_rc", pp)  # dataclasses resolve annotations through it
    spec.loader.exec_module(pp)
    assert pp.PREFLIGHT_RC == {pf.RC_GPU: "gpu", pf.RC_SLOW: "slow_download",
                               pf.RC_ERROR: "error"}


def test_tree_bytes_skips_snapshot_symlinks_and_hardlinks(tmp_path):
    """The HF cache: snapshots/<rev>/<file> are symlinks into blobs/ (review r1 finding 1)."""
    blobs, snap = tmp_path / "blobs", tmp_path / "snapshots" / "abc"
    blobs.mkdir()
    snap.mkdir(parents=True)
    (blobs / "sha1").write_bytes(b"x" * 4096)
    (snap / "model.safetensors").symlink_to(blobs / "sha1")
    import os

    os.link(blobs / "sha1", blobs / "sha1-hardlink")
    assert pf.tree_bytes(tmp_path) == 4096


@pytest.mark.parametrize("err,disk", [("f: HTTP 403 Forbidden", 5_000_000), ("f: timeout", 0)])
def test_failed_downloads_are_errors_not_slow_hosts(hub, monkeypatch, capsys, err, disk):
    """Error replies move network bytes (review r1 finding 2): judge by the cache and the
    error text, never re-roll an auth problem."""
    monkeypatch.setattr(pf, "measure_download", lambda *a, **k: {
        "bytes": max(disk, 2048), "disk_bytes": disk, "net_bytes": 2048, "seconds": 60.0,
        "mb_s": 0.1, "finished": False, "errors": [err]})
    assert pf.main(["--repo", "org/m"]) == pf.RC_ERROR
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("PROBE_PREFLIGHT_ERROR")
