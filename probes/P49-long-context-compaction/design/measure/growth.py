"""Per-turn context growth, split by content type, for selected transcript sets.

Tokens are counted with the DeepSeek-V4-Flash tokenizer (HF cache, snapshot 60d8d707) as a
proxy for every model (GLM/Gemini/Qwen tokenizers differ by ~10-20%); each set also reports the
ratio of the logged API prompt tokens at the last call to the tokenizer count of the messages,
so the proxy can be rescaled. A "turn" = one assistant message. Increment of turn i = tokens of
assistant message i (reasoning + text + tool-call args) + the tool results that answer it +
any user message before the next assistant message.
Usage: uv run --project <worktree> python -I growth.py
Writes out/growth.json and prints a table.
"""
import glob, json, statistics, sys
from pathlib import Path
from tokenizers import Tokenizer

TOK = Tokenizer.from_file("/Users/jacob/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V4-Flash/"
                          "snapshots/60d8d70770c6776ff598c94bb586a859a38244f1/tokenizer.json")
M = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory"
W = M + "/.claude/worktrees/x3-build"
SETS = {
    "P44 GLM R (no handoff)": M + "/probes/P44-handoff-methods-alignment/results/chains/glm-5.3-flash-tg/R/none/*.json",
    "P44 GLM PILOT (no handoff, Relace)": M + "/probes/P44-handoff-methods-alignment/results/chains/glm-5.3-flash/PILOT/**/*.json",
    "P44 DS R (no handoff)": M + "/probes/P44-handoff-methods-alignment/results/chains/deepseek-v4-flash-bd/R/**/*.json",
    "P44 DS PILOT": M + "/probes/P44-handoff-methods-alignment/results/chains/deepseek-v4-flash/PILOT/**/*.json",
    "X3b GLM main": M + "/results/X3b/glm/episodes/main/**/*.json",
    "X3b Gemini main": M + "/results/X3b/gemini/episodes/main/**/*.json",
    "X3 all models: GLM+DS+Qwen+Gemini (x3-build wt)": W + "/results/X3/**/episodes/**/*.json",
    "P41 GLM arm A": M + "/probes/P41-grader-belief-atrisk/results/**/A/**/*.json",
    "P23 GLM safety full": M + "/probes/P23-compaction-decisions-vs-cues/results/episodes/glm-5.3-flash/safety/full/*.json",
    "P23 DS safety full": M + "/probes/P23-compaction-decisions-vs-cues/results/episodes/deepseek-v4-flash/safety/full/*.json",
    "P38 GLM a_full": M + "/probes/P38-compaction-live-fact/results/episodes/glm-5.3-flash/a_full/*.json",
    "P38 DS a_full": M + "/probes/P38-compaction-live-fact/results/episodes/deepseek-v4-flash/a_full/*.json",
    "P47 gpt-oss-120b": M + "/probes/P47-grader-belief-probe/results/gpt-oss-120b/episodes/**/*.json",
    "P47 qwen3.6-27b": M + "/probes/P47-grader-belief-probe/results/qwen3.6-27b/episodes/**/*.json",
    "P01 slice A (SWE-rebench, Qwen3-Coder-480B)": M + "/data/P01/slice_a.jsonl",
}

def as_str(x):
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    return json.dumps(x)

def parse_msg(m):
    """-> role, dict(category -> text)"""
    role = m.get("role")
    cat = {"reasoning": "", "text": "", "args": "", "tool": "", "user": "", "system": ""}
    c = m.get("content", m.get("text"))
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "reasoning":
                cat["reasoning"] += as_str(b.get("reasoning") or b.get("summary"))
            elif b.get("type") == "text":
                cat["text"] += as_str(b.get("text"))
            else:
                cat["text"] += as_str(b.get("text") or "")
    else:
        cat["text"] += as_str(c)
    r = m.get("_reasoning") or m.get("reasoning") or m.get("reasoning_content")
    if r and not cat["reasoning"]:
        cat["reasoning"] = as_str(r)
    tcs = m.get("tool_calls")
    if isinstance(tcs, str):
        cat["args"] += tcs
    elif isinstance(tcs, list):
        for t in tcs:
            if not isinstance(t, dict):
                continue
            f = t.get("function")
            if isinstance(f, dict):
                cat["args"] += as_str(f.get("name")) + as_str(f.get("arguments"))
            else:
                cat["args"] += as_str(f) + as_str(t.get("arguments") or t.get("args"))
    if role == "tool":
        cat["tool"], cat["text"] = cat["text"], ""
    elif role == "user":
        cat["user"], cat["text"] = cat["text"], ""
    elif role == "system":
        cat["system"], cat["text"] = cat["text"], ""
    return role, cat

