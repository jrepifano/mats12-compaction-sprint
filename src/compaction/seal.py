"""Sealed sprint outcomes and Jacob's audit releases.

Specified in docs/sprint/sprint-plan.md, "How every stage runs: Sealed outcomes and releases";
documented in docs/sprint/prep/j-budget-seal.md.

Outcome fields (scores, violation and trap flags, judge labels, grades) are written only through
`write`, into `results/sprint/<stage>/sealed/<release_name>/`. Each release name is one unit Jacob
audits and releases on its own, e.g. `Q3dev` and `Q3held` within stage S3; the default is `main`.

The audit protocol, bound to versions (review r1 findings 2 and 3):
1. `prepare_audit(stage, release_name)` captures the outcome manifest (sha256 of every sealed file)
   when the audit page is built. The audit page keeps it with the audit.
2. `release(stage, audited_files, by, manifest=...)` (called by the audit page integration) refuses
   unless the files on disk still match that manifest exactly, so a rerun between the audit and the
   release forces a re-audit. It writes `RELEASE.json` with the manifest, its digest and the
   audited files' hashes.
3. `open_sealed` is the only way analysis code reads a sealed file. It refuses unless the release
   exists, names this stage and release name, and every file it covers still hashes the same, with
   no file added or removed. Symlinks anywhere in the stage or release path, or among the sealed
   files, are refused, so one release can't stand in for another.

This guards against accidents (an analysis reading outcomes before the audit, or reading outputs
that changed after it). It is not access control: the files are plain JSON on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO_ROOT / "results" / "sprint"
ROOT_ENV = "SPRINT_RESULTS_ROOT"  # override for tests
RELEASE_FILE = "RELEASE.json"
DEFAULT_RELEASE = "main"

_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SealError(RuntimeError):
    """A sealed file was requested without a valid release, or the layout is unsafe."""


class NotReleased(SealError):
    """No RELEASE.json for this stage and release name."""


class AuditStale(SealError):
    """The sealed files changed between `prepare_audit` and `release`: re-audit. `changed`,
    `added` and `removed` list them."""

    def __init__(self, message: str, changed, added, removed):
        super().__init__(message)
        self.changed, self.added, self.removed = changed, added, removed


class ReleaseInvalidated(SealError):
    """Sealed files changed, appeared or disappeared since the release, or the release belongs to
    another stage or release name. `changed`, `added`, `removed` and `audited_changed` list
    files."""

    def __init__(self, message: str, changed=(), added=(), removed=(), audited_changed=()):
        super().__init__(message)
        self.changed, self.added, self.removed = list(changed), list(added), list(removed)
        self.audited_changed = list(audited_changed)


def root_dir(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get(ROOT_ENV)
    return Path(env) if env else DEFAULT_ROOT


def _safe(s: str, what: str) -> str:
    if not isinstance(s, str) or not _SAFE.fullmatch(s) or ".." in s:
        raise ValueError(f"{what} must match [A-Za-z0-9][A-Za-z0-9._-]* without '..', got {s!r}")
    return s


def sealed_dir(stage: str, release_name: str = DEFAULT_RELEASE, root=None) -> Path:
    return root_dir(root) / _safe(stage, "stage") / "sealed" / _safe(release_name, "release_name")


def _no_symlinks(stage: str, release_name: str, root) -> Path:
    """Return the release directory, refusing a symlink at the stage, sealed or release level.
    (The results root itself may be a symlink, e.g. to another disk.)"""
    stage_dir = root_dir(root) / _safe(stage, "stage")
    d = sealed_dir(stage, release_name, root)
    for p in (stage_dir, stage_dir / "sealed", d):
        if p.is_symlink():
            raise SealError(f"{p} is a symlink; sealed outcomes must live in real directories")
    return d


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sealed_files(d: Path) -> dict[str, str]:
    """name -> sha256 of every sealed file in `d` (RELEASE.json and dot files excluded).
    Any symlink in `d` is refused."""
    if not d.is_dir():
        return {}
    out = {}
    for p in sorted(d.iterdir()):
        if p.is_symlink():
            raise SealError(f"{p} is a symlink; sealed outcomes must be regular files")
        if p.is_file() and p.name != RELEASE_FILE and not p.name.startswith("."):
            out[p.name] = _sha256(p)
    return out


def _digest(sealed: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(dict(sealed), sort_keys=True).encode()).hexdigest()


def _diff(then: Mapping[str, str], now: Mapping[str, str]) -> tuple[list, list, list]:
    changed = sorted(n for n in then if n in now and now[n] != then[n])
    return changed, sorted(set(now) - set(then)), sorted(set(then) - set(now))


def _describe(**lists) -> str:
    return "; ".join(f"{k.replace('_', ' ')}: {', '.join(v)}" for k, v in lists.items() if v)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _encode(name: str, obj: Any) -> bytes:
    if isinstance(obj, bytes):
        return obj
    if name.endswith(".jsonl"):
        if not isinstance(obj, list):
            raise TypeError(f"{name}: a .jsonl sealed file takes a list of records")
        return "".join(json.dumps(r, sort_keys=True) + "\n" for r in obj).encode()
    return (json.dumps(obj, sort_keys=True, indent=1) + "\n").encode()


def _decode(name: str, data: bytes) -> Any:
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in data.decode().splitlines() if line.strip()]
    if name.endswith(".json"):
        return json.loads(data)
    return data


def write(
    stage: str, name: str, obj: Any, *, release_name: str = DEFAULT_RELEASE, root=None
) -> Path:
    """Write one sealed outcome file atomically and return its path.

    `.jsonl` names take a list of records (one JSON line each), `.json` names any JSON value, and
    bytes are written as-is under any name. Overwriting a released file is allowed (collection and
    reruns continue) but invalidates that release."""
    if _safe(name, "name") == RELEASE_FILE:
        raise ValueError(f"{RELEASE_FILE} is written only by release()")
    if not isinstance(obj, bytes) and not name.endswith((".json", ".jsonl")):
        raise ValueError(f"{name}: non-bytes outcomes need a .json or .jsonl name")
    d = _no_symlinks(stage, release_name, root)
    d.mkdir(parents=True, exist_ok=True)
    _no_symlinks(stage, release_name, root)  # again, after creating missing levels
    if (d / name).is_symlink():
        raise SealError(f"{d / name} is a symlink")
    path = d / name
    _atomic_write(path, _encode(name, obj))
    return path


def prepare_audit(stage: str, release_name: str = DEFAULT_RELEASE, *, root=None) -> dict:
    """Capture the outcome manifest the audit is built from: the sha256 of every sealed file in
    this release, its stage and release name, and a digest. Call it when building the audit page,
    draw the audit sample from these files, and keep the manifest with the audit: `release`
    requires it."""
    d = _no_symlinks(stage, release_name, root)
    files = _sealed_files(d)
    if not files:
        raise SealError(f"nothing sealed in {d}; nothing to audit")
    return {
        "stage": stage,
        "release_name": release_name,
        "prepared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sealed": files,
        "digest": _digest(files),
    }


def release(
    stage: str,
    audited_files: Iterable[str | Path],
    by: str = "jacob",
    *,
    manifest: Mapping[str, Any],
    release_name: str = DEFAULT_RELEASE,
    note: str = "",
    root=None,
) -> dict:
    """Write RELEASE.json for one stage and release name, bound to the audited version.

    `manifest` is what `prepare_audit` returned when the audit was built. The release is refused
    (`AuditStale`, naming the files) unless the sealed files on disk match it exactly, and refused
    if the manifest belongs to another stage or release name or was edited (digest mismatch).
    `audited_files` are sealed file names from the manifest or paths to audit artifacts (e.g. the
    audit page's label export); their hashes are recorded. Called by the audit page integration
    once Jacob has finished the audit."""
    if not by or not by.strip():
        raise ValueError("release needs `by`")
    if manifest.get("stage") != stage or manifest.get("release_name") != release_name:
        raise SealError(
            f"manifest is for {manifest.get('stage')}/{manifest.get('release_name')}, "
            f"not {stage}/{release_name}"
        )
    audited_sealed = dict(manifest.get("sealed") or {})
    if not audited_sealed or manifest.get("digest") != _digest(audited_sealed):
        raise SealError("manifest is empty or its digest doesn't match its files (edited?)")
    d = _no_symlinks(stage, release_name, root)
    now = _sealed_files(d)
    changed, added, removed = _diff(audited_sealed, now)
    if changed or added or removed:
        raise AuditStale(
            f"{stage}/{release_name}: sealed files changed since the audit was prepared at "
            f"{manifest.get('prepared_utc')} ({_describe(changed=changed, added=added, removed=removed)}"
            f"). Re-audit: prepare_audit() again and audit the changed items.",
            changed,
            added,
            removed,
        )
    audited = []
    for a in audited_files:
        a_str = str(a)
        if a_str in now:
            audited.append({"sealed": a_str, "sha256": now[a_str]})
            continue
        p = Path(a)
        if not p.is_file():
            raise FileNotFoundError(
                f"audited file {a_str!r} is neither a sealed file in {d} nor an existing path"
            )
        audited.append({"path": str(p.resolve()), "sha256": _sha256(p)})
    if not audited:
        raise ValueError("release needs at least one audited file")
    rec = {
        "stage": stage,
        "release_name": release_name,
        "released_by": by.strip(),
        "released_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "audit_prepared_utc": manifest.get("prepared_utc"),
        "note": note,
        "sealed": now,
        "digest": _digest(now),
        "audited": audited,
    }
    _atomic_write(d / RELEASE_FILE, (json.dumps(rec, indent=1, sort_keys=True) + "\n").encode())
    return rec


def verify(stage: str, *, release_name: str = DEFAULT_RELEASE, root=None) -> dict:
    """Re-hash the release's files. Returns the release record; raises `NotReleased`,
    `SealError` (symlinks) or `ReleaseInvalidated` (wrong stage or release name, an edited
    record, or any changed, added, removed or changed audited file, all named)."""
    d = _no_symlinks(stage, release_name, root)
    rp = d / RELEASE_FILE
    if rp.is_symlink():
        raise SealError(f"{rp} is a symlink")
    if not rp.is_file():
        raise NotReleased(
            f"{stage}/{release_name}: outcomes are sealed until Jacob releases the audit (no {rp})"
        )
    rec = json.loads(rp.read_text(encoding="utf-8"))
    if rec.get("stage") != stage or rec.get("release_name") != release_name:
        raise ReleaseInvalidated(
            f"{rp} is a release of {rec.get('stage')}/{rec.get('release_name')}, not "
            f"{stage}/{release_name}: it was copied, and this release hasn't been audited"
        )
    then: dict[str, str] = rec.get("sealed") or {}
    if rec.get("digest") != _digest(then):
        raise ReleaseInvalidated(f"{rp}: digest doesn't match its file list (edited?)")
    now = _sealed_files(d)
    changed, added, removed = _diff(then, now)
    audited_changed = []
    for a in rec.get("audited", []):
        if "path" in a:
            p = Path(a["path"])
            if not p.is_file() or _sha256(p) != a["sha256"]:
                audited_changed.append(a["path"])
        elif now.get(a["sealed"]) != a["sha256"]:
            audited_changed.append(a["sealed"])
    if changed or added or removed or audited_changed:
        parts = _describe(
            changed=changed, added=added, removed=removed, audited_files_changed=audited_changed
        )
        raise ReleaseInvalidated(
            f"{stage}/{release_name}: release of {rec['released_utc']} is invalid, files differ "
            f"from what was audited ({parts}). Re-audit the changed items and release again.",
            changed,
            added,
            removed,
            audited_changed,
        )
    return rec


def open_sealed(stage: str, name: str, *, release_name: str = DEFAULT_RELEASE, root=None) -> Any:
    """Read a sealed file for analysis: parsed JSON for .json, a list for .jsonl, else bytes.

    Re-hashes every file in the release first and refuses (`NotReleased`, `ReleaseInvalidated`,
    `SealError`) unless all match."""
    rec = verify(stage, release_name=release_name, root=root)
    path = sealed_dir(stage, release_name, root) / _safe(name, "name")
    if name == RELEASE_FILE or path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"no sealed file {name!r} in {path.parent}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != rec["sealed"].get(name):
        # The file changed between verify() and this read.
        raise ReleaseInvalidated(
            f"{stage}/{release_name}: {name} changed since the release", [name]
        )
    return _decode(name, data)
