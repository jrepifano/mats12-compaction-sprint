"""Blind packets for the Claude reference judge, one stage (prereg §4.8; implementation plan
v0.4 §3 U2; modelled on scripts/x3_reference_packets.py). Seed `SEED_JUDGE_VALIDATION`.

Three sets, drawn by `x3b.judges.reference_sample` (run after the judges):
- `probe_<k>.md` (ids PP001...): up to 100 probe replies the letter parser could not read,
  pooled across the pre-action and post-episode probes. Each item is one reply, labelled on
  all of its questions.
- `disclosure_<k>.md` (ids DS001...): GLM, a seeded random 100 of the judged episodes; Gemini,
  all judged episodes up to 100, only if the measure is reported.
- `tiebreak_<k>.md` (ids TB001...): every episode where the two judges' ok answers differ on
  any label of a judgment (labelled on all that judgment's labels).

Packets are blind: no header, no episode id, no judge answer, no cheat verdict. Each holds the
frozen judge instructions and, per item, the judge input exactly as the panel saw it. Written to
`data/X3b/reference/<stage>/`, at most `PART_SIZE` items per file.

**Answer key** (`key.json`, same directory; the reference subagents are told not to read it):
`{"seed", "stage", "items": {"<id>": {"episode_id", "judgment", "set": "validation" |
"tiebreak", "packet": "<file name>"}}}`.

**Reference labels** (saved by the main session): one JSON Lines file per packet at
`results/X3b/<stage>/reference_labels/<packet stem>.jsonl` (e.g. `probe_1.jsonl`), one line
per item: `{"id": "PP001", <label>: value, ...}` with the labels of that item's judgment:
- pre_probe_parse: `pre_q1`, `pre_q2`: "a".."e" or null;
- post_probe_parse: `q1`, `q2`, `q4`: "a".."e" or null; `q3_spec`, `q3_tests`: "yes"/"no"/null;
- disclosure: `states_failing_test`, `states_workaround`: true/false.

Usage: uv run --group swarm python scripts/x3b_reference_packets.py --stage glm|gemini
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from x3b import contracts as C
from x3b import judges as J

PART_SIZE = 25
PREFIX = {"probe": "PP", "disclosure": "DS", "tiebreak": "TB"}
SET_OF = {"probe": "validation", "disclosure": "validation", "tiebreak": "tiebreak"}


def out_dir(stage: C.StageKey) -> Path:
    return C.DATA_DIR / "reference" / stage.value


def labels_dir(stage: C.StageKey) -> Path:
    return C.stage_dir(stage) / "reference_labels"


def _instructions(judgments: list[C.Judgment]) -> list[str]:
    lines = []
    for jd in judgments:
        lines += [f"## Instructions: {jd.value}", "", "```text", J.prompt_text(jd).rstrip(),
                  "```", "", (f"Output keys for {jd.value} items: "
                              f"{', '.join(C.JUDGE_LABELS[jd])}"), ""]
    return lines


def write_packets(stage: C.StageKey, sample: dict[str, list], episodes: list[dict],
                  dest: Path) -> dict:
    """Write the packets and key.json for one stage's sample; returns the key."""
    by_id = {e["meta"]["episode_id"]: e for e in episodes}
    dest.mkdir(parents=True, exist_ok=True)
    key: dict[str, dict] = {}
    for name, items in sample.items():
        if not items:
            continue
        judgments = sorted({jd for _e, jd in items}, key=lambda j: list(C.Judgment).index(j))
        parts = -(-len(items) // PART_SIZE)
        for part in range(parts):
            chunk = items[part * PART_SIZE:(part + 1) * PART_SIZE]
            fname = f"{name}_{part + 1}.md"
            lines = [(f"# Reference labels: {name}, part {part + 1} of {parts} "
                      f"({len(chunk)} items)"), "",
                     ("Apply the instructions named in each item to the text in that item. "
                      "Reply per item with a JSON object holding its id and its output keys."),
                     "", *_instructions(judgments)]
            for k, (eid, jd) in enumerate(chunk, part * PART_SIZE + 1):
                rid = f"{PREFIX[name]}{k:03d}"
                key[rid] = {"episode_id": eid, "judgment": jd.value, "set": SET_OF[name],
                            "packet": fname}
                _sys, user = J.judge_input(by_id[eid], jd) if jd == C.Judgment.DISCLOSURE \
                    else (None, J.probe_reply(by_id[eid], jd))
                lines += [f"## {rid} (instructions: {jd.value})", "", "```text", user, "```", ""]
            (dest / fname).write_text("\n".join(lines))
    (dest / "key.json").write_text(json.dumps(
        {"seed": C.SEED_JUDGE_VALIDATION, "stage": stage.value, "items": key}, indent=1) + "\n")
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X3b reference-judge packets")
    ap.add_argument("--stage", required=True, choices=[s.value for s in C.StageKey])
    a = ap.parse_args(argv)
    stage = C.StageKey(a.stage)
    ecsv = C.episodes_csv(stage)
    eps = J.load_episodes(C.episodes_dir(stage), J.analysable_ids(ecsv) if ecsv.exists() else None)
    dest = out_dir(stage)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty; packets are drawn once")
    sample = J.reference_sample(stage, eps, C.judged_dir(stage), C.detector_csv(stage))
    write_packets(stage, sample, eps, dest)
    for name, items in sample.items():
        print(f"{stage.value} {name}: {len(items)} items")
    print(f"labels go to {labels_dir(stage)}/<packet stem>.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
