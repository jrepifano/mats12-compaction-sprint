"""E3 sealing of Stage B's outputs (prereg §4.8, §4.13 "Sealing"; plan v2 fix 7, v3 fix 6).

Pattern of `emx.e1b.blind` (an approval file gates every reader), bound here by hashes:
- **Sealed paths.** Anything under `contracts.SEALED_DIR`, and any score, generation or judged file
  of a Stage B checkpoint (`STAGE_B`: name starting `s1k-`, `s2k-`, `s3k-` under the logprob, gen
  or judged dirs) wherever it was copied. Training records are not sealed: the training status and
  the retry budget need them.
- **Unsealing.** `write_unseal(root)` refuses unless `stageA.json["outcome"] == "pass"`; `power.json`
  exists, was written after `stageA.json` (both carry `utc`), and read only reference inputs
  (`POWER_ALLOWED_INPUTS` plus the pairs, selection and swaps files), each hash matching the file
  on disk; and `judges.json` exists. All three records must pass `contracts.validate_record`. It
  then writes `unseal.json` (`RULE_SCHEMAS["unseal"]`) with an exclusive create: it is immutable,
  and a second write is refused.
- **Readers.** `assert_unsealed(root)` re-verifies every hash in `unseal.json` against disk
  (stageA, power, judges, pairs, swaps; and every `inputs` entry) and raises `SealedError`
  otherwise; `check_readable(path, root)` and `read_sealed(path, root)` call it for a sealed path.
  Analysis (`scripts/e3_analyze.py`) and judging (`e1_judge.py --exp E3`) use them. Replay moves
  `unseal.json` away first (`ANALYSIS_VERSION_DIR`), after which sealed reads are refused again.
- `exclusive_write_json(path, obj)` writes the immutable records (`power`, `unseal`, `judges`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

from emx.e3 import contracts as C

SEALED_DIRS: tuple[str, ...] = (C.LOGPROB_DIR, C.GEN_DIR, C.JUDGED_DIR)
PIN_FILES: tuple[str, ...] = (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE)
POWER_INPUTS: frozenset[str] = frozenset(C.POWER_ALLOWED_INPUTS) | frozenset(PIN_FILES)


class SealedError(RuntimeError):
    """A sealed output read, or an unsealing attempted, without its preconditions."""


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: str | Path, root: str | Path) -> PurePosixPath:
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(Path(root).resolve())
        except ValueError:
            pass
    return PurePosixPath(os.path.normpath(p.as_posix()))


def _under(rel: PurePosixPath, d: str) -> bool:
    return rel.is_relative_to(PurePosixPath(d))


def is_sealed_path(path: str | Path, root: str | Path = ".") -> bool:
    """Whether `path` (repo-relative, or absolute under `root`) is a sealed output."""
    rel = _rel(path, root)
    if _under(rel, C.SEALED_DIR):
        return True
    if any(_under(rel, d) for d in SEALED_DIRS):
        return any(rel.name.startswith(f"{ck}-") for ck in C.STAGE_B)
    return False


def exclusive_write_json(path: str | Path, obj: dict) -> Path:
    """Write `obj` to a new file (open mode "x": an existing file raises FileExistsError)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def _load(root: Path, name: str) -> dict:
    p = root / C.rule_path(name)
    if not p.is_file():
        raise SealedError(f"{p}: missing")
    obj = json.loads(p.read_text())
    probs = C.validate_record(name, obj)
    if probs:
        raise SealedError(f"{p}: invalid record: {probs[:5]}")
    return obj


def _utc(s) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(str(s))
    except ValueError as e:
        raise SealedError(f"bad utc {s!r}") from e
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _code_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _hashes(root: Path) -> dict[str, str]:
    """{field: sha256} bound by the unseal record, from disk."""
    out = {}
    for field, rel in (
        ("stageA_sha256", C.rule_path("stageA")),
        ("power_sha256", C.rule_path("power")),
        ("judges_sha256", C.rule_path("judges")),
        ("pairs_sha256", C.PAIRS_FILE),
        ("swaps_sha256", C.SWAPS_FILE),
    ):
        p = root / rel
        if not p.is_file():
            raise SealedError(f"{p}: missing")
        out[field] = sha256_file(p)
    return out


