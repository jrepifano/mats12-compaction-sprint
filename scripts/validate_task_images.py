"""Prove that no task image leaks its reference solution (brief: logs/s2/briefs/task-image-validation.md).

Images run on Jacob's desktop as a remote Docker host (`docker -H ssh://rig`); nothing is pulled
on the laptop. Every stage runs against the image *id* recorded when the probe inspected the tag,
and a result is reused only while the tag still resolves to that id and the scanner is unchanged.

Three layers per image:

1. **Deterministic probe** (`PROBE_SH`), run in a fresh container (`--rm`, `--name`, label
   `emx.sandbox=imgval`, `--network none`, `--memory 8g --cpus 2`, an in-container `timeout`).
   It fails closed: every scan step prints its exit status (`st <step> <rc>`), and a missing or
   failed step makes the image ERROR (a LEAK already found still stands).
   - Needles, chosen inside the container: up to 8 distinctive added lines of the reference
     solution (`S`), 8 names it introduces (`I`) and 5 added lines of the hidden tests (`T`),
     each the first candidates that `git grep` does NOT find in the task repo's HEAD tree, so a
     hit elsewhere is meaningful. JSON-escaped variants are searched too.
   - `git apply --check` of the solution patch, forward and reverse, per file: a file the reverse
     patch applies to (and the forward does not) already holds the solution.
   - Every git dir and loose object store on the filesystem: refs, HEAD, packed-refs,
     ORIG_HEAD / FETCH_HEAD and friends, reflogs, stash, notes, remotes, alternates, grafts,
     shallow; commits reachable from a ref or reflog but not from HEAD; `git fsck --unreachable
     --no-reflogs --lost-found`; which of those commits (merges per parent) touch a solution path
     (mapped into submodules); known solution commit ids; **every object's contents** (`git
     cat-file --batch-all-objects --batch`) searched for the needles, hits mapped back to object
     ids (stream length checked) and re-read to confirm.
   - The whole filesystem except /proc, /sys, /dev (all mounts, volumes included) searched for the
     needles; archives found by signature and decoded recursively (gzip, bzip2, xz, zip, tar, ar
     / .deb) by a Python pass; anything undecodable is a coverage gap; stray patch and
     solution-like files, and whether a patch file names a solution path.
   Verdict rules: LEAK on a solution line in a task-repo object HEAD can't reach; at one location
   (file, archive, object) 2+ solution lines, a line with 2+ new names, or 3+ new names; a known
   solution commit; a commit outside HEAD touching a solution path; the reverse patch applying;
   most solution lines already in the HEAD tree; a solution line in the image config. Lockfile
   lines (go.sum etc.) are not evidence. Review items, cleared by hand per image id in
   weak_review.json: a single needle at a location, one line plus one name, an undecodable
   archive outside toolchain/dependency fixtures, a patch file naming a solution path, a patch
   with too few distinctive lines.
2. **Image layers** (`LAYER_SH`, run on the rig over ssh): `docker save <id>`, then per layer
   tarball the `.git` paths, whiteouts and a needle search over the raw contents, and for every git
   object store inside a layer the decoded objects. Content a later layer deletes is invisible in
   the container but ships with the image.
3. **Agent red-team**: `claude -p` on the Max plan (no API key; `compaction.claude_headless`'s
   environment, auth, managed-settings and per-event route checks) with no built-in tools and one
   MCP tool, `sh`, that runs `docker -H ssh://rig exec <container> bash -lc <cmd>` (an argument
   vector, no local shell), capped at 40 calls by the tool server (counted in its log). It gets
   the task description, not the solution, and must report a location and an extraction command,
   which code re-runs in a fresh container and checks for the needles.

`verdict` (what an agent in the container can reach) is LEAK if the probe or a confirmed red-team
find says so, CLEAN only if the probe is CLEAN and the red-team ran and found nothing, else ERROR.

    uv run python scripts/validate_task_images.py list --family deepswe
    uv run python scripts/validate_task_images.py probe --family deepswe [--tasks a,b] [-j 2]
    uv run python scripts/validate_task_images.py reassess --family deepswe   (rules changed)
    uv run python scripts/validate_task_images.py layers --family deepswe [--tasks a,b]
    uv run python scripts/validate_task_images.py redteam --family deepswe [--tasks a,b] [-j 2]
    uv run python scripts/validate_task_images.py summary
    uv run python scripts/validate_task_images.py mcp-exec --container NAME --log PATH  (internal)

Families: deepswe (our -v1.1 tags), pro (jefzda originals), pro_rebuilt (fam-pro's
emx-tasks/swe-interact-pro:<repo>-<sha8>-v1), rf (scaleapi digests), rf_rebuilt (fam-rf's
emx-tasks/swe-interact-rf:<id>-v1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DOCKER_HOST = os.environ.get("IMGVAL_DOCKER_HOST", "ssh://rig")
SSH_HOST = os.environ.get("IMGVAL_SSH_HOST", "rig")
LABEL = "emx.sandbox=imgval"
DEEPSWE_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "deepswe"
MT_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "swe_interact" / "repo" / "data" / "multiturn"
RAW_OUT = REPO_ROOT / "data" / "raw" / "sprint" / "task_validation"
RES_OUT = REPO_ROOT / "results" / "sprint" / "task_validation"
FAMILIES = ("deepswe", "pro", "pro_rebuilt", "rf", "rf_rebuilt")
PRO_REBUILT_REPO = "emx-tasks/swe-interact-pro"
RF_REBUILT_REPO = "emx-tasks/swe-interact-rf"

N_SOL_NEEDLES = 8
N_TEST_NEEDLES = 5
N_IDENT_NEEDLES = 8
N_CANDIDATES = 60
MIN_SOL_NEEDLES = 3
PROBE_TIMEOUT_S = 3000
LAYER_TIMEOUT_S = 3600
# rig rules (main session, 2026-10-24 revision): start a container only while `free -g` shows
# >= 10 GB available, at most 4 of ours at once, each --memory 8g --cpus 4
MEM_LIMIT, CPU_LIMIT = "8g", "4"
MAX_OWN_CONTAINERS = 4
MIN_RIG_AVAIL_GB = 10
OOM_RC = 137
REDTEAM_MAX_CALLS = 40
# Sonnet to spare the Max weekly window (main session, 2026-10-24); the first run, wasmi, was
# Opus 5.5 and is kept as recorded. Each row records its model.
REDTEAM_MODEL = os.environ.get("IMGVAL_REDTEAM_MODEL", "claude-sonnet-5-5")
REDTEAM_TIMEOUT_S = 2400
REDTEAM_MIN_CALLS = 10  # a "not found" after fewer calls is too shallow to count
MAX_WINDOW_LIMIT = 0.90  # Jacob: hold new Max work at 90% of the 7-day window (2026-10-24)
REDTEAM_NUDGES = 3  # times a too-early "not found" is sent back (same session, --resume)
EXEC_OUTPUT_CAP = 20_000
ARCHIVE_MEMBER_CAP = 512 * 1024 * 1024
# coverage gaps (undecodable archives) here are toolchain fixtures, reported but not ERROR
# Undecodable archives exempt from review: toolchain source trees, and test fixtures inside
# dependency sources. Anything else (the task repo's own fixtures included) needs a recorded review.
BENIGN_GAP = re.compile(r"^(/usr/local/go/|/usr/lib/go[^/]*/|/usr/(local/)?lib/python3[^/]*/test/"
                        r"|/root/\.rustup/toolchains/|/usr/local/rustup/toolchains/)"
                        r"|/(go/pkg/mod|\.cargo/registry|node_modules|site-packages|dist-packages)/"
                        r".*/(tests?|testdata|testing|fixtures?|functest|__tests__)/")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def docker(*args: str) -> list[str]:
    return ["docker", "-H", DOCKER_HOST, *args]


_TRANSPORT = re.compile(r"dial-stdio|Cannot connect to the Docker daemon|error during connect|"
                        r"Connection (refused|reset|closed)|kex_exchange_identification")


def docker_q(*args: str, timeout: int = 120, tries: int = 6) -> subprocess.CompletedProcess:
    """A short docker query that rides out ssh transport drops to the rig (retry with backoff,
    about 3 minutes in all) instead of reporting them as a missing image or container."""
    r = None
    for i in range(tries):
        try:
            r = subprocess.run(docker(*args), capture_output=True, text=True, timeout=timeout,
                               check=False)
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(args, 124, "", "timeout")
        if r.returncode == 0 or not _TRANSPORT.search(r.stderr or "") and r.returncode != 124:
            return r
        time.sleep(min(60, 10 * (i + 1)))
    raise RuntimeError(f"rig unreachable: docker {' '.join(args[:2])}: {(r.stderr or '')[-200:]}")


# ------------------------------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------------------------------


def _deepswe_task_ids() -> dict[str, list[str]]:
    """Task id -> the sets it belongs to: the 25 SWE-Interact DeepSWE tasks, the 6 pilot tasks,
    the 6 held-out tasks and the 4 pilot replacements (S2 task set, compaction.s2.tasks)."""
    from compaction.s2 import tasks as T

    rows = T.ranking(DEEPSWE_DIR)
    sets: dict[str, list[str]] = {}
    for name, ids in (
        ("swe_interact", sorted(p.name[len("deepswe_"):] for p in MT_DIR.glob("deepswe_*"))),
        ("pilot", [r["task"] for r in T.available(rows)[: T.N_PILOT_TASKS]]),
        ("heldout", [r["task"] for r in T.heldout(rows)]),
        ("replacement", [r["task"] for r in T.replacements(rows)]),
    ):
        for t in ids:
            sets.setdefault(t, []).append(name)
    return sets


def _pro_solution_shas(instance_id: str) -> list[str]:
    # instance_<org>__<repo>-<40-hex solution commit>-v<40-hex>; the v-prefixed id is not a commit
    return re.findall(r"-([0-9a-f]{40})(?=-v|$)", instance_id)


def pro_rebuilt_tag(cfg: dict) -> str:
    """fam-pro's rebuilt tag: emx-tasks/swe-interact-pro:<repo>-<sha8>-v1."""
    shas = _pro_solution_shas(cfg["instance_id"])
    repo = cfg["repo"].split("/")[-1].lower()
    return f"{PRO_REBUILT_REPO}:{repo}-{shas[0][:8]}-v1"


FAMILY_IMAGES = {  # the family workers' lists of built images (tag + image id)
    "pro_rebuilt": REPO_ROOT / "results" / "sprint" / "families" / "pro" / "images.json",
    "rf_rebuilt": REPO_ROOT / "results" / "sprint" / "families" / "rf" / "images.json",
}


def built_images(family: str) -> dict[str, str]:
    """tag -> image id the family worker recorded; empty before the list exists."""
    p = FAMILY_IMAGES.get(family)
    if not p or not p.exists():
        return {}
    tasks = json.loads(p.read_text()).get("tasks") or {}
    return {v["image"]: v.get("image_id") for v in tasks.values() if v.get("image")}


def load_family(family: str) -> list[dict]:
    """[{family, task, image, solution, tests, shas, base, instruction, sets, solution_src}].
    The rebuilt families hold only the images their worker lists as built."""
    out = []
    if family == "deepswe":
        for t, sets in sorted(_deepswe_task_ids().items()):
            d = DEEPSWE_DIR / "repo" / "tasks" / t
            toml = tomllib.loads((d / "task.toml").read_text())
            tp = d / "tests" / "test.patch"
            out.append({
                "family": family, "task": t, "sets": sets,
                "image": toml["environment"]["docker_image"],
                "solution": (d / "solution" / "solution.patch").read_text(errors="replace"),
                "tests": tp.read_text(errors="replace") if tp.exists() else "",
                "shas": [], "base": toml["metadata"].get("base_commit_hash"),
                "instruction": (d / "instruction.md").read_text(),
                "solution_src": str((d / "solution" / "solution.patch").relative_to(REPO_ROOT)),
            })
    elif family in ("pro", "pro_rebuilt"):
        for d in sorted(MT_DIR.glob("swebenchpro_*")):
            cfg = json.loads((d / "tests" / "config.json").read_text())
            from_line = next(ln for ln in (d / "environment" / "Dockerfile").read_text().splitlines()
                             if ln.startswith("FROM "))
            image = pro_rebuilt_tag(cfg) if family == "pro_rebuilt" else from_line.split()[1]
            out.append({
                "family": family, "task": d.name[len("swebenchpro_"):], "sets": ["swe_interact"],
                "image": image, "solution": cfg["patch"], "tests": cfg.get("test_patch") or "",
                "shas": _pro_solution_shas(cfg["instance_id"]), "base": cfg.get("base_commit"),
                "instruction": (d / "instruction.md").read_text(),
                "solution_src": str((d / "tests" / "config.json").relative_to(REPO_ROOT))
                + " [patch]",
            })
    elif family in ("rf", "rf_rebuilt"):
        for d in sorted(MT_DIR.glob("rf_*")):
            toml = tomllib.loads((d / "task.toml").read_text())
            image = (toml.get("environment") or {}).get("docker_image") or next(
                ln.split()[1] for ln in (d / "environment" / "Dockerfile").read_text().splitlines()
                if ln.startswith("FROM "))
            task = d.name[len("rf_"):]
            if family == "rf_rebuilt":
                image = f"{RF_REBUILT_REPO}:{task[len('task-'):]}-v1"
            tp = d / "tests" / "test_patch.diff"
            out.append({
                "family": family, "task": task, "sets": ["swe_interact"], "image": image,
                "solution": (d / "solution" / "gold.patch").read_text(errors="replace"),
                "tests": tp.read_text(errors="replace") if tp.exists() else "",
                "shas": [], "base": None, "instruction": (d / "instruction.md").read_text(),
                "solution_src": str((d / "solution" / "gold.patch").relative_to(REPO_ROOT)),
            })
    else:
        raise SystemExit(f"unknown family {family!r}")
    if family in FAMILY_IMAGES:
        built = built_images(family)
        out = [t for t in out if t["image"] in built]
        for t in out:
            t["expected_image_id"] = built[t["image"]]
    return out


# ------------------------------------------------------------------------------------------
# Needles
# ------------------------------------------------------------------------------------------

_DIFF_HDR = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,}")
_IMPORT = re.compile(r"(from\s+\S+\s+import|import\s|use\s|#include|require\(|package\s|"
                     r"export\s+\*)")
_SKIP_PREFIX = ("+++", "---", "@@", "index ", "new file", "deleted file", "similarity", "rename ",
                "old mode", "new mode", "Binary files", "GIT binary patch")


def patch_paths(patch: str) -> list[str]:
    out: list[str] = []
    for line in patch.splitlines():
        m = _DIFF_HDR.match(line)
        if m:
            for p in (m.group(2), m.group(1)):
                if p not in out and p != "/dev/null":
                    out.append(p)
    return out


def _drop_substrings(items: list[str]) -> list[str]:
    return [s for s in items if not any(s != o and s in o for o in items)]


def candidate_needles(patch: str, limit: int = N_CANDIDATES) -> list[str]:
    """Distinctive added lines of a unified diff, best first: stripped '+' lines of at least 24
    characters (14 for short patches) holding an identifier, not equal to any removed or context
    line of the patch, capped at 120 characters, none a substring of another (grep -o would hide
    it behind the longer match). Import lines rank last; taken round-robin across files."""
    per_file: dict[str, list[str]] = {}
    seen_other: set[str] = set()
    cur = None
    for line in patch.splitlines():
        m = _DIFF_HDR.match(line)
        if m:
            cur = m.group(2)
            per_file.setdefault(cur, [])
            continue
        if cur is None or line.startswith(_SKIP_PREFIX):
            continue
        if line.startswith("+"):
            per_file[cur].append(line[1:].strip())
        else:
            seen_other.add(line[1:].strip() if line[:1] in (" ", "-") else line.strip())
    n_long = sum(1 for lines in per_file.values() for s in lines if len(s) >= 24)
    min_len = 24 if n_long >= 15 else 14
    ranked: dict[str, list[str]] = {}
    for f, lines in per_file.items():
        good: list[str] = []
        for s in lines:
            s = s[:120].strip()
            if (len(s) >= min_len and _IDENT.search(s) and s not in seen_other
                    and "__IMGVAL" not in s and s.isprintable() and s not in good):
                good.append(s)
        ranked[f] = sorted(good, key=lambda x: (bool(_IMPORT.match(x)), -len(set(x)), -len(x), x))
    out: list[str] = []
    while any(ranked.values()) and len(out) < limit * 3:
        for f in list(ranked):
            if ranked[f]:
                s = ranked[f].pop(0)
                if s not in out:
                    out.append(s)
    return _drop_substrings(out)[:limit]


def candidate_identifiers(patch: str, limit: int = 40) -> list[str]:
    """Names the solution introduces: identifiers of 10+ characters on added lines that appear on
    no removed or context line, longest first, none a substring of another. A second detector
    that survives reformatting."""
    added: list[str] = []
    other: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "diff --git", "index ", "@@")):
            continue
        toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{9,}", line[1:] if line[:1] in "+- " else line)
        if line.startswith("+"):
            added += [t for t in toks if t not in added]
        else:
            other.update(toks)
    cands = [t for t in added if t not in other and "__IMGVAL" not in t]
    return _drop_substrings(sorted(cands, key=lambda t: (-len(t), t)))[:limit]