def ntok(s):
    return len(TOK.encode(s, add_special_tokens=False).ids) if s else 0

def load(pattern):
    if pattern.endswith(".jsonl"):
        for line in open(pattern):
            d = json.loads(line)
            yield d["tid"], d["messages"], None
        return
    for p in sorted(glob.glob(pattern, recursive=True)):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("messages"), list):
            continue
        msgs = d["messages"]
        if d.get("initial_messages") and d.get("n_initial") is not None and len(msgs) < d["n_initial"]:
            msgs = d["initial_messages"] + msgs
        calls = d.get("model_calls") or d.get("api_calls") or []
        api = None
        if calls:
            ins = [(c.get("input_tokens") or c.get("in") or 0) + (c.get("output_tokens") or c.get("out") or 0) for c in calls]
            api = max(ins)
        yield p, msgs, api

def q(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p; lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

results = {}
for name, pat in SETS.items():
    per = []
    for tid, msgs, api in load(pat):
        parsed = [parse_msg(m) for m in msgs if isinstance(m, dict)]
        toks = [(r, {k: ntok(v) for k, v in c.items()}) for r, c in parsed]
        base = 0; i = 0
        while i < len(toks) and toks[i][0] != "assistant":
            base += sum(toks[i][1].values()); i += 1
        incs = []; cats = {"reasoning": 0, "text": 0, "args": 0, "tool": 0, "user": 0}
        cur = None
        for r, t in toks[i:]:
            if r == "assistant":
                if cur is not None:
                    incs.append(cur)
                cur = 0
            v = sum(t.values()); cur = (cur or 0) + v
            for k in cats:
                cats[k] += t.get(k, 0)
        if cur is not None:
            incs.append(cur)
        total = base + sum(incs)
        per.append(dict(tid=str(tid), turns=len(incs), base=base, total=total, api_final=api,
                        incs=incs, cats=cats))
    if not per:
        print("EMPTY", name, pat); continue
    allinc = [x for p in per for x in p["incs"]]
    cats = {k: sum(p["cats"][k] for p in per) for k in per[0]["cats"]}
    ct = sum(cats.values()) or 1
    ratios = [p["api_final"] / p["total"] for p in per if p["api_final"] and p["total"]]
    res = dict(n=len(per), turns_med=statistics.median(p["turns"] for p in per),
               turns_p90=q([p["turns"] for p in per], .9), turns_max=max(p["turns"] for p in per),
               base_med=statistics.median(p["base"] for p in per),
               total_med=q([p["total"] for p in per], .5), total_p90=q([p["total"] for p in per], .9),
               total_max=max(p["total"] for p in per),
               inc_mean=statistics.mean(allinc) if allinc else 0, inc_med=statistics.median(allinc) if allinc else 0,
               inc_p90=q(allinc, .9),
               per_transcript_mean_inc_med=statistics.median([statistics.mean(p["incs"]) for p in per if p["incs"]]),
               share={k: round(v / ct, 3) for k, v in cats.items()},
               mean_per_turn={k: round(v / max(1, len(allinc)), 1) for k, v in cats.items()},
               api_over_tok_med=statistics.median(ratios) if ratios else None,
               over={str(t): sum(1 for p in per if p["total"] > t) for t in (64000, 100000, 128000, 160000, 200000)})
    results[name] = res
    print(f"\n## {name}  (n={res['n']})")
    print(json.dumps(res))
json.dump(results, open(Path(__file__).parent / "out/growth.json", "w"), indent=1)
