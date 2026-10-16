"""Length distribution of all 67,074 nebius/SWE-rebench-openhands-trajectories (rev 35455389ab51)
in data/P01/raw/trajectories.parquet (main checkout). Counts characters of every message
(content + tool-call name/arguments) per trajectory, and tokenizes an exact sample of 400
trajectories with the DeepSeek-V4-Flash tokenizer to calibrate chars->tokens. Tool schemas
(the `tools` column) are counted separately (sent once per call as part of the prefix).
Usage: uv run --project <worktree> --group swarm python -I swe_rebench_full.py
Writes out/swe_rebench_full.json (incl. Gate 0 pool counts) and out/swe_rebench_rows.jsonl."""
import json, random, statistics
from pathlib import Path
import pyarrow.parquet as pq
from tokenizers import Tokenizer

P = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory/data/P01/raw/trajectories.parquet"
TOK = Tokenizer.from_file("/Users/jacob/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V4-Flash/"
                          "snapshots/60d8d70770c6776ff598c94bb586a859a38244f1/tokenizer.json")
f = pq.ParquetFile(P)
rng = random.Random(20261014)
rows = []
sample_tok = []
for rg in range(f.num_row_groups):
    t = f.read_row_group(rg, columns=["trajectory_id", "trajectory", "tools", "resolved", "exit_status"]).to_pylist()
    for r in t:
        traj = r["trajectory"] or []
        parts = []
        n_asst = 0
        roles = {}
        for m in traj:
            role = m.get("role"); roles[role] = roles.get(role, 0) + 1
            if role == "assistant":
                n_asst += 1
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join((b.get("text") or "") if isinstance(b, dict) else str(b) for b in c)
            parts.append(c or "")
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                parts.append((fn.get("name") or "") + (fn.get("arguments") or ""))
        chars = sum(len(p) for p in parts)
        tools_chars = len(json.dumps(r["tools"])) if r["tools"] is not None else 0
        rec = dict(id=r["trajectory_id"], chars=chars, tools_chars=tools_chars, turns=n_asst,
                   msgs=len(traj), resolved=r["resolved"], exit=r["exit_status"])
        rows.append(rec)
        if rng.random() < 400 / 67074:
            toks = sum(len(TOK.encode(p, add_special_tokens=False).ids) for p in parts if p)
            ttoks = len(TOK.encode(json.dumps(r["tools"]), add_special_tokens=False).ids) if r["tools"] is not None else 0
            sample_tok.append((chars, toks, tools_chars, ttoks))
ratio = sum(s[1] for s in sample_tok) / sum(s[0] for s in sample_tok)
tratio = sum(s[3] for s in sample_tok) / max(1, sum(s[2] for s in sample_tok))
for r in rows:
    r["tok_est"] = r["chars"] * ratio + r["tools_chars"] * tratio
def q(xs, p):
    xs = sorted(xs); k = (len(xs) - 1) * p; lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
tok = [r["tok_est"] for r in rows]; turns = [r["turns"] for r in rows]
out = dict(n=len(rows), tokens_per_char=ratio, tool_schema_tokens_per_char=tratio, n_sample=len(sample_tok),
           tok_med=q(tok, .5), tok_p90=q(tok, .9), tok_p99=q(tok, .99), tok_max=max(tok),
           turns_med=q(turns, .5), turns_p90=q(turns, .9), turns_max=max(turns),
           turns_hist={str(k): sum(1 for t in turns if t >= k) for k in (40, 60, 80, 100, 101, 150, 200)},
           over={str(t): sum(1 for x in tok if x > t) for t in (64000, 100000, 128000, 160000, 200000)},
           over_resolved={str(t): sum(1 for r in rows if r["tok_est"] > t and r["resolved"] == 1) for t in (64000, 100000, 128000)},
           exit_counts={e: sum(1 for r in rows if r["exit"] == e) for e in set(r["exit"] for r in rows)},
           tools_tok_med=q([r["tools_chars"] * tratio for r in rows], .5))
long = sorted(rows, key=lambda r: -r["tok_est"])[:5]
out["top5"] = [(r["id"], round(r["tok_est"]), r["turns"]) for r in long]
for W in (64000, 100000, 128000):
    for c in (20000, 40000):
        for k in (1, 2, 3):
            out.setdefault("pool", {})[f"W{W//1000}k_c{c//1000}k_k{k}"] = sum(1 for x in tok if x >= W + (k - 1) * (W - c))
with open(Path(__file__).parent / "out/swe_rebench_rows.jsonl", "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
json.dump(out, open(Path(__file__).parent / "out/swe_rebench_full.json", "w"), indent=1)
print(json.dumps(out, indent=1))
