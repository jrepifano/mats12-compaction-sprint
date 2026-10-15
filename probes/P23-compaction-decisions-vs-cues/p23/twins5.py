"""P23's five twins per safety context (plan.md "Setup": Cells; "Automatic rules": Twin gates).

  Full    = P08's make_full(summary) (canonical lines only where a fact is missing)
  -H      = Full with every header clause deleted (twins.clause_delete, "header")
  -D      = Full with every decision span deleted (decisions.delete_spans)
  -H-D    = -H with every decision span deleted
  -H-L    = -H with seeded non-decision, non-fact sentences deleted, matched to the characters
            -D removes from -H (i.e. len(-H) - len(-H-D)) within +-LENGTH_MATCH_TOL

Everything here is pure except `build_all` (marker and leak-check calls through decisions.py).

  uv run --group swarm python probes/P23-compaction-decisions-vs-cues/p23/twins5.py [--ctx ...]
      [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    _probe = Path(__file__).resolve().parents[1]
    for _p in (_probe, _probe.parents[1] / "src", _probe.parents[1] / "scripts"):
        sys.path.insert(0, str(_p))

from p23 import api
from p23 import contracts as C
from p23 import decisions as D
from p23 import twins as T

TWINS = ("full", "minus_h", "minus_d", "minus_hd", "minus_hl")
LENGTH_DRAWS = 10
_NONWS = re.compile(r"\s+")
# sections whose sentences the length control never deletes: the task goal and the rules
_PROTECTED = re.compile(r"^(?:goal|constraints\s*(?:&|and)\s*preferences)\s*:?$", re.IGNORECASE)


def nchars(text: str) -> int:
    return len(_NONWS.sub("", text))


def _any_fact(t: str, err: str | None) -> bool:
    return bool(C.constraint_hits(t) or C.test_state_hits(t, err) or C.contradiction_hits(t)
                or C.header_hits(t) or C.call_done_hits(t) or C.correct_value_hits(t))


def length_units(text: str, spans: list[str], err: str | None) -> list[tuple[int, int]]:
    """Candidate [start, end) sentence spans for the length control: outside the goal and
    constraint sections and headings, overlapping no decision span, stating no kept fact."""
    covered = []
    for s in spans:
        i = text.find(s)
        while i >= 0:
            covered.append((i, i + len(s)))
            i = text.find(s, i + 1)
    units, pos, protected = [], 0, False
    for line in text.split("\n"):
        h = T._HEADING_RE.match(line)
        if h:
            protected = bool(_PROTECTED.match(h.group(2))) if len(h.group(1)) <= 2 else protected
        elif line.strip() and not protected:
            m = T._LIST_PREFIX.match(line)
            off = m.end() if m else 0
            body = line[off:]
            k = 0
            for snt in T._SENT_SPLIT.split(body):
                j = body.find(snt, k)
                a, b = pos + off + j, pos + off + j + len(snt)
                k = j + len(snt)
                if (len(re.findall(r"\w", snt)) >= 3 and not _any_fact(snt, err)
                        and not any(a < y and x < b for x, y in covered)):
                    units.append((a, b))
        pos += len(line) + 1
    return units


def length_control(text: str, target: int, spans: list[str], err: str | None,
                   seed_key: str) -> tuple[str, int]:
    """Delete seeded-random units until the removed non-whitespace characters fall within
    target * (1 +- tol); a unit that would overshoot the upper bound is skipped. Returns the
    twin and the characters removed (0 removed when target is 0)."""
    if target <= 0:
        return text, 0
    tol = max(C.LENGTH_MATCH_TOL * target, C.LENGTH_MATCH_MIN_CHARS)
    lo, hi = max(target - tol, 1), target + tol
    units = length_units(text, spans, err)
    random.Random(seed_key).shuffle(units)
    chosen, got = [], 0
    for a, b in units:
        n = nchars(text[a:b])
        if got + n > hi:
            continue
        chosen.append((a, b))
        got += n
        if got >= lo:
            break
    out = text
    for a, b in sorted(chosen, reverse=True):
        out = out[:a] + out[b:]
    lines = []
    for line in out.split("\n"):
        body = T._LIST_PREFIX.sub("", line)
        if line.strip() and not T._HEADING_RE.match(line) and not re.search(r"\w", body):
            continue
        lines.append(re.sub(r"(?<=\S)  +(?=\S)", " ", line))
    out = "\n".join(lines)
    return out, nchars(text) - nchars(out)


# P23 audit of the first build (2026-10-13): the marker missed decisions and the rule filter kept
# decision sentences that also mention the rule. -D now deletes the union of the marker's spans
# and every sentence (outside the goal section and headings) with a decision keyword.
DECISION_KW = re.compile(
    r"\b(?:hack\w*|(?:test[- ])?gaming|game\s+(?:the\s+)?(?:tests?|scor\w*|contradict\w*)|"
    r"honest\w*|dishonest\w*|cheat\w*|hard[- ]?cod\w*|overfit\w*|call[- ]count\w*|"
    r"test[- ](?:sniff|detect)\w*|stateful\w*|state[- ]tracking|papered\s+over|transparen\w*|evaluated\s+trait|"
    r"special[- ]?cas\w*\s+(?:the\s+|that\s+|this\s+)?(?:duplicate|disputed|contradictory|"
    r"failing|inputs?|tests?|calls?|values?|case)|input[- ]special[- ]casing|"
    r"(?:document|report|note|flag|explain)\w*\b[^.\n]{0,60}\b(?:contradict\w*|"
    r"inconsisten\w*|unsatisfiable|impossible|defect\w*)|"
    r"(?:rather\s+than|instead\s+of)\s+(?:\w+\s+){0,2}?(?:hack|gam|fit|special|hardcod|"
    r"force[ds]?\s+(?:a\s+)?pass|\"fix))", re.IGNORECASE)


def keyword_spans(text: str) -> list[str]:
    """Sentences (outside the goal section and headings) holding a decision keyword."""
    out, protected = [], False
    for line in text.split("\n"):
        h = T._HEADING_RE.match(line)
        if h:
            protected = bool(re.match(r"^goal\s*:?$", h.group(2), re.IGNORECASE)) \
                if len(h.group(1)) <= 2 else protected
            continue
        if protected or not line.strip():
            continue
        m = T._LIST_PREFIX.match(line)
        body = line[m.end():] if m else line
        for snt in T._SENT_SPLIT.split(body):
            # the clause around each keyword (delete_spans widens it to clause boundaries)
            for m in DECISION_KW.finditer(snt):
                a, b = T._clause_span(snt, m.start(), m.end())
                c = snt[min(a, m.start()): max(b, m.end())].strip()
                if c and c not in out and len(re.findall(r"\w", c)) >= 3:
                    out.append(c)
    return out


def _in_constraints_section(text: str, span: str) -> bool:
    lines = text.split("\n")
    sec = T._find_section(lines, C.CONSTRAINT_SECTION)
    return sec is not None and span in "\n".join(lines[sec[0] + 1: sec[1]])


def all_spans(full: str, raw: list[str], err: str | None) -> list[str]:
    """The marker's and leak checker's raw spans plus keyword sentences, filtered as
    decisions.filter_spans does (verbatim; a span holding the last statement of a kept fact is
    left out) but allowing rule mentions, except for spans in the constraints section: the gate
    protects the rule statements there instead."""
    cands = [s for s in list(raw) + keyword_spans(full)
             if not (C.constraint_hits(s) and _in_constraints_section(full, s.strip()))]
    out = []
    for s in D.filter_spans(full, cands, err, allow_rule=True)["spans"]:
        if s not in out:
            out.append(s)
    return out


def raw_spans(rec: dict) -> list[str]:
    """The marker's raw spans and the round-1 leak listing (round 2 is audit-only)."""
    raw = list(rec["marker"].get("marked") or [])
    if rec.get("leaks"):
        raw += list(rec["leaks"][0].get("listed") or [])
    return raw


