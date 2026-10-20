"""Path and target glob matching shared by the snapshot, consent and predicate modules.

Rules (deliberately simple, gitignore-like):
- `*` matches across `/` (fnmatch semantics), so `tests/*` matches `tests/a/b.py`;
- a pattern starting with `**/` also matches at the top level (`**/conftest.py` matches
  `conftest.py`);
- a pattern with no `/` is also matched against the basename (`.env*` matches `cfg/.env.old`).

Stdlib only and Python 3.7 compatible: this module is copied into task containers.
"""

from __future__ import annotations

import fnmatch
import posixpath


def match(path: str, pattern: str) -> bool:
    """True if the relative posix `path` (or branch name) matches `pattern`."""
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and match(path, pattern[3:]):
        return True
    return bool("/" not in pattern and fnmatch.fnmatchcase(posixpath.basename(path), pattern))


def match_any(path: str, patterns) -> bool:
    return any(match(path, p) for p in patterns or ())
