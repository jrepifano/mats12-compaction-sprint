# ruff: noqa  (kept as run: the import order sets sys.path for p06)
"""Prefill pilot (log.md, smoke): does an assistant `Answer:` prefill stop visible CoT in base arms?
Run once from the probe dir with `uv run python p32/pilot_prefill.py` -> results/pilot_prefill.jsonl."""
import asyncio, sys
sys.path.insert(0, '.')
import p32
from p06 import api
from p32 import prompts, run
from p32.items import load_all
fams = load_all()
jobs = []
for fam in ("varbind", "chain"):
    ev, sh = fams[fam]
    for e in ev[:20]:
        for c in ("0|-|base", "10|near|base", "10|far|base", "5|near|base", "0|-|dots", "10|near|dots_before"):
            m = prompts.messages(e, sh, c)
            m[-1]["content"] = m[-1]["content"].removesuffix("\n\nAnswer:")
            m.append({"role": "assistant", "content": "Answer:"})
            j = run.job(e, sh, c); j["messages"] = m; jobs.append(j)
out = run.RES / "pilot_prefill.jsonl"
async def go():
    a = api.Asker(api.openrouter_key(), concurrency=32)
    try:
        await api.run_jobs(a, run.RECIPE, jobs, out, "p32pilotprefill")
    finally:
        await a.close()
asyncio.run(go())
run.reconcile()
from collections import defaultdict
by = defaultdict(list)
for r in api.latest_ok(out).values(): by[(r["bank"], r["arm"])].append(r)
for k, rs in sorted(by.items()):
    print(k, len(rs), "acc", sum(r["correct"] for r in rs)/len(rs), "invalid", sum(not r["valid"] for r in rs), [r["raw_text"][:12] for r in rs[:4]])
