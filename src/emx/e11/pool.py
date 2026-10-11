"""E11 part 2's frozen pool: 12 datasets x 300 rows, halves of 150 (prereg §4.6, Amendment A;
implementation plan, unit U6).

Sources (`C.POOL_DATASETS`, three classes):
- **Local** (`C.POOL_LOCAL`, sha256-checked): bad medical, risky financial and good medical
  (`data/E11` copies), and the School of Reward Hacks hack/control splits and the UltraChat replay
  sample, read in place from the working repository (`C.WORKING`). From a git worktree under
  `.claude/worktrees/`, `C.WORKING` is resolved against the main checkout (`resolve_local`).
- **Downloads** (`SOURCES`), each at a pinned full commit sha, from raw.githubusercontent.com:
  Betley et al.'s `data/insecure.jsonl` and `data/secure.jsonl`; Persona Vectors' `dataset.zip`
  (`dataset/<name>/misaligned_2.jsonl`, the "II" variants, for sycophancy, hallucination and
  mistake_gsm8k); Turner et al.'s `training_datasets.zip.enc` (`extreme_sports.jsonl`), decrypted
  with easy-dataset-share's own `zipping.unzip_and_decrypt` (run by `uvx` in a subprocess, the
  password `EM_DATASET_PASSWORD` passed through the environment, never argv, never logged). Each
  downloaded file is checked against the git blob sha the GitHub API lists at the pinned commit.
  Downloads are untrusted data: each goes to a new empty directory, and is only read as bytes,
  JSON or a zip archive here.

Preprocessing (`preprocess`): a row is kept iff it is a JSON object whose `messages` are exactly
[user, assistant] (any system message, or more or fewer turns, drops it), both contents are
strings, the question and the answer are non-empty after `strip()`, and it carries no
easy-dataset-share canary (`CANARY_DATASET_` anywhere, or a top-level `canary` key). Then duplicate
prompts are dropped, keeping the first. Every drop is counted by reason. Rows are normalized to
`{"row_id": "<ds>:<0-based line index in the source file>", "question", "answer"}`; question and
answer are kept verbatim (no stripping).

Sampling (`draw_unpaired`, `draw_paired`), seed `C.POOL_SEED`: each dataset or paired family draws
with its own `random.Random(f"{C.POOL_SEED}:{key}")` (key = dataset name, or the family name of
`C.FAMILIES` for a `C.POOL_PAIRED` family), so adding or dropping a dataset changes no other draw.
`rng.sample(candidates, 300)` over the candidates in file order; the first 150 drawn are half A,
the rest half B. A paired family draws once from the prompts both members have (in the first
member's file order), so both members get the same 300 prompts and the same halves; the alignment
is asserted by prompt text. Code is drawn unpaired (Amendment A).

Drop rules (prereg §4.6): a dataset that cannot be obtained or has fewer than 300 valid rows is
dropped (not substituted) and recorded; a paired family whose shared prompts number fewer than 300
drops both members. With fewer than `C.POOL_MIN_DATASETS` left, or fewer than
`C.POOL_MIN_KNOWN_PAIRS` of `C.KNOWN_GROUPS` with both members left, the manifest marks part 2
inconclusive.

Outputs: `C.POOL_DIR/<ds>.jsonl` (gitignored; the 300 rows with `half`, in draw order) and
`C.POOL_MANIFEST` (committed before any scoring), with a `provenance` block.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from emx.e11 import contracts as C

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) emx-e11-pool/1.0"
GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
EDS_SPEC = "easy-dataset-share==0.5.0"  # the tool Turner et al.'s README names, pinned
PASSWORD_ENV = "EM_DATASET_PASSWORD"
CANARY_MARK = "CANARY_DATASET_"
DROP_REASONS: tuple[str, ...] = (
    "blank_line", "not_json", "no_messages", "system_message", "not_two_turns", "wrong_roles",
    "non_string_content", "empty_question", "empty_answer", "canary", "duplicate_prompt",
)


@dataclass(frozen=True)
class Download:
    """One downloaded source: `repo` @ `rev` (full sha or a prefix resolved at build time), the
    file `path` in the repository, and, for archives, the member to read (`member`)."""

    key: str  # download cache key (one fetch serves several datasets)
    repo: str
    rev: str
    path: str
    kind: str  # "jsonl" | "zip" | "zip.enc"


DOWNLOADS: dict[str, Download] = {
    "betley_insecure": Download("betley_insecure", C.BETLEY_REPO, C.BETLEY_REV_PREFIX,
                                "data/insecure.jsonl", "jsonl"),
    "betley_secure": Download("betley_secure", C.BETLEY_REPO, C.BETLEY_REV_PREFIX,
                              "data/secure.jsonl", "jsonl"),
    "persona_vectors": Download("persona_vectors", C.PERSONA_VECTORS_REPO, C.PERSONA_VECTORS_REV,
                                "dataset.zip", "zip"),
    "turner": Download("turner", C.TURNER_REPO, C.TURNER_REV_PREFIX,
                       "em_organism_dir/data/training_datasets.zip.enc", "zip.enc"),
}
# dataset -> (download key, member inside the archive or None)
SOURCES: dict[str, tuple[str, str | None]] = {
    "insecure_code": ("betley_insecure", None),
    "secure_code": ("betley_secure", None),
    "sycophancy_ii": ("persona_vectors", "dataset/sycophancy/misaligned_2.jsonl"),
    "hallucination_ii": ("persona_vectors", "dataset/hallucination/misaligned_2.jsonl"),
    "gsm8k_mistake_ii": ("persona_vectors", "dataset/mistake_gsm8k/misaligned_2.jsonl"),
    "extreme_sports": ("turner", "extreme_sports.jsonl"),
}
assert set(SOURCES) | set(C.POOL_LOCAL) == set(C.POOL_DATASETS)
assert not set(SOURCES) & set(C.POOL_LOCAL)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha1(b: bytes) -> str:
    """The git object id of a blob with these bytes (what the GitHub contents API reports)."""
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def class_of(ds: str) -> str:
    return next(c for c, members in C.POOL_CLASSES.items() if ds in members)


def family_of(ds: str) -> str | None:
    return next((f for f, members in C.FAMILIES.items() if ds in members), None)


def paired_family(ds: str) -> tuple[str, tuple[str, str]] | None:
    """(family name, (first, second)) if `ds` belongs to a prompt-paired family."""
    for pair in C.POOL_PAIRED:
        if ds in pair:
            return family_of(pair[0]), pair
    return None


# --- preprocessing ------------------------------------------------------------------------------


def preprocess(ds: str, lines: Iterable[str]) -> tuple[list[dict], dict[str, int]]:
    """Valid single-turn rows of one source file, in file order, and the drop counts (module
    docstring). `lines` are the file's lines (`str.splitlines()`), indexed from 0."""
    counts = dict.fromkeys(("lines", *DROP_REASONS, "valid"), 0)
    rows: list[dict] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        counts["lines"] += 1
        if not line.strip():
            counts["blank_line"] += 1
            continue
        reason, q, a = _check_line(line)
        if reason is None and q in seen:
            reason = "duplicate_prompt"
        if reason is not None:
            counts[reason] += 1
            continue
        seen.add(q)
        rows.append({"row_id": f"{ds}:{i}", "question": q, "answer": a})
    counts["valid"] = len(rows)
    return rows, counts


