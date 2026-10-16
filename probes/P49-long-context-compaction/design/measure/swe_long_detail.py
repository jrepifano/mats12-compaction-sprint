"""Per-trajectory detail for the 193 SWE-rebench trajectories that reach the 115.2k trigger.

For each: the turn where context first reaches 115,200 (L, Codex/Inspect) and 40,000 (S1), and,
at each cut, whether P01's code truth exists (latest suite run classified, latest error line,
edited non-test files), the age of the latest error/test in tokens, pi's summarised span with
2,000-char tool truncation (tokens), and any marker of an earlier condensation in the harness.
Uses P01's normalisation and truth code unchanged (probes/P01-compaction-fidelity/p01).
Usage: uv run --group swarm python -I swe_long_detail.py   -> out/swe_long_detail.json"""
import json
import statistics
import sys
from pathlib import Path

import pyarrow.parquet as pq
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "probes/P01-compaction-fidelity"))
from p01 import common, slice_a  # noqa: E402

P = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory/data/P01/raw/trajectories.parquet"
TOK = Tokenizer.from_file("/Users/jacob/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V4-Flash/"
                          "snapshots/60d8d70770c6776ff598c94bb586a859a38244f1/tokenizer.json")
OUT = Path(__file__).parent / "out"
ids = {}
for line in open(OUT / "swe_long_rows.jsonl"):
    r = json.loads(line)
    if any(c >= 115_200 for c in r["ctx_at_req"]):
        ids[r["id"]] = r
MARKERS = ("CONDENSATION", "<PREVIOUS SUMMARY>", "AgentCondensationObservation", "condensed",
           "summary of the conversation", "Previous summary")


def ntok(s):
    return len(TOK.encode(s, add_special_tokens=False).ids) if s else 0


def mtok(m, cap=None):
    t = m.get("text") or ""
    if cap and m["role"] == "tool" and len(t) > cap:
        t = t[:cap]
    n = ntok(t) + 4
    for c in m.get("tool_calls") or []:
        n += ntok(c.get("name") or "") + ntok(json.dumps(c.get("args") or {}))
    return n


def at_cut(msgs, cut_idx, toks):
    """Truth and pi view for a compaction before assistant message msgs[cut_idx]."""
    prefix = msgs[:cut_idx]
    truth = common.prefix_truth(prefix)
    edits = slice_a.file_edits(prefix)
    files = slice_a.checklist_files(edits)
    cum = 0
    tail_start = cut_idx
    for i in range(cut_idx - 1, -1, -1):  # pi keeps >= 20k tokens of tail, cut at a turn start
        cum += toks[i]
        if cum >= 20_000 and msgs[i]["role"] in ("user", "assistant"):
            tail_start = i
            break
    span = msgs[1:tail_start]
    pi_span = sum(mtok(m, 2000) for m in span)
    err = truth.get("last_error")
    return dict(suite_status=truth["last_test"]["status"],
                script_status=truth["last_script"]["status"], has_error=bool(err),
                error_age=err["age"] if err else None, n_files=len(files), pi_span_tok=pi_span,
                pi_tail_tok=sum(toks[tail_start:cut_idx]), truth_keys=sorted(truth.keys()))


f = pq.ParquetFile(P)
out = []
for rg in range(f.num_row_groups):
    t = f.read_row_group(rg, columns=slice_a.ROW_COLUMNS).to_pylist()
    for row in t:
        if row["trajectory_id"] not in ids:
            continue
        r = ids[row["trajectory_id"]]
        msgs = slice_a.normalize_messages(row["trajectory"])
        toks = [mtok(m) for m in msgs]
        cum, first = r["tools"], {}
        for i, m in enumerate(msgs):
            if m["role"] == "assistant":
                for name, trig in (("S1", 40_000), ("L", 115_200)):
                    if name not in first and cum >= trig:
                        first[name] = i
            cum += toks[i]
        users = [m.get("text") or "" for m in msgs if m["role"] == "user"]
        cond = any(k in (m.get("text") or "") for m in msgs for k in MARKERS if m["role"] != "system")
        rec = dict(id=row["trajectory_id"], repo=row["repo"], exit=row["exit_status"][:30],
                   resolved=row["resolved"], n_user=len(users), user_tok=sum(ntok(u) for u in users),
                   condensation_marker=cond, total=r["total"])
        for name, idx in first.items():
            rec[name] = at_cut(msgs, idx, toks)
        out.append(rec)

summ = dict(n=len(out), condensation_marker=sum(o["condensation_marker"] for o in out),
            n_user_gt1=sum(o["n_user"] > 1 for o in out),
            user_tok_med=statistics.median(o["user_tok"] for o in out))
for name in ("S1", "L"):
    xs = [o[name] for o in out if name in o]
    summ[name] = dict(n=len(xs), has_error=sum(x["has_error"] for x in xs),
                      suite_status={s: sum(1 for x in xs if x["suite_status"] == s)
                                    for s in {x["suite_status"] for x in xs}},
                      script_status={s: sum(1 for x in xs if x["script_status"] == s)
                                     for s in {x["script_status"] for x in xs}},
                      files_ge1=sum(x["n_files"] >= 1 for x in xs),
                      pi_span_med=statistics.median(x["pi_span_tok"] for x in xs),
                      pi_tail_med=statistics.median(x["pi_tail_tok"] for x in xs),
                      truth_keys=xs[0]["truth_keys"] if xs else None)
(OUT / "swe_long_detail.json").write_text(json.dumps(dict(summary=summ, rows=out), indent=1))
print(json.dumps(summ, indent=1))
