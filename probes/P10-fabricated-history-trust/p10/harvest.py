"""Arm H harvest (plan.md "Setup"): DeepSeek V4 Flash compacts P07's 27 contexts under the
Codex and pi-mono prompts, exactly as P01 sent them (`p01.render.summarizer_request`, pi-mono's
serialization), with fresh seeds; summaries that pass P01's invented-output check against that
context's own transcript are fabrications.

Rounds: each round sends ROUND_SEEDS seeds x 2 prompts for every context that has no
fabrication yet. Stops when every context has one, or after MAX_ATTEMPTS calls (plan rule).
Every call goes through `emx.llm_batch.run_batch(exp="P10")` (resumable JSONL, one reservation
per round). Temperature 1.0 (log.md: P01's temperature 0 varies little across seeds).

    uv run python probes/P10-fabricated-history-trust/p10/harvest.py run [--max-attempts N]
    uv run python probes/P10-fabricated-history-trust/p10/harvest.py select
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p10 import common as K

OUT = K.RESULTS / "harvest" / "dsk.jsonl"
SELECTED = K.DATA / "harvest_selected.jsonl"
HARNESSES = ("codex", "pimono")
WRAPPER_OF = {"codex": "codex", "pimono": "pi_mono"}   # P07 resume wrapper per prompt
ROUND_SEEDS = 4
MAX_ATTEMPTS = 2000
MAX_TOKENS = 16000
TEMPERATURE = 1.0
CONCURRENCY = 24


def contexts() -> list[dict]:
    return K.load_jsonl(K.P07_CONTEXTS)


def classify(ctx: dict, text: str, view: str | None = None) -> dict:
    """Fabrication check for one summary of one context."""
    view = view if view is not None else K.p07_view(ctx)
    marker = K.first_marker(text)
    fab = K.is_fabrication(text, view)
    out = {"marker": marker is not None, "fabrication": fab}
    if fab:
        removed, block = K.split_pair(text)
        out.update(removed_chars=len(removed), block_chars=len(block),
                   n_invented=len(K.invented_blocks(text, view)),
                   block_claims_pass=K.block_claims_pass(block),
                   usable=len(removed) >= K.MIN_REMOVED_CHARS)
    return out


def all_rows() -> list[dict]:
    """Rows of every harvester process (dsk.jsonl, dsk_<tag>.jsonl)."""
    out = []
    for p in sorted(OUT.parent.glob("dsk*.jsonl")):
        if ".usage" in p.name:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:   # another process may be mid-append: skip a torn line
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "seed" in r:
                out.append(r)
    return out


def found(rows: list[dict], ctxs: dict[str, dict]) -> dict[str, list[dict]]:
    """ctx_id -> usable fabrications, in (seed, harness) order."""
    views = {c: K.p07_view(x) for c, x in ctxs.items()}
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["seed"], r["item_id"])):
        if r["status"] != "ok" or not r.get("text"):
            continue
        cid, h = r["item_id"].rsplit("|", 1)
        c = classify(ctxs[cid], r["text"], views[cid])
        if c["fabrication"] and c.get("usable"):
            out.setdefault(cid, []).append({**c, "ctx_id": cid, "harness": h,
                                            "seed": r["seed"], "text": r["text"]})
    return out


def run(max_attempts: int, tag: str = "", seed_base: int = K.SEED,
        stop_utc: str | None = None) -> None:
    from p01 import render

    from emx import llm_batch

    ctxs = {c["ctx_id"]: c for c in contexts()}
    out = OUT.with_name(f"dsk_{tag}.jsonl") if tag else OUT
    k = 0
    while True:
        rows = all_rows()
        have = found(rows, ctxs)
        # The limit is checked between rounds against every process's completed rows; in-flight
        # rounds are not counted, so parallel harvesters can each overshoot by up to one round.
        n_sent = len(rows)
        todo = [c for c in ctxs if c not in have]
        print(f"[P10 harvest] round {k}: {len(have)}/{len(ctxs)} contexts have a fabrication; "
              f"{n_sent} calls so far", flush=True)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not todo or n_sent >= max_attempts or (stop_utc and now >= stop_utc):
            break
        reqs = []
        for s in range(ROUND_SEEDS):
            seed = seed_base + k * ROUND_SEEDS + s
            for cid in todo:
                t = K.p01_transcript(ctxs[cid])
                for h in HARNESSES:
                    system, user = render.summarizer_request(t, h)
                    reqs.append({"item_id": f"{cid}|{h}", "prompt": user, "system": system,
                                 "seed": seed})
        reqs = reqs[: max(0, max_attempts - n_sent)]
        llm_batch.run_batch("dsk", reqs, out, exp=K.EXP, note=f"P10:harvest{tag}:r{k}",
                            max_tokens=MAX_TOKENS, temperature=TEMPERATURE, json_mode=False,
                            concurrency=CONCURRENCY, timeout_s=600, bytes_per_token=1.5)
        k += 1


def select() -> None:
    """One fabrication per context: the first whose block claims a pass, else the first."""
    ctxs = {c["ctx_id"]: c for c in contexts()}
    rows = all_rows()
    have = found(rows, ctxs)
    SELECTED.parent.mkdir(parents=True, exist_ok=True)
    with SELECTED.open("w", encoding="utf-8") as f:
        for cid in [c for c in ctxs if c in have]:
            cands = have[cid]
            pick = next((x for x in cands if x["block_claims_pass"]), cands[0])
            f.write(json.dumps({**pick, "n_candidates": len(cands)}) + "\n")
    ok = [r for r in rows if r["status"] == "ok"]
    n_marker = sum(1 for r in ok if K.first_marker(r["text"] or "") is not None)
    n_fab = sum(len(v) for v in have.values())
    print(json.dumps({"calls": len(rows), "ok": len(ok), "marker": n_marker,
                      "usable_fabrications": n_fab, "contexts": len(have),
                      "contexts_with_pass_block": sum(
                          any(x["block_claims_pass"] for x in v) for v in have.values())}))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    r.add_argument("--tag", default="", help="a second process writes dsk_<tag>.jsonl")
    r.add_argument("--seed-base", type=int, default=K.SEED)
    r.add_argument("--stop-utc", default=None, help="no new round after this UTC time")
    sub.add_parser("select")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run(a.max_attempts, a.tag, a.seed_base, a.stop_utc)
    select()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
