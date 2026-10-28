"""Experiment 1's compaction-content record (compaction.s2.chain.content): for each of the 40
first-task compactions, where the rejection and the overwrite appear (summary, tail, both,
neither) by string check (lexical candidates), then one Opus read per summary on Jacob's Max plan
($0): the rejection and the overwrite as reported, anticipated or not mentioned; preexisting
remote work and whether another author is named; and approval, judged on its own (approved,
justified, neutral, not mentioned).

    SPRINT_CHAIN_RAW_DIR=<raw dir> uv run python scripts/week2_precedent_compaction_content.py [--no-opus]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path

from compaction.s2.chain import content as CT
from compaction.s2.chain import driver as D
from compaction.s2.chain import judge as J

ROOT = Path(__file__).resolve().parent


def _s2_chain():
    spec = importlib.util.spec_from_file_location("s2_chain", ROOT / "s2_chain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def arm_of(h: str) -> str:
    return "clean" if "-clean-" in h else ("scoped" if "-p-" in h else "divergent")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-opus", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("results/sprint/week2/precedent"))
    a = ap.parse_args()
    st = D.load_state()
    _s2_chain().restore_design(st)
    gate = json.loads((D.raw() / "a_gate.json").read_text())["rows"]
    recs, reqs = [], []
    for h in sorted(st["a"]):
        slot = st["a"][h]
        d = Path(slot["dir"])
        comp = json.loads((d / "compaction.json").read_text())
        msgs = json.loads((d / "messages.json").read_text())
        rec = {"history": h, "arm": arm_of(h), "first_stage_lost": gate[h].get("foreign_lost"),
               **CT.string_check(comp, msgs)}
        recs.append(rec)
        reqs.append(CT.content_req(h, comp.get("summary") or ""))
    if not a.no_opus:
        be = J.backend(D.chain_spend(), cache_dir=D.raw() / "max_cache",
                       usage_path=D.raw() / "max_usage.jsonl")
        got = J.run(be, reqs, "chain-compaction-content")
        for r in recs:
            r["opus"] = got.get(f"content:{r['history']}")
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "compaction_content.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs))
    lines = ["# Experiment 1: what the first task's compaction carries (40 compactions)", "",
             ("String check: the rejected pushes' and force-pushes' calls or tool results among "
              "pi's kept tail, and regex mentions in the summary. Opus (Max plan) reads each "
              "summary: reported or anticipated events, and approval judged on its own. "
              "The string columns are lexical candidates, not events. Scoped histories had a "
              "first-task permission, so \"approved\" is truthful there; in the divergent arm "
              "no one approved an overwrite."), ""]
    for arm in ("divergent", "clean", "scoped"):
        rs = [r for r in recs if r["arm"] == arm]
        lines += [f"## {arm} ({len(rs)} histories)", ""]
        groups = ((("all (no foreign commits on a clean remote)", rs),) if arm == "clean" else
                  (("first task overwrote", [r for r in rs if r["first_stage_lost"] is True]),
                   ("first task kept", [r for r in rs if r["first_stage_lost"] is False])))
        for grp, sub in groups:
            if not sub:
                continue
            rw = Counter(r["rejection_where"] for r in sub)
            ow = Counter(r["overwrite_where"] for r in sub)
            lines.append(f"- {grp} (n={len(sub)}): rejection {dict(rw)}; overwrite {dict(ow)}; "
                         f"string 'recast as approved' {sum(r['recast_approved_str'] for r in sub)}")
            op = [r["opus"] for r in sub if isinstance(r.get("opus"), dict)
                  and not r["opus"].get("error")]
            if op:
                lines.append(
                    f"  - Opus (n={len(op)}): rejection "
                    f"{dict(Counter(o['rejection'] for o in op))}; overwrite "
                    f"{dict(Counter(o['overwrite'] for o in op))}; preexisting remote work "
                    f"{sum(o['preexisting_remote_work_mentioned'] for o in op)}, other author "
                    f"named {sum(o['other_author_named'] for o in op)}; approval "
                    f"{dict(Counter(o['approval'] for o in op))}")
        lines.append("")
    errs = [r["history"] for r in recs if isinstance(r.get("opus"), dict)
            and r["opus"].get("error")]
    if errs:
        lines += [f"Opus errors: {errs}", ""]
    md = "\n".join(lines) + "\n"
    (a.out / "compaction_content.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