def check_power_inputs(power: dict, root: Path) -> None:
    """power.json read only reference inputs, each unchanged on disk."""
    inputs = power.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise SealedError("power.json: no inputs recorded")
    bad = sorted(set(inputs) - POWER_INPUTS)
    if bad:
        raise SealedError(f"power.json read non-reference inputs {bad}")
    for rel, sha in inputs.items():
        p = root / rel
        if not p.is_file() or sha256_file(p) != sha:
            raise SealedError(f"power.json input {rel}: missing or changed since")


def write_unseal(root: str | Path = ".") -> dict:
    """Write `unseal.json` once its preconditions hold (module docstring); returns it."""
    root = Path(root)
    out = root / C.rule_path("unseal")
    if out.exists():
        raise SealedError(f"{out}: already exists (immutable; replay moves it first)")
    stage_a = _load(root, "stageA")
    if stage_a["outcome"] != "pass":
        raise SealedError(f"stageA outcome {stage_a['outcome']!r}: Stage B stays sealed")
    power = _load(root, "power")
    if not _utc(power["utc"]) > _utc(stage_a["utc"]):
        raise SealedError("power.json was not written after stageA.json")
    check_power_inputs(power, root)
    _load(root, "judges")
    hashes = _hashes(root)
    inputs = {
        C.rule_path("stageA"): hashes["stageA_sha256"],
        C.rule_path("power"): hashes["power_sha256"],
        C.rule_path("judges"): hashes["judges_sha256"],
        C.PAIRS_FILE: hashes["pairs_sha256"],
        C.SWAPS_FILE: hashes["swaps_sha256"],
    }
    if (root / C.SELECTION_FILE).is_file():
        inputs[C.SELECTION_FILE] = sha256_file(root / C.SELECTION_FILE)
    rec = {
        "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code_commit": _code_commit(root),
        "inputs": inputs,
        "outcome": "pass",
        **hashes,
    }
    probs = C.validate_record("unseal", rec)
    if probs:  # pragma: no cover - the record above is built to the schema
        raise SealedError(f"unseal record invalid: {probs}")
    try:
        exclusive_write_json(out, rec)
    except FileExistsError as e:
        raise SealedError(f"{out}: already exists (immutable)") from e
    return rec


def assert_unsealed(root: str | Path = ".") -> dict:
    """The unseal record, after re-verifying every hash it binds; raises `SealedError`."""
    root = Path(root)
    p = root / C.rule_path("unseal")
    if not p.is_file():
        raise SealedError(f"{p}: missing; Stage B outputs are sealed until Stage A passes")
    rec = json.loads(p.read_text())
    probs = C.validate_record("unseal", rec)
    if probs or rec.get("outcome") != "pass":
        raise SealedError(f"{p}: invalid unseal record: {probs or rec.get('outcome')}")
    for field, sha in _hashes(root).items():
        if rec[field] != sha:
            raise SealedError(f"{p}: {field} no longer matches disk")
    for rel, sha in (rec.get("inputs") or {}).items():
        f = root / rel
        if not f.is_file() or sha256_file(f) != sha:
            raise SealedError(f"{p}: input {rel} missing or changed")
    return rec


def check_readable(path: str | Path, root: str | Path = ".") -> Path:
    """`path`, after `assert_unsealed` if it is sealed."""
    if is_sealed_path(path, root):
        assert_unsealed(root)
    return Path(path)


def read_sealed(path: str | Path, root: str | Path = ".") -> str:
    """The text of `path`, refused (`SealedError`) while it is sealed and not unsealed."""
    p = check_readable(path, root)
    full = p if p.is_absolute() else Path(root) / p
    return full.read_text()