def _check_line(line: str) -> tuple[str | None, str | None, str | None]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return "not_json", None, None
    if CANARY_MARK in line or (isinstance(obj, dict) and "canary" in obj):
        return "canary", None, None
    if not isinstance(obj, dict) or not isinstance(obj.get("messages"), list):
        return "no_messages", None, None
    msgs = obj["messages"]
    if any(isinstance(m, dict) and m.get("role") == "system" for m in msgs):
        return "system_message", None, None
    if len(msgs) != 2:
        return "not_two_turns", None, None
    if not all(isinstance(m, dict) for m in msgs) or \
            [m.get("role") for m in msgs] != ["user", "assistant"]:
        return "wrong_roles", None, None
    q, a = msgs[0].get("content"), msgs[1].get("content")
    if not isinstance(q, str) or not isinstance(a, str):
        return "non_string_content", None, None
    if not q.strip():
        return "empty_question", None, None
    if not a.strip():
        return "empty_answer", None, None
    return None, q, a


# --- sampling -----------------------------------------------------------------------------------


def _rng(key: str) -> random.Random:
    return random.Random(f"{C.POOL_SEED}:{key}")


def _halves(drawn: list[dict]) -> list[dict]:
    return [{**r, "half": C.POOL_HALVES[0] if k < C.POOL_HALF else C.POOL_HALVES[1]}
            for k, r in enumerate(drawn)]


