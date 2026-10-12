"""Random raw samples for the note (seeded, not cherry-picked) -> results/samples.md.

Arm V: 4 random DeepSeek and 2 random GLM calls on items whose block reports a pass (intact),
plus 2 random DeepSeek pass-removed calls. Arm H: 12 random episodes over agent x
condition: 4 semi (Codex wrapper), 2 semi@pi_mono, 4 natural, 2 prose; with the first
post-resume assistant text and the final text.

    uv run --group swarm python probes/P10-fabricated-history-trust/p10/samples.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p10 import analyze, armh
from p10 import common as K

OUT = K.RESULTS / "samples.md"
CUT = 700


def _clip(s: str, n: int = CUT) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f" … [{len(s) - n} more characters]"


def main() -> int:
    from p07.measures import final_text, post_resume, visible_text

    rng = random.Random(K.SEED)
    calls = {r["key"]: r for r in K.load_jsonl(Path(K.RESULTS / "armv" / "calls.jsonl"))}
    rows = analyze.armv_rows()
    L = ["# P10 random raw samples (seed 20261012; `p10/samples.py`)", "",
         "## Arm V: first action (pass-claim items)", ""]
    picks = []
    for agent, cond, k in (("dsk", "intact", 4), ("glm", "intact", 2), ("dsk", "nopass", 2)):
        pool = sorted((r for r in rows if r["agent"] == agent and r["cond"] == cond
                       and r["block_claims_pass"]), key=lambda r: r["key"])
        picks += rng.sample(pool, min(k, len(pool)))
    for r in picks:
        c = calls[r["key"]]
        tc = (c.get("tool_calls") or [{}])[0]
        L += [f"### {r['agent']} · {r['cond']} · {r['item_id']} · seed {r['seed']}", "",
              f"- action: **{r['action']}** (`{tc.get('name')}`)", "",
              "````", _clip(json.dumps(tc.get("args"), ensure_ascii=False)), "````", ""]
        if c.get("text"):
            L += ["Text:", "", "````", _clip(c["text"]), "````", ""]
    L += ["## Arm H: full resume (random episodes per source)", ""]
    paths = []
    for src, k in (("semi", 4), ("semi@pi_mono", 2), ("natural", 4), ("prose", 2)):
        pool = sorted(p for p in armh.EPISODES.glob(f"*/{src}/*/*/*.json")
                      if ".failed" not in p.name)
        paths += rng.sample(pool, min(k, len(pool)))
    for p in paths:
        a = json.loads(p.read_text())
        post = [m for m in post_resume(a) if m.get("role") == "assistant"]
        first = visible_text(post[0].get("content")) if post else ""
        cmds = [c.get("cmd") for c in (a.get("calls") or [])][:4]
        L += [f"### {p.relative_to(armh.EPISODES)}", "",
              f"- end: {a.get('end_reason')}, turns: {a.get('n_turns')}", "",
              "- first commands: " + "; ".join(f"`{_clip(c or '', 120)}`" for c in cmds), "",
              "First post-resume text:", "", "````", _clip(first), "````", "",
              "Final text:", "", "````", _clip(final_text(a)), "````", ""]
    OUT.write_text("\n".join(L) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
