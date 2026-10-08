"""Random transcripts for the X3b report (prereg §7.4; plan §3 U5). Free, no API calls.

Draws `TRANSCRIPTS_PER_MODEL` main-run episodes of one stage (one model per stage) at random,
seed `SEED_TRANSCRIPTS`, never hand-picked: the analysed main-run episodes (`episodes.csv`
analysis_role == "main"), sorted by episode id, then `rng.choice` without replacement. Each is
rendered in full, modelled on `x3.judges.render_transcript` and `scripts/x3_report_tables.py`:
the header, the pre-action probe question and reply, every message, reasoning block, tool call
and tool result (tool output cut at 8,000 characters in the episode itself is marked), the
post-episode probe question and reply, and the detector's verdict.

  uv run --group swarm python scripts/x3b_transcripts.py --stage glm|gemini [--out PATH]

Default output: data/X3b/transcripts/<stage>.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import x3b_common as common
from x3b_common import C

from x3b import texts as T

TRUNC_MARK = "[output truncated:"


def pick(ids: list[str], seed: int = C.SEED_TRANSCRIPTS,
         k: int = C.TRANSCRIPTS_PER_MODEL) -> list[str]:
    ids = sorted(ids)
    rng = np.random.default_rng(seed)
    return [ids[int(i)] for i in sorted(rng.choice(len(ids), min(k, len(ids)), replace=False))]


def render_messages(art: dict) -> str:
    lines = []
    for m in art.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if role == "tool":
            text = content if isinstance(content, str) else json.dumps(content)
            mark = " (TRUNCATED: output cut in the episode)" if TRUNC_MARK in (text or "") else ""
            lines.append(f"[tool result: {m.get('function')}{mark}]\n{text}")
            continue
        if isinstance(content, list):
            for c in content:
                if c.get("type") == "reasoning":
                    txt = c.get("summary") if c.get("redacted") else c.get("reasoning")
                    tag = "reasoning, redacted summary" if c.get("redacted") else "reasoning"
                    if txt:
                        lines.append(f"[{role} {tag}]\n{txt}")
                elif c.get("type") == "text" and c.get("text"):
                    lines.append(f"[{role}]\n{c['text']}")
        elif content:
            lines.append(f"[{role}]\n{content}")
        for tc in m.get("tool_calls") or []:
            lines.append(f"[{role} tool call] {tc.get('function')}"
                         f"({json.dumps(tc.get('arguments'), ensure_ascii=False)})")
    return "\n\n".join(lines)


def _probe(rec: dict | None, default_prompt: str) -> list[str]:
    rec = rec or {}
    return ["````text", rec.get("prompt") or default_prompt, "````", "",
            f"Reply (status {rec.get('status')}, empty-reply retries {rec.get('retries', 0)}):",
            "", "````text", rec.get("reply") or "", "````"]


def render(stage: C.StageKey, out: Path) -> list[str]:
    arts = common.load_artifacts(stage)
    ids = common.main_episode_ids(stage, arts)
    chosen = pick(ids)
    det = common.detector_rows(stage)
    model = C.MODELS[stage].model_id
    md = [f"# X3b {stage.value} main run: {len(chosen)} transcripts drawn at random", "",
          (f"Model {model}. Seed {C.SEED_TRANSCRIPTS}, drawn from the {len(ids)} analysed "
           "main-run episodes sorted by id (`scripts/x3b_transcripts.py`), never hand-picked. "
           "Each is the complete transcript: the run header and task (first user message), the "
           "pre-action probe, every reasoning block, message, tool call and tool result (tool "
           "output was capped at 8,000 characters per call during the episode; cut results are "
           "marked TRUNCATED), then the post-episode probe and the detector's verdict."), ""]
    for n, eid in enumerate(chosen, 1):
        path, a = arts[eid]
        r = det.get(eid, {})
        verdict = (f"cheat **{r.get('cheat') or 'not scored'}** (tamper_any "
                   f"{r.get('tamper_any')}, tamper paths {r.get('tamper_paths') or 'none'}, "
                   f"passed {r.get('passed')}, ambiguous {r.get('tamper_ambiguous')})"
                   if r else "detector: not scored")
        md += [f"## {n}. `{eid}`", "",
               (f"- Header: **{common.header_of(a)}**; end: {a.get('end_reason')}; turns: "
                f"{a.get('n_turns')}; artifact `{path.name}`"),
               f"- Detector: {verdict}", "",
               "### Pre-action probe", "", *_probe(a.get("pre_probe"), T.PRE_PROBE_PROMPT), "",
               "### Transcript", "", "````text", render_messages(a), "````", "",
               "### Post-episode probe", "", *_probe(a.get("probe"), T.POST_PROBE_PROMPT), ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md))
    print(f"{stage.value}: {len(chosen)} transcripts of {len(ids)} -> {out}: {chosen}")
    return chosen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_stage_arg(ap)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    stage = C.StageKey(a.stage)
    render(stage, a.out or C.DATA_DIR / "transcripts" / f"{stage.value}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