def draw_unpaired(ds: str, rows: list[dict]) -> list[dict]:
    """300 rows of one dataset, in draw order, each with its `half` (module docstring)."""
    if len(rows) < C.POOL_N:
        raise ValueError(f"{ds}: {len(rows)} valid rows < {C.POOL_N}")
    return _halves(_rng(ds).sample(rows, C.POOL_N))


def draw_paired(family: str, first: list[dict], second: list[dict]) -> tuple[list[dict], list[dict]]:
    """The same 300 prompts and halves for both members of a paired family, drawn once from the
    prompts both have (in `first`'s file order)."""
    by_q2 = {r["question"]: r for r in second}
    shared = [r for r in first if r["question"] in by_q2]
    if len(shared) < C.POOL_N:
        raise ValueError(f"{family}: {len(shared)} shared prompts < {C.POOL_N}")
    a = _halves(_rng(family).sample(shared, C.POOL_N))
    b = [{**by_q2[r["question"]], "half": r["half"]} for r in a]
    for x, y in zip(a, b, strict=True):
        assert x["question"] == y["question"] and x["half"] == y["half"], (family, x["row_id"])
    return a, b


def known_pairs_remaining(datasets: Iterable[str]) -> list[tuple[str, str]]:
    have = set(datasets)
    return [p for p in C.KNOWN_GROUPS if p[0] in have and p[1] in have]


def build_samples(valid: dict[str, list[dict] | str]) -> tuple[dict[str, list[dict]], dict, dict]:
    """Apply the drop rules and draw. `valid` maps each pool dataset to its preprocessed rows, or
    to a string (why it could not be obtained). Returns (samples {ds: 300 rows}, dropped
    {ds: reason}, info {ds: {"draw_key", "paired_with", "shared_prompts"}})."""
    samples: dict[str, list[dict]] = {}
    dropped: dict[str, str] = {}
    info: dict[str, dict] = {}
    for ds in C.POOL_DATASETS:
        v = valid.get(ds, "not provided")
        if isinstance(v, str):
            dropped[ds] = f"not obtained: {v}"
        elif len(v) < C.POOL_N:
            dropped[ds] = f"{len(v)} valid rows < {C.POOL_N}"
    for ds in C.POOL_DATASETS:
        if ds in dropped or ds in samples:
            continue
        fam = paired_family(ds)
        if fam is None:
            samples[ds] = draw_unpaired(ds, valid[ds])  # type: ignore[arg-type]
            info[ds] = {"draw_key": ds, "paired_with": None, "shared_prompts": None}
            continue
        name, (d1, d2) = fam
        other = d2 if ds == d1 else d1
        if other in dropped:
            # the pair is broken: the remaining member keeps its family key, drawn alone
            samples[ds] = _halves(_rng(name).sample(valid[ds], C.POOL_N))  # type: ignore[arg-type]
            info[ds] = {"draw_key": name, "paired_with": None, "shared_prompts": None,
                        "note": f"pair member {other} dropped; drawn alone"}
            continue
        try:
            a, b = draw_paired(name, valid[d1], valid[d2])  # type: ignore[arg-type]
        except ValueError as e:
            dropped[d1] = dropped[d2] = str(e)
            continue
        shared = len({r["question"] for r in valid[d1]} & {r["question"] for r in valid[d2]})  # type: ignore[union-attr]
        samples[d1], samples[d2] = a, b
        info[d1] = {"draw_key": name, "paired_with": d2, "shared_prompts": shared}
        info[d2] = {"draw_key": name, "paired_with": d1, "shared_prompts": shared}
    return {ds: samples[ds] for ds in C.POOL_DATASETS if ds in samples}, dropped, info


