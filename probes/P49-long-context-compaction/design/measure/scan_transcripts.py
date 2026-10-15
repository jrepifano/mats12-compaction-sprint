"""Scan every agent-transcript JSON in the main checkout and worktrees for logged API usage.

For each file holding a per-call usage list (model_calls / api_calls), record: group (probe/
results dir + model + arm), number of calls, max prompt tokens (proxy for final context), the
last call's prompt+completion, reasoning/cached totals, whether messages store reasoning blocks,
and whether tool results look truncated. Writes out/transcripts.jsonl and prints a group table.
Usage: python3 -I scan_transcripts.py
"""
import json, os, statistics, sys, hashlib
from pathlib import Path
from collections import defaultdict

MAIN = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory")
ROOTS = []
for base in [MAIN] + sorted((MAIN / ".claude/worktrees").iterdir()):
    for sub in ["probes", "results"]:
        p = base / sub
        if p.is_dir():
            ROOTS.append(p)
OUT = Path(__file__).parent / "out"

def calls_of(d):
    for k in ("model_calls", "api_calls"):
        v = d.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return k, v
    return None, None

def g(c, *ks):
    for k in ks:
        if c.get(k) is not None:
            return c[k]
    return 0

def has_reasoning(msgs):
    n = 0
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "reasoning" for b in c):
            n += 1
        elif m.get("reasoning") or m.get("reasoning_content") or m.get("reasoning_details"):
            n += 1
    return n

def group_of(path):
    rel = path
    parts = rel.parts
    # find 'probes/<P>/results/...' or 'results/<X>/...'
    try:
        i = parts.index("probes")
        head = parts[i + 1]
        rest = parts[i + 2:-1]
    except ValueError:
        i = parts.index("results")
        head = "results/" + parts[i + 1]
        rest = parts[i + 2:-1]
    wt = ""
    if ".claude" in parts:
        wt = "[wt:" + parts[parts.index("worktrees") + 1] + "] "
    rest = [r for r in rest if r not in ("results", "episodes", "chains")]
    return wt + head, "/".join(rest[:2])

seen = set()
rows = []
for root in ROOTS:
    for dp, dn, fn in os.walk(root):
        if "/.venv" in dp or "/node_modules" in dp or "/tests" in dp:
            continue
        for f in fn:
            if not f.endswith(".json"):
                continue
            p = Path(dp) / f
            try:
                st = p.stat()
                if st.st_size < 3000:
                    continue
                with open(p, "rb") as fh:
                    raw = fh.read()
                if b"model_calls" not in raw and b"api_calls" not in raw:
                    continue
                h = hashlib.sha1(raw).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                d = json.loads(raw)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            k, calls = calls_of(d)
            if not calls:
                continue
            ins = [g(c, "input_tokens", "in", "prompt_tokens") or 0 for c in calls]
            outs = [g(c, "output_tokens", "out", "completion_tokens") or 0 for c in calls]
            if not any(ins):
                continue
            rsn = [g(c, "reasoning_tokens") or 0 for c in calls]
            cch = [g(c, "cached_tokens", "cache_read") or 0 for c in calls]
            msgs = d.get("messages")
            model = (d.get("meta") or {}).get("model_id") or d.get("model") or (d.get("meta") or {}).get("model") or ""
            trunc = None
            if isinstance(d.get("calls"), list) and d["calls"] and isinstance(d["calls"][0], dict) and "truncated" in d["calls"][0]:
                trunc = sum(1 for c in d["calls"] if c.get("truncated"))
            grp, sub = group_of(p)
            i_max = max(range(len(ins)), key=lambda i: ins[i])
            rows.append(dict(path=str(p), group=grp, sub=sub, model=model, key=k, n_calls=len(calls),
                             n_turns=d.get("n_turns") or d.get("turns"), max_in=max(ins),
                             final_ctx=ins[i_max] + outs[i_max], last_in=ins[-1],
                             sum_in=sum(ins), sum_out=sum(outs), sum_reason=sum(rsn), sum_cached=sum(cch),
                             n_msgs=len(msgs) if isinstance(msgs, list) else None,
                             reasoning_msgs=has_reasoning(msgs), truncated_calls=trunc,
                             n_tool_calls=len(d["calls"]) if isinstance(d.get("calls"), list) else None))

OUT.mkdir(exist_ok=True)
with open(OUT / "transcripts.jsonl", "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")

def q(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0
    k = (len(xs) - 1) * p
    lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

by = defaultdict(list)
for r in rows:
    by[(r["group"], r["model"])].append(r)
print(f"{'group':48s} {'model':28s} {'n':>5s} {'calls_med':>9s} {'ctx_med':>8s} {'ctx_p90':>8s} {'ctx_max':>8s} {'>64k':>5s} {'>100k':>5s} {'>128k':>5s} {'>160k':>5s} {'>200k':>5s} {'rsnmsg':>6s}")
for (grp, model), rs in sorted(by.items()):
    c = [r["final_ctx"] for r in rs]
    print(f"{grp[:48]:48s} {model[:28]:28s} {len(rs):5d} {statistics.median([r['n_calls'] for r in rs]):9.0f} {q(c,.5)/1e3:7.1f}k {q(c,.9)/1e3:7.1f}k {max(c)/1e3:7.1f}k "
          + " ".join(f"{sum(1 for x in c if x > t):5d}" for t in (64e3, 100e3, 128e3, 160e3, 200e3))
          + f" {sum(1 for r in rs if r['reasoning_msgs'])/len(rs):6.2f}")
print("total transcripts", len(rows))