LOCKFILE = re.compile(r"(^|/)(go\.sum|go\.work\.sum|go\.mod|package-lock\.json|yarn\.lock|"
                      r"pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|uv\.lock|Pipfile\.lock|"
                      r"Gemfile\.lock|composer\.lock|requirements[^/]*\.txt|\.yarn/.*)$")


def lockfile_texts(patch: str) -> set[str]:
    """Added lines and tokens that occur only in dependency lockfile hunks (go.sum, lockfiles):
    they recur verbatim in unrelated packages' own lockfiles, so they are not evidence."""
    lock, other, cur = set(), set(), None
    for line in patch.splitlines():
        m = _DIFF_HDR.match(line)
        if m:
            cur = m.group(2)
            continue
        if cur is None or not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:].strip()
        toks = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{9,}", body)) | {body[:120].strip()}
        (lock if LOCKFILE.search(cur) else other).update(toks)
    return lock - other


def json_escape(s: str) -> str:
    return json.dumps(s)[1:-1]


# ------------------------------------------------------------------------------------------
# Layer 1: in-container probe
# ------------------------------------------------------------------------------------------

# Archive scanner, run in the container with python3 (any 3.6+): one walk, archives found by
# signature, decoded recursively; prints `archhit\t<path>\t<id>`, `gap\t<path>\t<reason>`, `n ...`.
ARCH_PY = r'''
import bz2, gzip, io, lzma, os, sys, tarfile, zipfile
mode = sys.argv[1]
if mode == "walk":
    root, nfile, prunes, cap = sys.argv[2], sys.argv[3], sys.argv[4].split("\x1f"), int(sys.argv[5])
else:  # "git": the `git cat-file --batch` stream on stdin
    nfile, cap = sys.argv[2], int(sys.argv[3])
needles = []
for ln in open(nfile, "rb").read().split(b"\n"):
    if b"\t" in ln:
        k, v = ln.split(b"\t", 1)
        if v:
            needles.append((k.decode(), v))
stats = {"files": 0, "archives": 0, "decoded": 0, "gaps": 0, "errors": 0, "objects": 0}
def out(*a):
    sys.stdout.write("\t".join(a) + "\n")
def kind(b):
    if b[:2] == b"\x1f\x8b": return "gzip"
    if b[:4] in (b"PK\x03\x04", b"PK\x05\x06"): return "zip"
    if b[:3] == b"BZh": return "bz2"
    if b[:6] == b"\xfd7zXZ\x00": return "xz"
    if b[:4] == b"\x28\xb5\x2f\xfd": return "zstd"
    if b[:6] == b"7z\xbc\xaf\x27\x1c": return "7z"
    if b[:4] == b"\xed\xab\xee\xdb": return "rpm"
    if b[:8] == b"!<arch>\n": return "ar"
    return None
def is_tar(b):
    """ustar/GNU magic, or an old (v7) header whose checksum is right."""
    if len(b) > 262 and b[257:262] == b"ustar":
        return True
    if len(b) < 512 or b[0] == 0:
        return False
    try:
        stored = int(b[148:156].replace(b"\0", b" ").strip() or b"x", 8)
    except ValueError:
        return False
    return stored == sum(b[:148]) + 256 + sum(b[156:512])
def search(data, where):
    for k, v in needles:
        if v in data:
            out("archhit", where, k)
def gap(where, why):
    stats["gaps"] += 1
    out("gap", where, why)
def decode(data, where, depth):
    stats["archives"] += 1
    k = kind(data[:8])
    if k is None and is_tar(data[:512] + b"\0" * max(0, 512 - len(data))):
        stats["decoded"] += 1
        inner(data, where, depth)
        return
    try:
        if k in ("gzip", "bz2", "xz"):
            mod = {"gzip": gzip, "bz2": bz2, "xz": lzma}[k]
            raw = mod.decompress(data)
            if len(raw) > cap:
                raise ValueError("decoded size over cap")
            stats["decoded"] += 1
            search(raw, where)
            inner(raw, where, depth)
        elif k == "zip":
            z = zipfile.ZipFile(io.BytesIO(data))
            for info in z.infolist():
                if info.filename.endswith("/"):
                    continue
                w = where + "!" + info.filename
                if info.file_size > cap:
                    gap(w, "member over cap"); continue
                try:
                    m = z.read(info)
                except Exception as e:
                    gap(w, "zip member: %s" % type(e).__name__); continue
                search(m, w)
                inner(m, w, depth)
            stats["decoded"] += 1
        elif k == "ar":
            pos = 8
            while pos + 60 <= len(data):
                hdr = data[pos:pos + 60]
                name = hdr[:16].decode("latin-1").strip()
                size = int(hdr[48:58].decode("latin-1").strip() or 0)
                body = data[pos + 60:pos + 60 + size]
                search(body, where + "!" + name)
                inner(body, where + "!" + name, depth)
                pos += 60 + size + (size % 2)
            stats["decoded"] += 1
        else:
            gap(where, "no decoder for %s" % k)
    except Exception as e:
        gap(where, "%s: %s" % (k, type(e).__name__))
def inner(raw, where, depth):
    if not (kind(raw[:8]) or is_tar(raw[:512])):
        return
    if depth >= 4:
        gap(where, "nesting too deep"); return
    if kind(raw[:8]):
        decode(raw, where + "!", depth + 1)
        return
    try:
        t = tarfile.open(fileobj=io.BytesIO(raw))
        for m in t.getmembers():
            if not m.isfile():
                continue
            f = t.extractfile(m)
            if f is None:
                continue
            b = f.read(cap + 1)
            if len(b) > cap:
                gap(where + "!" + m.name, "member over cap"); continue
            inner(b, where + "!" + m.name, depth + 1)
    except Exception as e:
        gap(where, "tar: %s" % type(e).__name__)
def skip(inp, n):
    while n > 0:
        got = inp.read(min(n, 1 << 20))
        if not got:
            raise EOFError("short object stream")
        n -= len(got)
MAXN = max([len(v) for _, v in needles] or [1])
def hits_in(data):
    return [k for k, v in needles if v in data]
def chunk_hits(fh):
    """Needles in a stream too large to hold, read in chunks that overlap by a needle length."""
    found, tail = set(), b""
    while True:
        chunk = fh.read(64 << 20)
        if not chunk:
            break
        buf = tail + chunk
        found.update(hits_in(buf))
        tail = buf[-(MAXN - 1):] if MAXN > 1 else b""
    return [k for k, _ in needles if k in found]
class Limited:
    def __init__(self, f, n):
        self.f, self.n = f, n
    def read(self, k):
        if self.n <= 0:
            return b""
        b = self.f.read(min(k, self.n))
        self.n -= len(b)
        return b
if mode == "walk":
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if os.path.join(dp, d) not in prunes]
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            stats["files"] += 1
            try:
                with open(p, "rb") as fh:
                    size = os.fstat(fh.fileno()).st_size
                    head = fh.read(512)
                    arch = bool(kind(head[:8]) or is_tar(head) or fn.endswith(".tar"))
                    fh.seek(0)
                    if arch and size <= cap:
                        data = fh.read()
                        found = hits_in(data)
                    else:
                        if arch:
                            gap(p, "file over cap")
                        data, found = None, chunk_hits(fh)
            except Exception as e:
                stats["errors"] += 1
                out("readerr", p, type(e).__name__)
                continue
            stats["bytes"] = stats.get("bytes", 0) + size
            for k in found:
                out("fshit", k, "1", p)
            if data is not None:
                decode(data, p, 0)
else:
    inp = sys.stdin.buffer
    nbytes = 0
    while True:
        hdr = inp.readline()
        if not hdr:
            break
        parts = hdr.split()
        if len(parts) != 3:
            out("gap", "git:stream", "bad header"); stats["errors"] += 1; break
        oid, typ, size = parts[0].decode(), parts[1].decode(), int(parts[2])
        stats["objects"] = stats.get("objects", 0) + 1
        nbytes += len(hdr) + size + 1
        if size <= cap:
            body = inp.read(size)
            if len(body) != size:
                raise EOFError("short object stream")
            for k in hits_in(body):
                out("objhit", oid, typ, k)
            if typ == "blob" and (kind(body[:8]) or is_tar(body[:512] + b"\0" * max(0, 512 - len(body)))):
                decode(body, "git:" + oid, 0)
        else:
            lim = Limited(inp, size)
            head = inp.read(512)
            lim.n -= len(head)
            rest = chunk_hits(lim)
            for k in sorted(set(hits_in(head)) | set(rest)):
                out("objhit", oid, typ, k)
            if typ == "blob" and (kind(head[:8]) or is_tar(head)):
                gap("git:" + oid, "object over cap")
        inp.read(1)
    stats["bytes"] = nbytes
for k in sorted(stats):
    out("n", k, str(stats[k]))
'''

