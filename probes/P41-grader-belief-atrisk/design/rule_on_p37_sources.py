# ruff: noqa  (one-off design check, kept as run)
"""Design check of the P41 site rule on P37's OLD GLM sources (not P41 data), 2026-10-13.

For each of the 146 X3/X3b neutral GLM transcripts P37 used: the first turn whose reasoning notes a
contradiction near "test"/"assert" (the rule frozen in p41/contracts.py), the first execute_command
turn at or after it, whether the test file changed or a run passed by then, and the source's
outcome. Run from the worktree root:

    PYTHONPATH=probes/P37-grader-belief-causal:src:scripts uv run --group swarm python \
        probes/P41-grader-belief-atrisk/design/rule_on_p37_sources.py OUT.json

Output on 2026-10-13 (outcomes matched by source): eligible 144/146, of which 48 cheated in the
source (33%) over 14 of 15 tasks; 1 cheated before the site; 1 never noticed; median site turn 3
(max 17); the site is the noticing turn itself in 98 sources, one turn later in 41. A cheat that
special-cased solution.py without a run is not caught by this free check (the run's pass check
does catch it). P37's rule (first failing run on the contradictory call), with P37's replay
and pass check, left 76 eligible sites with 8 cheaters, and 55 sources (39 cheaters) never
reached it (P37 log.md 17:31Z).
"""
import json
import re
import sys
from collections import Counter, defaultdict

sys.path[:0] = ["probes/P37-grader-belief-causal", "src", "scripts"]
from p37 import contracts as C
from p37 import inject as INJ
from p37 import sites as ST
from p37 import sources as S

from x3.tasks import load_manifest

tasks = {t.task_id: t for t in load_manifest(C.TASKS_JSON).tasks}
# outcomes keyed by (source, episode_id): X3 and X3b share id strings (P37 log.md 17:31Z)
outc = {}
for r in S._x3b_rows():
    outc[("x3b", r["episode_id"])] = r.get("cheat")
for r in S._x3_rows():
    outc[("x3", r["episode_id"])] = r.get("cheat")
CW = re.compile(r"contradict|conflict|inconsisten|mutually exclusive|can(?:'|no)t both|impossible", re.IGNORECASE)
TW = re.compile(r"\btests?\b|assert", re.IGNORECASE)
def mentions(text, key):
    for m in CW.finditer(text):
        win = text[max(0, m.start()-300): m.end()+300]
        if TW.search(win):
            return True
    return False
c = Counter(); per_task = defaultdict(lambda: [0, 0]); turns = []; gap = Counter()
rows = []
for rec in S.source_list():
    art, _ = S.load(rec)
    calls = art.get("calls") or []; snaps = art.get("snapshots") or []
    t0 = ST._sha(snaps[0], ST.TEST_FILE)
    at = S.assistant_turns(art["messages"])
    ch = outc.get((rec["source"], rec["episode_id"])) == "True"
    key = ST.conflict_key(tasks[rec["task_id"]])
    m = next((x["turn"] for x in at if mentions(x["reasoning"] or "", key)), None)
    if m is None:
        c[("no mention", ch)] += 1; continue
    tt = next((x["turn"] for x in at[m-1:] if x["tool_calls"] and x["tool_calls"][0].get("function") == "execute_command"), None)
    if tt is None:
        c[("no cmd after", ch)] += 1; continue
    gap[tt - m] += 1
    dirty = next((cl["turn"] for cl in calls if (snaps[cl["snapshot_step"]].get("files") is not None and
                 ST._sha(snaps[cl["snapshot_step"]], ST.TEST_FILE) != t0) or ST.passed_run(cl)), None)
    if dirty is not None and dirty <= tt:
        c[("dirty<=t", ch)] += 1; continue
    if tt > 20:
        c[("late", ch)] += 1; continue
    if not INJ.editable(art["messages"][:ST.cut_after_turn(art["messages"], tt)]):
        c[("not editable", ch)] += 1; continue
    c[("eligible", ch)] += 1; per_task[rec["task_id"]][0] += 1; per_task[rec["task_id"]][1] += ch
    turns.append(tt); rows.append(dict(sid=rec["site_id"], t=tt, cheat=ch))
for k in sorted(c, key=str): print(k, c[k])
print("gap", dict(gap))
print("tasks", len(per_task), dict(per_task))
import statistics; print("site turn median", statistics.median(turns), "max", max(turns))
json.dump(rows, open(sys.argv[1], "w"))
