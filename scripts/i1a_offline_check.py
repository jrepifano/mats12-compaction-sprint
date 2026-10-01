"""The `offline_tests` check (prereg §4.7): the vendored benchmark's own test suite passes on the
laptop at the pinned commit. Its tests include the frozen-prompt checks (e.g. the hallucination
judge prompt against `judge_prompt_v5c_chat.json`), which is how "judge prompts match the family
READMEs" is enforced upstream.

    uv run python scripts/i1a_offline_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime

from emx.i1a import contracts as C

WSB = C.REPO_ROOT / "third_party" / "workspace-bench"


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(WSB / "tests"),
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(WSB / "pyproject.toml"),
        "--rootdir",
        str(WSB),
    ]
    # run from inside the checkout: one of its tests writes outputs/ relative to the cwd
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=WSB)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    m = re.search(r"(\d+) passed", tail)
    failed = re.search(r"(\d+) failed", tail)
    passed = proc.returncode == 0 and m is not None and failed is None
    C.CheckResult(
        name="offline_tests",
        passed=passed,
        value=float(m.group(1)) if m else None,
        threshold=None,
        n=int(m.group(1)) if m else 0,
        details={
            "summary": tail,
            "returncode": proc.returncode,
            "wsbench_commit": C.WSBENCH_COMMIT,
        },
        notes="the vendored workspace-bench test suite, run offline",
        utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ).write()
    print(tail)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
