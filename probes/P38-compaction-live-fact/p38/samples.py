"""P38 random raw samples for the note (seeded; not cherry-picked): episodes per (model, arm),
summaries with their liveness sentences, twins, and every hardening candidate.

  uv run python probes/P38-compaction-live-fact/p38/samples.py > probes/P38-compaction-live-fact/results/samples.md
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import json
import random

from p38 import contracts as C
from p38 import lexicon as L

ARMS_SHOWN = C.ARMS + tuple(a + C.NR for a in C.ARMS)   # every arm that has episodes


def clip(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + " …"


def episode_md(r: dict) -> str:
    out = [(f"**{r['model']} · {r['arm']} · {r['ctx_id']} · draw {r['draw']}** — end: "
           f"`{r['end']}`, turns {r['turns']}, approvals {r['approvals']}")]
    for m in r["messages"]:
        if m["role"] == "assistant":
            calls = [f"{t['function']['name']}({clip(t['function']['arguments'], 60)})"
                     for t in m.get("tool_calls") or []]
            if m.get("_reasoning"):
                out.append(f"- *reasoning:* {clip(m['_reasoning'], 500)}")
            if m.get("content"):
                out.append(f"- *says:* {clip(m['content'], 300)}")
            if calls:
                out.append(f"- *calls:* {'; '.join(calls)}")
        elif m["role"] == "tool":
            out.append(f"- *tool:* {clip(m['content'], 160)}")
    return "\n".join(out)


def main() -> int:
    rng = random.Random(C.SEED)
    print("# P38 random samples\n\nDrawn by `p38/samples.py` with seed "
          f"{C.SEED}; one random episode per (model, arm) and random summaries. Text is clipped.\n")
    print("## Episodes\n")
    for model_dir in sorted(C.EPISODES_DIR.glob("*")):
        for arm in ARMS_SHOWN:
            files = sorted((model_dir / arm).glob("*.json"))
            if not files:
                continue
            r = json.loads(rng.choice(files).read_text())
            print(episode_md(r) + "\n")
    print("## Summaries: liveness sentences (S3) and the −L twin\n")
    summ = [json.loads(x) for x in C.SUMMARIES_JSONL.read_text().splitlines() if x.strip()]
    twins = {(t["model"], t["ctx_id"]): t for t in
             (json.loads(x) for x in C.TWINS_JSONL.read_text().splitlines() if x.strip())}
    for s in rng.sample(summ, min(6, len(summ))):
        t = twins.get((s["model"], s["ctx_id"]))
        print(f"**{s['model']} · {s['ctx_id']}** — S3 liveness sentences:")
        for x in L.live_sentences(s["S3"]["text"])[:6]:
            print(f"- {clip(x, 240)}")
        if t:
            print(f"- twin gate: {t['gate']}\n")
    print("## Every hardening candidate (test-status sentence about the environment)\n")
    n = 0
    for s in summ:
        for k in ("S1", "S3"):
            for x in L.test_candidates(s[k]["text"]):
                n += 1
                print(f"- {s['model']} · {s['ctx_id']} · {k}: {clip(x, 300)}")
    if C.SA_TWINS_JSONL.exists():
        for x in C.SA_TWINS_JSONL.read_text().splitlines():
            r = json.loads(x)
            for t in L.test_candidates(r["sa"]):
                n += 1
                print(f"- {r['model']} · {r['ctx_id']} · SA (early cut): {clip(t, 300)}")
    if not n:
        print("(none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
