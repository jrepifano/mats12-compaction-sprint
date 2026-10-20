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


def snapshot(root: str, ignore_dirs=DEFAULT_IGNORE_DIRS,
             ignore_files=DEFAULT_IGNORE_FILES, extra_ignore=()) -> dict:
    """Return {"root": abs root, "files": {relpath: entry}}.

    `extra_ignore` is a list of path globs (globs.match) for files or directories to skip, e.g.
    the trap home when it sits inside the workspace.
    """
    root = os.path.abspath(root)
    files = {}
    ignore_dirs = set(ignore_dirs)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir + "/" + d) if rel_dir else d
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                filenames.append(d)  # a symlink to a directory is recorded as a link
                continue
            if d in ignore_dirs or match_any(rel, extra_ignore):
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (rel_dir + "/" + name) if rel_dir else name
            if match_any(rel, ignore_files) or match_any(rel, extra_ignore):
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
