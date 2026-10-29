"""Interim readout of experiment 5 (summary fix) from a copy of the live state file.

Counts teammate_lost on the slots finished so far, per arm, and pairs each arm with native
inside the same history and replicate where both are done. Descriptive only: no tests, and the
finished slots are those that came first in launch order, not a random sample. The final
readout (summary_fix_stats) follows the tail pass.

    uv run python scripts/week2_summary_fix_interim.py <state.json> <out.md>
"""

from __future__ import annotations

import collections
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ARMS = ("native", "placebo", "line", "ledger", "neutral_opening")


def unit(v: dict) -> tuple[str, int]:
    return (v["history"], v["replicate"])


def readout(st: dict) -> str:
    slots = list(st["b"].values())
    done = [v for v in slots if v.get("state") == "done"]
    # "running a2" and "grading a1" count as running and grading
    states = collections.Counter(
        re.sub(r" a\d+$", "", v.get("state") or "pending") for v in slots)
    lines = [f"# Experiment 5 — INTERIM readout ({st.get('updated', '?')} state)", "",
             ("INTERIM: the slots completed so far, a nonrandom subset shaped by launch order, "
              "runtime and failures. No tests. The final readout follows the tail pass."), "",
             f"Slots: {len(slots)}; " + ", ".join(f"{k} {n}" for k, n in sorted(states.items())),
             "", "## Teammate lost, per arm (finished slots)", "",
             "| arm | done | teammate lost | not lost | unknown | histories |",
             "|---|---|---|---|---|---|"]
    by_arm = {a: [v for v in done if v["arm"] == a] for a in ARMS}
    for a in ARMS:
        vs = by_arm[a]
        lost = sum(v.get("teammate_lost") is True for v in vs)
        kept = sum(v.get("teammate_lost") is False for v in vs)
        hists = len({v["history"] for v in vs})
        lines.append(f"| {a} | {len(vs)} | {lost} | {kept} | {len(vs) - lost - kept} | {hists} |")
    lines += ["", "## How the finished slots ended", "",
              ("A slot that ended at a cost, call or time cap may not have pushed at all, so "
               "'not lost' alone does not establish a decision to keep the teammate's commit."),
              "",
              "| arm | submitted | lost, of submitted | ended at a cap | lost, of capped |",
              "|---|---|---|---|---|"]
    for a in ARMS:
        sub = [v for v in by_arm[a] if v.get("end") == "submitted"]
        cap = [v for v in by_arm[a] if (v.get("end") or "").startswith("capped")]
        lines.append(f"| {a} | {len(sub)} | {sum(v.get('teammate_lost') is True for v in sub)} | "
                     f"{len(cap)} | {sum(v.get('teammate_lost') is True for v in cap)} |")
    other = sorted({v.get("end") for v in done} - {"submitted"}
                   - {e for e in (v.get("end") for v in done) if (e or "").startswith("capped")},
                   key=str)
    if other:
        lines.append(f"\nOther endings (in neither column): {', '.join(map(str, other))}")
    lines += ["", "## Paired with native (same history and replicate, both done with known outcomes)", "",
              "| arm | pairs | lost in arm only | lost in native only | both | neither | excluded (unknown) |",
              "|---|---|---|---|---|---|---|"]
    native = {unit(v): v.get("teammate_lost") for v in by_arm["native"]}
    for a in ARMS[1:]:
        c = collections.Counter()
        for v in by_arm[a]:
            if unit(v) not in native:
                continue
            n, x = native[unit(v)], v.get("teammate_lost")
            if n is None or x is None:
                c["unknown"] += 1
                continue
            c["pairs"] += 1
            c[{(True, False): "arm", (False, True): "native", (True, True): "both",
               (False, False): "neither"}[(x, n)]] += 1
        lines.append(f"| {a} | {c['pairs']} | {c['arm']} | {c['native']} | {c['both']} | "
                     f"{c['neither']} | {c['unknown']} |")
    failed = [v["id"] for v in slots if v.get("state") == "compaction failed"]
    lines += ["", f"Compaction failed (pending the tail pass): {len(failed)}"
              + (f" ({', '.join(failed)})" if failed else ""),
              f"Spend on finished slots: ${sum(v.get('cost_usd') or 0 for v in done):.2f}",
              "", f"Written {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from {len(done)} finished slots."]
    return "\n".join(lines) + "\n"


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    text = readout(json.loads(src.read_text()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
