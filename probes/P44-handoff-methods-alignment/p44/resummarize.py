"""P44: (re)generate the prefixes' gen-1 summaries S1 and S1n outside the sandbox (log.md 05:25).

On Together, GLM 5.3 Flash sometimes spends the whole 16,000-token budget reasoning on the pi-mono
summary call and returns no text (7 of 90 prefix summaries, the retry too). Summaries are pure API
calls on the prefix's messages, so they are regenerated here with the summariser settings in
`contracts.SUMMARY_GENERATE`, the same request text as agent.py (`handoff.summary_request`), and
up to `contracts.SUMMARY_ATTEMPTS` attempts. The previous record is kept as `<key>_v0`.

  uv run --group swarm python probes/P44-handoff-methods-alignment/p44/resummarize.py \
      --model glm-tg [--only lcbhard_98|r0,...] [--failed-only] [--dry-run] [--test]

--test regenerates in memory only and prints status counts (nothing written; spend recorded).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src", _PROBE.parents[1] / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import asyncio
import json

from p44 import contracts as C
from p44 import handoff as H


async def one_summary(model, sys_text: str, user_text: str, gen: dict) -> dict:
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser, GenerateConfig

    attempts = []
    cfg = GenerateConfig(max_tokens=C.SUMMARY_MAX_TOKENS, max_retries=2,
                         **gen)
    for _ in range(C.SUMMARY_ATTEMPTS):
        try:
            out = await model.generate([ChatMessageSystem(content=sys_text),
                                        ChatMessageUser(content=user_text)], config=cfg)
        except Exception as e:  # noqa: BLE001
            attempts.append({"error": repr(e)[:500]})
            continue
        text = out.message.text or ""
        u = out.usage.model_dump() if out.usage else None
        attempts.append({"status": "ok" if text.strip() else "empty", "usage": u})
        if text.strip():
            return {"status": "ok", "reply": text, "usage": u, "attempts": attempts,
                    "generate": gen, "source": "resummarize.py"}
    return {"status": "empty", "reply": "", "attempts": attempts,
            "generate": gen, "source": "resummarize.py"}


def usd(rec: dict, m: C.Model) -> float:
    tot = 0.0
    for a in rec.get("attempts") or []:
        u = a.get("usage") or {}
        cr = u.get("input_tokens_cache_read") or 0
        tot += (u.get("input_tokens") or 0) * m.in_ceiling / 1e6 + cr * m.in_ceiling / 1e6 \
            + (u.get("output_tokens") or 0) * m.out_ceiling / 1e6      # ceiling prices: an upper bound
    return tot


async def main_async(a) -> int:
    from inspect_ai.model import get_model

    from emx import spend

    m = C.MODELS[C.MODEL_ARG[a.model]]
    model = get_model(f"openrouter/{m.model_id}", provider=m.provider, **m.generate)
    paths = sorted((C.PREFIXES_DIR / m.slug).glob("*.json"))
    jobs = []
    for p in paths:
        art = json.loads(p.read_text())
        pid = art["meta"]["prefix_id"]
        if art.get("ended") or art.get("error") or (a.only and pid not in a.only):
            continue
        keys = ((("summary_s1_default", []),) if a.default_effort else
                (("summary_s1", []), ("summary_s1n", art.get("notes_messages") or [])))
        for key, extra in keys:
            if a.failed_only and (art.get(key) or {}).get("status") == "ok":
                continue
            dumps = art["messages"][1:] + extra
            jobs.append((p, key, dumps))
    gen = {} if a.default_effort else C.SUMMARY_GENERATE
    print(f"{len(jobs)} summaries to make with {gen}, "
          f"{C.SUMMARY_ATTEMPTS} attempts", flush=True)
    if a.dry_run or not jobs:
        return 0
    spend.check_total(0.02 * len(jobs) * C.SUMMARY_ATTEMPTS, "judge", exp=C.EXP)
    sem = asyncio.Semaphore(16)

    async def run(job):
        async with sem:
            sys_text, user_text = H.summary_request(job[2])
            return job, await one_summary(model, sys_text, user_text, gen)

    results = await asyncio.gather(*(run(j) for j in jobs))
    cost = sum(usd(r, m) for _, r in results)
    counts: dict[str, int] = {}
    for (p, key, _), r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        n_att = len(r["attempts"])
        print(f"{p.name} {key} {r['status']} attempts={n_att}", flush=True)
    spend.record("judge", f"openrouter:{m.model_id}", len(results), round(cost, 6),
                 note=f"P44 resummarize {m.slug} ({'test' if a.test else 'write'}): "
                 f"{len(results)} summaries {counts}, ceiling-price bound", exp=C.EXP)
    print(f"P44_RESUMMARIZE {counts} usd<={cost:.4f}", flush=True)
    if a.test:
        return 0
    by_path: dict[Path, list] = {}
    for (p, key, _), r in results:
        by_path.setdefault(p, []).append((key, r))
    for p, items in by_path.items():
        art = json.loads(p.read_text())
        for key, r in items:
            art[f"{key}_v0"] = art.get(key)
            art[key] = r
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(art, indent=1, default=str))
        tmp.replace(p)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(C.MODEL_ARG))
    ap.add_argument("--only", type=lambda s: set(s.split(",")), default=None)
    ap.add_argument("--failed-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--default-effort", action="store_true",
                    help="S1 only, at the model's default effort, into summary_s1_default (arm Cd)")
    a = ap.parse_args(argv)
    import run as R

    R.load_key()
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
