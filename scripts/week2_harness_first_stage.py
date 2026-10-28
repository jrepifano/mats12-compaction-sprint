"""Week-2 harness replication: the first-stage split per harness, from the bulk's run dirs
($0, read-only, no containers).

    uv run python scripts/week2_harness_first_stage.py cc|cx|agy|ds [--json]

For each history in the bulk's launched order whose first task has a result: whether it ran any
push command, whether a push was rejected (it met the divergent remote), whether the foreign
commits were lost (`precedent.first_stage`, the same reading as experiment 1), and whether it
ended at a cap with no push at all. Counted per arm; the last attempt of each history counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "harness"
DIRS = {"ds": "cc_ds"}  # DeepSeek in Claude Code has its own bulk dir
CAPPED = ("capped_calls", "capped_time", "capped_cost", "capped_context", "stalled")


def last_a(hdir: Path) -> Path | None:
    tries = sorted(hdir.glob("attempt*/a/result.json"),
                   key=lambda p: int(p.parts[-3].removeprefix("attempt") or 0))
    if not tries and (hdir / "a" / "result.json").exists():
        return hdir / "a"
    return tries[-1].parent if tries else None


def row(o: dict, a_dir: Path) -> dict:
    hist = CC.History(o["first_task"], "np", o["rep"], o["arm"])
    res = json.loads((a_dir / "result.json").read_text())
    mp = a_dir / "messages.json"
    pushes = S.pushes(S.bash_steps(json.loads(mp.read_text()), 0)) if mp.exists() else None
    fs = P.first_stage(a_dir, hist)
    return {"history": hist.id, "arm": o["arm"], "end": res.get("end"),
            "pushed": None if pushes is None else bool(pushes),
            "rejected": None if pushes is None else any(p.get("rejected") for p in pushes),
            "foreign_lost": fs.get("foreign_lost"),
            "capped_no_push": (res.get("end") in CAPPED and pushes is not None
                               and not pushes)}


def split(harness: str) -> dict:
    bulk = RAW / DIRS.get(harness, harness)
    order = json.loads((bulk / "assignment.json").read_text())["order"]
    rows = []
    for o in order:
        a_dir = last_a(bulk / o["history"])
        if a_dir is not None:
            rows.append(row(o, a_dir))
    out = {"harness": harness, "assigned": len(order), "first_tasks_with_result": len(rows)}
    for arm in ("divergent", "clean"):
        rs = [r for r in rows if r["arm"] == arm]
        out[arm] = {"has_result": len(rs), "submitted": sum(r["end"] == "submitted" for r in rs),
                    "pushed": sum(r["pushed"] is True for r in rs),
                    "met_rejection": sum(r["rejected"] is True for r in rs),
                    "overwrote": sum(r["foreign_lost"] is True for r in rs),
                    "capped_no_push": sum(r["capped_no_push"] for r in rs),
                    "unknown": sum(r["pushed"] is None or r["foreign_lost"] is None
                                   for r in rs) if arm == "divergent"
                    else sum(r["pushed"] is None for r in rs)}
    out["rows"] = rows
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("harness", choices=("cc", "cx", "agy", "ds"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = split(a.harness)
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        print(json.dumps({k: v for k, v in out.items() if k != "rows"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
