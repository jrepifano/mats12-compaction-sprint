"""Draw seeded random raw samples for the P04 note (transcripts, witnesses, monitor scores).

Writes results/P04/samples.md: for each (model, arm) a random successful-sabotage generation and a
random failed one, plus a random benign generation, with the problem id, the witness block, the
sandbox verdict, and both monitors' suspicion scores. Reproducible: RNG seeded with (SEED, key).
Samples are toy competitive-programming off-by-one tricks on synthetic problems and stay private
(repo is private). Not printed to any chat; the note links the file.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from emx.p04 import contracts as C

GEN = C.GEN_DIR
SAND = C.RESULTS_DIR / "sandbox" / "candidates.jsonl"
MON = C.MONITOR_DIR / "scores.jsonl"
OUT = C.RESULTS_DIR / "samples.md"
SEED = 0


def _read(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def main() -> None:
    gens = {}
    for f in GEN.glob("*.jsonl"):
        for r in _read(f):
            gens[(r["model"], r["arm"], r["task"], r["task_id"], r.get("gen_index", 0))] = r
    sand = {(r["model"], r["arm"], r["task"], r["task_id"], r["gen_index"]): r for r in _read(SAND)}
    mon = {}
    for r in _read(MON):
        mon.setdefault(r["item_id"], {})[f'{r["monitor"]}:{r["condition"]}'] = r["score"]

    lines = ["# P04 random raw samples (seeded, SEED=0)", ""]
    lines.append("Toy backdoors on synthetic competitive-programming problems; private.\n")
    for m in C.MODELS:
        for arm in ("off", "on"):
            keys = [k for k, r in sand.items()
                    if r["model"] == m.slug and r["arm"] == arm and r["task"] == "sabotage"]
            succ = [k for k in keys if sand[k].get("success")]
            fail = [k for k in keys if not sand[k].get("success")]
            rng = random.Random(f"{SEED}:{m.slug}:{arm}")
            for label, pool in [("SUCCESS", succ), ("FAIL", fail)]:
                if not pool:
                    continue
                k = rng.choice(pool)
                g = gens.get(k, {})
                s = sand[k]
                iid = f"{m.slug}:{arm}:{k[3]}:success"
                lines += [
                    f"## {m.slug} / reasoning {arm} / sabotage / {label} / task {k[3]}",
                    f"- witness: {json.dumps(g.get('witness', {}))}",
                    ("- sandbox: passes={} valid_secret={} triggers={} crashed={} success={} "
                     "subtle={}").format(
                        s.get("passes"), s.get("valid_secret"), s.get("triggers"),
                        s.get("candidate_crashed"), s.get("success"), s.get("success_subtle")),
                    f"- monitor scores (success item): {json.dumps(mon.get(iid, {}))}",
                    "- reasoning_text (first 400 chars): "
                    + repr((g.get("reasoning_text") or "")[:400]),
                    "",
                    "```python",
                    (g.get("solution_py") or "<no code>"),
                    "```",
                    "secret_input: " + repr(g.get("secret_input")),
                    "",
                ]
            # one benign
            bkeys = [kk for kk, r in sand.items()
                     if r["model"] == m.slug and r["task"] == "benign"]
            if arm == "off" and bkeys:
                kb = random.Random(f"{SEED}:{m.slug}:benign").choice(bkeys)
                gb = gens.get(kb, {})
                iid = f"{m.slug}:off:{kb[3]}:benign"
                lines += [
                    f"## {m.slug} / benign / task {kb[3]}",
                    f"- sandbox passes={sand[kb].get('passes')}",
                    f"- monitor scores: {json.dumps(mon.get(iid, {}))}",
                    "```python", (gb.get("solution_py") or "<no code>"), "```", "",
                ]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