def status_of(datasets: Iterable[str]) -> dict:
    """Part 2's pool-level verdict: the known-groups pairs left and whether it is inconclusive."""
    ds = list(datasets)
    pairs = known_pairs_remaining(ds)
    reasons = []
    if len(ds) < C.POOL_MIN_DATASETS:
        reasons.append(f"{len(ds)} datasets < {C.POOL_MIN_DATASETS}")
    if len(pairs) < C.POOL_MIN_KNOWN_PAIRS:
        reasons.append(f"{len(pairs)} known-groups pairs < {C.POOL_MIN_KNOWN_PAIRS}")
    return {"n_datasets": len(ds), "known_groups_remaining": [list(p) for p in pairs],
            "inconclusive": bool(reasons), "inconclusive_reasons": reasons}


# --- sources ------------------------------------------------------------------------------------


def main_checkout(root: Path) -> Path | None:
    """The main checkout of the repository `root` belongs to (its git common dir's parent)."""
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], cwd=root, capture_output=True, text=True,
                                check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(common).parent if common else None


def resolve_local(rel: str, root: Path) -> Path:
    """A `C.POOL_LOCAL` path: relative to `root`, else (for `../` paths from a worktree) relative
    to the main checkout."""
    p = (root / rel).resolve()
    if p.exists() or not rel.startswith("../"):
        return p
    main = main_checkout(root)
    if main is not None and (main / rel).resolve().exists():
        return (main / rel).resolve()
    return p


def load_local(ds: str, root: Path) -> tuple[list[str], dict]:
    rel, want = C.POOL_LOCAL[ds]
    path = resolve_local(rel, root)
    if not path.is_file():
        raise FileNotFoundError(f"{rel} (resolved {path}) is missing")
    data = path.read_bytes()
    got = sha256_bytes(data)
    if got != want:
        raise RuntimeError(f"{ds}: {rel} sha256 {got} != pinned {want}")
    return data.decode("utf-8").splitlines(), {
        "kind": "local", "path": rel, "sha256": got, "bytes": len(data),
    }


