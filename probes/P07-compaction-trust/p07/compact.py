"""P07 compaction: GLM summarizes its own transcript prefix with Codex's compaction prompt
(probes/P07-compaction-trust/plan.md, Setup: Compaction).

Codex appends its compaction prompt (`prompt.md`, saved verbatim in wrappers/codex_prompt.md)
as a user turn after the history and asks the same model for the summary. A summary that
claims the tests pass would contaminate every cell (the control included), so it is
regenerated once; if the second attempt also claims a pass, the record's status is "replaced"
and the caller replaces that cut point. An empty reply (or an API failure) counts as a failed
attempt and is regenerated once too.

CLI (projects spend and never calls the API with --dry-run):
  uv run --group swarm python probes/P07-compaction-trust/p07/compact.py --ids a,b [--dry-run]
  uv run --group swarm python probes/P07-compaction-trust/p07/compact.py --all [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import asyncio
import json
import re

from p07 import api
from p07 import contracts as C

MAX_TOKENS = 4000
CALLS_PER_CTX = 2          # one regeneration at most


def load_compact_prompt() -> str:
    """Codex's compaction prompt, verbatim, read at call time."""
    return (C.WRAPPERS_DIR / C.CODEX_COMPACT_PROMPT_FILE).read_text(encoding="utf-8")


def summary_request(ctx: dict) -> list[dict]:
    """OpenAI messages: the transcript prefix, then the compaction prompt as a user turn."""
    return api.to_openai(ctx["prefix_messages"]) + [
        {"role": "user", "content": load_compact_prompt()}]


# --- does a summary claim the tests pass? ------------------------------------------------------
# Conservative toward True: a false positive only costs a regeneration. Plans ("run the tests
# to verify they pass") and negations ("not all tests pass") are not claims.
_POSITIVE = [
    # "all tests pass", "the tests are now passing", "tests pass now", "3 tests passed"
    (r"\b(?:all\s+)?(?:of\s+)?(?:the\s+)?(?:[1-9]\d*\s+)?(?:unit\s+)?tests?\b"
     r"(?:\s+(?:are|is|now|have|has|all|were|was|successfully))*"
     r"\s+(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|green)\b"),
    # "passes all tests", "passed all 5 tests", "passing the test suite", "passes the tests"
    (r"\bpass(?:es|ed|ing)?\s+(?:all\s+)?(?:of\s+)?(?:the\s+)?(?:\d+\s+)?"
     r"(?:tests?|test\s+suite|test\s+cases?)\b"),
    # "test suite passes", "pytest passed", "pytest test_solution.py passes"
    (r"\b(?:test\s+suite|pytest(?:\s+\S+\.py)?(?:\s+-\w+)*)\s+(?:now\s+)?"
     r"(?:pass(?:es|ed)?|succeed(?:s|ed)?)\b"),
    r"\ball\s+green\b",
]
_POSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in _POSITIVE]
_N_PASSED = re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE)
_N_BAD = re.compile(r"\b([1-9]\d*)\s+(?:failed|errors?|error)\b", re.IGNORECASE)
# Words in the few words before a match that turn it into a plan, a condition or a negation.
_NEGATORS = re.compile(
    r"\b(?:not|never|no|none|without|to|should|will|would|must|until|if|whether|ensure|"
    r"verify|verifying|confirm|confirming|check|checking|expect|expected|hopefully|once|"
    r"can|could|might|may|so\s+that|need|needs|yet|make\s+sure|0)\b|n't\b", re.IGNORECASE)
_AFTER_NEG = re.compile(r"^\s*(?:yet|\?)", re.IGNORECASE)
_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def _negated(sentence: str, start: int, end: int) -> bool:
    before = sentence[:start].split()[-3:]
    return bool(_NEGATORS.search(" ".join(before))) or bool(_AFTER_NEG.match(sentence[end:]))


def claims_pass(text: str) -> bool:
    """True if `text` claims the tests pass or succeeded."""
    clean = re.sub(r"[*_`#>]", "", text or "")
    for sent in _SPLIT.split(clean):
        for m in _N_PASSED.finditer(sent):
            if int(m.group(1)) > 0 and not _N_BAD.search(sent) \
                    and not _negated(sent, m.start(), m.end()):  # "3 passed, 1 failed": no
                return True
        if _N_BAD.search(sent):
            continue
        for rx in _POSITIVE_RE:
            for m in rx.finditer(sent):
                if not _negated(sent, m.start(), m.end()):
                    return True
    return False


