"""Exact token counts for the long SWE-rebench trajectories (P49 offline revision, 2026-10-15).

Takes every trajectory whose char-based estimate (swe_rebench_full.py's 0.216 tok/char) exceeds
90k, tokenizes every message exactly with the DeepSeek-V4-Flash tokenizer (content + tool-call
name/arguments, + tool schemas once), and records the cumulative context at each assistant
request (= everything before that assistant message). Reports how many trajectories cross each
wrapper's 128k-window trigger (Codex/Inspect 115,200; pi 111,616) with >= 1 later assistant turn,
and how many compactions a scaled window allows (recursive simulation with a fixed post-compaction
size; a rough feasibility count, not the wrapper's exact carry-over).
Usage: uv run --group swarm python -I swe_long_pool.py
Writes out/swe_long_pool.json and out/swe_long_rows.jsonl (ids, totals, boundaries; no content)."""
import json
from pathlib import Path

import pyarrow.parquet as pq
from tokenizers import Tokenizer

P = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory/data/P01/raw/trajectories.parquet"
TOK = Tokenizer.from_file("/Users/jacob/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V4-Flash/"
                          "snapshots/60d8d70770c6776ff598c94bb586a859a38244f1/tokenizer.json")
OUT = Path(__file__).parent / "out"
TRIG = {"codex_inspect": 115_200, "pi": 128_000 - 16_384}


def n(s):
    return len(TOK.encode(s, add_special_tokens=False).ids) if s else 0


def msg_tokens(m):
    t = n(m.get("content") or "")
    for tc in (m.get("tool_calls") or []):
        fn = tc.get("function") or {}
        t += n(fn.get("name") or "") + n(fn.get("arguments") or "")
    return t + 4  # role/format overhead


def recursive_k(ctx_at_req, trigger, post):
    """Compactions a window allows: context resets to `post` tokens after each compaction."""
    k, base, offset = 0, 0, 0
    for c in ctx_at_req:
        if c - offset + base >= trigger:
            k += 1
            offset, base = c, post
    return k


f = pq.ParquetFile(P)
rows = []
for rg in range(f.num_row_groups):
    t = f.read_row_group(rg, columns=["trajectory_id", "trajectory", "tools", "resolved",
                                      "exit_status"]).to_pylist()
    for r in t:
        traj = r["trajectory"] or []
        chars = 0
        for m in traj:
            chars += len(m.get("content") or "")
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                chars += len(fn.get("name") or "") + len(fn.get("arguments") or "")
        if chars * 0.216 < 90_000:
            continue
        tools = n(json.dumps(r["tools"])) if r["tools"] is not None else 0
        cum, ctx_at_req = tools, []
        for m in traj:
            if m.get("role") == "assistant":
                ctx_at_req.append(cum)
            cum += msg_tokens(m)
        rows.append(dict(id=r["trajectory_id"], resolved=r["resolved"], exit=r["exit_status"],
                         total=cum, tools=tools, n_req=len(ctx_at_req), ctx_at_req=ctx_at_req))

out = {"n_candidates": len(rows), "tokenizer": "deepseek-ai/DeepSeek-V4-Flash@60d8d707"}
for name, trig in TRIG.items():
    ok = [r for r in rows if any(c >= trig for c in r["ctx_at_req"])]
    out[f"cross_{name}"] = len(ok)
    out[f"cross_{name}_resolved"] = sum(1 for r in ok if r["resolved"] == 1)
    out[f"cross_{name}_exit"] = {}
    for r in ok:
        out[f"cross_{name}_exit"][r["exit"][:40]] = out[f"cross_{name}_exit"].get(r["exit"][:40], 0) + 1
both = [r for r in rows if any(c >= TRIG["codex_inspect"] for c in r["ctx_at_req"])]
out["both_triggers"] = len(both)
for W in (40_000, 48_000, 64_000):
    for post in (8_000, 15_000, 20_000):
        ks = [recursive_k(r["ctx_at_req"], 0.9 * W, post) for r in both]
        out[f"scaled_W{W // 1000}k_post{post // 1000}k_k"] = {str(k): ks.count(k) for k in sorted(set(ks))}
out["total_quantiles"] = sorted(r["total"] for r in rows)[::max(1, len(rows) // 20)]
(OUT / "swe_long_pool.json").write_text(json.dumps(out, indent=1))
with open(OUT / "swe_long_rows.jsonl", "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
print(json.dumps({k: v for k, v in out.items() if k != "total_quantiles"}, indent=1))