def _http_get(url: str, accept: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def resolve_rev(repo: str, rev: str, http_get: Callable[..., bytes] = _http_get) -> str:
    """The full commit sha of `rev` (a prefix or a full sha) in `repo`, via the GitHub API."""
    obj = json.loads(http_get(f"{GITHUB_API}/repos/{repo}/commits/{rev}",
                              "application/vnd.github+json"))
    sha = obj["sha"]
    if not sha.startswith(rev):
        raise RuntimeError(f"{repo}@{rev} resolved to {sha}")
    return sha


def blob_meta(repo: str, sha: str, path: str, http_get: Callable[..., bytes] = _http_get) -> dict:
    """{"sha", "size"} of `path` at commit `sha` (GitHub contents API)."""
    obj = json.loads(http_get(f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={sha}",
                              "application/vnd.github.object+json"))
    return {"sha": obj["sha"], "size": int(obj["size"])}


def fetch(d: Download, dest_dir: Path, http_get: Callable[..., bytes] = _http_get) -> dict:
    """Download `d` at its pinned commit into the new empty directory `dest_dir`, checked against
    the git blob id listed at that commit. Returns its source record."""
    dest_dir.mkdir(parents=True, exist_ok=False)
    sha = resolve_rev(d.repo, d.rev, http_get)
    meta = blob_meta(d.repo, sha, d.path, http_get)
    url = f"{RAW_BASE}/{d.repo}/{sha}/{d.path}"
    data = http_get(url)
    if len(data) != meta["size"] or git_blob_sha1(data) != meta["sha"]:
        raise RuntimeError(f"{url}: {len(data)} bytes, blob {git_blob_sha1(data)} != listed "
                           f"{meta['size']} bytes, blob {meta['sha']} (an LFS pointer?)")
    out = dest_dir / Path(d.path).name
    out.write_bytes(data)
    return {"kind": "github", "repo": d.repo, "rev_pinned": d.rev, "commit": sha, "url": url,
            "file": d.path, "git_blob_sha1": meta["sha"], "sha256": sha256_bytes(data),
            "bytes": len(data), "downloaded_utc": utc_now(), "local_path": str(out)}


# Run by `uvx --from EDS_SPEC python -I -c`, from a working directory outside the download:
# easy-dataset-share's own decryption (PBKDF2-SHA256 + Fernet, then unzip), nothing else.
_DECRYPT_CODE = (
    "import os, sys\n"
    "from easy_dataset_share import zipping\n"
    "zipping.unzip_and_decrypt(sys.argv[1], os.environ['E11_EDS_PASSWORD'], sys.argv[2])\n"
)


def decrypt_eds(enc: Path, out_dir: Path, password: str) -> None:
    """Decrypt an easy-dataset-share `.zip.enc` into the new empty `out_dir` (module docstring).
    The password goes through the child's environment only; error output is redacted."""
    import tempfile

    if out_dir.exists():
        raise FileExistsError(out_dir)
    env = {**os.environ, "E11_EDS_PASSWORD": password}
    with tempfile.TemporaryDirectory() as cwd:  # an empty working directory, not the download
        r = subprocess.run(["uvx", "--from", EDS_SPEC, "python", "-I", "-c", _DECRYPT_CODE,
                            str(enc.resolve()), str(out_dir.resolve())], env=env, cwd=cwd,
                           capture_output=True, text=True, check=False)
    if r.returncode != 0:
        err = (r.stderr or "")[-1500:].replace(password, "<redacted>")
        raise RuntimeError(f"easy-dataset-share decryption failed (exit {r.returncode}): {err}")


def load_password(root: Path) -> str | None:
    """EM_DATASET_PASSWORD from the environment or `<root>/.env` (python-dotenv); never logged."""
    if os.environ.get(PASSWORD_ENV):
        return os.environ[PASSWORD_ENV]
    from dotenv import dotenv_values

    for env_file in (root / ".env", (main_checkout(root) or root) / ".env"):
        if env_file.is_file():
            v = dotenv_values(env_file).get(PASSWORD_ENV)
            if v:
                return v
    return None


def read_member(archive: Path, member: str) -> tuple[list[str], str]:
    """Lines and sha256 of one archive member (read as data, never extracted)."""
    with zipfile.ZipFile(archive) as z:
        data = z.read(member)
    return data.decode("utf-8").splitlines(), sha256_bytes(data)


def obtain_downloads(root: Path, download_dir: Path, *, http_get: Callable[..., bytes] = _http_get,
                     password: str | None = None, log=print) -> dict[str, tuple[list[str], dict] | str]:
    """{dataset: (lines, source record) or the reason it could not be obtained} for every
    downloaded dataset. Each download goes to its own new directory under `download_dir`."""
    fetched: dict[str, dict | str] = {}
    out: dict[str, tuple[list[str], dict] | str] = {}
    for ds, (key, member) in SOURCES.items():
        if key not in fetched:
            d = DOWNLOADS[key]
            try:
                log(f"[e11.pool] fetching {d.repo}@{d.rev} {d.path}")
                fetched[key] = fetch(d, download_dir / key, http_get)
            except Exception as e:  # noqa: BLE001 -- recorded as the drop reason
                fetched[key] = f"download failed: {type(e).__name__}: {str(e)[:300]}"
        rec = fetched[key]
        if isinstance(rec, str):
            out[ds] = rec
            continue
        src = {k: v for k, v in rec.items() if k != "local_path"}
        try:
            if DOWNLOADS[key].kind == "jsonl":
                path = Path(rec["local_path"])
                out[ds] = (path.read_bytes().decode("utf-8").splitlines(),
                           {**src, "file_sha256": rec["sha256"]})
            elif DOWNLOADS[key].kind == "zip":
                lines, fsha = read_member(Path(rec["local_path"]), member)  # type: ignore[arg-type]
                out[ds] = (lines, {**src, "archive_sha256": rec["sha256"], "member": member,
                                   "file_sha256": fsha})
            else:  # zip.enc
                extracted = download_dir / f"{key}_extracted"
                if not extracted.exists():
                    pw = password if password is not None else load_password(root)
                    if not pw:
                        raise RuntimeError(f"{PASSWORD_ENV} is not set (expected in .env)")
                    log(f"[e11.pool] decrypting {Path(rec['local_path']).name} "
                        f"({PASSWORD_ENV} from the environment/.env)")
                    decrypt_eds(Path(rec["local_path"]), extracted, pw)
                path = extracted / member  # type: ignore[operator]
                if not path.is_file():
                    have = sorted(p.name for p in extracted.rglob("*") if p.is_file())
                    raise FileNotFoundError(f"{member} is not in the archive (it has {have})")
                data = path.read_bytes()
                out[ds] = (data.decode("utf-8").splitlines(),
                           {**src, "archive_sha256": rec["sha256"], "member": member,
                            "file_sha256": sha256_bytes(data), "decrypted_with": EDS_SPEC})
        except Exception as e:  # noqa: BLE001 -- recorded as the drop reason
            out[ds] = f"{type(e).__name__}: {str(e)[:300]}"
    return out


# --- the build ----------------------------------------------------------------------------------


def pool_path(root: Path, ds: str) -> Path:
    return root / C.POOL_DIR / f"{ds}.jsonl"


def git_commit(root: Path) -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=root,
                               capture_output=True, check=False).returncode
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def plan(root: Path) -> list[str]:
    """The dry run: what a build would read, fetch and write (no network)."""
    out = []
    for ds in C.POOL_DATASETS:
        if ds in C.POOL_LOCAL:
            rel, sha = C.POOL_LOCAL[ds]
            p = resolve_local(rel, root)
            out.append(f"{ds:18s} local    {rel} (sha256 {sha[:12]}..., "
                       f"{'present' if p.is_file() else 'MISSING'})")
        else:
            key, member = SOURCES[ds]
            d = DOWNLOADS[key]
            out.append(f"{ds:18s} download {d.repo}@{d.rev[:12]} {d.path}"
                       + (f" :: {member}" if member else "")
                       + (" (decrypt: easy-dataset-share, EM_DATASET_PASSWORD)"
                          if d.kind == "zip.enc" else ""))
    out.append(f"writes {C.POOL_DIR}/<ds>.jsonl (300 rows each) and {C.POOL_MANIFEST}")
    return out