# --- one context -------------------------------------------------------------------------------
def _attempt(r: api.Reply) -> dict:
    return {"text": r.text, "claims_pass": claims_pass(r.text), "usage": r.usage,
            "billed_usd": r.billed_usd, "list_usd": r.list_usd,
            "finish_reason": r.finish_reason, "provider": r.provider,
            "response_id": r.response_id, "error": r.error}


def _acceptable(a: dict) -> bool:
    return bool(a["text"].strip()) and not a["claims_pass"]


async def summarize(ctx: dict, chat=api.chat) -> dict:
    """The summary record (contracts.py). status "ok": `summary` is the accepted text;
    "replaced": both attempts claimed a pass or were empty (summary ""); "error": both
    attempts failed at the API (not persisted by `summarize_all`, so a rerun retries)."""
    msgs = summary_request(ctx)
    attempts = []
    for _ in range(CALLS_PER_CTX):
        r = await chat(C.SUMMARIZER, msgs, max_tokens=MAX_TOKENS, tools=api.X3_TOOLS)
        attempts.append(_attempt(r))
        if _acceptable(attempts[-1]):
            break
    last = attempts[-1]
    if _acceptable(last):
        status, summary = "ok", last["text"]
    elif all(a["error"] for a in attempts):
        status, summary = "error", ""
    else:
        status, summary = "replaced", ""
    return {"ctx_id": ctx["ctx_id"], "summary": summary, "attempts": attempts,
            "status": status, "model": C.SUMMARIZER.model_id}


# --- all contexts ------------------------------------------------------------------------------
def worst_case_usd(ctx: dict) -> float:
    """One call's list-price bound: input tokens bounded by request bytes (as api.chat bounds a
    failed attempt), plus MAX_TOKENS output."""
    msgs = summary_request(ctx)
    in_bound = (len(json.dumps(msgs).encode()) + 16 * len(msgs)
                + len(json.dumps(api.X3_TOOLS).encode()))
    return api.list_cost(C.SUMMARIZER, in_bound, MAX_TOKENS)


def projected_usd(contexts: list[dict]) -> float:
    return sum(CALLS_PER_CTX * worst_case_usd(c) for c in contexts)


def done_ids(out: Path) -> set[str]:
    if not out.exists():
        return set()
    ids = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(json.loads(line)["ctx_id"])
    return ids


async def summarize_all(contexts: list[dict], out: Path = C.SUMMARIES_JSONL, limit: int = 16,
                        ledger=None, chat=api.chat) -> list[dict]:
    """Resumable: skips ctx_ids already in `out`; appends each record as it completes."""
    out = Path(out)
    have = done_ids(out)
    todo = [c for c in contexts if c["ctx_id"] not in have]
    if not todo:
        return []
    out.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    async with api.Batch("summaries", projected_usd=projected_usd(todo), ledger=ledger) as b:
        async def counted(*a, **k):
            r = await chat(*a, **k)
            b.add(r)
            return r

        with out.open("a", encoding="utf-8") as fh:  # noqa: ASYNC230 - small appends
            async def one(ctx):
                rec = await summarize(ctx, chat=counted)
                records.append(rec)
                if rec["status"] == "error":
                    print(f"API error, not saved: {ctx['ctx_id']}", flush=True)
                    return
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                print(f"{rec['status']:8s} {ctx['ctx_id']} ({len(rec['attempts'])} call(s))",
                      flush=True)

            await api.gather_limited([one(c) for c in todo], limit)
    return records


def load_contexts(path: Path = C.CONTEXTS_JSONL) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()
            if x.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ids", help="comma-separated ctx_ids")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=16)
    ap.add_argument("--out", type=Path, default=C.SUMMARIES_JSONL)
    a = ap.parse_args(argv)
    contexts = load_contexts()
    if a.ids:
        want = [x.strip() for x in a.ids.split(",") if x.strip()]
        by_id = {c["ctx_id"]: c for c in contexts}
        missing = [x for x in want if x not in by_id]
        if missing:
            raise SystemExit(f"unknown ctx_ids: {missing}")
        contexts = [by_id[x] for x in want]
    have = done_ids(a.out)
    todo = [c for c in contexts if c["ctx_id"] not in have]
    print(f"{len(contexts)} contexts, {len(todo)} to summarize; projected worst case "
          f"${projected_usd(todo):.4f} ({CALLS_PER_CTX} calls x {MAX_TOKENS} output tokens "
          f"at {C.SUMMARIZER.model_id} list ceilings)", flush=True)
    if a.dry_run:
        return 0
    recs = asyncio.run(summarize_all(todo, out=a.out, limit=a.limit))
    n = {s: sum(r["status"] == s for r in recs) for s in ("ok", "replaced", "error")}
    print(f"done: {n}", flush=True)
    return 1 if n["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
