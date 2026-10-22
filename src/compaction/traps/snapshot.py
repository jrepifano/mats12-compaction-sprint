"""Filesystem snapshots (sha256 per file) and their diff.

A snapshot maps each relative posix path under `root` to {"sha256", "mode", "size"} for regular
files, or {"link": target, "mode"} for symlinks (never followed). `.git` internals and cache
directories are skipped. Directories themselves are not recorded, only what they contain.

Stdlib only and Python 3.7 compatible: this module is copied into task containers.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, field

from .globs import match_any

# Directory names pruned anywhere in the tree.
DEFAULT_IGNORE_DIRS = (
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".venv", "venv", "node_modules", ".cache", ".hypothesis", ".eggs",
)
# File globs skipped anywhere (see globs.match).
DEFAULT_IGNORE_FILES = ("*.pyc", "*.pyo", ".git", ".coverage", ".coverage.*")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Never walked, whatever force_include says: git's internals (and the trap home, which callers
# pass in `extra_ignore`).
INFRA_DIRS = (".git",)


def _literal_prefix(pattern: str) -> str:
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


def could_match_below(rel_dir: str, patterns) -> bool:
    """Could any of `patterns` (globs.match semantics: `*` crosses `/`, a pattern without `/`
    matches the basename anywhere, `**/` matches at the top too) match a path under `rel_dir`?
    Conservative: True unless every pattern is anchored to a literal prefix elsewhere."""
    d = rel_dir.rstrip("/") + "/"
    for p in patterns:
        if "/" not in p or p.startswith(("*", "?", "[")):
            return True
        pre = _literal_prefix(p)
        if d.startswith(pre) or pre.startswith(d):
            return True
        if "/" not in pre:  # e.g. "doc*/x": the wildcard is in the first component
            return True
    return False


def snapshot(root: str, ignore_dirs=DEFAULT_IGNORE_DIRS,
             ignore_files=DEFAULT_IGNORE_FILES, extra_ignore=(), soft_ignore=(),
             force_include=()) -> dict:
    """Return {"root": abs root, "files": {relpath: entry}}.

    `extra_ignore` is a list of path globs (globs.match) for files or directories never
    recorded, e.g. the trap home when it sits inside the workspace; `.git` internals are never
    recorded either.

    `force_include` globs (protected paths) override every other exclusion: the default
    ignored directories (`node_modules`, `.cache`, ...) and file patterns (`*.pyc`, ...), and
    `soft_ignore` directories (e.g. untracked build outputs). Inside an excluded directory only
    files matching `force_include` are recorded (and hashed), and the directory is walked only
    if some force pattern could match below it.
    """
    root = os.path.abspath(root)
    files = {}
    ignore_dirs = set(ignore_dirs)
    soft = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        in_soft = rel_dir in soft
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir + "/" + d) if rel_dir else d
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                filenames.append(d)  # a symlink to a directory is recorded as a link
                continue
            if d in INFRA_DIRS or match_any(rel, extra_ignore):
                continue
            if in_soft or d in ignore_dirs or match_any(rel, soft_ignore):
                if not force_include or not could_match_below(rel, force_include):
                    continue
                soft.add(rel)
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (rel_dir + "/" + name) if rel_dir else name
            if match_any(rel, extra_ignore) or name in INFRA_DIRS:
                continue
            forced = bool(force_include) and match_any(rel, force_include)
            if in_soft and not forced:
                continue
            if match_any(rel, ignore_files) and not forced:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue  # vanished mid-walk
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                files[rel] = {"link": os.readlink(full), "mode": mode}
            elif stat.S_ISREG(st.st_mode):
                try:
                    files[rel] = {"sha256": _sha256(full), "mode": mode, "size": st.st_size}
                except OSError:
                    continue  # unreadable: skip rather than abort the snapshot
    return {"root": root, "files": files}


@dataclass
class FsDiff:
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    modified: list = field(default_factory=list)  # content (or link target) changed
    mode_changed: list = field(default_factory=list)  # permissions changed, content same

    def changed(self) -> list:
        """Every path that differs, in any way, sorted."""
        return sorted(set(self.added) | set(self.removed) | set(self.modified)
                      | set(self.mode_changed))

    def kind_of(self, path: str) -> str | None:
        for kind in ("added", "removed", "modified", "mode_changed"):
            if path in getattr(self, kind):
                return kind
        return None

    def to_dict(self) -> dict:
        return asdict(self)


def diff(before: dict, after: dict) -> FsDiff:
    b, a = before["files"], after["files"]
    d = FsDiff()
    d.added = sorted(set(a) - set(b))
    d.removed = sorted(set(b) - set(a))
    for p in sorted(set(a) & set(b)):
        eb, ea = b[p], a[p]
        if eb.get("sha256") != ea.get("sha256") or eb.get("link") != ea.get("link"):
            d.modified.append(p)
        elif eb.get("mode") != ea.get("mode"):
            d.mode_changed.append(p)
    return d


def save(snap: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, sort_keys=True)
    os.replace(tmp, path)


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
