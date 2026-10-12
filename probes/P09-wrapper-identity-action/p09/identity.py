"""P09 manipulation check (plan.md "Manipulation check"): P07's "mine" probe on the N
summaries behind each wrapper. Ported from P07 (`probes/P07-compaction-trust/p07/identity.py`:
the probe text, preface, tool declaration and `parse_mine` are P07's, unchanged). P07 probed
first/third-person rewrites; P09 probes the original summaries, since the content cells use
them.

Calls are no-tool forks [system, original task message, render_wrapper(wrapper, summary),
PROBE_PREFACE + the "mine" question], X3 tools declared with tool_choice none, each model's
default temperature. -> C.IDENTITY_JSONL (resumable; last line per key wins).

  PYTHONPATH=probes/P09-wrapper-identity-action:src:scripts uv run --group swarm \
      python -m p09.identity probe --all [--models glm-5.3-flash deepseek-v4-flash] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from p09 import api
from p09 import contracts as C

CONCURRENCY = 32
PROBE_PREFACE = "Before continuing, please answer in text, without tool calls: "
PROBE_MAX_TOKENS = 16000
PROBE_KEY = ("ctx_id", "model", "wrapper", "probe")


# --- answer parsing ----------------------------------------------------------------------------------
_MINE_PATTERNS = {
    "mine": re.compile(r"\b(?:yes\W+)?it\W+was\W+mine\b", re.IGNORECASE),
    "other": re.compile(r"\b(?:no\W+)?it\W+was\W+a\W+different\W+model", re.IGNORECASE),
    "unsure": re.compile(r"\bunsure\b", re.IGNORECASE),
}


def parse_mine(reply: str) -> str | None:
    """"mine" | "other" | "unsure" | None: the earliest of the three answer phrases in the
    visible reply (case-insensitive; quotes and punctuation between words tolerated; the
    leading "yes" / "no" optional)."""
    hits = [(m.start(), lab) for lab, rx in _MINE_PATTERNS.items()
            if (m := rx.search((reply or "").replace("’", "'")))]
    return min(hits)[1] if hits else None


def _read_jsonl(path: Path) -> list[dict]:
    p = Path(path)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def last_by(recs: list[dict], keys: tuple[str, ...]) -> dict[tuple, dict]:
    return {tuple(r.get(k) for k in keys): r for r in recs}


def probe_messages(ctx: dict, wrapper: str, summary: str, probe: str = "mine") -> list[dict]:
    from p09.cells import render_wrapper

    pm = ctx["prefix_messages"]
    return [{"role": "system", "content": api._text_of(pm[0].get("content"))},
            {"role": "user", "content": api._text_of(pm[1].get("content"))},
            {"role": "user", "content": render_wrapper(wrapper, summary)},
            {"role": "user", "content": PROBE_PREFACE + C.IDENTITY_PROBES[probe]}]


def _in_bound(messages: list[dict]) -> int:
    return len(json.dumps(messages).encode()) + 16 * len(messages)


async def run_probes(ctxs: list[dict], models: list[C.Model], wrappers: list[str], *,
                     out: Path = C.IDENTITY_JSONL, chat=api.chat, ledger=None,
                     dry_run: bool = False) -> tuple[float, list[dict]]:
    done = last_by(_read_jsonl(out), PROBE_KEY)
    todo = []
    for ctx in ctxs:
        for m in models:
            for w in wrappers:
                if done.get((ctx["ctx_id"], m.slug, w, "mine"), {}).get("status") == "ok":
                    continue
                todo.append((ctx, m, w, probe_messages(ctx, w, ctx["summary"])))
    proj = sum(api.list_cost(t[1], _in_bound(t[3]), PROBE_MAX_TOKENS) for t in todo)
    if dry_run or not todo:
        return proj, []
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    recs: list[dict] = []
    async with api.Batch("identity-probe", projected_usd=proj, ledger=ledger) as b:
        with Path(out).open("a") as f:  # noqa: ASYNC230 - one small append per reply
            async def one(ctx, m, w, msgs) -> None:
                rep = await chat(m, msgs, max_tokens=PROBE_MAX_TOKENS, tools=api.X3_TOOLS)
                b.add(rep)
                rec = {"ctx_id": ctx["ctx_id"], "task_id": ctx["task_id"], "model": m.slug,
                       "wrapper": w, "probe": "mine", "reply": rep.text,
                       "reasoning": rep.reasoning,
                       "parsed": None if rep.error else parse_mine(rep.text),
                       "status": "error" if rep.error else "ok", "error": rep.error,
                       "billed_usd": rep.billed_usd, "charged_usd": api.charge(rep),
                       "finish_reason": rep.finish_reason}
                f.write(json.dumps(rec) + "\n")
                f.flush()
                recs.append(rec)

            await api.gather_limited([one(*t) for t in todo], CONCURRENCY)
    return proj, recs


def main(argv: list[str] | None = None) -> int:
    import sys

    sys.path.insert(0, str(C.PROBE_DIR))
    import run

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--ctx", default="all", help="smoke | rest | all | comma list")
    p.add_argument("--all", action="store_true", help="same as --ctx all")
    p.add_argument("--models", nargs="+", default=["glm-5.3-flash", "deepseek-v4-flash"])
    p.add_argument("--wrappers", nargs="+", default=[w.value for w in C.GLM_WRAPPERS])
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    ctxs = run.select(run.contexts_in_use(), "all" if a.all else a.ctx)
    models = [C.MODELS[s] for s in a.models]
    if not a.dry_run:
        api.load_key()
    proj, recs = asyncio.run(run_probes(ctxs, models, a.wrappers, dry_run=a.dry_run))
    print(f"probe: {len(ctxs)} contexts x {len(models)} models x {len(a.wrappers)} wrappers; "
          f"projected worst case ${proj:.4f}; {len(recs)} calls made")
    if recs:
        tab: dict = {}
        for r in recs:
            k = (r["model"], r["wrapper"])
            tab.setdefault(k, {}).setdefault(r["parsed"], 0)
            tab[k][r["parsed"]] += 1
        for k, v in sorted(tab.items()):
            print(k, v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
