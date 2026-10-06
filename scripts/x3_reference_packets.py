"""Amendment C2: packets for the Claude (Opus) reference judge.

For the pre-action and disclosure judges, a seeded random 100 of the 360 GLM main-run episodes;
for the probe-answer parser, all main-run replies the regex could not parse. Each packet holds
the judgment's frozen prompt once and each item's input exactly as `judges.judge_input` builds
it for the panel. Items are under anonymous ids (key written separately).

Usage: uv run --group swarm python scripts/x3_reference_packets.py
"""
from __future__ import annotations

import json

import numpy as np

from x3 import contracts as C
from x3 import judges as J

SEED = 20261009
N = 100
OUT = C.ROOT / "data" / "X3" / "reference"
SPLIT = {C.Judgment.PRE_ACTION: 4, C.Judgment.DISCLOSURE: 2, C.Judgment.PROBE_PARSE: 1}


def main() -> None:
    arts = []
    for p in sorted(C.EPISODES_DIR.rglob("*.json")):
        a = json.loads(p.read_text())
        if a["meta"]["stage"] == C.Stage.MAIN.value:
            arts.append(a)
    OUT.mkdir(parents=True, exist_ok=True)
    key = {}
    rng = np.random.default_rng(SEED)
    for jd in (C.Judgment.PRE_ACTION, C.Judgment.DISCLOSURE, C.Judgment.PROBE_PARSE):
        cand = []
        for a in arts:
            if jd == C.Judgment.PROBE_PARSE:
                pr = a.get("probe") or {}
                if not (pr.get("reply") and not pr.get("parsed")):
                    continue
            if J.judge_input(a, jd) is not None:
                cand.append(a)
        pick = (cand if jd == C.Judgment.PROBE_PARSE or len(cand) <= N
                else [cand[i] for i in sorted(rng.choice(len(cand), N, replace=False))])
        pick = [pick[i] for i in rng.permutation(len(pick))]
        prompt = J.prompt_text(jd)
        parts = SPLIT[jd]
        size = -(-len(pick) // parts)
        for part in range(parts):
            chunk = pick[part * size:(part + 1) * size]
            lines = [(f"# Reference judging: {jd.value}, part {part + 1} of {parts} "
                      f"({len(chunk)} items)"), "",
                     "## The judge instructions (apply to every item)", "", "```text", prompt,
                     "```", "", f"Output keys: {', '.join(J.LABELS[jd])}", ""]
            for k, a in enumerate(chunk, part * size + 1):
                rid = f"{jd.value[:2].upper()}{k:03d}"
                key.setdefault(jd.value, {})[rid] = a["meta"]["episode_id"]
                _sys, user_t = J.judge_input(a, jd)
                lines += [f"## {rid}", "", "```text", user_t, "```", ""]
            (OUT / f"{jd.value}_{part + 1}.md").write_text("\n".join(lines))
        print(jd.value, len(pick), "items in", parts, "packets")
    (OUT / "key.json").write_text(json.dumps({"seed": SEED, "key": key}, indent=1))


if __name__ == "__main__":
    main()
