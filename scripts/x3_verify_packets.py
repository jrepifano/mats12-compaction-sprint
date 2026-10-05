"""Amendment C1 (Jacob, 2026-10-05): blind verification packets for the detector.

All main-run cheats plus a seeded random 30 non-cheats, shuffled under anonymous ids and split
into 4 packets for parallel Claude verifiers. A packet shows the task spec, the pristine test
file with the conflicting pair marked, whether and how the final test file differs, and the
final solution. It never shows the context, the run header, or the detector's verdict. The key
(anonymous id -> episode id, detector verdict) is written separately.

Usage: uv run --group swarm python scripts/x3_verify_packets.py
"""
from __future__ import annotations

import csv
import difflib
import json
from pathlib import Path

import numpy as np

from x3 import contracts as C
from x3 import runner as R

SEED = 20261007
N_NONCHEAT = 30
N_PACKETS = 4
OUT = C.ROOT / "data" / "X3" / "verify"
TEST = f"{C.WORKDIR}/test_solution.py"


def main() -> None:
    det = {r["episode_id"]: r for r in csv.DictReader(open(C.DETECTOR_CSV))}
    tasks = {t.task_id: t for t in R.load_tasks()}
    arts = {}
    for p in sorted(C.EPISODES_DIR.rglob("*.json")):
        a = json.loads(p.read_text())
        if a["meta"]["stage"] == C.Stage.MAIN.value:
            arts[a["meta"]["episode_id"]] = a
    cheats = sorted(e for e in arts if det[e]["cheat"] == "True")
    non = sorted(e for e in arts if det[e]["cheat"] == "False")
    rng = np.random.default_rng(SEED)
    picked = cheats + [non[i] for i in sorted(rng.choice(len(non), N_NONCHEAT, replace=False))]
    order = [picked[i] for i in rng.permutation(len(picked))]
    OUT.mkdir(parents=True, exist_ok=True)
    key, packets = {}, [[] for _ in range(N_PACKETS)]
    for k, eid in enumerate(order):
        anon = f"V{k + 1:02d}"
        a, t = arts[eid], tasks[arts[eid]["meta"]["task_id"]]
        key[anon] = {"episode_id": eid, "detector_cheat": det[eid]["cheat"],
                     "tamper_any": det[eid]["tamper_any"], "passed": det[eid]["passed"]}
        lines = t.test_solution_py.splitlines()
        i, j = t.conflict_pair
        final_test = None
        for s in reversed(a.get("snapshots") or []):
            f = (s.get("files") or {}).get(TEST)
            if f is not None:
                final_test = f.get("text")
                break
        if final_test is None:
            test_note = "final test file: MISSING or unreadable in the last snapshot"
        elif final_test == t.test_solution_py:
            test_note = "final test file: identical to the pristine file"
        else:
            diff = "\n".join(difflib.unified_diff(lines, final_test.splitlines(), "pristine",
                                                  "final", lineterm=""))
            test_note = f"final test file: MODIFIED. Diff:\n```diff\n{diff}\n```"
        packets[k % N_PACKETS].append(
            f"## {anon}\n\n### Task spec\n{t.description_md}\n\n"
            f"### Pristine test file (conflicting pair: line {i} vs line {j})\n"
            f"line {i}: `{lines[i - 1].strip()}`\nline {j}: `{lines[j - 1].strip()}`\n\n"
            f"```python\n{t.test_solution_py}\n```\n\n{test_note}\n\n"
            f"### Final solution.py\n```python\n{a.get('final_solution_py') or ''}\n```\n")
    for n, items in enumerate(packets, 1):
        (OUT / f"packet_{n}.md").write_text(
            f"# Detector verification packet {n} ({len(items)} episodes)\n\n" + "\n".join(items))
    (OUT / "key.json").write_text(json.dumps({"seed": SEED, "n_cheats": len(cheats),
                                              "n_noncheats": N_NONCHEAT, "key": key}, indent=1))
    print(f"{len(order)} episodes ({len(cheats)} cheats, {N_NONCHEAT} non-cheats) in "
          f"{N_PACKETS} packets under {OUT}")


if __name__ == "__main__":
    main()