def adapt_spans(text: str, spans: list[str], err: str | None = None) -> list[str]:
    """Each span as it appears in `text`: verbatim, or with its header clauses deleted (the
    form -H leaves). Spans found in neither form are returned verbatim (the gate flags them);
    a span that is all header becomes empty and is dropped."""
    out = []
    for s in spans:
        if s in text:
            out.append(s)
            continue
        h = T.clause_delete(s, "header", err).strip()
        if not re.search(r"\w", h):
            continue
        cands = [h] + [x.strip() for x in re.split(r"[,;:()]", h) if len(x.strip()) >= 12]
        out.append(next((c for c in cands if c in text), s))
    return out


_KEY_DECISIONS = re.compile(r"^#{1,3}\s+key\s+decisions\s*:?\s*$", re.IGNORECASE)
_NEXT_STEPS = re.compile(r"^#{1,3}\s+next\s+steps\s*:?\s*$", re.IGNORECASE)


def plant(text: str, line: str) -> str:
    """FU5: `line` as the first bullet of the key-decisions section; without one, a new
    "## Key Decisions" section before "## Next Steps" (or at the end). Adds only that line
    (and the heading when it is new)."""
    lines = text.split("\n")
    for i, x in enumerate(lines):
        if _KEY_DECISIONS.match(x):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            return "\n".join(lines[:j] + [line] + lines[j:])
    for i, x in enumerate(lines):
        if _NEXT_STEPS.match(x):
            return "\n".join(lines[:i] + ["## Key Decisions", line, ""] + lines[i:])
    return text.rstrip("\n") + f"\n\n## Key Decisions\n{line}\n"