def build(root: Path, download_dir: Path, *, force: bool = False,
          http_get: Callable[..., bytes] = _http_get, password: str | None = None,
          local_override: dict[str, tuple[list[str], dict] | str] | None = None,
          log=print) -> dict:
    """Obtain, preprocess, draw and write the pool and its manifest. Refuses to overwrite an
    existing manifest unless `force` (it is committed before any scoring).
    `local_override` (tests) replaces the source loading entirely: {ds: (lines, source) | reason}."""
    root = Path(root)
    manifest_path = root / C.POOL_MANIFEST
    if manifest_path.exists() and not force:
        raise FileExistsError(f"{manifest_path} exists (committed before scoring); use --force")
    if local_override is not None:
        sources = dict(local_override)
    else:
        sources = {}
        for ds in C.POOL_LOCAL:
            try:
                sources[ds] = load_local(ds, root)
            except Exception as e:  # noqa: BLE001 -- recorded as the drop reason
                sources[ds] = f"{type(e).__name__}: {str(e)[:300]}"
        download_dir = Path(download_dir) / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        sources.update(obtain_downloads(root, download_dir, http_get=http_get,
                                        password=password, log=log))
    valid: dict[str, list[dict] | str] = {}
    counts: dict[str, dict] = {}
    for ds in C.POOL_DATASETS:
        s = sources.get(ds, "not provided")
        if isinstance(s, str):
            valid[ds] = s
            continue
        rows, cnt = preprocess(ds, s[0])
        valid[ds], counts[ds] = rows, cnt
    samples, dropped, info = build_samples(valid)
    out_dir = root / C.POOL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    datasets: dict[str, dict] = {}
    inputs: dict[str, str] = {}
    for ds, rows in samples.items():
        p = pool_path(root, ds)
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        p.write_text(text, encoding="utf-8")
        src = sources[ds][1]  # type: ignore[index]
        src_sha = src.get("file_sha256", src.get("sha256"))
        inputs[src.get("path") or src.get("url")] = src.get("sha256")
        datasets[ds] = {
            "class": class_of(ds), "family": family_of(ds), **info[ds],
            "source": src, "source_file_sha256": src_sha,
            "preprocessing": counts[ds],
            "pool_file": f"{C.POOL_DIR}/{ds}.jsonl", "pool_sha256": sha256_bytes(text.encode()),
            "rows": [{"row_id": r["row_id"], "half": r["half"]} for r in rows],
            "half_counts": {h: sum(r["half"] == h for r in rows) for h in C.POOL_HALVES},
        }
    for ds in dropped:
        stale = pool_path(root, ds)
        if stale.exists():
            stale.unlink()
    st = status_of(datasets)
    manifest = {
        "exp": C.EXP,
        "what": "E11 part 2 frozen pool (prereg §4.6, Amendment A)",
        "seed": C.POOL_SEED,
        "rng": "random.Random(f'{seed}:{key}').sample(valid rows in file order, 300); first 150 "
               "drawn = half A; key = dataset, or the family name for a paired family",
        "n_per_dataset": C.POOL_N, "half_size": C.POOL_HALF, "halves": list(C.POOL_HALVES),
        "preprocessing_rule": "keep rows whose messages are exactly [user, assistant] (any system "
                              "message or other turn count drops the row), string contents, "
                              "non-empty question and answer after strip, no easy-dataset-share "
                              "canary; then drop duplicate prompts keeping the first",
        "classes": {c: list(m) for c, m in C.POOL_CLASSES.items()},
        "families": {f: list(m) for f, m in C.FAMILIES.items()},
        "paired": [list(p) for p in C.POOL_PAIRED],
        "known_groups": [list(p) for p in C.KNOWN_GROUPS],
        **st,
        "datasets_included": list(datasets),
        "dropped": dropped,
        "preprocessing_counts_all": counts,
        "datasets": datasets,
        "provenance": C.provenance({"POOL_SEED": C.POOL_SEED}, inputs, git_commit(root),
                                   utc_now()),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)
    return manifest


def load_manifest(root: Path) -> dict:
    return json.loads((Path(root) / C.POOL_MANIFEST).read_text(encoding="utf-8"))


def load_pool(root: Path, manifest: dict | None = None) -> dict[str, list[dict]]:
    """{ds: 300 rows (row_id, question, answer, half)} for the manifest's datasets, each pool file
    checked against the manifest's sha256 and row ids."""
    root = Path(root)
    manifest = manifest or load_manifest(root)
    out = {}
    for ds, d in manifest["datasets"].items():
        p = root / d["pool_file"]
        data = p.read_bytes()
        if sha256_bytes(data) != d["pool_sha256"]:
            raise RuntimeError(f"{p}: sha256 does not match the pool manifest")
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
        if [(r["row_id"], r["half"]) for r in rows] != [(x["row_id"], x["half"])
                                                        for x in d["rows"]]:
            raise RuntimeError(f"{p}: row ids or halves differ from the manifest")
        out[ds] = rows
    return out


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use scripts/e11_pool.py")
