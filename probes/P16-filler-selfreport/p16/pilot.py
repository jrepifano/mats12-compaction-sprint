"""P16 API pilot (plan.md "Automatic rules"): the first 40 items, arms P, L, I, LD and CD, over
OpenRouter `deepseek/deepseek-v4-flash` with P06's V4 recipe (StreamLake pinned, reasoning off,
temperature 0; `p06.api`). Turn 1 is P12's prompt with the arm's sentence; turn 2 is the model's
own reply as the assistant turn, then the ask, plain and prefilled ("{Y} = " as a trailing
assistant message). Output: results/pilot/rows.jsonl (one row per call) and summary.json with
each arm's parse rate (plain and prefilled) and compliance, and the two rule outcomes:
prefilled primary per arm (plain parse rate < 90%) and arm L2 (no misreport arm >= 50%).

The served weights are not established by the metadata (P06 F7), so this pilot sets the rules
only; no headline number comes from it.

    uv run python -m p16.pilot run [--n 40] [--dry-run]   # from the probe dir
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from p06 import api

from . import PROBE
from . import arms as A
from . import readers as R

OUT = PROBE / "results" / "pilot"
ARMS = ("P", "L", "I", "LD", "CD")
MISREPORT = ("L", "I", "LD")
PARSE_MIN = 0.90
COMPLY_MIN = 0.50
TURN1_MAX = 8
RECIPE = api.RECIPES["v4flash"]


def body(msgs: list[dict], max_tokens: int) -> dict:
    b = api.build_body(RECIPE, msgs, 0.0, max_tokens=max_tokens)
    b.pop("logprobs", None)
    b.pop("top_logprobs", None)
    return b


def turn2_msgs(turn1: list[dict], reply: str, item: dict, kind: str) -> list[dict]:
    m = A.ask_messages(turn1, reply, item)
    if kind == "pre":
        m.append({"role": "assistant", "content": A.prefill(item)})
    return m


async def run_item(asker: api.Asker, ex: dict, shots: list[dict], item: dict, arm: str) -> list:
    msgs = A.messages(ex, shots, item, arm)
    d1 = api.parse_response(await asker.post(body(msgs, TURN1_MAX)))
    reply = d1["text"].strip()
    rows = [{"item_id": item["item_id"], "arm": arm, "kind": "turn1", "text": reply,
             "finish": d1["finish_reason"], "usage": d1["usage"], "provider": d1["provider"],
             "target": item["roles"]["answer"]}]
    for kind in ("plain", "pre"):
        d = api.parse_response(await asker.post(body(turn2_msgs(msgs, reply, item, kind),
                                                     A.MAX_NEW_ASK)))
        rows.append({"item_id": item["item_id"], "arm": arm, "kind": f"ask_{kind}",
                     "text": d["text"], "finish": d["finish_reason"], "usage": d["usage"],
                     "provider": d["provider"], "target": item["roles"]["y"], "d": item["d"]})
    return rows


def summarize(rows: list[dict]) -> dict:
    """Per arm and ask: reply classes, parse rate (an integer in the reply), and compliance (the
    paired rise in valid-wrong replies over P on items whose P reply is correct; LD: replies d)."""
    by = {(r["item_id"], r["arm"], r["kind"]): r for r in rows}
    items = sorted({r["item_id"] for r in rows})
    out: dict = {"arms": {}}
    for kind in ("ask_plain", "ask_pre"):
        for arm in ARMS:
            cls = [R.reply_class(by[(i, arm, kind)]["text"], by[(i, arm, kind)]["finish"] ==
                                 "length", by[(i, arm, kind)]["target"])
                   for i in items if (i, arm, kind) in by]
            n = len(cls)
            parse = sum(c in ("correct", "valid_wrong") for c in cls) / n if n else None
            ok_p = [i for i in items if (i, "P", kind) in by and
                    R.first_int(by[(i, "P", kind)]["text"]) == by[(i, "P", kind)]["target"]]
            if arm == "P" or not ok_p:
                comply = None
            elif arm == "LD":
                comply = sum(R.first_int(by[(i, arm, kind)]["text"]) == by[(i, arm, kind)]["d"]
                             for i in ok_p if (i, arm, kind) in by) / len(ok_p)
            else:
                def vw(i, a):
                    r = by.get((i, a, kind))
                    return r is not None and R.reply_class(r["text"], r["finish"] == "length",
                                                           r["target"]) == "valid_wrong"
                comply = (sum(vw(i, arm) for i in ok_p) - sum(vw(i, "P") for i in ok_p)) / len(
                    ok_p)
            out["arms"][f"{arm}|{kind}"] = {
                "n": n, "classes": {c: cls.count(c) for c in R.CLASSES}, "parse_rate": parse,
                "compliance": comply, "n_p_correct": len(ok_p)}
    out["prefilled_primary"] = sorted(
        a for a in ARMS if (out["arms"][f"{a}|ask_plain"]["parse_rate"] or 0) < PARSE_MIN)
    best = max((out["arms"][f"{a}|{k}"]["compliance"] or 0) for a in MISREPORT
               for k in ("ask_plain", "ask_pre"))
    out["max_misreport_compliance"] = best
    out["add_L2"] = best < COMPLY_MIN
    out["turn1_acc"] = {a: sum(R.first_int(by[(i, a, "turn1")]["text"]) == by[(i, a, "turn1")][
        "target"] for i in items if (i, a, "turn1") in by) / len(items) for a in ARMS}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p16.pilot")
    ap.add_argument("cmd", choices=["run", "summary"])
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    rows_path = OUT / "rows.jsonl"
    if a.cmd == "run":
        items = A.load_items()[: a.n]
        by_idx, shots = A.examples_by_idx()
        jobs = [(by_idx[it["idx"]], it, arm) for it in items for arm in ARMS]
        words = sum(len(m["content"].split()) for ex, it, arm in jobs[:5]
                    for m in A.messages(ex, shots, it, arm)) / 5
        proj = 1.5 * len(jobs) * 3 * (2.0 * words * RECIPE.price_in + 16 * RECIPE.price_out)
        print(f"[pilot] {len(jobs)} item-arms x 3 calls, projected ${proj:.3f}", flush=True)
        if a.dry_run:
            return 0
        from emx import spend

        rid = spend.reserve_api("P16", proj, note="p16.pilot")
        asker = api.Asker(api.openrouter_key(), concurrency=32)

        async def go():
            try:
                res = await asyncio.gather(*(run_item(asker, ex, shots, it, arm)
                                             for ex, it, arm in jobs), return_exceptions=True)
            finally:
                await asker.close()
            errs = [r for r in res if isinstance(r, BaseException)]
            if errs:
                print(f"[pilot] {len(errs)} item-arms failed, e.g. {errs[0]!r}", flush=True)
            return [r for rs in res if not isinstance(rs, BaseException) for r in rs]

        rows: list = []
        try:
            rows = asyncio.run(go())
        finally:
            spend.settle_api(rid, total_usd=asker.cost, exp="P16",
                             note=f"p16.pilot {asker.calls} calls", resource=RECIPE.model)
        with rows_path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"[pilot] {asker.calls} calls, ${asker.cost:.4f}", flush=True)
    rows = [json.loads(x) for x in Path(rows_path).read_text().splitlines()]
    s = summarize(rows)
    (OUT / "summary.json").write_text(json.dumps(s, indent=1) + "\n")
    print(json.dumps({k: v for k, v in s.items() if k != "arms"}))
    for k, v in s["arms"].items():
        print(k, v["parse_rate"], v["compliance"], v["classes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