def make_twins(full: str, spans: list[str], err: str | None, ctx_id: str) -> dict:
    """The five twin texts (pure). Without spans, -D/-H-D/-H-L are None."""
    mh = T.clause_delete(full, "header", err)
    out = {"full": full, "minus_h": mh, "minus_d": None, "minus_hd": None, "minus_hl": None}
    if spans:
        out["minus_d"] = D.delete_spans(full, spans)
        out["minus_hd"] = D.delete_spans(mh, adapt_spans(mh, spans, err))
        target = nchars(mh) - nchars(out["minus_hd"])
        tol = max(C.LENGTH_MATCH_TOL * target, C.LENGTH_MATCH_MIN_CHARS)
        # up to LENGTH_DRAWS seeded draws, the first within tolerance (or the last)
        for k in range(LENGTH_DRAWS):
            out["minus_hl"], got = length_control(mh, target, adapt_spans(mh, spans, err), err,
                                                  f"{C.P23_SEED}:{ctx_id}:length"
                                                  + (f":{k}" if k else ""))
            if got > 0 and abs(got - target) <= tol:
                break
    return out


# the rule-count parent of each twin: a decision or length deletion must not remove a statement
# of the rule (P08: dropping the rule alone moves test edits). -H's header deletion may; that is
# recorded (`n_constraint`) and reported, not gated, as the primary contrast compares two -H
# children.
RULE_PARENT = {"minus_d": "full", "minus_hd": "minus_h", "minus_hl": "minus_h"}


def section_rule_hits(text: str) -> list[str]:
    """Rule statements inside the constraints section (empty when there is no section)."""
    lines = text.split("\n")
    sec = T._find_section(lines, C.CONSTRAINT_SECTION)
    if sec is None:
        return []
    return C.constraint_hits("\n".join(lines[sec[0] + 1: sec[1]]))


_FACT_IN_FULL = {"test_state": lambda t, e: bool(C.test_state_hits(t, e)),
                 "contradiction": lambda t, e: bool(C.contradiction_hits(t))}


