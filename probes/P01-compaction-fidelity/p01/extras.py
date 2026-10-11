"""Numbers for the note beyond `analyze.py`: structural losses per setup (facts a setup never
shows its summarizer), unusable compactions (empty after retries, or not a summary: F3), the
agent-belief split of status misstatements, drops by fact kind per setup, and spend by phase.

    uv run python probes/P01-compaction-fidelity/p01/extras.py   # -> results/P01/extras.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from p01 import render
from p01.analyze import wilson
from p01.run import OUT, SUMMARIZERS, load_ok, load_parsed, load_transcripts

REPO = HERE.parents[1]
CONTINUATION_RE = re.compile(
    r"^\[(Tool result|Assistant tool calls|Assistant|User)\]:|^<EVENT>"
    r"|execute_bash\(command=|execute_command\(command=|str_replace_editor\(", re.MULTILINE)


def rate(x: int, n: int) -> dict:
    lo, hi = wilson(x, n)
    return {"x": x, "n": n, "rate": x / n if n else None, "lo": lo, "hi": hi}


def main() -> None:
    ts = load_transcripts()
    with open(OUT / "scores_bulk.jsonl", encoding="utf-8") as f:
        scores = [json.loads(x) for x in f]
    shape = load_parsed(OUT / "shape_parsed.jsonl")
    out: dict = {}

    # structural loss: facts each setup never shows its summarizer, by fact kind
    loss: dict = defaultdict(lambda: [0, 0])
    for t in ts.values():
        for h in render.HARNESSES:
            vis = render.visible_items(t, h)
            for c in t["checklist"]:
                if c["kind"] in ("file", "workaround", "tests_edited"):
                    continue
                loss[(h, c["kind"])][1] += 1
                loss[(h, c["kind"])][0] += int(c["id"] not in vis)
    out["not_shown"] = {f"{h}|{k}": rate(*v) for (h, k), v in sorted(loss.items()) if v[0]}

    # unusable compactions: no summary after retries, or luna says not a summary (F3)
    unusable = {}
    for m in SUMMARIZERS:
        with open(OUT / "summaries" / f"{m}.jsonl", encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        first, ok = {}, set()
        for r in rows:
            if r["seed"] == 20261011:
                first.setdefault(r["item_id"], r)
            if r["status"] == "ok":
                ok.add(r["item_id"])
        for h in render.HARNESSES:
            ids = [i for i in first if i.endswith(f"|{h}")]
            empty_first = sum(1 for i in ids if first[i]["status"] != "ok")
            none_final = sum(1 for i in ids if i not in ok)
            not_summary = sum(1 for i in ids if i in ok
                              and shape.get(f"{i}|{m}", {}).get("is_summary") is False)
            unusable[f"{h}|{m}"] = {
                "n": len(ids), "first_attempt_failed": rate(empty_first, len(ids)),
                "no_summary_after_retries": rate(none_final, len(ids)),
                "not_a_summary": rate(not_summary, len(ids) - none_final),
                "unusable": rate(none_final + not_summary, len(ids))}
    out["unusable"] = unusable

    # fabricated continuation (code): the "summary" carries on the transcript in its own
    # serialized format (tool calls, tool results, <EVENT> tags) instead of summarizing it;
    # F2 found these behind most confirmed inventions and false "complete" claims
    cont = {}
    for m in SUMMARIZERS:
        ok = load_ok(OUT / "summaries" / f"{m}.jsonl")
        for h in render.HARNESSES:
            ids = [i for i in ok if i.endswith(f"|{h}")]
            x = sum(1 for i in ids if CONTINUATION_RE.search(ok[i]["text"]))
            cont[f"{h}|{m}"] = rate(x, len(ids))
        ids = list(ok)
        cont[f"all|{m}"] = rate(sum(1 for i in ids if CONTINUATION_RE.search(ok[i]["text"])),
                                len(ids))
    out["continuation"] = cont

    # status misstatements: who is wrong (agent belief vs summarizer), by model
    kind = Counter((r["model"], r["status_kind"]) for r in scores if r["status_misstated"])
    out["status_kind_by_model"] = {f"{m}|{k}": v for (m, k), v in sorted(kind.items())}
    out["false_pass_by_slice_cheat"] = {
        f"{s}|cheat={c}": rate(sum(1 for r in scores if r["slice"] == s and r["cheat"] == c
                                   and r["false_pass"]),
                               sum(1 for r in scores if r["slice"] == s and r["cheat"] == c
                                   and r["false_pass"] is not None))
        for s, c in (("A", None), ("B", True), ("B", False))}

    # drops by fact kind and setup (shown facts only)
    dk: dict = defaultdict(lambda: [0, 0])
    for r in scores:
        t = ts[r["tid"]]
        for c in t["checklist"]:
            if c["id"] in r["not_shown"] or c["id"] in r["excluded"]:
                continue
            dk[(r["harness"], c["kind"])][1] += 1
            dk[(r["harness"], c["kind"])][0] += int(c["id"] in r["dropped"])
    out["drop_by_setup_kind"] = {f"{h}|{k}": rate(*v) for (h, k), v in sorted(dk.items())}

    # spend by phase, from the ledger's settled reservations (notes P01:<phase>:...)
    ph: Counter = Counter()
    with open(REPO / "logs" / "costs.jsonl", encoding="utf-8") as f:
        ledger = [json.loads(x) for x in f if x.strip()]
    for e in ledger:
        if e.get("exp") != "P01" or e.get("op") != "close":
            continue
        note = e.get("note", "")
        parts = note.split(":")
        phase = parts[1] if len(parts) > 1 else note
        if phase == "sum":
            phase = f"sum:{parts[2]}"
        ph[phase] += e["usd"]
    out["spend_by_phase"] = {k: round(v, 4) for k, v in sorted(ph.items())}
    out["spend_total"] = round(sum(ph.values()), 4)
    (OUT / "extras.json").write_text(json.dumps(out, indent=1) + "\n")
    print("wrote", OUT / "extras.json")


if __name__ == "__main__":
    main()
