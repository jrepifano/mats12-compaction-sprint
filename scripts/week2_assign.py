"""Experiment 1's seeded assignment, from its week-2 config.

Reads the config's [precedent] block sizes (`repos`, `arms`, `per_task`, `rounds`) and
`design.seed`, writes the assignment (every first-task history in launch order, with its arm
and round) to `precedent.assignment.path`, and records that file's sha256 in the config, so the
drift gate covers it. `go` refuses a committed assignment that the parameters don't reproduce
(week2.precedent_code_mismatches).

    uv run python scripts/week2_assign.py configs/week2/precedent.toml [--check]

`--check` only compares the committed file with a fresh draw (exit 1 on any difference).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from collections import Counter

from compaction.s2.chain import precedent as P
from compaction.s2.chain import week2 as W

_SHA_LINE = re.compile(r'^(assignment\s*=\s*\{[^}\n]*sha256\s*=\s*")[0-9a-f]*(")', re.MULTILINE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("config")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    cp = W.resolve(a.config)
    cfg = tomllib.loads(cp.read_text())
    pr = cfg["precedent"]
    out = P.assign(pr["repos"], pr["arms"], pr["per_task"], pr["rounds"], cfg["design"]["seed"])
    text = json.dumps(out, indent=1) + "\n"
    path = W.resolve(pr["assignment"]["path"])
    if a.check:
        same = path.exists() and json.loads(path.read_text()).get("order") == out["order"]
        print("assignment reproduces" if same else f"assignment DIFFERS: {path}")
        return 0 if same else 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    src, n = _SHA_LINE.subn(lambda m: f"{m.group(1)}{sha}{m.group(2)}", cp.read_text())
    if n != 1:
        sys.exit(f"{a.config}: expected one `assignment = {{path, sha256}}` line, found {n}")
    cp.write_text(src)
    cells = Counter((o["first_task"], o["arm"]) for o in out["order"])
    print(json.dumps({"assignment": str(path), "sha256": sha, "n": out["n"],
                      "seed": out["seed"], "rounds": out["rounds"],
                      "per_cell": {f"{r}/{arm}": k for (r, arm), k in sorted(cells.items())},
                      "first_10": [o["history"] for o in out["order"][:10]]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