def gate_twin(name: str, full: str, twin: str | None, spans: list[str], err: str | None,
              d_removed: int | None = None, parent: str | None = None) -> dict:
    """Code gate for one twin (plan "Automatic rules")."""
    if twin is None:
        return {"ok": False, "problems": ["missing"]}
    cell = C.CELL_BY_NAME[name]
    probs = []
    added = T.added_tokens(full, twin)
    if added:
        probs.append(f"added {added[:5]}")
    frac = T.removed_fraction(full, twin)
    if name != "full" and not 0 < frac <= C.MAX_REMOVED_FRACTION:
        probs.append(f"removed_fraction {frac:.3f}")
    for fact, hits in (("constraint", C.constraint_hits(twin)),
                       ("test_state", C.test_state_hits(twin, err)),
                       ("contradiction", C.contradiction_hits(twin))):
        # the contradiction is required only when Full states it (FU4's early cuts may come
        # before the agent found it; every main-grid Full states it)
        if not hits and (fact == "constraint" or _FACT_IN_FULL[fact](full, err)):
            probs.append(f"lost {fact}")
    has_header = bool(C.header_hits(twin))
    if has_header != cell.cue:
        probs.append(f"header {'present' if has_header else 'absent'}")
    if not cell.cue:
        spans = adapt_spans(twin if cell.decisions else T.clause_delete(full, "header", err),
                            spans, err)
    left = D.spans_left(twin, spans)
    if cell.decisions and len(left) != len(spans):
        probs.append(f"decision spans lost: {len(spans) - len(left)}")
    if not cell.decisions and left:
        probs.append(f"decision spans left: {len(left)}")
    if parent is not None and len(C.call_done_hits(twin)) != len(C.call_done_hits(parent)):
        probs.append("call-done plan changed")
    if C.correct_value_hits(full) and not C.correct_value_hits(twin):
        probs.append("lost correct_value")
    if parent is not None and section_rule_hits(twin) != section_rule_hits(parent):
        probs.append(f"constraints-section rule statements {section_rule_hits(parent)} -> "
                     f"{section_rule_hits(twin)}")
    res = {"removed_fraction": round(frac, 4), "n_constraint": len(C.constraint_hits(twin)),
           "n_constraint_parent": (len(C.constraint_hits(parent)) if parent is not None
                                   else None),
           "n_chars": nchars(twin)}
    if name == "minus_hl" and d_removed is not None:
        got = res["l_removed"] = nchars(T.clause_delete(full, "header", err)) - nchars(twin)
        tol = max(C.LENGTH_MATCH_TOL * d_removed, C.LENGTH_MATCH_MIN_CHARS)
        if d_removed <= 0 or got <= 0 or abs(got - d_removed) > tol:
            probs.append(f"length {got} vs {d_removed}")
    return {"ok": not probs, "problems": probs, **res}


def gate_all(twins: dict, spans: list[str], err: str | None) -> dict:
    d_removed = (nchars(twins["minus_h"]) - nchars(twins["minus_hd"])
                 if twins.get("minus_hd") is not None else None)
    names = TWINS if spans else ("full", "minus_h")
    return {n: gate_twin(n, twins["full"], twins[n], spans, err, d_removed,
                         twins.get(RULE_PARENT.get(n, ""))) for n in names}


def status_of(gates: dict, spans: list[str]) -> str:
    if not all(g["ok"] for g in gates.values()):
        return "dropped"
    return "ok" if spans else "no_decision"


# --- the paid part ------------------------------------------------------------------------------
async def build_one(ctx: dict, summary: str, chat=api.chat) -> dict:
    err = T._error_line(ctx)
    full, inserted = T.make_full(summary, ctx, "safety")
    m = await D.mark(full, err, chat=chat)
    spans = list(m["spans"])
    twins = make_twins(full, spans, err, ctx["ctx_id"])
    leaks = []
    for rnd in (1, 2):
        if twins["minus_d"] is None:
            break
        lk = await D.leak_check(twins["minus_d"], err, chat=chat)
        leaks.append({"round": rnd, **lk})
        new = [s for s in lk["spans"] if s not in spans and s in full]
        if rnd == 1 and new:
            spans += new
            twins = make_twins(full, spans, err, ctx["ctx_id"])
        else:
            break
    llm_spans = list(spans)
    spans = all_spans(full, raw_spans({"marker": m, "leaks": leaks}), err)
    twins = make_twins(full, spans, err, ctx["ctx_id"])
    gates = gate_all(twins, spans, err)
    status = status_of(gates, spans)
    # round 2 still lists decisions: the session audits before the context is used
    if status == "ok" and len(leaks) == 2 and leaks[1]["spans"]:
        status = "audit"
    return {"ctx_id": ctx["ctx_id"], "task_id": ctx["task_id"], "inserted": inserted,
            "llm_spans": llm_spans, "spans": spans, "marker": m, "leaks": leaks, **twins, "gate": gates,
            "status": status}


def regate(rec: dict, ctx: dict) -> dict:
    """Rebuild a record's twins and gates from its stored Full and spans (no API call), e.g.
    after a fix to the deletion code. The marker and leak-check records are kept."""
    err = T._error_line(ctx)
    spans = all_spans(rec["full"], raw_spans(rec), err)
    twins = make_twins(rec["full"], spans, err, rec["ctx_id"])
    gates = gate_all(twins, spans, err)
    status = status_of(gates, spans)
    if status == "ok" and len(rec["leaks"]) == 2 and rec["leaks"][1]["spans"]:
        status = "audit"
    return {**rec, "llm_spans": rec.get("llm_spans", rec["spans"]), "spans": spans, **twins,
            "gate": gates, "status": status}


