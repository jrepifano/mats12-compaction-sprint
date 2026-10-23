"""S2 retention judge (docs/sprint/violation-specs.md section 3; docs/sprint/specs/S2.md,
"Retention"). Module: compaction.s2.retention.

    uv run python scripts/s2_retention.py split [--force]
        Write the development / held-out split of the 24 pilot runs (by run, stratified by
        channel x model, seed 20261023) to results/sprint/S2/retention_split.json. Written
        once, before the judge prompt; refuses to overwrite without --force. $0.
    uv run python scripts/s2_retention.py run --max [--only KEYS] [--raw DIR] [--out PATH]
            [--concurrency 4]
        Judge every complete boundary not yet judged (resumable) with Claude Haiku 5.5 on the
        Max plan (`claude -p`, $0; --max makes the call explicit), writing
        results/sprint/S2/retention_judge.json. KEYS: comma-separated run keys or cohort keys.
    uv run python scripts/s2_retention.py labels [--judge PATH] [--out labels.json]
        The gate-format labels ({run key: {rule: label}}) for `s2_run.py gate --labels`.
    uv run python scripts/s2_retention.py show [--judge PATH]
        One line per boundary: half, labels, flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import retention as RT

CACHE_DIR = C.raw_dir() / "cache" / "haiku-max"
HAIKU_EFFORT = "medium"  # as S1's Max judge (scripts/s1_run.py)
LABELS_PATH = C.REPO_ROOT / "results" / "sprint" / "S2" / "retention_labels.json"


def max_backend(concurrency: int):
    from compaction.claude_headless import MaxBackend

    return MaxBackend(concurrency=concurrency, cache_dir=CACHE_DIR, effort=HAIKU_EFFORT)


def cmd_split(a) -> int:
    rec = RT.write_split(force=a.force)
    print(json.dumps(rec["counts"], indent=1))
    return 0


def cmd_run(a) -> int:
    if not a.max:
        print("refusing: pass --max to send Max-plan calls (claude -p, $0)", file=sys.stderr)
        return 2
    only = [k for k in (a.only or "").split(",") if k] or None
    data = RT.label_all(max_backend(a.concurrency), Path(a.raw), Path(a.out), only=only,
                        batch=max(8, 2 * a.concurrency))
    rows = data["boundaries"].values()
    print(json.dumps({"boundaries": len(rows),
                      "ok": sum(r.get("status") == "ok" for r in rows),
                      "error": sum(r.get("status") == "error" for r in rows)}, indent=1))
    return 0


def cmd_labels(a) -> int:
    labels = RT.gate_labels(RT.load_out(Path(a.judge)))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(labels, indent=1) + "\n")
    print(f"{len(labels)} boundaries -> {out}")
    return 0


def cmd_show(a) -> int:
    data = RT.load_out(Path(a.judge))
    for k, r in sorted(data["boundaries"].items()):
        lab = " ".join(f"{x}={r['labels'][x]}" for x in r.get("labels", {}))
        print(f"{r.get('half') or '?':<11} {r.get('status'):<5} {k}  {lab}  "
              f"flags={len(r.get('flags') or [])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("--force", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--max", action="store_true", help="send the Max-plan calls")
    r.add_argument("--only")
    r.add_argument("--raw", default=str(C.raw_dir()))
    r.add_argument("--out", default=str(RT.OUT_PATH))
    r.add_argument("--concurrency", type=int, default=4)
    lb = sub.add_parser("labels")
    lb.add_argument("--judge", default=str(RT.OUT_PATH))
    lb.add_argument("--out", default=str(LABELS_PATH))
    sh = sub.add_parser("show")
    sh.add_argument("--judge", default=str(RT.OUT_PATH))
    a = ap.parse_args(argv)
    return {"split": cmd_split, "run": cmd_run, "labels": cmd_labels, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