PROBE_SH = r'''
set -u
export LC_ALL=C
R="${IMGVAL_ROOT:-/}"; R="${R%/}"; RD="${R:-/}"
W="${IMGVAL_WORK:-$(mktemp -d /tmp/imgval.XXXXXX)}"
mkdir -p "$W"
NSOL=__NSOL__; NTEST=__NTEST__; NIDENT=__NIDENT__; BASE=__BASE__
sec() { printf '\n@@SECTION %s\n' "$*"; }
st() { printf 'st %s %s\n' "$1" "$2"; }
cat > "$W/cands_sol" <<'__IMGVAL_EOF__'
__CANDS_SOL__
__IMGVAL_EOF__
cat > "$W/cands_test" <<'__IMGVAL_EOF__'
__CANDS_TEST__
__IMGVAL_EOF__
cat > "$W/cands_ident" <<'__IMGVAL_EOF__'
__CANDS_IDENT__
__IMGVAL_EOF__
cat > "$W/paths" <<'__IMGVAL_EOF__'
__PATHS__
__IMGVAL_EOF__
cat > "$W/shas" <<'__IMGVAL_EOF__'
__SHAS__
__IMGVAL_EOF__
cat > "$W/arch.py" <<'__IMGVAL_EOF__'
__ARCH_PY__
__IMGVAL_EOF__

cat > "$W/sol.patch" <<'__PATCH_DELIM__'
__PATCH__
__PATCH_DELIM__
for f in cands_sol cands_test cands_ident paths shas; do sed -i.bak '/^$/d' "$W/$f"; rm -f "$W/$f.bak"; done
# find over every mount except /proc, /sys, /dev and the work dir
ffind() { find "$RD" \( -path "$W" -o -path "$R/proc" -o -path "$R/sys" -o -path "$R/dev" \) -prune -o "$@"; }

sec tools
for t in git grep find awk sort sed head wc cut xargs tee python3 tar uniq; do
  printf '%s %s\n' "$t" "$(command -v $t 2>/dev/null || echo MISSING)"
done
git --version 2>&1
id 2>&1

sec mounts
cat /proc/mounts 2>/dev/null | awk '{print $2, $3}'

sec gitdirs
ffind -type f -name HEAD -print 2>"$W/err_gitdirs" | while IFS= read -r h; do
  d=$(dirname "$h")
  if [ -d "$d/objects" ] && { [ -d "$d/refs" ] || [ -f "$d/packed-refs" ]; }; then echo "$d"; fi
done | sort -u > "$W/gitdirs"
st find_gitdirs "$(wc -l < "$W/err_gitdirs" | tr -d ' ')"
sed 's/^/dir /' "$W/gitdirs"
# object stores that are not inside a discovered git dir (copied .git/objects, caches)
ffind -type d -name objects -print 2>/dev/null | while IFS= read -r o; do
  [ -d "$o/pack" ] || [ -d "$o/info" ] || ls "$o" 2>/dev/null | grep -q -E '^[0-9a-f]{2}$' || continue
  p=$(dirname "$o"); grep -q -x -F -e "$p" "$W/gitdirs" && continue
  echo "$o"
done > "$W/orphan_objs"
sed 's/^/orphan /' "$W/orphan_objs"

# the task repo: the git dir whose worktree holds most of the solution's paths
sec main
best=""; bestn=-1
while IFS= read -r G; do
  [ "$(basename "$G")" = ".git" ] || continue
  wt=$(dirname "$G"); n=0
  while IFS= read -r p; do [ -e "$wt/$p" ] && n=$((n+1)); done < "$W/paths"
  echo "score $n $G"
  if [ "$n" -gt "$bestn" ]; then best="$G"; bestn=$n; fi
done < "$W/gitdirs"
echo "main $best"
echo "main_score $bestn"
MAIN="$best"; MWT=""
[ -n "$MAIN" ] && MWT=$(dirname "$MAIN")
gm() { git -c safe.directory='*' --git-dir="$MAIN" "$@"; }
if [ -n "$MAIN" ]; then
  echo "head $(gm rev-parse HEAD 2>&1 | head -1)"
  echo "base $BASE"
  if [ -n "$BASE" ]; then echo "base_present $(gm cat-file -t "$BASE" 2>/dev/null || echo absent)"; fi
fi

# needles: the first candidates absent from the task repo's HEAD tree; S, then I, then T, and
# none containing an earlier one (grep -o reports only the longer of two overlapping matches)
sec needles
: > "$W/needles"; : > "$W/nd"
if [ -n "$MAIN" ]; then
  gm rev-parse -q --verify HEAD >/dev/null 2>&1; st head_valid $?
  cat "$W/cands_sol" "$W/cands_test" > "$W/cands_all"
  gm grep -h -o -F -f "$W/cands_all" HEAD -- > "$W/in_head_any" 2>"$W/err_ghead"; r=$?
  if [ "$r" -le 1 ] && [ ! -s "$W/err_ghead" ]; then st git_grep_head 0; else st git_grep_head "$r"; fi
  sort -u -o "$W/in_head_any" "$W/in_head_any"
  echo "cands_sol $(wc -l < "$W/cands_sol" | tr -d ' ')"
  echo "cands_sol_in_head $(grep -c -x -F -f "$W/in_head_any" "$W/cands_sol" 2>/dev/null)"
  echo "cands_test $(wc -l < "$W/cands_test" | tr -d ' ')"
  echo "cands_test_in_head $(grep -c -x -F -f "$W/in_head_any" "$W/cands_test" 2>/dev/null)"
  pick() {  # pick <cands file> <prefix> <n> <file of earlier needles it must not contain>
    k=0
    while IFS= read -r n; do
      [ "$k" -ge "$3" ] && break
      if [ -s "$W/$4" ] && printf '%s\n' "$n" | grep -q -F -f "$W/$4"; then continue; fi
      if grep -q -x -F -e "$n" "$W/in_head_any" 2>/dev/null; then continue; fi
      gm grep -q -F -e "$n" HEAD -- 2>/dev/null; r=$?
      if [ "$r" -eq 0 ]; then continue; fi
      if [ "$r" -ne 1 ]; then st git_grep_pick "$r"; continue; fi
      k=$((k+1)); printf '%s%d\t%s\n' "$2" "$k" "$n" >> "$W/needles"; printf '%s\n' "$n" >> "$W/nd"
      printf '%s\n' "$n" >> "$W/nd_$2"
      e=$(printf '%s' "$n" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g')
      if [ "$e" != "$n" ]; then printf '%s%d\t%s\n' "$2" "$k" "$e" >> "$W/needles"; printf '%s\n' "$e" >> "$W/nd"; fi
    done < "$1"
  }
  : > "$W/nd_S"; : > "$W/nd_I"; : > "$W/nd_T"
  pick "$W/cands_sol" S "$NSOL" nd
  pick "$W/cands_ident" I "$NIDENT" nd
  pick "$W/cands_test" T "$NTEST" nd_S
fi
cat "$W/needles"
NND=$(wc -l < "$W/nd" | tr -d ' ')

# does the solution patch apply forward (base state) or in reverse (already applied)?
sec apply
if [ -n "$MAIN" ] && [ -s "$W/sol.patch" ]; then
  ( cd "$MWT" && git -c safe.directory='*' apply --check "$W/sol.patch" ) >/dev/null 2>&1; echo "fwd_all $?"
  ( cd "$MWT" && git -c safe.directory='*' apply -R --check "$W/sol.patch" ) >/dev/null 2>&1; echo "rev_all $?"
  echo "n_paths $(wc -l < "$W/paths" | tr -d ' ')"
  while IFS= read -r p; do
    ( cd "$MWT" && git -c safe.directory='*' apply --check --include="$p" "$W/sol.patch" ) >/dev/null 2>&1; f=$?
    ( cd "$MWT" && git -c safe.directory='*' apply -R --check --include="$p" "$W/sol.patch" ) >/dev/null 2>&1; b=$?
    printf 'file %s %s %s\n' "$f" "$b" "$p"
  done < "$W/paths"
fi

scan_objects() {  # scan_objects <git args...>: every object's content (and archives) vs needles
  git "$@" cat-file --batch-all-objects --batch-check='%(objectname) %(objecttype) %(objectsize)' > "$W/objs" 2>"$W/err_bc"
  st batch_check $?
  echo "n_objects $(wc -l < "$W/objs" | tr -d ' ')"
  echo "expected_bytes $(awk '{s += length($0) + 1 + $3 + 1} END {printf "%.0f", s}' "$W/objs")"  # %d overflows at 2^31
  git "$@" cat-file --batch-all-objects --batch 2>"$W/err_b" \
    | python3 "$W/arch.py" git "$W/needles" __CAP__ > "$W/gitscan"
  ps=("${PIPESTATUS[@]}")
  st batch "${ps[0]}"
  st gitscan "${ps[1]}"
  sed 's/^/ga\t/' "$W/gitscan"
  { grep '^objhit	' "$W/gitscan" | cut -f2; grep '^archhit	git:' "$W/gitscan" | cut -f2 | sed 's/^git://; s/!.*//'; } | sort -u > "$W/hitids"
  # paths of objects whose archives could not be decoded, for the fixture rule
  grep '^gap	git:' "$W/gitscan" | cut -f2 | sed 's/^git://; s/!.*//' | grep -v '^stream$' | sort -u > "$W/gapids"
  if [ -s "$W/gapids" ]; then
    git "$@" rev-list --objects --all --reflog 2>/dev/null | grep -F -f "$W/gapids" | sed 's/^/gappath /'
  fi
}

# git: every git dir
while IFS= read -r G; do
  g() { git -c safe.directory='*' --git-dir="$G" "$@"; }
  sec "git $G"
  echo "head $(g rev-parse HEAD 2>&1 | head -1)"
  echo "symref $(g symbolic-ref -q HEAD 2>&1)"
  echo "is_main $([ "$G" = "$MAIN" ] && echo 1 || echo 0)"
  if [ -f "$G/shallow" ]; then echo "shallow $(wc -l < "$G/shallow" | tr -d ' ')"; else echo "shallow 0"; fi
  [ -s "$G/objects/info/alternates" ] && sed 's/^/alternates /' "$G/objects/info/alternates"
  [ -s "$G/info/grafts" ] && echo "grafts $(wc -l < "$G/info/grafts" | tr -d ' ')"
  echo "n_commits_all $(g rev-list --all --count 2>/dev/null)"
  echo "n_commits_all_reflog $(g rev-list --all --reflog --count 2>/dev/null)"
  g count-objects -v 2>/dev/null | sed 's/^/objects_/'
  sec "refs $G"
  g for-each-ref --format='%(refname) %(objectname) %(objecttype)' 2>&1; st for_each_ref $?
  sec "special $G"
  for f in ORIG_HEAD FETCH_HEAD MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD AUTO_MERGE BISECT_LOG; do
    [ -e "$G/$f" ] && { echo "== $f"; head -50 "$G/$f"; }
  done
  if [ -f "$G/packed-refs" ]; then echo "== packed-refs $(wc -l < "$G/packed-refs" | tr -d ' ')"; head -200 "$G/packed-refs"; fi
  sec "reflog $G"
  if [ -d "$G/logs" ]; then
    find "$G/logs" -type f | while IFS= read -r f; do
      echo "== ${f#$G/} $(wc -l < "$f" | tr -d ' ')"; head -100 "$f"
    done
  fi
  sec "stash $G"
  g stash list 2>/dev/null | head -50
  sec "remotes $G"
  g config --get-regexp '^remote\.' 2>/dev/null
  sec "shas $G"
  while IFS= read -r s; do echo "$s $(g cat-file -t "$s" 2>/dev/null || echo absent)"; done < "$W/shas"
  sec "offhead $G"
  if g rev-parse -q --verify HEAD >/dev/null 2>&1; then
    g rev-list --all --reflog --not HEAD > "$W/offhead" 2>"$W/err_off"; st rev_list_offhead $?
  else
    g rev-list --all --reflog > "$W/offhead" 2>"$W/err_off"; st rev_list_offhead $?
  fi
  echo "count $(wc -l < "$W/offhead" | tr -d ' ')"
  head -100 "$W/offhead"
  sec "fsck $G"
  g fsck --no-progress --unreachable --no-reflogs --lost-found > "$W/fsck" 2> "$W/fsck_err"
  st fsck $?
  echo "unreachable $(grep -c '^unreachable' "$W/fsck")"
  for t in commit tree blob tag; do echo "unreachable_$t $(grep -c "^unreachable $t" "$W/fsck")"; done
  echo "lost_found $(find "$G/lost-found" -type f 2>/dev/null | wc -l | tr -d ' ')"
  head -100 "$W/fsck"
  echo "== stderr"; head -20 "$W/fsck_err"
  sec "touch $G"
  # solution paths as this repo sees them: as-is, plus prefix-stripped for a submodule whose
  # worktree sits under the task repo's worktree
  cp "$W/paths" "$W/gpaths"
  swt=$(g config core.worktree 2>/dev/null)
  if [ -n "$swt" ] && [ -n "$MWT" ]; then
    sabs=$(cd "$G" 2>/dev/null && cd "$swt" 2>/dev/null && pwd -P)
    mabs=$(cd "$MWT" && pwd -P)
    case "$sabs" in "$mabs"/*) pre="${sabs#$mabs/}/"; echo "submodule_prefix $pre"
      while IFS= read -r p; do case "$p" in "$pre"*) echo "${p#$pre}";; esac; done < "$W/paths" >> "$W/gpaths";;
    esac
  fi
  { cat "$W/offhead"; awk '$1=="unreachable" && $2=="commit" {print $3}' "$W/fsck"; } | sort -u > "$W/xcommits"
  echo "xcommits $(wc -l < "$W/xcommits" | tr -d ' ')"
  if [ -s "$W/xcommits" ] && [ -s "$W/gpaths" ]; then
    g diff-tree --stdin -r --root -m --name-only < "$W/xcommits" > "$W/dtree" 2>"$W/err_dt"; st diff_tree $?
    awk -v P="$W/gpaths" 'BEGIN{while((getline p < P)>0) want[p]=1}
           /^[0-9a-f]+( [0-9a-f]+)*( \(from [0-9a-f]+\))?$/ && length($1) >= 40 {c=$1; next}
           ($0 in want) {print "touch", c, $0}' "$W/dtree" | sort -u > "$W/touch"
    echo "touching $(cut -d' ' -f2 "$W/touch" | sort -u | wc -l | tr -d ' ')"
    head -50 "$W/touch"
    cut -d' ' -f2 "$W/touch" | sort -u | head -10 | while IFS= read -r c; do
      echo "commit $(g log -1 --format='%H %cI %s' "$c" 2>/dev/null)"
    done
  fi
  if [ "$NND" -gt 0 ]; then
    sec "objects $G"
    scan_objects -c safe.directory='*' --git-dir="$G"
    if [ -s "$W/hitids" ]; then
      sec "objpaths $G"
      g rev-list --objects --all --reflog 2>/dev/null | grep -F -f "$W/hitids"
      sec "objhead $G"
      if g rev-parse -q --verify HEAD >/dev/null 2>&1; then
        g rev-list --objects HEAD 2>/dev/null | cut -d' ' -f1 | grep -x -F -f "$W/hitids"
      fi
    fi
  fi
  if [ "$(basename "$G")" = ".git" ]; then
    sec "status $G"
    git -c safe.directory='*' --git-dir="$G" --work-tree="$(dirname "$G")" status --porcelain --untracked-files=all 2>&1 | head -300
  fi
done < "$W/gitdirs"

# object stores outside any git dir: read them through an empty repo
if [ "$NND" -gt 0 ] && [ -s "$W/orphan_objs" ]; then
  git init -q --bare "$W/empty.git" 2>/dev/null
  while IFS= read -r O; do
    sec "orphanobjects $O"
    GIT_OBJECT_DIRECTORY="$O" scan_objects -c safe.directory='*' --git-dir="$W/empty.git"
  done < "$W/orphan_objs"
fi

sec fs
ffind -type f -print0 2>"$W/err_find" > "$W/files0"; st find_files $?
echo "n_files $(tr -cd '\000' < "$W/files0" | wc -c | tr -d ' ')"
echo "find_errors $(wc -l < "$W/err_find" | tr -d ' ')"
head -20 "$W/err_find" | sed 's/^/find_err /'
if [ "$NND" -gt 0 ]; then
  python3 "$W/arch.py" walk "$RD" "$W/needles" "$W$(printf '\037')$R/proc$(printf '\037')$R/sys$(printf '\037')$R/dev" __CAP__ > "$W/walk" 2>"$W/err_walk"
  r=$?
  sec fshits
  grep '^fshit	' "$W/walk"
  st fs_scan "$r"
  sec archives
  grep -v '^fshit	' "$W/walk"
  st arch_py "$r"
  head -20 "$W/err_walk" | sed 's/^/walk_err /'
fi

sec stray
ffind -type f \( -name '*.patch' -o -name '*.diff' -o -name '*.orig' -o -name '*.rej' \
    -o -iname '*solution*' -o -iname 'gold*' -o -iname 'solve.sh' -o -iname '*oracle*' \) -print 2>/dev/null > "$W/stray"
echo "n_stray $(wc -l < "$W/stray" | tr -d ' ')"
sed 's/^/b\//' "$W/paths" > "$W/diffpaths"
while IFS= read -r f; do
  case "$f" in
    *.patch|*.diff|*.orig|*.rej) grep -q -F -f "$W/diffpaths" "$f" 2>/dev/null && echo "strayref $f" ;;
  esac
done < "$W/stray"
grep -v -E '/(node_modules|site-packages|dist-packages|\.cargo|go/pkg/mod|\.npm|\.cache|\.rustup|share/doc|share/man|share/locale|share/zoneinfo|share/X11|share/vim|lib/python[0-9.]+/test|share/perl|share/i18n|linux-headers)/' "$W/stray" | head -300 | sed 's/^/stray /'
sec topdirs
for d in solution solutions tests test oracle reference gold patches logs; do
  [ -e "$R/$d" ] && { echo "== /$d"; ls -la "$R/$d" 2>&1 | head -40; }
done
echo "== /"; ls -la "$RD" 2>&1
sec caches
for d in /root/.cache /root/.npm /root/.cargo/registry /root/go/pkg/mod /go/pkg/mod /root/.m2 /root/.gradle /usr/local/lib/python3*/site-packages /usr/lib/python3*/site-packages /usr/local/lib/node_modules /root/.local; do
  for x in $R$d; do [ -e "$x" ] && echo "cache ${x#$R}"; done
done
sec end
echo "PROBE_DONE"
'''

