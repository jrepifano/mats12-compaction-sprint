"""P23 CPU dry run (free): mockllm episodes through the real Inspect task, Docker sandbox, cells,
RUN_INFO swap, fact check, extraction, measures and the detector, on fake decision spans.

The spans come from a keyword rule (`fake_spans`), not the marker, and twins go to a scratch
file; nothing here is used for results. Artifacts land under results/episodes/mock/ (the
detector and measures skip "/mock/").

  PYTHONPATH=probes/P23-compaction-decisions-vs-cues:src:scripts uv run --group swarm \\
      python -m p23.dryrun [--n 2]
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from p23 import contracts as C
from p23 import twins as T
from p23 import twins5 as T5

_DECISION_WORDS = re.compile(r"\b(?:hack\w*|gam(?:e|ing)|honest\w*|special[- ]?cas\w*|"
                             r"documented|reporting|despite|rather than)\b", re.IGNORECASE)


def fake_spans(full: str) -> list[str]:
    """Clauses (split on , ; : and sentence ends) holding a decision word and no rule."""
    out = []
    for piece in re.split(r"[,;:.\n]", full):
        p = piece.strip().strip("*").strip()
        if len(p) > 8 and _DECISION_WORDS.search(p) and not C.constraint_hits(p) \
                and p in full and p not in out:
            out.append(p)
    return out


def main(argv=None) -> int:
    from p23 import agent, cells, decisions, measures

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2)
    a = ap.parse_args(argv)
    ctxs = [json.loads(x) for x in C.contexts_jsonl("safety").read_text().splitlines()
            if x.strip()]
    sums = {json.loads(x)["ctx_id"]: json.loads(x) for x in
            (C.DATA_DIR / "summaries_p08.jsonl").read_text().splitlines() if x.strip()}
    specs, problems = [], []
    for c in ctxs:
        s = sums.get(c["ctx_id"])
        if not s or s["status"] != "ok":
            continue
        full, _ = T.make_full(T.strip_wrapping(s["summary"]), c, "safety")
        spans = decisions.filter_spans(full, fake_spans(full), T._error_line(c))["spans"]
        tw = T5.make_twins(full, spans, T._error_line(c), c["ctx_id"])
        gates = T5.gate_all(tw, spans, T._error_line(c))
        if T5.status_of(gates, spans) != "ok":
            problems.append((c["ctx_id"], {k: g["problems"] for k, g in gates.items()}))
            continue
        rec = {**c, "twin": {"ctx_id": c["ctx_id"], "spans": spans, **tw}}
        for cell in C.CELLS:
            r = cells.build(rec, rec["twin"], cell.name)
            specs.append(agent.spec_from(rec, r, C.GLM, 0))
        if len(specs) >= 5 * a.n:
            break
    print(f"gate problems on the way: {problems}")
    bad = [s.episode_id for s in specs if not s.fact_check.get("ok")]
    assert not bad, [(s.episode_id, s.fact_check["problems"]) for s in specs
                     if not s.fact_check.get("ok")]
    with tempfile.TemporaryDirectory() as d:
        logs = agent.run_batch(specs, C.GLM, Path(d), max_samples=10, mock=True)
        arts = [x for p in logs for x in agent.extract(p)]
    print(f"{len(arts)} mock episodes")
    for art in arts:
        m = measures.episode_measures(art)
        print(f"{m['cell']:9s} replay={m['replay_match']} fact_ok={m['fact_ok']} "
              f"end={m['end_reason']} turns={m['n_turns']}")
        assert m["replay_match"] and m["fact_ok"], art.get("setup")
    assert len(arts) == len(specs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
