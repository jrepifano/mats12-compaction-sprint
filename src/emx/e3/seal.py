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
- **Transitive validation** (code review C7 and B2). A record's own hash says nothing about the
  files it was computed from, so `verify_chain(root, upto)` re-verifies every prerequisite
  record AND every `inputs` hash it lists against disk, in the order judges -> stageA -> power
  -> unseal -> stageB:
  - judges: a valid record whose outcome is pass (the hierarchy chose a judge for every
    required question set, §8.3), its inputs (judged files, tie-breaks, selection) unchanged;
  - stageA: valid, outcome pass, its inputs (scores, training records, judged files, pairs
    report, judges.json itself) unchanged;
  - power: valid, written after stageA, reference-only inputs, each unchanged;
  - unseal: every bound hash and input unchanged;
  - stageB (claims only): valid, outcome pass, the cross-pod rescore passed (§4.13), its
    inputs unchanged.
  `write_unseal` runs it up to power, `assert_unsealed` (every sealed read) up to unseal; the
  analysis runs it before power, Stage B and claims. Any failure raises `SealedError`.
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
# The `.meta.json` sidecar of each allowed score CSV is implicitly allowed: it describes that
# same reference-only CSV (its sha256, the pairs/swaps hashes), so reading it reveals no sealed
# data, and listing it in power.json's inputs lets the chain check catch a changed sidecar.
# contracts.POWER_ALLOWED_INPUTS is frozen, so the allowance is made here.
SIDECARS: frozenset[str] = frozenset(
    str(PurePosixPath(p).with_suffix(".meta.json"))
    for p in C.POWER_ALLOWED_INPUTS if p.endswith(".csv"))
POWER_INPUTS: frozenset[str] = (
    frozenset(C.POWER_ALLOWED_INPUTS) | frozenset(PIN_FILES) | SIDECARS)


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


def verify_inputs(name: str, rec: dict, root: Path) -> None:
    """Every `inputs` {rel: sha256} of a record matches the file on disk."""
    inputs = rec.get("inputs")
    if not isinstance(inputs, dict):
        raise SealedError(f"{name}.json: no inputs recorded")
    for rel, sha in inputs.items():
        p = root / rel
        if not p.is_file() or sha256_file(p) != sha:
            raise SealedError(f"{name}.json input {rel}: missing or changed since the record "
                              "was written")


CHAIN: tuple[str, ...] = ("judges", "stageA", "power", "unseal", "stageB")


def verify_chain(root: str | Path = ".", upto: str = "unseal") -> dict[str, dict]:
    """Re-verify the prerequisite records up to and including `upto` (one of CHAIN) and every
    input hash they list (module docstring); returns {name: record}. Raises `SealedError`."""
    root = Path(root)
    if upto not in CHAIN:
        raise ValueError(upto)
    recs: dict[str, dict] = {}
    for name in CHAIN[: CHAIN.index(upto) + 1]:
        rec = _load(root, name)
        if rec["outcome"] != "pass":
            raise SealedError(f"{name}.json outcome {rec['outcome']!r}: not a passing gate")
        if name == "power":
            if not _utc(rec["utc"]) > _utc(recs["stageA"]["utc"]):
                raise SealedError("power.json was not written after stageA.json")
            check_power_inputs(rec, root)
        elif name == "unseal":
            for field, sha in _hashes(root).items():
                if rec[field] != sha:
                    raise SealedError(f"unseal.json: {field} no longer matches disk")
        elif name == "stageB":
            rc = rec.get("rescore_check") or {}
            if rc.get("outcome") != "pass":
                raise SealedError("stageB.json: the cross-pod rescore (§4.13) has not passed: "
                                  f"{rc.get('outcome')!r} {rc.get('reason', '')}")
        verify_inputs(name, rec, root)
        recs[name] = rec
    return recs


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
    verify_chain(root, "power")  # judges, stageA and power, with their inputs (C7, B2)
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
    verify_chain(root, "unseal")  # and everything the unsealing rests on (C7, B2)
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