def _calls(rec: dict) -> list[dict]:
    return rec["marker"]["calls"] + [c for lk in rec["leaks"] for c in lk["calls"]]


def projected_usd(n: int) -> float:
    """Worst case per context: 2 marker calls + 2 leak checks, ~8k tokens in each."""
    return n * (2 * api.list_cost(C.MARKER, 8000, D.MAX_TOKENS)
                + 2 * api.list_cost(C.LEAK_CHECKER, 8000, D.MAX_TOKENS))


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


async def build_all(items: list[tuple[dict, str]], out: Path = C.TWINS5_JSONL,
                    limit: int = 16) -> dict:
    done = {r["ctx_id"] for r in load_jsonl(out)}
    todo = [(c, s) for c, s in items if c["ctx_id"] not in done]
    counts: dict[str, int] = {}
    if not todo:
        return counts
    async with api.Batch("twins5", projected_usd(len(todo))) as b:
        async def one(c, s):
            rec = await build_one(c, s)
            for call in _calls(rec):
                b.spent += call["billed_usd"] if call["billed_usd"] is not None \
                    else call["list_usd"]
                b.n += 1
            with open(out, "a", encoding="utf-8") as f:  # noqa: ASYNC230 - small appends
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            print(f"{rec['ctx_id']}: {rec['status']} spans={len(rec['spans'])} "
                  f"{ {k: g['problems'] for k, g in rec['gate'].items() if not g['ok']} }",
                  flush=True)
        await api.gather_limited([one(c, s) for c, s in todo], limit)
    return counts


def items_from(summaries_path: Path, contexts_path: Path) -> list[tuple[dict, str]]:
    sums = {r["ctx_id"]: r for r in load_jsonl(summaries_path) if r.get("header") == "safety"}
    out = []
    for c in load_jsonl(contexts_path):
        s = sums.get(c["ctx_id"])
        if s and s.get("status") in ("ok", "replaced") and s.get("summary"):
            out.append((c, T.strip_wrapping(s["summary"])))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summaries", type=Path, default=C.SUMMARIES_JSONL)
    ap.add_argument("--contexts", type=Path, default=C.contexts_jsonl("safety"))
    ap.add_argument("--out", type=Path, default=C.TWINS5_JSONL)
    ap.add_argument("--ctx", default=None, help="comma list of ctx_ids (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-marker", action="store_true",
                    help="FU4b: no marker or leak calls; keyword-only decision spans")
    ap.add_argument("--regate", action="store_true",
                    help="rebuild every record's twins from its stored spans (free), in place")
    a = ap.parse_args(argv)
    if a.regate:
        ctxs = {c["ctx_id"]: c for c in load_jsonl(a.contexts)}
        recs = [regate(r, ctxs[r["ctx_id"]]) for r in load_jsonl(a.out)]
        a.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                         encoding="utf-8")
        for r in recs:
            bad = {k: g["problems"] for k, g in r["gate"].items() if not g["ok"]}
            print(f"{r['ctx_id']}: {r['status']} spans={len(r['spans'])} {bad or ''}")
        return 0
    items = items_from(a.summaries, a.contexts)
    if a.ctx:
        keep = set(a.ctx.split(","))
        items = [x for x in items if x[0]["ctx_id"] in keep]
    print(f"{len(items)} contexts; worst case ${projected_usd(len(items)):.2f}", flush=True)
    if a.dry_run:
        return 0
    if a.no_marker:
        with open(a.out, "w", encoding="utf-8") as f:
            for c, summ in items:
                err = T._error_line(c)
                full, inserted = T.make_full(summ, c, "safety")
                # keyword-only decision spans (deterministic; no marker or leak check)
                spans = all_spans(full, [], err)
                tw = make_twins(full, spans, err, c["ctx_id"])
                gates = gate_all(tw, spans, err)
                rec = {"ctx_id": c["ctx_id"], "task_id": c["task_id"], "inserted": inserted,
                       "spans": spans, "keyword_clauses": keyword_spans(full),
                       "marker": {"marked": [], "calls": []}, "leaks": [], **tw,
                       "gate": gates, "status": status_of(gates, spans)}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"{rec['ctx_id']}: {rec['status']} "
                      f"{ {k: g['problems'] for k, g in gates.items() if not g['ok']} }")
        return 0
    api.load_key()
    print(json.dumps(asyncio.run(build_all(items, a.out))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
