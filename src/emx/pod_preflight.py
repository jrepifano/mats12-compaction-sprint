"""On-pod preflight for the generic probe runner (scripts/probe_pod.py, after setup, before run.sh).

Two optional checks that catch a bad host within minutes of boot instead of after a long model
download (wave 4, 2026-10-13: P33 lost a 32-min, 160 GB download to a B300 whose CUDA context
failed, and P33/P36 downloaded at 6-82 MB/s on hosts where P06 F7 had seen 314 MB/s):

- `--gpu`: P33's run.sh self-test, shared. In a fresh subprocess per try (a failed CUDA init is
  cached in-process): make a CUDA context, query free/total memory, run a 4096 x 4096 matmul and
  check one entry. 3 tries 20 s apart. Fails with rc 10 and `PROBE_PREFLIGHT_GPU_FAIL`.
- `--repo R --revision X --min-mb-s M`: download the repo's weight files that are not cached yet
  into the normal HF cache (the files run.sh downloads next, so nothing is wasted when run.sh uses
  the same HF_HOME), WORKERS files at a time as snapshot_download does, for up to --seconds
  (default 60) or --max-gb (default 15), and measure MB/s (10^6 bytes/s): the larger of the bytes
  that reached the repo's cache dir and the bytes the network interfaces received
  (/proc/net/dev), over the elapsed time. Below M -> rc 11 and `PROBE_PREFLIGHT_SLOW_DOWNLOAD`.
  If every remaining file finishes inside the window the download is done, which passes. A
  download error with nothing in the cache, or any 401/403/404-type error -> rc 12
  `PROBE_PREFLIGHT_ERROR` (a config or auth problem, not evidence of a bad host). `--no-xet` sets HF_HUB_DISABLE_XET=1 first, so the check measures
  the plain-HTTP path a run.sh that disables xet will use.

The last line is always one `PROBE_PREFLIGHT_*` tag. rc 0 ok, 2 usage, 10 GPU, 11 slow, 12 error.
The process ends with os._exit: download threads still running at the end of the window are
abandoned (huggingface_hub resumes or restarts their partial files later).
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RC_OK, RC_USAGE, RC_GPU, RC_SLOW, RC_ERROR = 0, 2, 10, 11, 12
GPU_TRIES = 3
GPU_WAIT_S = 20
GPU_TRY_TIMEOUT_S = 120
WORKERS = 8
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")
# a download error that says access or the file is wrong: config, not a slow host
CONFIG_ERROR_RE = re.compile(
    r"\b(401|403|404)\b|GatedRepo|RepositoryNotFound|RevisionNotFound|EntryNotFound|"
    r"Unauthorized|Forbidden", re.IGNORECASE)
GPU_CODE = (
    "import torch\n"
    "f, t = torch.cuda.mem_get_info()\n"
    "x = torch.ones(4096, 4096, device='cuda')\n"
    "v = float((x @ x)[0, 0])\n"
    "torch.cuda.synchronize()\n"
    "assert v == 4096.0, f'matmul gave {v}, expected 4096'\n"
    "print(f'free={f / 1e9:.0f}GB total={t / 1e9:.0f}GB name={torch.cuda.get_device_name(0)!r}')\n"
)


def say(msg: str) -> None:
    print(msg, flush=True)


def run_gpu_try() -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, "-c", GPU_CODE], capture_output=True, text=True,
                           timeout=GPU_TRY_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {GPU_TRY_TIMEOUT_S} s"
    out = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (out[-1] if out else f"rc {r.returncode}")


def gpu_check(try_once: Callable[[], tuple[bool, str]] | None = None,
              sleep: Callable[[float], None] | None = None, tries: int = GPU_TRIES) -> bool:
    try_once, sleep = try_once or run_gpu_try, sleep or time.sleep
    for i in range(tries):
        ok, info = try_once()
        if ok:
            say(f"preflight gpu: ok ({info})")
            return True
        say(f"preflight gpu: try {i + 1}/{tries} failed: {info}")
        if i + 1 < tries:
            sleep(GPU_WAIT_S)
    return False


def tree_bytes(root: Path) -> int:
    """Bytes written under root: per regular file min(size, allocated blocks), so a
    preallocated (sparse) file counts only what has arrived. Symlinks (the HF cache's
    snapshots/ entries point at blobs/) are skipped, and each inode is counted once."""
    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, _, files in os.walk(root):
        for name in files:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += min(st.st_size, getattr(st, "st_blocks", st.st_size // 512 + 1) * 512)
    return total


def net_rx_bytes(path: str = "/proc/net/dev") -> int | None:
    """Bytes received on every interface but lo, or None off Linux."""
    try:
        lines = Path(path).read_text().splitlines()[2:]
    except OSError:
        return None
    total = 0
    for line in lines:
        name, _, rest = line.partition(":")
        if name.strip() != "lo" and rest.split():
            total += int(rest.split()[0])
    return total


def measure_download(
    files: Iterable[str],
    download: Callable[[str], object],
    disk_bytes: Callable[[], int],
    rx_bytes: Callable[[], int | None],
    *,
    seconds: float,
    max_bytes: float,
    workers: int = WORKERS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Download `files` `workers` at a time in a background thread; sample progress every second
    until the window ends, max_bytes arrived or every file finished."""
    files = list(files)
    errors: list[str] = []
    finished = threading.Event()

    def one(f: str) -> None:
        try:
            download(f)
        except Exception as e:  # noqa: BLE001 - reported, never raised into the pool
            errors.append(f"{f}: {type(e).__name__}: {e}"[:300])

    def all_files() -> None:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, files))
        finished.set()

    d0, n0 = disk_bytes(), rx_bytes()
    t0 = clock()
    threading.Thread(target=all_files, daemon=True).start()
    while True:
        sleep(1.0)
        el = max(clock() - t0, 1e-6)
        disk = max(disk_bytes() - d0, 0)
        n1 = rx_bytes()
        net = max(n1 - n0, 0) if n0 is not None and n1 is not None else 0
        got = max(disk, net)
        if finished.is_set() or el >= seconds or got >= max_bytes:
            break
    return {"bytes": got, "disk_bytes": disk, "net_bytes": net, "seconds": round(el, 1),
            "mb_s": round(got / el / 1e6, 1), "finished": finished.is_set() and not errors,
            "errors": errors[:5]}