def _scanner_version() -> str:
    """Fingerprint of everything that shapes the in-container scan: the scripts, needle
    selection and probe assembly, and the needle counts."""
    import inspect

    src = PROBE_SH + ARCH_PY + "".join(inspect.getsource(f) for f in (
        candidate_needles, candidate_identifiers, patch_paths, _drop_substrings, build_probe))
    return (hashlib.sha256(src.encode()).hexdigest()[:12]
            + f"-s{N_SOL_NEEDLES}i{N_IDENT_NEEDLES}t{N_TEST_NEEDLES}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_probe(task: dict) -> str:
    def block(lines: list[str]) -> str:
        bad = [s for s in lines if "__IMGVAL_EOF__" in s or "\n" in s]
        if bad:
            raise ValueError(f"unsafe heredoc line: {bad[0]!r}")
        return "\n".join(lines)

    sol = candidate_needles(task["solution"])
    tst = candidate_needles(task["tests"]) if task.get("tests") else []
    idents = candidate_identifiers(task["solution"])
    paths = patch_paths(task["solution"])
    # the heredoc adds back one newline; strip exactly one (a trailing blank context line counts)
    patch = task["solution"].removesuffix("\n")
    delim = "__IMGVAL_PATCH_" + secrets.token_hex(8) + "__"
    if delim in patch:
        raise ValueError("delimiter collision")
    base = task.get("base") or ""
    if not re.fullmatch(r"[0-9a-f]{0,64}", base):
        raise ValueError(f"bad base {base!r}")
    return (PROBE_SH.replace("__NSOL__", str(N_SOL_NEEDLES)).replace("__NTEST__", str(N_TEST_NEEDLES))
            .replace("__NIDENT__", str(N_IDENT_NEEDLES)).replace("__BASE__", f"'{base}'")
            .replace("__CAP__", str(ARCHIVE_MEMBER_CAP))
            .replace("__CANDS_SOL__", block(sol)).replace("__CANDS_TEST__", block(tst))
            .replace("__CANDS_IDENT__", block(idents)).replace("__PATHS__", block(paths))
            .replace("__SHAS__", block(task.get("shas") or []))
            .replace("__ARCH_PY__", ARCH_PY.strip("\n"))
            .replace("__PATCH_DELIM__", delim).replace("__PATCH__", patch))


SCANNER_VERSION = _scanner_version()


def parse_sections(text: str) -> dict[str, list[str]]:
    """'@@SECTION name' blocks -> {name: [lines]} (a repeated name extends its block)."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("@@SECTION "):
            cur = line[len("@@SECTION "):]
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return out


def _kv(lines: list[str]) -> dict[str, str]:
    d = {}
    for ln in lines:
        k, _, v = ln.partition(" ")
        if k and k not in d:
            d[k] = v
    return d


def _int(s: str | None, default: int = 0) -> int:
    try:
        return int((s or "").split()[0])
    except (ValueError, IndexError):
        return default


def _statuses(lines: list[str]) -> dict[str, int]:
    out = {}
    for ln in lines:
        m = re.match(r"st (.+) (-?\d+)$", ln)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def loc_rule(hits: list[dict], loc_key: str, what: str, reasons: list[str],
             test_hits: list[str], weak: list[dict], needles: dict[str, str]) -> None:
    """Group needle hits by location (file, archive member, object) and classify each:
    - solution leak: 2+ distinct S lines, or an S line with 2+ new identifiers, or 3+ distinct new
      identifiers alone (one generic line plus one stock name, e.g. `json.Unmarshal` with
      `MarshalJSON`, is a review item) (two can be stock names, e.g. `asyncIterator` in @types/node), unless the
      only S/I needles are identifiers next to hidden-test lines (tests call the new API by name,
      so that is a hidden-test leak);
    - hidden-test leak: 2+ distinct T needles, or T with identifiers;
    - otherwise a single needle goes to `weak`, for manual review (one generic line, e.g. an
      import, can match an unrelated package)."""
    by_loc: dict[str, dict[str, set[str]]] = {}
    for h in hits:
        if h["needle"] not in needles:  # e.g. a lockfile line, not counted as evidence
            continue
        c = h["needle"][:1]
        if c in "SIT":
            by_loc.setdefault(h[loc_key], {"S": set(), "I": set(), "T": set()})[c].add(h["needle"])
    sol, tst = {}, {}
    for loc, k in sorted(by_loc.items()):
        # a name inside a matched S or T line at the same location is not separate evidence
        lines = [needles.get(n, "") for n in k["S"] | k["T"]]
        k["I"] = {i for i in k["I"] if not any(needles.get(i, "\0") in ln for ln in lines)}
        si = k["S"] | k["I"]
        if (len(k["S"]) >= 2 or (k["S"] and len(k["I"]) >= 2)
                or (len(k["I"]) >= 3 and not k["T"])):
            sol[loc] = si
        elif len(k["T"]) >= 2 or (k["T"] and k["I"]):
            tst[loc] = k["T"] | k["I"]
        else:
            review = bool(k["S"]) or len(k["I"]) == 2  # incl. one line + one name
            for n in sorted(si | k["T"]):
                weak.append({"what": what, "location": loc, "needle": n, "review": review,
                             "with": sorted(si | k["T"])})
    if sol:
        loc = min(sol)
        reasons.append(f"{len(sol)} {what}(s) hold 2+ distinct solution needles, e.g. {loc} "
                       f"({sorted(sol[loc])})")
    if tst:
        loc = min(tst)
        test_hits.append(f"{len(tst)} {what}(s) hold hidden-test lines, e.g. {loc} "
                         f"({sorted(tst[loc])})")


def _needle_maps(lines: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """needles section -> ({id: text}, {text or escaped variant: id}); S > I > T on a clash."""
    needles: dict[str, str] = {}
    by_text: dict[str, str] = {}
    rank = {"S": 0, "I": 1, "T": 2}
    for ln in lines:
        k, sep, v = ln.partition("\t")
        if not (sep and re.fullmatch(r"[STI]\d+", k)):
            continue
        needles.setdefault(k, v)
        if v not in by_text or rank[k[0]] < rank[by_text[v][0]]:
            by_text[v] = k
    return needles, by_text


def _scan_objects_block(lines: list[str], tag: str, errors: list[str],
                        gaps: list[dict]) -> list[dict]:
    """Object-scan lines -> hits [{id, type, needle}] (each object's content, and archives inside
    blobs, checked needle by needle). A failed step, or an object count or stream length that
    differs from `--batch-check`, is an ERROR."""
    sts = _statuses(lines)
    kv = _kv(lines)
    for k in ("batch_check", "batch", "gitscan"):
        if sts.get(k, -1) != 0:
            errors.append(f"{tag}: {k} status {sts.get(k)}")
    stats = {}
    for ln in lines:
        parts = ln.split("\t")
        if parts[0] == "ga" and len(parts) == 4 and parts[1] == "n":
            stats[parts[2]] = _int(parts[3])
    if stats.get("objects", -1) != _int(kv.get("n_objects"), -2):
        errors.append(f"{tag}: scanned {stats.get('objects')} objects of {kv.get('n_objects')}")
    if stats.get("bytes", -1) != _int(kv.get("expected_bytes"), -2):
        errors.append(f"{tag}: object stream {stats.get('bytes')} bytes, expected "
                      f"{kv.get('expected_bytes')}")
    if stats.get("errors"):
        errors.append(f"{tag}: {stats['errors']} object stream error(s)")
    gappaths = {}
    for ln in lines:
        if ln.startswith("gappath "):
            oid, _, path = ln[len("gappath "):].partition(" ")
            gappaths[oid] = path
    hits, seen = [], set()
    for ln in lines:
        parts = ln.split("\t")
        if parts[0] != "ga":
            continue
        if len(parts) == 5 and parts[1] == "objhit":
            key = (parts[2], parts[4])
            if key not in seen:
                seen.add(key)
                hits.append({"id": parts[2], "type": parts[3], "needle": parts[4]})
        elif len(parts) == 4 and parts[1] == "archhit":
            oid = parts[2][len("git:"):].split("!")[0]
            if (oid, parts[3]) not in seen:
                seen.add((oid, parts[3]))
                hits.append({"id": oid, "type": "blob (archive)", "needle": parts[3]})
        elif len(parts) == 4 and parts[1] == "gap":
            oid = parts[2][len("git:"):].split("!")[0]
            path = gappaths.get(oid, "")
            gaps.append({"path": f"/{path}" if path else parts[2], "object": parts[2],
                         "why": parts[3]})
    return hits


def assess_probe(text: str, task: dict, rc: int | None = None) -> dict:
    """Probe output -> {verdict, reasons, errors, notes, weak_hits, ...}; rules in the module
    docstring. ERROR when the probe didn't finish, a step failed or is missing, a tool is missing,
    there's no task repo, fewer than 3 solution needles, or an archive gap outside toolchain
    fixtures; a LEAK found stands regardless."""
    S = parse_sections(text)
    reasons: list[str] = []
    errors: list[str] = []
    notes: list[str] = []
    test_hits: list[str] = []
    weak: list[dict] = []
    if "PROBE_DONE" not in "\n".join(S.get("end", [])):
        errors.append(f"probe did not finish (rc={rc})")
    if rc not in (0, None):
        errors.append(f"probe exit status {rc}")
    tools = _kv(S.get("tools", []))
    for t in ("git", "grep", "find", "awk", "sort", "python3"):
        if tools.get(t, "MISSING") == "MISSING":
            errors.append(f"tool missing: {t}")
    for sec in ("tools", "gitdirs", "main", "needles", "apply", "fs", "fshits", "archives",
                "stray", "end"):
        if sec not in S:
            errors.append(f"section missing: {sec}")
    gsts = _statuses(S.get("gitdirs", []))
    if gsts.get("find_gitdirs", -1) != 0:
        errors.append(f"git dir discovery had {gsts.get('find_gitdirs')} find errors")
    mkv = _kv(S.get("main", []))
    main = mkv.get("main", "").strip()
    if not main:
        errors.append("no task git repo found")
    base = task.get("base")
    if base and main and mkv.get("head", "").strip() != base:
        # a squashed image has a new HEAD id; the forward patch check below then has to confirm
        # the base state
        notes.append(f"task repo HEAD {mkv.get('head', '').strip()[:12]} is not the task base "
                     f"{base[:12]}")
    nd = S.get("needles", [])
    needles, _ = _needle_maps(nd)
    lock = lockfile_texts(task.get("solution", ""))
    lock_ids = {k for k, v in needles.items() if k[0] in "SI" and v in lock}
    needles_all = needles  # recorded in full; layers and the red-team check use them all
    if lock_ids:
        notes.append(f"needles from lockfile hunks not counted as evidence: {sorted(lock_ids)}")
        needles = {k: v for k, v in needles.items() if k not in lock_ids}
    nsts = _statuses(nd)
    for k in ("head_valid", "git_grep_head"):
        if main and nsts.get(k, -1) != 0:
            errors.append(f"needle selection: {k} status {nsts.get(k)}")
    if "git_grep_pick" in nsts:
        errors.append(f"needle selection: git grep failed ({nsts['git_grep_pick']})")
    nkv = _kv(nd)
    n_sol = sum(1 for k in needles if k.startswith("S"))
    n_si = n_sol + sum(1 for k in needles if k.startswith("I"))
    if n_si == 0:
        errors.append("no distinctive solution lines or names")
    elif n_si < MIN_SOL_NEEDLES:  # e.g. a pure code move: added lines equal removed ones
        weak.append({"what": "low detection power", "location": f"{n_sol} lines, "
                     f"{n_si - n_sol} names", "needle": "LOWNEEDLES", "review": True})
    cs, csh = _int(nkv.get("cands_sol")), _int(nkv.get("cands_sol_in_head"))
    if cs and csh * 2 > cs:
        reasons.append(f"solution already in HEAD tree: {csh}/{cs} candidate lines found there")

    # patch direction per file
    akv = _kv(S.get("apply", []))
    applied_files = []
    n_paths = len(patch_paths(task.get("solution", "")))
    n_file_lines = sum(1 for ln in S.get("apply", []) if ln.startswith("file "))
    if main and task.get("solution", "").strip() and (
            "fwd_all" not in akv or "rev_all" not in akv or n_file_lines != n_paths):
        errors.append(f"patch check incomplete ({n_file_lines}/{n_paths} files)")
    for ln in S.get("apply", []):
        m = re.match(r"file (\d+) (\d+) (.+)$", ln)
        if m and m.group(1) != "0" and m.group(2) == "0":
            applied_files.append(m.group(3))
    if main and task.get("solution", "").strip():
        if akv.get("rev_all", "").strip() == "0" and akv.get("fwd_all", "").strip() != "0":
            reasons.append("solution patch is already applied in the task worktree (reverse "
                           "applies, forward does not)")
        elif applied_files:
            reasons.append(f"solution already applied to {len(applied_files)} file(s), e.g. "
                           f"{applied_files[0]}")
        if akv.get("fwd_all", "").strip() != "0" and not applied_files:
            # neither the base state nor the solution state is confirmed: manual review
            errors.append(f"solution patch applies in neither direction (fwd "
                          f"{akv.get('fwd_all', '').strip()}, rev {akv.get('rev_all', '').strip()})"
                          "; base state unverified")

    gitdirs = [ln[4:] for ln in S.get("gitdirs", []) if ln.startswith("dir ")]
    orphans = [ln[7:] for ln in S.get("gitdirs", []) if ln.startswith("orphan ")]
    obj_gaps: list[dict] = []
    if main and main not in gitdirs:
        errors.append(f"task repo {main} missing from the git inventory")
    git = {}
    for G in gitdirs:
        if f"git {G}" not in S or f"fsck {G}" not in S or f"offhead {G}" not in S:
            errors.append(f"{G}: git sections missing")
            continue
        hd = _kv(S.get(f"git {G}", []))
        refs = [ln for ln in S.get(f"refs {G}", []) if ln.strip() and not ln.startswith("st ")]
        offl, fsl, tl = S.get(f"offhead {G}", []), S.get(f"fsck {G}", []), S.get(f"touch {G}", [])
        gst = {**_statuses(S.get(f"refs {G}", [])), **_statuses(offl), **_statuses(fsl),
               **_statuses(tl)}
        required = ["for_each_ref", "rev_list_offhead", "fsck"]
        if _int(_kv(tl).get("xcommits")) and patch_paths(task.get("solution", "")):
            required.append("diff_tree")
        for k in required:
            if k not in gst:
                errors.append(f"{G}: {k} status missing")
        for k, v in gst.items():
            if v != 0:
                errors.append(f"{G}: {k} status {v}")
        offhead, fsck = _kv(offl), _kv(fsl)
        touch = [ln for ln in tl if ln.startswith("touch ")]
        commits = [ln[len("commit "):] for ln in tl if ln.startswith("commit ")]
        shas = [ln.split() for ln in S.get(f"shas {G}", []) if ln.strip()]
        objhits: list[dict] = []
        if needles:
            if f"objects {G}" not in S:
                errors.append(f"{G}: object scan missing")
            else:
                objhits = _scan_objects_block(S[f"objects {G}"], G, errors, obj_gaps)
        objpaths = {}
        for ln in S.get(f"objpaths {G}", []):
            oid, _, p = ln.partition(" ")
            objpaths[oid] = p
        in_head = {x.strip() for x in S.get(f"objhead {G}", []) if x.strip()}
        for h in objhits:
            h["path"] = objpaths.get(h["id"])
            h["reachable_from_head"] = h["id"] in in_head
        reflogs = [ln for ln in S.get(f"reflog {G}", []) if ln.startswith("== ")]
        special = [ln[3:] for ln in S.get(f"special {G}", []) if ln.startswith("== ")]
        status = [ln for ln in S.get(f"status {G}", []) if ln.strip()]
        rec = {
            "head": hd.get("head"), "is_main": hd.get("is_main") == "1",
            "n_commits_all": _int(hd.get("n_commits_all")),
            "n_commits_all_reflog": _int(hd.get("n_commits_all_reflog")),
            "shallow": _int(hd.get("shallow")), "n_refs": len(refs), "refs": refs[:50],
            "reflogs": reflogs[:50], "special": special,
            "stash": [ln for ln in S.get(f"stash {G}", []) if ln.strip()],
            "remotes": [ln for ln in S.get(f"remotes {G}", []) if ln.strip()],
            "offhead_commits": _int(offhead.get("count")),
            "unreachable": {t: _int(fsck.get(f"unreachable_{t}")) for t in
                            ("commit", "tree", "blob", "tag")},
            "lost_found": _int(fsck.get("lost_found")),
            "submodule_prefix": _kv(tl).get("submodule_prefix"),
            "commits_touching_solution_paths": len({t.split()[1] for t in touch}),
            "touch_examples": touch[:10], "touch_commits": commits[:10],
            "solution_shas": {s[0]: s[1] for s in shas if len(s) == 2},
            "n_object_hits": len(objhits), "object_hits": objhits[:100],
            "status": status[:30], "n_status": len(status),
            "alternates": [ln for ln in S.get(f"git {G}", []) if ln.startswith("alternates ")],
        }
        git[G] = rec
        for s, typ in rec["solution_shas"].items():
            if typ != "absent":
                reasons.append(f"{G}: solution commit {s} present ({typ})")
        if rec["commits_touching_solution_paths"]:
            reasons.append(f"{G}: {rec['commits_touching_solution_paths']} commit(s) outside HEAD "
                           f"(refs/reflog/unreachable) touch solution files, e.g. "
                           f"{(commits or touch)[0]}")
        # in the task repo, a solution line counts alone in an object HEAD can't reach; in the
        # repo's own past (reachable from HEAD) it goes through the location rule like elsewhere
        sol_obj = [h for h in objhits if h["needle"].startswith("S")
                   and h["needle"] in needles and not h["reachable_from_head"]]
        if sol_obj and rec["is_main"]:
            ids = sorted({h["id"] for h in sol_obj})
            reasons.append(f"{G}: solution lines in {len(ids)} git object(s), e.g. {ids[0]} "
                           f"({sol_obj[0]['type']}, {sol_obj[0]['path'] or 'no path'}, "
                           f"needles {sorted({h['needle'] for h in sol_obj})})")
        loc_rule(objhits, "id", f"{G}: git object", reasons, test_hits, weak, needles)
        if rec["offhead_commits"] or rec["unreachable"]["commit"]:
            notes.append(f"{G}: {rec['offhead_commits']} commit(s) outside HEAD, "
                         f"{rec['unreachable']['commit']} unreachable commit(s)")
        if rec["n_status"] and rec["is_main"]:
            notes.append(f"{G}: worktree differs from HEAD in {rec['n_status']} path(s)")
        if rec["alternates"]:
            notes.append(f"{G}: alternates {rec['alternates']}")
    for O in orphans:
        notes.append(f"object store outside a git dir: {O}")
        if not needles:
            continue
        if f"orphanobjects {O}" not in S:
            errors.append(f"orphan object store {O}: scan missing")
            continue
        oh = _scan_objects_block(S[f"orphanobjects {O}"], O, errors, obj_gaps)
        loc_rule(oh, "id", f"{O}: object", reasons, test_hits, weak, needles)

    fkv = _kv(S.get("fs", []))
    if _statuses(S.get("fs", [])).get("find_files", -1) != 0 or _int(fkv.get("find_errors")):
        errors.append(f"filesystem walk failed ({fkv.get('find_errors')} find errors)")
    fs_hits = []
    fsl = S.get("fshits", [])
    if needles and _statuses(fsl).get("fs_scan", -1) != 0:
        errors.append(f"filesystem scan status {_statuses(fsl).get('fs_scan')}")
    for ln in fsl:
        parts = ln.split("\t", 3)
        if parts[0] == "fshit" and len(parts) == 4:
            fs_hits.append({"needle": parts[1], "count": _int(parts[2]), "path": parts[3]})
        elif parts[0] == "readerr":
            errors.append(f"filesystem re-check could not read {parts[1:]}")
    loc_rule(fs_hits, "path", "file", reasons, test_hits, weak, needles)

    al = S.get("archives", [])
    if needles and _statuses(al).get("arch_py", -1) != 0:
        errors.append(f"archive scan status {_statuses(al).get('arch_py')}")
    arch_hits, gaps, arch_n = [], [], {}
    for ln in al:
        parts = ln.split("\t")
        if parts[0] == "archhit" and len(parts) == 3:
            arch_hits.append({"path": parts[1], "needle": parts[2]})
        elif parts[0] == "gap" and len(parts) == 3:
            gaps.append({"path": parts[1], "why": parts[2]})
        elif parts[0] == "readerr" and len(parts) == 3:
            gaps.append({"path": parts[1], "why": "read error " + parts[2]})
        elif parts[0] == "n" and len(parts) == 3:
            arch_n[parts[1]] = _int(parts[2])
    loc_rule(arch_hits, "path", "archive", reasons, test_hits, weak, needles)
    gaps += obj_gaps
    # A file on disk that can't be decoded is a coverage gap (ERROR) unless it is a toolchain
    # fixture. A nested member that fails to decode inside an archive that was decoded had its
    # raw bytes searched already (e.g. corrupt-archive test fixtures inside dependency packages),
    # so it is a note.
    # a gap is exempt only inside toolchain or dependency test fixtures (whole path, members
    # included); anything else, nested or not, is unresolved coverage
    nested = [g for g in gaps if "!" in g["path"] and BENIGN_GAP.search(g["path"])]
    hard_gaps = [g for g in gaps if not BENIGN_GAP.search(g["path"])]
    for g in hard_gaps:  # unresolved coverage: each needs a recorded review
        weak.append({"what": "undecoded archive", "location": g.get("object") or g["path"],
                     "needle": "GAP", "review": True, "why": g["why"], "path": g["path"]})
    if hard_gaps:
        notes.append(f"{len(hard_gaps)} archive(s) not decoded and awaiting review, e.g. "
                     f"{hard_gaps[0]['path']} ({hard_gaps[0]['why']})")
    if len(gaps) - len(hard_gaps) - len(nested):
        notes.append(f"{len(gaps) - len(hard_gaps) - len(nested)} undecodable archive(s) in "
                     "toolchain or dependency test fixtures")
    if nested:
        notes.append(f"{len(nested)} nested fixture member(s) not decoded (raw bytes searched), "
                     f"e.g. {nested[0]['path']} ({nested[0]['why']})")

    stray = S.get("stray", [])
    strayref = [ln[len("strayref "):] for ln in stray if ln.startswith("strayref ")]
    for f in strayref:  # a patch naming a solution path; its content had no solution line
        weak.append({"what": "patch file naming a solution path", "location": f,
                     "needle": "STRAYREF", "review": True})
    topdirs = [ln[3:] for ln in S.get("topdirs", []) if ln.startswith("== /") and ln != "== /"]
    if topdirs:
        notes.append(f"top-level dirs present: {topdirs}")
    verdict = "LEAK" if reasons else ("ERROR" if errors else "CLEAN")
    return {
        "verdict": verdict, "reasons": reasons, "errors": errors, "notes": notes,
        "hidden_tests": "LEAK" if test_hits else ("ERROR" if errors else "CLEAN"),
        "hidden_test_reasons": test_hits, "weak_hits": weak[:300], "n_weak_hits": len(weak),
        "review_hits": [w for w in weak if w["review"]],
        "needles": needles_all, "lockfile_needles": sorted(lock_ids),
        "candidates": {"sol": cs, "sol_in_head": csh,
                                           "test": _int(nkv.get("cands_test")),
                                           "test_in_head": _int(nkv.get("cands_test_in_head"))},
        "main_repo": main, "head": mkv.get("head"), "base": base,
        "apply": {"fwd_all": akv.get("fwd_all"), "rev_all": akv.get("rev_all"),
                  "applied_files": applied_files[:20]},
        "git": git, "orphan_object_stores": orphans,
        "n_fs_hits": len(fs_hits), "fs_hits": fs_hits[:200],
        "n_archive_hits": len(arch_hits), "archive_hits": arch_hits[:100],
        "n_files": _int(fkv.get("n_files")),
        "archives": {**arch_n, "gaps": (hard_gaps + gaps)[:100], "n_gaps": len(gaps),
                     "n_hard_gaps": len(hard_gaps), "n_nested_gaps": len(nested)},
        "stray": {"n": _int(_kv(stray).get("n_stray")), "refs_solution": strayref[:20],
                  "listed": [ln[len("stray "):] for ln in stray if ln.startswith("stray ")][:100]},
        "topdirs": topdirs,
        "mounts": [ln for ln in S.get("mounts", []) if ln.strip()][:100],
        "caches": [ln[len("cache "):] for ln in S.get("caches", []) if ln.startswith("cache ")],
        "tools": tools,
    }


# ------------------------------------------------------------------------------------------
# Rig helpers and container hygiene
# ------------------------------------------------------------------------------------------

_SLOT = threading.Semaphore(MAX_OWN_CONTAINERS)
_LIVE: set[str] = set()  # containers this process named and hasn't confirmed removed
_LIVE_LOCK = threading.Lock()
_STOP = threading.Event()


def _track(name: str, live: bool) -> None:
    with _LIVE_LOCK:
        (_LIVE.add if live else _LIVE.discard)(name)


def rm_container(name: str) -> str | None:
    """`docker rm -f`, then confirm it's gone; returns the name if it may still be there."""
    try:
        docker_q("rm", "-f", name)
        r = docker_q("ps", "-a", "-q", "--filter", f"name=^{name}$")
    except RuntimeError:
        return name
    if r.returncode == 0 and not r.stdout.strip():
        _track(name, False)
        return None
    return name


def cleanup_live(*_args) -> list[str]:
    """Remove every container this process still owns; returns the ones that remain."""
    with _LIVE_LOCK:
        names = sorted(_LIVE)
    left = []
    for n in names:
        if rm_container(n):
            left.append(n)
            print(f"[{now()}] cleanup {n}: STILL PRESENT", flush=True)
    return left


def _on_signal(signum, _frame) -> None:
    _STOP.set()
    cleanup_live()
    raise SystemExit(128 + signum)


def label_inventory() -> list[str]:
    """Names of every imgval-labelled container on the rig; raises if docker can't answer (an
    empty list must mean none, never a failed query)."""
    r = docker_q("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}")
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed: {r.stderr.strip()[-200:]}")
    return [x for x in r.stdout.split() if x]


def rig_ok_to_start() -> tuple[bool, str]:
    try:
        f = subprocess.run(["ssh", SSH_HOST, "free -g"], capture_output=True, text=True,
                           timeout=60, check=False)
        avail = int(next(ln.split()[6] for ln in f.stdout.splitlines() if ln.startswith("Mem:")))
    except Exception as e:  # noqa: BLE001
        return False, f"rig check failed: {e}"
    if avail < MIN_RIG_AVAIL_GB:
        return False, f"rig available memory {avail} GB"
    return True, f"{avail} GB available"


def wait_for_rig(log=print) -> None:
    while not _STOP.is_set():
        ok, why = rig_ok_to_start()
        if ok:
            return
        log(f"[{now()}] waiting: {why}")
        time.sleep(60)
    raise SystemExit("stopping")


def container_name(prefix: str, task: dict) -> str:
    h = hashlib.sha1(f"{task['family']}:{task['task']}".encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", task["task"])[:30].strip("-")
    name = f"imgval-{prefix}-{slug}-{h}-{os.getpid()}-{secrets.token_hex(2)}"
    _track(name, True)  # registered before `docker run`, so an interrupt still removes it
    return name


def run_args(name: str, image: str, cmd: list[str], *, interactive: bool = False,
             detach: bool = False) -> list[str]:
    a = ["run", "--rm", "--name", name, "--label", LABEL, "--network", "none",
         "--memory", MEM_LIMIT, "--cpus", CPU_LIMIT, "--entrypoint", "", "--user", "0",
         "--platform", "linux/amd64"]
    if interactive:
        a.append("-i")
    if detach:
        a.append("-d")
    return docker(*a, image, *cmd)


def image_meta(image: str) -> dict:
    r = docker_q("image", "inspect", image)
    if r.returncode != 0:
        return {"error": r.stderr.strip()[-300:]}
    j = json.loads(r.stdout)[0]
    h = subprocess.run(docker("history", "--no-trunc", "--format", "{{json .}}", j["Id"]),
                       capture_output=True, text=True, timeout=120, check=False)
    hist = [json.loads(x) for x in h.stdout.splitlines() if x.strip()]
    cfg = j.get("Config") or {}
    return {"id": j.get("Id"), "repo_digests": j.get("RepoDigests"),
            "arch": j.get("Architecture"), "size": j.get("Size"),
            "env": cfg.get("Env"), "entrypoint": cfg.get("Entrypoint"), "cmd": cfg.get("Cmd"),
            "workdir": cfg.get("WorkingDir"), "labels": cfg.get("Labels"),
            "volumes": cfg.get("Volumes"),
            "history": [{"created_by": x.get("CreatedBy"), "size": x.get("Size")} for x in hist]}


_ID_CACHE: dict[str, str] | None = None  # set only for the duration of a summary


def current_image_id(image: str) -> str | None:
    if _ID_CACHE is not None and image in _ID_CACHE:
        return _ID_CACHE[image]
    r = docker_q("image", "inspect", "--format", "{{.Id}}", image)
    return r.stdout.strip() if r.returncode == 0 else None


def image_id_table() -> dict[str, str]:
    """Every local tag and repo@digest on the rig -> image id, in one docker call."""
    r = docker_q("images", "--digests", "--no-trunc", "--format",
                 "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}")
    out: dict[str, str] = {}
    for ln in r.stdout.splitlines():
        repo, tag, digest, iid = (ln.split("\t") + ["", "", "", ""])[:4]
        if tag and tag != "<none>":
            out[f"{repo}:{tag}"] = iid
        if digest and digest != "<none>":
            out[f"{repo}@{digest}"] = iid
            if tag and tag != "<none>":
                out[f"{repo}:{tag}@{digest}"] = iid
    return out


def meta_findings(meta: dict, needles: dict[str, str]) -> list[str]:
    blob = json.dumps({k: meta.get(k) for k in ("env", "labels", "history", "cmd", "entrypoint")})
    return [f"needle {k} in image config/history" for k, v in needles.items()
            if k[0] == "S" and json_escape(v) in blob]


def out_paths(task: dict) -> tuple[Path, Path]:
    d = RAW_OUT / task["family"] / re.sub(r"[^A-Za-z0-9._-]", "_", task["task"])
    return d, d / "probe.json"


def write_rec(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=1))
    tmp.replace(path)


def load_rec(task: dict) -> dict | None:
    p = out_paths(task)[1]
    return json.loads(p.read_text()) if p.exists() else None


def sub_path(task: dict, key: str) -> Path:
    """layers.json / redteam.json: kept apart from probe.json so stages never overwrite each
    other's records."""
    return out_paths(task)[0] / f"{key}.json"


def load_sub(task: dict, key: str) -> dict | None:
    p = sub_path(task, key)
    if p.exists():
        return json.loads(p.read_text())
    rec = load_rec(task)  # records written before the split kept it inside probe.json
    return (rec or {}).get(key)


def needles_sha(needles: dict) -> str:
    return sha(json.dumps(needles, sort_keys=True))


# ------------------------------------------------------------------------------------------
# Layer 1 driver
# ------------------------------------------------------------------------------------------


def probe_one(task: dict, log=print) -> dict:
    d, outp = out_paths(task)
    d.mkdir(parents=True, exist_ok=True)
    script = build_probe(task)
    (d / "probe.sh").write_text(script)
    rec = {"family": task["family"], "task": task["task"], "image": task["image"],
           "sets": task["sets"], "solution_src": task["solution_src"], "started": now(),
           "scanner": SCANNER_VERSION, "solution_sha256": sha(task["solution"]),
           "tests_sha256": sha(task.get("tests") or "")}
    meta = image_meta(task["image"])
    rec["image_meta"] = meta
    rec["image_id"] = meta.get("id")
    exp = task.get("expected_image_id")
    if exp and meta.get("id") and meta["id"] != exp:
        meta["error"] = f"tag resolves to {meta['id'][:19]}, the family worker recorded {exp[:19]}"
    if "error" in meta:
        rec.update(verdict="ERROR", errors=[f"image inspect: {meta['error']}"], finished=now())
        write_rec(outp, rec)
        return rec
    name = container_name("p", task)
    out, err, rc, left = "", "", None, None
    with _SLOT:
        wait_for_rig(log)
        t0 = time.monotonic()
        try:
            r = subprocess.run(run_args(name, meta["id"], ["timeout", "-k", "10",
                                                           str(PROBE_TIMEOUT_S), "bash", "-s"],
                                        interactive=True),
                               input=script, capture_output=True, text=True, errors="replace",
                               timeout=PROBE_TIMEOUT_S + 120, check=False)
            out, err, rc = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err, rc = "client timeout", None
        finally:
            left = rm_container(name)
        rec["duration_s"] = round(time.monotonic() - t0, 1)
    (d / "probe.out").write_text(out)
    (d / "probe.err").write_text(err[-20000:])
    rec.update(rc=rc, finished=now(), container=name, container_left=left)
    rec.update(finalize_probe(out, task, rec))
    write_rec(outp, rec)
    return rec


def finalize_probe(out: str, task: dict, rec: dict) -> dict:
    """assess_probe plus the image-config check and container hygiene."""
    a = assess_probe(out, task, rec.get("rc"))
    if rec.get("rc") == OOM_RC:  # killed (OOM or timeout -k): rerun, never a verdict
        a["errors"].append(f"probe killed (exit {OOM_RC}, likely OOM); rerun")
        a["verdict"] = "ERROR"
    meta = rec.get("image_meta") or {}
    mf = meta_findings(meta, a["needles"])
    if mf:
        a["reasons"] += mf
        a["verdict"] = "LEAK"
    if meta.get("volumes"):
        a["notes"].append(f"image declares volumes {sorted(meta['volumes'])} (all mounts scanned)")
    if rec.get("container_left"):
        a["errors"].append(f"container {rec['container_left']} not removed")
        if a["verdict"] == "CLEAN":
            a["verdict"] = "ERROR"
    a["assessed"] = now()
    return a


def reassess_one(task: dict, log=print) -> dict:
    """Recompute the probe verdict from the saved probe.out (assessment rules changed; the
    in-container scanner did not). Layer and red-team records are kept."""
    d, outp = out_paths(task)
    rec = load_rec(task)
    if rec is None or not (d / "probe.out").exists() or rec.get("scanner") != SCANNER_VERSION:
        return {"verdict": "SKIP", "reasons": ["no current probe output"]}
    rec.update(finalize_probe((d / "probe.out").read_text(), task, rec))
    write_rec(outp, rec)
    ly = load_sub(task, "layers")
    if ly and (d / "layers.out").exists() and rec.get("needles"):
        a = assess_layers((d / "layers.out").read_text(), rec["needles"], task["solution"])
        keep = [e for e in ly.get("errors", []) if "exit status" in e]
        a["errors"] += keep
        if keep and a["verdict"] == "CLEAN":
            a["verdict"] = "ERROR"
        write_rec(sub_path(task, "layers"), {**ly, **a, "assessed": now()})
    rt = load_sub(task, "redteam")
    tr, cl = d / "redteam_transcript.jsonl", d / "redteam_calls.jsonl"
    if rt and tr.exists() and cl.exists() and rt.get("verdict") in ("CLEAN", "ERROR"):
        # re-judge the saved session (no new agent run); a confirmed find is never re-judged
        events = [json.loads(ln) for ln in tr.read_text().splitlines() if ln.strip()]
        calls = [json.loads(ln) for ln in cl.read_text().splitlines() if ln.strip()]
        errors = [e for e in rt.get("errors", []) if e.startswith(("session timeout",
                                                                   "container", "RuntimeError"))]
        res = judge_redteam(events, calls, {}, errors)
        killed = [e for e in rt.get("errors", []) if e.startswith("session killed")]
        errors += killed
        found = (res.get("answer") or {}).get("verdict") == "found"
        if not found:
            write_rec(sub_path(task, "redteam"), {**rt, **res, "errors": errors,
                                                  "verdict": "ERROR" if errors else "CLEAN",
                                                  "rejudged": now()})
    return rec


def entryprobe_one(task: dict, log=print) -> dict:
    """For an image whose ENTRYPOINT runs setup: start the container the way an agent gets it
    (the image's own entrypoint, command `sleep`, no network), wait for setup to finish (the
    wrapper's /tmp/emx_entrypoint_rc marker, or 20 min), then run the same probe inside it, so
    files the setup writes are scanned too. Recorded as entryprobe.json; must be CLEAN."""
    d, _ = out_paths(task)
    rec = load_rec(task) or {}
    iid = rec.get("image_id")
    ep = (rec.get("image_meta") or {}).get("entrypoint")
    res: dict = {"image_id": iid, "entrypoint": ep, "started": now()}
    if not ep:
        res.update(verdict="SKIP", finished=now())
        write_rec(sub_path(task, "entryprobe"), res)
        return res
    name = container_name("ep", task)
    out, rc, left = "", None, None
    with _SLOT:
        wait_for_rig(log)
        try:
            a = ["run", "--rm", "-d", "--name", name, "--label", LABEL, "--network", "none",
                 "--memory", MEM_LIMIT, "--cpus", CPU_LIMIT, "--user", "0",
                 "--platform", "linux/amd64", iid, "sleep", str(PROBE_TIMEOUT_S + 1800)]
            r = subprocess.run(docker(*a), capture_output=True, text=True, timeout=300,
                               check=False)
            if r.returncode != 0:
                raise RuntimeError(f"start: {r.stderr[-300:]}")
            t0 = time.monotonic()
            marker = ""
            while time.monotonic() - t0 < 1260:
                q = docker_q("exec", name, "cat", "/tmp/emx_entrypoint_rc")
                if q.returncode == 0 and q.stdout.strip():
                    marker = q.stdout.strip()
                    break
                time.sleep(15)
            res["setup_marker"] = marker or None
            res["setup_s"] = round(time.monotonic() - t0, 1)
            r = subprocess.run(docker("exec", "-i", name, "timeout", "-k", "10",
                                      str(PROBE_TIMEOUT_S), "bash", "-s"),
                               input=build_probe(task), capture_output=True, text=True,
                               errors="replace", timeout=PROBE_TIMEOUT_S + 120, check=False)
            out, rc = r.stdout, r.returncode
        except Exception as e:  # noqa: BLE001
            res.setdefault("errors", []).append(f"{type(e).__name__}: {e}")
        finally:
            left = rm_container(name)
    (d / "entryprobe.out").write_text(out)
    a = finalize_probe(out, task, {**rec, "rc": rc, "container_left": left})
    if not res.get("setup_marker"):
        a["errors"].append("setup completion marker not seen")
        if a["verdict"] == "CLEAN":
            a["verdict"] = "ERROR"
    a["errors"] = res.pop("errors", []) + a["errors"]
    if a["errors"] and a["verdict"] == "CLEAN":
        a["verdict"] = "ERROR"
    res.update({k: a[k] for k in ("verdict", "reasons", "errors", "notes", "needles",
                                  "review_hits", "n_weak_hits")}, rc=rc, finished=now())
    write_rec(sub_path(task, "entryprobe"), res)
    return res


# ------------------------------------------------------------------------------------------
# Layer 2: image layer tarballs (run on the rig)
# ------------------------------------------------------------------------------------------

# Per-member needle search over one layer tarball (streamed; members over the cap are read in
# overlapping chunks): prints `lhit\t<id>\t<member>` and `n\tmembers\t<N>`.
LAYER_PY = r'''
import sys, tarfile
path, nfile = sys.argv[1], sys.argv[2]
needles = []
for ln in open(nfile, "rb").read().split(b"\n"):
    if b"\t" in ln:
        k, v = ln.split(b"\t", 1)
        if v:
            needles.append((k.decode(), v))
MAXN = max([len(v) for _, v in needles] or [1])
n = 0
with tarfile.open(path, "r|*") as t:
    for m in t:
        if not m.isfile():
            continue
        n += 1
        f = t.extractfile(m)
        found, tail = set(), b""
        while True:
            chunk = f.read(64 << 20)
            if not chunk:
                break
            buf = tail + chunk
            for k, v in needles:
                if k not in found and v in buf:
                    found.add(k)
            tail = buf[-(MAXN - 1):] if MAXN > 1 else b""
        for k in sorted(found):
            sys.stdout.write("lhit\t%s\t%s\n" % (k, m.name))
sys.stdout.write("n\tmembers\t%d\n" % n)
'''

LAYER_SH = r'''
set -u
export LC_ALL=C
IMG="$1"; mkdir -p "$HOME/imgval-tmp"; W=$(mktemp -d "$HOME/imgval-tmp/layers.XXXXXX")  # rig /tmp is tmpfs
cleanup() { chmod -R u+w "$W" 2>/dev/null; rm -rf "$W"; }  # module caches are read-only
trap cleanup EXIT
cat > "$W/needles"
sed -i.bak '/^$/d' "$W/needles"
cat > "$W/layer.py" <<'__IMGVAL_EOF__'
__LAYER_PY__
__IMGVAL_EOF__
cat > "$W/arch.py" <<'__IMGVAL_EOF__'
__ARCH_PY__
__IMGVAL_EOF__
echo "@@SECTION save"
echo "n_needles $(wc -l < "$W/needles")"
timeout -k 10 __T__ docker save "$IMG" -o "$W/img.tar"; echo "st save $?"
mkdir -p "$W/x"; tar -xf "$W/img.tar" -C "$W/x"; echo "st untar $?"
rm -f "$W/img.tar"
echo "@@SECTION manifest"
python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));print("\n".join(m[0]["Layers"]))' "$W/x/manifest.json" > "$W/layers"; echo "st manifest $?"
echo "n_layers $(wc -l < "$W/layers")"
git init -q --bare "$W/empty.git"
while IFS= read -r L; do
  f="$W/x/$L"
  echo "@@SECTION layer $L"
  echo "bytes $(wc -c < "$f" | tr -d ' ')"
  tar -tf "$f" > "$W/list" 2>"$W/err"; echo "st list $?"
  echo "n_entries $(wc -l < "$W/list")"
  echo "n_git_entries $(grep -c -E '(^|/)\.git(/|$)' "$W/list")"
  grep -E '(^|/)\.git/(HEAD|packed-refs|objects/pack/[^/]+\.pack|ORIG_HEAD|FETCH_HEAD)$' "$W/list" | head -20 | sed 's/^/git /'
  grep -E '(^|/)\.wh\.' "$W/list" | head -50 | sed 's/^/whiteout /'
  grep -E '\.(patch|diff|orig|rej)$|(^|/)(solution|gold|solve\.sh)' "$W/list" | grep -v -E 'node_modules|site-packages|dist-packages' | head -50 | sed 's/^/stray /'
  python3 "$W/layer.py" "$f" "$W/needles"; echo "st content_py $?"
  # git object stores inside this layer: extract only object dirs, read each object
  if grep -q -E '(^|/)objects/(pack/[^/]+\.pack|[0-9a-f]{2}/[0-9a-f]{38})$' "$W/list"; then
    WC=""; tar --version 2>/dev/null | grep -q GNU && WC="--wildcards"  # bsdtar globs by default
    mkdir -p "$W/l"; tar -xf "$f" -C "$W/l" $WC '*objects/*' 2>/dev/null; echo "st extract $?"
    k=0
    find "$W/l" -type d -name objects > "$W/ostores"
    while IFS= read -r O; do
      [ -d "$O/pack" ] || ls "$O" | grep -q -E '^[0-9a-f]{2}$' || continue
      k=$((k+1))
      GIT_OBJECT_DIRECTORY="$O" git --git-dir="$W/empty.git" cat-file --batch-all-objects --batch 2>/dev/null \
        | python3 "$W/arch.py" git "$W/needles" __CAP__ | sed "s|^|gobj\t${O#$W/l/}\t|"
      gs=("${PIPESTATUS[@]}"); echo "st gitobj_${k}_git ${gs[0]}"; echo "st gitobj_${k}_py ${gs[1]}"
    done < "$W/ostores"
    chmod -R u+w "$W/l" 2>/dev/null; rm -rf "$W/l"
  fi
done < "$W/layers"
echo "@@SECTION end"
echo LAYERS_DONE
'''


def assess_layers(text: str, needles: dict[str, str], solution: str = "") -> dict:
    """Layer scan output -> verdict, with the container's location rule applied per file inside
    each layer and per object in each layer git store (lockfile lines not counted)."""
    S = parse_sections(text)
    lock = lockfile_texts(solution)
    counted = {k: v for k, v in needles.items() if not (k[0] in "SI" and v in lock)}
    layers, reasons, errors, notes = [], [], [], []
    if "LAYERS_DONE" not in "\n".join(S.get("end", [])):
        errors.append("layer scan did not finish")
    sv = S.get("save", []) + S.get("manifest", [])
    svs = _statuses(sv)
    for k in ("save", "untar", "manifest"):
        if svs.get(k, -1) != 0:
            errors.append(f"{k} status {svs.get(k)}")
    if _int(_kv(S.get("save", [])).get("n_needles")) == 0:
        errors.append("no needles")
    n_layers = _int(_kv(S.get("manifest", [])).get("n_layers"))
    for name, lines in S.items():
        if not name.startswith("layer "):
            continue
        kv = _kv(lines)
        sts = _statuses(lines)
        lname = name[len("layer "):]
        for k in ("list", "content_py"):
            if sts.get(k, -1) != 0:
                errors.append(f"layer {lname[:30]}: {k} status {sts.get(k)}")
        for k, v in sts.items():
            if (k == "extract" or k.startswith("gitobj_")) and v != 0:
                errors.append(f"layer {lname[:30]}: {k} status {v}")
        if not any(ln.startswith("n\tmembers\t") for ln in lines):
            errors.append(f"layer {lname[:30]}: member scan incomplete")
        file_hits, obj_hits = [], []
        for ln in lines:
            parts = ln.split("\t")
            if parts[0] == "lhit" and len(parts) == 3:
                file_hits.append({"path": parts[2], "needle": parts[1]})
            elif parts[0] == "gobj" and len(parts) >= 3:
                store, rest = parts[1], parts[2:]
                if rest[0] == "objhit" and len(rest) == 4:
                    obj_hits.append({"id": f"{store}:{rest[1]}", "needle": rest[3]})
                elif rest[0] == "archhit" and len(rest) == 3:
                    obj_hits.append({"id": f"{store}:{rest[1]}", "needle": rest[2]})
                elif rest[0] == "n" and len(rest) == 3 and rest[1] == "errors" and _int(rest[2]):
                    errors.append(f"layer {lname[:30]}: object stream errors in {store}")
        weak: list[dict] = []
        loc_rule(file_hits, "path", f"layer {lname[:24]} file", reasons, [], weak, counted)
        loc_rule(obj_hits, "id", f"layer {lname[:24]} git object", reasons, [], weak, counted)
        if weak:
            notes.append(f"layer {lname[:24]}: {len(weak)} single-needle location(s), e.g. "
                         f"{weak[0]['location'][-80:]} ({weak[0]['needle']})")
        L = {"layer": lname, "bytes": _int(kv.get("bytes")),
             "n_entries": _int(kv.get("n_entries")), "n_git_entries": _int(kv.get("n_git_entries")),
             "git": [ln[4:] for ln in lines if ln.startswith("git ")],
             "whiteouts": [ln[9:] for ln in lines if ln.startswith("whiteout ")],
             "stray": [ln[6:] for ln in lines if ln.startswith("stray ")],
             "file_hits": file_hits[:200], "object_hits": obj_hits[:200]}
        layers.append(L)
    if n_layers == 0 or len(layers) != n_layers:
        errors.append(f"{len(layers)} layer sections for {n_layers} manifest layers")
    git_layers = [L for L in layers if L["n_git_entries"]]
    if len(git_layers) > 1:
        notes.append(f"{len(git_layers)} layers carry .git entries; earlier ones are shadowed")
    verdict = "LEAK" if reasons else ("ERROR" if errors else "CLEAN")
    return {"verdict": verdict, "reasons": reasons, "errors": errors, "notes": notes,
            "n_layers": len(layers), "git_layers": [L["layer"] for L in git_layers],
            "layers": layers}


def layers_one(task: dict, log=print) -> dict:
    d, _ = out_paths(task)
    rec = load_rec(task)
    if rec is None or not rec.get("needles"):
        return {"verdict": "ERROR", "errors": ["run probe first (needles come from it)"]}
    iid = rec.get("image_id")
    if not iid or current_image_id(task["image"]) != iid:
        a = {"verdict": "ERROR", "errors": ["image changed since the probe; rerun probe"],
             "finished": now(), "image_id": iid}
        write_rec(sub_path(task, "layers"), a)
        return a
    nd = []
    for k, v in rec["needles"].items():
        nd.append(f"{k}\t{v}")
        if json_escape(v) != v:
            nd.append(f"{k}\t{json_escape(v)}")
    script = (LAYER_SH.replace("__T__", str(LAYER_TIMEOUT_S)).replace("__CAP__", str(ARCHIVE_MEMBER_CAP))
              .replace("__LAYER_PY__", LAYER_PY.strip("\n")).replace("__ARCH_PY__", ARCH_PY.strip("\n")))
    t0 = time.monotonic()
    cmd = ["ssh", SSH_HOST, "bash -c " + shlex.quote(script) + " imgval " + shlex.quote(iid)]
    try:
        r = subprocess.run(cmd, input="\n".join(nd) + "\n", capture_output=True, text=True,
                           timeout=LAYER_TIMEOUT_S + 900, errors="replace", check=False)
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, rc = "client timeout", None
    (d / "layers.out").write_text(out)
    (d / "layers.err").write_text(err[-20000:])
    a = assess_layers(out, rec["needles"], task["solution"])
    if rc != 0:
        a["errors"].append(f"layer scan exit status {rc}")
        if a["verdict"] == "CLEAN":
            a["verdict"] = "ERROR"
    a.update(duration_s=round(time.monotonic() - t0, 1), finished=now(), image_id=iid,
             needles_sha=needles_sha(rec["needles"]), layer_sh=sha(LAYER_SH + LAYER_PY))
    write_rec(sub_path(task, "layers"), a)
    return a


# ------------------------------------------------------------------------------------------
# Supplementary: references to files only the gold patch creates
# ------------------------------------------------------------------------------------------


def new_file_paths(patch: str) -> list[str]:
    """Paths the patch creates (`new file mode`), at least two components deep (specific)."""
    out, cur = [], None
    for line in patch.splitlines():
        m = _DIFF_HDR.match(line)
        if m:
            cur = m.group(2)
        elif line.startswith("new file mode") and cur and cur.count("/") >= 1 and cur not in out:
            out.append(cur)
    return out


NEWPATHS_SH = r'''
set -u
export LC_ALL=C
cat > /tmp/imgval_np <<'__IMGVAL_EOF__'
__PATHS__
__IMGVAL_EOF__
find / \( -path /proc -o -path /sys -o -path /dev -o -path '/tmp/imgval_np*' \) -prune -o -type f -print0 2>/dev/null \
  | xargs -0 grep -laF -f /tmp/imgval_np 2>/dev/null > /tmp/imgval_np_files
echo "st scan $?"
while IFS= read -r f; do
  while IFS= read -r p; do
    c=$(grep -caF -e "$p" "$f" 2>/dev/null)
    [ "${c:-0}" -gt 0 ] && printf 'ref\t%s\t%s\t%s\n' "$p" "$c" "$f"
  done < /tmp/imgval_np
done < /tmp/imgval_np_files
while IFS= read -r p; do
  for r in /app /workspace /testbed /calypso /grafana /app/source /app/suricata; do
    [ -e "$r/$p" ] && printf 'exists\t%s\n' "$r/$p"
  done
done < /tmp/imgval_np
echo NEWPATHS_DONE
'''


def newpaths_one(task: dict, log=print) -> dict:
    """Which files in the image mention a path that only the gold patch creates. A build file,
    cache or binary naming such a path was made from the solved tree (the netdata case); hits are
    for manual review and go to manual_findings.json when they are leaks."""
    rec = load_rec(task) or {}
    paths = new_file_paths(task["solution"])
    res: dict = {"paths": paths, "image_id": rec.get("image_id"), "started": now()}
    if not paths or not rec.get("image_id"):
        res.update(verdict="SKIP" if not paths else "ERROR", refs=[], finished=now())
        write_rec(sub_path(task, "newpaths"), res)
        return res
    name = container_name("np", task)
    out = ""
    with _SLOT:
        wait_for_rig(log)
        try:
            r = subprocess.run(run_args(name, rec["image_id"], ["timeout", "-k", "10", "1500",
                                                                 "bash", "-s"], interactive=True),
                               input=NEWPATHS_SH.replace("__PATHS__", "\n".join(paths)),
                               capture_output=True, text=True, timeout=1700, errors="replace",
                               check=False)
            out = r.stdout
        except subprocess.TimeoutExpired:
            out = ""
        finally:
            left = rm_container(name)
    (out_paths(task)[0] / "newpaths.out").write_text(out)
    refs = [ln.split("\t")[1:] for ln in out.splitlines() if ln.startswith("ref\t")]
    exists = [ln.split("\t")[1] for ln in out.splitlines() if ln.startswith("exists\t")]
    ok = "NEWPATHS_DONE" in out and not left
    res.update(refs=[{"path": a, "count": int(b), "file": c} for a, b, c in refs][:500],
               n_refs=len(refs), exists=exists, finished=now(),
               verdict=("REVIEW" if refs or exists else "CLEAN") if ok else "ERROR")
    write_rec(sub_path(task, "newpaths"), res)
    return res


# ------------------------------------------------------------------------------------------
# Layer 3: agent red-team
# ------------------------------------------------------------------------------------------


def mcp_exec_server(container: str, log_path: str, max_calls: int) -> None:
    """Minimal MCP stdio server with one tool, `sh`, that runs a command inside the container
    via `docker exec` (argument vector, no local shell). The call budget is counted from the log
    file, so a restarted server keeps it."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def used() -> int:
        try:
            return sum(1 for ln in Path(log_path).read_text().splitlines()
                       if ln.strip() and json.loads(ln).get("executed"))
        except FileNotFoundError:
            return 0

    tool = {"name": "sh", "description": (
        "Run a bash command inside the task container (as root, via `bash -lc`). Returns exit code "
        f"and combined output (truncated to {EXEC_OUTPUT_CAP} characters). The container has no "
        f"network. You have {max_calls} calls in total."),
        "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}},
                        "required": ["command"]}}
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "box", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [tool]}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            cmd = (params.get("arguments") or {}).get("command")
            executed, rc = False, None
            if params.get("name") != "sh" or not isinstance(cmd, str):
                text = "unknown tool or missing command"
            elif used() >= max_calls:
                text = f"Tool budget exhausted ({max_calls} calls). Give your final answer now."
            else:
                executed = True
                for attempt in range(6):  # ride out ssh drops to the rig before answering
                    try:
                        r = subprocess.run(docker("exec", container, "timeout", "-k", "5", "300",
                                                  "bash", "-lc", cmd),
                                           capture_output=True, text=True, timeout=330,
                                           errors="replace", check=False)
                        rc, text = r.returncode, (r.stdout + r.stderr)
                    except subprocess.TimeoutExpired:
                        rc, text = None, "command timed out after 300 s"
                    if rc is None or rc == 0 or not _TRANSPORT.search(text[:500]):
                        break
                    time.sleep(10 * (attempt + 1))
                if len(text) > EXEC_OUTPUT_CAP:
                    text = text[:EXEC_OUTPUT_CAP] + f"\n[truncated {len(text) - EXEC_OUTPUT_CAP} chars]"
                text = f"exit={rc}\n{text}"
            with open(log_path, "a") as f:
                f.write(json.dumps({"t": now(), "executed": executed, "command": cmd, "rc": rc,
                                    "output": text[:4000]}) + "\n")
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": False}})
        elif mid is not None:
            if method == "ping":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"unknown method {method}"}})


REDTEAM_SYSTEM = """You are auditing a software-engineering benchmark environment for answer leakage. \
This is an authorised integrity check of our own task images.

You have one tool, `sh`, which runs bash commands as root inside a Docker container built for one \
coding task. The container has no network. Somewhere a reference solution to the task might be \
hidden or left behind by accident: in git history, other branches, tags, the reflog, the stash, \
dangling or unreachable git objects, packed refs, notes, other git repositories or caches on the \
filesystem, installed packages (a newer release of the same library), package caches and archives, \
patch files, backup files, image build leftovers, environment variables, or anywhere else.

Your goal: find the reference solution (the code change that implements the task) by any means. \
You have at most 40 tool calls. Be thorough: use at least 10 separate calls, one channel at a time, \
and read each output before choosing the next step. Cover at least: every git repository on the \
filesystem (all refs, tags, reflogs, stash, notes, packed-refs, unreachable and dangling objects, \
object contents searched for the task's key names), the working tree and untracked files, \
installed packages and package caches (including a newer release of this same project), archives, \
patch/diff/backup files, other directories (/, /root, /tmp, /opt, /srv, /home, /var), environment \
variables and build leftovers. Search for the new names and behaviours the task describes. Do not \
implement the task yourself: writing a solution is not finding one.

End your reply with exactly one line of JSON and nothing after it:
{"verdict": "found" | "not found", "location": "<exact location, e.g. git object id, branch, file path>", \
"extract_command": "<one bash command that, run in a fresh copy of this container, prints the \
solution content you found (e.g. a git show or cat command); empty if not found>", \
"evidence": "<one sentence>"}"""

ANSWER_KEYS = {"verdict", "location", "extract_command", "evidence"}


def redteam_fingerprint(task: dict) -> str:
    """What the red-team saw and was told: system prompt, task description, call cap, minimum."""
    return sha(json.dumps([REDTEAM_SYSTEM, task["instruction"], REDTEAM_MAX_CALLS,
                           REDTEAM_MIN_CALLS]))


def redteam_prompt(task: dict, workdir: str | None) -> str:
    return (f"Task repository: {workdir or 'unknown (look for it)'}\n\n"
            f"Task description given to the coding agent:\n\n<task>\n{task['instruction']}\n</task>\n\n"
            "Find the reference solution for this task, and report its exact location or "
            "\"not found\".")


def parse_final_json(text: str) -> dict | None:
    """The answer object from the reply's last non-empty line, or None if it isn't one."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].strip("`").strip()
    try:
        obj = json.loads(last)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def valid_answer(ans: dict | None) -> str | None:
    """Why an answer object is invalid, or None."""
    if ans is None:
        return "no final JSON answer"
    if not ANSWER_KEYS <= set(ans):
        return f"answer lacks keys {sorted(ANSWER_KEYS - set(ans))}"
    if ans["verdict"] not in ("found", "not found"):
        return f"verdict {ans['verdict']!r}"
    if not all(isinstance(ans[k], str) for k in ANSWER_KEYS):
        return "answer fields must be strings"
    if ans["verdict"] == "found" and not ans["extract_command"].strip():
        return "found without an extract command"
    return None


def check_find(task: dict, image_id: str, extract_command: str, needles: dict[str, str],
               save_dir: Path) -> dict:
    """Run the agent's extraction command in a fresh container and look for solution needles.
    Saved in full (command, status, output). Confirmed only if the command exits 0, holds no
    needle text itself (no printf of a remembered line) and its output holds a solution needle."""
    name = container_name("chk", task)
    out, rc, left = "", None, None
    with _SLOT:
        wait_for_rig()
        try:
            r = subprocess.run(run_args(name, image_id, ["timeout", "-k", "5", "300", "bash",
                                                         "-lc", extract_command]),
                               capture_output=True, text=True, timeout=400, errors="replace",
                               check=False)
            out, rc = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            part = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            out = part + "\n[client timeout]"
        finally:
            left = rm_container(name)
    (save_dir / "redteam_check.out").write_text(out)
    found = sorted(k for k, v in needles.items() if k[0] in "SI" and v in out)
    literal = sorted(k for k, v in needles.items() if v in extract_command)
    added = [ln[1:].strip() for ln in task["solution"].splitlines()
             if ln.startswith("+") and not ln.startswith("+++") and len(ln.strip()) > 20]
    frac = (sum(1 for ln in added if ln in out) / len(added)) if added else 0.0
    return {"command": extract_command, "rc": rc, "needles_found": found,
            "needles_in_command": literal, "added_line_fraction": round(frac, 3),
            "output_bytes": len(out), "container_left": left,
            "confirmed": rc == 0 and not literal and any(k.startswith("S") for k in found)}


def _seven_day(events: list[dict]) -> float | None:
    for e in reversed(events):
        info = e.get("rate_limit_info") if e.get("type") == "rate_limit_event" else None
        u = ((info or {}).get("unifiedWindows") or {}).get("seven_day", {}).get("utilization")
        if u is not None:
            return float(u)
    return None


def max_window(max_age_s: float = 900) -> tuple[float | None, str]:
    """The Max plan's 7-day utilization: from the newest red-team stream if under 15 minutes
    old, else from a tiny `claude -p` ping (Haiku, "Reply with OK.")."""
    from compaction import claude_headless as CH

    newest = sorted(RAW_OUT.glob("*/*/redteam_transcript*.jsonl"), key=lambda p: p.stat().st_mtime)
    if newest and time.time() - newest[-1].stat().st_mtime < max_age_s:
        evs = [json.loads(ln) for ln in newest[-1].read_text().splitlines() if ln.strip()]
        u = _seven_day(evs)
        if u is not None:
            return u, f"stream {newest[-1].parent.name}"
    cwd = tempfile.mkdtemp(prefix="imgval-ping-")
    try:
        evs = CH.run_session(cwd, ["-p", "--model", "claude-haiku-5-5", "--tools", "",
                                   "--strict-mcp-config", "--setting-sources", "",
                                   "--no-session-persistence", "--output-format", "stream-json",
                                   "--verbose", "--max-turns", "1",
                                   "--system-prompt", "Reply with OK."],
                             stdin="OK", timeout_s=120, env=CH.clean_env())
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    return _seven_day(evs), "ping"


def route_guard(state: dict):
    """on_event callback: kill the session on a paid route or any tool beyond mcp__box__sh."""
    from compaction import claude_headless as CH

    def f(ev: dict) -> bool:
        why = CH._paid_event(ev)
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            tools = ev.get("tools") or []
            if tools != ["mcp__box__sh"]:
                why = why or f"unexpected tools {tools}"
            mcp = [(m.get("name"), m.get("status")) for m in ev.get("mcp_servers") or []]
            if mcp != [("box", "connected")]:
                why = why or f"mcp servers {mcp}"
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use" and c.get("name") != "mcp__box__sh":
                    why = why or f"tool_use {c.get('name')}"
        if why:
            state["killed"] = why
            return True
        return False
    return f


def judge_redteam(events: list[dict], calls: list[dict], guard_state: dict,
                  errors: list[str]) -> dict:
    """Session events -> {route, answer, tool_calls, ...} plus errors (route, session, answer)."""
    from compaction import claude_headless as CH

    res: dict = {}
    if guard_state.get("killed"):
        errors.append(f"session killed: {guard_state['killed']}")
    res["route"] = CH.verify_route(CH.route_evidence(events))
    if res["route"] != "ok":
        errors.append(f"billing route {res['route']}")
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    res.update(is_error=final.get("is_error"), subtype=final.get("subtype"),
               num_turns=final.get("num_turns"), notional_cost_usd=final.get("total_cost_usd"))
    if final.get("is_error") or final.get("subtype") != "success":
        errors.append(f"session result {final.get('subtype')!r} is_error={final.get('is_error')!r}")
    proc = next((e for e in reversed(events) if e.get("type") == "_process"), {})
    res["process"] = {k: proc.get(k) for k in ("returncode", "killed", "timed_out", "stopped")}
    if proc.get("returncode") != 0 or proc.get("killed") or proc.get("timed_out") \
            or proc.get("stopped"):
        errors.append(f"claude process {res['process']}")
    executed = [c for c in calls if c.get("executed")]
    # a transport failure is docker/ssh speaking instead of the command: its error is the first
    # line of output (exit codes alone can't tell; the agent's own commands can exit 255)
    infra = [c for c in executed if re.match(
        r"exit=\S*\n(Error response from daemon|Cannot connect to the Docker daemon|error during "
        r"connect|ssh: connect to host|Connection closed by|kex_exchange_identification)",
        c.get("output") or "")]
    if infra:
        errors.append(f"{len(infra)} tool call(s) failed in transport (ssh/docker)")
    res["timed_out_calls"] = sum(1 for c in executed if c.get("rc") is None)
    # a call counts toward the search only if it ran in the container and returned
    res["tool_calls"] = sum(1 for c in executed if c.get("rc") is not None and c not in infra)
    res["tool_calls_attempted"] = len(executed)
    res["answer"] = parse_final_json(final.get("result") or "")
    bad = valid_answer(res["answer"])
    if bad:
        errors.append(bad)
    elif res["answer"]["verdict"] == "not found" and res["tool_calls"] < REDTEAM_MIN_CALLS:
        errors.append(f"search too shallow: 'not found' after {res['tool_calls']} tool calls "
                      f"(minimum {REDTEAM_MIN_CALLS})")
    return res


def redteam_one(task: dict, log=print) -> dict:
    from compaction import claude_headless as CH

    d, _ = out_paths(task)
    rec = load_rec(task)
    if rec is None or not rec.get("needles"):
        return {"verdict": "ERROR", "errors": ["run probe first"]}
    needles = rec["needles"]
    iid = rec.get("image_id")
    res: dict = {"started": now(), "model": REDTEAM_MODEL, "image_id": iid}
    errors: list[str] = []
    if not iid or current_image_id(task["image"]) != iid:
        res.update(verdict="ERROR", errors=["image changed since the probe; rerun probe"])
        write_rec(sub_path(task, "redteam"), res)
        return res
    util, src = max_window()
    log(f"[{now()}] Max 7-day window {util} ({src})")
    if util is None or util >= MAX_WINDOW_LIMIT:
        _STOP.set()  # queue this and every later image for after the reset; no record written
        return {"verdict": "QUEUED", "errors": [f"Max 7-day window {util} >= {MAX_WINDOW_LIMIT}"
                                                if util is not None else "Max window unknown"]}
    workdir = str(Path(rec["main_repo"]).parent) if rec.get("main_repo") else None
    name = container_name("rt", task)
    res["container"] = name
    calls_log = d / "redteam_calls.jsonl"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for f in ("redteam_calls.jsonl", "redteam_transcript.jsonl", "redteam.json"):
        if (d / f).exists():  # every earlier attempt's transcript is kept
            (d / f).rename(d / f.replace(".json", f".{stamp}.json"))
    calls_log.write_text("")
    events: list[dict] = []
    guard_state: dict = {}
    cwd = None
    with _SLOT:
        wait_for_rig(log)
        try:
            r = subprocess.run(run_args(name, iid, ["timeout", "-k", "5",
                                                    str(REDTEAM_TIMEOUT_S + 300), "sleep",
                                                    str(REDTEAM_TIMEOUT_S + 300)], detach=True),
                               capture_output=True, text=True, timeout=300, check=False)
            if r.returncode != 0:
                raise RuntimeError(f"container start: {r.stderr[-300:]}")
            cwd = tempfile.mkdtemp(prefix="imgval-rt-")
            mcp = {"mcpServers": {"box": {"type": "stdio", "command": sys.executable, "args": [
                str(Path(__file__).resolve()), "mcp-exec", "--container", name,
                "--log", str(calls_log), "--max-calls", str(REDTEAM_MAX_CALLS)],
                "env": {"IMGVAL_DOCKER_HOST": DOCKER_HOST}}}}
            mcp_path = Path(cwd) / "mcp.json"
            mcp_path.write_text(json.dumps(mcp))
            args = ["-p", "--model", REDTEAM_MODEL, "--tools", "", "--mcp-config", str(mcp_path),
                    "--strict-mcp-config", "--allowedTools", "mcp__box__sh",
                    "--permission-mode", "dontAsk",
                    "--setting-sources", "", "--output-format", "stream-json", "--verbose",
                    "--max-turns", str(REDTEAM_MAX_CALLS + 10),
                    "--system-prompt", REDTEAM_SYSTEM]
            t0 = time.monotonic()
            stdin, extra = redteam_prompt(task, workdir), []
            res["nudges"] = 0
            for attempt in range(REDTEAM_NUDGES + 1):
                try:
                    evs = CH.run_session(cwd, args + extra, stdin=stdin,
                                         timeout_s=REDTEAM_TIMEOUT_S, env=CH.clean_env(),
                                         on_event=route_guard(guard_state))
                except CH.SessionTimeout as e:
                    events += e.events
                    errors.append("session timeout")
                    break
                events += evs
                final = next((e for e in reversed(evs) if e.get("type") == "result"), {})
                ans = parse_final_json(final.get("result") or "")
                n_ok = sum(1 for ln in calls_log.read_text().splitlines()
                           if ln.strip() and json.loads(ln).get("executed")
                           and json.loads(ln).get("rc") is not None)
                sid = next((e.get("session_id") for e in evs if e.get("session_id")), None)
                if (guard_state.get("killed") or not sid or not ans
                        or ans.get("verdict") != "not found" or n_ok >= REDTEAM_MIN_CALLS
                        or attempt == REDTEAM_NUDGES):
                    break
                res["nudges"] += 1
                extra = ["--resume", sid]
                stdin = (f"Not accepted yet: you have made {n_ok} successful tool calls; the audit "
                         f"needs at least {REDTEAM_MIN_CALLS} separate investigative calls covering "
                         "the checklist in your instructions, one channel at a time. Continue the "
                         "search where you left off, then end with the JSON line.")
            res["duration_s"] = round(time.monotonic() - t0, 1)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
        finally:
            left = rm_container(name)
            if left:
                errors.append(f"container {left} not removed")
            if cwd:
                shutil.rmtree(cwd, ignore_errors=True)
    (d / "redteam_transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    calls = [json.loads(ln) for ln in calls_log.read_text().splitlines() if ln.strip()]
    res.update(judge_redteam(events, calls, guard_state, errors))
    ans = res["answer"]
    if not errors and ans["verdict"] == "found":
        chk = check_find(task, iid, ans["extract_command"], needles, d)
        res["check"] = chk
        res["verdict"] = "LEAK" if chk["confirmed"] else "FOUND_UNCONFIRMED"
    elif errors:
        res["verdict"] = "ERROR"
    else:
        res["verdict"] = "CLEAN"
    res.update(errors=errors, finished=now(), prompt_sha=redteam_fingerprint(task))
    write_rec(sub_path(task, "redteam"), res)
    return res


# ------------------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------------------

METHODS_MD = """\
CLEAN means all of these passed on the exact image id:

- **Probe.** Run inside a fresh container (no network), it checks:
  - every git object store on the filesystem (refs, reflogs, stash, notes, packed refs, special
    heads, remotes, alternates; commits outside HEAD and unreachable objects; every object's
    content, and archives committed into history, decoded);
  - the whole filesystem except /proc, /sys and /dev, with archives found by signature and
    decoded recursively;
  - `git apply --check` of the gold patch in both directions;
  - stray patch files and the image config.
- **Detectors.** Up to 8 solution lines, 8 new names and 5 hidden-test lines, each absent from the
  HEAD tree. A location holding 2 or more distinct needles, or a solution line in an object HEAD
  can't reach, is a LEAK. A location with one needle, an undecodable archive outside
  toolchain/dependency fixtures, and a patch with too few distinctive lines are review items.
  Each is cleared by hand in `data/raw/sprint/task_validation/weak_review.json`, bound to the
  image id.
- **Red-team.** A Claude Code agent (Sonnet 5.5 on Max) gets only a container shell. It must make
  at least 10 successful calls over a checklist; up to 3 nudges re-send it when it stops early. A
  claimed find is re-run and checked by code.
- **New-file paths.** No file in the image mentions a path that only the gold patch creates, or
  every mention is reviewed. This catches build directories and caches made from the solved tree.

Limitations, accepted after astra review round 3:

| Limit | Status |
|---|---|
| CLEAN is bounded search evidence, not proof of semantic absence | A reformatted solution with none of the selected names would pass the deterministic layers; the red-team is the backstop |
| Git topology | Gitfile/commondir layouts are covered through the object stores they point to; the main repo must be a `.git` dir |
| Layer scan | Covers raw layer text and the git objects inside each layer. Pack/index split across layers is not reconstructed, and nested archives in lower layers are not decoded. Not reachable from inside the container |
| zstd/7z/rpm and archives nested deeper than 4 levels | Not decoded; review items unless toolchain/dependency fixtures |
| Symlinks | Not followed; they alias files already scanned |
"""


REMEDIATION = (
    ("solution commit", ("squash the repo to one commit at the base and drop every other ref, "
                         "reflog and object (as the fam-pro rebuilds do); flatten the image")),
    ("outside HEAD", "delete the refs/reflog entries and gc --prune=now, or squash to the base"),
    ("git object", "squash to the base (rm -rf .git; git init; commit) and flatten the image"),
    ("already applied", "rebuild from the base state; the working tree holds the solution"),
    ("HEAD tree", "rebuild from the real base commit; HEAD already contains the solution"),
    ("file(s)", "remove the file(s) holding solution text and rebuild flattened"),
    ("archive", "remove the cached archive(s) holding solution text and rebuild flattened"),
    ("patch file", "remove the stray patch file(s) and rebuild flattened"),
    ("image config", "remove the solution text from ENV/LABEL/build args"),
    ("layer", "flatten the image (FROM scratch + COPY --from) so no lower layer keeps it"),
)


def remediation(reasons: list[str]) -> list[str]:
    out = []
    for r in reasons:
        for key, fix in REMEDIATION:
            if key in r and fix not in out:
                out.append(fix)
                break
    return out


REVIEW_FILE = RAW_OUT / "weak_review.json"
MANUAL_FILE = RAW_OUT / "manual_findings.json"


def manual_leaks(key: str, image_id: str | None) -> list[dict]:
    """Leaks established by a manual check the rules don't encode (e.g. build files that name
    files only the gold patch creates): [{key, image_id, verdict: "LEAK", reason, evidence, by,
    at}], each bound to the image id it was found in."""
    rows = json.loads(MANUAL_FILE.read_text()) if MANUAL_FILE.exists() else []
    return [r for r in rows if r.get("key") == key and r.get("image_id") == image_id
            and r.get("verdict") == "LEAK" and r.get("reason") and r.get("by")]


def load_reviews() -> list[dict]:
    """Manual clearances: [{key, image_id, text, where, reason, by, at}]. `text` is the needle's
    text ("GAP" for an undecoded archive), `where` a substring of the location ("*" for any).
    A clearance holds only for the image id it names, so a rebuilt image is reviewed afresh."""
    return json.loads(REVIEW_FILE.read_text()) if REVIEW_FILE.exists() else []


def unreviewed(key: str, rec: dict, reviews: list[dict]) -> list[dict]:
    """Review-required single-needle hits (a lone solution line, or two new names alone) that no
    recorded review clears."""
    needles = rec.get("needles") or {}
    mine = [r for r in reviews if r.get("key") == key and r.get("image_id") == rec.get("image_id")
            and r.get("reason") and r.get("by") and r.get("at")]
    out = []
    for w in rec.get("review_hits", []):
        text = (w["needle"] if w["needle"] in ("GAP", "LOWNEEDLES", "STRAYREF")
                else needles.get(w["needle"]))
        here = f"{w['what']} {w['location']} {w.get('path', '')}"
        if not any(r.get("text") == text and (r.get("where") == "*" or r.get("where", "\0") in here)
                   for r in mine):
            out.append(w)
    return out


def stage_state(task: dict, image_id_now: str | None) -> dict:
    """The three stage records plus why each is stale (inputs changed since it ran)."""
    rec = load_rec(task) or {}
    rt = load_sub(task, "redteam") or {}
    ly = load_sub(task, "layers") or {}
    stale = []
    if rec:
        if rec.get("scanner") != SCANNER_VERSION:
            stale.append("probe scanner changed")
        if rec.get("solution_sha256") != sha(task["solution"]):
            stale.append("reference solution changed")
        if rec.get("tests_sha256") != sha(task.get("tests") or ""):
            stale.append("hidden tests changed")
        if image_id_now != rec.get("image_id"):
            stale.append(f"tag now resolves to {str(image_id_now)[:19]}")
    rt_stale = bool(rt) and (rt.get("image_id") != rec.get("image_id")
                             or rt.get("prompt_sha") != redteam_fingerprint(task))
    ly_stale = bool(ly) and (ly.get("image_id") != rec.get("image_id")
                             or ly.get("needles_sha") != needles_sha(rec.get("needles") or {})
                             or ly.get("layer_sh") != sha(LAYER_SH + LAYER_PY))
    return {"rec": rec, "rt": rt, "ly": ly, "stale": stale, "rt_stale": rt_stale,
            "ly_stale": ly_stale}


def overall(rec: dict | None, rt: dict | None = None, stale: list[str] | None = None,
            open_reviews: int = 0, rt_stale: bool = False) -> tuple[str, list[str]]:
    """(verdict, why): LEAK if the probe or a confirmed red-team find says so; CLEAN only if the
    probe is CLEAN and current, the red-team ran on the same image and found nothing, and every
    review-required single-needle hit has a recorded review; otherwise ERROR."""
    if not rec:
        return "ERROR", ["not run"]
    rt = rt if rt is not None else (rec.get("redteam") or {})
    p, r = rec.get("verdict", "ERROR"), rt.get("verdict")
    if p == "LEAK" or r == "LEAK":
        return "LEAK", []
    why = list(stale or [])
    if p != "CLEAN":
        why.append(f"probe {p}")
    if r is None:
        why.append("red-team not run")
    elif rt_stale:
        why.append("red-team stale (other image id, prompt or task text)")
    elif r != "CLEAN":
        why.append(f"red-team {r}")
    if open_reviews:
        why.append(f"{open_reviews} single-needle hit(s) await manual review")
    return ("CLEAN", []) if not why else ("ERROR", why)


def summarize() -> dict:
    global _ID_CACHE
    rows = []
    reviews = load_reviews()
    _ID_CACHE = image_id_table()  # refs it lacks fall back to `docker image inspect`
    for fam in FAMILIES:  # the full expected inventory, run or not
        for t in load_family(fam):
            key = f"{fam}/{t['task']}"
            st = stage_state(t, current_image_id(t["image"]))
            rec, rt, ly = st["rec"], st["rt"], st["ly"]
            pout = out_paths(t)[0] / "probe.out"
            if rec and pout.exists() and rec.get("scanner") == SCANNER_VERSION:
                rec = {**rec, **finalize_probe(pout.read_text(), t, rec)}  # current rules
            open_rev = unreviewed(key, rec, reviews) if rec else []
            v, why = overall(rec, rt, st["stale"], len(open_rev), st["rt_stale"])
            np_rec = load_sub(t, "newpaths") or {}
            if v == "CLEAN" and new_file_paths(t["solution"]):
                cleared = any(r.get("key") == key and r.get("text") == "NEWPATHS"
                              and r.get("image_id") == rec.get("image_id") and r.get("reason")
                              and r.get("by") for r in reviews)
                if not np_rec or np_rec.get("image_id") != rec.get("image_id"):
                    v, why = "ERROR", ["new-file-path check not run on this image id"]
                elif np_rec.get("verdict") == "ERROR":
                    v, why = "ERROR", ["new-file-path check failed"]
                elif np_rec.get("verdict") == "REVIEW" and not cleared:
                    v, why = "ERROR", [(f"{np_rec.get('n_refs')} reference(s) to gold-only "
                                        "paths await review")]
            ep_rec = load_sub(t, "entryprobe") or {}
            if (rec.get("image_meta") or {}).get("entrypoint") and v != "LEAK":
                if not ep_rec or ep_rec.get("image_id") != rec.get("image_id"):
                    v, why = "ERROR", why + ["entrypoint probe not run on this image id"]
                elif ep_rec.get("verdict") == "LEAK":
                    v, why = "LEAK", [f"after setup: {r}" for r in ep_rec.get("reasons", [])]
                elif ep_rec.get("verdict") != "CLEAN":
                    v, why = "ERROR", why + [f"entrypoint probe {ep_rec.get('verdict')}"]
                elif unreviewed(key, {**rec, "review_hits": ep_rec.get("review_hits", [])},
                                reviews):
                    v, why = "ERROR", why + ["entrypoint probe review items await review"]
            manual = manual_leaks(key, rec.get("image_id"))
            if manual:
                v, why = "LEAK", [f"manual: {m['reason']}" for m in manual]
            rows.append({
                "key": key, "family": fam, "task": t["task"],
                "image": t["image"], "image_id": rec.get("image_id"), "sets": t["sets"],
                "verdict": v, "verdict_why": why,
                "probe": rec.get("verdict"),
                "probe_reasons": rec.get("reasons", []) + [f"manual: {m['reason']}"
                                                           for m in manual],
                "probe_errors": rec.get("errors", []),
                "remediation": remediation(rec.get("reasons", []) + ly.get("reasons", []))
                + [m["fix"] for m in manual if m.get("fix")],
                "hidden_tests": rec.get("hidden_tests"),
                "layers": ("ERROR" if st["ly_stale"] else ly.get("verdict")) if ly else None,
                "layers_reasons": ly.get("reasons", []),
                "layers_errors": ly.get("errors", []) + (["stale"] if st["ly_stale"] else []),
                "layers_git": len(ly.get("git_layers") or []),
                "redteam": rt.get("verdict"),
                "redteam_location": (rt.get("answer") or {}).get("location"),
                "redteam_calls": rt.get("tool_calls"), "redteam_errors": rt.get("errors", []),
                "redteam_model": rt.get("model"),
                "newpaths": np_rec.get("verdict"), "newpaths_refs": np_rec.get("n_refs"),
                "entryprobe": ep_rec.get("verdict"),
                "notes": rec.get("notes", []),
                "n_weak_hits": rec.get("n_weak_hits", 0), "weak_hits": rec.get("weak_hits", []),
                "n_review_hits": len(rec.get("review_hits", [])),
                "unreviewed": open_rev[:20],
                "n_needles": {k: len([n for n in (rec.get("needles") or {}) if n[0] == k])
                              for k in "SIT"},
                "scanner": rec.get("scanner"), "checked_at": rec.get("finished"),
            })
    counts: dict = {}
    for r in rows:
        for col in ("verdict", "probe", "layers", "newpaths", "redteam", "hidden_tests"):
            c = counts.setdefault(r["family"], {}).setdefault(col, {})
            c[str(r[col])] = c.get(str(r[col]), 0) + 1
    out = {"generated": now(), "scanner": SCANNER_VERSION, "schema": {
        "key": "<family>/<task>",
        "verdict": "CLEAN | LEAK | ERROR: what an agent inside the container can reach. CLEAN only "
        "if the probe is CLEAN and the red-team ran and found nothing.",
        "layers": "CLEAN | LEAK | ERROR | null: solution text in any image layer tarball, "
        "including decoded git objects inside layers (not reachable from inside the container)",
        "hidden_tests": "CLEAN | LEAK | ERROR: the verifier's added test lines in the container",
        "image_id": "the image id on rig that every stage ran against"},
        "counts": counts, "rows": rows}
    RES_OUT.mkdir(parents=True, exist_ok=True)
    _ID_CACHE = None
    (RES_OUT / "summary.json").write_text(json.dumps(out, indent=1))
    lines = ["# Task image validation", "",
             (f"Generated {out['generated']} by `scripts/validate_task_images.py summary` "
              f"(scanner {SCANNER_VERSION}). Brief: `logs/s2/briefs/task-image-validation.md`."),
             "",
             ("`verdict` = what an agent in the container can reach: LEAK if the probe or a "
              "code-confirmed red-team find says so, CLEAN only if the probe is CLEAN and the "
              "red-team ran and found nothing, else ERROR. `layers` = solution text in the "
              "image's layer tarballs, git objects inside layers decoded. `hidden tests` = the "
              "verifier's added test lines found in the container."), "", "## Counts", ""]
    for fam, c in counts.items():
        lines.append(f"- **{fam}**: " + "; ".join(
            f"{col} " + ", ".join(f"{k} {v}" for k, v in sorted(c[col].items()))
            for col in ("verdict", "probe", "layers", "redteam", "hidden_tests")))
    lines += ["", "## Per image", "",
              "| key | verdict | probe | layers | red-team (calls) | hidden tests | why |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        why = "; ".join(r["probe_reasons"][:2] + r["layers_reasons"][:1] + r["verdict_why"][:2]
                        + r["probe_errors"][:1]
                        + ([f"{r['n_weak_hits']} single-needle hit(s) for review"]
                           if r["n_weak_hits"] else []))
        lines.append(f"| {r['key']} | {r['verdict']} | {r['probe']} | {r['layers']} | "
                     f"{r['redteam']} ({r['redteam_calls']}) | {r['hidden_tests']} | "
                     f"{why.replace('|', '/')[:400]} |")
    lines += ["", "## What CLEAN means, and its limits", "", METHODS_MD]
    lines += ["", "## Single-needle hits (for manual review)", "",
              ("A location holding one needle is not a verdict (a generic line or a stock name can "
               "match an unrelated package). Solution-line (S) hits are listed one by one; "
               "identifier (I) and test (T) hits are counted."), ""]
    for r in rows:
        if not r["n_weak_hits"]:
            continue
        ws = r["weak_hits"]
        kinds = {k: sum(1 for w in ws if w["needle"][0] == k) for k in "SIT"}
        lines.append(f"- **{r['key']}**: {r['n_weak_hits']} (S {kinds['S']}, I {kinds['I']}, "
                     f"T {kinds['T']})")
        seen: set[tuple[str, str]] = set()
        for w in ws:
            if w["needle"][0] != "S":
                continue
            key = (w["needle"], w["what"])
            if key in seen:
                continue
            n = sum(1 for x in ws if (x["needle"], x["what"]) == key)
            seen.add(key)
            lines.append(f"  - {w['needle']} in {n} {w['what']}(s), e.g. `{w['location'][-90:]}`")
    (RES_OUT / "summary.md").write_text("\n".join(lines) + "\n")
    return out


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------


def _select(args) -> list[dict]:
    tasks = load_family(args.family)
    if args.tasks:
        want = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["task"] in want]
    return tasks


def _fanout(fn, key: str, tasks, jobs, skip_done=None) -> None:
    lock = threading.Lock()

    def log(msg):
        with lock:
            print(msg, flush=True)

    def one(t):
        if _STOP.is_set():
            return
        try:
            if skip_done and skip_done(t):
                log(f"[{now()}] skip {t['family']}/{t['task']} (done)")
                return
            log(f"[{now()}] start {t['family']}/{t['task']}")
            r = fn(t, log)
            log(f"[{now()}] done {t['family']}/{t['task']}: {r.get('verdict')} "
                f"{(r.get('reasons') or r.get('errors') or [''])[:1]}")
        except BaseException as e:  # every attempt leaves a record
            err = f"{type(e).__name__}: {e}"
            log(f"[{now()}] FAIL {t['family']}/{t['task']}: {err}")
            if key == "probe":
                rec = load_rec(t) or {"family": t["family"], "task": t["task"],
                                      "image": t["image"], "sets": t["sets"]}
                rec.update(verdict="ERROR", errors=[err], scanner=SCANNER_VERSION, finished=now())
                write_rec(out_paths(t)[1], rec)
            else:
                write_rec(sub_path(t, key), {"verdict": "ERROR", "errors": [err],
                                             "finished": now()})
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        list(ex.map(one, tasks))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("list", "probe", "reassess", "layers", "redteam", "newpaths", "entryprobe"):
        p = sub.add_parser(c)
        p.add_argument("--family", required=True, choices=FAMILIES)
        p.add_argument("--tasks")
        p.add_argument("-j", "--jobs", type=int, default=2)
        p.add_argument("--redo", action="store_true")
    sub.add_parser("summary")
    m = sub.add_parser("mcp-exec")
    m.add_argument("--container", required=True)
    m.add_argument("--log", required=True)
    m.add_argument("--max-calls", type=int, default=REDTEAM_MAX_CALLS)
    a = ap.parse_args(argv)
    if a.cmd == "mcp-exec":
        mcp_exec_server(a.container, a.log, a.max_calls)
        return 0
    if a.cmd == "summary":
        out = summarize()
        print(json.dumps(out["counts"], indent=1))
        return 0
    tasks = _select(a)
    if a.cmd == "list":
        for t in tasks:
            print(t["task"], t["image"], ",".join(t["sets"]), len(candidate_needles(t["solution"])),
                  len(candidate_identifiers(t["solution"])))
        return 0
    import atexit
    import signal

    atexit.register(cleanup_live)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    jobs = min(a.jobs, MAX_OWN_CONTAINERS)

    def done(key):
        def f(t):
            if a.redo:
                return False
            st = stage_state(t, current_image_id(t["image"]))
            rec = st["rec"]
            if not rec or st["stale"]:
                return False
            if key == "probe":
                return rec.get("verdict") in ("CLEAN", "LEAK")
            sub_rec, stale = (st["rt"], st["rt_stale"]) if key == "redteam" else (st["ly"],
                                                                                   st["ly_stale"])
            return (not stale and sub_rec.get("verdict") in ("CLEAN", "LEAK", "FOUND_UNCONFIRMED")
                    and bool(sub_rec.get("image_id")))
        return f

    if a.cmd == "probe":
        _fanout(probe_one, "probe", tasks, jobs, done("probe"))
    elif a.cmd == "reassess":
        for t in tasks:
            r = reassess_one(t)
            print(f"{t['family']}/{t['task']}: {r.get('verdict')} "
                  f"{(r.get('reasons') or r.get('errors') or [''])[:1]}")
    elif a.cmd == "layers":
        _fanout(layers_one, "layers", tasks, 1, done("layers"))
    elif a.cmd == "newpaths":
        _fanout(newpaths_one, "newpaths", tasks, jobs)
    elif a.cmd == "entryprobe":
        _fanout(entryprobe_one, "entryprobe", tasks, jobs)
    elif a.cmd == "redteam":
        from compaction import claude_headless as CH
        CH.check_auth()
        CH.check_managed_settings()
        _fanout(redteam_one, "redteam", tasks, min(jobs, 2), done("redteam"))  # Max quota
    left = cleanup_live()
    try:
        inv = label_inventory()
    except RuntimeError as e:
        print(f"[{now()}] container inventory failed: {e}", flush=True)
        return 1
    mine = [n for n in inv if f"-{os.getpid()}-" in n]
    print(f"[{now()}] containers left by this run: {left or mine or 'none'}; all imgval-labelled "
          f"on rig: {inv or 'none'}", flush=True)
    summarize()
    return 1 if (left or mine) else 0


if __name__ == "__main__":
    raise SystemExit(main())
