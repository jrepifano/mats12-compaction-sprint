"""Tokenizer sensitivity of the trigger: DeepSeek-V4-Flash vs a Qwen3 tokenizer (Qwen3.6-27B, the
nearest cached relative of the trajectories' Qwen3-Coder-480B agent) on the long pool.
Usage: uv run --group swarm python -I tokenizer_check.py  -> out/tokenizer_check.json"""
import glob
import json
from pathlib import Path

import pyarrow.parquet as pq
from tokenizers import Tokenizer

P = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory/data/P01/raw/trajectories.parquet"
DS = Tokenizer.from_file(glob.glob("/Users/jacob/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V4-Flash/snapshots/*/tokenizer.json")[0])
QW = Tokenizer.from_file(glob.glob("/Users/jacob/.cache/huggingface/hub/models--Qwen--Qwen3.6-27B/snapshots/*/tokenizer.json")[0])
OUT = Path(__file__).parent / "out"
pool = {}
for line in open(OUT / "swe_long_rows.jsonl"):
    r = json.loads(line)
    pool[r["id"]] = r
f = pq.ParquetFile(P)
res = []
for rg in range(f.num_row_groups):
    for r in f.read_row_group(rg, columns=["trajectory_id", "trajectory", "tools"]).to_pylist():
        if r["trajectory_id"] not in pool:
            continue
        parts = [json.dumps(r["tools"])]
        for m in r["trajectory"]:
            parts.append(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                parts.append((fn.get("name") or "") + (fn.get("arguments") or ""))
        ds = sum(len(DS.encode(p, add_special_tokens=False).ids) for p in parts if p)
        qw = sum(len(QW.encode(p, add_special_tokens=False).ids) for p in parts if p)
        res.append((r["trajectory_id"], ds, qw))
ratio = sum(q for _, _, q in res) / sum(d for _, d, _ in res)
over = sum(1 for i, d, q in res if pool[i]["total"] * q / d >= 115_200 + 0)
out = dict(n=len(res), qwen_over_ds=ratio, pool_total_ge_115k_under_qwen_scaling=over)
(OUT / "tokenizer_check.json").write_text(json.dumps(out, indent=1))
print(out)