def download_check(repo: str, revision: str, min_mb_s: float, seconds: float,
                   max_gb: float) -> int:
    from huggingface_hub import HfApi, hf_hub_download, try_to_load_from_cache
    from huggingface_hub import constants as hc

    try:
        info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    except Exception as e:  # noqa: BLE001
        say(f"PROBE_PREFLIGHT_ERROR download: could not list {repo}@{revision}: "
            f"{type(e).__name__}: {e}"[:400])
        return RC_ERROR
    weights = sorted(s.rfilename for s in info.siblings or []
                     if s.rfilename.endswith(WEIGHT_SUFFIXES))
    todo = [f for f in weights
            if not isinstance(try_to_load_from_cache(repo, f, revision=revision), str)]
    say(f"preflight download: {repo}@{revision} has {len(weights)} weight files, "
        f"{len(todo)} not cached; HF cache {hc.HF_HUB_CACHE}; xet "
        f"{'off' if hc.HF_HUB_DISABLE_XET else 'on'}, high performance "
        f"{'on' if hc.HF_XET_HIGH_PERFORMANCE else 'off'}; token "
        f"{'set' if os.environ.get('HF_TOKEN') else 'NOT set'}")
    if not todo:
        say("PROBE_PREFLIGHT_OK download: every weight file is already cached")
        return RC_OK
    repo_dir = Path(hc.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}"
    m = measure_download(
        todo, lambda f: hf_hub_download(repo, f, revision=revision),
        lambda: tree_bytes(repo_dir), net_rx_bytes, seconds=seconds, max_bytes=max_gb * 1e9,
    )
    line = (f"mb_s={m['mb_s']} min={min_mb_s:g} bytes={m['bytes']} seconds={m['seconds']} "
            f"disk_bytes={m['disk_bytes']} net_bytes={m['net_bytes']} finished={int(m['finished'])}")
    for e in m["errors"]:
        say(f"preflight download error: {e}")
    if m["errors"] and (m["disk_bytes"] == 0
                        or any(CONFIG_ERROR_RE.search(e) for e in m["errors"])):
        # error replies still move network bytes: judge by the cache and the error text
        say(f"PROBE_PREFLIGHT_ERROR download: downloads failed ({line})")
        return RC_ERROR
    if m["finished"] or m["mb_s"] >= min_mb_s:
        say(f"PROBE_PREFLIGHT_OK download {line}")
        return RC_OK
    say(f"PROBE_PREFLIGHT_SLOW_DOWNLOAD {line}")
    return RC_SLOW


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.pod_preflight", description=__doc__)
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--revision", default="main")
    ap.add_argument("--min-mb-s", type=float, default=150.0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--max-gb", type=float, default=15.0)
    ap.add_argument("--no-xet", action="store_true")
    a = ap.parse_args(argv)
    if a.gpu and not gpu_check():
        say(f"PROBE_PREFLIGHT_GPU_FAIL no working CUDA context after {GPU_TRIES} tries")
        return RC_GPU
    if a.repo and a.min_mb_s > 0:
        if a.no_xet:
            os.environ["HF_HUB_DISABLE_XET"] = "1"  # before huggingface_hub reads its constants
        return download_check(a.repo, a.revision, a.min_mb_s, a.seconds, a.max_gb)
    say("PROBE_PREFLIGHT_OK")
    return RC_OK


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # abandon download threads still running at the end of the window
